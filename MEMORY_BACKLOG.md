# Memory Backlog

这份文档专门记录记忆系统的痛点、去重后的问题分类和后续设计方向。

当前约定：
- 先讨论 `memory / 长期记忆 / 检索 / 记录 / 遗忘 / 压缩 / 召回`。
- 自学习先不开发，只预留接口和沉淀队列位置。
- 任何“记住了”的行为，未来都必须能找到落盘证据。

## 一句话结论

记忆最大的痛点不是“没有记忆”，而是记忆层级、召回、任务状态、lesson 抽象和未来 skill 沉淀之间没有稳定同步。

## 方向总纲

这是当前 memory 设计的总方向：

记忆系统最大的坑，不是“没有记”，而是“记的位置不对、入口不唯一、压缩前没落盘、恢复时没读权威文件、把摘要当事实、把任务进度塞进长期记忆”。

正确方向：
- `MEMORY` 只做导航。
- `daily memory` 记当天状态。
- `SESSIONS` 做恢复线索。
- 任务目录做事实源。
- `lessons` 存教训。
- `toolchains` 存工具降级。
- `skill` 存可复用流程。
- `raw conversation archive` 做全量冷备份，默认不进 prompt，只用于审计、搜索和灾难恢复。

## 新增倾向：长期规则只存索引，不常驻全文

小叶子判断：
- 各家 memory 往往会存长期规则，但长期规则如果一条条写进常驻 memory，用久以后一定越来越大。
- 长期规则越多，启动上下文越胖，模型越容易被旧规则、低频规则、相似规则污染。
- 更合理的方式是：常驻 memory 只写“去哪里看”，规则正文放到专门文件里。

建议结构：

| 层级 | 文件 | 内容 |
| --- | --- | --- |
| 顶层导航 | `MEMORY.md` | 只放 1-2 行入口，例如“任务规则看 memory/routing/tasks.md” |
| 路由索引 | `memory/routing/INDEX.md` 或按主题拆分 | 写触发词、适用场景、权威文件路径、过期/验证方式 |
| 正式规则 | `references/.../*.md` 或 `memory/rules/.../*.md` | 放完整长期规则、例子、边界和维护说明 |

索引条目建议字段：

| 字段 | 说明 |
| --- | --- |
| `route_id` | 稳定 ID，方便日志和测试引用 |
| `topic` | 这条规则管什么事 |
| `trigger_keywords` | 用户说到哪些词时应该读 |
| `aliases` | 同义词、中文/英文/缩写 |
| `when_to_read` | 什么情况下必须读 |
| `authority_path` | 正式规则文件路径 |
| `scope` | 适用范围，避免全局污染 |
| `priority` | hot / normal / rare |
| `owner` | 谁维护 |
| `stale_check` | 怎么判断过期 |
| `last_verified_at` | 最近验证日期 |

关键问题：
- 只靠 prompt 写“请按索引读文件”，AI 可能不遵守。
- 真正可靠的办法是让运行时参与：模型回答前，代码先做 memory route，找出候选索引和必须读取的正式文件。

解决方案：
- 做一个 deterministic memory router，不完全交给 LLM 自觉。
- 每轮输入先过关键词 / FTS / route matcher，命中后生成 `required_read_paths`。
- 高置信命中时自动读取索引和正式文件摘要，再交给 LLM。
- 中置信命中时把候选文件作为“需要核对”的上下文给 LLM，并要求它先读再答。
- 低置信命中只记录候选，不强制注入，避免噪声。
- 对正式任务、危险操作、工具边界、安全规则，走 strict 模式：命中但未读权威文件，就不能直接给最终结论。
- 每次读取都写 `memory_read_receipt`，记录 route_id、读了哪个文件、hash、耗时、是否命中。
- `memory doctor` 定期检查索引里的路径是否存在、触发词是否重复、正式文件是否过期。

配置建议：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `memory_rule_routing_enabled` | true | 是否启用长期规则路由 |
| `memory_rule_routing_mode` | `soft` | `off` / `soft` / `strict` |
| `memory_rule_auto_read_limit` | 3 | 单轮最多自动读取几个正式规则文件 |
| `memory_rule_index_prompt_lines` | 20 | 顶层注入索引摘要的最大行数 |
| `memory_rule_receipt_enabled` | true | 是否记录读规则证据 |

判断：
- 这个方案能最大化省 token，因为常驻层只有导航和索引摘要。
- 它也能大幅缓解“AI 不遵守”的问题，因为关键路由由代码执行，不是只靠模型自觉。
- 但它不能 100% 保证模型理解正确，所以还需要 receipt、doctor、测试用例和任务模式 strict gate。

## 新增倾向：全量会话冷归档

小叶子倾向：
- 除了工具具体调用后展示出来的大段数据之外，所有会话都尽量存下来。
- 硬盘不是主要瓶颈，一年几百 GB 也可以接受。
- 这层是兜底，不代表每次启动都读取。

设计含义：
- 需要有一个 append-only 的原始会话归档层。
- 原始归档不等于 HOT，也不等于 daily memory。
- 原始归档默认不注入 prompt，只通过搜索、过滤、引用路径进入任务恢复。
- 工具调用的大输出不要无脑塞全文，可以存元数据、摘要、路径、hash、截断预览和是否可重新读取。

每条会话/事件建议至少带这些字段：

| 字段 | 用途 |
| --- | --- |
| `event_id` | 唯一 ID，方便引用和去重 |
| `session_id` | 属于哪个会话 |
| `request_id` | 属于哪个 gateway / adapter 请求 |
| `run_id` | 属于哪个 subagent / runner |
| `speaker` | 谁说的：user / assistant / tool / system / subagent |
| `target` | 说给谁：主代理、子代理、用户、外部 adapter |
| `action` | 行为类型：chat / ask / tool_call / tool_result / dispatch / acceptance / memory_write |
| `created_at` | 发生时间 |
| `status` | queued / processing / ok / failed / skipped |
| `error_code` | 失败时的稳定错误码 |
| `is_dispatch` | 是否是派遣任务 |
| `task_id` | 关联任务或任务目录 |
| `tool_name` | 工具名，没有则为空 |
| `tool_call_id` | 单次工具调用 ID |
| `tool_success` | 工具是否成功 |
| `content_preview` | 可读摘要或截断预览 |
| `content_path` | 大正文或 artifact 的落盘路径 |
| `content_hash` | 正文 hash，用来确认没被改 |
| `visibility` | hot / daily / task / archive / private |
| `source` | 来源：cli / gateway / adapter / live_lab / subagent |

