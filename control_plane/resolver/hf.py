"""HuggingFace hub access: metadata only, never weights.

Everything here is a plain HTTP GET against the hub API or a ``resolve`` URL.
We deliberately avoid ``transformers`` and avoid downloading shards: resolution
sits in a UI request path and has a two second cold budget.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field as dataclass_field
from typing import Any
from urllib.parse import quote

import requests

from .types import MetadataUnavailable, ModelNotFound

DEFAULT_ENDPOINT = "https://huggingface.co"
DEFAULT_TIMEOUT = float(os.environ.get("SPARKPLANE_HF_TIMEOUT", "8.0"))
_USER_AGENT = "sparkplane-resolver/1.0"


def hf_token() -> str | None:
    for var in ("SPARKPLANE_HF_TOKEN", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"):
        value = os.environ.get(var)
        if value:
            return value.strip()
    return None


def hf_endpoint() -> str:
    return os.environ.get("HF_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


@dataclass
class ModelInfo:
    """The subset of the hub's model record we act on."""

    model_id: str
    sha: str | None
    siblings: tuple[str, ...]
    safetensors: dict[str, Any] | None
    gguf: dict[str, Any] | None
    tags: tuple[str, ...]
    #: File name to size in bytes, when the hub reported blob sizes.
    file_sizes: dict[str, int] = dataclass_field(default_factory=dict)

    def has_file(self, name: str) -> bool:
        return name in self.siblings

    def shard_bytes(self) -> int | None:
        """On-disk bytes of the shards a runtime actually loads.

        Repos carry duplicate copies of the same weights: GPT-OSS ships an
        ``original/`` tree, Mistral ships ``consolidated.safetensors`` beside
        the sharded files. Counting either doubles the weight footprint, so
        only root-level files following the ``model*.safetensors`` convention
        count, with everything in the root as the fallback.
        """
        root = {
            name: size
            for name, size in self.file_sizes.items()
            if name.endswith(".safetensors") and "/" not in name
        }
        canonical = {n: s for n, s in root.items() if n.startswith("model")}
        total = sum((canonical or root).values())
        return total or None

    def gguf_files(self) -> list[str]:
        return [f for f in self.siblings if f.lower().endswith(".gguf")]

    def safetensors_total(self) -> int | None:
        """Total parameters as the hub counted them from the shard headers."""
        if not self.safetensors:
            return None
        total = self.safetensors.get("total")
        if isinstance(total, int) and total > 0:
            return total
        params = self.safetensors.get("parameters")
        if isinstance(params, dict) and params:
            summed = sum(v for v in params.values() if isinstance(v, int))
            return summed or None
        return None


