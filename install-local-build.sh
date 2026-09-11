#!/bin/sh
# install.sh, run against an image built from this checkout instead of one
# pulled from GHCR -- plus a menu for the container it starts, since a local
# build is something you kill and restart a lot more than a pull is.
#
#   ./install-local-build.sh                          # menu: build, status, logs, stop, kill...
#   ./install-local-build.sh --join URL --token TOK   # no menu: build, then join a cluster
#   ./install-local-build.sh --dry-run                # no menu: build, then just print the docker run
#
# Any argument skips the menu and goes straight to build-then-install, same
# as before -- that path stays scriptable. With no arguments at all, it opens
# an interactive menu instead of running.
#
# Defaults to --port 18080 --agent-port 18081, the "test instances on 18xxx"
# convention from CLAUDE.md's "This box" section, rather than install.sh's
# own 8080/8081 -- this is a dev-loop tool run on a box that already has real
# things bound to the real ports, and colliding with one of them is not this
# script's business to fix by killing it. Pass --port/--agent-port yourself
# to override; they are only defaults, and the last flag install.sh sees
# wins.
#
# Builds via docker/build.sh --load, which builds for this machine's
# architecture only (buildx cannot load a multi-arch manifest into the local
# daemon) and loads the result into the local docker daemon as
# derate/node:local. That tag has no registry host, so install.sh's own "pull
# failed, use the copy already on this machine" fallback (install.sh:522-543)
# is what actually runs it -- there is nothing to pull, and install.sh's
# IMAGE_REPOS already lists the bare "derate/node" repo, so upgrade and
# reclaim both recognize it. Every other flag install.sh takes is forwarded
# verbatim, same as install-dev.sh; --image still wins if you pass it
# yourself.
#
# **Not a copy of install.sh.** See install-dev.sh for why: a second copy is
# a second thing to keep in step, and the copy is always the half that goes
# stale. This builds, then finds the real installer and runs it. The menu
# below only ever calls plain `docker` against the container install.sh
# creates -- it does not reimplement --uninstall/--leave, it is a faster path
# to the commands you would otherwise have to remember.
#
# Unlike install.sh and install-dev.sh, this only makes sense from a
# checkout -- building needs the source tree, not just installer text -- so
# there is no curl-fetch fallback here.

set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TAG=local
IMAGE="derate/node:${TAG}"
# install.sh's own $CONTAINER -- has to match, since this menu manages the
# container that script creates, not one of its own.
CONTAINER=derate
PORT=18080
AGENT_PORT=18081

info() { printf '[derate] %s\n' "$*" >&2; }
die()  { printf '[derate] %s\n' "$*" >&2; exit 1; }

[ -f "$DIR/docker/build.sh" ] || die "docker/build.sh not found -- run this from a derate checkout."
[ -f "$DIR/install.sh" ] || die "install.sh not found next to this script."

build_and_install() {
    info "building ${IMAGE} from ${DIR}"
    IMAGE=derate/node TAG="$TAG" "$DIR/docker/build.sh" --load
    info "installing ${IMAGE}"
    DERATE_IMAGE="$IMAGE" sh "$DIR/install.sh" \
        --port "$PORT" --agent-port "$AGENT_PORT" "$@"
}

have_container() {
    command -v docker >/dev/null 2>&1 || return 1
    docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"
}

print_status() {
    if have_container; then
        docker ps -a --filter "name=^${CONTAINER}\$" \
            --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
    else
        info "no $CONTAINER container"
    fi
}

# docker stop sends SIGTERM, and the node does not exit on SIGTERM (see
# CLAUDE.md, "Coordinator restart") -- it always rides out the full grace
# period before docker falls back to SIGKILL. docker kill sends SIGKILL
# straight away, which is the fast path and why it is its own menu entry
# rather than folded into "stop".
do_stop()  { docker stop "$CONTAINER"; }
do_kill()  { docker kill "$CONTAINER"; }
do_logs()  { docker logs -f "$CONTAINER"; }
do_remove() {
    docker rm -f "$CONTAINER" >/dev/null
    info "removed $CONTAINER (data volume kept)"
}
do_purge() {
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    if docker volume rm "$CONTAINER" >/dev/null 2>&1; then
        info "deleted the $CONTAINER volume (cluster token, roster, telemetry)"
    else
        info "no $CONTAINER volume to delete"
    fi
}

# What's actually bound to a port, host-networking style: not a docker
# concern at all, since --port/--agent-port land directly on the host. Named
# and confirmed separately from the container "kill" above because what is
# there is not necessarily this script's own leftover -- it can be anything
# on the machine, including someone else's unrelated process or another
# live derate node. This asks before it sends a signal to something it did
# not start.
free_ports() {
    command -v fuser >/dev/null 2>&1 || { info "fuser not found -- can't inspect ports"; return; }
    found=0
    for p in "$PORT" "$AGENT_PORT"; do
        pids=$(fuser "$p/tcp" 2>/dev/null) || continue
        for pid in $pids; do
            found=1
            printf '  port %s: pid %s -- %s\n' "$p" "$pid" \
                "$(ps -o user=,cmd= -p "$pid" 2>/dev/null)"
        done
    done
    [ "$found" -eq 1 ] || { info "nothing listening on $PORT or $AGENT_PORT"; return; }
    printf '\nkill everything listed above? [y/N] '
    read -r confirm || return
    case "$confirm" in
        y|Y|yes|YES) ;;
        *) info "left alone"; return ;;
    esac
    for p in "$PORT" "$AGENT_PORT"; do
        pids=$(fuser "$p/tcp" 2>/dev/null) || continue
        for pid in $pids; do
            kill -9 "$pid" 2>/dev/null && info "killed pid $pid ($p)" \
                || info "could not kill pid $pid ($p) -- gone already, or not yours"
        done
    done
}

menu() {
    while true; do
        clear 2>/dev/null || true
        printf 'derate -- local build (port %s, agent port %s)\n\n' "$PORT" "$AGENT_PORT"
        print_status
        cat <<MENU

  1) build + install/run
  2) kill + rebuild + reinstall
  3) refresh status
  4) logs (follow -- ctrl-c returns here)
  5) stop      (graceful; slow -- see comment above)
  6) kill      (SIGKILL, immediate)
  7) remove container (keeps the $CONTAINER data volume)
  8) remove container and delete its data volume
  9) free ports $PORT/$AGENT_PORT (kill whatever's listening -- asks first)
  0) quit
MENU
        printf '\n> '
        read -r choice || exit 0
        case "$choice" in
            1) build_and_install ;;
            2)
                have_container && { do_kill || true; docker rm -f "$CONTAINER" >/dev/null 2>&1 || true; }
                build_and_install
                ;;
            3) ;;
            4) have_container && do_logs || info "no $CONTAINER container" ;;
            5) have_container && do_stop || info "no $CONTAINER container" ;;
            6) have_container && do_kill || info "no $CONTAINER container" ;;
            7) have_container && do_remove || info "no $CONTAINER container" ;;
            8) do_purge ;;
            9) free_ports ;;
            0) exit 0 ;;
            *) info "not a choice: $choice" ;;
        esac
        [ "$choice" = "4" ] || { printf '\n[enter to continue] '; read -r _ || true; }
    done
}

if [ "$#" -eq 0 ]; then
    command -v docker >/dev/null 2>&1 || die "docker not found."
    menu
else
    build_and_install "$@"
fi
