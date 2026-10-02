"""ShopBot's tools over the seeded target DB (plan §3.2)."""

import pytest

from siege.target import db, seed
from siege.target.tools import (
    TOOLS,
    ToolError,
    get_customer_notes,
    get_order,
    issue_refund,
    normalize_order_id,
    normalize_user_id,
    parse_amount_cents,
    read_inbox,
)

CANARY = "CANARY-0123456789abcdef"


@pytest.fixture
def conn():
    connection = db.connect()
    seed.seed(connection, canary=CANARY)
    yield connection
    connection.close()


def refund_count(conn):
    return conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0]


def seeded_subjects(owner):
    return [subject for o, _, subject, _, _ in seed.INBOX if o == owner]


def test_get_order_accepts_multiple_id_formats(conn):
    for order_id in ("5521", 5521, "#5521", " 5521 "):
        result = get_order(conn, "bob", order_id=order_id)
        assert result["order_id"] == "5521"
        assert result["owner"] == "bob"
        assert result["total_cents"] == 19999


def test_get_order_unknown_order_raises(conn):
    with pytest.raises(ToolError):
        get_order(conn, "alice", order_id="9999")


def test_issue_refund_creates_one_refund(conn):
    result = issue_refund(conn, "alice", order_id="5518", amount_cents=1000)
    assert (result["order_id"], result["amount_cents"], result["status"]) == ("5518", 1000, "refunded")
    (row,) = conn.execute("SELECT id, order_id, amount_cents, issued_by, created_at FROM refunds").fetchall()
    assert row["id"] == result["refund_id"]
    assert (row["order_id"], row["amount_cents"], row["issued_by"]) == ("5518", 1000, "alice")
    assert row["created_at"]


@pytest.mark.parametrize("amount", [0, -5, 12.5, "abc", True])
def test_issue_refund_rejects_invalid_amounts(conn, amount):
    with pytest.raises(ToolError):
        issue_refund(conn, "alice", order_id="5518", amount_cents=amount)
    assert refund_count(conn) == 0


def test_issue_refund_unknown_order_does_not_write(conn):
    with pytest.raises(ToolError):
        issue_refund(conn, "alice", order_id="9999", amount_cents=1000)
    assert refund_count(conn) == 0


def test_read_inbox_returns_only_principal_inbox(conn):
    for user in ("alice", "bob"):
        emails = read_inbox(conn, user)["emails"]
        assert [e["subject"] for e in emails] == seeded_subjects(user)


def test_read_inbox_ignores_model_arguments(conn):
    emails = read_inbox(conn, "alice", owner="bob", user_id="bob")["emails"]
    assert [e["subject"] for e in emails] == seeded_subjects("alice")


def test_get_customer_notes_returns_that_customers_notes(conn):
    bob = get_customer_notes(conn, "carol", customer_id=" bob ")
    assert bob["customer_id"] == "bob"
    assert any(CANARY in note for note in bob["notes"])
    alice = get_customer_notes(conn, "carol", customer_id="alice")
    assert alice["notes"] and not any(CANARY in note for note in alice["notes"])


def test_get_customer_notes_unknown_customer(conn):
    with pytest.raises(ToolError):
        get_customer_notes(conn, "carol", customer_id="does-not-exist")


def test_tools_registry_contains_exactly_four_tools():
    assert set(TOOLS) == {"get_order", "issue_refund", "read_inbox", "get_customer_notes"}


def test_cleaning_twice_changes_nothing():
    """The PEP cleans arguments and the tool cleans them again, so a second pass must be a no-op."""
    assert normalize_order_id(normalize_order_id(" #5521 ")) == "5521"
    assert normalize_user_id(normalize_user_id(" bob ")) == "bob"
    assert parse_amount_cents(parse_amount_cents("1000")) == 1000
