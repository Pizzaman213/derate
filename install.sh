#!/bin/sh
# derate installer. One command per machine, no configuration.
#
#   # the first node -- it becomes the coordinator and serves the UI on :8080
#   curl -fsSL https://raw.githubusercontent.com/Pizzaman213/derate/main/install.sh | sh
#
#   # every node after -- the UI's "Add a node" card composes this line for you,
#   # token and address already filled in
#   curl -fsSL http://<coordinator>:8080/install.sh | sh -s -- \
#       --join http://<coordinator>:8080 --token ej_...
#
# There is one image and one container. Role is decided at runtime by the node
# itself (00-architecture.md section 2), so this script's whole job is to put
# the container on the machine with the right two environment variables and
# then tell you what happened.
#
# POSIX sh on purpose: this runs through `curl | sh` on a box whose shell we do
# not get to choose. No bashisms, no arrays.

set -eu

IMAGE="${DERATE_IMAGE:-ghcr.io/pizzaman213/derate/node:latest}"
CONTAINER=derate
VOLUME=derate
LABEL=io.derate.node
# Every repository this node has ever shipped under. A machine installed before
# the move to GHCR is still running a derate node; it just does not answer to
# the current image name. Both are searched so an upgrade finds it.
IMAGE_REPOS="ghcr.io/pizzaman213/derate/node derate/node"
PORT=8080
AGENT_PORT=8081
JOIN=""
TOKEN=""
NAME=""
DRY_RUN=0
UNINSTALL=0
LEAVE=0
GPU=1
PURGE=0
KEEP_IMAGES=0
INSTALL_DOCKER=0
INSTALL_OLLAMA=0
HEALTH_TIMEOUT=60

usage() {
    cat <<'USAGE'
derate installer

  install.sh                          install this machine as the main node
  install.sh --join URL --token TOK   install it as a node of that cluster

Options
  --join URL          coordinator to join. Absent: this machine becomes one.
  --token TOKEN       enrollment token from the UI's "Add a node" card. An
                      enrollment token joins AND is admitted; the permanent
                      cluster token joins and waits for a click.
  --name NAME         override the node id (default: this machine's hostname)
  --image IMAGE       container image (default: ghcr.io/pizzaman213/derate/node:latest)
  --port PORT         coordinator/UI port (default: 8080)
  --agent-port PORT   node agent port (default: 8081)
  --install-docker    install Docker with get.docker.com if it is missing
  --install-ollama    also install Ollama on this machine and bind it to the
                      LAN, so a node with no GPU can serve a small model over
                      the network. Opt-in for the same reason --install-docker
                      is: this script does not install daemons you did not ask
                      for. The node itself does not need it.
  --no-gpu            do not pass the GPU into the container. On a machine
                      that has one the node then probes as unidentified
                      hardware; on a machine that has none -- a Pi, a NAS, a
                      spare box -- it probes as a CPU node, which is what it
                      is. Neither is given a rank by the planner.
  --dry-run           print the docker command that would run, and stop
  --uninstall         stop and remove the container (keeps the data volume)
  --purge             with --uninstall, also delete the data volume
  --leave             forget the cluster this machine belongs to -- drops the
                      cluster id and token, keeps this node's own identity and
                      everything else in the volume. Use it before moving a
                      machine to a different cluster, or the node keeps
                      presenting the old cluster's token and is turned away.
                      Without --uninstall it restarts the node afterwards, so
                      it comes back looking for a cluster to join.
  --keep-images       do not reclaim derate images the upgrade superseded
  -h, --help          this
USAGE
}

say()  { printf '%s\n' "$*"; }
info() { printf '[derate] %s\n' "$*" >&2; }
die()  { printf '[derate] %s\n' "$*" >&2; exit 1; }

need_value() {
    # $1 = flag name, $2 = value (may be unset)
    [ "$#" -ge 2 ] && [ -n "$2" ] || die "$1 needs a value. Try --help."
}

while [ $# -gt 0 ]; do
    case "$1" in
        --join)         need_value "$1" "${2:-}"; JOIN="$2"; shift 2 ;;
        --join=*)       JOIN="${1#*=}"; shift ;;
        --token)        need_value "$1" "${2:-}"; TOKEN="$2"; shift 2 ;;
        --token=*)      TOKEN="${1#*=}"; shift ;;
        --name)         need_value "$1" "${2:-}"; NAME="$2"; shift 2 ;;
        --name=*)       NAME="${1#*=}"; shift ;;
        --image)        need_value "$1" "${2:-}"; IMAGE="$2"; shift 2 ;;
        --image=*)      IMAGE="${1#*=}"; shift ;;
        --port)         need_value "$1" "${2:-}"; PORT="$2"; shift 2 ;;
        --port=*)       PORT="${1#*=}"; shift ;;
        --agent-port)   need_value "$1" "${2:-}"; AGENT_PORT="$2"; shift 2 ;;
        --agent-port=*) AGENT_PORT="${1#*=}"; shift ;;
        --install-docker) INSTALL_DOCKER=1; shift ;;
        --install-ollama) INSTALL_OLLAMA=1; shift ;;
        --no-gpu)       GPU=0; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        --uninstall)    UNINSTALL=1; shift ;;
        --purge)        PURGE=1; shift ;;
        --keep-images)  KEEP_IMAGES=1; shift ;;
        --leave)        LEAVE=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown option '$1'. Try --help." ;;
    esac
