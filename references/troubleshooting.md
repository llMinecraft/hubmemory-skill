# hubmemory 排障手册

## Bootstrap 完成后 status.py 说 watcher 未运行

1. 看 `~/hubmemory/logs/watcher.log` 最后 50 行
2. 看 `launchctl print gui/$UID/com.hubmemory.watcher` 输出的 `state` 和 `last exit code`
3. 常见原因：
   - `ModuleNotFoundError: watchdog` → 检查 `python3 -c "import watchdog"`，必要时手动 `pip install --user watchdog pyyaml requests`
   - plist `ProgramArguments` 里的 python 路径不存在 → 编辑 plist 或重新跑 `install_launchd.sh`
   - 权限问题 → `chmod +x ~/.claude/skills/hubmemory/scripts/*.py`

## live/ 里没出现新会话

1. 确认监听源存在：
   ```bash
   ls ~/.claude/projects/  # 有 slugified cwd 目录？
   ls ~/.codex/sessions/YYYY/MM/DD/  # 有 rollout 文件？
   ```
2. 检查 offsets.json：
   ```bash
   cat ~/hubmemory/state/offsets.json | jq
   ```
   如果对应文件的 `byte_offset` 短暂落后于实际文件大小，周期增量扫描应在默认 15 秒内补齐；持续落后说明补采线程异常。
3. 运行 `status.py`。`watcher health` 必须为 `healthy`；PID 存活但心跳过期属于假活，重新运行 `bootstrap.sh` 会自动恢复。
4. 用 `fs_usage` 或 `lsof` 确认 watcher 进程确实在监听目录：
   ```bash
   sudo fs_usage -w -f pathname $(cat ~/hubmemory/state/watcher.pid)
   ```

## 断点续传出错（数据重复或漏行）

- 数据重复 → 说明 offset 没写盘就崩溃了。删除 `state/offsets.json` 里对应条目，让下次从头扫（会重复覆盖当天 sessions.jsonl，可先备份）
- 数据漏行 → inode 匹配但 byte_offset 提前了。这种一般是文件被截断（少见）。同上处理

## 日报没生成 / 生成失败

- 看 `~/hubmemory/logs/daily_report.log`
- LLM 不是必需依赖；定时脚本会生成结构化草稿并创建 pending 任务，下一次 agent 会自动接管并完善 diary。若明确要启用固定 API/Qwen 增强：
  ```bash
  cd ~/space/yuzu
  backend/.venv-llm/bin/mlx_lm.server --model mlx-community/Qwen3.8-27B-4bit --port 8080 --host 127.0.0.1
  ```
- 手动重跑：
  ```bash
  python3 ~/.claude/skills/hubmemory/scripts/daily_report.py --date 2026-09-21
  ```

## Launchd 加载失败

```bash
launchctl load -w ~/Library/LaunchAgents/com.hubmemory.watcher.plist
# 报错：Bootstrap failed: 5: Input/output error
```

原因：plist 可能已 load，需要先 unload。或者 SIP 关闭状态。

```bash
launchctl unload ~/Library/LaunchAgents/com.hubmemory.watcher.plist 2>/dev/null
launchctl load -w ~/Library/LaunchAgents/com.hubmemory.watcher.plist
launchctl list | grep hubmemory
```

## 停 / 重启 / 卸载

```bash
# 停
launchctl unload ~/Library/LaunchAgents/com.hubmemory.watcher.plist
launchctl unload ~/Library/LaunchAgents/com.hubmemory.dailyreport.plist

# 彻底卸载
rm ~/Library/LaunchAgents/com.hubmemory.*.plist
rm -rf ~/hubmemory  # 数据也删（慎用）
```

## 数据敏感性

- `profile/constraints.md` 和 `profile/preferences.md` 可能含用户偏好/工作习惯，建议不要提交到公开仓库
- 若需跨机器同步，用 git-crypt 或私有仓库

## OpenClaw 为什么没有出现在 live/index.json

当前 watcher 没有解析 OpenClaw SQLite。不要把 `~/.openclaw/state` 加进 `listen` 伪装成已接入；需要先基于稳定的会话/消息表契约实现增量游标和去重。
