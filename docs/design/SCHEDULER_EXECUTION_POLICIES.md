# 定时任务执行、模型与预检合同

状态：L 线已与当前 TUI/IM 工作树整合，未发布；当前证据见 [整合记录](../tasks/L_T_IM_INTEGRATION_HANDOFF.md)。来源 真实 TUI 组合验收及 T 控制链集成状态见 [长任务报告](LONG_TASK_RUNTIME_REVIEW.md)。本合同扩展原 owner scheduler，不另建调度器、任务树、交付队列或模型配置源。

## 单一事实源与入口

- `scheduler/repository.py` 保存原 job/run、版本、claim epoch、开始时间和终态；`run_preparation.py` 在同一事务里冻结来源和预检观察。
- `scheduler/service.py` 仍负责 reserve → wake → claim → TaskLink；`scheduler/execution.py` 接入原 `background_claim.run_claimed`。
- `conversation/runtime.py` 只接执行开始、模型作用域、交付来源三处边界；原停止、输入领取、Compact 与 outbox 仍由各自模块负责。
- job 修改只影响以后预留的 run。已预留 run 的 prompt、执行策略、预检定义和 Skill 引用保持原快照；固定档案保存 ID，不复制凭据。

## 用户可配置字段

新增能力按 job 显式启用，不增加全局后台任务或默认模型请求。默认值来自 schema 对应的规范函数，旧 job 缺字段沿用原行为。

| 字段 | 默认值 | 语义 |
| --- | --- | --- |
| `execution.mode` | `followup` | 沿计划的执行会话；新 IM 计划默认已绑定独立后台，TUI 默认沿原会话 |
| `execution.mode=isolated` | 显式启用 | 每个 run 一个无通道绑定的独立上下文 |
| `execution.model_policy` | `inherit` | 初始与后续安全点继承来源会话的显式模型选择 |
| `execution.model_policy=fixed` | 显式启用 | 必须给出具体 `profile_id`，不接受可变 `default` 别名 |
| `policy.misfire` | `skip` | 超出原宽限期的错过周期跳过；`coalesce` 只补一次 |
| `policy.queue_timeout_seconds` | `0` | 预留后尚未实际开始的最长等待；0 关闭 |
| `policy.failure_pause_after` | `0` | 连续 failed/unknown 达阈值暂停未来周期；0 关闭 |
| `policy.failure_backoff_seconds` | `0` | 未来新 run 的指数退避基数；0 关闭，最长七天 |
| `precheck` | `null` | 关闭；对象显式指定只读工具及观察字段 |

`execution`、`policy` 是完整配置对象；提供对象中的缺省字段按表补齐。更新未提供的整个对象不变。`precheck=null` 明确关闭预检。

示例字段：

```json
{
  "execution": {"mode": "isolated", "model_policy": "inherit"},
  "policy": {"misfire": "coalesce", "queue_timeout_seconds": 900},
  "precheck": {"tool_name": "example_read_tool", "arguments": {}, "json_pointer": "/revision"}
}
```

工具名仅示意，产品没有内置 `example_read_tool`；实际必须是当前 owner 可用、按本次参数被原 ToolRuntimePolicy 判为 `read_only` 的工具。不能凭工具名字、自然语言或模型保证跳过权限判断。

## 执行与交付分离

独立上下文的线程 ID 由原 run ID 确定，重复物化沿同一 ThreadStore 记录，保留已存在的 Compact、模型和消息状态。创建线程不会复制来源历史或通道绑定。

独立指会话上下文独立，**不意味着文件系统隔离**。执行沿原会话 cwd、授权根和 owner 上界；有同目录写入需求时仍使用原资源与权限合同。

新 IM 计划先固定原布置者；显式 isolated 的 run 从原计划引用冻结同一来源，其他来源在 run 入队时冻结通道绑定，独立线程仅保存结构化来源引用。结果通过原路由与 outbox 投回创建会话；每次真正投递前确认冻结绑定仍指向原会话。来源重置、删除或绑定变化不会转投后来出现的会话。

投递失败保留冻结内容、回执和原 run。重试只发送该内容，不再调用模型或预检工具，也不重做业务操作。业务终态与投递状态分别展示。

子代理完成后的 waiting run 不能只凭 TaskLink completed 收口。仍有同任务存活或未知执行 claim、待处理生命周期 wake（含冻结投递）时保持 waiting。最终交付已确认后，先将该片的 response/delivery_status/delivery_reason 写回原 run，再确认原 wake，最后结束 run 并提交 checkpoint。重启对账读取同一组字段，不能把“孩子正在处理”的第一片回执当作最终结果；ended_at 使用实际收口时刻。

## 领取与开始时间

领取 scheduler claim 表示持有调度运行资格，尚不代表取得后台会话执行权。

真实顺序是：预留原 run → 原 wake → scheduler claim → 核对 TaskLink → 后台 claim 准入及中断登记 → `mark_run_running` → 可选预检 → 原模型工作片。

`started_at` 只在取得后台车道后首次写入。车道繁忙、准入未通过或开始前退出仍不产生开始事实。领取后再次校验原 claim/epoch，旧领取者不能确认新领取者的 wake。

队列期限冻结于预留时刻。未开始的 run 到期记 skipped 和 `SCHEDULER_QUEUE_EXPIRED`；已经执行、等待子任务或欠投递结果不按队列期限终止。期限不是模型总运行时长上限。

领取但未执行的进程死亡沿原身份恢复；已开始后死亡继续使用原 unknown/恢复合同，不能用“只是定时任务”作为重放副作用的依据。

