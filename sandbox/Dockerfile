# Siege target (ShopBot) sandbox image (plan §7, P7.1).
#
# Only the target ships into the sandbox: the attacker and orchestrator stay on
# the host and reach it over HTTP (D1). The target's DB is in-memory, so the
# container needs no persistent storage -- a small tmpfs for /tmp is enough, and
# the root filesystem is mounted read-only at run time (see sandbox/run.sh).
FROM python:3.12-slim

# Pinned runtime deps the target actually imports (§6.1): the FastAPI service and
# the Cedar PEP. pyyaml/httpx are host-side only; the target reaches Ollama with
# the standard-library urllib, so no HTTP client is needed here.
RUN pip install --no-cache-dir \
      cedarpy==4.12.1 \
      fastapi==0.142.2 \
      uvicorn==0.54.0 \
      pydantic==2.13.5

# A non-root user that owns nothing writable.
RUN useradd --create-home --uid 10001 shopbot
WORKDIR /app

# Ship only the target package and the shared modules it imports -- never the
# orchestrator or attacker (§9.1). The PEP reads cedar/schema.cedarschema; the
# base/fallback policies are host-side and arrive at runtime via PUT /policies.
COPY siege/__init__.py              siege/__init__.py
COPY siege/config.py                siege/config.py
COPY siege/llm.py                   siege/llm.py
COPY siege/target/                  siege/target/
COPY siege/cedar/schema.cedarschema siege/cedar/schema.cedarschema

# PYTHONDONTWRITEBYTECODE keeps Python from writing .pyc under the read-only root.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OLLAMA_HOST=http://host.docker.internal:11434

EXPOSE 8100
USER shopbot

# The canary is injected at run time (-e SIEGE_CANARY) so the orchestrator and
# the target share it; SIEGE_TARGET_MODEL optionally picks the ShopBot model.
CMD ["uvicorn", "siege.target.app:app", "--host", "0.0.0.0", "--port", "8100", "--log-level", "warning"]
