"""postmortem: turn a Claude Code Workflow run directory into a verdict.

Subcommands: scan (a whole fleet of runs), run (one run), rules (what to do
about the findings; propose/review are fleet-wide verbs under it), apply
(print a rule's diff against a file you maintain -- never writes it), trend
(weekly counts and before/after rates). See docs/DESIGN.md section 4.7/9 for
the full CLI contract and exit codes.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

from . import (
    __version__,
    config,
    detectors,
    hooks_cli,
    observe,
    reader,
    report,
    rescue,
    rules,
    state,
    trend,
    watch,
)
from .models import Finding, Run
from .redact import Redactor

# docs/DESIGN.md 8.2: the demo run ships inside the package (no second copy
# under tests/fixtures/), so `agent-postmortem demo` needs no --root and
# works from a fresh install with no real run data on disk.
#
# docs/DESIGN.md 8.2's D4: resolved through importlib.resources rather than
# Path(__file__).parent, and read back out only inside
# importlib.resources.as_file(...) at the point of use -- a plain
# Path(__file__).parent gives a real filesystem path when the package sits
# unpacked in site-packages, but reader.load_run's iterdir()/open() calls
# have nothing to open at all when the package is imported straight out of
# a wheel .whl (itself a zip) or any other zipimport-backed install; as_file
# is what makes that case transparently extract to a real temp directory
# instead.
_DEMO_RUN_REF = resources.files("agent_postmortem") / "demo" / "wf_00000000-d30"


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


class _Parser(argparse.ArgumentParser):
    """An ArgumentParser whose own usage errors exit 1, not argparse's default 2.

    Exit code 2 is reserved for "no runs found" (docs/DESIGN.md section 4.7);
    a bad flag is a usage error and must exit 1.
    """

    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(1, f"{self.prog}: error: {message}\n")


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


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="postmortem",
        description=(
            "Turn the run directories a Claude Code Workflow leaves behind into a "
            "verdict, evidence, and the rule that would have prevented the failure."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"agent-postmortem {__version__}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser(
        "scan",
        help=(
            "Scan every run under --root and print the runs that need attention. "
            "--all prints the full one-line-per-run list instead."
        ),
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
        help="Show at most N rows in the NEEDS ATTENTION list (default: 10; ignored with --all).",
    )
    scan.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    scan.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths and session ids instead of redacted placeholders.",
    )

    run = sub.add_parser(
        "run", help="Diagnose one run: verdict, findings with evidence, and the next action."
    )
    run.add_argument(
        "run_id", metavar="RUN_ID|PATH",
        help=(
            "A run id (for example wf_b0000000-002), a unique prefix of one, or a path "
            "to a run directory."
        ),
    )
    run.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    run.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    run.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths and session ids instead of redacted placeholders.",
    )
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
        help="Show up to two path:line evidence excerpts under each story.",
    )

    rules = sub.add_parser(
        "rules", help="List suggested rules and how many findings back each one."
    )
    rules.add_argument(
        "--run", dest="run_id", metavar="RUN_ID|PATH", default=None,
        help="Limit the count to one run instead of a full scan.",
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
    rules.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths and session ids instead of redacted placeholders.",
    )

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
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    rules_propose.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths and session ids instead of redacted placeholders.",
    )

    rules_review = rules_sub.add_parser(
        "review",
        help=(
            "Print each adopted rule's detector rate before and after adoption, "
            "and list rules to consider removing. Read-only; the owner records "
            "an adoption in their own config."
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
    rules_review.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths instead of redacted placeholders.",
    )

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
    apply_cmd.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths in error messages instead of redacted placeholders.",
    )

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
    trend.add_argument(
        "--no-redact", action="store_true",
        help="Print raw paths instead of redacted placeholders.",
    )

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
    )
    rescue_plan.add_argument(
        "run_id", metavar="RUN_ID|PATH",
        help="A run id, a unique prefix of one, or a path to a run directory.",
    )
    rescue_plan.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    rescue_plan.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    rescue_plan.add_argument(
        "--no-redact", action="store_true",
        help="Print real repo-relative paths and the prompt's first 300 characters.",
    )

    rescue_status = rescue_sub.add_parser(
        "status",
        help=(
            "Print, per unit of work, which declared check the main session's "
            "transcript shows was run after the run ended, and what it observed."
        ),
    )
    rescue_status.add_argument(
        "run_id", metavar="RUN_ID|PATH",
        help="A run id, a unique prefix of one, or a path to a run directory.",
    )
    rescue_status.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    rescue_status.add_argument(
        "--json", action="store_true", help="Print machine-readable JSON instead of text."
    )
    rescue_status.add_argument(
        "--no-redact", action="store_true",
        help="Print real repo-relative paths instead of redacted placeholders.",
    )

    watch_cmd = sub.add_parser(
        "watch",
        help=(
            "Read-only follower for one running workflow: prints KILL/FAILED/QUIET as "
            "they happen, then DONE or LIMIT. Designed to be a Claude Code Monitor command."
        ),
    )
    watch_cmd.add_argument(
        "run_id", metavar="RUN_ID|PATH",
        help="A run id, a unique prefix of one, or a path to a run directory.",
    )
    watch_cmd.add_argument(
        "--root", type=Path, default=_default_root(),
        help="Directory to search when RUN_ID is not itself a path (default: ~/.claude/projects).",
    )
    watch_cmd.add_argument(
        "--interval", type=_bounded_float("--interval", 0.2, 300), default=watch.DEFAULT_INTERVAL,
        help="Seconds between polls, 0.2 to 300 (default: 5).",
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

    hook = sub.add_parser(
        "hook", help="Claude Code plugin hook entry point; reads hook JSON from stdin."
    )
    hook.add_argument(
        "hook_command",
        choices=("check", "session-start", "session-end", "workflow-launched", "capabilities"),
        metavar="{check,session-start,session-end,workflow-launched,capabilities}",
        help="Which hook event this invocation is for.",
    )

    return parser


def _resolve_run_path(run_id_or_path: str, root: Path) -> Path:
    """Resolve a RUN_ID|PATH argument (a wf_* directory, a session .jsonl
    path, or an id prefix of either kind) to exactly one run path.

    Raises FileNotFoundError if nothing matches, or LookupError (message
    lists the candidates) if a prefix matches more than one run. A direct
    path that exists but is not actually a run (an arbitrary file, an
    unrelated directory) is returned as-is; reader.load_run raises NotARun
    for it and the caller turns that into exit 2 (docs/DESIGN.md 4.2), the
    same path an unrecognized RUN_ID takes.
    """
    direct = Path(run_id_or_path)
    if direct.exists():
        return direct

    candidates = [
        ref for ref in reader.discover_runs(root) if ref.run_id.startswith(run_id_or_path)
    ]
    if not candidates:
        raise FileNotFoundError(run_id_or_path)
    if len(candidates) > 1:
        names = ", ".join(ref.run_id for ref in candidates)
        raise LookupError(f"'{run_id_or_path}' matches more than one run under {root}: {names}")
    return candidates[0].path


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
    for warning in cfg.warnings:
        print(redactor(warning), file=sys.stderr)
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


def _cmd_run(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    try:
        run_path = _resolve_run_path(args.run_id, root)
    except FileNotFoundError:
        print(redactor(f"postmortem: no run matches '{args.run_id}' under {root}"), file=sys.stderr)
        return 2
    except LookupError as exc:
        print(redactor(f"postmortem: {exc}"), file=sys.stderr)
        return 1

    try:
        run = reader.load_run(run_path)
    except reader.NotARun:
        print(redactor(f"postmortem: '{args.run_id}' is not a run"), file=sys.stderr)
        return 2

    # docs/DESIGN.md 12's D-C: `run` always computes and prints findings and
    # the verdict, even for a workflow that has not finished (a started key
    # with no result yet) -- an unfinished-but-retried run is exactly the
    # kind of run a user most wants to inspect, and hiding it behind a bare
    # "no verdict yet" line hid that. render_run_text itself adds the
    # "(unfinished)" header tag (docs/DESIGN.md 4.7); --json and
    # --bug-report keep their own structured contracts, unchanged.
    findings = _run_findings(run)

    # docs/DESIGN.md 4.9: --observe is the only way observe_run ever runs; a
    # plain `run` (the default) never starts a git process at all, and text/
    # JSON output is byte-for-byte what it was before this flag existed.
    observation = observe.observe_run(run) if args.observe else None

    if args.bug_report:
        # docs/DESIGN.md 4.10: the bug report is unchanged by this unit --
        # it has its own section order and is meant to show everything, so a
        # silenced detector's finding still appears there.
        print(report.render_bug_report(run, findings, redactor), end="")
    elif args.json:
        print(
            json.dumps(
                report.to_json(run, findings, redactor, observation, cfg.detectors_off), indent=2,
            )
        )
    else:
        print(
            report.render_run_text(
                run, findings, redactor, observation,
                verbose=args.verbose, evidence=args.evidence,
                silenced_detectors=cfg.detectors_off,
            ),
            end="",
        )

    if any(finding.severity == "critical" for finding in findings):
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
    try:
        run_path = _resolve_run_path(args.run_id, root)
    except FileNotFoundError:
        print(redactor(f"postmortem: no run matches '{args.run_id}' under {root}"), file=sys.stderr)
        return None, 2
    except LookupError as exc:
        print(redactor(f"postmortem: {exc}"), file=sys.stderr)
        return None, 1

    try:
        return reader.load_run(run_path), None
    except reader.NotARun:
        print(redactor(f"postmortem: '{args.run_id}' is not a run"), file=sys.stderr)
        return None, 2


def _cmd_rescue_plan(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code

    run, exit_code = _resolve_run_or_exit(args, root, redactor)
    if run is None:
        return exit_code

    findings = _run_findings(run)
    # docs/DESIGN.md 4.11: rescue plan always runs observe_run, read-only,
    # the same way `run --observe` does -- there is no flag to turn it off.
    observation = observe.observe_run(run)
    now = report._now()
    newest_mtime = _newest_run_mtime(run)
    live = report.is_possibly_live(run, newest_mtime, now, hooks_cli._UNKNOWN_QUIET_SECONDS)

    plan = rescue.build_plan(run, observation, findings, cfg, possibly_live=live)

    if args.json:
        print(json.dumps(rescue.plan_to_json(run, plan, redactor), indent=2))
    else:
        print(rescue.render_plan_text(run, plan, redactor), end="")

    return plan.exit_code


def _cmd_rescue_status(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code

    run, exit_code = _resolve_run_or_exit(args, root, redactor)
    if run is None:
        return exit_code

    observation = observe.observe_run(run)
    status = rescue.build_status(run, cfg, observation)

    if args.json:
        print(json.dumps(rescue.status_to_json(run, status, redactor), indent=2))
    else:
        print(rescue.render_status_text(run, status, redactor), end="")

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
    redactor = Redactor(enabled=True)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    with resources.as_file(_DEMO_RUN_REF) as demo_run_path:
        run = reader.load_run(demo_run_path)
        findings = _run_findings(run)
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
    if any(finding.severity == "critical" for finding in findings):
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
            1 for attempt in run.attempts if attempt.outcome == "killed"
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


def _cmd_scan(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
    cfg, config_exit_code = _load_config_checked(redactor)
    if config_exit_code is not None:
        return config_exit_code
    kinds = frozenset({args.kind}) if args.kind else frozenset({"workflow", "session"})
    run_refs = reader.discover_runs(root, kinds=kinds)

    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            message = redactor(f"postmortem: --since must be YYYY-MM-DD, got '{args.since}'")
            print(message, file=sys.stderr)
            return 1

    now = report._now()
    seven_days_ago = now - timedelta(days=7)

    rows: list[dict] = []
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

    for ref in run_refs:
        try:
            run = reader.load_run(ref)
        except reader.NotARun:
            # discover_runs promises a RunRef for anything matching the
            # layout glob, not that every candidate is fully loadable; one
            # bad candidate must not abort the whole fleet scan (docs/
            # DESIGN.md 4.2).
            continue
        run_started = run.started_at
        if since_date is not None and run_started is not None and run_started.date() < since_date:
            continue

        findings = _run_findings(run)
        # docs/DESIGN.md 4.10: a silenced detector's findings never inform
        # scan's own verdict, top detector, or NEEDS ATTENTION reason (the
        # same rule `run`'s text/JSON follow) -- but they still count toward
        # `totals`/`findings_per_detector` below, exactly like trend.
        shown, _silenced = config.partition_silenced(findings, cfg)
        newest_mtime = _newest_run_mtime(run)
        live = report.is_possibly_live(run, newest_mtime, now, hooks_cli._UNKNOWN_QUIET_SECONDS)
        paths_at_risk = report.scan_paths_at_risk(run)
        age = report.scan_age_string(run, now, live)
        verdict = report.verdict_word(shown, run.status)
        attention = report.is_attention_candidate(run, shown, now, live)
        reason = report.attention_reason(run, shown) if attention else None

        rows.append({
            "run_id": run.run_id,
            "kind": run.kind,
            "date": run.started_at.date().isoformat() if run.started_at else "unknown",
            "project": run.project_slug,
            "agents": len(run.agents),
            "attempts": len(run.attempts),
            "verdict": verdict,
            "top_detector": _top_detector(shown),
            "finished": run.finished,
            "attention": attention,
            "attention_reason": reason,
            "age": age,
            "possibly_live": live,
            "paths_at_risk": paths_at_risk,
            "unfinished_state": report.unfinished_state(run, live),
        })

        if attention:
            candidates.append({
                "run": run,
                "age": age,
                "verdict": verdict,
                "reason": reason,
                "command": report.attention_next_command(run, now),
                "sort_key": report.attention_sort_key(run, shown, live, paths_at_risk, now),
            })

        _accumulate_scan_totals(totals, run, findings)
        window_ts = run.ended_at or run.started_at
        if window_ts is not None and report._aware(window_ts) >= seven_days_ago:
            _accumulate_scan_totals(totals_7d, run, findings)

        for finding in findings:
            totals["findings_per_detector"][finding.detector] = (
                totals["findings_per_detector"].get(finding.detector, 0) + 1
            )

    if not rows:
        print(redactor(f"postmortem: no runs found under {root}"), file=sys.stderr)
        return 2

    rows.sort(key=lambda row: row["date"])

    if args.json:
        payload: dict = {"schema": report.SCHEMA_VERSION, "rows": rows, "totals": totals}
        if redactor.enabled:
            payload = report.redact_value(payload, redactor)
        print(json.dumps(payload, indent=2))
    elif args.all:
        print(
            report.render_scan_text(rows, totals, redactor, silenced_detectors=cfg.detectors_off),
            end="",
        )
    else:
        candidates.sort(key=lambda candidate: candidate["sort_key"])
        shown_candidates = candidates[: args.limit]
        print(
            report.render_scan_triage_text(
                shown_candidates, len(candidates), len(rows), totals_7d, totals, redactor,
                silenced_detectors=cfg.detectors_off,
            ),
            end="",
        )

    # docs/DESIGN.md 4.6/4.7: scan's own exit code must reflect the worst
    # verdict across every included run, the same as 'run' does for one run
    # -- a caller scripting against scan otherwise has no way to detect that
    # any run in the fleet was FAILED or only INCOMPLETE-ly inspected.
    any_failed = any(row["verdict"].startswith("FAILED") for row in rows)
    any_incomplete = any("(INCOMPLETE)" in row["verdict"] for row in rows)
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
    redactor = Redactor(enabled=not args.no_redact)

    if args.show:
        rule = rules.RULES.get(args.show)
        if rule is None:
            print(redactor(f"postmortem: no such rule '{args.show}'"), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(rules.rule_to_json(rule), indent=2))
        else:
            print(redactor(rules.render_rule_show(rule)), end="")
        return 0

    root = args.root.expanduser()
    if args.run_id:
        try:
            run_path = _resolve_run_path(args.run_id, root)
        except FileNotFoundError:
            message = f"postmortem: no run matches '{args.run_id}' under {root}"
            print(redactor(message), file=sys.stderr)
            return 2
        except LookupError as exc:
            print(redactor(f"postmortem: {exc}"), file=sys.stderr)
            return 1
        try:
            findings = _run_findings(reader.load_run(run_path))
        except reader.NotARun:
            print(redactor(f"postmortem: '{args.run_id}' is not a run"), file=sys.stderr)
            return 2
    else:
        run_refs = reader.discover_runs(root)
        if not run_refs and not args.all:
            print(redactor(f"postmortem: no runs found under {root}"), file=sys.stderr)
            return 2
        findings = []
        for ref in run_refs:
            try:
                findings.extend(_run_findings(reader.load_run(ref)))
            except reader.NotARun:
                # See _cmd_scan: one bad discovered candidate must not abort
                # a whole-fleet rules listing.
                continue

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
    redactor = Redactor(enabled=not args.no_redact)

    since_date = None
    if args.since:
        try:
            since_date = datetime.strptime(args.since, "%Y-%m-%d").date()
        except ValueError:
            message = f"postmortem: --since must be YYYY-MM-DD, got '{args.since}'"
            print(redactor(message), file=sys.stderr)
            return 1

    run_refs = reader.discover_runs(root)
    if not run_refs:
        print(redactor(f"postmortem: no runs found under {root}"), file=sys.stderr)
        return 2

    runs_with_findings: list[tuple[Run, list[Finding]]] = []
    for ref in run_refs:
        try:
            run = reader.load_run(ref)
        except reader.NotARun:
            # See _cmd_scan: one bad discovered candidate must not abort a
            # whole-fleet proposal scan.
            continue
        run_started = run.started_at
        if since_date is not None and run_started is not None and run_started.date() < since_date:
            continue
        runs_with_findings.append((run, _run_findings(run)))

    rows = rules.propose_rules(runs_with_findings)

    if args.json:
        payload = {
            "schema": report.SCHEMA_VERSION,
            "proposals": [rules.propose_row_to_json(row) for row in rows],
        }
        print(json.dumps(payload, indent=2))
    else:
        print(redactor(rules.render_propose_text(rows)), end="")
    return 0


def _adopted_entries(cfg: config.Config) -> list[dict]:
    """docs/DESIGN.md 9/7.5: the owner's `[[adopted]]` config rows, plus any
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
    """docs/DESIGN.md 9: the owner's adopted rules' detector rate, N weeks
    before and after adoption. Read-only: reviewing never removes a rule,
    it only lists which ones show no improvement."""
    redactor = Redactor(enabled=not args.no_redact)
    cfg = config.load_config()
    adopted = _adopted_entries(cfg)

    if not adopted:
        if args.json:
            payload = {"schema": report.SCHEMA_VERSION, "reviews": [], "consider_removing": []}
            print(json.dumps(payload, indent=2))
        else:
            print("no adopted rule to review")
        return 0

    root = args.root.expanduser()
    run_refs = reader.discover_runs(root)
    if not run_refs:
        print(redactor(f"postmortem: no runs found under {root}"), file=sys.stderr)
        return 2

    runs_with_findings: list[tuple[Run, list[Finding]]] = []
    for ref in run_refs:
        try:
            run = reader.load_run(ref)
        except reader.NotARun:
            continue
        runs_with_findings.append((run, _run_findings(run)))

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


