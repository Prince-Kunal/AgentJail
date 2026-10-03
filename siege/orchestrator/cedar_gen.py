"""Generate a Cedar fix for a finding, validate it, and self-correct (plan §6.4-6.5, P4.3).

An LLM writes one `forbid` policy for a finding. Because small local models get
Cedar subtly wrong (§9.4), every candidate is:

  1. **normalised** deterministically -- strip code fences; turn `and`/`or` into
     `&&`/`||` outside string literals; append a missing `;`;
  2. **validated** with `cedarpy` against `base.cedar` + the candidate -- it must
     be exactly one `forbid` and no `permit`;
  3. **decision-tested** against the goal's `decision_tests` and the seed entities.

A failure is sent back to the *same* model once; if it still fails, the next model
in the cascade tries (default `qwen2.5:14b` then `qwen2.5:7b`, from §6.5). If every
model fails, the goal's hand-written fallback is used (`source=fallback`). A policy
that fails validation or a decision test is never returned as `generated` -- the
plan's hard rule (§6.5). `fix_run` stores each result in the `policies` table with
its source, model and the full attempt log.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any

from siege.config import get_settings
from siege.llm import LLMUnavailableError, Message, llm_call
from siege.orchestrator import cedar
from siege.orchestrator.goals import Goal, load_goals
from siege.orchestrator.store import Run, Store
from pydantic import BaseModel

# Tool -> Cedar action (§3.2); used only to describe the finding to the generator.
ACTION_BY_TOOL = {
    "get_order": "getOrder",
    "issue_refund": "issueRefund",
    "read_inbox": "readInbox",
    "get_customer_notes": "readNotes",
}
_TOOL_BY_PREDICATE = {
    "unauthorized_refund": "issue_refund",
    "notes_leak": "get_customer_notes",
    "over_refund": "issue_refund",
}


class CedarCandidate(BaseModel):
    """The structured output the generator must return."""

    policy: str
    rationale: str


@dataclass
class GenAttempt:
    model: str
    attempt: int  # 1 = first try, 2 = the one allowed retry
    policy: str | None
    ok: bool
    error: str  # why it was rejected (validator message or wrong decisions); "" when ok


@dataclass
class PolicyResult:
    goal_id: str
    cedar_text: str
    rationale: str
    source: str  # "generated" | "fallback"
    model: str | None  # the model that wrote it, or None for the fallback
    valid: bool
    attempts: list[GenAttempt] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Normalisation (§6.5 step 2) -- deterministic, no LLM
# ---------------------------------------------------------------------------

_STRING = re.compile(r'"(?:\\.|[^"\\])*"')  # a Cedar double-quoted string literal
_COMMENT = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_EFFECT = re.compile(r"\b(permit|forbid)\b")


def _strip_code_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):  # ``` or ```cedar
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines)
    return t.strip()


def _clean_outside_strings(segment: str) -> str:
    segment = re.sub(r"\band\b", "&&", segment)  # Cedar uses && / ||, not the words
    segment = re.sub(r"\bor\b", "||", segment)
    # A `;` directly before a `}` is a stray terminator the models put inside a
    # condition block (the 14b does this for G1, §9.4); conditions hold a boolean
    # expression and never a `;`, so dropping it is always safe.
    segment = re.sub(r";\s*}", "}", segment)
    return segment


def _clean_outside_string_literals(text: str) -> str:
    """Apply the syntax fixes everywhere except inside a string literal."""
    out: list[str] = []
    last = 0
    for m in _STRING.finditer(text):
        out.append(_clean_outside_strings(text[last:m.start()]))
        out.append(m.group(0))  # the string literal, left exactly as written
        last = m.end()
    out.append(_clean_outside_strings(text[last:]))
    return "".join(out)


def normalize(text: str) -> str:
    """Clean a model's policy into canonical Cedar (§6.5 step 2)."""
    text = _strip_code_fences(text)
    text = _clean_outside_string_literals(text).strip()
    if text and not text.endswith(";"):
        text += ";"
    return text


