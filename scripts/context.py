#!/usr/bin/env python3
"""Budgeted hubmemory retrieval with exact provenance and artifact pointers.

The output is evidence, not instructions. A delivered artifact means a source
message reports delivery; it does not prove that the file still exists.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path

from hybrid_query import search

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
RECENT_HINT_RE = re.compile(
    r"昨天|前天|最近|刚才|上次|之前|今天|这周|上周|另一个|其他会话|"
    r"yesterday|recent|earlier|last week|today|another (?:agent|session)", re.I,
)


def estimate_tokens(text: str) -> int:
    # Conservative UTF-8 estimate shared by the JSON and human output contracts.
    return max(1, (len(text.encode("utf-8")) + 3) // 4)


def _pick_line(record: dict) -> str:
    for field in ("conclusion", "title", "text", "content", "narrative"):
        value = record.get(field)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    for field in ("results", "goals", "actions", "next_steps"):
        value = record.get(field)
        if isinstance(value, list) and value:
            return " ".join(str(value[0]).split())
    return "(no summary field)"


def _confidence(record: dict) -> str:
    value = record.get("confidence")
    if isinstance(value, (int, float)):
        return "high" if value >= 0.7 else ("med" if value >= 0.5 else "low")
    return str(value) if value in {"high", "med", "low"} else "unknown"


def compact_hit(record: dict) -> dict:
    full_meta = record.get("_retrieval") or {}
    meta = {k: full_meta[k] for k in (
        "intent", "coverage", "relevance", "scope", "delivery_state", "timestamp", "source_line", "query_clean"
    ) if k in full_meta}
    if "strong_coverage" in full_meta:
        meta.setdefault("coverage", full_meta["strong_coverage"])
    if "delivery" in full_meta:
        meta.setdefault("delivery_state", full_meta["delivery"])
    artifacts = full_meta.get("artifacts", record.get("artifacts", record.get("_artifacts", [])))
    artifacts = [{"path": a.get("resolved") or a.get("path"),
                  "label": a.get("label", "")[:100]}
                 for a in artifacts if isinstance(a, dict) and not a.get("source_ref")][:3]
    line_number = record.get("_source_line") or meta.get("source_line")
    ref = record.get("_path")
    return {
        "kind": record.get("_source"),
        "period": record.get("period") or (record.get("ts") or "")[:10],
        "agent": record.get("agent"),
        "cwd": record.get("cwd"),
        "confidence": _confidence(record),
        "line": _pick_line(record)[:240],
        "score": record.get("_score"),
        "ref": ref,
        "source_line": line_number,
        "locator": ("%s#L%s" % (ref, line_number)) if ref and line_number else ref,
        "session": record.get("session_id") or "",
        "event_id": record.get("event_id"),
        "timestamp": record.get("ts") or meta.get("timestamp"),
        "retrieval": meta,
        "artifacts": artifacts,
    }


def routing_hint(query: str) -> str | None:
    if RECENT_HINT_RE.search(query or ""):
        return ("cross-session reference: search hubmemory FIRST; use exact source/artifact "
                "pointers, then read the actual file. Do not substitute Git discovery for memory. "
                "No hit is not proof of no work; consult live snapshots or ask for clarification.")
    return None


def assess_sufficiency(hits: list[dict]) -> tuple[str, str]:
    if not hits:
        return "thin", "no matching evidence; check live sessions or clarify the subject"
    meta = hits[0].get("retrieval") or {}
    coverage = meta.get("coverage", meta.get("relevance", 0))
    if meta.get("delivery_state") == "planned":
        return "thin", "source describes a plan, not a completed deliverable"
    intent = meta.get("intent") or {}
    if isinstance(intent, dict) and intent.get("artifact_request") and not hits[0].get("artifacts"):
        return "thin", "matched discussion but no exact artifact pointer; consult source/live snapshot"
    named_delivery = meta.get("delivery_state") == "delivered" and bool(hits[0].get("artifacts"))
    threshold = 0.5 if named_delivery else 0.65
    if isinstance(coverage, (int, float)) and coverage >= threshold:
        return "sufficient", "topic-matched evidence; verify source claims and read the artifact before review"
    return "thin", "weak or ambiguous topic match; raw ranking scores are not confidence"


def render(payload: dict) -> str:
    lines = []
    if payload.get("hint"):
        lines.append("hint: " + payload["hint"])
    for warning in payload.get("warnings", []):
        lines.append("warning: " + warning)
    if payload.get("pending_reports"):
        lines.append("pending reports: " + ", ".join(
            "%s %s[%s]" % (p["kind"], p["period"], p["status"])
            for p in payload["pending_reports"]))
    if payload["results"]:
        first = payload["results"][0]["retrieval"]
        lines.append("topic: %s | scope: %s" % (first.get("query_clean", "-"), first.get("scope", "-")))
    lines.append("sufficiency: %s — %s" % (payload["sufficiency"], payload["sufficiency_reason"]))
    lines.append("== relevant memory (%d shown, %d dropped) ==" % (
        len(payload["results"]), payload["dropped_by_budget"]))
    for hit in payload["results"]:
        meta = hit["retrieval"]
        lines.append("- [%s|%s|%s] %s" % (
            str(hit["kind"] or "unknown") + ("/cold" if ".tar.gz!" in str(hit.get("locator", "")) else ""), hit["timestamp"] or hit["period"] or "-",
            meta.get("delivery_state", "evidence"), hit["line"]))
        lines.append("    ref: %s" % hit["locator"])
        lines.append("    session: %s | event: %s" % (hit["session"] or "-", hit["event_id"] or "-"))
        if hit.get("cwd"):
            lines.append("    cwd: %s" % hit["cwd"])
        for artifact in hit["artifacts"][:3]:
            path = artifact.get("path") if isinstance(artifact, dict) else artifact
            if path:
                lines.append("    artifact (source-reported, not file-verified): %s" % path)
    return "\n".join(lines)


def build_payload(query: str, raw: list[dict], budget: int = 1200,
                  pending: list[dict] | None = None, json_output: bool = False) -> dict:
    hits = [compact_hit(r) for r in raw]
    payload = {
        "query": query, "hint": routing_hint(query), "budget_tokens": budget,
        "returned_tokens_est": 0, "results": [], "dropped_by_budget": len(hits),
        "sufficiency": "thin", "sufficiency_reason": "no matching evidence",
        "pending_reports": pending or [],
    }
    encode = (lambda p: json.dumps(p, ensure_ascii=False, indent=2)) if json_output else render
    # Never force an oversized first result into the response. Pointers and IDs
    # are indivisible: drop a hit rather than silently truncate its provenance.
    for hit in hits:
        trial = dict(payload, results=payload["results"] + [hit])
        trial["dropped_by_budget"] = len(hits) - len(trial["results"])
        trial["sufficiency"], trial["sufficiency_reason"] = assess_sufficiency(trial["results"])
        trial["returned_tokens_est"] = budget
        if estimate_tokens(encode(trial)) <= budget:
            payload = trial
    payload["sufficiency"], payload["sufficiency_reason"] = assess_sufficiency(payload["results"])
    if hits and not payload["results"]:
        payload["sufficiency_reason"] = "budget too small for exact evidence pointers; increase --budget"
    # Bound tiny budgets too: header-only fallback is explicit, never false sufficient.
    if estimate_tokens(encode(payload)) > budget:
        payload["hint"] = None
        payload["pending_reports"] = []
        payload["sufficiency_reason"] = "increase --budget for evidence"
        payload["query"] = ""
    for _ in range(3):
        payload["returned_tokens_est"] = estimate_tokens(encode(payload))
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default="")
    parser.add_argument("--cwd", default=os.getcwd())
    parser.add_argument("--agent")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=8)
    parser.add_argument("--budget", type=int, default=1200)
    parser.add_argument("--as-of", help="ISO timestamp: reproducible pre-question retrieval")
    parser.add_argument("--exclude-session", help="exclude this session to avoid self-echo")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.budget < 160:
        parser.error("--budget must be at least 160 (exact provenance needs space)")
    if args.days < 1 or args.limit < 1:
        parser.error("--days and --limit must be positive")
    pending = []
    for path in sorted((HUB_HOME / "reports" / "requests").glob("*.json")):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("status") != "done":
            pending.append({k: item.get(k) for k in ("kind", "period", "status")})
    kwargs = dict(days=args.days, limit=args.limit, cwd=args.cwd, agent=args.agent)
    if args.as_of:
        kwargs["as_of"] = args.as_of
    if args.exclude_session:
        kwargs["exclude_session"] = args.exclude_session
    try:
        raw = search(HUB_HOME, args.query, **kwargs) if args.query else []
    except ValueError as exc:
        parser.error(str(exc))
    from cold_storage import last_issues
    warnings = (["Cold archive read/integrity failure; results may be incomplete. Run hub archive verify YYYY-MM."]
                if last_issues() else [])
    payload = build_payload(args.query, raw, args.budget, pending, args.json)
    if warnings:
        # Reserve room for integrity warnings without violating output budget.
        reserve = estimate_tokens(" ".join(warnings)) + 32
        payload = build_payload(args.query, raw, max(80, args.budget - reserve), pending, args.json)
        payload["budget_tokens"] = args.budget
        payload["warnings"] = warnings
        encoder = (lambda p: json.dumps(p, ensure_ascii=False, indent=2)) if args.json else render
        if estimate_tokens(encoder(payload)) > args.budget:
            payload["results"] = []
            payload["pending_reports"] = []
            payload["hint"] = None
            payload["query"] = ""
            payload["dropped_by_budget"] = len(raw)
            payload["sufficiency"] = "thin"
            payload["sufficiency_reason"] = "cold archive unavailable"
        for _ in range(3):
            payload["returned_tokens_est"] = estimate_tokens(encoder(payload))
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else render(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
