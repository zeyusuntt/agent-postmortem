"""Redact strings before they reach a report, a JSON export, or a terminal.

redact() is the single entry point; every other module that prints text a
stranger might read calls it (or the Redactor wrapper) instead of formatting
raw paths or ids itself. See docs/DESIGN.md section 4.8 for the exact rules.
"""

from __future__ import annotations

import bisect
import hashlib
import itertools
import json
import os
import re
import unicodedata
from collections.abc import Callable
from pathlib import Path

from . import disclosure
from .disclosure import DisclosureContext

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

# Claude Code project slugs are a run of hyphen-joined path segments derived
# from an absolute path under /Users/<name>/... (macOS) or /home/<name>/...
# (Linux), for example "-Users-alice-Documents-demo" or
# "-home-alice-project". A segment may hold anything the source path could
# (spaces, apostrophes, non-ASCII characters); matching on whitespace rather
# than a fixed character class means a segment we did not anticipate still
# gets swept into the hash instead of silently ending the match early and
# leaking the rest of the slug. Agent ids (17 hex chars starting with "a")
# never match this pattern, so they pass through untouched.
_PROJECT_SLUG_RE = re.compile(r"-(?:Users|home)-\S+")

# Email addresses appear in captured git output ("Author: Name <x@y>").
# F fix 9 (W9-6): linear. A match can only start where the run of local-part characters starts (the
# lookbehind), and the run is taken once, possessively; without both, a long word with no `@` was
# scanned again from every one of its characters (100,000 characters took about 30 seconds).
_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]++@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# docs/DESIGN.md 5.1 (M8 review D-A): a home-relative path still names every
# directory between "~/" and the file itself verbatim -- "~/Documents/
# <client>/<sub>/notes.md" leaks the client name even after the home
# directory itself becomes "~". Collapsed to "~/.../<basename>" so only the
# file being pointed at survives, never the directories that name what it
# is about. A directory segment is "one or more non-slash characters
# immediately followed by a slash" -- spaces, apostrophes, and non-ASCII
# characters all pass through fine, since only the slash itself is a
# delimiter; the trailing basename stops at the next slash or whitespace
# (whichever bounds it in ordinary prose). Collapsing only fires with at
# least one directory segment to hide: a bare "~/notes.md" (nothing between
# "~/" and the file) already shows only its last segment and is left alone,
# and a bare "~" with no slash at all (shell shorthand, not a path) never
# matches "~/" in the first place -- so does a URL's "~user" spelling, since
# nothing there is followed immediately by a slash. (N3b: the collapsed form
# itself, "~/.../<basename>" followed by whitespace or the end, is a fixed
# point and is left alone, so a list of paths a sink already collapsed is not
# swallowed whole by the greedy directory run when it is redacted a second
# time. "~/.../a/b" is not that form and still collapses.)
_HOME_PATH_COLLAPSE_RE = re.compile(
    r"~/(?!\.\.\./[^/\s]+(?:\s|$))(?:[^/\n]+/)+([^/\s]+)"
)

# The same collapse for a /tmp or /private/tmp scratch path, but keeping its
# first segment (the scratch directory itself, for example a fixture or run
# id) rather than hiding everything: unlike a home directory, /tmp's first
# segment is not routinely a project or client name, and several of this
# tool's own detectors and fixtures point straight at
# "/tmp/<fixed-name>/<file>" with nothing to hide there at all. Only a
# deeper path -- something beyond that first segment -- gets collapsed.
# (N3b C6: "/tmp" must begin a path token, not sit inside one, and a segment
# never crosses whitespace, so an interior ".../tmp/..." in a longer path and
# the prose after a path are both left alone.)
# (F fix 9, W9-6: a segment never crosses a shell separator, but a no-break or ideographic space
# is an ordinary character of a name, so `\s` is not the class.)
_TMP_SEG = r"[^/ \t\n\r\f\v]"
_TMP_PATH_COLLAPSE_RE = re.compile(
    r"(?<![\w./~-])(/(?:private/)?tmp)/(" + _TMP_SEG + r"+)/(?:" + _TMP_SEG + r"+/)+("
    + _TMP_SEG + "+)"
)

# N3b P19 (S4, S10), F fix 4: what STARTS an absolute path in free text. A `/` that is not
# preceded by a word character, a slash, a dot, a tilde, a dollar sign, or an at sign, so
# "and/or", "3/4" and "https://host/x" are left alone. A `/` after `:`, `=`, or `-` counts
# (PYTHONPATH=/a:/b, R-X-/a/b), as does one glued to a short option (`-C/a/b`), and a `file://`
# URL counts whole (the scheme and a `localhost` host are dropped from the replacement). To keep
# ordinary prose out, a
# run after `:` or `-` must hold two segments or a file extension. Where the path ENDS is not
# decided here: the scanner below finds a start inside a complete shell word and renders the
# whole rest of the word (quotes, escapes and spaces inside it included) as one path.
_PATH_CHARS = r"\w.@+=,%-"
# F fix 9 (W9-1), F fix 10 (W10-1): a script written WITHOUT spaces between words (Chinese,
# Japanese, Korean, Thai, Lao, Tibetan, Myanmar, Khmer), and `\w` matches its characters, so a path
# typed right after a word (`无法读取/Users/x`, `อ่าน/srv/x`) would be "glued to a word". A character
# of that class directly before a start is a boundary instead. One class for every start lookbehind
# (the cost: `和/或` with two segments or an extension is a start, see `_start_counts`). A script
# written WITH spaces (Latin, Cyrillic, Arabic, Greek) is not in it: a word glued to a path there is
# the same construction as English `read/srv/x`, a recorded limit.
_UNSPACED = (
    "\u0e00-\u0fff"  # Thai, Lao, Tibetan
    "\u1000-\u109f"  # Myanmar
    "\u1100-\u11ff\u1780-\u17ff\u19e0-\u19ff"  # hangul jamo, Khmer, Khmer symbols
    "\u2e80-\u9fff\ua960-\ua97f\ua9e0-\ua9ff\uaa60-\uaa7f\uac00-\ud7ff\uf900-\ufaff"
    "\uff00-\uffef"
    "\U00020000-\U0003ffff"
)
_UNSPACED_BEFORE = "(?<=[" + _UNSPACED + "])"
_UNSPACED_RE = re.compile("[" + _UNSPACED + "]")
# M3-1: an apostrophe with a word character on both sides is a segment character too
# (`O'Neil`); at a segment edge or next to a space or a slash it is not.
_SEG_RUN = "(?:[" + _PATH_CHARS + "]|(?<=\\w)'(?=\\w))+"
# FA1: the one alphabet of a path token. A segment is a run of _PATH_CHARS, the separator
# is `/`; Pseudonyms.apply_paths scans tokens with exactly this class (there is no second,
# narrower one).
_PATH_TOKEN_CHAR_RE = re.compile("[/" + _PATH_CHARS + "]")  # `-` last: never a range
# W6-8: a `file://` URL's scheme is case-insensitive and may carry the host `localhost`; extra
# slashes (`file:////srv/x`) are one place.
_FILE_URL_RE = re.compile("(?i:file://(?:localhost)?)(?=/)")
# F fix 7 (W7-3): a `file://` URL with any host (`file://server/share/x`) is a path too: the host
# is part of what the URL names. `_FILE_URL_RE` is the local form whose scheme and host are dropped.
_FILE_URL_ANY_RE = re.compile(r"(?i:file://[\w.@:+-]*)(?=/)")
# F fix 7 (W7-3): a Windows path starts at a drive letter (`C:\`, `c:/`: one ASCII letter that no
# word character precedes) or at a UNC share (`\\server\share`: two backslashes that no word
# character or backslash precedes, a host, a backslash and a name character).
# F fix 8 (W8-8): a Windows namespace path (`\\?\UNC\server\share\x`, `\\?\C:\x`, `\\.\C:\x`)
# starts at its two backslashes, a `?` or `.` and a backslash.
_WIN_PATH_RE = re.compile(
    r"(?:(?<!\w)|" + _UNSPACED_BEFORE + r")[A-Za-z]:[\\/]"
    r"|(?:(?<![\\\w])|" + _UNSPACED_BEFORE + r")\\\\[\w.$-]+\\[^\s\\]"
    r"|(?:(?<![\\\w])|" + _UNSPACED_BEFORE + r")\\\\[?.]\\[^\s\\]"
)
_ABS_PATH_RE = re.compile(
    "(?i:file://[\\w.@:+-]*)(?=/)(?:/+" + _SEG_RUN + ")+/?"
    # N3b R2-2: a leading `//` (POSIX keeps two slashes; they name the same
    # place as one). Not after `:`, so a URL's `://` is never read as one.
    r"|(?:(?<![\w/.~$@:])|" + _UNSPACED_BEFORE + r")//+(?:" + _SEG_RUN + "/)*" + _SEG_RUN
    # F fix 8 (W8-4): `@` is not in the lookbehind: `curl -d @/srv/x` and `file=@/srv/x` upload a
    # path, and `@scope/pkg` or an address have a word character before the slash anyway
    + r"|(?:(?<![\w/.~$])|(?<=(?<!\w)-[A-Za-z])|" + _UNSPACED_BEFORE + r")/(?:" + _SEG_RUN + "/)*"
    + _SEG_RUN
    # N3b R2-2: the bare root `/` as a token of its own. A `/` with a space
    # on both sides is a separator in prose ("checkout / restore", "read /
    # write"), not a path, and is left alone.
    + r"|(?<![\w/.~$@:-])(?<!\s)/(?![\w/.@+=,%~$-])"
    r"|(?<=\s)/(?![\w/.@+=,%~$ -])"
    # (F fix 9, W9-4: a lone `/` before a space is never the root of a path whose first segment
    # starts with a space; the W8-8 recognizer hid prose such as `Pass / Fail rate is 3/4` and is a
    # recorded limit in docs/DESIGN.md 5.1.)
)
# N3b S9: C0 controls (and DEL and C1) never reach a terminal inside a name.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# W6-8: a home anchor at the start of a word: `~/`, `~user/` (another account's home) and the
# shell variable spellings of the home directory.
_HOME_START_RE = re.compile(r"~[\w.-]*/|\$HOME/|\$\{HOME\}/")
# F fix 8 (W8-2, W8-8): every anchor of a variable or another account's home, searched wherever a
# path start is searched (inside a quote, after a word that is not a proven prefix). The working
# directory is an absolute place in all but spelling: `$PWD/x`, `${PWD}/x`, `$OLDPWD/x` and
# `${OLDPWD}/x` are path starts too. Such a text is a relative spelling with no base, so every
# level shows it as a path-N (a span with whitespace is opaque like any other).
# F fix 9 (W9-2, W9-6): an `@` does not block the anchor (`curl -d @$HOME/x`), a CJK character is a
# boundary, and the project directory of a hook command (`$CLAUDE_PROJECT_DIR/`) is a working
# directory too.
_VAR_ANCHOR_RE = re.compile(
    r"(?:(?<![\w./~$])|" + _UNSPACED_BEFORE + r")(?:~[\w.-]*|\$HOME|\$\{HOME\}"
    r"|\$(?:\{(?:OLD)?PWD\}|(?:OLD)?PWD|\{CLAUDE_PROJECT_DIR\}|CLAUDE_PROJECT_DIR))/"
)
# F fix 8 (W8-3), F fix 9 (W9-5): a terminal control sequence. One parser for every use: `ESC [`,
# parameter bytes (`0-9 : ; < = > ?`, so colon-form 24-bit colour is one), intermediate bytes (space
# to `.`; a `/` is never one, so `ESC[` before a path never eats the path start) and a final byte;
# or the same colour text with the ESC byte already stripped (`[38:2::255:0:0m`: digits, `:` and
# `;` then `m`, which no word character may precede). A final byte is a word character, so a
# sequence glued to a path would hide the start: the start grammar sees the sequence blanked, and
# the sequence is a kept prefix and suffix.
_CSI = r"\x1b\[[0-?]*[ -.]*[@-~]|(?<!\w)\[[0-9:;]*m"
_CSI_RE = re.compile(_CSI)
_CSI_FIRST = frozenset("\x1b[")
_STRICT_PATH_RE = re.compile(r"/[^/]+/|[\w-]\.\w+$")


