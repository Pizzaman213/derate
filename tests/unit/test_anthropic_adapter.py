"""Pure Messages translation contracts; no network, credentials or CLI state."""

from __future__ import annotations

import copy
import json

import pytest

from control_plane.providers.anthropic_adapter import (
    MAX_EVENT_BYTES,
    MessagesToChat,
    chat_to_messages,
)


def request(**fields):
    return {"messages": [{"role": "user", "content": "hello"}], **fields}


def function(name, **fields):
    return {"type": "function", "function": {"name": name, **fields}}


def call(name, call_id, arguments='{"city":"Paris"}'):
    return {"id": call_id, **function(name, arguments=arguments)}


def sse(kind, *, crlf=False, event_only=False, multiline=False, **payload):
    body = payload if event_only else {"type": kind, **payload}
    # Newlines between object members are legal JSON and exercise SSE's
    # multiline-data join without breaking any JSON string literal.
    data = json.dumps(body, ensure_ascii=False, indent=2 if multiline else None)
    raw = f"event: {kind}\n" + "".join(f"data: {line}\n" for line in data.splitlines()) + "\n"
    return raw.replace("\n", "\r\n").encode() if crlf else raw.encode()


def start(**usage):
    return sse("message_start", message={"id": "msg-stable", "role": "assistant", "usage": usage})


def block(index, kind="text", **values):
    return sse("content_block_start", index=index, content_block={"type": kind, **values})


def delta(index, kind="text_delta", **values):
    return sse("content_block_delta", index=index, delta={"type": kind, **values})


def stop_block(index):
    return sse("content_block_stop", index=index)


def end(reason="end_turn", **usage):
    return sse("message_delta", delta={"stop_reason": reason}, usage=usage) + sse("message_stop")


def adapter(stream=True, names=None, scrub=lambda text: text):
    return MessagesToChat(stream=stream, model="router-alias", now=123.75,
                          tool_names=names or {}, scrub=scrub)


def frames(raw):
    result = []
    for event in raw.split(b"\n\n"):
        if not event:
            continue
        assert event.startswith(b"data: ")
        data = event[len(b"data: "):]
        result.append("[DONE]" if data == b"[DONE]" else json.loads(data))
    return result


def translated(raw, *, stream=True, names=None, pieces=None):
    converter = adapter(stream, names)
    outgoing = []
    if pieces is None:
        pieces = [raw]
    for piece in pieces:
        outgoing.extend(converter.feed(piece))
    final = converter.finish()
    return converter, b"".join(outgoing) + final if stream else final


def test_preserves_system_order_roles_and_exact_text_without_mutating_input():
    body = request(model="untrusted-hint", stream=False, messages=[
        {"role": "system", "content": "First\n\n  system  "},
        {"role": "developer", "content": [{"type": "text", "text": "Second"}]},
        {"role": "user", "content": "A"},
        {"role": "user", "content": [{"type": "text", "text": " B "}]},
        {"role": "assistant", "content": "Reply"},
        {"role": "assistant", "content": " more"},
        {"role": "system", "content": "Third"},
        {"role": "user", "content": "Next"},
    ], models=["fallback"], provider={"order": ["untrusted"]}, user="not-account-id")
    before = copy.deepcopy(body)
    out, names = chat_to_messages(body, "actual-upstream-id")
    assert body == before
    assert out["model"] == "actual-upstream-id"
    assert out["stream"] is True
    assert out["max_tokens"] == 4096
    assert out["system"] == [
        {"type": "text", "text": "First\n\n  system  "},
        {"type": "text", "text": "Second"},
        {"type": "text", "text": "Third"},
    ]
    assert out["messages"] == [
        {"role": "user", "content": [{"type": "text", "text": "A"}, {"type": "text", "text": " B "}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Reply"}, {"type": "text", "text": " more"}]},
        {"role": "user", "content": [{"type": "text", "text": "Next"}]},
    ]
    assert not ({"models", "provider", "user"} & out.keys())
    assert names == {}


def test_maps_images_without_fetching_or_rewriting_urls():
    out, _ = chat_to_messages(request(messages=[{"role": "user", "content": [
        {"type": "text", "text": "Look: "},
        {"type": "image_url", "image_url": {"url": "https://example.org/image?q=1"}},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]}]), "m")
    assert out["messages"][0]["content"] == [
        {"type": "text", "text": "Look: "},
        {"type": "image", "source": {"type": "url", "url": "https://example.org/image?q=1"}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}},
    ]


@pytest.mark.parametrize("url", [
    "data:image/svg+xml;base64,PHN2Zz4=", "data:image/png,hello", "data:image/png;base64,%%%%",
    "data:image/png;base64,", "file:///etc/passwd", "https:///missing-host", "",
])
def test_rejects_invalid_or_unsupported_images(url):
    with pytest.raises(ValueError, match="image_url"):
        chat_to_messages(request(messages=[{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": url}},
        ]}]), "m")


