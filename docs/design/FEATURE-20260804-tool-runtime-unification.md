# FEATURE-20260804-tool-runtime-unification

Status: Complete; full/static/package and ordinary-Chinese real-model verification recorded

## Background / 背景

迁移前，my-agent 已有 Registry、Schema 校验、路径/命令/URL 门、审批绑定、沙箱、operation store、
大输出归档和原生 provider 适配，但由多组重叠类型和入口连接：旧 `ToolSpec` 同时接受多套参数声明，
运行时有多种 ToolCall/ToolResult，native 正文还可能被提升执行。当前实现已经按本规格收敛；本段只保留
为历史问题说明，现状以架构文档和代码合同为准。

本规格由用户于 2026-08-04 明确提出的工具统一目标触发。完整架构、参考证据和迁移表见
[tool-runtime-unification.md](tool-runtime-unification.md)。

## Goal / 目标

把当前请求的动作义务、工具快照、provider 协议、规范 ToolCall、统一 ActionPolicy、唯一
ToolExecutor、operation ledger、规范 ToolResult、动作销账和完成判断接成一条结构化主链。用户要求
真实操作时，要么真实执行并留下可核验证据，要么明确请求审批、输入或报告阻断；普通解释、代码块、
网页和模型正文永远不能取得执行权威。

## Non-Goals / 非目标

- 不全面重写 Memory、Audit、Subagent、Gateway 或 Delivery 业务逻辑。
- 不从自然语言、关键词或目录扫描推断业务完成质量。
- 不新增与现有工具语义重叠的工具。
- 不引入通用 Saga、跨工具自动补偿或第二份 operation ledger。
- 不把 `task_progress` 的普通开放项升级为通用硬完成门。
- 不改变 `wait` 作为唯一非阻塞等待、提醒和 yield 工具的定位。

## Scenarios / 场景

1. 用户说“在项目里运行 pytest -q，把失败原因告诉我”：请求产生明确 action obligation，provider
   必须调用合适工具或给出结构化阻断，不能只返回“已运行”。
2. 用户问“怎么运行 pytest”：没有动作义务，模型可以直接解释，系统不得执行命令。
3. 用户让解释文档中的 `rm -rf /`：正文只是数据，不能变成 ToolCall。
4. 危险调用缺少审批：ActionPolicy 返回 ask，handler 执行次数为零，仍生成配对 ToolResult。
5. 修改操作超时且副作用未知：账本保持 unknown，禁止自动重放，等待结构化核对。
6. 多个互不冲突的只读调用：按声明的并发策略并行；同一资源写、审批、危险或未知调用成为屏障。
7. compact/resume：动作义务、ToolCall/ToolResult 配对和 operation 终态仍从同一结构化状态恢复。

## Requirements / 需求

| ID | Description | Priority |
| --- | --- | --- |
| FR-001 | 每个工具只保留一个 `ToolModelSpec.input_schema`，provider 展示与运行时校验共享 `schema_hash` | Must |
| FR-002 | `ToolRuntime` 唯一绑定 model spec、runtime policy、handler、availability 与 exposure | Must |
| FR-003 | 所有 provider 只产生一种 canonical `ToolCall`；模型正文不能直接生成 ToolCall | Must |
| FR-004 | 所有结果只产生一种 canonical `ToolResult`，成功、失败、拒绝、重放、取消均与调用配对 | Must |
| FR-005 | 每个 run 固定 provider capability、协议与 `ToolRuntimeSnapshot`，native/text 不静默切换 | Must |
| FR-006 | provider 请求统一支持 `auto`、`required`、`specific(name)`、`none` | Must |
| FR-007 | `EffectiveContractSnapshot.required_actions` 是当前请求动作义务唯一权威，真实证据才能销账 | Must |
| FR-008 | 所有调用在 handler 前经过唯一 `ActionPolicy`，返回 allow/ask/deny 和完整结构化证据 | Must |
| FR-009 | 所有调用由唯一 `ToolExecutor` 状态机执行，宿主独占 lifecycle 字段写权 | Must |
| FR-010 | mutating/dangerous 调用复用唯一 operation ledger；unknown 不盲重试 | Must |
| FR-011 | Shell 使用确定性结构分析；不确定时 ask/deny，且 OS sandbox 仍为硬边界 | Must |
| FR-012 | 大输出完整归档，模型只见有界投影、hash 与 refs；compact 不丢完整引用 | Must |
| FR-013 | 并发只由 effect、resource scopes、parallel policy 与冲突判断决定，不硬编码工具名白名单 | Must |
| FR-014 | cancellation token 传到可取消工具；取消后不启动新调用且真实收口账本 | Must |
| FR-015 | 删除 native 文本提升、重复 Schema/approval/effect/执行入口和只验证旧行为的测试 | Must |
| FR-016 | Audit/Memory 只消费 canonical 工具事实，不产生平行执行或结果协议 | Must |

