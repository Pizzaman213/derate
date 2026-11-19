"""AuthService: admins, lockout, sessions, audit -- against a real auth.db."""

from __future__ import annotations

import sqlite3

import pytest
from argon2 import PasswordHasher

from control_plane.auth import AuthService, hash_cookie
from control_plane.contracts.auth import (
    EMAIL_MAX_LEN,
    LOGIN_DELAY_MAX_S,
    LOGIN_LOCK_BASE_S,
    LOGIN_LOCK_MAX_S,
    LOGIN_LOCK_THRESHOLD,
    LOGIN_LOCK_USERNAME_MAX_S,
    LOGIN_LOCK_WINDOW_S,
    LOGIN_PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    SESSION_IDLE_S,
    SESSION_MAX_S,
    AuditAction,
    AuthPort,
    AuthUnavailable,
    InvalidCredentials,
    LoginLocked,
)

PW = "correct-horse-battery"
T0 = 1_800_000_000.0


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class CountingHasher(PasswordHasher):
    """argon2id with cheap parameters, counting every verify."""

    def __init__(self, **kw) -> None:
        super().__init__(time_cost=1, memory_cost=8, parallelism=1, **kw)
        self.verifies = 0
        self.verified_hashes: list[str] = []

    def verify(self, hash, password):  # noqa: A002 -- argon2's name
        self.verifies += 1
        self.verified_hashes.append(hash)
        return super().verify(hash, password)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def hasher():
    return CountingHasher()


@pytest.fixture
def svc(tmp_path, clock, hasher):
    return AuthService.open(tmp_path / "auth.db", clock=clock, hasher=hasher)


def _fail(svc, user, ip):
    with pytest.raises(InvalidCredentials):
        svc.login(user, "wrong-password-xx", ip=ip)


def _actions(svc) -> list[AuditAction]:
    entries, _ = svc.audit_page(page_size=1000)
    return [e.action for e in entries]


# -- admins ----------------------------------------------------------------


def test_implements_the_port(svc):
    assert isinstance(svc, AuthPort)


def test_admin_exists_flips_once_and_hash_is_argon2id(tmp_path, clock):
    svc = AuthService.open(tmp_path / "auth.db", clock=clock)  # default argon2 params
    assert svc.admin_exists() is False
    svc.create_admin("root", PW, created_by=None, ip="10.0.0.1")
    assert svc.admin_exists() is True
    svc.create_admin("second", PW + "2", created_by="root", ip=None)
    assert svc.admin_exists() is True
    stored = sqlite3.connect(tmp_path / "auth.db").execute(
        "SELECT password_hash FROM admins WHERE username='root'"
    ).fetchone()[0]
    assert stored != PW and PW not in stored
    assert stored.startswith("$argon2id$")


def test_weak_password_is_refused_naming_the_minimum(svc):
    with pytest.raises(ValueError, match=str(PASSWORD_MIN_LEN)):
        svc.create_admin("root", "x" * (PASSWORD_MIN_LEN - 1), created_by=None, ip=None)
    svc.create_admin("root", "x" * PASSWORD_MIN_LEN, created_by=None, ip=None)


@pytest.mark.parametrize("bad", ["", "   ", "a" * 65, "has space", "tab\there", "a/b", "ü"])
def test_bad_usernames_are_refused(svc, bad):
    with pytest.raises(ValueError):
        svc.create_admin(bad, PW, created_by=None, ip=None)
    assert svc.admin_exists() is False


@pytest.mark.parametrize("good", ["admin!", "semi;colon", "#ops$", "a+b=c?", "~root^", "x%y&z*"])
def test_usernames_may_carry_punctuation(svc, good):
    assert svc.create_admin(good, PW, created_by=None, ip=None).username == good
    svc.login(good, PW, ip=None)


def test_username_is_stripped_and_duplicates_refused(svc):
    assert svc.create_admin("  a.b-c_d@x  ", PW, created_by=None, ip=None).username == "a.b-c_d@x"
    with pytest.raises(ValueError, match="already exists"):
        svc.create_admin("a.b-c_d@x", PW, created_by="a.b-c_d@x", ip=None)
    assert [a.username for a in svc.list_admins()] == ["a.b-c_d@x"]


def test_email_is_stored_and_signs_in_case_insensitively(svc):
    admin = svc.create_admin("root", PW, email="  Root@Example.com ", created_by=None, ip=None)
    assert admin.email == "Root@Example.com"
    sess, _ = svc.login("root@example.COM", PW, ip=None)
    assert sess.username == "root"
    assert svc.list_admins()[0].email == "Root@Example.com"


