#!/usr/bin/env python3
"""Build deterministic, source-linked work evidence from session events.

The event stream remains the source of truth. Evidence is a derived index for
reports and retrieval, so it can always be rebuilt without a model.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any


NOISE_PREFIXES = (
    "<environment_context>",
    "<app-context>",
    "# agents.md instructions",
    "<collaboration_mode>",
)
STATUS_WORDS = {
    "completed": ("已完成", "完成了", "已修复", "通过", "成功", "已验证", "已经好了"),
    "blocked": ("阻塞", "blocked", "无法", "失败", "报错", "未能", "超时"),
    "in_progress": ("进行中", "继续", "下一步", "正在", "待处理", "todo"),
}
FILE_RE = re.compile(
    r"(?<![\w./-])(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|"
    r"(?<![\w./-])[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|json|md|yaml|yml|html|css|sql)(?![\w.-])"
)


def clip(value: str, limit: int = 600) -> str:
    text = " ".join((value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def representative(items: list[str], limit: int) -> list[str]:
    """Select entries across the full session instead of only its opening."""
    if len(items) <= limit:
        return items
    if limit <= 1:
        return items[-1:]
    indexes = {
        round(index * (len(items) - 1) / (limit - 1))
        for index in range(limit)
    }
    return [items[index] for index in sorted(indexes)]


def useful_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    lowered = text.lower()
    if not text or any(lowered.startswith(prefix) for prefix in NOISE_PREFIXES):
        return ""
    if "<heartbeat>" in lowered:
        return ""
    return text


def event_id(event: dict[str, Any], index: int) -> str:
    existing = event.get("event_id")
    if existing:
        return str(existing)
    identity = json.dumps(
        {
            "session_id": event.get("session_id"),
            "ts": event.get("ts"),
            "role": event.get("role"),
            "text": event.get("text"),
            "index": index,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(identity.encode("utf-8")).hexdigest()[:20]


def _tool_names(events: list[dict[str, Any]]) -> list[str]:
    names = {
        str(tool.get("name"))
        for event in events
        for tool in (event.get("tool_uses") or [])
        if isinstance(tool, dict) and tool.get("name")
    }
    return sorted(names)


def _files(events: list[dict[str, Any]], texts: list[str]) -> list[str]:
    found: set[str] = set()
    for text in texts:
        found.update(FILE_RE.findall(text))
    for event in events:
        for tool in event.get("tool_uses") or []:
            if isinstance(tool, dict):
                found.update(FILE_RE.findall(str(tool.get("input_summary") or "")))
    return sorted(found)[:80]


def _status(texts: list[str]) -> str:
    joined = " ".join(texts).lower()
    if any(word.lower() in joined for word in STATUS_WORDS["blocked"]):
        if not any(word.lower() in joined for word in STATUS_WORDS["completed"]):
            return "blocked"
    if any(word.lower() in joined for word in STATUS_WORDS["completed"]):
        return "completed"
    return "in_progress"


def build_evidence(events: list[dict[str, Any]], period: str | None = None) -> list[dict[str, Any]]:
    """Return one evidence record per active session, with source event ids."""
    grouped: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for index, event in enumerate(events):
        grouped[str(event.get("session_id") or "unknown")].append((index, event))

    records: list[dict[str, Any]] = []
    for session_id, indexed_events in grouped.items():
        session_events = [event for _, event in indexed_events]
        all_user_goals = [
            clip(useful_text(event.get("text") or ""), 500)
            for event in session_events
            if event.get("role") == "user" and useful_text(event.get("text") or "")
        ]
        user_goals = representative(all_user_goals, 5)
        assistant_texts = [
            clip(useful_text(event.get("text") or ""), 700)
            for event in session_events
            if event.get("role") == "assistant" and useful_text(event.get("text") or "")
        ]
        substantive_actions = representative(
            [text for text in assistant_texts if len(text) >= 60],
            12,
        )
        all_texts = user_goals + assistant_texts
        timestamps = [str(event.get("ts")) for event in session_events if event.get("ts")]
        agent = str(session_events[0].get("agent") or "unknown")
        model = str(session_events[0].get("model") or "unknown")
        cwd = str(session_events[0].get("cwd") or "")
        status = _status(all_texts)
        all_results = [
            text for text in assistant_texts
            if any(word.lower() in text.lower() for words in STATUS_WORDS.values() for word in words)
        ]
        results = representative(all_results, 8)
        all_next_steps = [
            text for text in assistant_texts
            if any(word.lower() in text.lower() for word in STATUS_WORDS["in_progress"])
        ]
        next_steps = representative(all_next_steps, 5)
        source_ids = [event_id(event, index) for index, event in indexed_events]
        fingerprint = hashlib.sha1(
            f"{period or ''}:{session_id}:{','.join(source_ids)}".encode("utf-8")
        ).hexdigest()[:20]
        records.append(
            {
                "id": f"evidence_{fingerprint}",
                "kind": "session_work",
                "period": period,
                "session_id": session_id,
                "agent": agent,
                "model": model,
                "cwd": cwd,
                "started_at": min(timestamps) if timestamps else None,
                "ended_at": max(timestamps) if timestamps else None,
                "goals": user_goals,
                "actions": substantive_actions,
                "results": results,
                "next_steps": next_steps,
                "status": status,
                "tools": _tool_names(session_events),
                "files": _files(session_events, all_texts),
                "source_event_ids": source_ids,
                "source_file": session_events[0].get("source_file"),
                "confidence": 0.65 if user_goals and substantive_actions else 0.45,
            }
        )
    records.sort(key=lambda record: (record.get("started_at") or "", record["session_id"]))
    return records


def evidence_text(record: dict[str, Any]) -> str:
    lines = [
        f"Session {record.get('session_id', '?')} ({record.get('agent', '?')})",
        f"Status: {record.get('status', 'unknown')}",
        "Goals: " + " | ".join(record.get("goals") or []),
        "Actions: " + " | ".join(record.get("actions") or []),
        "Results: " + " | ".join(record.get("results") or []),
        "Next: " + " | ".join(record.get("next_steps") or []),
        "Files: " + ", ".join(record.get("files") or []),
        "Sources: " + ", ".join(record.get("source_event_ids") or []),
    ]
    return "\n".join(lines)
