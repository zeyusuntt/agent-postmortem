"""G-B, G8 (v0.3.1): the corpus-check invariants 6 to 13, each with a passing and a violating
synthetic fixture. The checker lives in tests/g_corpus_support.py (a maintainer check, not shipped).
A violating fixture is a real synthetic tree whose loaded model, rendered
text or JSON is changed after the fact, or a pure value that disagrees.
"""

from __future__ import annotations

import json

import pytest

from agent_postmortem import cli, reader
from tests import g_corpus_support
from tests.g_helpers import Session, X, Y, async_launch, sync_launch
from tests.n5b_helpers import Spec, build_workflow
from tests.n5c2_helpers import run_cli


@pytest.fixture(scope="module")
def g():
    return g_corpus_support


def _session(tmp_path, *entries, children=(X,)):
    s = Session(tmp_path)
    s.main(*entries)
    for child in children:
        s.child(child)
    s.build()
    return s, reader.load_session(s.main_path)


def _json(capsys, s):
    return json.loads(run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root),
                                       "--json"])[1])


def _text(capsys, s):
    return run_cli(capsys, ["run", str(s.main_path), "--root", str(s.root)])[1]


# 6 ---------------------------------------------------------------------------------------------


def test_inv6_pure(g):
    same = g.inv6({"a": ("returned", "")}, {"a": ("returned", "")}, {"a"}, set())
    assert (same["all"].checked, same["all"].violations, same["nested"].checked) == (1, 0, 1)
    more = g.inv6({"a": ("returned", "")}, {"a": ("unknown", "x")}, set(), {"a"})
    assert more["all"].detail["reader_proves_more"] == 1 and more["resumed"].violations == 1
    less = g.inv6({"a": ("unknown", "x")}, {"a": ("reported_failed", "")}, set(), set())
    assert less["all"].detail["independent_proves_more"] == 1


def test_inv6_collector_passes_then_is_made_to_fail(g, tmp_path):
    s, run = _session(tmp_path, sync_launch("t1", X, 1, "completed"))
    assert g.collect_inv6(run, s.main_path)["all"].violations == 0
    run.attempts[0].return_state = "reported_failed"
    assert g.collect_inv6(run, s.main_path)["all"].violations == 1


# 7 ---------------------------------------------------------------------------------------------


def test_inv7_pure(g):
    assert g.inv7(["completed"], ["completed"], 0, 0).violations == 0
    bad = g.inv7(["completed"], ["killed"], 1, 0)
    assert bad.violations == 2 and bad.detail["outcome"] == 1 and bad.detail["interrupted"] == 1


def test_inv7_collector(g, tmp_path, capsys):
    s, run = _session(tmp_path, sync_launch("t1", X, 1, "failed"))
    payload = _json(capsys, s)
    assert g.collect_inv7(run, payload, s.main_path).violations == 0
    payload["run"]["attempts"][0]["outcome"] = "completed"
    assert g.collect_inv7(run, payload, s.main_path).violations == 1


# 8, 9 ------------------------------------------------------------------------------------------


def test_inv8_pure(g):
    assert g.inv8({"a": "completed"}, set()).violations == 0
    assert g.inv8({"a": "completed"}, {"a"}).violations == 1
    assert g.inv8({"a": "completed", "b": "open"}, {"b"}).violations == 0


def test_inv8_real_report_passes(g, tmp_path):
    s, run = _session(tmp_path, async_launch("t1", X, 1), sync_launch("t2", Y, 5),
                      children=(X, Y))
    tally = g.collect_inv8(run)
    assert (tally.checked, tally.violations) == (1, 0)


def test_inv9_pure_and_real(g, tmp_path, capsys):
    s, _run = _session(tmp_path, sync_launch("t1", X, 1, "completed"))
    text = _text(capsys, s)
    assert "WORK AT RISK  none" in text
    assert g.inv9(text, ["returned"]).violations == 0
    assert g.inv9(text, ["returned", "unknown"]).violations == 1


# 10 --------------------------------------------------------------------------------------------

DENIED = "Permission for this action was denied by the Claude Code auto mode classifier."


