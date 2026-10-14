# 飞书群引用附件

群里单独发送文件时，消息可能无法同时明确提及机器人。本功能支持：先把文件上传到该群，再**引用那条文件消息，明确 @机器人并说明任务**。不读取“上一条消息”，不自动处理没有触发机器人的文件，也不递归追溯更早的引用。

`feishu_quoted_media_enabled` 默认 `true`，可关闭新增的单条消息读取。群聊是否触发仍守原 `feishu_group_require_bot_mention`；管理员显式关闭原群门时，本功能尊重已有调用规则。私聊引用和显式控制命令不新增查询请求。

## 原主链与身份

1. WS/webhook 签名、可信群提及门与认证仍先于原 `ChannelManager.route_message`。
2. 原入站账冻结当前发送者、当前群/话题和 owner，先落 `prepared`，随后唯一 worker 才查询当前 `parent_id`。
3. GET 响应只选择唯一、准确的父消息编号，必须与当前群 `chat_id` 相同、明确未撤回，并具有相同话题证明。原根文件本身可能无 `root_id/thread_id`，当前 `root_id == parent.message_id` 是合法根证明；非根引用必须有共同 root/thread 锚且无冲突。
4. 复用 `extract_feishu_content` 只提取资源。父正文和父发送者不成为当前模型指令或身份；合并转发的其它返回项不是附件来源。
5. 每个引用资源附带准确 `source_message_id`，与本次 parent/chat/current-message 证明再次比对后，原资源下载器用该 ID 请求附件。当前消息自身的附件仍用当前 ID，不消费任意 media 自带的 source 字段。
6. 目录继续由当前消息的 `/ask` owner resolver 决定，落到该 owner 的 `media/input`，原 no-follow、私有原子 0600 写和跨 owner 读权限保持。全部当前/引用资源成功才注入路径；部分下载不会提交部分路径。
7. 精确 Gateway 请求仍在原入站账 `payload_ready` 冻结。冷重投沿原请求正文，不重新查询父消息或生成另一任务。

没有第二个附件缓存、消息账、队列或新的身份表；查询最多一次，资源下载沿原入口。只有原 prepared 阶段尚未冻结时的崩溃恢复可能再次读取同一 parent，文件名与归属保持稳定。

## 失败事实

权限不足、不可见、撤回、响应编号/群/话题不符、资源缺失或下载未完成，都以有界 `quoted_media.v1` 结构附到原用户 prompt。包含 `status/error_code`、父消息短引用、已知 HTTP/provider 数值代码及静态原因说明，不携带 API `msg`、响应正文、文件 key、异常 str、父发送者或 token。正常面向用户的说明由模型生成；没有把获取失败当成“无附件成功”，也不会让明确失败永久占据准备重试。

即使父 GET 已经失败，同条消息的当前附件仍须通过原下载与 owner/no-follow/私有落盘硬门；失败留原 durable 行，不归类为引用读取失败，也不回退管理员或共享目录。部分下载已落私有目录的文件可能存在，但路径不作为完整输入提交。

## 飞书权限与未覆盖范围

使用已有宿主应用 token，不给任何 owner 分发 `app_secret`。官方单条消息 GET 需要机器人在该群，并具有基础 `im:message` 或 `im:message:readonly` 以及 `im:message.group_msg`。仅 `group_at_msg` 接收提及权限不能保证能读取先前文件；缺少权限要在飞书应用管理台补授权，代码不能绕过。[官方查询消息接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/get)

文件继续使用消息资源接口，`message_id` 和资源 key 必须属于同一消息；不可见历史、撤回、保密下载限制和外部群等供应商约束仍有效。此切片没有增加大文件 Range、转发子附件递归或云文档 API；云文档应发送链接并明确 @机器人，读取授权仍走已有云资源工具。[官方下载消息资源接口](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message-resource/get)

## 验证边界

定向测试用合成 GET/资源字节替身，实际执行原群门、原入站账、owner resolver、私有文件落盘和 ReadFileTool 权限。覆盖普通/admin 当前主体、跨群/私聊、撤回/异常响应、topic/root、控制/开关关闭零新请求、混合资源源 ID、路径安全、部分失败及冷重投。

未调用真实飞书租户，不确认应用当前 scopes、实际父消息 payload 或用户现场文件的可见性；这些应由最终应用环境验收。公开接口参考与本地实现通过不能替代真实群送达证明。
