"""Day 0 stub: one fake OpenRouter provider, three models, canned tokens.

Wires LOCAL_FIRST against this before any real key exists. It is not a mock
object: it is the real :class:`ProviderService` driven through an
``httpx.MockTransport``, so discovery, streaming, usage accounting, backoff and
redaction all run the code that will run in production. Only the network is
fake.

The wiring the original note meant to delete was `node.py`'s call site, and
that happened: production supplies real providers behind
`GatewayDeps(strict=True, ...)`, which refuses to start if any port is still
missing. This file stayed: `__init__.py` exports `build_stub_service` and
`stub_transport` as this package's public fake, and
`tests/unit/test_providers.py` is what actually reaches for them.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx

from ..contracts.providers import ProviderKind
from .secrets import SecretStore
from .service import ProviderService

STUB_KEY_REF = "DERATE_STUB_PROVIDER_KEY"
STUB_KEY_VALUE = "sk-or-v1-stub00000000000000000000000000000000000000"
STUB_PROVIDER_ID = "openrouter-stub"

# Plausible published pricing, in USD per single token, which is the unit
# OpenRouter uses. Discovery multiplies to per-million.
STUB_MODELS = [
    {
        "id": "anthropic/claude-sonnet-4.5",
        "name": "Anthropic: Claude Sonnet 4.5",
        "context_length": 200000,
        "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        "supported_parameters": ["tools", "tool_choice", "temperature", "max_tokens"],
    },
    {
        "id": "openai/gpt-4o-mini",
        "name": "OpenAI: GPT-4o mini",
        "context_length": 128000,
        "pricing": {"prompt": "0.00000015", "completion": "0.0000006"},
        "supported_parameters": ["tools", "temperature", "max_tokens"],
    },
    {
        "id": "meta-llama/llama-3.3-70b-instruct",
        "name": "Meta: Llama 3.3 70B Instruct",
        "context_length": 131072,
        "pricing": {"prompt": "0.00000012", "completion": "0.0000003"},
        "supported_parameters": ["temperature", "max_tokens"],
    },
]

CANNED_TOKENS = [
    "Pipeline ", "parallel ", "beats ", "tensor ", "parallel ",
    "at ", "10.2 ", "GB/s.",
]


class _AsyncByteStream(httpx.AsyncByteStream):
    """Yields pre-baked chunks, optionally paced, so a stream looks like one."""

    def __init__(self, chunks: list[bytes], delay_s: float = 0.0) -> None:
        self._chunks = chunks
        self._delay_s = delay_s

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            yield chunk

    async def aclose(self) -> None:
        return None


def sse_chunks(model: str, tokens: list[str] | None = None, usage: bool = True) -> list[bytes]:
    """Canned OpenAI-shaped SSE frames, one per token."""
    tokens = tokens if tokens is not None else CANNED_TOKENS
    frames: list[bytes] = []
    for index, token in enumerate(tokens):
        payload = {
            "id": "chatcmpl-stub",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": token} if index == 0
                    else {"content": token},
                    "finish_reason": None,
                }
            ],
        }
        frames.append(b"data: " + json.dumps(payload).encode() + b"\n\n")
    frames.append(
        b"data: "
        + json.dumps(
            {
                "id": "chatcmpl-stub",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        ).encode()
        + b"\n\n"
    )
    if usage:
        frames.append(
            b"data: "
            + json.dumps(
                {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 24,
                        "completion_tokens": len(tokens),
                        "total_tokens": 24 + len(tokens),
                    },
                }
            ).encode()
            + b"\n\n"
        )
    frames.append(b"data: [DONE]\n\n")
    return frames


def completion_body(model: str, tokens: list[str] | None = None) -> dict:
    tokens = tokens if tokens is not None else CANNED_TOKENS
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "".join(tokens)},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 24,
            "completion_tokens": len(tokens),
            "total_tokens": 24 + len(tokens),
        },
    }


def stub_handler(delay_s: float = 0.0) -> Callable[[httpx.Request], httpx.Response]:
    """An httpx MockTransport handler standing in for OpenRouter."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/models"):
            return httpx.Response(200, json={"data": STUB_MODELS})
        if request.method == "POST" and path.endswith("/chat/completions"):
            body = json.loads(request.content or b"{}")
            model = body.get("model", "unknown")
            if body.get("stream"):
                return httpx.Response(
                    200,
                    headers={"content-type": "text/event-stream"},
                    stream=_AsyncByteStream(sse_chunks(model), delay_s),
                )
            return httpx.Response(200, json=completion_body(model))
        return httpx.Response(404, json={"error": {"message": f"no stub route for {path}"}})

    return handler


def stub_transport(delay_s: float = 0.0) -> httpx.MockTransport:
    return httpx.MockTransport(stub_handler(delay_s))


def build_stub_service(
    data_path: Path | None = None,
    *,
    delay_s: float = 0.02,
    now: Callable[[], float] | None = None,
) -> ProviderService:
    """A ProviderService with one healthy fake OpenRouter provider.

    The key is a reference like any other, pointing at a value in a 0600
    secrets file inside the stub's data directory. Nothing about the secret
    path is special-cased for the stub, which is the point.
    """
    root = Path(data_path) if data_path is not None else Path(
        tempfile.mkdtemp(prefix="derate-provider-stub-")
    )
    root.mkdir(parents=True, exist_ok=True)

    secrets = SecretStore(root / "secrets.json", env={})
    secrets.put(STUB_KEY_REF, STUB_KEY_VALUE)

    kwargs = {"now": now} if now is not None else {}
    service = ProviderService(
        data_path=root,
        secrets=secrets,
        client_factory=lambda: httpx.AsyncClient(transport=stub_transport(delay_s)),
        **kwargs,
    )
    if STUB_PROVIDER_ID not in {p.provider_id for p in service.list()}:
        provider = service.add(
            {
                "provider_id": STUB_PROVIDER_ID,
                "kind": ProviderKind.OPENROUTER,
                "display_name": "OpenRouter (stub)",
                "api_key_ref": STUB_KEY_REF,
                "priority": 10,
            }
        )
        # Switch the whole stub catalogue on. A real provider starts with
        # nothing enabled and waits for somebody to choose, but there is nobody
        # to choose here: this exists so LOCAL_FIRST can be wired against three
        # priced models before any key does, and a provider serving nothing
        # would rehearse the empty case instead of the one being built.
        service.update(
            STUB_PROVIDER_ID,
            {"enabled_models": [m.upstream_id for m in provider.models]},
        )
    return service
