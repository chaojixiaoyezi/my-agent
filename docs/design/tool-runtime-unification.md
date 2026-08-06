# Tool Runtime Unification / 工具运行时统一设计

Status: Complete; full/static/package and ordinary-Chinese real-model evidence recorded  
Feature: [FEATURE-20260804-tool-runtime-unification.md](FEATURE-20260804-tool-runtime-unification.md)  
Task: [TASK-20260804-1913-tool-runtime-unification.md](../tasks/completed/TASK-20260804-1913-tool-runtime-unification.md)

## 1. 决策摘要

my-agent 的工具系统只保留这一条权威链：

```text
当前用户请求
-> EffectiveContractSnapshot.required_actions
-> ToolRuntimeSnapshot
-> ToolChoice
-> ProviderToolProtocolAdapter
-> canonical ToolCall
-> ActionPolicy (allow | ask | deny)
-> ToolExecutor
-> OperationLedger / EffectReconciliation
-> canonical ToolResult
-> required-action settlement
-> CompletionGate
-> 模型依据真实结构化事实生成最终回复
```

四条不可退让的边界：

1. 只有 provider 的结构化工具事件，或 run 开始前明确选中的隔离文本 adapter，能创建 ToolCall。
   用户/模型正文、代码块、网页和文件内容没有执行权。
2. `ToolRuntime` 同时拥有给模型看的 spec、运行策略和 handler 绑定；Schema、availability、exposure
   与执行不能从不同注册表重建。
3. `ActionPolicy` 是副作用前唯一决策口；`ToolExecutor` 是 approval、sandbox、handler、账本、核对、
   持久化和投影的唯一编排口。
4. required action 只防止“无证据假完成”。它不代替安全授权、不扫描目录、不判断业务质量，也不把
   普通 `task_progress` 开放项变成硬门。

## 2. 当前证据边界

本设计基于 2026-08-04 的实际 checkout，而不是项目名印象或索引摘要。

| Project | Checkout | HEAD | 精读入口 |
| --- | --- | --- | --- |
| 会话运行时 | `/Users/example/study-agent/all-agent/会话运行时-main` | `578c1b2230288104041e880a86d0f7f3a5ca6e47` | `会话运行时-rs/tools/src/responses_api.rs`, `tool_spec.rs`, `tool_executor.rs`; `会话运行时-rs/core/src/tools/router.rs`, `registry.rs`, `orchestrator.rs`, `parallel.rs`; `会话运行时-rs/execpolicy/src/decision.rs` |
| 长期助手 | `/Users/example/study-agent/all-agent/长期助手-agent-main` | `0a62610f10cc34d696b2239b2c69fa1ba0f1ca63` | `tools/registry.py`, `tools/tool_result_storage.py`, `agent/tool_executor.py`, `agent/tool_dispatch_helpers.py`, `run_agent.py` |
| 终端交互 | `/Users/example/study-agent/all-agent/终端交互-main` | `6b25ab68b103a269d6555c3daedc532630c67544` | `src/types/permissions.ts`, `src/tools/BashTool/bashPermissions.ts`, `pathValidation.ts`, `readOnlyValidation.ts`, `src/utils/shell/readOnlyCommandValidation.ts`, `src/utils/permissions/filesystem.ts`, `src/utils/hooks/ssrfGuard.ts` |
| 通道运行时 | `/Users/example/study-agent/all-agent/通道运行时-main` | `9cf12734e675a1ec63abb09bf65e4f6d76835a1e` | `packages/tool-call-repair/src/contracts.ts`, `promote.ts`, `stream-normalizer.ts`; `src/agents/model-runtime-policy.ts`, `tool-policy-pipeline.ts`, `embedded-agent-runner/effective-tool-policy.ts`, `session-transcript-repair.ts` |

四份合同索引 workbook 只用于定位当前源码，不作为行为结论：
`会话运行时_contract_code_files.xlsx`、`长期助手_contract_code_files.xlsx`、
`终端交互_contract_code_files.xlsx`、`通道运行时_contract_code_files.xlsx`。

### 2.1 吸收与明确不照抄

| Reference | 吸收 | 明确不照抄 |
| --- | --- | --- |
| 会话运行时 | runtime 对象绑定 spec/handler/exposure/parallel；typed provider item 才创建 ToolCall；router/registry/orchestrator 窄腰；approval->sandbox->retry 集中；读写锁式并发屏障；cancellation token 与单一 terminal lifecycle | Rust/platform 专属实现；绕过 my-agent owner scope 或 operation store 的执行；无对应能力的沙箱升级 |
| 长期助手 | 带 generation 的稳定 registry snapshot；availability 与 schema 同快照；按原顺序的安全并发分段；交互审批窗口串行；结果顺序稳定；单轮输出预算一次结算 | 按工具名硬编码 reader/writer 白名单；正则猜危险命令；并行和串行两套重复 dispatch；归档失败后把完整结果静默截断 |
| 终端交互 | typed allow/ask/deny 与 reason/evidence；deny、ask 在 allow 前；解析真实 command/argv/operator/redirection；无法证明安全时 ask；原路径+解析路径/符号链接同时校验；DNS 结果参与 SSRF 决策 | 巨大命令白名单整包移植；模型 classifier 作为硬授权；用命令分类替代 OS sandbox；把 loopback 放行策略无条件复制 |
| 通道运行时 | run 前解析 provider/model runtime policy；按层过滤工具并保留排除来源；历史 ToolCall/ToolResult 保守配对；provider stream 边界修复不污染业务 handler | native 模式把独立正文提升成 ToolCall；跨不明确 occurrence 猜 ToolResult 归属；让修复层成为 native/text 的静默切换器 |

## 3. 迁移前基线诊断（历史，不是当前运行方式）

