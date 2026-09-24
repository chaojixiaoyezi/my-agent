# Verification：结构

`tool_call_archive_record.py` 两个外置输出入口均传原ToolCall的执行身份；`conversation/compact_tool_identity.py` 共用精确引用。native候选在摘要前捕获原IR配对身份，活动归档按同一四元键切分来源与保留区，裸call_id仅作展示。
第8步索引恢复补齐：externalizer 保存有界 `tool_process`，carried reader 恢复原 process 信封；投影唯一位于 `tooling/runtime_facts.py`，旧索引不推定清理成功。组件验证与真实 TUI 分开。

`tool_call_archive_record.py` 从实际 canonical `ToolCall` 向 `ExternalizeToolOutputRequest` 传递 run/attempt/turn；大小输出索引与 carried 恢复保持同一身份。`conversation/compact_tool_identity.py` 的四元引用只表达来源，不提供执行、验收或完成权威。legacy 缺字段保留并标 `uncertain`，不读取自然语言或解析 scoped 字符串来补身份。

`tooling/runtime_facts.py`接canonical handler_details，输出原verification块和有界process字段；`reducer.py`统一脱敏，`tool_call_archive_record.py`保留同一有界process，`runtime/loop_support.py`恢复后同口径展示。它们不改变执行状态或持久schema。

## 零工具续跑原生历史

`backends.response_completion.has_reasoning_content` 区分有效 typed 思考与空包；适配器返回原内容和用量。
`tool_loop.response_decision` 在 continue 时调用 `tool_ir_history.record_unexecuted_response_ir`，
追加独立 assistant 轮，不复用工具轮号，不记录未执行工具。实际工具轮和 final 沿原入口保存，
下一次请求、Compact 与取消保存共同消费这一份 IR，不另存展示文本作为模型历史。

## 渠道故障诊断

`tool_guard.loop_hints` 读取 `error_taxonomy` 的规范错误合同，明确的网络/能力不可用才进入渠道提示计数。
记录按 tool/call_id 选最新事实；不解析 stdout、错误描述或模型回复，不复制第二套错误分类表。
原工具结果、账本、审批和执行状态不变；只是避免将普通失败注入成错误的模型恢复指令。

## 工具结果正文与预览

`tool_call_archive_record.archive_tool_output_projection` 先归档原始结果，再按明确的
`output_externalized` 与 `preserve_prompt_output` 选择模型正文；仅外置结果使用归档预览。
内联和已分页读取保留整页及继续参数，之后共用脱敏/信任投影、canonical ToolResult 与 native IR。
`output_preview` 仍为有界日志字段，`projection_truncated` 描述模型正文是否缩成预览，不能用归档存在与否代替。

## 外部材料的可选阅读提示

`tool_context.external_material_order` 在 `_record_tool_call` 完成原归档后读取 canonical extract 页面元数据，
仅向同一 text/native 的 `result_rendered` 追加有界页序提示。`external_material_order` 独立默认关闭；
关闭不准备页面或当前问题。启用后复用原 `external_data/default` 脱敏和外部数据边界，拒绝摘录内的
URL 查询串，不发送 URL 字段、原调用参数、headers/body 或未读 artifact 内容。

适配器沿原 decision service/worker/调用账工作，准备至采用共用一个绝对阶段期限，页集合、归档 hash、
run/task、原权限和配置变化均使建议失效。它只返回覆盖所有原页的稳定优先级；缺数据、非选择、超时或
普通故障返回空提示，宿主取消继续传播。不产生新缓存、账本、读取或下载，不修改正式结果与 refs；
不能作为授权、工具成功或业务完成的判据。其它检索消费者仍保持原合同。

## 交付复核焦点的可选提示

