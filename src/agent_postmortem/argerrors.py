"""Parser and usage errors that never echo what the user typed (N3b P13,
docs/DESIGN.md 4.13.5).

A usage error is printed before any command has built its disclosure
context, so the level is decided from the raw argv alone: scan_level().
Every original argv token that is not part of the tool's own vocabulary (a
known option string, a subcommand name, or an option choice) is classified
and replaced in the message as a span: ErrorRenderer.render() replaces the
exact token (longest first, delimited so a short value never rewrites a
letter inside a word), never re-tokenizing argparse's prose. At raw the
message passes through unchanged.

Stdlib only. Part 2 reuses ErrorRenderer for handler-level errors (no run
found, ambiguous run, bad date).
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from . import disclosure
from .redact import _UUID_RE

# Commands that take --local/--raw/--no-redact (P12). watch, demo, and hook
# take no level flag, so an error from one of them is always rendered at
# share, whatever the argv says.
LEVEL_COMMANDS = frozenset({"scan", "run", "rules", "apply", "trend", "rescue"})

_LEVEL_FLAGS = {"--local": "local", "--raw": "raw", "--no-redact": "raw"}

# A character that would make a token a fragment of a longer word, option,
# number, or path in a message.
_LEFT = r"(?<![\w./~-])"
_RIGHT = r"(?![\w./~-])"


def _is_malformed_level_spelling(token: str) -> bool:
    """--raw=1, --local=yes, --loc, --no-red: spellings that are not exactly
    one of the three level flags but could be read as one."""
    base = token.split("=", 1)[0]
    if base in _LEVEL_FLAGS:
        return "=" in token
    return len(base) > 2 and any(flag.startswith(base) for flag in _LEVEL_FLAGS)


def scan_level(tokens: Iterable[str]) -> str:
    """The disclosure level an error message is rendered at: "share",
    "local", or "raw". Reads tokens before a bare `--` only. Exactly one of
    --local, --raw, --no-redact (written exactly) and nothing malformed fixes
    its level; two of them, a malformed spelling, no flag at all, or a
    command that takes no level flag (watch, demo, hook) fixes share."""
    found: list[str] = []
    malformed = False
    command: str | None = None
    for token in tokens:
        if token == "--":
            break
        if command is None and not token.startswith("-"):
            command = token
        if token in _LEVEL_FLAGS:
            found.append(token)
        elif token.startswith("--") and _is_malformed_level_spelling(token):
            malformed = True
    if command not in LEVEL_COMMANDS or malformed or len(found) != 1:
        return "share"
    return _LEVEL_FLAGS[found[0]]


class ErrorRenderer:
    """Replace the user's own words in an error message. `tokens` is the
    original argv; `known` is the set of the tool's own words (option
    strings, subcommand names, choices), shared by reference so a parser
    tree can fill it after the renderer is built."""

    def __init__(self, tokens: Iterable[str], known: set[str], level: str) -> None:
        self.tokens = list(tokens)
        self.known = known
        self.level = level
        # numbering shared by every span() of one command (handler errors)
        self._span_counters: dict[str, int] = {}
        self._span_seen: dict[str, str] = {}

    def _classify(
        self, token: str, counters: dict[str, int], seen: dict[str, str],
        forced: str | None = None,
    ) -> str:
        """The replacement for one user-supplied token. A path (contains a
        slash, or starts with ~ or .) is path-N (the share path rule; at
        local the same for now, P19's full rule arrives with Part 1b and is
        never absolute); a UUID is uuid:<last4>; a canonical agent id is
        agent-N (positional: no alias book exists for an error); anything
        else is value-N."""
        if token in seen:
            return seen[token]
        if forced == "path":
            kind = "path"
        elif forced is None and ("/" in token or token.startswith(("~", "."))):
            kind = "path"
        elif _UUID_RE.fullmatch(token):
            replacement = f"uuid:{token[-4:]}"
            seen[token] = replacement
            return replacement
        elif forced is None and disclosure.is_canonical_id("agent", token):
            kind = "agent"
        else:
            kind = "value"
        counters[kind] = counters.get(kind, 0) + 1
        seen[token] = f"{kind}-{counters[kind]}"
        return seen[token]

    def span(self, token: str, kind: str | None = None) -> str:
        """Handler-level errors (N3b P13): one value the user supplied, as it
        may appear in a message at this level. At raw the value itself; at
        share and local its replacement (path-N, uuid:<last4>, agent-N,
        value-N), numbered across every span of one command. `kind` "path"
        forces the path form for a value known to be a path whatever its
        spelling (a bare directory name). The caller builds the rest of the
        message from fixed text, so no prose is ever re-tokenized."""
        if self.level == "raw":
            return token
        return self._classify(token, self._span_counters, self._span_seen, kind)

    def _mapping(self) -> dict[str, str]:
        counters: dict[str, int] = {}
        seen: dict[str, str] = {}
        mapping: dict[str, str] = {}
        for token in self.tokens:
            if token == "" or token == "--" or token in self.known:
                continue
            if token.startswith("--") and "=" in token:
                name, _, value = token.partition("=")
                # The value part of every `--option=value` is the user's, whether
                # or not the name part is an exact option string (argparse also
                # accepts an unambiguous abbreviation such as `--verb=...`, and
                # echoes the value alone).
                if value and value not in self.known:
                    mapping[value] = self._classify(value, counters, seen)
                if name in self.known:
                    continue
            mapping[token] = self._classify(token, counters, seen)
        return mapping

    def render(self, message: str) -> str:
        if self.level == "raw" or not message:
            return message
        mapping = self._mapping()
        if not mapping:
            return message
        spellings: dict[str, str] = {}
        for token, replacement in mapping.items():
            spellings[token] = replacement
            quoted = repr(token)[1:-1]  # how argparse's %r spells it
            spellings.setdefault(quoted, replacement)
        pattern = re.compile(
            _LEFT + "(?:" + "|".join(
                re.escape(spelling) for spelling in sorted(spellings, key=len, reverse=True)
            ) + ")" + _RIGHT
        )
        return pattern.sub(lambda match: spellings[match.group(0)], message)
