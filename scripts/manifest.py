#!/usr/bin/env python3
"""hubmemory manifest — build ~/hubmemory/MANIFEST.md, the L0 index.

Hardening rules:
  1. Prefer the diary's '## Agent-readable summary' JSON block over regexing
     prose; prose labels are fallback only (survives language/label drift).
  2. Resolution decays with distance: yesterday gets goals, the last few days
     one line each, older days collapse into a monthly bucket line.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
LIVE_INDEX = HUB_HOME / "live" / "index.json"
TASKS_PATH = HUB_HOME / "live" / "tasks.json"
REQUESTS_DIR = HUB_HOME / "reports" / "requests"
DAILY_DIR = HUB_HOME / "daily"
MONTHLY_DIR = HUB_HOME / "monthly"
MEMORY_PATH = HUB_HOME / "memory" / "memories.jsonl"
MANIFEST_PATH = HUB_HOME / "MANIFEST.md"

JSON_BLOCK_RE = re.compile(r"## Agent-readable summary.*?`{3}json\s*(.*)\n?`{3}\s*$", re.S)
GOAL_LINE_RE = re.compile(r"^- 工作目标：(.+)$", re.M)
STATUS_LINE_RE = re.compile(r"^- 当前状态：(.+)$", re.M)
TASK_BLOCK_RE = re.compile(r"### 工作事项.*?(?=### |## |$)", re.S)
CONCLUSION_LINE_RE = re.compile(r"^- 任务结论：(.+)$", re.M)

STATUS_ICON = {
    "completed": "done", "done": "done",
    "in_progress": "wip",
    "blocked": "blocked",
    "failed": "fail",
}
CN_STATUS = {"已完成": "done", "进行中": "wip", "阻塞": "blocked", "失败": "fail"}


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def clip(text, limit=120):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def norm_status(raw):
    s = str(raw or "?").strip()
    return STATUS_ICON.get(s, CN_STATUS.get(s, "?"))


def _diary_from_json_block(text):
    m = JSON_BLOCK_RE.search(text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def parse_diary(text):
    """Return {tasks, statuses, goals, conclusions, open_loops}.
    JSON block wins; prose regex is the fallback."""
    js = _diary_from_json_block(text)
    if js and isinstance(js.get("tasks"), list):
        statuses, goals, conclusions = {}, [], []
        for t in js["tasks"]:
            s = norm_status(t.get("status"))
            statuses[s] = statuses.get(s, 0) + 1
            if t.get("goal"):
                goals.append(clip(str(t["goal"]), 80))
            if t.get("conclusion"):
                conclusions.append(clip(str(t["conclusion"]), 160))
        return {"tasks": len(js["tasks"]), "statuses": statuses,
                "goals": goals, "conclusions": conclusions,
                "open_loops": js.get("open_loops") or []}
    statuses, goals, conclusions = {}, [], []
    for m in TASK_BLOCK_RE.finditer(text):
        block = m.group(0)
        g = GOAL_LINE_RE.search(block)
        s = STATUS_LINE_RE.search(block)
        c = CONCLUSION_LINE_RE.search(block)
        if g:
            goals.append(clip(g.group(1).split("；")[0], 80))
        if c:
            conclusions.append(clip(c.group(1), 160))
        st = norm_status(s.group(1) if s else "?")
        statuses[st] = statuses.get(st, 0) + 1
    return {"tasks": sum(statuses.values()), "statuses": statuses,
            "goals": goals, "conclusions": conclusions, "open_loops": []}


INTENT_LINE_RE = re.compile(r"^- intent:\s*(.+)$", re.M)


def _live_md_intent(agent, sid):
    """Fallback: read the intent line watcher stamps on live/*.md."""
    live_dir = HUB_HOME / "live"
    path = live_dir / ("%s_%s.md" % (str(agent).replace("/", "-"), sid))
    try:
        m = INTENT_LINE_RE.search(path.read_text(encoding="utf-8"))
    except OSError:
        return ""
    if not m:
        return ""
    text = m.group(1).strip()
    return "" if text.startswith("(no user goal") else text


def live_sessions():
    data = read_json(LIVE_INDEX, {})
    rows = []
    for sid, entry in data.items():
        rows.append({
            "session_id": sid,
            "agent": entry.get("agent", "?"),
            "model": entry.get("model", "?"),
            "cwd": entry.get("cwd", ""),
            "status": entry.get("status", "?"),
            "last_activity": (entry.get("last_activity") or "")[:16].replace("T", " "),
            "messages": entry.get("message_count", 0),
            "intent": clip(
                entry.get("intent") or _live_md_intent(entry.get("agent"), sid), 90),
            "current": clip(entry.get("last_user") or "", 90),
        })
    rows.sort(key=lambda r: r["last_activity"], reverse=True)
    return rows


def pending_reports():
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for path in sorted(REQUESTS_DIR.glob("*.json")):
        req = read_json(path, {})
        if req and req.get("status") != "done":
            out.append({"kind": req.get("kind"), "period": req.get("period"),
                        "status": req.get("status"), "claimed_by": req.get("claimed_by")})
    return out


def day_summaries(days=7):
    today = datetime.now().date()
    rows = []
    for i in range(days):
        day = today - timedelta(days=i)
        d = day.isoformat()
        diary = DAILY_DIR / d / "diary.md"
        sessions = DAILY_DIR / d / "sessions.jsonl"
        if not diary.exists() and not sessions.exists():
            continue
        if not diary.exists():
            rows.append({"date": d, "age": i, "state": "no_diary"})
            continue
        parsed = parse_diary(diary.read_text(encoding="utf-8", errors="replace"))
        rows.append({"date": d, "age": i, "state": "ok",
                     "tasks": parsed["tasks"], "statuses": parsed["statuses"],
                     "goals": parsed["goals"], "conclusions": parsed["conclusions"],
                     "open_loops": parsed["open_loops"],
                     "ref": "daily/" + d + "/diary.md"})
    return rows


def open_loops(limit=8):
    loops = []
    for t in (read_json(TASKS_PATH, {}).get("active_tasks") or []):
        if t.get("status") not in {"done", "completed", "archived"}:
            loops.append("%s [%s]" % (t.get("task", "?"), t.get("status")))
    for r in pending_reports():
        loops.append("%s report %s is %s" % (r["kind"], r["period"], r["status"]))
    for day in day_summaries(7):
        for l in day.get("open_loops") or []:
            loops.append("%s: %s" % (day["date"], clip(l, 80)))
    return loops[:limit]


def memory_index(limit=10):
    if not MEMORY_PATH.exists():
        return []
    rows = []
    for line in MEMORY_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") in {"superseded", "stale", "archived"}:
            continue
        rows.append({"id": rec.get("id"), "type": rec.get("type", "fact"),
                     "title": rec.get("title", ""),
                     "updated": (rec.get("updated_at") or "")[:10]})
    rows.sort(key=lambda r: r["updated"], reverse=True)
    return rows[:limit]


def _fmt_day(day):
    """Render a day line with resolution decaying by age."""
    if day["state"] == "no_diary":
        return "- %s — sessions exist, diary not generated" % day["date"]
    statuses = ", ".join("%s:%s" % (k, v) for k, v in day["statuses"].items())
    ref = "  ref:%s" % day["ref"]
    age = day.get("age", 0)
    if age <= 1:
        goals = "; ".join(day["goals"][:3])
        concl = ("  concl: " + day["conclusions"][0]) if day["conclusions"] else ""
        return "- %s — %s项(%s) %s%s%s" % (day["date"], day["tasks"], statuses, goals, concl, ref)
    if age <= 4:
        goals = "; ".join(day["goals"][:2])
        return "- %s — %s项(%s) %s%s" % (day["date"], day["tasks"], statuses, goals, ref)
    return "- %s — %s项%s" % (day["date"], day["tasks"], ref)


def render(data):
    lines = [
        "<!-- Auto-generated by manifest.py. Read this FIRST (~450 tokens).",
        "     Pointers like daily/YYYY-MM-DD/diary.md are relative to ~/hubmemory/. -->",
        "",
        "# hubmemory manifest",
        "",
        "## Right now (live sessions)",
    ]
    live = [s for s in data["live_sessions"] if s["status"] == "active"]
    shown = live if live else data["live_sessions"][:3]
    for s in shown:
        name = Path(s["cwd"]).name or s["cwd"] or "?"
        head = "- %s(%s) %s last=%s cwd=%s"                % (s["agent"], s["model"], s["status"], s["last_activity"], name)
        intent, current = s.get("intent"), s.get("current")
        if intent and current and current != intent:
            head += " — 起点:%s | 当前:%s" % (intent[:50], current[:50])
        elif intent or current:
            head += " — %s" % (intent or current)
        lines.append(head)
    if not shown:
        lines.append("- (no live sessions)")

    lines += ["", "## Pending reports (claim me)"]
    today = datetime.now().date().isoformat()
    if data["pending_reports"]:
        for r in data["pending_reports"]:
            who = " by " + r["claimed_by"] if r.get("claimed_by") else ""
            draft = " (today - draft, claimable after rollover)"                 if r["kind"] == "daily" and r["period"] == today else ""
            lines.append("- %s %s [%s]%s%s" % (r["kind"], r["period"], r["status"], who, draft))
    else:
        lines.append("- none")

    lines += ["", "## Last 7 days (resolution decays with distance)"]
    for day in data["days"]:
        lines.append(_fmt_day(day))

    if data["open_loops"]:
        lines += ["", "## Open loops"]
        lines += ["- " + loop for loop in data["open_loops"]]

    if data["memories"]:
        lines += ["", "## Long-term memory index (title only; body in memory/memories.jsonl)"]
        for m in data["memories"]:
            lines.append("- [%s] %s (%s)" % (m["type"], m["title"], m["updated"]))

    lines += ["", "<!-- Detail: hub search '<q>' | context.py <q> --budget N | read daily/<date>/diary.md -->"]
    return "\n".join(lines) + "\n"


def build():
    return {"generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "live_sessions": live_sessions(), "pending_reports": pending_reports(),
            "open_loops": open_loops(), "days": day_summaries(7),
            "memories": memory_index()}


def write_manifest():
    MANIFEST_PATH.write_text(render(build()), encoding="utf-8")
    return MANIFEST_PATH


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = build()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        path = write_manifest()
        print("wrote %s (%d bytes)" % (path, path.stat().st_size))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

