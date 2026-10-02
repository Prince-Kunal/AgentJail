"""HTTP client for the target (plan §3.1, §9.1).

The orchestrator reaches the target only over HTTP and never imports siege.target,
so moving the target into Docker or Firecracker later changes nothing but the URL.
`chat` sends one message and returns the reply together with the tool-log rows that
call produced -- the rows carry resource_attrs/principal_attrs, which the evaluator
needs (plan §3.5).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import urlparse

import httpx

from siege.config import get_settings


@dataclass
class ChatTurn:
    reply: str
    tool_calls: list[dict[str, Any]]  # the tool-log rows this turn produced


class TargetClient:
    def __init__(self, base_url: str | None = None, client: httpx.Client | None = None, timeout: float = 600):
        self.base_url = (base_url or get_settings().target_url).rstrip("/")
        self._client = client or httpx.Client(base_url=self.base_url, timeout=timeout)
        self._owns = client is None
        self._seen: dict[str, int] = {}  # tool-log rows already consumed, per session

    def create_session(self, user_id: str, enforce: bool = False) -> str:
        r = self._client.post("/session", json={"user_id": user_id, "enforce": enforce})
        r.raise_for_status()
        sid = r.json()["session_id"]
        self._seen[sid] = 0
        return sid

    def chat(self, session_id: str, message: str) -> ChatTurn:
        r = self._client.post("/chat", json={"session_id": session_id, "message": message})
        r.raise_for_status()
        reply = r.json()["reply"]
        rows = self.tool_log(session_id)
        start = self._seen.get(session_id, 0)
        new_rows = rows[start:]
        self._seen[session_id] = len(rows)
        return ChatTurn(reply=reply, tool_calls=new_rows)

    def tool_log(self, session_id: str) -> list[dict[str, Any]]:
        r = self._client.get(f"/session/{session_id}/tool_log")
        r.raise_for_status()
        return r.json()["tool_log"]

    def put_policies(self, policies: str) -> None:
        r = self._client.put("/policies", json={"policies": policies})
        r.raise_for_status()

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> "TargetClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@contextmanager
def launched_target(
    canary: str,
    *,
    target_url: str | None = None,
    model: str | None = None,
    ready_timeout: float = 90,
) -> Iterator[TargetClient]:
    """Run the target as a uvicorn subprocess seeded with `canary`, yielding a client.

    This is how `siege run` stays self-contained: the orchestrator and the target
    share the run canary because the orchestrator sets SIEGE_CANARY for the child.
    """
    url = (target_url or get_settings().target_url).rstrip("/")
    parsed = urlparse(url)
    env = {**os.environ, "SIEGE_CANARY": canary}
    if model:
        env["SIEGE_TARGET_MODEL"] = model
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "siege.target.app:app",
         "--host", parsed.hostname or "127.0.0.1", "--port", str(parsed.port or 8100),
         "--log-level", "warning"],
        env=env,
    )
    client = TargetClient(base_url=url)
    try:
        _wait_ready(client, proc, ready_timeout)
        yield client
    finally:
        client.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_ready(client: TargetClient, proc: subprocess.Popen, timeout: float) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"target exited early (code {proc.returncode}) before becoming ready")
        try:
            if client._client.get("/openapi.json", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise TimeoutError(f"target at {client.base_url} was not ready within {timeout}s")
