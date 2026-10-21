#!/usr/bin/env python3
"""Archive safety test suite — all fixtures in TemporaryDirectory, never
touches the real ~/hubmemory.  Run:  python3 scripts/test_archive.py
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import archive as arch        # noqa: E402
import storage_lock           # noqa: E402

TODAY = date(2026, 10, 21)
OLD_MONTH = "2026-08"                     # ended + grace long past
GRACE_MONTH = "2026-10"                   # current month (never archivable)
PREV_MONTH = "2026-09"                    # ended Sep 30; archivable Oct 8+


def ev(eid: str, day: str = "2026-08-01", text: str = "x") -> dict:
    return {"event_id": eid, "session_id": "s1", "role": "user",
            "ts": f"{day}T10:00:00+00:00", "text": text}


def jlines(*events) -> str:
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


class HubTest(unittest.TestCase):
    """Base: isolated hub tree + patched module globals."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        (self.home / "daily").mkdir(parents=True)
        self._saved = {k: getattr(arch, k) for k in
                       ("HUB_HOME", "DAILY_DIR", "ARCHIVE_DIR",
                        "REQUESTS_DIR", "REPORTS_LOCK", "MONTHLY_DIR")}
        arch.HUB_HOME = self.home
        arch.DAILY_DIR = self.home / "daily"
        arch.ARCHIVE_DIR = self.home / "archive"
        arch.REQUESTS_DIR = self.home / "reports" / "requests"
        arch.REPORTS_LOCK = self.home / "reports" / ".lock"
        arch.MONTHLY_DIR = self.home / "monthly"

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(arch, k, v)
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------

    def mkday(self, day: str, files: dict[str, str] | None = None,
              empty_subdir: bool = False) -> Path:
        d = arch.DAILY_DIR / day
        d.mkdir(parents=True, exist_ok=True)
        for name, content in (files or {}).items():
            p = d / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        if empty_subdir:
            (d / "empty" / "nested").mkdir(parents=True)
        return d

    def request(self, kind: str, period: str, status: str = "pending") -> Path:
        arch.REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        p = arch.REQUESTS_DIR / f"{kind}-{period}.json"
        req = {"id": f"{kind}:{period}", "kind": kind, "period": period,
               "status": status}
        if status == "claimed":
            req["claimed_at"] = datetime.now().astimezone().isoformat()
        p.write_text(json.dumps(req), encoding="utf-8")
        return p

    def tar_members(self, month: str) -> dict[str, bytes]:
        tar_path = arch.ARCHIVE_DIR / month[:4] / f"{month}.tar.gz"
        out = {}
        with tarfile.open(tar_path, "r:gz") as tar:
            for m in tar:
                if m.isfile():
                    out[m.name] = tar.extractfile(m).read()
        return out

    def make_legacy_tar(self, month: str, days: dict[str, dict[str, str]],
                        with_index: bool = False) -> Path:
        year = arch.ARCHIVE_DIR / month[:4]
        year.mkdir(parents=True, exist_ok=True)
        tar_path = year / f"{month}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for day, files in days.items():
                for name, content in files.items():
                    data = content.encode()
                    info = tarfile.TarInfo(f"{month}/{day}/{name}")
                    info.size = len(data)
                    tar.addfile(info, io.BytesIO(data))
        if with_index:
            sha = hashlib.sha256(tar_path.read_bytes()).hexdigest()
            idx = {"month": month, "day_count": len(days),
                   "days": [{"date": d, "archived": True} for d in days],
                   "archive": {"path": str(tar_path), "sha256": sha,
                               "bytes": tar_path.stat().st_size}}
            (year / f"{month}.index.json").write_text(json.dumps(idx))
        return tar_path

    def health(self, *, version=None, age_sec: float = 5):
        st = self.home / "state"
        st.mkdir(parents=True, exist_ok=True)
        hb = datetime.now().astimezone() - timedelta(seconds=age_sec)
        data = {"pid": os.getpid(), "heartbeat_at": hb.isoformat()}
        if version is not None:
            data["storage_lock_version"] = version
        (st / "health.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# eligibility / grace / refusal
# ---------------------------------------------------------------------------

class TestEligibility(HubTest):

    def test_current_and_future_months_refused(self):
        self.mkday("2026-10-01", {"sessions.jsonl": jlines(ev("a", "2026-10-01"))})
        self.mkday("2026-11-05", {"sessions.jsonl": jlines(ev("b", "2026-11-05"))})
        self.assertEqual(arch.eligible_months(TODAY), [])
        r = arch.archive_month("2026-10", force=True, today=TODAY)
        self.assertEqual(r["error"], "month_not_finished")
        r = arch.archive_month("2026-11", force=True, today=TODAY)
        self.assertEqual(r["error"], "month_not_finished")

    def test_month_end_grace_boundary(self):
        # PREV_MONTH ends Sep 30 → archivable only Oct 8+ (grace 7 + 1)
        self.mkday("2026-09-02", {"sessions.jsonl": jlines(ev("g", "2026-09-02")),
                                  "diary.md": "d"})
        self.request("daily", "2026-09-02", "done")
        for day_num, want in [(7, False), (8, True), (9, True)]:
            t = date(2026, 10, day_num)
            got = PREV_MONTH in arch.eligible_months(t)
            self.assertEqual(got, want, f"day {day_num}")
        # --month without --force on Oct 5: blocked by grace
        r = arch.archive_month(PREV_MONTH, today=date(2026, 10, 5))
        self.assertEqual(r["error"], "grace_window")
        # force skips grace but nothing else
        r = arch.archive_month(PREV_MONTH, force=True, today=date(2026, 10, 5))
        self.assertTrue(r.get("ok"), r)

    def test_sparse_month_uses_calendar_end_not_last_day(self):
        # Only an early-September day exists: month still waits until Oct 8.
        self.mkday("2026-09-02", {"sessions.jsonl": jlines(ev("s", "2026-09-02"))})
        self.assertNotIn(PREV_MONTH, arch.eligible_months(date(2026, 10, 7)))

    def test_bad_month_rejected(self):
        self.assertEqual(arch.archive_month("2026-13")["error"], "bad_month")
        self.assertEqual(arch.archive_month("../etc")["error"], "bad_month")


# ---------------------------------------------------------------------------
# core archive + integrity
# ---------------------------------------------------------------------------

class TestArchive(HubTest):

    def _archivable_day(self):
        return self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("e1"), ev("e2")),
            "evidence.jsonl": jlines({"kind": "e", "id": 1}),
            "diary.md": "# 2026-08-01 diary\n",
            "nested/deep.txt": "deep"})

    def test_archive_roundtrip_lossless(self):
        self._archivable_day()
        src = arch.DAILY_DIR / "2026-08-01" / "nested/deep.txt"
        src.chmod(0o640)
        os.utime(src, (1700000000, 1700000000))
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertFalse((arch.DAILY_DIR / "2026-08-01").exists())
        members = self.tar_members(OLD_MONTH)
        self.assertIn("2026-08/2026-08-01/nested/deep.txt", members)
        self.assertIn("2026-08/2026-08-01/sessions.jsonl", members)
        # manifest + index written, digests match
        manifest = json.loads((arch.ARCHIVE_DIR / "2026" /
                               "2026-08.manifest.json").read_text())
        for name, want in manifest.items():
            got = members[name]
            self.assertEqual(hashlib.sha256(got).hexdigest(), want["sha256"])
            self.assertEqual(len(got), want["bytes"])
        idx = json.loads((arch.ARCHIVE_DIR / "2026" /
                          "2026-08.index.json").read_text())
        self.assertEqual(idx["archive"]["files"], len(members))
        self.assertEqual(idx["generation"], 1)
        # mode + mtime recorded
        deep = manifest["2026-08/2026-08-01/nested/deep.txt"]
        self.assertEqual(deep["mode"], 0o640)
        self.assertEqual(deep["mtime"], 1700000000)
        # extraction restores mode + mtime
        ex = arch.extract_month(OLD_MONTH)
        self.assertTrue(ex["ok"], ex)
        restored = arch.DAILY_DIR / "2026-08-01/nested/deep.txt"
        self.assertEqual(restored.stat().st_mode & 0o777, 0o640)
        self.assertEqual(int(restored.stat().st_mtime), 1700000000)
        self.assertEqual(restored.read_text(), "deep")

    def test_empty_subdir_preserved(self):
        self.mkday("2026-08-02",
                   {"sessions.jsonl": jlines(ev("ed", "2026-08-02")),
                    "diary.md": "d"}, empty_subdir=True)
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        names = set()
        with tarfile.open(arch.ARCHIVE_DIR / "2026" / "2026-08.tar.gz",
                          "r:gz") as tar:
            for m in tar:
                if m.isdir():
                    names.add(m.name)
        self.assertIn("2026-08/2026-08-02/empty/nested", names)
        ex = arch.extract_month(OLD_MONTH)
        self.assertTrue(ex["ok"], ex)
        self.assertTrue((arch.DAILY_DIR / "2026-08-02/empty/nested").is_dir())

    def test_verify_command(self):
        self._archivable_day()
        arch.archive_month(OLD_MONTH, today=TODAY)
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)
        # flip a byte inside the gzip payload
        tar_path = arch.ARCHIVE_DIR / "2026" / "2026-08.tar.gz"
        data = bytearray(tar_path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        tar_path.write_bytes(bytes(data))
        v = arch.verify_month(OLD_MONTH)
        self.assertFalse(v["ok"])

    def test_verify_digest_mismatch_is_not_success(self):
        self._archivable_day()
        result = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(result["ok"], result)
        idx = arch.ARCHIVE_DIR / "2026" / "2026-08.index.json"
        data = json.loads(idx.read_text())
        data["archive"]["sha256"] = "0" * 64
        idx.write_text(json.dumps(data))
        v = arch.verify_month(OLD_MONTH)
        self.assertFalse(v["ok"], v)
        self.assertFalse(v["verified"], v)

    def test_verify_statuses_legacy_and_verified(self):
        """ok is a digest guarantee: a mismatch is never ok; a digest-less
        legacy archive is ok but explicitly unverified."""
        self._archivable_day()
        arch.archive_month(OLD_MONTH, today=TODAY)
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)
        self.assertTrue(v["verified"])
        self.assertTrue(v["per_file_verified"])
        self.tearDown()
        self.setUp()
        self.make_legacy_tar(OLD_MONTH, {
            "2026-08-01": {"sessions.jsonl": jlines(ev("lg")), "diary.md": "d"}},
            with_index=False)
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)
        self.assertFalse(v["verified"])
        self.assertFalse(v["per_file_verified"])

    def test_truncated_gzip_refused(self):
        self._archivable_day()
        arch.archive_month(OLD_MONTH, today=TODAY)
        tar_path = arch.ARCHIVE_DIR / "2026" / "2026-08.tar.gz"
        tar_path.write_bytes(tar_path.read_bytes()[:-100])   # cut trailer
        r = arch.extract_month(OLD_MONTH)
        self.assertFalse(r["ok"])
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertFalse(r["ok"])

    def test_no_tmp_files_left(self):
        self._archivable_day()
        arch.archive_month(OLD_MONTH, today=TODAY)
        leftovers = [p for p in (arch.ARCHIVE_DIR / "2026").iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])
        self.assertFalse((arch.ARCHIVE_DIR / "2026" /
                          "2026-08.journal.json").exists())
        self.assertFalse((arch.ARCHIVE_DIR / "2026" /
                          ".2026-08.backup").exists())


