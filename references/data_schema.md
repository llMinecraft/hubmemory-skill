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

## 2. 贡献统计（已停止落盘）

不再生成 `contrib.json`。历史旧文件可保留在无损归档中，统计按需由 `sessions.jsonl` 计算。缺失的 token 使用量不能当作真实的零成本，也不能当作工作质量/贡献比例。

---

## 3. `daily/YYYY-MM-DD/diary.md`

v3 日报：定时脚本生成客观明细及待总结占位，保留 pending 请求。次日首个 agent 持 session/token 租约，只生成 `## 全天总结` 候选，发布程序装配最终日报。AI 不生成统计、任务明细、来源表或 JSON，也不顺带提取 profile。

全天总结固定分为 `### 今天做了哪些事`、`### 主要结果`、`### 待办与阻塞`。按主线归纳，不塞路径、代码、命令和原始提示词；必要证据用 `[n]` 标记，程序在总结末尾列出参考来源。TL;DR 从总结首个有效句/条目提取，不另调模型。程序明细中的状态只是来源声称/规则候选，不代表独立验收；无法确定时明确未确认。

新日报不再嵌入 `Agent-readable summary` JSON，也不新增同内容的 JSON 旁车；索引复用已有 evidence/session 数据。原始事件 JSONL、派生证据 JSONL、租约和归档校验 JSON 仍保留。旧版 v2 日报的 JSON 读取兼容，不为格式升级自动重开已完成日报。月报现有契约单独保留。

---

## 4. `profile/constraints.md`

追加式，每条一行：

```
- [2026-09-21 14:32] 不要给我加 emoji（session: 5bc9407a）
- [2026-09-21 15:10] 中文回复，技术术语保留英文（session: rollout-...）
```

去重由 `profile_store.py` 统一实现：Unicode NFKC、大小写、空白和句末标点规范化；保留最近措辞/时间，并合并完整来源元信息。写入持 `.profile.lock`，临时文件 fsync 后原子替换。不会模糊合并不同范围/否定规则；语义近义项由 AI 核对后显式整理。status 展示仅规则正文，避免哈希和重复来源占用上下文。

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

---

## N. live/index.json (per-session extended fields)

| field | type | note |
|---|---|---|
| intent | string | first non-noise user message of the session (origin goal), watcher-written |
| last_user | string | latest non-noise user message (where the session is now) |

intent != last_user means the session pivoted mid-way. MANIFEST shows both side by side.

## N+1. daily/YYYY-MM-DD/diary.md (structure contract)

1. Line 2: one-line TL;DR extracted by code from the AI overview.
2. Only the structured whole-day overview is AI-authored; deterministic reference list and factual session/statistics sections are assembled by code at publication.
3. No embedded JSON in new daily reports. Indexes use existing evidence/session data; old JSON-bearing daily reports remain readable. Monthly v2 reports retain their separate contract.
4. The request records contract_version=3, source fingerprint, output hash and the owning lease; no AI-generated capability, count or ref path is trusted.

## N+2. MANIFEST.md

Generated by manifest.py (~450 token L0 index). Keys: live_sessions (with intent/current),
pending_reports, days (resolution decays with age), open_loops, memories.
Everything derivable from sessions.jsonl.

## N+3. dsh (DeepSeek Harness) source

- 路径：~/.dsh/sessions/<workspace>/<session>/session.v4.jsonl.zstd
- 格式：zstd 压缩 JSONL，事件 type 为 user/message、assistant/message、tool/call、tool/result
- 处理：DshParser 整文件解压按行数增量，offsets.byte_offset 存压缩文件大小
- agent 标识：dsh；model 取 message.source.model（如 deepseek-flash）
