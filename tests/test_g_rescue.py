"""G-A, R1: `rescue plan` and `rescue status` for a session with no rescue unit but a child that
has no established return (docs/DESIGN.md 4.11, 5.3b, plan rev 5 R1), and the two new JSON keys
`empty_state` and `return_breakdown` in both shapes.
"""

from __future__ import annotations

import json

import pytest

from tests.g_helpers import (
    Session,
    X,
    async_launch,
    child_entries,
    iso,
    sync_launch,
    touch_age,
)
from tests.n5c2_helpers import run_cli

TWO_DAYS = 2 * 86400
SENTINEL = "[Request interrupted by user]"


def rescue(capsys, s, sub, *flags):
    return run_cli(capsys, ["rescue", sub, str(s.main_path), "--root", str(s.root), *flags])


def stale_pending(tmp_path):
    s = Session(tmp_path)
    s.main(async_launch("t1", X, 1)).child(X)
    s.build()
    touch_age(s.child_path(X), TWO_DAYS)
    touch_age(s.main_path, TWO_DAYS)
    return s


@pytest.mark.parametrize("sub", ["plan", "status"])
def test_no_rescue_unit_text_and_exit(tmp_path, capsys, sub):
    s = stale_pending(tmp_path)
    code, out, _err = rescue(capsys, s, sub)
    assert code == 4
    first = next(line for line in out.splitlines() if "RESCUE" in line)
    run_id = json.loads(rescue(capsys, s, sub, "--json")[1])["run"]["run_id"]
    assert first == (
        "NO RESCUE UNIT: 1 subagent has no established return (1 no return evidence found); "
        f"run agent-postmortem run {run_id} to see WORK AT RISK"
    )
    assert "NOTHING TO RESCUE" not in out


@pytest.mark.parametrize("sub", ["plan", "status"])
def test_no_rescue_unit_json_keys(tmp_path, capsys, sub):
    s = stale_pending(tmp_path)
    code, out, _err = rescue(capsys, s, sub, "--json")
    payload = json.loads(out)
    assert code == 4
    assert payload["empty_state"] == "no_rescue_unit_unestablished_returns"
    assert payload["return_breakdown"] == {
        "reported_failed": 0, "reported_killed": 0, "possibly_running": 0,
        "no_return_evidence": 1, "not_established": 0,
    }


def test_the_breakdown_counts_every_class(tmp_path, capsys):
    s = Session(tmp_path)
    ids = [X, "a" + "1" * 16, "a" + "2" * 16, "a" + "3" * 16, "a" + "4" * 16]
    s.main(
        sync_launch("t1", ids[0], 1, "failed"), sync_launch("t2", ids[1], 4, "killed"),
        async_launch("t3", ids[2], 7),
        sync_launch("t4", ids[3], 10), sync_launch("t5", ids[3], 13),
    )
    for child in ids[:4]:
        s.child(child)
    s.build()
    touch_age(s.child_path(ids[0]), TWO_DAYS)
    touch_age(s.child_path(ids[1]), TWO_DAYS)
    touch_age(s.child_path(ids[3]), TWO_DAYS)
    _code, out, _err = rescue(capsys, s, "plan", "--json")
    assert json.loads(out)["return_breakdown"] == {
        "reported_failed": 1, "reported_killed": 1, "possibly_running": 1,
        "no_return_evidence": 0, "not_established": 1,
    }


@pytest.mark.parametrize("sub", ["plan", "status"])
def test_a_session_with_every_child_returned_is_not_a_no_rescue_unit(tmp_path, capsys, sub):
    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    code, out, _err = rescue(capsys, s, sub, "--json")
    payload = json.loads(out)
    assert payload["empty_state"] == "nothing_yet"
    assert payload["return_breakdown"] is None
    assert "NO RESCUE UNIT" not in rescue(capsys, s, sub)[1]


@pytest.mark.parametrize("sub", ["plan", "status"])
def test_real_units_keep_their_text_and_null_keys(tmp_path, capsys, sub):
    s = Session(tmp_path)
    y = "a" + "5" * 16
    killed = child_entries(X, 1, 2)
    killed.append({"type": "user", "message": {"role": "user", "content": SENTINEL},
                   "timestamp": iso(3), "agentId": X})
    s.main(sync_launch("t1", X, 1), async_launch("t2", y, 4))
    s.child(X, killed, default=False).child(y)
    s.build()
    touch_age(s.child_path(y), TWO_DAYS)
    code, out, _err = rescue(capsys, s, sub)
    assert "NO RESCUE UNIT" not in out
    payload = json.loads(rescue(capsys, s, sub, "--json")[1])
    assert payload["units"] and payload["empty_state"] is None
    assert payload["return_breakdown"] is None


@pytest.mark.parametrize("sub", ["plan", "status"])
def test_workflow_rescue_carries_the_keys(tmp_path, monkeypatch, capsys, sub):
    from tests.n5b_helpers import Spec, build_workflow

    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-a20", [Spec("v2:x", 1)])
    code, out, _err = run_cli(
        capsys, ["rescue", sub, str(run_dir), "--root", str(tmp_path / "root"), "--json"],
    )
    payload = json.loads(out)
    assert payload["empty_state"] in {"nothing_to_rescue", "nothing_yet"}
    assert payload["return_breakdown"] is None


@pytest.mark.parametrize("sub", ["plan", "status"])
def test_unchecked_risk_state_name(tmp_path, monkeypatch, capsys, sub):
    from agent_postmortem import rescue as rescue_module

    s = Session(tmp_path)
    s.main(sync_launch("t1", X, 1)).child(X)
    s.build()
    monkeypatch.setattr(rescue_module, "empty_unit_state", lambda _run: ("risk", 2))
    _code, out, _err = rescue(capsys, s, sub, "--json")
    payload = json.loads(out)
    assert payload["empty_state"] == "unchecked_risk"
    assert payload["return_breakdown"] is None


def test_a_no_rescue_unit_plan_is_not_nothing_to_rescue(tmp_path, capsys):
    s = stale_pending(tmp_path)
    payload = json.loads(rescue(capsys, s, "plan", "--json")[1])
    assert payload["nothing_to_rescue"] is False and payload["units"] == []
    assert payload["brief"] is None and payload["exit_code"] == 4
