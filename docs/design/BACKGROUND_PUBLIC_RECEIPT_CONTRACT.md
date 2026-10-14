# 后台内部结果与公开回执合同

状态：本期已实施，真实模型语义验收仍需独立执行。本合同解决公开意图和响应来源的客观边界，不保证模型不会写出偏题内容。

## 原问题与适用范围

旧 `delivery.mode=origin` 把普通非空最终回复作为回执。合法 JSON、报告、短句均可投递；程序不能根据“（空）”等文字猜测静默，也不能把所有 JSON 视作异常。现场偏题 JSON 的具体模型来源没有确认，不能宣称只加调用来源核验便修好了模型相关性。

新增开关 `background_public_receipt_contract_enabled=true` 只开放显式完成合同。任务原 `delivery` 增加可选 `reply_policy=always|explicit`，创建/更新后随原 run 冻结。缺省仍是 `always`，旧记录不隐式迁移；`local/none` 的内部记录与默认新 IM 计划行为不变。

管理员/普通用户都由原工具和 owner 权限创建计划。通知策略只能控制是否回原来源，不新增收件人、身份或越权能力。

## 唯一主链路

仅当当前精确活动调度 run 冻结为 `{"mode":"origin","reply_policy":"explicit"}` 时，后台准备层给原 `RunParams.delivery_contract` 装配 `background_public_receipt`。旧同名携带字段先清掉，来源只取正式调度 run。关闭能力开关会明确失败，不能悄悄退回 raw final 外发。

主模型仍在原工具循环完成工作，工具阶段沿现有协议。最终零工具响应采用 `background-result.v1`：

```json
{
  "schema": "background-result.v1",
  "internal_result": "本轮内部执行结果",
  "notification_intent": "internal_only",
  "public_reply": ""
}
```

`internal_result` 必须非空，是模型的执行总结，**不是宿主核验结论**。公开意图只能是 `publish` 或 `internal_only`，不能通过普通正文、关键词、占位符或用户自然语言直接触发机器状态。合法 JSON 可以完整放在 `public_reply` 字符串里。只有已启用的受管完成阶段解析这个 schema，普通任务正文不解析它。

完整受管响应可以是原始 JSON，或整个响应只有一份明确标注 `json` 的代码围栏。通用解码入口位于 `common/structured_output.py::json_value_from_complete_response`：只去掉完整包装，不扫描正文中的 JSON，不接受前后说明、多个对象或多个围栏、其它代码标签及嵌套提取。解码后仍沿原四字段 schema、调用来源和公开义务校验；原 `json_objects_from_text` 的正文扫描用途保持独立，不能替代协议权威。普通文本和普通 JSON 的交付路径不使用此入口，不增加模型调用。

程序先走原取消、未知副作用、声明产物和截断守卫，再消费完成协议；`enable_tools=false` 的显式任务同样走完成出口。显式内部成功不发消息，内部结果和原生历史保存在原 owner 会话的同一内部记录，没有第二套消息队列或业务账本。

实际待交付附件不能被 `internal_only` 取消。已消费插话的回复欠账也不能被静默取消；合法现成公开回复直接清偿欠账。附件已就绪时允许 `publish` 的正文为空，附件仍走原交付链。

## 有界公开表达修复与成本

合规主轮正常完成增加 **0 次模型调用**，普通问答、旧 origin、local/none 不增加 schema 阶段或模型调用。

仅当本轮已明确 `publish` 但公开形态净化后没有正文且没有附件，或内部静默声明遇到插话回复欠账时，复用原 `queue_natural_user_reply` 的 `background_final_receipt` 阶段。最多增加 **1 次逻辑零工具表达调用**，不执行原业务工具。原传输层物理重试和供应商空响应修复仍有自己的既有预算。