# ---------------------------------------------------------------------------
# report safety
# ---------------------------------------------------------------------------

class TestReportGuards(HubTest):

    def test_pending_daily_blocks_and_force_does_not_bypass(self):
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("p")), "diary.md": "d"})
        self.request("daily", "2026-08-01", "pending")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "pending_reports")
        r = arch.archive_month(OLD_MONTH, force=True, today=TODAY)
        self.assertEqual(r["error"], "pending_reports")

    def test_claimed_daily_blocks(self):
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("c")), "diary.md": "d"})
        self.request("daily", "2026-08-01", "claimed")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "pending_reports")

    def test_missing_diary_enqueued_then_blocks(self):
        self.mkday("2026-08-01", {"sessions.jsonl": jlines(ev("m"))})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "pending_reports")
        req = json.loads((arch.REQUESTS_DIR /
                          "daily-2026-08-01.json").read_text())
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["kind"], "daily")
        # finish it: done request + diary → next run archives
        req["status"] = "done"
        (arch.REQUESTS_DIR / "daily-2026-08-01.json").write_text(json.dumps(req))
        (arch.DAILY_DIR / "2026-08-01" / "diary.md").write_text("d")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)

    def test_done_request_missing_diary_reopened(self):
        self.mkday("2026-08-01", {"sessions.jsonl": jlines(ev("rd"))})
        self.request("daily", "2026-08-01", "done")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "pending_reports")
        req = json.loads((arch.REQUESTS_DIR /
                          "daily-2026-08-01.json").read_text())
        self.assertEqual(req["status"], "pending")
        self.assertEqual(req["reopen_reason"], "diary_missing_at_archive")

    def test_derived_only_day_does_not_enqueue(self):
        # conversations.md alone is generated content, not a missing diary
        self.mkday("2026-08-01", {"conversations.md": "generated"})
        arch.REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
        arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertFalse((arch.REQUESTS_DIR / "daily-2026-08-01.json").exists())

    def test_monthly_pending_blocks(self):
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("mp")), "diary.md": "d"})
        self.request("monthly", OLD_MONTH, "pending")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "monthly_pending")
        r = arch.archive_month(OLD_MONTH, force=True, today=TODAY)
        self.assertEqual(r["error"], "monthly_pending")

    def test_monthly_done_missing_md_reopened(self):
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("mm")), "diary.md": "d"})
        self.request("monthly", OLD_MONTH, "done")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "monthly_report_missing")
        req = json.loads((arch.REQUESTS_DIR /
                          "monthly-2026-08.json").read_text())
        self.assertEqual(req["status"], "pending")

    def test_today_never_enqueued(self):
        self.mkday(TODAY.isoformat(), {"sessions.jsonl": jlines(ev("t"))})
        arch.missing_diary_days(TODAY)
        self.assertEqual(arch.missing_diary_days(TODAY), [])