关键边界：
- 全量归档是“硬盘兜底”，不是“上下文兜底”。
- 恢复任务时先用索引定位，再读权威文件。
- 不能因为原始归档里有一句“完成了”，就跳过任务目录核验。
- 如果涉及隐私、密钥、cookie、token，归档层也必须脱敏或拒写。

## 多层记忆必须可配置

小叶子补充：
- 多层记忆可以做，但要留足开关。
- 用户应该能自己选择记哪些层、不记哪些层、记多久、是否进入 prompt、是否索引、是否保留原文。
- 普通用户不应该面对一大堆细碎开关。更适合提供一个“全量记忆等级”，高级用户再覆盖细节项。

设计原则：
- 默认安全保守，不替用户偷偷扩大记忆范围。
- 每一层都要能独立开关。
- 每一层都要能设置保留期、大小预算、是否索引、是否进入 prompt。
- 涉及隐私、密钥、cookie、token、个人敏感内容时，默认脱敏或拒写。
- 配置必须能解释清楚：关闭后会失去什么能力，打开后会多写哪些数据。
- 配置解析必须安全默认。用户乱填、不懂的字符串、乱码、注入字符、越界数字，都不能直接生效。
- 非法配置值按默认值或安全值处理，例如归档等级非法时回落到默认等级，数值非法时回落到 0 或配置默认值。
- 回落时要有 warning / doctor 提示，不能悄悄误用。

建议配置维度：

| 配置维度 | 说明 |
| --- | --- |
| `enabled` | 这一层是否启用 |
| `write_policy` | always / explicit / task_only / off |
| `read_policy` | always / routed / explicit / off |
| `prompt_policy` | hot_only / summary_only / never / routed |
| `retention_days` | 保留多少天；0 表示不自动删除 |
| `max_bytes` | 单层最大硬盘预算 |
| `index_enabled` | 是否写入 SQLite/FTS/未来向量索引 |
| `raw_content_enabled` | 是否保留原文 |
| `redaction_enabled` | 是否启用密钥/隐私脱敏 |
| `user_confirm_required` | 写入前是否要求用户确认 |

配置安全规则：

| 配置类型 | 非法输入例子 | 处理方式 |
| --- | --- | --- |
| 等级枚举 | `abcd`、乱码、`99` | 回落默认等级，例如 `memory_archive_level=3` |
| 天数 / 大小 | `abc`、负数、注入字符串 | 回落默认值；确实约定 0 表示不限制时才用 0 |
| bool | `maybe`、空对象、奇怪字符串 | 回落默认 bool |
| policy 枚举 | 非 allowed 值 | 回落默认 policy |
| 路径 | 绝对危险路径、控制字符、越界路径 | 拒绝或回落默认路径，不自动创建危险目录 |

注意：
- “按默认的 0 算”只适用于明确约定 `0` 是安全值的字段，比如 `max_days=0` 表示不限制。
- 如果某个字段的 `0` 会扩大权限或扩大记录范围，就不能回落到 0，必须回落到更保守默认值。
- 配置错误本身也应该可观察，后续 `memory doctor` / `local-doctor` 要能提示。

建议第一版配置开关：

| 层级 | 默认 | 原因 |
| --- | --- | --- |
| raw archive | off 或 explicit | 全量会话归档隐私和磁盘影响大，要用户明确打开 |
| index | on | 没有索引就很难查，但只存结构化摘要和路径 |
| HOT | on | 极少量长期硬规则，需要稳定入口 |
| daily | on | 跨会话恢复很依赖当天状态 |
| task memory | on | 任务事实源必须稳定 |
| lessons | explicit | 教训需要抽象，不能自动乱写 |
| preferences | explicit | 用户偏好敏感，应该用户明确表达 |
| references | on | 这是入口导航，不存用户隐私正文 |
| skill draft | off | 自学习暂不开发，只预留 |

未来实现时，需要在 `agent_config.yaml` 里写清楚中文注释，并在配置 dataclass 中给出默认值。

### 全量记忆等级

小叶子建议把“全量会话归档”做成 0-3 级。

这里的等级只控制 raw archive 的原始保留程度，不改变任务目录事实源、daily 状态、HOT 入口这些恢复必需层。

| 等级 | 名称 | 保存内容 | 去掉什么 | 目标 |
| --- | --- | --- | --- | --- |
| 0 | full | 尽量保存完整会话、assistant 回复、用户消息、工具调用元数据、工具结果预览和 artifact 路径 | 只脱敏密钥/隐私，不保存极大工具全文 | 最大审计和灾难恢复 |
| 1 | without-heavy-tool-output | 保存完整会话和工具元数据，但不保存大段工具结果正文 | 去掉大文件读取结果、长 HTTP 响应、长搜索结果全文，只保留摘要/路径/hash | 基本完整恢复，减少体积 |
| 2 | summary-plus-events | 保存用户/assistant 关键消息、事件元数据、工具调用摘要、结果状态 | 去掉普通长回复全文和大部分工具展示正文 | 用较小体积保留恢复线索 |
| 3 | recovery-minimal | 保存任务恢复必需字段、短摘要、状态变化、失败原因、引用路径 | 去掉非关键闲聊全文、长模型回复、工具输出正文 | 最小存储，但仍能最大化恢复任务 |

等级 3 的底线：
- 必须保留 `session_id / request_id / run_id / task_id` 等关联字段。
- 必须保留用户关键意图摘要。
- 必须保留任务状态变化。
- 必须保留工具调用名、成功失败、错误码、artifact 路径。
- 必须保留 daily/task/handoff 的引用路径。
- 不保证能还原完整聊天措辞，但要尽量支持“继续把事做完”。

建议默认：
- 普通用户默认 `3`，保护隐私和体积，但保留恢复能力。
- 开发者/调试模式默认 `1`。
- 用户明确选择“全量冷备份”时才用 `0`。
- `2` 作为折中选项，适合想保留较多上下文但不想存太多原文的人。

细节开关策略：
- 细节开关可以保留，但默认不生效或隐藏为高级配置。
- 普通配置只暴露 `memory_archive_level`。
- 高级配置才允许覆盖 `raw_content_enabled`、`max_bytes`、`retention_days`、`index_enabled` 等字段。
- 不管等级是多少，密钥、cookie、token 都不能明文写入。

