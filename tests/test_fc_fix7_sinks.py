"""Fix round 7 (W7-2, W7-4, W7-5) of field milestone F: every piece that enters a rescue line is
scanned (the `status` claim and `reset_time` leaked at local), a tool-built sentence is never
rescanned (config warnings), and the named test gaps (docs/DESIGN.md 5.1, 4.11;
.reviews/v0.3/F-dispositions.md, "Fix round 6 and 6b review").

Each row of `.reviews/tooling/f/inventory_fix7.py` that classifies a rescue builder piece as
scanned names a test of this file.

Every fixture is synthetic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import config, reader, rescue
from agent_postmortem.disclosure import DisclosureContext
from agent_postmortem.redact import Redactor
from tests import n3b_helpers as h
from tests.privacy_rules import FAKE_UUID
from tests.test_fc_fix4_sinks import _plan, _redactor

HIDDEN = " [text hidden]"
HOME = Path("/home/user")
FIXTURE = Path(__file__).parent / "fixtures" / "runs" / "clean_run"
PATHY = "/srv/Zqa Zqb/Zqc"
PRIVATE = ("Zqa", "Zqb", "Zqc", "/srv")
LEVELS = ["share", "local"]


# --- W7-4: the status claim --------------------------------------------------------------------------


def _reported(claims):
    return {"type": "dict", "keys": sorted(claims), "claims": claims}


@pytest.mark.parametrize("value", [
    pytest.param("/srv/Zqa Zqb/Zqc", id="sonnet1_spaces"),
    pytest.param("/srv/Zqa/Zqb.md", id="sonnet1_no_space"),
    pytest.param("see /srv/Zqa/Zqb", id="sonnet1_prose_then_a_path"),
])
def test_fc7_w4_the_status_claim_is_scanned_at_local(value):
    run, plan = _plan([])
    plan.units[0].reported = _reported({"ok": True, "status": value, "tests_failed": 2})
    text = rescue.render_plan_text(run, plan, _redactor("local"))
    line = next(item for item in text.splitlines() if item.startswith("  REPORTED"))
    assert "Zqa" not in line and "Zqb" not in line and "/srv" not in line, line
    # the claims around it keep their own words
    assert line.startswith("  REPORTED  ok: true, status: ") and line.endswith(", tests_failed: 2")


def test_fc7_w4_the_status_claim_is_a_fixed_word_at_share_and_raw_at_raw():
    run, plan = _plan([])
    plan.units[0].reported = _reported({"status": PATHY})
    share = rescue.render_plan_text(run, plan, _redactor("share"))
    assert "REPORTED  status: set" in share and "Zqa" not in share
    raw = rescue.render_plan_text(run, plan, Redactor(enabled=False))
    assert f"REPORTED  status: {PATHY}" in raw
    payload = json.dumps(rescue.plan_to_json(run, plan, _redactor("local")))
    assert "Zqa" not in payload


# --- W7-4: every piece of a rescue builder goes through the scan -----------------------------------------


def _row(path_value):
    return rescue.PathRow(
        raw_path=path_value, display_key=path_value, state="DIRTY_NOW", state_reason=None,
        attempt_ordinals=[1], later_write=False,
    )


def _piece_plan(piece):
    run, plan = _plan([])
    unit = plan.units[0]
    if piece == "unit_label":
        unit.label = PATHY
    elif piece == "unit_phase":
        unit.phase = PATHY
    elif piece == "claim_commits":
        unit.reported = _reported({"commits": [PATHY]})
    elif piece == "claim_report_path":
        unit.reported = _reported({"report_path": PATHY})
    elif piece == "claim_status":
        unit.reported = _reported({"status": PATHY})
    elif piece == "path_display_key":
        unit.paths = [_row(PATHY)]
    elif piece == "path_state_reason":
        row = _row("src/a.py")
        row.state = "UNKNOWN"
        row.state_reason = PATHY
        unit.paths = [row]
    elif piece == "in_flight_tool":
        unit.in_flight = [rescue.InFlightRow(tool=PATHY, raw_path=None, display_key=None)]
    elif piece == "in_flight_path":
        unit.in_flight = [rescue.InFlightRow(tool="Write", raw_path=PATHY, display_key=PATHY)]
    elif piece == "commits":
        unit.commits = [PATHY]
    elif piece == "observation_reason":
        plan.observation_reason = PATHY
    elif piece == "declared_check":
        unit.declared_checks = [f"pytest {PATHY}"]
    elif piece == "killed_label":
        plan.killed_brief = [
            {"label": PATHY, "ordinal": 1, "time": "09:00Z", "silence_seconds": 5.0},
        ]
    elif piece == "killed_time":
        plan.killed_brief = [
            {"label": "impl", "ordinal": 1, "time": PATHY, "silence_seconds": 5.0},
        ]
    elif piece == "failed_label":
        plan.failed_brief = [
            {"label": PATHY, "cause": "usage_limit", "retry_now": "no", "reset_time": None},
        ]
    elif piece == "failed_cause":
        plan.failed_brief = [
            {"label": "impl", "cause": PATHY, "retry_now": "no", "reset_time": None},
        ]
    elif piece == "failed_retry_now":
        plan.failed_brief = [
            {"label": "impl", "cause": "usage_limit", "retry_now": PATHY, "reset_time": None},
        ]
    elif piece == "failed_reset_time":
        plan.failed_brief = [
            {"label": "impl", "cause": "usage_limit", "retry_now": "no", "reset_time": PATHY},
        ]
    elif piece == "already_exists_path":
        plan.already_exists_paths = [PATHY]
    elif piece == "already_exists_commit":
        plan.already_exists_commits = [PATHY]
    else:  # pragma: no cover
        raise AssertionError(piece)
    return run, plan


PIECES = [
    "unit_label", "unit_phase", "claim_report_path", "claim_status", "claim_commits",
    "path_display_key",
    "path_state_reason", "in_flight_tool", "in_flight_path", "commits", "observation_reason",
    "declared_check", "killed_label", "killed_time", "failed_label", "failed_cause",
    "failed_retry_now", "failed_reset_time", "already_exists_path", "already_exists_commit",
]


# share prints the fixed word `set` for the status claim: that row is covered by the test above
TEXT_ROWS = [
    pytest.param(piece, level, id=f"{piece}-{level}")
    for level in LEVELS for piece in PIECES
    if not (level == "share" and piece == "claim_status")
]


@pytest.mark.parametrize("piece,level", TEXT_ROWS)
def test_fc7_w4_every_piece_of_the_plan_text_is_scanned(piece, level):
    run, plan = _piece_plan(piece)
    text = rescue.render_plan_text(run, plan, _redactor(level), evidence=True)
    for word in PRIVATE:
        assert word not in text, (piece, word, text)


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("piece", PIECES)
def test_fc7_w4_every_piece_of_the_plan_json_is_scanned(piece, level):
    run, plan = _piece_plan(piece)
    payload = json.dumps(rescue.plan_to_json(run, plan, _redactor(level), evidence=True))
    for word in PRIVATE:
        assert word not in payload, (piece, word, payload)


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w4_the_brief_scans_reset_time_as_its_own_piece(level):
    run, plan = _piece_plan("failed_reset_time")
    plan.failed_brief.append(
        {"label": "other", "cause": "usage_limit", "retry_now": "yes", "reset_time": "9am (UTC)"}
    )
    text = rescue.render_plan_text(run, plan, _redactor(level))
    assert "resets 9am (UTC)" in text, text
    assert "Zqa" not in text and "Zqb" not in text, text


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w4_a_path_in_one_piece_does_not_hide_the_next_piece(level):
    run, plan = _piece_plan("path_display_key")
    plan.units[0].commits = ["abcdef1"]
    text = rescue.render_plan_text(run, plan, _redactor(level))
    assert "COMMITS   reported by these attempts: abcdef1" in text, text


# the in-flight path (sonnet minor 8, mutation P7): a path printed on the `IN FLIGHT` line
@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_the_in_flight_path_is_scanned_and_the_tool_word_is_kept(level):
    run, plan = _piece_plan("in_flight_path")
    text = rescue.render_plan_text(run, plan, _redactor(level))
    line = next(item for item in text.splitlines() if "IN FLIGHT" in item)
    assert line.startswith("  IN FLIGHT at the kill: Write ") and line.endswith("(no result recorded)"), line
    assert "Zqa" not in line and "/srv" not in line, line


# --- W7-4: the status text pieces -------------------------------------------------------------------------


def _status_with(unit_label=None, repo=None, command=None, reason=None, excerpt=None):
    run = reader.load_run(FIXTURE)
    last = (
        rescue.CountedRun(3, None, command, "completed_ok") if command is not None else None
    )
    unit = rescue.UnitStatus(
        key="impl", label=unit_label or "impl", checks_declared=1 if command else 0,
        last_counted=last, word="NOT_OBSERVED", writes_after=0, cycles=0, repo=repo,
    )
    not_counted = [rescue.NotCountedRow(7, reason, "x")] if reason is not None else []
    never = [rescue.NeverRow(9, excerpt, "git checkout")] if excerpt is not None else []
    status = rescue.RescueStatus(
        run_id=run.run_id, units=[unit], not_counted=not_counted, never_matches=never,
        transcript_unknown=False, checked=False, repo_root=None, exit_code=0,
    )
    return run, status


@pytest.mark.parametrize("level", LEVELS)
@pytest.mark.parametrize("field", ["unit_label", "repo", "command", "excerpt"])
def test_fc7_w4_every_piece_of_the_status_text_and_json_is_scanned(field, level):
    run, status = _status_with(**{
        "unit_label": {"unit_label": PATHY}, "repo": {"repo": PATHY},
        "command": {"command": f"pytest {PATHY}"}, "excerpt": {"excerpt": f"cat {PATHY}"},
    }[field])
    text = rescue.render_status_text(run, status, _redactor(level), evidence=True)
    payload = json.dumps(rescue.status_to_json(run, status, _redactor(level), evidence=True))
    for word in PRIVATE:
        assert word not in text, (field, word, text)
        assert word not in payload, (field, word, payload)


# --- W7-5: the hook notification run id in the first position (sonnet minor 8, mutation O29) -------------

SESSION_UUID = FAKE_UUID


def _notify(run_id, headline="1 finding"):
    from agent_postmortem import hooks_cli

    return hooks_cli._NotifyInfo(
        run_id=run_id, verdict_word="DEGRADED", headline=headline, is_incomplete=False,
        quiet_unfinished=False, finding_count=1, agent_ids=[],
    )


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_a_uuid_run_id_is_scanned_in_every_position_of_a_grouped_line(level):
    from agent_postmortem import hooks_cli

    lines = hooks_cli._format_notify_lines(
        [_notify(SESSION_UUID), _notify(SESSION_UUID)], _redactor(level),
    )
    assert len(lines) == 1
    assert SESSION_UUID not in lines[0], lines[0]
    assert lines[0].count("uuid:0000") == 2, lines[0]


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_a_uuid_run_id_is_scanned_in_both_positions_of_a_single_unit_line(level):
    from agent_postmortem import hooks_cli

    lines = hooks_cli._format_notify_lines([_notify(SESSION_UUID)], _redactor(level))
    assert SESSION_UUID not in lines[0], lines[0]
    assert lines[0].count("uuid:0000") == 2, lines[0]


# --- W7-2: a tool-built sentence is not rescanned: config warnings ----------------------------------------


def _typed(template, path, untrusted=None):
    """A warning built from typed parts (F fix 8, W8-10): the fixed words around `<cfg>` are text,
    `<cfg>` is the config path, and `untrusted` (a value inside the words) is an untrusted piece."""
    head, _sep, tail = template.partition("<cfg>")
    parts = [config.WarningPart("path", path)]
    if untrusted is None:
        parts = [config.WarningPart("text", head)] + parts + [config.WarningPart("text", tail)]
    else:
        before, _sep, after = tail.partition(untrusted)
        parts = [
            config.WarningPart("text", head), *parts, config.WarningPart("text", before),
            config.WarningPart("untrusted", untrusted), config.WarningPart("text", after),
        ]
    return config.ConfigWarning(*parts)


def _cfg(path, *warnings, untrusted=None):
    return config.Config(
        detectors_off=frozenset(), warnings=[_typed(w, path, untrusted) for w in warnings],
        config_file=path,
    )


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w2_a_warning_about_a_config_in_a_one_segment_home_path_keeps_its_sentence(level):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    cfg = _cfg(
        "/home/user/Zqg.toml", "agent-postmortem: <cfg>: [[repos]] must be an array of tables",
    )
    shown = cfg.shown_warnings(r.display_path, r.scrub_leftover_paths)
    expected = "~/Zqg.toml" if level == "share" else "~/.../Zqg.toml"
    assert shown == [f"agent-postmortem: {expected}: [[repos]] must be an array of tables"], shown


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w2_an_untrusted_piece_of_a_warning_is_scanned_and_the_fixed_words_are_kept(level):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    cfg = _cfg(
        "/srv/Zqa Zqb/config.toml",
        "agent-postmortem: <cfg>: unknown rule id '/srv/Zqc Zqd/x' in [[adopted]]",
        untrusted="/srv/Zqc Zqd/x",
    )
    shown = cfg.shown_warnings(r.display_path, r.scrub_leftover_paths)
    assert len(shown) == 1
    for word in ("Zqa", "Zqb", "Zqc", "Zqd", "/srv"):
        assert word not in shown[0], shown
    assert shown[0].startswith("agent-postmortem: path-1: unknown rule id '"), shown
    assert shown[0].endswith("' in [[adopted]]"), shown


def test_fc7_w2_the_default_shown_warnings_are_unchanged_without_a_scanner():
    cfg = _cfg("/home/user/c.toml", "agent-postmortem: <cfg>: x")
    assert cfg.shown_warnings(lambda value: "P") == ["agent-postmortem: P: x"]


@pytest.mark.parametrize("flag", [None, "--local"])
def test_fc7_w2_the_cli_prints_a_warning_for_a_one_segment_home_config_whole(
    flag, tmp_path, monkeypatch, capsys,
):
    from tests import n3b_planted as plant

    planted = plant.build(tmp_path)
    home = tmp_path / "homedir"
    home.mkdir()
    config_file = home / "Zqg.toml"
    config_file.write_text("repos = 5\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("AGENT_POSTMORTEM_CONFIG", str(config_file))
    _code, _out, err = h.cli(capsys, "scan", "--root", planted.root, *([flag] if flag else []))
    expected = "~/Zqg.toml" if flag is None else "~/.../Zqg.toml"
    assert f"agent-postmortem: {expected}: [[repos]] must be an array of tables" in err, err


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w2_exception_text_keeps_the_message_around_a_displayed_filename(level):
    r = Redactor(context=DisclosureContext(level=level, home=HOME))
    r.fresh_names(detailed=(level == "local"))
    exc = PermissionError(13, "Permission denied", "/home/user/Zqg.toml")
    shown = r.exception_text(exc)
    expected = "~/Zqg.toml" if level == "share" else "~/.../Zqg.toml"
    assert shown == f"[Errno 13] Permission denied: '{expected}'", shown


@pytest.mark.parametrize("level", LEVELS)
def test_fc7_w5_a_uuid_inside_the_notification_headline_is_scanned(level):
    from agent_postmortem import hooks_cli

    lines = hooks_cli._format_notify_lines(
        [_notify("wf_00000000-a1", headline=f"session {SESSION_UUID} failed")], _redactor(level),
    )
    assert SESSION_UUID not in lines[0], lines[0]
    assert "session uuid:0000 failed" in lines[0], lines[0]
    # the run id after it is its own piece, not hidden by the headline
    assert lines[0].endswith("wf_00000000-a1  /postmortem wf_00000000-a1"), lines[0]
