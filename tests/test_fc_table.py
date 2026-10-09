"""Part FC, P3 and P4 (field milestone F): the AGENT table's column widths and its legend
(docs/DESIGN.md 4.7, AGENT table; .reviews/v0.3/F-plan.md FC).

The field case (econ, design-docs, nebius, tb-science): at `--local` the AGENT column was a
fixed 10 characters wide, so a label such as `card B21` ran into the next column and read as
`card B2` plus one attempt. The rule: the shown label is truncated to at most 40 characters
(the first 37 and `...`), the AGENT column is `max(10, len(longest truncated label) + 2)`
wide, PHASE truncates at 22 (first 19 and `...`) with width `max(11, len(longest) + 2)`; the
share labels (`agent-N`, `phase-N`) keep today's widths so share bytes are unchanged. P4: when
any PATHS or COMMITS cell ends in `+`, one legend line sits directly under the table.

Fixtures are synthetic. Every wf_ id used here is in tests/synthetic-ids.txt.
"""

from __future__ import annotations

import json
import re

import pytest

from agent_postmortem import cli
from tests.n5c1_helpers import Att, bash_step, build_run, write_step
from tests.test_fa_commit_no_id import _attempt_facts, _call

LEGEND = "+ is a lower bound: incomplete evidence may hide more paths or commits."
LEVELS = [("share", []), ("local", ["--local"]), ("raw", ["--raw"])]


def _relabel(run_dir, labels: dict[int, str] | None = None, phases: dict[int, str] | None = None):
    """Rewrite the journal's labels and phases of a built run (attempt n is the n-th started
    record)."""
    path = run_dir / "journal.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    number = 0
    for row in rows:
        if row.get("type") == "started":
            number += 1
            if labels and number in labels:
                row["label"] = labels[number]
            if phases and number in phases:
                row["phase"] = phases[number]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _out(capsys, target, *flags) -> str:
    cli.main(["run", str(target), *flags])
    return capsys.readouterr().out


def _table(out: str) -> list[str]:
    """The header line and the rows under it."""
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("AGENT"))
    rows = [lines[start]]
    for line in lines[start + 1:]:
        if not line.strip() or line.startswith(("+ is a lower bound", "COMMITS ?")):
            break
        rows.append(line)
    return rows


def _run(tmp_path, monkeypatch, suffix, labels, phases=None, **kwargs):
    attempts = [Att(n + 1, key=f"v2:k{n}", journal="completed", start=10 + 5 * n)
                for n in range(len(labels))]
    run_dir = build_run(tmp_path, monkeypatch, f"wf_00000000-{suffix}", attempts, **kwargs)
    _relabel(run_dir, {n + 1: label for n, label in enumerate(labels)},
             {n + 1: phase for n, phase in enumerate(phases or [])} or None)
    return run_dir


def _columns(header: str) -> dict[str, int]:
    return {name: header.index(name) for name in ("AGENT", "PHASE", "ATTEMPTS") if name in header}


# --- P3: widths ---------------------------------------------------------------------------


@pytest.mark.parametrize("flags", [["--local"], ["--raw"]], ids=["local", "raw"])
def test_p3_the_field_label_no_longer_runs_into_the_next_column(
    tmp_path, monkeypatch, capsys, flags,
):
    """`card B21` with a ten character field read as `card B2` + `1`: the attempts count is
    separated from the label by at least two spaces now."""
    run_dir = _run(tmp_path, monkeypatch, "c01", ["card B21", "card B22 long label", "x"],
                   ["Build", "Build", "Build"])
    rows = _table(_out(capsys, run_dir, *flags))
    header, body = rows[0], rows[1:]
    width = max(10, len("card B22 long label") + 2)
    assert header.index("PHASE") == width
    for row, label in zip(body, ["card B21", "card B22 long label", "x"]):
        assert row[:width] == label.ljust(width)
        assert row[len(label):len(label) + 2] == "  "


@pytest.mark.parametrize("flags", [["--local"], ["--raw"]], ids=["local", "raw"])
@pytest.mark.parametrize(
    "label_length,expected_width",
    [
        (1, 10), (6, 10), (8, 10),  # the floor: the width was already 10
        (9, 11), (20, 22), (38, 40), (40, 42),
    ],
)
def test_p3_the_agent_width_is_the_longest_label_plus_two_with_a_floor_of_ten(
    tmp_path, monkeypatch, capsys, flags, label_length, expected_width,
):
    label = ("L" * label_length)
    run_dir = _run(tmp_path, monkeypatch, "c02", [label, "s"])
    header = _table(_out(capsys, run_dir, *flags))[0]
    assert header.index("PHASE") == expected_width


