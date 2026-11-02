"""A FAILED verdict says what failed when every agent completed (docs/DESIGN.md 4.7 rule 2;
field report 2026-10-30, "codex_unavailable and claim_contradiction caught real misses": the
table showed every agent completed, so FAILED alone was puzzling).

The verdict comes from Claude Code's own report that the workflow failed (4.13.3). When every
agent's last attempt still completed, the account says so in its first lines and names what to
check: the workflow's own result.
"""

from __future__ import annotations

from agent_postmortem.cli import main
from tests.test_field_v03 import _case5_fixture

SENTENCE = (
    "Claude Code reported the workflow failed, though every agent's last attempt completed: "
    "check what the workflow itself returned."
)


def test_a_failed_workflow_whose_agents_all_completed_says_what_failed(tmp_path, monkeypatch,
                                                                        capsys):
    run_dir, _parent = _case5_fixture(tmp_path, monkeypatch)
    assert main(["run", str(run_dir)]) == 3
    lines = capsys.readouterr().out.splitlines()
    assert "FAILED" in lines[0]
    assert SENTENCE in lines[1:4], lines[:5]


def test_a_clean_run_has_no_such_sentence(tmp_path, monkeypatch, capsys):
    from tests.test_field_v03 import _no_config
    from tests.test_share_label_case import _workflow
    _no_config(monkeypatch, tmp_path)
    _root, run_dir, _main = _workflow(tmp_path)
    main(["run", str(run_dir)])
    assert "reported the workflow failed" not in capsys.readouterr().out
