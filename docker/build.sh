#!/usr/bin/env bash
# Multi-arch build for ghcr.io/pizzaman213/derate/node.
#
# Both architectures, always. GB10 is arm64 and the workstation is usually
# amd64; an amd64-only image means the heterogeneous case does not work at
# all, which is half the demo.
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/pizzaman213/derate/node}"
TAG="${TAG:-latest}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"
BUILDER="${BUILDER:-derate}"
SPARKRUN_VERSION="${SPARKRUN_VERSION:-0.2.40}"
# Release builds leave this at 0: a UI that does not compile must fail the image.
SKIP_UI="${SKIP_UI:-0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    cat <<'USAGE'
usage: docker/build.sh [--push | --load] [--tag TAG]

  --push   push the multi-arch manifest to the registry
  --load   build for this machine's architecture only and load it into the
           local docker daemon. buildx cannot load a multi-arch manifest.
  (neither) build both architectures and discard, i.e. verify the build

env: IMAGE, TAG, PLATFORMS, BUILDER, SPARKRUN_VERSION, SKIP_UI
USAGE
}

OUTPUT=()
while [ "$#" -gt 0 ]; do
    case "$1" in
        --push) OUTPUT=(--push); shift ;;
        --load) OUTPUT=(--load); PLATFORMS="linux/$(docker version -f '{{.Server.Arch}}')"; shift ;;
        --tag) TAG="$2"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage; exit 2 ;;
    esac
done

if ! docker buildx version >/dev/null 2>&1; then
    echo "docker buildx is required for the multi-arch build." >&2
    echo "Install the buildx plugin, or build one architecture with:" >&2
    echo "    docker build -t ${IMAGE}:${TAG} ." >&2
    exit 1
fi

if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    echo "==> creating buildx builder '${BUILDER}'"
    docker buildx create --name "$BUILDER" --driver docker-container --bootstrap
fi

# The build this image is made from, stamped in so a running node can say what
# it is. Resolved here rather than inside the Dockerfile because the build
# context does not carry .git, and a container that cannot name its own build
# is how an old image comes to read as broken hardware.
if [ -z "${DERATE_BUILD:-}" ]; then
    DERATE_BUILD="$(git rev-parse --short=12 HEAD 2>/dev/null || echo "")"
    if [ -n "$DERATE_BUILD" ] && ! git diff --quiet HEAD 2>/dev/null; then
        # Shipping a dirty tree is a real thing people do under deadline. Say
        # so in the id rather than implying the SHA is reproducible.
        DERATE_BUILD="${DERATE_BUILD}+dirty"
    fi
fi
if [ -n "$DERATE_BUILD" ]; then
    echo "==> build id ${DERATE_BUILD}"
else
    echo "==> no build id available; this image will report an unidentified build" >&2
fi

echo "==> building ${IMAGE}:${TAG} for ${PLATFORMS}"
if ! docker buildx build \
    --builder "$BUILDER" \
    --platform "$PLATFORMS" \
    --build-arg "SPARKRUN_VERSION=${SPARKRUN_VERSION}" \
    --build-arg "SKIP_UI=${SKIP_UI}" \
    --build-arg "DERATE_BUILD=${DERATE_BUILD}" \
    --tag "${IMAGE}:${TAG}" \
    "${OUTPUT[@]}" \
    "$ROOT"
then
    rc=$?
    # Building for an architecture this machine cannot execute needs QEMU
    # binfmt handlers on the host kernel. Without them the failure surfaces
    # somewhere deep in apt and reads like a broken Dockerfile, so name the
    # real cause. Not checked up front: buildx reports only the native
    # platform on some setups that cross-build perfectly well.
    if [ "$PLATFORMS" != "linux/$(docker version -f '{{.Server.Arch}}')" ]; then
        cat >&2 <<'EOF'

If that failed while installing packages for the non-native architecture,
this host has no QEMU emulation registered. Install it once (reversible
with --uninstall):

    docker run --privileged --rm tonistiigi/binfmt --install all

Both architectures are required: GB10 is arm64, the workstation is usually
amd64, and an image missing one means the heterogeneous case does not work
at all. To build only what this machine can execute while iterating:

    PLATFORMS="linux/$(docker version -f '{{.Server.Arch}}')" docker/build.sh --load
EOF
    fi
    exit "$rc"
fi

if [ "${#OUTPUT[@]}" -eq 0 ]; then
    echo "==> build verified for ${PLATFORMS} (nothing exported; pass --load or --push)"
fi
