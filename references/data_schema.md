# hubmemory 数据结构定义

本文件是 watcher / 日报脚本 / 查询脚本共享的字段契约。修改字段前请同步更新所有下游脚本。

---

## 1. `daily/YYYY-MM-DD/sessions.jsonl`

每行一条事件，UTF-8 JSON。字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ts` | string | ISO 8601 时间戳（含时区偏移） |
| `agent` | string | `claude` \| `codex` \| 其他 |
| `model` | string | 模型 ID，如 `claude-opus-4-7`、`gpt-5.4` |
| `session_id` | string | 原 agent 的 session UUID |
| `cwd` | string | 会话的工作目录 |
| `role` | string | `user` \| `assistant` \| `tool` |
| `text` | string | 消息正文（跳过 thinking / reasoning） |
| `tool_uses` | array | assistant 消息内嵌的工具调用摘要 `[{name, input_summary}]` |
| `tokens_out` | int | 该条 assistant 消息的输出 token 数（user 消息为 0） |
| `source_file` | string | 原 jsonl 文件路径，便于回溯 |
| `event_id` | string | 基于源文件路径、字节偏移和原始记录生成的稳定 SHA-256，用于崩溃重放去重 |

user 和 tool 消息的 `tokens_out` 恒为 0。

---

## 2. `daily/YYYY-MM-DD/contrib.json`

按 session 分组的贡献表：

```json
{
  "session_id_or_group_key": {
    "cwd": "/path/to/project",
    "agents": {
      "claude:claude-opus-4-7": 12450,
      "codex:gpt-5.4": 8200
    },
    "ratios": {
      "claude:claude-opus-4-7": 0.60,
      "codex:gpt-5.4": 0.40
    },
    "total_tokens": 20650
  }
}
```

---

## 3. `daily/YYYY-MM-DD/diary.md`

Markdown。结构建议：

```markdown
# 2026-09-21 工作日记

## 主题 1：yuzu 项目文档库重构
- claude 完成后端 documents router 的 llm normalization 接入 [claude 100%]
- ...

## 主题 2：跨 agent 记忆系统
- codex 生成 skill 初稿骨架 [codex 45% / claude 55%]
- ...

## 统计
- 总 token 输出：20,650
- 涉及 agent：claude (60%), codex (40%)
```

---

## 4. `profile/constraints.md`

追加式，每条一行：

```
- [2026-09-21 14:32] 不要给我加 emoji（session: 5bc9407a）
- [2026-09-21 15:10] 中文回复，技术术语保留英文（session: rollout-...）
```

去重规则：对内容做 sha1，与已存在行比对，命中则跳过。

---

## 5. `profile/preferences.md`

同 constraints.md 格式，但内容为"偏好/风格"而非硬性约束。区分逻辑由 LLM prompt 完成。

---

## 6. `live/index.json`

```json
{
  "5bc9407a-ba65-4fbe-b078-139f56499fb1": {
    "agent": "claude",
    "model": "claude-opus-4-7",
    "cwd": "/path/to/project",
    "started_at": "2026-09-21T14:00:00+08:00",
    "last_activity": "2026-09-21T20:35:12+08:00",
    "status": "active",
    "message_count": 42
  },
  "01a0b7c5-...": {
    "agent": "codex",
    "model": "gpt-5.4",
    "cwd": "/path/to/project",
    "started_at": "2026-09-21T15:30:00+08:00",
    "last_activity": "2026-09-21T20:34:55+08:00",
    "status": "active",
    "message_count": 18
  }
}
```

`status` 取值：`active`（最近 5 分钟有活动）、`idle`（5-30 分钟）、`stale`（>30 分钟）。

---

## 7. `live/<session_id>.md`

单会话实时快照，保留最近 20 轮对话：

```markdown
# Session 5bc9407a (claude / claude-opus-4-7)
- cwd: /path/to/project
- 最近活动：2026-09-21 20:35:12

## 最近对话
### [20:34:20] user
帮我看下 documents router...

### [20:34:45] assistant
好的，我先读一下相关文件。
> tool: Read(backend/app/routers/documents.py)
> tool: Grep("normalize_")
...
```

---

## 8. `state/offsets.json`

```json
{
  "/Users/example/.claude/projects/-Users-example-project/5bc9407a-....jsonl": {
    "inode": 12345678,
    "byte_offset": 45238,
    "last_line_hash": "sha1:..."
  }
}
```

inode 不匹配 → 重置 offset 为 0（文件轮转）。
byte_offset 超过实际文件大小 → 也重置（可能被截断）。

---

## 9. `config.yaml`

```yaml
version: 1
hostname: user-mbp
timezone: Asia/Shanghai
listen:
  claude: "~/.claude/projects"
  codex: "~/.codex/sessions"
  # codex_archived: "~/.codex/archived_sessions"  # 可选
skip_content:
  - thinking
  - reasoning
llm:
  enabled: false  # 可选增强，默认关闭；日报不依赖本地模型
  endpoint: "http://127.0.0.1:8080/v1/chat/completions"
  model: "mlx-community/Qwen3.8-27B-4bit"
  timeout_seconds: 60
report:
  daily_hour: 3
  daily_minute: 7
retention:
  # live 保留当天全部会话；跨日 stale 会话在超过宽限期、已写入 daily
  # 且未绑定未完成任务后，从 index 和 Markdown 快照中清理。
  live_after_idle_hours: 24
watcher:
  reconcile_interval_seconds: 15  # 文件事件漏报时的增量补采周期，最小 5 秒
```
# hubmemory 数据结构

## evidence.jsonl

每个 `daily/YYYY-MM-DD/evidence.jsonl` 行对应一个有活动的 session，由
`sessions.jsonl` 确定性派生，可随时重建。字段包括：

```json
{
  "id": "evidence_...",
  "kind": "session_work",
  "period": "2026-09-24",
  "session_id": "...",
  "agent": "codex-desktop",
  "model": "...",
  "cwd": "/project",
  "goals": ["用户明确目标"],
  "actions": ["有证据的实际动作"],
  "results": ["结果或验证线索"],
  "next_steps": ["明确写出的下一步"],
  "status": "completed|in_progress|blocked",
  "files": ["src/example.py"],
  "tools": ["apply_patch"],
  "source_event_ids": ["..."],
  "confidence": 0.65
}
```

## memory/memories.jsonl

长期记忆只能由 AI 根据 evidence 确认后写入，不能覆盖事实源。推荐字段：

```json
{
  "id": "memory_...",
  "title": "简短稳定结论",
  "content": "可被未来 agent 直接使用的事实",
  "type": "constraint|preference|architecture|workflow|lesson|fact",
  "status": "active|superseded|stale|archived",
  "project": "/project",
  "confidence": 0.9,
  "created_at": "2026-09-25T00:00:00+08:00",
  "updated_at": "2026-09-25T00:00:00+08:00",
  "source_session_ids": ["..."],
  "source_event_ids": ["..."],
  "supersedes": [],
  "tags": ["..."],
  "access_count": 0
}
```

`sessions.jsonl` 始终是原始证据；任何派生文件损坏都应该通过 watcher、日报脚本或 backfill 重建。
