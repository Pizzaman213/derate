"""The runtime image is asked what it can load, and the answer wins.

Every test here is hermetic: `docker` is a shell script in tmp_path that
prints what the real one would. The point is not to verify vLLM's registry --
that changes weekly and is the whole reason this exists -- but to pin the
three rules that make asking safe, each of which a simpler version got wrong:
never pull, key the cache by image id rather than tag, and degrade to the
static table instead of raising.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from control_plane.resolver import imageprobe, support
from control_plane.resolver.imageprobe import ImageProbe

IMAGE = "ghcr.io/example/vllm:latest"


def _digest(ch: str) -> str:
    """A docker content digest: the only shape the probe accepts from a daemon."""
    return "sha256:" + ch * 64


ID_A = _digest("a")


@pytest.fixture(autouse=True)
def _no_probes_leak():
    """A probe is module state, so a test that records one must not leak it
    into the next: `support` would answer the rest of the suite from a fake
    registry."""
    support.clear_probes()
    yield
    support.clear_probes()


def _fake_docker(tmp_path, *, image_id=ID_A, architectures=("LlamaForCausalLM",),
                 version="0.28.0", inspect_rc=0, run_rc=0, removed=None, out_of_tree=None,
                 gpus_rc=0, quant_min_capability=None, kv_capabilities=None,
                 layers=None):
    """A `docker` that answers `image inspect` and `run`, and logs its calls.
    *gpus_rc* is what a `run --gpus all` exits with: a daemon with no NVIDIA
    runtime refuses the flag before the container starts."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    payload = json.dumps({
        "version": version,
        "architectures": list(architectures),
        "removed": removed or {},
        "out_of_tree": out_of_tree or {},
        "quant_min_capability": quant_min_capability or {},
        "kv_capabilities": kv_capabilities,
    })
    script = tmp_path / "docker"
    script.write_text(
        f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  [ {inspect_rc} -ne 0 ] && exit {inspect_rc}
  echo "{image_id}{'|' + json.dumps(layers).replace('"', chr(92) + '"') if layers else ''}"
  exit 0
fi
if [ "$1" = "run" ]; then
  case "$*" in *--gpus*) [ {gpus_rc} -ne 0 ] && exit {gpus_rc};; esac
  [ {run_rc} -ne 0 ] && exit {run_rc}
  echo "INFO 00:00:00 [importing.py:74] Triton is installed but 0 drivers found"
  echo 'derate-imageprobe:{payload}'
  exit 0