`tool_context.decision_delivery_quality` 是验证事实的只读消费者：`_record_tool_call` 完成原归档后经 `_optional_result_hints`
读取同 run/task 归档信封里的 `verification_evidence` 与 `verification_state`，不访问验证 SQLite、工具输出或文件。
每个 (root, kind, scope) 只取最新事件，其后同 root 的 stale 状态记为“其后有修改”；只在 `run_command` 刚产生新事件时评估。
默认关闭；开启后外发只有脱敏当前请求与焦点别名事实，采用时只把被选焦点的编号/kind/scope/status 追加到 text/native
共用展示。它不新造验证事件、不把 targeted 说成全量、不改原结果/归档/验证账/收口，不能作为验收或完成判据。

## 长等待与完成通知

`process_session` 的 wait 使用宿主取消令牌与单调时钟，不持锁长等，默认 30/上限 600 秒。
`ProcessSessionStore` 保存不可变通知地址与已发布回执；去重仍由 ConversationStore 负责。
新增测试覆盖通知写入后崩溃、陈旧回写、隔离与显式停止；不把等待超时或退出当业务验收失败/成功。

## 重复观测

`contracts/gates/tool_guardrail.py` 保存有界实际观测并按精确门码/handler_executed 排除自身拒绝；
`tool_guard/call_guardrail.py` 从规范结果提取执行事实，`action_policy.py` 按精确调用查最近结果。
`executor.py` 将原门恢复说明写入同一失败正文，不在显示层另造解释；归档和模型历史仍保留全部拒绝。
`filesystem_read_file.py` 的行/字符失败共用非文本说明，不隐式调用视觉服务。

## 补丁文件交接

`ApplyPatchTool` 从预检冻结路径和已提交列表产生 `artifact_refs`，包含真实绝对路径及 ready/deleted 状态。
`tool_call_archive_record` 沿原工具归档写入当前 run 的 registry；删除记录只有目标当前缺失才落墓碑。
部分失败保留已提交文件，未执行目标不登记；同回执相同路径状态只登记一次。自然子代理收口读取同一账本，
不再依赖模型复述路径、解析显示名或扫描同名文件。引用和展示均不能扩大工具权限或改变执行终态。

## 家目录不是 task 目录锁

普通工具的文件安全依据为 owner home、明确禁写路径与精确授权，而非用户业务目录是否属于另一 task。
晋升只登记可恢复运行记录，不修改路径、patch 正文或 cwd。其他执行者的状态是协作/恢复事实，不是额外文件锁；
取消与 active-turn 幂等依旧在唯一执行入口校验。Audit 精确读写边界保持不变。


```text
agent/verification/
|-- project_facts.py   # 从真实项目文件发现规范验证命令并按精确 token 分类：cd 前缀、&& 串联整体为 0 时逐段通过、126/127 记环境不可用、pytest 按参数形状判范围
|-- repository.py      # owner data/verification/evidence.sqlite3 事件与状态投影
`-- runtime.py         # 共用工具执行出口的唯一接线
```

模型提示侧不另建验收状态机：`agent/model_guidance.py` 是证据与动作授权软提示的唯一正文；
`agent/agent_core/tool_model_generation.py` 只在 backend 声明真实 system 能力时传入，OpenAI-compatible 映射为
首条 `role=system`，Anthropic-compatible 映射为顶层 `system`。`PromptBuilder` 仍只负责用户任务、角色、工具
目录和当前运行事实，不再用 `# System` 标题把宿主边界伪装成 user 正文；原始 user 与 native history 保持
顺序。`agent/agent_core/native_tool_protocol.py` 只读取同一轮 `ToolRuntimeSnapshot`，把动作授权段追加给
command strategy、默认 mutating/dangerous 或参数可升为副作用的工具；纯 read-only 工具保持原说明。
context/Compact 计数使用同一 backend capability，只统计实际发送的 system 内容。两处文本只影响模型行动
与陈述，不能创建 verification event、改变状态、禁用工具或授权探测；`system_prompt_override` 只替换角色
身份，不会移除 backend 共享边界。

