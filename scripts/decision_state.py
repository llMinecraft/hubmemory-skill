#!/usr/bin/env python3
"""Append-only decision log for hubmemory long-term memories.

`memory/decision_events.jsonl` records explicit verdicts — accepted /
rejected / superseded — about memory records written by remember.py into
`memory/memories.jsonl`. Design rules:

  * append-only — events are never edited or deleted; reversal is a new
    event (optionally linked through `reverses`);
  * explicit    — nothing is inferred: every event needs a non-empty
    reason and >= 1 provenance id (source_session_ids/source_event_ids,
    stored as supplied, existence not verified);
  * non-mutating— memories.jsonl is read, never rewritten; rejected and
    superseded entries stay in the corpus marked recommended=False with
    a caution instead of disappearing;
  * historical  — `as_of` cutoffs are EXCLUSIVE for events (ts < cutoff)
    and for structural supersedes (replacement created_at < cutoff);
  * fail closed — a malformed log line raises ValueError in load_states /
    annotate_documents so a broken revocation can never silently hide.

Public API (used by the hybrid_query/context integration):

  mark_decision(hub_home, memory_id, *, decision, reason, ...) -> event
  read_events(hub_home) -> (events, issues)   tolerant primitive
  iter_events(hub_home, as_of=None) -> events  strict: raises on issues
  load_states(hub_home, as_of=None) -> {memory_id: full state w/ history}
  resolve_state(hub_home, memory_id, as_of=None) -> one state
  annotate_documents(docs, hub_home, as_of=None) -> NEW list; every doc
      gets a COMPACT doc["decision"]: state/decision, recommended,
      rank_hint ("demote" => rank below viable hits; accepted stays
      "neutral", no boost), caution, reason, effective_at, event_id,
      replaced_by/replaces/cycle, source_session_ids/source_event_ids.
      Rejected/superseded/cyclic/ambiguous docs are RETAINED, not
      dropped. Memory docs get the resolved state; docs whose exact
      session_id+event_id match a mark's `target` get that mark; all
      others get {"state": "unmarked", "recommended": True}.

CLI (wired as `hub decision mark|list` by the lead):

  mark --memory ID --decision accepted|rejected|superseded --reason TEXT
      [--source-session S]* [--source-event E]* [--replacement M]*
      [--session S --event E] [--reverses dec_...] [--ts ISO] [--agent A]
  list [--as-of ISO] [--memory ID] [--events] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Iterable

import lockutil

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
VALID_DECISIONS = {"accepted", "rejected", "superseded"}
INACTIVE_RECORD_STATUSES = {"superseded", "stale", "archived"}
BARE_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _coerce_datetime(value: Any) -> datetime | None:
    """Parse an instant; naive values are assumed to be local time."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, str):
        text = value.strip()
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.combine(date.fromisoformat(text[:10]), time.min)
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _cutoff(as_of: Any) -> datetime | None:
    """Exclusive cutoff; a bare date means end of that day (hybrid_query rule)."""
    if as_of is None:
        return None
    dt = _coerce_datetime(as_of)
    if dt is None:
        raise ValueError("invalid as_of timestamp: %r" % as_of)
    if isinstance(as_of, str) and BARE_DATE_RE.fullmatch(as_of.strip()):
        dt = datetime.combine(dt.date(), time.max, tzinfo=dt.tzinfo)
    return dt


def _events_path(hub_home: Path) -> Path:
    return Path(hub_home) / "memory" / "decision_events.jsonl"


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def read_memories(hub_home: Path) -> dict[str, Any]:
    """remember.py records, non-mutating. Duplicated ids are kept for
    ambiguity reporting; the last appended record wins. `issues` lists
    malformed lines — resolution callers fail closed on them because a
    skipped line could hide a supersedes edge."""
    records: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    duplicates: set[str] = set()
    issues: list[str] = []
    path = Path(hub_home) / "memory" / "memories.jsonl"
    if path.exists():
        for line_no, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                issues.append("line %d: invalid JSON" % line_no)
                continue
            if not isinstance(record, dict):
                issues.append("line %d: not a record object" % line_no)
                continue
            records.append(record)
            mid = record.get("id")
            if not isinstance(mid, str) or not mid:
                continue
            if mid in by_id:
                duplicates.add(mid)
            by_id[mid] = record
    return {"records": records, "by_id": by_id, "duplicates": duplicates,
            "issues": issues}


