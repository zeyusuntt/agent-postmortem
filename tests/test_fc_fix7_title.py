"""Fix round 7 (W7-2) of field milestone F: a tool-built title never carries a path in-band for the
scanner to rediscover (docs/DESIGN.md 4.13.4, 5.1; .reviews/v0.3/F-dispositions.md, "Fix round 6
and 6b review"; Codex round 6 finding 3).

`Finding.title_parts` is a fixed template with one `{path}` placeholder plus the path value. The
renderer formats the template once, with the path rendered by the level's path function, and does
not scan the result: the title's pseudonym is the one in `metrics.paths`, whatever the path holds
(a space, a quote, one or several backquotes).

Every fixture is synthetic.
"""

from __future__ import annotations

import json
import re

import pytest

from agent_postmortem import cli, models, report
from agent_postmortem.detectors import claim_contradiction, missing_artifact
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.models import Finding
from agent_postmortem.redact import Redactor
from tests.test_fa_path_token import (
    FAKE_SLUG,
    FAKE_UUID,
    HOME,
    SUB,
    _assistant_text,
    _assistant_tool,
    _no_config,
    _tool_result,
    _user,
    _write_jsonl,
)

HIDDEN = " [text hidden]"
ABSENT_TAIL = "is absent now; the tool cannot tell whether it existed when the run ended"
UNCHECKED_TAIL = (
    "could not be checked now; the tool cannot tell whether it existed when the run ended"
)
PRIVATE = ("Client", "Secret", "deep", "notes.md", "Zqa", "Zqb", "Zqc")

# the exact input of Codex round 6 finding 3, two adjacent backquotes, several, and plain paths
PATHS = [
    pytest.param("/srv/Client` Secret/deep/notes.md", id="codex3_one_backquote_and_a_space"),
    pytest.param("/srv/Client`` Secret/deep/notes.md", id="codex3_two_adjacent_backquotes"),
    pytest.param("/srv/Client```Secret``deep`/notes.md", id="several_backquotes"),
    pytest.param("/srv/Zqa`Zqb/Zqc/notes.md", id="one_backquote_no_space"),
    pytest.param("/srv/Zqa``Zqb```Zqc/notes.md", id="several_backquotes_no_space"),
    pytest.param("/srv/Zqa Zqb/Zqc/notes.md", id="a_space"),
    pytest.param("/srv/Zqa/Zqb/Zqc/notes.md", id="plain"),
]

_BUILDERS = [
    pytest.param("missing_artifact", id="missing_artifact"),
    pytest.param("claim_contradiction", id="claim_contradiction"),
]


def _parts(detector, path, state):
    if detector == "missing_artifact":
        return missing_artifact._title_parts(path, state)
    return claim_contradiction._report_path_title_parts(path, state)


def _finding(detector, path, state="absent_now"):
    parts = _parts(detector, path, state)
    metrics = (
        {"paths": [path], "verified": False} if detector == "missing_artifact"
        else {"clauses": ["report_path_missing"], "claims": {"report_path": path}}
    )
    return Finding(
        detector=detector, severity="info", title=parts.raw(), run_id="wf_00000000-a1",
        agent_id=None, evidence=[], metrics=metrics, rule_id=None, title_parts=parts,
    )


def _redactor(level, *registered):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    names = r.fresh_names(detailed=(level == "local"))
    for value in registered:
        names.path(value)
    return r


def _metrics_path(exported, detector):
    if detector == "missing_artifact":
        return exported["metrics"]["paths"][0]
    return exported["metrics"]["claims"]["report_path"]


# --- the model and the two detectors ---------------------------------------------------------------


def test_fc7_w2_title_parts_hold_a_template_with_one_placeholder_and_the_path():
    parts = models.TitleParts("{path} is absent now", "/srv/a b/c.md")
    assert parts.template == "{path} is absent now" and parts.path == "/srv/a b/c.md"
    assert parts.raw() == "/srv/a b/c.md is absent now"
    # a path that holds the placeholder text is data, never a second substitution
    assert models.TitleParts("{path} x", "{path}").raw() == "{path} x"
    finding = Finding(
        detector="d", severity="info", title="t", run_id="r", agent_id=None, evidence=[],
        metrics={}, rule_id=None,
    )
    assert finding.title_parts is None


@pytest.mark.parametrize("detector", _BUILDERS)
@pytest.mark.parametrize(
    ("state", "tail"), [("absent_now", ABSENT_TAIL), ("unknown", UNCHECKED_TAIL)]
)
def test_fc7_w2_the_detector_title_is_a_template_and_a_path_with_no_backquotes(
    detector, state, tail,
):
    path = "/srv/work/cards/x.md"
    parts = _parts(detector, path, state)
    assert parts.template == "{path} " + tail
    assert parts.path == path
    assert parts.raw() == f"{path} {tail}"
    old = missing_artifact._title if detector == "missing_artifact" else (
        claim_contradiction._report_path_title
    )
    assert old(path, state) == f"{path} {tail}"
    assert "`" not in parts.raw()


