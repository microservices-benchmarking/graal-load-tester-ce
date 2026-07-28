#!/usr/bin/env python3
#
# Purpose:
#   Host-side bridge used by the Graal Load Tester Firecracker local deployment.
# 
# What it does:
#   Starts Firecracker from a generated config, connects to the guest shim over
#   vsock, buffers stdin while the VM boots (otherwise, if we start relaying the
#   bytes immediately, the stdin in the firecracker would be mangled), forwards 
#   request bytes into the guest, and streams the guest response back to stdout.
#
#   This layer is needed, to maintain the communication between tinyinit and
#   the application running in the firecracker: the communication through stdin
#   and stdout is not feasible, hence we need to use vsock.
#
#   Meaning we maintain following link:
#     nginx <-> tinyinit <-> host_bridge.py <-> [ guest_shim <-> app ]
#                                               ^^^^ firecracker  ^^^^
#
# Note:
#   This file was initially generated with AI assistance and then integrated
#   into the repository workflow.
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

DEVNULL_WRITER = open(os.devnull, "wb", buffering=0)

READY_BANNER = b"FC_READY\n"
DEFAULT_EOF_DELAY_SEC = 0.25
HTTP_PREFIX = b"HTTP/"


class BridgeError(RuntimeError):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def debug_log(message: str) -> None:
    if os.environ.get("FC_DEBUG", ""):
        eprint(f"[bridge] {message}")


def preview_bytes(data: bytes, limit: int = 96) -> str:
    sample = data[:limit]
    text = sample.decode("utf-8", errors="replace")
    return repr(text)


