"""The default `scan` says when it could only read a run in part ("NEEDS
ATTENTION (0 of 2 runs) / nothing needs attention" over a run whose journal had malformed fields,
which only `--all` showed as `CLEAN (INCOMPLETE)`; docs/DESIGN.md 4.7). Synthetic fixtures only.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent_postmortem.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "journal_malformed_fields"
CLEAN_FIXTURE = Path(__file__).parent / "fixtures" / "runs"


def _scan(capsys, root: Path, *flags) -> tuple[int, str]:
    code = main(["scan", "--root", str(root), *flags])
    return code, capsys.readouterr().out


def test_the_default_view_says_a_run_was_read_in_part(capsys):
    code, out = _scan(capsys, FIXTURE)
    lines = out.splitlines()
    assert lines[0].startswith("NEEDS ATTENTION (0 of 2 runs)"), out
    assert lines[1] == "  nothing needs attention in what could be read", out
    assert lines[2] == ("  1 of 2 runs could only be read in part, so it may hide more: "
                        "agent-postmortem scan --all"), out
    assert code == 4  # INCOMPLETE, as before


def test_all_still_shows_which_one(capsys):
    _code, out = _scan(capsys, FIXTURE, "--all")
    assert "(INCOMPLETE)" in out


def test_json_keeps_its_shape(capsys):
    _code, out = _scan(capsys, FIXTURE, "--json")
    payload = json.loads(out)
    assert "read_in_part" not in payload and any(
        "(INCOMPLETE)" in row["verdict"] for row in payload["rows"])


def test_a_scan_with_every_run_read_whole_says_nothing_more(tmp_path, capsys):
    from live_helpers import FAKE_UUID, user, write_jsonl
    from test_live_session import SLUG, T0

    write_jsonl(tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl", [user(None, T0, "x")])
    _code, out = _scan(capsys, tmp_path / "projects")
    assert "read in part" not in out and "in what could be read" not in out


def test_with_candidates_the_line_still_comes(tmp_path, capsys, monkeypatch):
    from agent_postmortem import report
    from agent_postmortem.redact import Redactor

    text = report.render_scan_triage_text(
        [], 3, 5, {}, {}, Redactor(), read_in_part=2)
    assert "2 of 5 runs could only be read in part, so they may hide more" in text, text
    assert "nothing needs attention" not in text


# --- a placeholder in a prompt's path names no one file -----------------


def test_a_capital_placeholder_in_a_file_name_is_a_template():
    from agent_postmortem.detectors.missing_artifact import artifact_paths

    prompt = ("Write /tmp/out/reply-N.json. Write /tmp/out/part_X.md. Write /tmp/out/N.txt. "
              "Write /tmp/out/run-NN.md. Write /tmp/out/item-ID.json. Write /tmp/out/report.md.")
    assert artifact_paths(prompt) == ["/tmp/out/report.md"]


def test_a_real_name_with_capitals_is_still_a_file():
    from agent_postmortem.detectors.missing_artifact import artifact_paths

    prompt = ("Write /tmp/out/README.md. Write /tmp/out/NOTES.txt. Write /tmp/out/Xray-N1.json. "
              "Write /tmp/out/run-X7.md. Write /tmp/out/FINAL-N-report.md.")
    assert artifact_paths(prompt) == ["/tmp/out/README.md", "/tmp/out/NOTES.txt",
                                      "/tmp/out/Xray-N1.json", "/tmp/out/run-X7.md"]


def test_a_placeholder_kept_by_an_older_build_is_not_told_as_missing(tmp_path):
    """0.5.0 kept `reply-N.json` in a subagent's facts, and a store of the same facts version is
    read as it is, so the rule also holds where the kept list is read."""
    from agent_postmortem import live

    gone = str(tmp_path / "report.md")
    template = str(tmp_path / "reply-N.json")
    assert live.missing_outputs([template, gone]) == [gone]
