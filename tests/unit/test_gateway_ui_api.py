"""P-UIAPI: GET/PATCH /api/settings, on its own router.

The two things worth testing hard are the ones that make the difference between
a control and the appearance of one: that a cap nobody can measure is refused
rather than stored, and that the router is registered above the StaticFiles
mount so it is reachable at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from control_plane.gateway import GatewayDeps, GatewaySettings, create_app
from control_plane.gateway import ui_api
from control_plane.gateway.settings_store import MUTABLE_FIELDS, SettingsStore


class _AccountingProviders:
    """A provider port that can say what has been spent, so a cap is
    enforceable."""

    def public_list(self, *, include_models: bool = True):
        return [{"provider_id": "openrouter", "spend_today_usd": 0.41}]

    def list(self):
        return []


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A gateway whose settings file lives in tmp_path rather than /data.

    Done by pointing DERATE_DATA_DIR at a temp dir rather than by injecting
    a store, so the test exercises the real wiring create_app builds -- the
    default path resolution is part of what is under test.
    """
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))

    def _app(providers=None):
        deps = GatewayDeps(providers=providers) if providers else GatewayDeps()
        return create_app(deps, settings=GatewaySettings(cluster_id="c-test"))

    _app.store = SettingsStore(tmp_path / "settings.json")
    return _app


# ==========================================================================
# Reachability -- the ordering bug that would make everything else moot
# ==========================================================================


def test_settings_is_reachable_even_when_the_ui_is_mounted(tmp_path, monkeypatch):
    """A Starlette mount at "/" catches every path not matched by an EARLIER
    route, so if this router were registered after it, /api/settings would
    quietly return index.html and the UI would see a JSON parse error three
    layers from the cause.

    Tested by actually mounting a UI and asking, rather than by inspecting
    app.router.routes -- this FastAPI wraps included routers in _IncludedRouter
    objects with no .path, so route introspection asserts on an implementation
    detail while this asserts on the property that matters."""
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path))
    ui = tmp_path / "ui"
    ui.mkdir()
    (ui / "index.html").write_text("<!doctype html><title>derate</title>")

    app = create_app(
        GatewayDeps(),
        settings=GatewaySettings(cluster_id="c-test", ui_dir=str(ui)),
    )
    with TestClient(app) as c:
        api = c.get("/api/settings")
        root = c.get("/")

    assert api.status_code == 200
    assert api.headers["content-type"].startswith("application/json"), (
        "the UI mount is shadowing /api/settings"
    )
    assert "electricity_rate_usd_per_kwh" in api.json()
    # And the mount still does its job.
    assert root.status_code == 200
    assert "derate" in root.text


def test_get_returns_every_writable_field_with_its_source(client):
    with TestClient(client()) as c:
        body = c.get("/api/settings").json()
    for key in MUTABLE_FIELDS:
        assert key in body, f"missing {key}"
        assert key in body["sources"], f"missing source for {key}"
    assert body["writable"] == list(MUTABLE_FIELDS)


def test_an_unset_value_is_labelled_default_not_silently_zero(client):
    """The UI has to tell '0.00 because nobody set it' from '0.00 because
    someone set it to zero'; they render differently."""
    with TestClient(client()) as c:
        body = c.get("/api/settings").json()
    assert body["electricity_rate_usd_per_kwh"] == 0.0
    assert body["sources"]["electricity_rate_usd_per_kwh"] == "default"
    assert body["daily_spend_cap_usd"] is None


def test_auto_restart_defaults_on(client):
    """A crashed model nobody is watching should come back on its own -- the
    setting has to default true, not just exist."""
    with TestClient(client()) as c:
        body = c.get("/api/settings").json()
    assert body["auto_restart_crashed_deployments"] is True
    assert body["sources"]["auto_restart_crashed_deployments"] == "default"


# ==========================================================================
# The honesty gate on the cap
# ==========================================================================


