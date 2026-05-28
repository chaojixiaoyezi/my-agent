# 主代理运行门与交付验收文档

日期：2026-05-24

这份文档记录最近几轮主代理运行门调整。目标不是把主代理改成模板机，而是让模型保持“会自己做事”的能力，同时把真正危险或容易假完成的地方交给底层机器合同兜住。

## 开发铁律

- 真实任务 prompt 保持普通人语言：说清做什么、要求什么、放哪里、要什么产物格式。
- 不允许因为某次真实任务失败，就新增“必须先做 X 才能做 Y”的前置硬门，除非 X 是安全、权限、路径、工具 schema、审批、幂等等真实运行边界。
- 交付质量问题优先走 closeout 验收、结构化返工单、草稿验证和最终收口，不要前置成开工阶段硬阻断。
- 文件格式、产物类型、协议、MIME type 属于开放世界。映射表只能做优化路径，不能因为“不在表里”就拒绝。
- 产物定位也要按开放世界处理：`spreadsheet`、`workbook` 这类通用名字要能定位 `.xlsx/.xls/.ods` 等常见表格文件；如果合同只声明 kind 和输出根目录，系统应该找候选文件并给出明确候选，而不是把 kind 当成字面后缀。
- `artifacts[].kind` 只接受字符串；如果模型把 kind 错写成对象，系统把其中的扩展名/MIME 当 `artifact_intent` 线索处理，再从路径或扩展名推导实际 kind，不能把对象 `str()` 后写进 registry。
- 每次开发必须同步更新文档；文档没有同步的代码改动视为未收尾。
- 子代理并发身份必须走显式 `RunScope` 和 run 事件账本。`inspect_agent_tree` 只是展示和汇总，不是身份事实源；线程级上下文只能作为权限上界和兼容兜底，不能成为新链路判断“当前是谁”的唯一依据。显式 scope 与当前 runner 上下文冲突时，必须返回结构化 `scope_resolution/scope_warnings`，不能静默吞掉。

## 参考项目结论

这一轮仍按 reference-first 工作方式：

- 通道运行时 更像控制面：task/run 状态、pending tool call、stop reason 都是结构化状态；它倾向于把“没推进”转成返工/等待/阻塞状态，而不是让前置模板决定任务怎么做。
- 长期助手 更像稳定运行器：活动追踪、工具网关、恢复边界清楚；它不会要求普通任务必须先写某个专项中间文件。
- 会话运行时 更强调事件流和结构化工具调用；工具结果、状态、上下文恢复都应该能通过 refs 和事件解释。
- 通道运行时 的 run/event envelope 思路要落到每次工具调用和状态记录：每条工具结果、状态事件都应该自带 `run_id`、`parent_run_id`、`root_run_id`，而不是从共享对象上的 current 字段反推。
- 通道运行时 的 TUI 和 task registry 允许 `activeRun` 这类显示缓存存在，但状态更新按 `runId + session/runtime scope` 落账；同一个 runId 在不同 scope 下冲突时不猜。本项目对应做法是：工具响应里输出 `scope_resolution`，当前 runner 只能收窄到自己的子树，不能把外部显式 parent/root 静默当真。
- 当前清理范围已经覆盖 tree/dispatch 之外的写账入口。`subagent_message`、`capability_request`、`open_case/request_collaboration/update_*`、`submit_evidence` 在 runner 内都以当前 run 为有效身份；模型传入的其它 sender/source/requester/actor 只会进入 `ignored_explicit` 和 `scope_warnings`，不会替别的 run 写消息、能力申请或证据。
- 终端交互 的可借鉴点主要是路径、权限、只读/写入边界，适合放到工具入口硬门。

本仓库吸收的是这些通用底座，不复制它们的业务任务模板。

## 当前运行门分层

### 真硬门

这些门可以阻断，因为它们保护运行安全或防止确定性假完成：

- 工具 schema / 未注册工具：模型调用不存在工具、参数结构错，不能执行。
- 路径边界：写到 workspace 外、路径越界、symlink 越界，必须拒绝。
- URL / command 边界：私网 URL、file URL、未授权 shell 操作符，必须拒绝。
- 工具工程限流 / circuit：同 tool+args 超过窗口预算或连续失败打开 circuit 时，可以临时拒绝本次工具调用，保护外部服务和运行器。
- 审批绑定：dangerous + real action 必须绑定 approval、args_hash、run_id、operation_id。
- 幂等：mutating / dangerous 工具必须带 idempotency key，重复执行要能复用或拒绝冲突。
- open file write session：分块写文件未 finish/abort 时不能最终成功；它是写入事务保护，不是次数熔断器。
- delivery closeout：模型交卷后机器验收不通过，不能假完成。
- delivery contract doctor：合同本身不是对象、`artifacts` 结构坏、路径越界这类入口合同错误不能继续执行到假完成。

### 软提醒 / 可配置次数门

这些门不应该默认把研究类长任务卡死。它们只提醒模型“该留下阶段进展了”，或在极端情况下根据用户配置阻断。

- 工具重复失败/无进展门：同工具、同参数、同类失败或相同结果重复。
- 单代理工具预算：同一个 run_id 在滚动窗口内调用工具过多。
- 工具轮上限：一次主代理/runner 工具循环最多允许多少个工具轮，默认不限制。
- runner 失败补跑预算：子代理失败后，父代理最多还允许补派几次。
- 同 run 重派预算：同一个 run_id 失败后最多允许被重新派几次。
- 探索熔断：连续只读、搜索、抓取，没有本地进展。
- 本地进展门：closeout 已失败后仍连续只读，没有修产物。
- closeout 返工预算：模型提交验收后，同一失败重复出现。

这些门的数字统一从配置读取，`0` 表示不按次数阻断。

### 配置单一来源

运行预算只允许从 `agent_py_agent/config/agent_config.yaml` 和 `AgentConfig` 读取，不能再在调用点藏第二份默认值。现在已经收敛的参数包括：

- runner auto 并发上限。
- 后台主代理上下文裁剪、pending wake 读取条数、claim TTL 和心跳间隔。
- 工具输出外置阈值、预览长度、工具调用 payload 解析预算。
- artifact 读取预算、合同状态扫描预算、skill guard 扫描预算。
- 小型真实验收 runtime 上限、真实运行复盘 report/log 读取预算。

这些值不是新硬门，只是“读多少、展示多少、扫描多少、自动并发多少”的运行预算。改 YAML 后应该直接生效；如果某个模块需要默认值，只能回到 `AgentConfig` schema 默认，不能自己写一套隐藏数字。

## 本轮已落地调整

### -2. 相对时间上下文从“日期提示”升级成“日期范围提示”

文件：

- `agent_py_agent/agent/prompting_parts/builder.py`
- `agent_py_agent/tests/test_prompting_builder.py`

真实 GitHub 周榜 smoke 暴露了一个非工具链问题：主 prompt 里已经有 `current_local_date/current_local_time`，但模型仍把“最近一周”搜索成旧年份资料，最后生成了结构正确但时间口径错误的 XLSX。

这不是新增硬门，也不是 GitHub 专项模板。修正方式是把每轮通用 Workspace Context 补成更明确的日历上下文：

```text
current_local_date
current_local_time
current_week_range
last_7_days_range
```

并提示模型：遇到“今天、最近、近一周、本周、今年”等相对时间时，先按 `current_local_date` 换成明确日期范围，再用于搜索和报告。这样仍由模型自己搜索、判断和写产物，只是减少它从旧网页年份或训练知识年份里猜日期。

第二轮复测又暴露了同类事实链问题：模型不再用旧年份，但会根据项目名猜 GitHub 仓库地址，导致最终 XLSX 里出现 404 链接。修正仍然放在通用 prompt 上，而不是写 GitHub 专项规则：

```text
最终产物里写 URL、项目地址、论文地址、下载地址或接口地址时，
优先使用工具结果里真实出现的链接；
如果链接是你从名称推断出来的，先用网页/HTTP 工具验证可访问。
```

这不是硬门，也不要求固定工具顺序。它只是告诉模型：链接是事实，不是可以靠名字拼出来的装饰字段。

### -1. 撤销错误的路径自动产物推断

文件：

- `agent_py_agent/agent/agent_core/delivery_requirement_materializer.py`
- `agent_py_agent/agent/agent_core/main_agent_delivery_closeout_artifacts.py`
- `agent_py_agent/agent/agent_core/orchestration_create_items.py`
- `agent_py_agent/agent/agent_core/orchestration_tool_specs.py`

这轮明确撤销一个走偏方向：

```text
看到用户需求里有明确文件路径
  -> 自动写入 artifacts[].preferred_path
```

这是错误的。文件路径可能是输入、参考、搜索范围、证据来源，也可能是最终产物。系统不能替模型猜。

新的规则：

- `artifacts` 只表示用户要求创建、修改或最终交付的产物。
- 要读取、参考、搜索、对比的文件，不进入 `artifacts`。
- 只有用户明确说“结果保存到某个文件 / 生成某个文件 / 修改某个文件”时，入口物化才可以写 `preferred_path`。
- 如果外部结构化合同已经把某个 artifact 标成 `input/source/reference/read_only/evidence/search`，closeout 会把它当输入类引用跳过，不再按交付物验收。
- 批量 `items/tasks` 派工时，顶层 `required_read_paths` 不再自动复制给每个子代理。每个子代理自己的输入文件写在自己的 `item.required_read_paths`；所有子代理都需要读的公共小资料可以放在顶层或 `context_packs`。

这不是新增硬门，而是删除错误自动推断。目标是回到 长期助手/通道运行时/终端交互 更朴素的模式：父代理把任务说清楚，子代理按自己的任务读输入、写结果、回报。

### 0. 工具重复失败/无进展门改成 长期助手 式动作级返工

文件：

