"""control_plane/keys: the key format, and KeyService against a real keys.db."""

from __future__ import annotations

import re
import sqlite3
import string

import pytest

from control_plane.contracts.auth import (
    KEY_EXPIRY_DAYS,
    KEY_ID_LEN,
    AllowlistExceedsOrg,
    ApiKey,
    AuthError,
    AuthErrorCode,
    AuthUnavailable,
    KeyPort,
    KeyStatus,
    LimitExceedsOrg,
)
from control_plane.contracts.routing import RoutingPolicy
from control_plane.keys import KeyService
from control_plane.keys import format as kf

DAY = 86400.0
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "keys.db"


@pytest.fixture
def svc(db, clock) -> KeyService:
    return KeyService.open(db, clock=clock)


def _key(svc: KeyService, **kw):
    kw.setdefault("name", "k")
    kw.setdefault("org_id", svc.default_org_id())
    kw.setdefault("created_by", "alice")
    kw.setdefault("expiry_days", 30)
    return svc.create_key(**kw)


def _code(exc_info) -> AuthErrorCode:
    return exc_info.value.code


# ------------------------------------------------------------------ format


def test_mint_1000_ids_are_10_lowercase_alnum_and_parse_back():
    alphabet = set(string.ascii_lowercase + string.digits)
    pattern = re.compile(kf.KEY_PATTERN)
    ids = set()
    for _ in range(1000):
        key_id, secret, raw = kf.mint()
        assert len(key_id) == KEY_ID_LEN and set(key_id) <= alphabet
        assert raw == f"drt_{key_id}_{secret}"
        assert kf.parse(raw) == (key_id, secret)  # secrets may contain "_"
        assert pattern.fullmatch(raw)
        ids.add(key_id)
    assert len(ids) == 1000


@pytest.mark.parametrize(
    "raw",
    [
        "sk_abcdefghij_" + "x" * 43,  # wrong prefix
        "DRT_abcdefghij_" + "x" * 43,
        "drt_abcdefghi_" + "x" * 43,  # 9-char id
        "drt_abcdefghijk_" + "x" * 43,  # 11-char id
        "drt_ABCDEFGHIJ_" + "x" * 43,  # uppercase id
        "drt_abcdefghi-_" + "x" * 43,  # bad charset
        "drt_abcdefghij_",  # empty secret
        "drt_abcdefghij",  # no separator
        "",
    ],
)
def test_parse_rejects(raw):
    assert kf.parse(raw) is None


def test_hash_and_verify():
    _, secret, _ = kf.mint()
    h = kf.hash_secret(secret)
    assert h != secret and len(h) == 64
    assert kf.verify(secret, h)
    assert not kf.verify(secret + "x", h)


# ------------------------------------------------------------- open / orgs


def test_implements_keyport(svc):
    assert isinstance(svc, KeyPort)


def test_default_org_created_once_and_stable_across_reopen(db, clock):
    a = KeyService.open(db, clock=clock)
    org_id = a.default_org_id()
    org = a.get_org(org_id)
    assert org.name == "default" and org.created_by == "system"
    assert re.fullmatch(r"org_[a-z0-9]{8}", org_id)
    b = KeyService.open(db, clock=clock)
    assert b.default_org_id() == org_id
    assert [o.org_id for o in b.list_orgs()] == [org_id]


def test_default_org_cannot_be_deleted(svc):
    with pytest.raises(ValueError, match="default org"):
        svc.delete_org(svc.default_org_id(), actor="a")


