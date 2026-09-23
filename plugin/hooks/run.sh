#!/usr/bin/env bash
# agent-postmortem plugin hook entry point (docs/DESIGN.md 7.2).
#
# Invoked by plugin/hooks/hooks.json as:
#   "${CLAUDE_PLUGIN_ROOT}/hooks/run.sh" <event>
#
# Resolves a compatible `agent-postmortem` CLI in this order:
#   1. $DATA_DIR/bin/agent-postmortem, if executable ($DATA_DIR: see below).
#   2. `agent-postmortem` on PATH, only if `agent-postmortem hook
#      capabilities` prints a single, well-formed {"protocol": 1, ...}
#      object within 2 seconds.
#   3. Otherwise, print a "CLI not found" systemMessage at most once a day
#      and exit 0.
# The original stdin (the hook JSON payload) reaches whichever CLI is
# resolved untouched -- byte for byte, trailing newline included -- since
# it is never captured into a shell variable along the way (docs/
# DESIGN.md 7.2's D10). The hook never fails a turn: a non-zero exit or
# malformed (non-JSON-object) output from the CLI is swallowed into a
# silent exit 0, and every code path below ends with `exit 0`.
#
# set -u catches a typo'd variable; set -e is deliberately NOT used, since
# most of the control flow here depends on commands (command -v, the CLI
# itself, uv) that are *expected* to fail along some paths.
set -u

EVENT="${1:-}"

# docs/DESIGN.md 7.2's D6: nothing below may reach Claude Code's own
# stderr unless a person opted in. Most individual writes already carry
# their own `2>/dev/null`, but a redirection target itself failing to open
# (an unwritable/missing directory) prints to whatever stderr already is
# *before* a same-command `2>/dev/null` can apply -- this blanket
# redirection, applied before any file is ever written, is what actually
# makes that silent.
if [ -z "${AGENT_POSTMORTEM_DEBUG:-}" ]; then
    exec 2>/dev/null
fi

# hooks/run.sh -> plugin root is one directory up.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
PLUGIN_JSON="$PLUGIN_ROOT/.claude-plugin/plugin.json"

# docs/DESIGN.md 7.2's D9: one data directory, derived once, used for
# resolution step (1), the install, the daily-gate markers, and the
# install log -- $CLAUDE_PLUGIN_DATA when set, else the XDG state
# directory. Never /tmp: a fallback there would be shared by every
# unrelated tool on the machine and would not survive a reboot on most
# distros, silently re-arming the "run the install/print the hint" gates
# far more often than once a day.
if [ -n "${CLAUDE_PLUGIN_DATA:-}" ]; then
    DATA_DIR="$CLAUDE_PLUGIN_DATA"
else
    DATA_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agent-postmortem/plugin"
fi

_tmpfile() {
    mktemp 2>/dev/null || printf '%s' "${DATA_DIR}/.run.$$.$RANDOM"
}

# docs/DESIGN.md 7.2's D6: mkdir -p DATA_DIR, gating every later write on
# its own success -- an unwritable/missing DATA_DIR means no marker, no
# log, no install, and no hint: run.sh prints nothing and exits 0.
_ensure_data_dir() {
    mkdir -p "$DATA_DIR" 2>/dev/null
}

# --- deadlines: kill the whole process group, not just one pid -------------
#
# docs/DESIGN.md 7.2's D3/D4: every real subprocess (the capabilities
# probe, the real hook call, the one-time install) runs under an internal
# deadline here, shorter than the outer hook timeout Claude Code itself
# enforces, so a hang inside agent-postmortem (or uv) ends in *this*
# script's own silent exit 0 instead of a hook-timeout error surfaced to
# the person. A deadline kill targets the command's whole process group
# (`setsid` when available makes the launched command its own session/
# group leader outright; otherwise a `set -m` subshell does the same for
# its own backgrounded job), plus a `pkill -P` sweep of direct children by
# parent pid as a second net -- killing only the one pid this script
# itself launched leaves any descendant (for example a CLI that shells
# out and backgrounds a slow subprocess of its own) running as an orphan
# after run.sh has already exited.