Compact 不建立第二套验证链。live tool-context 与 archive 共用一个有界 `model_summary` 投影：
归档记录还保存 tool round/index、operation、failure stage、handler executed、refs 和输出信任策略。
恢复时 reducer 直接消费这份同源投影；完整正文仍只在当前 owner/task artifact，旧摘要与读取游标都不能
反向制造工具成功、授权或下一任务动作。

## 数据流

1. `tool_call_runtime.execute_traced_tool_call` 得到真实 canonical `ToolResult`；handler 的 `ToolHandlerOutcome` 只在 Executor 内转换。
   在进入工具 registry 前，同一入口先处理 conversation task promotion 和精确 mutation workspace：
   主代理只从 exact request、未结束 Goal、active task 或精确旧项目写路径绑定当前 execution，child
   rebase 只能改变当前 runner 的
   `run_workspace` 并留下 host marker；随后的动态 write boundary 只信该结构化 marker，不信模型正文。
   promotion 成功会先把 mutable 外层 `RunParams.task_attributes` 同步到当前冻结的
   `ToolLoopExecuteParams.task_attributes` 投影，因此本轮第一条工作工具就能读取新建 task root；同步方向
   永远是外层权威到工具快照，不能由模型参数或 handler 结果反向覆盖。
   精确写目标既可使用绝对路径，也可使用当前 thread 的 durable `owner_home` 下唯一规范的
   `tasks/...` 地址；普通相对路径、包含 `..` 的路径、跨多个 task 的路径都不会触发绑定。历史
   task/progress 菜单、`task_progress select/start` 和自然语言任务判断都不在执行链中。
2. `runtime.py` 从 `ToolCallEnvelope.scope.root_task_id` 取得任务树身份。
3. `run_command` 只有命中项目声明的规范命令且进程真实退出时才写事件。
4. 文件工具只有返回 `ok=true` 时才登记 changed paths，并把旧状态投影为 stale。
5. 精简 `verification_evidence` / `verification_state` 固定嵌入 canonical
   `ToolResult.metadata.handler_details`，随工具上下文和 archive 供主模型使用；metadata 顶层不保留第二份
   旁路。成功验证后若发生 workspace mutation，state 记录最近 verification event ID/status 并置 stale；
   read/search 不改变 stale，只有新的真实规范验证命令产生新周期。
6. `tool_call_archive_record.py` 对其他结构化副作用证据使用显式字段白名单；当前接受
   宿主 approval gate 写入并绑定 exact permission/call/operation 的 `applied_tool_approval`；handler 私有
   metadata、模型正文和 UI 文案无权生成批准事实。该投影只供同一轮 current-turn、archive 与 Compact/恢复
   重建“本调用为何被允许”，不能反向批准另一调用。
   `message_tool_delivery.v1` 的成功状态、当前 owner 标记、receipt、用户投影、附件引用和有界
   `evidence_refs`，以及
   `tool_search` 的已加载工具名列表。参数审计只接受 `input_sources`、`input_coercions` 和不可逆
   `input_facts`，其中只有路径、来源引用、类型和 hash，不含任何参数值。工具搜索事实只供同一工具循环
   重建下一次模型可见 schema，不携带 Skill 正文、工具输出或权限事实。
   `_finalization_service.py` 只能从本轮成功 `send_message` archive 提取，不能从模型正文、工具名次数或
   provider 日志猜测；该证据只供 conversation source-delivery 收口，不写入 verification SQLite。
   `evidence_refs` 只把送达回执关联到同一 owner 已持久化的 Audit 记录，不复制记录正文，也不能反向
   生成工具成功、授权或验证事实。
7. 工具输出首次 externalize 时，大小输出索引统一保存宿主确认的 bounded `tool_execution` 与
   `tool_operation`，并记录工具所属的 exact `conversation_request_id`。child completion 信封携带同一
   turn id，background slice 用 durable task 找索引、再只从该 turn 恢复 handler/operation 事实；旧行仅
   允许同值 `request_id` 兼容。缺少
   `operation.status` 的 mutating 历史保持 unverified，`ok`、output preview 和模型正文都不能补猜成功。

