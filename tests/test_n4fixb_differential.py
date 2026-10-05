"""Fix B, M-N4-2: the speedups of the lexer and the classifier change no result.

tests/n4_reference_commands.py is the frozen classifier of the commit before the
speed work (a verbatim copy of src/agent_postmortem/commands.py at that commit,
never edited). Every function the ledger and the reader use must give the same
answer on the frozen copy and on the live module for:

- every string literal of the classifier test files (every table row), under
  several call cwds; and
- a seeded fuzz corpus of at least 50,000 generated commands (shell-shaped
  pieces glued with quotes, redirects, separators, heredocs, wrappers, and
  random character edits).

A generated command is never executed; it is only classified.
"""

from __future__ import annotations

import ast
import dataclasses
import random
from pathlib import Path

import pytest

from agent_postmortem import commands as live
from tests import n4_reference_commands as ref

TESTS = Path(__file__).resolve().parent
CWDS = (None, "/tmp/ws", "/tmp/ws/repo", "rel/dir", "//tmp/ws", "")
FUZZ_SEED = 20261003
FUZZ_COUNT = 60000
RESULTS = ("[main 1a2b3c4] msg\n 1 file changed", "no summary here", "[a b] x\n[main deadbee] y\n")


# Fields added after the frozen classifier (field milestone F, FA6): `Lexed.reason_at`
# and `CallClass.commit_command_present`. The frozen copy has neither, so the comparison is over every
# field it does have; tests/test_fa_commit_no_id.py pins the new ones.
_FIELDS_ADDED_AFTER_THE_FROZEN_COPY = frozenset({"reason_at", "commit_command_present"})


