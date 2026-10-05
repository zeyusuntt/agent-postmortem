"""N5a-1: schema 3 over today's behavior (docs/DESIGN.md 5.3b, 4.13.11 "N5").

Exact key sets per command and level, the folded types, aliases at share, no
excerpt key at share, a ledger on every attempt, a ledger list on every rescue
path row, `disclosure` everywhere (including `rules --show`), and `--local
--json` valid. The byte goldens are in tests/test_n5_schema3_goldens.py; this
file asserts the shapes by name so a reader can see which key each rule is
about.

The fixture fleet and its frozen clock are the goldens' (imported, not
copied). Every test here is public rendering, hence the n5 label (D17).
"""

from __future__ import annotations

import json
import re

import pytest

from agent_postmortem import disclosure, observe, reader, report, rescue
from agent_postmortem.redact import Redactor
from tests import test_n5_schema3_goldens as goldens
from tests.n3b_helpers import cli, make_secret_unavailable
from tests.test_n5_schema3_goldens import _PER_RUN_LABELS, _Env

# The fleet fixture lives with the goldens; binding it here makes it this module's.
env = goldens.env

LEVEL_FLAGS = {"share": [], "local": ["--local"], "raw": ["--raw"]}
LEVELS = sorted(LEVEL_FLAGS)

RUN_TOP = {"schema", "disclosure", "run", "findings", "rules", "verdict", "status", "stories", "next"}
RUN_OBJECT = {
    "run_id", "kind", "status", "claude_version", "started_at", "ended_at", "counts",
    "diagnostics", "attempts", "lifecycle",
}
LIFECYCLE = {"terminal", "source", "activity", "generations"}
ATTEMPT = {
    "agent_id", "key", "label", "phase", "agent_type", "ordinal", "outcome", "first_ts",
    "last_ts", "usage", "event_count", "interrupt", "reported", "facts", "ledger",
}
LEDGER = {"completeness", "reasons", "entries"}
LEDGER_ENTRY = {"effect", "basis", "completion", "path", "repo", "line"}
FINDING = {
    "detector", "severity", "title", "run_id", "agent_id", "evidence", "metrics", "rule_id",
    "rule_ids", "finding_id", "silenced",
}
SCAN_ROW = {
    "run_id", "kind", "date", "project", "agents", "attempts", "verdict", "top_detector",
    "finished", "attention", "attention_reason", "age", "possibly_live", "paths_at_risk",
    "paths_at_risk_complete", "unfinished_state",
}
PLAN_TOP = {
    "schema", "disclosure", "run", "nothing_to_rescue", "groups", "units", "brief", "exit_code",
}
PLAN_RUN = {"run_id", "kind", "finished", "possibly_live"}
PLAN_UNIT = {
    "agent", "phase", "attempts", "end", "task", "reported", "paths", "commits", "in_flight",
    "unknown", "checks", "steps",
}
PLAN_PATH = {"path", "group", "observation", "ledger", "attempts", "later_write_observed"}
PATH_LEDGER = {"attempt", "effect", "basis", "completion", "line"}
STATUS_TOP = {
    "schema", "disclosure", "run", "units", "not_counted", "never_matches",
    "transcript_unknown", "exit_code",
}
STATUS_UNIT = {
    "agent", "repo", "checks_declared", "last_counted_run", "word", "writes_after",
    "write_after_check", "cycles",
}
LIFECYCLE_SOURCES = {
    "notification", "task_stop", "launch_result", "journal", "conflict",
    "unrecognized_status", "incomplete_generation", "parent_unavailable",
    "ambiguous_launch", None,
}


def _run(env, capsys, argv):
    """(exit code, parsed stdout, stderr) of one main() call that must print JSON."""
    code, out, err = cli(capsys, *argv)
    return code, json.loads(out), err


def _per_run(env, capsys, command, label, level, *extra):
    argv = [*command, str(env.run_paths[label]), "--json", *LEVEL_FLAGS[level], *extra]
    return _run(env, capsys, argv)[1]


