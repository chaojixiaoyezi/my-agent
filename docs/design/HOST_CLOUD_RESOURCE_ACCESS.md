# 宿主凭据与云资源真实权限（R20/R21、C01–C04）

状态：2026-10-13 修复切片采用实时本人/群协作者证明与原准确管理员许可，定向合成验收 87 项通过，等待主线程联合发布 gate；未部署，真实飞书文档实发未作为验收来源。

云资源工具 `feishu_cloud` 使用原 `ChannelAdapterRegistry` 的 FeishuAdapter 和原租户 token 获取/缓存。应用凭据沿整机原配置、secret ref 留在宿主，不下发到用户工作目录、子代理参数、工具结果或脚本。其它 API 的私人账号密码沿 R04 `request_credentials` 加密引用与 `web_fetch` 宿主注入，当前支持 Basic/Bearer/自定义认证头；不声称已经实现任意登录表单、Cookie 会话或任意平台全套 SDK。

## 授权事实源与自助使用

共享应用的租户 token 只能说明应用有权限，不能证明当前用户或群也有权限。`feishu_cloud_collaborator_authorization_enabled` 位于 capability 配置，默认 true；开启时，已有飞书分享权限可以成为宿主工具的准确事实源，无需为每份文档再找管理员手工登记。

主、子、后台任务仍使用当前真实 owner：私聊取本人 `openid`，群取本群 `openchat`。provider owner 与当前可解析身份必须完全一致；本地管理员只使用已有的唯一飞书身份绑定，没有绑定或多条绑定都明确拒绝。模型不能提供或覆盖授权主体。当前群有权必须证明当前群是协作者，群内某人有权不代表群有权。

每次调用现查 `/drive/v1/permissions/{token}/members`，仅匹配准确主体编号、类型和 `view/edit/full_access`。读操作接受这三档；写操作仅接受 `edit/full_access`。普通文档所有者可能未列在协作者中，此时仅私人 owner 可以通过官方元数据中同一资源的准确 `owner_id` 证明本人所有权。群和 wiki 不走此私人所有者路径。应用不能查看协作者、事实缺失、未知权限或撤销后都拒绝业务请求，不回落管理员、不缓存权限、不把模型文案当许可。

知识库输入使用 `resource_type=wiki`：宿主先调用官方 `get_node` 定位实际 sheet/docx，再核对原知识库页面的 `container` 与 `single_page` 明确协作者许可。页面许可允许宿主完成同一页面对应的业务动作，无需重复手工授权两份 token。不能只凭底层对象的私人所有者或编辑许可绕过知识库页面授权；未授权时定位数据也不返回模型。

隐藏协作者、组织或父目录继承如果未在官方列表明确返回当前本人/群，本期不自行推断其有效权限。用户可以明确分享给本人/当前群，或申请原准确管理员许可。当前未实现用户 OAuth，不能把应用 OAuth 或租户 token 称作用户授权。

## 保留原准确管理员许可

每个 owner 原 `tool_policy.json` 的 `resource_grants` 块仍是人工授权的唯一位置，复用原文件锁和 600 私有原子保存。没有块即没有人工许可；块坏了在网络和凭据获取前失败关闭，不能靠自助权限绕过损坏策略。格式：

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

只有管理员主会话可沿原 `admin_controls` 操作：先 `action=list` 取得规范 `owner_id`；`resources` 查该用户资源；`resource_grant` 登记完整准确许可，替换同一资源的操作集；`resource_revoke` 撤销整项。只读 `list/resources` 不再强制审批，写入仍沿原必须本人确认的策略执行，Full Access 不免写入确认。普通用户和子代理不能给自己加许可。

群 owner 授权意味着该群助手可用这份资源，不等于个人 owner 也获准。允许资源与操作均不支持通配符，不接受 URL 当授权编号。每次真正请求前重读名单；管理员本人也不能因 Full Access 绕过准确许可或真实权限证明。明确的资源/操作人工许可保持原权威，不要求另有协作者事实；撤销人工许可后仍可能因本人真实飞书权限而获准，两种来源会在 `resource_authorization.source` 明确区分。已发出且供应商正在处理的请求不能靠撤销倒流。

## 表格保护范围

云文档级编辑许可不等于任意表格范围编辑许可。自助模式的表格写入会先读取 `metainfo?extFields=protectedRange&user_id_type=open_id`，必须获得准确目标工作表及明确保护数组；缺字段不当成无保护。存在保护时再按官方每批最多 5 个编号读取 `protected_range_batch_get?memberType=openId`，核验每项归属与完整编辑者事实。

`sheet_update` 的正规闭区间 A1 范围按官方从 1 开始、包含两端的 `ROWS/COLUMNS` 保护索引计算相交；二维数据超过声明范围的部分也纳入核验。不相交的更新可以正常执行，不会把整个工作表无故封锁。开放范围或无法解析的格式存在保护时返回可读的权限未知错误。

