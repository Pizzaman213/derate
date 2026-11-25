"""Hub popularity fields, the search filters, and GET /api/runtimes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import capacity_api, create_app
from control_plane.resolver import StubResolver as DetailResolver
from control_plane.resolver.hf import HubClient, hub_stats_from
from tests.unit.test_gateway import build_deps


@pytest.fixture(autouse=True)
def _clear_caches():
    capacity_api._search_cache.clear()
    capacity_api._hub_stats_cache.clear()
    yield
    capacity_api._search_cache.clear()
    capacity_api._hub_stats_cache.clear()


# ---- parsing -----------------------------------------------------------------


def test_stats_prefer_card_license_and_type_every_field():
    got = hub_stats_from({
        "downloads": 1200, "likes": 7, "lastModified": "2026-01-02T00:00:00.000Z",
        "cardData": {"license": "apache-2.0"}, "tags": ["license:mit"],
        "gated": "manual", "pipeline_tag": "text-generation",
    })
    assert got == {
        "downloads": 1200, "likes": 7, "last_modified": "2026-01-02T00:00:00.000Z",
        "license": "apache-2.0", "gated": True, "pipeline_tag": "text-generation",
    }


def test_stats_fall_back_to_the_license_tag_and_null_what_is_absent():
    got = hub_stats_from({"tags": ["x", "license:mit"], "gated": False, "likes": True})
    assert got["license"] == "mit"
    assert got["gated"] is False
    assert got["likes"] is None  # a bool is not a count
    assert got["downloads"] is None and got["pipeline_tag"] is None


class _Resp:
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class _Session:
    def __init__(self, data):
        self.headers: dict = {}
        self.data = data
        self.calls: list = []

    def get(self, url, timeout=None, **kw):
        self.calls.append((url, kw.get("params")))
        return _Resp(self.data)


def test_model_info_carries_the_stats():
    s = _Session({"sha": "a" * 40, "siblings": [], "downloads": 5,
                  "cardData": {"license": "llama3"}})
    info = HubClient(endpoint="https://hub", token="", session=s).model_info("o/m")
    assert info.downloads == 5 and info.license == "llama3"


def test_search_sends_author_and_pipeline_tag_only_when_given():
    s = _Session([])
    hub = HubClient(endpoint="https://hub", token="", session=s)
    hub.search("qwen", 5)
    assert "author" not in s.calls[-1][1] and "pipeline_tag" not in s.calls[-1][1]
    hub.search("qwen", 5, author="unsloth", pipeline_tag="text-generation")
    assert s.calls[-1][1]["author"] == "unsloth"
    assert s.calls[-1][1]["pipeline_tag"] == "text-generation"


# ---- routes ------------------------------------------------------------------


class _Searching(DetailResolver):
    def __init__(self):
        super().__init__()
        self.asked: list = []

    def search_models(
        self, query, limit=40, *, author=None, pipeline_tag=None, sort="downloads"
    ):
        self.asked.append((query, author, pipeline_tag))
        self.sorts = getattr(self, "sorts", []) + [sort]
        return [{"model_id": "unsloth/Qwen3-8B-GGUF", "resolved": False}]

    def hub_stats(self, model_id):
        raise RuntimeError("hub down")


@pytest.fixture()
def searching():
    deps = build_deps()
    deps.resolver = _Searching()
    with TestClient(create_app(deps)) as c:
        yield c, deps.resolver


@pytest.mark.parametrize("param", ["author", "task"])
def test_search_rejects_a_malformed_filter(searching, param):
    c, _ = searching
    r = c.get("/api/models/search", params={"q": "x", param: "bad name!"})
    assert r.status_code == 400
    assert param in r.json()["error"]["message"]


def test_search_forwards_filters_and_runs_with_no_query(searching):
    c, resolver = searching
    body = c.get(
        "/api/models/search", params={"author": "unsloth", "task": "text-generation"}
    ).json()
    assert resolver.asked == [("", "unsloth", "text-generation")]
    assert body["sources"]["huggingface"]["ok"] is True
    assert any(r["model_id"] == "unsloth/Qwen3-8B-GGUF" for r in body["results"])
    # A different filter is a different cache entry.
    c.get("/api/models/search", params={"author": "Qwen"})
    assert resolver.asked[-1] == ("", "Qwen", None)


def test_search_maps_sort_to_the_hubs_key(searching):
    c, resolver = searching
    assert c.get("/api/models/search", params={"q": "s", "sort": "trending"}).status_code == 200
    assert resolver.sorts[-1] == "trendingScore"
    c.get("/api/models/search", params={"q": "s", "sort": "updated"})
    assert resolver.sorts[-1] == "lastModified"
    # Same query, different sort: a different cache entry, so it asked again.
    assert len(resolver.sorts) == 2


def test_search_rejects_an_unknown_sort(searching):
    c, _ = searching
    r = c.get("/api/models/search", params={"q": "s", "sort": "x"})
    assert r.status_code == 400
    assert "sort" in r.json()["error"]["message"]


def test_an_empty_query_with_no_filter_browses_the_hub(searching):
    # The Hugging Face view opens on the hub's own top models; a guard that
    # answered "type to search" here left that view with nothing real in it.
    c, resolver = searching
    body = c.get("/api/models/search", params={"sort": "trending"}).json()
    assert resolver.asked[-1] == ("", None, None)
    assert resolver.sorts[-1] == "trendingScore"
    assert body["sources"]["huggingface"]["ok"] is True
    assert any(r["origin"] == "hub" for r in body["results"])



def test_detail_hub_is_null_when_the_hub_fails(searching):
    c, _ = searching
    r = c.get("/api/models/detail", params={"model_id": "Qwen/Qwen3-30B-A3B"})
    assert r.status_code == 200
    assert r.json()["hub"] is None


def test_runtimes_lists_every_runtime(searching, monkeypatch):
    monkeypatch.setattr(capacity_api, "_image_present", lambda image: False)
    c, _ = searching
    rows = {r["name"]: r for r in c.get("/api/runtimes").json()}
    assert set(rows) == {"vllm", "sglang", "tts", "llamacpp"}
    assert rows["llamacpp"]["formats"] == ["gguf"]
    assert rows["vllm"]["formats"] == ["safetensors"]
    assert rows["vllm"]["supports"]["speculative"] is True
    assert rows["sglang"]["supports"]["speculative"] is False
    assert rows["vllm"]["kv_dtypes"] and rows["sglang"]["kv_dtypes"] == []
    assert rows["vllm"]["version"] and rows["tts"]["version"] is None
    assert "env" in rows["sglang"]["engine_options"]
    assert rows["vllm"]["nccl_tunables"]


def test_runtimes_say_which_version_each_architectures_image_runs(searching, monkeypatch):
    """A sweep keys its record by the image the MEASURED machine runs. The
    default image's `version` filed a 3090 (vLLM 0.31) measurement under the
    Spark's 0.28.1; this is what the sweep reads instead, and an image nothing
    has probed is null -- a miss, never the default's version."""
    from types import SimpleNamespace

    from control_plane.deploy.flags import RUNTIMES, image_for_arch
    from control_plane.resolver import support

    monkeypatch.setattr(capacity_api, "_image_present", lambda image: False)
    monkeypatch.setattr(support, "_PROBED", {})
    monkeypatch.setattr(support, "_PROBED_IMAGES", {})
    spec = RUNTIMES["vllm"]
    support.record_probe(SimpleNamespace(
        runtime="vllm", image=image_for_arch(spec, "arm64"), version="0.28.1rc1",
        gguf=False))
    c, _ = searching
    rows = {r["name"]: r for r in c.get("/api/runtimes").json()}
    assert rows["vllm"]["image_versions"] == {"amd64": None, "arm64": "0.28.1rc1"}

    support.record_image_probe(SimpleNamespace(
        runtime="vllm", image=image_for_arch(spec, "amd64"), version="0.31.0",
        gguf=False))
    rows = {r["name"]: r for r in c.get("/api/runtimes").json()}
    assert rows["vllm"]["image_versions"] == {"amd64": "0.31.0", "arm64": "0.28.1rc1"}


