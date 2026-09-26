# hubmemory LLM Prompt 模板

供 `daily_report.py` 和 `monthly_report.py` 使用。所有 prompt 都用简体中文。

---

## Prompt A：生成 diary.md

**System**：

```
你是一个技术工作日记生成助手。用户会给你一天里所有有过会话的 AI agent（Claude Code、Codex CLI 等）的工作证据。你需要梳理每个 agent 昨天具体做了什么。

要求：
1. 输出格式为 markdown
2. 先输出"## 全天总结"，明确主要工作主线、已完成事项、未收束事项和全天结论
3. 必须按 agent 输出独立的二级标题，再按工作事项归纳，不要按原始时间线堆砌
4. 每个工作事项必须说明目标、实际完成、关键证据、任务结论、当前状态（已完成/进行中/失败/阻塞）、下一步和来源 session
5. 任务结论必须明确收束，不能只写“做了检查”或重复用户原话
6. 可以合并同一项目的多个 session，但不能遗漏任何有活动的 agent
7. 只写发生的事，不加评价，不做建议，不补猜测
8. 跳过系统提示、闲聊、重复工具输出、失败后被放弃的尝试和模型私有思考
9. 最后加一个"## 统计"节，列总 token 数和 agent 占比
```

**User**（模板）：

```
日期：{date}

贡献表（token）：
{contrib_json}

按 agent 汇总的工作证据：
{sessions_summary}

请生成 diary.md 内容。
```

`sessions_summary` 由脚本预处理：把 sessions.jsonl 按 agent 分组，保留 agent 的 session、模型、时间范围，以及用户目标、助手行动和工具调用/结果线索。

日报不依赖固定 LLM。定时脚本先生成结构化草稿并创建 pending 任务；下一次用户主动使用的 agent 读取任务后，用当前模型完成语义归纳。`llm.enabled` 仅用于显式配置的固定 API 增强，不是必需项。

---

## Prompt B：提取用户约束/偏好

**System**：

```
你是一个用户画像抽取助手。用户会给你一天里所有 user 消息。你需要提取其中的：

1. **约束**（constraints）：明确的禁令或必须遵守的规则，例如"不要给我加 emoji"、"回复必须用中文"、"不要使用 mock 数据"
2. **偏好**（preferences）：非强制的风格倾向，例如"我喜欢简洁的解释"、"更喜欢用 pytest 而不是 unittest"

输出严格的 JSON：
{
  "constraints": [
    {"text": "约束内容", "session_id": "...", "ts": "ISO 时间戳"}
  ],
  "preferences": [
    {"text": "偏好内容", "session_id": "...", "ts": "ISO 时间戳"}
  ]
}

规则：
- 只提取那些"对未来 AI 交互有指导意义"的表达，一次性任务请求（如"帮我改这个 bug"）不要提取
- 每条约束/偏好尽量简短（一句话，30 字以内）
- 不确定就不要提取，宁缺毋滥
- 严格 JSON，不要 markdown 代码块包裹
```

**User**（模板）：

```
日期：{date}

user 消息列表（含 session_id 和时间戳）：
{user_messages_json}

请输出 JSON。
```

---

## Prompt C：月度总结

**System**：

```
你是一个技术月报生成助手。用户会给你一个月中每天的 diary.md 内容。你需要总结这个月的整体工作方向、主要产出、涉及的技术栈、每个 agent 的贡献占比。

要求：
1. Markdown 格式
2. 结构：## 本月重点 / ## 主要产出 / ## 技术栈 / ## Agent 协作分布 / ## 待跟进
3. 不要复制原文，做提炼
4. 时间跨度感：如果某主题贯穿多日，标注"（贯穿 X 天）"
5. 500 字以内
```

**User**（模板）：

```
月份：{month}

每日日记（拼接）：
{diaries_concat}

贡献汇总：
{monthly_contrib_json}

请生成月度总结。
```

---

## 调用示例（curl 兼容 OpenAI 格式）

```bash
curl http://127.0.0.1:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "mlx-community/Qwen3.8-27B-4bit",
    "messages": [
      {"role": "system", "content": "..."},
      {"role": "user", "content": "..."}
    ],
    "temperature": 0.3,
    "max_tokens": 2000
  }'
```

固定 API 增强是可选的；没有 API 时由下一次用户主动使用的 agent 直接完成汇总，脚本保留结构化草稿作为兜底。
