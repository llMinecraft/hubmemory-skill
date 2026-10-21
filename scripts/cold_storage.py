#!/usr/bin/env python3
"""hubmemory cold storage — read-only access to month-level tar archives.

Counterpart to archive.py (which owns writes). This module lets
monthly_report.py, hybrid_query.py, manifest.py and `hub day` read archived
days WITHOUT extracting anything to disk:

    iter_days(hub_home)                       -> ISO days, union of hot daily/
                                               dirs + archived days (sorted)
    read_day_text(hub_home, day, filename, merge=False)
                                              -> str | None  (hot file wins;
                                               merge=True returns canonical
                                               merged hot+cold JSONL lines)
    iter_day_files(hub_home, day)             -> [{day, name, source, locator}]
    iter_day_records(hub_home, day, filename) -> merged hot+cold JSONL rows,
                                               deduped, with exact locators
    archives(hub_home, verify=True)           -> integrity/inventory status
                                               (verify=False never hashes)
    has_day(hub_home, day)                    -> cheap existence check

Archive layout (written by archive.py):
    archive/YYYY/YYYY-MM.tar.gz   members "YYYY-MM/YYYY-MM-DD/<file>"
    archive/YYYY/YYYY-MM.index.json  {"days": [{"date": ..., "diary.md": n,
                                      "sessions.jsonl": n, ...}],
                                      "archive": {"sha256": ..., ...}}

Integrity model — fail closed on corruption:
  * The .index.json sidecar gives month/day inventory for free (no gzip
    stream is opened to answer "does day X exist" when an index exists).
  * Before any archive member is READ, the tarball's sha256 is verified
    against index["archive"]["sha256"]. A mismatch (or an unreadable
    archive) quarantines the WHOLE archive: every read returns empty and
    the issue is recorded in last_issues().
  * Missing index (legacy/hand-made archives): members are discovered by a
    streaming scan and content is still served, flagged "unverified".
  * Member names are validated (month/day shape, no .., no absolute paths,
    no link/device entries); secret-looking basenames (.env, id_rsa, *.pem,
    credentials, *.key …) are never returned — on the cold side AND on the
    hot side, where symlinked files are additionally refused unless they
    resolve inside the hub root (no reading ~/.ssh through daily/).
  * Cache gates carry a file fingerprint (size, mtime_ns, head-digest) plus
    the index sidecar fingerprint and its recorded sha256, so an atomic
    same-size/same-mtime replacement of tar+index cannot serve stale bytes.
  * Everything is streamed (`r|gz`); day content is cached in a small
    bounded in-memory LRU. Nothing is ever written under daily/ or archive/.

Locator format (stable provenance used by hybrid_query/context):
    <abs tar path>!<member>#L<1-based line>      e.g.
    /Users/x/hubmemory/archive/2026/2026-09.tar.gz!2026-09/2026-09-30/sessions.jsonl#L3
Hot rows keep their plain filesystem locator "<path>#L<n>".

Stdlib only. Import-safe: no I/O at import time; hub_home is per-call.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import threading
from collections import OrderedDict
from datetime import date
from pathlib import Path
from typing import Any, Iterable

DAY_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^20\d{2}-\d{2}$")
_ARCHIVE_RE = re.compile(r"^(20\d{2}-\d{2})\.tar\.gz$")
_FILENAME_RE = re.compile(r"^[\w.()\-]+$")          # basename only, no slashes
_INDEXED_NAMES = ("sessions.jsonl", "evidence.jsonl", "diary.md",
                  "conversations.md", "contrib.json")

# Basenames that are never served out of an archive. The hub packs whatever
# a day dir contained; this is the read-side guard so cold access cannot
# become a secrets exfiltration path.
_SECRET_BASENAME_RE = re.compile(
    r"(?i)(^\.?env(\.|$)|secret|credential|passwd|password|id_rsa|id_dsa|"
    r"id_ed25519|\.pem$|\.key$|\.keystore$|\.p12$|\.pfx$|netrc|"
    r"token[s]?\.|cookies?\.)"
)

MAX_MEMBER_BYTES = 64 * 1024 * 1024   # refuse absurd members, memory stays bounded
_DAY_CACHE_LIMIT = 8                  # (archive, day) payloads kept in RAM
_MAP_CACHE_LIMIT = 16                 # member maps for index-less archives
_SHA_CHUNK = 1024 * 1024
_ISSUE_LIMIT = 50

_lock = threading.RLock()
# (str tar_path, day) -> (fingerprint, {basename: bytes})
_day_cache: "OrderedDict[tuple[str, str], tuple[tuple, dict[str, bytes]]]" = OrderedDict()
# str tar_path -> (fingerprint, {day: {basename: member_name}})
_map_cache: "OrderedDict[str, tuple[tuple, dict[str, dict[str, str]]]]" = OrderedDict()
# str tar_path -> (fingerprint, "ok"|"unverified"|"corrupt", issue|None)
_integrity_cache: dict[str, tuple[tuple, str, str | None]] = {}
# str index_path -> (index fingerprint, parsed index)
_index_cache: dict[str, tuple[tuple | None, dict[str, Any] | None]] = {}
_issues: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _hub(hub_home: os.PathLike | str) -> Path:
    return Path(hub_home)


def _norm_day(day: str | date) -> str:
    return day if isinstance(day, str) else day.isoformat()


def _norm_month(month: str) -> str:
    return month[:7]


def _index_path(tar_path: Path) -> Path:
    name = tar_path.name
    return tar_path.with_name(name[: -len(".tar.gz")] + ".index.json") \
        if name.endswith(".tar.gz") else tar_path.with_suffix(".index.json")


_FP_HEAD = 64 * 1024


def _fingerprint(path: Path) -> tuple | None:
    """(size, mtime_ns, sha1-of-head) — cheap identity that still catches an
    atomic same-size/same-mtime replacement, which size+mtime alone misses."""
    try:
        st = path.stat()
        with path.open("rb") as fh:
            head = fh.read(_FP_HEAD)
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns,
            hashlib.sha1(head).hexdigest())


def _read_index(tar_path: Path) -> dict[str, Any] | None:
    """Parsed .index.json sidecar, memoized against the file's fingerprint."""
    ip = _index_path(tar_path)
    fp = _fingerprint(ip)
    key = str(ip)
    with _lock:
        hit = _index_cache.get(key)
        if hit is not None and hit[0] == fp:
            return hit[1]
    data: dict[str, Any] | None = None
    if fp is not None:
        try:
            parsed = json.loads(ip.read_text(encoding="utf-8"))
            data = parsed if isinstance(parsed, dict) else None
        except (OSError, json.JSONDecodeError):
            data = None
    with _lock:
        _index_cache[key] = (fp, data)
        while len(_index_cache) > _MAP_CACHE_LIMIT:
            _index_cache.popitem(last=False)
    return data


