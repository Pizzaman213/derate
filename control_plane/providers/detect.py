"""Is there already a model runtime on this machine?

A node with no GPU cannot carry a rank, and the Models tab says so. What it can
do is run a small model itself and be routed to, which is what a *provider* is.
Getting there used to mean reading the node's address off one screen, typing it
into another, and knowing that Ollama listens on 11434 -- three steps that are
all the same fact, and the only one the operator actually has is "that machine
over there".

This probes for that fact instead. It is deliberately narrow:

**It only ever looks at machines already in the roster.** Nothing here scans a
subnet. The addresses come from nodes a human already admitted, so this cannot
find a stranger's server and offer it as a target, and it adds no reachability
the coordinator did not already have.

**It reports, it does not register.** Finding a runtime produces a suggestion,
which is the same position ``registry.offer_candidate`` takes about a
discovered node: discovery proposes, a human accepts. Registering silently
would put an unowned box in the routing table.

**A miss is not an error.** Nothing is listening is the overwhelmingly common
answer and is not worth a warning, so a refused connection, a timeout and a
reply we do not recognise all come back the same way -- ``None``.
"""

from __future__ import annotations

import logging
from typing import Any

from ..contracts.providers import ProviderKind

log = logging.getLogger(__name__)

#: How long to wait for a runtime to answer. Short on purpose: this runs
#: against every member when a screen opens, the target is on the LAN, and a
#: box that cannot answer a tags listing in this long is not one to route to.
DETECT_TIMEOUT_S = 1.5


class RuntimeProbe:
    """One kind of runtime we know how to recognise on a node.

    ``port`` and ``probe_path`` are the native API, not the OpenAI shim: the
    shim answers for several products and would tell us a server is there
    without telling us which. ``api/tags`` is Ollama's own and nothing else
    serves it, so a match is an identification rather than a guess.
    """

    def __init__(
        self,
        kind: ProviderKind,
        port: int,
        probe_path: str,
        base_url_template: str,
        entries_key: str,
        resident_path: str = "",
        control_path: str = "",
    ) -> None:
        self.kind = kind
        self.port = port
        self.probe_path = probe_path
        self.base_url_template = base_url_template
        self.entries_key = entries_key
        #: Which models are loaded into memory right now, as opposed to merely
        #: downloaded. Two different questions, and on a 2 GB machine the
        #: second one is the one that decides whether anything else runs.
        self.resident_path = resident_path
        #: Where a load or an unload is asked for. Empty means this build can
        #: report residency but not change it, and the UI must not offer a
        #: control it cannot honour.
        self.control_path = control_path

    def base_url(self, address: str) -> str:
        return self.base_url_template.format(address=address, port=self.port)


#: Only Ollama for now, and the shape is a list so adding llama.cpp or vLLM
#: later is a row rather than a refactor. Order is match order.
RUNTIME_PROBES: tuple[RuntimeProbe, ...] = (
    RuntimeProbe(
        kind=ProviderKind.OLLAMA,
        port=11434,
        probe_path="api/tags",
        # The /v1 shim is what a provider is registered against -- it is the
        # OpenAI-compatible surface the gateway proxies to. api/tags above is
        # only how we recognised it.
        base_url_template="http://{address}:{port}/v1",
        entries_key="models",
        resident_path="api/ps",
        # Ollama has no load/unload verb. `keep_alive` on a generate with an
        # empty prompt is the documented way: -1 pins the model in memory, 0
        # evicts it, and the reply comes back with done_reason "load" or
        # "unload" having generated no tokens.
        control_path="api/generate",
    ),
)


def _model_count(payload: Any, key: str) -> int | None:
    """How many models the runtime is holding, or None if it did not say.

    None and 0 are different answers and the UI renders them differently: a
    runtime with nothing pulled is ready to be given something, which is the
    normal state of a fresh Ollama and not a fault.
    """
    if not isinstance(payload, dict):
        return None
    entries = payload.get(key)
    return len(entries) if isinstance(entries, list) else None


