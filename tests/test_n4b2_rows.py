"""N4b-2 coverage group 6 (ledger entries per call): the truth table rows 1, 7
and 8 and the use of N4a's classification for rows 2 to 6, the completion
mapping (L7), file tools (L10), the evidence line and entry order (L11), and
the `repo` of each kind of entry (docs/DESIGN.md 4.13.1; .reviews/v0.3/N4-plan.md
L6, L7, L10, L11).

Every row asserts the whole entry, all six fields.
"""

from __future__ import annotations

import pytest

from agent_postmortem import reader
from tests.n4b2_helpers import (
    ANCHORED,
    CWD,
    NAME,
    ledger,
    one_call,
    read_calls,
    result,
    use,
    write_lines,
)


def entry(effect, basis, completion, path, repo, line):
    return {"effect": effect, "basis": basis, "completion": completion, "path": path,
            "repo": repo, "line": line}


def entries_of(call, name=NAME):
    return [e.to_dict() for e in ledger.call_entries(call, name).entries]


def reasons_of(call, name=NAME):
    return ledger.call_entries(call, name).reasons


# --- row 1: the four file tools, every completion ---------------------------------------

FILE_TOOLS = [
    ("Edit", "file_path"), ("Write", "file_path"), ("MultiEdit", "file_path"),
    ("NotebookEdit", "notebook_path"),
]


