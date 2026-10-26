#!/usr/bin/env python3
"""Dependency-free hybrid retrieval for hubmemory.

Pipeline (all deterministic; disposable incremental token cache, scan escape hatch):

  1. intent     — retrieval_intent.parse_intent() splits the query into a
                  topic (`clean`) plus temporal / actor / artifact hints and
                  records every removed filler span.
  2. corpus     — jsonl records are read non-mutating; each doc carries its
                  exact file path + 1-based source line. Archived days are
                  read through cold_storage (index-filtered, streamed, no
                  extraction): hot+cold same-day JSONL is deduped by
                  event_id/content and cold hits carry "tar!member#L"
                  locators.
  3. cutoff     — `as_of` (defaulting to `now` when given) excludes events
                  after the asking turn; undated records derived to `today`
                  (the as-of day or scan day) are dropped to avoid leakage.
  4. scoring    — BM25 where CJK bigrams / latin words are "strong" terms
                  and single CJK chars only fill; minute-sensitive half-life
                  recency for temporal intent; staged cwd scope with an
                  explicit cross-project fallback; artifact extraction
                  (markdown links + files, resolved against record cwd,
                  URLs and provenance paths never treated as artifacts);
                  delivered-vs-planned classification is language-inferred
                  only — filesystem existence/status is never claimed.

Backward compatible: search() keeps its old signature and result keys
(_source/_path/_score); new keyword-only args and per-hit metadata are all
additive.
"""
from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
from collections import Counter
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

try:
    from retrieval_intent import parse_intent as _parse_intent
except ImportError:  # scripts copied without the companion module
    _parse_intent = None

try:
    import cold_storage as _cold
except ImportError:  # cold archive reader not shipped alongside
    _cold = None


CJK_RE = re.compile(r"[\u4e00-\u9fff]")
SEGMENT_RE = re.compile(r"[A-Za-z0-9_./:-]+|[\u4e00-\u9fff]+")
SOURCE_WEIGHT = {"memory": 1.6, "evidence": 1.35, "report": 0.65, "session": 0.7, "profile": 1.0}

SYNONYMS = {
    "日记": ["diary", "daily"], "diary": ["日记", "daily"],
    "报告": ["report"], "report": ["报告"],
    "记忆": ["memory", "memories"], "memory": ["记忆"],
    "技能": ["skill", "skills"], "skill": ["技能"],
    "昨天": ["yesterday"], "yesterday": ["昨天"],
    "检索": ["search", "retrieval"], "search": ["检索"],
    "会话": ["session"], "session": ["会话"],
}

# Scoring weights. Kept as named constants so the tradeoffs are visible:
# strong-term coverage gates topicality; the rest are additive bonuses that
# can override weak lexical scores only when intent calls for it.
BM25_K1 = 1.2
BM25_B = 0.75
WEAK_TOKEN_FACTOR = 0.25        # single CJK chars count, but discounted
COVERAGE_FLOOR = 0.5            # lexical *= (FLOOR + (1-FLOOR) * strong coverage)
PHRASE_BONUS = 2.5
RECENCY_BASE_MAX = 0.5          # legacy day-level recency when no temporal intent
RECENCY_TEMPORAL_MAX = 8.0      # minute-sensitive recency for temporal intent
BASE_HALF_LIFE_DAYS = 30.0
SCOPE_SAME_CWD_BONUS = 3.0
OTHER_SESSION_BONUS = 1.5       # "另一个agent/另一个会话" intent
AGENT_MATCH_BONUS = 1.0
DELIVERED_BONUS = 7.0           # artifact intent + delivered status (language-inferred)
ARTIFACT_BONUS = 2.0            # record names a non-source-ref artifact
PLANNED_PENALTY = 0.7           # multiplicative damp when intent wants the artifact
TOPICAL_COVERAGE = 0.5          # delivery/artifact bonuses need >= half of strong terms
ECHO_FACTOR = 0.2               # multiplicative damp for verbatim question echoes
MAX_ARTIFACTS = 12