# ---------------------------------------------------------------------------
# extraction safety
# ---------------------------------------------------------------------------

class TestExtract(HubTest):

    def _archived(self, month: str = OLD_MONTH):
        self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("x")), "diary.md": "d"})
        r = arch.archive_month(month, today=TODAY)
        self.assertTrue(r["ok"], r)

    def test_would_clobber_refused(self):
        self._archived()
        self.mkday("2026-08-01", {"sessions.jsonl": "new"})
        r = arch.extract_month(OLD_MONTH)
        self.assertEqual(r["error"], "would_clobber")

    def test_symlink_ancestor_refused(self):
        self._archived()
        outside = self.home / "outside"
        outside.mkdir()
        os.symlink(outside, arch.DAILY_DIR / "2026-08-01")
        r = arch.extract_month(OLD_MONTH)
        self.assertFalse(r["ok"])
        # refused either as symlink-clobber or unsafe_target; nothing written
        self.assertIn(r["error"].split(":")[0],
                      ("would_clobber", "unsafe_target", "symlink_ancestor",
                       "ancestor_escapes_daily"))
        self.assertFalse((outside / "diary.md").exists())
        self.assertFalse((outside / "sessions.jsonl").exists())

    def test_unsafe_members_refused(self):
        tar_path = arch.ARCHIVE_DIR / "2026"
        tar_path.mkdir(parents=True, exist_ok=True)
        tp = tar_path / "2026-08.tar.gz"
        with tarfile.open(tp, "w:gz") as tar:
            data = b"payload"
            for name in ("../evil.txt", "2026-08/../../x", "/abs.txt"):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        r = arch.extract_month(OLD_MONTH)
        self.assertFalse(r["ok"])
        self.assertIn("unsafe", r["error"] + r.get("detail", "")
                      or "unsafe" in str(r))

    def test_laundered_member_names_refused(self):
        """Regression: prefix-stripping must NOT launder '/<month>/...' or
        '../<month>/...' into valid members — raw names rejected."""
        for raw, want in (
                ("/2026-08/2026-08-01/x.txt", None),
                ("../2026-08/2026-08-01/x.txt", None),
                ("./2026-08/2026-08-01/x.txt", None),
                ("2026-08//2026-08-01/x.txt", None),
                ("2026-08/./2026-08-01/x.txt", None),
                ("2026-08\\2026-08-01\\x.txt", None),
                ("2026-08/2026-08-01/x.txt",
                 ("2026-08", "2026-08-01", "x.txt")),
                ("2026-08/2026-08-01/nested/deep.txt",
                 ("2026-08", "2026-08-01", "nested/deep.txt"))):
            self.assertEqual(arch._member_parts(raw), want, raw)

    def test_no_staging_left_on_failure(self):
        self._archived()
        self.mkday("2026-08-01", {"sessions.jsonl": "new"})
        arch.extract_month(OLD_MONTH)
        leftovers = [p for p in arch.DAILY_DIR.iterdir()
                     if p.name.startswith(".archive-stage-")]
        self.assertEqual(leftovers, [])


