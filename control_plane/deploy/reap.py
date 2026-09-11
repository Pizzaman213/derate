"""Clear what derate left running and lost track of. Dry run by default.

The mirror image of :mod:`control_plane.deploy.autoadopt`, and deliberately
built to be its complement rather than its competitor. Adopt and reap are
opposite verdicts on ONE population -- sparkrun-named containers this
coordinator has no live record for -- and one probe separates them:

    probe() says the backend is ours          -> the adopter takes it
    probe() says a DIFFERENT model is there   -> neither touches it
    probe() says nothing answers              -> a reap candidate

So the sets are disjoint by construction, on the same call, with no shared
state and no lock between the two.

**A live backend is never reaped, and that is what makes peer coordinators a
non-problem.** Measured on the development box while this was being written:
four `sparkrun_*_solo` containers were launched by a coordinator running
inside a container and independently ADOPTED by a second coordinator on the
host. A reaper written to the obvious rule -- "sparkrun-named, and my store
has no record" -- run from either one before it adopted, would have destroyed
four of the other's live models. The identity probe is therefore an
independent veto, not one signal among several, and this module never needs to
know that other coordinators exist.

**Two populations, because the second is by far the larger one.** Containers
are what the TODO named, and on a healthy box there are usually none to clear.
The processes inside them are the real pile: `sparkrun logs` follows for ever,
so polling it stranded one `tail -f` per minute per deployment, and those
containers held 1195 and 2994 of them. The leak itself is fixed at source in
``SparkrunAdapter.log_snapshot``; this clears what it already left behind.

**Nothing here runs on a timer.** ``settings.py`` explains why the auto-adopt
flags are on by default -- a spurious adoption is a record to delete -- and
that argument does not transfer to a destructive verb. This is a command an
operator runs, it surveys unless told otherwise, and it prints the evidence
for every verdict including the refusals.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass, field

from ..gateway import states
from ..paths import data_dir
from .adopt import cluster_id_from_container_name, parse_serve_command
from .health import probe, wrong_model

logger = logging.getLogger(__name__)

#: The follower `sparkrun logs` leaves behind. Matched on the log path rather
#: than on `tail -f` alone so an operator's own shell inside a container is
#: never a candidate.
SERVE_LOG_TAIL = "/tmp/sparkrun_serve.log"

#: How many of the strays to keep per container. One, because the newest may
#: be a follower something is actively reading -- `stream_logs` subscribes for
#: real, and a launch being watched right now has one that is not garbage.
KEEP_NEWEST_TAILS = 1

_DOCKER_TIMEOUT_S = 10.0


def _docker() -> str:
    return shutil.which("docker") or "docker"


@dataclass(frozen=True)
class ContainerFacts:
    """One sparkrun-named container, as `docker ps` describes it."""

    name: str
    image: str
    age_s: float
    running: bool
    command: str | None = None


@dataclass(frozen=True)
class Verdict:
    """Why one container may or may not be cleared. `reason` is always set."""

    container_name: str
    cluster_id: str | None
    reapable: bool
    reason: str
    model_id: str | None = None
    port: int | None = None
    age_s: float = 0.0

    def line(self) -> str:
        mark = "REAP" if self.reapable else "keep"
        return "%-4s %-34s %s" % (mark, self.container_name, self.reason)


@dataclass
class TailStrays:
    """Stranded log followers inside one container."""

    container: str
    pids: list[int] = field(default_factory=list)
    kept: int = 0

    def line(self) -> str:
        return "%-34s %d stranded log follower(s), keeping %d" % (
            self.container,
            len(self.pids),
            self.kept,
        )


# -- the predicate ----------------------------------------------------------


def classify(
    facts: ContainerFacts,
    *,
    cluster_ids_live: set[str],
    cluster_ids_terminal: set[str],
    node_address: str,
    min_age_s: float,
    probe_fn=probe,
) -> Verdict:
    """Whether one container may be cleared, and the sentence saying why.

    Every clause is a veto, and the ORDER is load-bearing. The cheap
    record-and-age vetoes come first so a box full of unrelated containers
    costs no probes at all -- but the identity probe is last precisely so that
    nothing can return `reapable` without having asked the backend first. A
    clause that answers above it is a clause that can kill a live model.
    """
    cluster_id = cluster_id_from_container_name(facts.name)
    if cluster_id is None:
        # Not sparkrun's naming. `buildx_buildkit_sparkplane0` lives on the
        # development box and is the near-miss this anchored match exists for:
        # CLAUDE.md's rule is to grep for `sparkplane`, never for bare `spark`.
        return Verdict(facts.name, None, False, "not a sparkrun container name")

    if not facts.running:
        return Verdict(
            facts.name, cluster_id, False,
            "already exited; `docker rm` is the operator's call, not this one",
            age_s=facts.age_s,
        )

    if cluster_id in cluster_ids_live:
        # The peer-destruction guard. A record in a live state owns this, and a
        # backend that has stopped answering under a live record is Stop's
        # problem and the health watch's, not a reaper's.
        return Verdict(
            facts.name, cluster_id, False,
            "a live deployment record claims it", age_s=facts.age_s,
        )

    if facts.age_s < min_age_s:
        # A launch is legitimately up with a silent port for the whole
        # readiness window: CUDA graph capture alone is 40s+ and a large model
        # is far longer.
        return Verdict(
            facts.name, cluster_id, False,
            "only %.0fs old, inside the readiness window (%.0fs)"
            % (facts.age_s, min_age_s),
            age_s=facts.age_s,
        )

    spec = parse_serve_command(facts.command)
    if spec is None:
        # An unrecognised serve command is left exactly as found, the same
        # stance autoadopt.py takes. A future sparkrun runtime this cannot
        # parse must not be garbage by default.
        return Verdict(
            facts.name, cluster_id, False,
            "no record, and its command is not one this build recognises",
            age_s=facts.age_s,
        )

    healthy, reason = probe_fn(
        "http://%s:%d/v1" % (node_address, spec.port),
        expect_model=spec.model_id,
    )
    if healthy:
        # THE veto, and it is unconditional on purpose -- including over a
        # record this coordinator marked finished.
        #
        # A terminal record beside a backend that still answers as itself is
        # not an orphan, it is a coordinator that lost track: exactly what
        # `autoadopt.py` exists to repair, and it repairs it by ADOPTING this
        # container. Reaping instead would kill a model somebody is using, and
        # a restart is the very moment the two verbs are most likely to
        # disagree.
        #
        # Caught on the live box, by this module, in dry run: a peer restarted
        # the coordinator, its old records went STOPPED, and the terminal
        # clause used to sit ABOVE this probe -- so two containers that were
        # serving Qwen2.5-0.5B-Instruct and Qwen3-4B-AWQ came back `REAP`.
        # An order that lets any clause answer before this one is wrong.
        return Verdict(
            facts.name, cluster_id, False,
            "serving %s on :%d" % (spec.model_id, spec.port),
            spec.model_id, spec.port, facts.age_s,
        )
    if wrong_model(reason):
        # Somebody else's server holds that port. The evidence is then about a
        # different process and justifies nothing about this container.
        return Verdict(
            facts.name, cluster_id, False,
            "port :%d answers as something else -- %s" % (spec.port, reason),
            spec.model_id, spec.port, facts.age_s,
        )
    # Nothing answers, and nothing live claims it. Which of the two remaining
    # cases it is changes the sentence but not the verdict: derate recorded it
    # as finished and it is still up, or derate has no record of it at all.
    claimed = cluster_id in cluster_ids_terminal
    why = (
        "a record this coordinator marked finished, and nothing answers on :%d (%s)"
        if claimed
        else "no record, and nothing answers on :%d (%s)"
    )
    return Verdict(
        facts.name, cluster_id, True,
        why % (spec.port, reason or "silent"),
        spec.model_id, spec.port, facts.age_s,
    )


# -- reading the box --------------------------------------------------------


def _run(argv: list[str], *, timeout: float = _DOCKER_TIMEOUT_S):
    return subprocess.run(
        argv, capture_output=True, text=True, timeout=timeout, check=False
    )


def read_containers(*, now: float | None = None) -> list[ContainerFacts] | None:
    """Every sparkrun-named container on this machine, or None.

    None is "docker could not be asked", which is absence of evidence and must
    never be read as an empty box -- the same three-valued discipline
    ``sparkrun.check_job`` and ``registry/containers.py`` already use.

    Deliberately its own reader rather than ``registry/containers.py``'s: that
    one starts from ``nvidia-smi``'s compute processes, so a container whose
    engine has already died holds no CUDA context and is structurally
    invisible to it. That container is exactly the candidate here.
    """
    now = time.time() if now is None else now
    try:
        proc = _run([
            _docker(), "ps", "-a", "--no-trunc",
            "--filter", "name=^sparkrun_",
            "--format", "{{.Names}}\\t{{.Image}}\\t{{.CreatedAt}}\\t{{.State}}",
        ])
    except (subprocess.SubprocessError, OSError):
        logger.debug("could not list containers", exc_info=True)
        return None
    if proc.returncode != 0:
        return None

    out: list[ContainerFacts] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 4:
            continue
        name, image, created, state = (p.strip() for p in parts[:4])
        out.append(
            ContainerFacts(
                name=name,
                image=image,
                age_s=max(0.0, now - _created_at(created)),
                running=state.lower() == "running",
                command=_serve_command(name) if state.lower() == "running" else None,
            )
        )
    return out


def _created_at(value: str) -> float:
    """docker's `CreatedAt` -- '2026-09-09 17:29:11 +0000 UTC' -- as epoch.

    Unparseable reads as *now*, which makes the container look young and so
    keeps it: the age clause is a veto, and a failure to read the age must
    fall on the side of not touching anything.
    """
    try:
        stamp = " ".join(value.split()[:3])
        return time.mktime(time.strptime(stamp[:19], "%Y-%m-%d %H:%M:%S")) - (
            time.timezone if time.daylight == 0 else time.altzone
        )
    except (ValueError, IndexError, OverflowError):
        return time.time()


def _serve_command(container: str) -> str | None:
    """The serve command line running inside *container*, or None.

    Read from inside rather than from `docker inspect`: a solo launch execs
    the serve command into a container whose own entrypoint is a sleep, so the
    container's configured command says nothing about what it is serving.
    """
    try:
        proc = _run([_docker(), "exec", container, "ps", "-eo", "args="])
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None
    for line in proc.stdout.splitlines():
        if parse_serve_command(line) is not None:
            return line
    return None


def read_tail_strays(container: str, *, keep: int = KEEP_NEWEST_TAILS) -> TailStrays | None:
    """Stranded `sparkrun logs` followers inside one container, oldest first."""
    try:
        proc = _run([_docker(), "exec", container, "ps", "-eo", "pid=,etimes=,args="])
    except (subprocess.SubprocessError, OSError):
        return None
    if proc.returncode != 0:
        return None

    found: list[tuple[int, int]] = []  # (elapsed_seconds, pid)
    for line in proc.stdout.splitlines():
        if SERVE_LOG_TAIL not in line or " -f" not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            found.append((int(parts[1]), int(parts[0])))
        except ValueError:
            continue
    # Oldest first, so the newest -- which may be a live subscription -- is
    # what `keep` protects.
    found.sort(reverse=True)
    keep = max(0, keep)
    return TailStrays(
        container=container,
        pids=[pid for _, pid in found[: max(0, len(found) - keep)]],
        kept=min(keep, len(found)),
    )


# -- acting -----------------------------------------------------------------


def kill_tail_strays(strays: TailStrays) -> tuple[int, str]:
    """SIGKILL the stranded followers. Returns (killed, message).

    One `docker exec kill` for the whole batch rather than one per pid: these
    number in the thousands and a call each would take longer than the leak
    took to build.
    """
    if not strays.pids:
        return 0, "nothing to kill"
    argv = [_docker(), "exec", strays.container, "kill", "-9"]
    argv += [str(pid) for pid in strays.pids]
    try:
        proc = _run(argv, timeout=60.0)
    except (subprocess.SubprocessError, OSError) as exc:
        return 0, "could not kill: %s" % type(exc).__name__
    # A pid that exited between the read and the kill makes `kill` non-zero
    # while every other pid in the batch still died. Report the command's own
    # words and let the recount below be the truth.
    return len(strays.pids), (proc.stderr or proc.stdout or "").strip()


def stop_container(verdict: Verdict, adapter) -> tuple[bool, str]:
    """Tear one reapable container down through sparkrun, never docker.

    `sparkrun stop` is this project's teardown verb and the one `manager.stop`
    uses; reaching past it to `docker rm` would leave sparkrun's own view of
    the cluster claiming something that is gone.
    """
    if not verdict.reapable:
        raise ValueError("refusing to stop a container the survey kept")
    return adapter.stop(verdict.cluster_id)


# -- the survey -------------------------------------------------------------


def _records(state_dir) -> tuple[set[str], set[str]]:
    """(live cluster ids, terminal cluster ids) out of the deployment store."""
    from .store import DeploymentStore

    live: set[str] = set()
    terminal: set[str] = set()
    try:
        rows = DeploymentStore(state_dir).load_all()
    except Exception:
        logger.debug("could not read the deployment store", exc_info=True)
        return live, terminal
    for deployment, handle in rows:
        cluster_id = (handle or {}).get("cluster_id")
        if not cluster_id:
            continue
        if deployment.state in states.TERMINAL:
            terminal.add(cluster_id)
        else:
            live.add(cluster_id)
    return live, terminal


def survey(
    *,
    state_dir,
    node_address: str = "127.0.0.1",
    min_age_s: float = 1800.0,
    now: float | None = None,
) -> tuple[list[Verdict], list[TailStrays]]:
    """Look at the box. Changes nothing."""
    facts = read_containers(now=now)
    if facts is None:
        return [], []
    live, terminal = _records(state_dir)
    verdicts = [
        classify(
            f,
            cluster_ids_live=live,
            cluster_ids_terminal=terminal,
            node_address=node_address,
            min_age_s=min_age_s,
        )
        for f in facts
    ]
    strays = []
    for f in facts:
        if not f.running:
            continue
        found = read_tail_strays(f.name)
        if found is not None and found.pids:
            strays.append(found)
    return verdicts, strays


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m control_plane.deploy.reap",
        description=(
            "Survey sparkrun containers this coordinator has lost track of, "
            "and the log followers stranded inside them. Prints what it would "
            "do; --yes is required to do it."
        ),
    )
    parser.add_argument("--data-dir", default=None, help="defaults to paths.data_dir()")
    parser.add_argument("--node-address", default="127.0.0.1")
    parser.add_argument("--min-age-s", type=float, default=1800.0)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--yes", action="store_true", help="actually stop and kill what was found"
    )
    parser.add_argument(
        "--tails-only", action="store_true",
        help="ignore containers entirely; only clear stranded log followers",
    )
    args = parser.parse_args(argv)

    root = args.data_dir or str(data_dir())
    from pathlib import Path

    state_dir = Path(root) / "deployments"

    verdicts, strays = survey(
        state_dir=state_dir,
        node_address=args.node_address,
        min_age_s=args.min_age_s,
    )

    if args.json:
        print(json.dumps({
            "containers": [v.__dict__ for v in verdicts],
            "tail_strays": [
                {"container": s.container, "pids": s.pids, "kept": s.kept}
                for s in strays
            ],
        }, indent=2))
    else:
        if not verdicts:
            print("no sparkrun containers on this machine (or docker could not be asked)")
        for verdict in verdicts:
            print(verdict.line())
        for stray in strays:
            print(stray.line())

    reapable = [v for v in verdicts if v.reapable] if not args.tails_only else []
    if not args.yes:
        print(
            "\ndry run. %d container(s) and %d stranded follower(s) would be "
            "cleared; pass --yes to do it."
            % (len(reapable), sum(len(s.pids) for s in strays))
        )
        return 0

    for stray in strays:
        killed, message = kill_tail_strays(stray)
        print("killed %d follower(s) in %s %s" % (killed, stray.container, message))
    if reapable:
        from .sparkrun import SparkrunAdapter

        adapter = SparkrunAdapter()
        for verdict in reapable:
            ok, output = stop_container(verdict, adapter)
            print("%s %s: %s" % ("stopped" if ok else "FAILED", verdict.container_name, output.strip()))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
