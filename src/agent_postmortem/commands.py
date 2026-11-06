"""Pure command-text classifier for Bash and Monitor calls (docs/DESIGN.md
4.13.1, N4a). Standard library only, no subprocess, and no filesystem access
except the stat that `is_bounded` makes of an output redirect's target.

The transcript records a command's text, never the shell it ran in, so
nothing here can prove a command did not write. The grammars decide one
syntactic claim only: "no write is recognized in the recorded command".
"""

from __future__ import annotations

import functools
import os
import re
import stat as _stat
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Word:
    text: str
    raw: str
    quoted: bool


@dataclass(frozen=True)
class Redirect:
    fd: str | None
    op: str
    target: str | None
    target_quoted: bool
    glued_before: bool
    after_words: int
    body: str | None = None


@dataclass(frozen=True)
class Segment:
    words: tuple[Word, ...]
    redirects: tuple[Redirect, ...]
    sep_after: str | None


@dataclass(frozen=True)
class Lexed:
    segments: tuple[Segment, ...]
    reasons: tuple[str, ...]
    trailing_op: str | None
    # FA6: every (reason, segment index) the lexer raised, the index being the
    # segment under construction when it fired (a reason about the end of the
    # command names the last segment). `reasons` stays the call-global answer;
    # this lets one candidate segment be judged on its own (commit_command_ran).
    reason_at: tuple[tuple[str, int], ...] = field(default=(), compare=False, repr=False)

    @property
    def placed(self) -> bool:
        return not self.reasons


_FIRST_WORD_OPAQUE = frozenset(
    {"{", "!", "function", "alias", "eval", "exec", "source", ".", "command",
     "builtin", "time", "xargs", "env", "nohup"}
)
_ASSIGN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\[[^\]]*\])?\+?=")
_BRACE_RE = re.compile(r"\{[^{}]*(?:,|\.\.)[^{}]*\}")
_MESSAGE_RE = re.compile(
    r"\$\([ \t]*cat[ \t]+<<(-?)(?:'([A-Za-z_][A-Za-z0-9_]*)'|\"([A-Za-z_][A-Za-z0-9_]*)\")[ \t]*\n"
)
_CLOSE_PAREN_RE = re.compile(r"[ \t\n]*\)")
_EXTGLOB_PREV = "+@!?*"
# Characters scan() treats specially in any word state; everything else is a
# plain word character once a word is active.
_PLAIN_RUN_RE = re.compile(r"[^ \t\n\\'\"$`*?\[()&;|<>]+")
_SPECIAL_CHARS = frozenset(" \t\n\\'\"$`*?[()&;|<>")
# Inside double quotes: a run with no quote, backslash, or substitution start.
_DQUOTE_RUN_RE = re.compile(r'[^"\\$`]+')


class _Lexer:
    """One pass over the command. Opaque triggers are recorded as reasons and
    scanning continues, so the words of an opaque command are still available
    to the background and delegation rows."""

    def __init__(self, command: str, commit_message_form: bool) -> None:
        self.s = command
        self.n = len(command)
        self.i = 0
        self.message_form = commit_message_form
        self.reasons: list[str] = []
        self.reason_at: list[tuple[str, int]] = []
        self.segs: list[dict] = []
        self.words: list[Word] = []
        self.redirs: list[list] = []
        self.heredocs: list[tuple[list, bool, bool]] = []
        self.pending: list | None = None
        self.trailing_op: str | None = None
        self._reset_word()

    # -- helpers ------------------------------------------------------------
    def reason(self, name: str, index: int | None = None) -> None:
        if name not in self.reasons:
            self.reasons.append(name)
        self.reason_at.append((name, len(self.segs) if index is None else index))

    def _reset_word(self) -> None:
        self.w_active = False
        self.w_start = 0
        # Pieces of the word: the text, and the same with every quoted or
        # escaped character replaced by "Q" (what brace and extglob detection
        # look at); w_last is the final character of the second.
        self.w_text: list[str] = []
        self.w_unq: list[str] = []
        self.w_last = ""
        self.w_quoted = False

    def _begin_word(self) -> None:
        if not self.w_active:
            self.w_active = True
            self.w_start = self.i

    def _add(self, ch: str, quoted: bool) -> None:
        self._begin_word()
        self.w_text.append(ch)
        last = "Q" if quoted else ch
        self.w_unq.append(last)
        self.w_last = last
        if quoted:
            self.w_quoted = True

    def _add_quoted(self, text: str) -> None:
        """Every character of text, quoted (the word is already active)."""
        if text:
            self.w_text.append(text)
            self.w_unq.append("Q" * len(text))
            self.w_last = "Q"

    def flush_word(self) -> None:
        if not self.w_active:
            return
        pieces = self.w_text
        text = pieces[0] if len(pieces) == 1 else "".join(pieces)
        word = Word(text, self.s[self.w_start:self.i], self.w_quoted)
        if "{" in text and _BRACE_RE.search("".join(self.w_unq)):
            self.reason("brace")
        if self.pending is not None:
            pending = self.pending
            self.pending = None
            pending[2] = word.text
            pending[3] = word.quoted
            op = pending[1]
            if op in (">&", "<&"):
                if not (op == ">&" and pending[0] == "2" and word.text == "1"):
                    self.reason("fd_redirect")
            if op in ("<<", "<<-"):
                self.heredocs.append((pending, op == "<<-", word.quoted))
        else:
            self.words.append(word)
        self.w_active = False
        self.w_text = []
        self.w_unq = []
        self.w_last = ""
        self.w_quoted = False

    def end_segment(self, op: str) -> None:
        self.flush_word()
        if self.pending is not None:
            self.reason("missing_redirect_target")
            self.pending = None
        if self.words or self.redirs:
            self.segs.append({"words": self.words, "redirs": self.redirs, "sep": op})
            self.words = []
            self.redirs = []
        elif op != "\n":
            self.reason("empty_segment")

    # -- scanning -------------------------------------------------------------
    def scan(self) -> None:
        s, n = self.s, self.n
        plain_run = _PLAIN_RUN_RE.match
        special = _SPECIAL_CHARS
        while self.i < n:
            c = s[self.i]
            if c in " \t":
                self.flush_word()
                self.i += 1
                continue
            if c not in special and (self.w_active or not (c == "~" or c == "#" or c.isdigit())):
                # The common case first: a run of plain characters (every
                # character not in _SPECIAL_CHARS; ~, # and a digit are plain
                # inside a word and special only where a word starts).
                chunk = plain_run(s, self.i).group()
                if not self.w_active:
                    self.w_active = True
                    self.w_start = self.i
                self.w_text.append(chunk)
                self.w_unq.append(chunk)
                self.w_last = chunk[-1]
                self.i += len(chunk)
                if self.i < n and s[self.i] in " \t":
                    # The blank after a word ends it: what the next pass does.
                    self.flush_word()
                    self.i += 1
                continue
            if c == "\n":
                self.flush_word()
                self.i += 1
                if self.heredocs:
                    self._read_heredocs()
                self.end_segment("\n")
            elif c == "\\":
                self._backslash()
            elif c == "'":
                self._squote()
            elif c == '"':
                self._dquote()
            elif c == "$":
                self.reason("expansion")
                self._add(c, False)
                self.i += 1
            elif c == "`":
                self.reason("command_substitution")
                self._add(c, False)
                self.i += 1
            elif c in "*?[":
                self.reason("glob")
                self._add(c, False)
                self.i += 1
            elif c == "~" and not self.w_active:
                self.reason("tilde")
                self._add(c, False)
                self.i += 1
            elif c in "()":
                if c == "(" and self.w_last and self.w_last in _EXTGLOB_PREV:
                    self.reason("extglob")
                else:
                    self.reason("paren")
                self._add(c, False)
                self.i += 1
            elif c == ";":
                if s.startswith(";;", self.i):
                    self.reason("double_semicolon")
                    self.i += 1
                self.i += 1
                self.end_segment(";")
            elif c == "&":
                if s.startswith("&&", self.i):
                    self.i += 2
                    self.end_segment("&&")
                elif s.startswith("&>", self.i):
                    self._redirect(None, self.w_active)
                else:
                    self.i += 1
                    self.end_segment("&")
            elif c == "|":
                if s.startswith("||", self.i):
                    self.i += 2
                    self.end_segment("||")
                elif s.startswith("|&", self.i):
                    self.i += 2
                    self.end_segment("|&")
                else:
                    self.i += 1
                    self.end_segment("|")
            elif c in "<>":
                self._redirect(None, self.w_active)
            elif c == "#" and not self.w_active:
                nl = s.find("\n", self.i)
                self.i = n if nl < 0 else nl
            elif c.isdigit() and not self.w_active and self._fd_prefix():
                pass
            else:
                # ~, # or a digit that did not start a special form: plain.
                chunk = plain_run(s, self.i).group()
                self._begin_word()
                self.w_text.append(chunk)
                self.w_unq.append(chunk)
                self.w_last = chunk[-1]
                self.i += len(chunk)
        self._finish()

    def _fd_prefix(self) -> bool:
        j = self.i
        while j < self.n and self.s[j].isdigit():
            j += 1
        if j < self.n and self.s[j] in "<>":
            fd = self.s[self.i:j]
            self.i = j
            self._redirect(fd, False)
            return True
        return False

    def _backslash(self) -> None:
        s = self.s
        if self.i + 1 >= self.n:
            self._add("\\", False)
            self.i += 1
            return
        nxt = s[self.i + 1]
        if nxt == "\n":
            self.i += 2
            return
        self._begin_word()
        self._add(nxt, True)
        if nxt in "*?[":
            self.reason("glob")
        elif nxt == "$":
            self.reason("expansion")
        elif nxt == "`":
            self.reason("command_substitution")
        self.i += 2

    def _squote(self) -> None:
        end = self.s.find("'", self.i + 1)
        self._begin_word()
        self.w_quoted = True
        if end < 0:
            self.reason("unbalanced_quote")
            self.i = self.n
            return
        self._add_quoted(self.s[self.i + 1:end])
        self.i = end + 1

    def _dquote(self) -> None:
        s, n = self.s, self.n
        self._begin_word()
        self.w_quoted = True
        self.i += 1
        run = _DQUOTE_RUN_RE.match
        while self.i < n:
            found = run(s, self.i)
            if found is not None:
                self._add_quoted(found.group())
                self.i = found.end()
                if self.i >= n:
                    break
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\" and self.i + 1 < n and s[self.i + 1] in '$`"\\\n':
                nxt = s[self.i + 1]
                if nxt == "\n":
                    self.i += 2
                    continue
                if nxt == "$":
                    self.reason("expansion")
                elif nxt == "`":
                    self.reason("command_substitution")
                self._add(nxt, True)
                self.i += 2
            elif c == "$":
                if self.message_form and self._message_literal():
                    continue
                self.reason("expansion")
                self._add(c, True)
                self.i += 1
            elif c == "`":
                self.reason("command_substitution")
                self._add(c, True)
                self.i += 1
            else:
                self._add(c, True)
                self.i += 1
        self.reason("unbalanced_quote")

    def _message_literal(self) -> bool:
        """The one literal substitution: $(cat <<'DELIM' ... DELIM )."""
        s = self.s
        m = _MESSAGE_RE.match(s, self.i)
        if m is None:
            return False
        strip = m.group(1) == "-"
        delim = m.group(2) or m.group(3)
        pos = m.end()
        body: list[str] = []
        while pos <= self.n:
            eol = s.find("\n", pos)
            if eol < 0:
                return False
            line = s[pos:eol]
            if (line.lstrip("\t") if strip else line) == delim:
                close = _CLOSE_PAREN_RE.match(s, eol + 1)
                if close is None:
                    return False
                self._add_quoted("".join(body))
                self.i = close.end()
                return True
            body.append(line + "\n")
            pos = eol + 1
        return False

    def _redirect(self, fd: str | None, glued: bool) -> None:
        s = self.s
        if glued:
            self.flush_word()
        c = s[self.i]
        nxt = s[self.i + 1:self.i + 2]
        if c == "&":
            op = "&>>" if s.startswith("&>>", self.i) else "&>"
        elif c == ">":
            if nxt == "(":
                self.reason("process_substitution")
                self.i += 2
                return
            op = {">": ">>", "&": ">&", "|": ">|"}.get(nxt, ">")
        else:
            if nxt == "(":
                self.reason("process_substitution")
                self.i += 2
                return
            if s.startswith("<<<", self.i):
                self.reason("here_string")
                op = "<<<"
            elif s.startswith("<<-", self.i):
                op = "<<-"
            elif s.startswith("<<", self.i):
                op = "<<"
            else:
                op = {"&": "<&", ">": "<>"}.get(nxt, "<")
        self.i += len(op)
        if self.pending is not None:
            self.reason("missing_redirect_target")
        # [fd, op, target, target_quoted, glued_before, after_words, body]
        self.pending = [fd, op, None, False, glued, len(self.words), None]
        self.redirs.append(self.pending)

    def _read_heredocs(self) -> None:
        s = self.s
        for builder, strip, quoted in self.heredocs:
            delim = builder[2]
            body: list[str] = []
            found = False
            pos = self.i
            while pos <= self.n:
                eol = s.find("\n", pos)
                end = self.n if eol < 0 else eol
                line = s[pos:end]
                if (line.lstrip("\t") if strip else line) == delim:
                    found = True
                    pos = self.n if eol < 0 else eol + 1
                    break
                if eol < 0:
                    break
                body.append(line + "\n")
                pos = eol + 1
            if not found:
                self.reason("unclosed_heredoc")
                self.i = self.n
                self.heredocs = []
                return
            text = "".join(body)
            builder[6] = text
            if not quoted and ("$" in text or "`" in text):
                self.reason("heredoc_expansion")
            self.i = pos
        self.heredocs = []

    def _finish(self) -> None:
        self.flush_word()
        if self.pending is not None:
            self.reason("missing_redirect_target")
            self.pending = None
        if self.heredocs:
            self.reason("unclosed_heredoc")
        if self.words or self.redirs:
            self.segs.append({"words": self.words, "redirs": self.redirs, "sep": None})
        elif self.segs:
            last = self.segs[-1]["sep"]
            if last in ("&&", "||", "|", "|&"):
                self.reason("dangling_operator", len(self.segs) - 1)
            elif last in (";", "&"):
                self.trailing_op = last
            self.segs[-1]["sep"] = None

    def result(self) -> Lexed:
        segments = []
        for index, seg in enumerate(self.segs):
            words = tuple(seg["words"])
            if words:
                first = words[0]
                if first.text in _FIRST_WORD_OPAQUE or (
                    "=" in first.raw and _ASSIGN_RE.match(first.raw)
                ):
                    self.reason("first_word", index)
            segments.append(Segment(
                words,
                tuple(Redirect(*r) for r in seg["redirs"]),
                seg["sep"],
            ))
        return Lexed(
            tuple(segments), tuple(self.reasons), self.trailing_op, tuple(self.reason_at),
        )