@pytest.mark.parametrize("tool,field", FILE_TOOLS)
def test_a_file_tool_that_succeeded_is_observed(tmp_path, tool, field):
    call = one_call(tmp_path, tool, {field: "/tmp/ws/a.py"}, "ok")
    assert entries_of(call) == [
        entry("observed", "tool_result", "success", "/tmp/ws/a.py", CWD, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset()


@pytest.mark.parametrize("tool,field", FILE_TOOLS)
def test_a_file_tool_that_errored_is_attempted(tmp_path, tool, field):
    call = one_call(tmp_path, tool, {field: "/tmp/ws/a.py"}, "boom", is_error=True)
    assert entries_of(call) == [
        entry("attempted", "tool_result", "error", "/tmp/ws/a.py", CWD, f"{NAME}:2")
    ]


@pytest.mark.parametrize("tool,field", FILE_TOOLS)
def test_a_file_tool_with_no_result_is_attempted_on_its_own_line(tmp_path, tool, field):
    call = one_call(tmp_path, tool, {field: "/tmp/ws/a.py"})
    assert entries_of(call) == [
        entry("attempted", "tool_result", "no_result", "/tmp/ws/a.py", CWD, f"{NAME}:1")
    ]
    assert reasons_of(call) == frozenset({"unpaired_write_call"})


@pytest.mark.parametrize("tool,field", FILE_TOOLS)
def test_a_file_tool_with_an_ambiguous_id_is_attempted(tmp_path, tool, field):
    calls = read_calls(tmp_path, [
        use("t1", tool, {field: "/tmp/ws/a.py"}), use("t1", tool, {field: "/tmp/ws/b.py"}),
        result("t1", "ok"),
    ])
    # only the later duplicate is paired with the result (reader's id index), so
    # the first one is on its own tool_use line and also unpaired
    assert entries_of(calls[0]) == [
        entry("attempted", "tool_result", "ambiguous", "/tmp/ws/a.py", CWD, f"{NAME}:1")
    ]
    assert entries_of(calls[1]) == [
        entry("attempted", "tool_result", "ambiguous", "/tmp/ws/b.py", CWD, f"{NAME}:3")
    ]
    assert reasons_of(calls[0]) == frozenset({"ambiguous_tool_id", "unpaired_write_call"})
    assert reasons_of(calls[1]) == frozenset({"ambiguous_tool_id"})


def test_a_write_whose_result_says_it_was_backgrounded_has_no_result_completion(tmp_path):
    call = one_call(tmp_path, "Write", {"file_path": "/tmp/ws/a.py"}, ANCHORED)
    assert call.completion == "backgrounded"
    assert entries_of(call) == [
        entry("attempted", "tool_result", "no_result", "/tmp/ws/a.py", CWD, f"{NAME}:2")
    ]


def test_a_write_with_a_nonzero_exit_code_in_its_result_is_an_error(tmp_path):
    call = one_call(tmp_path, "Write", {"file_path": "/tmp/ws/a.py"}, "Exit code 2")
    assert call.completion == "completed_error"
    assert entries_of(call)[0]["completion"] == "error"
    assert entries_of(call)[0]["effect"] == "attempted"


@pytest.mark.parametrize("tool,field", FILE_TOOLS)
def test_a_file_tool_with_a_missing_path_has_a_null_path_and_a_reason(tmp_path, tool, field):
    call = one_call(tmp_path, tool, {}, "ok")
    assert entries_of(call) == [entry("observed", "tool_result", "success", None, CWD, f"{NAME}:2")]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


@pytest.mark.parametrize("bad", [5, None, ["a"], {"p": 1}])
def test_a_file_tool_with_a_non_string_path_has_a_null_path_and_a_reason(tmp_path, bad):
    call = one_call(tmp_path, "Edit", {"file_path": bad}, "ok")
    assert entries_of(call) == [entry("observed", "tool_result", "success", None, CWD, f"{NAME}:2")]
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


def test_notebook_edit_reads_notebook_path_not_file_path(tmp_path):
    call = one_call(tmp_path, "NotebookEdit", {"file_path": "/tmp/ws/a.ipynb"}, "ok")
    assert entries_of(call)[0]["path"] is None
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


def test_edit_reads_file_path_not_notebook_path(tmp_path):
    call = one_call(tmp_path, "Edit", {"notebook_path": "/tmp/ws/a.py"}, "ok")
    assert entries_of(call)[0]["path"] is None
    assert reasons_of(call) == frozenset({"unexpected_entry_shape"})


@pytest.mark.parametrize("tool,field", FILE_TOOLS)
def test_a_path_of_1024_characters_is_kept_and_1025_is_truncated(tmp_path, tool, field):
    ok = "/" + "a" * 1023
    too_long = "/" + "a" * 1024
    kept = one_call(tmp_path, tool, {field: ok}, "ok")
    assert entries_of(kept)[0]["path"] == ok
    assert reasons_of(kept) == frozenset()
    cut = one_call(tmp_path, tool, {field: too_long}, "ok")
    assert entries_of(cut) == [entry("observed", "tool_result", "success", None, CWD, f"{NAME}:2")]
    assert reasons_of(cut) == frozenset({"truncated_field"})


@pytest.mark.parametrize("cwd,repo", [("/tmp/ws", "/tmp/ws"), ("/tmp/ws/../ws2", "/tmp/ws2"),
                                      ("relative/dir", None), (None, None)])
def test_a_file_tool_entrys_repo_is_the_calls_absolute_cwd(tmp_path, cwd, repo):
    call = one_call(tmp_path, "Write", {"file_path": "/tmp/ws/a.py"}, "ok", cwd=cwd)
    assert entries_of(call)[0]["repo"] == repo


# --- row 7: the read-only tools never get an entry ------------------------------------

READ_ONLY_TOOLS = [
    "Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch", "ToolSearch", "TaskOutput",
    "TaskGet", "TaskList", "TaskCreate", "TaskUpdate", "TaskStop", "ListAgents", "SendUserFile",
    "AskUserQuestion", "SendFeedback", "Skill", "StructuredOutput", "SubagentHandback",
    "Artifact", "ScheduleWakeup", "TodoWrite",
]


@pytest.mark.parametrize("tool", READ_ONLY_TOOLS)
def test_a_read_only_tool_has_no_entry_and_no_reason(tmp_path, tool):
    call = one_call(tmp_path, tool, {"file_path": "/tmp/ws/a.py", "command": "rm -rf /"}, "ok")
    assert entries_of(call) == []
    assert reasons_of(call) == frozenset()


# --- row 8: every other tool name -----------------------------------------------------


@pytest.mark.parametrize(
    "tool", ["mcp__fs__write_file", "mcp__x", "bash", "Foo", "BashOutput", "Monitoring", ""]
)
def test_any_other_tool_is_possible_unknown_tool(tmp_path, tool):
    call = one_call(tmp_path, tool, {"path": "/tmp/ws/out.txt"}, "ok")
    assert entries_of(call) == [
        entry("possible", "unknown_tool", "success", None, CWD, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset({"possible_unknown_tool"})


def test_an_unknown_tool_with_no_result_is_possible_with_no_result(tmp_path):
    call = one_call(tmp_path, "mcp__fs__write_file", {})
    assert entries_of(call) == [
        entry("possible", "unknown_tool", "no_result", None, CWD, f"{NAME}:1")
    ]
    assert reasons_of(call) == frozenset({"possible_unknown_tool", "unpaired_write_call"})


def test_a_tool_use_block_with_no_name_is_row_8_and_a_shape_reason(tmp_path):
    path = tmp_path / NAME
    write_lines(path, [{
        "type": "assistant",
        "message": {"role": "assistant",
                    "content": [{"type": "tool_use", "id": "t1", "input": {}}]},
    }])
    (call,) = reader._read_transcript(path, [])[5]
    assert call.tool_name is None
    assert entries_of(call) == [
        entry("possible", "unknown_tool", "no_result", None, None, f"{NAME}:1")
    ]


# --- rows 2 to 6 for Bash and Monitor, through N4a's classification --------------------


def test_row_2_background_by_flag_for_bash_and_monitor(tmp_path):
    for tool in ("Bash", "Monitor"):
        call = one_call(tmp_path, tool, {"command": "cat f", "run_in_background": True}, "ok")
        assert entries_of(call) == [
            entry("possible", "background_process", "success", None, CWD, f"{NAME}:2")
        ]
        assert reasons_of(call) == frozenset(
            {"shell_context_unverified", "possible_background_process"}
        )


@pytest.mark.parametrize("command", ["nohup make", "cat f; nohup make", "cat f &", "make &"])
def test_row_2_background_by_nohup_or_trailing_ampersand(tmp_path, command):
    call = one_call(tmp_path, "Bash", {"command": command}, "ok")
    assert entries_of(call) == [
        entry("possible", "background_process", "success", None, CWD, f"{NAME}:2")
    ]


def test_row_2_background_by_an_anchored_result_marker(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "cat f"}, ANCHORED)
    assert entries_of(call) == [
        entry("possible", "background_process", "no_result", None, CWD, f"{NAME}:2")
    ]


def test_row_3_delegation_by_bash_program(tmp_path):
    for command in ("codex exec x", "aider --yes", "claude -p x",
                    "node /tmp/x/codex-companion.mjs task go"):
        call = one_call(tmp_path, "Bash", {"command": command}, "ok")
        assert entries_of(call) == [
            entry("possible", "delegation", "success", None, CWD, f"{NAME}:2")
        ], command
        assert reasons_of(call) == frozenset({"shell_context_unverified", "possible_delegation"})


@pytest.mark.parametrize("tool", ["Agent", "Workflow", "SendMessage", "Task"])
def test_row_3_delegation_tools(tmp_path, tool):
    call = one_call(tmp_path, tool, {"prompt": "go"}, "ok")
    assert entries_of(call) == [entry("possible", "delegation", "success", None, None, f"{NAME}:2")]
    assert reasons_of(call) == frozenset({"possible_delegation"})


def test_a_monitor_call_is_never_delegation(tmp_path):
    call = one_call(tmp_path, "Monitor", {"command": "codex exec x"}, "ok")
    assert entries_of(call) == [
        entry("possible", "opaque_command", "success", None, CWD, f"{NAME}:2")
    ]


def test_row_4_a_read_only_bash_or_monitor_call_has_no_entry(tmp_path):
    for tool in ("Bash", "Monitor"):
        call = one_call(tmp_path, tool, {"command": "cat f"}, "ok")
        assert entries_of(call) == []
        assert reasons_of(call) == frozenset({"shell_context_unverified"})


def test_row_5_the_tiny_grammar_is_claimed_target_on_success(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "echo x > /tmp/ws/f"}, "")
    assert entries_of(call) == [
        entry("claimed_target", "command_grammar", "success", "/tmp/ws/f", CWD, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset({"shell_context_unverified"})


def test_row_5_a_relative_target_resolves_against_the_cwd(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "echo x > sub/f"}, "")
    assert entries_of(call)[0]["path"] == "/tmp/ws/sub/f"


@pytest.mark.parametrize(
    "result_text,is_error,completion,effect",
    [("boom", True, "error", "attempted"), (None, False, "no_result", "attempted"),
     ("first line\n" + ANCHORED, False, "no_result", "attempted"),
     ("Exit code 1", False, "error", "attempted")],
)
def test_row_5_without_success_is_attempted(tmp_path, result_text, is_error, completion, effect):
    call = one_call(tmp_path, "Bash", {"command": "echo x > /tmp/ws/f"}, result_text,
                    is_error=is_error)
    expected_line = f"{NAME}:1" if result_text is None and not is_error else f"{NAME}:2"
    assert entries_of(call) == [
        entry(effect, "command_grammar", completion, "/tmp/ws/f", CWD, expected_line)
    ]


def test_row_5_an_ambiguous_call_is_attempted_with_ambiguous_completion(tmp_path):
    calls = read_calls(tmp_path, [
        use("t1", "Bash", {"command": "echo x > /tmp/ws/f"}),
        use("t1", "Bash", {"command": "echo y > /tmp/ws/g"}), result("t1", ""),
    ])
    assert entries_of(calls[0]) == [
        entry("attempted", "command_grammar", "ambiguous", "/tmp/ws/f", CWD, f"{NAME}:1")
    ]
    assert reasons_of(calls[0]) == frozenset(
        {"shell_context_unverified", "ambiguous_tool_id", "unpaired_write_call"}
    )
    assert entries_of(calls[1]) == [
        entry("attempted", "command_grammar", "ambiguous", "/tmp/ws/g", CWD, f"{NAME}:3")
    ]
    assert reasons_of(calls[1]) == frozenset({"shell_context_unverified", "ambiguous_tool_id"})


def test_row_5_one_entry_per_target_in_target_order(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "touch /tmp/ws/b /tmp/ws/a"}, "")
    assert [e["path"] for e in entries_of(call)] == ["/tmp/ws/b", "/tmp/ws/a"]
    assert {e["line"] for e in entries_of(call)} == {f"{NAME}:2"}


def test_row_5_a_sed_backup_gives_the_file_then_the_backup(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "sed -i.bak 's/x/y/' /tmp/ws/f"}, "")
    assert [e["path"] for e in entries_of(call)] == ["/tmp/ws/f", "/tmp/ws/f.bak"]


def test_row_5_a_target_over_the_limit_is_null_with_truncated_field(tmp_path):
    long_target = "/tmp/" + "a" * 1100
    call = one_call(tmp_path, "Bash", {"command": f"echo x > {long_target}"}, "")
    assert entries_of(call) == [
        entry("claimed_target", "command_grammar", "success", None, CWD, f"{NAME}:2")
    ]
    assert reasons_of(call) == frozenset({"shell_context_unverified", "truncated_field"})


def test_row_5_a_leading_cd_is_not_honored_by_the_tiny_grammar(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "cd /tmp/other && echo x > f"}, "")
    # a leading cd is not honored by the tiny grammar, so the command is opaque
    assert entries_of(call)[0]["basis"] == "opaque_command"


