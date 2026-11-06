#!/usr/bin/env bash
# The steps of .github/workflows/ci.yml on this machine, seen the way a runner sees them. Run it
# before pushing a release: 0.5.0 and 0.5.1 each failed their first CI run after every local check
# had passed.
#
# For each Python version, one at a time:
#   - a new one-commit repository holding exactly the tree of the commit, checked by tree hash;
#   - env -i, an empty HOME and CLAUDE_CONFIG_DIR, and PATH without claude;
#   - uv's own standalone build, never a Python found on this machine, and the version checked;
#     then, where Homebrew has one, Homebrew's newest, the build a macOS runner's uv finds first.
#     The two can differ: 3.14 and later limit recursion by the stack, and Homebrew's 3.14.8 parsed
#     JSON that uv's 3.14.5 refused, so only this row showed the failure of CI run 42145711237;
#   - CI's steps in CI's order. A failed step ends that version, as it ends a CI job.
# Then HOME must still be empty: a test that writes there writes to a real user's home.
#
# It cannot show Linux: this imitates CI on this machine's OS only. A Linux-only failure, as 0.5.0
# had, still first appears on the push. Read that CI run as well.
#
# It takes a few minutes per version, so run it detached, and only one runs at a time:
#   nohup scripts/ci-like.sh --out DIR > DIR/run.log 2>&1 </dev/null &
#
# Options:
#   --commit REV          what to test (default HEAD); uncommitted work is never part of it
#   --python "3.11 brew:3.14"  versions (default VERSIONS below, which a test holds equal to CI's
#                         matrix, and the Homebrew row); brew:X.Y is Homebrew's build of X.Y
#   --deadline SECONDS    limit for each step (default 1500)
#   --wait SECONDS        how long to wait for a free machine before each version (default 1800)
#   --out DIR             where transcripts and the summary go (default a new temporary directory)
#   --pytest-args "ARGS"  run part of the suite; the summary says it was only part
# Environment: CI_LIKE_MIN_FREE (percent of memory, default 40), CI_LIKE_MAX_LOAD (default 4).
#
# Exit: 0 every step passed on every version, 1 something failed, 2 usage, 3 the machine stayed
# busy or another run holds the lock.
set -u

ACTIVE_GROUP=""
ACTIVE_WATCHDOG=""
GRACE=${CI_LIKE_GRACE:-20}

# Stops the running step and what it started, at once: this run is about to delete its folder.
stop_active() {
    [ -n "$ACTIVE_WATCHDOG" ] && kill "$ACTIVE_WATCHDOG" 2>/dev/null
    if [ -n "$ACTIVE_GROUP" ]; then
        kill -TERM -"$ACTIVE_GROUP" 2>/dev/null
        sleep 1
        kill -KILL -"$ACTIVE_GROUP" 2>/dev/null
    fi
    ACTIVE_WATCHDOG=""
    ACTIVE_GROUP=""
}
trap 'stop_active; exit 130' INT
trap 'stop_active; exit 143' TERM

# Runs "$@" in a process group of its own. Past the deadline it is told to stop, then killed, and
# this returns 124 whatever the command's own exit code was. Whatever the step leaves running ends
# with it, as on a runner.
with_deadline() {
    local seconds=$1 pid rc expired="${TMPDIR:-/tmp}/ci-like-expired.$$.$RANDOM"
    shift
    set -m
    "$@" </dev/null &
    pid=$!
    set +m
    ACTIVE_GROUP=$pid
    (
        left=$seconds
        while [ "$left" -gt 0 ]; do
            sleep 1
            left=$((left - 1))
        done
        : > "$expired"
        echo "deadline of ${seconds}s passed: stopping this step"
        kill -TERM -"$pid" 2>/dev/null
        sleep "$GRACE"
        kill -KILL -"$pid" 2>/dev/null
    ) </dev/null &
    ACTIVE_WATCHDOG=$!
    wait "$pid"
    rc=$?
    kill "$ACTIVE_WATCHDOG" 2>/dev/null
    wait "$ACTIVE_WATCHDOG" 2>/dev/null
    kill -KILL -"$pid" 2>/dev/null
    ACTIVE_WATCHDOG=""
    ACTIVE_GROUP=""
    if [ -e "$expired" ]; then
        rm -f "$expired"
        return 124
    fi
    return "$rc"
}

# The tests' way in: tests/test_machine_independence.py checks with_deadline through this.
if [ "${1:-}" = "--run-with-deadline" ]; then
    shift
    seconds=$1
    shift
    with_deadline "$seconds" "$@"
    exit $?
fi

VERSIONS="3.11 3.12 3.13 3.14 3.15"
BREW_BIN=/opt/homebrew/bin
COMMIT=HEAD
DEADLINE=1500
WAIT=1800
OUT=""
PYTEST_ARGS=""
NAMED=0
MIN_FREE=${CI_LIKE_MIN_FREE:-40}
MAX_LOAD=${CI_LIKE_MAX_LOAD:-4}