def lex(command: str, *, commit_message_form: bool = False) -> Lexed:
    """Lex the FULL command text (docs/DESIGN.md 4.13.1 Tokenization, C-7).
    commit_message_form places the one literal `$(cat <<'DELIM' ... )`
    commit-message substitution as a single word (commit grammar only)."""
    lexer = _Lexer(command, commit_message_form)
    lexer.scan()
    return lexer.result()


# --- the closed read-only grammar (docs/DESIGN.md 4.13.1, C-8) -----------------


@dataclass(frozen=True)
class _Spec:
    """One program's option table: short options by character, long options
    by name, each with its arity (0, 1, or 2 arguments; an argument may be
    attached or separate). Anything outside the table is write-capable."""

    short: dict
    long: dict
    exact: frozenset = frozenset()
    long_optional: frozenset = frozenset()
    numeric: bool = False
    lenient: bool = False
    max_operands: int | None = None
    required: str | None = None


def _flags(chars: str, **extra: int) -> dict:
    table = {c: 0 for c in chars}
    table.update(extra)
    return table


_SPECS: dict[str, _Spec] = {
    "cat": _Spec(_flags("nbsvetA"), {}),
    "head": _Spec(_flags("", n=1, c=1), {}, numeric=True),
    "tail": _Spec(_flags("fF", n=1, c=1), {}, numeric=True),
    "nl": _Spec(_flags("p", b=1, n=1, w=1, s=1, v=1, i=1, l=1), {}),
    "wc": _Spec(_flags("lwcmL"), {}),
    "ls": _Spec(_flags("1aAlhRtrSdFGinops"), {}, exact=frozenset({"--color=never"})),
    "pwd": _Spec(_flags("LP"), {}, max_operands=0),
    "echo": _Spec(_flags("neE"), {}, lenient=True),
    "printf": _Spec({}, {}),
    "which": _Spec(_flags("as"), {}),
    "stat": _Spec(_flags("L", f=1, c=1), {"--format": 1}),
    "file": _Spec(_flags("biL"), {"--mime": 0}),
    "du": _Spec(_flags("shkmac", d=1), {}),
    "df": _Spec(_flags("hk"), {}),
    "cut": _Spec(_flags("", d=1, f=1, c=1, b=1), {}),
    "tr": _Spec(_flags("dsc"), {}),
    "basename": _Spec(_flags("a", s=1), {}),
    "dirname": _Spec({}, {}),
    "realpath": _Spec(_flags("qems"), {}),
    "grep": _Spec(
        _flags("rRnilLcvwxEFohHqs", A=1, B=1, C=1, e=1, m=1),
        {"--include": 1, "--exclude": 1, "--exclude-dir": 1},
        exact=frozenset({"--color=never"}),
    ),
    "rg": _Spec(
        _flags("FisSwxvclLnNouU", e=1, f=1, A=1, B=1, C=1, g=1, t=1, T=1, m=1),
        {
            "--type": 1, "--max-depth": 1, "--hidden": 0, "--no-ignore": 0, "--files": 0,
            "--files-with-matches": 0, "--count": 0, "--json": 0, "--no-config": 0,
        },
        required="--no-config",
    ),
    "diff": _Spec(
        _flags("ucqrNwbBis", U=1), {"--brief": 0}, long_optional=frozenset({"--unified"})
    ),
    "cmp": _Spec(_flags("ls"), {}),
    "jq": _Spec(_flags("rceSnsM"), {"--arg": 2, "--argjson": 2}),
    "sed": _Spec(_flags("nEr", e=1), {}),
}
_FIND_TESTS_1 = frozenset(
    {"-name", "-iname", "-path", "-ipath", "-regex", "-type", "-maxdepth", "-mindepth",
     "-newer", "-mtime", "-mmin", "-size", "-perm"}
)
_FIND_FLAGS_0 = frozenset(
    {"-empty", "-prune", "-print", "-print0", "-not", "!", "-o", "-a", "(", ")"}
)

READ_ONLY_PROGRAMS: tuple[str, ...] = tuple(sorted([*_SPECS, "find"]))


def _parse_args(spec: _Spec, args: list[str], *, posix: bool = False):
    """(options, operands) or None when an option is outside the table or
    lacks its argument. options is a list of (name, values). posix stops the
    options at the first operand (getopt, what macOS runs): every later word
    is an operand as written, and a `--` before it is recorded as an option
    named `--`."""
    opts: list[tuple[str, tuple[str, ...]]] = []
    operands: list[str] = []
    i = 0
    n = len(args)
    while i < n:
        a = args[i]
        if a == "--":
            if posix:
                opts.append(("--", ()))
            operands.extend(args[i + 1:])
            break
        if a.startswith("--"):
            if a in spec.exact:
                opts.append((a, ()))
                i += 1
                continue
            name, eq, val = a.partition("=")
            if name in spec.long_optional:
                opts.append((name, (val,) if eq else ()))
                i += 1
                continue
            arity = spec.long.get(name)
            if arity is None:
                if spec.lenient:
                    operands.append(a)
                    i += 1
                    continue
                return None
            if eq:
                if arity != 1:
                    return None
                opts.append((name, (val,)))
                i += 1
                continue
            values = args[i + 1:i + 1 + arity]
            if len(values) < arity:
                return None
            opts.append((name, tuple(values)))
            i += 1 + arity
            continue
        if a.startswith("-") and len(a) > 1:
            if spec.numeric and a[1:].isdigit():
                opts.append(("-NUM", (a[1:],)))
                i += 1
                continue
            consumed = 0
            bad = False
            j = 1
            while j < len(a):
                arity = spec.short.get(a[j])
                if arity is None:
                    bad = True
                    break
                if arity == 0:
                    j += 1
                    continue
                rest = a[j + 1:]
                need = arity - 1 if rest else arity
                values = args[i + 1:i + 1 + need]
                if len(values) < need:
                    return None
                opts.append(("-" + a[j], ((rest,) if rest else ()) + tuple(values)))
                consumed = need
                break
            if bad:
                if spec.lenient:
                    operands.append(a)
                    i += 1
                    continue
                return None
            i += 1 + consumed
            continue
        if posix:
            operands.extend(args[i:])
            break
        operands.append(a)
        i += 1
    return opts, operands


