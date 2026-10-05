"""N4a coverage group 6 (the command part): the per-call truth table of
docs/DESIGN.md 4.13.1 (C-16), rows 2 to 6 for Bash and Monitor, plus the
delegation tools of row 3. One exact row per (tool, row) pair, and a named
witness for every precedence and boundary the rows depend on.

Rows 1, 7, and 8 (file tools, read-only tools, unknown tools) and the
completion mapping belong to N4b; classify_call returns None for them.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands

CWD = "/tmp/ws"

BG = (2, "possible", "background_process")
DELEG = (3, "possible", "delegation")
READ = (4, None, None)
TINY = (5, "claimed_target", "command_grammar")
OPAQUE = (6, "possible", "opaque_command")


def cls(tool, command, **kwargs):
    kwargs.setdefault("cwd", CWD)
    return commands.classify_call(tool, command, **kwargs)


def triple(tool, command, **kwargs):
    c = cls(tool, command, **kwargs)
    assert c is not None, (tool, command)
    return (c.row, c.effect, c.basis)


# (tool, command, run_in_background, backgrounded_hit, expected)
ROWS = [
    # --- row 2: background (Bash and Monitor) ------------------------------------
    ("Bash", "cat f", True, False, BG),
    ("Monitor", "cat f", True, False, BG),
    ("Bash", "make", True, False, BG),
    ("Monitor", "make", True, False, BG),
    ("Bash", "nohup make", False, False, BG),
    ("Monitor", "nohup make", False, False, BG),
    ("Bash", "cat f; nohup make", False, False, BG),
    ("Monitor", "cat f; nohup make", False, False, BG),
    ("Bash", "nohup cat f", False, False, BG),
    ("Bash", "echo nohup", False, False, BG),
    ("Bash", "make && nohup make out", False, False, BG),
    ("Bash", "cat f &", False, False, BG),
    ("Monitor", "cat f &", False, False, BG),
    ("Bash", "cat f &\n", False, False, BG),
    ("Bash", "make &", False, False, BG),
    ("Monitor", "make &", False, False, BG),
    ("Bash", "echo x > f &", False, False, BG),
    ("Bash", "touch f &", False, False, BG),
    ("Bash", "cat a & cat b &", False, False, BG),
    ("Bash", "codex exec x &", False, False, BG),
    ("Bash", "codex exec x", True, False, BG),
    ("Bash", "aider --yes", True, False, BG),
    ("Bash", "echo x > f", True, False, BG),
    ("Bash", "cat f", False, True, BG),
    ("Monitor", "cat f", False, True, BG),
    ("Bash", "make", False, True, BG),
    ("Bash", "echo 'a nohup b", False, False, BG),
    # the predicates are strict: `true` the boolean, a token equal to nohup, a trailing &
    ("Bash", "cat f", False, False, READ),
    ("Bash", "cat f", None, False, READ),
    ("Bash", "cat f", "true", False, READ),
    ("Bash", "cat f", 1, False, READ),
    ("Bash", "echo nohupx", False, False, READ),
    ("Bash", "bash -c 'nohup x'", False, False, OPAQUE),
    ("Bash", "echo 'make &'", False, False, READ),
    ("Bash", "echo a\\&", False, False, READ),
    ("Bash", "printf x & wait", False, False, OPAQUE),
    ("Monitor", "printf x & wait", False, False, OPAQUE),
    ("Bash", "make & make", False, False, OPAQUE),
    ("Bash", "cat a & cat b", False, False, READ),
    # --- row 3: delegation (Bash only) -------------------------------------------
    ("Bash", "codex exec x", False, False, DELEG),
    ("Bash", "aider --yes", False, False, DELEG),
    ("Bash", "claude -p hi", False, False, DELEG),
    ("Bash", "claude --print hi", False, False, DELEG),
    ("Bash", "claude hi -p", False, False, DELEG),
    ("Bash", "node /x/codex-companion.mjs task", False, False, DELEG),
    ("Bash", "./codex-companion.mjs", False, False, DELEG),
    ("Bash", "/usr/local/bin/codex exec x", False, False, DELEG),
    ("Bash", "cd /a && codex exec x", False, False, DELEG),
    ("Bash", "cat f | codex exec x", False, False, DELEG),
    ("Bash", "make ; aider", False, False, DELEG),
    ("Bash", 'codex exec "$(cat p)"', False, False, DELEG),
    ("Bash", "claude hi", False, False, OPAQUE),
    ("Bash", "claude", False, False, OPAQUE),
    ("Bash", "claude --version", False, False, OPAQUE),
    ("Bash", "codexx exec", False, False, OPAQUE),
    ("Bash", "codex2 x", False, False, OPAQUE),
    ("Bash", "echo codex", False, False, READ),
    ("Bash", "echo aider claude", False, False, READ),
    ("Monitor", "codex exec x", False, False, OPAQUE),
    ("Monitor", "aider --yes", False, False, OPAQUE),
    ("Monitor", "claude -p hi", False, False, OPAQUE),
    ("Monitor", "node /x/codex-companion.mjs task", False, False, OPAQUE),
    # --- row 4: read-only (Bash and Monitor) -------------------------------------
    ("Bash", "cat f", False, False, READ),
    ("Monitor", "cat f", False, False, READ),
    ("Bash", "ls | wc -l", False, False, READ),
    ("Monitor", "ls | wc -l", False, False, READ),
    ("Bash", "cd /abs && cat f", False, False, READ),
    ("Monitor", "tail -f /tmp/x.log", False, False, READ),
    ("Bash", "rg --no-config x", False, False, READ),
    ("Bash", "grep x f 2>&1", False, False, READ),
    # --- row 5: the tiny write grammar (Bash only) -------------------------------
    ("Bash", "echo x > out", False, False, TINY),
    ("Bash", "echo x >> out", False, False, TINY),
    ("Bash", "touch f", False, False, TINY),
    ("Bash", "touch a b", False, False, TINY),
    ("Bash", "sed -i.bak 's/x/y/' f", False, False, TINY),
    ("Bash", "sed -i '' 's/x/y/' f", False, False, TINY),
    ("Bash", "cat f | tee out", False, False, TINY),
    ("Bash", "cat > f <<EOF\nx\nEOF", False, False, TINY),
    ("Monitor", "echo x > out", False, False, OPAQUE),
    ("Monitor", "touch f", False, False, OPAQUE),
    ("Monitor", "sed -i.bak 's/x/y/' f", False, False, OPAQUE),
    ("Monitor", "cat f | tee out", False, False, OPAQUE),
    ("Monitor", "cat > f <<EOF\nx\nEOF", False, False, OPAQUE),
    # --- row 6: any other command (Bash and Monitor) -----------------------------
    ("Bash", "make", False, False, OPAQUE),
    ("Monitor", "make", False, False, OPAQUE),
    ("Bash", "git status", False, False, OPAQUE),
    ("Monitor", "git status", False, False, OPAQUE),
    ("Bash", "cp a b", False, False, OPAQUE),
    ("Bash", "mv a b", False, False, OPAQUE),
    ("Bash", "cp -R t out", False, False, OPAQUE),
    ("Bash", "rm f", False, False, OPAQUE),
    ("Bash", "echo $X", False, False, OPAQUE),
    ("Bash", "echo x > $F", False, False, OPAQUE),
    ("Bash", "cd /a && echo x > f", False, False, OPAQUE),
    ("Bash", "python x.py", False, False, OPAQUE),
    ("Bash", "git commit -m x", False, False, OPAQUE),
    ("Bash", "", False, False, OPAQUE),
]


@pytest.mark.parametrize("tool,command,rib,hit,expected", ROWS)
def test_rows(tool, command, rib, hit, expected):
    assert triple(tool, command, run_in_background=rib, backgrounded_hit=hit) == expected


def test_the_tiny_grammar_row_needs_a_resolvable_target():
    assert triple("Bash", "echo x > rel", cwd=None) == OPAQUE
    assert triple("Bash", "echo x > /tmp/abs", cwd=None) == TINY


def test_the_tiny_grammar_row_carries_its_targets():
    c = cls("Bash", "touch a b")
    assert [(t.path, t.repo, t.kind) for t in c.targets] == [
        ("/tmp/ws/a", CWD, "touch"), ("/tmp/ws/b", CWD, "touch"),
    ]
    assert cls("Bash", "cat f").targets == ()
    assert cls("Bash", "make").targets == ()


# --- the delegation tools of row 3 -----------------------------------------------


@pytest.mark.parametrize("tool", ["Agent", "Workflow", "SendMessage", "Task"])
def test_delegation_tools(tool):
    assert triple(tool, None) == DELEG
    assert triple(tool, "anything") == DELEG
    c = cls(tool, None)
    assert c.targets == () and c.repo is None and not c.commit.is_commit


def test_the_delegation_tool_names_are_exactly_these():
    assert set(commands.DELEGATION_TOOLS) == {"Agent", "Workflow", "SendMessage", "Task"}


@pytest.mark.parametrize(
    "tool", ["Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "Grep", "mcp__x__y", "bash",
             "BASH", "monitor", "task", "agent", "", None],
)
def test_other_tools_are_left_to_the_ledger(tool):
    assert cls(tool, "cat f") is None


# --- precedence witnesses (each predicate pair where two overlap) -----------------


def test_background_before_delegation():
    # `codex exec x` is delegation, and with run_in_background it is also background
    assert triple("Bash", "codex exec x") == DELEG
    assert triple("Bash", "codex exec x", run_in_background=True) == BG


def test_background_before_read_only():
    assert triple("Bash", "cat f") == READ
    assert triple("Bash", "cat f &") == BG


def test_background_before_the_tiny_write_grammar():
    # a trailing & leaves one segment, so the tiny grammar would otherwise match
    assert triple("Bash", "echo x > f") == TINY
    assert triple("Bash", "echo x > f &") == BG


def test_background_before_opaque():
    assert triple("Bash", "make") == OPAQUE
    assert triple("Bash", "make &") == BG


def test_delegation_before_opaque():
    assert triple("Bash", "aider --yes") == DELEG


def test_nohup_codex_is_background_not_delegation():
    # the first word of the segment is nohup, so it never satisfies delegation
    assert triple("Bash", "nohup codex exec x") == BG


def test_anchored_marker_hit_versus_payload_only_mention():
    assert triple("Bash", "cat f", backgrounded_hit=True) == BG
    assert triple("Bash", "cat f", backgrounded_hit=False) == READ


def test_run_in_background_is_the_boolean_true_only():
    for value in (False, None, 0, 1, "true", "True", [], {}):
        assert triple("Bash", "cat f", run_in_background=value) == READ, value
    assert triple("Bash", "cat f", run_in_background=True) == BG


# --- long commands are classified from their full text ------------------------------


def test_a_command_over_2048_characters_is_classified_from_the_full_text():
    pad = "a" * 3000
    assert triple("Bash", f"echo {pad}") == READ
    long_write = cls("Bash", f"echo {pad} > out")
    assert (long_write.row, long_write.effect) == (5, "claimed_target")
    assert [t.path for t in long_write.targets] == ["/tmp/ws/out"]
    assert triple("Bash", f"echo {pad} &") == BG
    assert triple("Bash", f"echo {pad} && rm f") == OPAQUE
    assert triple("Bash", f"echo {pad} ; nohup make") == BG
    assert cls("Bash", f"echo {pad} && git commit -m x").commit.is_commit


# --- shape of the classification ---------------------------------------------------


def test_a_non_string_command():
    assert triple("Bash", None) == OPAQUE
    assert triple("Monitor", 5) == OPAQUE
    assert triple("Bash", None, run_in_background=True) == BG
    assert triple("Bash", None, backgrounded_hit=True) == BG


def test_commit_facts_ride_on_the_classification():
    c = cls("Bash", "git -C /r commit -m x")
    assert (c.row, c.basis) == (6, "opaque_command")
    assert [s.repo for s in c.commit.segments] == ["/r"]
    assert cls("Bash", "git commit -m x &").commit.segments == ()
    assert cls("Monitor", "git commit -m x").commit.segments == ()
    assert cls("Bash", "cd /abs && git commit -m x").commit.segments[0].repo == "/abs"
    assert cls("Bash", "git status").commit.segments == ()


def test_the_candidate_repo_of_a_call():
    assert cls("Bash", "cat f").repo == CWD
    assert cls("Bash", "cd /abs && make").repo == "/abs"
    assert cls("Bash", "cd sub && make").repo is None
    assert cls("Bash", "cat f", cwd=None).repo is None
    assert cls("Bash", "cat f", cwd="rel").repo is None
    # an opaque command still carries the call cwd as its candidate
    assert cls("Bash", "make $X").repo == CWD
    assert cls("Bash", "make $X", cwd=None).repo is None
    assert cls("Bash", "make &").repo == CWD
