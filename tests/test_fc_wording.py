"""Part FC, P5 to P8 and P10 (field milestone F): wording that told the reader something false
or too little (docs/DESIGN.md 4.7, 4.9, 4.3; .reviews/v0.3/F-plan.md FC).

- P5: `WORK AT RISK  none:` for an empty population no longer claims that "killed or failed
  attempts wrote no paths" when nothing was killed or failed.
- P6: the unassignable line names the attempt and the reason it could not be placed.
- P7: a refused repository says why and prints the manual command.
- P8: an empty REPO STATE says what its scope is.
- P10: `bash_timeout`'s title says "hit a hard timeout" only when every hit has the anchored
  timeout marker.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_postmortem import cli, ledger, reader, report
from agent_postmortem.detectors import bash_timeout
from tests.n5c1_helpers import (
    Att,
    Step,
    _aid,
    bash_step,
    build_run,
    init_repo,
    session_with_children,
    write_step,
)
from tests.test_detectors_tooling import _build_run
from tests.test_fa_generations import (
    _attempt,
    _generation,
)
from tests.test_fa_generations import (
    _run as _model_run,
)
from tests.test_field_v03 import (
    _assistant_text,
    _envelope,
    _no_config,
    _notification_entry,
    _parent_path,
    _user,
    _wf_dir,
    _workflow_launch,
    _workflow_launch_result,
    _write_jsonl,
)

DESIGN = (Path(__file__).parent.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8")
FLAT = " ".join(DESIGN.split())  # the contract wraps lines; compare with whitespace collapsed


def _flat(text: str) -> str:
    return " ".join(text.split())
LEVELS = [("share", []), ("local", ["--local"]), ("raw", ["--raw"])]
ALIAS = r"a:[0-9a-f]{12,}"

NONE_EMPTY = "WORK AT RISK  none: no attempt was killed, failed, or left unfinished."
NONE_NO_WRITE = "WORK AT RISK  none: the attempts at risk wrote no paths."
OLD_NONE = "killed or failed attempts wrote no paths"


def _out(capsys, target, *flags) -> str:
    cli.main(["run", str(target), *flags])
    return capsys.readouterr().out


def _risk_line(out: str) -> str:
    return next(line for line in out.splitlines() if line.startswith("WORK AT RISK"))


def _block(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("WORK AT RISK"))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


# --- P5 -------------------------------------------------------------------------------------


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p5_an_empty_population_says_nothing_was_killed_failed_or_unfinished(
    tmp_path, monkeypatch, capsys, level, flags,
):
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c20", [Att(1, journal="completed")])
    out = _out(capsys, run_dir, *flags)
    assert _block(out) == [NONE_EMPTY]
    assert OLD_NONE not in out


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p5_a_non_empty_complete_population_that_wrote_nothing_says_the_attempts_at_risk_wrote_no_paths(
    tmp_path, monkeypatch, capsys, level, flags,
):
    read_only = Step("t1", "Read", {"file_path": "/tmp/ws/x"}, "text", False, "/tmp/ws")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c21", [Att(1, steps=[read_only])])
    out = _out(capsys, run_dir, *flags)
    assert _block(out) == [NONE_NO_WRITE]
    assert OLD_NONE not in out


def test_p5_a_session_with_no_subagent_at_risk_says_nothing_was_killed(tmp_path, capsys):
    main, _ids = session_with_children(tmp_path, [(70, [], False, True), (71, [], False, True)])
    assert _block(_out(capsys, main, "--raw")) == [NONE_EMPTY]


def test_p5_a_session_whose_killed_subagent_wrote_nothing_says_the_attempts_at_risk_wrote_no_paths(
    tmp_path, capsys,
):
    read_only = Step("t1", "Read", {"file_path": "/tmp/ws/x"}, "text", False, "/tmp/ws")
    main, _ids = session_with_children(tmp_path, [(72, [read_only], True, True)])
    assert _block(_out(capsys, main, "--raw")) == [NONE_NO_WRITE]


def test_p5_the_none_words_never_replace_a_non_none_block(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c22", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    out = _out(capsys, run_dir, "--raw")
    assert "none:" not in _risk_line(out)
    assert _risk_line(out).startswith("WORK AT RISK  1 attempt at risk: 1 path named")


def test_p5_the_old_sentence_exists_nowhere_in_the_source_or_the_contract():
    source = Path(report.__file__).read_text(encoding="utf-8")
    assert OLD_NONE not in source
    assert "none: no attempt was killed, failed, or left unfinished." in source
    assert "none: the attempts at risk wrote no paths." in source
    assert "none: no attempt was killed, failed, or left unfinished." in FLAT
    assert "none: the attempts at risk wrote no paths." in FLAT
    assert "`WORK AT RISK  none: killed or failed attempts wrote no paths.`" not in DESIGN


# --- P6: the unassignable line names the attempt and the reason ----------------------------------


def test_p6_the_model_names_each_of_the_three_reasons():
    before = _model_run([_attempt(0.4)], [_generation(1, 0.5, 1.0)])
    assert ledger.unassignable_reason(before, before.attempts[0]) == "before_launch"
    no_first = _model_run([_attempt(None)], [_generation(1, 0.0, 1.0)])
    assert ledger.unassignable_reason(no_first, no_first.attempts[0]) == "no_usable_timestamp"
    no_generation = _model_run([_attempt(3.0)], [])
    assert ledger.unassignable_reason(no_generation, no_generation.attempts[0]) == (
        "no_launch_record"
    )
    missing = _model_run(
        [_attempt(10.0)], [_generation(1, 0.0, 1.0), _generation(2, 0.0, 101.0, launch=False)],
    )
    assert ledger.unassignable_reason(missing, missing.attempts[0]) == "launch_times_unusable"
    flat = _model_run([_attempt(10.0)], [_generation(1, 5.0, 6.0), _generation(2, 5.0, 7.0)])
    assert ledger.unassignable_reason(flat, flat.attempts[0]) == "launch_times_unusable"


def test_p6_launch_times_are_checked_before_the_attempts_own_timestamp():
    both = _model_run([_attempt(None)], [])
    assert ledger.unassignable_reason(both, both.attempts[0]) == "no_launch_record"


def test_p6_a_placed_attempt_and_a_session_attempt_have_no_reason():
    placed = _model_run([_attempt(0.6)], [_generation(1, 0.0, 1.0)])
    assert ledger.unassignable_reason(placed, placed.attempts[0]) is None
    session = _model_run([_attempt(0.4)], [_generation(1, 0.5, 1.0)])
    session.kind = "session"
    assert ledger.unassignable_reason(session, session.attempts[0]) is None


WF = "wf_00000000-c23"


def _launched(tmp_path, monkeypatch, *, agent_rows, launch_ts=True, journal_extra=None):
    """A workflow run with one Workflow launch call at T0; `agent_rows` is a list of
    (agent number, first entry offset in seconds from T0, extra leading entries)."""
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    run_dir = _wf_dir(root, WF)
    base = datetime.now(timezone.utc) - timedelta(minutes=20)
    journal = []
    for number, _offset, _lead in agent_rows:
        agent = _aid(number)
        journal.append({"type": "started", "key": f"v2:k{number}", "agentId": agent,
                        "label": f"card {number}", "phase": "Build"})
        journal.append({"type": "result", "key": f"v2:k{number}", "agentId": agent,
                        "result": {"ok": True}})
    _write_jsonl(run_dir / "journal.jsonl", journal)
    if journal_extra:
        with (run_dir / "journal.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(journal_extra)
    for number, offset, lead in agent_rows:
        agent = _aid(number)
        first = base + timedelta(seconds=offset)
        _write_jsonl(run_dir / f"agent-{agent}.jsonl", [
            *lead,
            _user("Implement the card.", first, agent),
            _assistant_text("Done.", first + timedelta(seconds=5), agent),
        ])
    launch = _workflow_launch("toolu_F", base)
    if not launch_ts:
        del launch["timestamp"]
    _write_jsonl(_parent_path(root), [
        launch,
        _workflow_launch_result("toolu_F", base + timedelta(seconds=1), WF, "tF"),
        _notification_entry(base + timedelta(minutes=10), _envelope("tF", "toolu_F", "completed")),
    ])
    return run_dir


def _incomplete_line(out: str) -> str:
    return next(
        line for line in out.splitlines()
        if line.startswith("  the list of attempts at risk may be incomplete: ")
    )


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p6_an_attempt_that_started_before_the_run_was_launched(
    tmp_path, monkeypatch, capsys, level, flags,
):
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=[(0x6A1, -1.5, []), (0x6A2, 2.0, [])])
    out = _out(capsys, run_dir, *flags)
    ident = ALIAS if level == "share" else re.escape(_aid(0x6A1))
    assert re.fullmatch(
        r"  the list of attempts at risk may be incomplete: " + ident +
        r" could not be placed in a launch of this run \(it started before the run was "
        r"launched\)\.", _incomplete_line(out)), _incomplete_line(out)
    assert "could not be placed in time" not in out


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p6_an_early_line_with_no_usable_timestamp(tmp_path, monkeypatch, capsys, level, flags):
    undated = _assistant_text("early note", datetime.now(timezone.utc), _aid(0x6B1))
    del undated["timestamp"]
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=[(0x6B1, 2.0, [undated])])
    out = _out(capsys, run_dir, *flags)
    ident = ALIAS if level == "share" else re.escape(_aid(0x6B1))
    assert re.fullmatch(
        r"  the list of attempts at risk may be incomplete: " + ident +
        r" could not be placed in a launch of this run \(an early line has no usable "
        r"timestamp\)\.", _incomplete_line(out)), _incomplete_line(out)


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p6_launch_times_that_are_missing(tmp_path, monkeypatch, capsys, level, flags):
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=[(0x6C1, 2.0, [])], launch_ts=False)
    out = _out(capsys, run_dir, *flags)
    ident = ALIAS if level == "share" else re.escape(_aid(0x6C1))
    assert re.fullmatch(
        r"  the list of attempts at risk may be incomplete: " + ident +
        r" could not be placed in a launch of this run \(the launch times are missing or out of "
        r"order\)\.", _incomplete_line(out)), _incomplete_line(out)


def test_p6_at_most_three_attempts_are_named_and_the_rest_are_counted(
    tmp_path, monkeypatch, capsys,
):
    rows = [(0x6D0 + n, -2.0 - n, []) for n in range(5)]
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=rows)
    line = _incomplete_line(_out(capsys, run_dir, "--raw"))
    named = [_aid(0x6D0 + n) for n in range(5)]
    assert not any(real in line for real in named[3:])
    assert line == (
        f"  the list of attempts at risk may be incomplete: {named[0]}, {named[1]}, {named[2]} "
        "and 2 more could not be placed in a launch of this run (it started before the run "
        "was launched)."
    )


def test_p6_one_attempt_over_three_reads_and_1_more(tmp_path, monkeypatch, capsys):
    rows = [(0x6E0 + n, -2.0 - n, []) for n in range(4)]
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=rows)
    line = _incomplete_line(_out(capsys, run_dir, "--raw"))
    named = [_aid(0x6E0 + n) for n in range(3)]
    assert line.endswith(
        f"{named[0]}, {named[1]}, {named[2]} and 1 more could not be placed in a launch of this "
        "run (it started before the run was launched)."
    ), line


def test_p6_two_attempts_with_one_reason_share_one_phrase(tmp_path, monkeypatch, capsys):
    rows = [(0x6E8, -2.0, []), (0x6E9, -3.0, [])]
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=rows)
    line = _incomplete_line(_out(capsys, run_dir, "--raw"))
    assert line == (
        f"  the list of attempts at risk may be incomplete: {_aid(0x6E8)} and {_aid(0x6E9)} could "
        "not be placed in a launch of this run (it started before the run was launched)."
    )


def test_p6_three_attempts_with_one_reason_are_listed_without_a_count(
    tmp_path, monkeypatch, capsys,
):
    rows = [(0x6EA + n, -2.0 - n, []) for n in range(3)]
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=rows)
    line = _incomplete_line(_out(capsys, run_dir, "--raw"))
    ids = [_aid(0x6EA + n) for n in range(3)]
    assert line == (
        f"  the list of attempts at risk may be incomplete: {ids[0]}, {ids[1]} and {ids[2]} "
        "could not be placed in a launch of this run (it started before the run was launched)."
    )


def test_p6_different_reasons_get_their_own_phrases_in_attempt_order(
    tmp_path, monkeypatch, capsys,
):
    undated = _assistant_text("early note", datetime.now(timezone.utc), _aid(0x6EE))
    del undated["timestamp"]
    rows = [(0x6ED, -2.0, []), (0x6EE, 2.0, [undated])]
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=rows)
    line = _incomplete_line(_out(capsys, run_dir, "--raw"))
    assert line == (
        f"  the list of attempts at risk may be incomplete: {_aid(0x6ED)} could not be placed in "
        "a launch of this run (it started before the run was launched); "
        f"{_aid(0x6EE)} could not be placed in a launch of this run (an early line has no "
        "usable timestamp)."
    )


def test_p6_the_words_join_with_the_discovery_reason(tmp_path, monkeypatch, capsys):
    run_dir = _launched(
        tmp_path, monkeypatch, agent_rows=[(0x6F1, -1.5, [])], journal_extra="not json at all\n",
    )
    line = _incomplete_line(_out(capsys, run_dir, "--raw"))
    assert line == (
        f"  the list of attempts at risk may be incomplete: {_aid(0x6F1)} could not be placed in "
        "a launch of this run (it started before the run was launched); some run files could "
        "not be read or matched."
    )


def test_p6_a_placed_run_prints_no_such_line(tmp_path, monkeypatch, capsys):
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=[(0x6A1, 0.5, [])])
    out = _out(capsys, run_dir, "--raw")
    assert "could not be placed" not in out


def test_p6_json_reason_codes_are_unchanged(tmp_path, monkeypatch, capsys):
    run_dir = _launched(tmp_path, monkeypatch, agent_rows=[(0x6A1, -1.5, [])])
    cli.main(["run", str(run_dir), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    (attempt,) = payload["run"]["attempts"]
    assert "unassignable_attempt" in attempt["ledger"]["reasons"]
    assert "could not be placed" not in json.dumps(payload)


def test_p6_the_contract_names_the_three_reasons():
    for words in (
        "could not be placed in a launch of this run",
        "it started before the run was launched",
        "an early line has no usable timestamp",
        "the launch times are missing or out of order",
    ):
        assert words in FLAT
    assert '"an attempt could not be placed in time"' not in FLAT


# --- P7 and P8: REPO STATE ------------------------------------------------------------------


REFUSED = (
    "checked: no (its ledger is partial: incomplete evidence may hide more writes, so the "
    "tool does not run status for it)"
)


def _shell_partial(tmp_path, monkeypatch, suffix="c24"):
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo)]),
    ])
    return run_dir, repo


def _unassignable_partial(tmp_path, monkeypatch, suffix="c25"):
    """A non-shell partial: the attempt wrote a path in the repository but started before the
    run was launched, so its ledger carries `unassignable_attempt`."""
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Att(1, journal="failed", start=-5, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    return run_dir, repo


def _undecodable_partial(tmp_path, monkeypatch, suffix="c35"):
    """A non-shell partial: one undecodable line in the attempt's transcript."""
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", [
        Att(1, journal="failed", steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    with (run_dir / f"agent-{_aid(1)}.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{ this line is not json\n")
    return run_dir, repo


PARTIALS = pytest.mark.parametrize(
    "make", [_shell_partial, _unassignable_partial, _undecodable_partial],
    ids=["a shell partial", "a non-shell partial (unassignable)", "a non-shell partial (undecodable line)"],
)


def _repo_state(out: str) -> list[str]:
    lines = out.splitlines()
    start = lines.index("REPO STATE")
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


@PARTIALS
def test_p7_a_refused_repository_says_why_at_raw(tmp_path, monkeypatch, capsys, make):
    run_dir, repo = make(tmp_path, monkeypatch)
    block = _repo_state(_out(capsys, run_dir, "--raw", "--observe"))
    assert f"  repository {repo}  {REFUSED}" in block
    index = block.index(f"  repository {repo}  {REFUSED}")
    assert block[index + 1] == f"    run yourself: git -C {repo} status --short"
    assert block[index + 2] == f"    {repo / 'tracked.txt'}  not observed"
    assert "(refused)" not in "\n".join(line for line in block if str(repo) in line)


@PARTIALS
def test_p7_a_refused_repository_says_why_at_share_with_an_alias_only(
    tmp_path, monkeypatch, capsys, make,
):
    run_dir, repo = make(tmp_path, monkeypatch)
    out = _out(capsys, run_dir, "--observe")
    block = _repo_state(out)
    index = next(i for i, line in enumerate(block) if line.endswith(REFUSED))
    match = re.fullmatch(r"  repository (path-\d+)  " + re.escape(REFUSED), block[index])
    assert match, block[index]
    alias = match.group(1)
    assert block[index + 1] == (
        f"    run yourself: git status --short in {alias}; --raw prints the exact command"
    )
    assert re.fullmatch(r"    path-\d+  not observed", block[index + 2])
    assert str(repo) not in out and "tracked.txt" not in out


@PARTIALS
def test_p7_the_repository_state_command_is_the_work_at_risk_command_indented(
    tmp_path, monkeypatch, capsys, make,
):
    run_dir, _repo = make(tmp_path, monkeypatch)
    for flags in (["--observe"], ["--local", "--observe"], ["--raw", "--observe"]):
        out = _out(capsys, run_dir, *flags)
        manual = [
            line for line in _block(out)
            if line.startswith("  run yourself: git ") and "the agent worked in" not in line
        ]
        assert manual, out
        state = _repo_state(out)
        assert ["  " + line for line in manual] == [
            line for line in state if line.startswith("    run yourself: git ")
        ]


def test_p7_a_missing_transcript_has_no_named_repository_so_only_the_unknown_group_is_refused(
    tmp_path, monkeypatch, capsys,
):
    """The missing attempt names no repository (it has no entries), so its refusal lands in the
    unknown group, whose wording is unchanged; the named repository stays checked."""
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c36", [
        Att(1, key="v2:a", journal="failed", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:a", journal="failed", start=40),
    ])
    (run_dir / f"agent-{_aid(2)}.jsonl").unlink()
    block = _repo_state(_out(capsys, run_dir, "--raw", "--observe"))
    assert f"  repository {repo}  checked: yes" in block
    assert "  repository unknown  checked: no (refused)" in block