def tool_history():
    return [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None, "tool_calls": [
            call("Weather", "call-1"), call("weather", "call-2"), call("mcp_Tool_0", "call-3"),
        ]},
        {"role": "tool", "tool_call_id": "call-2", "content": "lower result"},
        {"role": "tool", "tool_call_id": "call-1", "content": [{"type": "text", "text": "Upper result"}]},
        {"role": "tool", "tool_call_id": "call-3", "content": "third"},
        {"role": "assistant", "content": "Done"},
        {"role": "user", "content": "again"},
    ]


def test_tool_definitions_historical_calls_and_choice_share_lossless_names():
    out, names = chat_to_messages(request(
        tools=[function("Weather", description="upper", parameters={"type": "object"}), function("weather")],
        messages=tool_history(), tool_choice=function("weather"), parallel_tool_calls=False,
    ), "m")
    assert names == {"mcp_Tool_0": "Weather", "mcp_Tool_1": "weather", "mcp_Tool_2": "mcp_Tool_0"}
    assert out["tools"][0] == {"name": "mcp_Tool_0", "description": "upper", "input_schema": {"type": "object"}}
    assert out["tools"][1]["input_schema"] == {"type": "object", "properties": {}}
    assert out["tool_choice"] == {"type": "tool", "name": "mcp_Tool_1", "disable_parallel_tool_use": True}
    assert [m["role"] for m in out["messages"]] == ["user", "assistant", "user", "assistant", "user"]
    assert out["messages"][1]["content"] == [
        {"type": "tool_use", "id": f"call-{i + 1}", "name": f"mcp_Tool_{i}", "input": {"city": "Paris"}}
        for i in range(3)
    ]
    results = out["messages"][2]["content"]
    assert [r["tool_use_id"] for r in results] == ["call-2", "call-1", "call-3"]
    assert results[0]["content"] == [{"type": "text", "text": "lower result"}]


@pytest.mark.parametrize("choice,expected", [("auto", "auto"), ("required", "any")])
def test_generic_tool_choices(choice, expected):
    out, _ = chat_to_messages(request(tools=[function("F")], tool_choice=choice, parallel_tool_calls=False), "m")
    assert out["tool_choice"] == {"type": expected, "disable_parallel_tool_use": True}


def test_none_omits_offered_tools_but_keeps_historical_roundtrip_names():
    out, names = chat_to_messages(request(tools=[function("Weather")], messages=tool_history(), tool_choice="none"), "m")
    assert "tools" not in out and "tool_choice" not in out
    assert names["mcp_Tool_1"] == "weather"
    assert out["messages"][1]["content"][0]["type"] == "tool_use"


