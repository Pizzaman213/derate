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

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx
from starlette.responses import Response, StreamingResponse

from control_plane.contracts import TargetKind
from control_plane.providers.errors import (
    AdapterUnsupportedError,
    MissingKeyError,
    ProviderError,
    ProviderNotAdmittingError,
    UpstreamError,
)

from control_plane.providers.usage import UsageSniffer

from .errors import error_response
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

# Why an attempt came back without an answer. Transport means we never reached
# the backend; a server error means we reached it and it broke.
RETRY_TRANSPORT = "transport"
RETRY_SERVER_ERROR = "http_5xx"

# ProviderService.open_upstream() has one contract for every failure to reach
# an upstream: raise UpstreamError, always -- never let a raw httpx.HTTPError
# escape. A transport failure is therefore not a distinct exception type on
# this path, it is an UpstreamError synthesized with this exact message and
# status 502 (control_plane/providers/service.py, open_upstream's own
# `except httpx.HTTPError` branch). A genuine answer from a reachable
# backend always carries the backend's own message -- or, absent one,
# f"HTTP {status}" -- which can never collide with this prefix, so matching
# on it is how forward_provider tells "never reached it" from "reached it
# and it answered 502" apart without providers.py needing to say so any
# other way.
_SYNTHETIC_TRANSPORT_PREFIX = "could not reach upstream: "


def _synthetic_transport_error_class(exc: UpstreamError) -> str | None:
    """The wrapped exception's class name, if ``exc`` is that stand-in.

    Returns ``None`` for anything else, including a genuine 502 the backend
    answered with itself.
    """
    # A genuine backend 502 that happened to echo the prefix would still carry
    # a body; the synthesized stand-in never does.
    if (
        exc.status_code == 502
        and exc.message.startswith(_SYNTHETIC_TRANSPORT_PREFIX)
        and not exc.body
    ):
        return exc.message[len(_SYNTHETIC_TRANSPORT_PREFIX) :] or "UpstreamError"
    return None


@dataclass
class Attempt:
    """What one target did with a request.

    Either it produced a response to hand the client, or it produced a reason
    to offer the request to the next target. A retryable attempt still carries
    whatever the upstream managed to say, so the last attempt in an exhausted
    chain can be replayed verbatim rather than rewritten into a gateway error
    the client cannot act on.
    """

    response: Response | None = None
    retryable: bool = False
    reason: str = ""
    target_id: str = ""
    # Set for RETRY_SERVER_ERROR: the backend's own answer, held for replay.
    status: int | None = None
    headers: dict[str, str] | None = None
    body: bytes | None = None
    # Set for RETRY_TRANSPORT: the exception class name, and nothing more.
    # A transport failure has no backend words to preserve.
    error: str = ""


@dataclass
class _Usage:
    """What we learned about a response as it went past.

    ``tokens`` is what drives the strength score and has always been an
    estimate for streams. ``prompt_tokens``/``completion_tokens`` come from the
    upstream's own usage block when it sends one, and ``estimated`` says which
    of the two you are looking at -- a record that cannot tell a counted token
    from a guessed one is not worth keeping for a month.
    """

    tokens: int = 0
    ttft_s: float | None = None
    decode_s: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    estimated: bool = True


def _apply_sniffed(usage: _Usage, sniffer: "UsageSniffer") -> None:
    """Prefer the upstream's own count over ours, and say that we did."""
    try:
        sniffed = sniffer.result()
    except Exception:  # a malformed tail is not worth a failed request
        sniffed = None
    if sniffed is None:
        usage.completion_tokens = usage.tokens
        return
    usage.prompt_tokens = sniffed.input_tokens
    usage.completion_tokens = sniffed.output_tokens
    usage.estimated = False
    if sniffed.output_tokens:
        usage.tokens = sniffed.output_tokens


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
        # The frame count keeps driving routing exactly as before. This reads
        # the real usage block out of the stream's tail in parallel, using the
        # implementation the provider path already trusts for spend.
        self._sniffer = UsageSniffer(stream=True)

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        self._sniffer.feed(chunk)
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
        _apply_sniffed(self.usage, self._sniffer)
        return self.usage


class _BodyAccounting:
    """Non-streaming responses carry a usage block; read it from a bounded
    copy while the bytes are already on their way to the client."""

    LIMIT = 1024 * 1024

    def __init__(self, started: float) -> None:
        self.started = started
        self.usage = _Usage()
        self._sniffer = UsageSniffer(stream=False)
        self._seen = 0

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started
        if self._seen < self.LIMIT:
            room = self.LIMIT - self._seen
            self._sniffer.feed(chunk[:room])
            self._seen += min(len(chunk), room)

    def finish(self, now: float) -> _Usage:
        _apply_sniffed(self.usage, self._sniffer)
        return self.usage

