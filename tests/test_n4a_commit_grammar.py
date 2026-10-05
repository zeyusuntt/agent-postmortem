"""N4a coverage group 4: the commit grammar (docs/DESIGN.md 4.13.1, C-9) and
the reader's commit extraction that now uses it (C-11, C-14).

An id is an unverified, summary-shaped output line (DESIGN wording, pinned
below); these rows pin which calls may carry one and which line is taken.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_postmortem import commands, reader

CWD = "/tmp/ws"
DESIGN = Path(__file__).resolve().parents[1] / "docs" / "DESIGN.md"


def repos(command, cwd=CWD):
    return [s.repo for s in commands.commit_facts(command, cwd).segments]


def ids(command, result, cwd=CWD):
    return commands.commit_ids(commands.commit_facts(command, cwd), result)


MSG = '-m "$(cat <<\'EOF\'\nsubject\n\nbody\nEOF\n)"'

POSITIVE = [
    # plain forms
    ("git commit -m x", [CWD]),
    ('git commit -m "x y"', [CWD]),
    ("git commit -am x", [CWD]),
    ("git commit --amend --no-edit", [CWD]),
    ("git commit -F msgfile", [CWD]),
    ("git commit -F - <<'EOF'\nmsg\nEOF", [CWD]),
    ("git commit -m x 2>&1", [CWD]),
    ("git commit -m x -- f", [CWD]),
    ("git commit -m x -- -q", [CWD]),
    ("git commit --date=2026-01-01 -m x", [CWD]),
    ("git commit --signoff --no-verify --allow-empty -m x", [CWD]),
    ("git commit --author='A <a@x>' -m x", [CWD]),
    ('git commit -m "fix -q handling"', [CWD]),
    ("git commit --reuse-message=HEAD", [CWD]),
    ("git commit --squash=abc -m x", [CWD]),
    (f"git commit {MSG}", [CWD]),
    (f"cd /abs && git add -A && git commit {MSG}", ["/abs"]),
    # cd and -C
    ("cd /abs && git add . && git commit -m x", ["/abs"]),
    ("cd /abs/repo && git commit -m x", ["/abs/repo"]),
    ("cd sub && git commit -m x", [None]),
    ("git -C /abs commit -m x", ["/abs"]),
    ("git -C /abs -c user.name=x -c user.email=y commit -m x", ["/abs"]),
    ("git -c k=v -C /a -C b commit -m x", ["/a/b"]),
    ("git -C sub commit -m x", [f"{CWD}/sub"]),
    ("git --git-dir=/x/.git commit -m x", [None]),
    ("git --work-tree=/x commit -m x", [None]),
    ("git -C /a --git-dir=/x/.git commit -m x", [None]),
    ("cd /abs && git -C sub commit -m x", ["/abs/sub"]),
    # one wrapper level
    ("bash -c 'git commit -m x'", [CWD]),
    ('bash -lc "git add . && git commit -m x"', [CWD]),
    ("sh -c 'git commit -m x'", [CWD]),
    ("cd /abs && bash -c 'git commit -m x'", ["/abs"]),
    ("bash -c 'cd /in && git commit -m x'", ["/in"]),
    ("bash -c 'git commit -m x' && echo ok", [CWD]),
    ("git add . && bash -c 'git commit -m x'", [CWD]),
    # more than one commit segment, other segments around
    ("git commit -m a && git commit -m b", [CWD, CWD]),
    ("git -C /a commit -m x && git -C /b commit -m y", ["/a", "/b"]),
    ("git add . && git commit -m x && git push", [CWD]),
    ("git commit -m x && echo done", [CWD]),
    ("git status && git commit -m x", [CWD]),
    ("git add .\\\n && git commit -m x", [CWD]),
    ("git add . &&\ngit commit -m x", [CWD]),
    ("bash -c 'git commit -m a' && git commit -m b", [CWD, CWD]),
    ("git commit -m x # trailing comment", [CWD]),
    # a word assembled from quoted pieces is still the word `commit`
    ("git 'com''mit' -m x", [CWD]),
    ("git com\\mit -m x", [CWD]),
    ("git commi''t -m x", [CWD]),
    ('git "comm"it -m x', [CWD]),
    ("git com\\\nmit -m x", [CWD]),
]


@pytest.mark.parametrize("command,expected", POSITIVE)
def test_commit_segments_and_their_candidate_repos(command, expected):
    assert repos(command) == expected


NEGATIVE = [
    # A1 (rev 11): the separator rows that were here (`;`, `||`, `|`, a newline,
    # a trailing `;`, `false && ... ; printf`, `git add . ; git commit`,
    # `git add . || git commit`, `true | git commit`, `... && echo a ; echo b`)
    # moved by hand to A1_FLIPPED_POSITIVE below with their new expectation.
    # Still negative under A1: a backgrounded commit, and a poisoned call.
    "git commit -m x &",
    "git commit --dry-run ; printf '[main deadbee] fake\\n'",
    "git commit -m x && git push &",
    "git commit -m x & git status",
    "git commit -m x & git status && git log",
    # a dry-run or quiet commit anywhere in the chain makes the selected line unattributable
    "git commit -m x && git commit -q -m y",
    "git commit -q -m y && git commit -m x",
    "git commit --dry-run && git commit -m x",
    "git commit -m x && bash -c 'git commit --quiet -m y'",
    # dry-run and quiet forms, each rejected
    "git commit --dry-run",
    "git commit -m x --dry-run",
    "git commit -q -m x",
    "git commit --quiet -m x",
    "git commit --porcelain",
    "git commit --short",
    "git commit --long",
    "git commit -z",
    "git commit -qm x",
    "git commit -aq -m x",
    "git commit -zm x",
    "git commit --dry -m x",
    "git commit --dry-r -m x",
    "git commit --quie -m x",
    "git commit --qu -m x",
    "git commit --por",
    "git commit --sho",
    "git commit --lon",
    "git commit --d -m x",
    "git commit --dry-run=x",
    "git commit -q -m x && printf '[main deadbee] forged\\n'",
    "git add . && git commit --quiet -m x",
    # not a git commit segment
    "echo git commit",
    "# git commit -m x",
    "echo 'git commit -m x'",
    "printf '%s' \"git commit\"",
    "cat <<EOF\ngit commit -m x\nEOF",
    "grep 'git commit' f",
    "git log --grep=commit",
    "git add . && echo commit",
    "git status",
    "git commit-tree x",
    "git cherry-pick x",
    "git notacommit",
    "xargs git commit -m x",
    "env git commit -m x",
    "sudo git commit -m x",
    "time git commit -m x",
    "command git commit -m x",
    "nohup git commit -m x",
    "bash script.sh git commit",
    "git",
    "git commit2",
    "GIT_X=1 git commit -m x",
    # other or malformed global options
    "git --no-pager commit -m x",
    "git -p commit -m x",
    "git --git-dir /x commit -m x",
    "git -C commit",
    "git -c",
    "git -C/abs commit -m x",
    # opaque text
    'git commit -m "$MSG"',
    "git commit -m $(cat f)",
    "git commit -m *",
    'git commit -m "$(cat <<EOF\nm\nEOF\n)"',
    'git commit -m "$(cat f)"',
    'git commit -m "$(echo hi)"',
    "git commit -m x `echo y`",
    "git commit -m 'unbalanced",
    "git commit -m x && eval ls",
    # wrappers outside the one recognized level
    "bash -c \"bash -c 'git commit -m x'\"",
    "sh -c 'bash -lc \"git commit -m x\"'",
    "bash -c 'git commit -m x' extra",
    "bash -x -c 'git commit -m x'",
    "bash -ic 'git commit -m x'",
    "zsh -c 'git commit -m x'",
    "/bin/bash -c 'git commit -m x'",
    "bash -c",
    "bash -c 'git commit -m x' > out",
    # A1: `bash -c 'git commit -m x ; echo y'` and `bash -c 'git commit -m x ||
    # true'` moved to A1_FLIPPED_POSITIVE (a separator inside the one wrapper level).
    "bash -c 'git commit -m x &'",
    # a wrapper whose string is not a placed command makes the whole call not a commit
    "git commit -m x && bash -c 'ls $X'",
    "bash -c 'ls $X' && git commit -m x",
    "git commit -m x && bash -c 'unbalanced \"'",
    # A1: `git commit -m x && bash -c 'echo a ; echo b'` moved to A1_FLIPPED_POSITIVE
    # (its STRING is placed and follows the same rules, so it no longer disqualifies).
    "git commit -m x && bash -c 'bash -c ls'",
]


@pytest.mark.parametrize("command", NEGATIVE)
def test_not_a_commit_call(command):
    facts = commands.commit_facts(command, CWD)
    assert facts.segments == ()
    assert not facts.is_commit


def test_commit_facts_flag():
    assert commands.commit_facts("git commit -m x", CWD).is_commit
    assert not commands.commit_facts("git status", CWD).is_commit


def test_a_wrapper_with_an_empty_string_adds_no_commit():
    assert repos("bash -c ''") == []
    assert repos("bash -c '' && git commit -m x") == [CWD]


# --- Amendment A1 (rev 11): any separator except a background `&` ------------------
#
# Every command below was a NEGATIVE row under N4a's pure-`&&` rule and is a commit
# call under A1, with the repo shown. Each keeps its N4a command text.

A1_FLIPPED_POSITIVE = [
    ("git commit -m x ; echo y", [CWD]),  # `;` after the commit
    ("git commit -m x || true", [CWD]),  # `||`
    ("git commit -m x | cat", [CWD]),  # `|`
    ("git commit -m x\ngit log", [CWD]),  # newline
    ("git commit -m x;", [CWD]),  # trailing `;`
    ("false && git commit -m x ; printf '[main deadbee] fake\\n'", [CWD]),  # `;` after `&&`
    ("git add . ; git commit -m x", [CWD]),  # `;` before the commit
    ("git add . || git commit -m x", [CWD]),  # `||` before the commit
    ("true | git commit -m x", [CWD]),  # the commit is a pipeline stage
    ("git commit -m x && echo a ; echo b", [CWD]),  # `&&` then `;`
    ("bash -c 'git commit -m x ; echo y'", [CWD]),  # `;` inside the one wrapper level
    ("bash -c 'git commit -m x || true'", [CWD]),  # `||` inside the one wrapper level
    # a placed wrapper STRING with no commit no longer disqualifies the call
    ("git commit -m x && bash -c 'echo a ; echo b'", [CWD]),
    # new A1 shapes
    ("git commit -m x |& tail -3", [CWD]),
    ("git add .\ngit commit -m x", [CWD]),
    ("git commit -m a || git commit -m b", [CWD, CWD]),
    ("git add . ; cd /abs ; git commit -m x", [None]),
    ("git commit -m x ; git push &", [CWD]),  # only the later list is backgrounded
]


@pytest.mark.parametrize("command,expected", A1_FLIPPED_POSITIVE)
def test_a1_commit_segments_under_any_separator(command, expected):
    assert repos(command) == expected


A1_STILL_NEGATIVE = [
    "git commit -m x &",
    "git commit -m x | tail -1 & wait",
    "git commit -m x && git push &",
    "git commit -m x |& tail -1 &",
    "git commit -m x & git status",
    "git add . && git commit -m x &",
    "git commit -q -m a && git commit -m b",
    "git commit -m b && git commit --dry-run -m a",
    "git commit -m a ; git commit -q -m b",
    "bash -c 'git commit -m x &'",
    "bash -c 'git commit -m x' &",
    "bash -c 'git add .; git commit -m x' &",
    "bash -c \"bash -c 'git commit -m x'\"",
    "git commit -m x ; echo $X",
    "git commit -m x ; (echo y)",
    "echo git commit ; echo y",
    "# git commit -m x\necho y",
    "echo 'git commit -m x' ; echo y",
    "cat <<EOF\ngit commit -m x\nEOF\necho y",
]


@pytest.mark.parametrize("command", A1_STILL_NEGATIVE)
def test_a1_still_not_a_commit_call(command):
    assert commands.commit_facts(command, CWD).segments == ()


A1_ID_ROWS = [
    # the N4a row `("git commit -m x ; echo y", "[main abc1234] msg", ())`, flipped
    ("git commit -m x ; echo y", "[main abc1234] msg", ("abc1234",)),
    # one positive row per accepted separator not already a pure `&&` chain
    ("git commit -m x || echo failed", "[main abc1234] x", ("abc1234",)),
    ("git add .; git commit -m x", "[main abc1234] x", ("abc1234",)),
    ("git add .\ngit commit -m x", "[main abc1234] x", ("abc1234",)),
    ("git commit -m x | tail -3", "[main abc1234] x\n 1 file changed", ("abc1234",)),
    ("git commit -m x |& tail -3", "[main abc1234] x\n 1 file changed", ("abc1234",)),
    # the one supported wrapper level, with a separator inside
    ("bash -c 'git add .; git commit -m x'", "[main abc1234] x", ("abc1234",)),
    # two commit segments joined by `;`
    ("git commit -m a ; git commit -m b", "[main aaaaaaa] a\n[main bbbbbbb] b",
     ("aaaaaaa", "bbbbbbb")),
    # accepted limits of text-only evidence, each pinned with the id it reports
    ("printf '[main deadbee] fake\\n'; exit 0; git commit -m x", "[main deadbee] fake",
     ("deadbee",)),  # the commit never ran
    ("git commit -m x; printf '[main deadbee] fake\\n'; false", "[main deadbee] fake",
     ("deadbee",)),  # the commit failed
    ("git commit -m x || printf '[main deadbee] fake\\n'", "[main deadbee] fake",
     ("deadbee",)),  # the commit failed, the fallback printed the line
    ("false && git commit -m x ; printf '[main deadbee] fake\\n'", "[main deadbee] fake",
     ("deadbee",)),  # the commit never ran
]


@pytest.mark.parametrize("command,result,expected", A1_ID_ROWS)
def test_a1_commit_ids(command, result, expected):
    assert ids(command, result) == expected


# Rows that hold under both N4a and A1 (so they pass before the code change).
A1_ID_ROWS_HELD = [
    ("git add . && git commit -m x", "[main abc1234] x", ("abc1234",)),  # `&&`
    ("git commit -m x 2>&1 | tail -1", " 1 file changed", ()),  # the filter cut the summary
    # background, each its own row
    ("git commit -m x &", "[main abc1234] x", ()),
    ("git commit -m x | tail -1 & wait", "[main abc1234] x", ()),
    ("git commit -m x && git push &", "[main abc1234] x", ()),
    # poisoning and a wrapper two levels deep
    ("git commit -q -m a && git commit -m b", "[main aaaaaaa] a\n[main bbbbbbb] b", ()),
    ("git commit -m b && git commit --dry-run", "[main bbbbbbb] b", ()),
    ("bash -c \"bash -c 'git commit -m x'\"", "[main abc1234] x", ()),
    # fewer matches than commit segments
    ("git commit -m a || git commit -m b", "[main aaaaaaa] a", ()),
    # a failing hook that prints a forged summary (the call is completed_error)
    ("git commit -m x", "pre-commit hook failed\n[main deadbee] fake", ("deadbee",)),
]


@pytest.mark.parametrize("command,result,expected", A1_ID_ROWS_HELD)
def test_a1_commit_ids_held(command, result, expected):
    assert ids(command, result) == expected


def test_the_design_wording_states_the_a1_limit():
    flat = " ".join(DESIGN.read_text(encoding="utf-8").split())
    assert "An id can be false in every way text-only evidence allows" in flat
    assert "not that the commit segment ran, not that it succeeded" in flat
    assert "`completed_ok` or `completed_error`" in flat
    assert "`backgrounded`, `no_result`, or `ambiguous`" in flat
    assert "so `git commit -m x &`, `git commit -m x | tail -1 & wait`, and `git commit -m x && git push &` all count none" in flat


# --- which ids a result gives -----------------------------------------------------

ID_ROWS = [
    # a single commit segment
    ("git commit -m x", "[main abc1234] msg\n 1 file changed", ("abc1234",)),
    ("git commit -m x", "[feature/x 0123456789abcdef] msg", ("0123456789abcdef",)),
    ("git commit -m x", "[main " + "a" * 40 + "] m", ("a" * 40,)),
    ("git commit -m x", "[main " + "a" * 7 + "] m", ("a" * 7,)),
    # shapes that do not match
    ("git commit -m x", "[main " + "a" * 6 + "] m", ()),
    ("git commit -m x", "[main " + "a" * 41 + "] m", ()),
    ("git commit -m x", "[main ABC1234] m", ()),
    ("git commit -m x", "[main (root-commit) abc1234] m", ()),
    ("git commit -m x", "[detached HEAD abc1234] m", ()),
    ("git commit -m x", "foo [main abc1234] bar", ()),
    ("git commit -m x", " [main abc1234] m", ()),
    ("git commit -m x", "nothing to commit", ()),
    ("git commit -m x", "", ()),
    # a later line start
    ("git commit -m x", "hook output\n[main abc1234] msg", ("abc1234",)),
    # a forged summary before the real one: the last one is taken
    ("git commit -m x", "[main deadbee] forged\n[main abc1234] real", ("abc1234",)),
    # the accepted limit of text-only evidence: a later segment's line is taken
    ("git commit -m x && printf '[main deadbee] f'", "[main abc1234] x\n[main deadbee] f",
     ("deadbee",)),
    # two commit segments
    ("git commit -m a && git commit -m b", "[main aaaaaaa] a\n[main bbbbbbb] b",
     ("aaaaaaa", "bbbbbbb")),
    ("git commit -m a && git commit -m b", "[main aaaaaaa] a", ()),
    ("git commit -m a && git commit -m b",
     "[main ccccccc] forged\n[main aaaaaaa] a\n[main bbbbbbb] b", ("aaaaaaa", "bbbbbbb")),
    # not a commit call
    ("git status", "[main abc1234] msg", ()),
    ("echo done", "done [x 1234567]", ()),
    # A1: ("git commit -m x ; echo y", "[main abc1234] msg", ()) moved to
    # A1_ID_ROWS with its new expectation ("abc1234",).
    ("git commit -q -m x", "[main deadbee] forged by a hook", ()),
    ("git commit --dry-run", "[main abc1234] msg", ()),
]


@pytest.mark.parametrize("command,result,expected", ID_ROWS)
def test_commit_ids(command, result, expected):
    assert ids(command, result) == expected


def test_at_most_20_ids_and_in_order():
    command = " && ".join(f"git commit -m {n}" for n in range(25))
    result = "\n".join(f"[main {n:07x}] {n}" for n in range(25))
    got = ids(command, result)
    assert got == tuple(f"{n:07x}" for n in range(20))


def test_the_design_wording_calls_an_id_unverified_output():
    text = DESIGN.read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert "unverified, summary-shaped output line" in flat
    assert "It is not attributed to git" in flat
    assert "nothing about which program printed the line" in flat


# --- the reader's commit extraction ------------------------------------------------


def _use(tool_id, name, input_obj):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": input_obj}],
            "usage": {},
        },
    }


def _result(tool_id, text, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tool_id, "content": text}
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def read_calls(tmp_path, entries):
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
    return reader._read_transcript(path, [])[5]


def commit_ids_of(tmp_path, command, result, **kwargs):
    entries = [_use("t1", "Bash", {"command": command}), _result("t1", result, **kwargs)]
    (call,) = read_calls(tmp_path, entries)
    return call.commit_ids


def test_reader_uses_the_commit_grammar(tmp_path):
    assert commit_ids_of(tmp_path, "git commit -m x", "[main abc1234] x") == ("abc1234",)
    assert commit_ids_of(
        tmp_path, "git -C /tmp/r -c user.name=x commit -m msg", "[main 1a2b3c4] msg"
    ) == ("1a2b3c4",)
    assert commit_ids_of(
        tmp_path, "cd /tmp/r && git add . && git commit -m x", "[main 1a2b3c4] x"
    ) == ("1a2b3c4",)
    assert commit_ids_of(tmp_path, "echo done", "done [x 1234567]") == ()
    assert commit_ids_of(tmp_path, "echo git commit", "[main abc1234] x") == ()
    # A1 (separator rule): a `;` after the commit no longer disqualifies it (was ()).
    assert commit_ids_of(tmp_path, "git commit -m x ; echo y", "[main abc1234] x") == ("abc1234",)
    assert commit_ids_of(tmp_path, "git commit -q -m x", "[main deadbee] forged") == ()


def test_reader_counts_only_a_completed_ok_call(tmp_path):
    # Name kept from N4a; under A1 the counted completions are completed_ok and
    # completed_error, and the three excluded ones are asserted below.
    result = "[main abc1234] x"
    # A1 (completion rule): an is_error result and an "Exit code 1" result now
    # count (both were () under N4a).
    assert commit_ids_of(tmp_path, "git commit -m x", result, is_error=True) == ("abc1234",)
    assert commit_ids_of(tmp_path, "git commit -m x", result + "\nExit code 1") == ("abc1234",)
    assert commit_ids_of(tmp_path, "git commit -m x", result + "\nExit code 0") == ("abc1234",)
    backgrounded = (
        result + "\nCommand did not complete within its 120s timeout and was moved to the background"
    )
    (call,) = read_calls(tmp_path, [
        _use("t1", "Bash", {"command": "git commit -m x"}),
        _result("t1", backgrounded),
    ])
    assert call.commit_ids == ()
    (call,) = read_calls(tmp_path, [_use("t1", "Bash", {"command": "git commit -m x"})])
    assert call.commit_ids == ()


def test_reader_gives_an_ambiguous_call_no_ids(tmp_path):
    first_result_first = [
        _use("t1", "Bash", {"command": "git commit -m x"}),
        _result("t1", "[main abc1234] x"),
        _use("t1", "Bash", {"command": "git commit -m y"}),
        _result("t1", "[main bbbbbbb] y"),
    ]
    calls = read_calls(tmp_path, first_result_first)
    assert [c.ambiguous for c in calls] == [True, True]
    assert [c.commit_ids for c in calls] == [(), ()]
    duplicate_first = [
        _use("t1", "Bash", {"command": "git commit -m x"}),
        _use("t1", "Bash", {"command": "git commit -m y"}),
        _result("t1", "[main abc1234] x"),
    ]
    calls = read_calls(tmp_path, duplicate_first)
    assert [c.commit_ids for c in calls] == [(), ()]


def test_reader_classifies_the_full_command_not_the_stored_copy(tmp_path):
    long_command = "echo " + "a" * 3000 + " && git commit -m x"
    (call,) = read_calls(tmp_path, [
        _use("t1", "Bash", {"command": long_command}),
        _result("t1", "[main abc1234] x"),
    ])
    assert call.command is None and call.command_sha256 is not None
    assert call.commit_ids == ("abc1234",)
    long_no_commit = "echo " + "a" * 3000 + " && echo git commit"
    (call,) = read_calls(tmp_path, [
        _use("t1", "Bash", {"command": long_no_commit}),
        _result("t1", "[main abc1234] x"),
    ])
    assert call.commit_ids == ()


def test_reader_reads_the_full_result_text_not_the_excerpt(tmp_path):
    padding = "x" * 5000
    result = f"{padding}\n[main abc1234] x"
    assert commit_ids_of(tmp_path, "git commit -m x", result) == ("abc1234",)
    blocks = [
        _use("t1", "Bash", {"command": "git commit -m x"}),
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{
                    "type": "tool_result", "tool_use_id": "t1",
                    "content": [{"type": "text", "text": "hook line"},
                                {"type": "text", "text": "[main abc1234] x"}],
                }],
            },
        },
    ]
    (call,) = read_calls(tmp_path, blocks)
    assert call.commit_ids == ("abc1234",)


def test_the_reader_only_considers_bash_calls(tmp_path):
    (call,) = read_calls(tmp_path, [
        _use("t1", "Monitor", {"command": "git commit -m x"}),
        _result("t1", "[main abc1234] x"),
    ])
    assert call.commit_ids == ()


# --- Amendment A1: the reader's completion and background rows ---------------------

HOOK_FAILED = "pre-commit hook failed\n[main deadbee] fake"


def test_a1_reader_counts_an_is_error_result(tmp_path):
    assert commit_ids_of(
        tmp_path, "git commit -m x; git push", "[main abc1234] x", is_error=True
    ) == ("abc1234",)


def test_a1_reader_counts_a_nonzero_exit_code_result(tmp_path):
    assert commit_ids_of(
        tmp_path, "git commit -m x; git push", "[main abc1234] x\nExit code 1"
    ) == ("abc1234",)


def test_a1_reader_pins_a_failing_hooks_forged_line(tmp_path):
    # the accepted limit: a hook failed (completed_error) and printed a summary-shaped line
    assert commit_ids_of(tmp_path, "git commit -m x", HOOK_FAILED, is_error=True) == ("deadbee",)


def test_a1_reader_pins_the_accepted_limits_on_completed_ok(tmp_path):
    forged = "[main deadbee] fake"
    for command in (
        "printf '[main deadbee] fake\\n'; exit 0; git commit -m x",
        "git commit -m x || printf '[main deadbee] fake\\n'",
    ):
        assert commit_ids_of(tmp_path, command, forged) == ("deadbee",), command


def test_a1_reader_a_directly_backgrounded_commit_counts_none(tmp_path):
    assert commit_ids_of(tmp_path, "git commit -m x &", "[main abc1234] x") == ()


def test_a1_reader_a_commit_in_a_backgrounded_pipeline_counts_none(tmp_path):
    # the call itself is completed_ok
    assert commit_ids_of(tmp_path, "git commit -m x | tail -1 & wait", "[main abc1234] x") == ()
    (call,) = read_calls(tmp_path, [
        _use("t1", "Bash", {"command": "git commit -m x | tail -1 & wait"}),
        _result("t1", "[main abc1234] x"),
    ])
    assert call.completion == "completed_ok"


def test_a1_reader_a_backgrounded_completion_counts_none(tmp_path):
    text = (
        "[main abc1234] x\n"
        "Command did not complete within its 120s timeout and was moved to the background"
    )
    (call,) = read_calls(tmp_path, [
        _use("t1", "Bash", {"command": "git commit -m x"}),
        _result("t1", text),
    ])
    assert call.completion == "backgrounded"
    assert call.commit_ids == ()


def test_a1_reader_an_ambiguous_completion_counts_none(tmp_path):
    # a duplicated tool_use id, with a summary-shaped result attached
    calls = read_calls(tmp_path, [
        _use("t1", "Bash", {"command": "git commit -m x"}),
        _use("t1", "Bash", {"command": "git commit -m y"}),
        _result("t1", "[main abc1234] x"),
    ])
    assert [c.completion for c in calls] == ["ambiguous", "ambiguous"]
    assert [c.commit_ids for c in calls] == [(), ()]


def test_a1_reader_a_no_result_completion_counts_none(tmp_path):
    # there is no result text to match, so no id can come from this completion
    (call,) = read_calls(tmp_path, [_use("t1", "Bash", {"command": "git commit -m x"})])
    assert call.completion == "no_result"
    assert call.commit_ids == ()


def test_a1_reader_a_poisoned_call_counts_none_even_when_it_errors(tmp_path):
    assert commit_ids_of(
        tmp_path, "git commit -q -m a && git commit -m b", "[main aaaaaaa] a\n[main bbbbbbb] b",
        is_error=True,
    ) == ()


def test_a1_reader_a_wrapper_with_a_separator_counts(tmp_path):
    # A1 accepts a separator inside the one supported wrapper level
    assert commit_ids_of(
        tmp_path, "bash -c 'git add .; git commit -m x'", "[main abc1234] x"
    ) == ("abc1234",)
