# 决策模型 P5-A：召回前判断只读审计

状态：2026-09-22 只读审计完成，随后主线已实施默认关闭的补充查询首片；尚未调用真实 Jev 或完成 P5-A 全项验收。工作台为 `codex/decision-model-integration`，审计时 HEAD `ef497a904`，共享未提交改动持续变化。

## 要解决的问题与结论

普通对话可能需要历史事实，也可能只需当前上下文。Jev 可帮助判断补充哪些记忆，但错判不能让用户已经要求的历史资料静默消失。当前普通聊天请求只有 `user_prompt` 文本，没有可信的“本轮明确要求查历史”结构化标志。因此 **P5-A 暂不能实现 Jev 决定 `skip_formal_recall=True`**，也不能用“记得/以前/历史”等词表充当安全门。

最小可自动消费的第一片应是**仅增加的检索建议**：原路由、原查询和原 HOT/lesson/long-term 选择先保持；Jev 在正式 long-term 检索前，从宿主提供的有限补充查询候选中选择一个。宿主先得到原查询结果，仅在 `memory_top_k` 尚有空位、正式字符预算仍有余量时，用同一 owner、同一 `MemoryRecallScope` 的原检索器补齐。原结果保持原序且不被替换；无合法建议、`not_needed`、`need_data`、超时、额度/配置错误、取消之外的普通错误均沿原召回。模型无需用户逐次选择或补决策资料。若实现触及检索访问计数，必须先让原检索器支持“候选查询不记访问，最终采用后才记访问”，避免未注入的补充候选被误记为使用。没有这项原检索器合同，就先做只观察，不宣称 `apply` 可用。

当前 TypeSafe 协议只接受 `choice`、`score`、`noul` 题目，**不能让 Jev 自由生成 `supplemental_query` 文本**。因此补充查询候选必须由宿主从现有请求/任务上下文预先生成并冻结为有限 ID；目前 `RuntimeContextRequest` 没有验证过的候选查询字段，候选生成与无副作用检索是 `apply` 前的两个实际前置条件。若这两个条件做不到，首片只能 `observe`。该建议只选择检索候选，不声明记忆来源权限、路径、任务身份、正式条目 ID 或“无需召回”机器状态。若将来需要真正跳过普通召回，先要有由可信命令/工具/调用方写入的结构化请求意图及覆盖证明；Jev 自己推断出的意图不是这份证明。显式记忆命令即使有结构化身份，也应继续走原命令路径，不用这条可选优化替代。

## 已核对的权威链与接缝

| 位置 | 当前事实 | P5-A 边界 |
| --- | --- | --- |
| `agent_core/runtime/loop_models.py::RuntimeContextRequest` | 有 `user_prompt`、`context_scope`、请求/运行/任务 ID、task attributes；无可信 `requires_history` 或等价字段。 | 不从文本推导硬跳过；操作身份沿原字段。 |
| `agent_core/runtime/loop_support.py::_prepare_runtime_context` | 先由 `task_local`/`control_plane` 或 owner `memory_enabled=false` 构成 `recall_suppressed`；再建路由、scope、项目 scope、`search_scoped` 与正式合并。 | 建议入口只能位于原总闸和 scope 形成后、`search_scoped` 前；总闸关闭时连可选 Jev 和正式索引都不碰。 |
| `memory_store/recall.py::MemoryRecallScope.from_runtime` 与 `long_term_record_matches_scope` | personal/global 及结构化任务范围精确匹配；legacy 无合法范围不得自动进入 prompt。 | 模型不得追加 scope 或从 query 文字猜 project ID。 |
| `memory_store/jsonl.py::search_scoped` | 从 active 正式 JSONL 建 allowlist，以 hybrid 检索，限定 `top_k`，返回前 `_note_access`。 | 补充查询必须复用它的 allowlist/范围/排序，不另建影子库；访问计数需要先与最终采用分开。 |
| `memory_routing/context.py::build_routed_memory_context` | 一次性完成 route 匹配、原 `auto_read_limit` 选择、授权文件读取及 receipts。 | 首片不插进路由读文件顺序，也不让 Jev 生成路径；否则需先拆开候选和读取并证明原 required set 不丢。 |
| `loop_support.py::_formal_memories_for_request` | HOT、已读 lesson、原 long-term 去重，再按 10,000 字符预算保留；`PreparedRuntimeContext.memories` 进入唯一上下文投影。 | 补充结果排在原 long-term 之后；原 HOT/lesson 和原候选的预算/顺序不变，补充项只占剩余空间。 |
| `memory_store/decision_recall.py::rerank_recalled_memories` | P3 对**已经授权且预算选中**的长期事实原槽位排序，失效/非选择保留原序。 | P5-A 与 P3 不合并成一次决策：前者可能补充原检索未命中的事实，后者仅排序已选事实。两者共享原 stage/期限、设置/账本/消费复核。 |

