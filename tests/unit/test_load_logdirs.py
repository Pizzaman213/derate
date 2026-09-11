"""Where the load harness puts its logs, and why it used to leave 96 of them.

Deliberately NOT in `test_load.py`, which is `pytestmark = pytest.mark.slow`
end to end: none of this needs a load run, and a regression test that only
runs under `-m slow` would not have caught the bug it exists for. The bug was
visible on every ordinary `pytest -m "not slow"`.
"""

from __future__ import annotations

import glob
import importlib
import os
import tempfile
import time

from tests.load import harness


def test_importing_the_harness_creates_no_directory():
    """THE regression test. `LOG_DIR` was a module-level `tempfile.mkdtemp`,
    and `tests/unit/test_load.py` imports this module at module scope -- so
    pytest's own collection made one on every run, including runs that
    deselected every load test without executing one. Measured on the
    development box: 96 directories, 92 of them empty. Killed runs were
    blamed; collection was doing most of it."""
    pattern = os.path.join(tempfile.gettempdir(), harness.LOG_DIR_PREFIX + "*")
    before = set(glob.glob(pattern))
    importlib.reload(harness)
    assert set(glob.glob(pattern)) == before


def test_the_directory_is_made_on_first_use_and_then_reused(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(harness, "_log_dir", None)
    monkeypatch.delenv("DERATE_LOAD_LOGS", raising=False)

    first = harness.log_dir()
    assert os.path.isdir(first)
    # Memoized: a second call is the same directory, not a second mkdtemp.
    assert harness.log_dir() == first
    assert len(list(tmp_path.iterdir())) == 1


def test_the_environment_still_wins(monkeypatch, tmp_path):
    monkeypatch.setattr(harness, "_log_dir", None)
    monkeypatch.setenv("DERATE_LOAD_LOGS", str(tmp_path / "named"))
    assert harness.log_dir() == str(tmp_path / "named")


# -- the sweep --------------------------------------------------------------


def _stale(path, age_s=7200.0):
    path.mkdir()
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def test_the_sweep_removes_only_empty_directories(monkeypatch, tmp_path):
    """The four non-empty ones on the development box are the log tails of
    runs that actually died -- the only copy of that evidence. They must
    survive every sweep, for ever."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(harness, "_log_dir", None)

    empty = _stale(tmp_path / (harness.LOG_DIR_PREFIX + "empty"))
    holds = _stale(tmp_path / (harness.LOG_DIR_PREFIX + "evidence"))
    (holds / "gateway.log").write_text("Traceback ...\n")

    removed = harness.sweep_stale_log_dirs()

    assert str(empty) in removed
    assert not empty.exists()
    assert holds.exists()
    assert (holds / "gateway.log").read_text().startswith("Traceback")


def test_a_recently_created_directory_survives(monkeypatch, tmp_path):
    """Another harness may be running on this box right now, and its directory
    is legitimately empty for the moment between mkdtemp and the first
    Proc.start."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(harness, "_log_dir", None)

    fresh = tmp_path / (harness.LOG_DIR_PREFIX + "fresh")
    fresh.mkdir()
    assert harness.sweep_stale_log_dirs() == []
    assert fresh.exists()


def test_unrelated_directories_are_never_touched(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(harness, "_log_dir", None)

    others = [
        _stale(tmp_path / "derate-live"),
        _stale(tmp_path / "pytest-of-connor"),
        _stale(tmp_path / "derate-load-logs"),  # the prefix without its dash
    ]
    assert harness.sweep_stale_log_dirs() == []
    assert all(p.exists() for p in others)


def test_this_runs_own_directory_is_never_swept(monkeypatch, tmp_path):
    """It is legitimately empty until the first Proc writes, and the sweep
    runs before that."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(harness, "_log_dir", None)
    monkeypatch.delenv("DERATE_LOAD_LOGS", raising=False)

    mine = harness.log_dir()
    old = time.time() - 7200.0
    os.utime(mine, (old, old))

    assert harness.sweep_stale_log_dirs() == []
    assert os.path.isdir(mine)


def test_a_directory_that_cannot_be_removed_is_skipped_rather_than_raised(
    monkeypatch, tmp_path
):
    """A load run must never fail because it could not tidy up: it was only
    ever asking for somewhere to put a log file."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(harness, "_log_dir", None)
    _stale(tmp_path / (harness.LOG_DIR_PREFIX + "locked"))

    def boom(path):
        raise PermissionError(path)

    monkeypatch.setattr(os, "rmdir", boom)
    assert harness.sweep_stale_log_dirs() == []
