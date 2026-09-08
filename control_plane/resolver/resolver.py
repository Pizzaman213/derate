"""The resolver: a HuggingFace model id in, a complete ModelShape out.

Everything downstream reads these numbers. Agent D's memory arithmetic and
Agent E's parallelism choice are both wrong if the KV head count, the active
parameter count or the quantization is wrong here, so every field is either
read from real metadata or accompanied by a warning saying it was not.
"""

from __future__ import annotations

import logging
import re
import threading

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from control_plane.contracts import ModelShape, NodeProfile
from control_plane.contracts.quant import (
    BYTES_PER_PARAM,
    DEFAULT_DTYPE,
    bytes_per_param,
    normalize_dtype,
    quant_info,
)

from . import gguf as gguf_mod
from . import gguf_names, imageprobe, quant_detect, support
from .cache import ShapeCache
from .config_map import Mapped, map_config, vision_config
from .hf import HubClient, ModelInfo, count_safetensors_params, safetensors_header
from .params import ParamBreakdown, analytic_breakdown, reconcile
from .types import (
    QuantVariant,
    MetadataUnavailable,
    ModelNotFound,
    ParamSource,
    QuantSource,
    Resolution,
    ResolverError,
    SupportVerdict,
    UnsupportedArchitecture,
)

log = logging.getLogger("resolver.probe")

#: Above this, the hub's parameter tally is not believed. It has been seen to
#: count storage elements rather than logical weights on exotic 4-bit packings,
#: and being 40 percent wrong about a 70B model is not a rounding error.
_TALLY_DISAGREEMENT_LIMIT = 0.15


