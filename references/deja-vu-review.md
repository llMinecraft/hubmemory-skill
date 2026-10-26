# deja-vu 学习与取舍（2026-10-26）

## 审阅基线与方法

- 上游 https://github.com/vshulcz/deja-vu ，固定 revision `808a317b370717f919e7118f59fe9d0c84e5b7a5`。
- 阅读实现与测试，未安装上游插件、未运行其自动安装/同步/模型功能，也未执行上游测试。MIT 许可；本次独立实现机制，不移植代码。
- 不把 README 的 97.2% recall、节省一半 tokens 等声明当成 hubmemory 实测；测试存在不等于已经通过。

## 值得借鉴

| 机制 | 上游实现证据 | 对 hubmemory 的适配 |
|---|---|---|
| 分级、按需展开上下文 | `internal/search/search.go` 的 printContextBody / printContextChunks，context_cut_marked_test.go | 搜索小卡片 → hub read 精确 session/event 的问题及相邻回答 → 原始 ref。显示省略事件数、正文裁剪情况；不把全部历史注入 |
| 隐私是数据流约束 | `internal/redact/redact.go`；redaction_audit_test.go 扫描所有生成物 | 在预算检索摘要和新 L2 输出层遮蔽常见凭据，先脱敏再裁剪；原始 JSONL 不变。不是对未知秘密格式的 DLP 保证 |
| 承认有限覆盖 | `internal/search/pool.go` Pool/Unread，search_rungs.go 显式宽松检索阶梯 | 保留 thin/sufficient 的主题区分，新增有界展开的 shown/omitted；管理队列最多展示3项，其余仅总数，不挤占证据 |
| 保留副本归属及来源 | `internal/index/session_copies.go` settleCopies；ingest.go dropRepeatedMessages/orphanedSessions | 保持现有冷热合并、事件ID+内容去重与冲突保留；不按相似文案删源记录。多会话继承回声作为后续专项 |
| 标识废弃决定与代码变化 | README promote、代码变化提示 | 我们已有 memory status/supersedes，但决策失效的闭环仍需专项设计；不能凭时间或mtime自动把旧判断判错 |

## 不照搬的部分

- 不引入 Go 服务、embedding、云模型或第二套事实存储；当前需求不需要这些运行依赖。
- 不把上游索引当作无损归档。indexedText 会去包装、NFC、脱敏与裁剪，不能替代原始会话。
- 不采用策略配置读取/解析失败后默认放行的方式（policy.Load）；自动注入与归档校验必须明确拒绝不完整或不可信结果。
- 不把大量 harness adapter 数量当作质量指标；格式变化需要针对性 fixture 和端到端验证。
- 不照搬每次编辑/失败都自动注入的触发频率，避免上下文反复膨胀；保持当前任务先检索、按需展开。
- 上游 candidate pooling 限制的是候选会话，不是严格内存/时间上限；我们的 hub read 也明确只有输出预算，单日输入仍可能较大。

## 本次实现

1. `context.py` 将待报告队列压缩为3项和真实总数，不更改任务状态；完整队列仍用 report_due.py list。
2. `privacy.py` 独立实现常见 provider key、Bearer、凭据赋值、URL密码、私钥块的输出遮蔽；适用于 budget search 的摘要、查询元数据及 hub read 正文。旧 query 全记录入口和磁盘原始档案不在此次安全输出覆盖内；精确路径/session/event不伪造或改写。
3. `recall_read.py` / `hub read`：日期+完整会话ID+可选精确事件ID；默认1000估算token，保留锚点和前置问题、相邻回答；冷热合并；歧义事件/损坏归档拒绝；显示省略与裁剪。只读，不新增旁车，不调模型。
4. 隔离 fixture 测试覆盖先脱敏后裁剪、幂等、源对象不变、65+待办不挤占证据、锚点与问题、预算、歧义ID拒绝。

```bash
hub search '之前的连接池修复' --budget 800
hub read --date YYYY-MM-DD --session <完整ID> --event <精确event_id> --budget 1000
```

## 本次验收

- 检索回归34、新增安全与渐进读取6、日报消费者15、冷存储29、报告22通过；smoke 24项无失败，doctor 7/7。真实历史检索验收3/3通过。
- 真实单日样例共289条会话事件；1000估算token预算下展示问题与回答2条、省略287条，输出约438估算token。正文裁剪明确标记，原始文件哈希未变化。此为一个样例，不是整体命中率/耗时基准。
- 当前真实管理队列65项，预算检索只列3项并注明其余62项，未改动任何请求状态。

## 后续优先级（未声称已完成）

- 增量、可重建的本地词项索引，避免每次扫描一个月；先量化延迟、读字节及命中变化再引入。
- 当前检索的前240字去重键存在前缀碰撞及跨文件继承回声风险；不能照抄按文本全局合并而丢失不同时间/来源归属，需先补冲突与多源引用测试。
- 调整 unverified evidence、memory confidence 和 conflict_of 的检索信任表达，先保证真实历史验收不退化；同义词扩展不应稀释原查询的主题覆盖。
- 决策 accepted/rejected/superseded 的人工证据闭环，召回时提示“曾尝试后撤回”，避免重复踩坑。
- 引用文件内容hash快照检测变化；不把当前文件存在等同于历史结论仍有效。
- parser 格式漂移fixture、丢弃率与采集健康告警。涉及当前DSH隐私封装的内容必须以实际运行时数据核验，不能只根据工具显示的空字符串推断原文件为空。
