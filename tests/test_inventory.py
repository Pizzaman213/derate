"""The model registry: the merge, and the ways it could quietly lie.

Every test here names the class of bug it exists to catch. The registry is a
materialized view of five sources, and the failures worth pinning are not
"does SQLite work" but the ones that would leave a screen looking full while
saying something false: a source silently dropping out of the merge, an
unreachable node reading as an empty disk, a fit verdict appearing for a
question nobody asked.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from control_plane.inventory import ModelInventory
from control_plane.inventory.db import SCHEMA_VERSION


# -- fixtures ---------------------------------------------------------------


@dataclass
class FakeShape:
    model_id: str


@dataclass
class FakePlan:
    node_ids: list[str] = field(default_factory=list)


@dataclass
class FakeDeployment:
    deployment_id: str
    served_name: str
    shape: FakeShape
    state: str = "ready"
    runtime: str = "vllm"
    plan: FakePlan = field(default_factory=FakePlan)
    last_error: str | None = None


@dataclass
class FakeCurated:
    model_id: str
    label: str
    detail: str = ""
    default_context: int | None = None
    default_concurrency: int | None = None


def provider_fact(provider_id="openrouter", **kw):
    base = {
        "provider_id": provider_id,
        "display_name": "OpenRouter",
        "healthy": True,
        "enabled": True,
        "admitting": True,
        "admission_block": None,
        "last_error": None,
    }
    base.update(kw)
    return base


def catalogue_row(upstream_id, *, enabled=True, served_name=None, **kw):
    base = {
        "upstream_id": upstream_id,
        "served_name": served_name or upstream_id,
        "enabled": enabled,
        "context_length": 8192,
        "input_cost_per_mtok": None,
        "output_cost_per_mtok": None,
        "supports_tools": False,
        "supports_streaming": True,
        "modality": "text",
    }
    base.update(kw)
    return base


def cache_report(nodes, measured_at=100.0):
    return {"measured_at": measured_at, "nodes": nodes}


def node_ok(node_id, repos):
    return {
        "node_id": node_id,
        "models": {"available": True, "reason": None, "repos": repos},
    }


def node_down(node_id, reason):
    return {"node_id": node_id, "models": {"available": False, "reason": reason}}


def repo(repo_id, *, size=1000, blob_count=8):
    return {
        "repo_id": repo_id,
        "folder": "models--" + repo_id.replace("/", "--"),
        "bytes": size,
        "blob_count": blob_count,
    }


@pytest.fixture
def inv(tmp_path):
    return ModelInventory(tmp_path / "models.db")


def by_id(inventory):
    return {m.model_id: m for m in inventory.list_models()}


# -- the merge --------------------------------------------------------------


def test_one_model_from_three_sources_is_one_row_carrying_all_three(inv):
    """The whole point. A model that is curated AND running AND on disk is one
    row that says so -- not three rows each with a third of the story, which
    is what six independent browser-side builders produced before they were
    folded here."""
    inv.refresh_fast(
        deployments=[FakeDeployment("d-1", "gpt-oss", FakeShape("openai/gpt-oss-120b"))],
        curated=[FakeCurated("openai/gpt-oss-120b", "gpt-oss-120b", detail="MoE")],
    )
    inv.refresh_cache(cache_report([node_ok("spark-01", [repo("openai/gpt-oss-120b")])]))

    rows = inv.list_models()
    assert len(rows) == 1
    row = rows[0]
    assert row.facets == ["running", "ondisk", "catalog"]
    assert row.label == "gpt-oss-120b"
    assert row.detail == "MoE"
    assert row.served_names == ["gpt-oss"]
    assert row.cached_on == ["spark-01"]


def test_a_model_id_is_never_case_folded(inv):
    """OpenRouter spells a model `qwen/qwen3-30b-a3b` where the hub spells it
    `Qwen/Qwen3-30B-A3B`. Folding them would hand an un-served remote row a
    local verdict for weights that are not the same thing."""
    inv.refresh_fast(
        curated=[FakeCurated("Qwen/Qwen3-30B-A3B", "qwen3")],
        provider_facts=[provider_fact()],
        catalogues={"openrouter": [catalogue_row("qwen/qwen3-30b-a3b")]},
    )
    rows = by_id(inv)
    assert set(rows) == {"Qwen/Qwen3-30B-A3B", "qwen/qwen3-30b-a3b"}
    assert rows["Qwen/Qwen3-30B-A3B"].facets == ["catalog"]
    assert rows["qwen/qwen3-30b-a3b"].facets == ["provider"]


def test_a_served_provider_model_is_never_also_an_offer(inv):
    """The browser had to drop an offer for a provider already serving the
    model, because the two endpoints feeding it polled 15s and 300s apart and
    for one tick a row claimed both -- which drew a Serve button beside a Stop
    serving one. One table with a flag, read once, closes that window by
    construction."""
    inv.refresh_fast(
        provider_facts=[provider_fact()],
        catalogues={
            "openrouter": [
                catalogue_row("a/served", enabled=True),
                catalogue_row("b/offered", enabled=False),
            ]
        },
    )
    rows = by_id(inv)
    served = [p for p in rows["a/served"].providers if p.served]
    offered = [p for p in rows["a/served"].providers if not p.served]
    assert len(served) == 1 and offered == []
    assert rows["a/served"].facets == ["provider"]
    assert rows["b/offered"].facets == ["offered"]
    assert all(not p.served for p in rows["b/offered"].providers)


def test_an_unserved_model_claims_no_served_name(inv):
    """Nothing answers to it at /v1. Letting it into served_names would make
    the row findable by a name that routes nowhere -- and, if anything ever
    wired selection to this payload, would put a name in the URL that 404s."""
    inv.refresh_fast(
        provider_facts=[provider_fact()],
        catalogues={"openrouter": [catalogue_row("b/offered", enabled=False)]},
    )
    assert by_id(inv)["b/offered"].served_names == []


def test_a_finished_deployment_is_still_recorded(inv):
    """A FAILED deployment is the answer to "what happened to this model".
    Filtering terminal states here would delete that sentence; the screen
    bands them by verdict and says what went wrong."""
    inv.refresh_fast(
        deployments=[
            FakeDeployment(
                "d-1", "x", FakeShape("org/x"), state="failed", last_error="OOM at load"
            )
        ]
    )
    row = by_id(inv)["org/x"]
    assert row.deployments[0].state == "failed"
    assert row.deployments[0].last_error == "OOM at load"


def test_provider_enabled_is_recorded_apart_from_served(inv):
    """`servable()` filters by the allowlist and deliberately does NOT filter
    by `provider.enabled` -- `build_index` does that separately. So an
    allowlisted model on a switched-off provider is listed today while nothing
    routes to it, and a reader needs both facts to tell those apart."""
    inv.refresh_fast(
        provider_facts=[provider_fact(enabled=False)],
        catalogues={"openrouter": [catalogue_row("a/m", enabled=True)]},
    )
    prov = by_id(inv)["a/m"].providers[0]
    assert prov.served is True
    assert prov.provider_enabled is False


# -- weights on disk --------------------------------------------------------


def test_bytes_on_disk_is_the_largest_node_never_the_sum(inv):
    """Two node records can share one physical cache -- this cluster registers
    a node and a probe worker on the same host -- and summing there would
    claim double the size for a single download."""
    inv.refresh_cache(
        cache_report(
            [
                node_ok("a", [repo("org/m", size=182)]),
                node_ok("b", [repo("org/m", size=182)]),
            ]
        )
    )
    row = by_id(inv)["org/m"]
    assert row.bytes_on_disk == 182
    assert row.cached_on == ["a", "b"]


def test_an_empty_cache_directory_is_not_on_disk(inv):
    """blob_count 0 is a resolve that touched the repo and wrote nothing.
    Counting it said "already downloaded, the first launch does not have to
    pull it" about a model of which not one byte was present."""
    inv.refresh_cache(
        cache_report([node_ok("a", [repo("org/empty", size=0, blob_count=0)])])
    )
    assert "org/empty" not in by_id(inv)


