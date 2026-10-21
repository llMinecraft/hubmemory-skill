"""Shared semantic report contract. Validation never writes source data."""
from __future__ import annotations

import json
import re
from pathlib import Path

STATUSES = ("completed", "in_progress", "blocked", "failed")
DRAFT_MARKERS = ("内置规则", "规则模板", "规则草稿", "LLM 不可用", "LLM disabled", "回填", "backfill", "draft")


def read_daily_text(home: Path, day: str, name: str) -> str:
    """Read hot or cold data; an unavailable cold reader fails closed."""
    path = home / "daily" / day / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    try:
        from cold_storage import read_day_text
        return read_day_text(home, day, name) or ""
    except (ImportError, FileNotFoundError):
        return ""


def session_rows(home: Path, day: str) -> list[dict]:
    from cold_storage import iter_day_records
    return iter_day_records(home, day, "sessions.jsonl")


def available_dates(home: Path) -> list[str]:
    dates = set()
    root = home / "daily"
    if root.exists():
        dates.update(p.name for p in root.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name))
    try:
        from cold_storage import iter_days
        dates.update(iter_days(home))
    except ImportError:
        pass
    return sorted(dates)


def parse_summary(text: str) -> dict:
    section = re.search(r"^## Agent-readable summary\s*$\s*.*?```json\s*\n(.*?)\n```", text, re.M | re.S)
    if not section:
        raise ValueError("missing_summary_json")
    try:
        payload = json.loads(section.group(1))
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid_summary_json") from exc
    if not isinstance(payload, dict):
        raise ValueError("invalid_summary_schema")
    return payload


def counts_for(tasks: list[dict]) -> dict:
    return {"task_count": len(tasks), "done": sum(t["status"] == "completed" for t in tasks),
            **{s: sum(t["status"] == s for t in tasks) for s in STATUSES if s != "completed"}}


def tldr_for(tasks: list[dict], main: str) -> str:
    c = counts_for(tasks)
    return (f'> **TL;DR**: {c["task_count"]}项任务 | 已完成{c["done"]} 进行中{c["in_progress"]} '
            f'阻塞/失败{c["blocked"] + c["failed"]} | 主线: {main}')


