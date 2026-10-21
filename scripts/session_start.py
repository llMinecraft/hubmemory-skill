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
    """Inspect all available hot/cold dates, not just a rolling hot week."""
    from report_contract import available_dates, read_daily_text
    enqueued = []
    today_note = None
    today = datetime.now().date().isoformat()
    for day in available_dates(HUB_HOME):
        hot = (DAILY_DIR / day / "sessions.jsonl").exists()
        req = report_due.read_request(report_due.request_path("daily", day))
        if not hot:
            # Index inventory is enough for legacy cold dates; no full-history
            # decompression or retrospective v2 migration during L0 startup.
            from cold_storage import iter_day_files
            inventory = {item["name"] for item in iter_day_files(HUB_HOME, day)}
            if "sessions.jsonl" not in inventory:
                continue
            if "diary.md" in inventory and req.get("contract_version") not in (2, 3):
                continue
        elif not (DAILY_DIR / day / "sessions.jsonl").stat().st_size:
            continue
        if day >= today:
            if day == today:
                today_note = f"today ({today}) is in progress; claim after day rollover"
            continue
        req = report_due.read_request(report_due.request_path("daily", day))
        diary = read_daily_text(HUB_HOME, day, "diary.md")
        if req.get("status") == "done":
            from report_contract import DRAFT_MARKERS, validate_report
            try:
                stale = report_due.completed_snapshot_error(req)
                if stale:
                    raise ValueError(stale)
                if req.get("contract_version") in (2, 3):
                    # v2/v3 reports were published under a checked contract;
                    # revalidation keeps tamper detection. Unversioned
                    # legacy/deterministic done reports only reopen on
                    # missing/draft diaries — never on contract drift.
                    validate_report(diary, "daily", day, HUB_HOME)
                elif not diary.strip() or any(
                        marker.lower() in diary.splitlines()[0].lower()
                        for marker in DRAFT_MARKERS):
                    raise ValueError("missing_or_draft_diary")
                continue
            except ValueError as exc:
                reason = "completed report invalid or missing: " + str(exc)
            result = report_due.reopen_request("daily", day, reason)
            if result.get("ok"):
                enqueued.append(day)
        elif not req:
            day_dir = DAILY_DIR / day
            report_due.ensure_request("daily", day, [str(day_dir / "sessions.jsonl"), str(day_dir / "evidence.jsonl")])
            enqueued.append(day)
    return enqueued, today_note


def next_action(pending, watcher):
    today = datetime.now().date().isoformat()
    claimable = [r for r in pending
                 if ((r.get("status") == "pending") or
                     (r.get("status") == "claimed" and not report_due.lease_active(r)))
                 and (r.get("period", "") < (today if r.get("kind") == "daily" else today[:7]))
                 and (r.get("kind") != "monthly" or not report_due.monthly_dependencies(r["period"]))]
    if claimable:
        # Daily handoff is newest-first: yesterday before historical backfill.
        dailies = [r for r in claimable if r.get("kind") == "daily"]
        first = max(dailies or claimable, key=lambda r: r["period"])
        if first["kind"] == "daily":
            return (f"claim report daily {first['period']} via report_due.py claim "
                    f"--kind daily --period {first['period']} --agent <agent> --model <model> --session <session>; "
                    "keep returned claim_token; write ONLY the 「全天总结」 overview "
                    "candidate (分层小标题 + [n] refs — no task details, stats, or JSON; "
                    "the program assembles the rest), then report_due.py publish "
                    "--kind daily with the same --agent --session --token and --input <overview-file>")
        return (f"claim report monthly {first['period']} via report_due.py claim "
                f"--kind monthly --period {first['period']} --agent <agent> --model <model> --session <session>; "
                "keep returned claim_token; write the monthly candidate per SKILL.md, "
                "then report_due.py publish with the same --agent --session --token and --input <candidate>")
    if pending:
        return "none - reports are actively leased, in-progress periods, or waiting for daily dependencies"
    if watcher["state"] != "alive":
        return "watcher unhealthy (%s) - run bootstrap.sh" % watcher["state"]
    return "none - memory is current"


def historical_backlog(pending):
    """Informational inventory only; never hide, delete, or defer requests."""
    from cold_storage import iter_day_files
    result = []
    for req in pending:
        day = req.get("period", "")
        if req.get("kind") != "daily" or (DAILY_DIR / day / "sessions.jsonl").exists():
            continue
        if any(item.get("name") == "sessions.jsonl" and item.get("source") == "cold"
               for item in iter_day_files(HUB_HOME, day)):
            result.append(day)
    return sorted(result)


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
        "historical_cold_backlog": historical_backlog(pending),
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
    if data.get("historical_cold_backlog"):
        days = data["historical_cold_backlog"]
        print(f"historical cold backlog: {len(days)} requests ({days[0]} → {days[-1]}); preserved pending, newest daily handoff takes priority")
    print("next: %s" % data["next_action"])
    print('retrieval gate: references like 刚才写的/那个方案/另一个会话 → hub search "<user words>" --cwd "$PWD" --budget 800 FIRST; then read artifact. Startup manifest is not a per-turn search.')
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

