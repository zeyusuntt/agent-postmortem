"""Warm against cold `scan` on a frozen copy of real logs (docs/DESIGN.md 4.14.9 items 3, 4).

    PYTHONPATH=src python scripts/scan_cache_check.py [--source DIR] [--work DIR] [--form json|text]

1. Copies --source (default ~/.claude/projects) into --work: an APFS clone (`cp -cRp`) where the
   filesystem allows it, else a plain copy. Nothing under --source is ever written.
2. Cold `scan` of the copy into a private state directory A.
3. Appends a copy of its last line, stamped one minute before the pinned clock, to the main
   transcript and to one subagent transcript of the copy's session with the most subagents
   (the case a live session makes on every scan); the new end time changes that session's row,
   so the cold output before the growth differs from the two after it.
4. Warm `scan` on state A; then a cold `scan` on state B, a copy of A without either cache (the
   same disclosure secret, so the same pseudonyms).
5. Compares the two scans: exit status, stdout byte for byte, and stderr without the progress
   line (which a scan prints only when it will read more than 50 runs).

Every scan runs in its own process with the clocks the output reads pinned to one instant, so
ages match between runs minutes apart. Prints, per scan: wall and CPU seconds, runs loaded,
transcripts parsed and reused, and the load averages before and after; then the comparison.
Exit status 0 when the two scans match and neither was a usage error (exit 1), found no runs
(exit 2) or printed nothing. Standard library only, plus agent_postmortem.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


def _scan_child(state: str, out: str, fixed: str, scan_args: list[str]) -> int:
    """One scan with pinned clocks and counters, run in a child process."""
    import types

    from agent_postmortem import cli, reader, report
    from agent_postmortem.detectors import claim_contradiction

    when = datetime.fromisoformat(fixed)
    report._now = lambda: when
    claim_contradiction._now = lambda: when
    reader.time = types.SimpleNamespace(time=lambda: when.timestamp())
    os.environ["XDG_STATE_HOME"] = state
    counts = {"loaded": 0, "parsed": 0, "reused": 0}
    real_read = reader._read_transcript
    real_load = cli._load_listed_run
    real_memo_read = reader.TranscriptMemo.read

    def read(*args, **kwargs):
        counts["parsed"] += 1
        return real_read(*args, **kwargs)

    def load(ref, unreadable):
        counts["loaded"] += 1
        return real_load(ref, unreadable)

    def memo_read(self, *args, **kwargs):
        before = self.reused
        result = real_memo_read(self, *args, **kwargs)
        counts["reused"] += self.reused - before
        return result

    reader._read_transcript = read
    cli._load_listed_run = load
    reader.TranscriptMemo.read = memo_read
    wall, cpu = time.perf_counter(), time.process_time()
    with open(out, "w", encoding="utf-8") as handle:
        stdout = sys.stdout
        sys.stdout = handle
        try:
            code = cli.main(["scan", *scan_args])
        finally:
            sys.stdout = stdout
    counts.update(wall=time.perf_counter() - wall, cpu=time.process_time() - cpu, code=code)
    print(json.dumps(counts), file=sys.stderr)
    return code


def _load() -> str:
    return " / ".join(f"{value:.1f}" for value in os.getloadavg())


def _scan(label: str, state: Path, out: Path, fixed: str, scan_args: list[str]) -> dict:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    before = _load()
    child = subprocess.run(
        [sys.executable, __file__, "_scan", str(state), str(out), fixed, *scan_args],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": "src"},
    )
    lines = child.stderr.splitlines()
    if not lines or not lines[-1].startswith("{"):
        sys.exit(f"{label}: scan did not finish\n{child.stderr}")
    counts = json.loads(lines[-1])
    if counts["code"] in (1, 2) or not out.read_bytes():
        sys.exit(f"{label}: scan exit {counts['code']}, {out.stat().st_size} bytes of output\n"
                 f"{child.stderr}")
    stderr = [line for line in lines[:-1] if not line.startswith("postmortem: scanning ")]
    out.with_suffix(".err").write_text("\n".join(stderr), encoding="utf-8")
    print(f"{label:<44} wall {counts['wall']:6.2f} s  cpu {counts['cpu']:6.2f} s  "
          f"runs loaded {counts['loaded']:4d}  parsed {counts['parsed']:5d}  "
          f"reused {counts['reused']:5d}  load {before} -> {_load()}")
    return counts


def _largest_session(root: Path) -> tuple[Path, Path] | None:
    """(main transcript, a middle subagent transcript) of the session with the most subagents."""
    best = None
    for subagents in root.glob("*/*/subagents"):
        children = sorted(subagents.glob("agent-*.jsonl"))
        main = subagents.parent.with_suffix(".jsonl")
        if children and main.is_file() and (best is None or len(children) > len(best[1])):
            best = (main, children)
    return None if best is None else (best[0], best[1][len(best[1]) // 2])


def _append_last_line(path: Path, when: datetime) -> None:
    last = path.read_bytes().rstrip(b"\n").rsplit(b"\n", 1)[-1]
    try:
        entry = json.loads(last)
    except ValueError:
        entry = None
    if isinstance(entry, dict):
        entry["timestamp"] = when.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        last = json.dumps(entry).encode("utf-8")
    with path.open("ab") as handle:
        handle.write(last + b"\n")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "_scan":
        return _scan_child(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, default=Path("~/.claude/projects").expanduser())
    parser.add_argument("--work", type=Path, default=Path("/private/tmp/ap-scan-check"))
    parser.add_argument("--form", choices=("json", "text"), default="json")
    args = parser.parse_args()
    source, work = args.source.resolve(), args.work.resolve()
    if work == source or source in work.parents or work in source.parents:
        sys.exit("--work must be outside --source")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, mode=0o700)
    root = work / "projects"
    if subprocess.run(["cp", "-cRp", str(source), str(root)], capture_output=True).returncode:
        shutil.rmtree(root, ignore_errors=True)
        shutil.copytree(source, root)
    fixed = (datetime.now(timezone.utc) + timedelta(minutes=30)).replace(microsecond=0).isoformat()
    scan_args = ["--root", str(root), *(["--json"] if args.form == "json" else [])]
    state_a, state_b = work / "state-a", work / "state-b"
    _scan("cold, state A", state_a, work / "cold-before.out", fixed, scan_args)
    grown = _largest_session(root)
    if grown is None:
        sys.exit("no session with subagents in --source")
    for path in grown:
        _append_last_line(path, datetime.fromisoformat(fixed) - timedelta(minutes=1))
    warm = _scan("warm, state A, largest session grown", state_a, work / "warm.out", fixed,
                 scan_args)
    shutil.copytree(state_a, state_b, symlinks=True)
    (state_b / "agent-postmortem" / "scan-cache.json").unlink(missing_ok=True)
    shutil.rmtree(state_b / "agent-postmortem" / "transcript-cache", ignore_errors=True)
    cold = _scan("cold, state B (no cache), same data", state_b, work / "cold.out", fixed,
                 scan_args)
    same = all(
        (work / f"warm{suffix}").read_bytes() == (work / f"cold{suffix}").read_bytes()
        for suffix in (".out", ".err")
    ) and warm["code"] == cold["code"]
    print(f"warm  {_sha(work / 'warm.out')}\ncold  {_sha(work / 'cold.out')}\n"
          f"before growth {_sha(work / 'cold-before.out')}")
    moved = (work / "cold-before.out").read_bytes() != (work / "cold.out").read_bytes()
    print(f"exit status warm {warm['code']}, cold {cold['code']}")
    print(f"{'IDENTICAL' if same else 'DIFFERENT'}; warm scan parsed {warm['parsed']}, "
          f"reused {warm['reused']}; the growth {'changed' if moved else 'did not change'} "
          "the output")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
