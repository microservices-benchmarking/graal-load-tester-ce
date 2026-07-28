#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FILE="$ROOT/gos_harness/config/local_configs.py"

[[ -f "$FILE" ]] || {
    echo "Missing $FILE" >&2
    exit 1
}

if grep -q '"activation": {' "$FILE" \
    && grep -q 'env_vars = {key:' "$FILE"; then
    echo "New tinyinit schema is already enabled."
    exit 0
fi

git -C "$ROOT" apply - <<'PATCH'
diff --git a/gos_harness/config/local_configs.py b/gos_harness/config/local_configs.py
--- a/gos_harness/config/local_configs.py
+++ b/gos_harness/config/local_configs.py
@@ -7,8 +7,10 @@ from gos_harness import scenario_parser
 TINYINIT_CONFIG_TEMPLATE: Template = Template("""
 {
-  "sock_path": "${socket_file}",
   "cmdline": [${command}],
-  "activation_type": "${app_type}",
+  "activation": {
+    "type": "${app_type}",
+    "sock_path": "${socket_file}"
+  },
   "env_vars": ${env_vars},
   "profiling": true
 }
@@ -105,11 +107,11 @@ def build_tinyinit_config(src_dir: str, command: str, socket_file: str, app_type
     os.mkdir(config_dir)
     config_file = os.path.join(config_dir, "config.json")
     with open(config_file, 'w') as config_handle:
-        env_vars = [[key, str(value)] for key, value in (app_env or {}).items()]
+        env_vars = {key: str(value) for key, value in (app_env or {}).items()}
         host_bridge_python = os.getenv("HOST_BRIDGE_PYTHON")
         if host_bridge_python:
             # Propagates the host bridge interpreter through tinyinit so Firecracker activations use the requested Python.
-            env_vars.append(["HOST_BRIDGE_PYTHON", host_bridge_python])
+            env_vars["HOST_BRIDGE_PYTHON"] = host_bridge_python
         tinyinit_content = TINYINIT_CONFIG_TEMPLATE.substitute(
             socket_file=socket_file,
             # Command is split by shlex: "./mybin -param1 value" -> ".mybin", "-param1", "value"
PATCH

echo "Enabled the tinyinit 0.1.32+ activation schema in $FILE"
