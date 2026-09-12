# 模型空正文截断的终态诊断

## 问题与边界

供应商正常结束 HTTP 流不等于助手已写出完整正文。模型可能用完输出预算，最后一轮只有思考、没有正文和工具调用。后端已经提供结构化 `unfinished / MODEL_RESPONSE_TRUNCATED / max-tokens`，但 Gateway 的空正文检查原来会先抛 `USER_REPLY_UNAVAILABLE`，导致客户端只看到泛化的“任务处理失败”，本轮原生消息也未进入正常 final 提交。

本修复只处理通用响应协议和展示，不判断业务完成质量，不分析思考文字，不新增自动重试、自动续跑或工具执行，不修改 Compact、权限和验收边界。

## 对照与实现

- 对照本机 会话运行时 `会话运行时-rs/core/src/session/turn.rs` 的 `ResponseEvent::Completed` 分支：原始模型响应完成、用量记账、是否存在最后正文与后续执行是分开的结构化事实。这里适配已有 `AgentRunResult` 与 `turn_end`，不照搬另一个控制循环。
- `request_errors.gateway_empty_model_response_projection` 只接受空正文、`runtime_status=unfinished`、`runtime_reason=MODEL_RESPONSE_TRUNCATED` 且归一化结束原因为 `max-tokens` 的组合。显式 `completed` 不会被推断覆盖，其他错误和普通空回复保持原处理。
- `request_execution` 将该空正文 final 按原请求 ID 幂等落账：正文仍为空，保留原生消息、工具结果、显示快照与结束原因。落账失败仍走原有相同 payload 的延迟补交，不重新调用模型。
- 请求完成装配后返回 `ok=false / status=failed / error_code=MODEL_RESPONSE_TRUNCATED`，同时保留 `runtime_status=unfinished`、供应商来源、工具轮数与 `turn_end_reason=max-tokens`。`failed` 是本次请求生命周期终态，不是整项任务未做任何工作的判定；已有排队/Working 收口不变。
- 用户看到的技术提示是“本次模型输出达到上限，尚未形成完整正文；已有工具操作和历史保留。”提示只作为系统错误展示，不放进模型正文；空正文不会作为 IM 回复内容。有部分正文的既有截断路径保持原样。

## 验证

定向回归覆盖：空正文截断完整通过 Gateway 运行结果、原生历史落账、请求终态提交；一轮只调用一次模拟模型；有正文截断与空正文截断分别覆盖普通和 rich transcript、立即落账和延迟 repair；工具结果可从 canonical native 历史还原；普通空正文和显式完成不被误分类；TUI 复用已有错误事件显示提示、结束 Working，不添加伪造或空白助手回复。

验收不运行原业务任务。真实 TUI 验收由集成方使用无害本地任务进行，不能把上述离线回归当作已完成真实模型测试。