class ModelResolver:
    """Implements ``ResolverPort``. Safe to share; the cache is thread safe enough."""

    def __init__(
        self,
        client: HubClient | None = None,
        cache: ShapeCache | None = None,
        *,
        offline: bool = False,
        runtime_images: dict[str, str] | None = None,
    ) -> None:
        self.client = client or HubClient()
        self.cache = cache if cache is not None else ShapeCache()
        self.offline = offline or os.environ.get("DERATE_OFFLINE") == "1"
        # {runtime: container image}, from the composition root, which is the
        # only thing that knows both. Empty means the support table answers
        # from its static lists, which is the state on any machine without
        # docker -- see `start` and `imageprobe`.
        self.runtime_images = dict(runtime_images or {})

    # ---- startup -------------------------------------------------------

    def start(self) -> None:
        """Ask each runtime image what it can load. Returns immediately.

        Called by the gateway lifespan's resolver step, which is bounded by
        `startup_step_timeout_s` -- five seconds, against a container start
        that takes fifteen to thirty. So the probe runs on its own thread and
        this returns at once: a startup step that timed out would be recorded
        as degraded and, worse, would have thrown away the answer.

        Until it lands, the static tables answer. That is the same result as
        a machine with no docker, which is why nothing here waits on it.
        """
        if not self.runtime_images:
            return
        threading.Thread(
            target=self._probe_runtime_images, name="derate-imageprobe", daemon=True
        ).start()

    def _probe_runtime_images(self) -> None:
        cache_dir = self.cache.directory.parent / "runtimes"
        for runtime, image in self.runtime_images.items():
            try:
                found = imageprobe.probe(runtime, image, cache_dir=cache_dir)
            except Exception:  # pragma: no cover - probe is best effort
                found = None
            if found is None:
                log.info(
                    "runtime probe: %s (%s) not readable here, keeping the "
                    "static architecture table",
                    runtime,
                    image,
                )
                continue
            support.record_probe(found)
            log.info(
                "runtime probe: %s loads %d architectures, per %s",
                runtime,
                len(found.architectures),
                found.provenance,
            )

    # ---- ResolverPort --------------------------------------------------

    def resolve(self, model_id: str, dtype: str | None = None) -> ModelShape:
        return self.resolve_full(model_id, dtype).shape

    # ---- the full answer -----------------------------------------------

    def resolve_full(
        self,
        model_id: str,
        dtype: str | None = None,
        revision: str = "main",
        *,
        refresh: bool = False,
    ) -> Resolution:
        """Resolve a model id, a local directory, or a local ``.gguf`` file."""
        started = time.perf_counter()

        local = Path(model_id).expanduser()
        if local.is_dir() and (local / "config.json").is_file():
            return self._resolve_local_dir(local, dtype, started)

        is_remote_gguf = model_id.startswith("hf://")
        if is_remote_gguf or local.suffix.lower() == ".gguf":
            # An ``hf://`` reference reads the file over the network, so it is
            # gated and cached exactly like a hub config resolution below. A
            # local file needs no network, is exempt from the offline gate,
            # and -- like ``_resolve_local_dir`` -- is never cached: caching a
            # path on disk would go stale the moment that file was replaced or
            # deleted, and every read is already as cheap as a cache hit.
            return self._resolve_gguf_cached(
                model_id, dtype, revision, started,
                refresh=refresh, needs_network=is_remote_gguf,
            )

        if not refresh:
            hit = self.cache.get(model_id, revision, dtype)
            if hit is not None:
                hit.elapsed_ms = (time.perf_counter() - started) * 1000.0
                return hit

        if self.offline:
            raise MetadataUnavailable(
                f"{model_id!r} is not cached and the resolver is in offline mode"
            )

        info, config = self._fetch_metadata(model_id, revision)
        res = self._build(
            model_id=model_id,
            config=config,
            info=info,
            dtype_override=dtype,
            revision=(info.sha if info and info.sha else revision),
        )
        res.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.cache.put(model_id, revision, dtype, res)
        if info and info.sha and info.sha != revision:
            # Also key it by commit, so a pinned resolve is an immediate hit.
            self.cache.put(model_id, info.sha, dtype, res)
        return res

    def resolve_config(
        self,
        config: dict[str, Any],
        model_id: str,
        dtype: str | None = None,
        *,
        measured_total: int | None = None,
        weight_bytes: int | None = None,
        revision: str = "inline",
    ) -> Resolution:
        """Resolve from a config dict already in hand. No network, no cache.

        The path for a config someone already fetched, and the one the offline
        tests use.
        """
        started = time.perf_counter()
        res = self._build(
            model_id=model_id,
            config=config,
            info=None,
            dtype_override=dtype,
            revision=revision,
            measured_total=measured_total,
            weight_bytes=weight_bytes,
        )
        res.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return res

    def _fetch_metadata(
        self, model_id: str, revision: str
    ) -> tuple[ModelInfo | None, dict[str, Any]]:
        """Model record and config.json, fetched at the same time."""
        with ThreadPoolExecutor(max_workers=2) as pool:
            info_future = pool.submit(self._safe_model_info, model_id, revision)
            config_future = pool.submit(self.client.config, model_id, revision)
            config = config_future.result()
            info = info_future.result()
        return info, config

    def _safe_model_info(self, model_id: str, revision: str) -> ModelInfo | None:
        try:
            return self.client.model_info(model_id, revision)
        except ResolverError:
            return None  # config.json alone is enough, with warnings

    def _resolve_local_dir(self, path: Path, dtype: str | None, started: float) -> Resolution:
        import json

        config = json.loads((path / "config.json").read_text())
        measured, weight_bytes = _count_local_shards(path)
        if weight_bytes is None:
            index = path / "model.safetensors.index.json"
            if index.is_file():
                try:
                    meta = json.loads(index.read_text()).get("metadata", {})
                    weight_bytes = int(meta.get("total_size") or 0) or None
                except (OSError, ValueError):
                    weight_bytes = None
        res = self._build(
            model_id=str(path),
            config=config,
            info=None,
            dtype_override=dtype,
            revision="local",
            measured_total=measured,
            weight_bytes=weight_bytes,
        )
        res.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return res

    # ---- construction ---------------------------------------------------

    def _build(
        self,
        *,
        model_id: str,
        config: dict[str, Any],
        info: ModelInfo | None,
        dtype_override: str | None,
        revision: str,
        measured_total: int | None = None,
        weight_bytes: int | None = None,
    ) -> Resolution:
        warnings: list[str] = []
        try:
            mapped = map_config(config)
        except KeyError as exc:
            raise UnsupportedArchitecture(
                f"cannot read a shape out of {model_id!r}: {exc}"
            ) from exc
        warnings.extend(mapped.warnings)

        if info is None and not model_id.startswith("/"):
            warnings.append(
                "the hub's model record could not be read; parameter counts come "
                "from the config rather than from the weight index"
            )

        dtype, quant_source, quant_warnings = self._detect_quant(
            config, model_id, info, dtype_override, revision
        )
        warnings.extend(quant_warnings)

        breakdown = analytic_breakdown(mapped, vision_config(config))
        measured, param_source, measure_warnings = self._measure_params(
            model_id, info, mapped, breakdown, dtype, measured_total
        )
        warnings.extend(measure_warnings)

        accounting = reconcile(mapped, breakdown, measured)
        warnings.extend(accounting.warnings)

        if weight_bytes is None and info is not None:
            weight_bytes = _shard_bytes(info)
        if weight_bytes and breakdown.mtp and accounting.total_params:
            # The shards on disk include the MTP module we just excluded from
            # the parameter count. Charge bytes for what a runtime loads.
            checkpoint = accounting.total_params + breakdown.mtp
            weight_bytes = int(weight_bytes * accounting.total_params / checkpoint)
            warnings.append(
                "measured weight bytes were reduced in the same proportion, since "
                "the shards on disk carry that module too"
            )
        warnings.extend(
            _weight_byte_warnings(accounting.total_params, dtype, weight_bytes)
        )

        shape = _shape_from(model_id, mapped, accounting, dtype)
        verdict = support.build_verdict(mapped.architectures, dtype)
        for entry in verdict.runtimes:
            if not entry.ok:
                warnings.append(f"{entry.runtime}: {entry.reason}")

        return Resolution(
            shape=shape,
            revision=revision,
            param_source=param_source,
            quant_source=quant_source,
            support=verdict,
            warnings=warnings,
            weight_bytes=weight_bytes,
            architectures=mapped.architectures,
            model_type=mapped.model_type,
            max_position_embeddings=mapped.max_position_embeddings,
            param_breakdown=breakdown.as_dict(),
            resolved_at=time.time(),
        )

    def _detect_quant(
        self,
        config: dict[str, Any],
        model_id: str,
        info: ModelInfo | None,
        override: str | None,
        revision: str,
    ) -> tuple[str, QuantSource, list[str]]:
        sidecar = None
        needs_sidecar = not isinstance(config.get("quantization_config"), dict)
        if needs_sidecar and info is not None and info.has_file("hf_quant_config.json"):
            try:
                sidecar = self.client.file_json(
                    info.model_id, "hf_quant_config.json", revision=revision
                )
            except ResolverError:
                sidecar = None
        return quant_detect.detect(
            config, model_id, override=override, hf_quant_config=sidecar
        )

    def _measure_params(
        self,
        model_id: str,
        info: ModelInfo | None,
        mapped: Mapped,
        breakdown: ParamBreakdown,
        dtype: str,
        measured_total: int | None,
    ) -> tuple[int | None, ParamSource, list[str]]:
        """Get the real parameter count. Never a formula when a weight file can say."""
        warnings: list[str] = []
        analytic = breakdown.total_with_mtp

        if measured_total:
            return measured_total, ParamSource.SAFETENSORS_HEADERS, warnings

        tally = info.safetensors_total() if info else None
        if tally:
            drift = abs(tally - analytic) / analytic if analytic else 0.0
            if drift <= _TALLY_DISAGREEMENT_LIMIT:
                return tally, ParamSource.HUB_SAFETENSORS_INDEX, warnings

            # The two disagree. Shard bytes are the tie-breaker: they are a
            # measurement neither figure can argue with. The hub's tally counts
            # packed storage elements on some 4-bit formats, and the analytic
            # model cannot describe a network whose layers differ from each
            # other, so either one can be the wrong one.
            shard_bytes = _shard_bytes(info)
            implied = shard_bytes / bytes_per_param(dtype) if shard_bytes else None
            if implied:
                if abs(tally - implied) <= abs(analytic - implied):
                    warnings.append(
                        f"the config-derived estimate of {analytic / 1e9:.1f}B "
                        f"parameters disagrees with the hub's {tally / 1e9:.1f}B; "
                        f"{shard_bytes / 1024**3:.0f} GiB of shards at {dtype} back "
                        "the hub, which was used"
                    )
                    return tally, ParamSource.HUB_SAFETENSORS_INDEX, warnings
                warnings.append(
                    f"the hub counts {tally / 1e9:.1f}B parameters where the config "
                    f"describes {analytic / 1e9:.1f}B; {shard_bytes / 1024**3:.0f} GiB "
                    f"of shards at {dtype} back the config, which was used"
                )
                return None, ParamSource.CONFIG_ESTIMATE, warnings

            # No bytes to arbitrate with. Take the larger: over-stating the
            # footprint costs a refusal, under-stating it costs an OOM.
            if tally > analytic:
                warnings.append(
                    f"the hub counts {tally / 1e9:.1f}B parameters where the config "
                    f"describes {analytic / 1e9:.1f}B, and nothing could arbitrate; "
                    "took the larger"
                )
                return tally, ParamSource.HUB_SAFETENSORS_INDEX, warnings
            warnings.append(
                f"the hub counts {tally / 1e9:.1f}B parameters where the config "
                f"describes {analytic / 1e9:.1f}B, and nothing could arbitrate; "
                "took the larger, which is the config figure"
            )

        # No usable tally. Try the index's byte total before giving up on
        # measurement entirely: shard bytes divided by bytes per parameter is
        # still a measurement, just a coarser one.
        if info is not None and info.has_file("model.safetensors.index.json"):
            shard_bytes = _shard_bytes(info)
            if shard_bytes:
                per_param = bytes_per_param(dtype)
                estimate = int(shard_bytes / per_param)
                # Tighter than the tally test: shard bytes include block scales
                # and any tensor the repo left wide, so this only stands in when
                # it corroborates the config rather than contradicting it.
                drift = abs(estimate - analytic) / analytic if analytic else 1.0
                if drift <= 0.05:
                    warnings.append(
                        "parameter count derived from shard bytes divided by "
                        f"{per_param} bytes per parameter"
                    )
                    return estimate, ParamSource.INDEX_TOTAL_SIZE, warnings

        return None, ParamSource.CONFIG_ESTIMATE, warnings

    # ---- GGUF -----------------------------------------------------------

    def _resolve_gguf_cached(
        self,
        model_id: str,
        dtype: str | None,
        revision: str,
        started: float,
        *,
        refresh: bool,
        needs_network: bool,
    ) -> Resolution:
        """``resolve_gguf_full`` behind the same cache and offline gate as a
        hub config resolution, so a repeat ``hf://`` resolve is a cache hit
        rather than a fresh Range-read, and offline mode never reaches the
        network for one that is not already cached.

        A local file takes neither the cache nor the offline gate: it needs no
        network, and unlike an ``hf://`` blob (immutable once fetched -- the
        whole reason it is safe to cache), a path on disk can be replaced or
        deleted out from under a long-lived resolver. Caching it would serve a
        stale shape for up to the TTL after a replacement, or silently keep
        "resolving" a file that no longer exists; re-reading the header is the
        exact behavior ``_resolve_local_dir`` already keeps for a config
        directory, and a header-only read is cheap enough that there is
        nothing to save by memoizing it.
        """
        if not needs_network:
            res = self.resolve_gguf_full(model_id, dtype=dtype)
            res.elapsed_ms = (time.perf_counter() - started) * 1000.0
            return res

        if not refresh:
            hit = self.cache.get(model_id, revision, dtype)
            if hit is not None:
                hit.elapsed_ms = (time.perf_counter() - started) * 1000.0
                return hit

        if self.offline:
            raise MetadataUnavailable(
                f"{model_id!r} is not cached and the resolver is in offline mode"
            )

        res = self.resolve_gguf_full(model_id, dtype=dtype)
        res.elapsed_ms = (time.perf_counter() - started) * 1000.0
        self.cache.put(model_id, revision, dtype, res)
        return res

    def resolve_gguf(self, path: str) -> ModelShape:
        return self.resolve_gguf_full(path).shape

    def resolve_gguf_full(self, path: str, dtype: str | None = None) -> Resolution:
        """Read a GGUF header, local or on the hub as ``hf://repo/file.gguf``.

        This is the public entry point below ``resolve_full``'s cache and
        offline gate, so a caller reaching it directly for a remote blob gets
        the same offline honesty rather than a bypass: an uncached ``hf://``
        path still raises instead of opening a socket. ``resolve_full`` already
        checks this before it ever calls in here; the check is repeated so a
        direct call carries the same guarantee.
        """
        started = time.perf_counter()
        name = path
        if path.startswith("hf://"):
            if self.offline:
                raise MetadataUnavailable(
                    f"{path!r} is not cached and the resolver is in offline mode"
                )
            repo_file = path[len("hf://") :]
            parts = repo_file.split("/")
            if len(parts) < 3:
                raise ModelNotFound(f"expected hf://owner/repo/file.gguf, got {path!r}")
            repo = "/".join(parts[:2])
            filename = "/".join(parts[2:])
            reader = gguf_mod.RangeReader(
                lambda start, length: self.client.read_range(repo, filename, start, length)
            )
            header = gguf_mod.read_header(reader)
            name = f"{repo}/{filename}"
        else:
            local = Path(path).expanduser()
            if not local.is_file():
                raise ModelNotFound(f"no GGUF file at {path!r}")
            header = gguf_mod.read_gguf_file(str(local))
            name = str(local)

        res = _resolution_from_gguf(header, name, dtype)
        res.elapsed_ms = (time.perf_counter() - started) * 1000.0
        return res

    # ---- support ---------------------------------------------------------

    def supported_by(self, shape: ModelShape, runtime: str) -> tuple[bool, str]:
        """Whether a runtime can load this shape, architecture and quant both.

        ``ModelShape`` carries no architecture field, so the architecture comes
        from the cached resolution for this model id when there is one.
        """
        architectures = self._architectures_for(shape)
        return support.evaluate_runtime(runtime, architectures, shape.dtype).as_tuple()

    def supported_on(
        self, shape: ModelShape, runtime: str, nodes: list[NodeProfile]
    ) -> tuple[bool, str]:
        """As ``supported_by``, and also whether these nodes can run the quant."""
        ok, reason = self.supported_by(shape, runtime)
        if not ok:
            return False, reason
        quant_ok, problems = support.check_nodes(shape.dtype, nodes)
        if not quant_ok:
            return False, "; ".join(problems)
        if problems:
            return True, f"{reason}; {'; '.join(problems)}"
        return True, reason

    def modality_of(self, shape: ModelShape) -> str:
        """Which endpoint family this model answers on: "text" for anything
        this build does not recognise as speech.

        Same architecture lookup as ``supported_by``, and the same limitation:
        a shape that was never resolved here reports no architecture and
        therefore reads as text, which is the safe default -- it means the
        model is offered on the routes it always was.
        """
        return support.modality_for(self._architectures_for(shape))

    def support_verdict(self, shape: ModelShape) -> SupportVerdict:
        return support.build_verdict(self._architectures_for(shape), shape.dtype)

    def _architectures_for(self, shape: ModelShape) -> tuple[str, ...]:
        """The architecture for a shape, from whatever resolution produced it.

        ``ModelShape`` has no architecture field, so this reaches back into the
        cache. A shape that was never resolved here -- a fixture, say -- reports
        no architecture, and the support check says unverified rather than
        inventing a verdict.
        """
        for dtype in (None, shape.dtype):
            cached = self.cache.get(shape.model_id, "main", dtype)
            if cached is not None and cached.architectures:
                return cached.architectures
        return ()

    # ---- quantization variants -------------------------------------------

    #: Shard suffix llama.cpp writes when a quantization spans several files.
    _SHARD_RE = re.compile(r"-\d{5}-of-\d{5}(?=\.gguf$)", re.IGNORECASE)

    #: Suffixes a quantizer appends to a repository name. Stripped from both
    #: sides of a comparison so a repo differing only by its quantization is
    #: recognised as the same model, and one differing by anything else is not.
    _QUANT_SUFFIXES = (
        "-GGUF", "-AWQ", "-GPTQ", "-FP8", "-FP4", "-bnb-4bit", "-INT4", "-INT8",
        "-NVFP4", "-MXFP4", "-4bit", "-8bit",
    )

    #: How many GGUF repositories to open for one model. Each one costs a
    #: model-info round trip, and the tail of a hub search for a popular model
    #: is other people's re-uploads of the same quantizations. Six covers the
    #: publishers who actually maintain distinct ladders -- Unsloth, bartowski,
    #: mradermacher, lmstudio-community and a couple of others -- without
    #: turning one click into thirty requests.
    _MAX_GGUF_REPOS = 6

    #: How many GGUF headers one ``quant_variants`` call may read.
    #:
    #: Scoped to the whole enumeration, not to each repository: six repos at
    #: three probes each would be eighteen multi-second ranged reads, which is
    #: the 20s timeout again with extra steps. A probe costs one or more 4 MiB
    #: ranged reads because ``read_header`` walks every KV pair to reach
    #: ``general.file_type``, and a tokenizer with 151k entries sits in front
    #: of it -- measured at 2.4-3.3s per file against the live hub.
    #:
    #: Three is enough for a repo that ships one or two oddly-named builds and
    #: not enough to blow the budget on a repo where nothing parses. When it is
    #: spent the remaining files are priced at the default and SAY SO -- the
    #: note must never claim a header could not be read when it was never read.
    _MAX_HEADER_PROBES = 3

    @staticmethod
    def _looks_like_gguf_repo(hit_id: str, tags: tuple[str, ...] | list[str]) -> bool:
        """Does this search hit ship GGUF files?

        ``endswith("gguf")`` was too strict in both directions publishers
        actually deviate: ``-GGUF-v2`` and ``-gguf-imatrix`` are ordinary GGUF
        repositories whose names carry a qualifier after the format, and the
        hub tags them all ``gguf`` regardless of what the name does. Ask the
        tag first and fall back to the name as a token.
        """
        if any(str(t).strip().lower() == "gguf" for t in tags):
            return True
        return "gguf" in re.split(r"[-_./\s]+", hit_id.lower())

    #: Qualifiers a requantizer appends *after* the format, to say this is its
    #: second go at the same weights: ``-GGUF-v2``, ``-gguf-imatrix``,
    #: mradermacher's ``-i1-GGUF``.
    _BUILD_QUALIFIER_RE = re.compile(r"[-_.](?:v\d+(?:\.\d+)?|i1|imatrix|imat)$", re.IGNORECASE)

    @classmethod
    def _quant_stem(cls, model_id: str) -> str:
        """A repository name reduced to the model it quantizes."""
        name = model_id.split("/")[-1]
        saw_format = False
        changed = True
        while changed:
            changed = False
            # A build qualifier only counts in the company of a format suffix,
            # on either side of it: "Widget-7B-GGUF-v2" and mradermacher's
            # "Widget-7B-i1-GGUF" are both somebody's second pass at the same
            # weights. A bare "-v0.2" is a different model, and collapsing
            # Mistral-7B-Instruct-v0.2 into -v0.1 would offer one model as a
            # build of the other -- the confusion the exact-match guard exists
            # to prevent.
            match = cls._BUILD_QUALIFIER_RE.search(name)
            if match:
                stripped = name[: match.start()]
                exposes = any(stripped.upper().endswith(s.upper()) for s in cls._QUANT_SUFFIXES)
                if stripped and (exposes or saw_format):
                    name = stripped
                    changed = True
                    continue
            for suffix in cls._QUANT_SUFFIXES:
                if name.upper().endswith(suffix.upper()) and len(name) > len(suffix):
                    name = name[: -len(suffix)]
                    saw_format = True
                    changed = True
        return name.lower().replace("-", "").replace("_", "").replace(".", "")

    def available_quants(self, model_id: str) -> list[str]:
        """Quantization schemes obtainable for this model, this repo included.

        The scheme keys only. :meth:`quant_variants` is the richer answer and
        this is now derived from it, so the two can never disagree about what
        exists.
        """
        return sorted({v.dtype for v in self.quant_variants(model_id)})

    def search_models(self, query: str, limit: int = 40) -> list[dict]:
        """Hub search hits, trimmed, and deliberately unresolved.

        Resolving here would be one network round trip per row per keystroke.
        The caller gets names and popularity and nothing that claims to be a
        shape; whoever wants a shape asks for one model.

        Raises rather than returning ``[]`` when the hub refused us -- "no
        results" and "rate limited" are different answers and the screen has to
        be able to say which.
        """
        if self.offline:
            raise MetadataUnavailable(
                "the resolver is offline; the hub cannot be searched"
            )
        hits = self.client.search(query, limit=limit)
        out: list[dict] = []
        for hit in hits:
            model_id = str(hit.get("id") or hit.get("modelId") or "")
            if not model_id or "mlx" in model_id.lower():
                continue
            tags = [str(t) for t in (hit.get("tags") or ())]
            out.append(
                {
                    "model_id": model_id,
                    "downloads": hit.get("downloads"),
                    "likes": hit.get("likes"),
                    "pipeline_tag": hit.get("pipeline_tag"),
                    "last_modified": hit.get("lastModified"),
                    "gated": hit.get("gated"),
                    "tags": tags[:12],
                    # A guess from the name, offered as one. Cheap, and it is
                    # what lets a list show "-AWQ" and "-GGUF" apart without a
                    # resolve per row.
                    "quant_hint": quant_detect.from_name(model_id),
                    # No shape here. The wire says so rather than leaving the
                    # client to infer it from missing keys.
                    "resolved": False,
                }
            )
        return out

    def quant_variants(self, model_id: str) -> list[QuantVariant]:
        """Every obtainable set of weights, with what it costs and where it is.

        Names are the only signal most quantizers leave, so this is a shortlist
        to offer a person, not a promise that each one loads. That caveat is
        carried on every variant rather than left in this docstring, because it
        has to reach the screen.

        Two things this does that the old scheme-key version could not. It
        keeps the repository each scheme came from, without which a variant is
        not launchable -- a quantization is a different repo, not a flag. And
        when the model id is itself a GGUF repo, it enumerates that repo's own
        files, which is the case Unsloth's entire catalogue consists of and the
        one that otherwise resolves to a single confident ``bf16`` read out of
        a leftover ``config.json``.
        """
        variants: list[QuantVariant] = []
        seen: set[tuple[str, str | None]] = set()

        def add(variant: QuantVariant) -> None:
            key = (variant.repo_id, variant.gguf_file)
            if key not in seen:
                seen.add(key)
                variants.append(variant)

        # Enumerate this repository's own files first. A GGUF repository is the
        # common case in a quantization catalogue and it changes what the repo
        # id itself means, so the "self" entry below has to be built knowing the
        # answer.
        # One header-read budget for this whole call, spent across this
        # repository and every GGUF repo the search below opens. A list so the
        # callee can decrement it. Per-repo budgets would be _MAX_GGUF_REPOS
        # times larger, which is the timeout this exists to prevent.
        budget = [self._MAX_HEADER_PROBES]

        own_files: list[QuantVariant] = []
        if not self.offline:
            own_files = self._gguf_file_variants(model_id, add_to=None, budget=budget)

        try:
            res = self.resolve_full(model_id)
            is_collection = bool(own_files)
            add(
                QuantVariant(
                    dtype=res.shape.dtype,
                    label=res.shape.dtype,
                    repo_id=model_id,
                    source="self",
                    file_bytes=res.weight_bytes,
                    # A repository that ships nothing but .gguf files is not
                    # launchable by its own id, whatever its config.json says.
                    # These repos keep the original config, so torch_dtype reads
                    # "bfloat16" and the repo resolves to a confident bf16 that
                    # is not a set of weights anything here could load. That is
                    # the single most misleading answer this resolver can give
                    # about a quantization catalogue, so it is refused here
                    # rather than left for a launch to discover.
                    launchable=(
                        not is_collection
                        and quant_info(res.shape.dtype).family != "gguf"
                    ),
                    note=(
                        f"this repository ships {len(own_files)} quantizations; "
                        "choose one of them rather than the repository itself"
                        if is_collection
                        else "the repository as it stands"
                    ),
                )
            )
        except ResolverError:
            pass

        for variant in own_files:
            add(variant)

        if self.offline:
            return variants

        stem = model_id.split("/")[-1]
        for suffix in self._QUANT_SUFFIXES:
            if stem.upper().endswith(suffix.upper()):
                stem = stem[: -len(suffix)]
        try:
            hits = self.client.search(stem, limit=60)
        except ResolverError:
            return variants

        needle = self._quant_stem(model_id)
        gguf_repos: list[tuple[int, str]] = []
        for hit in hits:
            hit_id = str(hit.get("id") or hit.get("modelId") or "")
            if not hit_id or hit_id == model_id:
                continue
            lowered = hit_id.lower()
            # Substring matching is too loose to decide what is a variant of
            # what. Searching "Qwen3-30B-A3B" returns Qwen3-30B-A3B-Thinking-2507
            # and -Instruct-2507, whose names contain the stem but which are
            # different models -- offering them as quantizations would present a
            # 21 GB download as a smaller build of the thing you asked for. So
            # the hit has to reduce to exactly the stem once its own quantization
            # suffix is removed.
            if needle and self._quant_stem(hit_id) != needle:
                continue
            if "mlx" in lowered:
                continue  # Apple silicon format; nothing in this cluster loads it
            downloads = hit.get("downloads")
            downloads = int(downloads) if isinstance(downloads, int) else None
            if self._looks_like_gguf_repo(hit_id, hit.get("tags") or ()):
                # Ordered by popularity below rather than by the order the hub
                # happened to return them, so a truncated list keeps the
                # ladders people actually use.
                gguf_repos.append((downloads if downloads is not None else -1, hit_id))
            key = quant_detect.from_name(hit_id)
            if key:
                add(
                    QuantVariant(
                        dtype=key,
                        label=hit_id.split("/")[-1],
                        repo_id=hit_id,
                        source="repo_name",
                        downloads=downloads,
                        launchable=quant_info(key).family != "gguf",
                        note="scheme read from the repository name",
                    )
                )
                continue
            for tag in hit.get("tags", []) or ():
                key = normalize_dtype(str(tag))
                if key and key in BYTES_PER_PARAM:
                    add(
                        QuantVariant(
                            dtype=key,
                            label=hit_id.split("/")[-1],
                            repo_id=hit_id,
                            source="tags",
                            downloads=downloads,
                            launchable=quant_info(key).family != "gguf",
                            note=f"scheme read from the repository tag {tag!r}",
                        )
                    )
                    break

        gguf_repos.sort(key=lambda pair: pair[0], reverse=True)
        for _, repo in gguf_repos[: self._MAX_GGUF_REPOS]:
            self._gguf_file_variants(repo, add_to=add, budget=budget)
        return variants

    def _dtype_from_header(self, repo_id: str, filename: str) -> str | None:
        """The scheme a GGUF declares about itself, or ``None``.

        Costs one ranged read of a few KiB -- the metadata block, never the
        weights -- so it is spent only on files whose name refused to say.
        Every failure degrades to ``None``: an unreadable header is a reason to
        show the file with a caveat, not a reason to hide it.
        """
        if self.offline:
            return None
        try:
            reader = gguf_mod.RangeReader(
                lambda start, length: self.client.read_range(repo_id, filename, start, length)
            )
            return gguf_mod.dominant_file_type(gguf_mod.read_header(reader))
        except Exception:
            return None

    def _gguf_file_variants(
        self, repo_id: str, add_to=None, budget: list[int] | None = None
    ) -> list[QuantVariant]:
        """One variant per quantization in a GGUF repo, sized from the hub.

        Shards are summed, not listed. ``unsloth/Qwen3-30B-A3B-GGUF`` ships its
        BF16 build as ``...-00001-of-00002.gguf`` plus ``...-00002-of-00002.gguf``;
        drawn as two rows they would be two variants that are each too small to
        be the model, and picking either would download half of it.

        Two things a listing must not do, both of which this used to do. It must
        not offer files that are not weights -- a repository's vision projector
        and its importance matrix live in the same folder under the same
        extension. And it must not drop a real quantization because its name is
        unfashionable: a ``.gguf`` says what it is in its own header, so a name
        that parses to nothing is a reason to read the file, not to pretend it
        is absent.

        ``budget`` is a one-element list of remaining header reads, shared
        across every repository in one ``quant_variants`` call. ``None`` means
        unlimited, which is what a direct caller gets.
        """
        try:
            info = self.client.model_info(repo_id)
        except ResolverError:
            return []

        grouped: dict[str, dict] = {}
        for filename in info.gguf_files():
            if not gguf_names.is_weight_file(filename):
                continue
            stem = gguf_names.variant_stem(filename)
            family = gguf_names.shard_family(filename)
            entry = grouped.setdefault(
                stem,
                {
                    "dtype": quant_detect.from_name(filename),
                    "token": gguf_names.quant_token(filename),
                    "first": None,
                    "any": filename,
                    "files": [],
                    "bytes": 0,
                    "measured": False,
                    "shards": 0,
                    "expected": family[2] if family else 1,
                },
            )
            entry["shards"] += 1
            entry["files"].append(filename)
            # Shard one carries the header. For a single-file quantization
            # that is the file itself.
            if family is None or family[1] == 1:
                entry["first"] = filename
            size = info.file_sizes.get(filename)
            if isinstance(size, int) and size > 0:
                entry["bytes"] += size
                entry["measured"] = True

        out: list[QuantVariant] = []
        for stem, entry in sorted(grouped.items()):
            first = entry["first"] or entry["any"]
            label = entry["token"]
            if not label:
                label = stem.rsplit("/", 1)[-1]
                if label.lower().endswith(".gguf"):
                    label = label[: -len(".gguf")]

            notes = ["one file inside a GGUF repository; size is measured, not estimated"]
            dtype = entry["dtype"]
            if not dtype:
                # Two different failures, and they must not share a sentence.
                # A spent budget means the header was never opened; saying it
                # "could not be read" would report a measurement that was never
                # attempted.
                spent = budget is not None and budget[0] <= 0
                if not spent:
                    if budget is not None:
                        budget[0] -= 1
                    dtype = self._dtype_from_header(repo_id, first)
                if dtype:
                    notes.append("scheme read from the file's own header, not its name")
                elif spent:
                    dtype = DEFAULT_DTYPE
                    notes.append(
                        "the name says nothing and this enumeration had already "
                        "spent its header-read budget, so the file was not "
                        f"opened; priced at {DEFAULT_DTYPE}, which over-charges "
                        "rather than under-charges"
                    )
                else:
                    # Charged at the default rather than at a guess. The
                    # measured size is still the honest number beside it.
                    dtype = DEFAULT_DTYPE
                    notes.append(
                        "the name says nothing and the header could not be "
                        f"read; priced at {DEFAULT_DTYPE}, which over-charges "
                        "rather than under-charges"
                    )

            shards, expected = entry["shards"], entry["expected"]
            if expected > 1:
                notes.append(f"{shards} of {expected} shards, summed")
                if shards != expected:
                    notes.append(
                        "the repository is missing part of this set; the size "
                        "below is what is actually published"
                    )

            variant = QuantVariant(
                dtype=dtype,
                label=label,
                repo_id=repo_id,
                source="gguf_file",
                gguf_file=first,
                file_bytes=entry["bytes"] if entry["measured"] else None,
                shard_count=shards,
                shard_files=tuple(sorted(entry["files"])),
                # A single .gguf file is not a launchable target: both serve
                # command templates take a repository path, and no runtime here
                # claims to load llama.cpp's format anyway.
                launchable=False,
                note="; ".join(notes),
            )
            out.append(variant)
            if add_to is not None:
                add_to(variant)
        return out


