#!/usr/bin/env python3
"""hubmemory backfill — 通过统一租约 API 为历史日期补登 pending 请求和规则草稿。

v3 日报契约：标题/TL;DR/会话概览/工作事项/统计全部由程序确定性装配，
不内嵌 JSON 块；AI 只产出「全天总结」候选（由 claim→publish 流程提交）。
backfill 本身绝不调用 AI 写整篇日报、明细或统计 —— 只生成无 JSON 的
确定性草稿（标题含「规则草稿」标记）并把 request 留为 pending；claimed/
done 请求永不覆盖。contrib.json 已停止生成，缺失不触发补登。

用法：
    python3 backfill.py                  # 处理所有缺少 diary.md 的日期
    python3 backfill.py --from 2026-08-01  # 只处理指定日期之后
    python3 backfill.py --date 2026-09-10  # 只处理某一天
    python3 backfill.py --force            # 重建 pending 草稿
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from evidence import build_evidence


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR",
    str(Path(__file__).resolve().parent.parent),
))
DAILY_DIR = HUB_HOME / "daily"
LOGS_DIR = HUB_HOME / "logs"


def log(msg: str) -> None:
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_sessions(target_date: date) -> list[dict]:
    path = DAILY_DIR / target_date.strftime("%Y-%m-%d") / "sessions.jsonl"
    if not path.exists():
        return []
    events = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                continue
    return events


def _build_conversations_md(d: str, events: list[dict]) -> str:
    from collections import defaultdict
    grouped: dict[str, list[dict]] = {}
    for evt in events:
        sid = evt.get("session_id") or "unknown"
        grouped.setdefault(sid, []).append(evt)

    total_user = sum(1 for e in events if e.get("role") == "user")
    total_asst = sum(1 for e in events if e.get("role") == "assistant")
    lines = [
        f"# {d} 会话归档",
        "",
        f"> 共 {len(grouped)} 个 session，{total_user} 条用户消息，{total_asst} 条 AI 回复",
        "",
    ]
    for sid, evts in grouped.items():
        agent = evts[0].get("agent", "?")
        model = evts[0].get("model", "?")
        cwd = evts[0].get("cwd", "")
        first_ts = (evts[0].get("ts") or "")[:16].replace("T", " ")
        last_ts = (evts[-1].get("ts") or "")[:16].replace("T", " ")
        u_cnt = sum(1 for e in evts if e.get("role") == "user")
        a_cnt = sum(1 for e in evts if e.get("role") == "assistant")
        lines += [
            f"## {agent} / {model}",
            f"- session: `{sid[:16]}…`",
        ]
        if cwd:
            lines.append(f"- cwd: {cwd}")
        lines += [
            f"- 时间: {first_ts} → {last_ts}",
            f"- 消息: 用户 {u_cnt} 条，AI {a_cnt} 条",
            "",
        ]
        for evt in evts:
            role = evt.get("role", "?")
            if role == "tool":
                continue
            ts_str = (evt.get("ts") or "")[:16].replace("T", " ")
            text = (evt.get("text") or "").strip()
            if len(text) > 400:
                text = text[:400] + "…"
            tool_uses = evt.get("tool_uses") or []
            if role == "user":
                lines.append(f"**[{ts_str}] 用户**")
            elif role == "assistant":
                lines.append(f"**[{ts_str}] AI**")
            else:
                lines.append(f"**[{ts_str}] {role}**")
            if text:
                lines.append(text)
            for tu in tool_uses:
                lines.append(f"> `{tu.get('name', '?')}` {tu.get('input_summary', '')}")
            lines.append("")
    return "\n".join(lines)


def compute_contrib(events: list[dict]) -> dict:
    grouped: dict[str, dict] = {}
    for evt in events:
        sid = evt.get("session_id") or "unknown"
        key = f"{evt.get('agent', '?')}:{evt.get('model', '?')}"
        entry = grouped.setdefault(sid, {"cwd": evt.get("cwd", ""), "agents": defaultdict(int)})
        if evt.get("cwd") and not entry["cwd"]:
            entry["cwd"] = evt["cwd"]
        entry["agents"][key] += int(evt.get("tokens_out") or 0)
    result = {}
    for sid, entry in grouped.items():
        total = sum(entry["agents"].values())
        ratios = {k: round(v / total, 4) for k, v in entry["agents"].items()} if total > 0 else {}
        result[sid] = {
            "cwd": entry["cwd"],
            "agents": dict(entry["agents"]),
            "ratios": ratios,
            "total_tokens": total,
        }
    return result


def diary_fallback(target_date: date, events: list[dict], contrib: dict) -> str:
    """Compatibility name: backfill drafts use the same unmistakable marker.

    Deterministic v3 draft only — no JSON block, no AI-written detail; the
    returned request stays pending for an overview-only claim."""
    from daily_report import diary_fallback as fallback
    return fallback(target_date, events, contrib, build_evidence(events, str(target_date)))


def try_llm_diary(target_date: date, events: list[dict], contrib: dict) -> str | None:
    """Legacy helper name kept for callers; under v3 'LLM' means at most the
    「全天总结」 overview candidate — never whole-report generation."""
    from daily_report import generate_daily
    from report_contract import read_daily_text
    generate_daily(target_date, use_llm=True)
    return read_daily_text(HUB_HOME, str(target_date), "diary.md") or None


def process_date(target_date: date, force: bool, use_llm: bool) -> bool:
    """Force rebuilds pending drafts only, never steals claims or replaces done."""
    from daily_report import generate_daily
    return generate_daily(target_date, use_llm=use_llm)


def all_dates_in_daily() -> list[date]:
    from report_contract import available_dates
    return [date.fromisoformat(day) for day in available_dates(HUB_HOME)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="只处理某一天 YYYY-MM-DD")
    parser.add_argument("--from", dest="from_date", type=str, help="处理从该日期开始的所有天")
    parser.add_argument("--force", action="store_true", help="重建 pending 草稿；不覆盖 claimed/done 报告")
    parser.add_argument("--no-llm", action="store_true",
                        help="跳过 LLM 全天总结候选生成，直接产出确定性规则草稿（其余部分本来就不是 AI 生成的）")
    args = parser.parse_args()

    use_llm = not args.no_llm

    if args.date:
        dates = [parse_date(args.date)]
    else:
        dates = all_dates_in_daily()
        if args.from_date:
            cutoff = parse_date(args.from_date)
            dates = [d for d in dates if d >= cutoff]

    log(f"backfill: {len(dates)} 个日期，force={args.force}, llm={use_llm}")
    processed = 0
    for d in dates:
        if process_date(d, args.force, use_llm):
            processed += 1

    log(f"done: {processed}/{len(dates)} 个日期已处理")
    return 0


if __name__ == "__main__":
    sys.exit(main())
