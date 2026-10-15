# S23 群凭据可信回调交接

状态：本地实现、隔离替身验证，待根集成简单表单与存储线。未提交、推送、部署或真实飞书发卡。

## 实施范围

- 原 ir1 HMAC 与 owner/key 路径不变，签名增加 `presentation={kind:group,chat_id:<原群>}`、`submitter_policy=current_group_member`。`actor_id` 仍是原任务发起人，不能改成最后提交者。
- 只有 `task_credential`、Feishu group owner、签名原群完全一致时接受不同提交者。认证适配器按每次回调的 operator open_id 查询当前成员，再经原宿主认证回环携 `submitter={actor_id,chat_id,current_group_member}`。Gateway 同时验 MAC、期限、owner、可信提交者与真实 chat，之后原 TaskCredential 继续验 message、输入代次、执行账。
- 群凭据不套个人首设/解锁。私人凭据、模型卡、审批仍要求原 actor 与私人锁；给模型卡/审批签群呈现字段会拒绝。
- 成员确认使用官方 GET `/im/v1/chats/:chat_id/members?member_id_type=open_id` 全分页逐项匹配，不缓存资格。tenant token 的 `is_in_chat` 只能确认机器人本身，不能当成真实点击者资格。
- 普通表单值仍只沿认证回环交原加密输入入口，回执整卡替换只含宿主固定结果；不加入普通聊天、模型或日志。
- 追加诊断：Feishu 明确非零 `code` 返回 `CHANNEL_CONTROL_PROVIDER_REJECTED`、数值 `provider_code` 和 `effect_outcome=not_started`，不复制原 msg/data。无 token 为 `CHANNEL_CONTROL_AUTH_UNAVAILABLE/not_started`；网络异常、缺成功消息身份为 unknown。原 `ControlCardDeliveryReceipt` 新增上述事实字段，成功 confirmed。存储线应读取 typed facts。

## 文件

`adapter/private_controls.py`、`user_space/interaction_refs.py`、`gateway_parts/private_interaction_service.py`；追加诊断为 `adapter/feishu.py`、`delivery/control_cards.py`。测试为 `test_credential_group_callbacks.py`、`test_im_control_cards.py`。

## 验证

使用项目 `.venv` Python，五文件 focused：`test_credential_group_callbacks.py test_im_interaction_refs.py test_im_control_cards.py test_task_credentials.py test_im_model_cards.py` 共 66 项通过。改动文件 Ruff、`git diff --check`、strict code-size 通过（无 hard finding，blocked=False）；生成尺寸报告已恢复，不纳入本线 diff。

覆盖：原发起者/同群成员/已离群三身份 SDK 与 webhook 公共回调形状到 mock 认证 HTTP；篡改、错人、跨群/转发、离群、私人未首设、群未首设、模型/审批不可借群权限、成员分页；秘密不进结果。原 TaskCredential 测试覆盖 pending/ready 重复及准确卡/输入限制，简单表单线另负责新 generation、并发/重填和既有版本覆盖授权，需集成后联合复跑。

## 官方核验与未验边界

