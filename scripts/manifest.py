#!/usr/bin/env python3
"""hubmemory manifest — build ~/hubmemory/MANIFEST.md, the L0 index.

Hardening rules:
  1. Task counts/statuses come from evidence.jsonl records, whose
     status_basis is 'latest_source_claim_unverified' — they are source
     claims, never rendered as verified. Diary content (v3 no-JSON first,
     legacy v2 JSON block accepted, prose as last resort) is the fallback.
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

try:
    import cold_storage as _cold
except ImportError:  # cold archive reader absent: manifest stays hot-only
    _cold = None

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
LIVE_INDEX = HUB_HOME / "live" / "index.json"
TASKS_PATH = HUB_HOME / "live" / "tasks.json"
REQUESTS_DIR = HUB_HOME / "reports" / "requests"
DAILY_DIR = HUB_HOME / "daily"
MONTHLY_DIR = HUB_HOME / "monthly"
MEMORY_PATH = HUB_HOME / "memory" / "memories.jsonl"
MANIFEST_PATH = HUB_HOME / "MANIFEST.md"

JSON_BLOCK_RE = re.compile(r"## Agent-readable summary.*?`{3}json\s*(.*)\n?`{3}\s*$", re.S)
TLDR_RE = re.compile(r"^>\s*\*\*TL;DR\*\*\s*[:：]\s*(.+)$", re.M)
OVERVIEW_RE = re.compile(r"^## 全天总结\s*\n(.*?)(?=^## |\Z)", re.M | re.S)
GOAL_LINE_RE = re.compile(r"^-?\s*\*{0,2}工作目标\*{0,2}\s*[:：]\s*(.+)$", re.M)
STATUS_LINE_RE = re.compile(r"^-?\s*\*{0,2}当前状态\*{0,2}\s*[:：]\s*(.+)$", re.M)
TASK_BLOCK_RE = re.compile(r"^### .*?(?=^### |^## |\Z)", re.S | re.M)
CONCLUSION_LINE_RE = re.compile(r"^-?\s*\*{0,2}任务结论\*{0,2}\s*[:：]\s*(.+)$", re.M)
NEXT_LINE_RE = re.compile(r"^-?\s*\*{0,2}下一步\*{0,2}\s*[:：]\s*(.+)$", re.M)
# v3 assembled report: "## 数据统计（程序生成）" carries
# "- 事件数：N；会话数：N；工作事项：N" and
# "- 状态分布（来源声称，未经独立验证）：已完成 x 项；进行中 y 项…"
STATS_COUNT_RE = re.compile(r"工作事项\s*[:：]\s*(\d+)")
STATS_DIST_RE = re.compile(
    r"状态分布[^：:]*[:：]\s*(.+)$", re.M)
STATS_PAIR_RE = re.compile(r"(已完成|完成|进行中|阻塞|失败)\s*(\d+)\s*项")

STATUS_ICON = {
    "completed": "done", "done": "done",
    "in_progress": "wip",
    "blocked": "blocked",
    "failed": "fail",
}
CN_STATUS = {"已完成": "done", "完成": "done", "进行中": "wip",
             "阻塞": "blocked", "失败": "fail"}


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
    # Drop qualifiers like "完成（来源声称）" before mapping.
    s = re.sub(r"[（(].*?[)）]", "", s).strip()
    return STATUS_ICON.get(s, CN_STATUS.get(s, "?"))


def diary_tldr(text):
    """Second-line `> **TL;DR**:` of any diary format; '' when absent."""
    m = TLDR_RE.search(text or "")
    return clip(m.group(1), 200) if m else ""


def diary_overview(text):
    """Body of `## 全天总结` — the only AI-authored section in v3 diaries."""
    m = OVERVIEW_RE.search(text or "")
    return m.group(1).strip() if m else ""


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
    Legacy v2 '## Agent-readable summary' JSON block wins when present;
    v3 diaries embed no JSON and take the deterministic-prose path."""
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
    statuses, goals, conclusions, loops = {}, [], [], []
    for m in TASK_BLOCK_RE.finditer(text):
        block = m.group(0)
        g = GOAL_LINE_RE.search(block)
        s = STATUS_LINE_RE.search(block)
        c = CONCLUSION_LINE_RE.search(block)
        nxt = NEXT_LINE_RE.search(block)
        if not (g or s or c or nxt):
            continue  # header-only ### (e.g. 会话一览) is not a task
        if g:
            goals.append(clip(g.group(1).split("；")[0], 80))
        if c:
            conclusions.append(clip(c.group(1), 160))
        if nxt:
            loops.append(clip(nxt.group(1).split("；")[0], 80))
        st = norm_status(s.group(1) if s else "?")
        statuses[st] = statuses.get(st, 0) + 1
    if not statuses:
        # v3 assembled report has no task JSON; its program-generated
        # 数据统计 block still carries trustworthy counts.
        dist = STATS_DIST_RE.search(text)
        if dist:
            for label, count in STATS_PAIR_RE.findall(dist.group(1)):
                statuses[norm_status(label)] = int(count)
        count = STATS_COUNT_RE.search(text)
        if count:
            missing = int(count.group(1)) - sum(statuses.values())
            if missing > 0:
                statuses.setdefault("?", missing)
    return {"tasks": sum(statuses.values()), "statuses": statuses,
            "goals": goals, "conclusions": conclusions,
            "open_loops": loops}


