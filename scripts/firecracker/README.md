# Firecracker Prototype V1

This directory is the cleaned source-of-truth for the local `nginx -> tinyinit -> firecracker` prototype.

It keeps the earlier exploratory files in the parent directory intact, but the files here are the ones to use for further iteration.

## Layout

- `guest_shim.c`
  Guest `/init`. It accepts one host bridge connection over vsock and runs `/app` on stdin/stdout.

- `host_bridge.py`
  Host-side child process for `tinyinit`. It starts reading stdin immediately, boots Firecracker, bridges bytes to the guest, and writes the guest response back to stdout.

- `make-firecracker-bundle.sh`
  Rebuilds the runnable bundle into `out/`.

## Rebuild

```bash
./scripts/firecracker/make-firecracker-bundle.sh \
  --binary /path/to/app \
  --kernel /path/to/vmlinux \
  --out-dir ./scripts/firecracker/out
```

## Notes

- Generated artifacts belong under `./scripts/firecracker/out/`.
- Requests are expected to be supplied by the caller of `run-bridge.sh`.
- The bridge handles closed host stdout without aborting the guest-side response path.
