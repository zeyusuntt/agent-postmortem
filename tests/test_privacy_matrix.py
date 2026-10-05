"""N3c privacy matrix: one row per (sink, level) over one planted run.

Every text sink of the CLI, at every disclosure level it accepts, runs
in-process over the synthetic run of tests/n3b_planted.py (a label, phase
title, repository path, two raw agent ids, a session UUID, a project slug and
a transcript excerpt, all distinctive strings). Per row:

- share (the default): no planted value in stdout or stderr, no "disclosure:"
  line, and the sink's own structural line exists (a sink that prints nothing
  fails);
- local (--local): the first line is the local label, the real names the sink
  shows appear, no absolute path appears anywhere;
- raw (--raw): the first line is the raw label;
- local and raw, for a sink without --evidence or run without it: no planted
  excerpt text.

The last test walks the real argparse parser, so a command or subcommand
without a row here fails (docs/DESIGN.md 4.13.5, N3 plan P8 to P19).

`--json` output has its own rows (JSON_SINKS, N5a-2): schema 3 names its level
in a top-level `disclosure` field instead of a first line, so the JSON rows
assert that field, the structural values, aliases at share, and the same
absence rules; a second meta-test fails when a JSON-capable command or output
option has no JSON row.
"""

from __future__ import annotations

import argparse
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

from agent_postmortem import cli, reader, state
from agent_postmortem.report import EXCERPT_NOTE
from tests import n3b_helpers as h
from tests import n3b_planted as plant
from tests import test_watch as tw

LEVEL_FLAGS = {"share": [], "local": ["--local"], "raw": ["--raw"]}
PROMPT = "Fix the module."
RETRY_PROMPT = "Retry the fix."
RULE = "R-RETRY-SAFE"
TARGET_PLANT = "zzTarget" + "Plant" + "7f1"
ADOPTED_PLANT = "zzAdopted" + "Plant" + "7f2"
CONTENT_PLANT = "zzContent" + "Plant" + "7f3"
# A run directory name that is not a canonical workflow run id: watch must
# print the reader's `wf_<8 hex>-x` replacement, never the name (N3c fix 1).
RUN_DIR_PLANT = "wf_zz" + "Planted" + "RunDir"
RUN_DIR_REPLACEMENT = reader.valid_workflow_run_id(RUN_DIR_PLANT)


@dataclass
class Env:
    planted: plant.Planted
    tmp_path: Path
    capsys: pytest.CaptureFixture
    monkeypatch: pytest.MonkeyPatch
    extra_absent: list[str] = field(default_factory=list)  # absent at share only
    dir_names: list[str] = field(default_factory=list)  # absent at share and local


def _run_cli(env: Env, *argv) -> tuple[int, str, str]:
    return h.cli(env.capsys, *argv)