def default_api_socket(config_path: Path) -> str:
    digest = hashlib.sha256(str(config_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"/tmp/fc-api-{digest}.sock"


def unlink_if_exists(path: str | Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def write_runtime_config(config_path: Path, vsock_uds: str) -> Path:
    with config_path.open("r", encoding="utf-8") as src:
        config = json.load(src)
    config.setdefault("vsock", {})["uds_path"] = vsock_uds

    digest = hashlib.sha256(f"{config_path.resolve()}::{os.getpid()}::{vsock_uds}".encode("utf-8")).hexdigest()[:16]
    runtime_path = Path(f"/tmp/fc-config-{digest}.json")
    with runtime_path.open("w", encoding="utf-8") as dst:
        json.dump(config, dst)
    return runtime_path


def read_until_newline(sock: socket.socket, timeout: float) -> bytes:
    sock.settimeout(timeout)
    data = bytearray()
    while b"\n" not in data:
        chunk = sock.recv(1)
        if not chunk:
            raise BridgeError("unexpected EOF while waiting for Firecracker vsock handshake")
        data.extend(chunk)
    return bytes(data)


def read_exact_ready_banner(sock: socket.socket, timeout: float) -> None:
    sock.settimeout(timeout)
    data = bytearray()
    while len(data) < len(READY_BANNER):
        chunk = sock.recv(len(READY_BANNER) - len(data))
        if not chunk:
            raise BridgeError("unexpected EOF while waiting for guest ready banner")
        data.extend(chunk)
    if bytes(data) != READY_BANNER:
        raise BridgeError(f"unexpected guest ready banner: {bytes(data)!r}")
    sock.settimeout(None)


def connect_vsock_tunnel(uds_path: str, guest_port: int, timeout_sec: float) -> socket.socket:
    deadline = time.time() + timeout_sec
    last_error: Exception | None = None

    while time.time() < deadline:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(uds_path)
            sock.sendall(f"CONNECT {guest_port}\n".encode("ascii"))
            response = read_until_newline(sock, 1.0).decode("utf-8", errors="replace").strip()
            if response.startswith("OK "):
                sock.settimeout(None)
                return sock
            sock.close()
            last_error = BridgeError(f"unexpected Firecracker vsock response: {response!r}")
        except (FileNotFoundError, ConnectionRefusedError, socket.timeout, OSError, BridgeError) as exc:
            last_error = exc
            try:
                if sock is not None:
                    sock.close()
            except Exception:
                pass
            time.sleep(0.1)

    raise BridgeError(f"could not connect to Firecracker vsock tunnel at {uds_path}: {last_error}")


def stdin_reader(stdin, chunks: queue.Queue[bytes | None], stop_event: threading.Event) -> None:
    fd = stdin.fileno()
    try:
        while not stop_event.is_set():
            data = os.read(fd, 65536)
            if not data:
                debug_log("stdin reached EOF")
                break
            debug_log(f"stdin -> queue {len(data)} bytes: {preview_bytes(data)}")
            chunks.put(data)
    finally:
        chunks.put(None)


def guest_writer(
    sock: socket.socket,
    chunks: queue.Queue[bytes | None],
    stop_event: threading.Event,
) -> None:
    eof_delay_sec = float(os.environ.get("FC_EOF_DELAY_SEC", str(DEFAULT_EOF_DELAY_SEC)))
    write_side_closed = False
    try:
        while not stop_event.is_set():
            item = chunks.get()
            if item is None:
                # Redirected files hit EOF immediately. Delay the half-close a bit
                # so the guest app sees a stable request stream first, then close
                # stdin to allow apps that terminate on end-of-input to exit.
                if eof_delay_sec > 0:
                    time.sleep(eof_delay_sec)
                if not write_side_closed:
                    try:
                        sock.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    write_side_closed = True
                debug_log("guest stdin half-closed after stdin EOF")
                return
            debug_log(f"queue -> guest {len(item)} bytes: {preview_bytes(item)}")
            sock.sendall(item)
    except OSError:
        stop_event.set()


def guest_reader(sock: socket.socket, stdout, stop_event: threading.Event) -> None:
    sync_to_http = os.environ.get("FC_SYNC_TO_HTTP", "1") not in ("0", "false", "False", "no", "NO")
    pending = bytearray()
    sink = stdout

    def write_output(data: bytes) -> bool:
        nonlocal sink
        try:
            sink.write(data)
            sink.flush()
            return True
        except (BrokenPipeError, ValueError):
            if sink is not DEVNULL_WRITER:
                debug_log("host stdout closed; draining guest output to /dev/null")
                sink = DEVNULL_WRITER
            return False

    try:
        while True:
            data = sock.recv(65536)
            if not data:
                if pending:
                    debug_log(f"guest -> host EOF with buffered non-HTTP bytes: {preview_bytes(bytes(pending))}")
                else:
                    debug_log("guest -> host EOF")
                break
            debug_log(f"guest -> host {len(data)} bytes: {preview_bytes(data)}")
            if not sync_to_http:
                write_output(data)
                continue

            pending.extend(data)
            idx = pending.find(HTTP_PREFIX)
            if idx >= 0:
                write_output(pending[idx:])
                debug_log(f"forwarded HTTP response starting at buffered offset {idx}")
                sync_to_http = False
                pending.clear()
            elif len(pending) >= len(HTTP_PREFIX):
                debug_log(f"buffered pre-HTTP bytes so far: {preview_bytes(bytes(pending))}")
    except OSError:
        stop_event.set()


def launch_firecracker(
    firecracker_bin: str,
    api_sock: str,
    config_json: str,
    stdout_target,
    stderr_target,
) -> subprocess.Popen[bytes]:
    unlink_if_exists(api_sock)
    return subprocess.Popen(
        [firecracker_bin, "--api-sock", api_sock, "--config-file", config_json],
        stdin=subprocess.DEVNULL,
        stdout=stdout_target,
        stderr=stderr_target,
    )


def wait_for_process_exit(proc: subprocess.Popen[bytes], timeout_sec: float) -> int | None:
    try:
        return proc.wait(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side stdin/stdout bridge for Firecracker vsock guest shim.")
    parser.add_argument("--config", required=True, help="Path to firecracker.json")
    parser.add_argument("--vsock-uds", required=True, help="Host Unix socket path used by Firecracker vsock device")
    parser.add_argument("--guest-port", type=int, default=9000, help="Guest vsock port the shim listens on")
    parser.add_argument("--firecracker-bin", default=shutil.which("firecracker") or "firecracker", help="Firecracker binary")
    parser.add_argument("--api-sock", default=None, help="Short Firecracker API socket path")
    parser.add_argument("--connect-timeout", type=float, default=20.0, help="Seconds to wait for guest vsock readiness")
    parser.add_argument("--firecracker-stderr", default="inherit", choices=["inherit", "drop"], help="What to do with Firecracker stderr logs")
    parser.add_argument("--debug", action="store_true", help="Print bridge progress to stderr")
    parser.add_argument("--show-serial", action="store_true", help="Forward Firecracker stdout (guest serial) to stderr")
    return parser.parse_args()


def forward_stream(prefix: str, src, stop_event: threading.Event) -> None:
    try:
        while not stop_event.is_set():
            line = src.readline()
            if not line:
                break
            try:
                decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            except AttributeError:
                decoded = str(line).rstrip("\n")
            eprint(f"{prefix}{decoded}")
    except Exception as exc:
        if not stop_event.is_set():
            eprint(f"{prefix}<stream error: {exc}>")


def main() -> int:
    args = parse_args()
    config_path = Path(args.config)
    api_sock = args.api_sock or default_api_socket(config_path)
    runtime_config_path = write_runtime_config(config_path, args.vsock_uds)
    stderr_target = None if args.firecracker_stderr == "inherit" else subprocess.DEVNULL
    stdout_target = subprocess.PIPE if args.show_serial else subprocess.DEVNULL

    stdin_chunks: queue.Queue[bytes | None] = queue.Queue()
    stop_event = threading.Event()

    reader_thread = threading.Thread(
        target=stdin_reader,
        args=(sys.stdin.buffer, stdin_chunks, stop_event),
        daemon=True,
    )
    reader_thread.start()

    if args.debug:
        eprint(f"[bridge] config={args.config}")
        eprint(f"[bridge] runtime_config={runtime_config_path}")
        eprint(f"[bridge] api_sock={api_sock}")
        eprint(f"[bridge] vsock_uds={args.vsock_uds}")
        eprint(f"[bridge] guest_port={args.guest_port}")

    unlink_if_exists(args.vsock_uds)
    proc = launch_firecracker(args.firecracker_bin, api_sock, str(runtime_config_path), stdout_target, stderr_target)

    serial_thread = None
    if args.show_serial and proc.stdout is not None:
        serial_thread = threading.Thread(
            target=forward_stream,
            args=("[serial] ", proc.stdout, stop_event),
            daemon=True,
        )
        serial_thread.start()

    if args.debug:
        eprint("[bridge] firecracker launched; waiting for guest vsock")

    def terminate(*_args) -> None:
        stop_event.set()
        if proc.poll() is None:
            proc.terminate()

    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)

    try:
        guest_sock = connect_vsock_tunnel(args.vsock_uds, args.guest_port, args.connect_timeout)
    except Exception:
        if args.debug:
            eprint("[bridge] failed to connect to guest vsock")
        terminate()
        rc = wait_for_process_exit(proc, 2.0)
        if rc is None:
            proc.kill()
        raise

    if args.debug:
        eprint("[bridge] connected to guest vsock")

    try:
        read_exact_ready_banner(guest_sock, 5.0)
    except Exception:
        if args.debug:
            eprint("[bridge] failed while waiting for guest ready banner")
        terminate()
        rc = wait_for_process_exit(proc, 2.0)
        if rc is None:
            proc.kill()
        raise

    if args.debug:
        eprint("[bridge] guest ready banner received")

    writer_thread = threading.Thread(
        target=guest_writer,
        args=(guest_sock, stdin_chunks, stop_event),
        daemon=True,
    )
    output_thread = threading.Thread(
        target=guest_reader,
        args=(guest_sock, sys.stdout.buffer, stop_event),
        daemon=True,
    )
    writer_thread.start()
    output_thread.start()

    output_thread.join()
    stop_event.set()
    if args.debug:
        eprint("[bridge] guest output stream ended")
    try:
        guest_sock.close()
    except OSError:
        pass
    writer_thread.join(timeout=1.0)

    rc = wait_for_process_exit(proc, 2.0)
    if rc is None:
        proc.terminate()
        rc = wait_for_process_exit(proc, 2.0)
    if rc is None:
        proc.kill()
        rc = proc.wait(timeout=2.0)

    if serial_thread is not None:
        serial_thread.join(timeout=1.0)
    if args.debug:
        eprint(f"[bridge] firecracker exited rc={rc}")

    try:
        sys.stdout.flush()
    except BrokenPipeError:
        try:
            sys.stdout.close()
        except Exception:
            pass
        sys.stdout = open(os.devnull, "w")

    unlink_if_exists(runtime_config_path)
    unlink_if_exists(api_sock)
    unlink_if_exists(args.vsock_uds)

    return 0 if rc == 0 else rc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BridgeError as exc:
        print(f"bridge error: {exc}", file=sys.stderr)
        raise SystemExit(1)
