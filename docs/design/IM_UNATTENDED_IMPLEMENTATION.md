# 无人值守 IM、私人输入与受控飞书 CLI

状态：2026-10-14，P01–P12 代码集成中，综合 gate、发行及本机部署尚未登记完成。此前只读评估中的缺陷与事实保留为历史，当前行为以本文件和正式工具合同为准。

## 正常执行与止损

共享 `home/config/capability_config.yaml` 是新增部署边界的唯一配置源。`owner_default_permission_mode: auto` 仅在 owner 未显式选择时继承，显式 ask/auto/full-access 保持；非法策略拒绝，auto 不扩大路径、网络、角色、资源或插件范围。仅可信本机 local/main 的管理和重启操作免可选确认，普通/群/子代理不能借发起人身份得到宿主权限。

本人取消子代理、托管进程及会话停止沿原控制链免可选审批；跨 owner、非父子或错误执行身份仍拒绝。群与无人值守剩余人工确认在发布 pending 前终止为 `APPROVAL_NONINTERACTIVE`。交互确认使用共享 `tool_approval_timeout_seconds: 1800` 的绝对截止；运输失败、无人可批和超时分别形成不可自动重询的原操作结果，不用新 call/attempt 重开期限。需要新授权时由明确用户控制重新发起任务，不能用模型正文清拒绝账。

卡片回调按 kind/reference/action 去重；未解锁或无效点击不消耗合法决定。原人、原调用、期限和取消状态仍在各处理器重核，重复已决定的回调保持幂等。

## 私人输入、凭据和恢复

`request_credentials` 可声明必要字段，并查询、更新、撤销自己的准确引用。群中发起后给原发起人私聊卡，群不显示密码表单。值加密保存在原私人交互目录，只返回不透明 cref；不下发到脚本环境、普通文件、模型或子代理上下文。

原 SessionTask 引用输入依赖，工具调用完整归档后退出工作片，结清 claim 并释放当前聊天执行权。输入回调与原输入截止使用原 runtime Wake；核验准确人、任务、子树、取消及现行权限后恢复一次。待输入不是常驻 worker；每根最多32个原子输入槽，重复不新增槽、终态立即释放，超额无新私卡/秘密残留。不新增每员工等待线程或历史扫描。停止原任务同时取消待输入，不误撤销已单独授权的可用凭据。

宿主 HTTP 引用使用支持原 Basic/Bearer/header 及声明式登录 JSON、Cookie/CSRF、一次401刷新、受控签名。认证、跳转和回显仍走原网络及秘密净化合同；未知写入不自动重放。不能把任意脚本加密计算当作秘密不可见通道。

## 渐进发现与飞书使用

`list_tools` 返回本人真实审批模式、来源、路径/网络边界及可用/折叠/未配置原因；`tool_search`按需加载具体 schema，沿当前线程跨 Compact 保持。普通主/子/后台工具集合沿父级、角色和 owner/任务交集；leaf 不获得宿主管理或派工能力。`/help` 和 `/status`给同源导航与权限事实，查询不会修改权限。

`feishu_cloud` 仍是云文档操作入口，消息和文件继续沿原 Delivery。新增后端采用固定官方 lark-cli v1.0.98 提交的公开 Go 扩展，仅接宿主冻结的方法/官方路径/身份和私有 stdin；不开放任意 CLI 参数、登录缓存、宿主密钥导出或程序下载。本人模式先用同一UAT做一次固定userinfo只读验证，并核对宿主actor；业务仅发一次，阻止自动重试/重定向，保留 HTTP 和供应商 code；CLI失败不会切换另一后端重放写入。

默认 `feishu_cli_backend_enabled: true`，完整安装包内含对应平台程序、来源与许可证。开发源码没有发行二进制时如实显示未安装。部署者显式关闭此键才使用原宿主 HTTP 后端，权限合同不变。

群使用应用身份，只能操作当前群已共享或准确管理员授权的资源。普通私人用户也可使用应用身份处理已共享文档；个人资源选择 `identity=user`，先用渐进工具 `feishu_auth` 建立本人 OAuth。元数据、资源权限、表格保护及正式业务操作使用同一本人身份，不能先借应用或管理员查资源。本人token只在宿主密文记录中，刷新仍核验原open_id与范围；群不能共用管理员或某成员个人登录。

