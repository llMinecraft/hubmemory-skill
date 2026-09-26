#!/usr/bin/env python3
"""hubmemory daily_report — 汇总一天的 sessions.jsonl，产出 diary.md、contrib.json，
并可选地通过本地 LLM 提取用户约束/偏好写入 profile/。

用法：
    python3 daily_report.py                     # 默认处理昨天
    python3 daily_report.py --date 2026-09-21   # 指定日期
    python3 daily_report.py --auto              # launchd 自动模式（跑昨天，若月末再跑月报）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

from evidence import build_evidence, evidence_text


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR",
    os.path.expanduser("~/.claude/skills/hubmemory"),
))
DAILY_DIR = HUB_HOME / "daily"
PROFILE_DIR = HUB_HOME / "profile"
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


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def load_sessions(target_date: date) -> list[dict]:
    path = DAILY_DIR / target_date.strftime("%Y-%m-%d") / "sessions.jsonl"
    if not path.exists():
        return []
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def compute_contrib(events: list[dict]) -> dict:
    """按 session_id 分组，每组内按 agent+model 汇总 tokens_out。"""
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
        ratios = {}
        if total > 0:
            for k, v in entry["agents"].items():
                ratios[k] = round(v / total, 4)
        result[sid] = {
            "cwd": entry["cwd"],
            "agents": dict(entry["agents"]),
            "ratios": ratios,
            "total_tokens": total,
        }
    return result


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _is_context_noise(text: str) -> bool:
    normalized = (text or "").strip().lower()
    return (
        not normalized
        or normalized.startswith("<environment_context>")
        or normalized.startswith("<app-context>")
        or normalized.startswith("# agents.md instructions")
        or normalized.startswith("<collaboration_mode>")
        or "<heartbeat>" in normalized
    )


def build_work_summary(events: list[dict], max_evidence_per_agent: int = 80) -> str:
    """Build a compact evidence pack grouped by agent, not by raw session."""
    grouped: dict[str, list[dict]] = defaultdict(list)
    for evt in events:
        grouped[evt.get("agent") or "unknown"].append(evt)

    parts: list[str] = []
    for agent, agent_events in grouped.items():
        sessions = defaultdict(list)
        for evt in agent_events:
            sessions[evt.get("session_id") or "unknown"].append(evt)
        models = sorted({evt.get("model") or "?" for evt in agent_events})
        timestamps = [evt.get("ts", "") for evt in agent_events if evt.get("ts")]
        parts.append(f"## agent: {agent}")
        parts.append(f"sessions: {len(sessions)}; models: {', '.join(models)}")
        if timestamps:
            parts.append(f"time: {min(timestamps)[:19].replace('T', ' ')} -> {max(timestamps)[:19].replace('T', ' ')}")

        candidates = []
        for index, evt in enumerate(agent_events):
            role = evt.get("role") or "?"
            text = (evt.get("text") or "").strip()
            tools = evt.get("tool_uses") or []
            tool_names = ", ".join(tu.get("name", "?") for tu in tools)
            if role in {"developer", "system"}:
                continue
            if _is_context_noise(text) and not tool_names:
                continue
            # Prefer actual user goals and substantive assistant actions over raw tool output.
            if role == "assistant" and text:
                priority = 4 if len(text) >= 80 else 2
            elif role == "user" and text:
                priority = 3
            elif tool_names:
                priority = 2
            else:
                priority = 1
            candidates.append((priority, index, evt, tool_names))

        selected = sorted(candidates, key=lambda item: (-item[0], item[1]))[:max_evidence_per_agent]
        for _, _, evt, tool_names in sorted(selected, key=lambda item: item[1]):
            ts = evt.get("ts", "?")[:19].replace("T", " ")
            role = evt.get("role", "?")
            text = _clip(evt.get("text") or "", 420)
            detail = f" tools={tool_names}" if tool_names else ""
            parts.append(f"- [{ts}] {role}{detail}: {text}")
        parts.append("")
    return "\n".join(parts)


def build_evidence_summary(records: list[dict]) -> str:
    """Build the model input from structured, source-linked evidence."""
    parts = [f"## work item {index}" for index, _ in enumerate(records, 1)]
    if not records:
        return ""
    parts = []
    for index, record in enumerate(records, 1):
        parts.append(f"## work item {index}\n{evidence_text(record)}")
    return "\n\n".join(parts)


def call_llm(endpoint: str, model: str, system: str, user: str, timeout: int = 60) -> str | None:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
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
        log(f"LLM response unparseable: {exc}; body head={body[:200]}")
        return None


def diary_via_llm(target_date: date, contrib: dict, summary: str, config: dict) -> str | None:
    llm_cfg = config.get("llm") or {}
    if llm_cfg.get("enabled") is not True:
        return None
    endpoint = llm_cfg.get("endpoint")
    model = llm_cfg.get("model")
    if not endpoint or not model:
        return None
    system = (
        "你是一个技术工作日记生成助手。你的任务是梳理昨天所有有过会话的 agent 实际做了什么。"
        "输入已经按 agent 分组，包含用户目标、助手行动、工具调用和结果线索。"
        "必须先输出一个 `## 全天总结`，明确当天的主要工作主线、已完成事项、进行中/阻塞事项和整体结论。"
        "然后按 agent 输出独立的二级标题；每个 agent 下按工作事项归纳，且每项必须包含：目标、实际完成、"
        "关键证据、任务结论、当前状态（已完成/进行中/失败/阻塞）、下一步、来源 session。"
        "任务结论必须是对该项工作的明确收束，不能只写‘做了检查’或重复用户问题。"
        "可以合并同一项目的多个 session，但不能遗漏有活动的 agent。"
        "跳过系统提示、闲聊、重复的工具输出、失败后被放弃的尝试和模型私有思考。"
        "只写输入中有证据的事实，不补猜测，不加评价，不做建议。"
        "最后加一个 '## 统计' 节列总 token 和 agent 占比。输出 markdown。"
    )
    user_msg = (
        f"日期：{target_date}\n\n"
        f"贡献表（token）：\n{json.dumps(contrib, ensure_ascii=False, indent=2)}\n\n"
        f"按 agent 汇总的工作证据：\n{summary}\n\n"
        f"请生成昨天的 diary.md。先给全天总结，再逐项给出明确任务结论；不要只复制证据原文。"
    )
    return call_llm(endpoint, model, system, user_msg,
                    timeout=llm_cfg.get("timeout_seconds", 60))


def valid_diary(text: str) -> bool:
    """Reject a model response that omits the handoff sections we require."""
    if not text or "## 全天总结" not in text or "## 统计" not in text:
        return False
    return all(marker in text for marker in ("任务结论", "当前状态", "来源"))


STATUS_LABELS = {
    "completed": "已完成",
    "in_progress": "进行中",
    "blocked": "阻塞",
    "failed": "失败",
}


def record_conclusion(record: dict) -> str:
    """Turn evidence into an explicit, bounded conclusion for one work item."""
    results = [str(item).strip() for item in (record.get("results") or []) if str(item).strip()]
    actions = [str(item).strip() for item in (record.get("actions") or []) if str(item).strip()]
    status = record.get("status") or "in_progress"
    result_markers = (
        "已完成", "已经完成", "已经修复", "已经补回", "已经成功", "已经通过", "已验证",
        "成功", "通过", "修复", "补回", "配置好了", "生效", "结论是", "原因是",
    )
    confirmed = [
        result for result in results
        if any(marker in result for marker in result_markers)
    ]
    if confirmed:
        return _clip(confirmed[-1], 900)
    if results:
        return _clip(results[-1], 900)
    if status == "completed" and actions:
        return "证据显示该事项已完成主要处理，但没有提取到独立的最终验证结论：" + _clip(actions[-1], 500)
    if status == "blocked":
        return "该事项未形成完成结论，当前被证据中的失败或阻塞信息卡住。"
    if actions:
        return "尚未形成完成结论；当前已记录的最后动作是：" + _clip(actions[-1], 500)
    return "没有识别到足够的实际动作，无法确认该事项已完成。"


def day_overview(evidence: list[dict]) -> list[str]:
    """Render a compact whole-day handoff without pretending to be semantic AI."""
    counts = defaultdict(int)
    for item in evidence:
        counts[item.get("status") or "in_progress"] += 1
    agents = sorted({item.get("agent") or "unknown" for item in evidence})
    lines = [
        "## 全天总结",
        f"- 活动范围：{len(agents)} 个 agent、{len(evidence)} 个会话/工作事项。",
        "- 状态分布：" + "；".join(
            f"{STATUS_LABELS.get(key, key)} {counts[key]} 项"
            for key in ("completed", "in_progress", "blocked", "failed")
            if counts[key]
        ) + "。",
    ]
    completed = [item for item in evidence if item.get("status") == "completed"]
    active = [item for item in evidence if item.get("status") in {"in_progress", "blocked", "failed"}]
    if completed:
        lines.append("- 已完成主线：" + "；".join(
            _clip((item.get("goals") or ["未命名事项"])[0], 180) for item in completed[:5]
        ) + "。")
        lines.append("- 主要结论：" + "；".join(
            _clip(record_conclusion(item), 260) for item in completed[:5]
        ) + "。")
    if active:
        lines.append("- 未收束事项：" + "；".join(
            _clip((item.get("goals") or ["未命名事项"])[0], 180) for item in active[:5]
        ) + "。")
    if completed and not active:
        overall = "当天记录中的工作事项均有完成证据。"
    elif completed:
        overall = "当天同时包含已完成事项和未收束事项；后者不能视为已完成。"
    else:
        overall = "当天记录没有足够的完成证据，不能将工作事项视为已完成。"
    lines.append(f"- 全天结论：{overall}")
    return lines


def diary_fallback(
    target_date: date,
    events: list[dict],
    contrib: dict,
    evidence: list[dict] | None = None,
) -> str:
    lines = [f"# {target_date} 工作日记", ""]
    if evidence:
        lines.extend(day_overview(evidence))
        lines.append("")
        for agent in sorted({item.get("agent") or "unknown" for item in evidence}):
            lines.append(f"## {agent}")
            for item in [record for record in evidence if record.get("agent") == agent]:
                sid = str(item.get("session_id") or "unknown")
                goals = item.get("goals") or ["未识别到明确目标"]
                actions = item.get("actions") or ["未识别到足够的具体动作"]
                next_steps = item.get("next_steps") or ["未识别到下一步"]
                status = item.get("status", "in_progress")
                lines.extend([
                    f"### 工作事项 · Session {sid[:8]}",
                    f"- 工作目标：{'；'.join(goals[:3])}",
                    "- 实际动作：",
                    *[f"  - {action}" for action in actions[:6]],
                    f"- 关键证据：{'；'.join((item.get('results') or actions or ['没有明确证据'])[:4])}",
                    f"- 任务结论：{record_conclusion(item)}",
                    f"- 当前状态：{STATUS_LABELS.get(status, status)}",
                    f"- 下一步：{'；'.join(next_steps[:3]) if status != 'completed' else '无已识别的未完成步骤'}",
                    f"- 涉及文件/工具：{', '.join((item.get('files') or [])[:20]) or '未识别'}；工具：{', '.join((item.get('tools') or [])[:12]) or '未识别'}",
                    f"- 来源：`{sid}`；证据事件数：{len(item.get('source_event_ids') or [])}",
                    "",
                ])
            lines.append("")
    else:
        lines.extend([
            "## 全天总结",
            "- 当天没有可用的结构化证据，无法生成工作结论。",
            "- 全天结论：没有足够事实判断当天是否完成了任务。",
            "",
        ])
    grouped: dict[str, list[dict]] = defaultdict(list)
    if evidence:
        grouped = defaultdict(list)
    else:
        for evt in events:
            grouped[evt.get("agent") or "unknown"].append(evt)

    for agent, agent_events in grouped.items():
        sessions = defaultdict(list)
        for evt in agent_events:
            sessions[evt.get("session_id") or "unknown"].append(evt)
        models = sorted({evt.get("model") or "?" for evt in agent_events})
        lines.append(f"## {agent}")
        lines.append(f"- 会话数：{len(sessions)}；模型：{', '.join(models)}")
        for sid, evts in sessions.items():
            lines.append(f"### 工作事项 · Session {sid[:8]}")
            timestamps = [evt.get("ts", "") for evt in evts if evt.get("ts")]
            if timestamps:
                lines.append(f"- 时间：{min(timestamps)[:16].replace('T', ' ')} - {max(timestamps)[:16].replace('T', ' ')}")
            user_work = [
                _clip(evt.get("text") or "", 180)
                for evt in evts
                if evt.get("role") == "user" and not _is_context_noise(evt.get("text") or "")
            ][:3]
            actions = [
                _clip(evt.get("text") or "", 220)
                for evt in evts
                if evt.get("role") == "assistant"
                and not _is_context_noise(evt.get("text") or "")
                and len((evt.get("text") or "").strip()) >= 60
            ][:6]
            tool_names = sorted({
                tu.get("name", "?")
                for evt in evts
                for tu in (evt.get("tool_uses") or [])
                if tu.get("name")
            })
            if user_work:
                lines.append("- 工作目标：" + "；".join(user_work))
            if actions:
                lines.append("- 具体工作：")
                lines.extend(f"  - {action}" for action in actions)
            lines.append("- 任务结论：未生成结构化证据，无法确认最终结果。")
            lines.append("- 当前状态：进行中")
            if tool_names:
                lines.append("- 使用工具：" + ", ".join(tool_names))
        lines.append("")
        lines.append("")

    total = sum(v.get("total_tokens", 0) for v in contrib.values())
    lines.append("## 统计")
    lines.append(f"- 总 token 输出：{total:,}")
    agent_totals: dict[str, int] = defaultdict(int)
    for sess in contrib.values():
        for k, v in sess["agents"].items():
            agent_totals[k] += v
    if total > 0:
        for k, v in agent_totals.items():
            lines.append(f"- {k}: {v:,} ({round(v/total*100, 1)}%)")
    return "\n".join(lines)


def extract_profile_via_llm(target_date: date, events: list[dict], config: dict) -> dict | None:
    llm_cfg = config.get("llm") or {}
    endpoint = llm_cfg.get("endpoint")
    model = llm_cfg.get("model")
    if not endpoint or not model:
        return None

    user_msgs = [
        {
            "session_id": evt.get("session_id"),
            "ts": evt.get("ts"),
            "text": (evt.get("text") or "")[:600],
        }
        for evt in events
        if evt.get("role") == "user" and (evt.get("text") or "").strip()
    ]
    if not user_msgs:
        return None

    system = (
        "你是一个用户画像抽取助手。用户会给你一天里所有 user 消息。"
        "提取其中的：1) constraints（明确的禁令/必守规则）2) preferences（非强制的风格倾向）。"
        "只提取对未来 AI 交互有指导意义的表达；一次性任务请求不要提取；每条 30 字以内；"
        "不确定就不要提取。严格输出 JSON，不要 markdown 代码块包裹，格式："
        '{"constraints":[{"text":"...","session_id":"...","ts":"..."}],'
        '"preferences":[{"text":"...","session_id":"...","ts":"..."}]}'
    )
    user_content = (
        f"日期：{target_date}\n\n"
        f"user 消息：\n{json.dumps(user_msgs, ensure_ascii=False, indent=2)}\n\n"
        "请输出 JSON。"
    )
    resp = call_llm(endpoint, model, system, user_content,
                    timeout=llm_cfg.get("timeout_seconds", 60))
    if not resp:
        return None
    # 尝试从可能的 code fence 里剥 JSON
    stripped = resp.strip()
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        log(f"profile LLM response has no JSON: {stripped[:200]}")
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        log(f"profile JSON parse failed: {exc}; head={stripped[:200]}")
        return None


def append_profile(entries: list[dict], filename: str) -> int:
    if not entries:
        return 0
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    path = PROFILE_DIR / filename
    existing_hashes: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.search(r"\((?:sha1:)?([0-9a-f]{8,})\)$", line)
            if m:
                existing_hashes.add(m.group(1))
    lines = []
    added = 0
    for e in entries:
        text = (e.get("text") or "").strip()
        if not text:
            continue
        h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        if h in existing_hashes:
            continue
        ts = (e.get("ts") or "")[:16].replace("T", " ")
        sid = (e.get("session_id") or "")[:8]
        lines.append(f"- [{ts}] {text} (session: {sid}, sha1:{h})")
        existing_hashes.add(h)
        added += 1
    if lines:
        with path.open("a", encoding="utf-8") as f:
            if path.stat().st_size > 0 and not path.read_text(encoding="utf-8").endswith("\n"):
                f.write("\n")
            f.write("\n".join(lines) + "\n")
    return added


def log(msg: str) -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    line = f"[{datetime.now().isoformat(timespec='seconds')}] {msg}"
    print(line)
    with (LOGS_DIR / "daily_report.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def is_month_end(d: date) -> bool:
    next_day = d + timedelta(days=1)
    return next_day.month != d.month


def run_monthly_if_needed(target_date: date) -> None:
    if not is_month_end(target_date):
        return
    monthly_script = SKILL_DIR / "scripts" / "monthly_report.py"
    if not monthly_script.exists():
        log("monthly_report.py missing, skip")
        return
    import subprocess
    month_str = target_date.strftime("%Y-%m")
    log(f"triggering monthly report for {month_str}")
    subprocess.run(
        [sys.executable, str(monthly_script), "--month", month_str],
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=str, help="YYYY-MM-DD; default = yesterday")
    parser.add_argument("--auto", action="store_true", help="launchd 模式，跑昨天，月末再跑月报")
    args = parser.parse_args()

    if args.date:
        target = parse_date(args.date)
    else:
        target = (datetime.now() - timedelta(days=1)).date()

    log(f"daily report start for {target}")
    events = load_sessions(target)
    if not events:
        log(f"no events for {target}, nothing to do")
        return 0

    contrib = compute_contrib(events)
    daily_target = DAILY_DIR / target.strftime("%Y-%m-%d")
    daily_target.mkdir(parents=True, exist_ok=True)
    (daily_target / "contrib.json").write_text(
        json.dumps(contrib, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    config = load_config()
    evidence = build_evidence(events, str(target))
    evidence_path = daily_target / "evidence.jsonl"
    evidence_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in evidence),
        encoding="utf-8",
    )
    summary = build_evidence_summary(evidence) or build_work_summary(events)
    from report_due import complete_request, ensure_request
    request = ensure_request(
        "daily",
        str(target),
        [
            str(daily_target / "sessions.jsonl"),
            str(daily_target / "evidence.jsonl"),
            str(daily_target / "contrib.json"),
        ],
    )
    if request.get("status") == "done" and (daily_target / "diary.md").exists():
        log(f"daily report already completed for {target}, preserving AI summary")
        if args.auto:
            run_monthly_if_needed(target)
        return 0
    llm_enabled = (config.get("llm") or {}).get("enabled") is True
    diary = diary_via_llm(target, contrib, summary, config)
    if diary and not valid_diary(diary):
        log("LLM diary failed required structure validation, using deterministic diary template")
        diary = None
    used_llm = bool(diary)
    if not diary:
        if llm_enabled:
            log("LLM unavailable, using deterministic diary template")
        else:
            log("LLM disabled, using deterministic diary template")
        diary = diary_fallback(target, events, contrib, evidence)
    (daily_target / "diary.md").write_text(diary, encoding="utf-8")
    log(f"wrote diary.md ({'LLM' if used_llm else 'deterministic'})")
    if used_llm:
        complete_request("daily", str(target), "configured-llm", (config.get("llm") or {}).get("model", "unknown"))

    if used_llm:
        profile = extract_profile_via_llm(target, events, config)
        if profile:
            c_added = append_profile(profile.get("constraints") or [], "constraints.md")
            p_added = append_profile(profile.get("preferences") or [], "preferences.md")
            log(f"profile updated: +{c_added} constraints, +{p_added} preferences")

    if args.auto:
        run_monthly_if_needed(target)

    log("daily report done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