# --- run and demo -------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_run_json_key_sets(env, capsys, level):
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["run"], label, level)
        assert set(payload) == RUN_TOP, label
        assert payload["schema"] == 3
        assert payload["disclosure"] == level
        assert set(payload["run"]) == RUN_OBJECT, label
        for attempt in payload["run"]["attempts"]:
            assert set(attempt) == ATTEMPT, label
            assert set(attempt["ledger"]) == LEDGER, label
            for entry in attempt["ledger"]["entries"]:
                assert set(entry) == LEDGER_ENTRY, label
        for finding in payload["findings"]:
            assert set(finding) == FINDING, label


def test_demo_json_is_schema_3_at_share(env, capsys):
    payload = _run(env, capsys, ["demo", "--json"])[1]
    assert set(payload) == RUN_TOP
    assert payload["schema"] == 3
    assert payload["disclosure"] == "share"
    assert set(payload["run"]) == RUN_OBJECT
    assert all(set(a) == ATTEMPT for a in payload["run"]["attempts"])


def test_run_observe_adds_the_observation_key_after_the_schema_3_keys(env, capsys):
    payload = _per_run(env, capsys, ["run"], "bare:clean_run", "share", "--observe")
    assert set(payload) == RUN_TOP | {"observation"}


@pytest.mark.parametrize("level", LEVELS)
def test_lifecycle_object_and_folded_types(env, capsys, level):
    seen_workflow = seen_session = False
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["run"], label, level)
        run = payload["run"]
        if run["kind"] == "session":
            seen_session = True
            assert run["lifecycle"] is None, label
        else:
            seen_workflow = True
            lifecycle = run["lifecycle"]
            assert set(lifecycle) == LIFECYCLE, label
            assert lifecycle["terminal"] in {
                "completed", "failed", "stopped", "journal_resolved", "unknown", "none",
            }
            assert lifecycle["source"] in LIFECYCLE_SOURCES
            assert (lifecycle["source"] is None) == (lifecycle["terminal"] == "none")
            assert lifecycle["activity"] in {"recent", "stale", "unknown"}
            assert isinstance(lifecycle["generations"], int)
            assert not isinstance(lifecycle["generations"], bool)
            assert lifecycle["generations"] >= 0
        for attempt in run["attempts"]:
            assert attempt["agent_type"] is None or isinstance(attempt["agent_type"], str)
    assert seen_workflow and seen_session


def test_a_workflow_with_no_launch_has_zero_generations_in_json(env, capsys):
    payload = _per_run(env, capsys, ["run"], "bare:clean_run", "share")
    assert payload["run"]["lifecycle"]["generations"] == 0


# --- the ledger -----------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_every_attempt_has_a_ledger_object_and_some_have_entries(env, capsys, level):
    entries_seen = 0
    for label in _PER_RUN_LABELS:
        for attempt in _per_run(env, capsys, ["run"], label, level)["run"]["attempts"]:
            ledger = attempt["ledger"]
            assert ledger["completeness"] in {"complete", "partial"}
            assert (ledger["completeness"] == "complete") == (ledger["reasons"] == [])
            for entry in ledger["entries"]:
                entries_seen += 1
                assert entry["effect"] in {"observed", "claimed_target", "attempted", "possible"}
                assert entry["path"] is None or isinstance(entry["path"], str)
                assert entry["repo"] is None or isinstance(entry["repo"], str)
                assert re.fullmatch(r".+:[0-9]+", entry["line"])
    assert entries_seen > 0


def test_share_ledger_paths_repos_and_lines_are_pseudonyms_and_aliases(env, capsys):
    real_ids = _real_agent_ids(env)
    checked = 0
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["run"], label, "share")
        for attempt in payload["run"]["attempts"]:
            for entry in attempt["ledger"]["entries"]:
                checked += 1
                for key in ("path", "repo"):
                    assert entry[key] is None or re.fullmatch(r"path-[0-9]+", entry[key]), entry
                file_name = entry["line"].rsplit(":", 1)[0]
                assert not any(real in file_name for real in real_ids), entry
                assert re.fullmatch(
                    r"agent-[0-9a-f]{12,}\.jsonl|agent-[0-9]+\.jsonl|session-[0-9a-f]{12,}\.jsonl"
                    r"|session-[0-9]+\.jsonl|journal\.jsonl|evidence-[0-9]+",
                    file_name,
                ), entry
    assert checked > 0


