"""Persistent incremental document/token cache for hubmemory hybrid retrieval.

Acceleration layer ONLY — no ranking changes. `hybrid_query.iter_documents`
still owns corpus semantics (source truth is never rewritten); this module
memoizes its output per source bucket so a warm lookup skips re-reading and
re-tokenizing unchanged days. Exact BM25 is preserved because cached docs
return byte-identical `text`/`record`/`path`/`line` plus `_index_tokens`
(token -> count, plain dict) equivalent to `Counter(tokenize(doc["text"]))`.

Usage (lead integration side)::

    from retrieval_index import load_documents, make_hybrid_loader
    docs, counters, stats = load_documents(
        hub_home, days=30, ref_date=ref_date,
        loader=make_hybrid_loader(hub_home), tokenizer=tokenize)
    # docs carry doc["_index_tokens"]; counters[i] == Counter(tokenize(docs[i]["text"]))

Loader contract (required for correctness):
    loader(day: str | None) -> list[doc]
      day=None         -> global docs only (memory + profiles), e.g.
                          iter_documents(..., exact_day="0000-00-00")
      day="YYYY-MM-DD" -> that day's docs only, e.g.
                          iter_documents(..., exact_day=day, include_global=False)
    Documents must be JSON-serializable (iter_documents records are).

Merge order mirrors iter_documents exactly:
    non-profile globals (memory) ++ day buckets (ISO desc) ++ profile docs.

Freshness guarantees:
  * Bucket signatures are recomputed on EVERY load from filesystem state;
    cached payloads are served only on an exact signature match, so append /
    edit / delete / rename-replace of any contributing source invalidates.
  * Archive claims come from a persisted archive->days inventory keyed on a
    composite fingerprint mirroring cold_storage._archive_fp (tar stat+head
    sha1, index sidecar stat+head sha1, recorded sha256). A coordinated
    tar+index replacement invalidates every day the archive claims.
  * Archive verification failure (sha mismatch / unreadable / corrupt) raises
    ValueError loudly instead of serving anything derived from it.
  * Any other cache-layer failure falls back to an explicit full scan
    (loader per bucket) with stats["fallback"]=True — the caller always gets
    fresh docs; stale bytes are never served.

Concurrency: SQLite WAL + busy_timeout, INSERT OR REPLACE per bucket. Racing
writers converge on the same signature->payload mapping; readers never see a
partial bucket (single-row writes inside one transaction).

Storage: <hub>/state/retrieval_index.sqlite3 (+ -wal/-shm). Directory 0700,
files 0600, contents are raw cached docs — treat as private. Symlinked state
dir or db file is refused (defense against planting foreign files).

CLI:
    retrieval_index.py --hub <path> status
    retrieval_index.py --hub <path> rebuild [--days N] [--ref-date YYYY-MM-DD]
    retrieval_index.py --hub <path> clear
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import tarfile
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

SCHEMA_VERSION = 1
TOKENIZER_CACHE_VERSION = 1
_FP_HEAD = 64 * 1024
_MEMBER_PARTS = 3
_NO_DAY = "0000-00-00"          # exact_day sentinel that never matches
_WIDE_DAYS = 36525              # per-day loader: cutoff ~100y back, keeps all

# Basenames iter_documents reads out of a hot daily/<day>/ dir.
_HOT_FILES = ("evidence.jsonl", "sessions.jsonl", "diary.md", "conversations.md")

try:
    import cold_storage as _cold  # presence gates archive use (parity w/ hybrid)
except ImportError:               # mirror hybrid_query: no cold => no archives
    _cold = None


class CacheError(RuntimeError):
    """Internal: force explicit-scan fallback without touching stored rows."""


# ---------------------------------------------------------------------------
# fingerprints


def _file_fp(path: Path) -> tuple | None:
    """(ino, size, mtime_ns, sha1 head64k) — catches append/edit/delete and
    atomic same-size/same-mtime rename replacement. Follows symlinks so the
    signature reflects the bytes iter_documents would read."""
    try:
        st = path.stat()
        with path.open("rb") as fh:
            head = fh.read(_FP_HEAD)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns,
            hashlib.sha1(head).hexdigest())


def _index_path(tar_path: Path) -> Path:
    name = tar_path.name
    return tar_path.with_name(name[: -len(".tar.gz")] + ".index.json") \
        if name.endswith(".tar.gz") else tar_path.with_suffix(".index.json")


def _read_index(tar_path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_index_path(tar_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _index_days(index: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    """day -> {filename: size}; mirrors cold_storage._index_days."""
    out: dict[str, dict[str, int]] = {}
    for entry in (index or {}).get("days") or []:
        if not isinstance(entry, dict):
            continue
        day = entry.get("date")
        if not isinstance(day, str):
            continue
        out[day] = {k: v for k, v in entry.items()
                    if k != "date" and k != "archived" and isinstance(v, int)}
    return out


def _expected_sha(index: dict[str, Any] | None) -> str | None:
    if not index:
        return None
    sha = (index.get("archive") or {}).get("sha256")
    return sha if isinstance(sha, str) else None


def _archive_fp(tar_path: Path, index: dict[str, Any] | None) -> tuple | None:
    """Composite identity mirroring cold_storage._archive_fp."""
    tf = _file_fp(tar_path)
    if tf is None:
        return None
    index_fp = _file_fp(_index_path(tar_path))
    return (tf, index_fp, _expected_sha(index))


def _sha256_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _scan_member_days(tar_path: Path) -> set[str]:
    """Day names provably inside an index-less archive (streaming pass)."""
    days: set[str] = set()
    try:
        with tarfile.open(tar_path, "r|gz") as tar:
            for m in tar:
                if not m.isfile():
                    continue
                name = m.name or ""
                if name.startswith("/") or name.startswith("~") or ".." in name:
                    continue
                parts = [p for p in name.split("/") if p]
                if len(parts) != _MEMBER_PARTS:
                    continue
                try:
                    date.fromisoformat(parts[1])
                except ValueError:
                    continue
                days.add(parts[1])
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"archive unreadable: {tar_path}: {exc}")
    return days


def _compute_archive(tar_path: Path) -> tuple[str, list[str], tuple | None]:
    """(status, days, composite_fp) — mirrors cold_storage verification.
    'ok'         index exists, sha256 recorded and matches.
    'unverified' index missing or sha-less; member scan proves days.
    'corrupt'    sha mismatch / unreadable — ValueError, never served."""
    index = _read_index(tar_path)
    fp = _archive_fp(tar_path, index)
    if fp is None:
        raise ValueError(f"archive unreadable/missing: {tar_path}")
    if index is not None:
        recorded = _expected_sha(index)
        days = sorted(_index_days(index))
        if recorded:
            actual = _sha256_file(tar_path)
            if actual != recorded:
                raise ValueError(
                    f"archive sha256 mismatch (corrupt, quarantined): {tar_path}")
            return "ok", days, fp
        return "unverified", days, fp
    return "unverified", sorted(_scan_member_days(tar_path)), fp


# ---------------------------------------------------------------------------
# sqlite layer


def _db_path(hub_home: Path) -> Path:
    return hub_home / "state" / "retrieval_index.sqlite3"


def _reject_symlink(path: Path) -> None:
    if os.path.islink(path):
        raise CacheError(f"refusing symlinked cache path: {path}")


def _connect(hub_home: Path) -> sqlite3.Connection:
    state = hub_home / "state"
    db = _db_path(hub_home)
    _reject_symlink(state)
    for p in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
        _reject_symlink(p)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(state, 0o700)
    except OSError:
        pass
    try:
        conn = sqlite3.connect(str(db), timeout=10.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error as exc:
        raise CacheError(f"db open failed: {exc}")
    _chmod_private(db)
    return conn


def _chmod_private(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(path) + suffix)
        try:
            if p.exists():
                os.chmod(p, 0o600)
        except OSError:
            pass


def _init_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta "
        "(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS buckets "
        "(bucket TEXT PRIMARY KEY, signature TEXT NOT NULL, "
        " payload TEXT NOT NULL, built_at REAL NOT NULL, "
        " doc_count INTEGER NOT NULL)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS archives "
        "(path TEXT PRIMARY KEY, fp TEXT NOT NULL, days TEXT NOT NULL, "
        " status TEXT NOT NULL)")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        conn.execute("DELETE FROM buckets")
        conn.execute("DELETE FROM archives")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES "
        "('tokenizer_cache_version', ?)", (str(TOKENIZER_CACHE_VERSION),))
    conn.commit()


def _archive_record(conn: sqlite3.Connection, tar_path: Path,
                    stats: dict) -> dict[str, Any]:
    """Persisted archive->days inventory, refreshed only when fp changes.
    Raises ValueError on corruption — callers let it propagate."""
    index = _read_index(tar_path)
    fp = _archive_fp(tar_path, index)
    if fp is None:
        raise ValueError(f"archive unreadable/missing: {tar_path}")
    key = str(tar_path)
    fp_json = json.dumps(fp)
    row = conn.execute(
        "SELECT fp, days, status FROM archives WHERE path=?", (key,)).fetchone()
    if row and row[0] == fp_json:
        return {"fp": fp, "days": json.loads(row[1]), "status": row[2]}
    status, days, _ = _compute_archive(tar_path)
    stats["archives_rescanned"] += 1
    conn.execute(
        "INSERT OR REPLACE INTO archives(path, fp, days, status) "
        "VALUES (?,?,?,?)", (key, fp_json, json.dumps(days), status))
    return {"fp": fp, "days": days, "status": status}


def _list_archives(hub_home: Path) -> list[Path]:
    root = hub_home / "archive"
    if not root.is_dir():
        return []
    return sorted(root.glob("20??/20??-??.tar.gz"))


def _day_sources(hub_home: Path, conn: sqlite3.Connection | None,
                 stats: dict) -> dict[str, dict[str, Any]]:
    """day -> {'hot': {name: fp|None}|None, 'cold': [(tar, fp)]}.
    conn=None (explicit-scan fallback) computes inventory without persisting."""
    days: dict[str, dict[str, Any]] = {}
    daily_root = hub_home / "daily"
    if daily_root.is_dir():
        for p in daily_root.glob("20??-??-??"):
            days.setdefault(p.name, {"hot": None, "cold": []})
    for name in sorted(days):
        if (daily_root / name).is_dir():
            days[name]["hot"] = {
                f: _file_fp(daily_root / name / f) for f in _HOT_FILES}
    if _cold is not None:
        for tar in _list_archives(hub_home):
            if conn is not None:
                # cached path: ValueError propagates — never serve stale
                rec = _archive_record(conn, tar, stats)
            else:
                # explicit-scan fallback mirrors cold fail-closed semantics:
                # corrupt archives are quarantined, not fatal
                try:
                    rec = _fresh_archive(tar, stats)
                except ValueError:
                    stats["archives_corrupt"] = \
                        stats.get("archives_corrupt", 0) + 1
                    continue
            stats["archives_tracked"] += 1
            for d in rec["days"]:
                days.setdefault(d, {"hot": None, "cold": []})
                days[d]["cold"].append((str(tar), rec["fp"]))
    return days


def _fresh_archive(tar_path: Path, stats: dict) -> dict[str, Any]:
    status, days, fp = _compute_archive(tar_path)
    stats["archives_rescanned"] += 1
    return {"fp": fp, "days": days, "status": status}


# ---------------------------------------------------------------------------
# signatures + payload


def _signature(payload: Any) -> str:
    blob = json.dumps(payload, separators=(",", ":"),
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _global_signature(hub_home: Path) -> str:
    return _signature({
        "v": TOKENIZER_CACHE_VERSION,
        "memory": _file_fp(hub_home / "memory" / "memories.jsonl"),
    })


def _profiles_signature(hub_home: Path) -> str:
    root = hub_home / "profile"
    return _signature({
        "v": TOKENIZER_CACHE_VERSION,
        "constraints": _file_fp(root / "constraints.md"),
        "preferences": _file_fp(root / "preferences.md"),
    })


def _day_signature(day: str, src: dict[str, Any]) -> str:
    cold = sorted((tar, fp) for tar, fp in src["cold"])
    return _signature({
        "v": TOKENIZER_CACHE_VERSION, "day": day,
        "hot": src["hot"], "cold": cold,
    })


def _bucket_get(conn: sqlite3.Connection, bucket: str,
                signature: str) -> list[dict[str, Any]] | None:
    row = conn.execute(
        "SELECT signature, payload FROM buckets WHERE bucket=?",
        (bucket,)).fetchone()
    if not row or row[0] != signature:
        return None
    try:
        data = json.loads(row[1])
    except ValueError:
        return None
    if data.get("v") != TOKENIZER_CACHE_VERSION:
        return None
    docs = data.get("docs")
    return docs if isinstance(docs, list) else None


def _bucket_put(conn: sqlite3.Connection, bucket: str, signature: str,
                docs: list[dict[str, Any]]) -> bool:
    """INSERT OR REPLACE — racing writers converge on identical signature->
    payload content; a row can never be half-written (single statement)."""
    try:
        payload = json.dumps({"v": TOKENIZER_CACHE_VERSION, "docs": docs},
                             separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return False
    conn.execute(
        "INSERT OR REPLACE INTO buckets(bucket, signature, payload, "
        "built_at, doc_count) VALUES (?,?,?,?,?)",
        (bucket, signature, payload, time.time(), len(docs)))
    return True


def _ensure_tokens(docs: list[dict[str, Any]],
                   tokenizer: Callable[[str], list[str]]) -> None:
    for doc in docs:
        tokens = doc.get("_index_tokens")
        if not isinstance(tokens, dict):
            doc["_index_tokens"] = dict(Counter(tokenizer(doc.get("text", ""))))


# ---------------------------------------------------------------------------
# public API


def _split_globals(docs: list[dict[str, Any]]) -> tuple[list, list]:
    """(non-profile docs, profile docs) preserving loader order within each."""
    head = [d for d in docs if d.get("source") != "profile"]
    tail = [d for d in docs if d.get("source") == "profile"]
    return head, tail


def _stats() -> dict[str, Any]:
    """Compact keys only — safe to surface on every search."""
    return {"docs": 0, "hit_count": 0, "miss_count": 0, "uncacheable": 0,
            "elapsed_ms": 0, "fallback": False,
            "archives_tracked": 0, "archives_rescanned": 0}


def _cold_issues() -> list[dict[str, Any]]:
    if _cold is None:
        return []
    try:
        return _cold.last_issues() or []
    except Exception:
        return []


def load_documents(hub_home, days: int = 30, ref_date: date | None = None,
                   *, loader: Callable[[str | None], list[dict[str, Any]]],
                   tokenizer: Callable[[str], list[str]] | None = None):
    """(docs, counters, stats). Cached docs carry `_index_tokens`;
    counters[i] == Counter(tokenize(docs[i]['text'])) for every i.

    stats keys: docs, hit_count, miss_count, uncacheable, elapsed_ms,
    fallback, archives_tracked, archives_rescanned, plus fallback_reason /
    cold_issues only when non-empty. ValueError propagates on archive
    corruption (never serve stale); other cache faults -> fallback scan."""
    hub_home = Path(hub_home)
    t0 = time.monotonic()
    if tokenizer is None:
        tokenizer = _hybrid().tokenize
    try:
        docs, stats = _load_cached(hub_home, days, ref_date, loader, tokenizer)
    except (CacheError, sqlite3.Error, OSError) as exc:
        docs, stats = _load_scan(hub_home, days, ref_date, loader, tokenizer)
        stats["fallback"] = True
        # type name only — exception text may embed private paths/content
        stats["fallback_reason"] = type(exc).__name__
    counters = [Counter(d["_index_tokens"]) if isinstance(
        d.get("_index_tokens"), dict)
        else Counter(tokenizer(d.get("text", ""))) for d in docs]
    stats["docs"] = len(docs)
    stats["elapsed_ms"] = round((time.monotonic() - t0) * 1000.0, 2)
    issues = _cold_issues()
    if issues:
        stats["cold_issues"] = issues
    return docs, counters, stats


def _window_days(src_days: dict[str, dict[str, Any]], days: int,
                 ref_date: date | None) -> list[str]:
    cutoff = (ref_date or date.today()) - timedelta(days=max(0, days - 1))
    out = []
    for name in src_days:
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if d >= cutoff:                    # mirrors iter_documents: keeps
            out.append(name)               # future days for _within_cutoff
    return sorted(out, reverse=True)


def _one_day_source(hub_home: Path, conn: sqlite3.Connection,
                    stats: dict, day: str) -> dict[str, Any]:
    """Fresh signature input for a single day (hot fps + cold claims).
    hot=None when no daily/<day> dir — must stay identical to the
    normalization _day_sources produces for cold-only days."""
    daily = hub_home / "daily" / day
    src = {"hot": None, "cold": []}
    if daily.is_dir():
        src["hot"] = {f: _file_fp(daily / f) for f in _HOT_FILES}
    if _cold is not None:
        for tar in _list_archives(hub_home):
            rec = _archive_record(conn, tar, stats)
            if day in rec["days"]:
                src["cold"].append((str(tar), rec["fp"]))
    return src


def _try_stable(sig_fn, load_fn):
    """(docs, signature, cacheable). Snapshot sig -> load -> re-verify; a
    source that changed mid-load is retried ONCE, and if still unstable the
    fresh docs are served without caching (never a torn write under a sig
    that no longer describes the source)."""
    sig = sig_fn()
    docs = load_fn()
    if sig_fn() == sig:
        return docs, sig, True
    sig = sig_fn()
    docs = load_fn()
    return docs, sig, sig_fn() == sig


def _load_cached(hub_home, days, ref_date, loader, tokenizer):
    stats = _stats()
    conn = _connect(hub_home)
    try:
        _init_schema(conn)
        src_days = _day_sources(hub_home, conn, stats)
        wanted = _window_days(src_days, days, ref_date)

        # --- globals: one loader(None) covers memory + profiles, but each
        # bucket has its own signature so a profile edit mid-load cannot be
        # cached under the memory sig (and vice versa). ---
        global_docs: list[dict[str, Any]] | None = None
        head: list[dict[str, Any]] | None = None
        tail: list[dict[str, Any]] | None = None
        g_sig = _global_signature(hub_home)
        p_sig = _profiles_signature(hub_home)
        cached = _bucket_get(conn, "global", g_sig)
        if cached is not None:
            stats["hit_count"] += 1
            head = cached
        cached = _bucket_get(conn, "profiles", p_sig)
        if cached is not None:
            stats["hit_count"] += 1
            tail = cached
        if head is None or tail is None:
            stats["miss_count"] += (head is None) + (tail is None)

            def _both_sigs():
                return (_global_signature(hub_home),
                        _profiles_signature(hub_home))

            def _load_globals():
                g = loader(None)
                _ensure_tokens(g, tokenizer)
                return g

            global_docs, (g_sig2, p_sig2), stable = _try_stable(
                _both_sigs, _load_globals)
            g_head, g_tail = _split_globals(global_docs)
            if head is None:
                head = g_head
                if not (stable and _bucket_put(conn, "global", g_sig2, head)):
                    stats["uncacheable"] += 1
            if tail is None:
                tail = g_tail
                if not (stable and _bucket_put(conn, "profiles", p_sig2, tail)):
                    stats["uncacheable"] += 1

        pieces: dict[str, list[dict[str, Any]]] = {
            "global": head or [], "profiles": tail or []}
        for day in wanted:
            bucket = f"day:{day}"
            sig = _day_signature(day, src_days[day])
            cached = _bucket_get(conn, bucket, sig)
            if cached is not None:
                stats["hit_count"] += 1
                pieces[bucket] = cached
                continue
            stats["miss_count"] += 1

            def _sig(d=day):
                return _day_signature(
                    d, _one_day_source(hub_home, conn, stats, d))

            def _load(d=day):
                fresh = loader(d)
                _ensure_tokens(fresh, tokenizer)
                return fresh

            fresh, sig2, stable = _try_stable(_sig, _load)
            if not (stable and _bucket_put(conn, bucket, sig2, fresh)):
                stats["uncacheable"] += 1
            pieces[bucket] = fresh
        conn.commit()
    finally:
        _chmod_private(_db_path(hub_home))
        conn.close()

    docs: list[dict[str, Any]] = []
    docs.extend(pieces["global"])
    for d in wanted:
        docs.extend(pieces[f"day:{d}"])
    docs.extend(pieces["profiles"])
    return docs, stats


def _load_scan(hub_home, days, ref_date, loader, tokenizer):
    """Explicit scan fallback: same buckets straight from loader, no db."""
    stats = _stats()
    src_days = _day_sources(hub_home, None, stats)
    wanted = _window_days(src_days, days, ref_date)
    global_docs = loader(None)
    _ensure_tokens(global_docs, tokenizer)
    head, tail = _split_globals(global_docs)
    docs = list(head)
    stats["miss_count"] += 1
    for d in wanted:
        fresh = loader(d)
        _ensure_tokens(fresh, tokenizer)
        docs.extend(fresh)
        stats["miss_count"] += 1
    docs.extend(tail)
    return docs, stats


def _hybrid():
    try:
        import hybrid_query
        return hybrid_query
    except ImportError as exc:  # pragma: no cover - environment issue
        raise CacheError(f"hybrid_query unavailable: {exc}")


def make_hybrid_loader(hub_home) -> Callable[[str | None], list[dict[str, Any]]]:
    """Loader satisfying the bucket contract via hybrid_query.iter_documents.
    day=None -> globals only (sentinel exact_day matches nothing);
    day str  -> that day's docs only."""
    hub_home = Path(hub_home)

    def loader(day: str | None) -> list[dict[str, Any]]:
        hq = _hybrid()
        if day is None:
            return hq.iter_documents(hub_home, days=_WIDE_DAYS,
                                     exact_day=_NO_DAY, include_global=True)
        return hq.iter_documents(hub_home, days=_WIDE_DAYS,
                                 exact_day=day, include_global=False)

    return loader