def test_a_model_only_on_disk_still_gets_a_row(inv):
    """Somebody pulled it by hand. Nothing curated it, nothing serves it, and
    it is still the largest thing on the disk."""
    inv.refresh_cache(cache_report([node_ok("a", [repo("org/handpulled")])]))
    assert by_id(inv)["org/handpulled"].facets == ["ondisk"]


def test_an_unreachable_node_does_not_erase_its_weights(inv):
    """The load-bearing one. A node that could not be read must not silently
    become "this model is not on disk anywhere" -- the same class of mistake
    as rendering an unreadable disk as 0 bytes free. Its rows stand, its
    observed_at stays where it was, and only attempted_at moves."""
    inv.refresh_cache(
        cache_report(
            [node_ok("a", [repo("org/m")]), node_ok("b", [repo("org/m")])],
            measured_at=100.0,
        )
    )
    sentence = "The node agent on 'b' did not answer: [Errno 111] Connection refused."
    inv.refresh_cache(
        cache_report(
            [node_ok("a", [repo("org/m")]), node_down("b", sentence)], measured_at=200.0
        )
    )

    assert by_id(inv)["org/m"].cached_on == ["a", "b"]
    scans = {s.node_id: s for s in inv.cache_scans()}
    assert scans["b"].available is False
    assert scans["b"].reason == sentence
    assert scans["b"].observed_at == 100.0
    assert scans["b"].attempted_at > 100.0
    assert scans["a"].observed_at == 200.0