fi
exit 127
"""
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script), log


class TestProbe:
    def test_it_reads_the_registry_out_of_the_image(self, tmp_path):
        docker, _ = _fake_docker(
            tmp_path, architectures=("LlamaForCausalLM", "Gemma4ForConditionalGeneration")
        )
        found = imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert found is not None
        assert "Gemma4ForConditionalGeneration" in found.architectures
        assert found.version == "0.28.0"
        assert found.image == IMAGE

    def test_the_answer_survives_the_noise_the_image_prints_first(self, tmp_path):
        """vLLM logs Triton and CUDA lines to stdout on import, so the payload
        has to be found in the middle of the output rather than assumed to be
        all of it."""
        docker, _ = _fake_docker(tmp_path)
        found = imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert found is not None and found.architectures == frozenset({"LlamaForCausalLM"})

    def test_a_missing_image_is_never_pulled(self, tmp_path):
        """The whole reason `image inspect` runs first. `docker run` would
        fetch 24 GB inside a page load, on a coordinator that may not be the
        machine that serves models at all."""
        docker, log = _fake_docker(tmp_path, inspect_rc=1)
        assert imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None
        calls = log.read_text()
        assert "image inspect" in calls
        assert "run" not in calls.replace("image inspect", "")

    def test_no_docker_at_all_is_none_not_an_exception(self, tmp_path):
        """A coordinator without docker is a real deployment, not an error."""
        assert imageprobe.probe(
            "vllm", IMAGE, cache_dir=tmp_path / "c", docker=str(tmp_path / "absent")
        ) is None

    def test_a_runtime_with_no_script_is_left_alone(self, tmp_path):
        """`tts` is derate's own server, authored rather than read out of
        somebody else's image -- the only runtime with no probe script."""
        docker, log = _fake_docker(tmp_path)
        assert imageprobe.probe("tts", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None
        assert not log.exists()

    def test_sglang_is_probed_the_same_way_vllm_is(self, tmp_path):
        docker, _ = _fake_docker(
            tmp_path, architectures=("LlamaForCausalLM", "Gemma4ForConditionalGeneration")
        )
        found = imageprobe.probe("sglang", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert found is not None
        assert "Gemma4ForConditionalGeneration" in found.architectures

    def test_a_probe_answers_for_the_same_layers_under_another_daemons_id(self, tmp_path):
        """The amd64 image is ``sha256:db08...`` on the RTX box (containerd
        store) and ``sha256:1d61...`` on the coordinator after save/load
        (overlay2). Keyed by that id, a probe made on one machine could never
        answer for another, and a machine that has not launched yet could not
        be judged at all. Keyed by the layers, the coordinator's own copy
        answers from the cache -- without starting an image it cannot run."""
        layers = [_digest("1"), _digest("2")]
        cache = tmp_path / "c"
        on_box, _ = _fake_docker(tmp_path / "box", image_id=_digest("d"), layers=layers,
                                 architectures=("LlamaForCausalLM",))
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=on_box) is not None

        here, log = _fake_docker(tmp_path / "here", image_id=_digest("e"), layers=layers,
                                 run_rc=1)
        found = imageprobe.probe("vllm", "derate/vllm-audio:amd64", cache_dir=cache,
                                 docker=here, run=False)
        assert found is not None
        assert found.image == "derate/vllm-audio:amd64" and found.image_id == _digest("e")
        assert " run " not in f" {log.read_text()} "

        other, log = _fake_docker(tmp_path / "other", image_id=_digest("e"),
                                  layers=[_digest("1"), _digest("3")])
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=other, run=False) is None
        assert " run " not in f" {log.read_text()} "

    def test_a_container_that_fails_degrades(self, tmp_path):
        docker, _ = _fake_docker(tmp_path, run_rc=1)
        assert imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None

    def test_an_empty_registry_is_not_believed(self, tmp_path):
        """An image that answers with nothing is a broken probe, not a runtime
        that loads no models -- and believing it would refuse every launch."""
        docker, _ = _fake_docker(tmp_path, architectures=())
        assert imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None

    def test_it_reads_removed_and_out_of_tree_architectures(self, tmp_path):
        """These cost nothing beyond the import already paid for the servable
        set -- no per-class inspection -- and are what let a refusal name a
        plugin or a dropped version instead of a dead end."""
        docker, _ = _fake_docker(
            tmp_path,
            removed={"MllamaForConditionalGeneration": "v0.10.2"},
            out_of_tree={"BartForConditionalGeneration": "https://example.com/bart-plugin"},
        )
        found = imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert found.removed == {"MllamaForConditionalGeneration": "v0.10.2"}
        assert found.out_of_tree == {"BartForConditionalGeneration": "https://example.com/bart-plugin"}

    def test_empty_removed_and_out_of_tree_is_a_coherent_answer(self, tmp_path):
        """Unlike an empty `architectures` list, an image that has dropped or
        moved nothing is a perfectly good probe, not a broken one."""
        docker, _ = _fake_docker(tmp_path)
        found = imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert found is not None
        assert found.removed == {}
        assert found.out_of_tree == {}


