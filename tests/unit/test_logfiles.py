"""The centralized log folder: one place, two files, nothing leaked.

No network and no GPU. Every test here guards one of the three properties
``control_plane/logfiles.py`` claims: a startup that cannot write files still
starts, a key never reaches a file, and the folder is bounded.

The handlers go on the *real* root logger, because that is the only place they
can see ``gateway.proxy`` -- a record logged through a named logger reaches a
handler by propagation, and a stand-in root would receive nothing. The autouse
fixture below is what keeps that from leaking into the rest of the suite.
"""

from __future__ import annotations

import logging
import os
import pathlib

import pytest

from control_plane import logfiles
from control_plane.paths import logs_dir, project_root
from control_plane.redaction import Redactor

REAL_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"


@pytest.fixture(autouse=True)
def _clean_root():
    """Detach anything this module installs, whatever the test did."""
    root = logging.getLogger()
    before = root.level
    root.setLevel(logging.DEBUG)
    try:
        yield root
    finally:
        logfiles.uninstall()
        root.setLevel(before)


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """The developer's own log-folder variables must not decide what these assert.

    Spelled out one at a time rather than as a prefix on purpose:
    ``test_single_source.py`` greps the tree for that prefix and would report
    the wildcard as an undeclared variable.
    """
    for name in (
        "DERATE_LOG_DIR",
        "DERATE_LOG_FILES",
        "DERATE_LOG_MAX_BYTES",
        "DERATE_LOG_BACKUPS",
        "DERATE_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


def _read(folder, name):
    path = folder / name
    return path.read_text(encoding="utf-8") if path.exists() else ""


# -- where the folder is -----------------------------------------------------


class TestWhereItLands:
    def test_the_project_root_by_default(self):
        """Next to the code -- /home/connor/derate here, /opt/derate in the image."""
        assert logs_dir({}) == project_root() / "logs"

    def test_the_data_root_does_not_decide_it(self, tmp_path):
        """A regression guard, because it used to and the two look alike."""
        assert logs_dir({"DERATE_DATA_DIR": str(tmp_path)}) != tmp_path / "logs"

    def test_an_operator_who_names_a_folder_gets_it(self, tmp_path):
        named = tmp_path / "var" / "log" / "derate"
        env = {"DERATE_DATA_DIR": str(tmp_path), "DERATE_LOG_DIR": str(named)}
        assert logs_dir(env) == named

    def test_a_container_can_put_them_back_on_the_volume(self, tmp_path):
        """The documented one-liner for a deployment that wants `docker rm` survived."""
        assert logs_dir({"DERATE_LOG_DIR": "/data/logs"}) == pathlib.Path("/data/logs")

    def test_an_unwritable_project_root_falls_through_to_the_data_root(
        self, tmp_path, monkeypatch
    ):
        """A read-only checkout is not a reason to fail, or to pick nowhere."""
        blocked = tmp_path / "readonly-checkout"
        blocked.mkdir()
        blocked.chmod(0o500)
        monkeypatch.setattr("control_plane.paths.project_root", lambda: blocked)
        try:
            assert logs_dir({"DERATE_DATA_DIR": str(tmp_path)}) == tmp_path / "logs"
        finally:
            blocked.chmod(0o700)

    def test_asking_where_does_not_create_it(self, tmp_path):
        folder = logs_dir({"DERATE_LOG_DIR": str(tmp_path / "nope")})
        assert not folder.exists()


# -- the two files -----------------------------------------------------------


class TestTheTwoFiles:
    def test_install_creates_the_folder_and_both_files(self, tmp_path):
        folder = tmp_path / "logs"
        assert logfiles.install(directory=folder, level="DEBUG") == folder
        assert (folder / logfiles.NODE_LOG).exists()
        assert (folder / logfiles.PROXY_LOG).exists()

    def test_the_wide_file_takes_everything(self, tmp_path):
        logfiles.install(directory=tmp_path, level="DEBUG")
        logging.getLogger("control_plane.registry.roster").info("roster line")
        logging.getLogger("gateway.proxy").info("proxy line")
        wide = _read(tmp_path, logfiles.NODE_LOG)
        assert "roster line" in wide
        assert "proxy line" in wide

    def test_the_narrow_file_takes_only_the_request_path(self, tmp_path):
        logfiles.install(directory=tmp_path, level="DEBUG")
        logging.getLogger("control_plane.registry.roster").info("roster line")
        logging.getLogger("control_plane.links.service").info("link line")
        for name in logfiles.PROXY_LOGGERS:
            logging.getLogger(name).info("from %s", name)
        narrow = _read(tmp_path, logfiles.PROXY_LOG)
        assert "roster line" not in narrow
        assert "link line" not in narrow
        for name in logfiles.PROXY_LOGGERS:
            assert f"from {name}" in narrow

    def test_a_child_of_a_proxy_logger_is_the_proxy(self, tmp_path):
        """``control_plane.providers`` is a prefix, not an exact name."""
        logfiles.install(directory=tmp_path, level="DEBUG")
        logging.getLogger("control_plane.providers.service").warning("upstream 502")
        assert "upstream 502" in _read(tmp_path, logfiles.PROXY_LOG)

    def test_a_traceback_stays_whole_in_the_narrow_file(self, tmp_path):
        """The reason the narrow file exists rather than a grep of the wide one.

        A continuation line carries no logger name, so ``grep gateway.proxy``
        keeps the first line of a failure and drops the exception under it.
        Filtering at the handler keeps the record.
        """
        logfiles.install(directory=tmp_path, level="DEBUG")
        try:
            raise ValueError("upstream closed the connection")
        except ValueError:
            logging.getLogger("gateway.proxy").exception("forward failed")
        narrow = _read(tmp_path, logfiles.PROXY_LOG)
        assert "forward failed" in narrow
        assert "Traceback (most recent call last)" in narrow
        assert "ValueError: upstream closed the connection" in narrow

    def test_stderr_still_gets_everything(self, tmp_path, capsys):
        """Files are additive. Removing the console is not what this does."""
        root = logging.getLogger()
        console = logging.StreamHandler()
        root.addHandler(console)
        try:
            logfiles.install(directory=tmp_path, level="DEBUG")
            logging.getLogger("gateway.proxy").warning("still on stderr")
        finally:
            root.removeHandler(console)
        assert "still on stderr" in capsys.readouterr().err
        assert "still on stderr" in _read(tmp_path, logfiles.NODE_LOG)


# -- nothing leaks -----------------------------------------------------------


class TestNothingLeaks:
    def test_a_key_shape_never_reaches_either_file(self, tmp_path):
        """No redactor was passed: the vendor patterns alone must catch this."""
        logfiles.install(directory=tmp_path, level="DEBUG")
        logging.getLogger("gateway.proxy").warning("upstream said %s", REAL_KEY)
        for name in (logfiles.NODE_LOG, logfiles.PROXY_LOG):
            body = _read(tmp_path, name)
            assert REAL_KEY not in body
            assert "***" in body

    def test_adopting_the_service_redactor_scrubs_a_remembered_value(self, tmp_path):
        """A value with no recognisable shape is only scrubbed once remembered."""
        opaque = "hunter2-not-a-vendor-prefix-9081726354"
        logfiles.install(directory=tmp_path, level="DEBUG")
        logging.getLogger("gateway.proxy").warning("before: %s", opaque)
        assert opaque in _read(tmp_path, logfiles.NODE_LOG)

        redactor = Redactor()
        redactor.remember(opaque)
        logfiles.adopt_redactor(redactor)
        logging.getLogger("gateway.proxy").warning("after: %s", opaque)

        wide = _read(tmp_path, logfiles.NODE_LOG)
        assert "after: ***" in wide
        assert wide.count(opaque) == 1  # the "before" line, and only it

    def test_adopting_does_not_stack_filters(self, tmp_path):
        logfiles.install(directory=tmp_path, level="DEBUG")
        for _ in range(3):
            logfiles.adopt_redactor(Redactor())
        installed = [
            h for h in logging.getLogger().handlers
            if getattr(h, "_derate_logfile", None)
        ]
        assert len(installed) == 2
        for handler in installed:
            redacting = [
                f for f in handler.filters if f.__class__.__name__ == "SecretRedactingFilter"
            ]
            assert len(redacting) == 1

    def test_adopting_nothing_is_a_no_op(self, tmp_path):
        logfiles.install(directory=tmp_path, level="DEBUG")
        logfiles.adopt_redactor(None)
        logging.getLogger("gateway.proxy").warning("still writing")
        assert "still writing" in _read(tmp_path, logfiles.NODE_LOG)


# -- it never fails a startup ------------------------------------------------


class TestItNeverFailsAStartup:
    def test_an_unwritable_folder_is_a_warning_not_an_exception(self, tmp_path):
        blocked = tmp_path / "read-only"
        blocked.mkdir()
        blocked.chmod(0o500)
        try:
            assert logfiles.install(directory=blocked / "logs") is None
        finally:
            blocked.chmod(0o700)
        assert not [
            h for h in logging.getLogger().handlers
            if getattr(h, "_derate_logfile", None)
        ]

    def test_a_file_that_is_a_directory_is_a_warning_not_an_exception(self, tmp_path):
        (tmp_path / logfiles.NODE_LOG).mkdir()
        assert logfiles.install(directory=tmp_path) is None
        assert not [
            h for h in logging.getLogger().handlers
            if getattr(h, "_derate_logfile", None)
        ]

    def test_the_off_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DERATE_LOG_FILES", "0")
        assert logfiles.install(directory=tmp_path) is None
        assert not (tmp_path / logfiles.NODE_LOG).exists()

    def test_installing_twice_does_not_double_the_handlers(self, tmp_path):
        assert logfiles.install(directory=tmp_path, level="DEBUG") == tmp_path
        assert logfiles.install(directory=tmp_path, level="DEBUG") == tmp_path
        logging.getLogger("gateway.proxy").warning("once")
        assert _read(tmp_path, logfiles.NODE_LOG).count("once") == 1

    def test_uninstall_releases_the_files(self, tmp_path):
        logfiles.install(directory=tmp_path, level="DEBUG")
        logfiles.uninstall()
        logging.getLogger("gateway.proxy").warning("after uninstall")
        assert "after uninstall" not in _read(tmp_path, logfiles.NODE_LOG)


# -- the folder is bounded ---------------------------------------------------


class TestBounded:
    def test_it_rotates_and_stops(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DERATE_LOG_MAX_BYTES", "2048")
        monkeypatch.setenv("DERATE_LOG_BACKUPS", "2")
        logfiles.install(directory=tmp_path, level="DEBUG")
        line = "x" * 200
        for _ in range(400):
            logging.getLogger("gateway.proxy").warning(line)
        logfiles.uninstall()

        for stem in (logfiles.NODE_LOG, logfiles.PROXY_LOG):
            kept = sorted(p.name for p in tmp_path.glob(f"{stem}*"))
            assert kept == [stem, f"{stem}.1", f"{stem}.2"]
        # 400 x ~200 bytes is 80 KB written into a 6 KB ceiling per file.
        assert sum(p.stat().st_size for p in tmp_path.iterdir()) < 20 * 1024

    def test_the_defaults_are_the_declared_ones(self, tmp_path):
        logfiles.install(directory=tmp_path)
        handler = next(
            h for h in logging.getLogger().handlers
            if getattr(h, "_derate_logfile", None) == logfiles.NODE_LOG
        )
        assert handler.maxBytes == logfiles.LOG_MAX_BYTES
        assert handler.backupCount == logfiles.LOG_BACKUPS


# -- reading a tail back -------------------------------------------------


class TestTail:
    def test_a_missing_file_is_unavailable_not_an_error(self, tmp_path):
        result = logfiles.tail("node", env={"DERATE_LOG_DIR": str(tmp_path)})
        assert result == {
            "lines": [],
            "path": str(tmp_path / logfiles.NODE_LOG),
            "truncated": False,
            "available": False,
            "reason": f"No {logfiles.NODE_LOG} yet on this machine.",
        }

    def test_fewer_lines_than_the_limit_come_back_whole(self, tmp_path):
        (tmp_path / logfiles.NODE_LOG).write_text("a\nb\nc\n", encoding="utf-8")
        result = logfiles.tail("node", limit=10, env={"DERATE_LOG_DIR": str(tmp_path)})
        assert result["lines"] == ["a", "b", "c"]
        assert result["available"] is True
        assert result["truncated"] is False

    def test_more_lines_than_the_limit_keeps_only_the_newest(self, tmp_path):
        (tmp_path / logfiles.NODE_LOG).write_text(
            "\n".join(f"line {i}" for i in range(100)) + "\n", encoding="utf-8"
        )
        result = logfiles.tail("node", limit=3, env={"DERATE_LOG_DIR": str(tmp_path)})
        assert result["lines"] == ["line 97", "line 98", "line 99"]
        # There was more file than this asked for, but the byte cap was never
        # touched -- the two truncation reasons must not be conflated.
        assert result["truncated"] is False

    def test_a_file_bigger_than_max_bytes_is_read_bounded_and_says_so(self, tmp_path):
        lines = [f"line {i:05d} of a large file" for i in range(20000)]
        whole = "\n".join(lines) + "\n"
        (tmp_path / logfiles.PROXY_LOG).write_text(whole, encoding="utf-8")
        max_bytes = 4096
        result = logfiles.tail(
            "proxy", limit=500, max_bytes=max_bytes, env={"DERATE_LOG_DIR": str(tmp_path)}
        )
        # Ground truth computed on the whole file, so the bounded read's
        # partial-leading-line handling is checked against a real split
        # rather than trusted blind.
        expected = whole.split("\n")
        if expected and expected[-1] == "":
            expected = expected[:-1]
        # The byte cap left fewer lines available than the 500 asked for --
        # that shortfall is exactly what "truncated" reports.
        assert 0 < len(result["lines"]) < 500
        assert result["lines"] == expected[-len(result["lines"]):]
        assert result["truncated"] is True

    def test_which_must_be_node_or_proxy(self, tmp_path):
        with pytest.raises(ValueError):
            logfiles.tail("bogus", env={"DERATE_LOG_DIR": str(tmp_path)})


# -- the worker path stays light ---------------------------------------------


def test_importing_logfiles_does_not_pull_the_provider_stack():
    """``node.py``'s contract: a worker imports nothing from providers.

    This is why the scrubber lives in ``control_plane/redaction.py``. The
    import is done in a subprocess because the rest of the suite has long since
    imported everything.
    """
    import subprocess
    import sys

    code = (
        "import sys; import control_plane.logfiles; "
        "print('httpx' in sys.modules, "
        "[m for m in sys.modules if m.startswith('control_plane.providers')])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False []", out.stdout