def _find_ok(args: list[str]) -> bool:
    i = 0
    n = len(args)
    while i < n and not (args[i].startswith("-") or args[i] in ("!", "(", ")")):
        i += 1
    while i < n:
        word = args[i]
        if word in _FIND_FLAGS_0:
            i += 1
        elif word in _FIND_TESTS_1:
            if i + 1 >= n:
                return False
            i += 2
        else:
            return False
    return True


def _sed_ok(args: list[str]) -> bool:
    parsed = _parse_args(_SPECS["sed"], args)
    if parsed is None:
        return False
    opts, operands = parsed
    scripts = [values[-1] if values else "" for name, values in opts if name == "-e"]
    if not scripts:
        if not operands:
            return False
        scripts = [operands[0]]
    return all(sed_script_ok(script) for script in scripts)


def _sed_regex_end(script: str, i: int, delim: str, mode: int) -> int:
    """Index just past the closing delimiter of a regex or replacement that
    starts at i, or -1. mode 0: backslash escapes everywhere, no bracket
    expressions; mode 1 and 2 honor bracket expressions, with a backslash
    inside one escaping (1) or literal (2)."""
    n = len(script)
    while i < n:
        c = script[i]
        if c == "\\":
            i += 2
            continue
        if c == delim:
            return i + 1
        if mode and c == "[":
            i += 1
            if i < n and script[i] == "^":
                i += 1
            if i < n and script[i] == "]":
                i += 1
            while i < n and script[i] != "]":
                if script[i] == "[" and i + 1 < n and script[i + 1] in ":.=":
                    close = script.find(script[i + 1] + "]", i + 2)
                    if close < 0:
                        return -1
                    i = close + 2
                    continue
                if mode == 1 and script[i] == "\\":
                    i += 1
                i += 1
            if i >= n:
                return -1
            i += 1
            continue
        i += 1
    return -1


def _sed_address(script: str, i: int, mode: int, second: bool) -> int:
    """Index after one address starting at i (i itself when there is none),
    or -1 when it is malformed."""
    n = len(script)
    if i >= n:
        return i
    c = script[i]
    if c.isdigit():
        while i < n and script[i].isdigit():
            i += 1
        if not second and i < n and script[i] == "~":
            i += 1
            if i >= n or not script[i].isdigit():
                return -1
            while i < n and script[i].isdigit():
                i += 1
        return i
    if c == "$":
        return i + 1
    if second and c in "+~":
        i += 1
        if i >= n or not script[i].isdigit():
            return -1
        while i < n and script[i].isdigit():
            i += 1
        return i
    if c == "/" or c == "\\":
        if c == "\\":
            if i + 1 >= n or script[i + 1] in "\\\n":
                return -1
            delim = script[i + 1]
            start = i + 2
        else:
            delim = "/"
            start = i + 1
        end = _sed_regex_end(script, start, delim, mode)
        if end < 0:
            return -1
        if end < n and script[end] == "I":
            end += 1
        return end
    return i


def _sed_parse(script: str, mode: int):
    """The (offset, command, flags) list of a script, or None when it is not
    inside the closed sed grammar."""
    n = len(script)
    i = 0
    out: list[tuple[int, str, str]] = []
    while True:
        while i < n and script[i] in " \t\n;":
            i += 1
        if i >= n:
            return out
        start = i
        j = _sed_address(script, i, mode, False)
        if j < 0:
            return None
        if j > i:
            i = j
            if i < n and script[i] == ",":
                k = _sed_address(script, i + 1, mode, True)
                if k < 0 or k == i + 1:
                    return None
                i = k
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] == "!":
            i += 1
            while i < n and script[i] in " \t":
                i += 1
        if i >= n:
            return None
        cmd = script[i]
        i += 1
        flags = ""
        if cmd in "pdn=N":
            pass
        elif cmd == "q":
            while i < n and script[i].isdigit():
                flags += script[i]
                i += 1
        elif cmd == "s":
            if i >= n or script[i] in "\\\n":
                return None
            delim = script[i]
            end = _sed_regex_end(script, i + 1, delim, mode)
            if end < 0:
                return None
            end = _sed_regex_end(script, end, delim, 0)
            if end < 0:
                return None
            i = end
            while i < n and script[i] in "gpIi0123456789":
                flags += script[i]
                i += 1
        else:
            return None
        while i < n and script[i] in " \t":
            i += 1
        if i < n and script[i] not in ";\n":
            return None
        out.append((start, cmd, flags))


def sed_script_ok(script: str) -> bool:
    """A script of addresses plus p d q = n N and s (flags g p I i and
    digits). A script whose structure depends on how a sed treats a
    delimiter inside a bracket expression is rejected: it must parse the
    same under every reading."""
    readings = [_sed_parse(script, mode) for mode in (0, 1, 2)]
    return readings[0] is not None and readings[0] == readings[1] == readings[2]


def _redirects_read_only(segment: Segment) -> bool:
    for r in segment.redirects:
        if r.glued_before:
            return False
        if r.op == ">&" and r.fd == "2" and r.target == "1":
            continue
        if r.target == "/dev/null" and (
            (r.op in (">", ">>") and r.fd in (None, "1", "2")) or (r.op == "&>" and r.fd is None)
        ):
            continue
        return False
    return True


def _segment_read_only(segment: Segment) -> bool:
    if not segment.words or not _redirects_read_only(segment):
        return False
    program = segment.words[0].text
    args = [w.text for w in segment.words[1:]]
    if program == "find":
        return _find_ok(args)
    spec = _SPECS.get(program)
    if spec is None:
        return False
    if program == "sed":
        return _sed_ok(args)
    parsed = _parse_args(spec, args)
    if parsed is None:
        return False
    opts, operands = parsed
    if spec.max_operands is not None and len(operands) > spec.max_operands:
        return False
    if spec.required is not None and not any(name == spec.required for name, _ in opts):
        return False
    return True


def cd_operand(segment: Segment) -> str | None:
    """DIR of a segment that is exactly `cd DIR` with one plain word that
    does not start with `-`; None for every other form (L3)."""
    if (
        len(segment.words) == 2
        and not segment.redirects
        and segment.words[0].text == "cd"
        and segment.words[1].text
        and not segment.words[1].text.startswith("-")
    ):
        return segment.words[1].text
    return None


def is_read_only(lexed: Lexed) -> bool:
    """The read-only grammar recognizes no write in the recorded command: the
    command is placed and every segment is read-only, after at most one
    leading `cd DIR &&`."""
    if not lexed.placed or not lexed.segments:
        return False
    segments = list(lexed.segments)
    if cd_operand(segments[0]) is not None:
        if segments[0].sep_after != "&&" or len(segments) == 1:
            return False
        segments = segments[1:]
    return all(_segment_read_only(seg) for seg in segments)


@dataclass(frozen=True)
class WriteTarget:
    path: str | None
    repo: str | None
    kind: str
    truncated: bool = False


@dataclass(frozen=True)
class WriteMatch:
    form: str
    targets: tuple[WriteTarget, ...]


_PATH_LIMIT = 1024
_SUFFIX_RE = re.compile(r"[A-Za-z0-9._~-]+\Z")
_TOUCH_SPEC = _Spec(_flags("cam", t=1, d=1, r=1), {})


def normalize_path(path: str) -> str:
    """The lexical spelling of a path (D7): `os.path.normpath`, with the
    exactly-two leading slashes it keeps collapsed to one."""
    path = os.path.normpath(path)
    if path.startswith("//"):
        return "/" + path.lstrip("/")
    return path


_normalize_base = functools.lru_cache(maxsize=512)(normalize_path)


def _base(cwd: str | None) -> str | None:
    """A call cwd is a candidate base only when it is absolute (L9). The few
    distinct cwds of a run are normalized once each."""
    if isinstance(cwd, str) and cwd.startswith("/"):
        return _normalize_base(cwd)
    return None


def resolve_target(base: str | None, target: str) -> tuple[str | None, bool] | None:
    """(path, truncated) for a target, or None when the text cannot place it:
    an empty target, or a relative one with no absolute base (D7)."""
    if not target:
        return None
    if target.startswith("/"):
        path = normalize_path(target)
    elif base is not None:
        path = normalize_path(os.path.join(base, target))
    else:
        return None
    if len(path) > _PATH_LIMIT:
        return None, True
    return path, False


def _targets(base: str | None, entries: list[tuple[str, str]]) -> tuple[WriteTarget, ...] | None:
    out: list[WriteTarget] = []
    for kind, target in entries:
        resolved = resolve_target(base, target)
        if resolved is None:
            return None
        out.append(WriteTarget(resolved[0], base, kind, resolved[1]))
    return tuple(out)


def _plain_segment(segment: Segment) -> Segment:
    return Segment(segment.words, (), segment.sep_after)


def _write_redirect(seg: Segment, base: str | None) -> WriteMatch | None:
    if len(seg.redirects) != 1 or not seg.words:
        return None
    r = seg.redirects[0]
    if (
        r.op not in (">", ">>")
        or r.fd not in (None, "1")
        or r.glued_before
        or r.after_words != len(seg.words)
        or not r.target
    ):
        return None
    if not _segment_read_only(_plain_segment(seg)):
        return None
    targets = _targets(base, [("redirect", r.target)])
    return WriteMatch("redirect", targets) if targets else None


