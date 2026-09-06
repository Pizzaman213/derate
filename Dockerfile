# sparkplane/node -- one image, every machine, role resolved at runtime.
#
# There is no separate coordinator image and no separate worker image. The
# first container to come up becomes the coordinator and serves the UI on
# :8080; the rest find it over mDNS and join. Same command everywhere:
#
#     docker run --network host -v sparkplane:/data sparkplane/node
#
# Build both architectures or the heterogeneous case does not work: GB10 is
# arm64, the workstation is usually amd64. See docker/build.sh.

# ---------------------------------------------------------------------------
# Stage 1: the UI bundle. Built here so the API and the UI ship from the same
# origin -- no CORS, no second service. Skipped entirely when ui/ is absent,
# which is how this builds before Agent H lands.
# ---------------------------------------------------------------------------
FROM node:22-slim AS ui
# SKIP_UI=1 builds the image without the UI. For iterating on the container
# itself while Agent H's tree is mid-edit; a release build never sets it,
# because a UI that does not compile should fail the image, loudly.
ARG SKIP_UI=0
WORKDIR /src
# The whole context, because `COPY ui/ .` fails outright when ui/ does not
# exist yet and this image has to build before Agent H lands.
COPY . /src
RUN mkdir -p /ui-dist \
 && if [ "$SKIP_UI" = "1" ] || [ ! -f ui/package.json ]; then \
        echo '<!doctype html><meta charset=utf-8><title>sparkplane</title>' > /ui-dist/index.html ; \
        echo '<p>UI not built into this image.' >> /ui-dist/index.html ; \
    else \
        cd ui \
     && (npm ci --no-audit --no-fund || npm install --no-audit --no-fund) \
     && npm run build \
     && cp -r dist/. /ui-dist/ ; \
    fi

# ---------------------------------------------------------------------------
# Stage 2: python dependencies, into a venv we copy wholesale.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

# Pinned to the version this adapter's flags were verified against. Bump with
# --build-arg SPARKRUN_VERSION=... after re-checking control_plane/deploy/flags.py.
ARG SPARKRUN_VERSION=0.2.40

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir "sparkrun==${SPARKRUN_VERSION}"

# ---------------------------------------------------------------------------
# Stage 3: the runtime image.
# ---------------------------------------------------------------------------
FROM python:3.12-slim

LABEL org.opencontainers.image.title="sparkplane/node" \
      org.opencontainers.image.description="Spark control plane node: agent, coordinator, gateway, UI" \
      org.opencontainers.image.source="https://github.com/sparkplane/sparkplane"

# openssh-client: sparkrun drives the cluster over SSH.
# iproute2: interface inspection for the host-networking preflight and for
#           reporting ConnectX-7 topology.
# curl: the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-client \
      iproute2 \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/root

WORKDIR /opt/sparkplane
COPY control_plane/ /opt/sparkplane/control_plane/
COPY docker/ /opt/sparkplane/docker/
COPY --from=ui /ui-dist /opt/sparkplane/ui/dist
ENV PYTHONPATH=/opt/sparkplane \
    SPARKPLANE_UI_DIR=/opt/sparkplane/ui/dist

# Every one of these has a working default, so first run needs no configuration.
ENV SPARKPLANE_ROLE=auto \
    SPARKPLANE_PORT=8080 \
    SPARKPLANE_AGENT_PORT=8081 \
    SPARKPLANE_DATA=/data
# SPARKPLANE_TOKEN: unset -> the coordinator generates and persists one.
# SPARKPLANE_JOIN:  unset -> discover the coordinator over mDNS.

# Cluster token, node registry, link measurements, deployment records,
# resolved-shape cache, synthesized recipes, sparkrun job metadata.
VOLUME ["/data"]

# Documentation only -- host networking ignores published ports, and this
# container refuses to start on a bridge.
EXPOSE 8080 8081

# /agent/health exists in both roles. Never health check /api/cluster: it is
# coordinator-only and would mark every worker unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${SPARKPLANE_AGENT_PORT:-8081}/agent/health" \
   || curl -fsS "http://127.0.0.1:${SPARKPLANE_PORT:-8080}/agent/health" \
   || exit 1

ENTRYPOINT ["/opt/sparkplane/docker/entrypoint.sh"]
