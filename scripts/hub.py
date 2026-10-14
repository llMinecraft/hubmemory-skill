#!/usr/bin/env python3
"""hub — unified hubmemory CLI. One command for humans and agents.

    hub start              session_start check (what agents run on launch)
    hub status             watcher/live/queue overview
    hub today              today's work so far (human-readable)
    hub day 2026-10-13     human-readable day summary (reads diary JSON block)
    hub search <query>     budgeted memory search (wraps context.py)
    hub query <query>      full-record search (wraps query.py)
    hub doctor             health checks
    hub init               bootstrap watcher + scheduler for this OS
    hub manifest           print MANIFEST.md
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR", str(Path(__file__).resolve().parent.parent)))
SCRIPTS = SKILL_DIR / "scripts"
PY = sys.executable or "python3"

JSON_BLOCK_RE = re.compile(r"## Agent-readable summary.*?`{3}json\s*(.*)\n?`{3}\s*$", re.S)


def run(script, *args):
    return subprocess.call([PY, str(SCRIPTS / script), *args])


def _read_diary_json(day):
    path = HUB_HOME / "daily" / day / "diary.md"
    if not path.exists():
        return None
    m = JSON_BLOCK_RE.search(path.read_text(encoding="utf-8", errors="replace"))
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def day_view(day):
    """Human-readable day summary from the machine block (no model call)."""
    data = _read_diary_json(day)
    if not data:
        diary = HUB_HOME / "daily" / day / "diary.md"
        if diary.exists():
            print(diary.read_text(encoding="utf-8"))
        else:
            print("%s: no diary. sessions exist? %s"
                  % (day, (HUB_HOME / "daily" / day / "sessions.jsonl").exists()))
        return 1
    done = data.get("done", 0)
    print("# %s — %d tasks, %d done" % (day, data.get("task_count", 0), done))
    for i, t in enumerate(data.get("tasks") or [], 1):
        icon = {"completed": "done", "blocked": "BLOCKED",
                "failed": "FAIL", "in_progress": "wip"}.get(t.get("status"), "?")
        print("\n%d. [%s] %s" % (i, icon, t.get("goal") or "(no goal)"))
        print("   conclusion: %s" % (t.get("conclusion") or "-"))
        nxt = t.get("next") or []
        if nxt:
            print("   next: %s" % nxt[0])
        print("   agent: %s | session: %s | conf: %s"
              % (t.get("agent"), (t.get("session") or "")[:8], t.get("confidence")))
    loops = data.get("open_loops") or []
    if loops:
        print("\nOpen loops:")
        for l in loops:
            print("  - %s" % l)
    return 0


def main():
    if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}:
        print(__doc__.strip())
        return 0
    cmd, rest = sys.argv[1], sys.argv[2:]

    if cmd == "start":
        return run("session_start.py", *rest)
    if cmd == "status":
        return run("status.py", *rest)
    if cmd == "today":
        return day_view(datetime.now().date().isoformat())
    if cmd == "day":
        return day_view(rest[0] if rest else datetime.now().date().isoformat())
    if cmd == "search":
        return run("context.py", *rest) if rest else print("usage: hub search <query> [--budget N]")
    if cmd == "query":
        return run("query.py", *rest) if rest else print("usage: hub query <query>")
    if cmd == "doctor":
        return run("doctor.py", *rest)
    if cmd == "init":
        target = SCRIPTS / ("bootstrap.ps1" if os.name == "nt" else "bootstrap.sh")
        if os.name == "nt":
            return subprocess.call(["powershell", "-ExecutionPolicy", "Bypass",
                                    "-File", str(target), *rest])
        return subprocess.call(["bash", str(target), *rest])
    if cmd == "manifest":
        p = HUB_HOME / "MANIFEST.md"
        print(p.read_text(encoding="utf-8") if p.exists() else "(missing - run: hub start)")
        return 0
    if cmd == "claim":
        return run("report_due.py", "claim", *rest)
    print("unknown command: %s\n" % cmd)
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

