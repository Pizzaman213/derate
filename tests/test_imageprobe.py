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
import stat

import pytest

from control_plane.resolver import imageprobe, support
from control_plane.resolver.imageprobe import ImageProbe

IMAGE = "ghcr.io/example/vllm:latest"


@pytest.fixture(autouse=True)
def _no_probes_leak():
    """A probe is module state, so a test that records one must not leak it
    into the next: `support` would answer the rest of the suite from a fake
    registry."""
    support.clear_probes()
    yield
    support.clear_probes()


def _fake_docker(tmp_path, *, image_id="sha256:aaa", architectures=("LlamaForCausalLM",),
                 version="0.28.0", inspect_rc=0, run_rc=0):
    """A `docker` that answers `image inspect` and `run`, and logs its calls."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    log = tmp_path / "calls.log"
    payload = json.dumps({"version": version, "architectures": list(architectures)})
    script = tmp_path / "docker"
    script.write_text(
        f"""#!/bin/sh
echo "$@" >> {log}
if [ "$1" = "image" ] && [ "$2" = "inspect" ]; then
  [ {inspect_rc} -ne 0 ] && exit {inspect_rc}
  echo "{image_id}"
  exit 0
fi
if [ "$1" = "run" ]; then
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
        docker, log = _fake_docker(tmp_path)
        assert imageprobe.probe("sglang", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None
        assert not log.exists()

    def test_a_container_that_fails_degrades(self, tmp_path):
        docker, _ = _fake_docker(tmp_path, run_rc=1)
        assert imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None

    def test_an_empty_registry_is_not_believed(self, tmp_path):
        """An image that answers with nothing is a broken probe, not a runtime
        that loads no models -- and believing it would refuse every launch."""
        docker, _ = _fake_docker(tmp_path, architectures=())
        assert imageprobe.probe("vllm", IMAGE, cache_dir=tmp_path / "c", docker=docker) is None


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
        old, _ = _fake_docker(tmp_path / "a", image_id="sha256:111",
                              architectures=("LlamaForCausalLM",))
        new, _ = _fake_docker(tmp_path / "b", image_id="sha256:222",
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


class TestTheProbeWins:
    def _probe(self, *architectures):
        return ImageProbe(
            runtime="vllm", image=IMAGE, image_id="sha256:aaa",
            version="0.28.0", architectures=frozenset(architectures),
        )

    def test_an_architecture_the_static_table_never_heard_of_is_supported(self):
        """The bug this whole module exists for: the image could load it, the
        list could not name it, and the launch was refused with a 400."""
        arch = "Gemma4ForConditionalGeneration"
        support.clear_probes()
        support.record_probe(self._probe("LlamaForCausalLM", arch))
        assert support.build_verdict((arch,), "bf16").for_runtime("vllm").ok

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

    def test_architectures_for_is_the_one_lookup(self):
        support.record_probe(self._probe("OnlyHereForCausalLM"))
        assert support.architectures_for("vllm") == frozenset({"OnlyHereForCausalLM"})
        # Untouched runtimes keep their own tables.
        assert "LlamaForCausalLM" in support.architectures_for("sglang")
        assert support.architectures_for("nonsense") == frozenset()


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