def _memory_issues(mem: dict[str, Any]) -> None:
    if mem.get("issues"):
        raise ValueError("malformed memories.jsonl: %s"
                         % "; ".join(mem["issues"]))


def _str_list(value: Any) -> bool:
    return (isinstance(value, list)
            and all(isinstance(v, str) and v for v in value))


def _event_valid(raw: Any) -> bool:
    """Strict schema gate for replaying the log. A line that parses but
    lacks required shape (reason, >=1 provenance id, valid superseded
    replacements, well-formed exact target) counts as malformed so a
    broken revocation fails closed instead of silently replaying."""
    if not isinstance(raw, dict):
        return False
    if not isinstance(raw.get("id"), str) or not raw["id"]:
        return False
    if not isinstance(raw.get("memory_id"), str) or not raw["memory_id"]:
        return False
    decision = raw.get("decision")
    if decision not in VALID_DECISIONS:
        return False
    if _coerce_datetime(raw.get("ts")) is None:
        return False
    if not isinstance(raw.get("reason"), str) or not raw["reason"].strip():
        return False
    ssids = raw.get("source_session_ids")
    seids = raw.get("source_event_ids")
    if ssids is not None and not _str_list(ssids):
        return False
    if seids is not None and not _str_list(seids):
        return False
    if not (ssids or seids):  # at least one provenance id required
        return False
    repls = raw.get("replacement_ids")
    if repls is not None and not _str_list(repls):
        return False  # a bare string would split into characters
    if decision == "superseded":
        if not repls or raw["memory_id"] in repls:
            return False
    elif repls:
        return False  # replacements only belong to superseded
    target = raw.get("target")
    if target is not None:
        if not (isinstance(target, dict)
                and isinstance(target.get("session_id"), str)
                and isinstance(target.get("event_id"), str)):
            return False
    if raw.get("reverses") is not None and not isinstance(raw["reverses"], str):
        return False
    return True