class TestProbeCache:
    def test_the_second_ask_does_not_start_a_container(self, tmp_path):
        docker, log = _fake_docker(tmp_path)
        cache = tmp_path / "c"
        first = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker)
        runs = log.read_text().count("run --rm")
        second = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker)
        assert first == second
        assert log.read_text().count("run --rm") == runs == 1

    def test_a_moved_tag_re_probes(self, tmp_path):
        """`:latest` is the pinned tag and it moves. Keyed by tag, the cache
        would keep answering for last week's image -- which is precisely the
        staleness this module exists to end."""
        cache = tmp_path / "c"
        old, _ = _fake_docker(tmp_path / "a", image_id=_digest("1"),
                              architectures=("LlamaForCausalLM",))
        new, _ = _fake_docker(tmp_path / "b", image_id=_digest("2"),
                              architectures=("LlamaForCausalLM", "Gemma4ForConditionalGeneration"))
        before = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=old)
        after = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=new)
        assert "Gemma4ForConditionalGeneration" not in before.architectures
        assert "Gemma4ForConditionalGeneration" in after.architectures

    def test_an_unwritable_cache_costs_a_container_start_not_the_answer(self, tmp_path):
        docker, _ = _fake_docker(tmp_path)
        blocked = tmp_path / "blocked"
        blocked.write_text("not a directory")
        assert imageprobe.probe("vllm", IMAGE, cache_dir=blocked, docker=docker) is not None

    def test_a_cache_entry_missing_removed_and_out_of_tree_re_probes(self, tmp_path):
        """A cache written before this field existed would otherwise be served
        forever as "vLLM never dropped or moved anything" -- false for every
        build there has ever been. `from_dict` requires the keys, so a stale
        entry is a `KeyError` -> a miss in `_read_cache` -> a re-probe."""
        cache = tmp_path / "c"
        cache.mkdir()
        stale = {
            "runtime": "vllm", "image": IMAGE, "image_id": ID_A,
            "version": "0.27.0", "architectures": ["LlamaForCausalLM"],
            "speculators": [],
            # `removed` / `out_of_tree` deliberately absent.
        }
        (cache / f"vllm-{ID_A.replace(':', '-')}.json").write_text(json.dumps(stale))
        docker, log = _fake_docker(
            tmp_path, image_id=ID_A,
            removed={"MllamaForConditionalGeneration": "v0.10.2"},
        )
        found = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker)
        assert "run --rm" in log.read_text()
        assert found.removed == {"MllamaForConditionalGeneration": "v0.10.2"}

    def test_a_cache_entry_missing_the_silicon_facts_re_probes(self, tmp_path):
        """Served from cache, an entry written before these fields existed
        would read as "this image states no minimum capability for anything"
        -- and the GPU gate would fall back to the static table on an image
        that had already answered."""
        cache = tmp_path / "c"
        cache.mkdir()
        stale = {
            "runtime": "vllm", "image": IMAGE, "image_id": ID_A,
            "version": "0.28.0", "architectures": ["LlamaForCausalLM"],
            "speculators": [], "removed": {}, "out_of_tree": {},
            # `quant_min_capability` / `kv_capabilities` deliberately absent.
        }
        (cache / f"vllm-{ID_A.replace(':', '-')}.json").write_text(json.dumps(stale))
        docker, log = _fake_docker(
            tmp_path, image_id=ID_A, quant_min_capability={"fp_quant": 100}
        )
        found = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker)
        assert "run --rm" in log.read_text()
        assert found.quant_min_capability == {"fp_quant": 100}


