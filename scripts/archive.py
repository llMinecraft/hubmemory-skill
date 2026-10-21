#!/usr/bin/env python3
"""hubmemory archive — lossless, crash-safe month-level packing for ~/hubmemory/daily.

Safety model (audit hardening):
  * Locks (scripts/storage_lock.py), strict order, outermost first:
      state/archive.lock          EXCLUSIVE for the whole critical section
      state/write-YYYY-MM.lock    EXCLUSIVE while packing/deleting a month,
                                  SHARED by every daily/ writer (watcher
                                  DailyWriter.append, conversations rebuild,
                                  extract staging, report writers under
                                  archive.lock).
    No writer can append into a day dir while it is packed, verified and
    deleted; a late event may still arrive *after* deletion — it simply
    recreates the dir and is folded in by the next rearchive run.
  * Atomic publish + durable journal: each file is written to
    <name>.<rand>.tmp then os.replace()d — a reader never sees a TORN file,
    but the (tar, index, manifest) TRIO cannot commit atomically, so a
    transactional journal guards it:
      1. the previous trio is copied to .<month>.backup/ (fsynced)
      2. <month>.journal.json records {phase, expected_sha, generation}
      3. tar is published, then manifest, then index (journal phase updates)
      4. journal+backup removed after the commit is durable
    On the next run _recover_journal() normalizes before anything else:
      - new tar landed (sha == expected_sha): sidecars are re-derived from
        the tar (GROUND TRUTH) and republished — commit finished
      - old/no tar: the backup trio is restored — rollback, retry clean
    Lock-free readers may see a transient sha mismatch mid-commit and fail
    closed ("corrupt") for that window; they never see torn content and
    the journal guarantees no permanent wedge.
  * Integrity: index["archive"]["sha256"] covers the tarball file;
    index["manifest"] + <month>.manifest.json map every member name to
    {bytes, sha256, mode, mtime}.  Verification streams each member and
    checks name set + per-file size + digest before anything is deleted
    or extracted.  Mode and mtime ride in the tar header (lossless
    metadata); empty dirs are packed as explicit directory members.
  * Stream IO: tar writes use tar.add/addfile with file objects; member
    copies, hashing and extraction run in fixed-size chunks.  Corruption
    mid-archive (truncated gzip, flipped bytes, member drift) is caught by
    the verify step — never entombed.
  * Rearchive: when daily/ day dirs reappear for an already-archived month
    (extract restore, watcher late writes), the verified archive is merged
    with the on-disk files into a NEW tarball:
      - identical bytes            -> carry archived member through
      - *.jsonl both sides         -> line-wise union deduped by event_id
                                    (then exact line); same event_id with
                                    different content is a CONFLICT
      - other differing file       -> day refused, listed in "conflicts"
    A conflicted day keeps its archived bytes AND its live dir: no data loss.
  * Crash behavior: a crash between publish and delete leaves the new
    archive plus live dirs; the next run resumes by re-hashing the dirs —
    unchanged dirs are deleted (interrupted retry), changed dirs enter the
    normal merge path.  Temp files and staging dirs are cleaned at start.
  * Extraction: integrity digest verified, members preflight-validated
    (no absolute paths, "..", links, devices, foreign-month names, extra
    members beyond the manifest), symlink ancestors refused via realpath
    resolution, streamed into a hidden staging dir under daily/, per-file
    sha256 re-verified, then one rename per day — existing paths are never
    overwritten (checked pre-flight AND post-stage, both under the locks).
  * Reports safety: before eligibility is computed, every past day dir with
    source data but no diary is enqueued as a pending daily report
    (schema-compatible with report_due.ensure_request, serialized via
    reports/.lock); a "done" request whose diary.md is absent is reopened,
    not trusted.  Pending/claimed daily requests and a pending/claimed
    monthly-<month> request block the month — an unfinished semantic diary
    is never entombed.  Derived content (conversations.md, dotfiles) alone
    does not trigger enqueue.
  * Watcher protocol: if state/health.json shows a live watcher that does
    not advertise storage_lock_version, destructive operations refuse —
    an old DailyWriter could still write into a dir mid-delete.
  * Force policy: --force skips ONLY the grace window.  It never skips the
    pending-report guard, current/future-month refusal, integrity
    verification, watcher protocol check, or conflict refusal.

Policy:
  * daily/ keeps only the CURRENT month plus a grace window: a month becomes
    eligible the day after its calendar end leaves the MANIFEST 7-day view
    (i.e. on the GRACE_DAYS+1-th day of the following month at the earliest).
  * Archive format (unchanged, readable by cold_storage.py and any tar):
    archive/YYYY/YYYY-MM.tar.gz  members "YYYY-MM/YYYY-MM-DD/<file>"
    archive/YYYY/YYYY-MM.index.json  per-day sizes + archive sha256/bytes
    archive/YYYY/YYYY-MM.manifest.json member-level {bytes, sha256, mode, mtime}
  * Source dirs are deleted ONLY after the tarball passes full verification.
  * Scope note: file content + per-file mode/mtime are lossless; empty
    SUBdirectories are preserved as tar dir members.  A day dir containing
    no files at all is not packed (nothing recoverable is lost — the member
    namespace cannot address a fileless day without breaking the legacy
    <month>/<day>/<file> format cold readers rely on).

Usage:
    python3 archive.py --auto                 # archive eligible months
    python3 archive.py --month 2026-09        # one month (grace skipped with --force)
    python3 archive.py extract 2026-09        # restore a month back into daily/
    python3 archive.py verify  2026-09        # sha256 + member manifest check
    python3 archive.py list                   # show archives + eligibility
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import sys
import tarfile
import tempfile
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import lockutil
import storage_lock

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
DAILY_DIR = HUB_HOME / "daily"
ARCHIVE_DIR = HUB_HOME / "archive"
REQUESTS_DIR = HUB_HOME / "reports" / "requests"
REPORTS_LOCK = HUB_HOME / "reports" / ".lock"
MONTHLY_DIR = HUB_HOME / "monthly"

# Days a just-ended month stays readable in daily/ for the MANIFEST window.
GRACE_DAYS = 7
_CHUNK = 1024 * 1024
_MAX_JSONL_MERGE_BYTES = 128 * 1024 * 1024   # refuse absurd merge inputs
_STAGING_PREFIX = ".archive-stage-"
_TMP_SUFFIX = ".tmp"
WATCHER_LOCK_PROTOCOL = 1
_HEALTH_MAX_AGE = timedelta(minutes=10)


def _journal_path(month: str) -> Path:
    return ARCHIVE_DIR / month[:4] / f"{month}.journal.json"


def _backup_dir(month: str) -> Path:
    return ARCHIVE_DIR / month[:4] / f".{month}.backup"


def _trio_names(month: str) -> tuple[str, str, str]:
    return (f"{month}.tar.gz", f"{month}.manifest.json",
            f"{month}.index.json")

DAY_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^20\d{2}-\d{2}$")
_MONTH_DAY_RE = re.compile(r"^(20\d{2})-(\d{2})$")

# Content generated FROM sessions — a dir holding only this (or dotfiles)
# is not "source data missing its diary".
_DERIVED_NAMES = {"conversations.md"}


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}")


def month_of(day: date) -> str:
    return day.strftime("%Y-%m")


def _month_end(month: str) -> date:
    m = _MONTH_DAY_RE.match(month)
    year, mon = int(m.group(1)), int(m.group(2))
    nxt = date(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return nxt - timedelta(days=1)


def _month_archivable(month: str, today: date) -> bool:
    """Eligible once the month has fully left the GRACE_DAYS window after
    its calendar end (archived on day GRACE_DAYS+1 of the next month)."""
    return month < month_of(today) and \
        today > _month_end(month) + timedelta(days=GRACE_DAYS)


def _sha256_stream(fh, limit: int | None = None) -> str:
    h = hashlib.sha256()
    remaining = limit
    while True:
        want = _CHUNK if remaining is None else min(_CHUNK, remaining)
        if want <= 0:
            break
        chunk = fh.read(want)
        if not chunk:
            break
        h.update(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return h.hexdigest()


def _sha256_path(path: Path) -> str:
    with path.open("rb") as fh:
        return _sha256_stream(fh)


def _atomic_publish(tmp: Path, dest: Path) -> None:
    """fsync temp, os.replace into place, fsync the directory."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    try:
        dir_fd = os.open(str(dest.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _invalidate_cold_caches() -> None:
    """Same-process cold_storage readers memoize by (size, mtime); after a
    publish/delete, drop their caches so they never serve a stale map."""
    try:
        import cold_storage
        cold_storage.clear_caches()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# watcher protocol guard
# ---------------------------------------------------------------------------

def _watcher_protocol_guard() -> dict[str, Any] | None:
    """Refuse destructive ops while a live watcher predates the lock protocol.
    A fresh heartbeat without storage_lock_version means the running
    DailyWriter can still write into a day dir mid-delete."""
    health_path = HUB_HOME / "state" / "health.json"   # follows patched HUB_HOME
    try:
        health = json.loads(health_path.read_text(encoding="utf-8"))
        hb = datetime.fromisoformat(health["heartbeat_at"])
        if hb.tzinfo is None:
            hb = hb.astimezone()
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None                                    # no live watcher info
    if datetime.now().astimezone() - hb > _HEALTH_MAX_AGE:
        return None                                    # stale: watcher dead
    if health.get("storage_lock_version") == WATCHER_LOCK_PROTOCOL:
        return None
    return {"ok": False, "error": "watcher_lock_protocol_missing",
            "detail": "live watcher predates storage locks; "
                      "restart it before archiving"}


# ---------------------------------------------------------------------------
# inventory / eligibility
# ---------------------------------------------------------------------------

def day_dirs() -> list[tuple[date, Path]]:
    out = []
    if DAILY_DIR.is_dir():
        for p in DAILY_DIR.iterdir():
            if p.is_dir() and not p.is_symlink() and DAY_RE.match(p.name):
                try:
                    out.append((date.fromisoformat(p.name), p))
                except ValueError:
                    continue
    return sorted(out)


def _read_request(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


_MALFORMED_PERIOD = "<malformed>"


def pending_periods(kind: str = "daily") -> set[str]:
    """Periods whose report request is unfinished (pending or claimed).
    Unparseable request files FAIL CLOSED: they yield the <malformed>
    marker which blocks every month until investigated."""
    pending = set()
    if REQUESTS_DIR.exists():
        for p in REQUESTS_DIR.glob(f"{kind}-*.json"):
            req = _read_request(p)
            if not req:
                pending.add(_MALFORMED_PERIOD)
                continue
            if req.get("status") != "done":
                pending.add(req.get("period"))
    return pending


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(6) + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _request_path(kind: str, period: str) -> Path:
    return REQUESTS_DIR / f"{kind}-{period}.json"


def _request_is_done(req: dict[str, Any]) -> bool:
    return req.get("status") == "done"


def _reopen_request(path: Path, req: dict[str, Any], reason: str) -> None:
    """Caller must hold reports/.lock. Never disturbs a live claim lease."""
    if req.get("status") == "claimed":
        return
    req.update(status="pending",
               reopened_at=datetime.now().astimezone().isoformat(timespec="seconds"),
               reopen_reason=reason)
    for key in ("claimed_at", "claimed_by", "claimed_model",
                "claimed_session", "claim_token"):
        req.pop(key, None)
    _atomic_text(path, json.dumps(req, ensure_ascii=False, indent=2) + "\n")


def _day_needs_diary(day_dir: Path) -> bool:
    """Source data but no diary.md. Derived files (conversations.md) and
    dotfiles alone do not count as source data."""
    if (day_dir / "diary.md").is_file():
        return False
    for f in day_dir.rglob("*"):
        if not (f.is_file() and not f.is_symlink()):
            continue
        if f.name in _DERIVED_NAMES or f.name.startswith("."):
            continue
        return True
    return False


def _enqueue_missing_diaries(days: list[str]) -> dict[str, Any]:
    """Ensure a pending daily report request exists for every listed day.
    Schema-compatible with report_due.ensure_request/reopen_request;
    serialized via reports/.lock (innermost — acquired last, released
    before any archive/month lock)."""
    enqueued: list[str] = []
    reopened: list[str] = []
    if not days:
        return {"enqueued": enqueued, "reopened": reopened}
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with lockutil.file_lock(REPORTS_LOCK):
            for day in days:
                path = _request_path("daily", day)
                req = _read_request(path)
                if not req:
                    req = {"id": f"daily:{day}", "kind": "daily", "period": day,
                           "status": "pending",
                           "created_at": datetime.now().astimezone()
                           .isoformat(timespec="seconds"),
                           "source": ["archive-gap"]}
                    _atomic_text(path, json.dumps(req, ensure_ascii=False,
                                                  indent=2) + "\n")
                    enqueued.append(day)
                elif _request_is_done(req):
                    _reopen_request(path, req, "diary_missing_at_archive")
                    reopened.append(day)
                # pending/claimed: already protected, leave the lease alone.
    except OSError as exc:
        return {"enqueued": enqueued, "reopened": reopened,
                "error": f"reports_lock: {exc}"}
    return {"enqueued": enqueued, "reopened": reopened}


def monthly_report_guard(month: str) -> dict[str, Any]:
    """Protect an unfinished monthly roll-up: a pending/claimed
    monthly-<month> request blocks; a done request whose monthly/<month>.md
    vanished is reopened under reports/.lock."""
    req_path = _request_path("monthly", month)
    req = _read_request(req_path)
    md_exists = (MONTHLY_DIR / f"{month}.md").is_file()
    if not req:
        return {"blocked": False}
    if not _request_is_done(req):
        return {"blocked": True, "reason": "monthly_pending", "period": month}
    if not md_exists:
        try:
            with lockutil.file_lock(REPORTS_LOCK):
                req = _read_request(req_path)
                if _request_is_done(req):
                    _reopen_request(req_path, req,
                                    "monthly_report_missing_at_archive")
        except OSError:
            pass
        return {"blocked": True, "reason": "monthly_report_missing",
                "period": month}
    return {"blocked": False}


def missing_diary_days(today: date | None = None) -> list[str]:
    """Past (non-today) day dirs containing source data but no diary."""
    today = today or date.today()
    return [d.isoformat() for d, p in day_dirs()
            if d < today and _day_needs_diary(p)]


def eligible_months(today: date | None = None) -> list[str]:
    """Months past the post-month-end grace window. Current and future
    months are never eligible."""
    today = today or date.today()
    months = {month_of(d) for d, _p in day_dirs()}
    return sorted(m for m in months if _month_archivable(m, today))


# ---------------------------------------------------------------------------
# tar member / manifest helpers
# ---------------------------------------------------------------------------

def _member_parts(name: str) -> tuple[str, str, str] | None:
    """Validate a CANONICAL '<month>/<day>/<path>' member name — no
    normalization.  Absolute paths, '~', backslashes, '.', '..', empty or
    repeated separators are rejected outright; stripping prefixes first
    would launder hostile names into valid-looking ones."""
    if not name or name.startswith(("/", "\\", "~")) or "\\" in name:
        return None
    parts = name.split("/")
    if len(parts) < 3:
        return None
    if any(p in ("", ".", "..") for p in parts):
        return None
    if not (MONTH_RE.match(parts[0]) and DAY_RE.match(parts[1])):
        return None
    return parts[0], parts[1], "/".join(parts[2:])


def _scan_dir(day_dir: Path) -> dict[str, dict[str, Any]]:
    """rel-path -> {sha256, bytes} for every regular file under a day dir."""
    out: dict[str, dict[str, Any]] = {}
    for f in sorted(day_dir.rglob("*")):
        if not (f.is_file() and not f.is_symlink()):
            continue
        rel = f.relative_to(day_dir).as_posix()
        out[rel] = {"sha256": _sha256_path(f), "bytes": f.stat().st_size}
    return out


def _read_index(month: str) -> dict[str, Any] | None:
    try:
        data = json.loads((ARCHIVE_DIR / month[:4] / f"{month}.index.json")
                          .read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# durable journal: backup -> publish -> commit, with rollback/recovery
# ---------------------------------------------------------------------------

def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_journal(month: str, journal: dict[str, Any]) -> None:
    journal["updated_at"] = datetime.now().astimezone() \
        .isoformat(timespec="seconds")
    _atomic_text(_journal_path(month),
                 json.dumps(journal, ensure_ascii=False, indent=2) + "\n")
    _fsync_dir(_journal_path(month).parent)


def _read_journal(month: str) -> dict[str, Any] | None:
    try:
        data = json.loads(_journal_path(month).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _backup_trio(month: str) -> None:
    """Copy the existing trio into .<month>.backup/ — file contents AND
    directory fsynced so the rollback copy survives power loss."""
    backup = _backup_dir(month)
    shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir(parents=True, exist_ok=True)
    year_dir = ARCHIVE_DIR / month[:4]
    for name in _trio_names(month):
        src = year_dir / name
        if src.is_file():
            dst = backup / name
            shutil.copy2(src, dst)
            with dst.open("rb") as fh:
                os.fsync(fh.fileno())
    _fsync_dir(backup)


def _restore_trio(month: str) -> list[str]:
    """Rollback: restore backup trio over any half-published files."""
    backup = _backup_dir(month)
    year_dir = ARCHIVE_DIR / month[:4]
    restored = []
    for name in _trio_names(month):
        src, dst = backup / name, year_dir / name
        if src.is_file():
            shutil.copy2(src, dst)
            restored.append(name)
        elif dst.exists():
            dst.unlink()
    _fsync_dir(year_dir)
    return restored


def _finish_journal(month: str) -> None:
    """Commit: drop journal and backups after the trio is durable."""
    _journal_path(month).unlink(missing_ok=True)
    shutil.rmtree(_backup_dir(month), ignore_errors=True)
    _fsync_dir(ARCHIVE_DIR / month[:4])


def _recover_journal(month: str) -> dict[str, Any]:
    """Normalize an interrupted transaction BEFORE planning.
    Returns {"action": ...} — never raises on its own."""
    journal = _read_journal(month)
    if journal is None:
        # orphan backup with no journal: publication never started
        shutil.rmtree(_backup_dir(month), ignore_errors=True)
        return {"action": "none"}
    year_dir = ARCHIVE_DIR / month[:4]
    tar_path = year_dir / f"{month}.tar.gz"
    expected = journal.get("expected_sha")
    try:
        members, _dm = _iter_tar_members(tar_path, month) \
            if tar_path.is_file() else (None, None)
    except (ValueError, tarfile.TarError, OSError, EOFError):
        members = None
    actual = _sha256_path(tar_path) if members is not None else None

    if members is not None and expected and actual == expected:
        # New tar fully landed: sidecars may be stale/missing — re-derive
        # them from the tarball (ground truth) and finish the commit.
        _repair_sidecars(month, members, tar_path)
        _finish_journal(month)
        _invalidate_cold_caches()
        return {"action": "committed_new_trio"}
    # Old or no tar on disk: publication never landed — roll the backup
    # trio back over whatever half-published state exists.
    restored = _restore_trio(month)
    _finish_journal(month)
    _invalidate_cold_caches()
    return {"action": "rolled_back", "restored": restored}


def _read_stored_manifest(month: str) -> dict[str, dict[str, Any]] | None:
    """member -> {sha256, bytes, ...} from the manifest sidecar; falls back
    to index['manifest'] for sidecar-less archives."""
    candidates = [ARCHIVE_DIR / month[:4] / f"{month}.manifest.json"]
    index = _read_index(month)
    try:
        data = json.loads(candidates[0].read_text(encoding="utf-8"))
        if isinstance(data, dict) and all(
                isinstance(v, dict) and "sha256" in v for v in data.values()):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    if index:
        manifest = index.get("manifest")
        if isinstance(manifest, dict) and all(
                isinstance(v, dict) and "sha256" in v for v in manifest.values()):
            return manifest
    return None


def _iter_tar_members(tar_path: Path, month: str) -> tuple[dict, dict]:
    """Stream-scan a tarball -> (files, dirs):
    files: member name -> {sha256, bytes, day, rel, mode, mtime}
    dirs:  member name -> {day, rel, mode, mtime}
    Raises ValueError on unsafe/unexpected members; tar errors propagate."""
    files: dict[str, dict[str, Any]] = {}
    dirs: dict[str, dict[str, Any]] = {}
    with tarfile.open(tar_path, "r|gz") as tar:
        for m in tar:
            if m.isdir():
                parts = _member_parts(m.name)
                if parts is not None and parts[0] == month:
                    dirs[m.name] = {"day": parts[1], "rel": parts[2],
                                    "mode": m.mode, "mtime": m.mtime}
                elif m.name and (".." in m.name or m.name.startswith("/")):
                    raise ValueError(f"unsafe_member:{m.name}")
                continue
            if not m.isfile():
                raise ValueError(f"unsafe_member_type:{m.name}")
            parts = _member_parts(m.name)
            if parts is None or parts[0] != month:
                raise ValueError(f"unsafe_member:{m.name}")
            fh = tar.extractfile(m)
            if fh is None:
                raise ValueError(f"unreadable_member:{m.name}")
            files[m.name] = {"sha256": _sha256_stream(fh, m.size),
                             "bytes": m.size, "day": parts[1],
                             "rel": parts[2], "mode": m.mode,
                             "mtime": m.mtime}
    return files, dirs


def _load_manifest(month: str, members: dict[str, dict[str, Any]],
                   tar_path: Path) -> tuple[dict | None, str]:
    """Return (manifest_or_None, status):
      "ok"       stored manifest matches the scanned members
      "legacy"   no digest evidence at all — serve unverified, never trust
      "derived"  journal-authenticated recovery committed the tar (ground
                 truth) and sidecars must be republished from it
      "corrupt"  stored digests disagree with the tar — QUARANTINE: a
                 mismatch is never silently repaired into trust; that
                 would let a bit-flip downgrade the checksum guarantee
    Mid-publish divergence is normalized by _recover_journal BEFORE this
    runs, so any remaining mismatch is genuine corruption."""
    stored = _read_stored_manifest(month)
    index = _read_index(month)
    expected_sha = (index or {}).get("archive", {}).get("sha256")
    if stored is not None:
        consistent = set(stored) == set(members) and all(
            members[n]["sha256"] == want.get("sha256")
            and members[n]["bytes"] == want.get("bytes")
            for n, want in stored.items())
        if not consistent:
            return None, "corrupt"
        out = {}
        for n, want in stored.items():
            entry = dict(members[n])
            entry["mode"] = want.get("mode", members[n]["mode"])
            entry["mtime"] = want.get("mtime", members[n]["mtime"])
            out[n] = entry
        if expected_sha is None:
            return out, "derived"            # index lost: republish trio
        if expected_sha != _sha256_path(tar_path):
            return None, "corrupt"           # manifest ok but tar != index
        return out, "ok"
    if expected_sha is None:
        return None, "legacy"                # nothing to verify against
    # index sha only, no manifest: tar authenticated, per-file manifest is
    # legitimately re-derived (nothing can disagree) then republished.
    if expected_sha != _sha256_path(tar_path):
        return None, "corrupt"
    derived = {n: {"sha256": m["sha256"], "bytes": m["bytes"],
                   "mode": m["mode"], "mtime": m["mtime"]}
               for n, m in members.items()}
    return derived, "derived"


def _repair_sidecars(month: str, members: dict[str, dict[str, Any]],
                     tar_path: Path) -> dict[str, dict[str, Any]]:
    """Rebuild index+manifest sidecars from the verified tarball on disk
    (crash recovery / legacy migration).  Caller holds the locks."""
    manifest = {n: {"sha256": m["sha256"], "bytes": m["bytes"],
                    "mode": m["mode"], "mtime": m["mtime"]}
                for n, m in members.items()}
    index = _build_month_index(month, sorted({m["day"] for m in members.values()}),
                               manifest)
    index["archive"] = {"path": str(tar_path), "sha256": _sha256_path(tar_path),
                        "bytes": tar_path.stat().st_size, "files": len(manifest)}
    index["manifest"] = manifest
    year_dir = tar_path.parent
    _atomic_text(year_dir / f"{month}.manifest.json",
                 json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    _atomic_text(year_dir / f"{month}.index.json",
                 json.dumps(index, ensure_ascii=False, indent=2) + "\n")
    _invalidate_cold_caches()
    return manifest


# ---------------------------------------------------------------------------
# JSONL merge (event_id dedup, conflict refusal)
# ---------------------------------------------------------------------------

def _line_event_id(obj: Any) -> str | None:
    if isinstance(obj, dict):
        eid = obj.get("event_id")
        if isinstance(eid, str) and eid:
            return eid
    return None


def _merge_jsonl(old: bytes, new: bytes) -> bytes | None:
    """PREFIX-PRESERVING union: the output always starts with the archived
    bytes VERBATIM (blanks, duplicates, order, line numbering — cold
    citations like <tar>!<member>#L<n> never shift).  Only genuinely new
    lines are appended.
      - new line already present verbatim       -> skipped
      - new line's event_id seen with canonically-equal content
                                                -> skipped (re-serialized
                                                   duplicates are NOT a
                                                   conflict)
      - new line's event_id seen with DIFFERENT content
                                                -> None (CONFLICT)
      - otherwise                               -> appended in order"""
    seen_lines: set[bytes] = set()
    seen_eids: dict[str, str] = {}
    for raw in old.splitlines():
        line = raw.rstrip(b"\r")
        if not line.strip() or line in seen_lines:
            continue
        seen_lines.add(line)
        try:
            obj = json.loads(line)
            eid = _line_event_id(obj)
        except (json.JSONDecodeError, ValueError):
            continue
        if eid is not None:
            seen_eids[eid] = json.dumps(
                obj, sort_keys=True, ensure_ascii=False,
                separators=(",", ":"), default=str)

    appended: list[bytes] = []
    for raw in new.splitlines():
        line = raw.rstrip(b"\r")
        if not line.strip() or line in seen_lines:
            continue
        try:
            obj = json.loads(line)
            eid = _line_event_id(obj)
        except (json.JSONDecodeError, ValueError):
            eid = None
            obj = None
        if eid is not None:
            canon = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                               separators=(",", ":"), default=str)
            if eid in seen_eids:
                if seen_eids[eid] != canon:
                    return None                  # same id, different content
                continue                         # canonical duplicate
            seen_eids[eid] = canon
        seen_lines.add(line)
        appended.append(line)

    if not appended:
        return old
    base = old if old.endswith(b"\n") else old + b"\n"
    return base + b"\n".join(appended) + b"\n"


# ---------------------------------------------------------------------------
# month index sidecar
# ---------------------------------------------------------------------------

def _build_month_index(month: str, days: list[str],
                       manifest: dict[str, dict[str, Any]]) -> dict:
    day_entries = []
    for day in sorted(days):
        info = {"date": day}
        prefix = f"{month}/{day}/"
        for name in ("sessions.jsonl", "evidence.jsonl", "diary.md",
                     "conversations.md"):
            entry = manifest.get(prefix + name)
            info[name] = entry["bytes"] if entry else 0
        info["archived"] = True
        day_entries.append(info)
    return {
        "month": month,
        "archived_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "day_count": len(day_entries),
        "days": day_entries,
    }


# ---------------------------------------------------------------------------
# staging cleanup (crash recovery)
# ---------------------------------------------------------------------------

def _cleanup_staging() -> None:
    if DAILY_DIR.is_dir():
        for p in DAILY_DIR.iterdir():
            if p.is_dir() and not p.is_symlink() and \
                    p.name.startswith(_STAGING_PREFIX):
                shutil.rmtree(p, ignore_errors=True)


def _iter_tmp_files(root: Path, stem: str) -> Iterable[Path]:
    if root.is_dir():
        for p in root.glob(f"{stem}.*{_TMP_SUFFIX}"):
            if p.is_file():
                yield p


# ---------------------------------------------------------------------------
# merge planning + tar writing
# ---------------------------------------------------------------------------

def _plan_merge(month: str, dirs: list[tuple[date, Path]],
                tar_path: Path | None) -> dict[str, Any]:
    """Compare live dirs against the existing tarball.
    plan: member name -> ("raw"|"merged"|"disk"|"dirmember", src).
    JSONL merges happen HERE, at plan time — a same-event_id divergence
    demotes the day to "conflicted" (all archived members carried verbatim,
    live dir kept); no partial merge can escape into the new tarball."""
    plan: dict[str, tuple[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    members: dict[str, dict[str, Any]] = {}
    dir_members: dict[str, dict[str, Any]] = {}
    manifest: dict[str, dict[str, Any]] | None = None
    integrity = "fresh"
    disk: dict[str, dict[str, dict[str, Any]]] = {}
    for d, p in dirs:
        disk[d.isoformat()] = _scan_dir(p)

    if tar_path is not None:
        members, dir_members = _iter_tar_members(tar_path, month)
        manifest, integrity = _load_manifest(month, members, tar_path)
        if integrity == "corrupt":
            raise ValueError("archive_corrupt:manifest_mismatch")

    live_days = {d.isoformat() for d, _ in dirs}
    # Archived days whose live dir is gone: carry members through verbatim.
    for name, info in members.items():
        if info["day"] not in live_days:
            plan[name] = ("raw", None)
    # Empty-dir members always carry through — packing a directory member
    # alongside file members is harmless and preserves empty subtrees.
    for name, info in dir_members.items():
        plan[name] = ("dirmember", info)

    conflict_days: set[str] = set()
    with ExitStack() as stack:
        reader = stack.enter_context(_OldTarReader(tar_path)) \
            if tar_path is not None else None
        for day, p in dirs:
            day_iso = day.isoformat()
            disk_files = disk[day_iso]
            prefix = f"{month}/{day_iso}/"
            day_plan: dict[str, tuple[str, Any]] = {}
            day_conflicts: list[str] = []
            for rel, meta in disk_files.items():
                member = prefix + rel
                old = members.get(member)
                if old is None:
                    day_plan[member] = ("disk", p / rel)
                elif old["sha256"] == meta["sha256"]:
                    day_plan[member] = ("raw", None)
                elif rel.endswith(".jsonl"):
                    assert reader is not None
                    _m, fh = reader.open_member(member)
                    with fh:
                        old_bytes = fh.read(_MAX_JSONL_MERGE_BYTES + 1)
                    new_bytes = (p / rel).read_bytes()
                    if len(old_bytes) > _MAX_JSONL_MERGE_BYTES or \
                            len(new_bytes) > _MAX_JSONL_MERGE_BYTES:
                        day_conflicts.append(member)
                        continue
                    merged = _merge_jsonl(old_bytes, new_bytes)
                    if merged is None:
                        day_conflicts.append(member)
                    else:
                        day_plan[member] = ("merged", merged)
                else:
                    day_conflicts.append(member)
            # Archived files absent on disk: keep the archived copy —
            # deleting the dir must not lose them.
            for name, info in members.items():
                if info["day"] == day_iso and info["rel"] not in disk_files \
                        and name not in day_plan:
                    day_plan[name] = ("raw", None)
            # Preserve empty subdirectories as explicit dir members
            # (mode+mtime).
            for sub in sorted(p.rglob("*")):
                if sub.is_dir() and not sub.is_symlink():
                    try:
                        empty = not any(sub.iterdir())
                    except OSError:
                        empty = False
                    if empty:
                        st = sub.stat()
                        day_plan.setdefault(
                            prefix + sub.relative_to(p).as_posix(),
                            ("dirmember", {"mode": stat_mode(st),
                                           "mtime": int(st.st_mtime)}))
            if day_conflicts:
                # A conflicted day is carried VERBATIM from the old archive —
                # every member (the conflicting file AND cold-only siblings
                # like original sessions.jsonl) is preserved.  The live
                # overlay dir is kept on disk untouched: no data is ever lost.
                conflict_days.add(day_iso)
                conflicts.append({"day": day_iso, "files": day_conflicts})
            else:
                plan.update(day_plan)

    # Conflicted days: every archived member carries through unchanged.
    for day_iso in conflict_days:
        for name, info in members.items():
            if info["day"] == day_iso:
                plan[name] = ("raw", None)

    return {"plan": plan, "conflicts": conflicts,
            "conflict_days": conflict_days, "members": members,
            "dir_members": dir_members, "manifest": manifest,
            "integrity": integrity if tar_path is not None else "fresh",
            "disk": disk}


class _OldTarReader:
    """Seekable reader over a 'r:gz' tar for copy-through members."""

    def __init__(self, path: Path):
        self.path = path
        self._tar = None
        self._members = None

    def __enter__(self):
        self._tar = tarfile.open(self.path, "r:gz")
        self._members = {m.name: m for m in self._tar.getmembers() if m.isfile()}
        return self

    def __exit__(self, *exc):
        if self._tar is not None:
            self._tar.close()

    def open_member(self, name: str):
        m = self._members[name]
        fh = self._tar.extractfile(m)
        if fh is None:
            raise ValueError(f"unreadable_member:{name}")
        return m, fh


def _write_tar_streamed(tmp_path: Path, month: str,
                        plan: dict[str, tuple[str, Any]],
                        members: dict[str, dict[str, Any]],
                        old_tar: Path | None,
                        disk: dict[str, dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Build the tarball streaming; returns member->manifest entries."""
    out_manifest: dict[str, dict[str, Any]] = {}
    with ExitStack() as stack:
        tar = stack.enter_context(tarfile.open(tmp_path, "w:gz"))
        reader = stack.enter_context(_OldTarReader(old_tar)) if old_tar else None
        for name in sorted(plan):
            kind, src = plan[name]
            if kind == "dirmember":
                info = tarfile.TarInfo(name)
                info.type = tarfile.DIRTYPE
                info.mode = src.get("mode", 0o755)
                info.mtime = src.get("mtime", 0)
                tar.addfile(info)
                continue
            day, rel = name.split("/", 2)[1], name.split("/", 2)[2]
            if kind == "disk":
                tar.add(src, arcname=name, recursive=False)
                st = os.stat(src)
                out_manifest[name] = {
                    "sha256": disk[day][rel]["sha256"],
                    "bytes": disk[day][rel]["bytes"],
                    "mode": stat_mode(st), "mtime": int(st.st_mtime)}
            elif kind == "raw":
                assert reader is not None
                m, fh = reader.open_member(name)
                with fh:
                    info = tarfile.TarInfo(name)
                    info.size = m.size
                    info.mode = m.mode
                    info.mtime = m.mtime
                    tar.addfile(info, fh)
                out_manifest[name] = dict(members[name])
                out_manifest[name].pop("day", None)
                out_manifest[name].pop("rel", None)
            elif kind == "merged":
                merged = src                       # bytes computed at plan time
                info = tarfile.TarInfo(name)
                info.size = len(merged)
                info.mode = members[name]["mode"]
                info.mtime = members[name]["mtime"]
                tar.addfile(info, io.BytesIO(merged))
                out_manifest[name] = {
                    "sha256": hashlib.sha256(merged).hexdigest(),
                    "bytes": len(merged),
                    "mode": members[name]["mode"],
                    "mtime": int(members[name]["mtime"])}
    return out_manifest


def stat_mode(st) -> int:
    return st.st_mode & 0o777


def _verify_tar(tar_path: Path, month: str,
                expected: dict[str, dict[str, Any]]) -> None:
    """Stream-verify: member-name set, per-file bytes, per-file sha256."""
    got, _dirs = _iter_tar_members(tar_path, month)
    want_names, got_names = set(expected), set(got)
    if want_names != got_names:
        raise ValueError("member_set_mismatch:"
                         f"missing={sorted(want_names - got_names)[:5]}"
                         f" extra={sorted(got_names - want_names)[:5]}")
    bad = [n for n in want_names
           if got[n]["sha256"] != expected[n]["sha256"]
           or got[n]["bytes"] != expected[n]["bytes"]]
    if bad:
        raise ValueError(f"member_digest_mismatch:{bad[:5]}")


def _delete_day_dir(day_dir: Path,
                    snapshot: dict[str, dict[str, Any]]) -> bool:
    """Re-hash every file before rmtree; refuse on drift or unarchived
    content.  `snapshot` is the hash map captured when the dir was packed
    (planned["disk"]) — merged files have different archived bytes by
    design, so the baseline is the snapshot, not the manifest.  Every
    snapshot byte is provably in the new tar: 'disk' members are byte-copies,
    'raw' members matched the snapshot hash, 'merge' unions every snapshot
    line (verified by _verify_tar)."""
    current = _scan_dir(day_dir)
    if set(current) != set(snapshot):
        return False                              # a file appeared/vanished
    for rel, meta in current.items():
        if snapshot[rel]["sha256"] != meta["sha256"]:
            return False                          # drifted since packing
    shutil.rmtree(day_dir)
    return True


# ---------------------------------------------------------------------------
# archive one month
# ---------------------------------------------------------------------------

def _blocked(reason: str, month: str, **extra) -> dict[str, Any]:
    return {"ok": False, "error": reason, "month": month, **extra}


def _pending_daily_requests(month: str,
                            dirs: list[tuple[date, Path]]) -> list[str]:
    """Unfinished daily requests FOR THE WHOLE MONTH — including archived
    (cold-only) days.  A pending diary for a cold day must still block the
    month's rearchive, not just a live overlay."""
    pending = pending_periods("daily")
    return sorted(p for p in pending
                  if p == _MALFORMED_PERIOD or
                  (isinstance(p, str) and p.startswith(month + "-")))


def archive_month(month: str, *, force: bool = False,
                  today: date | None = None) -> dict[str, Any]:
    """Archive (or rearchive) one month.  force skips ONLY the grace window;
    every safety gate still applies."""
    if not (MONTH_RE.match(month) and 1 <= int(month[5:7]) <= 12):
        return _blocked("bad_month", month)
    today = today or date.today()
    if month >= month_of(today):
        return _blocked("month_not_finished", month,
                        detail="current or future month")

    # Gap remediation BEFORE locks: every past day with source data but no
    # diary gets a pending request (done-but-missing requests are reopened).
    try:
        gaps = [d for d in missing_diary_days(today) if d.startswith(month)]
        enqueue = _enqueue_missing_diaries(gaps) if gaps else {}
    except OSError as exc:
        enqueue = {"error": str(exc)}

    protocol = _watcher_protocol_guard()
    if protocol is not None:
        return {**protocol, "month": month, "enqueue": enqueue}

    with storage_lock.archive_lock(HUB_HOME):
        with storage_lock.month_lock(HUB_HOME, month, shared=False):
            _cleanup_staging()
            recovery = _recover_journal(month)
            dirs = [(d, p) for d, p in day_dirs() if month_of(d) == month]
            year_dir = ARCHIVE_DIR / month[:4]
            tar_path = year_dir / f"{month}.tar.gz"
            rearchive = tar_path.exists()
            if not dirs and not rearchive:
                return _blocked("no_day_dirs", month)

            # --- safety gates (re-checked under the exclusive locks) -------
            if dirs and not _month_archivable(month, today) and not force:
                return _blocked("grace_window", month,
                                eligible_after=(_month_end(month)
                                                + timedelta(days=GRACE_DAYS + 1))
                                .isoformat())
            open_days = _pending_daily_requests(month, dirs)
            if open_days:
                return _blocked("pending_reports", month,
                                days=open_days, enqueue=enqueue)
            guard = monthly_report_guard(month)
            if guard["blocked"]:
                return _blocked(guard["reason"], month, enqueue=enqueue)

            # --- repair-only path: tar exists, no live dirs ----------------
            if not dirs:
                try:
                    members, dir_members = _iter_tar_members(tar_path, month)
                except (ValueError, tarfile.TarError, OSError, EOFError) as exc:
                    return _blocked("existing_archive_unreadable", month,
                                    detail=str(exc), enqueue=enqueue)
                manifest, integrity = _load_manifest(month, members, tar_path)
                if integrity == "corrupt":
                    return _blocked("existing_archive_corrupt", month,
                                    enqueue=enqueue)
                if integrity == "derived":
                    _repair_sidecars(month, members, tar_path)
                return {"ok": True, "month": month, "rearchive": False,
                        "integrity": integrity,
                        "recovery": recovery["action"],
                        "removed_dirs": [], "kept_dirs": [],
                        "conflicts": [], "enqueue": enqueue,
                        "archive": str(tar_path),
                        "files": len(manifest or members),
                        "sha256": (_read_index(month) or {})
                        .get("archive", {}).get("sha256")}

            # --- plan ------------------------------------------------------
            try:
                planned = _plan_merge(month, dirs, tar_path if rearchive else None)
            except ValueError as exc:
                if str(exc).startswith("archive_corrupt"):
                    return _blocked("existing_archive_corrupt", month,
                                    detail=str(exc), enqueue=enqueue)
                return _blocked("existing_archive_unreadable", month,
                                detail=str(exc), enqueue=enqueue)
            except (tarfile.TarError, OSError, EOFError) as exc:
                return _blocked("existing_archive_unreadable", month,
                                detail=str(exc), enqueue=enqueue)
            plan = planned["plan"]
            conflicts = planned["conflicts"]
            conflict_days = planned["conflict_days"]
            members = planned["members"]

            # --- build to temp, verify, atomically publish -----------------
            year_dir.mkdir(parents=True, exist_ok=True)
            for p in _iter_tmp_files(year_dir, month):
                p.unlink(missing_ok=True)
            tmp_tar = year_dir / f"{month}.{secrets.token_hex(8)}{_TMP_SUFFIX}"
            tmp_index = year_dir / f"{month}.{secrets.token_hex(8)}.index{_TMP_SUFFIX}"
            tmp_manifest = year_dir / f"{month}.{secrets.token_hex(8)}.manifest{_TMP_SUFFIX}"
            try:
                out_manifest = _write_tar_streamed(
                    tmp_tar, month, plan, members,
                    tar_path if rearchive else None, planned["disk"])
                _verify_tar(tmp_tar, month, out_manifest)
                tar_sha = _sha256_path(tmp_tar)
                tar_bytes = tmp_tar.stat().st_size
                days = sorted({v["day"] for v in
                               _iter_tar_members(tmp_tar, month)[0].values()})
                index = _build_month_index(month, days, out_manifest)
                index["archive"] = {"path": str(tar_path), "sha256": tar_sha,
                                    "bytes": tar_bytes,
                                    "files": len(out_manifest)}
                index["manifest"] = out_manifest
                index["generation"] = int(
                    (_read_index(month) or {}).get("generation") or 0) + 1
                tmp_index.write_text(json.dumps(index, ensure_ascii=False,
                                                indent=2) + "\n",
                                     encoding="utf-8")
                tmp_manifest.write_text(json.dumps(
                    out_manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                # Durable transaction: durable backup of the old trio,
                # journal with expected tar sha, then publish.  Recovery
                # either commits the new trio (tar = ground truth) or
                # restores the backup — no wedged half-state survives.
                _backup_trio(month)
                journal = {"month": month, "phase": "prepared",
                           "expected_sha": tar_sha,
                           "generation": index["generation"]}
                _write_journal(month, journal)
                _atomic_publish(tmp_tar, tar_path)
                journal["phase"] = "tar_published"
                _write_journal(month, journal)
                _atomic_publish(tmp_manifest,
                                year_dir / f"{month}.manifest.json")
                journal["phase"] = "manifest_published"
                _write_journal(month, journal)
                _atomic_publish(tmp_index, year_dir / f"{month}.index.json")
                journal["phase"] = "committed"
                _write_journal(month, journal)
                _invalidate_cold_caches()
            finally:
                for p in (tmp_tar, tmp_index, tmp_manifest):
                    p.unlink(missing_ok=True)

            # --- delete only fully-archived, hash-stable dirs --------------
            removed: list[str] = []
            kept: list[str] = []
            for d, p in dirs:
                day_iso = d.isoformat()
                if day_iso in conflict_days:
                    kept.append(day_iso)
                    continue
                if _delete_day_dir(p, planned["disk"].get(day_iso, {})):
                    removed.append(day_iso)
                else:
                    kept.append(day_iso)
            _finish_journal(month)          # transaction durable
            _invalidate_cold_caches()

            return {"ok": True, "month": month,
                    "rearchive": rearchive,
                    "recovery": recovery["action"],
                    "integrity": planned["integrity"],
                    "removed_dirs": removed, "kept_dirs": kept,
                    "conflicts": conflicts, "enqueue": enqueue,
                    "archive": str(tar_path),
                    "index": str(year_dir / f"{month}.index.json"),
                    "manifest": str(year_dir / f"{month}.manifest.json"),
                    "files": len(out_manifest),
                    "sha256": index["archive"]["sha256"]}


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------

def _safe_target(base_resolved: Path, rel_parts: list[str]) -> Path:
    target = base_resolved.joinpath(*rel_parts)
    try:
        target.resolve().relative_to(base_resolved)
    except ValueError:
        raise ValueError(f"unsafe_target:{target}")
    return target


def _check_no_symlink_ancestors(daily: Path, rel_parts: list[str]) -> None:
    """Every existing ancestor of the target under daily/ must resolve
    inside daily/ and must not itself be a symlink."""
    probe = daily
    for part in rel_parts[:-1]:
        probe = probe / part
        if probe.is_symlink():
            raise ValueError(f"symlink_ancestor:{probe}")
        if probe.exists():
            resolved = probe.resolve()
            if resolved != daily and daily not in resolved.parents:
                raise ValueError(f"ancestor_escapes_daily:{probe}")


def extract_month(month: str) -> dict[str, Any]:
    if not MONTH_RE.match(month):
        return {"ok": False, "error": "bad_month", "month": month}
    tar_path = ARCHIVE_DIR / month[:4] / f"{month}.tar.gz"
    if not tar_path.exists():
        return {"ok": False, "error": "archive_not_found", "path": str(tar_path)}

    protocol = _watcher_protocol_guard()
    if protocol is not None:
        return {**protocol, "month": month}

    with storage_lock.archive_lock(HUB_HOME):
        with storage_lock.month_lock(HUB_HOME, month, shared=False):
            _cleanup_staging()
            _recover_journal(month)
            DAILY_DIR.mkdir(parents=True, exist_ok=True)
            daily = DAILY_DIR.resolve()

            # --- integrity digest check (tar is truth; journal leftovers
            # already normalized by _recover_journal) -----------------------
            try:
                members, dir_members = _iter_tar_members(tar_path, month)
            except (ValueError, tarfile.TarError, OSError, EOFError) as exc:
                return {"ok": False, "error": "archive_unreadable",
                        "detail": str(exc)}
            manifest, integrity = _load_manifest(month, members, tar_path)
            if integrity == "corrupt":
                return {"ok": False, "error": "archive_corrupt"}
            if integrity == "derived":
                manifest = _repair_sidecars(month, members, tar_path)

            # --- preflight: every target new + strictly inside daily/ ------
            targets: dict[str, Path] = {}
            try:
                for name in members:
                    _m, day, rel = _member_parts(name)
                    parts = [day] + rel.split("/")
                    target = _safe_target(daily, parts)
                    _check_no_symlink_ancestors(daily, parts)
                    if os.path.lexists(target):
                        return {"ok": False, "error": "would_clobber",
                                "path": str(target)}
                    targets[name] = target
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            # --- stage + verify + publish ----------------------------------
            staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX,
                                            dir=str(DAILY_DIR)))
            try:
                if staging.resolve().parent != daily:
                    return {"ok": False, "error": "staging_escapes_daily"}
                with tarfile.open(tar_path, "r|gz") as tar:
                    for m in tar:
                        if m.isdir():
                            parts = _member_parts(m.name)
                            if parts is not None:
                                dp = staging / parts[1] / parts[2]
                                dp.mkdir(parents=True, exist_ok=True)
                                os.chmod(dp, m.mode or 0o755)
                                os.utime(dp, (m.mtime, m.mtime))
                            continue
                        if not m.isfile():
                            continue
                        parts = _member_parts(m.name)
                        if parts is None:
                            return {"ok": False, "error": "unsafe_member",
                                    "member": m.name}
                        rel_out = staging / parts[1] / parts[2]
                        rel_out.parent.mkdir(parents=True, exist_ok=True)
                        fh = tar.extractfile(m)
                        if fh is None:
                            return {"ok": False, "error": "unreadable_member",
                                    "member": m.name}
                        digest = hashlib.sha256()
                        with fh, rel_out.open("wb") as out:
                            while True:
                                chunk = fh.read(_CHUNK)
                                if not chunk:
                                    break
                                digest.update(chunk)
                                out.write(chunk)
                            out.flush()
                            os.fsync(out.fileno())
                        want = (manifest or {}).get(m.name)
                        if want and (digest.hexdigest() != want["sha256"]
                                     or rel_out.stat().st_size != want["bytes"]):
                            return {"ok": False,
                                    "error": "staged_digest_mismatch",
                                    "member": m.name}
                        os.chmod(rel_out, m.mode if m.mode else 0o644)
                        if m.mtime:
                            os.utime(rel_out, (m.mtime, m.mtime))

                # second clobber check post-stage (still under the locks)
                days = sorted({p.name for p in staging.iterdir() if p.is_dir()})
                for day in days:
                    dest = DAILY_DIR / day
                    if os.path.lexists(dest):
                        return {"ok": False, "error": "would_clobber",
                                "path": str(dest), "day": day}
                restored = []
                for day in days:
                    os.replace(staging / day, DAILY_DIR / day)
                    restored.append(day)
                _invalidate_cold_caches()
                return {"ok": True, "month": month,
                        "restored_days": restored,
                        "restored_to": str(DAILY_DIR),
                        "integrity": integrity}
            finally:
                shutil.rmtree(staging, ignore_errors=True)


# ---------------------------------------------------------------------------
# integrity check command
# ---------------------------------------------------------------------------

def verify_month(month: str) -> dict[str, Any]:
    if not MONTH_RE.match(month):
        return {"ok": False, "error": "bad_month"}
    tar_path = ARCHIVE_DIR / month[:4] / f"{month}.tar.gz"
    if not tar_path.exists():
        return {"ok": False, "error": "archive_not_found"}
    index = _read_index(month)
    stored = _read_stored_manifest(month)
    expected_sha = (index or {}).get("archive", {}).get("sha256")
    try:
        members, _dirs = _iter_tar_members(tar_path, month)
    except (ValueError, tarfile.TarError, OSError, EOFError) as exc:
        return {"ok": False, "error": "archive_unreadable", "detail": str(exc)}
    issues = []
    corrupt = False
    if expected_sha:
        actual = _sha256_path(tar_path)
        if actual != expected_sha:
            corrupt = True
            issues.append("index sha256 mismatch; refuse use (journal recovery required if a transaction was interrupted)")
    else:
        issues.append("no index sha256 (legacy archive; unverified)")
    if stored is not None:
        bad = [n for n, want in stored.items()
               if n not in members
               or members[n]["sha256"] != want.get("sha256")
               or members[n]["bytes"] != want.get("bytes")]
        extra = set(members) - set(stored)
        if bad:
            corrupt = True
            issues.append(f"manifest mismatched members: {sorted(bad)[:5]}")
        if extra:
            corrupt = True
            issues.append(f"members missing from manifest: {sorted(extra)[:5]}")
    else:
        issues.append("no manifest (per-file digests unavailable)")
    return {"ok": not corrupt,
            "verified": bool(expected_sha) and not corrupt,
            "per_file_verified": stored is not None and not corrupt,
            "month": month, "members": len(members), "issues": issues}


# ---------------------------------------------------------------------------
# status / cli
# ---------------------------------------------------------------------------

def list_status() -> None:
    archived = []
    if ARCHIVE_DIR.exists():
        for p in sorted(ARCHIVE_DIR.glob("20??/20??-??.tar.gz")):
            archived.append((p.name[:-len(".tar.gz")], p.stat().st_size))
    eligible = set(eligible_months())
    pending = pending_periods("daily")
    in_daily = sorted({month_of(d) for d, _p in day_dirs()})
    print("== hubmemory archive status ==")
    print("daily/ months:", ", ".join(in_daily) or "(none)")
    for m in in_daily:
        reason = []
        if m == month_of(date.today()):
            reason.append("current")
        elif m in eligible:
            reason.append("eligible")
        else:
            reason.append("grace window")
        open_days = sorted(d for d in pending if d.startswith(m))
        if open_days:
            reason.append(f"pending:{','.join(open_days)}")
        print(f"  {m}  [{'; '.join(reason)}]")
    gaps = missing_diary_days()
    if gaps:
        print(f"missing diaries (auto-enqueue on archive): "
              f"{len(gaps)} day(s): {', '.join(gaps[:8])}"
              + (" …" if len(gaps) > 8 else ""))
    print("archives:")
    if not archived:
        print("  (none)")
    for m, size in archived:
        print(f"  {m}  {size/1024:.0f} KiB")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs="?",
                        choices=("list", "extract", "verify"), default=None)
    parser.add_argument("period", nargs="?", help="YYYY-MM for extract/verify")
    parser.add_argument("--auto", action="store_true")
    parser.add_argument("--month")
    parser.add_argument("--force", action="store_true",
                        help="skip ONLY the grace window; safety gates still apply")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command in ("extract", "verify"):
        if not args.period:
            print(f"usage: archive.py {args.command} YYYY-MM", file=sys.stderr)
            return 2
        fn = extract_month if args.command == "extract" else verify_month
        result = fn(args.period)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1
    if args.command == "list" or (not args.auto and not args.month):
        list_status()
        return 0

    results = []
    months = [args.month] if args.month else eligible_months()
    if not months:
        log("no eligible months")
    for month in months:
        try:
            result = archive_month(month, force=args.force)
        except storage_lock.LockTimeout as exc:
            result = {"ok": False, "error": "lock_timeout", "detail": str(exc)}
        if result.get("ok"):
            note = "rearchived" if result.get("rearchive") else "archived"
            log(f"{note} {month}: removed={result['removed_dirs']} "
                f"kept={result['kept_dirs']} -> {result['archive']} "
                f"({result['files']} files)")
            if result.get("conflicts"):
                log(f"  conflicts (live dirs kept): {result['conflicts']}")
        else:
            log(f"skip {month}: {result.get('error')} "
                f"{result.get('days', result.get('detail', ''))}")
        results.append(result)
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