主代理每一轮都能看到上述 typed verification state 和原始工具结果，并由模型决定继续修复还是给出 final。
宿主只把 operation ledger 写入 response/transcript 元数据供审计，不再用 stale、failed、Todo open 等事实
覆盖 plain final、注入隐藏返工或创建额外 model call。`project_facts.py` 仍从 manifest 发现规范验证命令；
它提供模型上下文，不拥有普通任务完成权。
分类只认一次返回码能证明的事实：开头的 `cd <现有目录> &&` 会被剥离并把 cd 目标当作 cwd；`&&` 串联只在整条返回 0 时逐段记为 passed（信封另附 `verification_evidence_chain`）；返回码 126/127 记为 `environment_unavailable`；未加引号的管道 `|`、`|&` 与后台 `&`，以及 `;`、`||`，让返回码无法归属，不记证据。

UNKNOWN 副作用、取消、越权和危险路径仍在工具执行期 fail-closed，不能因删除机器完成判官而自动重放；
显式 required action 继续由自身结构化协议裁决。区别在于：已知失败（包括用户故意要求的非零退出码）已经
完整返回模型，模型随后给出 plain final 时当前 turn 就按 会话运行时 语义自然结束。

工具失败诊断沿同一公共出口保留四个正交事实：

1. `error_code` 表示发生了什么；`failure_stage` 表示失败位于 protocol、authorization、validation、
   runtime_gate、execution、effect_reconciliation 或 persistence。
2. `handler_executed` 只回答当前调用是否进入真实实现；`duration_ms` 是统一 Registry 计时结果。
   runtime guard、owner/path/effect/availability 拒绝必须在 handler 前标 false；实现返回的业务失败标 true。
3. trace、audit、tool index、runtime ledger、compact/recovery 消费同一个 canonical
   `ToolResult` 投影；handler 内部 `ToolHandlerOutcome` 不得越过 Executor 成为历史权威，也不得从错误
   字符串、provider 文本或 IM 消息重分类。
4. 幂等重放的当前调用标 false，并保留首次执行的嵌套事实；timeout/effect unknown 进入
   effect reconciliation，不能因 `retryable=true` 盲目再次产生副作用。

工具参数在进入上述工具出口前走同一结构：

1. provider adapter 先形成 canonical `ToolCall`，外层身份/幂等元数据与参数天然分层；Schema 声明字段全部保留为输入。
2. `ToolModelSpec.input_schema` 是唯一 runtime/provider Schema；旧 compiler 和 builtin 多字段声明已删除。
3. `tool_input_completion.py` 只对缺失字段应用 `ToolRuntimePolicy.input_policy` 明示安全默认值或 Registry 可信上下文绑定；
   显式字段永不覆盖，Schema `default` 注解本身没有执行权，并输出不含原值的 `source/source_ref`。
   该账目经显式白名单进入短/长工具输出索引，归档只负责审计，不能反向参与参数补全。
4. `tool_input_schema.py` 只做无歧义类型纠正，并在路径、effect、审批和实现前返回结构化问题。
5. 参数 gate、guardrail/rate-limit 哈希和 `registry_invoke` 使用同一份 Schema 感知输入；handler 只接
   已去除外层元数据的工具参数。MCP 也走该入口，不另设宽松参数通道。

副作用工具在参数、权限、路径、approval、availability 和 effect 门都通过后，再进入一条权威执行链：

1. canonical `ToolCall` 使用 provider call id 或隔离 text adapter 生成的精确 call id 形成
   `owner + run + operation_id`；参数相同不等于同一操作，模型参数不能改写该身份。
2. `local_storage/tool_operations.py` 用 SQLite `BEGIN IMMEDIATE` 在实现前原子占位。正式 Registry 默认
   要求该 store；不可用时在 handler 前返回 `TOOL_OPERATION_STORE_UNAVAILABLE`，裸 Registry 也不能
   静默绕开。