_run_with_deadline() {
    deadline_seconds="$1"
    shift
    # Bash job control (`set -m`, needed below on a platform with no
    # setsid) silently redirects a *backgrounded* command's stdin to
    # /dev/null on its own -- not something a plain '&' does outside job
    # control, but very much something this function's own use of it
    # would do by accident. Save whatever fd 0 currently is (the real hook
    # payload for the main call; /dev/null, explicitly, for the probe and
    # the install) on fd 8 so it can be reattached explicitly below,
    # regardless of that job-control side effect (docs/DESIGN.md 7.2's
    # D10: exact bytes, unmodified, must still reach the launched command).
    exec 8<&0
    pid_file="$(_tmpfile)"
    status_file="$(_tmpfile)"

    # This whole wrapper runs as one background job so the deadline
    # watcher below and the main script can each proceed independently of
    # it; within it, the real command is itself backgrounded (via setsid
    # or set -m) so it gets a process group of its own, distinct from the
    # wrapper's.
    (
        if command -v setsid >/dev/null 2>&1; then
            setsid "$@" 0<&8 &
        else
            set -m
            "$@" 0<&8 &
        fi
        child_pid=$!
        printf '%s' "$child_pid" >"$pid_file" 2>/dev/null
        wait "$child_pid" 2>/dev/null
        printf '%s' "$?" >"$status_file" 2>/dev/null
    ) &
    wrapper_pid=$!
    exec 8<&-

    # The watcher's own stdout/stderr/stdin are explicitly severed from
    # whatever run.sh's are: left connected to run.sh's real stdout (the
    # hook's output pipe) or stdin (the hook payload), an orphaned sleep,
    # or a `wait` that never returns, would hold that pipe open and the
    # process reading it (Claude Code, or a test's subprocess.run) would
    # block long after run.sh itself has exited.
    (
        sleep "$deadline_seconds"
        child_pid="$(cat "$pid_file" 2>/dev/null)"
        if [ -n "$child_pid" ]; then
            pgid="$(ps -o pgid= -p "$child_pid" 2>/dev/null | tr -d '[:space:]')"
            if [ -n "$pgid" ]; then
                kill -9 -- "-$pgid" 2>/dev/null
            fi
            pkill -9 -P "$child_pid" 2>/dev/null
            kill -9 "$child_pid" 2>/dev/null
        fi
        kill -9 "$wrapper_pid" 2>/dev/null
    ) </dev/null >/dev/null 2>&1 &
    watcher_pid=$!

    wait "$wrapper_pid" 2>/dev/null
    status="$(cat "$status_file" 2>/dev/null)"

    # Cleanup, from a trap-like sequence run unconditionally here (not an
    # actual `trap`, since this function can be called more than once per
    # script invocation and each call's cleanup is local to it): stop the
    # watcher's own sleep so it never outlives this function, then remove
    # the temp files.
    kill "$watcher_pid" 2>/dev/null
    wait "$watcher_pid" 2>/dev/null
    rm -f "$pid_file" "$status_file" 2>/dev/null

    if [ -z "$status" ]; then
        status=137  # killed before it recorded its own real exit status
    fi
    return "$status"
}

_is_executable_file() {
    [ -n "${1:-}" ] && [ -f "$1" ] && [ -x "$1" ]
}

# --- JSON validation: python3 when available, a strict regex otherwise -----
#
# docs/DESIGN.md 7.2's D2: the CLI always emits either nothing or a single
# json.dumps(...) object, never a bare array/string/number and never more
# than one value; this rejects a stack trace, garbage, or two concatenated
# objects without requiring python3 (jq is not assumed to exist either).

# Presence on PATH is not enough: macOS ships a /usr/bin/python3 stub that
# refuses to run at all (exit 69) until the Xcode license is accepted, and
# a restricted PATH (this script's own tests; some CI images) can resolve
# exactly that stub with nothing real behind it. Actually try running it,
# once per script invocation (cached; both the probe and the main call
# ask this).
_python3_usable=""
_have_python3() {
    if [ -z "$_python3_usable" ]; then
        if command -v python3 >/dev/null 2>&1 && printf '' | python3 -c 'import json, sys' >/dev/null 2>&1; then
            _python3_usable=yes
        else
            _python3_usable=no
        fi
    fi
    [ "$_python3_usable" = yes ]
}

