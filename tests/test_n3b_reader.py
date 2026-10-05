"""N3b reader changes (Part 1a): C-5 and P10b.

C-5 (N3-plan P10, docs/DESIGN.md 4.6): a `started` event whose agentId is
missing, null, or a string that is not a canonical 17-hex agent id builds its
Attempt as before but forces Run.status INCOMPLETE with the diagnostic
`journal.jsonl:<line>: started event has no canonical agentId`. A
non-string agentId keeps 4.6's skip and its own diagnostic.

P10b (N3-plan, N3b D-h): a reader diagnostic that quotes a journal key is a
KeyedDiagnostic: its plain string is the safe form (`key@<line>`, never the
key) and `.raw` carries today's exact text for the raw sinks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import reader

C5_TEXT = "started event has no canonical agentId"


def _wf(tmp_path: Path, entries: list[dict]) -> Path:
    run_dir = tmp_path / "wf_00000000-c20"
    run_dir.mkdir()
    (run_dir / "journal.jsonl").write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )
    return run_dir


def _started(key: str, **extra) -> dict:
    return {
        "type": "started", "timestamp": "2026-09-20T09:00:00.000Z", "key": key,
        "label": "impl:x", "phase": "Implement", **extra,
    }


GOOD_ID = "ac900000000000001"


def test_canonical_agent_id_adds_no_c5_diagnostic(tmp_path):
    run = reader.load_run(_wf(tmp_path, [{"type": "launched"}, _started("v2:k", agentId=GOOD_ID)]))
    assert not [d for d in run.diagnostics if C5_TEXT in d]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"agentId": None},
        {"agentId": ""},
        {"agentId": "not-an-agent-id"},
        {"agentId": "AC900000000000001"},
        {"agentId": "ac90000000000000"},
        {"agentId": "ac9000000000000011"},
    ],
    ids=["missing", "null", "empty", "words", "uppercase", "short", "long"],
)
def test_c5_non_canonical_agent_id_forces_incomplete(tmp_path, extra):
    run = reader.load_run(_wf(tmp_path, [{"type": "launched"}, _started("v2:k", **extra)]))
    assert f"journal.jsonl:2: {C5_TEXT}" in run.diagnostics
    assert run.status == "INCOMPLETE"
    assert len(run.attempts) == 1  # the Attempt is still built, as today


def test_c5_diagnostic_names_the_line_of_each_started_event(tmp_path):
    run = reader.load_run(
        _wf(
            tmp_path,
            [
                {"type": "launched"},
                _started("v2:a", agentId=GOOD_ID),
                _started("v2:b"),
                _started("v2:c", agentId=None),
            ],
        )
    )
    c5 = [d for d in run.diagnostics if C5_TEXT in d]
    assert c5 == [f"journal.jsonl:3: {C5_TEXT}", f"journal.jsonl:4: {C5_TEXT}"]


def test_non_string_agent_id_keeps_the_existing_skip_and_diagnostic(tmp_path):
    run = reader.load_run(_wf(tmp_path, [{"type": "launched"}, _started("v2:k", agentId=123)]))
    assert "journal.jsonl:2: started event has a non-string agentId" in run.diagnostics
    assert not [d for d in run.diagnostics if C5_TEXT in d]
    assert run.status == "INCOMPLETE"
    assert run.attempts == []


# --- P10b: a diagnostic that would quote a journal key ------------------------

SENSITIVE_KEY = "v2:sensitive-client-key"
PLANTED_AGENT_ID = "ac900000000000009"
EMBEDDING_KEY = "v2:" + PLANTED_AGENT_ID + "0123456789abcdef" * 2 + "0123456789abcde"


def _key_journal(key: str) -> list[dict]:
    """Line 1 launched; 2 started (superseded); 3 started; 4 result; 5 an
    orphan failed event; 6 a result whose agentId does not match."""
    return [
        {"type": "launched"},
        _started(key, agentId="ac900000000000001"),
        _started(key, agentId="ac900000000000002"),
        {"type": "result", "key": key, "agentId": "ac900000000000002", "result": {"ok": True}},
        {"type": "failed", "key": key, "agentId": "ac900000000000002"},
        _started("v2:other", agentId="ac900000000000003"),
        {"type": "result", "key": "v2:other", "agentId": "ac900000000000004", "result": {}},
    ]


@pytest.mark.parametrize("key", [SENSITIVE_KEY, EMBEDDING_KEY], ids=["arbitrary", "valid-shaped"])
def test_p10b_plain_diagnostic_never_quotes_the_key(tmp_path, key):
    run = reader.load_run(_wf(tmp_path, _key_journal(key)))
    joined = "\n".join(run.diagnostics)
    assert key not in joined
    assert PLANTED_AGENT_ID not in joined
    assert "journal.jsonl: key@2 attempt 1 was replaced by a later attempt with no recorded outcome" in run.diagnostics
    assert "journal.jsonl: 'failed' event for key@5 has no open attempt" in run.diagnostics
    assert (
        "journal.jsonl: 'result' event for key@7 does not match the open attempt's agent id"
        in run.diagnostics
    )


def test_p10b_raw_attribute_is_todays_exact_text(tmp_path):
    run = reader.load_run(_wf(tmp_path, _key_journal(SENSITIVE_KEY)))
    raws = [reader_raw(d) for d in run.diagnostics if "key@" in d]
    assert (
        f"journal.jsonl: key {SENSITIVE_KEY!r} attempt 1 was replaced by a later attempt "
        "with no recorded outcome"
    ) in raws
    assert f"journal.jsonl: 'failed' event for key {SENSITIVE_KEY!r} has no open attempt" in raws
    assert (
        "journal.jsonl: 'result' event for key 'v2:other' does not match the open attempt's "
        "agent id"
    ) in raws


def reader_raw(diagnostic):
    return diagnostic.raw


@pytest.mark.parametrize("key", [SENSITIVE_KEY, EMBEDDING_KEY], ids=["arbitrary", "valid-shaped"])
def test_p10b_cli_json_share_and_raw(tmp_path, capsys, key):
    from agent_postmortem.cli import main

    run_dir = _wf(tmp_path, _key_journal(key))

    main(["run", str(run_dir), "--json"])
    share = capsys.readouterr().out
    assert key not in share
    assert PLANTED_AGENT_ID not in share
    assert "journal.jsonl: key@2 attempt 1 was replaced by a later attempt" in share

    main(["run", str(run_dir), "--json", "--no-redact"])
    raw = capsys.readouterr().out
    assert json.dumps(f"journal.jsonl: key {key!r} attempt 1 was replaced by a later attempt "
                      "with no recorded outcome") in raw


def test_p10b_text_at_share_and_local_never_carries_the_key(tmp_path, capsys):
    from agent_postmortem.cli import main

    run_dir = _wf(tmp_path, _key_journal(EMBEDDING_KEY))
    for level_flags in ([], ["--local"]):
        main(["run", str(run_dir), "--verbose", "--evidence", *level_flags])
        out = capsys.readouterr().out
        assert EMBEDDING_KEY not in out
        assert PLANTED_AGENT_ID not in out
    # No text sink prints Run.diagnostics today (only JSON does), so the key
    # cannot reach text; this pins that a future text sink starts safe.
