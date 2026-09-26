#!/usr/bin/env python3
"""Manage pending daily/monthly report jobs for whichever agent is active next."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
REPORTS_DIR = HUB_HOME / "reports"
REQUESTS_DIR = REPORTS_DIR / "requests"
LOCK_PATH = REPORTS_DIR / ".lock"
CLAIM_TIMEOUT = timedelta(hours=6)


def now() -> datetime:
    return datetime.now().astimezone()


def timestamp() -> str:
    return now().isoformat(timespec="seconds")


def request_path(kind: str, period: str) -> Path:
    return REQUESTS_DIR / f"{kind}-{period}.json"


def read_request(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def write_request(path: Path, request: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def locked():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+", encoding="utf-8")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    return handle


def ensure_request(kind: str, period: str, source: list[str] | None = None) -> dict:
    """Create a pending request unless this period was already completed."""
    path = request_path(kind, period)
    handle = locked()
    try:
        request = read_request(path)
        if request.get("status") == "done":
            return request
        if not request:
            request = {
                "id": f"{kind}:{period}",
                "kind": kind,
                "period": period,
                "status": "pending",
                "created_at": timestamp(),
                "source": source or [],
            }
        else:
            request["status"] = "pending"
            request["updated_at"] = timestamp()
            if source:
                request["source"] = source
        write_request(path, request)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return request


def all_requests() -> list[dict]:
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    requests = []
    for path in sorted(REQUESTS_DIR.glob("*.json")):
        request = read_request(path)
        if request:
            requests.append(request)
    return requests


def claim_request(kind: str, period: str, agent: str, model: str) -> dict:
    path = request_path(kind, period)
    handle = locked()
    try:
        request = read_request(path)
        if not request:
            return {"ok": False, "error": "request_not_found"}
        if request.get("status") == "done":
            return {"ok": False, "error": "already_done", "request": request}
        if request.get("status") == "claimed":
            try:
                claimed_at = datetime.fromisoformat(request["claimed_at"])
            except (KeyError, TypeError, ValueError):
                claimed_at = now() - CLAIM_TIMEOUT - timedelta(seconds=1)
            if now() - claimed_at < CLAIM_TIMEOUT and request.get("claimed_by") != agent:
                return {"ok": False, "error": "already_claimed", "request": request}
        request.update({
            "status": "claimed",
            "claimed_at": timestamp(),
            "claimed_by": agent,
            "claimed_model": model,
        })
        write_request(path, request)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return {"ok": True, "request": request}


def complete_request(kind: str, period: str, agent: str, model: str) -> dict:
    path = request_path(kind, period)
    output_path = (
        HUB_HOME / "daily" / period / "diary.md"
        if kind == "daily"
        else HUB_HOME / "monthly" / f"{period}.md"
    )
    handle = locked()
    try:
        request = read_request(path)
        if not request:
            return {"ok": False, "error": "request_not_found"}
        try:
            output = output_path.read_text(encoding="utf-8")
        except OSError:
            return {"ok": False, "error": "report_output_not_found", "path": str(output_path)}
        draft_markers = ("内置规则", "规则模板", "LLM 不可用", "LLM disabled")
        if any(marker in output.splitlines()[0] if output.splitlines() else "" for marker in draft_markers):
            return {"ok": False, "error": "report_is_still_draft", "path": str(output_path)}
        request.update({
            "status": "done",
            "completed_at": timestamp(),
            "completed_by": agent,
            "completed_model": model,
        })
        write_request(path, request)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return {"ok": True, "request": request}


def reopen_request(kind: str, period: str, reason: str) -> dict:
    path = request_path(kind, period)
    handle = locked()
    try:
        request = read_request(path)
        if not request:
            return {"ok": False, "error": "request_not_found"}
        request.update({"status": "pending", "reopened_at": timestamp(), "reopen_reason": reason})
        write_request(path, request)
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    return {"ok": True, "request": request}


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list")
    list_parser.add_argument("--json", action="store_true")
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--kind", choices=("daily", "monthly"), required=True)
    enqueue.add_argument("--period", required=True)
    enqueue.add_argument("--source", action="append", default=[])
    for name in ("claim", "complete", "reopen"):
        command = sub.add_parser(name)
        command.add_argument("--kind", choices=("daily", "monthly"), required=True)
        command.add_argument("--period", required=True)
        command.add_argument("--agent", default="unknown")
        command.add_argument("--model", default="unknown")
        command.add_argument("--reason", default="manual retry")
    args = parser.parse_args()

    if args.command == "list":
        requests = [r for r in all_requests() if r.get("status") != "done"]
        if args.json:
            print(json.dumps(requests, ensure_ascii=False, indent=2))
        else:
            for request in requests:
                print(f"{request.get('status', '?')} {request.get('kind')} {request.get('period')}")
        return 0
    if args.command == "enqueue":
        result = ensure_request(args.kind, args.period, args.source)
    elif args.command == "claim":
        result = claim_request(args.kind, args.period, args.agent, args.model)
    elif args.command == "complete":
        result = complete_request(args.kind, args.period, args.agent, args.model)
    else:
        result = reopen_request(args.kind, args.period, args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
