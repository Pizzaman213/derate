"""The launch log a screen polls while a model downloads.

Two failures this pins, both found watching a real download from the node
page: the only bar that carries a download RATE (huggingface_hub's byte bar,
forced onto the pipe) interleaves with the file-count bar and ends every frame
in a cursor-up escape, so collapsing onto the previous line alone filled the
buffer with alternating frames; and a launch that had printed nothing yet
answered `none`, which a screen reads as "stop polling".
"""

from __future__ import annotations

import logging
import os
from unittest import mock

import pytest

from control_plane.contracts import DeploymentState as S
from control_plane.deploy.sparkrun import SparkrunAdapter
from tests import fixtures as fx
from tests.unit.test_deploy import FakeProbe, FakeRegistry, make_manager, wait_for

#: What `sparkrun run` printed through a pipe with TQDM_POSITION=-1, verbatim
#: from huggingface_hub 1.8.0 / tqdm 4.69.1 (the versions sparkrun ships),
#: after universal-newline splitting -- blanks and escapes included.
HF_FRAMES = [
    "",
    "Downloading (incomplete total...): 0.00B [00:00, ?B/s]\x1b[A",
    "",
    "Fetching 6 files:   0%|          | 0/6 [00:00<?, ?it/s]\x1b[A",
    "",
    "Downloading (incomplete total...):   1%|          | 9.82M/988M [00:00<00:19, 49.2MB/s]  \x1b[A",
    "",
    "Fetching 6 files:  50%|█████     | 3/6 [00:10<00:11,  3.72s/it]\x1b[A",
    "",
    "Downloading (incomplete total...):  45%|████▌     | 444M/988M [00:03<00:04, 120MB/s]\x1b[A",
]


def _launch(tmp_path, output):
    manager = make_manager(tmp_path, probe=FakeProbe(healthy_by_default=False))
    manager.adapter.launch_output = output
    dep = manager.launch(fx.QWEN3_30B_A3B, fx.single_node_plan(), fx.fits(), "vllm", 8192, 64)
    return manager, dep


@pytest.mark.parametrize("trust_remote_code", [False, True])
def test_the_launch_environment_turns_on_the_byte_bar_with_the_rate(tmp_path, trust_remote_code):
    """huggingface_hub's byte bar is `disable=is_tqdm_disabled(...)`, and None
    there means tqdm switches it off on a pipe -- leaving only the file count,
    which says `s/it` and nothing about speed. Asked of the library itself, in
    exactly the environment `sparkrun run` is handed, on both env branches."""
    hub_utils = pytest.importorskip("huggingface_hub.utils")
    adapter = SparkrunAdapter(FakeRegistry(fx.SPARK_01), recipe_dir=tmp_path / "recipes")
    adapter.gated_probe = lambda _model_id: False
    env = adapter._launch_env("Qwen/Qwen3-4B", trust_remote_code=trust_remote_code)
    with mock.patch.dict(os.environ, env, clear=True):
        assert hub_utils.is_tqdm_disabled(logging.INFO) is False


def test_interleaved_download_bars_keep_one_clean_frame_each_and_their_rate(tmp_path):
    manager, dep = _launch(
        tmp_path, ["[2/6] Building image", *HF_FRAMES, "[3/6] Distributing model"]
    )
    assert wait_for(
        lambda: "[3/6] Distributing model" in manager.log_tail(dep.deployment_id)["lines"]
    )
    lines = manager.log_tail(dep.deployment_id)["lines"]
    assert lines[0] == "[2/6] Building image"
    assert lines[-1] == "[3/6] Distributing model"
    middle = lines[1:-1]
    # One line per bar, each at its newest frame, nothing else between them.
    assert len(middle) == 2, middle
    downloading = [ln for ln in middle if ln.startswith("Downloading")]
    assert downloading == [
        "Downloading (incomplete total...):  45%|████▌     | 444M/988M [00:03<00:04, 120MB/s]"
    ]
    assert [ln for ln in middle if ln.startswith("Fetching")][0].startswith("Fetching 6 files:  50%")
    assert not any("\x1b" in ln or not ln.strip() for ln in lines)
    manager.close()


def test_a_frame_never_jumps_above_a_line_printed_after_it(tmp_path):
    first = "Fetching 6 files:  50%|#####     | 3/6 [00:10<00:11,  3.72s/it]"
    later = "Fetching 6 files: 100%|##########| 6/6 [00:20<00:00,  3.30s/it]"
    manager, dep = _launch(tmp_path, [first, "[3/6] Distributing model", later])
    assert wait_for(lambda: later in manager.log_tail(dep.deployment_id)["lines"])
    lines = manager.log_tail(dep.deployment_id)["lines"]
    assert lines.index(first) < lines.index("[3/6] Distributing model") < lines.index(later)
    manager.close()