# ---------------------------------------------------------------------------
# rearchive / merge / conflicts
# ---------------------------------------------------------------------------

class TestRearchive(HubTest):

    def _seed(self, month: str = OLD_MONTH):
        self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("a1"), ev("a2")),
            "diary.md": "# diary\n"})
        self.mkday("2026-08-02", {
            "sessions.jsonl": jlines(ev("b1", "2026-08-02")),
            "diary.md": "d2"})
        r = arch.archive_month(month, today=TODAY)
        self.assertTrue(r["ok"], r)

    def test_late_write_merged_by_event_id(self):
        self._seed()
        # capture the archived bytes BEFORE the merge
        before = self.tar_members(
            OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]
        # late event lands in the recreated day dir
        self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("a2"), ev("a3")),
            "diary.md": "# diary\n"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["rearchive"])
        self.assertIn("2026-08-01", r["removed_dirs"])
        text = self.tar_members(OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]\
            .decode()
        ids = [json.loads(l)["event_id"] for l in text.splitlines()]
        self.assertEqual(ids, ["a1", "a2", "a3"])   # old order + new, deduped

    def test_merge_preserves_archived_byte_prefix(self):
        """Cold citations '<tar>!<member>#L<n>' must never shift: the merged
        member begins with the previously-archived bytes verbatim."""
        self._seed()
        before = self.tar_members(
            OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]
        self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("a9")) + "\n" + jlines(ev("a10")),
            "diary.md": "# diary\n"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        after = self.tar_members(
            OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]
        self.assertTrue(after.startswith(before))
        self.assertEqual(after.count(b"\n"), before.count(b"\n") + 2)

    def test_event_id_conflict_refuses_day_keeps_both(self):
        self._seed()
        # same event_id, different content → conflicted day: archived
        # members carried verbatim, live overlay kept, never a crash.
        self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("a1", text="DIFFERENT")),
            "diary.md": "# diary\n"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertIn("2026-08-01", r["kept_dirs"])
        self.assertEqual(r["conflicts"][0]["day"], "2026-08-01")
        # live dir must still exist with its content
        self.assertIn("DIFFERENT", (arch.DAILY_DIR / "2026-08-01" /
                                    "sessions.jsonl").read_text())
        # archived original must be intact in the NEW tar
        text = self.tar_members(OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]\
            .decode()
        self.assertIn('"a1"', text)
        self.assertIn('"a2"', text)
        self.assertNotIn("DIFFERENT", text)

    def test_canonical_json_dup_not_a_conflict(self):
        self._seed()
        # same event_id, same content, re-serialized differently
        line = '{"event_id":"a1","session_id":"s1","role":"user",' \
               '"ts":"2026-08-01T10:00:00+00:00","text":"x"}'
        self.mkday("2026-08-01", {
            "sessions.jsonl": line + "\n" + jlines(ev("a3")),
            "diary.md": "# diary\n"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertIn("2026-08-01", r["removed_dirs"])
        text = self.tar_members(OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]\
            .decode()
        ids = [json.loads(l)["event_id"] for l in text.splitlines()]
        self.assertEqual(sorted(set(ids)), ["a1", "a2", "a3"])
        self.assertEqual(len(ids), 3)

    def test_conflict_day_preserves_cold_only_members(self):
        """P1 regression: conflicted overlay day must keep ALL archived
        members — original cold-only files must survive in the new tar."""
        self._seed()
        # live overlay has ONLY a conflicting diary.md; sessions.jsonl is
        # cold-only in the archive and must be carried verbatim.
        self.mkday("2026-08-01", {"diary.md": "totally different diary"})
        self.mkday("2026-08-03", {
            "sessions.jsonl": jlines(ev("c1", "2026-08-03")), "diary.md": "d"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertIn("2026-08-01", r["kept_dirs"])
        self.assertIn("2026-08-03", r["removed_dirs"])
        members = self.tar_members(OLD_MONTH)
        orig = members["2026-08/2026-08-01/sessions.jsonl"].decode()
        self.assertIn('"a1"', orig) and self.assertIn('"a2"', orig)
        self.assertEqual(members["2026-08/2026-08-01/diary.md"], b"# diary\n")
        # live overlay untouched on disk
        self.assertEqual((arch.DAILY_DIR / "2026-08-01" / "diary.md")
                         .read_text(), "totally different diary")
        self.assertIn("2026-08-03", "".join(members))

    def test_non_jsonl_conflict_keeps_live_dir(self):
        self._seed()
        self.mkday("2026-08-02", {
            "sessions.jsonl": jlines(ev("b1", "2026-08-02")),
            "diary.md": "MODIFIED diary", "extra.bin": "bin"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertIn("2026-08-02", r["kept_dirs"])
        self.assertTrue((arch.DAILY_DIR / "2026-08-02").exists())
        members = self.tar_members(OLD_MONTH)
        # extra.bin (new live-only file) is part of the conflicted day's
        # overlay — the conflicted day keeps archived bytes AND live dir,
        # extra.bin stays on disk (never silently dropped)
        self.assertTrue((arch.DAILY_DIR / "2026-08-02" / "extra.bin").exists())

    def test_restored_month_rearchives(self):
        self._seed()
        ex = arch.extract_month(OLD_MONTH)
        self.assertTrue(ex["ok"], ex)
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["rearchive"])
        self.assertFalse((arch.DAILY_DIR / "2026-08-01").exists())

    def test_corrupt_manifest_quarantined_not_repaired(self):
        """A manifest that disagrees with a valid tar = corruption: refuse,
        never silently bless altered facts (downgrade protection)."""
        self._seed()
        mpath = arch.ARCHIVE_DIR / "2026" / "2026-08.manifest.json"
        mpath.write_text(json.dumps({"bogus": {"sha256": "0" * 64,
                                               "bytes": 1}}))
        self.mkday("2026-08-03", {
            "sessions.jsonl": jlines(ev("r1", "2026-08-03")), "diary.md": "d"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertFalse(r["ok"])
        self.assertIn("corrupt", r["error"])
        # nothing mutated: live overlay kept, old tar untouched
        self.assertTrue((arch.DAILY_DIR / "2026-08-03").exists())
        v = arch.verify_month(OLD_MONTH)
        self.assertFalse(v["ok"])
        ex = arch.extract_month(OLD_MONTH)
        self.assertFalse(ex["ok"])

    def test_missing_manifest_derived_from_authenticated_tar(self):
        """Index sha intact + manifest missing → tar is authenticated,
        per-file manifest is derived and republished."""
        self._seed()
        (arch.ARCHIVE_DIR / "2026" / "2026-08.manifest.json").unlink()
        # also drop the embedded manifest copy from the index
        ipath = arch.ARCHIVE_DIR / "2026" / "2026-08.index.json"
        idx = json.loads(ipath.read_text())
        idx.pop("manifest", None)
        ipath.write_text(json.dumps(idx))
        self.mkday("2026-08-03", {
            "sessions.jsonl": jlines(ev("r2", "2026-08-03")), "diary.md": "d"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertTrue((arch.ARCHIVE_DIR / "2026" /
                         "2026-08.manifest.json").exists())
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)

    def test_legacy_tar_no_sidecars(self):
        self.make_legacy_tar(OLD_MONTH, {
            "2026-08-01": {"sessions.jsonl": jlines(ev("leg")),
                           "diary.md": "d"}})
        self.mkday("2026-08-02", {
            "sessions.jsonl": jlines(ev("new1", "2026-08-02")), "diary.md": "d"})
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        members = self.tar_members(OLD_MONTH)
        self.assertIn('"leg"', members["2026-08/2026-08-01/sessions.jsonl"]
                      .decode())


# ---------------------------------------------------------------------------
# crash / journal recovery
# ---------------------------------------------------------------------------

class TestCrashRecovery(HubTest):

    def _seed_live(self):
        self.mkday("2026-08-01", {
            "sessions.jsonl": jlines(ev("z1")), "diary.md": "d"})

    def _forge_journal(self, month: str, phase: str, expected_sha: str):
        arch._write_journal(month, {"month": month, "phase": phase,
                                    "expected_sha": expected_sha,
                                    "generation": 2})

    def test_crash_after_tar_publish_commits_new(self):
        """Journal + new tar on disk, sidecars stale → recovery commits the
        NEW trio (tar is ground truth)."""
        self._seed_live()
        arch.archive_month(OLD_MONTH, today=TODAY)
        # extract to bring live dirs back
        arch.extract_month(OLD_MONTH)
        old_tar = arch.ARCHIVE_DIR / "2026" / "2026-08.tar.gz"
        old_bytes = old_tar.read_bytes()
        # simulate a rearchive crash: build+publish a NEW tar (different
        # content), journal says expected_sha = new sha, index/manifest stale
        day = arch.DAILY_DIR / "2026-08-04"
        day.mkdir()
        (day / "sessions.jsonl").write_text(jlines(ev("z9", "2026-08-04")))
        (day / "diary.md").write_text("d")
        members = {}
        for name, meta in arch._scan_dir(day).items():
            members[f"2026-08/2026-08-04/{name}"] = meta
        tmp = arch.ARCHIVE_DIR / "2026" / "fake-new.tar.gz"
        with tarfile.open(tmp, "w:gz") as tar:
            with tarfile.open(old_tar, "r:gz") as src:
                for m in src.getmembers():
                    fh = src.extractfile(m)
                    if fh:
                        tar.addfile(m, fh)
            for name, content in [
                    ("2026-08/2026-08-04/sessions.jsonl",
                     jlines(ev("z9", "2026-08-04")).encode())]:
                info = tarfile.TarInfo(name)
                info.size = len(content)
                tar.addfile(info, io.BytesIO(content))
        new_sha = hashlib.sha256(tmp.read_bytes()).hexdigest()
        arch._backup_trio(OLD_MONTH)
        os.replace(tmp, old_tar)
        self._forge_journal(OLD_MONTH, "tar_published", new_sha)
        # recovery in the next run must commit the new trio
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["recovery"], "committed_new_trio")
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)
        members = self.tar_members(OLD_MONTH)
        self.assertIn("2026-08/2026-08-04/sessions.jsonl", members)
        self.assertIn('"z1"', members["2026-08/2026-08-01/sessions.jsonl"]
                      .decode())

    def test_crash_before_publish_rolls_back(self):
        """Journal exists but tar is still the OLD one → backup trio
        restored, rerun completes cleanly, nothing lost."""
        self._seed_live()
        arch.archive_month(OLD_MONTH, today=TODAY)
        arch.extract_month(OLD_MONTH)
        old_tar = arch.ARCHIVE_DIR / "2026" / "2026-08.tar.gz"
        old_sha = hashlib.sha256(old_tar.read_bytes()).hexdigest()
        # forge journal claiming a publish that never landed
        arch._backup_trio(OLD_MONTH)
        self._forge_journal(OLD_MONTH, "prepared", "f" * 64)
        # also corrupt index to simulate half-state
        (arch.ARCHIVE_DIR / "2026" / "2026-08.index.json").write_text("{}")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["recovery"], "rolled_back")
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)

    def test_interrupted_delete_resumes(self):
        """Crash mid-delete leaves dirs; rerun deletes them (hash-stable)."""
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("i1")), "diary.md": "d"})
        self.mkday("2026-08-02",
                   {"sessions.jsonl": jlines(ev("i2", "2026-08-02")),
                    "diary.md": "d"})
        orig = shutil.rmtree
        calls = {"n": 0}

        def boom(*a, **k):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("simulated crash mid-delete")
            return orig(*a, **k)

        arch.shutil.rmtree = boom
        try:
            with self.assertRaises(RuntimeError):
                arch.archive_month(OLD_MONTH, today=TODAY)
        finally:
            arch.shutil.rmtree = orig
        # one dir deleted, one still live — rerun resumes
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        self.assertFalse((arch.DAILY_DIR / "2026-08-02").exists())
        v = arch.verify_month(OLD_MONTH)
        self.assertTrue(v["ok"], v)

    def test_changed_dir_after_crash_goes_to_merge(self):
        """Dir modified after crash is NOT blindly deleted — it enters the
        merge path (or conflict), never data loss."""
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("j1")), "diary.md": "d"})
        orig = shutil.rmtree

        def boom(*a, **k):
            raise RuntimeError("crash before first delete")

        arch.shutil.rmtree = boom
        try:
            with self.assertRaises(RuntimeError):
                arch.archive_month(OLD_MONTH, today=TODAY)
        finally:
            arch.shutil.rmtree = orig
        # mutate the live dir after the "crash"
        f = arch.DAILY_DIR / "2026-08-01" / "sessions.jsonl"
        f.write_text(f.read_text() + json.dumps(ev("j2")) + "\n")
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        text = self.tar_members(OLD_MONTH)["2026-08/2026-08-01/sessions.jsonl"]\
            .decode()
        self.assertIn('"j1"', text) and self.assertIn('"j2"', text)


