#!/usr/bin/env python3
"""hubmemory status — 打印当前 watcher 状态、live 会话概览、最近用户约束。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
PID_PATH = HUB_HOME / "state" / "watcher.pid"
INDEX_PATH = HUB_HOME / "live" / "index.json"
CONSTRAINTS_PATH = HUB_HOME / "profile" / "constraints.md"
TASKS_PATH = HUB_HOME / "live" / "tasks.json"
REPORT_REQUESTS_DIR = HUB_HOME / "reports" / "requests"
HEALTH_PATH = HUB_HOME / "state" / "health.json"


def check_pid(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def check_launchd(label: str) -> str:
    try:
        result = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return "loaded"
        return "not loaded"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def format_health(expected_pid: int | None) -> str:
    if not HEALTH_PATH.exists():
        return "missing (watcher has not emitted a heartbeat)"
    try:
        health = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        heartbeat = datetime.fromisoformat(health["heartbeat_at"]).astimezone()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return "unhealthy (health.json unreadable)"
    age = max(0, int((datetime.now().astimezone() - heartbeat).total_seconds()))
    interval = int(health.get("reconcile_interval_seconds") or 15)
    pid_matches = expected_pid is None or health.get("pid") == expected_pid
    healthy = (
        age <= max(90, interval * 4)
        and pid_matches
        and health.get("status") == "running"
        and not health.get("last_error")
    )
    state = "healthy" if healthy else "unhealthy"
    details = f"heartbeat={age}s ago, reconcile={health.get('last_reconcile_files', '?')} file(s)"
    if health.get("last_error"):
        details += f", error={health['last_error']}"
    if not pid_matches:
        details += ", pid mismatch"
    return f"{state} ({details})"


def format_index() -> str:
    if not INDEX_PATH.exists():
        return "  (no live sessions yet)"
    try:
        data = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "  (index.json unreadable)"
    if not data:
        return "  (no live sessions yet)"

    def sort_key(item):
        return item[1].get("last_activity", "")

    lines = []
    for sid, entry in sorted(data.items(), key=sort_key, reverse=True):
        status = entry.get("status", "?")
        agent = entry.get("agent", "?")
        model = entry.get("model", "?")
        cwd = entry.get("cwd", "?")
        last = entry.get("last_activity", "?")[:19].replace("T", " ")
        msgs = entry.get("message_count", 0)
        lines.append(f"  [{status:6}] {agent:6} {model:24} cwd={cwd}")
        lines.append(f"           session={sid[:8]}… last={last} msgs={msgs}")
    return "\n".join(lines)


def format_active_tasks() -> str:
    if not TASKS_PATH.exists():
        return "  (no task declarations)"
    try:
        data = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "  (tasks.json unreadable)"
    tasks = data.get("active_tasks") or []
    if not tasks:
        return "  (no task declarations)"
    now = datetime.now().astimezone()
    lines = []
    for t in sorted(tasks, key=lambda x: x.get("updated_at", ""), reverse=True):
        status = t.get("status", "?")
        agent = t.get("agent", "?")
        task = t.get("task", "?")
        updated = t.get("updated_at", "?")
        needs = t.get("needs_from")
        try:
            dt = datetime.fromisoformat(updated).astimezone()
            delta = int((now - dt).total_seconds())
            age = f"{delta // 60}m ago" if delta < 3600 else f"{delta // 3600}h ago"
        except (ValueError, TypeError):
            age = "?"
        line = f"  [{status:7}] {agent:16} {task} ({age})"
        if needs:
            line += f"\n             needs: {needs}"
        lines.append(line)
    return "\n".join(lines)


def format_recent_constraints(limit: int = 5) -> str:
    if not CONSTRAINTS_PATH.exists():
        return "  (no constraints recorded yet)"
    lines = CONSTRAINTS_PATH.read_text(encoding="utf-8").strip().splitlines()
    lines = [ln for ln in lines if ln.strip().startswith("- ")]
    if not lines:
        return "  (no constraints recorded yet)"
    return "\n".join(f"  {ln}" for ln in lines[-limit:])


def format_pending_reports() -> str:
    if not REPORT_REQUESTS_DIR.exists():
        return "  (no pending reports)"
    pending = []
    for path in sorted(REPORT_REQUESTS_DIR.glob("*.json")):
        try:
            request = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if request.get("status") != "done":
            pending.append(
                f"  [{request.get('status', '?'):7}] {request.get('kind', '?')} {request.get('period', '?')}"
            )
    return "\n".join(pending) if pending else "  (no pending reports)"


def main() -> int:
    print(f"hubmemory home: {HUB_HOME}")

    watcher_pid = None
    if PID_PATH.exists():
        try:
            pid = int(PID_PATH.read_text().strip())
            watcher_pid = pid
            alive = check_pid(pid)
            print(f"watcher pid   : {pid} ({'ALIVE' if alive else 'DEAD (stale pid file)'})")
        except ValueError:
            print("watcher pid   : (pid file corrupted)")
    else:
        print("watcher pid   : (not running)")

    print(f"launchd       : watcher={check_launchd('com.hubmemory.watcher')}, "
          f"dailyreport={check_launchd('com.hubmemory.dailyreport')}")
    print(f"watcher health: {format_health(watcher_pid)}")

    print(f"\nActive task declarations:")
    print(format_active_tasks())

    print(f"\nLive sessions ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}):")
    print(format_index())

    print("\nRecent user constraints (last 5):")
    print(format_recent_constraints())

    print("\nPending reports:")
    print(format_pending_reports())

    return 0


if __name__ == "__main__":
    sys.exit(main())
