"""N4a coverage group 2: the closed read-only grammar (docs/DESIGN.md 4.13.1,
C-8). One accepted spelling per option family of every program and one
rejected spelling per near-miss; every named row of the plan by its exact
command text.
"""

from __future__ import annotations

import pytest

from agent_postmortem import commands


def ro(command: str) -> bool:
    return commands.is_read_only(commands.lex(command))


PROGRAMS = (
    "cat head tail nl wc ls pwd echo printf which stat file du df cut tr basename "
    "dirname realpath grep rg find sed diff cmp jq"
).split()


def test_the_program_list_is_exactly_the_contract():
    assert set(commands.READ_ONLY_PROGRAMS) == set(PROGRAMS)
    assert len(commands.READ_ONLY_PROGRAMS) == len(PROGRAMS)


ACCEPT = [
    # cat
    "cat f", "cat -n f", "cat -nbsv f", "cat -A f g", "cat -e -t f", "cat -", "cat -- f",
    # head and tail
    "head f", "head -n 5 f", "head -n5 f", "head -c 10 f", "head -5 f", "head -c10 f",
    "head -n5", "tail -n5", "cut -d,", "stat -c%s", "du -d1", "sed -ne p", "grep -A2",
    "tail -n 20 f", "tail -f f", "tail -F f", "tail -n5 f", "tail -20 f", "tail -fn 5 f",
    # nl, wc
    "nl f", "nl -ba f", "nl -b a -w 3 f", "nl -p f",
    "wc -l f", "wc -lwcmL f", "wc f",
    # ls
    "ls", "ls -la", "ls -1aAlhRtrSdFGinops d", "ls --color=never d", "ls -- d",
    # pwd
    "pwd", "pwd -L", "pwd -P",
    # echo, printf
    "echo", "echo hi", "echo -n hi", "echo -ne hi", "echo -x", "echo ---", "echo -- hi",
    "printf '%s\\n' x", "printf -- '%s' x", "printf hi",
    # which, stat, file
    "which ls", "which -a ls",
    "stat f", "stat -f %z f", "stat -c %s f", "stat -L f", "stat --format %s f",
    "stat --format=%s f", "stat -c%s f",
    "file f", "file -b f", "file -bi f", "file -L f", "file --mime f",
    # du, df
    "du -sh d", "du -d 1 d", "du -d1 d", "du -skmac d",
    "df -h", "df -hk", "df",
    # cut, tr
    "cut -d, -f1 f", "cut -d , -f 1 f", "cut -c1-3 f", "cut -b 1 f",
    "tr a b", "tr -d x", "tr -ds a b", "tr 'a-z' 'A-Z'",
    # basename, dirname, realpath
    "basename a", "basename -a a b", "basename -s .x a", "basename a .x", "dirname a",
    "realpath f", "realpath -e f",
    # grep
    "grep x f", "grep -rn x d", "grep -e x -e y f", "grep -A 2 x f", "grep -A2 x f",
    "grep -m 1 x f", "grep --include '*.py' -r x d", "grep --include='*.py' x d",
    "grep --exclude-dir node_modules -r x d", "grep --color=never x f", "grep -Eio x f",
    "grep -q x f",
    # rg
    "rg --no-config x", "rg --no-config -n x d", "rg --no-config -e x -e y",
    "rg --no-config -A 2 x", "rg --no-config -A2 x", "rg --no-config -g '*.py' x",
    "rg --no-config --max-depth 2 x", "rg --no-config --max-depth=2 x",
    "rg --no-config --files", "rg --no-config --json x", "rg --no-config -t py x",
    "rg --no-config --type py x", "rg --no-config -m 3 x", "rg --no-config -m3 x",
    "rg --no-config --hidden --no-ignore -uU x", "rg --no-config -F -i -s -S -w -x -v x",
    "rg --no-config -c -l -L -N -o x", "rg --no-config -T py x", "rg --no-config -f pats x",
    "rg --no-config --files-with-matches --count x", "rg --no-config x -n",
    # find
    "find .", "find . -name x", "find . -type f -name '*.py'", "find . -maxdepth 2 -mindepth 1",
    "find . -name x -o -name y", "find . \\( -name x -o -name y \\) -print", "find . ! -name x",
    "find . -not -name x -print0", "find d1 d2 -empty",
    "find . -newer f -mtime -1 -mmin 5 -size +1k -perm 644 -prune",
    "find . -path x -ipath y -iname z -regex r", "find . -a -name x",
    # sed
    "sed -n 1,5p f", "sed -n '1,5p' f", "sed -n -e 1p f", "sed -e s/x/y/ f",
    "sed -E 's/(a)/\\1/g' f", "sed -r 's/a/b/gI' f", "sed 's/a/b/2' f", "sed '/x/d' f",
    "sed -n '$p' f", "sed '2q' f", "sed -n '/a/,/b/p' f", "sed -ne p f", "sed 'n;d' f",
    "sed '=' f", "sed -n '1!p' f", "sed 'N;s/\\n/ /' f", "sed 's|a|b|' f",
    "sed -n '1~2p' f", "sed -n '2,+3p' f", "sed 's/a/b/ip' f", "sed '3q5' f",
    "sed -n -e 1p -e 3p f",
    # diff, cmp, jq
    "diff a b", "diff -u a b", "diff -U 3 a b", "diff -U3 a b", "diff -ru a b",
    "diff --brief a b", "diff --unified a b", "diff --unified=5 a b", "diff -q a b",
    "diff -Nbwi a b", "diff -s a b", "diff -c a b", "diff -B a b",
    "cmp a b", "cmp -s a b", "cmp -l a b",
    "jq . f", "jq -r .a f", "jq -c . f", "jq -n 1", "jq -s . f", "jq -S . f", "jq -e . f",
    "jq -M . f", "jq --arg k v .a f", "jq --argjson k 1 .a f", "jq -rc .a f",
    # redirects
    "cat f 2>&1", "cat f >/dev/null", "cat f > /dev/null", "cat f 1>/dev/null",
    "cat f 2>/dev/null", "cat f &>/dev/null", "cat f >/dev/null 2>&1", "cat f 2> /dev/null",
    "cat f >> /dev/null", "cat f >>/dev/null", "cat f 2>>/dev/null", "cat f 1>>/dev/null",
    "cat f 2>&1 | wc -l",
    # composition
    "cat f | wc -l", "cat a && cat b", "cat a || cat b", "cat a; cat b", "cat a\ncat b",
    "cat f |& wc", "cat a & cat b", "cat f # comment", "cat f &&\ncat g",
    # one leading cd
    "cd /abs && cat f", "cd sub && cat f", "cd /abs && ls | wc -l", "cd '/a b' && cat f",
    # quoted data
    "grep 'a b' f", "echo \"x\"", "echo 'rm -rf /'",
]


