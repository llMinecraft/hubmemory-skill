#!/usr/bin/env python3
"""Cross-platform advisory file lock + process check.

POSIX -> fcntl.flock ; Windows -> msvcrt.locking.
Shared json/jsonl state mutations go through these helpers so hubmemory
works on macOS, Linux and Windows without edits.
"""
from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

IS_WINDOWS = os.name == 'nt'

if not IS_WINDOWS:
    import fcntl


@contextmanager
def file_lock(path):
    """Exclusive advisory lock on path. Yields an open binary handle."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        if IS_WINDOWS:
            import msvcrt
            handle.seek(0)
            deadline = time.time() + 15
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    if time.time() > deadline:
                        raise TimeoutError('lock timeout: ' + str(path))
                    time.sleep(0.05)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
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


def acquire_lock(path):
    """Non-contextmanager variant for report_due; returns a holder dict."""
    holder = {'cm': file_lock(path), 'handle': None}
    holder['handle'] = holder['cm'].__enter__()
    return holder


def release_lock(holder):
    holder['cm'].__exit__(None, None, None)
    holder['handle'] = None


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        try:
            out = subprocess.run(
                ['tasklist', '/FI', 'PID eq ' + str(pid), '/FO', 'CSV', '/NH'],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return (str(pid) + ',') in out.replace('"', '')
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