def _daemons(tmp_path, local: str, remotes: dict[str, str]) -> str:
    """A `docker` that is the coordinator's own daemon without `-H` and the
    named host's with it -- each one a `_fake_docker` script."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cases = "".join(f'  {host}) shift 2; exec {path} "$@";;\n' for host, path in remotes.items())
    script = tmp_path / "docker"
    script.write_text(
        f'#!/bin/sh\nif [ "$1" = "-H" ]; then\n case "$2" in\n{cases}  *) exit 1;;\n'
        f' esac\nfi\nexec {local} "$@"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


class TestUntrustedDaemons:
    """An image id and its layers come from a daemon that may be another
    machine's -- one a compromised node controls."""

    @pytest.mark.parametrize("image_id, layers", [
        ("sha256:x/../../escaped", None),
        (ID_A, ["sha256:x/../../escaped"]),
        ("../escaped", None),
    ])
    def test_a_crafted_identity_writes_nothing_and_is_not_probed(
        self, tmp_path, image_id, layers
    ):
        """Keyed by the raw id, `sha256:x/../../escaped` made the cache write
        land outside the cache folder -- `mkdir(parents=True)` + `os.replace`
        overwriting whatever the daemon named."""
        docker, log = _fake_docker(tmp_path / "bin", image_id=image_id, layers=layers)
        cache = tmp_path / "data" / "runtimes"
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker) is None
        assert "run --rm" not in log.read_text()
        assert list((tmp_path / "data").rglob("*")) in ([], [cache])
        assert not list(tmp_path.rglob("escaped*"))

    def test_each_guard_refuses_a_crafted_value_on_its_own(self, tmp_path):
        """The identity check and the path check each stop the escape above,
        so either could be deleted with that test still green. Each is held
        here alone: no identity from a non-digest id, and no cache path for a
        key `_identity` would never produce or for a runtime name that is not
        a plain identifier."""
        docker, _ = _fake_docker(tmp_path / "bin", image_id="sha256:x/../../escaped")
        assert imageprobe._identity(IMAGE, docker=docker, timeout=5.0, host=None) is None
        cache = tmp_path / "c"
        good = "layers-" + "a" * 64
        assert imageprobe._cache_path(cache, "vllm", good) == cache / f"vllm-{good}.json"
        # Shapes that would stay inside the folder, so only the key and name
        # checks can refuse them: they are not what `_identity` produces.
        assert imageprobe._cache_path(cache, "vllm", "sha256-" + "z" * 64) is None
        assert imageprobe._cache_path(cache, "vllm", "anything") is None
        assert imageprobe._cache_path(cache, "vllm.x", good) is None
        # And one that would leave it.
        assert imageprobe._cache_path(cache, "vllm", "sha256-x/../../escaped") is None

    def test_an_unconfirmed_remote_answer_speaks_only_for_its_host(self, tmp_path):
        """The coordinator holds no copy of the image, so nothing confirms what
        host A's daemon said. It must not become what host B, or the
        coordinator's cache-only lookup, is judged by."""
        layers = [_digest("1"), _digest("2")]
        liar, _ = _fake_docker(tmp_path / "a", image_id=_digest("b"), layers=layers,
                               architectures=("PoisonForCausalLM",))
        honest, b_log = _fake_docker(tmp_path / "b", image_id=_digest("b"), layers=layers,
                                     run_rc=1)
        absent, _ = _fake_docker(tmp_path / "coord", inspect_rc=1)
        docker = _daemons(tmp_path / "bin", absent, {"ssh://a": liar, "ssh://b": honest})
        cache = tmp_path / "c"

        found = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker, host="ssh://a")
        assert found is not None and "PoisonForCausalLM" in found.architectures
        # Host A's own re-ask is answered from its own entry.
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker,
                                host="ssh://a", run=False) is not None
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker, run=False) is None
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker,
                                host="ssh://b") is None
        assert "run --rm" in b_log.read_text()  # B was really asked, not answered for
        entries = [json.loads(p.read_text()) for p in cache.glob("*.json")]
        assert [e["source"] for e in entries] == ["ssh://a, unconfirmed: that host only"]

    def test_a_remote_answer_the_coordinators_copy_confirms_is_shared(self, tmp_path):
        """The case that must keep working: the coordinator holds the amd64
        image (so sparkrun can ship it) and cannot run it; a machine of that
        architecture answers, and the answer speaks for every such machine."""
        layers = [_digest("1"), _digest("2")]
        remote, _ = _fake_docker(tmp_path / "a", image_id=_digest("d"), layers=layers)
        mine, _ = _fake_docker(tmp_path / "coord", image_id=_digest("e"), layers=layers,
                               run_rc=1)
        docker = _daemons(tmp_path / "bin", mine, {"ssh://a": remote})
        cache = tmp_path / "c"

        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker,
                                host="ssh://a") is not None
        found = imageprobe.probe("vllm", IMAGE, cache_dir=cache, docker=docker, run=False)
        assert found is not None and found.image_id == _digest("e")
        entries = [json.loads(p.read_text()) for p in cache.glob("*.json")]
        assert [e["source"] for e in entries] == [
            "ssh://a, confirmed by the coordinator's copy"
        ]

    @pytest.mark.parametrize("coordinator_holds_it", [False, True])
    def test_the_resolver_adopts_only_a_confirmed_answer(
        self, tmp_path, monkeypatch, coordinator_holds_it
    ):
        """`support` keys a probe by image, so whatever the arch loop adopts
        judges every machine of that architecture. An answer only its own
        host vouches for is not adopted; a confirmed one is."""
        from control_plane.resolver.resolver import ModelResolver

        layers = [_digest("1"), _digest("2")]
        remote, _ = _fake_docker(tmp_path / "a", image_id=_digest("d"), layers=layers)
        mine, _ = (
            _fake_docker(tmp_path / "coord", image_id=_digest("e"), layers=layers, run_rc=1)
            if coordinator_holds_it else _fake_docker(tmp_path / "coord", inspect_rc=1)
        )
        _daemons(tmp_path / "bin", mine, {"ssh://a": remote})
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        resolver = ModelResolver(
            arch_images={("vllm", "amd64"): IMAGE}, arch_hosts=lambda arch: ["ssh://a"]
        )
        found, where = resolver._probe_arch_image("vllm", "amd64", IMAGE, tmp_path / "c")
        assert (found is not None) is coordinator_holds_it
        assert where == ("ssh://a" if coordinator_holds_it else None)

    def test_the_coordinators_cached_answer_needs_no_machine_of_that_arch(
        self, tmp_path, monkeypatch
    ):
        """Once an amd64 machine has answered and the coordinator's copy
        confirmed it, the coordinator's cache answers for that image even when
        the only amd64 machine up now has never pulled it."""
        from control_plane.resolver.resolver import ModelResolver

        layers = [_digest("1"), _digest("2")]
        remote, _ = _fake_docker(tmp_path / "a", image_id=_digest("d"), layers=layers)
        fresh, fresh_log = _fake_docker(tmp_path / "b", inspect_rc=1)
        mine, _ = _fake_docker(tmp_path / "coord", image_id=_digest("e"), layers=layers,
                               run_rc=1)
        _daemons(tmp_path / "bin", mine, {"ssh://a": remote, "ssh://b": fresh})
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        cache = tmp_path / "c"
        assert imageprobe.probe("vllm", IMAGE, cache_dir=cache, host="ssh://a") is not None

        resolver = ModelResolver(
            arch_images={("vllm", "amd64"): IMAGE}, arch_hosts=lambda arch: ["ssh://b"]
        )
        found, where = resolver._probe_arch_image("vllm", "amd64", IMAGE, cache)
        assert found is not None and found.image == IMAGE
        assert where == "the coordinator's cache"
        assert not fresh_log.exists() or "run" not in fresh_log.read_text()

    def test_a_pass_adopts_each_architectures_image_for_the_gate(self, tmp_path, monkeypatch):
        """What the arch loop finds is what `evaluate_runtime(image=)` judges an
        amd64 machine by. Found and not recorded, the 3090 would be judged by
        the static table forever."""
        from control_plane.resolver.cache import ShapeCache
        from control_plane.resolver.resolver import ModelResolver

        layers = [_digest("1"), _digest("2")]
        remote, _ = _fake_docker(tmp_path / "a", image_id=_digest("d"), layers=layers,
                                 architectures=("OnlyOnAmd64ForCausalLM",), version="0.31.0")
        mine, _ = _fake_docker(tmp_path / "coord", image_id=_digest("e"), layers=layers,
                               run_rc=1)
        _daemons(tmp_path / "bin", mine, {"ssh://a": remote})
        monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
        resolver = ModelResolver(
            cache=ShapeCache(directory=tmp_path / "data" / "cache"),
            arch_images={("vllm", "amd64"): IMAGE},
            arch_hosts=lambda arch: ["ssh://a"] if arch == "amd64" else [],
        )
        assert support.probed_image("vllm", IMAGE) is None

        resolver._probe_arch_images_once()

        adopted = support.probed_image("vllm", IMAGE)
        assert adopted is not None and adopted.version == "0.31.0"
        verdict = support.evaluate_runtime(
            "vllm", ("OnlyOnAmd64ForCausalLM",), "bf16", image=IMAGE
        )
        assert verdict.ok, verdict.reason


