# 增量检索与决策状态（2026-10-26）

## 边界

- 原始 sessions.jsonl 与归档仍是证据来源，缓存不替代备份。无新模型调用、无第三方数据库服务。
- 第一阶段实现的是 SQLite 文档/词频增量缓存，不是向量库或新的召回算法。按天和全局源指纹失效，只重读和分词变化的桶；仍用完整 eligible corpus 的 BM25，保证原有排名可对照。
- 索引首次建立需要时间与磁盘；查询仍需读取缓存并计算语料统计，不承诺常数耗时。当天频繁变化会触发当天重建，不是逐条追加式 token 更新。
- 精确 path/line、冷热合并、截止时间、冲突标志与原扫描一致。损坏归档不读陈旧缓存；缓存不可用则回退源扫描并给出 mode。决策日志损坏则拒绝，不回退成“未撤回”。

## 使用

```bash
hub search '之前的连接池修复' --budget 800
hub search '之前的连接池修复' --no-index --budget 800
hub index status
hub index rebuild --days 30
hub index clear
```

也可 `HUBMEMORY_RETRIEVAL_INDEX=0` 禁用加速。缓存位于 `<hub>/state/retrieval_index.sqlite3`，文件0600；含私有历史的可重建副本，不能提交或外发。CLI clear只清缓存数据，不删除原始会话。缓存的tokens与原tokenizer一致；修改tokenizer时必须提高TOKENIZER_CACHE_VERSION。

## 决策状态（显式、有来源、可逆）

先用 remember.py 建立带来源的长期记忆，取其完整 memory id，再记录明确决定：

```bash
hub decision mark --memory <memory_id> --decision rejected \
  --reason '实际验证后决定撤回，具体原因' \
  --source-session <完整来源session> --source-event <精确来源event>

hub decision mark --memory <old_id> --decision superseded \
  --replacement <new_id> --reason '由新方案替代' --source-event <来源event>

hub decision mark --memory <memory_id> --decision accepted \
  --reason '新证据支持重新采用' --source-event <来源event>

hub decision list --memory <memory_id> --json
hub decision list --as-of 2026-10-25T12:00:00+08:00 --json
```

- 状态记录追加到 memory/decision_events.jsonl；不改写 memories.jsonl，不删除旧结论。原因与至少一项来源ID必填；来源ID由操作者提供，存在性/语义并未经自动认证。
- 新记忆的supersedes同样参与替代关系解析。拒绝自身替代、未知/重复目标和替代环；读到环或冲突时标为不可推荐。
- 检索中旧记忆保留“历史撤回/已被替代”提示并排在可用结果之后。accepted不额外加分；单独命中被撤回方案不能被标为充分答案。
- as_of是排他截止点：撤回事件和替代记忆的创建时间均不得穿越历史。追加事件可以恢复accepted，但若结构性替代关系仍存在会提示冲突，不假装自动消失。
- 已有legacy status=stale/archived/superseded的过滤行为保留；没有自动迁移全部历史决定。
- 可选session/event定位只作精确匹配，标为unverified，不代表验证该条事实。不会因为文本相似就给其他会话贴状态。

## 验收与已知限制

真实历史3个固定查询（本机单次样例，不是统计性能保证）：

| 查询 | 原扫描秒 | 热缓存秒 | 前8项分数/引用 |
|---|---:|---:|---|
| 你看一下刚才写的项目立项书 | 10.3051 | 6.3956 | 完全一致 |
| 刚才的立项书 | 6.2701 | 2.4322 | 完全一致 |
| 项目立项书 | 7.9900 | 7.2359 | 完全一致 |

该窗口约8.8万文档，35个桶中34命中、1个当天桶随实时采集变化而重建。首次更新测得15.1664秒，慢于源扫描；缓存约186MB（会随语料增长），不是零空间代价。其收益主要来自避免重复解析/分词，BM25仍遍历文档，长查询不保证同等加速。默认启用，可随时--no-index对照或禁用。缓存仅做加速，不改变搜索结果的真实性边界。

缓存回归24项覆盖冷热、历史窗口、追加/改写/删除、归档及索引变化、并发、权限、读取中源变动与损坏回退；决策日志回归21项覆盖严格事件格式与损坏记忆/日志拒绝；端到端回归验证撤回提示、同名不同ID不误去重、历史截止、扫描/缓存一致。全部单元回归合计235项通过，smoke 24项、doctor 7/7、真实检索验收3/3通过。
