"""``gateway/isolated.py``: the stub the agents' MCP tool looks at.

An agent's shell keeps its exports, so the caller's environment is hostile
input here: it may name the live data dir, the live log dir (whose audit
mirror is the chain the coordinator verifies), the operator's audit sink, the
real HF cache, or an API token that would lock the stub's own screens.
"""

from __future__ import annotations

import pytest

from control_plane import paths
from control_plane.gateway.isolated import isolate

HOSTILE = {
    "DERATE_DATA_DIR": "/home/connor/derate-data",
    "DERATE_LOG_DIR": "/home/connor/derate-data/logs",
    "DERATE_AUDIT_SINK": "udp://127.0.0.1:5514",
    "DERATE_AUDIT_SINK_TOKEN": "t",
    "DERATE_API_TOKEN": "x",
    "DERATE_PUBLIC_ORIGIN": "https://derate.example",
    "HF_HUB_CACHE": "/home/connor/.cache/huggingface/hub",
    # Read BEFORE HF_HOME by registry/modelcache.py, and a real hub token.
    "HUGGINGFACE_HUB_CACHE": "/home/connor/.cache/huggingface/hub",
    "HUGGING_FACE_HUB_TOKEN": "hf_x",
    "https_proxy": "http://corp-proxy:3128",
    "PATH": "/usr/bin",
}


def test_a_hostile_environment_is_replaced_not_defaulted(tmp_path):
    env = dict(HOSTILE)
    isolate(tmp_path, env)

    assert paths.data_dir(env).is_relative_to(tmp_path.resolve())
    assert paths.logs_dir(env).is_relative_to(tmp_path.resolve())
    for gone in (
        "DERATE_AUDIT_SINK", "DERATE_AUDIT_SINK_TOKEN", "DERATE_API_TOKEN", "DERATE_PUBLIC_ORIGIN",
        "HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "HUGGING_FACE_HUB_TOKEN",
    ):
        assert gone not in env
    # Fenced, not merely unset: nothing outside loopback, no docker, no sparkrun, no hub.
    assert env["https_proxy"].startswith("http://127.0.0.1:")
    assert env["DOCKER_HOST"].startswith("unix:///nonexistent")
    assert env["DERATE_SPARKRUN_BIN"] == "/bin/false"
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["DERATE_AUTO_ADOPT_CONTAINERS"] == env["DERATE_AUTO_ADOPT_RUNTIMES"] == "0"
    assert env["PATH"] == "/usr/bin"


def test_a_data_dir_that_resolves_outside_the_root_is_refused(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "data").symlink_to(live)

    with pytest.raises(SystemExit, match="data dir"):
        isolate(root, {})


def test_a_root_that_already_holds_derate_state_is_refused(tmp_path):
    # A live data dir typed as ROOT: its logs/audit is the real chain.
    (tmp_path / "logs" / "audit").mkdir(parents=True)
    env = dict(HOSTILE)

    with pytest.raises(SystemExit, match="already holds derate state"):
        isolate(tmp_path, env)
    assert env == HOSTILE, "a refusal must not have half-rewritten the environment"
