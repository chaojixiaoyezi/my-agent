# 群机器人事件订阅

状态：2026-10-15 本地实现与定向验收中，尚未发布，不代表真实租户的无 @ 机器人事件已获权限或验收通过。

## 权威与执行

事件订阅是原 scheduler 的 `schedule.kind=event_source`。监控仍只存在 owner 原 `store.json` 的 job/rules、runs 和原 history；执行复用原 SessionTask、Wake、领取、后台模型与投递，不新建队列、轮询器或权限账。原 `due_index` 只投影真实 pending 的下一次关注时间，供冷 owner 和重启恢复；未建订阅或没有事件不会触发平台历史查询。

创建、更新、恢复、手动运行使用群能力门 `monitors.create`；修改助手配置使用 `assistant.configure`。授权撤销约束未来操作，不把创建授权变成已有任务的运行许可证。运行仍核原 owner、冻结创建者/来源、当前 job 与事件代次。取消当前运行保留未来订阅；取消订阅使用原暂停或删除，免能力门但保留原操作者范围检查。暂停、删除、条件或来源修改先退休旧代，旧代不得再获准开始模型或外发；已准入调用不可假称已撤回，清理未静止继续沿原取消意图。

每条事件规则的 `event_source_state` 保存在同一个 job，包含稳定 `generation`、近期 receipt、退休水位、pending 及派发时间。正常预留、开工和收尾会改变 job CAS version，但不退休事件 generation。新单规则事件 job 规范为原 isolated 执行模式；复合规则沿原 isolated 规则视图。事件内容只追加为本次模型的不可信资料，不修改 creator，也不成为权限、配置或控制指令。

## 来源与运输

模型只提供当前群准确的原引用消息编号。宿主通过现有 Delivery registry 的 REST adapter 精确 Get 消息，核对 message_id、chat_id、未删除以及 `sender_type=app/id_type=app_id`，得到 canonical 来源 app_id，并保留 `source_message_id` 证明。它与接收事件的 `sender_type=bot/sender_id.open_id` 属不同 namespace，禁止按名字、前缀或字符串相等映射。多个来源机器人分别核验；接收 app、群租户、chat 和来源 app_id 都在计划中固定。

SDK/webhook 保留 sender_type、接收 app/tenant、消息及话题身份。user 走原人类入口；bot/app 始终走明确 observation 入口，不能执行授权命令或生成 HostChannelActor；未知来源拒绝。默认群 @ 门继续约束普通人；原部署显式 `feishu_group_require_bot_mention=false` 偏好仍保留，新订阅不改变它。

观察先进入原 GatewayIngress 的 `kind=observation` 行，禁止 human user_id。原 worker 带实际宿主凭据调用 `/channel-observations`，不调用 `/ask`、不创建占位或普通回复 watcher。相同 app/tenant/chat/source/message 的重投保留稳定 Ingress refs；成功或明确 ignored 才终结，背压和不可核验保持重试。Gateway 通过原 owner resolver 选准确群；最终机器人 app_id 由同 message 的 Get 证明，群 tenant 由同 chat 元数据规范化，不采用外部邀请人的 tenant。

自然语言条件仅由原 LLM 研判。机器只过滤结构化来源、来源代次、时间和资源界限。原 `origin+explicit` 投递协议决定是否回原群，不从回复正文猜“需要告警”。引用、卡片或其它机器人声明不会授予权力。

## 积压、去重与回环

首批可以立即预留；后续最小间隔 60 秒。每条规则 pending 最多 16 个事件，已有运行保持原冻结批次，新事件合入下一批。任意命中订阅已满时，本事件全部订阅一起背压，不部分写入或假称 accepted。每条已登记事件完整保留原 Ingress refs；64 KiB 限制只作用于模型 JSON 视图，按实际转义字节计算，完整列出每条引用及截断事实，原 pending/run 内容不随视图截断。

近期精确 receipt 最多 4096 项，和整个 job 一起服从原 owner quota。迟到窗口固定 3600 秒：在原锁事务中推进 `retired_through`，关闭 provider create_time 小于或等于边界的完整时间桶；同 timestamp 的未知 ID 也不能重放。已见 provider 最大时间与宿主当前时间共同限定退休边界，未来超过宿主 300 秒或非有限时间返回明确重试错误，不能抬水位清除真实去重事实。退休 receipt 不删除原 pending/run 批次或其 Ingress refs。

窗口内保留完整摘要检查同 ID 异体。窗口外首次未见消息、以及丢失 HTTP 回执后的旧重投，明确返回 `EVENT_SOURCE_STALE`，不声称已分析；原 job 记录 stale 重投次数和最后摘要引用。正常持续流会自然退休近期 receipt；近期超过 4096 或 pending 满才背压，年龄退出窗口后可继续，无需周期性删除重建监控。

自身机器人及可由 root/parent 精确 Get 证明回复自身出站消息的因果回环会被拒绝。订阅精确来源、批次最短间隔、有限积压、近期容量和原 owner 配额共同限制未知外部因果的成本；不能从外部机器人自由文本推断完整跨应用因果链。原取消和 generation 核验也作用于冻结重投；准确事件 run 即使已归档仍从原 history 读取，不得因为离开活动 store 就退化为普通外发。

