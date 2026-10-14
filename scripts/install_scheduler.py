#!/usr/bin/env python3
"""install_scheduler — register the hubmemory watcher with the OS scheduler.

    macOS   -> launchd user agent (plist in ~/Library/LaunchAgents)
    Linux   -> systemd --user service+timer, falls back to cron @reboot
    Windows -> schtasks /Create (runs at logon)

    python3 install_scheduler.py            # install + start
    python3 install_scheduler.py --status   # report only
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(os.environ.get(
    "HUBMEMORY_SKILL_DIR", str(Path(__file__).resolve().parent.parent)))
HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
PY = sys.executable
WATCHER = str(SCRIPTS / "watcher.py") if (SCRIPTS := SKILL_DIR / "scripts") else ""
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


def sh(*cmd, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    kw.setdefault("timeout", 15)
    return subprocess.run(list(cmd), **kw)


# ---------- macOS (launchd) ----------
def install_macos():
    plist = Path.home() / "Library" / "LaunchAgents" / "com.hubmemory.watcher.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    (HUB_HOME / "logs").mkdir(parents=True, exist_ok=True)
    plist.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        "<plist version=\"1.0\"><dict>\n"
        "  <key>Label</key><string>com.hubmemory.watcher</string>\n"
        "  <key>ProgramArguments</key><array>\n"
        "    <string>%s</string><string>%s</string>\n"
        "  </array>\n"
        "  <key>EnvironmentVariables</key><dict>\n"
        "    <key>HUBMEMORY_HOME</key><string>%s</string>\n"
        "    <key>HUBMEMORY_SKILL_DIR</key><string>%s</string>\n"
        "  </dict>\n"
        "  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>\n"
        "  <key>StandardOutPath</key><string>%s</string>\n"
        "  <key>StandardErrorPath</key><string>%s</string>\n"
        "</dict></plist>\n"
        % (PY, WATCHER, HUB_HOME, SKILL_DIR,
           HUB_HOME / "logs" / "watcher.log", HUB_HOME / "logs" / "watcher.err"),
        encoding="utf-8",
    )
    sh("launchctl", "unload", str(plist))
    return sh("launchctl", "load", str(plist)).returncode == 0


# ---------- Linux (systemd --user, then cron) ----------
def install_linux():
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    try:
        unit_dir.mkdir(parents=True, exist_ok=True)
        (unit_dir / "hubmemory-watcher.service").write_text(
            "[Unit]\nDescription=hubmemory watcher\n\n"
            "[Service]\nExecStart=%s %s\nRestart=always\n"
            "Environment=HUBMEMORY_HOME=%s\nEnvironment=HUBMEMORY_SKILL_DIR=%s\n\n"
            "[Install]\nWantedBy=default.target\n"
            % (PY, WATCHER, HUB_HOME, SKILL_DIR),
            encoding="utf-8")
        r1 = sh("systemctl", "--user", "daemon-reload")
        r2 = sh("systemctl", "--user", "enable", "--now", "hubmemory-watcher.service")
        if r1.returncode == 0 and r2.returncode == 0:
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    # cron @reboot fallback
    try:
        cur = sh("crontab", "-l").stdout or ""
        line = "@reboot %s %s >> %s 2>&1" % (PY, WATCHER, HUB_HOME / "logs" / "watcher.log")
        if "hubmemory" not in cur:
            sh("crontab", "-", input=cur + line + "\n")
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


# ---------- Windows (schtasks) ----------
def install_windows():
    return sh(
        "schtasks", "/Create", "/TN", "hubmemory-watcher", "/SC", "ONLOGON",
        "/TR", '"%s" "%s"' % (PY, WATCHER), "/F",
    ).returncode == 0


def status():
    if IS_WINDOWS:
        r = sh("schtasks", "/Query", "/TN", "hubmemory-watcher")
        return r.returncode == 0, "schtasks"
    if IS_MACOS:
        r = sh("launchctl", "list", "com.hubmemory.watcher")
        return r.returncode == 0, "launchd"
    r = sh("systemctl", "--user", "is-active", "hubmemory-watcher.service")
    if r.stdout.strip() == "active":
        return True, "systemd-user"
    r = sh("crontab", "-l")
    if "hubmemory" in (r.stdout or ""):
        return True, "cron"
    return False, "none"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--status", action="store_true")
    args = p.parse_args()
    if args.status:
        ok, how = status()
        print("scheduler: %s (%s)" % ("installed" if ok else "not installed", how))
        return 0 if ok else 1
    ok = install_macos() if IS_MACOS else (install_windows() if IS_WINDOWS else install_linux())
    print("install_scheduler: %s" % ("ok" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

