# Hubmemory 归档与报告加固（2026-10）

## 范围

加固阶段仅本地验证；后续日报 v3 改造获用户明确授权后统一提交推送。不破坏性迁移既有 6–9 月历史包。stdlib tarfile/gzip、hashlib、原子替换及现有 lockutil 实现，不引入数据库/向量服务依赖。

## 关键边界

- sessions 是唯一事实源；规则 evidence 状态为最新来源声称，semantic_verified=false，不等于独立验收。
- 日报由次日首个有任务 agent 领取并用当前模型语义汇总。规则脚本只留草稿/pending。租约以 agent+完整 session+随机 token 绑定；发布前验证来源快照未变化、正文/JSON结构、引用及计数。
- 自动化只能检查结构、引用和显式计数，不能证明 AI 的语义判断百分之百正确；验收仍应核对源事件与结论。
- 初版 v2 日报保留精简 JSON；后续用户要求低成本 v3 已取消新日报 JSON（见 daily-v3.md），AI 只写分层全天总结，其余程序装配。历史 v2 读取兼容，不为版本变更静默重写。
- profile 只自动合并规范化后相同文本，保留最新措辞/时间及全部来源。不同范围、否定和语义相近规则不以模糊相似度自动丢弃。
- 冷数据直接通过 tar!member#L 读取；查询不把全月恢复到 daily。冷热重叠保留完整事实和准确 locator，冲突不会假装不存在。
- 完整性校验失败必须显式报告并拒绝使用，不能把“读失败”解释为“没有历史”。旧包无逐文件清单时只说明包级校验/成员可读，不追认已删除的原目录逐字节等价。

## 历史队列迁移记录

2026-10-21 09:35:45–09:35:47（Asia/Shanghai）在中间版本全历史补漏策略部署期间，49 个旧月日期被自动登记为 pending。具体调用会话未能确认；所有请求对应冷归档，未覆盖事件或日报。保留请求以免丢失合法的历史待总结事项，不静默删除/标 done。当前启动入口按索引识别历史队列，先处理最近已闭合日期，展示历史积压数量，不再因旧报告缺少新契约而大规模回迁。

## 测试入口

```bash
python3 scripts/test_archive.py
python3 scripts/test_reports.py
python3 scripts/test_cold_storage.py
python3 scripts/test_profiles_evidence.py
python3 scripts/test_retrieval.py
python3 scripts/test_watcher.py
python3 scripts/smoke_test.py
python3 scripts/doctor.py
```

测试必须使用临时 HUBMEMORY_HOME。并发归档、打包中追加、覆盖恢复、软链越界、损坏成员、发布中断、冲突再归档、双会话领取、来源变更、空稿/假稿完成等负向场景属于必测项。

## 最终验收（2026-10-21）

- archive 42、reports 21、cold_storage 29、profiles/evidence 7、retrieval 32、watcher 13、smoke 21 项，合计 165 项通过；doctor 7/7；compileall 和 git diff --check 通过。
- 实际 watcher 已受控重载，health.storage_lock_version=1。原有服务运行路径/端口未变。
- 10-19 和 10-20 日报已由当前 AI 语义归纳，经 session/token claim→契约校验→publish 正式发布，completion_kind=semantic、contract_version=2。其余历史积压未冒充完成。
- 6–9 月原有四个压缩包共 217 个文件全部可读，包级 SHA256 与原记录一致。它们没有原始逐文件 manifest，per_file_verified=false；未改写旧包或追认源目录逐字节等价。
- 归档和报告发布共用锁已在运行 watcher 生效；冷热引用和历史 cutoff 回归通过。加固阶段未推送，随后与用户授权的日报 v3 一起发布。

## 已知限制

- Windows 共享锁退化为排他锁，正确性不变但并发度降低。
- 超过 128 MiB 的 JSONL 合并保守保留为冲突；不同内容的非 JSONL 文件不静默覆盖，保留热目录待处理。
- 月初 7 天缓冲、未完成日报/月报、冲突日可使 daily 暂时保留旧月。这是保数据例外，不强行清空目录。
- 逐文件可验证范围为普通文件内容及保留的 mode/mtime、归档内目录；完全空的日期目录不会单独打包。不是 ACL/xattr/全文件系统快照。
- 单个 tar/index/manifest 是原子发布，三文件并非一次原子操作；中间窗口读者可能拒绝读取，持久 journal 在恢复时提交或回滚，绝不将未知哈希不符当作成功。
