"""G8 invariants 6 to 13 of the 0.3.1 corpus check (maintainer test support, not shipped).

Every invariant is a small pure function over plain data (so a synthetic test can hand it a
violating or a passing value) plus a collector that reads the data from a loaded run, its rendered
text and JSON, or its raw transcripts. The independent side walks raw transcripts with its own code
(`tests.g_independent_return` for T0 to T5) and imports only the shared G3 predicate
(`agent_postmortem.claims.self_reported_problems`) and the shared G7 predicate
(`agent_postmortem.markers.denial_kind`) from the package; it never calls the reader's decision.
A check returns `Tally(checked, violations, detail)`: integers and fixed words only.

  6  independent five-valued return_state vs the reader's (all, nested, resumed; both directions)
  7  exported outcome of a session child vs the independent E1 mapping; counts.interrupted
  8  END `completed` and listed by W1 in one report
  9  a session WORK AT RISK `none` while some child is not `returned`
 10  denial sentences (raw walk, shared predicate) vs the permission_denied counts
 11  a G3 SelfReport or a legacy major clause vs exactly one major claim_contradiction
 12  rescue printing NOTHING TO RESCUE for a session with a child not `returned`
 13  a session-owned workflow left out of `run --here` / `scan --here` with the variable set
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from tests import g_independent_return as ind


def quiet_main(cli, argv):
    """cli.main with stdout and stderr captured; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


KILL_PREFIX = "[Request interrupted by user"
PROVEN = frozenset({"returned", "reported_failed", "reported_killed"})
E1 = {"returned": "completed", "reported_failed": "failed", "reported_killed": "killed",
      "pending": "open"}
LEGACY_MAJOR_FIXED = ("tests_failed", "suite_failed")


@dataclass
class Tally:
    checked: int = 0
    violations: int = 0
    detail: Counter = field(default_factory=Counter)

    def add(self, other: "Tally") -> None:
        self.checked += other.checked
        self.violations += other.violations
        self.detail.update(other.detail)


# --- raw walking (no package import) -----------------------------------------------------------


def entries_of(path: Path):
    """(line number, entry) for every JSON object line of a transcript; nothing on a read error."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for number, raw in enumerate(lines, 1):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if isinstance(entry, dict):
            yield number, entry


def last_user_text(path: Path) -> str | None:
    """The text of the last user-role text a transcript carries (a string, a bare string item or a
    text block; tool results are not user text), or None."""
    last = None
    for _n, entry in entries_of(path):
        if entry.get("type") != "user":
            continue
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            last = content
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    last = block
                elif isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    last = text if isinstance(text, str) else ""
    return last


def killed_by_sentinel(path: Path) -> bool:
    text = last_user_text(path)
    return text is not None and text.startswith(KILL_PREFIX)


def error_results(path: Path):
    """The flattened text of every tool_result block with is_error true, leading whitespace
    stripped: a string content as is, a list's first text element."""
    for _n, entry in entries_of(path):
        message = entry.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if entry.get("type") != "user" or not isinstance(content, list):
            continue
        for block in content:
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            if block.get("is_error") is not True:
                continue
            body = block.get("content")
            if isinstance(body, list):
                body = next((b.get("text") for b in body
                             if isinstance(b, dict) and b.get("type") == "text"
                             and isinstance(b.get("text"), str)), "")
            yield body.lstrip(" \t\r\n") if isinstance(body, str) else ""


# --- 6: the return state ------------------------------------------------------------------------


def inv6(reader_states: dict, indep_states: dict, nested: set, resumed: set) -> dict[str, Tally]:
    """Per child present in both maps: a difference of the state word is a violation. Direction
    `reader_proves_more` (the reader proves a state the independent side does not) and
    `independent_proves_more` are counted separately, everything else is `other`."""
    out = {"all": Tally(), "nested": Tally(), "resumed": Tally()}
    for child, (r_state, _r_reason) in reader_states.items():
        if child not in indep_states:
            continue
        i_state = indep_states[child][0]
        groups = ["all"] + (["nested"] if child in nested else []) + (
            ["resumed"] if child in resumed else [])
        differs = r_state != i_state
        if not differs:
            direction = None
        elif r_state in PROVEN and i_state != r_state:
            direction = "reader_proves_more"
        elif i_state in PROVEN:
            direction = "independent_proves_more"
        else:
            direction = "other"
        for group in groups:
            out[group].checked += 1
            if differs:
                out[group].violations += 1
                out[group].detail[direction] += 1
    return out


def collect_inv6(run, main_path: Path) -> dict[str, Tally]:
    indep = ind.states(main_path)
    reader_states = {a.agent_id: (a.return_state, a.return_reason) for a in run.attempts}
    nested = set()
    sub = main_path.with_suffix("") / "subagents"
    for child in indep:
        meta_path = sub / f"agent-{child}.meta.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and ("parentAgentId" in meta or meta.get("spawnDepth") not in
                                       (None, 1, "1")):
            nested.add(child)
    resumed = {a.agent_id for a in run.attempts if a.resumes > 0}
    return inv6(reader_states, indep, nested, resumed)


