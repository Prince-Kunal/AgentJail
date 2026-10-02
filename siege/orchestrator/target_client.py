"""HTTP client for the target (plan §3.1, §9.1).

The orchestrator reaches the target only over HTTP and never imports siege.target,
so moving the target into Docker or Firecracker later changes nothing but the URL.
`chat` sends one message and returns the reply together with the tool-log rows that
call produced -- the rows carry resource_attrs/principal_attrs, which the evaluator
needs (plan §3.5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
