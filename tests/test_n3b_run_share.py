"""N3b Part 1b, share level: `run` text and `run --bug-report` (P9, P10, P11,
P16). One synthetic run with planted values (tests/n3b_planted.py); at share
none of them may appear, while the structural lines exist and carry the alias.
"""

from __future__ import annotations

import re

import pytest

from agent_postmortem import disclosure
from agent_postmortem.cli import main
from tests import n3b_planted as plant


@pytest.fixture
def planted(tmp_path):
    return plant.build(tmp_path)


def _run(planted, capsys, *flags):
    capsys.readouterr()
    code = main(["run", str(planted.run_dir), *flags])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _alias(kind: str, real: str) -> str:
    book = disclosure.register([(kind, real)])
    assert book.available
    return book.alias(kind, real)


def _planted_values(planted):
    return {
        "label": planted.label, "phase": planted.phase, "excerpt": planted.excerpt,
        "agent1": planted.agent1, "agent2": planted.agent2, "session": planted.session,
        "slug": planted.slug, "repo": str(planted.repo), "repo name": planted.repo.name,
        "repo file": str(planted.repo_file),
    }


def _assert_nothing_planted(out: str, planted):
    for name, value in _planted_values(planted).items():
        assert value not in out, f"{name} leaked at share"
    # Not even as a fragment of the project slug or the id.
    assert planted.agent1[:12] not in out
    assert "zzplant" not in out


SHARE_TEXT_FLAGS = ["--verbose", "--evidence", "--observe"]


def test_share_text_hides_every_planted_value_and_aliases_the_pointers(planted, capsys):
    _code, out, _err = _run(planted, capsys, *SHARE_TEXT_FLAGS)
    _assert_nothing_planted(out, planted)
    a1, a2 = _alias("agent", planted.agent1), _alias("agent", planted.agent2)
    assert f"agent-{a1}.jsonl:5" in out
    assert f"agent-{a1}.jsonl:6" in out
    assert f"agent-{a2}.jsonl:1" in out
    assert re.fullmatch(r"[0-9a-f]{12,64}", a1)
    assert "disclosure:" not in out


def test_share_pointer_carries_the_marker_code_and_never_an_excerpt(planted, capsys):
    _code, out, _err = _run(planted, capsys, *SHARE_TEXT_FLAGS)
    a1 = _alias("agent", planted.agent1)
    assert f"agent-{a1}.jsonl:5 [bash_timeout]" in out
    assert f"agent-{a1}.jsonl:6 [kill_sentinel]" in out
    assert "Exit code 143" not in out
    assert "[Request interrupted by user]" not in out
    # a pointer that is not a marker line carries no code
    a2 = _alias("agent", planted.agent2)
    assert re.search(rf"agent-{a2}\.jsonl:1$", out, re.MULTILINE)


def test_share_verbose_without_evidence_prints_pointers_but_no_note(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--verbose")
    a1 = _alias("agent", planted.agent1)
    assert f"agent-{a1}.jsonl:5 [bash_timeout]" in out
    assert "--local or --raw" not in out
    _assert_nothing_planted(out, planted)


def test_share_evidence_flag_prints_exactly_one_note_line(planted, capsys):
    _code, out, _err = _run(planted, capsys, *SHARE_TEXT_FLAGS)
    notes = [line for line in out.splitlines() if "excerpts need --local or --raw" in line]
    assert len(notes) == 1
    _code, out, _err = _run(planted, capsys, "--evidence")
    assert len([line for line in out.splitlines() if "excerpts need --local or --raw" in line]) == 1


def test_share_repo_state_uses_path_n(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--observe")
    assert "REPO STATE" in out
    assert "path-1" in out
    assert str(planted.repo) not in out


def test_share_aliases_are_stable_across_two_commands(planted, capsys):
    _code, first, _err = _run(planted, capsys, *SHARE_TEXT_FLAGS)
    _code, second, _err = _run(planted, capsys, *SHARE_TEXT_FLAGS)
    assert first == second


def test_share_bug_report_hides_every_planted_value(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--bug-report")
    _assert_nothing_planted(out, planted)
    a1 = _alias("agent", planted.agent1)
    assert f"agent-{a1}.jsonl:5 [bash_timeout]" in out
    assert f"agent-{a1}.jsonl:6 [kill_sentinel]" in out


def test_share_bug_report_project_line_carries_the_project_alias(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--bug-report")
    alias = _alias("project", planted.slug)
    assert f"project project:{alias}, 1 logical agent(s):" in out
    assert re.fullmatch(r"[0-9a-f]{12,64}", alias)


def test_share_bug_report_footer_matches_what_is_printed(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--bug-report")
    assert "Not redacted: evidence lines" not in out
    assert "pointers and marker codes only" in out
    assert "no transcript excerpts" in out
    assert "—" not in out


def test_share_bug_report_evidence_flag_prints_the_note_once(planted, capsys):
    _code, out, _err = _run(planted, capsys, "--bug-report", "--evidence")
    assert len([line for line in out.splitlines() if "excerpts need --local or --raw" in line]) == 1
    _assert_nothing_planted(out, planted)


def test_share_free_text_substitutes_the_runs_own_identities(planted):
    """P10/P11: a collected identity quoted inside any free text renders by
    its alias, the project slug before structural redaction."""
    from agent_postmortem.redact import Redactor

    identities = [("project", planted.slug), ("session", planted.session),
                  ("agent", planted.agent1)]
    book = disclosure.register(identities)
    context = disclosure.DisclosureContext(level="share", book=book, identities=tuple(identities))
    redactor = Redactor(context=context)
    text = redactor(f"in {planted.slug} session {planted.session} agent {planted.agent1} done")
    assert planted.slug not in text and planted.session not in text and planted.agent1 not in text
    assert f"project:{book.alias('project', planted.slug)}" in text
    assert f"s:{book.alias('session', planted.session)}" in text
    assert f"a:{book.alias('agent', planted.agent1)}" in text
