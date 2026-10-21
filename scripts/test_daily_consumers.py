#!/usr/bin/env python3
"""Consumer tests for the v3 daily diary contract (AI = 全天总结 only).

Verifies the files that *read* diaries still work without an embedded
'## Agent-readable summary' JSON block:
  - manifest.py      evidence-first stats, claims marker, cold fallback
  - hub.py           `hub day` = TL;DR + 全天总结 (L1); --full = whole file
  - monthly_report.py prompt digests = TL;DR + overview, never task JSON
  - session_start.py next_action instructs an overview-only candidate
  - backfill.py      deterministic no-JSON draft; contrib.json never born

All fixtures live in a temp HUBMEMORY_HOME. The real ~/hubmemory is never
touched. Python 3.9 compatible (stdlib only).
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
from datetime import date, timedelta
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS))

import archive as arch          # noqa: E402
import backfill                 # noqa: E402
import cold_storage as cold     # noqa: E402
import evidence                 # noqa: E402
import hub                      # noqa: E402
import manifest                 # noqa: E402
import monthly_report           # noqa: E402
import report_due               # noqa: E402
import session_start            # noqa: E402

FENCE = chr(96) * 3
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
COLD_DAY = "2026-08-15"
COLD_MONTH = "2026-08"

V3_DIARY = """# {day} 工作日记
> **TL;DR**: 2项任务 | done:1 blocked:1 | 主线: easy-stock 接入

## 全天总结

### 主线
量化接入当日完成[1]，归档冲突保留。

## 会话概览

### 会话一览
- dsh / model-x

## 任务明细

### 1. easy-stock 接入
- 工作目标: 开盘前接通行情
- 当前状态: 完成（来源声称）
- 任务结论: 当日接通
- 下一步: 补齐历史池
- 来源: session-aaa

### 2. 无损归档
- **工作目标**: 月度归档
- **当前状态**: blocked
- **任务结论**: 冲突日保留双份
"""

V2_DIARY = """# {day} 工作总结
> **TL;DR**: 1项任务 | done:1

## 全天总结

v2 正文概览。

## Agent-readable summary