现有原链还会在普通请求扫描正式 long-term 的 project scope。P5-A 不应借此扩大项目范围；项目 scope 的增补需要单独核对其原授权语义。`task_local`、`control_plane`、owner 总闸关闭时的零正式读取已有 `test_memory_first_loop.py` 测试。普通 `context_scope="conversation"` 不等于“用户不需要记忆”。

## 推荐的首片合同

1. 在现有 decision settings 增加独立 `pre_recall` 接入点，默认 `off`，仍使用共用 `begin_decision_stage` / `decide` / `decision_outcome_is_current`、原模型 profile、取消、绝对期限和调用账。不能复用 P3 `recall` 的开关并暗中多发一次请求。若 P3 也开启，同一轮准备应共用一个阶段绝对期限，避免两个 4 秒串行预算。
2. 宿主在原总闸与 `MemoryRecallScope` 确定后准备有界状态：当前问题、可信 owner/thread/run/task 引用、原允许范围与配置版本；默认不向 Jev 暴露未召回的全库正文/索引。宿主须先从已有请求/任务上下文构造有界、非空、互异的查询候选及 ID，且它们只能是检索文本，不能携带权限。Jev 的 `choice` 只选候选 ID 或 `not_needed` / `need_data` / `no_match` / `abstain`；无候选则不调用。`need_data` 只诊断已有资料不足，不触发向用户追问或扩大读取。
3. 原 `search_scoped(user_prompt, memory_top_k, predicate)` 是必做基线。基线返回不足 `memory_top_k` 且原字符预算有余量时，才在相同 scope 内执行最多一次建议查询，按原 entry ID 去重，追加至基线尾部；最终 `_budgeted_formal_memories` 再守总预算。为了不让被丢弃结果增加 touch，应先抽取原检索内部“候选/最终访问确认”接缝，正常关闭行为与原 `search_scoped` 完全等价。
4. 采用前复查原 owner 总闸、scope、task attributes、设置/连接版本、绝对期限与取消；原源已撤销、变更或无剩余预算时只保留此刻合法的原结果。可选调用的失败不能改变 `search_scoped` 的原异常语义，也不能把原召回失败伪装成空历史。
5. 结果只在本轮 `PreparedRuntimeContext.memories` 中，诊断写原 `routed_context.findings` 的固定码，输入 token 进原 decision 用量；不写长期记忆、不保存第二套候选或费用账。用户/agent 可以事先按原设置服务开关和调时长，运行时无需逐次干预。

真正“少检索/零检索”的性能收益不属于此首片；它主要检验补充召回能否减少遗漏。若首片仍需一次 Jev 加一次原检索、最多再一次补充检索，真实时延和输入 token 可能增加，必须用关闭/观察/采用对照及漏召回样本检验收益，不以 Jev 自评分作结论。质量不足就保持默认关闭。

## 仍不能做的场景

- 普通中文聊天里仅凭自然语言或 Jev 推断“无需历史”，跳过原 long-term、HOT、lesson 或 route 读取。
- 让 Jev 提供文件路径、任意 memory ID、owner/project scope，或改变原 `memory_top_k`、路由读取上限和 10,000 字符预算。
- 用补充查询替换原查询、抢占已选结果槽位，或在预算裁剪前把补充项排在原结果前。
- 将会话历史搜索、Daily、Candidate、Ops、Compact checkpoint 当作 formal long-term 的同一来源；它们各有自己的权威和权限。
- 在没有无副作用候选检索 API 前，声称一次额外 `search_scoped` 的落空候选没有写入/访问影响。

