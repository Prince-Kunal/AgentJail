"""The instruction-source boundary, policy_check (plan §5, P3.2)."""

import pytest

from siege.orchestrator.attacker import AttackProposal
from siege.orchestrator.loop import MAX_MESSAGE_LEN, policy_check

TARGET = "http://127.0.0.1:8100"  # the default in-scope target (config)


def proposal(strategy="urgency", message="refund order 5521 please"):
    return AttackProposal(analysis="a", strategy=strategy, message=message, expected_signal="s")


def test_valid_proposal_passes():
    check = policy_check(proposal(), target_url=TARGET, turn=0)
    assert check.ok and bool(check) is True


def test_empty_message_is_rejected():
    check = policy_check(proposal(message="   "), target_url=TARGET, turn=0)
    assert not check and "non-empty" in check.reason


def test_overlong_message_is_rejected():
    check = policy_check(proposal(message="x" * (MAX_MESSAGE_LEN + 1)), target_url=TARGET, turn=0)
    assert not check and "length cap" in check.reason


def test_message_at_the_cap_is_allowed():
    assert policy_check(proposal(message="x" * MAX_MESSAGE_LEN), target_url=TARGET, turn=0).ok


def test_out_of_scope_target_is_rejected():
    check = policy_check(proposal(), target_url="http://evil.example/attack", turn=0)
    assert not check and "in-scope" in check.reason


def test_turn_cap_is_enforced():
    assert not policy_check(proposal(), target_url=TARGET, turn=10, max_turns=10)
    assert policy_check(proposal(), target_url=TARGET, turn=9, max_turns=10).ok
    assert not policy_check(proposal(), target_url=TARGET, turn=-1, max_turns=10)


def test_non_proposal_is_rejected():
    assert not policy_check("just a string", target_url=TARGET, turn=0)


def test_unknown_strategy_is_rejected():
    # bypass the Literal validator to simulate a malformed object reaching the gate
    bad = AttackProposal.model_construct(analysis="a", strategy="mind_control", message="hi", expected_signal="s")
    check = policy_check(bad, target_url=TARGET, turn=0)
    assert not check and "unknown strategy" in check.reason