def validate_report(text: str, kind: str, period: str, home: Path | None = None) -> dict:
    """Return parsed digest or raise ValueError; counts/citations are verified, not trusted."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty_report")
    lines = text.splitlines()
    if any(marker.lower() in lines[0].lower() for marker in DRAFT_MARKERS):
        raise ValueError("report_is_still_draft")
    if len(lines) < 3 or not lines[0].startswith("# ") or period not in lines[0]:
        raise ValueError("invalid_report_title")
    if not lines[1].startswith("> **TL;DR**:"):
        raise ValueError("missing_tldr_second_line")
    # Daily contract v3: deterministic assembly, no Agent-readable JSON.
    if kind == "daily" and len(lines) > 2 and lines[2].strip() == "<!-- report-contract: daily-v3 -->":
        import daily_format
        rows = daily_format.evidence_rows(home, period) if home is not None else []
        daily_format.validate_assembled_report(
            text, period, daily_format.ref_map_for(rows) if rows else None, home)
        return {"contract": "daily-v3", "period": period}
    overview = "全天总结" if kind == "daily" else "月度总结"
    section = re.search(r"^## " + overview + r"\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not section or not re.sub(r"[#>*`\-\s]", "", section.group(1)):
        raise ValueError("missing_semantic_overview")
    # These are deliberately concise labels, not token-statistics requirements.
    for marker in ("工作目标", "实际动作", "关键证据", "任务结论", "当前状态", "下一步", "来源"):
        if marker not in text:
            raise ValueError("missing_task_section:" + marker)
    payload = parse_summary(text)
    if payload.get("date" if kind == "daily" else "month") != period:
        raise ValueError("summary_period_mismatch")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("empty_or_invalid_tasks")
    if not isinstance(payload.get("open_loops"), list) or not all(isinstance(x, str) and x.strip() for x in payload["open_loops"]):
        raise ValueError("invalid_open_loops")
    source_cache = {}
    for task in tasks:
        if not isinstance(task, dict):
            raise ValueError("invalid_task_schema")
        for key in ("goal", "conclusion", "session"):
            if not isinstance(task.get(key), str) or not task[key].strip() or task[key] == "unknown":
                raise ValueError("invalid_task_" + key)
        if task.get("status") not in STATUSES or task.get("confidence") not in ("low", "medium", "high"):
            raise ValueError("invalid_task_status_or_confidence")
        citations = task.get("citations")
        if not isinstance(citations, list) or not citations:
            raise ValueError("missing_task_citations")
        for citation in citations:
            if not isinstance(citation, dict) or not all(isinstance(citation.get(k), str) and citation[k].strip() for k in ("session", "event_id", "ref")):
                raise ValueError("invalid_citation_schema")
            ref = citation["ref"]
            match = re.fullmatch(r"(?:.*/)?daily/(\d{4}-\d{2}-\d{2})/sessions\.jsonl#L([1-9]\d*)", ref)
            cold_match = re.fullmatch(r"(.+\.tar\.gz)!([0-9]{4}-[0-9]{2})/(\d{4}-\d{2}-\d{2})/sessions\.jsonl#L([1-9]\d*)", ref)
            if (not match and not cold_match) or citation["session"] != task["session"]:
                raise ValueError("invalid_citation_ref_or_session")
            day = cold_match.group(3) if cold_match else match.group(1)
            number = int(cold_match.group(4) if cold_match else match.group(2))
            if (day != period if kind == "daily" else not day.startswith(period + "-")):
                raise ValueError("citation_period_mismatch")
            if home is None:
                continue
            from evidence import event_id
            if cold_match:
                expected_tar = home / "archive" / day[:4] / (day[:7] + ".tar.gz")
                if day[:7] != cold_match.group(2) or Path(cold_match.group(1)).resolve() != expected_tar.resolve():
                    raise ValueError("invalid_cold_citation_archive")
                from cold_storage import iter_day_records
                cited = next((row for row in iter_day_records(home, day, "sessions.jsonl") if row["locator"] == ref), None)
                if not cited:
                    raise ValueError("cold_citation_not_found")
                event = cited["record"]
            else:
                hot_path = home / "daily" / day / "sessions.jsonl"
                path_part = ref.rsplit("#L", 1)[0]
                if Path(path_part).is_absolute() and Path(path_part).resolve() != hot_path.resolve():
                    raise ValueError("invalid_hot_citation_path")
                if hot_path.exists():
                    # Overlay lines are always physical hot lines, never merged.
                    if day not in source_cache:
                        source_cache[day] = hot_path.read_text(encoding="utf-8").splitlines()
                    rows = source_cache[day]
                    if number > len(rows):
                        raise ValueError("citation_not_found")
                    try:
                        event = json.loads(rows[number - 1])
                    except ValueError as exc:
                        raise ValueError("citation_invalid_source") from exc
                else:
                    # Archiving preserves an old hot citation as an exact member
                    # line alias; never reinterpret it as merged logical row N.
                    from cold_storage import iter_day_records
                    cited = next((row for row in iter_day_records(home, day, "sessions.jsonl")
                                  if row.get("source") == "cold" and row["line"] == number), None)
                    if not cited:
                        raise ValueError("citation_not_found")
                    event = cited["record"]
            if not isinstance(event, dict) or event.get("session_id") != citation["session"] or event_id(event, number - 1) != citation["event_id"]:
                raise ValueError("citation_source_mismatch")
    counts = counts_for(tasks)
    for key, value in counts.items():
        if type(payload.get(key)) is not int or payload[key] != value:
            raise ValueError("summary_count_mismatch:" + key)
    if not lines[1].split(":", 1)[1].strip():
        raise ValueError("empty_tldr")
    # Natural TLDR wording is allowed. If numeric status claims are supplied,
    # they must still agree with the machine-derived counts.
    for pattern, expected in ((r"(\d+)项任务", counts["task_count"]),
                              (r"已完成\s*(\d+)", counts["done"]),
                              (r"进行中\s*(\d+)", counts["in_progress"]),
                              (r"阻塞/失败\s*(\d+)", counts["blocked"] + counts["failed"])):
        match = re.search(pattern, lines[1])
        if match and int(match.group(1)) != expected:
            raise ValueError("tldr_count_mismatch")
    if any(t["status"] != "completed" for t in tasks) and not payload["open_loops"]:
        raise ValueError("missing_open_loops")
    return payload


def prompt_contract(kind: str, period: str) -> str:
    """The exact same shape is used by configured LLMs and interactive agents."""
    if kind == "daily":
        # v3: AI writes ONLY the overview; program assembles everything else.
        import daily_format
        return daily_format.overview_prompt(period)
    overview = "月度总结"
    key = "date" if kind == "daily" else "month"
    return (
        f"输出简洁中文 Markdown，不含私有思考、token统计或空泛评价。首行 # {period} 工作总结，"
        "第二行必须为 > **TL;DR**: N项任务 | 已完成D 进行中P 阻塞/失败B | 主线: 实际主线。"
        f"随后 ## {overview} 用短段概括主线、产出、未收束事项和整体结论；按 agent/事项精简组织，"
        "每项保留工作目标、实际动作、关键证据、任务结论、当前状态、下一步、来源 session。"
        "只依据提供的事实；交付声称不是验证，证据不足明确标 low。最后 ## Agent-readable summary，"
        f"内含一个 json fenced block，对象字段 {key}:\"{period}\", task_count, done, in_progress, blocked, failed,"
        " open_loops:[未完成事项], tasks:[{goal,status,conclusion,session,confidence,citations:[{session,event_id,ref}]}]。"
        "status 仅 completed/in_progress/blocked/failed；confidence 仅 high/medium/low；session 用完整ID。"
        "ref 原样使用提供的热文件绝对路径或 archive.tar.gz!成员#L行号，event_id/session 必须与该行一致，禁止伪造。"
        "所有计数从 tasks 状态计算，TLDR B=blocked+failed；不得省略JSON，不要复制证据堆砌。"
    )
