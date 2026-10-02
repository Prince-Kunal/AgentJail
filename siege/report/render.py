"""Static HTML report from a run's SQLite data (plan §8, P5.1).

`render_run(store, run_id)` returns the HTML; `write_report(store, run_id)` writes it
to `runs/<run_id>/report.html`. Everything comes from the Store. The only extra input
is `goals.yaml` -- for each goal's description and its decision tests, which are
re-checked against the stored policy purely for display. No network and no live
target: the report is deterministic, so it renders the same from a recorded run.

The page tells one story per finding: the breach (before), the Cedar policy it forced
and who wrote it, and the rerun outcome (after). A summary strip and the happy-path
result frame it: attacks blocked, legitimate use intact.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from siege.config import get_settings
from siege.orchestrator import cedar
from siege.orchestrator.goals import load_goals
from siege.orchestrator.store import Store

TEMPLATE = Path(__file__).resolve().parent / "template.html"

_OUTCOME_PILL = {"BLOCKED": "blocked", "STILL_BREACHED": "still", "NOT_REPRODUCED": "notrepro"}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def _display_policy(policies: list[dict]) -> dict | None:
    """The policy to show for a finding: the latest valid one, else the latest attempt."""
    if not policies:
        return None
    valid = [p for p in policies if p["valid"]]
    return (valid or policies)[-1]


def _latest_replay(reruns: list[dict]) -> dict | None:
    replays = [r for r in reruns if r["mode"] == "replay"]
    return replays[-1] if replays else None


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _meta_html(run) -> str:
    models = run.config.get("models") or {}
    model_str = ", ".join(f"{role}: {name}" for role, name in models.items()) or "—"
    goals = ", ".join(run.config.get("goals") or []) or "—"
    bits = [
        f"<span><b>Run</b> {_esc(run.id)}</span>",
        f"<span><b>Started</b> {_esc(run.started_at)}</span>",
        f"<span><b>Goals</b> {_esc(goals)}</span>",
        f"<span><b>Max turns</b> {_esc(run.config.get('max_turns'))}</span>",
        f"<span><b>Models</b> {_esc(model_str)}</span>",
    ]
    return '<div class="meta">' + "".join(bits) + "</div>"


def _card(n: str, k: str, cls: str = "") -> str:
    return f'<div class="card {cls}"><div class="n">{_esc(n)}</div><div class="k">{_esc(k)}</div></div>'


def _summary_html(run, findings, policies_by_finding, reruns_by_finding, happy) -> str:
    goals_tested = len(run.config.get("goals") or sorted({f["goal_id"] for f in findings}))
    generated = fallback = 0
    for f in findings:
        p = _display_policy(policies_by_finding[f["id"]])
        if p and p["source"] == "generated":
            generated += 1
        elif p:
            fallback += 1
    blocked = sum(
        1 for f in findings
        if (_latest_replay(reruns_by_finding[f["id"]]) or {}).get("outcome") == "BLOCKED"
    )
    happy_ok = happy is not None and happy["ok"]
    cards = [
        _card(str(goals_tested), "goals tested"),
        _card(str(len(findings)), "breaches found", "bad" if findings else ""),
        _card(f"{generated}/{generated + fallback}", "policies model-generated"),
        _card(f"{blocked}/{len(findings)}", "breaches blocked on rerun", "good" if blocked == len(findings) and findings else ""),
        _card("OK" if happy_ok else ("FAILED" if happy is not None else "—"),
              "happy path", "good" if happy_ok else ("bad" if happy is not None else "")),
    ]
    return '<div class="cards">' + "".join(cards) + "</div>"


def _tool_call_html(tc: dict) -> str:
    args = tc.get("args")
    call = f'{tc.get("tool")}({_esc(json.dumps(args)) if args is not None else ""})'
    lines = [f"<pre><code>{call}</code></pre>"]
    kv = []
    if tc.get("principal") is not None:
        role = (tc.get("principal_attrs") or {}).get("role")
        kv.append(f'<div class="kv"><b>principal</b> {_esc(tc.get("principal"))}'
                  + (f" (role: {_esc(role)})" if role else "") + "</div>")
    if tc.get("resource_attrs"):
        res = ", ".join(f"{k}={v}" for k, v in tc["resource_attrs"].items())
        kv.append(f'<div class="kv"><b>resource</b> {_esc(res)}</div>')
    if "executed" in tc:
        kv.append(f'<div class="kv"><b>executed</b> {_esc(tc.get("executed"))}'
                  + (f' · <b>decision</b> {_esc(tc.get("decision"))}' if tc.get("decision") else "") + "</div>")
    return lines[0] + "".join(kv)


def _evidence_html(evidence: dict | None) -> str:
    if not evidence:
        return '<div class="muted">no evidence recorded</div>'
    parts = []
    if evidence.get("description"):
        parts.append(f'<div class="kv">{_esc(evidence["description"])}</div>')
    if evidence.get("tool_call"):
        parts.append(_tool_call_html(evidence["tool_call"]))
    if evidence.get("canary_snippet"):
        parts.append(f'<pre><code>{_esc(evidence["canary_snippet"])}</code></pre>')
    return "".join(parts) or '<div class="muted">no evidence recorded</div>'


def _decision_tests_html(policy: dict | None, goal) -> str:
    if not policy or not policy.get("cedar_text") or goal is None:
        return ""
    try:
        results = cedar.run_decision_tests(cedar.policy_set(policy["cedar_text"]), goal.decision_tests)
    except Exception:
        return ""
    rows = []
    for r in results:
        verdict = f'<span class="ok">✓ {_esc(r.got)}</span>' if r.ok else \
                  f'<span class="no">✗ got {_esc(r.got)}, wanted {_esc(r.expect)}</span>'
        rows.append(f"<tr><td>{_esc(r.principal)}</td><td>{_esc(r.action)}</td>"
                    f"<td><code>{_esc(r.resource)}</code></td><td>{verdict}</td></tr>")
    return ('<table class="dt"><tr><th>principal</th><th>action</th><th>resource</th>'
            '<th>decision</th></tr>' + "".join(rows) + "</table>")


def _policy_html(policy: dict | None, goal) -> str:
    if policy is None:
        return '<div class="policy muted">No policy was generated for this finding.</div>'
    who = "hand-written fallback" if policy["source"] == "fallback" else f"generated by {_esc(policy['model'])}"
    attempts = policy.get("attempts") or []
    out = ['<div class="policy">']
    out.append(f'<div class="src">Cedar policy — {who}, {len(attempts)} attempt(s), '
               f'{"valid" if policy["valid"] else "INVALID"}.</div>')
    if policy.get("rationale"):
        out.append(f'<div class="kv">{_esc(policy["rationale"])}</div>')
    out.append(f'<pre><code>{_esc(policy["cedar_text"])}</code></pre>')
    out.append(_decision_tests_html(policy, goal))
    out.append("</div>")
    return "".join(out)


def _after_html(replay: dict | None) -> str:
    if replay is None:
        return '<div class="muted">not rerun yet</div>'
    outcome = replay["outcome"]
    pill = _OUTCOME_PILL.get(outcome, "")
    parts = [f'<div><span class="pill {pill}">{_esc(outcome)}</span> '
             f'<span class="muted">after {_esc(replay.get("tries"))} try/tries</span></div>']
    ev = replay.get("evidence") or {}
    if ev.get("note"):
        parts.append(f'<div class="kv" style="margin-top:8px">{_esc(ev["note"])}</div>')
    if ev.get("tool_call"):
        parts.append(_tool_call_html(ev["tool_call"]))
    return "".join(parts)


def _findings_html(run, findings, policies_by_finding, reruns_by_finding, goals) -> str:
    if not findings:
        return '<div class="finding"><div class="muted">No breaches were found in this run.</div></div>'
    # Goals that share a fallback share their fix: one Cedar policy covers both the
    # direct attack and the indirect-injection path (§3.4, e.g. G1 and G3).
    fallback_group: dict[str, list[str]] = {}
    for gid, g in goals.items():
        fallback_group.setdefault(g.fallback, []).append(gid)
    finding_goal_ids = {f["goal_id"] for f in findings}

    cards = []
    for f in findings:
        goal = goals.get(f["goal_id"])
        policy = _display_policy(policies_by_finding[f["id"]])
        replay = _latest_replay(reruns_by_finding[f["id"]])
        desc = (goal.description.strip() if goal else "")
        shared = sorted(
            x for x in fallback_group.get(goal.fallback, []) if goal and x != f["goal_id"] and x in finding_goal_ids
        ) if goal else []
        shared_note = (
            f'<div class="shared">Same Cedar policy as {_esc(", ".join(shared))} — '
            f'one authorization rule covers both the direct and the indirect-injection path (§3.4).</div>'
            if shared else ""
        )
        top = (f'<div class="top"><h3>{_esc(f["goal_id"])} '
               f'<span class="muted" style="font-weight:400">· {_esc(f["winning_strategy"])}</span></h3>'
               f'<span class="pill sev">{_esc(f["severity"])}</span></div>')
        meta = (f'<div class="kv" style="margin:6px 0 10px">'
                f'turns to breach: <b>{_esc(f["turns_to_breach"])}</b></div>')
        ba = (f'<div class="ba">'
              f'<div class="panel before"><div class="label">Before — breach</div>'
              f'{_evidence_html(f.get("evidence_tool_call"))}</div>'
              f'<div class="panel after"><div class="label">After — Cedar enforced</div>'
              f'{_after_html(replay)}</div></div>')
        cards.append(f'<div class="finding">{top}'
                     + (f'<div class="desc">{_esc(desc)}</div>' if desc else "")
                     + meta + ba + shared_note + _policy_html(policy, goal) + "</div>")
    return "".join(cards)


def _happy_html(happy: dict | None) -> str:
    if happy is None:
        return '<div class="banner bad">The happy path was not run.</div>'
    banner = ('<div class="banner good">Happy path OK — every legitimate action is still allowed.</div>'
              if happy["ok"] else
              '<div class="banner bad">Happy path FAILED — a fix broke legitimate use.</div>')
    steps = happy.get("steps") or []
    if not steps:
        return banner
    items = []
    for s in steps:
        ok = s.get("decision") == "allow" and s.get("executed")
        status = '<span class="ok">allowed</span>' if ok else \
                 f'<span class="no">{_esc(s.get("detail") or s.get("decision"))}</span>'
        items.append(f'<li><span>alice can {_esc(s.get("name"))}</span>{status}</li>')
    return banner + '<ul class="steps" style="margin-top:12px">' + "".join(items) + "</ul>"


def _sayonly_html(run, attempts, findings) -> str:
    # D2: every demo goal is a tool-abuse / data-exfiltration goal, so Cedar is always
    # the fix and there are no "say-only" (bad text, no tool abuse) failures to list.
    return ('<div class="finding"><div class="muted">None. Every goal in this run is a '
            'tool-abuse or data-exfiltration goal, so each breach is fixed by a Cedar policy '
            'rather than a guardrail recommendation (D2).</div></div>')


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_run(store: Store, run_id: int, *, goals: dict | None = None) -> str:
    """Render the full HTML report for `run_id` as a string (plan §8)."""
    run = store.get_run(run_id)
    if run is None:
        raise ValueError(f"no run {run_id}")
    goals = goals if goals is not None else load_goals()
    findings = store.findings(run_id)
    attempts = store.attempts(run_id)
    happy = store.happy_path(run_id)
    policies_by_finding = {f["id"]: store.policies(f["id"]) for f in findings}
    reruns_by_finding = {f["id"]: store.reruns(f["id"]) for f in findings}

    html_doc = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "<!-- META -->": _meta_html(run),
        "<!-- SUMMARY -->": _summary_html(run, findings, policies_by_finding, reruns_by_finding, happy),
        "<!-- FINDINGS -->": _findings_html(run, findings, policies_by_finding, reruns_by_finding, goals),
        "<!-- HAPPY -->": _happy_html(happy),
        "<!-- SAYONLY -->": _sayonly_html(run, attempts, findings),
    }
    for sentinel, content in replacements.items():
        html_doc = html_doc.replace(sentinel, content)
    return html_doc


def write_report(store: Store, run_id: int, *, out_dir: Path | None = None, goals: dict | None = None) -> Path:
    """Write the report to `runs/<run_id>/report.html` (or `out_dir`) and return the path."""
    html_doc = render_run(store, run_id, goals=goals)
    base = out_dir if out_dir is not None else (get_settings().runs_dir / str(run_id))
    base.mkdir(parents=True, exist_ok=True)
    path = base / "report.html"
    path.write_text(html_doc, encoding="utf-8")
    return path
