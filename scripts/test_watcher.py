#!/usr/bin/env python3
"""Focused regression tests for hubmemory watcher durability."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


WATCHER_PATH = Path(__file__).with_name("watcher.py")


def load_watcher(hub_home: Path):
    os.environ["HUBMEMORY_HOME"] = str(hub_home)
    spec = importlib.util.spec_from_file_location("hubmemory_watcher_tested", WATCHER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class WatcherDurabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.hub = self.root / "hub"
        self.source_root = self.root / "codex"
        for path in (self.hub / "state", self.hub / "live", self.hub / "daily", self.source_root):
            path.mkdir(parents=True, exist_ok=True)
        self.w = load_watcher(self.hub)
        self.config = {"listen": {"codex": str(self.source_root)}}
        self.offsets = self.w.OffsetStore(self.hub / "state" / "offsets.json")
        self.live = self.w.LiveIndex(self.hub / "live" / "index.json")
        self.daily = self.w.DailyWriter(self.hub / "daily")
        self.processor = self.w.SessionProcessor(self.offsets, self.live, self.daily, self.config)
        self.session_id = "01a11111-2222-7333-8444-555555555555"
        self.source = self.source_root / f"rollout-2026-09-23T20-00-00-{self.session_id}.jsonl"
        self.meta = {
            "timestamp": "2026-09-23T12:00:00Z",
            "type": "session_meta",
            "payload": {
                "session_id": self.session_id,
                "cwd": "/tmp/project",
                "model_provider": "local",
                "originator": "Codex Desktop",
            },
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def append_record(self, record: dict, newline: bool = True) -> None:
        with self.source.open("ab") as f:
            f.write(json.dumps(record).encode())
            if newline:
                f.write(b"\n")

    def message(self, text: str) -> dict:
        return {
            "timestamp": "2026-09-23T12:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }

    def events(self) -> list[dict]:
        path = self.hub / "daily" / "2026-09-23" / "sessions.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_reconcile_recovers_missed_file_event_without_duplicates(self) -> None:
        self.append_record(self.meta)
        self.processor.process_file(self.source)
        self.append_record(self.message("missed event"))

        self.assertEqual(self.w.reconcile_once(self.processor, [self.source_root]), 1)
        self.assertEqual([event["text"] for event in self.events()], ["missed event"])
        self.assertEqual(self.w.reconcile_once(self.processor, [self.source_root]), 0)
        self.assertEqual(len(self.events()), 1)

    def test_partial_tail_is_retried_after_newline_arrives(self) -> None:
        self.append_record(self.meta)
        partial = json.dumps(self.message("partial")).encode()
        with self.source.open("ab") as f:
            f.write(partial)
        self.processor.process_file(self.source)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.offsets.get(str(self.source))["byte_offset"], len(json.dumps(self.meta).encode()) + 1)

        with self.source.open("ab") as f:
            f.write(b"\n")
        self.processor.process_file(self.source)
        self.assertEqual([event["text"] for event in self.events()], ["partial"])

    def test_incremental_restart_primes_codex_metadata_and_custom_tools(self) -> None:
        self.append_record(self.meta)
        self.processor.process_file(self.source)
        self.append_record({
            "timestamp": "2026-09-23T12:00:02Z",
            "type": "response_item",
            "payload": {"type": "custom_tool_call", "name": "exec", "input": "ls"},
        })

        restarted = self.w.SessionProcessor(self.offsets, self.live, self.daily, self.config)
        restarted.process_file(self.source)
        event = self.events()[0]
        self.assertEqual(event["agent"], "codex-desktop")
        self.assertEqual(event["cwd"], "/tmp/project")
        self.assertEqual(event["model"], "local")
        self.assertEqual(event["tool_uses"][0]["name"], "exec")

    def test_snapshot_event_id_deduplicates_legacy_rows(self) -> None:
        event = {
            "ts": "2026-09-23T12:00:00+00:00",
            "agent": "qclaw",
            "model": "unknown",
            "session_id": "qclaw-memory-2026-09-23",
            "cwd": "/tmp",
            "role": "assistant",
            "text": "legacy snapshot",
            "tool_uses": [],
            "tokens_out": 5,
            "source_file": "/tmp/2026-09-23.md",
        }
        target = self.hub / "daily" / "2026-09-23" / "sessions.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(event) + "\n")
        event["event_id"] = self.w.snapshot_event_id(event)
        self.assertFalse(self.daily.append(event))
        self.assertEqual(len(target.read_text().splitlines()), 1)

    def add_archived_event(self, day: str, session_id: str) -> None:
        target = self.hub / "daily" / day / "sessions.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"session_id": session_id}) + "\n")

    def add_live_entry(self, session_id: str, last_activity: str, status: str = "stale") -> Path:
        self.live.data[session_id] = {
            "agent": "codex",
            "model": "test",
            "cwd": "/tmp",
            "started_at": last_activity,
            "last_activity": last_activity,
            "status": status,
            "message_count": 1,
        }
        snapshot = self.hub / "live" / f"codex_{session_id}.md"
        snapshot.write_text("snapshot", encoding="utf-8")
        return snapshot

    def test_live_retention_keeps_today_grace_tasks_and_unarchived(self) -> None:
        now = datetime.fromisoformat("2026-09-26T12:00:00+08:00")
        cases = {
            "remove-archived-old": ("2026-09-24T08:00:00+08:00", True),
            "keep-today": ("2026-09-26T08:00:00+08:00", True),
            "keep-grace": ("2026-09-25T20:00:00+08:00", True),
            "keep-task": ("2026-09-24T09:00:00+08:00", True),
            "keep-unarchived": ("2026-09-24T10:00:00+08:00", False),
        }
        snapshots = {}
        for session_id, (last_activity, archived) in cases.items():
            snapshots[session_id] = self.add_live_entry(session_id, last_activity)
            if archived:
                self.add_archived_event(last_activity[:10], session_id)
        tasks = {
            "active_tasks": [{
                "agent": "codex",
                "session_id": "keep-task",
                "status": "blocked",
            }]
        }
        (self.hub / "live" / "tasks.json").write_text(json.dumps(tasks), encoding="utf-8")

        removed = self.live.prune_stale(now)

        self.assertEqual(removed, ["remove-archived-old"])
        self.assertNotIn("remove-archived-old", self.live.data)
        self.assertFalse(snapshots["remove-archived-old"].exists())
        for session_id in cases.keys() - {"remove-archived-old"}:
            self.assertIn(session_id, self.live.data)
            self.assertTrue(snapshots[session_id].exists())

    def test_pruned_session_is_recreated_by_later_event(self) -> None:
        now = datetime.fromisoformat("2026-09-26T12:00:00+08:00")
        session_id = "reactivated"
        self.add_live_entry(session_id, "2026-09-24T08:00:00+08:00")
        self.add_archived_event("2026-09-24", session_id)
        self.assertEqual(self.live.prune_stale(now), [session_id])

        self.live.update_session(session_id, {
            "agent": "codex",
            "model": "test",
            "cwd": "/tmp",
            "ts": "2026-09-26T12:01:00+08:00",
        })

        self.assertIn(session_id, self.live.data)
        self.assertEqual(self.live.data[session_id]["status"], "active")
        self.assertEqual(self.live.data[session_id]["message_count"], 1)


if __name__ == "__main__":
    unittest.main()
