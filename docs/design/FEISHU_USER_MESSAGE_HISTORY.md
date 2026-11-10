# 飞书本人会话与历史读取

状态：源码切片；未代表真实应用授权或上线验收。公开工具仍为 `channel_messages`，不新增凭据账、订阅、后台轮询或跨 owner 搜索。

- 默认 `selector=current_group` 保持当前群规范归属、机器人/成员/租户证明和租户身份；模型不能在此模式指定别的群。
- `selector=user` 仅私人 owner，沿既有 `feishu_oauth.user_access_token` 的 owner/actor、宿主 run scope、密文记录、刷新与撤销 CAS。群 owner 不能借私人用户授权读取私聊。受控 CLI 在每次业务请求前验证 token 对应用户。
- `action=list_chats` 对官方 `/im/v1/chats` 显式发送 `types=p2p,group`；会话来自该用户、该应用实际可见范围，不从应用权限推导全量私人会话。`action=list` 使用准确 chat_id 及原七天时间窗。
- 用户身份需要 `im:chat:read`（会话列表），消息读取需要 `im:message:readonly`、`im:message.p2p_msg:get_as_user`、`im:message.group_msg:get_as_user`。应用可用范围、群内机器人及飞书外部群等限制仍由官方 API 执行；OAuth 不能替代管理员申请、审核和发布应用权限。
- `action=status` 不联网、不读取 token，返回结构化 required_scopes、permission_settings_url、official_api_docs 和原 `feishu_auth authorize` 入口。permission_status=not_verified 不声称权限已获批。用户读失败同样携带上述事实，绝不要求向模型粘贴 token。
- 默认开放历史能力入口，仍可显式关闭；启用入口不会自动调用网络或创建新授权。用户 CLI 后端未配置、授权缺失/撤销或 owner/actor 不符均明确拒绝，不降级为机器人身份。

## 有界与一致性

沿原历史预算：每次最多五页、200 条、每页50、每条正文2000字符、七天窗及8秒协作式业务读取预算。原 OAuth 刷新走其独立既有请求合同。CLI 子请求超时取剩余业务预算；进程关闭与取消保持原实现。

游标绑定 owner/actor/app、动作、会话与过滤参数；它仅是定位信息。每页及返回前重新核原授权，撤权或身份变化时丢弃已缓冲正文。列表的 complete 仅表示本次供应商可见集合与指定类型已穷尽；has_more/coverage/next_cursor 保留部分结果。不据消息文案推断权限，不把空结果当作其他应用或全部历史不存在。

## 官方依据

- [获取消息列表](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/list)
- [官方 CLI 会话列表参考（v1.0.98）](https://github.com/larksuite/cli/blob/v1.0.98/skills/lark-im/references/lark-im-chat-list.md)
- [官方 Python SDK 会话列表参数](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/list_chat_request.py)

验证采用原 OAuth 密文/刷新链、正式 ToolRegistry 与私有 CLI 子进程连接 loopback 假供应商；未读取真实个人历史、未执行真实 OAuth 或付费模型请求。共享导航/发行证据由根集成维护。
