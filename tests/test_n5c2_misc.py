"""N5c-2: a unit's own brief and checks, the commit wording, the launch predicate
(docs/DESIGN.md 4.11, 4.13.1, 5.3b; plan S7 and S11).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_postmortem import reader, rescue
from tests.n5c1_helpers import Att, Step, bash_step, init_repo, write_step
from tests.n5c2_helpers import build_scenario, run_cli
from tests.test_field_v03 import (
    FAKE_SLUG,
    FAKE_UUID,
    _aid,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

DESIGN = Path(__file__).resolve().parent.parent / "docs" / "DESIGN.md"

def _dirs(tmp_path, *names):
    out = []
    for name in names:
        path = tmp_path / "w" / name
        path.mkdir(parents=True, exist_ok=True)
        out.append(path)
    return out


# --------------------------------------------------------------------------------------------
# a unit's relaunch brief comes from its own ledger only
# --------------------------------------------------------------------------------------------


def test_each_unit_lists_only_its_own_paths_ledger_commits_and_in_flight(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    commit_a = Step("a2", "Bash", {"command": "git commit -m a"}, "[main aaaaaaa] a", False, str(repo_a))
    commit_b = Step("b2", "Bash", {"command": "git commit -m b"}, "[main bbbbbbb] b", False, str(repo_b))
    in_flight_b = Step("b3", "Edit", {"file_path": str(repo_b / "late.py")}, None, False, str(repo_b))
    attempts = [
        Att(1, key="v2:a", steps=[write_step("a1", repo_a / "f.py", repo_a), commit_a]),
        Att(2, key="v2:b", start=40, steps=[
            write_step("b1", repo_b / "f.py", repo_b), commit_b, in_flight_b,
        ]),
    ]
    scenario = build_scenario(tmp_path, monkeypatch, "5c8", attempts)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--json", "--raw"])
    units = {unit["agent"]: unit for unit in json.loads(out)["units"]}
    unit_a, unit_b = units["impl-1"], units["impl-2"]

    assert [row["path"] for row in unit_a["paths"]] == [str(repo_a / "f.py")]
    assert [row["path"] for row in unit_b["paths"]] == [str(repo_b / "f.py"), str(repo_b / "late.py")]
    assert unit_a["commits"] == ["aaaaaaa"]
    assert unit_b["commits"] == ["bbbbbbb"]
    assert unit_a["in_flight"] == []
    assert [entry["path"] for entry in unit_b["in_flight"]] == [str(repo_b / "late.py")]

    # a ledger element names its own unit's transcript, and its own attempt ordinal only
    id_a, id_b = _aid(1), _aid(2)
    for row in unit_a["paths"]:
        assert row["ledger"]
        assert all(e["line"].startswith(f"agent-{id_a}.jsonl:") for e in row["ledger"])
    for row in unit_b["paths"]:
        assert row["ledger"]
        assert all(e["line"].startswith(f"agent-{id_b}.jsonl:") for e in row["ledger"])
    assert unit_a["task"]["pointer"].startswith(f"agent-{id_a}.jsonl:")
    assert unit_b["task"]["pointer"].startswith(f"agent-{id_b}.jsonl:")


def test_two_units_writing_the_same_path_each_list_only_their_own_ledger_entries(
    tmp_path, monkeypatch, capsys,
):
    (repo,) = _dirs(tmp_path, "a")
    attempts = [
        Att(1, key="v2:a", steps=[write_step("a1", repo / "shared.py", repo)]),
        Att(2, key="v2:b", start=40, steps=[
            write_step("b1", repo / "shared.py", repo), write_step("b2", repo / "shared.py", repo),
        ]),
    ]
    scenario = build_scenario(tmp_path, monkeypatch, "5c8", attempts)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--json", "--raw"])
    units = {unit["agent"]: unit for unit in json.loads(out)["units"]}
    (row_a,) = units["impl-1"]["paths"]
    (row_b,) = units["impl-2"]["paths"]
    assert [e["attempt"] for e in row_a["ledger"]] == [1]
    assert len(row_b["ledger"]) == 2 and {e["attempt"] for e in row_b["ledger"]} == {1}
    assert all(f"agent-{_aid(1)}" in e["line"] for e in row_a["ledger"])
    assert all(f"agent-{_aid(2)}" in e["line"] for e in row_b["ledger"])


# --------------------------------------------------------------------------------------------
# a unit's CHECKS come from its own declared repositories, never from an observation
# --------------------------------------------------------------------------------------------


def test_each_unit_names_the_checks_of_its_own_declared_repository(tmp_path, monkeypatch, capsys):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [
        Att(1, key="v2:a", steps=[write_step("a1", repo_a / "f.py", repo_a)]),
        Att(2, key="v2:b", start=40, steps=[write_step("b1", repo_b / "f.py", repo_b)]),
    ]
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c9", attempts,
        repos=[(str(repo_a), ["make a"]), (str(repo_b), ["make b"])],
    )
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--raw"])
    blocks = out.split("UNIT ")[1:]
    assert "CHECKS    declared for this repo: make a" in blocks[0]
    assert "make b" not in blocks[0]
    assert "CHECKS    declared for this repo: make b" in blocks[1]
    assert "make a" not in blocks[1]
    _code, out, _err = run_cli(
        capsys, ["rescue", "plan", str(scenario.run_dir), "--json", "--raw"],
    )
    units = {unit["agent"]: unit for unit in json.loads(out)["units"]}
    assert units["impl-1"]["checks"] == ["make a"]
    assert units["impl-2"]["checks"] == ["make b"]


def test_a_unit_that_wrote_in_two_declared_repositories_lists_both_repositories_checks(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [Att(1, steps=[
        write_step("a1", repo_a / "f.py", repo_a), write_step("a2", repo_b / "f.py", repo_b),
    ])]
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c9", attempts,
        repos=[(str(repo_a), ["make a"]), (str(repo_b), ["make b"])],
    )
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--raw"])
    assert "CHECKS    declared across 2 repositories: make a; make b" in out
    assert (
        "[decide] run the declared checks, each from its own repository root: make a; make b"
        in out
    )
    _code, out, _err = run_cli(
        capsys, ["rescue", "plan", str(scenario.run_dir), "--json", "--raw"],
    )
    (unit,) = json.loads(out)["units"]
    assert unit["checks"] == ["make a", "make b"]


def test_a_refused_group_still_finds_its_units_declared_checks_in_the_plan(
    tmp_path, monkeypatch, capsys,
):
    (repo_a,) = _dirs(tmp_path, "a")
    attempts = [Att(1, steps=[
        write_step("a1", repo_a / "f.py", repo_a), bash_step("a2", "make", repo_a),
    ])]
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c9", attempts, repos=[(str(repo_a), ["make a"])],
    )
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--raw"])
    assert "CHECKS    declared for this repo: make a" in out


def test_the_transitional_checks_root_is_gone():
    assert not hasattr(rescue, "_checks_root")


# --------------------------------------------------------------------------------------------
# commit ids are unverified, summary-shaped output lines: no rendering says one was created
# --------------------------------------------------------------------------------------------

_CREATED = re.compile(
    r"\bcommits?\b[^.;\n]{0,40}\b(?:created|exists?|made|landed|recorded|succeeded)\b"
    r"|\b(?:created|made|exists?)\b[^.;\n]{0,20}\bcommits?\b"
    r"|already exists:[^;\n]*\bcommits?\b",
    re.IGNORECASE,
)


def _commit_run(tmp_path, monkeypatch):
    (repo,) = _dirs(tmp_path, "a")
    attempts = [
        Att(1, key="v2:a", steps=[
            write_step("a1", repo / "f.py", repo),
            Step("a2", "Bash", {"command": "git commit -m wip"}, "[main abc1234] wip", False, str(repo)),
        ]),
    ]
    return build_scenario(tmp_path, monkeypatch, "5ca", attempts).run_dir


@pytest.mark.parametrize("level", [[], ["--local"], ["--raw"]], ids=["share", "local", "raw"])
def test_no_rendering_says_a_commit_was_created_or_exists(level, tmp_path, monkeypatch, capsys):
    run_dir = _commit_run(tmp_path, monkeypatch)
    texts = []
    for argv in (
        ["run", str(run_dir), *level],
        ["run", str(run_dir), "--verbose", *level],
        ["rescue", "plan", str(run_dir), *level],
        ["rescue", "status", str(run_dir), *level],
        ["scan", "--root", str(tmp_path / "root"), *level],
        ["scan", "--root", str(tmp_path / "root"), "--all", *level],
    ):
        _code, out, _err = run_cli(capsys, argv)
        texts.append((argv, out))
    seen_commit_word = False
    for argv, text in texts:
        for line in text.splitlines():
            if re.search(r"\bcommits?\b", line, re.IGNORECASE):
                seen_commit_word = True
            assert not _CREATED.search(line), (argv, line)
    assert seen_commit_word, "the scenario must make some rendering mention commits"


def test_the_renderings_keep_the_words_commits_and_reported(tmp_path, monkeypatch, capsys):
    run_dir = _commit_run(tmp_path, monkeypatch)
    _code, out, _err = run_cli(capsys, ["run", str(run_dir), "--raw"])
    assert "COMMITS" in out
    assert re.search(r"WORK AT RISK .*1 commit reported", out)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--raw"])
    assert "COMMITS   reported by these attempts: abc1234" in out


def test_the_relaunch_brief_says_commits_reported_not_that_they_exist(tmp_path, monkeypatch, capsys):
    run_dir = _commit_run(tmp_path, monkeypatch)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--raw"])
    (line,) = [line for line in out.splitlines() if line.startswith("  already exists:")]
    assert line == "  already exists: 1 paths (" + str(_dirs(tmp_path, "a")[0] / "f.py") + "); commits reported: 1 (abc1234)"
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(run_dir), "--json", "--raw"])
    assert json.loads(out)["brief"]["already_exists"]["commits"] == ["abc1234"]


def test_design_documents_commits_as_unverified_summary_shaped_output_lines():
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index("### 5.3b JSON schema 3")
    section = text[start:]
    assert "unverified," in section and "summary-shaped output lines" in section


# --------------------------------------------------------------------------------------------
# reader._launch_code: the launch line predicate is kept, with its reachable case
# --------------------------------------------------------------------------------------------


def _session_with_a_workflow_launch(tmp_path, *, lone_carriage_return: bool):
    """A session main transcript with one Workflow launch whose run directory exists.
    `lone_carriage_return` puts a carriage return before the launch's line, with no
    newline after it: the transcript read (universal newlines) numbers that launch one line
    later than the parent index, which splits on newline bytes only."""
    from datetime import datetime, timedelta, timezone

    from tests.test_field_v03 import _assistant_text, _user, _wf_dir

    root = tmp_path / "root"
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    wf = "wf_00000000-5cb"
    run_dir = _wf_dir(root, wf)
    agent = _aid(1)
    _write_jsonl(run_dir / "journal.jsonl", [
        {"type": "started", "key": "v2:x", "agentId": agent, "label": "l", "phase": "p"},
        {"type": "result", "key": "v2:x", "agentId": agent, "result": {"ok": True}},
    ])
    _write_jsonl(run_dir / f"agent-{agent}.jsonl", [
        _user("hi", base, agent), _assistant_text("done", base + timedelta(seconds=1), agent),
    ])
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    launch = json.dumps(_workflow_launch("toolu_A", base))
    lines = [
        json.dumps(_user("start", base)),
        ("\r" if lone_carriage_return else "") + launch,
        json.dumps(_workflow_launch_result("toolu_A", base + timedelta(seconds=1), wf, "tA")),
    ]
    main.parent.mkdir(parents=True, exist_ok=True)
    main.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return main


def test_a_workflow_launch_whose_generation_names_the_same_line_reconciles(tmp_path):
    main = _session_with_a_workflow_launch(tmp_path, lone_carriage_return=False)
    run = reader.load_session(main)
    assert run.discovery["transcripts"] == []


def test_a_workflow_launch_the_parent_index_places_on_another_line_is_not_reconciled(tmp_path):
    # The two reads of the parent transcript are separate (the index is a cached, incremental
    # read), so the launch's own line is part of what must agree. When it does not, the launch
    # is reported as undiscovered rather than matched by its id alone (4.13.1).
    main = _session_with_a_workflow_launch(tmp_path, lone_carriage_return=True)
    run = reader.load_session(main)
    assert run.discovery["transcripts"] == [
        f"{main.name}:3: Workflow launch has no discovered child (undiscovered)"
    ]


def test_design_states_why_the_launch_line_must_agree():
    text = DESIGN.read_text(encoding="utf-8")
    assert "the launch's own line is part of what must agree" in text



def test_a_check_two_repositories_declare_is_listed_once_for_a_unit_that_wrote_in_both(
    tmp_path, monkeypatch, capsys,
):
    repo_a, repo_b = _dirs(tmp_path, "a", "b")
    attempts = [Att(1, steps=[
        write_step("a1", repo_a / "f.py", repo_a), write_step("a2", repo_b / "f.py", repo_b),
    ])]
    scenario = build_scenario(
        tmp_path, monkeypatch, "5c9", attempts,
        repos=[(str(repo_a), ["make check"]), (str(repo_b), ["make check"])],
    )
    _code, out, _err = run_cli(
        capsys, ["rescue", "plan", str(scenario.run_dir), "--json", "--raw"],
    )
    (unit,) = json.loads(out)["units"]
    assert unit["checks"] == ["make check"]


def test_plan_exit_code_is_0_for_an_unfinished_run_whose_files_are_old_and_whose_lifecycle_is_none(
    tmp_path, monkeypatch, capsys,
):
    # The lifecycle terminal `none` (no notification yet) is not `unknown`: an unfinished,
    # quiet run with every fact known is exit 0 for the plan, as before.
    from tests.n5c2_helpers import age_files

    repo = init_repo(tmp_path / "repo")
    attempts = [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:b", journal="open", start=40),
    ]
    scenario = build_scenario(tmp_path, monkeypatch, "5cd", attempts, parent="running")
    age_files(tmp_path)
    code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--json"])
    assert json.loads(out)["run"]["finished"] is False
    assert code == 0


def test_the_relaunch_brief_says_zero_commits_reported_when_none_was(tmp_path, monkeypatch, capsys):
    (repo,) = _dirs(tmp_path, "a")
    attempts = [Att(1, steps=[write_step("a1", repo / "f.py", repo)])]
    scenario = build_scenario(tmp_path, monkeypatch, "5ca", attempts)
    _code, out, _err = run_cli(capsys, ["rescue", "plan", str(scenario.run_dir), "--raw"])
    assert f"  already exists: 1 paths ({repo / 'f.py'}); commits reported: 0" in out.splitlines()
