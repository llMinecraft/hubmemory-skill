"""Tests for retrieval_index.py — parity vs hybrid_query.iter_documents,
invalidation (append/edit/delete), archive overlay + corruption, historical
ref_date windows, multiprocess concurrency, bounded compact stats, 0600.

Run:  python3 test_retrieval_index.py   (from scripts/)
"""

from __future__ import annotations

import io
import hashlib
import json
import multiprocessing as mp
import os
import sys
import tarfile
import tempfile
import time
import unittest
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hybrid_query as hq  # noqa: E402
import cold_storage as cold  # noqa: E402
import retrieval_index as ri  # noqa: E402


TODAY = date.today()
D1 = (TODAY - timedelta(days=1)).isoformat()
D2 = (TODAY - timedelta(days=2)).isoformat()
D3 = (TODAY - timedelta(days=3)).isoformat()
OLD = (TODAY - timedelta(days=40)).isoformat()
FUTURE = (TODAY + timedelta(days=1)).isoformat()


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _write_day(hub: Path, day: str, *, evidence=(), sessions=(),
               diary=None, conversations=None):
    d = hub / "daily" / day
    d.mkdir(parents=True, exist_ok=True)
    if evidence:
        (d / "evidence.jsonl").write_text(
            "".join(_j(r) + "\n" for r in evidence), encoding="utf-8")
    if sessions:
        (d / "sessions.jsonl").write_text(
            "".join(_j(r) + "\n" for r in sessions), encoding="utf-8")
    if diary is not None:
        (d / "diary.md").write_text(diary, encoding="utf-8")
    if conversations is not None:
        (d / "conversations.md").write_text(conversations, encoding="utf-8")