本节保留开工时看到的问题，用来解释为什么要删除旧链。下列 `ToolSpec`、多套
ToolCall/ToolResult、正文提升和动态协议回退均已迁移或删除；当前实现以第 4 节之后的对象和
第 11 节完成表为准。

### 3.1 重叠权威

当前至少有四组重叠的调用/结果合同：

| Current | Purpose now | Problem |
| --- | --- | --- |
| `backends/tool_ir.py::ToolCall/ToolResult` | provider 历史回放 | 字段过少，无法承载 schema/run/operation/refs；与执行合同分离 |
| `action_protocol.py::ToolCallEnvelope/ToolCallResultEnvelope` | Registry/action envelope | 又一套 call/result，字段名和状态不同 |
| `contracts/tool_protocol_v2.py::ToolCallEnvelope/ToolResultEnvelope` | gate/归一化协议 | 无 provider source、turn、schema hash；与 action envelope 同名不同义 |
| `tooling/models.py::ToolExecutionResult` | 真实执行结果 | 生命周期最完整，但不是 provider/history 的唯一 ToolResult |

`ToolSpec` 同时保存 `parameters`、`parameter_schema`、`required_parameters` 和可选 `input_schema`；
`requires_approval` 与 effect/approval gate 重复；`effect`、timeout、output policy 和软提示全塞在同一类。

### 3.2 协议和完成旁路

- `response_decision._tool_calls_from_response()` 在 native 没有结构化 block 时仍解析正文，再由
  `text_tool_call_promotion.py` 提升执行。
- `native_tool_use_active()` 可按 backend/model 子串在 run 内隐式回退 text，没有 run capability record。
- backend `generate()` 没有 `tool_choice`，四种选择无法成为 provider 请求事实。
- `_no_tool_calls_decision()` 没有读取当前请求的 required action；普通正文会直接结束。
- `execute_tool_round()` 仍消费拍平 dict 并按工具名硬编码编排例外；调用/结果先在 text/archive 路径记录，
  native IR 再双写。

### 3.3 已有优势必须保留

- 请求级 `ToolRuntimeSnapshot` 已用于目录、搜索、native schema 与执行收窄。
- `tool_spec_schema.py` 已有有限 JSON Schema 验证、类型纠正和可信补参。
- path/URL/command、effect、approval binding、rate limit、guardrail 已有结构化 gate。
- operation coordinator 已能 claim、replay、unknown、reconcile、persist，且失败关闭。
- `handler_executed`、`failure_stage`、`effect_outcome/source_ref` 已由宿主维护。
- owner/write boundary、bwrap、输出归档/信任/脱敏、compact 配对与 operation verification 已存在。

统一工作是在这些优势上删除重复表示和冲突入口，不重写业务工具。

## 4. 目标数据模型

### 4.1 ToolModelSpec

```python
@dataclass(frozen=True)
class ToolModelSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    schema_hash: str
    hints: ToolModelHints
```

`schema_hash = sha256(canonical_json(input_schema))`。`use_cases`、`avoid_when`、`keywords`、`examples`
进入 `ToolModelHints`，只影响目录/检索，不参与任何安全决策。目录参数说明从 Schema properties 生成，
不再维护参数描述副本。

### 4.2 ToolRuntimePolicy

```python
@dataclass(frozen=True)
class ToolRuntimePolicy:
    effect_resolver: EffectResolver
    approval_policy: ApprovalPolicy
    sandbox_policy: SandboxPolicy
    idempotency_policy: IdempotencyPolicy
    timeout_policy: TimeoutPolicy
    concurrency_policy: ConcurrencyPolicy
    resource_scopes: ResourceScopePolicy
    output_policy: OutputPolicy
    availability_policy: AvailabilityPolicy
    input_policy: ToolInputPolicy
    promotes_task: bool
```

`availability` 是 runtime 的无副作用检查；policy 只声明如何判断。`requires_approval` 删除：审批完全由
resolved effect、resource scopes、owner boundary 和现有 binding 决定。

### 4.3 ToolRuntime 与 Snapshot

```python
@dataclass(frozen=True)
class ToolRuntime:
    model_spec: ToolModelSpec
    runtime_policy: ToolRuntimePolicy
    handler: ToolHandler
    availability: ToolAvailability
    exposure: ToolExposure

@dataclass(frozen=True)
class ToolRuntimeSnapshot:
    run_id: str
    runtimes: tuple[ToolRuntime, ...]
    available_tool_names: frozenset[str]
    unavailable_tools: tuple[tuple[str, str, str], ...]
    allowed_tools: frozenset[str] | None
    owner_type: str
    snapshot_hash: str
```

Registry 拒绝重复名称。prompt、tool search、provider schema、ActionPolicy 和 Executor 都只拿 snapshot；
后续 progressive disclosure 只能从 snapshot 中做减法/短时曝光，不能回读全局注册表扩权。

### 4.4 Canonical ToolCall

```python
@dataclass(frozen=True)
class ToolCall:
    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    source_protocol: str
    schema_hash: str
    run_id: str
    turn_id: str
    attempt_id: str
    required_action_id: str = ""
    operation_id: str = ""
    idempotency_key: str = ""
```

provider call id 是 opaque，但在本地以 `(run_id, attempt_id, call_id)` 形成 scoped identity。ToolCall
创建时必须精确命中 snapshot runtime 和 schema hash；未知工具也生成 terminal protocol/authorization
ToolResult，但绝不进入 handler。

### 4.5 Canonical ToolResult

```python
@dataclass(frozen=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: str
    content_blocks: tuple[ContentBlock, ...]
    error_code: str = ""
    error_category: str = ""
    retryable: bool = False
    recommended_action: str = ""
    recovery_hint: str = ""
    handler_executed: bool = False
    failure_stage: str = ""
    duration_ms: int = 0
    operation: OperationFacts | None = None
    effect_outcome: str = ""
    effect_source_ref: str = ""
    refs: tuple[ToolResultRef, ...] = ()
    output_trust: str = "runtime"
    output_redaction: str = "default"
    metadata: dict[str, Any] = field(default_factory=dict)
```

