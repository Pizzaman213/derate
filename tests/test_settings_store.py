"""P-STORE: the persisted mutable-settings layer.

The two properties worth testing hard are the ones a reasonable implementation
gets wrong: that `None` and `0.0` stay different instructions for the spend cap,
and that the file beats the environment rather than the other way round.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from control_plane.gateway.settings_store import (
    MUTABLE_FIELDS,
    SCHEMA_VERSION,
    SettingsError,
    SettingsStore,
    resolve,
)


@pytest.fixture()
def store(tmp_path):
    return SettingsStore(tmp_path / "settings.json")


# ==========================================================================
# The allowlist
# ==========================================================================


def test_the_writable_set_is_exactly_four_fields():
    """GatewaySettings has 42 fields. Persisting all of them would let a stale
    file pin a code default forever."""
    assert MUTABLE_FIELDS == (
        "electricity_rate_usd_per_kwh",
        "local_only",
        "daily_spend_cap_usd",
        "auto_restart_crashed_deployments",
    )


def test_an_unknown_key_is_refused_and_names_the_writable_set(store):
    with pytest.raises(SettingsError) as exc:
        store.patch({}, {"critical_memory_pct": 0.5})
    message = str(exc.value)
    assert "critical_memory_pct" in message
    for field in MUTABLE_FIELDS:
        assert field in message
    assert not store.path.exists(), "a refused patch must not write anything"


def test_a_key_outside_the_allowlist_cannot_reach_the_file(store):
    """save() filters as well as patch(), so no caller can smuggle one in."""
    store.save({"local_only": True, "port": 9999, "cluster_id": "nope"})
    body = json.loads(store.path.read_text())["settings"]
    assert body == {"local_only": True}


# ==========================================================================
# None is not zero. This is the one that matters.
# ==========================================================================


def test_a_null_cap_and_a_zero_cap_are_different_instructions(store):
    """None means no cap. 0.0 means spend nothing -- a legitimate way to say
    local-only while leaving the providers configured. A form whose empty field
    yields 0.0 would silently turn one into the other."""
    store.save({"daily_spend_cap_usd": None})
    assert store.load()["daily_spend_cap_usd"] is None

    store.save({"daily_spend_cap_usd": 0.0})
    reloaded = store.load()["daily_spend_cap_usd"]
    assert reloaded == 0.0
    assert reloaded is not None


def test_a_zero_rate_round_trips_as_zero_not_as_absent(store):
    """0.0 is the meaningful default -- local is free -- so it must survive as a
    set value, distinguishable from never having been set."""
    store.save({"electricity_rate_usd_per_kwh": 0.0})
    loaded = store.load()
    assert loaded["electricity_rate_usd_per_kwh"] == 0.0
    assert "electricity_rate_usd_per_kwh" in loaded


# ==========================================================================
# Validation refuses rather than coercing
# ==========================================================================


@pytest.mark.parametrize(
    "key,value",
    [
        ("electricity_rate_usd_per_kwh", -0.01),
        ("electricity_rate_usd_per_kwh", "0.14"),
        ("electricity_rate_usd_per_kwh", True),  # bool is not a number here
        ("daily_spend_cap_usd", -1),
        ("daily_spend_cap_usd", "5.00"),
        ("local_only", "true"),
        ("local_only", 1),
        ("auto_restart_crashed_deployments", "true"),
        ("auto_restart_crashed_deployments", 1),
    ],
)
def test_bad_values_are_refused_with_a_reason(store, key, value):
    with pytest.raises(SettingsError) as exc:
        store.patch({}, {key: value})
    assert key in str(exc.value)


def test_a_patch_merges_over_current_rather_than_replacing(store):
    current = {"local_only": True, "electricity_rate_usd_per_kwh": 0.28}
    merged = store.patch(current, {"daily_spend_cap_usd": 5.0})
    assert merged == {
        "local_only": True,
        "electricity_rate_usd_per_kwh": 0.28,
        "daily_spend_cap_usd": 5.0,
    }


# ==========================================================================
# Durability
# ==========================================================================


def test_the_file_is_written_atomically_and_private(store):
    store.save({"local_only": True})
    mode = stat.S_IMODE(os.stat(store.path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    leftovers = list(store.path.parent.glob(".settings-*"))
    assert leftovers == [], f"temp file left behind: {leftovers}"


def test_settings_survive_a_restart(tmp_path):
    path = tmp_path / "settings.json"
    SettingsStore(path).patch({}, {"local_only": True, "daily_spend_cap_usd": 25.0})
    # A brand new store object, as a restart would build.
    assert SettingsStore(path).load() == {
        "local_only": True,
        "daily_spend_cap_usd": 25.0,
    }


def test_the_schema_version_is_present_from_the_first_write(store):
    store.save({"local_only": False})
    assert json.loads(store.path.read_text())["version"] == SCHEMA_VERSION


# ==========================================================================
# Degradation: a bad file never stops the gateway starting
# ==========================================================================


def test_a_missing_file_is_not_an_error(store):
    assert store.load() == {}


@pytest.mark.parametrize(
    "text",
    [
        "{ not json",
        "[]",
        '{"version": 999, "settings": {"local_only": true}}',
        '{"version": 1}',
    ],
)
def test_an_unusable_file_degrades_to_empty_rather_than_raising(store, text):
    """An operator locked out of a running cluster by a bad config file is worse
    off than one whose last preference was forgotten."""
    store.path.write_text(text)
    assert store.load() == {}


def test_one_bad_field_does_not_discard_the_others(store):
    store.path.write_text(
        json.dumps(
            {
                "version": SCHEMA_VERSION,
                "settings": {"local_only": True, "electricity_rate_usd_per_kwh": -5},
            }
        )
    )
    assert store.load() == {"local_only": True}


# ==========================================================================
# Precedence: file > env > default
# ==========================================================================


def test_the_file_beats_the_environment():
    """Deliberately the opposite of the usual ordering. The file is a human
    action taken through the UI; env is the deployment's opinion. Nothing in the
    container sets these, so env-beats-file would make the UI controls
    permanently inert -- H-8's failure mode exactly."""
    resolved = resolve(
        file_values={"electricity_rate_usd_per_kwh": 0.28},
        env_values={"electricity_rate_usd_per_kwh": 0.10},
        defaults={"electricity_rate_usd_per_kwh": 0.0},
    )
    assert resolved["electricity_rate_usd_per_kwh"].value == 0.28
    assert resolved["electricity_rate_usd_per_kwh"].source == "file"


def test_the_environment_beats_the_default():
    resolved = resolve(
        file_values={},
        env_values={"electricity_rate_usd_per_kwh": 0.10},
        defaults={"electricity_rate_usd_per_kwh": 0.0},
    )
    assert resolved["electricity_rate_usd_per_kwh"].value == 0.10
    assert resolved["electricity_rate_usd_per_kwh"].source == "env"


def test_every_field_reports_a_source_even_when_unset():
    """The UI has to tell '0.00 because nobody set it' from '0.00 because
    someone set it to zero'."""
    resolved = resolve(
        {},
        {},
        {
            "electricity_rate_usd_per_kwh": 0.0,
            "local_only": False,
            "auto_restart_crashed_deployments": True,
        },
    )
    assert set(resolved) == set(MUTABLE_FIELDS)
    assert all(r.source == "default" for r in resolved.values())
    assert resolved["daily_spend_cap_usd"].value is None