def test_local_and_raw_ledger_keep_real_names_and_never_an_absolute_path_at_local(env, capsys):
    saw_path = False
    for label in _PER_RUN_LABELS:
        local = _per_run(env, capsys, ["run"], label, "local")
        raw = _per_run(env, capsys, ["run"], label, "raw")
        for attempt in local["run"]["attempts"]:
            for entry in attempt["ledger"]["entries"]:
                assert re.fullmatch(r"agent-[0-9a-f]{17}\.jsonl:[0-9]+", entry["line"]) or (
                    not entry["line"].startswith("agent-")
                ), entry
                for key in ("path", "repo"):
                    if entry[key] is not None:
                        saw_path = True
                        assert not entry[key].startswith("/"), entry
        for attempt in raw["run"]["attempts"]:
            for entry in attempt["ledger"]["entries"]:
                if entry["path"] is not None:
                    assert entry["path"].startswith("/"), entry
    assert saw_path


# --- aliases ----------------------------------------------------------------------------


def _real_agent_ids(env: _Env) -> set[str]:
    ids: set[str] = set()
    for label in _PER_RUN_LABELS:
        run = reader.load_run(env.run_paths[label])
        ids.update(
            a.agent_id for a in run.attempts if disclosure.is_canonical_id("agent", a.agent_id)
        )
    assert ids
    return ids


def test_share_json_never_carries_a_real_agent_id(env, capsys):
    real_ids = _real_agent_ids(env)
    for command in (["run"], ["rescue", "plan"], ["rescue", "status"]):
        for label in _PER_RUN_LABELS:
            argv = [*command, str(env.run_paths[label]), "--json"]
            text = cli(capsys, *argv)[1]
            for real in real_ids:
                assert real not in text, (command, label)


def test_share_agent_ids_are_aliases_or_positional_and_local_raw_are_real(env, capsys):
    real_ids = _real_agent_ids(env)
    alias = re.compile(r"a:[0-9a-f]{12,}|agent-[0-9]+")
    for label in _PER_RUN_LABELS:
        run = reader.load_run(env.run_paths[label])
        canonical = {a.agent_id for a in run.attempts if disclosure.is_canonical_id("agent", a.agent_id)}
        for level in LEVELS:
            payload = _per_run(env, capsys, ["run"], label, level)
            for attempt, source in zip(payload["run"]["attempts"], run.attempts):
                if level == "share":
                    assert alias.fullmatch(attempt["agent_id"]), (label, attempt["agent_id"])
                    if source.agent_id in canonical:
                        assert attempt["agent_id"].startswith("a:"), label
                else:
                    assert attempt["agent_id"] == source.agent_id, (label, level)
            for finding in payload["findings"]:
                if finding["agent_id"] is None:
                    continue
                if level == "share":
                    assert alias.fullmatch(finding["agent_id"]), (label, finding["agent_id"])
                else:
                    assert finding["agent_id"] in real_ids | {a.agent_id for a in run.attempts}


def test_the_alias_in_run_json_is_the_alias_text_prints(env, capsys):
    label = "attempt_facts:wf_00000000-af0"
    run = reader.load_run(env.run_paths[label])
    real = next(a.agent_id for a in run.attempts if disclosure.is_canonical_id("agent", a.agent_id))
    payload = _per_run(env, capsys, ["run"], label, "share")
    shown = {a["agent_id"] for a in payload["run"]["attempts"]}
    stable = disclosure.register([("agent", real)]).alias("agent", real)
    assert f"a:{stable}" in shown


def test_scan_project_is_an_alias_at_share_and_the_directory_name_at_local_and_raw(env, capsys):
    argv = ["scan", "--all", "--json", "--root", str(env.fleet)]
    share = _run(env, capsys, argv)[1]
    local = _run(env, capsys, [*argv, "--local"])[1]
    raw = _run(env, capsys, [*argv, "--raw"])[1]
    assert {row["project"] for row in share["rows"]}
    for row in share["rows"]:
        assert re.fullmatch(r"project:[0-9a-f]{12,}", row["project"]), row["project"]
    for payload in (local, raw):
        for row in payload["rows"]:
            assert not row["project"].startswith("project:"), row["project"]
    assert {r["project"] for r in local["rows"]} == {r["project"] for r in raw["rows"]}


