# IM 进度恢复合同

2026-10-14，R01/R02 本地实现，真实 IM 验收仍待主线。

默认 120 秒心跳与 100 字预览不变。心跳来自真实工具/model 请求的 monotonic 经过时间，不调用 LLM。关闭或取消沿原观察资格立即失权；单次 sink 故障不再终止线程，按原间隔恢复，连续三次失败才停止，成功清零。沿 OpenAI Agents Python sandbox retry 的有界尝试/成功复位思想，不引入其依赖或请求重试。

进度仍只使用原 pending 的 message_id、claim_epoch、dispatch 与 progress_cursor。回执新增 failure_kind（permanent/transient/unknown）；本地权限或能力拒绝归 permanent，provider bool 与捕获异常归 unknown，不解析异常正文。已知消息 ID 的替换编辑可按原记录恢复；退避 2/4 秒，连续三次失败停用展示，成功清零。未知首发不盲重发，仍允许最终回复；进程退出留下的冻结 dispatch 继续走既有 provider 查询/幂等窗口合同。

失败编辑消费其游标，后续退避结束发送当前最新候选，不重放旧内容。外发资格仍由原 claim/epoch CAS 和 dispatch 活进程裁决；不建立第二队列。最终回复先轮询，已有未完成的冻结 dispatch 先按原身份对账；展示失败不吞最终消息。

完整候选先经 common.log_redaction 脱敏再裁剪，更新时间在预览预算中保留。off 模式仍给工具名、真实经过时间、等待工具结果与更新时间；模型等待沿原 model_liveness。/status 读取最新 typed running/finished/interrupted/model_liveness/content 事件，旧 started 只声明已开始且等待当前更新，缺更新时间明确展示未知；不从自然语言正文推断进度。

审批缓存旧测试缺 requested_at/expiry，当前合同正确 fail closed。只为该合成测试补结构化未来 expires_at；不放宽生产授权。

建议下一步：主线组合 R09 脱敏与九项 focused/strict gate；其它 agent 可只读复核首发未知、同 ID 编辑与停止优先，不触碰真实服务或并行写这些文件。
