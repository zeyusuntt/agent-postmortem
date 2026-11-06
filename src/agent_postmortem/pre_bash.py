"""`hook pre-bash` (docs/DESIGN.md 4.14.5): before a Bash call runs, warn when it starts
`codex exec` in the background with no stdin redirect.

codex exec reads stdin to its end when stdin is not a terminal. Under the Bash tool that is
usually harmless: over 30 days on the reference machine, 276 foreground and 51 of 60 background
runs with no redirect finished. A background one hung 26 h on 2026-11-02, its log holding only
"Reading additional input from stdin...". So this is a warning added beside the call
(`additionalContext`), never a deny: the call runs, and the agent sees the warning with the
launch result and can stop and rerun it.

PreToolUse fires on every Bash call, so this module answers almost every call from the payload
alone: no "codex" in the command, no import past `json`.
"""

from __future__ import annotations

import json
import re
import sys

_STDIN_OPS = frozenset({"<", "<<", "<<-", "<<<", "<>", "<&"})  # `0<&3` and `0<&-` too

WARNING = (
    "STDIN this background `codex exec` has no stdin redirect. codex reads stdin to its end "
    "when it is not a terminal, and in the background an open stdin can hold it forever (one "
    "review hung 26 h, its log only \"Reading additional input from stdin...\"). Rerun it with "
    "</dev/null and a timeout: TaskStop the task this call starts if it is already running."
)

_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
# the prefixes that run the command after them, each with its options that take a value
# (`env -u X`, `env -iu X`, `time -p`, `command --`, `timeout -s TERM
# 900` hid the command)
_WRAPPERS = {
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nice": frozenset({"-n", "--adjustment"}),
    "timeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "gtimeout": frozenset({"-k", "--kill-after", "-s", "--signal"}),
    "time": frozenset({"-o", "--output", "-f", "--format"}),
    "command": frozenset(),
    "exec": frozenset({"-a"}),
    "nohup": frozenset(),
    "builtin": frozenset(),
}
# codex's own options that take a value, before its subcommand
_CODEX_VALUE_OPTIONS = frozenset({"-c", "--config", "-m", "--model", "-p", "--profile", "-s",
                                  "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "-i",
                                  "--image", "--enable", "--disable", "--local-provider",
                                  "--add-dir"})


def _command_words(words: list[str]) -> list[str] | None:
    """A segment's words from its command word on, after the prefixes that run it unchanged
    (assignments, the wrappers above with their options, clustered or not, and `--`, the
    duration of `timeout`, `mise exec|x ... --`); `env -S STRING` gives STRING's words. None
    when the segment runs no command (`command -v`, `command -V`: a lookup)."""
    i, n = 0, len(words)
    while i < n:
        word = words[i]
        if _ASSIGN_RE.match(word):
            i += 1
            continue
        if word == "mise" and i + 1 < n and words[i + 1] in ("exec", "x") and "--" in words[i:]:
            i = words.index("--", i) + 1
            continue
        takes = _WRAPPERS.get(word)
        if takes is None:
            break
        i += 1
        while i < n and words[i].startswith("-") and words[i] != "-":
            option = words[i]
            i += 1
            if option == "--":
                break
            name, eq, value = option.partition("=")
            if option.startswith("--"):
                if name in takes and not eq:
                    value = words[i] if i < n else ""
                    i += 1
                if word == "env" and name == "--split-string":
                    return _command_words(_split(value) + words[i:])
                continue
            if word == "command" and set(option[1:]) & {"v", "V"}:
                return None  # a lookup, which runs nothing
            for at, letter in enumerate(option[1:], start=1):
                if f"-{letter}" in takes:
                    value = option[at + 1:]
                    if not value:
                        value = words[i] if i < n else ""
                        i += 1
                    if word == "env" and letter == "S":
                        return _command_words(_split(value) + words[i:])
                    break
        if word in ("timeout", "gtimeout"):
            i += 1  # the duration
    return words[i:]