def as_data(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: as_data(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name not in _FIELDS_ADDED_AFTER_THE_FROZEN_COPY
        }
    if isinstance(value, (tuple, list)):
        return [as_data(item) for item in value]
    return value


def table_commands() -> list[str]:
    found: dict[str, None] = {}
    for path in sorted(TESTS.glob("test_n4*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value:
                found[node.value] = None
    return list(found)


PROGRAMS = (
    "cat", "head", "tail", "ls", "grep", "rg", "sed", "find", "touch", "tee", "echo", "printf",
    "git", "cd", "pwd", "wc", "jq", "diff", "cmp", "cp", "mv", "rm", "nohup", "xargs", "env",
    "bash", "sh", "codex", "claude", "make", "python3", "postmortem", "time", "eval", "source",
    "stat", "file", "du", "df", "cut", "tr", "basename", "dirname", "realpath", "which", "nl",
    "sort", "uniq", "date", "type", "pushd", "popd", "export", "true", "false", "test", "[",
    "aider", "git commit", "git -C /tmp/b commit", "git -C rel commit", "git --git-dir=/x commit",
)
ARGS = (
    "-n", "-l", "-la", "-i", "-i.bak", "-e", "-f", "-t", "-d", "-r", "-c", "-a", "-m", "-q",
    "-p", "-s", "-E", "-A", "-B", "-C", "-U", "-1", "-5", "-NUM", "--", "--color=never",
    "--no-config", "--unified=3", "--brief", "--in-place", "--dry-run", "--include", "--arg",
    "foo", "bar.txt", "/tmp/x", "/tmp/ws/f", "../y", "./z", "a b", "'a b'", '"c d"', "$HOME",
    "$(ls)", "`ls`", "*.py", "{a,b}", "~/z", "~", "s/x/y/", "'s/a/b/g;p'", "'w x'", "1p",
    "'s/a/b/e'", "p", "-m", "msg", '"msg"', "'msg'", "-m'x'", "&", "2", "1", "/dev/null",
    "a\\ b", "\\$x", "\\\\", "'", '"', "$", "#", "#c", "=", "a=b", "A=1", "(", ")", "!", "\\",
    "2>&1", "[a]", "?", "x?y", "+(a|b)", "@(a)", "!(x)", "{1..3}", "-name", "'*.py'", "-type",
    "f", "-prune", "-o", "-print", "(", ")", "!", "1,3p", "$(cat <<'EOF'\nmsg\nEOF\n)",
    "\"$(cat <<'EOF'\nmsg\nEOF\n)\"", "\"$(cat <<-'EOF'\nmsg\n\tEOF\n)\"",
    "\"$(cat <<EOF\nmsg\nEOF\n)\"", "\"a\\\"b\"", "'a\\'", "\"unterminated", "'unterminated",
    "-ac", "-ca", "-x", "-h", "origin", "main", "status", "log", "add", "-A", "push",
)
REDIRECTS = (
    ">", ">>", "2>", "2>>", "1>", "1>>", "&>", "&>>", ">&", "<&", "2>&1", "1>&2", ">|", "<>",
    "<", "<<EOF", "<<-EOF", "<<'EOF'", '<<"EOF"', "<<<", ">(x)", "<(y)", "3>", "3>&1", "10>",
    "0<", ">&2", "1>/dev/null", "2>/dev/null", ">/dev/null", ">>/dev/null", "&>/dev/null",
)
TARGETS = ("out", "/tmp/o", "/dev/null", "$F", "'a b'", "../up", "f.txt", "-", "x y", "")
SEPARATORS = (" && ", " || ", " ; ", ";", " | ", " |& ", " & ", " &\n", "\n", "\n\n", " ;; ",
              "&&", "||", "|", ";", "&", " \\\n", " # c\n")
HEREDOC_BODIES = ("body\n", "\tEOF\n", "  EOF\n", "$HOME\n", "`ls`\n", "", "a\nb\nc\n", "EOF2\n")
WRAPPERS = ("bash -c ", "sh -c ", "bash -lc ", "sudo ", "(", "{ ", "! ", "time ", "nohup ", "env X=1 ")
SPECIAL = "\\'\"$`*?[](){}&;|<>#~\n\t !=,.-/:@+%^"


CLEAN_PROGRAMS = (
    "cat", "head", "tail", "ls", "grep", "rg", "sed", "find", "touch", "tee", "echo", "printf",
    "wc", "jq", "diff", "cmp", "stat", "file", "du", "df", "cut", "tr", "basename", "dirname",
    "realpath", "which", "nl", "pwd", "git status", "git commit", "git -C /tmp/b commit",
    "git -C rel commit", "cp", "rm", "make", "sort",
    "cat", "ls", "grep", "head", "echo", "sed", "find", "touch", "wc", "rg", "diff", "jq",
)
CLEAN_ARGS = (
    "-n", "-l", "-la", "-i", "-i.bak", "-e", "-f", "-t", "-d", "-r", "-c", "-a", "-m", "-q",
    "-s", "-E", "-A", "-B", "-C", "-U", "-1", "-5", "--", "--color=never", "--no-config",
    "--unified=3", "--brief", "--arg", "--files", "--type", "foo", "bar.txt", "/tmp/x", "f",
    "/tmp/ws/f", "../y", "./z", "'a b'", '"c d"', "s/x/y/", "'s/a/b/g;p'", "'w x'", "1p", "p",
    "2", "1", "msg", '"msg"', "-m", "-name", "-type", "-prune", "-o", "-print", "-maxdepth",
    "202601010000", "2026-01-01", "ref", "-ac", "-ca", "-x", "-", "x", "y", "/dev/null",
    "'s/a/b/e'", "'1,3p'", "'$!d'", "'/x/p'", "-p", "-h", "-S", "-L", "-v",
)
CLEAN_REDIRECTS = (
    ">", ">>", "2>", "2>>", "1>", "&>", "2>&1", "1>&2", ">/dev/null", "2>/dev/null",
    ">>/dev/null", "&>/dev/null", "<", "<<EOF", "<<-EOF", "<<'EOF'",
)
CLEAN_SEPARATORS = (" && ", " || ", " ; ", " | ", " |& ", " & ", "\n", " ;", " # c\n")


def gen_clean_segment(rng: random.Random) -> str:
    words = [rng.choice(CLEAN_PROGRAMS)]
    if words[0] in ("git commit", "git -C /tmp/b commit", "git -C rel commit") or rng.random() < 0.1:
        words.append("-m")
        words.append(rng.choice(("msg", "'msg'", '"msg"', "-q", "--dry-run", "--quiet", "--short")))
    for _ in range(rng.randrange(0, 5)):
        words.append(rng.choice(CLEAN_ARGS))
    if rng.random() < 0.25:
        redirect = rng.choice(CLEAN_REDIRECTS)
        if redirect.startswith("<<"):
            piece = redirect
        elif redirect in ("2>&1", "1>&2"):
            piece = redirect
        else:
            piece = redirect + rng.choice(("", " ")) + rng.choice(("out", "/tmp/o", "/dev/null", "../up"))
        words.insert(rng.randrange(len(words) + 1), piece)
    text = " ".join(words)
    if "<<" in text:
        text += "\n" + rng.choice(HEREDOC_BODIES[:1] + HEREDOC_BODIES[1:3] + HEREDOC_BODIES[4:7])
        text += rng.choice(("EOF", "EOF", "EOF", "\tEOF"))
    return text


def gen_clean_command(rng: random.Random) -> str:
    kind = rng.randrange(12)
    if kind == 3:
        target = rng.choice(("out", "/tmp/o", "../up", "'a b'", "d/f.txt", "f", "-x"))
        flag = rng.choice(("", "-c ", "-t 202601010000 ", "-r ref ", "-am ", "-- "))
        return rng.choice((
            f"echo hi > {target}", f"echo hi >> {target}", f"cat a b > {target}",
            f"ls -la | tee {target}", f"cat f | grep x | tee -a {target}",
            f"sed -i 's/a/b/' {target}", f"sed -i.bak 's/a/b/g' {target}",
            f"sed -i '' 's/a/b/' {target}", f"sed -i .orig p {target}", f"touch {flag}{target}",
            f"touch {flag}{target} g", f"touch {target} {flag}g", f"cat > {target} <<EOF\nx\nEOF\n",
            f"cat > {target} <<-EOF\nx\n\tEOF\n", f"printf x > {target}", f"cd /tmp/a && echo x > {target}",
            f"echo x > {target} 2>&1", f"echo x 2> {target}", f"sed -n p f > {target}",
            f"sed -i s/x/y/ {target} -n", f"echo x > {'a' * rng.choice((1010, 1019, 1020, 1030))}",
        ))
    if kind == 0:
        return "cd " + rng.choice(("/tmp/a", "rel", "/tmp/a/b")) + " && " + gen_clean_segment(rng)
    if kind == 1:
        inner = gen_clean_segment(rng).replace("'", "")
        return rng.choice(("bash -c ", "sh -c ", "bash -lc ")) + "'" + inner + "'"
    if kind == 2:
        return rng.choice((
            'git commit -m "$(cat <<\'EOF\'\nmsg\nEOF\n)"',
            'git -C /tmp/b commit -m "$(cat <<-\'EOF\'\nmsg\n\tEOF\n)" && git status',
            'cd /tmp/a && git add -A && git commit -m "$(cat <<\'EOF\'\nmsg\nEOF\n)"',
        ))
    parts = [gen_clean_segment(rng)]
    for _ in range(rng.choice((0, 0, 0, 0, 1, 1, 2))):
        parts.append(rng.choice(CLEAN_SEPARATORS))
        parts.append(gen_clean_segment(rng))
    if rng.random() < 0.08:
        parts.append(rng.choice((" &", ";", " ;", "\n")))
    return "".join(parts)


def quote_variant(rng: random.Random, word: str) -> str:
    kind = rng.randrange(8)
    if kind == 0 and "'" not in word:
        return f"'{word}'"
    if kind == 1 and '"' not in word and "$" not in word and "`" not in word:
        return f'"{word}"'
    if kind == 2 and word:
        cut = rng.randrange(len(word))
        return word[:cut] + "\\" + word[cut:]
    return word


def gen_segment(rng: random.Random) -> str:
    words = [rng.choice(PROGRAMS)]
    for _ in range(rng.randrange(0, 6)):
        words.append(quote_variant(rng, rng.choice(ARGS)))
    for _ in range(rng.choice((0, 0, 0, 1, 1, 2))):
        spot = rng.randrange(len(words) + 1)
        redirect = rng.choice(REDIRECTS)
        target = rng.choice(TARGETS) if not redirect.startswith("<<") and "&" != redirect[-1] else ""
        glue = rng.choice(("", " ", " ", " "))
        if redirect.startswith("<<") or redirect in ("2>&1", "1>&2"):
            piece = redirect
        else:
            piece = redirect + glue + target
        if rng.random() < 0.15 and spot > 0:
            words[spot - 1] = words[spot - 1] + piece
        else:
            words.insert(spot, piece)
    text = " ".join(words)
    if "<<" in text:
        body = rng.choice(HEREDOC_BODIES)
        text += "\n" + body + rng.choice(("EOF", "EOF", "EOF", "\tEOF", "EOF ", "NOPE"))
    return text


def gen_command(rng: random.Random) -> str:
    if rng.random() < 0.55:
        return gen_clean_command(rng)
    kind = rng.randrange(20)
    if kind == 0:
        return "cd " + rng.choice(("/tmp/a", "rel", "-", "", "/tmp/a b", "$X")) + " && " + gen_segment(rng)
    if kind == 1:
        inner = gen_segment(rng).replace('"', '\\"')
        return rng.choice(WRAPPERS) + '"' + inner + '"'
    if kind == 2:
        return rng.choice(WRAPPERS) + "'" + gen_segment(rng).replace("'", "") + "'"
    if kind == 3:
        return "".join(rng.choice(SPECIAL + "abc123") for _ in range(rng.randrange(0, 40)))
    parts = [gen_segment(rng)]
    for _ in range(rng.choice((0, 0, 1, 1, 2, 3))):
        parts.append(rng.choice(SEPARATORS))
        parts.append(gen_segment(rng))
    if rng.random() < 0.1:
        parts.append(rng.choice((" &", ";", " ;", "\n", " &&", " |", "")))
    text = "".join(parts)
    if rng.random() < 0.3:
        chars = list(text)
        for _ in range(rng.randrange(1, 4)):
            op = rng.randrange(3)
            spot = rng.randrange(len(chars) + 1)
            if op == 0:
                chars.insert(spot, rng.choice(SPECIAL))
            elif op == 1 and chars:
                del chars[min(spot, len(chars) - 1)]
            elif chars:
                chars[min(spot, len(chars) - 1)] = rng.choice(SPECIAL)
        text = "".join(chars)
    return text


def fuzz_commands() -> list[str]:
    rng = random.Random(FUZZ_SEED)
    return [gen_command(rng) for _ in range(FUZZ_COUNT)]


def compare(command: str, cwd, rng_flags: tuple[bool, bool, str]) -> None:
    background, hit, tool = rng_flags
    for message_form in (False, True):
        new = live.lex(command, commit_message_form=message_form)
        old = ref.lex(command, commit_message_form=message_form)
        assert as_data(new) == as_data(old), ("lex", message_form, command)
    new_lexed = live.lex(command)
    old_lexed = ref.lex(command)
    assert live.is_read_only(new_lexed) == ref.is_read_only(old_lexed), ("ro", command)
    assert as_data(live.match_write_grammar(new_lexed, cwd)) == as_data(
        ref.match_write_grammar(old_lexed, cwd)
    ), ("write", command, cwd)
    assert as_data(live.segment_bases(new_lexed, cwd)) == as_data(
        ref.segment_bases(old_lexed, cwd)
    ), ("bases", command, cwd)
    new_facts = live.commit_facts(command, cwd)
    old_facts = ref.commit_facts(command, cwd)
    assert as_data(new_facts) == as_data(old_facts), ("commit", command, cwd)
    for text in RESULTS:
        assert live.commit_ids(new_facts, text) == ref.commit_ids(old_facts, text)
    kwargs = {"run_in_background": background, "backgrounded_hit": hit, "cwd": cwd}
    new_class = live._classify_call(tool, command, **kwargs)
    old_class = ref._classify_call(tool, command, **kwargs)
    assert as_data(new_class) == as_data(old_class), ("classify", tool, command, kwargs)
    if new_class is not None:
        assert new_class.to_dict() == old_class.to_dict()
    cached = live.classify_call(tool, command, **kwargs)
    assert as_data(cached) == as_data(old_class), ("classify_call", tool, command, kwargs)


def test_every_table_row_classifies_identically_to_the_frozen_classifier():
    rows = table_commands()
    assert len(rows) > 1000
    for index, command in enumerate(rows):
        cwd = CWDS[index % len(CWDS)]
        compare(command, cwd, (index % 7 == 0, index % 11 == 0, "Bash"))
        compare(command, "/tmp/ws", (False, False, "Monitor"))
        compare(command, CWDS[(index + 1) % len(CWDS)], (False, False, "Bash"))


def test_the_fuzz_corpus_classifies_identically_to_the_frozen_classifier():
    corpus = fuzz_commands()
    assert len(corpus) >= 50000
    assert len(set(corpus)) > 40000
    tools = ("Bash", "Bash", "Bash", "Monitor", "Agent")
    for index, command in enumerate(corpus):
        cwd = CWDS[index % len(CWDS)]
        compare(command, cwd, (index % 13 == 0, index % 17 == 0, tools[index % len(tools)]))


@pytest.mark.parametrize("script", [
    "p", "1p", "$!d", "/x/p", "s/a/b/", "s/a/b/g", "s,a,b,", "s/[/]/x/", "w x", "s/a/b/w x",
    "1,3p;5d", "/a/,/b/p", "\\,x,p", "q5", "n;N", "y/a/b/", "s/a/b/e", "", ";;", "{p}",
])
def test_sed_scripts_agree(script):
    assert live.sed_script_ok(script) == ref.sed_script_ok(script)


def test_the_frozen_reference_is_not_the_live_module():
    assert ref is not live
    assert ref.__name__ != live.__name__
