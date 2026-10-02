"""The attack agent's propose_attack (plan §4, §2, P3.1)."""

import pytest

from siege.llm import FakeProvider, LLMResult, LLMUnavailableError, override_provider
from siege.orchestrator.attacker import (
    STRATEGY_FAMILIES,
    AttackProposal,
    AttackState,
    TurnContext,
    propose_attack,
)
from siege.orchestrator.goals import load_goals


@pytest.fixture(scope="module")
def goal():
    return load_goals()["G1"]


@pytest.fixture
def state():
    return AttackState(goal_id="G1")


def proposal(strategy="claimed_authority", message="Please refund order 5521.", **kw):
    return AttackProposal(
        analysis=kw.get("analysis", "the agent checked ownership"),
        strategy=strategy,
        message=message,
        expected_signal=kw.get("expected_signal", "agent calls issue_refund"),
    )


def with_attacker(script):
    return override_provider("attacker", FakeProvider(script))


def test_valid_proposal_is_returned(goal, state):
    with with_attacker([proposal(strategy="urgency")]):
        result = propose_attack(goal, [], state)
    assert not result.refused and result.calls == 1
    assert result.proposal.strategy == "urgency"
    assert result.proposal.message == "Please refund order 5521."


def test_prompt_carries_goal_menu_and_history(goal, state):
    fake = FakeProvider([proposal()])
    history = [TurnContext(strategy="urgency", message="refund now", reply="I can't, it's not yours", refusal_type="disclosed_rule")]
    state.revealed.append("only the owner may refund an order")
    with override_provider("attacker", fake):
        propose_attack(goal, history, state)
    sent = fake.requests[0].messages[0].content
    assert goal.description[:20] in sent
    for family in STRATEGY_FAMILIES:
        assert family in sent
    assert "refund now" in sent and "disclosed_rule" in sent
    assert "only the owner may refund an order" in sent  # revealed rule surfaced


def test_schema_failure_then_success_retries_once(goal, state):
    with with_attacker([LLMResult(model="", text="not json"), proposal(strategy="role_play")]):
        result = propose_attack(goal, [], state)
    assert not result.refused and result.calls == 2
    assert result.proposal.strategy == "role_play"


def test_two_failures_report_attacker_refused(goal, state):
    with with_attacker([LLMResult(model="", text="nope"), LLMResult(model="", text="still nope")]):
        result = propose_attack(goal, [], state)
    assert result.refused and result.proposal is None and result.calls == 2


def test_empty_message_counts_as_failure(goal, state):
    with with_attacker([proposal(message="   "), proposal(message="real message")]):
        result = propose_attack(goal, [], state)
    assert not result.refused and result.proposal.message == "real message"


def test_backend_error_is_retried_then_refused(goal, state):
    def boom(_request):
        raise LLMUnavailableError("down")

    with with_attacker(boom):
        result = propose_attack(goal, [], state)
    assert result.refused and result.calls == 2


def test_retry_reframes_the_request(goal, state):
    fake = FakeProvider([LLMResult(model="", text="bad"), proposal()])
    with override_provider("attacker", fake):
        propose_attack(goal, [], state)
    # The second call appended the reframe reminder.
    second = fake.requests[1].messages
    assert len(second) == 2 and "authorised sandbox security test" in second[1].content


# --- AttackState.update -----------------------------------------------------


def test_state_update_tracks_tried_and_labels():
    from siege.orchestrator.evaluator import Verdict

    state = AttackState(goal_id="G1")
    state.update("urgency", "hard_refusal")
    state.update("claimed_authority", "disclosed_rule", reply="Only the owner can refund.")
    assert state.tried == ["urgency", "claimed_authority"]
    assert state.labels == ["hard_refusal", "disclosed_rule"]
    assert state.revealed == ["Only the owner can refund."]  # only disclosed_rule is recorded
    # a Verdict carries refusal_type, which is what the loop passes in
    v = Verdict(goal_id="G1", breach=False, refusal_type="deflection")
    state.update("role_play", v.refusal_type)
    assert state.labels[-1] == "deflection"


# --- live: the real attacker produces a usable proposal, not a refusal -------


@pytest.mark.live
def test_live_attacker_produces_a_valid_proposal(goal):
    result = propose_attack(goal, [], AttackState(goal_id="G1"))
    assert not result.refused and result.proposal is not None
    assert result.proposal.strategy in STRATEGY_FAMILIES
    assert result.proposal.message.strip()