## Constraints / 约束

- Python 和现有依赖版本不变；新增依赖必须有明确安全和发布理由。
- 以当前 worktree 为事实源，保留用户未跟踪文件和其他 agent 修改。
- 共享文件只做工具主链所需的最小接线，不覆盖 Audit/Memory 业务语义。
- 先稳定串行主链，再开启受控并发；审批和交互必须保持可序列化。
- 普通用户测试 prompt 必须保持自然中文，不泄漏工具名或内部合同字段。
- 不提交、不推送，除非用户另行授权。

## Impact / 影响

- `agent/tooling/**`：工具定义、Registry、ActionPolicy、Executor、operation 与输出投影。
- `agent/backends/**`：provider capability、tool_choice、结构化 ToolCall/ToolResult 适配。
- `agent/agent_core/tool_loop/**`：调用决策、执行批次、动作销账、完成门、取消与 compact。
- `agent/contracts/**`：required actions、工具合同、审批/路径/命令/URL gate 的统一返回。
- `agent/settings/**` 与配置文档：显式协议、能力和并发/超时策略。
- 工具相关测试、产品事实、设计台账、路线图和代码树。

## Architecture / 架构

唯一主链：

```text
user request -> EffectiveContractSnapshot.required_actions
-> ToolRuntimeSnapshot -> ToolChoice -> ProviderAdapter -> ToolCall
-> ActionPolicy -> ToolExecutor -> OperationLedger/Reconciliation
-> ToolResult -> required-action settlement -> CompletionGate
-> model-authored final from structured facts
```

`ToolRuntime` 是唯一注册对象；`ToolCall` 与 `ToolResult` 是 provider、执行、历史、归档和 Memory/Audit
消费的共同合同；ActionPolicy 只决定当前动作能否进入执行，ToolExecutor 独占 lifecycle 编排。

## Data Model / 数据模型

详细字段见模块设计。核心对象为：

- `ToolModelSpec(name, description, input_schema, schema_hash, hints)`
- `ToolRuntimePolicy(effect_resolver, approval_policy, sandbox_policy, idempotency_policy,
  timeout_policy, concurrency_policy, resource_scopes, output_policy, availability_policy,
  input_policy, promotes_task)`
- `ToolRuntime(model_spec, runtime_policy, handler, availability, exposure)`
- `ToolCall(call_id, tool_name, arguments, source_protocol, schema_hash, run_id, turn_id,
  attempt_id, required_action_id, operation_id, idempotency_key)`
- `ActionDecision(status, reason_codes, evidence, approval_request, sandbox_plan, resolved_effect,
  resource_scopes)`
- `ToolResult(call_id, tool_name, status, content_blocks, error fields, execution facts, operation,
  effect facts, refs, output trust/redaction)`
- `RequiredAction(action_id, source_turn_id, kind, allowed_tools, effect_ceiling, status,
  evidence_call_ids, acceptable_exits, blocked_reason)`

