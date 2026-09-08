"""Where persistent state lives, on whichever machine this is.

Eleven modules used to answer this question independently, each with the same
``os.environ.get("DERATE_DATA_DIR", "/data")``. That is right in the container
and quietly wrong everywhere else, because ``/`` is not writable on macOS and
every writer under ``/data`` swallows the failure with a ``log.warning`` rather
than raising -- identity.py, roster.py, enrollment.py and shell_config.py all
make that tradeoff deliberately, so a node can keep running on a read-only
volume. Stacked together the effect is that a coordinator on a laptop
regenerates its cluster token on every start and stops recognising its own
workers, having reported nothing worse than a warning.

So the fallback has to be somewhere writable *before* the write is attempted.
``resolver/cache.py`` already worked this out for its own cache; this is that
function generalised and given the whole estate to hold.

The container is unaffected: ``DERATE_DATA_DIR`` is set by
``docker/entrypoint.sh``, and even unset, ``/data`` exists and is writable
there, so it is still chosen. Nothing about the deployed path changes.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path

#: The container's data volume. Preferred whenever it is real and writable,
#: including on a Linux host that happens to have one, so a native install on
#: a node that previously ran the image finds the same state.
CONTAINER_DATA_DIR = Path("/data")

_APP = "derate"


def _platform_default() -> Path:
    """Where this OS expects an application to keep state.

    Not ``~/.derate``: on macOS and Windows a dotfile in the home directory is
    the wrong convention, and the estate here includes SQLite journals that a
    backup tool should be able to recognise and a sync tool should not try to
    merge.
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / _APP
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / _APP
        return Path.home() / "AppData" / "Local" / _APP
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / _APP
    return Path.home() / ".local" / "share" / _APP


def _usable(path: Path) -> bool:
    """Can we actually write here *now*?

    Existence is not enough. On Windows ``Path("/data")`` is drive-relative and
    resolves to ``C:\\data``, which is creatable, so a bare ``exists()`` check
    would put the estate somewhere nobody would think to look. And a mount that
    is present but read-only is exactly the case this module exists to catch.
    """
    try:
        return path.is_dir() and os.access(path, os.W_OK)
    except OSError:
        return False


def default_data_dir() -> Path:
    """The data root when nothing named one. Never returns an unwritable path.

    ``/data`` when it is real and writable -- which is the container, and a
    Linux host that once ran the image -- else this platform's
    application-state directory.
    """
    if _usable(CONTAINER_DATA_DIR):
        return CONTAINER_DATA_DIR
    return _platform_default()


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The data root for this process. Never raises, never returns unwritable.

    ``DERATE_DATA_DIR`` wins unconditionally -- an operator naming a path is
    entitled to be wrong about it, and second-guessing them here would make the
    variable untrustworthy. Otherwise :func:`default_data_dir`.

    *env* exists for ``RegistryConfig.from_env``, which takes the environment
    as an argument so it can be tested without touching the real one. Passing
    it here rather than reading ``os.environ`` keeps that property intact.

    The directory is *not* created here. Callers that write create their own
    parents, and a read-only caller asking where the estate is should not have
    the side effect of making one.
    """
    env = os.environ if env is None else env
    named = env.get("DERATE_DATA_DIR")
    if named:
        return Path(named)
    return default_data_dir()


def data_path(*parts: str) -> Path:
    """``data_dir()`` joined with *parts*. For the one-liner call sites."""
    return data_dir().joinpath(*parts)


#: The log folder's name, under whichever root holds it.
LOGS_DIR = "logs"


def project_root() -> Path:
    """The directory the code was installed into: the one holding ``control_plane``.

    ``/home/connor/derate`` in a source checkout, ``/opt/derate`` in the image
    (Dockerfile ``WORKDIR /opt/derate``, with ``control_plane/`` copied under
    it). Derived from this file's own location rather than from a cwd, because
    the entrypoint's working directory is not the process's for long and a node
    agent resolves this after uvicorn has been running for a week.
    """
    return Path(__file__).resolve().parent.parent


def logs_dir(env: Mapping[str, str] | None = None) -> Path:
    """The one folder a derate process writes its log files into.

    ``DERATE_LOG_DIR`` wins unconditionally, for the reason :func:`data_dir`
    gives: an operator naming ``/var/log/derate`` is entitled to be obeyed, and
    a resolver that second-guesses the name makes the variable untrustworthy.

    Otherwise ``<project root>/logs`` -- next to the code, where somebody
    debugging is already standing. That is a different choice from every other
    writer in this module, which put its state under :func:`data_dir`, and the
    tradeoff is worth stating: in the container the project root is
    ``/opt/derate``, which is a layer and not the ``derate:/data`` volume, so
    logs there do not survive ``docker rm``. A deployment that wants them to
    sets ``DERATE_LOG_DIR=/data/logs`` and gets the old behaviour exactly.

    The one thing this will not do is return somewhere unwritable. A checkout
    on a read-only mount, or a ``control_plane`` imported out of a
    site-packages nobody can write to, falls through to the data root -- which
    :func:`default_data_dir` has already guaranteed is writable. A log folder
    is not worth failing a startup over, and ``logfiles.install`` degrades to
    stderr rather than raising if even that is gone.

    Not created here -- ``data_dir`` does not create its directory either, and
    a caller asking where the logs are should not have the side effect of
    making somewhere to put them. :func:`control_plane.logfiles.install` is
    what creates it, because it is the one caller that is about to write.
    """
    env = os.environ if env is None else env
    named = env.get("DERATE_LOG_DIR")
    if named:
        return Path(named)
    root = project_root()
    if _usable(root):
        return root / LOGS_DIR
    return data_dir(env) / LOGS_DIR