def _split(text: str) -> list[str]:
    """`env -S STRING`'s words, by its own quoting rules."""
    import shlex

    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _without_substitutions(command: str) -> str:
    """The command with every `$(...)` and backtick span (balanced, quote-aware) replaced by a
    plain word, so a redirect inside one is never read as the outer command's
    (`codex exec $(cat </dev/null )`); literal text, a here-string's `)` included, stays."""
    out: list[str] = []
    i, n = 0, len(command)
    single = double = False
    while i < n:
        ch = command[i]
        if single:
            single = ch != "'"
        elif ch == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        elif ch == "'" and not double:
            single = True
        elif ch == '"':
            double = not double
        elif ch == "`" or (ch == "$" and command[i + 1:i + 2] == "("):
            end = _substitution_end(command, i)
            out.append("SUBSTITUTION")
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _substitution_end(command: str, start: int) -> int:
    """The index just past the substitution opening at `start` (its end of text if it never
    closes)."""
    n = len(command)
    if command[start] == "`":
        i = start + 1
        while i < n and command[i] != "`":
            i += 2 if command[i] == "\\" else 1
        return min(n, i + 1)
    depth, i = 0, start + 1
    single = double = False
    while i < n:
        ch = command[i]
        if single:
            single = ch != "'"
        elif ch == "\\":
            i += 2
            continue
        elif ch == "'" and not double:
            single = True
        elif ch == '"':
            double = not double
        elif not double and ch == "(":
            depth += 1
        elif not double and ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _runs_codex_exec(words: list[str]) -> bool:
    """`codex exec` or `codex e`, after codex's own options and their values."""
    if not words or words[0].rsplit("/", 1)[-1] != "codex":
        return False
    i = 1
    while i < len(words) and words[i].startswith("-"):
        option = words[i]
        i += 2 if option in _CODEX_VALUE_OPTIONS else 1
    return i < len(words) and words[i] in ("exec", "e")


# Words before a command word in a segment of a conditional or a loop.
_CONTROL_WORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!"})
_OPENERS = frozenset({"if", "while", "until", "for"})


def _compounds(segments) -> tuple[set[int], set[int]]:
    """The segments inside a conditional or loop, and those of them whose stdin is given to it as a
    whole, by a redirect on its `fi` or `done` or by a pipe into it."""
    inside: set[int] = set()
    fed: set[int] = set()
    open_at: list[tuple[int, bool]] = []
    for index, seg in enumerate(segments):
        for word in (word.text for word in seg.words):
            if word in _OPENERS:
                piped = index > 0 and segments[index - 1].sep_after in ("|", "|&")
                open_at.append((index, piped))
            elif word not in _CONTROL_WORDS:
                break
        head = seg.words[0].text if seg.words else ""
        if head in ("fi", "done") and open_at:
            start, piped = open_at.pop()
            inside.update(range(start, index + 1))
            if piped or any(_sets_stdin(r) for r in seg.redirects):
                fed.update(range(start, index + 1))
    return inside, fed


def _sets_stdin(redirect) -> bool:
    """A redirect that gives fd 0 something other than the caller's stdin: `<`, a heredoc, a
    here-string, `<&`, or any form naming fd 0 (`0>&-`)."""
    return (redirect.op in _STDIN_OPS and redirect.fd in (None, "0")) or redirect.fd == "0"


def unredirected_codex(command: str) -> bool:
    """Whether a segment of this command runs `codex exec` (or `codex e`) as its command and
    reads the caller's stdin: no stdin redirect on that segment (one inside a substitution
    does not count), none on a commandless `exec` before it, and not fed by a pipe.
    A command with a `{ ...; }` group or a subshell is not judged: its stdin may come from the
    group."""
    from . import commands  # only for a command that names codex

    segments = commands.lex(_without_substitutions(command)).segments
    if any(seg.words and (seg.words[0].text in ("{", "}")
                          or any(word.raw.startswith("(") for word in seg.words))
           for seg in segments):
        return False
    inside, fed = _compounds(segments)
    previous_sep = None
    shell_stdin = False  # `exec </dev/null` changed the shell's own stdin
    for index, seg in enumerate(segments):
        words = [word.text for word in seg.words]
        # only an `exec` that surely runs in this shell: the start of a list, not piped, not
        # backgrounded, not after `&&` or `||`
        if words == ["exec"] and index not in inside \
                and any(_sets_stdin(r) for r in seg.redirects) \
                and previous_sep in (None, ";", "\n") and seg.sep_after not in ("|", "|&", "&"):
            shell_stdin = True
        while words and words[0] in _CONTROL_WORDS:
            words = words[1:]
        command_words = _command_words(words)
        if command_words is not None and _runs_codex_exec(command_words):
            redirected = index in fed or any(_sets_stdin(r) for r in seg.redirects)
            if not (redirected or shell_stdin) and previous_sep not in ("|", "|&"):
                return True
        previous_sep = seg.sep_after
    return False


def run() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "null")
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PreToolUse":
            return 0
        if payload.get("tool_name") != "Bash":
            return 0  # a subagent's own call too: the warning is about the caller's own call
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, dict):
            return 0
        command = tool_input.get("command")
        if not isinstance(command, str) or "codex" not in (
                command.replace("\\\n", "").replace('"', "").replace("'", "")
                .replace("\\", "")):
            return 0  # `co"dex"` and `co\<newline>dex` are codex too
        if tool_input.get("run_in_background") is not True:
            return 0  # in the foreground the Bash tool's stdin ends at once (276 of 276 here)
        if not unredirected_codex(command):
            return 0
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "additionalContext": WARNING,
        }}))
        return 0
    except Exception:
        return 0