class TestTheSiliconFacts:
    """The probe asks vLLM for its quantization floors and the KV widths its
    attention selector has a kernel for, and needs a GPU driver to ask the
    second -- which a GPU-less coordinator does not have."""

    def test_it_asks_with_the_gpu_driver_mounted(self, tmp_path):
        docker, log = _fake_docker(
            tmp_path,
            quant_min_capability={"fp8": 75, "fp_quant": 100},
            kv_capabilities={"auto": ["8.6", "12.1"], "fp8": ["8.6", "12.1"], "nvfp4": []},
        )
        found = imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert "--gpus all" in log.read_text()
        assert found.quant_min_capability == {"fp8": 75, "fp_quant": 100}
        assert found.kv_capabilities["nvfp4"] == ()
        assert found.kv_asked() == frozenset({"8.6", "12.1"})

    def test_a_daemon_that_refuses_the_gpu_still_gets_the_architectures(self, tmp_path):
        """Refused `--gpus all` used to be the whole probe failing: no
        architecture list on a coordinator with no NVIDIA runtime. It is
        retried without, and the KV half reads as unknown, never as none."""
        docker, log = _fake_docker(tmp_path, gpus_rc=125)
        found = imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker)
        assert found is not None
        assert found.architectures == frozenset({"LlamaForCausalLM"})
        assert found.kv_capabilities is None
        runs = [line for line in log.read_text().splitlines() if line.startswith("run")]
        assert len(runs) == 2 and "--gpus" in runs[0] and "--gpus" not in runs[1]