def test_unopenable_path_raises_auth_unavailable(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(AuthUnavailable):
        KeyService.open(blocker / "keys.db")


def test_org_names_unique_among_live_orgs(svc):
    org = svc.create_org("acme", actor="a")
    with pytest.raises(ValueError, match="acme"):
        svc.create_org("acme", actor="a")
    svc.delete_org(org.org_id, actor="a")
    again = svc.create_org("acme", actor="a")
    assert again.org_id != org.org_id
    other = svc.create_org("beta", actor="a")
    with pytest.raises(ValueError, match="acme"):
        svc.update_org(other.org_id, actor="a", name="acme")


def test_delete_org_with_live_keys_names_the_count(svc):
    org = svc.create_org("acme", actor="a")
    _key(svc, org_id=org.org_id)
    k2 = _key(svc, org_id=org.org_id)
    with pytest.raises(ValueError, match="2 key"):
        svc.delete_org(org.org_id, actor="a")
    svc.delete_key(k2.key.key_id, actor="a")
    with pytest.raises(ValueError, match="1 key"):
        svc.delete_org(org.org_id, actor="a")


def test_soft_deleted_org_refuses_its_keys_and_new_keys(svc):
    org = svc.create_org("acme", actor="a")
    m = _key(svc, org_id=org.org_id)
    svc.delete_key(m.key.key_id, actor="a")
    svc.delete_org(org.org_id, actor="a")
    assert svc.get_org(org.org_id).deleted_at == T0
    assert org.org_id not in [o.org_id for o in svc.list_orgs()]
    assert org.org_id in [o.org_id for o in svc.list_orgs(include_deleted=True)]
    with pytest.raises(ValueError, match="deleted"):
        _key(svc, org_id=org.org_id)


def test_update_org_fields_and_validation(svc):
    org = svc.create_org("acme", actor="a", limits={"rpm": 100})
    up = svc.update_org(
        org.org_id,
        actor="a",
        allowed_models=["m1", "m2"],
        default_key_limits={"requests_cycle": 5},
        default_key_expiry_days=10,
        routing_policy="round_robin",
    )
    assert up.limits == {"rpm": 100.0}
    assert up.allowed_models == ["m1", "m2"]
    assert up.default_key_limits == {"requests_cycle": 5.0}
    assert up.default_key_expiry_days == 10
    assert up.routing_policy is RoutingPolicy.ROUND_ROBIN
    with pytest.raises(ValueError, match="default_key_expiry_days"):
        svc.update_org(org.org_id, actor="a", default_key_expiry_days=2)
    with pytest.raises(ValueError, match="bogus"):
        svc.update_org(org.org_id, actor="a", bogus=1)
    with pytest.raises(ValueError, match="routing_policy"):
        svc.update_org(org.org_id, actor="a", routing_policy="nope")
    assert svc.update_org(org.org_id, actor="a", allowed_models=None).allowed_models is None


# ------------------------------------------------------------------ create


@pytest.mark.parametrize("days", KEY_EXPIRY_DAYS)
def test_every_expiry_option_round_trips(svc, days, db, clock):
    m = _key(svc, expiry_days=days)
    assert m.key.expires_at == T0 + days * DAY
    assert KeyService.open(db, clock=clock).get_key(m.key.key_id).expires_at == T0 + days * DAY


@pytest.mark.parametrize("days", [2, 0, -1, 366, 30.5, True])
def test_expiry_outside_the_options_is_refused(svc, days):
    with pytest.raises(ValueError, match="365"):
        _key(svc, expiry_days=days)


def test_non_admin_key_without_expiry_is_refused(svc):
    with pytest.raises(ValueError, match="expiry"):
        _key(svc, expiry_days=None)


def test_org_defaults_fill_expiry_and_limits(svc):
    org = svc.create_org(
        "acme", actor="a", default_key_expiry_days=50, default_key_limits={"rpm": 7}
    )
    m = _key(svc, org_id=org.org_id, expiry_days=None)
    assert m.key.expires_at == T0 + 50 * DAY
    assert m.key.limits == {"rpm": 7.0}
    explicit = _key(svc, org_id=org.org_id, limits={"tpm": 9})
    assert explicit.key.limits == {"tpm": 9.0}


@pytest.mark.parametrize(
    "limits, needle",
    [
        ({"rps": 1}, "rps"),
        ({"model::rpm": 1}, "model::rpm"),
        ({"model:gpt:qps": 1}, "model:gpt:qps"),
        ({"rpm": 0}, "rpm"),
        ({"rpm": -3}, "rpm"),
        ({"rpm": "10"}, "rpm"),
        ({"rpm": True}, "rpm"),
    ],
)
def test_bad_limits_are_refused_naming_the_field(svc, limits, needle):
    with pytest.raises(ValueError, match=re.escape(needle)):
        _key(svc, limits=limits)


def test_model_limits_with_colons_in_the_id_are_accepted(svc):
    m = _key(svc, limits={"model:org/model:v2:tpm": 1000, "max_concurrent": 2})
    assert m.key.limits == {"model:org/model:v2:tpm": 1000.0, "max_concurrent": 2.0}


def test_secret_never_stored_in_clear(svc, db):
    m = _key(svc)
    secret = kf.parse(m.secret_once)[1]
    assert secret not in db.read_bytes().decode("latin-1")
    assert not hasattr(m.key, "secret_hash")
    assert m.secret_once not in repr(m.key)


def test_unknown_org_is_keyerror(svc):
    with pytest.raises(KeyError):
        _key(svc, org_id="org_nope0000")


# ------------------------------------------------------------ authenticate


def test_authenticate_success_updates_last_used(svc, clock):
    m = _key(svc)
    clock.t = T0 + 10
    k = svc.authenticate(m.secret_once, ip="10.0.0.1")
    assert isinstance(k, ApiKey) and k.key_id == m.key.key_id
    stored = svc.get_key(m.key.key_id)
    assert (stored.last_used_at, stored.last_used_ip) == (T0 + 10, "10.0.0.1")
    # within the throttle window, same ip: the row is not rewritten
    svc.authenticate(m.secret_once, ip="10.0.0.1", now=T0 + 12)
    assert svc.get_key(m.key.key_id).last_used_at == T0 + 10
    # a new ip is always recorded
    svc.authenticate(m.secret_once, ip="10.0.0.2", now=T0 + 13)
    assert svc.get_key(m.key.key_id).last_used_ip == "10.0.0.2"


@pytest.mark.parametrize(
    "mangle",
    [
        lambda raw: "garbage",
        lambda raw: "drt_zzzzzzzzzz_" + "x" * 43,  # unknown id
        lambda raw: raw[:-1] + ("A" if raw[-1] != "A" else "B"),  # wrong secret
    ],
)
def test_invalid_keys(svc, mangle):
    m = _key(svc)
    with pytest.raises(AuthError) as e:
        svc.authenticate(mangle(m.secret_once), ip=None)
    assert _code(e) is AuthErrorCode.INVALID_API_KEY
    assert m.secret_once not in e.value.message


def test_disabled_beats_expired_and_wrong_secret_beats_both(svc, clock):
    m = _key(svc, expiry_days=1)
    svc.disable_key(m.key.key_id, actor="a", reason="leak")
    later = T0 + 2 * DAY
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once, ip=None, now=later)
    assert _code(e) is AuthErrorCode.API_KEY_DISABLED
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once[:-2] + "zz", ip=None, now=later)
    assert _code(e) is AuthErrorCode.INVALID_API_KEY