def test_json_falls_back_to_positional_names_and_one_stderr_diagnostic(
    env, capsys, monkeypatch, tmp_path
):
    make_secret_unavailable(monkeypatch, tmp_path)
    label = "attempt_facts:wf_00000000-af0"
    code, out, err = cli(capsys, "run", str(env.run_paths[label]), "--json")
    payload = json.loads(out)
    assert code in (0, 3, 4)
    ids = [a["agent_id"] for a in payload["run"]["attempts"]]
    assert ids and all(re.fullmatch(r"agent-[0-9]+", agent_id) for agent_id in ids)
    assert err.count(disclosure.UNAVAILABLE_DIAGNOSTIC) == 1
    assert out.count(disclosure.UNAVAILABLE_DIAGNOSTIC) == 0


def test_json_registration_leaves_no_diagnostic_when_the_book_is_available(env, capsys):
    label = "attempt_facts:wf_00000000-af0"
    code, out, err = cli(capsys, "run", str(env.run_paths[label]), "--json")
    assert disclosure.UNAVAILABLE_DIAGNOSTIC not in err


# --- no excerpt key at share ------------------------------------------------------------


_SHARE_POINTER = re.compile(
    r"(agent-[0-9a-f]{12,}|agent-[0-9]+|session-[0-9a-f]{12,}|session-[0-9]+)\.jsonl:[0-9]+"
    r"( \[[a-z_,]+\])?|journal\.jsonl:[0-9]+( \[[a-z_,]+\])?|evidence-[0-9]+"
)


def test_share_finding_evidence_is_pointers_only_even_with_evidence(env, capsys):
    seen = 0
    for label in _PER_RUN_LABELS:
        for extra in ([], ["--evidence"]):
            payload = _per_run(env, capsys, ["run"], label, "share", *extra)
            for finding in payload["findings"]:
                for line in finding["evidence"]:
                    seen += 1
                    assert _SHARE_POINTER.fullmatch(line), (label, line)
    assert seen > 0


def test_local_and_raw_evidence_has_excerpts_only_with_the_evidence_flag(env, capsys):
    for level in ("local", "raw"):
        with_excerpt = without = 0
        for label in _PER_RUN_LABELS:
            for line in _evidence_lines(_per_run(env, capsys, ["run"], label, level)):
                if re.fullmatch(r"[A-Za-z0-9._-]+:[0-9]+( \[[a-z_,]+\])?|evidence-[0-9]+", line):
                    without += 1
            for line in _evidence_lines(
                _per_run(env, capsys, ["run"], label, level, "--evidence")
            ):
                if re.search(r":[0-9]+: ", line):
                    with_excerpt += 1
        assert with_excerpt > 0, level
        assert without > 0, level


def _evidence_lines(payload):
    for finding in payload["findings"]:
        yield from finding["evidence"]


def _units_with_tasks(env, capsys, level, *extra):
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["rescue", "plan"], label, level, *extra)
        for unit in payload["units"]:
            if unit["task"] is not None:
                yield label, unit


def test_rescue_plan_task_text_is_absent_at_share_and_without_evidence(env, capsys):
    count = 0
    for level, extra in (("share", []), ("share", ["--evidence"]), ("local", []), ("raw", [])):
        for _label, unit in _units_with_tasks(env, capsys, level, *extra):
            count += 1
            assert set(unit["task"]) == {"pointer", "length"}, (level, extra)
    assert count > 0


@pytest.mark.parametrize("level", ["local", "raw"])
def test_rescue_plan_task_text_exists_at_local_and_raw_only_with_evidence(env, capsys, level):
    count = 0
    for _label, unit in _units_with_tasks(env, capsys, level, "--evidence"):
        count += 1
        assert set(unit["task"]) == {"pointer", "length", "text"}
        assert isinstance(unit["task"]["text"], str)
    assert count > 0


def test_rescue_task_pointer_is_an_aliased_file_at_share(env, capsys):
    real_ids = _real_agent_ids(env)
    for _label, unit in _units_with_tasks(env, capsys, "share"):
        pointer = unit["task"]["pointer"]
        assert pointer is None or _SHARE_POINTER.fullmatch(pointer), pointer
        assert pointer is None or not any(real in pointer for real in real_ids)


def _status_for_excerpts(env):
    run = reader.load_run(env.run_paths["bare:clean_run"])
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[],
        not_counted=[rescue.NotCountedRow(7, "compound command", "uv run pytest -q", "command")],
        never_matches=[rescue.NeverRow(9, "git checkout -- a.py", "git checkout")],
        transcript_unknown=False, checked=False, repo_root=None, exit_code=0,
        listed_paths=(),
    )
    return run, status


