#!/usr/bin/env python3
"""hubmemory watcher — 常驻监控 Claude Code / Codex CLI / qclaw 会话文件的进程。

监听：
- ~/.claude/projects/**/*.jsonl      Claude Code
- ~/.codex/sessions/**/*.jsonl        Codex CLI / Desktop / VSCode 插件
- ~/.qclaw/workspace/memory/*.md      qclaw 每日工作日志
- ~/.qclaw/workspace/sessions/*/store.json   qclaw session 元数据
"""
from __future__ import annotations

import atexit
import fnmatch
import hashlib
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install --user pyyaml", file=sys.stderr)
    sys.exit(1)

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:
    print("ERROR: watchdog not installed. Run: pip install --user watchdog", file=sys.stderr)
    sys.exit(1)


HUB_HOME = Path(os.environ.get("HUBMEMORY_HOME", os.path.expanduser("~/hubmemory")))
CONFIG_PATH = HUB_HOME / "config.yaml"
STATE_DIR = HUB_HOME / "state"
LIVE_DIR = HUB_HOME / "live"
DAILY_DIR = HUB_HOME / "daily"
LOGS_DIR = HUB_HOME / "logs"
OFFSETS_PATH = STATE_DIR / "offsets.json"
PID_PATH = STATE_DIR / "watcher.pid"
INDEX_PATH = LIVE_DIR / "index.json"
TASKS_PATH = LIVE_DIR / "tasks.json"

INITIAL_BACKFILL_DAYS = 3
FLUSH_INTERVAL_SEC = 30
RECONCILE_INTERVAL_SEC = 15
LIVE_RECENT_TURNS = 20
STATUS_ACTIVE_SEC = 5 * 60
STATUS_IDLE_SEC = 30 * 60
HEALTH_PATH = STATE_DIR / "health.json"

logger = logging.getLogger("hubmemory.watcher")


def setup_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return default_config()
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or default_config()


def default_config() -> dict[str, Any]:
    return {
        "version": 1,
        "listen": {
            "claude": str(Path.home() / ".claude" / "projects"),
            "codex": str(Path.home() / ".codex" / "sessions"),
        },
        "skip_content": ["thinking", "reasoning"],
        "retention": {"live_after_idle_hours": 24},
    }


def expand_path(p: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(p)))


def local_now() -> datetime:
    return datetime.now().astimezone()


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone()
    except (ValueError, TypeError):
        return None


def date_str(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def snapshot_event_id(event: dict[str, Any]) -> str:
    """Stable id for whole-file snapshot sources, including legacy rows."""
    identity = {
        "source_file": event.get("source_file") or "",
        "session_id": event.get("session_id") or "",
        "ts": event.get("ts") or "",
        "role": event.get("role") or "",
        "text": event.get("text") or "",
    }
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return "snapshot:" + hashlib.sha256(encoded).hexdigest()


class OffsetStore:
    """线程安全的 offsets 持久化。"""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, Any]] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("offsets.json unreadable, starting fresh: %s", exc)
            self.data = {}

    def get(self, path_str: str) -> dict[str, Any]:
        with self.lock:
            return dict(self.data.get(path_str, {}))

    def set(self, path_str: str, inode: int, offset: int) -> None:
        with self.lock:
            self.data[path_str] = {"inode": inode, "byte_offset": offset}
            self.dirty = True

    def flush(self) -> None:
        with self.lock:
            if not self.dirty:
                return
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)
            self.dirty = False


