#!/usr/bin/env python3
"""hubmemory recent — 汇总最近一段时间的会话活动，供 AI 或用户回答"最近干了什么"。

用法：
    python3 recent.py                 # 默认最近 24 小时
    python3 recent.py --hours 6       # 最近 6 小时
    python3 recent.py --days 3        # 最近 3 天
    python3 recent.py --cwd /path     # 只看某工作目录
    python3 recent.py --topN 3        # 每个 group 展示前 3 条用户提问

输出：
- 按 cwd 聚合的活动摘要（sessions、tokens、agent 占比、工具统计、代表性用户提问）
- 若时间范围覆盖了某一整天且已生成 diary.md，附带 diary 内容
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
DAILY_DIR = HUB_HOME / "daily"


def parse_ts(s: str) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone()
    except (ValueError, TypeError):
        return None


def load_events_in_range(since: datetime, until: datetime) -> list[dict]:
    """加载 [since, until] 内所有事件，跨多个 daily/YYYY-MM-DD/ 目录。"""
    events = []
    cur = since.date()
    end = until.date()
    while cur <= end:
        path = DAILY_DIR / cur.strftime("%Y-%m-%d") / "sessions.jsonl"
        cur = cur + timedelta(days=1)
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_ts(evt.get("ts"))
                if ts and since <= ts <= until:
                    events.append(evt)
    return events


def summarize(events: list[dict], top_n_user_msgs: int = 3) -> dict:
    """按 cwd 分组汇总。"""
    by_cwd: dict[str, dict] = defaultdict(lambda: {
        "sessions": set(),
        "agents": Counter(),          # agent+model -> tokens_out
        "agent_msgs": Counter(),      # agent -> msg count
        "tools": Counter(),           # tool name -> count
        "user_msgs": [],              # (ts, text)
        "first_ts": None,
        "last_ts": None,
    })
    for evt in events:
        cwd = evt.get("cwd") or "(unknown cwd)"
        g = by_cwd[cwd]
        g["sessions"].add(evt.get("session_id") or "?")
        agent = evt.get("agent") or "?"
        model = evt.get("model") or "?"
        key = f"{agent}:{model}"
        g["agents"][key] += int(evt.get("tokens_out") or 0)
        g["agent_msgs"][agent] += 1
        for tu in evt.get("tool_uses") or []:
            g["tools"][tu.get("name") or "?"] += 1
        if evt.get("role") == "user":
            txt = (evt.get("text") or "").strip()
            if txt and not txt.startswith("[tool_result]") and not txt.startswith("[tool_output]"):
                g["user_msgs"].append((evt.get("ts"), txt))
        ts = parse_ts(evt.get("ts"))
        if ts:
            if g["first_ts"] is None or ts < g["first_ts"]:
                g["first_ts"] = ts
            if g["last_ts"] is None or ts > g["last_ts"]:
                g["last_ts"] = ts

    for cwd, g in by_cwd.items():
        g["sessions"] = sorted(g["sessions"])
        g["user_msgs"] = g["user_msgs"][:top_n_user_msgs] if False else g["user_msgs"]
    return by_cwd


def format_report(by_cwd: dict, since: datetime, until: datetime, top_n_user_msgs: int) -> str:
    lines = [
        f"== 最近活动（{since.strftime('%Y-%m-%d %H:%M')} 至 {until.strftime('%Y-%m-%d %H:%M')}）==",
        "",
    ]
    if not by_cwd:
        lines.append("(该时段无会话记录)")
        return "\n".join(lines)

    # 按 last_ts 倒序展示（最活跃的在最前）
    ordered = sorted(by_cwd.items(), key=lambda kv: kv[1]["last_ts"] or datetime.min.replace(tzinfo=since.tzinfo), reverse=True)

    for cwd, g in ordered:
        total_tokens = sum(g["agents"].values())
        n_sess = len(g["sessions"])
        first = g["first_ts"].strftime("%m-%d %H:%M") if g["first_ts"] else "?"
        last = g["last_ts"].strftime("%m-%d %H:%M") if g["last_ts"] else "?"
        lines.append(f"### {cwd}")
        lines.append(f"- 时段: {first} → {last}, {n_sess} 个 session")

        # Agent 占比
        if total_tokens > 0:
            agent_lines = []
            for key, tokens in g["agents"].most_common():
                pct = round(tokens / total_tokens * 100, 1)
                agent_lines.append(f"{key}: {tokens:,} tokens ({pct}%)")
            lines.append("- Agent 占比: " + " / ".join(agent_lines))
        else:
            lines.append("- Agent 占比: 无 assistant token 输出")

        lines.append(f"- 消息数: " + ", ".join(f"{a}={c}" for a, c in g["agent_msgs"].most_common()))

        # 工具统计
        if g["tools"]:
            top_tools = g["tools"].most_common(6)
            lines.append("- 主要工具: " + ", ".join(f"{n}({c})" for n, c in top_tools))

        # 代表性用户提问
        if g["user_msgs"]:
            lines.append(f"- 关键提问（前 {top_n_user_msgs} 条）:")
            for ts, txt in g["user_msgs"][:top_n_user_msgs]:
                snippet = txt.replace("\n", " ")
                if len(snippet) > 120:
                    snippet = snippet[:120] + "…"
                t = (ts or "")[:16].replace("T", " ")
                lines.append(f"  - [{t}] {snippet}")

        lines.append("")

    return "\n".join(lines)


def attach_diaries(since: datetime, until: datetime) -> str:
    """如果时间范围覆盖了某完整天，附上那天的 diary.md。"""
    out = []
    cur = since.date()
    end = until.date()
    while cur <= end:
        # 只附"整天被覆盖"的日记，避免半天数据误导
        day_start = datetime.combine(cur, datetime.min.time()).astimezone(since.tzinfo)
        day_end = day_start + timedelta(days=1)
        cur = cur + timedelta(days=1)
        if since <= day_start and until >= day_end - timedelta(microseconds=1):
            path = DAILY_DIR / (cur - timedelta(days=1)).strftime("%Y-%m-%d") / "diary.md"
            if path.exists():
                out.append(f"\n--- diary: {path.parent.name} ---\n")
                out.append(path.read_text(encoding="utf-8"))
    return "\n".join(out)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=float, help="最近 N 小时（与 --days 二选一）")
    parser.add_argument("--days", type=float, help="最近 N 天")
    parser.add_argument("--cwd", type=str, help="只看某工作目录（前缀匹配）")
    parser.add_argument("--topN", type=int, default=3, help="每 group 展示的用户提问数量")
    parser.add_argument("--no-diary", action="store_true", help="不附带 diary.md")
    args = parser.parse_args()

    now = datetime.now().astimezone()
    if args.hours is not None:
        since = now - timedelta(hours=args.hours)
    elif args.days is not None:
        since = now - timedelta(days=args.days)
    else:
        since = now - timedelta(hours=24)

    events = load_events_in_range(since, now)
    if args.cwd:
        events = [e for e in events if (e.get("cwd") or "").startswith(args.cwd)]

    grouped = summarize(events, top_n_user_msgs=args.topN)
    report = format_report(grouped, since, now, top_n_user_msgs=args.topN)
    print(report)

    if not args.no_diary:
        extra = attach_diaries(since, now)
        if extra.strip():
            print(extra)

    return 0 if events else 1


if __name__ == "__main__":
    sys.exit(main())