def _cmd_apply(args: argparse.Namespace) -> int:
    redactor = Redactor(enabled=not args.no_redact)
    if args.rule_id not in rules.RULES:
        print(redactor(f"postmortem: no such rule '{args.rule_id}'"), file=sys.stderr)
        return 1

    target = args.target.expanduser()
    if _target_looks_like_a_run_directory(target):
        print(
            redactor(
                f"postmortem: refusing to target a run directory ({target}); "
                "apply targets a file you maintain, never a run directory"
            ),
            file=sys.stderr,
        )
        return 1

    rule = rules.RULES[args.rule_id]
    try:
        diff = rules.apply_rule(args.rule_id, target)
    except (ValueError, OSError) as exc:
        # Covers an unterminated rules block and a target that could not be
        # read (permissions, a broken symlink, and the like) -- apply_rule
        # only ever reads target, it never writes it (docs/DESIGN.md 9, R3).
        print(redactor(f"postmortem: {exc}"), file=sys.stderr)
        return 1

    # docs/DESIGN.md 5.2's D2: apply routes its diff, its message, and its
    # hook-install note through the same active Redactor as its own error
    # messages -- a unified diff's own header quotes the target path twice,
    # and a target under the user's home is exactly the kind of path
    # redaction exists for.
    if diff:
        print(redactor(diff), end="")
    else:
        print(redactor(f"postmortem: {args.rule_id} already present in {target}; nothing to add"))

    note = rules.render_hook_install_note(rule)
    if note:
        print(redactor(note), end="")

    # docs/DESIGN.md 9/R3: apply never writes target itself, with or without
    # a diff to show -- this closing sentence is the last line every time,
    # so the tool never leaves the impression it just did the pasting for you.
    # The target is the caller's own argument; naming it here would only
    # show its redacted form by default, which reads as a different file.
    print(
        "Apply it yourself: paste the block into the target file, or save this "
        "output and run git apply."
    )
    return 0


