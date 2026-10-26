#!/usr/bin/env python3
"""Tests for decision_state.py — timestamps, transitions, validation,
concurrent appends, provenance, unknown/ambiguous targets, cycles."""
from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import decision_state as ds


def _mem(mid, status="active", created="2026-10-01T10:00:00+08:00",
         supersedes=None):
    return {
        "id": mid, "title": "t-" + mid, "content": "c-" + mid,
        "type": "fact", "status": status, "project": "",
        "confidence": 0.7, "created_at": created, "updated_at": created,
        "source_session_ids": ["sess-1"], "source_event_ids": [],
        "supersedes": supersedes or [], "tags": [], "access_count": 0,
    }


class DecisionCase(unittest.TestCase):
    def setUp(self):
        self._td = TemporaryDirectory()
        self.hub = Path(self._td.name)
        (self.hub / "memory").mkdir(parents=True)
        self.mems = {}
        self._write_mems()

    def tearDown(self):
        self._td.cleanup()

    def _write_mems(self):
        path = self.hub / "memory" / "memories.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for m in self.mems.values():
                fh.write(json.dumps(m) + "\n")

    def add_mem(self, mid, **kw):
        self.mems[mid] = _mem(mid, **kw)
        self._write_mems()

    def mark(self, mid, **kw):
        kw.setdefault("decision", "accepted")
        kw.setdefault("reason", "r")
        kw.setdefault("source_session_ids", ["sess-1"])
        return ds.mark_decision(self.hub, mid, **kw)


