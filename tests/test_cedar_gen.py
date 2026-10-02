"""The Cedar generator: normalisation, validation + decision tests, the cascade (plan §6.4-6.5, P4.7)."""

import pytest

from siege.llm import FakeProvider, override_provider
from siege.orchestrator import cedar, cedar_gen
from siege.orchestrator.cedar_gen import CedarCandidate, normalize
from siege.orchestrator.goals import load_goals


@pytest.fixture(scope="module")
def g1():
    return load_goals()["G1"]


def cand(policy, rationale="blocks the abuse"):
    return CedarCandidate(policy=policy, rationale=rationale)


# The correct G1 fix: a refund only by the order's owner.
VALID_G1 = (
    'forbid (\n  principal,\n  action == Action::"issueRefund",\n  resource\n) '
    "unless {\n  resource.owner == principal\n};"
)
# Validates as Cedar syntax but uses a non-existent attribute (Users have no `owner`), §9.4.
BAD_VALIDATION = (
    'forbid (\n  principal,\n  action == Action::"issueRefund",\n  resource\n) '
    "unless {\n  principal.owner == resource\n};"
)
# Valid and well-typed, but too permissive: it only forbids refunds above $10,000,
# so alice refunding bob's 5521 is still allowed -> fails the goal's decision tests.
TOO_PERMISSIVE = (
    'forbid (\n  principal,\n  action == Action::"issueRefund",\n  resource\n) '
    "when {\n  context.amount > 1000000\n};"
)


# --- normalize (P4.7: unit tests, incl. `and` inside a string literal) -------


def test_normalize_strips_code_fences():
    assert normalize("```cedar\nforbid (principal, action, resource);\n```") == \
        "forbid (principal, action, resource);"


def test_normalize_appends_a_missing_semicolon():
    assert normalize("forbid (principal, action, resource)").endswith(";")


def test_normalize_swaps_and_or_outside_strings():
    out = normalize("forbid (p, a, r) when { x == 1 and y == 2 or z == 3 };")
    assert "&&" in out and "||" in out
    assert " and " not in out and " or " not in out


def test_normalize_leaves_and_inside_a_string_literal_alone():
    out = normalize('forbid (p, a, r) when { r.owner == "alpha and omega" };')
    assert '"alpha and omega"' in out  # the `and` inside the string is untouched
    assert "&&" not in out


def test_normalize_swaps_outside_but_not_inside_a_string():
    out = normalize('forbid (p, a, r) when { x == "cats or dogs" and y == 1 };')
    assert '"cats or dogs"' in out  # the `or` inside the string survives
    assert "&&" in out and "||" not in out


def test_normalize_drops_a_stray_semicolon_inside_a_condition(g1):
    # The 14b writes `resource.owner != principal;` inside the when-block (§9.4);
    # the stray `;` is removed so the policy becomes valid and correct.
    import re
    raw = 'forbid (principal, action == Action::"issueRefund", resource) when { resource.owner != principal; }'
    out = normalize(raw)
    assert not re.search(r";\s*}", out)  # no stray terminator left inside the block
    assert out.endswith(";")             # the policy itself still ends with one
    ok, error = cedar_gen.check(out, g1)
    assert ok, error


# --- check: validation + decision tests (§6.5 steps 3-4) --------------------


def test_check_accepts_the_correct_g1_policy(g1):
    ok, error = cedar_gen.check(VALID_G1, g1)
    assert ok and error == ""


def test_check_rejects_a_permit(g1):
    ok, error = cedar_gen.check("permit (principal, action, resource);", g1)
    assert not ok and "permit" in error


def test_check_rejects_two_policies(g1):
    ok, error = cedar_gen.check(VALID_G1 + "\n" + VALID_G1, g1)
    assert not ok and "forbid" in error  # must be exactly one


def test_check_rejects_an_invalid_attribute(g1):
    ok, error = cedar_gen.check(BAD_VALIDATION, g1)
    assert not ok and "validation" in error


def test_check_rejects_a_too_permissive_policy(g1):
    ok, error = cedar_gen.check(TOO_PERMISSIVE, g1)
    assert not ok and "wrong decisions" in error


