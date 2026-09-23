#!/usr/bin/env bash
# Produce the public history from a sanitized export (DESIGN.md 5.4).
#
# Usage: scripts/export-public.sh <destination-dir>
#
# `git archive HEAD` copies the tracked tree (nothing outside it, nothing
# from history) into a brand-new directory, which becomes a brand-new git
# repository with one commit, "Initial release v0.1.0", under the public
# identity in PUBLIC_NAME/PUBLIC_EMAIL. The private working copy this script
# runs from is never pushed anywhere: its push.default is set to "nothing"
# and every remote it has is removed. scripts/check-privacy.sh then runs
# inside the new repository and its result is printed.
#
# This script is not run as part of the test suite or CI; it is a one-time,
# owner-driven step, and the owner confirms before anything is pushed
# (DESIGN.md 11.8).

set -euo pipefail

usage() {
    echo "usage: $0 <destination-dir>" >&2
    exit 2
}

[ $# -eq 1 ] || usage
DEST="$1"

if [ -z "${PUBLIC_NAME:-}" ] || [ -z "${PUBLIC_EMAIL:-}" ]; then
    echo "PUBLIC_NAME and PUBLIC_EMAIL must both be set." >&2
    exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Resolve DEST to an absolute path against the caller's cwd, before
# anything below changes directories to ROOT_DIR. A relative DEST must stay
# relative to where this script was invoked from; otherwise a later step
# that reads "$DEST" after the cd resolves it against ROOT_DIR instead, and
# a caller-relative destination that happened not to exist yet (so the
# existence check below passed) can turn into an existing directory inside
# the repository itself, for example
# "scripts/export-public.sh scripts" run from outside the repo silently
# extracting into the repository's own scripts/ directory. Resolving through
# the parent directory (which must already exist) also normalizes any ".."
# or symlink components, so the "inside the repository" check further down
# cannot be fooled by a merely textual prefix match.
DEST_PARENT="$(dirname -- "$DEST")"
DEST_BASE="$(basename -- "$DEST")"
if ! DEST_PARENT_ABS="$(cd "$DEST_PARENT" 2>/dev/null && pwd)"; then
    echo "destination's parent directory does not exist: $DEST_PARENT" >&2
    exit 1
fi
DEST="$DEST_PARENT_ABS/$DEST_BASE"

if [ -e "$DEST" ]; then
    echo "refusing to export into an existing path: $DEST" >&2
    exit 1
fi

case "$DEST" in
    "$ROOT_DIR"|"$ROOT_DIR"/*)
        echo "refusing to export into the source repository: $DEST" >&2
        exit 1
        ;;
esac

cd "$ROOT_DIR"

# Not "mkdir -p": DEST must not already exist (checked above) and this must
# fail rather than silently reuse it if something raced to create it since.
mkdir -- "$DEST"
git archive HEAD | (cd "$DEST" && tar -xf -)

git -C "$DEST" init -b main
git -C "$DEST" config user.name "$PUBLIC_NAME"
git -C "$DEST" config user.email "$PUBLIC_EMAIL"
git -C "$DEST" add -A
git -C "$DEST" commit -m "Initial release v0.1.0"

# The private working copy this script runs from stays private: no default
# push target, and no remote left configured to push to by accident.
git config push.default nothing
for remote in $(git remote); do
    git remote remove "$remote"
done

echo "Exported to $DEST; running scripts/check-privacy.sh there:"
STATUS=0
"$DEST/scripts/check-privacy.sh" || STATUS=$?
if [ "$STATUS" -eq 0 ]; then
    echo "check-privacy.sh passed on $DEST"
else
    echo "check-privacy.sh failed on $DEST (exit $STATUS)"
fi
exit "$STATUS"
