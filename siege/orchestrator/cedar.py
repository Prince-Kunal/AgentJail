"""Host-side Cedar helpers (plan §6.1, §6.5).

Loads the schema/base/fallback files under siege/cedar/, builds the seed entity
set, and runs validation and decision tests. This is the orchestrator's view,
used to check fallback and generated policies (P4.1, P4.3) and the rerun (P4.4).
The PEP builds its own entities from the DB; it does not import this.

The seed entities mirror the §3.3 data in siege/target/seed.py; a test cross-checks
they stay in sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import cedarpy

PACKAGE_DIR = Path(__file__).resolve().parent.parent  # siege/
CEDAR_DIR = PACKAGE_DIR / "cedar"

# Seed data mirrored from siege/target/seed.py (§3.3), for host-side decision tests.
SEED_USERS = {"alice": "customer", "bob": "customer", "carol": "staff"}
SEED_ORDERS = {"5518": ("alice", 2499), "5520": ("alice", 4599), "5521": ("bob", 19999), "5524": ("bob", 3999)}
SEED_INBOX_OWNERS = ("alice", "bob")


@lru_cache(maxsize=1)
def load_schema() -> str:
    return (CEDAR_DIR / "schema.cedarschema").read_text()


def load_base() -> str:
    return (CEDAR_DIR / "base.cedar").read_text()


def load_policy_file(rel_path: str) -> str:
    """Load a policy by a goals.yaml-style path such as 'cedar/fallback/G1.cedar'."""
    return (PACKAGE_DIR / rel_path).read_text()


def policy_set(*policies: str) -> str:
    """base.cedar plus the given policies, the set the PEP actually enforces (D4)."""
    return "\n\n".join([load_base(), *policies])


def _uref(user_id: str) -> dict[str, Any]:
    return {"__entity": {"type": "User", "id": user_id}}


def seed_entities() -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for uid, role in SEED_USERS.items():
        entities.append({"uid": {"type": "User", "id": uid}, "attrs": {"role": role}, "parents": []})
    for oid, (owner, total) in SEED_ORDERS.items():
        entities.append({"uid": {"type": "Order", "id": oid},
                         "attrs": {"owner": _uref(owner), "total": total}, "parents": []})
    for owner in SEED_INBOX_OWNERS:
        entities.append({"uid": {"type": "Inbox", "id": owner},
                         "attrs": {"owner": _uref(owner)}, "parents": []})
    return entities


def validate(policies: str):
    """cedarpy.validate_policies against the schema; .validation_passed / .errors."""
    return cedarpy.validate_policies(policies, load_schema())


def decide(policies: str, principal: str, action: str, resource: str,
           context: dict | None = None, entities: list | None = None) -> tuple[str, Any]:
    """Return ('allow'|'deny', raw_result). Any evaluation error counts as deny (§6.6)."""
    request = {
        "principal": f'User::"{principal}"',
        "action": f'Action::"{action}"',
        "resource": resource,
        "context": context or {},
    }
    result = cedarpy.is_authorized(request, policies, seed_entities() if entities is None else entities, schema=load_schema())
    allow = str(result.decision).endswith("Allow") and not result.diagnostics.errors
    return ("allow" if allow else "deny"), result


@dataclass
class DecisionTestResult:
    principal: str
    action: str
    resource: str
    expect: str
    got: str
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.got == self.expect


def run_decision_tests(policies: str, decision_tests, entities: list | None = None) -> list[DecisionTestResult]:
    results = []
    for t in decision_tests:
        got, raw = decide(policies, t.principal, t.action, t.resource, t.context, entities)
        results.append(DecisionTestResult(
            t.principal, t.action, t.resource, t.expect, got, [str(e) for e in raw.diagnostics.errors]
        ))
    return results


def decision_tests_pass(policies: str, decision_tests, entities: list | None = None) -> bool:
    return all(r.ok for r in run_decision_tests(policies, decision_tests, entities))
