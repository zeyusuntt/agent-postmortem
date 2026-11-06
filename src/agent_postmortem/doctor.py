"""`agent-postmortem doctor`: can alerts reach this session? (docs/DESIGN.md 4.14.12:
quiet can mean healthy or broken).

The plugin's normal success is silence, and its failures are silent too: hooks never fail a turn,
so a missing plugin, an old Claude Code or a state directory it cannot write all look like nothing
to report. This reads what it can and says which. Claude Code's version comes from what the
session's own transcript records. The one program it runs is `claude agents --json --all`, once,
to see whether the job files the hooks read still say what Claude Code itself says; no hook runs it.

Read-only over everything but the tool's own state, where a transaction proves it can write and
`--test-alert` leaves one flag for the next hook call to pick up. Prints no path below `--raw` and
no worker's name below `--local`. Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import hook_gate, live, stalls, state, status, watcher, workers
from .redact import Redactor

OK, WARN, FAIL, INFO = "OK", "WARN", "FAIL", "INFO"
PLUGIN_KEY = "agent-postmortem@agent-postmortem"
TEST_ALERT = ("ALERT TEST from agent-postmortem doctor: this line reached you through the per-call "
              "hook, so alerts can reach this session. Nothing is wrong.")
_WIDTH = 14
# dotted numbers and a known prerelease word: an id, a name or a sentence stays unprinted
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:-(?:alpha|beta|rc|dev|pre)\.?[0-9]*)?")
_WRAPPER_MARK = b"agent-postmortem for a session's Bash tool"  # plugin/bin/agent-postmortem
_NATIVE_TIMEOUT_SECONDS = 10  # 0.18 s measured with 110 sessions listed
_NATIVE_SETTLED_SECONDS = 5  # a job file changed more recently than this may be mid-change
_NATIVE_NAMED_MAX = 8
_JOB_ID_RE = re.compile(r"[0-9a-f]{8}")
_EVERY_TOOL = (None, "", "*", ".*")  # as a hook group's matcher
_STATE_WORDS = frozenset({"working", "blocked", "done", "stopped", "failed"})  # as workers.classify


def _config_dir() -> Path:
    """Where this Claude Code keeps everything: the first of CLAUDE_CONFIG_DIR, as transcripts."""
    first = os.environ.get("CLAUDE_CONFIG_DIR", "").split(",", 1)[0].strip()
    return Path(first).expanduser() if first else Path.home() / ".claude"


def _plugins_root() -> Path:
    return _config_dir() / "plugins"


def _settings_path() -> Path:
    return _config_dir() / "settings.json"


def _read_json(path: Path) -> object:
    try:
        with path.open("rb") as handle:
            return workers.loads_bounded(handle.read(1 << 22))
    except (OSError, ValueError, RecursionError):
        return None


def _check(name: str, word: str, detail: str, named: str = "") -> dict:
    return {"name": name, "status": word, "detail": detail, "named": named}


def _plugin_folder(root: Path) -> Path | None:
    """The folder Claude Code reads the plugin from: a directory marketplace's own, else the
    installed copy."""
    known = _read_json(root / "known_marketplaces.json")
    entry = known.get("agent-postmortem") if isinstance(known, dict) else None
    source = entry.get("source") if isinstance(entry, dict) else None
    if isinstance(source, dict) and source.get("source") == "directory" \
            and isinstance(source.get("path"), str):
        market = _read_json(Path(source["path"]) / ".claude-plugin" / "marketplace.json")
        plugins = market.get("plugins") if isinstance(market, dict) else None
        for plugin in plugins if isinstance(plugins, list) else []:
            if isinstance(plugin, dict) and plugin.get("name") == "agent-postmortem" \
                    and isinstance(plugin.get("source"), str):
                return Path(source["path"]) / plugin["source"]
    installed = _read_json(root / "installed_plugins.json")
    plugins = installed.get("plugins") if isinstance(installed, dict) else None
    rows = plugins.get(PLUGIN_KEY) if isinstance(plugins, dict) else None
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    path = row.get("installPath") if row else None
    return Path(path) if isinstance(path, str) else None


def _is_installed(root: Path) -> bool:
    installed = _read_json(root / "installed_plugins.json")
    plugins = installed.get("plugins") if isinstance(installed, dict) else None
    return isinstance(plugins, dict) and PLUGIN_KEY in plugins


def _hook_copies() -> list[Path]:
    """Where run.sh looks first for the CLI its hooks run: the data directory a hook is given, below
    the plugins folder, or with none there the XDG state directory, its fallback. This process's
    own $CLAUDE_PLUGIN_DATA says nothing: a session's commands can hold another plugin's."""
    try:
        folders = sorted((_plugins_root() / "data").glob("agent-postmortem-*"))
    except OSError:
        folders = []
    folders = folders or [state.state_dir() / "plugin"]
    copies = [folder / "bin" / "agent-postmortem" for folder in folders]
    return [copy for copy in copies
            if copy.is_file() and os.access(copy, os.X_OK) and not _is_plugin_wrapper(copy)]


def _is_plugin_wrapper(path: Path) -> bool:
    try:
        if not path.is_file():  # a pipe with no writer never answers
            return False
        with path.open("rb") as handle:
            return _WRAPPER_MARK in handle.read(256)
    except OSError:
        return False


def _hook_cli_found() -> bool:
    """run.sh's order: its data directory, then PATH, by name only (running it is its own check).
    uvx puts its throwaway environment first on PATH for the command it runs and no hook sees
    that, so this interpreter's folder counts only when it is no environment or the active one. The
    plugin's own bin/ script is on a session's PATH and not on a hook's, and only runs the copies
    above, so a CLI in that place does not count."""
    if _hook_copies():
        return True
    own = None
    activated = os.path.realpath(os.environ.get("VIRTUAL_ENV") or "")
    if sys.prefix != sys.base_prefix and activated != os.path.realpath(sys.prefix):
        own = os.path.realpath(os.path.dirname(sys.executable))
    for part in os.environ.get("PATH", "").split(os.pathsep):
        if part and os.path.realpath(part) != own:
            found = shutil.which("agent-postmortem", path=part)
            if found is not None and not _is_plugin_wrapper(Path(found)):
                return True
    return False


def _check_cli(version_text: str) -> list[dict]:
    # the build stamp names a branch, which is shown from --local
    plain, _sep, stamp = version_text.partition(" (")
    rows = [_check("cli", OK, plain, stamp.removesuffix(")"))]
    running = os.path.realpath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    if os.path.basename(running) in ("agent-postmortem", "postmortem"):
        for copy in _hook_copies()[:1]:
            if os.path.realpath(copy) != running:
                rows.append(_check("cli copies", WARN, "the plugin's hooks run their own copy of "
                                   "the CLI, a different one from this"))
    return rows


def _check_hook_cli() -> list[dict]:
    if not _is_installed(_plugins_root()) or _hook_cli_found():
        return []
    return [_check("hooks cli", FAIL, "the plugin's hooks have no CLI to run, so they say nothing. "
                                      "It installs one at session start when uv is on PATH: "
                                      "restart the session, or uv tool install agent-postmortem")]


def _binds(hooks: object, event: str, command: str, *, option: str | None = None,
           every_tool: bool = False) -> bool:
    """Whether hooks.json runs `command` on `event`, with `option` set when one is named, and for
    every tool when that is asked."""
    table = hooks.get("hooks") if isinstance(hooks, dict) else None
    groups = table.get(event) if isinstance(table, dict) else None
    for group in groups if isinstance(groups, list) else []:
        if every_tool and isinstance(group, dict) and group.get("matcher") not in _EVERY_TOOL:
            continue
        rows = group.get("hooks") if isinstance(group, dict) else None
        for row in rows if isinstance(rows, list) else []:
            text = row.get("command") if isinstance(row, dict) else None
            if isinstance(text, str) and _runs_our_entry(text, command) \
                    and (option is None or row.get(option) is True):
                return True
    return False


def _runs_our_entry(command_line: str, word: str) -> bool:
    """Exactly `<...>/run.sh <word>`, as the shell would split it."""
    try:
        args = shlex.split(command_line)
    except ValueError:
        return False
    return len(args) == 2 and args[0].rsplit("/", 1)[-1] == "run.sh" and args[1] == word


def _absent(path: Path) -> bool:
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _settings_in_force(session: Path | None) -> tuple[list[dict], bool]:
    """The user's settings and the session's own project's, whichever can be read, and whether a
    file that is there could not be: it may be the one that turns the hooks off."""
    paths = [_settings_path()]
    cwd = _first_entry(session).get("cwd") if session is not None else None
    if isinstance(cwd, str):
        paths += [Path(cwd) / ".claude" / name for name in ("settings.json", "settings.local.json")]
    settings, unread = [], False
    for path in paths:
        found = _read_json(path)
        if isinstance(found, dict):
            settings.append(found)
        elif not _absent(path):
            unread = True
    return settings, unread


def _turned_off(settings: dict) -> bool:
    enabled = settings.get("enabledPlugins")
    return isinstance(enabled, dict) and enabled.get(PLUGIN_KEY) is False


def _check_plugin(session: Path | None) -> tuple[list[dict], Path | None]:
    root = _plugins_root()
    settings, unread = _settings_in_force(session)
    if any(found.get("disableAllHooks") is True for found in settings):
        return [_check("plugin", FAIL, "disableAllHooks is set, so no hook runs at all")], None
    if not _is_installed(root):
        return [_check("plugin", FAIL, "not installed; the CLI alone tells nobody: /plugin install "
                                       "agent-postmortem@agent-postmortem")], None
    if any(_turned_off(found) for found in settings):
        return [_check("plugin", FAIL, "installed but turned off: /plugin enable "
                                       "agent-postmortem@agent-postmortem")], None
    folder = _plugin_folder(root)
    hooks = _read_json(folder / "hooks" / "hooks.json") if folder is not None else None
    if not isinstance(hooks, dict):
        return [_check("plugin", WARN, "installed, but its hooks could not be read here")], folder
    manifest = _read_json(folder / ".claude-plugin" / "plugin.json")
    version = manifest.get("version") if isinstance(manifest, dict) else None
    named = f"version {version}" if isinstance(version, str) and _VERSION_RE.fullmatch(version) \
        else "version unknown"
    launcher = folder / "hooks" / "run.sh"
    if not os.access(launcher, os.X_OK):
        return [_check("plugin", FAIL, f"installed, {named}, but its launcher hooks/run.sh is "
                                       "missing or cannot run: reinstall the plugin")], folder
    if not _binds(hooks, "PostToolUse", "post-tool", every_tool=True):
        return [_check("plugin", FAIL, f"installed, {named}, with no per-call hook (post-tool): "
                                       "reinstall the plugin")], folder
    if not _binds(hooks, "UserPromptSubmit", "prompt-submit"):
        return [_check("plugin", WARN, f"installed, {named}, with no hook at a prompt "
                                       "(prompt-submit): reinstall the plugin")], folder
    if not _binds(hooks, "Stop", "stop-watch", option="asyncRewake"):
        return [_check("plugin", WARN, f"installed, {named}; too old to wake an idle session: "
                                       "update the plugin")], folder
    if unread:
        return [_check("plugin", WARN, f"installed, {named}, but a settings file could not be "
                                       "read, so a setting that turns its hooks off would not "
                                       "show")], folder
    return [_check("plugin", OK, f"installed, {named}, with the idle watcher")], folder


def _count_sessions(root: Path) -> tuple[int | None, int, int]:
    """(transcripts, project folders, folders it could not list); `glob` would hide a folder it may
    not list, which reads the same as an empty one. None when the root itself cannot be listed."""
    try:
        with os.scandir(root) as entries:
            folders = [entry.path for entry in entries if entry.is_dir()]
    except OSError:
        return None, 0, 0
    sessions = denied = 0
    for folder in folders:
        try:
            with os.scandir(folder) as entries:
                sessions += sum(1 for entry in entries if entry.name.endswith(".jsonl"))
        except OSError:
            denied += 1
    return sessions, len(folders), denied


def _is_own_projects(root: Path) -> bool:
    return root.expanduser().resolve() == (_config_dir() / "projects").resolve()


def _jobs_root(root: Path) -> Path:
    """Beside the transcripts: another spelling of the config's own projects folder means the
    config's job files."""
    if _is_own_projects(root):
        return _config_dir() / "jobs"
    return root.expanduser().resolve().parent / "jobs"


