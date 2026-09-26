# hubmemory-skill

hubmemory 是一个面向本机多 Agent 的共享记忆 Skill。它监听 Claude Code、Codex CLI / Desktop 等工具产生的会话文件，把事件统一归档到 `~/hubmemory`，并提供实时状态、历史检索、日报、月报和长期记忆能力。

官方镜像（内容同步）：

- Gitee：<https://gitee.com/llMinecraft/hubmemory-skill>
- GitHub：<https://github.com/llMinecraft/hubmemory-skill>

## 主要能力

- 跨 Agent 会话采集与统一归档
- `live` 实时会话状态和多 Agent 任务声明
- `daily` 事实事件、结构化证据和工作日报
- 零模型依赖的混合检索；本地 LLM 仅作为可选增强
- 带来源证据的长期记忆
- 文件事件监听与周期增量补采
- 已归档 stale 实时会话的安全淘汰

## 平台与依赖

- macOS（后台服务当前使用 `launchd`）
- Python 3.9+
- Python 包：`watchdog`、`PyYAML`

`bootstrap.sh` 会检查依赖并在缺失时通过 `pip --user` 安装。

## 安装

### 推荐：一份源码供 Claude 和 Codex 共用

```bash
mkdir -p ~/.local/share
git clone https://gitee.com/llMinecraft/hubmemory-skill.git \
  ~/.local/share/hubmemory-skill
bash ~/.local/share/hubmemory-skill/scripts/install_skill_links.sh
bash ~/.local/share/hubmemory-skill/scripts/bootstrap.sh
```

也可以从 GitHub 克隆：

```bash
git clone https://github.com/llMinecraft/hubmemory-skill.git \
  ~/.local/share/hubmemory-skill
```

安装器会创建：

```text
~/.claude/skills/hubmemory -> ~/.local/share/hubmemory-skill
~/.codex/skills/hubmemory  -> ~/.local/share/hubmemory-skill
```

如果目标已经存在，安装器不会覆盖内容，而是先移动成同级的时间戳备份，例如 `hubmemory.backup-20260926-103000`。重复执行是幂等的。

可以先预览操作：

```bash
bash ~/.local/share/hubmemory-skill/scripts/install_skill_links.sh --dry-run
```

也可以只安装某个 Agent 的入口：

```bash
bash scripts/install_skill_links.sh --target claude
bash scripts/install_skill_links.sh --target codex
```

如果仓库已经克隆在 `~/.claude/skills/hubmemory`，无需搬迁；直接在仓库内运行 `bash scripts/install_skill_links.sh`，安装器会保留 Claude 原目录，并为 Codex 创建整目录软链接。

## 接入 Agent 指令

在全局 `AGENTS.md` 或等效指令中加入：

```markdown
本机部署了 hubmemory。新会话开始时：

1. 执行 `bash ~/.claude/skills/hubmemory/scripts/bootstrap.sh`
2. 查阅 `~/hubmemory/live/index.json`
3. 遵守 `~/hubmemory/profile/constraints.md`
4. 用户提到“上次”“昨天”“之前”时，使用
   `python3 ~/.claude/skills/hubmemory/scripts/query.py <关键词>` 检索历史
```

完整调用规范见 [SKILL.md](SKILL.md)。

## 常用命令

```bash
# 系统状态
python3 ~/.claude/skills/hubmemory/scripts/status.py

# 最近 24 小时活动
python3 ~/.claude/skills/hubmemory/scripts/recent.py --hours 24

# 历史混合检索
python3 ~/.claude/skills/hubmemory/scripts/query.py "关键词" --days 7

# 为当前任务提取相关上下文
python3 ~/.claude/skills/hubmemory/scripts/context.py "当前任务" --limit 8

# 运行 watcher 回归测试
python3 ~/.claude/skills/hubmemory/scripts/test_watcher.py
```

## 数据目录

运行数据默认写入 `~/hubmemory`，不写入 Skill 源码仓库：

```text
~/hubmemory/
├── live/       # 今日会话及仍活跃、处于宽限期的会话
├── daily/      # 按日保存的事实事件、证据和日报
├── monthly/    # 月度总结
├── memory/     # 带来源的长期记忆
├── profile/    # 用户约束和偏好
├── reports/    # 待处理报告任务
├── state/      # watcher 偏移、PID 和健康状态
└── logs/       # 运行日志
```

可通过环境变量覆盖路径：

```bash
export HUBMEMORY_SKILL_DIR=/path/to/hubmemory-skill
export HUBMEMORY_HOME=/path/to/hubmemory-data
```

## 配置

首次启动会创建 `~/hubmemory/config.yaml`。关键配置示例：

```yaml
retention:
  live_after_idle_hours: 24
watcher:
  reconcile_interval_seconds: 15
llm:
  enabled: false
```

`live_after_idle_hours` 是跨日 stale 会话的宽限期。只有已进入 `daily`、没有未完成任务保护且超过宽限期的会话，才会从实时索引和 Markdown 快照中清理。

## 隐私说明

- 会话正文、日报、长期记忆和日志保存在本机 `~/hubmemory`。
- 不要将 `~/hubmemory` 提交到 Git 仓库。
- 日报和检索默认不依赖云端模型。
- 如启用 LLM 增强，请自行确认所配置服务的数据处理和隐私策略。

## 更新

```bash
cd ~/.local/share/hubmemory-skill
git pull --ff-only
bash scripts/bootstrap.sh
```

## 卸载后台任务

```bash
launchctl unload ~/Library/LaunchAgents/com.hubmemory.watcher.plist
launchctl unload ~/Library/LaunchAgents/com.hubmemory.dailyreport.plist
```

卸载后台任务不会删除 `~/hubmemory` 中的历史数据。

## 开发验证

```bash
python3 -m py_compile scripts/*.py
python3 scripts/test_watcher.py
bash scripts/test_install_skill_links.sh
```

## 许可证

当前仓库暂未附带开源许可证。除非仓库后续明确添加许可证，否则保留全部权利。
