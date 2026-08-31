# Memory Structure

本文只描述当前记忆主链路（单 owner 视角，local/main 下的持久化事实源）。

## 事实源

- 正式长期记忆：`owners/local/main/memory/long_term/memory.jsonl`（晋升后的正式事实）。
- 日常记忆：`memory/daily/`（按日），`memory/lessons/`（经验教训）。
- 候选：`memory/candidates.jsonl`（待结构化证据/冲突/阈值核验，晋升前不具事实权威；只有 SOUL 候选等待用户确认）。
- recovery snapshot：`memory/hooks/YYYY-MM-DD.jsonl`（运行经历归档的轻量恢复线索）。
- 运行事实（runtime facts）与 raw archive：由 `run --save` 写，`memory-resume`
  据此恢复上下文；与 `ConversationStore`（transcript/guidance/goal）分开。
- 任务级事实源（task.yaml / run_workspace.json / runtime.db）是任务生命周期权威，
  记忆层只消费不覆盖。

## 权威与投影

- 记忆晋升、候选审核、HOT/lesson 写入走 `AgentConfig` 开关与统一 Candidate/Curator
  链，入口收敛在 memory 模块；CLI 普通 run 不直接把对话正文写进正式长期记忆
  （`--no-save` 关闭本次运行归档，ConversationStore 与审计仍照常）。
- `promotion_mode` 由宿主根据 typed target/type/origin/action 每次重算，不接受模型或旧账本把
  自主候选永久降为人工。长期事实、USER、AGENTS、lesson 与 HOT 在满足各自证据、精确目标、冲突、
  阈值、CAS、quota 和注入扫描后自主提交；只有 SOUL 进入 owner 用户确认链。
- 所有 active/candidate/daily/lesson/HOT/persona 路径都从当前 owner 的 `HomePaths` 解析。共享 Gateway
  只调度多个 owner，绝不共享这些仓库；投影、索引和恢复包不得把其它 owner 的正文带入当前模型。
- `update_persona` 的写入频率保护也必须以 canonical owner home 分桶；同一 Gateway 的其它 owner 不能消耗
  当前 owner 的额度。限频只是一层软资源保护，拒绝必须结构化标记 `effect_outcome=not_started` 并保留可
  退避错误码，不能污染记忆正文、确认链或副作用未知账。
- `memory_path` 等路径由 home 解析统一给出（显式配置 > MY_AGENT_HOME 环境变量 >
  ~/.my-agent 兜底），记忆模块不自行猜测 owner home。

## 运行中 native 工具历史摘要

- `agent/memory_archive/compact_semantic_summary.py` 是 carried archive 续跑摘要与运行中
  native IR 摘要共用的语义摘要入口，但不拥有 Compact 状态、工具执行或完成判定。
- 摘要后端调用统一经 `agent/conversation/auxiliary_model_call.py` 包装，复用当前 agent 的
  `ModelCallLedger`、provider attempt observer、全局 admission 和指标；Compact 不再拥有第二套线程超时
  或隐形调用计数。调用持续时间由 backend/provider 已配置的网络超时负责收口。
- 运行中真实 turn 的摘要输入由“上一代 ConversationThread 完整摘要 + 本次 native 工具
  历史 + 当前任务”组成，输出是可独立替代上一代的完整摘要，不是只描述本次增量的片段。
- provider 消息顺序固定为“真实任务 user → native history → synthetic Compact user 指令”。这是 会话运行时
  compaction turn 的适配；普通 backend 的 `prompt + messages` 会把 prompt 放最前，不能直接复用该顺序。
- `agent/conversation/live_tool_compact.py` 才负责把这份摘要连同精确移除/保留的 tool-call
  ID 写入 checkpoint，并通过 ConversationStore 的同一 CAS 推进 generation；TUI 只投影
  已提交的代次和 token 前后值。
- `_tool_loop_service` 在摘要、计量、checkpoint 与 CAS 的实际边界发送同一个 content-free progress block；
  `LiveToolCompactCommitRequest.after_checkpoint` 只允许在 checkpoint 已成功、CAS 尚未开始时发 committing
  milestone，异常被吞掉，不能反噬会话提交。进度条和 spinner 是投影，不是第二本 Compact 账。
- in-memory 候选未达到 recovery target 时用 `superseded/candidate_discarded` 关闭这一个 operation：它只撤销
  展示块，不写 checkpoint、generation 或 failure circuit。后续 transcript attempt 用独立 operation id 接管；
  `failed` 只代表摘要 transport、checkpoint、CAS 等真实故障。
- `save` 与 transcript authority 是两个结构化事实：前者决定 `Agent.run` 是否写旧式回复/记忆，后者决定
  当前 exact thread 是否拥有 Compact CAS。后台 main 由 ConversationStore 另行提交回复，因此可以
  `save=False + authoritative=true`；辅助、不保存且非权威的展示回合仍只能临时摘要。任何正文权威的
  main/child/grandchild 都不能在摘要 transport/调用失败后先删除历史，必须整体失败并恢复原 native IR。
