"""`status` follows the disclosure levels (docs/DESIGN.md 4.13.5, 4.14.4; 0.4).

Field reports 2026-10-30: "redacted agent ids cannot be traced back" (a CLAIM row named an
alias that matched no agent file, and only --no-redact helped) and "status shows a blocked
background session that cannot be identified" (`--no-redact` printed the same alias, and the
session's name was nowhere). At the default level a row stays an alias and the output says once
how to see more; `--local` prints the real agent id and a background session's name and working
directory name; `--raw` also prints the session id and the full directory.
"""

from __future__ import annotations

import json
import re

from live_helpers import FAKE_UUID, user, write_jsonl
from test_live_session import SLUG, T0
from test_live_status import _busy_session

from agent_postmortem.cli import main

BG_ID = f"00000000-0000-4000-8000-{0xB9:012x}"
BG_NAME = "zz-bg-session-name"
BG_CWD = "/tmp/zz-bg-work/zz-bg-repo"
HINT = "--local prints agent ids and background session names"


def _with_blocked_job(tmp_path, **fields):
    write_jsonl(tmp_path / "projects" / SLUG / f"{BG_ID}.jsonl", [user(None, T0, "x")])
    job = tmp_path / "jobs" / "000000b9"
    job.mkdir(parents=True)
    (job / "state.json").write_text(json.dumps({
        "state": "blocked", "detail": "private detail", "sessionId": BG_ID, "name": BG_NAME,
        "cwd": BG_CWD, "updatedAt": "2026-10-30T12:00:00.000Z", **fields,
    }), encoding="utf-8")


def _status(capsys, main_path, *flags):
    assert main(["status", str(main_path), *flags]) == 0
    return capsys.readouterr().out


def test_share_rows_are_aliases_and_the_output_says_how_to_see_more(tmp_path, capsys):
    main_path, (running, _clean, killed, claimed) = _busy_session(tmp_path)
    out = _status(capsys, main_path)
    for agent in (running, killed, claimed):
        assert agent not in out
    assert re.search(r"^  INTERRUPTED\s+a:[0-9a-f]{12}", out, re.M)
    assert out.rstrip("\n").splitlines()[-1] == f"({HINT})"


def test_local_and_raw_rows_name_the_real_agent_id(tmp_path, capsys):
    main_path, (running, _clean, killed, claimed) = _busy_session(tmp_path)
    for flag in ("--local", "--raw"):
        out = _status(capsys, main_path, flag)
        assert re.search(rf"^  INTERRUPTED\s+{killed}\b", out, re.M), out
        assert re.search(rf"^  RUNNING\s+{running}\b", out, re.M), out
        assert re.search(rf"^  CLAIM\s+{claimed}\b", out, re.M), out
        assert not re.search(r"^  [A-Z]+\s+a:[0-9a-f]{12}", out, re.M), out
        assert HINT not in out


def test_json_carries_the_agent_id_only_at_local_and_raw(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    share = json.loads(_status(capsys, main_path, "--json"))
    assert all("agent_id" not in row for row in share["subagents"])
    for flag in ("--local", "--raw"):
        payload = json.loads(_status(capsys, main_path, "--json", flag))
        assert all(row["agent_id"] == row["agent"] for row in payload["subagents"])
        assert all(re.fullmatch(r"a[0-9a-f]{16}", row["agent_id"]) for row in payload["subagents"])


def test_a_blocked_background_session_is_named_at_local_and_raw_only(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    _with_blocked_job(tmp_path)
    share = _status(capsys, main_path)
    line = next(line for line in share.splitlines() if line.startswith("  BLOCKED"))
    assert re.match(r"  BLOCKED\s+s_[0-9a-f]{10}  for ", line), line
    assert BG_NAME not in share and "zz-bg" not in share and BG_ID not in share
    local = _status(capsys, main_path, "--local")
    line = next(line for line in local.splitlines() if line.startswith("  BLOCKED"))
    assert re.match(rf"  BLOCKED\s+s_[0-9a-f]{{10}}  {BG_NAME} \(zz-bg-repo\)  for ", line), line
    assert BG_ID not in local and "/tmp/zz-bg-work" not in local
    raw = _status(capsys, main_path, "--raw")
    line = next(line for line in raw.splitlines() if line.startswith("  BLOCKED"))
    assert re.match(rf"  BLOCKED\s+{BG_ID}  {BG_NAME} \({re.escape(BG_CWD)}\)  for ", line), line
    for text in (share, local, raw):
        assert "private detail" not in text


def test_background_json_follows_the_same_levels(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    _with_blocked_job(tmp_path)
    share = json.loads(_status(capsys, main_path, "--json"))["background"]["sessions"]
    assert share and set(share[0]) == {"session", "state", "age_seconds", "class", "mine"}
    local = json.loads(_status(capsys, main_path, "--json", "--local"))["background"]["sessions"]
    assert (local[0]["name"], local[0]["cwd"]) == (BG_NAME, "zz-bg-repo")
    assert "session_id" not in local[0]
    raw = json.loads(_status(capsys, main_path, "--json", "--raw"))["background"]["sessions"]
    assert (raw[0]["session_id"], raw[0]["name"], raw[0]["cwd"]) == (BG_ID, BG_NAME, BG_CWD)


def test_a_session_with_nothing_listed_prints_no_hint(tmp_path, capsys):
    main_path = tmp_path / "projects" / SLUG / f"{FAKE_UUID}.jsonl"
    write_jsonl(main_path, [user(None, T0, "start")])
    out = _status(capsys, main_path)
    assert HINT not in out


# --- batch 2 review ----------------------------------------------------------------------------


def test_a_name_holding_a_path_or_a_session_id_is_redacted_at_local_in_text_and_json(
    tmp_path, capsys,
):
    main_path, _ids = _busy_session(tmp_path)
    hidden_id = f"00000000-0000-4000-8000-{0xBA:012x}"
    name = f"{hidden_id} fix /opt/zz-private/p"
    assert len(name) <= 60  # the whole name survives the 60-character cut
    _with_blocked_job(tmp_path, name=name)
    for flags in (("--local",), ("--local", "--json")):
        out = _status(capsys, main_path, *flags)
        assert "/opt/zz-private" not in out and hidden_id not in out, (flags, out)
        assert " fix " in out


def test_control_characters_never_reach_the_terminal_from_a_cwd(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    _with_blocked_job(tmp_path, cwd="/tmp/zz-bg-work/zz-repo\nINJECTED\u001b[31m")
    for flag in ("--local", "--raw"):
        out = _status(capsys, main_path, flag)
        assert "\x1b" not in out
        assert not any(line.startswith("INJECTED") for line in out.splitlines()), out


def test_a_nameless_job_still_shows_its_folder(tmp_path, capsys):
    main_path, _ids = _busy_session(tmp_path)
    _with_blocked_job(tmp_path, name="")
    local = _status(capsys, main_path, "--local")
    line = next(line for line in local.splitlines() if line.startswith("  BLOCKED"))
    assert "(zz-bg-repo)" in line, line
    raw = _status(capsys, main_path, "--raw")
    line = next(line for line in raw.splitlines() if line.startswith("  BLOCKED"))
    assert f"({BG_CWD})" in line, line
