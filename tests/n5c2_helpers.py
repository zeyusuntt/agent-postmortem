"""Shared synthetic builders for the N5c-2 tests (rescue per declared repository,
the exit-code tables, watch's DONE).

Everything is synthetic: the "repositories" are plain directories under
pytest's tmp_path (rescue status attributes lexically and never asks git), the
workflow run is a real run directory written through `tests.n5c1_helpers.build_run`,
and the main-session calls a status run reads are appended to its parent
transcript after the run ended.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from agent_postmortem import cli
from tests.n5c1_helpers import BASE, Att, bash_step, build_run, init_repo, write_step
from tests.test_field_v03 import _iso, _parent_path

__all__ = [
    "Att", "BASE", "Call", "Scenario", "bash_step", "build_scenario", "exit_of", "init_repo",
    "shell", "monitor", "file_write", "multiedit_write", "notebook_write", "write_step",
    "age_files", "run_cli",
]

CHECK = "make check"


@dataclass
class Call:
    """One main-session call after the run ended: kind `bash` or `write`."""

    kind: str
    text: str  # the command, or the path
    cwd: str | None
    result: str = ""
    error: bool = False
    tool_id: str | None = None  # a fixed tool_use id (two calls sharing one are ambiguous)
    no_result: bool = False  # the call has no tool_result at all


def shell(
    command: str, cwd, *, error: bool = False, result: str = "", tool_id: str | None = None,
    no_result: bool = False,
) -> Call:
    return Call(
        "bash", command, str(cwd) if cwd is not None else None, result, error, tool_id, no_result,
    )


def monitor(command: str, cwd, *, result: str = "") -> Call:
    return Call("monitor", command, str(cwd) if cwd is not None else None, result)


def multiedit_write(
    path, cwd, *, error: bool = False, result: str = "", tool_id: str | None = None,
) -> Call:
    return Call(
        "multiedit", str(path), str(cwd) if cwd is not None else None, result, error, tool_id,
    )


def notebook_write(path, cwd) -> Call:
    return Call("notebook", str(path), str(cwd) if cwd is not None else None)


def file_write(path, cwd) -> Call:
    return Call("write", str(path), str(cwd) if cwd is not None else None)


@dataclass
class Scenario:
    run_dir: Path
    root: Path
    parent: Path


def _call_entries(index: int, call: Call) -> list[dict]:
    when = BASE + timedelta(minutes=25, seconds=4 * index)
    tool_id = call.tool_id or f"toolu_c{index}"
    name = {
        "bash": "Bash", "monitor": "Monitor", "write": "Write", "notebook": "NotebookEdit",
        "multiedit": "MultiEdit",
    }[call.kind]
    tool_input = {
        "write": {"file_path": call.text}, "notebook": {"notebook_path": call.text},
        "multiedit": {"file_path": call.text, "edits": []},
    }.get(call.kind, {"command": call.text})
    use = {
        "type": "assistant", "timestamp": _iso(when),
        "message": {"role": "assistant", "usage": {}, "content": [
            {"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]},
    }
    result = {
        "type": "user", "timestamp": _iso(when + timedelta(seconds=1)),
        "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_id, "content": call.result or "ok",
             "is_error": call.error}]},
    }
    if call.cwd is not None:
        use["cwd"] = call.cwd
        result["cwd"] = call.cwd
    return [use] if call.no_result else [use, result]


def build_scenario(
    tmp_path: Path, monkeypatch, wf_suffix: str, attempts: list[Att], *,
    repos: list[tuple[str, list[str]]] | None = None, calls: list[Call] | None = None,
    parent: str = "completed", config_text: str | None = None,
) -> Scenario:
    """A workflow run (`attempts`, written by build_run), a configuration declaring
    `repos` ([(path, [checks])]), and `calls` appended to the parent transcript
    after the run ended. `config_text` replaces the generated configuration."""
    run_dir = build_run(
        tmp_path, monkeypatch, f"wf_00000000-{wf_suffix}", attempts, parent=parent,
    )
    root = tmp_path / "root"
    parent_path = _parent_path(root)
    if calls:
        lines = []
        for index, call in enumerate(calls):
            lines.extend(_call_entries(index, call))
        with parent_path.open("a", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(line) + "\n")
    if config_text is None and repos is not None:
        pieces = []
        for path, checks in repos:
            joined = ", ".join(json.dumps(check) for check in checks)
            pieces.append(f'[[repos]]\npath = {json.dumps(str(path))}\nchecks = [{joined}]\n')
        config_text = "\n".join(pieces)
    if config_text is not None:
        config = tmp_path / "cfg.toml"
        config.write_text(config_text, encoding="utf-8")
        monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config))
    return Scenario(run_dir=run_dir, root=root, parent=parent_path)


def age_files(*roots: Path, seconds: float = 7200.0) -> None:
    """Make every file under the given roots (or the file itself) old, so a run
    is not 'possibly live' by its modification times."""
    then = time.time() - seconds
    for root in roots:
        if root.is_file():
            os.utime(root, (then, then))
            continue
        for path in root.rglob("*"):
            if path.is_file():
                os.utime(path, (then, then))


def exit_of(argv: list[str]) -> int:
    """The process exit status of `cli.main(argv)`, whether it returns or raises SystemExit."""
    try:
        code = cli.main(argv)
    except SystemExit as exc:  # argparse
        code = exc.code
    return 0 if code is None else int(code)


def run_cli(capsys, argv: list[str]) -> tuple[int, str, str]:
    code = exit_of(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err
