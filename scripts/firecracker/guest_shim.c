/*
 * Purpose:
 *   Guest-side shim used by the Graal Load Tester Firecracker local deployment.
 *
 * What it does:
 *   Boots as guest /init, accepts a single host bridge connection over vsock,
 *   launches /app with stdin/stdout connected to that stream, and relays bytes
 *   between the host bridge and the guest application.
 *
 *   This layer is needed, to maintain the communication between tinyinit and
 *   the application running in the firecracker: the communication through stdin
 *   and stdout is not feasible, hence we need to use vsock.
 *
 *   Meaning we maintain following link:
 *     nginx <-> tinyinit <-> host_bridge.py <-> [ guest_shim <-> app ]
 *                                               ^^^^ firecracker  ^^^^
 *
 * Note:
 *   This file was initially generated with AI assistance and then integrated
 *   into the repository workflow.
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <sys/stat.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <linux/vm_sockets.h>

#define DEFAULT_PORT 9000
#define DEFAULT_APP "/app"
#define DEFAULT_APP_ARGV_FILE "/etc/firecracker-app-argv"
#define BUFFER_SIZE 65536
#define READY_BANNER "FC_READY\n"

static void die(const char *message) {
    perror(message);
    _exit(111);
}

static bool debug_enabled = false;

static void debug_log(const char *fmt, ...) {
    if (!debug_enabled) {
        return;
    }

    char buffer[1024];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(buffer, sizeof(buffer), fmt, args);
    va_end(args);
    if (n > 0) {
        dprintf(STDERR_FILENO, "[guest-shim] %s\n", buffer);
    }
}

static int write_all(int fd, const void *buf, size_t count) {
    const char *ptr = (const char *)buf;
    while (count > 0) {
        ssize_t written = write(fd, ptr, count);
        if (written < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }
        ptr += written;
        count -= (size_t)written;
    }
    return 0;
}

static void mount_guest_filesystems(void) {
    /*
     * Only /proc is required here so procfs-based runtime checks inside the guest keep working.
     *   - this mount is required for newer native images
     */
    if (mount("proc", "/proc", "proc", 0, "") != 0 && errno != EBUSY) {
        die("mount(/proc)");
    }
}

static char **build_default_argv(const char *app_path) {
    char **argv = malloc(2 * sizeof(char *));
    if (argv == NULL) {
        errno = ENOMEM;
        die("malloc(argv)");
    }
    argv[0] = (char *)app_path;
    argv[1] = NULL;
    return argv;
}

static char **load_app_argv(const char *app_path) {
    /*
     * The argv file is written by the host as NUL-delimited strings; prepend app_path as argv[0].
     *  - this makes sure that applications such as micronaut pegasus can use the launch arguments.
     *  - the parsing should not introduce considerable overhead (compared to e.g. hardcoded arguments).
     */
    int fd = open(DEFAULT_APP_ARGV_FILE, O_RDONLY);
    if (fd < 0) {
        if (errno == ENOENT) {
            return build_default_argv(app_path);
        }
        die("open(app argv file)");
    }

    struct stat st;
    if (fstat(fd, &st) != 0) {
        close(fd);
        die("fstat(app argv file)");
    }
    if (st.st_size == 0) {
        close(fd);
        return build_default_argv(app_path);
    }

    size_t size = (size_t)st.st_size;
    char *buffer = malloc(size + 1);
    if (buffer == NULL) {
        close(fd);
        errno = ENOMEM;
        die("malloc(app argv buffer)");
    }

    size_t offset = 0;
    while (offset < size) {
        ssize_t n = read(fd, buffer + offset, size - offset);
        if (n < 0) {
            if (errno == EINTR) {
                continue;
            }
            close(fd);
            die("read(app argv file)");
        }
        if (n == 0) {
            break;
        }
        offset += (size_t)n;
    }
    close(fd);
    buffer[offset] = '\0';

    size_t argc = 1;
    for (size_t i = 0; i < offset;) {
        if (buffer[i] == '\0') {
            i++;
            continue;
        }
        argc++;
        while (i < offset && buffer[i] != '\0') {
            i++;
        }
    }

    char **argv = malloc((argc + 1) * sizeof(char *));
    if (argv == NULL) {
        errno = ENOMEM;
        die("malloc(argv)");
    }

    size_t argi = 0;
    argv[argi++] = (char *)app_path;
    for (size_t i = 0; i < offset;) {
        if (buffer[i] == '\0') {
            i++;
            continue;
        }
        argv[argi++] = &buffer[i];
        while (i < offset && buffer[i] != '\0') {
            i++;
        }
    }
    argv[argi] = NULL;
    return argv;
}

