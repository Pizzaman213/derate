"""Pure OpenAI Chat Completions / Anthropic Messages translation.

Request validation raises ``ValueError`` naming the unsupported/malformed field;
ProviderService turns that into HTTP 400. No credentials or subscription identity
prefixes belong here. The response seam matches ResponsesToChat: ``error`` is
(status, code, message), and ``feed`` returns SSE only for streaming clients.
Call ``finish`` at transport EOF in both modes: it detects a truncated stream,
returns remaining error SSE for streaming clients, or the buffered JSON body.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

ERROR_STATUS = {
    "invalid_request_error": 400,
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "request_too_large_error": 413,
    "rate_limit_error": 429,
    "overloaded_error": 503,
    "api_error": 502,
}
UNKNOWN_ERROR_STATUS = 502
MAX_EVENT_BYTES = 5 * 1024 * 1024
_DONE = b"data: [DONE]\n\n"
_ROUTING_FIELDS = frozenset({"models", "route", "fallbacks", "provider"})
_REQUEST_FIELDS = frozenset({
    "model", "messages", "stream", "stream_options", "tools", "tool_choice",
    "parallel_tool_calls", "max_tokens", "max_completion_tokens", "temperature",
    "top_p", "stop", "reasoning_effort", "n", "response_format", "user",
}) | _ROUTING_FIELDS


def _bad(field: str, detail: str) -> ValueError:
    return ValueError(f"{field}: {detail}")


def _only_fields(value: dict, allowed: set[str] | frozenset[str], field: str) -> None:
    for key in value:
        if key not in allowed:
            raise _bad(f"{field}.{key}" if field else str(key), "is not supported by Messages providers")


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise _bad(field, "must be a nonempty string")
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _json(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_constant)


def _image(value: Any, field: str) -> dict:
    if not isinstance(value, dict):
        raise _bad(field, "must contain a url")
    _only_fields(value, {"url", "detail"}, field)
    if value.get("detail", "auto") != "auto":
        raise _bad(f"{field}.detail", "only auto is supported")
    url = _name(value.get("url"), f"{field}.url")
    if url.startswith("data:"):
        header, sep, data = url.partition(",")
        media_type = header[5:].removesuffix(";base64")
        if not sep or not header.endswith(";base64") or media_type not in {
            "image/jpeg", "image/png", "image/gif", "image/webp",
        }:
            raise _bad(field, "requires a supported base64 image data URI")
        try:
            decoded = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error):
            raise _bad(field, "contains invalid image base64") from None
        if not decoded:
            raise _bad(field, "contains empty image base64")
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in ("http", "https") and bool(parsed.hostname)
    except ValueError:
        valid = False
    if not valid:
        raise _bad(field, "requires an http(s) URL or supported image data URI")
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content(value: Any, field: str, *, images: bool = False, nullable: bool = False) -> list[dict]:
    if value is None and nullable:
        return []
    if isinstance(value, str):
        return [{"type": "text", "text": value}]
    if not isinstance(value, list):
        raise _bad(field, "must be text or a list of content parts")
    result = []
    for index, part in enumerate(value):
        path = f"{field}[{index}]"
        if not isinstance(part, dict):
            raise _bad(path, "must be a content-part object")
        kind = part.get("type")
        if kind == "text":
            _only_fields(part, {"type", "text"}, path)
            if not isinstance(part.get("text"), str):
                raise _bad(f"{path}.text", "must be a string")
            result.append({"type": "text", "text": part["text"]})
        elif kind == "image_url" and images:
            _only_fields(part, {"type", "image_url"}, path)
            result.append(_image(part.get("image_url"), f"{path}.image_url"))
        else:
            raise _bad(f"{path}.type", f"content part {kind!r} is not supported")
    return result


def chat_to_messages(body: dict, upstream_id: str) -> tuple[dict, dict[str, str]]:
    """Translate a request without mutating it; return native body and name map.

    Compatibility tool names are deterministic, collision-free, and reversible:
    declarations precede historical calls in first-occurrence order. The map is
    upstream name -> exact original spelling, including case.
    """
    _only_fields(body, _REQUEST_FIELDS, "")
    if "n" in body and (type(body["n"]) is not int or body["n"] != 1):
        raise _bad("n", "only one choice is supported")
    if "response_format" in body and body["response_format"] != {"type": "text"}:
        raise _bad("response_format", "only the default/text response format is supported")
    if "stream_options" in body:
        options = body["stream_options"]
        if not isinstance(options, dict):
            raise _bad("stream_options", "must be an object")
        _only_fields(options, {"include_usage"}, "stream_options")
    out: dict[str, Any] = {"model": upstream_id, "stream": True}
    if "max_tokens" in body and "max_completion_tokens" in body and body["max_tokens"] != body["max_completion_tokens"]:
        raise _bad("max_completion_tokens", "conflicts with max_tokens")
    out["max_tokens"] = body.get("max_completion_tokens", body.get("max_tokens", 4096))
    for field in ("temperature", "top_p"):
        if field in body:
            out[field] = body[field]
    if "stop" in body:
        stop = body["stop"]
        if isinstance(stop, str):
            stop = [stop]
        if not isinstance(stop, list) or not all(isinstance(item, str) for item in stop):
            raise _bad("stop", "must be a string or list of strings")
        out["stop_sequences"] = list(stop)
    if "reasoning_effort" in body:
        effort = body["reasoning_effort"]
        if effort == "none":
            out["thinking"] = {"type": "disabled"}
        elif isinstance(effort, str) and effort in {"low", "medium", "high", "xhigh", "max"}:
            out["thinking"] = {"type": "adaptive"}
            out["output_config"] = {"effort": effort}
        else:
            raise _bad("reasoning_effort", "must be none, low, medium, high, xhigh or max; minimal is not supported")

    originals: dict[str, str] = {}
    names: dict[str, str] = {}

    def mapped(value: Any, field: str) -> str:
        original = _name(value, field)
        if original not in originals:
            native = f"mcp_Tool_{len(originals)}"
            originals[original] = native
            names[native] = original
        return originals[original]

    tools = body.get("tools", [])
    if not isinstance(tools, list):
        raise _bad("tools", "must be a list")
    definitions = []
    declared: set[str] = set()
    for index, tool in enumerate(tools):
        path = f"tools[{index}]"
        if not isinstance(tool, dict) or tool.get("type") != "function":
            raise _bad(path, "only function tools are supported")
        _only_fields(tool, {"type", "function"}, path)
        fn = tool.get("function")
        if not isinstance(fn, dict):
            raise _bad(f"{path}.function", "must be an object")
        _only_fields(fn, {"name", "description", "parameters", "strict"}, f"{path}.function")
        if fn.get("strict") not in (None, False):
            raise _bad(f"{path}.function.strict", "strict schemas are not supported")
        name = mapped(fn.get("name"), f"{path}.function.name")
        if name in declared:
            raise _bad(f"{path}.function.name", "duplicate tool definition")
        declared.add(name)
        schema = fn.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(schema, dict):
            raise _bad(f"{path}.function.parameters", "must be a JSON schema object")
        definition = {"name": name, "input_schema": schema}
        if "description" in fn:
            if not isinstance(fn["description"], str):
                raise _bad(f"{path}.function.description", "must be a string")
            definition["description"] = fn["description"]
        definitions.append(definition)

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _bad("messages", "must be a nonempty list")
    native_messages: list[dict] = []
    system: list[dict] = []
    pending: set[str] = set()
    call_ids: set[str] = set()
    for index, message in enumerate(messages):
        path = f"messages[{index}]"
        if not isinstance(message, dict):
            raise _bad(path, "must be an object")
        role = message.get("role")
        if role not in ("system", "developer", "user", "assistant", "tool"):
            raise _bad(f"{path}.role", f"unknown role {role!r}")
        allowed = {"role", "content"}
        if role == "assistant":
            allowed.add("tool_calls")
        elif role == "tool":
            allowed.add("tool_call_id")
        _only_fields(message, allowed, path)
        if pending and role != "tool":
            raise _bad(f"{path}.role", "tool_calls require paired tool replies before the next message")
        if role in ("system", "developer"):
            system.extend(_content(message.get("content"), f"{path}.content"))
            continue
        if role == "tool":
            call_id = _name(message.get("tool_call_id"), f"{path}.tool_call_id")
            if call_id not in pending:
                raise _bad(f"{path}.tool_call_id", "has no unmatched preceding tool call")
            pending.remove(call_id)
            blocks = [{"type": "tool_result", "tool_use_id": call_id,
                       "content": _content(message.get("content"), f"{path}.content")}]
            native_role = "user"
        else:
            calls = message.get("tool_calls", [])
            if not isinstance(calls, list):
                raise _bad(f"{path}.tool_calls", "must be a list")
            blocks = _content(message.get("content"), f"{path}.content", images=role == "user", nullable=role == "assistant" and bool(calls))
            native_role = role
            for call_index, call in enumerate(calls):
                call_path = f"{path}.tool_calls[{call_index}]"
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise _bad(call_path, "only function tool calls are supported")
                _only_fields(call, {"id", "type", "function"}, call_path)
                call_id = _name(call.get("id"), f"{call_path}.id")
                if call_id in call_ids:
                    raise _bad(f"{call_path}.id", "duplicate tool call ID")
                call_ids.add(call_id)
                fn = call.get("function")
                if not isinstance(fn, dict):
                    raise _bad(f"{call_path}.function", "must be an object")
                _only_fields(fn, {"name", "arguments"}, f"{call_path}.function")
                arguments = fn.get("arguments")
                try:
                    args = _json(arguments) if isinstance(arguments, str) else None
                except ValueError:
                    args = None
                if not isinstance(args, dict):
                    raise _bad(f"{call_path}.function.arguments", "must encode a JSON object")
                blocks.append({"type": "tool_use", "id": call_id,
                               "name": mapped(fn.get("name"), f"{call_path}.function.name"), "input": args})
                pending.add(call_id)
        if native_messages and native_messages[-1]["role"] == native_role:
            native_messages[-1]["content"].extend(blocks)
        else:
            native_messages.append({"role": native_role, "content": blocks})
    if pending:
        raise _bad("messages.tool_calls", "require paired tool replies")
    if not native_messages:
        raise _bad("messages", "must include a user or assistant message")
    out["messages"] = native_messages
    if system:
        out["system"] = system

    choice = body.get("tool_choice", "auto")
    native_choice: dict[str, Any]
    if isinstance(choice, str) and choice in {"auto", "required", "none"}:
        native_choice = {"type": "any" if choice == "required" else "auto"}
    elif isinstance(choice, dict) and choice.get("type") == "function" and isinstance(choice.get("function"), dict):
        _only_fields(choice, {"type", "function"}, "tool_choice")
        _only_fields(choice["function"], {"name"}, "tool_choice.function")
        original = choice["function"].get("name")
        if not isinstance(original, str) or original not in originals or originals[original] not in declared:
            raise _bad("tool_choice.function.name", "must name an offered function")
        native_choice = {"type": "tool", "name": originals[original]}
    else:
        raise _bad("tool_choice", "must be auto, required, none or a named function")
    if "parallel_tool_calls" in body and type(body["parallel_tool_calls"]) is not bool:
        raise _bad("parallel_tool_calls", "must be a boolean")
    if definitions and choice != "none":
        out["tools"] = definitions
        if body.get("parallel_tool_calls") is False:
            native_choice["disable_parallel_tool_use"] = True
        out["tool_choice"] = native_choice
    elif choice == "required":
        raise _bad("tool_choice", "required needs offered tools")
    return out, names


def _frame(value: Any) -> bytes:
    return b"data: " + json.dumps(value, separators=(",", ":")).encode() + b"\n\n"


class MessagesToChat:
    """Incremental native SSE conversion, with explicit failure at premature EOF."""

    def __init__(self, *, stream: bool, model: str, now: float,
                 tool_names: dict[str, str], scrub: Callable[[str], str]) -> None:
        self._stream = stream
        self._model = model
        self._created = int(now)
        self._tool_names = tool_names
        self._scrub = scrub
        self._buf = bytearray()
        self._data: list[str] = []
        self._event = ""
        self._event_bytes = 0
        self._id = ""
        self._started = False
        self._blocks: dict[int, dict[str, Any]] = {}
        self._tools: list[dict] = []
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._usage: dict[str, int] = {}
        self._stop_reason: str | None = None
        self._finish_reason: str | None = None
        self._error: tuple[int, str, str] | None = None
        self._finished = False
        self._emitted = False

    @property
    def error(self) -> tuple[int, str, str] | None:
        return self._error

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def emitted(self) -> bool:
        return self._emitted

    def _failure(self, code: str, message: str, status: int = 502) -> list[bytes]:
        if self._finished:
            return []
        code, message = self._scrub(code), self._scrub(message)
        self._error = (status, code, message)
        self._finished = True
        self._buf.clear()
        self._data.clear()
        return [_frame({"error": {"message": message, "type": "upstream_error", "code": code}}), _DONE]

    def _protocol_error(self, message: str) -> list[bytes]:
        return self._failure("invalid_upstream_stream", f"Anthropic Messages stream: {message}")

    def feed(self, chunk: bytes) -> list[bytes]:
        if self._finished:
            return []
        self._buf.extend(chunk)
        out: list[bytes] = []
        offset = 0
        while not self._finished:
            nl = self._buf.find(b"\n", offset)
            if nl < 0:
                break
            raw = bytes(self._buf[offset:nl])
            self._event_bytes += nl + 1 - offset
            offset = nl + 1
            if self._event_bytes > MAX_EVENT_BYTES:
                out.extend(self._protocol_error("event exceeds 5 MiB"))
                break
            try:
                line = raw.removesuffix(b"\r").decode("utf-8")
            except UnicodeDecodeError:
                out.extend(self._protocol_error("invalid UTF-8 in event"))
                break
            if not line:
                out.extend(self._dispatch())
                self._event_bytes = 0
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    self._event = value
                elif field == "data":
                    self._data.append(value)
        if not self._finished:
            del self._buf[:offset]
            if self._event_bytes + len(self._buf) > MAX_EVENT_BYTES:
                out.extend(self._protocol_error("event exceeds 5 MiB"))
        if self._stream and out:
            self._emitted = True
            return out
        return []

    def _dispatch(self) -> list[bytes]:
        data, event = "\n".join(self._data), self._event
        self._data, self._event = [], ""
        if not data:
            return []
        try:
            payload = _json(data)
        except ValueError:
            return self._protocol_error("malformed JSON event")
        if not isinstance(payload, dict):
            return self._protocol_error("event data must be an object")
        kind = payload.get("type", event)
        if not isinstance(kind, str) or not kind:
            return self._protocol_error("event type must be a nonempty string")
        try:
            return self._handle(kind, payload)
        except (ValueError, TypeError, KeyError):
            return self._protocol_error(f"malformed {kind} event")

    def _envelope(self) -> dict:
        return {"id": "chatcmpl-" + self._id, "object": "chat.completion.chunk",
                "created": self._created, "model": self._model}

    def _chunk(self, delta: dict, finish_reason: str | None = None) -> bytes:
        return _frame({**self._envelope(), "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason},
        ]})

    def _record_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            raise ValueError("usage")
        for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            if field in usage:
                value = usage[field]
                if type(value) is not int or value < 0:
                    raise ValueError("usage tokens")
                self._usage[field] = value

    def _chat_usage(self) -> dict:
        usage: dict[str, Any] = {}
        # Cache fields are optional on uncached requests; missing base input or
        # output counts remain unknown, rather than being fabricated as zero.
        if "input_tokens" in self._usage:
            usage["prompt_tokens"] = sum(self._usage.get(field, 0) for field in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
            ))
        if "cache_read_input_tokens" in self._usage:
            usage["prompt_tokens_details"] = {"cached_tokens": self._usage["cache_read_input_tokens"]}
        if "output_tokens" in self._usage:
            usage["completion_tokens"] = self._usage["output_tokens"]
        if "prompt_tokens" in usage and "completion_tokens" in usage:
            usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        return usage

    def _text(self, kind: str, text: Any) -> list[bytes]:
        if not isinstance(text, str):
            raise ValueError("text delta")
        if not text:
            return []
        target = self._content if kind == "text" else self._reasoning
        target.append(text)
        return [self._chunk({"content" if kind == "text" else "reasoning": text})]

    def _block_index(self, payload: dict) -> int:
        index = payload.get("index")
        if type(index) is not int or index < 0:
            raise ValueError("content block index")
        return index

    def _handle(self, kind: str, payload: dict) -> list[bytes]:
        if kind == "error":
            err = payload.get("error")
            if not isinstance(err, dict):
                raise ValueError("error")
            code = str(err.get("type") or "api_error")
            message = str(err.get("message") or "the Anthropic Messages response failed")
            return self._failure(code, message, ERROR_STATUS.get(code, UNKNOWN_ERROR_STATUS))
        if kind == "message_start":
            message = payload.get("message")
            if self._started or not isinstance(message, dict):
                raise ValueError("message_start")
            self._id = _name(message.get("id"), "message.id")
            self._record_usage(message.get("usage", {}))
            self._started = True
            return [self._chunk({"role": "assistant"})]
        if kind not in {"content_block_start", "content_block_delta", "content_block_stop", "message_delta", "message_stop"}:
            return []  # ping and additive event types do not change the result.
        if not self._started:
            raise ValueError("event before message_start")
        if kind == "content_block_start":
            index = self._block_index(payload)
            if index in self._blocks:
                raise ValueError("duplicate block")
            block = payload.get("content_block")
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ValueError("content_block")
            state: dict[str, Any] = {"type": block["type"], "closed": False}
            self._blocks[index] = state
            if block["type"] in ("text", "thinking"):
                return self._text(block["type"], block.get("text" if block["type"] == "text" else "thinking", ""))
            if block["type"] == "tool_use":
                tool_index = len(self._tools)
                state["tool_index"] = tool_index
                name = _name(block.get("name"), "tool_use.name")
                if name not in self._tool_names:
                    raise ValueError("unrecognized tool name")
                initial = block.get("input", {})
                if not isinstance(initial, dict):
                    raise ValueError("tool input")
                # The native protocol opens streamed tool inputs with {}. A
                # nonempty initial object is complete and cannot take deltas.
                args = json.dumps(initial, separators=(",", ":")) if initial else ""
                state["initial_input"] = bool(initial)
                tool = {"id": _name(block.get("id"), "tool_use.id"), "type": "function",
                        "function": {"name": self._tool_names[name], "arguments": args}}
                self._tools.append(tool)
                return [self._chunk({"tool_calls": [{"index": tool_index, **tool}]})]
            return []
        if kind in ("content_block_delta", "content_block_stop"):
            index = self._block_index(payload)
            state = self._blocks.get(index)
            if state is None or state["closed"]:
                raise ValueError("unknown or closed block")
            if kind == "content_block_stop":
                state["closed"] = True
                if state["type"] == "tool_use":
                    tool_index = state["tool_index"]
                    args = self._tools[tool_index]["function"]["arguments"]
                    if not args:
                        self._tools[tool_index]["function"]["arguments"] = "{}"
                        return [self._chunk({"tool_calls": [{"index": tool_index, "function": {"arguments": "{}"}}]})]
                return []
            delta = payload.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("delta")
            delta_kind = delta.get("type")
            if not isinstance(delta_kind, str) or not delta_kind:
                raise ValueError("delta type")
            if delta_kind in ("text_delta", "thinking_delta"):
                expected = "text" if delta_kind == "text_delta" else "thinking"
                if state["type"] != expected:
                    raise ValueError("delta/block mismatch")
                return self._text(expected, delta.get("text" if expected == "text" else "thinking"))
            if delta_kind == "input_json_delta":
                if state["type"] != "tool_use" or state["initial_input"]:
                    raise ValueError("tool delta/block mismatch")
                text = delta.get("partial_json")
                if not isinstance(text, str):
                    raise ValueError("partial_json")
                tool_index = state["tool_index"]
                self._tools[tool_index]["function"]["arguments"] += text
                return [self._chunk({"tool_calls": [{"index": tool_index, "function": {"arguments": text}}]})] if text else []
            return []  # signatures and other additive delta fields are opaque.
        if kind == "message_delta":
            delta = payload.get("delta")
            if not isinstance(delta, dict):
                raise ValueError("message delta")
            if delta.get("stop_reason") is not None:
                self._stop_reason = delta["stop_reason"]
            if "usage" in payload:
                self._record_usage(payload["usage"])
            return []
        reasons = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
                   "tool_use": "tool_calls", "refusal": "content_filter"}
        if not isinstance(self._stop_reason, str) or self._stop_reason not in reasons:
            return self._failure("unsupported_stop_reason", f"Anthropic Messages returned unsupported stop_reason {self._stop_reason!r}")
        if any(not state["closed"] for state in self._blocks.values()):
            raise ValueError("message stopped with open content blocks")
        # A token limit may legitimately interrupt a tool's JSON mid-object.
        # Validate only once the terminal reason distinguishes truncation from
        # a completed call; preserve the exact partial arguments on length.
        if self._stop_reason != "max_tokens":
            for tool in self._tools:
                if not isinstance(_json(tool["function"]["arguments"]), dict):
                    raise ValueError("tool arguments must be an object")
        self._finish_reason = reasons[self._stop_reason]
        self._finished = True
        frames = [self._chunk({}, self._finish_reason)]
        usage = self._chat_usage()
        if usage:
            frames.append(_frame({**self._envelope(), "choices": [], "usage": usage}))
        return [*frames, _DONE]

    def finish(self) -> bytes:
        """Finalize transport EOF, returning buffered JSON or remaining error SSE."""
        frames = []
        if not self._finished:
            frames = self._protocol_error("ended before message_stop (truncated response)")
        if self._stream:
            if frames:
                self._emitted = True
            return b"".join(frames)
        self._emitted = True
        if self._error:
            _, code, message = self._error
            return json.dumps({"error": {"message": message, "type": "upstream_error", "code": code}}, separators=(",", ":")).encode()
        message: dict[str, Any] = {"role": "assistant", "content": "".join(self._content) if self._content or not self._tools else None}
        if self._reasoning:
            message["reasoning"] = "".join(self._reasoning)
        if self._tools:
            message["tool_calls"] = self._tools
        result = {**self._envelope(), "object": "chat.completion", "choices": [
            {"index": 0, "message": message, "finish_reason": self._finish_reason},
        ]}
        usage = self._chat_usage()
        if usage:
            result["usage"] = usage
        return json.dumps(result, separators=(",", ":")).encode()
