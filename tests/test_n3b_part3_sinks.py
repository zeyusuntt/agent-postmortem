"""N3b Part 3 per-sink share tests over the planted run (spec "Per-sink
tests"): a planted label, phase title, repo path, raw agent id, session UUID
and project slug are absent from every hook message and every watch line,
while the expected line exists and carries the alias.
"""

from __future__ import annotations

import io
import json

from agent_postmortem.cli import main
from tests import n3b_helpers as h
from tests import n3b_planted as plant
from tests import test_watch as tw


def _hook_check(monkeypatch, capsys, planted, *, active=False):
    main_transcript = planted.root / planted.slug / f"{planted.session}.jsonl"
    main_transcript.write_text(
        json.dumps({"type": "user", "timestamp": "2026-02-01T00:00:00.000Z",
                    "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )
    payload = {"hook_event_name": "Stop", "transcript_path": str(main_transcript),
               "session_id": planted.session, "stop_hook_active": active}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    capsys.readouterr()
    code = main(["hook", "check"])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_hook_check_over_the_planted_run_names_aliases_and_leaks_nothing(
    tmp_path, monkeypatch, capsys,
):
    planted = plant.build(tmp_path)
    # N5b (4.13.4): the runtime kill of the planted run's first attempt is a
    # minor finding now; this test names that agent, so the notify threshold
    # is lowered to minor.
    monkeypatch.setenv("AGENT_POSTMORTEM_NOTIFY", "minor")
    for active in (False, True):
        # a second invocation must stay silent for the same findings, so each
        # form gets its own state file
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / f"state-{active}"))
        _code, out, err = _hook_check(monkeypatch, capsys, planted, active=active)
        assert out, "the hook emitted nothing for the planted run"
        assert err == ""
        message = json.loads(out)["systemMessage"]
        alias = h.alias_of("agent", planted.agent1)
        assert f"a:{alias}" in message
        assert "/postmortem wf_00000000-b10" in message
        h.assert_nothing_planted(out, planted)


def test_watch_over_the_planted_run_names_aliases_and_leaks_nothing(
    tmp_path, monkeypatch, capsys,
):
    planted = plant.build(tmp_path)
    for flags in ([], ["--json"]):
        clock = tw._Clock()
        tw._patch_clock(monkeypatch, clock)
        tw._patch_scripted_sleep(monkeypatch, [], clock)
        capsys.readouterr()
        main(["watch", str(planted.run_dir), "--interval", "1", *flags])
        captured = capsys.readouterr()
        out = captured.out
        alias = h.alias_of("agent", planted.agent1)
        assert f"a:{alias}" in out
        assert "KILL" in out
        assert captured.err == ""
        h.assert_nothing_planted(out, planted)
        if flags:
            for line in out.splitlines():
                json.loads(line)
