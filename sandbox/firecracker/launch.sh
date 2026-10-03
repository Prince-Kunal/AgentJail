#!/usr/bin/env bash
# Boot the Siege target rootfs in a Firecracker microVM (plan §7, P7.3).
#
# LINUX + KVM host only (needs /dev/kvm). On macOS there is no KVM, so the dev
# machine and demo use Docker instead (sandbox/run.sh); see sandbox/README.md for
# the recorded decision. This sets up a tap device so the host can reach the
# service (172.16.0.2:8100) and the VM can reach host Ollama (172.16.0.1), then
# boots Firecracker from vmconfig.json.
set -euo pipefail
cd "$(dirname "$0")/../.."          # repo root

[ -e /dev/kvm ] || { echo "no /dev/kvm -- Firecracker needs a Linux KVM host. Use Docker: sandbox/run.sh" >&2; exit 1; }
command -v firecracker >/dev/null || { echo "firecracker not on PATH (see sandbox/README.md)" >&2; exit 1; }
: "${KERNEL:?set KERNEL to an uncompressed vmlinux (see sandbox/README.md)}"

ROOTFS=${ROOTFS:-sandbox/firecracker/rootfs.ext4}
TAP=${TAP:-fc-tap0}
HOST_IF=${HOST_IF:-eth0}             # the host's uplink, for VM->Ollama NAT
CANARY=${SIEGE_CANARY:-CANARY-$(openssl rand -hex 8)}
API_SOCK=/tmp/firecracker-siege.sock
CONFIG=/tmp/firecracker-siege.json

[ -f "$ROOTFS" ] || { echo "no $ROOTFS -- build it first: sudo sandbox/firecracker/build-rootfs.sh" >&2; exit 1; }

# tap device: host side 172.16.0.1/24, VM gets 172.16.0.2 (set by /init).
sudo ip link del "$TAP" 2>/dev/null || true
sudo ip tuntap add dev "$TAP" mode tap
sudo ip addr add 172.16.0.1/24 dev "$TAP"
sudo ip link set "$TAP" up
# Let the VM reach host Ollama (and only what the host routes); the orchestrator
# reaches the VM directly over the tap. Tighten with egress rules per §7/README.
sudo sysctl -w net.ipv4.ip_forward=1 >/dev/null
sudo iptables -t nat -C POSTROUTING -o "$HOST_IF" -j MASQUERADE 2>/dev/null \
  || sudo iptables -t nat -A POSTROUTING -o "$HOST_IF" -j MASQUERADE

sed -e "s#@KERNEL@#${KERNEL}#" -e "s#@ROOTFS@#${ROOTFS}#" \
    -e "s#@TAP@#${TAP}#"       -e "s#@CANARY@#${CANARY}#" \
    sandbox/firecracker/vmconfig.json > "$CONFIG"
rm -f "$API_SOCK"

echo "booting Firecracker; target will be at http://172.16.0.2:8100"
echo "drive it from the host:"
echo "  SIEGE_TARGET_URL=http://172.16.0.2:8100 SIEGE_CANARY=$CANARY siege demo --no-launch"
exec firecracker --api-sock "$API_SOCK" --config-file "$CONFIG"
