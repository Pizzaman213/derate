"""R2/R3/R4/R6/R7/E2/E3/E4/SC-05 record side: remote code is opt-in and
announced, local model paths are confined, hub ids are checked before the
network, launches record what they trusted and ran, and the small file/URL
sinks are sanitised."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from control_plane.deploy.recipes import check_extra_args_safe, synthesize
from control_plane.deploy.store import safe_deployment_id
from control_plane.gateway import create_app
from control_plane.registry import shell_config
from control_plane.resolver import ModelResolver
from control_plane.resolver.cache import ShapeCache
from control_plane.resolver.hf import is_hub_id
from control_plane.resolver.types import ModelNotFound
from tests import fixtures as fx
from tests.unit.test_deploy import FakeAdapter, make_manager, wait_for
from tests.unit.test_gateway import FakeDeployments, build_deps

DATA = Path(__file__).resolve().parents[1] / "resolver_data"


def _config(name: str) -> dict:
    return json.loads((DATA / f"{name}.config.json").read_text())


def _synth(tmp_path, **kw):
    return synthesize(
        fx.GPT_OSS_120B, fx.pp2_plan(), kw.pop("runtime", "vllm"), 8192, 8,
        "m", port=8100, gpu_memory_utilization=0.5, recipe_dir=tmp_path, **kw,
    )


# -- R2: --trust-remote-code is opt-in -------------------------------------


@pytest.mark.parametrize("runtime", ["vllm", "sglang", "tts"])
def test_serve_command_has_no_remote_code_flag_unless_set(tmp_path, runtime):
    plan = fx.single_node_plan()
    off = synthesize(
        fx.GPT_OSS_120B, plan, runtime, 8192, 8, "m", port=8100,
        gpu_memory_utilization=0.5, recipe_dir=tmp_path,
    )
    on = synthesize(
        fx.GPT_OSS_120B, plan, runtime, 8192, 8, "m", port=8100,
        gpu_memory_utilization=0.5, recipe_dir=tmp_path, trust_remote_code=True,
    )
    assert "--trust-remote-code" not in off.content
    assert "--trust-remote-code" in on.content


def test_custom_command_gets_the_flag_only_from_the_field(tmp_path):
    off = _synth(tmp_path, custom_command=("--max-model-len", "4096"))
    on = _synth(tmp_path, custom_command=("--max-model-len", "4096"), trust_remote_code=True)
    assert "--trust-remote-code" not in off.content
    assert "--trust-remote-code" in on.content


@pytest.mark.parametrize("token", ["--trust-remote-code", "--trust-remote-code=true"])
def test_the_flag_is_refused_as_free_text(token):
    with pytest.raises(ValueError, match="trust_remote_code field"):
        check_extra_args_safe((token,))
    with pytest.raises(ValueError, match="trust_remote_code field"):
        check_extra_args_safe((token,), field="custom_command")


def test_resolver_flags_auto_map(tmp_path):
    resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "c"))
    plain = resolver.resolve_config(_config("llama-3.3-70b"), "meta-llama/Llama-3.3-70B")
    assert plain.remote_code is False
    assert not any("--trust-remote-code" in w for w in plain.warnings)

    config = dict(_config("llama-3.3-70b"), auto_map={"AutoModel": "x.Model"})
    res = resolver.resolve_config(config, "evil/typosquat")
    assert res.remote_code is True
    assert any(
        w.startswith("this repository ships Python that --trust-remote-code would execute")
        and "auto_map" in w
        for w in res.warnings
    )


def test_resolver_flags_py_files_in_a_local_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_MODEL_DIRS", str(tmp_path))
    model = tmp_path / "m"
    model.mkdir()
    (model / "config.json").write_text(json.dumps(_config("llama-3.3-70b")))
    (model / "modeling_evil.py").write_text("import os\n")
    resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "c"))
    res = resolver.resolve_full(str(model))
    assert res.remote_code is True
    assert any("modeling_evil.py" in w for w in res.warnings)


def test_remote_code_survives_the_cache_round_trip(tmp_path):
    from control_plane.resolver.cache import from_dict, to_dict

    resolver = ModelResolver(cache=ShapeCache(directory=tmp_path / "c"))
    config = dict(_config("llama-3.3-70b"), auto_map={"AutoModel": "x.Model"})
    res = resolver.resolve_config(config, "evil/typosquat")
    assert from_dict(json.loads(json.dumps(to_dict(res)))).remote_code is True


# -- R3: local paths are confined ------------------------------------------


@pytest.fixture
def confined(tmp_path, monkeypatch):
    monkeypatch.setenv("DERATE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DERATE_HF_CACHE", str(tmp_path / "hf"))
    monkeypatch.delenv("DERATE_MODEL_DIRS", raising=False)
    (tmp_path / "data").mkdir()
    (tmp_path / "hf").mkdir()
    return ModelResolver(cache=ShapeCache(directory=tmp_path / "c"))


def test_local_path_outside_roots_is_not_found_without_echoing_it(tmp_path, confined):
    outside = tmp_path / "outside-secret"
    outside.mkdir()
    (outside / "config.json").write_text(json.dumps(_config("llama-3.3-70b")))
    for path in (str(outside), str(tmp_path / "missing-secret"), "~/.docker"):
        with pytest.raises(ModelNotFound) as exc:
            confined.resolve_full(path)
        assert "secret" not in str(exc.value)
        assert ".docker" not in str(exc.value)
    # Same refusal for an existing and a missing path: no existence oracle.
    with pytest.raises(ModelNotFound) as a:
        confined.resolve_full(str(outside))
    with pytest.raises(ModelNotFound) as b:
        confined.resolve_full(str(tmp_path / "missing-secret"))
    assert str(a.value) == str(b.value)


def test_local_path_inside_a_root_still_resolves(tmp_path, confined):
    model = tmp_path / "data" / "m"
    model.mkdir()
    (model / "config.json").write_text(json.dumps(_config("llama-3.3-70b")))
    assert confined.resolve_full(str(model)).shape.total_params > 0


def test_traversal_out_of_a_root_is_refused(tmp_path, confined):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.json").write_text(json.dumps(_config("llama-3.3-70b")))
    with pytest.raises(ModelNotFound):
        confined.resolve_full(str(tmp_path / "data" / ".." / "outside"))


# -- R4: hub id grammar, before the network --------------------------------


class _NoNetwork:
    def __getattr__(self, name):
        raise AssertionError(f"network reached: {name}")


@pytest.mark.parametrize("good", ["meta-llama/Llama-3.3-70B", "Qwen/Qwen3-0.6B", "a/b.c_d-e"])
def test_is_hub_id_accepts(good):
    assert is_hub_id(good)


@pytest.mark.parametrize(
    "bad", ["org/..", "../x", "org/../x", "org", "org/x/y", "-org/x", "org/x?y", "org/x#y", ""]
)
def test_is_hub_id_refuses(bad):
    assert not is_hub_id(bad)


@pytest.mark.parametrize("model_id", ["org/..", "hf://org/../x.gguf", "hf://../x/y.gguf", "hf://o/r/../x.gguf"])
def test_traversal_ids_never_reach_the_hub(tmp_path, model_id):
    resolver = ModelResolver(client=_NoNetwork(), cache=ShapeCache(directory=tmp_path / "c"))
    with pytest.raises(ModelNotFound):
        resolver.resolve_full(model_id)


# -- R6 / SC-05: the launch record -----------------------------------------


class _TrustingDeployments(FakeDeployments):
    def launch(self, shape, plan, fit, runtime, ctx, max_seqs, *, trust_remote_code=False, **kw):
        dep = super().launch(shape, plan, fit, runtime, ctx, max_seqs, **kw)
        dep.trust_remote_code = trust_remote_code
        dep.image = "ghcr.io/example/vllm@sha256:" + "0" * 64
        return dep


def _launched_row(client, dep_id: str) -> dict:
    entries = client.get("/api/audit", params={"page_size": 500}).json()["entries"]
    rows = [e for e in entries if e["action"] == "deployment_launched" and e["target_id"] == dep_id]
    assert len(rows) == 1, rows
    return rows[0]






class _RecordingAdapter(FakeAdapter):
    def launch(self, *args, trust_remote_code=False, **kw):
        self.trusted = trust_remote_code
        return super().launch(*args, **kw)




# -- E4: the failed-log name -----------------------------------------------


def test_safe_deployment_id():
    assert safe_deployment_id("../../etc/passwd") == "etcpasswd"
    assert safe_deployment_id("d-1_x") == "d-1_x"
    assert safe_deployment_id("dé1") == "d1"
    with pytest.raises(ValueError):
        safe_deployment_id("../..")


def test_archived_log_name_cannot_leave_its_directory(tmp_path):
    manager = make_manager(tmp_path)
    manager.failed_log_dir.mkdir(parents=True, exist_ok=True)
    (manager.failed_log_dir / "x.log").write_text("inside\n")
    (tmp_path / "outside.log").write_text("outside\n")
    assert manager._archived_log("../outside", limit=5) == []
    assert manager._archived_log("../x", limit=5) == ["inside"]
    assert manager._archived_log("..", limit=5) == []


# -- E2: cache-delete folder ------------------------------------------------




# -- R7: avatar fan-out ------------------------------------------------------










# -- E3: shell key -----------------------------------------------------------


def test_shell_key_is_created_0600_even_under_a_permissive_umask(tmp_path, monkeypatch):
    monkeypatch.delenv("DERATE_SHELL_KEY", raising=False)
    old = os.umask(0)
    try:
        key = shell_config.ensure_key(tmp_path)
    finally:
        os.umask(old)
    path = shell_config.key_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == key


def test_shell_key_never_writes_through_a_planted_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DERATE_SHELL_KEY", raising=False)
    target = tmp_path / "victim"
    target.write_text("")
    path = shell_config.key_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target)
    key = shell_config.ensure_key(tmp_path)
    assert key  # still usable this run
    assert target.read_text() == ""


@pytest.mark.parametrize(
    "token",
    [
        "--tool-parser-plugin=/cache/huggingface/hub/models--Org--Repo/snapshots/abc/evil.py",
        "--reasoning-parser-plugin",
        "--middleware=evil.mod",
    ],
)
def test_code_loading_flags_are_not_free_text(token):
    """R-1: a plugin-path flag execs Python; it may not ride in as extra_args."""
    with pytest.raises(ValueError, match="loads Python"):
        check_extra_args_safe([token])