def test_a_cap_is_refused_when_nothing_reports_spend(client):
    """Accepting it and failing open would ship a control that appears to guard
    money and does not. 501 with a sentence is the honest third option."""
    with TestClient(client()) as c:
        response = c.patch("/api/settings", json={"daily_spend_cap_usd": 5.0})
    assert response.status_code == 501
    body = response.json()
    assert body["error"]["code"] == "not_implemented"
    assert "spend" in body["error"]["message"]
    assert client.store.load() == {}, "a refused cap must not be persisted"


def test_a_cap_is_accepted_when_a_port_reports_spend(client):
    with TestClient(client(providers=_AccountingProviders())) as c:
        response = c.patch("/api/settings", json={"daily_spend_cap_usd": 5.0})
    assert response.status_code == 200
    assert response.json()["daily_spend_cap_usd"] == 5.0


def test_clearing_the_cap_is_allowed_even_without_accounting(client):
    """Removing a cap needs no measurement -- only setting one does."""
    with TestClient(client()) as c:
        response = c.patch("/api/settings", json={"daily_spend_cap_usd": None})
    assert response.status_code == 200
    assert response.json()["daily_spend_cap_usd"] is None


def test_enforceability_is_reported_so_the_ui_can_disable_the_control(client):
    with TestClient(client()) as c:
        assert c.get("/api/settings").json()["daily_spend_cap_enforceable"] is False
    with TestClient(client(providers=_AccountingProviders())) as c:
        assert c.get("/api/settings").json()["daily_spend_cap_enforceable"] is True


# ==========================================================================
# Writes
# ==========================================================================


def test_a_write_persists_and_is_labelled_as_coming_from_the_file(client):
    with TestClient(client()) as c:
        body = c.patch("/api/settings", json={"local_only": True}).json()
    assert body["local_only"] is True
    assert body["sources"]["local_only"] == "file"
    assert client.store.load()["local_only"] is True


def test_auto_restart_can_be_toggled_off_and_persists(client):
    with TestClient(client()) as c:
        body = c.patch(
            "/api/settings", json={"auto_restart_crashed_deployments": False}
        ).json()
    assert body["auto_restart_crashed_deployments"] is False
    assert body["sources"]["auto_restart_crashed_deployments"] == "file"
    assert client.store.load()["auto_restart_crashed_deployments"] is False


def test_a_write_takes_effect_on_the_live_settings_object(client):
    """Next request, not next restart."""
    app = client()
    with TestClient(app) as c:
        c.patch("/api/settings", json={"local_only": True})
    assert app.state.ctx.settings.local_only is True


def test_an_unknown_key_is_a_400_that_names_the_writable_set(client):
    """Silently ignoring it is how a control ends up looking wired and doing
    nothing."""
    with TestClient(client()) as c:
        response = c.patch("/api/settings", json={"critical_memory_pct": 0.5})
    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "critical_memory_pct" in message
    for key in MUTABLE_FIELDS:
        assert key in message


@pytest.mark.parametrize(
    "payload", [{"electricity_rate_usd_per_kwh": -1}, {"local_only": "yes"}, {"daily_spend_cap_usd": -5}]
)
def test_bad_values_are_refused_with_a_reason(client, payload):
    with TestClient(client()) as c:
        response = c.patch("/api/settings", json=payload)
    assert response.status_code == 400


def test_a_non_object_body_is_refused(client):
    with TestClient(client()) as c:
        assert c.patch("/api/settings", json=[1, 2, 3]).status_code == 400


def test_a_partial_patch_leaves_the_other_fields_alone(client):
    with TestClient(client()) as c:
        c.patch("/api/settings", json={"electricity_rate_usd_per_kwh": 0.28})
        body = c.patch("/api/settings", json={"local_only": True}).json()
    assert body["electricity_rate_usd_per_kwh"] == 0.28
    assert body["local_only"] is True


def test_settings_survive_a_restart(client):
    with TestClient(client()) as c:
        c.patch("/api/settings", json={"electricity_rate_usd_per_kwh": 0.28})
    # A brand new app against the same store, as a restart would build.
    with TestClient(client()) as c:
        body = c.get("/api/settings").json()
    assert body["electricity_rate_usd_per_kwh"] == 0.28
    assert body["sources"]["electricity_rate_usd_per_kwh"] == "file"
