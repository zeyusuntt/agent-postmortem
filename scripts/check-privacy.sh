#!/usr/bin/env bash
# Repository privacy audit (DESIGN.md 5.4).
#
# Runs the shared rules in tests/privacy_rules.py over the working tree
# (git ls-files), git history (`git log --all -p` and commit author fields),
# and any built dist/ artifact, then additionally greps all of it for the
# current account name. CI runs this on the tree only; the author runs it
# before every push with no flags, which covers everything.
#
# Usage: scripts/check-privacy.sh [--tree] [--history] [--artifacts DIR]
# No flags means all three. Prints one line per hit and exits non-zero if
# any rule finds something.
#
# Env:
#   PUBLIC_NAME, PUBLIC_EMAIL  the expected public commit identity; when set,
#                              --history also flags any other author name or
#                              email in `git log --all --format=%an%n%ae`.
#   CI                         when set, the account-name grep is skipped.

set -euo pipefail

usage() {
    echo "usage: $0 [--tree] [--history] [--artifacts DIR]" >&2
    exit 2
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RUN_TREE=0
RUN_HISTORY=0
RUN_ARTIFACTS=0
ARTIFACTS_DIR="dist"
ANY_FLAG=0

while [ $# -gt 0 ]; do
    case "$1" in
        --tree)
            RUN_TREE=1
            ANY_FLAG=1
            shift
            ;;
        --history)
            RUN_HISTORY=1
            ANY_FLAG=1
            shift
            ;;
        --artifacts)
            [ $# -ge 2 ] || usage
            RUN_ARTIFACTS=1
            ANY_FLAG=1
            ARTIFACTS_DIR="$2"
            shift 2
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ "$ANY_FLAG" -eq 0 ]; then
    RUN_TREE=1
    RUN_HISTORY=1
    RUN_ARTIFACTS=1
fi

# Prefer uv, which guarantees the same interpreter the tests run under;
# fall back to plain python3 with the repo root on PYTHONPATH so tests/
# resolves as an importable package (mirrors pyproject's pythonpath).
if command -v uv >/dev/null 2>&1; then
    PY_RUN=(uv run python)
else
    PY_RUN=(python3)
    export PYTHONPATH="$ROOT_DIR${PYTHONPATH:+:$PYTHONPATH}"
fi

# Only a real developer account name is worth a dedicated sweep: skip the
# generic CI runner account and skip entirely when CI is set.
ACCOUNT_NAME=""
if [ -z "${CI:-}" ]; then
    ACCOUNT_NAME="$(id -un 2>/dev/null || true)"
    if [ "$ACCOUNT_NAME" = "runner" ]; then
        ACCOUNT_NAME=""
    fi
fi

PY_ARGS=()
[ "$RUN_TREE" -eq 1 ] && PY_ARGS+=(--tree)
[ "$RUN_HISTORY" -eq 1 ] && PY_ARGS+=(--history)
if [ "$RUN_ARTIFACTS" -eq 1 ]; then
    PY_ARGS+=(--artifacts "$ARTIFACTS_DIR")
fi
[ -n "${PUBLIC_NAME:-}" ] && PY_ARGS+=(--public-name "$PUBLIC_NAME")
[ -n "${PUBLIC_EMAIL:-}" ] && PY_ARGS+=(--public-email "$PUBLIC_EMAIL")
if [ -n "$ACCOUNT_NAME" ]; then
    PY_ARGS+=(--denylist-term "$ACCOUNT_NAME")
fi

STATUS=0
if ! "${PY_RUN[@]}" -m tests.privacy_rules "${PY_ARGS[@]}"; then
    STATUS=1
fi

exit "$STATUS"
