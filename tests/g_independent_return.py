"""Independent re-implementation of the G plan's T0 to T5 (rev 6) (maintainer test support, not shipped).

Reads raw session transcripts with its own JSON walking and never imports the reader, so a
difference against `Attempt.return_state` is a lead to investigate in either implementation.
`states(main_path) -> {child_id: (state, reason)}` for the transcript-backed subagents of one
session (the loader's universe). Run as a script on a root: counts only.
"""
from __future__ import annotations

import collections
import json
import re
import sys
from datetime import datetime
from pathlib import Path

TAG_RE = {tag: re.compile(rf"<{tag}>(.*?)</{tag}>", re.S) for tag in ("task-id", "tool-use-id", "status")}
STOPPED = {"killed", "stopped", "cancelled"}
TERMINAL = {"completed", "failed"} | STOPPED
QUEUED_PREFIX = "Message queued for delivery"
CHILD_IDS: set = set()


class Unknown(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def envelope(text):
    """4.13.3: the whole stripped text is one envelope with exactly one of each tag."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    if not (s.startswith("<task-notification>") and s.endswith("</task-notification>")):
        return None
    values = {}
    for tag, rx in TAG_RE.items():
        found = rx.findall(s)
        if len(found) != 1:
            return None
        values[tag] = found[0].strip()
    return values["task-id"], values["tool-use-id"], values["status"]


class Transcript:
    """What T1 to T5 read from one transcript: tool uses, their results, accepted notifications."""

    def __init__(self, path: Path):
        self.path = path
        self.ok = True
        self.uses = {}                           # id -> (name, line, input, timestamp)
        self.use_count = collections.Counter()
        self.results = collections.defaultdict(list)   # id -> [(line, block, entry_tur, has_tur)]
        self.notes = []                          # (line, task, tool_use, status), deduplicated
        self.note_ts = {}                        # line -> entry timestamp
        self.bad_links = {}                      # GA-4: JSON text of a non-string use id -> tool
        self.bad_named = set()                   # GA-4: strings named by those uses and results
        try:
            data = path.read_bytes()
        except OSError:
            self.ok = False
            return
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            # G-C (GC-1): a final line with no trailing newline that is not UTF-8 is a
            # half-written line and is dropped; an undecodable byte anywhere else is unreadable
            head, sep, _tail = data.rpartition(b"\n")
            try:
                text = head.decode("utf-8") + ("\n" if sep else "")
            except UnicodeDecodeError:
                self.ok = False
                return
        raw_lines = text.splitlines()
        candidates = []
        bad_results = []
        first_origin = None
        for line_no, raw in enumerate(raw_lines, 1):
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            if "origin" in entry and first_origin is None:
                first_origin = line_no
            kind = entry.get("type")
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            content = message.get("content")
            if kind == "assistant" and isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_use":
                        tid = part.get("id")
                        if not isinstance(tid, str):
                            # GA-4 (DESIGN 4.13.1 T0): the id cannot key a launch or a request;
                            # the children its `to` or `recipient` name are malformed
                            if part.get("name") in ("Agent", "Task", "SendMessage"):
                                self.bad_links[json.dumps(tid, sort_keys=True)] = part["name"]
                                given = part.get("input") if isinstance(part.get("input"), dict) else {}
                                self.bad_named.update(
                                    v for v in (given.get("to"), given.get("recipient"))
                                    if isinstance(v, str)
                                )
                            continue
                        self.use_count[tid] += 1
                        if tid not in self.uses:
                            self.uses[tid] = (part.get("name"), line_no, part.get("input"), entry.get("timestamp"))
            if kind == "user" and isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "tool_result":
                        rid = part.get("tool_use_id")
                        if not isinstance(rid, str):
                            bad_results.append((rid, part, entry.get("toolUseResult")))
                            continue
                        self.results[rid].append(
                            (line_no, part, entry.get("toolUseResult"), "toolUseResult" in entry)
                        )
            if kind == "queue-operation" and entry.get("operation") == "enqueue":
                candidates.append((line_no, entry.get("content"), "queue", entry.get("timestamp")))
            if kind == "user":
                origin = entry.get("origin")
                if isinstance(origin, dict) and origin.get("kind") == "task-notification":
                    candidates.append((line_no, content, "user_origin", entry.get("timestamp")))
                elif "origin" not in entry:
                    candidates.append((line_no, content, "user_no_origin", entry.get("timestamp")))
        for rid, part, given_tur in bad_results:
            if json.dumps(rid, sort_keys=True) not in self.bad_links:
                continue
            tur = given_tur if isinstance(given_tur, dict) else {}
            self.bad_named.update(
                v for v in (part.get("agentId"), tur.get("agentId"), tur.get("resumedAgentId"))
                if isinstance(v, str)
            )
        best = {}
        for line_no, text, form, ts in candidates:
            if form == "user_no_origin" and first_origin is not None and line_no >= first_origin:
                continue
            env = envelope(text)
            if env is None:
                continue
            if env not in best or line_no < best[env][0]:
                best[env] = (line_no, ts)
        self.notes = sorted((line, *env) for env, (line, _ts) in best.items())
        self.note_ts = {line: ts for _env, (line, ts) in best.items()}

    def distinct_results(self, tid):
        seen, out = set(), []
        for rec in self.results.get(tid, []):
            key = json.dumps([rec[1], rec[2], rec[3]], sort_keys=True, default=str)
            if key not in seen:
                seen.add(key)
                out.append(rec)
        return out


def _result_class(rec):
    """('error'|'rejected', None) or ('ok', (status_present, status, is_async, ids)); raises on T0 shapes."""
    _line, block, tur, has_tur = rec
    if block.get("is_error") is True:
        return ("rejected" if isinstance(tur, str) else "error"), None
    ids = set()
    if "agentId" in block:
        if not isinstance(block["agentId"], str):
            raise Unknown("malformed")
        ids.add(block["agentId"])
    status_present = False
    status = is_async = None
    if has_tur:
        if not isinstance(tur, dict):
            raise Unknown("malformed")
        if "agentId" in tur:
            if not isinstance(tur["agentId"], str):
                raise Unknown("malformed")
            ids.add(tur["agentId"])
        if "status" in tur:
            status_present = True
            status = tur["status"]
            if not isinstance(status, str):
                raise Unknown("malformed")
        if "isAsync" in tur:
            is_async = tur["isAsync"]
    return "ok", (status_present, status, is_async, ids)


def _named_ids(rec):
    """The string agent ids a result carries (block `agentId`, entry `toolUseResult.agentId`),
    whether or not the result is otherwise well formed (DESIGN 4.13.1 T2: it names the child)."""
    _line, block, tur, _has = rec
    ids = set()
    if isinstance(block.get("agentId"), str):
        ids.add(block["agentId"])
    if isinstance(tur, dict) and isinstance(tur.get("agentId"), str):
        ids.add(tur["agentId"])
    return ids


def _norm(status):
    if status == "completed":
        return "returned"
    if status == "failed":
        return "reported_failed"
    if status in STOPPED:
        return "reported_killed"
    return None


def decide(child, launcher, main, issuers, meta_use=None):
    if any(child in t.bad_named for t in (launcher, main, *issuers)):
        raise Unknown("malformed")                       # GA-4
    notes = [n for n in main.notes if n[1] == child]
    agent_uses = {tid for tid, u in launcher.uses.items() if u[0] in ("Agent", "Task")}
    by_result = set()
    for tid in agent_uses:
        for rec in launcher.distinct_results(tid):
            if child in _named_ids(rec):
                by_result.add(tid)
    by_note = {n[2] for n in notes if n[2] in agent_uses}
    # DESIGN 4.13.1 T2: a third source, the launcher's Agent or Task use the child's meta
    # `toolUseId` names; a string naming no such use adds nothing
    by_meta = {meta_use} if meta_use in agent_uses else set()
    candidates = by_result | by_note | by_meta
    if not candidates:
        raise Unknown("no_evidence")
    if len(candidates) > 1:
        raise Unknown("conflict")
    tid = next(iter(candidates))
    if sum(t.use_count[tid] for t in issuers) != 1:
        raise Unknown("conflict")
    _name, launch_line, _inp, launch_ts = launcher.uses[tid]
    results = launcher.distinct_results(tid)
    if len(results) > 1:
        raise Unknown("conflict")
    sync_value = None
    if results:
        cls, info = _result_class(results[0])
        if cls in ("error", "rejected"):
            raise Unknown("malformed")
        status_present, status, is_async, ids = info
        if ids and ids != {child}:
            raise Unknown("conflict")
        if is_async is not None and not isinstance(is_async, bool):
            raise Unknown("malformed")
        if is_async is True and status in TERMINAL:
            raise Unknown("malformed")
        asynchronous = status == "async_launched" or (is_async is True and not status_present) or (not ids and tid in by_note)
        if not asynchronous:
            if not status_present or status == "completed":
                sync_value = "returned"
            elif _norm(status):
                sync_value = _norm(status)
            else:
                raise Unknown("status_unrecognized")
    elif tid not in by_note:
        raise Unknown("no_evidence")

    rounds = [(parse_ts(launch_ts), tid, launcher, launch_line)]
    for issuer in issuers:
        for sid, (name, line, inp, ts) in issuer.uses.items():
            if name != "SendMessage":
                continue
            res = issuer.distinct_results(sid)
            inp = inp if isinstance(inp, dict) else {}
            fields = {k: inp[k] for k in ("to", "recipient") if k in inp}
            names = {v for v in fields.values() if isinstance(v, str)}
            resumed = None
            if len(res) == 1 and res[0][1].get("is_error") is not True and res[0][3]:
                tur = res[0][2]
                if isinstance(tur, dict) and isinstance(tur.get("resumedAgentId"), str):
                    resumed = tur["resumedAgentId"]
            if child not in names and resumed != child:
                continue
            if res and all(r[1].get("is_error") is True for r in res):
                continue                                   # rejected: touches no child
            if not fields or any(not isinstance(v, str) for v in fields.values()):
                raise Unknown("malformed")
            # GA-7: differing values contradict only when one of them is another child's id
            if len(set(fields.values())) > 1 and (names & (CHILD_IDS - {child})):
                raise Unknown("malformed")
            if resumed == child and names != {child} and (names & (CHILD_IDS - {child})):
                raise Unknown("malformed")
            if resumed == child:
                names = {child}        # an agent NAME in `to`; resumedAgentId decides
            if len(res) != 1:
                raise Unknown("delivery_unknown")
            tur = res[0][2] if res[0][3] else None
            if not isinstance(tur, dict) or tur.get("success") is not True:
                raise Unknown("malformed")
            if "resumedAgentId" in tur:
                if tur.get("resumedAgentId") != child:
                    if tur.get("resumedAgentId") in CHILD_IDS or not isinstance(tur.get("resumedAgentId"), str):
                        raise Unknown("malformed")
                    continue          # it resumed someone else (by name): not this child
                if sum(t.use_count[sid] for t in issuers) != 1:
                    raise Unknown("conflict")
                rounds.append((parse_ts(ts), sid, issuer, line))
                continue
            message = tur.get("message")
            if not (isinstance(message, str) and message.startswith(QUEUED_PREFIX)):
                raise Unknown("malformed")
            # queued into the running round: no round
    if len(rounds) > 1:
        instants = [r[0] for r in rounds]
        if any(x is None for x in instants) or len(set(instants)) != len(instants):
            raise Unknown("ambiguous")
        if min(instants) != instants[0]:
            raise Unknown("ambiguous")
        rounds.sort(key=lambda r: r[0])
    by_id = {r[1]: r for r in rounds}
    for line, _task, tu, _status in notes:
        if tu not in by_id:
            raise Unknown("conflict")
        req_ts, _rid, issuer, req_line = by_id[tu]
        if issuer is main:
            if line < req_line:
                raise Unknown("ambiguous")   # rev 6 T4: before its request, launch included
        else:
            # another transcript: ordered by timestamp, an equal instant is not after the
            # request (GA-6) and neither is a notification older than the launch (T4)
            nts = parse_ts(main.note_ts.get(line))
            if nts is None or req_ts is None or nts <= req_ts:
                raise Unknown("ambiguous")
    latest_id = rounds[-1][1]
    values = set()
    for n in notes:
        if n[2] != latest_id:
            continue
        v = _norm(n[3])
        if v is None:
            raise Unknown("status_unrecognized")
        values.add(v)
    if latest_id == tid and sync_value is not None:
        values.add(sync_value)
    if not values:
        return "pending", "none"
    if len(values) > 1:
        raise Unknown("conflict")
    return values.pop(), "none"


def states(main_path: Path) -> dict:
    sub_dir = main_path.with_suffix("") / "subagents"
    children = sorted(p.name[len("agent-"):-len(".jsonl")] for p in sub_dir.glob("agent-*.jsonl")) if sub_dir.is_dir() else []
    if not children:
        return {}
    main = Transcript(main_path)
    transcripts = {"main": main}
    for child in children:
        transcripts[child] = Transcript(sub_dir / f"agent-{child}.jsonl")
    issuers = [t for t in transcripts.values() if t.ok]
    CHILD_IDS.clear()
    CHILD_IDS.update(children)
    if any(not t.ok for t in transcripts.values()):
        return {child: ("unknown", "parent_unreadable") for child in children}   # GA-1
    out = {}
    for child in children:
        try:
            meta_path = sub_dir / f"agent-{child}.meta.json"
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    raise Unknown("malformed")
                if not isinstance(meta, dict):
                    raise Unknown("malformed")
            depth = 1
            if "spawnDepth" in meta:
                raw = meta["spawnDepth"]
                # DESIGN 4.13.1 T0: an integer of 1 or more; a boolean or a string is not one
                if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
                    raise Unknown("malformed")
                depth = raw
            meta_use = None
            if "toolUseId" in meta:
                # DESIGN 4.13.1 T0: present and not a string is malformed
                if not isinstance(meta["toolUseId"], str):
                    raise Unknown("malformed")
                meta_use = meta["toolUseId"]
            if "parentAgentId" in meta:
                pid = meta["parentAgentId"]
                if not isinstance(pid, str) or not pid or pid == child:
                    raise Unknown("malformed")
                if pid not in children:
                    raise Unknown("parent_missing")
                launcher = transcripts[pid]
            else:
                if depth >= 2:
                    raise Unknown("parent_missing")
                launcher = main
            if not main.ok or not launcher.ok:
                raise Unknown("parent_unreadable")
            out[child] = decide(child, launcher, main, issuers, meta_use)
        except Unknown as exc:
            out[child] = ("unknown", exc.reason)
    return out


if __name__ == "__main__":
    root = Path(sys.argv[1])
    counts = collections.Counter()
    for main in root.glob("*/*.jsonl"):
        for state, reason in states(main).values():
            counts[(state, reason)] += 1
    for key, n in sorted(counts.items()):
        print(n, key)