def _write_tee(segments: tuple[Segment, ...], base: str | None) -> WriteMatch | None:
    if len(segments) < 2 or any(seg.sep_after != "|" for seg in segments[:-1]):
        return None
    last = segments[-1]
    if last.redirects:
        return None
    words = [w.text for w in last.words]
    if words[:1] != ["tee"]:
        return None
    rest = words[1:]
    if rest[:1] == ["-a"]:
        rest = rest[1:]
    if len(rest) != 1 or rest[0].startswith("-"):
        return None
    if not all(_segment_read_only(seg) for seg in segments[:-1]):
        return None
    targets = _targets(base, [("tee", rest[0])])
    return WriteMatch("tee", targets) if targets else None


def _write_sed_i(seg: Segment, base: str | None) -> WriteMatch | None:
    if seg.redirects:
        return None
    args = [w.text for w in seg.words[1:]]
    if not args:
        return None
    first = args[0]
    suffix = ""
    if first == "-i":
        rest = args[1:]
        if len(rest) == 3:
            suffix = rest[0]
            if suffix and not (suffix.startswith(".") and _SUFFIX_RE.match(suffix)):
                return None
            rest = rest[1:]
        elif len(rest) != 2:
            return None
    elif first.startswith("-i") and not first.startswith("--"):
        suffix = first[2:]
        if not _SUFFIX_RE.match(suffix):
            return None
        rest = args[1:]
        if len(rest) != 2:
            return None
    else:
        return None
    script, target = rest
    if script.startswith("-") or target.startswith("-") or not sed_script_ok(script):
        return None
    entries = [("sed_i", target)]
    resolved = resolve_target(base, target)
    if resolved is None:
        return None
    targets = list(_targets(base, entries) or ())
    if not targets:
        return None
    if suffix:
        path, truncated = resolved
        backup = None if path is None else path + suffix
        if backup is not None and len(backup) > _PATH_LIMIT:
            backup, truncated = None, True
        targets.append(WriteTarget(backup, base, "sed_backup", truncated))
    return WriteMatch("sed_i", tuple(targets))


def _write_touch(seg: Segment, base: str | None) -> WriteMatch | None:
    if seg.redirects:
        return None
    parsed = _parse_args(_TOUCH_SPEC, [w.text for w in seg.words[1:]], posix=True)
    if parsed is None:
        return None
    opts, operands = parsed
    # S4: after the first operand every word is a file name on macOS, so an
    # option-looking one would make the claimed target set incomplete.
    if not any(name == "--" for name, _ in opts) and any(
        word.startswith("-") and len(word) > 1 for word in operands[1:]
    ):
        return None
    if sum(1 for name, _ in opts if name in ("-t", "-d", "-r")) > 1 or not operands:
        return None
    targets = _targets(base, [("touch", operand) for operand in operands])
    return WriteMatch("touch", targets) if targets else None


def _write_cat_heredoc(seg: Segment, base: str | None) -> WriteMatch | None:
    if [w.text for w in seg.words] != ["cat"] or len(seg.redirects) != 2:
        return None
    out, doc = seg.redirects
    if (
        out.op != ">" or out.fd is not None or out.glued_before or out.after_words != 1
        or not out.target
        or doc.op not in ("<<", "<<-") or doc.fd is not None or doc.body is None
    ):
        return None
    targets = _targets(base, [("heredoc", out.target)])
    return WriteMatch("heredoc", targets) if targets else None


def match_write_grammar(lexed: Lexed, cwd: str | None) -> WriteMatch | None:
    """The tiny write grammar (C-12): one of five exact forms, the producer
    proven read-only, every path named resolved lexically. cp and mv are
    never matched. None means the command is not in the grammar."""
    if not lexed.placed or not lexed.segments or lexed.trailing_op is not None:
        return None
    base = _base(cwd)
    segments = lexed.segments
    if len(segments) >= 2:
        return _write_tee(segments, base)
    seg = segments[0]
    if not seg.words:
        return None
    program = seg.words[0].text
    if program == "sed":
        return _write_sed_i(seg, base) or _write_redirect(seg, base)
    if program == "touch":
        return _write_touch(seg, base)
    if program == "cat" and len(seg.redirects) == 2:
        return _write_cat_heredoc(seg, base)
    return _write_redirect(seg, base)


@dataclass(frozen=True)
class GitPrefix:
    subcommand: str
    args: tuple[str, ...]
    c_dirs: tuple[str, ...]
    pins_location: bool


def segment_bases(lexed: Lexed, cwd: str | None) -> tuple[str | None, ...]:
    """The candidate base directory in force when each segment starts (L9).
    The base is the call cwd when absolute; a leading `cd DIR &&` with an
    absolute plain DIR replaces it for the later segments; every other cd
    shape (relative, listed forms, joined by something other than `&&`, a
    later cd, pushd, popd) makes it unknown from then on. An opaque command
    has no base at all."""
    if not lexed.placed:
        return (None,) * len(lexed.segments)
    base = _base(cwd)
    out: list[str | None] = []
    for index, seg in enumerate(lexed.segments):
        out.append(base)
        if not seg.words or seg.words[0].text not in ("cd", "pushd", "popd"):
            continue
        operand = cd_operand(seg)
        if (
            index == 0
            and operand is not None
            and seg.sep_after == "&&"
            and operand.startswith("/")
        ):
            base = normalize_path(operand)
        else:
            base = None
    return tuple(out)


def git_prefix(words: list[str]) -> GitPrefix | None:
    """The global options before a git subcommand, when they are only `-C
    DIR` (any number, in order), `-c K=V`, `--git-dir=...`, and
    `--work-tree=...`; None for any other option or a missing subcommand."""
    if not words or words[0] != "git":
        return None
    c_dirs: list[str] = []
    pins = False
    i = 1
    while i < len(words):
        word = words[i]
        if word == "-C":
            if i + 1 >= len(words):
                return None
            c_dirs.append(words[i + 1])
            i += 2
        elif word == "-c":
            if i + 1 >= len(words):
                return None
            i += 2
        elif word.startswith(("--git-dir=", "--work-tree=")):
            pins = True
            i += 1
        elif word.startswith("-"):
            return None
        else:
            return GitPrefix(word, tuple(words[i + 1:]), tuple(c_dirs), pins)
    return None


def git_repo(base: str | None, prefix: GitPrefix) -> str | None:
    """The candidate repo of a git segment: the base with each -C applied in
    order; None when a relative -C meets an unknown base, or when
    --git-dir or --work-tree is present (L9)."""
    if prefix.pins_location:
        return None
    repo = base
    for directory in prefix.c_dirs:
        if not directory:
            continue
        if directory.startswith("/"):
            repo = normalize_path(directory)
        elif repo is not None:
            repo = normalize_path(os.path.join(repo, directory))
    return repo


@dataclass(frozen=True)
class CommitSegment:
    repo: str | None


@dataclass(frozen=True)
class CommitFacts:
    segments: tuple[CommitSegment, ...]
    # False when the segments come from a command the lexer does not
    # place as a whole (judged segment by segment, DESIGN 4.13.1). Their repo is unknown, and
    # the ledger keeps the call's own candidate repository for such a call.
    placed: bool = True

    @property
    def is_commit(self) -> bool:
        return bool(self.segments)


# A1: the only call completions whose result can give a commit id.
COMMIT_COMPLETIONS = ("completed_ok", "completed_error")
_COMMIT_ID_RE = re.compile(r"(?m)^\[[^\]\s]+ ([0-9a-f]{7,40})\]")
_MAX_COMMIT_IDS_PER_CALL = 20
_REJECTED_COMMIT_LONG = ("--dry-run", "--quiet", "--porcelain", "--short", "--long")
# FA6: the same set without the quiet form, for commit_command_ran, which still
# excludes a dry run but recognizes a quiet commit.
_DRY_RUN_COMMIT_LONG = ("--dry-run", "--porcelain", "--short", "--long")
_OPTION_CLUSTER_RE = re.compile(r"-[A-Za-z]+\Z")
_SHELL_WRAPPERS = ("bash", "sh")


def _commit_flags_ok(args: tuple[str, ...], *, allow_quiet: bool = False) -> bool:
    """False for a commit segment with a dry-run or quiet form: --dry-run,
    -q, --quiet, --porcelain, --short, --long, -z, an unambiguous
    abbreviation of a long one, or a short cluster containing q or z. Words
    after `--` are pathspecs. With allow_quiet (FA6's commit_command_ran) the
    quiet forms (-q, --quiet, a cluster with q) are accepted and every dry-run
    form is still rejected."""
    long_forms = _DRY_RUN_COMMIT_LONG if allow_quiet else _REJECTED_COMMIT_LONG
    rejected_short = "z" if allow_quiet else "qz"
    for word in args:
        if word == "--":
            break
        if word.startswith("--"):
            name = word.split("=", 1)[0]
            if len(name) >= 3 and any(r.startswith(name) for r in long_forms):
                return False
        elif _OPTION_CLUSTER_RE.match(word) and any(c in word for c in rejected_short):
            return False
    return True


def _backgrounded_lists(lexed: Lexed) -> list[bool]:
    """For each segment, whether the and-or list that contains it is ended by
    `&` (shell semantics: `&` applies to the whole `&&`/`||` list, pipelines
    included). A list ends at `;`, a newline, `&`, or the end of the command
    (A1)."""
    segments = lexed.segments
    flags = [False] * len(segments)
    start = 0
    for index, seg in enumerate(segments):
        last = index == len(segments) - 1
        if not (last or seg.sep_after in (";", "\n", "&")):
            continue
        background = seg.sep_after == "&" or (last and lexed.trailing_op == "&")
        for j in range(start, index + 1):
            flags[j] = background
        start = index + 1
    return flags