def _check_inputs(root: Path) -> list[dict]:
    rows = []
    sessions, folders, denied = _count_sessions(root)
    if sessions is None and _absent(root) and _is_own_projects(root):
        # a new machine: nothing failed, nothing is there yet (a --root it was given is a failure)
        rows.append(_check("transcripts", WARN, "none yet: no session has run here, so run one "
                                                "first", str(root)))
    elif sessions is None or (folders and denied == folders):
        return [_check("transcripts", FAIL, "the directory could not be read, so nothing can be "
                                            "checked", str(root))]
    elif denied:
        rows.append(_check("transcripts", WARN, f"{sessions} sessions read; {denied} project folder"
                                                f"{'' if denied == 1 else 's'} could not be read",
                           str(root)))
    else:
        rows.append(_check("transcripts", OK if sessions else WARN,
                           f"readable, {sessions} session{'' if sessions == 1 else 's'}"
                           if sessions else "readable, none yet: run a session first", str(root)))
    jobs_root = _jobs_root(root)
    try:
        jobs = sum(1 for path in jobs_root.iterdir() if path.is_dir()) if jobs_root.is_dir() else 0
        rows.append(_check("job files", OK if jobs else INFO,
                           f"readable, {jobs} background session{'' if jobs == 1 else 's'}" if jobs
                           else "none yet: no `claude --bg` on this machine", str(jobs_root)))
    except OSError:
        rows.append(_check("job files", FAIL, "the directory could not be read, so workers cannot "
                                              "be followed", str(jobs_root)))
    return rows


