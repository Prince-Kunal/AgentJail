"""Load and validate the attack goals from goals.yaml (plan §3.4, P2.1).

The Goal model is the shared shape the evaluator (the `predicate`), the Cedar
generator (the `rule` and `decision_tests`) and the loop (the `description` and
`severity`) all read.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

SEVERITIES = ("critical", "high", "medium", "low")
EXPECTS = ("allow", "deny")

# siege/goals.yaml, next to the package (this file is siege/orchestrator/goals.py).
DEFAULT_GOALS_PATH = Path(__file__).resolve().parent.parent / "goals.yaml"


class DecisionTest(BaseModel):
    """One expected Cedar decision for a goal's policy (plan §6.5)."""

    principal: str
    action: str
    resource: str
    context: dict = Field(default_factory=dict)
    expect: str

    @field_validator("expect")
    @classmethod
    def _known_expect(cls, value: str) -> str:
        if value not in EXPECTS:
            raise ValueError(f"expect={value!r}; must be one of {EXPECTS}")
        return value


class Goal(BaseModel):
    id: str
    description: str
    predicate: str
    severity: str
    fallback: str
    rule: str
    decision_tests: list[DecisionTest]

    @field_validator("severity")
    @classmethod
    def _known_severity(cls, value: str) -> str:
        if value not in SEVERITIES:
            raise ValueError(f"severity={value!r}; must be one of {SEVERITIES}")
        return value

    @field_validator("decision_tests")
    @classmethod
    def _non_empty(cls, value: list[DecisionTest]) -> list[DecisionTest]:
        if not value:
            raise ValueError("a goal needs at least one decision_test")
        return value


def load_goals(path: str | Path | None = None) -> dict[str, Goal]:
    """Load goals.yaml into an ordered {id: Goal} map, rejecting duplicate IDs."""
    path = Path(path) if path else DEFAULT_GOALS_PATH
    raw = yaml.safe_load(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"{path} must hold a list of goals, got {type(raw).__name__}")
    goals: dict[str, Goal] = {}
    for entry in raw:
        goal = Goal(**entry)
        if goal.id in goals:
            raise ValueError(f"duplicate goal id {goal.id!r} in {path}")
        goals[goal.id] = goal
    return goals