def test_duplicate_email_is_refused(svc):
    svc.create_admin("root", PW, email="root@example.com", created_by=None, ip=None)
    with pytest.raises(ValueError, match="already exists"):
        svc.create_admin("ops", PW, email="ROOT@example.com", created_by="root", ip=None)
    assert [a.username for a in svc.list_admins()] == ["root"]


@pytest.mark.parametrize("bad", ["", "   ", "no-at-sign", "a@b", "a b@c.d", "a@b/c.d", "x" * 250 + "@a.bc"])
def test_bad_emails_are_refused(svc, bad):
    with pytest.raises(ValueError):
        svc.create_admin("root", PW, email=bad, created_by=None, ip=None)
    assert svc.admin_exists() is False


def test_an_exact_username_beats_another_admins_email(svc):
    svc.create_admin("ops@example.com", PW, email="a@example.com", created_by=None, ip=None)
    svc.create_admin("root", PW + "2", email="ops@example.com", created_by="x", ip=None)
    assert svc.login("ops@example.com", PW, ip=None)[0].username == "ops@example.com"


def test_lock_is_on_the_typed_identifier_so_email_and_username_answer_alike(svc):
    """L4: a 429 must not link an email to its username. Failures through the
    email lock the email; the username then answers exactly as an unrelated
    name does."""
    svc.create_admin("root", PW, email="root@example.com", created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD):
        _fail(svc, "root@example.com", "1.1.1.1")
    with pytest.raises(LoginLocked):
        svc.login("ROOT@example.com", PW, ip="1.1.1.1")  # case-folded
    with pytest.raises(InvalidCredentials):
        svc.login("root", "wrong-password-xx", ip="1.1.1.1")
    with pytest.raises(InvalidCredentials):
        svc.login("someone-else", "wrong-password-xx", ip="1.1.1.1")
    # the audit row still names the admin the email resolved to
    entries, _ = svc.audit_page(action=AuditAction.LOGIN_FAILED, page_size=100)
    assert {e.actor for e in entries} == {"root", "someone-else"}