class LiveIndex:
    """live/index.json 的封装 + 内存中的会话对话窗口。"""

    def __init__(
        self,
        path: Path,
        *,
        daily_dir: Path = DAILY_DIR,
        live_dir: Path = LIVE_DIR,
        tasks_path: Path = TASKS_PATH,
        retention_hours: float = 24,
    ):
        self.path = path
        self.daily_dir = daily_dir
        self.live_dir = live_dir
        self.tasks_path = tasks_path
        self.retention_hours = max(0, retention_hours)
        self.lock = threading.Lock()
        self.data: dict[str, dict[str, Any]] = {}
        self.recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=LIVE_RECENT_TURNS))
        self._archived_session_cache: dict[str, tuple[int, int, set[str]]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as f:
                self.data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self.data = {}
        # 重启后从今天的 sessions.jsonl 恢复各 session 的最近 N 轮对话
        today = date_str(local_now())
        sessions_path = self.daily_dir / today / "sessions.jsonl"
        if not sessions_path.exists():
            return
        try:
            per_session: dict[str, list[dict]] = {}
            with sessions_path.open("r", encoding="utf-8") as f:
                for line in f:
                    try:
                        evt = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    sid = evt.get("session_id")
                    if not sid:
                        continue
                    per_session.setdefault(sid, []).append(evt)
            for sid, events in per_session.items():
                for evt in events[-LIVE_RECENT_TURNS:]:
                    self.recent[sid].append(evt)
        except OSError:
            pass

    def update_session(self, session_id: str, event: dict[str, Any]) -> None:
        with self.lock:
            entry = self.data.setdefault(session_id, {
                "agent": event["agent"],
                "model": event.get("model") or "unknown",
                "cwd": event.get("cwd") or "",
                "started_at": event["ts"],
                "last_activity": event["ts"],
                "status": "active",
                "message_count": 0,
            })
            entry["last_activity"] = event["ts"]
            entry["agent"] = event["agent"]
            entry["status"] = "active"
            if event.get("model"):
                entry["model"] = event["model"]
            if event.get("cwd"):
                entry["cwd"] = event["cwd"]
            entry["message_count"] += 1
            self.recent[session_id].append(event)

    def refresh_status(self, now: datetime | None = None) -> None:
        now = now or local_now()
        with self.lock:
            for entry in self.data.values():
                last = parse_iso(entry.get("last_activity"))
                if not last:
                    continue
                delta = (now - last).total_seconds()
                if delta < STATUS_ACTIVE_SEC:
                    entry["status"] = "active"
                elif delta < STATUS_IDLE_SEC:
                    entry["status"] = "idle"
                else:
                    entry["status"] = "stale"

    def _protected_session_ids(self) -> set[str] | None:
        if not self.tasks_path.exists():
            return set()
        try:
            payload = json.loads(self.tasks_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # tasks.json 无法读取时宁可暂缓清理，避免误删正在协调的会话。
            logger.warning("live retention skipped: tasks file unreadable: %s", exc)
            return None
        protected_statuses = {"active", "blocked", "paused"}
        return {
            task.get("session_id")
            for task in payload.get("active_tasks") or []
            if task.get("status") in protected_statuses and task.get("session_id")
        }

    def _archived_session_ids(self, day: str) -> set[str]:
        sessions_path = self.daily_dir / day / "sessions.jsonl"
        try:
            stat = sessions_path.stat()
        except OSError:
            return set()
        cached = self._archived_session_cache.get(day)
        cache_key = (stat.st_mtime_ns, stat.st_size)
        if cached and cached[:2] == cache_key:
            return cached[2]
        session_ids: set[str] = set()
        try:
            with sessions_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        session_id = json.loads(line).get("session_id")
                    except (json.JSONDecodeError, AttributeError):
                        continue
                    if session_id:
                        session_ids.add(session_id)
        except OSError:
            return set()
        self._archived_session_cache[day] = (*cache_key, session_ids)
        return session_ids

    def _remove_snapshot(self, session_id: str) -> None:
        suffix = f"_{session_id}.md"
        try:
            candidates = list(self.live_dir.glob("*.md"))
        except OSError:
            return
        for path in candidates:
            if not path.name.endswith(suffix):
                continue
            try:
                path.unlink()
            except OSError as exc:
                logger.warning("failed to remove stale live snapshot %s: %s", path, exc)

    def prune_stale(self, now: datetime | None = None) -> list[str]:
        """Drop old stale sessions only after their fact events are archived."""
        now = now or local_now()
        protected = self._protected_session_ids()
        if protected is None:
            return []
        cutoff = now - timedelta(hours=self.retention_hours)
        today = date_str(now)
        removed: list[str] = []
        with self.lock:
            for session_id, entry in list(self.data.items()):
                last = parse_iso(entry.get("last_activity"))
                if (
                    entry.get("status") != "stale"
                    or not last
                    or date_str(last) == today
                    or last > cutoff
                    or session_id in protected
                    or session_id not in self._archived_session_ids(date_str(last))
                ):
                    continue
                self.data.pop(session_id, None)
                self.recent.pop(session_id, None)
                # Keep deletion inside the lock. A new event can recreate the live
                # entry afterwards and its newly written snapshot will then survive.
                self._remove_snapshot(session_id)
                removed.append(session_id)
        if removed:
            logger.info("pruned %d archived stale live session(s)", len(removed))
        return removed

    def flush(self) -> None:
        now = local_now()
        self.refresh_status(now)
        self.prune_stale(now)
        with self.lock:
            tmp = self.path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            tmp.replace(self.path)

    def dump_session_md(self, session_id: str) -> None:
        with self.lock:
            entry = dict(self.data.get(session_id, {}))
            events = list(self.recent.get(session_id, []))
        if not entry:
            return
        agent = (entry.get("agent") or "unknown").replace("/", "-")
        md_path = LIVE_DIR / f"{agent}_{session_id}.md"
        lines = [
            f"# Session {session_id[:8]} ({entry.get('agent')} / {entry.get('model')})",
            f"- cwd: {entry.get('cwd')}",
            f"- started: {entry.get('started_at')}",
            f"- last activity: {entry.get('last_activity')}",
            f"- status: {entry.get('status')}",
            f"- messages: {entry.get('message_count')}",
            "",
            "## 最近对话",
        ]
        for evt in events:
            if evt.get("role") == "tool":
                continue
            ts_display = evt["ts"][:19].replace("T", " ")
            role = evt["role"]
            text = (evt.get("text") or "").strip()
            if len(text) > 800:
                text = text[:800] + "…"
            lines.append(f"### [{ts_display}] {role}")
            if text:
                lines.append(text)
            for tu in evt.get("tool_uses") or []:
                lines.append(f"> tool: {tu['name']}({tu.get('input_summary', '')})")
            lines.append("")
        md_path.write_text("\n".join(lines), encoding="utf-8")


class DailyWriter:
    """按日期分文件的会话事件 append。"""

    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.Lock()
        self.handles: dict[str, Any] = {}
        self.seen_event_ids: dict[str, set[str]] = {}
        self._rebuild_pending: set[str] = set()
        self._rebuild_lock = threading.Lock()

    def _load_seen_event_ids(self, d: str, target: Path) -> set[str]:
        seen = self.seen_event_ids.get(d)
        if seen is not None:
            return seen
        seen = set()
        if target.exists():
            try:
                with target.open("r", encoding="utf-8") as f:
                    for line in f:
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        event_id = event.get("event_id")
                        if event_id:
                            seen.add(event_id)
                        # Older whole-file snapshot rows predate event_id. Index a
                        # semantic id so upgrading does not append them once more.
                        seen.add(snapshot_event_id(event))
            except OSError:
                pass
        self.seen_event_ids[d] = seen
        return seen

    def append(self, event: dict[str, Any]) -> bool:
        dt = parse_iso(event["ts"]) or local_now()
        d = date_str(dt)
        target_dir = self.root / d
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "sessions.jsonl"
        with self.lock:
            seen = self._load_seen_event_ids(d, target)
            event_id = event.get("event_id")
            if event_id and event_id in seen:
                return False
            with target.open("a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            if event_id:
                seen.add(event_id)
        with self._rebuild_lock:
            self._rebuild_pending.add(d)
        return True

    def flush_conversations(self) -> None:
        with self._rebuild_lock:
            pending = set(self._rebuild_pending)
            self._rebuild_pending.clear()
        for d in pending:
            try:
                self._rebuild_conversations(d)
            except Exception as exc:
                logger.warning("rebuild_conversations failed for %s: %s", d, exc)

    def _rebuild_conversations(self, d: str) -> None:
        sessions_path = self.root / d / "sessions.jsonl"
        if not sessions_path.exists():
            return
        events: list[dict] = []
        with sessions_path.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue

        if not events:
            return

        # 按 session_id 分组，保留原始时间顺序
        grouped: dict[str, list[dict]] = {}
        for evt in events:
            sid = evt.get("session_id") or "unknown"
            grouped.setdefault(sid, []).append(evt)

        lines = [f"# {d} 会话归档", ""]
        total_user = sum(1 for e in events if e.get("role") == "user")
        total_assistant = sum(1 for e in events if e.get("role") == "assistant")
        lines.append(f"> 共 {len(grouped)} 个 session，{total_user} 条用户消息，{total_assistant} 条 AI 回复")
        lines.append("")

        for sid, evts in grouped.items():
            agent = evts[0].get("agent", "?")
            model = evts[0].get("model", "?")
            cwd = evts[0].get("cwd", "")
            first_ts = (evts[0].get("ts") or "")[:16].replace("T", " ")
            last_ts = (evts[-1].get("ts") or "")[:16].replace("T", " ")
            user_count = sum(1 for e in evts if e.get("role") == "user")
            asst_count = sum(1 for e in evts if e.get("role") == "assistant")
            lines.append(f"## {agent} / {model}")
            lines.append(f"- session: `{sid[:16]}…`")
            if cwd:
                lines.append(f"- cwd: {cwd}")
            lines.append(f"- 时间: {first_ts} → {last_ts}")
            lines.append(f"- 消息: 用户 {user_count} 条，AI {asst_count} 条")
            lines.append("")

            for evt in evts:
                role = evt.get("role", "?")
                if role == "tool":
                    continue
                ts_str = (evt.get("ts") or "")[:16].replace("T", " ")
                text = (evt.get("text") or "").strip()
                if len(text) > 400:
                    text = text[:400] + "…"
                tool_uses = evt.get("tool_uses") or []

                if role == "user":
                    lines.append(f"**[{ts_str}] 用户**")
                elif role == "assistant":
                    lines.append(f"**[{ts_str}] AI**")
                else:
                    lines.append(f"**[{ts_str}] {role}**")

                if text:
                    lines.append(text)
                for tu in tool_uses:
                    lines.append(f"> `{tu.get('name', '?')}` {tu.get('input_summary', '')}")
                lines.append("")

        out_path = self.root / d / "conversations.md"
        tmp = out_path.with_suffix(".md.tmp")
        tmp.write_text("\n".join(lines), encoding="utf-8")
        tmp.replace(out_path)


def summarize_input(value: Any, max_len: int = 120) -> str:
    try:
        s = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    except (TypeError, ValueError):
        s = str(value)
    s = " ".join(s.split())
    return s[:max_len] + ("…" if len(s) > max_len else "")


def parse_claude_line(line: dict[str, Any], source: str) -> dict[str, Any] | None:
    msg_type = line.get("type")
    if msg_type not in ("user", "assistant"):
        return None
    message = line.get("message") or {}
    role = message.get("role") or msg_type
    ts = line.get("timestamp")
    if not ts:
        return None

    content = message.get("content")
    text_parts: list[str] = []
    tool_uses: list[dict[str, str]] = []
    has_tool_result = False

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text_parts.append(item.get("text", ""))
            elif item_type == "thinking":
                continue
            elif item_type == "tool_use":
                tool_uses.append({
                    "name": item.get("name", "?"),
                    "input_summary": summarize_input(item.get("input")),
                })
            elif item_type == "tool_result":
                has_tool_result = True

    text = "\n".join(p for p in text_parts if p).strip()

    # user 消息里只有 tool_result（无用户输入文本）→ 归为 tool 角色，不计入用户对话
    if not text and not tool_uses:
        if has_tool_result:
            role = "tool"
        else:
            return None

    usage = message.get("usage") or {}
    tokens_out = usage.get("output_tokens", 0) if role == "assistant" else 0

    return {
        "ts": parse_iso(ts).isoformat() if parse_iso(ts) else ts,
        "agent": "claude",
        "model": message.get("model") or "unknown",
        "session_id": line.get("sessionId") or "",
        "cwd": line.get("cwd") or "",
        "role": role,
        "text": text,
        "tool_uses": tool_uses,
        "tokens_out": tokens_out,
        "source_file": source,
    }


class CodexParser:
    """Codex rollout 文件是流式的，首行 session_meta 之后是 response_item。"""

    def __init__(self):
        self.session_meta: dict[str, dict[str, Any]] = {}

    @staticmethod
    def session_id_from_source(source: str) -> str:
        fname = Path(source).stem
        parts = fname.split("-")
        return "-".join(parts[-5:]) if len(parts) >= 5 else ""

    def prime_source(self, file_path: Path) -> None:
        """Load the immutable first-line metadata before an incremental read."""
        sid = self.session_id_from_source(str(file_path))
        if not sid or sid in self.session_meta:
            return
        try:
            with file_path.open("rb") as f:
                raw = f.readline()
            if raw.endswith(b"\n"):
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
                if parsed.get("type") == "session_meta":
                    self.parse(parsed, str(file_path))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return

    def parse(self, line: dict[str, Any], source: str) -> dict[str, Any] | None:
        line_type = line.get("type")
        if line_type == "session_meta":
            payload = line.get("payload") or line
            sid = payload.get("session_id") or payload.get("id") or ""
            if sid:
                self.session_meta[sid] = {
                    "cwd": payload.get("cwd") or "",
                    "model": payload.get("model_provider") or payload.get("model") or "",
                    "originator": payload.get("originator") or "",
                    "cli_version": payload.get("cli_version") or "",
                    "started_at": payload.get("timestamp") or line.get("timestamp"),
                }
            return None

        if line_type == "turn_context":
            payload = line.get("payload") or {}
            sid = payload.get("session_id") or self.session_id_from_source(source)
            if sid:
                meta = self.session_meta.setdefault(sid, {})
                if payload.get("model"):
                    meta["model"] = payload["model"]
                if payload.get("cwd"):
                    meta["cwd"] = payload["cwd"]
            return None

        if line_type != "response_item":
            return None

        payload = line.get("payload") or {}
        payload_type = payload.get("type")
        ts = line.get("timestamp") or payload.get("timestamp")

        # 从文件名回推 session_id（rollout-<ts>-<uuid>.jsonl）
        sid = payload.get("session_id") or ""
        if not sid:
            sid = self.session_id_from_source(source)
        meta = self.session_meta.get(sid, {})

        if payload_type == "reasoning":
            return None

        text_parts: list[str] = []
        tool_uses: list[dict[str, str]] = []
        role = payload.get("role") or "assistant"

        if payload_type == "message":
            content = payload.get("content") or []
            if isinstance(content, list):
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    itype = item.get("type")
                    if itype in ("input_text", "output_text", "text"):
                        text_parts.append(item.get("text", ""))
            elif isinstance(content, str):
                text_parts.append(content)
        elif payload_type in ("function_call", "custom_tool_call"):
            role = "assistant"
            tool_uses.append({
                "name": payload.get("name") or "?",
                "input_summary": summarize_input(
                    payload.get("arguments") if payload_type == "function_call"
                    else payload.get("input")
                ),
            })
        elif payload_type in ("function_call_output", "custom_tool_call_output"):
            role = "tool"
            output = payload.get("output")
            text_parts.append(f"[tool_output] {summarize_input(output, max_len=400)}")
        else:
            return None

        text = "\n".join(p for p in text_parts if p).strip()
        if not text and not tool_uses:
            return None

        usage = payload.get("usage") or {}
        tokens_out = 0
        if role == "assistant":
            tokens_out = usage.get("output_tokens") or usage.get("completion_tokens") or 0
            if not tokens_out and text:
                tokens_out = max(1, len(text) // 3)  # 粗估

        originator = (meta.get("originator") or "").lower()
        if "desktop" in originator:
            agent_label = "codex-desktop"
        elif "vscode" in originator:
            agent_label = "codex-vscode"
        elif originator:
            agent_label = f"codex-{originator}"
        else:
            agent_label = "codex"

        return {
            "ts": parse_iso(ts).isoformat() if parse_iso(ts) else ts,
            "agent": agent_label,
            "model": meta.get("model") or "unknown",
            "session_id": sid,
            "cwd": meta.get("cwd") or "",
            "role": role,
            "text": text,
            "tool_uses": tool_uses,
            "tokens_out": tokens_out,
            "source_file": source,
        }


class QclawMemoryParser:
    """解析 ~/.qclaw/workspace/memory/YYYY-MM-DD.md 格式的工作日志。

    qclaw memory 文件是 markdown，按时间戳和主题分段，直接对应 hubmemory 的 diary 层。
    每次文件变化时把整个文件内容作为一条 assistant 事件写入当天 sessions.jsonl。
    """

    def parse(self, file_path: Path) -> list[dict[str, Any]]:
        try:
            text = file_path.read_text(encoding="utf-8").strip()
        except OSError:
            return []
        if not text:
            return []
        # 从文件名提取日期
        date_str = file_path.stem  # YYYY-MM-DD
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(
                hour=23, minute=59, second=0,
                tzinfo=datetime.now().astimezone().tzinfo,
            )
        except ValueError:
            dt = local_now()
        ts = dt.isoformat()
        mtime = file_path.stat().st_mtime
        return [{
            "ts": ts,
            "agent": "qclaw",
            "model": "unknown",
            "session_id": f"qclaw-memory-{date_str}",
            "cwd": str(file_path.parent),
            "role": "assistant",
            "text": text,
            "tool_uses": [],
            "tokens_out": max(1, len(text) // 3),
            "source_file": str(file_path),
            "_mtime": mtime,
        }]


class QclawSessionParser:
    """解析 ~/.qclaw/workspace/sessions/<uuid>/store.json + summary_*.md。

    store.json 提供 session 元数据（name, status, createdAt, lastMessageCount）。
    summary_*.md 提供高质量的任务摘要（任务背景、执行过程、关键结果）。
    """

    def parse(self, file_path: Path) -> list[dict[str, Any]]:
        if file_path.name != "store.json":
            return []
        try:
            store = json.loads(file_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []

        sid = store.get("id") or file_path.parent.name
        name = store.get("name") or sid
        created_at = store.get("createdAt") or store.get("updatedAt") or ""
        updated_at = store.get("updatedAt") or created_at
        status = store.get("status") or "unknown"
        msg_count = store.get("lastMessageCount") or 0
        model = "unknown"
        for ag in store.get("agents") or []:
            if ag.get("model") and ag["model"] != "unknown":
                model = ag["model"]
                break

        # 找同目录下的 summary_*.md
        summary_text = ""
        for f in file_path.parent.glob("summary_*.md"):
            try:
                summary_text = f.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            break

        text = f"Session: {name} (status={status}, messages={msg_count})"
        if summary_text:
            text = summary_text

        ts = parse_iso(updated_at) or local_now()
        events = []
        if text:
            events.append({
                "ts": ts.isoformat(),
                "agent": "qclaw",
                "model": model,
                "session_id": f"qclaw-{sid}",
                "cwd": str(file_path.parent.parent.parent),
                "role": "assistant",
                "text": text,
                "tool_uses": [],
                "tokens_out": max(1, len(text) // 3),
                "source_file": str(file_path),
            })
        return events


class SessionProcessor:
    def __init__(self, offsets: OffsetStore, live: LiveIndex, daily: DailyWriter, config: dict[str, Any]):
        self.offsets = offsets
        self.live = live
        self.daily = daily
        self.config = config
        self.codex_parser = CodexParser()
        self.qclaw_memory_parser = QclawMemoryParser()
        self.qclaw_session_parser = QclawSessionParser()
        self.lock = threading.Lock()

    def classify(self, file_path: Path) -> str | None:
        for key, raw in (self.config.get("listen") or {}).items():
            if not raw:
                continue
            root = expand_path(raw)
            try:
                file_path.relative_to(root)
            except ValueError:
                continue
            if key.startswith("claude"):
                return "claude"
            if key.startswith("codex"):
                return "codex"
            if key.startswith("qclaw"):
                return key  # qclaw_memory or qclaw_sessions
            return key
        return None

    def process_file(self, file_path: Path) -> None:
        agent = self.classify(file_path)
        if agent is None:
            return
        if not file_path.exists() or not file_path.is_file():
            return

        # qclaw memory md 和 sessions store.json 用专门 parser，不走 offset 逻辑
        if agent == "qclaw_memory" and file_path.suffix == ".md":
            events = self.qclaw_memory_parser.parse(file_path)
            for event in events:
                event["event_id"] = snapshot_event_id(event)
                if self.daily.append(event):
                    self.live.update_session(event["session_id"], event)
                    self.live.dump_session_md(event["session_id"])
            if events:
                self.live.flush()
            return

        if agent == "qclaw_sessions" and file_path.name == "store.json":
            events = self.qclaw_session_parser.parse(file_path)
            for event in events:
                event["event_id"] = snapshot_event_id(event)
                if self.daily.append(event):
                    self.live.update_session(event["session_id"], event)
                    self.live.dump_session_md(event["session_id"])
            if events:
                self.live.flush()
            return
        with self.lock:
            path_str = str(file_path)
            try:
                stat = file_path.stat()
                offset_entry = self.offsets.get(path_str)
                expected_inode = offset_entry.get("inode")
                start_offset = int(offset_entry.get("byte_offset", 0))
                if expected_inode is not None and expected_inode != stat.st_ino:
                    start_offset = 0
                if start_offset > stat.st_size:
                    start_offset = 0
                if agent == "codex":
                    self.codex_parser.prime_source(file_path)

                with file_path.open("rb") as f:
                    f.seek(start_offset)
                    updated_sessions: set[str] = set()
                    processed_bytes = start_offset
                    while True:
                        line_start = f.tell()
                        raw_bytes = f.readline()
                        if not raw_bytes:
                            break
                        # Writers may be interrupted between bytes. Commit offsets only
                        # through complete JSONL records so the tail is retried later.
                        if not raw_bytes.endswith(b"\n"):
                            processed_bytes = line_start
                            break
                        processed_bytes = f.tell()
                        raw_line = raw_bytes.decode("utf-8", errors="replace").strip()
                        if not raw_line:
                            continue
                        try:
                            parsed = json.loads(raw_line)
                        except json.JSONDecodeError:
                            continue
                        if agent == "claude":
                            event = parse_claude_line(parsed, path_str)
                        else:
                            event = self.codex_parser.parse(parsed, path_str)
                        if event is None:
                            continue
                        if not event.get("session_id"):
                            # 无法定位 session 的直接跳过（Codex 极少数情况）
                            continue
                        event["event_id"] = hashlib.sha256(
                            f"{path_str}:{line_start}:".encode("utf-8") + raw_bytes
                        ).hexdigest()
                        if self.daily.append(event):
                            self.live.update_session(event["session_id"], event)
                            updated_sessions.add(event["session_id"])
                    self.offsets.set(path_str, stat.st_ino, processed_bytes)
                    for sid in updated_sessions:
                        self.live.dump_session_md(sid)
                    if updated_sessions:
                        self.live.flush()
                    self.offsets.flush()
            except OSError as exc:
                logger.warning("read failed %s: %s", file_path, exc)


class HealthState:
    """Atomic watcher heartbeat and reconciliation telemetry."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.started_at = local_now().isoformat()
        self.data: dict[str, Any] = {}

    def write(self, **updates: Any) -> None:
        with self.lock:
            self.data.update(updates)
            self.data.update({
                "pid": os.getpid(),
                "started_at": self.started_at,
                "heartbeat_at": local_now().isoformat(),
            })
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.path)


class JsonlHandler(FileSystemEventHandler):
    def __init__(self, work_queue: queue.Queue):
        self.q = work_queue

    def _enqueue(self, path_str: str) -> None:
        p = Path(path_str)
        if p.suffix in (".jsonl", ".md", ".json"):
            self.q.put(p)

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._enqueue(event.src_path)

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._enqueue(event.dest_path)


def initial_backfill(processor: SessionProcessor, roots: list[Path], days: int) -> None:
    cutoff = time.time() - days * 86400
    seen = 0
    for root in roots:
        if not root.exists():
            logger.info("root not present, skip: %s", root)
            continue
        # 根据 root 对应的 agent 类型决定扫描的文件模式
        agent = processor.classify(root / "placeholder.jsonl")
        if agent and agent.startswith("qclaw_memory"):
            patterns = ["*.md"]
        elif agent and agent.startswith("qclaw_sessions"):
            patterns = ["store.json"]
        else:
            patterns = ["*.jsonl"]
        for pattern in patterns:
            for path in root.rglob(pattern):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if mtime < cutoff:
                    try:
                        st = path.stat()
                        if str(path) not in processor.offsets.data:
                            processor.offsets.set(str(path), st.st_ino, st.st_size)
                    except OSError:
                        pass
                    continue
                processor.process_file(path)
                seen += 1
    logger.info("initial backfill scanned %d recent files under %s", seen, roots)


def periodic_flush(
    offsets: OffsetStore,
    live: LiveIndex,
    daily: DailyWriter,
    health: HealthState,
    stop_event: threading.Event,
) -> None:
    while not stop_event.is_set():
        stop_event.wait(FLUSH_INTERVAL_SEC)
        try:
            offsets.flush()
            live.flush()
            daily.flush_conversations()
            health.write(last_flush_at=local_now().isoformat())
        except Exception as exc:
            logger.exception("flush failed: %s", exc)
            health.write(last_error=f"flush: {exc}")


def source_patterns(processor: SessionProcessor, root: Path) -> list[str]:
    agent = processor.classify(root / "placeholder.jsonl")
    if agent and agent.startswith("qclaw_memory"):
        return ["*.md"]
    if agent and agent.startswith("qclaw_sessions"):
        return ["store.json"]
    return ["*.jsonl"]


def reconcile_once(processor: SessionProcessor, roots: list[Path]) -> int:
    """Process sources whose size/inode differs from the committed offset."""
    reconciled = 0
    for root in roots:
        if not root.exists():
            continue
        for pattern in source_patterns(processor, root):
            for path in root.rglob(pattern):
                agent = processor.classify(path)
                if agent in ("qclaw_memory", "qclaw_sessions"):
                    # These formats are whole-file snapshots and still rely on native
                    # file events to avoid duplicating the same snapshot every cycle.
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                entry = processor.offsets.get(str(path))
                inode = entry.get("inode")
                offset = int(entry.get("byte_offset", 0))
                if inode != stat.st_ino or offset != stat.st_size:
                    processor.process_file(path)
                    reconciled += 1
    return reconciled


def reconciliation_loop(
    processor: SessionProcessor,
    roots: list[Path],
    health: HealthState,
    interval_seconds: int,
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(interval_seconds):
        try:
            count = reconcile_once(processor, roots)
            health.write(
                last_reconcile_at=local_now().isoformat(),
                last_reconcile_files=count,
                last_error=None,
            )
            if count:
                logger.info("reconciled %d lagging source file(s)", count)
        except Exception as exc:
            logger.exception("reconciliation failed: %s", exc)
            health.write(last_error=f"reconcile: {exc}")


def worker_loop(processor: SessionProcessor, work_queue: queue.Queue, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            path = work_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            processor.process_file(path)
        except Exception:
            logger.exception("process_file failed: %s", path)


def write_pid() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PID_PATH.write_text(str(os.getpid()), encoding="utf-8")


def cleanup_pid() -> None:
    try:
        if PID_PATH.exists():
            current = PID_PATH.read_text().strip()
            if current == str(os.getpid()):
                PID_PATH.unlink()
    except OSError:
        pass


def main() -> int:
    for d in (STATE_DIR, LIVE_DIR, DAILY_DIR, LOGS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    setup_logging()
    write_pid()
    atexit.register(cleanup_pid)

    config = load_config()
    offsets = OffsetStore(OFFSETS_PATH)
    retention_config = config.get("retention") or {}
    try:
        live_retention_hours = float(retention_config.get("live_after_idle_hours", 24))
    except (TypeError, ValueError):
        live_retention_hours = 24
    live = LiveIndex(INDEX_PATH, retention_hours=live_retention_hours)
    daily = DailyWriter(DAILY_DIR)
    processor = SessionProcessor(offsets, live, daily, config)
    health = HealthState(HEALTH_PATH)
    watcher_config = config.get("watcher") or {}
    try:
        reconcile_interval = max(5, int(
            watcher_config.get("reconcile_interval_seconds", RECONCILE_INTERVAL_SEC)
        ))
    except (TypeError, ValueError):
        reconcile_interval = RECONCILE_INTERVAL_SEC

    claude_root = expand_path(config["listen"]["claude"])
    codex_root = expand_path(config["listen"]["codex"])
    roots: list[Path] = []
    for key, raw in (config.get("listen") or {}).items():
        if not raw:
            continue
        p = expand_path(raw)
        if p and p not in roots:
            roots.append(p)

    logger.info("hubmemory watcher starting (pid=%d)", os.getpid())
    logger.info("claude root: %s (exists=%s)", claude_root, claude_root.exists())
    logger.info("codex root: %s (exists=%s)", codex_root, codex_root.exists())
    for r in roots:
        if r not in (claude_root, codex_root):
            logger.info("extra root: %s (exists=%s)", r, r.exists())

    initial_backfill(processor, roots, INITIAL_BACKFILL_DAYS)
    offsets.flush()
    live.flush()
    health.write(
        status="running",
        roots=[str(root) for root in roots],
        reconcile_interval_seconds=reconcile_interval,
        live_retention_hours=live_retention_hours,
        last_reconcile_at=local_now().isoformat(),
        last_reconcile_files=0,
        last_error=None,
    )

    work_queue: queue.Queue = queue.Queue()
    handler = JsonlHandler(work_queue)
    observer = Observer()
    for root in roots:
        if root.exists():
            observer.schedule(handler, str(root), recursive=True)
    observer.start()

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=worker_loop, args=(processor, work_queue, stop_event), daemon=True),
        threading.Thread(target=periodic_flush, args=(offsets, live, daily, health, stop_event), daemon=True),
        threading.Thread(
            target=reconciliation_loop,
            args=(processor, roots, health, reconcile_interval, stop_event),
            daemon=True,
        ),
    ]
    for t in threads:
        t.start()

    def handle_signal(signum, _frame):
        logger.info("received signal %d, shutting down", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while not stop_event.is_set():
            time.sleep(1.0)
    finally:
        observer.stop()
        observer.join(timeout=5)
        stop_event.set()
        offsets.flush()
        live.flush()
        health.write(status="stopped", stopped_at=local_now().isoformat())
        logger.info("hubmemory watcher stopped")

    return 0


if __name__ == "__main__":
    sys.exit(main())