static int parse_cmdline_value(const char *key, char *out, size_t out_size) {
    FILE *fp = fopen("/proc/cmdline", "r");
    if (fp == NULL) {
        return -1;
    }

    char line[4096];
    if (fgets(line, sizeof(line), fp) == NULL) {
        fclose(fp);
        return -1;
    }
    fclose(fp);

    char *saveptr = NULL;
    for (char *token = strtok_r(line, " \n", &saveptr);
         token != NULL;
         token = strtok_r(NULL, " \n", &saveptr)) {
        size_t key_len = strlen(key);
        if (strncmp(token, key, key_len) == 0 && token[key_len] == '=') {
            const char *value = token + key_len + 1;
            snprintf(out, out_size, "%s", value);
            return 0;
        }
    }
    return -1;
}

static uint16_t parse_port(void) {
    char value[64];
    if (parse_cmdline_value("shim.port", value, sizeof(value)) != 0) {
        return DEFAULT_PORT;
    }
    char *end = NULL;
    long parsed = strtol(value, &end, 10);
    if (end == value || *end != '\0' || parsed <= 0 || parsed > 65535) {
        return DEFAULT_PORT;
    }
    return (uint16_t)parsed;
}

static void parse_app_path(char *out, size_t out_size) {
    if (parse_cmdline_value("shim.app", out, out_size) != 0) {
        snprintf(out, out_size, "%s", DEFAULT_APP);
    }
}

static bool parse_debug_enabled(void) {
    char value[64];
    if (parse_cmdline_value("shim.debug", value, sizeof(value)) != 0) {
        return false;
    }
    return strcmp(value, "1") == 0 || strcmp(value, "true") == 0 || strcmp(value, "yes") == 0;
}

