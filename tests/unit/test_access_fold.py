"""gateway/accessfold.py: a session's routine reads fold per minute, nothing
else folds, and the counts written always add up to the hits offered."""

from __future__ import annotations

from control_plane.gateway.accessfold import AccessFold, AccessHit, foldable


def _hit(rid: str, **kw) -> AccessHit:
    base = dict(
        ctx=None, request_id=rid, ts=0.0, client_ip="10.0.0.1", client_port=5000,
        peer_ip=None, who="session:root", user_agent="ua", method="GET",
        path="/api/cluster", query="", status=200, duration_ms=1.0,
    )
    base.update(kw)
    return AccessHit(**base)


def _fold(**kw):
    now = [0.0]
    written: list[tuple[str, int]] = []
    fold = AccessFold(lambda h, n: written.append((h.request_id, n)), clock=lambda: now[0], **kw)
    return fold, now, written


def test_identical_reads_fold_and_the_window_closes():
    fold, now, written = _fold()
    for rid, t in (("r1", 0.0), ("r2", 10.0), ("r3", 59.0)):
        now[0] = t
        fold.offer(_hit(rid))
    assert written == [("r1", 1)]
    now[0] = 61.0
    fold.offer(_hit("r4"))
    assert written == [("r1", 1), ("r3", 2), ("r4", 1)]
    now[0] = 125.0
    fold.flush_due()
    assert len(written) == 3
    fold.offer(_hit("r5"))
    fold.offer(_hit("r6"))
    fold.flush_due()
    assert written[-1] == ("r5", 1)
    now[0] = 190.0
    fold.flush_due()
    assert written[-1] == ("r6", 1) and len(written) == 5


def test_unfoldable_hits_through_the_gate_path_write_once_each():
    assert foldable(_hit("x")) and foldable(_hit("x", status=304, method="HEAD"))
    for kw in (dict(method="POST"), dict(status=404), dict(who="key:abc"),
               dict(path="/v1/chat/completions"), dict(method="WS")):
        assert not foldable(_hit("x", **kw))
        fold, _, written = _fold()
        direct: list[tuple[str, int]] = []
        for i in range(3):
            hit = _hit(f"r{i}", **kw)
            if foldable(hit):
                fold.offer(hit)
            else:
                direct.append((hit.request_id, 1))
        fold.flush_all()
        assert direct == [("r0", 1), ("r1", 1), ("r2", 1)] and written == []


def test_eviction_writes_the_evicted_keys_pending_hits():
    fold, _, written = _fold(max_keys=2)
    fold.offer(_hit("a1", path="/api/a"))
    fold.offer(_hit("a2", path="/api/a"))
    fold.offer(_hit("b1", path="/api/b"))
    fold.offer(_hit("c1", path="/api/c"))
    assert ("a2", 1) in written


def test_counts_sum_to_offers_after_flush_all():
    fold, now, written = _fold(max_keys=3)
    offered = 0
    for i in range(200):
        now[0] = i * 1.7
        fold.offer(_hit(f"r{i}", path=f"/api/p{i % 5}", query=f"q={i % 2}"))
        offered += 1
        if i % 30 == 0:
            fold.flush_due()
    fold.flush_all()
    assert sum(n for _, n in written) == offered