def test_a_launch_that_has_printed_nothing_yet_answers_from_the_buffer(tmp_path):
    """Held behind another engine starting on its node: LAUNCHING, no lines,
    no cluster id. `none` here froze the node page's log for the whole wait."""
    manager = make_manager(tmp_path, probe=FakeProbe(healthy_by_default=False))
    node_ids = tuple(fx.single_node_plan().node_ids)
    assert manager._hold_starting(node_ids, deadline=manager._clock() + 5)
    dep = manager.launch(fx.QWEN3_30B_A3B, fx.single_node_plan(), fx.fits(), "vllm", 8192, 64)
    assert manager.get(dep.deployment_id).state is S.LAUNCHING
    answer = manager.log_tail(dep.deployment_id)
    assert answer["source"] == "buffer", answer
    assert answer["lines"] == []
    manager._release_starting(node_ids)
    manager.close()


def test_a_failed_launchs_kept_output_is_its_lines_not_its_frames():
    """`last_error` keeps the launcher's whole output. With the byte bar on, a
    download is ~10 frames a second; the evidence is the lines, the newest
    frame of each bar, and the error -- nothing in between, no escapes."""
    from control_plane.deploy.progress import collapse_frames

    error = "Error: No space left on device"
    kept = collapse_frames("\r\n".join(["[2/6] Building image", *HF_FRAMES * 50, error]))
    lines = kept.splitlines()
    assert lines[0] == "[2/6] Building image"
    assert lines[-1] == error
    assert len(lines) == 4, lines
    assert "\x1b" not in kept


def test_a_line_printed_onto_a_bars_last_frame_stays_its_own_line(tmp_path):
    """tqdm ends a frame with a cursor-up and no newline, and huggingface_hub
    leaves the byte bar open, so sparkrun's next step header is printed onto
    the bar's line. Stripping the escape glued them; worse, the glued line read
    as a frame of that bar and the next frame overwrote the header."""
    glued = (
        "Downloading (incomplete total...):  99%|█████████▉| 980M/988M "
        "[00:09<00:00, 120MB/s]\x1b[A[3/6] Distributing model"
    )
    manager, dep = _launch(
        tmp_path,
        [
            *HF_FRAMES,
            glued,
            "Downloading (incomplete total...): 100%|██████████| 988M/988M [00:10<00:00, 98MB/s]\x1b[A",
            "[4/6] Launching vllm runtime",
        ],
    )
    assert wait_for(
        lambda: "[4/6] Launching vllm runtime" in manager.log_tail(dep.deployment_id)["lines"]
    )
    lines = manager.log_tail(dep.deployment_id)["lines"]
    assert "[3/6] Distributing model" in lines, lines
    assert lines.index("[3/6] Distributing model") < lines.index("[4/6] Launching vllm runtime")
    assert not any("[3/6]" in ln and "MB/s" in ln for ln in lines), lines
    manager.close()


def test_a_second_download_does_not_overwrite_the_first_ones_totals():
    """A target and its speculative head download back to back under the same
    bar names, with nothing printed between at sparkrun's default verbosity."""
    from control_plane.deploy.progress import collapse_frames

    # Captured: the byte bar always ENDS past its total with the percent
    # dropped (gzip-served files add bytes and no total) -- never on `100%|`.
    target = "Downloading (incomplete total...): 1.52GB [00:33, 62.3MB/s]"
    kept = collapse_frames(
        "\n".join(
            [
                "Fetching 6 files:  50%|█████     | 3/6 [00:05<00:05,  1.68s/it]",
                "Downloading (incomplete total...):  77%|███████▋  | 1.17G/1.51G [00:25<00:07, 62.0MB/s]",
                "Fetching 6 files: 100%|██████████| 6/6 [00:33<00:00,  5.50s/it]",
                target,
                "Fetching 6 files:   0%|          | 0/6 [00:00<?, ?it/s]",
                "Downloading (incomplete total...):  10%|█         | 1.5M/14.9M [00:00<00:01, 9.0MB/s]",
            ]
        )
    ).splitlines()
    assert target in kept, kept
    assert kept.index(target) < kept.index(
        "Downloading (incomplete total...):  10%|█         | 1.5M/14.9M [00:00<00:01, 9.0MB/s]"
    )
    # Mid-download the first one still collapsed: 77% gave way to its end.
    assert not any("1.17G/1.51G" in ln for ln in kept), kept


def test_a_bar_rounding_to_100_percent_is_still_one_line():
    """tqdm prints `100%|` from 99.5%, and the byte bar's total grows as files
    start, so `100%` mid-download is common; only a RESTARTED clock is a new bar."""
    from control_plane.deploy.progress import collapse_frames

    kept = collapse_frames(
        "\n".join(
            [
                "Downloading (incomplete total...): 100%|█████████▉| 995M/1000M [00:09<00:00, 110MB/s]",
                "Downloading (incomplete total...): 100%|█████████▉| 997M/1000M [00:09<00:00, 110MB/s]",
                "Downloading (incomplete total...): 100%|██████████| 1.0G/1.0G [00:10<00:00, 100MB/s]",
                "Download complete: : 1.0GB [00:10, 100MB/s]",
                "Download complete: : 1.0GB [00:10, 99.8MB/s]",
            ]
        )
    ).splitlines()
    assert kept == [
        "Downloading (incomplete total...): 100%|██████████| 1.0G/1.0G [00:10<00:00, 100MB/s]",
        "Download complete: : 1.0GB [00:10, 99.8MB/s]",
    ], kept