usage() {
    echo "usage: $0 [--commit REV] [--python \"VERSIONS\"] [--deadline SECONDS] [--wait SECONDS]" >&2
    echo "       [--out DIR] [--pytest-args \"ARGS\"]" >&2
    exit 2
}

while [ $# -gt 0 ]; do
    [ $# -ge 2 ] || usage
    case "$1" in
        --commit) COMMIT=$2 ;;
        --python) VERSIONS=$2; NAMED=1 ;;
        --deadline) DEADLINE=$2 ;;
        --wait) WAIT=$2 ;;
        --out) OUT=$2 ;;
        --pytest-args) PYTEST_ARGS=$2 ;;
        *) usage ;;
    esac
    shift 2
done

[ -n "${VERSIONS// /}" ] || {
    echo "ci-like: no Python version to test" >&2
    exit 2
}

if [ "$NAMED" -eq 0 ]; then
    for newest in 3.15 3.14; do
        if [ -x "$BREW_BIN/python$newest" ]; then
            VERSIONS="$VERSIONS brew:$newest"
            break
        fi
    done
fi

SOURCE=$(git rev-parse --show-toplevel) || exit 2
SHA=$(git -C "$SOURCE" rev-parse --verify "$COMMIT^{commit}") || exit 2
TREE=$(git -C "$SOURCE" rev-parse "$SHA^{tree}") || exit 2
UV=$(command -v uv) || { echo "ci-like: uv is not on PATH" >&2; exit 2; }
NODE=$(command -v node) || {
    echo "ci-like: node is not on PATH: a runner has it, and the hook tests skip without it" >&2
    exit 2
}
UV_CACHE=$("$UV" cache dir) || exit 2
UV_PYTHONS=$("$UV" python dir) || exit 2

LOCK="${TMPDIR:-/tmp}/agent-postmortem-ci-like.lock"
if ! mkdir "$LOCK" 2>/dev/null; then
    holder=$(cat "$LOCK/pid" 2>/dev/null)
    if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
        echo "ci-like: another run holds the lock (pid $holder): $LOCK" >&2
    else
        echo "ci-like: a lock with no live run: $LOCK. If none is starting, remove that folder." >&2
    fi
    exit 3
fi
echo $$ > "$LOCK/pid"

WORK=$(mktemp -d "${TMPDIR:-/tmp}/ci-like.XXXXXX") || exit 2
cleanup() {
    stop_active
    rm -rf "$WORK" "$LOCK"
}
trap cleanup EXIT

[ -n "$OUT" ] || OUT=$(mktemp -d "${TMPDIR:-/tmp}/ci-like-out.XXXXXX")
mkdir -p "$OUT" || exit 2
SUMMARY="$OUT/ci-like-summary.txt"

free_percent() {
    command -v memory_pressure >/dev/null 2>&1 || return 0
    memory_pressure 2>/dev/null | awk '/free percentage/ { gsub("%", "", $NF); print $NF }'
}

load_now() {
    if command -v sysctl >/dev/null 2>&1 && sysctl -n vm.loadavg >/dev/null 2>&1; then
        sysctl -n vm.loadavg | awk '{ print $2 }'
    elif [ -r /proc/loadavg ]; then
        cut -d' ' -f1 /proc/loadavg
    fi
}

say() {
    echo "$*" | tee -a "$SUMMARY"
}

# In the main shell and never in a pipeline: bash holds a signal back until a pipeline ends, so a
# stopped run would go on holding the lock for as long as this waits.
wait_for_room() {
    local waited=0 free load said=0
    while :; do
        free=$(free_percent)
        load=$(load_now)
        if awk -v f="${free:-100}" -v l="${load:-0}" -v mf="$MIN_FREE" -v ml="$MAX_LOAD" \
            'BEGIN { exit !(f + 0 >= mf + 0 && l + 0 < ml + 0) }'; then
            say "machine ok: free ${free:-?}% load ${load:-?} at $(date '+%H:%M:%S')"
            return 0
        fi
        if [ "$said" -eq 0 ]; then
            say "waiting for room: free ${free:-?}% (need $MIN_FREE), load ${load:-?} (need under $MAX_LOAD)"
            said=1
        fi
        if [ "$waited" -ge "$WAIT" ]; then
            say "machine busy for ${WAIT}s: free ${free:-?}% (need $MIN_FREE), load ${load:-?} (need under $MAX_LOAD)"
            return 1
        fi
        sleep 10
        waited=$((waited + 10))
    done
}

# One command line, in the clone, with nothing of this machine but uv's downloads.
as_runner() {
    env -i HOME="$HOME_DIR" CLAUDE_CONFIG_DIR="$CONFIG_DIR" PATH="$RUNNER_PATH" CI=true \
        LANG=en_US.UTF-8 TMPDIR="$TMP_DIR" \
        UV_PYTHON="$version" "$PREFERENCE" \
        UV_CACHE_DIR="$UV_CACHE" UV_PYTHON_INSTALL_DIR="$UV_PYTHONS" \
        bash -c 'cd "$1" && eval "$2"' ci-like "$CLONE" "$1"
}