def test_expired_flips_status_at_the_boundary(svc):
    m = _key(svc, expiry_days=1)
    exp = T0 + DAY
    svc.authenticate(m.secret_once, ip=None, now=exp - 0.001)
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once, ip=None, now=exp)
    assert _code(e) is AuthErrorCode.API_KEY_EXPIRED
    assert svc.get_key(m.key.key_id).status is KeyStatus.EXPIRED
    # stays expired even if asked with an earlier clock
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once, ip=None, now=T0)
    assert _code(e) is AuthErrorCode.API_KEY_EXPIRED


def test_expiry_forced_by_editing_the_row_is_honoured(svc, db, clock):
    """The walkthrough forces expiry with the sqlite3 CLI; a second connection
    writing expires_at must be seen by the running service."""
    m = _key(svc, expiry_days=1)
    with sqlite3.connect(db) as c:
        c.execute("UPDATE api_keys SET expires_at=? WHERE key_id=?", (T0 - 60, m.key.key_id))
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once, ip=None)
    assert _code(e) is AuthErrorCode.API_KEY_EXPIRED


def test_enable_restores_and_refuses_expired(svc, clock):
    m = _key(svc, expiry_days=1)
    d = svc.disable_key(m.key.key_id, actor="bob", reason="why")
    assert (d.status, d.disabled_by, d.disabled_reason, d.disabled_at) == (
        KeyStatus.DISABLED, "bob", "why", T0,
    )
    e = svc.enable_key(m.key.key_id, actor="bob")
    assert e.status is KeyStatus.ACTIVE and e.disabled_at is None
    svc.authenticate(m.secret_once, ip=None)
    clock.t = T0 + 2 * DAY
    svc.disable_key(m.key.key_id, actor="bob")
    with pytest.raises(ValueError, match="expired"):
        svc.enable_key(m.key.key_id, actor="bob")


# ---------------------------------------------------------- delete / rotate


def test_soft_delete_keeps_row_hides_it_and_refuses_it(svc):
    m = _key(svc, name="gone", limits={"rpm": 3})
    svc.authenticate(m.secret_once, ip="1.2.3.4")
    svc.delete_key(m.key.key_id, actor="a")
    k = svc.get_key(m.key.key_id)
    assert k is not None and k.deleted_at == T0
    assert (k.name, k.org_id, k.limits, k.last_used_ip) == (
        "gone", svc.default_org_id(), {"rpm": 3.0}, "1.2.3.4",
    )
    assert m.key.key_id not in [x.key_id for x in svc.list_keys()]
    assert m.key.key_id in [x.key_id for x in svc.list_keys(include_deleted=True)]
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once, ip=None)
    assert _code(e) is AuthErrorCode.INVALID_API_KEY
    for mutate in (
        lambda: svc.update_key(m.key.key_id, actor="a", name="x"),
        lambda: svc.rotate_key(m.key.key_id, actor="a"),
        lambda: svc.enable_key(m.key.key_id, actor="a"),
    ):
        with pytest.raises(ValueError, match="deleted"):
            mutate()


