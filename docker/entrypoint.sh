#!/usr/bin/env bash
# sparkplane node entrypoint.
#
# Refuse bridge networking, prepare /data, then hand off to the node app.
# Every environment variable has a working default: first run needs no
# configuration, which is the whole pitch.
set -euo pipefail

export SPARKPLANE_ROLE="${SPARKPLANE_ROLE:-auto}"
export SPARKPLANE_PORT="${SPARKPLANE_PORT:-8080}"
export SPARKPLANE_AGENT_PORT="${SPARKPLANE_AGENT_PORT:-8081}"
export SPARKPLANE_DATA="${SPARKPLANE_DATA:-/data}"
# Python components read SPARKPLANE_DATA_DIR
export SPARKPLANE_DATA_DIR="${SPARKPLANE_DATA_DIR:-$SPARKPLANE_DATA}"
# SPARKPLANE_TOKEN unset means the coordinator generates one on first run and
# persists it under /data. SPARKPLANE_JOIN unset means discover over mDNS.

python3 /opt/sparkplane/docker/preflight.py || exit 1

mkdir -p "$SPARKPLANE_DATA"/{deployments,recipes,telemetry}

# sparkrun keeps its job metadata under $HOME/.cache/sparkrun. Persist it in
# the volume so a container restart can still find, check and stop the
# workloads this node launched.
export HOME="${HOME:-/root}"
mkdir -p "$HOME/.cache"
if [ ! -e "$HOME/.cache/sparkrun" ]; then
    mkdir -p "$SPARKPLANE_DATA/sparkrun-cache"
    ln -s "$SPARKPLANE_DATA/sparkrun-cache" "$HOME/.cache/sparkrun"
fi

if command -v sparkrun >/dev/null 2>&1; then
    echo "[sparkplane] sparkrun $(sparkrun --version 2>/dev/null | awk '{print $NF}')" >&2
else
    echo "[sparkplane] WARNING: sparkrun is not on PATH. Discovery, planning and" >&2
    echo "[sparkplane] the UI still work; launching a backend will be refused." >&2
fi

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

if [ -n "${SPARKPLANE_ENTRYPOINT:-}" ]; then
    exec python3 -m "$SPARKPLANE_ENTRYPOINT"
fi

# Agents A and G provide control_plane.node. Until then, the placeholder keeps
# the image a real artifact: it boots, it answers /agent/health, and it says
# what is missing.
if python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('control_plane.node') else 1)" 2>/dev/null; then
    exec python3 -m control_plane.node
fi

exec python3 /opt/sparkplane/docker/placeholder_app.py