async def detect_runtime(address: str, client, timeout: float = DETECT_TIMEOUT_S):
    """Probe one node's address for a known runtime. None when there is none.

    *client* is the registry's ``AgentClient``: the coordinator already owns a
    pooled HTTP client that knows how to fail quietly, and opening a second one
    here would double the connection budget for a probe that mostly finds
    nothing.
    """
    if not address:
        return None
    for probe in RUNTIME_PROBES:
        url = f"http://{address}:{probe.port}/{probe.probe_path}"
        try:
            payload = await client.get_json(url, timeout=timeout)
        except Exception:
            # Nothing listening is the common case and not a warning.
            log.debug("no %s on %s", probe.kind.value, address)
            continue
        if not isinstance(payload, dict) or probe.entries_key not in payload:
            # Something answered on that port but did not speak the API. Do not
            # claim to have identified it; an unknown server on 11434 is more
            # likely someone else's than a runtime we can route to.
            log.debug("%s:%d answered but is not %s", address, probe.port, probe.kind.value)
            continue
        return {
            "kind": probe.kind.value,
            "base_url": probe.base_url(address),
            "model_count": _model_count(payload, probe.entries_key),
            "models": await _models(probe, address, client, payload, timeout),
            # Whether this build can actually load and unload here. The UI keys
            # its buttons off this rather than off the kind, so a runtime we can
            # only observe never renders a control that would do nothing.
            "controllable": bool(probe.control_path),
        }
    return None


async def _models(probe, address, client, tags, timeout) -> list[dict]:
    """Every model on this runtime, and whether it is loaded right now.

    Downloaded and loaded are different states and the difference is the whole
    point on a small machine: a 0.5B sitting on disk costs nothing, and the
    same model resident costs half of a Raspberry Pi's RAM. The roster already
    reports the machine's free memory; this says what is spending it.

    Residency is best-effort. If the runtime will not say, every model comes
    back ``resident: None`` -- unknown, which the UI renders as neither loaded
    nor unloaded rather than guessing one.
    """
    resident: dict[str, dict] = {}
    if probe.resident_path:
        try:
            ps = await client.get_json(
                f"http://{address}:{probe.port}/{probe.resident_path}", timeout=timeout
            )
            for entry in (ps or {}).get("models") or []:
                name = entry.get("name") or entry.get("model")
                if name:
                    resident[str(name)] = entry
        except Exception:
            log.debug("no residency from %s on %s", probe.kind.value, address)
            resident = {}

    known = probe.resident_path != "" and resident is not None
    out: list[dict] = []
    for entry in (tags or {}).get(probe.entries_key) or []:
        name = entry.get("name") or entry.get("model")
        if not name:
            continue
        live = resident.get(str(name))
        out.append(
            {
                "name": str(name),
                # On disk. What the pull cost.
                "size": entry.get("size"),
                # In memory, and 0/absent on a CPU-only box: Ollama reports
                # size_vram, which is honestly zero when there is no VRAM. The
                # footprint that matters there is `size` on the /api/ps entry,
                # so prefer it and fall back rather than reporting 0 bytes for
                # a model that is plainly resident.
                "resident_bytes": (live.get("size") or live.get("size_vram")) if live else None,
                "resident": (live is not None) if known else None,
            }
        )
    return out


async def set_resident(address: str, model: str, resident: bool, client, timeout: float = 60.0):
    """Load a model into memory, or evict it. Returns the runtime's own reply.

    Ollama has no load verb: a generate with an empty prompt and ``keep_alive``
    of -1 pins the model and 0 evicts it, and the reply says which it did in
    ``done_reason`` without having produced a token. That is the documented
    route and it is why this is a POST to the generate path rather than to
    something that sounds like what it does.

    The timeout is generous compared with detection's. Loading a model reads it
    off an SD card, which on the machine this exists for is slow enough that
    the default would abort a load that was going to succeed.
    """
    for probe in RUNTIME_PROBES:
        if not probe.control_path:
            continue
        url = f"http://{address}:{probe.port}/{probe.control_path}"
        return await client.post_json(
            url,
            {"model": model, "keep_alive": -1 if resident else 0},
            timeout=timeout,
        )
    raise RuntimeError("no runtime on this node can be loaded or unloaded")
