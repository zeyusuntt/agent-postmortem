"""postmortem: turn a Claude Code Workflow run directory into a verdict.

Five subcommands: scan (a whole fleet of runs), run (one run), rules (what
to do about the findings), apply (write a rule into a file you maintain),
trend (whether an applied rule worked). See docs/DESIGN.md section 4.7 for
the full CLI contract and exit codes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from importlib import resources
from pathlib import Path

from . import __version__, detectors, hooks_cli, reader, report, rules, state, trend
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
        "scan", help="Scan every run under --root and print one line per run plus fleet totals."
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

    apply_cmd = sub.add_parser(
        "apply", help="Write a rule into a target markdown file as a managed block."
    )
    apply_cmd.add_argument(
        "rule_id", metavar="RULE_ID", help="The rule to apply, for example R-RETRY-SAFE."
    )
    apply_cmd.add_argument(
        "--target", type=Path, required=True,
        help="Markdown file to add the rule's managed block to.",
    )
    apply_cmd.add_argument(
        "--write", action="store_true",
        help="Write the change. Without this flag, apply only prints a diff and exits 0.",
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


def _top_detector(findings: list[Finding]) -> str | None:
    if not findings:
        return None
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.detector] = counts.get(finding.detector, 0) + 1
    return max(counts, key=lambda detector_id: counts[detector_id])


def _cmd_run(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
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

    if args.bug_report:
        print(report.render_bug_report(run, findings, redactor), end="")
    elif args.json:
        print(json.dumps(report.to_json(run, findings, redactor), indent=2))
    else:
        print(report.render_run_text(run, findings, redactor), end="")

    if any(finding.severity == "critical" for finding in findings):
        return 3
    if run.status == "INCOMPLETE":
        return 4
    return 0


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
    with resources.as_file(_DEMO_RUN_REF) as demo_run_path:
        run = reader.load_run(demo_run_path)
        findings = _run_findings(run)
    if args.json:
        print(json.dumps(report.to_json(run, findings, redactor), indent=2))
    else:
        print(report.render_run_text(run, findings, redactor), end="")
    if any(finding.severity == "critical" for finding in findings):
        return 3
    if run.status == "INCOMPLETE":
        return 4
    return 0


def _cmd_scan(args: argparse.Namespace) -> int:
    root = args.root.expanduser()
    redactor = Redactor(enabled=not args.no_redact)
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

    rows: list[dict] = []
    # docs/DESIGN.md 4.7: totals are stratified by kind, never one combined
    # count -- a workflow's "runs with retries" and a session's "subagents"
    # answer different questions and must not be averaged together.
    totals = {
        "workflow": {"runs": 0, "runs_with_retries": 0, "attempts": 0, "wasted_hours": 0.0},
        "session": {"runs": 0, "subagents": 0, "interrupted": 0},
        "findings_per_detector": {},
    }

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
        rows.append({
            "run_id": run.run_id,
            "kind": run.kind,
            "date": run.started_at.date().isoformat() if run.started_at else "unknown",
            "project": run.project_slug,
            "agents": len(run.agents),
            "attempts": len(run.attempts),
            "verdict": report.verdict_word(findings, run.status),
            "top_detector": _top_detector(findings),
            "finished": run.finished,
        })

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

        for finding in findings:
            totals["findings_per_detector"][finding.detector] = (
                totals["findings_per_detector"].get(finding.detector, 0) + 1
            )

    if not rows:
        print(redactor(f"postmortem: no runs found under {root}"), file=sys.stderr)
        return 2

    rows.sort(key=lambda row: row["date"])

    if args.json:
        payload: dict = {"rows": rows, "totals": totals}
        if redactor.enabled:
            payload = report.redact_value(payload, redactor)
        print(json.dumps(payload, indent=2))
    else:
        print(report.render_scan_text(rows, totals, redactor), end="")

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
        payload = [
            {"rule_id": rule.id, "title": rule.title, "count": count} for rule, count in rows
        ]
        print(json.dumps(payload, indent=2))
    else:
        print(redactor(rules.render_rules_text(rows)), end="")
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
                f"postmortem: refusing to write into a run directory ({target}); "
                "apply targets a rules file you maintain, never a run directory"
            ),
            file=sys.stderr,
        )
        return 1

    rule = rules.RULES[args.rule_id]
    try:
        diff = rules.apply_rule(args.rule_id, target, write=args.write)
    except (ValueError, OSError, state.StateError) as exc:
        # Covers an unterminated rules block, a symlinked target
        # apply_rule refuses to write through, a filesystem failure
        # reading or writing the state file, state.lock held by another
        # process for 2s (StateLocked), and a state.json newer than this
        # build understands (StateTooNew).
        print(redactor(f"postmortem: {exc}"), file=sys.stderr)
        return 1

    # docs/DESIGN.md 5.2's D2: apply routes its diff, its success messages,
    # and its hook-install note through the same active Redactor as its own
    # error messages -- a unified diff's own header quotes the target path
    # twice, and a target under the user's home is exactly the kind of path
    # redaction exists for.
    if diff:
        print(redactor(diff), end="")
    elif getattr(diff, "already_present_unrecorded", False):
        # docs/DESIGN.md 9: the rule was already in target but state had no
        # matching record -- drift, not a plain no-op (--no-redact may show
        # this target because the user themselves typed it just now).
        print(redactor(f"postmortem: {args.rule_id}: already present, recorded ({target})"))
    else:
        print(redactor(f"postmortem: {args.rule_id} is already applied to {target}; nothing to do"))

    note = rules.render_hook_install_note(rule)
    if note:
        print(redactor(note), end="")

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
    if args.command == "hook":
        return hooks_cli.dispatch(args.hook_command)

    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