def _commit_segments(
    lexed: Lexed, cwd: str | None, depth: int
) -> list[tuple[CommitSegment, bool]] | None:
    """The commit segments of one placed command, each with whether it is
    backgrounded in the text (A1), or None when the call can count no id:
    the text is not placed, a wrapper is deeper than one level or holds text
    that is not placed, or a commit segment carries a dry-run or quiet flag
    (poisoning: such a call counts no id at all)."""
    # Every segment is judged on its own, the way FA6 judges presence,
    # so an assignment or an expansion in another segment no longer hides a literal commit. A
    # command the lexer does not place is judged only at the top level (a wrapper's STRING must
    # be placed). Any word that assigns a git variable makes the call count no id: git reads its
    # repository, work tree and index from those.
    if not lexed.placed and depth >= 1:
        return None
    if any(_GIT_ASSIGN_RE.match(word.text) for seg in lexed.segments for word in seg.words):
        return None
    own_reasons: dict[int, set[str]] = {}
    for reason, index in lexed.reason_at:
        own_reasons.setdefault(index, set()).add(reason)
    found: list[tuple[CommitSegment, bool]] = []
    background = _backgrounded_lists(lexed)
    for index, (seg, base, bg) in enumerate(
        zip(lexed.segments, segment_bases(lexed, cwd), background)
    ):
        reasons = own_reasons.get(index, set())
        if _heredoc_runs_a_commit_spelling(seg):
            return None
        spells = _COMMIT_SPELLING_RE.search(" ".join(w.raw for w in seg.words)) is not None
        first = seg.words[0].raw if seg.words else ""
        if reasons - {"first_word"}:
            # Not literal: only an ordinary program's arguments or redirects may be opaque, and
            # no substitution in them may spell `commit` (`echo "$(git commit
            # -q)"` runs a commit whatever program the substitution is an argument of).
            if _substitution_spells_commit(seg, reasons) or (
                spells and not _ordinary_program(first)
            ):
                return None
            continue
        words = _run_as_is_command(seg.words)
        if words is None:
            # Literal assignments alone run nothing (a value with `$` is opaque above); a prefix
            # that takes an option or has no `--` may hide a commit.
            if spells and not all(_ASSIGN_RE.match(word.raw) for word in seg.words):
                return None
            continue
        if not _is_commit_or_wrapper(words):
            # An opaque first word (eval, time, ...), also one a prefix exposed
            # (`mise exec -- time git commit -q`), may hide a commit no id can be matched to.
            if spells and words[0] in _FIRST_WORD_OPAQUE:
                return None
            continue
        if (
            len(words) == 3
            and not seg.redirects
            and words[0] in _SHELL_WRAPPERS
            and words[1] in ("-c", "-lc")
        ):
            if depth >= 1:
                return None
            inner = _commit_segments(lex(words[2], commit_message_form=True), base, depth + 1)
            if inner is None:
                return None
            found.extend((commit, bg or inner_bg) for commit, inner_bg in inner)
            continue
        if words[:1] != ["git"]:
            continue
        prefix = git_prefix(words)
        if prefix is None or prefix.subcommand != "commit":
            continue
        if not _commit_flags_ok(prefix.args):
            return None
        found.append((CommitSegment(git_repo(base, prefix)), bg))
    return found


# An assignment word that names a git variable (GIT_DIR, GIT_WORK_TREE,
# GIT_INDEX_FILE, ...), as a prefix, a segment of its own, or an export/env argument, matched on
# the word with its quotes removed (`export "GIT_DIR=..."`).
_GIT_ASSIGN_RE = re.compile(r"GIT_[A-Za-z0-9_]*(\[[^\]]*\])?\+?=")


_SUBSTITUTION_REASONS = frozenset({"command_substitution", "process_substitution", "paren"})


def _opens_substitution(raw: str) -> bool:
    """Whether a word as written opens a substitution: `$(` or a backtick outside single quotes
    and not escaped (double quotes do not stop either), `<(` or `>(` outside any quotes. Text in
    single quotes is data. An ANSI-C quote `$'...'` counts as an opener: the
    lexer does not model it (it ends `$'can\\'t'` at the escaped quote, where bash does not), so
    nothing after it can be judged."""
    single = double = False
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if single:
            single = ch != "'"
        elif ch == "\\":
            i += 2
            continue
        elif ch == "$" and raw[i + 1:i + 2] == "'" and not double:
            return True
        elif ch == "'" and not double:
            single = True
        elif ch == '"':
            double = not double
        elif ch == "`" or (ch == "$" and raw[i + 1:i + 2] == "("):
            return True
        elif ch in "<>" and raw[i + 1:i + 2] == "(" and not double:
            return True
        i += 1
    return False


def _heredoc_runs_a_commit_spelling(seg: Segment) -> bool:
    """Whether a heredoc of this segment with an unquoted delimiter has a `$(` or a backtick in
    its body (quotes there are plain characters, so they stop neither) and spells `commit`.
    Checked on every segment: the lexer gives the body's reason to the segment it is building
    when the body is read, which is the next one in `cat <<EOF | cat`."""
    for redirect in seg.redirects:
        body = redirect.body
        if body is not None and not redirect.target_quoted and ("$(" in body or "`" in body):
            if _COMMIT_SPELLING_RE.search(body) is not None:
                return True
    return False


def _substitution_spells_commit(seg: Segment, reasons: set[str]) -> bool:
    """Whether a segment the lexer raised a reason for holds a substitution in its words or
    redirect targets (an opener there, or a reason the lexer raised for one whose opener it
    drops, such as `<(`) and that text spells `commit`. A target is kept with its quotes
    removed, so a `$(` in it counts even when it was single-quoted (counting no id is the
    cautious side)."""
    targets = [r.target for r in seg.redirects if r.target is not None]
    opened = (
        bool(reasons & _SUBSTITUTION_REASONS)
        or any(_opens_substitution(word.raw) for word in seg.words)
        or any("$(" in target or "`" in target for target in targets)
    )
    text = "\n".join([word.raw for word in seg.words] + targets)
    return opened and _COMMIT_SPELLING_RE.search(text) is not None


_PLAIN_PROGRAM_RE = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.+-]*(/[A-Za-z0-9_.+-]+)*/?|/[A-Za-z0-9_./+-]+")


def _ordinary_program(word: str) -> bool:
    """Whether a segment's first word is a literal program name whose arguments cannot make it a
    git commit A1 reads: not git, not a shell wrapper, not an opaque word, not a run-as-is
    prefix, and free of quotes, expansions and assignments. Its arguments and redirects may then
    be opaque without hiding a commit (a script that commits is outside the grammar, as for a
    placed command)."""
    if _PLAIN_PROGRAM_RE.fullmatch(word) is None:
        return False
    name = word.rsplit("/", 1)[-1]
    return name not in ("git", "mise", "env", *_SHELL_WRAPPERS) and name not in _FIRST_WORD_OPAQUE


def _is_commit_or_wrapper(words: list[str]) -> bool:
    """Whether a segment's words (after a run-as-is prefix) are a git commit segment or the
    `bash -c STRING` wrapper A1 searches: the two shapes an unplaced segment with a
    first-word reason may still be judged as."""
    if len(words) == 3 and words[0] in _SHELL_WRAPPERS and words[1] in ("-c", "-lc"):
        return True
    prefix = git_prefix(words)
    return prefix is not None and prefix.subcommand == "commit"


def commit_command_ran(words: list[str] | tuple[str, ...]) -> bool:
    """FA6, the grammar fact (DESIGN 4.13.1): whether one segment's words are a
    `git commit` command, quiet forms included (-q, --quiet, a short cluster with
    q) and every dry-run form (--dry-run, --porcelain, --short, --long, -z, their
    abbreviations) excluded. A1's id grammar rejects the quiet forms because a
    quiet segment poisons the call's id count; this fact says only that the
    command is present, never that the shell reached it or that it printed an id.
    Segment-level: placement and backgrounding are judged by commit_ran_in_call."""
    prefix = git_prefix(list(words))
    return (
        prefix is not None
        and prefix.subcommand == "commit"
        and _commit_flags_ok(prefix.args, allow_quiet=True)
    )


def _run_as_is_command(words) -> list[str] | None:
    """FA6 (COMMITS 0 for an agent that committed): a segment's words
    after the prefixes that run the command after them unchanged, in the same directory:
    leading NAME=VALUE words, `env` followed by NAME=VALUE words only, and `mise exec|x
    [tool@version ...] --`. None when such a prefix takes an option (`env -C DIR`, `mise exec
    -C DIR` could change the directory), `mise exec` has no `--`, or nothing follows. Words
    with no such prefix come back unchanged, so an opaque first word (`eval`, `time`) still
    reads as no commit. Used for the presence fact only; A1's id grammar keeps such a segment
    unplaced, since an assignment can name another repository (GIT_DIR=...)."""
    rest = list(words)
    i, n = 0, len(rest)
    # NAME=VALUE is an assignment only where a shell or env reads one: at the start and after
    # `env`. mise runs the word after `--` as the command itself.
    assignments = True
    while i < n:
        word = rest[i]
        if assignments and "=" in word.raw and _ASSIGN_RE.match(word.raw):
            i += 1
        elif word.raw == "env":
            i += 1
            assignments = True
            if i < n and rest[i].text.startswith("-"):
                return None
        elif word.raw == "mise" and i + 1 < n and rest[i + 1].raw in ("exec", "x"):
            j = i + 2
            while j < n and rest[j].raw != "--":
                if rest[j].text.startswith("-"):
                    return None
                j += 1
            if j >= n:
                return None
            i = j + 1
            assignments = False
        else:
            break
    if i >= n:
        return None
    return [word.text for word in rest[i:]]


def _commit_ran_in_lexed(lexed: Lexed, depth: int) -> bool:
    """Whether some foreground commit segment of one lexed command is a
    commit_command_ran segment whose OWN reasons are empty. A reason raised in
    another segment (an expansion in an earlier `cd $WT`) does not hide it;
    any reason in the candidate itself (opaque or malformed syntax, quoting,
    heredoc data) fails closed, as does an `&` ending its and-or list."""
    own_reasons: dict[int, set[str]] = {}
    for reason, index in lexed.reason_at:
        own_reasons.setdefault(index, set()).add(reason)
    background = _backgrounded_lists(lexed)
    for index, (seg, bg) in enumerate(zip(lexed.segments, background)):
        if bg or own_reasons.get(index, set()) - {"first_word"}:
            continue
        words = _run_as_is_command(seg.words)
        if words is None:
            continue
        if (
            len(words) == 3
            and not seg.redirects
            and words[0] in _SHELL_WRAPPERS
            and words[1] in ("-c", "-lc")
        ):
            inner = lex(words[2], commit_message_form=True) if depth < 1 else None
            if inner is not None and _commit_ran_in_lexed(inner, depth + 1):
                return True
            continue
        if commit_command_ran(words):
            return True
    return False


