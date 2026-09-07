#!/bin/sh
# derate installer. One command per machine, no configuration.
#
#   # the first node -- it becomes the coordinator and serves the UI on :8080
#   curl -fsSL https://raw.githubusercontent.com/Pizzaman213/derate/integration/install.sh | sh
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
GPU=1
PURGE=0
KEEP_IMAGES=0
INSTALL_DOCKER=0
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
  --no-gpu            do not pass the GPU into the container. The node joins
                      but probes as unidentified hardware and the planner
                      will not place work on it.
  --dry-run           print the docker command that would run, and stop
  --uninstall         stop and remove the container (keeps the data volume)
  --purge             with --uninstall, also delete the data volume
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
        --no-gpu)       GPU=0; shift ;;
        --dry-run)      DRY_RUN=1; shift ;;
        --uninstall)    UNINSTALL=1; shift ;;
        --purge)        PURGE=1; shift ;;
        --keep-images)  KEEP_IMAGES=1; shift ;;
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

if [ "$UNINSTALL" -eq 1 ]; then
    ensure_docker
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

[ "$(uname -s)" = "Linux" ] || [ "$DRY_RUN" -eq 1 ] || die \
    "derate nodes run on Linux. This is $(uname -s)."

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
# Without them the container has no nvidia-smi, and a node with no nvidia-smi
# probes as DeviceClass.UNKNOWN: no device class, no GPU name, zero memory,
# zero power, zero temperature. It still joins, still reports healthy, still
# appears in the roster -- as hardware the coordinator says it cannot confirm
# is eligible. That is the worst shape a failure can take here, because nothing
# looks broken; the node is simply, quietly, never planned onto.
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
info "pulling $IMAGE"
if ! $DOCKER pull "$IMAGE" >/dev/null 2>&1; then
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
        info "reports healthy, but it probes as unidentified hardware -- no GPU"
        info "name, no memory, no power, no temperature -- and the planner will not"
        info "place work on it. Install the NVIDIA container toolkit and re-run:"
        info "  https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html"
    else
        info "Docker could not pass a GPU into the container, and this machine"
        info "has no NVIDIA driver on PATH, so there may be no GPU to pass. The"
        info "node started without it: it joins and reports healthy, but it"
        info "probes as unidentified hardware -- no GPU name, no memory, no"
        info "power, no temperature -- and the planner will not place work on"
        info "it. If this machine does have an NVIDIA GPU, install the driver"
        info "and the container toolkit and re-run. If it does not, pass"
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

info "waiting for the node to come up"
waited=0
until probe "http://127.0.0.1:$AGENT_PORT/agent/health"; do
    if [ "$waited" -ge "$HEALTH_TIMEOUT" ]; then
        info "the node did not answer /agent/health within ${HEALTH_TIMEOUT}s."
        info "it may still be starting. Its logs:"
        $DOCKER logs --tail 30 "$CONTAINER" >&2 || true
        exit 1
    fi
    if ! $DOCKER ps --format '{{.Names}}' | grep -qx "$CONTAINER"; then
        info "the container exited. Its logs:"
        $DOCKER logs --tail 40 "$CONTAINER" >&2 || true
        exit 1
    fi
    sleep 2
    waited=$((waited + 2))
done

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
