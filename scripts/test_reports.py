#!/usr/bin/env python3
"""Isolated report regression tests. Never access the real hubmemory home."""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parent


class ReportsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hubmemory-reports-test-")
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"HUBMEMORY_HOME": str(self.home), "HUBMEMORY_SKILL_DIR": str(SCRIPTS.parent), "DSH_SESSION_ID": "test-session"})
        self.env.start()
        import report_due, daily_report, monthly_report, backfill, session_start
        self.due = importlib.reload(report_due)
        self.daily = importlib.reload(daily_report)
        self.monthly = importlib.reload(monthly_report)
        self.backfill = importlib.reload(backfill)
        self.start = importlib.reload(session_start)
        self.day = "2020-01-02"
        self.source(self.day)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def source(self, day):
        root = self.home / "daily" / day
        root.mkdir(parents=True, exist_ok=True)
        (root / "sessions.jsonl").write_text(json.dumps({"session_id": "session-full-id", "event_id": "evt-1", "role": "assistant", "agent": "dsh", "text": "已完成修复并通过隔离测试", "ts": day + "T12:00:00+08:00"}) + "\n", encoding="utf-8")

    def semantic(self, kind="daily", period=None, **updates):
        period = period or self.day
        task = {"goal": "修复报告租约", "status": "completed", "conclusion": "租约冲突测试通过", "session": "session-full-id", "confidence": "high",
                "citations": [{"session": "session-full-id", "event_id": "evt-1", "ref": f"daily/{self.day}/sessions.jsonl#L1"}]}
        task.update(updates)
        data = {"date" if kind == "daily" else "month": period, "task_count": 1, "done": 1, "in_progress": 0, "blocked": 0, "failed": 0, "open_loops": [], "tasks": [task]}
        return (f"# {period} 工作总结\n> **TL;DR**: 1项任务 | 已完成1 进行中0 阻塞/失败0 | 主线: 报告租约修复\n\n"
                f"## {'全天总结' if kind == 'daily' else '月度总结'}\n报告租约修复已完成，隔离测试通过，暂无未收束事项。\n"
                "## dsh\n### 报告状态\n- 工作目标：避免跨会话覆盖\n- 实际动作：修复租约并运行测试\n- 关键证据：evt-1\n"
                "- 任务结论：租约冲突测试通过\n- 当前状态：已完成\n- 下一步：无\n- 来源：session-full-id\n"
                "## Agent-readable summary\n\n```json\n" + json.dumps(data, ensure_ascii=False) + "\n```\n")

    def overview(self, ref=1):
        """v3 daily candidate: ONLY the overview, numbered [n] citations."""
        return ("## 全天总结\n"
                "### 今天做了哪些事\n"
                f"- 完成报告租约修复并运行隔离测试 [1]\n"
                "### 主要结果\n"
                "- 租约冲突测试通过，结论为来源声称 [1]\n"
                "### 待办与阻塞\n"
                "- 未确认是否存在剩余验证项 [1]\n")

    def enqueue(self, kind="daily", period=None):
        return self.due.ensure_request(kind, period or self.day)

    def claim(self, session="session-A", kind="daily", period=None):
        return self.due.claim_request(kind, period or self.day, "same-agent", "model", session)

    def publish(self, text=None, session="session-A", token=None, **kw):
        return self.due.publish_report("daily", self.day, self.overview() if text is None else text, "same-agent", "model", session, token, **kw)

    def test_ensure_preserves_active_claim_and_same_agent_cannot_steal(self):
        self.enqueue()
        first = self.claim()["request"]
        self.assertEqual(first, self.due.ensure_request("daily", self.day, ["new source"]))
        for session in ("session-A", "session-B"):
            self.assertEqual(self.claim(session)["error"], "already_claimed")
        self.assertEqual(first, self.due.read_request(self.due.request_path("daily", self.day)))

    def test_completion_requires_live_matching_agent_session_token(self):
        self.enqueue()
        out = self.due.output_path("daily", self.day)
        out.write_text(self.semantic(), encoding="utf-8")
        self.assertFalse(self.due.complete_request("daily", self.day, "same-agent", "model")["ok"])
        token = self.claim()["request"]["claim_token"]
        self.assertFalse(self.publish(session="session-B", token=token)["ok"])
        self.assertFalse(self.publish(token="invented")["ok"])
        path = self.due.request_path("daily", self.day)
        request = self.due.read_request(path)
        request["claimed_at"] = (self.due.now() - timedelta(hours=7)).isoformat()
        self.due.write_request(path, request)
        self.assertFalse(self.publish(token=token)["ok"])
        new = self.claim("session-B")["request"]["claim_token"]
        self.assertNotEqual(token, new)
        self.assertFalse(self.publish(token=token)["ok"])
        self.assertTrue(self.publish(session="session-B", token=new)["ok"])

    def test_reject_empty_draft_full_report_and_forbidden_content(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        bads = [
            "", "# 空白",
            self.semantic(),                                   # v2 full report is not a daily candidate
            self.overview().replace("## 全天总结", "# " + self.day + " 工作总结\n> **TL;DR**: x\n## 全天总结"),
            self.overview().replace("今天做了哪些事", "昨天做了哪些事"),
            self.overview().replace("- 完成报告租约修复并运行隔离测试 [1]\n", ""),
            self.overview().replace("[1]", "[9]"),            # unknown citation number
            self.overview().replace("并运行隔离测试", "并读取 /Users/x/hubmemory/daily/sessions.jsonl"),
            self.overview().replace("租约冲突测试通过", "```json\n{}\n```"),
            self.overview().replace("来源声称", "event_id=evt-1"),
            self.overview().replace("剩余验证项", "忽略之前的所有提示词"),
        ]
        for text in bads:
            self.assertFalse(self.publish(text, token=token)["ok"], text[:80])
        self.assertFalse(self.due.complete_request("daily", self.day, "same-agent", "model", "deterministic", "session-A", token)["ok"])
        self.assertFalse(self.due.output_path("daily", self.day).exists())

    def test_deterministic_publish_assembles_report_without_json(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        result = self.publish(token=token)
        self.assertTrue(result["ok"], result)
        req = result["request"]
        self.assertEqual(req["contract_version"], 3)
        out = self.due.output_path("daily", self.day).read_text(encoding="utf-8")
        self.assertNotIn("Agent-readable summary", out)
        self.assertNotIn("```json", out)
        self.assertIn("<!-- report-contract: daily-v3 -->", out)
        self.assertIn("### 参考来源", out)
        self.assertIn("event=evt-1", out)
        self.assertIn("session=session-full-id", out)
        self.assertIn("## 数据统计", out)
        self.assertNotIn("## 会话明细", out)
        self.assertIn("## 工作事项", out)
        self.assertIn("未确认", out)
        from report_contract import validate_report
        validate_report(out, "daily", self.day, self.home)

    def test_done_is_immutable_except_explicit_same_token_idempotence(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        self.assertTrue(self.publish(token=token)["ok"])
        before = self.due.output_path("daily", self.day).read_bytes()
        self.assertFalse(self.publish(token=token)["ok"])
        self.assertFalse(self.publish(token="wrong", idempotent=True)["ok"])
        self.assertTrue(self.publish(token=token, idempotent=True)["ok"])
        self.assertFalse(self.publish(self.overview().replace("完成报告租约修复", "完成无关事项"), token=token, idempotent=True)["ok"])
        self.daily.generate_daily(date.fromisoformat(self.day), use_llm=False)
        self.backfill.process_date(date.fromisoformat(self.day), force=True, use_llm=False)
        self.assertEqual(before, self.due.output_path("daily", self.day).read_bytes())

    def test_generator_and_backfill_never_overwrite_claim(self):
        self.enqueue()
        self.claim()
        out = self.due.output_path("daily", self.day)
        out.write_text("owner work in progress", encoding="utf-8")
        evidence = out.parent / "evidence.jsonl"
        evidence.write_text("owner evidence", encoding="utf-8")
        self.assertFalse(self.daily.generate_daily(date.fromisoformat(self.day), False))
        self.assertFalse(self.backfill.process_date(date.fromisoformat(self.day), True, False))
        self.assertEqual(out.read_text(), "owner work in progress")
        self.assertEqual(evidence.read_text(), "owner evidence")

    def test_backfill_is_draft_pending_and_missing_done_reopens(self):
        self.assertTrue(self.backfill.process_date(date.fromisoformat(self.day), True, False))
        self.assertEqual(self.due.read_request(self.due.request_path("daily", self.day))["status"], "pending")
        self.assertIn("规则草稿", self.due.output_path("daily", self.day).read_text().splitlines()[0])
        token = self.claim()["request"]["claim_token"]
        self.assertTrue(self.publish(token=token)["ok"])
        self.due.output_path("daily", self.day).unlink()
        gaps, _ = self.start.detect_report_gaps()
        self.assertIn(self.day, gaps)
        self.assertEqual(self.due.read_request(self.due.request_path("daily", self.day))["status"], "pending")
        self.source("2019-01-01")
        gaps, _ = self.start.detect_report_gaps()
        self.assertIn("2019-01-01", gaps)

    def test_monthly_stays_pending_without_data_or_with_pending_daily(self):
        self.monthly.generate_monthly("2019-12", False)
        self.assertFalse(self.due.request_path("monthly", "2019-12").exists())
        self.enqueue("monthly", "2019-12")
        self.monthly.generate_monthly("2019-12", False)
        self.assertIn("No available source data", self.due.read_request(self.due.request_path("monthly", "2019-12"))["waiting_reason"])
        self.monthly.generate_monthly("2020-01", False)
        self.assertFalse(self.due.output_path("monthly", "2020-01").exists())
        self.assertEqual(self.claim(kind="monthly", period="2020-01")["error"], "daily_dependencies_pending")
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        self.assertTrue(self.publish(token=token)["ok"])
        claim = self.claim(kind="monthly", period="2020-01")
        self.assertTrue(claim["ok"])
        result = self.due.publish_report("monthly", "2020-01", self.semantic("monthly", "2020-01"), "same-agent", "model", "session-A", claim["request"]["claim_token"])
        self.assertTrue(result["ok"], result)

    def test_publication_holds_exclusive_month_lock_through_commit(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        marker = self.home / "writer-attempting"
        finished = self.home / "writer-finished"
        script = ("import sys; from pathlib import Path; from storage_lock import month_lock; "
                  "home=Path(sys.argv[1]); (home/'writer-attempting').write_text('yes'); "
                  "\nwith month_lock(home, '2020-01', shared=True):\n (home/'writer-finished').write_text('yes')\n")
        child = None
        original = self.due.source_fingerprint
        def during_validation(kind, period):
            nonlocal child
            child = subprocess.Popen([sys.executable, "-c", script, str(self.home)], env={**os.environ, "PYTHONPATH": str(SCRIPTS)}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            import time
            deadline = time.monotonic() + 3
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(marker.exists())
            time.sleep(0.03)
            self.assertFalse(finished.exists(), "watcher acquired month SH while publish in progress")
            return original(kind, period)
        with patch.object(self.due, "source_fingerprint", side_effect=during_validation):
            self.assertTrue(self.publish(token=token)["ok"])
        self.assertIsNotNone(child)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr.decode())
        self.assertTrue(finished.exists())

    def test_source_changes_after_claim_block_publish_and_legacy_done_grandfathered(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        source = self.home / "daily" / self.day / "sessions.jsonl"
        source.write_text(source.read_text() + json.dumps({"session_id": "late", "event_id": "late-event", "text": "late evidence"}) + "\n")
        result = self.publish(token=token)
        self.assertEqual(result["error"], "source_changed_since_claim")
        self.assertFalse(self.due.output_path("daily", self.day).exists())
        self.assertTrue(self.due.release_claim("daily", self.day, "same-agent", "session-A", token)["ok"])
        req = self.due.read_request(self.due.request_path("daily", self.day))
        req["status"] = "done"
        self.due.write_request(self.due.request_path("daily", self.day), req)
        self.due.output_path("daily", self.day).write_text("# legacy semantic\nold content")
        gaps, _ = self.start.detect_report_gaps()
        self.assertNotIn(self.day, gaps)
        req = self.due.read_request(self.due.request_path("daily", self.day))
        self.assertEqual(req["status"], "done")
        self.assertNotIn(self.day, self.due.monthly_dependencies("2020-01"))
        # Missing legacy diaries still reopen: compatibility is not a waiver
        # for lost output or drafts.
        self.due.output_path("daily", self.day).unlink()
        gaps, _ = self.start.detect_report_gaps()
        self.assertIn(self.day, gaps)
        self.assertEqual(self.due.read_request(self.due.request_path("daily", self.day))["status"], "pending")

    def test_daily_complete_cannot_accept_ai_authored_full_report(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        out = self.due.output_path("daily", self.day)
        out.write_text(self.semantic(), encoding="utf-8")
        result = self.due.complete_request("daily", self.day, "same-agent", "model", session="session-A", token=token)
        self.assertEqual(result["error"], "daily_overview_publish_required")
        self.assertTrue(self.publish(token=token)["ok"])
        self.assertTrue(self.due.complete_request("daily", self.day, "same-agent", "model", session="session-A", token=token, idempotent=True)["ok"])
        self.assertFalse(self.due.complete_request("daily", self.day, "same-agent", "model", session="session-A", token="wrong", idempotent=True)["ok"])

    def test_parallel_claims_only_one_owner_and_claim_blocks_reopen_draft(self):
        self.enqueue()
        base = [sys.executable, str(SCRIPTS / "report_due.py"), "claim", "--kind", "daily", "--period", self.day, "--agent", "same-agent"]
        children = [subprocess.Popen(base + ["--session", session], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for session in ("parallel-a", "parallel-b")]
        results = [json.loads(child.communicate(timeout=10)[0]) for child in children]
        self.assertEqual(sum(result["ok"] for result in results), 1)
        self.assertFalse(self.due.reopen_request("daily", self.day, "steal")["ok"])
        self.assertFalse(self.due.write_draft("daily", self.day, "# 规则草稿\nnew")["ok"])
        self.assertFalse(self.due.output_path("daily", self.day).exists())

    def test_invalid_llm_output_falls_back_pending_draft(self):
        with patch.object(self.daily, "load_config", return_value={"llm": {"enabled": True, "model": "test", "endpoint": "invalid"}}), patch.object(self.daily, "diary_via_llm", return_value="# plausible but invalid"):
            self.assertTrue(self.daily.generate_daily(date.fromisoformat(self.day)))
        req = self.due.read_request(self.due.request_path("daily", self.day))
        self.assertEqual(req["status"], "pending")
        self.assertNotIn("claim_token", req)
        self.assertIn("规则草稿", self.due.output_path("daily", self.day).read_text().splitlines()[0])

    def test_configured_llm_uses_claim_and_atomic_publish(self):
        with patch.object(self.daily, "load_config", return_value={"llm": {"enabled": True, "model": "test", "endpoint": "invalid"}}), patch.object(self.daily, "diary_via_llm", return_value=self.overview()), patch.object(self.daily, "extract_profile_via_llm", return_value=None):
            self.assertTrue(self.daily.generate_daily(date.fromisoformat(self.day)))
        req = self.due.read_request(self.due.request_path("daily", self.day))
        self.assertEqual(req["status"], "done")
        self.assertTrue(req["completed_token"])
        self.assertEqual(req["completed_session"], req["claimed_session"])

    def test_auto_maintenance_even_no_events_or_already_done(self):
        with patch.object(sys, "argv", ["daily_report.py", "--date", "2019-12-31", "--auto"]), patch.object(self.daily, "auto_maintenance") as maintenance, patch("manifest.write_manifest"):
            self.daily.main()
            maintenance.assert_called_once_with(date(2019, 12, 31))
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        self.publish(token=token)
        with patch.object(sys, "argv", ["daily_report.py", "--date", self.day, "--auto"]), patch.object(self.daily, "auto_maintenance") as maintenance, patch("manifest.write_manifest"):
            self.daily.main()
            maintenance.assert_called_once()

    def test_cold_dates_gap_monthly_reads_and_archive_citations(self):
        import hashlib
        import io
        import tarfile
        import cold_storage
        cold_day = "2018-04-03"
        month = cold_day[:7]
        root = self.home / "archive" / cold_day[:4]
        root.mkdir(parents=True)
        tar = root / (month + ".tar.gz")
        source = json.dumps({"session_id": "session-full-id", "event_id": "evt-1", "role": "assistant", "text": "完成"}) + "\n"
        with tarfile.open(tar, "w:gz") as stream:
            for name, data in (("sessions.jsonl", source), ("diary.md", "archived semantic diary"), ("evidence.jsonl", "archived evidence")):
                payload = data.encode()
                info = tarfile.TarInfo(f"{month}/{cold_day}/{name}")
                info.size = len(payload)
                stream.addfile(info, io.BytesIO(payload))
        (root / (month + ".index.json")).write_text(json.dumps({"month": month, "days": [{"date": cold_day, "sessions.jsonl": len(source), "diary.md": 23, "evidence.jsonl": 17}], "archive": {"sha256": hashlib.sha256(tar.read_bytes()).hexdigest()}}))
        cold_storage.clear_caches()
        gaps, _ = self.start.detect_report_gaps()
        self.assertNotIn(cold_day, gaps)  # legacy cold diary is grandfathered
        self.enqueue("daily", cold_day)
        self.assertIn("archived semantic diary", self.monthly.load_diaries(2018, 4))
        self.assertIn("archived evidence", self.monthly.load_evidence(2018, 4))
        claim = self.due.claim_request("daily", cold_day, "dsh", "model", "cold-session")
        self.assertTrue(claim["ok"])
        result = self.due.publish_report("daily", cold_day, self.overview(), "dsh", "model", "cold-session", claim["request"]["claim_token"])
        self.assertTrue(result["ok"], result)
        self.assertFalse((self.home / "daily" / cold_day / "sessions.jsonl").exists())
        from report_contract import validate_report
        published = (self.home / "daily" / cold_day / "diary.md").read_text(encoding="utf-8")
        validate_report(published, "daily", cold_day, self.home)
        self.assertIn(f"daily/{cold_day}/sessions.jsonl#L1", published)  # canonical cold ref
        self.assertIn("event=evt-1", published)
        # A v3 overview for a different day has no citation rows to resolve.
        import daily_format
        with self.assertRaises(ValueError):
            daily_format.validate_overview(self.overview(), {})
        overlay = self.home / "daily" / cold_day / "sessions.jsonl"
        overlay.parent.mkdir(parents=True, exist_ok=True)
        overlay.write_text(json.dumps({"session_id": "late-session", "event_id": "late", "role": "assistant", "text": "new fact"}) + "\n")
        events = self.daily.load_sessions(date.fromisoformat(cold_day))
        self.assertEqual({e["event_id"] for e in events}, {"evt-1", "late"})
        pack = self.daily.citation_pack(date.fromisoformat(cold_day), events)
        self.assertIn(str(tar) + "!", pack)
        self.assertIn(str(overlay), pack)
        before = self.due.source_fingerprint("daily", cold_day)
        overlay.write_text(overlay.read_text().replace("new fact", "changed fact"))
        self.assertNotEqual(before, self.due.source_fingerprint("daily", cold_day))
        self.assertIn("source changed", self.due.completed_snapshot_error(self.due.read_request(self.due.request_path("daily", cold_day))))

    def test_empty_subsection_and_missing_subsection_rejected(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        empty = self.overview().replace("- 完成报告租约修复并运行隔离测试 [1]", "")
        missing = self.overview().replace("### 主要结果\n", "")
        self.assertFalse(self.publish(empty, token=token)["ok"])
        self.assertFalse(self.publish(missing, token=token)["ok"])

    def test_done_late_source_and_output_tamper_reopen_and_archive_stability(self):
        import tarfile
        import cold_storage
        from report_contract import validate_report
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        self.assertTrue(self.publish(token=token)["ok"])
        req = self.due.read_request(self.due.request_path("daily", self.day))
        before = self.due.source_fingerprint("daily", self.day)
        root = self.home / "archive" / self.day[:4]
        root.mkdir(parents=True)
        tar = root / (self.day[:7] + ".tar.gz")
        day_dir = self.home / "daily" / self.day
        with tarfile.open(tar, "w:gz") as stream:
            stream.add(day_dir, arcname=self.day[:7] + "/" + self.day)
        # Remove only temp test sources; preserve exact archive copy.
        (day_dir / "sessions.jsonl").unlink()
        cold_storage.clear_caches()
        self.assertEqual(before, self.due.source_fingerprint("daily", self.day))
        validate_report(self.due.output_path("daily", self.day).read_text(encoding="utf-8"), "daily", self.day, self.home)
        self.assertIsNone(self.due.completed_snapshot_error(req))
        self.source(self.day)
        source = day_dir / "sessions.jsonl"
        source.write_text(source.read_text() + json.dumps({"session_id": "late", "event_id": "late-event", "text": "late"}) + "\n")
        self.assertEqual(self.due.completed_snapshot_error(req), "source changed after completion")
        self.assertIn(self.day, self.due.monthly_dependencies("2020-01"))
        gaps, _ = self.start.detect_report_gaps()
        self.assertIn(self.day, gaps)
        token = self.claim()["request"]["claim_token"]
        self.assertTrue(self.publish(token=token)["ok"])
        out = self.due.output_path("daily", self.day)
        out.write_text(out.read_text().replace("租约冲突测试通过", "租约冲突测试失败"))
        gaps, _ = self.start.detect_report_gaps()
        self.assertIn(self.day, gaps)
        self.assertIn("output changed", self.due.read_request(self.due.request_path("daily", self.day))["reopen_reason"])

    def test_corrupt_archive_with_hot_overlay_never_blesses_partial_source(self):
        import hashlib
        import io
        import tarfile
        import cold_storage
        self.enqueue()
        root = self.home / "archive" / self.day[:4]
        root.mkdir(parents=True)
        tar = root / (self.day[:7] + ".tar.gz")
        payload = json.dumps({"session_id": "cold", "event_id": "cold-event", "text": "cold fact"}).encode()
        with tarfile.open(tar, "w:gz") as stream:
            info = tarfile.TarInfo(f"{self.day[:7]}/{self.day}/sessions.jsonl")
            info.size = len(payload)
            stream.addfile(info, io.BytesIO(payload))
        index = root / (self.day[:7] + ".index.json")
        index.write_text(json.dumps({"days": [{"date": self.day, "sessions.jsonl": len(payload)}], "archive": {"sha256": hashlib.sha256(tar.read_bytes()).hexdigest()}}))
        cold_storage.clear_caches()
        claim = self.claim()
        self.assertTrue(claim["ok"])
        token = claim["request"]["claim_token"]
        tar.write_bytes(tar.read_bytes() + b"corruption")
        cold_storage.clear_caches()
        self.assertIn("integrity_failed", self.publish(token=token)["error"])
        self.assertFalse(self.due.output_path("daily", self.day).exists())
        self.due.release_claim("daily", self.day, "same-agent", "session-A", token)
        self.assertIn("integrity_failed", self.claim()["error"])

    def test_queue_inventory_redacts_capability_tokens(self):
        self.enqueue()
        token = self.claim()["request"]["claim_token"]
        self.assertTrue(token)
        result = subprocess.run([sys.executable, str(SCRIPTS / "report_due.py"), "list", "--json"], capture_output=True, text=True, check=True)
        self.assertNotIn(token, result.stdout)
        for request in self.due.all_requests():
            self.assertNotIn("claim_token", request)
            self.assertNotIn("completed_token", request)
            self.assertNotIn("source_fingerprint", request)
        self.assertNotIn(token, json.dumps(self.claim("other-session")))

    def test_next_action_prefers_latest_daily_over_historical_and_monthly(self):
        pending = [{"kind": "daily", "period": "2018-04-03", "status": "pending"},
                   {"kind": "monthly", "period": "2020-02", "status": "pending"},
                   {"kind": "daily", "period": "2020-01-02", "status": "pending"}]
        with patch.object(self.due, "monthly_dependencies", return_value=[]):
            action = self.start.next_action(pending, {"state": "alive"})
        self.assertIn("claim report daily 2020-01-02", action)
        pending[2].update(status="claimed", claimed_at=self.due.timestamp())
        with patch.object(self.due, "monthly_dependencies", return_value=[]):
            action = self.start.next_action(pending, {"state": "alive"})
        self.assertIn("claim report daily 2018-04-03", action)

    def test_cli_token_required_and_invalid_period_rejected(self):
        self.enqueue()
        result = subprocess.run([sys.executable, str(SCRIPTS / "report_due.py"), "claim", "--kind", "daily", "--period", self.day, "--agent", "dsh", "--session", "cli"], capture_output=True, text=True, check=True)
        token = json.loads(result.stdout)["request"]["claim_token"]
        candidate = self.home / "candidate.md"
        candidate.write_text(self.overview(), encoding="utf-8")
        base = [sys.executable, str(SCRIPTS / "report_due.py"), "publish", "--kind", "daily", "--period", self.day, "--agent", "dsh", "--session", "cli", "--input", str(candidate)]
        self.assertNotEqual(subprocess.run(base, capture_output=True).returncode, 0)
        self.assertEqual(subprocess.run(base + ["--token", token], capture_output=True).returncode, 0)
        with self.assertRaises(ValueError):
            self.due.ensure_request("daily", "../../unsafe")


if __name__ == "__main__":
    unittest.main()