## 压缩前 Hook 是必需能力

小叶子补充：
- 多轮上下文压缩之后，agent 仍然容易忘记。
- 必须有一个压缩前 hook：在上下文真正压缩之前，自动把这一轮会话先结构化落盘。
- 这份快照可以接近 `memory_archive_level=3`，也就是不追求保存所有原文，但必须保存恢复所需字段。
- 可以排除 system prompt 和内置 prompt，重点保存用户消息、assistant 关键回复、行动、工具调用元数据、状态变化、错误和引用路径。
- 如果之后 agent 胡言乱语、丢失会话或状态漂移，用户应该能直接让它“去找回状态”。
- hook 快照每天生成一个文件，但不和 daily memory 混在一起。
- hook 快照要有单独保留期配置，默认 7 天；硬盘足够的用户可以自行调大或不限制。

建议目录：

| 目录 | 用途 |
| --- | --- |
| `memory/daily/YYYY-MM-DD.md` | 当天状态摘要、决策、下一步 |
| `memory/hooks/YYYY-MM-DD.jsonl` | 压缩前 hook 结构化快照，一行一个 snapshot |
| `memory/raw/YYYY-MM-DD.jsonl` | 全量冷归档，按 archive level 控制内容 |
| `memory/index/` | 索引或索引元数据 |
| `memory/hot/` | HOT 入口和最高频铁律 |
| `memory/lessons/` | 抽象教训 |
| `memory/toolchains/` | 工具链和降级经验 |
| `memory/tasks/` | 任务恢复索引，不替代任务目录本身 |

关键原则：
- 所有记忆都按功能分开，不同功能的记忆不混放。
- daily memory 不存 hook 全量快照，只引用 hook 文件或写短摘要。
- hook 文件按天追加，适合快速按日期恢复。
- raw archive、hook、daily、lessons、toolchains、HOT 都是不同层，不能互相塞正文。
- 同一个功能的记忆放一块，入口清晰明确。

保留期配置建议：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `memory_hook_retention_days` | 7 | hook 快照保留天数 |
| `memory_hook_max_days` | 0 | 最大保留天数；0 表示不限制，由用户硬盘决定 |
| `memory_hook_enabled` | true | 是否启用压缩前 hook 快照 |
| `memory_hook_archive_level` | 3 | hook 默认按全量记忆等级 3 保存 |

压缩前 hook 至少要写入：

| 字段 | 用途 |
| --- | --- |
| `session_id` | 当前会话 |
| `compression_id` | 第几次压缩 / 本次压缩事件 |
| `turn_range` | 本次压缩覆盖哪些轮次 |
| `started_at` / `ended_at` | 时间范围 |
| `participants` | 用户、主代理、子代理、工具 |
| `user_intents` | 用户这段时间真正想做什么 |
| `assistant_actions` | agent 做了哪些动作 |
| `tool_calls` | 工具名、参数摘要、成功失败、错误码、artifact 路径 |
| `dispatch_events` | 是否派遣任务、派给谁、run_id、状态 |
| `task_refs` | 关联任务目录、STATUS、HANDOFF、ACCEPTANCE、TESTS 路径 |
| `decisions` | 本轮形成的重要决定 |
| `open_questions` | 还没解决的问题 |
| `next_actions` | 恢复后第一步应该做什么 |
| `token_usage` | 输入、输出、工具、累计、估算剩余 |
| `archive_level` | 本次按哪个归档等级保存 |
| `content_paths` | 大正文和快照文件路径 |

关键规则：
- 压缩前 hook 失败时，不能静默吞掉。
- 失败要写 LocalStore event，并在可见状态里提示用户。
- hook 写入完成后要 readback 验证。
- 压缩摘要不能替代 hook 快照；摘要只给模型继续推理，hook 快照用于恢复和审计。
- 多轮压缩后，恢复应该优先读最近的 compression snapshot，再读任务目录权威文件。

实时 token 可见性也是必需能力：
- 用户应该能看到当前会话 token 消耗。
- 至少展示：当前轮输入估算、输出估算、工具结果估算、累计估算、距离压缩阈值还有多少。
- 如果 token 估算不准，要标记为 estimate，不要假装精确。
- 中文、工具 schema、system prompt、messages、tool results 都要纳入估算思路。
- 未来 `status` / `chat` / Live Lab 里都应该能看到 token / context budget 状态。

## 压缩方式调研

这一节记录各家常见做法，方便后续选型。

### 1. 截断 / 滑动窗口

代表：
- LangGraph / LangChain 的 trim messages。
- OpenAI Realtime API 的 truncation。

做法：
- 到达 token 边界时，删掉最旧消息或只保留最后 N 条。
- 可以用 token 数触发，也可以保留某个比例的上下文。

优点：
- 简单、确定、便宜。
- 适合紧急止血，避免超过模型上下文。

缺点：
- 会直接丢信息。
- 对长任务恢复最危险，必须先有压缩前 hook。

我们怎么用：
- 只作为最后保险，不作为主要记忆策略。
- 必须先写 `memory/hooks/YYYY-MM-DD.jsonl`，再允许截断。

参考：
- LangGraph memory docs: https://docs.langchain.com/oss/javascript/langgraph/add-memory
- OpenAI Realtime truncation docs: https://developers.openai.com/api/reference/resources/realtime

### 2. 滚动摘要

代表：
- LangGraph 在 state 里同时保存 `messages` 和 `summary`。

做法：
- 当消息积累到一定量时，用模型生成/扩展 summary。
- 删除旧消息，只保留摘要和最近几轮。

优点：
- 比直接截断保留更多语义。
- 成本比全量上下文低。

缺点：
- 摘要会丢顺序、证据、状态细节。
- 多轮摘要会出现语义漂移，把“未完成”和“已完成”混在一起。

我们怎么用：
- 可以作为模型继续推理的短上下文。
- 不能作为事实源。
- 摘要必须引用 compression snapshot 和任务目录路径。

参考：
- LangGraph summarize messages: https://docs.langchain.com/oss/javascript/langgraph/add-memory

### 3. 自动 compact

代表：
- OpenAI Agents SDK 的 `OpenAIResponsesCompactionSession`。

做法：
- session 负责持久化历史。
- 历史变长后，通过 hook 判断是否触发 compact。
- compact 后清空底层 session，再写入压缩后的 item list。
- 可以自动触发，也可以手动在空闲时触发。

优点：
- 机制清楚，触发点可定制。
- 适合和 session 存储绑定。

