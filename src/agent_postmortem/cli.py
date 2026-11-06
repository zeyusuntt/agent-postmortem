"""postmortem: turn a Claude Code Workflow run directory into a verdict.

Subcommands: scan (a whole fleet of runs), run (one run), rules (what to do
about the findings; propose/review are fleet-wide verbs under it), apply
(print a rule's diff against a file you maintain -- never writes it), trend
(weekly counts and before/after rates). See docs/DESIGN.md section 4.7/9 for
the full CLI contract and exit codes.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import json
import math
import os
import pickle
import re
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

from . import (
    __version__,
    argerrors,
    config,
    detectors,
    disclosure,
    hooks_cli,
    live,
    observe,
    reader,
    report,
    rescue,
    rules,
    severity,
    state,
    status,
    statusline,
    trend,
    watch,
    workers,
)
from .detectors import claim_contradiction
from .disclosure import FIRST_LINE, DisclosureContext
from .models import Finding, Run
from .redact import Redactor

# docs/DESIGN.md 8.2: the demo run ships inside the package (no second copy
# under tests/fixtures/), so `agent-postmortem demo` needs no --root and
# works from a fresh install with no real run data on disk.
#
# docs/DESIGN.md 8.2's D4: resolved through importlib.resources rather than
# Path(__file__).parent, and turned into a real directory only at the point
# of use (_demo_project_dir) -- a package unpacked in site-packages is a real
# filesystem path already, but reader.load_run's iterdir()/open() calls have
# nothing to open when the package is imported straight out of a wheel .whl
# (itself a zip). importlib.resources.as_file extracts a directory only from
# Python 3.12 on; on 3.11 such an import gets a clear message.
# N3b S12: the demo renders with a home directory that matches no account on
# any machine, so the bare-account-name rule never rewrites a word of it.
_DEMO_HOME = Path("/zz-demo-home/zz-no-account")
# The demo is a run in the standard layout (docs/DESIGN.md 4.2, 8.2): the
# project directory holds the parent session transcript and the run's
# <session>/subagents/workflows/ tree. The project directory is materialized
# as a whole, so a zipped package still has the parent transcript next to the
# run; _DEMO_RUN_PARTS is the run's path inside it.
_DEMO_PROJECT_REF = resources.files("agent_postmortem") / "demo" / "-home-user-project"
_DEMO_RUN_PARTS = (
    "00000000-0000-4000-8000-000000000000", "subagents", "workflows", "wf_00000000-d30",
)


# importlib.resources.as_file extracts a directory traversable only from Python 3.12 on.
_AS_FILE_TAKES_DIRECTORIES = sys.version_info >= (3, 12)


class DemoUnavailable(Exception):
    """The packaged demo cannot be read as a directory on this interpreter."""


@contextlib.contextmanager
def _demo_project_dir(ref) -> Iterator[Path]:
    """The packaged demo project directory `ref` (an importlib.resources
    traversable) as a real directory (docs/DESIGN.md 8.2, D4). A package on
    the filesystem, which every pip, uv, and pipx install is, is used in
    place. A package imported from a zip needs importlib.resources.as_file,
    which extracts a directory only from Python 3.12 on; on 3.11 the demo
    says so instead of failing (the tool writes nothing itself outside its
    state directory, so it does not copy the tree out on its own)."""
    if isinstance(ref, Path):
        yield ref
        return
    if not _AS_FILE_TAKES_DIRECTORIES:
        raise DemoUnavailable(
            "the demo needs agent-postmortem installed on disk on Python 3.11; "
            "install the wheel with pip, uv, or pipx instead of importing it from the zip"
        )
    with resources.as_file(ref) as project:
        yield project


def _default_root() -> Path:
    """~/.claude/projects, overridable by CLAUDE_CONFIG_DIR -- the first
    entry when it is comma-separated -- for parity with ccusage (docs/
    DESIGN.md 4.8). Read fresh on every call (not a module-level constant):
    _build_parser() calls this once per invocation, so a test that sets the
    env var before calling main() is honored without reimporting anything.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    first_entry = config_dir.split(",", 1)[0].strip()
    if first_entry:
        return Path(first_entry).expanduser() / "projects"
    return Path("~/.claude/projects").expanduser()


def _build_context(level: str, *, repo_root: str | None = None) -> DisclosureContext:
    """N3b D-a: the one disclosure context a command builds, once, from its
    parsed level. No alias book in Part 1a (Part 1b registers identities and
    attaches one); `repo_root` stays None until a command that already made
    an observation passes it (D-n)."""
    return DisclosureContext(level=level, repo_root=repo_root)


def _render_guarded(
    *, level: str, identities: list[tuple[str, str]], repo_root: str | None,
    command: str, render,
) -> str:
    """N3b P8/P9/D-b/D-c for a one-shot text command: render the whole output
    to a string with one registered book.

    At share the command registers every identity it collected in one locked
    registration, renders, and, if the book was available when rendering
    started and degraded during it (a slow-path registration failed), renders
    the whole output once more with a positional-only book built from the
    same identities, so one output never mixes stable and positional aliases.
    When the final book is unavailable the one-line diagnostic goes to
    stderr, never stdout. local and raw render no alias, so they register
    nothing and print no diagnostic. A share render with nothing collected
    (a sink whose text can show no identity) registers nothing either: there
    is no alias to make stable, so no state is created and no diagnostic is
    printed for it.

    `render(redactor)` returns the output text."""
    if level != "share" or not identities:
        context = DisclosureContext(
            level=level, repo_root=repo_root, identities=tuple(identities),
        )
        return render(Redactor(context=context))

    def build(book: disclosure.AliasBook) -> str:
        context = DisclosureContext(
            level=level, book=book, repo_root=repo_root, identities=tuple(identities),
        )
        return render(Redactor(context=context))

    book = disclosure.register(identities)
    started_available = book.available
    output = build(book)
    if started_available and not book.available:
        book = disclosure.fallback_book(identities)
        output = build(book)
    if not book.available:
        print(f"agent-postmortem {command}: {disclosure.UNAVAILABLE_DIAGNOSTIC}", file=sys.stderr)
    return output


def _json_guarded(
    *, level: str, identities: list[tuple[str, str]], repo_root: str | None,
    command: str, build,
) -> str:
    """N5a-1: a versioned --json payload rendered through the same guarded
    registration its text sibling uses (one locked registration at share, one
    positional re-render when the book degrades mid-render, the one stderr
    diagnostic), so a JSON output never mixes stable and positional aliases.
    `build(redactor)` returns the payload dict; the result is the text to
    print."""
    return _render_guarded(
        level=level, identities=identities, repo_root=repo_root, command=command,
        render=lambda redactor: json.dumps(build(redactor), indent=2) + "\n",
    )


def _level_of(args: argparse.Namespace) -> str:
    """The parsed disclosure level of a command that takes a level flag."""
    return args.level


