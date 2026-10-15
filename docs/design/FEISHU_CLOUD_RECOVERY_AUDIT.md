# 飞书云错误、原操作恢复与最小工具审计

2026-10-14：S06/S07/S08 已实现并完成隔离定向验证，尚未真实飞书验收或随本切片发布。原需求见 [22 项需求](IM_CLOUD_SCHEDULE_LATENCY_REVIEW.md#当前待办重排发卡故障单列第一项2026-10-14)。

## 共享供应商错误事实

HTTP 与受控 CLI 共用 `adapter/feishu_cloud.py` 的分类器。保留实际 HTTP 包装状态、数值 API code 和请求阶段；没有可靠包装状态时用 0 表示未保留，不伪造 HTTP 200。

- `90202`：范围或单元格载荷校验失败，`FEISHU_CLOUD_RANGE_INVALID`。
- `90215`：工作表不存在，`FEISHU_CLOUD_SHEET_NOT_FOUND`。
- `90213`：资源/保护范围权限拒绝，`FEISHU_CLOUD_RESOURCE_FORBIDDEN`。
- 范围和工作表错误仅在原 `/sheets/v2/spreadsheets/.../values*` 动作合同中证明明确拒绝。陌生路径的同码仍不能自动认定写入未发生。
- 明确拒绝是 `not_started`，允许原任务修正参数；陌生业务码、运输超时、回包丢失或无法投影的写入保持 `unknown`，不根据供应商 msg 或模型文字推断副作用。

错误含义依据飞书官方[通用错误码](https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code)。不复制响应任意正文、认证头或异常正文。

## 原 UNKNOWN 操作核对

唯一权威仍是原 `tool_operations`。原执行 `operation_id`、执行 attempt 和参数 hash 保持不变，不新造恢复队列、凭据库或状态权威。

1. 同一 owner/run/task 内参数和 namespace 相同的 UNKNOWN，换 call/operation 编号也先返回原记录；不会开始第二次追加。未来独立周期的不同 run 仍独立记账，不清除旧 UNKNOWN。
2. 原协调器领取核对租约，使用当前真实 attempt、原 owner/root、tool、args hash 和原操作代数进行事务核验。取消/终止的任务、旧 attempt、跨任务或不匹配参数均拒绝。
3. 跨 attempt 的恢复只在原 outcome JSON 保存当前核对者、epoch 和只读标记，CAS 递增原操作代数；原操作执行 attempt 不迁移。资源锁归准确当前核对者。
4. 结算仍核对 holder/generation，跨 attempt 还需核对当前活跃 attempt 和 epoch。旧执行者、取消后的恢复者或被再次换代的核对者不能结算。只读接管不能把 `safe_to_retry/not_started` 变成重执行许可。
5. `sheet_update` 仅对确切字面值矩形做一次同身份、同 owner 的授权读取；资源和网络许可继续经过原云工具入口。范围/形状准确且值一致时，目标状态证明原更新要求已满足，沿原 operation 结算成功，不再执行写入。
6. 目标状态核对不证明是哪位协作者最后写入，因而只表达“目标状态已满足”；不一致不能证明原操作没写，继续 UNKNOWN。公式、复杂值、无法精确比较或脱敏标记保持不可核验。
7. 追加位置未知、没有供应商原回执或可靠幂等凭据的 `sheet_append/document_append` 始终安全受阻，不扫描表格猜重复行、不修改旧 UNKNOWN、不换编号盲追加。需要人工或供应商可证明的原操作事实。

读取使用现有 `feishu_cloud_verify_sheet_writes_enabled` 开关；关闭时不新增核对请求。恢复只读原目标，不重新拉取业务资料、重做分析或清理既有工作区。

## 后台最小审计

`save=False` 继续关闭聊天、运行正文和长期记忆归档，但工具完成时通过原 owner `append_raw_event` 写一条 `minimal_tool_audit` 投影，不另造操作权威。

投影保存 owner、实际 HostActor、thread/task/run、准确 job/rule、工具声明式动作、原 operation、结果/阶段、HTTP/API 数值。动作只接工具 schema 声明的枚举；不序列化开放参数、表格值、工具输出、模型摘要、URL、认证头、异常正文或秘密。规则身份优先核对 owner 原 scheduler run。

核对请求以原 operation 为审计对象，同时保留本次 call ID。原操作仍 UNKNOWN 时，审计不能改成执行失败可重试。审计写入失败只留下安全异常类型，不改变原操作结果或自动重试业务动作。

这是安全审计缺口修复，不新增保存聊天开关。完整聊天保存开启时继续沿原归档路径，不重复写第二份最小审计。

## 验证与边界

- HTTP/CLI 同码与不同 HTTP 包装矩阵，准确范围/工作表/权限错误及陌生路径 UNKNOWN。
- 真实 Agent/Registry/原 managed 操作账：同/跨 attempt 更新核对、未知追加不重复、换调用编号仍命中原操作、未来独立 run 不清除旧未知记录，分析文件保留。
- 原 RuntimeDB 合同：owner/root/tool/args/current attempt、旧 holder、取消前后、再换代、并发 claim、只读接管禁止重执行。
- 三 owner 的 no-save 审计、秘密/单元格/正文排除、准确原操作与 job/rule、审计故障不改写操作。
- 原定时 waiting/failure policy 回归继续覆盖未来周期；显式配置 `failure_pause_after/backoff` 仍遵守用户策略，默认不会因单轮 UNKNOWN 永久停 job。

未向真实飞书发送请求；官方 API 原子拒绝语义和回包形状仍须真实租户按授权验收，不能把合成供应商当作现场已恢复。
