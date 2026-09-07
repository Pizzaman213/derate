"""GGUF header reading.

Reads the KV metadata block and the tensor directory, never the weights. Byte
sizes come from summing each tensor's own block-quantized size, so a mixed-quant
file -- which every llama.cpp release produces, since attention and embedding
tensors are left wider than the MLP -- is measured rather than approximated by
multiplying a parameter count by a nominal bit width.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass
from typing import Any, BinaryIO

from .types import MetadataUnavailable

GGUF_MAGIC = b"GGUF"

# Value type tags in the KV block.
_UINT8, _INT8, _UINT16, _INT16, _UINT32, _INT32 = 0, 1, 2, 3, 4, 5
_FLOAT32, _BOOL, _STRING, _ARRAY, _UINT64, _INT64, _FLOAT64 = 6, 7, 8, 9, 10, 11, 12

_SCALAR_FORMAT: dict[int, tuple[str, int]] = {
    _UINT8: ("<B", 1), _INT8: ("<b", 1),
    _UINT16: ("<H", 2), _INT16: ("<h", 2),
    _UINT32: ("<I", 4), _INT32: ("<i", 4),
    _FLOAT32: ("<f", 4), _BOOL: ("<?", 1),
    _UINT64: ("<Q", 8), _INT64: ("<q", 8), _FLOAT64: ("<d", 8),
}

#: ggml tensor type -> (name, elements per block, bytes per block).
GGML_TYPES: dict[int, tuple[str, int, int]] = {
    0: ("F32", 1, 4),
    1: ("F16", 1, 2),
    2: ("Q4_0", 32, 18),
    3: ("Q4_1", 32, 20),
    6: ("Q5_0", 32, 22),
    7: ("Q5_1", 32, 24),
    8: ("Q8_0", 32, 34),
    9: ("Q8_1", 32, 40),
    10: ("Q2_K", 256, 84),
    11: ("Q3_K", 256, 110),
    12: ("Q4_K", 256, 144),
    13: ("Q5_K", 256, 176),
    14: ("Q6_K", 256, 210),
    15: ("Q8_K", 256, 292),
    16: ("IQ2_XXS", 256, 66),
    17: ("IQ2_XS", 256, 74),
    18: ("IQ3_XXS", 256, 98),
    19: ("IQ1_S", 256, 50),
    20: ("IQ4_NL", 32, 18),
    21: ("IQ3_S", 256, 110),
    22: ("IQ2_S", 256, 82),
    23: ("IQ4_XS", 256, 136),
    24: ("I8", 1, 1),
    25: ("I16", 1, 2),
    26: ("I32", 1, 4),
    27: ("I64", 1, 8),
    28: ("F64", 1, 8),
    29: ("IQ1_M", 256, 56),
    30: ("BF16", 1, 2),
    34: ("TQ1_0", 256, 54),
    35: ("TQ2_0", 256, 66),
    39: ("MXFP4", 32, 17),
}

#: general.file_type -> a key in BYTES_PER_PARAM. The enum is llama.cpp's
#: LLAMA_FTYPE; only the values that name a storage format appear here.
GGUF_FILE_TYPES: dict[int, str] = {
    0: "fp32", 1: "fp16",
    # 3, 8 and 9 are Q4_1, Q5_0 and Q5_1. All three used to answer "q4_0",
    # which charges 4.5 bpw for formats that cost 5.0, 5.5 and 6.0 -- a third
    # under on the worst of them, and under is the direction that turns into an
    # out-of-memory kill minutes into a load rather than a refusal before it.
    2: "q4_0", 3: "q4_1", 7: "q8_0", 8: "q5_0", 9: "q5_1",
    10: "q2_k", 11: "q3_k_m", 12: "q3_k_m", 13: "q3_k_m",
    14: "q4_k_m", 15: "q4_k_m", 16: "q5_k_m", 17: "q5_k_m",
    18: "q6_k",
    # 19 and 20 are IQ2_XXS and IQ2_XS, not two more spellings of Q2_K; 21 is
    # Q2_K_S. Everything from 22 to 31 is the rest of the importance-matrix
    # family and was absent entirely, so those files fell through to
    # dominant_file_type() -> None and were then charged at the bf16 default --
    # four to eight times their real footprint, which is what made every
    # Unsloth GGUF repo look like it would not fit.
    19: "iq2_xxs", 20: "iq2_xs", 21: "q2_k_s",
    22: "iq3_xs", 23: "iq3_xxs", 24: "iq1_s", 25: "iq4_nl",
    26: "iq3_s", 27: "iq3_m", 28: "iq2_s", 29: "iq2_m",
    30: "iq4_xs", 31: "iq1_m",
    32: "bf16", 38: "mxfp4", 39: "nvfp4",
}


class _Reader:
    """Sequential reader over a local file or an HTTP byte range source."""

    def read(self, n: int) -> bytes:  # pragma: no cover - interface
        raise NotImplementedError

    @property
    def offset(self) -> int:  # pragma: no cover - interface
        raise NotImplementedError

    def skip(self, n: int) -> None:
        remaining = n
        while remaining > 0:
            remaining -= len(self.read(min(remaining, 1 << 20)))


class FileReader(_Reader):
    def __init__(self, handle: BinaryIO) -> None:
        self._handle = handle

    def read(self, n: int) -> bytes:
        data = self._handle.read(n)
        if len(data) != n:
            raise MetadataUnavailable("GGUF file ended inside the header")
        return data

    @property
    def offset(self) -> int:
        return self._handle.tell()

    def skip(self, n: int) -> None:
        self._handle.seek(n, os.SEEK_CUR)


class RangeReader(_Reader):
    """Reads a remote file forward in chunks, using HTTP Range requests."""

    def __init__(self, fetch, chunk_size: int = 4 << 20) -> None:
        self._fetch = fetch  # (start, length) -> bytes
        self._chunk = chunk_size
        self._buffer = b""
        self._buffer_start = 0
        self._pos = 0

    def read(self, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            local = self._pos - self._buffer_start
            available = len(self._buffer) - local
            if available <= 0:
                self._buffer_start = self._pos
                self._buffer = self._fetch(self._pos, max(self._chunk, n))
                if not self._buffer:
                    raise MetadataUnavailable("GGUF range request returned no data")
                local, available = 0, len(self._buffer)
            take = min(n, available)
            out += self._buffer[local : local + take]
            self._pos += take
            n -= take
        return bytes(out)

    @property
    def offset(self) -> int:
        return self._pos

    def skip(self, n: int) -> None:
        self._pos += n


@dataclass
class GGUFTensor:
    name: str
    dims: tuple[int, ...]
    ggml_type: int

    @property
    def elements(self) -> int:
        count = 1
        for d in self.dims:
            count *= d
        return count

    @property
    def type_name(self) -> str:
        return GGML_TYPES.get(self.ggml_type, (f"TYPE_{self.ggml_type}", 1, 4))[0]

    @property
    def nbytes(self) -> int:
        _, block, size = GGML_TYPES.get(self.ggml_type, ("UNKNOWN", 1, 4))
        return (self.elements // block) * size if block else 0


@dataclass
class GGUFHeader:
    version: int
    tensor_count: int
    metadata: dict[str, Any]
    tensors: list[GGUFTensor]

    @property
    def architecture(self) -> str:
        return str(self.metadata.get("general.architecture", ""))

    def get(self, key: str, default: Any = None) -> Any:
        """Look a key up with and without the architecture prefix."""
        if key in self.metadata:
            return self.metadata[key]
        arch = self.architecture
        if arch and f"{arch}.{key}" in self.metadata:
            return self.metadata[f"{arch}.{key}"]
        suffix = "." + key
        for name, value in self.metadata.items():
            if name.endswith(suffix):
                return value
        return default

    @property
    def total_params(self) -> int:
        return sum(t.elements for t in self.tensors)

    @property
    def total_bytes(self) -> int:
        return sum(t.nbytes for t in self.tensors)

    def expert_params(self) -> int:
        """Parameters in routed expert tensors, by name."""
        return sum(t.elements for t in self.tensors if _is_expert_tensor(t.name))

    def embedding_params(self) -> int:
        return sum(
            t.elements
            for t in self.tensors
            if t.name in ("token_embd.weight", "tok_embeddings.weight")
        )


def _is_expert_tensor(name: str) -> bool:
    lowered = name.lower()
    return any(
        tag in lowered
        for tag in ("ffn_gate_exps", "ffn_up_exps", "ffn_down_exps", ".experts.", "_exps.")
    ) and "shexp" not in lowered


def _read_string(reader: _Reader) -> str:
    (length,) = struct.unpack("<Q", reader.read(8))
    if length > 64 << 20:
        raise MetadataUnavailable(f"implausible GGUF string length {length}")
    return reader.read(length).decode("utf-8", errors="replace")


def _read_value(reader: _Reader, value_type: int, *, materialize: bool = True) -> Any:
    if value_type in _SCALAR_FORMAT:
        fmt, size = _SCALAR_FORMAT[value_type]
        return struct.unpack(fmt, reader.read(size))[0]
    if value_type == _STRING:
        return _read_string(reader)
    if value_type == _ARRAY:
        (item_type,) = struct.unpack("<I", reader.read(4))
        (count,) = struct.unpack("<Q", reader.read(8))
        # Token vocabularies run to hundreds of thousands of strings. We need
        # their length, not their contents, so walk past them.
        keep = materialize and count <= 4096
        items: list[Any] = []
        if item_type in _SCALAR_FORMAT and not keep:
            reader.skip(_SCALAR_FORMAT[item_type][1] * count)
            return _ArrayStub(count, item_type)
        for _ in range(count):
            value = _read_value(reader, item_type, materialize=keep)
            if keep:
                items.append(value)
        return items if keep else _ArrayStub(count, item_type)
    raise MetadataUnavailable(f"unknown GGUF value type {value_type}")


@dataclass
class _ArrayStub:
    """A large array we walked past. Its length is what we needed."""

    length: int
    item_type: int

    def __len__(self) -> int:
        return self.length


def read_header(reader: _Reader, *, max_tensors: int = 2_000_000) -> GGUFHeader:
    magic = reader.read(4)
    if magic != GGUF_MAGIC:
        raise MetadataUnavailable(f"not a GGUF file (magic {magic!r})")
    (version,) = struct.unpack("<I", reader.read(4))
    if version not in (2, 3):
        raise MetadataUnavailable(f"unsupported GGUF version {version}")
    tensor_count, kv_count = struct.unpack("<QQ", reader.read(16))
    if tensor_count > max_tensors:
        raise MetadataUnavailable(f"implausible GGUF tensor count {tensor_count}")

    metadata: dict[str, Any] = {}
    for _ in range(kv_count):
        key = _read_string(reader)
        (value_type,) = struct.unpack("<I", reader.read(4))
        metadata[key] = _read_value(reader, value_type)

    tensors: list[GGUFTensor] = []
    for _ in range(tensor_count):
        name = _read_string(reader)
        (n_dims,) = struct.unpack("<I", reader.read(4))
        dims = struct.unpack(f"<{n_dims}Q", reader.read(8 * n_dims))
        ggml_type, _offset = struct.unpack("<IQ", reader.read(12))
        tensors.append(GGUFTensor(name=name, dims=dims, ggml_type=ggml_type))

    return GGUFHeader(
        version=version, tensor_count=tensor_count, metadata=metadata, tensors=tensors
    )


def read_gguf_file(path: str) -> GGUFHeader:
    with open(path, "rb") as handle:
        return read_header(FileReader(handle))


def dominant_file_type(header: GGUFHeader) -> str | None:
    """The dtype key for this file, from its declared file type."""
    ftype = header.get("general.file_type")
    if isinstance(ftype, int):
        return GGUF_FILE_TYPES.get(ftype)
    return None
