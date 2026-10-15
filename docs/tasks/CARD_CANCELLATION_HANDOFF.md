# 凭据与模型卡取消增量交接

状态：独立树本地实现，按集成树只读快照单独打补丁；未提交、推送、部署或真实租户渲染。审批、解锁、OAuth、人设与通用点击审计helper由C线独占，本片只借用helper联测，不重复打包。

## 覆盖与权限

- 凭据各字段/URL组合沿原form外取消按钮；模型选择、新增、编辑、私人表单全部新增form外普通callback，不带form_submit、不参加required验证，不发送表单秘密。
- 有效MAC/channel/原actor/真实message/chat及原行锁仍必需。群凭据及已签名模型selection取消要求当前成员证明；模型select/default/edit/save与私人表单继续原actor规则。模型取消单独签cancellation_presentation，不把群cancel资格扩大成配置权限。
- cancel允许关闭过期准确卡、免个人解锁、关闭开关后的原卡仍可仅取消；不能批准、改模型/密码、撤回有效token/凭据、取消其它任务或绕过代次。
- 首次赢锁取消记录原行cancelled_by，并沿C helper取可信身份索引显示名/稳定ID/UTC clicked_at，安全事实同时进原审计。群重复/两人争点只返回首个决定，不重审计。已完成行只回IM_INTERACTION_ALREADY_HANDLED，不回滚结果。

## 输入生命周期

凭据cancel只退休准确input_id：pending→cancelled、armed=false、resolved_at，移除原task等待refs并复用原截止调度清理。不创建业务Wake。若凭据行已保存但输入退休失败，原卡重试补收口。容量1下取消后可立即申请新卡，原父任务保持active而task_has_waiting_input=false；不取消兄弟或父任务。

原input_tool_boundary另有早取消竞争修复：已cancelled输入直接投影终态，不被迟到工具归档重新armed/发Wake/设置waiting_input_id。

## 集成

共享3文件interaction_refs.py/private_interaction_service.py/adapter.private_controls.py由本片交根；C沿这些文件叠加其它卡，不整文件覆盖。private_service留feishu_oauth→cancel_oauth_card分派，adapter补my_agent_feishu_oauth并通用优先result.message/clicked_by。

依赖C线user_space.interaction_actor_audit.record_interaction_actor；不得单独遗漏该helper上线。本片新user_space.interaction_cancellation.py、gateway_parts/model_control_dispatch.py与tests/test_card_cancellation.py须补总树。设计文档同步IM_CREDENTIAL_COLLECTION与IM_MODEL_CONTROL_CARDS。

## 投递网络竞争

模型卡save不调用probe/discover，凭据卡submit只本地加密并commit ready；实际HTTP验证在ready使用链，cancel此时回已处理。selection add/edit的私人子卡投递改原行dispatch_id两阶段CAS，网络不持原卡锁。取消赢先后迟到投递不能覆盖终态，私人子卡签父control_id并在save前复核原父已完成add/edit，不能继续保存。未知投递不自动重试，保留可取消状态。

## 验证

最终8文件组合161项通过、1项跳过（默认8420端口占用时原隔离回环测试跳过）；覆盖编辑表单、两人竞争、空SDK表单、过期/旧代次、父子投递竞争与早归档竞争。无真实模型/真实飞书请求，真实租户按钮渲染未验。

建议下一步：根先联合本片与C helper及其它卡取消，再联跑相关回归和本地严格gate。可并行只读审查MAC动作隔离、CAS和等待收口；勿并行覆盖共享运输文件，守住取消不授予配置/凭据使用权的边界。


# 全卡片取消与点击审计：C线交接

## 范围与基线

独立树 card-cancel-audit-20261014，基于根 c0cabbb21 与当时未提交代码快照；未改根工作树、不提交推送部署、不联系真实飞书。补丁只交本片取消改动，不含已交 S07、transfer 或借用的 B 模型/凭据/运输文件。

## 实际完成

