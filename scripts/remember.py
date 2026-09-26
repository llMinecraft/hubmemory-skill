#!/usr/bin/env python3
"""Validate and append an AI-confirmed long-term memory record."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
MEMORY_PATH = HUB_HOME / "memory" / "memories.jsonl"
LOCK_PATH = HUB_HOME / "memory" / ".lock"
VALID_TYPES = {"constraint", "preference", "architecture", "workflow", "lesson", "fact"}
VALID_STATUSES = {"active", "superseded", "stale", "archived"}


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def normalize_record(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    content = str(raw.get("content") or "").strip()
    memory_type = str(raw.get("type") or "fact").strip()
    if not title or not content:
        raise ValueError("title and content are required")
    if memory_type not in VALID_TYPES:
        raise ValueError(f"type must be one of: {', '.join(sorted(VALID_TYPES))}")
    status = str(raw.get("status") or "active").strip()
    if status not in VALID_STATUSES:
        raise ValueError(f"status must be one of: {', '.join(sorted(VALID_STATUSES))}")
    confidence = float(raw.get("confidence", 0.7))
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    now = timestamp()
    identity = hashlib.sha1(f"{title}\n{content}".encode("utf-8")).hexdigest()[:20]
    source_session_ids = sorted({str(x) for x in (raw.get("source_session_ids") or []) if x})
    source_event_ids = sorted({str(x) for x in (raw.get("source_event_ids") or []) if x})
    if not source_session_ids and not source_event_ids:
        raise ValueError("at least one source_session_ids or source_event_ids entry is required")
    return {
        "id": str(raw.get("id") or f"memory_{identity}"),
        "title": title,
        "content": content,
        "type": memory_type,
        "status": status,
        "project": str(raw.get("project") or ""),
        "confidence": confidence,
        "created_at": str(raw.get("created_at") or now),
        "updated_at": str(raw.get("updated_at") or now),
        "source_session_ids": source_session_ids,
        "source_event_ids": source_event_ids,
        "supersedes": sorted({str(x) for x in (raw.get("supersedes") or []) if x}),
        "tags": sorted({str(x) for x in (raw.get("tags") or []) if x}),
        "access_count": int(raw.get("access_count") or 0),
    }


def append_record(record: dict[str, Any]) -> tuple[bool, str]:
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.touch(exist_ok=True)
    with LOCK_PATH.open("r+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            existing: list[dict[str, Any]] = []
            if MEMORY_PATH.exists():
                for line in MEMORY_PATH.read_text(encoding="utf-8").splitlines():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        existing.append(value)
            for item in existing:
                if item.get("id") == record["id"]:
                    return False, str(record["id"])
            with MEMORY_PATH.open("a", encoding="utf-8") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
            return True, str(record["id"])
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("add",))
    parser.add_argument("--json", action="store_true", help="read one record from stdin")
    parser.add_argument("--title")
    parser.add_argument("--content")
    parser.add_argument("--type", default="fact")
    parser.add_argument("--project", default="")
    parser.add_argument("--confidence", type=float, default=0.7)
    parser.add_argument("--source-session", action="append", default=[])
    parser.add_argument("--source-event", action="append", default=[])
    parser.add_argument("--supersedes", action="append", default=[])
    parser.add_argument("--tag", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.json:
            raw = json.load(__import__("sys").stdin)
        else:
            raw = {
                "title": args.title,
                "content": args.content,
                "type": args.type,
                "project": args.project,
                "confidence": args.confidence,
                "source_session_ids": args.source_session,
                "source_event_ids": args.source_event,
                "supersedes": args.supersedes,
                "tags": args.tag,
            }
        record = normalize_record(raw)
        added, memory_id = append_record(record)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, "added": added, "id": memory_id}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