`status` 统一为 `succeeded|failed|cancelled|skipped|approval_required|unknown`。模型所需的字符串预览
由 `ToolResult.render_for_prompt()` 和 output projection 生成，不再成为主存储字段。Memory/Audit 需要的 `ok/status`、hash、size、
artifact/source refs 都从此对象投影。

### 4.6 字段白话字典

下面不是另一份 Schema，而是给开发者看的字段释义。机器权威仍是当前 dataclass 和
`ToolModelSpec.input_schema`。

#### 给模型看的工具说明

| 字段 | 白话作用 | 能否决定安全/执行 |
| --- | --- | --- |
| `ToolModelSpec.name` | 工具的唯一名字，例如 `read_file`。provider、快照和 handler 都必须用同一个名字。 | 只作为身份；还要命中当前快照。 |
| `description` | 告诉模型这个工具做什么。 | 不能；文字不是授权。 |
| `input_schema` | 唯一完整 JSON Schema：允许哪些参数、类型、必填项、枚举和嵌套结构。 | 能；provider 展示和执行前校验共用它。 |
| `schema_hash` | `input_schema` 规范化后的 SHA-256 指纹。 | 能；调用携带的指纹不一致就拒绝。 |
| `hints` | 检索和选工具的软提示集合。 | 不能。 |
| `hints.category` | 工具归类，用于目录和渐进披露。 | 不能。 |
| `hints.use_cases` | 哪些情况适合用。 | 不能。 |
| `hints.avoid_when` | 哪些情况通常别用。 | 不能。 |
| `hints.keywords` | 工具搜索关键词。 | 不能。 |
| `hints.examples` | 给模型看的参数示例。 | 不能；示例不会绕过 Schema。 |

#### 运行策略

| 字段 | 白话作用 |
| --- | --- |
| `effect_resolver` | 根据已校验参数判断本次是只读、会修改，还是危险动作；shell 使用结构化命令分析。 |
| `effect_resolver.default_effect` | 参数没有命中明确变体时采用的保守副作用等级。 |
| `effect_resolver.by_parameter` | 某个参数的精确取值如何改变副作用等级，例如 `action=read` 与 `action=update`。 |
| `effect_resolver.strategy` | `declared` 表示按声明映射；`command` 表示解析真实 shell 结构。 |
| `effect_resolver.command_parameter` | `command` 策略具体读取哪个参数。 |
| `approval_policy.mode` | 哪个副作用等级必须有绑定审批：`never/dangerous/mutating/always`。它取代旧 `requires_approval` 布尔值。 |
| `sandbox_policy.mode` | 是否继承、强制或明确不使用 OS 沙箱；策略无法满足时 handler 不启动。 |
| `idempotency_policy.scope` | 空表示只读/无需业务幂等；`operation` 按本次操作去重；`business` 用工具定义的稳定业务键去重。 |
| `timeout_policy.seconds` | handler/operation 的运行时限；审批等待不算在里面。 |
| `concurrency_policy.mode` | `serial` 串行、`parallel_safe` 可在资源不冲突时并行、`barrier` 前后都要等。 |
| `resource_scopes.mode` | 资源身份从参数生成、静态声明，或明确没有资源范围。 |
| `resource_scopes.parameter_names` | 从哪些参数提取资源身份，例如文件路径或进程 session。 |
| `resource_scopes.static_scopes` | 工具固定占用哪些资源。 |
| `output_policy.refs` | 工具结果中哪些引用字段应被正式保留。 |
| `output_policy.trust` | `runtime` 是本机运行事实；`external_data` 是网页/MCP 等外部不可信数据。 |
| `output_policy.redaction` | 投影前采用普通脱敏还是源码友好脱敏。 |
| `availability_policy.mode` | 可用性由无副作用 handler 探针检查，或声明为始终可用。可用性不是授权。 |
| `input_policy.internal_parameters` | 只允许宿主在 handler 入口补入、绝不展示给模型的参数。 |
| `input_policy.safe_parameter_defaults` | 工具作者逐字段确认的无歧义缺省值；Schema 自带 `default` 不会自动取得执行权。 |
| `input_policy.trusted_parameter_bindings` | 从当前 run、写边界或 Registry 的结构化事实补入/核对参数，并记录来源。 |
| `input_policy.local_file_url_parameters` | 哪些 URL 参数允许按受控本地文件路径检查。 |
| `promotes_task` | 该工具真的进入执行时，是否把普通会话提升成持久任务；它不代表成功。 |

#### 工具、快照和协议

| 对象/字段 | 白话作用 |
| --- | --- |
| `ToolRuntime.model_spec` | 当前工具给模型看的唯一说明。 |
| `ToolRuntime.runtime_policy` | 当前工具副作用、审批、沙箱、幂等、并发、输入和输出规则。 |
| `ToolRuntime.handler` | 真正做业务动作的代码；只有 `ToolExecutor` 能进入它。 |
| `ToolRuntime.availability` | 当前进程是否具备依赖和配置，以及不可用错误码/原因。 |
| `ToolRuntime.exposure` | 是否对模型可见、允许哪些 owner 类型看到。 |
| `ToolRuntimeSnapshot.run_id` | 这份工具面属于哪次 run。 |
| `runtimes` | 本 run 已授权、可用并绑定 handler 的不可变工具集合。 |
| `available_tool_names` | `runtimes` 名字的校验性集合；两者不一致就拒绝建快照。 |
| `unavailable_tools` | 授权范围内但当前不可用的工具及错误事实，不会混入可执行集合。 |
| `allowed_tools` | 上层结构化授权给出的原始白名单；`None` 表示没有额外收窄。 |
| `owner_type` | 主代理、任务本地代理等运行主体类型。 |
| `snapshot_hash` | run id、owner 和工具名/Schema/可用性组成的快照指纹，防止目录与执行漂移。 |
| `ProviderToolCapability.provider` | 后端类型，例如 OpenAI-compatible 或 Anthropic-compatible。 |
| `endpoint` | 实际工具请求端点身份，不包含密钥。 |
| `model` | 实际模型名。 |
| `stream` | 本 run 是否使用流式返回；流式与非流式能力分别记录。 |
| `native_supported` | 探测是否明确证明这个组合支持原生工具事件。 |
| `evidence` | 这条能力事实来自哪次探测或明确配置。 |
| `observed_at` | 能力被观测的时间。 |
| `ToolProtocolSnapshot.run_id` | 协议快照所属 run；必须与工具快照相同。 |
| `source_protocol` | 本 run 唯一协议，只能是 `native` 或显式 `text`。 |
| `capability` | 上述 provider/endpoint/model/stream 的能力事实。 |

