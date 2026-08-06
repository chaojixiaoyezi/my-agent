# Memory：初衷与权威边界

## 解决什么问题

Memory 不是“把聊天全部复制到一个文件”，而是把三类不同需求拆开：

- 原始经历可追溯：完整对话、工具结果、任务状态在各自事实源中保真。
- 值得长期记住的内容可审核：模型提炼先成为 Candidate，不会因为写入时间更晚或置信度更高就自动变成事实。
- 当前任务能安全召回：只把当前 owner、当前 scope 下的 active 正式记忆装入唯一 `<memory-context>` 数据信封。

最终目标是让 Agent 在跨轮、跨天、压缩和重启后仍能找回可靠信息，同时避免“原始档案、每日摘要、候选、正式事实、教训和人格”互相冒充。

## 一条主链

```text
ConversationStore / audit / tool artifact / task result
                    |
                    v
          MemoryCuratorService（有界读取）
                    |
          +---------+----------+
          |                    |
          v                    v
 memory/daily/*.jsonl   memory/candidates.jsonl
   经历摘要，非事实       唯一候选账本与状态机
                               |
                               v
                    MemoryPromotionService
                       /       |       \
                      v        v        v
              long_term   lessons/HOT  PersonaRepository
```

所有触发原因只进入同一个 Curator；所有候选只进入同一个 CandidateService；所有正式晋升只进入同一个 PromotionService。不存在 `learning_drafts`、task-local `memory_gate` 或 `ops` 候选旁路。

## 各层如何理解

- ConversationStore 是完整用户/AI 对话原文和稳定顺序的唯一权威。
- `audit/YYYY-MM-DD.jsonl` 是运行黑匣子与引用索引，不是完整对话副本。
- `tasks/.../work/blobs/tool_outputs/` 保存大工具输出正文；其他层只保存 preview、hash、size 与 ref。
- `memory/daily/YYYY-MM-DD.jsonl` 是 Curator 从经历中提炼的每日摘要，不是正式事实，也不默认整批注入 Prompt。
- `memory/candidates.jsonl` 是所有提议的唯一候选事实源；候选的时间和 confidence 都不具有事实权威。
- `memory/long_term/memory.jsonl` 是具体事实、事件与项目知识的唯一可召回正式正文源。
- `memory/lessons/*.md` 是正式可复用教训的唯一正文源；`routing/INDEX.md` 只是可重建导航。
- `memory-hot.md` 只放由正式 lesson 晋升的短规则和精确 lesson 引用。
- `memory.md` 只做短导航，不保存事实或大段教训。
- `USER.md`、`SOUL.md`、`AGENTS.md` 只由 PersonaRepository 管理；USER 需要用户明确表达，SOUL/AGENTS 还需要明确确认。

## 核心判断原则

1. 新时间不自动覆盖旧事实。同一 `subject_key + scope` 的不同内容必须提出精确 replace，或进入 `blocked_conflict`。
2. 不同 scope 可以并存。例如“个人电脑使用 macOS”与“公司服务器使用 Linux”是两条不冲突的正式事实。
3. 临时要求不是稳定偏好。“这次架构问题请详细解释”使用 temporary/session scope，不写 USER。
4. `model_inferred` 永远只是候选；confidence 只是排序/审核参考，不是证据。
5. `tool_verified` 必须回查 Tools 模块的正式 operation 记录；failed、timeout、cancelled、unknown effect 都不能作为成功证据。
6. 子代理只能提交 finding/lesson/evidence 候选，不能直接修改 owner 的正式 Memory 或 Persona。

## 明确不做

- 不把完整对话复制进 daily、Candidate 或 long-term。
- 不把工具大输出复制进任何记忆正文。
- 不让 SQLite、FTS、向量库或 global index 取代正式文件；这些索引都必须可重建。
- 不通过用户自然语言猜 scope、状态、工具是否成功或任务是否终态。
- 不靠 Prompt 约定让后台模型“自觉不越权”；宿主不给它工具循环，只接受严格 JSON Schema。
- 不因 Curator 失败阻断正常聊天或 Compact；失败写稳定错误码且不推进游标。

## 与短期上下文的关系

Compact 只负责当前上下文压缩和任务恢复。它可以在压缩前向 Curator 提交 `pre_compact` 请求，但摘要本身不能直接成为长期事实。原始 ConversationStore、audit、工具 artifact、任务状态和正式 Memory 继续分别保持权威。