- `agent_py_agent/agent/agent_core/tool_call_guardrail.py`
- `agent_py_agent/agent/contracts/gates/tool_guardrail.py`
- `agent_py_agent/agent/contracts/offline_tool_contract.py`
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`
- `agent_py_agent/config/runtime_guard_config.yaml`

这条门只看结构化工具事实，不读任务自然语言：

```text
同工具 + 同参数 + 同类失败 / 相同结果
```

默认配置：

```yaml
repeat_fail_threshold: 10
terminal_block_enabled: false
```

语义：

- 第 N 次：给第一次中文返工提示，要求模型换关键词、换参数、换工具或换数据来源。
- 第 2N 次：给第二次更强提示。
- 第 3N 次之后如果还要原样调用：拦截这一次工具调用，返回合成工具结果，告诉模型必须换路。
- 默认不杀任务，模型还能继续换办法。

`terminal_block_enabled: false` 是默认值，表示只拦重复动作，不把整个任务置为 `blocked`。只有显式改成 `true`，3N 后继续重复才会把任务置为 blocked。

`repeat_fail_threshold: 0` 表示无限，不按次数拦截；系统只在 50、100 次给软提示。

分页、游标、大文件分片读取不会被误伤：如果同参数调用每次返回的结果不同，或者结果里体现 cursor/offset/rows 等真实进展，就不算无进展。

这轮也清掉了旧的合同层双轨逻辑。以前 `contracts/gates/tool_guardrail.py` 还有另一套字段：

```text
exact_failure_warn_after / exact_failure_block_after
same_tool_failure_warn_after / same_tool_failure_block_after
no_progress_warn_after / no_progress_block_after
```

这些字段会让合同层和主代理运行时出现两套口径：一边是 2/3/5 次硬拦，一边是 N/2N/3N 动作级返工。现在已经统一为 `repeat_fail_threshold` 和 `terminal_block_enabled`。

统一后的机器码也改成描述真实语义：

```text
TOOL_GUARDRAIL_REPEAT_FAILURE_BLOCKED
TOOL_GUARDRAIL_NO_PROGRESS_BLOCKED
```

它们的含义是“本次同一路径工具动作被拦，模型需要换策略”，不是“任务天然失败”。只有 `terminal_block_enabled: true` 时，调用方才可以把 3N 后的重复撞墙转成整个任务 blocked。

### 1. 工具工程限流 / circuit 配置化

文件：

- `agent_py_agent/agent/tooling/registry_runtime_gate_pipeline.py`
- `agent_py_agent/config/runtime_guard_config.yaml`

这条门是工程保护，不是任务质量门。它只看结构化工具事实：

```text
同 run + 同 tool + 同 args_hash
```

默认配置：

```yaml
tool_rate_window_seconds: 60
tool_rate_max_calls: 60
tool_circuit_failure_threshold: 3
tool_circuit_backoff_seconds: [1, 2, 4, 8, 16, 30]
tool_rate_max_records: 256
```

语义：

- `tool_rate_window_seconds`：统计窗口，默认 60 秒。
- `tool_rate_max_calls`：窗口内同 tool+args 最多调用次数，`0` 表示不限频率。
- `tool_circuit_failure_threshold`：连续多少次 retryable 失败后打开 circuit，`0` 表示不启用失败熔断。
- `tool_circuit_backoff_seconds`：circuit 打开后的退避等待秒数。
- `tool_rate_max_records`：最多保留多少条限流记录，避免账本无限增长。

默认 60 秒 60 次不会卡住“10 秒监控一次 API”的长期任务；如果长期监控或批量采集需要更高频率，可以在 `write_boundary.tool_rate_limit_policy` 里显式覆盖。限流/circuit 命中时只拒绝本次工具调用，并返回 `retry_after_seconds`，不把整个任务直接判死。

### 2. 单代理工具预算配置化

文件：

- `agent_py_agent/agent/agent_core/tool_agent_budget.py`
- `agent_py_agent/config/runtime_guard_config.yaml`

这条门按 `run_id` 统计单个代理的工具调用预算，主要用于子代理/后台 runner 防空转，不是主代理普通研究任务的质量门。

默认配置：

```yaml
tool_agent_budget_window_seconds: 600
tool_agent_budget_max_calls: 200
```

语义：

- `tool_agent_budget_window_seconds`：滚动窗口秒数，默认最近 10 分钟。
- `tool_agent_budget_max_calls`：窗口内最多多少次工具调用，`0` 表示关闭这个预算门。
- 没有 `run_id` 时跳过，避免把主代理普通聊天或不同任务混到同一个预算桶里。

这条门统计的是工具调用，不是模型回合：一次 `read_file`、`web_search`、`write_file` 都各算 1 次。窗口是滚动刷新，每次工具执行前会丢掉窗口外旧记录。

### 3. 工具轮与 runner 重派预算配置化

文件：

- `agent_py_agent/agent/agent_core/_tool_loop_service.py`
- `agent_py_agent/agent/agent_core/runner_dispatch.py`
- `agent_py_agent/config/runtime_guard_config.yaml`

默认配置：

```yaml
max_tool_rounds: 0
runner_failure_retry_limit: 2
same_run_redispatch_limit: 1
```

语义：

- `max_tool_rounds`：一次主代理/runner 工具循环最多允许多少个工具轮。`0` 表示不限制。工具轮是模型一次回复里请求的一批工具，不是单个工具调用。
- `runner_failure_retry_limit`：子代理 runner 失败后最多允许补跑几次。默认 `2` 表示第 1 次正常执行失败后，最多还可以再补跑 2 次。
- `same_run_redispatch_limit`：同一个 `run_id` 失败后最多允许被重新派几次。默认 `1` 表示同一个 run 失败后最多再派一次；`0` 表示不限制同 run 重派次数。

这两个 runner 数字都是上限，不是强制路线。父代理仍可自行判断是否重派同一个 run、拆小任务、新建子代理、自己接管或向用户汇报阻塞。系统只是在超过预算时不再自动把同一个失败 run 放入普通 runner 候选，避免父代理无意识反复撞同一路。

`dispatch_subagents` 可以不传 capability router。此时 runner 照常执行，但 runner 后置的 capability request 自动 route+rerun 会被跳过；只有显式传入 `CapabilityRouter` 时，系统才会尝试把新能力申请自动授权并重跑一次。

工具上限和最终回答的子代理事实收口只在“runner 真实跨过 dispatch”后接管。也就是说：

- `create_subagents` 只创建任务，不算执行。
- `dispatch_subagents apply=false`、dry-run、状态检查不算 runner 执行。
- 旧的“输入依赖检查 + materialize_subagent_inputs”启动修补路径已经删除。`required_read_paths` 是给 runner 的上下文/读授权提示，不再作为启动前硬依赖。
- 一旦某个 run 真正进入 runner 执行或重试，后续即使主代理又读文件、查状态、搜索，最终回答仍要按真实 `task.json` 防假完成。
- `DONE/VERIFIED` 表示 runner 已写出结果但还没通过最终收口。它仍然不算严格完成，但不再自动触发最终回答状态摘要抢答；主代理可以如实汇报“已运行、结果路径、等待收口”。只有 `FAILED/BLOCKED/TIMEOUT/CHANNEL_ERROR` 或最终收口明确 `REJECT` 这类需要修复的事实，才返回子代理状态摘要，避免口头完成掩盖落盘失败。
- 父级检查会检查路径存在；像 `source_file`、`subagent_summary` 这类非路径值只是逻辑结果键或上层字段名，不能被当成缺失文件硬拒。runner Markdown 的 `Declared Output Targets` 会展示两类 refs，但只有 `required_file_refs` 才是必须落地的文件路径。内部 `output.json` 只作为运行报告，不能单独冒充已声明的文件型用户产物。
- `dispatch_subagents` 跑完一批子代理后，不再因为所有子代理 `DONE/VERIFIED` 就立刻替主代理收口。原因是多代理任务经常需要主代理继续读取子代理产物、发现新线索、发起下一轮协作或写最终交付物。系统仍会在工具轮数耗尽、模型空响应、明确 `FAILED/BLOCKED/TIMEOUT/CHANNEL_ERROR`、最终收口 `REJECT` 等场景用真实 task 状态兜底；正常完成的一批子代理结果要先交回主代理继续判断。
- 协作意图不再作为最终收口硬门。子代理写了“需要协作 / 需要别人确认 / 跨来源验证”等内容时，系统只通过协作账本、工具调用记录和调试日志观察流程是否跑通；不能因为它没有留下 case/request/evidence ref 就直接判验收失败。
- 协作修复不再由最终收口自动补洞。协作应该在运行时由发现者发起：发现问题、请求同级/上级/匹配能力代理响应、到 deadline 汇总命中/未命中/未回复，再上报父级检查层只负责产物和安全边界，不替模型判断“此刻必须协作”。
- `list_collaboration_requests` 是只读控制面能力，默认进入子/孙代理基础工具包。响应者即使不知道 case_id/request_id，也可以按自身 `run_id/agent_name/role` 查到待响应请求；这不是业务专项工具，也不会替模型判断某个线索该怎么查。
- `collaboration://case/...`、`collaboration://request/...` 和 `collaboration://evidence/...` 是逻辑控制面引用，不是本地文件路径。最终收口会把它们当作协作证据入口，不会用文件存在性检查把它们误判成“产物路径不存在”。
- 如果 runner 已经真实调用 `raise_collaboration_event`、`request_collaboration` 或 `submit_evidence` 这类协作账本工具，父级和主代理会在 dispatch 输出、case 状态、日志和最终产物里看到这些事实；它们不再变成最终 closeout 的硬阻断。
- 已废弃方向：`collaboration_closeout`、`collaboration_intent_resolved` 和“只开 case 不算完成所以打回验收”曾经把协作当成交付验收门，导致流程里出现多层卡点。现在协作账本只记录谁发现、问了谁、谁回、谁没回、证据在哪、汇总是什么；到点带部分结果继续推进。

### 3.1 协作请求按开放世界能力路由

文件：

- `agent_py_agent/agent/subagents/services/base.py`
- `agent_py_agent/agent/collaboration/store.py`
- `agent_py_agent/agent/collaboration/tools.py`

子代理创建完成后，会把当前结构化能力登记到 `CollaborationStore`：

- 显式 `attributes.capabilities / collaboration_capabilities / provided_capabilities` 原样保留。
- `role`、`agent_name` 和 `allowed_tools` 原样保留，方便已有系统按自己的命名体系路由。
- 对常见工具只补通用别名：read/search/fetch/list 这类补 `query`，`submit_evidence` 补 `evidence_submission`，write/append/save 这类补 `write`。

这不是专项模板，也不是封闭枚举。未知工具名仍会作为能力原值登记；别名只是让普通协作请求可以写 `required_capabilities=["query","evidence_submission"]`，不用提前知道具体工具名。

因此子代理 A 可以只发：

```json
{"required_capabilities": ["query", "evidence_submission"]}
```

系统会按能力注册表找到合适的已有子代理 B。如果 A 明确知道目标，也仍然可以写 `target_agent_ids`；显式 target 优先。

`open_case` 的 thread 解析也遵守同一条原则：已有外部/长期会话 thread 优先；如果本地 runner 只有已知 `task_id/run_id`，没有飞书、微信或 CLI thread，系统会用结构化任务记录自动物化一个 `internal` thread 并绑定任务。模型猜错 `thread_id` 或 `task_id` 时，只要当前确实处在子代理 runner 内，系统会退回当前 runner 的真实 run_id；不会因为一个坏 thread/task 字段把整个协作 case 打断。

### 3.2 通用线索协作请求

新增语义：

```text
request_collaboration
  problem_statement
  observed_facts
  query_intent
  query_hints
  routing_requirements
  response_contract
  context_refs

submit_evidence
  queried_scopes
  used_query_hints
  miss_reason
  response_facts
  followup_suggestions
  query_actions
```

这不是 IP、hostname、日志、订单、数据库或告警专项。`observed_facts[].kind` 是开放世界标签，`query_hints` 是 LLM 给响应者的软查询建议，响应者可以完整查、拆分查、改写查、扩大/缩小范围或换来源。系统不把这些字段当封闭枚举，也不根据字段值写业务分支。

机器层只做四件事：

```text
1. 保存谁提出了什么问题
2. 保存有哪些线索事实和来源 refs
3. 把同一线索包交给被请求的代理
4. 要求响应者用 evidence refs、queried_scopes、limitations、matched/miss_reason 回来
```

所以一个代理发现事件后，不需要知道其他代理“是否已经有这个线索”；它只需要把线索、查询意图、软查询提示和响应要求写进协作请求。目标代理由 LLM 显式指定或系统按能力/来源候选匹配；响应代理再按自己的职责和工具决定怎么查。

### 3.2.1 多目标请求按 responder 单独闭环

一个 `request_collaboration` 可以同时发给多个目标代理。机器语义是：

```text
request_id 一样
target_agent_ids 有多个
每个目标代理都要有自己的 evidence 或明确未命中响应
```