def _main_transcript(env: Env) -> Path:
    path = env.planted.root / env.planted.slug / f"{env.planted.session}.jsonl"
    path.write_text(
        json.dumps({"type": "user", "timestamp": "2026-02-01T00:00:00.000Z",
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    return path


def _hook(env: Env, command: str, **payload) -> tuple[int, str, str]:
    body = {"transcript_path": str(_main_transcript(env)), "session_id": env.planted.session}
    body.update(payload)
    env.monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(body)))
    return h.cli(env.capsys, "hook", command)


# --- runners: (env, level flags) -> (code, stdout, stderr) -------------------


def _run_args(*extra):
    def runner(env: Env, flags: list[str]):
        return _run_cli(env, "run", env.planted.run_dir, *extra, *flags)
    return runner


def _scan_args(*extra):
    def runner(env: Env, flags: list[str]):
        return _run_cli(env, "scan", "--root", env.planted.root, *extra, *flags)
    return runner


def _rescue_args(sub: str, *extra):
    def runner(env: Env, flags: list[str]):
        return _run_cli(env, "rescue", sub, env.planted.run_dir, *extra, *flags)
    return runner


def _rules_args(*extra, root=True):
    def runner(env: Env, flags: list[str]):
        tail = ["--root", env.planted.root] if root else []
        return _run_cli(env, "rules", *extra, *tail, *flags)
    return runner


def _rules_run(env: Env, flags: list[str]):
    return _run_cli(env, "rules", "--run", env.planted.run_dir, *flags)


def _rules_propose_run(*extra):
    def runner(env: Env, flags: list[str]):
        return _run_cli(env, "rules", "propose", "--run", env.planted.run_dir, *extra, *flags)
    return runner


def _apply(env: Env, flags: list[str]):
    target = env.tmp_path / TARGET_PLANT / "CLAUDE.md"
    target.parent.mkdir()
    target.write_text(f"line one {CONTENT_PLANT}\nline two\n", encoding="utf-8")
    env.extra_absent += [CONTENT_PLANT, "line one"]  # the local diff shows context lines on purpose
    env.dir_names += [TARGET_PLANT]
    return _run_cli(env, "apply", RULE, "--target", target, *flags)


def _trend(env: Env, flags: list[str]):
    state.update(lambda s: s["applied"].append(
        {"rule_id": RULE, "target": f"/opt/{ADOPTED_PLANT}/CLAUDE.md",
         "applied_at": "2026-08-15T00:00:00Z"}))
    env.dir_names += [ADOPTED_PLANT]
    return _run_cli(env, "trend", "--root", env.planted.root, *flags)


def _demo(env: Env, flags: list[str]):
    return _run_cli(env, "demo", *flags)


def _watch(*extra):
    def runner(env: Env, flags: list[str]):
        clock = tw._Clock()
        tw._patch_clock(env.monkeypatch, clock)
        tw._patch_scripted_sleep(env.monkeypatch, [], clock)
        return _run_cli(env, "watch", env.planted.run_dir, "--interval", "1", *extra, *flags)
    return runner


def _watch_noncanonical(*extra):
    def runner(env: Env, flags: list[str]):
        renamed = env.planted.run_dir.parent / RUN_DIR_PLANT
        env.planted.run_dir.rename(renamed)
        env.dir_names += [RUN_DIR_PLANT]
        clock = tw._Clock()
        tw._patch_clock(env.monkeypatch, clock)
        tw._patch_scripted_sleep(env.monkeypatch, [], clock)
        return _run_cli(env, "watch", renamed, "--interval", "1", *extra, *flags)
    return runner


def _hook_check(active: bool):
    def runner(env: Env, flags: list[str]):
        return _hook(env, "check", hook_event_name="Stop", stop_hook_active=active)
    return runner


def _hook_session_end(env: Env, flags: list[str]):
    return _hook(env, "session-end", hook_event_name="SessionEnd")


def _hook_session_start(env: Env, flags: list[str]):
    _hook(env, "session-end", hook_event_name="SessionEnd")  # primes the digest
    return _hook(env, "session-start", hook_event_name="SessionStart", source="startup")


def _hook_workflow_launched(env: Env, flags: list[str]):
    return _hook(env, "workflow-launched", hook_event_name="PostToolUse", tool_name="Workflow",
                 tool_response={"runId": plant.WF_ID})


# --- the table ---------------------------------------------------------------


@dataclass(frozen=True)
class Sink:
    name: str
    covers: tuple[str, ...]  # the parser path this row exercises
    runner: Callable
    expect: tuple[str, ...]  # structural lines that must exist at every level
    levels: tuple[str, ...] = ("share", "local", "raw")
    # real names shown at local, as attribute names of the planted run
    local_names: tuple[str, ...] = ()
    # the sink prints transcript excerpts when asked (--evidence): the
    # "no excerpt" check at local and raw applies only to sinks without it
    evidence: bool = False
    json_lines: bool = False  # every stdout line is one JSON object
    silent: bool = False  # the sink prints nothing by design (session-end)
    share_expect: tuple[str, ...] = ()  # structural lines that exist at share only
    excerpt_shown: bool = False  # local and raw print the planted excerpt (--evidence)


SHARE_ONLY = ("share",)

SINKS = [
    Sink("run", ("run",), _run_args(),
         ("RUN wf_00000000-b10", "AGENT  ", "  PHASE  "), local_names=("label",)),
    Sink("run --verbose --evidence", ("run",), _run_args("--verbose", "--evidence"),
         ("RUN wf_00000000-b10", "STORIES (2)", "FINDINGS ("),
         local_names=("label", "agent1"), evidence=True),
    Sink("run --observe", ("run",), _run_args("--observe"),
         ("RUN wf_00000000-b10", "WORK AT RISK"), local_names=("label",)),
    Sink("run --bug-report", ("run",), _run_args("--bug-report"),
         ("## Reproduction", "## Evidence", "From run wf_00000000-b10"),
         local_names=("label", "agent1", "slug")),
    Sink("run --bug-report --evidence", ("run",), _run_args("--bug-report", "--evidence"),
         ("## Reproduction", "## Evidence", "From run wf_00000000-b10"),
         local_names=("label", "agent1", "slug"), evidence=True, excerpt_shown=True,
         share_expect=(EXCERPT_NOTE,)),
    Sink("demo", ("demo",), _demo, ("RUN wf_00000000-d30  DEGRADED", "STORIES (2)"),
         levels=SHARE_ONLY),
    Sink("scan", ("scan",), _scan_args(), ("NEEDS ATTENTION (1 of 1 runs)", "ALL TIME")),
    Sink("scan --all", ("scan",), _scan_args("--all"), ("wf_00000000-b10", "workflow"),
         local_names=("slug",)),
    Sink("rescue plan", ("rescue", "plan"), _rescue_args("plan"),
         ("UNIT ", "BRIEF FOR A RELAUNCH"), local_names=("label", "agent1")),
    Sink("rescue plan --evidence", ("rescue", "plan"), _rescue_args("plan", "--evidence"),
         ("UNIT ", "BRIEF FOR A RELAUNCH"), local_names=("label", "agent1"), evidence=True),
    Sink("rescue status", ("rescue", "status"), _rescue_args("status"),
         ("UNIT ", "checks declared"), local_names=("label",)),
    Sink("rescue status --evidence", ("rescue", "status"), _rescue_args("status", "--evidence"),
         ("UNIT ", "checks declared"), local_names=("label",), evidence=True),
    Sink("rules", ("rules",), _rules_args(), ("R-",)),
    Sink("rules --all", ("rules",), _rules_args("--all"), (RULE,)),
    Sink("rules --show", ("rules",), _rules_args("--show", RULE, root=False), (RULE,)),
    Sink("rules --run", ("rules",), _rules_run, ("R-",)),
    Sink("rules propose", ("rules", "propose"), _rules_args("propose"),
         ("no rule is backed by",)),
    Sink("rules propose --run", ("rules", "propose"), _rules_propose_run(),
         ("rules backed by findings in run wf_00000000-b10", RULE)),
    Sink("rules review", ("rules", "review"), _rules_args("review"),
         ("no adopted rule to review",)),
    Sink("apply", ("apply",), _apply, (RULE,), share_expect=("path-1",)),
    Sink("trend", ("trend",), _trend, ("WEEKLY DETECTOR COUNTS", RULE)),
    Sink("watch", ("watch",), _watch(), ("KILL",), levels=SHARE_ONLY),
    Sink("watch --json", ("watch",), _watch("--json"), ("KILL",), levels=SHARE_ONLY,
         json_lines=True),
    Sink("watch (non-canonical run dir)", ("watch",), _watch_noncanonical(),
         ("KILL", RUN_DIR_REPLACEMENT), levels=SHARE_ONLY),
    Sink("watch --json (non-canonical run dir)", ("watch",), _watch_noncanonical("--json"),
         ("KILL", RUN_DIR_REPLACEMENT), levels=SHARE_ONLY, json_lines=True),
    Sink("hook check", ("hook", "check"), _hook_check(False),
         ("systemMessage", "/postmortem wf_00000000-b10", "a:"), levels=SHARE_ONLY,
         json_lines=True),
    Sink("hook check (stop_hook_active)", ("hook", "check"), _hook_check(True),
         ("systemMessage", "/postmortem wf_00000000-b10", "a:"), levels=SHARE_ONLY,
         json_lines=True),
    Sink("hook session-start", ("hook", "session-start"), _hook_session_start,
         ("systemMessage", "analyzed since last digest", "/postmortem "), levels=SHARE_ONLY,
         json_lines=True),
    # docs/DESIGN.md 7.2: session-end prints nothing; the row pins that it
    # stays silent (stdout and stderr empty) and its effect is what the
    # session-start row reads back.
    Sink("hook session-end", ("hook", "session-end"), _hook_session_end, (),
         levels=SHARE_ONLY, silent=True),
    Sink("hook workflow-launched", ("hook", "workflow-launched"), _hook_workflow_launched,
         ("Workflow wf_00000000-b10 launched", "agent-postmortem watch wf_00000000-b10"),
         levels=SHARE_ONLY, json_lines=True),
]

# Parser paths that print no run-derived text, so they need no row.
ALLOWLIST = {
    # prints one static JSON object, {"protocol": 1, "version": ...}, and reads
    # no run, no state, and no transcript (test_hook_capabilities_is_static).
    ("hook", "capabilities"),
}

ROWS = [(sink, level) for sink in SINKS for level in sink.levels]
ROW_IDS = [f"{sink.name} @ {level}" for sink, level in ROWS]


def _planted_strings(env: Env) -> dict[str, str]:
    values = dict(h.planted_values(env.planted))
    values["prompt"] = PROMPT
    values["retry prompt"] = RETRY_PROMPT
    values["tmp dir"] = str(env.planted.repo.parent)
    values["repo basename"] = env.planted.repo.name
    for index, extra in enumerate([*env.extra_absent, *env.dir_names]):
        values[f"extra {index}"] = extra
    return values


@pytest.mark.parametrize(("sink", "level"), ROWS, ids=ROW_IDS)
def test_matrix_row(sink, level, tmp_path, monkeypatch, capsys):
    env = Env(plant.build(tmp_path), tmp_path, capsys, monkeypatch)
    code, out, err = sink.runner(env, LEVEL_FLAGS[level])
    assert code in (0, 3, 4), (code, err)
    combined = out + err
    lines = out.splitlines()

    if sink.silent:
        assert out == "" and err == "", (out, err)
    else:
        assert out, f"{sink.name} printed nothing"
        for line in sink.expect:
            assert line in out, f"structural line missing: {line!r}"
    if level == "share":
        for line in sink.share_expect:
            assert line in out, f"share structural line missing: {line!r}"
    if sink.json_lines:
        for line in lines:
            assert isinstance(json.loads(line), dict)

    if level == "share":
        for name, value in _planted_strings(env).items():
            assert value not in combined, f"{name} leaked at share: {value}"
        assert env.planted.agent1[:12] not in combined
        assert "zzplant" not in combined
        assert "disclosure:" not in combined
        return

    expected_label = {"local": h.LOCAL_LABEL, "raw": h.RAW_LABEL}[level]
    assert lines[0] == expected_label
    assert not any(line.startswith("disclosure:") for line in lines[1:])
    if not sink.evidence:
        assert env.planted.excerpt not in combined, "excerpt text without --evidence"
    if sink.excerpt_shown:
        assert env.planted.excerpt in out, "--evidence excerpt missing"
    if level == "local":
        for attribute in sink.local_names:
            assert getattr(env.planted, attribute) in out, f"{attribute} missing at local"
        h.assert_no_absolute_path(combined, env.planted)
        for name in env.dir_names:
            assert name not in combined, f"directory name leaked at local: {name}"


def test_no_redact_is_the_raw_alias(tmp_path, capsys):
    planted = plant.build(tmp_path)
    _code, raw, _err = h.cli(capsys, "run", planted.run_dir, "--raw")
    _code, alias, _err = h.cli(capsys, "run", planted.run_dir, "--no-redact")
    assert alias == raw
    assert raw.splitlines()[0] == h.RAW_LABEL


def test_hook_capabilities_is_static(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "x"})))
    _code, out, err = h.cli(capsys, "hook", "capabilities")
    assert err == ""
    assert set(json.loads(out)) == {"protocol", "version"}


