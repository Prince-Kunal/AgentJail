#!/usr/bin/env bash
# Build a Firecracker rootfs containing only the Siege target (plan §7, P7.3).
#
# LINUX + KVM host only -- not runnable on macOS (Firecracker needs /dev/kvm; the
# dev machine uses Docker instead, see sandbox/README.md). This reuses the Docker
# image (sandbox/Dockerfile) as the single source of the target + its deps: it
# exports that image's filesystem into an ext4 rootfs and adds a tiny init that
# brings up networking and starts the service on boot.
#
# Requires: root (mount/mkfs/losetup), docker, mkfs.ext4. Produces rootfs.ext4.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repo root

[ "$(id -u)" = "0" ] || { echo "run as root (needs mount/mkfs): sudo $0" >&2; exit 1; }
command -v mkfs.ext4 >/dev/null || { echo "mkfs.ext4 not found (Linux only)" >&2; exit 1; }

IMAGE=${SIEGE_IMAGE:-siege-target}
ROOTFS=${ROOTFS:-sandbox/firecracker/rootfs.ext4}
SIZE_MB=${SIZE_MB:-768}

echo "==> building $IMAGE (same minimal target as Docker)"
docker build -f sandbox/Dockerfile -t "$IMAGE" .

echo "==> exporting the image filesystem into $ROOTFS (${SIZE_MB}M ext4)"
cid=$(docker create "$IMAGE")
trap 'docker rm -f "$cid" >/dev/null 2>&1 || true' EXIT
rm -f "$ROOTFS"
truncate -s "${SIZE_MB}M" "$ROOTFS"
mkfs.ext4 -F -q "$ROOTFS"
mnt=$(mktemp -d)
mount -o loop "$ROOTFS" "$mnt"
docker export "$cid" | tar -x -C "$mnt"

echo "==> installing init (network up, then start the service)"
cat > "$mnt/init" <<'INIT'
#!/bin/sh
# Firecracker PID 1: minimal bring-up, then exec the target service.
mount -t proc  proc /proc
mount -t sysfs sys  /sys
mount -t tmpfs tmpfs /tmp            # the only writable spot; the DB is in-memory

# The canary is passed on the kernel cmdline so the VM and orchestrator share it.
for tok in $(cat /proc/cmdline); do
  case "$tok" in SIEGE_CANARY=*) export SIEGE_CANARY="${tok#SIEGE_CANARY=}" ;; esac
done

ip link set lo up
ip addr add 172.16.0.2/24 dev eth0
ip link set eth0 up
ip route add default via 172.16.0.1          # the host tap; also the route to Ollama

export PYTHONPATH=/app PYTHONDONTWRITEBYTECODE=1
export OLLAMA_HOST="${OLLAMA_HOST:-http://172.16.0.1:11434}"
cd /app
exec /usr/local/bin/uvicorn siege.target.app:app --host 0.0.0.0 --port 8100 --log-level warning
INIT
chmod +x "$mnt/init"

umount "$mnt"; rmdir "$mnt"
echo "==> rootfs ready: $ROOTFS"
echo "    boot it with: sudo KERNEL=/path/to/vmlinux sandbox/firecracker/launch.sh"