def tokenize(text: str) -> list[str]:
    """English tokens (lower, plural stripped) + CJK unigrams and bigrams."""
    tokens: list[str] = []
    for seg in SEGMENT_RE.findall(text or ""):
        if CJK_RE.match(seg):
            tokens.extend(seg)
            tokens.extend(seg[i:i + 2] for i in range(len(seg) - 1))
        else:
            w = seg.lower().strip("._:/-")
            if len(w) < 2:
                continue
            tokens.append(w)
            if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
                tokens.append(w[:-1])
    return tokens


def expand_synonyms(tokens):
    out = list(tokens)
    for t in tokens:
        for word, syns in SYNONYMS.items():
            if t in tokenize(word):
                out.extend(syns)
    return out


def extract_filters(query):
    filters = {}
    rest = query
    for key in ("agent", "date", "source"):
        m = re.search(key + r":([\w.-]+)", rest)
        if m:
            filters[key] = m.group(1)
            rest = rest.replace(m.group(0), " ")
    return filters, rest.strip()


def _iter_jsonl(path: Path) -> Iterable[tuple[dict[str, Any], int]]:
    """Yield (record, 1-based line number); reading is non-mutating."""
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value, line_no


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    """Backward-compatible wrapper: records only, no line numbers."""
    for record, _line_no in _iter_jsonl(path):
        yield record