# --- the parser-coverage meta-test -------------------------------------------


def _walk(parser: argparse.ArgumentParser, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Every command path a user can run: a leaf subcommand, and a command
    whose own subcommand is optional (`rules`) also on its own."""
    paths: set[tuple[str, ...]] = set()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, child in action.choices.items():
                child_prefix = (*prefix, name)
                nested = _walk(child, child_prefix)
                if nested:
                    paths |= nested
                    if any(
                        isinstance(a, argparse._SubParsersAction) and not a.required
                        for a in child._actions
                    ):
                        paths.add(child_prefix)
                else:
                    paths.add(child_prefix)
        elif not action.option_strings and action.choices and prefix:
            # a positional that names the subcommand (`hook <command>`)
            for name in action.choices:
                paths.add((*prefix, str(name)))
    return paths


def test_every_parser_command_has_a_matrix_row_or_an_allowlist_entry():
    parser = cli._build_parser()
    paths = _walk(parser)
    covered = {sink.covers for sink in SINKS}
    missing = sorted(paths - covered - ALLOWLIST)
    assert not missing, f"commands with no privacy matrix row: {missing}"
    stale = sorted((covered | ALLOWLIST) - paths)
    assert not stale, f"matrix rows or allowlist entries for commands that do not exist: {stale}"
    # the bare `rules` and a `rescue`/`hook` group are not themselves sinks
    assert ("rules",) in paths and ("rescue",) not in paths and ("hook",) not in paths
    assert {("scan",), ("run",), ("demo",), ("watch",), ("apply",), ("trend",)} <= paths


def test_the_walk_sees_a_new_command_and_a_new_nested_subcommand():
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    sub.add_parser("newcmd")
    nested = sub.add_parser("newgroup").add_subparsers(dest="x", required=True)
    nested.add_parser("leaf")
    paths = _walk(parser)
    assert ("newcmd",) in paths and ("newgroup", "leaf") in paths


# --- JSON rows (N5a-2, docs/DESIGN.md 4.13.11 "N5", plan S10) -----------------
#
# Every JSON surface at every level it accepts. A versioned document names its
# level in the top-level `disclosure` field (never a prose first line). Per row:
#
# - share: schema 3, `disclosure == "share"`, the structural values, a stable
#   alias where the sink carries an identity, no planted value, and no
#   `excerpt` key anywhere in the tree;
# - local: the real names the sink is designed to show, no absolute path in any
#   key or value, excerpts only with --evidence;
# - raw: the real value where the sink carries it, excerpts only with --evidence.
#
# watch JSONL and the hook messages keep their rows above (share only).

ALL_LEVELS = ("share", "local", "raw")
# options that change what a JSON document carries; a JSON-capable command with
# one of these needs a row that exercises it
OUTPUT_OPTIONS = frozenset({"--all", "--show", "--run", "--observe", "--verbose", "--evidence", "--bug-report"})
# (command path, option) -> why no JSON row exists for it
JSON_OPTION_EXEMPT = {
    ("run", "--bug-report"): "prints the markdown bug report even with --json (a text sink, row above)",
}


@dataclass(frozen=True)
class JsonSink:
    name: str
    covers: tuple[str, ...]
    runner: Callable
    expect: tuple[str, ...]  # substrings that exist at every level
    options: tuple[str, ...] = ()  # the OUTPUT_OPTIONS this row exercises
    levels: tuple[str, ...] = ALL_LEVELS
    versioned: bool = True  # carries `schema: 3` (rules --show has no schema)
    local_names: tuple[str, ...] = ()  # planted attributes shown at local
    raw_names: tuple[str, ...] = ()  # planted attributes shown at raw
    share_alias: tuple[str, ...] = ()  # regexes that must match at share
    evidence: bool = False  # --evidence given: no excerpt-absence check at local and raw
    excerpt_shown: bool = False  # local and raw carry the planted excerpt


def _run_args_demo_json(env: Env, flags: list[str]):
    return _run_cli(env, "demo", "--json", *flags)


def _rules_run_json(env: Env, flags: list[str]):
    return _run_cli(env, "rules", "--run", env.planted.run_dir, "--json", *flags)


def _trend_json(env: Env, flags: list[str]):
    state.update(lambda s: s["applied"].append(
        {"rule_id": RULE, "target": f"/opt/{ADOPTED_PLANT}/CLAUDE.md",
         "applied_at": "2026-08-15T00:00:00Z"}))
    env.dir_names += [ADOPTED_PLANT]
    return _run_cli(env, "trend", "--root", env.planted.root, "--json", *flags)


AGENT_ALIAS = r'"agent_id": "a:[0-9a-f]{12,}"'
PROJECT_ALIAS = r'"project": "project:[0-9a-f]{6,}"'
POINTER_ALIAS = r'"pointer": "agent-[0-9a-f]{12,}\.jsonl:\d+"'
PATH_ALIAS = r'"path": "path-1"'
RUN_ID = '"run_id": "wf_00000000-b10"'

JSON_SINKS: list[JsonSink] = [
    JsonSink("run --json", ("run",), _run_args("--json"), (RUN_ID, '"attempts"'),
             local_names=("label", "agent1"), raw_names=("label", "agent1", "repo"),
             share_alias=(AGENT_ALIAS,)),
    JsonSink("run --json --verbose --evidence", ("run",), _run_args("--json", "--verbose", "--evidence"),
             (RUN_ID, '"findings"'), options=("--verbose", "--evidence"),
             local_names=("label", "agent1"), raw_names=("label", "agent1", "repo"),
             share_alias=(AGENT_ALIAS,), evidence=True, excerpt_shown=True),
    JsonSink("run --json --observe", ("run",), _run_args("--json", "--observe"),
             (RUN_ID, '"observation"'), options=("--observe",),
             local_names=("label", "agent1"), raw_names=("label", "agent1", "repo"),
             share_alias=(AGENT_ALIAS, PATH_ALIAS)),
    JsonSink("demo --json", ("demo",), _run_args_demo_json, ('"run_id": "wf_00000000-d30"', '"attempts"'),
             levels=SHARE_ONLY, share_alias=(AGENT_ALIAS,)),
    JsonSink("scan --json", ("scan",), _scan_args("--json"), ('"rows"', RUN_ID),
             local_names=("slug",), raw_names=("slug",), share_alias=(PROJECT_ALIAS,)),
    JsonSink("scan --json --all", ("scan",), _scan_args("--json", "--all"), ('"rows"', RUN_ID),
             options=("--all",), local_names=("slug",), raw_names=("slug",),
             share_alias=(PROJECT_ALIAS,)),
    JsonSink("trend --json", ("trend",), _trend_json, ('"weekly_counts"', RULE)),
    JsonSink("rules --json", ("rules",), _rules_args("--json"), ('"rules"', RULE)),
    JsonSink("rules --json --all", ("rules",), _rules_args("--json", "--all"), ('"rules"', RULE),
             options=("--all",)),
    JsonSink("rules --json --run", ("rules",), _rules_run_json, ('"rules"', RULE), options=("--run",)),
    JsonSink("rules --show --json", ("rules",), _rules_args("--show", RULE, "--json", root=False),
             (RULE,), options=("--show",), versioned=False),
    JsonSink("rules propose --json", ("rules", "propose"), _rules_args("propose", "--json"),
             ('"schema": 3',)),
    JsonSink("rules propose --json --run", ("rules", "propose"), _rules_propose_run("--json"),
             ('"proposals"', RULE, '"adopted": null'), options=("--run",)),
    JsonSink("rules review --json", ("rules", "review"), _rules_args("review", "--json"),
             ('"schema": 3',)),
    JsonSink("rescue plan --json", ("rescue", "plan"), _rescue_args("plan", "--json"),
             ('"units"', RUN_ID), local_names=("label", "agent1"), raw_names=("label", "agent1"),
             share_alias=(POINTER_ALIAS, PATH_ALIAS)),
    JsonSink("rescue plan --json --evidence", ("rescue", "plan"),
             _rescue_args("plan", "--json", "--evidence"), ('"units"', RUN_ID), options=("--evidence",),
             local_names=("label", "agent1"), raw_names=("label", "agent1"),
             share_alias=(POINTER_ALIAS, PATH_ALIAS), evidence=True),
    JsonSink("rescue status --json", ("rescue", "status"), _rescue_args("status", "--json"),
             ('"units"', '"exit_code"'), local_names=("label",), raw_names=("label",)),
    JsonSink("rescue status --json --evidence", ("rescue", "status"),
             _rescue_args("status", "--json", "--evidence"), ('"units"', '"exit_code"'),
             options=("--evidence",), local_names=("label",), raw_names=("label",), evidence=True),
]
JSON_ROWS = [(sink, level) for sink in JSON_SINKS for level in sink.levels]
JSON_ROW_IDS = [f"{sink.name} @ {level}" for sink, level in JSON_ROWS]


def _parser_at(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> argparse.ArgumentParser:
    for name in path:
        action = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        if action is None:  # `hook <command>`: the command is a positional choice
            break
        parser = action.choices[name]
    return parser


def _options_of(parser: argparse.ArgumentParser) -> set[str]:
    return {o for a in parser._actions for o in a.option_strings}


def _json_gaps(parser: argparse.ArgumentParser, rows: list, exempt: dict) -> list[str]:
    """Every way the JSON rows fail to cover the parser's JSON-capable commands."""
    gaps: list[str] = []
    for path in sorted(_walk(parser)):
        options = _options_of(_parser_at(parser, path))
        if "--json" not in options:
            continue
        mine = [row for row in rows if row.covers == path]
        if not mine:
            gaps.append(f"{' '.join(path)}: no JSON row")
            continue
        exercised = {option for row in mine for option in getattr(row, "options", ())}
        for option in sorted((options & OUTPUT_OPTIONS) - exercised):
            if (path[-1], option) not in exempt and (path[0], option) not in exempt:
                gaps.append(f"{' '.join(path)} {option}: no JSON row exercises it")
        accepts_levels = {"--local", "--raw"} <= options
        want = set(ALL_LEVELS) if accepts_levels else {"share"}
        have = {level for row in mine for level in row.levels}
        if have != want:
            gaps.append(f"{' '.join(path)}: JSON rows cover levels {sorted(have)}, parser accepts {sorted(want)}")
    return gaps


def _json_strings(node, keys: list[str], values: list[str]) -> None:
    """Every key and every string value of a JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            keys.append(key)
            _json_strings(value, keys, values)
    elif isinstance(node, list):
        for item in node:
            _json_strings(item, keys, values)
    elif isinstance(node, str):
        values.append(node)


@pytest.mark.parametrize(("sink", "level"), JSON_ROWS, ids=JSON_ROW_IDS)
def test_json_matrix_row(sink, level, tmp_path, monkeypatch, capsys):
    env = Env(plant.build(tmp_path), tmp_path, capsys, monkeypatch)
    code, out, err = sink.runner(env, LEVEL_FLAGS[level])
    assert code in (0, 3, 4), (code, err)
    combined = out + err
    assert out, f"{sink.name} printed nothing"
    # one JSON document, no prose label line in front of it
    assert out.lstrip().startswith("{"), out[:60]
    doc = json.loads(out)
    keys: list[str] = []
    values: list[str] = []
    _json_strings(doc, keys, values)
    assert doc["disclosure"] == level
    if sink.versioned:
        assert doc["schema"] == 3
    else:
        assert "schema" not in doc
    assert h.LOCAL_LABEL not in combined and h.RAW_LABEL not in combined
    for fragment in sink.expect:
        assert fragment in out, f"structural value missing: {fragment!r}"

    if level == "share":
        for pattern in sink.share_alias:
            assert re.search(pattern, out), f"alias missing at share: {pattern}"
        for name, value in _planted_strings(env).items():
            assert value not in combined, f"{name} leaked at share: {value}"
        assert env.planted.agent1[:12] not in combined
        assert "zzplant" not in combined
        assert "excerpt" not in keys, "an excerpt key at share"
        return

    if not sink.evidence:
        assert env.planted.excerpt not in combined, "excerpt text without --evidence"
        assert "excerpt" not in keys
    if sink.excerpt_shown:
        assert env.planted.excerpt in out, "--evidence excerpt missing"
    names = sink.local_names if level == "local" else sink.raw_names
    for attribute in names:
        assert str(getattr(env.planted, attribute)) in out, f"{attribute} missing at {level}"
    if level == "local":
        # keys and values both: no absolute path anywhere in the document
        h.assert_no_absolute_path("\\n".join([*keys, *values]), env.planted)
        for name in env.dir_names:
            assert name not in combined, f"directory name leaked at local: {name}"


def test_every_json_capable_command_and_option_has_a_json_row():
    rows = [*JSON_SINKS, *(s for s in SINKS if s.json_lines)]
    gaps = _json_gaps(cli._build_parser(), rows, JSON_OPTION_EXEMPT)
    assert not gaps, gaps


def test_the_json_gap_check_sees_a_new_json_command_and_a_new_json_option():
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    newcmd = sub.add_parser("newjson")
    newcmd.add_argument("--json", action="store_true")
    run = sub.choices["run"]
    run.add_argument("--observe-everything", action="store_true")
    rows = [*JSON_SINKS, *(s for s in SINKS if s.json_lines)]
    gaps = _json_gaps(parser, rows, JSON_OPTION_EXEMPT)
    assert "newjson: no JSON row" in gaps
    # a command row that exercises no OUTPUT option makes the check name each one it left out
    fake = JsonSink("fake run", ("run",), lambda env, flags: (0, "", ""), (), options=())
    gaps = _json_gaps(cli._build_parser(), [fake], JSON_OPTION_EXEMPT)
    assert any(gap.startswith("run --observe:") for gap in gaps)
    assert not any("--bug-report" in gap for gap in gaps)  # exempt, with a reason
