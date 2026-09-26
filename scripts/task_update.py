#!/usr/bin/env python3
"""hubmemory task_update — agent 主动声明当前任务状态，实现跨 agent 协调。

用法：
    # 开始任务
    python3 task_update.py --agent claude --task "重构 documents router" --status active

    # 完成任务
    python3 task_update.py --agent claude --task "重构 documents router" --status done

    # 列出当前所有活跃任务
    python3 task_update.py --list

    # 清除某 agent 的任务声明
    python3 task_update.py --agent claude --clear
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
TASKS_PATH = HUB_HOME / "live" / "tasks.json"
LIVE_DIR = HUB_HOME / "live"


def load_tasks() -> dict:
    if not TASKS_PATH.exists():
        return {"active_tasks": []}
    try:
        return json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"active_tasks": []}


def save_tasks(data: dict) -> None:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TASKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(TASKS_PATH)


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def upsert_task(agent: str, task: str, status: str, session_id: str | None, cwd: str | None, needs_from: str | None) -> None:
    data = load_tasks()
    tasks = data["active_tasks"]
    # 找同一 agent 的现有任务条目
    existing = next((t for t in tasks if t.get("agent") == agent), None)
    entry = {
        "agent": agent,
        "session_id": session_id or "",
        "task": task,
        "status": status,
        "needs_from": needs_from,
        "cwd": cwd or str(Path.cwd()),
        "updated_at": now_iso(),
    }
    if existing:
        existing.update(entry)
    else:
        tasks.append(entry)
    save_tasks(data)
    print(f"[{now_iso()}] task updated: {agent} -> {task!r} ({status})")


def clear_agent(agent: str) -> None:
    data = load_tasks()
    before = len(data["active_tasks"])
    data["active_tasks"] = [t for t in data["active_tasks"] if t.get("agent") != agent]
    save_tasks(data)
    print(f"cleared {before - len(data['active_tasks'])} task(s) for agent '{agent}'")


def list_tasks() -> None:
    data = load_tasks()
    tasks = data.get("active_tasks") or []
    if not tasks:
        print("(no active task declarations)")
        return
    now = datetime.now().astimezone()
    for t in sorted(tasks, key=lambda x: x.get("updated_at", ""), reverse=True):
        updated = t.get("updated_at", "?")
        age_str = ""
        try:
            dt = datetime.fromisoformat(updated).astimezone()
            delta = int((now - dt).total_seconds())
            if delta < 60:
                age_str = f" ({delta}s ago)"
            elif delta < 3600:
                age_str = f" ({delta // 60}m ago)"
            else:
                age_str = f" ({delta // 3600}h ago)"
        except (ValueError, TypeError):
            pass
        status = t.get("status", "?")
        agent = t.get("agent", "?")
        task = t.get("task", "?")
        cwd = t.get("cwd", "")
        needs = t.get("needs_from")
        print(f"[{status:6}] {agent:16} {task}")
        if cwd:
            print(f"         cwd: {cwd}")
        if needs:
            print(f"         needs: {needs}")
        print(f"         updated: {updated}{age_str}")
        sid = t.get("session_id")
        if sid:
            print(f"         session: {sid[:16]}…")
        print()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agent", type=str, help="agent 名称（如 claude / qclaw / codex-desktop）")
    parser.add_argument("--task", type=str, help="任务描述（一句话）")
    parser.add_argument("--status", type=str, choices=["active", "done", "blocked", "paused"], help="任务状态")
    parser.add_argument("--session", type=str, help="当前 session ID（可选）")
    parser.add_argument("--cwd", type=str, help="工作目录（默认当前目录）")
    parser.add_argument("--needs-from", type=str, help="需要哪个 agent 配合（如 'claude: 请帮我写后端接口'）")
    parser.add_argument("--list", action="store_true", help="列出当前所有任务声明")
    parser.add_argument("--clear", action="store_true", help="清除该 agent 的任务声明（配合 --agent）")
    args = parser.parse_args()

    if args.list:
        list_tasks()
        return 0

    if not args.agent:
        parser.error("--agent is required unless using --list")

    if args.clear:
        clear_agent(args.agent)
        return 0

    if not args.task or not args.status:
        parser.error("--task and --status are required to update a task")

    upsert_task(
        agent=args.agent,
        task=args.task,
        status=args.status,
        session_id=args.session,
        cwd=args.cwd,
        needs_from=args.needs_from,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
