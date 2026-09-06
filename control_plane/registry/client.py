"""HTTP access to other nodes' agents.

Isolated behind a tiny interface so the registry's health, telemetry and
probe-back paths can be tested without a network or a mock HTTP library.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

from .errors import ProbeFailed

log = logging.getLogger(__name__)


class AgentClient(Protocol):
    """What the registry needs from the outside world. Nothing more."""

    async def get_json(self, url: str, timeout: float) -> dict: ...
    async def post_json(self, url: str, payload: dict, timeout: float) -> dict: ...


class HttpAgentClient:
    """httpx-backed client. Every call is bounded by an explicit timeout."""

    def __init__(self, client: Any = None) -> None:
        self._client = client
        self._owned = client is None

    async def _get_client(self) -> Any:
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient()
        return self._client

    async def get_json(self, url: str, timeout: float) -> dict:
        client = await self._get_client()
        try:
            response = await client.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise ProbeFailed(f"GET {url} failed: {exc}") from exc

    async def post_json(self, url: str, payload: dict, timeout: float) -> dict:
        client = await self._get_client()
        try:
            response = await client.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise ProbeFailed(f"POST {url} failed: {exc}") from exc

    async def aclose(self) -> None:
        if self._client is not None and self._owned:
            try:
                await self._client.aclose()
            except Exception:
                pass
        self._client = None