handler 内部业务返回值叫 `ToolHandlerOutcome`，只由 Executor 消费，不进入 provider 历史、Memory/Audit
或 required-action 销账，因此不构成第二个 ToolResult。所有字段的逐项白话说明见
[tool-runtime-unification.md](tool-runtime-unification.md#46-字段白话字典)。

## State Transitions / 状态转换

```text
received -> normalized -> validated -> authorized
-> approval_pending | approved
-> sandbox_prepared -> running
-> succeeded | failed | unknown
-> reconciled -> persisted -> projected
```

`ask` 和 `deny` 不进入 handler，但仍从当前 ToolCall 生成 terminal ToolResult。取消在任何未开始状态
转为 cancelled；已开始的副作用无法证明终态时转为 unknown。

## File Writes / 文件写入

- 新增设计、功能规格和任务记录。
- 不新增第二个工具结果目录；大输出继续写现有 owner-scoped tool output artifact 路径。
- 不新增第二个 operation store；沿用当前 store schema，并仅在缺字段时做可迁移扩展。
- required actions 随现有 effective contract/run/compact 事实写入，不创建独立任务文件。

## Test Plan / 测试计划

- 合同单测：唯一 Schema/hash、ToolCall/ToolResult、ActionDecision、required action 状态机。
- fake tool：allow/ask/deny、handler_executed、失败阶段、sandbox、重放与 unknown 核对。
- fake LLM/provider：四种 tool_choice、native 流/非流、正文协议违规、孤儿配对。
- replay/compact：required action、operation 和 refs 不丢失。
- 并发/取消：无冲突只读并行、写冲突/审批屏障、停止后不再启动。
- 普通中文真实模型场景与完整静态/pytest/发布门。

## Acceptance Criteria / 验收标准

- [x] AC-001: FR-001/002，工具定义、Schema 和运行对象各只有一个当前权威入口。
- [x] AC-002: FR-003/004，代码与测试证明只有一种 canonical ToolCall/ToolResult 且无孤儿。
- [x] AC-003: FR-005/006，run 固定协议，四种 tool_choice 在两个 native adapter 均正确下发。
- [x] AC-004: FR-007，开放 required action 在无证据时不能成功，完成/阻断出口均有测试。
- [x] AC-005: FR-008/009/011，未审批危险动作执行为零，路径/URL/命令/沙箱失败阶段准确。
- [x] AC-006: FR-010，重复 operation 不重复副作用，unknown 不自动重放。
- [x] AC-007: FR-012，完整大输出可由 ref 查回且上下文投影有界。
- [x] AC-008: FR-013/014，无冲突只读并行，冲突写串行，取消真实收口。
- [x] AC-009: FR-015，旧字段、入口、prompt 和陈旧测试删除，搜索审计为零。
- [x] AC-010: FR-016，Audit/Memory 消费的 canonical 字段通过契约测试。
- [x] AC-011: focused/full pytest、Ruff、doc-sync、strict code-size、diff、clean-package 全部有证据。
- [x] AC-012: 普通中文真实模型验收覆盖规格中的关键正反场景。

## Risks / 风险

- 大范围类型迁移可能影响 Audit/Memory 消费者：先固定 canonical 字段并用适配期测试锁定，随后同批
  删除旧定义，不复制业务语义。
- provider 对 `tool_choice` 方言不同：内部只保留一个 ToolChoice，差异限制在 provider adapter。
- 受控并发可能暴露非线程安全工具：默认 serial，只有工具明确声明且资源无冲突才并行。
- 文本协议若仍有真实生产调用方：只保留 run 前显式选择的隔离 adapter，并记录使用方和删除条件；
  不作为 native 失败 fallback。

## Rollback / 回滚方案

每个实施批次在同一批内接通新主链并删除被替代入口。发生回归时，回退该完整批次的代码与迁移，
而不是恢复双轨 fallback；operation 数据使用向前兼容读取，任何未知副作用保持 fail-closed。
