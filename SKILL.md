---
name: hubmemory
description: 本机跨 agent 共享记忆中枢。监控并汇总 Claude Code / Codex CLI / Codex Desktop 的所有会话，统一存入 ~/hubmemory。当用户询问"最近/今天/昨天/这几天在干什么"、"上次做了什么"、"另一个 agent 在做什么"、"之前提到过 xxx 吗"时必须使用。新会话开始时主动 bootstrap，让自己感知其他 agent 的当前状态。
---

# hubmemory — 本机多 agent 共享记忆中枢

## 接入规则（每次新会话必须执行，全部幂等）

```bash
# 第一步：确认 watcher 在运行（已在跑则秒退，不会重复启动）
bash ~/.claude/skills/hubmemory/scripts/bootstrap.sh

# 第二步：感知当前状态 + 遵守用户约束（status.py 输出已含约束摘要）
python3 ~/.claude/skills/hubmemory/scripts/status.py
```

如果你的工具不支持 Bash，用文件读取工具直接读 `~/hubmemory/profile/constraints.md` 和 `~/hubmemory/live/index.json`。

### 自动接管待汇总报告

每次启动新会话时，在回答用户之前检查待处理的日报/月报：

```bash
python3 ~/.claude/skills/hubmemory/scripts/report_due.py list --json
```

如果存在 `pending` 或已超时的 `claimed` 任务，当前 agent 自动领取并完成，不向用户询问是否执行：

```bash
python3 ~/.claude/skills/hubmemory/scripts/report_due.py claim \
  --kind daily --period YYYY-MM-DD --agent <当前agent> --model <当前模型>
```

读取任务的 `source` 文件，用你当前正在使用的模型总结实际工作，直接写入对应的 `diary.md` 或 `monthly/YYYY-MM.md`。总结完成后标记：

```bash
python3 ~/.claude/skills/hubmemory/scripts/report_due.py complete \
  --kind daily --period YYYY-MM-DD --agent <当前agent> --model <当前模型>
```

不要把模型私有思考写入报告；报告只记录有证据的目标、实际动作、结果、状态和下一步。一个 agent 已领取的任务不要重复处理。

日报必须先给出 `## 全天总结`，说明当天主要工作主线、已完成事项、未收束事项和全天结论；随后按 agent 和工作事项逐项写出目标、实际完成、关键证据、任务结论、当前状态（已完成/进行中/失败/阻塞）、下一步和来源 session。不要只罗列消息或工具调用。完成日报/月报后，如果证据显示某个结论对未来多个 session 仍然有效，再用 `remember.py add --json` 写入长期记忆。必须提供至少一个 `source_session_ids` 或 `source_event_ids`；一次性任务、猜测和未验证结论不要写入。

### 当前任务相关记忆

如果用户给出了具体任务，在回答前用当前任务做一次混合检索；不要把整个历史目录塞进上下文：

```bash
python3 ~/.claude/skills/hubmemory/scripts/context.py "<用户当前任务>" \
  --cwd "$PWD" --limit 8
```

该命令综合检索长期记忆、结构化 evidence、日报/月报和原始 session 事件，并优先当前项目。没有命中时继续正常工作，不要为了检索反复扩大上下文。

---

## 多 agent 协作上下文读取策略

读取分两层，按需升级，避免不必要的 token 消耗。

**第一层（必读，~3,000 tokens）：建立"谁在做什么"的认知**

```bash
# 1. 活跃会话概览（~400 tokens）
cat ~/hubmemory/live/index.json

# 2. 活跃 agent 的 live 快照（每个约 1,500 tokens，只读 status=active 的）
cat ~/hubmemory/live/<agent>_<session_id>.md
```

读完第一层你应该能回答：哪个 agent 在活跃、在哪个 cwd 工作、最近做了什么、任务是否还在进行。

**第二层（按需读，~5,000-8,000 tokens）：理解具体决策背景**

只在以下情况升级到第二层：
- 用户问"上次怎么决定的"、"之前做了什么"
- 你需要接续另一个 agent 的工作而不是并行执行
- 两个 agent 的工作范围可能重叠

```bash
# 最近 6 小时跨 agent 活动摘要
python3 ~/.claude/skills/hubmemory/scripts/recent.py --hours 6

# 或直接查今天的对话归档（按会话分组，带时间戳）
# 文件：~/hubmemory/daily/YYYY-MM-DD/conversations.md
```

**第三层（主动写入）：声明当前任务状态，实现真正协调**

开始处理重要任务时，主动更新 `~/hubmemory/live/tasks.json`，让其他 agent 知道你在做什么、是否需要配合：

```bash
python3 ~/.claude/skills/hubmemory/scripts/task_update.py \
  --agent claude \
  --session <session_id> \
  --task "正在重构 documents router" \
  --status active \
  --cwd /path/to/project
```

任务完成时更新 `--status done`。这是多 agent 协调的关键——没有这一步，其他 agent 只能被动感知，无法主动协调。

---

## 回答用户时的行动规则

用户问到以下场景时，**必须先执行对应命令，再基于输出回答**，禁止凭上下文记忆猜测：

**"最近/今天/昨天/这几天在干什么"**

时间参数选择：
- 当天问"今天"或"最近" → `--hours 24`
- 问"昨天" → `--days 2`（跨越昨天全天）
- 问"这几天/最近 N 天" → `--days N`

```bash
python3 ~/.claude/skills/hubmemory/scripts/recent.py --hours 24
# 只看某个项目：加 --cwd /path/to/project
# 增加展示提问数：加 --topN 5
```

输出已按工作目录聚合，包含 agent 占比、工具调用统计、代表性用户提问，直接据此回答。

