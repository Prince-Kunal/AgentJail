"""The `siege` command line (plan §9, P3.4, P4.6, P5.2, P8.1).

`siege run` attacks the goals and stores the result; `siege show` prints a stored
run; `siege fix <run_id>` generates a Cedar fix per finding (§6.4-6.5); `siege
rerun <run_id>` replays each finding with the PEP on and runs the happy path
(§6.7); `siege report <run_id>` writes the static HTML report (§8); `siege verify
[dir]` deterministically re-checks a recorded run with no LLM -- the judge path
(§8); and `siege demo` does run + fix + rerun + report in one go against a target.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from typing import Sequence

from siege.config import get_settings
from siege.orchestrator import cedar_gen, rerun, verify
from siege.orchestrator.goals import load_goals
from siege.orchestrator.loop import SessionResult, TurnEvent, run_session
from siege.orchestrator.store import Store
from siege.orchestrator.target_client import TargetClient, launched_target
from siege.report.render import write_report


def _trunc(text: str | None, n: int = 160) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _new_canary() -> str:
    # The orchestrator owns the canary (run config, §3.3); it never imports siege.target.
    return f"CANARY-{secrets.token_hex(8)}"


def print_turn_event(event: TurnEvent) -> None:
    if event.kind == "attacker_refused":
        print(f"  turn {event.turn}: [attacker refused]")
        return
    if event.kind == "rejected":
        print(f"  turn {event.turn}: [rejected] {event.reason}")
        return
    print(f"  turn {event.turn}: strategy={event.strategy}")
    print(f"    > {_trunc(event.message)}")
    print(f"    < {_trunc(event.reply)}")
    for call in event.tool_calls or []:
        flag = "executed" if call.get("executed") else "not executed"
        print(f"      tool: {call.get('tool')} [{call.get('decision')}, {flag}]")
    tag = "  *** BREACH ***" if event.breach else ""
    print(f"    refusal_type={event.refusal_type}  breach={event.breach}{tag}")


def print_policy(finding: dict, result) -> None:
    where = result.source + (f" by {result.model}" if result.model else "")
    print(f"  {finding['goal_id']}: {where} — {len(result.attempts)} attempt(s), valid={result.valid}")
    if result.rationale:
        print(f"    rationale: {_trunc(result.rationale, 140)}")


def print_replay(r) -> None:
    print(f"  {r.goal_id}: {r.outcome} (after {r.tries} try/tries)")


def _run_goals(goal_ids: Sequence[str], run, target, store, max_turns) -> list[SessionResult]:
    goals = load_goals()
    results = []
    for gid in goal_ids:
        print(f"\n=== {gid}: {goals[gid].description.strip().splitlines()[0]}")
        result = run_session(goals[gid], run, target, store, max_turns=max_turns, on_event=print_turn_event)
        verdict = f"BREACH in {result.turns_used} turn(s)" if result.breached else "no breach"
        print(f"  -> {verdict}")
        results.append(result)
    return results


def cmd_run(args: argparse.Namespace) -> int:
    settings = get_settings()
    goals = load_goals()
    goal_ids = args.goal or list(goals)
    unknown = [g for g in goal_ids if g not in goals]
    if unknown:
        print(f"unknown goal(s): {', '.join(unknown)}; known: {', '.join(goals)}", file=sys.stderr)
        return 2
    max_turns = args.max_turns if args.max_turns is not None else settings.max_turns

    if args.no_launch:
        canary = os.environ.get("SIEGE_CANARY") or _new_canary()
        if not os.environ.get("SIEGE_CANARY"):
            print("note: --no-launch without SIEGE_CANARY set; G2 canary leaks won't be detected", file=sys.stderr)
    else:
        canary = _new_canary()

    # Record the model the target will actually run, including a --target-model override.
    target_model = args.target_model or settings.role("target").model
    config = {
        "models": {
            "attacker": settings.role("attacker").model,
            "target": target_model,
            "labeller": settings.role("labeller").model,
        },
        "max_turns": max_turns,
        "canary": canary,
        "attacker_user": settings.attacker_user,
        "goals": goal_ids,
        "seed": os.environ.get("SIEGE_SEED"),
    }

    store = Store()
    run = store.create_run(config)
    print(f"run {run.id} (canary {canary[:13]}..., target_model {config['models']['target']})")
    try:
        if args.no_launch:
            with TargetClient() as target:
                results = _run_goals(goal_ids, run, target, store, max_turns)
        else:
            with launched_target(canary, model=args.target_model) as target:
                results = _run_goals(goal_ids, run, target, store, max_turns)
    finally:
        store.close()

    breached = sum(r.breached for r in results)
    print(f"\nrun {run.id}: {breached}/{len(results)} goal(s) breached. See `siege show {run.id}`.")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = Store()
    try:
        run = store.get_run(args.run_id)
        if run is None:
            print(f"no run {args.run_id}", file=sys.stderr)
            return 1
        print(f"run {run.id}  started {run.started_at}")
        print(f"config: {run.config}")
        print("\nattempts:")
        for a in store.attempts(run.id):
            extra = f" strategy={a['strategy']}" if a["strategy"] else ""
            print(f"  turn {a['turn']} {a['goal_id']} [{a['status']}]{extra} "
                  f"breach={bool(a['breach'])} refusal={a['refusal_type']}")
            if a["message"]:
                print(f"    > {_trunc(a['message'])}")
            if a["reply"]:
                print(f"    < {_trunc(a['reply'])}")
        findings = store.findings(run.id)
        print(f"\nfindings ({len(findings)}):")
        for f in findings:
            print(f"  {f['goal_id']} [{f['severity']}] turns_to_breach={f['turns_to_breach']} "
                  f"strategy={f['winning_strategy']}")
            evidence = f["evidence_tool_call"] or {}
            if evidence.get("description"):
                print(f"    evidence: {evidence['description']}")
    finally:
        store.close()
    return 0


def cmd_fix(args: argparse.Namespace) -> int:
    store = Store()
    try:
        run = store.get_run(args.run_id)
        if run is None:
            print(f"no run {args.run_id}", file=sys.stderr)
            return 1
        findings = store.findings(run.id)
        if not findings:
            print(f"run {run.id} has no findings to fix.")
            return 0
        print(f"run {run.id}: generating Cedar fixes for {len(findings)} finding(s) "
              f"(cascade: {', '.join(get_settings().cedar_models)})")
        done = cedar_gen.fix_run(run, store, on_policy=print_policy)
        skipped = len(findings) - len(done)
        if skipped:
            print(f"  ({skipped} finding(s) already had a valid policy; skipped)")
        generated = sum(1 for _f, r in done if r.source == "generated")
        print(f"\nrun {run.id}: {generated} generated, {len(done) - generated} fell back. "
              f"See `siege rerun {run.id}`.")
    finally:
        store.close()
    return 0


def cmd_rerun(args: argparse.Namespace) -> int:
    store = Store()
    try:
        run = store.get_run(args.run_id)
        if run is None:
            print(f"no run {args.run_id}", file=sys.stderr)
            return 1
        findings = store.findings(run.id)
        if not findings:
            print(f"run {run.id} has no findings to rerun.")
            return 0
        target_model = (run.config.get("models") or {}).get("target")
        print(f"run {run.id}: replaying {len(findings)} finding(s) with the PEP ON"
              + (f" (target {target_model})" if target_model else ""))
        if args.no_launch:
            with TargetClient() as target:
                result = rerun.rerun(run, store, target, on_replay=print_replay)
        else:
            with launched_target(run.canary or _new_canary(), model=target_model) as target:
                result = rerun.rerun(run, store, target, on_replay=print_replay)
        print("\n" + result.summary())
    finally:
        store.close()
    return 0 if result.ok else 1


def cmd_demo(args: argparse.Namespace) -> int:
    settings = get_settings()
    goals = load_goals()
    goal_ids = args.goal or list(goals)
    unknown = [g for g in goal_ids if g not in goals]
    if unknown:
        print(f"unknown goal(s): {', '.join(unknown)}; known: {', '.join(goals)}", file=sys.stderr)
        return 2
    max_turns = args.max_turns if args.max_turns is not None else settings.max_turns
    # With --no-launch the target is already running (e.g. the Docker sandbox, §7);
    # it was seeded with SIEGE_CANARY, so the orchestrator must use the same one.
    if args.no_launch:
        canary = os.environ.get("SIEGE_CANARY") or _new_canary()
        if not os.environ.get("SIEGE_CANARY"):
            print("note: --no-launch without SIEGE_CANARY set; G2 canary leaks won't be detected", file=sys.stderr)
    else:
        canary = _new_canary()
    target_model = args.target_model or settings.role("target").model
    config = {
        "models": {
            "attacker": settings.role("attacker").model,
            "target": target_model,
            "labeller": settings.role("labeller").model,
        },
        "max_turns": max_turns,
        "canary": canary,
        "attacker_user": settings.attacker_user,
        "goals": goal_ids,
        "seed": os.environ.get("SIEGE_SEED"),
    }
    store = Store()
    run = store.create_run(config)
    where = "external target (--no-launch)" if args.no_launch else "launched target"
    print(f"demo run {run.id} (canary {canary[:13]}..., target_model {target_model}, {where})")
    target_cm = TargetClient() if args.no_launch else launched_target(canary, model=args.target_model)
    try:
        with target_cm as target:
            print("\n== ATTACK (PEP off) ==")
            results = _run_goals(goal_ids, run, target, store, max_turns)
            if not any(r.breached for r in results):
                print("\nno breaches; nothing to fix or rerun.")
                return 0
            print("\n== FIX (generate Cedar policies) ==")
            cedar_gen.fix_run(run, store, on_policy=print_policy)
            print("\n== RERUN (PEP on) ==")
            rr = rerun.rerun(run, store, target, on_replay=print_replay)
            print("\n" + rr.summary())
            report_path = write_report(store, run.id)
            breached = sum(r.breached for r in results)
            c = rr.outcomes()
            print(f"\ndemo run {run.id}: {breached} breach(es) → {c['BLOCKED']} blocked, "
                  f"{c['NOT_REPRODUCED']} not reproduced, {c['STILL_BREACHED']} still breached; "
                  f"happy path {'OK' if rr.happy_path and rr.happy_path.ok else 'FAILED'}.")
            print(f"report: {report_path}")
            return 0 if rr.ok else 1
    finally:
        store.close()


def cmd_verify(args: argparse.Namespace) -> int:
    try:
        data = verify.load_demo(args.path)
    except FileNotFoundError:
        print(f"no recorded run at {args.path!r} (expected {args.path}/run.json)", file=sys.stderr)
        return 1
    result = verify.verify_run(data, out_dir=args.path)
    print(result.summary())
    print(f"report: {result.report_path}")
    return 0 if result.ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    store = Store()
    try:
        run = store.get_run(args.run_id)
        if run is None:
            print(f"no run {args.run_id}", file=sys.stderr)
            return 1
        path = write_report(store, run.id)
    finally:
        store.close()
    print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="siege", description="Adaptive AI red team for AI agents.")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="attack goals against the target and store the results")
    run.add_argument("--goal", action="append", help="goal id (repeatable); default: all goals")
    run.add_argument("--max-turns", type=int, default=None, help="override MAX_TURNS")
    run.add_argument("--target-model", default=None, help="model for the launched target (e.g. qwen2.5:7b)")
    run.add_argument("--no-launch", action="store_true", help="use an already-running target instead of launching one")
    run.set_defaults(func=cmd_run)

    show = sub.add_parser("show", help="print a stored run")
    show.add_argument("run_id", type=int)
    show.set_defaults(func=cmd_show)

    fix = sub.add_parser("fix", help="generate and store a Cedar fix for each finding of a run")
    fix.add_argument("run_id", type=int)
    fix.set_defaults(func=cmd_fix)

    rerun_p = sub.add_parser("rerun", help="replay each finding with the PEP on and run the happy path")
    rerun_p.add_argument("run_id", type=int)
    rerun_p.add_argument("--no-launch", action="store_true",
                         help="use an already-running target instead of launching one")
    rerun_p.set_defaults(func=cmd_rerun)

    report_p = sub.add_parser("report", help="write runs/<run_id>/report.html from a stored run")
    report_p.add_argument("run_id", type=int)
    report_p.set_defaults(func=cmd_report)

    verify_p = sub.add_parser("verify", help="deterministically verify a recorded run with no LLM (the judge path)")
    verify_p.add_argument("path", nargs="?", default="demo", help="dir holding run.json (default: demo)")
    verify_p.set_defaults(func=cmd_verify)

    demo = sub.add_parser("demo", help="run, fix, rerun and report in one go against a single target")
    demo.add_argument("--goal", action="append", help="goal id (repeatable); default: all goals")
    demo.add_argument("--max-turns", type=int, default=None, help="override MAX_TURNS")
    demo.add_argument("--target-model", default=None, help="model for the launched target (e.g. qwen2.5:7b)")
    demo.add_argument("--no-launch", action="store_true",
                      help="use an already-running target (e.g. the Docker sandbox) instead of launching one")
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
