#!/usr/bin/env python3
"""hubmemory session_start — the single entry point every agent should run.

Called by ~/.codex/AGENTS.md (Codex) and Claude's SessionStart hook.
Deterministic only — it never calls a model. It:

  1. verifies watcher health (reports it, does not fail hard),
  2. detects report gaps (yesterday has sessions but no diary and no
     pending request) and enqueues them,
  3. prints the MANIFEST + pending reports + one clear next-action line.

    python3 session_start.py              # human + agent readable
    python3 session_start.py --json       # machine readable
    python3 session_start.py --budget 400 # cap manifest output size
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR",
    str(Path(__file__).resolve().parent.parent),
))
SCRIPTS = SKILL_DIR / "scripts"
DAILY_DIR = HUB_HOME / "daily"
PID_PATH = HUB_HOME / "state" / "watcher.pid"
HEALTH_PATH = HUB_HOME / "state" / "health.json"
MANIFEST_PATH = HUB_HOME / "MANIFEST.md"

sys.path.insert(0, str(SCRIPTS))
import manifest as manifest_mod  # noqa: E402
import report_due  # noqa: E402


try:
    from lockutil import pid_alive
except ImportError:
    def pid_alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except (OSError, TypeError):
            return False


def watcher_status():
    if not PID_PATH.exists():
        return {"state": "not_running"}
    try:
        pid = int(PID_PATH.read_text().strip())
    except (ValueError, OSError):
        return {"state": "corrupt_pid"}
    if not pid_alive(pid):
        return {"state": "dead", "pid": pid}
    # heartbeat freshness
    try:
        health = json.loads(HEALTH_PATH.read_text(encoding="utf-8"))
        hb = datetime.fromisoformat(health["heartbeat_at"]).astimezone()
        age = int((datetime.now().astimezone() - hb).total_seconds())
        fresh = age <= 90
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        age, fresh = None, False
    return {"state": "alive" if fresh else "stale_heartbeat", "pid": pid, "heartbeat_age_s": age}


def detect_report_gaps():
    """Enqueue requests for days that have sessions but no diary / request."""
    enqueued = []
    today_note = None
    today = datetime.now().date()
    for i in range(1, 8):  # look back a week
        day = today - timedelta(days=i)
        d = day.isoformat()
        day_dir = DAILY_DIR / d
        sessions = day_dir / "sessions.jsonl"
        diary = day_dir / "diary.md"
        if not sessions.exists() or sessions.stat().st_size == 0:
            continue
        req_path = report_due.REQUESTS_DIR / ("daily-%s.json" % d)
        req = report_due.read_request(req_path)
        if diary.exists() and req.get("status") == "done":
            continue
        if req.get("status") in {"pending", "claimed"}:
            continue
        # gap: sessions exist, no diary, no open request -> enqueue
        report_due.ensure_request(
            "daily", d,
            [str(sessions), str(day_dir / "evidence.jsonl")],
        )
        enqueued.append(d)
    # Today is still in progress: surface it, but never make it claimable.
    tdir = DAILY_DIR / today.isoformat()
    tsessions = tdir / "sessions.jsonl"
    if tsessions.exists() and tsessions.stat().st_size > 0 and not (tdir / "diary.md").exists():
        today_note = (
            "today (%s) has sessions but no diary yet - normal; the claimable "
            "request appears after day rollover" % today.isoformat()
        )
    return enqueued, today_note


def next_action(pending, watcher):
    today = datetime.now().date().isoformat()
    claimable = [
        r for r in pending
        if not (r.get("kind") == "daily" and r.get("period") == today)
    ]
    if claimable:
        first = claimable[0]
        return (
            "claim report %s %s via report_due.py claim --kind %s --period %s, "
            "then write %s/%s/%s/%s per SKILL.md diary contract"
            % (first["kind"], first["period"], first["kind"], first["period"],
               HUB_HOME,
               "daily" if first["kind"] == "daily" else "monthly",
               first["period"],
               "diary.md" if first["kind"] == "daily" else (first["period"] + ".md"))
        )
    if pending and not claimable:
        return "none - today's diary is still a draft; leave it for day rollover"
    if watcher["state"] != "alive":
        return "watcher unhealthy (%s) - run bootstrap.sh" % watcher["state"]
    return "none - memory is current"


def gather():
    watcher = watcher_status()
    enqueued, today_note = detect_report_gaps()
    manifest_mod.write_manifest()  # keep L0 fresh on every session start
    pending = manifest_mod.pending_reports()
    return {
        "watcher": watcher,
        "enqueued_gaps": enqueued,
        "today_note": today_note,
        "pending_reports": pending,
        "manifest": str(MANIFEST_PATH),
        "next_action": next_action(pending, watcher),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--budget", type=int, default=0,
                        help="cap MANIFEST bytes; 0 = full")
    args = parser.parse_args()
    data = gather()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    w = data["watcher"]
    print("== hubmemory session_start ==")
    print("watcher: %s%s" % (
        w["state"],
        (" (heartbeat %ss ago)" % w["heartbeat_age_s"]) if w.get("heartbeat_age_s") is not None else "",
    ))
    if data["enqueued_gaps"]:
        print("enqueued missing diaries: %s" % ", ".join(data["enqueued_gaps"]))
    if data["pending_reports"]:
        print("pending reports:")
        for r in data["pending_reports"]:
            who = " by " + r["claimed_by"] if r.get("claimed_by") else ""
            print("  - %s %s [%s]%s" % (r["kind"], r["period"], r["status"], who))
    print("next: %s" % data["next_action"])
    if data.get("today_note"):
        print("note: %s" % data["today_note"])
    print("")
    try:
        text = MANIFEST_PATH.read_text(encoding="utf-8")
        if args.budget and len(text.encode("utf-8")) > args.budget:
            text = text.encode("utf-8")[: args.budget].decode("utf-8", errors="ignore") + "\n…(truncated)\n"
        print(text)
    except OSError:
        print("(MANIFEST.md missing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