def read_events(hub_home: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """(valid events, issues). Tolerant primitive for tooling; resolution
    paths use iter_events/load_states which fail closed instead."""
    events: list[dict[str, Any]] = []
    issues: list[str] = []
    path = _events_path(hub_home)
    if not path.exists():
        return events, issues
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            issues.append("line %d: invalid JSON" % line_no)
            continue
        if not _event_valid(raw):
            issues.append("line %d: invalid decision event" % line_no)
            continue
        raw["_line"] = line_no
        events.append(raw)
    return events, issues


def iter_events(hub_home: Path, as_of: Any = None) -> list[dict[str, Any]]:
    """Valid events with ts < cutoff. STRICT: any malformed line raises
    ValueError — a broken revocation must not be silently skipped."""
    events, issues = read_events(hub_home)
    if issues:
        raise ValueError("malformed decision_events.jsonl: %s" % "; ".join(issues))
    cutoff = _cutoff(as_of)
    if cutoff is None:
        return events
    return [e for e in events if _coerce_datetime(e["ts"]) < cutoff]


# ------------------------------------------------------------ supersede graph


def _structural_edges(mem: dict[str, Any], cutoff: datetime | None
                      ) -> dict[str, set[str]]:
    """replaced -> {replacements} from remember.py `supersedes`. A
    replacement only counts in a historical view when its created_at
    predates the cutoff (exclusive)."""
    edges: dict[str, set[str]] = defaultdict(set)
    for record in mem["records"]:
        rid = record.get("id")
        if not isinstance(rid, str) or not rid:
            continue
        if cutoff is not None:
            created = _coerce_datetime(record.get("created_at"))
            if created is None or created >= cutoff:
                continue
        for old in record.get("supersedes") or []:
            edges[str(old)].add(rid)
    return edges


def _decision_edges(latest: dict[str, dict[str, Any]], mem: dict[str, Any],
                    cutoff: datetime | None) -> dict[str, set[str]]:
    edges: dict[str, set[str]] = defaultdict(set)
    for mid, event in latest.items():
        if event["decision"] != "superseded":
            continue
        for repl in event.get("replacement_ids") or []:
            repl_rec = mem["by_id"].get(repl)
            if cutoff is not None and repl_rec is not None:
                created = _coerce_datetime(repl_rec.get("created_at"))
                if created is not None and created >= cutoff:
                    continue
            edges[mid].add(repl)
    return edges


def _reachable(edges: dict[str, set[str]], start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(edges.get(node, ()))
    return seen


def _on_cycle(edges: dict[str, set[str]], node: str) -> bool:
    """True when `node` can reach itself via replaced->replacement edges."""
    seen: set[str] = set()
    stack = list(edges.get(node, ()))
    while stack:
        current = stack.pop()
        if current == node:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(edges.get(current, ()))
    return False


def _latest_events(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Latest applicable event per memory: highest (ts, file line)."""
    latest: dict[str, dict[str, Any]] = {}
    for event in events:
        key = (_coerce_datetime(event["ts"]), event.get("_line") or 0)
        current = latest.get(event["memory_id"])
        if current is None or key >= (
                _coerce_datetime(current["ts"]), current.get("_line") or 0):
            latest[event["memory_id"]] = event
    return latest


def _merged_edges(mem: dict[str, Any], latest: dict[str, dict[str, Any]],
                  cutoff: datetime | None) -> dict[str, set[str]]:
    edges = _structural_edges(mem, cutoff)
    for old, new in _decision_edges(latest, mem, cutoff).items():
        edges[old] |= new
    return edges


# -------------------------------------------------------------------- marking


def mark_decision(hub_home: Path, memory_id: str, *, decision: str,
                  reason: str,
                  source_session_ids: Iterable[str] = (),
                  source_event_ids: Iterable[str] = (),
                  replacement_ids: Iterable[str] = (),
                  session_id: str | None = None,
                  event_id: str | None = None,
                  reverses: str | None = None,
                  ts: Any = None,
                  agent: str | None = None,
                  actor: str | None = None) -> dict[str, Any]:
    """Validate and append one decision event; returns the stored event.
    Raises ValueError on any contract violation; nothing is written then.
    memories.jsonl is read for validation only, never edited."""
    hub_home = Path(hub_home)
    mem = read_memories(hub_home)
    _memory_issues(mem)  # fail closed: a hidden line could hide supersedes

    mid = str(memory_id or "").strip()
    if not mid:
        raise ValueError("memory_id is required")
    if mid in mem["duplicates"]:
        raise ValueError("ambiguous target: memory id %s is duplicated" % mid)
    if mid not in mem["by_id"]:
        raise ValueError("unknown memory target: %s" % mid)

    decision = str(decision or "").strip()
    if decision not in VALID_DECISIONS:
        raise ValueError("decision must be one of: %s"
                         % ", ".join(sorted(VALID_DECISIONS)))
    reason = str(reason or "").strip()
    if not reason:
        raise ValueError("reason is required")
    ssids = sorted({str(s) for s in (source_session_ids or []) if s})
    seids = sorted({str(s) for s in (source_event_ids or []) if s})
    if not ssids and not seids:
        raise ValueError(
            "at least one source_session_ids or source_event_ids entry is required")

    repls = sorted({str(r) for r in (replacement_ids or []) if r})
    if decision == "superseded" and not repls:
        raise ValueError("superseded requires at least one replacement id")
    if decision != "superseded" and repls:
        raise ValueError("replacement_ids only apply to a superseded decision")
    for repl in repls:
        if repl == mid:
            raise ValueError("self-supersession rejected: %s" % mid)
        if repl in mem["duplicates"]:
            raise ValueError("ambiguous replacement: id %s is duplicated" % repl)
        if repl not in mem["by_id"]:
            raise ValueError("unknown replacement memory: %s" % repl)

    if (session_id is None) != (event_id is None):
        raise ValueError("session_id and event_id must be given together")

    if ts is None:
        ts_str = _timestamp()
    else:
        dt = _coerce_datetime(ts)
        if dt is None:
            raise ValueError("invalid ts: %r" % ts)
        ts_str = dt.isoformat(timespec="seconds")

    if decision == "superseded":
        events, issues = read_events(hub_home)
        if issues:
            raise ValueError(
                "malformed decision_events.jsonl: %s" % "; ".join(issues))
        edges = _merged_edges(mem, _latest_events(events), None)
        for repl in repls:
            if mid in _reachable(edges, repl):
                raise ValueError(
                    "supersede cycle rejected: %s -> %s would close a loop"
                    % (mid, repl))

    event: dict[str, Any] = {
        "memory_id": mid, "decision": decision, "reason": reason,
        "source_session_ids": ssids, "source_event_ids": seids, "ts": ts_str,
    }
    if repls:
        event["replacement_ids"] = repls
    if reverses:
        event["reverses"] = str(reverses)
    if session_id is not None:
        # Exact anchor stored as supplied; verification is a later,
        # coordinated step — disclosed via target_verification.
        event["target"] = {"session_id": str(session_id),
                           "event_id": str(event_id)}
        event["target_verification"] = "unverified"
    if agent:
        event["agent"] = str(agent)
    if actor:
        event["actor"] = str(actor)

    lock_path = Path(hub_home) / "memory" / ".lock"  # shared with remember.py
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    with lockutil.file_lock(lock_path):
        stored, _issues = read_events(hub_home)
        known_ids = {e.get("id") for e in stored}
        if reverses is not None and str(reverses) not in known_ids:
            raise ValueError("unknown reverses event id: %s" % reverses)
        digest = hashlib.sha1(json.dumps(
            event, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        new_id = "dec_" + digest
        counter = 1
        while new_id in known_ids:
            counter += 1
            new_id = "dec_%s_%d" % (digest, counter)
        event = {"id": new_id, **event}
        with _events_path(hub_home).open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()
            os.fsync(output.fileno())
    return event


# ----------------------------------------------------------------- resolution


def load_states(hub_home: Path, as_of: Any = None
                ) -> dict[str, dict[str, Any]]:
    """Effective state per memory id: latest applicable event wins
    (accepted/rejected/superseded are reversible), merged with the
    structural supersedes graph. Full version — `history` included —
    used by `list`; annotate_documents emits the compact projection."""
    hub_home = Path(hub_home)
    cutoff = _cutoff(as_of)
    mem = read_memories(hub_home)
    _memory_issues(mem)  # fail closed: hidden line could hide supersedes
    events = iter_events(hub_home, as_of)  # strict: raises on malformed log
    by_mem: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for event in events:
        by_mem[event["memory_id"]].append(event)
    latest = _latest_events(events)
    edges = _merged_edges(mem, latest, cutoff)
    replaces: dict[str, set[str]] = defaultdict(set)
    for old, new in edges.items():
        for repl in new:
            replaces[repl].add(old)

    states: dict[str, dict[str, Any]] = {}
    for mid in sorted(set(mem["by_id"]) | set(by_mem)):
        record = mem["by_id"].get(mid)
        record_status = record.get("status") if record else None
        history = by_mem.get(mid, [])
        last = latest.get(mid)
        decision = last["decision"] if last else None
        replaced_by = sorted(edges.get(mid, ()))
        cycle = _on_cycle(edges, mid)
        ambiguous = mid in mem["duplicates"]
        missing = record is None
        conflict = decision == "accepted" and bool(replaced_by)

        if decision:
            state = decision
        elif replaced_by or record_status == "superseded":
            state = "superseded"
        else:
            state = "unmarked"

        if missing:
            caution = "unknown memory id"
        elif ambiguous:
            caution = "ambiguous: duplicated memory id"
        elif cycle:
            caution = "cycle in supersede graph"
        elif conflict:
            caution = "conflict: accepted but supersede edges still exist"
        elif state == "rejected":
            caution = "historical: rejected — retained as caution, not recommended"
        elif state == "superseded" and replaced_by:
            caution = "superseded: replaced by %s" % ", ".join(replaced_by)
        elif state == "superseded":
            caution = "superseded"
        elif record_status in INACTIVE_RECORD_STATUSES:
            caution = "legacy record status: %s" % record_status
        else:
            caution = None

        recommended = not (
            state in {"rejected", "superseded"} or cycle or ambiguous
            or missing or conflict
            or record_status in {"stale", "archived"})
        states[mid] = {
            "memory_id": mid,
            "state": state,
            "decision": decision,
            "recommended": recommended,
            "rank_hint": "neutral" if recommended else "demote",
            "caution": caution,
            "reason": last.get("reason") if last else None,
            "effective_at": last.get("ts") if last else None,
            "event_id": last.get("id") if last else None,
            "source_session_ids": last.get("source_session_ids", []) if last else [],
            "source_event_ids": last.get("source_event_ids", []) if last else [],
            "replaced_by": replaced_by,
            "replaces": sorted(replaces.get(mid, ())),
            "cycle": cycle,
            "ambiguous": ambiguous,
            "missing_record": missing,
            "conflict": conflict,
            "record_status": record_status,
            "history": history,
        }
    return states


def resolve_state(hub_home: Path, memory_id: str,
                  as_of: Any = None) -> dict[str, Any] | None:
    return load_states(hub_home, as_of).get(str(memory_id))


# ------------------------------------------------------------------ annotate

_COMPACT_KEYS = (
    "memory_id", "state", "decision", "recommended", "rank_hint", "caution",
    "reason", "effective_at", "event_id", "source_session_ids",
    "source_event_ids", "replaced_by", "replaces", "cycle", "record_status",
)


def _compact(state: dict[str, Any]) -> dict[str, Any]:
    """Whitelist projection — never inflate every hit with full history."""
    return {k: state[k] for k in _COMPACT_KEYS if k in state}


def _unmarked() -> dict[str, Any]:
    return {"state": "unmarked", "decision": None, "recommended": True,
            "rank_hint": "neutral", "caution": None, "replaced_by": [],
            "replaces": [], "cycle": False}


def _target_mark(marks: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact annotation for a doc matching a mark's exact session+event."""
    last = max(marks, key=lambda e: (_coerce_datetime(e["ts"]),
                                     e.get("_line") or 0))
    state = last["decision"]
    recommended = state not in {"rejected", "superseded"}
    return {
        "state": state, "decision": state, "recommended": recommended,
        "rank_hint": "neutral" if recommended else "demote",
        "caution": None if recommended else
        "historical: %s — retained as caution, not recommended" % state,
        "memory_id": last["memory_id"], "reason": last.get("reason"),
        "effective_at": last.get("ts"), "event_id": last.get("id"),
        "source_session_ids": last.get("source_session_ids", []),
        "source_event_ids": last.get("source_event_ids", []),
        "replaced_by": list(last.get("replacement_ids") or []),
        "replaces": [], "cycle": False,
    }


def annotate_documents(docs: Iterable[dict[str, Any]], hub_home: Path,
                       as_of: Any = None) -> list[dict[str, Any]]:
    """Attach doc["decision"] where a decision applies; docs without one
    pass through UNCHANGED objects (absent key == unmarked/recommended).
    Only memory docs and exact session+event targets are annotated, so a
    large corpus costs one load_states plus shallow copies for the few
    changed docs. Fail closed: malformed log raises ValueError."""
    hub_home = Path(hub_home)
    states = load_states(hub_home, as_of)
    target_map: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for event in iter_events(hub_home, as_of):
        target = event.get("target")
        if isinstance(target, dict) and target.get("session_id") and target.get("event_id"):
            target_map[(str(target["session_id"]),
                        str(target["event_id"]))].append(event)
    out: list[dict[str, Any]] = []
    for doc in docs or []:
        record = doc.get("record") if isinstance(doc.get("record"), dict) else doc
        mid = record.get("id") or record.get("memory_id")
        decision = None
        if isinstance(mid, str) and mid in states:
            decision = _compact(states[mid])
        elif isinstance(mid, str) and mid.startswith("memory_"):
            decision = dict(_unmarked(), state="unknown", recommended=False,
                            rank_hint="demote", caution="unknown memory id",
                            memory_id=mid)
        else:
            sid, eid = record.get("session_id"), record.get("event_id")
            marks = target_map.get((str(sid), str(eid))) if sid and eid else None
            if marks:
                decision = _target_mark(marks)
        if decision is not None:
            new_doc = dict(doc)  # shallow copy only for annotated docs
            new_doc["decision"] = decision
            out.append(new_doc)
        else:
            out.append(doc)
    return out


# ------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision_state", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    mark = sub.add_parser("mark")
    mark.add_argument("--memory", required=True)
    mark.add_argument("--decision", required=True, choices=sorted(VALID_DECISIONS))
    mark.add_argument("--reason", required=True)
    mark.add_argument("--source-session", action="append", default=[])
    mark.add_argument("--source-event", action="append", default=[])
    mark.add_argument("--replacement", action="append", default=[])
    mark.add_argument("--session", help="exact session anchor (with --event)")
    mark.add_argument("--event", help="exact event anchor (with --session)")
    mark.add_argument("--reverses", help="dec_... id of the event being reversed")
    mark.add_argument("--ts", help="ISO timestamp; default now")
    mark.add_argument("--agent")
    mark.add_argument("--actor")
    mark.add_argument("--hub-home", default=str(HUB_HOME))
    lst = sub.add_parser("list")
    lst.add_argument("--as-of", help="exclusive historical cutoff")
    lst.add_argument("--memory")
    lst.add_argument("--events", action="store_true",
                     help="raw applicable events instead of resolved states")
    lst.add_argument("--json", action="store_true")
    lst.add_argument("--hub-home", default=str(HUB_HOME))
    args = parser.parse_args(argv)
    try:
        if args.command == "mark":
            event = mark_decision(
                Path(args.hub_home), args.memory,
                decision=args.decision, reason=args.reason,
                source_session_ids=args.source_session,
                source_event_ids=args.source_event,
                replacement_ids=args.replacement,
                session_id=args.session, event_id=args.event,
                reverses=args.reverses, ts=args.ts,
                agent=args.agent, actor=args.actor)
            print(json.dumps({"ok": True, "event": event}, ensure_ascii=False))
            return 0
        if args.events:
            events = iter_events(Path(args.hub_home), args.as_of)
            if args.memory:
                events = [e for e in events
                          if e.get("memory_id") == args.memory]
            if args.json:
                print(json.dumps(events, ensure_ascii=False, indent=2))
            else:
                for e in events:
                    print("%s  %s  %-10s  %s" % (
                        e.get("id", "-"), e.get("ts", "-"),
                        e.get("decision", "-"), e.get("memory_id", "-")))
            return 0
        states = load_states(Path(args.hub_home), args.as_of)
        if args.memory:
            states = ({args.memory: states[args.memory]}
                      if args.memory in states else {})
        if args.json:
            print(json.dumps(states, ensure_ascii=False, indent=2))
            return 0
        for mid, st in states.items():
            parts = ["%s  state=%s" % (mid, st["state"])]
            if st["replaced_by"]:
                parts.append("replaced_by=%s" % ",".join(st["replaced_by"]))
            if st["cycle"]:
                parts.append("cycle")
            if st["effective_at"]:
                parts.append("ts=%s" % st["effective_at"])
            if st["caution"]:
                parts.append("caution=%s" % st["caution"])
            print("  ".join(parts))
        return 0
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