def test_vllm_offers_gguf_only_when_its_image_carries_the_plugin(searching, monkeypatch):
    from control_plane.resolver import support
    from control_plane.resolver.imageprobe import ImageProbe

    monkeypatch.setattr(capacity_api, "_image_present", lambda image: False)
    c, _ = searching
    probe = dict(
        runtime="vllm", image="img", image_id="sha256:a", version="0.28.1",
        architectures=frozenset({"Qwen3ForCausalLM"}),
    )
    try:
        support.record_probe(ImageProbe(**probe, gguf=False))
        rows = {r["name"]: r for r in c.get("/api/runtimes").json()}
        assert rows["vllm"]["formats"] == ["safetensors"]
        support.record_probe(ImageProbe(**probe, gguf=True))
        rows = {r["name"]: r for r in c.get("/api/runtimes").json()}
        # Native format first: the Models tab opens on it.
        assert rows["vllm"]["formats"] == ["safetensors", "gguf"]
        assert rows["sglang"]["formats"] == ["safetensors"]
    finally:
        support.clear_probes()


def test_runtimes_carry_the_kv_width_the_gate_charges(searching, monkeypatch):
    monkeypatch.setattr(capacity_api, "_image_present", lambda image: False)
    c, _ = searching
    rows = {r["name"]: r for r in c.get("/api/runtimes").json()}
    widths = rows["vllm"]["kv_dtype_bytes"]
    assert widths["fp8"] == 1
    assert widths["bfloat16"] == 2
    assert "auto" not in widths  # unpriced names are absent, not guessed
    assert set(widths) <= set(rows["vllm"]["kv_dtypes"])
    assert rows["sglang"]["kv_dtype_bytes"] == {}
