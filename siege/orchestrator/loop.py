"""The orchestrator loop and its gate (plan §5).

`policy_check` is the instruction-source boundary, in code (P3.2): the attacker
only ever *proposes*, and this decides whether a proposal may be sent. It checks
the proposal is well formed, the message is a plain string within a length cap,
the destination is the configured in-scope target, and the turn is within the cap.

`run_session` (P3.3) runs one goal end to end: it binds a target session as the
attacker user, then each turn asks the attacker for a message, gates it through
`policy_check`, sends it to the target, decides a breach from the tool log and
canary (D5), labels the refusal (advisory), and persists everything. A rejected
proposal and an attacker refusal each still consume a turn (plan §2). It stops at
the first breach with a stored finding, or after MAX_TURNS with none.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Callable

from siege.config import get_settings
from siege.orchestrator.attacker import (
    STRATEGY_FAMILIES,
    AttackProposal,
    AttackState,
    TurnContext,
    propose_attack,
)
from siege.orchestrator.evaluator import evaluate, label_refusal
from siege.orchestrator.goals import Goal
from siege.orchestrator.store import Run, Store

MAX_MESSAGE_LEN = 4000


@dataclass
class CheckResult:
    ok: bool
    reason: str = "ok"

    def __bool__(self) -> bool:
        return self.ok


def policy_check(
    proposal: AttackProposal,
    *,
    target_url: str,
    turn: int,
    max_turns: int | None = None,
    max_message_len: int = MAX_MESSAGE_LEN,
) -> CheckResult:
    """Decide whether `proposal` may be sent to `target_url` on `turn` (plan §5)."""
    settings = get_settings()
    if max_turns is None:
        max_turns = settings.max_turns

    if not isinstance(proposal, AttackProposal):
        return CheckResult(False, "proposal is not an AttackProposal")
    if proposal.strategy not in STRATEGY_FAMILIES:
        return CheckResult(False, f"unknown strategy {proposal.strategy!r}")

    message = proposal.message
    if not isinstance(message, str) or not message.strip():
        return CheckResult(False, "message must be a non-empty string")
    if len(message) > max_message_len:
        return CheckResult(False, f"message exceeds the length cap ({len(message)} > {max_message_len})")

    # The attacker can't redirect where messages go: only the configured target is in scope.
    if target_url != settings.target_url:
        return CheckResult(False, f"target {target_url!r} is not the in-scope target {settings.target_url!r}")

    if not 0 <= turn < max_turns:
        return CheckResult(False, f"turn {turn} is outside the cap [0, {max_turns})")

    return CheckResult(True, "ok")


# ---------------------------------------------------------------------------
# The session loop (plan §5, P3.3)
# ---------------------------------------------------------------------------


@dataclass
class TurnEvent:
    """One turn's outcome, for live display (CLI) or assertions (tests)."""

    turn: int
    kind: str  # "sent" | "rejected" | "attacker_refused"
    strategy: str | None = None
    message: str | None = None
    reply: str | None = None
    refusal_type: str | None = None
    breach: bool = False
    tool_calls: list[dict[str, Any]] | None = None
    reason: str | None = None  # why a proposal was rejected


@dataclass
class SessionResult:
    goal_id: str
    breached: bool
    finding_id: int | None
    turns_used: int
    history: list[TurnContext]


def run_session(
    goal: Goal,
    run: Run,
    target: Any,
    store: Store,
    *,
    max_turns: int | None = None,
    attacker_user: str | None = None,
    on_event: Callable[[TurnEvent], None] | None = None,
) -> SessionResult:
    """Run `goal` against `target` for up to MAX_TURNS, persisting everything to `store`."""
    settings = get_settings()
    max_turns = max_turns if max_turns is not None else settings.max_turns
    attacker_user = attacker_user or settings.attacker_user

    def emit(event: TurnEvent) -> None:
        if on_event is not None:
            on_event(event)

    sid = target.create_session(user_id=attacker_user, enforce=False)
    history: list[TurnContext] = []
    state = AttackState(goal_id=goal.id)

    for turn in range(max_turns):
        proposal_result = propose_attack(goal, history, state)
        if proposal_result.refused:
            store.record_attempt(run.id, goal.id, turn, status="attacker_refused")
            emit(TurnEvent(turn, "attacker_refused"))
            continue

        proposal = proposal_result.proposal
        check = policy_check(proposal, target_url=target.base_url, turn=turn, max_turns=max_turns)
        if not check:
            store.record_attempt(
                run.id, goal.id, turn, strategy=proposal.strategy, message=proposal.message, status="rejected"
            )
            emit(TurnEvent(turn, "rejected", strategy=proposal.strategy, message=proposal.message, reason=check.reason))
            continue

        chat = target.chat(sid, proposal.message)
        verdict = evaluate(goal, chat.reply, chat.tool_calls, run.canary)
        verdict.refusal_type = label_refusal(chat.reply)
        history.append(TurnContext(proposal.strategy, proposal.message, chat.reply, verdict.refusal_type))
        store.record_attempt(
            run.id, goal.id, turn,
            strategy=proposal.strategy, message=proposal.message, reply=chat.reply,
            refusal_type=verdict.refusal_type, status="sent", breach=verdict.breach,
        )
        emit(TurnEvent(
            turn, "sent", strategy=proposal.strategy, message=proposal.message, reply=chat.reply,
            refusal_type=verdict.refusal_type, breach=verdict.breach, tool_calls=chat.tool_calls,
        ))

        if verdict.breach:
            finding_id = store.record_finding(
                run.id, goal.id,
                turns_to_breach=turn + 1,
                winning_strategy=proposal.strategy,
                evidence_tool_call=dataclasses.asdict(verdict.evidence) if verdict.evidence else None,
                severity=goal.severity,
            )
            return SessionResult(goal.id, True, finding_id, turn + 1, history)

        state.update(proposal.strategy, verdict.refusal_type, chat.reply)

    return SessionResult(goal.id, False, None, max_turns, history)
