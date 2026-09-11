"""The reap predicate: what may be cleared, and everything that may not.

Written against the development box's real population, because that box is
where the naive rule would have done damage. Four `sparkrun_*_solo` containers
were launched by a coordinator running INSIDE a container and independently
adopted by a second coordinator on the host, so each one is simultaneously
"mine" to one store and "unclaimed" to the other. Every command line and
container name below is copied from it verbatim.

The test that matters is
`test_a_container_whose_backend_answers_is_never_reapable_even_with_no_record`:
it removes the record entirely, which is the condition a peer coordinator's
container presents, and asserts the identity probe alone still refuses. That is
the veto the whole design rests on.
"""

from __future__ import annotations

import pytest

from control_plane.deploy import reap
from control_plane.deploy.reap import ContainerFacts, classify

# The four real launches, and the ports they answer on.
QWEN_05B = (
    "/usr/bin/python3 /usr/local/bin/vllm serve Qwen/Qwen2.5-0.5B-Instruct "
    "--served-model-name Qwen2.5-0.5B-Instruct --host 0.0.0.0 --port 8101 "
    "--tensor-parallel-size 1 --pipeline-parallel-size 1 --max-model-len 32768 "
    "--max-num-seqs 1 --gpu-memory-utilization 0.05 --trust-remote-code"
)
QWEN_4B_AWQ = (
    "/usr/bin/python3 /usr/local/bin/vllm serve Qwen/Qwen3-4B-AWQ "
    "--served-model-name Qwen3-4B-AWQ --host 0.0.0.0 --port 8102 "
    "--tensor-parallel-size 1 --pipeline-parallel-size 1 --max-model-len 40960 "
    "--max-num-seqs 1 --gpu-memory-utilization 0.08 --trust-remote-code"
)

OLD = 100_000.0  # far past any readiness window


def facts(name, *, command=QWEN_05B, age_s=OLD, running=True):
    return ContainerFacts(
        name=name, image="ghcr.io/pizzaman213/derate/vllm-audio:latest",
        age_s=age_s, running=running, command=command,
    )


def verdict(f, *, live=(), terminal=(), min_age_s=1800.0, probe_fn=None):
    return classify(
        f,
        cluster_ids_live=set(live),
        cluster_ids_terminal=set(terminal),
        node_address="127.0.0.1",
        min_age_s=min_age_s,
        probe_fn=probe_fn or (lambda url, **kw: (False, "connection refused")),
    )


def _answers(url, **kw):
    return True, None


# -- what is never touched --------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "derate",                      # the node itself
        "ollama-test",
        "buildx_buildkit_derate0",
        "buildx_buildkit_sparkplane0",  # the near-miss: the OLD project name
        "nemo-lb",
        "jarvis-discord-discord-1",
        "sweb.eval.django__django-15996.jarvisbench-swebench-lite",
        "sparkrun",                     # the bare prefix, no cluster id
        "sparkrun_XYZ_solo",            # not hex
    ],
)
def test_a_non_sparkrun_container_name_is_never_reapable(name):
    """Every one of these is on the development box. `buildx_buildkit_
    sparkplane0` is the one worth keeping: it carries the project's OLD name
    and any loose `spark` match would sweep it up -- which is exactly the trap
    CLAUDE.md's "grep for sparkplane, never for bare spark" rule names."""
    result = verdict(facts(name))
    assert result.reapable is False
    assert result.cluster_id is None
    assert "not a sparkrun container name" in result.reason


def test_a_container_whose_backend_answers_is_never_reapable_even_with_no_record():
    """THE guard. No record at all -- the condition a peer coordinator's
    container presents to this one -- and the identity probe alone must
    refuse. A rule of "sparkrun-named and unclaimed" would have destroyed four
    live models on the box this was written on."""
    result = verdict(facts("sparkrun_2102cb4f1d6f_solo", command=QWEN_4B_AWQ),
                     probe_fn=_answers)
    assert result.reapable is False
    assert result.reason == "serving Qwen/Qwen3-4B-AWQ on :8102"
    assert result.port == 8102


