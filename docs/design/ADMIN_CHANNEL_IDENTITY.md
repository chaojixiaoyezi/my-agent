# IM 管理员身份与聊天内工具审批

状态：已实现并合入 main（2026-09-26），本地严格门禁通过；真实飞书验收待做。

## 背景

- 管理员原来只有本机主用户 local/main（本机 TUI）。飞书用户由 `request_worker._resolve_request_owner_identity`
  按渠道身份解析成 `feishu/user/<open_id>`，永远不是管理员。
- IM 请求不带 `client_capabilities.tool_approval`，需要确认的工具在 `stream_approval` 里直接得到 `unavailable` 被拒。
- 用户决定：管理员在飞书里也能以管理员身份工作。先在飞书私聊里证明身份，之后这个私聊就按管理员运行；
  飞书里的工具确认用管理员密码完成。
- 这是 [Gateway 安全重启](GATEWAY_SAFE_RESTART.md) 里“IM 里当管理员（待用户确认）”一项的落地方式：用户选了
  “密码绑定 + 密码批准”。

## 组成

| 部分 | 位置 | 说明 |
| --- | --- | --- |
| 管理员密码 | `agent/user_space/admin_password.py` | scrypt 派生值、失败节流、固定拒绝文案 |
| 身份绑定 | `agent/user_space/admin_channel_identity.py` | `(channel, user_id)` 精确绑定表 |
| owner 解析 | `gateway_parts/request_worker.py` | `private_channel_identity`、`admin_channel_identity_enabled`、`admin_channel_identity_for_request` |
| 控制命令 | `gateway_parts/admin_control_service.py` | `/admin`、`/approve`、`/deny` 的唯一执行入口 |
| 审批开关 | `gateway_parts/request_execution.py` | `_gateway_request_interactive_approvals`：服务端核实后开启交互审批 |
| 审批提示 | `gateway_parts/http_handlers.py`、`adapter/manager.py` | `/progress` 投影 `permission_requested`，适配器渲染提示 |
| 不落原文 | `command_catalog.py`（`sensitive_input`）、`control_commands.py`（`persisted_control_command_text`） | 适配器、TUI、回执共用同一声明 |
| 本机 CLI | `cli/admin_identity_commands.py` | `admin-password set/status/clear`、`admin-identities list/remove` |

## 数据文件

全部位于 `<my_agent_home>/config/`，文件 0600、目录 0700，经 `common/nofollow_fs` 原子写入（不跟随符号链接）。

| 文件 | schema | 内容 |
| --- | --- | --- |
| `admin-password.json` | `admin_password.v1` | `algorithm=scrypt`、`n/r/p`、`salt`（base64）、`hash`（base64）、`updated_at` |
| `admin-password-attempts.json` | `admin_password_attempts.v1` | 按 `渠道:用户ID` 记录窗口内失败时间和 `locked_until`；窗口外且未锁定的条目写回时丢弃 |
| `admin-channel-identities.json` | `admin_channel_identities.v1` | `[{channel, user_id, bound_at}]`；channel 小写，user_id 保持原样 |

读取规则：密码文件缺失或损坏时，校验一律不通过，而且照样做一次同成本的派生，不给出计时差别。
绑定文件损坏时查询按“未绑定”处理，写入拒绝覆盖。失败记录损坏时，本次校验按拒绝处理，不当作“没有失败”。

## 流程

### 设置密码（本机）

`my-agent admin-password set` 只在配置的 base owner 为 local/main 时运行。密码经 `getpass` 输入两次，不回显。
要求至少 8 个字符，首尾不能有空白，不能含控制字符（否则 IM 里的 `/admin <密码>` 无法无歧义地还原）。
重新设置会换新盐；清除密码不会删除已有绑定。

### `/admin <密码>`（IM 一对一私聊）

1. 适配器识别这是 `sensitive_input` 命令，不写持久入站队列，在一次性线程里直接 POST `/ask`。
2. Gateway `/ask` 在模型、会话记录和请求队列之前把它解析为控制命令。`execute_gateway_control_operation`
   在写回执、计算摘要之前，就把正文换成 `/admin ******`。
3. `admin_control_service` 依次核对：不是本机基础通道（local/cli/chat/gateway-cli/http）；开关开启且 base owner
   为 local/main；adapter 显式给出 `channel_chat_type` 为 p2p/private。然后按 `渠道:用户ID` 执行节流校验，
   通过后写入绑定。
4. `/admin status` 查看当前绑定，`/admin logout` 删除自己的绑定。

### owner 解析

`_resolve_request_owner_identity` 在逐用户路由之前先调用 `admin_channel_identity_for_request`。命中已绑定的私聊时，
返回 base owner（local/main）。控制作用域的 `resolve_gateway_scope_owner`、`bind_gateway_control_scope_owner`
经同一函数，所以结果一致。群聊、缺私聊类型、未绑定、开关关闭或 base owner 不是 local/main 时，行为与原来相同。
绑定后，这个私聊使用 local/main 的会话空间（按飞书会话 ID 另开线程）、记忆、模型和审批模式。解除绑定后回到自己的 owner。

### 聊天内审批

- `_handle_gateway_request` 创建 chunk writer 时，`interactive_approvals` 取以下任一：客户端显式声明 `tool_approval`
  （TUI）；或服务端核实该请求来自已绑定的管理员私聊，且执行 owner 正是本机管理员（`is_permission_admin`）。
  IM 客户端不能自己声明这项能力。