@pytest.mark.parametrize("flags", [["--local"], ["--raw"]], ids=["local", "raw"])
@pytest.mark.parametrize("length", [41, 45, 80])
def test_p3_a_label_over_forty_characters_keeps_its_first_37_and_ends_in_three_dots(
    tmp_path, monkeypatch, capsys, flags, length,
):
    label = "".join(chr(ord("a") + n % 26) for n in range(length))
    run_dir = _run(tmp_path, monkeypatch, "c03", [label])
    header, row = _table(_out(capsys, run_dir, *flags))
    shown = label[:37] + "..."
    assert len(shown) == 40
    assert header.index("PHASE") == 42
    assert row[:42] == shown + "  "
    assert label not in "\n".join((header, row))


def test_p3_a_label_of_exactly_forty_characters_is_not_truncated(tmp_path, monkeypatch, capsys):
    label = "k" * 40
    run_dir = _run(tmp_path, monkeypatch, "c04", [label])
    header, row = _table(_out(capsys, run_dir, "--local"))
    assert row.startswith(label + "  Implement")
    assert "..." not in row


@pytest.mark.parametrize("flags", [["--local"], ["--raw"]], ids=["local", "raw"])
@pytest.mark.parametrize(
    "longest,expected_width",
    [
        (1, 11), (9, 11),  # the floor: the width was already 11
        (10, 12), (14, 16), (22, 24),
    ],
)
def test_p3_the_phase_width_is_the_longest_phase_plus_two_with_a_floor_of_eleven(
    tmp_path, monkeypatch, capsys, flags, longest, expected_width,
):
    run_dir = _run(tmp_path, monkeypatch, "c05", ["a", "b"], ["P" * longest, "Q"])
    header = _table(_out(capsys, run_dir, *flags))[0]
    assert header.index("ATTEMPTS") - header.index("PHASE") == expected_width


@pytest.mark.parametrize("flags", [["--local"], ["--raw"]], ids=["local", "raw"])
def test_p3_a_phase_over_22_characters_keeps_its_first_19_and_ends_in_three_dots(
    tmp_path, monkeypatch, capsys, flags,
):
    phase = "Implementation-and-verification-phase"
    run_dir = _run(tmp_path, monkeypatch, "c06", ["a"], [phase])
    header, row = _table(_out(capsys, run_dir, *flags))
    shown = phase[:19] + "..."
    assert len(shown) == 22
    start = header.index("PHASE")
    assert header.index("ATTEMPTS") - start == 24
    assert row[start:start + 24] == shown + "  "


def test_p3_a_phase_of_exactly_22_characters_is_not_truncated(tmp_path, monkeypatch, capsys):
    phase = "p" * 22
    run_dir = _run(tmp_path, monkeypatch, "c07", ["a"], [phase])
    header, row = _table(_out(capsys, run_dir, "--local"))
    assert phase + "  1" in row and "..." not in row


def test_p3_the_agent_and_phase_widths_move_the_later_columns_together(
    tmp_path, monkeypatch, capsys,
):
    run_dir = _run(tmp_path, monkeypatch, "c08", ["a-fairly-long-label-here", "b"],
                   ["a-long-phase-title-x", "c"])
    header, first, second = _table(_out(capsys, run_dir, "--local"))
    column = header.index("ATTEMPTS")
    assert column == len("a-fairly-long-label-here") + 2 + len("a-long-phase-title-x") + 2
    for row in (first, second):
        assert re.match(r"\d", row[column]) and row[column - 1] == " "
    assert header.index("KILLED") - column == 10 + 19


@pytest.mark.parametrize("flags", [[], ["--local"], ["--raw"]], ids=["share", "local", "raw"])
def test_p3_a_table_with_short_labels_keeps_the_old_widths(tmp_path, monkeypatch, capsys, flags):
    run_dir = _run(tmp_path, monkeypatch, "c09", ["impl-1", "impl-2"], ["Build", "Build"])
    header = _table(_out(capsys, run_dir, *flags))[0]
    assert header == (
        f"{'AGENT':<10}{'PHASE':<11}{'ATTEMPTS':<10}{'END':<19}{'KILLED':<8}{'PATHS':<7}"
        f"{'COMMITS':<9}WASTED"
    )


def test_p3_share_labels_stay_within_the_old_widths_even_for_long_real_labels(
    tmp_path, monkeypatch, capsys,
):
    labels = [f"a very long real label number {n:02d} that must never reach share" for n in range(12)]
    phases = [f"a very long real phase title {n:02d} must not leak" for n in range(12)]
    run_dir = _run(tmp_path, monkeypatch, "c10", labels, phases)
    rows = _table(_out(capsys, run_dir))
    assert rows[0] == (
        f"{'AGENT':<10}{'PHASE':<11}{'ATTEMPTS':<10}{'END':<19}{'KILLED':<8}{'PATHS':<7}"
        f"{'COMMITS':<9}WASTED"
    )
    assert len(rows) == 13
    for n, row in enumerate(rows[1:], start=1):
        assert row[:10].rstrip() == f"agent-{n}"
        assert row[10:21].rstrip() == f"phase-{n}"
        assert row[21:31].rstrip() == "1"
    assert "real label" not in "\n".join(rows)
    assert "real phase" not in "\n".join(rows)


