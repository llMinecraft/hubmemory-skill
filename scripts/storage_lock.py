#!/usr/bin/env python3
"""hubmemory storage locks — shared/exclusive advisory locks for daily/ writes.

Two lock files, fixed order (outermost first):

    state/archive.lock            archive_lock(hub_home)
        EXCLUSIVE while archive.py packs/verifies/publishes/deletes a month
        and while extract/rearchive mutate daily/ or archive/.  Report
        writers (report_due.writer_guard) take the same lock so protected
        publication can never interleave with archive movement.

    state/write-YYYY-MM.lock      month_lock(hub_home, "YYYY-MM")
        SHARED by every writer appending into daily/YYYY-MM-DD/ (watcher
        DailyWriter, extract staging).  EXCLUSIVE by archive.py while it
        packs + deletes that month — a late event can never race a day dir
        out from under the tarball or be deleted mid-append.

    reports/.lock                 (owned by report_due) stays INNERMOST.
        No code holding reports/.lock may acquire either lock above.

Cold readers (cold_storage.py) take NO lock.  archive.py publishes each of
tar/index/manifest via temp+os.replace (a reader never sees a torn FILE),
but the trio cannot commit atomically — a mid-publish reader may see a
transient digest mismatch and must fail closed; archive's durable journal
(state is in archive/<year>/.<month>.backup + <month>.journal.json)
guarantees the next run commits or rolls back, so no permanent wedge.

Shared mode degrades to exclusive on platforms without flock (Windows
msvcrt.locking has no shared variant) — correct, just less concurrent.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == "nt"
if not IS_WINDOWS:
    import fcntl

_LOCK_TIMEOUT = 60.0          # seconds; a wedged writer must not wedge the hub
_POLL = 0.05


def _locks_dir(hub_home: os.PathLike | str) -> Path:
    d = Path(hub_home) / "state"
    d.mkdir(parents=True, exist_ok=True)
    return d


def archive_lock_path(hub_home: os.PathLike | str) -> Path:
    """Path shared with report_due.writer_guard — do NOT rename."""
    return _locks_dir(hub_home) / "archive.lock"


def month_lock_path(hub_home: os.PathLike | str, month: str) -> Path:
    return _locks_dir(hub_home) / f"write-{month[:7]}.lock"


class LockTimeout(TimeoutError):
    pass


@contextmanager
def _flock(path: Path, shared: bool):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            deadline = time.time() + _LOCK_TIMEOUT
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise LockTimeout(f"lock timeout: {path}")
                    time.sleep(_POLL)
        else:
            flag = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
            deadline = time.time() + _LOCK_TIMEOUT
            while True:
                try:
                    fcntl.flock(handle.fileno(), flag | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, OSError):
                    if time.time() > deadline:
                        raise LockTimeout(f"lock timeout: {path}")
                    time.sleep(_POLL)
        yield handle
    finally:
        try:
            if IS_WINDOWS:
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


@contextmanager
def archive_lock(hub_home: os.PathLike | str, *, shared: bool = False):
    """Outermost storage lock. Archive/extract: shared=False (default).
    Writers protecting a whole publication may also take it (exclusive)."""
    with _flock(archive_lock_path(hub_home), shared):
        yield


@contextmanager
def month_lock(hub_home: os.PathLike | str, month: str, *, shared: bool):
    """Per-month write lock, strictly inside archive_lock.
    Writers/appends: shared=True.  Archive delete/pack: shared=False."""
    with _flock(month_lock_path(hub_home, month), shared):
        yield