#### 调用、决策和结果

| 对象/字段 | 白话作用 |
| --- | --- |
| `ToolChoice.mode` | `auto` 可自行选择、`required` 必须选一个、`specific` 必须选指定工具、`none` 禁止调用。 |
| `ToolChoice.tool_name` | 仅 `specific` 使用的精确工具名。 |
| `ToolChoice.reason` | 宿主为什么做这个选择，供审计；不交给模型改写权威。 |
| `ToolCall.call_id` | provider 给出的调用 ID；text adapter 则由整块内容稳定生成。 |
| `tool_name` | 本次要调用的工具，必须命中快照。 |
| `arguments` | provider 的结构化参数对象；正文不能填写这个字段。 |
| `source_protocol` | 调用究竟来自原生事件还是显式 text adapter。 |
| `schema_hash` | 创建调用时绑定的唯一 Schema 指纹。 |
| `run_id/turn_id/attempt_id` | 分别定位整次运行、当前对话轮、当前模型尝试，避免跨轮串账。 |
| `required_action_id` | 本调用准备给哪条动作义务提供证据；为空不表示自动满足任何义务。 |
| `operation_id` | 一次业务动作的稳定身份；默认由 run/attempt/call 构造，也可由可信业务规则替换。 |
| `idempotency_key` | operation ledger 用来识别重复副作用的键。 |
| `ActionDecision.status` | `allow` 才能进 handler；`ask` 请求审批；`deny` 明确拒绝。 |
| `reason_codes` | 决策原因的稳定错误码列表。 |
| `evidence` | Schema、快照、路径、命令、审批等宿主检查事实。 |
| `approval_request` | `ask` 时要展示/绑定的精确审批请求。 |
| `sandbox_plan` | handler 启动前要落实的 OS 隔离计划。 |
| `resolved_effect` | 本次具体调用最终解析出的副作用等级。 |
| `resource_scopes` | 本次读写的规范化资源身份，用于冲突和并发判断。 |
| `ToolHandlerOutcome` | handler 内部业务返回值，只在 Executor 内存在；不是 provider、历史、Memory 或销账权威。 |
| `ToolHandlerOutcome.ok/output` | handler 自报业务成功与原始正文；`output` 一律按不可信数据处理。 |
| `ToolHandlerOutcome.error_code/reported_error_code` | 前者必须是宿主已注册的控制错误码；后者仅保留工具/提供方原始报码。正文里的同名 JSON 无效。 |
| `ToolHandlerOutcome.effect_outcome/effect_source_ref` | handler 能证明的 `not_started/unknown` 和核对引用；成功确认由 Executor 结合 operation 写入。 |
| `ToolHandlerOutcome.handler_executed/failure_stage/duration_ms` | 宿主边界补写的内部执行事实，handler 正文不能伪造。 |
| `ToolResult.status` | 唯一终态：成功、失败、取消、跳过、待审批或结果未知。 |
| `content_blocks` | 给模型的有界 text/json/ref 块，不是完整原始存储。 |
| `error_code/error_category` | 统一错误码及大类。 |
| `retryable/recommended_action/recovery_hint` | 由错误分类表生成的重试事实、下一步类型和有界修复提示。 |
| `handler_executed` | 是否真的越过宿主入口进入 handler。拒绝、审批和 replay 可为 false。 |
| `failure_stage` | 失败属于 protocol、authorization、validation、runtime_gate、execution、effect_reconciliation 或 persistence。 |
| `duration_ms` | 从 Executor 接收调用到形成终态的耗时。 |
| `operation` | 修改/危险动作的账本事实；只读结果通常为空。 |
| `effect_outcome` | `confirmed` 已证明，`not_started` 已证明没发生，`unknown` 可能发生但无法确认。 |
| `effect_source_ref` | 支撑副作用结论的稳定引用。 |
| `refs` | 完整原文、artifact、source 等耐久引用及 hash/size。 |
| `output_trust/output_redaction` | 模型投影的信任和脱敏边界。 |
| `metadata` | 宿主附加的 action decision、输入来源、hash/size 等结构化事实；不接受输出正文反向写入。 |
| `ToolOperation.operation_id/idempotency_key/args_hash` | “哪次动作、怎样去重、参数是否完全相同”。 |
| `ToolOperation.status` | operation 的运行/成功/失败/unknown 终态。 |
| `handler_executed/effect_outcome/effect_source_ref` | 账本层保存的真实执行与副作用证据。 |
| `attempt_count/replayed` | 实际尝试次数以及本结果是否来自重放。 |
| `result_ref` | 稳定 `tool-operation://<run>/<operation>` 结果引用，重放仍保持一致。 |
| `created_at/updated_at` | operation 创建和最后更新时刻。 |
| `ToolResultRef.kind/ref` | 引用类型和稳定地址。 |
| `sha256/size_bytes/summary/mime_type` | 完整对象的指纹、大小、有界摘要和媒体类型。 |

## 5. Provider Capability、协议和 ToolChoice

