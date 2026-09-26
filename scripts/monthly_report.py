#!/usr/bin/env python3
"""hubmemory monthly_report — 汇总一个月的 diary.md，生成月度总结。

用法：
    python3 monthly_report.py --month 2026-09
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
DAILY_DIR = HUB_HOME / "daily"
MONTHLY_DIR = HUB_HOME / "monthly"
LOGS_DIR = HUB_HOME / "logs"


def load_config() -> dict:
    path = HUB_HOME / "config.yaml"
    if not path.exists():
        return {}
    try:
        import yaml
        with path.open("r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def log(msg: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with (LOGS_DIR / "daily_report.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def month_days(year: int, month: int) -> list[date]:
    d = date(year, month, 1)
    days = []
    while d.month == month:
        days.append(d)
        d += timedelta(days=1)
    return days


def load_diaries(year: int, month: int) -> str:
    parts = []
    for d in month_days(year, month):
        path = DAILY_DIR / d.strftime("%Y-%m-%d") / "diary.md"
        if path.exists():
            parts.append(f"=== {d} ===\n" + path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def load_evidence(year: int, month: int) -> str:
    parts = []
    for d in month_days(year, month):
        path = DAILY_DIR / d.strftime("%Y-%m-%d") / "evidence.jsonl"
        if not path.exists():
            continue
        parts.append(f"=== {d} evidence ===\n" + path.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def aggregate_contrib(year: int, month: int) -> dict:
    totals: dict[str, int] = defaultdict(int)
    grand_total = 0
    for d in month_days(year, month):
        path = DAILY_DIR / d.strftime("%Y-%m-%d") / "contrib.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for sess in data.values():
            for k, v in (sess.get("agents") or {}).items():
                totals[k] += int(v)
                grand_total += int(v)
    ratios = {}
    if grand_total > 0:
        for k, v in totals.items():
            ratios[k] = round(v / grand_total, 4)
    return {"agents": dict(totals), "ratios": ratios, "total_tokens": grand_total}


def call_llm(endpoint: str, model: str, system: str, user: str, timeout: int = 90) -> str | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, TimeoutError) as exc:
        log(f"LLM call failed: {exc}")
        return None
    try:
        obj = json.loads(body)
        return obj["choices"][0]["message"]["content"]
    except (KeyError, IndexError, json.JSONDecodeError) as exc:
        log(f"LLM response unparseable: {exc}")
        return None


def summary_fallback(year: int, month: int, diaries: str, evidence: str, contrib: dict) -> str:
    lines = [f"# {year}-{month:02d} 月度总结（规则模板）", ""]
    total = contrib.get("total_tokens", 0)
    lines.append(f"总 token 输出：{total:,}")
    for k, v in (contrib.get("agents") or {}).items():
        pct = round(v / total * 100, 1) if total else 0
        lines.append(f"- {k}: {v:,} ({pct}%)")
    lines.append("")
    lines.append("## 结构化工作证据索引")
    lines.append("")
    lines.append(evidence[:20000] if evidence.strip() else diaries[:20000])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--month", required=True, help="YYYY-MM")
    args = parser.parse_args()

    y, m = args.month.split("-")
    year, month = int(y), int(m)

    diaries = load_diaries(year, month)
    evidence = load_evidence(year, month)
    if not diaries.strip() and not evidence.strip():
        log(f"no diaries found for {args.month}")
        return 0

    contrib = aggregate_contrib(year, month)
    from report_due import complete_request, ensure_request
    request = ensure_request(
        "monthly",
        args.month,
        [str(DAILY_DIR), str(MONTHLY_DIR / f"{args.month}.md")],
    )
    if request.get("status") == "done" and (MONTHLY_DIR / f"{args.month}.md").exists():
        log(f"monthly report already completed for {args.month}, preserving AI summary")
        return 0

    config = load_config()
    llm_cfg = config.get("llm") or {}
    endpoint = llm_cfg.get("endpoint")
    model = llm_cfg.get("model")

    summary = None
    if llm_cfg.get("enabled") is True and endpoint and model:
        system = (
            "你是一个技术月报生成助手。用户会给你一个月中每天的 diary.md 内容。"
            "请总结整体工作方向、主要产出、涉及的技术栈、每个 agent 的贡献占比、以及待跟进事项。"
            "Markdown 格式，500 字以内，不要复制原文。"
        )
        user_msg = (
            f"月份：{args.month}\n\n"
            f"结构化工作证据：\n{evidence[:20000]}\n\n"
            f"每日日记：\n{diaries[:12000]}\n\n"
            f"贡献汇总：\n{json.dumps(contrib, ensure_ascii=False, indent=2)}\n\n"
            "请生成月度总结。"
        )
        summary = call_llm(endpoint, model, system, user_msg,
                           timeout=llm_cfg.get("timeout_seconds", 90))

    used_llm = bool(summary)
    if not summary:
        log("LLM disabled or unavailable, using deterministic monthly template")
        summary = summary_fallback(year, month, diaries, evidence, contrib)

    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
    out = MONTHLY_DIR / f"{args.month}.md"
    out.write_text(summary, encoding="utf-8")
    log(f"monthly summary written: {out}")
    if used_llm:
        complete_request("monthly", args.month, "configured-llm", model)
    return 0


if __name__ == "__main__":
    sys.exit(main())