## 部署配置与验收边界

新增六项部署参数均只由部署者修改，模型不能写 owner 局部覆盖：

```yaml
owner_default_permission_mode: auto
tool_approval_timeout_seconds: 1800
feishu_cli_backend_enabled: true
feishu_user_oauth_enabled: true
feishu_oauth_redirect_uri: ""
feishu_oauth_scopes:
  - offline_access
```

本人OAuth须部署者配置准确 HTTPS `/feishu/oauth/callback` 并在飞书开放平台登记，发布对应用户权限及 scope。空回调明确未配置，不能伪称已可本人登录；应用身份的现有群文档操作不依赖个人回调。浏览器公开回调先验原交互MAC，再验owner/actor、期限及任务；授权代码和token不进入日志或模型。

开发验收沿合同→真工具/假模型→隔离后台/IM回调→少量真实环境；Go包装测试使用合成运输执行真实官方命令。合成接口证明本仓库主链，不能替代真实租户权限、首次私人发卡、移动端表单或OAuth后台登记，也不证明几十万用户吞吐。更广安全专项仍暂停，本文只记录本期批准的正常执行和权限接缝修复。

## 权限帮助与本人会话读取（2026-11-09，源码集成中）

`feishu_auth action=permissions/status` 现在返回当前部署应用的 `permission_settings_url`、
`event_settings_url`、各能力所需 scopes/events、本人 OAuth 状态和准确配置阻塞。
没有应用或 OAuth 回调时仍可发现只读帮助；申请入口始终标记 `permission_status=not_verified`，
不能把配置、链接或模型声明当成飞书已授权限。实际 authorize/revoke 继续要求原私人 owner、
应用凭据、部署范围、原任务/网络及本人 OAuth 合同；群只能查询配置帮助，不能共享个人 OAuth。

后台申请、租户审核/发布和本人 OAuth 为不同步骤。首次用户授权仍需本人点击卡片，之后沿原
密文授权刷新。新模板默认申请本人会话发现与单聊/群历史读取范围；部署定制 scope 列表继续
作为宿主上界，不因模型调用扩权。旧模板迁移及真实内网权限验收另记，不把源码测试作为
应用后台已经开通或内网问题已修复的证据。

申请清单按实际接口区分：会话枚举的 `im:chat:read` 不能代替精确群信息读取的
`im:chat:readonly`。接收所有用户群消息使用 `im:message.group_msg`，包含其他机器人则使用
`im:message.group_msg.include_bot:read`；精确消息读取和群成员核验仍各守原平台权限。
接收事件需在同应用事件配置订阅 `im.message.receive_v1`，不能只申请接口权限而漏掉事件配置。

## 当前云工具覆盖

| 能力 | 当前公开动作或边界 |
|---|---|
| 已有电子表格 | `sheet_meta`、`sheet_read`、`sheet_append`、`sheet_update`、`sheet_add`、`sheet_rename` |
| 已有文档 | `document_read`、`document_append` |
| Wiki准确节点定位 | `wiki_resolve`，定位结果不自行授予底层对象权限 |
| 样式、行列及文档块修改 | 尚无公开动作；后续可沿同资源编辑权限扩展，但仍需对应参数、验证和未知写入恢复合同 |
| 新建文档或整个工作簿 | 尚未实现；须先定义目标文件夹/根目录创建权、对象归属与原操作恢复，已有文档 edit 不能代表容器 create |
| 多维表 | 尚未实现公开动作；表、字段、记录及应用创建分别需要准确资源与归属合同 |
| 导入、导出 | 尚未实现；导入涉及上传和创建，导出涉及异步任务、下载与产物登记 |

功能缺失由工具 Schema 和上述覆盖事实表达，不能通过向用户索取应用密钥、把共享凭据写到
任务脚本或开放任意 HTTP 来补。官方范围参见[云文档概述](https://open.feishu.cn/document/server-docs/docs/docs-overview)。

## 建议下一步

先完成集成 gate 与包内主链验收，再按同一提交部署本机和发布Linux更新包。真实个人OAuth验收需要部署者登记回调与专用测试资源；可独立于群应用身份验收并行，不改变生产监控或替员工授权。
