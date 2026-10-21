#!/usr/bin/env python3
"""smoke_test — fast regression checks for hubmemory's invariants.

    python3 smoke_test.py     # exit 0 = all pass
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import hybrid_query as hq  # noqa: E402
import lockutil  # noqa: E402
import manifest  # noqa: E402
import daily_report  # noqa: E402
import report_due  # noqa: E402
import archive as archive_mod  # noqa: E402

FAILURES = []


def check(name, ok, detail=""):
    print("[%s] %s %s" % ("PASS" if ok else "FAIL", name, detail))
    if not ok:
        FAILURES.append(name)


with tempfile.TemporaryDirectory() as td:
    lock_path = Path(td) / ".lock"
    with lockutil.file_lock(lock_path) as fh:
        fh.write(b"x")
    check("lockutil.file_lock", lock_path.exists())

check("lockutil.pid_alive(self)", lockutil.pid_alive(os.getpid()))
check("lockutil.pid_alive(bad)", not lockutil.pid_alive(-1))

toks = hq.tokenize("日报生成 report generation")
check("tokenizer cjk bigram", any(len(t) == 2 for t in toks))
check("tokenizer english lower+plural", "report" in toks and "generation" in toks)

filters, rest = hq.extract_filters("agent:codex date:2026-10-13 日报")
check("filters parsed", filters.get("agent") == "codex" and filters.get("date") == "2026-10-13")
check("filters stripped", "agent:" not in rest and "日报" in rest)

expanded = hq.expand_synonyms(["日记"])
check("synonym expand", "diary" in expanded)

FENCE = chr(96) * 3
DIARY = (
    "# 2026-10-13 工作日记\n"
    "> TL;DR: x\n"
    "### 工作事项 · Session aaa\n"
    "- 工作目标：should be ignored\n- 当前状态：已完成\n"
    "## Agent-readable summary\n\n" + FENCE + "json\n"
    + json.dumps({"date": "2026-10-13", "task_count": 1, "done": 0,
                  "open_loops": ["loop"], "tasks": [
                      {"goal": "real goal", "status": "in_progress",
                       "conclusion": "c"}]}, ensure_ascii=False)
    + "\n" + FENCE + "\n"
)
parsed = manifest.parse_diary(DIARY)
check("manifest prefers json block",
      parsed["goals"] == ["real goal"] and parsed["statuses"].get("wip") == 1,
      str(parsed["goals"]))

parsed2 = manifest.parse_diary(DIARY.split("## Agent-readable summary")[0])
check("manifest prose fallback", parsed2["goals"] == ["should be ignored"])

# v3 diary: AI writes only 「全天总结」; program emits labels, zero JSON.
V3_DIARY = (
    "# 2026-10-20 工作日记\n"
    "> **TL;DR**: 3项任务：1 done 1 wip 1 blocked | 主线: 量化接入\n"
    "## 全天总结\n\n### 主线\n量化接入完成[1]。\n\n## 会话概览\n"
    "### 会话一览\n### 1. 模拟交易\n"
    "- 工作目标: easy-stock 接入\n- 当前状态: 完成（来源声称）\n"
    "- 任务结论: 当日接通\n- 下一步: 补齐历史池\n"
    "### 2. 归档\n"
    "- **工作目标**: 无损归档\n- **当前状态**: blocked\n"
    "- **任务结论**: 冲突保留\n"
)
pv3 = manifest.parse_diary(V3_DIARY)
check("manifest parses v3 no-JSON diary",
      pv3["tasks"] == 2 and pv3["statuses"].get("done") == 1
      and pv3["statuses"].get("blocked") == 1
      and pv3["open_loops"] == ["补齐历史池"],
      str(pv3))
check("v3 overview + tldr extraction",
      "量化接入完成" in manifest.diary_overview(V3_DIARY)
      and "3项任务" in manifest.diary_tldr(V3_DIARY))
check("v3 diary contains no summary JSON",
      "```json" not in V3_DIARY and manifest._diary_from_json_block(V3_DIARY) is None)

check("status icons", manifest.norm_status("已完成") == "done"
      and manifest.norm_status("blocked") == "blocked"
      and manifest.norm_status("???") == "?"
      and manifest.norm_status("完成（来源声称）") == "done")

# --- profile dedupe ---------------------------------------------------------
check("profile key ignores punct/case",
      daily_report._profile_key("不要动数据库。") == daily_report._profile_key("不 要 动 数 据 库!"))
check("profile entry text strips meta",
      daily_report._profile_entry_text("- [2026-10-20 10:00] 不许重启 (session: abcd1234, sha1:deadbeef1234)")
      == "不许重启")

with tempfile.TemporaryDirectory() as td:
    prof = Path(td) / "constraints.md"
    prof.write_text(
        "- [2026-10-19 10:00] 不许重启服务 (session: aaaa, sha1:1111)\n"
        "- [2026-10-20 09:00] 不要动数据库。(session: bbbb, sha1:2222)\n"
        "- [2026-10-20 10:00] 不要动数据库！(session: cccc, sha1:3333)\n",
        encoding="utf-8")
    old_profile_dir = daily_report.PROFILE_DIR
    daily_report.PROFILE_DIR = Path(td)
    try:
        added = daily_report.append_profile(
            [{"text": "不要动数据库。", "ts": "2026-10-20 11:00", "session_id": "dddd"},
             {"text": "新增的约束", "ts": "2026-10-20 12:00", "session_id": "eeee"}],
            "constraints.md")
    finally:
        daily_report.PROFILE_DIR = old_profile_dir
    body = prof.read_text(encoding="utf-8")
    check("profile dedupe keeps unique lines",
          body.count("不要动数据库") == 1 and added == 1 and "新增的约束" in body,
          body.replace("\n", " | "))

# --- draft diary cannot be completed ----------------------------------------
with tempfile.TemporaryDirectory() as td:
    old_hub, old_reqs, old_lock = report_due.HUB_HOME, report_due.REQUESTS_DIR, report_due.LOCK_PATH
    report_due.HUB_HOME = Path(td)
    report_due.REQUESTS_DIR = Path(td) / "reports" / "requests"
    report_due.LOCK_PATH = Path(td) / "reports" / ".lock"
    try:
        day = Path(td) / "daily" / "2026-10-19"
        day.mkdir(parents=True)
        (day / "diary.md").write_text(
            "# 2026-10-19 工作日记（规则草稿，待语义总结）\n> **TL;DR**: x\n", encoding="utf-8")
        report_due.ensure_request("daily", "2026-10-19", [str(day / "diary.md")])
        result = report_due.complete_request("daily", "2026-10-19", "agent", "model", "deterministic")
    finally:
        report_due.HUB_HOME, report_due.REQUESTS_DIR, report_due.LOCK_PATH = old_hub, old_reqs, old_lock
    check("draft diary cannot be marked done",
          not result.get("ok") and result.get("error") in {"report_is_still_draft", "semantic_completion_required"}, str(result))

# --- month archiving: eligibility, lossless round-trip, pending guard --------
from datetime import date as _d

with tempfile.TemporaryDirectory() as td:
    old_home, old_daily, old_arch, old_reqs, old_rlock, old_monthly = (
        archive_mod.HUB_HOME, archive_mod.DAILY_DIR,
        archive_mod.ARCHIVE_DIR, archive_mod.REQUESTS_DIR,
        archive_mod.REPORTS_LOCK, archive_mod.MONTHLY_DIR)
    archive_mod.HUB_HOME = Path(td)
    archive_mod.DAILY_DIR = Path(td) / "daily"
    archive_mod.ARCHIVE_DIR = Path(td) / "archive"
    archive_mod.REQUESTS_DIR = Path(td) / "reports" / "requests"
    archive_mod.REPORTS_LOCK = Path(td) / "reports" / ".lock"
    archive_mod.MONTHLY_DIR = Path(td) / "monthly"
    try:
        # two old days (well outside grace), one pending day in same old month,
        # and a current-month day
        for name, files in {
            "2025-01-03": ["sessions.jsonl", "diary.md"],
            "2025-01-04": ["sessions.jsonl", "diary.md"],
            "2025-02-10": ["sessions.jsonl"],
        }.items():
            dp = archive_mod.DAILY_DIR / name
            dp.mkdir(parents=True)
            for f in files:
                (dp / f).write_text("x" * 50, encoding="utf-8")
            if name.startswith("2025-01"):
                archive_mod.REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
                (archive_mod.REQUESTS_DIR / ("daily-" + name + ".json")).write_text(
                    json.dumps({"kind": "daily", "period": name, "status": "done"}), encoding="utf-8")
        # current month day (not archivable)
        cur = _d.today().strftime("%Y-%m-%d")
        (archive_mod.DAILY_DIR / cur).mkdir(exist_ok=True)

        elig = archive_mod.eligible_months()
        check("archive eligible: old months in, current month out",
              "2025-01" in elig and "2025-02" in elig and _d.today().strftime("%Y-%m") not in elig,
              str(elig))

        # pending request on 2025-02-10 blocks that month only
        old_rd = (report_due.HUB_HOME, report_due.REQUESTS_DIR, report_due.LOCK_PATH)
        report_due.HUB_HOME = Path(td)
        report_due.REQUESTS_DIR = archive_mod.REQUESTS_DIR
        report_due.LOCK_PATH = Path(td) / "reports" / ".lock"
        report_due.ensure_request("daily", "2025-02-10")
        blocked = archive_mod.archive_month("2025-02")
        report_due.HUB_HOME, report_due.REQUESTS_DIR, report_due.LOCK_PATH = old_rd
        check("archive skips month with pending diary",
              blocked.get("error") == "pending_reports", str(blocked))

        result = archive_mod.archive_month("2025-01")
        tar = Path(result.get("archive", ""))
        idx = Path(result.get("index", ""))
        check("month archived losslessly",
              result.get("ok") and tar.exists() and idx.exists()
              and not (archive_mod.DAILY_DIR / "2025-01-03").exists(),
              str(result.get("error")))

        # index content sane
        meta = json.loads(idx.read_text()) if idx.exists() else {}
        check("archive index records days",
              meta.get("day_count") == 2 and meta.get("archive", {}).get("sha256"),
              str(meta.get("day_count")))

        # extract restores files verbatim
        ex = archive_mod.extract_month("2025-01")
        restored = (archive_mod.DAILY_DIR / "2025-01-03" / "sessions.jsonl")
        check("extract round-trip restores bytes",
              ex.get("ok") and restored.exists() and restored.read_text() == "x" * 50,
              str(ex.get("error")))
    finally:
        archive_mod.HUB_HOME, archive_mod.DAILY_DIR = old_home, old_daily
        archive_mod.ARCHIVE_DIR, archive_mod.REQUESTS_DIR = old_arch, old_reqs
        archive_mod.REPORTS_LOCK, archive_mod.MONTHLY_DIR = old_rlock, old_monthly

# --- deterministic fallback diary carries draft marker ----------------------
class _FakeDate:
    def __str__(self): return "2026-10-19"
_d = daily_report.diary_fallback(_FakeDate(), [], {}, [])
check("fallback diary title is draft-marked",
      _d.splitlines()[0].startswith("# 2026-10-19") and "规则草稿" in _d.splitlines()[0],
      _d.splitlines()[0])

print("")
print("checks done, %d failure(s)" % len(FAILURES))
sys.exit(0 if not FAILURES else 1)