缺点：
- compact 会改写 session，如果没有外部 raw archive，会丢原始现场。
- 重 compact 可能影响流式响应延迟。

我们怎么用：
- 借鉴“可插拔 session + shouldTrigger hook + 可手动 compact”。
- 但我们的 compact 前必须先写 raw/hook snapshot，不能只改写 session。

参考：
- OpenAI Agents SDK sessions: https://openai.github.io/openai-agents-js/guides/sessions/

### 4. 短期记忆溢出到长期记忆块

代表：
- LlamaIndex Memory。

做法：
- 短期 chat history 有 token budget。
- 超过比例后，把最旧的一批消息 flush 到长期 memory blocks。
- 长期 memory blocks 可以是静态信息、事实抽取、向量记忆。
- 每个 block 有 priority，超预算时按优先级截断。

优点：
- 分层清楚。
- 适合我们正在讨论的 HOT / daily / archive / vector 分层。
- priority 机制可以防止关键记忆被先删。

缺点：
- 事实抽取依赖模型，可能抽错或漏。
- 向量召回仍不是事实源。

我们怎么用：
- 借鉴 token ratio、flush size、memory blocks、priority。
- HOT 永远最高优先级；raw archive 不进 prompt；task refs 比普通摘要优先。

参考：
- LlamaIndex memory docs: https://developers.llamaindex.ai/python/framework/module_guides/deploying/agents/memory/

### 5. 两阶段提取和归并

代表：
- OpenAI Agents SDK sandbox memory。

做法：
- run 结束后先把 conversation segment 追加到会话文件。
- Phase 1：从 conversation file 生成 summary 和 raw memory extract；system、developer、reasoning 内容会被省略；太长时保留开头和结尾。
- Phase 2：consolidation agent 读取 raw memories，需要更多证据时再打开 conversation summaries，把模式沉淀到 `MEMORY.md` 和 `memory_summary.md`。

优点：
- 原始会话、摘要、可长期记忆分开。
- 先提取，再归并，适合把“聊天流水”变成“长期规则”。
- 支持不同 memory layout 隔离不同 agent。

缺点：
- 归并依赖模型质量。
- 如果 Phase 1 输入已经被截断，仍会丢中间细节。

我们怎么用：
- 非常适合我们：raw/archive -> hook/session summary -> daily/task/lessons。
- 但要避免自动写正式 skill；skill draft 仍需用户确认。

参考：
- OpenAI Agents SDK sandbox memory: https://openai.github.io/openai-agents-python/sandbox/memory/

### 6. 操作系统式分层记忆

代表：
- MemGPT / Letta 方向。

做法：
- 把上下文窗口看成快内存，把外部存储看成慢内存。
- agent 主动在不同记忆层之间搬运信息。
- 用类似 OS 的虚拟上下文管理，让有限窗口表现得像更大的记忆空间。

优点：
- 概念上适合长期 agent。
- 和我们 HOT / daily / task / raw / archive 的方向一致。

缺点：
- 实现复杂。
- 如果没有强事实源和审计，很容易“自己搬错记忆”。

我们怎么用：
- 采用分层思想，不一开始照搬复杂自主搬运。
- 第一版先做确定性 hook、索引、恢复和人工可见状态。

参考：
- MemGPT paper: https://arxiv.org/abs/2310.08560

### 7. 启动入口瘦身和按需规则

代表：
- Claude Code 的 `CLAUDE.md` / scoped rules / skills。

做法：
- 启动时加载短规则文件。
- 常驻规则要短，复杂流程放到 skills 或 path-scoped rules。
- 更具体的规则优先于更泛的规则。

优点：
- 防止启动上下文过胖。
- 和“MEMORY 只做导航”方向一致。

缺点：
- 这不是完整压缩方案，只解决启动入口和规则路由。

我们怎么用：
- `MEMORY` / HOT 只放入口和硬规则。
- 长流程进 skill draft / skill；详细规则进 references。

参考：
- Claude Code memory docs: https://code.claude.com/docs/en/memory

## 我们的压缩方案倾向

不要选单一方案，而是组合：

1. 实时 token/context budget
   - 每轮都能看到估算消耗。
   - 达到 warning 阈值先提醒，达到 hard 阈值才触发压缩。

2. 压缩前 hook
   - 必须先写 `memory/hooks/YYYY-MM-DD.jsonl`。
   - 按 `memory_hook_archive_level=3` 保存结构化恢复快照。
   - 写完 readback，失败则提示，不静默压缩。

3. 会话继续用滚动摘要
   - 摘要用于下一轮模型推理。
   - 摘要必须带 snapshot/task refs。
   - 摘要不是事实源。

4. 原始内容进冷归档
   - 按 `memory_archive_level` 控制原文保留程度。
   - 大工具输出只存路径、hash、摘要和可重读信息。

5. 后台提取和归并
   - 从 hook/raw/archive 里提取 daily/task/lesson 候选。
   - lessons 只进 lesson 层。
   - skill 只生成 draft，不自动生效。

6. 恢复时按权威顺序读
   - 最近 compression snapshot。
   - task STATUS / HANDOFF / ACCEPTANCE / TESTS。
   - daily memory。
   - HOT / MEMORY routes。
   - 必要时再搜 raw archive / RAG。

压缩模式建议：

| 模式 | 用途 | 是否默认 |
| --- | --- | --- |
| `off` | 不自动压缩，超限时报错或提示用户 | 否 |
| `truncate_after_snapshot` | 写快照后直接滑窗截断 | 应急 |
| `rolling_summary` | 写快照后生成/扩展摘要，保留最近几轮 | 默认 |
| `structured_summary` | 写快照后生成固定字段摘要 | 任务模式默认 |
| `extract_then_consolidate` | 后台把快照/归档提炼成 daily/lesson | 后台任务 |

第一版建议：
- 默认 `rolling_summary`。
- 正式任务开启 `structured_summary`。
- 禁止没有 snapshot 的 truncation。
- `off` 只给调试和极端隐私场景。

## 痛点去重

### 1. 说记住了，但没落盘

表现：
- 聊天里答应“记住了”，但没有写入任何持久文件。
- 重启、会话压缩、上下文切换后，这条信息就消失。

未来要求：
- 用户明确要求记住时，必须写入真实存储。
- 写完后必须验证文件存在、内容非空、路径正确。
- 回复用户时要说明写到了哪里，不能只说“记住了”。
- “写入成功”不等于“下次一定能想起”。还要有触发词、入口索引和权威路径。

