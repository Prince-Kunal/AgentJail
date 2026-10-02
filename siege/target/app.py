"""The target service: ShopBot behind the endpoints from plan §3.1 (P1.5).

Only the orchestrator calls these; the attacker never does. The principal is
bound to the session on the server when it is created and comes from nowhere
else (D3) -- no message can change who the agent acts as.

State lives in `app.state`: one seeded in-memory SQLite connection (the whole
target DB, including the tool log), the run canary, the loaded policy set, and
the live sessions with their conversation history. A single lock serialises
every request, which is enough for the one-conversation-at-a-time demo and lets
the shared connection be used safely from FastAPI's threadpool.

This phase runs with the PEP off (`enforce=false`). Cedar enforcement, and the
policy set that `PUT /policies` stores, are wired in at P4.2.
"""

from __future__ import annotations

import dataclasses
import os
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from siege.llm import Message
from siege.target import db, seed
from siege.target.agent import run_turn
from siege.target.pep import Session, read_tool_log


class SessionRequest(BaseModel):
    user_id: str
    enforce: bool = False


class SessionResponse(BaseModel):
    session_id: str


class ChatRequest(BaseModel):
    session_id: str
    message: str


class ToolCallOut(BaseModel):
    tool: str
    args: dict
    decision: str
    executed: bool
    result_summary: str


class ChatResponse(BaseModel):
    reply: str
    tool_calls: list[ToolCallOut]


class PoliciesRequest(BaseModel):
    policies: str


class PoliciesResponse(BaseModel):
    ok: bool = True
    bytes: int


@dataclasses.dataclass
class _SessionState:
    principal: str
    enforce: bool
    turn: int = 0
    history: list[Message] = dataclasses.field(default_factory=list)


def create_app(canary: str | None = None) -> FastAPI:
    app = FastAPI(title="Siege target (ShopBot)")
    conn = db.connect(":memory:", check_same_thread=False)

    app.state.conn = conn
    app.state.canary = seed.seed(conn, canary=canary or os.environ.get("SIEGE_CANARY"))
    app.state.policies = ""  # loaded via PUT /policies; used by the PEP from P4.2
    app.state.sessions: dict[str, _SessionState] = {}
    app.state.lock = threading.Lock()
    app.state.next_session = 0

    @app.post("/session", response_model=SessionResponse)
    def create_session(req: SessionRequest) -> SessionResponse:
        with app.state.lock:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (req.user_id,)).fetchone() is None:
                raise HTTPException(status_code=400, detail=f"unknown user {req.user_id!r}")
            app.state.next_session += 1
            session_id = f"sess-{app.state.next_session}"
            app.state.sessions[session_id] = _SessionState(principal=req.user_id, enforce=req.enforce)
        return SessionResponse(session_id=session_id)

    @app.post("/chat", response_model=ChatResponse)
    def chat(req: ChatRequest) -> ChatResponse:
        with app.state.lock:
            state = app.state.sessions.get(req.session_id)
            if state is None:
                raise HTTPException(status_code=404, detail="unknown session")
            # The principal is the session's, never the message's (D3).
            session = Session(req.session_id, state.principal, state.enforce)
            try:
                result = run_turn(conn, session, state.turn, req.message, state.history)
            except NotImplementedError as exc:
                raise HTTPException(status_code=501, detail=str(exc))
            state.history = result.messages
            state.turn += 1
        return ChatResponse(
            reply=result.reply,
            tool_calls=[ToolCallOut(**dataclasses.asdict(c)) for c in result.tool_calls],
        )

    @app.get("/session/{session_id}/tool_log")
    def tool_log(session_id: str) -> dict:
        with app.state.lock:
            if session_id not in app.state.sessions:
                raise HTTPException(status_code=404, detail="unknown session")
            rows = read_tool_log(conn, session_id)
        return {"session_id": session_id, "tool_log": rows}

    @app.put("/policies", response_model=PoliciesResponse)
    def put_policies(req: PoliciesRequest) -> PoliciesResponse:
        with app.state.lock:
            app.state.policies = req.policies
        return PoliciesResponse(bytes=len(req.policies))

    return app


app = create_app()
