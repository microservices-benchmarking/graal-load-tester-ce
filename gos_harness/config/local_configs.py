from __future__ import annotations

import json
import os
import shlex
from string import Template
from gos_harness import gos_logging as logging
from gos_harness import scenario_parser

TINYINIT_CONFIG_TEMPLATE: Template = Template("""
{
  "cmdline": [${command}],
  "activation": {
    "type": "${app_type}",
    "sock_path": "${socket_file}"
  },
  "env_vars": ${env_vars},
  "profiling": true
}
""")
NGINX_CONFIG_TEMPLATE: Template = Template("""
worker_processes 1;
pid ${nginx_prefix}.pid;
error_log stderr;

events {
  worker_connections 512;
}

http {
  default_type application/octet-stream;

  keepalive_timeout ${nginx_keepalive_timeout};

  proxy_http_version 1.1;

  server {
    server_name _;
    listen ${port} default;

    # Set these to locations accessible without sudo.
    access_log ${nginx_dir}/nginx_host.access.log;
    client_body_temp_path ${nginx_dir}/client_body;
    fastcgi_temp_path ${nginx_dir}/fastcgi_temp;
    proxy_temp_path ${nginx_dir}/proxy_temp;
    scgi_temp_path ${nginx_dir}/scgi_temp;
    uwsgi_temp_path ${nginx_dir}/uwsgi_temp;

    location /heartbeat {
      add_header Content-Type text/plain;
      return 200 'nginx is  up and running';
    }

    location / {
      proxy_pass http://${proxy_server};
      proxy_set_header Host $$host;
      proxy_set_header Connection "";
      proxy_set_header X-OCI-GRAAL-Request-Start "$$msec";
      proxy_send_timeout 60s;
      proxy_read_timeout 60s;
      send_timeout 60s;
    }
  }

  upstream ${proxy_server} {
    keepalive ${nginx_keepalive};
    keepalive_timeout ${nginx_keepalive_timeout};
    keepalive_requests ${nginx_keepalive_requests};
    server unix:${socket_file};
  }
}
""")
GRAALHOST_FSMAPPINGS_ENTRY_TEMPLATE = Template(""",
    {"concrete": "${host_path}", "virt": "${graalhost_path}", "mutable": true}"""
)
GRAALHOST_VERIFIED_FSMAPPINGS_ENTRY_TEMPLATE = Template(""",
    {"concrete": "${host_path}", "virt": "${graalhost_path}", "verif": true}"""
)
# Match the POSIX deployment's runtime resources without exposing the host root.
GRAALHOST_CONFIG_TEMPLATE: Template = Template("""
{
  "fds": {
    "stdin": "stdin",
    "stdout": "stdout",
    "stderr": "log"
  },
  "fsmappings": [
    {"concrete": "/proc/self/maps", "virt": "/proc/self/maps"},
    {"concrete": "/proc/net/if_inet6", "virt": "/proc/net/if_inet6"},
    {"concrete": "/proc/stat", "virt": "/proc/stat"},
    {"concrete": ${os_release_path}, "virt": "/etc/os-release"},
    {"concrete": "/usr/share/zoneinfo", "virt": "/usr/share/zoneinfo"},
    {"concrete": "/dev/null", "virt": "/dev/null", "mutable": true},
    {"concrete": "/dev/random", "virt": "/dev/random"},
    {"concrete": "/dev/urandom", "virt": "/dev/urandom"}${extra_fsmappings}
  ],
  "env": ${env_vars},
  "testing_default_mappings":true,
  "working_dir": "/",
  "fd_limit": 4096,
  "ep": "21478959648604160"
}
""")

def _parse_graalhost_mount(mount: str) -> tuple[str, str]:
    host_path, separator, graalhost_path = mount.partition("=")
    if not separator:
        graalhost_path = host_path
    return host_path, graalhost_path