def _add_level_flags(parser: argparse.ArgumentParser) -> None:
    """N3b P12: the disclosure level flags of a command that had --no-redact
    in v0.2. One mutually exclusive group; the parsed level lands in
    args.level ("share" when no flag is given). --no-redact stays as an
    alias of --raw."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--local", dest="level", action="store_const", const="local",
        help=(
            "Print real labels and agent ids but never an absolute path, for your own "
            "eyes. Do not paste the result."
        ),
    )
    group.add_argument(
        "--raw", dest="level", action="store_const", const="raw",
        help="Print raw paths, ids, and names with no redaction. Do not paste the result.",
    )
    group.add_argument(
        "--no-redact", dest="level", action="store_const", const="raw",
        help="Alias of --raw.",
    )
    parser.set_defaults(level="share")


def _redactor_for(args: argparse.Namespace) -> Redactor:
    return Redactor(context=_build_context(_level_of(args)))


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose own usage errors exit 1, not argparse's default 2.

    Exit code 2 is reserved for "no runs found" (docs/DESIGN.md section 4.7);
    a bad flag is a usage error and must exit 1.

    N3b P13: `error_renderer` (an argerrors.ErrorRenderer) rewrites the
    message before it is printed, so a usage error never echoes what the user
    typed at share or local. Every nested subparser is built with the same
    renderer, so `rules propose` and `rescue plan` errors render the same way
    as a top-level one.
    """

    def __init__(self, *args, error_renderer: argerrors.ErrorRenderer | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._error_renderer = error_renderer

    def add_subparsers(self, **kwargs):
        if self._error_renderer is not None:
            kwargs.setdefault(
                "parser_class",
                functools.partial(_Parser, error_renderer=self._error_renderer),
            )
        return super().add_subparsers(**kwargs)

    def error(self, message: str) -> None:
        if self._error_renderer is not None:
            message = self._error_renderer.render(message)
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


def _collect_known_words(parser: argparse.ArgumentParser, known: set[str]) -> None:
    """The tool's own vocabulary: option strings, subcommand names, choices.
    These are never replaced in an error message."""
    for action in parser._actions:
        known.update(action.option_strings)
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                known.add(name)
                _collect_known_words(sub, known)
        elif action.choices:
            known.update(str(choice) for choice in action.choices)


def _bounded_float(flag: str, low: float, high: float):
    """docs/DESIGN.md 4.12 (release finding F11): an argparse `type=`
    callable for a watch flag that must be a finite float within
    [low, high]. Raising argparse.ArgumentTypeError makes _Parser.error
    (above) turn an out-of-range or non-finite value into a plain usage
    error -- exit 1, a message naming the flag -- rather than letting it
    reach watch_run at all (a zero or negative --interval would busy-spin
    or raise inside time.sleep; a huge one could defeat the 60s no-journal
    limit or the --max-hours deadline)."""

    def _parse(raw: str) -> float:
        try:
            value = float(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{flag} must be a number") from exc
        if not math.isfinite(value) or not (low <= value <= high):
            raise argparse.ArgumentTypeError(
                f"{flag} must be a finite number between {low} and {high}"
            )
        return value

    return _parse


_RUN_ID_HELP = (
    "A run id (for example wf_b0000000-002), a unique prefix of one, a session UUID, an "
    "agent transcript stem (agent-<id>), an agent alias (a:<alias>, as printed by a share "
    "report), or a path to a run directory. An alias or UUID that matches more than one "
    "run is an error listing the candidates."
)

_SCAN_EXIT_CODES = """\
exit codes:
  exit 0  every run scanned is CLEAN or DEGRADED and complete
  exit 1  usage error
  exit 2  no runs found under --root
  exit 3  some run's verdict is FAILED (3 wins when 4 also applies)
  exit 4  some run is INCOMPLETE and none is FAILED
"""


# docs/DESIGN.md 4.8 ("Exit codes in --help", field milestone F P11): the epilogs below are
# the contract's text verbatim; tests/test_fc_help.py pins each one against DESIGN.
_RUN_EXIT_CODES = """\
exit codes:
  exit 0  the status is complete and the verdict is CLEAN or DEGRADED
  exit 1  usage error
  exit 2  no such run, or not a run
  exit 3  the verdict is FAILED (3 wins when 4 also applies)
  exit 4  the status is INCOMPLETE and the verdict is not FAILED
"""

_RESCUE_PLAN_EXIT_CODES = """\
exit codes (the first condition that applies wins):
  exit 1  usage error, or a configuration error
  exit 2  no such run
  exit 4  the run may still be running, there is nothing to rescue yet, a finished
          run's risk population is not empty or not complete, the lifecycle
          terminal is unknown, or any UNKNOWN fact is in the packet
  exit 0  otherwise
"""

_RESCUE_STATUS_EXIT_CODES = """\
exit codes (the first condition that applies wins):
  exit 1  usage error, or a configuration error
  exit 2  no such run
  exit 3  any row is NONZERO_OBSERVED
  exit 4  the lifecycle terminal is unknown, any row is NOT_OBSERVED or
          WRITE_AFTER_CHECK, the parent transcript could not be read, any
          NEVER-list match, or no unit at all in a run that is not finished with
          every attempt completed
  exit 0  otherwise
"""

_STATUS_EXIT_CODES = """\
exit codes:
  exit 0  the snapshot was printed
  exit 1  usage error (no session named and CLAUDE_CODE_SESSION_ID not set)
  exit 2  no such session
"""


_WATCH_EXIT_CODES = """\
exit codes (the first condition that applies wins):
  exit 1  usage error
  exit 141  the reader of stdout closed the pipe (128 + SIGPIPE, as a shell
          reports it): watch stops at the line it could not deliver
  exit 2  no journal appeared within 60 seconds (with --session: no such session)
  exit 3  DONE with verdict FAILED
  exit 4  LIMIT (--max-hours, or a malformed journal), or DONE with the lifecycle
          terminal unknown and the verdict not FAILED
  exit 0  DONE otherwise (verdict CLEAN or DEGRADED, with or without the
          (INCOMPLETE) suffix); with --session: ENDED, or another watch already
          follows the session
"""


# docs/DESIGN.md 8.1 (0.4): a wheel built from a git checkout carries the commit it came from
# (hatch_build.py); a release carries nothing. Shown by --version only: `hook capabilities` keeps
# the plain version, which the plugin compares with its manifest (4.14.11).
_BUILD_INFO_FILE = "_build_info.json"
_BUILD_BRANCH_RE = re.compile(r"[A-Za-z0-9._/-]{1,80}\Z")
_BUILD_COMMIT_RE = re.compile(r"[0-9a-f]{7,40}\Z")
_BUILD_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")


def _read_build_info() -> object:
    """The build stamp packaged with this install, or None (a release, or a source checkout)."""
    try:
        text = resources.files("agent_postmortem").joinpath(_BUILD_INFO_FILE).read_text("utf-8")
        return json.loads(text)
    except (OSError, ValueError):
        return None


def _version_text(info: object) -> str:
    """`agent-postmortem <version>`, followed by ` (<branch> <commit>[+dirty], <date>)` when
    `info` is a well-formed build stamp; anything else in it is ignored."""
    plain = f"agent-postmortem {__version__}"
    if not isinstance(info, dict):
        return plain
    branch, commit, date, dirty = (info.get(k) for k in ("branch", "commit", "date", "dirty"))
    if not (
        isinstance(branch, str) and _BUILD_BRANCH_RE.match(branch)
        and isinstance(commit, str) and _BUILD_COMMIT_RE.match(commit)
        and isinstance(date, str) and _BUILD_DATE_RE.match(date)
        and isinstance(dirty, bool)
    ):
        return plain
    return f"{plain} ({branch} {commit}{'+dirty' if dirty else ''}, {date})"


def _build_parser(tokens: list[str] | None = None) -> argparse.ArgumentParser:
    """`tokens` is the original argv; it fixes the level and the words a
    usage error must not echo (argerrors). Without it (tests that only parse)
    errors render at share with nothing to replace."""
    known: set[str] = set()
    renderer = argerrors.ErrorRenderer(
        tokens or [], known, argerrors.scan_level(tokens or []),
    )
    parser = _Parser(
        error_renderer=renderer,
        prog="postmortem",
        description=(
            "Turn the run directories a Claude Code Workflow leaves behind into a "
            "verdict, evidence, and a rule that might have helped."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=_version_text(_read_build_info()),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help=(
            "Scan every run under --root and print the runs that need attention. "
            "--all prints the full one-line-per-run list instead."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_SCAN_EXIT_CODES,
    )
    scan.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search for runs (default: ~/.claude/projects).",
    )
    scan.add_argument(
        "--since", default=None, metavar="YYYY-MM-DD",
        help="Only include runs that started on or after this date.",
    )
    scan.add_argument(
        "--kind", choices=("workflow", "session"), default=None,
        help="Only include runs of this kind (default: both).",
    )
    scan.add_argument(
        "--all", action="store_true",
        help="Print one line per run, chronological, instead of the NEEDS ATTENTION triage list.",
    )
    scan.add_argument(
        "--limit", type=int, default=10, metavar="N",
        help=(
            "Show at most N rows in each of the NEEDS ATTENTION and LIVE NOW lists "
            "(default: 10; ignored with --all)."
        ),
    )
    scan.add_argument(
        "--here", action="store_true",
        help=(
            "Only include runs that worked in this directory or a parent of it (a run's "
            "own transcripts name the directories it worked in)."
        ),
    )
    scan.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    scan.add_argument(
        "--no-cache", action="store_true",
        help="Read and analyze every run, ignoring the run cache and the stored transcript parses "
        "(docs/DESIGN.md 4.14.9).",
    )
    _add_level_flags(scan)

    run = sub.add_parser(
        "run", help="Diagnose one run: verdict, findings with evidence, and the next action.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_RUN_EXIT_CODES,
    )
    run.add_argument(
        "run_id", metavar="RUN_ID|PATH", nargs="?", default=None, help=_RUN_ID_HELP,
    )
    run.add_argument(
        "--here", action="store_true",
        help=(
            "Diagnose the session you are working in (CLAUDE_CODE_SESSION_ID), else the most "
            "recently active run that worked in this directory or a parent of it, instead of "
            "naming one (give either RUN_ID or --here)."
        ),
    )
    run.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    run.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    _add_level_flags(run)
    run.add_argument(
        "--bug-report", action="store_true",
        help="Render a markdown bug report instead of the default text report.",
    )
    run.add_argument(
        "--observe", action="store_true",
        help=(
            "Ask git (read-only) whether the paths a killed or failed attempt wrote are "
            "still dirty, clean, or missing. Off by default (docs/DESIGN.md 4.9)."
        ),
    )
    run.add_argument(
        "--verbose", action="store_true",
        help=(
            "Show info-severity findings and the full v0.1-style FINDINGS list, "
            "including hidden agents' stories."
        ),
    )
    run.add_argument(
        "--evidence", action="store_true",
        help=(
            "Show up to two path:line evidence excerpts under each story (text), or "
            "keep excerpts in each finding's evidence (JSON, --local and --raw only)."
        ),
    )

    rules = sub.add_parser(
        "rules", help="List suggested rules and how many findings back each one."
    )
    rules.add_argument(
        "--run", dest="run_id", metavar="RUN_ID|PATH", default=None,
        help="Limit the count to one run instead of a full scan. " + _RUN_ID_HELP,
    )
    rules.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search for runs (default: ~/.claude/projects).",
    )
    rules.add_argument(
        "--all", action="store_true",
        help="List every rule in the library, not just those with findings.",
    )
    rules.add_argument(
        "--show", metavar="RULE_ID", default=None, help="Print the full text of one rule."
    )
    rules.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    _add_level_flags(rules)

    rules_sub = rules.add_subparsers(dest="rules_command", required=False)

    rules_propose = rules_sub.add_parser(
        "propose",
        help=(
            "Print, per rule backed by findings in at least 3 runs on 2 distinct "
            "days, the evidence and where it belongs. Proposals only; writes nothing."
        ),
    )
    rules_propose.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search for runs (default: ~/.claude/projects).",
    )
    rules_propose.add_argument(
        "--since", metavar="YYYY-MM-DD", default=None,
        help="Only consider runs started on or after this date.",
    )
    rules_propose.add_argument(
        "--run", dest="run_id", metavar="RUN_ID|PATH", default=None,
        help=(
            "Count only this one run and list every rule a finding in it backs "
            "(cannot be combined with --since). " + _RUN_ID_HELP
        ),
    )
    rules_propose.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    _add_level_flags(rules_propose)

    rules_review = rules_sub.add_parser(
        "review",
        help=(
            "Print each adopted rule's detector rate before and after adoption, "
            "and list rules to consider removing. Read-only; you record "
            "an adoption in your own config."
        ),
    )
    rules_review.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search for runs (default: ~/.claude/projects).",
    )
    rules_review.add_argument(
        "--weeks", type=int, default=4,
        help="Weeks before and after adoption to compare (default: 4).",
    )
    rules_review.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    _add_level_flags(rules_review)

    apply_cmd = sub.add_parser(
        "apply",
        help=(
            "Print the rule's managed block as a diff against a target file. "
            "Never writes the target; a human applies the diff themselves."
        ),
    )
    apply_cmd.add_argument(
        "rule_id", metavar="RULE_ID", help="The rule to apply, for example R-RETRY-SAFE."
    )
    apply_cmd.add_argument(
        "--target", type=Path, required=True,
        help="File the rule's managed block would be added to.",
    )
    _add_level_flags(apply_cmd)

    trend = sub.add_parser(
        "trend", help="Show weekly detector counts and before/after rates for applied rules."
    )
    trend.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search for runs (default: ~/.claude/projects).",
    )
    trend.add_argument(
        "--weeks", type=int, default=8, help="Number of ISO weeks to show (default: 8)."
    )
    trend.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    _add_level_flags(trend)

    demo = sub.add_parser(
        "demo", help="Render the packaged synthetic demo run; needs no --root and no real run data."
    )
    demo.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )

    rescue = sub.add_parser(
        "rescue",
        help=(
            "Guided rescue: a read-only decision packet (plan) and observed check "
            "status (status) for a killed or failed run. Never edits, runs a check, "
            "or touches git beyond what --observe already does."
        ),
    )
    rescue_sub = rescue.add_subparsers(dest="rescue_command", required=True)

    rescue_plan = rescue_sub.add_parser(
        "plan",
        help=(
            "Print, per unit of work, the task, what it wrote, its repo state, "
            "declared checks, and the steps a human or the main session should take."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_RESCUE_PLAN_EXIT_CODES,
    )
    rescue_plan.add_argument("run_id", metavar="RUN_ID|PATH", help=_RUN_ID_HELP)
    rescue_plan.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    rescue_plan.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    rescue_plan.add_argument(
        "--evidence", action="store_true",
        help=(
            "Quote the task prompt (plan) or the command excerpt (status) at --local "
            "and --raw, in text and in JSON; the default level prints one note (text) "
            "or no excerpt key (JSON)."
        ),
    )
    _add_level_flags(rescue_plan)

    rescue_status = rescue_sub.add_parser(
        "status",
        help=(
            "Print, per unit of work, which declared check the main session's "
            "transcript shows was run after the run ended, and what it observed."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_RESCUE_STATUS_EXIT_CODES,
    )
    rescue_status.add_argument("run_id", metavar="RUN_ID|PATH", help=_RUN_ID_HELP)
    rescue_status.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    rescue_status.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    rescue_status.add_argument(
        "--evidence", action="store_true",
        help=(
            "Quote the task prompt (plan) or the command excerpt (status) at --local "
            "and --raw, in text and in JSON; the default level prints one note (text) "
            "or no excerpt key (JSON)."
        ),
    )
    _add_level_flags(rescue_status)

    doctor_cmd = sub.add_parser(
        "doctor",
        help=(
            "Say whether alerts can reach a session: the CLI, the plugin, Claude Code's version, "
            "readable transcripts and job files, writable state, this session's workers and the "
            "idle watcher. --test-alert sends one line through the per-call hook."
        ),
    )
    doctor_cmd.add_argument(
        "--session", default="", metavar="SESSION",
        help="A session id or transcript path (default: the caller's session, if any).",
    )
    doctor_cmd.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search (default: ~/.claude/projects).",
    )
    doctor_cmd.add_argument(
        "--test-alert", action="store_true",
        help="Leave one line for the session's next hook call, to see an alert arrive.",
    )
    doctor_cmd.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    _add_level_flags(doctor_cmd)

    watch_cmd = sub.add_parser(
        "watch",
        help=(
            "Read-only follower for one running workflow (RUN_ID) or one session (--session), "
            "including a background worker's blocked or needs-input state: prints START, then "
            "KILL/FAILED/OK/QUIET as they happen, then DONE or LIMIT. Designed to be a Claude "
            "Code Monitor command."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_WATCH_EXIT_CODES,
    )
    watch_cmd.add_argument(
        "run_id", metavar="RUN_ID|PATH", nargs="?", default=None,
        help="A run id, a unique prefix of one, or a path to a run directory.",
    )
    watch_cmd.add_argument(
        "--session", nargs="?", const="", default=None, metavar="SESSION_ID|PATH",
        help=(
            "Follow one session instead of one workflow: its subagents' kills, failures that "
            "left changes, and returns their own transcripts contradict (docs/DESIGN.md 4.14.3). "
            "With no value, the caller's own session (CLAUDE_CODE_SESSION_ID)."
        ),
    )
    watch_cmd.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    watch_cmd.add_argument(
        "--interval", type=_bounded_float("--interval", 0.2, 300), default=None,
        help="Seconds between polls, 0.2 to 300 (default: 5; 2 with --session).",
    )
    watch_cmd.add_argument(
        "--max-hours", type=_bounded_float("--max-hours", 0.01, 168),
        default=watch.DEFAULT_MAX_HOURS,
        help=(
            "Give up and exit 4 if the run has not finished after this many hours, "
            "0.01 to 168 (default: 6)."
        ),
    )
    watch_cmd.add_argument(
        "--quiet-after", type=_bounded_float("--quiet-after", 10, 86400),
        default=watch.DEFAULT_QUIET_AFTER,
        help=(
            "Seconds of no transcript activity before an open attempt is QUIET, "
            "10 to 86400 (default: 300)."
        ),
    )
    watch_cmd.add_argument(
        "--done-grace", type=_bounded_float("--done-grace", 0, 3600),
        default=watch.DEFAULT_DONE_GRACE,
        help=(
            "Seconds every key must stay resolved, with no new start, before DONE is "
            "printed, 0 to 3600; survives the gap between sequential phases (default: 30)."
        ),
    )
    watch_cmd.add_argument(
        "--json", action="store_true", help="Print one JSON object per line instead of text."
    )

    status_cmd = sub.add_parser(
        "status",
        help=(
            "One snapshot of one session: its subagents running, returned, interrupted or failed, "
            "what a stopped one changed, returns their own transcripts contradict, and the "
            "background sessions' native states (docs/DESIGN.md 4.14.4)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_STATUS_EXIT_CODES,
    )
    status_cmd.add_argument(
        "session", metavar="SESSION_ID|PATH", nargs="?", default=None,
        help="A session UUID, its public id (s_...), or its transcript path (default: the "
             "caller's own session, CLAUDE_CODE_SESSION_ID).",
    )
    status_cmd.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search for the session (default: ~/.claude/projects).",
    )
    status_cmd.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    _add_level_flags(status_cmd)

    statusline_cmd = sub.add_parser(
        "statusline",
        help=(
            "Claude Code subagentStatusLine command: reads the rows on stdin and rewrites only "
            "the ones it adds a fact to (docs/DESIGN.md 4.14.7)."
        ),
    )
    statusline_cmd.add_argument("statusline_kind", choices=("subagents",), metavar="subagents")

    hook = sub.add_parser(
        "hook", help="Claude Code plugin hook entry point; reads hook JSON from stdin."
    )
    hook.add_argument(
        "hook_command",
        choices=("check", "session-start", "session-end", "workflow-launched", "agent-launched",
                 "post-tool", "prompt-submit", "pre-bash", "stop-watch", "capabilities"),
        metavar="{check,session-start,session-end,workflow-launched,agent-launched,post-tool,"
                "prompt-submit,pre-bash,stop-watch,capabilities}",
        help="Which hook event this invocation is for.",
    )

    _collect_known_words(parser, known)
    return parser


class _AmbiguousRun(LookupError):
    """A run id argument that matches more than one run. str(exc) is v0.2's
    message (what raw prints); the parts let share and local build theirs from
    fixed text and spans (N3b P13). `kind` is the dispatch form that matched:
    "prefix", "uuid", "agent_stem", or "alias" (N6), which picks the advice."""

    def __init__(
        self, prefix: str, root: Path, run_ids: list[str], kind: str = "prefix",
    ) -> None:
        super().__init__(
            f"'{prefix}' matches more than one run under {root}: {', '.join(run_ids)}"
        )
        self.prefix = prefix
        self.root = root
        self.run_ids = run_ids
        self.kind = kind


class _AliasUnavailable(LookupError):
    """An `a:<alias>` RUN_ID while stable aliases are unavailable on this machine
    (docs/DESIGN.md 4.13.6, N6): the alias cannot be resolved, never a traceback."""


_ALIAS_FORM = re.compile(r"a:[0-9a-f]{12,64}")
_STEM_PREFIX = "agent-"


def _runs_for_stem(agent_id: str, refs: list[reader.RunRef]) -> list[reader.RunRef]:
    """The runs whose loaded attempts hold this agent id (candidates, then confirmed)."""
    return reader.runs_holding_agent(refs, agent_id)


def _runs_for_alias(
    alias_hex: str, refs: list[reader.RunRef],
) -> list[reader.RunRef] | None:
    """The runs holding an agent whose registered alias is exactly `alias_hex`, or None
    when stable aliases are unavailable (a read-only lookup, disclosure.lookup_agent_alias)."""
    ids_by_ref = {ref: reader.agent_id_candidates(ref) for ref in refs}
    matched = disclosure.lookup_agent_alias(alias_hex, ids_by_ref)
    if matched is None:
        return None
    return [
        ref for ref in refs if ref in matched and reader.run_holds_agent(ref, matched[ref])
    ]


def _one_run(found: list[reader.RunRef], typed: str, root: Path, kind: str) -> Path:
    if not found:
        raise FileNotFoundError(typed)
    if len(found) > 1:
        raise _AmbiguousRun(typed, root, [ref.run_id for ref in found], kind)
    return found[0].path


def _resolve_run_path(run_id_or_path: str, root: Path) -> Path:
    """Resolve a RUN_ID|PATH argument to exactly one run path. Forms, in order
    (docs/DESIGN.md 4.8, 4.13.6): an existing path, returned as-is; `a:` and 12 to 64
    lowercase hex (an agent alias); a session UUID; `agent-` and an agent id (a transcript
    stem); anything else is an id prefix of either kind of run. The special forms never
    fall through to the prefix rule.

    Raises FileNotFoundError if nothing matches, _AmbiguousRun (message lists the
    candidates) if more than one run matches, and _AliasUnavailable for an alias while
    stable aliases are unavailable. A direct path that exists but is not actually a run
    (an arbitrary file, an unrelated directory) is returned as-is; reader.load_run raises
    NotARun for it and the caller turns that into exit 2 (docs/DESIGN.md 4.2), the same
    path an unrecognized RUN_ID takes.
    """
    direct = Path(run_id_or_path)
    if direct.exists():
        return direct

    if _ALIAS_FORM.fullmatch(run_id_or_path):
        found = _runs_for_alias(run_id_or_path[2:], reader.discover_runs(root))
        if found is None:
            raise _AliasUnavailable(run_id_or_path)
        return _one_run(found, run_id_or_path, root, "alias")
    if reader._UUID_RE.fullmatch(run_id_or_path):
        sessions = reader.discover_runs(root, kinds=frozenset({"session"}))
        found = [ref for ref in sessions if ref.path.stem == run_id_or_path]
        return _one_run(found, run_id_or_path, root, "uuid")
    if run_id_or_path.startswith(_STEM_PREFIX) and reader._AGENT_ID_RE.fullmatch(
        run_id_or_path[len(_STEM_PREFIX):]
    ):
        found = _runs_for_stem(
            run_id_or_path[len(_STEM_PREFIX):], reader.discover_runs(root),
        )
        return _one_run(found, run_id_or_path, root, "agent_stem")

    candidates = [
        ref for ref in reader.discover_runs(root) if ref.run_id.startswith(run_id_or_path)
    ]
    return _one_run(candidates, run_id_or_path, root, "prefix")


_RUN_EXPECTED = (
    "expected a run id, a unique prefix of one, a session UUID, an agent-<id> "
    "transcript stem, a:<alias>, or the path of a run directory"
)
_ALIAS_EXPECTED = (
    "expected a run id, a session UUID, an agent-<id> transcript stem, or a:<alias>"
)
_PREFIX_ADVICE = "expected a longer prefix or the path of a run directory"
_EXACT_ADVICE = "expected one of these run ids or the path of a run directory"
_NO_MATCH_NEXT = "next: agent-postmortem scan --all lists every run id"
_RUN_ID_SHAPE = re.compile(r"wf_[0-9a-f]{8}-[0-9a-f]{3}|s_[0-9a-f]{10,12}")
_HERE_TAIL = "worked in this directory or a parent of it"
# docs/DESIGN.md 4.8 (0.3.4): the first-run empty state. The same lines at every level: they
# name no path or id of the person's own.
_EMPTY_STATE_GUIDANCE = (
    "  agent-postmortem reads the transcripts Claude Code saves there: Workflow runs, and "
    "sessions with the subagents they launched.\n"
    "  next: agent-postmortem demo   (a sample report on a packaged synthetic run)\n"
    "  If Claude Code saves its data somewhere else, pass --root <dir> or set CLAUDE_CONFIG_DIR. "
    "It deletes transcripts older than 30 days by default."
)
_HERE_ELSEWHERE_GUIDANCE = (
    "  agent-postmortem scan lists every run it found; agent-postmortem run <run-id> "
    "diagnoses one."
)
_STANDARD_ROOT = Path("~/.claude/projects")


def _shown_root(args: argparse.Namespace, root: Path, errors) -> str:
    """The root as an error names it (docs/DESIGN.md 4.8): at raw the path itself; at share
    and local the standard location `~/.claude/projects` home-relative, since it is the same
    on every machine and not private, and any other root as a span (path-N), as every path
    the person typed is (N3b P13)."""
    if getattr(args, "level", "share") == "raw":
        return str(root)
    try:
        standard = Path(root).expanduser() == _STANDARD_ROOT.expanduser()
    except (OSError, RuntimeError):
        standard = False
    return str(_STANDARD_ROOT) if standard else errors.span(str(root), "path")


def _root_has_no_runs(root: Path | None) -> bool:
    """docs/DESIGN.md 4.8: nothing under the root that could be a run. Asked only on an error
    path, so it globs and never parses (reader.has_run_candidates)."""
    return root is not None and not reader.has_run_candidates(root)


def _no_runs_text(
    args: argparse.Namespace, root: Path, unreadable_files: int = 0,
    unreadable_runs: list[str] | None = None,
) -> str:
    """The empty state: where the tool looked (and whether it exists), what it reads, and
    what to run next. Exit code 2 is unchanged (4.6)."""
    level = getattr(args, "level", "share")
    shown = _shown_root(args, root, _errors_for(args) if level != "raw" else None)
    try:
        missing = not Path(root).exists()
    except OSError:
        missing = False
    tail = " (it does not exist yet)" if missing else ""
    extra = ""
    if unreadable_files:
        extra += (
            f"  {unreadable_files} transcript {_plural_word(unreadable_files, 'file')} there "
            f"{'had' if unreadable_files == 1 else 'have'} no readable line (not JSON, or nested "
            "too deeply)\n"
        )
    if unreadable_runs:
        extra += (
            f"  {len(unreadable_runs)} {_plural_word(len(unreadable_runs), 'run')} there could not "
            f"be read ({_some_ids(unreadable_runs)})\n"
        )
    return f"postmortem: no runs found under {shown}{tail}\n{extra}{_EMPTY_STATE_GUIDANCE}"


def _resolve_or_report(
    args: argparse.Namespace, root: Path,
) -> tuple[Path | None, int | None]:
    """(path, None) on success, (None, exit code) after printing the error: the one
    place run, rescue plan, rescue status, and rules --run turn a RUN_ID into a path
    (docs/DESIGN.md 4.8: rescue and rules resolve exactly as run does)."""
    try:
        return _resolve_run_path(args.run_id, root), None
    except FileNotFoundError:
        print(_run_error(args, "no_match", root=root), file=sys.stderr)
        return None, 2
    except _AmbiguousRun as exc:
        print(_run_error(args, "ambiguous", exc=exc), file=sys.stderr)
        return None, 1
    except _AliasUnavailable:
        print(_run_error(args, "alias_unavailable"), file=sys.stderr)
        return None, 2


def _alias_unavailable_text(shown: str) -> str:
    return (
        f"postmortem: stable aliases unavailable on this machine, so '{shown}' cannot be "
        f"resolved ({_ALIAS_EXPECTED})"
    )


def _run_error(
    args: argparse.Namespace, kind: str, *, root: Path | None = None,
    exc: _AmbiguousRun | None = None, unreadable_files: int = 0,
    unreadable_runs: list[str] | None = None,
) -> str:
    """The stderr text of a handler-level run error (N3b P13). At raw, v0.2's
    message exactly. At share and local, fixed text around spans of what the
    user supplied (path-N, value-N, uuid:<last4>, agent-N), and the kind of
    argument expected; a run id listed as a candidate is not private (it is the
    id `scan` prints) unless it does not have a run id's shape."""
    level = getattr(args, "level", "share")
    if kind in ("no_match", "here_no_match") and _root_has_no_runs(root):
        kind = "no_runs"  # docs/DESIGN.md 4.8: nothing under the root at all is the empty state
    if kind == "no_runs":
        assert root is not None
        return _no_runs_text(args, root, unreadable_files, unreadable_runs or [])
    if level == "raw":
        return {
            "no_match": lambda: f"postmortem: no run matches '{args.run_id}' under {root}",
            "ambiguous": lambda: f"postmortem: {exc}",
            "not_a_run": lambda: f"postmortem: '{args.run_id}' is not a run",
            "alias_unavailable": lambda: _alias_unavailable_text(args.run_id),
            "here_no_match": lambda: (
                f"postmortem: no run under {root} {_HERE_TAIL}\n{_HERE_ELSEWHERE_GUIDANCE}"
            ),
            "scan_here_no_match": lambda: (
                f"postmortem: no run under {root} that matches the requested filters "
                f"{_HERE_TAIL}"
            ),
        }[kind]()
    errors = _errors_for(args)
    if kind == "no_match":
        # a public run id is what scan prints, so it is repeated as typed (0.4, DESIGN 4.8)
        typed = args.run_id
        shown = typed if _RUN_ID_SHAPE.fullmatch(typed or "") else errors.span(typed)
        return (
            f"postmortem: no run matches '{shown}' under "
            f"{_shown_root(args, root, errors)} ({_RUN_EXPECTED})\n"
            f"  {_NO_MATCH_NEXT}"
        )
    if kind == "ambiguous":
        assert exc is not None
        shown_root = errors.span(str(exc.root), "path")
        shown_prefix = errors.span(exc.prefix)
        ids = ", ".join(
            run_id if _RUN_ID_SHAPE.fullmatch(run_id) else errors.span(run_id, "value")
            for run_id in exc.run_ids
        )
        advice = _PREFIX_ADVICE if exc.kind == "prefix" else _EXACT_ADVICE
        return (
            f"postmortem: '{shown_prefix}' matches more than one run under {shown_root}: "
            f"{ids} ({advice})"
        )
    if kind == "alias_unavailable":
        return _alias_unavailable_text(errors.span(args.run_id))
    if kind == "here_no_match":
        return (
            f"postmortem: no run under {_shown_root(args, root, errors)} {_HERE_TAIL}\n"
            f"{_HERE_ELSEWHERE_GUIDANCE}"
        )
    if kind == "scan_here_no_match":
        return (
            f"postmortem: no run under {_shown_root(args, root, errors)} that matches the "
            f"requested filters {_HERE_TAIL}"
        )
    if kind == "not_a_run":
        return (
            f"postmortem: '{errors.span(args.run_id)}' is not a run "
            "(expected a workflow run directory or a session transcript)"
        )
    raise ValueError(f"unknown run error kind: {kind}")


def _since_error(args: argparse.Namespace) -> str:
    """The bad `--since` date message (N3b P13): the format is named, the typed
    value is a span at share and local."""
    level = getattr(args, "level", "share")
    shown = args.since if level == "raw" else _errors_for(args).span(args.since, "value")
    return f"postmortem: --since must be YYYY-MM-DD, got '{shown}'"


def _run_findings(run: Run) -> list[Finding]:
    return detectors.run_detectors(run)


def _load_config_checked(redactor: Redactor) -> tuple[config.Config, int | None]:
    """Load the user config (docs/DESIGN.md 4.10), printing any warnings to
    stderr through the active Redactor; returns (config, exit_code) where
    exit_code is None to keep going or 1 when [detectors].off named a
    critical detector id -- a silenced critical would change the exit code
    (docs/DESIGN.md 4.6), so the CLI refuses to run at all rather than
    quietly drop the id and proceed (a hook takes the opposite path: it
    just never lands in config.detectors_off, silently, since a hook must
    never fail a turn -- docs/DESIGN.md 7.2)."""
    cfg = config.load_config()
    # each known path is rendered once and the text between the paths is scanned piece by piece;
    # the finished warning is printed as it is (docs/DESIGN.md 5.1, F fix 7)
    for warning in cfg.shown_warnings(redactor.display_path, redactor.scrub_leftover_paths):
        print(warning, file=sys.stderr)
    if cfg.critical_rejected:
        ids = ", ".join(sorted(cfg.critical_rejected))
        print(
            redactor(
                f"postmortem: [detectors].off names a critical detector ({ids}); "
                "critical detectors can never be silenced"
            ),
            file=sys.stderr,
        )
        return cfg, 1
    return cfg, None


def _run_files_for_liveness(run: Run) -> list[Path]:
    """Every file whose mtime counts toward unit M3b's 'possibly live' rule
    (docs/DESIGN.md 4.7's scan triage, rule 1a): for a workflow run, every
    file directly inside its wf_* directory (journal, meta, transcripts --
    the same population hooks_cli._unit_files uses for kind='workflow');
    for a session run, its own main .jsonl plus every file under its
    subagents/ directory, when one exists. Never recursive beyond that one
    level: today's layout never nests a run's own files any deeper."""
    if run.kind == "workflow":
        try:
            return [p for p in run.path.iterdir() if p.is_file()]
        except OSError:
            return []
    files = [run.path]
    subagents_dir = run.path.parent / run.path.stem / "subagents"
    if subagents_dir.is_dir():
        try:
            files.extend(p for p in subagents_dir.iterdir() if p.is_file())
        except OSError:
            pass
    return files


def _newest_run_mtime(run: Run) -> float | None:
    """The newest mtime among _run_files_for_liveness(run), or None when no
    file could be statted at all (docs/DESIGN.md 4.7's scan triage rule 1a)."""
    newest: float | None = None
    for path in _run_files_for_liveness(run):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _top_detector(findings: list[Finding]) -> str | None:
    """The detector with the most major-or-critical findings, or None when
    there are none (docs/DESIGN.md 4.7: a CLEAN run, by definition, has no
    major or critical finding, so scan shows '-' for it rather than
    whichever info detector -- wasted_time on every run -- happened to have
    a finding)."""
    counts: dict[str, int] = {}
    for finding in findings:
        if finding.severity not in ("major", "critical"):
            continue
        counts[finding.detector] = counts.get(finding.detector, 0) + 1
    if not counts:
        return None
    return max(counts, key=lambda detector_id: counts[detector_id])


_RUN_USAGE_BOTH = "postmortem: run takes either RUN_ID or --here, not both"
_RUN_USAGE_NEITHER = "postmortem: run needs one of RUN_ID or --here"


_CALLER_SESSION_ENV = "CLAUDE_CODE_SESSION_ID"


def _caller_session_id() -> str | None:
    """The caller's own session id (docs/DESIGN.md 4.13.6, field milestone F): the value of
    CLAUDE_CODE_SESSION_ID when it is a canonical UUID, lowercased, else None. A hint, not a
    security boundary: it only chooses among runs that already matched the caller's directory."""
    value = os.environ.get(_CALLER_SESSION_ENV)
    if isinstance(value, str) and reader._UUID_RE.fullmatch(value):
        return value.lower()
    return None


def _activity_of(run: Run) -> datetime | None:
    return run.ended_at if run.ended_at is not None else run.started_at


def _here_key(ref: reader.RunRef, run: Run) -> tuple:
    """The `--here` fallback order (docs/DESIGN.md 4.13.6), ascending: a run with an activity
    time beats one without, then the activity time, the run id, and the path."""
    activity = _activity_of(run)
    stamp = report._aware(activity).timestamp() if activity is not None else 0.0
    return (activity is not None, stamp, run.run_id, str(ref.path))


@dataclass
class _HereChoice:
    ref: reader.RunRef
    run: Run
    reason: str  # "your current session" | "most recent activity"
    others: list[tuple[reader.RunRef, Run]]  # every other matching run, best first
    matched: int


def _pick_run_here(root: Path) -> _HereChoice | None:
    """The run `--here` shows (docs/DESIGN.md 4.8, 4.13.6): among the runs whose cwd set holds
    the caller's directory or a parent of it, (1) the session run whose transcript stem equals
    the caller's own session id, else (2) the most recent activity. None when nothing matches
    or the caller's own directory cannot be read."""
    caller = _working_directory()
    if caller is None:
        return None
    matches: list[tuple[reader.RunRef, Run]] = []
    # docs/DESIGN.md 4.13.6 (G5): the workflows of the caller's own session join the set
    owned = reader.caller_workflow_paths(root, _caller_session_id(), caller)
    unreadable: list[str] = []
    for ref in reader.discover_runs(root):
        run = _load_listed_run(ref, unreadable)
        if run is None:
            continue
        if ref.path in owned or reader.run_matches_cwd(run, caller):
            matches.append((ref, run))
    _print_left_out(unreadable)
    if not matches:
        return None
    matches.sort(key=lambda pair: _here_key(*pair), reverse=True)
    chosen = 0
    reason = "most recent activity"
    own = _caller_session_id()
    if own is not None:
        for index, (ref, run) in enumerate(matches):
            if run.kind == "session" and ref.path.stem.lower() == own:
                chosen, reason = index, "your current session"
                break
    ref, run = matches[chosen]
    return _HereChoice(ref, run, reason, matches[:chosen] + matches[chosen + 1:], len(matches))


def _here_line(args: argparse.Namespace, choice: _HereChoice) -> str:
    """The one stderr line of a `run --here` that matched more than one run (docs/DESIGN.md
    4.13.6): run ids and ages only, never a directory, a path, or a session UUID."""
    now = report._now()
    level = getattr(args, "level", "share")

    def shown_id(run_id: str) -> str:
        if level == "raw" or _RUN_ID_SHAPE.fullmatch(run_id):
            return run_id
        return _errors_for(args).span(run_id, "value")

    def age(run: Run) -> str:
        activity = _activity_of(run)
        return "unknown" if activity is None else report._age_string(activity, now, short=True)

    also = ", ".join(
        f"{shown_id(run.run_id)} ({age(run)})" for _ref, run in choice.others[:3]
    )
    return (
        f"postmortem: {choice.matched} runs worked here; showing {shown_id(choice.run.run_id)} "
        f"({choice.reason}); also: {also}"
    )


def _pick_run_path(
    args: argparse.Namespace, root: Path,
) -> tuple[Path | None, Run | None, int | None]:
    """`run`'s run path: RUN_ID resolved as every run-resolving command does, or, with
    --here, the run docs/DESIGN.md 4.13.6 selects together with the run already loaded to
    choose it (rendered as is: no second load, so a run that vanishes between choosing and
    rendering cannot fail the command)."""
    if not args.here:
        path, failure = _resolve_or_report(args, root)
        return path, None, failure
    picked = _pick_run_here(root)
    if picked is None:
        print(_run_error(args, "here_no_match", root=root), file=sys.stderr)
        return None, None, 2
    if picked.matched > 1:
        print(_here_line(args, picked), file=sys.stderr)
    return picked.ref.path, picked.run, None


def _cmd_run(args: argparse.Namespace) -> int:
    if args.here and args.run_id is not None:
        print(_RUN_USAGE_BOTH, file=sys.stderr)
        return 1
    if not args.here and args.run_id is None:
        print(_RUN_USAGE_NEITHER, file=sys.stderr)
        return 1
    root = args.root.expanduser()
    redactor = _redactor_for(args)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    run_path, run, failure = _pick_run_path(args, root)
    if run_path is None:
        return failure

    if run is None:
        try:
            run = reader.load_run(run_path)
        except reader.NotARun:
            print(_run_error(args, "not_a_run"), file=sys.stderr)
            return 2

    # docs/DESIGN.md 12's D-C: `run` always computes and prints findings and
    # the verdict, even for a workflow that has not finished (a started key
    # with no result yet) -- an unfinished-but-retried run is exactly the
    # kind of run a user most wants to inspect, and hiding it behind a bare
    # "no verdict yet" line hid that. render_run_text itself adds the
    # "(unfinished)" header tag (docs/DESIGN.md 4.7); --json and
    # --bug-report keep their own structured contracts, unchanged.
    findings = _run_findings(run)

    # docs/DESIGN.md 4.9: --observe is the only way observe_groups ever runs here; a
    # plain `run` (the default) never starts a git process at all, and text/
    # JSON output is byte-for-byte what it was before this flag existed.
    observation = observe.observe_groups(run) if args.observe else None

    # N3b P9: identities a text render may show, collected once and
    # registered once (see _render_guarded); JSON registers nothing (D-k).
    identities = report.collect_identities(run, findings)
    repo_root = observation.repo_root if observation is not None else None

    if args.bug_report:
        # docs/DESIGN.md 4.10: the bug report is unchanged by this unit --
        # it has its own section order and is meant to show everything, so a
        # silenced detector's finding still appears there.
        print(
            _render_guarded(
                level=_level_of(args), identities=identities, repo_root=repo_root,
                command="run",
                render=lambda r: report.render_bug_report(run, findings, r, args.evidence),
            ),
            end="",
        )
    elif args.json:
        print(
            _json_guarded(
                level=_level_of(args), identities=identities, repo_root=repo_root,
                command="run",
                build=lambda r: report.to_json(
                    run, findings, r, observation, cfg.detectors_off, evidence=args.evidence,
                ),
            ),
            end="",
        )
    else:
        print(
            _render_guarded(
                level=_level_of(args), identities=identities, repo_root=repo_root,
                command="run",
                render=lambda r: report.render_run_text(
                    run, findings, r, observation,
                    verbose=args.verbose, evidence=args.evidence,
                    silenced_detectors=cfg.detectors_off,
                ),
            ),
            end="",
        )

    # docs/DESIGN.md 4.6, 5.3b: exit 3 means the verdict is FAILED (a failed
    # lifecycle terminal or a critical finding), 4 means INCOMPLETE without
    # FAILED; 3 wins when both apply. Over every finding, silenced or not.
    if report.is_failed(run, findings):
        return 3
    if run.status == "INCOMPLETE":
        return 4
    return 0


def _resolve_run_or_exit(
    args: argparse.Namespace, root: Path, redactor: Redactor,
) -> tuple[Run | None, int | None]:
    """(run, None) on success, (None, exit_code) when the caller should
    return immediately -- the run-resolution steps `rescue plan`/`rescue
    status` share verbatim with `_cmd_run` (docs/DESIGN.md 4.8: 'RUN_ID
    resolution as for run')."""
    run_path, failure = _resolve_or_report(args, root)
    if run_path is None:
        return None, failure

    try:
        return reader.load_run(run_path), None
    except reader.NotARun:
        print(_run_error(args, "not_a_run"), file=sys.stderr)
        return None, 2


def _cmd_rescue_plan(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = _redactor_for(args)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code

    run, exit_code = _resolve_run_or_exit(args, root, redactor)
    if run is None:
        return exit_code

    findings = _run_findings(run)
    # docs/DESIGN.md 4.11: rescue plan always runs observe_groups, read-only,
    # the same way `run --observe` does -- there is no flag to turn it off.
    observation = observe.observe_groups(run)
    now = report._now()
    newest_mtime = _newest_run_mtime(run)
    live = report.is_possibly_live(run, newest_mtime, now, hooks_cli._UNKNOWN_QUIET_SECONDS)

    plan = rescue.build_plan(run, observation, findings, cfg, possibly_live=live)

    if args.json:
        print(
            _json_guarded(
                level=_level_of(args), identities=report.collect_identities(run, findings),
                repo_root=observation.repo_root, command="rescue plan",
                build=lambda r: rescue.plan_to_json(run, plan, r, evidence=args.evidence),
            ),
            end="",
        )
    else:
        # N3b P9, D-n: the run's identities registered once; the repository
        # root is the one the observation above already resolved.
        print(
            _render_guarded(
                level=_level_of(args), identities=report.collect_identities(run, findings),
                repo_root=observation.repo_root, command="rescue plan",
                render=lambda r: rescue.render_plan_text(
                    run, plan, r, evidence=args.evidence, caller_session=_caller_session_id(),
                ),
            ),
            end="",
        )

    return plan.exit_code


def _cmd_rescue_status(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = _redactor_for(args)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    # docs/DESIGN.md 4.11 (N5c-2, plan S7): two `[[repos]]` tables with one normalized path
    # cannot be attributed to either, so it is a configuration error, reported before the
    # run is looked for.
    for duplicated in rescue.duplicate_roots(cfg):
        print(
            f"postmortem: [[repos]] names the same repository twice: "
            f"{redactor.display_path(duplicated)}; each repository may be declared once",
            file=sys.stderr,
        )
        return 1

    run, exit_code = _resolve_run_or_exit(args, root, redactor)
    if run is None:
        return exit_code

    observation = observe.observe_groups(run)
    status = rescue.build_status(run, cfg, observation)

    if args.json:
        print(
            _json_guarded(
                level=_level_of(args), identities=report.collect_identities(run, []),
                repo_root=observation.repo_root, command="rescue status",
                build=lambda r: rescue.status_to_json(run, status, r, evidence=args.evidence),
            ),
            end="",
        )
    else:
        print(
            _render_guarded(
                level=_level_of(args), identities=report.collect_identities(run, []),
                repo_root=observation.repo_root, command="rescue status",
                render=lambda r: rescue.render_status_text(run, status, r, evidence=args.evidence),
            ),
            end="",
        )

    return status.exit_code


def _cmd_demo(args: argparse.Namespace) -> int:
    # docs/DESIGN.md 8.2: rendered through the normal run path -- same
    # report renderer, same redaction default, same exit-code rule (D5) --
    # just pointed at the run packaged inside agent_postmortem/demo/
    # instead of a caller-supplied --root, so a first run needs no data on
    # disk. as_file (D4) is what makes the packaged directory readable even
    # when the package itself is a zip; the context manager only needs to
    # span the parse, since findings and the rendered text no longer touch
    # the filesystem afterward.
    redactor = Redactor(context=_build_context("share"))
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    try:
        with _demo_project_dir(_DEMO_PROJECT_REF) as demo_project_path:
            run = reader.load_run(demo_project_path.joinpath(*_DEMO_RUN_PARTS))
            findings = _run_findings(run)
    except DemoUnavailable as exc:
        print(f"agent-postmortem demo: {exc}", file=sys.stderr)
        return 2
    # N3b D-i: the demo's identities in one fixed order, registered in memory
    # under the public demo secret. Never the state directory, never the lock,
    # never the unavailable diagnostic. The JSON uses the same book as the
    # text, so an alias is the same string in both.
    identities = report.collect_identities(run, findings)
    book = disclosure.register_in_memory(identities, disclosure.DEMO_SECRET)
    redactor = Redactor(
        context=DisclosureContext(
            level="share", home=_DEMO_HOME, book=book, identities=tuple(identities),
        ),
    )
    if args.json:
        print(
            json.dumps(
                report.to_json(run, findings, redactor, silenced_detectors=cfg.detectors_off),
                indent=2,
            )
        )
    else:
        print(
            report.render_run_text(
                run, findings, redactor, show_age=False, silenced_detectors=cfg.detectors_off,
            ),
            end="",
        )
    # docs/DESIGN.md 4.6, 5.3b: exit 3 means the verdict is FAILED (a failed
    # lifecycle terminal or a critical finding), 4 means INCOMPLETE without
    # FAILED; 3 wins when both apply. Over every finding, silenced or not.
    if report.is_failed(run, findings):
        return 3
    if run.status == "INCOMPLETE":
        return 4
    return 0


def _empty_scan_totals() -> dict:
    # docs/DESIGN.md 4.7: totals are stratified by kind, never one combined
    # count -- a workflow's "runs with retries" and a session's "subagents"
    # answer different questions and must not be averaged together.
    return {
        "workflow": {"runs": 0, "runs_with_retries": 0, "attempts": 0, "wasted_hours": 0.0},
        "session": {"runs": 0, "subagents": 0, "interrupted": 0},
    }


def _accumulate_scan_totals(totals: dict, run: Run, findings: list[Finding]) -> None:
    if run.kind == "session":
        totals["session"]["runs"] += 1
        totals["session"]["subagents"] += len(run.attempts)
        totals["session"]["interrupted"] += sum(
            1 for attempt in run.attempts if attempt.interrupt is not None
        )
    else:
        totals["workflow"]["runs"] += 1
        totals["workflow"]["attempts"] += len(run.attempts)
        if report.silent_retry_count(run) > 0:
            totals["workflow"]["runs_with_retries"] += 1
        for finding in findings:
            if finding.detector == "wasted_time":
                totals["workflow"]["wasted_hours"] += (
                    finding.metrics.get("wasted_seconds", 0.0) / 3600.0
                )


def _attention_sessions(root: Path, now_ts: float, kind: str | None,
                        here_caller: str | None) -> list[dict]:
    """docs/DESIGN.md 4.7: the background sessions in trouble now:
    STALLED (above) and those blocked on a person or failed, by the job's own class, whoever
    launched them and regardless of --since. One that delivered waits on a decision, no trouble,
    and is not listed. The caller's own workers are marked, from its own transcript."""
    if kind == "workflow":
        return []
    def want(cwd: str | None) -> bool:  # --here, applied before any job is read
        return here_caller is None or (
            isinstance(cwd, str) and (cwd == here_caller or cwd.startswith(here_caller + "/")))

    caller = _caller_session_id()
    main_path = status._job_transcript(root, caller) if caller else None
    mine = status.own_workers(main_path) if main_path is not None else {}
    background = status._background(root.parent / "jobs", root, now_ts, max_reads=None, want=want,
                                    mine=mine)
    return [entry for entry in background["sessions"]
            if entry.get("session") and want(entry.get("cwd")) and (
                entry.get("state") == "stalled" or entry.get("class") in workers.TROUBLE)]


def _fold_blocked(blocked: list[dict], candidates: list[dict], live_now: list[dict],
                  row_ids: set[str], level: str) -> tuple[int, int]:
    """Each blocked or failed background session in NEEDS ATTENTION, the caller's own first, then
    the rest after the stalled ones, replacing that session's own candidate (whose reason it keeps
    after "also:") and its LIVE NOW entry. Another session's older than 3 days is only counted.
    Returns (how many are no scanned run, for the header's run count; how many were counted)."""
    named = level in ("local", "raw")
    older = sum(1 for entry in blocked if status._old_blocked(entry))
    shown = [entry for entry in blocked if not status._old_blocked(entry)]
    for entry in shown:
        public = entry["session"]
        failed = entry.get("state") == "failed"
        reason = "this background session failed" if failed else workers.reason_words(
            entry["class"], entry.get("label") if named else None)
        for existing in [c for c in candidates if c["run"].run_id == public]:
            reason += f" (also: {existing['reason']})"
            candidates.remove(existing)
        live_now[:] = [e for e in live_now if e["run"].run_id != public]
        age = float(entry.get("age_seconds") or 0)
        candidates.append({
            "run": SimpleNamespace(run_id=public), "age": f"for {status._age(age)}",
            "verdict": "FAILED" if failed else (
                "WAITING" if entry.get("class") == workers.TOOL_WAIT else "BLOCKED"),
            "reason": reason,
            "command": status.next_action(entry) if named
            else "agent-postmortem status --local",
            "mark": "dispatched by this session" if entry.get("mine") else None,
            "sort_key": (-2 if entry.get("mine") else -1, 1, 0, 0, -age, 0.0),
        })
    return sum(1 for entry in shown if entry["session"] not in row_ids), older


def _stall_reason(stall: dict) -> str:
    count = stall.get("commands", 1)
    what = f"{count} {_plural_word(count, 'background command')} silent"
    if stall.get("silent_seconds") is not None:
        what += f" {status._age(stall['silent_seconds'])}"
    if stall.get("waiting_on_stdin"):
        what += ", waiting on stdin"
    # only a command's own files are seen, and it may end yet
    return what + ("; nothing will wake it" if stall.get("waiting_on_stdin")
                   else "; may be hung or writing elsewhere, and only its end would wake it")


def _fold_stalled(stalled: list[dict], candidates: list[dict], live_now: list[dict],
                  row_ids: set[str]) -> int:
    """Each STALLED session first in NEEDS ATTENTION, replacing that session's own candidate
    (whose reason it keeps after "also:") and its LIVE NOW entry; the number of them that are
    no scanned run, for the header's run count."""
    for entry in stalled:
        public = entry["session"]
        stall = entry.get("stall") or {}
        reason = _stall_reason(stall)
        # the caller's marker survives the fold, else it comes from the id
        mark = next((c.get("mark") for c in [*candidates, *live_now]
                     if c["run"].run_id == public and c.get("mark")), None)
        caller = _caller_session_id()
        if mark is None and caller and str(entry.get("session_id", "")).lower() == caller:
            mark = "this session"
        if mark is None and entry.get("mine"):
            mark = "dispatched by this session"  # as for a blocked one
        for existing in [c for c in candidates if c["run"].run_id == public]:
            reason += f" (also: {existing['reason']})"
            candidates.remove(existing)
        live_now[:] = [e for e in live_now if e["run"].run_id != public]
        idle = float(entry.get("age_seconds") or 0)
        candidates.append({
            "run": SimpleNamespace(run_id=public), "age": f"idle {status._age(idle)}",
            "verdict": "STALLED", "reason": reason, "command": f"agent-postmortem status {public}",
            "mark": mark, "sort_key": (-2 if entry.get("mine") else -1, 0, 0, 0, -idle, 0.0),
        })
    return sum(1 for entry in stalled if entry["session"] not in row_ids)


def _scan_payload(rows: list[dict], totals: dict, redactor: Redactor,
                  stalled: list[dict] | None = None, blocked: list[dict] | None = None) -> dict:
    """docs/DESIGN.md 5.3b `scan --json`: every field structurally redacted as
    before, except `project`, which is the identity form of 4.13.5 (the alias
    at share, the directory name at local and raw)."""
    shown_rows = []
    for row in rows:
        shown = {}
        for key, value in row.items():
            if key == "project":
                shown[key] = report.project_text(value, redactor)
            else:
                shown[key] = report.redact_value(value, redactor)
        shown_rows.append(shown)
    payload = {
        "schema": report.SCHEMA_VERSION,
        "disclosure": redactor.level,
        "rows": shown_rows,
        "totals": report.redact_value(totals, redactor),
    }
    if stalled:
        # background sessions idle on a command that will not end; the
        # key is present only then, so every other scan document keeps its shape
        payload["stalled"] = [
            {"session": entry["session"], "idle_seconds": entry.get("age_seconds"),
             **{key: (entry.get("stall") or {}).get(key)
                for key in ("commands", "waiting_on_stdin", "silent_seconds")},
             "next_command": f"agent-postmortem status {entry['session']}"}
            for entry in stalled
        ]
    if blocked:
        # present only then, as `stalled` above; ids from --local
        named = redactor.level in ("local", "raw")
        payload["blocked"] = [
            {"session": entry["session"], "state": entry["state"], "class": entry["class"],
             "age_seconds": entry.get("age_seconds"), "mine": bool(entry.get("mine")),
             **({"short": entry.get("short"), "label": entry.get("label"),
                 "next_command": status.next_action(entry)} if named
                else {"next_command": "agent-postmortem status --local"})}
            for entry in blocked
        ]
    return payload


def _plural_word(n: int, word: str) -> str:
    return word if n == 1 else f"{word}s"


def _some_ids(ids: list[str]) -> str:
    shown = ", ".join(ids[:3])
    return shown if len(ids) <= 3 else f"{shown} and {len(ids) - 3} more"


def _scan_skipped_line(
    skipped_lines: list[tuple[str, int]], unreadable_files: int, meta_files: int,
    unreadable_runs: list[str],
) -> str:
    """docs/DESIGN.md 4.8 (0.3.4): one text line saying what the scan could not read; empty when
    it read everything. Run ids are the ids scan prints anyway; no path is named."""
    clauses: list[str] = []
    if skipped_lines:
        lines = sum(count for _run_id, count in skipped_lines)
        runs = len(skipped_lines)
        clauses.append(
            f"{lines} unreadable {_plural_word(lines, 'line')} in {runs} "
            f"{_plural_word(runs, 'run')} ({_some_ids([run_id for run_id, _ in skipped_lines])}), "
            "still scanned"
        )
    if unreadable_files:
        clauses.append(
            f"{unreadable_files} transcript {_plural_word(unreadable_files, 'file')} with no "
            "readable line"
        )
    if meta_files:
        clauses.append(
            f"{meta_files} agent meta {_plural_word(meta_files, 'file')} that could not be read"
        )
    if unreadable_runs:
        clauses.append(
            f"{len(unreadable_runs)} {_plural_word(len(unreadable_runs), 'run')} that could not "
            f"be read ({_some_ids(unreadable_runs)})"
        )
    if not clauses:
        return ""
    return (
        "SKIPPED       " + "; ".join(clauses)
        + "; agent-postmortem run <run-id> --json lists each line under diagnostics"
    )


# docs/DESIGN.md 4.8 (0.3.4): what a command says about a run it could not read at all
_UNREADABLE_RUN = "could not be read (its data is nested too deeply to follow); nothing was judged"


def _load_listed_run(ref: reader.RunRef, unreadable: list[str]) -> Run | None:
    """One discovered run for a command that reads many (docs/DESIGN.md 4.2, 4.8): None for a
    candidate that is not a run, and for a run that cannot be read, whose id joins
    `unreadable`. One bad candidate never aborts the whole command."""
    try:
        return reader.load_run(ref)
    except reader.NotARun:
        return None
    except reader.RunUnreadable as exc:
        unreadable.append(exc.run_id)
        return None


def _print_left_out(unreadable: list[str]) -> None:
    """docs/DESIGN.md 4.8 (0.3.4): the runs a command could not read are named on stderr, so
    JSON on stdout stays one document."""
    if unreadable:
        count = len(unreadable)
        print(
            f"postmortem: {count} {_plural_word(count, 'run')} could not be read and "
            f"{'was' if count == 1 else 'were'} left out ({_some_ids(unreadable)})",
            file=sys.stderr,
        )


# docs/DESIGN.md 4.14.9 item 2: a run whose newest file was written before --since minus one day
# cannot have started on or after --since, so it is never read. The flag exists for the test that
# proves the result set is the same with and without it.
_SINCE_MTIME_PREFILTER = True
_SINCE_MARGIN = timedelta(days=1)
# docs/DESIGN.md 4.14.9 item 1: the stderr progress line.
_PROGRESS_MIN_INTERVAL = 0.5
_PROGRESS_NOTE_MIN_RUNS = 50


def _ref_files(ref: reader.RunRef) -> list[Path]:
    """The files _run_files_for_liveness would name for this ref, found without loading it."""
    if ref.kind == "workflow":
        try:
            return [p for p in ref.path.iterdir() if p.is_file()]
        except OSError:
            return []
    files = [ref.path]
    subagents_dir = ref.path.parent / ref.path.stem / "subagents"
    try:
        files.extend(p for p in subagents_dir.iterdir() if p.is_file())
    except OSError:
        pass
    return files


def _ref_newest_mtime(ref: reader.RunRef) -> float | None:
    newest: float | None = None
    for path in _ref_files(ref):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest


def _has_a_timestamp(ref: reader.RunRef) -> bool:
    """Whether any transcript of the run carries a timestamp in its first 64 KB. The reader
    takes a run's start from its transcripts' events only (a workflow's agent-*.jsonl, a
    session's main and subagent transcripts), never from a journal or a meta file; a run with
    no such timestamp has an unknown start, which --since keeps (4.14.9), so it is read."""
    for path in _ref_files(ref):
        transcript = path.suffix == ".jsonl" and (
            ref.kind != "workflow" or path.name.startswith("agent-"))
        if not transcript:
            continue
        try:
            with path.open("rb") as handle:
                head = handle.read(65536)
        except OSError:
            continue
        for raw in head.split(b"\n"):
            if b'"timestamp"' not in raw:
                continue
            try:
                entry = json.loads(raw)
            except (ValueError, RecursionError):
                continue  # a torn last line or not JSON: no evidence either way
            if isinstance(entry, dict) and entry.get("type") in ("user", "assistant") \
                    and reader._parse_ts(entry.get("timestamp")) is not None:
                return True  # an event the loader takes a start from
    return False


def _written_before_since(ref: reader.RunRef, since_date) -> bool:
    """True when every file of the run was last written before --since minus one day and the run
    has a start to compare (4.14.9): such a run cannot have started on or after --since."""
    newest = _ref_newest_mtime(ref)
    if newest is None:
        return False
    cutoff = datetime(since_date.year, since_date.month, since_date.day, tzinfo=timezone.utc)
    return newest < (cutoff - _SINCE_MARGIN).timestamp() and _has_a_timestamp(ref)


class _ScanProgress:
    """docs/DESIGN.md 4.14.9 item 1: on a terminal, one stderr line updated at most every
    _PROGRESS_MIN_INTERVAL seconds and erased at the end; off a terminal, one line at the start
    of a scan that reads more than _PROGRESS_NOTE_MIN_RUNS runs, and nothing else."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.stream = sys.stderr
        try:
            self.tty = bool(self.stream.isatty())
        except (AttributeError, ValueError, OSError):
            self.tty = False
        self.last = float("-inf")
        self.shown = False
        if not self.tty and total > _PROGRESS_NOTE_MIN_RUNS:
            self._write(
                f"postmortem: scanning {total} runs; a first scan of a long history can take "
                "a minute or more\n"
            )

    def _write(self, text: str) -> None:
        try:
            self.stream.write(text)
            self.stream.flush()
        except (OSError, ValueError):
            pass

    def step(self, done: int) -> None:
        if not self.tty:
            return
        now = time.monotonic()
        if done < self.total and now - self.last < _PROGRESS_MIN_INTERVAL:
            return
        self.last = now
        self.shown = True
        self._write(f"\rscanning {done}/{self.total} runs")

    def finish(self) -> None:
        if self.tty and self.shown:
            self._write("\r\x1b[K")


# --- the scan run cache (docs/DESIGN.md 4.14.9 item 3) ------------------------

_SCAN_CACHE_SCHEMA = 2  # 2 (0.4): facts carry rule ids


def _iso_dt(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _scan_cache_meta(cfg) -> dict:
    """What every entry depends on beyond its own files: the cache schema, the package version
    (parser, detectors and lifecycle all ship in it), the active config's silenced set, and the
    code digest and json limits (4.14.9 item 4), since a run's facts come from the same reads;
    the limits count even when the source cannot be read for the digest."""
    off = sorted(getattr(cfg, "detectors_off", ()) or ())
    digest = hashlib.sha256(json.dumps(off).encode("utf-8")).hexdigest()[:16]
    return {"schema": _SCAN_CACHE_SCHEMA, "version": __version__, "config": digest,
            "code": f"{_transcript_cache_code()}:{_json_limits()}"}


def _stat_key(path: Path) -> list | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]


def _scan_cache_key(ref: reader.RunRef) -> dict | None:
    """Every input file of the run (the liveness population of 4.7, which is every file a load
    reads for that kind) by name and (inode, size, mtime_ns, ctime_ns), and a workflow's parent
    transcript the same way. None when a file vanished between listing and stat."""
    files = []
    for path in sorted(_ref_files(ref)):
        stat_key = _stat_key(path)
        if stat_key is None:
            return None
        files.append([path.name, *stat_key])
    parent = None
    workflows = None
    if ref.kind == "workflow":
        parent_path = reader._parent_transcript_path_for_workflow_dir(ref.path)
        parent = _stat_key(parent_path) if parent_path is not None else None
    else:
        # a session's launch reconciliation reads which workflow directories exist
        workflow_root = ref.path.parent / ref.path.stem / "subagents" / "workflows"
        try:
            workflows = sorted(p.name for p in workflow_root.iterdir() if p.is_dir())
        except OSError:
            workflows = []
    return {"files": files, "parent": parent, "workflows": workflows}


def _read_scan_cache(meta: dict) -> dict:
    data = state.read_cache(state.scan_cache_path())
    if {k: data.get(k) for k in meta} != meta:
        return {}
    runs = data.get("runs")
    return runs if isinstance(runs, dict) else {}


def _cached_facts(entry: object, key: dict) -> dict | None:
    """An entry's facts when its key matches, every recorded absence probe still answers the
    same, and its 72-hour report window (claim_contradiction) has not closed since."""
    if not isinstance(entry, dict) or entry.get("key") != key:
        return None
    expires_at = entry.get("expires_at")
    now_ts = claim_contradiction._now().timestamp()
    if isinstance(expires_at, (int, float)) and now_ts >= expires_at:
        return None
    probes = entry.get("probes")
    if not isinstance(probes, list):
        return None
    for probe in probes:
        if not (isinstance(probe, list) and len(probe) == 2):
            return None
        if severity.absence(probe[0]) != probe[1]:
            return None
    facts = entry.get("facts")
    return facts if isinstance(facts, dict) else None


def _scan_cache_expiry(run: Run) -> float | None:
    """When claim_contradiction's report_path clause stops being evaluated for this run, if it
    was still being evaluated when the run was analyzed (its findings can change then)."""
    if run.kind != "workflow" or run.ended_at is None:
        return None
    if not claim_contradiction._report_path_checkable(run):
        return None
    # in epoch seconds: a run that ends near datetime.max (year 9999) must not abort the scan
    # (0.3.4's rule: one run never stops a whole scan)
    return (report._aware(run.ended_at).timestamp()
            + claim_contradiction.REPORT_PATH_MAX_AGE.total_seconds())


def _write_scan_cache(meta: dict, updates: dict[str, dict]) -> None:
    try:
        with state.cache_transaction(state.scan_cache_path()) as data:
            same = {k: data.get(k) for k in meta} == meta
            runs = data.get("runs") if same and isinstance(data.get("runs"), dict) else {}
            runs.update(updates)
            for path_str in list(runs):
                if not os.path.exists(path_str):
                    del runs[path_str]
            data.clear()
            data.update(meta)
            data["runs"] = runs
    except state.StateError:
        pass


# --- the transcript cache (docs/DESIGN.md 4.14.9 item 4) -----------------------

_TRANSCRIPT_CACHE_SCHEMA = 1
# stored parses are kept for a run written in the last day (one being written may change again)
_TRANSCRIPT_CACHE_RECENT = timedelta(hours=24)
# and a file not written for two days is removed
_TRANSCRIPT_CACHE_KEEP = timedelta(days=2)


@functools.cache
def _transcript_cache_code() -> str:
    """A digest of the Python build and the package's own source: a stored parse holds the
    package's objects and what the interpreter's json and datetime made of the file, so any
    change to either discards it, not only a new version. Empty when the source cannot be read,
    which turns the cache off."""
    package = Path(__file__).resolve().parent
    digest = hashlib.sha256(sys.version.encode("utf-8") + b"\0")
    try:
        for path in sorted(package.rglob("*.py")):
            digest.update(path.relative_to(package).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except OSError:
        return ""
    return digest.hexdigest()


def _json_limits() -> str:
    """The two interpreter limits json reads while it decodes: an integer longer than the int
    digits limit, or a value nested deeper than the recursion limit, makes a line unparseable."""
    return f"{sys.get_int_max_str_digits()}:{sys.getrecursionlimit()}"


def _transcript_cache_identity() -> str:
    """What a stored parse is valid for: the code digest and the json limits. Empty when the
    source cannot be read, which turns the transcript cache off."""
    code = _transcript_cache_code()
    return f"{code}:{_json_limits()}" if code else ""


def _written_recently(key: dict, now: datetime) -> bool:
    """Whether any input file of the run (its scan cache key) was written in the last day."""
    newest = max((entry[3] for entry in key["files"]), default=None)
    return newest is not None and newest / 1e9 >= (now - _TRANSCRIPT_CACHE_RECENT).timestamp()


def _transcript_cache_path(ref: reader.RunRef) -> Path:
    name = hashlib.sha256(os.fsencode(ref.path)).hexdigest()[:32]
    return state.transcript_cache_dir() / f"{name}.pickle"


def _read_transcript_cache(ref: reader.RunRef) -> dict:
    """The stored parses of one run: {} unless its file is private (state.read_private_bytes),
    holds only built-in values, and names this run, this schema and this identity."""
    code = _transcript_cache_identity()
    if not code:
        return {}
    raw = state.read_private_bytes(_transcript_cache_path(ref))
    if raw is None:
        return {}
    try:
        data = reader.unpickle_plain(raw)
    except Exception:
        return {}
    if not isinstance(data, dict) or (
        data.get("schema"), data.get("code"), data.get("run")
    ) != (_TRANSCRIPT_CACHE_SCHEMA, code, str(ref.path)):
        return {}
    entries = data.get("entries")
    return entries if isinstance(entries, dict) else {}


def _load_scanned_run(ref: reader.RunRef, unreadable: list[str], memo, analyze):
    """(run, analyze(run), memo): _load_listed_run and analyze, through memo when there is one.
    When a load or its analysis reused a stored parse and then failed (raised, or found the run
    unreadable), both are done again from the files alone with a fresh memo, so a stored parse
    can never be why a run fails; a failure with no reuse stands as it is."""

    def attempt(memo):
        found: list[str] = []
        with reader.transcript_memo(memo) if memo is not None else contextlib.nullcontext():
            run = _load_listed_run(ref, found)
            analyzed = analyze(run) if run is not None else None
        return run, analyzed, found

    if memo is None:
        run, analyzed, found = attempt(None)
    else:
        try:
            run, analyzed, found = attempt(memo)
        except Exception:
            if not memo.reused:
                raise
            run, analyzed, found = None, None, [ref.run_id]
        if memo.reused and found:
            memo = reader.TranscriptMemo({}, store=memo.store)
            run, analyzed, found = attempt(memo)
    unreadable.extend(found)
    return run, analyzed, memo


def _write_transcript_cache(ref: reader.RunRef, memo: reader.TranscriptMemo) -> None:
    """The parses this load used, as the run's whole file; an unusable cache is skipped."""
    code = _transcript_cache_identity()
    if not code:
        return
    data = pickle.dumps(
        {"schema": _TRANSCRIPT_CACHE_SCHEMA, "code": code, "run": str(ref.path),
         "entries": memo.used},
        protocol=pickle.HIGHEST_PROTOCOL,
    )
    try:
        state.write_private_bytes(_transcript_cache_path(ref), data)
    except state.StateError:
        pass


@contextlib.contextmanager
def _recorded_probes():
    """Every severity.absence() answer given while one run is analyzed, as [path, answer]."""
    probes: list[list] = []
    real = severity.absence

    def recording(path):
        answer = real(path)
        probes.append([path, answer])
        return answer

    severity.absence = recording
    try:
        yield probes
    finally:
        severity.absence = real


def _scan_facts(run: Run, findings: list[Finding], shown: list[Finding]) -> dict:
    """The clock-free facts one row, its triage entry and its totals are built from."""
    risk_paths, _commits = report._risk_paths_and_commits(run)
    per_detector: dict[str, int] = {}
    for finding in findings:
        per_detector[finding.detector] = per_detector.get(finding.detector, 0) + 1
    wasted_all = sum(
        finding.metrics.get("wasted_seconds", 0.0) for finding in findings
        if finding.detector == "wasted_time"
    )
    lifecycle = run.lifecycle
    return {
        "run_id": run.run_id, "kind": run.kind, "project": run.project_slug,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "ended_at": run.ended_at.isoformat() if run.ended_at else None,
        "agents": len(run.agents), "attempts": len(run.attempts), "finished": run.finished,
        "lifecycle": None if lifecycle is None else {"terminal": lifecycle.terminal},
        "verdict": report.verdict_of(run, shown),
        "verdict_bare": report._verdict_bare(run, shown),
        "top_detector": _top_detector(shown),
        "attention_reason": report.attention_reason(run, shown),
        "risk_paths": bool(risk_paths),
        "paths_at_risk": report.scan_paths_at_risk(run),
        "paths_at_risk_complete": report.scan_paths_at_risk_complete(run),
        "wasted_seconds_shown": report.wasted_seconds_of(shown),
        "totals": {
            "subagents": len(run.attempts),
            "interrupted": sum(1 for a in run.attempts if a.interrupt is not None),
            "retried": report.silent_retry_count(run) > 0,
            "wasted_hours": wasted_all / 3600.0,
        },
        "findings_per_detector": per_detector,
        # every finding's rule ids, shown or silenced, which is what `rules propose` counts
        "rule_ids": [list(finding.rule_ids) for finding in findings],
        "skipped_lines": run.skipped_lines, "skipped_files": run.skipped_files,
    }


def _caller_mark(ref: reader.RunRef) -> str | None:
    """docs/DESIGN.md 4.7 (0.4): `this session` for the session CLAUDE_CODE_SESSION_ID names,
    `started by this session` for a workflow under it, else None. A marker, never an identity:
    it is computed from the run's own path at every level."""
    caller = _caller_session_id()
    if caller is None:
        return None
    if ref.kind == "session":
        return "this session" if ref.path.stem.lower() == caller else None
    parts = ref.path.parts
    if len(parts) >= 4 and parts[-2] == "workflows" and parts[-4].lower() == caller:
        return "started by this session"
    return None


def _scan_entry(facts: dict, now: datetime, newest_mtime: float | None,
                ref: reader.RunRef) -> dict:
    """The row, NEEDS ATTENTION candidate and LIVE NOW entry of one run, from its facts and the
    clock; the one path both a freshly analyzed run and a cached one take (4.14.9)."""
    lifecycle = facts["lifecycle"]
    run_like = SimpleNamespace(
        run_id=facts["run_id"], kind=facts["kind"], finished=facts["finished"],
        started_at=_iso_dt(facts["started_at"]), ended_at=_iso_dt(facts["ended_at"]),
        lifecycle=None if lifecycle is None else SimpleNamespace(
            terminal=lifecycle["terminal"], activity=reader.activity_word(newest_mtime),
        ),
    )
    live = report.is_possibly_live(run_like, newest_mtime, now, hooks_cli._UNKNOWN_QUIET_SECONDS)
    age = report.scan_age_string(run_like, now, live)
    attention = report.attention_candidate_from(
        facts["verdict_bare"], run_like, facts["risk_paths"], now,
    )
    reason = facts["attention_reason"] if attention else None
    command = report.attention_command_from(run_like.run_id, facts["risk_paths"], run_like, now)
    row = {
        "run_id": facts["run_id"],
        "kind": facts["kind"],
        "date": run_like.started_at.date().isoformat() if run_like.started_at else "unknown",
        "project": facts["project"],
        "agents": facts["agents"],
        "attempts": facts["attempts"],
        "verdict": facts["verdict"],
        "top_detector": facts["top_detector"],
        "finished": facts["finished"],
        "attention": attention,
        "attention_reason": reason,
        "age": age,
        "possibly_live": live,
        "paths_at_risk": facts["paths_at_risk"],
        "paths_at_risk_complete": facts["paths_at_risk_complete"],
        "unfinished_state": report.unfinished_state(run_like, live),
    }
    candidate = None
    live_entry = None
    if attention:
        candidate = {
            "run": run_like, "age": age, "verdict": facts["verdict"], "reason": reason,
            "command": command, "mark": _caller_mark(ref),
            "sort_key": report.attention_sort_key_from(
                run_like, facts["verdict_bare"], facts["wasted_seconds_shown"], live,
                facts["paths_at_risk"], now,
            ),
        }
    elif live:
        live_entry = {"run": run_like, "verdict": facts["verdict"], "command": command,
                      "mark": _caller_mark(ref),
                      "sort_key": _here_key(ref, run_like)}
    return {"row": row, "candidate": candidate, "live": live_entry}


def _accumulate_scan_totals_from(totals: dict, facts: dict) -> None:
    """_accumulate_scan_totals over kept facts."""
    kept = facts["totals"]
    if facts["kind"] == "session":
        totals["session"]["runs"] += 1
        totals["session"]["subagents"] += kept["subagents"]
        totals["session"]["interrupted"] += kept["interrupted"]
    else:
        totals["workflow"]["runs"] += 1
        totals["workflow"]["attempts"] += facts["attempts"]
        if kept["retried"]:
            totals["workflow"]["runs_with_retries"] += 1
        totals["workflow"]["wasted_hours"] += kept["wasted_hours"]


def _facts_of_ref(
    ref: reader.RunRef, cfg, cache_runs: dict, cache_updates: dict, unreadable_runs: list, *,
    use_cache: bool, no_cache: bool, now: datetime, keep=None,
) -> tuple[dict | None, dict | None]:
    """One run's scan facts and cache key (docs/DESIGN.md 4.14.9): from the run cache when the
    key still holds, else loaded (only the transcripts whose files moved are parsed again),
    analyzed and queued in `cache_updates`. None for a run that cannot be read (counted in
    `unreadable_runs`) or that `keep` rejects. `scan` and `rules propose` both read runs here."""
    key = _scan_cache_key(ref) if use_cache else None
    facts = _cached_facts(cache_runs.get(str(ref.path)), key) if key is not None else None
    if facts is not None:
        return facts, key
    # discover_runs promises a RunRef for anything matching the layout glob, not that every
    # candidate is fully loadable; one bad candidate must not abort the whole fleet scan
    # (docs/DESIGN.md 4.2), and a run that cannot be read is counted (4.8)
    memo = None
    if key is not None:
        # 4.14.9 item 4: only the transcripts whose files moved are parsed again
        memo = reader.TranscriptMemo(
            {} if no_cache else _read_transcript_cache(ref), store=_written_recently(key, now),
        )

    def analyze(run: Run) -> tuple[dict, list] | None:
        if keep is not None and not keep(run):
            return None
        with _recorded_probes() as probes:
            findings = _run_findings(run)
            # docs/DESIGN.md 4.10: a silenced detector's findings never inform scan's own
            # verdict, top detector, or NEEDS ATTENTION reason (the same rule `run`'s text/JSON
            # follow), but they still count toward `totals` and `findings_per_detector`, exactly
            # like trend.
            shown, _silenced = config.partition_silenced(findings, cfg)
            return _scan_facts(run, findings, shown), probes

    run, analyzed, memo = _load_scanned_run(ref, unreadable_runs, memo, analyze)
    if memo is not None and memo.store and memo.parsed:
        _write_transcript_cache(ref, memo)
    if run is None or analyzed is None:
        return None, key
    facts, probes = analyzed
    if key is not None:
        cache_updates[str(ref.path)] = {
            "key": key, "facts": facts, "probes": probes, "expires_at": _scan_cache_expiry(run),
        }
    return facts, key


def _cmd_scan(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = _redactor_for(args)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    kinds = frozenset({args.kind}) if args.kind else frozenset({"workflow", "session"})
    # docs/DESIGN.md 4.8 (0.3.4): what scan could not read is said, never silently dropped
    unreadable_files: list[Path] = []
    run_refs = reader.discover_runs(root, kinds=kinds, skipped=unreadable_files)
    unreadable_runs: list[str] = []
    skipped_lines: list[tuple[str, int]] = []
    skipped_meta_files = 0

    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(_since_error(args), file=sys.stderr)
            return 1

    # docs/DESIGN.md 4.13.6 (N6): --here is a filter on the runs themselves, applied before
    # rows, totals, the 7-day totals, and the triage candidates are computed.
    here_caller = _working_directory() if args.here else None
    # docs/DESIGN.md 4.13.6 (G5): the workflows of the caller's own session join the set
    here_owned = (
        reader.caller_workflow_paths(root, _caller_session_id(), here_caller)
        if args.here else frozenset()
    )

    now = report._now()
    seven_days_ago = now - timedelta(days=7)

    rows: list[dict] = []
    started_at_by_row: dict[int, tuple[int, float]] = {}
    # docs/DESIGN.md 4.7: totals stratified by kind, over every run matching
    # --since/--kind ("ALL TIME" in the triage view, and the JSON payload's
    # own 'totals', unchanged in shape from before unit M3b). totals_7d is
    # the same shape, accumulated only for a run whose ended_at (or
    # started_at when ended_at is None) falls in the last 7 days -- the
    # triage view's "LAST 7 DAYS" line.
    totals = _empty_scan_totals()
    totals["findings_per_detector"] = {}
    totals_7d = _empty_scan_totals()
    # unit M3b: every NEEDS ATTENTION candidate found while scanning, kept
    # alongside the Run/Finding objects the triage renderer and the ranking
    # key both need; sorted and truncated to --limit only once scanning is
    # done, so the header's "k of N" always reports the true count.
    candidates: list[dict] = []
    # docs/DESIGN.md 4.7 (FB2): possibly-live runs that are not candidates, for LIVE NOW.
    live_now: list[dict] = []

    if since_date is not None and _SINCE_MTIME_PREFILTER:
        run_refs = [ref for ref in run_refs if not _written_before_since(ref, since_date)]
    progress = _ScanProgress(len(run_refs))

    use_cache = not args.here
    cache_meta = _scan_cache_meta(cfg)
    cache_runs = _read_scan_cache(cache_meta) if use_cache and not args.no_cache else {}
    cache_updates: dict[str, dict] = {}

    for index, ref in enumerate(run_refs, 1):
        progress.step(index)
        keep = None
        if args.here:
            def keep(run: Run, ref=ref) -> bool:
                return here_caller is not None and (
                    ref.path in here_owned or reader.run_matches_cwd(run, here_caller)
                )
        facts, key = _facts_of_ref(
            ref, cfg, cache_runs, cache_updates, unreadable_runs,
            use_cache=use_cache, no_cache=args.no_cache, now=now, keep=keep,
        )
        if facts is None:
            continue
        started = _iso_dt(facts["started_at"])
        if since_date is not None and started is not None and started.date() < since_date:
            continue
        if facts["skipped_lines"]:
            skipped_lines.append((facts["run_id"], facts["skipped_lines"]))
        skipped_meta_files += facts["skipped_files"]
        newest_mtime = (
            max((entry[3] for entry in key["files"]), default=None) / 1e9
            if key is not None and key["files"] else _ref_newest_mtime(ref)
        )
        entry = _scan_entry(facts, now, newest_mtime, ref)

        # release review of 0.3.0: rows follow the instant a run started (in UTC, whatever offset
        # its timestamps were written with), oldest first, with an undated run before every dated
        # one, so the last row is the newest run that has a start time
        started_at_by_row[len(rows)] = (
            (1, report._aware(started).timestamp()) if started else (0, 0.0)
        )
        rows.append(entry["row"])
        if entry["candidate"] is not None:
            candidates.append(entry["candidate"])
        elif entry["live"] is not None:
            live_now.append(entry["live"])

        _accumulate_scan_totals_from(totals, facts)
        window_ts = _iso_dt(facts["ended_at"]) or started
        if window_ts is None and newest_mtime is not None:
            # an undated run, which --since keeps: placed by its newest write (it cannot have
            # started after it), so the 7-day line counts it whenever SINCE can (4.7)
            window_ts = datetime.fromtimestamp(newest_mtime, tz=timezone.utc)
        if window_ts is not None and report._aware(window_ts) >= seven_days_ago:
            _accumulate_scan_totals_from(totals_7d, facts)

        for detector_id, count in facts["findings_per_detector"].items():
            totals["findings_per_detector"][detector_id] = (
                totals["findings_per_detector"].get(detector_id, 0) + count
            )

    if use_cache and cache_updates:
        _write_scan_cache(cache_meta, cache_updates)
    if use_cache:
        state.prune_private_files(
            state.transcript_cache_dir(), (now - _TRANSCRIPT_CACHE_KEEP).timestamp()
        )
    progress.finish()
    # docs/DESIGN.md 4.7: background sessions idle on a command that
    # will not end, read now and outside the scan cache and --since, since they are stuck now
    attention = _attention_sessions(root, now.timestamp(), args.kind, here_caller)
    stalled = [entry for entry in attention if entry.get("state") == "stalled"]
    blocked = [entry for entry in attention if entry.get("state") != "stalled"]
    if not rows and not attention:
        kind = "scan_here_no_match" if args.here else "no_runs"
        print(
            _run_error(
                args, kind, root=root,
                unreadable_files=len(unreadable_files), unreadable_runs=unreadable_runs,
            ),
            file=sys.stderr,
        )
        if kind != "no_runs":
            _print_left_out(unreadable_runs)  # the empty state already names them
        # docs/DESIGN.md 4.8 (0.3.4): a run that could not be read leaves the scan INCOMPLETE
        return 4 if unreadable_runs else 2

    order = sorted(range(len(rows)), key=lambda i: (started_at_by_row[i], rows[i]["run_id"]))
    rows = [rows[i] for i in order]

    if args.json:
        print(
            _json_guarded(
                level=_level_of(args),
                identities=report.collect_project_identities(row["project"] for row in rows),
                repo_root=None, command="scan",
                build=lambda r: _scan_payload(rows, totals, r, stalled, blocked),
            ),
            end="",
        )
    elif args.all:
        # N3b P9: scan --all prints one project per row; the triage view
        # below prints no identity, so it collects (and registers) nothing.
        print(
            _render_guarded(
                level=_level_of(args),
                identities=report.collect_project_identities(row["project"] for row in rows),
                repo_root=None, command="scan",
                render=lambda r: report.render_scan_text(
                    rows, totals, r, silenced_detectors=cfg.detectors_off,
                ),
            ),
            end="",
        )
    else:
        row_ids = {row["run_id"] for row in rows}
        extra_runs = _fold_stalled(stalled, candidates, live_now, row_ids)
        more_runs, older_blocked = _fold_blocked(blocked, candidates, live_now, row_ids,
                                                 _level_of(args))
        extra_runs += more_runs
        candidates.sort(key=lambda candidate: candidate["sort_key"])
        shown_candidates = candidates[: args.limit]
        live_now.sort(key=lambda entry: entry["sort_key"], reverse=True)
        print(
            _render_guarded(
                level=_level_of(args), identities=[], repo_root=None, command="scan",
                render=lambda r: report.render_scan_triage_text(
                    shown_candidates, len(candidates), len(rows) + extra_runs, totals_7d,
                    totals, r,
                    silenced_detectors=cfg.detectors_off,
                    live_now=live_now[: args.limit], total_live_now=len(live_now),
                    both_kinds=args.kind is None,
                    all_label=f"SINCE {args.since}" if since_date is not None else "ALL TIME",
                    older_blocked=older_blocked,
                    read_in_part=sum(1 for row in rows if "(INCOMPLETE)" in row["verdict"]),
                ),
            ),
            end="",
        )

    skipped_line = _scan_skipped_line(
        skipped_lines, len(unreadable_files), skipped_meta_files, unreadable_runs,
    )
    if skipped_line:
        # with --json, stdout stays one JSON document
        print(skipped_line, file=sys.stderr if args.json else sys.stdout)

    # docs/DESIGN.md 4.6/4.7: scan's own exit code must reflect the worst
    # verdict across every included run, the same as 'run' does for one run
    # -- a caller scripting against scan otherwise has no way to detect that
    # any run in the fleet was FAILED or only INCOMPLETE-ly inspected.
    any_failed = any(row["verdict"].startswith("FAILED") for row in rows)
    any_incomplete = any("(INCOMPLETE)" in row["verdict"] for row in rows) or bool(
        unreadable_runs
    )
    if any_failed:
        return 3
    if any_incomplete:
        return 4
    return 0


def _cmd_rules(args: argparse.Namespace) -> int:
    # docs/DESIGN.md 9: `rules propose`/`rules review` are separate verbs
    # under the same `rules` subparser (their own flags, no --run/--all/
    # --show), dispatched before anything below touches an attribute only
    # the bare `rules` namespace carries.
    if getattr(args, "rules_command", None) == "propose":
        return _cmd_rules_propose(args)
    if getattr(args, "rules_command", None) == "review":
        return _cmd_rules_review(args)

    # docs/DESIGN.md 5.2's D2: rules prints error messages, rule text, and
    # rows through the same active Redactor as every other command -- a
    # --run target or a --root default can carry a real path just like
    # run/scan/apply's can.
    redactor = _redactor_for(args)

    if args.show:
        rule = rules.RULES.get(args.show)
        if rule is None:
            print(_no_such_rule(args, args.show), file=sys.stderr)
            return 1
        if args.json:
            payload = {"disclosure": args.level, **rules.rule_to_json(rule)}
            print(json.dumps(payload, indent=2))
        else:
            # the template path is rendered once by display_path; the rule's own words are fixed
            # text, and the assembled text is not scanned again (docs/DESIGN.md 5.1, F fix 7)
            print(rules.render_rule_show(rule, redactor.display_path), end="")
        return 0

    root = args.root.expanduser()
    if args.run_id:
        run_path, failure = _resolve_or_report(args, root)
        if run_path is None:
            return failure
        try:
            findings = _run_findings(reader.load_run(run_path))
        except reader.NotARun:
            print(_run_error(args, "not_a_run"), file=sys.stderr)
            return 2
    else:
        run_refs = reader.discover_runs(root)
        if not run_refs and not args.all:
            print(_run_error(args, "no_runs", root=root), file=sys.stderr)
            return 2
        findings = []
        unreadable: list[str] = []
        for ref in run_refs:
            run = _load_listed_run(ref, unreadable)
            if run is not None:
                findings.extend(_run_findings(run))
        _print_left_out(unreadable)

    if args.all:
        backing = {rule.id: count for rule, count in rules.rules_for(findings)}
        rows = [(rules.RULES[rule_id], backing.get(rule_id, 0)) for rule_id in sorted(rules.RULES)]
    else:
        rows = rules.rules_for(findings)

    if args.json:
        # docs/DESIGN.md 5.3: schema 2 wraps what used to be the bare list
        # in a {"schema", "rules"} object so this command can carry the same
        # top-level version marker as run/scan/trend/demo's JSON.
        payload = {
            "schema": report.SCHEMA_VERSION,
            "disclosure": args.level,
            "rules": [
                {"rule_id": rule.id, "title": rule.title, "count": count} for rule, count in rows
            ],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(redactor(rules.render_rules_text(rows)), end="")
    return 0


def _cmd_rules_propose(args: argparse.Namespace) -> int:
    """docs/DESIGN.md 9: scan the fleet like `scan` does and print the rules
    backed by enough evidence to propose adopting. Read-only; writes
    nothing, the same as every other `rules` verb."""
    root = args.root.expanduser()
    redactor = _redactor_for(args)

    if args.run_id and args.since:
        # docs/DESIGN.md 9 (N8): one run has no date range to filter.
        print(
            "postmortem: --run cannot be combined with --since (a single run has no date range)",
            file=sys.stderr,
        )
        return 1

    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            print(_since_error(args), file=sys.stderr)
            return 1

    runs_with_findings: list[tuple[Run, list[Finding]]] = []
    run_id_shown = None
    if args.run_id:
        run_path, failure = _resolve_or_report(args, root)
        if run_path is None:
            return failure
        try:
            run = reader.load_run(run_path)
        except reader.NotARun:
            print(_run_error(args, "not_a_run"), file=sys.stderr)
            return 2
        runs_with_findings.append((run, _run_findings(run)))
        run_id_shown = run.run_id
    else:
        run_refs = reader.discover_runs(root)
        if not run_refs:
            print(_run_error(args, "no_runs", root=root), file=sys.stderr)
            return 2

        # docs/DESIGN.md 9 (0.4): the fleet is read through scan's run cache, so an unchanged
        # run is not loaded again; a run and its findings are what propose_rules counts (an id,
        # a start date, each finding's rule ids)
        cfg = config.load_config()
        cache_meta = _scan_cache_meta(cfg)
        cache_runs = _read_scan_cache(cache_meta)
        cache_updates: dict[str, dict] = {}
        now = report._now()
        unreadable: list[str] = []
        for ref in run_refs:
            facts, _key = _facts_of_ref(
                ref, cfg, cache_runs, cache_updates, unreadable,
                use_cache=True, no_cache=False, now=now,
            )
            if facts is None:
                continue
            run_started = _iso_dt(facts["started_at"])
            if (
                since_date is not None
                and run_started is not None
                and run_started.date() < since_date
            ):
                continue
            runs_with_findings.append((
                SimpleNamespace(run_id=facts["run_id"], started_at=run_started),
                [SimpleNamespace(rule_ids=tuple(ids)) for ids in facts["rule_ids"]],
            ))
        if cache_updates:
            _write_scan_cache(cache_meta, cache_updates)
        _print_left_out(unreadable)

    # docs/DESIGN.md 9 (N8): an adopted rule is a status row, never proposed
    # again. Only the user's config [[adopted]] records name one.
    rows = rules.propose_rules(
        runs_with_findings, config.load_config().adopted, single_run=bool(args.run_id),
    )

    if args.json:
        payload = {
            "schema": report.SCHEMA_VERSION,
            "disclosure": args.level,
            "proposals": [rules.propose_row_to_json(row) for row in rows],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(redactor(rules.render_propose_text(rows, run_id=run_id_shown)), end="")
    return 0


def _adopted_entries(cfg: config.Config) -> list[dict]:
    """docs/DESIGN.md 9/7.5: the user's `[[adopted]]` config rows, plus any
    legacy v0.1 state-store `applied` entry (id, target, applied_at) that
    isn't already the same adoption declared in config -- v0.1 users keep
    their history even though nothing writes a new `applied` entry anymore.
    A filesystem error or a state.json newer than this build understands is
    treated as "no legacy entries" rather than failing the whole command:
    the config's own [[adopted]] rows still review just fine."""
    entries = [
        {"rule_id": item.rule, "target": item.target, "applied_at": item.date}
        for item in cfg.adopted
    ]
    seen = {(e["rule_id"], e["applied_at"][:10], e["target"]) for e in entries}

    try:
        legacy = state.load_state().get("applied", [])
    except (OSError, state.StateTooNew):
        legacy = []

    for entry in legacy:
        if not isinstance(entry, dict):
            continue
        rule_id, target = entry.get("rule_id"), entry.get("target")
        applied_at = entry.get("applied_at") or ""
        key = (rule_id, applied_at[:10], target)
        if key in seen:
            continue
        seen.add(key)
        entries.append({"rule_id": rule_id, "target": target, "applied_at": applied_at})
    return entries


def _cmd_rules_review(args: argparse.Namespace) -> int:
    """docs/DESIGN.md 9: the user's adopted rules' detector rate, N weeks
    before and after adoption. Read-only: reviewing never removes a rule,
    it only lists which ones show no improvement."""
    redactor = _redactor_for(args)
    cfg = config.load_config()
    adopted = _adopted_entries(cfg)

    if not adopted:
        if args.json:
            payload = {
                "schema": report.SCHEMA_VERSION, "disclosure": args.level, "reviews": [],
                "consider_removing": [],
            }
            print(json.dumps(payload, indent=2))
        else:
            print("no adopted rule to review")
        return 0

    root = args.root.expanduser()
    run_refs = reader.discover_runs(root)
    if not run_refs:
        print(_run_error(args, "no_runs", root=root), file=sys.stderr)
        return 2

    runs_with_findings: list[tuple[Run, list[Finding]]] = []
    unreadable: list[str] = []
    for ref in run_refs:
        run = _load_listed_run(ref, unreadable)
        if run is not None:
            runs_with_findings.append((run, _run_findings(run)))
    _print_left_out(unreadable)

    rows = trend.review_rows(adopted, runs_with_findings, args.weeks)

    if args.json:
        print(json.dumps(trend.review_to_json(rows, redactor), indent=2))
    else:
        print(redactor(trend.render_review_text(rows)), end="")
    return 0


def _target_looks_like_a_run_directory(target: Path) -> bool:
    marker = "/subagents/workflows/"
    candidates = {str(target)}
    try:
        candidates.add(str(target.resolve()))
    except OSError:
        pass
    return any(marker in candidate for candidate in candidates)


def _errors_for(args: argparse.Namespace) -> argerrors.ErrorRenderer:
    """N3b P13: the one span renderer a handler uses for the values the user
    typed, at the command's own level. Built once per command so the numbering
    (path-1, value-1) is shared by every span it prints."""
    renderer = getattr(args, "_handler_errors", None)
    if renderer is None:
        renderer = argerrors.ErrorRenderer([], set(), getattr(args, "level", "share"))
        args._handler_errors = renderer
    return renderer


def _working_directory() -> str | None:
    try:
        return os.getcwd()
    except OSError:
        return None


def _no_such_rule(args: argparse.Namespace, typed: str) -> str:
    """The unknown-rule-id error of `apply` and `rules --show` (N3b P13)."""
    if getattr(args, "level", "share") == "raw":
        return f"postmortem: no such rule '{typed}'"
    return (
        f"postmortem: no such rule '{_errors_for(args).span(typed, 'value')}' "
        "(expected a rule id such as R-RETRY-SAFE; 'agent-postmortem rules --all' lists them)"
    )


_APPLY_SHARE_NOTE = (
    "note: the actual patch needs --local or --raw (it quotes the target file, "
    "which the default level never prints)"
)
_APPLY_SHARE_CLOSING = (
    "Apply it yourself: paste the directive into the target file's managed block."
)
_APPLY_LOCAL_CLOSING = (
    "Apply it yourself: paste the block into the target file. This diff is for reading; "
    "--raw prints one git apply can use."
)
_APPLY_CLOSING = (
    "Apply it yourself: paste the block into the target file, or save this "
    "output and run git apply."
)


def _template_shown(redactor: Redactor, path: str) -> str:
    """The hook template, this package's own file. Under the home it reads as any path does; outside
    it, a system or Homebrew install, by name alone, since `node path-1` says nothing. Its folders
    stay hidden below --raw."""
    home = str(redactor.context.home).rstrip("/")
    if redactor.level == "raw" or (home and path.startswith(home + "/")):
        return redactor.display_path(path)
    return f".../{os.path.basename(path)}"


def _cmd_apply(args: argparse.Namespace) -> int:
    """`apply` (docs/DESIGN.md 9; N3b P18, C-4, P19).

    share prints no byte derived from the target file: the target as path-N,
    the requested rule's canonical directive, one note line saying the actual
    patch needs --local or --raw; the already-present message and every error
    name the target as path-N too. local prints a unified diff with 3 context
    lines whose header is the P19 form (apply makes no observation, so no
    repository root: a path inside home is ~/.../<basename>, any other path-N,
    never absolute). raw is today's output, usable with `git apply`."""
    level = _level_of(args)
    redactor = _redactor_for(args)
    errors = _errors_for(args)
    if args.rule_id not in rules.RULES:
        print(_no_such_rule(args, args.rule_id), file=sys.stderr)
        return 1

    target = args.target.expanduser()
    if _target_looks_like_a_run_directory(target):
        shown = str(target) if level == "raw" else errors.span(str(args.target), "path")
        print(
            f"postmortem: refusing to target a run directory ({shown}); "
            "apply targets a file you maintain, never a run directory",
            file=sys.stderr,
        )
        return 1

    names = redactor.fresh_names(detailed=True)
    display = names.path(str(target), _working_directory()) or str(target)

    rule = rules.RULES[args.rule_id]
    try:
        if level == "local":
            diff = rules.apply_rule(args.rule_id, target, label=display)
        else:
            diff = rules.apply_rule(args.rule_id, target)
    except (ValueError, OSError) as exc:
        # Covers an unterminated rules block and a target that could not be
        # read (permissions, a broken symlink, and the like) -- apply_rule
        # only ever reads target, it never writes it (docs/DESIGN.md 9, R3).
        if level == "share":
            # No byte of the target and no OS-reported path: a fixed sentence
            # per kind of failure, the target named as path-N.
            if isinstance(exc, UnicodeDecodeError):
                reason = "it is not valid UTF-8 text"
            elif isinstance(exc, OSError):
                reason = f"could not read it ({exc.strerror or 'read error'})"
            else:
                reason = "it has an unterminated agent-postmortem rules block"
            print(f"postmortem: target {display}: {reason}", file=sys.stderr)
        else:
            print(redactor(f"postmortem: {exc}"), file=sys.stderr)
        return 1

    # docs/DESIGN.md 5.2's D2: apply routes its diff, its message, and its
    # hook-install note through the same active Redactor as its own error
    # messages. At share nothing printed here comes from the target file
    # except the one bit the already-present message states.
    if level == "share":
        if diff:
            print(f"rule {args.rule_id} would add this directive to {display}:")
            print(rules.directive_line(args.rule_id))
            print(_APPLY_SHARE_NOTE)
        else:
            print(f"postmortem: {args.rule_id} already present in {display}; nothing to add")
    elif diff:
        print(redactor(diff), end="")
    else:
        # `display` is the P19 form of the target, rendered once: the line is not scanned again
        print(f"postmortem: {args.rule_id} already present in {display}; nothing to add")

    note = rules.render_hook_install_note(rule, lambda path: _template_shown(redactor, path))
    if note:
        print(note, end="")  # the template path was rendered once; the note is not rescanned
        if level != "raw":
            print("The template's full path prints with --raw.")

    # docs/DESIGN.md 9/R3: apply never writes target itself, with or without
    # a diff to show -- this closing sentence is the last line every time,
    # so the tool never leaves the impression it just did the pasting for you.
    print({
        "share": _APPLY_SHARE_CLOSING, "local": _APPLY_LOCAL_CLOSING, "raw": _APPLY_CLOSING,
    }[level])
    return 0


def _cmd_trend(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = _redactor_for(args)
    run_refs = reader.discover_runs(root)
    if not run_refs:
        print(_run_error(args, "no_runs", root=root), file=sys.stderr)
        return 2

    runs_with_findings: list[tuple[Run, list[Finding]]] = []
    unreadable: list[str] = []
    for ref in run_refs:
        run = _load_listed_run(ref, unreadable)
        if run is not None:
            runs_with_findings.append((run, _run_findings(run)))
    _print_left_out(unreadable)

    weekly = trend.weekly_counts(runs_with_findings, args.weeks)
    try:
        applied = state.load_state().get("applied", [])
    except (OSError, state.StateTooNew) as exc:
        # A filesystem failure, or a state.json newer than this build
        # understands, is not "no rules applied": report it plainly rather
        # than silently showing an empty before/after section that looks
        # like no rule ever ran.
        print(
            f"postmortem: could not read the state file: {redactor.exception_text(exc)}",
            file=sys.stderr,
        )
        return 1
    before_after_rows = trend.before_after(applied, runs_with_findings)

    if args.json:
        print(json.dumps(trend.to_json(weekly, before_after_rows, args.weeks, redactor), indent=2))
    else:
        print(trend.render_trend_text(weekly, before_after_rows, args.weeks, redactor), end="")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    from . import doctor

    root = args.root.expanduser()
    named = args.session if args.session else os.environ.get(_CALLER_SESSION_ENV, "").strip()
    main_path = live.resolve_session(named, root) if named else None
    if args.session and main_path is None:
        print("postmortem: no such session", file=sys.stderr)
        return 2
    if main_path is not None:
        main_path = main_path.resolve()  # a symlink's own name is no session id
    if args.test_alert and main_path is None:
        print("postmortem: doctor --test-alert needs a session: run it inside one, or pass "
              "--session", file=sys.stderr)
        return 1
    redactor = _redactor_for(args)
    if args.test_alert:
        armed = doctor.arm_test_alert(main_path)
        words = ("test alert armed: at your next tool call or prompt in this session you should "
                 "read a line starting ALERT TEST. If you do not, this session is not running the "
                 "plugin's hooks: /reload-plugins or restart." if armed else
                 "could not arm the test alert: the state directory is not writable, which is "
                 "itself why alerts would not arrive")
        if args.json:
            sys.stdout.write(doctor.render_armed_json(armed, words, redactor))
        else:
            print(f"DOCTOR  {words}")
        return 0 if armed else 3
    report = doctor.diagnose(root, main_path, _version_text(_read_build_info()))
    if args.json:
        sys.stdout.write(doctor.render_json(report, redactor))
    else:
        sys.stdout.write(doctor.render_text(report, redactor, can_test=main_path is not None))
    return 3 if report["failed"] else 0


def _cmd_status(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    if not args.session and not os.environ.get(_CALLER_SESSION_ENV, "").strip():
        print(
            "postmortem: status with no session follows the caller's session, and "
            "CLAUDE_CODE_SESSION_ID is not set; name a session id or a transcript path",
            file=sys.stderr,
        )
        return 1
    main_path = live.resolve_session(args.session, root)
    if main_path is None:
        print("postmortem: no such session", file=sys.stderr)
        return 2
    snap = status.snapshot(main_path)
    redactor = live.session_redactor(
        main_path, [row["agent_id"] for row in snap["subagents"]], base=_redactor_for(args))
    if args.json:
        sys.stdout.write(status.render_json(snap, redactor))
    else:
        sys.stdout.write(status.render_text(snap, redactor))
    return 0


# docs/DESIGN.md 4.12: watch flushes every line, so a
# reader that closed the pipe stops it at the next line; 128 + SIGPIPE, as a shell reports it.
_WATCH_READER_GONE = 141


def _cmd_watch(args: argparse.Namespace) -> int:
    try:
        return _watch(args)
    except BrokenPipeError:
        _silence_closed_stdout()
        return _WATCH_READER_GONE


def _silence_closed_stdout() -> None:
    """Point stdout's descriptor at the null device, so the interpreter's own flush at exit does
    not fail a second time on the closed pipe (and print "Exception ignored")."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, sys.stdout.fileno())
        finally:
            os.close(devnull)
    except (OSError, ValueError, AttributeError):
        pass


def _watch(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    if args.session is not None:
        if args.run_id is not None:
            print("postmortem: watch takes a RUN_ID or --session, not both", file=sys.stderr)
            return 1
        if not args.session and not os.environ.get(_CALLER_SESSION_ENV, "").strip():
            print(
                "postmortem: watch --session with no value follows the caller's session, and "
                "CLAUDE_CODE_SESSION_ID is not set; name a session id or a transcript path",
                file=sys.stderr,
            )
            return 1
        main_path = live.resolve_session(args.session, root)
        if main_path is None:
            print("postmortem: no such session", file=sys.stderr)
            return 2
        return live.watch_session(
            main_path, interval=args.interval or live.DEFAULT_SESSION_INTERVAL,
            max_hours=args.max_hours, json_mode=args.json, out=sys.stdout, err=sys.stderr,
        )
    if args.run_id is None:
        print("postmortem: watch needs a RUN_ID, or --session", file=sys.stderr)
        return 1
    return watch.watch_run(
        args.run_id, root,
        interval=args.interval or watch.DEFAULT_INTERVAL, max_hours=args.max_hours,
        quiet_after=args.quiet_after,
        done_grace=args.done_grace, json_mode=args.json, out=sys.stdout, err=sys.stderr,
    )


class _FirstLineStdout:
    """Wrap stdout so the first non-empty write is preceded by the P15
    label line. Every print path of a text command goes through write(), so
    no command has to remember to print the label itself."""

    def __init__(self, stream, label: str) -> None:
        self._stream = stream
        self._label = label
        self._emitted = False

    def write(self, text: str) -> int:
        if not self._emitted and text:
            self._emitted = True
            self._stream.write(self._label + "\n")
        return self._stream.write(text)

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


def main(argv: list[str] | None = None) -> int:
    tokens = list(sys.argv[1:] if argv is None else argv)
    parser = _build_parser(tokens)
    args = parser.parse_args(tokens)
    try:
        return _run(args, parser)
    except reader.RunUnreadable as exc:
        # docs/DESIGN.md 4.8 (0.3.4): the one run a command was asked about could not be read
        print(f"postmortem: {exc.run_id} {_UNREADABLE_RUN}", file=sys.stderr)
        return 4
    except Exception as exc:  # noqa: BLE001 - the last resort: nothing escapes as a traceback
        return _report_uncaught(exc, args)


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    # N3b P15: at local and raw, text output starts with the disclosure
    # label. JSON never carries it, and a command that writes nothing to
    # stdout (an error that only writes to stderr) prints no label either.
    level = getattr(args, "level", "share")
    if level in FIRST_LINE and not getattr(args, "json", False):
        real_stdout = sys.stdout
        sys.stdout = _FirstLineStdout(real_stdout, FIRST_LINE[level])
        try:
            return _dispatch(args, parser)
        finally:
            sys.stdout = real_stdout
    return _dispatch(args, parser)


def _report_uncaught(exc: Exception, args: argparse.Namespace) -> int:
    """F fix 8 (W8-5, docs/DESIGN.md 5.1): an exception no command handled prints one line, never
    the interpreter's traceback (which carries the raw path an OSError names). The text is
    `Redactor.exception_text` at the active level; if even that fails, the class name alone.
    An interrupt or an exit is not an `Exception` and is not caught."""
    name = type(exc).__name__
    try:
        redactor = Redactor(context=_build_context(getattr(args, "level", "share")))
        shown = redactor.exception_text(exc)
        line = f"agent-postmortem: unexpected {name}: {shown}"
    except Exception:  # noqa: BLE001 - the report of a failure must never fail in turn
        line = f"agent-postmortem: unexpected {name}"
    try:
        print(line, file=sys.stderr)
    except Exception:  # noqa: BLE001 - a closed stderr is not a reason to raise
        pass
    return 1


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "rules":
        return _cmd_rules(args)
    if args.command == "apply":
        return _cmd_apply(args)
    if args.command == "trend":
        return _cmd_trend(args)
    if args.command == "demo":
        return _cmd_demo(args)
    if args.command == "rescue":
        if args.rescue_command == "plan":
            return _cmd_rescue_plan(args)
        if args.rescue_command == "status":
            return _cmd_rescue_status(args)
    if args.command == "watch":
        return _cmd_watch(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "doctor":
        return _cmd_doctor(args)
    if args.command == "statusline":
        return statusline.main_subagents()
    if args.command == "hook":
        return hooks_cli.dispatch(args.hook_command)

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