{fence}json
{{"date": "{day}", "task_count": 1, "done": 1, "open_loops": [],
  "tasks": [{{"goal": "legacy goal", "status": "completed",
             "conclusion": "legacy done", "session": "s1",
             "confidence": "medium", "citations": []}}]}}
{fence}
"""

EVIDENCE_LINE = {
    "id": "evidence_x", "kind": "session_work",
    "period": None, "session_id": "session-aaa",
    "agent": "dsh", "model": "model-x", "cwd": "/repo",
    "goals": ["接入 easy-stock"], "actions": ["连接行情"],
    "results": ["当日接通行情"], "next_steps": ["补齐历史池"],
    "status": "completed",
    "status_basis": "latest_source_claim_unverified",
    "semantic_verified": False, "confidence": 0.65,
    "source_event_ids": ["e1"],
}


def v3_diary(day):
    return V3_DIARY.format(day=day)


def v2_diary(day):
    return V2_DIARY.format(day=day, fence=FENCE)


def ev_line(period, status="completed", goal="接入 easy-stock",
            result="当日接通行情", nxt="补齐历史池", sid="session-aaa"):
    rec = dict(EVIDENCE_LINE, period=period, status=status,
               goals=[goal], results=[result], next_steps=[nxt],
               session_id=sid)
    return json.dumps(rec, ensure_ascii=False)


class ConsumerTest(unittest.TestCase):
    """Temp-home fixture; modules reloaded under HUBMEMORY_HOME."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "daily").mkdir(parents=True)
        cold.clear_caches()
        self._env = os.environ.get("HUBMEMORY_HOME")
        os.environ["HUBMEMORY_HOME"] = str(self.home)
        for mod in (manifest, hub, monthly_report, session_start,
                    report_due, backfill):
            importlib.reload(mod)
        # report_due globals point at tmp too (reload re-reads env).
        for mod in (cold,):
            mod.clear_caches()

    def tearDown(self):
        if self._env is None:
            os.environ.pop("HUBMEMORY_HOME", None)
        else:
            os.environ["HUBMEMORY_HOME"] = self._env
        for mod in (manifest, hub, monthly_report, session_start,
                    report_due, backfill):
            importlib.reload(mod)
        cold.clear_caches()
        self._tmp.cleanup()

    # -- helpers -----------------------------------------------------------

    def mkday(self, day, diary=None, evidence_lines=None,
              sessions=1):
        d = self.home / "daily" / day
        d.mkdir(parents=True, exist_ok=True)
        if sessions is not None:
            events = [{"event_id": "e%d" % i, "session_id": "session-aaa",
                       "agent": "dsh", "model": "model-x", "role": "user",
                       "ts": day + "T10:0%d:00+08:00" % i, "text": "goal %d" % i,
                       "tokens_out": 5, "cwd": "/repo"}
                      for i in range(sessions)]
            (d / "sessions.jsonl").write_text(
                "".join(json.dumps(e, ensure_ascii=False) + "\n"
                        for e in events), encoding="utf-8")
        if diary is not None:
            (d / "diary.md").write_text(diary, encoding="utf-8")
        if evidence_lines:
            (d / "evidence.jsonl").write_text(
                "\n".join(evidence_lines) + "\n", encoding="utf-8")
        return d

    def mk_cold_month(self, month, days):
        """Hand-built tar+index archive: month/YYYY-MM-DD/<files>."""
        year = month[:4]
        adir = self.home / "archive" / year
        adir.mkdir(parents=True, exist_ok=True)
        tar_path = adir / (month + ".tar.gz")
        with tarfile.open(tar_path, "w:gz") as tar:
            for day, files in sorted(days.items()):
                for name, text in sorted(files.items()):
                    data = text.encode("utf-8")
                    member = tarfile.TarInfo("%s/%s/%s" % (month, day, name))
                    member.size = len(data)
                    member.mtime = 0
                    member.mode = 0o644
                    tar.addfile(member, io.BytesIO(data))
        index = {
            "month": month,
            "archive": {"sha256": hashlib.sha256(
                tar_path.read_bytes()).hexdigest()},
            "day_count": len(days),
            "days": [{"date": day, **{name: len(text.encode("utf-8"))
                                      for name, text in files.items()}}
                     for day, files in sorted(days.items())],
        }
        (adir / (month + ".index.json")).write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
        cold.clear_caches()
        return tar_path

    # -- manifest -----------------------------------------------------------

    def test_manifest_evidence_stats_marked_as_claims(self):
        self.mkday(YESTERDAY, diary=v3_diary(YESTERDAY),
                   evidence_lines=[ev_line(YESTERDAY, "completed"),
                                   ev_line(YESTERDAY, "blocked",
                                           goal="归档", result="冲突保留",
                                           nxt="人工处理", sid="session-bbb")])
        rows = manifest.day_summaries(2)
        row = next(r for r in rows if r["date"] == YESTERDAY)
        self.assertEqual(row["tasks"], 2)
        self.assertEqual(row["statuses"], {"done": 1, "blocked": 1})
        self.assertTrue(row["claims"])
        line = manifest._fmt_day(row)
        self.assertIn("来源声称", line)
        self.assertIn("接入 easy-stock", line)
        self.assertIn("补齐历史池", row["open_loops"])

    def test_manifest_no_diary_day_uses_evidence(self):
        self.mkday(YESTERDAY, diary=None,
                   evidence_lines=[ev_line(YESTERDAY, "in_progress",
                                           nxt="继续")])
        rows = manifest.day_summaries(2)
        row = next(r for r in rows if r["date"] == YESTERDAY)
        self.assertEqual(row["state"], "ok")
        self.assertEqual(row["statuses"], {"wip": 1})
        self.assertTrue(row["claims"])

    def test_manifest_legacy_v2_json_not_claim_flag(self):
        self.mkday(YESTERDAY, diary=v2_diary(YESTERDAY), evidence_lines=None)
        rows = manifest.day_summaries(2)
        row = next(r for r in rows if r["date"] == YESTERDAY)
        self.assertEqual(row["tasks"], 1)
        self.assertEqual(row["statuses"], {"done": 1})
        self.assertFalse(row["claims"])
        self.assertEqual(row["goals"], ["legacy goal"])

    def test_parse_diary_v3_prose_labels(self):
        parsed = manifest.parse_diary(v3_diary(YESTERDAY))
        self.assertEqual(parsed["tasks"], 2)
        self.assertEqual(parsed["statuses"].get("done"), 1)
        self.assertEqual(parsed["statuses"].get("blocked"), 1)
        self.assertEqual(parsed["open_loops"], ["补齐历史池"])
        self.assertIsNone(manifest._diary_from_json_block(v3_diary(YESTERDAY)))

    def test_manifest_cold_day_fallback(self):
        self.mk_cold_month(COLD_MONTH, {
            COLD_DAY: {"sessions.jsonl": '{"e":1}\n',
                       "evidence.jsonl": ev_line(COLD_DAY, "in_progress",
                                                 goal="冷任务",
                                                 result="进行中") + "\n",
                       "diary.md": v3_diary(COLD_DAY)}})
        # Force the date into the 7-day window by calling _day_row directly.
        row = manifest._day_row(COLD_DAY, 30, "", True)
        self.assertEqual(row["tasks"], 1)
        self.assertEqual(row["statuses"], {"wip": 1})
        self.assertEqual(row["goals"], ["冷任务"])
        self.assertTrue(row["claims"])
        self.assertIn("archive/2026/2026-08.tar.gz!", row["ref"])

    # -- hub ----------------------------------------------------------------

    def test_hub_day_shows_overview_only(self):
        self.mkday(YESTERDAY, diary=v3_diary(YESTERDAY),
                   evidence_lines=[ev_line(YESTERDAY)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hub.day_view(YESTERDAY)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("TL;DR", out)
        self.assertIn("全天总结", out)
        self.assertIn("量化接入当日完成", out)
        self.assertIn("来源声称", out)
        self.assertNotIn("工作目标", out)       # task details are --full only
        self.assertNotIn("Agent-readable", out)

    def test_hub_day_full_and_legacy_v2(self):
        self.mkday(YESTERDAY, diary=v2_diary(YESTERDAY))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hub.day_view(YESTERDAY)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("v2 正文概览", out)
        self.assertNotIn("```json", out)         # JSON hidden at L1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hub.day_view(YESTERDAY, full=True)
        self.assertEqual(rc, 0)
        self.assertIn("Agent-readable summary", buf.getvalue())

    def test_hub_day_cold_archive(self):
        self.mk_cold_month(COLD_MONTH, {
            COLD_DAY: {"diary.md": v3_diary(COLD_DAY),
                       "sessions.jsonl": '{"e":1}\n'}})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hub.day_view(COLD_DAY)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("量化接入当日完成", out)

    def test_hub_day_missing(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = hub.day_view("2026-01-01")
        self.assertEqual(rc, 1)
        self.assertIn("no diary", buf.getvalue())

    # -- monthly ------------------------------------------------------------

    def test_monthly_digest_uses_overview_not_details(self):
        self.mkday("2026-10-05", diary=v3_diary("2026-10-05"))
        self.mkday("2026-10-06", diary=v2_diary("2026-10-06"))
        digest = monthly_report.load_diaries(2026, 10)
        self.assertIn("=== 2026-10-05 ===", digest)
        self.assertIn("量化接入当日完成", digest)
        self.assertIn("v2 正文概览", digest)
        self.assertNotIn("工作目标", digest)     # task details excluded
        self.assertNotIn("```json", digest)      # no JSON block in prompt

    def test_monthly_digest_cold_day(self):
        self.mk_cold_month(COLD_MONTH, {
            COLD_DAY: {"diary.md": v3_diary(COLD_DAY),
                       "sessions.jsonl": '{"e":1}\n'}})
        digest = monthly_report.load_diaries(2026, 8)
        self.assertIn("=== 2026-08-15 ===", digest)
        self.assertIn("量化接入当日完成", digest)

    def test_monthly_contrib_counts_cold_sessions(self):
        cold_events = "".join(
            json.dumps({"event_id": "c1", "session_id": "s9", "agent": "claude",
                        "model": "m", "role": "user", "tokens_out": 7,
                        "ts": COLD_DAY + "T09:00:00+08:00"}) + "\n")
        self.mk_cold_month(COLD_MONTH, {
            COLD_DAY: {"sessions.jsonl": cold_events}})
        totals = monthly_report.aggregate_contrib(2026, 8)
        self.assertEqual(totals["agents"].get("claude:m"), 7)
        self.assertEqual(totals["total_tokens"], 7)

    # -- session_start ------------------------------------------------------

    def test_next_action_overview_only_candidate(self):
        pending = [{"kind": "daily", "period": "2026-10-19",
                    "status": "pending"}]
        hint = session_start.next_action(pending, {"state": "alive"})
        self.assertIn("全天总结", hint)
        self.assertIn("overview", hint)
        self.assertNotIn("candidate per SKILL.md", hint)

    def test_next_action_monthly_keeps_full_candidate(self):
        pending = [{"kind": "monthly", "period": "2026-09",
                    "status": "pending"}]
        with unittest.mock.patch.object(
                report_due, "monthly_dependencies", return_value=[]):
            hint = session_start.next_action(pending, {"state": "alive"})
        self.assertIn("monthly candidate", hint)

    # -- backfill contract ---------------------------------------------------

    def test_backfill_draft_has_no_json_and_stays_deterministic(self):
        """diary_fallback must produce a marker draft with zero AI detail."""
        target = self.home / "daily" / "2026-08-20"
        target.mkdir(parents=True)
        (target / "sessions.jsonl").write_text(
            json.dumps({"event_id": "b1", "session_id": "s1", "agent": "dsh",
                        "model": "m", "role": "user", "text": "补登任务",
                        "ts": "2026-08-20T10:00:00+08:00"}) + "\n",
            encoding="utf-8")
        events = backfill.load_sessions(date(2026, 8, 20))
        draft = backfill.diary_fallback(date(2026, 8, 20), events, {})
        self.assertIn("规则草稿", draft.splitlines()[0])
        self.assertNotIn("```json", draft)
        self.assertNotIn("Agent-readable summary", draft)
        # No contrib.json anywhere in the fixture.
        self.assertFalse(list(self.home.rglob("contrib.json")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
