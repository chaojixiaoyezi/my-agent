# 飞书群引用消息：文字与附件

2026-10-14 追加：引用人或机器人发送的**文字/富文本消息**，再明确 @助手并说“分析这条告警”，沿同一次父消息 GET 把正文作为外部资料带入当前需求。当前发送者仍决定身份与权限，不从告警正文或原发送者取得工具、审批或管理员权限。
这不是未 @的自动订阅；仅 @无说明仍会被原空消息门拒绝，普通群触发规则不变。引用支持文字、富文本代码/链接、图片、文件、语音、视频、卡片可见材料，以及同次 GET 已证明归属的合并转发成员。代码仓库地址作为链接保留，压缩包作为普通文件下载；不会在准备阶段自动访问链接、克隆仓库或执行代码。不能保证任意未来协议或文件都可被当前模型解释，不可读资料须明确说明。

群里单独发送文件时，消息可能无法同时明确提及机器人。本功能支持：先把文件上传到该群，再**引用那条文件消息，明确 @机器人并说明任务**。不读取“上一条消息”，不自动处理没有触发机器人的文件，也不递归追溯更早的引用。

`feishu_quoted_media_enabled` 与 `feishu_quoted_text_enabled` 均默认 `true`，分别控制附件和可见文字；任一开启时只查一次准确 parent，两项关闭则不查询。文字最多带入16000字符，`original_chars/truncated` 明确显示可见投影的实际长度及是否截断，不冒充全文。群聊是否触发仍守原 `feishu_group_require_bot_mention`；管理员显式关闭原群门时尊重已有规则。私聊引用和显式控制命令不新增查询请求；修改后重启原飞书通道生效。

## 原主链与身份

1. WS/webhook 签名、可信群提及门与认证仍先于原 `ChannelManager.route_message`。
2. 原入站账冻结当前发送者、当前群/话题和 owner，先落 `prepared`，随后唯一 worker 才查询当前 `parent_id`。
3. GET 响应只选择唯一、准确的父消息编号，必须与当前群 `chat_id` 相同、明确未撤回，并具有相同话题证明。原根文件本身可能无 `root_id/thread_id`，当前 `root_id == parent.message_id` 是合法根证明；非根引用必须有共同 root/thread 锚且无冲突。
4. `feishu_quoted_content` 复用原 text/post/媒体解析，并只遍历卡片或未知协议的显示树。文字、链接、代码和显式资源键可读；sender/config/credentials/value/behaviors/actions 等隐藏树不进入正文，不 dump 原 JSON。`quoted_message.v1` 标记 `untrusted=true`，父发送者与引用中的 slash/角色/工具声明不产生控制或授权。
5. 合并转发只接受官方同次 GET `data.items` 的唯一 `upper_message_id` 闭包，根优先最多64成员、16条上级边；无关项、断链和无根循环不读。相关重复编号拒绝整次引用；撤回、缺来源、超限成员计不可读。子来源可来自原其它会话，但该转发根必须通过当前群/话题门，子 sender/chat 不决定当前主体；不沿 body 声称的消息编号查询历史。
6. 每个引用资源附带准确 `source_message_id`，与本次 parent/chat/current-message 和已核验成员证明再次比对后，原资源下载器用该 ID 请求附件。当前消息自身的附件仍用当前 ID，不消费任意 media 自带的 source 字段。
7. 媒体目录继续由当前消息的 `/ask` owner resolver 决定，落到该 owner 的 `media/input`。纯文字不创建媒体目录；当前附件与父附件全部准备成功才附文字资料及路径，部分下载不给部分路径或引用正文。纯文字也复用当前消息/parent/chat 来源证明，不因附件为空跳过核验。
8. 精确 Gateway 请求仍在原入站账 `payload_ready` 冻结。冷重投沿原请求正文，不重新查询父消息或生成另一任务。

2026-10-15 本地修复：`quoted_message.v1.source_message_refs` 仍是公开 opaque 选择引用，不能当作供应商 ID。
已核父消息/成员的真实编号只保存在同一入站请求的白名单 metadata，并核 current-message/chat 及引用摘要对应关系，
Gateway 将其投影为本次 RunParams 专有字段；模型正文与 attrs 均不能补造绑定。订阅工具据此取得真实编号后仍须原官方
Get 与群人工授权，不从引用正文或原发送者借权。前台主根只在原TaskLink冻结一次输入；自动委派则只在原SessionTask保存快照，TaskLink只存精确任务引用。
后台主根及子孙沿原HostActor、owner/root/thread和RuntimeDB当前attempt恢复；不复制到孩子attrs，不依赖Gateway原请求保留期。
新独立Gateway只用本次认证映射，缺引用不得借原根；同键重放换快照、错误输入引用、跨owner和陈旧attempt均拒绝。