def test_rotate_kills_old_secret_and_keeps_everything_else(svc):
    m = _key(svc, limits={"rpm": 5}, notes="n")
    r = svc.rotate_key(m.key.key_id, actor="a")
    assert r.secret_once != m.secret_once
    assert kf.parse(r.secret_once)[0] == m.key.key_id
    with pytest.raises(AuthError) as e:
        svc.authenticate(m.secret_once, ip=None)
    assert _code(e) is AuthErrorCode.INVALID_API_KEY
    assert svc.authenticate(r.secret_once, ip=None).key_id == m.key.key_id
    assert (r.key.created_at, r.key.expires_at, r.key.limits, r.key.notes) == (
        m.key.created_at, m.key.expires_at, {"rpm": 5.0}, "n",
    )


# ------------------------------------------------------------------ update


def test_update_key_fields(svc):
    m = _key(svc)
    k = svc.update_key(
        m.key.key_id,
        actor="a",
        name="renamed",
        notes="hi",
        limits={"requests_cycle": 5},
        allowed_models=["a", "b"],
        routing_policy=RoutingPolicy.FAILOVER,
        cloud_fallback_local_model="local-8b",
    )
    assert (k.name, k.notes, k.limits, k.allowed_models) == (
        "renamed", "hi", {"requests_cycle": 5.0}, ["a", "b"],
    )
    assert k.routing_policy is RoutingPolicy.FAILOVER
    assert k.cloud_fallback_local_model == "local-8b"
    k = svc.update_key(m.key.key_id, actor="a", routing_policy=None, allowed_models=None)
    assert k.routing_policy is None and k.allowed_models is None


@pytest.mark.parametrize("field", ["expires_at", "expiry_days", "status", "secret_hash"])
def test_update_key_refuses_non_editable_fields(svc, field):
    m = _key(svc)
    with pytest.raises(ValueError, match=field):
        svc.update_key(m.key.key_id, actor="a", **{field: 1})


# ------------------------------------------------------------------- sweep


def test_sweep_expired_flips_only_past_active_non_admin_keys(svc):
    short = _key(svc, expiry_days=1)
    long = _key(svc, expiry_days=365)
    disabled = _key(svc, expiry_days=1)
    svc.disable_key(disabled.key.key_id, actor="a")
    admin = _key(svc, expiry_days=None, is_admin_key=True, admin_username="alice")
    later = T0 + 400 * DAY
    assert svc.sweep_expired(now=T0 + DAY - 1) == []
    swept = svc.sweep_expired(now=T0 + 2 * DAY)
    assert swept == [short.key.key_id]
    assert svc.get_key(short.key.key_id).status is KeyStatus.EXPIRED
    assert svc.get_key(disabled.key.key_id).status is KeyStatus.DISABLED
    assert sorted(svc.sweep_expired(now=later)) == [long.key.key_id]
    assert svc.get_key(admin.key.key_id).status is KeyStatus.ACTIVE


# --------------------------------------------------------------- admin key


def test_admin_key_never_expires(svc):
    m = _key(svc, expiry_days=None, is_admin_key=True, admin_username="alice", name="alice")
    assert m.key.expires_at is None and m.key.is_admin_key
    assert m.key.admin_username == "alice"
    assert svc.authenticate(m.secret_once, ip=None, now=T0 + 10_000 * DAY).key_id == m.key.key_id
    assert svc.sweep_expired(now=T0 + 10_000 * DAY) == []


def test_admin_key_rules(svc):
    with pytest.raises(ValueError, match="never expires"):
        _key(svc, expiry_days=30, is_admin_key=True, admin_username="alice")
    with pytest.raises(ValueError, match="admin_username"):
        _key(svc, expiry_days=None, is_admin_key=True)
    with pytest.raises(ValueError, match="admin_username"):
        _key(svc, admin_username="alice")


def test_admin_key_ignores_org_default_expiry(svc):
    org = svc.create_org("acme", actor="a", default_key_expiry_days=1)
    m = _key(svc, org_id=org.org_id, expiry_days=None, is_admin_key=True, admin_username="x")
    assert m.key.expires_at is None


# ------------------------------------------------------------------ policy