def test_a_node_never_read_reports_null_not_zero(inv):
    """`observed_at: None` is "never measured". Zero would read as measured,
    at the epoch, which is a number somebody could subtract from."""
    inv.refresh_cache(cache_report([node_down("a", "no model cache route")]))
    scan = inv.cache_scans()[0]
    assert scan.observed_at is None
    assert scan.available is False


def test_a_departed_node_stops_asserting_weights(inv):
    inv.refresh_cache(
        cache_report([node_ok("a", [repo("org/m")]), node_ok("b", [repo("org/n")])])
    )
    inv.drop_nodes({"a"})
    rows = by_id(inv)
    assert "org/m" in rows and "org/n" not in rows
    assert [s.node_id for s in inv.cache_scans()] == ["a"]


# -- what must never be in here ---------------------------------------------


def test_no_fit_verdict_reaches_a_record(inv):
    """A verdict is an answer to a question -- (model, context, concurrency,
    node set) -- and this table is asked none of them. A field here would make
    the registry assert a verdict nobody asked for, and give the screen two
    sources of `verdict` that can disagree."""
    inv.refresh_fast(curated=[FakeCurated("org/x", "x")])
    row = by_id(inv)["org/x"]
    for banned in (
        "verdict",
        "reason",
        "basis",
        "predicted_decode_tps",
        "headroom",
        "total_params",
        "dtype",
        "native_dtype",
        "requantized",
        "warnings",
    ):
        assert not hasattr(row, banned), f"{banned} must stay on /api/capacity"


def test_no_key_material_reaches_a_record(inv):
    """The one unrecoverable mistake in this product. A provider row carries
    display facts and prices; it never carries a base URL, a key, or the NAME
    of a key."""
    inv.refresh_fast(
        provider_facts=[provider_fact(api_key_ref="DERATE_OPENROUTER_API_KEY")],
        catalogues={"openrouter": [catalogue_row("a/m")]},
    )
    prov = by_id(inv)["a/m"].providers[0]
    assert not hasattr(prov, "api_key")
    assert not hasattr(prov, "api_key_ref")
    assert not hasattr(prov, "base_url")


# -- durability and degradation ---------------------------------------------


def test_rows_survive_the_process_that_wrote_them(tmp_path):
    first = ModelInventory(tmp_path / "models.db")
    first.refresh_fast(curated=[FakeCurated("org/x", "x")])
    first.close()

    second = ModelInventory(tmp_path / "models.db")
    assert [m.model_id for m in second.list_models()] == ["org/x"]


def test_an_unchanged_refresh_does_not_rewrite_the_tables(inv):
    """Several hundred provider rows turn over on a catalogue refresh. If the
    digest's inputs included anything that moves on its own -- spend,
    outstanding counts, a clock -- every tick would rewrite all of them
    forever, and no unit test would show it."""
    kw = dict(
        provider_facts=[provider_fact()],
        catalogues={"openrouter": [catalogue_row("a/m")]},
    )
    first = inv.refresh_fast(**kw)
    assert inv.refresh_fast(**kw) == first
    assert inv.refresh_fast(**kw) == first


