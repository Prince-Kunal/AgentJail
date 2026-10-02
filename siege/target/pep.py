"""The PEP: every tool call goes through here and is logged (plan §3.5, §6.6).

`dispatch` wraps the tool dispatch. For each attempt it cleans the model's
arguments, maps the tool to its Cedar action and resource (§3.2), loads the
principal's and resource's attributes from the DB, runs the tool, and writes one
`tool_log` row. The principal comes from the session, never from the message (D3).

This phase supports only `enforce=false`, so every decision is `not_enforced`
(plan P1.3). The Cedar path -- building entities, calling `is_authorized`, and
denying on a `forbid` -- is added in P4.2, in the marked branch below.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from siege.target.tools import (
    TOOLS,
    ToolError,
    normalize_order_id,
    normalize_user_id,
    parse_amount_cents,
)


@dataclass(frozen=True)
class Session:
    """A bound target session. The principal is fixed here, not by any message (D3).

    The session store and HTTP binding arrive in P1.5; this is what they produce.
    """

    session_id: str
    principal: str
    enforce: bool = False


@dataclass
class DispatchResult:
    tool: str
    args: dict[str, Any]
    decision: str
    executed: bool
    result: dict[str, Any] | None
    result_summary: str
    content: str  # what the agent feeds back to the LLM as the tool result


def dispatch(conn: sqlite3.Connection, session: Session, turn: int, tool: str, args: Any) -> DispatchResult:
    """Run one tool call for `session` at `turn`, logging the attempt either way."""
    principal_attrs = _user_attrs(conn, session.principal)
    try:
        _action, _resource, resource_attrs, _context, call_args = _bind(conn, session.principal, tool, args)
    except ToolError as exc:
        # Malformed call (unknown tool, missing or bad argument): the resource
        # can't even be resolved. Still logged as an attempt, executed=false.
        return _log(conn, session, turn, tool, _as_dict(args), principal_attrs, None,
                    "not_enforced", False, None, f"error: {exc}", str(exc))

    if session.enforce:
        # P4.2 replaces this with the Cedar check over _action/_resource/_context:
        # deny -> log decision="deny", executed=false, return "Action denied by policy".
        raise NotImplementedError("Cedar enforcement arrives in P4.2; use enforce=false in Phase 1")

    try:
        result = TOOLS[tool](conn, session.principal, **call_args)
    except ToolError as exc:
        return _log(conn, session, turn, tool, call_args, principal_attrs, resource_attrs,
                    "not_enforced", False, None, f"error: {exc}", str(exc))
    return _log(conn, session, turn, tool, call_args, principal_attrs, resource_attrs,
                "not_enforced", True, result, _summary(tool, result), json.dumps(result))


def read_tool_log(conn: sqlite3.Connection, session_id: str) -> list[dict[str, Any]]:
    """Return a session's tool-log rows in order, with the JSON columns parsed (plan §3.1)."""
    rows = conn.execute("SELECT * FROM tool_log WHERE session_id = ? ORDER BY id", (session_id,)).fetchall()
    return [
        {
            "id": r["id"],
            "session_id": r["session_id"],
            "turn": r["turn"],
            "tool": r["tool"],
            "args": json.loads(r["args"]),
            "principal": r["principal"],
            "principal_attrs": json.loads(r["principal_attrs"]),
            "resource_attrs": None if r["resource_attrs"] is None else json.loads(r["resource_attrs"]),
            "decision": r["decision"],
            "executed": bool(r["executed"]),
            "result_summary": r["result_summary"],
            "ts": r["ts"],
        }
        for r in rows
    ]


# --- internals --------------------------------------------------------------

def _bind(conn, principal, tool, args):
    """Map a tool call to (action, resource uid, resource_attrs, context, cleaned call args).

    Cleans the arguments the same way the tools do, so Cedar, the log and the
    tool all see one canonical form. A raw "#5521" would otherwise name an
    Order that doesn't exist, and the G1 forbid would silently not apply.
    Only known arguments are passed on, so stray model arguments can't reach a tool.
    """
    if tool == "get_order":
        oid = normalize_order_id(_arg(args, "order_id"))
        return "getOrder", f'Order::"{oid}"', _order_attrs(conn, oid), {}, {"order_id": oid}
    if tool == "issue_refund":
        oid = normalize_order_id(_arg(args, "order_id"))
        amount = parse_amount_cents(_arg(args, "amount_cents"))
        return ("issueRefund", f'Order::"{oid}"', _order_attrs(conn, oid),
                {"amount": amount}, {"order_id": oid, "amount_cents": amount})
    if tool == "read_inbox":
        # The inbox is always the session user's (D3), so its owner is the principal.
        return "readInbox", f'Inbox::"{principal}"', {"owner": principal}, {}, {}
    if tool == "get_customer_notes":
        cid = normalize_user_id(_arg(args, "customer_id"))
        return "readNotes", f'User::"{cid}"', _user_attrs(conn, cid), {}, {"customer_id": cid}
    raise ToolError(f"unknown tool {tool!r}")


def _arg(args, key):
    try:
        return args[key]
    except (KeyError, TypeError):
        raise ToolError(f"missing argument {key!r}")


def _order_attrs(conn, order_id):
    row = conn.execute("SELECT owner, total_cents FROM orders WHERE id = ?", (order_id,)).fetchone()
    return {"owner": row["owner"], "total": row["total_cents"]} if row else None


def _user_attrs(conn, user_id):
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    return {"role": row["role"]} if row else None


def _summary(tool, result):
    if tool == "get_order":
        return f"order {result['order_id']} owner={result['owner']} total={result['total_cents']}c"
    if tool == "issue_refund":
        return f"refund {result['refund_id']} on order {result['order_id']} for {result['amount_cents']}c"
    if tool == "read_inbox":
        return f"{len(result['emails'])} email(s)"
    if tool == "get_customer_notes":
        return f"{len(result['notes'])} note(s) for {result['customer_id']}"
    return "ok"


def _as_dict(args):
    return args if isinstance(args, dict) else {"_raw": args}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _log(conn, session, turn, tool, args, principal_attrs, resource_attrs,
         decision, executed, result, result_summary, content):
    conn.execute(
        """INSERT INTO tool_log
               (session_id, turn, tool, args, principal, principal_attrs,
                resource_attrs, decision, executed, result_summary, ts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session.session_id, turn, tool, json.dumps(args, default=str), session.principal,
            json.dumps(principal_attrs), None if resource_attrs is None else json.dumps(resource_attrs),
            decision, int(executed), result_summary, _now(),
        ),
    )
    conn.commit()
    return DispatchResult(tool, dict(args) if isinstance(args, dict) else _as_dict(args),
                          decision, executed, result, result_summary, content)
