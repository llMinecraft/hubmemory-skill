#!/usr/bin/env python3
"""Isolated regression tests for session_start.py --handoff daily handoff.

Never touches the real hubmemory home: every case runs the CLI against a
fresh temp HUBMEMORY_HOME in a subprocess, and queue fixtures are seeded
directly. Covers: newest-first daily choice, same-day refusal, foreign
leases never stolen, missing identity/model refusal, same-owner resume,
the real bounded evidence pack, and bounded model-free output.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
START = SCRIPTS / "session_start.py"
DUE = SCRIPTS / "report_due.py"
ME = {"agent": "dsh", "model": "dsh-model-x", "session": "sess-mine-1"}

# Env vars that must not leak identity/model defaults into the subprocess.
IDENTITY_ENV = (
    "DSH_SESSION_ID", "CLAUDE_SESSION_ID", "CODEX_SESSION_ID",
    "HUBMEMORY_MODEL", "DSH_MODEL", "CLAUDE_MODEL", "ANTHROPIC_MODEL",
    "CODEX_MODEL", "OPENAI_MODEL",
)


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hubmemory-handoff-test-")
        self.home = Path(self.temp.name)
        self.day = "2020-01-02"

    def tearDown(self):
        self.temp.cleanup()

    # -- fixtures ---------------------------------------------------------

    def source(self, day, marker=None):
        root = self.home / "daily" / day
        root.mkdir(parents=True, exist_ok=True)
        events = [
            {"session_id": "sess-src", "event_id": f"evt-{day}-u", "role": "user",
             "agent": "dsh", "text": marker or f"当日工作目标 {day}", "ts": day + "T09:00:00+08:00"},
            {"session_id": "sess-src", "event_id": f"evt-{day}-a", "role": "assistant",
             "agent": "dsh", "text": f"已完成 {day} 的隔离回归测试并交付结果" * 3,
             "ts": day + "T12:00:00+08:00"},
        ]
        (root / "sessions.jsonl").write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events),
            encoding="utf-8")

    def request_path(self, kind, period):
        return self.home / "reports" / "requests" / f"{kind}-{period}.json"

    def write_request(self, kind, period, **fields):
        path = self.request_path(kind, period)
        path.parent.mkdir(parents=True, exist_ok=True)
        request = {"id": f"{kind}:{period}", "kind": kind, "period": period,
                   "status": "pending", "created_at": "2020-01-01T00:00:00+08:00"}
        request.update(fields)
        path.write_text(json.dumps(request, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        return request

    def read_request(self, kind, period):
        return json.loads(self.request_path(kind, period).read_text(encoding="utf-8"))

    def claimed(self, period, agent, session, hours_ago=0, token="tok-x"):
        stamp = (datetime.now().astimezone()
                 - timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        return self.write_request(
            "daily", period, status="claimed", claimed_at=stamp,
            claimed_by=agent, claimed_model="m", claimed_session=session,
            claim_token=token)

    def enqueue(self, day):
        self.source(day)
        self.write_request("daily", day)

    # -- CLI --------------------------------------------------------------

    def run_start(self, *args, env_extra=None, strip_dsh=True):
        env = {k: v for k, v in os.environ.items() if k not in IDENTITY_ENV}
        if strip_dsh:
            env = {k: v for k, v in env.items() if not k.startswith("DSH_")}
        env["HUBMEMORY_HOME"] = str(self.home)
        env["HUBMEMORY_SKILL_DIR"] = str(SCRIPTS.parent)
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, str(START), *args],
            capture_output=True, text=True, env=env)

    def handoff_json(self, *args, env_extra=None):
        result = self.run_start("--handoff", "--json", *args, env_extra=env_extra)
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result, json.loads(result.stdout)["handoff"]

    # -- tests ------------------------------------------------------------

    def test_handoff_claims_newest_daily_first_with_real_token_and_pack(self):
        self.enqueue("2020-01-01")
        self.enqueue("2020-01-02")
        self.enqueue("2020-01-03")   # newest claimable daily wins
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(payload["ok"], payload)
        self.assertEqual(payload["action"], "claimed")
        self.assertEqual(payload["period"], "2020-01-03")
        req = self.read_request("daily", "2020-01-03")
        self.assertEqual(req["status"], "claimed")
        self.assertEqual(req["claimed_by"], ME["agent"])
        self.assertEqual(req["claimed_model"], ME["model"])
        self.assertEqual(req["claimed_session"], ME["session"])
        # The handed-off token is the real on-disk capability, not a placeholder.
        self.assertEqual(payload["claim_token"], req["claim_token"])
        self.assertTrue(len(payload["claim_token"]) > 20)
        self.assertIn("--token " + req["claim_token"], payload["publish_command"])
        for sibling in ("2020-01-01", "2020-01-02"):
            self.assertEqual(self.read_request("daily", sibling)["status"], "pending")

    def test_handoff_refuses_current_day_and_future(self):
        today = datetime.now().date().isoformat()
        future = (datetime.now().date() + timedelta(days=1)).isoformat()
        self.enqueue(today)
        self.enqueue(future)
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "no_claimable_daily")
        for day in (today, future):
            self.assertEqual(self.read_request("daily", day)["status"], "pending")

    def test_handoff_skips_foreign_lease_and_claims_next_free(self):
        self.claimed("2020-01-05", "claude", "sess-foreign", token="foreign-tok")
        self.source("2020-01-05")
        self.enqueue("2020-01-04")
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertEqual(result.returncode, 0, result.stderr)
        # The foreign-leased newest daily is skipped, not stolen; the next
        # claimable daily is handed off instead.
        self.assertEqual(payload["period"], "2020-01-04")
        req = self.read_request("daily", "2020-01-05")
        self.assertEqual(req["claimed_by"], "claude")
        self.assertEqual(req["claim_token"], "foreign-tok")

    def test_handoff_never_steals_foreign_active_lease(self):
        self.claimed("2020-01-05", "claude", "sess-foreign", token="foreign-tok")
        self.source("2020-01-05")
        self.claimed("2020-01-04", "codex", "sess-foreign-2", token="foreign-tok-2")
        self.source("2020-01-04")
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "no_claimable_daily")
        self.assertEqual(len(payload["foreign_active_leases"]), 2)
        self.assertEqual(payload["foreign_active_leases"][0]["period"], "2020-01-04")
        # Both foreign leases remain untouched.
        self.assertEqual(self.read_request("daily", "2020-01-05")["claimed_by"], "claude")
        self.assertEqual(self.read_request("daily", "2020-01-04")["claimed_by"], "codex")

    def test_handoff_requires_real_identity_and_model(self):
        self.enqueue(self.day)
        # No identity env, no flags: refused, nothing claimed.
        result, payload = self.handoff_json()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"], "missing_identity_model")
        # Identity but no model env/--model: refused, never fabricated.
        result, payload = self.handoff_json("--agent", "dsh", "--session", "s1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"], "missing_model")
        req = self.read_request("daily", self.day)
        self.assertEqual(req["status"], "pending")
        self.assertNotIn("claimed_by", req)
        # Session + model env defaults resolve without flags.
        result, payload = self.handoff_json(
            "--agent", "dsh",
            env_extra={"DSH_SESSION_ID": "sess-env", "DSH_MODEL": "env-model-9"})
        self.assertEqual(result.returncode, 0, result.stderr)
        req = self.read_request("daily", self.day)
        self.assertEqual(req["claimed_session"], "sess-env")
        self.assertEqual(req["claimed_model"], "env-model-9")
        # Agent defaults to dsh when a DSH_* env is present.
        self.assertEqual(req["claimed_by"], "dsh")

    def test_handoff_resumes_same_owner_active_lease_without_duplicating(self):
        self.enqueue(self.day)
        _, first = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        token = first["claim_token"]
        # Newer pending daily appears; the live own lease still wins.
        self.enqueue("2020-01-09")
        result, second = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(second["action"], "resumed")
        self.assertEqual(second["period"], self.day)
        self.assertEqual(second["claim_token"], token)
        req = self.read_request("daily", self.day)
        self.assertEqual(req["claim_token"], token)
        self.assertEqual(req["claimed_at"], first["claimed_at"])
        self.assertEqual(self.read_request("daily", "2020-01-09")["status"], "pending")
        # Same agent from a DIFFERENT session must not resume/steal.
        result, third = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", "sess-other")
        self.assertEqual(third["period"], "2020-01-09")

    def test_handoff_expired_lease_is_reclaimable(self):
        self.enqueue(self.day)
        self.claimed(self.day, "claude", "sess-old", hours_ago=7, token="expired-tok")
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(payload["action"], "claimed")
        req = self.read_request("daily", self.day)
        self.assertEqual(req["claimed_by"], ME["agent"])
        self.assertNotEqual(req["claim_token"], "expired-tok")

    def test_handoff_evidence_pack_is_real_bounded_and_no_model_calls(self):
        day = "2020-01-07"
        self.enqueue(day)
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertTrue(payload["ok"], payload)
        pack = payload["evidence_pack"]
        # Real numbered evidence rows from the day's sessions.jsonl.
        self.assertIn("[1]", pack)
        self.assertIn("user/dsh", pack)
        self.assertIn("当日工作目标", pack)
        self.assertIn("覆盖：", pack)
        self.assertLess(len(pack), 40000)
        # Instructions constrain the AI to the 全天总结 overview contract.
        self.assertIn("## 全天总结", payload["overview_instructions"])
        self.assertIn("[n]", payload["overview_instructions"])

    def test_handoff_text_output_bounded_and_publish_command_is_last(self):
        self.enqueue("2020-01-01")
        self.enqueue("2020-01-02")
        result = self.run_start(
            "--handoff", "--agent", ME["agent"], "--model", ME["model"],
            "--session", ME["session"])
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        # Bounded: no manifest/backlog dump in handoff mode.
        self.assertLess(len(out), 30000)
        self.assertNotIn("pending reports", out)
        self.assertNotIn("historical cold backlog", out)
        self.assertNotIn("## Last 7 days", out)
        self.assertIn("claim_token: ", out)
        token = out.split("claim_token: ", 1)[1].splitlines()[0].strip()
        self.assertTrue(len(token) > 20)
        self.assertIn("== evidence pack", out)
        self.assertIn("[1]", out)
        # The final line IS the real publish command with the real token.
        last = out.strip().splitlines()[-1]
        self.assertIn("report_due.py", last)
        self.assertIn(" publish ", last)
        self.assertIn("--token " + token, last)
        self.assertIn("--period 2020-01-02", last)
        self.assertIn("--input ", last)
        self.assertIn("--agent " + ME["agent"], last)
        self.assertIn("--session " + ME["session"], last)

    def test_default_startup_compact_pending_and_next_action_compat(self):
        for i in range(1, 8):
            self.enqueue("2020-01-0%d" % i)
        self.write_request("monthly", "2020-01", status="pending")
        result = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("pending reports (8):", out)
        # Newest-first, capped at 5 entries with an explicit omission count.
        shown = [line for line in out.splitlines() if line.startswith("  - ")]
        self.assertEqual(len(shown), 5)
        self.assertIn("2020-01-07", shown[0])
        self.assertIn("3 older requests omitted", out)
        self.assertIn("next: claim report daily 2020-01-07", out)

    def test_json_default_keeps_full_pending_and_next_action(self):
        for i in range(1, 8):
            self.enqueue("2020-01-0%d" % i)
        result = self.run_start("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(len(data["pending_reports"]), 7)
        self.assertIn("claim report daily 2020-01-07", data["next_action"])

    def test_handoff_nothing_pending_is_honest(self):
        result, payload = self.handoff_json(
            "--agent", ME["agent"], "--model", ME["model"], "--session", ME["session"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload["error"], "no_claimable_daily")
        self.assertEqual(payload["foreign_active_leases"], [])


if __name__ == "__main__":
    unittest.main()
