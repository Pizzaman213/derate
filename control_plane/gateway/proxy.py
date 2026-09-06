"""Upstream proxying.

Three rules, in order of importance:

1. Do not buffer streams. Server-sent events pass through chunk by chunk as
   they arrive. Buffering a stream to inspect it defeats the purpose of it.
2. Do not rewrite backend errors. A client debugging a vLLM error sees the
   vLLM error, with the vLLM status code.
3. Never let a provider API key escape. It is set on the upstream request and
   appears nowhere else -- not in a response, not in a log line, not in an
   exception.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from starlette.responses import JSONResponse, Response, StreamingResponse

from control_plane.contracts import TargetKind

from .settings import GatewaySettings
from .stats import StatsRegistry

log = logging.getLogger("gateway.proxy")

# Headers we never copy in either direction: they describe the hop, not the
# payload, and forwarding them corrupts a streamed response.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
}

_SSE_TOKEN_MARKER = b"data:"
_DONE_MARKER = b"[DONE]"


@dataclass
class _Usage:
    """What we learned about a response as it went past."""

    tokens: int = 0
    ttft_s: float | None = None
    decode_s: float | None = None


class _StreamAccounting:
    """Counts tokens in a passing SSE stream without holding it up.

    Approximate by construction: one ``data:`` frame is one token for a normal
    chat completion stream. Good enough to drive a strength score, and it costs
    a substring scan per chunk rather than a parse.
    """

    def __init__(self, started: float) -> None:
        self.started = started
        self.usage = _Usage()
        self._tail = b""
        self._first_token_at: float | None = None

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        # Carry a few bytes across the boundary so a marker split between two
        # chunks is still counted exactly once.
        window = self._tail + chunk
        count = window.count(_SSE_TOKEN_MARKER) - window.count(_DONE_MARKER)
        if count > 0:
            if self._first_token_at is None:
                self._first_token_at = now
            self.usage.tokens += count
        self._tail = window[-len(_SSE_TOKEN_MARKER) :]

    def finish(self, now: float) -> _Usage:
        if self._first_token_at is not None:
            self.usage.decode_s = max(0.0, now - self._first_token_at)
        return self.usage


class _BodyAccounting:
    """Non-streaming responses carry a usage block; read it from a bounded
    copy while the bytes are already on their way to the client."""

    LIMIT = 1024 * 1024

    def __init__(self, started: float) -> None:
        self.started = started
        self.usage = _Usage()
        self._buf = bytearray()

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        if len(self._buf) < self.LIMIT:
            self._buf.extend(chunk[: self.LIMIT - len(self._buf)])

    def finish(self, now: float) -> _Usage:
        try:
            import json

            payload = json.loads(bytes(self._buf))
            usage = payload.get("usage") or {}
            tokens = usage.get("completion_tokens")
            if tokens is None:
                tokens = usage.get("total_tokens")
            if isinstance(tokens, int):
                self.usage.tokens = tokens
        except Exception:
            pass  # not JSON, truncated, or an error body. Nothing to learn.
        self.usage.decode_s = max(0.0, now - self.started)
        return self.usage


def _forward_request_headers(incoming: dict[str, str]) -> dict[str, str]:
    out = {}
    for key, value in incoming.items():
        lowered = key.lower()
        if lowered in _HOP_BY_HOP or lowered == "authorization":
            continue
        out[key] = value
    return out


def _forward_response_headers(headers: httpx.Headers) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


class UpstreamProxy:
    def __init__(self, settings: GatewaySettings, client: httpx.AsyncClient | None = None):
        self._settings = settings
        self._client = client
        self._owns_client = client is None

    async def start(self) -> None:
        if self._client is None:
            timeout = httpx.Timeout(
                connect=self._settings.upstream_connect_timeout_s,
                read=self._settings.upstream_read_timeout_s,
                write=self._settings.upstream_connect_timeout_s,
                pool=self._settings.upstream_connect_timeout_s,
            )
            limits = httpx.Limits(
                max_connections=self._settings.upstream_pool_limit,
                max_keepalive_connections=self._settings.upstream_pool_limit,
            )
            self._client = httpx.AsyncClient(timeout=timeout, limits=limits)

    async def stop(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        if self._owns_client:
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("UpstreamProxy.start() was not called")
        return self._client

    async def forward(
        self,
        *,
        selection,
        path: str,
        body: dict,
        client_headers: dict[str, str],
        api_key: str | None,
        stats: StatsRegistry,
        streaming: bool,
        on_finish=None,
    ) -> Response:
        target = selection.target
        url = target.backend_url.rstrip("/") + path
        headers = _forward_request_headers(client_headers)
        headers["content-type"] = "application/json"

        if target.kind is TargetKind.REMOTE:
            if api_key:
                headers["authorization"] = f"Bearer {api_key}"
        else:
            incoming_auth = next(
                (v for k, v in client_headers.items() if k.lower() == "authorization"),
                None,
            )
            if incoming_auth:
                headers["authorization"] = incoming_auth

        target_stats = stats.get(target.target_id)
        target_stats.begin()
        started = time.monotonic()
        accounting = _StreamAccounting(started) if streaming else _BodyAccounting(started)
        finished = False

        def settle(usage: _Usage | None, failed: bool) -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            duration = time.monotonic() - started
            if failed or usage is None:
                target_stats.fail()
            else:
                target_stats.complete(
                    tokens=usage.tokens,
                    duration_s=duration,
                    decode_s=usage.decode_s,
                    ttft_s=usage.ttft_s,
                )
            if on_finish is not None:
                on_finish()

        request = self.client.build_request("POST", url, json=body, headers=headers)
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            settle(None, failed=True)
            # There is no backend error to preserve here: we never reached it.
            # Say so plainly rather than inventing an upstream status.
            log.warning("upstream unreachable: %s (%s)", url, type(exc).__name__)
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": (
                            f"Upstream for '{selection.config.served_name}' is "
                            f"unreachable: {type(exc).__name__}."
                        ),
                        "type": "upstream_unreachable",
                        "code": "upstream_unreachable",
                    }
                },
            )

        async def stream_body():
            try:
                async for chunk in response.aiter_raw():
                    accounting.note_chunk(chunk, time.monotonic())
                    yield chunk
                settle(accounting.finish(time.monotonic()), failed=False)
            except Exception:
                settle(None, failed=True)
                raise
            finally:
                await response.aclose()

        return StreamingResponse(
            stream_body(),
            status_code=response.status_code,
            headers=_forward_response_headers(response.headers),
        )