### 5.1 Run 前固定能力

`ProviderToolCapability` 记录 provider、endpoint identity（不含 secret）、model、stream、协议、支持的
tool-choice modes、probe source 和 capability hash。能力在创建 ToolRuntimeSnapshot 前解析，整次 run
不可变；provider 重试必须复用同一能力，不得因一轮没有 tool call 就切 text。

当前仓库正式配置为 native，仓库内没有生产配置选择 text。运行规则已经收口为：

- `tool_protocol: native` 必须由当前 provider + endpoint + model + stream 探测明确证明原生能力；否则
  以 `TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE` 失败关闭。
- `tool_protocol: text` 只能由部署配置直接选择。旧的“native 再按模型名子串改成 text”字段已删除，
  拼错协议也直接报错。
- `TextToolProtocolAdapter` 只接受整条 assistant 响应由一个或多个完整、顶层、裸 JSON
  `[TOOL_CALL]...[/TOOL_CALL]` 块组成。块前后夹正文、Markdown/反引号、坏 JSON、缺闭合标记或半截参数
  都产生一次 `PROTOCOL_VIOLATION`，调用数和 handler 执行数均为 0。
- 流边界可以提前发现半截/超大写入并停止继续收取危险形状，但不能把已经看到的前缀提升执行；最终解析
  必须等 provider 宣告整条响应结束，以便发现尾随正文。
- native 模式只认 provider 结构化事件；正文里的任何文本工具块都清空本轮 calls 并进入一次有界纠偏，
  永远不会触发 text adapter。

text adapter 的保留边界是“显式配置的非原生端点”，不是 native 的兼容兜底。删除条件：支持清单中的
所有生产 endpoint 都有 native 流/非流真机证据，并连续两个发布周期没有显式 text 配置或运行记录；届时
同批删除 adapter、文本 prompt、stream 文本边界、配置枚举和专属测试。

### 5.2 ToolChoice

```python
class ToolChoiceMode(Enum): AUTO, REQUIRED, SPECIFIC, NONE

@dataclass(frozen=True)
class ToolChoice:
    mode: ToolChoiceMode
    tool_name: str = ""
    reason: str = ""
```

决策只读 open required actions 与当前 exposed runtimes：无义务为 auto/none；唯一允许工具为 specific；
多工具为 required；销账后的总结轮为 auto/none。需要审批、澄清或能力缺失时 CompletionGate 可以给出
结构化出口，不强迫模型乱调用。

OpenAI adapter 映射到 `auto|required|none|{"type":"function",...}`；Anthropic adapter 映射到
`auto|any|none|{"type":"tool",...}`。不支持某模式时 run 创建即 fail-closed 或采用事先声明的等价模式，
不能在请求后猜。

### 5.3 Native 正文违规

native response 有正文 `[TOOL_CALL]`、XML tool 标签或其他已知文本协议帧且没有对应 provider call 时：

1. 生成 host-owned `PROTOCOL_VIOLATION` 事实；
2. 最多纠偏一次，明确要求 provider-native call；
3. 再次违规则用结构化 blocked/unfinished 出口；
4. 绝不创建 ToolCall、operation 或 handler invocation。

## 6. Required Actions 与 CompletionGate

`RequiredAction` 直接属于 `EffectiveContractSnapshot`：

```python
@dataclass(frozen=True)
class RequiredAction:
    action_id: str
    source_turn_id: str
    kind: str
    allowed_tools: tuple[str, ...]
    effect_ceiling: str
    status: str
    evidence_call_ids: tuple[str, ...] = ()
    acceptable_exits: tuple[str, ...] = ()
    blocked_reason: str = ""
    description: str = ""
    success_criteria: str = ""
    no_tool_attempts: int = 0
```

状态为 `open|satisfied|unfinished|needs_user_input|approval_required|blocked`。动作来源必须是请求入口的
结构化 contract 构建器；模型正文不能新增、修改或销账。

字段白话含义：`action_id` 是这一项义务的唯一编号；`source_turn_id` 指向哪条用户轮产生了义务；
`kind` 是动作类型；`allowed_tools` 只限定哪些工具可提供证据；`effect_ceiling` 限定本义务最多允许到
哪一档副作用；`status` 是当前状态；`evidence_call_ids` 只收已配对的调用 ID；`acceptable_exits` 规定
不能成功时允许如何诚实退出；`blocked_reason` 保存机器阻断原因；`description/success_criteria` 是给模型
理解目标和成功条件的说明，不承担授权；`no_tool_attempts` 只记录无调用纠偏次数，第一次纠偏、第二次
转结构化未完成，不能无限循环。

结算规则：

- read-only：同 action 允许工具的 canonical ToolResult 必须 succeeded。
- mutating/dangerous：除 succeeded ToolResult 外，operation 必须 succeeded 且副作用证据不是 unknown。
- ask/deny/能力缺失：只按 `acceptable_exits` 转成对应非成功终态。
- 失败或 unknown 仅作为证据，不满足成功动作。
- call_id 必须与 required_action_id 绑定，不能拿旧轮或其他动作的成功结果代偿。

CompletionGate：无 open action 可自然结束；首次 open+no-call 注入一次结构化 correction；再次无调用，
按已知事实返回 unfinished/needs_user_input/approval_required/blocked。它不解析“完成了”等自然语言，
不扫描目录，不执行验收，不对普通业务质量做硬判断。

## 7. ActionPolicy

唯一接口：

```python
@dataclass(frozen=True)
class ActionDecision:
    status: str  # allow | ask | deny
    reason_codes: tuple[str, ...]
    evidence: Mapping[str, Any]
    approval_request: ApprovalRequest | None
    sandbox_plan: SandboxPlan | None
    resolved_effect: str
    resource_scopes: tuple[ResourceScope, ...]
```

固定顺序：

