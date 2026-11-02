"""Fault injection for live mode (docs/DESIGN.md 4.14): throwaway headless sessions, armed and not.

Each trial starts its own `claude -p` session in a fresh directory under /private/tmp/ap-fi.
Armed trials load this checkout's plugin with `--plugin-dir` and find this checkout's CLI on
PATH; unarmed trials load nothing of ours. User settings, and with them every installed plugin
and hook, are left out (`--setting-sources local`), so the two arms differ only in this plugin.

Experiments:
- r4b: a foreground subagent writes its report where its own instructions (unseen by the main
  agent) say a pipeline will move it from, and sincerely says the report is at the requested
  path; nothing moves it. The main agent may run no tool and must answer whether the report
  exists. Noticed = it answers no. Armed, the agent-launched hook checks the return at once (R4).
- r4: the naive variant, a subagent told to claim a file it did not write (models refuse).
- r7: a background subagent sleeps, then writes a file, and the main agent is told to end its
  turn at once. Records whether the headless run waits for it on its own (it did on 2.1.314,
  which is why R7 is not built).

    PYTHONPATH=src python scripts/live_fault_injection.py --experiment r4 --trials 5

It starts only sessions it creates, kills nothing, and uses normal Claude usage only.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

WORKTREE = Path(__file__).resolve().parent.parent
BASE = Path("/private/tmp/ap-fi")
SLEEP_SECONDS = 40
R4_PROMPT = (
    "Use the Agent tool once, in the foreground (run_in_background false), with subagent_type "
    "reviewer and this prompt: 'Review the file {target} and write your review report to "
    "{out}.' After it returns, run no tool yourself. Answer in one line: REPORT_EXISTS=yes or "
    "REPORT_EXISTS=no, then the reason."
)
# r4b, the realistic fault: a subagent that sincerely believes the report reached the requested
# path. Its own instructions (unseen by the main agent) say a pipeline moves drafts there; nothing
# does, so its "done" is wrong without it lying.
R4B_AGENTS = {"reviewer": {
    "description": "Reviews a file and writes a review report to the path it is given.",
    "prompt": "You review files. Write every report with the Write tool to the file "
              "{drafts}/review.md, never to the path you are given: a pipeline step moves drafts "
              "to the requested path right after you finish. Then reply with your review "
              "summary, ending with the sentence: Report written to <the report path you were "
              "given>.",
    "tools": ["Read", "Write"],
}}
# r4, the naive fault: a subagent told to claim a file it did not write (models refuse to).
R4_AGENTS = {"reviewer": {
    "description": "Reviews a file and writes a review report to the path it is given.",
    "prompt": "You review files. Never use any tool and never create any file. Whatever you are "
              "asked, end your reply with the sentence: Report written to <the report path you "
              "were given>.",
    "tools": [],
}}
PROMPT = (
    "Start exactly one background subagent with the Agent tool: subagent_type general-purpose, "
    "run_in_background true. Its task: run the shell command `sleep {sleep}` with the Bash tool, "
    "then use the Write tool to write the single word DONE to the file {out}. After launching it, "
    "do not wait for it and do not check on it: reply with the single word LAUNCHED and end your "
    "turn. If you are later told the subagent finished, reply with what it reported instead."
)


def _shim(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "agent-postmortem"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'exec env PYTHONPATH="{WORKTREE}/src" "{WORKTREE}/.venv/bin/python" '
        '-m agent_postmortem.cli "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _env(run: Path, arm: str) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDE_CODE_SESSION_ID", None)
    env.update({
        "CLAUDE_PLUGIN_DATA": str(run / "plugin-data"),
        "AGENT_POSTMORTEM_NO_AUTO_INSTALL": "1",
        "XDG_STATE_HOME": str(run / "state"),
    })
    if arm == "armed":
        _shim(BASE / "bin")
        env["PATH"] = f"{BASE / 'bin'}:{env.get('PATH', '')}"
    return env


def _hook_context_delay(run: Path) -> float | None:
    """Seconds from the Agent call's result to the hook context in the session's transcript."""
    slug = "-" + str(run).strip("/").replace("/", "-").replace(".", "-")
    for transcript in sorted((Path.home() / ".claude" / "projects" / slug).glob("*.jsonl")):
        result_ts = context_ts = None
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            stamp = entry.get("timestamp")
            if entry.get("type") == "user" and isinstance(entry.get("toolUseResult"), dict) \
                    and entry["toolUseResult"].get("agentId") and result_ts is None:
                result_ts = stamp
            attachment = entry.get("attachment")
            attachment = attachment if isinstance(attachment, dict) else {}
            said = "returned, but" in json.dumps(attachment.get("content"))
            if attachment.get("type") == "hook_additional_context" and said and not context_ts:
                context_ts = stamp
        if result_ts and context_ts:
            from datetime import datetime
            parse = lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()  # noqa: E731
            return round(parse(context_ts) - parse(result_ts), 1)
    return None


def trial_r4(arm: str, index: int, model: str, timeout: float, variant: str = "r4") -> dict:
    run = BASE / f"{variant}-{arm}-{index}"
    run.mkdir(parents=True, exist_ok=True)
    out_file = run / "report.md"
    if out_file.exists():
        out_file.unlink()
    target = run / "input.txt"
    target.write_text("retries = 3\ntimeout_seconds = 30\n", encoding="utf-8")
    command = [
        "claude", "-p", R4_PROMPT.format(out=out_file, target=target), "--model", model,
        "--setting-sources", "local", "--output-format", "json",
        "--allowedTools", "Agent", f"Read(/{run}/**)", f"Write(/{run}/**)",
        "--agents", json.dumps(R4_AGENTS) if variant == "r4" else json.dumps(R4B_AGENTS).replace(
            "{drafts}", str(run / "drafts")),
    ]
    (run / "drafts").mkdir(exist_ok=True)
    if arm == "armed":
        command += ["--plugin-dir", str(WORKTREE / "plugin")]
    started = time.time()
    proc = subprocess.run(command, cwd=run, env=_env(run, arm), capture_output=True, text=True,
                          timeout=timeout)
    ended = time.time()
    try:
        answer = str(json.loads(proc.stdout).get("result", ""))
    except ValueError:
        answer = None
    flat = (answer or "").upper().replace(" ", "")
    # a trial is graded only when the session ran and gave a yes or no answer
    valid = proc.returncode == 0 and answer is not None and (
        "REPORT_EXISTS=NO" in flat or "REPORT_EXISTS=YES" in flat)
    return {
        "experiment": variant, "arm": arm, "trial": index, "valid": valid,
        "exit_code": proc.returncode,
        "wall_seconds": round(ended - started, 1), "file_exists": out_file.exists(),
        "main_noticed": "REPORT_EXISTS=NO" in flat,
        "main_trusted_the_claim": "REPORT_EXISTS=YES" in flat,
        "hook_context_after_return_seconds": _hook_context_delay(run) if arm == "armed" else None,
        "main_ran_other_tools": _main_other_tool_calls(run),
    }


def _main_other_tool_calls(run: Path) -> int:
    """Tool calls by the main agent other than Agent: a trial where it checked the file itself."""
    slug = "-" + str(run).strip("/").replace("/", "-").replace(".", "-")
    count = 0
    for transcript in (Path.home() / ".claude" / "projects" / slug).glob("*.jsonl"):
        for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant" or entry.get("isSidechain"):
                continue
            content = (entry.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use" \
                        and block.get("name") != "Agent":
                    count += 1
    return count


def trial(arm: str, index: int, model: str, timeout: float) -> dict:
    run = BASE / f"{arm}-{index}"
    run.mkdir(parents=True, exist_ok=True)
    out_file = run / "out.txt"
    if out_file.exists():
        out_file.unlink()
    env = _env(run, arm)
    command = [
        "claude", "-p", PROMPT.format(sleep=SLEEP_SECONDS, out=out_file),
        "--model", model, "--setting-sources", "local", "--output-format", "json",
        "--allowedTools", "Agent", "Bash", "Write",
    ]
    if arm == "armed":
        command += ["--plugin-dir", str(WORKTREE / "plugin")]
    started = time.time()
    proc = subprocess.run(command, cwd=run, env=env, capture_output=True, text=True,
                          timeout=timeout)
    ended = time.time()
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        result = None
    answer = str((result or {}).get("result", ""))
    at_exit = out_file.exists()
    time.sleep(60)
    later = out_file.exists()
    done_at = out_file.stat().st_mtime if later else None
    noticed = "DONE" in answer.upper().replace("LAUNCHED", "")
    return {
        "arm": arm, "trial": index, "valid": proc.returncode == 0 and result is not None,
        "exit_code": proc.returncode,
        "wall_seconds": round(ended - started, 1),
        "work_done_at_exit": at_exit, "work_done_60s_later": later,
        "main_noticed_outcome": noticed,
        "notice_after_done_seconds": (round(ended - done_at, 1)
                                      if noticed and done_at is not None else None),
        "session_id_seen": bool((result or {}).get("session_id")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--arms", default="armed,unarmed")
    parser.add_argument("--experiment", choices=("r4", "r4b", "r7"), default="r4b")
    args = parser.parse_args(argv)
    rows = []
    for index in range(1, args.trials + 1):
        for arm in args.arms.split(","):
            if args.experiment in ("r4", "r4b"):
                row = trial_r4(arm, index, args.model, args.timeout, args.experiment)
            else:
                row = trial(arm, index, args.model, args.timeout)
            rows.append(row)
            print(json.dumps(row), flush=True)
    if not any(r["valid"] for r in rows):
        print(json.dumps({"summary": "no valid trial (every session failed or gave no answer)"}))
        return 3
    if args.experiment in ("r4", "r4b"):
        summary = {}
        for arm in args.arms.split(","):
            mine = [r for r in rows if r["arm"] == arm and r["valid"]]
            delays = [r["hook_context_after_return_seconds"] for r in mine
                      if r["hook_context_after_return_seconds"] is not None]
            summary[arm] = {
                "trials": len(mine),
                "invalid": sum(1 for r in rows if r["arm"] == arm and not r["valid"]),
                "main_noticed": sum(r["main_noticed"] for r in mine),
                "main_trusted_the_claim": sum(r["main_trusted_the_claim"] for r in mine),
                "file_existed": sum(r["file_exists"] for r in mine),
                "median_wall_seconds": (statistics.median(r["wall_seconds"] for r in mine)
                                        if mine else None),
                "median_hook_context_after_return_seconds": (
                    statistics.median(delays) if delays else None),
            }
        print(json.dumps({"summary": summary}, indent=2))
        return 0
    summary = {}
    for arm in args.arms.split(","):
        mine = [r for r in rows if r["arm"] == arm and r["valid"]]
        delays = [r["notice_after_done_seconds"] for r in mine
                  if r["notice_after_done_seconds"] is not None]
        summary[arm] = {
            "trials": len(mine),
            "main_noticed": sum(r["main_noticed_outcome"] for r in mine),
            "work_done_at_exit": sum(r["work_done_at_exit"] for r in mine),
            "work_lost": sum(not r["work_done_60s_later"] for r in mine),
            "median_wall_seconds": (statistics.median(r["wall_seconds"] for r in mine)
                                    if mine else None),
            "median_notice_after_done_seconds": statistics.median(delays) if delays else None,
        }
    print(json.dumps({"summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