class MarkValidationTest(DecisionCase):
    def test_mark_appends_event_with_required_fields(self):
        self.add_mem("memory_a")
        ev = self.mark("memory_a", reason="verified by user",
                       source_event_ids=["e9"])
        self.assertEqual(ev["decision"], "accepted")
        self.assertTrue(ev["id"].startswith("dec_"))
        self.assertIn("ts", ev)
        lines = (self.hub / "memory" / "decision_events.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["memory_id"], "memory_a")

    def test_unknown_and_ambiguous_target_rejected(self):
        self.add_mem("memory_a")
        with self.assertRaisesRegex(ValueError, "unknown memory"):
            self.mark("memory_missing")
        self.mems["memory_dup2"] = _mem("memory_a")
        self._write_mems()
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.mark("memory_a")

    def test_invalid_decision_reason_provenance_ts_rejected(self):
        self.add_mem("memory_a")
        with self.assertRaises(ValueError):
            self.mark("memory_a", decision="maybe")
        with self.assertRaises(ValueError):
            self.mark("memory_a", reason="   ")
        with self.assertRaisesRegex(ValueError, "source"):
            self.mark("memory_a", source_session_ids=[], source_event_ids=[])
        with self.assertRaisesRegex(ValueError, "invalid ts"):
            self.mark("memory_a", ts="not-a-date")
        self.assertEqual(ds.read_events(self.hub)[0], [])  # nothing written

    def test_superseded_requires_known_replacement_no_self(self):
        self.add_mem("memory_a")
        with self.assertRaisesRegex(ValueError, "replacement"):
            self.mark("memory_a", decision="superseded")
        with self.assertRaisesRegex(ValueError, "unknown replacement"):
            self.mark("memory_a", decision="superseded",
                      replacement_ids=["memory_x"])
        with self.assertRaisesRegex(ValueError, "self"):
            self.mark("memory_a", decision="superseded",
                      replacement_ids=["memory_a"])
        with self.assertRaisesRegex(ValueError, "only apply"):
            self.mark("memory_a", decision="accepted",
                      replacement_ids=["memory_b"])

    def test_target_requires_pair_and_is_stored_unverified(self):
        self.add_mem("memory_a")
        with self.assertRaisesRegex(ValueError, "together"):
            self.mark("memory_a", session_id="s1")
        ev = self.mark("memory_a", session_id="s1", event_id="e1")
        self.assertEqual(ev["target"], {"session_id": "s1", "event_id": "e1"})
        self.assertEqual(ev["target_verification"], "unverified")

    def test_structural_and_decision_cycles_rejected(self):
        # structural: A supersedes B (edge B->A) -> A cannot be
        # superseded by B back (would close the loop)
        self.add_mem("memory_a", supersedes=["memory_b"])
        self.add_mem("memory_b")
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.mark("memory_a", decision="superseded",
                      replacement_ids=["memory_b"])
        # decision: A superseded by B -> B cannot be superseded by A
        self.add_mem("memory_c")
        self.add_mem("memory_d")
        self.mark("memory_c", decision="superseded",
                  replacement_ids=["memory_d"])
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.mark("memory_d", decision="superseded",
                      replacement_ids=["memory_c"])


class ResolutionTest(DecisionCase):
    def test_transitions_and_reversibility(self):
        self.add_mem("memory_a")
        self.mark("memory_a", decision="rejected", ts="2026-10-20T10:00:00+08:00")
        st = ds.resolve_state(self.hub, "memory_a")
        self.assertEqual(st["state"], "rejected")
        self.assertFalse(st["recommended"])
        self.assertIn("historical", st["caution"])
        ev2 = self.mark("memory_a", decision="accepted",
                        ts="2026-10-21T10:00:00+08:00")
        st = ds.resolve_state(self.hub, "memory_a")
        self.assertEqual(st["state"], "accepted")
        self.assertTrue(st["recommended"])
        self.assertEqual(st["event_id"], ev2["id"])
        self.assertEqual(len(st["history"]), 2)  # log keeps both, latest wins

    def test_as_of_exclusive_cutoff(self):
        self.add_mem("memory_a")
        self.mark("memory_a", decision="rejected", ts="2026-10-20T10:00:00+08:00")
        self.mark("memory_a", decision="accepted", ts="2026-10-21T10:00:00+08:00")
        # exactly at the second event ts => it is excluded (exclusive)
        st = ds.resolve_state(self.hub, "memory_a",
                              as_of="2026-10-21T10:00:00+08:00")
        self.assertEqual(st["state"], "rejected")
        st = ds.resolve_state(self.hub, "memory_a",
                              as_of="2026-10-22T00:00:00+08:00")
        self.assertEqual(st["state"], "accepted")
        with self.assertRaises(ValueError):
            ds.resolve_state(self.hub, "memory_a", as_of="garbage")

    def test_structural_supersedes_and_creation_cutoff(self):
        self.add_mem("memory_old")
        self.add_mem("memory_new", created="2026-10-20T10:00:00+08:00",
                     supersedes=["memory_old"])
        st = ds.resolve_state(self.hub, "memory_old")
        self.assertEqual(st["state"], "superseded")
        self.assertEqual(st["replaced_by"], ["memory_new"])
        # before the replacement record existed it cannot supersede
        st = ds.resolve_state(self.hub, "memory_old",
                              as_of="2026-10-20T09:00:00+08:00")
        self.assertEqual(st["state"], "unmarked")

    def test_structural_cycle_flagged_not_recommended(self):
        self.add_mem("memory_a", supersedes=["memory_b"])
        self.add_mem("memory_b", supersedes=["memory_a"])
        st = ds.resolve_state(self.hub, "memory_a")
        self.assertTrue(st["cycle"])
        self.assertFalse(st["recommended"])
        self.assertIn("cycle", st["caution"])

    def test_malformed_log_fails_closed(self):
        self.add_mem("memory_a")
        self.mark("memory_a")
        path = self.hub / "memory" / "decision_events.jsonl"
        with path.open("a") as fh:
            fh.write("not json\n")
            fh.write(json.dumps({"memory_id": "memory_a"}) + "\n")
        events, issues = ds.read_events(self.hub)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(issues), 2)
        with self.assertRaisesRegex(ValueError, "malformed"):
            ds.load_states(self.hub)

    def _append_raw(self, obj):
        path = self.hub / "memory" / "decision_events.jsonl"
        with path.open("a") as fh:
            fh.write((obj if isinstance(obj, str)
                      else json.dumps(obj)) + "\n")

    def _base_event(self):
        return {"id": "dec_x1", "memory_id": "memory_a",
                "decision": "rejected", "reason": "r",
                "source_session_ids": ["s"], "source_event_ids": [],
                "ts": "2026-10-20T10:00:00+08:00"}

    def test_valid_json_lacking_schema_is_malformed(self):
        self.add_mem("memory_a")
        cases = [
            # missing id / reason / provenance
            dict(self._base_event(), id=None),
            dict(self._base_event(), reason=""),
            dict(self._base_event(), source_session_ids=[],
                 source_event_ids=[]),
            dict(self._base_event(), source_session_ids="s1"),  # not a list
            dict(self._base_event(), source_event_ids=["e", 3]),
            # superseded: replacement_ids as bare string (would split to
            # characters), empty list, self-reference
            dict(self._base_event(), decision="superseded",
                 replacement_ids="memory_b"),
            dict(self._base_event(), decision="superseded",
                 replacement_ids=[]),
            dict(self._base_event(), decision="superseded",
                 replacement_ids=["memory_a"]),
            # replacements on non-superseded
            dict(self._base_event(), replacement_ids=["memory_b"]),
            # malformed exact target / reverses types
            dict(self._base_event(), target={"session_id": "s"}),
            dict(self._base_event(), reverses=3),
        ]
        for i, bad in enumerate(cases):
            with self.subTest(case=i):
                self._append_raw(bad)
                with self.assertRaisesRegex(ValueError, "malformed"):
                    ds.iter_events(self.hub)
                (self.hub / "memory" / "decision_events.jsonl").unlink()

    def test_malformed_memories_fails_closed(self):
        self.add_mem("memory_a")
        path = self.hub / "memory" / "memories.jsonl"
        with path.open("a") as fh:
            fh.write("not json\n")
        with self.assertRaisesRegex(ValueError, "malformed memories"):
            ds.load_states(self.hub)
        with self.assertRaisesRegex(ValueError, "malformed memories"):
            self.mark("memory_a")

    def test_events_on_missing_memory_are_state_not_crash(self):
        self.add_mem("memory_a")
        self.mark("memory_a", decision="rejected")
        self.mems.clear()
        self._write_mems()
        st = ds.resolve_state(self.hub, "memory_a")
        self.assertEqual(st["state"], "rejected")
        self.assertTrue(st["missing_record"])
        self.assertFalse(st["recommended"])


