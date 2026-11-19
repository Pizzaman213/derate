"""Streamed tokens reach the throughput window while they stream.

The cluster graph's response blocks are emitted at ``tokens_per_sec`` off the
1 Hz metrics frame. When tokens were only counted at ``complete()``, that rate
stayed at zero for the whole decode and then appeared, all at once, after the
response had finished -- the graph drew the reply after it was over. These pin
the live path: the rate moves during the stream, a token is never counted
twice, and a fast stream cannot overrun the window's fixed-size buffer.
"""

from __future__ import annotations

from control_plane.gateway.proxy import _StreamAccounting
from control_plane.gateway.stats import TargetStats


def test_rate_rises_while_streaming_not_after() -> None:
    st = TargetStats("t")
    st.begin()
    st.note_tokens(40, now=100.0)
    st.note_tokens(40, now=101.0)
    assert st.tokens_per_sec(10.0, now=101.0) == 8.0
    assert st.outstanding == 1, "nothing settled yet: the rate came from the live path"


def test_complete_adds_only_what_streaming_did_not() -> None:
    st = TargetStats("t")
    st.begin()
    st.note_tokens(30, now=100.0)
    st.complete(tokens=30, duration_s=1.0, now=100.5, already_counted=30)
    assert st.tokens_per_sec(10.0, now=100.5) == 3.0, "a streamed token is never counted twice"
    assert st.total_tokens == 30


def test_upstream_usage_above_the_frame_count_tops_up_the_difference() -> None:
    st = TargetStats("t")
    st.begin()
    st.note_tokens(20, now=100.0)
    st.complete(tokens=25, duration_s=1.0, now=100.5, already_counted=20)
    assert st.tokens_per_sec(10.0, now=100.5) == 2.5


def test_non_streaming_still_counts_at_complete() -> None:
    st = TargetStats("t")
    st.begin()
    st.complete(tokens=50, duration_s=2.0, now=100.0)
    assert st.tokens_per_sec(10.0, now=100.0) == 5.0


def test_a_fast_stream_does_not_overrun_the_window() -> None:
    # 1000 tok/s for 10 s, one token per chunk. Unbucketed, 10 000 entries
    # into a 512-slot deque would keep only the last half second.
    st = TargetStats("t")
    for i in range(10_000):
        st.note_tokens(1, now=100.0 + i / 1000)
    assert st.tokens_per_sec(10.0, now=109.999) == 1000.0


def test_stream_accounting_reports_each_token_as_it_passes() -> None:
    seen: list[int] = []
    acc = _StreamAccounting(0.0, on_tokens=seen.append)
    acc.note_chunk(b'data: {"a":1}\n\ndata: {"a":2}\n\n', 0.1)
    acc.note_chunk(b'data: {"a":3}\n\n', 0.2)
    acc.note_chunk(b"data: [DONE]\n\n", 0.3)
    assert seen == [2, 1]
    assert acc.live == 3 == acc.usage.tokens


def test_a_failing_live_counter_never_breaks_the_stream() -> None:
    def boom(_: int) -> None:
        raise RuntimeError("metrics down")

    acc = _StreamAccounting(0.0, on_tokens=boom)
    acc.note_chunk(b'data: {"a":1}\n\n', 0.1)
    assert acc.usage.tokens == 1
    assert acc.live == 0, "nothing was counted live, so settle adds it all"
