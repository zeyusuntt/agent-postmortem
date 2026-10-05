"""N4a coverage group 3: the tiny write grammar (docs/DESIGN.md 4.13.1, C-12).
Every form gets exact targets (path, repo, kind); every near-miss must not
match, so the command falls to the opaque row.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands

CWD = "/tmp/ws/repo"


def match(command: str, cwd: str | None = CWD):
    return commands.match_write_grammar(commands.lex(command), cwd)


def targets(command: str, cwd: str | None = CWD):
    result = match(command, cwd)
    assert result is not None, command
    return [(t.path, t.repo, t.kind) for t in result.targets]


R = "/tmp/ws/repo"

EXACT_ROWS = [
    # redirect
    ("echo hi > out.txt", [(f"{R}/out.txt", R, "redirect")]),
    ("echo hi >> out.txt", [(f"{R}/out.txt", R, "redirect")]),
    ("echo hi >/tmp/x/log", [("/tmp/x/log", R, "redirect")]),
    ("printf '%s' x > ../up/f", [("/tmp/ws/up/f", R, "redirect")]),
    ("cat a b > c", [(f"{R}/c", R, "redirect")]),
    ("ls -la > listing.txt", [(f"{R}/listing.txt", R, "redirect")]),
    ("grep x f > hits", [(f"{R}/hits", R, "redirect")]),
    ("sed -n 1p f > out", [(f"{R}/out", R, "redirect")]),
    ("cat > f", [(f"{R}/f", R, "redirect")]),
    ("echo x 1> f", [(f"{R}/f", R, "redirect")]),
    ("echo x > ./a/../b", [(f"{R}/b", R, "redirect")]),
    ("echo x > 'a b'", [(f"{R}/a b", R, "redirect")]),
    # tee
    ("cat f | tee out", [(f"{R}/out", R, "tee")]),
    ("cat f | tee -a out", [(f"{R}/out", R, "tee")]),
    ("ls | grep x | tee out", [(f"{R}/out", R, "tee")]),
    ("cat f 2>&1 | tee /tmp/z", [("/tmp/z", R, "tee")]),
    # sed -i
    ("sed -i 's/x/y/' f", [(f"{R}/f", R, "sed_i")]),
    ("sed -i '' 's/x/y/' f", [(f"{R}/f", R, "sed_i")]),
    ("sed -i.bak 's/x/y/' f", [(f"{R}/f", R, "sed_i"), (f"{R}/f.bak", R, "sed_backup")]),
    ("sed -i .bak 's/x/y/' f", [(f"{R}/f", R, "sed_i"), (f"{R}/f.bak", R, "sed_backup")]),
    ("sed -i~ 's/x/y/' f", [(f"{R}/f", R, "sed_i"), (f"{R}/f~", R, "sed_backup")]),
    ("sed -ibak 's/x/y/' f", [(f"{R}/f", R, "sed_i"), (f"{R}/fbak", R, "sed_backup")]),
    ("sed -i.orig 'p' d/f", [(f"{R}/d/f", R, "sed_i"), (f"{R}/d/f.orig", R, "sed_backup")]),
    ("sed -i.bak 's/x/y/' /tmp/abs", [("/tmp/abs", R, "sed_i"), ("/tmp/abs.bak", R, "sed_backup")]),
    # touch
    ("touch f", [(f"{R}/f", R, "touch")]),
    ("touch a b", [(f"{R}/a", R, "touch"), (f"{R}/b", R, "touch")]),
    ("touch a a", [(f"{R}/a", R, "touch"), (f"{R}/a", R, "touch")]),
    ("touch -c f", [(f"{R}/f", R, "touch")]),
    ("touch -a -m f", [(f"{R}/f", R, "touch")]),
    ("touch -ca f", [(f"{R}/f", R, "touch")]),
    ("touch -t 202601010000 f", [(f"{R}/f", R, "touch")]),
    ("touch -d 2026-01-01 f g", [(f"{R}/f", R, "touch"), (f"{R}/g", R, "touch")]),
    ("touch -r ref f", [(f"{R}/f", R, "touch")]),
    ("touch -- -x", [(f"{R}/-x", R, "touch")]),
    # cat heredoc
    ("cat > f <<EOF\nx\nEOF", [(f"{R}/f", R, "heredoc")]),
    ("cat > f <<'EOF'\n$x\nEOF", [(f"{R}/f", R, "heredoc")]),
    ('cat > f <<"EOF"\nx\nEOF\n', [(f"{R}/f", R, "heredoc")]),
    ("cat > f <<-EOF\n\tx\n\tEOF", [(f"{R}/f", R, "heredoc")]),
]


@pytest.mark.parametrize("command,expected", EXACT_ROWS)
def test_exact_targets(command, expected):
    assert targets(command) == expected


def test_form_names():
    assert match("echo hi > f").form == "redirect"
    assert match("cat f | tee out").form == "tee"
    assert match("sed -i 's/x/y/' f").form == "sed_i"
    assert match("touch f").form == "touch"
    assert match("cat > f <<EOF\nx\nEOF").form == "heredoc"


NO_MATCH = [
    # glued redirect, producers that are not read-only, extra structure
    "echo hi>f", "rm v > log", "cp a b > log", "git log > out", "sort f > out",
    "echo x 2>&1 > f", "echo x > a > b", "> f", "echo x >| f", "echo x > f 2>&1",
    "echo x > f && ls", "echo x > f;", "echo x > f &", "cat f | wc > out", "ls > f\nls > g",
    "echo > f x", "echo $X > f", "echo x > \"$F\"", "echo x > *.txt", "echo x > ~/f",
    "cd /a && echo x > f", "cd /a ; echo x > f", "echo x >& f", "echo x > f <<EOF\nx\nEOF",
    "echo x > ''", "echo x >",
    # tee
    "rm v | tee log", "git log | tee out", "cat f | tee", "cat f | tee a b", "cat f | tee -a",
    "cat f | tee -i out", "cat f |& tee out", "cat f | tee out | wc", "cat f | tee out > x",
    "cat f | tee -- out", "cat f | tee -x out", "cat f | tee -a -a out", "tee out",
    "cat f || tee out", "cat f && tee out", "cat f | tee out &",
    # sed -i
    "sed -i 's/x/y/' f g", "sed -i -e 'w x' f", "sed -i 'w x' f", "sed -i 's/x/y/'",
    "sed --in-place 's/x/y/' f", "sed -n -i p f", "sed -i -n p f", "sed -i.bak -e 's/x/y/' f",
    "sed -i 's/x/y/' -- f", "sed -i '-x' f", "sed -i 's/x/y/w out' f", "sed -i 's/x/y/e' f",
    "sed -i'*.bak' 's/x/y/' f", "sed -i 'bak/*' 's/x/y/' f", "sed -i.b/ak 's/x/y/' f",
    "sed -i'bak*' 's/x/y/' f", "sed -i bak 's/x/y/' f", "sed -i -e p f", "sed -i", "sed -i f",
    "sed -i.bak 's/x/y/' f g", "sed -i '' 's/x/y/' f g", "sed -i '' 'w x' f", "sed -f s -i f",
    "sed -i.bak -n p f", "sed -iE 's/x/y/' f g", "sed -i.bak 's/x/y/' -f",
    # touch
    "touch", "touch -c", "touch -d", "touch -d x -t y f", "touch -x f", "touch -h f",
    "touch f > g", "touch f | tee g", "touch -t", "touch $F", "touch *.x", "touch -d x",
    # cat heredoc
    "cat >> f <<EOF\nx\nEOF", "cat > f <<EOF\nx\nEOF\nls", "cat <<EOF > f\nx\nEOF",
    "cat -n > f <<EOF\nx\nEOF", "cat > f <<EOF | wc\nx\nEOF", "cat > f g <<EOF\nx\nEOF",
    "cat > f <<EOF\n$X\nEOF", "cat>f <<EOF\nx\nEOF", "cat > f <<EOF; ls\nx\nEOF",
    "cat > f < g <<EOF\nx\nEOF",
    # cp and mv are always opaque
    "mv a b", "mv a d", "cp a b", "cp a d", "cp -R t out", "mv -t d a", "cp a b c d",
    "cp -t d a", "mv -f a b", "cp -- a b", "mv a b > log",
    # programs that merely look like forms
    "echo x", "ls", "tee f", "sed -n p f", "touch2 f", "cat f",
]


@pytest.mark.parametrize("command", NO_MATCH)
def test_no_match(command):
    assert match(command) is None


def test_producer_must_be_read_only_not_merely_known():
    # each producer below is a program the write grammar must not trust
    for producer in ("rm v", "cp a b", "mv a b", "touch t", "sort f", "uniq f", "git status",
                     "date", "type ls", "postmortem scan", "tee x", "dd if=a"):
        assert match(f"{producer} > log") is None, producer
        assert match(f"{producer} | tee log") is None, producer


# --- paths and bases -----------------------------------------------------------

def test_relative_target_needs_an_absolute_call_cwd():
    assert match("echo x > rel", None) is None
    assert match("echo x > rel", "rel/dir") is None
    assert match("echo x > rel", "") is None
    assert match("touch /abs rel", None) is None
    assert match("touch rel /abs", None) is None
    assert match("sed -i.bak 's/a/b/' rel", None) is None


def test_absolute_target_needs_no_cwd_and_repo_is_null_without_one():
    assert targets("echo x > /tmp/abs", None) == [("/tmp/abs", None, "redirect")]
    assert targets("touch /tmp/a /tmp/b", None) == [
        ("/tmp/a", None, "touch"), ("/tmp/b", None, "touch"),
    ]
    assert targets("sed -i.bak 's/a/b/' /tmp/f", None) == [
        ("/tmp/f", None, "sed_i"), ("/tmp/f.bak", None, "sed_backup"),
    ]


def test_the_repo_is_the_normalized_call_cwd():
    assert targets("echo x > f", "/tmp/ws/../ws/repo/") == [
        ("/tmp/ws/repo/f", "/tmp/ws/repo", "redirect")
    ]


def test_a_target_over_1024_characters_is_stored_as_none_and_flagged():
    long_target = "/tmp/" + "a" * 1100
    result = match(f"echo x > {long_target}")
    assert result is not None
    (target,) = result.targets
    assert target.path is None
    assert target.truncated is True
    assert target.kind == "redirect"
    short = match("echo x > f").targets[0]
    assert short.truncated is False
    exactly = "/tmp/" + "a" * (1024 - len("/tmp/"))
    assert match(f"echo x > {exactly}").targets[0].path == exactly


def test_a_placed_command_is_required():
    assert match("echo x > $F") is None
    assert match("echo 'x > f") is None
