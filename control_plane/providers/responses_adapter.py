"""Chat Completions in, OpenAI Responses out, and back again.

A ChatGPT plan provider only answers ``POST /v1/responses``, streamed, with
``store: false``; derate's clients speak ``/v1/chat/completions``. This module
is the whole translation and nothing else: no I/O, no credentials, no runtime
notes. :func:`chat_to_responses` builds the upstream body and
:class:`ResponsesToChat` turns the upstream SSE back into chat chunks (or one
``chat.completion`` when the client did not stream).

What the Responses side cannot express is refused with a ``ValueError`` naming
the field rather than dropped, because a silently ignored ``n`` or content part
is an answer to a different question. Sampling knobs the plan endpoint rejects
(``temperature``, ``top_p``, ``max_tokens``...) are dropped: they change how an
answer is phrased, not what was asked.
"""

from __future__ import annotations

import json
from typing import Any

# The plan endpoint's own refusal codes and the status each one means. They
# arrive either as an HTTP error body or mid-stream as ``response.failed``;
# the second has no status of its own, so this table supplies it.
ERROR_STATUS: dict[str, int] = {
    "subscription_sharing_usage_limit_exceeded": 429,
    "subscription_sharing_usage_unavailable": 503,
    "subscription_sharing_user_unavailable": 503,
    "subscription_sharing_unsupported_capability": 400,
    "subscription_sharing_user_not_eligible": 403,
    "subscription_sharing_invalid_user": 401,
}
UNKNOWN_ERROR_STATUS = 502

_UNSUPPORTED = "is not supported by ChatGPT plan providers"


