# Verification：结构

```text
agent/verification/
|-- project_facts.py   # 从真实项目文件发现规范验证命令，并按精确 token 分类
|-- repository.py      # owner data/verification/evidence.sqlite3 事件与状态投影
`-- runtime.py         # 共用工具执行出口的唯一接线
```

Compact 不建立第二套验证链。live tool-context 与 archive 共用一个有界 `model_summary` 投影：
归档记录还保存 tool round/index、operation、failure stage、handler executed、refs 和输出信任策略。
恢复时 reducer 直接消费这份同源投影；完整正文仍只在当前 owner/task artifact，旧摘要与读取游标都不能
反向制造工具成功、授权或下一任务动作。

## 数据流

1. `tool_call_runtime.execute_traced_tool_call` 得到真实 canonical `ToolResult`；handler 的 `ToolHandlerOutcome` 只在 Executor 内转换。
   在进入工具 registry 前，同一入口先处理 conversation task promotion 和精确 mutation workspace：
   主代理从 thread sticky cwd 自动绑定当前 execution，child rebase 只能改变当前 runner 的
   `run_workspace` 并留下 host marker；随后的动态 write boundary 只信该结构化 marker，不信模型正文。
   精确写目标既可使用绝对路径，也可使用当前 thread 的 durable `owner_home` 下唯一规范的
   `tasks/...` 地址；普通相对路径、包含 `..` 的路径、跨多个 task 的路径都不会触发绑定。历史
   task/progress 菜单、`task_progress select/start` 和自然语言任务判断都不在执行链中。
2. `runtime.py` 从 `ToolCallEnvelope.scope.root_task_id` 取得任务树身份。
3. `run_command` 只有命中项目声明的规范命令且进程真实退出时才写事件。
4. 文件工具只有返回 `ok=true` 时才登记 changed paths，并把旧状态投影为 stale。
5. 精简 `verification_evidence` / `verification_state` 随工具上下文和 archive 供主模型使用。
6. `tool_call_archive_record.py` 对其他结构化副作用证据使用显式字段白名单；当前接受
   `message_tool_delivery.v1` 的成功状态、当前 owner 标记、receipt、用户投影、附件引用和有界
   `evidence_refs`，以及
   `tool_search` 的已加载工具名列表。参数审计只接受 `input_sources`、`input_coercions` 和不可逆
   `input_facts`，其中只有路径、来源引用、类型和 hash，不含任何参数值。工具搜索事实只供同一工具循环
   重建下一次模型可见 schema，不携带 Skill 正文、工具输出或权限事实。
   `_finalization_service.py` 只能从本轮成功 `send_message` archive 提取，不能从模型正文、工具名次数或
   provider 日志猜测；该证据只供 conversation source-delivery 收口，不写入 verification SQLite。
   `evidence_refs` 只把送达回执关联到同一 owner 已持久化的 Audit 记录，不复制记录正文，也不能反向
   生成工具成功、授权或验证事实。

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

工具正文进入模型前还经过一条与执行权分开的投影链：

1. `ToolRuntimePolicy.output_policy` 声明工具的最低输出边界；handler 的单次结果只能收紧，
   Executor 把有效策略写入同一个 canonical `ToolResult`，不能绕过权限/effect/operation 主链。
2. `agent_core/tool_context/reducer.py` 是 live model context 的唯一正文出口。外部数据正文先统一脱敏，
   再放入不可信数据边界；status、error code、verification facts、hash、大小与 artifact ref 保持
   结构化，不能被正文里的伪标签覆盖。
3. externalizer、compact、runtime event、机械恢复、shared context 和 handoff 只传递相同 typed
   trust/redaction 元数据，不维护第二份工具名单。完整原文仅保存在 owner/task artifact。
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
- 声明 `idempotency_scope=business` 的工具必须覆盖 `business_idempotency_key`；键只能来自 typed
  owner/request/目标和规范参数，不能含模型 call id，也不能由用户自然语言推断。能查询外部操作状态时
  才覆盖 `reconcile_operation`；没有证据就保留 unknown。