def test_fc7_w2_a_claim_contradiction_finding_carries_its_parts(monkeypatch):
    from tests.test_claim_contradiction import AGENT, _attempt, _load

    run = _load()
    path = "/srv/Client` Secret/deep/notes.md"
    _attempt(run, "report_missing").reported["claims"]["report_path"] = path
    findings = {f.agent_id: f for f in claim_contradiction.DETECTOR.run(run)}
    finding = findings[AGENT["report_missing"]]
    assert finding.title_parts == models.TitleParts("{path} " + ABSENT_TAIL, path)
    assert finding.title == f"{path} {ABSENT_TAIL}"
    # a finding with other clauses keeps its sentence title and has no parts
    assert findings[AGENT["tests_failed"]].title_parts is None


# --- rendering: text and JSON, share and local, registered and not -------------------------------------


@pytest.mark.parametrize("registered", [True, False])
@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("detector", _BUILDERS)
@pytest.mark.parametrize("path", PATHS)
def test_fc7_w2_the_title_keeps_its_sentence_and_its_pseudonym_is_the_one_in_the_metrics(
    path, detector, level, registered,
):
    r = _redactor(level, *([path] if registered else []))
    finding = _finding(detector, path)
    exported = report._export_finding(finding, r, frozenset())
    match = re.fullmatch(r"(path-\d+) " + re.escape(ABSENT_TAIL), exported["title"])
    assert match, exported["title"]
    shown = _metrics_path(exported, detector)
    assert shown.removesuffix(HIDDEN) == match.group(1), (exported["title"], shown)
    # the same title through the text report and the bug report
    line = report._findings_section([finding], r)[1]
    assert line == f"[info] {detector}  {exported['title']}", line
    evidence = report._bug_evidence_lines(
        type("RunStub", (), {"run_id": "wf_00000000-a1"})(), [finding], r,
    )
    assert f"- [info] {detector}: {exported['title']}" in evidence, evidence
    for word in PRIVATE:
        assert word not in json.dumps(exported), (word, exported)
        assert word not in line, (word, line)


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("detector", _BUILDERS)
@pytest.mark.parametrize("path", PATHS)
def test_fc7_w2_the_could_not_be_checked_form_is_rendered_the_same_way(path, detector, level):
    r = _redactor(level, path)
    exported = report._export_finding(_finding(detector, path, "unknown"), r, frozenset())
    match = re.fullmatch(r"(path-\d+) " + re.escape(UNCHECKED_TAIL), exported["title"])
    assert match, exported["title"]
    assert _metrics_path(exported, detector).removesuffix(HIDDEN) == match.group(1)


@pytest.mark.parametrize("detector", _BUILDERS)
@pytest.mark.parametrize("path", PATHS)
def test_fc7_w2_raw_prints_the_real_path_without_backquote_wrapping(path, detector):
    r = Redactor(enabled=False)
    exported = report._export_finding(_finding(detector, path), r, frozenset())
    assert exported["title"] == f"{path} {ABSENT_TAIL}"
    assert report._findings_section([_finding(detector, path)], r)[1] == (
        f"[info] {detector}  {path} {ABSENT_TAIL}"
    )


def test_fc7_w2_a_path_inside_home_is_shown_in_the_level_form_and_the_sentence_survives():
    path = "/home/user/Zqa/Zqb/notes.md"
    share = _redactor("share")
    title = report._export_finding(_finding("missing_artifact", path), share, frozenset())["title"]
    assert title == f"~/.../notes.md {ABSENT_TAIL}", title
    local = _redactor("local")
    title = report._export_finding(_finding("missing_artifact", path), local, frozenset())["title"]
    assert title == f"~/.../notes.md {ABSENT_TAIL}", title


@pytest.mark.parametrize("level", ["share", "local"])
def test_fc7_w2_a_title_with_no_parts_still_goes_through_the_scanner(level):
    r = _redactor(level)
    finding = Finding(
        detector="silent_retry", severity="major", title="see /srv/Zqa/Zqb/notes.md now",
        run_id="wf_00000000-a1", agent_id=None, evidence=[], metrics={}, rule_id=None,
    )
    title = report._export_finding(finding, r, frozenset())["title"]
    assert title == f"see path-1{HIDDEN}"


# --- end to end: a session whose prompt names a path with backquotes ------------------------------------

E2E_PATHS = [
    pytest.param("/srv/Zqa`Zqb/Zqc/notes.md", id="one_backquote"),
    pytest.param("/srv/Zqa``Zqb```Zqc/notes.md", id="several_backquotes"),
    pytest.param("/srv/Zqa/Zqb/Zqc/notes.md", id="plain"),
]


def _session(tmp_path, monkeypatch, path):
    _no_config(monkeypatch, tmp_path)
    root = tmp_path / "root"
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)
    main = root / FAKE_SLUG / f"{FAKE_UUID}.jsonl"
    _write_jsonl(main, [
        {**_user("Plan.", base), "sessionId": FAKE_UUID},
        {**_assistant_text("Delegating.", base + timedelta(seconds=1)), "sessionId": FAKE_UUID},
    ])
    sub = main.parent / FAKE_UUID / "subagents" / f"agent-{SUB}.jsonl"
    bash = _assistant_tool("tb", "Bash", {"command": "ls"}, base + timedelta(seconds=3), SUB)
    _write_jsonl(sub, [
        _user(f"Please write the notes to {path} when done.", base + timedelta(seconds=2), SUB),
        bash,
        _tool_result("tb", "ok", base + timedelta(seconds=4), SUB),
        _assistant_text("Done.", base + timedelta(seconds=5), SUB),
    ])
    return main


