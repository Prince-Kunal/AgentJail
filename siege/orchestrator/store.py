"""SQLite persistence for a run and everything it produces (plan §5, P2.4).

One `Store` wraps the orchestrator DB (default: config `db_path`, runs/siege.db).
It holds the five tables from §5 -- runs, attempts, findings, policies, reruns --
and the JSON columns (config, evidence, policy attempts) are dumped on write and
parsed on read. The target keeps its own separate DB (siege/target/db.py); the
orchestrator never touches it, only this one.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from siege.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT NOT NULL,
    config_json TEXT NOT NULL            -- models, MAX_TURNS, canary, seed
);

CREATE TABLE IF NOT EXISTS attempts (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs (id),
    goal_id      TEXT    NOT NULL,
    turn         INTEGER NOT NULL,
    strategy     TEXT,
    message      TEXT,
    reply        TEXT,
    refusal_type TEXT,                    -- advisory label (§4); may be 'unknown'
    status       TEXT    NOT NULL,        -- e.g. sent | rejected | attacker_refused
    breach       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS findings (
    id                 INTEGER PRIMARY KEY,
    run_id             INTEGER NOT NULL REFERENCES runs (id),
    goal_id            TEXT    NOT NULL,
    turns_to_breach    INTEGER,
    winning_strategy   TEXT,
    evidence_tool_call TEXT,              -- JSON: the breach evidence
    severity           TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS policies (
    id            INTEGER PRIMARY KEY,
    finding_id    INTEGER NOT NULL REFERENCES findings (id),
    cedar_text    TEXT,
    rationale     TEXT,
    source        TEXT    NOT NULL CHECK (source IN ('generated', 'fallback')),
    model         TEXT,
    attempts_json TEXT,                   -- JSON: models tried, retries, failure reasons
    valid         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS reruns (
    id         INTEGER PRIMARY KEY,
    finding_id INTEGER NOT NULL REFERENCES findings (id),
    mode       TEXT    NOT NULL CHECK (mode IN ('replay', 'adaptive')),
    outcome    TEXT    NOT NULL,          -- BLOCKED | NOT_REPRODUCED | STILL_BREACHED
    tries      INTEGER,
    evidence   TEXT                       -- JSON
);
"""


@dataclass
class Run:
    id: int
    started_at: str
    config: dict[str, Any]

    @property
    def canary(self) -> str | None:
        return self.config.get("canary")


def _loads(value: str | None) -> Any:
    return json.loads(value) if value is not None else None


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else get_settings().db_path
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- writes -------------------------------------------------------------

    def create_run(self, config: dict[str, Any]) -> Run:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO runs (started_at, config_json) VALUES (?, ?)",
                (started_at, json.dumps(config)),
            )
        return Run(id=cur.lastrowid, started_at=started_at, config=config)

    def record_attempt(
        self,
        run_id: int,
        goal_id: str,
        turn: int,
        *,
        strategy: str | None = None,
        message: str | None = None,
        reply: str | None = None,
        refusal_type: str | None = None,
        status: str,
        breach: bool = False,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO attempts
                       (run_id, goal_id, turn, strategy, message, reply, refusal_type, status, breach)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, goal_id, turn, strategy, message, reply, refusal_type, status, int(breach)),
            )
        return cur.lastrowid

    def record_finding(
        self,
        run_id: int,
        goal_id: str,
        *,
        turns_to_breach: int | None,
        winning_strategy: str | None,
        evidence_tool_call: Any,
        severity: str,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO findings
                       (run_id, goal_id, turns_to_breach, winning_strategy, evidence_tool_call, severity)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, goal_id, turns_to_breach, winning_strategy, json.dumps(evidence_tool_call), severity),
            )
        return cur.lastrowid

    def record_policy(
        self,
        finding_id: int,
        *,
        cedar_text: str | None,
        rationale: str | None,
        source: str,
        model: str | None,
        attempts: Any = None,
        valid: bool,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                """INSERT INTO policies
                       (finding_id, cedar_text, rationale, source, model, attempts_json, valid)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (finding_id, cedar_text, rationale, source, model, json.dumps(attempts), int(valid)),
            )
        return cur.lastrowid

    def record_rerun(
        self,
        finding_id: int,
        *,
        mode: str,
        outcome: str,
        tries: int | None = None,
        evidence: Any = None,
    ) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO reruns (finding_id, mode, outcome, tries, evidence) VALUES (?, ?, ?, ?, ?)",
                (finding_id, mode, outcome, tries, json.dumps(evidence)),
            )
        return cur.lastrowid

    # --- reads --------------------------------------------------------------

    def get_run(self, run_id: int) -> Run | None:
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return Run(id=row["id"], started_at=row["started_at"], config=json.loads(row["config_json"]))

    def attempts(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM attempts WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [{**dict(r), "breach": bool(r["breach"])} for r in rows]

    def findings(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM findings WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        return [{**dict(r), "evidence_tool_call": _loads(r["evidence_tool_call"])} for r in rows]

    def policies(self, finding_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM policies WHERE finding_id = ? ORDER BY id", (finding_id,)
        ).fetchall()
        return [{**dict(r), "valid": bool(r["valid"]), "attempts": _loads(r["attempts_json"])} for r in rows]

    def reruns(self, finding_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM reruns WHERE finding_id = ? ORDER BY id", (finding_id,)
        ).fetchall()
        return [{**dict(r), "evidence": _loads(r["evidence"])} for r in rows]