def _start_counts(before: str, value: str, after: str) -> bool:
    """Whether the POSIX start `value` (what `_ABS_PATH_RE` matched) counts as a path start given
    `before`, the character in front of it, and `after`, the character right behind it ("" at the
    start or end of the text). After `:`, `-` or a WORD character of a script written without
    spaces (F fix 10, W10-2; F fix 11, W11-2: its punctuation and spaces are ordinary boundaries;
    F fix 12, W12-1: a combining mark of the class, such as a Thai tone mark, ends a word like a
    letter does) a run counts only with two segments, a file extension (decided with the trailing
    `.` and `,` of the match removed, F fix 11, W11-1) or a `/` right after it (a segment the path
    alphabet cannot read), so `and/or`, `read-/write`, `测试通过/失败 共 12 项`, `读/写` and
    `ใช่/ไม่ใช่` stay prose; a `file://` URL and every other position count. The one rule for the
    raw text (`_abs_starts`) and for the decoded text of an escaped word (`_decoded_starts`)."""
    if _FILE_URL_ANY_RE.match(value):
        return True
    strict = before in (":", "-") or (
        before != ""
        and (_is_word_char(before) or unicodedata.category(before).startswith("M"))
        and _UNSPACED_RE.match(before) is not None
    )
    if strict:
        core = _one_leading_slash(value).rstrip(".,")
        return after == "/" or _STRICT_PATH_RE.search(core) is not None
    return True


# --- the free-text path scanner (docs/DESIGN.md 5.1, F fix 4) -------------------------------------
#
# A path in free text is found inside a complete shell word, and once a word holds a path the
# whole rest of the word is rendered as one path. No regular expression splits a path at a quote,
# an escape or a space: three review rounds each found one more spelling that a character class
# or a quoted-span regex cut in two, leaving a raw private tail. The scanner is
#   _lex          the text into words and boundaries, with the raw span of every decoded character;
#   _PathLookup   the registered path values, matched in NFC form, indexed by first character;
#   _rewrite_word one word that holds a path, rewritten as prefix + rendered path + raw suffix;
#   scan_paths    the whole pass, the one entry every free-text output reaches.

_OPERATORS = frozenset(";|&<>()")
# The shell separates words at a space, a tab and a newline (and a carriage return in a CRLF
# log); a no-break space or an ideographic space is an ordinary character of a name.
_SPACE = frozenset(" \t\n\r\f\v")
_BREAKS = _OPERATORS | _SPACE
_LDQ = "\N{LEFT DOUBLE QUOTATION MARK}"
_RDQ = "\N{RIGHT DOUBLE QUOTATION MARK}"
_LSQ = "\N{LEFT SINGLE QUOTATION MARK}"
_RSQ = "\N{RIGHT SINGLE QUOTATION MARK}"
_DLQ = "\N{DOUBLE LOW-9 QUOTATION MARK}"
# Paired display wrappers (docs/DESIGN.md 5.1): the opener maps to the characters that close its
# fragment. A fragment is literal (no escapes) and may hold spaces; an unpaired opener takes the
# rest of its line (fail closed). The backquote is a markdown code span, not a shell command
# substitution; U+201D opens a fragment only where a closing partner follows (`_rdq_opens`).
_WRAPPER_CLOSERS = {
    "`": "`",
    _LDQ: _RDQ + _LDQ,
    _RDQ: _RDQ + _LDQ,
    _DLQ: _LDQ + _RDQ,
    _LSQ: _RSQ,
    "\u00ab": "\u00bb",  # guillemets
    "\u2039": "\u203a",  # single guillemets
    "\u300c": "\u300d",  # CJK corner brackets
    "\u300e": "\u300f",  # CJK white corner brackets
}
# A closing wrapper that ends a word ends what the forward join may extend over.
_CLOSING_WRAPPERS = frozenset("".join(_WRAPPER_CLOSERS.values())) - {"`"}
_HIDDEN_MARK = " [text hidden]"
# The only text kept in front of a path: a proven syntactic prefix. `NAME=` (a shell
# identifier), `--option=`, a short option glued to the path (`-C/x`), a rule id (`R-NOPE-`), and
# an opening bracket or backquote. Anything else before the path start is part of the path
# (fail closed).
_PREFIX_RE = re.compile(
    r"(?:" + _CSI + r")*[\[{`]*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=|--[A-Za-z0-9][A-Za-z0-9_-]*=|-[A-Za-z](?=/)"
    # a rule id (R-NO-LONG-BASH-) glued to a path is a name, never part of it
    r"|R(?:-[A-Z0-9]+)+-(?=/))?"
)
# The raw suffix kept after a path: an evidence pointer (`:12`, `:12:3`), then sentence
# punctuation, a closing bracket, quote or wrapper, or an escaped trailing space.
_SUFFIX_WINDOW = 512
_SUFFIX_RE = re.compile(
    r"(?:(?::[0-9]+){1,2})?(?:" + _CSI + r"|[.,;:)\]}!?`"
    + re.escape("".join(sorted(_CLOSING_WRAPPERS))) + r" \t\n\r\f\v])*\Z"
)


# F fix 10 (W10-4): an interior run of slashes names the same place as one (`/home/user/Docs//Acme/
# plan.md`); the structural collapses of `redact()` stop at an empty segment, so the token is
# collapsed first, and the structural form must then be one of the shapes the contract shows
# (`~`, `~/name`, `~/.../name`, `/tmp`, `/tmp/first`, `/tmp/first/name`, `/tmp/first/.../name`,
# each with `/private` in front of `/tmp` and an optional trailing slash), else the token is its
# own path-N.
_STRUCTURAL_SHAPE_RE = re.compile(
    r"~(?:/(?:\.\.\./)?[^/]+)?/?|(?:/private)?/tmp(?:/[^/]+(?:/(?:\.\.\./)?[^/]+)?)?/?"
)
_SLASH_RUN_RE = re.compile(r"(?<=.)/{2,}", re.DOTALL)


def _collapse_slash_runs(token: str) -> str:
    return _SLASH_RUN_RE.sub("/", token)


def _nfc(text: str) -> str:
    return text if text.isascii() else unicodedata.normalize("NFC", text)


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _inword_apostrophe(text: str, at: int) -> bool:
    """text[at] is an apostrophe with a word character on both sides (`it's`, `O'Neil`): an
    ordinary character, not a quote (the path alphabet counts it as token text too)."""
    return (
        text[at] == "'" and 0 < at < len(text) - 1
        and _is_word_char(text[at - 1]) and _is_word_char(text[at + 1])
    )


def _inword_quote(text: str, at: int) -> bool:
    """text[at] is an apostrophe or a U+2019 with a word character on both sides: an ordinary
    character, never a closing quote."""
    return (
        text[at] in ("'", _RSQ) and 0 < at < len(text) - 1
        and _is_word_char(text[at - 1]) and _is_word_char(text[at + 1])
    )


def _one_leading_slash(value: str) -> str:
    """N3b R2-2: `//a/b` and `/a/b` name the same place; place one spelling."""
    return "/" + value.lstrip("/") if value.startswith("//") else value


class _Word:
    """One logical shell word (or one run of boundary characters): its raw span `[start, end)`,
    its decoded text, the raw span `[rstart[k], rend[k])` of every decoded character k, and the
    quote fragments it opened, each `[opening index, closing index or -1]` in the raw text."""

    __slots__ = (
        "start", "end", "boundary", "chars", "rstart", "rend", "frags", "text", "escaped",
    )

    def __init__(self, start: int, boundary: bool = False) -> None:
        self.start = start
        self.end = start
        self.boundary = boundary
        self.chars: list[str] = []
        self.rstart: list[int] = []
        self.rend: list[int] = []
        self.frags: list[list[int]] = []
        self.text = ""
        self.escaped = False  # a JSON escape was decoded inside a double-quoted fragment

    def add(self, ch: str, start: int, end: int) -> None:
        self.chars.append(ch)
        self.rstart.append(start)
        self.rend.append(end)