# ---- helpers -------------------------------------------------------------


def _shard_bytes(info: ModelInfo) -> int | None:
    return info.shard_bytes()


def _count_local_shards(path: Path) -> tuple[int | None, int | None]:
    """Exact parameters and bytes from the safetensors headers in a directory.

    Reading the headers is the whole point: a parameter count computed from
    layer and hidden size is a formula, and formulas are what this component
    exists to replace.
    """
    shards = sorted(path.glob("*.safetensors"))
    if not shards:
        return None, None
    params = 0
    nbytes = 0
    for shard in shards:
        try:
            with open(shard, "rb") as handle:
                prefix = handle.read(8)
                header_len = int.from_bytes(prefix, "little")
                if header_len <= 0 or header_len > 200 * 1024 * 1024:
                    return None, None
                blob = prefix + handle.read(header_len)
            header = safetensors_header(blob)
        except (OSError, MetadataUnavailable):
            return None, None
        shard_params, shard_bytes = count_safetensors_params(header)
        params += shard_params
        nbytes += shard_bytes
    return (params or None), (nbytes or None)


def _weight_byte_warnings(
    total_params: int, dtype: str, weight_bytes: int | None
) -> list[str]:
    if not weight_bytes or not total_params:
        return []
    expected = total_params * bytes_per_param(dtype)
    if expected <= 0:
        return []
    ratio = weight_bytes / expected
    if ratio > 1.03:
        return [
            f"weights occupy {weight_bytes / 1024**3:.1f} GiB on disk against the "
            f"{expected / 1024**3:.1f} GiB that {total_params / 1e9:.1f}B parameters at "
            f"{dtype} implies, because the repo is mixed precision; charge the "
            "measured figure, which the resolution carries as weight_bytes"
        ]
    return []