# --- 7: the exported outcome and the interrupted count ------------------------------------------


def indep_outcome(state: str, killed: bool) -> str:
    return "killed" if killed else E1.get(state, "unknown")


def inv7(exported: list[str], indep_outcomes: list[str], exported_interrupted: int,
         indep_interrupts: int) -> Tally:
    tally = Tally()
    for shown, expected in zip(exported, indep_outcomes):
        tally.checked += 1
        if shown != expected:
            tally.violations += 1
            tally.detail["outcome"] += 1
    tally.checked += 1
    if exported_interrupted != indep_interrupts:
        tally.violations += 1
        tally.detail["interrupted"] += 1
    return tally


def collect_inv7(run, run_payload: dict, main_path: Path) -> Tally:
    indep = ind.states(main_path)
    sub = main_path.with_suffix("") / "subagents"
    killed = {a.agent_id: killed_by_sentinel(sub / f"agent-{a.agent_id}.jsonl")
              for a in run.attempts}
    outcomes = [indep_outcome(indep.get(a.agent_id, ("unknown", ""))[0], killed[a.agent_id])
                for a in run.attempts]
    attempts = run_payload["run"]["attempts"]
    return inv7([a["outcome"] for a in attempts], outcomes,
                run_payload["run"]["counts"]["interrupted"], sum(killed.values()))


# --- 8 by identity, 9 on the rendered text -----------------------------------------------------------------

def inv8(exported: dict, listed: set) -> Tally:
    """A child is a violation when its exported outcome (its effective end) is `completed` and the
    W1 guard lists it. Both sides are keyed by the child's identity (its agent id), never by a
    rendered name: two subagents with one label share one AGENT-table name at share."""
    tally = Tally(checked=1)
    both = {key for key in listed if exported.get(key) == "completed"}
    if both:
        tally.violations = 1
        tally.detail["agents"] += len(both)
    return tally


def collect_inv8(run) -> Tally:
    """Invariant 8 over a loaded session: the exported outcomes (`effective_end`) and the children
    the report's W1 guard lists (outside the risk population, return state not `returned`)."""
    from agent_postmortem import ledger, report
    from agent_postmortem.models import effective_end

    if run.kind != "session":
        return Tally()
    exported = {attempt.agent_id: effective_end(attempt) for attempt in run.attempts}
    listed = {a.agent_id for a in report._unreturned_subagents(run, ledger.risk_population(run))}
    return inv8(exported, listed)


def inv9(text: str, child_states: list[str]) -> Tally:
    """A rendered `WORK AT RISK  none` while a child is not `returned`."""
    tally = Tally(checked=1)
    if any(line.startswith("WORK AT RISK  none") for line in text.splitlines()) and any(
        state != "returned" for state in child_states
    ):
        tally.violations = 1
    return tally


# --- 10: denials ---------------------------------------------------------------------------------

DENIAL_RES = {
    "auto_mode": re.compile(r"Permission for this action was denied by the Claude Code auto "
                            r"mode classifier\."),
    "user": re.compile(r"The user doesn't want to proceed with this tool use\."),
    "safety_check": re.compile(r"Permission for this command was denied by a built-in Claude Code "
                               r"safety check, not by the user\."),
    "permission_rule": re.compile(r"Permission to use [A-Za-z0-9_]{1,} with command [\s\S]*? has "
                                  r"been denied\.[ \t]*\r?(?:\n|\Z)"),
}


def raw_denial_counts(path: Path) -> tuple[Counter, Counter]:
    """(counts by the shared G7 predicate, counts by the independent regexes) over one transcript's
    is_error results."""
    from agent_postmortem import markers

    shared, regex = Counter(), Counter()
    for text in error_results(path):
        kind = markers.denial_kind(text)
        if kind is not None:
            shared[kind] += 1
        for name, pattern in DENIAL_RES.items():
            if pattern.match(text):
                regex[name] += 1
                break
    return shared, regex


def inv10(raw_by_attempt: dict, finding_counts: dict) -> Tally:
    """Both maps: attempt key -> {kind: n}. A difference in either direction is a violation."""
    tally = Tally()
    for key in set(raw_by_attempt) | set(finding_counts):
        tally.checked += 1
        raw = {k: v for k, v in raw_by_attempt.get(key, {}).items() if v}
        found = {k: v for k, v in finding_counts.get(key, {}).items() if v}
        if raw != found:
            tally.violations += 1
            tally.detail["detector_missing" if sum(raw.values()) > sum(found.values())
                         else "detector_extra"] += 1
    return tally


