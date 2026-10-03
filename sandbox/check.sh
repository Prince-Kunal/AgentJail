#!/usr/bin/env bash
# Verify the sandbox hardening of a running Siege target container (plan §7, P7.2).
#
# Hard requirements (a FAIL exits non-zero): runs as non-root, the root fs is
# read-only, the only writable place is the tmpfs, Ollama is reachable, and the
# orchestrator can reach the service. Egress to the wider internet is a WARN, not
# a FAIL: the egress-restricted network blocks it on a Linux host, but Docker
# Desktop's own VM NAT does not (see sandbox/README.md).
set -uo pipefail

NAME=${SIEGE_CONTAINER:-siege-target}
PORT=${SIEGE_PORT:-8100}
fails=0

say() { printf '  %-4s %s\n' "$1" "$2"; }
inq() { docker exec "$NAME" python -c "$1" >/dev/null 2>&1; }   # run a probe in-container

if ! docker inspect "$NAME" >/dev/null 2>&1; then
  echo "no running container '$NAME'. Start it first with ./sandbox/run.sh" >&2
  exit 2
fi

echo "checking sandbox '$NAME':"

# 1. non-root
uid=$(docker exec "$NAME" python -c 'import os;print(os.getuid())' 2>/dev/null || echo "?")
if [ "$uid" != "0" ] && [ "$uid" != "?" ]; then say PASS "runs as non-root (uid $uid)"; else say FAIL "runs as uid $uid"; fails=$((fails+1)); fi

# 2. read-only root filesystem (a write under /app must be refused)
if inq "open('/app/__probe','w')"; then say FAIL "root filesystem is writable (/app)"; fails=$((fails+1)); else say PASS "root filesystem is read-only"; fi

# 3. the tmpfs is the only writable place
if inq "p='/tmp/__probe';open(p,'w').write('x');__import__('os').remove(p)"; then say PASS "/tmp (tmpfs) is writable"; else say FAIL "/tmp is not writable"; fails=$((fails+1)); fi

# 4. the LLM API (host Ollama) is reachable -- the target needs it
if inq "import urllib.request as u;u.urlopen('http://host.docker.internal:11434/api/tags',timeout=6)"; then say PASS "reaches the LLM API (host Ollama)"; else say FAIL "cannot reach the LLM API"; fails=$((fails+1)); fi

# 5. the orchestrator (host) can reach the service
if curl -fsS "http://127.0.0.1:${PORT}/openapi.json" >/dev/null 2>&1; then say PASS "orchestrator can reach the service (:$PORT)"; else say FAIL "service not reachable on :$PORT"; fails=$((fails+1)); fi

# 6. egress to the wider internet (WARN, platform-dependent)
if inq "import urllib.request as u;u.urlopen('http://example.com',timeout=6)"; then
  say WARN "internet is reachable -- egress allow-listing needs a Linux host or gVisor (README)"
else
  say PASS "internet egress is blocked"
fi

echo
if [ "$fails" -eq 0 ]; then echo "sandbox hardening OK."; else echo "$fails hard check(s) FAILED." >&2; fi
exit "$fails"