def _count_effects(policy: str) -> tuple[int, int]:
    """(permits, forbids) in the candidate, ignoring comments and string literals."""
    stripped = _STRING.sub('""', _COMMENT.sub("", policy))
    heads = _EFFECT.findall(stripped)
    return heads.count("permit"), heads.count("forbid")


# ---------------------------------------------------------------------------
# Validation + decision tests (§6.5 steps 3-4)
# ---------------------------------------------------------------------------


def check(policy: str, goal: Goal) -> tuple[bool, str]:
    """A candidate is good only if it is one forbid, validates, and passes every decision test."""
    permits, forbids = _count_effects(policy)
    if forbids != 1 or permits != 0:
        return False, f"must be exactly one forbid policy and no permit (found {forbids} forbid, {permits} permit)"

    full = cedar.policy_set(policy)
    validation = cedar.validate(full)
    if not validation.validation_passed:
        return False, "validation failed: " + ("; ".join(str(e) for e in validation.errors) or "invalid policy")

    bad = [r for r in cedar.run_decision_tests(full, goal.decision_tests) if not r.ok]
    if bad:
        detail = "; ".join(f"{r.principal} {r.action} {r.resource}: expected {r.expect}, got {r.got}" for r in bad)
        return False, "wrong decisions: " + detail
    return True, ""


# ---------------------------------------------------------------------------
# The prompt (§6.4): schema + facts + syntax rules + three worked OTHER rules
# ---------------------------------------------------------------------------


def _build_system_prompt() -> str:
    schema = cedar.load_schema().strip()
    return f"""\
You write authorization policies in the Cedar policy language. Given one abuse to \
prevent, you return exactly ONE Cedar `forbid` policy that stops it while leaving \
every other action allowed.

The Cedar schema is:

{schema}

Facts about this schema (follow them exactly):
- A User has only the attribute `role` (a string, e.g. "customer" or "staff"). \
Users have no `owner` and no `id` attribute.
- An Order has `owner` (a User) and `total` (a Long, whole cents).
- An Inbox has `owner` (a User).
- For issueRefund the requested amount is in the request context as `context.amount` (a Long).
- Compare the requester directly to an owner: `resource.owner == principal`.

Cedar syntax rules:
- Use `&&` and `||`, never the words `and` or `or`.
- Every policy ends with a semicolon `;`.
- Return exactly ONE `forbid` policy. Never write a `permit` (a base permit already exists).
- A `forbid ... unless {{ C }}` blocks the action except when C holds; \
`forbid ... when {{ C }}` blocks it only when C holds.

Worked examples of OTHER rules (do not copy them; follow their shape):

A customer may read only their own inbox:
forbid (
  principal,
  action == Action::"readInbox",
  resource
) unless {{
  resource.owner == principal
}};

Only staff may look up orders:
forbid (
  principal,
  action == Action::"getOrder",
  resource
) unless {{
  principal.role == "staff"
}};

A refund may never exceed the order total:
forbid (
  principal,
  action == Action::"issueRefund",
  resource
) when {{
  context.amount > resource.total
}};

Return JSON with two fields: `policy` (the Cedar policy text) and `rationale` \
(one sentence explaining what it blocks)."""


SYSTEM_PROMPT = _build_system_prompt()


def facts_from_finding(finding: dict[str, Any], goal: Goal) -> dict[str, Any]:
    """Pull the grounding facts for the prompt out of a finding's evidence (§6.4)."""
    tool_call = ((finding.get("evidence_tool_call") or {}).get("tool_call")) or {}
    tool = tool_call.get("tool") or _TOOL_BY_PREDICATE.get(goal.predicate, "")
    return {
        "tool": tool,
        "action": ACTION_BY_TOOL.get(tool, ""),
        "principal": tool_call.get("principal"),
        "principal_attrs": tool_call.get("principal_attrs") or {},
        "args": tool_call.get("args") or {},
        "resource_attrs": tool_call.get("resource_attrs") or {},
    }