# True (exit 0) iff $1 is exactly one JSON object -- no trailing data, no
# second object concatenated after it.
_looks_like_json_object() {
    if _have_python3; then
        printf '%s' "$1" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
sys.exit(0 if isinstance(data, dict) else 1)
' 2>/dev/null
        py_status=$?
        case "$py_status" in
            0) return 0 ;;
            1) return 1 ;;
        esac
        # python3 itself misbehaved (crashed, wrong exit code) rather than
        # validly answering yes/no -- fall through to the regex path
        # rather than trusting a meaningless status.
    fi
    _looks_like_json_object_regex "$1"
}

_looks_like_json_object_regex() {
    trimmed="$(printf '%s' "$1" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "$trimmed" in
        \{*\}) ;;
        *) return 1 ;;
    esac
    # A crude but effective concatenated-object guard: a single well-formed
    # object never has a '}' followed later by a '{' (nesting only ever
    # opens *before* it closes going outside-in); "{}{}" or "{}garbage{"
    # both get caught here even though the plain \{*\} glob alone would
    # accept them.
    case "$trimmed" in
        *"}"*"{"*) return 1 ;;
    esac
    return 0
}

# True (exit 0) iff $1 is exactly one JSON object whose top-level
# "protocol" field is the integer 1 (not 1.5, not "1", not true).
_looks_like_capabilities_v1() {
    if _have_python3; then
        printf '%s' "$1" | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(1)
protocol = data.get("protocol") if isinstance(data, dict) else None
ok = isinstance(data, dict) and type(protocol) is int and protocol == 1
sys.exit(0 if ok else 1)
' 2>/dev/null
        py_status=$?
        case "$py_status" in
            0) return 0 ;;
            1) return 1 ;;
        esac
        # As in _looks_like_json_object: an unexpected exit status means
        # python3 itself misbehaved, not that it validly said no.
    fi
    _looks_like_json_object_regex "$1" || return 1
    printf '%s' "$1" | grep -Eq '"protocol"[[:space:]]*:[[:space:]]*1([^0-9.]|$)'
}

# True (exit 0) iff `agent-postmortem hook capabilities` is on PATH and
# prints a well-formed {"protocol": 1, ...} within 2 seconds. Reads from
# /dev/null (docs/DESIGN.md 7.2's D10: a capability probe must never
# consume the real hook payload run.sh still needs to pass through).
_probe_path_cli() {
    command -v agent-postmortem >/dev/null 2>&1 || return 1
    out_file="$(_tmpfile)"
    _run_with_deadline 2 agent-postmortem hook capabilities >"$out_file" 2>/dev/null </dev/null
    probe_output="$(cat "$out_file" 2>/dev/null)"
    rm -f "$out_file" 2>/dev/null
    _looks_like_capabilities_v1 "$probe_output"
}

# Prints the resolved CLI (a path or "agent-postmortem") on stdout and
# returns 0, or prints nothing and returns 1 when no compatible CLI
# resolves.
_resolve_cli() {
    if _is_executable_file "$DATA_DIR/bin/agent-postmortem"; then
        printf '%s' "$DATA_DIR/bin/agent-postmortem"
        return 0
    fi
    if _probe_path_cli; then
        printf '%s' "agent-postmortem"
        return 0
    fi
    return 1
}

# True iff $1 is a file that was modified in the last 24 hours (`find
# -mtime -1` is the portable BSD/GNU way to ask that; it avoids a `stat`
# whose flags differ between macOS and Linux).
_modified_within_a_day() {
    [ -f "$1" ] && [ -n "$(find "$1" -mtime -1 -print -quit 2>/dev/null)" ]
}

# docs/DESIGN.md 7.2's D5: an atomic, non-blocking lock via `mkdir` (which
# either creates the directory or fails, atomically, even across
# processes sharing a filesystem) so two hooks racing the same
# once-a-day gate can never both see it as due and both act on it. A
# process that loses the race simply skips acting this one time -- there
# is always a later hook invocation to try again, and blocking/retrying
# here would risk this hook itself hanging.
_LOCKS_HELD=""

_try_lock() {
    if mkdir "$1" 2>/dev/null; then
        _LOCKS_HELD="$_LOCKS_HELD $1"
        return 0
    fi
    return 1
}

_release_locks() {
    for lock_dir in $_LOCKS_HELD; do
        rmdir "$lock_dir" 2>/dev/null
    done
    _LOCKS_HELD=""
}
trap _release_locks EXIT

