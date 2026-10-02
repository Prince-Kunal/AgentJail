"""The orchestrator loop and its gate (plan §5).

`policy_check` is the instruction-source boundary, in code (P3.2): the attacker
only ever *proposes*, and this decides whether a proposal may be sent. It checks
the proposal is well formed, the message is a plain string within a length cap,
the destination is the configured in-scope target, and the turn is within the
cap. `run_session` (the loop that ties the attacker, target and evaluator
together) is added in P3.3.
"""

from __future__ import annotations

from dataclasses import dataclass

from siege.config import get_settings
from siege.orchestrator.attacker import STRATEGY_FAMILIES, AttackProposal

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