def test_p7_the_unknown_group_keeps_its_refused_wording(tmp_path, monkeypatch, capsys):
    run_dir, _repo = _shell_partial(tmp_path, monkeypatch, "c26")
    block = _repo_state(_out(capsys, run_dir, "--raw", "--observe"))
    assert "  repository unknown  checked: no (refused)" in block


def test_p7_json_reason_codes_are_unchanged(tmp_path, monkeypatch, capsys):
    run_dir, repo = _shell_partial(tmp_path, monkeypatch, "c27")
    cli.main(["run", str(run_dir), "--json", "--observe", "--raw"])
    groups = json.loads(capsys.readouterr().out)["observation"]["groups"]
    assert {group["reason"] for group in groups} == {"refused"}
    assert all(group["refused"] and not group["checked"] for group in groups)


def test_p7_an_observed_repository_still_reads_checked_yes(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n", encoding="utf-8")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c28", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    block = _repo_state(_out(capsys, run_dir, "--raw", "--observe"))
    assert block[1] == f"  repository {repo}  checked: yes"
    assert not any("run yourself" in line for line in block)


def test_p8_an_empty_repo_state_names_its_scope(tmp_path, monkeypatch, capsys):
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c29", [Att(1, journal="completed")])
    for flags in ([], ["--local"], ["--raw"]):
        out = _out(capsys, run_dir, *flags, "--observe")
        assert "REPO STATE  (no repository holds a path at risk)" in out.splitlines()
        assert "REPO STATE" not in [line for line in out.splitlines()]
        assert "(no repositories)" not in out


def test_p8_the_wording_is_one_line_and_the_next_section_follows_a_blank_line(
    tmp_path, monkeypatch, capsys,
):
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c30", [Att(1, journal="completed")])
    lines = _out(capsys, run_dir, "--raw", "--observe").splitlines()
    index = lines.index("REPO STATE  (no repository holds a path at risk)")
    assert lines[index + 1] == ""


def test_p7_p8_the_contract_states_both():
    assert _flat(
        "(its ledger is partial: incomplete evidence may hide more writes, so the tool does not "
        "run status for it)"
    ) in FLAT
    assert _flat("REPO STATE  (no repository holds a path at risk)") in FLAT


# --- P10: bash_timeout's title --------------------------------------------------------------


ANCHORED = "Exit code 143\nCommand timed out after 10m 0s"
EXIT_ONLY = {"kind": "tool_result", "tool_name": "Bash", "text": "", "exit_code": 143}


def _anchored_row():
    return {"kind": "tool_result", "tool_name": "Bash", "text": ANCHORED, "exit_code": 143}


def _title(rows, suffix="c31"):
    run = _build_run(f"wf_00000000-{suffix}", "a8310000000000001", rows)
    (finding,) = bash_timeout.DETECTOR.run(run)
    return finding


def test_p10_every_hit_anchored_keeps_the_hard_timeout_title():
    assert _title([_anchored_row()]).title == "1 Bash command hit a hard timeout"
    assert _title([_anchored_row(), _anchored_row()]).title == "2 Bash commands hit a hard timeout"


def test_p10_an_anchored_marker_without_an_exit_code_field_still_counts_as_anchored():
    row = {"kind": "tool_result", "tool_name": "Bash", "text": ANCHORED, "exit_code": None}
    assert _title([row]).title == "1 Bash command hit a hard timeout"


def test_p10_an_exit_code_only_hit_says_it_ended_with_exit_code_143():
    finding = _title([EXIT_ONLY])
    assert finding.title == (
        "1 Bash command ended with exit code 143 (a hard timeout or another termination)"
    )
    two = _title([EXIT_ONLY, dict(EXIT_ONLY)], "c32")
    assert two.title == (
        "2 Bash commands ended with exit code 143 (a hard timeout or another termination)"
    )


def test_p10_one_exit_code_only_hit_among_anchored_ones_changes_the_title():
    finding = _title([_anchored_row(), EXIT_ONLY], "c33")
    assert finding.title == (
        "2 Bash commands ended with exit code 143 (a hard timeout or another termination)"
    )


def test_p10_an_exit_code_only_hit_decides_wherever_it_sits():
    expected = "2 Bash commands ended with exit code 143 (a hard timeout or another termination)"
    assert _title([EXIT_ONLY, _anchored_row()], "c39").title == expected
    assert _title([_anchored_row(), EXIT_ONLY, _anchored_row()], "c40").title.startswith("3 ")
    assert "ended with exit code 143" in _title(
        [_anchored_row(), EXIT_ONLY, _anchored_row()], "c41"
    ).title


def test_p10_severity_metrics_rule_and_evidence_are_unchanged():
    finding = _title([EXIT_ONLY, _anchored_row()], "c34")
    assert finding.severity == "minor"
    assert finding.metrics == {"count": 2}
    assert finding.rule_id == "R-NO-LONG-BASH"
    assert len(finding.evidence) == 2


def test_p10_the_on_disk_fixture_with_the_anchored_marker_keeps_its_title():
    fixture = Path(__file__).parent / "fixtures" / "runs" / "bash_backgrounded_timeout"
    (finding,) = bash_timeout.DETECTOR.run(reader.load_run(fixture))
    assert finding.title == "1 Bash command hit a hard timeout"


def test_p10_the_contract_states_both_titles():
    assert "hit a hard timeout" in FLAT
    assert "ended with exit code 143 (a hard timeout or another termination)" in FLAT
