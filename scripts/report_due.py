#!/usr/bin/env python3
"""Lease-owned report queue and supported atomic draft/semantic publication API.

Lock order: archive/writer lock (when supplied by cold_storage), then reports.
Never hold reports/.lock while acquiring an archive lock.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import lockutil
from report_contract import available_dates, read_daily_text, session_rows, validate_report

HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
REPORTS_DIR = HUB_HOME / "reports"
REQUESTS_DIR = REPORTS_DIR / "requests"
LOCK_PATH = REPORTS_DIR / ".lock"
CLAIM_TIMEOUT = timedelta(hours=6)


def now():
    return datetime.now().astimezone()


def timestamp():
    return now().isoformat(timespec="seconds")


def validate_period(kind, period):
    if kind not in ("daily", "monthly"):
        raise ValueError("invalid_kind")
    fmt = "%Y-%m-%d" if kind == "daily" else "%Y-%m"
    if datetime.strptime(period, fmt).strftime(fmt) != period:
        raise ValueError("invalid_period")


def request_path(kind, period):
    validate_period(kind, period)
    return REQUESTS_DIR / f"{kind}-{period}.json"


def output_path(kind, period):
    validate_period(kind, period)
    return HUB_HOME / "daily" / period / "diary.md" if kind == "daily" else HUB_HOME / "monthly" / f"{period}.md"


def read_request(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def write_request(path, request):
    atomic_text(path, json.dumps(request, ensure_ascii=False, indent=2) + "\n")


def locked():
    REQUESTS_DIR.mkdir(parents=True, exist_ok=True)
    return lockutil.acquire_lock(LOCK_PATH)


@contextmanager
def writer_guard(kind=None, period=None):
    # Watcher appends take month SH; publication must take month EX as well.
    from contextlib import nullcontext
    from storage_lock import archive_lock, month_lock
    with archive_lock(HUB_HOME):
        with (month_lock(HUB_HOME, period[:7], shared=False) if period else nullcontext()):
            yield


def lease_active(request):
    if request.get("status") != "claimed":
        return False
    try:
        claimed = datetime.fromisoformat(request["claimed_at"])
        return timedelta(0) <= now() - claimed < CLAIM_TIMEOUT
    except (KeyError, TypeError, ValueError):
        return False


def _clear_claim(request):
    for key in ("claimed_at", "claimed_by", "claimed_model", "claimed_session", "claim_token"):
        request.pop(key, None)


def ensure_request(kind, period, source=None):
    """Idempotently enqueue; never reset or renew an existing lease."""
    path = request_path(kind, period)
    handle = locked()
    try:
        request = read_request(path)
        if request:
            return request
        request = {"id": f"{kind}:{period}", "kind": kind, "period": period,
                   "status": "pending", "created_at": timestamp(), "source": source or []}
        write_request(path, request)
        return request
    finally:
        lockutil.release_lock(handle)


def public_request(request):
    """Queue inventory is not a capability delivery channel."""
    return {key: value for key, value in request.items()
            if key not in {"claim_token", "completed_token", "source_fingerprint"}}


def all_requests():
    if not REQUESTS_DIR.exists():
        return []
    return [public_request(r) for p in sorted(REQUESTS_DIR.glob("*.json")) if (r := read_request(p))]


def monthly_dependencies(period):
    missing = []
    for day in available_dates(HUB_HOME):
        if not day.startswith(period + "-") or not session_rows(HUB_HOME, day):
            continue
        req = read_request(request_path("daily", day))
        diary = read_daily_text(HUB_HOME, day, "diary.md")
        if completed_snapshot_error(req):
            missing.append(day)
            continue
        # Explicit migration policy: archived legacy semantic diaries are usable
        # context, not silently relabelled v2/v3. Pending/claimed still block.
        if not (HUB_HOME / "daily" / day / "sessions.jsonl").exists() and req.get("contract_version") not in (2, 3) and req.get("status") not in ("pending", "claimed") and diary.strip():
            from report_contract import DRAFT_MARKERS
            if not any(marker.lower() in diary.splitlines()[0].lower() for marker in DRAFT_MARKERS):
                continue
        if req.get("status") != "done" or not diary.strip():
            missing.append(day)
            continue
        # Unversioned legacy/deterministic done reports were completed before
        # contract v2/v3 existed; a substantive diary stays done — contract
        # drift alone must not block the monthly forever. Integrity is still
        # guarded by completed_snapshot_error above when fingerprint data
        # exists, and the DRAFT_MARKER check below.
        if req.get("contract_version") in (2, 3):
            try:
                validate_report(diary, "daily", day, HUB_HOME)
            except ValueError:
                missing.append(day)
        else:
            from report_contract import DRAFT_MARKERS
            if any(marker.lower() in diary.splitlines()[0].lower() for marker in DRAFT_MARKERS):
                missing.append(day)
    return missing


def source_fingerprint(kind, period):
    days = [period] if kind == "daily" else [d for d in available_dates(HUB_HOME) if d.startswith(period + "-")]
    digest = hashlib.sha256()
    # A hot overlay must never hide unavailable/corrupt archived facts.
    import cold_storage
    archive = cold_storage.archive_status(HUB_HOME, period[:7], verify=True)
    if archive and archive["status"] == "corrupt":
        raise ValueError("source_archive_integrity_failed:" + period[:7])
    for day in days:
        rows = session_rows(HUB_HOME, day)
        if not rows:
            continue
        digest.update(day.encode())
        digest.update(b"\0")
        # Content identity only: moving hot data into tar must not change the
        # snapshot. Physical line locators are independently citation-checked.
        canonical = sorted(json.dumps(row["record"], ensure_ascii=False, sort_keys=True) for row in rows)
        digest.update(json.dumps(canonical, ensure_ascii=False).encode())
        if kind == "monthly":
            digest.update(b"\0diary\0")
            digest.update(read_daily_text(HUB_HOME, day, "diary.md").encode())
    return digest.hexdigest()


def completed_snapshot_error(request):
    if request.get("contract_version") not in (2, 3) or request.get("status") != "done":
        return None
    kind, period = request["kind"], request["period"]
    try:
        current = source_fingerprint(kind, period)
    except ValueError as exc:
        return str(exc)
    if request.get("source_fingerprint") != current:
        return "source changed after completion"
    content = read_daily_text(HUB_HOME, period, "diary.md") if kind == "daily" else (output_path(kind, period).read_text(encoding="utf-8") if output_path(kind, period).exists() else "")
    if hashlib.sha256(content.encode()).hexdigest() != request.get("output_sha256"):
        return "report output changed after completion"
    return None


def claim_request(kind, period, agent, model, session=None):
    path = request_path(kind, period)
    session = session or os.environ.get("DSH_SESSION_ID")
    if not agent or agent == "unknown" or not session:
        return {"ok": False, "error": "agent_and_session_required"}
    if period >= now().strftime("%Y-%m-%d" if kind == "daily" else "%Y-%m"):
        return {"ok": False, "error": "period_not_closed"}
    # Source/cold reads happen before the reports lock (archive lock order).
    if kind == "monthly" and not any(day.startswith(period + "-") and session_rows(HUB_HOME, day) for day in available_dates(HUB_HOME)):
        return {"ok": False, "error": "monthly_data_unavailable"}
    dependencies = monthly_dependencies(period) if kind == "monthly" else []
    if dependencies:
        return {"ok": False, "error": "daily_dependencies_pending", "days": dependencies}
    try:
        fingerprint = source_fingerprint(kind, period)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    handle = locked()
    try:
        request = read_request(path)
        if not request:
            return {"ok": False, "error": "request_not_found"}
        if request.get("status") == "done":
            return {"ok": False, "error": "already_done", "request": public_request(request)}
        if lease_active(request):
            return {"ok": False, "error": "already_claimed", "request": public_request(request)}
        request.update(status="claimed", claimed_at=timestamp(), claimed_by=agent,
                       claimed_model=model, claimed_session=session, claim_token=secrets.token_urlsafe(32),
                       source_fingerprint=fingerprint)
        write_request(path, request)
        return {"ok": True, "request": request}
    finally:
        lockutil.release_lock(handle)


def _ownership_error(request, agent, session, token):
    if not lease_active(request):
        return "valid_claim_required"
    if not token or request.get("claimed_by") != agent or request.get("claimed_session") != session or not secrets.compare_digest(str(request.get("claim_token", "")), token):
        return "claim_owner_mismatch"
    return None


def _finish(kind, period, agent, model, completion_kind, session, token, text, idempotent):
    if completion_kind != "semantic":
        return {"ok": False, "error": "semantic_completion_required"}
    if kind == "monthly" and (deps := monthly_dependencies(period)):
        return {"ok": False, "error": "daily_dependencies_pending", "days": deps}
    path = request_path(kind, period)
    # Serialize source reads, report publication and archive movement in that order.
    with writer_guard(kind, period):
        handle = locked()
        try:
            request = read_request(path)
            if not request:
                return {"ok": False, "error": "request_not_found"}
            if kind == "monthly" and (deps := monthly_dependencies(period)):
                return {"ok": False, "error": "daily_dependencies_pending", "days": deps}
            if request.get("status") == "done":
                same = token and request.get("completed_token") == token and request.get("completed_by") == agent and request.get("completed_session") == session
                if idempotent and same:
                    existing = output_path(kind, period).read_text(encoding="utf-8") if output_path(kind, period).exists() else ""
                    if hashlib.sha256(existing.encode()).hexdigest() == request.get("output_sha256"):
                        same_input = text is None
                        if text is not None:
                            if kind == "daily":
                                try:
                                    import daily_format
                                    rows = daily_format.evidence_rows(HUB_HOME, period)
                                    canonical = daily_format.validate_overview(text, daily_format.ref_map_for(rows))
                                    same_input = daily_format.overview_sha256(canonical) == request.get("overview_sha256")
                                except ValueError:
                                    same_input = False
                            else:
                                same_input = text == existing
                        if same_input:
                            return {"ok": True, "idempotent": True, "request": request}
                return {"ok": False, "error": "already_done"}
            error = _ownership_error(request, agent, session, token)
            if error:
                return {"ok": False, "error": error}
            if kind == "daily" and text is None:
                return {"ok": False, "error": "daily_overview_publish_required"}
            try:
                current = source_fingerprint(kind, period)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
            if request.get("source_fingerprint") != current:
                return {"ok": False, "error": "source_changed_since_claim"}
            out = output_path(kind, period)
            overview_sha = None
            version = 2
            if kind == "daily" and text is not None:
                # Daily v3: the candidate is ONLY the AI overview; program
                # deterministically assembles TL;DR, stats and evidence under
                # the same archive→month→reports lock. Raw LLM output is
                # never written to diary.md.
                import daily_format
                try:
                    rows = daily_format.evidence_rows(HUB_HOME, period)
                    mapping = daily_format.ref_map_for(rows)
                    canonical = daily_format.validate_overview(text, mapping)
                    events = [row["record"] for row in session_rows(HUB_HOME, period)]
                    from evidence import build_evidence
                    evidence = build_evidence(events, period)
                    content = daily_format.assemble_daily_report(
                        canonical, period, evidence, events, mapping, HUB_HOME)
                    daily_format.validate_assembled_report(content, period, mapping, HUB_HOME)
                except ValueError as exc:
                    return {"ok": False, "error": str(exc)}
                overview_sha = daily_format.overview_sha256(canonical)
                version = 3
            else:
                try:
                    content = out.read_text(encoding="utf-8") if text is None else text
                    validate_report(content, kind, period, HUB_HOME)
                except (OSError, ValueError) as exc:
                    return {"ok": False, "error": str(exc)}
                if kind == "daily" and len(content.splitlines()) > 2 and \
                        content.splitlines()[2].strip() == "<!-- report-contract: daily-v3 -->":
                    version = 3
            if text is not None:
                atomic_text(out, content)
            request.update(status="done", completed_at=timestamp(), completed_by=agent,
                           completed_model=model, completed_session=session, completed_token=token,
                           completion_kind="semantic", contract_version=version,
                           output_sha256=hashlib.sha256(content.encode()).hexdigest())
            if overview_sha:
                request["overview_sha256"] = overview_sha
            write_request(path, request)
            return {"ok": True, "request": request}
        finally:
            lockutil.release_lock(handle)


def complete_request(kind, period, agent, model, completion_kind="semantic", session=None, token=None, idempotent=False):
    return _finish(kind, period, agent, model, completion_kind, session or os.environ.get("DSH_SESSION_ID"), token, None, idempotent)


def publish_report(kind, period, text, agent, model, session, token, idempotent=False):
    """Validate and atomically replace output only for the live lease owner."""
    return _finish(kind, period, agent, model, "semantic", session, token, text, idempotent)


def write_draft(kind, period, text):
    """Only pending/expired work can receive a draft; never overwrite any claim."""
    with writer_guard(kind, period):
        handle = locked()
        try:
            req = read_request(request_path(kind, period))
            if req.get("status") != "pending":
                return {"ok": False, "error": "report_not_pending"}
            if not isinstance(text, str) or not text.splitlines() or not any(m in text.splitlines()[0] for m in ("规则草稿", "规则模板")):
                return {"ok": False, "error": "draft_marker_required"}
            atomic_text(output_path(kind, period), text)
            return {"ok": True}
        finally:
            lockutil.release_lock(handle)


def release_claim(kind, period, agent, session, token):
    handle = locked()
    try:
        path = request_path(kind, period)
        request = read_request(path)
        error = _ownership_error(request, agent, session, token)
        if error:
            return {"ok": False, "error": error}
        request["status"] = "pending"
        _clear_claim(request)
        write_request(path, request)
        return {"ok": True}
    finally:
        lockutil.release_lock(handle)


def reopen_request(kind, period, reason):
    handle = locked()
    try:
        path = request_path(kind, period)
        request = read_request(path)
        if not request:
            return {"ok": False, "error": "request_not_found"}
        if lease_active(request):
            return {"ok": False, "error": "already_claimed"}
        request.update(status="pending", reopened_at=timestamp(), reopen_reason=reason)
        _clear_claim(request)
        write_request(path, request)
        return {"ok": True, "request": request}
    finally:
        lockutil.release_lock(handle)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--json", action="store_true")
    for name in ("enqueue", "claim", "complete", "publish", "reopen", "release"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--kind", choices=("daily", "monthly"), required=True)
        cmd.add_argument("--period", required=True)
        cmd.add_argument("--agent", default="unknown")
        cmd.add_argument("--model", default="unknown")
        cmd.add_argument("--session", default=os.environ.get("DSH_SESSION_ID"))
        cmd.add_argument("--token")
        cmd.add_argument("--reason", default="manual retry")
        cmd.add_argument("--source", action="append", default=[])
        cmd.add_argument("--completion-kind", default="semantic")
        cmd.add_argument("--idempotent", action="store_true")
        if name == "publish":
            cmd.add_argument("--input", required=True, help="semantic Markdown candidate; '-' reads stdin")
    args = parser.parse_args()
    try:
        if args.command == "list":
            result = [r for r in all_requests() if r.get("status") != "done"]
        elif args.command == "enqueue":
            result = public_request(ensure_request(args.kind, args.period, args.source))
        elif args.command == "claim":
            result = claim_request(args.kind, args.period, args.agent, args.model, args.session)
        elif args.command == "complete":
            result = complete_request(args.kind, args.period, args.agent, args.model, args.completion_kind, args.session, args.token, args.idempotent)
        elif args.command == "publish":
            text = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
            result = publish_report(args.kind, args.period, text, args.agent, args.model, args.session, args.token, args.idempotent)
        elif args.command == "release":
            result = release_claim(args.kind, args.period, args.agent, args.session, args.token)
        else:
            result = reopen_request(args.kind, args.period, args.reason)
    except (ValueError, OSError) as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