- 需要确认的工具沿用原 `StreamApproval`：先向 chunk 流写 `permission_requested`，再在 `permission_bridge` 上等待
  精确决定文件，与 TUI 完全相同。自主模式切换和 `/stop` 取消照常生效；等待没有超时，与 TUI 一致。
- `/progress` 把 `permission_requested` 投影成 `{kind, tool, summary}`。summary 取审批请求里已脱敏的描述，
  外部渠道收敛宿主路径，并限制在 200 字以内。这个事件不受 verbose 档位限制。
  适配器渲染为“代理请求：<摘要>。回复 /approve <管理员密码> 允许本次，/deny 拒绝。”。它只是过程消息，不是最终回复。

### `/approve <密码>` 与 `/deny`

1. 作用域检查与 `/admin` 相同。`/approve` 还要求当前私聊已绑定，未绑定时不校验密码。
2. `pending_tool_approvals` 只在本会话仍开放的精确前台回合里找审批，逐项核对以下条件：
   - 控制作用域 owner 与请求 owner 相同；
   - 事件里的请求号等于回合号；
   - 事件发布于本次认领（`lease_started_at`）之后，Gateway 重启前旧执行留下的请求不算；
   - 该审批还没有决定文件。
3. 没有待决审批时返回 `APPROVAL_NOT_PENDING`，多于一个时返回 `APPROVAL_AMBIGUOUS`，系统不猜测目标。
4. `/approve` 通过节流校验后写 `approved`（仅本次）；`/deny` 不需要密码，写 `denied`。决定经
   `write_gateway_permission_decision` 写入，原等待方再逐字段核对 binding 后继续。

### 未绑定时的指引（2026-09-26）

真实使用中管理员设好密码后直接在飞书发普通消息，因私聊尚未绑定仍按飞书普通用户运行，得到 `MODEL_NOT_CONFIGURED`，
提示只提 `/model`，没有指出应先 `/admin`。现在在同时满足以下结构化事实时，给这类报错和 `/model` 的“没有可选模型”回复
追加一句 `/admin <管理员密码>` 指引（并提醒撤回含密码消息）：请求来自 IM 私聊（`private_channel_identity`）、
`admin_channel_identity_enabled` 生效、本机已设管理员密码、这个私聊尚未绑定。判定在
`request_worker.admin_binding_hint_for_request`（执行请求的可能是按用户隔离的 agent，owner 池把它的 owner 字段改成了该用户，
所以判定不依赖“基础 owner 是 local/main”，只看开关、全局数据根里的密码文件与绑定表；密码只能由 local/main 的 CLI 设置），
失败回复经 `request_execution._gateway_user_error`，`/model` 经
`model_profile_service._admin_hint`。`/admin` 本就在公开命令帮助里，指引不增加暴露面；群聊、已绑定、未设密码时都不提示。

## 密码明文可能去向与处理

| 位置 | 处理 |
| --- | --- |
| Gateway 控制回执 `command_text` 与 `input_digest` | 落盘前换成 `/admin ******`、`/approve ******` |
| 会话记录、模型请求、请求队列 | 控制命令在这些之前被拦截，不进入 |
| 日志、审计 | 不记录命令正文；适配器只记异常类型 |
| 适配器持久入站、回复 watcher、占位句柄 | 敏感命令不进入；Gateway 不可达时只回复“服务暂时不可用”，不重试 |
| TUI 输入历史、TUI 控制 outbox | `_remember_input` 跳过；终端本地拒绝，不发送、不保存 |
| 飞书服务端的原消息 | 无法控制；成功回复都提示撤回，群聊里误发会提示撤回并更换密码 |

## 配置与错误码

- `admin_channel_identity_enabled`（默认 `true`）：没有设置密码时不会产生任何绑定。设为 `false` 时 `/admin`
  拒绝，已有绑定不生效，文件保留。
- 错误码：`ADMIN_PASSWORD_REJECTED`（密码错误与锁定共用，不暴露差别）、`ADMIN_IDENTITY_SCOPE_INVALID`、
  `ADMIN_IDENTITY_NOT_BOUND`、`ADMIN_IDENTITY_STORE_UNAVAILABLE`、`APPROVAL_NOT_PENDING`、`APPROVAL_AMBIGUOUS`。

## 已知边界

- 飞书会保留含密码的原消息，只能靠用户撤回。适配器与 Gateway 之间经本机回环 HTTP 明文传一次。
- 节流按渠道身份计数，挡不住分散在很多 IM 身份上的猜测。本机回环调用方本来就可信（不带身份头即为本机管理员）。
- 只接前台回合的审批。后台续跑、Goal 自动续跑和子代理的审批走耐久审批账本，需要消费者续租，IM 仍无人接收，
  照旧按无法确认拒绝。
- 审批提示按过程消息至多投递一次。发送失败时用户看不到提示，可以直接发 `/deny`、`/approve` 或 `/stop`。
- QQ 适配器不带 `chat_type`，不能使用 `/admin`。可选的 ASGI 规模化入站（`asgi_entry`/`ingress_queue`）会先把
  飞书事件写进队列库，不在本切片的不落盘范围内。
- 飞书里另有“私聊会话锁”的密码卡（`session_lock`），与管理员密码无关，两者互不影响。
- 绑定或解除绑定前已经在运行的回合仍属原 owner，之后的控制（如 `/stop`）按新 owner 解析，找不到那个回合。
  需要时先 `/stop` 再切换身份。

## 测试

`test_admin_identity_store.py`、`test_admin_identity_gateway.py`、`test_admin_identity_clients.py`，说明见 TESTS.md
“IM 管理员身份与聊天内审批”一节。