def _cmd_trend(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
    run_refs = reader.discover_runs(root)
    if not run_refs:
        print(redactor(f"postmortem: no runs found under {root}"), file=sys.stderr)
        return 2

    runs_with_findings: list[tuple[Run, list[Finding]]] = []
    for ref in run_refs:
        try:
            run = reader.load_run(ref)
        except reader.NotARun:
            # See _cmd_scan: one bad discovered candidate must not abort
            # the whole trend report.
            continue
        runs_with_findings.append((run, _run_findings(run)))

    weekly = trend.weekly_counts(runs_with_findings, args.weeks)
    try:
        applied = state.load_state().get("applied", [])
    except (OSError, state.StateTooNew) as exc:
        # A filesystem failure, or a state.json newer than this build
        # understands, is not "no rules applied": report it plainly rather
        # than silently showing an empty before/after section that looks
        # like no rule ever ran.
        print(redactor(f"postmortem: could not read the state file: {exc}"), file=sys.stderr)
        return 1
    before_after_rows = trend.before_after(applied, runs_with_findings)

    if args.json:
        print(json.dumps(trend.to_json(weekly, before_after_rows, args.weeks, redactor), indent=2))
    else:
        print(trend.render_trend_text(weekly, before_after_rows, args.weeks, redactor), end="")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    return watch.watch_run(
        args.run_id, root,
        interval=args.interval, max_hours=args.max_hours, quiet_after=args.quiet_after,
        done_grace=args.done_grace, json_mode=args.json, out=sys.stdout, err=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

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
    if args.command == "hook":
        return hooks_cli.dispatch(args.hook_command)

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