class _NoAccounting:
    """For a body that carries no tokens to count.

    TTFT is still real -- it is when the client could first have seen a byte,
    whatever those bytes are -- so it is kept. Everything else stays None,
    which is what settle() records as "not measured" rather than as zero.
    """

    def __init__(self, started: float) -> None:
        self.started = started
        self.usage = _Usage()

    def note_chunk(self, chunk: bytes, now: float) -> None:
        if self.usage.ttft_s is None:
            self.usage.ttft_s = now - self.started

    def finish(self, now: float) -> _Usage:
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


async def _read_bounded(response: httpx.Response, limit: int = _BodyAccounting.LIMIT) -> bytes:
    """Drain a server error body so it can be replayed later, capped.

    An error body is small. Reading one costs a target its place in the chain
    but keeps its words, which is what lets an exhausted chain answer with the
    backend's own error instead of a gateway error the client cannot act on.
    """
    buf = bytearray()
    try:
        async for chunk in response.aiter_raw():
            if len(buf) >= limit:
                break
            buf.extend(chunk[: limit - len(buf)])
    except httpx.HTTPError:
        pass  # a truncated error body still says more than none
    finally:
        await response.aclose()
    return bytes(buf)


class UpstreamProxy:
    def __init__(
        self,
        settings: GatewaySettings,
        client: httpx.AsyncClient | None = None,
        breaker=None,
        sink=None,
    ):
        from control_plane.telemetry import NULL_SINK

        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._breaker = breaker
        self._sink = sink or NULL_SINK

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

    # -- circuit breaker ---------------------------------------------------
    #
    # Only the transport verdict is reported. A backend that answers 500 is
    # reachable, and benching it for saying so would let one malformed request
    # take every replica of a model out of rotation at once.

    def _note_transport_failure(self, target_id: str) -> None:
        if self._breaker is not None:
            self._breaker.record_transport_failure(target_id)

    def _abandon_probe(self, target_id: str) -> None:
        """Give a claimed half-open probe back with NO transport verdict.

        For exits that never sent anything (misconfiguration: unresolvable
        key, unsupported adapter). Recording success would close a circuit
        the network never vouched for; recording failure would bench a
        target for a config fact. Without this, ``circuit.probing`` stays
        True forever and the target becomes permanently unselectable.
        """
        if self._breaker is not None:
            self._breaker.abandon(target_id)

    def _note_transport_success(self, target_id: str) -> None:
        if self._breaker is not None:
            self._breaker.record_success(target_id)

    async def forward(
        self,
        *,
        selection,
        path: str,
        body: dict | None,
        client_headers: dict[str, str],
        api_key: str | None,
        stats: StatsRegistry,
        streaming: bool,
        on_finish=None,
        trace=None,
        attempt_no: int = 0,
        content: bytes | None = None,
        content_type: str | None = None,
        count_tokens: bool = True,
    ) -> Attempt:
        """One target's turn. Returns an :class:`Attempt`, not a response.

        The caller decides whether a retryable attempt is worth handing to
        another target; this method only reports what happened.
        """
        target = selection.target
        url = target.backend_url.rstrip("/") + path
        headers = _forward_request_headers(client_headers)
        # A multipart upload is forwarded byte for byte under the client's own
        # content-type, boundary included -- re-encoding it would mean parsing
        # and rebuilding an audio file to change nothing about it.
        headers["content-type"] = content_type or "application/json"

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
        # Token accounting reads SSE frames or a JSON usage block; an audio
        # body is neither. Sniffing one would buffer a megabyte of MP3 to
        # learn nothing, and the frame count it produced would be fiction.
        if not count_tokens:
            accounting = _NoAccounting(started)
        elif streaming:
            accounting = _StreamAccounting(started)
        else:
            accounting = _BodyAccounting(started)
        finished = False

        def settle(
            usage: _Usage | None,
            failed: bool,
            *,
            status: int | None = None,
            retry_reason: str = "",
            error_class: str = "",
        ) -> None:
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
            # The numbers above are folded into an EWMA and are unrecoverable
            # from here on. This is the one moment they exist as themselves.
            if trace is not None:
                try:
                    self._sink.request(
                        trace.record(
                            selection=selection,
                            attempt_no=attempt_no,
                            retry_reason=retry_reason,
                            status=status,
                            error_class=error_class,
                            tokens=0 if usage is None else usage.tokens,
                            completion_tokens=None if usage is None else usage.completion_tokens,
                            prompt_tokens=None if usage is None else usage.prompt_tokens,
                            tokens_estimated=True if usage is None else usage.estimated,
                            ttft_s=None if usage is None else usage.ttft_s,
                            decode_s=None if usage is None else usage.decode_s,
                            duration_s=duration,
                        )
                    )
                except Exception:
                    log.debug("could not record request telemetry", exc_info=True)
            if on_finish is not None:
                on_finish()

        if content is not None:
            request = self.client.build_request(
                "POST", url, content=content, headers=headers
            )
        else:
            request = self.client.build_request("POST", url, json=body, headers=headers)
        try:
            response = await self.client.send(request, stream=True)
        except httpx.HTTPError as exc:
            settle(
                None,
                failed=True,
                retry_reason=RETRY_TRANSPORT,
                error_class=type(exc).__name__,
            )
            self._note_transport_failure(target.target_id)
            # There is no backend error to preserve here: we never reached it.
            log.warning("upstream unreachable: %s (%s)", url, type(exc).__name__)
            return Attempt(
                retryable=True,
                reason=RETRY_TRANSPORT,
                target_id=target.target_id,
                error=type(exc).__name__,
            )

        # It answered, so the transport is fine whatever it went on to say.
        self._note_transport_success(target.target_id)

        if response.status_code >= 500:
            captured = await _read_bounded(response)
            settle(
                None,
                failed=True,
                status=response.status_code,
                retry_reason=RETRY_SERVER_ERROR,
            )
            log.warning(
                "upstream %s answered %s; offering the request to another target",
                target.target_id,
                response.status_code,
            )
            return Attempt(
                retryable=True,
                reason=RETRY_SERVER_ERROR,
                target_id=target.target_id,
                status=response.status_code,
                headers=_forward_response_headers(response.headers),
                body=captured,
            )

        # Hold the response line until the first body chunk lands, so a node
        # that dies during prefill is still someone else's to answer. One chunk
        # is held, never the stream, so rule 1 is intact: the client's first
        # byte arrives when it always would have, and only the status line
        # moves to that moment.
        #
        # aiter_raw() may be called exactly once -- a second call raises
        # StreamConsumed -- so the iterator, not the response, is what gets
        # handed to the generator below.
        stream = response.aiter_raw()
        pending = asyncio.ensure_future(anext(stream))
        # asyncio.wait rather than wait_for: a timeout must leave the read
        # running. wait_for would cancel it mid-read and corrupt the stream,
        # and a slow first token is a long prefill, not a failure.
        await asyncio.wait({pending}, timeout=self._settings.upstream_header_hold_s)

        first: bytes | None = None
        held = pending
        if pending.done():
            held = None
            try:
                first = pending.result()
            except StopAsyncIteration:
                first = None  # an empty body is an answer, not a failure
            except httpx.HTTPError as exc:
                await response.aclose()
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_TRANSPORT,
                    error_class=type(exc).__name__,
                )
                self._note_transport_failure(target.target_id)
                log.warning(
                    "upstream died before its first chunk: %s (%s)",
                    url,
                    type(exc).__name__,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_TRANSPORT,
                    target_id=target.target_id,
                    error=type(exc).__name__,
                )
        else:
            # Past the hold budget. Commit the headers now and let the
            # generator wait out the rest of the prefill; the attempt simply
            # stops being retryable from here.
            log.info(
                "%s has not produced a first chunk in %.0fs; releasing headers",
                target.target_id,
                self._settings.upstream_header_hold_s,
            )

        async def stream_body():
            try:
                opening = first
                if held is not None:
                    try:
                        opening = await held
                    except StopAsyncIteration:
                        opening = None
                if opening is not None:
                    # Counted here rather than at the peek, so TTFT is the
                    # moment the client could first have seen a byte and a
                    # marker split across the peek boundary is still counted
                    # exactly once.
                    accounting.note_chunk(opening, time.monotonic())
                    yield opening
                async for chunk in stream:
                    accounting.note_chunk(chunk, time.monotonic())
                    yield chunk
                settle(
                    accounting.finish(time.monotonic()),
                    failed=False,
                    status=response.status_code,
                )
            except httpx.HTTPError:
                self._note_transport_failure(target.target_id)
                raise
            finally:
                # A client that hangs up raises GeneratorExit or CancelledError,
                # both of which derive from BaseException and so never reached
                # an `except Exception`. settle() is idempotent, so settling
                # here is a no-op after a clean finish and is the only thing
                # that releases the KV commitment of an abandoned stream.
                settle(None, failed=True, error_class="ClientDisconnected")
                await response.aclose()

        return Attempt(
            response=StreamingResponse(
                stream_body(),
                status_code=response.status_code,
                headers=_forward_response_headers(response.headers),
            ),
            target_id=target.target_id,
        )

    async def forward_provider(
        self,
        *,
        target,
        provider_id: str,
        upstream_id: str,
        path: str,
        body: dict,
        stats: StatsRegistry,
        streaming: bool,
        open_upstream,
        on_finish=None,
        selection=None,
        trace=None,
        attempt_no: int = 0,
    ) -> Attempt | None:
        """One target's turn, routed through a provider's own ``open_upstream``.

        This is what makes a provider's backoff, spend accounting, auth
        handling and budget enforcement actually run on live traffic (audit
        H-9) -- forwarding with a raw resolved key, as :meth:`forward` does,
        bypasses all of it. Settle-exactly-once and the outstanding count
        follow the same shape as :meth:`forward`; only where the bytes come
        from differs.

        Returns ``None`` when the provider refused to admit the request
        (rate limited, over budget, disabled, or otherwise unusable) before
        anything was sent. That is not a backend failure to replay -- there
        is nothing of the backend's to replay -- it is a reason for the
        caller to try a different target right away.
        """
        target_stats = stats.get(target.target_id)
        target_stats.begin()
        started = time.monotonic()
        accounting = _StreamAccounting(started) if streaming else _BodyAccounting(started)
        finished = False

        def settle(
            usage: _Usage | None,
            failed: bool,
            *,
            status: int | None = None,
            retry_reason: str = "",
            error_class: str = "",
        ) -> None:
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
            # Mirrors forward(): the one moment these numbers exist as
            # themselves, before they are folded into an EWMA. Without this a
            # request that a client can name by its X-Request-Id has no row
            # once the providers port exposes open_upstream -- exactly the
            # failure RequestTrace exists to prevent (audit H-9).
            if trace is not None:
                try:
                    self._sink.request(
                        trace.record(
                            selection=selection,
                            attempt_no=attempt_no,
                            retry_reason=retry_reason,
                            status=status,
                            error_class=error_class,
                            tokens=0 if usage is None else usage.tokens,
                            completion_tokens=None if usage is None else usage.completion_tokens,
                            prompt_tokens=None if usage is None else usage.prompt_tokens,
                            tokens_estimated=True if usage is None else usage.estimated,
                            ttft_s=None if usage is None else usage.ttft_s,
                            decode_s=None if usage is None else usage.decode_s,
                            duration_s=duration,
                        )
                    )
                except Exception:
                    log.debug("could not record request telemetry", exc_info=True)
            if on_finish is not None:
                on_finish()

        # An @asynccontextmanager instance: __aenter__ runs the provider's
        # _prepare() and the request loop up to its first yield. If it raises
        # there -- every branch below except the final one -- the generator
        # is already fully unwound and __aexit__ must not be called: the
        # context manager was never successfully entered.
        manager = open_upstream(provider_id, upstream_id, body, streaming, endpoint=path)
        try:
            upstream = await manager.__aenter__()
        except ProviderNotAdmittingError:
            # Rate limited, over budget, or disabled -- nothing was sent, and
            # there is no transport verdict to report either way.
            settle(None, failed=True)
            return None
        except MissingKeyError as exc:
            # The referenced env var or secret does not resolve: a
            # misconfiguration, not a capacity problem, and the single most
            # likely first-run failure. Answer exactly as the raw-key path
            # does (openai_api._dispatch's resolve_key branch) rather than
            # letting a broader `except ProviderError` fold it into "try
            # another target", which would report it to the client as
            # no_target_admitting -- capacity, not the honest "this
            # provider's key is unusable" (audit H-9).
            if trace is not None:
                trace.error_code = "provider_key_unavailable"
            settle(None, failed=True, status=502, error_class=type(exc).__name__)
            self._abandon_probe(target.target_id)
            log.warning(
                "could not resolve api key for provider %s", provider_id
            )
            return Attempt(
                response=error_response(
                    502,
                    f"Provider '{provider_id}' has no usable credential configured.",
                    "server_error",
                    "provider_key_unavailable",
                ),
                target_id=target.target_id,
            )
        except AdapterUnsupportedError as exc:
            # This provider's wire format has no adapter in this build: also
            # a configuration fact, not capacity, and also swallowed by a
            # bare `except ProviderError` before this fix.
            if trace is not None:
                trace.error_code = "provider_adapter_unsupported"
            settle(None, failed=True, status=502, error_class=type(exc).__name__)
            self._abandon_probe(target.target_id)
            log.warning("provider %s: %s", provider_id, exc)
            return Attempt(
                response=error_response(
                    502, str(exc), "server_error", "provider_adapter_unsupported"
                ),
                target_id=target.target_id,
            )
        except UpstreamError as exc:
            transport_class = _synthetic_transport_error_class(exc)
            if transport_class is not None:
                # ProviderService could not reach the upstream at all and has
                # no way to say so except by raising UpstreamError -- its
                # contract has no other shape for a transport failure. There
                # is no backend answer here to replay, so this is a
                # transport failure like any other: retryable, and the
                # breaker must hear about it as a failure, not a success
                # (audit H-9 -- a dead remote must be able to trip the
                # breaker, and a half-open probe against it must not
                # immediately re-close the circuit).
                settle(
                    None,
                    failed=True,
                    retry_reason=RETRY_TRANSPORT,
                    error_class=transport_class,
                )
                self._note_transport_failure(target.target_id)
                log.warning(
                    "provider %s unreachable for %s (%s)",
                    provider_id, upstream_id, transport_class,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_TRANSPORT,
                    target_id=target.target_id,
                    error=transport_class,
                )
            # It answered -- with an error, but it answered -- so the
            # transport verdict is success regardless of the status code.
            self._note_transport_success(target.target_id)
            headers: dict[str, str] = {"content-type": "application/json"}
            if exc.retry_after_s:
                headers["retry-after"] = str(int(exc.retry_after_s))
            if exc.status_code >= 500:
                settle(
                    None,
                    failed=True,
                    status=exc.status_code,
                    retry_reason=RETRY_SERVER_ERROR,
                )
                return Attempt(
                    retryable=True,
                    reason=RETRY_SERVER_ERROR,
                    target_id=target.target_id,
                    status=exc.status_code,
                    headers=headers,
                    body=exc.body.encode("utf-8", "replace"),
                )
            settle(None, failed=True, status=exc.status_code)
            return Attempt(
                response=Response(
                    content=exc.body.encode("utf-8", "replace"),
                    status_code=exc.status_code,
                    headers=headers,
                ),
                target_id=target.target_id,
            )
        except ProviderError:
            # Anything else this package raises (UnknownProviderError and any
            # future ProviderError this method does not yet know to name):
            # a fact about this target's configuration, not this request.
            # Try someone else rather than fail the whole request on it.
            settle(None, failed=True)
            return None
        except httpx.HTTPError as exc:
            # Defensive: ProviderService itself never lets a raw httpx error
            # escape __aenter__ (it wraps every one as UpstreamError above),
            # but a providers port is a Protocol, not this one class, and a
            # future or third-party implementation may not make that promise.
            settle(
                None,
                failed=True,
                retry_reason=RETRY_TRANSPORT,
                error_class=type(exc).__name__,
            )
            self._note_transport_failure(target.target_id)
            log.warning(
                "provider %s unreachable for %s (%s)",
                provider_id, upstream_id, type(exc).__name__,
            )
            return Attempt(
                retryable=True,
                reason=RETRY_TRANSPORT,
                target_id=target.target_id,
                error=type(exc).__name__,
            )

        self._note_transport_success(target.target_id)

        async def stream_body():
            exc_info: tuple = (None, None, None)
            try:
                async for chunk in upstream.body:
                    accounting.note_chunk(chunk, time.monotonic())
                    yield chunk
                settle(
                    accounting.finish(time.monotonic()),
                    failed=False,
                    status=upstream.status_code,
                )
            except BaseException as exc:
                # A client disconnect raises GeneratorExit or CancelledError
                # here, both of which derive from BaseException and so never
                # reach an `except Exception` -- and an httpx.HTTPError mid
                # stream is a genuine transport failure the breaker must
                # hear about, same as one before the first byte. Either way
                # the real exception, not a synthesized clean exit, is what
                # goes to the provider's own context manager below: passing
                # None there would tell ProviderService.open_upstream this
                # transfer finished cleanly, and it would call
                # runtime.note_success() and record spend for one that did
                # not (service.py's post-yield path).
                if isinstance(exc, httpx.HTTPError):
                    self._note_transport_failure(target.target_id)
                exc_info = (type(exc), exc, exc.__traceback__)
                raise
            finally:
                # settle() is idempotent, so this is a no-op after a clean
                # finish and is the only thing that releases the commitment
                # of an abandoned stream otherwise.
                settle(None, failed=True)
                await manager.__aexit__(*exc_info)

        return Attempt(
            response=StreamingResponse(
                stream_body(),
                status_code=upstream.status_code,
                media_type=upstream.media_type,
                headers=upstream.headers,
            ),
            target_id=target.target_id,
        )