@pytest.mark.parametrize("flag", [None, "--local"])
@pytest.mark.parametrize("path", E2E_PATHS)
def test_fc7_w2_run_json_title_story_and_metrics_agree_and_keep_the_sentence(
    path, flag, tmp_path, monkeypatch, capsys,
):
    main = _session(tmp_path, monkeypatch, path)
    cli.main(["run", str(main), "--json", *([flag] if flag else [])])
    out = capsys.readouterr().out
    payload = json.loads(out)
    found = [f for f in payload["findings"] if f["detector"] == "missing_artifact"]
    assert len(found) == 1, found
    match = re.fullmatch(r"(path-\d+) " + re.escape(ABSENT_TAIL), found[0]["title"])
    assert match, found[0]["title"]
    assert found[0]["metrics"]["paths"] == [match.group(1)]
    for word in ("Zqa", "Zqb", "Zqc", "notes.md"):
        assert word not in out, word


@pytest.mark.parametrize("flag", [None, "--local"])
@pytest.mark.parametrize("path", E2E_PATHS)
def test_fc7_w2_run_text_findings_and_stories_keep_the_sentence(
    path, flag, tmp_path, monkeypatch, capsys,
):
    main = _session(tmp_path, monkeypatch, path)
    cli.main(["run", str(main), "--verbose", *([flag] if flag else [])])
    out = capsys.readouterr().out
    assert re.search(r"\[info\] missing_artifact  path-\d+ " + re.escape(ABSENT_TAIL), out), out
    stories = out.split("STORIES", 1)[1].split("FINDINGS", 1)[0]
    assert re.search(r"path-\d+ is absent now;", stories), stories
    for word in ("Zqa", "Zqb", "Zqc", "notes.md"):
        assert word not in out, word


@pytest.mark.parametrize("path", E2E_PATHS)
def test_fc7_w2_raw_run_json_prints_the_real_path_in_the_title(path, tmp_path, monkeypatch, capsys):
    main = _session(tmp_path, monkeypatch, path)
    cli.main(["run", str(main), "--json", "--raw"])
    payload = json.loads(capsys.readouterr().out)
    found = [f for f in payload["findings"] if f["detector"] == "missing_artifact"]
    assert [f["title"] for f in found] == [f"{path} {ABSENT_TAIL}"]


def test_fc7_w2_the_unit_of_a_story_is_a_clause_the_title_parts_split_a_run_story():
    """A run-level story joins several titles: a path title is a piece of its own, the clauses
    around it are scanned separately, and the story keeps its capitals and its final period."""
    r = _redactor("share")
    pieces = report._sentences_pieces(
        ["silent text first", _parts("missing_artifact", "/srv/Zqa/Zqb/notes.md", "absent_now")]
    )
    shown = report._render_pieces(pieces, r)
    assert shown == f"Silent text first. path-1 {ABSENT_TAIL}.", shown
    assert report._sentences(["a b", "c."]) == "A b. C."
    # a template that starts with a word is capitalized like any clause, and loses a final period
    lowered = report._sentences_pieces([models.TitleParts("lower {path} tail.", "/a/b")])
    assert lowered == [models.TitleParts("Lower {path} tail", "/a/b"), "."], lowered
    assert report._sentences_pieces([]) == [] and report._sentences_pieces([""]) == []


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("path", [
    pytest.param("/srv/Zqa`Zqb/Zqc/notes.md", id="a_backquote"),
    pytest.param("/srv/Zqa Zqb/Zqc/notes.md", id="a_space"),
])
def test_fc7_w2_a_run_story_renders_a_title_with_parts_in_text_and_json(path, level):
    """A run-level story (a finding with no agent) built from a title with parts: the JSON story
    text and the text STORIES line both render the path once and keep the tool's own words."""
    from pathlib import Path

    from agent_postmortem import reader

    fixture = Path(__file__).parent / "fixtures" / "runs" / "clean_run"
    run = reader.load_run(fixture)
    finding = _finding("missing_artifact", path)
    finding.severity = "major"  # a story is told for a non-info finding
    r = _redactor(level)
    payload = report.to_json(run, [finding], r)
    stories = [story["text"] for story in payload["stories"] if story["agent"] == "run"]
    assert len(stories) == 1, payload["stories"]
    assert re.fullmatch(r"path-\d+ " + re.escape(ABSENT_TAIL) + r"\.", stories[0]), stories[0]
    text = report.render_run_text(run, [finding], _redactor(level))
    stories_block = text.split("STORIES", 1)[1].split("FINDINGS", 1)[0]
    assert re.search(r"run  path-\d+ is absent now;", stories_block), stories_block
    for word in PRIVATE:
        assert word not in stories[0] and word not in text, (word, text)