@pytest.mark.parametrize("level", LEVELS)
def test_status_excerpt_keys_follow_the_level_and_the_evidence_flag(env, level):
    run, status = _status_for_excerpts(env)
    redactor = Redactor(context=disclosure.DisclosureContext(level=level))
    plain = rescue.status_to_json(run, status, redactor)
    for row in plain["not_counted"] + plain["never_matches"]:
        assert "excerpt" not in row
    assert plain["never_matches"][0]["category"] == "git checkout"
    redactor = Redactor(context=disclosure.DisclosureContext(level=level))
    shown = rescue.status_to_json(run, status, redactor, evidence=True)
    for row in shown["not_counted"] + shown["never_matches"]:
        assert ("excerpt" in row) == (level != "share")


# --- scan, trend, rules -----------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("flags", [[], ["--all"]])
def test_scan_json_key_sets_and_finished_type(env, capsys, level, flags):
    argv = ["scan", *flags, "--json", "--root", str(env.fleet), *LEVEL_FLAGS[level]]
    payload = _run(env, capsys, argv)[1]
    assert set(payload) == {"schema", "disclosure", "rows", "totals"}
    assert payload["schema"] == 3 and payload["disclosure"] == level
    kinds = set()
    for row in payload["rows"]:
        assert set(row) == SCAN_ROW
        kinds.add(row["kind"])
        assert row["finished"] is None or isinstance(row["finished"], bool)
        if row["kind"] == "session":
            assert row["finished"] is None
        else:
            assert isinstance(row["finished"], bool)
    assert kinds == {"workflow", "session"}


@pytest.mark.parametrize("level", LEVELS)
def test_trend_rules_propose_review_json_carry_schema_3_and_disclosure(env, capsys, level):
    flags = LEVEL_FLAGS[level]
    root = ["--root", str(env.fleet)]
    cases = {
        "trend": (["trend", "--weeks", "52", "--json", *root, *flags],
                  {"schema", "disclosure", "weeks", "weekly_counts", "before_after"}),
        "rules": (["rules", "--all", "--json", *root, *flags],
                  {"schema", "disclosure", "rules"}),
        "propose": (["rules", "propose", "--json", *root, *flags],
                    {"schema", "disclosure", "proposals"}),
        "review": (["rules", "review", "--json", *root, *flags],
                   {"schema", "disclosure", "reviews", "consider_removing"}),
    }
    for name, (argv, keys) in cases.items():
        payload = _run(env, capsys, argv)[1]
        assert set(payload) == keys, name
        assert payload["schema"] == 3 and payload["disclosure"] == level, name
    trend_payload = _run(env, capsys, cases["trend"][0])[1]
    assert trend_payload["weeks"] == 52


def test_rules_review_with_nothing_adopted_still_carries_disclosure(
    env, capsys, monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(tmp_path / "no-config.toml"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "other-state"))
    payload = _run(env, capsys, ["rules", "review", "--json", "--root", str(env.fleet)])[1]
    assert payload == {
        "schema": 3, "disclosure": "share", "reviews": [], "consider_removing": [],
    }


@pytest.mark.parametrize("level", LEVELS)
def test_rules_show_json_gains_disclosure_and_still_has_no_schema(env, capsys, level):
    argv = ["rules", "--show", "R-RETRY-SAFE", "--json", *LEVEL_FLAGS[level]]
    payload = _run(env, capsys, argv)[1]
    assert set(payload) == {
        "disclosure", "rule_id", "title", "enforcement", "detectors", "target_kind", "text",
    }
    assert payload["disclosure"] == level