# docs/DESIGN.md 7.3/7.4: at most once a day, tell the user how to install
# the CLI, via a marker file under $DATA_DIR. Silent (no hint, no marker
# write attempted) when $DATA_DIR itself cannot be created -- docs/
# DESIGN.md 7.2's D6.
_print_missing_cli_hint_if_due() {
    _ensure_data_dir || return 0
    _try_lock "$DATA_DIR/cli-missing-hint.lock" || return 0
    marker="$DATA_DIR/cli-missing-hint"
    if _modified_within_a_day "$marker"; then
        return 0
    fi
    printf '{"systemMessage": "agent-postmortem CLI not found; run: uv tool install agent-postmortem"}\n'
    : >"$marker" 2>/dev/null
}

_plugin_version() {
    # A crude, dependency-free extraction of "version": "X" from
    # plugin.json; avoids requiring jq or python3 just to read one field.
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$PLUGIN_JSON" 2>/dev/null | head -n 1
}

# docs/DESIGN.md 7.3: the plugin's only network action. SessionStart only,
# and only when no compatible CLI resolved above.
_maybe_install() {
    [ "$EVENT" = "session-start" ] || return 0
    [ -z "${AGENT_POSTMORTEM_NO_AUTO_INSTALL:-}" ] || return 0
    command -v uv >/dev/null 2>&1 || return 0
    _ensure_data_dir || return 0
    _try_lock "$DATA_DIR/install.lock" || return 0

    install_log="$DATA_DIR/install.log"
    if _modified_within_a_day "$install_log"; then
        return 0
    fi

    pinned_version="$(_plugin_version)"
    [ -n "$pinned_version" ] || return 0

    {
        printf '%s attempting: uv tool install agent-postmortem==%s\n' \
            "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$pinned_version"
    } >>"$install_log" 2>/dev/null

    mkdir -p "$DATA_DIR/bin" 2>/dev/null
    UV_TOOL_DIR="$DATA_DIR" UV_TOOL_BIN_DIR="$DATA_DIR/bin" \
        _run_with_deadline 60 uv tool install "agent-postmortem==$pinned_version" --python 3.11+ \
        </dev/null >>"$install_log" 2>&1
}

# ---- main -----------------------------------------------------------------

CLI="$(_resolve_cli)"

if [ -z "$CLI" ]; then
    _maybe_install
    CLI="$(_resolve_cli)"
fi

if [ -z "$CLI" ]; then
    _print_missing_cli_hint_if_due
    exit 0
fi

# docs/DESIGN.md 7.2's D3: an internal deadline shorter than this event's
# own outer hook timeout (hooks.json: check/workflow-launched 10s,
# session-end 20s, session-start 90s), so a hang inside the CLI ends here,
# silently, rather than as a hook-timeout Claude Code itself reports.
case "$EVENT" in
    session-end) HOOK_DEADLINE=18 ;;
    session-start) HOOK_DEADLINE=80 ;;
    *) HOOK_DEADLINE=8 ;;
esac

# docs/DESIGN.md 12's D-B: a test-only override. Never read by a real
# invocation of the plugin (nothing sets this env var outside the test
# suite) -- it exists solely so a test can exercise the real deadline-kill
# path against a fake CLI that sleeps a couple of seconds past a 1s
# deadline, instead of waiting out the real 8/18/80s default.
if [ -n "${AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS:-}" ]; then
    HOOK_DEADLINE="$AGENT_POSTMORTEM_HOOK_DEADLINE_SECONDS"
fi

out_file="$(_tmpfile)"
# No stdin redirection here at all: this inherits run.sh's own stdin
# exactly as Claude Code gave it -- byte for byte, trailing newline
# included (docs/DESIGN.md 7.2's D10) -- all the way through
# _run_with_deadline's setsid/set-m backgrounding, neither of which
# touches fd 0 for the command they launch.
_run_with_deadline "$HOOK_DEADLINE" "$CLI" hook "$EVENT" >"$out_file" 2>/dev/null
cli_status=$?
cli_output="$(cat "$out_file" 2>/dev/null)"
rm -f "$out_file" 2>/dev/null

if [ "$cli_status" -eq 0 ] && [ -n "$cli_output" ] && _looks_like_json_object "$cli_output"; then
    printf '%s\n' "$cli_output"
fi

exit 0