def test_row_6_an_opaque_command_is_possible(tmp_path):
    for tool in ("Bash", "Monitor"):
        call = one_call(tmp_path, tool, {"command": "make all"}, "ok")
        assert entries_of(call) == [
            entry("possible", "opaque_command", "success", None, CWD, f"{NAME}:2")
        ]
        assert reasons_of(call) == frozenset(
            {"shell_context_unverified", "possible_opaque_command"}
        )


def test_row_6_a_monitor_call_matching_the_tiny_grammar_text_is_opaque(tmp_path):
    call = one_call(tmp_path, "Monitor", {"command": "touch f"}, "ok")
    assert entries_of(call) == [
        entry("possible", "opaque_command", "success", None, CWD, f"{NAME}:2")
    ]


def test_row_6_repo_is_the_classification_candidate(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "cd /tmp/other && make"}, "ok")
    assert entries_of(call) == [
        entry("possible", "opaque_command", "success", None, "/tmp/other", f"{NAME}:2")
    ]


def test_row_6_with_no_cwd_has_a_null_repo(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "make"}, "ok", cwd=None)
    assert entries_of(call)[0]["repo"] is None


def test_a_command_over_2048_characters_is_classified_from_its_full_text(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "echo " + "a" * 3000 + " &"}, "ok")
    assert call.command is None
    assert entries_of(call)[0]["basis"] == "background_process"


