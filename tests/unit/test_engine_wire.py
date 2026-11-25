"""What derate reads off an inference engine's own HTTP surface, over a real
socket.

``test_metrics_scrape`` style tests feed ``metrics_scrape`` a string. The gap
is the half in front of the parser: the URL a ``backend_url`` ending in
``/v1`` turns into, a 200 versus a 404, an engine that is not there, and an
exposition with one series per engine rank. A stub engine on a real port answers
the way each runtime does -- vLLM exports Prometheus counters, llama.cpp's
server does not export the ``vllm:`` family at all -- and the reads must come
back as numbers for the first and as *no reading* for the others, never as zeros.

Hermetic and fast; the real engine is ``test_engine_e2e.py``.
"""

from __future__ import annotations

import http.server
import threading

import pytest

from control_plane import metrics_scrape

#: Two engine ranks, the shape a tensor-parallel launch exports. Counters sum
#: across ranks; the KV gauge is a fraction and takes the fullest rank.
VLLM_METRICS = """\
# HELP vllm:kv_cache_usage_perc KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0"} 0.25
vllm:kv_cache_usage_perc{engine="1"} 0.5
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0"} 2
vllm:num_requests_running{engine="1"} 1
# TYPE vllm:num_requests_waiting gauge
vllm:num_requests_waiting{engine="0"} 0
# TYPE vllm:num_preemptions_total counter
vllm:num_preemptions_total{engine="0"} 3
vllm:num_preemptions_total{engine="1"} 4
# TYPE vllm:spec_decode_num_drafts_total counter
vllm:spec_decode_num_drafts_total{engine="0"} 10
vllm:spec_decode_num_drafts_total{engine="1"} 10
# TYPE vllm:spec_decode_num_draft_tokens_total counter
vllm:spec_decode_num_draft_tokens_total{engine="0"} 30
vllm:spec_decode_num_draft_tokens_total{engine="1"} 30
# TYPE vllm:spec_decode_num_accepted_tokens_total counter
vllm:spec_decode_num_accepted_tokens_total{engine="0"} 18
vllm:spec_decode_num_accepted_tokens_total{engine="1"} 12
# TYPE vllm:spec_decode_num_accepted_tokens_per_pos_total counter
vllm:spec_decode_num_accepted_tokens_per_pos_total{engine="0",position="0"} 9
vllm:spec_decode_num_accepted_tokens_per_pos_total{engine="0",position="1"} 6
vllm:spec_decode_num_accepted_tokens_per_pos_total{engine="1",position="0"} 8
vllm:spec_decode_num_accepted_tokens_per_pos_total{engine="1",position="1"} 4
"""


def _engine(metrics: str | None):
    """A stub engine. ``metrics`` None means the runtime has no /metrics."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.path)
            if self.path == "/metrics" and metrics is not None:
                body = metrics.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
            else:
                body = b"{}"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


@pytest.fixture
def vllm():
    server, seen = _engine(VLLM_METRICS)
    yield "http://127.0.0.1:%d/v1" % server.server_port, seen
    server.shutdown()


def test_metrics_are_a_sibling_of_the_v1_base_not_a_child(vllm):
    url, seen = vllm
    assert metrics_scrape.fetch(url) is not None
    assert seen == ["/metrics"]


def test_load_is_summed_for_counters_and_fullest_rank_for_the_cache(vllm):
    load = metrics_scrape.read_engine_load(vllm[0])
    assert load is not None
    assert load.kv_cache_usage == 0.5  # fullest rank, not 0.75
    assert load.requests_running == 3
    assert load.requests_waiting == 0
    assert load.preemptions == 7


def test_speculation_is_summed_across_ranks_and_keeps_every_position(vllm):
    spec = metrics_scrape.read(vllm[0])
    assert spec is not None
    assert (spec.drafts, spec.draft_tokens, spec.accepted_tokens) == (20, 60, 30)
    assert spec.accepted_per_pos == [17, 10]
    assert spec.basis == "per_pos"


def test_an_engine_without_metrics_is_no_reading_not_zeros():
    """llama.cpp's server answers 404 here. The caller says so on screen; a
    zero would read as an idle cache and a preemption count of none."""
    server, _ = _engine(None)
    try:
        url = "http://127.0.0.1:%d/v1" % server.server_port
        assert metrics_scrape.fetch(url) is None
        assert metrics_scrape.read_engine_load(url) is None
        assert metrics_scrape.read(url) is None
    finally:
        server.shutdown()


def test_an_engine_that_is_not_there_is_no_reading():
    server, _ = _engine(VLLM_METRICS)
    url = "http://127.0.0.1:%d/v1" % server.server_port
    server.shutdown()
    server.server_close()
    assert metrics_scrape.read_engine_load(url, timeout=1.0) is None