因此不能因为第一个目标代理提交了 evidence，就把整个 request 当成 completed。当前规则：

- `pending_requests_for_agent()` 只会跳过“当前代理自己已经响应过”的 request。
- 多目标 request 在所有目标都有 evidence 之前，`case_status.pending_request_count` 仍会保留缺口。
- 某个 responder 提前调用 `update_collaboration_request(status=completed)` 时，如果其他目标还没响应，系统只记录 partial completion，并把 request 保持为 pending。
- 所有目标都提交 evidence 后，request 才进入 completed 计数。

这条规则解决的是通用协作账本问题，不是 IP/日志专项：无论线索是 IP、文件 hash、订单号、用户 ID、异常指标、自然语言片段还是一组开放字段，只要一个请求点名多个响应方，就必须按响应方身份闭环。

### 3.2.2 协作线索进入 runner prompt

响应代理不能只看到“有人叫你协作”，还要看到结构化线索包。runner prompt 会给 targeted request 展示：

```text
problem_statement
observed_facts
query_intent
query_hints
routing_requirements
response_contract
context_refs
```

这些字段不是封闭枚举，也不是硬编码业务模板。它们只告诉响应代理“问题是什么、线索来自哪里、可以怎么查、回答需要带哪些结构化证据”。实际查法仍由 LLM 和可用工具决定。

### 3.2.3 阶段 0-8：任意发现者触发协作，而不是固定协调员

这轮把“无固定协调员”的协作链路补成通用控制面：

```text
任意子/孙代理发现需要协作
  -> raise_collaboration_event 一步打开 case 并创建 request
  -> target_agent_ids 明确点名，或 required_capabilities 自动匹配候选
  -> 响应代理用 list_collaboration_requests / case_status 找到待办
  -> 响应代理查询自己的数据源，submit_evidence 提交 refs-first 证据
  -> update_collaboration_request 标记完成、阻塞或需要换路
  -> 主代理或上级读取 case_status / inspect_agent_tree 后判断收口、改派或升级
```

阶段落点：

- 阶段 0：批量创建子代理时，顶层 `output_files/output_refs/artifact_refs` 不再自动继承到每个子任务。它们是父级最终交付目标，不是每个 worker 的输出路径。item 自己显式声明的输出仍然保留。
- 阶段 1：新增 `raise_collaboration_event`，把“发现协作事件”压成一个通用工具动作，避免模型只在 `output.json` 写 `collaboration_required` 却没有 case/request。
- 阶段 2：`request_collaboration` 继续支持显式 `target_agent_ids` 和按 `required_capabilities` 匹配，不写 IP、日志、API、数据库等专项字段。
- 阶段 3：响应者已经有 `list_collaboration_requests` 和 runner targeted request 注入；它可以不知道 case_id，也能发现点名给自己的请求。
- 阶段 4：不知道目标时先按能力路由；路由错了用 `reroute_collaboration_request`，不在自然语言里说“我觉得应该找谁”。
- 阶段 5：临时查询 worker 不需要新专项协议。上级或响应代理可以继续用 `schedule_child_subagents` 派短生命周期 worker，并把 `case_id/request_id/context_refs` 放进普通结构化上下文；worker 完成后提交 evidence refs。
- 阶段 6：后台主代理和长期会话只读 `case_status`、`inspect_agent_tree`、wake signals 和线程消息来判断是否需要 LLM 汇报/升级，不把 watch 巡逻变成默认 dispatch。
- 阶段 7：协作 request 已经点名多个响应者时，隐式 dispatch 可以按 `collaboration_auto_dispatch_max_runners` 一次唤醒多名 responder。普通 dispatch 仍保持默认宽度；只有待响应协作账本触发这个放宽，避免协作证据串行排队。
- 阶段 8：`create_subagents` 会把父级原始任务摘要作为有界 `parent_task_directive` context pack 传给直接子代理。这不是专项模板，也不是硬门；它只是防止主代理把 item goal 写短以后，子代理忘记“任意发现者要发起协作、输出放哪、用户限制是什么”等父级要求。
- 阶段 9：规模测试按 5/10/20 子代理逐级放大；没通过小规模前不跑大规模，避免把偶然成功当成架构成功。
- 持续记录走偏反例：固定协调员、事后猜产物、共享最终输出路径、全部子代理 DONE 就自动收口，这些都不要再作为默认路线。

补充修复链路：

- 最终收口不再因为 child 写了“待协作”但没有 case/request/evidence ref 而创建协作补洞 repair。协作是运行时动作，不是验收失败类型。
- repair worker 只处理普通产物损坏、脚本没跑、文件缺失、输入缺失等真实验收问题；不要用 repair worker 去补“协作应该发生但没发生”。
- repair contract 的通用要求是：读取 failure refs、保留原始完整验收目标、必要时修复产物。它不再要求在 repair run 内补 collaboration refs。
- `current_turn_run_state` 会把最终收口 REJECT 的 run 单独放进 `final_closeout_rejected_run_ids`，并把 `next_action` 改成 `resolve_final_closeout_rejected_refs`，真实动作以 `final_closeout_repair_advice.suggested_tool_call` 为准。这样不会同时提示“再跑一次 acceptance”，也不会把所有 REJECT 都强行导向 repair worker。
- 协作意图识别不再作为硬验收逻辑存在。运行时可以保留日志或观察指标，帮助复盘“模型是否该发起协作却没有发起”，但这些指标不能卡住子代理验收或主代理 closeout。
- `dispatch_subagents` 接受 `dispatch_run_ids`/`subagent_run_ids`/`dispatch_subagent_ids`/`dispatch_subagent_run_ids` 作为 `run_ids` 的兼容别名。真实模型经常直接照抄 `create_subagents` 返回的 `dispatch_run_ids` 字段，或把目标放进 `orchestration.subagent_run_ids`；这属于工具协议自描述一致性问题，不是专项任务逻辑。
- `create_subagents` 的 `output_files/output_refs/artifact_refs` 可以是字符串列表，也可以是对象列表，例如 `{"output_path": "...", "description": "..."}`。系统只把对象里的路径字段持久化为文件合同，不会把整个对象字符串当成“必须存在的文件名”。这也是开放世界协议容错：对象可以带描述、来源、用途等扩展字段，验收层只硬查明确的本地路径。
- `context_manifest` 如果是普通说明文本，例如“请仔细阅读 source_01.txt”，不会被输入依赖门当成启动前必须存在的文件路径。只有 `required_read_paths/input_refs/input_files` 等结构化字段、对象里的输入语义字段、列表形式 refs，或整段就是纯路径列表的字符串，才会变成硬输入依赖。这样模型可以把自然语言 brief 放进 context_manifest，而不会因为一句中文说明生成 `阅读source_01.txt` 这种假缺失路径。
- 顶层 dispatch 不再把“协作未落账”提升成 `raise_collaboration_event` suggested tool。发现者需要协作时，应该在自己的运行中直接发起；如果没发起，测试和日志应指出 runtime/prompt/工具暴露问题，而不是通过验收补开 case。
- `requires_collab=true`、`collaboration_request={...}`、`coordination_request={...}` 这类字段只能作为运行时观察信号，不能变成“没有 case/request/evidence ref 就不能完成”的硬门。
- 瘦身 runner prompt 会展示短版 `context_packs`，包括 `parent_task_directive` 和 sibling roster。完整包仍保存在 `context_bundle.json`，prompt 里只放摘要，避免上下文膨胀。
- 父级共享 brief 只来自读取类工具结果，例如 `read_file/read_artifact/web_search/web_fetch/web_extract/http_request/search_text/list_files`。`write_file` 这类写入回声不会再传给新 child，避免主代理误写的草稿最终报告污染子代理判断。
- `open_case` 只是开协作房间，`request_collaboration` 才是发请求，`submit_evidence` 才是回证据；这只是账本语义，不再作为验收卡点。空 case 可以被日志标注为“可能没推进”，但不能挡住任务流程。
- `open_case` 工具结果可以给下一步建议，帮助模型少走弯路；建议是软提示，不是必须按这一路走的硬约束。
- 当前轮次内仍为 open 的空 case 不再触发 closeout 打回。case 状态用于复盘和继续调度：谁没回、谁超时、是否要部分收口，由模型和上级根据任务上下文判断。
- 协作窗口对外只推荐两种基础状态：`open` 表示仍在收集，`close` 表示窗口到点或上级已决定带已有材料继续推进。`close` 不是“任务成功”，只是“这轮协作收集结束，可以带部分证据、未回复名单和不可达目标继续”。
- `request_collaboration` / `raise_collaboration_event` 会把模型可见名字解析成真实 `run_id`。目标可用时，工具结果给出 `suggested_dispatch_tool_call`，并明确使用 `dry_run=false` 唤醒响应者；目标不可达时，工具结果和 `case_status` 都会列入 `unavailable_targets`，不再让发现者一直等。
- 多目标 request 的完成判断按“每个显式目标都有响应证据”计算。代理名、run_id 可以互相映射，但共享角色（例如 worker）和过宽基名（例如 Agent-1 退化成 Agent）不能拿来证明所有目标都已响应；否则一个代理提交 evidence 会把同类代理全都误算成完成。
- 多目标 request 不允许无限等全员。`deadline_at/deadline_seconds` 到期后，未响应目标会进入 `timed_out_request_ids` 和 `missing_responder_agent_ids_by_request`，但不再算硬 `blocked_request`。主代理可以基于已有 positive/negative evidence 形成部分结论，明确列出未响应者、缺失证据和限制，再决定继续补派、换路、升级或收口。
- `request_collaboration` / `raise_collaboration_event` 没有显式 deadline 时，会使用 `collaboration_default_deadline_seconds` 自动补一个相对截止时间。默认 120 秒；配置 0 表示不自动补。这样普通协作不会因为模型忘写 deadline 而永久等待。
- 当多目标证据已经齐了但 case 仍为 open，系统可以在 case 状态里提示“可汇总/可标 resolved”，但不再通过 closeout 强制要求先更新 case 状态。
- 当 deadline 已到但仍有 responder 未回，`case_status` 读取时就能看到 timed out/missing responder；后台 `CollaborationCoordinator.tick()` 会把 case 状态推进为 `close` 并写 observation/wake。模型可以按部分证据推进，不能无限等待，也不能因为有人未回就让流程死卡。
- `dispatch_subagents` 对模型公开的首选执行开关是 `dry_run`：`dry_run=true` 只预览，`dry_run=false` 才真实推进 runner。旧的 `apply/execute_runners` 继续兼容，但新 prompt、工具建议和协作唤醒都不再让模型同时猜三套开关。
- `dispatch_subagents` 的顶层响应不再优先展示 `collaboration_closeout` 或把 `next_action` 改成 `continue_collaboration_or_dispatch_pending_requests`。协作信息如果需要展示，应作为只读状态/日志/账本摘要，不覆盖普通 dispatch 结果。
- 最终收口不再把“需要协作但没调用协作工具”提升为顶层 `suggested_tool_call`。如果测试发现模型漏协作，应修 runtime 工具暴露、prompt 简化或自动协作触发，而不是在验收阶段补开 case。
- `dispatch_subagents` 会把 `final_closeout_repair_advice`、`suggested_tool_call`、`next_action` 放在长 `records` 前面。真实模型或 live prompt 只读工具结果前段时，也能先看到普通修复或继续调度建议，不会因为建议埋在几万字记录后面而误走慢路。协作 case 不再由 closeout 兜底补开；发现者需要协作时，应在自己的运行中直接记录 case/request。
- 协作意图文本检测曾经用于硬验收，这是走偏的设计。后续如果保留文本检测，只能作为调试日志或指标，不得因为自然语言里出现或没出现某个词而判定任务失败。
- 产物路径合同会去掉 product root 已经包含的相对前缀。例如 product root 是 `.../outputs/discoveries`，模型声明 `outputs/discoveries/a.json` 时，最终检查路径应是 `.../outputs/discoveries/a.json`，不能拼成双层 `outputs/discoveries/outputs/discoveries/a.json`。
- 已废弃：`create_subagents items[]` 不再因为多个子代理声明同一个输出 ref 而拒绝创建，也不再要求用 `dependencies/required_read_paths` 建流水线等待。共享写入风险应由普通写入工具、append-only case/event log 和最终 closeout 暴露，不在创建阶段阻塞协作流。