def test_an_unchanged_storage_reading_does_not_move_the_revision(inv):
    """Found in production, not by a test, which is why this one exists.

    Six screens poll `/api/storage` on a 30s timer and every one of them feeds
    the registry. Without a short-circuit here the cache rows were deleted and
    reinserted every two or three seconds against a cluster where nothing had
    been downloaded -- and `revision`, which exists so a reader can tell a real
    change from a re-poll, climbed past 250 in five minutes and meant nothing.

    `measured_at` moves on every fan-out whether or not a byte moved, so it
    must stay out of the digest; hashing it would make this unreachable.
    """
    report = cache_report([node_ok("a", [repo("org/m")])], measured_at=100.0)
    first = inv.refresh_cache(report)
    again = inv.refresh_cache(
        cache_report([node_ok("a", [repo("org/m")])], measured_at=200.0)
    )
    assert again == first, "a re-poll of an unchanged disk moved the revision"

    # ...and a real change still does.
    moved = inv.refresh_cache(
        cache_report([node_ok("a", [repo("org/m"), repo("org/n")])], measured_at=300.0)
    )
    assert moved > first


def test_a_re_poll_still_records_that_we_asked(inv):
    """The short-circuit must not make the registry look like it stopped
    looking. `attempted_at` moves even when nothing changed, because a screen
    saying "still asking, last answer 40 minutes ago" needs both halves."""
    inv.refresh_cache(cache_report([node_ok("a", [repo("org/m")])], measured_at=100.0))
    before = inv.cache_scans()[0].attempted_at
    inv.refresh_cache(cache_report([node_ok("a", [repo("org/m")])], measured_at=100.0))
    after = inv.cache_scans()[0]
    assert after.attempted_at >= before
    assert after.observed_at == 100.0, "a re-poll must not restamp the reading"


def test_a_changed_source_does_move_the_revision(inv):
    kw = dict(provider_facts=[provider_fact()], catalogues={"openrouter": []})
    first = inv.refresh_fast(**kw)
    second = inv.refresh_fast(
        provider_facts=[provider_fact()],
        catalogues={"openrouter": [catalogue_row("a/m")]},
    )
    assert second > first


def test_a_failed_feed_keeps_its_rows_and_says_so(inv):
    """A partial read written as though it were the whole picture would delete
    every row the failed feed owns. Same rule as an unreachable node."""
    inv.refresh_fast(
        provider_facts=[provider_fact()],
        catalogues={"openrouter": [catalogue_row("a/m")]},
    )
    inv.refresh_fast(errors={"providers": "the provider service is not available"})

    assert "a/m" in by_id(inv)
    sources = inv.sources()
    assert sources["providers"]["ok"] is False
    assert sources["providers"]["reason"] == "the provider service is not available"


def test_a_corrupt_database_is_discarded_rather_than_raised(tmp_path):
    """A corrupt file must never lock an operator out of a running cluster,
    and nothing in here is anything but derived."""
    path = tmp_path / "models.db"
    path.write_bytes(b"\x00" * 4096)

    inv = ModelInventory(path)
    assert inv.list_models() == []
    inv.refresh_fast(curated=[FakeCurated("org/x", "x")])
    assert [m.model_id for m in inv.list_models()] == ["org/x"]


def test_a_schema_bump_discards_and_rebuilds(tmp_path):
    """Every row is re-derivable from stores that are still live, so a version
    mismatch is answered by throwing the file away -- not by a migration path
    that has to be right forever. The archive makes the opposite choice
    because it holds the only copy of what it collected."""
    path = tmp_path / "models.db"
    first = ModelInventory(path)
    first.refresh_fast(curated=[FakeCurated("org/x", "x")])
    first._conn.execute("UPDATE meta SET v='999' WHERE k='schema_version'")
    first.close()

    second = ModelInventory(path)
    assert second.list_models() == []
    row = second._conn.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    assert int(row["v"]) == SCHEMA_VERSION


def test_a_missing_database_answers_empty_rather_than_raising(tmp_path):
    inv = ModelInventory(tmp_path / "nested" / "models.db")
    assert inv.list_models() == []
    assert inv.revision == 0


def test_a_provider_port_without_a_catalogue_degrades(inv):
    """`catalogue` is not on the frozen ProviderPort -- the gateway reaches it
    by duck-typing. A port that implements only the protocol yields a registry
    with no provider rows, not a traceback."""
    inv.refresh_fast(
        deployments=[FakeDeployment("d-1", "x", FakeShape("org/x"))],
        provider_facts=[provider_fact()],
        catalogues=None,
    )
    assert by_id(inv)["org/x"].facets == ["running"]