- 官方成员接口：[获取群成员列表](https://open.feishu.cn/document/server-docs/group/chat-member/get)。本机 `lark-oapi==1.7.1` 官方 SDK 的 `event/callback/model/p2_card_action_trigger.py` 明确有 `form_value`、`context.open_message_id/open_chat_id` 和 operator 字段；已有统一提取器复用这些字段。
- [官方输入框文档](https://open.feishu.cn/document/feishu-cards/card-components/interactive-components/input) 在当前网页抓取中无正文。未获得 `input_type=password` 的官方可读证据或真客户端结果，因此不能说密码类型非法，也不能说它已验证兼容；此次未擅改其类型。用户接受普通群表单，但实际显示、输入隔离/清空和提交必须真实客户端验收。
- 发卡路径直接把 JSON 交消息 HTTP，并无本地 Card SDK schema 校验；原平台非零 code 被丢为 `{}`，随后误归缺回执，本次已修安全诊断。不能从模拟拒绝断言真实事故根因。
- 成员列表查询受平台权限和网络影响，失败关闭本次提交；回调时与查询后的离群竞态由平台事实时点决定，没有本地长期成员授权。
- 原凭据在原任务/owner 内的更新权限，由简单表单线的原记录 `submitter_actor_id`、原 actor 和 candidate/CAS 验证，运输线不另建权限账。

建议下一步：根先合并 form/storage 的唯一 TaskCredential 状态实现并复跑联合回放及严格 gate；其他 agent 可独立审查群/私人授权负例，不能同时改凭据状态权威。随后安排真实飞书原群与本人私聊客户端验收，保留实际平台 code/message ID，未验证前不声称群卡已真实送达。

## 根复审追加：分页与错误边界

`_group_reference_hint` 先限制8192字符、三段ir1版本与签名长度，再解码；不允许任意大提示触发成员查询。当前成员查询最多十页、1.5秒总单调时钟预算，每个成员HTTP调用用剩余时间作为timeout，重复cursor、权限错误、坏页和网络故障产生固定 `IM_GROUP_MEMBERSHIP_UNAVAILABLE`，超限为 `IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED`。完整页明确无成员才是阴性。适配器分别显示“当前不在原群”和“权限/网络无法核验”或“超过预算”，无法核验不送Gateway、不改卡片状态，不把有效卡说成已失效。token读取沿原认证路径；它若已耗完预算，不再开始成员HTTP查询。

追加 hint/大群十页/循环cursor/超时/权限错误与明确非成员反例，群回调加原控制卡共33项通过。建议下一步：根仅同步本追加的 `private_controls.py`、`feishu.py` HTTP timeout签名/调用与 `test_credential_group_callbacks.py`，保持Gateway验证不放宽。

## 群 /model 三按钮共同回调修复

用户报告三按钮统一出现“确认未完成”，本线没有据此认定用户权限错误。源码发现可复现的共同缺口：`cli/adapter.py::_feishu_adapter_config` 原来不传 `gateway_port`，普通消息的 ChannelManager 读取真实配置，控制卡的 FeishuAdapter 却独立默认8420。因此非默认端口部署可出现聊天可用、卡片全回错端口。另一个同链缺口是控制卡原来只读本机凭据文件，未接普通消息已支持的 `gateway_auth_token`。本次两者沿现有adapter config/唯一GatewayClientCredentials透传，不接受按钮或表单自称token，不另造认证源。没有读取线上回调日志，故这是确认的源码缺陷与隔离复现，不冒称已经证明用户现场唯一根因。

原 `_submit_control_request` 只发送一次、不自动重试写操作。401/403、404、凭据缺失、连接失败、2秒超时、非JSON/错误形状分别输出安全固定 error_code/error_stage；HTTP错误只读取已知宿主错误码，不显示供应商/网关原始正文或异常。Gateway handler 将OS/Runtime故障报503/IM_INTERACTION_HOST_UNAVAILABLE/execution，身份/版本/格式拒绝仍403/IM_INTERACTION_INVALID/interaction。私人首设/解锁提示继续沿原码与密码卡，不套群凭据免锁。超时只说结果尚未确认，原模型控制 keyed digest/记录负责同逻辑重放。

验证五文件：`test_card_callback_recovery.py`、`test_im_model_cards.py`、`test_credential_group_callbacks.py`、`test_feishu_quoted_media.py`、`test_feishu_quoted_text.py` 共151项收集，其中150通过、1跳过；Ruff/diff/strict-size通过（hard=0、blocked=False）。真实隔离随机端口 loopback 覆盖原群三个按钮→原handle_private_interaction→原模型选择/默认/私人新增表单，以及私人首设锁正反例；默认8420真实loopback的HTTP403安全诊断通过。18420当时被既有服务占用，测试明确跳过，没有连接该服务；18420配置接线有离线断言。HTTP403/404/超时/格式等另有替身矩阵，宿主503与身份403均无秘密回显。

本次完整最新文件：`agent/adapter/private_controls.py`、`agent/adapter/feishu.py`、`agent/gateway_parts/private_interaction_service.py`、`cli/adapter.py`，追加 `tests/test_card_callback_recovery.py`，已有 `tests/test_im_model_cards.py` 中mock read接受有界read参数；`test_credential_group_callbacks.py` mock同样同步。总文档/树/TESTS由根统一更新。

建议下一步：根同步上述完整最新文件并跑组合严格gate；其他agent适合并行审查错误码和私人锁负例，不应同时改回调函数。真实飞书验收应使用准确安装端口、版本和宿主数据根，收集安全码/阶段后再决定现场根因；不接触已占用18420的既有服务，不能把隔离HTTP成功算真实飞书按钮已恢复。
