"""llm.py without a real LLM: the fake provider, plus local stand-in HTTP servers for Ollama."""

import http.server
import json
import socket
import threading
import time

import pytest
from pydantic import BaseModel

from siege import llm
from siege.config import get_settings
from siege.llm import (
    FakeProvider,
    LLMRequest,
    LLMResult,
    LLMUnavailableError,
    Message,
    OllamaProvider,
    ToolCall,
    ToolSpec,
    llm_call,
    override_provider,
)


class Label(BaseModel):
    kind: str


def ask(role="target", **kwargs):
    return llm_call(role, "system prompt", [Message.user("hi")], **kwargs)


# --- llm_call with the fake provider --------------------------------------------


def test_plain_text_uses_configured_model():
    fake = FakeProvider(["hello"])
    with override_provider("target", fake):
        r = ask()
    assert r.ok and r.text == "hello" and r.model == "qwen2.5:14b"
    assert r.latency_s >= 0


def test_requests_are_recorded():
    tool = ToolSpec("get_order", "Look up an order.", {"type": "object", "properties": {}})
    fake = FakeProvider(["ok"])
    with override_provider("target", fake):
        llm_call("target", "sys", [Message.user("status?")], tools=[tool])
    (req,) = fake.requests
    assert req.system == "sys" and req.messages == (Message.user("status?"),)
    assert req.tools == (tool,) and req.config.role == "target" and req.schema is None


def test_model_override_reaches_request_and_result():
    fake = FakeProvider(["x"])
    with override_provider("cedar", fake):
        r = ask("cedar", model="qwen2.5:7b")
    assert fake.requests[0].model == "qwen2.5:7b" and r.model == "qwen2.5:7b"


def test_schema_is_validated_in_llm_call():
    with override_provider("labeller", FakeProvider(['{"kind": "refusal"}', Label(kind="x")])):
        from_text = ask("labeller", schema=Label)
        from_model = ask("labeller", schema=Label)
    assert from_text.ok and from_text.parsed == Label(kind="refusal")
    assert from_model.parsed == Label(kind="x")


def test_schema_mismatch_sets_error_without_raising():
    with override_provider("labeller", FakeProvider(["not json", '{"other": 1}'])):
        bad_json = ask("labeller", schema=Label)
        wrong_shape = ask("labeller", schema=Label)
    for r in (bad_json, wrong_shape):
        assert not r.ok and r.parsed is None and r.error.startswith("schema validation failed")


def test_refusal_is_a_result_not_an_exception():
    with override_provider("attacker", FakeProvider([{"refused": True}])):
        r = ask("attacker", schema=Label)
    assert r.refused and not r.ok and r.parsed is None and r.error is None


def test_scripted_tool_calls_and_callable_script():
    call = ToolCall("c0", "issue_refund", {"order_id": "1001", "amount_cents": 2000})
    fake = FakeProvider(lambda req: {"tool_calls": [call]} if req.tools else "no tools")
    tool = ToolSpec("issue_refund", "Refund.", {"type": "object", "properties": {}})
    with override_provider("target", fake):
        with_tools = ask(tools=[tool])
        without = ask()
    assert with_tools.tool_calls == [call] and without.text == "no tools"


def test_scripted_dict_is_not_mutated():
    shared = {"text": "t", "model": "m1"}
    with override_provider("target", FakeProvider(lambda req: shared)):
        first, second = ask(), ask()
    assert first.model == second.model == "m1"
    assert shared == {"text": "t", "model": "m1"}


def test_scripted_llmresult_gets_model_filled_in():
    with override_provider("target", FakeProvider([LLMResult(model="", text="t")])):
        assert ask().model == "qwen2.5:14b"


def test_exhausted_script_raises():
    with override_provider("target", FakeProvider(["one"])):
        ask()
        with pytest.raises(AssertionError, match="exhausted"):
            ask()


def test_override_is_scoped_and_nestable():
    outer, inner = FakeProvider(["outer"]), FakeProvider(["inner"])
    with override_provider("target", outer):
        with override_provider("target", inner):
            assert ask().text == "inner"
        assert ask().text == "outer"
    assert "target" not in llm._provider_overrides


def test_fake_provider_without_override_raises(monkeypatch):
    monkeypatch.setenv("SIEGE_TARGET_PROVIDER", "fake")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="override_provider"):
        ask()


@pytest.mark.parametrize("provider", ["anthropic", "openai"])
def test_cloud_providers_not_implemented_and_not_imported(monkeypatch, provider):
    import sys

    monkeypatch.setenv("SIEGE_TARGET_PROVIDER", provider)
    get_settings.cache_clear()
    with pytest.raises(NotImplementedError):
        ask()
    assert "anthropic" not in sys.modules and "openai" not in sys.modules


# --- Message round trip into the Ollama wire format -----------------------------


