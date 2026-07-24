# Verification：结构

```text
agent/verification/
|-- project_facts.py   # 从真实项目文件发现规范验证命令，并按精确 token 分类
|-- repository.py      # owner data/verification/evidence.sqlite3 事件与状态投影
`-- runtime.py         # 共用工具执行出口的唯一接线
```

## 数据流

1. `tool_call_runtime.execute_traced_tool_call` 得到真实 `ToolExecutionResult`。
   在进入工具 registry 前，同一入口先处理 conversation task promotion 和精确 mutation workspace：
   main select 可改变全局 task，child rebase 只能改变当前 runner 的 `run_workspace` 并留下 host marker；
   随后的动态 write boundary 只信该结构化 marker，不信模型正文。
2. `runtime.py` 从 `ToolCallEnvelope.scope.root_task_id` 取得任务树身份。
3. `run_command` 只有命中项目声明的规范命令且进程真实退出时才写事件。
4. 文件工具只有返回 `ok=true` 时才登记 changed paths，并把旧状态投影为 stale。
5. 精简 `verification_evidence` / `verification_state` 随工具上下文和 archive 供主模型使用。
6. `tool_call_archive_record.py` 对其他结构化副作用证据使用显式字段白名单；当前接受
   `message_tool_delivery.v1` 的成功状态、当前 owner 标记、receipt、用户投影和附件引用，以及
   `tool_search` 的已加载工具名列表。参数审计只接受 `input_sources`、`input_coercions` 和不可逆
   `input_facts`，其中只有路径、来源引用、类型和 hash，不含任何参数值。工具搜索事实只供同一工具循环
   重建下一次模型可见 schema，不携带 Skill 正文、工具输出或权限事实。
   `_finalization_service.py` 只能从本轮成功 `send_message` archive 提取，不能从模型正文、工具名次数或
   provider 日志猜测；该证据只供 conversation source-delivery 收口，不写入 verification SQLite。

工具参数在进入上述工具出口前走同一结构：

1. typed ToolCallEnvelope 先拆出外层身份/幂等元数据；扁平执行 payload 中的 Schema 声明字段全部保留为输入。
2. `tool_spec_schema.py` 从完整 `input_schema` 或 builtin 旧声明编译唯一 runtime Schema。
3. `tool_input_completion.py` 只对缺失字段应用 ToolSpec 明示安全默认值或 Registry 可信上下文绑定；
   显式字段永不覆盖，Schema `default` 注解本身没有执行权，并输出不含原值的 `source/source_ref`。
   该账目经显式白名单进入短/长工具输出索引，归档只负责审计，不能反向参与参数补全。
4. `tool_input_schema.py` 只做无歧义类型纠正，并在路径、effect、审批和实现前返回结构化问题。
5. 参数 gate、guardrail/rate-limit 哈希和 `registry_invoke` 使用同一份 Schema 感知输入；handler 只接
   已去除外层元数据的工具参数。MCP 也走该入口，不另设宽松参数通道。

副作用工具在参数、权限、路径、approval、availability 和 effect 门都通过后，再进入一条权威执行链：

1. `action_protocol.ToolCallEnvelope` 使用 provider call id 或框架生成的精确 call id 形成
   `owner + run + operation_id`；参数相同不等于同一操作，模型参数不能改写该身份。
2. `local_storage/tool_operations.py` 用 SQLite `BEGIN IMMEDIATE` 在实现前原子占位。正式 Registry 默认
   要求该 store；不可用时在 handler 前返回 `TOOL_OPERATION_STORE_UNAVAILABLE`，裸 Registry 也不能
   静默绕开。
3. `tooling/tool_operation_coordinator.py` 对首份 claim 只调用一次 handler，并保存完整
   `ToolExecutionResult`；同一精确操作重放保存结果，不再执行。不同参数复用身份会冲突，正在执行的
   副本只返回 in-flight。
4. 持有进程死亡、跨主机 lease 过期或终态内容不可读时转为 `TOOL_OPERATION_OUTCOME_UNKNOWN`；
   因副作用可能已发生，系统禁止自动重做。runtime gate ledger 只保留审计，不再反向充当执行依据。
5. 只有 read-only 工具可做一次通用瞬时重试；mutating/dangerous 工具即使提供方错误标为 retryable，
   也不在 Registry 内盲重试。`send_message`、文件写入、shell、子代理、定时等共用这条链，不各自保存
   第二份进程内/磁盘回执。

## Owner 边界

数据库固定写入当前 `runtime_owner_root/data/verification/`。owner、thread、root task 和 project root
共同组成状态键；模型参数不能指定数据库位置，也不能通过正文改变身份。

## 修改注意

- 新验证工具必须接同一个公共工具出口，不能另建 IM hook。
- scope、exit 和 stale 只能由结构化事件决定。
- targeted 永远不能在投影层变成 full。
- 新增 archive envelope 字段必须逐字段压缩并说明消费者；不得把任意工具私有结果整包带入最终回复。
- `input_sources` 只能引用 Registry typed context 或 ToolSpec 声明；归档回放不能把它变成新参数、
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
  当前 `run_workspace` 执行。任何 child cwd 改变都不得 reopen/supersede/select 父 conversation task。
- 失败工具必须携带注册错误码；不得依赖 `ToolExecutionResult` 的 `UNKNOWN_ERROR` 兜底表达已知参数、
  scope 或资源错误。
- 新增 Schema assertion 必须同时被 canonical compiler、provider projection 和 runtime validator 支持；
  否则在注册/启动边界 fail-closed，不能只让 provider 看见而执行端忽略。
- 新增副作用工具必须声明 `idempotency_scope` 并经过上述 operation coordinator；不得读取 audit ledger
  判断“是否执行过”，也不得因参数相同自行推断为同一业务动作。
