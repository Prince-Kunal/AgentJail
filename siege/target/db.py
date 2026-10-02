"""Target DB: the SQLite schema behind ShopBot's tools (plan §3.2, §3.3).

Money is integer cents because Cedar has no decimals. User and order IDs are
text, matching the Cedar entity IDs (`User::"alice"`, `Order::"5521"`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id   TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('customer', 'staff'))
);

CREATE TABLE IF NOT EXISTS orders (
    id          TEXT PRIMARY KEY,
    owner       TEXT NOT NULL REFERENCES users (id),
    item        TEXT NOT NULL,
    total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
    status      TEXT NOT NULL
);

-- One row per executed issue_refund. Nothing caps the amount at the order
-- total: the target is deliberately weak, and that rule is a Cedar fix (G4).
CREATE TABLE IF NOT EXISTS refunds (
    id           INTEGER PRIMARY KEY,
    order_id     TEXT NOT NULL REFERENCES orders (id),
    amount_cents INTEGER NOT NULL,
    issued_by    TEXT NOT NULL REFERENCES users (id),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbox (
    id       INTEGER PRIMARY KEY,
    owner    TEXT NOT NULL REFERENCES users (id),
    sender   TEXT NOT NULL,
    subject  TEXT NOT NULL,
    body     TEXT NOT NULL,
    received TEXT NOT NULL
);

-- Internal notes about a customer, written by staff.
CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY,
    customer_id TEXT NOT NULL REFERENCES users (id),
    author      TEXT NOT NULL REFERENCES users (id),
    body        TEXT NOT NULL
);

-- The PEP's ground-truth log: one row per tool-call attempt (plan §3.5).
-- principal_attrs and resource_attrs hold the Cedar entity attributes the PEP
-- loaded from the DB, so the host evaluator can check predicates (ownership,
-- staff-only) without reaching into this DB. Runtime state, not seeded.
CREATE TABLE IF NOT EXISTS tool_log (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT    NOT NULL,
    turn            INTEGER NOT NULL,
    tool            TEXT    NOT NULL,
    args            TEXT    NOT NULL,   -- JSON: the cleaned call arguments
    principal       TEXT    NOT NULL,
    principal_attrs TEXT    NOT NULL,   -- JSON: {"role": ...}
    resource_attrs  TEXT,               -- JSON; NULL when the resource doesn't exist
    decision        TEXT    NOT NULL CHECK (decision IN ('allow', 'deny', 'not_enforced')),
    executed        INTEGER NOT NULL CHECK (executed IN (0, 1)),
    result_summary  TEXT    NOT NULL,
    ts              TEXT    NOT NULL
);
"""

# Parents before children, so inserts run in this order and deletes in reverse.
TABLES = ("users", "orders", "refunds", "inbox", "notes")


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open the target DB (in memory by default) and create any missing tables."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