def collect_inv10(run, findings) -> tuple[Tally, Tally]:
    """(the invariant, the predicate-vs-regex agreement tally of the raw walk)."""
    raw: dict = {}
    agree = Tally()
    attempts = ([run.main] if run.main is not None else []) + list(run.attempts)
    for attempt in attempts:
        if attempt.transcript_path is None:
            continue
        shared, regex = raw_denial_counts(Path(attempt.transcript_path))
        key = None if attempt is run.main else attempt.agent_id
        raw[key] = dict(shared)
        agree.checked += 1
        if shared != regex:
            agree.violations += 1
    found: dict = {}
    for finding in findings:
        if finding.detector == "permission_denied":
            found[finding.agent_id] = {
                "auto_mode": finding.metrics.get("auto_mode", 0),
                "user": finding.metrics.get("user", 0),
                "permission_rule": finding.metrics.get("permission_rule", 0),
                "safety_check": finding.metrics.get("safety_check", 0),
            }
    return inv10(raw, found), agree


# --- 11: the claim_contradiction coverage ----------------------------------------------------


def expected_major(result: object) -> bool:
    """A legacy major clause read from the raw journal result, or a G3 SelfReport (shared)."""
    from agent_postmortem.claims import self_reported_problems

    if not isinstance(result, dict):
        return False
    for key in LEGACY_MAJOR_FIXED:
        value = result.get(key)
        if type(value) is int and value > 0:
            return True
    if result.get("ok") is False or result.get("ready") is False:
        return True
    if isinstance(result.get("blockers"), list) and result["blockers"]:
        return True
    return bool(self_reported_problems(result))


def inv11(expected: dict, majors: dict) -> Tally:
    """expected: agent -> bool; majors: agent -> number of major claim_contradiction findings."""
    tally = Tally()
    for agent, want in expected.items():
        tally.checked += 1
        have = majors.get(agent, 0)
        if (want and have != 1) or (not want and have != 0):
            tally.violations += 1
            tally.detail["missing_or_duplicate" if want else "unexpected"] += 1
    return tally


def collect_inv11(run, findings) -> Tally:
    expected = {}
    journal = Path(run.path) / "journal.jsonl"
    raw = {}
    try:
        raw = dict(enumerate(journal.read_text(encoding="utf-8", errors="replace").splitlines(), 1))
    except OSError:
        pass
    for attempt in run.attempts:
        if attempt.journal_state != "completed" or attempt.reported_line is None:
            continue
        try:
            entry = json.loads(raw.get(attempt.reported_line, ""))
        except ValueError:
            continue
        expected[attempt.agent_id] = expected_major(entry.get("result")
                                                    if isinstance(entry, dict) else None)
    majors = Counter(f.agent_id for f in findings
                     if f.detector == "claim_contradiction" and f.severity == "major")
    return inv11(expected, majors)


# --- 12: rescue ----------------------------------------------------------------------------------


def inv12(plan_text: str, child_states: list[str]) -> Tally:
    tally = Tally(checked=1)
    if "NOTHING TO RESCUE" in plan_text and any(s != "returned" for s in child_states):
        tally.violations = 1
    return tally


# --- 13: --here and the caller's own workflows ---------------------------------------------------


def inv13(owned: set, with_variable: set, without_variable: set) -> Tally:
    """Owned workflow ids left out with the variable set are violations; `before` is how many were
    already in without it."""
    tally = Tally(checked=len(owned))
    tally.violations = len(owned - with_variable)
    tally.detail["before_included"] = len(owned & without_variable)
    tally.detail["after_included"] = len(owned & with_variable)
    return tally


def owned_workflows(main_path: Path) -> set[str]:
    base = main_path.with_suffix("") / "subagents" / "workflows"
    try:
        return {p.name for p in base.glob("wf_*") if p.is_dir()}
    except OSError:
        return set()


def first_cwd(main_path: Path) -> str | None:
    for _n, entry in entries_of(main_path):
        cwd = entry.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


def collect_inv13(root: Path, ref, cli, quiet_main) -> Tally:
    """Run both commands for the owning session `ref` as its caller, with and without the
    variable. `cli._working_directory` is replaced for the duration (the caller's directory need
    not exist on this machine)."""
    owned = owned_workflows(ref.path)
    caller = first_cwd(ref.path)
    if not owned or caller is None:
        return Tally()
    saved_cwd, saved_env = cli._working_directory, os.environ.get("CLAUDE_CODE_SESSION_ID")
    cli._working_directory = lambda: caller

    def seen() -> set:
        _c, out, _e = quiet_main(
            cli, ["scan", "--here", "--kind", "workflow", "--json", "--root", str(root)])
        scanned = set()
        try:
            scanned = {row["run_id"] for row in json.loads(out)["rows"]}
        except (ValueError, KeyError, TypeError):
            pass
        picked = cli._pick_run_here(root)
        in_run = set()
        if picked is not None:
            in_run = {picked.run.run_id} | {run.run_id for _r, run in picked.others}
        return scanned & in_run  # a violation in either command

    try:
        os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        without = seen()
        os.environ["CLAUDE_CODE_SESSION_ID"] = ref.path.stem
        with_variable = seen()
    finally:
        cli._working_directory = saved_cwd
        if saved_env is None:
            os.environ.pop("CLAUDE_CODE_SESSION_ID", None)
        else:
            os.environ["CLAUDE_CODE_SESSION_ID"] = saved_env
    return inv13(owned, with_variable, without)