def _archive_fp(tar_path: Path) -> tuple | None:
    """Composite fingerprint: tarball fingerprint + index fingerprint +
    the sha256 the index promises.  Any half of a coordinated tar+index
    replacement flips the value, so no cache can serve stale proof."""
    tf = _fingerprint(tar_path)
    if tf is None:
        return None
    ip = _index_path(tar_path)
    index_fp = _fingerprint(ip)
    index = _read_index(tar_path)
    return (tf, index_fp, _expected_sha(index))


def _expected_sha(index: dict[str, Any] | None) -> str | None:
    if not index:
        return None
    sha = (index.get("archive") or {}).get("sha256")
    return sha if isinstance(sha, str) and sha else None


def _index_days(index: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """day -> {filename: size} from an index sidecar (authoritative inventory)."""
    out: dict[str, dict[str, Any]] = {}
    for entry in (index or {}).get("days") or []:
        if not isinstance(entry, dict):
            continue
        d = entry.get("date")
        if not (isinstance(d, str) and DAY_RE.match(d)):
            continue
        files = {k: v for k, v in entry.items()
                 if isinstance(k, str) and k != "date"
                 and k != "archived" and isinstance(v, int)}
        out[d] = files
    return out


def _note_issue(archive: Path, issue: str) -> None:
    with _lock:
        _issues.append({"archive": str(archive), "issue": issue})
        del _issues[:-_ISSUE_LIMIT]


def last_issues() -> list[dict[str, Any]]:
    """Recent integrity/safety problems (corrupt archives, unsafe members)."""
    with _lock:
        return list(_issues)


def clear_caches() -> None:
    """Drop memoized scans/verifications (tests, long-running watchers)."""
    with _lock:
        _day_cache.clear()
        _map_cache.clear()
        _integrity_cache.clear()
        _index_cache.clear()
        _issues.clear()


def _is_safe_filename(name: str) -> bool:
    """Basename-only, no traversal, never a secret-looking file."""
    if not name or not _FILENAME_RE.match(name):
        return False
    if _SECRET_BASENAME_RE.search(name):
        return False
    return True


def _member_parts(raw: str) -> list[str] | None:
    """Normalize a tar member name; None => unsafe/irrelevant member."""
    norm = raw.replace("\\", "/").lstrip("./")
    if not norm or norm.startswith("/") or norm.startswith("~"):
        return None
    parts = [p for p in norm.split("/") if p not in ("", ".")]
    if len(parts) < 3 or ".." in parts:
        return None
    if not (MONTH_RE.match(parts[0]) and DAY_RE.match(parts[1])):
        return None
    return parts


# ---------------------------------------------------------------------------
# archive inventory + integrity
# ---------------------------------------------------------------------------

def _list_archives(hub: Path) -> list[tuple[str, Path]]:
    root = hub / "archive"
    out = []
    if root.is_dir():
        for p in root.glob("20??/20??-??.tar.gz"):
            m = _ARCHIVE_RE.match(p.name)
            if m and p.parent.name == p.name[:4]:
                out.append((m.group(1), p))
    return sorted(out)


def _verify_archive(tar_path: Path) -> str:
    """"ok" | "unverified" | "corrupt".  Memoized per archive fingerprint
    (tar + index sidecar + recorded sha): any coordinated replacement —
    including atomic same-size/same-mtime ones — invalidates the result.

    "ok"         index exists, sha256 recorded, tarball digest matches.
    "unverified" index missing or carries no sha256 — content still served.
    "corrupt"    sha mismatch or unreadable stream — archive quarantined.
    """
    key = str(tar_path)
    fp = _archive_fp(tar_path)
    if fp is None:
        _note_issue(tar_path, "archive unreadable/missing")
        return "corrupt"
    with _lock:
        hit = _integrity_cache.get(key)
        if hit and hit[0] == fp:
            return hit[1]

    expected = fp[2]
    if expected is None:
        state, issue = "unverified", None
    else:
        digest = hashlib.sha256()
        try:
            with tar_path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(_SHA_CHUNK), b""):
                    digest.update(chunk)
        except OSError as exc:
            state, issue = "corrupt", f"unreadable: {exc}"
        else:
            if digest.hexdigest() == expected:
                state, issue = "ok", None
            else:
                state, issue = "corrupt", "sha256 mismatch"

    if state == "corrupt":
        _note_issue(tar_path, issue or "corrupt")
    with _lock:
        _integrity_cache[key] = (fp, state, issue)
    return state