_JSON_SIMPLE_ESCAPES = {"/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}
_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")


def _json_escape(text: str, at: int) -> tuple[str, int] | None:
    """The JSON escape that starts at text[at] (a backslash): its character and raw length, or
    None. The escapes of a slash, n, t, r, b, f and uXXXX are decoded (a surrogate pair is one
    character; a lone surrogate is not decoded, it stays as its escape text)."""
    letter = text[at + 1:at + 2]
    if letter in _JSON_SIMPLE_ESCAPES:
        return _JSON_SIMPLE_ESCAPES[letter], 2
    if letter != "u" or not _HEX4_RE.fullmatch(text[at + 2:at + 6]):
        return None
    code = int(text[at + 2:at + 6], 16)
    if 0xD800 <= code < 0xDC00:
        low = text[at + 6:at + 12]
        if low[:2] == "\\u" and _HEX4_RE.fullmatch(low[2:]) and 0xDC00 <= int(low[2:], 16) < 0xE000:
            return chr(0x10000 + ((code - 0xD800) << 10) + (int(low[2:], 16) - 0xDC00)), 12
        return None
    if 0xDC00 <= code < 0xE000:
        return None
    return chr(code), 6


def _lex_double(text: str, at: int, word: _Word) -> int:
    """A double-quoted fragment opened at text[at]; the index after it. A backslash escapes `"`,
    a backslash, `$`, a backquote and a newline, and the JSON escapes (see `_json_escape`) are
    decoded too (a truncated JSON string is still a path); before any other character it stays
    literal. An unescaped newline ends an unclosed fragment (fail closed: the rest of the line
    was inside)."""
    fragment = [at, -1]
    word.frags.append(fragment)
    size = len(text)
    i = at + 1
    while i < size:
        ch = text[i]
        if ch == '"':
            fragment[1] = i
            return i + 1
        if ch == "\n":
            return i
        if ch == "\\" and i + 1 < size:
            following = text[i + 1]
            if following == "\n":
                i += 2
                continue
            if following == "\r" and text[i + 2:i + 3] == "\n":
                i += 3
                continue
            if following in '"\\$`':
                word.add(following, i, i + 2)
                if following == "\\":
                    word.escaped = True  # a Windows path is found on the decoded text too
                i += 2
                continue
            decoded = _json_escape(text, i)
            if decoded is not None:
                word.add(decoded[0], i, i + decoded[1])
                word.escaped = True
                i += decoded[1]
                continue
        word.add(ch, i, i + 1)
        i += 1
    return i


def _lex_literal(
    text: str, at: int, closers: str, word: _Word, registered: "_LineRegistered | None" = None,
) -> int:
    """A fragment with no escapes (single quotes, typographic quotes, display wrappers), opened at
    text[at] and closed by any character of `closers`; the index after it. A U+2019 between two
    word characters is an apostrophe, not a closer. A fragment opened by U+2018 that is
    path-bearing (`_path_bearing`) closes at the last U+2019 of its line before the next U+2018
    (F fix 10, W10-3), or runs to the end of the line after a cut-off possessive."""
    fragment = [at, -1]
    word.frags.append(fragment)
    size = len(text)
    i = at + 1
    while i < size:
        ch = text[i]
        if ch in closers and not _inword_quote(text, i):
            if text[at] == _LSQ and _path_bearing(text, at, i, registered):
                fragment[1], i = _extend_to_last_quote(text, i, word, _RSQ, _LSQ)
                return i
            fragment[1] = i
            return i + 1
        if ch == "\n":
            return i
        word.add(ch, i, i + 1)
        i += 1
    return i


def _lex_single(
    text: str, at: int, word: _Word, registered: "_LineRegistered | None" = None,
) -> int:
    """A single-quoted fragment opened at text[at]: `\\'` and `\\\\` inside it are the quote and
    the backslash, the union of the shell rule and the Python repr rule (a repr of a string that
    holds both quote kinds is one fragment at every sink; the union can only widen a span), any
    other backslash stays literal. An in-word apostrophe (`Bob's`) is name text and never closes
    the fragment (F fix 8, W8-1, made fail-closed in F fix 8b: Node, git and GNU tools print a name
    in single quotes with no escaping, and an excerpt can be cut off before the real closing
    quote). A fragment that holds a path start closes at the LAST quote of its line that is not an
    in-word apostrophe (F fix 9, W9-3, fail closed): a possessive apostrophe before a space
    (`'/srv/James' Notes/x.md'`) is indistinguishable from a closing quote, so the first such quote
    never ends the span; a later quoted phrase on the line is hidden with the path. An unescaped
    newline ends an unclosed fragment, so the rest of the line is hidden.

    F fix 10 (W10-3): a fragment is path-bearing (`_path_bearing`) when its text holds an absolute
    start or a registered value occurs between its opener and the end of its line; the quote that
    closes it is the last of its line as above, and not even that one when it is a possessive of a
    path cut off before its real closing quote (`_cut_off_possessive`): the fragment then runs to
    the end of the line."""
    fragment = [at, -1]
    word.frags.append(fragment)
    size = len(text)
    i = at + 1
    while i < size:
        ch = text[i]
        if ch == "\\" and i + 1 < size and text[i + 1] in "'\\":
            word.add(text[i + 1], i, i + 2)
            i += 2
            continue
        if ch == "'":
            if _inword_apostrophe(text, i):
                word.add(ch, i, i + 1)  # `Bob's` inside `'/srv/Bob's Docs/x'` is name text
                i += 1
                continue
            if _path_bearing(text, at, i, registered):
                fragment[1], i = _extend_to_last_quote(text, i, word, "'")
                return i
            fragment[1] = i
            return i + 1
        if ch == "\n":
            return i
        word.add(ch, i, i + 1)
        i += 1
    return i


def _path_bearing(text: str, at: int, close: int, registered: "_LineRegistered | None") -> bool:
    """Whether the quote fragment opened at text[at] whose first closing candidate is text[close]
    is path-bearing (F fix 10, W10-3): its text holds an absolute path start (a bare `'/'` names no
    directory and does not count), or a registered value occurs between its opener and the end of
    its line."""
    quoted = text[at + 1:close]
    if quoted.strip() != "/" and _abs_starts(quoted):
        return True
    return registered is not None and registered.after(at)


_AFTER_QUOTE_RE = re.compile(r"[ \t]+(\S*)")
_NAME_DOT_RE = re.compile(r"\.\w")


def _cut_off_possessive(text: str, quote: int) -> bool:
    """Whether the quote text[quote] may be a possessive of a path cut off before its real closing
    quote (F fix 10, W10-3, rule b): it follows a word character and is followed by one or more
    spaces or tabs and then a token (the characters up to the next whitespace) that holds `/`,
    `\\` or a `.` followed by a word character (`open '/srv/James' Notes/x`, `... Notes.md`)."""
    if quote == 0 or not _is_word_char(text[quote - 1]):
        return False
    after = _AFTER_QUOTE_RE.match(text, quote + 1)
    if after is None:
        return False
    token = after.group(1)
    return "/" in token or "\\" in token or _NAME_DOT_RE.search(token) is not None


def _extend_to_last_quote(
    text: str, close: int, word: _Word, quote: str, opener: str | None = None,
) -> tuple[int, int]:
    """(the raw index of the quote that closes a path-bearing fragment, the index to lex on from)
    for a fragment whose first closing candidate is text[close]. The closing quote is the last
    `quote` of the line of `text[close]` that is not an in-word apostrophe (at least `close`
    itself; for a U+2018 fragment it also stops before the next `opener` of the line), F fix 9,
    W9-3; the text between is added to the word as name text. When that quote is a possessive of a
    cut-off path (`_cut_off_possessive`) the fragment is not closed there: the index is -1 and the
    word takes the rest of the line (fail closed)."""
    line_end = text.find("\n", close)
    line_end = len(text) if line_end < 0 else line_end
    limit = line_end
    if opener is not None:
        following = text.find(opener, close, line_end)
        if following >= 0:
            limit = following
    last = text.rfind(quote, close, limit)
    while last > close and _inword_quote(text, last):
        last = text.rfind(quote, close, last)
    cut = _cut_off_possessive(text, last)
    stop = line_end if cut else last
    k = close
    while k < stop:
        if quote == "'" and text[k] == "\\" and k + 1 < stop and text[k + 1] in "'\\":
            word.add(text[k + 1], k, k + 2)  # the escapes of `_lex_single`, in the text between
            k += 2
        else:
            word.add(text[k], k, k + 1)
            k += 1
    return (-1, line_end) if cut else (last, last + 1)


class _LineRegistered:
    """Whether a registered value occurs between a quote opener and the end of its line (F fix 10,
    W10-3). The line is analysed once, however many fragments it holds: the raw line gives the
    index of the last registered value in it; the line with `\\'` and `\\\\` decoded and the NFC
    form of the line (and of the decoded line) give no index, so any value that hits one of them
    makes every fragment on the line path-bearing (fail closed instead of mapping indexes back).
    Cost: O(L) probes of the lookup per line of L characters per spelling."""

    __slots__ = ("_lookup", "_text", "_start", "_end", "_last", "_wide")

    def __init__(self, lookup: "_PathLookup", text: str) -> None:
        self._lookup = lookup
        self._text = text
        self._start = self._end = -1
        self._last = -1
        self._wide = False

    def after(self, opener: int) -> bool:
        if not self._start <= opener < self._end:
            self._analyse(opener)
        return self._wide or self._last >= opener

    def _holds(self, line: str) -> int:
        """The index of the last registered value in `line`, or -1."""
        first = self._lookup._by_first
        for at in range(len(line) - 1, -1, -1):
            if line[at] in first and self._lookup.longest_at(line, at) is not None:
                return at
        return -1

    def _analyse(self, opener: int) -> None:
        text = self._text
        start = text.rfind("\n", 0, opener) + 1
        end = text.find("\n", opener)
        end = len(text) if end < 0 else end
        line = text[start:end]
        last = self._holds(line)
        self._start, self._end = start, end
        self._last = start + last if last >= 0 else -1
        spellings = []
        normal = _nfc(line)
        if normal != line:
            spellings.append(normal)
        if "\\" in line:
            decoded = _SINGLE_ESCAPE_RE.sub(r"\1", line)
            spellings.append(decoded)
            if _nfc(decoded) != decoded:
                spellings.append(_nfc(decoded))
        self._wide = any(self._holds(spelling) >= 0 for spelling in spellings)


_SINGLE_ESCAPE_RE = re.compile(r"\\(['\\])")


def _rdq_opens(text: str, at: int) -> bool:
    """U+201D is a closing quote far more often than an opener: it opens a display fragment only
    where a closing partner follows on the same line, a U+201D after a word character
    (`Client” Secret.txt”`, the smart-quote spelling of a half-quoted name) and a U+201D or a
    U+201C otherwise. A lone one glued to a path stays a suffix."""
    line_end = text.find("\n", at + 1)
    line_end = len(text) if line_end < 0 else line_end
    if text.find(_RDQ, at + 1, line_end) >= 0:
        return True
    if at and _is_word_char(text[at - 1]):
        return False
    return text.find(_LDQ, at + 1, line_end) >= 0


def _lex_word(
    text: str, at: int, word: _Word, registered: "_LineRegistered | None" = None,
) -> int:
    size = len(text)
    i = at
    while i < size:
        ch = text[i]
        if ch in _BREAKS:
            break
        if ch == "\\":
            if i + 1 >= size:
                word.add(ch, i, i + 1)
                i += 1
            elif text[i + 1] == "\n":
                i += 2  # a line continuation joins the next line into this word
            elif text[i + 1] == "\r" and text[i + 2:i + 3] == "\n":
                i += 3
            else:
                word.add(text[i + 1], i, i + 2)
                i += 2
        elif ch == '"':
            i = _lex_double(text, i, word)
        elif ch == "'":
            if _inword_apostrophe(text, i):
                word.add(ch, i, i + 1)
                i += 1
            else:
                i = _lex_single(text, i, word, registered)
        elif ch in _CSI_FIRST and (csi := _CSI_RE.match(text, i)) is not None:
            # a control sequence is name text of the word, `;` `<` `>` and all (F fix 8, W8-3)
            for k in range(i, csi.end()):
                word.add(text[k], k, k + 1)
            i = csi.end()
        elif ch in _WRAPPER_CLOSERS and (ch != _RDQ or _rdq_opens(text, i)):
            i = _lex_literal(text, i, _WRAPPER_CLOSERS[ch], word, registered)
        else:
            word.add(ch, i, i + 1)
            i += 1
    return i


def _lex(text: str, lookup: "_PathLookup | None" = None) -> list[_Word]:
    """The text as alternating words and boundaries (docs/DESIGN.md 5.1). Outside quotes a word
    ends at unescaped whitespace or at one of `; | & < > ( )` (a run of them is one boundary,
    copied back unchanged); an unescaped newline always ends a word and no quote crosses it. With a
    registered `lookup` a single-quote or U+2018 fragment is also path-bearing when a registered
    value follows its opener on its line (F fix 10, W10-3); `_holds_start` passes none."""
    size = len(text)
    registered = _LineRegistered(lookup, text) if lookup else None
    segments: list[_Word] = []
    i = 0
    while i < size:
        ch = text[i]
        if ch in _BREAKS:
            j = i + 1
            while j < size and text[j] in _BREAKS:
                j += 1
            boundary = _Word(i, boundary=True)
            boundary.end = j
            boundary.text = text[i:j]
            segments.append(boundary)
            i = j
            continue
        word = _Word(i)
        i = _lex_word(text, i, word, registered)
        word.end = i
        word.text = "".join(word.chars)
        segments.append(word)
    return segments


class _PathLookup:
    """The registered report_path values, each stored once in NFC form (an NFD spelling matches
    an NFC registration and the reverse; no case folding), indexed by first character.

    Bound: one entry per distinct registered value (a run registers the distinct paths its
    claims, observations and ledgers name), memory O(total bytes of the values), kept in step
    with the path namespace incrementally (it only grows). A scan costs O(L * k) for L characters
    of text and k, the number of registered values sharing a first character at a position
    (every absolute value shares `/`); the value is tried longest first with `startswith`."""

    __slots__ = ("_by_nfc", "_by_first", "_seen", "_aliases")

    def __init__(self) -> None:
        self._aliases: set[str] = set()
        self._by_nfc: dict[str, str] = {}
        self._by_first: dict[str, list[str]] = {}
        self._seen = 0

    def __bool__(self) -> bool:
        return bool(self._by_nfc)

    def sync(self, mapping: dict[str, str]) -> None:
        if len(mapping) == self._seen:
            return
        for value in itertools.islice(mapping, self._seen, None):
            if not value or value == "/":
                continue  # the root is registered but is a substring of every path
            # a value that ends in a slash also matches without it (`/srv/my work/` and
            # `/srv/my work`), as a name the directory is called by
            keys = [(_nfc(value), False)]
            if len(value) > 1 and value.endswith("/") and value.rstrip("/"):
                keys.append((_nfc(value.rstrip("/")), True))
            for key, alias in keys:
                if key in self._by_nfc:
                    if not alias and key in self._aliases:
                        # a genuine registration outranks the slash-less spelling of another
                        self._by_nfc[key] = value
                        self._aliases.discard(key)
                    continue
                self._by_nfc[key] = value
                if alias:
                    self._aliases.add(key)
                bisect.insort(
                    self._by_first.setdefault(key[0], []), key, key=lambda item: -len(item),
                )
        self._seen = len(mapping)

    def exact(self, text: str) -> str | None:
        """The registered value that `text` is, in NFC form, or None."""
        return self._by_nfc.get(_nfc(text))

    def lead(self, text: str) -> tuple[str, int] | None:
        """(the registered value that `text` starts with, the length of that value and any
        sentence punctuation right after it) when whitespace follows (the first word of a span
        that holds more); None otherwise. Only for a text that is already in NFC form, so the
        length is a length in `text` too."""
        normal = _nfc(text)
        if normal != text:
            return None
        key = self.longest_at(normal, 0) if normal else None
        if key is None:
            return None
        end = len(key)
        while end < len(normal) and normal[end] in ".,;:!?":
            end += 1  # sentence punctuation right after the value belongs to the sentence
        if end < len(normal) and normal[end] in _SPACE:
            return self._by_nfc[key], end
        return None

    def longest_at(self, text: str, at: int) -> str | None:
        for key in self._by_first.get(text[at], ()):
            if text.startswith(key, at):
                return key
        return None

    def hits(self, parts: list[str]) -> list[tuple[int, int]]:
        """(first part, last part) of every registered value in the NFC form of the parts
        joined; only a value that spans parts is wider than its own part."""
        normal = [_nfc(part) for part in parts]
        joined = "".join(normal)
        ends = list(itertools.accumulate(len(part) for part in normal))
        found: list[tuple[int, int]] = []
        first = self._by_first
        for at, ch in enumerate(joined):
            if ch not in first:
                continue
            key = self.longest_at(joined, at)
            if key is not None:
                found.append((
                    bisect.bisect_right(ends, at), bisect.bisect_right(ends, at + len(key) - 1),
                ))
        return found


def _blank_csi(text: str) -> str:
    """`text` with every terminal control sequence replaced by spaces of the same length (indexes
    are unchanged): the start grammar then sees a boundary where its final byte would be a word
    character (F fix 8, W8-3; F fix 9, W9-5)."""
    if "[" not in text:
        return text
    return _CSI_RE.sub(lambda match: " " * len(match.group(0)), text)


def _abs_starts(text: str) -> list[int]:
    """Every index of `text` where the structural grammar `_ABS_PATH_RE` starts an absolute path
    (a run after `:` or `-` counts only with two segments or a file extension, so `and/or` and
    `read-/write` stay prose), `_WIN_PATH_RE` starts a Windows path, or `_VAR_ANCHOR_RE` starts a
    home or working-directory anchor (`~/`, `~user/`, `$HOME/`, `$PWD/`, wherever it stands). Found
    on the raw text, in its real context: a lone `/` between spaces is prose, the same `/` inside
    quotes is the root. A colour sequence is blanked first. One pass for the whole text."""
    text = _blank_csi(text)
    found = []
    for match in _ABS_PATH_RE.finditer(text):
        if _start_counts(
            text[match.start() - 1] if match.start() else "", match.group(0),
            text[match.end()] if match.end() < len(text) else "",
        ):
            found.append(match.start())
    extra = [match.start() for match in _WIN_PATH_RE.finditer(text)]
    extra.extend(match.start() for match in _VAR_ANCHOR_RE.finditer(text))
    return sorted(set(found + extra)) if extra else found


def _decoded_starts(word: _Word) -> list[int]:
    """The raw index of every path start in the DECODED text of a word that decoded an escape
    inside a double-quoted fragment (`"\\u002fsrv\\u002fx y"` has no literal `/`, and a quoted
    `"\\\\server\\x"` no unescaped backslash, so the raw text cannot show them): an absolute
    start, a home anchor or a Windows start. Empty when the decoded text holds none."""
    decoded = _blank_csi(word.text)
    found = []
    for match in _ABS_PATH_RE.finditer(decoded):
        if _start_counts(
            decoded[match.start() - 1] if match.start() else "", match.group(0),
            decoded[match.end()] if match.end() < len(decoded) else "",
        ):
            found.append(word.rstart[match.start()])
    for pattern in (_VAR_ANCHOR_RE, _WIN_PATH_RE):
        found.extend(word.rstart[match.start()] for match in pattern.finditer(decoded))
    return found


def _anchors_in(word: _Word, starts: list[int]) -> list[int]:
    """The raw index of every path start inside the word (`starts`: every start of the whole
    text), a leading home anchor (`~/`, `~user/`, `$HOME/`, `${HOME}/`) after the proven prefix,
    and, for a word that decoded an escape, every start in its decoded text; empty when it holds
    none."""
    decoded = word.text
    prefix = _PREFIX_RE.match(decoded).end()
    at = bisect.bisect_left(starts, word.start)
    found = []
    while at < len(starts) and starts[at] < word.end:
        found.append(starts[at])
        at += 1
    if prefix < len(decoded) and _HOME_START_RE.match(decoded, prefix):
        found.append(word.rstart[prefix])
    if word.escaped:
        found.extend(_decoded_starts(word))
    return found


def _abs_start_in(word: _Word, starts: list[int]) -> int | None:
    """The raw index of the first path start inside the word (see `_anchors_in`); None when it
    holds none."""
    found = _anchors_in(word, starts)
    return min(found) if found else None


def _rewrite_word(
    word: _Word, text: str, lookup: _PathLookup, flagged: bool, names: "Pseudonyms",
    render: Callable[[str, bool], str], starts: list[int],
    render_hidden: Callable[[str, bool], str] | None = None,
) -> str | None:
    """The replacement for the raw span of one word that holds a path, or None when it holds
    none. The path starts at the start of the word, moved right only past a proven prefix, and
    runs to the end of the word minus a raw suffix; it is rendered once. A span whose decoded
    text holds whitespace may be more than a path: it is rendered as an opaque path-N
    (`render_hidden`, never a structural or repo-relative form) and followed by the marker."""
    decoded = word.text
    size = len(decoded)
    prefix = _PREFIX_RE.match(decoded).end()
    if not flagged and _abs_start_in(word, starts) is None:
        return None
    start = prefix if prefix < size else 0
    if start and lookup:
        # a registered value that overlaps the prefix is part of the path
        normal = _nfc(decoded)
        for at in range(start):
            key = lookup.longest_at(normal, at) if normal[at] in lookup._by_first else None
            if key is not None and at + len(key) > start:
                start = 0
                break
    tail = decoded[start:]
    exact = lookup.exact(tail) if lookup else None
    path_text = tail
    if exact is None:
        # F fix 10 (W10-5): the search window is the last 512 characters (a lookbehind still sees
        # the characters before it): a longer run of suffix characters is shortened, so the extra
        # characters stay inside the hidden path (fail closed), and the search stays linear
        found = _SUFFIX_RE.search(tail, max(0, len(tail) - _SUFFIX_WINDOW))
        if found is not None and found.start() > 0:
            path_text = tail[:found.start()]
            if lookup:
                exact = lookup.exact(path_text)
    end = start + len(path_text)
    spaced = any(ch in _SPACE for ch in path_text)
    # the path as the raw text spells it (quotes and backslashes in place): a registered value
    # that holds a quote character or a backslash matches its raw spelling, and a Windows path is
    # recognized by it (an unquoted backslash is a shell escape, so the decoded text lost it)
    raw_path = text[word.rstart[start]:word.rend[end - 1]] if end > start else ""
    if exact is None and lookup and raw_path != path_text:
        exact = lookup.exact(raw_path)
    lead = lookup.lead(path_text) if lookup and exact is None and spaced else None
    if lead is not None:
        # only a plain separator (not an escaped or quoted space, which belongs to a longer name)
        at = start + lead[1]
        if not (
            word.rend[at] - word.rstart[at] == 1 and not _in_fragment(word, word.rstart[at])
        ):
            lead = None
    if lead is None and lookup and exact is None and spaced and raw_path != path_text:
        raw_lead = lookup.lead(raw_path)
        if raw_lead is not None:
            at = word.rstart[start] + raw_lead[1]
            if text[at] in _SPACE and text[at - 1] != "\\" and not _in_fragment(word, at):
                lead = raw_lead
    if exact is not None:
        shown = names.registered_display(exact)
    elif lead is not None:
        # a span that starts with a registered value followed by hidden text keeps that value's
        # pseudonym (the same path in two places is one pseudonym), plus the marker
        shown = names.registered_display(lead[0])
    else:
        candidate = path_text
        file_url = _FILE_URL_RE.match(candidate)
        if file_url is not None:
            candidate = candidate[file_url.end():].rstrip("/") or "/"
        candidate = _one_leading_slash(candidate)
        if candidate.startswith("${HOME}/"):
            candidate = "~" + candidate[len("${HOME}"):]
        elif candidate.startswith("$HOME/"):
            candidate = "~" + candidate[len("$HOME"):]
        # a Windows path (or a `file://` URL naming a host) is never a repo-relative or a home
        # form: it is always its own opaque path-N
        foreign = (
            _WIN_PATH_RE.match(raw_path) is not None or _WIN_PATH_RE.match(path_text) is not None
            or (_FILE_URL_ANY_RE.match(path_text) is not None and file_url is None)
        )
        if (spaced or foreign) and render_hidden is not None:
            shown = render_hidden(candidate, flagged)
        else:
            shown = render(candidate, flagged)
    path_start = word.rstart[start]
    hidden_to = word.rend[end - 1]
    dropped = set()
    for fragment in word.frags:
        if fragment[0] >= path_start:
            dropped.add(fragment[0])
            if fragment[1] >= 0:
                dropped.add(fragment[1])
        elif path_start <= fragment[1] < hidden_to:
            # a quote opened before the path start whose closing quote is inside the hidden span
            # (`PATH="/usr/bin":/srv/a b`): neither delimiter is kept, so none is left unpaired
            dropped.add(fragment[0])
    prefix_text = "".join(text[k] for k in range(word.start, path_start) if k not in dropped)
    kept_tail = "".join(text[k] for k in range(hidden_to, word.end) if k not in dropped)
    return prefix_text + shown + (_HIDDEN_MARK if spaced else "") + kept_tail


def _join(segments: list[_Word], text: str, first: int, last: int) -> _Word:
    """The words first..last (and the boundaries between them) as one word: a registered value
    that holds whitespace or an operator spans them."""
    joined = _Word(segments[first].start)
    joined.end = segments[last].end
    for segment in segments[first:last + 1]:
        if segment.boundary:
            for k in range(segment.start, segment.end):
                joined.add(text[k], k, k + 1)
        else:
            joined.chars.extend(segment.chars)
            joined.rstart.extend(segment.rstart)
            joined.rend.extend(segment.rend)
            joined.frags.extend(segment.frags)
    joined.text = "".join(joined.chars)
    return joined


# The end-of-line rule (docs/DESIGN.md 5.1, F fix 7). A path start in unquoted context hides
# everything to the end of its line. There is NO other stop: text that came from a transcript, a
# file, an exception or the environment is untrusted, and no operator, closing wrapper or
# line-number shape in it is proof that the path ended (every one of them is legal inside a
# POSIX file name). The span ends only at the end of the line or at the closing delimiter of a
# fragment that was opened before the path start, which the lexer has already made one word.
# The display forms this tool prints for a collapsed path (`~/.../name`, `/tmp/dir/.../name`) are
# a fixed point of the scanner: a text that already holds one (a line scanned a second time) keeps
# the words after it, because the `...` segment says the directories were hidden already.
_DISPLAY_FORM_RE = re.compile(r"(?:~|/(?:private/)?tmp/[^/\s]+)/\.\.\./[^/\s]+")


def _in_fragment(word: _Word, raw: int) -> bool:
    """Whether the raw index `raw` lies inside a quote or display-wrapper fragment of the word."""
    return any(f[0] < raw and (f[1] < 0 or raw < f[1]) for f in word.frags)


def _in_closed_fragment(word: _Word, openers: list[int], raw: int) -> bool:
    """Whether the raw index `raw` lies inside a fragment of the word that has been closed.
    Fragments never nest and come in order, so the one that can hold `raw` is the last that
    opened before it (`openers`: the opening index of every fragment of the word, in order)."""
    at = bisect.bisect_left(openers, raw) - 1
    return at >= 0 and word.frags[at][1] >= 0 and raw < word.frags[at][1]


def _line_span(
    segments: list[_Word], k: int, starts: list[int],
) -> tuple[int, int | None]:
    """(index of the last segment of the span that starts at the word k, the raw index where
    the span ends inside that segment, or None when it ends with the segment). A path start in
    unquoted context hides everything to the end of its line (docs/DESIGN.md 5.1): the span runs
    over every following word and separator of the same line. A word whose every path start sits
    inside a closed quote ends with the quote: its word is not extended."""
    first = segments[k]
    anchors = _anchors_in(first, starts)
    if not anchors:
        anchors = [first.rstart[0] if first.rstart else first.start]
    openers = [fragment[0] for fragment in first.frags]
    if all(_in_closed_fragment(first, openers, anchor) for anchor in anchors):
        return k, None
    from_char = bisect.bisect_left(first.rstart, min(anchors))
    shown = _DISPLAY_FORM_RE.match(first.text, from_char)
    if shown is not None and _SUFFIX_RE.fullmatch(first.text[shown.end():]):
        return k, None
    last = k
    j = k + 1
    while j < len(segments):
        segment = segments[j]
        if segment.boundary:
            line_end = segment.text.find("\n")
            if line_end >= 0:
                cut = segment.start + line_end
                return (j if cut > segment.start else last), cut
        last = j
        j += 1
    return last, None


def _truncate(word: _Word, end: int) -> None:
    """Cut a joined word at the raw index `end` (a stop inside its last segment)."""
    count = bisect.bisect_right(word.rend, end)
    del word.chars[count:], word.rstart[count:], word.rend[count:]
    word.frags = [[f[0], f[1] if f[1] < end else -1] for f in word.frags if f[0] < end]
    word.end = end
    word.text = "".join(word.chars)


_JSON_STRING_RE = re.compile(r'"(?:[^"\\]|\\.)*"', re.DOTALL)
# Where a JSON document can plausibly start: an object opens with a key or closes at once, an
# array with a value or closes at once. `[Errno 13]`, `{not json}` and a run of `{` never match,
# so they cost no failed parse.
_JSON_OPEN_RE = re.compile(r'\{\s*["}]|\[\s*["\-\d\[{tfn\]]')
# A bracket span is a JSON document only as its own token (docs/DESIGN.md 5.1): after the start of
# the text, whitespace, `:`, `,`, `[` or `{`, and before the end of the text, whitespace, `,`, `]`
# or `}`. A span glued to a word character is part of a name.
_JSON_BEFORE = frozenset(" \t\n\r\f\v:,[{")
_JSON_AFTER = frozenset(" \t\n\r\f\v,]}")


def _json_spans(text: str) -> list[tuple[int, int]]:
    """The maximal `{...}` and `[...]` substrings of `text` that parse as JSON, left to right.
    A failed attempt is charged the distance to its error plus the cost of the error object
    (the decoder counts newlines up to the error position), and the total is capped at a small
    multiple of the text, so a hostile run of brackets cannot make the pass quadratic (a text
    past the cap keeps the fail-closed free-text path)."""
    spans: list[tuple[int, int]] = []
    decoder = json.JSONDecoder()
    budget = 4 * len(text) + 10_000
    position = 0
    while budget > 0:
        opener = _JSON_OPEN_RE.search(text, position)
        if opener is None:
            break
        start = opener.start()
        if start and text[start - 1] not in _JSON_BEFORE:
            position = start + 1  # glued to a word: part of a name (`Acme[2024]/Q1`)
            budget -= 1
            continue
        try:
            _value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError as exc:
            budget -= max(exc.pos - start, 0) + 1 + exc.pos // 16
            position = start + 1
        except RecursionError:
            budget -= 1000
            position = start + 1
        else:
            budget -= end - start
            if end < len(text) and text[end] not in _JSON_AFTER:
                position = start + 1  # glued to a word after the span
                continue
            spans.append((start, end))
            position = end
    return spans


def _scan_json_span(span: str, scan: Callable[[str], str]) -> str:
    """One JSON document, in place: every string literal (a key or a value) goes through `scan`
    and keeps its exact spelling when it comes back unchanged; everything else is kept."""
    def one(match: re.Match) -> str:
        literal = match.group(0)
        value = json.loads(literal)
        shown = scan(value)
        # ensure_ascii: every non-ASCII character and every lone surrogate is written as an ASCII
        # escape, so the result always encodes and prints
        return literal if shown == value else json.dumps(shown)

    return _JSON_STRING_RE.sub(one, span)


def _holds_start(chunk: str) -> bool:
    """Whether `chunk` holds a path start by the word scanner's own predicate: a start in the raw
    text (`_abs_starts`) or, for a word that decodes an escape inside a double-quoted fragment, a
    start in its decoded text (`_decoded_starts`). The words of the chunk are lexed only when it
    holds a backslash, the one character a decoded escape needs."""
    if _abs_starts(chunk):
        return True
    if "\\" in chunk:
        return any(
            word.escaped and _decoded_starts(word) for word in _lex(chunk) if not word.boundary
        )
    return False


def _json_spans_clear_of_paths(
    text: str, spans: list[tuple[int, int]], lookup: _PathLookup,
) -> list[tuple[int, int]]:
    """The JSON spans that no path on the same line precedes. A path hides the rest of its line
    (docs/DESIGN.md 5.1), so a bracket span after a path, on the same line, is part of what the
    path hides, not a document to read value by value: it is left to the word scanner.

    Linear (F fix 7, W7-5): the text between the lower bound of a span (the end of the last kept
    span or the start of its line) and the span is read once. A span that follows a path on its
    line makes every later span of that line follow it too, so those are skipped without a new
    scan; a kept span moves the lower bound to its own end, so no text is read twice. "A path
    precedes" is the predicate of the word scanner (`_holds_start`: an absolute, Windows, UNC,
    namespace or `file://` start, a home or working-directory anchor, a start in the decoded text
    of an escaped word, a registered value), not the presence of a slash (F fix 8, W8-9)."""
    kept: list[tuple[int, int]] = []
    low = -1  # the lower bound the verdict below holds for
    dirty = False  # a path precedes every span from `low` on
    line_start = 0
    previous = 0
    for start, end in spans:
        newline = text.rfind("\n", previous, start)
        if newline >= 0:
            line_start = newline + 1
        previous = start
        bound = max(kept[-1][1] if kept else 0, line_start)
        if bound != low:
            low, dirty = bound, False
            chunk = text[low:start]
            if _holds_start(chunk):
                dirty = True  # a path start precedes the span
            elif lookup and lookup.hits([chunk]):
                dirty = True  # a registered value precedes the span
        if dirty:
            continue
        kept.append((start, end))
    return kept


def scan_paths(
    text: str, names: "Pseudonyms", render: Callable[[str, bool], str], *, absolute: bool = True,
    render_hidden: Callable[[str, bool], str] | None = None,
) -> str:
    """The one free-text path scanner (docs/DESIGN.md 5.1). Every word that holds a registered
    path value of `names` (decoded or raw spelling, NFC form), or, with `absolute`, an absolute
    path, is rewritten as its prefix, the path rendered once by `render(path text, whether a
    registered value was found)` and its raw suffix. A word with neither keeps its bytes. A span
    that holds whitespace is rendered by `render_hidden` (default `render`) and gets a marker.
    JSON (the whole text, or a maximal `{...}` or `[...]` substring that stands as its own token)
    is scanned string by string."""
    if not text:
        return text
    lookup = names.path_lookup()
    if not lookup and (
        not absolute or ("/" not in text and "~" not in text and "\\" not in text)
    ):
        return text

    def again(piece: str) -> str:
        return scan_paths(piece, names, render, absolute=absolute, render_hidden=render_hidden)

    if "{" in text or "[" in text:
        spans = _json_spans_clear_of_paths(text, _json_spans(text), lookup)
        if spans:
            out: list[str] = []
            position = 0
            for start, end in spans:
                out.append(again(text[position:start]) if start > position else "")
                out.append(_scan_json_span(text[start:end], again))
                position = end
            out.append(again(text[position:]) if position < len(text) else "")
            shown = "".join(out)
            return text if shown == text else shown
    return _scan_words(text, names, render, lookup, absolute, render_hidden)


def _scan_words(
    text: str, names: "Pseudonyms", render: Callable[[str, bool], str], lookup: _PathLookup,
    absolute: bool, render_hidden: Callable[[str, bool], str] | None,
) -> str:
    segments = _lex(text, lookup)
    flags = [False] * len(segments)
    if lookup:
        parts = [segment.text for segment in segments]
        ranges = lookup.hits(parts)
        raw_parts = [text[segment.start:segment.end] for segment in segments]
        if raw_parts != parts:
            ranges += lookup.hits(raw_parts)
        spans: list[list[int]] = []
        for first, last in sorted(set(ranges)):
            while first <= last and segments[first].boundary:
                first += 1
            while last >= first and segments[last].boundary:
                last -= 1
            if first > last:
                continue
            if spans and first <= spans[-1][1]:
                spans[-1][1] = max(spans[-1][1], last)
            else:
                spans.append([first, last])
        if spans:
            rebuilt: list[_Word] = []
            flags = []
            k = 0
            pending = iter(spans)
            span = next(pending, None)
            while k < len(segments):
                if span is not None and k == span[0]:
                    first, last = span
                    rebuilt.append(
                        segments[first] if first == last else _join(segments, text, first, last)
                    )
                    flags.append(True)
                    k = last + 1
                    span = next(pending, None)
                else:
                    rebuilt.append(segments[k])
                    flags.append(False)
                    k += 1
            segments = rebuilt
    out: list[str] = []
    position = 0
    starts = _abs_starts(text) if absolute else []
    k = 0
    while k < len(segments):
        segment, flagged = segments[k], flags[k]
        k += 1
        if segment.boundary or not segment.text:
            continue
        if flagged or _abs_start_in(segment, starts) is not None:
            end, cut = _line_span(segments, k - 1, starts)
            if end > k - 1 or cut is not None:
                segment = _join(segments, text, k - 1, end)
                if cut is not None and cut < segment.end:
                    _truncate(segment, cut)
                k = end + 1
        rewritten = _rewrite_word(
            segment, text, lookup, flagged, names, render, starts, render_hidden,
        )
        if rewritten is None:
            continue
        out.append(text[position:segment.start])
        out.append(rewritten)
        position = segment.end
    if not out:
        return text
    out.append(text[position:])
    return "".join(out)


# The account name shows up on its own in ls owner columns, ps rows and
# pytest temp dirs. Shorter names would collide with ordinary words.
_MIN_BARE_USER_LEN = 3

# docs/DESIGN.md 5.1: agent types this tool itself defines. They identify a
# role, not something a user typed, so Pseudonyms.type() leaves them alone.
BUILT_IN_AGENT_TYPES = frozenset({
    "general-purpose", "Explore", "Plan", "code-writer", "code-reviewer",
    "test-writer", "test-runner", "debugger", "doc-scribe",
})


def _redact_uuid(match: re.Match) -> str:
    value = match.group(0)
    return f"uuid:{value[-4:]}"


def _redact_email(match: re.Match) -> str:
    digest = hashlib.sha1(match.group(0).encode("utf-8")).hexdigest()[:6]
    return f"email:{digest}"


def _redact_project_slug(match: re.Match) -> str:
    value = match.group(0)
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:6]
    return f"project:{digest}"


