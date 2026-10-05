"""Fix round 8 (W8-5, W8-6, W8-10) of field milestone F: an unreadable run directory is a
diagnostic and never a crash, an uncaught exception prints through the redactor, a config warning
is built from typed parts, and the test gaps the round 7 mutation tables named (docs/DESIGN.md
5.1, 4.1; .reviews/v0.3/F-dispositions.md, "Fix round 7 review" and "Codex round 7").

Each row of `.reviews/tooling/f/inventory_fix8.py` that classifies an output as scanned names a
test of this file or of the earlier fix files.

Every fixture is synthetic.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path

import pytest

from agent_postmortem import cli, config, report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.models import Finding, TitleParts
from agent_postmortem.redact import Redactor
from tests import n3b_helpers as h
from tests.test_fc_fix5_redact import HIDDEN, _redactor

HOME = Path("/home/user")
LEVELS = ["share", "local"]
WF = "wf_00000000-b10"
PRIVATE = ("Zq8a", "Zq8b", "Bob", "Zq8c")
HOSTILE_PROJECT = "-Users-zed-Zq8a Bob's Zq8b"

needs_permission_bits = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores directory permission bits"
)


# --- W8-5: an unreadable run directory ---------------------------------------------------------------


def _unreadable_root(tmp_path):
    root = tmp_path / "projects"
    run = root / HOSTILE_PROJECT / "sess" / "subagents" / "workflows" / WF
    run.mkdir(parents=True)
    (run / "journal.jsonl").write_text('{"type":"launched"}\n', encoding="utf-8")
    os.chmod(run, 0)
    return root, run


def _commands(root, run):
    return {
        "scan": ["scan", "--root", root],
        "scan --all": ["scan", "--all", "--root", root],
        "scan --json": ["scan", "--json", "--root", root],
        "trend": ["trend", "--root", root],
        "run <dir>": ["run", run],
        "run <dir> --verbose --evidence": ["run", run, "--verbose", "--evidence"],
        "run <dir> --json": ["run", run, "--json"],
        "run <id> --root": ["run", WF, "--root", root],
        "rescue plan": ["rescue", "plan", run],
        "rules --run": ["rules", "--run", run],
    }


@needs_permission_bits
@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w5_an_unreadable_run_directory_is_a_diagnostic_never_a_crash(
    level, tmp_path, capsys,
):
    root, run = _unreadable_root(tmp_path)
    try:
        for command, argv in _commands(root, run).items():
            code, out, err = h.cli(capsys, *argv, *(["--local"] if level == "local" else []))
            assert isinstance(code, int), command
            assert "Traceback" not in out + err, (command, err)
            assert "PermissionError" not in out + err, (command, err)
            if level == "share":
                for word in PRIVATE:
                    assert word not in out + err, (command, word, (out + err)[:400])
            else:
                # local prints real names (the project directory is one), never an absolute path
                for fragment in (str(tmp_path), str(tmp_path.resolve()), "Permission denied: '/"):
                    assert fragment not in out + err, (command, fragment, (out + err)[:400])
    finally:
        os.chmod(run, 0o755)


@needs_permission_bits
def test_fc8_w5_one_unreadable_run_does_not_hide_the_readable_ones_from_scan(tmp_path, capsys):
    root, run = _unreadable_root(tmp_path)
    good = root / "-Users-zed-fine" / "sess" / "subagents" / "workflows" / "wf_00000000-b11"
    good.mkdir(parents=True)
    (good / "journal.jsonl").write_text('{"type":"launched"}\n', encoding="utf-8")
    try:
        code, out, _err = h.cli(capsys, "scan", "--all", "--root", root)
    finally:
        os.chmod(run, 0o755)
    assert isinstance(code, int)
    assert "wf_00000000-b11" in out, out


@needs_permission_bits
def test_fc8_w5_the_reader_names_an_unreadable_journal_as_a_diagnostic(tmp_path):
    from agent_postmortem import reader

    _root, run = _unreadable_root(tmp_path)
    try:
        loaded = reader.load_run(run)
    finally:
        os.chmod(run, 0o755)
    assert any("journal.jsonl: unreadable" in item for item in loaded.diagnostics), loaded.diagnostics


def _raising(exc):
    def command(_args):
        raise exc

    return command


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w5_an_uncaught_oserror_prints_through_the_redactor_and_exits_one(
    level, monkeypatch, capsys,
):
    monkeypatch.setattr(
        cli, "_cmd_scan",
        _raising(PermissionError(13, "Permission denied", "/srv/Zq8a Bob's Zq8b/journal.jsonl")),
    )
    code, out, err = h.cli(capsys, "scan", *(["--local"] if level == "local" else []))
    assert code == 1
    assert "Traceback" not in out + err
    for word in PRIVATE + ("/srv",):
        assert word not in out + err, (word, err)
    assert "PermissionError" in err and "Permission denied" in err, err


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w5_an_uncaught_exception_with_a_path_in_its_message_prints_scanned(
    level, monkeypatch, capsys,
):
    monkeypatch.setattr(
        cli, "_cmd_scan",
        _raising(RuntimeError("cannot open '/srv/Zq8a/Bob's Zq8b/x' now")),
    )
    code, out, err = h.cli(capsys, "scan", *(["--local"] if level == "local" else []))
    assert code == 1
    for word in PRIVATE + ("/srv",):
        assert word not in out + err, (word, err)
    assert "RuntimeError" in err, err


def test_fc8_w5_raw_prints_the_exception_as_it_is(monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "_cmd_scan",
        _raising(PermissionError(13, "Permission denied", "/srv/Zq8a Bob's Zq8b/journal.jsonl")),
    )
    code, _out, err = h.cli(capsys, "scan", "--raw")
    assert code == 1
    assert "/srv/Zq8a Bob's Zq8b/journal.jsonl" in err, err


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_fc8_w5_the_last_resort_handler_does_not_swallow_an_interrupt_or_an_exit(
    exc, monkeypatch, capsys,
):
    monkeypatch.setattr(cli, "_cmd_scan", _raising(exc()))
    with pytest.raises(exc):
        h.cli(capsys, "scan")


def test_fc8_w5_a_failure_while_printing_the_failure_still_exits_one(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_cmd_scan", _raising(RuntimeError("x")))

    def broken(self, exc):
        raise ValueError("render failed")

    monkeypatch.setattr(Redactor, "exception_text", broken)
    code, _out, err = h.cli(capsys, "scan")
    assert code == 1
    assert "RuntimeError" in err and "Traceback" not in err, err


# --- W8-10: a config warning is built from typed parts -----------------------------------------------


def _config_file(tmp_path, repo_path, check):
    file = tmp_path / "cfg" / "config.toml"
    file.parent.mkdir()
    file.write_text(
        f'[[repos]]\npath = "{repo_path}"\nchecks = [{json.dumps(check)}]\n', encoding="utf-8",
    )
    return file


@pytest.mark.parametrize("flag", [None, "--local"])
@pytest.mark.parametrize("quote", ["'", '"'])
def test_fc8_w10_a_repository_path_that_prefixes_a_longer_untrusted_path_hides_the_tail(
    flag, quote, tmp_path, monkeypatch, capsys,
):
    check = f"pytest {quote}/srv/data Client Secret/deep/notes.md{quote}; echo done"
    file = _config_file(tmp_path, "/srv/data", check)
    config._cache.clear()
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(file))
    root = tmp_path / "projects"
    root.mkdir()
    try:
        _code, _out, err = h.cli(capsys, "scan", "--root", root, *([flag] if flag else []))
    finally:
        config._cache.clear()
    assert "declared check rejected" in err, err
    for word in ("Client", "Secret", "deep", "notes.md", "/srv"):
        assert word not in err, (word, err)


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w10_the_exact_review_setup_through_the_warning_builder(level, tmp_path):
    file = _config_file(
        tmp_path, "/srv/data", 'pytest "/srv/data Client Secret/deep/notes.md"; echo done',
    )
    cfg = config._parse(file)
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    shown = cfg.shown_warnings(r.display_path, r.scrub_leftover_paths)
    assert len(shown) == 1
    for word in ("Client", "Secret", "deep", "notes.md", "/srv"):
        assert word not in shown[0], (word, shown)
    assert "declared check rejected, compound or directory-changing command:" in shown[0], shown


def test_fc8_w10_every_warning_of_the_parser_carries_typed_parts(tmp_path):
    file = tmp_path / "c.toml"
    file.write_text(
        '[detectors]\noff = ["nope_zq8a"]\n[[repos]]\npath = "/srv/data"\nchecks = ["cd x"]\n'
        '[[adopted]]\nrule = "R-NOT-A-RULE-ZQ8"\ndate = "2026-01-01"\n',
        encoding="utf-8",
    )
    cfg = config._parse(file)
    assert len(cfg.warnings) == 3
    for warning in cfg.warnings:
        assert isinstance(warning, config.ConfigWarning), warning
        assert "".join(part.value for part in warning.parts) == str(warning)
        assert {part.kind for part in warning.parts} <= {"text", "path", "untrusted"}
    kinds = [[part.kind for part in warning.parts] for warning in cfg.warnings]
    assert all("path" in each for each in kinds), kinds
    assert all("untrusted" in each for each in kinds), kinds


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("body", [
    pytest.param("checks = 5\n", id="checks_not_a_list"),
    pytest.param('checks = [""]\n', id="check_not_a_string"),
])
def test_fc8_w10_the_repository_path_field_of_a_warning_is_displayed_as_one_value(
    level, body, tmp_path,
):
    file = tmp_path / "c.toml"
    file.write_text(f'[[repos]]\npath = "/srv/Zq8a Zq8b"\n{body}', encoding="utf-8")
    cfg = config._parse(file)
    assert len(cfg.warnings) == 1
    assert "/srv/Zq8a Zq8b" in cfg.warnings[0]
    kinds = [part.kind for part in cfg.warnings[0].parts]
    assert kinds.count("path") == 2, kinds  # the config file and the repository path
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    shown = cfg.shown_warnings(r.display_path, r.scrub_leftover_paths)[0]
    for word in ("Zq8a", "Zq8b", "/srv"):
        assert word not in shown, (word, shown)
    assert shown.endswith(" (path-2)"), shown


def test_fc8_w10_a_parse_failure_message_is_an_untrusted_piece(tmp_path):
    file = tmp_path / "c.toml"
    file.write_text("[[repos\n", encoding="utf-8")
    cfg = config._parse(file)
    assert len(cfg.warnings) == 1
    kinds = [part.kind for part in cfg.warnings[0].parts]
    assert kinds == ["text", "path", "text", "text", "untrusted"], kinds


def test_fc8_w10_no_regex_split_of_an_assembled_warning_remains():
    source = inspect.getsource(config.Config.shown_warnings)
    for needle in ("re.compile", ".split(", "splitter", "re.escape"):
        assert needle not in source, needle


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w10_a_plain_string_warning_is_one_untrusted_piece(level):
    cfg = config.Config(
        detectors_off=frozenset(), warnings=["agent-postmortem: see /srv/Zq8a Zq8b/x now"],
        config_file="/srv/Zq8a Zq8b/x",
    )
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    shown = cfg.shown_warnings(r.display_path, r.scrub_leftover_paths)
    assert shown == [f"agent-postmortem: see path-1{HIDDEN}"], shown


# --- W8-6: the test gaps of the round 7 mutation table -----------------------------------------------


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w6_a_name_that_ends_in_letters_after_a_colon_is_part_of_the_path(level):
    """The evidence suffix is `:<digits>` (and `:<digits>:<digits>`) only: `:abc` is name text."""
    assert _redactor(level)("see /srv/Zq8a/Zq8b:abc") == "see path-1"
    assert _redactor(level)("see /srv/Zq8a/Zq8b:12") == "see path-1:12"
    assert _redactor(level)("see /srv/Zq8a/Zq8b:12:3") == "see path-1:12:3"
    assert _redactor(level)("see /srv/Zq8a/Zq8b:12a") == "see path-1"


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w6_an_oserror_with_two_filenames_one_holding_the_other_renders_each_once(level):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    exc = OSError(2, "No such file", "/srv/Zq8a", None, "/srv/Zq8a/Zq8b")
    shown = r.exception_text(exc)
    assert "Zq8a" not in shown and "Zq8b" not in shown and "/srv" not in shown, shown
    assert shown.count("path-") == 2, shown
    assert shown.startswith("[Errno 2] No such file: 'path-"), shown


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w6_a_display_form_with_more_after_it_is_not_a_fixed_point(level):
    """`~/.../name` alone (or with sentence punctuation) is the display form the tool printed and
    keeps the words after it; a word that holds more path after the display form is a path."""
    r = _redactor(level)
    assert r("see ~/.../x.md now") == "see ~/.../x.md now"
    out = r("see ~/.../Zq8a/Zq8b Zq8c here")
    assert out == f"see path-1{HIDDEN}", out


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w6_a_title_that_names_a_home_path_is_not_rescanned(level):
    """A finished title template is the tool's own sentence: its rendered `~/n.md` is a home path
    start only to the scanner, and a rescan would hide the words after it."""
    r = Redactor(context=DisclosureContext(level="share", home=HOME))
    r.fresh_names(detailed=False)
    parts = TitleParts("{path} is absent now; the tool cannot tell whether it was lost", "~/n.md")
    finding = Finding(
        detector="missing_artifact", severity="major", title=parts.raw(), run_id="wf_00000000-a1",
        agent_id=None, evidence=[], metrics={}, rule_id=None, title_parts=parts,
    )
    expected = "~/n.md is absent now; the tool cannot tell whether it was lost"
    assert r.title_parts(parts) == expected
    assert report._finding_title(finding, r) == expected
    exported = report._export_finding(finding, r, frozenset())
    assert exported["title"] == expected, exported["title"]


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w6_a_home_anchor_inside_an_escaped_word_needs_a_word_start(level):
    """The decoded text of an escaped word is searched for home anchors only at a word start:
    `a~b/c` inside a decoded string is prose."""
    r = _redactor(level)
    for text in ('"a~b/c \\u0041 d"', '"x~y/z \\u0041"', '"1$HOME/q \\u0041"'):
        assert r(text) == text, text
    assert "Zq8a" not in r('"~zed/Zq8a/b \\u0041"')


@pytest.mark.parametrize("level", LEVELS)
def test_fc8_w6_a_single_curly_quote_fragment_closes_only_at_its_partner(level):
    r = _redactor(level)
    text = "see ‘/srv/Zq8a” Zq8b/x’ now"
    out = r(text)
    assert "Zq8a" not in out and "Zq8b" not in out, out
    assert out == f"see ‘path-1{HIDDEN}’ now", out


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("text,expected", [
    pytest.param("see /srv/Zq8a Zq8b\nnext", f"see path-1{HIDDEN}\nnext", id="plain_newline"),
    pytest.param("see /srv/Zq8a Zq8b \nnext", f"see path-1{HIDDEN} \nnext", id="space_newline"),
    pytest.param("see /srv/Zq8a Zq8b;\nnext", f"see path-1{HIDDEN}\nnext", id="operator_newline"),
    pytest.param("see /srv/Zq8a Zq8b\r\nnext", f"see path-1{HIDDEN}\r\nnext", id="crlf"),
    pytest.param("see /srv/Zq8a Zq8b\n\nnext", f"see path-1{HIDDEN}\n\nnext", id="blank_line"),
    pytest.param("see /srv/Zq8a Zq8b\n", f"see path-1{HIDDEN}\n", id="trailing_newline"),
])
def test_fc8_w6_a_hidden_span_keeps_the_line_break_that_ends_it(level, text, expected):
    out = _redactor(level)(text)
    assert out.count("\n") == text.count("\n"), (out, text)
    assert out.splitlines()[1:] == expected.splitlines()[1:], out