# --- the completion mapping on a tiny-grammar row, one per source completion --------------


@pytest.mark.parametrize(
    "result_text,is_error,expected",
    [("", False, "success"), ("x", True, "error"), (None, False, "no_result"),
     ("ok\n" + ANCHORED, False, "no_result")],
)
def test_the_completion_mapping_on_a_bash_row(tmp_path, result_text, is_error, expected):
    call = one_call(tmp_path, "Bash", {"command": "echo x > /tmp/ws/f"}, result_text,
                    is_error=is_error)
    assert entries_of(call)[0]["completion"] == expected


# --- order and evidence (L11) ----------------------------------------------------------


def test_entries_follow_tool_use_order_not_result_arrival_order(tmp_path):
    entries = [
        use("t1", "Write", {"file_path": "/tmp/ws/one"}),
        use("t2", "Write", {"file_path": "/tmp/ws/two"}),
        result("t2", "ok"),
        result("t1", "ok"),
    ]
    calls = read_calls(tmp_path, entries)
    built = ledger.build_ledger(calls, NAME, ledger.TranscriptState())
    assert [e.to_dict() for e in built.entries] == [
        entry("observed", "tool_result", "success", "/tmp/ws/one", CWD, f"{NAME}:4"),
        entry("observed", "tool_result", "success", "/tmp/ws/two", CWD, f"{NAME}:3"),
    ]


