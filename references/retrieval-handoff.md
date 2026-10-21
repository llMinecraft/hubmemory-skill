# Integration review for retrieval engine owner — resolved

以上/下方为实施过程快照，**不代表当前缺陷清单**。2026-10-18 Lead 完成收尾：历史时钟锚定、无效截止时间校验、cwd 分阶段优先、时间乘性降权、带空格/锚点路径、否定完成语义。最终 32 项隔离回归和 3 项真实历史验收全通过。此文件保留作审查记录，不作为检索事实源。


Lead has NOT edited hybrid_query.py or retrieval_intent.py to avoid concurrent writes. Please finish, run tests and report; all context/docs/global rules/tests implemented by lead.

Current gates: `python3 scripts/test_retrieval.py` (29 tests) has 4 failures:
- current cwd delivery must outrank other project's exact wording (scope currently +3 only).
- invalid as_of must raise ValueError, not silently disable cutoff.
- as_of must anchor now/ref_date for recency + scan window: old 2025-01-03 cutoff + 昨天 should find 2025-01-02.
- Markdown <docs/Deploy Guide.md#L12> should extract path with spaces and strip anchor for file read.

Historical `python3 scripts/acceptance_retrieval.py`: original natural full query now PASS, short '刚才的立项书' and plain '项目立项书' still FAIL Top1 planning not delivered. Generic artifact query (立项书/文档/方案 etc) can infer artifact intent even absent verb 写的; don't hardcode exact charter.

context already maps strong_coverage/delivery to coverage/delivery_state, artifacts[].resolved to absolute path. No need change its contract.

Additional review: cutoff exact boundary exclusive fixed; `_within_cutoff` should not OR wall ref_date bypass cutoff. Do not return raw filler when cleaned query empty. Don't classify '尚未完成/未生成/not completed' delivered. Stage cwd priority for meaningful local topic evidence, with labeled cross-project fallback. Avoid unbounded scope preference for unrelated topic noise.

Please do NOT write temporary coordination into permanent memory; sibling did this once, lead archived it. This handoff is temporary engineering notes only.
