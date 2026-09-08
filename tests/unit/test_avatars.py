"""Publisher avatars: the three-state answer, and the caching that is the point.

The defect these cover is one the old client could not avoid: ~45 publishers on
a Models grid, each resolved by the BROWSER against an unauthenticated hub
endpoint, on every page load. That is over the hub's burst limit on its own, so
the grid 429ed itself and every card fell back to two letters until a backoff
that doubled towards half an hour expired. The fix is not a better backoff --
it is asking once, ever, on the coordinator, and keeping the answer.

So what is worth pinning here is not "an avatar can be fetched". It is:

  * a publisher is asked about ONCE even when ninety cards want it at once,
  * a hub non-answer never becomes "this publisher has no mark",
  * an owner that has not resolved yet is ABSENT from the answer rather than
    reported as having none, and
  * the deadline does not cancel the work it was waiting on.
"""

from __future__ import annotations

import asyncio

import pytest

from control_plane.resolver import avatars

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """A cache per test, and no module state carried between them."""
    from control_plane.providers.logos import LogoCache

    monkeypatch.setattr(avatars, "_cache", LogoCache(tmp_path / "avatars"))
    monkeypatch.setattr(avatars, "_locks", {})
    monkeypatch.setattr(avatars, "_semaphore", None)
    monkeypatch.setattr(avatars, "_running", set())
    yield


def fake_fetch(calls, *, answers=None, fails=(), slow=()):
    """A stand-in for `avatars.fetch` that records who it was asked about."""
    answers = answers or {}

    async def fetch(owner):
        calls.append(owner)
        if owner in slow:
            await asyncio.sleep(5)
        if owner in fails:
            raise RuntimeError("hub returned 429")
        found = answers.get(owner, PNG)
        return (found, "image/png") if found else None

    return fetch


def test_a_publisher_is_asked_about_once_however_many_cards_want_it(monkeypatch):
    """The whole reason this moved off the browser.

    Ninety cards from one publisher is one lookup, and the second batch is
    zero: the answer is cached, so the hub is not approached again at all.
    """
    calls: list[str] = []
    monkeypatch.setattr(avatars, "fetch", fake_fetch(calls))

    first = asyncio.run(avatars.resolve_many(["Qwen"] * 90))
    assert first == {"Qwen": True}
    assert calls == ["Qwen"]

    second = asyncio.run(avatars.resolve_many(["Qwen"]))
    assert second == {"Qwen": True}
    assert calls == ["Qwen"], "a cached publisher must not be looked up again"


def test_a_publisher_with_no_mark_is_a_permanent_no_not_a_repeat_lookup():
    """`False` is an answer, and answering it is what stops the re-asking."""
    calls: list[str] = []

    async def go():
        avatars.fetch = fake_fetch(calls, answers={"nobody": None})
        first = await avatars.resolve_many(["nobody"])
        second = await avatars.resolve_many(["nobody"])
        return first, second

    original = avatars.fetch
    try:
        first, second = asyncio.run(go())
    finally:
        avatars.fetch = original

    assert first == {"nobody": False}
    assert second == {"nobody": False}
    assert calls == ["nobody"]


def test_a_hub_non_answer_is_never_reported_as_having_no_mark(monkeypatch):
    """A 429 is not "this publisher has no avatar".

    Conflating the two is the defect that would survive the move to the server:
    one rate-limited minute would write a miss that keeps the letters for a
    day, which is exactly the failure this whole path exists to end.
    """
    calls: list[str] = []
    monkeypatch.setattr(avatars, "fetch", fake_fetch(calls, fails={"Qwen"}))

    answer = asyncio.run(avatars.resolve_many(["Qwen"]))
    assert answer.get("Qwen") is not False, "a hub failure must not read as 'no mark'"


def test_an_owner_still_resolving_is_absent_rather_than_false(monkeypatch):
    """The third state, which is what lets the client retry without a timer."""
    calls: list[str] = []
    monkeypatch.setattr(avatars, "fetch", fake_fetch(calls, slow={"slowpoke"}))

    answer = asyncio.run(avatars.resolve_many(["slowpoke"], deadline_s=0.05))
    assert "slowpoke" not in answer
    assert answer == {}


def test_the_deadline_does_not_cancel_the_lookup_it_gave_up_waiting_for(monkeypatch):
    """`wait`, not `wait_for(gather(...))`.

    Cancelling on the deadline would throw away a request that was already in
    flight and make the next batch pay the hub for it again -- which in a grid
    that retries is an unbounded number of lookups for one publisher, the
    opposite of the point.
    """
    calls: list[str] = []
    finished: list[str] = []

    async def fetch(owner):
        calls.append(owner)
        await asyncio.sleep(0.2)
        finished.append(owner)
        return PNG, "image/png"

    monkeypatch.setattr(avatars, "fetch", fetch)

    async def go():
        first = await avatars.resolve_many(["late"], deadline_s=0.02)
        # Nothing to report yet...
        assert first == {}
        # ...but the work was not thrown away, and lands on its own.
        await asyncio.sleep(0.4)
        return await avatars.resolve_many(["late"])

    assert asyncio.run(go()) == {"late": True}
    assert finished == ["late"]
    assert calls == ["late"], "the straggler must not be re-fetched"


def test_concurrent_hub_lookups_are_bounded(monkeypatch):
    """Moving the fan-out to the server would have changed nothing on its own.

    Forty-five simultaneous requests is what got the browser rate limited; the
    coordinator making the same forty-five simultaneously would be the same
    burst from a different IP.
    """
    live = 0
    peak = 0

    async def fetch(owner):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return PNG, "image/png"

    monkeypatch.setattr(avatars, "fetch", fetch)
    owners = [f"pub-{i}" for i in range(40)]
    answer = asyncio.run(avatars.resolve_many(owners, deadline_s=10))

    assert answer == {name: True for name in owners}
    assert peak <= avatars._MAX_CONCURRENT


def test_a_name_that_cannot_be_a_hub_namespace_is_refused_before_it_is_asked():
    """`normalize` is the gate, and a path separator is the thing it stops."""
    assert avatars.normalize(" Qwen ") == "Qwen"
    assert avatars.normalize("Qwen") == "Qwen"
    # Case is significant to the hub: `Qwen` and `qwen` are different records.
    assert avatars.normalize("qwen") == "qwen"
    for bad in ("", "   ", "a/b", "..", ".hidden", "a?b", "a#b", "a%2e", "x" * 97):
        assert avatars.normalize(bad) == "", bad


def test_a_refused_name_never_reaches_the_hub(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(avatars, "fetch", fake_fetch(calls))

    assert asyncio.run(avatars.resolve_many(["../etc/passwd", "", "ok"])) == {"ok": True}
    assert calls == ["ok"]
