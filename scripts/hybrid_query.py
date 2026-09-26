#!/usr/bin/env python3
"""Dependency-free hybrid retrieval for hubmemory.

The default combines lexical relevance with source quality, metadata matches,
and recency. It is intentionally deterministic; embeddings can be added later
without changing the record format or the CLI contract.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable


TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{2,}|[\u4e00-\u9fff]")
SOURCE_WEIGHT = {"memory": 1.6, "evidence": 1.35, "report": 0.65, "session": 0.7, "profile": 1.0}


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text or "")]


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


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


def _record_text(record: dict[str, Any]) -> str:
    fields = (
        "title", "content", "narrative", "text", "cwd", "agent", "model",
        "status", "goals", "actions", "results", "next_steps", "files", "tools",
    )
    parts: list[str] = []
    for field in fields:
        value = record.get(field)
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif value is not None:
            parts.append(str(value))
    return " ".join(parts)


def _make_doc(source: str, path: Path, record: dict[str, Any], text: str) -> dict[str, Any]:
    return {"source": source, "path": str(path), "record": record, "text": text}


def iter_documents(hub_home: Path, days: int = 30) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    memory_path = hub_home / "memory" / "memories.jsonl"
    for record in _read_jsonl(memory_path):
        if record.get("status") in {"superseded", "stale", "archived"}:
            continue
        docs.append(_make_doc("memory", memory_path, record, _record_text(record)))

    daily_root = hub_home / "daily"
    cutoff = date.today() - timedelta(days=max(0, days - 1))
    for daily in sorted(daily_root.glob("20??-??-??"), reverse=True):
        try:
            day = date.fromisoformat(daily.name)
        except ValueError:
            continue
        if day < cutoff:
            continue
        evidence_path = daily / "evidence.jsonl"
        for record in _read_jsonl(evidence_path):
            record.setdefault("period", daily.name)
            docs.append(_make_doc("evidence", evidence_path, record, _record_text(record)))
        for name in ("diary.md", "conversations.md"):
            path = daily / name
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                docs.append(_make_doc("report", path, {"period": daily.name, "title": name}, text))
        sessions_path = daily / "sessions.jsonl"
        for record in _read_jsonl(sessions_path):
            docs.append(_make_doc("session", sessions_path, record, _record_text(record)))

    for path in (hub_home / "profile" / "constraints.md", hub_home / "profile" / "preferences.md"):
        if path.exists():
            docs.append(_make_doc("profile", path, {"title": path.name}, path.read_text(encoding="utf-8", errors="replace")))
    return docs


def _metadata_match(record: dict[str, Any], cwd: str | None, agent: str | None) -> float:
    score = 0.0
    if cwd and record.get("cwd") == cwd:
        score += 1.5
    if agent and record.get("agent") == agent:
        score += 1.0
    return score


def search(hub_home: Path, query: str, *, days: int = 30, limit: int = 20,
           cwd: str | None = None, agent: str | None = None) -> list[dict[str, Any]]:
    query_tokens = tokenize(query)
    if not query_tokens:
        return []
    docs = iter_documents(hub_home, days)
    document_tokens = [Counter(tokenize(doc["text"])) for doc in docs]
    document_lengths = [sum(tokens.values()) for tokens in document_tokens]
    average_length = max(1.0, sum(document_lengths) / max(1, len(document_lengths)))
    document_frequency = Counter()
    for tokens in document_tokens:
        document_frequency.update(tokens.keys())
    query_lower = query.lower()
    ranked: list[dict[str, Any]] = []
    for doc, tokens, document_length in zip(docs, document_tokens, document_lengths):
        lexical = 0.0
        for token in query_tokens:
            count = tokens.get(token, 0)
            if not count:
                continue
            idf = math.log((1 + len(docs)) / (1 + document_frequency[token])) + 1
            # BM25-style saturation and document-length normalization prevent
            # long conversation archives from winning by repeating a term.
            k1 = 1.2
            b = 0.75
            denominator = count + k1 * (1 - b + b * document_length / average_length)
            lexical += idf * ((count * (k1 + 1)) / denominator)
        if not lexical and query_lower not in doc["text"].lower():
            continue
        phrase_bonus = 2.5 if query_lower in doc["text"].lower() else 0.0
        metadata = _metadata_match(doc["record"], cwd, agent)
        source_weight = SOURCE_WEIGHT.get(doc["source"], 0.5)
        score = (lexical + phrase_bonus + metadata) * source_weight
        record = dict(doc["record"])
        record.update({"_source": doc["source"], "_path": doc["path"], "_score": round(score, 4)})
        ranked.append(record)
    ranked.sort(key=lambda item: (-item["_score"], str(item.get("period") or item.get("ts") or ""), str(item.get("session_id") or "")))
    return ranked[: max(1, limit)]
