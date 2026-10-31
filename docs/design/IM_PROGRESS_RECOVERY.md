# IM 进度恢复合同

## 2026-10-31 W0/W2/W5/W6 落地边界

- **原话题聚合**：`ChannelManager` 先在 `GatewayIngressStore` 持久受理，再由原 worker 提交 Gateway。原 `PendingGatewayReply` 冻结 `reply_policy`、供应商 `provider_thread_id/root_id/parent_id` 与入站 `message_id`；内部会话编号与供应商话题编号不可混用。进度首发、同卡编辑和最终正文沿原回复锚点，不因当前配置变化改绑话题，不新建队列。
- **草稿与真实回执**：`progress_draft.py` 在原 pending 内保存展示消息身份、源 revision、步骤及组件首次 dispatch/供应商回执；Card 2.0 首发后只编辑同一个确认 ID。未知首发只在原生幂等窗口内用相同 key 和冻结内容对账；未知、展示失败或编辑失败不表示任务失败，也不能自动替换新卡。最终消息沿自己的稳定 key 与组件账收口。
- **有界阶段投影**：`delivery/execution_view.py` 与 `adapter/feishu_progress_card.py` 已实现纯投影。只消费结构化真实工具事件；模型宣称完成不产生步骤。保留 `call_id` 顺序、阶段、ok/handler_executed、耗时与引用；迟到 started 不覆盖已确认终态。未知总数仍未知，长标签及最终 JSON 字节预算有界，卡片默认折叠；投影不能授予 claim、重放或权限。
- **实际接线与来源边界**：历史 W0 卡片链失败定位到兼容 renderer；上游 `7fa990672` 已把 `DeliveryService.deliver_progress()` 接入 `adapter/feishu_progress_card.py`，同一外发 JSON 等值断言复测通过，W0 未修改生产代码或用 monkeypatch 补接。原始 Gateway 进度轮询仍用 W5 `project_execution_steps()`；W0 在拉取网络替身侧用真实 W6 通用投影构造公开批次，验证它经原草稿、worker、投递服务和实际 renderer 到 provider 边界，不代表生产轮询已改用 W6 通用投影。当前六节点全通过，同时核对步骤身份/终态/revision、原话题路由、创建→编辑→最终正文顺序及确切业务正文；不证明真实飞书显示或网络可达。
- **前台恢复交接**：`ForegroundRecoveryHandoffRef` 的落地形式是 `foreground-recovery-handoff.v1`：同 thread、attempt_ids、原恢复 event_ids 与 source_revision 的结构化字典，由 RuntimeRepository 原事件重建并校验，随 `_ForegroundRecoveryRunParams` 传递。不是说明文案，也不是重新执行旧操作的许可证。unknown 与 pending_handoff 期间后台原来源保留；准确原发起人的前台接手成功确认后才解除屏障。同 owner 的健康任务仍可独立 claim。
- **接班观察点**：W0 在临时文件、真实 RuntimeRepository/会话账本上验证排空门、cleanup 标记、原请求重排、同代 ready 后通知和交接引用重读。提供方、进程存活观察与模型结果使用测试替身；真实换进程、真实飞书和线上模型端到端均未验证。

以下保留原 R01/R02 恢复合同，不把历史本地结论升级为真实 IM 验收。

2026-10-14，R01/R02 本地实现，真实 IM 验收仍待主线。

默认 120 秒心跳与 100 字预览不变。心跳来自真实工具/model 请求的 monotonic 经过时间，不调用 LLM。关闭或取消沿原观察资格立即失权；单次 sink 故障不再终止线程，按原间隔恢复，连续三次失败才停止，成功清零。沿 OpenAI Agents Python sandbox retry 的有界尝试/成功复位思想，不引入其依赖或请求重试。

进度仍只使用原 pending 的 message_id、claim_epoch、dispatch 与 progress_cursor。回执新增 failure_kind（permanent/transient/unknown）；本地权限或能力拒绝归 permanent，provider bool 与捕获异常归 unknown，不解析异常正文。已知消息 ID 的替换编辑可按原记录恢复；退避 2/4 秒，连续三次失败停用展示，成功清零。未知首发不盲重发，仍允许最终回复；进程退出留下的冻结 dispatch 继续走既有 provider 查询/幂等窗口合同。

失败编辑消费其游标，后续退避结束发送当前最新候选，不重放旧内容。外发资格仍由原 claim/epoch CAS 和 dispatch 活进程裁决；不建立第二队列。最终回复先轮询，已有未完成的冻结 dispatch 先按原身份对账；展示失败不吞最终消息。

完整候选先经 common.log_redaction 脱敏再裁剪，更新时间在预览预算中保留。off 模式仍给工具名、真实经过时间、等待工具结果与更新时间；模型等待沿原 model_liveness。/status 读取最新 typed running/finished/interrupted/model_liveness/content 事件，旧 started 只声明已开始且等待当前更新，缺更新时间明确展示未知；不从自然语言正文推断进度。

审批缓存旧测试缺 requested_at/expiry，当前合同正确 fail closed。只为该合成测试补结构化未来 expires_at；不放宽生产授权。

建议下一步：主线组合 R09 脱敏与九项 focused/strict gate；其它 agent 可只读复核首发未知、同 ID 编辑与停止优先，不触碰真实服务或并行写这些文件。
