"""`tests/placement_sweep.py`: which shapes it launches, and how it scores them."""

from __future__ import annotations

import json

import httpx
import pytest

from tests import placement_sweep as ps
from tests.unit.test_compute_floor import _two_node_client


def _degrees(tp=1, pp=1, ep=1, dp=1):
    return {"tensor_parallel": tp, "pipeline_parallel": pp,
            "expert_parallel": ep, "data_parallel": dp}


CANDIDATES = {
    "recommended_kind": "pipeline",
    "candidates": [
        {"kind": "pipeline", "degrees": _degrees(pp=2), "node_ids": ["a", "b"],
         "verdict": "fits", "per_node_bytes": 1, "reason": "pp wins", "error": None},
        {"kind": "tensor", "degrees": _degrees(tp=2), "node_ids": ["a", "b"],
         "verdict": "fits_degraded", "per_node_bytes": 1, "reason": "tp", "error": None},
        {"kind": "expert", "degrees": _degrees(ep=2, dp=2), "node_ids": ["a", "b"],
         "verdict": None, "per_node_bytes": None, "reason": None,
         "error": "Cross-node expert parallel needs sparkrun >= 0.3.7."},
        {"kind": "single_node", "degrees": _degrees(), "node_ids": ["a"],
         "verdict": "wont_fit", "per_node_bytes": 9, "reason": "one", "error": None},
    ],
}


def test_candidates_keep_the_planners_order_pick_and_refusals():
    found = ps.enumerate_candidates({"recommended_plan": {"kind": "pipeline"}}, CANDIDATES)
    assert [c.kind for c in found] == ["pipeline", "tensor", "expert", "single_node"]
    assert [c.planner_rank for c in found] == [0, 1, 2, 3]
    assert [c.recommended for c in found] == [True, False, False, False]
    # fits_degraded is launched: it is the gate's prediction worth testing.
    assert [c.viable for c in found] == [True, True, False, False]
    assert found[2].error.startswith("Cross-node expert parallel")
    assert found[3].label == "single_node (1 rank on a)"
    assert found[1].label == "tensor (TP2 on a,b)"


def test_an_older_gateway_falls_back_to_one_alternative_per_kind():
    plan = {
        "recommended_plan": {"kind": "tensor"},
        "alternatives": [
            {"kind": "tensor", **_degrees(tp=2), "world_size": 2, "node_ids": ["a", "b"]},
            {"kind": "tensor", **_degrees(tp=1), "world_size": 1, "node_ids": ["a"]},
            {"kind": "pipeline", **_degrees(pp=2), "world_size": 2, "node_ids": ["a", "b"]},
        ],
    }
    found = ps.enumerate_candidates(plan, None)
    assert [(c.kind, c.degrees["tensor_parallel"], c.degrees["pipeline_parallel"])
            for c in found] == [("tensor", 2, 1), ("pipeline", 1, 2)]
    assert [c.recommended for c in found] == [True, False]
    assert all(c.viable for c in found)


def test_the_launch_body_pins_both_the_hosts_and_the_degrees():
    c = ps.enumerate_candidates({}, CANDIDATES)[1]
    body = ps.launch_body(c, "Qwen/Qwen3-4B", 8192, 8)
    assert body == {
        "model_id": "Qwen/Qwen3-4B", "context": 8192, "concurrency": 8,
        "target": "throughput", "runtime": "vllm", "node_ids": ["a", "b"],
        "parallelism": _degrees(tp=2), "served_name": "Qwen3-4B-placement-tensor",
    }
    # Distinct per shape, and inside the recipe grammar's alphabet.
    assert ps.served_name_for("org/We ird+Name", "single_node") == "We-ird-Name-placement-single_node"


def test_a_phase_separates_one_streams_rate_from_the_aggregate():
    samples = [
        # 11 tokens: 10 after the first over 1 s -> 10 tok/s; ttft 0.2 s
        ps.Sample(sent=0.0, first=0.2, last=1.2, tokens=11),
        # 21 tokens over 1 s -> 20 tok/s; ttft 0.4 s
        ps.Sample(sent=0.0, first=0.4, last=1.4, tokens=21),
        ps.Sample(sent=0.0, first=None, last=None, tokens=0, error="HTTP 500: boom"),
    ]
    p = ps.summarise_phase(samples, 2)
    assert p.requests == 2 and p.errors == 1 and p.reason == "HTTP 500: boom"
    assert p.decode_tps_each == pytest.approx(15.0)
    assert p.aggregate_tps == pytest.approx(32 / 1.4)
    assert p.ttft_ms_p50 == pytest.approx(300.0)
    assert p.ttft_ms_mean == pytest.approx(300.0)


def test_a_phase_with_nothing_served_has_no_figures():
    p = ps.summarise_phase([ps.Sample(0.0, None, None, 0, "refused")], 1)
    assert (p.requests, p.decode_tps_each, p.aggregate_tps, p.ttft_ms_p50) == (0, None, None, None)


