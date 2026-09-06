"""Backend health probing.

vLLM and SGLang both serve /health at the origin, not under /v1. We probe
that first and fall back to /v1/models, which is what sparkrun itself checks
and what a runtime behind a proxy is most likely to answer.

urllib rather than httpx so the probe has no dependency of its own and can
run from a watchdog thread without an event loop.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from .sparkrun import backend_origin

logger = logging.getLogger(__name__)

HEALTH_PATHS = ("/health", "/v1/models")


def probe(backend_url: str, *, timeout: float = 3.0) -> tuple[bool, str | None]:
    """Is the backend answering? Returns (healthy, reason_when_not)."""
    origin = backend_origin(backend_url)
    last: str | None = None
    for path in HEALTH_PATHS:
        url = origin + path
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                if 200 <= response.status < 300:
                    return True, None
                last = "%s returned HTTP %d" % (url, response.status)
        except urllib.error.HTTPError as exc:
            last = "%s returned HTTP %d" % (url, exc.code)
        except urllib.error.URLError as exc:
            last = "%s unreachable: %s" % (url, exc.reason)
        except OSError as exc:
            last = "%s unreachable: %s" % (url, exc)
    return False, last