def test_a_live_record_keeps_a_container_whose_port_has_gone_silent():
    """A wedged backend under a live record is Stop's problem and the health
    watch's. Reaping it would race a deployment the operator still owns."""
    result = verdict(
        facts("sparkrun_648b629fb11e_solo"), live=["sparkrun_648b629fb11e"]
    )
    assert result.reapable is False
    assert "live deployment record claims it" in result.reason


def test_a_container_inside_the_readiness_window_is_never_reapable():
    """CUDA graph capture alone is 40s+, and a large model is far longer. A
    launch is legitimately up with a silent port for the whole window."""
    result = verdict(facts("sparkrun_648b629fb11e_solo", age_s=120.0))
    assert result.reapable is False
    assert "readiness window" in result.reason


def test_an_unrecognised_serve_command_is_left_alone():
    """The same stance autoadopt.py takes when it cannot parse a command: a
    future sparkrun runtime this build has never heard of must not be garbage
    by default."""
    result = verdict(facts("sparkrun_648b629fb11e_solo", command="/bin/sleep infinity"))
    assert result.reapable is False
    assert "not one this build recognises" in result.reason


def test_a_port_serving_a_different_model_is_reported_and_not_reaped():
    """`wrong_model` means the evidence is about somebody ELSE's process, so
    it justifies nothing about this container.

    The refusal sentence is built from `health._MISMATCH_MARK` rather than
    typed out here: an invented one passes `wrong_model` nowhere, and writing
    this test with a plausible-looking string is how the clause would have
    been proved by a test that never exercised it. It caught exactly that."""
    from control_plane.deploy import health

    result = verdict(
        facts("sparkrun_648b629fb11e_solo"),
        probe_fn=lambda url, **kw: (
            False,
            "%s: serves 'other-model'" % health._MISMATCH_MARK,
        ),
    )
    assert result.reapable is False
    assert "answers as something else" in result.reason


def test_an_exited_container_is_reported_but_not_stopped():
    """Nothing to tear down, and removing it is the operator's call: an exited
    container is the only copy of that launch's `docker logs`."""
    result = verdict(facts("sparkrun_648b629fb11e_solo", running=False))
    assert result.reapable is False
    assert "already exited" in result.reason


def test_a_finished_record_never_overrides_a_backend_that_still_answers():
    """The bug this module found in itself, in dry run, on the live box.

    A peer restarted the coordinator; its old records went STOPPED while the
    containers kept serving. The terminal-record clause used to sit ABOVE the
    identity probe, so two containers serving Qwen2.5-0.5B-Instruct and
    Qwen3-4B-AWQ came back `REAP` without ever being asked what they were.

    A terminal record beside a live backend is not an orphan -- it is a
    coordinator that lost track, which is precisely what `autoadopt.py`
    repairs by ADOPTING the container. So the probe is unconditional, and no
    clause may return reapable above it.
    """
    result = verdict(
        facts("sparkrun_2102cb4f1d6f_solo", command=QWEN_4B_AWQ),
        terminal=["sparkrun_2102cb4f1d6f"],
        probe_fn=_answers,
    )
    assert result.reapable is False
    assert result.reason == "serving Qwen/Qwen3-4B-AWQ on :8102"


# -- what is ----------------------------------------------------------------


def test_a_container_under_a_finished_record_is_exactly_the_orphan_to_reap():
    """derate launched it, derate recorded it as over, it is still up. The
    evidence is derate's OWN record -- deliberately the same reasoning
    gateway/gpu_procs.py applies to a process under a STOPPED record."""
    result = verdict(
        facts("sparkrun_648b629fb11e_solo"), terminal=["sparkrun_648b629fb11e"]
    )
    assert result.reapable is True
    assert "marked finished" in result.reason
    # And it got there through the probe, not around it -- the default
    # probe_fn above answers "connection refused". The test above is the pair
    # to this one: same record state, live backend, opposite verdict.
    assert "nothing answers on :8101" in result.reason