### 2. 记忆层级混乱

表现：
- 临时上下文、长期偏好、任务状态、技术流水、lesson、skill 种子混在一起。
- `MEMORY.md` 或长期记忆文件越来越臃肿，真正该常驻的规则被淹没。

未来要求：
- 记忆写入时必须先分类。
- 不同层级有不同保存位置、生命周期和召回方式。
- 普通任务流水不能污染长期偏好。

建议层级：

| 层级 | 用途 | 例子 | 召回方式 |
| --- | --- | --- | --- |
| HOT | 必须常驻的偏好和硬规则 | 小叶子的长期偏好、不能忘的协作规则 | 默认注入或固定入口 |
| Daily | 当天流水和阶段记录 | 今天做了哪些事、临时决策 | 按日期读取 |
| Task | 某个任务的状态和证据 | STATUS、RUNLOG、HANDOFF、测试结果 | 跟任务 ID / 目录绑定 |
| Lesson | 可复用经验，但还不是 skill | 某类坑下次怎么避 | 检索 + 索引入口 |
| Archive | 旧历史和低频材料 | 过期上下文、历史 session | 默认不注入，只搜索 |
| Skill Draft | 未来自学习候选 | 可复用流程草稿 | 只预留，不自动启用 |

第二批补充：
- `MEMORY.md` 不能存正文，应该只做长期引用入口。
- 详细规则、任务规范、外部知识库、lesson、toolchain、skill 都应该有自己的目录和入口。
- 具体任务进度不能写进长期入口，避免未来每次启动都带入旧任务状态。

### 3. 长期记忆过多，启动上下文被污染

表现：
- 旧 lesson 和旧任务记录全塞长期文件。
- 启动变慢，上下文变长。
- 过期经验容易被当成当前事实。

未来要求：
- 长期常驻内容必须很少，只放高价值规则。
- lesson 需要抽象和过期策略。
- 历史记录默认进入 Archive / LocalStore，不直接进入 HOT。
- `memory-hot` 也不能变成第二个臃肿 `MEMORY.md`；HOT 只放最高频铁律和入口引用。

### 4. RAG 召回不稳定

表现：
- 明明记过，但语义搜索没有召回。
- 关键规则如果只靠 RAG，就会在关键时刻缺席。

未来要求：
- 关键规则不能只靠 RAG。
- HOT 层、INDEX 和引用入口必须存在。
- RAG 只能作为补充召回，不能承担硬规则入口。
- RAG 摘要不是证据。它只能帮我们找到方向，最终还要读权威文件。
- 记忆检索不能只搜一个词，要支持同义词/别名组合，比如 `记忆 OR memory OR SESSIONS OR RAG OR 压缩 OR 上下文`。
- 检索层级建议是：先精确搜会话/文件，再 SQLite FTS5，最后才走向量 RAG。

### 5. 压缩前没有及时 flush

表现：
- 会话压缩、长任务切阶段、派工批次收束前没有写状态。
- 恢复时只能靠残缺聊天上下文，容易断片。

未来要求：
- 压缩前、长任务阶段结束前、subagent 批次收束前必须 flush。
- flush 目标至少包括任务状态、运行日志、handoff 和必要记忆。
- 用户说“继续”时，应该有一个固定恢复入口。
- token 估算不能只用 `字符数/4` 或 `文件 bytes/4`，尤其中文会不准。
- watchdog 触发依据和实际写入来源必须一致，不能“文件很大所以触发”，结果写入的却是已经压缩过的摘要残渣。
- 压缩前 hook 必须先写结构化恢复快照，再允许真正压缩。
- 多轮压缩后恢复，不能只信最后一次摘要，要能追溯 compression snapshot。

### 6. 任务状态和记忆不同步

表现：
- memory 里说任务完成，但任务目录里的 `STATUS.md`、证据、测试没更新。
- 或任务目录更新了，daily memory 没记录。

未来要求：
- “任务完成”不能只写在 memory 里。
- 任务状态必须以任务目录和证据为准，memory 只做摘要和索引。
- 写任务状态时要同步写 timeline / daily 摘要。
- 正式任务恢复时，必须读任务目录里的 `STATUS.md`、`WORK_LOG.md`、`HANDOFF.md`、`ACCEPTANCE.md`、`TESTS.md` 等权威文件。

### 7. 历史 session 污染当前判断

表现：
- 旧会话里写过“已完成/已修复”，但当前文件状态已经变化。
- 模型看到旧结论后误判当前真实状态。

未来要求：
- 历史 session 只能作为线索，不能作为当前事实。
- 涉及代码状态、测试状态、任务完成状态时，必须重新看文件或证据。
- 记忆要带时间、来源、可信度和是否需要重新验证。
- `SESSIONS` 不是最终事实源，只是恢复线索。
- 压缩摘要可能混淆“未完成”和“已完成”，最终状态必须以任务目录为准。

### 8. 子代理成果没有进入可恢复记忆

表现：
- 子代理做完只在聊天里汇报。
- 父会话压缩后，关键结论、文件路径、教训、证据都断了。

未来要求：
- 子代理完成后必须写任务目录。
- 父代理验收时要抽取关键摘要进入可恢复入口。
- 子代理产物至少要有：结果摘要、改动文件、证据路径、测试结果、剩余风险。
- 子代理隔离上下文里的信息不会天然进入主会话，必须通过 `STATUS/WORK_LOG/ACCEPTANCE/DEBRIEF` 等文件落盘。

### 9. Lesson 没有抽象化

表现：
- 只记“今天某任务踩坑”，没有提炼成通用规则。
- 同类问题下次继续踩。

未来要求：
- lesson 要从具体事件抽象成可复用规则。
- lesson 需要适用条件和反例，避免过度泛化。
- lesson 可以进入未来 Skill Draft 队列，但现在不自动转 skill。
- 记忆、lesson、toolchain、skill 的边界要清楚：lesson 是教训，toolchain 是工具链和降级策略，skill 是可执行工作流。

### 10. 记忆和 skill 没打通

表现：
- 发现可复用流程后，没有进入 skill 提炼队列。
- 下次还是靠临场想。

当前决定：
- 自学习先不做。
- 先预留 `Skill Draft` / `Learning Candidate` 概念。
- 任何自动写正式 skill 的行为都禁止，未来也必须用户确认。
- 复杂流程可以标记为 skill 候选，但本阶段只记录，不升级。