### 3.2.4 走偏记录：保留为教训，不再复用

前几轮为了快速让 11 子代理样例“看起来跑通”，出现过几条偏路。这里不删记录，但明确标成反例：

- 固定协调员反例：测试里预设“第 11 个子代理就是协调员”，会绕开真实需求。真实系统里任意子代理或孙代理都可能先发现事件，发现者应该能直接上报协作事件，或让上级/主代理接管。
- 事后猜产物反例：主代理读一堆 worker 输出后自己猜哪个包含线索，容易漏掉发现者已经写在产物里的待协作意图。正确做法是发现者留下 `collaboration://case/...` 或 `collaboration://request/...`。
- 共享输出路径反例：把父级最终 `output_files` 继承给每个子代理，会让多个 worker 抢写同一个最终交付物。正确做法是 worker 写各自产物，父代理汇总后写最终产物。
- all-green 自动收口反例：`dispatch_subagents` 看到所有直接子代理 DONE/VERIFIED 就替主代理结束，会切断“读结果 -> 发起第二轮协作 -> 汇总”的链路。现在正常 DONE 会交回主代理继续判断。
- 任务专项化反例：不要为了 IP、GitHub、论文、PDF、XLSX 写固定 case。IP 只是测试数据；底层字段必须能表达任意线索、任意证据源和任意产物类型。

### 3.3 主任务自动恢复预算配置化

文件：

- `agent_py_agent/agent/contracts/main_agent_auto_resume.py`
- `agent_py_agent/config/runtime_guard_config.yaml`

默认配置：

```yaml
main_agent_auto_resume_attempt_limit: 3
```

语义：

- `main_agent_auto_resume_attempt_limit`：同一个主任务中断/失败后，Supervisor/Recovery Runtime 最多自动恢复几次。
- 默认 `3` 表示最多自动续跑 3 次。
- `0` 表示不按次数限制自动恢复，但仍然必须通过 checkpoint、lease、恢复包、产物和副作用安全检查。
- 如果某个请求显式传入 `max_auto_recovery_attempts`，以请求里的结构化值为准；否则使用这个统一运行门配置。

这条门只管“系统自动救任务的次数”，不负责判断任务质量，也不让恢复器直接回复用户。恢复器写事实和状态，新主代理或 MessageRouter 再向用户说明恢复结果。

### 4. 探索熔断配置化

文件：

- `agent_py_agent/agent/agent_core/exploration_fuse_config.py`
- `agent_py_agent/config/runtime_guard_config.yaml`
- `agent_py_agent/agent/agent_core/tool_exploration_fuse.py`

现在默认 `round_threshold=300`。模型连续 300 个工具轮都只做 read/search/fetch/list 且没有本地落地动作，才会触发最终阻断。达到 1/5、2/5、4/5 时只给中文软提示。

如果用户填 `round_threshold: 0`：

- 不按次数阻断。
- 只在 `unlimited_hint_rounds` 里的轮次提示。
- 默认提示轮次是 50、150、250。

这解决了之前 GitHub/论文类任务被 10 轮搜索过早熔断的问题。

### 5. 本地进展门配置化

文件：

- `agent_py_agent/agent/agent_core/exploration_fuse_config.py`
- `agent_py_agent/config/runtime_guard_config.yaml`
- `agent_py_agent/agent/agent_core/tool_local_progress_guard.py`

现在只保留一个参数：`local_progress_unlimited_hint_interval`。

- 默认每 10 轮提示一次。
- 它只提醒，不阻断。

这条门的定位是“提醒模型别只读不修”，不是“替 closeout 判死刑”。真正的交付结果仍由 closeout 在模型提交验收或最终回复时判断。

### 6. 显式提交验收工具

文件：

- `agent_py_agent/agent/tooling/delivery_acceptance.py`
- `agent_py_agent/agent/tooling/registry_bootstrap.py`
- `agent_py_agent/agent/agent_core/tool_loop_completion.py`

新增通用工具 `submit_for_acceptance`。模型可以在认为任务完成时显式提交验收。这个工具本身不判定成功，只记录“我要交卷了”；真正通过与否仍由 delivery closeout 读取机器合同和文件系统事实判断。

它不是专项工具，不知道 GitHub、论文、PDF、XLSX 或购物站是什么。

### 7. 隐式最终回复验收

文件：

- `agent_py_agent/agent/agent_core/tool_loop_response_decision.py`

如果模型没有调用工具、准备直接最终回复，而当前 run 有 delivery contract，系统会把这次最终回复视为隐式提交验收：

- 验收通过：返回完成。
- 验收失败：把结构化返工单放回下一轮，让模型继续修。
- 没有 delivery contract：保持普通回复行为。

这样既支持显式 `submit_for_acceptance`，也兼容其他项目常见的“模型最终回答触发系统验收”模式。

### 8. closeout 返工预算配置化

文件：

- `agent_py_agent/agent/agent_core/delivery_closeout_config.py`
- `agent_py_agent/config/runtime_guard_config.yaml`
- `agent_py_agent/agent/agent_core/main_agent_delivery_closeout_progress.py`

现在只保留两类通用预算：

- `invalid_artifacts_retry_limit`：产物齐全但内容、格式、字段、证据、质量门不合格。
- `missing_artifacts_retry_limit`：必交产物缺失、路径无效、无法定位或路径越界。

默认都是 0，表示不按次数阻断，只持续返回结构化返工单。

旧的 2、4、5、6 细分阈值已被压缩掉，避免概念重叠。

### 9. 合同 Doctor 只提示不阻断

文件：

- `agent_py_agent/agent/agent_core/main_agent_delivery_closeout.py`
- `agent_py_agent/agent/contracts/delivery_contract_doctor.py`

如果 delivery contract 自身结构坏掉，第一次验收会把 `[delivery-contract-doctor]` 放回模型上下文，告诉模型入口合同需要重新物化或修复。若下一次仍是同一份坏合同，系统不再返回 `DELIVERY_CONTRACT_DOCTOR_BLOCKED`，而是让模型按普通最终回复继续收口；doctor 报告只保留在 `.agent_delivery/contract_doctor.json` 和上下文提示里。

这不是产物质量硬门，也不是要求用户写技术 prompt；它只处理机器合同本身不可运行的诊断，不能挡住普通任务。

### 9.1 元数据质量默认不阻断

文件：

- `agent_py_agent/agent/contracts/gates/artifact_provenance.py`
- `agent_py_agent/agent/agent_core/main_agent_delivery_closeout_quality.py`
- `agent_py_agent/agent/agent_core/main_agent_delivery_fact_evidence.py`

产物 provenance 缺失、跨 run 来源、delivery quality payload 缺失、fact evidence payload 缺失、自动派生的事实口径 warning，默认都写进 closeout 报告的 `warning_codes`，不阻断最终交付。只有外部显式结构化合同声明 `enforcement: required`、`hard`、`block` 或 `blocking` 时，delivery quality / fact evidence gate 才会变成硬返工。

这样保留了诊断和复盘能力，但不会因为“账本没写齐”把一个已经生成的 xlsx/pdf/word/html 普通任务卡死。

### 10. 删除 delivery repair 独立运行门

删除文件：

- `agent_py_agent/agent/agent_core/tool_delivery_repair_guard.py`
- `agent_py_agent/agent/agent_core/tool_delivery_repair_rejection_context.py`

同时清理：

- `agent_py_agent/agent/agent_core/tool_delivery_repair_*.py`
- `agent_py_agent/tests/test_tool_delivery_repair_*.py`
- `agent_py_agent/tests/tool_delivery_repair_fixtures.py`

调整文件：