def _member_map(tar_path: Path) -> dict[str, dict[str, str]]:
    """day -> {basename: member_name}.  One streaming pass, memoized.

    Used for index-less archives and for files the index does not list.
    Corrupt/unsafe members are dropped; exotic members (links, devices)
    are never touched.
    """
    key = str(tar_path)
    fp = _archive_fp(tar_path)
    if fp is None:
        return {}
    with _lock:
        hit = _map_cache.get(key)
        if hit and hit[0] == fp:
            _map_cache.move_to_end(key)
            return hit[1]

    days: dict[str, dict[str, str]] = {}
    try:
        with tarfile.open(tar_path, "r|gz") as tar:
            for m in tar:
                if not m.isfile():
                    continue
                parts = _member_parts(m.name)
                if parts is None:
                    if m.name and (".." in m.name or m.name.startswith("/")):
                        _note_issue(tar_path, f"unsafe member skipped: {m.name[:80]}")
                    continue
                day, name = parts[1], parts[-1]
                if not _is_safe_filename(name):
                    continue
                days.setdefault(day, {})[name] = "/".join(parts)
    except (tarfile.TarError, OSError, EOFError) as exc:
        _note_issue(tar_path, f"scan failed: {exc}")
        days = {}

    with _lock:
        _map_cache[key] = (fp, days)
        _map_cache.move_to_end(key)
        while len(_map_cache) > _MAP_CACHE_LIMIT:
            _map_cache.popitem(last=False)
    return days


