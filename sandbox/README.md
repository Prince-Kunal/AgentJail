# Sandbox (plan §7)

The test agent, its tools, its DB, and the Cedar **PEP** run inside the sandbox.
The orchestrator and attacker stay on the host and reach the target only over
HTTP (**D1**). Cedar denies *authorized-but-wrong* actions; the sandbox contains
anything that gets past it. Two independent layers — that is the pitch.

Only the target ships in: `sandbox/Dockerfile` copies `siege/target/` plus the
shared modules it imports (`llm.py`, `config.py`, `cedar/schema.cedarschema`) and
**nothing from `orchestrator/` or the attacker** (§9.1).

## Docker (default — works on macOS and Linux)

```sh
./sandbox/run.sh                 # builds + starts the hardened container, prints the canary
./sandbox/check.sh               # verifies the hardening of the running container

# then drive it from the host (use the canary run.sh printed):
SIEGE_CANARY=<printed> siege demo --no-launch --target-model qwen2.5:7b
docker rm -f siege-target        # stop
```

`--no-launch` tells the orchestrator to use the already-running container instead
of launching a subprocess; `TARGET_URL` stays `http://127.0.0.1:8100`, so
"nothing else changes" (§7). The canary must match, which is why `run.sh` prints
it and the orchestrator reads `SIEGE_CANARY`.

**Hardening (`run.sh`, §7):** non-root user, **all capabilities dropped**,
**read-only root filesystem**, a small in-memory **tmpfs for `/tmp`** (the DB is
in-memory, so nothing is persisted — there is no DB volume to mount), **no host
mounts**, `no-new-privileges`, pid/memory limits, and the port published on
**loopback only**. `check.sh` confirms each of these on the running container.

**Egress.** The container runs on a bridge with IP-masquerade disabled so it can
reach the host (Ollama) and be reached by the orchestrator, but not NAT out to
the internet. This blocks internet egress **on a Linux host**. On **Docker
Desktop (macOS)** the LinuxKit VM applies its *own* outbound NAT, which Docker's
bridge options can't switch off, so the internet stays reachable there — `check.sh`
reports this as a `WARN`. On macOS the sandbox therefore relies on the
filesystem/process hardening above (and optionally gVisor, `--runtime=runsc`);
strict egress allow-listing needs a Linux host or a Linux KVM microVM. This is
the documented Docker trade-off (§7: "the only thing lost is the hardware-isolated
line").

## Firecracker (Linux KVM host only)

Firecracker needs Linux with **KVM** (`/dev/kvm`); it does **not** run on macOS.

```sh
sudo sandbox/firecracker/build-rootfs.sh            # reuses the Docker image -> rootfs.ext4
sudo KERNEL=/path/to/vmlinux sandbox/firecracker/launch.sh   # tap networking + boot

# target comes up at 172.16.0.2:8100; drive it from the host:
SIEGE_TARGET_URL=http://172.16.0.2:8100 SIEGE_CANARY=<from launch.sh> siege demo --no-launch
```

- `build-rootfs.sh` exports the same minimal target image into an ext4 rootfs and
  adds an `/init` that brings up networking and starts the service.
- `vmconfig.json` is the microVM config (kernel, rootfs, one tap NIC, 1 vCPU /
  512 MiB); `launch.sh` fills its placeholders and boots Firecracker.
- You supply an uncompressed `vmlinux` (e.g. the kernel from the Firecracker
  quickstart/CI, or a locally built one) via `KERNEL`.

Kata Containers is a middle ground (Firecracker-backed isolation, container UX).

## Decision of record (Phase 7 exit)

**No Linux KVM host is available for this project, so the sandbox of record is
Docker.** `siege demo` passes against the Docker sandbox (verified: attack → fix →
rerun → report, with the PEP enforcing inside the container). The Firecracker
scripts above are provided and documented for a Linux KVM host but are **untested
on the macOS dev machine** (no `/dev/kvm`). Confirm `/dev/kvm` on any future demo
host before relying on the Firecracker path.
