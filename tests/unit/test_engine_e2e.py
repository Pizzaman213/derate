"""A real inference engine, launched and asked for tokens, with no GPU.

Every other engine test here stands in for the engine. This one does not: it
drives a LIVE coordinator over HTTP, launches a ~100 MB GGUF on the ``llamacpp``
runtime (the CPU runtime: ``ghcr.io/ggml-org/llama.cpp:server`` through
sparkrun and docker), waits for READY, and checks what comes back through
``/v1`` -- the fit gate, the planner, the recipe, the launch, the health probe,
the gateway's proxy and the engine's own wire format, in one pass.

What it proves that nothing hermetic can:

* a launch that clears every gate still produces a server that answers;
* a non-streamed completion carries real text and a real ``usage`` block;
* a streamed one is well-formed SSE -- more than one data frame, a final
  ``[DONE]`` -- and its deltas add up to text, so the proxy did not buffer,
  truncate or reorder what the engine sent;
* ``/v1/models`` names what was launched.

vLLM and SGLang are not here: they need a GPU, so a launch on either stays a
manual run against real hardware (``test_gateway.py::
test_a_real_tiny_model_launches_and_answers``).

Skips only when ``DERATE_E2E_ORIGIN`` is unset. Set, it is a promise -- a launch
that fails is a failure, not a skip, because "this machine cannot launch" is
exactly the finding. ``DERATE_E2E_ENGINE_MODEL`` swaps the checkpoint.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from tests.unit import e2e_support

E2E_ORIGIN = os.environ.get("DERATE_E2E_ORIGIN")

#: Small, ungated, and instruction-tuned, so it has a chat template. The
#: Q4_K_M file is ~105 MB: a download and a load measured in seconds, which is
#: the point -- the engine path is under test, not the model.
MODEL = os.environ.get(
    "DERATE_E2E_ENGINE_MODEL",
    "hf://bartowski/SmolLM2-135M-Instruct-GGUF/SmolLM2-135M-Instruct-Q4_K_M.gguf",
)

#: Image pull, weight download, sparkrun prep, engine start. Generous: the
#: first pull of the llama.cpp image is the long part on a cold runner.
READY_TIMEOUT_S = 900


def _wait_ready(http, origin: str, deployment_id: str) -> dict:
    """Poll to READY, or fail with the record's own last_error -- the refusal a
    person needs is the one the deployment recorded, not "timed out"."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last: dict = {}
    while time.monotonic() < deadline:
        reply = http.get(origin + "/api/deployments")
        reply.raise_for_status()
        rows = [d for d in reply.json() if d["deployment_id"] == deployment_id]
        if rows:
            last = rows[0]
            if last["state"] == "ready":
                return last
            if last["state"] in ("failed", "stopped"):
                pytest.fail("%s went %s: %s" % (deployment_id, last["state"], last.get("last_error")))
        time.sleep(5)
    pytest.fail(
        "%s never became ready in %ds; last state %r, last error %r"
        % (deployment_id, READY_TIMEOUT_S, last.get("state"), last.get("last_error"))
    )


@pytest.mark.slow
def test_a_real_llamacpp_engine_launches_and_streams():
    origin = e2e_support.require(E2E_ORIGIN, "DERATE_E2E_ORIGIN", "/healthz")
    http = e2e_support.client(origin)

    launched = http.post(
        origin + "/api/deployments",
        timeout=300,
        json={
            "model_id": MODEL,
            "runtime": "llamacpp",
            "concurrency": 1,
            "context_length": 2048,
        },
    )
    assert launched.status_code < 400, "launching %s: %s" % (MODEL, launched.text[:2000])
    dep = launched.json()["deployment_id"]

    try:
        record = _wait_ready(http, origin, dep)
        served = record["served_name"]

        listed = http.get(origin + "/v1/models")
        assert listed.status_code == 200, listed.text[:500]
        assert served in [m["id"] for m in listed.json()["data"]], (
            "%r is READY but /v1/models does not list it" % served
        )

        body = {
            "model": served,
            "messages": [{"role": "user", "content": "Say hello."}],
            "max_tokens": 16,
            "temperature": 0,
        }

        reply = http.post(origin + "/v1/chat/completions", timeout=120, json=body)
        assert reply.status_code == 200, reply.text[:2000]
        answer = reply.json()
        assert answer["choices"][0]["message"]["content"].strip(), "a 200 with no text"
        assert answer["usage"]["completion_tokens"] > 0, answer["usage"]

        frames: list[str] = []
        deltas: list[str] = []
        with http.stream(
            "POST", origin + "/v1/chat/completions", timeout=120, json={**body, "stream": True}
        ) as streamed:
            assert streamed.status_code == 200, streamed.read()[:2000]
            assert "text/event-stream" in streamed.headers.get("content-type", "")
            for line in streamed.iter_lines():
                if not line.startswith("data:"):
                    continue
                frame = line[len("data:"):].strip()
                frames.append(frame)
                if frame == "[DONE]":
                    break
                for choice in json.loads(frame).get("choices", []):
                    deltas.append((choice.get("delta") or {}).get("content") or "")

        assert frames and frames[-1] == "[DONE]", "the stream did not end with [DONE]: %r" % frames[-3:]
        assert len(frames) > 2, "one frame is a buffered answer, not a stream: %r" % frames
        assert "".join(deltas).strip(), "a stream whose deltas add up to nothing"
    finally:
        try:
            http.delete(origin + "/api/deployments/" + dep, timeout=120)
        except Exception:  # teardown must not mask why the test failed
            pass
