#!/usr/bin/env bash
# Build and run the Siege target in a hardened Docker sandbox (plan §7, P7.1/P7.2).
#
# Only the target runs in here (D1). The orchestrator stays on the host and
# reaches it at TARGET_URL (default http://127.0.0.1:8100) -- "nothing else
# changes" (§7). The container is started DETACHED; this script waits until the
# service is ready and prints how to drive it and how to stop it.
#
# The canary must match between the container and the orchestrator so G2 (canary)
# detection works: this script generates one if SIEGE_CANARY is unset and prints
# the exact line to export before running the orchestrator with --no-launch.
set -euo pipefail

cd "$(dirname "$0")/.."            # repo root = the Docker build context

IMAGE=${SIEGE_IMAGE:-siege-target}
NAME=${SIEGE_CONTAINER:-siege-target}
PORT=${SIEGE_PORT:-8100}
NET=${SIEGE_NET:-siege-sandbox}
TARGET_MODEL=${SIEGE_TARGET_MODEL:-}
CANARY=${SIEGE_CANARY:-}
if [ -z "$CANARY" ]; then
  CANARY="CANARY-$(openssl rand -hex 8 2>/dev/null || python3 -c 'import secrets;print(secrets.token_hex(8))')"
fi

echo "==> building $IMAGE (only the target ships in)"
docker build -f sandbox/Dockerfile -t "$IMAGE" .

# An isolated bridge with IP masquerade disabled: the container can reach the
# host (Ollama) and be reached by the orchestrator, but outbound NAT to the
# internet is off (plan §7, P7.2). This blocks internet egress on a Linux host;
# on Docker Desktop the VM's own NAT still applies, so see sandbox/README.md.
if ! docker network inspect "$NET" >/dev/null 2>&1; then
  echo "==> creating egress-restricted network $NET"
  docker network create --driver bridge \
    --opt com.docker.network.bridge.enable_ip_masquerade=false "$NET" >/dev/null
fi

docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "==> starting hardened container $NAME"
# Hardening (§7): non-root user, all capabilities dropped, read-only root fs, a
# small in-memory tmpfs for /tmp (the DB is in-memory -- nothing is persisted),
# no host mounts, no privilege escalation, and tight pid/memory limits. The port
# is published on loopback only, for the orchestrator.
docker run -d --rm \
  --name "$NAME" \
  --network "$NET" \
  --add-host host.docker.internal:host-gateway \
  -p 127.0.0.1:"$PORT":8100 \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=16m \
  --pids-limit 128 \
  --memory 1g \
  -e SIEGE_CANARY="$CANARY" \
  ${TARGET_MODEL:+-e SIEGE_TARGET_MODEL="$TARGET_MODEL"} \
  "$IMAGE" >/dev/null

echo -n "==> waiting for the sandbox to be ready"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${PORT}/openapi.json" >/dev/null 2>&1; then
    echo " ok"
    echo
    echo "sandbox up:   http://127.0.0.1:${PORT}   (container: $NAME, network: $NET)"
    echo "drive it:     export SIEGE_CANARY=$CANARY"
    echo "              SIEGE_CANARY=$CANARY siege demo --no-launch${TARGET_MODEL:+ --target-model $TARGET_MODEL}"
    echo "verify it:    ./sandbox/check.sh"
    echo "logs:         docker logs -f $NAME"
    echo "stop:         docker rm -f $NAME"
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo " FAILED" >&2
echo "the sandbox did not become ready; last logs:" >&2
docker logs "$NAME" 2>&1 | tail -20 >&2
exit 1