done

[ -n "$JOIN" ] || [ -z "$TOKEN" ] || die \
    "--token without --join has nothing to present it to. Add --join <coordinator url>."

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

DOCKER=docker
docker_ok() { $DOCKER info >/dev/null 2>&1; }

ensure_docker() {
    if command -v docker >/dev/null 2>&1; then
        if docker_ok; then return 0; fi
        if [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1; then
            # Almost always "your user is not in the docker group". Re-running
            # under sudo is the fix people actually apply, so apply it rather
            # than printing it.
            DOCKER="sudo docker"
            if docker_ok; then
                info "using sudo for docker (this user is not in the docker group)"
                return 0
            fi
        fi
        die "docker is installed but not responding. Is the daemon running? Try: systemctl start docker"
    fi

    if [ "$INSTALL_DOCKER" -eq 1 ]; then
        info "installing Docker via get.docker.com"
        command -v curl >/dev/null 2>&1 || die "curl is needed to install Docker."
        curl -fsSL https://get.docker.com | sh
        docker_ok || DOCKER="sudo docker"
        docker_ok || die "Docker was installed but is not responding yet. Start it and re-run this script."
        return 0
    fi

    # Deliberately not silent-installing a daemon from a piped script. Say
    # exactly what to run instead.
    die "docker is not installed.
    Install it, then re-run this script:
        curl -fsSL https://get.docker.com | sh
    or re-run this script with --install-docker to do that automatically.
    Packaged installs: https://docs.docker.com/engine/install/"
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

# Forgetting a cluster is not the same as forgetting the machine. `node.json`
# is this box's own identity and it stays: a machine that comes back keeps its
# place, its label and its position on the cluster floor. `cluster.json` is the
# membership -- the id and the shared token -- and that is what has to go, or
# the node keeps presenting the old cluster's credential to the new one and is
# rejected by it.
leave_cluster() {
    ensure_docker
    if $DOCKER ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        $DOCKER exec "$CONTAINER" rm -f /data/cluster.json >/dev/null 2>&1 \
            && info "forgot the cluster (kept this node's identity)" \
            || info "this node did not belong to a cluster"
    else
        info "no $CONTAINER container: nothing to forget"
    fi
}

if [ "$LEAVE" -eq 1 ] && [ "$UNINSTALL" -eq 0 ]; then
    leave_cluster
    $DOCKER restart "$CONTAINER" >/dev/null 2>&1 \
        && info "restarted $CONTAINER; it is looking for a cluster to join" \
        || true
    exit 0
fi

if [ "$UNINSTALL" -eq 1 ]; then
    ensure_docker
    [ "$LEAVE" -eq 0 ] || leave_cluster
    if $DOCKER ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        $DOCKER rm -f "$CONTAINER" >/dev/null
        info "removed the $CONTAINER container"
    else
        info "no $CONTAINER container to remove"
    fi
    if [ "$PURGE" -eq 1 ]; then
        $DOCKER volume rm "$VOLUME" >/dev/null 2>&1 \
            && info "deleted the $VOLUME volume (cluster token, roster, telemetry)" \
            || info "no $VOLUME volume to delete"
    else
        info "kept the $VOLUME volume. Re-run with --purge to delete it."
    fi
    exit 0
fi

# ---------------------------------------------------------------------------
# Compose the run
# ---------------------------------------------------------------------------

# This installer installs a *container*, and the flags below are Linux-host
# features: Docker Desktop's --network host does not reach the LAN, --gpus
# needs the NVIDIA container toolkit, and --pid=host has no host to name. So
# this is not a check that could be relaxed -- it is the wrong installer for
# those machines, and they have a right one. Say which, rather than stopping.
[ "$(uname -s)" = "Linux" ] || [ "$DRY_RUN" -eq 1 ] || die \
    "derate's container runs on Linux, and this is $(uname -s).
Install it natively instead:
    pipx install derate && derate
To join an existing cluster, add its address and an enrollment token:
    DERATE_JOIN=http://<coordinator>:8080 DERATE_TOKEN=ej_... derate
Settings -> Add a node on the coordinator composes that line for you.
A machine installed this way is a full cluster member: it is discovered, it
reports its hardware and it can serve models through a local runtime. It
cannot carry a tensor- or pipeline-parallel rank, because that is launched
through sparkrun onto Linux GPU nodes."

# Host networking is not optional: mDNS is multicast and does not cross a
# bridge, and the agent needs the real interfaces to report the ConnectX-7
# topology. The container refuses to start without it, so this is not a
# preference the script is expressing.
set -- -d --name "$CONTAINER" --network host --restart unless-stopped
# The mark a later run finds this container by. Names are not enough: a node
# started by hand, or by an older installer under a different name, is still an
# instance of this thing and still binds the same ports.
set -- "$@" --label "$LABEL=1"
set -- "$@" -v "$VOLUME:/data"

# sparkrun drives the cluster over SSH and reads its own config. Mounted only
# when the paths exist: a bind mount of a missing path silently creates a
# root-owned directory in the operator's home.
[ -d "${HOME:-/root}/.config/sparkrun" ] && \
    set -- "$@" -v "${HOME:-/root}/.config/sparkrun:/root/.config/sparkrun:ro"
[ -d "${HOME:-/root}/.ssh" ] && \
    set -- "$@" -v "${HOME:-/root}/.ssh:/root/.ssh:ro"

# The model cache the runtime downloads into, read-write. This is the mount
# that lets the Storage tab show and reclaim downloaded weights, which are the
# largest thing on a serving box by a wide margin. Mounted only when it already
# exists, for the same reason as the two above: a bind mount of a missing path
# creates a root-owned directory in the operator's home.
[ -d "${HOME:-/root}/.cache/huggingface" ] && \
    set -- "$@" -v "${HOME:-/root}/.cache/huggingface:/root/.cache/huggingface"

set -- "$@" -e "DERATE_PORT=$PORT" -e "DERATE_AGENT_PORT=$AGENT_PORT"
[ -n "$JOIN" ]  && set -- "$@" -e "DERATE_JOIN=$JOIN"
[ -n "$TOKEN" ] && set -- "$@" -e "DERATE_TOKEN=$TOKEN"
[ -n "$NAME" ]  && set -- "$@" -e "DERATE_NODE_ID=$NAME"
set -- "$@" "$IMAGE"

# The GPU. Held apart from the rest of the run because a Docker that cannot
# honour --gpus has to be retried without it, and rebuilding the whole argument
# list in POSIX sh to drop two flags is worse than composing them separately.
#
# Without them the container has no nvidia-smi, and on a machine that has a
# GPU that is DeviceClass.UNKNOWN: no device class, no GPU name, zero memory.
# It still joins, still reports healthy, still appears in the roster -- as
# hardware the coordinator says it cannot confirm is eligible. That is the
# worst shape a failure can take here, because nothing looks broken; the node
# is simply, quietly, never planned onto.
#
# What makes that verdict survivable is that the probe does not reach it from
# the absence of nvidia-smi alone: registry/probe.py also reads
# /proc/driver/nvidia and the PCI vendor ids, and BOTH are visible from inside
# a container started without these flags -- the proc entry belongs to the
# loaded driver, the bus is the host's. So a GPU machine missing the container
# toolkit still reads as unidentified, which is what keeps this failure
# findable, while a machine that genuinely has no GPU reads as DeviceClass.CPU
# and is a cluster member in good standing.
#
# --pid=host belongs to the same flag, not to a separate appetite for
# privilege. On GB10 every aggregate FB memory field reads [N/A] and
# --query-compute-apps is the only thing that separates a resident model from
# the desktop. nvidia-smi reports the processes it can see, so from inside its
# own PID namespace it sees none of them, reports zero bytes of GPU memory, and
# the whole unified pool is attributed to the operating system.
# Always requested unless --no-gpu. Docker is the authority on whether the
# device can actually be handed over, and the run-and-retry below asks it --
# so nothing here tries to predict the answer. The previous gate on a host
# nvidia-smi was a bad proxy for it: the container toolkit injects nvidia-smi
# into the container from the driver, so whether the host happens to have that
# binary on PATH is not what determines whether the container gets a GPU. A
# machine with a working driver and a non-login shell's PATH would be silently
# downgraded to an unidentified node -- the exact quiet failure the comment
# above is about, arrived at by the check meant to prevent it.
GPU_ARGS_PRESENT=0
[ "$GPU" -eq 1 ] && GPU_ARGS_PRESENT=1

# Only ever used to word the fallback message correctly. A machine with no
# driver at all and one whose Docker cannot reach the driver are the same
# failed `docker run` and different advice.
HAS_DRIVER=0
command -v nvidia-smi >/dev/null 2>&1 && HAS_DRIVER=1

# Runs docker with the GPU flags when they were asked for and are available.
run_node() {
    if [ "$GPU_ARGS_PRESENT" -eq 1 ]; then
        $DOCKER run --gpus all --pid=host "$@"
    else
        $DOCKER run "$@"
    fi
}

# Before ensure_docker: --dry-run is how you inspect the command from a machine
# that has no Docker yet, which is most of the machines you would want to ask.
if [ "$DRY_RUN" -eq 1 ]; then
    if [ "$GPU_ARGS_PRESENT" -eq 1 ]; then
        say "docker run --gpus all --pid=host $*"
    else
        say "docker run $*"
    fi
    exit 0
fi

ensure_docker

# Every container on this machine that is an instance of this node, whatever it
# is called. Three ways in, because an install can be older than any one of
# them: the label this script now stamps on what it creates, the fixed name it
# has always used, and the image itself -- which catches a container somebody
# started by hand with `docker run`. A node is a node; if it is here it holds
# the ports and the volume, and a second one would fight it for both.
existing_nodes() {
    {
        $DOCKER ps -aq --filter "label=$LABEL" 2>/dev/null
        $DOCKER ps -aq --filter "name=^${CONTAINER}$" 2>/dev/null
        for repo in $IMAGE_REPOS; do
            $DOCKER images -q "$repo" 2>/dev/null | while read -r img; do
                [ -n "$img" ] && $DOCKER ps -aq --filter "ancestor=$img" 2>/dev/null
            done
        done
    } | sort -u
}

OLD_NODES="$(existing_nodes)"

# ---------------------------------------------------------------------------
# The ports
#
# The container runs on host networking, so $PORT and $AGENT_PORT are this
# machine's own ports and whatever already holds one wins. The node then dies
# in uvicorn with EADDRINUSE -- and because `--restart unless-stopped` starts
# it again immediately, a crash loop reads as a running container, so the wait
# at the bottom of this script sat out its whole timeout and printed a
# traceback whose only useful line had already scrolled past the tail. Ask
# before pulling an image and destroying a working node for a run that cannot
# bind.
# ---------------------------------------------------------------------------

# The listening address when something holds the port, nothing when it is
# free, and nothing when this machine has no way to tell. Not knowing must not
# become a refusal: without ss or netstat the run below is still attempted and
# diagnose() still reads the bind error out of the container's logs.
port_listener() {
    if command -v ss >/dev/null 2>&1; then
        ss -ltn 2>/dev/null | awk -v p=":$1\$" '$4 ~ p { print $4; exit }'
    elif command -v netstat >/dev/null 2>&1; then
        netstat -ltn 2>/dev/null | awk -v p=":$1\$" '$4 ~ p { print $4; exit }'
    fi
}

# $1 = port, $2 = the flag that moves it, $3 = seconds to wait for it to clear
# (a socket does not always disappear the instant `docker rm` returns). Says
# what is wrong and returns 0 when the port is taken.
port_conflict() {
    _waited_port=0
    _held="$(port_listener "$1")"
    while [ -n "$_held" ] && [ "$_waited_port" -lt "${3:-0}" ]; do
        sleep 1
        _waited_port=$((_waited_port + 1))
        _held="$(port_listener "$1")"
    done
    [ -n "$_held" ] || return 1
    info "port $1 is already in use on this machine ($_held is listening)."
    info "The node uses host networking, so it binds this machine's own ports"
    info "and cannot have that one. Re-run with $2 <free port>, or free it."
    return 0
}

# $1 = seconds to wait for each port. Dies naming every port that is taken,
# not just the first: being sent back twice for two ports is worse than once.
require_ports() {
    _conflict=0
    if port_conflict "$PORT" --port "$1"; then _conflict=1; fi
    if port_conflict "$AGENT_PORT" --agent-port "$1"; then _conflict=1; fi
    [ "$_conflict" -eq 0 ] || return 1
    return 0
}

# A node already running here holds these ports itself and is about to be
# replaced, so this early question is only asked when there is nothing of ours
# to remove. It is asked again below, unconditionally, once the old container
# is gone.
if [ -z "$OLD_NODES" ]; then
    require_ports 0 || die "nothing on this machine was changed."
fi

# ---------------------------------------------------------------------------
# Upgrade in place
#
# Pull first, destroy second. The other order -- which this script used to use
# -- removes a working node and only then discovers the registry is
# unreachable, leaving the machine with nothing running and nothing to run.
# ---------------------------------------------------------------------------

# A locally built or `docker load`-ed image is a first-class case, not an
# error: a lab cluster has no registry, and the tag somebody built on the
# coordinator is on that machine and nowhere else. So a failed pull is fatal
# only when this machine does not already have the image.
#
# Docker's own progress goes to the terminal rather than to /dev/null. A
# silent pull is indistinguishable from a hung one -- this line was the last
# thing on screen for as long as the download took, with nothing to say whether
# it was moving -- and when a pull fails, docker's message ("manifest unknown",
# "denied") is the only thing that says why. Both were being discarded.
info "pulling $IMAGE"
if ! $DOCKER pull "$IMAGE" >&2; then
    if $DOCKER image inspect "$IMAGE" >/dev/null 2>&1; then
        info "could not pull $IMAGE; using the copy already on this machine"
    else
        info "could not pull $IMAGE, and this machine has no local copy."
        info "Copy it from a machine that has it:"
        info "  docker save $IMAGE | gzip > node.tgz   # there"
        info "  gunzip -c node.tgz | sudo docker load  # here"
        die "no image to run"
    fi
fi

if [ -n "$OLD_NODES" ]; then
    for id in $OLD_NODES; do
        was="$($DOCKER inspect -f '{{.Name}} ({{.State.Status}})' "$id" 2>/dev/null \
               | sed 's|^/||')"
        info "replacing existing node ${was:-$id}"
    done
    # The volume is deliberately untouched: it holds the cluster token, the
    # node registry, measured links and deployment records. An upgrade that
    # forgot which cluster the machine was in would be a reinstall.
    info "the $VOLUME volume is kept"
    # shellcheck disable=SC2086
    $DOCKER rm -f $OLD_NODES >/dev/null 2>&1 || true
fi

# The old node's sockets are released asynchronously, so this waits rather than
# racing it. Anything still holding a port now belongs to somebody else, and no
# amount of restarting will change that.
require_ports 10 || die "the node was not started."

# Docker's own message is the only useful thing to say about a failed start, so
# it is kept rather than discarded -- but it cannot go straight to the terminal,
# because the first attempt failing is a normal step on a machine that has a
# driver and no container toolkit.
RUN_ERR="${TMPDIR:-/tmp}/derate-run.$$"
give_up() {
    cat "$RUN_ERR" >&2
    rm -f "$RUN_ERR"
    die "could not start the $CONTAINER container"
}

info "starting the node"
if ! run_node "$@" >/dev/null 2>"$RUN_ERR"; then
    # Almost always the NVIDIA container toolkit: the driver is on the machine,
    # so nvidia-smi answered above, but Docker has no way to hand the device to
    # a container. Installing it is not this script's business, and refusing to
    # install at all would be worse than a node that runs -- so fall back, and
    # say plainly what was lost, because an unidentified node is invisible in
    # exactly the way that reads as a bug somewhere else.
    [ "$GPU_ARGS_PRESENT" -eq 1 ] || give_up
    GPU_ARGS_PRESENT=0
    $DOCKER rm -f "$CONTAINER" >/dev/null 2>&1 || true
    run_node "$@" >/dev/null 2>"$RUN_ERR" || give_up
    if [ "$HAS_DRIVER" -eq 1 ]; then
        info "this machine has an NVIDIA driver but Docker could not pass the GPU"
        info "into the container, so the node started without it. It joins and"
        info "reports healthy, and it reports host memory, temperature and CPU --"
        info "but the probe can see the driver and not the GPU, so it records"
        info "unidentified hardware and the planner will not place work on it."
        info "Install the NVIDIA container toolkit and re-run:"
        info "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    else
        info "Docker could not pass a GPU into the container, and this machine has"
        info "no NVIDIA driver on PATH. The node started without it, and what it"
        info "then reports depends on what is actually here. If there is no NVIDIA"
        info "hardware at all -- a Pi, a NAS, a spare box -- it probes as a CPU"
        info "node: a cluster member with real host telemetry, which can front a"
        info "provider but will not be given a rank. If there IS a card, the probe"
        info "finds it on the PCI bus and records unidentified hardware instead;"
        info "install the driver and the container toolkit and re-run. Pass"
        info "--no-gpu to skip this attempt and this message."
    fi
fi
rm -f "$RUN_ERR"

# ---------------------------------------------------------------------------
# Wait, then say what happened
# ---------------------------------------------------------------------------

# /agent/health is the one endpoint that exists in BOTH roles. /api/cluster is
# coordinator-only and would report every worker as broken.
probe() {
    if command -v curl >/dev/null 2>&1; then
        curl -fsS "$1" >/dev/null 2>&1
    elif command -v wget >/dev/null 2>&1; then
        wget -q -O /dev/null "$1" 2>/dev/null
    else
        # Nothing to probe with. Not a failure: the container is running and
        # will come up on its own.
        return 0
    fi
}

# Why the node is not up, in the order the answer is worth having. A bind
# failure is named rather than shown, because the line that explains it --
# uvicorn's OSError -- is some ninety frames of asyncio traceback above the end
# of the log it produces, so a tail of the last thirty lines printed every
# frame and none of the cause. The whole log is searched, and the tail is only
# the fallback for a failure this script does not recognise.
diagnose() {
    _bind="$($DOCKER logs "$CONTAINER" 2>&1 | grep -m1 'address already in use' || true)"
    if [ -n "$_bind" ]; then
        _p="$(printf '%s' "$_bind" | sed -n 's/.*, \([0-9]\{1,5\}\)).*/\1/p')"
        info "the node cannot bind port ${_p:-$PORT}: something else on this"
        info "machine is already listening on it. Docker says the container is"
        info "running because it keeps restarting it; it exits every time."
        info "  $_bind"
        if [ "${_p:-$PORT}" = "$AGENT_PORT" ]; then
            info "Re-run with --agent-port <free port>, or free that port."
        else
            info "Re-run with --port <free port>, or free that port."
        fi
        info "The container is left in place; --uninstall removes it."
        return 0
    fi
    info "its logs:"
    $DOCKER logs --tail 60 "$CONTAINER" >&2 || true
}

info "waiting for the node to come up"
waited=0
until probe "http://127.0.0.1:$AGENT_PORT/agent/health"; do
    # A crash loop reads as a running container. `--restart unless-stopped`
    # brings the node back the moment it exits, so the exited-check below never
    # fires for one that dies on startup -- which is how a node that was dead
    # three seconds in was waited on for the full timeout. The restart counter
    # is the thing that tells them apart, and one restart is already an answer:
    # nothing about waiting longer changes what the next start will do.
    #
    # Anything that is not a number is treated as no restarts: a docker too
    # old to answer, or one that answers with nothing, must leave the wait
    # below intact rather than turning `[ "" -gt 0 ]` into a failed install.
    restarted="$($DOCKER inspect -f '{{.RestartCount}}' "$CONTAINER" 2>/dev/null || true)"
    case "$restarted" in ''|*[!0-9]*) restarted=0 ;; esac
    if [ "$restarted" -gt 0 ]; then
        info "the node is restarting in a loop, so it is not going to come up."
        diagnose
        exit 1
    fi
    if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
        info "the node did not answer /agent/health within ${HEALTH_TIMEOUT}s."
        info "it may still be starting."
        diagnose
        exit 1
    fi
    if ! $DOCKER ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        info "the container exited."
        diagnose
        exit 1
    fi
    sleep 2
    waited=$((waited + 2))
done

# Ollama, when it was asked for. After the node is confirmed up, deliberately:
# this is an extra, and a machine whose node did not start has a worse problem
# than a missing runtime.
#
# A node with no GPU cannot be given a rank -- the planner will not place a
# model on it and the fit gate refuses, because there is no GPU memory to
# budget. What such a machine CAN do is run a small model itself and be routed
# to as a provider. That is what this installs; the node does not need it and
# never calls it.
#
# Bound to 0.0.0.0 on purpose. Ollama listens on 127.0.0.1 by default, which is
# correct for a laptop and useless here: the coordinator is on another machine
# and would find nothing. This is the one setting that decides whether the
# runtime is reachable at all, so it is set here rather than left as an
# instruction somebody has to find.
install_ollama() {
    if command -v ollama >/dev/null 2>&1; then
        info "ollama is already installed"
    else
        info "installing Ollama via ollama.com/install.sh"
        command -v curl >/dev/null 2>&1 || die "curl is needed to install Ollama."
        curl -fsSL https://ollama.com/install.sh | sh || \
            die "the Ollama installer failed. Install it yourself and re-run with --install-ollama."
    fi

    # systemd is how the vendor script installs it. Without systemd we have no
    # supported way to make the bind address stick across a reboot, and saying
    # so beats writing a unit file for an init system we did not detect.
    if ! command -v systemctl >/dev/null 2>&1; then
        info "ollama is installed, but this machine has no systemctl, so its"
        info "listen address was not changed. It will only answer on localhost"
        info "and the coordinator will not find it. Set OLLAMA_HOST=0.0.0.0:11434"
        info "in however this machine starts services."
        return 0
    fi

    SUDO=""
    [ "$(id -u)" -ne 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"
    OLLAMA_DROPIN=/etc/systemd/system/ollama.service.d
    info "binding ollama to 0.0.0.0:11434 so the coordinator can reach it"
    $SUDO mkdir -p "$OLLAMA_DROPIN" || die "could not write $OLLAMA_DROPIN"
    # A drop-in rather than an edit of the unit: the vendor's installer owns
    # ollama.service and will overwrite it on upgrade. This survives that.
    printf '[Service]\nEnvironment="OLLAMA_HOST=0.0.0.0:11434"\n' \
        | $SUDO tee "$OLLAMA_DROPIN/derate.conf" >/dev/null \
        || die "could not write the ollama drop-in"
    $SUDO systemctl daemon-reload || true
    $SUDO systemctl enable --now ollama >/dev/null 2>&1 || true
    $SUDO systemctl restart ollama || die "ollama would not restart. Check: systemctl status ollama"

    # Confirm it is actually answering on the LAN address, not just running.
    # "installed" and "reachable" are different claims and only the second one
    # is what the coordinator needs.
    waited=0
    until probe "http://127.0.0.1:11434/api/tags"; do
        if [ "$waited" -ge 30 ]; then
            info "ollama was installed but is not answering on 11434 yet."
            info "check: systemctl status ollama"
            return 0
        fi
        sleep 2
        waited=$((waited + 2))
    done
    info "ollama is up on 0.0.0.0:11434"
    info "open this node in the UI and use 'Add as a provider' to route to it"
}

if [ "$INSTALL_OLLAMA" -eq 1 ]; then
    install_ollama
fi

# Which build is now on this machine. Read off the container, not out of this
# script: the container inherits the image's labels and it is the thing that is
# actually running, so this cannot drift from the truth the roster will report.
# `with` rather than a bare `index` so an image built before the label existed
# prints nothing instead of a template's idea of nothing.
INSTALLED_BUILD=$(
    $DOCKER inspect \
        -f '{{with index .Config.Labels "org.opencontainers.image.revision"}}{{.}}{{end}}' \
        "$CONTAINER" 2>/dev/null || true
)
if [ -n "$INSTALLED_BUILD" ]; then
    info "installed build $INSTALLED_BUILD"
else
    # The branch that earns its place. An image built without DERATE_BUILD
    # carries no stamp, and this node will sit in the roster with its build
    # unknown -- which reads as a mystery box rather than as an unstamped
    # build. Saying it here costs nothing; discovering it from a roster three
    # days later costs an afternoon.
    info "installed an image with no build stamp; the roster will show this node's build as unknown"
fi

# The node answered /agent/health, so the image it is running is known good and
# the ones it replaced are dead weight -- a derate image is ~300 MB and an
# upgrade would otherwise leave every previous one on the disk forever. Done
# only after the health probe passes, so a failed start still has something to
# roll back to. `docker rmi` refuses an image any container still references,
# so the failure mode here is a no-op, never a broken node.
if [ "$KEEP_IMAGES" -eq 0 ]; then
    IN_USE=$($DOCKER inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true)
    RECLAIMED=0
    for repo in $IMAGE_REPOS; do
        for img in $($DOCKER images --no-trunc -q "$repo" 2>/dev/null | sort -u); do
            if [ "$img" != "$IN_USE" ] && $DOCKER rmi "$img" >/dev/null 2>&1; then
                RECLAIMED=$((RECLAIMED + 1))
            fi
        done
    done
    if [ "$RECLAIMED" -gt 0 ]; then
        info "reclaimed $RECLAIMED superseded derate image(s). --keep-images skips this."
    fi
fi

LOGS=$($DOCKER logs "$CONTAINER" 2>&1 || true)

if [ -z "$JOIN" ]; then
    CLUSTER_ID=$(
        $DOCKER exec "$CONTAINER" cat /data/cluster.json 2>/dev/null |
        sed -n 's/.*"cluster_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p'
    )
    ADDR=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "${ADDR:-}" ] || ADDR=127.0.0.1
    say ""
    say "  derate is up."
    say ""
    say "  cluster:  ${CLUSTER_ID:-unknown}"
    say "  ui:       http://$ADDR:$PORT"
    say ""
    say "  Add another machine: open the UI, Settings -> Add a node, and run the"
    say "  command it gives you on that machine. It joins and is admitted; there"
    say "  is nothing to click afterwards."
    say ""
    # The permanent cluster token stays off the terminal on purpose. It is the
    # forever-secret, the UI mints a one-hour one for installs, and a token in
    # a scrollback is a token in a screenshot.
    say "  (The permanent cluster token, if you ever need it directly:"
    say "   docker exec $CONTAINER cat /data/cluster.json)"
    say ""
    exit 0
fi

say ""
if printf '%s' "$LOGS" | grep -q "status=member"; then
    say "  This machine joined $JOIN and was admitted. It is a member now."
    say "  It will appear in the cluster graph within a few seconds."
elif printf '%s' "$LOGS" | grep -q "status=candidate"; then
    say "  This machine reached $JOIN and is waiting to be admitted."
    say "  Open the UI at $JOIN, go to Settings, and click Admit next to it."
    say ""
    say "  A token from the UI's \"Add a node\" card admits automatically."
    say "  The permanent cluster token does not, by design."
else
    say "  This machine is up and looking for a coordinator."
    say "  Check the UI at $JOIN, or read: docker logs $CONTAINER"
fi
say ""