def _text_of(content: Any) -> str:
    """The text of a system/assistant/tool content: a string or text parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and part.get("type") in ("text", "input_text", "output_text"):
                out.append(str(part.get("text") or ""))
            else:
                kind = part.get("type") if isinstance(part, dict) else type(part).__name__
                raise ValueError(f"content part {kind!r} {_UNSUPPORTED}")
        return "".join(out)
    raise ValueError(f"content of type {type(content).__name__!r} {_UNSUPPORTED}")


def _user_content(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "input_text", "text": content}]
    if not isinstance(content, list):
        raise ValueError(f"content of type {type(content).__name__!r} {_UNSUPPORTED}")
    parts: list[dict[str, Any]] = []
    for part in content:
        kind = part.get("type") if isinstance(part, dict) else type(part).__name__
        if kind == "text":
            parts.append({"type": "input_text", "text": str(part.get("text") or "")})
        elif kind == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, dict) else image
            if not isinstance(url, str) or not url:
                raise ValueError("image_url content part has no url")
            parts.append({"type": "input_image", "image_url": url})
        else:
            raise ValueError(f"content part {kind!r} {_UNSUPPORTED}")
    return parts


def _tool_output(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(p, dict) and p.get("type") == "text" for p in content
    ):
        return "".join(str(p.get("text") or "") for p in content)
    return json.dumps(content)


def chat_to_responses(body: dict, upstream_id: str) -> dict:
    """The ``/v1/responses`` body for a ``/v1/chat/completions`` body."""
    n = body.get("n")
    if isinstance(n, int) and n > 1:
        raise ValueError(f"n > 1 {_UNSUPPORTED}")

    instructions: list[str] = []
    items: list[dict[str, Any]] = []
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            raise ValueError("every message must be an object")
        role = msg.get("role")
        content = msg.get("content")
        if role in ("system", "developer"):
            text = _text_of(content)
            if text:
                instructions.append(text)
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": _user_content(content)})
        elif role == "assistant":
            text = _text_of(content)
            if text:
                items.append({
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                })
            calls = msg.get("tool_calls") or []
            if not isinstance(calls, list):
                raise ValueError("tool_calls must be a list of objects")
            for call in calls:
                if not isinstance(call, dict):
                    raise ValueError("every tool call must be an object")
                fn = call.get("function") or {}
                if not isinstance(fn, dict):
                    raise ValueError("a tool call's function must be an object")
                items.append({
                    "type": "function_call",
                    "call_id": call.get("id"),
                    "name": fn.get("name"),
                    "arguments": fn.get("arguments") or "",
                })
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": msg.get("tool_call_id"),
                "output": _tool_output(content),
            })
        else:
            raise ValueError(f"message role {role!r} {_UNSUPPORTED}")

    out: dict[str, Any] = {"model": upstream_id}
    if instructions:
        out["instructions"] = "\n\n".join(instructions)
    out["input"] = items

    tools = body.get("tools")
    if tools:
        flat = []
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                kind = tool.get("type") if isinstance(tool, dict) else type(tool).__name__
                raise ValueError(f"tool type {kind!r} {_UNSUPPORTED}")
            fn = tool.get("function") or {}
            if not isinstance(fn, dict):
                raise ValueError("a tool's function must be an object")
            entry: dict[str, Any] = {"type": "function", "name": fn.get("name")}
            if "description" in fn:
                entry["description"] = fn["description"]
            if "parameters" in fn:
                entry["parameters"] = fn["parameters"]
            flat.append(entry)
        out["tools"] = flat

    choice = body.get("tool_choice")
    if isinstance(choice, str):
        out["tool_choice"] = choice
    elif isinstance(choice, dict):
        if choice.get("type") == "function" and isinstance(choice.get("function"), dict):
            out["tool_choice"] = {"type": "function", "name": choice["function"].get("name")}
        else:
            out["tool_choice"] = choice

    effort = body.get("reasoning_effort")
    if effort is not None:
        reasoning: dict[str, Any] = {"effort": effort}
        # A summary is the only reasoning text the Responses API hands out, and
        # only when asked; without it a reasoning model thinks in silence.
        # Nothing to summarise when reasoning was switched off.
        if effort != "none":
            reasoning["summary"] = "auto"
        out["reasoning"] = reasoning

    fmt = body.get("response_format")
    if isinstance(fmt, dict):
        if fmt.get("type") == "json_schema":
            spec = fmt.get("json_schema") or {}
            schema_fmt: dict[str, Any] = {"type": "json_schema", "name": spec.get("name")}
            if "schema" in spec:
                schema_fmt["schema"] = spec["schema"]
            if "strict" in spec:
                schema_fmt["strict"] = spec["strict"]
            out["text"] = {"format": schema_fmt}
        elif fmt.get("type") == "json_object":
            out["text"] = {"format": {"type": "json_object"}}

    out["store"] = False
    out["stream"] = True
    return out


def _frame(obj: Any) -> bytes:
    return b"data: " + json.dumps(obj, separators=(",", ":")).encode() + b"\n\n"


_DONE = b"data: [DONE]\n\n"


class ResponsesToChat:
    """Upstream Responses SSE in, chat-completions bytes out.

    ``feed`` takes raw upstream bytes in whatever pieces the transport hands
    over and returns the SSE frames to send on (always empty when the client
    did not stream). ``finish`` is the non-stream body. ``error`` is set once
    the upstream reported a failure, as (status, code, message); ``finished``
    once it reached a terminal event, so a caller can tell a stream that was
    cut off from one that ended.
    """

    def __init__(self, stream: bool, model: str, now: float) -> None:
        self._stream = stream
        self._model = model
        self._created = int(now)
        self._buf = b""
        self._data: list[str] = []
        self._event = ""
        self._response_id = ""
        self._role_sent = False
        self._content: list[str] = []
        self._reasoning: list[str] = []
        self._tools: list[dict[str, Any]] = []
        self._tool_by_item: dict[str, int] = {}
        self._tool_by_output: dict[int, int] = {}
        self._finish_reason: str | None = None
        self._usage: dict[str, Any] | None = None
        self._error: tuple[int, str, str] | None = None
        self._finished = False
        self._emitted = False

    @property
    def error(self) -> tuple[int, str, str] | None:
        return self._error

    @property
    def emitted(self) -> bool:
        return self._emitted

    @property
    def finished(self) -> bool:
        return self._finished

    # -- SSE parsing -------------------------------------------------------

    def feed(self, chunk: bytes) -> list[bytes]:
        out: list[bytes] = []
        self._buf += chunk
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            raw = self._buf[:nl]
            self._buf = self._buf[nl + 1:]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            line = raw.decode("utf-8", errors="replace")
            if line == "":
                out.extend(self._dispatch())
            elif line.startswith(":"):
                continue
            else:
                field, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
                if field == "data":
                    self._data.append(value)
                elif field == "event":
                    self._event = value
        if out and self._stream:
            self._emitted = True
        return out if self._stream else []

    def _dispatch(self) -> list[bytes]:
        data, event = "\n".join(self._data), self._event
        self._data, self._event = [], ""
        if not data or data == "[DONE]" or self._finished:
            return []
        try:
            payload = json.loads(data)
        except ValueError:
            return []
        if not isinstance(payload, dict):
            return []
        return self._handle(str(payload.get("type") or event), payload)

    # -- event mapping -----------------------------------------------------

    def _chunk(self, delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
        return _frame({
            "id": "chatcmpl-" + self._response_id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        })

    def _role(self) -> list[bytes]:
        if self._role_sent:
            return []
        self._role_sent = True
        return [self._chunk({"role": "assistant"})]

    def _tool_index(self, payload: dict[str, Any]) -> int | None:
        item_id = payload.get("item_id")
        if isinstance(item_id, str) and item_id in self._tool_by_item:
            return self._tool_by_item[item_id]
        out_idx = payload.get("output_index")
        if isinstance(out_idx, int):
            return self._tool_by_output.get(out_idx)
        return None

    def _handle(self, kind: str, p: dict[str, Any]) -> list[bytes]:
        if kind in ("response.created", "response.in_progress"):
            resp = p.get("response") or {}
            if not self._response_id and resp.get("id"):
                self._response_id = str(resp["id"])
            # The role chunk goes out now, with the headers, as every other
            # kind's first frame does: the proxy times TTFT off the first
            # frame, and holding it for the first text would count the
            # model's reasoning phase as this kind's alone.
            return self._role() if kind == "response.created" else []

        if kind == "response.output_text.delta":
            text = p.get("delta") or ""
            if not text:
                return []
            self._content.append(text)
            return [*self._role(), self._chunk({"content": text})]

        if kind == "response.reasoning_summary_text.delta":
            # Out as `delta.reasoning`, the field vLLM and OpenRouter stream
            # their reasoning on, so a chat client reads one shape whatever
            # served it.
            text = p.get("delta") or ""
            if not text:
                return []
            self._reasoning.append(text)
            return [*self._role(), self._chunk({"reasoning": text})]

        if kind == "response.reasoning_summary_part.added":
            # Each summary part is a paragraph; the parts arrive with no
            # separator of their own.
            if not self._reasoning:
                return []
            self._reasoning.append("\n\n")
            return [self._chunk({"reasoning": "\n\n"})]

        if kind == "response.output_item.added":
            item = p.get("item") or {}
            if item.get("type") != "function_call":
                return []
            index = len(self._tools)
            self._tools.append({
                "id": item.get("call_id"),
                "type": "function",
                "function": {"name": item.get("name"), "arguments": ""},
            })
            if item.get("id"):
                self._tool_by_item[str(item["id"])] = index
            if isinstance(p.get("output_index"), int):
                self._tool_by_output[p["output_index"]] = index
            return [*self._role(), self._chunk({"tool_calls": [{
                "index": index,
                "id": item.get("call_id"),
                "type": "function",
                "function": {"name": item.get("name"), "arguments": ""},
            }]})]

        if kind == "response.function_call_arguments.delta":
            index = self._tool_index(p)
            text = p.get("delta") or ""
            if index is None or not text:
                return []
            self._tools[index]["function"]["arguments"] += text
            return [*self._role(), self._chunk({"tool_calls": [{
                "index": index, "function": {"arguments": text},
            }]})]

        if kind == "response.output_item.done":
            # An upstream that sends the arguments whole rather than as deltas.
            item = p.get("item") or {}
            if item.get("type") != "function_call":
                return []
            index = self._tool_index({"item_id": item.get("id"), "output_index": p.get("output_index")})
            args = item.get("arguments") or ""
            if index is None or not args or self._tools[index]["function"]["arguments"]:
                return []
            self._tools[index]["function"]["arguments"] = args
            return [self._chunk({"tool_calls": [{"index": index, "function": {"arguments": args}}]})]

        if kind in ("response.completed", "response.incomplete"):
            resp = p.get("response") or {}
            if not self._response_id and resp.get("id"):
                self._response_id = str(resp["id"])
            if kind == "response.incomplete":
                self._finish_reason = "length"
            else:
                self._finish_reason = "tool_calls" if self._tools else "stop"
            usage = resp.get("usage")
            if isinstance(usage, dict):
                prompt = int(usage.get("input_tokens") or 0)
                completion = int(usage.get("output_tokens") or 0)
                self._usage = {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                }
                details = usage.get("output_tokens_details")
                if isinstance(details, dict) and isinstance(details.get("reasoning_tokens"), int):
                    self._usage["completion_tokens_details"] = {
                        "reasoning_tokens": details["reasoning_tokens"],
                    }
            self._finished = True
            frames = [*self._role(), self._chunk({}, self._finish_reason)]
            if self._usage is not None:
                frames.append(_frame({
                    "id": "chatcmpl-" + self._response_id,
                    "object": "chat.completion.chunk",
                    "created": self._created,
                    "model": self._model,
                    "choices": [],
                    "usage": self._usage,
                }))
            frames.append(_DONE)
            return frames

        if kind in ("response.failed", "error"):
            if kind == "response.failed":
                err = (p.get("response") or {}).get("error") or {}
            else:
                err = p.get("error") if isinstance(p.get("error"), dict) else p
            code = str(err.get("code") or err.get("type") or "upstream_error")
            message = str(err.get("message") or "the ChatGPT plan response failed")
            self._error = (ERROR_STATUS.get(code, UNKNOWN_ERROR_STATUS), code, message)
            self._finished = True
            return [
                _frame({"error": {"message": message, "type": "server_error", "code": code}}),
                _DONE,
            ]

        # content_part/output_text .done, the summary's own .done events, and
        # anything new.
        return []

    # -- non-stream ----------------------------------------------------------

    def finish(self) -> bytes:
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(self._content) if self._content or not self._tools else None,
        }
        if self._tools:
            message["tool_calls"] = self._tools
        if self._reasoning:
            message["reasoning"] = "".join(self._reasoning)
        body: dict[str, Any] = {
            "id": "chatcmpl-" + self._response_id,
            "object": "chat.completion",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "message": message, "finish_reason": self._finish_reason}],
        }
        if self._usage is not None:
            body["usage"] = self._usage
        self._emitted = True
        return json.dumps(body, separators=(",", ":")).encode()