static int create_vsock_listener(uint16_t port) {
    int fd = socket(AF_VSOCK, SOCK_STREAM, 0);
    if (fd < 0) {
        die("socket(AF_VSOCK)");
    }

    struct sockaddr_vm addr;
    memset(&addr, 0, sizeof(addr));
    addr.svm_family = AF_VSOCK;
    addr.svm_cid = VMADDR_CID_ANY;
    addr.svm_port = port;

    if (bind(fd, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        close(fd);
        die("bind(AF_VSOCK)");
    }
    if (listen(fd, 1) < 0) {
        close(fd);
        die("listen(AF_VSOCK)");
    }
    return fd;
}

static pid_t spawn_child(const char *app_path, int *child_stdin, int *child_stdout) {
    int stdin_pipe[2];
    int stdout_pipe[2];
    if (pipe(stdin_pipe) < 0) {
        die("pipe(stdin)");
    }
    if (pipe(stdout_pipe) < 0) {
        close(stdin_pipe[0]);
        close(stdin_pipe[1]);
        die("pipe(stdout)");
    }

    pid_t child = fork();
    if (child < 0) {
        close(stdin_pipe[0]);
        close(stdin_pipe[1]);
        close(stdout_pipe[0]);
        close(stdout_pipe[1]);
        die("fork");
    }

    if (child == 0) {
        if (dup2(stdin_pipe[0], STDIN_FILENO) < 0) {
            die("dup2(stdin)");
        }
        if (dup2(stdout_pipe[1], STDOUT_FILENO) < 0) {
            die("dup2(stdout)");
        }

        close(stdin_pipe[0]);
        close(stdin_pipe[1]);
        close(stdout_pipe[0]);
        close(stdout_pipe[1]);

        struct timespec ts;
        if (clock_gettime(CLOCK_REALTIME, &ts) == 0) {
            char app_start_ms[64];
            snprintf(app_start_ms, sizeof(app_start_ms), "%lld",
                     (long long)ts.tv_sec * 1000LL + (long long)ts.tv_nsec / 1000000LL);
            setenv("FC_SHIM_APP_START_MS", app_start_ms, 1);
        }

        char **argv = load_app_argv(app_path);
        execv(app_path, argv);
        die("execv(app)");
    }

    close(stdin_pipe[0]);
    close(stdout_pipe[1]);
    *child_stdin = stdin_pipe[1];
    *child_stdout = stdout_pipe[0];
    return child;
}

static int pump_between(int conn_fd, int child_stdin, int child_stdout) {
    bool conn_open = true;
    bool child_open = true;
    bool child_stdin_open = true;
    char buffer[BUFFER_SIZE];

    while (conn_open || child_open) {
        struct pollfd pfds[2];
        nfds_t nfds = 0;

        if (conn_open) {
            pfds[nfds].fd = conn_fd;
            pfds[nfds].events = POLLIN | POLLHUP;
            pfds[nfds].revents = 0;
            nfds++;
        }
        if (child_open) {
            pfds[nfds].fd = child_stdout;
            pfds[nfds].events = POLLIN | POLLHUP;
            pfds[nfds].revents = 0;
            nfds++;
        }

        int rc = poll(pfds, nfds, -1);
        if (rc < 0) {
            if (errno == EINTR) {
                continue;
            }
            return -1;
        }

        nfds_t idx = 0;
        if (conn_open) {
            short revents = pfds[idx].revents;
            idx++;
            if ((revents & POLLIN) != 0) {
                ssize_t n = read(conn_fd, buffer, sizeof(buffer));
                if (n < 0) {
                    if (errno != EINTR) {
                        return -1;
                    }
                } else if (n == 0) {
                    conn_open = false;
                    if (child_stdin_open) {
                        close(child_stdin);
                        child_stdin_open = false;
                    }
                } else {
                    if (child_stdin_open && write_all(child_stdin, buffer, (size_t)n) < 0) {
                        return -1;
                    }
                }
            } else if ((revents & (POLLHUP | POLLERR | POLLNVAL)) != 0) {
                conn_open = false;
                if (child_stdin_open) {
                    close(child_stdin);
                    child_stdin_open = false;
                }
            }
        }

        if (child_open) {
            short revents = pfds[idx].revents;
            if ((revents & POLLIN) != 0) {
                ssize_t n = read(child_stdout, buffer, sizeof(buffer));
                if (n < 0) {
                    if (errno != EINTR) {
                        return -1;
                    }
                } else if (n == 0) {
                    child_open = false;
                    close(child_stdout);
                    shutdown(conn_fd, SHUT_WR);
                } else {
                    if (write_all(conn_fd, buffer, (size_t)n) < 0) {
                        return -1;
                    }
                }
            } else if ((revents & (POLLHUP | POLLERR | POLLNVAL)) != 0) {
                child_open = false;
                close(child_stdout);
                shutdown(conn_fd, SHUT_WR);
            }
        }
    }

    if (child_stdin_open) {
        close(child_stdin);
    }
    return 0;
}

int main(void) {
    signal(SIGPIPE, SIG_IGN);
    mount_guest_filesystems();

    uint16_t port = parse_port();
    char app_path[1024];
    parse_app_path(app_path, sizeof(app_path));
    debug_enabled = parse_debug_enabled();
    debug_log("booted; listening on vsock port %u, app=%s", port, app_path);

    int listen_fd = create_vsock_listener(port);
    int conn_fd = accept(listen_fd, NULL, NULL);
    if (conn_fd < 0) {
        die("accept(AF_VSOCK)");
    }
    close(listen_fd);
    debug_log("accepted vsock connection");

    if (write_all(conn_fd, READY_BANNER, strlen(READY_BANNER)) < 0) {
        close(conn_fd);
        die("write(READY_BANNER)");
    }
    debug_log("sent ready banner");

    int child_stdin = -1;
    int child_stdout = -1;
    pid_t child = spawn_child(app_path, &child_stdin, &child_stdout);
    debug_log("spawned child pid=%d", child);

    int pump_rc = pump_between(conn_fd, child_stdin, child_stdout);
    close(conn_fd);

    int status = 0;
    waitpid(child, &status, 0);
    debug_log("child exited; pump_rc=%d", pump_rc);

    if (pump_rc != 0) {
        return 111;
    }
    if (WIFEXITED(status)) {
        return WEXITSTATUS(status);
    }
    if (WIFSIGNALED(status)) {
        return 128 + WTERMSIG(status);
    }
    return 111;
}
