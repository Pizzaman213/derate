#!/usr/bin/env bash
# Attach UI screenshots to a pull request.
#
#   .github/scripts/pr_screens.sh <pr-number> <image.png> [<image.png> ...]
#
# The images are committed to the orphan branch `pr-screens` under <pr-number>/,
# never to the PR's own branch, so merging the PR does not carry binaries into
# dev. Each is linked by commit SHA (a pinned URL that later pushes cannot
# change) and appended to the PR description as a "Screenshots" section.
# Re-running for the same PR replaces that section rather than adding another.
set -euo pipefail

if [ "$#" -lt 2 ]; then
    sed -n '2,10p' "$0" >&2
    exit 2
fi
PR="$1"; shift
case "$PR" in *[!0-9]*|'') echo "pr-number must be a number, got '$PR'" >&2; exit 2 ;; esac
for f in "$@"; do
    [ -f "$f" ] || { echo "no such file: $f" >&2; exit 2; }
done

REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner)"
BRANCH=pr-screens
WORK="$(mktemp -d)"
trap 'git worktree remove --force "$WORK" >/dev/null 2>&1 || true; rm -rf "$WORK"' EXIT

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch -q origin "$BRANCH"
    git worktree add -q --detach "$WORK" "origin/$BRANCH"
else
    git worktree add -q --detach "$WORK"
    git -C "$WORK" checkout -q --orphan "$BRANCH"
    git -C "$WORK" rm -rq --cached . >/dev/null
    find "$WORK" -mindepth 1 -maxdepth 1 ! -name .git -exec rm -rf {} +
    printf 'Screenshots attached to pull requests. Not part of the product.\n' > "$WORK/README.md"
    git -C "$WORK" add README.md
fi

mkdir -p "$WORK/$PR"
for f in "$@"; do cp "$f" "$WORK/$PR/"; done
git -C "$WORK" add "$PR"
git -C "$WORK" commit -qm "screens for #$PR" || true
git -C "$WORK" push -q origin "HEAD:refs/heads/$BRANCH"
SHA="$(git -C "$WORK" rev-parse HEAD)"

SECTION="$(mktemp)"
{
    echo '<!-- pr-screens -->'
    echo '## Screenshots'
    for f in "$@"; do
        name="$(basename "$f")"
        echo
        echo "**${name%.*}**"
        echo
        echo "![${name%.*}](https://github.com/$REPO/blob/$SHA/$PR/$name?raw=true)"
    done
    echo '<!-- /pr-screens -->'
} > "$SECTION"

BODY="$(gh pr view "$PR" --json body -q .body)"
CLEAN="$(printf '%s\n' "$BODY" | sed '/<!-- pr-screens -->/,/<!-- \/pr-screens -->/d')"
printf '%s\n\n%s\n' "$CLEAN" "$(cat "$SECTION")" | gh pr edit "$PR" --body-file - >/dev/null
rm -f "$SECTION"
echo "attached $# image(s) to #$PR at $SHA"