def test_p3_a_session_table_without_phases_uses_only_the_agent_width(tmp_path, capsys):
    from tests.n5c1_helpers import session_with_children
    main, _ids = session_with_children(tmp_path, [(1, [], False, True), (2, [], False, True)])
    out = _out(capsys, main, "--local")
    rows = _table(out)
    assert "PHASE" not in rows[0]
    width = rows[0].index("ATTEMPTS")
    assert width >= 10
    for row in rows[1:]:
        assert row[width - 2:width] == "  "
    assert rows[0].startswith(f"{'AGENT':<{width}}ATTEMPTS")


def test_p3_json_is_unaffected_by_the_display_truncation(tmp_path, monkeypatch, capsys):
    label = "z" * 60
    run_dir = _run(tmp_path, monkeypatch, "c11", [label])
    cli.main(["run", str(run_dir), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    assert label in json.dumps(payload)
    assert "..." not in "".join(
        agent.get("label", "") for agent in payload["run"].get("agents", [])
        if isinstance(agent, dict)
    )


# --- P4: legend -----------------------------------------------------------------------------


def _legend_follows_the_table(out: str) -> bool:
    lines = out.splitlines()
    rows = _table(out)
    start = next(i for i, line in enumerate(lines) if line.startswith("AGENT"))
    return lines[start + len(rows)] == LEGEND


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p4_a_shell_partial_shows_the_legend_under_the_table(
    tmp_path, monkeypatch, capsys, level, flags,
):
    from tests.n5c1_helpers import init_repo
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c12", [
        Att(1, journal="completed", steps=[
            write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo),
        ]),
    ])
    out = _out(capsys, run_dir, *flags)
    assert any(re.search(r"\b\d+\+", row) for row in _table(out)[1:])
    assert _legend_follows_the_table(out)
    assert out.count(LEGEND) == 1


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p4_a_missing_transcript_partial_shows_the_legend(
    tmp_path, monkeypatch, capsys, level, flags,
):
    from tests.n5c1_helpers import _aid, init_repo
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c13", [
        Att(1, key="v2:a", journal="failed", steps=[write_step("t1", repo / "tracked.txt", repo)]),
        Att(2, key="v2:a", journal="completed", start=40),
    ])
    (run_dir / f"agent-{_aid(2)}.jsonl").unlink()
    out = _out(capsys, run_dir, *flags)
    cells = " ".join(_table(out)[1:])
    assert re.search(r"\b\d+\+", cells), out
    assert _legend_follows_the_table(out)


@pytest.mark.parametrize("level,flags", LEVELS)
def test_p4_a_commit_without_an_id_shows_the_legend(tmp_path, capsys, level, flags):
    run, _ = _attempt_facts(tmp_path, _call("git add -A; git commit -q -m x"))
    out = _out(capsys, run.path, *flags)
    table = _table(out)
    column = table[0].index("COMMITS")  # G-B G4: the COMMITS cell was "0+"
    assert [row[column:column + 9].strip() for row in table[1:]] == ["?"]
    assert _legend_follows_the_table(out)


def test_p4_no_plus_means_no_legend(tmp_path, monkeypatch, capsys):
    run_dir = _run(tmp_path, monkeypatch, "c14", ["a", "b"])
    out = _out(capsys, run_dir, "--local")
    assert "+" not in " ".join(_table(out))
    assert LEGEND not in out
    assert "lower bound" not in out


def test_p4_a_question_mark_alone_is_not_a_plus(tmp_path, monkeypatch, capsys):
    from tests.n5c1_helpers import _aid
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c15", [
        Att(1, journal="completed"),
    ])
    (run_dir / f"agent-{_aid(1)}.jsonl").unlink()
    out = _out(capsys, run_dir, "--local")
    cells = " ".join(_table(out)[1:])
    assert "+" not in cells
    assert LEGEND not in out


def test_p4_the_legend_is_text_only_and_json_is_unchanged(tmp_path, capsys):
    run, _ = _attempt_facts(tmp_path, _call("git add -A; git commit -q -m x"))
    cli.main(["run", str(run.path), "--json"])
    assert LEGEND not in capsys.readouterr().out