## 参考与验证

本地参考核对了 `docs/design/README.md`、`CONTRACTS_MAP.md`、`CODEBASE_TREE.md` 与 `docs/modules/memory/{02-progress,04-structure}.md` 的入口/权威索引；源码边界以上表为准。协议还核对了 `backends/typesafe_decision_wire.py::_validate_question`，确认没有自由文本回答。外部只读参考为本机 `study-agent/all-agent/openclaw-main/docs/concepts/{memory,memory-search}.md`：它把显式 `memory_search` 与自动注入分开，自动注入限定可信晋升来源，混合检索保留原召回途径；只借鉴“明确来源边界和失败语义”，不照搬其索引/插件实现。该参考文档还将 session 搜索另列，支持本审计的来源分离判断。

本次执行：

```text
python3 -m pytest -o addopts='' agent_py_agent/tests/test_decision_recall.py agent_py_agent/tests/test_memory_recall_v2.py agent_py_agent/tests/test_memory_first_loop.py -q --tb=short
```

结果 **53 passed、6 failed**。六个失败均为既有接口错位：`_formal_memories_for_request` 已改收 `skip_formal_recall`，上述前两个测试文件仍有六处传 `task_local`，触发 `TypeError`；本审计只记录，主线或对应运行时 owner 修复后重跑。没有跑真实模型、Gateway 或外部网络。

后续主线记录：运行时 owner 仅修这两个测试文件的旧调用，主线重跑上述三文件 **59 passed**；不改变本审计对 P5-A 尚未实现的结论。
后续主线已实施：正式 JSONL 复用原 scoped 排序提供候选不 touch 与最终访问确认，
且从完整问题语句产生至多四个有界查询候选。独立 `pre_recall` 设置点默认 `off`；原完整查询、HOT、lesson 与原长期事实先进入唯一正式上下文准备入口，Jev 最多选择一个补充查询，仅在 `memory_top_k` 和 10,000 字符预算有空位时，按原 scope 检索并追加确认有效的事实。P3 召回后排序和本点共用一次阶段绝对期限。关闭/观察/四种非选择/错误不执行补充搜索；来源撤销、过期和取消不复活旧事实。六个 focused 文件 **136 passed**，含受控真实 JSONL 漏召回样本：基线先选 Orion、补充片段仅追加 Lyra、访问只确认新增项；相关 Ruff 通过。该首片不等于 P5-A 全项验收；普通召回仍先执行。

隔离真实 Jev 对照随后完成两轮，每轮 `off/observe/apply`，共 **4 次**官方 HTTP：关闭零请求，观察与采用各一次；实际返回 `jev-1.13.0`，每次输入678，约0.84—0.86秒。两轮均建议第二个查询片段，但本次纯词面 JSONL 的两条正式事实已被完整问题基线召回，`apply` **没有追加记录**，现以 `no_addition` 明确诊断，不能把建议成功当成召回收益。目录哈希未变，临时 thread 设置已恢复。脱敏证据位于仓库外 `~/.codex/private-tests/decision-model-live/p5a/20260922T222847/summary.json`；未实际调用 MiniMax 或 Gateway。这说明消费/计量链可用，也暴露“查询片段是完整查询子集，词面检索下常无新增”的效果限制，默认保持关闭。

后续继续验证：有已知漏召回的真实语义检索样本、Gateway 普通聊天的最终来源和净输入 token；不让测试者替被测代理补材料。若没有实际收益，保持默认关闭。

## 交接与建议下一步

原只读审计只拥有本文件；上面的后续实施由主线完成。下一步是隔离真实接口验收与原召回对照，必要时再修通用接缝；与子代理异模、主会话选模并行时分别守住文件和 Gateway 所有权。风险守门点是：任何 Jev 建议均不得静默删掉原召回，任何补充命中均不得越过 owner/scope/数量/字符预算。