**"上次/之前提到过 xxx"**

```bash
python3 ~/.claude/skills/hubmemory/scripts/query.py <关键词> --days 7
```

**"另一个 agent 在干嘛" / "Codex/Claude 现在在做什么"**

读文件（比 bash cat 更通用）：`~/hubmemory/live/index.json`

如需查看某个会话详情：`~/hubmemory/live/<session_id>.md`（最近 20 轮对话）

---

## 记忆目录（AI 需要主动读写的路径）

```
~/hubmemory/
├── profile/
│   ├── constraints.md     # 用户对 AI 的约束/禁令（每条带时间戳）← 每次必读
│   └── preferences.md     # 用户偏好/风格
├── live/
│   ├── index.json         # 今日会话及跨日仍活跃/处于宽限期的实时概览
│   └── <session_id>.md    # 单会话最近 20 轮对话快照
├── daily/YYYY-MM-DD/
│   ├── sessions.jsonl     # 当天完整会话事件流
│   ├── evidence.jsonl      # 从事实事件派生的结构化工作证据，可重建
│   ├── diary.md           # 当天工作日记（凌晨 3:07 自动生成）
│   └── contrib.json       # 各 agent token 贡献比例
├── memory/
│   └── memories.jsonl      # AI 确认后的长期记忆，必须带来源和状态
└── monthly/YYYY-MM.md     # 月度总结（月末自动生成）
```

---

## 脚本速查（所有脚本在 `~/.claude/skills/hubmemory/scripts/`）

| 脚本 | 常用参数 | 用途 |
|---|---|---|
| `bootstrap.sh` | 无 | 幂等启动（装依赖 + 拉起 watcher + 装 launchd） |
| `status.py` | 无 | watcher 状态 + 任务声明 + live 会话 + 最近约束 |
| `recent.py` | `--hours N` / `--days N` / `--cwd path` / `--topN K` | 最近活动摘要（主要查询入口） |
| `query.py` | `<关键词>` `--days N` `--limit K` `--cwd path` `--agent name` | 混合检索长期记忆、evidence、报告、session |
| `context.py` | `[当前任务]` `--cwd path` `--limit K` | 输出当前 agent 应读取的相关记忆和待处理报告 |
| `remember.py` | `add --json` 或 `add --title ... --content ...` | 校验并追加带证据来源的长期记忆 |
| `task_update.py` | `--agent <name> --task <描述> --status active\|done\|blocked` | 主动声明当前任务状态（多 agent 协调频道） |
| `task_update.py` | `--list` | 列出所有 agent 的当前任务声明 |
| `daily_report.py` | `--date YYYY-MM-DD`（默认昨天） | 手动触发日报 |
| `monthly_report.py` | `--month YYYY-MM` | 手动触发月度总结 |
| `report_due.py` | `list` / `claim` / `complete` | 跨 agent 领取和完成待汇总报告 |

watcher 管理：
- 停止：`launchctl unload ~/Library/LaunchAgents/com.hubmemory.watcher.plist`
- 重启：`launchctl load -w ~/Library/LaunchAgents/com.hubmemory.watcher.plist`
- 日志：`~/hubmemory/logs/watcher.log`

`status.py` 的 `watcher health` 必须为 `healthy`。PID 存活但心跳过期属于假活；`bootstrap.sh` 会自动重启。watcher 同时使用文件事件和周期增量扫描，默认每 15 秒补采一次漏报事件。

`live` 不是历史归档：当天会话全部保留；跨日 stale 会话超过 `retention.live_after_idle_hours`、确认已进入 `daily` 且没有未完成任务声明后，会自动从实时索引和 Markdown 快照中清理。清理不影响事实事件、报告、长期记忆或采集偏移；旧 session 再次产生事件时会自动回到 `live`。

---

## 系统说明

**监听范围**：`~/.claude/projects/**/*.jsonl`（Claude Code）、`~/.codex/sessions/**/*.jsonl`（Codex CLI / Desktop / VSCode 插件）、QClaw 的 memory/session 快照。OpenClaw SQLite 尚未接入，不应仅通过配置监听目录来宣称支持。

**Agent 标签**：`claude` / `codex-desktop` / `codex-vscode` / `codex`（纯 CLI）

**贡献比例**：`tokens_out(agent) / Σ tokens_out(同 cwd + 同天所有 agent)`

**日报生成**（零模型依赖）：
- 定时脚本只负责生成结构化草稿并创建待处理任务，不绑定任何固定模型。
- 下一次用户主动使用的 agent 会自动领取 pending 日报/月报，用当前 agent 的模型完成语义汇总。
- 没有新的 agent 会话时，任务保留在队列中，不丢数据；本地 Qwen 不是运行依赖。

**记忆检索与长期记忆**：
- `sessions.jsonl` 是唯一事实源；`evidence.jsonl`、日报和长期记忆都是可重建派生物。
- 当前默认检索是零依赖混合检索：词项相关性 + 短语命中 + agent/cwd 元数据 + 来源质量；以后可在同一记录格式上增加 embedding，不改变调用方。
- AI 只有在证据充分时才把稳定事实写入 `memory/memories.jsonl`，必须带 `source_session_ids`、`source_event_ids`、`confidence`、`status` 和 `supersedes`；不把一次性任务和未经验证的猜测写成长期记忆。
- 检索结果只注入前 N 条；不要每次工具调用都注入，避免上下文和 token 被历史淹没。

**排障**：先看 `~/hubmemory/logs/watcher.log`，数据结构详见 [references/data_schema.md](~/.claude/skills/hubmemory/references/data_schema.md)