# ---------------------------------------------------------------------------
# CLI


def _cmd_status(hub_home: Path) -> int:
    db = _db_path(hub_home)
    out = {"hub": str(hub_home), "db": str(db), "exists": db.exists()}
    if db.exists() and not os.path.islink(db):
        try:
            conn = sqlite3.connect(str(db), timeout=5.0)
            out["buckets"], out["cached_docs"] = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(doc_count),0) "
                "FROM buckets").fetchone()
            out["archives"] = conn.execute(
                "SELECT COUNT(*) FROM archives").fetchone()[0]
            out["user_version"] = conn.execute(
                "PRAGMA user_version").fetchone()[0]
            conn.close()
            out["size_bytes"] = db.stat().st_size
        except (sqlite3.Error, OSError) as exc:
            out["error"] = str(exc)
    print(json.dumps(out, indent=2, default=str))
    return 0


def _cmd_rebuild(hub_home: Path, days: int, ref_date: date | None) -> int:
    conn = _connect(hub_home)
    _init_schema(conn)
    conn.execute("DELETE FROM buckets")
    conn.execute("DELETE FROM archives")
    conn.commit()
    conn.close()
    docs, _counters, stats = load_documents(
        hub_home, days=days, ref_date=ref_date,
        loader=make_hybrid_loader(hub_home))
    stats["rebuilt_docs"] = len(docs)
    print(json.dumps(stats, indent=2, default=str))
    return 0


def _cmd_clear(hub_home: Path) -> int:
    db = _db_path(hub_home)
    removed = []
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db) + suffix)
        if p.exists() and not os.path.islink(p):
            p.unlink()
            removed.append(p.name)
    print(json.dumps({"removed": removed}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hub", default=os.environ.get(
        "HUBMEMORY_HOME", str(Path.home() / "hubmemory")))
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    rb = sub.add_parser("rebuild")
    rb.add_argument("--days", type=int, default=30)
    rb.add_argument("--ref-date", default=None,
                    help="YYYY-MM-DD anchor for rebuild")
    sub.add_parser("clear")
    args = parser.parse_args(argv)
    hub = Path(args.hub).expanduser()
    if args.cmd == "status":
        return _cmd_status(hub)
    if args.cmd == "clear":
        return _cmd_clear(hub)
    ref = date.fromisoformat(args.ref_date) \
        if getattr(args, "ref_date", None) else None
    return _cmd_rebuild(hub, getattr(args, "days", 30), ref)


if __name__ == "__main__":
    sys.exit(main())
