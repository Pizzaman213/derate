"""The quantization ladder, every KV width and every speculative method, asked
of a LIVE coordinator for one model whose numbers are known from outside derate.

`thinkingmachines/inkling-2-mini` is the subject because it broke the gate in
every way this file checks, at once, on the screen:

* its ladder offered a 0.6 GiB DSpark draft head as the model's BF16 "best
  fit", a GGUF repository as a size-less safetensors build, two rows under one
  name, and judged every row on a GB10 and an RTX 3090 pooled at TP=2 -- a split
  the planner never picks -- so the 3090's free memory refused builds that fit
  the Spark alone;
* its KV cache was 3x too high from config.json and 44x too low from the GGUF
  header: it is a looped hybrid (20 of 24 layers windowed at 1024, layers 6..17
  run three times), and llama.cpp keeps a cache per loop visit -- 48 slots, 8
  of them global. llama-server's own log puts the global part at 32 KiB per
  token; that figure is pinned below, at every width the plan offers;
* the plan offered KV widths it then refused, and accepted ones the launch
  refuses; and the checkpoint's own speculator (`mtp_config`, DSpark under
  another name) was invisible.

Skips only when ``DERATE_E2E_ORIGIN`` is unset (see `e2e_support`). Read-only:
nothing here launches, downloads or writes.
"""

from __future__ import annotations

import os

import pytest

from control_plane.resolver import gguf_names
from tests.unit import e2e_support

E2E_ORIGIN = os.environ.get("DERATE_E2E_ORIGIN")

MODEL = "thinkingmachines/inkling-2-mini"

#: Inkling 2 Mini's cache, from its config and llama-server's allocation log:
#: 8 global slots x 8 KV heads x 128 dims x K and V x 2 bytes, per token, and
#: 40 windowed slots holding at most 1024 tokens each.
GLOBAL_BYTES_PER_TOKEN = 8 * 8 * 128 * 2 * 2
WINDOWED_BYTES = 40 * 8 * 128 * 2 * 2 * 1024

#: Long enough that every windowed slot is full, so growth is the global part.
CONTEXTS = (32768, 131072)

#: The enumeration is a dozen hub calls on a cold cache.
SLOW = 300


@pytest.fixture(scope="module")
def http():
    origin = e2e_support.require(E2E_ORIGIN, "DERATE_E2E_ORIGIN", "/healthz")
    client = e2e_support.client(origin)
    client.base_url = origin
    return client


@pytest.fixture(scope="module")
def ladder(http):
    reply = http.get(
        "/api/models/variants",
        params={"model_id": MODEL, "context": 131072, "concurrency": 1},
        timeout=SLOW,
    )
    reply.raise_for_status()
    return reply.json()


def _plan(http, model_id: str, **body) -> tuple[int, dict]:
    reply = http.post(
        "/api/plan",
        json={"model_id": model_id, "runtime": "vllm", "concurrency": 1, **body},
        timeout=SLOW,
    )
    return reply.status_code, reply.json()


def _kv(http, model_id: str, **body) -> int:
    status, out = _plan(http, model_id, **body)
    assert status == 200, out
    return out["fit"]["breakdown"]["kv_cache"]


@pytest.fixture(scope="module")
def subjects(ladder) -> list[str]:
    """The checkpoint itself and the GGUF build the ladder ranks first: the
    two mappers (config.json and a GGUF header) that must agree."""
    files = [v for v in ladder["variants"] if v["gguf_file"]]
    assert files, "the ladder lists no GGUF build of %s" % MODEL
    top = files[0]
    return [MODEL, "hf://%s/%s" % (top["repo_id"], top["gguf_file"])]


# -- the ladder ---------------------------------------------------------------


def test_every_file_row_is_a_set_of_weights(ladder):
    """A draft head, a projector or an importance matrix priced as the model
    fits anywhere, and the smallest thing that fits becomes the pick."""
    not_weights = [
        (v["repo_id"], v["gguf_file"])
        for v in ladder["variants"]
        if v["gguf_file"] and not gguf_names.is_weight_file(v["gguf_file"])
    ]
    assert not not_weights


def test_no_two_rows_of_one_repository_share_a_name(ladder):
    """The UI names a row `(repo_id, label)`; a shared one put the
    recommendation on the wrong row, or on several."""
    rows = ladder["variants"]
    names = [(v["repo_id"], v["label"]) for v in rows]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    assert not duplicated


def test_a_gguf_repository_is_never_offered_as_a_launchable_build(ladder):
    """Its tags describe its files. A repository-level row read off them
    claimed a safetensors build that does not exist, with no size."""
    claimed = [
        v["repo_id"]
        for v in ladder["variants"]
        if v["gguf_file"] is None
        and v["repo_id"].lower().rstrip("/").split("-")[-1] == "gguf"
        and v["launchable"]
    ]
    assert not claimed


