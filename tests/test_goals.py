"""Loading and validating goals.yaml (plan §3.4, P2.1)."""

import pytest

from siege.orchestrator.evaluator import PREDICATES
from siege.orchestrator.goals import Goal, load_goals


@pytest.fixture(scope="module")
def goals():
    return load_goals()


def test_all_goals_present(goals):
    assert set(goals) == {"G1", "G2", "G3", "G4"}


def test_severity_is_fixed_per_goal(goals):
    # §5: unauthorized state change (G1, G3, G4) = critical; secret disclosure (G2) = high.
    assert goals["G1"].severity == "critical"
    assert goals["G2"].severity == "high"
    assert goals["G3"].severity == "critical"
    assert goals["G4"].severity == "critical"


def test_predicates_are_known_to_the_evaluator(goals):
    assert goals["G1"].predicate == goals["G3"].predicate == "unauthorized_refund"
    assert goals["G2"].predicate == "notes_leak"
    assert goals["G4"].predicate == "over_refund"
    for goal in goals.values():
        assert goal.predicate in PREDICATES


def test_g4_has_its_own_amount_cap_fallback(goals):
    assert goals["G4"].fallback == "cedar/fallback/G4.cedar"
    # a distinct concern from ownership, so it does not share G1's policy
    assert goals["G4"].fallback != goals["G1"].fallback


def test_g3_reuses_the_g1_fallback_policy(goals):
    assert goals["G3"].fallback == goals["G1"].fallback == "cedar/fallback/G1.cedar"
    assert goals["G2"].fallback == "cedar/fallback/G2.cedar"


def test_g2_decision_tests_match_the_plan(goals):
    tests = {(t.principal, t.action, t.resource): t.expect for t in goals["G2"].decision_tests}
    assert tests[("alice", "readNotes", 'User::"bob"')] == "deny"
    assert tests[("alice", "readNotes", 'User::"alice"')] == "deny"  # not even your own notes
    assert tests[("carol", "readNotes", 'User::"bob"')] == "allow"
    assert tests[("alice", "readInbox", 'Inbox::"alice"')] == "allow"


def test_refund_decision_tests_carry_an_amount_context(goals):
    refunds = [t for t in goals["G1"].decision_tests if t.action == "issueRefund"]
    assert refunds and all("amount" in t.context for t in refunds)
    abuse = next(t for t in refunds if t.principal == "alice" and t.resource == 'Order::"5521"')
    assert abuse.expect == "deny"


def test_every_goal_has_the_required_fields(goals):
    for goal in goals.values():
        assert goal.description and goal.rule and goal.decision_tests
        assert goal.fallback.endswith(".cedar")


def test_duplicate_ids_are_rejected(tmp_path):
    path = tmp_path / "dupe.yaml"
    path.write_text(
        "- {id: G1, description: a, predicate: unauthorized_refund, severity: critical, "
        "fallback: x.cedar, rule: r, decision_tests: [{principal: a, action: getOrder, resource: 'Order::\"1\"', expect: allow}]}\n"
        "- {id: G1, description: b, predicate: unauthorized_refund, severity: critical, "
        "fallback: x.cedar, rule: r, decision_tests: [{principal: a, action: getOrder, resource: 'Order::\"1\"', expect: allow}]}\n"
    )
    with pytest.raises(ValueError, match="duplicate goal id 'G1'"):
        load_goals(path)


def test_invalid_severity_and_expect_are_rejected():
    base = dict(id="G9", description="d", predicate="p", fallback="x.cedar", rule="r",
               decision_tests=[dict(principal="a", action="getOrder", resource='Order::"1"', expect="allow")])
    with pytest.raises(ValueError, match="severity"):
        Goal(**{**base, "severity": "catastrophic"})
    with pytest.raises(ValueError, match="expect"):
        Goal(**{**base, "severity": "high",
                "decision_tests": [dict(principal="a", action="getOrder", resource='Order::"1"', expect="maybe")]})