def test_p4_the_legend_stays_under_the_table_before_repo_state(tmp_path, monkeypatch, capsys):
    from tests.n5c1_helpers import init_repo
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c16", [
        Att(1, journal="failed", steps=[
            write_step("t1", repo / "tracked.txt", repo), bash_step("t2", "make", repo),
        ]),
    ])
    out = _out(capsys, run_dir, "--raw", "--observe")
    lines = out.splitlines()
    legend = lines.index(LEGEND)
    assert lines[legend - 1] == _table(out)[-1]
    assert lines.index("REPO STATE") > legend


def test_fc_design_states_the_widths_and_the_legend():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "docs" / "DESIGN.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "max(10, len(longest truncated label) + 2)" in flat
    assert "max(11, len(longest truncated phase) + 2)" in flat
    assert LEGEND in flat


def test_p3_a_path_in_a_local_label_is_scrubbed_before_the_width_is_measured(
    tmp_path, monkeypatch, capsys,
):
    """The shown label goes through the local path scrub first and is truncated after, so the
    column is as wide as what is printed (a 66-character label that scrubs to 17 characters
    does not leave a 42-character gap)."""
    label = "see /opt/some/very/long/directory/name/for/the/project/file.py now ok"
    run_dir = _run(tmp_path, monkeypatch, "c37", [label, "b"])
    header, first, second = _table(_out(capsys, run_dir, "--local"))
    shown = "see path-1 [text hidden]"
    assert header.index("PHASE") == len(shown) + 2
    assert first.startswith(shown + "  Implement")
    assert second.startswith("b".ljust(len(shown) + 2) + "Implement")
    assert "/opt" not in "\n".join((header, first, second))


def test_p4_the_legend_shows_when_only_an_earlier_agent_has_a_plus(
    tmp_path, monkeypatch, capsys,
):
    from tests.n5c1_helpers import init_repo
    repo = init_repo(tmp_path / "repo")
    run_dir = build_run(tmp_path, monkeypatch, "wf_00000000-c38", [
        Att(1, key="v2:a", journal="completed", steps=[bash_step("t1", "make", repo)]),
        Att(2, key="v2:b", journal="completed", start=40),
    ])
    out = _out(capsys, run_dir, "--local")
    header, first, second = _table(out)
    assert "+" in first and "+" not in second
    assert _legend_follows_the_table(out)


def test_p4_a_commits_plus_alone_earns_the_legend():
    """COMMITS reads `N+` from the attempt fact even when PATHS has no plus (a complete
    ledger), so the legend follows the COMMITS cell too."""
    from agent_postmortem import report
    from agent_postmortem.disclosure import DisclosureContext
    from agent_postmortem.models import AttemptFacts, Ledger
    from agent_postmortem.redact import Redactor
    from tests.n5b_helpers import model_attempt, model_workflow_run

    facts = AttemptFacts(
        paths_written=[], in_flight=[], commits=["abc1234"], cwds=[], background_unfinished=0,
        last_tool=None, seconds_since_last_tool_at_end=None, commit_commands_without_id=2,
    )  # G-B G4: one known id (with none the cell is `?` and earns the other legend)
    attempt = model_attempt(1, ledger=Ledger([], "complete", []), facts=facts)
    run = model_workflow_run([attempt])
    redactor = Redactor(context=DisclosureContext(level="local"))
    redactor.fresh_names(detailed=True)
    rows = report._agent_table(run, redactor)
    assert rows[-1] == LEGEND
    assert len(rows) == 3
    cells = rows[1].split()
    assert "1+" in cells and cells.count("1+") == 1


def test_p3_the_table_scrubs_an_unregistered_label_and_phase_before_measuring_them():
    """Called without the run's names registered (the unit seam), the table still scrubs a
    path out of the label and the phase first, so the widths are those of what is printed."""
    from agent_postmortem import report
    from agent_postmortem.disclosure import DisclosureContext
    from agent_postmortem.redact import Redactor
    from tests.n5b_helpers import model_attempt, model_workflow_run

    run = model_workflow_run([model_attempt(1, key="v2:a"), model_attempt(2, key="v2:b")])
    run.agents[0].label = "see /opt/some/very/long/directory/name/for/the/project/file.py now ok"
    run.agents[0].phase = "phase at /opt/another/quite/long/path/to/the/phase/dir/x.txt end"
    redactor = Redactor(context=DisclosureContext(level="local"))
    redactor.fresh_names(detailed=True)
    header, first, second = report._agent_table(run, redactor)
    # the phase is truncated to 22 characters after the scrub (the marker is cut by the ellipsis)
    label, phase = "see path-1 [text hidden]", "phase at path-2 [te..."
    assert header.index("PHASE") == len(label) + 2
    assert header.index("ATTEMPTS") == len(label) + 2 + len(phase) + 2
    assert first.startswith(label + "  " + phase + "  1")
    assert second.startswith("impl-2".ljust(len(label) + 2) + "Implement".ljust(len(phase) + 2))
    assert "/opt" not in "\n".join((header, first, second))