@pytest.mark.parametrize("command", ACCEPT)
def test_accepted_spellings(command):
    assert ro(command)


REJECT = [
    # an option outside the table, per program
    "cat -z f", "cat --number f", "cat -u f",
    "head -z f", "head --lines=5 f", "tail -s 1 f", "tail --follow f",
    "nl -z f", "wc -z f", "wc --lines f",
    "ls -X", "ls --color=always", "ls -lX", "ls --sort=size", "ls -q",
    "pwd x", "pwd -x",
    "printf -v x hi", "printf -vx hi", "printf -x hi",
    "which -x ls", "stat -x f", "stat --printf %s f", "file -f list", "file -C", "file -z f",
    "du --files0-from=x", "du -X", "df -T", "df -x tmpfs",
    "cut -z f", "cut --output-delimiter=x f", "tr -x a", "basename -x a",
    "realpath --relative-to d f", "realpath -z f",
    "grep -f pats f", "grep -P x f", "grep -z x f", "grep --exclude-from=x y f",
    "grep --color=always x f", "grep -a x f", "grep --null x f",
    # an attached argument on a flag that takes none, a missing argument
    "cat -n5 f", "grep -nr5 x f", "wc -l5 f", "ls --color=never=x d",
    "head -n", "head -c", "grep -e", "cut -d", "find . -name", "jq --arg k", "du -d", "stat -c",
    "tail -n", "grep -A", "rg --no-config -e", "rg --no-config --max-depth",
    # rg: the plan's named row and each child-executing option one by one
    "rg --no-config --color=always --hostname-bin=./writer "
    "--hyperlink-format='file://{host}{path}' needle .",
    "rg -z x f", "rg --search-zip x f",
    "rg --no-config -z x f", "rg --no-config --search-zip x f",
    "rg --no-config --pre ./w x", "rg --no-config --pre=./w x",
    "rg --no-config --pre-glob '*.x' x", "rg --no-config --pre-glob='*.x' x",
    "rg --no-config --hostname-bin ./w x", "rg --no-config --hostname-bin=./w x",
    "rg --no-config --hyperlink-format fmt x", "rg --no-config --hyperlink-format=fmt x",
    "rg --no-config --color=always x", "rg --no-config --color=never x",
    "rg --no-config --sort path x", "rg --no-config -r y x", "rg --no-config --replace y x",
    "rg --no-config --engine pcre2 x", "rg --no-config --ignore-file f x",
    "rg x", "rg -n x d", "rg --files",
    # find: actions that write or run a program, an unlisted option
    r"find . -exec rm {} \;", "find . -delete", "find . -fprint out", "find . -fprintf o x",
    r"find . -ok rm {} \;", r"find . -execdir x \;", "find . -fls o", "find -L .",
    "find . -newerXY x", "find . -printf x", "find . -prune -exec x {} +",
    "find . -exec", "find . -execdir", "find . -ok", "find . -fprint", "find . -fls",
    "find . -fprint0", "find . -delete -print",
    # sed: in-place, script files, writing commands and flags
    "sed -f s p", "sed -nf s p", "sed 's/[/]/;/w out/p' f", "sed -n 's/[/]/;/w out/p' f",
    "sed -i s/x/y/ f", "sed -i.bak s/x/y/ f", "sed --in-place s/x/y/ f",
    "sed --in-place=.bak s/x/y/ f", "sed -f script f", "sed -n -f s f", "sed -nf s f",
    "sed 'w out' f", "sed 's/x/y/w out' f", "sed 's/x/y/e' f", "sed 'e' f", "sed 'r f2' f",
    "sed 'R f2' f", "sed 'W o' f", "sed -e 'w x' f", "sed 'y/a/b/' f", "sed '{p}' f",
    "sed -s p f", "sed -z p f", "sed --expression=p f", "sed 'p;w x' f", "sed 's/x/y/gw x' f",
    "sed -n", "sed -E", "sed -ni p f", "sed -in p f", "sed p -i f", "sed 's/x/y/m' f",
    "sed '# c' f", "sed 'F' f", "sed 'l' f", "sed 's/x/y' f", "sed 'p p' f",
    # diff, cmp, jq
    "diff -o out a b", "diff --output=x a b", "diff -D x a b", "diff --to-file=x a b",
    "diff -x a b", "diff -y a b",
    "cmp -i 5 a b",
    "jq -i . f", "jq --rawfile k f .", "jq -f prog f", "jq --slurpfile k f .", "jq -h",
    # programs outside the closed list (removed from the earlier grammar, and others)
    "git status", "git log", "git diff", "git show HEAD", "git branch", "git rev-parse HEAD",
    "sort f", "sort -o out f", "uniq f", "uniq -c f", "uniq a b", "date", "date -u",
    "date 0101000026", "type ls", "agent-postmortem scan", "postmortem run x",
    "cp a b", "mv a b", "rm f", "touch f", "tee f", "curl x", "python x", "awk 1 f",
    "tar tf x", "mkdir d", "chmod +x f", "ln -s a b", "dd if=a of=b", "kill 1", "sleep 1",
    "test -f f", "true", "false", "wait", "export X=1", "cd /tmp", "open f", "make", "bash x",
    "sh -c 'cat f'", "python3 -c 'print(1)'", "node x", "npm test", "ruff check", "pytest",
    # cd rules: only one leading exact `cd DIR &&`
    "cd - && cat f", "cd && cat f", "cd -- && cat f", "cd -L d && cat f", "cd -P d && cat f",
    "cd a b && cat f", "cat f && cd /x && cat g", "cd /a ; cat f", "cd /a || cat f",
    "cd /a && cd /b && cat f", "cd '' && cat f", "cd /a | cat f",
    # redirects outside the closed set
    "cat f > out", "cat f >> out", "cat f > /dev/null2", "cat f 3>/dev/null", "cat f >&2",
    "cat f 2>out", "cat f >/dev/null/x", "cat f > /dev/nulll", "cat < f",
    "cat f &>>/dev/null", "cat f 2>&1 > out", "cat f &> out", "cat f 2>&1 >out",
    "echo x>/dev/null", "cat f >| out", "cat <<EOF\nx\nEOF", "cat f <> g",
    # the hostile set
    "OPT=-delete find . \"$OPT\"", "uniq {a,b}", "uniq *", "PATH=./bin cat f",
    "f() { ls; }", "alias x=y", "time cat f", "command cat f", "builtin cat f", "exec cat f",
    "eval cat f", "(cat f)", "{ cat f; }", "xargs cat", "env cat f", "cat $(echo f)",
    "uniq +(input)", "cat @(a|b)", "ls !(x)", "cat `echo f`", "cat \"$F\"",
    # lexer opaque triggers inside an otherwise read-only command
    "cat $F", "cat *.py", "cat ~/x", "cat f > $O", "ls {a,b}", "cat <(ls)",
    "cat \"$(echo f)\"", "cat f | wc -l >(x)", "echo ?", "echo [a]", "cat 'unbalanced",
    "nohup cat f", "cat f &&", "! cat f",
]