def _day_member_names(tar_path: Path, index: dict[str, Any] | None,
                      day: str) -> dict[str, str]:
    """basename -> member name for one day.  Index names are authoritative;
    member map fills in names the index does not track."""
    names: dict[str, str] = {}
    month = _norm_month(tar_path.name)
    indexed = _index_days(index).get(day)
    if indexed:
        for name in indexed:
            if _is_safe_filename(name):
                names[name] = f"{month}/{day}/{name}"
    for name, member in _member_map(tar_path).get(day, {}).items():
        names.setdefault(name, member)
    return names


def _iter_cold_sources(hub: Path, day: str, *,
                       shallow: bool = False
                       ) -> Iterable[tuple[str, Path, dict | None]]:
    """(month, tar_path, index) for every archive claiming `day`.

    Index-listed days skip the member scan entirely (no gzip stream opened
    just to answer inventory).  Corrupt archives are yielded nowhere.
    Index-less (legacy) archives normally need one streaming name-scan;
    with shallow=True they are yielded lazily — callers that only need
    confirmed days may skip their membership, saving a full decompress.
    """
    for month, tar in _list_archives(hub):
        index = _read_index(tar)
        if index is not None:
            if day not in _index_days(index):
                continue                      # cheap: trust the index
        elif not shallow:
            if day not in _member_map(tar):
                continue                      # legacy: scan names once
        if _verify_archive(tar) == "corrupt":
            continue
        yield month, tar, index


def _cold_source_confirmed(tar_path: Path, index: dict[str, Any] | None,
                           day: str) -> bool:
    """True when `day` provably lives in this archive (index hit or member
    name present after a scan)."""
    if index is not None:
        return day in _index_days(index)
    return day in _member_map(tar_path)


def _read_day_bytes(tar_path: Path, day: str) -> dict[str, bytes]:
    """{basename: bytes} for one day inside an archive — single streaming
    pass, bounded LRU gated on the archive fingerprint, never touches the
    filesystem for output."""
    key = (str(tar_path), day)
    fp = _archive_fp(tar_path)
    if fp is None:
        return {}
    with _lock:
        hit = _day_cache.get(key)
        if hit is not None and hit[0] == fp:
            _day_cache.move_to_end(key)
            return hit[1]
    if _verify_archive(tar_path) == "corrupt":
        return {}
    wanted = _day_member_names(tar_path, _read_index(tar_path), day)
    out: dict[str, bytes] = {}
    if wanted:
        try:
            with tarfile.open(tar_path, "r|gz") as tar:
                for m in tar:
                    parts = _member_parts(m.name)
                    if parts is None or parts[1] != day:
                        continue
                    name = parts[-1]
                    if name not in wanted or not m.isfile():
                        continue
                    if m.size > MAX_MEMBER_BYTES:
                        _note_issue(tar_path,
                                    f"oversized member skipped: {m.name[:80]}")
                        continue
                    fh = tar.extractfile(m)
                    if fh is not None:
                        out[name] = fh.read(MAX_MEMBER_BYTES + 1)[:MAX_MEMBER_BYTES]
        except (tarfile.TarError, OSError, EOFError) as exc:
            _note_issue(tar_path, f"read failed: {exc}")
            return {}
    with _lock:
        _day_cache[key] = (fp, out)
        _day_cache.move_to_end(key)
        while len(_day_cache) > _DAY_CACHE_LIMIT:
            _day_cache.popitem(last=False)
    return out


def _read_member_text(hub: Path, day: str, filename: str) -> str | None:
    for _month, tar, _index in _iter_cold_sources(hub, day):
        payload = _read_day_bytes(tar, day)
        if filename in payload:
            return payload[filename].decode("utf-8", errors="replace")
    return None


