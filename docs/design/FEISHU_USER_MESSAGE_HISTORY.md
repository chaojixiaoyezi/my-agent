# 飞书本人会话与历史读取

状态：源码切片；未代表真实应用授权或上线验收。公开工具仍为 `channel_messages`，不新增凭据账、订阅、后台轮询或跨 owner 搜索。

- 默认 `selector=current_group` 保持当前群规范归属、机器人/成员/租户证明和租户身份；模型不能在此模式指定别的群。
- `selector=user` 仅私人 owner，沿既有 `feishu_oauth.user_access_token` 的 owner/actor、宿主 run scope、密文记录、刷新与撤销 CAS。群 owner 不能借私人用户授权读取私聊。受控 CLI 在每次业务请求前验证 token 对应用户。
- `action=list_chats` 对官方 `/im/v1/chats` 显式发送 `types=p2p,group`；会话来自该用户、该应用实际可见范围，不从应用权限推导全量私人会话。`action=list` 使用准确 chat_id 及原七天时间窗。
- 用户身份需要 `im:chat:read`（会话列表），消息读取及会话类型、成员核验需要 `im:chat:readonly`、`im:message:readonly` 和相应的 `im:message.p2p_msg:get_as_user` 或 `im:message.group_msg:get_as_user`。本人读取群历史时，在分页和返回前，用同一人的令牌核验会话类型和本人的在群状态。实测在同一个内群，本人在群、机器人不在群，飞书对应用身份返回 HTTP 400、错误码 230002，对本人身份返回 HTTP 200、错误码 0 及消息。平台对目标会话作出的拒绝仍原样保留。机器人所属群的应用身份读取、群共享会话和群订阅保留各自的成员核验。
- `action=status` 不联网、不读取 token，返回结构化 required_scopes、permission_settings_url、official_api_docs 和原 `feishu_auth authorize` 入口。permission_status=not_verified 不声称权限已获批。用户读失败同样携带上述事实，绝不要求向模型粘贴 token。
- 默认开放历史能力入口，仍可显式关闭；启用入口不会自动调用网络或创建新授权。用户 CLI 后端未配置、授权缺失/撤销或 owner/actor 不符均明确拒绝，不降级为机器人身份。

## 单机免回调授权

部署者在共享数据目录的 `config/capability_config.yaml` 设置本人授权。留空回调时，Gateway 向飞书申请设备授权，用户在机器人私聊里点卡并在飞书完成同意。机器只需允许出站 HTTPS 到 `accounts.feishu.cn` 和 `open.feishu.cn`；机器人已有的长连接仍使用原配置。保留已登记的 HTTPS 回调时沿原 PKCE 流程。

```yaml
feishu_user_oauth_enabled: true
feishu_oauth_redirect_uri: ""
feishu_oauth_reauthorize_hours: 8760
```

小时数须为正整数。改成 `24` 并重启 Gateway 后，距上次本人同意已满 24 小时的用户，下次使用个人身份时需要重新同意。刷新令牌保持上次本人同意时间。普通聊天和机器人以应用身份读取自己所在群继续沿原来的路径。

Gateway 按已送达卡片的原用户、原任务和期限轮询。飞书可能在换令牌时附带本次申请以外的细分权限。宿主仅将本次申请且实际获准的交集登记为可用，业务操作取令牌前还会核对所需权限。刷新可收窄已准范围。密文只保存在原用户的授权记录。撤销先使本地记录立即失效，再向平台撤销该设备授权的令牌，并分别报告结果。

排查时先核对 `feishu_auth(action="status")` 里的 `provider_error`、`provider_http_status`、`validation_phase` 和 `validation_facts`。等待同意时平台可返回 `authorization_pending`。登录通知、成功发卡、本人同意、原任务获得凭据和成功查询各自有独立证据。群历史接口的 `230002` 表示飞书拒绝机器人在群外读取；历史可见性以相应会话设置和真实对照结果为准。

设备授权端点、轮询节流和撤销协议见[飞书官方 CLI 设备授权实现](https://github.com/larksuite/cli/blob/v1.0.98/internal/auth/device_flow.go)和[撤销实现](https://github.com/larksuite/cli/blob/v1.0.98/internal/auth/revoke.go)。

## 有界与一致性

沿原历史预算：每次最多五页、200 条、每页50、每条正文2000字符、七天窗及8秒协作式业务读取预算。原 OAuth 刷新走其独立既有请求合同。CLI 子请求超时取剩余业务预算；进程关闭与取消保持原实现。

游标绑定 owner/actor/app、动作、会话与过滤参数；它仅是定位信息。每页及返回前重新核原授权，撤权或身份变化时丢弃已缓冲正文。列表的 complete 仅表示本次供应商可见集合与指定类型已穷尽；has_more/coverage/next_cursor 保留部分结果。不据消息文案推断权限，不把空结果当作其他应用或全部历史不存在。

## 官方依据

- [获取消息列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)
- [官方 CLI 会话列表参考（v1.0.98）](https://github.com/larksuite/cli/blob/v1.0.98/skills/lark-im/references/lark-im-chat-list.md)
- [官方 Python SDK 会话列表参数](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/list_chat_request.py)

定向回归通过正式 ToolRegistry 与受控 CLI 检查隔离、撤权和分页。隔离 Gateway 上，测试用户通过设备授权卡同意后，宿主以本人身份读到单聊和群聊。测试端使本地到期时间提前后，飞书真实续期成功，原同意时间保留。修复后的恢复链使用同一个真实授权，在新的私有测试任务中由后台模型调用列表并只返回总数。该记录与第一次点击触发的原始唤醒分别保存。最后由测试任务调用本人撤销，飞书拒绝使用旧访问令牌和旧刷新令牌。

该次机器人所在测试群的应用身份历史请求被飞书拒绝（HTTP 400、230027）。没有独立的机器人入群时间可核对，入群前的历史可见性仍未验证。