- `agent_py_agent/agent/agent_core/tool_loop_response_decision.py`
- `agent_py_agent/agent/agent_core/main_agent_delivery_closeout.py`
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`

旧逻辑在 closeout 生成结构化返工单之后，额外用 `_MAX_REPAIRS = 2` 控制“模型忽略返工动作”的次数。这个逻辑和探索熔断、本地进展门、closeout 返工预算重叠，而且比它们更硬，会让任务在仍可返工时提前 blocked。

现在不再有独立的 delivery repair 运行门。closeout 失败后，由同一份 `[delivery-contract-check]` 上下文携带：

- `failed_artifacts`
- `failed_gates`
- `recovery_actions`
- `repair_guidance`

模型仍可以先读、搜、检查上下文，也可以直接写入、修复、构建；系统只在模型提交验收或最终回复时重新 closeout。连续只读由探索熔断/本地进展门提示，同一失败重复由 closeout 返工预算处理，不再额外增加一层 delivery repair 阻断。

### 10.1 未解决工具失败只返工提示

文件：

- `agent_py_agent/agent/agent_core/tool_unresolved_runtime_issue_guard.py`
- `agent_py_agent/agent/agent_core/tool_loop_unresolved_runtime_issue_decision.py`

旧逻辑发现归档工具记录里仍有未解决的结构化失败时，会先给模型 3 次返工上下文；超过后返回 `UNRESOLVED_RUNTIME_ISSUES_BLOCKED`，把最终收口置为 `blocked`。这和 closeout 的返工循环重复，也会把“还能换工具、换路径、重写产物、重新复验”的普通问题变成硬停。

现在这条只做两件事：

- 从工具归档里提取失败工具、失败 target、`blocker_codes` 等机器事实。
- 把这些事实作为 `[tool-system unresolved-runtime-issues]` 返工提示放回下一轮模型。

它不再按次数封死任务，也不再返回 `runtime_status=blocked`。如果模型确实无法修复，应该说明真实阻塞原因和需要用户补充的信息；系统不因为这条 guard 自己终止任务。

### 10.2 删除子代理产物完整性专用硬门

删除文件：

- `agent_py_agent/agent/subagents/result_artifact_integrity.py`
- `agent_py_agent/agent/agent_core/orchestration_artifact_integrity_signals.py`
- `agent_py_agent/agent/agent_core/orchestration_artifact_integrity_repair.py`

旧逻辑有两层：

- 子代理结构化结果里声明了本地产物路径，但文件不存在时，runner 直接把任务改成 `BLOCKED/missing_artifact_refs`。
- 父级调度层看到 `artifact_integrity_failed` 时，额外生成 `artifact_integrity_repair_advice`，建议创建专门 repair worker。

这两层都和统一 closeout / 普通 runner 状态重复，而且会让子代理协作和普通交付多一条特殊卡点。现在它们都被移除：

- runner 不再因为声明的本地产物 ref 缺失而直接改成 `BLOCKED`。
- dispatch/progress payload 不再生成 `artifact_integrity_repair_advice` 或 `create_repair_child_from_artifact_integrity_refs`。
- 产物缺失、内容损坏、格式不合格等问题交给统一 closeout 或父级模型按普通任务状态处理。

### 11. 删除 bootstrap 开工物化硬门

删除文件：

- `agent_py_agent/agent/agent_core/tool_bootstrap_materialization_guard.py`

同时清理：

- `BOOTSTRAP_MATERIALIZATION_BLOCKED` 真实运行复盘分类。
- bootstrap 运行时拦截分支。
- bootstrap hard fixture 和旧测试。

原因：

bootstrap 开工物化门不是安全门，也不是最终收口门。它会把普通任务变成“必须先写某个中间 JSON / checkpoint 才能继续”，这会把 LLM 从会做事的人降成跑模板的人。

现在保留的只有 delivery contract prompt 里的软参考，例如建议尽早留下草稿或阶段产物；它不会拦截 `web_search`、`web_fetch`、`web_extract`、`read_file`、`list_files`，也不会因为还没写中间文件就停任务。

### 12. 废弃 open write session，统一到通用写入工具

本节是历史纠偏记录：`file_write_session`、`append_file`、`replace_in_file`、`write_structured_json`、`data_to_workbook`、`markdown_to_pdf` 已从模型可见工具面删除。

当前写入面只保留：

- `write_file`：原子写入完整文件；文本用 `content`，PDF/XLSX/图片/压缩包等二进制产物用 `data_base64`。
- `apply_patch`：局部修改、新增、删除或移动文本文件。
- `run_command`：在获得权限时用脚本或系统工具生成复杂格式，再由通用文件工具落盘或验收。

这次纠偏的原因：

- 分块 session 让模型必须记住 begin/append/finish/reset/abort，普通任务容易因为没 finish 被卡住。
- 固定 builder 工具会把主代理从“会做事的人”推成“跑模板的人”，并且很难覆盖 Word、PPT、视频、XML、未知格式等开放世界产物。
- 其他参考项目更常见的路线是通用写文件、补丁和命令工具，产物格式由模型/脚本/外部工具生成，系统只管路径、安全、原子写入和 closeout 验收。

现在不再存在 open-session 专属阻断、专属返工预算或专属周期提醒。大文件或复杂格式失败时，系统只返回普通工具错误、解析错误或 closeout 返工单；模型继续用 `write_file`、`apply_patch`、`run_command` 换路修复。

### 13. 只读代理树状态与 dispatch 推进分离

文件：

- `agent_py_agent/agent/agent_core/agent_tree_status.py`
- `agent_py_agent/agent/agent_core/orchestration_tools.py`
- `agent_py_agent/agent/subagents/kernel.py`
- `agent_py_agent/agent/subagents/model_task.py`
- `agent_py_agent/agent/subagents/services/session_progress.py`

新增通用只读工具 `inspect_agent_tree`。它只返回主代理、子代理、孙代理的状态树，不创建任务、不调度、不恢复、不验收，也不会清理 `has_pending_work`。

每个树节点都暴露结构化身份链：

```text
task_id
run_id
parent_task_id
parent_run_id
root_run_id
depth
agent_kind
```

子代理运行时工具调用会持续写入轻量状态：

```text
heartbeat_at
current_tool
last_progress_at
last_progress_summary
artifact_refs
blockers
```

这里的 `current_tool` 是最近观测到的工具，不表示它一定仍在执行；真实执行权仍由 runner/lease 控制。`last_progress_*` 是最近成功工具或写入进展的摘要，用来让父级检查结论。

语义边界：

- `inspect_agent_tree` / `subagent_board`：查看状态，读事实，不推进。
- `dispatch_subagents` / `dispatch_loop`：推进任务，可能创建、恢复、重派、验收。
- `DispatchNoProgressTracker`：只判断 dispatch 里的重复空转，不判断普通状态查看。

这样用户问“看看子代理在干啥”时，主代理应该调用 `inspect_agent_tree` 或 `subagent_board` 汇报状态；只有用户要求“继续推进/恢复/重派/验收”时，才调用 `dispatch_subagents`。

### 14. watch 默认观察，不默认推进

文件：

- `agent_py_agent/agent/agent_core/dispatch_params.py`
- `agent_py_agent/agent/agent_core/services/watch_service.py`
- `agent_py_agent/cli/_dispatch.py`
- `agent_py_agent/cli/subcommands_agents.py`

`WatchParams.advance` 默认是 `False`。这表示 watch 循环默认只是后台巡逻：写心跳、读取 `inspect_agent_tree` 状态、生成只读 watch 报告，不调用 `dispatch_subagents`。

只有显式设置：

```text
advance=True
```

或者 CLI 使用：

```text
subagents-dispatch --watch --advance
```

watch 才会进入旧的推进路径，调用 `dispatch_subagents`。

这条边界是为了防止“看一眼子代理状态”误变成“推进/重派/验收子代理”。`planner`、`execute_runners`、`execute_runners` 都属于推进语义；在 `--watch` 下使用这些开关时必须同时显式给 `--advance`。

例外边界：`gateway run` 和 `daemon` 是明确的常驻运行器入口，不是“看一眼状态”的入口。它们会显式传 `advance=True`，保持后台任务队列可以继续推进。换句话说，默认只读的是 watch 能力本身；真正名字和职责就是“运行器”的入口必须显式声明推进。

### 15. 长期会话和后台主代理唤醒底座

文件：

- `agent_py_agent/agent/conversation/models.py`
- `agent_py_agent/agent/conversation/store.py`
- `agent_py_agent/agent/conversation/channels.py`
- `agent_py_agent/agent/conversation/runtime.py`
- `agent_py_agent/agent/core.py`
- `agent_py_agent/agent/settings/config.py`

这一层解决的是“主代理像 会话运行时 线程一样，过一小时、重启后、换飞书/微信入口后还能接着同一个上下文工作”。它不是任务模板，也不替用户定义 GitHub/PDF/XLSX 这类专项 case。

新增的机器事实：

```text
ConversationThread  = 一段长期对话线程
MessageLogEntry     = 线程里的用户/助手消息流水
ChannelBinding      = 飞书/微信/internal 等渠道入口绑定
ThreadTaskLink      = 线程和任务的绑定
ProgressPolicy      = 定时汇报策略
```

关键边界：

- watch/scheduler 只决定“什么时候叫醒”，不是 LLM 大脑。
- `BackgroundMainAgentRuntime` 负责加载线程消息、任务绑定、代理树快照，再调用同一个 `SimpleAgent.run()`。
- 定时汇报走 `ProgressPolicy`，到期后唤醒后台主代理，回复通过 channel route 投递。
- `BackgroundMainAgentScheduler` 对同一个 thread 使用 per-thread claim，防止同一轮或跨 tick 重复烧后台主代理。claim TTL 表示“多久没有 heartbeat 就认为运行者死了”，不是任务最长运行时间；正常运行中由 heartbeat 续约，结束时先停 heartbeat 再 finish claim。
- background claim 是“执行权账本”，不是验收门。运行失败时 claim 会写成 `failed`，并保留 `last_error`、`task_id`、最近工具/进展和任务树状态桶；下一次同 thread 唤醒会把上一任 claim 摘要带进 `Recovery Snapshot`，让接手主代理先对账，而不是从自然语言里猜“上次做到哪”。
- `Recovery Snapshot` 只读 claim、agent tree 和 artifact registry，给出恢复提示，不阻断任务、不替代 closeout、不把普通质量问题变成硬门。真正的事实优先级仍是 artifact registry、agent tree、claim ledger、run/tool trace；模型文本里的路径和完成声明只能当线索。
- 默认 heartbeat 间隔按 TTL 的安全比例计算。生产默认 TTL 900 秒时约 300 秒续约一次；小 TTL 测试场景会保持间隔小于 TTL，避免第一次续约前 claim 已经过期。
- fake Feishu / fake WeChat 只用于离线验证跨渠道恢复；真实适配器以后只需要接入同一套 `ChannelBinding` 和发送接口。
- 后台主代理 prompt 仍是普通任务上下文 + 结构化事实，不要求用户写工程字段。
- 新渠道默认不会自动混入“同用户最近 thread”，避免同一个人在新群/新私聊开新任务时串上下文；只有消息入口明确走继续上下文时，才用 `reuse_latest_for_user` 恢复最近 thread。

和参考项目的对应关系：

- 长期助手 的 `SessionSource/SessionContext` 把“消息来自哪里、应该回哪里”做成结构化字段；这里用 `ChannelBinding` 和 route target 承接。
- 长期助手 cron scheduler 只做 due job 唤醒，真正回答仍交给 agent；这里的 `BackgroundMainAgentScheduler.tick()` 也是只找 due policy 并叫醒 runtime。
- 通道运行时 watch 更偏控制面守护，强调 lock、restart 和状态观察；这里继续保持 `inspect_agent_tree` 只读，调度动作留给主代理判断。
- 会话运行时 / OpenAI Agents 的 resume 测试强调“恢复时要用保存过的会话项和未完成状态”；这里用 `ConversationStore` 的 thread/messages/tasks/policies 文件账本重建上下文。

当前离线验收覆盖：

- 跨渠道同一用户从 fake Feishu 切到 fake WeChat 后恢复同一个 thread。
- 重启 `ConversationStore` 后仍能读取消息、任务绑定、渠道绑定。
- `ProgressPolicy` 到期后唤醒后台主代理，并投递 internal/fake channel 消息。
- 进程重启后重新创建 `SimpleAgent + ConversationStore + Scheduler`，仍能按旧 policy 继续汇报。
- 长后台运行期间会续租 background claim；已有未过期 claim 时，同 thread 的下一次 tick 不会重复唤醒。
- 后台 runtime 抛异常时 claim 不再伪装成 `finished`，而是记录为 `failed` 并携带可接手事实；下一次唤醒 prompt 会包含非阻断的 `Recovery Snapshot`。
- 真实本地子代理协作链路：主代理创建两个子代理，一个通过自己的 `run_id` 打开 collaboration case 并发请求，另一个提交 refs-first 证据并更新请求状态；重启后后台主代理被 case escalation 唤醒，先读 `case_status`，再读 `inspect_agent_tree`，最后把回复投回原 thread。

本地 CLI 入口：

```bash
my-agent background-main-agent message --channel internal --conversation-id local --user-id user --content "普通用户消息"
my-agent background-main-agent bind-task --thread-id <thread> --task-id <task> --goal "任务目标" --progress-interval-seconds 60
my-agent background-main-agent tick
my-agent background-main-agent status --json
my-agent background-main-agent service --interval 5 --max-cycles 3
```

语义：

- `message` 只把普通渠道消息写入 thread；显式 `--run-background` 才立即唤醒主代理。
- `bind-task` 只写 task link 和可选 progress policy。
- `tick` 执行一次 due-policy 检查，命中的策略会调用 `SimpleAgent.run()`。
- `status` 是只读控制面看板，汇总 thread、绑定任务、wake queue、待主代理处理的 observation、progress policy、协作 case 和代理树，不调用 LLM、不推进任务。
- `service` 是前台循环 tick，不创建系统守护进程，也不连接真实飞书/微信凭证。

### 15.1 证据型子代理验收

文件：

- `agent_py_agent/agent/subagents/parsing.py`
- `agent_py_agent/agent/subagents/final_closeout_empty_report.py`

这一层解决的是调查、查询、监控、状态核验这类任务：子代理可能只需要返回“查到了什么 / 没查到什么 / 证据在哪里”，不一定会生成新的 PDF、XLSX、HTML 或其它用户产物。

关键语义：

- `test_execution.json` 里 `total=0 failed=0` 不等于测试失败。它只表示“没有可执行测试项”。
- 如果子代理已有可追踪 `evidence_refs` 或 `artifact_refs`，最终收口应进入 `inspect_only`，由普通 acceptance findings 继续判断证据链，而不是直接 rescue。
- 如果模型把 `evidence_refs/artifact_refs` 放在 `[SUBAGENT_RESULT]` 顶层，没有包进 `evidence_packets`，解析层会补一个 refs-only evidence packet，避免证据在落盘时丢失。
- 内部 run 文件不能冒充证据：`output.json`、`reports/`、agent-run workspace、runner result、debrief 等仍会被 run-private 过滤排除。

大白话：

```text
查资料的子代理不一定要新写一个文件。
只要它给了可追踪证据 ref，父级检查这个证据，
而不是因为没有 pytest / command test 就说任务失败。
```

这不是专项模板：系统不关心 ref 里是 IP、日志、订单、数据库记录、网页快照还是论文片段，只看它是不是结构化 refs，且是不是当前 run 的内部报告伪装出来的证据。

### 16. 子/孙代理事件唤醒主代理

文件：

- `agent_py_agent/agent/conversation/models.py`
- `agent_py_agent/agent/conversation/store.py`
- `agent_py_agent/agent/conversation/runtime.py`
- `agent_py_agent/agent/agent_core/orchestration_tools.py`
- `agent_py_agent/agent/agent_core/orchestration_tool_specs.py`
- `agent_py_agent/cli/background_main_agent.py`

这一层解决的是长期任务里“下面的代理发现情况，主代理要不要立刻知道”的问题。它不是安全专项、API 专项或告警专项，而是通用事件控制面。

新增机器事实：

```text
ObservationEvent = 子/孙代理看到的进展、阻塞、风险、证据变化或待复核事实
WakeSignal       = 需要叫醒主代理的持久信号
```

语义：

- `raise_observation`：写一条观察事实。普通观察不会自己推进任务；如果标记 `requires_main_agent`，下一次后台 tick 会把它交给主代理判断。
- `raise_main_event`：写观察事实并创建 wake signal。`urgency=urgent` 会进入 urgent wake queue，后台 service 不必等完整 interval 结束就能醒来处理。
- `background-main-agent observe`：本地 CLI 调试入口，等价于外部通道或子代理写入一条 observation。
- `BackgroundMainAgentScheduler.tick()` 现在先处理 pending wake signal，再处理未处理的 `requires_main_agent` observation，最后才处理到期 `ProgressPolicy`。
- `BackgroundMainAgentRuntime` 会把 `Recent Observations` 和 `Pending Wake Signals` 放进后台主代理上下文，由 LLM 决定二次分析、调度或用用户习惯语言汇报。

硬边界：

- 子/孙代理上报的是结构化事实：`thread_id/task_id/event_type/summary/source_agent_id/root_task_id/evidence_refs`。
- 不从普通自然语言里猜“哪个任务、哪个线程、是否紧急”；没有 `thread_id` 时只能用已绑定的 `task_id` 反查。
- `dedupe_key` 用来避免同一个事件重复叫醒主代理。
- service 仍然不会每 60 秒烧一次 LLM；它只是短间隔查看 wake queue。没有 due policy、wake signal 或未处理 observation 时，不调用主代理。

对应到长期监控场景：

```text
孙代理每 10 秒读一次 API
  -> 没异常：只写自己的心跳/进展，主代理不醒
  -> 非紧急待复核：raise_observation(requires_main_agent=true)，下次 tick 让主代理判断
  -> 紧急事件：raise_main_event(urgency=urgent)，主代理立即醒来分析/调度/汇报
