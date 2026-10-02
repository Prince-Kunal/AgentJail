"""The hand-written Cedar schema, base and fallback policies (plan §6.1-§6.4, P4.1)."""

import pytest

from siege.orchestrator import cedar
from siege.orchestrator.goals import load_goals


@pytest.fixture(scope="module")
def goals():
    return load_goals()


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("//"))


def test_schema_and_base_validate():
    result = cedar.validate(cedar.load_base())
    assert result.validation_passed, result.errors


def test_each_fallback_validates_with_the_base(goals):
    for goal in goals.values():
        policies = cedar.policy_set(cedar.load_policy_file(goal.fallback))
        result = cedar.validate(policies)
        assert result.validation_passed, f"{goal.id} fallback invalid: {result.errors}"


def test_each_fallback_is_a_single_forbid(goals):
    seen = set()
    for goal in goals.values():
        if goal.fallback in seen:
            continue
        seen.add(goal.fallback)
        body = _strip_comments(cedar.load_policy_file(goal.fallback))
        assert "forbid" in body and "permit" not in body  # a fix is a forbid layered on base (D4)
        assert body.count("forbid") == 1


def test_each_fallback_passes_its_goals_decision_tests(goals):
    """The key check (P4.1/P4.7): base + fallback must produce every expected decision."""
    for goal in goals.values():
        policies = cedar.policy_set(cedar.load_policy_file(goal.fallback))
        results = cedar.run_decision_tests(policies, goal.decision_tests)
        failures = [(r.principal, r.action, r.resource, r.got, r.expect) for r in results if not r.ok]
        assert not failures, f"{goal.id} fallback failed decision tests: {failures}"


def test_g3_reuses_the_g1_fallback(goals):
    assert goals["G3"].fallback == goals["G1"].fallback


def test_a_typo_attribute_is_rejected():
    bad = 'forbid (principal, action == Action::"issueRefund", resource) unless { resource.ownerr == principal };'
    result = cedar.validate(cedar.policy_set(bad))
    assert not result.validation_passed and result.errors


def test_without_the_base_everything_denies(goals):
    """D4: fixes alone (no base permit) deny even legitimate use."""
    g1 = cedar.load_policy_file(goals["G1"].fallback)
    # bob refunding his own order is allowed with the base, denied without it
    assert cedar.decide(cedar.policy_set(g1), "bob", "issueRefund", 'Order::"5521"', {"amount": 19999})[0] == "allow"
    assert cedar.decide(g1, "bob", "issueRefund", 'Order::"5521"', {"amount": 19999})[0] == "deny"


def test_seed_entities_match_the_target_seed_data():
    """Guard against drift between host-side entities and siege/target/seed.py (§3.3)."""
    from siege.target import seed

    seed_users = {uid: role for uid, _name, role in seed.USERS}
    assert cedar.SEED_USERS == seed_users
    seed_orders = {oid: (owner, total) for oid, owner, _item, total, _status in seed.ORDERS}
    assert cedar.SEED_ORDERS == seed_orders
    inbox_owners = {owner for owner, *_ in seed.INBOX}
    assert set(cedar.SEED_INBOX_OWNERS) == inbox_owners