# --- rescue -----------------------------------------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_rescue_plan_json_key_sets_and_the_path_row_split(env, capsys, level):
    rows = 0
    sessions = workflows = 0
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["rescue", "plan"], label, level)
        assert set(payload) == PLAN_TOP, label
        assert payload["schema"] == 3 and payload["disclosure"] == level
        assert set(payload["run"]) == PLAN_RUN
        finished = payload["run"]["finished"]
        if payload["run"]["kind"] == "session":
            sessions += 1
            assert finished is None
        else:
            workflows += 1
            assert isinstance(finished, bool)
        for unit in payload["units"]:
            assert set(unit) == PLAN_UNIT, label
            for row in unit["paths"]:
                rows += 1
                assert set(row) == PLAN_PATH, label
                # N5c-1: null when the path's group was refused or not checked
                if row["observation"] is not None:
                    assert set(row["observation"]) == {"state", "reason"}
                    assert row["observation"]["state"] in {
                        "DIRTY_NOW", "CLEAN_NOW", "MISSING_NOW", "UNKNOWN",
                    }
                assert isinstance(row["ledger"], list)
                for element in row["ledger"]:
                    assert set(element) == PATH_LEDGER
                    assert isinstance(element["attempt"], int)
    assert rows > 0 and sessions and workflows


def test_rescue_path_ledger_is_a_list_one_element_per_entry_in_attempt_order(env, capsys):
    saw_element = False
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["rescue", "plan"], label, "raw")
        for unit in payload["units"]:
            for row in unit["paths"]:
                ordinals = [element["attempt"] for element in row["ledger"]]
                assert ordinals == sorted(ordinals), label
                assert set(ordinals) <= set(row["attempts"]), label
                saw_element = saw_element or bool(row["ledger"])
    assert saw_element


@pytest.mark.parametrize("level", LEVELS)
def test_rescue_status_json_key_sets(env, capsys, level):
    units = 0
    for label in _PER_RUN_LABELS:
        payload = _per_run(env, capsys, ["rescue", "status"], label, level)
        assert set(payload) == STATUS_TOP, label
        assert payload["schema"] == 3 and payload["disclosure"] == level
        assert set(payload["run"]) == {"run_id"}
        for unit in payload["units"]:
            units += 1
            assert set(unit) == STATUS_UNIT
    assert units > 0


# --- --local --json ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["scan", "--json"], ["scan", "--all", "--json"], ["trend", "--json"],
        ["rules", "--json"], ["rules", "propose", "--json"], ["rules", "review", "--json"],
    ],
)
def test_local_json_is_valid_for_every_fleet_command(env, capsys, argv):
    code, out, err = cli(capsys, *argv, "--root", str(env.fleet), "--local")
    assert "applies to text output" not in err
    assert json.loads(out)["disclosure"] == "local"
    assert not out.startswith("disclosure:")


@pytest.mark.parametrize("command", [["run"], ["rescue", "plan"], ["rescue", "status"]])
def test_local_json_is_valid_for_every_per_run_command(env, capsys, command):
    path = env.run_paths["attempt_facts:wf_00000000-af0"]
    code, out, err = cli(capsys, *command, str(path), "--json", "--local")
    assert "applies to text output" not in err
    assert json.loads(out)["disclosure"] == "local"


def test_local_rules_show_json_is_valid(env, capsys):
    code, out, err = cli(capsys, "rules", "--show", "R-RETRY-SAFE", "--json", "--local")
    assert code == 0
    assert json.loads(out)["disclosure"] == "local"


def test_local_json_shows_real_labels_and_no_absolute_path(env, capsys):
    path = env.run_paths["attempt_facts:wf_00000000-af0"]
    run = reader.load_run(path)
    payload = _run(env, capsys, ["run", str(path), "--json", "--local"])[1]
    labels = {a["label"] for a in payload["run"]["attempts"]}
    assert labels == {a.label for a in run.attempts}
    assert not any(label.startswith("agent-") and label[6:].isdigit() for label in labels)
    text = json.dumps(payload)
    assert str(env.root) not in text


# --- observation, aliased ----------------------------------------------------------------


def test_observation_attempts_carry_the_alias_not_the_raw_agent_id(env):
    run = reader.load_run(env.run_paths["attempt_facts:wf_00000000-af0"])
    real = next(a.agent_id for a in run.attempts if disclosure.is_canonical_id("agent", a.agent_id))
    observation = observe.RunObservation([observe.GroupObservation(
        repo="/tmp/repo", refused=False, checked=True, reason=None, repo_root="/tmp/repo",
        paths={
            "/tmp/repo/a.py": observe.PathObservation(
                state=observe.DIRTY_NOW, later_write_observed=False,
                attempts=[{"agent_id": real, "ordinal": 1}],
            ),
        },
    )])
    book = disclosure.register([("agent", real)])
    context = disclosure.DisclosureContext(
        level="share", book=book, identities=(("agent", real),),
    )
    payload = report.to_json(run, [], Redactor(context=context), observation)
    text = json.dumps(payload)
    assert real not in text
    (group,) = payload["observation"]["groups"]
    (row,) = group["paths"].values()
    assert row["attempts"] == [{"agent_id": f"a:{book.alias('agent', real)}", "ordinal": 1}]


