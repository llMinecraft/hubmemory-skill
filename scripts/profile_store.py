"""Atomic, locked profile storage; conservative textual dedupe, never fuzzy merge.

Semantically similar rules may differ in scope/negation. AI may propose merges,
but must preserve explicit prohibitions and source provenance rather than using
an edit-distance threshold to silently discard a rule.
"""
from __future__ import annotations
import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from lockutil import file_lock

META = re.compile(r"\s*\((?:(?:session|sha1|sources):).*\)\s*$")
STAMP = re.compile(r"^\s*[-*]\s*\[[^\]]*\]\s*")


def entry_text(line: str) -> str:
    return META.sub("", STAMP.sub("", line.strip()).removeprefix("- ")).strip()


def profile_key(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold().strip()
    # Do not remove meaningful internal operators, e.g. != vs ==.
    return re.sub(r"\s+", "", text).rstrip("。.!！;；")


def _sources(line: str) -> list[str]:
    m = META.search(line)
    if not m:
        return []
    return list(dict.fromkeys(x.strip() for x in m.group(0).strip()[1:-1].removeprefix("sources:").split(" | ") if x.strip()))


def unique_lines(lines: list[str]) -> list[str]:
    """Keep latest wording/timestamp, union provenance from duplicate lines."""
    out: list[str] = []
    positions: dict[str, int] = {}
    sources: dict[str, list[str]] = {}
    for line in lines:
        if not line.strip().startswith(("- ", "* ")):
            out.append(line)
            continue
        key = profile_key(entry_text(line))
        if not key:
            out.append(line)
            continue
        provenance = sources.setdefault(key, [])
        for source in _sources(line):
            if source not in provenance:
                provenance.append(source)
        if key in positions:
            out[positions[key]] = None
        positions[key] = len(out)
        core = META.sub("", line).rstrip()
        out.append(core)
    for key, pos in positions.items():
        if sources[key]:
            out[pos] += " (sources: " + " | ".join(sources[key]) + ")"
    return [line for line in out if line is not None]


def _atomic(path: Path, body: str) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body); f.flush(); os.fsync(f.fileno())
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def dedupe_profile_file(path: Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.parent / ".profile.lock"):
        if not path.exists():
            return 0
        body = path.read_text(encoding="utf-8")
        old = body.splitlines()
        new = unique_lines(old)
        updated = "\n".join(new) + ("\n" if new else "")
        if updated != body:
            _atomic(path, updated)
        return len(old) - len(new)


def append_profile(entries: list[dict], filename: str, profile_dir: Path) -> int:
    if filename not in {"constraints.md", "preferences.md"}:
        raise ValueError("unsupported profile filename")
    root = Path(profile_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / filename
    with file_lock(root / ".profile.lock"):
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        lines = old.splitlines()
        keys = {profile_key(entry_text(line)) for line in lines if line.lstrip().startswith(("- ", "* "))}
        added = 0
        for e in entries:
            text = " ".join(str(e.get("text") or "").split())
            if not text:
                continue
            key = profile_key(text)
            if key not in keys:
                added += 1
                keys.add(key)
            digest = hashlib.sha1(text.encode()).hexdigest()[:12]
            ts = str(e.get("ts") or "")[:16].replace("T", " ")
            sid = str(e.get("session_id") or "unknown")
            lines.append(f"- [{ts}] {text} (session: {sid}, sha1:{digest})")
        new = unique_lines(lines)
        body = "\n".join(new) + ("\n" if new else "")
        if body != old:
            _atomic(path, body)
        return added
