"""Redact strings before they reach a report, a JSON export, or a terminal.

redact() is the single entry point; every other module that prints text a
stranger might read calls it (or the Redactor wrapper) instead of formatting
raw paths or ids itself. See docs/DESIGN.md section 4.8 for the exact rules.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

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
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

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
# nothing there is followed immediately by a slash.
_HOME_PATH_COLLAPSE_RE = re.compile(r"~/(?:[^/\n]+/)+([^/\s]+)")

# The same collapse for a /tmp or /private/tmp scratch path, but keeping its
# first segment (the scratch directory itself, for example a fixture or run
# id) rather than hiding everything: unlike a home directory, /tmp's first
# segment is not routinely a project or client name, and several of this
# tool's own detectors and fixtures point straight at
# "/tmp/<fixed-name>/<file>" with nothing to hide there at all. Only a
# deeper path -- something beyond that first segment -- gets collapsed.
_TMP_PATH_COLLAPSE_RE = re.compile(
    r"(/(?:private/)?tmp)/([^/\s]+)/(?:[^/\n]+/)+([^/\s]+)"
)

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

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._maps: dict[str, dict[str, str]] = {category: {} for category in self._CATEGORIES}

    def _pseudonym(self, category: str, value: str | None) -> str | None:
        if not self.enabled or not value:
            return value
        mapping = self._maps[category]
        if value not in mapping:
            mapping[value] = f"{category}-{len(mapping) + 1}"
        return mapping[value]

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

    def path(self, value: str | None) -> str | None:
        """A report_path claim value (docs/DESIGN.md 5.1, M1 review finding
        1): its own "path" namespace, numbered in order of first appearance
        like every other category. Unlike agent/desc/phase/type/target, a
        registered path is substituted everywhere the plain Redactor renders
        text (see apply_paths, used by Redactor.__call__), not only inside
        Finding.title -- a relative path or an absolute path outside home
        and /tmp must never survive structural redact() unchanged the way it
        used to."""
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

    def apply_paths(self, text: str) -> str:
        """Replace every report_path value registered so far (the "path"
        namespace) wherever it occurs verbatim in `text` -- a plain substring
        replace, not apply()'s word-boundary anchored one, since a path's own
        characters (/, ., -) are not all "word" characters and a path is
        never partially a whole word to begin with. Longest path first, so
        one path that is a prefix of another is never partially clobbered.
        Called by Redactor.__call__ itself (docs/DESIGN.md 5.1), unlike
        apply(), which only title() calls."""
        if not self.enabled or not text:
            return text
        result = text
        for original, pseudonym in sorted(
            self._maps["path"].items(), key=lambda pair: len(pair[0]), reverse=True
        ):
            if not original:
                continue
            result = result.replace(original, pseudonym)
        return result


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

    def __init__(self, enabled: bool = True, home: Path | None = None) -> None:
        self.enabled = enabled
        self.home = home
        self.names = Pseudonyms(enabled=enabled)

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
        return redact(self.names.apply_paths(text), home=self.home)

    def title(self, text: str) -> str:
        """Finding.title only (docs/DESIGN.md 5.1/5.2): everything __call__
        already does (structural redact() plus path substitution), followed
        by pseudonym substitution over every name `names` has registered so
        far. Never call this on evidence, a diagnostic, or a metrics value --
        see the class docstring."""
        if not self.enabled or not text:
            return text
        return self.names.apply(self(text))
