"""N3b P10, P16: evidence pointer forms at share, through the run text and
the bug report. Every file-name form evidence uses is covered: agent
transcript, session transcript, agent meta, session meta, journal; plus the
strings that must never print (a subdirectory prefix, a non-canonical id,
free text).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from agent_postmortem import disclosure, report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.models import Finding
from agent_postmortem.redact import Redactor
from tests.test_bug_report import _make_findings, _make_run

AGENT = "a1111111111111111"  # the id tests/test_bug_report._make_run uses
SESSION = str(uuid.UUID(int=0x5E55))
OTHER_SESSION = str(uuid.UUID(int=0x5E56))
EXCERPT = "zzPointerExcerpt7"


def _render(evidence: list[str], *, level="share", book=None, flags=None, bug=False):
    run = _make_run()
    findings = _make_findings(run)[:1]
    findings[0].evidence = evidence
    identities = report.collect_identities(run, findings)
    if level == "share" and book is None:
        book = disclosure.register(identities)
    context = DisclosureContext(
        level=level, book=book, home=Path("/srv/people/tester"), identities=tuple(identities),
    )
    redactor = Redactor(context=context)
    if bug:
        return report.render_bug_report(run, findings, redactor, **(flags or {}))
    return report.render_run_text(
        run, findings, redactor, verbose=True, **(flags or {}),
    )


def _alias(kind, real):
    book = disclosure.register([(kind, real)])
    return book.alias(kind, real)


@pytest.mark.parametrize("bug", [False, True])
def test_every_canonical_file_name_form_is_aliased(bug):
    a, s, s2 = _alias("agent", AGENT), _alias("session", SESSION), _alias("session", OTHER_SESSION)
    out = _render(
        [
            f"agent-{AGENT}.jsonl:3: {EXCERPT} one",
            f"agent-{AGENT}.meta.json:1: {EXCERPT} two",
            f"{SESSION}.jsonl:5: {EXCERPT} three",
            f"{OTHER_SESSION}.meta.json:2",
            "journal.jsonl:7: " + EXCERPT + " four",
        ],
        bug=bug,
    )
    assert f"agent-{a}.jsonl:3" in out
    assert f"agent-{a}.meta.json:1" in out
    assert f"session-{s}.jsonl:5" in out
    assert f"session-{s2}.meta.json:2" in out
    assert "journal.jsonl:7" in out
    assert EXCERPT not in out
    for real in (AGENT, SESSION, OTHER_SESSION):
        assert real not in out
    # bare hex inside file names, no a: prefix (N5c-1: WORK AT RISK lines print
    # agents as a:<alias>, so the check reads the evidence pointer lines only)
    pointer_lines = [line for line in out.splitlines() if ".jsonl:" in line or ".meta.json:" in line]
    assert pointer_lines
    assert all("a:" not in line.replace("data:", "") for line in pointer_lines)


@pytest.mark.parametrize("bug", [False, True])
@pytest.mark.parametrize(
    "bad",
    [
        f"sub/agent-{AGENT}.jsonl:3: x",                 # a subdirectory prefix
        f"../agent-{AGENT}.jsonl:3: x",
        "agent-notanid.jsonl:3: x",                       # a non-canonical id
        f"agent-{AGENT.upper()}.jsonl:3: x",              # uppercase hex
        f"{SESSION.upper()}.jsonl:3: x",                  # uppercase uuid
        f"agent-{AGENT}.jsonl.bak:3: x",                  # unknown extension
        "free text with no pointer shape zzPointerExcerpt7",
        f"agent-{AGENT}.jsonl:three: x",                  # no line number
        f"agent-{AGENT}.jsonl:3 x",                       # no excerpt separator
        "",
    ],
)
def test_an_unparseable_or_non_canonical_pointer_prints_a_placeholder_never_its_text(bad, bug):
    out = _render([bad], bug=bug)
    assert "evidence-1" in out
    assert "zzPointerExcerpt7" not in out
    assert AGENT not in out and AGENT.upper() not in out
    assert "notanid" not in out
    assert SESSION.upper() not in out and "sub/" not in out


def test_placeholders_are_numbered_by_distinct_string_in_order():
    out = _render(["one", "two", "one"], bug=True)
    lines = [line.strip() for line in out.splitlines() if "evidence-" in line]
    # S13: the repeated "one" renders as the same line, which shows once.
    assert [line.split()[-1] for line in lines] == ["evidence-1", "evidence-2"]


def test_a_pointer_with_no_marker_hit_carries_no_code():
    alias = _alias("agent", AGENT)
    out = _render([f"agent-{AGENT}.jsonl:8: x"], bug=True)
    assert f"  - agent-{alias}.jsonl:8\n" in out  # tests/test_bug_report._make_run has no hits


def test_fallback_book_names_files_positionally():
    book = disclosure.fallback_book()
    out = _render(
        [f"agent-{AGENT}.jsonl:3: x", f"{SESSION}.jsonl:4: x", f"agent-{AGENT}.meta.json:1"],
        book=book, bug=True,
    )
    assert "agent-1.jsonl:3" in out
    assert "session-1.jsonl:4" in out
    assert "agent-1.meta.json:1" in out
    assert AGENT not in out and SESSION not in out


def test_local_prints_pointer_with_real_file_name_and_no_excerpt_without_evidence():
    out = _render([f"agent-{AGENT}.jsonl:3: {EXCERPT} one", f"{SESSION}.jsonl:4: x"], level="local")
    assert f"agent-{AGENT}.jsonl:3" in out
    assert f"{SESSION}.jsonl:4" in out
    assert EXCERPT not in out


def test_local_with_evidence_prints_the_excerpt_and_keeps_the_real_file_name():
    out = _render(
        [f"agent-{AGENT}.jsonl:3: {EXCERPT} one", f"{SESSION}.jsonl:4: x"], level="local",
        flags={"evidence": True},
    )
    assert f"agent-{AGENT}.jsonl:3: {EXCERPT} one" in out
    assert f"{SESSION}.jsonl:4: x" in out  # not uuid:<last4>


def test_local_and_raw_hide_an_unparseable_string_unless_evidence_is_requested():
    for level in ("local", "raw"):
        without = _render(["free text zzPointerExcerpt7"], level=level, bug=True)
        assert "zzPointerExcerpt7" not in without
        assert "evidence-1" in without
        with_evidence = _render(
            ["free text zzPointerExcerpt7"], level=level, bug=True, flags={"evidence": True},
        )
        assert "free text zzPointerExcerpt7" in with_evidence


def test_the_run_text_stories_with_evidence_omit_nothing_at_share_but_the_excerpt():
    a = _alias("agent", AGENT)
    out = _render([f"agent-{AGENT}.jsonl:3: {EXCERPT} one"], flags={"evidence": True})
    assert f"agent-{a}.jsonl:3" in out
    assert EXCERPT not in out
    assert "excerpts need --local or --raw" in out


def test_findings_are_unchanged_objects_evidence_is_still_the_raw_string():
    """P16: markers are looked up from the attempts' hits; Finding and its
    JSON never change."""
    run = _make_run()
    findings = _make_findings(run)
    before = [finding.to_dict() for finding in findings]
    _render([f"agent-{AGENT}.jsonl:3: x"])
    assert isinstance(findings[0], Finding)
    assert [finding.to_dict() for finding in findings] == before