def _measured(c, each, agg):
    return ps.Result(c, "measured", phases=[
        ps.Phase(concurrency=1, requests=1, decode_tps_each=each),
        ps.Phase(concurrency=8, requests=8, aggregate_tps=agg),
    ])


def test_compare_names_where_the_measured_order_departs_from_the_pick():
    pp, tp, ep, single = ps.enumerate_candidates({}, CANDIDATES)
    results = [
        _measured(pp, each=20.0, agg=150.0),
        _measured(tp, each=30.0, agg=120.0),
        ps.Result(ep, "refused", ep.error),
        ps.Result(single, "refused", "verdict wont_fit"),
    ]
    out = ps.compare(results, 8)
    assert out["planner_pick"] == "pipeline"
    assert out["planner_order"] == ["pipeline", "tensor"]
    at1, at8 = out["by_concurrency"]
    assert (at1["concurrency"], at1["metric"]) == (1, "decode_tps_each")
    assert at1["measured_order"] == ["tensor", "pipeline"]
    assert at1["agrees"] is False
    assert at1["pick_vs_best"] == pytest.approx(20 / 30)
    assert at8["measured_order"] == ["pipeline", "tensor"]
    assert at8["agrees"] is True and at8["pick_vs_best"] == pytest.approx(1.0)

    table = ps.render_table(results, out, 8)
    assert "measured tensor > pipeline; disagrees with the planner's pick (pipeline), which delivered 67% of the best" in table
    assert "agrees with the planner's pick (pipeline)" in table
    # A refusal is printed whole under its row.
    assert "Cross-node expert parallel needs sparkrun >= 0.3.7." in table


def test_a_pick_that_never_served_cannot_agree_or_disagree():
    pp, tp, *_ = ps.enumerate_candidates({}, CANDIDATES)
    out = ps.compare([ps.Result(pp, "failed", "EngineCore failed to start."),
                      _measured(tp, each=30.0, agg=120.0)], 8)
    assert [row["agrees"] for row in out["by_concurrency"]] == [None, None]
    assert out["by_concurrency"][0]["measured_order"] == ["tensor"]


def test_concurrency_one_is_measured_once():
    (pp, *_) = ps.enumerate_candidates({}, CANDIDATES)
    r = ps.Result(pp, "measured", phases=[ps.Phase(concurrency=1, requests=1, decode_tps_each=5.0)])
    assert [row["metric"] for row in ps.compare([r], 1)["by_concurrency"]] == ["decode_tps_each"]


def test_login_carries_the_session_cookie_on_later_calls():
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            assert json.loads(request.content) == {"username": "admin", "password": "pw"}
            return httpx.Response(200, json={}, headers={
                "set-cookie": "derate_session=abc123; HttpOnly; Path=/; SameSite=strict",
            })
        seen.append(request.headers.get("cookie"))
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(base_url="http://coord", transport=httpx.MockTransport(handler)) as c:
        ps.login(c, "admin", "pw")
        assert ps._call(c, "GET", "/api/cluster") == {"ok": True}
    assert seen == ["derate_session=abc123"]


def test_a_refused_login_and_a_gateway_refusal_keep_their_own_words():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            return httpx.Response(401, json={"error": {"message": "bad password"}})
        return httpx.Response(409, json={
            "error": {"message": "Qwen3-4B is already deployed.", "code": "already_deployed"},
            "conflict": {"deployment_id": "dep-1"},
        })

    with httpx.Client(base_url="http://coord", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ps.SweepError, match="answered 401"):
            ps.login(c, "admin", "pw")
        with pytest.raises(ps.SweepError) as caught:
            ps._call(c, "POST", "/api/deployments", body={})
    assert str(caught.value) == "Qwen3-4B is already deployed."
    assert caught.value.conflict_id == "dep-1" and caught.value.status == 409


def test_help_exits_cleanly(capsys):
    with pytest.raises(SystemExit) as code:
        ps.main(["--help"])
    assert code.value.code == 0
    assert "--dry-run" in capsys.readouterr().out