- 工具审批增加独立“取消”，保留“拒绝”语义。取消映射原 ToolApprovalDecision.cancelled，和批准/拒绝共用原transition锁；签名原actor、原scope/claim/attempt、真实message/chat都要匹配。过期或原执行失活可关闭准确原等待，不会批准新代次或取消任务。先前已决不可改写，重复本卡取消返回首次点击者。
- 密码首设、解锁、改密增加form外独立callback取消按钮，无form_submit，空表单即可取消。生产发卡用原ir1签名并保存真实卡回执，原SessionLockStore同用户行新增card_nonce/mode/status/expiry/message/chat/actor字段；不新增权限账。nonce替换使旧卡失效，原跨进程文件锁串行submit/cancel。取消不改password_hash/last_activity，不解锁、不锁定、不清密码、不续送消息队列；取消后迟到submit不能运行密码服务。
- OAuth授权链接卡增加取消。MAC/原actor/state/input及真实卡回执相等才可退休原pending/exchanging；原输入依赖沿原函数结算。网络中取消后，成功ready或失败回执都不能改写cancelled。已完成ready不撤销已有token，返回原卡已处理。
- 遗留长期人设卡保留确认/不写并增加取消。原pending token与owner/actor权限不变，confirm/cancel共用原记录锁；取消记录留在原待确认文件，不写SOUL，过期可关闭，重复不重复审计。“不写”复用同取消语义。全仓当前未发现此legacy卡构造/发送的内部调用点，public builder和adapter回调仍保留，因此没有为它另造签名协议。
- 新interaction_actor_audit.py只读原identity_store的展示名，稳定ID兜底；名字不授权。各赢锁首决在原行保存actor_id/actor_name/clicked_at，原AuditLogger记录IM_CARD_CLICK/verified_click；不收表单、签名引用、密钥、密码或设定正文。日志只声明核验点击，不假称业务完成。群卡公开回执由B共用adapter使用同字段展示。

## 与B协调

B独占interaction_refs.py/private_interaction_service.py/private_controls.py：decision=cancel允许正确MAC的过期引用，所有cancel免个人锁，但原actor/指定群成员仍核验；OAuth typedkind及cancel_oauth_card分派；统一优先result.message并展示clicked_by，ALREADY_HANDLED为固定toast。本片借用这3文件测试但不打包。

B另外修改model_control_cards、model_control_dispatch、task_credentials、task_credential_forms、interaction_cancellation、conversation/input_tool_boundary及其测试，本片只借用联测，不打包。根先合B共享路由再合本片。

## 本片文件

生产：user_space/interaction_actor_audit.py、session_lock/card_control.py、session_lock/store.py、session_lock/feishu_cards.py、conversation/im_tool_approval.py、conversation/agent_tool_approval.py、user_space/feishu_oauth.py、adapter/feishu.py、adapter/feishu_card.py、capability/persona_pending.py。

测试：新增test_card_cancellation_controls.py；旧test_session_lock.py、test_feishu_first_contact_lock.py将无签名mock改为原生产prepare/bind的签名/nonce/回执，test_feishu_card.py断言独立第三个取消按钮。没有为旧无签名生产卡增加回退入口。

## 验证与边界

定向新增20项覆盖空表单、原密码锁事实不变、错人/跨chat/转发、旧nonce、篡改、过期重放、密码submit/cancel竞态、审批取消/批准竞态、免锁、原任务不取消、OAuth已ready不撤销、取消后迟到成功与失败、过期OAuth及人设、persona confirm/cancel CAS。扩展联测文件列表：

```
agent_py_agent/tests/test_card_cancellation_controls.py
agent_py_agent/tests/test_card_cancellation.py
agent_py_agent/tests/test_session_lock.py
agent_py_agent/tests/test_feishu_card.py
agent_py_agent/tests/test_im_tool_approval.py
agent_py_agent/tests/test_feishu_oauth.py
agent_py_agent/tests/test_feishu_first_contact_lock.py
agent_py_agent/tests/test_credential_callback_replay.py
agent_py_agent/tests/test_card_callback_recovery.py
```

本线6文件138项通过；借用B最终模型/凭据依赖后，另3文件59通过、1跳过（保留原环境端口占用保护），合计197通过、1跳过。新增20项最后增强原待解锁消息队列保留回归后再定向复跑。全仓Ruff、doc-sync、diff检查通过，strict code-size hard=0。

仅mock运输/供应商，原owner/签名/锁/密码/输入/审批状态均生产实现。真实Feishu客户端渲染未验；密码卡既有input_type=password/show_icon兼容仍未核实，已删除“V6.8支持”的无证据注释，不能把mock回执算作密文显示或真实送达验收。

## 建议下一步

根先合B共享取消入口与actor传递，再应用本片最小增量，按上述列表跑focused与严格gate，并同步总文件树/TESTS/设计台账。可由另一代理并行只读审查密码nonce迁移和OAuth时序；真实发送只能用已授权测试环境，不要把离线回归等同真实客户端验证。