def test_the_recommendation_is_one_row_that_fits(ladder):
    pick = ladder["recommended"]
    if pick is None:
        pytest.skip("nothing launchable fits this cluster right now")
    rows = [
        v
        for v in ladder["variants"]
        if (v["repo_id"], v["label"], v["gguf_file"])
        == (pick["repo_id"], pick["label"], pick["gguf_file"])
    ]
    assert len(rows) == 1, pick
    assert rows[0]["fits"] and rows[0]["launchable"]


def test_the_ladder_is_sized_on_like_hardware(http, ladder):
    """Unnamed machines are judged where the planner would place them: one
    group of interchangeable hardware, never a GB10 pooled with a 3090."""
    placed = set(ladder["sized_on"]["nodes"])
    reply = http.get("/api/nodes", timeout=30)
    reply.raise_for_status()
    kinds = {
        (n["device_class"], n["gpu_name"]) for n in reply.json() if n["node_id"] in placed
    }
    assert len(kinds) <= 1, kinds


# -- the KV cache ---------------------------------------------------------------


def test_both_mappers_charge_the_measured_cache(http, subjects):
    """Above the window only the 8 global slots grow: 32 KiB per token, the
    rate llama-server logs. From config.json and from the GGUF header alike."""
    low, high = CONTEXTS
    for model_id in subjects:
        at_low = _kv(http, model_id, context=low)
        at_high = _kv(http, model_id, context=high)
        assert at_high - at_low == GLOBAL_BYTES_PER_TOKEN * (high - low), model_id
        assert at_low == GLOBAL_BYTES_PER_TOKEN * low + WINDOWED_BYTES, model_id


def test_the_cache_is_per_sequence(http, subjects):
    for model_id in subjects:
        one = _kv(http, model_id, context=CONTEXTS[0])
        assert _kv(http, model_id, context=CONTEXTS[0], concurrency=4) == 4 * one, model_id


def test_every_kv_width_offered_is_accepted_and_priced_at_its_ratio(http, subjects):
    """What the picker offers is what the plan takes, and the bytes move by
    exactly the ratio shown beside it. The weights do not move at all."""
    for model_id in subjects:
        status, base = _plan(http, model_id, context=CONTEXTS[0])
        assert status == 200, base
        sixteen = base["fit"]["breakdown"]["kv_cache"]
        weights = base["fit"]["breakdown"]["weights"]
        offered = base["kv_dtype_ratios"]
        assert {"fp8", "float16"} <= set(offered), sorted(offered)
        for name, ratio in offered.items():
            status, out = _plan(http, model_id, context=CONTEXTS[0], kv_dtype=name)
            assert status == 200, (model_id, name, out)
            got = out["fit"]["breakdown"]
            assert got["kv_cache"] == pytest.approx(sixteen * ratio, rel=1e-6), (model_id, name)
            assert got["weights"] == weights, (model_id, name)


@pytest.mark.parametrize("kv_dtype", ["int8", "fp8_inc", "q8_0"])
def test_a_width_the_engine_cannot_take_is_refused_by_the_plan(http, kv_dtype):
    status, out = _plan(http, MODEL, context=CONTEXTS[0], kv_dtype=kv_dtype)
    assert status == 400, out
    assert kv_dtype in out["error"]["message"]


# -- speculative decoding -------------------------------------------------------


def test_the_checkpoints_own_speculator_is_named_and_not_offered(http):
    """`mtp_config` is DSpark: named, with the sentence saying why derate will
    not budget it, and refused when asked for. ngram needs nothing."""
    status, out = _plan(http, MODEL, context=CONTEXTS[0])
    assert status == 200, out
    options = {o["method"]: o for o in out["speculative_options"]}
    assert set(options) == {"dspark", "ngram"}
    assert options["dspark"]["draft_params"] is None
    assert not options["dspark"]["launchable"]


@pytest.mark.parametrize("method", ["mtp", "dspark", "eagle3"])
def test_a_speculator_it_cannot_budget_is_refused(http, method):
    status, out = _plan(
        http, MODEL, context=CONTEXTS[0],
        speculative={"method": method, "num_speculative_tokens": 2},
    )
    assert status == 400, out


def test_ngram_is_priced_at_nothing(http):
    status, base = _plan(http, MODEL, context=CONTEXTS[0])
    assert status == 200, base
    status, out = _plan(
        http, MODEL, context=CONTEXTS[0],
        speculative={"method": "ngram", "num_speculative_tokens": 2},
    )
    assert status == 200, out
    assert out["speculative"]["method"] == "ngram"
    assert out["fit"]["breakdown"]["weights"] == base["fit"]["breakdown"]["weights"]


def test_a_gguf_build_is_never_speculated(http, subjects):
    status, out = _plan(
        http, subjects[1], context=CONTEXTS[0],
        speculative={"method": "ngram", "num_speculative_tokens": 2},
    )
    assert status == 400, out
