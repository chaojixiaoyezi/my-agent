# 模型不完整响应的终态诊断

## 问题与边界

供应商正常结束 HTTP 流不等于助手已写出完整正文。模型可能用完输出预算，最后一轮只有思考、没有正文和工具调用。后端已经提供结构化 `unfinished / MODEL_RESPONSE_TRUNCATED / max-tokens`，但 Gateway 的空正文检查原来会先抛 `USER_REPLY_UNAVAILABLE`，导致客户端只看到泛化的“任务处理失败”，本轮原生消息也未进入正常 final 提交。

本修复只处理通用响应协议和展示，不判断业务完成质量，不分析思考文字，不新增自动重试、自动续跑或工具执行，不修改 Compact、权限和验收边界。

## 对照与实现

- 对照本机 会话运行时 `会话运行时-rs/core/src/session/turn.rs` 的 `ResponseEvent::Completed` 分支：原始模型响应完成、用量记账、是否存在最后正文与后续执行是分开的结构化事实。这里适配已有 `AgentRunResult` 与 `turn_end`，不照搬另一个控制循环。
- `request_errors.gateway_model_response_error_projection` 接受空正文、`runtime_status=unfinished`、`runtime_reason=MODEL_RESPONSE_TRUNCATED` 且结束原因为 `max-tokens` 的组合；也接受 `runtime_source=model_provider`、`runtime_status=error`、`turn_end_reason=error`、`MODEL_*` 原因码的运行结果。显式 `completed` 不会被推断覆盖，工具权限等非模型错误和普通空回复保持原处理。
- `request_execution` 将该空正文 final 按原请求 ID 幂等落账：正文仍为空，保留原生消息、工具结果、显示快照与结束原因。落账失败仍走原有相同 payload 的延迟补交，不重新调用模型。
- 请求完成装配后返回 `ok=false / status=failed / error_code=MODEL_RESPONSE_TRUNCATED`，同时保留 `runtime_status=unfinished`、供应商来源、工具轮数与 `turn_end_reason=max-tokens`。`failed` 是本次请求生命周期终态，不是整项任务未做任何工作的判定；已有排队/Working 收口不变。
- 用户看到的技术提示是“本次模型输出达到上限，尚未形成完整正文；已有工具操作和历史保留。”提示只作为系统错误展示，不放进模型正文；空正文不会作为 IM 回复内容。有部分正文的既有截断路径保持原样。

## 验证

定向回归覆盖：空正文截断完整通过 Gateway 运行结果、原生历史落账、请求终态提交；一轮只调用一次模拟模型；有正文截断与空正文截断分别覆盖普通和 rich transcript、立即落账和延迟 repair；工具结果可从 canonical native 历史还原；普通空正文和显式完成不被误分类；TUI 复用已有错误事件显示提示、结束 Working，不添加伪造或空白助手回复。

验收不运行原业务任务。真实 TUI 验收由集成方使用无害本地任务进行，不能把上述离线回归当作已完成真实模型测试。

## 有正文但工具协议失败

模型可能先写出简短正文，再返回不完整或格式无效的工具参数。后端会将整组未完成工具调用隔离为零执行，并提供 `MODEL_TOOL_ARGUMENTS_INVALID / error`。原 Gateway 只要正文非空就返回 `ok=true / done`，TUI 又只识别长度上限，因此用户看到半句正文后空闲，以为任务仍将继续。

此类结果现在按原结构化错误投影为本轮 `failed`，提示“模型返回的工具调用参数不完整或格式无效，本次调用未执行，本轮已停止；已有工具操作和历史保留。”真实正文与原生历史保持原样；TUI 先显示已有正文，再显示单独错误并关闭 Working。断流、响应过滤以及新增的 `MODEL_*` 供应商错误同样保留原错误码，不猜成配置、密钥或额度问题。没有改动参数解析器、重构缺失参数、执行残缺调用或增加自动续跑。

### 参数无效改为有界纠正（2026-09-30）

- **变化**：上面“本轮已停止”的处理对长时间自主任务代价太大：09-30 真实开发任务里，deepseek-v4-flash 工作会话写了一半测试，一次工具参数
  不是合法 JSON，474 秒的工作整轮以 `MODEL_TOOL_ARGUMENTS_INVALID` 结束。官方 Codex 的做法是把参数解析失败作为结果回给模型、让它改了重发。
- **现在**：`response_decision._invalid_tool_arguments_decision` 只认后端归一的 `runtime_status=error / MODEL_TOOL_ARGUMENTS_INVALID /
  model_provider` 且零工具块的响应：整组仍零执行，回灌一条宿主纠正（`[tool-arguments-invalid]`，只带结构化工具名，要求重发完整 JSON、
  长内容分块），同一轮续跑。连续 `_INVALID_ARGUMENT_REPAIR_LIMIT`（3）次仍无效时，沿原无工具分支按上面的错误投影结束本轮；
  形成可执行调用后计数清零（`ToolLoopRepairCounters.invalid_arguments_repairs`，与累计型协议修复分开）。
- **不变**：不解析正文、不修复或执行残缺参数；断流（`MODEL_STREAM_INCOMPLETE`）、内容过滤等其它 `MODEL_*` 错误照旧直接结束；长度截断的
  分块写恢复与预算不变；Gateway 错误投影与用户提示不变（仍在超过上限时出现）。纠正事件与协议违规同路写入 runtime_events。

## 原生写恢复的原因分类（2026-09-26，本地修正）

`truncated=True` 表示整轮工具不得执行，不能单独证明达到了输出长度上限。
后端已把坏JSON、非对象参数、断流和过滤分别归一为对应`MODEL_*`错误；原写恢复只看该布尔，
会把这些原因覆盖为`NATIVE_TRUNCATED_WRITE_LOOP`，并错误追加分块请求或显示长度上限文案。

原`response_decision`现在只接受后端归一的`unfinished / MODEL_RESPONSE_TRUNCATED / model_provider`
和`truncated=True`共同成立时的长度恢复；其它错误沿原无工具分支保留同一响应对象、零执行、零新增纠偏请求。
不重新维护provider结束原因别名，不解析供应商正文，不按参数字符数或配置默认cap猜因。
同一工具循环最多两次分块纠偏，第三次已确认长度截断保留原宿主guard，同时保留原`stop_reason`、
`turn_end_reason`、用量及截断工具名。计数是本循环累计，不代表同一文件连续写失败；未执行的调用没有空参工具回执。

真实SSE collector→原backend归一→原裁决的7个反例先红，修正后相邻四文件74项通过。
既有长度guard测试还核对原`should_continue_task`和真实Goal续接裁决不发布wake；
无网络SimpleAgent主链重放精确3次模型替身请求、0工具，返回后未因保留`max-tokens`取得新续跑资格。
这不代表封禁其它独立控制动作：旧非权威会话的Compact仍可按原cycle和已有工具进展续接，该路径不读取本次保留的字段。
该结果不反推旧真实B01的结束原因：原partial参数、provider结束字段及实际请求cap没有持久证据，仍保持未知。
现有Gateway错误投影、任务完成条件和请求/attempt/task分层不改；未新增模型循环或第二份持久账。
未来有界诊断字段应复用原调用身份与运行账另行设计，不能在本片默认保存原参数或个人路径。
