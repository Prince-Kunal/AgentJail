"""ShopBot's tools (plan §3.2): plain functions over the target DB.

Every tool has the same signature, `tool(conn, principal, **model_args)`. The
principal is the user bound to the session (D3); the model never supplies it.

Authorization isn't checked here: the PEP does that before a tool runs (§6.6).
Tools only reject input that makes no sense, raising `ToolError` without
changing anything.

Before building a Cedar request, the PEP must clean the model's arguments with
the `normalize_*` and `parse_*` helpers below, exactly as the tools do. If it
used a raw ID such as "#5521", Cedar would see `Order::"#5521"`, which doesn't
exist: the G1 forbid fails to evaluate, Cedar skips it, and the refund on order
5521 is allowed. Cleaning twice is harmless, because the helpers are idempotent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable


class ToolError(ValueError):
    """The model's arguments don't make sense (unknown order, bad amount). Nothing was changed."""


def normalize_order_id(value: Any) -> str:
    """Accept 5521, "5521" or "#5521"; return "5521"."""
    return str(value).strip().lstrip("#")


def normalize_user_id(value: Any) -> str:
    return str(value).strip()


def parse_amount_cents(value: Any) -> int:
    """Accept a positive whole number of cents (1000 or "1000"); reject 0, negatives, floats and booleans."""
    if not isinstance(value, bool) and isinstance(value, (int, str)):
        try:
            amount = int(value)
        except ValueError:
            pass
        else:
            if amount > 0:
                return amount
    raise ToolError(f"amount_cents must be a positive whole number of cents, got {value!r}")


def get_order(conn: sqlite3.Connection, principal: str, *, order_id: Any) -> dict[str, Any]:
    order_id = normalize_order_id(order_id)
    row = conn.execute(
        "SELECT id, owner, item, total_cents, status FROM orders WHERE id = ?", (order_id,)
    ).fetchone()
    if row is None:
        raise ToolError(f"order {order_id!r} does not exist")
    return {
        "order_id": row["id"],
        "owner": row["owner"],
        "item": row["item"],
        "total_cents": row["total_cents"],
        "status": row["status"],
    }


def issue_refund(conn: sqlite3.Connection, principal: str, *, order_id: Any, amount_cents: Any) -> dict[str, Any]:
    """Record a refund issued by the principal.

    Doesn't check that the principal owns the order or that the amount is within
    the order total: those are the G1 and G4 Cedar fixes.
    """
    order_id = normalize_order_id(order_id)
    amount = parse_amount_cents(amount_cents)
    if conn.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone() is None:
        raise ToolError(f"order {order_id!r} does not exist")

    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with conn:
        cursor = conn.execute(
            "INSERT INTO refunds (order_id, amount_cents, issued_by, created_at) VALUES (?, ?, ?, ?)",
            (order_id, amount, principal, created_at),
        )
    return {"refund_id": cursor.lastrowid, "order_id": order_id, "amount_cents": amount, "status": "refunded"}


def read_inbox(conn: sqlite3.Connection, principal: str, **_: Any) -> dict[str, Any]:
    """Return the principal's own emails.

    Any arguments are ignored: small models often pass stray ones to a tool that
    takes none, and the inbox is always the session user's (D3).
    """
    rows = conn.execute(
        "SELECT id, sender, subject, body, received FROM inbox WHERE owner = ? ORDER BY id", (principal,)
    ).fetchall()
    return {"emails": [dict(row) for row in rows]}


def get_customer_notes(conn: sqlite3.Connection, principal: str, *, customer_id: Any) -> dict[str, Any]:
    """Return a customer's internal notes. Doesn't check the principal's role: that's the G2 Cedar fix."""
    customer_id = normalize_user_id(customer_id)
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (customer_id,)).fetchone() is None:
        raise ToolError(f"customer {customer_id!r} does not exist")
    rows = conn.execute("SELECT body FROM notes WHERE customer_id = ? ORDER BY id", (customer_id,)).fetchall()
    return {"customer_id": customer_id, "notes": [row["body"] for row in rows]}


TOOLS: dict[str, Callable[..., dict[str, Any]]] = {
    "get_order": get_order,
    "issue_refund": issue_refund,
    "read_inbox": read_inbox,
    "get_customer_notes": get_customer_notes,
}