def test_dry_run_against_a_real_planner_prints_bodies_and_launches_nothing(capsys):
    with _two_node_client() as client:
        code = ps.main(
            ["--model", "Qwen/Qwen3-30B-A3B", "--nodes", "spark-01,spark-02",
             "--concurrency", "4", "--dry-run"],
            client=client,
        )
        deployed = client.get("/api/deployments").json()
    out = capsys.readouterr().out
    print(out)  # visible under -s: this is the proof the bodies are well-formed
    assert code == 0
    assert deployed == []
    bodies = [json.loads(line.split("POST /api/deployments ", 1)[1])
              for line in out.splitlines() if "POST /api/deployments " in line]
    assert bodies, out
    kinds = {b["served_name"].rsplit("placement-", 1)[1] for b in bodies}
    assert {"single_node", "tensor"} <= kinds
    for b in bodies:
        assert b["model_id"] == "Qwen/Qwen3-30B-A3B" and b["concurrency"] == 4
        assert b["context"] > 0 and b["node_ids"]
        assert set(b["parallelism"]) == set(ps.DEGREE_AXES)
        world = b["parallelism"]["tensor_parallel"] * b["parallelism"]["pipeline_parallel"]
        assert world * b["parallelism"]["data_parallel"] >= len(b["node_ids"])
    assert out.count("*") >= 1  # the pick is marked
    assert f"dry run: {len(bodies)} of" in out


# -- teardown is confirmed before the next shape ---------------------------------


class _Gateway:
    """A fake coordinator whose deployment reports `states` in turn after DELETE."""

    def __init__(self, states, delete_status=200):
        self.states = list(states)
        self.delete_status = delete_status
        self.deleted = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            self.deleted = True
            if self.delete_status >= 400:
                return httpx.Response(self.delete_status,
                                      json={"error": {"message": "the node did not answer"}})
            return httpx.Response(200, json={})
        if not self.deleted:
            return httpx.Response(200, json={"state": "ready", "backend_url": "http://eng/v1"})
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if state == 404:
            return httpx.Response(404, json={"error": {"message": "no such deployment"}})
        return httpx.Response(200, json={"state": state, "backend_url": "http://eng/v1"})


def _stop(gw, *, answers=lambda b: False, timeout=10.0):
    now = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        now[0] += s

    with httpx.Client(base_url="http://coord", transport=httpx.MockTransport(gw.handler)) as c:
        ps.stop(c, "dep-1", timeout=timeout, poll_s=1.0, answers=answers,
                sleep=sleep, clock=lambda: now[0])
    return sleeps


def test_stop_waits_until_the_record_says_stopped():
    sleeps = _stop(_Gateway(["stopping", "stopping", "stopped"]))
    assert len(sleeps) == 2


def test_stop_accepts_a_record_that_is_gone():
    assert _stop(_Gateway([404])) == []


def test_stop_waits_for_the_engine_to_stop_answering():
    alive = iter([True, True, False])
    sleeps = _stop(_Gateway(["stopped"]), answers=lambda b: next(alive))
    assert len(sleeps) == 2


def test_a_refused_stop_ends_the_sweep():
    with pytest.raises(ps.SweepError, match="stopping dep-1 failed"):
        _stop(_Gateway(["ready"], delete_status=502))


def test_a_stop_that_never_finishes_ends_the_sweep():
    with pytest.raises(ps.SweepError, match="still stopping 10s after"):
        _stop(_Gateway(["stopping"]))


def test_a_stopped_record_with_a_live_engine_ends_the_sweep():
    with pytest.raises(ps.SweepError, match="engine at http://eng/v1 still answers"):
        _stop(_Gateway(["stopped"]), answers=lambda b: True)


def test_measure_one_propagates_an_unconfirmed_teardown(monkeypatch):
    gw = _Gateway(["stopping"])

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"deployment_id": "dep-1"})
        return gw.handler(request)

    monkeypatch.setattr(ps, "wait_ready", lambda *a, **k: (_ for _ in ()).throw(ps.SweepError("boom")))
    real_stop = ps.stop
    monkeypatch.setattr(ps, "stop", lambda c, d, timeout: real_stop(
        c, d, timeout=0.0, poll_s=0.0, answers=lambda b: False, sleep=lambda s: None))
    monkeypatch.setattr(ps.time, "sleep", lambda s: pytest.fail("slept past a failed stop"))
    args = ps.build_parser().parse_args(["--model", "m", "--nodes", "a"])
    cand = ps.enumerate_candidates({"recommended_plan": {}}, None)
    with httpx.Client(base_url="http://coord", transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ps.SweepError, match="ending the sweep"):
            ps.measure_one(c, cand[0] if cand else None, {"served_name": "s"}, [], 1, args)


def _engine_answering(status, monkeypatch):
    def get(url, timeout):
        return httpx.Response(status, request=httpx.Request("GET", url))
    monkeypatch.setattr(ps.httpx, "get", get)


def test_an_engine_answering_500_is_still_alive(monkeypatch):
    _engine_answering(500, monkeypatch)
    assert ps._backend_answers("http://eng/v1") is True
    with pytest.raises(ps.SweepError, match="engine at http://eng/v1 still answers"):
        _stop(_Gateway(["stopped"]), answers=ps._backend_answers)


def test_only_a_connection_failure_means_the_engine_is_gone(monkeypatch):
    def get(url, timeout):
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))
    monkeypatch.setattr(ps.httpx, "get", get)
    assert ps._backend_answers("http://eng/v1") is False
