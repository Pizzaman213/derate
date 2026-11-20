"""HTTP access to other nodes' agents.

Isolated behind a tiny interface so the registry's health, telemetry and
probe-back paths can be tested without a network or a mock HTTP library.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol

from .errors import ProbeFailed

log = logging.getLogger(__name__)

# One client, shared for the coordinator's lifetime, talking to every node
# agent in the cluster. httpx's own default (100/20) is sized for a general
# web client, not a fleet of agents this coordinator itself controls.
_MAX_CONNECTIONS = 64

# The largest answer an agent call buffers. A profile, a health body or a
# telemetry sample is a few kilobytes; the journal pull passes its own, larger
# bound (collector.HttpSource).
MAX_RESPONSE_BYTES = 256 * 1024


class AgentClient(Protocol):
    """What the registry needs from the outside world. Nothing more."""

    async def get_json(
        self, url: str, timeout: float, headers: dict[str, str] | None = None
    ) -> dict: ...
    async def post_json(
        self,
        url: str,
        payload: dict,
        timeout: float,
        headers: dict[str, str] | None = None,
    ) -> dict: ...


class HttpAgentClient:
    """httpx-backed client. Every call is bounded by an explicit timeout."""

    def __init__(self, client: Any = None, max_connections: int = 0) -> None:
        self._client = client
        self._owned = client is None
        self._max_connections = max_connections or _MAX_CONNECTIONS

    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=self._max_connections,
                    max_keepalive_connections=self._max_connections,
                )
            )
        return self._client

    async def get_json(
        self,
        url: str,
        timeout: float,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> dict:
        """GET JSON. `headers` carries the agent token, as for post_json."""
        return await self._request("GET", url, None, timeout, headers, max_bytes)

    async def post_json(
        self,
        url: str,
        payload: dict,
        timeout: float,
        headers: dict[str, str] | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> dict:
        """POST JSON. `headers` carries the agent token to a peer's agent.

        Keyword-defaulted rather than required: every caller that predates the
        credentialed agent routes passes three arguments and still works.
        """
        return await self._request("POST", url, payload, timeout, headers, max_bytes)

    async def _request(
        self,
        method: str,
        url: str,
        payload: dict | None,
        timeout: float,
        headers: dict[str, str] | None,
        max_bytes: int,
    ) -> dict:
        """One bounded exchange (N-4): at most *max_bytes* of body, and the
        whole answer inside ``timeout * 2`` -- not merely each read inside
        ``timeout``, which a peer writing one byte at a time never trips."""
        client = await self._get_client()
        deadline = timeout * 2

        async def exchange() -> Any:
            kwargs: dict[str, Any] = {"timeout": timeout, "headers": headers or None}
            if payload is not None:
                kwargs["json"] = payload
            async with client.stream(method, url, **kwargs) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise ProbeFailed(f"{url}: response exceeded {max_bytes} bytes")
                return json.loads(bytes(body))

        try:
            return await asyncio.wait_for(exchange(), deadline)
        except ProbeFailed:
            raise
        except asyncio.TimeoutError as exc:
            raise ProbeFailed(f"{url}: no complete answer within {deadline:.0f}s") from exc
        except Exception as exc:
            raise ProbeFailed(f"{method} {url} failed: {exc}") from exc

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None
