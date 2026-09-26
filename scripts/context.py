#!/usr/bin/env python3
"""Print compact, task-relevant hubmemory context for an agent session."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from hybrid_query import search


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--cwd")
    parser.add_argument("--agent")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    results = search(HUB_HOME, args.query, days=args.days, limit=args.limit, cwd=args.cwd, agent=args.agent) if args.query else []
    pending = []
    request_dir = HUB_HOME / "reports" / "requests"
    for path in sorted(request_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("status") != "done":
            pending.append(item)
    payload = {"query": args.query, "results": results, "pending_reports": pending}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if pending:
        print("== Pending reports ==")
        for item in pending:
            print(f"- {item.get('kind')} {item.get('period')} [{item.get('status')}]" )
    if args.query:
        print(f"== Relevant memory ({len(results)}) ==")
        for index, item in enumerate(results, 1):
            title = item.get("title") or item.get("goals") or item.get("text") or "(untitled)"
            if isinstance(title, list):
                title = title[0] if title else "(untitled)"
            print(f"{index}. [{item.get('_source')}] {title}")
            print(f"   source={item.get('_path')} score={item.get('_score')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
