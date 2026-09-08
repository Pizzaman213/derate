"""``GET /api/models`` on the wire.

Two properties carry the weight. That the route is reachable with the UI
mounted -- get that wrong and every other assertion is testing a page of HTML.
And that it does not shadow the four literal paths ``capacity_api`` already
owns under ``/api/models/``, which is the failure that would arrive months
later when somebody reorders ``create_app`` for an unrelated reason.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from tests.test_gateway import build_deps


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Keep the registry out of the developer's real data directory.

    ``create_app`` opens ``<data_dir>/models.db``, and ``data_dir()`` reads
    ``DERATE_DATA_DIR`` on every call, so pointing it at tmp is enough.
    """
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture()
def client():
    with TestClient(create_app(build_deps())) as c:
        yield c


def test_the_route_is_reachable_with_the_ui_mounted(tmp_path):
    """A Starlette mount at "/" catches everything not matched by an EARLIER
    route. Registered after it, this would quietly serve index.html and
    surface as a JSON parse error three layers from the cause."""
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")

    app = create_app(
        GatewayDeps(),
        settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)),
    )
    with TestClient(app) as c:
        reply = c.get("/api/models")

    assert reply.status_code == 200
    assert reply.headers["content-type"].startswith("application/json"), (
        "the UI mount is shadowing /api/models"
    )


def test_it_does_not_shadow_capacity_apis_literal_paths(client):
    """`/api/models/quant-table`, `/search`, `/detail` and `/variants` belong
    to capacity_api. This is the test that fails the day somebody adds
    `/api/models/{model_id}` here."""
    assert client.get("/api/models/quant-table").status_code == 200
    assert "schemes" in client.get("/api/models/quant-table").json()
    # The other three answer their own shapes, whatever their status: what
    # matters is that none of them is being handled by the inventory router,
    # which would answer a `models` list.
    for path in ("/api/models/search?q=llama", "/api/models/detail", "/api/models/variants"):
        body = client.get(path)
        if body.status_code == 200:
            assert "models" not in body.json() or "results" in body.json()


def test_the_payload_names_its_sources_and_its_schema(client):
    """Folding five fetches into one leaves the browser with no failed request
    to notice, so the per-feed sentence has to travel in the payload or the
    Models tab's "a feed failing names itself" rule stops existing."""
    body = client.get("/api/models").json()
    assert isinstance(body["models"], list)
    assert body["schema_version"] >= 1
    assert "sources" in body and "store" in body["sources"]
    assert body["sources"]["store"]["ok"] is True


def test_no_fit_verdict_reaches_the_wire(client):
    """A verdict is an answer to a question this endpoint is not asked. A
    field here would give the screen two sources of `verdict` that can
    disagree -- the exact drift this endpoint exists to remove, one layer
    down."""
    for row in client.get("/api/models").json()["models"]:
        for banned in (
            "verdict",
            "reason",
            "basis",
            "predicted_decode_tps",
            "headroom",
            "total_params",
            "dtype",
            "requantized",
            "warnings",
        ):
            assert banned not in row, f"{banned} must stay on /api/capacity"


def test_no_credential_field_reaches_the_wire(client):
    """The one unrecoverable mistake in this product, checked on a new surface.

    Asked structurally, over field NAMES, rather than by searching the text.
    Both halves of that matter. A substring search would miss `api_key: '***'`
    -- a key slot on every provider row, which is how an inverse of
    `redact.ts` gets added later without anybody noticing. And it would fire
    on a payload that is behaving correctly: a provider whose key does not
    resolve reports `last_error: "api_key_ref 'DERATE_OPENROUTER_API_KEY'
    resolves to nothing..."`, and that sentence is the product telling an
    operator what to fix. The reference NAME is deliberately not secret --
    `/api/providers` publishes it as a field and `/api/providers/secret-refs`
    exists to serve nothing else -- so the sentence stays, verbatim, and only
    the slots are refused.
    """
    banned = {"api_key", "api_key_ref", "base_url", "backend_url", "secret", "token"}

    def walk(node, path="$"):
        if isinstance(node, dict):
            for key, value in node.items():
                assert key not in banned, f"{path}.{key} is a credential slot"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]")

    walk(client.get("/api/models").json())


def test_served_and_offered_are_disjoint_on_the_wire(client):
    """One table with a flag becomes two arrays on the way out, and they can
    never overlap. An offer for a provider already serving the model is what
    drew a Serve button beside a Stop serving one."""
    for row in client.get("/api/models").json()["models"]:
        served = {p["provider_id"] for p in row["providers"]}
        assert all(o["provider_id"] not in served for o in row["offers"])


def test_every_row_has_at_least_one_facet(client):
    """A row with no provenance is a row that exists for no reason anybody can
    name, and the band it would draw in is undefined."""
    for row in client.get("/api/models").json()["models"]:
        assert row["where"], row["model_id"]
        assert "hub" not in row["where"], (
            "hub search resolves nothing; its hits are a query's answer, not a "
            "fact about this cluster, and the browser adds that facet"
        )


def test_the_curated_catalogue_reaches_it(client):
    """The four curated shapes are a source of this endpoint. If they stop
    arriving, the Models tab looks full and the shortlist is silently gone."""
    from control_plane.fit.catalog import CURATED_MODELS

    rows = {r["model_id"] for r in client.get("/api/models").json()["models"]}
    assert {m.model_id for m in CURATED_MODELS} <= rows


def test_a_model_id_filter_narrows_without_a_path_parameter(client):
    """`/api/models?model_id=` rather than `/api/models/{id}`, which would
    shadow capacity_api's four."""
    from control_plane.fit.catalog import CURATED_MODELS

    wanted = CURATED_MODELS[0].model_id
    body = client.get("/api/models", params={"model_id": wanted}).json()
    assert [r["model_id"] for r in body["models"]] == [wanted]