### 11. 引用路径不清楚

表现：
- 长规则外移到 references 后，入口没有写触发条件。
- 新会话不知道什么时候该读哪个文件。

未来要求：
- 每个外移引用文件都要有入口索引。
- 索引必须写清楚触发条件、文件路径、适用范围和过期条件。
- HOT 层只放入口和硬规则，不塞全文。
- `MEMORY.md` 的引用是导航，不是参考读物；看到引用后必须继续读对应文件。
- 外部知识库如果只下载不建入口，也等于不可用。需要 `readme`、registry、分类索引和触发条件。
- 旧入口和新入口并存时，要标记 deprecated，避免模型读旧规则。

### 12. 今日 memory 不存在或未及时创建

表现：
- 新一天开始时，当天 memory 文件还没创建。
- 当天上下文没有固定落点。

未来要求：
- 每日首次写入或启动时自动确保 daily memory 存在。
- daily 文件只放当天流水和阶段摘要，不放长期硬规则。
- daily memory 至少应包含任务目录、当前状态、已完成交付物、未完成项/下一步、新规则/新偏好摘要和引用路径。

### 13. 重要偏好和普通流水混杂

表现：
- 小叶子的长期偏好、任务流水、技术日志混在一起。
- 召回时要么漏掉重要偏好，要么带入太多噪音。

未来要求：
- 长期偏好进入 HOT / Preference 层。
- 普通流水进入 Daily / Task / Archive。
- 写入时必须标注 `kind`、`scope`、`ttl` 或类似字段。

### 14. 缺少恢复快照

表现：
- 没有 `HANDOFF.md` / `MEMORY_LAST` 类恢复点。
- 用户说“继续”时，要重新翻很多历史。

未来要求：
- 每个长任务都要有可恢复快照。
- 快照要短，能回答：现在做到哪、下一步是什么、证据在哪、风险是什么。
- 用户说“继续 / 刚刚 / 上次 / 还没完”时，系统应该先恢复，不应该让用户重复解释。

### 15. 记忆更新没有验证

表现：
- 以为写了 memory，但文件不存在、内容为空或路径写错。

未来要求：
- 每次记忆写入后做 readback。
- 写入失败要明确报错，不能静默吞掉。
- 关键记忆写入要进入 LocalStore event，方便 `timeline` 查。

### 16. SESSIONS 写入质量会直接影响恢复

表现：
- 每条内容截断太短，技术细节、任务要求、报错栈被腰斩。
- 多行内容被强行压成一行，代码、错误栈、报告结构变成一坨。
- 追加写入时重复表头，导致表格乱、重复行多。

未来要求：
- SESSIONS 截断长度要足够保留可执行信息。
- 多行内容要保留结构痕迹，例如用 `↵` 标记换行。
- 新文件才写表头，旧文件只追加。
- SESSIONS 仍然只是线索，不替代任务目录。

### 17. 辅助压缩 / 辅助 flush 也会因为模型配置失败

表现：
- 辅助 memory flush 可能因为后端不支持 `temperature` 参数失败。
- 辅助压缩模型名错误会导致 summary 失败。

风险：
- 主对话不一定崩，但记忆写入或摘要生成失败。
- 用户会感觉“你明明说记住了，怎么又丢了”。

未来要求：
- 辅助模型调用要按后端能力裁剪参数。
- 辅助模型名必须可验证。
- memory flush / compression 失败要有显式错误和 fallback，不允许静默失败。

### 18. 长任务和普通聊天要隔离

表现：
- 长任务、大量回流、风险内容如果都打进主聊天，会让普通会话变慢、变脏、压缩更频繁。

未来要求：
- 长任务结果应落盘到任务目录，聊天只回短摘要。
- 主会话负责协调和展示，不承担所有原始细节。
- 不同入口（CLI、QQ、外部 adapter）要有清楚的记忆隔离和摘要策略。

### 19. 启动顺序和入口权威必须唯一

表现：
- 新会话如果没读启动权威入口，就会少规则、少偏好、少现场。
- 如果多个入口同时存在且没有 deprecated 标记，会读错旧记忆。

未来要求：
- 启动顺序以一个权威文件为准，避免多处复制导致漂移。
- HOT 层不要复制完整启动流程，只放入口引用。
- deprecated 入口要显式标记，不作为第一决策入口。

## 初步设计原则

1. 记忆必须先分层，再落盘。
2. HOT 层宁可少，不可乱。
3. RAG 只能辅助，不能替代关键入口。
4. 任务状态以任务目录和证据为准，memory 只做摘要和索引。
5. 历史结论默认需要重新验证，不能直接当当前事实。
6. flush 是生命周期事件，不是随缘动作。
7. 子代理成果必须从“聊天汇报”变成“任务目录 + 可恢复摘要”。
8. lesson 必须抽象，不能只堆流水。
9. skill 沉淀只预留，不自动开发、不自动写正式 skill。
10. 每次写记忆都要能验证：写到哪、写了什么、是否可读。
11. `MEMORY.md` 是导航，不是正文；引用文件必须按触发条件继续读取。
12. `SESSIONS` 是恢复线索，不是最终事实源。
13. 大任务必须靠任务目录和结构化状态，不靠脑内上下文。
14. 记忆要可触发、可路由、可验证，而不是越多越好。
15. 记忆最终服务于“继续把事做完”，不是服务于“存很多文本”。
16. 每一层记忆都必须有用户可控开关和预算。
17. 上下文压缩前必须先结构化落盘，token 消耗必须实时可见。
18. 记忆按功能分目录存放，不同功能的记忆不能混成一个大文件。
19. 记忆配置必须安全默认，非法值回落默认/安全值并可观察。

## 未来可能的最小闭环

先不写代码，只记录方向：

1. `memory doctor`
   - 检查 daily memory 是否存在。
   - 检查关键 memory 文件是否空。
   - 检查 LocalStore memory 索引是否和 JSONL 数量一致。

2. `memory flush`
   - 把当前任务状态、最近 gateway 请求、subagent 摘要写成恢复快照。
   - 支持压缩前、阶段收束前手动触发。

3. `memory write` 的结构化入口
   - 要求指定 `kind`：preference / daily / task / lesson / archive。
   - 写入后必须 readback 验证。

4. HOT / INDEX 入口
   - HOT 放长期硬规则和入口。
   - INDEX 记录哪些引用文件在什么情况下该读。

