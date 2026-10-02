"""scripts/chat.py end to end: CLI -> HTTP -> target, with a scripted fake LLM (P1.6)."""

import httpx
import pytest
from fastapi.testclient import TestClient

from siege.llm import FakeProvider, LLMResult, ToolCall, override_provider
from siege.scripts import chat
from siege.target.app import create_app

CANARY = "CANARY-0123456789abcdef"


@pytest.fixture
def client():
    # TestClient drives the ASGI app synchronously and exposes the .post/.get/.close
    # that chat.py uses on its httpx.Client; good enough to run the CLI end to end.
    with TestClient(create_app(canary=CANARY)) as c:
        yield c


def says(text):
    return LLMResult(model="", text=text)


def calls_tool(name, **arguments):
    return LLMResult(model="", tool_calls=[ToolCall(id=f"c_{name}", name=name, arguments=arguments)])


def test_one_shot_message_shows_tool_call_and_reply(client, capsys):
    script = [calls_tool("issue_refund", order_id="5521", amount_cents=19999), says("Refunded order 5521.")]
    with override_provider("target", FakeProvider(script)):
        code = chat.main(["--user", "alice", "-m", "refund order 5521"], client=client)
    assert code == 0
    out = capsys.readouterr().out
    assert "session" in out and "as alice" in out
    assert "issue_refund(order_id='5521', amount_cents=19999)" in out
    assert "executed" in out
    assert "ShopBot> Refunded order 5521." in out


def test_several_messages_run_in_order(client, capsys):
    script = [calls_tool("get_order", order_id="5518"), says("one"), says("two")]
    with override_provider("target", FakeProvider(script)):
        code = chat.main(["--user", "alice", "-m", "look up 5518", "-m", "thanks"], client=client)
    assert code == 0
    out = capsys.readouterr().out
    assert out.index("ShopBot> one") < out.index("ShopBot> two")


def test_connection_error_prints_a_hint(capsys):
    # No server behind this transport: every request fails to connect.
    transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")))
    with httpx.Client(transport=transport, base_url="http://target") as dead:
        code = chat.main(["--user", "alice", "-m", "hi"], client=dead)
    assert code == 1
    assert "Could not reach the target" in capsys.readouterr().err


def test_build_parser_defaults_come_from_config():
    args = chat.build_parser().parse_args([])
    assert args.user == "alice"
    assert args.url == "http://127.0.0.1:8100"
    assert args.enforce is False and args.message is None
