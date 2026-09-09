# derate/node -- one image, every machine, role resolved at runtime.
#
# There is no separate coordinator image and no separate worker image. The
# first container to come up becomes the coordinator and serves the UI on
# :8080; the rest find it over mDNS and join. Same command everywhere:
#
#     docker run --network host --gpus all --pid=host -v derate:/data ghcr.io/pizzaman213/derate/node
#
# Build both architectures or the heterogeneous case does not work: GB10 is
# arm64, the workstation is usually amd64. See docker/build.sh.

# ---------------------------------------------------------------------------
# Stage 1: the UI bundle. Built here so the API and the UI ship from the same
# origin -- no CORS, no second service. Skipped entirely when ui/ is absent,
# which is how this builds before Agent H lands.
# ---------------------------------------------------------------------------
# --platform=$BUILDPLATFORM: the bundle is static files, identical for every
# target arch, so it is built once natively instead of once per platform --
# and the emulated half of a multi-arch build never runs npm at all.
FROM --platform=$BUILDPLATFORM node:22-slim AS ui
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
        echo '<!doctype html><meta charset=utf-8><title>derate</title>' > /ui-dist/index.html ; \
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

# openssh-client: sparkrun drives the cluster over SSH.
# git: sparkrun's recipe registry is a git clone and its RegistryManager shells
#      out to the binary -- ensure_initialized() -> update() -> _clone_or_pull()
#      -> subprocess.run(["git", ...]). Without it every launch from inside this
#      image dies with FileNotFoundError: 'git' before it has read a recipe,
#      which is a traceback about subprocess rather than about a missing
#      package. It is only invisible when derate runs from a checkout, where
#      the developer's machine has git and the registry is already cloned.
# iproute2: interface inspection for the host-networking preflight and for
#           reporting ConnectX-7 topology.
# curl: the container healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      openssh-client \
      git \
      iproute2 \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# The docker CLI -- the client only, never the daemon. sparkrun starts the
# runtime container by running `docker` itself: containers/registry.py asks
# `docker image inspect` whether the image is here before it pulls, and
# orchestration/executor_docker.py builds the `docker run` for the model. A
# node without this binary clears every gate, plans, writes a recipe, and dies
# in subprocess with FileNotFoundError: 'docker' at [3/6] Distributing
# resources -- which is what happened on every launch from a containerized node
# until this layer existed.
#
# The client talks to the HOST's daemon over the socket install.sh mounts, so
# the container it starts is a sibling on the host, not a child here. Nothing
# in this image runs containers itself.
#
# Debian's docker.io is the wrong package for that: 32 MB down, 128 MB
# installed, and most of it is the daemon and containerd, which this image must
# never start. The upstream static bundle is one binary, extracted alone --
# 40 MB on disk, ~18 MB in the layer.
#
# Pinned, and pinned to what this fleet's daemons run (29.2.1). The CLI
# negotiates the API version down to whatever daemon answers, so a newer client
# would work; a pin that matches is one less thing to be surprised by, and an
# unpinned URL would make the image un-rebuildable.
ARG DOCKER_CLI_VERSION=29.2.1
ARG TARGETARCH
RUN set -eux; \
    case "$TARGETARCH" in \
      amd64) arch=x86_64 ;; \
      arm64) arch=aarch64 ;; \
      *) echo "no static docker CLI is published for $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/$arch/docker-${DOCKER_CLI_VERSION}.tgz" \
      | tar -xzf - -C /usr/local/bin --strip-components=1 docker/docker; \
    docker --version

COPY --from=deps /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/root

WORKDIR /opt/derate
COPY control_plane/ /opt/derate/control_plane/
COPY docker/ /opt/derate/docker/
# The coordinator serves these exact bytes at GET /install.sh, so every node
# after the first installs from the cluster it is joining rather than from the
# internet. Same file the repo's raw URL hands the first node.
COPY install.sh /opt/derate/install.sh
COPY --from=ui /ui-dist /opt/derate/ui/dist
ENV PYTHONPATH=/opt/derate \
    DERATE_UI_DIR=/opt/derate/ui/dist

# What the NVIDIA container runtime reads when it is the default runtime, so
# that a host configured that way needs no --gpus on the command line. It is
# inert everywhere else.
#
# `utility` and not `compute`: that capability set is nvidia-smi and NVML and
# nothing more, which is the whole of what this image does with a GPU. It
# probes and it samples; it never runs a model. Asking for the CUDA runtime
# would inject libraries nothing here links against.
ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=utility

# Every one of these has a working default, so first run needs no configuration.
ENV DERATE_ROLE=auto \
    DERATE_PORT=8080 \
    DERATE_AGENT_PORT=8081 \
    DERATE_DATA=/data
# DERATE_TOKEN: unset -> the coordinator generates and persists one.
# DERATE_JOIN:  unset -> discover the coordinator over mDNS.

# Cluster token, node registry, link measurements, deployment records,
# resolved-shape cache, synthesized recipes, sparkrun job metadata.
VOLUME ["/data"]

# Documentation only -- host networking ignores published ports, and this
# container refuses to start on a bridge.
EXPOSE 8080 8081

# /agent/health exists in both roles. Never health check /api/cluster: it is
# coordinator-only and would mark every worker unhealthy.
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${DERATE_AGENT_PORT:-8081}/agent/health" \
   || curl -fsS "http://127.0.0.1:${DERATE_PORT:-8080}/agent/health" \
   || exit 1

# The build this image was made from, and the LAST thing this stage does.
#
# Position is load-bearing rather than tidy. The value changes on every commit,
# and BuildKit chains cache keys through the image config, so an ENV set before
# the apt layer and the docker CLI download would miss both on every build --
# 40 MB re-fetched through QEMU for arm64 to record a twelve-character string.
# At the end it invalidates nothing but itself.
#
# Empty on a plain `docker build`, which is honest: control_plane/version.py
# renders that as "an unidentified build" rather than inventing one.
# docker/build.sh passes it, and so does .github/workflows/publish-image.yml --
# .dockerignore drops .git, so an unstamped image has no second source to fall
# back to and every published node reads as unidentified.
ARG DERATE_BUILD=""

LABEL org.opencontainers.image.title="derate/node" \
      org.opencontainers.image.description="Derate node: agent, coordinator, gateway, UI" \
      org.opencontainers.image.source="https://github.com/Pizzaman213/derate" \
      org.opencontainers.image.revision="${DERATE_BUILD}"

# Read by control_plane.version.build_id() and reported on /agent/profile and
# /agent/health, so a node running an old image can be told apart from a node
# whose hardware genuinely cannot be identified. Those two used to render
# identically, which is how a working Raspberry Pi came to read as a fault.
ENV DERATE_BUILD="${DERATE_BUILD}"

ENTRYPOINT ["/opt/derate/docker/entrypoint.sh"]
