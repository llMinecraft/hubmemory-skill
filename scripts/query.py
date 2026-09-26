#!/usr/bin/env python3
"""hubmemory query — 在长期记忆、证据、报告和原始事件中做混合检索。

用法：
    python3 query.py <关键词> [--days N] [--limit K]

默认在最近 7 天内检索，最多返回 20 条命中。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from hybrid_query import search


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
DAILY_DIR = HUB_HOME / "daily"
PROFILE_DIR = HUB_HOME / "profile"


def iter_daily_dirs(days: int):
    today = datetime.now().date()
    for i in range(days):
        d = today - timedelta(days=i)
        target = DAILY_DIR / d.strftime("%Y-%m-%d")
        if target.exists():
            yield target


def format_hit(item: dict) -> str:
    source = item.get("_source", "?")
    score = item.get("_score", 0)
    period = item.get("period") or (item.get("ts") or "?")[:10]
    agent = item.get("agent", "?")
    sid = (item.get("session_id") or "")[:8]
    title = item.get("title")
    if not title:
        for field in ("goals", "actions", "results", "text", "content", "narrative"):
            value = item.get(field)
            if isinstance(value, list) and value:
                title = value[0]
                break
            if isinstance(value, str) and value.strip():
                title = value
                break
    title = " ".join(str(title or "(untitled)").split())
    return f"[{period}] [{source}] score={score} {agent} sid={sid}\n  {title[:320]}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("keyword")
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--cwd")
    parser.add_argument("--agent")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    kw = args.keyword.strip()
    if not kw:
        print("empty keyword", file=sys.stderr)
        return 2

    hits = search(HUB_HOME, kw, days=max(1, args.days), limit=max(1, args.limit), cwd=args.cwd, agent=args.agent)
    if args.json:
        print(json.dumps(hits, ensure_ascii=False, indent=2))
    else:
        print(f"Searching last {args.days} days for: {kw!r}\n")
        if hits:
            print(f"== Hybrid results ({len(hits)} hits) ==")
            for hit in hits:
                print(format_hit(hit))
                print()
        else:
            print("(no memory/evidence/session hits)")
    return 0 if hits else 1


if __name__ == "__main__":
    sys.exit(main())