def test_validate_against_org_names_the_field_and_both_numbers(svc):
    org = svc.create_org("acme", actor="a", limits={"rpm": 100, "model:m1:tpm": 50})
    svc.validate_against_org(
        org=org, limits={"rpm": 100, "tpm": 10**9}, allowed_models=None, exposed=[]
    )
    with pytest.raises(LimitExceedsOrg) as e:
        svc.validate_against_org(org=org, limits={"rpm": 101}, allowed_models=None, exposed=[])
    assert e.value.field == "rpm" and e.value.code is AuthErrorCode.LIMIT_EXCEEDS_ORG
    assert "rpm" in e.value.message and "101" in e.value.message and "100" in e.value.message
    with pytest.raises(LimitExceedsOrg) as e:
        svc.validate_against_org(
            org=org, limits={"model:m1:tpm": 51}, allowed_models=None, exposed=[]
        )
    assert e.value.field == "model:m1:tpm"


def test_allowlist_subset_rule(svc):
    explicit = svc.create_org("acme", actor="a", allowed_models=["a", "b"])
    svc.validate_against_org(org=explicit, limits={}, allowed_models=["a"], exposed=[])
    with pytest.raises(AllowlistExceedsOrg) as e:
        svc.validate_against_org(
            org=explicit, limits={}, allowed_models=["c", "a", "z"], exposed=["c", "z"]
        )
    assert e.value.models == ["c", "z"]
    assert e.value.code is AuthErrorCode.ALLOWLIST_EXCEEDS_ORG
    # an org with no list allows what is exposed
    open_org = svc.get_org(svc.default_org_id())
    svc.validate_against_org(org=open_org, limits={}, allowed_models=["x"], exposed=["x", "y"])
    with pytest.raises(AllowlistExceedsOrg):
        svc.validate_against_org(org=open_org, limits={}, allowed_models=["q"], exposed=["x"])


def test_effective_allowlist(svc):
    explicit = svc.create_org("acme", actor="a", allowed_models=["a", "b", "offline"])
    inherit = _key(svc, org_id=explicit.org_id).key
    narrowed = _key(svc, org_id=explicit.org_id, allowed_models=["b", "c"]).key
    # an explicit org list is not intersected with what is exposed right now
    assert svc.effective_allowlist(inherit, explicit, ["a"]) == ["a", "b", "offline"]
    assert svc.effective_allowlist(narrowed, explicit, ["a", "b", "c"]) == ["b"]
    open_org = svc.get_org(svc.default_org_id())
    k = _key(svc).key
    assert svc.effective_allowlist(k, open_org, ["x", "y"]) == ["x", "y"]
    k2 = _key(svc, allowed_models=["y", "z"]).key
    assert svc.effective_allowlist(k2, open_org, ["x", "y"]) == ["y"]


# ----------------------------------------------------------------- persist


def test_fields_survive_reopen(db, clock):
    a = KeyService.open(db, clock=clock)
    org = a.create_org(
        "acme", actor="a", limits={"rpm": 10}, routing_policy=RoutingPolicy.COST_AWARE
    )
    m = a.create_key(
        name="k",
        org_id=org.org_id,
        created_by="alice",
        expiry_days=10,
        limits={"model:m:usd_cycle": 2.5},
        allowed_models=["m"],
        routing_policy="local_first",
        notes="note",
        cloud_fallback_local_model="local",
    )
    a.authenticate(m.secret_once, ip="9.9.9.9", now=T0 + 1)
    a.disable_key(m.key.key_id, actor="bob", reason="r")
    before_key = a.get_key(m.key.key_id)
    before_org = a.get_org(org.org_id)
    a.close()
    b = KeyService.open(db, clock=clock)
    assert b.get_key(m.key.key_id) == before_key
    assert b.get_org(org.org_id) == before_org
    assert before_key.last_used_at == T0 + 1 and before_key.last_used_ip == "9.9.9.9"
    b.enable_key(m.key.key_id, actor="bob")
    assert b.authenticate(m.secret_once, ip=None, now=T0 + 2).key_id == m.key.key_id


def test_two_services_on_one_file_see_each_others_writes(db, clock):
    a = KeyService.open(db, clock=clock)
    b = KeyService.open(db, clock=clock)
    assert a.default_org_id() == b.default_org_id()
    m = a.create_key(name="k", org_id=a.default_org_id(), created_by="x", expiry_days=1)
    b.disable_key(m.key.key_id, actor="x")
    with pytest.raises(AuthError) as e:
        a.authenticate(m.secret_once, ip=None)
    assert _code(e) is AuthErrorCode.API_KEY_DISABLED