def _collapse_home_path(match: re.Match) -> str:
    return f"~/.../{match.group(1)}"


def _collapse_tmp_path(match: re.Match) -> str:
    root, first_segment, basename = match.group(1), match.group(2), match.group(3)
    return f"{root}/{first_segment}/.../{basename}"


def _home_pattern(home_str: str) -> re.Pattern:
    """Regex for the home directory, including truncated spellings of it.

    Transcripts quote commands cut at a byte limit, so a home of
    /Users/alice can appear as "/Users/ali". Every non-empty prefix of the
    user segment matches, but only at a boundary: end of text, a path
    separator, or a character that cannot continue a user name. That keeps
    /Users/alicedoe (a different account) and /Users/bob untouched.
    """
    home = Path(home_str)
    user = home.name
    parent = str(home.parent)
    if not user or parent == home_str:
        return re.compile(re.escape(home_str))
    prefixes = [re.escape(user[:i]) for i in range(len(user), 0, -1)]
    return re.compile(
        re.escape(parent) + "/(?:" + "|".join(prefixes) + r")(?=$|/|[^A-Za-z0-9._-])"
    )


def redact(text: str, home: Path | None = None) -> str:
    """Replace the home directory, UUIDs, and project slugs with placeholders.

    - The home directory (default Path.home()) becomes "~".
    - Any UUID (8-4-4-4-12 hex) becomes "uuid:" plus its last 4 hex chars.
    - Any "-Users-<name>-..." project slug becomes "project:" plus a 6-char
      hash of the slug.
    - The bare account name (3+ chars, whole word) becomes "user".
    - Any email address becomes "email:" plus a 6-char hash.
    - A home-relative path beyond "~/" and a /tmp or /private/tmp path
      beyond its first segment collapse to ".../<basename>" (docs/
      DESIGN.md 5.1's D-A): only the file being pointed at survives, never
      the directory names in between.
    Nothing else changes. Agent ids are left alone on purpose.
    """
    if not text:
        return text
    result = text
    home_str = str(home) if home is not None else str(Path.home())
    # Order matters: slugs, emails and UUIDs are hashed whole first, so the
    # bare-name rule below cannot rewrite a token inside them (a home segment
    # such as "home" would otherwise break "-home-alice-project").
    result = _PROJECT_SLUG_RE.sub(_redact_project_slug, result)
    result = _EMAIL_RE.sub(_redact_email, result)
    result = _UUID_RE.sub(_redact_uuid, result)
    if home_str and home_str != "/":
        result = _home_pattern(home_str).sub("~", result)
        user = Path(home_str).name
        if len(user) >= _MIN_BARE_USER_LEN:
            result = re.sub(
                r"(?<![A-Za-z0-9_])" + re.escape(user) + r"(?![A-Za-z0-9_])", "user", result
            )
    # Collapse the directories of a home-relative or /tmp path (D-A): run
    # after the home substitution above, so a path that started out as an
    # absolute home directory is covered too, not only one already spelled
    # with "~/" in the source text.
    result = _HOME_PATH_COLLAPSE_RE.sub(_collapse_home_path, result)
    result = _TMP_PATH_COLLAPSE_RE.sub(_collapse_tmp_path, result)
    return result


