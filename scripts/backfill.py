#!/usr/bin/env python3
"""hubmemory backfill — 对历史 daily 目录补生成 contrib.json 和 diary.md。

用法：
    python3 backfill.py                  # 处理所有缺少 diary.md 或 contrib.json 的日期
    python3 backfill.py --from 2026-08-01  # 只处理指定日期之后
    python3 backfill.py --date 2026-09-10  # 只处理某一天
    python3 backfill.py --force            # 强制覆盖已有文件
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
    os.path.expanduser("~/.claude/skills/hubmemory"),
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
    lines = [f"# {target_date} 工作日记（回填）", ""]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for evt in events:
        grouped[evt.get("session_id") or "unknown"].append(evt)

    for sid, evts in grouped.items():
        agent = evts[0].get("agent", "?")
        model = evts[0].get("model", "?")
        cwd = evts[0].get("cwd", "?")
        ratios = contrib.get(sid, {}).get("ratios", {})
        ratio_str = " / ".join(f"{k}: {round(v * 100)}%" for k, v in ratios.items())
        lines.append(f"## Session {sid[:8]} ({agent}/{model})")
        lines.append(f"cwd: {cwd}")
        if ratio_str:
            lines.append(f"[{ratio_str}]")
        user_msgs = [e for e in evts if e.get("role") == "user" and (e.get("text") or "").strip()]
        lines.append(f"用户提问 {len(user_msgs)} 条，assistant 回复 {sum(1 for e in evts if e.get('role') == 'assistant')} 条")
        # 列出代表性的用户提问（前 5 条）
        for evt in user_msgs[:5]:
            txt = (evt.get("text") or "").strip().replace("\n", " ")
            if len(txt) > 100:
                txt = txt[:100] + "…"
            ts = (evt.get("ts") or "")[:16].replace("T", " ")
            lines.append(f"- [{ts}] {txt}")
        if len(user_msgs) > 5:
            lines.append(f"- … ({len(user_msgs) - 5} 条已省略)")
        lines.append("")

    total = sum(v.get("total_tokens", 0) for v in contrib.values())
    agent_totals: dict[str, int] = defaultdict(int)
    for sess in contrib.values():
        for k, v in sess["agents"].items():
            agent_totals[k] += v
    lines.append("## 统计")
    lines.append(f"- 总 token 输出：{total:,}")
    if total > 0:
        for k, v in sorted(agent_totals.items(), key=lambda x: -x[1]):
            lines.append(f"- {k}: {v:,} ({round(v/total*100, 1)}%)")
    return "\n".join(lines)


def try_llm_diary(target_date: date, events: list[dict], contrib: dict) -> str | None:
    import subprocess
    daily_report = SKILL_DIR / "scripts" / "daily_report.py"
    if not daily_report.exists():
        return None
    result = subprocess.run(
        [sys.executable, str(daily_report), "--date", str(target_date)],
        capture_output=True, text=True, timeout=120,
    )
    # 如果日报脚本成功写了 diary.md，直接读回来
    diary_path = DAILY_DIR / target_date.strftime("%Y-%m-%d") / "diary.md"
    if result.returncode == 0 and diary_path.exists():
        return diary_path.read_text(encoding="utf-8")
    return None


def process_date(target_date: date, force: bool, use_llm: bool) -> bool:
    daily_dir = DAILY_DIR / target_date.strftime("%Y-%m-%d")
    sessions_path = daily_dir / "sessions.jsonl"
    contrib_path = daily_dir / "contrib.json"
    diary_path = daily_dir / "diary.md"
    evidence_path = daily_dir / "evidence.jsonl"

    if not sessions_path.exists():
        return False

    needs_contrib = force or not contrib_path.exists()
    needs_diary = force or not diary_path.exists()
    needs_conversations = force or not (daily_dir / "conversations.md").exists()
    needs_evidence = force or not evidence_path.exists()
    if not needs_contrib and not needs_diary and not needs_conversations and not needs_evidence:
        return False

    events = load_sessions(target_date)
    if not events:
        log(f"{target_date}: sessions.jsonl 为空，跳过")
        return False

    log(f"{target_date}: {len(events)} events, contrib={'需要' if needs_contrib else '已有'}, diary={'需要' if needs_diary else '已有'}, evidence={'需要' if needs_evidence else '已有'}")

    if needs_evidence:
        evidence = build_evidence(events, str(target_date))
        tmp = evidence_path.with_suffix(".jsonl.tmp")
        tmp.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in evidence),
            encoding="utf-8",
        )
        tmp.replace(evidence_path)
        log(f"  wrote evidence.jsonl ({len(evidence)} sessions)")

    if needs_contrib:
        contrib = compute_contrib(events)
        tmp = contrib_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(contrib, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(contrib_path)
        log(f"  wrote contrib.json ({len(contrib)} sessions)")
    else:
        try:
            contrib = json.loads(contrib_path.read_text(encoding="utf-8"))
        except Exception:
            contrib = compute_contrib(events)

    if needs_diary:
        diary = None
        if use_llm:
            diary = try_llm_diary(target_date, events, contrib)
        if not diary:
            diary = diary_fallback(target_date, events, contrib)
        diary_path.write_text(diary, encoding="utf-8")
        log(f"  wrote diary.md ({'LLM' if use_llm and 'LLM 不可用' not in diary else 'fallback'})")

    if needs_conversations:
        conversations = _build_conversations_md(str(target_date), events)
        conv_path = daily_dir / "conversations.md"
        tmp = conv_path.with_suffix(".md.tmp")
        tmp.write_text(conversations, encoding="utf-8")
        tmp.replace(conv_path)
        log(f"  wrote conversations.md ({len(events)} events)")

    return True


def all_dates_in_daily() -> list[date]:
    result = []
    for d in sorted(DAILY_DIR.iterdir()):
        if d.is_dir():
            try:
                result.append(datetime.strptime(d.name, "%Y-%m-%d").date())
            except ValueError:
                continue
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="只处理某一天 YYYY-MM-DD")
    parser.add_argument("--from", dest="from_date", type=str, help="处理从该日期开始的所有天")
    parser.add_argument("--force", action="store_true", help="强制覆盖已有 diary.md / contrib.json")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM，直接用规则模板")
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