def commit_ran_in_call(command: str) -> bool:
    """FA6: whether a Bash command text contains a foreground commit_command_ran
    segment (the call-level presence fact; the caller adds the completion and no-id
    conditions). Reachability is not judged: control flow may skip the segment."""
    if not isinstance(command, str) or _COMMIT_SPELLING_RE.search(command) is None:
        return False
    return _commit_ran_in_lexed(lex(command, commit_message_form=True), 0)


# A placed word is its pieces with quotes and backslashes removed, so a word
# `commit` always shows up as the substring `commit` once those characters
# (and a line continuation's newline) are stripped: a sound prefilter that
# only skips commands that cannot hold a commit segment.
_COMMIT_SPELLING_RE = re.compile(
    "c['\"\\\\\n]*o['\"\\\\\n]*m['\"\\\\\n]*m['\"\\\\\n]*i['\"\\\\\n]*t"
)


def commit_facts(command: str, cwd: str | None = None) -> CommitFacts:
    """The commit segments of a command (L8, amended by A1). Empty unless the
    command is placed by the lexer, no commit segment carries a dry-run or
    quiet flag, and no commit segment is backgrounded in the text."""
    if not isinstance(command, str) or _COMMIT_SPELLING_RE.search(command) is None:
        return CommitFacts(())
    lexed = lex(command, commit_message_form=True)
    found = _commit_segments(lexed, cwd, 0)
    if found is None or any(background for _, background in found):
        return CommitFacts(())
    if not lexed.placed:
        # no segment of such a command has a known repository, not even behind an absolute -C
        # or a cd inside a wrapper
        return CommitFacts(tuple(CommitSegment(None) for _ in found), False)
    return CommitFacts(tuple(commit for commit, _ in found))


def commit_ids(facts: CommitFacts, result_text: str) -> tuple[str, ...]:
    """The last k line-start summary matches of a result, for k commit
    segments: an unverified, summary-shaped output line, not attributed to
    git (DESIGN 4.13.1). Empty when the result has fewer matches than k.
    The caller guarantees the call's completion is in COMMIT_COMPLETIONS."""
    k = len(facts.segments)
    if k == 0:
        return ()
    matches = _COMMIT_ID_RE.findall(result_text)
    if len(matches) < k:
        return ()
    return tuple(matches[-k:][:_MAX_COMMIT_IDS_PER_CALL])


DELEGATION_TOOLS = ("Agent", "Workflow", "SendMessage", "Task")
_DELEGATION_PROGRAMS = ("codex", "aider")
_NO_COMMIT = CommitFacts(())


@dataclass(frozen=True)
class CallClass:
    """One Bash, Monitor, or delegation-tool call classified into its row of
    docs/DESIGN.md 4.13.1's truth table (L6). effect is None (no entry),
    'possible', or 'claimed_target' (row 5: N4b maps a call that did not
    succeed to 'attempted'). basis is None, 'background_process',
    'delegation', 'command_grammar', or 'opaque_command'. targets are the
    tiny grammar's paths (row 5 only). commit holds the commit segments of
    the call. repo is the call's candidate repository: the base in force at
    its last segment, or the call cwd for a command the lexer does not place.
    Completion is never decided here."""

    row: int
    effect: str | None
    basis: str | None
    targets: tuple[WriteTarget, ...]
    commit: CommitFacts
    repo: str | None
    # FA6 (C1): the call CONTAINS a foreground commit command (a quiet commit
    # included); False for a background call. Presence only: the shell may never have
    # reached it (`git diff --quiet || git commit -q`). Reader-private like ToolCall._path_state:
    # out of equality, repr, and to_dict (the classification's own shape is unchanged).
    commit_command_present: bool = field(default=False, compare=False, repr=False)

    def to_dict(self) -> dict:
        """The private, complete form (ToolCall.to_dict, N4b-1)."""
        return {
            "row": self.row,
            "effect": self.effect,
            "basis": self.basis,
            "targets": [
                {"path": t.path, "repo": t.repo, "kind": t.kind, "truncated": t.truncated}
                for t in self.targets
            ],
            "commit": [{"repo": seg.repo} for seg in self.commit.segments],
            "repo": self.repo,
        }


def _has_nohup(command: str, lexed: Lexed) -> bool:
    if any(w.text == "nohup" for seg in lexed.segments for w in seg.words):
        return True
    structural = "unbalanced_quote" in lexed.reasons or "unclosed_heredoc" in lexed.reasons
    return structural and "nohup" in command.split()


def _is_delegation(lexed: Lexed) -> bool:
    for seg in lexed.segments:
        words = [w.text for w in seg.words]
        if not words:
            continue
        program = words[0].rpartition("/")[2]
        if program in _DELEGATION_PROGRAMS:
            return True
        if program == "claude" and any(w in ("-p", "--print") for w in words[1:]):
            return True
        if "codex-companion.mjs" in "\0".join(words) and any(
            w.endswith("codex-companion.mjs") for w in words
        ):
            return True
    return False


# A transcript repeats the same few commands many times, and the lexer is a
# per-character loop, so the pure classification of a (tool, command, flags,
# cwd) tuple is memoized. CallClass is frozen, so sharing one is safe. Very
# long commands are not cached (they would pin large strings).
_CACHE_COMMAND_LIMIT = 2048


def classify_call(
    tool_name: str | None,
    command: object,
    *,
    run_in_background: object = False,
    backgrounded_hit: bool = False,
    cwd: str | None = None,
) -> CallClass | None:
    """classify_call's contract is _classify_call's below; this wrapper only
    memoizes it."""
    if isinstance(command, str) and len(command) <= _CACHE_COMMAND_LIMIT:
        return _classify_cached(
            tool_name, command, run_in_background is True, backgrounded_hit, cwd
        )
    return _classify_call(
        tool_name, command, run_in_background=run_in_background,
        backgrounded_hit=backgrounded_hit, cwd=cwd,
    )


@functools.lru_cache(maxsize=4096)
def _classify_cached(
    tool_name: str | None, command: str, background: bool, backgrounded_hit: bool,
    cwd: str | None,
) -> CallClass | None:
    return _classify_call(
        tool_name, command, run_in_background=background, backgrounded_hit=backgrounded_hit,
        cwd=cwd,
    )


def _classify_call(
    tool_name: str | None,
    command: object,
    *,
    run_in_background: object = False,
    backgrounded_hit: bool = False,
    cwd: str | None = None,
) -> CallClass | None:
    """Rows 2 to 6 of the truth table for a Bash or Monitor call, and row 3
    for the delegation tools; None for every other tool name (the file
    tools, the read-only tools, and unknown tools belong to the ledger).
    backgrounded_hit says whether an anchored bash_backgrounded marker hit
    belongs to this call's result."""
    if tool_name in DELEGATION_TOOLS:
        return CallClass(3, "possible", "delegation", (), _NO_COMMIT, None)
    if tool_name not in ("Bash", "Monitor"):
        return None
    is_bash = tool_name == "Bash"
    base = _base(cwd)
    background = run_in_background is True or backgrounded_hit
    if not isinstance(command, str):
        if background:
            return CallClass(2, "possible", "background_process", (), _NO_COMMIT, base)
        return CallClass(6, "possible", "opaque_command", (), _NO_COMMIT, base)
    lexed = lex(command)
    commit = commit_facts(command, cwd) if is_bash else _NO_COMMIT
    if lexed.placed and lexed.segments:
        repo = segment_bases(lexed, cwd)[-1]
    else:
        repo = base
    if background or lexed.trailing_op == "&" or _has_nohup(command, lexed):
        return CallClass(2, "possible", "background_process", (), commit, repo)
    present = is_bash and commit_ran_in_call(command)
    if is_bash and _is_delegation(lexed):
        return CallClass(3, "possible", "delegation", (), commit, repo, present)
    if is_read_only(lexed):
        return CallClass(4, None, None, (), commit, repo, present)
    if is_bash:
        match = match_write_grammar(lexed, cwd)
        if match is not None:
            return CallClass(
                5, "claimed_target", "command_grammar", match.targets, commit, repo, present,
            )
    return CallClass(6, "possible", "opaque_command", (), commit, repo, present)


# --- what ends on its own (2026-11-04) ----------------------------------

_DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)([smhd]?)")


def positive_duration(word: str) -> bool:
    match = _DURATION_RE.fullmatch(word)
    return match is not None and float(match.group(1)) > 0


# signals whose default action ends nothing: a timeout sending one needs a kill-after
_NONTERMINATING = frozenset({"CONT", "STOP", "TSTP", "TTIN", "TTOU", "CHLD", "CLD", "URG",
                             "WINCH", "INFO", "PWR"})


def _ends_by_signal(signal_name: str | None, kill_after: str | None) -> bool:
    if kill_after is not None and positive_duration(kill_after):
        return True
    if signal_name is None:
        return True  # TERM
    name = signal_name.upper().removeprefix("SIG")
    return not name.isdigit() and name not in _NONTERMINATING


_TIMEOUT_FLAGS = ("--foreground", "--preserve-status", "--verbose")


def _long_option(name: str, full: str) -> bool:
    """`--sig` is `--signal`: GNU getopt takes an unambiguous prefix."""
    return len(name) >= 4 and full.startswith(name)


def _opens_without_blocking(target: str | None, cwd: str | None) -> bool:
    """Whether opening an output redirect's target cannot block before the command starts: not
    a FIFO or a device (`/dev/null` aside), as far as a stat now shows."""
    if not target:
        return False
    if target == "/dev/null":
        return True
    if target.startswith("/dev/"):
        return False
    path = target if target.startswith("/") else (os.path.join(cwd, target) if cwd else None)
    if path is None:
        return True  # cannot be placed; a FIFO there is the rare case
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return True  # created as a regular file
    except OSError:
        return False
    return _stat.S_ISREG(info.st_mode)


def _redirects_cannot_block(segment: Segment, cwd: str | None) -> bool:
    """Stdin only from /dev/null, output only to files that open without blocking: a FIFO with no
    reader holds the shell before the command even starts."""
    for redirect in segment.redirects:
        if redirect.op in ("<", "<>") and redirect.target != "/dev/null":
            return False
        if redirect.op == ">&" and isinstance(redirect.target, str) \
                and re.fullmatch(r"[0-9]*-?", redirect.target):
            continue  # a descriptor copy or close
        if redirect.op in (">", ">>", ">|", "&>", "&>>", ">&") \
                and not _opens_without_blocking(redirect.target, cwd):
            return False
    return True


