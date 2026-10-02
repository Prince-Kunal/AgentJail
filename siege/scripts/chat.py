"""Talk to the target by hand as a chosen user (plan §1, P1.6).

    python -m siege.scripts.chat --user alice
    python -m siege.scripts.chat --user alice -m "refund order 5521"

Start the target first:  uvicorn siege.target.app:app --port 8100

The script reaches the target only over HTTP (§9.1), exactly as the orchestrator
will. It opens a session bound to --user (the principal, D3), then sends each
message and prints every tool call the agent made before the reply. In the REPL,
`/log` dumps the tool log and `/quit` exits.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from siege.config import get_settings

TIMEOUT = 600  # a cold 14b can take a few seconds on the first turn


def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="siege-chat", description="Talk to the Siege target by hand.")
    parser.add_argument("--user", default=settings.attacker_user, help="the session principal (default: %(default)s)")
    parser.add_argument("--url", default=settings.target_url, help="target base URL (default: %(default)s)")
    parser.add_argument("--enforce", action="store_true", help="turn the PEP on for this session (P4.2+)")
    parser.add_argument(
        "-m", "--message", action="append", metavar="TEXT",
        help="send this message and exit; repeat for several turns (default: interactive)",
    )
    return parser


def _format_call(call: dict[str, Any]) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in call["args"].items())
    flag = "executed" if call["executed"] else "not executed"
    line = f"  -> {call['tool']}({args})  [{call['decision']}, {flag}]"
    if call.get("result_summary"):
        line += f"  {call['result_summary']}"
    return line


def _open_session(client: httpx.Client, user: str, enforce: bool) -> str:
    r = client.post("/session", json={"user_id": user, "enforce": enforce})
    r.raise_for_status()
    return r.json()["session_id"]


def _send(client: httpx.Client, session_id: str, message: str) -> None:
    r = client.post("/chat", json={"session_id": session_id, "message": message})
    if r.status_code != 200:
        print(f"  [error {r.status_code}] {_detail(r)}")
        return
    body = r.json()
    for call in body["tool_calls"]:
        print(_format_call(call))
    print(f"ShopBot> {body['reply']}")


def _dump_log(client: httpx.Client, session_id: str) -> None:
    rows = client.get(f"/session/{session_id}/tool_log").json()["tool_log"]
    if not rows:
        print("  (tool log is empty)")
        return
    for row in rows:
        print(
            f"  turn {row['turn']}: {row['tool']} args={row['args']} "
            f"principal={row['principal']} decision={row['decision']} "
            f"executed={row['executed']} resource_attrs={row['resource_attrs']}"
        )


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text


def _repl(client: httpx.Client, session_id: str, user: str) -> None:
    print("Type a message, /log to dump the tool log, or /quit to exit.")
    while True:
        try:
            message = input(f"{user}> ").strip()
        except EOFError:
            print()
            break
        if not message:
            continue
        if message in ("/quit", "/exit"):
            break
        if message == "/log":
            _dump_log(client, session_id)
            continue
        _send(client, session_id, message)


def main(argv: list[str] | None = None, client: httpx.Client | None = None) -> int:
    args = build_parser().parse_args(argv)
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=args.url, timeout=TIMEOUT)
    try:
        try:
            session_id = _open_session(client, args.user, args.enforce)
        except httpx.ConnectError:
            print(
                f"Could not reach the target at {args.url}. Start it with:\n"
                f"  uvicorn siege.target.app:app --port 8100",
                file=sys.stderr,
            )
            return 1
        except httpx.HTTPStatusError as exc:
            print(f"Could not open a session: {_detail(exc.response)}", file=sys.stderr)
            return 1
        print(f"session {session_id} as {args.user} (enforce={args.enforce})")
        if args.message:
            for message in args.message:
                print(f"{args.user}> {message}")
                _send(client, session_id, message)
        else:
            _repl(client, session_id, args.user)
    finally:
        if own_client:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