def test_tool_call_round_trip_wire_format():
    call = ToolCall("call_0", "get_order", {"order_id": "1001"})
    history = [
        Message.user("status?"),
        LLMResult(model="m", tool_calls=[call]).as_message(),
        Message.tool_result(call, '{"status": "shipped"}'),
    ]
    wire = [OllamaProvider._message_to_ollama(m) for m in history]
    assert wire == [
        {"role": "user", "content": "status?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "get_order", "arguments": {"order_id": "1001"}}}]},
        {"role": "tool", "content": '{"status": "shipped"}', "tool_name": "get_order"},
    ]


# --- OllamaProvider against local stand-in servers ------------------------------


def serve(handler_body):
    """Start a local HTTP server; `handler_body(handler)` handles each POST. Returns its URL."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))  # consume the request
            handler_body(self)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}", server


@pytest.fixture
def ollama_returning():
    servers = []

    def start(payload, status=200):
        def reply(h):
            body = json.dumps(payload).encode()
            h.send_response(status)
            h.send_header("Content-Type", "application/json")
            h.end_headers()
            h.wfile.write(body)

        url, server = serve(reply)
        servers.append(server)
        return OllamaProvider(url)

    yield start
    for s in servers:
        s.shutdown()


def request(schema=None, tools=()):
    return LLMRequest(get_settings().target, "qwen2.5:7b", "sys", (Message.user("x"),), schema, tuple(tools))


def test_parses_text_and_tool_calls(ollama_returning):
    provider = ollama_returning({"message": {"role": "assistant", "content": "checking", "tool_calls": [
        {"id": "abc", "function": {"name": "get_order", "arguments": {"order_id": "1001"}}},
        {"id": None, "function": {"name": "read_inbox", "arguments": "{}"}},
    ]}})
    r = provider.complete(request())
    assert r.text == "checking" and r.ok
    assert r.tool_calls == [ToolCall("abc", "get_order", {"order_id": "1001"}),
                            ToolCall("call_1", "read_inbox", {})]


@pytest.mark.parametrize("arguments, expected", [("{not json", "invalid tool arguments"), ("[1]", "must be an object")])
def test_malformed_tool_arguments_are_a_model_error(ollama_returning, arguments, expected):
    provider = ollama_returning({"message": {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "get_order", "arguments": arguments}}]}})
    r = provider.complete(request())
    assert not r.ok and expected in r.error and r.tool_calls == []


def test_request_payload_shape():
    seen = {}

    class Capture(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"message": {"role": "assistant", "content": "{}"}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    tool = ToolSpec("get_order", "Look up.", {"type": "object", "properties": {"order_id": {"type": "string"}}})
    OllamaProvider(f"http://127.0.0.1:{server.server_address[1]}").complete(request(schema=Label, tools=[tool]))
    server.shutdown()

    assert seen["model"] == "qwen2.5:7b" and seen["stream"] is False
    assert seen["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["options"]["temperature"] == 0.0 and seen["options"]["num_predict"] == 16000
    assert seen["format"] == Label.model_json_schema()
    assert seen["tools"] == [{"type": "function", "function": {
        "name": "get_order", "description": "Look up.",
        "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}}}}]


def test_temperature_omitted_when_unset(monkeypatch):
    seen = {}

    class Capture(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            body = json.dumps({"message": {"role": "assistant", "content": "ok"}}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Capture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    attacker_cfg = get_settings().attacker  # temperature None by default
    OllamaProvider(f"http://127.0.0.1:{server.server_address[1]}").complete(
        LLMRequest(attacker_cfg, "qwen2.5:7b", "sys", (Message.user("x"),)))
    server.shutdown()
    assert "temperature" not in seen["options"] and "format" not in seen and "tools" not in seen


# --- Transport failures raise LLMUnavailableError --------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connection_refused():
    with pytest.raises(LLMUnavailableError, match="Could not connect"):
        OllamaProvider(f"http://127.0.0.1:{free_port()}").complete(request())


def test_http_error_includes_detail(ollama_returning):
    provider = ollama_returning({"error": "model 'x' not found"}, status=404)
    with pytest.raises(LLMUnavailableError, match="HTTP 404.*not found"):
        provider.complete(request())


def test_error_field_in_200_response(ollama_returning):
    with pytest.raises(LLMUnavailableError, match="returned an error: out of memory"):
        ollama_returning({"error": "out of memory"}).complete(request())


def test_missing_message(ollama_returning):
    with pytest.raises(LLMUnavailableError, match="has no message"):
        ollama_returning({"done": True}).complete(request())


def test_read_timeout(monkeypatch):
    url, server = serve(lambda h: time.sleep(2))
    real_urlopen = llm.urlopen
    monkeypatch.setattr(llm, "urlopen", lambda req, timeout: real_urlopen(req, timeout=0.5))
    with pytest.raises(LLMUnavailableError, match="failed mid-request"):
        OllamaProvider(url).complete(request())
    server.shutdown()


def test_transport_errors_are_runtime_errors():
    assert issubclass(LLMUnavailableError, RuntimeError)
