"""Configuration for the node shell. Off unless deliberately turned on.

Kept in its own module rather than added to ``registry/config.py`` for the same
reason ``telemetry/config.py`` is separate: this is the configuration of one
capability, and it is the capability whose defaults matter most. A reader
asking "can somebody get a root prompt on this box" should find the whole
answer in one short file.
"""

from __future__ import annotations

import os
from pathlib import Path

from control_plane.paths import data_dir as _data_dir

#: The file a node writes its generated key to when one was not supplied.
#: Under the data root, beside the telemetry databases, at 0600.
KEY_FILE = "shell.key"

#: Default login shell inside the session.
DEFAULT_SHELL = "/bin/bash"


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def enabled() -> bool:
    """Whether the shell route exists at all.

    **Off by default**, and read once at startup by the app factory rather than
    per request, so nothing arriving over the network can switch it on
    underneath a running process. Unset, the route is never registered rather
    than registered-and-refusing.

    This default is a deliberate decision by the operator of this cluster and
    is not a knob to be turned for convenience. It was briefly flipped to
    on-by-default on the reasoning that the key is the real gate and a present
    but unopenable door costs little. That reasoning is sound as far as it
    goes, and it still gives up the property worth most here: with the route
    absent there is nothing to fingerprint, nothing to attack, and no key
    material loaded on a node nobody intends to get a prompt on. Given this
    `/api` surface has no authentication, binds `0.0.0.0`, and is reachable
    across a tailnet, two gates are the point rather than redundancy.

    Defence in depth survives one of its layers being wrong. One gate does not.
    """
    return _flag("DERATE_SHELL", False)


def bootstrap_key(data_dir: Path | str | None = None) -> str:
    """Make sure a key exists before the route opens, and say where it is.

    Written to the console with ``print`` and deliberately NOT through
    ``logging``: the node's log handler ships records to the coordinator and
    archives them, and a secret in a queryable database is not out of band any
    more. A console is read by somebody already on the machine, which is
    exactly the audience this key is for.

    The key itself is printed only on the boot that generated it. After that
    only its path is, because a secret reprinted on every restart ends up in
    every scrollback, ``docker logs`` capture and journal on the box.
    """
    import sys

    had_one = bool(shell_key(data_dir))
    key = ensure_key(data_dir)
    path = key_path(data_dir)
    if had_one:
        note = f"derate: node shell is enabled; its key is in {path}"
    elif path.exists():
        note = (
            f"derate: node shell is enabled. Generated a key and wrote it to {path}:\n"
            f"    {key}\n"
            "    No route serves this. Copy it from here or from that file."
        )
    else:
        # A read-only root, or a data dir that could not be created. Still
        # usable for this run, but it will differ after a restart -- worth
        # saying now rather than leaving somebody to find their key stopped
        # working for no visible reason.
        note = (
            f"derate: node shell is enabled, but {path} could not be written, so\n"
            f"    this key lasts only until the process restarts:\n"
            f"    {key}\n"
            "    Set DERATE_SHELL_KEY to pin one, or DERATE_SHELL=0 to switch the shell off."
        )
    print(note, file=sys.stderr, flush=True)
    return key


def shell_binary() -> str:
    return os.environ.get("DERATE_SHELL_BINARY", DEFAULT_SHELL)


def key_path(data_dir: Path | str | None = None) -> Path:
    root = Path(data_dir) if data_dir is not None else _data_dir()
    return root / KEY_FILE


def shell_key(data_dir: Path | str | None = None) -> str:
    """The secret that opens a session, or "" if there is none.

    Env first, then the file. **Nothing mints this over HTTP and no route
    returns it**, which is the whole point: ``POST /api/enroll`` is
    unauthenticated and its token buys the permanent cluster token through
    ``POST /api/nodes/join``, so a gate built on the cluster token is a gate
    the network can open for itself. This one has to be read off the machine.
    """
    env = os.environ.get("DERATE_SHELL_KEY", "").strip()
    if env:
        return env
    try:
        return key_path(data_dir).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def ensure_key(data_dir: Path | str | None = None) -> str:
    """Return the key, generating and persisting one if there is none.

    Only called when the shell is enabled. A generated key is written 0600 and
    printed once to the node's own log, which is the out-of-band channel: it
    reaches somebody who can already read the machine's console, and nobody
    else. It is deliberately NOT an event on the telemetry bus -- that gets
    shipped to the coordinator and archived, and a secret that ends up in a
    queryable database is not out of band any more.
    """
    import secrets
    import stat

    existing = shell_key(data_dir)
    if existing:
        return existing
    key = secrets.token_urlsafe(24)
    path = key_path(data_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(key + "\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Cannot persist it: still return it, so the process that generated it
        # can be used this run. It will differ after a restart, which is worse
        # than stable but better than no shell at all on a read-only root.
        pass
    return key


def shell_origins() -> tuple[str, ...]:
    """Browser origins allowed to open a shell socket.

    Empty means "do not check", which is the same-origin case: a browser sends
    the page's own origin and this process cannot know what name the operator
    reached it by. Set ``DERATE_SHELL_ORIGINS`` when the UI is served from
    somewhere else, and the check becomes an allowlist.
    """
    raw = os.environ.get("DERATE_SHELL_ORIGINS", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def idle_timeout_s() -> float:
    """Close a session nobody has typed into. 0 disables.

    A root shell left open in a forgotten tab is the failure mode this exists
    for -- ``/v1/realtime`` has no equivalent and should not be the model here.
    """
    raw = os.environ.get("DERATE_SHELL_IDLE_S", "1800")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 1800.0
