"""The happy-path check (plan §6.7, P4.5), driven with a scripted target."""

from siege.orchestrator.target_client import ChatTurn
from siege.scripts.happy_path import default_policy_set, run_happy_path


def row(tool, decision="allow", executed=True):
    return {"tool": tool, "decision": decision, "executed": executed}


class FakeTarget:
    """A target client whose chat replies come from a handler keyed on the message."""

    def __init__(self, handler):
        self.handler = handler
        self.sessions: list[tuple[str, bool]] = []

    def create_session(self, user_id, enforce=False):
        self.sessions.append((user_id, enforce))
        return f"sess-{len(self.sessions)}"

    def chat(self, session_id, message):
        return self.handler(message)


def good_handler(message):
    m = message.lower()
    if "look up" in m:
        return ChatTurn("Here is your order.", [row("get_order")])
    if "refund" in m:
        return ChatTurn("Refunded.", [row("issue_refund")])
    if "inbox" in m:
        return ChatTurn("Your inbox:", [row("read_inbox")])
    return ChatTurn("ok", [])


def test_happy_path_passes_when_all_three_are_allowed():
    target = FakeTarget(good_handler)
    result = run_happy_path(target)
    assert result.ok
    assert [s.tool for s in result.steps] == ["get_order", "issue_refund", "read_inbox"]
    assert all(s.decision == "allow" and s.executed for s in result.steps)
    assert target.sessions == [("alice", True)]  # one enforce session, as alice (§6.7)


def test_happy_path_fails_when_a_legit_action_is_denied():
    def handler(message):
        if "refund" in message.lower():
            return ChatTurn("I can't.", [row("issue_refund", decision="deny", executed=False)])
        return good_handler(message)

    result = run_happy_path(FakeTarget(handler))
    assert not result.ok
    refund = next(s for s in result.steps if s.tool == "issue_refund")
    assert refund.decision == "deny" and not refund.executed


def test_happy_path_fails_when_the_agent_never_calls_the_tool():
    def handler(message):
        if "inbox" in message.lower():
            return ChatTurn("I won't read it.", [])
        return good_handler(message)

    result = run_happy_path(FakeTarget(handler))
    assert not result.ok
    inbox = next(s for s in result.steps if s.tool == "read_inbox")
    assert "never called" in inbox.detail


def test_happy_path_picks_the_action_even_if_the_agent_looks_first():
    def handler(message):
        if "refund" in message.lower():
            # the agent looks the order up, then refunds it: the refund is the action
            return ChatTurn("ok", [row("get_order"), row("issue_refund")])
        return good_handler(message)

    result = run_happy_path(FakeTarget(handler))
    assert result.ok
    refund = next(s for s in result.steps if s.tool == "issue_refund")
    assert refund.decision == "allow" and refund.executed


def test_summary_names_each_action():
    text = run_happy_path(FakeTarget(good_handler)).summary()
    assert "happy path OK" in text
    assert "look up her own order" in text and "read her own inbox" in text


def test_default_policy_set_is_base_plus_both_fallbacks():
    policies = default_policy_set()
    assert "permit (principal, action, resource)" in policies
    assert 'action == Action::"issueRefund"' in policies  # G1
    assert 'action == Action::"readNotes"' in policies     # G2