def _make_archive(hub: Path, month: str, days: dict[str, dict[str, str]],
                  *, index_sha="auto") -> Path:
    """Mirror archive.py/test_cold_storage layout + index sidecar."""
    year_dir = hub / "archive" / month[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    tar_path = year_dir / f"{month}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for day in sorted(days):
            for name in sorted(days[day]):
                data = days[day][name].encode("utf-8")
                info = tarfile.TarInfo(f"{month}/{day}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    if index_sha is not None:
        sha = hashlib.sha256(tar_path.read_bytes()).hexdigest() \
            if index_sha == "auto" else index_sha
        idx = {
            "month": month,
            "day_count": len(days),
            "days": [{"date": d,
                      **{n: len(days[d][n].encode()) for n in days[d]},
                      "archived": True}
                     for d in sorted(days)],
            "archive": {"path": str(tar_path), "sha256": sha,
                        "bytes": tar_path.stat().st_size,
                        "files": sum(len(v) for v in days.values())},
        }
        (year_dir / f"{month}.index.json").write_text(
            json.dumps(idx, ensure_ascii=False, indent=2), encoding="utf-8")
    return tar_path


def _strip_docs(docs):
    """Compare docs without the cache-added _index_tokens."""
    out = []
    for d in docs:
        d = dict(d)
        d.pop("_index_tokens", None)
        out.append(d)
    return out


def _load(hub, days=30, ref_date=None):
    return ri.load_documents(
        hub, days=days, ref_date=ref_date,
        loader=ri.make_hybrid_loader(hub), tokenizer=hq.tokenize)


def _baseline(hub, days=30, ref_date=None):
    return hq.iter_documents(hub, days=days, ref_date=ref_date)


class HubFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.hub = Path(self.tmp.name)
        (self.hub / "memory").mkdir()
        (self.hub / "profile").mkdir()
        (self.hub / "memory" / "memories.jsonl").write_text(
            _j({"id": "m1", "text": "alpha memory keyword 记忆",
                "status": "active", "ts": f"{D1}T10:00:00+08:00"}) + "\n"
            + _j({"id": "m2", "text": "superseded gone",
                  "status": "superseded"}) + "\n",
            encoding="utf-8")
        (self.hub / "profile" / "constraints.md").write_text(
            "no secrets constraint 约束", encoding="utf-8")
        (self.hub / "profile" / "preferences.md").write_text(
            "prefer terse 偏好", encoding="utf-8")
        _write_day(self.hub, D1,
                   evidence=[{"event_id": "e1",
                              "ts": f"{D1}T09:00:00+08:00",
                              "text": "hot evidence 一"}],
                   sessions=[{"session_id": "s1",
                              "ts": f"{D1}T09:30:00+08:00",
                              "text": "hot session"}],
                   diary="diary hot 一")
        _write_day(self.hub, D2,
                   sessions=[{"session_id": "s2",
                              "ts": f"{D2}T08:00:00+08:00",
                              "text": "older session"}])

    def tearDown(self):
        self.tmp.cleanup()

    def assertParity(self, docs, days=30, ref_date=None):
        base = _baseline(self.hub, days=days, ref_date=ref_date)
        self.assertEqual(_strip_docs(docs), base)


class TestParity(HubFixture):
    def test_cold_first_load_matches_iter_documents(self):
        docs, counters, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertFalse(stats["fallback"])
        self.assertGreater(stats["miss_count"], 0)
        self.assertEqual(stats["hit_count"], 0)
        self.assertEqual(stats["docs"], len(docs))

    def test_warm_hit_matches_and_hits_everything(self):
        _load(self.hub)
        docs2, counters2, stats2 = _load(self.hub)
        self.assertParity(docs2)
        self.assertEqual(stats2["miss_count"], 0)
        self.assertGreater(stats2["hit_count"], 0)

    def test_counters_equal_tokenize(self):
        docs, counters, _ = _load(self.hub)
        self.assertEqual(len(docs), len(counters))
        for doc, c in zip(docs, counters):
            self.assertEqual(c, Counter(hq.tokenize(doc["text"])))
            self.assertIsInstance(doc["_index_tokens"], dict)

    def test_historical_ref_date(self):
        docs, _, _ = _load(self.hub, days=2, ref_date=TODAY - timedelta(days=1))
        base = _baseline(self.hub, days=2, ref_date=TODAY - timedelta(days=1))
        self.assertEqual(_strip_docs(docs), base)
        # D3/older never loaded; window = D2..D1 window ending yesterday.
        loaded_days = {d["record"].get("period")
                       for d in docs if d["source"] in ("evidence", "session")}
        self.assertNotIn(D3, loaded_days)

    def test_future_day_kept_for_downstream_cutoff(self):
        _write_day(self.hub, FUTURE,
                   sessions=[{"session_id": "fut",
                              "ts": f"{FUTURE}T00:00:00+08:00",
                              "text": "future session"}])
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)  # parity includes future-day docs


class TestInvalidation(HubFixture):
    def test_daily_append_invalidates(self):
        _load(self.hub)
        path = self.hub / "daily" / D1 / "sessions.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(_j({"session_id": "s-new",
                         "ts": f"{D1}T11:00:00+08:00",
                         "text": "appended session"}) + "\n")
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        texts = [d["text"] for d in docs]
        self.assertTrue(any("appended session" in t for t in texts))
        self.assertGreater(stats["miss_count"], 0)

    def test_daily_edit_and_delete(self):
        _load(self.hub)
        diary = self.hub / "daily" / D1 / "diary.md"
        diary.write_text("edited diary 修改", encoding="utf-8")
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)
        self.assertTrue(any("edited diary" in d["text"] for d in docs))

        os.remove(self.hub / "daily" / D1 / "evidence.jsonl")
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)
        self.assertFalse(any("hot evidence" in d["text"] for d in docs))

    def test_delete_whole_day_dir(self):
        _load(self.hub)
        for p in (self.hub / "daily" / D1).glob("*"):
            p.unlink()
        (self.hub / "daily" / D1).rmdir()
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)

    def test_memory_edit_and_delete(self):
        _load(self.hub)
        mp_ = self.hub / "memory" / "memories.jsonl"
        mp_.write_text(_j({"id": "m9", "text": "edited memory 改写",
                           "status": "active"}) + "\n", encoding="utf-8")
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)
        mp_.unlink()
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)
        self.assertFalse(
            any(d["source"] == "memory" for d in _strip_docs(docs)))

    def test_profile_change_only_reloads_profiles(self):
        _load(self.hub)
        (self.hub / "profile" / "preferences.md").write_text(
            "changed pref", encoding="utf-8")
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertEqual(stats["miss_count"], 1)  # profiles bucket only


