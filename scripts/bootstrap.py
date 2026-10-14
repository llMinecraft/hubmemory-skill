#!/usr/bin/env python3
"""bootstrap.py — cross-platform hubmemory installer/health fixer.

Works on macOS / Linux / Windows. Idempotent.
    python3 bootstrap.py            # set everything up
    python3 bootstrap.py --check    # verify only, exit 1 on problems
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR", str(Path(__file__).resolve().parent.parent)))
HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
SCRIPTS = SKILL_DIR / "scripts"
PY = sys.executable

sys.path.insert(0, str(SCRIPTS))
import lockutil  # noqa: E402


def say(msg):
    print("[bootstrap] %s" % msg)


def ensure_dirs():
    for d in ("daily", "monthly", "memory", "profile", "live",
              "state", "logs", "reports/requests"):
        (HUB_HOME / d).mkdir(parents=True, exist_ok=True)
    for f in ("memory/memories.jsonl", "profile/constraints.md",
              "profile/preferences.md"):
        p = HUB_HOME / f
        if not p.exists():
            p.touch()
    say("dirs ready: %s" % HUB_HOME)


def ensure_config():
    cfg = HUB_HOME / "config.yaml"
    if cfg.exists():
        return
    cfg.write_text(
        "version: 1\ntimezone: Asia/Shanghai\n"
        "listen:\n  claude: ~/.claude/projects\n  codex: ~/.codex/sessions\n"
        "skip_content:\n  - thinking\n  - reasoning\n"
        "llm:\n  enabled: false\n"
        "report:\n  daily_hour: 3\n  daily_minute: 7\n"
        "retention:\n  live_after_idle_hours: 24\n"
        "watcher:\n  reconcile_interval_seconds: 15\n",
        encoding="utf-8")
    say("wrote default config.yaml")


def ensure_deps(check_only=False):
    missing = []
    for mod in ("watchdog", "yaml"):
        try:
            __import__(mod)
        except ImportError:
            missing.append("pyyaml" if mod == "yaml" else mod)
    if missing and not check_only:
        say("installing deps: %s" % ", ".join(missing))
        subprocess.check_call([PY, "-m", "pip", "install", "--user", *missing])
    elif missing:
        say("MISSING deps: %s" % ", ".join(missing))
        return False
    return True


def watcher_healthy():
    pid_path = HUB_HOME / "state" / "watcher.pid"
    health_path = HUB_HOME / "state" / "health.json"
    if not pid_path.exists():
        return False
    try:
        pid = int(pid_path.read_text().strip())
    except (ValueError, OSError):
        return False
    if not lockutil.pid_alive(pid):
        return False
    try:
        import json
        from datetime import datetime
        h = json.loads(health_path.read_text(encoding="utf-8"))
        age = (datetime.now().astimezone()
               - datetime.fromisoformat(h["heartbeat_at"]).astimezone()).total_seconds()
        return age <= 90 and h.get("status") == "running"
    except Exception:
        return False


def ensure_watcher(check_only=False):
    if watcher_healthy():
        say("watcher already healthy")
        return True
    if check_only:
        say("watcher not healthy")
        return False
    say("installing scheduler + starting watcher")
    ok = subprocess.call([PY, str(SCRIPTS / "install_scheduler.py")]) == 0
    for _ in range(8):
        if watcher_healthy():
            say("watcher is healthy")
            return True
        time.sleep(1)
    say("WARN: watcher did not come up; check %s" % (HUB_HOME / "logs" / "watcher.log"))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    say("python=%s platform=%s" % (PY.split(os.sep)[-1], sys.platform))
    ensure_dirs()
    ensure_config()
    deps_ok = ensure_deps(args.check)
    watch_ok = ensure_watcher(args.check)
    try:
        import manifest
        manifest.write_manifest()
        say("manifest refreshed")
    except Exception as exc:
        say("manifest skipped: %s" % exc)
    ok = deps_ok and watch_ok
    say("done (%s)" % ("ok" if ok else "issues found - run doctor.py"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

