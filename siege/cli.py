"""The `siege` command line (plan §9, P3.4).

For now: `siege run` runs goals against the target and stores the result, and
`siege show` prints a stored run. `fix`, `rerun`, `report` and `demo` arrive in
Phases 4-5.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from typing import Sequence

from siege.config import get_settings
from siege.orchestrator.goals import load_goals
from siege.orchestrator.loop import SessionResult, TurnEvent, run_session
from siege.orchestrator.store import Store
from siege.orchestrator.target_client import TargetClient, launched_target


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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
