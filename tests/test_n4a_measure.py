"""The N4a measurement script, asserted against a small hand-counted corpus.

The script itself lives in the private, untracked .reviews/tooling/n4/
directory; this test is skipped where it is absent. The corpus is built here
from synthetic commands only. Every expected number below was counted by hand
from the call list in the fixture builder.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".reviews" / "tooling" / "n4" / "measure.py"

pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="private measure.py is not present")


def _load():
    spec = importlib.util.spec_from_file_location("n4a_measure", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _use(tool_id, name, input_obj, cwd="/tmp/ws"):
    return {
        "type": "assistant",
        "cwd": cwd,
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
        },
    }


def _result(tool_id, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def _write(path, entries):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")


# (id, tool, input, result text, is_error)
SESSION_CALLS = [
    ("t01", "Bash", {"command": "cat f"}, None, False),
    ("t02", "Bash", {"command": "make"}, None, False),
    ("t03", "Bash", {"command": "echo x > out"}, None, False),
    ("t04", "Bash", {"command": "nohup make"}, None, False),
    ("t05", "Monitor", {"command": "tail -f x.log"}, None, False),
    ("t06", "Bash", {"command": "codex exec x"}, None, False),
    ("t07", "Bash", {"command": "git commit -m x"}, "[main abc1234] x", False),
    ("t08", "Bash", {"command": "git -C /r commit -m y"}, "[main bbbbbbb] y", False),
    ("t09", "Bash", {"command": "git commit -m z ; echo q"}, "[main ccccccc] z", False),
    ("t10", "Bash", {"command": "cd /abs && git commit -m w"}, "[main ddddddd] w", False),
    ("t11", "Bash", {"command": "echo $X"}, None, False),
    ("t12", "Bash", {"command": "cd sub && ls"}, None, False),
    ("t13", "Bash", {"command": "ls; cd /x; ls"}, None, False),
    ("t14", "Bash", {"command": "make &"}, None, False),
    ("t15", "Bash", {"command": "cat f", "run_in_background": True}, None, False),
    ("t16", "Bash", {"command": None}, None, False),
    ("t17", "Bash", {"command": "git commit -m e"}, "[main eeeeeee] e", True),
    ("t18", "Bash", {"command": "cd - && ls"}, None, False),
    ("t19", "Bash", {"command": "cd /a ; ls"}, None, False),
    ("t20", "Bash", {"command": "pushd /a && ls"}, None, False),
    ("t21", "Bash", {"command": "cd $D && ls"}, None, False),
]

RUN_CALLS = [
    ("j1", "Bash", {"command": "rm f"}, None, False),
    ("j2", "Bash", {"command": "git commit -m a && git commit -m b"},
     "[main aaaaaaa] a\n[main bbbbbbb] b", False),
    ("j3", "Bash", {"command": "cat f"}, None, False),
]


def _entries(calls):
    out = []
    for tool_id, name, input_obj, text, is_error in calls:
        out.append(_use(tool_id, name, input_obj))
        if text is not None:
            out.append(_result(tool_id, text, is_error))
    return out


@pytest.fixture()
def corpus(tmp_path):
    root = tmp_path / "projects"
    _write(root / "-tmp-proj" / "s1.jsonl", _entries(SESSION_CALLS))
    run = root / "-tmp-proj" / "s1" / "workflows" / "wf_00000000-m01"
    _write(run / "agent-a0000000000000001.jsonl", _entries(RUN_CALLS))
    (run / "journal.jsonl").write_text("{}\n", encoding="utf-8")
    return root


def test_counters_match_the_hand_count(corpus):
    measure = _load().measure(corpus)
    expected = {
        # transcripts
        "transcripts.files": 1,
        "transcripts.files_with_shell_call": 1,
        "transcripts.files_with_possible_call": 1,
        "transcripts.calls_total": 21,
        "transcripts.calls_by_tool.Bash": 20,
        "transcripts.calls_by_tool.Monitor": 1,
        "transcripts.row_distribution.row2_background": 3,
        "transcripts.row_distribution.row3_delegation": 1,
        "transcripts.row_distribution.row4_read_only": 3,
        "transcripts.row_distribution.row5_tiny_write": 1,
        "transcripts.row_distribution.row6_opaque": 12,
        "transcripts.row_distribution.other": 1,
        "transcripts.possible_calls": 17,
        "transcripts.bash_classifier_possible_share": 0.8095,
        "transcripts.row2_kind.nohup": 1,
        "transcripts.row2_kind.trailing_ampersand": 1,
        "transcripts.row2_kind.run_in_background": 1,
        "transcripts.row2_kind.other": 0,
        "transcripts.row6_split.placed_not_recognized": 10,
        "transcripts.row6_split.not_placed": 2,
        "transcripts.row6_program.make": 1,
        "transcripts.row6_program.git": 4,
        "transcripts.row6_program.other": 5,
        "transcripts.row6_lexer_reason.expansion": 2,
        "transcripts.row6_lexer_reason.other": 0,
        "transcripts.cd_shapes.no_cd": 13,
        "transcripts.cd_shapes.leading_cd_absolute_and": 1,
        "transcripts.cd_shapes.leading_cd_relative_and": 1,
        "transcripts.cd_shapes.leading_cd_other_form": 1,
        "transcripts.cd_shapes.leading_cd_joined_other": 1,
        "transcripts.cd_shapes.cd_not_leading": 1,
        "transcripts.cd_shapes.cd_in_opaque_command": 1,
        "transcripts.cd_shapes.pushd_popd": 1,
        "transcripts.cd_shapes.other": 0,
        "transcripts.commit_calls_seen_by_either_rule": 5,
        # A1 (rev 11): t09 (`;` after the commit) and t17 (an is_error result) were
        # only_v02 under N4a (2 both, 2 only_v02); both are now counted by L8 too.
        "transcripts.commit_found_by_both": 4,
        "transcripts.commit_both_same_ids": 4,
        "transcripts.commit_both_different_ids": 0,
        "transcripts.commit_only_v02": 0,
        "transcripts.commit_only_v02_reason.not_pure_and_chain": 0,
        "transcripts.commit_only_v02_reason.not_completed_ok": 0,
        "transcripts.commit_only_v02_reason.other": 0,
        "transcripts.commit_only_l8": 1,
        "transcripts.commit_only_l8_reason.git_global_options": 1,
        "transcripts.commit_only_l8_reason.other": 0,
        "transcripts.commit_call_without_ids": 0,
        # journals
        "journals.journal_files": 1,
        "journals.files": 1,
        "journals.files_with_shell_call": 1,
        "journals.files_with_possible_call": 1,
        "journals.calls_total": 3,
        "journals.calls_by_tool.Bash": 3,
        "journals.row_distribution.row4_read_only": 1,
        "journals.row_distribution.row6_opaque": 2,
        "journals.row_distribution.other": 0,
        "journals.possible_calls": 2,
        "journals.bash_classifier_possible_share": 0.6667,
        "journals.row6_split.placed_not_recognized": 2,
        "journals.row6_program.rm": 1,
        "journals.row6_program.git": 1,
        "journals.cd_shapes.no_cd": 3,
        "journals.commit_calls_seen_by_either_rule": 1,
        "journals.commit_found_by_both": 1,
        "journals.commit_both_same_ids": 1,
        "journals.commit_only_v02": 0,
        "journals.commit_only_l8": 0,
        # both
        "all.bash_classifier_possible_share": 0.7917,
    }
    for key, value in expected.items():
        assert measure.get(key) == value, (key, measure.get(key), value)


def test_every_breakdown_has_an_other_row(corpus):
    measure = _load().measure(corpus)
    for source in ("transcripts", "journals"):
        for group in ("row_distribution", "row2_kind", "row6_program", "row6_lexer_reason",
                      "cd_shapes", "commit_only_v02_reason", "commit_only_l8_reason"):
            assert f"{source}.{group}.other" in measure, (source, group)


def test_an_empty_corpus_prints_zero_rows_and_null_shares(tmp_path):
    (tmp_path / "projects").mkdir()
    measure = _load().measure(tmp_path / "projects")
    assert measure["transcripts.calls_total"] == 0
    assert measure["transcripts.bash_classifier_possible_share"] is None
    assert measure["all.bash_classifier_possible_share"] is None
    assert measure["transcripts.row_distribution.other"] == 0


def test_printed_output_carries_no_command_text(corpus, capsys):
    module = _load()
    module.main(["measure.py", str(corpus)])
    printed = capsys.readouterr().out
    assert "transcripts.calls_total: 21" in printed
    for secret in ("abc1234", "bbbbbbb", "/abs", "/tmp/ws", "codex exec", "cat f", "x.log", "rm f"):
        assert secret not in printed, secret
