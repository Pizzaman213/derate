"""POST /api/plan/command and POST /api/plan/concurrency.

The command preview must be the launch's own rendering and refusal, and the
users chart's one-user point must be the Verdict card's number exactly.
"""

from __future__ import annotations

import dataclasses

import pytest

from control_plane.fit.calculator import bandwidth_ceiling_tps, predict_decode_tps
from tests.unit.test_deploy import make_manager
from tests.unit.test_speculative import _client, _plan_body

import tests.fixtures as fx


@pytest.mark.parametrize("kv", [0.0, 1.5e8, 3.2e9])
def test_one_user_ceiling_is_the_cards_decode_prediction_exactly(kv):
    for shape in (fx.GPT_OSS_120B,):
        assert bandwidth_ceiling_tps(shape, 273.0, kv, 1) == predict_decode_tps(shape, 273.0, kv)


def test_ceiling_each_falls_as_users_share_the_bandwidth():
    each = [bandwidth_ceiling_tps(fx.GPT_OSS_120B, 273.0, 2e8, u) for u in (1, 4, 16)]
    assert each[0] > each[1] > each[2] > 0


def _preview_client(tmp_path):
    client = _client()
    ctx = client.app.state.ctx
    ctx.deps = dataclasses.replace(ctx.deps, deployments=make_manager(tmp_path))
    return client


def test_command_preview_carries_the_engine_flags(tmp_path):
    with _preview_client(tmp_path) as client:
        r = client.post(
            "/api/plan/command",
            json=_plan_body(engine_options={
                "reasoning_parser": "qwen3",
                "max_num_batched_tokens": 4096,
                "async_scheduling": True,
            }),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    argv = " ".join(body["argv"]) + "\n" + body["recipe"]
    assert "--max-num-batched-tokens" in argv and "4096" in argv
    assert "--reasoning-parser" in argv and "qwen3" in argv
    assert "--async-scheduling" in argv
    assert body["notes"]
    assert isinstance(body["nccl_env"], dict)


def test_command_preview_refuses_a_vllm_option_on_sglang(tmp_path):
    with _preview_client(tmp_path) as client:
        r = client.post(
            "/api/plan/command",
            json=_plan_body(
                runtime="sglang",
                engine_options={"reasoning_parser": "qwen3"},
            ),
        )
    assert r.status_code == 400, r.text
    assert "reasoning_parser" in r.json()["error"]["message"]


def test_concurrency_wall_drops_as_context_grows_and_one_user_is_the_card():
    small = {"model_id": "Qwen/Qwen3-30B-A3B"}
    with _client() as client:
        plan = client.post(
            "/api/plan", json=_plan_body(context=8192, concurrency=1, **small)
        ).json()
        r = client.post(
            "/api/plan/concurrency",
            json=_plan_body(contexts=[8192, 32768], max_users=16, **small),
        )
    assert r.status_code == 200, r.text
    body = r.json()
    walls = [s["wall_users"] for s in body["series"]]
    assert walls[0] > walls[1]
    first = body["series"][0]["points"][0]
    assert first["users"] == 1
    fit = plan["fit_live"] or plan["fit"]
    assert first["ceiling_tps_each"] == pytest.approx(fit["predicted_decode_tps"], abs=1e-3)
    assert "compute-bound" in body["caveat"]


def test_concurrency_rejects_a_bad_context_list():
    with _client() as client:
        r = client.post("/api/plan/concurrency", json=_plan_body(contexts=[]))
    assert r.status_code == 400


def test_the_users_wall_charges_a_turboquant_pipeline_as_its_busiest_stage(monkeypatch):
    """One sequence's cache is `kv_cache_bytes_per_rank`, not the whole-model
    figure over `kv_divisor`: the second spreads TurboQuant's wide boundary
    layers across a pipeline when the end stages each hold two, and so put
    the wall above what the busiest stage can actually hold."""
    from control_plane.fit import kv as fit_kv
    from control_plane.gateway import internal_api
    from tests.unit.test_fit import make_plan

    small = {"model_id": "Qwen/Qwen3-30B-A3B"}
    tq, plan = "turboquant_k8v4", make_plan(pp=4, node_ids=("spark-01",))
    real = internal_api._plan_and_fit
    shapes = []

    async def pipelined(ctx, payload):
        out = await real(ctx, payload)
        shapes.append(out.shape)
        return dataclasses.replace(out, plan=plan, kv_dtype=tq)

    monkeypatch.setattr(internal_api, "_plan_and_fit", pipelined)
    with _client() as client:
        r = client.post(
            "/api/plan/concurrency", json=_plan_body(contexts=[32768], max_users=256, **small)
        )
        plan_body = client.post(
            "/api/plan", json=_plan_body(context=32768, concurrency=1, **small)
        ).json()
    assert r.status_code == 200, r.text
    fit = plan_body["fit_live"] or plan_body["fit"]
    per_seq = fit_kv.kv_cache_bytes_per_rank(shapes[0], plan, 32768, 1, tq)
    fixed = fit["breakdown"]["total"] - fit["breakdown"]["kv_cache"]
    expected = max(0, int((fit["usable_per_node"] - fixed) // per_seq))
    assert expected > 1, "the wall must be off the floor for this to compare anything"
    assert r.json()["series"][0]["wall_users"] == expected
