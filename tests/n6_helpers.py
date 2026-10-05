"""Synthetic builders for the N6 identity tests (not a test_*.py module).

Everything is synthetic and assembled at run time: session UUIDs come from
`uid(n)`, agent ids from `aid(n)`, working directories sit under a caller's
tmp_path or under /work, and every workflow run id is a registered wf_ id.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = datetime(2026, 1, 5, 12, 0, 0, tzinfo=timezone.utc)

SLUG_A = "-home-user-n6a"
SLUG_B = "-home-user-n6b"

WF1 = "wf_00000000-6a1"
WF2 = "wf_00000000-6a2"
WF3 = "wf_00000000-6a3"
WF_DUP = "wf_00000000-6a4"


def uid(n: int) -> str:
    """A session UUID that is never a real one."""
    return str(uuid.UUID(int=(0x6E6 << 100) + n))


def aid(n: int) -> str:
    """A conforming agent id: 'a' plus 16 hex characters."""
    return "a6e6" + format(n, "013x")


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def entry(
    kind: str, text: str, moment: datetime | None, *, cwd=..., session: str | None = None,
    agent: str | None = None,
) -> dict:
    """One transcript line. `cwd=...` (the default) leaves the key out; any other
    value, including None, "" and a number, is written as given."""
    if kind == "user":
        row: dict = {"type": "user", "message": {"role": "user", "content": text}}
    else:
        row = {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}],
                        "usage": {}},
        }
    if moment is not None:
        row["timestamp"] = iso(moment)
    if cwd is not ...:
        row["cwd"] = cwd
    if session is not None:
        row["sessionId"] = session
    if agent is not None:
        row["agentId"] = agent
    return row


def write_lines(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(row if isinstance(row, str) else json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def transcript_rows(
    cwd, start: datetime | None, *, session: str | None = None, agent: str | None = None,
) -> list[dict]:
    second = None if start is None else start + timedelta(seconds=1)
    return [
        entry("user", "work on it", start, cwd=cwd, session=session, agent=agent),
        entry("assistant", "done", second, cwd=cwd, session=session, agent=agent),
    ]


def session_path(root: Path, slug: str, session: str) -> Path:
    return root / slug / f"{session}.jsonl"


def write_session(
    root: Path, slug: str, session: str, *, cwd, start: datetime | None = BASE,
    children: tuple = (),
) -> Path:
    """A session run: the main transcript plus one subagent transcript per
    (agent_id, cwd, start) triple of `children`. Returns the main path."""
    path = session_path(root, slug, session)
    write_lines(path, transcript_rows(cwd, start, session=session))
    for child_id, child_cwd, child_start in children:
        write_lines(
            root / slug / session / "subagents" / f"agent-{child_id}.jsonl",
            transcript_rows(child_cwd, child_start, session=session, agent=child_id),
        )
    return path


def workflow_dir(root: Path, slug: str, parent: str, wf_id: str) -> Path:
    return root / slug / parent / "subagents" / "workflows" / wf_id


def write_workflow(
    root: Path, slug: str, parent: str, wf_id: str, attempts: tuple, *, ghosts: tuple = (),
    strays: tuple = (), parent_cwd=..., journal_extra: dict | None = None,
) -> Path:
    """A workflow run. `attempts` is (agent_id, cwd, start) triples, each with a
    journal started and result event and a transcript. `ghosts` are agent ids with a
    started event and no transcript; `strays` are agent ids with a transcript and no
    journal event. A `parent_cwd` other than `...` also writes the parent session
    transcript. Returns the run directory."""
    run_dir = workflow_dir(root, slug, parent, wf_id)
    journal: list[dict] = []
    for index, (agent_id, cwd, start) in enumerate(attempts):
        started = {"type": "started", "key": f"k{index}", "agentId": agent_id,
                   "label": "impl", "phase": "Implement"}
        if journal_extra:
            started.update(journal_extra)
        journal.append(started)
        journal.append({"type": "result", "key": f"k{index}", "agentId": agent_id,
                        "result": {"ok": True}})
        write_lines(run_dir / f"agent-{agent_id}.jsonl",
                    transcript_rows(cwd, start, agent=agent_id))
    for index, agent_id in enumerate(ghosts):
        journal.append({"type": "started", "key": f"g{index}", "agentId": agent_id,
                        "label": "ghost", "phase": "Implement"})
    for agent_id in strays:
        write_lines(run_dir / f"agent-{agent_id}.jsonl",
                    transcript_rows("/work/n6/stray", BASE, agent=agent_id))
    write_lines(run_dir / "journal.jsonl", journal)
    if parent_cwd is not ...:
        write_lines(session_path(root, slug, parent),
                    transcript_rows(parent_cwd, BASE, session=parent))
    return run_dir


@dataclass
class IdentityRoot:
    """One synthetic root with two projects.

    Project A: session S1 (children s1a, s1b); session S2; workflow WF1 under parent P1
    (attempts w1a, w1b and the transcript-less attempt ghost); workflow WF2 under P1
    (attempts w2a, w2b and a stray transcript named by no journal event).
    Project B: session S2 again (the same UUID, a second run); workflow WF3 under P3
    (attempts w1a again, so one agent id lives in two runs, and w3b).
    """

    root: Path
    s1: str
    s2: str
    p1: str
    p3: str
    s1a: str
    s1b: str
    w1a: str
    w1b: str
    ghost: str
    w2a: str
    w2b: str
    stray: str
    w3b: str
    paths: dict = field(default_factory=dict)


def build_identity_root(tmp_path: Path) -> IdentityRoot:
    root = tmp_path / "idroot"
    ident = IdentityRoot(
        root=root, s1=uid(1), s2=uid(2), p1=uid(3), p3=uid(4), s1a=aid(11), s1b=aid(12),
        w1a=aid(21), w1b=aid(22), ghost=aid(23), w2a=aid(31), w2b=aid(32), stray=aid(33),
        w3b=aid(42),
    )
    start = BASE
    ident.paths["s1"] = write_session(
        root, SLUG_A, ident.s1, cwd="/work/n6/s1", start=start,
        children=((ident.s1a, "/work/n6/s1", start), (ident.s1b, "/work/n6/s1", start)),
    )
    ident.paths["s2a"] = write_session(root, SLUG_A, ident.s2, cwd="/work/n6/s2", start=start)
    ident.paths["s2b"] = write_session(root, SLUG_B, ident.s2, cwd="/work/n6/s2", start=start)
    ident.paths["wf1"] = write_workflow(
        root, SLUG_A, ident.p1, WF1,
        ((ident.w1a, "/work/n6/w1", start), (ident.w1b, "/work/n6/w1", start)),
        ghosts=(ident.ghost,),
    )
    ident.paths["wf2"] = write_workflow(
        root, SLUG_A, ident.p1, WF2,
        ((ident.w2a, "/work/n6/w2", start), (ident.w2b, "/work/n6/w2", start)),
        strays=(ident.stray,),
    )
    ident.paths["wf3"] = write_workflow(
        root, SLUG_B, ident.p3, WF3,
        ((ident.w1a, "/work/n6/w3", start), (ident.w3b, "/work/n6/w3", start)),
    )
    return ident


# The stderr texts N6 pins (docs/DESIGN.md 4.8, 4.13.6). Spans (path-N, value-N,
# uuid:<last4>) are what share and local print for a value the user typed.
EXPECTED_RUN = (
    "expected a run id, a unique prefix of one, a session UUID, an agent-<id> "
    "transcript stem, a:<alias>, or the path of a run directory"
)
EXPECTED_UNAVAILABLE = (
    "expected a run id, a session UUID, an agent-<id> transcript stem, or a:<alias>"
)
PREFIX_ADVICE = "expected a longer prefix or the path of a run directory"
EXACT_ADVICE = "expected one of these run ids or the path of a run directory"
NO_BOTH = "postmortem: run takes either RUN_ID or --here, not both\n"
NO_NEITHER = "postmortem: run needs one of RUN_ID or --here\n"
HERE_TAIL = "worked in this directory or a parent of it"
