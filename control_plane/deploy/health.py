"""Backend health probing.

vLLM and SGLang both serve /health at the origin, not under /v1. We probe
that first and fall back to /v1/models, which is what sparkrun itself checks
and what a runtime behind a proxy is most likely to answer.

urllib rather than httpx so the probe has no dependency of its own and can
run from a watchdog thread without an event loop.

**Answering is not the same as being ours.** A 200 on a port says a server is
there, not that it is the server we started. `_allocate_port` hands out ports
from a counter and cannot see what a machine is already running, so a runtime
somebody else left on 8100 got adopted as a healthy deployment: the floor drew
it ready, routing sent it traffic, and asking for `Qwen2.5-0.5B-Instruct`
returned completions from a llama.cpp build serving `ling-3.0-flash`. Nothing
in the stack could notice, because nothing had ever asked the port what it
was serving. `expect_model` is that question.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .sparkrun import backend_origin

logger = logging.getLogger(__name__)

HEALTH_PATHS = ("/health", "/v1/models")
MODELS_PATH = "/v1/models"

# The clause that makes a refusal an IDENTITY refusal rather than a dead port.
# Written by `probe` and read by `wrong_model`, both in this module, so the two
# cannot drift: a caller that needs to tell "your backend is gone" from
# "somebody else's backend is answering for it" asks here rather than matching
# on prose of its own.
_MISMATCH_MARK = "something else is already on this port"

# A body larger than this is not a model list. Read bounded rather than
# whole: this runs on a watchdog thread against a port that may belong to
# anything at all, and an unbounded read from a stranger is a hang.
_MAX_BODY = 256 * 1024


def _names(payload: object) -> list[str]:
    """Every name a `/v1/models` body claims to serve.

    Three shapes, because three kinds of server end up on these ports.
    OpenAI's is `data[].id`, which vLLM and SGLang both emit and fill with
    `--served-model-name`. vLLM also carries `data[].root` -- the repository
    path it actually loaded -- which is worth reading because it is the one
    field that survives a served name we did not choose. Ollama and
    llama-server add a top-level `models[]` with `name`/`model`, and it is a
    llama-server that caused this.
    """
    out: list[str] = []
    if not isinstance(payload, dict):
        return out
    for row in payload.get("data") or []:
        if isinstance(row, dict):
            out.extend(str(row[k]) for k in ("id", "root") if row.get(k))
    for row in payload.get("models") or []:
        if isinstance(row, dict):
            out.extend(str(row[k]) for k in ("name", "model") if row.get(k))
    return out


def _tail(name: str) -> str:
    """`Qwen/Qwen2.5-0.5B-Instruct` and `Qwen2.5-0.5B-Instruct` are one model.

    `default_served_name` is the repository's last segment, so a runtime that
    reports the path it loaded rather than the name we gave it is agreeing
    with us, not contradicting us. Matching on the tail is what keeps this
    check from killing a healthy deployment over a prefix.
    """
    return name.rsplit("/", 1)[-1].strip().casefold()


def serves(names: list[str], expected: str) -> bool:
    """Does this list name the model we launched?"""
    wanted = _tail(expected)
    return any(_tail(n) == wanted for n in names)


def wrong_model(reason: str | None) -> bool:
    """Is this refusal "somebody else is on the port", not "nothing is"?

    The difference decides what an operator does next. A dead port is a
    deployment to restart; a port serving another model is a deployment whose
    url is a lie, and restarting it just re-adopts the stranger.
    """
    return bool(reason) and _MISMATCH_MARK in reason


def _models(origin: str, timeout: float) -> tuple[list[str] | None, str | None]:
    """What the port says it serves, or None when it would not say.

    None is "nothing learned", which is not the same as "wrong model": a
    runtime behind a proxy that does not speak the OpenAI model list has
    always been probed on liveness alone, and this must not start refusing
    it. Only a port that names something ELSE is a mismatch.
    """
    url = origin + MODELS_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                return None, "%s returned HTTP %d" % (url, response.status)
            body = response.read(_MAX_BODY)
    except urllib.error.HTTPError as exc:
        return None, "%s returned HTTP %d" % (url, exc.code)
    except urllib.error.URLError as exc:
        return None, "%s unreachable: %s" % (url, exc.reason)
    except OSError as exc:
        return None, "%s unreachable: %s" % (url, exc)
    try:
        names = _names(json.loads(body))
    except (ValueError, TypeError):
        return None, None
    return (names or None), None


def probe(
    backend_url: str,
    *,
    timeout: float = 3.0,
    expect_model: str | None = None,
    health_path: str | None = None,
) -> tuple[bool, str | None]:
    """Is the backend answering -- and, when asked, is it ours?

    Returns (healthy, reason_when_not).

    Without `expect_model` this is liveness and nothing more, which is what
    every caller used to get. With it, `/v1/models` is consulted FIRST and a
    port naming a different model is unhealthy, with a reason that names what
    it found: adopting a stranger silently is the failure this exists to stop,
    and falling through to `/health` afterwards would restore it -- a
    mismatched server answers `/health` perfectly well.

    A port that answers but names nothing we can read falls back to the
    liveness probe. It is the pre-existing situation, not a new fault, and
    refusing it would kill deployments behind proxies that never spoke this
    dialect.

    `health_path`, when given, REPLACES the first liveness path rather than
    adding one: every runtime's own `RuntimeSpec.health_path` is `"/health"`
    today, the same as `HEALTH_PATHS[0]`, so a caller that always threads it
    through costs nothing now and stops a future runtime with a genuinely
    different liveness endpoint from being silently ignored. Replacing rather
    than prepending also keeps the round-trip count -- and
    `manager.py`'s `hard_deadline = probe_timeout * len(HEALTH_PATHS)` budget
    built on it -- unchanged.
    """
    origin = backend_origin(backend_url)
    if expect_model:
        names, _ = _models(origin, timeout)
        if names is not None:
            if serves(names, expect_model):
                return True, None
            return False, (
                "%s%s is serving %s, not %s -- %s. Stop that server, or stop "
                "this deployment: while it stands, requests for %s are "
                "answered by another model."
                % (
                    origin,
                    MODELS_PATH,
                    ", ".join(sorted(set(names))[:4]),
                    expect_model,
                    _MISMATCH_MARK,
                    expect_model,
                )
            )
        # Nothing learned. Fall through to liveness, whose own wording is the
        # right one for a port that did not answer at all.
    paths = HEALTH_PATHS
    if health_path and health_path != HEALTH_PATHS[0]:
        paths = (health_path,) + tuple(p for p in HEALTH_PATHS if p != health_path)
    last: str | None = None
    for path in paths:
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