修复事实包含当前请求、准确执行来源、原工具账的有界状态/效果/产物引用以及明确标记为模型撰写的内部总结。表达可以如实说明不能确认的内容，不能把内部总结升级成“宿主已验证”。修复失败保留内部结果，返回明确错误；失败公开回执不会授权再次执行刚执行过的业务。

供应商物理空输出仍走原失败/有界恢复，不等价于已声明内部成功。受管信封格式错误、调用来源冲突和公开义务冲突均明确失败，不通过坏回复、任意正文或无内容假装成功静默。

后台进度窗口也服从同一次冻结策略。标准独立定时线程原本不会创建 IM 委派窗口；定时任务沿已有委派线程 followup 时，`BackgroundIMProgress` 现在先按准确根任务查询活动调度 run，并核对原 TaskLink 的执行线程。`local/none` 或 `origin+explicit` 不启动观察者，因此在最终 `publish` 决定前不会发送心跳或“本轮已结束”编辑。该检查不读取 job 当前版本，不改原审批 scope，不新增观察状态；普通 IM 委派和旧 `origin+always` 的进度以及前台 120 秒反馈保持原样。来源读错或 ID/线程冲突时关闭可选进度，不能冒认另一条运行。

## 调用来源绑定

原 `ModelCallLedger` 的真实 `finished` 出口为响应签发本进程私有 seal，包含 `request_id/run_id/task_id/thread_id/attempt_id/call_id/logical_call_id/purpose`。provider 返回的同名来源/内部结果/公开回执字段在签发前清掉。模型 payload 的 dict、持久元数据或旧响应投影不能重建 seal。

主循环按当前请求/运行/线程/尝试及最新实际调用核对来源。decision、compact、探针或别的运行不能冒充主业务回复；自然表达和回执修复分别使用其真实用途。该核验 **不检查语义相关性**：同一真实主调用仍可能错误理解任务，公开正文也可能偏题，这部分只能由模型纪律和后续真实样本检验，不作为安全授权依据。

内部结果、公开内容和公开意图沿同一个 `ModelResponse → AgentRunResult → BackgroundExecutionResult → 原 ReplyEnvelope` 交接。来源事实保留在现有 metadata；待投递内容仍沿原完整 owner 信封冻结。冷重投不重建 seal，不请求模型、不重新判断业务，仅重投已冻结的正文/附件/metadata，并保留原来源目标验证和去重规则。

## 参考与验证

先阅读本机成熟参考的协议入口，再实现通用接缝：Codex `codex-rs/protocol/README.md`、`models.rs` 的 `MessagePhase/turn_id`、`protocol.rs` 的 `TurnCompleteEvent`；OpenClaw `src/channels/turn/delivery-result.ts` 与 `durable-delivery.ts` 的独立 delivery intent、可见结果、抑制结果和完整回执。只借鉴阶段/来源/投递事实分离，不采用自然语言静默 token。

定向测试：`test_background_public_receipt_contract.py`、`test_background_public_receipt_integration.py`。覆盖准确来源/用途、伪造 dict 和 provider metadata、旧响应、物理空响应、正常 JSON、内部成功、附件/插话欠账、一次公开表达修复、旧策略零成本、原调度成功/失败和冻结重投。组合验证保留原模型计量、finalization、委派、unknown-effect、delivery contract、native 响应和架构边界测试。

真实模型内容相关性、真实飞书外发和长期监控仍需最终验收；本切片的合成运输不能充当真实外发证据。

`.20261013.7` 候选的独立 MiniMax-M2.7 合成验收已确认完整完成合同进入真实原生请求，模型返回合法四字段 `internal_only` 信封，但用单一 `json` 代码围栏包装。原直接 `json.loads` 把包装误判为 `BACKGROUND_RECEIPT_CONTRACT_INVALID`，该运行已正式失败且没有外发。此缺陷由上述完整包装解码修复，保留原失败证据；不重跑原已失败业务、不修改产物，修后候选由主线程另行做同需求的独立验收。这一结果不证明模型永远不会偏题。