def build_tinyinit_config(src_dir: str, command: str, socket_file: str, app_type: str, app_env: dict[str, object] | None = None) -> str:
    config_dir = os.path.join(src_dir, "tinyinit_configs")
    os.mkdir(config_dir)
    config_file = os.path.join(config_dir, "config.json")
    with open(config_file, 'w') as config_handle:
        env_vars = {key: str(value) for key, value in (app_env or {}).items()}
        host_bridge_python = os.getenv("HOST_BRIDGE_PYTHON")
        if host_bridge_python:
            # Propagates the host bridge interpreter through tinyinit so Firecracker activations use the requested Python.
            env_vars["HOST_BRIDGE_PYTHON"] = host_bridge_python
        tinyinit_content = TINYINIT_CONFIG_TEMPLATE.substitute(
            socket_file=socket_file,
            # Command is split by shlex: "./mybin -param1 value" -> ".mybin", "-param1", "value"
            command=", ".join(f'"{c}"' for c in shlex.split(command)),
            app_type=app_type,
            env_vars=json.dumps(env_vars)
        )
        logging.log_file_content(config_file, tinyinit_content)
        config_handle.write(tinyinit_content)
    return config_dir

def build_nginx_config(
    app_name: str,
    src_dir: str,
    nginx_prefix: str,
    socket_file: str,
    port: int,
    keepalive: int = 5,
    keepalive_timeout: int | str = 5,
    keepalive_requests: int = 10_000_000,
) -> str:
    config_file = os.path.join(src_dir, "nginx.conf")
    with open(config_file, 'w') as config_handle:
        nginx_content = NGINX_CONFIG_TEMPLATE.substitute(
            nginx_prefix=nginx_prefix,
            socket_file=socket_file,
            nginx_dir=src_dir,
            port=port,
            proxy_server=f"{app_name.replace('-', '_')}_server",
            nginx_keepalive=keepalive,
            nginx_keepalive_timeout=keepalive_timeout,
            nginx_keepalive_requests=keepalive_requests
        )
        logging.log_file_content(config_file, nginx_content)
        config_handle.write(nginx_content)
    return config_file

def _graalhost_verified_library_mappings(app_env: dict[str, object] | None) -> list[str]:
    library_path = (app_env or {}).get("LD_LIBRARY_PATH")
    if not library_path:
        return []

    mappings: list[str] = []
    seen_paths: set[str] = set()
    for library_dir in str(library_path).split(os.pathsep):
        if not library_dir or not os.path.isabs(library_dir) or not os.path.isdir(library_dir):
            continue
        for library_name in sorted(os.listdir(library_dir)):
            if not (library_name.endswith(".so") or ".so." in library_name):
                continue
            library_file = os.path.join(library_dir, library_name)
            if not os.path.isfile(library_file) or library_file in seen_paths:
                continue
            seen_paths.add(library_file)
            mappings.append(library_file)
    return mappings

def build_graalhost_config(src_dir: str, config: scenario_parser.LocalBinaryConfig) -> str:
    if isinstance(config, scenario_parser.LocalBinaryConfig) and config.graalhost_config_file is not None:
        return config.graalhost_config_file
    extra_dir_mounts = config.extra_graalhost_dir_mounts
    config_file = os.path.join(src_dir, "graalhost-config.json")
    with open(config_file, 'w') as config_handle:
        extra_fsmappings = ""
        if extra_dir_mounts is not None and len(extra_dir_mounts) > 0:
            parsed_mounts = [_parse_graalhost_mount(mount) for mount in extra_dir_mounts]
            extra_fsmappings = ''.join([
                GRAALHOST_FSMAPPINGS_ENTRY_TEMPLATE.substitute(
                    host_path=host_path,
                    graalhost_path=graalhost_path
                ) for host_path, graalhost_path in parsed_mounts
            ])
        env_vars = {
            **{key: str(value) for key, value in (config.environment or {}).items()},
            "TINYINIT_ACTIVATION": "%{TINYINIT_ACTIVATION}",
            "TINYINIT_FORK": "%{TINYINIT_FORK}",
            "TINYINIT_EXEC": "%{TINYINIT_EXEC}",
        }
        verified_library_mappings = ''.join([
            GRAALHOST_VERIFIED_FSMAPPINGS_ENTRY_TEMPLATE.substitute(
                host_path=library_path,
                graalhost_path=library_path
            ) for library_path in _graalhost_verified_library_mappings(config.environment)
        ])
        graalhost_content = GRAALHOST_CONFIG_TEMPLATE.substitute(
            os_release_path=json.dumps(os.path.realpath("/etc/os-release")),
            extra_fsmappings=f"{extra_fsmappings}{verified_library_mappings}",
            env_vars=json.dumps(env_vars, indent=4)
        )
        logging.log_file_content(config_file, graalhost_content)
        config_handle.write(graalhost_content)
    return config_file