1. ToolCall/snapshot/schema hash 与 owner/allowed/exposure/availability。
2. normalize + JSON Schema + trusted completion。
3. effect resolver 与 resource scope resolver。
4. path canonical/resolved/symlink 和 owner/write/read boundary。
5. URL scheme、DNS/IP/SSRF 与网络策略。
6. shell AST/argv/operator/pipe/redirection/path 的确定性分析。
7. explicit deny -> ask -> allow，审批 binding 与 sandbox plan。
8. rate limit/guardrail/idempotency admission。

现有 GateDecision 可作为底层 gate 结果，但最终只由 ActionPolicy 聚合成 ActionDecision。任何底层
NEED_APPROVAL 都映射 ask；明确安全/授权缺口为 deny；多个结果取 deny > ask > allow，保留每层 provenance。

Registry/业务 handler 中仍可重复检查“实际解析路径没有越界”“进程确实在沙箱里”等同一硬事实，这是
防御纵深，不是第二个 allow/ask/deny 决策器：它们只能把已允许调用进一步拒绝，不能把 ActionPolicy 的
ask/deny 翻成 allow。`contracts/tool_adapter_readiness_contract.py` 中仍有一个名为
`requires_approval` 的外部 adapter 上线检查字段，它描述“某个外部适配器是否声明审批能力”，不是工具
定义字段、不会决定当前 ToolCall，因此不与已经删除的工具级 `requires_approval` 重复。

Shell 策略不靠命令名关键词：解析失败、变量展开无法求值、复杂 shell construct、未知重定向均 ask；
明确危险 target/越权/绕过为 deny；只有完整结构和 flags/paths 均证明只读才 allow。OS sandbox 仍是最后
硬边界，无法准备 sandbox 时不得调用 handler。

## 8. ToolExecutor 与 Operation Ledger

ToolExecutor 为每个 ToolCall 创建单一 lifecycle record：

```text
received -> normalized -> validated -> authorized
-> approval_pending | approved -> sandbox_prepared -> running
-> succeeded | failed | unknown -> reconciled -> persisted -> projected
```

要求：

- allow 前不调用 handler；ask/deny/cancel 仍生成一条配对 ToolResult。
- `handler_executed` 只在进入 handler 的宿主边界置真。
- 每个阶段只有拥有者能写 `failure_stage`；正文和 handler output 无权覆盖。
- approval wait 不消耗 handler timeout；approval binding 与 exact ToolCall/operation/owner/run 绑定。
- sandbox denial 的升级和重试只在 Executor 内发生，不由各工具自行决定。
- mutating/dangerous 先 claim operation，再执行，再持久化；持久化失败返回 unknown。
- handler_executed=false 可按错误类别修正重试；read-only 已执行通常可重试；副作用已执行必须先核对；
  unknown 永远不盲重放。

Operation ledger 继续使用现有 store，确保字段覆盖：`operation_id`、`idempotency_key`、`args_hash`、
`status`、`handler_executed`、`effect_outcome`、`effect_source_ref`、`attempt_count`、`result_ref`、
`created_at`、`updated_at`。迁移只补字段/投影，不建第二份表。

## 9. 并发与取消

第一阶段所有 runtime 默认 `serial`。串行主链和配对测试通过后启用：

- `ConcurrencyPolicy(mode=serial|parallel_safe|barrier)`；未知、危险、审批、交互为 barrier。
- resource scopes 是规范化 `(kind, access, identity)`；read/read 可并行，任一 write 且 identity 重叠即冲突。
- 按原 call 顺序构造 contiguous segments；segment 内并行，segment 间严格顺序；输出仍按原索引返回。
- 不使用 `_CONTENT_OUTPUT_TOOLS` 或 reader/writer 工具名白名单决定并发。
- approval gate 单独序列化，避免并发弹窗；单轮 output budget 在所有结果完成后只结算一次。

统一 `CancellationToken` 由 active turn 创建并进入 ToolExecutor/handler context。停止后不再 admission 新调用；
shell 终止进程组，MCP/HTTP/long poll 使用各自取消原语。已完成结果保留；未开始为 cancelled；已开始但
副作用无法证明为 unknown 并持久化。

## 10. 大输出、历史和 compact/resume

- handler 返回 raw content blocks；OutputPolicy 在 Executor 的 persisted->projected 阶段统一脱敏和归档。
- 超阈值或单轮预算超限时，完整结果先写 owner-scoped artifact，再生成 bounded preview、hash、size 和 ref。
- 归档失败不能把“完整结果已保存”伪造成成功；需要完整结果的策略返回 persistence failure/unknown。
- provider history 直接保存 canonical AssistantTurn + ToolCall + ToolResult；不再由 text archive 反向重建 IR。
- transcript repair 只做明确 occurrence 的配对；缺结果生成 synthetic failed/cancelled result，重复/歧义不猜。
- compact 只移除完整 call/result pair，并在 replacement summary 旁保留 required actions、operation facts、refs
  和 coverage；resume 从同一结构化 checkpoint 恢复。

## 11. 迁移与删除完成表

下表左列是迁移前名字。B1-B7 的代码迁移均已落地；当前只剩最终完整验证和文档证据收口。历史/存储
字段名 `parameters` 可以继续表示“某次调用实际传了什么”，但不再用来声明工具 Schema。