3. `idempotency_scope=operation` 只把同一 provider call 当作同一动作；
   `idempotency_scope=business` 还要求工具从可信运行事实和规范参数生成稳定业务键，缺键在 handler
   前 fail-closed。业务键在 owner 内跨 run 去重，但不同 owner 永不共享。
4. `tooling/tool_operation_coordinator.py` 对首份 claim 只调用一次 handler，并保存完整
   `ToolHandlerOutcome` 供 Executor 生成 canonical `ToolResult`；同一精确操作重放保存结果，不再执行。不同参数复用身份会冲突，正在执行的
   副本只返回 in-flight。handler 返回不是最终成功权威；只有 completion 原子保存成功才可继续返回
   `ok=true`，保存异常统一降级为 unknown，原 handler 报告只以脱敏旁证保留。
5. 持有进程死亡、跨主机 lease 过期、终态内容不可读、mutating 工具返回 timeout，或实现明确报告
   `effect_outcome=unknown` 时，都转为 `TOOL_OPERATION_OUTCOME_UNKNOWN`；因副作用可能已发生，系统
   禁止自动重做。只有工具的结构化只读 reconciler 带非空 `source_ref` 明确证明 succeeded、failed 或
   not_started，才能保存终态或以新 generation 原子重开同一 operation。两个核对者并发时最多一个能
   重开。runtime gate ledger 只保留审计，不再反向充当执行依据。
6. 只有 read-only 工具可做一次通用瞬时重试；mutating/dangerous 工具即使提供方错误标为 retryable，
   也不在 Registry 内盲重试。`send_message`、文件写入、shell、子代理、定时等共用这条链，不各自保存
   第二份进程内/磁盘回执。
7. 同一模型轮按 runtime 的 `ConcurrencyPolicy + resource_scopes` 构造连续安全分段；只读且资源不冲突
   才并行，写冲突、审批、危险和未知调用形成 barrier，最终结果仍按 provider 原顺序记录。通用底座不从
   自然语言猜调用依赖，不实现跨外部系统 Saga。archive 与 control-plane event
   投影每项 operation/effect；compact 的语义摘要必须另保留中段非成功副作用事实，最终模型据真实
   部分结果说明完成、失败或未知。
8. 普通会话的首个 `promotes_task` 工具在进入上述 policy/operation 链前，先从结构化 conversation link
   惰性创建或复用 canonical task workspace，并原子刷新该调用的 cwd、runtime roots、write boundary 与
   旧路径 rebase。工具预算、duplicate/stale guard 和 ManagedOperationStore authority 仍只在规范化后的
   canonical call 上运行一次；纯聊天和已取消调用不因这条准备链创建 task。这样首个 handler 与同轮后续
   工具使用同一目录，而不是依赖第二次模型调用自行找回产物。

工具正文进入模型前还经过一条与执行权分开的投影链：

1. `ToolRuntimePolicy.output_policy` 声明工具的最低输出边界；handler 的单次结果只能收紧，
   Executor 把有效策略写入同一个 canonical `ToolResult`，不能绕过权限/effect/operation 主链。
2. `agent_core/tool_context/reducer.py` 是 live model context 的唯一正文出口。外部数据正文先统一脱敏，
   再放入不可信数据边界；status、error code、verification facts、hash、大小与 artifact ref 保持
   结构化，不能被正文里的伪标签覆盖。
3. externalizer、compact、runtime event、机械恢复、shared context 和 handoff 只传递相同 typed
   trust/redaction 元数据，不维护第二份工具名单。完整原文仅保存在 owner/task artifact。完整工具回执为了
   ref 解析仍进入 artifact registry，但必须标记 `artifact_role=tool_output_archive` 和
   `shell_preimage_policy=exclude`；它不是用户交付物，不能被每条后续 shell 再复制成恢复前像。
4. `read_artifact` 固定继承外部数据边界；`read_file/search_text` 只有实际读取或命中 canonical
   `work/blobs/tool_outputs/` 时才收紧。JSON wrapper 与纯文本 archive 统一按结构来源处理，不按后缀、
   文件正文或用户自然语言猜信任。