def _safe_hot_file(hub: Path, day: str, filename: str) -> Path | None:
    """daily/<day>/<filename> resolved for reading, or None.

    Regular files pass; symlinked entries must resolve INSIDE the hub root
    — a sessions.jsonl pointing at ~/.ssh or /etc would otherwise turn the
    read API into a secrets exfiltration path."""
    if not _is_safe_filename(filename):
        return None
    path = hub / "daily" / day / filename
    try:
        if path.is_symlink():
            root = Path(os.path.realpath(hub))
            target = Path(os.path.realpath(path))
            if not str(target).startswith(str(root) + os.sep):
                return None
        if not path.is_file():
            return None
    except OSError:
        return None
    return path


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def iter_days(hub_home: os.PathLike | str) -> list[str]:
    """Every known day (hot daily/ dirs ∪ archived days), sorted ISO strings."""
    hub = _hub(hub_home)
    days: set[str] = set()
    daily = hub / "daily"
    if daily.is_dir():
        for p in daily.iterdir():
            if p.is_dir() and DAY_RE.match(p.name):
                days.add(p.name)
    for _month, tar in _list_archives(hub):
        index = _read_index(tar)
        if index is not None:
            days.update(_index_days(index))
            continue
        if _verify_archive(tar) == "corrupt":
            continue                       # fail closed: no index, no proof
        days.update(_member_map(tar))
    return sorted(days)


def has_day(hub_home: os.PathLike | str, day: str | date) -> bool:
    hub = _hub(hub_home)
    d = _norm_day(day)
    if (hub / "daily" / d).is_dir():
        return True
    return any(True for _ in _iter_cold_sources(hub, d))


def read_day_text(hub_home: os.PathLike | str, day: str | date,
                  filename: str, *, merge: bool = False) -> str | None:
    """File content for a day.  Hot daily/<day>/<filename> wins; otherwise the
    bytes are streamed out of the month archive.  None when absent, unsafe,
    or the archive failed integrity verification.

    merge=True (JSONL files only): returns canonical merged lines — hot
    lines first, then cold lines whose records are not dedup duplicates —
    so a partial hot overlay can no longer hide archived originals.
    Use read_day_jsonl/iter_day_records when exact per-line locators matter.
    """
    hub = _hub(hub_home)
    d = _norm_day(day)
    if not _is_safe_filename(filename):
        return None
    hot = _safe_hot_file(hub, d, filename)
    if merge and filename.endswith(".jsonl"):
        merged = read_day_jsonl(hub, d, filename)
        return merged if merged else None
    if hot is not None:
        try:
            return hot.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return _read_member_text(hub, d, filename)


def read_day_jsonl(hub_home: os.PathLike | str, day: str | date,
                   filename: str = "sessions.jsonl") -> str:
    """Canonical merged JSONL text for a day: every hot line first, then
    cold lines whose parsed records are not dedup duplicates of hot rows.
    Original line bytes are preserved (no re-serialization); malformed
    cold lines are dropped, malformed hot lines pass through unchanged.
    Empty string when the file exists nowhere."""
    hub = _hub(hub_home)
    d = _norm_day(day)
    if not _is_safe_filename(filename):
        return ""
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()

    hot = _safe_hot_file(hub, d, filename)
    if hot is not None:
        try:
            for line in hot.read_text(encoding="utf-8",
                                      errors="replace").splitlines():
                lines.append(line)
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    seen.add(_record_key(value))
        except OSError:
            pass

    for month, tar, _index in _iter_cold_sources(hub, d):
        payload = _read_day_bytes(tar, d)
        if filename not in payload:
            continue
        text = payload[filename].decode("utf-8", errors="replace")
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(value, dict):
                continue
            key = _record_key(value)
            if key in seen:
                continue                      # dedup twin — exact ref via
            seen.add(key)                     # iter_day_records if needed
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def read_archive_text(hub_home: os.PathLike | str, day: str | date,
                      filename: str) -> str | None:
    """Cold-side-only counterpart of read_day_text: reads ONLY the archive
    copy even when a hot file exists (used for overlay comparison)."""
    d = _norm_day(day)
    if not _is_safe_filename(filename):
        return None
    return _read_member_text(_hub(hub_home), d, filename)