@pytest.mark.parametrize("change,field", [
    ({"tools": [{"type": "web_search"}]}, "tools"),
    ({"tools": [function("F", strict=True)]}, "strict"),
    ({"tool_choice": "required"}, "tool_choice"),
    ({"tools": [function("F")], "tool_choice": function("missing")}, "tool_choice"),
    ({"parallel_tool_calls": "false"}, "parallel_tool_calls"),
    ({"tools": [function("F"), function("F")]}, "name"),
    ({"tools": [function("F", parameters=[])]}, "parameters"),
])
def test_rejects_unsupported_tools_and_choices(change, field):
    with pytest.raises(ValueError, match=field):
        chat_to_messages(request(**change), "m")


@pytest.mark.parametrize("messages,field", [
    ([{"role": "tool", "tool_call_id": "unknown", "content": "orphan"}], "tool_call_id"),
    ([{"role": "assistant", "tool_calls": [call("F", "a")]}], "tool_calls"),
    ([{"role": "assistant", "tool_calls": [call("F", "a")]}, {"role": "user", "content": "skip"}], "role"),
    ([{"role": "assistant", "tool_calls": [call("F", "a"), call("F", "a")]}], "id"),
    ([{"role": "assistant", "tool_calls": [call("F", "a", "[]")]}], "arguments"),
    ([{"role": "assistant", "tool_calls": [call("F", "a", '{"x":NaN}')]}], "arguments"),
    ([{"role": "assistant", "tool_calls": [call("F", "a", "not json")]}], "arguments"),
    ([{"role": "assistant", "tool_calls": [call("F", "a", {"x": 1})]}], "arguments"),
    ([{"role": "assistant", "tool_calls": [call("F", "a")]},
      {"role": "tool", "tool_call_id": "a", "content": "first"},
      {"role": "tool", "tool_call_id": "a", "content": "duplicate"}], "tool_call_id"),
])
def test_rejects_unpaired_or_malformed_tool_history(messages, field):
    with pytest.raises(ValueError, match=field):
        chat_to_messages(request(messages=messages), "m")


def test_supported_parameters_and_metadata():
    out, _ = chat_to_messages(request(max_completion_tokens=123, max_tokens=123,
        temperature=0.2, top_p=0.8, stop=["END", "!"], n=1,
        response_format={"type": "text"}, stream_options={"include_usage": False}, user="external"), "m")
    assert out["max_tokens"] == 123
    assert out["temperature"] == 0.2 and out["top_p"] == 0.8
    assert out["stop_sequences"] == ["END", "!"]
    assert "user" not in out and "stream_options" not in out
    assert "thinking" not in out
    assert chat_to_messages(request(stop="END"), "m")[0]["stop_sequences"] == ["END"]


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_preserves_explicit_adaptive_effort_without_model_inference(effort):
    out, _ = chat_to_messages(request(reasoning_effort=effort), "unknown-model")
    assert out["thinking"] == {"type": "adaptive"}
    assert out["output_config"] == {"effort": effort}


def test_disabled_thinking_and_no_default_override():
    out, _ = chat_to_messages(request(reasoning_effort="none"), "m")
    assert out["thinking"] == {"type": "disabled"}
    assert "output_config" not in out


