from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel

from siege.config import RoleConfig, get_settings


class LLMUnavailableError(RuntimeError):
    """The LLM backend could not be reached or answered with a non-model error.

    Raised for infrastructure faults only (connection, HTTP, timeout, broken
    response). Problems with the model's own output go in `LLMResult.error`.
    """


# ---------------------------------------------------------------------------
# Neutral message / tool types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class Message:
    role: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @staticmethod
    def user(content: str) -> "Message":
        return Message(role="user", content=content)

    @staticmethod
    def tool_result(call: ToolCall, content: str) -> "Message":
        return Message(
            role="tool",
            content=content,
            tool_call_id=call.id,
            name=call.name,
        )


@dataclass
class LLMResult:
    model: str
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    parsed: BaseModel | None = None
    refused: bool = False
    error: str | None = None
    latency_s: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.refused and self.error is None

    def as_message(self) -> Message:
        return Message(
            role="assistant",
            content=self.text,
            tool_calls=tuple(self.tool_calls),
        )


# ---------------------------------------------------------------------------
# Internal request type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LLMRequest:
    config: RoleConfig
    model: str
    system: str
    messages: tuple[Message, ...]
    schema: type[BaseModel] | None = None
    tools: tuple[ToolSpec, ...] = ()


FakeResponse = (
    str
    | BaseModel
    | dict[str, Any]
    | LLMResult
)

FakeScript = (
    list[FakeResponse]
    | Callable[[LLMRequest], FakeResponse]
)


# ---------------------------------------------------------------------------
# Fake provider
# ---------------------------------------------------------------------------

class FakeProvider:
    def __init__(self, script: FakeScript):
        self.script = script
        self.requests: list[LLMRequest] = []
        self._index = 0

    def complete(self, request: LLMRequest) -> LLMResult:
        self.requests.append(request)

        if callable(self.script):
            response = self.script(request)
        else:
            if self._index >= len(self.script):
                raise AssertionError("fake script exhausted")

            response = self.script[self._index]
            self._index += 1

        if isinstance(response, LLMResult):
            if not response.model:
                response.model = request.model
            return response

        if isinstance(response, BaseModel):
            return LLMResult(
                model=request.model,
                text=response.model_dump_json(),
            )

        if isinstance(response, str):
            return LLMResult(
                model=request.model,
                text=response,
            )

        if isinstance(response, dict):
            fields = dict(response)  # never mutate the test's scripted dict
            return LLMResult(
                model=fields.pop("model", request.model),
                **fields,
            )

        raise TypeError(
            f"Unsupported fake response type: {type(response).__name__}"
        )


# ---------------------------------------------------------------------------
# Ollama provider
# ---------------------------------------------------------------------------