def _check_state() -> list[dict]:
    try:
        with state.cache_transaction(state.live_store_path()):
            pass
    except state.StateError:
        # a per-call line still goes out, unrecorded: a limit, not "will not reach you"
        return [_check("state", WARN, "not writable: lines may repeat and an idle session is never "
                                      "woken", str(state.state_dir()))]
    return [_check("state", OK, "writable", str(state.state_dir()))]


def _first_entry(main_path: Path) -> dict:
    """The transcript's first entry with a timestamp and working directory, as far as they show."""
    found: dict = {}
    try:
        with main_path.open("rb") as handle:
            for raw in handle.read(1 << 18).splitlines():
                try:
                    entry = workers.loads_bounded(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(entry, dict):
                    continue
                for key in ("timestamp", "cwd"):
                    if key not in found and isinstance(entry.get(key), str):
                        found[key] = entry[key]
                if len(found) == 2:
                    break
    except OSError:
        pass
    return found


def _newest_transcript(root: Path) -> Path | None:
    try:
        return max(root.glob("*/*.jsonl"), key=lambda path: path.stat().st_mtime, default=None)
    except OSError:
        return None


def _check_claude_code(session: Path | None, root: Path) -> list[dict]:
    where = session if session is not None else _newest_transcript(root)
    version = watcher.transcript_version(str(where)) if where is not None else None
    if version is None:
        return [_check("claude code", WARN, "version unknown: no transcript records one, so an "
                                            "idle session is not woken")]
    text = ".".join(str(part) for part in version)
    if version < watcher.MIN_VERSION:
        floor = ".".join(str(part) for part in watcher.MIN_VERSION)
        return [_check("claude code", WARN, f"{text}; older than {floor}, so an idle session is "
                                            "not woken. The per-call line still works")]
    return [_check("claude code", OK, f"{text}; can wake an idle session")]


def _check_session(session: Path | None, folder: Path | None) -> list[dict]:
    if session is None:
        return [_check("this session", INFO, "no session named, so nothing here looks at one: run "
                                             "this inside a session, or pass --session")]
    kinds = stalls.live_sessions(stalls.default_sessions_root(session))
    kind = kinds.get(session.stem)
    words = {"bg": "a live background session", "interactive": "a live interactive session"}
    rows = [_check("this session", OK, words.get(kind, "found; no live process names it now"))]
    if folder is not None:
        try:
            changed = (folder / "hooks" / "hooks.json").stat().st_mtime
        except OSError:
            changed = None
        started = workers._instant(_first_entry(session).get("timestamp"))
        if changed is not None and started is not None and started < changed:
            rows.append(_check("hooks", WARN, "the plugin's hooks changed after this session "
                                              "started: /reload-plugins or restart to get them"))
    return rows


def _check_workers(session: Path | None) -> list[dict]:
    if session is None:
        return []
    launches = status.own_workers(session)
    jobs_root = live.default_jobs_root(session)
    now = time.time()
    counts: dict[str, int] = {}
    names: list[str] = []
    unreadable = 0
    for short, launch in launches.items():
        path = workers.job_file(jobs_root, short)
        data = workers.read_job(path)
        if data is None:
            unreadable += not workers.job_gone(path)
            continue
        found = workers.view(short, path, data, now, launch.get("name") if isinstance(
            launch, dict) else None)
        if found is None:  # a state word this build does not know: an update may have changed it
            unreadable += 1
            continue
        counts[found["class"]] = counts.get(found["class"], 0) + 1
        names.append(f"{found['name']} ({short})")
    total = sum(counts.values())
    rows = []
    if unreadable:
        rows.append(_check("worker files", WARN, f"{unreadable} launched by this session could not "
                                                 "be read, so their changes are not told"))
    if not total and not unreadable:
        rows.append(_check("workers", INFO, "none launched by this session"))
    elif total:
        words = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
        rows.append(_check("workers", INFO, f"{total} launched by this session: {words}",
                           ", ".join(names)))
    return rows


def _claude_path() -> str | None:
    return shutil.which("claude")


def _native_rows(stdout: bytes) -> list[tuple[str, str]] | None:
    """(id, state) of each background session in `claude agents --json`, or None when it is not
    the shape this reads. An interactive session has no id and no job file, so it is passed over."""
    try:
        rows = workers.loads_bounded(stdout)
    except (ValueError, RecursionError):
        return None
    if not isinstance(rows, list):
        return None
    found = []
    for row in rows:
        if not isinstance(row, dict):
            return None
        if "id" not in row and row.get("kind") != "background":
            continue
        short, word = row.get("id"), row.get("state")
        if not (isinstance(short, str) and _JOB_ID_RE.fullmatch(short) and isinstance(word, str)):
            return None
        found.append((short, word))
    return found


def _state_word(word: object) -> str:
    return word if isinstance(word, str) and word in _STATE_WORDS else "another word"


def _in_flight(data: dict) -> bool:
    flight = data.get("inFlight")
    tasks = flight.get("tasks") if isinstance(flight, dict) else None
    return isinstance(tasks, int) and not isinstance(tasks, bool) and tasks > 0


def _job_reading(jobs_root: Path, short: str, word: str, now: float) -> tuple[str, str | None]:
    """Whether a job file says what the native list says: ("same"|"changing"|"differs", why)."""
    path = workers.job_file(jobs_root, short)
    data = workers.read_job(path)
    signature = workers.job_signature(path)
    if signature is not None and now - signature[0] / 1e9 < _NATIVE_SETTLED_SECONDS:
        return "changing", None
    if data is None:
        return "differs", "no job file" if workers.job_gone(path) else "job file unreadable"
    job_word = workers.effective_state(data)
    if word == job_word and word in _STATE_WORDS:
        return "same", None
    # its turn ended and something it started will wake it: the native list says working, the job
    # file done, for a worker on its own cron (one of 101 real sessions, 2026-11-05)
    if word == "working" and job_word == "done" and _in_flight(data):
        return "same", None
    return "differs", f"job file {_state_word(job_word)}"


def _check_native(root: Path) -> list[dict]:
    """The job files are not a published format and every notice rests on them. Claude Code's
    own list says the same thing from its side, so a difference is the first sign an update changed
    them. Run once per doctor, never from a hook: it costs 160 MB and 0.18 s."""
    name = "native state"
    if (path := _claude_path()) is None:
        return [_check(name, INFO, "not compared with `claude agents --json`: "
                                   "claude is not on PATH")]
    if not _is_own_projects(root):
        return [_check(name, INFO, "not compared: `claude agents --json` lists this machine's "
                                   "sessions, and another --root was named")]
    try:
        done = subprocess.run([path, "agents", "--json", "--all"], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=_NATIVE_TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is None or done.returncode != 0:
        return [_check(name, INFO, "`claude agents --json` could not be compared: it failed or "
                                   f"took over {_NATIVE_TIMEOUT_SECONDS} s")]
    rows = _native_rows(done.stdout)
    if rows is None:
        return [_check(name, WARN, "`claude agents --json` has a shape this build does not read, "
                                   "so it was not compared")]
    jobs_root = _config_dir() / "jobs"
    now = time.time()
    compared = 0
    differing: list[str] = []
    missing = unknown = 0
    for short, word in rows:
        reading, why = _job_reading(jobs_root, short, word, now)
        if reading == "changing":
            continue
        compared += 1
        if reading == "differs":
            differing.append(f"{short} reads {_state_word(word)}, {why}")
            missing += why == "no job file"
            unknown += word not in _STATE_WORDS
    plural = "" if compared == 1 else "s"
    if not compared:
        return [_check(name, INFO, "no background session to compare")]
    if not differing:
        return [_check(name, OK, f"`claude agents --json` agrees with the job files on {compared} "
                                 f"background session{plural}")]
    extra = len(differing) - _NATIVE_NAMED_MAX
    named = ", ".join(differing[:_NATIVE_NAMED_MAX]) + (f", and {extra} more" if extra > 0 else "")
    notes = f", {missing} with no job file" if missing else ""
    notes += f", {unknown} in a state this build does not know" if unknown else ""
    return [_check(name, WARN, f"{len(differing)} of {compared} background sessions read "
                               f"differently in `claude agents --json`{notes}. If it repeats, "
                               "the job files may have changed, and a worker can be told wrong",
                   named)]


def _check_watcher(session: Path | None) -> list[dict]:
    if os.environ.get("AGENT_POSTMORTEM_WAKE") == "0":
        return [_check("idle watcher", WARN, "turned off by AGENT_POSTMORTEM_WAKE=0: an idle "
                                             "session is not woken")]
    if session is None:
        return [_check("idle watcher", INFO, "not checked: no session named")]
    if not watcher.enabled(session):
        return [_check("idle watcher", WARN, "cannot run in this session: this Claude Code does "
                                             "not have asyncRewake")]
    return [_check("idle watcher", OK, "can run when a turn ends with a worker or a command out")]


def diagnose(root: Path, session: Path | None, version_text: str) -> dict:
    rows = _check_cli(version_text)
    plugin_rows, folder = _check_plugin(session)
    rows += plugin_rows
    rows += _check_hook_cli()
    rows += _check_claude_code(session, root)
    rows += _check_inputs(root)
    rows += _check_state()
    rows += _check_session(session, folder)
    rows += _check_workers(session)
    rows += _check_native(root)
    rows += _check_watcher(session)
    failed = next((row for row in rows if row["status"] == FAIL), None)
    warned = next((row for row in rows if row["status"] == WARN), None)
    if failed is not None:
        verdict = f"alerts will not reach you: {failed['name']}: {failed['detail']}"
    elif warned is not None:
        verdict = f"alerts can reach you, with a limit: {warned['name']}: {warned['detail']}"
    else:
        verdict = "nothing here would stop an alert reaching this session"
    return {"verdict": verdict, "checks": rows, "failed": failed is not None}


def render_text(report: dict, redactor: Redactor, can_test: bool) -> str:
    """Fixed words, counts and a checked version skip the redactor, which would hide
    `/reload-plugins` as a path; a named worker or path passes it, shown only from its level."""
    lines = [f"DOCTOR  {report['verdict']}"]
    for row in report["checks"]:
        named = _named(row, redactor)
        lines.append(f"  {row['status']:<5}{row['name']:<{_WIDTH}}{row['detail']}"
                     + (f" ({named})" if named else ""))
    if can_test:
        lines.append("  to see one arrive: agent-postmortem doctor --test-alert")
    return "\n".join(lines) + "\n"


def _named(row: dict, redactor: Redactor) -> str:
    if not row["named"]:
        return ""
    if redactor.level == "raw":
        return row["named"]
    shown_locally = row["name"] in ("cli", "workers", "native state")
    return redactor(row["named"]) if redactor.level == "local" and shown_locally else ""


def render_json(report: dict, redactor: Redactor) -> str:
    checks = []
    for row in report["checks"]:
        shown = {"name": row["name"], "status": row["status"], "detail": row["detail"]}
        named = _named(row, redactor)
        if named:
            shown["named"] = named
        checks.append(shown)
    return json.dumps({"schema": 3, "disclosure": redactor.level, "document": "doctor",
                       "verdict": report["verdict"], "checks": checks}, sort_keys=True) + "\n"


def render_armed_json(armed: bool, verdict: str, redactor: Redactor) -> str:
    return json.dumps({"schema": 3, "disclosure": redactor.level, "document": "doctor",
                       "armed": armed, "verdict": verdict, "checks": []}, sort_keys=True) + "\n"


def arm_test_alert(main_path: Path) -> bool:
    """Leave one flag for the next hook call of this session, and open its gate. False when either
    cannot be written, which is itself why alerts would not arrive."""
    try:
        with state.cache_transaction(state.live_store_path()) as data:
            record = live._store_session(data, live.session_key(main_path))
            record["test_alert"] = time.time()
            record["seen"] = time.time()
    except state.StateError:
        return False
    path = hook_gate._gate_path(str(main_path))
    # `alert`: a hook already in flight, which read before this, must not close the gate (4.14.12)
    gate = {**(hook_gate._read_gate(path) or {}), "pending": True, "alert": time.time()}
    try:
        state.write_private_bytes(path, json.dumps(gate).encode("utf-8"))
    except state.StateError:
        return False
    return True