```

这吸收了 长期助手 cron wake gate 的思路：调度器只判断是否该叫醒，真正怎么说、怎么分析、是否调度，仍交给主代理 LLM。

### 17. 通用多代理协作控制面

日期：2026-05-25

文件：

- `agent_py_agent/agent/collaboration/models.py`
- `agent_py_agent/agent/collaboration/store.py`
- `agent_py_agent/agent/collaboration/coordinator.py`
- `agent_py_agent/agent/collaboration/tools.py`
- `agent_py_agent/agent/conversation/runtime.py`
- `agent_py_agent/agent/core.py`

这一层解决的是“多个主/子/孙代理怎么围绕同一件事快速协作、补证据、升级主代理”的问题。它不是日志分析专项，也不是某个 API/数据库/文件解析专项。

新增通用机器事实：

```text
AgentCapability      = 代理能力和当前状态
CollaborationCase    = 一间协作房间
CollaborationRequest = 一个代理向其他代理发出的补证据请求
EvidencePacket       = refs-first 证据包
CaseParticipant      = case 参与者记录
CaseDecision         = coordinator 的控制面决策
```

新增模型工具：

```text
open_case              打开协作 case
request_collaboration  请求其他代理围绕实体/问题补充证据
submit_evidence        提交证据 refs 和摘要
update_collaboration_request
                       更新协作请求生命周期，如 working/completed/blocked
reroute_collaboration_request
                       把阻塞或不合适的协作请求结构化改派到新目标代理
update_case_status     推进 case 生命周期并写入决策摘要
case_status            只读查看 case 状态
```

语义边界：

- `entities`、`capabilities`、`priority`、`evidence_refs` 都是开放世界字段，不做封闭枚举硬拒。
- 协作账本只保存轻量结构化事实；大日志、大文件、API 响应、数据库快照、截图、文档正文必须通过 `evidence_refs` 指向外部产物。
- `CollaborationCoordinator.tick()` 只把达到条件的 case 转成 `ObservationEvent/WakeSignal`，不调用 LLM、不替主代理做业务结论。
- `BackgroundMainAgentScheduler.tick()` 会先处理协作 case，再处理 wake/observation/progress policy。这样紧急协作事件可以复用现有后台主代理唤醒链路。
- 子代理默认获得协作工具，可以参与 case；但它们提交的是证据和请求，不直接替主代理对用户收口。
- case 的基础窗口状态是 `open/close`：`open` 表示还在收集响应，`close` 表示本轮收集结束。关闭窗口不等于交付成功，也不会替代普通任务 closeout。
- `CollaborationRequest` 现在有独立生命周期。响应者可以把请求更新为进行中、已完成、阻塞或自定义状态；`case_status` 会折叠同一 `request_id` 的 append-only 快照，返回当前状态，同时保留 `request_history_count` 方便回放。
- `case_status` 会给出 `pending/blocked/timed_out/completed/declined` 请求计数、`missing_evidence_request_ids`、`missing_responder_agent_ids_by_request`、`unavailable_target_agent_ids_by_request`、`ready_for_main_agent` 和 `requires_main_agent`。这些都是机器字段，主代理和 watcher 不需要从普通问题文本里猜。
- `status` 是开放世界字段，系统不限制只能用 open/triaged/closed 这几个值。唯一硬约束是：如果状态表达关闭、解决、完成这类终态语义，必须留下 `summary`、`decision_type` 或已有 `CaseDecision`，避免 case 无声消失。
- `case_status` 和 `update_collaboration_request` / `reroute_collaboration_request` 的工具结果进入 live prompt 时，会保留 request/evidence/ready 等高信号摘要。完整 JSON 仍可外置，但不能把关键控制字段压缩没，让模型只看到一句模糊自然语言。
- `case_status` 现在带 `rework` 和 `rework_targets`。这不是专项规则，而是从阻塞请求、缺证据请求和结构化 `request_id/status/evidence.request_id` 生成的返工目标，告诉主代理该换来源、换参数、询问响应者、补交证据或标记真实阻塞。
- `rework_targets` 会从结构化 metadata 读取 `alternate_sources_available`、`candidate_target_agent_ids` 或 `alternate_target_agent_ids`，输出 `candidate_target_agent_ids` 和 `primary_tool=reroute_collaboration_request`。这只读机器字段，不解析普通自然语言摘要。
- `reroute_collaboration_request` 是明确的换路动作：必须传 `case_id`、`request_id` 和新的 `target_agent_ids`。系统会把新目标写进 `CollaborationRequest.target_agent_ids`，并记录 `original_target_agent_ids`、`rerouted_from`、`rerouted_to` 和审计 decision，避免模型只在 metadata 或最终回复里说“已换路”。
- `CollaborationStore.overview()` 汇总所有 case 的 ready、blocked、missing evidence、evidence、participant 和 decision 计数，给真实任务前的控制面体检使用。它只输出结构化计数和 bounded `ready_cases/blockers`，不展开大证据正文。
- `BackgroundMainAgentScheduler.tick()` 对同一个 thread 的多条 pending wake signal 做批处理：同一轮只唤醒一次后台主代理，其他同 thread wake 标记为已处理，避免一个 case 的多个证据/阻塞事件把主代理重复烧多次。
- 后台主代理上下文里的可用控制动作加入 `update_collaboration_request`、`reroute_collaboration_request` 和 `update_case_status`。LLM 不只是看状态，也能在研判后把协作请求改成完成、阻塞、结构化换策略，或推进 case 生命周期。
- `tool_protocol_v2.normalize_tool_call()` 保持开放世界：旧式 flat tool call 里的业务参数 `status` 不再被误当成协议状态枚举。比如 `{"tool": "update_case_status", "status": "needs_replan"}` 会归一化为协议 `status=pending`，而把 `needs_replan` 留在工具 input 里。

对应到用户提的联动场景：

```text
某个代理发现线索
  -> open_case
  -> request_collaboration 给其他来源/能力代理
其他代理并行查询或分析
  -> submit_evidence 交 refs-first 证据包
  -> update_collaboration_request 标记完成、阻塞或继续处理中
coordinator 发现证据够了、deadline 到了、请求阻塞或请求可收口
  -> 写 observation / urgent wake
后台主代理醒来
  -> case_status + inspect_agent_tree
  -> 必要时 reroute_collaboration_request / update_collaboration_request
  -> 判断是否二次分析、继续派工、汇报用户或触发审批动作
