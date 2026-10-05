"""Pure command-text classifier for Bash and Monitor calls (docs/DESIGN.md
4.13.1, N4a). Standard library only: no filesystem access, no subprocess.

The transcript records a command's text, never the shell it ran in, so
nothing here can prove a command did not write. The grammars decide one
syntactic claim only: "no write is recognized in the recorded command".
"""

from __future__ import annotations

import functools
import os
import re
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
    if not lexed.placed:
        return None
    found: list[tuple[CommitSegment, bool]] = []
    background = _backgrounded_lists(lexed)
    for seg, base, bg in zip(lexed.segments, segment_bases(lexed, cwd), background):
        words = [w.text for w in seg.words]
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


def _commit_ran_in_lexed(lexed: Lexed, depth: int) -> bool:
    """Whether some foreground commit segment of one lexed command is a
    commit_command_ran segment whose OWN reasons are empty. A reason raised in
    another segment (an expansion in an earlier `cd $WT`) does not hide it;
    any reason in the candidate itself (opaque or malformed syntax, quoting,
    heredoc data) fails closed, as does an `&` ending its and-or list."""
    flagged = {index for _, index in lexed.reason_at}
    background = _backgrounded_lists(lexed)
    for index, (seg, bg) in enumerate(zip(lexed.segments, background)):
        if bg or index in flagged:
            continue
        words = [w.text for w in seg.words]
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
    found = _commit_segments(lex(command, commit_message_form=True), cwd, 0)
    if found is None or any(background for _, background in found):
        return CommitFacts(())
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