class HubClient:
    """Thin, timeout-bounded hub client. One session, kept warm."""

    def __init__(
        self,
        endpoint: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.endpoint = (endpoint or hf_endpoint()).rstrip("/")
        self.token = token if token is not None else hf_token()
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": _USER_AGENT})
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    # ---- low level -----------------------------------------------------

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        try:
            return self.session.get(url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:  # network down, DNS, TLS
            raise MetadataUnavailable(f"hub request failed: {url}: {exc}") from exc

    def _raise_for(self, resp: requests.Response, model_id: str, what: str) -> None:
        if resp.status_code == 404:
            raise ModelNotFound(f"{what} not found for {model_id!r}")
        if resp.status_code in (401, 403):
            # The hub answers 401 for a repo that does not exist as well as for
            # one you may not read, so say both rather than send someone hunting
            # for a token when they have a typo.
            raise MetadataUnavailable(
                f"cannot read {what} for {model_id!r} (HTTP {resp.status_code}): the "
                "repo does not exist, or it is gated or private. Check the id; if it "
                "is right, set HF_TOKEN to a token that has accepted the licence."
            )
        if resp.status_code == 429:
            raise MetadataUnavailable(f"hub rate limited while fetching {what} for {model_id!r}")
        if resp.status_code >= 400:
            raise MetadataUnavailable(
                f"hub returned HTTP {resp.status_code} for {what} of {model_id!r}"
            )

    # ---- metadata ------------------------------------------------------

    def model_info(self, model_id: str, revision: str = "main") -> ModelInfo:
        url = f"{self.endpoint}/api/models/{quote(model_id, safe='/')}"
        params = {"blobs": "true"}  # blob sizes give us real weight bytes
        if revision and revision != "main":
            url = f"{url}/revision/{quote(revision, safe='')}"
        resp = self._get(url, params=params)
        self._raise_for(resp, model_id, "model info")
        try:
            data = resp.json()
        except ValueError as exc:
            raise MetadataUnavailable(f"model info for {model_id!r} was not JSON") from exc
        raw_siblings = [s for s in data.get("siblings", []) if isinstance(s, dict)]
        siblings = tuple(s.get("rfilename", "") for s in raw_siblings)
        file_sizes = {
            s["rfilename"]: int(s["size"])
            for s in raw_siblings
            if s.get("rfilename") and isinstance(s.get("size"), int)
        }
        return ModelInfo(
            model_id=model_id,
            sha=data.get("sha"),
            siblings=siblings,
            safetensors=data.get("safetensors"),
            gguf=data.get("gguf"),
            tags=tuple(data.get("tags", []) or ()),
            file_sizes=file_sizes,
        )

    def file_json(
        self, model_id: str, filename: str, revision: str = "main"
    ) -> dict[str, Any] | None:
        """Fetch a JSON file from a repo. ``None`` when it simply is not there."""
        url = (
            f"{self.endpoint}/{quote(model_id, safe='/')}"
            f"/resolve/{quote(revision, safe='')}/{quote(filename, safe='/')}"
        )
        resp = self._get(url, allow_redirects=True)
        if resp.status_code == 404:
            return None
        self._raise_for(resp, model_id, filename)
        try:
            return resp.json()
        except ValueError as exc:
            raise MetadataUnavailable(f"{filename} of {model_id!r} was not JSON") from exc

    def config(self, model_id: str, revision: str = "main") -> dict[str, Any]:
        config = self.file_json(model_id, "config.json", revision=revision)
        if config is None:
            raise ModelNotFound(
                f"{model_id!r} has no config.json. If this is a GGUF repo, "
                "resolve the .gguf file directly."
            )
        return config

    def read_range(
        self, model_id: str, filename: str, start: int, length: int, revision: str = "main"
    ) -> bytes:
        """Byte range from a repo file. Used to read headers, never weights."""
        url = (
            f"{self.endpoint}/{quote(model_id, safe='/')}"
            f"/resolve/{quote(revision, safe='')}/{quote(filename, safe='/')}"
        )
        headers = {"Range": f"bytes={start}-{start + length - 1}"}
        resp = self._get(url, headers=headers, allow_redirects=True)
        self._raise_for(resp, model_id, filename)
        return resp.content

    def search(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        url = f"{self.endpoint}/api/models"
        resp = self._get(
            url,
            params={
                "search": query,
                "limit": str(limit),
                "sort": "downloads",
                "direction": "-1",
            },
        )
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return data if isinstance(data, list) else []


def safetensors_header(blob: bytes) -> dict[str, Any]:
    """Parse a safetensors header from the first bytes of a shard.

    Layout: 8-byte little-endian header length, then that many bytes of JSON
    mapping tensor name to ``{dtype, shape, data_offsets}``.
    """
    if len(blob) < 8:
        raise MetadataUnavailable("safetensors header truncated")
    header_len = int.from_bytes(blob[:8], "little")
    if header_len <= 0 or header_len > 200 * 1024 * 1024:
        raise MetadataUnavailable(f"implausible safetensors header length {header_len}")
    if len(blob) < 8 + header_len:
        raise MetadataUnavailable("safetensors header longer than the fetched range")
    try:
        return json.loads(blob[8 : 8 + header_len])
    except ValueError as exc:
        raise MetadataUnavailable("safetensors header was not JSON") from exc


#: Tensor name suffixes that carry scales or packing side-data rather than
#: weights. Counting them as parameters inflates the total.
_NON_PARAMETER_SUFFIXES = (
    "_scales", ".scales", "scale_inv", ".weight_scale", ".input_scale",
    ".qzeros", ".g_idx", ".weight_shape", ".bias_scale",
)


def count_safetensors_params(header: dict[str, Any]) -> tuple[int, int]:
    """Logical parameters and stored bytes from one shard's header.

    Packed formats store several weights per element -- MXFP4 puts two 4-bit
    values in a byte, GPTQ and AWQ put eight in an int32 -- so element counts
    are unpacked back into weights. Scale and zero-point tensors are storage,
    not parameters, and are counted in the bytes but not in the total.
    """
    params = 0
    nbytes = 0
    for name, entry in header.items():
        if name == "__metadata__" or not isinstance(entry, dict):
            continue
        shape = entry.get("shape") or []
        dtype = str(entry.get("dtype", ""))
        elements = 1
        for dim in shape:
            elements *= int(dim)
        offsets = entry.get("data_offsets") or (0, 0)
        try:
            nbytes += int(offsets[1]) - int(offsets[0])
        except (IndexError, TypeError, ValueError):
            nbytes += int(elements * SAFETENSORS_ELEMENT_BYTES.get(dtype, 2))
        if any(name.endswith(suffix) or suffix in name for suffix in _NON_PARAMETER_SUFFIXES):
            continue
        if dtype == "U8" and name.endswith("_blocks"):
            params += elements * 2  # two 4-bit values per byte
        elif dtype in ("I32", "U32") and (name.endswith("qweight") or ".qweight" in name):
            params += elements * 8  # eight 4-bit values per int32
        else:
            params += elements
    return params, nbytes


#: Bytes per element for the dtype strings safetensors headers use.
SAFETENSORS_ELEMENT_BYTES: dict[str, float] = {
    "F64": 8, "I64": 8, "U64": 8,
    "F32": 4, "I32": 4, "U32": 4,
    "F16": 2, "BF16": 2, "I16": 2, "U16": 2,
    "F8_E4M3": 1, "F8_E5M2": 1, "I8": 1, "U8": 1, "BOOL": 1,
    "F4": 0.5, "U4": 0.5, "I4": 0.5,
}
