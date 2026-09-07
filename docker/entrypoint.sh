#!/usr/bin/env bash
# derate node entrypoint.
#
# Refuse bridge networking, prepare /data, then hand off to the node app.
# Every environment variable has a working default: first run needs no
# configuration, which is the whole pitch.
set -euo pipefail

export DERATE_ROLE="${DERATE_ROLE:-auto}"
export DERATE_PORT="${DERATE_PORT:-8080}"
export DERATE_AGENT_PORT="${DERATE_AGENT_PORT:-8081}"
export DERATE_DATA="${DERATE_DATA:-/data}"
# Python components read DERATE_DATA_DIR
export DERATE_DATA_DIR="${DERATE_DATA_DIR:-$DERATE_DATA}"
# DERATE_TOKEN unset means the coordinator generates one on first run and
# persists it under /data. DERATE_JOIN unset means discover over mDNS.

python3 /opt/derate/docker/preflight.py || exit 1

mkdir -p "$DERATE_DATA"/{deployments,recipes,telemetry}

# sparkrun keeps its job metadata under $HOME/.cache/sparkrun. Persist it in
# the volume so a container restart can still find, check and stop the
# workloads this node launched.
export HOME="${HOME:-/root}"
mkdir -p "$HOME/.cache"
if [ ! -e "$HOME/.cache/sparkrun" ]; then
    mkdir -p "$DERATE_DATA/sparkrun-cache"
    ln -s "$DERATE_DATA/sparkrun-cache" "$HOME/.cache/sparkrun"
fi

if command -v sparkrun >/dev/null 2>&1; then
    echo "[derate] sparkrun $(sparkrun --version 2>/dev/null | awk '{print $NF}')" >&2
else
    echo "[derate] WARNING: sparkrun is not on PATH. Discovery, planning and" >&2
    echo "[derate] the UI still work; launching a backend will be refused." >&2
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if [ -n "${DERATE_ENTRYPOINT:-}" ]; then
    exec python3 -m "$DERATE_ENTRYPOINT"
fi

# Agents A and G provide control_plane.node. Until then, the placeholder keeps
# the image a real artifact: it boots, it answers /agent/health, and it says
# what is missing.
if python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('control_plane.node') else 1)" 2>/dev/null; then
    exec python3 -m control_plane.node
fi

exec python3 /opt/derate/docker/placeholder_app.py
