"""The five /api/history/* endpoints, tested at the wire.

They are live and mounted, and `grep "api/history" tests/` found nothing before
this file: `test_telemetry.py` covers the query functions and the /agent/journal
leg, but nothing exercised the HTTP layer between them. The two things only a
wire test can catch are here — that the disabled path returns a structured 503
rather than an exception, and that the envelope a UI has to branch on
(`resolution`, `gaps`, `truncated`) actually survives serialization.

Owned by the derate/UI session. Kept in its own file per the campaign's
per-file discipline; `test_telemetry.py`, `test_gateway.py` and
`test_gateway_runtime.py` all have other owners.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.registry.telemetry import TelemetrySample
from control_plane.telemetry import KIND_EVENT, KIND_LOG, KIND_SAMPLE
from control_plane.telemetry.archive import Archive, open_archive
from control_plane.telemetry.service import Telemetry

#: /api/history/nodes is deliberately absent: it has a ring fallback and is
#: covered by its own truth table below. These three have nothing behind them
#: when telemetry is off, and inventing a fallback for them would be worse than
#: saying so.
NO_FALLBACK_ROUTES = (
    "/api/history/requests",
    "/api/history/events",
    "/api/history/logs",
)
ALL_HISTORY_ROUTES = ("/api/history/nodes", *NO_FALLBACK_ROUTES)


class _RingRegistry:
    """A registry that keeps the 300-sample in-memory ring, as the real one
    does. The shipped StubRegistry has no history(), which is the OTHER branch
    of the truth table."""

    def __init__(self, samples=None):
        self._samples = samples or []

    def history(self, node_id, seconds=60):
        return list(self._samples)

    def list_nodes(self):
        return []

    def healthy_nodes(self):
        return []


def _client(telemetry: Telemetry) -> TestClient:
    settings = GatewaySettings(cluster_id="c-test")
    return TestClient(create_app(GatewayDeps(), settings=settings, telemetry=telemetry))


@pytest.fixture()
def archive(tmp_path) -> Archive:
    return open_archive(tmp_path)


@pytest.fixture()
def live(tmp_path) -> Telemetry:
    """A real coordinator: journal + archive. `Telemetry.enabled` is
    `journal is not None`, so an archive alone reads as disabled."""
    telemetry = Telemetry.open(tmp_path, node_id="spark-01", coordinator=True)
    yield telemetry
    telemetry.journal.close(timeout=2.0)


def _ingest(archive: Archive, node_id: str, rows: list[dict]) -> None:
    """Rows land in typed tables keyed PRIMARY KEY(node_id, seq) for events and
    logs, so every row needs a distinct seq or the second silently replaces the
    first. Samples are keyed on ts and would survive without it -- which is
    exactly the kind of asymmetry a helper should absorb once."""
    numbered = [{**row, "seq": row.get("seq", i + 1)} for i, row in enumerate(rows)]
    archive.ingest(node_id, {"node_id": node_id, "rows": numbered, "next": len(numbered)})


# ==========================================================================
# Telemetry off. This is the state on every dev machine and in this suite,
# so it is the path most likely to be hit and least likely to be exercised.
# ==========================================================================


@pytest.mark.parametrize("route", NO_FALLBACK_ROUTES)
def test_routes_without_a_fallback_are_a_structured_503_when_telemetry_is_off(route):
    """Not a 500, not an empty 200. A UI has to tell 'the feature is off' from
    'the query failed' from 'the window was genuinely empty', and only a
    distinct status plus a code makes that possible.

    These three have no in-memory equivalent, so 503 is the honest answer.
    Inventing a fallback for them would be worse than saying so."""
    with _client(Telemetry.disabled("not configured")) as client:
        response = client.get(route)
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "history_unavailable"
    # The reason is carried through rather than flattened to a generic string.
    assert "not configured" in body["error"]["message"]


# ==========================================================================
# /api/history/nodes has three outcomes, not two. Written from the contract
# the telemetry session published, not from reading their implementation --
# a divergence here is a defect report, not a test to adjust.
# ==========================================================================


def test_nodes_falls_back_to_the_ring_when_telemetry_is_off():
    """Telemetry off + a registry that keeps the ring: 200, resolution "ring",
    durable false. The ring is real data and refusing it would be a lie of
    omission; calling it durable would be the opposite lie."""
    now = time.time()
    deps = GatewayDeps(registry=_RingRegistry([{"ts": now - 5, "power_w": 71.0}]))
    app = create_app(deps, settings=GatewaySettings(cluster_id="c-test"),
                     telemetry=Telemetry.disabled("not configured"))
    with TestClient(app) as client:
        response = client.get("/api/history/nodes",
                              params={"node_id": "spark-01", "from": "-60s"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["resolution"] == "ring"
    assert body["durable"] is False


def test_nodes_is_503_when_telemetry_is_off_and_there_is_no_ring():
    """Day-0 StubRegistry exposes no history(), so there is genuinely nothing
    to answer with."""
    with _client(Telemetry.disabled("not configured")) as client:
        response = client.get("/api/history/nodes", params={"from": "-60s"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "history_unavailable"


def test_the_archive_path_is_marked_durable(live):
    """The whole point of the flag: a consumer must be able to tell a 5-minute
    RAM ring that dies on restart from 30 days of SQLite."""
    with _client(live) as client:
        body = client.get("/api/history/nodes", params={"from": "-60s"}).json()
    assert body["durable"] is True
    assert body["resolution"] in ("raw", "1m", "1h")


@pytest.mark.parametrize("route", ALL_HISTORY_ROUTES)
def test_every_successful_history_response_carries_durable(live, route):
    """Asserting on status alone would let a response that silently dropped the
    flag pass, and `durable` is what the UI branches on."""
    with _client(live) as client:
        response = client.get(route, params={"from": "-60s"})
    assert response.status_code == 200, response.text
    assert "durable" in response.json(), f"{route} lost the durable flag"


def test_status_reports_disabled_without_erroring():
    """/api/history/status is the endpoint a UI polls to decide whether to draw
    the surface at all, so it must answer even when everything else 503s."""
    with _client(Telemetry.disabled("no data root")) as client:
        response = client.get("/api/history/status")
    assert response.status_code == 200
    assert response.json()["enabled"] is False


def test_status_reports_enabled_when_telemetry_is_open(live):
    with _client(live) as client:
        body = client.get("/api/history/status").json()
    assert body["enabled"] is True


def test_the_ring_and_the_archive_return_the_same_shape(live):
    """Not in the published contract, but the property that decides how much
    branching a consumer needs. Both paths already carry `resolution` and
    `durable`; if their SAMPLE keys also match, a UI branches on raw-vs-rollup
    and nothing else. If they ever diverge, every chart needs a third case."""
    now = time.time()
    # Built from as_dict() rather than typed out, so the fixture itself cannot
    # be the thing that diverges. A hand-written key list here would keep
    # passing while the two real paths drifted apart -- which is the one failure
    # this test exists to catch.
    sample = TelemetrySample(
        ts=now - 5, memory_used=10, memory_total=128, power_watts=71.0,
        temperature_c=62.0, utilization_pct=34.0, gpu_memory_used=9,
        gpu_process_count=1, host_memory_total=128,
        host_memory_available=100, swap_used=0,
        clock_throttle_bits=0x4, sm_clock_mhz=1500, sm_clock_max_mhz=3003,
        swap_in_bps=0.0, swap_out_bps=1048576.0, major_faults_per_s=2.0,
        memory_pressure_pct=11.0,
    ).as_dict()
    _ingest(live.archive, "spark-01",
            [{"kind": KIND_SAMPLE, "ts": now - 5, "body": dict(sample)}])
    with _client(live) as client:
        archived = client.get(
            "/api/history/nodes", params={"node_id": "spark-01", "from": "-60s"}
        ).json()

    deps = GatewayDeps(registry=_RingRegistry([dict(sample)]))
    app = create_app(deps, settings=GatewaySettings(cluster_id="c-test"),
                     telemetry=Telemetry.disabled("off"))
    with TestClient(app) as client:
        ringed = client.get(
            "/api/history/nodes", params={"node_id": "spark-01", "from": "-60s"}
        ).json()

    assert archived["resolution"] == "raw" and archived["durable"] is True
    assert ringed["resolution"] == "ring" and ringed["durable"] is False
    assert set(archived) == set(ringed), "the envelopes must not diverge"
    assert set(archived["samples"][0]) == set(ringed["samples"][0]), (
        "sample keys diverged; a UI would need a third branch"
    )


# ==========================================================================
# The envelope a UI must branch on
# ==========================================================================


def test_nodes_returns_the_envelope_the_ui_branches_on(live):
    archive = live.archive
    now = time.time()
    _ingest(
        archive,
        "spark-01",
        [
            {
                "kind": KIND_SAMPLE,
                "ts": now - 10,
                "body": {
                    "ts": now - 10,
                    "memory_used": 10 * 1024**3,
                    "memory_total": 128 * 1024**3,
                    "power_w": 71.0,
                    "temp_c": 62.0,
                    "util_pct": 34.0,
                    "gpu_memory_used": 9 * 1024**3,
                    "gpu_process_count": 1,
                    "host_memory_total": 128 * 1024**3,
                    "host_memory_available": 100 * 1024**3,
                    "swap_used": 0,
                },
            }
        ],
    )
    with _client(live) as client:
        body = client.get("/api/history/nodes", params={"from": "-1h"}).json()

    # Without `resolution` a consumer cannot know whether to read `power_w` or
    # `power_w_avg`, and without `gaps` a flat line is indistinguishable from a
    # trimmed one.
    for key in ("from", "to", "resolution", "gaps", "truncated", "samples"):
        assert key in body, f"missing {key}"
    assert isinstance(body["gaps"], list)
    assert body["truncated"] is False


def test_a_fresh_write_comes_back_at_raw_resolution(live):
    """Rollups lag by ROLL_LAG_S, so a sample written seconds ago can only be
    answered raw. If this ever returns 1m the column names change under the UI."""
    archive = live.archive
    now = time.time()
    _ingest(
        archive,
        "spark-01",
        [{"kind": KIND_SAMPLE, "ts": now - 5, "body": {"ts": now - 5, "power_w": 71.0}}],
    )
    with _client(live) as client:
        body = client.get("/api/history/nodes", params={"from": "-60s"}).json()
    assert body["resolution"] == "raw"
    assert body["samples"], "a sample written 5s ago must be in a 60s window"
    assert "power_w" in body["samples"][0]


def test_node_id_filters_rather_than_being_ignored(live):
    archive = live.archive
    now = time.time()
    for node in ("spark-01", "spark-02"):
        _ingest(
            archive,
            node,
            [{"kind": KIND_SAMPLE, "ts": now - 5, "body": {"ts": now - 5, "power_w": 70.0}}],
        )
    with _client(live) as client:
        body = client.get(
            "/api/history/nodes", params={"node_id": "spark-01", "from": "-60s"}
        ).json()
    assert {row["node_id"] for row in body["samples"]} == {"spark-01"}


def test_an_empty_window_is_an_empty_list_not_an_error(live):
    """Nothing recorded is a real answer and must not look like a failure."""
    with _client(live) as client:
        response = client.get("/api/history/nodes", params={"from": "-60s"})
    assert response.status_code == 200
    assert response.json()["samples"] == []


# ==========================================================================
# Events and logs
# ==========================================================================


def test_the_access_log_is_excluded_by_default_and_recoverable(live):
    """`/api/history/logs` hides what the handler no longer records.

    Two windows have to read the same: one recorded before the handler learned
    to drop uvicorn.access, and one recorded after. The route's default
    `exclude` is the handler's own list, so they do -- and `?exclude=`
    (present, empty) still reaches the rows already on disk, because deleting
    them was never the point.
    """
    archive = live.archive
    now = time.time()
    _ingest(
        archive,
        "spark-01",
        [
            {
                "kind": KIND_LOG,
                "ts": now - 5,
                "body": {
                    "ts": now - 5,
                    "level": "INFO",
                    "logger": "uvicorn.access",
                    "message": '127.0.0.1 - "GET /agent/telemetry HTTP/1.1" 200',
                },
            },
            {
                "kind": KIND_LOG,
                "ts": now - 4,
                "body": {
                    "ts": now - 4,
                    "level": "INFO",
                    "logger": "httpx",
                    "message": "HTTP Request: GET http://x/agent/telemetry",
                },
            },
            {
                "kind": KIND_LOG,
                "ts": now - 3,
                "body": {
                    "ts": now - 3,
                    "level": "WARNING",
                    "logger": "control_plane.links.measure",
                    "message": "no usable measurement",
                },
            },
        ],
    )

    with _client(live) as client:
        clean = client.get("/api/history/logs", params={"from": "-1h"}).json()
        everything = client.get(
            "/api/history/logs", params={"from": "-1h", "exclude": ""}
        ).json()
        narrowed = client.get(
            "/api/history/logs", params={"from": "-1h", "exclude": "httpx"}
        ).json()

    assert [r["logger"] for r in clean["logs"]] == ["control_plane.links.measure"]
    assert {r["logger"] for r in everything["logs"]} == {
        "uvicorn.access",
        "httpx",
        "control_plane.links.measure",
    }
    # An explicit list replaces the default rather than adding to it.
    assert {r["logger"] for r in narrowed["logs"]} == {
        "uvicorn.access",
        "control_plane.links.measure",
    }


def test_events_are_filterable_by_node(live):
    """`node_id` is stored and selected on every event row, but had no filter
    until the node page needed one — so "what happened on this machine" meant
    fetching the whole cluster's events and discarding most of them in the
    browser. `logs` has filtered on it since it shipped; this is the same
    clause on the same column."""
    archive = live.archive
    now = time.time()
    for node in ("spark-01", "spark-02"):
        _ingest(
            archive,
            node,
            [
                {
                    "kind": KIND_EVENT,
                    "ts": now - 5,
                    "body": {
                        "source": "deploy",
                        "type": "state_changed",
                        "ts": now - 5,
                        "deployment_id": f"d-{node}",
                    },
                }
            ],
        )

    with _client(live) as client:
        scoped = client.get(
            "/api/history/events", params={"node_id": "spark-02", "from": "-1h"}
        ).json()
        every = client.get("/api/history/events", params={"from": "-1h"}).json()

    assert scoped["events"], f"expected spark-02's event, got {scoped}"
    assert all(r["node_id"] == "spark-02" for r in scoped["events"])
    # An absent node_id still means "no filter", so the unscoped call is
    # unchanged by this.
    assert {r["node_id"] for r in every["events"]} == {"spark-01", "spark-02"}


def test_events_are_flattened_and_filterable_by_deployment(live):
    """The envelope puts `type` and `ts` at the top level beside the payload —
    the same shape EventBus.recent() produces, so a consumer written against
    one works with the other."""
    archive = live.archive
    now = time.time()
    _ingest(
        archive,
        "spark-01",
        [
            {
                "kind": KIND_EVENT,
                "ts": now - 5,
                "body": {
                    "source": "deploy",
                    "type": "state_changed",
                    "ts": now - 5,
                    "deployment_id": "d-a",
                    "served_name": "gpt-oss-120b",
                    "from": "launching",
                    "to": "ready",
                },
            },
            {
                "kind": KIND_EVENT,
                "ts": now - 4,
                "body": {
                    "source": "deploy",
                    "type": "launch_failed",
                    "ts": now - 4,
                    "deployment_id": "d-b",
                    "served_name": "other",
                },
            },
        ],
    )
    with _client(live) as client:
        body = client.get(
            "/api/history/events", params={"deployment_id": "d-a", "from": "-1h"}
        ).json()

    rows = body["events"] if "events" in body else body.get("rows", [])
    assert rows, f"expected the d-a event, got {body}"
    assert all(r.get("deployment_id") == "d-a" for r in rows)
    row = rows[0]
    assert row["type"] == "state_changed"
    assert "ts" in row
    # `from`/`to` are literal wire keys — `from` is a Python keyword, so it is
    # emitted via **{} and is easy to lose in a refactor.
    assert row.get("from") == "launching"
    assert row.get("to") == "ready"


def test_logs_are_queryable_by_level_and_substring(live):
    archive = live.archive
    now = time.time()
    _ingest(
        archive,
        "spark-01",
        [
            {
                "kind": KIND_LOG,
                "ts": now - 5,
                "body": {
                    "ts": now - 5,
                    "level": "ERROR",
                    "logger": "control_plane.deploy.manager",
                    "message": "d-a failed: CUDA out of memory",
                },
            },
            {
                "kind": KIND_LOG,
                "ts": now - 4,
                "body": {
                    "ts": now - 4,
                    "level": "INFO",
                    "logger": "control_plane.gateway",
                    "message": "routine",
                },
            },
        ],
    )
    with _client(live) as client:
        body = client.get(
            "/api/history/logs", params={"level": "ERROR", "q": "out of memory", "from": "-1h"}
        ).json()
    rows = body["logs"] if "logs" in body else body.get("rows", [])
    assert rows, f"expected the ERROR line, got {body}"
    assert all(r["level"] == "ERROR" for r in rows)
    assert "out of memory" in rows[0]["message"]


# ==========================================================================
# Failure handling
# ==========================================================================


def test_a_broken_archive_is_a_502_not_a_traceback(live):
    """The query runs on a worker thread; an exception there must not escape as
    a 500 with a stack trace."""
    archive = live.archive
    archive.conn.close()
    with _client(live) as client:
        response = client.get("/api/history/nodes", params={"from": "-60s"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "history_failed"


def test_limit_is_clamped_rather_than_trusted(live):
    """QUERY_MAX_ROWS exists so one request cannot ask for the whole archive."""
    with _client(live) as client:
        response = client.get(
            "/api/history/nodes", params={"from": "-1h", "limit": 10_000_000}
        )
    assert response.status_code == 200


def test_history_routes_do_not_shadow_the_existing_nodes_route(live):
    """/api/history/* is a sibling of /api/nodes, not a child, so there is no
    path-parameter collision — but a future move under /api/nodes/{id}/history
    would collide with /api/nodes/{node_id}. Pinned so that move is deliberate."""
    with _client(live) as client:
        assert client.get("/api/nodes").status_code == 200
        assert client.get("/api/history/status").status_code == 200