## 模型与运输开始边界

实际 backend.generate、后台 channels.deliver 和显式 send_message.deliver 前，共用原 job `event_source_state.active_effects` 的 `begin_event_effect` CAS。控制退代先提交，则新调用没有准入、模型或运输调用次数为零；开始 CAS 先提交，则调用已准入在途，控制仍可立即退代并拒绝后续操作。这个线性化点不声称磁盘 CAS 与 TCP 字节物理同时，也不持 scheduler 锁跨慢模型或 HTTP 请求。

在途标记至多 64 项，绑定原 call_id / delivery idempotency_key、run、generation、claim_epoch、PID 与启动指纹，同 nonce finally 释放。崩溃遗留只复用原 runner 三态死亡证明回收；单凭超时、旧 epoch、run 终态或无法核实进程不能宣布旧运输已结束。死亡回收仅释放容量，不重放未知副作用，不改原模型/投递结果账；活或未知的满容量明确拒绝新准入。原 pending cancellation 事实不会因为释放标记而假称已经撤回。

同代已归档 run 的冻结未外发信封可沿原 outbox 重试，原 history 不追加或复活运行；退代后拒绝新运输。冻结 `external_sent=True` 已证明外发成功时，只按原回执补 canonical，绝不再次发送，不能让新的开始门改变已有外发事实。普通无事件任务保持原路径且不写 scheduler。子代理以原 SubAgentTask/root 及当前 runner 身份找来源，不能用模型自报 task_id 逃逸。

## 群角色与 provider 事件

当前成员、群主和机器人自身成员资格共用同一宿主 REST adapter，不缓存第二份权限。群详情以 `chat_mode=group/topic` 判断群；`chat_type=private/public` 是可见性，不能作为群/单聊判断。人类成员必须完整分页，并核对 open_id 类型；tenant token 的 is_in_chat 只证明本机器人在群。群主必须有明确 open_id；没有消费的可选 manager 字段缺失不阻断群主核验。

官方 bot/user added/deleted 事件归一后写原 `tool_policy.group_authorization`。SDK header 的 event_id 和整数 create_time 负责幂等及事件时序；operator 仅保留官方 open_id。事件 namespace 的 tenant 与当前群快照同源。删除后若 GetChat 已失权，可信删除事件只沿同 app/chat 原绑定做纯失效，不用于新增邀请人或授予权限。

## 平台权限与验收边界

优先核验只收其它群机器人的 `im:message.group_bot_msg:readonly`；官方接收事件权限表列出该 scope，但具体租户可用性须平台实际确认。`im:message.group_msg.include_bot:read` 会扩大到人类及其他机器人，不能静默替代窄权限。`im:message.group_at_msg.include_bot:readonly` 仍要求 @；普通 `im:message.group_msg` 不包含机器人。权限缺口只提供明确诊断，不自动申请或修改真实应用权限。

事件接收、精确消息读取、群信息与完整成员读取各守平台权限。没有事件不能单凭静默推断“已获 scope”或“没有符合消息”。仅允许原飞书官方域名出站，运行时不下载依赖。

一手文档：

- [接收消息事件及各 scope 语义](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [精确获取消息及 sender/id_type](https://open.feishu.cn/document/server-docs/im-v1/message/get)
- [群详情、chat_mode 与 chat_type](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat/get)
- [完整群成员分页](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-members/get)
- [判断当前 token 主体是否在群](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-members/is_in_chat)
- [机器人加入事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-member-bot/events/added)
- [机器人移除事件](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/chat-member-bot/events/deleted)

## 定向验证

`test_scheduler_event_sources.py` 覆盖原账本/运行/历史、跨范围拒绝、重启去重、同时间桶退休、4200 条连续流、近期满容量背压、非有限/未来时间、完整 refs、JSON 转义预算、busy 合并、稳定代次、取消/暂停/删除/更新、原 due_index 冷恢复与归档后迟到投递。

`test_scheduler_event_effects.py` 沿原完整 Agent/Store/后台运行固化四条真实生命周期屏障，覆盖暂停在 mark_run_running 后或 origin 校验后先提交，仍分别零模型/零运输；补充开始先赢后的在途事实、原 nonce 清理、64 项崩溃回收、活/未知进程不回收、同代归档重投不复活 run/history，以及原子代理显式发送的暂停正反例。模型与运输使用替身并硬断网络，不能代替真实平台验收。

`test_feishu_bot_observations.py` 覆盖真实 SDK 字段归一、bot/app 与 human 分流、默认无 @ 人类拒绝、自身与跨接收实例拒绝、原持久 Ingress、无普通 watcher、app_id/open_id 分离、精确 Get 同群、完整成员及 private/public/topic 群形状、单聊拒绝和宿主凭据先于请求体读取。真实事件、模型研判和实际群回复仍须在发布候选中按准确 source/actor/app 验收，不能用这些离线测试替代。
