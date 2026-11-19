"""R1: a hostile GGUF header or range server cannot make the resolver read,
fetch or allocate without bound."""

from __future__ import annotations

import io
import struct

import pytest

from control_plane.resolver import gguf
from control_plane.resolver.gguf import (
    GGML_MAX_DIMS,
    MAX_ARRAY_ITEMS,
    MAX_KV_COUNT,
    FileReader,
    RangeReader,
    read_header,
)
from control_plane.resolver.hf import HubClient
from control_plane.resolver.types import MetadataUnavailable

_STRING, _ARRAY, _UINT32 = 8, 9, 4


def _s(text: str) -> bytes:
    raw = text.encode()
    return struct.pack("<Q", len(raw)) + raw


def _header(tensor_count: int, kv_count: int) -> bytes:
    return b"GGUF" + struct.pack("<IQQ", 3, tensor_count, kv_count)


def _read(blob: bytes):
    return read_header(FileReader(io.BytesIO(blob)))


def test_kv_count_cap():
    with pytest.raises(MetadataUnavailable, match="metadata count"):
        _read(_header(0, MAX_KV_COUNT + 1))


def test_tensor_rank_cap():
    blob = _header(1, 0) + _s("w") + struct.pack("<I", GGML_MAX_DIMS + 1)
    with pytest.raises(MetadataUnavailable, match="tensor rank"):
        _read(blob)


def test_rank_at_cap_still_reads():
    blob = (
        _header(1, 0) + _s("w") + struct.pack("<I", GGML_MAX_DIMS)
        + struct.pack(f"<{GGML_MAX_DIMS}Q", 1, 2, 3, 4) + struct.pack("<IQ", 0, 0)
    )
    header = _read(blob)
    assert header.tensors[0].dims == (1, 2, 3, 4)


def test_array_count_cap():
    blob = _header(0, 1) + _s("tokens") + struct.pack("<I", _ARRAY) + struct.pack(
        "<IQ", _STRING, MAX_ARRAY_ITEMS + 1
    )
    with pytest.raises(MetadataUnavailable, match="array length"):
        _read(blob)


def test_byte_budget(monkeypatch):
    # A scalar array under the item cap but past the budget is walked by
    # skip(), which is charged like a read.
    monkeypatch.setattr(gguf, "HEADER_BYTE_BUDGET", 1 << 20)
    blob = _header(0, 1) + _s("big") + struct.pack("<I", _ARRAY) + struct.pack(
        "<IQ", _UINT32, (2 << 20) // 4
    )
    with pytest.raises(MetadataUnavailable, match="exceeds 256 MiB"):
        _read(blob)


def test_range_reader_clamps_each_fetch():
    asked: list[int] = []

    def fetch(start: int, length: int) -> bytes:
        asked.append(length)
        return b"\0" * length

    reader = RangeReader(fetch, chunk_size=4 << 20)
    reader.read(100 << 20)
    assert asked and max(asked) <= gguf.RANGE_FETCH_MAX


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = io.BytesIO(body)

    def read(self, n: int, decode_content: bool = True) -> bytes:
        return self._body.read(n)


class _Resp:
    status_code = 206

    def __init__(self, body: bytes) -> None:
        self.raw = _Raw(body)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _Session:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.kwargs: dict = {}
        self.resp: _Resp | None = None

    def get(self, url, **kwargs):
        self.kwargs = kwargs
        self.resp = _Resp(self.body)
        return self.resp


def test_read_range_returns_exactly_length_from_a_longer_body():
    # A server that ignores Range and sends the whole file.
    session = _Session(b"x" * 10_000)
    client = HubClient()
    client.session = session
    out = client.read_range("org/repo", "m.gguf", 0, 100)
    assert out == b"x" * 100
    assert session.kwargs.get("stream") is True
    assert session.resp is not None and session.resp.closed


def test_read_range_refuses_more_than_64_mib():
    client = HubClient()
    client.session = _Session(b"")
    with pytest.raises(ValueError):
        client.read_range("org/repo", "m.gguf", 0, (64 << 20) + 1)


def test_read_range_never_inflates_a_compressed_body():
    """R-5: a gzip bomb answered to a Range GET is read as the bytes it is."""
    seen: dict = {}

    class _Recording(_Raw):
        def read(self, n: int, decode_content: bool = True) -> bytes:
            seen["decode_content"] = decode_content
            return super().read(n)

    session = _Session(b"x" * 100)
    client = HubClient()
    client.session = session
    original = session.get

    def get(url, **kwargs):
        resp = original(url, **kwargs)
        resp.raw = _Recording(b"x" * 100)
        return resp

    session.get = get
    client.read_range("org/repo", "m.gguf", 0, 10)
    assert seen["decode_content"] is False
    assert session.kwargs["headers"]["Accept-Encoding"] == "identity"