```

能力分工点：

- 通道运行时 的 event ledger / replay 思路：协作过程以结构化事件和 refs 记录，而不是让模型在长上下文里记。
- 长期助手 的 wake gate 思路：调度器只判断是否叫醒，真正分析和汇报交给主代理。
- 终端交互 的 control request/response 思路：把跨执行体请求放到控制面，而不是让不同代理互相猜对方状态。

当前离线演练覆盖：

- 一个普通 thread 绑定一个任务。
- 两个来源代理注册能力并围绕同一 case 提交 refs-first 证据。
- coordinator 在后台 tick 中把 urgent case 升级为 wake signal。
- 后台主代理被唤醒后，真实走工具循环调用 `case_status` 读取两个证据包，再调用 `inspect_agent_tree` 查看代理树。
- 长期 watcher 场景里，响应者把 request 标为 `blocked` 后，normal priority case 也会写入 normal wake，主代理下一次醒来能看到 `blocked_request_count` 和 `ready_for_main_agent`，而不是等人工催。
- 最终回复通过 fake internal channel 投递；整个过程不依赖真实 LLM、真实飞书、真实日志平台或业务专项模板。

本地 CLI 可视化入口：

```bash
my-agent collaboration overview
my-agent collaboration overview --json
my-agent collaboration list
my-agent collaboration list --status open --json
my-agent collaboration status --case-id <case-id>
my-agent collaboration update-request --case-id <case-id> --request-id <request-id> --status blocked --summary "来源不可用，需要换策略"
my-agent collaboration update-status --case-id <case-id> --status closed --summary "证据已收口，结论已同步"
```

语义：

- `overview` 只读汇总所有 case 的 readiness、阻塞、缺证据和 ready case。它适合真实复杂任务前确认控制面是否还有等待主代理处理的协作项。
- `list` 只读列出 case 摘要：状态、优先级、请求数、待响应请求数、证据数、决策数。
- `status` 只读展示单个 case 的请求、证据、参与者和决策。
- `update-request` 只更新某个协作请求的生命周期，并追加审计决策；如果传入结构化 `target_agent_ids`，也可以同时改派请求目标；不会关闭 case，也不会触发 dispatch。
- `reroute_collaboration_request` 是模型工具，不是当前 CLI 子命令；它用于后台主代理或子代理在工具循环中把阻塞请求改派到新目标。人工 CLI 侧如果需要同类操作，后续应补独立 `reroute-request` 子命令，不要让操作者手改 JSONL。
- `update-status` 会更新 case 状态并追加一条 `CaseDecision`，用于主代理或人工把 case 标记为已研判、已解决或已关闭。
- 待响应、阻塞、完成和缺证据请求只根据结构化 `request_id/status/evidence.request_id` 计算，不从自然语言问题里猜。
- `list/status` 只是看板，不会调用 LLM、不会 dispatch、不会改变 case 状态。

### 18. 真实本地子/孙代理继承长期会话绑定

日期：2026-05-25

文件：

- `agent_py_agent/agent/agent_core/runtime_mixin.py`
- `agent_py_agent/agent/agent_core/orchestration_create_policy.py`
- `agent_py_agent/agent/agent_core/orchestration_tools.py`
- `agent_py_agent/agent/collaboration/tools.py`
- `agent_py_agent/agent/subagents/services/hierarchy_context.py`
- `agent_py_agent/agent/subagents/context_bundle.py`
- `agent_py_agent/agent/agent_core/runner_prompt_context_summary.py`
- `agent_py_agent/agent/conversation/store.py`
- `agent_py_agent/cli/background_main_agent.py`
- `agent_py_agent/cli/commands/background_main_agent.py`

这一层把前面“脚本写 observation/wake ledger”的离线验证，接到真实本地 `create_subagents -> dispatch_subagents -> run_subagent` 链路上。目标仍然不是写业务模板，而是让任意长期任务里的子/孙代理都能回到同一个主代理会话。

新增语义：

- 主代理在带 `RunParams.task_id` 的长期 thread 内调用 `create_subagents` 时，创建出的子代理会自动继承 `conversation_thread_id` 和 `conversation_task_id`。
- 继承字段会通过 hierarchy context 继续传给孙代理；孙代理不需要知道飞书/微信/internal 通道细节，只需要带自己的 `run_id/task_id` 上报。
- `raise_main_event`、`raise_observation`、`open_case` 支持用子/孙代理自己的 `task_id/run_id` 反查 thread。系统先查 conversation task binding，再查 subagent attributes；`open_case` 在本地任务无外部绑定时可创建 internal thread，不从自然语言里猜线程。
- 子/孙代理 context bundle 和 runner prompt summary 会带上最小 conversation refs：`thread_id`、`root_task_id`。这些是机器 refs，不是要求用户在 prompt 里填写工程字段。
- `background-main-agent status` 提供只读控制面体检：会话数、绑定任务数、待处理 wake、未处理 observation、progress policy、协作 case 和代理树 schema 都能一次看到。

本地验收场景：

```text
普通用户消息绑定 root task
  -> 主代理用普通自然语言目标创建本地子代理
  -> 子代理通过真实 runner 执行
  -> 子代理调用 raise_main_event
  -> 进程重启后重新创建 SimpleAgent / ConversationStore / Scheduler
  -> 后台主代理 tick 读取持久 wake signal
  -> 调用同一个 SimpleAgent.run()
  -> 通过 fake internal channel 把回复投递回原 thread
```

边界：

- 这条测试不接真实飞书/微信，不消耗真实外部 LLM；它验证的是本地长期会话、子代理继承、事件唤醒、重启恢复和投递链路。
- 真实外部通道后续只需要接 `ChannelBinding` 和 adapter；核心会话/协作/wake 账本不需要为某个渠道重写。
- `status` 和 `inspect_agent_tree` 仍然只读；看状态不等于推进任务。推进仍由主代理显式调用 `dispatch_subagents` 或后台运行器入口显式开启。

### 19. 子代理 runner 的协作控制面入口

日期：2026-05-25

文件：

- `agent_py_agent/agent/agent_core/runner_prompts.py`
- `agent_py_agent/agent/agent_core/runner_prompt_context_summary.py`
- `agent_py_agent/agent/collaboration/store.py`
- `agent_py_agent/agent/subagents/manager_runner_context.py`
- `agent_py_agent/agent/subagents/context_bundle.py`
- `agent_py_agent/tests/test_subagent_prompt_contract.py`
- `agent_py_agent/tests/test_collaboration_control_plane.py`

真实 MiniMax 小场景暴露了一个通用入口问题：协作工具已经授权给子代理，但 runner prompt 只展示工具名和普通执行合同，没有告诉模型“什么时候应该开 case、什么时候请求别的代理、什么时候提交 refs-first 证据”。结果模型容易退回成读写报告，而不是进入协作账本。

这次补的是通用可执行入口，不是专项模板：

```text
只要当前 runner 的 allowed_tools 里有协作工具
  -> prompt 才追加“协作控制面”段落
  -> 按实际授权工具解释 open_case / request_collaboration / submit_evidence / update_collaboration_request / reroute_collaboration_request / case_status
  -> 要求最终 evidence_packets 或 next_actions 引用 collaboration://case/<id>、collaboration://request/<id> 或真实 artifact/evidence refs
```

边界：

- 工具没授权时不注入这段，普通子代理 prompt 不变厚。
- 这段不解析用户自然语言，也不判断某个业务任务必须协作。
- 它只把已有结构化工具权限变成模型能理解的动作入口，避免“有工具但不会自然用”。
- 如果同一个 runner 同时有 `open_case` 和 `request_collaboration`，提示会说明：开 case 后如果还需要其他代理回应或补证据，建议继续创建 request；如果只是记录事件，可以停在 open case。
- 如果协作账本里已有 `target_agent_ids` 点名当前 runner 的未响应 request，`SubAgentManager` 会把它写进 `context_bundle.collaboration.targeted_requests`。runner prompt 会明确告诉响应者优先复用已有 `case/request`，按 `case_status -> submit_evidence -> update_collaboration_request` 处理，除非发现全新问题，不要再开第二个 `open_case`。
- `pending_requests_for_agent()` 只读结构化 `agent_id/agent_name/agent_role/target_agent_ids/request_id/evidence.request_id/status`，不会从 goal 或 question 自然语言里猜目标。它会兼容系统给展示名追加的数字后缀，例如 `Agent-B-2` 可响应发给 `Agent-B` 的请求；也支持发给结构化 role 的请求，例如 `target_agent_ids=["agent-b"]`。已有证据或已完成/拒绝的请求不会再塞给响应者。
- `dispatch_subagents` 会把“新协作请求点名某个空闲/已完成代理”视为新的待办。也就是说，B 如果先跑完了，而 A 后来才创建 request，下一轮 dispatch 会在当前 scoped / include_run_ids 范围内优先把 B 作为 responder 再跑一轮；这不是重试失败任务，而是处理新的协作输入。
- 协作结果走 collaboration store、conversation wake、case_status、evidence refs 和上级/主代理汇报；
  不再进入专门的 collaboration closeout/acceptance 硬门。不能只靠 summary 说“我已经协作”，
  但缺协作账本事实只作为日志/观察问题暴露，由父级按任务目标继续推进。

## 显式协作请求入口处理

真实场景里还有一个更靠前的问题：用户已经用普通话明确说“创建多个子代理”“联合其他代理/人员调查”，root 仍可能自己直接读资料、写报告，然后口头说完成。

旧实现曾把这类需求物化成 `orchestration_contract.v1`，并在最终回答前检查 `create_subagents` / `dispatch_subagents` 等工具事实。这个硬门后来证明太容易把协作流程卡住：模型必须踩固定步骤，否则系统会本地返工或阻断。

当前运行语义：

- 入口物化器不再生成 `orchestration_contract`。
- dispatch 后只把代理树、运行状态、产物 refs 和证据 refs 交还给主代理/父代理。
- 系统不再替主代理生成“未完成/已完成”的本地结论，也不再用协作合同挡最终回答。
- 如果任务确实需要继续协作，由主代理/父代理根据 tree/refs/dispatch 状态继续派工、查询、汇总或向用户说明阻塞。

调度工具的参数容错也挂在这里：`dispatch_subagents` 的显式目标可以写 `run_ids`、`include_run_ids`、`subagent_ids`、`target_subagent_ids`、`target_run_ids`、`agent_ids`、`child_run_ids`、`children`，也可以写 `items:[{"run_id":"..."}]`。这些字段都归一成同一组 run id。`direct_children=true` 是范围意图，不是 run id；顶层会使用本轮已经创建/触碰的子代理，runner 内部会使用当前节点的直接孩子。顶层 root 如果显式给了目标 ID，省略 `apply/execute_runners` 时默认真实推进；如果只是想 dry-run，必须显式写 `apply=false`。

历史上这里曾经按 worker/coordinator 自动拆成两波执行，后来证明这是隐藏流水线，会让父级以为“我一次指定了这些 run”，但运行时偷偷改了顺序。当前规则已经删除这层隐性拆波：`dispatch_subagents` 按父级显式 `run_ids` 顺序和并发配置推进候选。若任务确实需要“先收集再汇总”，由父级 prompt/计划明确先 dispatch 收集者，查看 tree/board/产物后再 dispatch 汇总者。

最终收口执行器支持通用存在性别名：`file_exists/path_exists/artifact_exists` 会归一成 `file_check`。如果测试项没有写 `file_path`，系统只会从同一 `output.artifacts[].path` 的机器字段展开目标；不会从测试名、summary 或任务正文猜路径。

这刀参考的是 通道运行时 / OpenHuman 的显式协调工具思路：协作不是自然语言承诺，而是能被控制面、工具记录和状态回路看见的事实。

## 当前 create / dispatch 触发方式

主代理现在支持两种交卷方式：

1. 显式交卷：模型调用 `submit_for_acceptance`。
2. 隐式交卷：模型没有工具调用，准备最终回复。

两种方式最后都会走同一套 delivery closeout。模型不能只靠自然语言说“完成了”绕过验收。

## 仍需注意的测试影响

旧测试里有一批 fake backend 默认“写完文件后系统每轮自动 closeout”。现在 closeout 已改成显式/隐式提交触发，所以这批测试需要后续统一迁移：

- 写完产物后 fake backend 应该调用 `submit_for_acceptance`，或者返回无工具最终回答触发隐式验收。
- 不应该继续期待“任意工具执行后系统立刻 closeout”。

这个迁移是测试语义更新，不是重新引入 bootstrap 硬门。

## 下一步建议

1. 把旧 closeout 集成测试迁移到“显式/隐式提交验收”语义。
2. 用普通用户 prompt 重跑单周 GitHub 任务，观察模型是否能自由检索、写阶段材料、生成最终产物并提交验收。
3. 如果产物内容仍有幻觉，不加新的前置硬门；改为事实声明、来源引用、事实核对、closeout 返工单这类后置质量闭环。

## 显式资料路径只读授权

真实多代理协作时，root 常会把“某个子代理负责哪个资料文件”写进 `create_subagents.items[].goal`。如果模型漏填 `required_read_paths`，旧链路会让子代理知道文件名，却没有工具层只读授权，最终在 `read_file` 处被 `PATH_WORKSPACE_ESCAPE_BLOCKED` 卡住。

现在的规则：

```text
create_subagents item goal 中出现明确文件路径
  -> 作为 required_read_paths 保存到 context_manifest
  -> runner context 写入 allowed_read_roots
  -> path gate 允许读取该资料路径
  -> 不加入 allowed_write_roots / product_write_roots