# Runs one step, says how it ended; returns its exit code.
step() {
    local name=$1 line=$2 started rc
    started=$(date +%s)
    echo "--- $name: $line"
    with_deadline "$DEADLINE" as_runner "$line"
    rc=$?
    echo "--- $name: exit $rc in $(($(date +%s) - started))s"
    return "$rc"
}

make_clone() {
    mkdir -p "$CLONE" || return 1
    git -C "$SOURCE" archive "$SHA" | tar -x -C "$CLONE" || return 1
    git -C "$CLONE" init -q -b main || return 1
    git -C "$CLONE" add -A || return 1
    git -C "$CLONE" -c user.name=ci-like -c user.email=ci-like@example.com \
        -c commit.gpgsign=false -c core.hooksPath=/dev/null commit -q -m "ci-like" || return 1
    [ "$(git -C "$CLONE" rev-parse 'HEAD^{tree}')" = "$TREE" ] || {
        echo "the clone's tree is not the tree of $SHA"
        return 1
    }
}

one_version() {
    version=${1#brew:}
    BASE="$WORK/${1/:/-}"  # no colon: this path goes on PATH
    PREFERENCE=UV_PYTHON_PREFERENCE=only-managed
    CLONE="$BASE/repo"
    HOME_DIR="$BASE/home"
    CONFIG_DIR="$BASE/claude-config"
    TMP_DIR="$BASE/tmp"
    BIN_DIR="$BASE/bin"
    mkdir -p "$HOME_DIR" "$CONFIG_DIR" "$TMP_DIR" "$BIN_DIR" || return 1
    ln -s "$UV" "$BIN_DIR/uv" || return 1
    if [ "$1" != "$version" ]; then
        PREFERENCE=UV_PYTHON_PREFERENCE=only-system
        ln -s "$BREW_BIN/python$version" "$BIN_DIR/python$version" || return 1
    fi
    ln -s "$NODE" "$BIN_DIR/node" || return 1
    RUNNER_PATH="$BIN_DIR:/usr/bin:/bin:/usr/sbin:/sbin"
    if PATH="$RUNNER_PATH" command -v claude >/dev/null 2>&1; then
        echo "claude is on this run's PATH: $(PATH="$RUNNER_PATH" command -v claude)"
        return 1
    fi
    make_clone || return 1
    echo "clone of $SHA, tree $TREE"

    if [ "$1" = "$version" ]; then
        step "python" "uv python install --no-bin $version" || return 1
    fi
    step "sync" "uv sync --extra dev" || return 1
    step "version" "uv run python scripts/check-python-version.py $version" || return 1
    step "lint" "uv run ruff check ." || return 1
    step "test" "uv run pytest -rs $PYTEST_ARGS" || return 1
    step "build" "uv build" || return 1
    step "privacy" "scripts/check-privacy.sh --tree --artifacts dist/" || return 1
    echo "--- plugin validation: skipped, as on a runner: claude is not on PATH"

    left=$(cd "$HOME_DIR" && find . -mindepth 1 | sed 's#^\./##'; cd "$CONFIG_DIR" && find . -mindepth 1 | sed 's#^\./#config: #')
    if [ -n "$left" ]; then
        echo "--- written to the empty home or config dir, which no step should do:"
        echo "$left"
        return 1
    fi
    echo "--- home and config dir are still empty"
}

{
    echo "ci-like $(date '+%Y-%m-%d %H:%M:%S %Z') commit $SHA tree $TREE"
    echo "versions: $VERSIONS"
    [ -z "$PYTEST_ARGS" ] || echo "PART of the suite only: $PYTEST_ARGS"
} | tee "$SUMMARY"

FAILED=0
for wanted in $VERSIONS; do
    TRANSCRIPT="$OUT/ci-like-${wanted/:/-}.txt"
    if ! wait_for_room; then
        say "ci-like: not run on $wanted"
        exit 3
    fi
    started=$(date +%s)
    one_version "$wanted" > "$TRANSCRIPT" 2>&1
    rc=$?
    running=$(grep -E '^(CPython|PyPy) ' "$TRANSCRIPT" | head -n 1)
    tally=$(grep -E '^[0-9]+ (passed|failed)|^=* ?[0-9]+ (passed|failed)' "$TRANSCRIPT" | tail -n 1)
    if [ "$rc" -eq 0 ]; then
        word=PASS
    else
        word=FAIL
        FAILED=1
    fi
    say "$word $wanted: ${running:-interpreter not reached} | ${tally:-no pytest result} | $(($(date +%s) - started))s"
    if [ "$rc" -ne 0 ]; then
        grep -E '^FAILED |^ERROR |^--- .*: exit [1-9]|^asked for|^--- written|deadline of' "$TRANSCRIPT" | cut -c1-200 | head -n 40 | tee -a "$SUMMARY"
    fi
    rm -rf "$WORK/${wanted/:/-}"
done

if [ "$FAILED" -eq 0 ]; then
    say "ci-like: PASS on every version; Linux not shown"
else
    say "ci-like: FAIL"
fi
echo "ci-like: transcripts and summary in $OUT"
exit "$FAILED"