`sheet_append` 的真实目的行由飞书计算，不能用请求起始范围断言不相交；本期需本人被明确列为目标工作表所有保护项的 `openId` 编辑者。群不能借群成员个人或应用的保护编辑权；有相交保护的群更新、存在保护的群追加，需要调整共享/保护范围或受控的准确管理员操作许可。`lockInfo` 文案从不参与判断。

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

原人工 `wiki_resolve` 许可仍只授权定位，不自动扩展成 sheet/docx 写权；只有实际对象的准确操作许可或原页面实时许可才允许业务操作。调用路径由宿主动作生成，不开放任意 URL、认证头或 app_secret 参数。主/子/孙仍受原工具快照与父级网络上界约束。

`feishu_cloud_tools_enabled` 是 capability 配置总闸，默认 true；关闭自助权限开关时保留原准确人工许可模式。Owner 网络关闭、父级本片网络关闭、功能关闭和损坏策略均在读取凭据或发 HTTP 前拒绝。自助启用且人工许可缺失时，可先用宿主请求核验权限；未证明权限不发业务读写请求。

飞书侧还需应用接口权限、文档协作者及查看协作者的权限。错误区分应用 scope、认证失效、资源拒绝、资源不存在、频率限制、额度耗尽、服务故障与拒绝重定向；公开数值 `http_status/provider_code`、稳定 `reason` 和副作用事实，绝不复制供应商正文。写入超时、429/5xx、陌生 API 拒绝或不可验证结果保留 unknown，不自造重试；原 ToolExecutor 操作账继续负责防重复及未知副作用门。认证 HTTP 不跟随跳转，成功 HTTP 后若响应无法投影，写入仍记 unknown，不能被参数错误覆盖成未执行。响应有大小界限，并在结果归档前清理宿主秘密及其编码回显。

接口核对参考：[官方表格概览](https://open.feishu.cn/document/server-docs/docs/sheets-v3/overview)、[官方 SDK 范围读写示例](https://github.com/larksuite/oapi-sdk-python-demo/blob/main/composite_api/sheets/copy_and_paste_by_range.py)、[官方文档块创建](https://open.feishu.cn/document/server-docs/docs/docs/docx-v1/document-block/create)、[官方 wiki 定位](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space-node/get_node)。实现继续用标准库，没有新增 SDK 依赖。

授权接口核对：[获取协作者](https://open.feishu.cn/document/server-docs/docs/permission/permission-member/list)、[表格元数据及保护清单](https://open.feishu.cn/document/server-docs/historic-version/docs/sheets/obtain-spreadsheet-metadata)、[获取保护范围详情](https://open.feishu.cn/document/server-docs/docs/sheets-v3/protect-range/retrieve-protection-scopes)、[官方通用错误码](https://open.feishu.cn/document/server-docs/api-call-guide/generic-error-code)。动态页面的正文由飞书公开 `document_portal/v1/document/get_detail` 只读接口核对，没有用真实租户 token 调用权限或文档接口。

## 入站与外发隔离

Gateway 服务队列是单一整机控制面，不等于物理所在的 main 业务资料。当前默认 `workspace/runtime/services` 纳入原 H2 私有存储声明，文件与 Shell/PTY 在任何权限模式不能直接读取其跨用户正文。用户资料、会话、任务和产物仍按 owner 原目录分离；不另造按 owner 排队的第二套 Gateway。

新真实适配器入站在落盘前沿原 owner resolver 保存归属；HTTP 在原入队事务前冻结 `host_request_owner.v1` 与规范 `owner_id`。Body 自报的 owner 被丢弃，适配器记录与 Gateway 归属冲突失败关闭；已受理工作不因后来的管理员绑定变化改 owner。旧记录缺归属不说明由 main 执行，历史实际执行只能核对原 thread/runtime 绑定，不自动补写线上历史。

非 local owner 消息目标仍取其本人/本群，不能读取管理员身份作为回落。飞书供应商两种目标分别使用 `ou_ → open_id`、`oc_ → chat_id`；地址格式校验不等于目标授权。后台委派与监控的通知沿原冻结来源，group owner 回原群，私聊回原发起人；followup 与 isolated 定时投递都在首次准备冻结来源，并核对准确 origin、目标和当前绑定；错误目标保留原 outbox，不写 sent。旧线程缺 owner 时只用准确原 run owner 做投影；非空 owner 已改变则拒绝。审批及凭据卡始终私人发送，不随群通知改成群卡。

验收：合成真实 HTTP 和原 ToolExecutor 覆盖三类 owner 准确人工许可、自助私人/群追加读回、本人所有者、只读分享、实时撤销、群不能借私人/应用权限、wiki 两种页面许可及未授权定位不泄漏、表格保护范围闭区间/编辑者/批次/损坏事实拒绝、文档块、网络/配置关门、管理员查询/写入审批、重定向/未知写入及秘密回显。通道原测试仍负责两人/群入队和投递隔离。真实应用协作者、零保护元数据返回形状与文档读回仍待隔离飞书验收，不用合成服务冒充真实云文档。