```

边界：

- 只提取路径形态 token，不根据自然语言判断任务该怎么做。
- 只开放读取，不开放写入。
- 显式结构化字段仍是首选：`required_read_paths`、`input_files`、`source_refs` 等会原样保留。
- 这不是 IP/日志/论文/GitHub 专项；任何普通资料文件都走同一条只读授权链。

真实模型也可能把 `context_manifest` 写成 refs-only 短写：

```json
{"context_manifest": ["/path/to/source.txt"]}
```

这类形态现在会被归一成：

```json
{"context_manifest": {"required_read_paths": ["/path/to/source.txt"]}}
```

同一个结构化参数包也可以给出输出 refs：

```json
{
  "context_manifest": {"source": "/tmp/task/inputs/a.txt"},
  "output_files": ["/tmp/task/outputs/a.result"]
}
```

如果输入 ref 真实存在，并且输出 ref 与它共享一个足够窄的任务目录，系统会把输出文件父目录授权给这个 child。这个规则只看结构化 refs 和文件系统事实，不从 `goal` 里猜授权；未知扩展名也按开放世界文件 ref 处理。

旧的 `input_dependencies_skipped` / `input_materialization_recovery` / `materialize_subagent_inputs` 链路已删除。原因是它把普通子代理工作变成了“先满足某个中间路径门才能启动”，容易让协作和真实任务卡在控制面。现在的语义更简单：

```text
create_subagents 写清楚 goal / refs
  -> dispatch_subagents 直接让子代理跑
  -> 子代理读不到文件时，用工具结果/状态/证据向父级报告
  -> 父级按 tree/board/case 继续补派、重试、询问用户或收口
```

`required_read_paths` 仍可用于提示和读授权，但不是启动前验收项；不存在就让真实工具调用暴露问题，不提前把 runner 拦掉。

边界：

- 只处理结构化 refs，不从 `goal` 普通自然语言里猜输入。
- 不复制文件、不回绑路径、不生成 `materialized_inputs.json`。
- 子代理读不到文件时，读工具返回真实错误；父代理通过 tree/board/case 看到事实后再决定补派、换路径、问用户或收口。
- 未知文件后缀不会被拒绝；文件格式是开放世界，ref 层只负责传递结构化路径事实。

同一轮继续删除了 workflow sibling 依赖和 runner 角色阶段候选门：

- `workflow_depends_on` 不再是 `SubAgentTask` 运行时字段。
- `subagents/dependency_artifact_refs.py` 已删除，不再自动把上游 sibling 产物塞进下游 read refs。
- `dispatch_subagents` 不再因为角色是 tester/reviewer/checker 或所谓 phase 顺序，只放行一部分 runner。

真要流水线时，父代理显式控制即可：先创建/dispatch A，看到 A 结果后，再创建/dispatch B。这样普通并行协作、临时响应和广播自查不会被隐藏的“生产线顺序”卡住。

## 已废弃：创建阶段 pending dispatch 拦截

`pending_dispatch_redirect.v1` 曾经用于阻止 root 在已有 run 尚未 dispatch 时继续创建子代理。协作场景证明这会误伤临时加派和事件响应：发现者需要能继续找帮手，不能因为上一批 run 还没推进就被 create 阶段拦下。

当前规则是：`create_subagents` 默认创建后直接启动新 run，不再要求主代理再手动催一次。只有显式传 `defer_start=true` 时，才只创建/复用任务记录。父代理后续要看状态、追加提示、推进卡住项、重跑某几个 run 或尝试恢复时，再使用 `dispatch_subagents`。

真实模型后端下，这个“直接启动”不是短命 CLI 里的 daemon thread，而是独立 `subagents-dispatch --apply --execute-runners --run-id ... --background-launch-id ...` 进程。这样 `my-agent run` 返回后，子代理 runner 仍然能继续推进；后台进程会把 `attributes.background_start.status` 写成 `running/finished/failed`，任务树不会因为父进程退出而只剩一个假启动标记。离线 `echo` 后端保留进程内线程，方便单测和本地 smoke 不额外启动子进程。

同一轮还废弃了 workflow 自动套娃：全局 `subagent_workflow_mode=auto` 不再静默作用到普通 `create_subagents` / `dispatch_subagents`。只有本次工具参数明确写 `workflow_mode=plan` 或 `workflow_mode=auto` 才会启用 workflow；未知值如 `parallel` 一律当 `off`，避免普通 worker 被拆成 implement/verify 孙代理。

`dispatch_subagents` 现在更像“运行中的引导/推进工具”：它可以带 `runner_instruction`，也接受 `prompt`、`message`、`guidance` 这类别名，作为给目标子代理/孙代理的本轮补充提示。它同时保留人工催办、推进卡住项、重跑指定 run、查一轮状态并尝试恢复这些能力。

模型可见的 `dispatch_subagents` 返回里，顶层 `dry_run` 才是整次调用是否真实推进的事实。逐记录统计统一叫 `record_dry_run_count` / `record_applied_count`，逐条记录也统一叫 `record_dry_run` / `record_applied`，避免主代理看到嵌套 `dry_run=true` 后误以为整次 dispatch 都只是预览。

```json
{
  "tool": "dispatch_subagents",
  "apply": true,
  "execute_runners": true,
  "run_ids": ["subagent-..."],
  "prompt": "继续检查遗漏，查完把结果写到自己的产物里并汇报给上级。"
}
```

如果主代理只是想先登记任务，不让子代理立刻开跑，需要显式说：

```json
{
  "tool": "create_subagents",
  "goal": "先登记后续资料整理任务",
  "count": 3,
  "defer_start": true
}
```

如果模型只想查看状态，不需要 dispatch，可以调用 `subagent_board` / `inspect_agent_tree` 读取 tree/status。系统不再本地抢答“已完成/未完成”，也不再用额外父级验收专用门替代统一 closeout。

`subagent_board` 会额外给父级两个观察字段：`running_seconds` 表示这个子代理从最近心跳/创建到现在大概跑了多久，`seconds_since_progress` 表示距离最近一次真实进展大概过了多久。这两个字段只帮助父级判断“要不要查看、提醒、补救”，不触发自动阻断。

看板还会返回 `aggregation_readiness`：子代理总数、已完成数、可读产物数、未完成 run id。它的作用是提醒父级汇总前先读已完成 refs、继续推进未完成项；不是新的验收门，也不替代统一 closeout。

入口物化层支持可选 `target_coverage_contract`。它只描述“用户希望覆盖哪些目标、时间段、名单或来源范围”，closeout 报告会生成 `target_coverage_status`，列出已覆盖和缺失项。这个账本默认 `should_block=false`，用于提醒模型补齐或向用户说明缺口，不把开放世界研究任务卡成固定模板。

历史上的验收-only dispatch 建议类似：

```text
execute_runners = false
execute_acceptance_tests = true
auto_apply_result_followup = true
```

这类父级验收专项收口已经降级为历史试错记录。当前路线是：子代理运行事实进入 tree/refs，最终质量统一回到普通任务 closeout；如果以后要让所有子/孙节点也走 closeout，应复用同一套 closeout 配置，而不是再造父级验收协议。

## 显式产物写入根授权

当模型创建子代理时，如果同时声明了产物目标和写入根：

```json
{
  "output_files": ["/task/outputs/result.json"],
  "extra_write_roots": ["/task/outputs"]
}
```

`create_subagents` 的预检会允许这个目标通过，因为目标文件位于显式写入根下。旧逻辑只认主仓库 workspace，导致模型即使补了 `extra_write_roots` 也继续被拒绝。

边界：

- `output_files/artifact_refs` 是目标，不是授权根。
- 只有 `extra_write_roots/write_roots/target_roots` 这类结构化字段能成为写入根。
- 普通 `goal` 文本不能生成写权限。
- `/` 和用户 home 这类过宽目录不会作为写入根接受。
- 这只是让创建前预检和 runner 的 `allowed_write_roots` 对齐，不改变实际工具执行时的写边界。

## 接管 run 不能丢结构化交接单

当 runner 超时、断连或被接管时，新 takeover run 不是一个“重新理解任务”的空任务。它必须带着旧 run 的机器交接事实继续：

- `quality_contract`
- `context_manifest.required_read_paths / hint_read_paths / task_pack_refs`
- `context_packs`，例如 sibling roster、协作线索包、系统派生合同
- `attributes.output_files / output_refs / artifact_refs`
- `artifact_refs / evidence_refs`
- 原 run 已被授权的写入根

接管层只重写 `takeover_source_run_id`、`takeover_source_refs`、`takeover_chain_depth` 这类审计字段。这样 coordinator 或 repair worker 被接管后，仍知道要读哪些资料、有哪些兄弟/子任务、最终应该写到哪里。

如果旧代码已经创建过一个缺字段的 takeover run，后续复用它时会自动补齐源 run 的缺失交接字段；已有 takeover 自己新增的字段不被覆盖。这是恢复链一致性修复，不是 IP、日志、GitHub、论文等专项规则。

## dispatch_subagents 只返回索引和状态，不替父代理抢答

`dispatch_subagents` 是推进/查看子代理树的控制面工具，不是最终回答生成器。

当前语义：

- 工具执行后返回紧凑的 `result_refs_by_run` / `child_result_index`、状态摘要、`output_json`、产物 refs 和调度报告 refs。
- 顶层主代理拿到这些索引后，必须自己进入下一轮模型判断：是否汇总、是否继续调度、是否读某个 ref、是否写最终报告。
- 系统不再因为“子代理都 DONE/VERIFIED”就在工具轮后直接生成 `未再发起额外模型请求` 的本地收口回答。
- 如果没有显式 run scope，`dispatch_subagents` 会退回列出当前可见子代理的紧凑状态索引，支持父代理“按一下查状态”。
- `inspect_agent_tree` / `subagent_board` 仍是只读状态工具；`dispatch_subagents` 可以推进 runner，也会返回本次推进后的索引状态。

仍保留的兜底：

- 工具轮数耗尽或模型最终空响应时，系统可以按真实 task.json 生成事实状态报告，避免请求挂死。
- 最终回答如果和持久化子代理状态冲突，例如仍有 `BLOCKED/FAILED/TIMEOUT/CHANNEL_ERROR`，最终回答守卫会用状态摘要纠正，防止口头报喜。

这次改动删掉了旧的“完成后本地直接收口”路径。它对齐的是 通道运行时/长期助手/终端交互 更朴素的交接方式：子代理产出结果和 refs，父代理或请求者 agent 负责综合判断和最终表达。