@pytest.mark.parametrize("command", REJECT)
def test_rejected_spellings(command):
    assert not ro(command)


def test_removed_programs_are_each_write_capable():
    for program in ("date", "uniq", "type", "sort", "git", "agent-postmortem", "postmortem"):
        assert program not in commands.READ_ONLY_PROGRAMS
        assert not ro(f"{program} f")


def test_an_empty_command_is_not_read_only():
    assert not ro("")
    assert not ro("   \n")
    assert not ro("# only a comment")


# --- the sed script rule -------------------------------------------------------------

SED_OK = [
    "p", "d", "q", "q3", "=", "n", "N", "1p", "$p", "1,5p", "/a/p", "/a/,/b/d", "2,+1p", "2,~4p",
    "1~2p", "1!d", "1 ! d", "s/a/b/", "s/a/b/g", "s/a/b/gpIi", "s/a/b/3", "s/a/b/2g",
    "s|a|b|", "s,a,b,", "s/a\\/b/c/", "s/a/b\\/c/", "p;p", "p\np", "p; d", "1p;2d", " p ",
    "s/a/b/;p", "\\,a,p", "/a/Ip", "/a/I,/b/Id", "", ";",
]


@pytest.mark.parametrize("script", SED_OK)
def test_sed_script_accepted(script):
    assert commands.sed_script_ok(script)


SED_BAD = [
    "y", "y;p", "s/[/]/;/w out/p", "w out", "w", "W out", "e", "e cmd", "r f", "R f", "y/a/b/", "{p}", "{", "}", "# c", "F",
    "l", "i\\x", "a x", "c x", "D", "G", "H", "g", "h", "x", "b", "t", "T", ":a", "v", "z", "Q",
    "s/a/b/w out", "s/a/b/e", "s/a/b/m", "s/a/b/M", "s/a/b/x", "s/a/b", "s/a", "s", "s//",
    "p p", "pp", "1,p", ",p", "1,2,3p", "1p2", "/a", "/a/", "\\", "s\\a\\b\\", "s\na\nb\n",
    "p;w x", "1w x", "1,2w x", "s/a/b/;w x", "$!{p}",
]


@pytest.mark.parametrize("script", SED_BAD)
def test_sed_script_rejected(script):
    assert not commands.sed_script_ok(script)
