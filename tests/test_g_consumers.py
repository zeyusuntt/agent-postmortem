"""G-A consumers of `return_state` (plan rev 5): E1 exported outcome and interrupted counts, G1 END,
W1 WORK AT RISK words, G6 one name per agent, G9 RESUMES column, and the consumer pins that must
NOT move (KILLED, scan totals, risk population, WASTED).

Fixtures are synthetic, built in tmp_path; every id is built in code.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_postmortem import cli, detectors, reader
from tests.g_helpers import (
    SESSION,
    Session,
    async_launch,
    child_entries,
    iso,
    note,
    resumed,
    send,
    sync_launch,
    touch_age,
    use,
)
from tests.n5c2_helpers import run_cli

A1, A2, A3, A4, A5, A6 = ("a" + str(n) * 16 for n in range(1, 7))
SENTINEL = "[Request interrupted by user]"
TWO_DAYS = 2 * 86400


def effective_end(attempt):
    from agent_postmortem.models import effective_end as real

    return real(attempt)


def sentinel_child(child, start=1, end=3):
    entries = child_entries(child, start, end - 1)
    entries.append({
        "type": "user", "message": {"role": "user", "content": SENTINEL},
        "timestamp": iso(end), "agentId": child,
    })
    return entries


def labelled(label):
    return {"description": label}


def run_text(capsys, s, *flags):
    main = s.build() if not s.main_path.exists() else s.main_path
    code, out, _err = run_cli(capsys, ["run", str(main), "--root", str(s.root), *flags])
    return code, out


def run_json(capsys, s, *flags):
    _code, out = run_text(capsys, s, "--json", "--raw", *flags)
    return json.loads(out)


def attempts_by_id(payload):
    return {a["agent_id"]: a for a in payload["run"]["attempts"]}


def mixed_session(tmp_path):
    """One child per state: A1 returned, A2 reported_failed, A3 reported_killed, A4 pending,
    A5 unknown (no launch)."""
    s = Session(tmp_path)
    s.main(
        sync_launch("t1", A1, 1), sync_launch("t2", A2, 4, "failed"),
        sync_launch("t3", A3, 7, "killed"), async_launch("t4", A4, 10),
    )
    for child, label in ((A1, "alpha"), (A2, "beta"), (A3, "gamma"), (A4, "delta"), (A5, "eps")):
        s.child(child, meta=labelled(label))
    s.build()
    return s


# --- E1 ---------------------------------------------------------------------------------------


def test_exported_outcome_is_the_effective_end(tmp_path, capsys):
    payload = run_json(capsys, mixed_session(tmp_path))
    got = {
        child: (a["outcome"], a["return_state"], a["return_reason"], a["resumes"])
        for child, a in attempts_by_id(payload).items()
    }
    assert got == {
        A1: ("completed", "returned", "none", 0),
        A2: ("failed", "reported_failed", "none", 0),
        A3: ("killed", "reported_killed", "none", 0),
        A4: ("open", "pending", "none", 0),
        A5: ("unknown", "unknown", "no_evidence", 0),
    }


def test_the_kill_sentinel_takes_precedence_in_the_effective_end(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(sync_launch("t1", A1, 1)).child(A1, sentinel_child(A1), default=False)
    attempt = attempts_by_id(run_json(capsys, s))[A1]
    assert (attempt["outcome"], attempt["return_state"]) == ("killed", "returned")


def test_the_internal_outcome_is_unchanged_and_drives_risk_and_the_effective_end_does_not(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", A1, 1, "failed"), sync_launch("t2", A2, 4, "killed"))
    s.child(A1).child(A2)
    run = reader.load_session(s.build())
    assert [a.outcome for a in run.attempts] == ["completed", "completed"]
    from agent_postmortem import ledger

    assert ledger.risk_population(run) == []
    assert effective_end(run.attempts[0]) == "failed"
    assert effective_end(run.attempts[1]) == "killed"


def test_interrupted_counts_every_interrupt_and_nothing_else(tmp_path, capsys):
    """A sentinel child counts; a notification-only kill (no sentinel) counts in neither
    `counts.interrupted` nor the KILLED column; the KILLED column drops a parent stop."""
    s = Session(tmp_path)
    s.main(
        sync_launch("t1", A1, 1), sync_launch("t2", A2, 4), async_launch("t3", A3, 7),
        note(30, A3, "t3", "killed"),
        use("tstop", 20, "TaskStop", {"task_id": A2}),
    )
    s.child(A1, sentinel_child(A1, 1, 300), default=False, meta=labelled("alpha"))
    s.child(A2, sentinel_child(A2, 4, 21), default=False, meta=labelled("beta"))
    s.child(A3, meta=labelled("gamma"))
    payload = run_json(capsys, s)
    assert payload["run"]["counts"]["interrupted"] == 2
    by_id = attempts_by_id(payload)
    assert by_id[A3]["outcome"] == "killed" and by_id[A3]["interrupt"] is None
    assert by_id[A2]["interrupt"]["cause"] == "parent_stop"
    _code, out = run_text(capsys, s, "--local")
    assert "2 interrupted" in out
    # KILLED: alpha unattributed 1, beta parent stop 0, gamma notification-only kill 0
    header = next(line for line in out.splitlines() if line.startswith("AGENT"))
    column = header.index("KILLED")
    cells = {
        name: out_line[column:column + 8].strip()
        for out_line in out.splitlines() for name in ("alpha", "beta", "gamma")
        if out_line.startswith(name)
    }
    assert cells == {"alpha": "1", "beta": "0", "gamma": "0"}


def test_scan_totals_count_interrupts_not_effective_ends(tmp_path):
    s = Session(tmp_path)
    s.main(sync_launch("t1", A1, 1), sync_launch("t2", A2, 4, "killed"))
    s.child(A1, sentinel_child(A1), default=False).child(A2)
    run = reader.load_session(s.build())
    totals = cli._empty_scan_totals()
    cli._accumulate_scan_totals(totals, run, detectors.run_detectors(run))
    assert totals["session"]["interrupted"] == 1
    assert effective_end(run.attempts[1]) == "killed"


# --- G1 ---------------------------------------------------------------------------------------


def end_words(out):
    words = {}
    for line in out.splitlines():
        match = re.match(r"^(alpha|beta|gamma|delta|eps)\s+(\d+)\s+(\d+\s+)?(.{1,19}?)\s{2,}", line)
        if match:
            words[match.group(1)] = match.group(4).strip()
    return words


def test_end_words_for_proven_states(tmp_path, capsys):
    s = mixed_session(tmp_path)
    for child in (A1, A2, A3):
        touch_age(s.child_path(child), 30)
    _code, out = run_text(capsys, s, "--local")
    words = end_words(out)
    assert (words["alpha"], words["beta"], words["gamma"]) == ("completed", "failed", "killed")


@pytest.mark.parametrize("age,expected", [(30, "possibly running"), (TWO_DAYS, "unknown")])
def test_end_of_an_open_or_unknown_child_follows_the_mtime(tmp_path, capsys, age, expected):
    s = mixed_session(tmp_path)
    for child in (A1, A2, A3, A4, A5):
        touch_age(s.child_path(child), age)
    _code, out = run_text(capsys, s, "--local")
    words = end_words(out)
    assert (words["delta"], words["eps"]) == (expected, expected)


def test_a_proven_state_is_never_changed_by_the_mtime(tmp_path, capsys):
    s = mixed_session(tmp_path)
    for child in (A1, A2, A3):
        touch_age(s.child_path(child), TWO_DAYS)
    _code, out = run_text(capsys, s, "--local")
    words = end_words(out)
    assert (words["alpha"], words["beta"], words["gamma"]) == ("completed", "failed", "killed")


# --- W1 ---------------------------------------------------------------------------------------


def risk_block(out):
    lines = out.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("WORK AT RISK"))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        block.append(line)
    return block


def test_work_at_risk_lists_every_reason_in_breakdown_order(tmp_path, capsys):
    s = mixed_session(tmp_path)
    for child in (A1, A2, A3):
        touch_age(s.child_path(child), TWO_DAYS)
    touch_age(s.child_path(A4), 30)
    touch_age(s.child_path(A5), TWO_DAYS)
    _code, out = run_text(capsys, s, "--local")
    block = risk_block(out)
    assert block[0] == (
        "WORK AT RISK  unknown: 4 subagents have no established return (1 reported failed, "
        "1 reported killed, 1 possibly running, 1 no return evidence found); "
        "the tool cannot say what they wrote."
    )
    assert block[1:] == [
        f"  {A2} (beta)  reported failed",
        f"  {A3} (gamma)  reported killed",
        f"  {A4} (delta)  possibly running",
        f"  {A5} (eps)  no return evidence found",
    ]


def test_work_at_risk_stale_pending_and_stale_unknown_share_a_phrase_and_other_unknowns_differ(
    tmp_path, capsys,
):
    s = Session(tmp_path)
    s.main(
        async_launch("t1", A1, 1),
        sync_launch("t2", A2, 4), sync_launch("t3", A2, 7),
    )
    s.child(A1, meta=labelled("alpha")).child(A2, meta=labelled("beta"))
    s.build()
    for child in (A1, A2):
        touch_age(s.child_path(child), TWO_DAYS)
    _code, out = run_text(capsys, s, "--local")
    block = risk_block(out)
    assert "(1 no return evidence found, 1 return could not be established)" in block[0]
    assert f"  {A1} (alpha)  no return evidence found" in block
    assert f"  {A2} (beta)  return could not be established" in block


def test_work_at_risk_nested_child_says_in_its_parent_agent(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(sync_launch("tx", A1, 1))
    s.child(A1, child_entries(A1), async_launch("ty", A2, 4), meta=labelled("alpha"))
    s.child(A2, meta={"description": "beta", "parentAgentId": A1})
    s.build()
    for child in (A1, A2):
        touch_age(s.child_path(child), TWO_DAYS)
    _code, out = run_text(capsys, s, "--local")
    assert f"  {A2} (beta)  no return evidence found in its parent agent" in risk_block(out)


def test_work_at_risk_trailing_line_when_the_population_is_not_empty(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(sync_launch("t1", A1, 1), sync_launch("t2", A2, 4, "failed"))
    s.child(A1, sentinel_child(A1), default=False, meta=labelled("alpha"))
    s.child(A2, meta=labelled("beta"))
    s.build()
    touch_age(s.child_path(A1), TWO_DAYS)
    touch_age(s.child_path(A2), TWO_DAYS)
    _code, out = run_text(capsys, s, "--local")
    assert (
        f"  1 subagent outside this list has no established return: {A2} (beta)"
        in risk_block(out)
    )


def test_work_at_risk_none_needs_every_child_returned(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(sync_launch("t1", A1, 1)).child(A1, meta=labelled("alpha"))
    _code, out = run_text(capsys, s, "--local")
    assert risk_block(out)[0] == "WORK AT RISK  none: no attempt was killed, failed, or left unfinished."


def test_no_string_never_returned_is_left_in_src():
    src = Path(reader.__file__).parent
    hits = [p.name for p in src.rglob("*.py") if "never returned" in p.read_text(encoding="utf-8")]
    assert hits == []


# --- G6 ---------------------------------------------------------------------------------------


def test_names_at_share_local_and_raw_with_duplicate_labels(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(async_launch("t1", A1, 1), async_launch("t2", A2, 4))
    s.child(A1, meta=labelled("same")).child(A2, meta=labelled("same"))
    s.build()
    for child in (A1, A2):
        touch_age(s.child_path(child), TWO_DAYS)
    _c, share = run_text(capsys, s)
    lines = [line for line in risk_block(share)[1:]]
    assert len(lines) == 2
    aliases = []
    for line in lines:
        match = re.fullmatch(r"  (a:[0-9a-z]+) \((agent-\d+)\)  no return evidence found", line)
        assert match, line
        aliases.append(match.group(1))
    assert len(set(aliases)) == 2
    assert A1 not in share and A2 not in share and "same" not in share
    for flags in (["--local"], ["--raw"]):
        _c, text = run_text(capsys, s, *flags)
        assert f"  {A1} (same)  no return evidence found" in risk_block(text)
        assert f"  {A2} (same)  no return evidence found" in risk_block(text)


# --- G9 ---------------------------------------------------------------------------------------


def resumed_session(tmp_path):
    s = Session(tmp_path)
    s.main(
        sync_launch("t1", A1, 1), sync_launch("t2", A2, 4),
        send("ts", 10, A1), resumed("ts", 11, A1), note(20, A1, "ts", "completed"),
    )
    s.child(A1, meta=labelled("alpha")).child(A2, meta=labelled("beta"))
    return s


def test_resumes_column_for_a_clean_resumed_child(tmp_path, capsys):
    s = resumed_session(tmp_path)
    s.build()
    _code, out = run_text(capsys, s, "--local")
    header = next(line for line in out.splitlines() if line.startswith("AGENT"))
    assert re.search(r"ATTEMPTS\s+RESUMES\s+END", header)
    rows = {line.split()[0]: line for line in out.splitlines() if re.match(r"^(alpha|beta)\s", line)}
    assert re.match(r"^alpha\s+1\s+1\s+completed", rows["alpha"])
    assert re.match(r"^beta\s+1\s+completed", rows["beta"])
    assert header.index("END") == rows["alpha"].index("completed")
    assert header.index("END") == rows["beta"].index("completed")
    top = next(line for line in out.splitlines() if line.startswith("SESSION"))
    assert "CLEAN" in top
    assert risk_block(out)[0].startswith("WORK AT RISK  none")


def test_no_resumes_column_when_nothing_was_resumed(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(sync_launch("t1", A1, 1)).child(A1, meta=labelled("alpha"))
    _code, out = run_text(capsys, s, "--local")
    header = next(line for line in out.splitlines() if line.startswith("AGENT"))
    assert "RESUMES" not in header


def test_interrupted_counts_a_human_interrupt_but_the_killed_column_does_not(tmp_path, capsys):
    """The parent session's own interrupt lands beside the child's: cause `human`."""
    s = Session(tmp_path)
    s.main(
        sync_launch("t1", A1, 1),
        {"type": "user", "message": {"role": "user", "content": SENTINEL},
         "timestamp": iso(50), "sessionId": SESSION},
    )
    s.child(A1, sentinel_child(A1, 1, 51), default=False, meta=labelled("alpha"))
    payload = run_json(capsys, s)
    assert payload["run"]["counts"]["interrupted"] == 1
    assert attempts_by_id(payload)[A1]["interrupt"]["cause"] == "human"
    _code, out = run_text(capsys, s, "--local")
    header = next(line for line in out.splitlines() if line.startswith("AGENT"))
    row = next(line for line in out.splitlines() if line.startswith("alpha"))
    assert row[header.index("KILLED"):header.index("KILLED") + 8].strip() == "0"


def test_a_hostile_label_in_a_work_at_risk_line_leaks_no_path(tmp_path, capsys):
    s = Session(tmp_path)
    s.main(async_launch("t1", A1, 1))
    s.child(A1, meta=labelled("work in /tmp/zz/secret/proj"))
    s.build()
    for flags in ([], ["--local"]):
        _code, out = run_text(capsys, s, *flags)
        line = risk_block(out)[1]
        assert "/tmp/zz" not in line
        assert line.endswith("  possibly running")


def test_interrupted_counts_every_interrupt_of_a_workflow_too(tmp_path, monkeypatch, capsys):
    """A workflow attempt the journal closed `failed` that also ends in the kill sentinel keeps
    `outcome` failed and still counts as one interrupt (E1: counts.interrupted is the number of
    interrupts, not the number of killed outcomes)."""
    from tests.n5b_helpers import Spec, build_workflow

    run_dir = build_workflow(
        tmp_path, monkeypatch, "wf_00000000-a21",
        [Spec("v2:x", 1, journal="failed", kill=True), Spec("v2:y", 2)],
    )
    code, out, _err = run_cli(
        capsys, ["run", str(run_dir), "--root", str(tmp_path / "root"), "--json", "--raw"],
    )
    payload = json.loads(out)
    assert payload["run"]["counts"]["interrupted"] == 1
    assert sorted(a["outcome"] for a in payload["run"]["attempts"]) == ["completed", "failed"]