# --- the cascade with the fake LLM (P4.7) -----------------------------------


def test_generate_accepts_a_valid_first_answer(g1):
    with override_provider("cedar", FakeProvider([cand(VALID_G1)])):
        result = cedar_gen.generate_policy(g1, models=("m1",))
    assert result.source == "generated" and result.valid and result.model == "m1"
    assert len(result.attempts) == 1 and result.attempts[0].ok


def test_generate_retries_after_a_validator_error(g1):
    with override_provider("cedar", FakeProvider([cand(BAD_VALIDATION), cand(VALID_G1)])):
        result = cedar_gen.generate_policy(g1, models=("m1",))
    assert result.source == "generated" and result.model == "m1"
    assert [a.ok for a in result.attempts] == [False, True]
    assert "validation" in result.attempts[0].error


def test_generate_retries_after_a_wrong_decision(g1):
    with override_provider("cedar", FakeProvider([cand(TOO_PERMISSIVE), cand(VALID_G1)])):
        result = cedar_gen.generate_policy(g1, models=("m1",))
    assert result.source == "generated"
    assert [a.ok for a in result.attempts] == [False, True]
    assert "wrong decisions" in result.attempts[0].error


def test_generate_moves_to_the_next_model_after_two_failures(g1):
    # m1 fails its try and its retry; m2 gets it right.
    script = [cand(BAD_VALIDATION), cand(BAD_VALIDATION), cand(VALID_G1)]
    with override_provider("cedar", FakeProvider(script)):
        result = cedar_gen.generate_policy(g1, models=("m1", "m2"))
    assert result.source == "generated" and result.model == "m2"
    assert [(a.model, a.ok) for a in result.attempts] == [("m1", False), ("m1", False), ("m2", True)]


def test_generate_falls_back_when_every_model_fails(g1):
    with override_provider("cedar", FakeProvider([cand(BAD_VALIDATION)] * 4)):
        result = cedar_gen.generate_policy(g1, models=("m1", "m2"))
    assert result.source == "fallback" and result.model is None and result.valid
    assert result.cedar_text == cedar.load_policy_file(g1.fallback)
    assert len(result.attempts) == 4 and not any(a.ok for a in result.attempts)


def test_generate_treats_unparseable_output_as_a_failed_attempt(g1):
    with override_provider("cedar", FakeProvider(["not json at all", cand(VALID_G1)])):
        result = cedar_gen.generate_policy(g1, models=("m1",))
    assert result.source == "generated"
    assert result.attempts[0].policy is None and not result.attempts[0].ok


# --- facts for the prompt ----------------------------------------------------


def test_facts_from_finding_reads_the_evidence_row(g1):
    finding = {"evidence_tool_call": {"tool_call": {
        "tool": "issue_refund", "principal": "alice", "principal_attrs": {"role": "customer"},
        "args": {"order_id": "5521", "amount_cents": 19999},
        "resource_attrs": {"owner": "bob", "total": 19999}}}}
    facts = cedar_gen.facts_from_finding(finding, g1)
    assert facts["tool"] == "issue_refund" and facts["action"] == "issueRefund"
    assert facts["principal"] == "alice" and facts["resource_attrs"]["owner"] == "bob"


def test_generate_accepts_a_valid_g2_policy():
    # G2 is a different goal (notes staff-only); the same pipeline must handle it.
    g2 = load_goals()["G2"]
    valid_g2 = 'forbid (principal, action == Action::"readNotes", resource) unless { principal.role == "staff" };'
    with override_provider("cedar", FakeProvider([cand(valid_g2)])):
        result = cedar_gen.generate_policy(g2, models=("m1",))
    assert result.source == "generated" and result.valid and result.model == "m1"


def test_facts_from_finding_falls_back_to_the_predicate_tool(g1):
    # a canary-only G2 finding has no tool_call row; fall back to the predicate's tool
    from siege.orchestrator.goals import load_goals as _lg
    g2 = _lg()["G2"]
    facts = cedar_gen.facts_from_finding({"evidence_tool_call": {"kind": "canary"}}, g2)
    assert facts["tool"] == "get_customer_notes" and facts["action"] == "readNotes"