@pytest.mark.parametrize("fields,field", [
    ({"n": 2}, "n"), ({"n": 0}, "n"), ({"n": True}, "n"),
    ({"max_completion_tokens": 2, "max_tokens": 1}, "max_completion_tokens"),
    ({"response_format": {"type": "json_object"}}, "response_format"),
    ({"response_format": {"type": "json_schema", "json_schema": {}}}, "response_format"),
    ({"logprobs": False}, "logprobs"), ({"top_logprobs": 1}, "top_logprobs"),
    ({"presence_penalty": 0}, "presence_penalty"), ({"frequency_penalty": 0}, "frequency_penalty"),
    ({"seed": 1}, "seed"), ({"logit_bias": {}}, "logit_bias"),
    ({"modalities": ["audio"]}, "modalities"), ({"audio": {}}, "audio"),
    ({"reasoning_effort": "minimal"}, "reasoning_effort"), ({"reasoning_effort": "unknown"}, "reasoning_effort"),
    ({"stop": [1]}, "stop"), ({"stream_options": {"other": True}}, "stream_options.other"),
    ({"messages": []}, "messages"), ({"messages": "hello"}, "messages"),
    ({"messages": [None]}, "messages"),
    ({"messages": [{"role": "unknown", "content": "x"}]}, "role"),
    ({"messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {}}]}]}, "content"),
    ({"messages": [{"role": "user", "content": [{"type": "video_url", "video_url": "x"}]}]}, "content"),
    ({"messages": [{"role": "user", "content": [{"type": "text", "text": 42}]}]}, "text"),
    ({"messages": [{"role": "user", "content": "hi", "name": "somebody"}]}, "name"),
])
def test_unsupported_or_malformed_semantics_name_the_field(fields, field):
    with pytest.raises(ValueError, match=field):
        chat_to_messages(request(**fields), "m")


def conversation_events():
    return [
        start(input_tokens=11, cache_creation_input_tokens=5, cache_read_input_tokens=7, output_tokens=0),
        block(0, "thinking", thinking=""),
        delta(0, "thinking_delta", thinking="Plan 🌍"),
        delta(0, "signature_delta", signature="opaque"),
        stop_block(0),
        block(2, text=""), delta(2, text="Hi é"), delta(2, text=" 👩🏽‍💻"), stop_block(2),
        block(5, "tool_use", id="c1", name="mcp_Tool_0", input={}),
        delta(5, "input_json_delta", partial_json='{"city":'),
        delta(5, "input_json_delta", partial_json='"Paris"}'), stop_block(5),
        block(9, "tool_use", id="c2", name="mcp_Tool_1", input={}),
        delta(9, "input_json_delta", partial_json='{"n":2}'), stop_block(9),
        sse("message_delta", delta={"stop_reason": "tool_use"}, usage={"output_tokens": 4}),
        # Counts are cumulative, not incremental, and cache totals repeat.
        sse("message_delta", delta={"stop_reason": "tool_use"}, usage={
            "input_tokens": 11, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 7, "output_tokens": 8,
        }),
        sse("message_stop"),
    ]


def expected_usage():
    return {"prompt_tokens": 23, "completion_tokens": 8, "total_tokens": 31,
            "prompt_tokens_details": {"cached_tokens": 7}}


def test_stream_and_buffer_agree_in_text_thinking_tools_finish_and_usage():
    names = {"mcp_Tool_0": "Weather", "mcp_Tool_1": "weather"}
    wire = b"".join(conversation_events())
    converter, raw = translated(wire, names=names)
    buffered, whole = translated(wire, names=names, stream=False)
    assert converter.finished and buffered.finished
    assert converter.error is None and buffered.error is None
    assert converter.emitted and buffered.emitted
    output = frames(raw)
    assert output.count("[DONE]") == 1 and output[-1] == "[DONE]"
    chunks = output[:-1]
    assert all(c["id"] == "chatcmpl-msg-stable" and c["model"] == "router-alias" and c["created"] == 123 for c in chunks)
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    changes = [c["choices"][0]["delta"] for c in chunks if c["choices"]]
    assert [d["role"] for d in changes if "role" in d] == ["assistant"]
    assert "".join(d.get("content", "") for d in changes) == "Hi é 👩🏽‍💻"
    assert "".join(d.get("reasoning", "") for d in changes) == "Plan 🌍"
    streamed_tools = {}
    for change in changes:
        for tool_delta in change.get("tool_calls", []):
            tool = streamed_tools.setdefault(tool_delta["index"], {"function": {"arguments": ""}})
            tool.update({key: value for key, value in tool_delta.items() if key not in ("index", "function")})
            fn = tool_delta.get("function", {})
            if "name" in fn:
                tool["function"]["name"] = fn["name"]
            tool["function"]["arguments"] += fn.get("arguments", "")
    body = json.loads(whole)
    message = body["choices"][0]["message"]
    assert message == {"role": "assistant", "content": "Hi é 👩🏽‍💻", "reasoning": "Plan 🌍", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "Weather", "arguments": '{"city":"Paris"}'}},
        {"id": "c2", "type": "function", "function": {"name": "weather", "arguments": '{"n":2}'}},
    ]}
    assert list(streamed_tools.values()) == message["tool_calls"]
    assert chunks[-2]["choices"][0]["finish_reason"] == body["choices"][0]["finish_reason"] == "tool_calls"
    assert chunks[-1]["usage"] == body["usage"] == expected_usage()
    assert chunks[-1]["choices"] == []