5. 子代理收束摘要
   - 子代理完成后，父代理从工单中抽取固定摘要。
   - 摘要写入任务目录和 LocalStore，而不是只留在聊天里。

6. `memory resume`
   - 听到“继续 / 刚刚 / 上次 / 还没完”时触发。
   - 先读最近 SESSIONS 线索，再读任务目录权威文件。
   - 回复用户时先说明恢复到哪里，再继续动作。

7. `memory routes`
   - 维护 HOT / MEMORY / references / lessons / toolchains / skills 的入口索引。
   - 每条入口写清触发词、权威路径、维护者、过期/验证方式。

8. `session writer`
   - 控制 SESSIONS 截断长度、换行保留、表头追加规则。
   - 写入后可被恢复流程稳定消费。

9. `compression preflight`
   - 压缩前先 flush 当前任务状态。
   - 检查辅助模型参数和模型名是否可用。
   - 失败时写明确 fallback 事件。

10. `compression snapshot hook`
   - 在上下文压缩前保存本轮会话恢复快照。
   - 默认按 `memory_archive_level=3` 保存恢复必需字段。
   - 支持用户发现异常后按 `session_id` / 时间 / task_id 找回状态。
   - 按天写入 `memory/hooks/YYYY-MM-DD.jsonl`，默认保留 7 天。

11. `context budget status`
   - 实时展示 token / context budget。
   - 支持 `status`、chat、Live Lab 输出当前估算消耗和压缩风险。

12. `deprecated entry check`
   - 扫描旧 MEMORY / SKILL_INDEX / registry 入口。
   - 给 deprecated 入口加标记，避免模型把旧入口当权威。

13. `memory config doctor`
   - 检查记忆配置是否有非法值、乱码、越界数字和危险路径。
   - 展示最终生效值和回落原因。
   - 不让错误配置静默改变记忆范围或隐私边界。

## 暂不开发但要预留

### 自学习 / Skill Draft

现在不做：
- 不自动生成正式 skill。
- 不自动改 `SKILL.md`。
- 不自动把 lesson 升级成流程。

但要预留：
- lesson 可以标记 `skill_candidate=true`。
- 未来生成 learning draft 时，必须写明来源任务、触发原因、适用场景、用户确认状态。

## 第二批用户输入的归并结果

本批 30 条没有按原文平铺，而是合并到上面的分类里：

- `MEMORY.md 大杂烩`、`memory-hot 过胖`、`长期记忆和任务进度混杂`：合并到层级混乱、长期记忆污染、HOT 约束。
- `MEMORY 引用不读`、`引用路径不清楚`、`外部知识库无入口`、`旧入口漂移`：合并到引用入口和入口权威。
- `daily memory 未写`、`大型任务不能靠上下文`、`继续时恢复顺序`：合并到 flush、daily、恢复快照和任务目录权威。
- `SESSIONS 不是事实源`、`摘要混淆状态`、`RAG 不是事实源`：合并到历史线索不等于当前事实。
- `token 估算`、`watchdog 来源不一致`、`辅助模型参数/模型名错误`：合并到压缩和辅助 flush 风险。
- `SESSIONS 截断、重复表头、多行压扁`：新增为 SESSIONS 写入质量问题。
- `QQ 长任务污染主会话`、`子代理记忆不进主会话`：合并到长任务隔离和子代理落盘。
- `记忆检索不能只搜一个词`、`写入不等于想起`：合并到检索策略和可触发规则。

## 等后续痛点来了再继续合并

下一批输入时，按这几个方向继续去重：
- 是否是新问题，还是属于上面的 15 类。
- 是否需要立刻开发，还是先作为规则记录。
- 是否会影响 `framework-runtime` 或 `tools-boundary` 两条并行线。
- 是否属于 memory 本身，还是属于未来 self-learning。

## 原始痛点索引

这一节保留用户输入的原始问题形态，方便以后回查。

说明：
- 这里的“证据”来自用户提供的历史经验和历史文件线索。
- 不代表这些历史文件都存在于当前仓库。
- 上面的“痛点去重”是设计归并；这里是参考索引。

### 第一批原始要点

1. 说“记住了”，但没落盘
   - 只在聊天里答应，没有写入 memory、任务目录或 lessons。
   - 重启或压缩后就丢。

2. 写错层级
   - 临时上下文、长期规则、任务状态、教训、skill 种子混在一起。
   - 结果是长期入口臃肿，真正该常驻的规则被淹没。

3. 长期记忆堆太多，启动上下文被污染
   - 旧 lessons 全塞长期文件。
   - 旧经验容易被当成当前事实。

4. RAG 召回不稳定
   - 明明记过但语义搜索没召回。
   - 关键规则必须有 HOT 层、INDEX 或固定引用入口。

5. 压缩前没有及时 flush
   - 会话压缩、长任务切阶段、派工批次收束前没有写状态。
   - 恢复时容易断片。

6. 任务状态和记忆不同步
   - memory 说完成，但任务目录状态、证据、测试没更新。
   - 或任务目录更新了，daily memory 没记录。

7. 历史 session 污染判断
   - 旧会话里的“完成了/已修复”误导当前判断。
   - 当前文件状态可能已经变了。

8. 子代理成果不进记忆
   - 子代理只在会话里汇报，没有写关键结论、文件路径、教训。
   - 父会话压缩后难接。

9. 教训没有抽象化
   - 只记某天某任务踩坑，没有提炼成通用规则。
   - 同类问题反复出现。

10. 记忆和 skill 没打通
    - 发现可复用流程后，没有进入 skill 提炼队列。
    - 下次还是靠临场想。

11. 记忆引用路径不清楚
    - 长规则外移到 references 后，入口没写触发条件。
    - 新会话不知道该读哪个文件。

12. 今日 memory 不存在或未及时创建
    - 新一天开始但当天 memory 文件没创建。
    - 当天上下文没有固定落点。

13. 重要偏好和普通流水混杂
    - 长期偏好、任务流水、技术日志混在一起。
    - 影响召回准确性。

14. 缺少恢复快照
    - 没有 HANDOFF / MEMORY_LAST 类恢复点。
    - 用户说“继续”时要重新翻很多历史。

15. 记忆更新没有验证
    - 写了 memory 但没有确认文件存在、内容非空、路径正确。
    - 容易以为记了其实没记成。

### 第二批原始要点

