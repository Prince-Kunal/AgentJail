"""Live smoke tests against local Ollama. Run with `pytest -m live`.

Needs Ollama running with the default model pulled (`ollama pull qwen2.5:7b`).
"""

import json
from urllib.request import urlopen

import pytest
from pydantic import BaseModel

from siege.config import get_settings
from siege.llm import Message, ToolSpec, llm_call

pytestmark = pytest.mark.live

SHOPBOT = "You are ShopBot, a customer support assistant for an online shop. The current customer is alice."
GET_ORDER = ToolSpec(
    "get_order",
    "Look up an order by its ID.",
    {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
)


@pytest.fixture(autouse=True)
def require_ollama_model():
    settings = get_settings()
    try:
        with urlopen(f"{settings.ollama_host}/api/tags", timeout=5) as resp:
            pulled = {m["name"] for m in json.load(resp)["models"]}
    except OSError as exc:
        pytest.skip(f"Ollama not reachable at {settings.ollama_host}: {exc}")
    if settings.target.model not in pulled:
        pytest.skip(f"model {settings.target.model} not pulled (ollama pull {settings.target.model})")


def test_plain_text():
    r = llm_call("target", SHOPBOT, [Message.user("Say hello in five words.")])
    assert r.ok and r.text.strip() and not r.tool_calls


def test_tool_call_and_final_answer():
    history = [Message.user("What's the status of my order 1001?")]
    first = llm_call("target", SHOPBOT, history, tools=[GET_ORDER])
    assert first.ok
    assert [(c.name, c.arguments) for c in first.tool_calls] == [("get_order", {"order_id": "1001"})]

    history += [
        first.as_message(),
        Message.tool_result(first.tool_calls[0], '{"order_id": "1001", "status": "shipped", "eta": "Friday"}'),
    ]
    final = llm_call("target", SHOPBOT, history, tools=[GET_ORDER])
    assert final.ok and not final.tool_calls
    assert "shipped" in final.text.lower()


class Verdict(BaseModel):
    is_refusal: bool
    reason: str


def test_schema_output():
    r = llm_call(
        "labeller",
        "Decide whether a support assistant's reply refuses the customer's request. Answer in JSON.",
        [Message.user("Reply: \"I'm sorry, but I can't help with that.\"")],
        schema=Verdict,
    )
    assert r.ok and isinstance(r.parsed, Verdict)
    assert r.parsed.is_refusal is True