def test_a_call_with_two_targets_keeps_them_together_in_order(tmp_path):
    entries = [
        use("t1", "Write", {"file_path": "/tmp/ws/one"}),
        use("t2", "Bash", {"command": "touch /tmp/ws/b /tmp/ws/a"}),
        use("t3", "Edit", {"file_path": "/tmp/ws/three"}),
        result("t1", "ok"), result("t2", ""), result("t3", "ok"),
    ]
    built = ledger.build_ledger(read_calls(tmp_path, entries), NAME, ledger.TranscriptState())
    assert [e.path for e in built.entries] == [
        "/tmp/ws/one", "/tmp/ws/b", "/tmp/ws/a", "/tmp/ws/three"
    ]


def test_the_ledger_of_a_clean_file_tool_transcript_is_complete(tmp_path):
    calls = read_calls(tmp_path, [use("t1", "Write", {"file_path": "/tmp/ws/a"}), result("t1")])
    built = ledger.build_ledger(calls, NAME, ledger.TranscriptState())
    assert built.completeness == "complete"
    assert built.reasons == []


# --- a shell call with commit segments names the commit repositories (C-10) -------------


def test_a_git_c_commit_names_the_dash_c_repository(tmp_path):
    call = one_call(
        tmp_path, "Bash",
        {"command": "git -C /tmp/agent-postmortem-fixture/repo -c user.name=x commit -m msg"},
        "[main 1a2b3c4] msg",
    )
    assert entries_of(call) == [
        entry("possible", "opaque_command", "success", None,
              "/tmp/agent-postmortem-fixture/repo", f"{NAME}:2")
    ]


def test_a_commit_after_an_absolute_cd_names_that_directory(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "cd /tmp/other && git commit -m x"}, "ok")
    assert [e["repo"] for e in entries_of(call)] == ["/tmp/other"]


def test_two_commit_segments_in_two_repositories_give_one_null_repo_entry(tmp_path):
    call = one_call(
        tmp_path, "Bash", {"command": "git -C /tmp/b commit -m x && git -C /tmp/a commit -m y"}, "ok"
    )
    assert [e["repo"] for e in entries_of(call)] == [None]
    assert {e["basis"] for e in entries_of(call)} == {"opaque_command"}


def test_two_commit_segments_in_one_repository_give_one_entry(tmp_path):
    call = one_call(
        tmp_path, "Bash", {"command": "git -C /tmp/b commit -m x && git -C /tmp/b commit -m y"}, "ok"
    )
    assert [e["repo"] for e in entries_of(call)] == ["/tmp/b"]


def test_a_commit_with_an_unknown_repository_gives_a_null_repo_entry(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "git --git-dir=/x/.git commit -m x"}, "ok")
    assert [e["repo"] for e in entries_of(call)] == [None]


def test_a_commit_in_a_known_and_an_unknown_repository_gives_one_null_repo_entry(tmp_path):
    call = one_call(
        tmp_path, "Bash", {"command": "git -C /tmp/b commit -m x && git --git-dir=/x commit -m y"},
        "ok",
    )
    assert [e["repo"] for e in entries_of(call)] == [None]


def test_a_poisoned_commit_falls_back_to_the_calls_own_candidate(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "git -C /tmp/b commit -q -m x"}, "ok")
    assert [e["repo"] for e in entries_of(call)] == [CWD]


def test_a_backgrounded_commit_call_is_on_the_background_row_at_its_candidate(tmp_path):
    call = one_call(tmp_path, "Bash", {"command": "git -C /tmp/b commit -m x &"}, "ok")
    assert entries_of(call) == [
        entry("possible", "background_process", "success", None, CWD, f"{NAME}:2")
    ]