1. `MEMORY.md` 容易变成大杂烩
   - 具体规则、任务细节、长表格、运行日志都塞进去。
   - 修正方向：`MEMORY.md` 只存引用指针，具体内容外置。

2. `MEMORY.md` 里的引用如果不继续读，就等于没记住
   - `MEMORY.md` 是导航，不是正文。
   - 遇到话题必须按指向路径继续读对应文件。

3. daily memory 如果没及时写，跨会话会断
   - 长任务、跨天任务、复杂任务不能只靠聊天上下文。
   - daily memory 至少记录任务目录、状态、已完成、未完成、下一步、新规则引用。

4. `SESSIONS` 不是最终事实源，只是恢复线索
   - 只读会话不读任务目录，会被中间状态误导。
   - 正式任务要读 `STATUS.md`、`WORK_LOG.md`、`HANDOFF.md`、`SUBTASKS.md`。

5. 用户说“继续”时，不能装失忆，也不能让用户重复解释
   - 听到继续、刚刚、上次、还没完，要先恢复上下文。
   - 先恢复，再告诉用户当前恢复到哪里。

6. `memory-hot` 不能塞太多东西
   - HOT 层只放最高频执行铁律和入口引用。
   - 详细规则去 references/routing。

7. 长期记忆和任务进度容易混在一起
   - 长期记忆存稳定偏好、规则、环境事实、长期教训。
   - 任务进度存任务目录和 daily memory。

8. 大型任务不能靠上下文记忆
   - 大任务产物多、阶段多、子任务多，压缩后容易丢状态。
   - 必须靠分层 JSON 状态、WORK_LOG、ACCEPTANCE、TESTS、HANDOFF 和逐层汇总。

9. 中文 token 估算导致压缩时机不稳
   - `char/4` 或 `bytes/4` 对中文和完整请求体不准。
   - 正确估算应考虑 system_prompt、messages、tools。

10. watchdog 触发和实际写入来源不同
    - 阈值看 session 文件大小，写入看 session JSON messages。
    - 文件很大但 messages 可能已压缩成摘要，最终写入残渣。

11. `SESSIONS` 内容截断太狠
    - 每条只截 150 字符会丢技术细节、任务要求、报错栈。
    - 修正方向：提高截断长度，保留更多上下文。

12. `SESSIONS` 重复表头、格式乱
    - 追加写入不判断文件是否存在时会重复写表头。
    - 修正方向：新文件写表头，旧文件只追加。

13. 多行内容被压成一行后可读性差
    - `" ".join()` 会把代码、错误栈、报告结构压成一坨。
    - 修正方向：换行替换成 `↵`，保留结构痕迹。

14. 辅助记忆 flush 可能被模型参数坑住
    - 某些后端拒绝 temperature 参数。
    - 结果是主对话不崩，但辅助记忆写入失败。

15. 辅助压缩模型名错误会导致 summary 失败
    - 历史出现过 unknown model 导致 context summary / compression 失败。
    - 影响压缩后上下文总结质量。

16. RAG / 向量记忆不能替代精确状态文件
    - RAG 适合语义召回，不适合当任务状态唯一来源。
    - 任务状态仍以精确文件为准。

17. 只靠 RAG 注入有“看起来想起了，其实没核对”的风险
    - RAG 摘要不是完整证据。
    - 最终要读 STATUS、ACCEPTANCE、TESTS、MEMORY 引用文件、references 规则文件。

18. 旧入口和新入口并存，会导致读错记忆
    - 旧 MEMORY 快照、旧 SKILL_INDEX、旧入口文件可能误导模型。
    - 需要 deprecated 标记和唯一权威入口。

19. 记忆、外部知识库、skill 的边界容易混
    - 内容应该分别进入 memory、daily、SESSIONS、memory-hot、lessons、toolchains、skills。
    - 混放会导致查不到、重复、入口漂移、子代理读错。

20. “记住”不能只是嘴上答应
    - 用户说记住时，要先判断类型再落到正确位置。
    - 偏好、任务状态、教训、工具链经验、复杂流程分别有不同落点。

21. “读了 memory”不等于“恢复了任务”
    - memory 可能只包含摘要或引用。
    - 正式任务恢复必须读任务目录。

22. 压缩后摘要可能把“未完成”和“已完成”混淆
    - 摘要会丢时间顺序和最终状态。
    - 判断状态以任务目录里的 STATUS / ACCEPTANCE 为准。

23. QQ 长任务会污染主会话记忆
    - 长任务、大量回流、风险内容打进主会话会让普通聊天变慢变脏。
    - 长任务结果应落盘到任务目录，聊天只回短摘要。

24. 子代理记忆不能天然进入主会话
    - 子代理隔离上下文里的信息不会自动成为主代理可靠记忆。
    - 子代理必须写 STATUS / WORK_LOG / ACCEPTANCE / DEBRIEF。

25. 记忆检索不能只搜一个词
    - 一个概念可能有多个词：memory、SESSIONS、压缩、上下文、RAG、daily memory。
    - 跨会话检索要用 OR 组合关键词。

26. 启动顺序一旦不执行，后面都会偏
    - 新会话不读权威入口会少规则、少偏好、少现场。
    - 启动顺序必须以一个权威入口为准，避免漂移。

27. 记忆不是越多越好，关键是可触发、可路由、可验证
    - 每条记忆要回答触发词、权威文件、维护者、过期验证方式。

28. 外部知识库只下载不建入口，也等于不可用
    - 本地有资料但没有 readme、registry、分类索引时，任务时用不上。

29. 记忆成功写入不等于下次一定会自动想起
    - 需要一行引用、routing 触发词、skill 入口、lessons/toolchains 分类和 daily 摘要。

30. 最根上的教训：记忆要服务“继续把事做完”
    - 价值标准是：不让用户重复解释、不把已完成说成未完成、不忘边界和偏好、不重复踩坑、不靠猜继续任务、能找到权威文件和证据。

31. 多轮上下文压缩仍会遗忘，压缩前必须保存结构化快照
    - 快照接近全量等级 3：不一定保存所有原文，但字段要丰富。
    - 用户发现 agent 丢状态时，可以要求按快照找回。
    - token 消耗和距离压缩阈值必须实时可见。

32. hook 快照和 daily memory 不能混放
    - hook 快照按天写到独立目录，例如 `memory/hooks/YYYY-MM-DD.jsonl`。
    - daily memory 只写当天状态摘要和引用路径。
    - hook 默认保留 7 天，用户可以调大，硬盘够就随便造。
