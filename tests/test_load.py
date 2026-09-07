"""SLO regression guards, derived from what the load harness measured.

Deliberately small: the full ladder in ``tests/load`` is an exploration tool
that takes minutes, while these are the three properties that must not silently
regress. Run with ``pytest tests/test_load.py -m slow``.
"""

from __future__ import annotations

import time

import pytest

from tests.load.harness import Cluster, merge, run_drivers, stream_jobs

pytestmark = pytest.mark.slow

# Measured headroom, not aspiration: the harness derated the streaming path at
# 128 concurrent streams, so 32 sits well inside the working range and a
# failure here means something genuinely regressed rather than the box being
# busy.
STREAMS = 32
DURATION = 8.0
ITL_S = 0.02


@pytest.fixture(scope="module")
def cluster():
    cluster = Cluster(backends=2, tokens=24, ttft=0.01, itl=ITL_S)
    cluster.start()
    cluster.pin_policy()
    try:
        yield cluster
    finally:
        cluster.stop()


def _url(cluster: Cluster) -> str:
    return f"{cluster.gateway_url}/v1/chat/completions"


def test_streaming_adds_less_than_one_token_period(cluster):
    """The gateway's central latency claim, measured under real concurrency.

    ``agents/G-gateway.md:136`` promises no measurable added latency against
    calling the backend directly. Anything under one token period is not
    measurable by a client, because it is inside the gap it was already waiting
    through.
    """
    jobs = stream_jobs(_url(cluster), STREAMS, DURATION, 4)
    direct = merge(run_drivers(_spread(cluster, jobs)))
    gateway = merge(run_drivers(jobs))

    assert gateway["ok"] > 0, gateway["errors"]
    added = gateway["itl"].pct(0.99) - direct["itl"].pct(0.99)
    assert added < ITL_S * 1000.0, (
        f"gateway added {added:.1f} ms of inter-token latency at {STREAMS} "
        f"streams, more than the {ITL_S * 1000:.0f} ms token period"
    )


def test_abandoned_streams_release_every_commitment(cluster):
    """Audit H-1. A client that hangs up must cost the gateway nothing.

    A leaked ``outstanding`` never comes back: least-outstanding routing
    inverts, the KV commitment is never released, and the deployment ends in a
    permanent 429 that only a restart clears.
    """
    cluster.probe(reset=True)
    result = merge(
        run_drivers(
            stream_jobs(
                _url(cluster), STREAMS, DURATION, 4, abandon_every=1, abandon_after=2
            )
        )
    )
    assert result["abandoned"] > 0, "the probe never actually hung up on anything"

    deadline = time.time() + 20.0
    probe = {}
    while time.time() < deadline:
        probe = cluster.probe()
        if not probe.get("outstanding_total") and not probe.get("kv_committed"):
            break
        time.sleep(0.5)

    assert probe.get("outstanding_total") == 0, probe.get("targets")
    assert not probe.get("kv_committed"), probe.get("kv_committed")


def test_healthy_backends_do_not_trip_the_breaker(cluster):
    """Load alone must not be mistaken for a node going away.

    The harness showed that it can be: past the shared 256-connection pool,
    httpx raises PoolTimeout, proxy.py cannot tell it from an unreachable node,
    and the breaker benches healthy targets. This guards the working range.
    """
    cluster.probe(reset=True)
    merge(run_drivers(stream_jobs(_url(cluster), STREAMS, DURATION, 4)))
    probe = cluster.probe()
    assert not probe.get("circuits"), (
        f"breaker opened on {probe['circuits']} while every backend was healthy"
    )


def _spread(cluster: Cluster, jobs):
    from tests.load.harness import spread

    return spread(jobs, cluster.backend_urls)
