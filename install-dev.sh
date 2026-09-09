#!/bin/sh
# install.sh, pointed at the dev package.
#
#   curl -fsSL https://raw.githubusercontent.com/Pizzaman213/derate/dev/install-dev.sh | sh
#
# One thing differs from the real installer, and it is which image gets pulled:
# ghcr.io/pizzaman213/derate/node-dev:latest, which .github/workflows/publish-
# image.yml builds from every push to dev, instead of .../derate/node:latest,
# which is main's and is what a stranger's `curl ... | sh` resolves. The two are
# separate GHCR packages for exactly this reason -- a tag is one typo away from
# the other, a package name is not.
#
# Every flag install.sh takes is forwarded verbatim, so joining a cluster with a
# dev image is the ordinary command with the ordinary arguments:
#
#   curl -fsSL .../install-dev.sh | sh -s -- --join http://host:8080 --token ej_...
#
# `--image` still wins if you pass it: this only supplies the default, through
# DERATE_IMAGE, and install.sh's own argument parsing runs afterwards.
#
# **Not a copy of install.sh.** That script is port checks, the Docker prompt on
# /dev/tty, the enrollment handshake, upgrade detection and uninstall; a second
# copy would be a second thing to keep in step, and the copy is always the half
# that goes stale. This finds the real one and runs it.

set -eu

# The dev package. install.sh defaults to node:latest and control_plane/
# envspec.py declares that as DERATE_IMAGE's default, which stays true: this is
# a different default for this entry point, not a change to that one.
IMAGE_DEFAULT="ghcr.io/pizzaman213/derate/node-dev:latest"

# Where the installer comes from when there is no checkout to read it from.
# dev, not main: an installer and an image that disagree about what a node is
# have no reason to work together, and this file only exists on dev anyway.
INSTALLER_URL="https://raw.githubusercontent.com/Pizzaman213/derate/dev/install.sh"

IMAGE="${DERATE_IMAGE:-$IMAGE_DEFAULT}"

info() { printf '[derate] %s\n' "$*" >&2; }
die()  { printf '[derate] %s\n' "$*" >&2; exit 1; }

# The sibling install.sh, but only when this script was run as a file. Piped
# from curl, `$0` is `sh` with no directory in it, and resolving that to `.`
# would silently prefer whatever install.sh happens to be in the working
# directory -- including one with uncommitted edits in it.
local_installer() {
    case "$0" in
        */*) ;;
        *) return 1 ;;
    esac
    dir=$(dirname -- "$0")
    [ -f "$dir/install.sh" ] || return 1
    printf '%s\n' "$dir/install.sh"
}

info "dev installer: $IMAGE"

if SIBLING=$(local_installer); then
    info "running $SIBLING from this checkout"
    DERATE_IMAGE="$IMAGE" exec sh "$SIBLING" "$@"
fi

command -v curl >/dev/null 2>&1 || die "this needs curl to fetch the installer"

# Fetched whole before a line of it runs. A connection that dies halfway
# through `curl | sh` executes the first half of a script, and the first half of
# an installer is the half that stops the running node.
BODY=$(curl -fsSL "$INSTALLER_URL") || die "could not fetch $INSTALLER_URL"
[ -n "$BODY" ] || die "$INSTALLER_URL was empty"

info "running install.sh from the dev branch"
printf '%s\n' "$BODY" | DERATE_IMAGE="$IMAGE" sh -s -- "$@"
