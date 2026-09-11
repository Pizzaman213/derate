"""Operational constants and paths for the provider subsystem.

Kept local to this package. Nothing here is a frozen contract; the frozen
material is in ``control_plane.contracts``.
"""

from __future__ import annotations

import os
from pathlib import Path

from control_plane.paths import data_dir as _data_dir

# The only thing a key ever renders as. Defined in control_plane/redaction.py
# with the scrubber that writes it, and re-exported here because this is where
# the provider subsystem has always looked for it.
from control_plane.redaction import REDACTED  # noqa: F401

PROVIDERS_FILE = "providers.json"
SECRETS_FILE = "secrets.json"

# Model lists refresh on add, on demand, and on this timer.
PROVIDER_REFRESH_S = 6 * 3600

# How often the background scan re-probes the roster for a runtime nobody
# has adopted yet (control_plane/providers/autoadopt.py). Short enough that
# "auto" feels immediate -- detect_runtime's own timeout is 1.5s per probe
# kind and there is currently one kind, so a full-roster round is cheap --
# and independent of PROVIDER_REFRESH_S above, which is about refreshing an
# ALREADY-adopted provider's model list, not finding a new one.
RUNTIME_AUTOADOPT_INTERVAL_S = 30.0

# 429 backoff, exponential between these bounds, when the upstream sends no
# Retry-After of its own. A rate-limited provider is temporarily unavailable,
# not unhealthy.
BACKOFF_MIN_S = 1.0
BACKOFF_MAX_S = 60.0

# Separate, larger cap on an upstream-*sent* Retry-After. The exponential
# backoff above is ours to bound tightly, but re-admitting earlier than an
# upstream explicitly asked for risks tripping its limiter again; this is a
# DoS guard against an upstream (or a spoofed header) asking us to back off
# for an unreasonable length of time, not a substitute for honoring the ask.
RETRY_AFTER_MAX_S = 300.0

# 5xx: one retry, jittered, then unhealthy until a request succeeds.
SERVER_ERROR_RETRIES = 1
RETRY_JITTER_S = 0.25

CONNECT_TIMEOUT_S = 10.0
WRITE_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 120.0
STREAM_READ_TIMEOUT_S = 300.0
DISCOVERY_TIMEOUT_S = 20.0

# How much of a response we are willing to hold in order to read its `usage`
# block. Streams keep only a tail; a stream is never buffered whole.
USAGE_TAIL_BYTES = 64 * 1024
USAGE_BODY_LIMIT_BYTES = 8 * 1024 * 1024

# Spend is accumulated in memory and flushed no more often than this.
SPEND_PERSIST_INTERVAL_S = 30.0

# How many bytes of an upstream error body we echo back.
ERROR_BODY_LIMIT_BYTES = 8 * 1024

# RouteTarget carries a single cost scalar but providers publish two. Blend
# them, weighted toward output, which is what a decode-heavy serving workload
# actually spends. Both published figures stay intact on the ProviderModel.
COST_BLEND_INPUT_WEIGHT = 0.25
COST_BLEND_OUTPUT_WEIGHT = 0.75


def data_dir() -> Path:
    """Where persistent state lives. ``/data`` in the container.

    Delegates so that a native install on macOS or Windows, which has no
    writable ``/data``, still lands somewhere the secrets file survives a
    restart. See ``control_plane/paths.py``.
    """
    return _data_dir()


#: Fraction of a machine's free memory a pulled model's weights may occupy.
#:
#: Weights are not the whole cost -- the server process, its KV cache and the
#: operating system all want room in the same pool -- so the download total is
#: checked against a share of what is free rather than all of it. On a box with
#: no GPU this pool is host RAM, and a machine that swaps a model is not slow,
#: it is unusable, which is why this refuses rather than warns.
PULL_HEADROOM = float(os.environ.get("DERATE_PULL_HEADROOM", "0.8"))