class AnnotateTest(DecisionCase):
    def test_annotate_compact_state_retained_not_recommended(self):
        self.add_mem("memory_a")
        self.add_mem("memory_b")
        self.mark("memory_a", decision="rejected", reason="wrong fact")
        docs = [{"id": "memory_a"}, {"id": "memory_b"}, {"text": "plain"}]
        out = ds.annotate_documents(docs, self.hub)
        self.assertEqual(len(out), 3)  # retained, not dropped
        d_a, d_b = out[0]["decision"], out[1]["decision"]
        self.assertEqual(d_a["state"], "rejected")
        self.assertFalse(d_a["recommended"])
        self.assertEqual(d_a["rank_hint"], "demote")
        self.assertIn("historical", d_a["caution"])
        self.assertEqual(d_a["reason"], "wrong fact")
        self.assertIn("effective_at", d_a)
        self.assertNotIn("history", d_a)  # compact: no per-hit inflation
        self.assertEqual(d_b["state"], "unmarked")
        self.assertTrue(d_b["recommended"])
        self.assertNotIn("decision", out[2])   # unmarked docs pass through
        self.assertIs(out[2], docs[2])         # no copy for unchanged docs
        self.assertNotIn("decision", docs[0])  # inputs not mutated

    def test_annotate_exact_session_event_target(self):
        self.add_mem("memory_a")
        ev = self.mark("memory_a", decision="rejected",
                       session_id="sess-9", event_id="ev-3")
        docs = [{"session_id": "sess-9", "event_id": "ev-3"},
                {"session_id": "sess-9", "event_id": "other"}]
        out = ds.annotate_documents(docs, self.hub)
        self.assertEqual(out[0]["decision"]["state"], "rejected")
        self.assertFalse(out[0]["decision"]["recommended"])
        self.assertEqual(out[0]["decision"]["event_id"], ev["id"])
        self.assertNotIn("decision", out[1])  # non-target doc untouched

    def test_annotate_as_of_historical_view(self):
        self.add_mem("memory_a")
        self.mark("memory_a", decision="rejected", ts="2026-10-20T10:00:00+08:00")
        docs = [{"id": "memory_a"}]
        now = ds.annotate_documents(docs, self.hub)[0]["decision"]
        past = ds.annotate_documents(
            docs, self.hub, as_of="2026-10-19T00:00:00+08:00")[0]["decision"]
        self.assertEqual(now["state"], "rejected")
        self.assertEqual(past["state"], "unmarked")


class ProvenanceConcurrencyTest(DecisionCase):
    def test_provenance_stored_verbatim_and_sources_kept(self):
        self.add_mem("memory_a")
        ev = self.mark("memory_a", reason="user said so",
                       source_session_ids=["sess-x", "sess-y"],
                       source_event_ids=["e1"], agent="dsh")
        self.assertEqual(ev["source_session_ids"], ["sess-x", "sess-y"])
        self.assertEqual(ev["source_event_ids"], ["e1"])
        self.assertEqual(ev["agent"], "dsh")

    def test_reverses_references_existing_event(self):
        self.add_mem("memory_a")
        ev1 = self.mark("memory_a", decision="rejected")
        ev2 = self.mark("memory_a", decision="accepted", reverses=ev1["id"],
                        ts="2026-10-22T10:00:00+08:00")
        self.assertEqual(ev2["reverses"], ev1["id"])
        with self.assertRaisesRegex(ValueError, "unknown reverses"):
            self.mark("memory_a", decision="accepted", reverses="dec_nope")

    def test_concurrent_appends_unique_ids_no_loss(self):
        self.add_mem("memory_a")
        n = 12
        errors = []

        def worker(i):
            try:
                ds.mark_decision(self.hub, "memory_a", decision="accepted",
                                 reason="t%d" % i, source_session_ids=["s%d" % i])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        events, issues = ds.read_events(self.hub)
        self.assertEqual(issues, [])
        self.assertEqual(len(events), n)
        self.assertEqual(len({e["id"] for e in events}), n)


class CliTest(DecisionCase):
    def test_cli_mark_and_list(self):
        self.add_mem("memory_a")
        rc = ds.main(["mark", "--hub-home", str(self.hub),
                      "--memory", "memory_a", "--decision", "rejected",
                      "--reason", "stale", "--source-session", "s1"])
        self.assertEqual(rc, 0)
        rc = ds.main(["list", "--hub-home", str(self.hub), "--json"])
        self.assertEqual(rc, 0)
        rc = ds.main(["mark", "--hub-home", str(self.hub),
                      "--memory", "memory_ghost", "--decision", "accepted",
                      "--reason", "x", "--source-session", "s1"])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
