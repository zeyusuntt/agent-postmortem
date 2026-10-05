"""N3b fix A, S9 and C3: at local a label, description, phase, agent type or
project name is real, but never an absolute path and never a control
character; every assembled local line (the AGENT table, the stories, the bug
report reproduction) goes through the P19 scrub."""

from __future__ import annotations

import json

import pytest

from agent_postmortem import report
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests import n3b_helpers as h
from tests import n3b_planted as plant

MARK = "zz" + "name" + "5b2"
CONTROLS = "\x1b[31mRED\nINJECT\ttab\x07bell"


def _spellings(planted):
    return {
        "absolute": f"/opt/{MARK}/secret.py",
        "tilde": f"~/{MARK}/x.py",
        "repository": f"{planted.repo}/src/{MARK}.py",
        "controls": f"lbl{MARK} {CONTROLS}",
        "abs_and_controls": f"/opt/{MARK}/s.py {CONTROLS}",
    }


def _rewrite(planted, field: str, value: str) -> None:
    escaped = json.dumps(value)[1:-1]
    for path in sorted(planted.run_dir.iterdir()):
        if path.suffix not in (".json", ".jsonl"):
            continue
        text = path.read_text(encoding="utf-8")
        if field == "label":
            text = text.replace(plant.LABEL, escaped)
        elif field == "phase":
            text = text.replace(plant.PHASE, escaped)
        elif field == "type":
            text = text.replace('"general-purpose"', json.dumps(value))
        elif field == "description" and path.name.endswith(".meta.json"):
            data = json.loads(text)
            data["description"] = value
            text = json.dumps(data)
        path.write_text(text, encoding="utf-8")


def _assert_clean(out: str, planted) -> None:
    h.assert_no_absolute_path(out, planted)
    assert "\x1b" not in out and "\x07" not in out and "\t" not in out
    for line in out.splitlines():
        assert not line.startswith("INJECT"), line
    assert "/opt/" not in out


@pytest.mark.parametrize("kind", ["absolute", "tilde", "repository", "controls", "abs_and_controls"])
@pytest.mark.parametrize("field", ["label", "phase", "type", "description"])
@pytest.mark.parametrize("flags", [("--verbose",), ("--bug-report",)])
def test_local_names_are_never_absolute_paths_or_control_characters(
    kind, field, flags, tmp_path, capsys
):
    planted = plant.build(tmp_path)
    value = _spellings(planted)[kind]
    _rewrite(planted, field, value)
    _code, out, _err = h.cli(capsys, "run", planted.run_dir, "--local", *flags)
    _assert_clean(out, planted)
    if kind in ("absolute", "tilde", "repository", "abs_and_controls"):
        assert MARK not in out or kind == "controls", out
    if kind == "controls":
        assert f"lbl{MARK}" in out or field != "label"


def test_a_label_keeps_its_real_text_when_it_is_not_a_path(tmp_path, capsys):
    planted = plant.build(tmp_path)
    _rewrite(planted, "label", f"plain-name-{MARK}")
    _code, out, _err = h.cli(capsys, "run", planted.run_dir, "--local", "--verbose")
    assert f"plain-name-{MARK}" in out


def test_each_assembled_table_row_is_scrubbed_whole(tmp_path):
    planted = plant.build(tmp_path)
    _rewrite(planted, "label", f"/opt/{MARK}/s.py {CONTROLS}")
    from agent_postmortem import reader

    ref = reader.discover_runs(planted.root)[0]
    run = reader.load_run(ref)
    redactor = Redactor(context=DisclosureContext(level="local"))
    redactor.fresh_names(detailed=True)
    rows = report._agent_table(run, redactor)
    # the table is the header and the row, plus P4's lower-bound legend line when a cell ends in +
    assert [row for row in rows if not row.startswith("+ is a lower bound")] == rows[:2]
    assert len(rows) in (2, 3)
    assert MARK not in "\n".join(rows)


def test_the_project_name_loses_control_characters_at_local():
    redactor = Redactor(context=DisclosureContext(level="local"))
    redactor.fresh_names(detailed=True)
    shown = report.project_text("-Users-x\x1b[31m-proj\tq", redactor)
    assert "\x1b" not in shown and "\t" not in shown
    assert "-proj" in shown


def test_share_is_unchanged_by_the_local_name_rules(tmp_path, capsys):
    planted = plant.build(tmp_path)
    _rewrite(planted, "label", f"/opt/{MARK}/s.py {CONTROLS}")
    _code, out, _err = h.cli(capsys, "run", planted.run_dir, "--verbose")
    assert MARK not in out
    assert "agent-1" in out
