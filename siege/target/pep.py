"""The PEP: every tool call goes through here and is logged (plan §3.5, §6.6).

`dispatch` wraps the tool dispatch. For each attempt it cleans the model's
arguments, maps the tool to its Cedar action and resource (§3.2), loads the
principal's and resource's attributes from the DB, runs the tool, and writes one
`tool_log` row. The principal comes from the session, never from the message (D3).

With `enforce=false` every decision is `not_enforced` and the tool runs. With
`enforce=true` (P4.2) the Cedar path runs first: entities are built from the DB,
`is_authorized` is called against the loaded policy set, and on a deny the tool
does not run (`decision=deny`, executed=false, "Action denied by policy"). Any
Cedar evaluation error counts as a deny, and an empty policy set denies everything
(default-deny, D4). The PEP reads only the shared schema file, never orchestrator
code.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cedarpy

from siege.target.tools import (
    TOOLS,
    ToolError,
    normalize_order_id,
    normalize_user_id,
    parse_amount_cents,
)

# Shared schema file (siege/cedar/schema.cedarschema); read directly so the target
# stays independent of the orchestrator (§9.1).
_SCHEMA = (Path(__file__).resolve().parent.parent / "cedar" / "schema.cedarschema").read_text()
_UID_RE = re.compile(r'^(\w+)::"(.*)"$')


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


def dispatch(
    conn: sqlite3.Connection, session: Session, turn: int, tool: str, args: Any, policies: str = ""
) -> DispatchResult:
    """Run one tool call for `session` at `turn`, enforcing `policies` when the session enforces."""
    principal_attrs = _user_attrs(conn, session.principal)
    try:
        action, resource, resource_attrs, context, call_args = _bind(conn, session.principal, tool, args)
    except ToolError as exc:
        # Malformed call (unknown tool, missing or bad argument): the resource
        # can't even be resolved, so Cedar is never consulted. Logged, executed=false.
        return _log(conn, session, turn, tool, _as_dict(args), principal_attrs, None,
                    "not_enforced", False, None, f"error: {exc}", str(exc))

    if session.enforce:
        if _authorize(conn, session.principal, action, resource, context, policies) == "deny":
            return _log(conn, session, turn, tool, call_args, principal_attrs, resource_attrs,
                        "deny", False, None, "denied by policy", "Action denied by policy")
        decision = "allow"
    else:
        decision = "not_enforced"

    try:
        result = TOOLS[tool](conn, session.principal, **call_args)
    except ToolError as exc:
        return _log(conn, session, turn, tool, call_args, principal_attrs, resource_attrs,
                    decision, False, None, f"error: {exc}", str(exc))
    return _log(conn, session, turn, tool, call_args, principal_attrs, resource_attrs,
                decision, True, result, _summary(tool, result), json.dumps(result))


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


# --- Cedar enforcement (plan §6.6, P4.2) ------------------------------------

def _authorize(conn, principal, action, resource, context, policies) -> str:
    """Return 'allow' or 'deny' for one tool call under the loaded policy set."""
    if not (policies or "").strip():
        return "deny"  # no policy set loaded -> default deny (D4)
    rtype, rid = _parse_uid(resource)
    entities = _build_entities(conn, principal, rtype, rid)
    request = {
        "principal": f'User::"{principal}"',
        "action": f'Action::"{action}"',
        "resource": resource,
        "context": context or {},
    }
    result = cedarpy.is_authorized(request, policies, entities, schema=_SCHEMA)
    # A missing entity (e.g. an order that doesn't exist) evaluates with an error
    # and must not be treated as allow -- any error counts as deny (§6.6).
    allow = str(result.decision).endswith("Allow") and not result.diagnostics.errors
    return "allow" if allow else "deny"


def _parse_uid(uid: str) -> tuple[str, str]:
    match = _UID_RE.match(uid)
    if not match:
        raise ValueError(f"not a Cedar uid: {uid!r}")
    return match.group(1), match.group(2)


def _uref(user_id: str) -> dict[str, Any]:
    return {"__entity": {"type": "User", "id": user_id}}


def _build_entities(conn, principal, rtype, rid) -> list[dict[str, Any]]:
    """Build the Cedar entities this request touches, from the DB (§6.6 step 2)."""
    entities: dict[tuple[str, str], dict[str, Any]] = {}

    def add_user(uid: str) -> None:
        if ("User", uid) in entities:
            return
        row = conn.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()
        if row is not None:
            entities[("User", uid)] = {"uid": {"type": "User", "id": uid}, "attrs": {"role": row["role"]}, "parents": []}

    add_user(principal)
    if rtype == "Order":
        row = conn.execute("SELECT owner, total_cents FROM orders WHERE id = ?", (rid,)).fetchone()
        if row is not None:
            add_user(row["owner"])
            entities[("Order", rid)] = {
                "uid": {"type": "Order", "id": rid},
                "attrs": {"owner": _uref(row["owner"]), "total": row["total_cents"]},
                "parents": [],
            }
    elif rtype == "Inbox":
        add_user(rid)  # the inbox id is its owner
        entities[("Inbox", rid)] = {"uid": {"type": "Inbox", "id": rid}, "attrs": {"owner": _uref(rid)}, "parents": []}
    elif rtype == "User":
        add_user(rid)
    return list(entities.values())


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