## Owner 边界

数据库固定写入当前 `runtime_owner_root/data/verification/`。owner、thread、root task 和 project root
共同组成状态键；模型参数不能指定数据库位置，也不能通过正文改变身份。

Conversation thread 的 sticky workspace、task 索引、Compact 状态、通道绑定和活动时间共用一份持久
记录，但各写入入口只原子更新自己负责的字段。消息、摘要、verbose、通道绑定和后台 observation 即使
拿到旧快照，也必须在文件锁内重新读取最新记录后合并，不能把已经切换的 `workspace_task_id` 写回旧值。

## 修改注意

- 新验证工具必须接同一个公共工具出口，不能另建 IM hook。
- scope、exit 和 stale 只能由结构化事件决定。
- targeted 永远不能在投影层变成 full。
- 新增 archive envelope 字段必须逐字段压缩并说明消费者；不得把任意工具私有结果整包带入最终回复。
- `input_sources` 只能引用 Registry typed context 或 `ToolRuntimePolicy.input_policy` 声明；归档回放不能把它变成新参数、
  owner 授权或工具执行依据。持久层只接受单行有界的 `path/source/source_ref`，不得保存来源项里的
  参数值或任意结果 envelope 私有字段。
- `scoped_call_id` 只是审计身份，不代表正文已外置；没有 `artifact_ref/source_artifact_ref` 的短输出
  不得给模型生成 `read_artifact` 提示。
- `tool_search` 只改变同一 run 后续模型调用的可见工具定义；授权、effect、owner/path/sandbox 和
  `allowed_tools` 仍由原 Tool Gateway 边界决定，归档回放不能扩大这些结构化限制。run 开始时
  `ToolRegistry.runtime_snapshot` 固定 `注册工具 ∩ owner policy ∩ allowed_tools ∩ availability`；
  文本目录、推荐区、原生 Schema、`list_tools`、`tool_search` 和最终执行共用该快照。搜索只做减法，
  子代理不能看到未授权工具的名字或说明；工具在快照后掉线时，执行入口还会无副作用复检并返回
  `TOOL_UNAVAILABLE`，不会进入真实实现。
- availability 只读结构化配置或既有资源状态：视觉/LSP 配置、Playwright 依赖与既有连接、MCP
  已握手子进程。检查本身不得发网络请求、启动浏览器/LSP/MCP 或进行业务写入；权限判断永远先于
  readiness 原因展示，二者不能互相代替。
- 子代理的父 task lineage 与 cwd 必须分开；验证/归档沿父 `conversation_task_id` 归账，实际文件边界沿
  当前 `run_workspace` 执行。任何 child cwd 改变都不得 reopen、supersede 或重新绑定父
  conversation task。
- 失败工具必须携带注册错误码；不得依赖 `ToolHandlerOutcome` 的 `UNKNOWN_ERROR` 兜底表达已知参数、
  scope 或资源错误。
- 新增 Schema assertion 必须同时被 canonical compiler、provider projection 和 runtime validator 支持；
  否则在注册/启动边界 fail-closed，不能只让 provider 看见而执行端忽略。
- 新增副作用工具必须声明 `idempotency_scope` 并经过上述 operation coordinator；不得读取 audit ledger
  判断“是否执行过”，也不得因参数相同自行推断为同一业务动作。
- 工具拥有 owner 私有 crash-window 状态时，只能通过通用 `on_operation_settled` 在权威 ToolOperation
  succeeded/failed 落库后回收；UNKNOWN 必须保留。回收失败不得改写已结算结果，幂等 replay 可重试回收。
- 声明 `idempotency_scope=business` 的工具必须覆盖 `business_idempotency_key`；键只能来自 typed
  owner/request/目标和规范参数，不能含模型 call id，也不能由用户自然语言推断。能查询外部操作状态时
  才覆盖 `reconcile_operation`；没有证据就保留 unknown。