- completed-empty 与调用失败是两个合同：前者表示 provider 请求和用量账都已正常结束，只从 typed IR 生成
  `compact-mechanical-fallback.v1` 的有界非权威续接投影并继续 checkpoint/CAS；后者仍抛错、恢复 IR 并累计
  熔断。transcript Compact 对 completed-empty 使用 raw row + structured operation evidence 的同类有界投影。
  两种机械投影都不判断完成、不授权路径、不替代 archive、operation ledger、artifact 或真实文件。
- 非空 provider 正文也不能自动成为 live handoff：只接受以 `compact-live-handoff.v1` 开头、六个固定语义栏
  完整且不含供应商工具协议的纯文本。非法正文与 completed-empty 一样使用 typed IR 机械投影，但 reason
  区分 `provider_empty_summary` 与 `provider_invalid_summary_shape`，便于审计而不把模型文本升级为机器状态。
- 同一个 `CompactionSummary` 容器里的 thread summary 与 active-turn carried handoff 以稳定 schema marker
  区分；二次 Compact 只删除 exact 上一代 thread summary，不能吞掉 carried handoff。当前任务由首条
  provider user message 唯一承载，Compact synthetic user 只放摘要指令与上一代 summary。
- 摘要不是执行事实源。精确副作用和交付仍以 raw archive、operation ledger、artifact
  registry、任务工作区和真实文件为准。

## lifecycle wake 的 carried tool archive

- 每个 root task 的 `work/blobs/tool_outputs/index.jsonl` 同时索引 bounded `tool_call` 与外置
  `tool_output`。child lifecycle 后续工作片用 durable task id 定位这一文件，再按 completion 信封的 exact
  `conversation_request_id` 读取，保留 append 顺序，并以 `scoped_call_id` 去重；新行显式保存 turn id，
  旧行仅以同值 `request_id` 兼容，其它 task、同 task 的其它 turn 和 child workspace 不扫描。
- 恢复记录沿既有 `carried_archive_tool_calls` 进入工具循环，重建已执行工具、one-shot key、工具轮基线、
  参数和 artifact refs。索引同时保存宿主执行 `parameters` 与 provider 原始 `model_parameters`：前者只给
  宿主审计、恢复和幂等使用，后者才允许进入 carried 摘要或模型 replay。两者都使用限深、限宽、凭据脱敏
  的 JSON 投影，Todo items、批量派工 items 与 typed covers 不得因嵌套而变成空数组。旧索引只有在
  `input_sources` 能证明字段来源时才提取模型视图。大输出正文仍留在 owner 私有 artifact，按需读取；
  索引损坏时 fail-soft 回到已有 task/transcript 上下文，但不得编造已执行事实。
- 恢复到 live prompt 时再次经过同一大参数 reducer，`write_file.content` 只留下路径、模式、长度、hash 和
  短 preview；chronology index 超限时使用 bounded head + newest tail，并显式记录中段省略数量。完整正文
  只能按 artifact ref 读取，不能因下一工作片启动而整份回灌。
- 工具首次 externalize 时同时写入宿主确认的 bounded `tool_execution` 与 `tool_operation`。carried record
  将它们恢复到现有 handler/failure/operation status 字段，供同一 active turn 的 operation verification
  原样核对；任意诊断私有字段和工具正文不进入索引，没有 typed operation 终态时继续 fail-closed。
- text 协议继续读取机械 tool-context；native 跨进程续跑不能伪造原 provider ToolCall/ToolResult 对，改为
  把同一批 carried 记录压成唯一、有界的 `CompactionSummary` handoff 放回 IR。它随之后每次 provider
  请求持续可见，但不推进 ConversationThread generation，不增加 TUI `compact N`，也不获得副作用权威。
- 这条链只解决同一 active turn 跨后台工作片的连续性，不新建 Compact 账本，不推进 generation，也不让
  自然语言计划获得机器权威。

## Artifact 分页与大输出恢复

- 完整工具输出先交给唯一 externalizer 判定和归档，再产生模型 preview；工具可以用结构化
  `tool_output_policy.requires_recovery_artifact=true` 要求保留完整正文，但不能指定宿主路径。全局大小阈值
  和预览容量仍是另两条通用触发条件。
- artifact 索引为模型提供稳定逻辑 `canonical_artifact_ref`；物理 `artifact_path` 仅供宿主 CLI/审计，TUI
  进度和模型参数不得泄漏它。读取窗口统一返回 `window_start/window_end/total_chars` 与前后剩余方向；只有
  `has_more_after=true` 才提供 `next_offset`。tail 读取即使省略前缀也表示已经到 EOF，search 的截断只代表
  匹配展示上限，不代表正文还有下一页。
- 原始 artifact、append-only index 和 operation ledger 继续是事实源；模型 preview、摘要和逻辑 ref 只是
  有界续接材料，不能证明工具成功、任务完成或授权路径。

## 2026-08-17 测试适配记录

- 记忆相关测试的 `tool_protocol="text"` 配置移除、假后端 native 化、协议快照默认
  native（EXEC-31b 适配，详见 02-progress.md）。
- compact 自动续接链的 native 工具轮差异已记录 xfail，待按 native 语义适配。
