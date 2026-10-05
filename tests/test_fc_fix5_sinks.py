"""Fix round 5 (W5-1 to W5-4) of field milestone F through the rescue, check and name sinks the
reviewers list (docs/DESIGN.md 5.1, 4.11; .reviews/v0.3/F-dispositions.md, "Fix round 4 (W)
review"): the new inputs reach rescue text and JSON excerpts, declared checks at local and the
local name values.

Every fixture is synthetic.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import rescue
from tests.test_fc_fix4_sinks import _plan, _redactor, _status_outputs

PRIVATE = ("Client", "Secret", "deep", "notes.md")
HIDDEN = " [text hidden]"

EXCERPTS = [
    pytest.param("cannot open /srv/data/Client Secret/deep/notes.md now",
                 f"cannot open path-1{HIDDEN}", id="codex1_unquoted_space"),  # F fix 6 (W6-2)
    pytest.param("cat „/srv/data/Client Secret/deep/n.md“ now",
                 f"cat „path-1{HIDDEN}“ now", id="codex1_german_low_quotes"),
    pytest.param('cat "/srv/data/Client\\\r\nSecret/deep/notes.md" now',
                 None, id="codex2_backslash_crlf_in_double_quotes"),
    pytest.param("see `/srv/data/Client Secret/deep/notes.md` now",
                 f"see `path-1{HIDDEN}` now", id="markdown_code_span"),
    pytest.param('error="/srv/private/file failed with EACCES"',
                 f'error="path-1{HIDDEN}"', id="codex3_prose_in_a_quote"),
]


@pytest.mark.parametrize("excerpt,expected", EXCERPTS)
def test_fc5_a_local_rescue_excerpt_shows_no_private_tail_and_the_marker(excerpt, expected):
    text, payload = _status_outputs(excerpt, ())
    for word in PRIVATE:
        assert word not in text, (word, text)
        assert word not in payload, (word, payload)
    if expected is not None:
        assert f"line 9: {expected}" in text, text
        assert json.loads(payload)["never_matches"][0]["excerpt"] == expected, payload


def test_fc5_a_local_rescue_excerpt_keeps_the_keys_of_compact_json():
    excerpt = '{"p":"/srv/data/Client/n.md","s":"failed","n":7}'
    text, payload = _status_outputs(excerpt, ())
    expected = '{"p":"path-1","s":"failed","n":7}'
    assert json.loads(payload)["never_matches"][0]["excerpt"] == expected, payload
    assert f"line 9: {expected}" in text, text


CHECK_ROWS = [
    "pytest /srv/data/Client Secret/deep/notes.md -k x",
    'pytest „/srv/data/Client Secret/deep/notes.md“ -k x',
    'pytest "/srv/data/Client\\\r\nSecret/deep/notes.md" -k x',
]


@pytest.mark.parametrize("check", [
    pytest.param(CHECK_ROWS[0], id="unquoted_space"),
    pytest.param(CHECK_ROWS[1], id="german_low_quotes"),
    pytest.param(CHECK_ROWS[2], id="backslash_crlf"),
])
def test_fc5_a_local_declared_check_shows_no_private_tail(check):
    run, plan = _plan([check])
    text = rescue.render_plan_text(run, plan, _redactor("local"))
    payload = json.dumps(rescue.plan_to_json(run, plan, _redactor("local")))
    for word in PRIVATE:
        assert word not in text, (word, text)
        assert word not in payload, (word, payload)
    assert "pytest" in text and "pytest" in payload
    share = rescue.render_plan_text(run, plan, _redactor("share"))
    assert "check-1" in share and "pytest" not in share


@pytest.mark.parametrize("value", [
    pytest.param("fix /srv/data/Client Secret/deep/notes.md now", id="unquoted_space"),
    pytest.param("fix „/srv/data/Client Secret/deep/notes.md“ now", id="german_low_quotes"),
    pytest.param('fix "/srv/data/Client\\\r\nSecret/deep/notes.md" now', id="backslash_crlf"),
])
def test_fc5_a_local_name_value_shows_no_private_tail(value):
    r = _redactor("local")
    names = r.fresh_names(detailed=True)
    for shown in (names.agent(value), names.description(value), names.phase(value),
                  names.local_text(value), r.title(value)):
        for word in PRIVATE:
            assert word not in shown, (word, shown)


# --- scan triage and the hook notification ------------------------------------------------------------

TRIAGE_ROWS = [
    pytest.param("cannot open /srv/data/Client Secret/deep/notes.md now",
                 f"cannot open path-1{HIDDEN}", id="unquoted_space"),  # F fix 6 (W6-2)
    pytest.param("cat „/srv/data/Client Secret/deep/n.md“ now",
                 f"cat „path-1{HIDDEN}“ now", id="german_low_quotes"),
    pytest.param('error="/srv/private/file failed with EACCES"',
                 f'error="path-1{HIDDEN}"', id="prose_in_a_quote"),
]


@pytest.mark.parametrize("level", ["share", "local"])
@pytest.mark.parametrize("text,expected", TRIAGE_ROWS)
def test_fc5_scan_triage_scans_the_reason_and_the_command(level, text, expected):
    from types import SimpleNamespace

    from agent_postmortem import report
    from tests.test_fc_scan import TOTALS

    candidate = {
        "run": SimpleNamespace(run_id="wf_00000000-a1"), "age": "1d", "verdict": "DEGRADED",
        "reason": text, "command": f"cat {text}",
    }
    out = report.render_scan_triage_text([candidate], 1, 1, TOTALS, TOTALS, _redactor(level))
    for word in PRIVATE:
        assert word not in out, (word, out)
    assert f"DEGRADED  {expected}" in out, out


@pytest.mark.parametrize("level", ["share", "local"])
def test_fc5_a_hook_notification_headline_is_scanned_piece_by_piece(level):
    from agent_postmortem import hooks_cli

    notify = hooks_cli._NotifyInfo(
        run_id="wf_00000000-a1", verdict_word="DEGRADED",
        headline="cannot open /srv/data/Client Secret/deep/notes.md now",
        is_incomplete=False, quiet_unfinished=False, finding_count=1, agent_ids=[],
    )
    lines = hooks_cli._format_notify_lines([notify], _redactor(level))
    for word in PRIVATE:
        assert word not in lines[0], (word, lines[0])
    assert f"cannot open path-1{HIDDEN}" in lines[0] and "now" not in lines[0]  # F fix 6 (W6-2)
    assert lines[0].endswith("/postmortem wf_00000000-a1"), lines[0]
