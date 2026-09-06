"""The resolver: a HuggingFace model id in, a complete ModelShape out.

Everything downstream reads these numbers. Agent D's memory arithmetic and
Agent E's parallelism choice are both wrong if the KV head count, the active
parameter count or the quantization is wrong here, so every field is either
read from real metadata or accompanied by a warning saying it was not.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from control_plane.contracts import ModelShape, NodeProfile
from control_plane.contracts.quant import BYTES_PER_PARAM, bytes_per_param, normalize_dtype

from . import gguf as gguf_mod
from . import quant_detect, support
from .cache import ShapeCache
from .config_map import Mapped, map_config, vision_config
from .hf import HubClient, ModelInfo, count_safetensors_params, safetensors_header
from .params import ParamBreakdown, analytic_breakdown, reconcile
from .types import (
    MetadataUnavailable,
    ModelNotFound,
    ParamSource,
    QuantSource,
    Resolution,
    ResolverError,
    SupportVerdict,
    UnsupportedArchitecture,
)

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
    ) -> None:
        self.client = client or HubClient()
        self.cache = cache if cache is not None else ShapeCache()
        self.offline = offline or os.environ.get("SPARKPLANE_OFFLINE") == "1"

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
        if local.suffix.lower() == ".gguf" or model_id.startswith("hf://"):
            return self.resolve_gguf_full(model_id, dtype=dtype)
        if local.is_dir() and (local / "config.json").is_file():
            return self._resolve_local_dir(local, dtype, started)

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
            model_id, info, mapped, breakdown, dtype, revision, measured_total
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
        revision: str,
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
            warnings.append(
                f"the hub counts {tally / 1e9:.1f}B parameters where the config "
                f"describes {analytic / 1e9:.1f}B; that tally counts packed storage "
                "elements on some 4-bit formats, so the config figure was used"
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

    def resolve_gguf(self, path: str) -> ModelShape:
        return self.resolve_gguf_full(path).shape

    def resolve_gguf_full(self, path: str, dtype: str | None = None) -> Resolution:
        """Read a GGUF header, local or on the hub as ``hf://repo/file.gguf``."""
        started = time.perf_counter()
        name = path
        if path.startswith("hf://"):
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

    def available_quants(self, model_id: str) -> list[str]:
        """Quantization schemes obtainable for this model, this repo included.

        Searches the hub for sibling repos of the same model. Names are the only
        signal most quantizers leave, so this is a shortlist to offer a user,
        not a promise that each one loads.
        """
        found: set[str] = set()
        try:
            res = self.resolve_full(model_id)
            found.add(res.shape.dtype)
        except ResolverError:
            pass

        if self.offline:
            return sorted(found)

        base = model_id.split("/")[-1]
        stem = base
        for suffix in ("-GGUF", "-AWQ", "-GPTQ", "-FP8", "-FP4", "-bnb-4bit", "-INT4", "-INT8"):
            if stem.upper().endswith(suffix.upper()):
                stem = stem[: -len(suffix)]
        try:
            hits = self.client.search(stem, limit=60)
        except ResolverError:
            return sorted(found)

        needle = stem.lower().replace("-", "").replace("_", "")
        gguf_repos: list[str] = []
        for hit in hits:
            hit_id = str(hit.get("id") or hit.get("modelId") or "")
            lowered = hit_id.lower()
            flattened = hit_id.split("/")[-1].lower().replace("-", "").replace("_", "")
            if needle and needle not in flattened:
                continue
            if "mlx" in lowered:
                continue  # Apple silicon format; nothing in this cluster loads it
            if lowered.endswith("gguf") and len(gguf_repos) < 2:
                gguf_repos.append(hit_id)
            key = quant_detect.from_name(hit_id)
            if key:
                found.add(key)
            for tag in hit.get("tags", []) or ():
                key = normalize_dtype(str(tag))
                if key and key in BYTES_PER_PARAM:
                    found.add(key)

        # A GGUF repo holds one file per quantization, so the schemes are in the
        # file names rather than the repo name. Two repos is enough to enumerate
        # the usual ladder without turning this into a crawl.
        for repo in gguf_repos:
            try:
                info = self.client.model_info(repo)
            except ResolverError:
                continue
            for filename in info.gguf_files():
                key = quant_detect.from_name(filename)
                if key:
                    found.add(key)
        return sorted(found)


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
