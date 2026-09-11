"""Reconstructing a launch from what is already running.

deploy/adopt.py's parser and deploy/manager.py::adopt(), the two pieces that
do not need a live node agent or a real docker socket to test. Live
end-to-end coverage (node agent -> ancestor walk -> parse -> identity probe
-> adopt) needs a real GPU node and is exercised manually, not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from control_plane.contracts import DeploymentOrigin, DeploymentState as S  # noqa: E402
from control_plane.deploy import events as ev  # noqa: E402
from control_plane.deploy.adopt import (  # noqa: E402
    cluster_id_from_container_name,
    parse_serve_command,
)
from control_plane.deploy.autoadopt import ContainerAutoAdopter  # noqa: E402
from control_plane.deploy.manager import DeploymentManager  # noqa: E402
from control_plane.procmatch import matches_deployment  # noqa: E402
from tests import fixtures as fx  # noqa: E402

VLLM_COMMAND = (
    "/usr/bin/python3 /usr/local/bin/vllm serve Qwen/Qwen2.5-0.5B-Instruct "
    "--served-model-name Qwen2.5-0.5B-Instruct --host 0.0.0.0 --port 8101 "
    "--tensor-parallel-size 1 --pipeline-parallel-size 1 --max-model-len 32768 "
    "--max-num-seqs 1 --gpu-memory-utilization 0.05 --trust-remote-code "
    "--kv-cache-memory-bytes 455081984"
)

TTS_COMMAND = (
    "python3 -m control_plane.runtimes.tts --model derate/audio8-tts-preview-0.6b "
    "--served-model-name Audio8-TTS-Preview-0.6b --host 0.0.0.0 --port 8090 "
    "--tensor-parallel-size 1 --pipeline-parallel-size 1 --max-model-len 4096 "
    "--max-num-seqs 4 --gpu-memory-utilization 0.10 --trust-remote-code"
)

SGLANG_COMMAND = (
    "python3 -m sglang.launch_server --model-path Qwen/Qwen3-8B "
    "--served-model-name Qwen3-8B --host 0.0.0.0 --port 8200 --tp-size 2 --pp-size 1 "
    "--context-length 40960 --max-running-requests 8 --mem-fraction-static 0.30 "
    "--trust-remote-code --enable-ep-moe"
)


# ==========================================================================
# parse_serve_command: mirrors flags.py's three command templates.
# ==========================================================================


def test_a_vllm_serve_command_parses_every_flag_routing_needs():
    spec = parse_serve_command(VLLM_COMMAND)
    assert spec is not None
    assert spec.runtime == "vllm"
    assert spec.model_id == "Qwen/Qwen2.5-0.5B-Instruct"
    assert spec.served_name == "Qwen2.5-0.5B-Instruct"
    assert spec.port == 8101
    assert spec.tensor_parallel == 1
    assert spec.pipeline_parallel == 1
    assert spec.context_length == 32768
    assert spec.max_concurrent_seqs == 1
    assert spec.gpu_memory_utilization == 0.05
    assert spec.expert_parallel is False


def test_a_tts_serve_command_parses_as_its_own_runtime():
    spec = parse_serve_command(TTS_COMMAND)
    assert spec is not None
    assert spec.runtime == "tts"
    assert spec.model_id == "derate/audio8-tts-preview-0.6b"
    assert spec.served_name == "Audio8-TTS-Preview-0.6b"
    assert spec.port == 8090


def test_an_sglang_serve_command_reads_its_own_flag_names():
    spec = parse_serve_command(SGLANG_COMMAND)
    assert spec is not None
    assert spec.runtime == "sglang"
    assert spec.model_id == "Qwen/Qwen3-8B"
    assert spec.tensor_parallel == 2
    assert spec.context_length == 40960
    assert spec.max_concurrent_seqs == 8
    assert spec.gpu_memory_utilization == 0.30
    assert spec.expert_parallel is True


def test_a_renamed_engine_core_argv_is_not_a_serve_command():
    # The exact failure mode this parser exists to refuse rather than guess
    # through: vLLM's forked engine process renames its own argv, and the
    # cmdline read off it carries none of the launch's flags.
    assert parse_serve_command("VLLM::EngineCore") is None


def test_an_unrelated_process_is_not_a_serve_command():
    assert parse_serve_command("nginx: master process /usr/sbin/nginx") is None


def test_a_serve_command_missing_a_routing_flag_refuses_rather_than_guesses():
    # No --served-model-name: routing has nothing to key a served name on.
    broken = VLLM_COMMAND.replace(
        "--served-model-name Qwen2.5-0.5B-Instruct ", ""
    )
    assert parse_serve_command(broken) is None


def test_empty_or_none_command_is_not_a_serve_command():
    assert parse_serve_command("") is None
    assert parse_serve_command(None) is None


# ==========================================================================
# cluster_id_from_container_name: sparkrun's own naming convention.
# ==========================================================================


def test_a_sparkrun_solo_container_name_yields_its_cluster_id():
    assert (
        cluster_id_from_container_name("sparkrun_648b629fb11e_solo")
        == "sparkrun_648b629fb11e"
    )


def test_an_unrelated_container_name_is_not_sparkruns():
    assert cluster_id_from_container_name("ollama-test") is None
    assert cluster_id_from_container_name("derate") is None
    assert cluster_id_from_container_name(None) is None
    assert cluster_id_from_container_name("") is None


# ==========================================================================
# DeploymentManager.adopt(): rehydration by construction, not by transition.
# ==========================================================================


def _manager(tmp_path) -> DeploymentManager:
    return DeploymentManager(state_dir=tmp_path, autostart=False)


def test_adopt_records_a_ready_deployment_marked_as_adopted(tmp_path):
    manager = _manager(tmp_path)
    deployment = manager.adopt(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-01"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Llama-3.3-70B",
        backend_url="http://192.168.0.71:8101/v1",
        context_length=32768,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_648b629fb11e",
        node_address="192.168.0.71",
        port=8101,
    )
    assert deployment is not None
    assert deployment.state is S.READY
    assert deployment.origin is DeploymentOrigin.ADOPTED
    assert deployment.backend_url == "http://192.168.0.71:8101/v1"
    assert manager.get(deployment.deployment_id) is deployment
    assert manager.handles()[deployment.deployment_id]["cluster_id"] == (
        "sparkrun_648b629fb11e"
    )


def test_adopt_persists_and_reconcile_rehydrates_it_after_a_restart(tmp_path):
    first = _manager(tmp_path)
    deployment = first.adopt(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-01"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Llama-3.3-70B",
        backend_url="http://192.168.0.71:8101/v1",
        context_length=32768,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_648b629fb11e",
        node_address="192.168.0.71",
        port=8101,
    )

    second = _manager(tmp_path)
    rehydrated = second.get(deployment.deployment_id)
    assert rehydrated is None  # reconcile() has not run yet
    second.reconcile()
    rehydrated = second.get(deployment.deployment_id)
    assert rehydrated is not None
    assert rehydrated.origin is DeploymentOrigin.ADOPTED


def test_adopting_the_same_cluster_id_twice_is_a_no_op(tmp_path):
    manager = _manager(tmp_path)
    kwargs = dict(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-01"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Llama-3.3-70B",
        backend_url="http://192.168.0.71:8101/v1",
        context_length=32768,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_648b629fb11e",
        node_address="192.168.0.71",
        port=8101,
    )
    first = manager.adopt(**kwargs)
    second = manager.adopt(**kwargs)
    assert first is not None
    assert second is None
    assert len(manager.list()) == 1


def test_adopt_emits_its_own_event_not_reconciled(tmp_path):
    manager = _manager(tmp_path)
    manager.adopt(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-01"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Llama-3.3-70B",
        backend_url="http://192.168.0.71:8101/v1",
        context_length=32768,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_648b629fb11e",
        node_address="192.168.0.71",
        port=8101,
    )
    events = [e["type"] for e in manager.bus.recent()]
    assert ev.ADOPTED in events
    assert ev.RECONCILED not in events


# ==========================================================================
# ContainerAutoAdopter._handles_by_node(): a port collision across two
# different nodes must not read as "already tracked" on either one.
#
# Confirmed live: spark-26af carries ~200 records from earlier test churn,
# several allocated the same port numbers spark-4d38's real orphaned
# containers use (each node's port counter starts from the same base
# independently). matches_deployment() matches on a bare port number with no
# idea which node it came from, so the dedup check has to filter by node
# itself -- this is the regression the collision above caused live.
# ==========================================================================


def test_handles_are_grouped_by_the_node_the_plan_actually_places_them_on(tmp_path):
    manager = _manager(tmp_path)
    manager.adopt(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-26af"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Qwen2.5-0.5B-Instruct",
        backend_url="http://192.168.0.172:8101/v1",
        context_length=32768,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_aaaaaaaaaaaa",
        node_address="192.168.0.172",
        port=8101,
    )
    manager.adopt(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-4d38"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Qwen3-4B-AWQ",
        backend_url="http://192.168.0.71:8102/v1",
        context_length=40960,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_bbbbbbbbbbbb",
        node_address="192.168.0.71",
        port=8102,
    )

    adopter = ContainerAutoAdopter(registry=None, resolver=None, fit=None, deployments=manager)
    by_node = adopter._handles_by_node()

    assert [h["port"] for h in by_node["spark-26af"]] == [8101]
    assert [h["port"] for h in by_node["spark-4d38"]] == [8102]


def test_a_port_shared_across_nodes_does_not_falsely_dedup_the_other_node(tmp_path):
    manager = _manager(tmp_path)
    # spark-26af already has a real, tracked deployment on port 8101.
    manager.adopt(
        shape=fx.LLAMA_3_3_70B,
        plan=fx.single_node_plan("spark-26af"),
        fit=fx.fits(),
        runtime="vllm",
        served_name="Qwen2.5-0.5B-Instruct",
        backend_url="http://192.168.0.172:8101/v1",
        context_length=32768,
        max_concurrent_seqs=1,
        cluster_id="sparkrun_aaaaaaaaaaaa",
        node_address="192.168.0.172",
        port=8101,
    )
    adopter = ContainerAutoAdopter(registry=None, resolver=None, fit=None, deployments=manager)
    by_node = adopter._handles_by_node()

    # spark-4d38's own orphan independently landed on the same port number --
    # ordinary, since each node allocates from the same base. Checked against
    # spark-4d38's OWN handles (empty), it is correctly not a duplicate.
    candidate_command = VLLM_COMMAND  # --port 8101, per the constant above
    assert not any(
        matches_deployment(candidate_command, h.get("cluster_id"), h.get("port"))
        for h in by_node.get("spark-4d38", [])
    )
    # The bug this guards against: checking the UNSCOPED handle set would
    # have matched spark-26af's record purely on the shared port number.
    assert any(
        matches_deployment(candidate_command, h.get("cluster_id"), h.get("port"))
        for h in manager.handles().values()
    )
