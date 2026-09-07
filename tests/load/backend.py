"""A fake vLLM runtime, deliberately cheap.

The harness exists to find where *the gateway* breaks, so the thing on the far
side of it must never be the bottleneck and must never be the thing that
varies. This backend pre-encodes every byte it will ever send, records no
request bodies, and does no JSON parsing it can avoid.

Every fault it injects is deterministic -- a counter and a modulus, never a
random draw -- so a run that finds a cliff can be replayed onto it.

    python -m tests.load.backend --port 9001 --tokens 64 --itl 0.02
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

# Both separator styles httpx may produce, so the streaming flag is found
# without paying for a json.loads on every request.
_STREAM_MARKERS = (b'"stream": true', b'"stream":true')

_MODEL = "loadtest"


def _sse_frames(tokens: int) -> list[bytes]:
    """One pre-encoded SSE frame per token, plus the terminator."""
    frames = []
    for i in range(tokens):
        payload = {
            "id": "chatcmpl-load",
            "object": "chat.completion.chunk",
            "created": 1757193600,
            "model": _MODEL,
            "choices": [{"index": 0, "delta": {"content": f"t{i}"}}],
        }
        frames.append(b"data: " + json.dumps(payload).encode() + b"\n\n")
    frames.append(b"data: [DONE]\n\n")
    return frames


def _completion_body(tokens: int) -> bytes:
    return json.dumps(
        {
            "id": "chatcmpl-load",
            "object": "chat.completion",
            "created": 1757193600,
            "model": _MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "x" * tokens},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": tokens,
                "total_tokens": 8 + tokens,
            },
        }
    ).encode()


_EMBEDDING_BODY = json.dumps(
    {
        "object": "list",
        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }
).encode()

_ERROR_BODY = json.dumps(
    {"error": {"message": "fake backend is unwell", "type": "server_error"}}
).encode()

_JSON = "application/json"


class FakeRuntime:
    """Knobs are plain attributes so ``POST /control`` can move them mid-run."""

    def __init__(
        self,
        *,
        tokens: int = 64,
        ttft_s: float = 0.0,
        itl_s: float = 0.0,
        max_inflight: int = 0,
        error_every: int = 0,
        hang_every: int = 0,
        die_every: int = 0,
    ) -> None:
        self.tokens = tokens
        self.ttft_s = ttft_s
        self.itl_s = itl_s
        # Past this many concurrent requests, answer 503 -- a saturated vLLM
        # shedding load rather than a dead one.
        self.max_inflight = max_inflight
        self.error_every = error_every
        self.hang_every = hang_every
        self.die_every = die_every

        self.frames = _sse_frames(tokens)
        self.completion = _completion_body(tokens)

        self.seq = 0
        self.inflight = 0
        self.peak_inflight = 0
        self.served = 0
        self.streamed = 0
        self.errored = 0
        self.hung = 0
        self.died = 0
        self.refused = 0

        self.app = Starlette(
            routes=[
                Route("/v1/chat/completions", self._chat, methods=["POST"]),
                Route("/v1/completions", self._chat, methods=["POST"]),
                Route("/v1/embeddings", self._embeddings, methods=["POST"]),
                Route("/control", self._control, methods=["POST"]),
                Route("/stats", self._stats, methods=["GET"]),
                Route("/health", self._health, methods=["GET"]),
            ]
        )

    # -- knobs -------------------------------------------------------------

    def configure(self, spec: dict) -> None:
        retoken = False
        for key in (
            "tokens",
            "ttft_s",
            "itl_s",
            "max_inflight",
            "error_every",
            "hang_every",
            "die_every",
        ):
            if key in spec:
                value = spec[key]
                if key in ("ttft_s", "itl_s"):
                    value = float(value)
                else:
                    value = int(value)
                    if key == "tokens":
                        retoken = retoken or value != self.tokens
                setattr(self, key, value)
        if retoken:
            self.frames = _sse_frames(self.tokens)
            self.completion = _completion_body(self.tokens)

    def _due(self, every: int, n: int) -> bool:
        return every > 0 and n % every == 0

    # -- routes ------------------------------------------------------------

    async def _chat(self, request: Request) -> Response:
        raw = await request.body()
        self.seq += 1
        n = self.seq

        if self.max_inflight and self.inflight >= self.max_inflight:
            self.refused += 1
            return Response(
                _ERROR_BODY, status_code=503, media_type=_JSON,
                headers={"retry-after": "1"},
            )

        if self._due(self.error_every, n):
            self.errored += 1
            return Response(_ERROR_BODY, status_code=500, media_type=_JSON)

        if self._due(self.hang_every, n):
            # Never answers. The client's own timeout, or its disconnect, is
            # what ends this -- which is the point of the probe.
            self.hung += 1
            self.inflight += 1
            try:
                await asyncio.Event().wait()
            finally:
                self.inflight -= 1
            return Response(b"", status_code=204)  # unreachable

        streaming = any(marker in raw for marker in _STREAM_MARKERS)
        dying = self._due(self.die_every, n)
        if dying:
            self.died += 1

        self.inflight += 1
        if self.inflight > self.peak_inflight:
            self.peak_inflight = self.inflight

        if not streaming:
            try:
                if self.ttft_s or self.itl_s:
                    await asyncio.sleep(self.ttft_s + self.itl_s * self.tokens)
                if dying:
                    raise ConnectionResetError("fake backend dropped the connection")
                self.served += 1
                return Response(self.completion, media_type=_JSON)
            finally:
                self.inflight -= 1

        self.streamed += 1
        return StreamingResponse(
            self._sse(dying), media_type="text/event-stream",
            headers={"cache-control": "no-cache"},
        )

    async def _sse(self, dying: bool):
        try:
            if self.ttft_s:
                await asyncio.sleep(self.ttft_s)
            if dying:
                # Headers are already on the wire. Dropping here is what a node
                # dying during prefill looks like from the gateway's side.
                raise ConnectionResetError("fake backend died during prefill")
            for frame in self.frames:
                if self.itl_s:
                    await asyncio.sleep(self.itl_s)
                yield frame
            self.served += 1
        finally:
            self.inflight -= 1

    async def _embeddings(self, request: Request) -> Response:
        await request.body()
        self.seq += 1
        self.served += 1
        return Response(_EMBEDDING_BODY, media_type=_JSON)

    async def _control(self, request: Request) -> Response:
        self.configure(await request.json())
        return Response(json.dumps(self.snapshot()).encode(), media_type=_JSON)

    async def _stats(self, request: Request) -> Response:
        return Response(json.dumps(self.snapshot()).encode(), media_type=_JSON)

    async def _health(self, request: Request) -> Response:
        return Response(b'{"ok":true}', media_type=_JSON)

    def snapshot(self) -> dict:
        return {
            "pid": os.getpid(),
            "seq": self.seq,
            "served": self.served,
            "streamed": self.streamed,
            "errored": self.errored,
            "hung": self.hung,
            "died": self.died,
            "refused": self.refused,
            "inflight": self.inflight,
            "peak_inflight": self.peak_inflight,
            "tokens": self.tokens,
            "ttft_s": self.ttft_s,
            "itl_s": self.itl_s,
            "max_inflight": self.max_inflight,
            "error_every": self.error_every,
            "hang_every": self.hang_every,
            "die_every": self.die_every,
        }


def build(**kwargs) -> FakeRuntime:
    return FakeRuntime(**kwargs)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="fake vLLM for the load harness")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--tokens", type=int, default=64)
    parser.add_argument("--ttft", type=float, default=0.0)
    parser.add_argument("--itl", type=float, default=0.0)
    parser.add_argument("--max-inflight", type=int, default=0)
    parser.add_argument("--backlog", type=int, default=4096)
    args = parser.parse_args()

    runtime = FakeRuntime(
        tokens=args.tokens,
        ttft_s=args.ttft,
        itl_s=args.itl,
        max_inflight=args.max_inflight,
    )
    uvicorn.run(
        runtime.app,
        host=args.host,
        port=args.port,
        log_level="error",
        access_log=False,
        backlog=args.backlog,
    )


if __name__ == "__main__":
    main()