## 模型来源

`inherit` 的选择权威是原会话。实际执行线程与选模来源线程分别记入原模型调用账；下一次安全请求边界复用 M1 刷新，不新建一套切换循环。

`fixed` 在每片开始解析指定档案，原会话后来的选择不改变它。固定的是档案引用，档案本身后续被修改时按最新有效定义解析；并非把所有采样参数永久封存。创建和修改计划时校验引用，执行时再次校验撤销、删除和权限变化。

档案不存在不自动切回默认或第三个模型。已开始的响应与工具仍按原请求依赖收尾；新请求采用时配置、backend、prompt、工具视图、上下文容量、历史与用量来源一起变化。

M2 中 `bound` 只证明宿主当前依赖，`last_request` 来自实际模型调用账。供应方未报告缓存数据时显示未知，不能把选模等同于服务端收到或缓存失效。

## 结构化预检和检查点

预检配置只包含 `tool_name`、`arguments`、可选 `json_pointer`。省略指针时比较完整工具输出；含时间、延迟等易变字段的结果应显式选择稳定字段。提供指针时输出必须可解析为 JSON。

`create/update` 可直接填写这个对象，例如 `{"tool_name":"read_file","arguments":{"path":"source.txt"}}`。`prompt` 描述检查有变化后的工作。模型如果只在 prompt 中写“先检查”，而没有保存 precheck，运行时仍按关闭预检执行，不从自然语言补造配置。

预检使用同一 ToolRegistry snapshot、ToolExecutor、原 task/run、workspace 和 authority。`agent_core/tool_only_execution.py` 装配原工具循环参数，经 `execute_traced_tool_call` 与 `_record_tool_call` 写入原归档和 `tool_completed` 事件，actor 为 `host`，不伪造模型 tool_use。原归档后的只读观察回调交付完整正文，比较不使用模型预览、脱敏展示或截断部分；归档失败按失败处理。该工作片不构造 fake LLM；中断令牌传入执行器，因此取消后不会继续只读重试。

| 观察结果 | 后续行为 | 检查点 |
| --- | --- | --- |
| `changed` | 执行原模型工作 | 原 run 成功并完成交付后提交 |
| `no_change` | 本周期静默 skipped，不调用模型 | 保持原已确认点 |
| `failed` | failed，保存原因码，不调用模型 | 不推进 |

无变化仅说明所选观察值与上一次已确认观察一致，不能证明全部外部状态无变化。摘要包含预检配置；修改配置会触发新比较，不能继承不同检查的结论。

观察写入原 run 一次，恢复复用；job checkpoint 在同一 done 终态事务推进。动作失败、unknown、投递待重试或期间 job 的预检配置已修改，都不覆盖 job 的已确认点。第一次没有 checkpoint 时视为 changed。

预检原参数和工具完整回执仍归原工具账；schedule 元数据仅投影 enabled、状态、调用 ID、时间和错误码，不返回参数或正文。

## 错过周期与失败策略

`skip` 保留原宽限期语义；`coalesce` 把多个错过周期合成一个原 run，并记录 scheduled_for 与 lateness，下一自然周期仍按原时区计算。不产生每个错过周期各跑一次的积压。

同 job 不重叠规则保留。连续失败策略只控制后续周期，不自动重跑 failed/unknown 的旧 run。成功重置连续失败和退避；跳过不算业务成功；成功也不会悄悄解除手动暂停。

手动运行仍是显式的新 run；不复用失败 run 的副作用身份。暂停、恢复和夏令时计算沿原 schedule/repository 合同，须用可控时钟测试，不能从普通触发外推。

## 验证边界

定向测试入口：`test_scheduler_execution.py`、`test_scheduler_precheck.py`、`test_scheduler_policy.py`，与原 scheduler runtime/service/unknown/frozen delivery 组合测试一起运行。异常注入覆盖开始写入失败、旧 claim、停止、预检配置变更和服务恢复。

真实验收从测试机原生 TUI 提交普通需求，由被测 agent 创建计划、修改文件并产生回复。测试者只准备合成输入、执行明确控制操作和观察证据，不能补任务产物。

学习来源继续使用既有结构化合同；本基线的 Skill 自学习已经排除 `background_main_agent`，预检不进入 FinalizationService。报告所指新版 AGENTS 来源合同未在本基线集成；该组合单独列为依赖，不在 scheduler 添加第二张学习排除名单。

## 预检工作目录与失败原因

预检与后续模型工作共享原后台请求的 thread cwd、runtime workspace roots、Skill 与 task 属性装配。宿主工具入口不得自行拼接一套 cwd 合同；既有 owner 目录权限继续生效。原 ToolResult 失败码保存在观察的 `tool_error_code`，配合 call_id 可定位共享工具账，不在状态查询中复制参数或输出正文。

## 子代理完成后的续接来源

scheduler run 的 ID 是持久任务来源，不是聊天请求 ID。生命周期续接先核实原 repository 中同 run/thread 的记录，复用后台 durable_task 投影；目标仍来自原 run/TaskLink，普通额外用户请求仍需在 canonical 历史中存在。不会为续接补造聊天消息，也不重新预检或新建定时 run。

预检字段说明：json_pointer 允许 null，与省略同义，比较完整工具输出；空字符串仍是 RFC 6901 的 JSON 根指针，要求工具输出本身是 JSON。schema 与原规范化函数保持一致，不把错误指针默默改为全量比较。
