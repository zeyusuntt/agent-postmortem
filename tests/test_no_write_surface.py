"""docs/DESIGN.md 2/9 (unit M6, R3): the tool has no code path that writes
an instruction file, and its own text never sounds like it does. This
sweeps the rendered skill files, the README, and every string literal in
src/ for the two substrings whose presence would contradict that: the
removed `--write` flag, and a named instruction file (CLAUDE.md) the tool
might sound like it writes.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "agent_postmortem"

_PROSE_FILES = (
    REPO_ROOT / "plugin" / "skills" / "postmortem" / "SKILL.md",
    REPO_ROOT / "plugin" / "skills" / "rescue" / "SKILL.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "plugin" / "README.md",
)

# The one place "CLAUDE.md" is allowed at all (docs/DESIGN.md 9, R3): the
# rescue skill's own sentence telling a session never to touch an
# instruction file during a rescue. Any other occurrence, anywhere in the
# swept corpus, would mean the tool's own text drifted back toward sounding
# like it writes one.
_ALLOWED_CLAUDE_MD_FILE = REPO_ROOT / "plugin" / "skills" / "rescue" / "SKILL.md"
_ALLOWED_CLAUDE_MD_LINE_MARKER = "never edit"


def _python_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _string_literals(py_file: Path) -> list[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_write_flag_never_appears_in_skills_or_readme():
    for path in _PROSE_FILES:
        assert "--write" not in path.read_text(encoding="utf-8"), path


def test_write_flag_never_appears_in_a_src_string_literal():
    for py_file in _python_files():
        for literal in _string_literals(py_file):
            assert "--write" not in literal, f"{py_file}: {literal!r}"


def test_claude_md_only_appears_in_the_sentence_that_says_the_tool_never_writes_one():
    offenders = []
    for path in _PROSE_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "CLAUDE.md" not in line:
                continue
            if path == _ALLOWED_CLAUDE_MD_FILE and _ALLOWED_CLAUDE_MD_LINE_MARKER in line.lower():
                continue
            offenders.append((str(path), line))
    assert not offenders, offenders


def test_claude_md_never_appears_in_a_src_string_literal():
    offenders = []
    for py_file in _python_files():
        for literal in _string_literals(py_file):
            if "CLAUDE.md" in literal:
                offenders.append((str(py_file), literal))
    assert not offenders, offenders


# --- AST scan of every write/exec-capable call in src/ (release finding F14) --
#
# The prior version of this sweep only grepped for a handful of string
# literals and could not see a real subprocess/open call at all -- passing
# whenever a forbidden shape simply was not spelled the one way the grep
# expected. This scans the actual parsed AST of every module under
# src/agent_postmortem/ for a call matching one of the write- or
# process-capable sink shapes docs/DESIGN.md 5.4 names -- open()/Path.open(),
# write_text()/write_bytes(), os.replace/os.rename, shutil.*, subprocess.*,
# os.system, os.exec*/os.spawn*, os.popen -- and asserts the exact set found
# equals an explicit, justified allowlist. Anything not on the allowlist
# fails the test; anything on the allowlist that stops appearing in source
# also fails it, so the list can never silently go stale.


def _base_name(node: ast.expr) -> str | None:
    """Best-effort dotted prefix of a Call's func (e.g. "os" for os.replace,
    "path" for path.open, "shutil" for shutil.which)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _base_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _matched_sink(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return "open" if func.id == "open" else None
    if not isinstance(func, ast.Attribute):
        return None
    attr = func.attr
    base = _base_name(func.value) or ""
    if attr == "open":
        return f"{base}.open" if base else "<expr>.open"
    if attr in ("write_text", "write_bytes"):
        return f"{base}.{attr}" if base else attr
    if base == "os" and attr in ("replace", "rename", "system", "popen"):
        return f"os.{attr}"
    if base == "os" and (attr.startswith("exec") or attr.startswith("spawn")):
        return f"os.{attr}"
    if base == "shutil":
        return f"shutil.{attr}"
    if base == "subprocess":
        return f"subprocess.{attr}"
    return None


def _path_open_mode(call: ast.Call) -> str:
    """A Path.open()-shaped call's mode argument (first positional, or a
    `mode=` keyword, as a literal string); "r" when omitted, matching
    Path.open's own default. Used only to check calls this sweep did not
    already resolve as os.open (state.py's own low-level fd opens, which
    take no mode string at all)."""
    if (
        call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ):
        return call.args[0].value
    for kw in call.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return "r"


def _write_and_exec_sink_calls() -> list[tuple[str, int, str, ast.Call]]:
    """(module path relative to src/agent_postmortem/, line, matched sink
    name, the Call node itself) for every call in the tree matching
    _matched_sink, sorted by (module, line)."""
    hits: list[tuple[str, int, str, ast.Call]] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                matched = _matched_sink(node)
                if matched:
                    hits.append((str(path.relative_to(SRC)), node.lineno, matched, node))
    hits.sort(key=lambda hit: (hit[0], hit[1]))
    return hits


# The explicit allowlist docs/DESIGN.md 5.4 requires: every (module, line,
# sink) this sweep can find, with why each one is safe. Anything the sweep
# finds that is not a key here fails test_every_write_or_exec_sink_call_
# is_on_the_explicit_allowlist below; any key here the sweep no longer finds
# fails it too.
_ALLOWED_SINK_CALLS = {
    ("hooks_cli.py", 266, "path.open"):
        "_iter_json_lines: read a unit's own journal/transcript line by line, mode 'r'",
    ("observe.py", 277, "subprocess.run"):
        "observe.py's one documented git invocation point (docs/DESIGN.md 4.9 rule 4): "
        "git_path is always the shutil.which('git') result below, argv is always one of "
        "the five fixed templates rule 4 lists, never a shell, never repo-steerable",
    ("observe.py", 427, "shutil.which"):
        "resolve the git executable once (docs/DESIGN.md 4.9 rule 4, release finding F1); "
        "a read-only PATH lookup, writes nothing",
    ("observe.py", 652, "shutil.which"):
        "_GitSession.ready: the same one-time git lookup for the per-repository groups "
        "(docs/DESIGN.md 4.9, N5c-1); a read-only PATH lookup, writes nothing",
    # GC-1b: the session sniffers, _fork_scan (docs/DESIGN.md 4.2, FA4), _read_transcript, the
    # hook's kill-sentinel check and rescue's task text all read through _transcript_lines
    ("reader.py", 375, "path.open"):
        "_ends_in_torn_line: read a transcript's last bytes backwards, mode 'rb', never written",
    ("reader.py", 405, "path.open"): "_transcript_lines: read a transcript line by line, mode 'r'",
    ("reader.py", 413, "path.open"):
        "_transcript_lines: read on past a torn last line, mode 'r' (surrogateescape)",
    ("reader.py", 914, "journal_path.open"): "read journal.jsonl line by line, mode 'r'",
    ("reader.py", 1192, "meta_path.open"): "read an agent's own *.meta.json, mode 'r'",
    ("reader.py", 2647, "transcript_path.open"):
        "probe that a transcript can be opened at all, immediately closed again, mode 'r'",
    ("state.py", 231, "os.open"):
        "_backup_writer: create 'state.json.corrupt-<timestamp>' inside the state "
        "directory, O_CREAT|O_EXCL, never a caller-named path (docs/DESIGN.md 7.5)",
    ("state.py", 438, "os.open"):
        "_open_lock_file: open/create state.lock inside the state directory, O_CREAT, "
        "never a caller-named path",
    ("state.py", 469, "os.open"):
        "_fsync_dir: open the state directory itself read-only (O_RDONLY), to fsync it "
        "after a rename (best effort, state.json's own writer only)",
    ("state.py", 497, "os.replace"):
        "_write_state: the state store's own atomic rename of its temp file over "
        "state.json, both inside the state directory",
    ("state.py", 691, "os.open"):
        "_open_verified_state_dir: open the state directory itself read-only "
        "(O_RDONLY|O_DIRECTORY|O_NOFOLLOW) to fstat it before either disclosure file is "
        "ever touched, and (N3a fix round 3, new Finding 1) returns this same verified "
        "descriptor for lock_state_dir to keep open and use for every later disclosure "
        "operation instead of reopening by path; reads only, touches no path a caller "
        "names (docs/DESIGN.md 7.5a, N3a fix round 2, Finding 4)",
    ("state.py", 738, "os.open"):
        "_open_lock_file_at: open/create state.lock relative to the verified state "
        "directory descriptor (dir_fd), O_CREAT|O_RDWR|O_NONBLOCK|O_NOFOLLOW, mode 0600, "
        "so the held lock always belongs to the directory every disclosure operation "
        "uses (N3a Codex round 4, MAJOR 1); never a caller-named path",
    ("state.py", 876, "os.open"):
        "_open_probe_fd: O_RDONLY|O_NONBLOCK|O_NOFOLLOW probe open of the disclosure "
        "secret or alias registry, both fixed names, either inside the state directory "
        "by path or (N3a fix round 3, new Finding 1) relative to the verified directory "
        "descriptor via dir_fd -- never a caller-named path (docs/DESIGN.md 7.5, N3a)",
    ("state.py", 942, "os.open"):
        "_fsync_dir_strict: open the state directory itself read-only (O_RDONLY), to "
        "fsync it after the secret create or the registry rename, only when no dir_fd "
        "was given (N3a fix round 3: with dir_fd, it fsyncs that already-open "
        "descriptor directly instead); the disclosure-only sibling of _fsync_dir that "
        "reports failure instead of swallowing it (docs/DESIGN.md 7.5a, N3a fix round "
        "2, Finding 1)",
    ("state.py", 1039, "os.open"):
        "create_secret: O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW exclusive create of the "
        "disclosure secret, a fixed name, either inside the state directory by path or "
        "(N3a fix round 3, new Finding 1) relative to the verified directory "
        "descriptor via dir_fd -- never a caller-named path (docs/DESIGN.md 7.5, N3a)",
    ("state.py", 1124, "os.open"):
        "_create_exclusive_temp_file: O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW create of the "
        "alias registry's own temp file, either inside the state directory by path or "
        "(N3a fix round 3, new Finding 1) relative to the verified directory "
        "descriptor via dir_fd -- never a caller-named path (docs/DESIGN.md 7.5, N3a)",
    ("state.py", 1166, "os.open"):
        "_verify_registry_after_commit: O_RDONLY|O_NONBLOCK|O_NOFOLLOW reopen of the "
        "alias registry's own fixed path, right after write_registry_bytes's commit, "
        "either by path or (N3a fix round 3, new Finding 1) relative to the verified "
        "directory descriptor via dir_fd, to verify type/mode/owner/content from the "
        "descriptor instead of a symlink-following stat() (docs/DESIGN.md 7.5a, N3a "
        "fix round 2, Finding 6; O_NONBLOCK added in fix round 3, new Finding 2); "
        "reads only, touches no caller-named path",
    ("state.py", 1242, "os.rename"):
        "write_registry_bytes: the alias registry's own atomic commit, used only when "
        "dir_fd is given (N3a fix round 3, new Finding 1) -- os.rename(..., "
        "src_dir_fd=dir_fd, dst_dir_fd=dir_fd) renames the temp file over aliases.json "
        "relative to the verified directory descriptor; POSIX rename() is already an "
        "atomic overwrite, and os.rename (unlike os.replace) is the function "
        "os.supports_dir_fd actually lists on this platform -- both names, never a "
        "caller-named path (docs/DESIGN.md 7.5, N3a)",
    ("state.py", 1244, "os.replace"):
        "write_registry_bytes: the alias registry's own atomic rename of its temp "
        "file over aliases.json by path, used only when no dir_fd was given, both "
        "inside the state directory (docs/DESIGN.md 7.5, N3a)",
    ("watch.py", 174, "path.open"):
        "_read_new_journal_lines: read appended journal bytes, mode 'rb', never written",
    ("reader.py", 3488, "self.path.open"):
        "ParentIndex._hash_prefix: re-read already-parsed prefix bytes to validate the "
        "incremental cache, mode 'rb', never written",
    ("reader.py", 3686, "self.path.open"):
        "ParentIndex._parse_increment: read appended parent-transcript bytes, mode 'rb', "
        "never written",
}


def test_every_write_or_exec_sink_call_is_on_the_explicit_allowlist():
    found = _write_and_exec_sink_calls()
    found_keys = {(mod, line, sink) for mod, line, sink, _node in found}
    allowed_keys = set(_ALLOWED_SINK_CALLS)
    unexplained = found_keys - allowed_keys
    assert not unexplained, f"unlisted write/exec sink call(s), add to the allowlist or fix: {sorted(unexplained)}"
    stale = allowed_keys - found_keys
    assert not stale, f"allowlist entries no longer found in source (delete them): {sorted(stale)}"


def test_no_write_sink_call_outside_state_py_writes_anywhere():
    """docs/DESIGN.md 2/5.4: the tool is read-only over the data it
    inspects; it never writes inside a run directory. Structurally: every
    matched sink call outside state.py is either a Path.open() in a
    read-only mode ('r'/'rb'), or observe.py's own subprocess.run/
    shutil.which, which run git read-only and touch no path at all. A write
    to a caller-named path (a run/transcript path) outside the state
    directory therefore cannot exist without also breaking this test."""
    for module, line, sink, node in _write_and_exec_sink_calls():
        if module == "state.py":
            continue
        if sink.endswith(".open"):
            mode = _path_open_mode(node)
            assert set(mode) <= set("rb"), f"{module}:{line} opens in a writing mode: {mode!r}"
        elif sink in ("subprocess.run", "shutil.which"):
            continue
        else:
            raise AssertionError(
                f"{module}:{line} is a write/exec sink outside state.py not accounted for: {sink}"
            )