def day_evidence(day):
    """evidence.jsonl records for a day (hot file or streamed cold bytes).

    Records carry status_basis='latest_source_claim_unverified'; callers must
    surface them as source claims, not verified facts."""
    path = DAILY_DIR / day / "evidence.jsonl"
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace")
    elif _cold is not None:
        text = _cold.read_day_text(HUB_HOME, day, "evidence.jsonl") or ""
    else:
        return []
    rows = []
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def evidence_summary(records):
    """{tasks,statuses,goals,conclusions,open_loops} from evidence records."""
    statuses, goals, conclusions, loops = {}, [], [], []
    for rec in records:
        st = norm_status(rec.get("status"))
        statuses[st] = statuses.get(st, 0) + 1
        g = (rec.get("goals") or [""])[0]
        if g:
            goals.append(clip(g, 80))
        r = (rec.get("results") or [""])[0]
        if r:
            conclusions.append(clip(r, 160))
        for step in (rec.get("next_steps") or [])[:2]:
            loops.append(clip(step, 80))
    return {"tasks": len(records), "statuses": statuses,
            "goals": goals, "conclusions": conclusions,
            "open_loops": loops}


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


def _day_row(d, age, text, cold):
    """One manifest row: evidence.jsonl first, diary content as fallback.

    Stats from evidence or diary prose are *source claims* — the rendered
    line carries a （来源声称） marker; only a legacy v2 JSON block is
    treated as a self-describing report and left unflagged."""
    ev = day_evidence(d)
    if ev:
        parsed = evidence_summary(ev)
        claims = True
    else:
        parsed = parse_diary(text or "")
        claims = parsed["tasks"] > 0 and _diary_from_json_block(text or "") is None
    row = {"date": d, "age": age, "state": "ok", "cold": cold,
           "tasks": parsed["tasks"], "statuses": parsed["statuses"],
           "goals": parsed["goals"], "conclusions": parsed["conclusions"],
           "open_loops": parsed["open_loops"], "claims": claims}
    if cold:
        row["ref"] = "archive/%s/%s.tar.gz!%s/%s/diary.md" % (d[:4], d[:7], d[:7], d)
    else:
        row["ref"] = "daily/%s/diary.md" % d
    return row


def day_summaries(days=7):
    today = datetime.now().date()
    rows = []
    for i in range(days):
        day = today - timedelta(days=i)
        d = day.isoformat()
        diary = DAILY_DIR / d / "diary.md"
        sessions = DAILY_DIR / d / "sessions.jsonl"
        if not diary.exists() and not sessions.exists():
            # Cold fallback: an archived day inside the window. Reads stream
            # out of the month tarball; nothing is written to disk.
            if _cold is None:
                continue
            text = _cold.read_day_text(HUB_HOME, d, "diary.md")
            has_sessions = _cold.has_day(HUB_HOME, d)
            ev = day_evidence(d)
            if text is None and not has_sessions and not ev:
                continue
            if text is None and not ev:
                rows.append({"date": d, "age": i, "state": "no_diary",
                             "cold": True})
                continue
            rows.append(_day_row(d, i, text or "", True))
            continue
        if not diary.exists():
            ev = day_evidence(d)
            if ev:
                rows.append(_day_row(d, i, "", False))
            else:
                rows.append({"date": d, "age": i, "state": "no_diary"})
            continue
        rows.append(_day_row(d, i,
                             diary.read_text(encoding="utf-8", errors="replace"),
                             False))
    return rows


def archived_months(limit=12):
    """One line per month archive for the manifest — metadata-only
    (verify=False): index.json + already-cached checks, never opens or
    hashes a gzip stream.  Corrupt/unverifiable archives are flagged."""
    if _cold is None:
        return []
    out = []
    for a in _cold.archives(HUB_HOME, verify=False)[:limit]:
        flag = {"ok": "", "unverified": " (unverified)",
                "not_checked": "", "no_index": " (no index)",
                "corrupt": " (CORRUPT — quarantined)"}.get(a["status"], " ?")
        out.append({"month": a["month"], "days": a["day_count"],
                    "status": a["status"], "flag": flag,
                    "ref": "archive/%s/%s.tar.gz" % (a["month"][:4], a["month"])})
    return out


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
        tag = " (archived)" if day.get("cold") else ""
        return "- %s — sessions exist, diary not generated%s" % (day["date"], tag)
    statuses = ", ".join("%s:%s" % (k, v) for k, v in day["statuses"].items())
    claim = "（来源声称）" if day.get("claims") else ""
    ref = "  ref:%s" % day["ref"]
    age = day.get("age", 0)
    if age <= 1:
        goals = "; ".join(day["goals"][:3])
        concl = ("  concl: " + day["conclusions"][0]) if day["conclusions"] else ""
        return "- %s — %s项%s(%s) %s%s%s" % (day["date"], day["tasks"], claim, statuses, goals, concl, ref)
    if age <= 4:
        goals = "; ".join(day["goals"][:2])
        return "- %s — %s项%s(%s) %s%s" % (day["date"], day["tasks"], claim, statuses, goals, ref)
    return "- %s — %s项%s%s" % (day["date"], day["tasks"], claim, ref)


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

    if data.get("archived_months"):
        lines += ["", "## Archived months (cold storage; search transparently, "
                  "extract only on demand)"]
        for m in data["archived_months"]:
            lines.append("- %s — %s days%s  ref:%s"
                         % (m["month"], m["days"], m["flag"], m["ref"]))

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
            "archived_months": archived_months(),
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

