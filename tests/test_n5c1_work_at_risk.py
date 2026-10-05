"""N5c-1: WORK AT RISK from the ledger and the repository groups (plan S4, S11;
DESIGN 4.7, 4.13.1). The exact strings below are the contract the owner reviews.

`none` only when 4.13.1 allows it and, for a session, every subagent outside the
risk population returned a result to its parent; otherwise the block says what is
known. One line per population attempt; a refused group runs no git and ends with
the manual command (`git -C <repo> status --short` at local and raw, the alias and
a `--local` note at share); the unknown group gets the instruction with the
candidate cwd as a labeled hint at local and raw only.
"""

from __future__ import annotations

import re

import pytest

from agent_postmortem import cli, reader
from tests.n5c1_helpers import (
    Att,
    Step,
    bash_step,
    build_run,
    init_repo,
    session_with_children,
    write_step,
)
from tests.test_field_v03 import _aid, _assistant_tool, _kill, _tool_result

NONE_EMPTY_LINE = "WORK AT RISK  none: no attempt was killed, failed, or left unfinished."
NONE_NO_WRITE_LINE = "WORK AT RISK  none: the attempts at risk wrote no paths."
LEVELS = [("share", []), ("local", ["--local"]), ("raw", ["--raw"])]
ALIAS = r"a:[0-9a-f]{12,}"


def _block(out: str) -> list[str]:
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("WORK AT RISK"))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip():
            break
        block.append(line)
    return block


def _run_text(capsys, target, *flags):
    cli.main(["run", str(target), *flags])
    return capsys.readouterr().out


def _attempt_ident(level: str, number: int) -> str:
    return ALIAS if level == "share" else re.escape(_aid(number))


def _match(lines: list[str], patterns: list[str]) -> None:
    assert len(lines) == len(patterns), lines
    for line, pattern in zip(lines, patterns):
        assert re.fullmatch(pattern, line), (line, pattern)


def _workflow(tmp_path, monkeypatch, suffix, atts, **kwargs):
    return build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", atts, **kwargs)


# --- none ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("level,flags", LEVELS)
def test_an_empty_complete_population_is_none(tmp_path, monkeypatch, capsys, level, flags):
    run_dir = _workflow(tmp_path, monkeypatch, "a50", [Att(1, journal="completed")])
    assert _block(_run_text(capsys, run_dir, *flags)) == [NONE_EMPTY_LINE]


@pytest.mark.parametrize("level,flags", LEVELS)
def test_a_complete_population_that_names_no_path_is_none(
    tmp_path, monkeypatch, capsys, level, flags,
):
    read_only = Step("t1", "Read", {"file_path": "/tmp/ws/x"}, "text", False, "/tmp/ws")
    run_dir = _workflow(tmp_path, monkeypatch, "a51", [Att(1, steps=[read_only])])
    assert _block(_run_text(capsys, run_dir, *flags)) == [NONE_NO_WRITE_LINE]


# --- file tools only: a complete ledger -------------------------------------------------------------