def test_last_admin_cannot_be_removed(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    with pytest.raises(ValueError, match="last admin"):
        svc.remove_admin("root", actor="root", ip=None)
    svc.create_admin("ops", PW, created_by="root", ip=None)
    svc.remove_admin("root", actor="ops", ip=None)
    assert [a.username for a in svc.list_admins()] == ["ops"]
    with pytest.raises(ValueError, match="last admin"):
        svc.remove_admin("ops", actor="ops", ip=None)
    with pytest.raises(ValueError, match="no admin"):
        svc.remove_admin("ghost", actor="ops", ip=None)


def test_removing_an_admin_kills_their_sessions(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    svc.create_admin("ops", PW, created_by="root", ip=None)
    _, cookie = svc.login("ops", PW, ip=None)
    assert svc.session(cookie) is not None
    svc.remove_admin("ops", actor="root", ip=None)
    assert svc.session(cookie) is None
    with pytest.raises(InvalidCredentials):
        svc.login("ops", PW, ip=None)


def test_change_password(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    _, cookie = svc.login("root", PW, ip=None)
    with pytest.raises(InvalidCredentials):
        svc.change_password("root", "not-the-password", "new-password-123", actor="root", ip=None)
    with pytest.raises(ValueError, match=str(PASSWORD_MIN_LEN)):
        svc.change_password("root", PW, "short", actor="root", ip=None)
    assert svc.session(cookie) is not None  # failed changes revoke nothing
    svc.change_password("root", PW, "new-password-123", actor="root", ip=None)
    assert svc.session(cookie) is None  # every session of that admin is revoked
    with pytest.raises(InvalidCredentials):
        svc.login("root", PW, ip="1.1.1.1")
    svc.login("root", "new-password-123", ip="1.1.1.2")


# -- login / lockout -------------------------------------------------------


def test_login_returns_session_and_stores_only_the_cookie_hash(svc, tmp_path, clock):
    svc.create_admin("root", PW, created_by=None, ip=None)
    sess, cookie = svc.login("root", PW, ip="10.0.0.9")
    assert sess.session_id == hash_cookie(cookie) != cookie
    assert sess.expires_at == T0 + SESSION_MAX_S
    assert sess.ip == "10.0.0.9" and sess.username == "root"
    raw = (tmp_path / "auth.db").read_bytes() + (tmp_path / "auth.db-wal").read_bytes()
    assert cookie.encode() not in raw


def test_unknown_user_login_still_runs_a_verify(svc, hasher):
    svc.create_admin("root", PW, created_by=None, ip=None)
    before = hasher.verifies
    with pytest.raises(InvalidCredentials):
        svc.login("nobody", PW, ip="1.2.3.4")
    assert hasher.verifies == before + 1
    assert hasher.verified_hashes[-1].startswith("$argon2id$")
    # the unknown user and a wrong password give the same error
    with pytest.raises(InvalidCredentials) as wrong:
        svc.login("root", "nope-nope-nope", ip="1.2.3.5")
    assert wrong.value.code.value == "invalid_credentials"


def test_lockout_per_pair_with_backoff(svc, clock, hasher):
    svc.create_admin("root", PW, created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD):
        _fail(svc, "root", "10.0.0.1")
    with pytest.raises(LoginLocked) as locked:
        svc.login("root", PW, ip="10.0.0.1")  # even the right password
    assert locked.value.retry_after_s == pytest.approx(LOGIN_LOCK_BASE_S)
    assert locked.value.code.value == "login_locked"

    clock.t += 10
    with pytest.raises(LoginLocked) as locked:
        svc.login("root", PW, ip="10.0.0.1")
    assert locked.value.retry_after_s == pytest.approx(LOGIN_LOCK_BASE_S - 10)

    clock.t += LOGIN_LOCK_BASE_S  # lock over; a 6th failure doubles it
    _fail(svc, "root", "10.0.0.1")
    with pytest.raises(LoginLocked) as locked:
        svc.login("root", PW, ip="10.0.0.1")
    assert locked.value.retry_after_s == pytest.approx(LOGIN_LOCK_BASE_S * 2)

    # another username is untouched
    svc.create_admin("ops", PW, created_by="root", ip=None)
    svc.login("ops", PW, ip="10.0.0.1")

    clock.t += LOGIN_LOCK_BASE_S * 2
    svc.login("root", PW, ip="10.0.0.1")


def test_spread_failures_delay_the_username_and_never_refuse(svc, clock):
    """A3: failures spread over addresses below the pair threshold hold the
    username as a delay; the right password still signs in."""
    svc.create_admin("root", PW, created_by=None, ip=None)
    for i in range(LOGIN_LOCK_THRESHOLD - 1):
        _fail(svc, "root", "10.0.0.1")
        _fail(svc, "root", "10.0.0.2")
    assert svc.login_delay_s("10.9.9.9", "root") == pytest.approx(LOGIN_DELAY_MAX_S)
    assert svc.login_delay_s("10.9.9.9", "ops") == 0
    assert svc.login("root", PW, ip="10.9.9.9")[0].username == "root"


def test_failures_from_one_ip_lock_that_pair_only(svc):
    """M6: five failures for admin from A lock (admin, A) and nothing else."""
    svc.create_admin("root", PW, created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD):
        _fail(svc, "root", "6.6.6.6")
    with pytest.raises(LoginLocked) as locked:
        svc.login("root", PW, ip="6.6.6.6")
    assert locked.value.retry_after_s == pytest.approx(LOGIN_LOCK_BASE_S)
    # the same admin from another address is not locked at all
    assert svc.login("root", PW, ip="7.7.7.7")[0].username == "root"


def test_spread_failures_delay_the_username_for_at_most_the_cap(svc, clock, tmp_path):
    svc.create_admin("root", PW, created_by=None, ip=None)
    conn = sqlite3.connect(tmp_path / "auth.db")
    conn.executemany(
        "INSERT INTO login_attempts(ts, username, ip, ok) VALUES(?, 'root', ?, 0)",
        [(clock.t, f"10.1.0.{i}") for i in range(40)],  # one each: no pair is locked
    )
    conn.commit()
    assert svc.login_delay_s("7.7.7.7", "root") == pytest.approx(LOGIN_DELAY_MAX_S)
    svc.login("root", PW, ip="7.7.7.7")  # delayed by the gateway, not refused
    clock.t += LOGIN_LOCK_USERNAME_MAX_S
    assert svc.login_delay_s("7.7.7.7", "root") == 0


def test_the_attempt_row_is_written_with_the_lock_read(svc, tmp_path, monkeypatch):
    """A6: the failure row exists before the password is verified, so a
    concurrent guess sees it; a success flips that same row to ok=1."""
    svc.create_admin("root", PW, created_by=None, ip=None)
    seen = []
    real = svc._verify

    def spy(stored, password):
        seen.append(
            sqlite3.connect(tmp_path / "auth.db")
            .execute("SELECT ok FROM login_attempts ORDER BY rowid DESC LIMIT 1")
            .fetchone()
        )
        return real(stored, password)

    monkeypatch.setattr(svc, "_verify", spy)
    svc.login("root", PW, ip="1.1.1.1")
    assert seen == [(0,)]
    rows = sqlite3.connect(tmp_path / "auth.db").execute(
        "SELECT ok FROM login_attempts"
    ).fetchall()
    assert rows == [(1,)]


def test_failures_across_usernames_from_one_ip_delay_but_never_refuse(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    assert svc.login_delay_s("6.6.6.6") == 0
    for i in range(LOGIN_LOCK_THRESHOLD):
        _fail(svc, f"guess{i}", "6.6.6.6")
    assert svc.login_delay_s("6.6.6.6") == 1
    for i in range(10):
        _fail(svc, f"more{i}", "6.6.6.6")
    assert svc.login_delay_s("6.6.6.6") == LOGIN_DELAY_MAX_S
    assert svc.login_delay_s("7.7.7.7") == 0
    svc.login("root", PW, ip="6.6.6.6")  # a delay, not a lock


def test_lock_onset_is_audited_once(svc, tmp_path):
    svc.create_admin("root", PW, created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD - 1):
        _fail(svc, "root", "1.1.1.1")
    assert AuditAction.LOGIN_LOCKED not in _actions(svc)
    _fail(svc, "root", "1.1.1.1")
    for _ in range(3):
        with pytest.raises(LoginLocked):
            svc.login("root", PW, ip="1.1.1.1")
    locked = [e for e in svc.audit_page(page_size=1000)[0] if e.action is AuditAction.LOGIN_LOCKED]
    assert len(locked) == 1
    assert locked[0].ip == "1.1.1.1" and locked[0].actor == "root"
    assert locked[0].after["hold_s"] == pytest.approx(LOGIN_LOCK_BASE_S)


@pytest.mark.parametrize(
    "username,password",
    [("x" * (EMAIL_MAX_LEN + 1), PW), ("root", "p" * (LOGIN_PASSWORD_MAX_LEN + 1)), ("", PW)],
)
def test_oversized_login_input_is_refused_without_a_row(svc, hasher, tmp_path, username, password):
    svc.create_admin("root", PW, created_by=None, ip=None)
    rows = lambda: sqlite3.connect(tmp_path / "auth.db").execute(  # noqa: E731
        "SELECT (SELECT COUNT(*) FROM audit) + (SELECT COUNT(*) FROM login_attempts)"
    ).fetchone()[0]
    before, verifies = rows(), hasher.verifies
    with pytest.raises(InvalidCredentials):
        svc.login(username, password, ip="1.1.1.1")
    assert rows() == before
    assert hasher.verifies == verifies + 1  # the same argon2 work as any refusal


def test_one_below_threshold_is_not_locked(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD - 1):
        _fail(svc, "root", "1.1.1.1")
    svc.login("root", PW, ip="1.1.1.1")


def test_locked_login_neither_verifies_nor_records(svc, hasher, tmp_path):
    svc.create_admin("root", PW, created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD):
        _fail(svc, "root", "1.1.1.1")
    count = lambda: sqlite3.connect(tmp_path / "auth.db").execute(  # noqa: E731
        "SELECT COUNT(*) FROM login_attempts"
    ).fetchone()[0]
    attempts, verifies = count(), hasher.verifies
    for _ in range(3):
        with pytest.raises(LoginLocked) as locked:
            svc.login("root", PW, ip="1.1.1.1")
        # hammering while locked does not extend the lock
        assert locked.value.retry_after_s == pytest.approx(LOGIN_LOCK_BASE_S)
    assert hasher.verifies == verifies
    assert count() == attempts


def test_backoff_is_capped_and_failures_age_out(svc, clock, tmp_path):
    svc.create_admin("root", PW, created_by=None, ip=None)
    for _ in range(LOGIN_LOCK_THRESHOLD):
        _fail(svc, "root", None)
    # push the count far past the cap by writing failures directly
    conn = sqlite3.connect(tmp_path / "auth.db")
    conn.executemany(
        "INSERT INTO login_attempts(ts, username, ip, ok) VALUES(?, 'root', NULL, 0)",
        [(clock.t,)] * 20,
    )
    conn.commit()
    with pytest.raises(LoginLocked) as locked:
        svc.login("root", PW, ip=None)
    assert locked.value.retry_after_s == pytest.approx(LOGIN_LOCK_MAX_S)
    clock.t += LOGIN_LOCK_WINDOW_S + 1  # every failure is now outside the window
    svc.login("root", PW, ip=None)


def test_rehash_on_login_when_parameters_change(tmp_path, clock):
    path = tmp_path / "auth.db"
    AuthService.open(path, clock=clock, hasher=CountingHasher()).create_admin(
        "root", PW, created_by=None, ip=None
    )
    old = sqlite3.connect(path).execute("SELECT password_hash FROM admins").fetchone()[0]
    stronger = PasswordHasher(time_cost=2, memory_cost=16, parallelism=1)
    svc = AuthService.open(path, clock=clock, hasher=stronger)
    svc.login("root", PW, ip=None)
    new = sqlite3.connect(path).execute("SELECT password_hash FROM admins").fetchone()[0]
    assert new != old and not stronger.check_needs_rehash(new)
    svc.login("root", PW, ip=None)  # the rehashed password still works


# -- sessions --------------------------------------------------------------


def test_session_idle_expiry(svc, clock):
    svc.create_admin("root", PW, created_by=None, ip=None)
    _, cookie = svc.login("root", PW, ip=None)
    assert svc.session(cookie, now=T0 + SESSION_IDLE_S) is not None  # touches last_seen
    assert svc.session(cookie, now=T0 + 2 * SESSION_IDLE_S) is not None  # exactly idle limit
    assert svc.session(cookie, now=T0 + 3 * SESSION_IDLE_S + 1) is None
    # and the row is gone, not just hidden
    assert svc.session(cookie, now=T0 + 2 * SESSION_IDLE_S) is None


def test_session_absolute_expiry_despite_activity(svc, clock):
    svc.create_admin("root", PW, created_by=None, ip=None)
    _, cookie = svc.login("root", PW, ip=None)
    t = T0
    while t + 86400 < T0 + SESSION_MAX_S:
        t += 86400
        assert svc.session(cookie, now=t) is not None
    assert svc.session(cookie, now=T0 + SESSION_MAX_S) is None


def test_last_seen_is_written_at_most_once_a_minute(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    _, cookie = svc.login("root", PW, ip=None)
    assert svc.session(cookie, now=T0 + 30).last_seen_at == T0
    assert svc.session(cookie, now=T0 + 61).last_seen_at == T0 + 61


def test_unknown_and_empty_cookies(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    assert svc.session("") is None
    assert svc.session("not-a-cookie") is None
    sess, cookie = svc.login("root", PW, ip=None)
    assert svc.session(sess.session_id) is None  # the stored hash is not a cookie


def test_logout_ends_only_that_session(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    _, a = svc.login("root", PW, ip=None)
    _, b = svc.login("root", PW, ip=None)
    svc.logout(a, ip="1.1.1.1")
    assert svc.session(a) is None
    assert svc.session(b) is not None
    svc.logout(a, ip=None)  # second logout is a no-op, not an error


def test_reopen_keeps_admins_and_sessions(tmp_path, clock, hasher):
    path = tmp_path / "auth.db"
    first = AuthService.open(path, clock=clock, hasher=hasher)
    first.create_admin("root", PW, created_by=None, ip=None)
    _, cookie = first.login("root", PW, ip=None)
    first.close()
    again = AuthService.open(path, clock=clock, hasher=hasher)
    assert again.admin_exists()
    assert again.session(cookie).username == "root"
    again.login("root", PW, ip=None)


# -- audit -----------------------------------------------------------------


def test_every_service_emitted_action_is_audited_without_secrets(svc, tmp_path):
    svc.create_admin("root", PW, created_by=None, ip="1.1.1.1")
    svc.create_admin("ops", PW, created_by="root", ip="1.1.1.1")
    _fail(svc, "root", "2.2.2.2")
    _, cookie = svc.login("root", PW, ip="2.2.2.2")
    svc.logout(cookie, ip="2.2.2.2")
    svc.change_password("root", PW, "brand-new-password", actor="root", ip="3.3.3.3")
    svc.remove_admin("ops", actor="root", ip="3.3.3.3")

    entries, total = svc.audit_page(page_size=1000)
    assert total == 7
    by_action = {e.action: e for e in entries}
    assert set(by_action) == {
        AuditAction.SETUP_ADMIN_CREATED,
        AuditAction.ADMIN_ADDED,
        AuditAction.LOGIN_FAILED,
        AuditAction.LOGIN,
        AuditAction.LOGOUT,
        AuditAction.PASSWORD_CHANGED,
        AuditAction.ADMIN_REMOVED,
    }
    assert by_action[AuditAction.SETUP_ADMIN_CREATED].actor == "root"
    assert by_action[AuditAction.ADMIN_ADDED].actor == "root"
    assert by_action[AuditAction.ADMIN_ADDED].target_id == "ops"
    assert by_action[AuditAction.LOGIN_FAILED].actor == "root"
    assert by_action[AuditAction.LOGIN_FAILED].target_kind == "admin"
    assert by_action[AuditAction.LOGOUT].ip == "2.2.2.2"
    assert by_action[AuditAction.ADMIN_REMOVED].before["username"] == "ops"

    dump = sqlite3.connect(tmp_path / "auth.db").execute(
        "SELECT group_concat(coalesce(before_json,'') || coalesce(after_json,''), '|') FROM audit"
    ).fetchone()[0]
    assert PW not in dump and "brand-new-password" not in dump and "argon2" not in dump


def test_audit_page_filters_and_pages(svc, clock):
    for i in range(5):
        clock.t = T0 + i
        svc.audit("root", AuditAction.KEY_CREATED, "key", f"k{i}_x%", after={"i": i})
    clock.t = T0 + 10
    svc.audit("ops", AuditAction.ORG_CREATED, "org", "acme")

    entries, total = svc.audit_page(page_size=2)
    assert total == 6 and [e.target_id for e in entries] == ["acme", "k4_x%"]  # newest first
    entries, _ = svc.audit_page(page=3, page_size=2)
    assert [e.target_id for e in entries] == ["k1_x%", "k0_x%"]

    assert svc.audit_page(actor="ops")[1] == 1
    assert svc.audit_page(actor="roo")[1] == 0  # actor is exact
    assert svc.audit_page(action="key_created")[1] == 5
    assert svc.audit_page(action=AuditAction.ORG_CREATED)[1] == 1
    assert svc.audit_page(target="k3")[0][0].target_id == "k3_x%"
    assert svc.audit_page(target="org")[1] == 1  # matches target_kind
    assert svc.audit_page(target="_x%")[1] == 5  # literal, not a LIKE pattern
    assert svc.audit_page(start=T0 + 1, end=T0 + 3)[1] == 2
    entry = svc.audit_page(target="k2")[0][0]
    assert entry.after == {"i": 2} and entry.before is None


def test_no_audit_delete_path_exists():
    import inspect

    import control_plane.auth.service as service
    import control_plane.auth.store as store

    for module in (service, store):
        src = inspect.getsource(module).upper()
        assert "DELETE FROM AUDIT" not in src
        assert "DROP TABLE" not in src


# -- fail closed -----------------------------------------------------------


def test_unopenable_paths_raise_auth_unavailable(tmp_path):
    with pytest.raises(AuthUnavailable):
        AuthService.open(tmp_path)  # a directory
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(AuthUnavailable):
        AuthService.open(blocker / "sub" / "auth.db")  # parent cannot be created
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not sqlite" * 100)
    with pytest.raises(AuthUnavailable):
        AuthService.open(garbage)


def test_foreign_schema_version_is_refused_not_wiped(tmp_path, clock, hasher):
    path = tmp_path / "auth.db"
    AuthService.open(path, clock=clock, hasher=hasher).create_admin(
        "root", PW, created_by=None, ip=None
    )
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    with pytest.raises(AuthUnavailable, match="v99"):
        AuthService.open(path, clock=clock, hasher=hasher)
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM admins").fetchone()[0] == 1


def test_v1_file_migrates_in_place(tmp_path, clock, hasher):
    path = tmp_path / "auth.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE admins (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_at    REAL NOT NULL,
            created_by    TEXT
        );
        INSERT INTO meta VALUES('schema_version', '1');
        """
    )
    conn.execute("INSERT INTO admins VALUES('root', ?, ?, NULL)", (hasher.hash(PW), T0))
    conn.commit()
    conn.close()

    svc = AuthService.open(path, clock=clock, hasher=hasher)
    assert [(a.username, a.email) for a in svc.list_admins()] == [("root", None)]
    assert svc.login("root", PW, ip=None)[0].username == "root"
    version = lambda: sqlite3.connect(path).execute(  # noqa: E731
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert version() == "4"
    svc.close()

    again = AuthService.open(path, clock=clock, hasher=hasher)
    assert version() == "4"
    again.create_admin("ops", PW, email="ops@example.com", created_by="root", ip=None)
    assert {a.username: a.email for a in again.list_admins()} == {
        "root": None,
        "ops": "ops@example.com",
    }


# -- v3: request context, sessions, revocation, password policy ---------------


def test_password_policy_refuses_the_username_the_email_and_the_denylist(svc):
    with pytest.raises(ValueError, match="username"):
        svc.create_admin("harbor", "my-Harbor-pass-1", created_by=None, ip=None)
    with pytest.raises(ValueError, match="email"):
        svc.create_admin("root", "lighthouse-keeper-9", email="Lighthouse@example.com", created_by=None, ip=None)
    with pytest.raises(ValueError, match="common"):
        svc.create_admin("root", "Password1234", created_by=None, ip=None)
    assert svc.admin_exists() is False
    svc.create_admin("root", PW, email="root@example.com", created_by=None, ip=None)
    with pytest.raises(ValueError, match="username"):
        svc.change_password("root", PW, "new-root-password", actor="root", ip=None)


def test_request_context_lands_on_the_rows_and_the_session(svc):
    svc.create_admin("root", PW, created_by=None, ip="1.1.1.1", request_id="r-000000000001", user_agent="setup/1")
    sess, _ = svc.login("root", PW, ip="2.2.2.2", request_id="r-000000000002", user_agent="drill/1")
    assert sess.user_agent == "drill/1"
    svc.audit("root", AuditAction.API_WRITE, "route", "PATCH /api/settings", ip="2.2.2.2",
              request_id="r-000000000003", user_agent="drill/1", session_id=sess.session_id[:12], status=200)
    by_action = {e.action: e for e in svc.audit_page(page_size=100)[0]}
    login = by_action[AuditAction.LOGIN]
    assert (login.request_id, login.user_agent, login.session_id) == (
        "r-000000000002", "drill/1", sess.session_id[:12],
    )
    write = by_action[AuditAction.API_WRITE]
    assert write.status == 200 and write.session_id == sess.session_id[:12]
    assert by_action[AuditAction.SETUP_ADMIN_CREATED].request_id == "r-000000000001"


def test_revoke_all_ends_every_session_and_audits_the_count(svc):
    svc.create_admin("root", PW, created_by=None, ip=None)
    svc.create_admin("ops", PW, created_by="root", ip=None)
    _, a = svc.login("root", PW, ip=None)
    _, b = svc.login("root", PW, ip=None)
    _, other = svc.login("ops", PW, ip=None)
    assert svc.revoke_all("root", actor="root", ip="9.9.9.9", request_id="r-00000000000a") == 2
    assert svc.session(a) is None and svc.session(b) is None
    assert svc.session(other) is not None
    entry = svc.audit_page(action=AuditAction.SESSIONS_REVOKED)[0][0]
    assert entry.after["count"] == 2 and entry.ip == "9.9.9.9" and entry.request_id == "r-00000000000a"


# -- v3: the hash chain ---------------------------------------------------------


def test_chain_verifies_and_a_tampered_row_is_named(svc, tmp_path):
    from control_plane.auth import verify

    svc.create_admin("root", PW, created_by=None, ip="1.1.1.1")
    for i in range(4):
        svc.audit("root", AuditAction.KEY_CREATED, "key", f"k{i}", after={"i": i}, ip="1.1.1.1")
    report = svc.verify_chain()
    assert report.ok and report.rows == 5 and report.first_bad_id is None
    assert report.head_hash == svc.head_hash()[0] and svc.head_hash()[1] == 5
    assert verify.main(["--db", str(tmp_path / "auth.db")]) == 0

    conn = sqlite3.connect(tmp_path / "auth.db")
    bad = conn.execute("SELECT id FROM audit ORDER BY id LIMIT 1 OFFSET 2").fetchone()[0]
    conn.execute("UPDATE audit SET ip='6.6.6.6' WHERE id=?", (bad,))
    conn.commit()
    report = svc.verify_chain()
    assert not report.ok and report.first_bad_id == bad and report.rows == 2
    assert verify.main(["--db", str(tmp_path / "auth.db")]) == 1


def test_a_deleted_row_breaks_the_chain_at_the_next_one(svc, tmp_path):
    for i in range(4):
        svc.audit("root", AuditAction.KEY_CREATED, "key", f"k{i}")
    conn = sqlite3.connect(tmp_path / "auth.db")
    ids = [r[0] for r in conn.execute("SELECT id FROM audit ORDER BY id")]
    conn.execute("DELETE FROM audit WHERE id=?", (ids[1],))
    conn.commit()
    assert svc.verify_chain().first_bad_id == ids[2]


def test_v2_file_migrates_to_v3_in_place_keeping_rows(tmp_path, clock, hasher):
    path = tmp_path / "auth.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE admins (username TEXT PRIMARY KEY, email TEXT, password_hash TEXT NOT NULL,
                             created_at REAL NOT NULL, created_by TEXT);
        CREATE TABLE sessions (sid_hash TEXT PRIMARY KEY, username TEXT NOT NULL, created_at REAL NOT NULL,
                               last_seen_at REAL NOT NULL, expires_at REAL NOT NULL, ip TEXT);
        CREATE TABLE login_attempts (id INTEGER PRIMARY KEY, ts REAL NOT NULL, username TEXT NOT NULL,
                                     ip TEXT, ok INTEGER NOT NULL);
        CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, actor TEXT NOT NULL,
                            action TEXT NOT NULL, target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
                            before_json TEXT, after_json TEXT, ip TEXT);
        INSERT INTO meta VALUES('schema_version', '2');
        INSERT INTO audit(ts, actor, action, target_kind, target_id, ip)
            VALUES(1.0, 'root', 'login', 'admin', 'root', '1.1.1.1');
        """
    )
    conn.execute("INSERT INTO admins VALUES('root', 'root@example.com', ?, ?, NULL)", (hasher.hash(PW), T0))
    conn.commit()
    conn.close()

    svc = AuthService.open(path, clock=clock, hasher=hasher)
    old = svc.audit_page(page_size=10)[0]
    assert len(old) == 1 and old[0].ip == "1.1.1.1" and old[0].request_id is None
    sess, _ = svc.login("root", PW, ip="2.2.2.2", user_agent="ua/1")
    assert sess.user_agent == "ua/1"
    assert sqlite3.connect(path).execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()[0] == "4"
    report = svc.verify_chain()
    # the pre-v3 row is unchained; the chain starts at the first v3 row
    assert report.ok and report.rows == 1


# -- v3: the mirror and the sink ------------------------------------------------


def test_every_row_is_mirrored_to_a_private_append_only_file(tmp_path, clock, hasher):
    import json
    import stat

    from control_plane.auditlog import AuditMirror

    mirror = AuditMirror(tmp_path / "logs" / "audit", clock=clock)
    svc = AuthService.open(tmp_path / "auth.db", clock=clock, hasher=hasher, mirror=mirror)
    svc.create_admin("root", PW, created_by=None, ip="1.1.1.1")
    svc.audit("root", AuditAction.API_WRITE, "route", "PUT /api/routing/x", request_id="r-0000000000ab", status=200)
    with pytest.raises(ValueError):
        svc.create_admin("root", PW, created_by="root", ip=None)  # rolled back: no line

    files = [f for f in (tmp_path / "logs" / "audit").iterdir() if f.name != "HEAD"]
    assert [f.name for f in files] == [mirror.path_for(clock.t).name]
    assert (tmp_path / "logs" / "audit" / "HEAD").exists()
    assert stat.S_IMODE(files[0].stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "logs" / "audit").stat().st_mode) == 0o700
    lines = [json.loads(x) for x in files[0].read_text().splitlines()]
    db = {e.id: e for e in svc.audit_page(page_size=100)[0]}
    assert [line["id"] for line in lines] == sorted(db)
    assert lines[1]["request_id"] == "r-0000000000ab" and lines[1]["ts_iso"].endswith("Z")
    assert lines[1]["row_hash"] == svc.head_hash()[0]


def test_mirror_prunes_files_older_than_the_retention(tmp_path, clock):
    from control_plane.auditlog import AuditMirror

    folder = tmp_path / "audit"
    folder.mkdir()
    (folder / "audit-20000101.jsonl").write_text("{}\n")
    keep = folder / AuditMirror(folder).path_for(clock.t - 86400).name
    keep.write_text("{}\n")
    AuditMirror(folder, days=30, clock=clock).write({"ts": clock.t, "kind": "audit"})
    names = sorted(p.name for p in folder.iterdir())
    assert "audit-20000101.jsonl" not in names and keep.name in names


def test_a_failing_mirror_never_fails_the_audit_write(tmp_path, clock, hasher):
    class Broken:
        def write(self, row):
            raise OSError("disk full")

    svc = AuthService.open(tmp_path / "auth.db", clock=clock, hasher=hasher, mirror=Broken())
    svc.audit("root", AuditAction.KEY_CREATED, "key", "k1")
    assert svc.audit_page()[1] == 1