| 当前模块/字段/入口 | 新权威位置 | 主要调用方 | 迁移方式 | 删除批次 | 其他 Agent 影响 |
| --- | --- | --- | --- | --- | --- |
| `ToolSpec.parameters/parameter_schema/required_parameters/input_schema` | `ToolModelSpec.input_schema` | builtin/MCP/provider/catalog/gates | 逐工具迁移完整 Schema，目录从 Schema 生成 | B1 | Audit/Memory 的 tool spec 只需最小构造迁移 |
| `ToolSpec.effect/effect_by_parameter/default_mode/idempotency_scope/requires_approval/timeout/output_*` | `ToolRuntimePolicy` | gates/registry/executor | 迁移 policy builders；删除 approval bool | B1/B4 | 不改变业务 handler |
| Registry `dict[str, BaseTool]` + specs 重建 | `dict[str, ToolRuntime]` + immutable snapshot | catalog/search/provider/executor | 注册时绑定 runtime；拒绝重复 | B1 | capability tool 消费新 snapshot |
| `backends.tool_ir.ToolCall` | canonical `ToolCall` | adapters/history/compact | 扩字段并移到唯一合同模块 | B2 | Memory/Audit 消费公共字段 |
| `action_protocol.ToolCallEnvelope` | canonical `ToolCall` | registry/recovery/verification | scope 字段拍入 canonical；迁移序列化 | B2 | shared minimal wire change |
| `contracts.tool_protocol_v2.ToolCallEnvelope` | canonical `ToolCall` | gates/ledger | 删除第二归一化对象，gate 直接读 ToolCall | B2 | replay fixture 迁移 |
| `backends.tool_ir.ToolResult`、`ToolCallResultEnvelope`、`ToolResultEnvelope`、`ToolExecutionResult` | canonical `ToolResult`；内部 `ToolHandlerOutcome` | handler/executor/history/archive/memory | provider/history/销账只用 canonical；handler 的非权威业务返回值已改名，旧 dataclass 删除 | B2/B5 | Memory 字段由唯一投影提供 |
| `native_tool_use_active()` 每轮动态猜测 | `ToolProtocolSnapshot.capability` | prompt/generation/history | run 开始真实探测并固定；删除模型名子串覆盖 | B2/B7 | 无业务影响 |
| backend `tools` 无 choice | `ProviderToolRequest(tools, tool_choice, capability)` | OpenAI/Anthropic adapters | 方言仅在 adapter | B2 | 无 |
| `text_tool_call_promotion.py` | native protocol violation | response decision | 纠偏一次，不执行 | B2 | 删除旧 promotion 测试 |
| `parse_registry_tool_calls()` 混在 execution | 隔离 `TextToolProtocolAdapter` | 显式 `tool_protocol=text` 部署 | 旧 parser/JSON repair/执行入口已删除；adapter 只认完整独立帧 | B2/B7 | 配置文档说明删除条件 |
| `EffectiveContractSnapshot.effective_contract` 内无 typed action | `required_actions` 字段 | request builder/tool loop/compact | 同一 snapshot 扩展；不建任务状态 | B3 | Gateway/Audit 只透传 |
| `_no_tool_calls_decision()` 普通 break | `CompletionGate` | tool loop | open action 首次纠偏、再次结构化退出 | B3 | 不影响普通无义务聊天 |
| gates 各自返回最终执行语义 | `ActionPolicy -> ActionDecision` | Executor | 底层 gate 保留，唯一聚合与优先级 | B4 | Memory 不参与授权 |
| Registry `_execute_*`/invoke/coordinator 多层编排 | `ToolExecutor` | tool loop/direct tools | 状态机收口；handler 仅业务动作 | B5 | Audit 只观察 canonical result |
| `execute_tool_round()` 工具名例外和纯串行 | policy/resource segment executor | tool loop | 先串行，再分段并行；结果按原顺序 | B6 | Subagent tools 默认 barrier |
| thread-local interrupt checks | `CancellationToken` in execution context | shell/MCP/HTTP/process | 适配现有 interrupt source | B6 | 不改 Gateway stop 语义 |
| native IR/text archive 双写 | canonical history + archive projection | compact/resume/provider | 先写 canonical，再投影 | B6 | Memory/Audit 消费 projection |
| 旧 prompt、aliases、legacy tests | 新合同和自然中文场景 | docs/tests | 调用方迁完即删除 | B7 | 共享文档同步 |

## 12. 并行 Agent 边界

### Audit Agent

不修改 `agent/ingestion/**`、`watch_stream` 业务、Audit source/worker/verdict 语义。工具线只提供 canonical
ToolCall/ToolResult、ActionDecision 和 output/source refs；若 Audit ToolSpec 必须迁移，只改构造形状，
不改 handler、spool、ack、source_ref 或 supervision。

### Memory Agent

不修改 `memory_store/**`、`memory_archive/**`、`memory_routing/**` 的长期记忆语义和存储 schema，也不改
Memory/Persona 工具业务。Memory 从 canonical ToolResult 投影：`tool_name`、`call_id/scoped_call_id`、
`ok/status`、`error_code`、`output_hash`、`output_size_bytes`、`artifact_ref`、`source_ref`。工具线负责通用
result/ref 接线与全局 allow/ask/deny、路径/URL/命令/审批。

共享文件每次编辑前查看当前 diff；不做广泛格式化，不覆盖新的 owner 修改。发生语义冲突时优先让对应
domain 保留业务权威，工具线只保留通用执行事实。

## 13. 实施批次与每批退出条件

1. **B1 定义与 Schema（完成）**：所有注册工具已迁移到 `ToolModelSpec + ToolRuntimePolicy`，旧声明字段和
   schema compiler 已删除；快照建立时校验唯一 hash。
2. **B2 Provider/IR（完成）**：capability、ToolChoice、canonical ToolCall/ToolResult、OpenAI/Anthropic
   方言和 native violation 已接通；promotion、重复协议对象及模型名 text 覆盖已删除。
3. **B3 Required actions（完成）**：effective contract、严格结构化 assessor、settlement、CompletionGate
   和 compact carrier 已接通；assessor 自身失败也不能放行假完成。
4. **B4 ActionPolicy（完成）**：授权/schema/effect/path/URL/shell/approval/sandbox/rate/guardrail 由一个
  决策器聚合；Registry 仅保留同一硬事实的防御纵深。
5. **B5 Executor/Ledger（完成）**：唯一状态机、operation claim/reconcile/replay/persistence 和稳定
   `result_ref` 已接通；旧 execution entry、`ToolExecutionResult` 名称和直接 handler 测试入口已删除。