class Pseudonyms:
    """Per-render mapping from a user-authored name to a stable, ordinal
    pseudonym (docs/DESIGN.md 5.1).

    An attempt label maps to agent-N, a meta description to desc-N, a phase
    to phase-N, a non-built-in agent_type to type-N, and a trend rule target
    to target-N, each in its own namespace and its own order of first
    appearance (M8 review D-D): one counter per field family, not shared
    across them, so a label and a differently-worded description for the
    same attempt never consume each other's ordinals and leave a gap in,
    say, an AGENT table that only ever prints labels. A description that is
    literally identical to its attempt's label is still naming the same
    thing, though, so description() special-cases that: it returns the
    label's own agent-N pseudonym rather than minting a new desc-N for text
    that is not actually a second name. Built-in agent types
    (BUILT_IN_AGENT_TYPES) are not user-authored and pass through unchanged.

    A caller registers a name with agent()/description()/phase()/type()/
    target(), which also returns its pseudonym; apply() then substitutes
    every name registered so far wherever it occurs as a whole word inside a
    larger piece of text (docs/DESIGN.md 5.1/5.2: Finding.title only --
    never evidence, a diagnostic, or a metrics value, which are a faithful
    quote of transcript text, not a rendered sentence built from a label),
    longest name first so one name that is a substring of another is never
    partially clobbered, and a word-boundary anchor
    ((?<![A-Za-z0-9_])name(?![A-Za-z0-9_])) so a short registered name (for
    example an agent_type "cli") never matches as a fragment of an unrelated
    word ("client").

    One instance belongs to one render (docs/DESIGN.md 5.1: "rebuilt for the
    next [report]"), so two reports never share a mapping and cannot be
    linked by a repeated pseudonym. enabled=False (--no-redact) makes every
    method a pass-through.
    """

    # docs/DESIGN.md 5.1: "path" is a different kind of category from the
    # other five (agent/desc/phase/type/target). Those five name something a
    # detector wrote a sentence about (a label, a phase) and are confined to
    # Finding.title (5.1/5.2: never evidence, a diagnostic, or a metrics
    # value, which are meant to stay a faithful quote). A report_path is
    # itself a metrics/reported value, not a title ingredient, and the whole
    # point of pseudonymizing it (M1 review finding 1) is that it must never
    # appear raw in ANY of those places by default -- so it is substituted by
    # the plain Redactor call too (see Redactor.__call__ and apply_paths
    # below), not only by title(). _NAME_CATEGORIES keeps the original five
    # separate from "path" so apply() (title-only) never touches a path and
    # apply_paths() (used everywhere) never touches a name.
    _NAME_CATEGORIES = ("agent", "desc", "phase", "type", "target")
    # docs/DESIGN.md 4.11/5.1 (F7 hardening): "check" is a third kind of
    # category, alongside the five names and "path" -- a declared check
    # command is neither a label a detector wrote a sentence about, nor a
    # path this tool ever needs to substring-match inside other prose. It is
    # only ever substituted at the single point rescue.py renders it (a
    # direct `names.check(value)` call, the same pattern `path` already uses
    # for a report_path claim or an observation path), so it needs no
    # apply()/apply_paths()-style substring pass of its own.
    _CATEGORIES = _NAME_CATEGORIES + ("path", "check")

    def __init__(
        self,
        enabled: bool = True,
        *,
        real_names: bool = False,
        resolver: Callable[[str, str | None, Callable[[str], str]], str] | None = None,
    ) -> None:
        self.enabled = enabled
        # N3b P10 (local): real_names makes the five name categories pass
        # through (a label, description, phase, agent type, and rule target
        # print as written), and `resolver` (the P19 path function) decides
        # what a registered path prints as, instead of path-N. Only the run
        # sinks turn either on; every other sink keeps share's pseudonyms
        # until its own part lands (D-l).
        self.real_names = real_names
        self._resolver = resolver
        self._outside_paths: dict[str, str] = {}
        self._maps: dict[str, dict[str, str]] = {category: {} for category in self._CATEGORIES}
        self._lookup: _PathLookup | None = None

    def _outside_path(self, value: str) -> str:
        if value not in self._outside_paths:
            self._outside_paths[value] = f"path-{len(self._outside_paths) + 1}"
        return self._outside_paths[value]

    def _pseudonym(self, category: str, value: str | None) -> str | None:
        if not self.enabled or not value:
            return value
        if self.real_names and category in self._NAME_CATEGORIES:
            return self.local_text(value)
        if self.real_names and category == "check":
            return value
        mapping = self._maps[category]
        if value not in mapping:
            mapping[value] = f"{category}-{len(mapping) + 1}"
        return mapping[value]

    def local_text(self, value: str) -> str:
        """N3b S9, C3: a real name at local. Control characters become a
        space, an absolute path goes through the one P19 path function, and a
        home-relative path collapses to ~/.../<basename>: a name is real but
        never an absolute path and never a terminal escape."""
        text = scan_paths(
            value, self, lambda found, _hit: self.path(found) or "",
            render_hidden=lambda found, _hit: self.opaque_path(found) or "",
        )
        text = _CONTROL_RE.sub(" ", text)
        return _HOME_PATH_COLLAPSE_RE.sub(_collapse_home_path, text)

    def agent(self, value: str | None) -> str | None:
        """An attempt label."""
        return self._pseudonym("agent", value)

    def description(self, value: str | None, *, label: str | None = None) -> str | None:
        """A meta description: its own "desc" namespace, except when it is
        literally identical to the attempt's own label (still naming the
        same agent, not a second name to count), in which case it reuses
        the label's already-registered agent-N pseudonym (D-D)."""
        if not self.enabled or not value:
            return value
        if label is not None and value == label:
            return self.agent(label)
        return self._pseudonym("desc", value)

    def phase(self, value: str | None) -> str | None:
        return self._pseudonym("phase", value)

    def type(self, value: str | None) -> str | None:
        if not self.enabled or not value or value in BUILT_IN_AGENT_TYPES:
            return value
        return self._pseudonym("type", value)

    def target(self, value: str | None) -> str | None:
        """A trend rule target (docs/DESIGN.md 4.7/5.1): the file a rule is
        proposed for, for example an instruction-file path that can embed a
        client or project name. Rendered by the caller as 'target-N
        (basename)', never the raw path, unless --no-redact."""
        return self._pseudonym("target", value)

    def path(
        self, value: str | None, base: str | None = None, *, resolve_from: str | None = None,
    ) -> str | None:
        """A report_path claim value (docs/DESIGN.md 5.1, M1 review finding
        1): its own "path" namespace, numbered in order of first appearance
        like every other category. Unlike agent/desc/phase/type/target, a
        registered path is substituted everywhere the plain Redactor renders
        text (see apply_paths, used by Redactor.__call__), not only inside
        Finding.title -- a relative path or an absolute path outside home
        and /tmp must never survive structural redact() unchanged the way it
        used to.

        At local (N3b P19) a resolver is attached: the display is the path
        function's answer (repo-relative, ~/.../name, or path-N), made
        absolute against `base` when the value is relative. `resolve_from`
        names the original spelling the path function should place when
        `value` is a display form of it (rescue's repo-relative key): the
        mapping is still keyed by `value`, the text that will appear."""
        if self._resolver is not None and self.enabled and value:
            mapping = self._maps["path"]
            if value not in mapping:
                source = resolve_from if resolve_from else value
                mapping[value] = self._resolver(source, base, self._outside_path)
            return mapping[value]
        return self._pseudonym("path", value)

    def opaque_path(self, value: str | None) -> str | None:
        """A span that holds whitespace (docs/DESIGN.md 5.1): the span may be more than a path,
        so it is always its own path-N, never a structural, repo-relative or home form. At local
        with the P19 resolver it is numbered with the paths P19 places outside the repository."""
        if not self.enabled or not value:
            return value
        if self._resolver is not None:
            return self._outside_path(value)
        return self._pseudonym("path", value)

    def check(self, value: str | None) -> str | None:
        """A declared check command (docs/DESIGN.md 4.11/5.1, F7 hardening):
        its own "check" namespace, numbered in order of first appearance.
        rescue.py calls this directly at every point it would otherwise
        print a declared check's own text (the CHECKS/STEPS lines of
        `rescue plan`, the 'last counted run' line of `rescue status`) so
        the full command -- which can carry arbitrary flags, arguments, or
        an embedded path -- is replaced wholesale by default, not merely
        redacted in place."""
        return self._pseudonym("check", value)

    def apply(self, text: str) -> str:
        """Replace every name (agent/desc/phase/type/target -- never path,
        see apply_paths) registered so far with its pseudonym wherever it
        occurs as a whole word inside `text`. A name never registered
        (nothing has called agent()/phase()/type()/target() with it yet) is
        left untouched: apply() only substitutes, it never registers."""
        if not self.enabled or not text:
            return text
        result = text
        pairs = [
            (original, pseudonym)
            for category in self._NAME_CATEGORIES
            for original, pseudonym in self._maps[category].items()
        ]
        for original, pseudonym in sorted(pairs, key=lambda pair: len(pair[0]), reverse=True):
            if not original:
                continue
            pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(original) + r"(?![A-Za-z0-9_])")
            result = pattern.sub(pseudonym, result)
        return result

    def registered_paths_in(self, token: str) -> list[str]:
        """Every registered report_path value (the root `/` aside) that occurs inside
        `token`: the values a token renders as its own path-N must not leave behind."""
        return [
            v for v in self._maps["path"]
            if v and v != "/" and (v in token or (v.endswith("/") and v.rstrip("/") in token))
        ]

    def path_lookup(self) -> _PathLookup:
        """The registered path values as the scanner's NFC lookup, in step with the namespace."""
        if self._lookup is None:
            self._lookup = _PathLookup()
        self._lookup.sync(self._maps["path"])
        return self._lookup

    def registered_path(self, value: str) -> str | None:
        """What the registered path `value` prints as (its pseudonym; at local the P19 display),
        or None when it was never registered."""
        return self._maps["path"].get(value)

    def registered_display(self, value: str) -> str:
        """What a registered path value prints as (its pseudonym; at local the P19 display)."""
        return self._maps["path"][value]

    def alias_path(self, value: str, like: str) -> None:
        """Register `value` (another spelling of the path `like`) so it prints exactly as `like`
        does: rescue lists a path by its repo-relative key and the transcript spells it absolute."""
        if not self.enabled or not value or not like:
            return
        mapping = self._maps["path"]
        if value not in mapping:
            mapping[value] = self.path(like) or ""

    def apply_paths(self, text: str, render: Callable[[str], str] | None = None) -> str:
        """Replace every report_path value registered so far (the "path"
        namespace) wherever it occurs in `text`, by shell WORD, not by substring
        (docs/DESIGN.md 5.1, FA1 and F fix 4). Each word that holds a registered value
        (decoded or raw spelling, NFC form; a value that holds a space or an operator is
        found across words) is rendered whole from the path start to its end: a word that
        is exactly a registered value becomes its pseudonym; a longer one is rendered whole
        by `render` (the single-path renderer of the active level; default `self.path`, the
        word's own path-N), so neither the registered value nor its continuation survives
        and no pseudonym is glued to a raw tail. One pass over the original text, so a
        pseudonym is never matched again by a shorter registered value. Registered values
        only: an unregistered absolute path is `scan_paths`'s `absolute` pass, which
        Redactor.__call__ also runs."""
        if not self.enabled or not text:
            return text
        if render is None:
            def show(token: str, _hit: bool) -> str:
                return self.path(token) or ""
        else:
            def show(token: str, _hit: bool) -> str:
                return render(token)

        def show_hidden(token: str, _hit: bool) -> str:
            return self.opaque_path(token) or ""

        return scan_paths(text, self, show, absolute=False, render_hidden=show_hidden)