def _date_from(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _coerce_datetime(value: Any) -> datetime | None:
    """Parse an instant; naive values are assumed to be local time."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, time.min)
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                dt = datetime.combine(date.fromisoformat(value.strip()[:10]), time.min)
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return dt


def _doc_instant(record: dict[str, Any]) -> datetime | None:
    """Most precise timestamp on a record, or None when undated."""
    for field in ("ts", "updated_at", "created_at", "ended_at", "started_at"):
        dt = _coerce_datetime(record.get(field))
        if dt:
            return dt
    return None


TOOL_ECHO_RE = re.compile(
    r"^\[(tool_output|tool_result)\]\s*(\[|Script completed|Chunk ID|Process exited|Output:)",
    re.I,
)


def _record_text(record: dict[str, Any]) -> str:
    fields = (
        "title", "content", "narrative", "text", "cwd", "agent", "model",
        "status", "goals", "actions", "results", "next_steps", "files", "tools",
    )
    parts: list[str] = []
    for field in fields:
        value = record.get(field)
        if isinstance(value, list):
            parts.extend(str(item) for item in value
                         if not TOOL_ECHO_RE.match(str(item)))
        elif value is not None:
            v = str(value)
            if not TOOL_ECHO_RE.match(v):
                parts.append(v)
    return " ".join(parts)


def _make_doc(source: str, path: Path, record: dict[str, Any], text: str,
              line_no: int | None = None) -> dict[str, Any]:
    return {"source": source, "path": str(path), "record": record,
            "text": text, "line": line_no}


def iter_documents(hub_home: Path, days: int = 30,
                   ref_date: date | None = None, *, exact_day: str | None = None,
                   include_global: bool = True) -> list[dict[str, Any]]:
    """Load the scan window. `ref_date` anchors "today" for historical replays."""
    docs: list[dict[str, Any]] = []
    memory_path = hub_home / "memory" / "memories.jsonl"
    for record, line_no in (_iter_jsonl(memory_path) if include_global else []):
        if record.get("status") in {"superseded", "stale", "archived"}:
            continue
        docs.append(_make_doc("memory", memory_path, record,
                              _record_text(record), line_no))

    daily_root = hub_home / "daily"
    cutoff = (ref_date or date.today()) - timedelta(days=max(0, days - 1))
    # Day set = hot daily/ dirs ∪ archived days (cold). Index-listed cold
    # days cost no decompression; member content streams only when a day
    # inside the window lives solely in an archive.
    if _cold is not None:
        all_days = _cold.iter_days(hub_home)
    else:
        all_days = [p.name for p in daily_root.glob("20??-??-??")]
    for day_name in sorted(all_days, reverse=True):
        if exact_day is not None and day_name != exact_day:
            continue
        try:
            day = date.fromisoformat(day_name)
        except ValueError:
            continue
        if day < cutoff:
            continue
        daily = daily_root / day_name
        is_hot = daily.is_dir()
        is_cold = (not is_hot) and _cold is not None
        if not is_hot and not is_cold:
            continue

        if is_hot:
            # Hot JSONL pass (unchanged semantics, keeps live locators).
            evidence_path = daily / "evidence.jsonl"
            for record, line_no in _iter_jsonl(evidence_path):
                record.setdefault("period", day_name)
                docs.append(_make_doc("evidence", evidence_path, record,
                                      _record_text(record), line_no))
            sessions_path = daily / "sessions.jsonl"
            for record, line_no in _iter_jsonl(sessions_path):
                docs.append(_make_doc("session", sessions_path, record,
                                      _record_text(record), line_no))
            md_texts = {}
            for name in ("diary.md", "conversations.md"):
                path = daily / name
                if path.exists():
                    text = path.read_text(encoding="utf-8", errors="replace")
                    md_texts[name] = text
                    docs.append(_make_doc(
                        "report", path, {"period": day_name, "title": name},
                        text))
        else:
            md_texts = {}

        if _cold is not None and _cold.has_day(hub_home, day_name):
            # Overlay-aware cold merge: shared JSONL events keep the hot
            # locator (dup rows are skipped here — they remain visible via
            # cold_storage.iter_day_records for tooling that wants them);
            # cold-only events keep their tar!member#L locators.
            for filename, source in (("evidence.jsonl", "evidence"),
                                     ("sessions.jsonl", "session")):
                for row in _cold.iter_day_records(hub_home, day_name, filename):
                    if row["source"] != "cold" or row["dup"]:
                        continue          # hot already emitted / overlay twin
                    record = row["record"]
                    record.setdefault("period", day_name)
                    ref, _, line = row["locator"].rpartition("#L")
                    doc = _make_doc(
                        source, ref, record, _record_text(record),
                        int(line) if line else None)
                    if row.get("conflict_of"):
                        # same event_id, divergent content vs the hot copy —
                        # surfaced, never silently merged
                        doc["conflict_of"] = row["conflict_of"]
                    docs.append(doc)
            for name in ("diary.md", "conversations.md"):
                cold_text = _cold.read_archive_text(hub_home, day_name, name)
                if cold_text is None:
                    continue
                if is_hot and cold_text == md_texts.get(name):
                    continue  # identical twin: the live file already speaks
                ref = _cold.archive_member_locator(hub_home, day_name, name)
                if ref:
                    docs.append(_make_doc(
                        "report", ref,
                        {"period": day_name, "title": name,
                         "cold_overlay": is_hot}, cold_text))

    for path in (hub_home / "profile" / "constraints.md", hub_home / "profile" / "preferences.md"):
        if include_global and path.exists():
            docs.append(_make_doc("profile", path, {"title": path.name},
                                  path.read_text(encoding="utf-8", errors="replace")))
    return docs


def _metadata_match(record: dict[str, Any], cwd: str | None, agent: str | None) -> float:
    score = 0.0
    if cwd and record.get("cwd") == cwd:
        score += 1.5
    if agent and record.get("agent") == agent:
        score += 1.0
    return score


def _doc_date(record):
    d = record.get("period") or _date_from(record.get("ts") or record.get("created_at"))
    return d.isoformat() if hasattr(d, "isoformat") else (d or "")


def _doc_day(record: dict[str, Any]) -> date | None:
    """The day a record belongs to, incl. undated records in a dated folder."""
    instant = _doc_instant(record)
    if instant:
        return instant.date()
    d = record.get("period") or _date_from(record.get("ts") or record.get("created_at"))
    if hasattr(d, "isoformat"):
        return date.fromisoformat(d.isoformat()[:10])
    return _date_from(str(d)) if d else None


def _within_cutoff(doc: dict[str, Any], cutoff: datetime | None,
                   ref_date: date) -> bool:
    """Exclude post-cutoff events; undated records derived to 'today' are
    dropped too (their true position in time is unknown — leakage risk)."""
    if cutoff is None:
        return True
    instant = _doc_instant(doc["record"])
    if instant:
        return instant < cutoff  # exclusive: the asking turn itself is out
    day = _doc_day(doc["record"])
    if day is None:
        return True  # profiles and other timeless docs stay
    return day < cutoff.date()


# ---------------------------------------------------------------------------
# Artifacts: markdown links and record 'files' entries naming real outputs.
# Never http(s) (may carry tokens), never source/provenance pointers, never
# tool internals — artifact paths are resolved against the record's cwd but
# not verified, so callers must not read them as "file exists".
# ---------------------------------------------------------------------------

_MD_LINK_RE = re.compile(r"\[([^\]]{0,160})\]\((<[^>\n]+>|[^)\s]+)")
_BACKTICK_PATH_RE = re.compile(r"`([^`\n]{2,160})`")
_URL_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_SECRET_HINT_RE = re.compile(
    r"(?i)(token|secret|api[_-]?key|passwd|password|credential|cookie"
    r"|authorization|sig=|key=)"
)
_SECRET_BASENAME_RE = re.compile(
    r"(?:auth\.json|\.?env(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?"
    r"|\.?(?:netrc|npmrc|pypirc)|.+\.(?:pem|key|p12|pfx|keystore))$", re.I,
)
_SOURCE_REF_HINTS = ("/sessions/", "/.dsh/", "/.claude/", "/.codex/", "/live/")
# File extensions that mark an actual deliverable; config/data/tool outputs.
_FILE_EXT_RE = re.compile(
    r"\.(?:md|markdown|txt|rst|pdf|html?|css|jsx?|tsx?|ts|mjs|cjs|py|sh|rb|go|rs|"
    r"java|kt|swift|c|h|cpp|hpp|cs|php|pl|r|sql|json|ya?ml|toml|ini|cfg|xml|csv|"
    r"tsv|xlsx?|docx|pptx|png|jpe?g|gif|webp|svg|mp4|mov|wav|mp3|zip|tar|gz|"
    r"sqlite3?|db|log|patch|diff|canvas|base|ipynb)$",
    re.I,
)

_DELIVERY_MARKERS_RE = re.compile(
    r"已完成|已写好|写好了|已生成|已创建|已保存|已交付|已写入|已完成并|已落地"
    r"|已更新|已提交|已经(?:写好|生成|创建|完成)|做完了|搞定了"
    r"|\bdone\b|\bcompleted\b|\bcreated\b|\bgenerated\b|\bwritten\b"
    r"|\bfinalized\b|\bdelivered\b|\bupdated\b|\bsaved\b",
    re.I,
)
_PLANNED_MARKERS_RE = re.compile(
    r"将会|我(?:会|将|先)|打算|计划|准备|下一步|接下来|我会|TODO|todo"
    r"|\bwill\b|\bgoing\s+to\b|\bplan(?:ned|ning)?\b|\bnext\b",
    re.I,
)


def _artifact_entry(raw_path: str, label: str, via: str,
                    cwd: str | None, hub_home: Path) -> dict[str, Any] | None:
    path = raw_path.strip().strip("<>")
    if not path or _URL_RE.match(path):
        return None  # URLs are never artifacts (may carry tokens/secrets)
    if path.startswith("#") or _SECRET_HINT_RE.search(path):
        return None
    path = path.split("#", 1)[0].split("?", 1)[0]
    if _SECRET_BASENAME_RE.fullmatch(path.replace("\\", "/").rsplit("/", 1)[-1]):
        return None  # credential paths are not user-facing deliverables
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        if not cwd:
            resolved = path  # nothing to resolve against; keep raw
        else:
            resolved = str(Path(cwd).expanduser() / resolved)
    else:
        resolved = str(resolved)
    source_ref = (
        resolved.startswith(str(hub_home))
        or any(hint in resolved for hint in _SOURCE_REF_HINTS)
    )
    return {
        "path": path,
        "resolved": resolved,
        "label": label.strip()[:160],
        "via": via,
        "source_ref": source_ref,
    }


def _files_entries(value: Any) -> list[str]:
    """Parse the 'files' field: real list, stringified list, or bare string."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        try:
            parsed = ast.literal_eval(value)
            if isinstance(parsed, (list, tuple)):
                return [str(v) for v in parsed]
        except (ValueError, SyntaxError):
            pass
        return [value]
    return []


def extract_artifacts(record: dict[str, Any], hub_home: Path) -> list[dict[str, Any]]:
    """Artifact candidates: markdown links to files + explicit files field."""
    cwd = record.get("cwd")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(raw: str, label: str, via: str) -> None:
        cleaned = raw.strip().strip("<>")
        if not _FILE_EXT_RE.search(cleaned.split("?")[0].split("#")[0]):
            return
        entry = _artifact_entry(cleaned, label, via, cwd, hub_home)
        if entry and entry["resolved"] not in seen:
            seen.add(entry["resolved"])
            if len(out) < MAX_ARTIFACTS:
                out.append(entry)

    text = record.get("text") or ""
    if isinstance(text, str):
        for m in _MD_LINK_RE.finditer(text):
            _add(m.group(2), m.group(1), "markdown_link")
        for m in _BACKTICK_PATH_RE.finditer(text):
            # `path` must look file-like (has / or .) — code spans with
            # identifiers like `foo` or `arg_name` are rejected by the
            # extension check anyway, this just keeps noise down.
            if "/" in m.group(1) or "." in m.group(1):
                _add(m.group(1), "", "backtick_path")
    for field in ("files", "artifacts", "deliverables"):
        for raw in _files_entries(record.get(field)):
            _add(raw, "", "files_field")
    return out


def _names_artifact(record: dict[str, Any], artifacts: list[dict[str, Any]]) -> bool:
    """True when the record points at a deliverable: a markdown link or
    backtick path (written by the agent itself) or a small curated files
    list. Status-only 'completed' records and bulk files dumps do not
    qualify — they describe work, not a named artifact."""
    if any(not a["source_ref"] and a["via"] in ("markdown_link", "backtick_path")
           for a in artifacts):
        return True
    return any(
        not a["source_ref"] and a["via"] == "files_field"
        for a in artifacts[:3]
    ) and len(artifacts) <= 3


def _classify_delivery(record: dict[str, Any]) -> str:
    """Language-inferred delivery state: delivered | planned | mentioned.

    Status fields count only when the record looks like a work summary
    (evidence); 'active' memories are facts, not finished work. Markers in
    the text take precedence, delivered over planned.
    """
    status = str(record.get("status") or "").lower()
    # Scan text/results only: next_steps is planned-by-definition and would
    # flip every record to 'planned'.
    text = " ".join(
        str(record.get(f) or "") for f in ("text", "results")
    )
    # Negated markers (not completed / 尚未完成) must not count as delivery.
    positive_text = re.sub(
        r"(?:尚未|还没|并未|未曾|未|没有)(?:完成|生成|创建|交付|保存|写好)"
        r"|\b(?:not|never|isn't|wasn't)\s+(?:yet\s+)?(?:completed|done|created|generated|delivered|saved)\b",
        "", text, flags=re.I)
    if status in {"completed", "done", "success", "succeeded"} or _DELIVERY_MARKERS_RE.search(positive_text):
        return "delivered"
    if _PLANNED_MARKERS_RE.search(text):
        return "planned"
    if record.get("role") in {"user", "system"}:
        return "mentioned"
    return "unknown"


def _is_echo(doc: dict[str, Any], clean_query: str) -> bool:
    """The asking turn itself (a user msg equal to the query) is an echo,
    not evidence — demote it below the documents it refers to."""
    record = doc["record"]
    if record.get("role") not in {"user", "system"}:
        return False
    if doc["source"] not in {"session", "evidence"}:
        return False
    normalized = " ".join(str(record.get("text") or "").split())
    clean = " ".join(str(clean_query or "").split())
    return bool(normalized) and bool(clean) and normalized == clean


def _recency(doc: dict[str, Any], temporal: dict | None,
             ref_now: datetime, ref_date: date) -> tuple[float, float | None]:
    """Return (score, age_minutes). Temporal intent uses a per-kind half-life;
    day-offset/explicit-date kinds measure symmetric distance from noon of
    the target day. Without temporal intent keep the legacy day decay."""
    instant = _doc_instant(doc["record"])
    if temporal:
        half_life = max(1.0, float(temporal.get("half_life_minutes") or 1440.0))
        if instant:
            if temporal.get("symmetric"):
                target = date.fromisoformat(temporal["target_date"])
                anchor = datetime.combine(target, time(12, 0), tzinfo=instant.tzinfo)
                age_minutes = abs((instant - anchor).total_seconds()) / 60.0
            else:
                age_minutes = max(0.0, (ref_now - instant).total_seconds() / 60.0)
            return RECENCY_TEMPORAL_MAX * 0.5 ** (age_minutes / half_life), age_minutes
        # undated doc under temporal intent: only its day can speak
        day = _doc_day(doc["record"])
        if day is None:
            return 0.0, None
        target_day = date.fromisoformat(temporal["target_date"]) \
            if temporal.get("symmetric") else ref_now.date()
        age_minutes = abs((day - target_day).days) * 1440.0
        return RECENCY_TEMPORAL_MAX * 0.5 ** (age_minutes / half_life), age_minutes
    day = _doc_day(doc["record"])
    if day is None:
        return 0.0, None
    age_days = max(0, (ref_date - day).days)
    if instant:
        age_days = max(0, (ref_now - instant).total_seconds() / 86400.0)
    return RECENCY_BASE_MAX * max(0.0, 1.0 - age_days / BASE_HALF_LIFE_DAYS), age_days * 1440.0


def search(hub_home: Path, query: str, *, days: int = 30, limit: int = 20,
           cwd: str | None = None, agent: str | None = None,
           now: datetime | str | None = None,
           as_of: datetime | str | None = None,
           exclude_session: str | None = None,
           use_index: bool | None = None) -> list[dict[str, Any]]:
    """Hybrid retrieval over hubmemory.

    Backward compatible: positional (hub_home, query) + days/limit/cwd/agent.
    New keyword-only args:
      now             reference instant for recency + 'today' anchoring
      as_of           historical cutoff — events at/after it are excluded;
                      defaults to `now` when only now is given; a bare date
                      means end of that day
      exclude_session drop an exact full session id — e.g. the asking
                      session, so the question echo cannot win

    Every hit keeps _source/_path/_score and adds:
      _source_line  1-based line in the jsonl file (None for whole-file docs)
      artifacts     extracted artifact dicts (only when non-empty)
      _retrieval    structured provenance + score decomposition
    """
    hub_home = Path(hub_home)
    cutoff = _coerce_datetime(as_of) if as_of is not None else None
    if as_of is not None and cutoff is None:
        raise ValueError("invalid --as-of timestamp: %r" % as_of)
    if isinstance(as_of, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of.strip()):
        cutoff = datetime.combine(cutoff.date(), time.max, tzinfo=cutoff.tzinfo)
    ref_now = _coerce_datetime(now) if now is not None else cutoff
    if now is not None and ref_now is None:
        raise ValueError("invalid now timestamp: %r" % now)
    ref_now = ref_now or datetime.now().astimezone()
    ref_date = ref_now.date()
    if cutoff is None and now is not None:
        cutoff = ref_now

    filters, remainder = extract_filters(query)
    if agent:
        filters.setdefault("agent", agent)
    intent = _parse_intent(remainder, now=ref_now) if _parse_intent else {
        "raw": query, "clean": remainder, "removed": [],
        "temporal": None, "other_agent": False, "artifact_request": False,
    }
    clean_query = intent["clean"]
    if not clean_query:
        return []  # pure deixis/time has no topic: consult live or clarify

    query_tokens = expand_synonyms(tokenize(clean_query))
    # Strong terms carry the topic: CJK bigrams and latin/technical words.
    # Lone CJK chars are too generic to gate on (你/看/了 pollute everything)
    # so they only contribute a discounted fill score.
    strong_terms = {
        t for t in query_tokens
        if len(t) >= 2 or not CJK_RE.match(t)
    }
    weak_terms = [t for t in query_tokens if t not in strong_terms]

    enabled = use_index if use_index is not None else os.environ.get(
        "HUBMEMORY_RETRIEVAL_INDEX", "1").lower() not in {"0", "false", "off"}
    index_stats = {"mode": "scan"}
    if enabled:
        from retrieval_index import load_documents, make_hybrid_loader
        try:
            docs, _, stats = load_documents(hub_home, days, ref_date,
                loader=make_hybrid_loader(hub_home), tokenizer=tokenize)
            index_stats = {k: v for k, v in stats.items() if k in {
                "docs", "hit_count", "miss_count", "elapsed_ms", "fallback"}}
            index_stats["mode"] = "scan_fallback" if stats.get("fallback") else "incremental_tokens"
        except ValueError:
            # Preserve the scan API: cold reader quarantines corrupt members
            # and records integrity warnings. NEVER use cached stale content.
            docs = iter_documents(hub_home, days, ref_date=ref_date)
            index_stats = {"mode": "scan_fallback", "fallback": True,
                           "reason": "index_or_archive_validation"}
    else:
        docs = iter_documents(hub_home, days, ref_date=ref_date)
    docs = [d for d in docs if _within_cutoff(d, cutoff, ref_date)]
    from decision_state import annotate_documents
    docs = annotate_documents(docs, hub_home, as_of=cutoff)
    if exclude_session:
        # Exact IDs only: 'session-' is a common prefix and must not silently
        # erase all DSH sessions under an allegedly unique-prefix API.
        docs = [d for d in docs if d["record"].get("session_id") != exclude_session]
    if filters.get("agent"):
        docs = [d for d in docs if d["record"].get("agent") == filters["agent"]]
    if filters.get("source"):
        docs = [d for d in docs if d["source"] == filters["source"]]
    if filters.get("date"):
        docs = [d for d in docs if _doc_date(d["record"]) == filters["date"]]
    if not docs:
        return []

    document_tokens = [Counter(doc["_index_tokens"]) if "_index_tokens" in doc
                       else Counter(tokenize(doc["text"])) for doc in docs]
    document_lengths = [sum(tokens.values()) for tokens in document_tokens]
    average_length = max(1.0, sum(document_lengths) / max(1, len(document_lengths)))
    document_frequency = Counter()
    for tokens in document_tokens:
        document_frequency.update(tokens.keys())

    phrase_text = clean_query.lower()
    ranked: list[dict[str, Any]] = []
    for doc, tokens, document_length in zip(docs, document_tokens, document_lengths):
        strong_lex = 0.0
        weak_lex = 0.0
        strong_hit = 0
        strong_seen: set[str] = set()
        for token in query_tokens:
            count = tokens.get(token, 0)
            if not count:
                continue
            idf = math.log((1 + len(docs)) / (1 + document_frequency[token])) + 1
            denominator = count + BM25_K1 * (1 - BM25_B + BM25_B * document_length / average_length)
            value = idf * ((count * (BM25_K1 + 1)) / denominator)
            if token in strong_terms:
                strong_lex += value
                if token not in strong_seen:
                    strong_seen.add(token)
                    strong_hit += 1
            else:
                weak_lex += value
        coverage = strong_hit / len(strong_terms) if strong_terms else 0.0
        lexical = (strong_lex + WEAK_TOKEN_FACTOR * weak_lex)
        if strong_terms:
            lexical *= COVERAGE_FLOOR + (1.0 - COVERAGE_FLOOR) * coverage
        phrase_bonus = PHRASE_BONUS if phrase_text and phrase_text in doc["text"].lower() else 0.0
        # Topicality gate: with a topic query, a doc must hit a strong term or
        # the cleaned phrase; weak unigram-only hits are filler collisions.
        # With no strong terms (pure temporal/actor query) every doc passes.
        if strong_terms and not (strong_lex > 0 or phrase_bonus):
            continue

        recency, age_minutes = _recency(doc, intent.get("temporal"), ref_now, ref_date)

        record = doc["record"]
        scope = "cwd" if (cwd and record.get("cwd") == cwd) else (
            "cross_project" if cwd else "any")
        scope_bonus = SCOPE_SAME_CWD_BONUS if scope == "cwd" else 0.0
        agent_bonus = AGENT_MATCH_BONUS if (agent and record.get("agent") == agent) else 0.0
        other_session_bonus = (
            OTHER_SESSION_BONUS
            if intent.get("other_agent") and exclude_session
            and record.get("session_id") and record.get("session_id") != exclude_session
            else 0.0
        )
        artifacts = extract_artifacts(record, hub_home)
        for a in artifacts:
            a["delivery"] = "see_record"
        delivery = _classify_delivery(record)
        # Delivery semantics: a user searching for a thing wants its delivered
        # state, not the plan to produce it. The full bonus goes to records
        # that actually NAME a deliverable (link / backtick path / curated
        # files list). A query asking for a produced thing cannot establish
        # that a broad completed summary delivered it: bulk file inventories
        # must not gain a delivery bonus solely from query intent.
        # Announced plans are damped for every topical hit.
        topical = not strong_terms or coverage >= TOPICAL_COVERAGE or phrase_bonus > 0
        named_artifact = _names_artifact(record, artifacts)
        artifact_bonus = 0.0
        delivery_bonus = 0.0
        planned_factor = 1.0
        if topical:
            if named_artifact:
                artifact_bonus = ARTIFACT_BONUS
            if delivery == "delivered" and named_artifact:
                delivery_bonus = DELIVERED_BONUS
            elif delivery == "planned":
                planned_factor = PLANNED_PENALTY
        echo = _is_echo(doc, intent["raw"])

        source_weight = SOURCE_WEIGHT.get(doc["source"], 0.5)
        score = (
            lexical + phrase_bonus + scope_bonus + agent_bonus
            + other_session_bonus + recency + artifact_bonus + delivery_bonus
        ) * source_weight * planned_factor
        if intent.get("temporal"):
            # Recency changes the weight of the evidence, not just a small
            # additive tie-break; otherwise a year-old exact phrase beats a
            # just-delivered paraphrase under '刚才'. Keep stale fallback visible.
            score *= 0.25 + 0.75 * min(1.0, recency / RECENCY_TEMPORAL_MAX)
        if echo:
            score *= ECHO_FACTOR

        decision = doc.get("decision") or {}
        out = dict(record)
        if decision.get("state") not in {None, "unmarked"} or not decision.get("recommended", True):
            out["_decision"] = decision
        out.update({"_source": doc["source"], "_path": doc["path"],
                    "_score": round(score, 4), "_source_line": doc["line"]})
        if doc.get("conflict_of"):
            out["_conflict_of"] = doc["conflict_of"]
        if artifacts:
            out["artifacts"] = artifacts
        out["_retrieval"] = {
            "index": index_stats,
            "query_clean": clean_query,
            "removed": intent.get("removed") or [],
            "intent": {
                "temporal": intent.get("temporal"),
                "other_agent": intent.get("other_agent"),
                "artifact_request": intent.get("artifact_request"),
            },
            "matched_terms": sorted(strong_seen),
            "strong_coverage": round(coverage, 3),
            "components": {
                "lexical": round(lexical, 4),
                "phrase": phrase_bonus,
                "scope": scope_bonus,
                "agent": agent_bonus,
                "other_session": other_session_bonus,
                "recency": round(recency, 4),
                "artifact": artifact_bonus,
                "delivery": delivery_bonus,
                "source_weight": source_weight,
                "planned_factor": planned_factor,
                "echo_factor": ECHO_FACTOR if echo else 1.0,
            },
            "age_minutes": round(age_minutes, 1) if age_minutes is not None else None,
            "scope": scope,
            "delivery": delivery,
            "delivery_basis": "language-inferred; artifact paths are resolved, not verified",
            "session": record.get("session_id"),
            "source_line": doc["line"],
            "echo": echo,
        }
        ranked.append(out)

    # Dedup: same source file + same leading content = one hit.
    seen_keys = set()
    deduped = []
    for item in ranked:
        body = " ".join(str(
            item.get("title") or item.get("goals") or item.get("text")
            or item.get("content") or item.get("narrative") or ""
        ).split())[:240]
        # Different memory IDs can share a title but represent alternative
        # decisions; never deduplicate away the accepted replacement.
        key = ((item.get("_path"), "memory", item.get("id"))
               if item.get("_source") == "memory" and item.get("id") else (
                   item.get("_path"),
                   hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]))
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped.append(item)
    # Stage meaningful local evidence before global fallback; a one-bigram
    # collision is not enough to hide a substantially better cross-project hit.
    def rank_key(item):
        meta = item["_retrieval"]
        local = (meta["scope"] == "cwd" and meta["strong_coverage"] >= 0.5
                 and not meta["echo"])
        historical = not (item.get("_decision") or {}).get("recommended", True)
        return (1 if historical else 0, 0 if local else 1, -item["_score"],
                str(item.get("period") or item.get("ts") or ""),
                str(item.get("session_id") or ""))
    deduped.sort(key=rank_key)
    results = deduped[: max(1, limit)]

    # Staged scope: same-cwd hits are boosted above, never exclusive. When a
    # cwd was given but no cwd-scoped hit made the top-N, mark the fallback so
    # callers can tell "found in another project" from "found locally".
    if cwd and results and not any(r["_retrieval"]["scope"] == "cwd" for r in results):
        for r in results:
            r["_retrieval"]["cwd_fallback"] = True
    return results