6. **B6 concurrency/cancel/output/history（完成）**：声明驱动分段、token、进程组/HTTP/MCP 取消、
   大输出归档、历史与 compact/resume 已接通，结果保持 provider 原顺序。
7. **B7 删除和验收（完成）**：旧代码搜索、工具 focused matrix、全量 pytest、静态门和
   普通中文真实模型矩阵已通过；当前证据见
   `validation/real_runs/tool-runtime-20260805T141123Z/report.json` 和本文的完成审计。

每批都必须“接通新链->focused test->删除旧链->搜索调用方->记录结果”，不能只新增。

## 14. 验证矩阵

除功能规格场景外，硬性搜索/结构审计包括：

- 旧 ToolCall/ToolResult/ToolExecutionResult dataclass 定义数量为零；只有 canonical ToolCall/ToolResult，
  handler 内部值明确命名为 `ToolHandlerOutcome`。
- `parameters=|parameter_schema=|required_parameters=|requires_approval=` 工具定义构造数量为零；调用历史中
  表示“实际参数”的 `parameters` 存储字段和外部 adapter readiness 的同名检查不算工具定义。
- native runtime import/引用 `text_tool_call_promotion` 数量为零。
- provider schema hash 等于 runtime validation schema hash。
- 所有 terminal ToolCall 有且仅有一个 ToolResult。
- ActionPolicy 之外没有 handler 前的最终 allow/ask/deny 聚合入口。
- ToolExecutor 之外没有 production handler invocation。
- unknown operation 自动重放路径为零。
- `tool_protocol_text_models`、协议拼写兜底、native 失败后 text 接管路径为零。
- 不完整/夹正文文本块、native 伪文本块的 canonical ToolCall 和 handler 执行次数均为零。

用户追加的强制用例采用固定 ID，保留原始普通中文输入，同时断言机器事实而不只看最终文案。首条为
`T-USER-001`：输入“已经联系印度方进行查杀和防火墙block\t态势感知恶意软件告警(SOC推送监控)”
是已经完成动作的状态汇报，不是新执行请求。验收必须证明语义评估为 informational、native
`tool_choice=none`，native/text 适配后均为 0 个 canonical ToolCall、0 次 handler 执行和 0 条 operation；
任何模型生成的伪调用都由 host choice 门拒绝。后续用户以“追加当前 Goal 测试”给出的原文、期望和
禁止项均按相同方式追加，未经用户明确同意不得弱化、删除、改写输入或用 mock 冒充真实模型验收。

`AgentRunResult.tool_runtime_evidence` 是验收和运维用的只读投影，它记录 run 快照 hash、
provider/endpoint/model/stream/capability、结构化义务判定、required actions、每轮 tool choice、
协议违规与 CompletionGate；它不参与调度、授权或执行，不是第二份权威。原生模式下
即使尚无 IR 工具历史，宿主的 required-action/协议纠偏指引也会以 provider message 送达且只送一次。
Anthropic-compatible 的辅助结构化判定只接受强制的非执行 `tool_use` 包络，使用非流式、零温度
和有界的原生通道重试；正文 JSON/XML/伪工具块不会被解析成该判定，更不会成为可执行 ToolCall。

最终命令：

```bash
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

真实模型 prompt 保持普通中文：真实运行 pytest、只问运行方法、解释危险命令、修复参数、审批拒绝、
超时 unknown、大输出、并发、取消、compact/resume、native 流/非流。结果记录 provider/model/endpoint
identity/stream/capability hash、调用和结果数量、operation 终态，不记录 secret。

## 15. 完成审计

完成审计已将 FR/AC、用户追加用例、字段、状态、删除项和强制命令映射到当前代码、测试或运行证据：

- 结构审计只找到一个 `ToolModelSpec/ToolRuntimePolicy/ToolRuntime/ToolRuntimeSnapshot`、一个 canonical
  `ToolCall/ToolResult`、一个 `ActionPolicy` 和一个 `ToolExecutor`；旧 promotion/protocol-v2/parser/
  JSON repair/registry execution 模块已删除，旧名只剩历史测试函数名或文档迁移记录。
- 合同、fake tool/fake LLM、provider adapter、replay/compact、并发/取消和安全回归已经由 focused matrix 及
  `python3 -m pytest -q --tb=short --cache-clear` 覆盖；后者到 100% 且退出码 0。
- `python3 -m ruff check agent_py_agent scripts` 通过；`check_doc_sync.py` 返回 `DOC_SYNC_PASS`；
  strict code-size 返回 `blocked=False`；offline contract matrix 返回 `ok=true/findings=[]`；
  `git diff --check` 通过。
- `scripts/run_tool_real_acceptance.py` 在 MiniMax-M2.7、Anthropic-compatible endpoint、native+stream run 上以四条
  原始普通中文输入全部通过，完整脱敏证据为
  `validation/real_runs/tool-runtime-20260805T141123Z/report.json`。macOS 没有 owner-scoped Linux `bwrap`，
  T-TOOL-REAL-001 因此按合同结构化阻断，没有用宿主命令降级或伪造 pytest 成功；三条 informational
  用例均为 `tool_choice=none`、0 ToolCall、0 handler、0 operation。
- 发布清洁度按“工作树可见性 + 制品内容”两层记录：精确 worktree 命令必须如实报告用户/
  并行 Agent 未提交修改与运行数据，实际阻断 2171 项/169,721,025 字节，不会为过门删除它们。最终
  wheel 和 sdist 均通过 artifact 模式，SHA-256 分别为
  `20d8b3d9d5f9e17aaa70c75545a2b3b2bcc8703a64fbd177af16fea2163189f0` 和
  `2db40accc13e4cf0798f343d4123e7b45a68649d8b3ed54aca394743d6fefe1c`。