def _user_prompt(goal: Goal, facts: dict[str, Any]) -> str:
    action = facts.get("action") or "?"
    principal = facts.get("principal") or "a customer"
    role = (facts.get("principal_attrs") or {}).get("role", "?")
    resource = facts.get("resource_attrs") or {}
    resource_desc = ", ".join(f"{k}={v}" for k, v in resource.items()) or "unknown"
    return f"""\
A security test found this abuse, which must be prevented:

- Tool abused: {facts.get('tool') or '?'}  (Cedar action: Action::"{action}")
- Called by principal: User::"{principal}" (role: {role})
- On a resource with attributes: {resource_desc}
- Arguments: {facts.get('args')}

The rule to enforce:
{goal.rule.strip()}

Write one Cedar `forbid` policy that enforces this rule for everyone (not just this \
one principal or resource), and leaves all legitimate use allowed. Return JSON \
{{policy, rationale}}."""


# ---------------------------------------------------------------------------
# The cascade (§6.5)
# ---------------------------------------------------------------------------


def _call_model(goal: Goal, facts: dict[str, Any], model: str,
                prev_policy: str | None, prev_error: str | None) -> CedarCandidate | None:
    messages = [Message.user(_user_prompt(goal, facts))]
    if prev_error:
        correction = "Your previous policy was rejected.\n"
        if prev_policy:
            correction += f"Previous policy:\n{prev_policy}\n"
        correction += f"Reason: {prev_error}\nReturn a corrected policy as JSON {{policy, rationale}}."
        messages.append(Message.user(correction))
    try:
        result = llm_call("cedar", SYSTEM_PROMPT, messages, schema=CedarCandidate, model=model)
    except LLMUnavailableError:
        return None
    if not result.ok or result.parsed is None:
        return None
    return result.parsed  # a CedarCandidate


def generate_policy(goal: Goal, facts: dict[str, Any] | None = None, *,
                    models: tuple[str, ...] | None = None) -> PolicyResult:
    """Generate a validated Cedar fix for `goal`, falling back when the cascade can't (§6.5)."""
    models = tuple(models) if models is not None else get_settings().cedar_models
    facts = facts or {}
    attempts: list[GenAttempt] = []

    for model in models:
        prev_policy: str | None = None
        prev_error: str | None = None
        for attempt_no in (1, 2):  # the first try plus the one allowed retry
            candidate = _call_model(goal, facts, model, prev_policy, prev_error)
            if candidate is None:
                attempts.append(GenAttempt(model, attempt_no, None, False, "model returned no valid JSON"))
                prev_policy, prev_error = None, "Return JSON {policy, rationale} with exactly one forbid policy."
                continue
            policy = normalize(candidate.policy)
            ok, error = check(policy, goal)
            attempts.append(GenAttempt(model, attempt_no, policy, ok, error))
            if ok:
                return PolicyResult(goal.id, policy, candidate.rationale.strip(), "generated", model, True, attempts)
            prev_policy, prev_error = policy, error

    fallback = cedar.load_policy_file(goal.fallback)
    return PolicyResult(
        goal.id, fallback,
        "Hand-written fallback policy; the generator could not produce a valid one.",
        "fallback", None, True, attempts,
    )


# ---------------------------------------------------------------------------
# Orchestration: generate + store a fix per finding of a run (used by `siege fix`)
# ---------------------------------------------------------------------------


def fix_run(run: Run, store: Store, *, models: tuple[str, ...] | None = None,
            skip_existing: bool = True, on_policy=None) -> list[tuple[dict[str, Any], PolicyResult]]:
    """Generate and store a Cedar fix for each finding of `run` (plan §6.4-6.5)."""
    goals = load_goals()
    done: list[tuple[dict[str, Any], PolicyResult]] = []
    for finding in store.findings(run.id):
        if skip_existing and any(p["valid"] for p in store.policies(finding["id"])):
            continue  # already fixed (idempotent re-runs, and demo reusing a fix)
        goal = goals[finding["goal_id"]]
        result = generate_policy(goal, facts_from_finding(finding, goal), models=models)
        store.record_policy(
            finding["id"], cedar_text=result.cedar_text, rationale=result.rationale,
            source=result.source, model=result.model,
            attempts=[dataclasses.asdict(a) for a in result.attempts], valid=result.valid,
        )
        done.append((finding, result))
        if on_policy is not None:
            on_policy(finding, result)
    return done
