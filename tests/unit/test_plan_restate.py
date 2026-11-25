"""`_plan_and_fit` re-asks the planner only when it derived a context or a
concurrency the first question did not carry, so the plan's sentence states
the numbers the verdict was taken at -- and never re-asks the same question.
"""

from __future__ import annotations

import pytest

from control_plane.gateway import internal_api
from tests.unit.test_gateway import StubPlanner, TestClient, build_deps, create_app


def _calls(monkeypatch, body, *, concurrency=None, context=None):
    seen = []

    class Recording(StubPlanner):
        def plan(self, shape, nodes, link, target, concurrency, *, context_length=None, kv_dtype=None):
            seen.append((context_length, concurrency))
            return super().plan(shape, nodes, link, target, concurrency,
                                context_length=context_length, kv_dtype=kv_dtype)

    if concurrency is not None:
        monkeypatch.setattr(internal_api, "concurrency_for", lambda *a, **k: concurrency)
    if context is not None:
        monkeypatch.setattr(internal_api, "context_for", lambda *a, **k: context)
    deps = build_deps()
    deps.planner = Recording()
    with TestClient(create_app(deps)) as client:
        reply = client.post("/api/plan", json={"model_id": "meta-llama/Llama-3.3-70B-Instruct", **body})
    assert reply.status_code == 200, reply.text
    return seen


@pytest.mark.parametrize("body, concurrency, context, expected", [
    ({"context": 32768}, 1, None, [(32768, 1)]),
    ({"context": 32768}, 8, None, [(32768, 1), (32768, 8)]),
    ({"concurrency": 4}, None, 40960, [(None, 4), (40960, 4)]),
    ({"context": 8192, "concurrency": 4}, None, None, [(8192, 4)]),
])
def test_the_planner_is_asked_again_only_when_a_derived_number_changed(monkeypatch, body, concurrency, context, expected):
    assert _calls(monkeypatch, body, concurrency=concurrency, context=context) == expected