class OllamaProvider:
    def __init__(self, host: str):
        self.host = host.rstrip("/")

    def complete(self, request: LLMRequest) -> LLMResult:
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": request.system,
            }
        ]

        for message in request.messages:
            messages.append(self._message_to_ollama(message))

        cfg = request.config

        payload: dict[str, Any] = {
            "model": request.model,
            "stream": False,
            "messages": messages,
            "options": {
                "num_predict": cfg.max_tokens,
            },
        }

        if cfg.temperature is not None:
            payload["options"]["temperature"] = cfg.temperature

        if request.schema is not None:
            payload["format"] = request.schema.model_json_schema()

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                    },
                }
                for tool in request.tools
            ]

        body = json.dumps(payload).encode("utf-8")

        http_request = Request(
            f"{self.host}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urlopen(http_request, timeout=600) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise LLMUnavailableError(
                f"Ollama HTTP {exc.code}: {detail}"
            ) from exc
        except URLError as exc:
            raise LLMUnavailableError(
                f"Could not connect to Ollama at {self.host}: {exc.reason}"
            ) from exc
        except (TimeoutError, OSError) as exc:
            # Raised directly (not as URLError) when the read times out or the
            # connection drops mid-response, e.g. while a large model loads.
            raise LLMUnavailableError(
                f"Ollama at {self.host} failed mid-request: {exc}"
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise LLMUnavailableError(
                f"Ollama returned invalid JSON: {exc}"
            ) from exc

        if not isinstance(data, dict) or "error" in data:
            detail = data.get("error") if isinstance(data, dict) else data
            raise LLMUnavailableError(f"Ollama returned an error: {detail}")

        message = data.get("message")

        if not isinstance(message, dict):
            raise LLMUnavailableError(
                f"Ollama response has no message: {raw[:200]}"
            )

        text = message.get("content", "") or ""

        tool_calls: list[ToolCall] = []

        for index, raw_call in enumerate(message.get("tool_calls") or []):
            function = raw_call.get("function", {})
            name = function.get("name", "")

            arguments = function.get("arguments", {})

            # Malformed arguments are a model mistake, not an infrastructure
            # fault: report them in the result instead of raising.
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    return LLMResult(
                        model=request.model,
                        text=text,
                        error=f"invalid tool arguments for {name!r}: {arguments[:200]}",
                    )

            if not isinstance(arguments, dict):
                return LLMResult(
                    model=request.model,
                    text=text,
                    error=(
                        f"tool arguments for {name!r} must be an object, "
                        f"got {type(arguments).__name__}"
                    ),
                )

            tool_calls.append(
                ToolCall(
                    id=raw_call.get("id") or f"call_{index}",
                    name=name,
                    arguments=arguments,
                )
            )

        return LLMResult(
            model=request.model,
            text=text,
            tool_calls=tool_calls,
            refused=False,
        )

    @staticmethod
    def _message_to_ollama(message: Message) -> dict[str, Any]:
        result: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }

        if message.role == "assistant" and message.tool_calls:
            result["tool_calls"] = [
                {
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                }
                for call in message.tool_calls
            ]

        if message.role == "tool":
            result["tool_name"] = message.name

        return result


# ---------------------------------------------------------------------------
# Provider overrides for tests
# ---------------------------------------------------------------------------

_provider_overrides: dict[str, Any] = {}


@contextmanager
def override_provider(role: str, provider: Any) -> Iterator[Any]:
    previous = _provider_overrides.get(role)

    _provider_overrides[role] = provider

    try:
        yield provider
    finally:
        if previous is None:
            _provider_overrides.pop(role, None)
        else:
            _provider_overrides[role] = previous


# ---------------------------------------------------------------------------
# Provider selection
# ---------------------------------------------------------------------------

def _get_provider(role: str, config: RoleConfig) -> Any:
    if role in _provider_overrides:
        return _provider_overrides[role]

    if config.provider == "ollama":
        settings = get_settings()
        return OllamaProvider(settings.ollama_host)

    if config.provider == "fake":
        raise RuntimeError(
            f"Provider 'fake' requires override_provider('{role}', ...)"
        )

    if config.provider in {"anthropic", "openai"}:
        raise NotImplementedError(
            f"Cloud provider '{config.provider}' is not implemented yet"
        )

    raise ValueError(f"Unknown LLM provider: {config.provider}")


# ---------------------------------------------------------------------------
# Public LLM call
# ---------------------------------------------------------------------------

def llm_call(
    role: str,
    system: str,
    messages: Sequence[Message],
    schema: type[BaseModel] | None = None,
    tools: Sequence[ToolSpec] | None = None,
    model: str | None = None,
) -> LLMResult:

    settings = get_settings()
    config = settings.role(role)

    selected_model = model or config.require_model()

    request = LLMRequest(
        config=config,
        model=selected_model,
        system=system,
        messages=tuple(messages),
        schema=schema,
        tools=tuple(tools or ()),
    )

    provider = _get_provider(role, config)

    started = time.perf_counter()

    result = provider.complete(request)

    result.latency_s = time.perf_counter() - started

    if result.model == "":
        result.model = selected_model

    if schema is not None and result.error is None and not result.refused:
        try:
            result.parsed = schema.model_validate_json(result.text)
        except Exception as exc:
            result.error = f"schema validation failed: {exc}"
            result.parsed = None

    return result