class TestImageProbeRoundTrip:
    def test_as_dict_from_dict_round_trips_removed_and_out_of_tree(self):
        original = ImageProbe(
            runtime="vllm", image=IMAGE, image_id="sha256:aaa", version="0.28.0",
            architectures=frozenset({"LlamaForCausalLM"}),
            speculators=frozenset({"Gemma4MTPModel"}),
            removed={"MllamaForConditionalGeneration": "v0.10.2"},
            out_of_tree={"BartForConditionalGeneration": "https://example.com/bart-plugin"},
            quant_min_capability={"fp_quant": 100},
            kv_capabilities={"auto": ("8.6",), "nvfp4": ()},
        )
        restored = ImageProbe.from_dict(original.as_dict())
        assert restored == original


class TestTheProbeWins:
    def _probe(self, *architectures, removed=None, out_of_tree=None):
        return ImageProbe(
            runtime="vllm", image=IMAGE, image_id="sha256:aaa",
            version="0.28.0", architectures=frozenset(architectures),
            removed=removed or {}, out_of_tree=out_of_tree or {},
        )

    def test_an_architecture_the_static_table_never_heard_of_is_supported(self):
        """The bug this whole module exists for: the image could load it, the
        list could not name it, and the launch was refused with a 400."""
        arch = "Gemma4ForConditionalGeneration"
        support.clear_probes()
        support.record_probe(self._probe("LlamaForCausalLM", arch))
        assert support.build_verdict((arch,), "bf16").for_runtime("vllm").ok

    def test_a_probed_sglang_image_also_beats_its_static_table(self):
        """The motivating case: `Gemma4ForConditionalGeneration` is missing
        from the hand-kept `SGLANG_ARCHITECTURES`, and a real SGLang image
        registers it. The probe has to win here exactly as it does for vllm."""
        arch = "Gemma4ForConditionalGeneration"
        assert arch not in support.SGLANG_ARCHITECTURES
        support.record_probe(ImageProbe(
            runtime="sglang", image=IMAGE, image_id="sha256:bbb",
            version="0.5.12", architectures=frozenset({"LlamaForCausalLM", arch}),
        ))
        assert support.build_verdict((arch,), "bf16").for_runtime("sglang").ok

    def test_an_architecture_the_image_dropped_stops_being_supported(self):
        """The other direction, and the reason this cannot be additive-only:
        vLLM removes models. `MllamaForConditionalGeneration` -- Llama 3.2
        Vision -- was on the static list after the image could no longer load
        it, which is a launch that clears every gate and dies at load."""
        support.record_probe(self._probe("LlamaForCausalLM"))
        entry = support.build_verdict(("MllamaForConditionalGeneration",), "bf16")
        assert not entry.for_runtime("vllm").ok

    def test_a_refusal_cites_the_image_it_read(self):
        """"Not in vllm's list" is unfalsifiable from the outside. Naming the
        image and its version makes the claim checkable."""
        support.record_probe(self._probe("LlamaForCausalLM"))
        reason = support.build_verdict(("NoSuchForCausalLM",), "bf16").for_runtime("vllm").reason
        assert IMAGE in reason and "0.28.0" in reason

    def test_without_a_probe_the_static_table_still_answers(self):
        """The fallback is not a degraded mode to apologise for -- it is what
        every machine without a docker socket runs on."""
        assert support.build_verdict(("LlamaForCausalLM",), "bf16").for_runtime("vllm").ok

    def test_runtimes_serving_follows_the_probe(self):
        """`_elsewhere` sends a refused reader to another runtime by name. If
        it read the static table while the verdict read the probe, it could
        name a runtime that no longer loads the thing."""
        support.record_probe(self._probe("OnlyHereForCausalLM"))
        assert support.runtimes_serving(("OnlyHereForCausalLM",)) == ["vllm"]
        assert "vllm" not in support.runtimes_serving(("LlamaForCausalLM",))

    def test_a_registered_but_known_broken_architecture_is_not_offered_elsewhere(self):
        """Registration alone is not "loads it" -- `DiffusionGemmaForBlockDiffusion`
        is in vLLM's own registry and still refused, via `VLLM_KNOWN_BROKEN`,
        for a documented CUDA graph capture crash. A reader refused on sglang
        or tts must not be told "the vllm runtime loads it" when vllm's own
        verdict for the same architecture is also UNSUPPORTED."""
        support.record_probe(self._probe(
            "LlamaForCausalLM", "DiffusionGemmaForBlockDiffusion",
        ))
        assert support.runtimes_serving(("DiffusionGemmaForBlockDiffusion",)) == []
        reason = support.build_verdict(
            ("DiffusionGemmaForBlockDiffusion",), "bf16"
        ).for_runtime("sglang").reason
        assert "vllm" not in reason

    def test_architectures_for_is_the_one_lookup(self):
        support.record_probe(self._probe("OnlyHereForCausalLM"))
        assert support.architectures_for("vllm") == frozenset({"OnlyHereForCausalLM"})
        # Untouched runtimes keep their own tables.
        assert "LlamaForCausalLM" in support.architectures_for("sglang")
        assert support.architectures_for("nonsense") == frozenset()

    def test_a_refusal_names_the_out_of_tree_plugin(self):
        """"Not in the registry" is true and useless when the architecture
        moved to a plugin vLLM itself names. Say where it went."""
        support.record_probe(self._probe(
            "LlamaForCausalLM",
            out_of_tree={"BartForConditionalGeneration": "https://example.com/bart-plugin"},
        ))
        reason = support.build_verdict(
            ("BartForConditionalGeneration",), "bf16"
        ).for_runtime("vllm").reason
        assert "https://example.com/bart-plugin" in reason
        assert not support.build_verdict(
            ("BartForConditionalGeneration",), "bf16"
        ).for_runtime("vllm").ok

    def test_a_refusal_names_the_version_that_dropped_it(self):
        """Distinct from "never supported": the operator's next move is an
        older image, not a different checkpoint."""
        support.record_probe(self._probe(
            "LlamaForCausalLM",
            removed={"MllamaForConditionalGeneration": "v0.10.2"},
        ))
        reason = support.build_verdict(
            ("MllamaForConditionalGeneration",), "bf16"
        ).for_runtime("vllm").reason
        assert "v0.10.2" in reason

    def test_an_architecture_absent_from_both_new_dicts_keeps_the_old_wording(self):
        support.record_probe(self._probe("LlamaForCausalLM"))
        reason = support.build_verdict(
            ("NoSuchForCausalLM",), "bf16"
        ).for_runtime("vllm").reason
        assert "is not in the model registry of" in reason