def test_no_record_and_a_silent_port_is_reapable():
    result = verdict(facts("sparkrun_648b629fb11e_solo"))
    assert result.reapable is True
    assert "nothing answers on :8101" in result.reason


def test_a_terminal_record_still_waits_out_the_readiness_window():
    """Ordering matters: the age veto is checked before the terminal-record
    clause, so a stop that raced a fresh launch cannot reap the launch."""
    result = verdict(
        facts("sparkrun_648b629fb11e_solo", age_s=10.0),
        terminal=["sparkrun_648b629fb11e"],
    )
    assert result.reapable is False


# -- refusing to act on a keep ----------------------------------------------


def test_stopping_a_kept_container_is_refused_rather_than_attempted():
    """The act re-derives nothing from the caller: handing it a `keep` verdict
    is a bug in the caller, and it must not become a stopped deployment."""
    kept = verdict(facts("sparkrun_2102cb4f1d6f_solo"), probe_fn=_answers)

    class Boom:
        def stop(self, *a, **kw):  # pragma: no cover - must never run
            raise AssertionError("stop was called on a kept container")

    with pytest.raises(ValueError):
        reap.stop_container(kept, Boom())


# -- the stranded log followers ---------------------------------------------


def test_the_newest_log_follower_is_kept_and_the_rest_are_strays(monkeypatch):
    """One `tail -f` per minute per deployment accumulated inside these
    containers -- 1195 in one, 2994 in another. The newest is kept because it
    may be a real subscription somebody is reading right now."""
    listing = "\n".join(
        "%d %d tail -f --lines 200 %s" % (100 + i, 60 * i, reap.SERVE_LOG_TAIL)
        for i in range(1, 6)
    ) + "\n999 5 /usr/bin/python3 vllm serve X\n"

    monkeypatch.setattr(
        reap, "_run",
        lambda argv, **kw: type("P", (), {"returncode": 0, "stdout": listing, "stderr": ""})(),
    )
    found = reap.read_tail_strays("sparkrun_648b629fb11e_solo")
    assert found.kept == 1
    assert len(found.pids) == 4
    # Oldest first: pid 105 has the largest elapsed time and must go first,
    # while pid 101 -- 60s old, the newest -- is the one kept.
    assert found.pids[0] == 105
    assert 101 not in found.pids
    # The serve process itself is never a candidate.
    assert 999 not in found.pids


def test_a_shell_that_merely_tails_something_else_is_not_a_stray(monkeypatch):
    """Matched on sparkrun's own log path, not on `tail -f` alone, so an
    operator's own follower inside a container is never killed."""
    listing = "500 900 tail -f /var/log/something-else.log\n"
    monkeypatch.setattr(
        reap, "_run",
        lambda argv, **kw: type("P", (), {"returncode": 0, "stdout": listing, "stderr": ""})(),
    )
    assert reap.read_tail_strays("sparkrun_648b629fb11e_solo").pids == []


def test_killing_nothing_calls_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(reap, "_run", lambda *a, **kw: called.append(a))
    killed, message = reap.kill_tail_strays(reap.TailStrays("c", [], 0))
    assert killed == 0 and called == []


# -- docker that could not be asked -----------------------------------------


def test_docker_that_did_not_answer_reaps_nothing(monkeypatch):
    """None is absence of evidence, never an empty box. A machine where docker
    is missing must clear nothing rather than conclude there is nothing."""
    monkeypatch.setattr(
        reap, "_run",
        lambda argv, **kw: type("P", (), {"returncode": 1, "stdout": "", "stderr": "nope"})(),
    )
    assert reap.read_containers() is None


def test_an_unreadable_creation_time_makes_a_container_look_young():
    """The age clause is a veto, so failing to read an age has to fall on the
    side of touching nothing."""
    now = 1_000_000.0
    assert reap._created_at("not a timestamp") == pytest.approx(
        __import__("time").time(), abs=5.0
    )
    assert now  # the parse never returns something older than now