class Redactor:
    """Callable wrapper around redact() with a no-op mode for --no-redact.

    Passing an instance around (instead of a bare function) lets callers ask
    once "am I redacting?" and lets report.py treat --redact/--no-redact as
    one flag instead of branching at every call site. Every Redactor carries
    its own Pseudonyms instance (`names`).

    Calling the Redactor itself (__call__) runs structural redact() only --
    home directory, UUIDs, project slugs, emails, the bare account name.
    This is what evidence lines, diagnostics, and metrics values go through
    (docs/DESIGN.md 4.5/5.2: evidence is a faithful quote of transcript
    text, and a pseudonym substitution has no business rewriting prose that
    only happens to share a word with a registered name). `title()` is the
    one place pseudonym substitution also applies, on top of structural
    redact(): docs/DESIGN.md 5.1/5.2 scope pseudonyms to Finding.title and
    to the structured name fields (label, phase, meta description,
    agent_type) a renderer registers directly through `names`.
    """

    def __init__(
        self,
        enabled: bool = True,
        home: Path | None = None,
        *,
        context: DisclosureContext | None = None,
    ) -> None:
        # N3b D-a: a command builds one DisclosureContext from its parsed
        # level and passes it here; enabled is then level != "raw" and the
        # context's home is the one redact() uses. Redactor(enabled=...)
        # keeps working for every existing caller: it builds the matching
        # context itself (share when enabled, raw when not, no book).
        if context is None:
            context = DisclosureContext(
                level="share" if enabled else "raw",
                home=home if home is not None else Path.home(),
            )
            self.home = home
        else:
            self.home = context.home
        self.context = context
        self.enabled = context.enabled
        self.names = Pseudonyms(enabled=self.enabled)
        self._scrub_paths = False
        self._leftover_names: Pseudonyms | None = None
        self._fallback: disclosure.AliasBook | None = None
        self._identity_patterns: list[tuple[str, re.Pattern, dict[str, str]]] | None = None

    @property
    def level(self) -> str:
        return self.context.level

    def _local_resolver(
        self, value: str, base: str | None, outside: Callable[[str], str]
    ) -> str:
        return disclosure.local_path(
            value, repo_root=self.context.repo_root, home=self.home, base=base,
            outside=outside,
        )

    def fresh_names(self, *, detailed: bool = False) -> Pseudonyms:
        """A new per-render Pseudonyms for this redactor. `detailed` is the
        run sinks' request for the local level's real names and P19 paths
        (N3b); at share and raw it changes nothing, and any other sink never
        sets it."""
        local = detailed and self.context.level == "local"
        self._scrub_paths = local
        self.names = Pseudonyms(
            enabled=self.enabled, real_names=local,
            resolver=self._local_resolver if local else None,
        )
        return self.names

    def _render_path_text(self, token: str, hit: bool, *, leftover: bool = False) -> str:
        """FA1, the single-path renderer for the path text of a word (docs/DESIGN.md 5.1) at the
        active level. `hit`: the word held a registered value (its path-N is in the run's own
        namespace); otherwise the path is unregistered. local with the P19 resolver (the run
        sinks) or a hit: the P19 path function through the path namespace (repo-relative,
        `~/.../name`, else path-N). share, and local without a resolver: a token inside the home
        directory or under /tmp or /private/tmp gets the structural form (identities first, then
        home `~` and the `/tmp` collapse); every other token, relative ones included, becomes its
        own path-N. `leftover` (scrub_leftover_paths): an unregistered path is numbered in the
        leftover namespace, which `display_path` shares, and a /tmp path is path-N."""
        names = self._leftover() if leftover and not hit else self.names
        if self.level == "local" and (hit or names._resolver is not None):
            return names.path(token) or ""
        token = _collapse_slash_runs(token)
        home = str(self.context.home).rstrip("/")
        inside_home = token.startswith("~/") or (
            bool(home) and (token == home or token.startswith(home + "/"))
        )
        under_tmp = (hit or not leftover) and (
            token in ("/tmp", "/private/tmp") or token.startswith(("/tmp/", "/private/tmp/"))
        )
        if inside_home or under_tmp:
            shown = redact(self._substitute_identities(token), home=self.home)
            if _STRUCTURAL_SHAPE_RE.fullmatch(shown) and not self._keeps_registered(
                token, shown, home,
            ):
                return shown
        return names.path(token) or ""

    def _render_hidden_text(self, token: str, hit: bool, *, leftover: bool = False) -> str:
        """A span that holds whitespace (docs/DESIGN.md 5.1): always its own opaque path-N, in the
        namespace `_render_path_text` would use, never a structural or repo-relative form."""
        names = self._leftover() if leftover and not hit else self.names
        return names.opaque_path(token) or ""

    def _keeps_registered(self, token: str, shown: str, home: str) -> bool:
        """S1 (docs/DESIGN.md 5.1): whether the structural form `shown` of `token` still
        holds, verbatim, a registered value the token encloses, or the part a collapse
        keeps of one (its last two segments; its `~`-relative spelling inside home)."""
        for value in self.names.registered_paths_in(token):
            segments = [part for part in value.split("/") if part]
            needles = {value, "/".join(segments[-2:])}
            if home and value.startswith(home + "/"):
                needles.add("~" + value[len(home):])
            if any(needle and needle in shown for needle in needles):
                return True
        return False

    def _scrub_absolute_paths(self, text: str) -> str:
        """P19 at local: every absolute path in free text goes through the
        one path function, so none is printed as is. Registered in the same
        path namespace as report paths, so one path has one display."""
        if not self._scrub_paths:
            return text
        return scan_paths(
            text, self.names, self._render_path_text, render_hidden=self._render_hidden_text,
        )

    def scrub_local_line(self, text: str) -> str:
        """C3: a fully assembled local line (an AGENT table row, a
        reproduction line) through the P19 scrub, whatever field carried a
        path. Only the run sinks that asked for local detail scrub; share and
        raw lines pass through unchanged."""
        return self._scrub_absolute_paths(text)

    def scrub_leftover_paths(self, text: str) -> str:
        """__call__, with an unregistered absolute path numbered in the leftover namespace (the
        one `display_path` uses): a location outside home and /tmp (a config or state file, an
        installed package) becomes path-N at share and the P19 answer at local (outside home
        and the repository: path-N). A path inside home keeps its `~/...` form. raw is
        unchanged. For a sink whose text is built from a path the command did not read out of a
        run (config warnings, a state-file error, a hook template location, a debug
        traceback)."""
        if not self.enabled or not text:
            return text
        text = scan_paths(
            text, self.names,
            lambda token, hit: self._render_path_text(token, hit, leftover=True),
            render_hidden=lambda token, hit: self._render_hidden_text(token, hit, leftover=True),
        )
        text = self._substitute_identities(text)
        return redact(text, home=self.home)

    def _leftover(self) -> Pseudonyms:
        if self._leftover_names is None:
            local = self.context.level == "local"
            self._leftover_names = Pseudonyms(
                enabled=True, real_names=local,
                resolver=self._local_resolver if local else None,
            )
        return self._leftover_names

    def display_path(self, value: object) -> str:
        """N3b C4: a path the tool itself knows and is about to interpolate
        into a message (a config file, a state file, a template). It is
        redacted as one value, never by regex on the composed message, so a
        space in it leaks no tail. raw: as is. share: the structural home
        form (`~/.../name`) inside home, else path-N. local: the P19 answer."""
        text = os.fspath(value) if isinstance(value, os.PathLike) else str(value)
        if self.level == "raw" or not text:
            return text
        if self.level == "share":
            home = str(self.context.home).rstrip("/")
            if home and (text == home or text.startswith(home + "/")):
                parts = text[len(home):].strip("/").split("/")
                if parts == [""]:
                    return "~"
                return f"~/{parts[0]}" if len(parts) == 1 else f"~/.../{parts[-1]}"
        return self._leftover().path(text) or ""

    def exception_text(self, exc: BaseException) -> str:
        """str(exc) with the paths the exception itself names (an OSError's
        filenames) replaced by display_path before anything else, then the
        leftover-path scrub for what remains."""
        text = str(exc)
        if self.level == "raw":
            return text
        if isinstance(exc, OSError):
            # each filename is rendered once, as one value, and the message around it is scanned
            # piece by piece: the finished text is not scanned again (docs/DESIGN.md 5.1)
            spellings = sorted(
                {
                    repr(name): name for name in (exc.filename, exc.filename2)
                    if isinstance(name, str) and name
                }.items(),
                key=lambda item: len(item[0]), reverse=True,
            )
            if spellings:
                splitter = re.compile("(" + "|".join(re.escape(r) for r, _ in spellings) + ")")
                shown = dict(spellings)
                return "".join(
                    repr(self.display_path(shown[piece])) if index % 2
                    else self.scrub_leftover_paths(piece)
                    for index, piece in enumerate(splitter.split(text))
                )
        return self.scrub_leftover_paths(text)

    @property
    def book(self) -> disclosure.AliasBook:
        """The alias book a share sink asks. A command that registered its
        identities attached one to the context; a caller that did not (a
        direct render call, a sink Part 2 has not wired yet) gets a
        positional-only book of its own, so no sink can ever print a stable
        or real name by lacking one."""
        if self.context.book is not None:
            return self.context.book
        if self._fallback is None:
            self._fallback = disclosure.fallback_book()
        return self._fallback

    def identity_text(self, kind: str, real_id: str) -> str:
        """P10 rendered form of an identity at share: a:<alias>,
        s:<alias>, project:<alias>; positional (agent-N) on fallback."""
        return disclosure.rendered_form(kind, self.book.alias(kind, real_id))

    def _identity_regexes(self):
        if self._identity_patterns is None:
            by_kind: dict[str, dict[str, str]] = {}
            for kind, real_id in self.context.identities:
                by_kind.setdefault(kind, {})[real_id] = kind
            patterns = []
            # Project slugs first: they are the longest and contain hyphens
            # the other kinds' boundaries would otherwise split on.
            for kind in ("project", "session", "agent"):
                ids = by_kind.get(kind)
                if not ids:
                    continue
                alternation = "|".join(
                    re.escape(real_id) for real_id in sorted(ids, key=len, reverse=True)
                )
                if kind == "project":
                    pattern = re.compile(
                        r"(?<![A-Za-z0-9_])(?:" + alternation + r")(?![A-Za-z0-9_-])"
                    )
                else:
                    pattern = re.compile(
                        r"(?<![0-9A-Za-z])(?:" + alternation + r")(?![0-9A-Za-z])"
                    )
                patterns.append((kind, pattern, ids))
            self._identity_patterns = patterns
        return self._identity_patterns

    def _substitute_identities(self, text: str) -> str:
        """P10/P11 at share: every collected identity quoted in free text
        becomes its rendered form (the run's own project slug before
        structural redaction, the order apply_paths already uses). Any other
        id or slug keeps the structural backstop."""
        if self.context.level != "share" or not self.context.identities:
            return text
        for kind, pattern, _ids in self._identity_regexes():
            text = pattern.sub(
                lambda match, kind=kind: self.identity_text(kind, match.group(0)), text
            )
        return text

    def __call__(self, text: str) -> str:
        """Structural redact(), plus (docs/DESIGN.md 5.1, M1 review finding
        1) substituting any report_path value already registered on `names`
        -- run BEFORE redact() itself, not after, so a report_path under the
        home directory is replaced by its own path-N pseudonym wholesale
        rather than first being collapsed by redact()'s home/tmp rules into
        a "~/.../basename" shape that no longer contains the raw substring
        apply_paths needs to match. This is the one exception to "evidence/
        diagnostics/metrics never see a pseudonym" (5.2): report_path is a
        self-reported claim value, not a faithful transcript quote, and must
        never appear raw by default wherever it is rendered."""
        if not self.enabled or not text:
            return text
        text = scan_paths(
            text, self.names, self._render_path_text, render_hidden=self._render_hidden_text,
        )
        text = self._substitute_identities(text)
        return redact(text, home=self.home)

    def check_text(self, value: str | None) -> str | None:
        """A declared check command as a sink prints it (docs/DESIGN.md 4.11, 5.1): `check-N`
        at share, as is at raw, and at local the real command after the one free-text path
        scanner (so no absolute path survives in `checks[]`, `steps[]` or the status line)."""
        shown = self.names.check(value)
        if self.level == "local" and shown:
            return self(shown)
        return shown

    def title_parts(self, parts) -> str:
        """A title built from structured parts (docs/DESIGN.md 4.13.4, 5.1; F fix 7, W7-2): the
        fixed template with its one `{path}` placeholder filled by the path rendered ONCE, never
        scanned again. `parts` has `.template` and `.path` (`models.TitleParts`). raw: the real
        path. share and local: the path's registered pseudonym (at local the P19 display), else
        what the plain call renders for the path alone, which is what `metrics.paths` and
        `metrics.claims.report_path` print, so the title's `path-N` is theirs. The words around
        the placeholder are the tool's own and are never given to the scanner."""
        head, _sep, tail = parts.template.partition("{path}")
        if not self.enabled:
            return head + parts.path + tail
        shown = self.names.registered_path(parts.path)
        if shown is None:
            shown = self(parts.path)
            if shown.endswith(_HIDDEN_MARK):
                # the value is one path, so nothing but a path was hidden: no marker
                shown = shown[:-len(_HIDDEN_MARK)]
        if self._scrub_paths:
            shown = _CONTROL_RE.sub(" ", shown)
        return head + shown + tail

    def title(self, text: str) -> str:
        """Finding.title only (docs/DESIGN.md 5.1/5.2): everything __call__
        already does (structural redact() plus path substitution), followed
        by pseudonym substitution over every name `names` has registered so
        far. Never call this on evidence, a diagnostic, or a metrics value --
        see the class docstring."""
        if not self.enabled or not text:
            return text
        # N3b S1: registered names first, so a label that itself holds a
        # UUID, email, or project-slug shape is replaced whole before the
        # structural pass rewrites part of it.
        shown = self(self.names.apply(text))
        if self._scrub_paths:
            # N3b S9: a title is one line; no control character survives it. Flattened after
            # the scan (F fix 5), so a backslash and a CRLF stay a line continuation for it.
            shown = _CONTROL_RE.sub(" ", shown)
        return shown