# --- exit codes and watch are unchanged ---------------------------------------------------


def test_watch_json_keeps_no_schema_and_no_disclosure_key(env, capsys):
    path = env.run_paths["bare:clean_run"]
    code, out, _err = cli(capsys, "watch", str(path), "--json", "--max-hours", "0.01")
    lines = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert lines
    for event in lines:
        assert "schema" not in event and "disclosure" not in event


# --- the per-path ledger list (4.13.11), built from hand-made attempts ---------------------


def _unit_with_two_writers():
    from agent_postmortem.models import (
        Attempt,
        Ledger,
        LedgerEntry,
        LogicalAgent,
        ToolCall,
    )

    def attempt(ordinal, outcome, calls, entries):
        return Attempt(
            agent_id=f"a{ordinal:016x}", key="k", label="impl:x", phase="Implement",
            agent_type=None, meta={}, transcript_path=None, events=[], first_ts=None,
            last_ts=None, ordinal=ordinal, outcome=outcome, usage={}, tool_calls=calls,
            ledger=Ledger(entries, "complete", []),
        )

    first = attempt(
        1, "killed",
        [ToolCall("t1", "Write", 3, None, path="src/a.py", cwd="/tmp/ws/")],
        [
            LedgerEntry("observed", "tool_result", "success", "/tmp/ws/src/a.py", "/tmp/ws", "f.jsonl:4"),
            LedgerEntry("observed", "tool_result", "success", "/tmp/ws/other.py", "/tmp/ws", "f.jsonl:6"),
        ],
    )
    second = attempt(
        2, "failed",
        [ToolCall("t9", "Edit", 8, None, path="/tmp/ws/src/a.py", cwd="/tmp/elsewhere")],
        [LedgerEntry("attempted", "tool_result", "error", "/tmp/ws/src/a.py", None, "g.jsonl:9")],
    )
    return LogicalAgent(key="k", label="impl:x", phase="Implement", attempts=[second, first])


def test_path_ledger_matches_a_relative_path_by_the_call_cwd_in_attempt_order():
    unit = _unit_with_two_writers()
    pairs = rescue._path_ledger(unit, "src/a.py")
    assert [(ordinal, entry.line) for ordinal, entry in pairs] == [(1, "f.jsonl:4")]
    pairs = rescue._path_ledger(unit, "/tmp/ws/src/a.py")
    assert [(ordinal, entry.effect) for ordinal, entry in pairs] == [(2, "attempted")]


def test_path_ledger_is_empty_when_no_entry_names_the_path():
    assert rescue._path_ledger(_unit_with_two_writers(), "nowhere.py") == []


# --- registration at share, through the same helper text uses ---------------------------------


@pytest.fixture
def books(monkeypatch):
    created: list[disclosure.AliasBook] = []
    real_register = disclosure.register

    def recording_register(identities, **kwargs):
        book = real_register(identities, **kwargs)
        created.append(book)
        return book

    monkeypatch.setattr(disclosure, "register", recording_register)
    return created


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "{run}", "--json"],
        ["rescue", "plan", "{run}", "--json"],
        ["rescue", "status", "{run}", "--json"],
        ["scan", "--all", "--json", "--root", "{fleet}"],
        ["scan", "--json", "--root", "{fleet}"],
    ],
)
def test_share_json_registers_once_and_never_takes_the_slow_path(env, capsys, books, argv):
    run = str(env.run_paths["attempt_facts:wf_00000000-af0"])
    argv = [item.format(run=run, fleet=env.fleet) for item in argv]
    cli(capsys, *argv)
    assert len(books) == 1
    assert books[0].slow_path_count == 0


@pytest.mark.parametrize(
    "argv",
    [["trend", "--json"], ["rules", "--all", "--json"], ["rules", "propose", "--json"]],
)
def test_json_of_a_command_that_shows_no_identity_registers_nothing(env, capsys, books, argv):
    cli(capsys, *argv, "--root", str(env.fleet))
    assert books == []
