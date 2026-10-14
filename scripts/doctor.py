#!/usr/bin/env python3
"""hubmemory doctor — one-command health report.

Checks watcher, scheduler, locks, stale claims, manifest freshness,
jsonl integrity, and dependency availability. Exit 0 = all pass.

    python3 doctor.py [--json]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR", str(Path(__file__).resolve().parent.parent)))
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"

try:
    import lockutil
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import lockutil

CHECKS = []


def check(name, ok, detail="", warn=False):
    CHECKS.append({"name": name, "ok": bool(ok), "warn": bool(warn), "detail": detail})


def scheduler_state():
    """Return (installed, detail) for whatever scheduler this OS uses."""
    if IS_WINDOWS:
        try:
            out = subprocess.run(["schtasks", "/Query", "/TN", "hubmemory-watcher"],
                                 capture_output=True, text=True, timeout=5)
            return out.returncode == 0, "schtasks"
        except (OSError, subprocess.SubprocessError):
            return False, "schtasks unavailable"
    if IS_MACOS:
        try:
            ok = subprocess.run(["launchctl", "list", "com.hubmemory.watcher"],
                                capture_output=True, timeout=5).returncode == 0
            return ok, "launchd"
        except (OSError, subprocess.SubprocessError):
            return False, "launchd unavailable"
    # linux: prefer systemd user unit, else cron
    try:
        ok = subprocess.run(
            ["systemctl", "--user", "is-active", "hubmemory-watcher.service"],
            capture_output=True, text=True, timeout=5).stdout.strip() == "active"
        if ok:
            return True, "systemd-user"
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        out = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
        if "hubmemory" in out.stdout:
            return True, "cron"
    except (OSError, subprocess.SubprocessError):
        pass
    return False, "no scheduler found (systemd-user/cron)"


def check_watcher():
    pid_path = HUB_HOME / "state" / "watcher.pid"
    health_path = HUB_HOME / "state" / "health.json"
    if not pid_path.exists():
        check("watcher", False, "not running (no pid file)")
        return
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        check("watcher", False, "corrupt pid file")
        return
    if not lockutil.pid_alive(pid):
        check("watcher", False, "pid %d dead (stale pid file)" % pid)
        return
    try:
        h = json.loads(health_path.read_text(encoding="utf-8"))
        age = int((datetime.now().astimezone()
                   - datetime.fromisoformat(h["heartbeat_at"]).astimezone()).total_seconds())
        if age > 90:
            check("watcher", False, "alive but heartbeat %ds stale" % age)
        elif h.get("last_error"):
            check("watcher", False, "last_error: %s" % h["last_error"])
        else:
            check("watcher", True, "pid %d, heartbeat %ds ago" % (pid, age))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        check("watcher", False, "pid alive but health.json unreadable", warn=True)


def check_scheduler():
    ok, detail = scheduler_state()
    check("scheduler", ok, detail)


def check_claims():
    req_dir = HUB_HOME / "reports" / "requests"
    stuck, pending = [], []
    if req_dir.exists():
        for p in sorted(req_dir.glob("*.json")):
            try:
                r = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stuck.append(p.name + "(unreadable)")
                continue
            if r.get("status") == "claimed":
                try:
                    age_h = (datetime.now().astimezone()
                             - datetime.fromisoformat(r["claimed_at"]).astimezone()).total_seconds() / 3600
                    if age_h > 6:
                        stuck.append("%s claimed %.1fh ago by %s"
                                     % (r.get("id"), age_h, r.get("claimed_by")))
                except (KeyError, ValueError, TypeError):
                    pass
            elif r.get("status") == "pending":
                pending.append(r.get("id"))
    if stuck:
        check("claims", False, "; ".join(stuck))
    elif pending:
        check("claims", True, "%d pending: %s" % (len(pending), ", ".join(pending)))
    else:
        check("claims", True, "queue clean")


def check_manifest():
    p = HUB_HOME / "MANIFEST.md"
    if not p.exists():
        check("manifest", False, "missing - run session_start.py")
        return
    age = int((datetime.now().timestamp() - p.stat().st_mtime) / 60)
    check("manifest", age < 60 * 24, "%d bytes, updated %dmin ago" % (p.stat().st_size, age),
          warn=age >= 60 * 24)


def check_jsonl():
    bad = []
    root = HUB_HOME / "daily"
    today = datetime.now().date()
    scanned = 0
    for i in range(0, 8):
        d = (today - timedelta(days=i)).isoformat()
        for name in ("sessions.jsonl", "evidence.jsonl"):
            p = root / d / name
            if not p.exists():
                continue
            scanned += 1
            with p.open(encoding="utf-8") as fh:
                for n, line in enumerate(fh, 1):
                    try:
                        json.loads(line)
                    except json.JSONDecodeError:
                        bad.append("%s:%d" % (name, n))
                        break
    check("jsonl_integrity", not bad,
          "%d files clean" % scanned if not bad else "corrupt lines: " + ", ".join(bad[:5]))


def check_deps():
    missing = []
    for mod in ("watchdog", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    check("python_deps", not missing,
          "ok" if not missing else "missing: " + ", ".join(missing))


def check_gaps():
    today = datetime.now().date()
    gaps = []
    for i in range(1, 8):
        d = (today - timedelta(days=i)).isoformat()
        day = HUB_HOME / "daily" / d
        if (day / "sessions.jsonl").exists() and not (day / "diary.md").exists():
            gaps.append(d)
    check("diary_gaps", not gaps,
          "none" if not gaps else "sessions without diary: " + ", ".join(gaps))


def main():
    check_watcher()
    check_scheduler()
    check_claims()
    check_manifest()
    check_jsonl()
    check_deps()
    check_gaps()

    if "--json" in sys.argv:
        print(json.dumps(CHECKS, ensure_ascii=False, indent=2))
        return 0 if all(c["ok"] for c in CHECKS) else 1

    print("hubmemory doctor")
    print("home: %s | platform: %s" % (HUB_HOME, "windows" if IS_WINDOWS else sys.platform))
    for c in CHECKS:
        icon = "PASS" if c["ok"] and not c["warn"] else ("WARN" if c["ok"] or c["warn"] else "FAIL")
        print("[%s] %-16s %s" % (icon, c["name"], c["detail"]))
    fails = [c for c in CHECKS if not c["ok"]]
    print("")
    print("%d/%d checks pass%s" % (
        len(CHECKS) - len(fails), len(CHECKS),
        "" if not fails else " - run session_start.py or bootstrap to repair"))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())

