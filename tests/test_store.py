"""The SQLite store for runs and their results (plan §5, P2.4)."""

import sqlite3

import pytest

from siege.orchestrator.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "siege.db")
    yield s
    s.close()


CONFIG = {"models": {"attacker": "qwen2.5:14b"}, "max_turns": 10, "canary": "CANARY-abc123", "seed": 7}


def test_create_run_roundtrips_config_and_canary(store):
    run = store.create_run(CONFIG)
    assert run.id and run.started_at and run.canary == "CANARY-abc123"
    got = store.get_run(run.id)
    assert got.config == CONFIG and got.canary == "CANARY-abc123"


def test_get_unknown_run_is_none(store):
    assert store.get_run(999) is None


def test_attempts_are_stored_in_order(store):
    run = store.create_run(CONFIG)
    store.record_attempt(run.id, "G1", 0, strategy="claimed_authority", message="m0",
                         status="rejected", breach=False)
    store.record_attempt(run.id, "G1", 1, strategy="supply_expected_credential", message="m1",
                         reply="done", refusal_type="complied_no_breach", status="sent", breach=True)
    rows = store.attempts(run.id)
    assert [r["turn"] for r in rows] == [0, 1]
    assert rows[0]["status"] == "rejected" and rows[0]["breach"] is False
    assert rows[1]["breach"] is True and rows[1]["refusal_type"] == "complied_no_breach"


def test_finding_stores_evidence_as_json(store):
    run = store.create_run(CONFIG)
    evidence = {"kind": "tool_call", "tool_call": {"tool": "issue_refund", "args": {"order_id": "5521"}}}
    fid = store.record_finding(run.id, "G1", turns_to_breach=3, winning_strategy="urgency",
                               evidence_tool_call=evidence, severity="critical")
    (finding,) = store.findings(run.id)
    assert finding["id"] == fid
    assert finding["evidence_tool_call"] == evidence  # round-trips through JSON
    assert finding["severity"] == "critical" and finding["turns_to_breach"] == 3


def test_policy_and_rerun_roundtrip(store):
    run = store.create_run(CONFIG)
    fid = store.record_finding(run.id, "G1", turns_to_breach=1, winning_strategy="x",
                               evidence_tool_call={}, severity="critical")
    store.record_policy(fid, cedar_text="forbid(...);", rationale="owner only",
                        source="generated", model="qwen2.5:14b",
                        attempts=[{"model": "qwen2.5:14b", "retries": 0}], valid=True)
    (policy,) = store.policies(fid)
    assert policy["valid"] is True and policy["source"] == "generated"
    assert policy["attempts"] == [{"model": "qwen2.5:14b", "retries": 0}]

    store.record_rerun(fid, mode="replay", outcome="BLOCKED", tries=1, evidence={"decision": "deny"})
    (rerun,) = store.reruns(fid)
    assert rerun["mode"] == "replay" and rerun["outcome"] == "BLOCKED"
    assert rerun["evidence"] == {"decision": "deny"}


def test_source_and_mode_checks_are_enforced(store):
    run = store.create_run(CONFIG)
    fid = store.record_finding(run.id, "G1", turns_to_breach=1, winning_strategy="x",
                               evidence_tool_call={}, severity="critical")
    with pytest.raises(sqlite3.IntegrityError):
        store.record_policy(fid, cedar_text="x", rationale="y", source="invented", model="m", valid=False)
    with pytest.raises(sqlite3.IntegrityError):
        store.record_rerun(fid, mode="teleport", outcome="BLOCKED")


def test_foreign_keys_are_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.record_attempt(999, "G1", 0, status="sent")


def test_runs_are_isolated(store):
    a = store.create_run(CONFIG)
    b = store.create_run(CONFIG)
    store.record_attempt(a.id, "G1", 0, status="sent")
    assert len(store.attempts(a.id)) == 1
    assert store.attempts(b.id) == []


def test_store_defaults_to_config_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("SIEGE_DB_PATH", str(tmp_path / "nested" / "default.db"))
    from siege.config import get_settings

    get_settings.cache_clear()
    s = Store()
    try:
        assert s.path == tmp_path / "nested" / "default.db"
        assert s.path.exists()
    finally:
        s.close()