def _shape_from(model_id: str, mapped: Mapped, accounting, dtype: str) -> ModelShape:
    return ModelShape(
        model_id=model_id,
        num_layers=mapped.num_layers,
        hidden_size=mapped.hidden_size,
        num_attention_heads=mapped.num_attention_heads,
        num_kv_heads=mapped.num_kv_heads,
        vocab_size=mapped.vocab_size,
        total_params=accounting.total_params,
        dtype=dtype,
        head_dim=mapped.head_dim,
        num_experts=mapped.num_experts,
        num_experts_per_token=mapped.num_experts_per_token,
        active_params=accounting.active_params,
        sliding_window=mapped.sliding_window,
        layers_with_full_attention=mapped.layers_with_full_attention,
        mla_latent_dim=mapped.mla_latent_dim,
        mla_rope_dim=mapped.qk_rope_head_dim,
        vision_params=accounting.vision_params,
    )


def _resolution_from_gguf(
    header: gguf_mod.GGUFHeader, name: str, dtype_override: str | None
) -> Resolution:
    """Build a shape from a GGUF header, measuring bytes tensor by tensor."""
    warnings: list[str] = []
    layers = int(header.get("block_count") or 0)
    hidden = int(header.get("embedding_length") or 0)
    heads = int(header.get("attention.head_count") or 0)
    if not (layers and hidden and heads):
        raise UnsupportedArchitecture(
            f"GGUF header for {name!r} is missing block_count, embedding_length "
            "or attention.head_count"
        )

    kv_heads = header.get("attention.head_count_kv")
    if kv_heads is None:
        kv_heads = heads
        warnings.append(
            "GGUF header has no attention.head_count_kv; assuming multi-head "
            f"attention with {heads} KV heads (no grouping)"
        )
    kv_heads = int(kv_heads) or heads

    key_length = header.get("attention.key_length")
    head_dim = int(key_length) if key_length else (hidden // heads if heads else None)

    vocab = header.get("vocab_size")
    if not vocab:
        tokens = header.metadata.get("tokenizer.ggml.tokens")
        vocab = len(tokens) if tokens is not None else 0
    vocab = int(vocab or 0)
    if not vocab:
        vocab = 32000
        warnings.append("GGUF header declares no vocabulary size; assumed 32000")

    experts = int(header.get("expert_count") or 0)
    experts_used = int(header.get("expert_used_count") or 0)
    if experts and not experts_used:
        experts_used = 2
        warnings.append(
            f"GGUF header declares {experts} experts but no expert_used_count; "
            "assumed 2"
        )

    window = header.get("attention.sliding_window")
    sliding_window = int(window) if window else None
    layers_full = None
    if sliding_window:
        layers_full = 0
        warnings.append(
            f"GGUF header declares a {sliding_window}-token window but not which "
            "layers use it; charged every layer as windowed"
        )

    total_params = header.total_params
    weight_bytes = header.total_bytes

    dtype = None
    quant_source = QuantSource.GGUF_FILE_TYPE
    if dtype_override:
        dtype = quant_detect.normalize_override(dtype_override)
        quant_source = QuantSource.OVERRIDE
    else:
        dtype = gguf_mod.dominant_file_type(header)
        if dtype is None:
            dtype = quant_detect.from_name(name)
            quant_source = QuantSource.REPO_NAME
        if dtype is None:
            dtype, source, extra = quant_detect.detect({}, name)
            quant_source = source
            warnings.extend(extra)

    measured_bpp = weight_bytes / total_params if total_params else 0.0
    if measured_bpp and abs(measured_bpp - bytes_per_param(dtype)) / measured_bpp > 0.03:
        warnings.append(
            f"the file averages {measured_bpp:.4f} bytes per parameter where {dtype} "
            f"implies {bytes_per_param(dtype):.4f}; GGUF mixes tensor types within a "
            "file, so charge the measured weight_bytes rather than the dtype"
        )

    active = None
    expert_params = header.expert_params()
    if experts and expert_params:
        fraction = experts_used / experts
        active = max(
            int(total_params - header.embedding_params() - expert_params * (1 - fraction)),
            int(total_params * 0.01),
        )

    shape = ModelShape(
        model_id=name,
        num_layers=layers,
        hidden_size=hidden,
        num_attention_heads=heads,
        num_kv_heads=kv_heads,
        vocab_size=vocab,
        total_params=total_params,
        dtype=dtype,
        head_dim=head_dim,
        num_experts=experts,
        num_experts_per_token=experts_used,
        active_params=active,
        sliding_window=sliding_window,
        layers_with_full_attention=layers_full,
        mla_latent_dim=None,
        vision_params=0,
    )

    architecture = header.architecture
    verdict = support.build_verdict((architecture,) if architecture else (), dtype)
    warnings.append(
        "GGUF is llama.cpp's format; neither vLLM nor SGLang loads it reliably, "
        "so this shape is for planning, not for a launch through either runtime"
    )

    return Resolution(
        shape=shape,
        revision="gguf",
        param_source=ParamSource.GGUF_TENSORS,
        quant_source=quant_source,
        support=verdict,
        warnings=warnings,
        weight_bytes=weight_bytes,
        architectures=(architecture,) if architecture else (),
        model_type=architecture,
        max_position_embeddings=int(header.get("context_length") or 0) or None,
        param_breakdown={
            "total": total_params,
            "routed_experts": expert_params,
            "embedding": header.embedding_params(),
        },
        resolved_at=time.time(),
    )
