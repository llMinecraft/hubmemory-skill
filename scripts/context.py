#!/usr/bin/env python3
"""hubmemory context — compact, token-budgeted, task-relevant memory.

Design: return CONCLUSIONS + pointers, not payloads. Each hit is one line
(title/conclusion + confidence + ref). The agent reads the ref only when a
hit looks insufficient. Rough token estimate = utf-8 bytes / 4.

    python3 context.py "日报" --budget 800     # ~800 token cap
    python3 context.py "日报" --json --budget 800
    python3 context.py                        # no query -> routing hints only
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from hybrid_query import search

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))

RECENT_HINT_RE = re.compile(
    r"昨天|前天|最近|刚才|上次|之前|今天|这周|上周|yesterday|recent|earlier|last week|today",
    re.I,
)


def estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 4)


def _pick_line(record: dict) -> str:
    """Best single-line conclusion/title for a hit."""
    for field in ("conclusion", "title", "text", "content", "narrative"):
        v = record.get(field)
        if isinstance(v, str) and v.strip():
            return " ".join(v.split())
    for field in ("results", "goals", "actions", "next_steps"):
        v = record.get(field)
        if isinstance(v, list) and v:
            return " ".join(str(v[0]).split())
    return "(no summary field)"


def _confidence(record: dict) -> str:
    c = record.get("confidence")
    if isinstance(c, (int, float)):
        return "high" if c >= 0.7 else ("med" if c >= 0.5 else "low")
    return "?"


def compact_hit(record: dict) -> dict:
    return {
        "kind": record.get("_source"),
        "period": record.get("period") or (record.get("ts") or "")[:10],
        "agent": record.get("agent"),
        "confidence": _confidence(record),
        "line": _pick_line(record)[:240],
        "score": record.get("_score"),
        "ref": record.get("_path"),
        "session": (record.get("session_id") or "")[:8],
    }


def routing_hint(query: str) -> str | None:
    if RECENT_HINT_RE.search(query or ""):
        return (
            "recent-work question: read ~/hubmemory/MANIFEST.md 'Last 7 days' "
            "first (one line/day + ref); open the ref diary only if needed"
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--cwd")
    parser.add_argument("--agent")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--budget", type=int, default=1200,
                        help="max ~tokens of output; hits beyond budget are dropped")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    pending = []
    request_dir = HUB_HOME / "reports" / "requests"
    for path in sorted(request_dir.glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("status") != "done":
            pending.append(item)

    hint = routing_hint(args.query)
    raw = (
        search(HUB_HOME, args.query, days=args.days, limit=args.limit,
               cwd=args.cwd, agent=args.agent)
        if args.query else []
    )
    hits = [compact_hit(r) for r in raw]

    # budget: keep header + hits until token cap
    budget_chars = max(200, args.budget * 4)
    lines_out = []
    used = estimate_tokens("header")
    shown, dropped = [], 0
    for hit in hits:
        line = "[%s|%s|conf=%s] %s -> %s" % (
            hit["kind"], hit["period"] or "-", hit["confidence"],
            hit["line"], hit["ref"],
        )
        cost = estimate_tokens(line)
        if used + cost > args.budget and shown:
            dropped += 1
            continue
        used += cost
        shown.append(hit)

    payload = {
        "query": args.query,
        "hint": hint,
        "budget_tokens": args.budget,
        "returned_tokens_est": used,
        "results": shown,
        "dropped_by_budget": dropped,
        "sufficiency": (
            "sufficient" if shown and shown[0]["score"] and shown[0]["score"] >= 1.0
            else "thin — widen --days or read MANIFEST.md / the ref diaries"
        ),
        "pending_reports": [
            {"kind": p.get("kind"), "period": p.get("period"), "status": p.get("status")}
            for p in pending
        ],
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if hint:
        print("hint: %s" % hint)
    if payload["pending_reports"]:
        print("pending reports: " + ", ".join(
            "%s %s[%s]" % (p["kind"], p["period"], p["status"])
            for p in payload["pending_reports"]
        ))
    if args.query:
        print("== relevant memory (%d shown, %d dropped by budget, ~%d tok) ==" % (
            len(shown), dropped, used,
        ))
        for hit in shown:
            print("- [%s|%s|conf=%s] %s" % (hit["kind"], hit["period"] or "-", hit["confidence"], hit["line"]))
            print("    ref: %s" % hit["ref"])
        if dropped:
            print("  …+%d more; raise --budget to see them" % dropped)
    else:
        print("(no query — pass keywords to search memory; MANIFEST.md has the 7-day one-line index)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