class TestResolverStart:
    def test_start_returns_before_the_probe_does(self):
        """The gateway's resolver step is bounded by `startup_step_timeout_s`
        -- five seconds, against a container start of fifteen to thirty. A
        blocking probe would be recorded as a degraded startup and its answer
        thrown away."""
        from control_plane.resolver.resolver import ModelResolver

        resolver = ModelResolver(runtime_images={"vllm": "no-such-image:ever"})
        assert resolver.start() is None
        assert support.probed("vllm") is None

    def test_no_images_means_no_thread_at_all(self):
        from control_plane.resolver.resolver import ModelResolver

        assert ModelResolver().start() is None


class TestSglangClassificationHeuristic:
    """Unlike vLLM's, sglang's classification heuristic (MRO-ancestor-name
    markers, because sglang's registry is one flat dict with no category
    sets to subtract by) lives entirely inside a string that only runs in
    the real container -- the one piece of sglang logic nothing else here
    tests hermetically. This execs the literal `_SGLANG_SCRIPT` against a
    fake `sglang` package carrying the real tricky cases this session's
    investigation of the real image turned up: `MistralModel`'s hidden
    pooling ancestry (a real false negative -- its own name says nothing
    about pooling, only `LlamaEmbeddingModel`'s does) and
    `EmbeddingAccessMixin` sitting on plainly generative classes too (a real
    false positive trap it is not).
    """

    def _run_script(self, models: dict, version: str = "0.5.12") -> dict:
        import contextlib
        import io
        import sys
        import types

        sglang_mod = types.ModuleType("sglang")
        sglang_mod.__version__ = version
        srt_mod = types.ModuleType("sglang.srt")
        models_pkg = types.ModuleType("sglang.srt.models")
        registry_mod = types.ModuleType("sglang.srt.models.registry")

        class _FakeModelRegistry:
            pass

        fake_registry = _FakeModelRegistry()
        fake_registry.models = models
        registry_mod.ModelRegistry = fake_registry
        # Mirrors what Python's own import machinery sets up for a real
        # package, so `from sglang.srt.models.registry import ModelRegistry`
        # has no way to tell this isn't one.
        sglang_mod.srt = srt_mod
        srt_mod.models = models_pkg
        models_pkg.registry = registry_mod

        names = (
            "sglang", "sglang.srt", "sglang.srt.models", "sglang.srt.models.registry",
        )
        saved = {name: sys.modules.get(name) for name in names}
        sys.modules.update({
            "sglang": sglang_mod, "sglang.srt": srt_mod,
            "sglang.srt.models": models_pkg, "sglang.srt.models.registry": registry_mod,
        })
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exec(compile(imageprobe._SGLANG_SCRIPT, "<sglang_script>", "exec"), {})
            out = buf.getvalue()
        finally:
            for name, mod in saved.items():
                if mod is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = mod

        line = next(l for l in out.splitlines() if l.startswith("derate-imageprobe:"))
        return json.loads(line[len("derate-imageprobe:"):])

    def test_classifies_every_tricky_case_correctly(self):
        class LlamaEmbeddingModel:
            pass

        class MistralModel(LlamaEmbeddingModel):
            """The real false negative this session's investigation found:
            its own name says nothing about pooling."""

        class FooForCausalLM:
            pass

        class BarForCausalLMEagle3:
            pass

        class TransformersForCausalLM:
            pass

        class EmbeddingAccessMixin:
            pass

        class Gemma3ForCausalLM(EmbeddingAccessMixin):
            """The real false-positive trap: carries the mixin and is
            plainly generative -- must not be excluded by it."""

        payload = self._run_script({
            "FooForCausalLM": FooForCausalLM,
            "LlamaEmbeddingModel": LlamaEmbeddingModel,
            "MistralModel": MistralModel,
            "BarForCausalLMEagle3": BarForCausalLMEagle3,
            "TransformersForCausalLM": TransformersForCausalLM,
            "Gemma3ForCausalLM": Gemma3ForCausalLM,
        })

        servable = set(payload["architectures"])
        assert servable == {"FooForCausalLM", "Gemma3ForCausalLM"}

    def test_a_non_class_registry_entry_is_skipped_not_crashed_on(self):
        """`ModelRegistry.models`'s real type hint allows a lazy string
        reference as a value, not only a class -- `isinstance(cls, type)`
        in the script is what that guards against."""
        class FooForCausalLM:
            pass

        payload = self._run_script({
            "FooForCausalLM": FooForCausalLM,
            "SomeLazyRef": "sglang.srt.models.lazy:SomeLazyRef",
        })
        assert payload["architectures"] == ["FooForCausalLM"]

    def test_the_scripts_own_version_string_reaches_the_payload(self):
        class FooForCausalLM:
            pass

        payload = self._run_script({"FooForCausalLM": FooForCausalLM}, version="0.6.0")
        assert payload["version"] == "0.6.0"