# ---------------------------------------------------------------------------
# watcher protocol + concurrency
# ---------------------------------------------------------------------------

class TestWatcherGuard(HubTest):

    def test_live_unpatched_watcher_blocks(self):
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("w")), "diary.md": "d"})
        self.health(version=None)
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertEqual(r["error"], "watcher_lock_protocol_missing")
        r = arch.archive_month(OLD_MONTH, force=True, today=TODAY)
        self.assertEqual(r["error"], "watcher_lock_protocol_missing")

    def test_patched_or_stale_watcher_allows(self):
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("w2")), "diary.md": "d"})
        self.health(version=arch.WATCHER_LOCK_PROTOCOL)
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)
        # stale heartbeat = dead watcher
        self.tearDown()
        self.setUp()
        self.mkday("2026-08-01",
                   {"sessions.jsonl": jlines(ev("w3")), "diary.md": "d"})
        self.health(version=None, age_sec=3600)
        r = arch.archive_month(OLD_MONTH, today=TODAY)
        self.assertTrue(r["ok"], r)


class TestConcurrency(HubTest):

    def test_concurrent_appends_no_data_loss(self):
        """Writers holding the SHARED month lock while archive runs:
        every emitted event lands in the final archive (merged) or stays
        live — never lost, archive never corrupt."""
        month = OLD_MONTH
        day = "2026-08-01"
        self.mkday(day, {"sessions.jsonl": jlines(ev("seed")), "diary.md": "d"})
        emitted: list[str] = []
        stop = threading.Event()
        errors: list[Exception] = []

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                eid = f"concurrent-{n}"
                try:
                    with storage_lock.month_lock(self.home, month,
                                                 shared=True):
                        ddir = arch.DAILY_DIR / day
                        ddir.mkdir(parents=True, exist_ok=True)
                        target = ddir / "sessions.jsonl"
                        seen = set()
                        if target.exists():
                            for line in target.read_text().splitlines():
                                seen.add(json.loads(line)["event_id"])
                        if eid not in seen:
                            with target.open("a") as f:
                                f.write(json.dumps(ev(eid)) + "\n")
                                f.flush()
                                os.fsync(f.fileno())
                        emitted.append(eid)
                except Exception as exc:     # noqa: BLE001
                    errors.append(exc)
                time.sleep(0.001)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        for t in threads:
            t.start()
        # archive repeatedly while writers race
        for _ in range(4):
            r = arch.archive_month(month, force=True, today=TODAY)
            # pending requests may block some runs — acceptable
            time.sleep(0.01)
        stop.set()
        for t in threads:
            t.join(timeout=15)
        self.assertEqual(errors, [])
        # finish pending reports so a final archive can run: mark all
        # requests done + write diaries for live dirs
        if arch.REQUESTS_DIR.exists():
            for p in arch.REQUESTS_DIR.glob("daily-*.json"):
                req = json.loads(p.read_text())
                req["status"] = "done"
                p.write_text(json.dumps(req))
        for d, _p in arch.day_dirs():
            dp = arch.DAILY_DIR / d.isoformat()
            if not (dp / "diary.md").exists():
                (dp / "diary.md").write_text("d")
        r = arch.archive_month(month, force=True, today=TODAY)
        # collect every event_id the final archive holds
        final_ids: set[str] = set()
        if (arch.ARCHIVE_DIR / "2026" / "2026-08.tar.gz").exists():
            for name, blob in self.tar_members(month).items():
                if name.endswith("sessions.jsonl"):
                    for line in blob.decode().splitlines():
                        try:
                            final_ids.add(json.loads(line)["event_id"])
                        except (json.JSONDecodeError, KeyError):
                            pass
        # plus anything still live
        for line in (arch.DAILY_DIR / day / "sessions.jsonl")\
                .read_text().splitlines() if \
                (arch.DAILY_DIR / day / "sessions.jsonl").exists() else []:
            final_ids.add(json.loads(line)["event_id"])
        missing = set(emitted) - final_ids
        self.assertEqual(missing, set(),
                         f"lost events: {sorted(missing)[:10]}")

    def test_shared_lock_blocks_exclusive(self):
        old_timeout = storage_lock._LOCK_TIMEOUT
        storage_lock._LOCK_TIMEOUT = 2.0
        try:
            with storage_lock.month_lock(self.home, OLD_MONTH, shared=False):
                acquired = []

                def try_shared():
                    try:
                        with storage_lock.month_lock(self.home, OLD_MONTH,
                                                     shared=True):
                            acquired.append(True)
                    except storage_lock.LockTimeout:
                        acquired.append(False)

                t = threading.Thread(target=try_shared)
                t.start()
                t.join(timeout=15)
                self.assertEqual(acquired, [False])
        finally:
            storage_lock._LOCK_TIMEOUT = old_timeout


if __name__ == "__main__":
    unittest.main(verbosity=2)
