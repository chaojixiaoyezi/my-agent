# 宿主凭据与准确云资源授权（R20/R21）

状态：2026-10-13 隔离分支实现及本地严格验收完成，准备按授权合 main；未部署，真实飞书文档实发未作为验收来源。

云资源工具 `feishu_cloud` 使用原 `ChannelAdapterRegistry` 的 FeishuAdapter 和原租户 token 获取/缓存。应用凭据沿整机原配置、secret ref 留在宿主，不下发到用户工作目录、子代理参数、工具结果或脚本。其它 API 的私人账号密码沿 R04 `request_credentials` 加密引用与 `web_fetch` 宿主注入，当前支持 Basic/Bearer/自定义认证头；不声称已经实现任意登录表单、Cookie 会话或任意平台全套 SDK。

## 授权的唯一位置

每个 owner 原 `tool_policy.json` 的 `resource_grants` 块，复用原文件锁和 600 私有原子保存。没有块即没有云资源授权；块坏了失败关闭。格式：

```json
{
  "schema": "owner_resource_grants.v1",
  "items": [{
    "provider": "feishu",
    "resource_type": "sheet",
    "resource_token": "synthetic_sheet_token",
    "operations": ["sheet_read", "sheet_append"]
  }]
}
```

只有管理员主会话可沿原 `admin_controls` 操作：先 `action=list` 取得规范 `owner_id`；`resources` 查该用户资源；`resource_grant` 登记完整准确许可，替换同一资源的操作集；`resource_revoke` 撤销整项。沿原必须本人确认的策略执行，Full Access 不免此确认。普通用户和子代理不能给自己加许可。

群 owner 授权意味着该群助手可用这份资源，不等于个人 owner 也获准。允许资源与操作均不支持通配符，不接受 URL 当授权编号。每次真正请求前重读名单；管理员本人也不能因 Full Access 绕过准确资源门。已发出且供应商正在处理的请求不能靠撤销倒流。

## 工具动作与事实

| action | resource_type | 参数及供应商动作 |
| --- | --- | --- |
| sheet_read | sheet | `range`；GET 单个范围 |
| sheet_append | sheet | `range`、二维 `values`；POST values_append |
| sheet_update | sheet | `range`、二维 `values`；PUT values |
| document_read | docx | 可选 `block_id`、`page_token`；分页 GET children |
| document_append | docx | `children`、可选父 `block_id`；POST children，末尾追加 |
| wiki_resolve | wiki | GET 原 wiki 节点，返回公开 `resource_type/resource_id` |

文档分页响应的 `next_cursor` 对应下一次参数 `page_token`；原 token 命名不进入字符串日志，认证秘密仍按原规则打码。

wiki 定位权限不会自动授予背后的 sheet/docx 写权；解析后必须对准确对象另行登记操作。调用路径由宿主动作生成，不开放任意 URL、认证头或 app_secret 参数。主/子/孙仍受原工具快照与父级网络上界约束。

`feishu_cloud_tools_enabled` 是 capability 配置总闸，默认 true；空许可名单仍完全不能请求资源。Owner 网络关闭、父级本片网络关闭、功能关闭、缺许可均在读取凭据或发 HTTP 前拒绝。

飞书侧还需应用接口权限和文档协作者。底座许可与飞书许可分别负责；API 非零/HTTP 拒绝返回稳定可读错误，不将供应商错误正文写入模型。正确性以供应商回执和显式读回为准。写入超时、5xx 或不可验证结果记 unknown，不自造重试；原 ToolExecutor 操作账继续负责防重复及未知副作用门。认证 HTTP 不跟随跳转，成功 HTTP 后若响应无法投影，写入仍记 unknown，不能被参数错误覆盖成未执行。响应有大小界限，并在结果归档前清理宿主秘密及其编码回显。

接口核对参考：[官方表格概览](https://open.feishu.cn/document/server-docs/docs/sheets-v3/overview)、[官方 SDK 范围读写示例](https://github.com/larksuite/oapi-sdk-python-demo/blob/main/composite_api/sheets/copy_and_paste_by_range.py)、[官方文档块创建](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create)、[官方 wiki 定位](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)。实现继续用标准库，没有新增 SDK 依赖。

## 入站与外发隔离

Gateway 服务队列是单一整机控制面，不等于物理所在的 main 业务资料。当前默认 `workspace/runtime/services` 纳入原 H2 私有存储声明，文件与 Shell/PTY 在任何权限模式不能直接读取其跨用户正文。用户资料、会话、任务和产物仍按 owner 原目录分离；不另造按 owner 排队的第二套 Gateway。

新真实适配器入站在落盘前沿原 owner resolver 保存归属；HTTP 在原入队事务前冻结 `host_request_owner.v1` 与规范 `owner_id`。Body 自报的 owner 被丢弃，适配器记录与 Gateway 归属冲突失败关闭；已受理工作不因后来的管理员绑定变化改 owner。旧记录缺归属不说明由 main 执行，历史实际执行只能核对原 thread/runtime 绑定，不自动补写线上历史。

非 local owner 消息目标仍取其本人/本群，不能读取管理员身份作为回落。飞书供应商两种目标分别使用 `ou_ → open_id`、`oc_ → chat_id`；地址格式校验不等于目标授权。后台委派与监控的通知沿原冻结来源，group owner 回原群，私聊回原发起人；followup 与 isolated 定时投递都在首次准备冻结来源，并核对准确 origin、目标和当前绑定；错误目标保留原 outbox，不写 sent。旧线程缺 owner 时只用准确原 run owner 做投影；非空 owner 已改变则拒绝。审批及凭据卡始终私人发送，不随群通知改成群卡。

验收：合成真实 HTTP 和原 ToolExecutor 覆盖三类 owner 追加读回/更新、跨 owner/操作拒绝、撤销、wiki 不放大权限、文档块、网络/配置关门、重定向/未知写入及秘密回显。通道测试覆盖两人/群入队、冻结归属、管理员解绑、共享队列文件/Shell 拒绝及原群发送。真实应用协作者与文档读回仍待隔离飞书验收，不用合成服务冒充真实云文档。