def is_bounded(command: str, cwd: str | None = None) -> bool:
    """One command wholly under `timeout`/`gtimeout` with a positive duration and a signal that
    ends it (or a positive kill-after), with nothing that can block before timeout starts: no
    expansion or substitution, stdin only from /dev/null, a heredoc or a literal here-string,
    output only to files that open without blocking, and only options timeout is known to take
    (`timeout 1 true; tail -f x`, `timeout 0 x`, `timeout 1 true "$(sleep
    9)"`, `timeout -s CONT 1 x`, `timeout -sCONT 1 x` and `timeout 1 true > fifo` do not end)."""
    lexed = lex(command)
    if set(lexed.reasons) - {"here_string"} or len(lexed.segments) != 1:
        return False
    segment = lexed.segments[0]
    if not _redirects_cannot_block(segment, cwd):
        return False
    words = [word.text for word in segment.words]
    i = 0
    while i < len(words) and re.match(r"[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    if i >= len(words) or words[i] not in ("timeout", "gtimeout"):
        return False
    i += 1
    signal_name = kill_after = None
    while i < len(words) and words[i].startswith("-") and words[i] != "-":
        option = words[i]
        i += 1
        if option == "--":
            break
        if option.startswith("--"):
            name, eq, value = option.partition("=")
            takes = "signal" if _long_option(name, "--signal") else (
                "kill" if _long_option(name, "--kill-after") else None)
            if takes is None:
                if any(_long_option(name, flag) for flag in _TIMEOUT_FLAGS) and not eq:
                    continue
                return False  # an option timeout is not known to take
            if not eq:
                value = words[i] if i < len(words) else ""
                i += 1
        else:
            letter, value = option[1:2], option[2:]
            if letter == "v" and not value:
                continue
            if letter not in ("s", "k"):
                return False
            takes = "signal" if letter == "s" else "kill"
            if not value:
                value = words[i] if i < len(words) else ""
                i += 1
        if takes == "signal":
            signal_name = value
        else:
            kill_after = value
    return i + 1 < len(words) and positive_duration(words[i]) \
        and _ends_by_signal(signal_name, kill_after)


_SILENT_WAIT_RE = re.compile(r"(?:^|\s)#\s*postmortem:\s*silent-wait\b", re.M)
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
_ALARM_THEN_EXEC_RE = re.compile(r"\s*alarm\s+([1-9]\d*)\s*;\s*exec\s+@ARGV\s*;?\s*")
_ALARM_MAX = 2**31 - 1  # past what perl keeps it wraps, and 2**32 reads as 0: no alarm at all
_ALARM_SHIFT_THEN_EXEC_RE = re.compile(r"\s*alarm\s+shift\s*;\s*exec\s+@ARGV\s*;?\s*")
_NOT_LITERAL_RE = re.compile(r"[$*?\[\]`~]")
_COUNT_RE = re.compile(r"[0-9]+")
_DASH_COUNT_RE = re.compile(r"-[0-9]+")
_VARIABLE_RE = re.compile(r"\$(?:([A-Za-z_][A-Za-z0-9_]*)|\{([A-Za-z_][A-Za-z0-9_]*)\})")
_DATE_ONLY_RE = re.compile(r"date(?:\s+[-+\w%:.]+)*")
_CAT_FILE_RE = re.compile(r"cat\s+(/[-\w./]+)")
_SAYS_AND_ENDS = frozenset({"echo", "printf", "date", "true", ":", "ls", "exit"})
_TOOL_NAMES = frozenset({"agent-postmortem", "postmortem"})
_TOOL_MODULES = frozenset({"agent_postmortem", "agent_postmortem.cli"})
_TOOL_LOOKS = frozenset({"status", "scan", "run", "watch"})


def _is_perl_alarm(words: list[str]) -> bool:
    """Exactly `[caffeinate -flags] perl -e 'alarm N; exec @ARGV' command ...`, N positive, or
    `'alarm shift; exec @ARGV' N command ...`: the forms that prove an alarm is set before what it
    bounds starts. Any other script can leave no alarm (`alarm $t` with `$t` unset)."""
    i = 0
    if words and words[0].rsplit("/", 1)[-1] == "caffeinate":
        i = 1
        while i < len(words) and words[i].startswith("-"):
            i += 1
    if len(words) < i + 4 or words[i].rsplit("/", 1)[-1] != "perl" or words[i + 1] != "-e":
        return False
    script, rest = words[i + 2], words[i + 3:]
    literal = _ALARM_THEN_EXEC_RE.fullmatch(script)
    if literal is not None:
        # an option after the script is perl's: -n and -p read input before the alarm is ever set
        return int(literal.group(1)) <= _ALARM_MAX and not rest[0].startswith("-")
    return bool(_ALARM_SHIFT_THEN_EXEC_RE.fullmatch(script)) and len(rest) > 1 \
        and _COUNT_RE.fullmatch(rest[0]) is not None and 0 < int(rest[0]) <= _ALARM_MAX \
        and not rest[1].startswith("-")


def _expand(word: str, known: dict[str, str]) -> str:
    """`$NAME` and `${NAME}` of a literal assignment earlier in this command; anything else stays
    as written, and so reads as not literal."""
    return _VARIABLE_RE.sub(lambda m: known.get(m.group(1) or m.group(2), m.group(0)), word)


def _file_opens_at_once(word: str, base: "str | None | tuple") -> bool:
    """A file named literally that is a regular one, or not there yet, where the base is known. With
    several bases (a `cd X;` that may have failed), in each of them."""
    if _NOT_LITERAL_RE.search(word) or word.startswith("-") or not word:
        return False
    bases = base if isinstance(base, tuple) else (base,)
    if not word.startswith("/") and (not bases or any(item is None for item in bases)):
        return False
    return all(_opens_without_blocking(word, item) for item in bases)


def _is_quick_step(words: list[str], piped: bool, base: "tuple", known: dict[str, str]) -> bool:
    """A step beside a long run that surely ends at once: a short list by exact shape. A wrong False
    costs one line told. Everything else, however harmless it looks, is False: readers can wait on
    stdin or `/dev/zero`, `sed` and `awk` can loop, `rm` can prompt, `source` can do anything."""
    if not words:
        return True  # only a redirect
    name = words[0]
    if name in _SAYS_AND_ENDS or _ASSIGNMENT_RE.match(name):
        # what they print or assign cannot wait: every substitution was checked before this
        return name in _SAYS_AND_ENDS or all(_ASSIGNMENT_RE.match(word) for word in words)
    if name == "export":
        return all(_ASSIGNMENT_RE.match(word) for word in words[1:])
    words = [_expand(word, known) for word in words]
    if name == "cd":
        return len(words) == 2 and not _NOT_LITERAL_RE.search(words[1])
    if name == "mkdir":
        return len(words) == 3 and words[1] == "-p" and not _NOT_LITERAL_RE.search(words[2])
    if name == "wc" and words[1:2] in (["-l"], ["-c"]):
        args = words[2:]
    elif name in ("tail", "head") and words[1:2] == ["-n"] and len(words) > 2 \
            and _COUNT_RE.fullmatch(words[2]):
        args = words[3:]
    elif name in ("tail", "head") and len(words) > 1 and _DASH_COUNT_RE.fullmatch(words[1]):
        args = words[2:]
    elif name == "cut" and len(words) == 2 and words[1].startswith(("-c", "-f")):
        args = []
    elif name == "tee" and len(words) == 2:
        return piped and _file_opens_at_once(words[1], base)
    else:
        return False
    if not args:
        return piped
    return len(args) == 1 and _file_opens_at_once(args[0], base)


def _redirects_end_at_once(segment: Segment, base: "tuple", known: dict[str, str]) -> bool:
    for redirect in segment.redirects:
        if redirect.op == ">&" and isinstance(redirect.target, str) \
                and re.fullmatch(r"[0-9]*-?", redirect.target):
            continue
        target = _expand(redirect.target, known) if isinstance(redirect.target, str) else None
        if redirect.op == "<" and target == "/dev/null":
            continue
        if redirect.op not in (">", ">>", ">|", "&>", "&>>", ">&") or target is None \
                or not _file_opens_at_once(target, base):
            return False
    return True


def _substitutions_are_only_date(command: str) -> bool:
    """`S=$(date +%s)` and `$(cat /a/regular/file)`, which real wrappers use for a start time and
    a prompt, and nothing else: a substitution runs before anything around it, so one that waits
    holds everything."""
    from .pre_bash import _substitution_end

    i, n = 0, len(command)
    single = double = False
    while i < n:
        ch = command[i]
        if single:
            single = ch != "'"
        elif ch == "\\":
            i += 2
            continue
        elif ch == "'" and not double:
            single = True
        elif ch == '"':
            double = not double
        elif ch == "`" or (ch == "$" and command[i + 1:i + 2] == "("):
            end = _substitution_end(command, i)
            inner = (command[i + 1:end - 1] if ch == "`" else command[i + 2:end - 1]).strip()
            read = _CAT_FILE_RE.fullmatch(inner)
            if not _DATE_ONLY_RE.fullmatch(inner) \
                    and not (read and _file_opens_at_once(read.group(1), None)):
                return False
            i = end
            continue
        i += 1
    return True


def under_its_own_alarm(command: str, cwd: str | None = None) -> bool:
    """A long run under `perl -e 'alarm N; exec @ARGV'`, the same bound as `timeout` where there is
    none (macOS), with only steps around it that surely end at once. One real session
    had 15 such wrappers. This reads the plain ones as bounded and tells the rest once, on purpose:
    a wrong False costs one line, which says it may be fine; a wrong True silences the only alarm
    for a real hang, and a looser rule let through a dozen ways to do that. So:
    every step and redirect by exact shape, with paths literal or from a literal assignment."""
    if "perl" not in command or "alarm" not in command or not _substitutions_are_only_date(command):
        return False
    from .pre_bash import _without_substitutions  # no import at module level: it imports this

    # where the command can be now: a `cd X;` that fails leaves it where it was
    bases: tuple = (cwd if isinstance(cwd, str) and cwd.startswith("/") else None,)
    # a `cd X &&` that fails skips only its own list: the next one runs where it was
    resume: tuple = ()
    known: dict[str, str] = {}
    found = False
    previous = None
    lexed = lex(_without_substitutions(command))
    # what runs in the background outlives the alarm, and a list in it is a subshell
    if lexed.trailing_op == "&" or any(seg.sep_after == "&" for seg in lexed.segments):
        return False
    for segment in lexed.segments:
        words = [word.text for word in segment.words]
        if not _redirects_end_at_once(segment, bases, known):
            return False
        if _is_perl_alarm(words):
            found = True
        elif not _is_quick_step(words, previous in ("|", "|&"), bases, known):
            return False
        if words[:1] == ["cd"]:
            target = _expand(words[1], known) if len(words) == 2 else ""
            moved = tuple(
                normalize_path(target) if target.startswith("/")
                else (normalize_path(os.path.join(item, target)) if item and target else None)
                for item in bases)
            if segment.sep_after == "&&":
                resume = (*resume, *bases)
                bases = moved
            else:
                bases = (*moved, *bases)
        if words[:1] == ["printf"] and any(word.startswith("-v") for word in words[1:]):
            return False  # it sets a variable of this shell
        # only what persists in this shell: `export`, or a segment of nothing but assignments. A
        # prefix or a child's argument changes nothing here
        persists = words[1:] if words[:1] == ["export"] else (
            words if all(_ASSIGNMENT_RE.match(word) for word in words) else [])
        # and only if it surely ran here: not skipped by a `&&` or `||` before it, and not in the
        # subshell a pipe gives it
        surely = previous in (None, ";", "\n") and segment.sep_after not in ("|", "|&")
        for word in persists:
            if not _ASSIGNMENT_RE.match(word):
                continue
            name, _eq, value = word.partition("=")
            if name.endswith("+") or not surely:
                known.pop(name.rstrip("+"), None)  # what it now holds is not worked out here
                continue
            value = _expand(value, known)
            # one that an unquoted `$NAME` would split or glob is not one operand
            if _NOT_LITERAL_RE.search(value) or "SUBSTITUTION" in value \
                    or any(char.isspace() or char in "*?[" for char in value):
                known.pop(name, None)
            else:
                known[name] = value
        if resume and segment.sep_after not in ("&&", "|", "|&"):
            bases, resume = (*bases, *resume), ()
        previous = segment.sep_after
    return found


def _names_a_look(words: list[str]) -> bool:
    head = words[0].rsplit("/", 1)[-1] if words else ""
    if "--help" in words or "-h" in words:
        return False  # it prints usage and looks at nothing
    if head in _TOOL_NAMES:
        return len(words) > 1 and words[1] in _TOOL_LOOKS
    if head in ("uv", "uvx"):
        rest = words[2:] if head == "uv" and words[1:2] == ["run"] else words[1:]
        if head == "uv" and words[1:2] != ["run"]:
            return False
        while rest and rest[0].startswith("-"):
            rest = rest[1:]
        return _names_a_look(rest)
    if head in ("python", "python3") and words[1:2] == ["-m"]:
        return len(words) > 3 and words[2] in _TOOL_MODULES and words[3] in _TOOL_LOOKS
    return False


def runs_this_tool(command: str) -> bool:
    """Whether a segment of this command runs `status`, `scan`, `run` or `watch` of this tool, by
    its command word, directly or through `uv run`, `uvx` or `python -m`: an `echo` or a `grep`
    naming it does not."""
    if "postmortem" not in command:
        return False
    from .pre_bash import _command_words, _without_substitutions

    return any(_names_a_look(_command_words([word.text for word in segment.words]) or [])
               for segment in lex(_without_substitutions(command)).segments)


_CAFFEINATE_FLAGS_RE = re.compile(r"-[dimsu]+")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
_PYTHON_NAME_RE = re.compile(r"python(?:3(?:\.\d+)?)?")
_TIMER_RE = re.compile(r"\s*import\s+time\s*;\s*time\.sleep\(\s*(\d+(?:\.\d+)?)\s*\)\s*;?\s*")


def _without_caffeinate(words: list[str]) -> list[str]:
    """`caffeinate -i sleep 900` keeps the machine awake and waits for its command."""
    if words[:1] and words[0].rsplit("/", 1)[-1] == "caffeinate":
        rest = words[1:]
        while rest and _CAFFEINATE_FLAGS_RE.fullmatch(rest[0]):
            rest = rest[1:]
        return rest
    return words


def leading_sleep_seconds(command: str) -> float | None:
    """How long a command sleeps before anything else, when it starts with one plain `sleep N`: for
    that long its silence is what it says it will do. What comes after is judged as usual."""
    lexed = lex(command)
    if not lexed.segments or lexed.trailing_op not in (None, ";"):
        return None
    first = lexed.segments[0]
    if first.redirects or first.sep_after not in (None, ";", "\n", "&&"):
        return None
    words = _without_caffeinate([word.text for word in first.words])
    if len(words) != 2 or words[0].rsplit("/", 1)[-1] not in ("sleep", "gsleep"):
        return None
    match = _DURATION_RE.fullmatch(words[1])
    seconds = float(match.group(1)) * _DURATION_UNITS[match.group(2)] if match else 0.0
    return seconds if seconds > 0 else None


def _only_waits(words: list[str]) -> bool:
    """`sleep N` by any path, or exactly `python3 -c 'import time; time.sleep(N)'`
    (an agent refused a foreground `sleep` wrote its timer this way). N > 0."""
    words = _without_caffeinate(words)
    name = words[0].rsplit("/", 1)[-1] if words else ""
    if name in ("sleep", "gsleep"):
        return len(words) == 2 and positive_duration(words[1])
    if len(words) == 3 and _PYTHON_NAME_RE.fullmatch(name) and words[1] == "-c":
        script = _TIMER_RE.fullmatch(words[2])
        return script is not None and float(script.group(1)) > 0
    return False


def is_timer(command: str) -> bool:
    """A shell task that only waits, and so arms a deadline: `_only_waits`, then at most one literal
    `echo`, `printf`, `true` or `:` joined by `;` or `&&`, with no redirect, expansion or
    substitution anywhere (`sleep 1; echo "$(tail -f x)"` never ends)."""
    lexed = lex(command)
    segments = lexed.segments
    if not lexed.placed or lexed.trailing_op or not 1 <= len(segments) <= 2:
        return False
    first = [word.text for word in segments[0].words]
    if not _only_waits(first) or segments[0].redirects:
        return False
    if len(segments) == 2:
        second = [word.text for word in segments[1].words]
        if segments[0].sep_after not in (";", "&&", "\n") or segments[1].redirects or not second \
                or second[0].rsplit("/", 1)[-1] not in ("echo", "printf", "true", ":"):
            return False
    return True


_END_ONLY_STAGES = frozenset({"tail", "sort", "tac", "wc"})
_AFTER_THE_PIPE = frozenset({"echo", "printf", "true", ":", "date"})
_TAIL_FLAG_RE = re.compile(
    r"-\d+[bcl]?|-[nc]-?\d+[a-zA-Z]*|--(?:lines|bytes)=-?\d+[a-zA-Z]*|-[qv]|--quiet|--silent|--verbose")


def _tail_holds_its_pipe(words: list[str]) -> bool:
    """`tail` with a count from the end at most: `-f` and a start like `-n +1` print as they go, and
    a file operand leaves the pipe unread."""
    index = 0
    while index < len(words):
        if words[index] in ("-n", "-c"):
            index += 1  # its count
            if index >= len(words) or not re.fullmatch(r"-?\d+[a-zA-Z]*", words[index]):
                return False
        elif not _TAIL_FLAG_RE.fullmatch(words[index]):
            return False
        index += 1
    return True


def _holds_its_pipe(name: str, words: list[str]) -> bool:
    """Whether this stage reads the pipe and holds it to the end: `sort -m` merges as it goes, and a
    file operand or `--files0-from` leaves the pipe unread (2). Flags that take a
    value in the next word read as an operand: a wrong False costs only the wording."""
    if name == "tail":
        return _tail_holds_its_pipe(words)
    if not all(word.startswith("-") and word != "-" and not word.startswith("--files0-from")
               for word in words):
        return False
    return name != "sort" or not any(
        word == "--merge" or (not word.startswith("--") and "m" in word) for word in words)


def _ends_at_once(segment: Segment) -> bool:
    """A report after the pipe, with nothing in it that can wait."""
    return bool(segment.words) and not segment.redirects \
        and segment.words[0].text in _AFTER_THE_PIPE \
        and not any("$(" in word.raw or "`" in word.raw for word in segment.words)


def writes_only_at_its_end(command: str) -> str | None:
    """The last pipe stage's name when it holds everything until its input ends, so the command's
    output says nothing until then (397 of 2696 real background commands
    end in `| tail`). Not quiet by design: `codex exec | tail` hanging on stdin looks the same, so
    such a command is still told once, in softer words. A wrong answer costs only the wording."""
    lexed = lex(command)
    if lexed.trailing_op:
        return None
    segments = list(lexed.segments)
    while len(segments) > 1 and _ends_at_once(segments[-1]) \
            and segments[-2].sep_after in (";", "\n", "&&", "||"):
        segments.pop()  # `... | tail -5; echo "exit $?"` ends a moment after the pipe does
    if len(segments) < 2 or segments[-2].sep_after not in ("|", "|&"):
        return None
    stage = segments[-1]
    words = [word.text for word in stage.words]
    name = words[0].rsplit("/", 1)[-1] if words else ""
    if name not in _END_ONLY_STAGES or stage.redirects:
        return None
    return name if _holds_its_pipe(name, words[1:]) else None


def quiet_by_design(command: str, cwd: str | None = None) -> bool:
    """A background command whose silence is its normal state: wholly
    under a real timeout (`is_bounded`) or a perl alarm, a timer, or carrying the marker `#
    postmortem: silent-wait`. An unbounded wait loop with none of them is still told once."""
    return bool(_SILENT_WAIT_RE.search(command)) or is_timer(command) \
        or under_its_own_alarm(command, cwd) or is_bounded(command, cwd)