class TestArchive(HubFixture):
    def _archive_d3(self):
        month = D3[:7]
        return _make_archive(self.hub, month, {
            D3: {"sessions.jsonl":
                 _j({"session_id": "cold-s", "text": "cold session 冷"}) + "\n",
                 "diary.md": "cold diary 冷"}})

    def test_cold_only_day(self):
        self._archive_d3()
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)
        self.assertTrue(any("cold session" in d["text"] for d in docs))
        # cold locator kept
        cold_docs = [d for d in docs if "!" in d["path"]]
        self.assertTrue(cold_docs)

    def test_cold_only_day_second_load_zero_misses(self):
        """Cold-only (no hot dir) days must hit on second load — regression
        for hot=None vs hot={f:None} signature divergence."""
        self._archive_d3()
        _load(self.hub)
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertEqual(stats["miss_count"], 0)
        self.assertGreater(stats["hit_count"], 0)

    def test_hot_cold_overlay_dedup(self):
        """Hot file present + archive twin: hot locator wins, no dupes."""
        ev_line = _j({"event_id": "dup1",
                      "ts": f"{D1}T01:00:00+08:00",
                      "text": "overlay evidence"})
        _write_day(self.hub, D1,
                   evidence=[json.loads(ev_line)], diary="diary hot 一")
        month = D1[:7]
        _make_archive(self.hub, month, {
            D1: {"evidence.jsonl": ev_line + "\n",
                 "sessions.jsonl":
                 _j({"session_id": "cold-only", "text": "cold only session"})
                 + "\n"}})
        docs, _, _ = _load(self.hub)
        self.assertParity(docs)
        overlay = [d for d in docs if "overlay evidence" in d["text"]]
        self.assertEqual(len(overlay), 1)
        self.assertFalse(any("!" in d["path"] for d in overlay))
        self.assertTrue(any("cold only session" in d["text"] for d in docs))

    def test_archive_rebuild_invalidates_day(self):
        self._archive_d3()
        _load(self.hub)
        month = D3[:7]
        _make_archive(self.hub, month, {
            D3: {"sessions.jsonl":
                 _j({"session_id": "cold-s2", "text": "cold session v2"})
                 + "\n"}})
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertTrue(any("cold session v2" in d["text"] for d in docs))
        self.assertFalse(any(d["text"] == "cold session 冷" for d in docs))
        self.assertGreater(stats["miss_count"], 0)

    def test_index_change_without_tar_change_invalidates(self):
        """Index sidecar edits change the composite fp even if tar untouched."""
        tar = self._archive_d3()
        _load(self.hub)
        idx_path = ri._index_path(tar)
        idx = json.loads(idx_path.read_text())
        # claim a second (nonexistent) day — fp changes, day set changes
        idx["days"].append({"date": D2, "archived": True})
        idx_path.write_text(json.dumps(idx), encoding="utf-8")
        docs, _, stats = _load(self.hub)
        # D2 now has a cold claim; loader must reflect whatever truth yields
        self.assertParity(docs)
        self.assertGreater(stats["archives_rescanned"], 0)

    def test_corrupt_archive_raises_valueerror(self):
        tar = self._archive_d3()
        _load(self.hub)  # warm
        # tamper tar bytes: same size, new mtime (fp changes -> rescan ->
        # sha256 mismatch -> ValueError)
        data = bytearray(tar.read_bytes())
        data[-2] ^= 0xFF
        tar.write_bytes(bytes(data))
        with self.assertRaises(ValueError):
            _load(self.hub)

    def test_corrupt_archive_first_load_raises(self):
        tar = self._archive_d3()
        data = bytearray(tar.read_bytes())
        data[-2] ^= 0xFF
        tar.write_bytes(bytes(data))
        with self.assertRaises(ValueError):
            _load(self.hub)


class TestStatsAndStorage(HubFixture):
    def test_stats_keys_compact_and_bounded(self):
        _load(self.hub)
        docs, counters, stats = _load(self.hub)
        self.assertLessEqual(
            set(stats),
            {"docs", "hit_count", "miss_count", "uncacheable",
             "elapsed_ms", "fallback", "archives_tracked",
             "archives_rescanned", "fallback_reason", "cold_issues"})
        self.assertIsInstance(stats["elapsed_ms"], float)
        self.assertIsInstance(stats["docs"], int)
        # no path/bucket-key leakage
        for v in stats.values():
            self.assertNotIsInstance(v, list)
            if isinstance(v, str):
                self.assertNotIn(str(self.hub), v)

    def test_db_private_permissions(self):
        _load(self.hub)
        state = self.hub / "state"
        self.assertTrue((state / "retrieval_index.sqlite3").exists())
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)
        for p in state.glob("retrieval_index.sqlite3*"):
            self.assertEqual(p.stat().st_mode & 0o777, 0o600, str(p))

    def test_corrupt_db_falls_back_fresh(self):
        _load(self.hub)
        db = self.hub / "state" / "retrieval_index.sqlite3"
        db.write_bytes(b"not a sqlite database")
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertTrue(stats["fallback"])
        self.assertIn("fallback_reason", stats)
        self.assertNotIn(str(self.hub), stats["fallback_reason"])

    def test_symlink_db_falls_back(self):
        _load(self.hub)
        state = self.hub / "state"
        db = state / "retrieval_index.sqlite3"
        real = self.hub / "real.sqlite3"
        db.rename(real)
        for suffix in ("-wal", "-shm"):
            p = Path(str(db) + suffix)
            if p.exists():
                p.unlink()
        os.symlink(real, db)
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertTrue(stats["fallback"])