def test_returned_case_sensitive_names_can_be_resubmitted_with_tool_results():
    names = {"mcp_Tool_0": "Weather", "mcp_Tool_1": "weather"}
    _, whole = translated(b"".join(conversation_events()), stream=False, names=names)
    message = json.loads(whole)["choices"][0]["message"]
    # The UI owns reasoning display; ordinary OpenAI tool history carries the
    # assistant's content/tool_calls and original response call IDs.
    message.pop("reasoning")
    body = request(tools=[function("Weather"), function("weather")], messages=[
        {"role": "user", "content": "weather"}, message,
        {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
        {"role": "tool", "tool_call_id": "c2", "content": "cloudy"},
    ])
    native, returned = chat_to_messages(body, "m")
    assert returned == names
    assert [b["name"] for b in native["messages"][1]["content"] if b["type"] == "tool_use"] == list(names)
    assert [b["tool_use_id"] for b in native["messages"][2]["content"]] == ["c1", "c2"]


def test_every_single_byte_and_every_two_piece_boundary_preserves_crlf_utf8_and_multiline_sse():
    wire = b": comment\r\n\r\n" + sse("message_start", crlf=True, event_only=True, multiline=True,
        message={"id": "split", "usage": {"input_tokens": 2}})
    wire += sse("ping", crlf=True) + sse("new_additive_event", crlf=True, data={"safe": True})
    wire += block(0, text="").replace(b"\n", b"\r\n")
    wire += sse("content_block_delta", crlf=True, multiline=True, index=0, delta={"type": "text_delta", "text": "é 😀 漢字"})
    wire += stop_block(0).replace(b"\n", b"\r\n") + end(output_tokens=3).replace(b"\n", b"\r\n")
    expected_converter, expected = translated(wire)
    assert expected_converter.error is None
    converter, result = translated(wire, pieces=[bytes([value]) for value in wire])
    assert converter.error is None and result == expected
    for boundary in range(len(wire) + 1):
        converter, result = translated(wire, pieces=[wire[:boundary], wire[boundary:]])
        assert converter.error is None and result == expected, boundary


def test_role_is_emitted_at_message_start_and_buffering_emits_nothing_early():
    stream = adapter()
    buffered = adapter(False)
    assert buffered.feed(start()) == []
    assert not buffered.emitted
    emitted = stream.feed(start())
    assert frames(b"".join(emitted))[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert stream.emitted and not stream.finished


@pytest.mark.parametrize("reason,expected", [
    ("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"),
    ("tool_use", "tool_calls"), ("refusal", "content_filter"),
])
def test_native_terminal_reason_mapping(reason, expected):
    converter, raw = translated(start() + end(reason))
    assert converter.error is None
    assert frames(raw)[-2]["choices"][0]["finish_reason"] == expected


@pytest.mark.parametrize("reason", ["pause_turn", "new_reason", None])
def test_unknown_or_missing_terminal_reason_is_real_failure(reason):
    converter, raw = translated(start() + end(reason))
    assert converter.error[0] == 502
    assert converter.error[1] == "unsupported_stop_reason"
    output = frames(raw)
    assert output[-1] == "[DONE]"
    assert "error" in output[-2]
    assert not any(c.get("choices") and c["choices"][0]["finish_reason"] for c in output if isinstance(c, dict))


@pytest.mark.parametrize("usage,expected", [
    ({}, None), ({"input_tokens": 7}, {"prompt_tokens": 7}),
    ({"output_tokens": 2}, {"completion_tokens": 2}),
    ({"cache_read_input_tokens": 4}, {"prompt_tokens_details": {"cached_tokens": 4}}),
    ({"input_tokens": 0, "output_tokens": 0}, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}),
])
def test_unknown_usage_is_absent_not_guessed(usage, expected):
    _, raw = translated(start(**usage) + end(), stream=False)
    body = json.loads(raw)
    assert body.get("usage") == expected


def test_nonempty_initial_tool_input_and_empty_object_are_not_duplicated():
    wire = start() + block(3, "tool_use", id="c1", name="mcp_Tool_0", input={"a": 1}) + stop_block(3)
    wire += block(7, "tool_use", id="c2", name="mcp_Tool_0", input={}) + stop_block(7) + end("tool_use")
    converter, raw = translated(wire, names={"mcp_Tool_0": "F"})
    assert converter.error is None
    arguments = {0: "", 1: ""}
    for event in frames(raw):
        if isinstance(event, dict) and event.get("choices"):
            for d in event["choices"][0]["delta"].get("tool_calls", []):
                arguments[d["index"]] += d.get("function", {}).get("arguments", "")
    assert arguments == {0: '{"a":1}', 1: "{}"}


@pytest.mark.parametrize("streaming", [True, False])
def test_token_limit_preserves_truncated_tool_json(streaming):
    converter = adapter(streaming, names={"mcp_Tool_0": "Weather"})
    partial = '{"city":"Par'
    wire = (
        start(input_tokens=2)
        + block(5, "tool_use", id="c1", name="mcp_Tool_0", input={})
        + delta(5, "input_json_delta", partial_json=partial)
        + stop_block(5)
    )
    outgoing = converter.feed(wire)
    # A closed content block alone does not tell us whether generation was
    # truncated. Defer validation until the native terminal reason arrives.
    assert converter.error is None and not converter.finished
    outgoing.extend(converter.feed(end("max_tokens", output_tokens=3)))
    final = converter.finish()
    assert converter.finished and converter.error is None
    if streaming:
        output = frames(b"".join(outgoing) + final)
        assert output.count("[DONE]") == 1 and output[-1] == "[DONE]"
        assert all("error" not in event for event in output if isinstance(event, dict))
        choices = [event["choices"][0] for event in output[:-1] if event["choices"]]
        tools = [tool for choice in choices for tool in choice["delta"].get("tool_calls", [])]
        assert tools[0]["id"] == "c1"
        assert tools[0]["function"]["name"] == "Weather"
        assert all(tool["index"] == 0 for tool in tools)
        assert "".join(tool["function"].get("arguments", "") for tool in tools) == partial
        assert [choice["finish_reason"] for choice in choices if choice["finish_reason"]] == ["length"]
        assert output[-2]["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    else:
        assert outgoing == []
        body = json.loads(final)
        assert body["choices"][0]["finish_reason"] == "length"
        assert body["choices"][0]["message"]["tool_calls"] == [
            {"id": "c1", "type": "function", "function": {"name": "Weather", "arguments": partial}},
        ]
        assert body["usage"] == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize("reason", ["tool_use", "end_turn"])
@pytest.mark.parametrize("arguments", ['{"city":"Par', "[]"])
def test_successful_terminal_reason_rejects_malformed_tool_arguments(streaming, reason, arguments):
    converter = adapter(streaming, names={"mcp_Tool_0": "Weather"})
    wire = (
        start()
        + block(5, "tool_use", id="c1", name="mcp_Tool_0", input={})
        + delta(5, "input_json_delta", partial_json=arguments)
        + stop_block(5)
    )
    outgoing = converter.feed(wire)
    assert converter.error is None and not converter.finished
    outgoing.extend(converter.feed(end(reason)))
    final = converter.finish()
    assert converter.finished and converter.error[0:2] == (502, "invalid_upstream_stream")
    if streaming:
        output = frames(b"".join(outgoing) + final)
        assert output.count("[DONE]") == 1 and output[-1] == "[DONE]"
        assert output[-2]["error"]["code"] == "invalid_upstream_stream"
        assert not any(event.get("choices") and event["choices"][0]["finish_reason"]
                       for event in output if isinstance(event, dict))
    else:
        assert outgoing == []
        assert json.loads(final)["error"]["code"] == "invalid_upstream_stream"


@pytest.mark.parametrize("kind,status", [
    ("invalid_request_error", 400), ("authentication_error", 401), ("permission_error", 403),
    ("not_found_error", 404), ("request_too_large", 413), ("request_too_large_error", 413),
    ("rate_limit_error", 429), ("overloaded_error", 503), ("api_error", 502), ("new_failure", 502),
])
def test_native_errors_are_scrubbed_in_both_stream_and_buffered_modes(kind, status):
    secret = "SENTINEL_CREDENTIAL"
    wire = start() + sse("error", error={"type": kind, "message": f"refused {secret}"})
    for streaming in (True, False):
        converter = adapter(streaming, scrub=lambda text: text.replace(secret, "[redacted]"))
        output = b"".join(converter.feed(wire)) + converter.finish()
        assert converter.finished
        assert converter.error == (status, kind, "refused [redacted]")
        assert secret.encode() not in output
        if streaming:
            parsed = frames(output)
            assert parsed[-1] == "[DONE]" and "error" in parsed[-2]
            assert not any(c.get("choices") and c["choices"][0]["finish_reason"] for c in parsed if isinstance(c, dict))
        else:
            assert json.loads(output)["error"]["message"] == "refused [redacted]"


def test_native_error_code_is_scrubbed_before_exposure():
    converter = adapter(scrub=lambda text: text.replace("SECRET", "[redacted]"))
    raw = b"".join(converter.feed(sse("error", error={"type": "SECRET", "message": "SECRET"})))
    assert b"SECRET" not in raw
    assert converter.error == (502, "[redacted]", "[redacted]")


@pytest.mark.parametrize("wire", [
    b"event: message_start\ndata: {bad json}\n\n",
    b"data: []\n\n", b"data: {\"type\":42}\n\n", b"data: {\"type\":\"ping\",\"bad\":NaN}\n\n",
    b"data: \xff\n\n", sse("message_start", message={}),
    start() + start(), start(input_tokens=-1), start(input_tokens=True),
    delta(0, text="before start"), start() + delta(0, text="no block"),
    start() + block(0, text="") + block(0, text="duplicate"),
    start() + block(0, text="") + delta(0, "thinking_delta", thinking="wrong block"),
    start() + block(0, text="") + end(),
    start() + block(0, text="") + stop_block(0) + delta(0, text="closed"),
    start() + block(0, "tool_use", id="x", name="mcp_Tool_0", input={})
        + delta(0, "input_json_delta", partial_json="[]") + stop_block(0),
    start() + block(0, "tool_use", id="x", name="mcp_Tool_0", input={})
        + delta(0, "input_json_delta", partial_json="{") + stop_block(0),
    start() + block(0, "tool_use", id="x", name="unknown", input={}),
])
def test_malformed_protocol_never_finishes_successfully(wire):
    converter, output = translated(wire, names={"mcp_Tool_0": "F"})
    assert converter.finished and converter.error[0] == 502
    assert converter.error[1] == "invalid_upstream_stream"
    parsed = frames(output)
    assert parsed[-1] == "[DONE]" and "error" in parsed[-2]
    assert not any(c.get("choices") and c["choices"][0]["finish_reason"] for c in parsed if isinstance(c, dict))


@pytest.mark.parametrize("wire", [
    b"", start(), start() + b"event: message_stop\ndata: {\"type\":\"message_stop\"}",
    start() + block(0, text="") + delta(0, text="partial"),
    start() + block(0, text="") + delta(0, text="partial") + stop_block(0)
        + sse("message_delta", delta={"stop_reason": "end_turn"}, usage={"output_tokens": 1}),
])
def test_early_eof_yields_error_once_for_streaming_and_nonstreaming(wire):
    for streaming in (True, False):
        converter = adapter(streaming)
        raw = b"".join(converter.feed(wire)) + converter.finish()
        assert converter.error[0] == 502 and "message_stop" in converter.error[2]
        assert converter.finished
        if streaming:
            output = frames(raw)
            assert output[-1] == "[DONE]" and output.count("[DONE]") == 1
            assert "error" in output[-2]
            assert converter.finish() == b""
        else:
            assert "error" in json.loads(raw)
        assert converter.feed(end()) == []


def test_incomplete_events_are_bounded_even_with_many_complete_data_lines():
    for pieces in (
        [b"data: " + b"x" * MAX_EVENT_BYTES],
        [b": " + b"x" * 1024 + b"\n"] * (MAX_EVENT_BYTES // 1024 + 1),
        [b"data: \"" + b"x" * (MAX_EVENT_BYTES // 2), b"x" * (MAX_EVENT_BYTES // 2)],
    ):
        converter = adapter()
        output = b"".join(frame for piece in pieces for frame in converter.feed(piece))
        assert converter.error[0] == 502 and "5 MiB" in converter.error[2]
        assert frames(output)[-1] == "[DONE]"
        assert converter.finish() == b""


def test_event_limit_is_per_event_not_per_network_chunk():
    # A single transport chunk can legitimately contain many small SSE events.
    wire = b": ping\n\n" * (MAX_EVENT_BYTES // 8 + 1) + start() + end()
    converter, raw = translated(wire)
    assert converter.error is None
    assert frames(raw)[-1] == "[DONE]"


def test_terminal_event_is_not_repeated_or_overwritten_by_trailing_events():
    converter = adapter()
    output = converter.feed(start() + end() + sse("error", error={"type": "api_error", "message": "too late"}))
    assert converter.error is None and converter.finished
    assert frames(b"".join(output)).count("[DONE]") == 1
    assert converter.feed(start() + end()) == []
    assert converter.finish() == b""


def test_tool_json_and_reasoning_survive_arbitrary_transport_splits():
    names = {"mcp_Tool_0": "Weather", "mcp_Tool_1": "weather"}
    wire = b"".join(conversation_events()).replace(b"\n", b"\r\n")
    expected_converter, expected = translated(wire, names=names)
    assert expected_converter.error is None
    for size in (1, 2, 7, 31, 127):
        pieces = [wire[offset:offset + size] for offset in range(0, len(wire), size)]
        converter, actual = translated(wire, names=names, pieces=pieces)
        assert converter.error is None and actual == expected


@pytest.mark.parametrize("wire", [
    b"event: message_start\ndata: {not-json}\n\n",
    b"data: []\n\n",
    b"data: {\"type\": NaN}\n\n",
    b"data: {\"type\": \"ping\", \"text\": \"\xff\"}\n\n",
    b"data: {}\n\n",
    sse("content_block_delta", index=0, delta={}),
])
def test_invalid_json_utf8_and_missing_types_are_errors(wire):
    converter, raw = translated(wire)
    assert converter.error[0] == 502
    assert converter.error[1] == "invalid_upstream_stream"
    assert frames(raw)[-1] == "[DONE]"


def test_buffered_finish_is_idempotent_for_success_and_failure():
    for wire in (start(input_tokens=1) + end(output_tokens=2), start()):
        converter = adapter(False)
        assert converter.feed(wire) == []
        first = converter.finish()
        assert converter.finish() == first
        assert json.loads(first)