def archive_member_locator(hub_home: os.PathLike | str, day: str | date,
                           filename: str) -> str | None:
    """'<abs tar>!<member>' for the first verified archive holding
    day/filename, or None.  Inventory-level: no member content is read."""
    hub = _hub(hub_home)
    d = _norm_day(day)
    if not _is_safe_filename(filename):
        return None
    for month, tar, index in _iter_cold_sources(hub, d):
        if filename in _day_member_names(tar, index, d):
            return f"{tar}!{month}/{d}/{filename}"
    return None


def iter_day_files(hub_home: os.PathLike | str, day: str | date) -> list[dict[str, Any]]:
    """Inventory of one day across hot + cold.  No file contents are read;
    cold sizes come from the index when available."""
    hub = _hub(hub_home)
    d = _norm_day(day)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    day_dir = hub / "daily" / d
    if day_dir.is_dir():
        try:
            hub_root = Path(os.path.realpath(hub))
        except OSError:
            hub_root = hub.resolve()
        for p in sorted(day_dir.rglob("*")):
            if not p.is_file():
                continue
            if not _is_safe_filename(p.name):
                continue                      # same guard as cold side
            try:
                if not str(Path(os.path.realpath(p))).startswith(
                        str(hub_root) + os.sep):
                    continue                  # escaping symlink/whiteout
            except OSError:
                continue
            rel = p.relative_to(day_dir).as_posix()
            seen.add(rel)
            out.append({
                "day": d, "name": p.name, "path": rel, "source": "hot",
                "size": p.stat().st_size,
                "symlink": p.is_symlink(),
                "locator": str(p),
            })
    for month, tar, index in _iter_cold_sources(hub, d):
        names = _day_member_names(tar, index, d)
        indexed = _index_days(index).get(d) or {}
        for name in sorted(names):
            if name in seen:
                continue
            seen.add(name)
            out.append({
                "day": d, "name": name, "path": name, "source": "cold",
                "size": indexed.get(name),
                "locator": f"{tar}!{month}/{d}/{name}",
            })
    return out


def _record_key(record: dict[str, Any]) -> tuple[str, str]:
    """Dedup identity: (event_id, content-hash) when event_id is present —
    so two records sharing an id but with DIFFERENT content are kept as a
    conflict, never silently merged — else canonical content hash alone."""
    canon = json.dumps(record, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=str)
    digest = hashlib.sha1(canon.encode("utf-8")).hexdigest()
    eid = record.get("event_id")
    if eid:
        return ("event_id+content", f"{eid}:{digest}")
    return ("content", digest)


def _content_key(record: dict[str, Any]) -> str:
    canon = json.dumps(record, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":"), default=str)
    return hashlib.sha1(canon.encode("utf-8")).hexdigest()


def _iter_jsonl_text(text: str) -> Iterable[tuple[dict[str, Any], int]]:
    for line_no, line in enumerate(text.splitlines(), 1):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value, line_no


def iter_day_records(hub_home: os.PathLike | str, day: str | date,
                     filename: str = "sessions.jsonl") -> list[dict[str, Any]]:
    """Merged hot+cold JSONL rows for one day.

    Dedup by (event_id, content) — a pair sharing an event_id but with
    DIFFERENT content is kept on both sides and flagged
    conflict_of=<locator of first copy>, so an overwritten/replayed id can
    never silently hide the archived original.  Exact duplicates are kept
    as rows flagged dup=True (hot copy first, live locator wins);
    cold-only events surface with "<tar>!<member>#L<n>" locators.
    """
    hub = _hub(hub_home)
    d = _norm_day(day)
    if not _is_safe_filename(filename):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    by_id: dict[str, dict[str, str]] = {}   # event_id -> {content_hash: locator}

    def _push(record: dict[str, Any], line_no: int, source: str,
              locator: str) -> None:
        key = _record_key(record)
        dup = key in seen
        seen.add(key)
        row = {"record": record, "line": line_no, "source": source,
               "locator": locator, "dup": dup}
        eid = record.get("event_id")
        if eid is not None:
            ch = _content_key(record)
            prior = by_id.setdefault(str(eid), {})
            if prior and ch not in prior:
                # same event_id, divergent content — a conflict, not a dup
                row["conflict_of"] = next(iter(prior.values()))
            prior.setdefault(ch, locator)
        rows.append(row)

    hot = _safe_hot_file(hub, d, filename)
    if hot is not None:
        try:
            text = hot.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for record, line_no in _iter_jsonl_text(text):
            _push(record, line_no, "hot", f"{hot}#L{line_no}")

    for month, tar, _index in _iter_cold_sources(hub, d):
        payload = _read_day_bytes(tar, d)
        if filename not in payload:
            continue
        text = payload[filename].decode("utf-8", errors="replace")
        member = f"{month}/{d}/{filename}"
        for record, line_no in _iter_jsonl_text(text):
            _push(record, line_no, "cold", f"{tar}!{member}#L{line_no}")
    return rows