def _denial_run(tmp_path):
    from tests.n4b2_helpers import result, use, workflow_run

    return workflow_run(tmp_path, [use("t1", "Bash", {"command": "ls"}),
                                   result("t1", DENIED, is_error=True)])


def test_inv10_pure(g):
    assert g.inv10({"a": {"user": 1}}, {"a": {"user": 1, "auto_mode": 0}}).violations == 0
    assert g.inv10({"a": {"user": 1}}, {}).detail["detector_missing"] == 1
    assert g.inv10({}, {"a": {"user": 1}}).detail["detector_extra"] == 1


def test_inv10_collector(g, tmp_path):
    run = _denial_run(tmp_path)
    findings = cli._run_findings(run)
    tally, agree = g.collect_inv10(run, findings)
    assert (tally.violations, agree.violations) == (0, 0) and tally.checked == 1
    dropped = [f for f in findings if f.detector != "permission_denied"]
    assert g.collect_inv10(run, dropped)[0].violations == 1


# 11 --------------------------------------------------------------------------------------------


def test_inv11_pure(g):
    assert g.inv11({"a": True, "b": False}, {"a": 1}).violations == 0
    assert g.inv11({"a": True}, {}).violations == 1
    assert g.inv11({"a": True}, {"a": 2}).violations == 1
    assert g.inv11({"a": False}, {"a": 1}).detail["unexpected"] == 1


@pytest.mark.parametrize("result,want", [
    ({"x_failed": 1}, True), ({"blocked": True}, True), ({"tests_failed": 2}, True),
    ({"ok": False}, True), ({"blockers": ["a"]}, True), ({"x_failed": 0}, False),
    ({"blocked": "yes"}, False), ("text", False),
])
def test_expected_major(g, result, want):
    assert g.expected_major(result) is want


def test_inv11_collector(g, tmp_path, monkeypatch):
    run_dir = build_workflow(tmp_path, monkeypatch, "wf_00000000-ea7",
                             [Spec("v2:x", 1, result={"ci_failed": 1})])
    run = reader.load_run(run_dir)
    findings = cli._run_findings(run)
    assert g.collect_inv11(run, findings).violations == 0
    assert g.collect_inv11(run, []).violations == 1


# 12 --------------------------------------------------------------------------------------------


def test_inv12(g):
    assert g.inv12("NO RESCUE UNIT: 1 subagent", ["unknown"]).violations == 0
    assert g.inv12("NOTHING TO RESCUE: every attempt completed.", ["unknown"]).violations == 1
    assert g.inv12("NOTHING TO RESCUE: every attempt completed.", ["returned"]).violations == 0


# 13 --------------------------------------------------------------------------------------------


def test_inv13_pure(g):
    assert g.inv13({"wf"}, {"wf"}, set()).violations == 0
    bad = g.inv13({"wf"}, set(), set())
    assert bad.violations == 1


def test_inv13_collector_and_the_resolver_mutation(g, tmp_path, monkeypatch):
    from tests.n6_helpers import SLUG_A, aid, entry, session_path, uid, write_lines, write_workflow
    from tests.test_g_here import T0

    caller = (tmp_path / "work" / "repo").resolve()
    caller.mkdir(parents=True)
    monkeypatch.chdir(caller)
    root = tmp_path / "root"
    path = session_path(root, SLUG_A, uid(1))
    write_lines(path, [entry("user", "w", T0, cwd=str(caller), session=uid(1))])
    write_workflow(root, SLUG_A, uid(1), "wf_00000000-ea8", ((aid(1), "/work/elsewhere", T0),))
    ref = reader.RunRef(kind="session", path=path, run_id="x")
    tally = g.collect_inv13(root, ref, cli, g.quiet_main)
    assert (tally.checked, tally.violations) == (1, 0)
    assert tally.detail["before_included"] == 0 and tally.detail["after_included"] == 1
    monkeypatch.setattr(reader, "caller_workflow_paths", lambda *a, **k: frozenset())
    assert g.collect_inv13(root, ref, cli, g.quiet_main).violations == 1