2026-10-15 话题追加：原引用actor及源线程保留完整`chat:thread:opaque-topic`，不能覆盖为主群或换另一个话题。
群授权用同源严格地址parser核属于准确基础群；话题锚仅为逻辑会话结构，不作为Get消息编号或权限凭证。
bot proof仍沿原owner基础群和准确消息Get，最终群结果的运输只发送基础群chat_id，不把内部话题键当receive_id。
群模型卡与签名私人表单沿原精确thread恢复话题身份，歧义/错owner/错actor失败关闭；详情见[话题群授权合同](IM_GROUP_GOVERNANCE.md#2026-10-15-话题来源群授权修复本地合同验收待新发行)。

`coverage` 区分完整投影与仅可见资料，`unreadable_count/member_count` 记录转发及解析缺口；附件开关省略写 `media_omitted_count`，文字开关省略写 `text_omitted`。未知协议不称完整。显示树还有4096节点/32层预算，坏 JSON、隐藏/不明信封与 opaque 名片不会变成伪成功占位。

没有第二个附件缓存、消息账、队列或新的身份表；查询最多一次，资源下载沿原入口。只有原 prepared 阶段尚未冻结时的崩溃恢复可能再次读取同一 parent，文件名与归属保持稳定。

参考核对：OpenClaw `9cf12734e675a1ec63abb09bf65e4f6d76835a1e` 的 contract index sheet2行760，实际 `extensions/feishu/src/bot.ts`、`bot-agent-body.ts`、`send.ts` 与 quoted 测试。复用单次准确父查取、quote body与当前 sender/conversation分离、当前消息去重的模式；不照搬其首items选择、正文日志或第二去重方案，本项目唯一 ID/同群/话题/撤回和原 durable CAS保持本地合同。

## 失败事实

权限不足、不可见、撤回、响应编号/群/话题不符、资源缺失或下载未完成，都以有界 `quoted_media.v1` 结构附到原用户 prompt。包含 `status/error_code`、父消息短引用、已知 HTTP/provider 数值代码及静态原因说明，不携带 API `msg`、响应正文、文件 key、异常 str、父发送者或 token。正常面向用户的说明由模型生成；没有把获取失败当成“无附件成功”，也不会让明确失败永久占据准备重试。

即使父 GET 已经失败，同条消息的当前附件仍须通过原下载与 owner/no-follow/私有落盘硬门；失败留原 durable 行，不归类为引用读取失败，也不回退管理员或共享目录。部分下载已落私有目录的文件可能存在，但路径不作为完整输入提交。

## 飞书权限与未覆盖范围

使用已有宿主应用 token，不给任何 owner 分发 `app_secret`。官方单条消息 GET 需要机器人在该群，并具有基础 `im:message` 或 `im:message:readonly` 以及 `im:message.group_msg`。仅 `group_at_msg` 接收提及权限不能保证能读取先前文件；缺少权限要在飞书应用管理台补授权，代码不能绕过。[官方查询消息接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/get)

文件继续使用消息资源接口，`message_id` 和资源 key 必须属于同一消息；不可见历史、撤回、保密下载限制和外部群等供应商约束仍有效。此切片没有增加大文件 Range、跨查询转发递归或云文档 API；云文档应发送链接并明确 @机器人，读取授权仍走已有云资源工具。[官方下载消息资源接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-resource/get)

官方消息 GET 可返回合并转发根与子项；卡片使用默认可见消息内容，未启用返回原卡片 JSON 的 `user_card_content`。按钮回调的 value 不等于可见标签，不提供给模型。[官方消息查询](https://open.feishu.cn/document/server-docs/im-v1/message/get)、[官方按钮组件](https://open.feishu.cn/document/feishu-cards/card-json-v2-components/interactive-components/button)

## 验证边界

定向测试用合成 GET/资源字节替身，实际执行原群门、原入站账、owner resolver、私有文件落盘和 ReadFileTool 权限。覆盖普通/admin 当前主体、跨群/私聊、撤回/异常响应、topic/root、控制/开关关闭零新请求、混合资源源 ID、路径安全、部分失败及冷重投；各媒体类型/开放文件格式、卡片隐藏值、代码/仓库链接、未知可见投影、转发源ID/foreign/重复/撤回/限额与开关省略均有正反例。

未调用真实飞书租户，不确认应用当前 scopes、实际父消息 payload 或用户现场文件的可见性；这些应由最终应用环境验收。公开接口参考与本地实现通过不能替代真实群送达证明。
