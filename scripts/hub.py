#!/usr/bin/env python3
"""hub — unified hubmemory CLI. One command for humans and agents.

    hub start              session_start check (what agents run on launch)
    hub status             watcher/live/queue overview
    hub today              today's work so far (TL;DR + 全天总结)
    hub day 2026-10-13     TL;DR + 全天总结 (L1); add --full for the whole diary
    hub search <query>     budgeted memory search (wraps context.py)
    hub query <query>      full-record search (wraps query.py)
    hub read --date D --session ID [--event ID] [--budget N]
                           bounded L2 exchange with exact hot/cold provenance
    hub doctor             health checks
    hub init               bootstrap watcher + scheduler for this OS
    hub manifest           print MANIFEST.md
    hub archive            pack eligible past months (--auto | --month | extract | list)
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

sys.path.insert(0, str(SCRIPTS))
try:
    import cold_storage as _cold
except ImportError:  # cold archive reader absent
    _cold = None

JSON_BLOCK_RE = re.compile(r"## Agent-readable summary.*?`{3}json\s*(.*)\n?`{3}\s*$", re.S)


def run(script, *args):
    return subprocess.call([PY, str(SCRIPTS / script), *args])


def _read_diary_text(day):
    """diary.md for a day: hot file, else streamed from the month archive."""
    path = HUB_HOME / "daily" / day / "diary.md"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    if _cold is not None:
        return _cold.read_day_text(HUB_HOME, day, "diary.md")
    return None


def _day_stats(day):
    """Deterministic counts from evidence.jsonl, else the diary itself.
    Returns (task_count, status_counts); all source claims, not verified."""
    statuses = {}
    try:
        sys.path.insert(0, str(SCRIPTS))
        import manifest as _manifest
        recs = _manifest.day_evidence(day)
        if recs:
            for r in recs:
                k = _manifest.norm_status(r.get("status"))
                statuses[k] = statuses.get(k, 0) + 1
            return len(recs), statuses
        parsed = _manifest.parse_diary(_read_diary_text(day) or "")
        return parsed["tasks"], parsed["statuses"]
    except Exception:
        return 0, statuses


def day_view(day, full=False):
    """L1 view: TL;DR + `## 全天总结` (+ deterministic counts). --full for all.

    No embedded JSON block is required (v3 diaries carry none); v2 diaries
    display identically. Archived days stream from the month tarball —
    nothing is extracted to disk. Counts are source claims (来源声称),
    not verified completion."""
    text = _read_diary_text(day)
    if text is None:
        sessions = (HUB_HOME / "daily" / day / "sessions.jsonl").exists()
        if not sessions and _cold is not None:
            sessions = _cold.has_day(HUB_HOME, day)
        print("%s: no diary. sessions exist? %s" % (day, sessions))
        return 1
    if full:
        print(text)
        return 0
    tldr = overview = ""
    try:
        import manifest as _manifest
        tldr = _manifest.diary_tldr(text)
        overview = _manifest.diary_overview(text)
    except Exception:
        pass
    total, statuses = _day_stats(day)
    status_str = ", ".join("%s:%s" % kv for kv in statuses.items())
    print("# %s — %s项%s（来源声称）" % (day, total,
          "(" + status_str + ")" if status_str else ""))
    if tldr:
        print("\n> TL;DR: %s" % tldr)
    if overview:
        print("\n## 全天总结\n\n%s" % overview)
    if not tldr and not overview:
        # Unknown format: show head, not a full dump.
        print("\n".join(text.splitlines()[:40]))
        print("\n… (truncated — use --full for the complete diary)")
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
        return day_view(datetime.now().date().isoformat(),
                        full="--full" in rest)
    if cmd == "day":
        full = "--full" in rest
        day_arg = next((a for a in rest if not a.startswith("-")), None)
        return day_view(day_arg or datetime.now().date().isoformat(), full=full)
    if cmd == "search":
        return run("context.py", *rest) if rest else print("usage: hub search <query> [--budget N]")
    if cmd == "read":
        return run("recall_read.py", *rest)
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
    if cmd == "archive":
        return run("archive.py", *rest) if rest else run("archive.py", "--auto")
    if cmd == "claim":
        return run("report_due.py", "claim", *rest)
    print("unknown command: %s\n" % cmd)
    print(__doc__.strip())
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

