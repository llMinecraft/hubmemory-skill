# hubmemory 优化交付与验证结果

2026-10-18，本机已生效，未提交 Git/推送远端，未重启 GUI 或 watcher。

## 改动

- `scripts/retrieval_intent.py`：纯标准库意图解析，拆分原话主题、时间、其他会话和产物引用，记录移除的口语片段。
- `scripts/hybrid_query.py`：中文双字主题门槛、分钟级衰减（同时调节证据权重）、有意义的当前项目结果优先、跨项目回退、计划/交付/提问回声区分、路径抽取和精确 JSONL 行号。历史截止点排他并锚定历史时钟，无效时间报错；排除会话只接受完整 ID 精确匹配。
- `scripts/context.py`：人读/JSON 预算、完整来源、主题充分度、不把分数当置信度、来源产物绝对路径和未核验提示。
- `scripts/query.py`：兼容完整记录查询，补精确来源/产物和历史参数。
- `scripts/session_start.py`、`SKILL.md`、`README.md` 和本机 DSH/Codex 全局指令：统一入口、扩大 Skill 发现描述、每轮先查共享记忆规则。
- 新增 `scripts/test_retrieval.py`、`scripts/acceptance_retrieval.py`；完整方案见 `optimization-plan.md`。

## 实测

| 项目 | 结果 |
|---|---|
| 新增隔离检索回归 | 32/32 PASS |
| 原有 smoke 检查 | 11/11 PASS |
| 原有 watcher 回归 | 13/13 PASS；原测试有 TemporaryDirectory ResourceWarning，不影响通过 |
| doctor | 7/7 PASS，watcher PID 43948 正常，队列和 JSONL 正常 |
| diff whitespace | PASS |
| 真实历史原话/短问/主题问 | 3/3 Top1 正确且 sufficient |

历史参数：cwd `/Users/chakeyi/space/yuzu`，as-of `2026-10-18T19:01:39+08:00`。三条查询：

1. 你看一下刚才写的项目立项书
2. 刚才的立项书
3. 项目立项书

均命中已交付来源 `session-e98a601a-9496-4821-9366-0d55e4a073e6`，归档 `daily/2026-10-18/sessions.jsonl` 第 1102 行，event `snapshot:bd61acb16bb45ae052d8ae60dba01119ba956c062540267f3b75a3180e67bc4a`；产物为该来源 cwd 下的 `docs/SYNTHESIS_CHAMBER_CHARTER.md`。未使用提问后的事件或当日无精确时间派生物。此为历史证据重建，不是当时 watcher 状态快照。

最终一次三查询测量耗时约 5.1 / 2.8 / 5.5 秒，输出估算 760 / 743 / 683 token（均 ≤800）。估算不是任意模型真实 tokenizer 的精确 token 数。当前每次扫描窗口，不宣称显著加速；未引入可能失效的持久缓存。

## 如何复验

```bash
cd ~/.agents/skills/hubmemory
/usr/bin/python3 scripts/test_retrieval.py
/usr/bin/python3 scripts/smoke_test.py
/usr/bin/python3 scripts/test_watcher.py
/usr/bin/python3 scripts/acceptance_retrieval.py --json
/usr/bin/python3 scripts/doctor.py
```

## 生效范围与验收边界

- Claude/Codex skill 链接共用本机这一份源码，CLI 下次调用立即生效。
- 本次会话已收到更新的 Skill catalog；全局 Agent 指令在新会话验收最可靠，不保证已有会话重读。
- **未安装强制运行时插件**。规则触发与检索能力是两件事：用新会话不提 hubmemory 进行真实行为验收，观察第一条定位工具。
- 来源 `delivered` 是消息声称，不能替代文件读取/独立测试；仅计划应 thin。没有主题的“刚才那个”不猜，转 live 或澄清。
- 未改原始会话事实、watcher 或业务文件。协调子任务曾误写一条临时长期记忆，Lead 已标 archived，保留审计但不再检索；其余长期记忆未动。
- 严格跨项目隐私隔离、完整语义消歧和任意模型 100% 主动调用不在本次已验证能力内。
