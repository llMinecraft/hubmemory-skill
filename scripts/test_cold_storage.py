#!/usr/bin/env python3
"""Cold archive read tests. All fixtures live in a temporary HUBMEMORY_HOME;
nothing under ~/hubmemory or this repo's live data is touched."""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cold_storage as cold
import hybrid_query as hq
import manifest

NOW = "2026-10-21T09:00:00+08:00"
CWD = "/projects/alpha"


def make_day(home: Path, day: str, files: dict[str, str]) -> Path:
    """Write a hot daily/<day>/ dir."""
    d = home / "daily" / day
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")
    return d


def make_archive(home: Path, month: str,
                 days: dict[str, dict[str, str]],
                 *, write_index: bool = True,
                 index_sha: str | None = "auto") -> Path:
    """Pack a month archive exactly like archive.py does (stdlib tar+gz,
    members '<month>/<day>/<file>') plus the .index.json sidecar."""
    year_dir = home / "archive" / month[:4]
    year_dir.mkdir(parents=True, exist_ok=True)
    tar_path = year_dir / f"{month}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        for day in sorted(days):
            for name in sorted(days[day]):
                data = days[day][name].encode("utf-8")
                info = tarfile.TarInfo(f"{month}/{day}/{name}")
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    if write_index:
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


def ev(event_id, ts, text, **kw):
    return dict(event_id=event_id, ts=ts, text=text, role="assistant",
                agent="dsh", session_id="s-" + event_id, cwd=CWD, **kw)


def jsonl(records):
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)


class ColdStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        cold.clear_caches()

    # -- inventory ----------------------------------------------------------

    def test_iter_days_unions_hot_and_cold(self):
        make_day(self.home, "2026-10-20", {"sessions.jsonl": jsonl(
            [ev("h1", "2026-10-20T10:00:00+08:00", "hot day event")])})
        make_archive(self.home, "2026-09", {
            "2026-09-28": {"sessions.jsonl": jsonl(
                [ev("c1", "2026-09-28T10:00:00+08:00", "cold one")])},
            "2026-09-29": {"diary.md": "# 2026-09-29\n"},
        })
        days = cold.iter_days(self.home)
        self.assertEqual(days, ["2026-09-28", "2026-09-29", "2026-10-20"])

    def test_read_day_text_prefers_hot(self):
        files = {"diary.md": "COLD VERSION"}
        make_archive(self.home, "2026-10", {"2026-10-20": files})
        make_day(self.home, "2026-10-20", {"diary.md": "HOT VERSION"})
        self.assertEqual(
            cold.read_day_text(self.home, "2026-10-20", "diary.md"),
            "HOT VERSION")
        self.assertEqual(
            cold.read_archive_text(self.home, "2026-10-20", "diary.md"),
            "COLD VERSION")

    def test_read_day_text_cold_only(self):
        make_archive(self.home, "2026-09", {
            "2026-09-30": {"diary.md": "# archived diary\n"}})
        self.assertEqual(cold.read_day_text(self.home, "2026-09-30",
                                            "diary.md"), "# archived diary\n")
        self.assertIsNone(cold.read_day_text(self.home, "2026-09-30",
                                             "evidence.jsonl"))
        self.assertIsNone(cold.read_day_text(self.home, "2026-08-01",
                                             "diary.md"))

    def test_no_disk_extraction(self):
        make_archive(self.home, "2026-09", {
            "2026-09-30": {"diary.md": "x", "sessions.jsonl": "{}\n"}})
        cold.read_day_text(self.home, "2026-09-30", "diary.md")
        cold.iter_day_records(self.home, "2026-09-30")
        self.assertFalse((self.home / "daily").exists())
        self.assertTrue(list((self.home / "archive").glob("**/*.tar.gz")))

    # -- provenance -----------------------------------------------------------

    def test_cold_locator_format(self):
        make_archive(self.home, "2026-09", {
            "2026-09-30": {"sessions.jsonl": jsonl(
                [ev("e1", "2026-09-30T10:00:00+08:00", "first"),
                 ev("e2", "2026-09-30T11:00:00+08:00", "second")])}})
        rows = cold.iter_day_records(self.home, "2026-09-30")
        self.assertEqual(len(rows), 2)
        loc = rows[1]["locator"]
        self.assertRegex(loc, r"2026-09\.tar\.gz!2026-09/2026-09-30/"
                              r"sessions\.jsonl#L2$")
        self.assertTrue(Path(loc.split("!")[0]).is_absolute())

    # -- overlay --------------------------------------------------------------

    def test_overlay_dedup_preserves_cold_only_events(self):
        a = ev("a", "2026-10-19T10:00:00+08:00", "shared event")
        b = ev("b", "2026-10-19T11:00:00+08:00", "hot only")
        c = ev("c", "2026-10-19T12:00:00+08:00", "cold only")
        make_day(self.home, "2026-10-19", {"sessions.jsonl": jsonl([a, b])})
        make_archive(self.home, "2026-10",
                     {"2026-10-19": {"sessions.jsonl": jsonl([a, c])}})
        rows = cold.iter_day_records(self.home, "2026-10-19")
        # hot rows first (original live locators), 'a' cold twin flagged dup,
        # 'c' preserved with a tar locator — nothing silently hidden.
        self.assertEqual([r["record"]["event_id"] for r in rows],
                         ["a", "b", "a", "c"])
        self.assertFalse(rows[0]["dup"])      # first 'a' copy is hot
        self.assertEqual(rows[0]["source"], "hot")
        self.assertIn("#L1", rows[0]["locator"])
        cold_rows = [r for r in rows if r["source"] == "cold"]
        self.assertTrue(cold_rows[0]["dup"])
        self.assertFalse(cold_rows[1]["dup"])
        self.assertIn("!", cold_rows[1]["locator"])

    def test_same_event_id_divergent_content_is_conflict_not_dup(self):
        hot_ev = ev("same", "2026-10-19T10:00:00+08:00", "version one")
        cold_ev = ev("same", "2026-10-19T10:00:00+08:00", "version two")
        make_day(self.home, "2026-10-19", {"sessions.jsonl": jsonl([hot_ev])})
        make_archive(self.home, "2026-10",
                     {"2026-10-19": {"sessions.jsonl": jsonl([cold_ev])}})
        rows = cold.iter_day_records(self.home, "2026-10-19")
        self.assertEqual(len(rows), 2)          # both kept — no silent merge
        self.assertFalse(rows[1]["dup"])
        self.assertIn("conflict_of", rows[1])
        self.assertIn("#L1", rows[1]["conflict_of"])

    def test_conflict_surfaces_in_search(self):
        hot_ev = ev("same", "2026-10-19T10:00:00+08:00", "量子密钥分发方案A")
        cold_ev = ev("same", "2026-10-19T10:00:00+08:00", "量子密钥分发方案B")
        make_day(self.home, "2026-10-19", {"sessions.jsonl": jsonl([hot_ev])})
        make_archive(self.home, "2026-10",
                     {"2026-10-19": {"sessions.jsonl": jsonl([cold_ev])}})
        hits = hq.search(self.home, "量子密钥分发", as_of=NOW, days=30)
        self.assertEqual(len(hits), 2)
        conflicted = [h for h in hits if h.get("_conflict_of")]
        self.assertEqual(len(conflicted), 1)
        self.assertIn(".tar.gz!", conflicted[0]["_path"])

    def test_read_day_jsonl_merges_without_hiding_originals(self):
        a = ev("a", "2026-10-19T10:00:00+08:00", "shared")
        b = ev("b", "2026-10-19T11:00:00+08:00", "hot only")
        c = ev("c", "2026-10-19T12:00:00+08:00", "cold only")
        make_day(self.home, "2026-10-19", {"sessions.jsonl": jsonl([a, b])})
        make_archive(self.home, "2026-10",
                     {"2026-10-19": {"sessions.jsonl": jsonl([a, c])}})
        merged = cold.read_day_jsonl(self.home, "2026-10-19")
        ids = [json.loads(l)["event_id"] for l in merged.splitlines()]
        self.assertEqual(ids, ["a", "b", "c"])   # hot first, cold-only appended
        # merge=True on read_day_text gives the same canonical merged text
        self.assertEqual(
            cold.read_day_text(self.home, "2026-10-19", "sessions.jsonl",
                               merge=True), merged)
        # default (non-merge) still prefers the hot file byte-for-byte
        self.assertEqual(
            cold.read_day_text(self.home, "2026-10-19", "sessions.jsonl"),
            jsonl([a, b]))

    def test_read_day_jsonl_keeps_conflicting_versions(self):
        v1 = ev("x", "2026-10-19T10:00:00+08:00", "version one")
        v2 = ev("x", "2026-10-19T10:00:00+08:00", "version two")
        make_day(self.home, "2026-10-19", {"sessions.jsonl": jsonl([v1])})
        make_archive(self.home, "2026-10",
                     {"2026-10-19": {"sessions.jsonl": jsonl([v2])}})
        merged = cold.read_day_jsonl(self.home, "2026-10-19")
        texts = [json.loads(l)["text"] for l in merged.splitlines()]
        self.assertEqual(texts, ["version one", "version two"])

    def test_hot_symlink_escape_not_served(self):
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        secret = Path(outside.name) / "secret.txt"
        secret.write_text("TOP SECRET")
        day_dir = make_day(self.home, "2026-10-19", {})
        os.symlink(secret, day_dir / "diary.md")
        self.assertIsNone(cold.read_day_text(self.home, "2026-10-19",
                                             "diary.md"))
        self.assertEqual(cold.iter_day_files(self.home, "2026-10-19"), [])

    def test_hot_symlink_inside_hub_served(self):
        real = self.home / "store" / "real_diary.md"
        real.parent.mkdir(parents=True)
        real.write_text("inside diary")
        day_dir = make_day(self.home, "2026-10-19", {})
        os.symlink(real, day_dir / "diary.md")
        self.assertEqual(cold.read_day_text(self.home, "2026-10-19",
                                            "diary.md"), "inside diary")

    def test_atomic_same_size_replace_invalidates_cache(self):
        import os as _os
        month, day = "2026-09", "2026-09-15"
        tar = make_archive(self.home, month,
                           {day: {"diary.md": "AAAA original content"}})
        first = cold.read_day_text(self.home, day, "diary.md")
        self.assertEqual(first, "AAAA original content")
        # rebuild tar+index atomically, same content length, forced same mtime
        tmp = self.home / "archive" / "tmp_rebuild"
        new_tar = make_archive(tmp, month,
                               {day: {"diary.md": "BBBB replaced content"}})
        new_idx = tmp / "archive" / month[:4] / f"{month}.index.json"
        idx_path = self.home / "archive" / month[:4] / f"{month}.index.json"
        old_st = tar.stat()
        _os.replace(new_tar, tar)
        _os.replace(new_idx, idx_path)
        _os.utime(tar, ns=(old_st.st_atime_ns, old_st.st_mtime_ns))
        _os.utime(idx_path, ns=(old_st.st_atime_ns, old_st.st_mtime_ns))
        self.assertEqual(cold.read_day_text(self.home, day, "diary.md"),
                         "BBBB replaced content")

    def test_archive_status_targeted(self):
        tar = make_archive(self.home, "2026-09",
                           {"2026-09-15": {"sessions.jsonl": "{}\n"}})
        make_archive(self.home, "2026-08",
                     {"2026-08-15": {"sessions.jsonl": "{}\n"}})
        # verify=True on one month does not hash the other
        calls = []
        orig = hashlib.sha256
        def spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        import unittest.mock as mock
        with mock.patch.object(cold.hashlib, "sha256", side_effect=spy):
            st = cold.archive_status(self.home, "2026-09")
        self.assertEqual(st["status"], "ok")
        self.assertTrue(st["verified"])
        self.assertTrue(st["sha256"])
        self.assertEqual(len(calls), 1)          # exactly one tar hashed
        self.assertIsNone(cold.archive_status(self.home, "2026-07"))
        self.assertIsNone(cold.archive_status(self.home, "bad-month"))
        # verify=False metadata path
        st2 = cold.archive_status(self.home, "2026-08", verify=False)
        self.assertEqual(st2["status"], "not_checked")
        # corrupt month fails closed
        with tar.open("ab") as fh:
            fh.write(b"corruption")
        cold.clear_caches()
        st3 = cold.archive_status(self.home, "2026-09")
        self.assertEqual(st3["status"], "corrupt")
        self.assertFalse(st3["verified"])
        self.assertTrue(st3["issues"])

    def test_archives_verify_false_never_hashes(self):
        tar = make_archive(self.home, "2026-09",
                           {"2026-09-15": {"sessions.jsonl": "{}\n"}})
        calls = []
        orig = hashlib.sha256
        def spy(*a, **k):
            calls.append(1)
            return orig(*a, **k)
        import unittest.mock as mock
        with mock.patch.object(cold.hashlib, "sha256", side_effect=spy):
            rows = cold.archives(self.home, verify=False)
        self.assertFalse(calls, "verify=False must not hash the tarball")
        self.assertEqual(rows[0]["status"], "not_checked")
        self.assertEqual(rows[0]["day_count"], 1)

    def test_hybrid_overlay_no_double_hits(self):
        a = ev("a", "2026-10-19T10:00:00+08:00", "磁悬浮轴承设计已保存 docs/F.md")
        c = ev("c", "2026-10-19T12:00:00+08:00", "磁悬浮轴承冷存独有记录")
        make_day(self.home, "2026-10-19", {"sessions.jsonl": jsonl([a])})
        make_archive(self.home, "2026-10",
                     {"2026-10-19": {"sessions.jsonl": jsonl([a, c])}})
        hits = hq.search(self.home, "磁悬浮轴承", cwd=CWD, as_of=NOW, days=30)
        ids = [h["event_id"] for h in hits]
        self.assertEqual(ids.count("a"), 1)       # event_id dedup
        self.assertIn("c", ids)
        cold_hit = next(h for h in hits if h["event_id"] == "c")
        self.assertIn(".tar.gz!", cold_hit["_path"])
        self.assertIsInstance(cold_hit["_source_line"], int)

    # -- retrieval ------------------------------------------------------------

    def test_search_finds_archived_event(self):
        make_archive(self.home, "2026-09", {
            "2026-09-15": {"sessions.jsonl": jsonl([ev(
                "arch", "2026-09-15T09:00:00+08:00",
                "已完成[归档月报告](<docs/ARCH.md>)")])}})
        hits = hq.search(self.home, "归档月报告", cwd=CWD, as_of=NOW, days=60)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["event_id"], "arch")
        self.assertIn("2026-09.tar.gz!2026-09/2026-09-15/sessions.jsonl",
                      hits[0]["_path"])

    def test_as_of_applies_to_archived_events(self):
        make_archive(self.home, "2026-09", {
            "2026-09-15": {"sessions.jsonl": jsonl([ev(
                "late", "2026-09-15T23:00:00+08:00", "冷存档事件")])}})
        hits = hq.search(self.home, "冷存档", as_of="2026-09-15T12:00:00+08:00",
                         days=60)
        self.assertEqual(hits, [])

    def test_archived_diary_is_report_source(self):
        make_archive(self.home, "2026-09", {
            "2026-09-15": {"diary.md": "# diary\n\n完成了月度回归测试。\n"}})
        hits = hq.search(self.home, "回归测试", as_of=NOW, days=60)
        self.assertTrue(hits)
        self.assertEqual(hits[0]["_source"], "report")
        self.assertIn(".tar.gz!", hits[0]["_path"])

    def test_hot_only_days_never_decompress(self):
        make_archive(self.home, "2026-09", {
            "2026-09-15": {"sessions.jsonl": "{}\n"}})
        make_day(self.home, "2026-10-20", {"sessions.jsonl": jsonl([ev(
            "hot", "2026-10-20T10:00:00+08:00", "热数据检索")])})
        hits = hq.search(self.home, "热数据检索", as_of=NOW, days=7)
        self.assertEqual(hits[0]["event_id"], "hot")
        # last-7 search on hot days must not have decompressed any member
        self.assertFalse(cold._day_cache)

    # -- integrity --------------------------------------------------------------

    def test_corrupt_archive_fails_closed(self):
        tar = make_archive(self.home, "2026-09", {
            "2026-09-15": {"sessions.jsonl": jsonl([ev(
                "x", "2026-09-15T09:00:00+08:00", "冷存档事件")])}})
        with tar.open("ab") as fh:      # flip bytes after index was written
            fh.write(b"corruption")
        self.assertIsNone(cold.read_day_text(self.home, "2026-09-15",
                                             "sessions.jsonl"))
        status = cold.archives(self.home)[0]
        self.assertEqual(status["status"], "corrupt")
        self.assertFalse(status["verified"])
        issues = cold.last_issues()
        self.assertTrue(issues)
        # CLI/lead warning path: issue entries name the archive + cause
        self.assertEqual(issues[-1]["archive"], str(tar))
        self.assertIn("sha256", issues[-1]["issue"])
        hits = hq.search(self.home, "冷存档事件", as_of=NOW, days=60)
        self.assertEqual(hits, [])                      # nothing leaks

    def test_unreadable_indexless_archive_records_issue(self):
        year_dir = self.home / "archive" / "2026"
        year_dir.mkdir(parents=True)
        tar = year_dir / "2026-09.tar.gz"
        tar.write_bytes(b"not a gzip stream at all")
        self.assertEqual(cold.iter_days(self.home), [])  # fail closed
        self.assertIsNone(cold.read_day_text(self.home, "2026-09-15",
                                             "sessions.jsonl"))
        self.assertTrue(any("scan failed" in i["issue"] or "unreadable" in i["issue"]
                            for i in cold.last_issues()))

    def test_legacy_archive_without_index_is_served_unverified(self):
        make_archive(self.home, "2026-09", {
            "2026-09-15": {"sessions.jsonl": jsonl([ev(
                "leg", "2026-09-15T09:00:00+08:00", "legacy 存档")])}},
            write_index=False)
        self.assertIn("2026-09-15", cold.iter_days(self.home))
        text = cold.read_day_text(self.home, "2026-09-15", "sessions.jsonl")
        self.assertIn("legacy", text)
        self.assertEqual(cold.archives(self.home)[0]["status"], "unverified")

    def test_secrets_never_served(self):
        make_archive(self.home, "2026-09", {"2026-09-15": {
            ".env": "KEY=x", "id_rsa": "PRIVATE", "credentials.json": "{}",
            "sessions.jsonl": "{}\n"}})
        for name in (".env", "id_rsa", "credentials.json"):
            self.assertIsNone(cold.read_day_text(self.home, "2026-09-15", name))
        names = [f["name"] for f in cold.iter_day_files(self.home,
                                                        "2026-09-15")]
        self.assertEqual(names, ["sessions.jsonl"])

    def test_traversal_members_ignored(self):
        year_dir = self.home / "archive" / "2026"
        year_dir.mkdir(parents=True)
        tar_path = year_dir / "2026-09.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            data = b"evil"
            info = tarfile.TarInfo("../escape.md")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            good = tarfile.TarInfo("2026-09/2026-09-15/diary.md")
            gd = b"safe diary"
            good.size = len(gd)
            tar.addfile(good, io.BytesIO(gd))
        self.assertEqual(cold.read_day_text(self.home, "2026-09-15",
                                            "diary.md"), "safe diary")
        self.assertFalse((self.home / "escape.md").exists())

    # -- manifest / hub integration -------------------------------------------

    def _patch_manifest(self):
        manifest.HUB_HOME = self.home
        manifest.DAILY_DIR = self.home / "daily"
        manifest.MANIFEST_PATH = self.home / "MANIFEST.md"
        manifest.LIVE_INDEX = self.home / "live" / "index.json"
        manifest.TASKS_PATH = self.home / "live" / "tasks.json"
        manifest.REQUESTS_DIR = self.home / "reports" / "requests"
        manifest.MEMORY_PATH = self.home / "memory" / "memories.jsonl"

    def test_manifest_lists_archived_months_and_days(self):
        self._patch_manifest()
        day = (manifest.datetime.now().date() - manifest.timedelta(days=2))
        month = day.strftime("%Y-%m")
        make_archive(self.home, month, {
            day.isoformat(): {"diary.md":
                              "# d\n\n## Agent-readable summary\n```json\n"
                              '{"tasks": [], "task_count": 0}\n```\n'}})
        data = manifest.build()
        self.assertTrue(any(m["month"] == month
                            for m in data["archived_months"]))
        row = next(d for d in data["days"] if d["date"] == day.isoformat())
        self.assertTrue(row.get("cold"))
        self.assertIn(".tar.gz!", row["ref"])
        text = manifest.render(data)
        self.assertIn("Archived months", text)

    def test_manifest_hot_only_still_fast(self):
        self._patch_manifest()
        today = manifest.datetime.now().date().isoformat()
        make_day(self.home, today, {"diary.md": "# today\n"})
        make_archive(self.home, "2026-09",
                     {"2026-09-15": {"diary.md": "x"}})
        manifest.day_summaries(7)
        self.assertFalse(cold._day_cache)   # no decompression for hot days

    def test_hub_day_view_reads_archived_diary(self):
        import hub as hubmod
        old_home = hubmod.HUB_HOME
        hubmod.HUB_HOME = self.home
        self.addCleanup(setattr, hubmod, "HUB_HOME", old_home)
        diary = ("# 2026-09-15\n\n## Agent-readable summary\n```json\n"
                 '{"task_count": 1, "done": 1, "tasks": [{"status": '
                 '"completed", "goal": "archived work", "conclusion": "ok"}]'
                 "}\n```\n")
        make_archive(self.home, "2026-09",
                     {"2026-09-15": {"diary.md": diary}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = hubmod.day_view("2026-09-15")
        self.assertEqual(rc, 0)
        self.assertIn("archived work", buf.getvalue())

    def test_iter_day_files_inventory(self):
        make_archive(self.home, "2026-09", {"2026-09-15": {
            "diary.md": "x", "sessions.jsonl": "{}\n"}})
        make_day(self.home, "2026-09-15", {"diary.md": "hot diary"})
        files = {f["name"]: f for f in cold.iter_day_files(self.home,
                                                          "2026-09-15")}
        self.assertEqual(files["diary.md"]["source"], "hot")
        self.assertEqual(files["sessions.jsonl"]["source"], "cold")
        self.assertIn("!", files["sessions.jsonl"]["locator"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