class TestSourceRace(HubFixture):
    def test_source_change_mid_load_never_cached_under_old_sig(self):
        """Force a miss, then mutate the file INSIDE the loader: the
        snapshot->load->re-verify dance must retry once and cache under the
        NEW signature only — never a torn payload under the stale sig."""
        _load(self.hub)  # warm everything
        diary = self.hub / "daily" / D1 / "diary.md"
        base_loader = ri.make_hybrid_loader(self.hub)
        raced = {"done": False}

        def racing_loader(day):
            if day == D1 and not raced["done"]:
                raced["done"] = True
                diary.write_text("raced diary edit 竞态", encoding="utf-8")
            return base_loader(day)

        # Sabotage the day-signature lookup so bucket_get misses and the
        # loader is invoked; _try_stable then re-verifies the real sig.
        orig = ri._day_signature
        calls = {"n": 0}

        def flaky_sig(day, src):
            calls["n"] += 1
            if calls["n"] == 1:
                return "stale-view"   # first lookup misses
            return orig(day, src)

        saved = ri._day_signature
        ri._day_signature = flaky_sig
        try:
            docs, _, stats = ri.load_documents(
                self.hub, loader=racing_loader, tokenizer=hq.tokenize)
        finally:
            ri._day_signature = saved
        self.assertParity(docs)
        self.assertTrue(any("raced diary" in d["text"] for d in docs))
        # The raced payload was cached under the post-race signature, so a
        # plain reload must hit (miss_count==0) and stay in parity.
        docs, _, stats = _load(self.hub)
        self.assertParity(docs)
        self.assertEqual(stats["miss_count"], 0)


class TestConcurrency(HubFixture):
    def test_multiprocess_parallel_loads_consistent(self):
        _load(self.hub)  # schema warm
        # fork crashes (SIGSEGV) on macOS in this interpreter; spawn is the
        # safe portable context — module-level worker is picklable.
        method = "spawn" if sys.platform == "darwin" else "fork"
        try:
            ctx = mp.get_context(method)
        except ValueError:
            ctx = mp.get_context("spawn")
        q = ctx.Queue()
        procs = [ctx.Process(target=_worker_load,
                             args=(str(self.hub), q)) for _ in range(4)]
        for p in procs:
            p.start()
        results = [q.get(timeout=60) for _ in procs]
        for p in procs:
            p.join(timeout=60)
            self.assertEqual(p.exitcode, 0)
        base = _strip_docs(_baseline(self.hub))
        for res in results:
            self.assertNotIsInstance(res, str)  # error string, not doc list
            self.assertEqual(res, base)


def _worker_load(hub_str, q):
    try:
        hub = Path(hub_str)
        docs, _c, _s = _load(hub)
        q.put(_strip_docs(docs))
    except Exception as exc:  # surface failure, never crash silently
        q.put(f"ERROR:{type(exc).__name__}:{exc}")


class TestCLI(HubFixture):
    def test_status_rebuild_clear(self):
        import subprocess
        script = str(Path(ri.__file__).resolve())
        env = dict(os.environ)
        r = subprocess.run([sys.executable, script, "--hub", str(self.hub),
                            "rebuild", "--days", "30"],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertGreater(out["rebuilt_docs"], 0)
        r = subprocess.run([sys.executable, script, "--hub", str(self.hub),
                            "status"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["exists"])
        self.assertGreater(out["cached_docs"], 0)
        r = subprocess.run([sys.executable, script, "--hub", str(self.hub),
                            "clear"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(
            (self.hub / "state" / "retrieval_index.sqlite3").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