def archive_status(hub_home: os.PathLike | str, month: str, *,
                   verify: bool = True) -> dict[str, Any] | None:
    """Targeted integrity status for ONE month archive — the guard for
    report source fingerprints and any caller that must fail closed when a
    specific archive is corrupt, without hashing unrelated tarballs.

    Returns None when no archive exists for `month`.
    status: ok | unverified | corrupt | no_index | not_checked
      ok          verify=True, index sha256 checked and matches
      unverified  verify=True, index present but carries no sha256
      corrupt     sha mismatch or unreadable tar — fail closed
      no_index    verify=False and no index sidecar (legacy)
      not_checked verify=False with an index but no cached verification
    verify=True hashes ONLY this month's tarball (memoized by fingerprint).
    """
    hub = _hub(hub_home)
    if not MONTH_RE.match(month or ""):
        return None
    tar = hub / "archive" / month[:4] / f"{month}.tar.gz"
    if not tar.is_file():
        return None
    index = _read_index(tar)
    if verify:
        status = _verify_archive(tar)
    else:
        cached = _integrity_cache.get(str(tar))
        if cached and cached[0] == _archive_fp(tar):
            status = cached[1]
        else:
            status = "not_checked" if index is not None else "no_index"
    info = (index or {}).get("archive") or {}
    return {
        "month": month,
        "status": status,
        "verified": status == "ok",
        "tar": str(tar),
        "index": str(_index_path(tar)),
        "index_present": index is not None,
        "sha256": info.get("sha256"),
        "issues": [i["issue"] for i in last_issues()
                   if i["archive"] == str(tar)],
    }


def archives(hub_home: os.PathLike | str, *, verify: bool = True
             ) -> list[dict[str, Any]]:
    """Inventory + integrity status of every month archive.

    status: ok | unverified | corrupt | not_checked | no_index
    verify=True  -> each tarball's sha256 is compared to the index
                   (memoized; the only call that hashes full archives).
    verify=False -> metadata-only inventory for MANIFEST/doctor paths:
                   never opens a gzip stream and never hashes; integrity
                   surfaces only when a prior verification already ran
                   (cached), otherwise "not_checked"/"no_index".
    """
    hub = _hub(hub_home)
    out: list[dict[str, Any]] = []
    for month, tar in _list_archives(hub):
        index = _read_index(tar)
        if verify:
            status = _verify_archive(tar)
        else:
            cached = _integrity_cache.get(str(tar))
            if cached and cached[0] == _archive_fp(tar):
                status = cached[1]            # stale-free: fingerprint match
            elif index is None:
                status = "no_index"
            else:
                status = "not_checked"
        days = sorted(_index_days(index)) if index is not None else (
            sorted(_member_map(tar))
            if verify and status != "corrupt" else [])
        info = (index or {}).get("archive") or {}
        out.append({
            "month": month,
            "tar": str(tar),
            "index": str(_index_path(tar)),
            "index_present": index is not None,
            "status": status,
            "verified": status == "ok",
            "sha256": info.get("sha256"),
            "bytes": info.get("bytes"),
            "day_count": len(days),
            "days": days,
            "issues": [i["issue"] for i in last_issues()
                       if i["archive"] == str(tar)],
        })
    return out