@pytest.mark.parametrize("level,flags", LEVELS)
def test_one_attempt_that_wrote_one_path_with_a_file_tool(
    tmp_path, monkeypatch, capsys, level, flags,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a52", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    block = _block(_run_text(capsys, run_dir, *flags))
    _match(block, [
        re.escape("WORK AT RISK  1 attempt at risk: 1 path named, 0 commits reported."),
        "  " + _attempt_ident(level, 1) + re.escape("  wrote 1 path (file tools)"),
        re.escape("  repo state: run with --observe"),
    ])


@pytest.mark.parametrize("level,flags", LEVELS)
def test_two_attempts_and_three_paths_use_the_plural_words(
    tmp_path, monkeypatch, capsys, level, flags,
):
    repo = init_repo(tmp_path / "repo", {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
    run_dir = _workflow(tmp_path, monkeypatch, "a53", [
        Att(1, key="v2:a", steps=[
            write_step("t1", repo / "a.txt", repo), write_step("t2", repo / "b.txt", repo),
        ]),
        Att(2, key="v2:b", start=40, steps=[write_step("u1", repo / "c.txt", repo)]),
    ])
    block = _block(_run_text(capsys, run_dir, *flags))
    _match(block, [
        re.escape("WORK AT RISK  2 attempts at risk: 3 paths named, 0 commits reported."),
        "  " + _attempt_ident(level, 1) + re.escape("  wrote 2 paths (file tools)"),
        "  " + _attempt_ident(level, 2) + re.escape("  wrote 1 path (file tools)"),
        re.escape("  repo state: run with --observe"),
    ])


def test_with_observe_the_counts_line_replaces_the_hint(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo", {"a.txt": "a\n", "b.txt": "b\n"})
    (repo / "a.txt").write_text("changed\n", encoding="utf-8")
    run_dir = _workflow(tmp_path, monkeypatch, "a54", [
        Att(1, steps=[
            write_step("t1", repo / "a.txt", repo), write_step("t2", repo / "b.txt", repo),
            write_step("t3", repo / "gone.txt", repo),
        ]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw", "--observe"))
    assert block[0] == "WORK AT RISK  1 attempt at risk: 3 paths named, 0 commits reported."
    assert block[-1] == "  1 path dirty now, 1 missing, 1 clean"


def test_an_unchecked_but_not_refused_group_says_why_with_observe(tmp_path, monkeypatch, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    run_dir = _workflow(tmp_path, monkeypatch, "a55", [
        Att(1, steps=[write_step("t1", plain / "f.txt", plain)]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw", "--observe"))
    assert block[-2] == "  repo state not checked (not a git repository)"
    assert block[-1] == f"  run yourself: git -C {plain} status --short"


def test_a_historical_run_says_so_instead_of_the_observe_hint(tmp_path, monkeypatch, capsys):
    from datetime import timedelta

    from agent_postmortem import report
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a56", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    run = reader.load_run(run_dir)
    monkeypatch.setattr(report, "_now", lambda: run.ended_at + timedelta(days=9))
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[-1] == "  historical: repo state not checked (run older than 72h)"


# --- partial ledgers: refused groups and the manual command ----------------------------------------


def _commit_step(repo):
    return Step(
        "t9", "Bash", {"command": "git commit -m x"}, "[main 1234567] x", False, str(repo),
    )


def test_a_refused_group_at_raw_ends_with_the_runnable_command(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a57", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), _commit_step(repo)]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block == [
        "WORK AT RISK  1 attempt at risk: 1 path named, 1 commit reported; "
        "the tool cannot see all of what was written.",
        f"  {_aid(1)}  wrote 1 path (file tools); ran 1 shell command that may have written more",
        f"  run yourself: git -C {repo} status --short",
        "  run yourself: git status --short in the repository the agent worked in",
        f"    hint: an attempt ran in {repo}",
    ]


def test_a_refused_group_at_local_shows_the_local_spelling_and_the_hint(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a58", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), _commit_step(repo)]),
    ])
    out = _run_text(capsys, run_dir, "--local")
    block = _block(out)
    assert str(repo) not in out
    assert block[0].startswith("WORK AT RISK  1 attempt at risk: 1 path named, 1 commit reported;")
    commands = [line for line in block if "run yourself" in line]
    assert len(commands) == 2
    assert re.fullmatch(r"  run yourself: git -C \S+ status --short", commands[0])
    assert commands[1] == "  run yourself: git status --short in the repository the agent worked in"
    assert any(line.startswith("    hint: an attempt ran in ") for line in block)


def test_a_refused_group_at_share_names_the_alias_and_points_at_local(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a59", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo), _commit_step(repo)]),
    ])
    out = _run_text(capsys, run_dir)
    block = _block(out)
    assert str(repo) not in out and "tracked.txt" not in out
    _match(block, [
        re.escape("WORK AT RISK  1 attempt at risk: 1 path named, 1 commit reported; "
                  "the tool cannot see all of what was written."),
        "  " + ALIAS + re.escape(
            "  wrote 1 path (file tools); ran 1 shell command that may have written more"),
        r"  run yourself: git status --short in path-\d+; --raw prints the exact command",
        re.escape("  run yourself: git status --short in the repository the agent worked in"),
    ])


def test_the_hint_is_the_only_place_a_cwd_appears_and_never_at_share(
    tmp_path, monkeypatch, capsys,
):
    work = tmp_path / "scratch"
    work.mkdir()
    run_dir = _workflow(tmp_path, monkeypatch, "a5a", [
        Att(1, steps=[bash_step("t1", "make build", work)]),
    ])
    shared = _block(_run_text(capsys, run_dir))
    raw = _block(_run_text(capsys, run_dir, "--raw"))
    assert not any("hint:" in line for line in shared)
    assert f"    hint: an attempt ran in {work}" in raw
    assert not any(str(work) in line for line in shared)


def test_shell_commands_without_a_path_are_not_none_and_say_what_is_known(
    tmp_path, monkeypatch, capsys,
):
    work = tmp_path / "scratch"
    work.mkdir()
    steps = [bash_step("t1", "make build", work), bash_step("t2", "make test", work)]
    run_dir = _workflow(tmp_path, monkeypatch, "a5b", [Att(1, steps=steps)])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[0] == (
        "WORK AT RISK  1 attempt at risk: 0 paths named, 0 commits reported; "
        "the tool cannot see all of what was written."
    )
    assert block[1] == f"  {_aid(1)}  ran 2 shell commands that may have written more"


def test_an_unrecognized_tool_and_a_delegation_each_get_their_clause(
    tmp_path, monkeypatch, capsys,
):
    steps = [
        Step("t1", "mcp__fs__write_file", {"path": "/tmp/ws/o"}, "ok", False, "/tmp/ws"),
        Step("t2", "mcp__fs__write_file", {"path": "/tmp/ws/p"}, "ok", False, "/tmp/ws"),
        Step("t3", "Agent", {"prompt": "x"}, "done", False, "/tmp/ws"),
    ]
    run_dir = _workflow(tmp_path, monkeypatch, "a5c", [Att(1, steps=steps)])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[1] == (
        f"  {_aid(1)}  called 2 unrecognized tools that may have written; "
        "delegated 1 call that may have written"
    )


def test_an_attempt_level_partial_reason_is_named_in_its_line(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    unpaired = Step("t1", "Write", {"file_path": str(repo / "tracked.txt")}, None, False, str(repo))
    run_dir = _workflow(tmp_path, monkeypatch, "a5d", [Att(1, steps=[unpaired])])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[1] == (
        f"  {_aid(1)}  tried 1 path with no confirmed result; "
        "ledger incomplete (unpaired_write_call)"
    )


def test_an_attempt_that_wrote_nothing_the_ledger_names_is_listed_plainly(
    tmp_path, monkeypatch, capsys,
):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a5e", [
        Att(1, key="v2:a", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:b", start=40, steps=[Step("u1", "Read", {"file_path": "/tmp/ws/x"}, "t", False, "/tmp/ws")]),
    ])
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[2] == f"  {_aid(2)}  wrote nothing the ledger names"


# --- an incomplete population ----------------------------------------------------------------------


@pytest.mark.parametrize("level,flags", LEVELS)
def test_an_incomplete_empty_population_is_unknown_never_none(
    tmp_path, monkeypatch, capsys, level, flags,
):
    run_dir = _workflow(tmp_path, monkeypatch, "a5f", [Att(1, journal="completed")])
    parent = run_dir.parents[2].parent / "00000000-0000-4000-8000-000000000000.jsonl"
    if not parent.exists():
        parent = next(run_dir.parents[3].glob("*.jsonl"))
    with parent.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    block = _block(_run_text(capsys, run_dir, *flags))
    assert block == [
        "WORK AT RISK  unknown: the tool could not establish which attempts were at risk: "
        "some run files could not be read or matched."
    ]


def test_a_nonempty_incomplete_population_adds_the_run_level_line(tmp_path, monkeypatch, capsys):
    repo = init_repo(tmp_path / "repo")
    run_dir = _workflow(tmp_path, monkeypatch, "a60", [
        Att(1, steps=[write_step("t1", repo / "tracked.txt", repo)]),
    ])
    parent = next(run_dir.parents[3].glob("*.jsonl"))
    with parent.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    block = _block(_run_text(capsys, run_dir, "--raw"))
    assert block[0] == (
        "WORK AT RISK  1 attempt at risk: 1 path named, 0 commits reported; "
        "the tool cannot see all of what was written."
    )
    assert "  the list of attempts at risk may be incomplete: some run files could not be read "\
        "or matched." in block


# --- the session guard (S11) ----------------------------------------------------------------------------


def _write_unpaired(number: int, repo):
    return Step(f"w{number}", "Write", {"file_path": str(repo / "tracked.txt")}, None, False, str(repo))


def test_a_session_whose_every_outside_subagent_returned_is_none(tmp_path, capsys):
    main, _ids = session_with_children(
        tmp_path, [(70, [], False, True), (71, [], False, True)],
    )
    cli.main(["run", str(main), "--raw"])
    assert _block(capsys.readouterr().out) == [NONE_EMPTY_LINE]


def _session_block(main, capsys, *flags):
    cli.main(["run", str(main), *flags])
    return _block(capsys.readouterr().out)


def test_a_session_child_that_never_returned_makes_none_unknown(tmp_path, capsys):
    main, ids = session_with_children(
        tmp_path, [(72, [], False, True), (73, [], False, False)],
    )
    block = _session_block(main, capsys, "--raw")
    assert block == [
        "WORK AT RISK  unknown: 1 subagent never returned a result to the main session; "
        "the tool cannot say what it wrote.",
        f"  {ids[1]}  no result returned to the main session",
    ]


def test_the_no_sentinel_unpaired_write_child_is_unknown(tmp_path, capsys):
    repo = init_repo(tmp_path / "repo")
    main, ids = session_with_children(
        tmp_path, [(74, [_write_unpaired(74, repo)], False, False)],
    )
    block = _session_block(main, capsys, "--raw")
    assert block[0].startswith("WORK AT RISK  unknown: 1 subagent never returned")
    assert block[1] == f"  {ids[0]}  no result returned to the main session"


def test_two_unreturned_children_use_the_plural_and_share_hides_the_real_ids(tmp_path, capsys):
    main, ids = session_with_children(
        tmp_path, [(75, [], False, False), (76, [], False, False)],
    )
    raw = _session_block(main, capsys, "--raw")
    assert raw[0] == (
        "WORK AT RISK  unknown: 2 subagents never returned a result to the main session; "
        "the tool cannot say what they wrote."
    )
    shared = _session_block(main, capsys)
    assert shared[0] == raw[0]
    assert all(real not in line for line in shared for real in ids)
    assert all(re.fullmatch(r"  (a:[0-9a-f]{12,}|agent-\d+)  no result returned to the main session",
                            line) for line in shared[1:])


def test_an_interrupted_child_is_in_the_population_and_the_guard_still_names_other_children(
    tmp_path, capsys,
):
    repo = init_repo(tmp_path / "repo")
    killed = [write_step("k1", repo / "tracked.txt", repo)]
    main, ids = session_with_children(
        tmp_path, [(77, killed, True, True), (78, [], False, False)],
    )
    block = _session_block(main, capsys, "--raw")
    assert block[0] == "WORK AT RISK  1 attempt at risk: 1 path named, 0 commits reported."
    assert f"  {ids[0]}  wrote 1 path (file tools)" in block
    assert f"  1 subagent outside this list never returned a result to the main session: {ids[1]}" in block


def test_the_session_main_thread_is_listed_as_main(tmp_path, capsys):
    repo = init_repo(tmp_path / "repo")
    from datetime import timedelta

    from tests.n5c1_helpers import BASE
    entries = [
        {**_assistant_tool("m1", "Write", {"file_path": str(repo / "tracked.txt")},
                           BASE + timedelta(seconds=5)), "cwd": str(repo)},
        {**_tool_result("m1", "ok", BASE + timedelta(seconds=6)), "cwd": str(repo)},
        _kill(BASE + timedelta(seconds=7)),
    ]
    main, _ids = session_with_children(tmp_path, [], main_entries=entries)
    block = _session_block(main, capsys, "--raw")
    assert block[0] == "WORK AT RISK  1 attempt at risk: 1 path named, 0 commits reported."
    assert block[1] == "  main  wrote 1 path (file tools)"
