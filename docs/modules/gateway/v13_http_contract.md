# v1.3 Gateway HTTP / 公开流合同

本文约定 W2 的增量字段；原有鉴权、owner/thread 隔离及脱敏边界不变。客户端不得用中文消息、模型名称或正文判断机器状态。`/result` 是终态和最终正文的权威，进度不是结果替代品。

## 可机读合同摘要

以下 JSON 供合同测试读取。`writer_statuses` 是当前请求结果写者的字面赋值集合；不能把 Gateway 服务进程的 `stopped` 或实验记录的 `completed` 混入请求写者证据。五终态公共合同保留 `stopped`，但本基线没有直接产生它的请求结果写者；它是客户端必须接受的保留终态，不代表本轮已验证真实停止请求会产出该值。

```json
{
  "result": {
    "terminal_statuses": ["done", "failed", "cancelled", "stopped", "interrupted"],
    "writer_statuses": ["done", "failed", "cancelled", "interrupted"],
    "writer_nonterminal_statuses": ["pending"],
    "reserved_terminal_statuses": ["stopped"],
    "nonterminal_statuses": ["queued", "processing"],
    "response": "string",
    "channel_delivery": {"content": "string"}
  },
  "history": {
    "active_request_id": "string",
    "in_flight_status": ["idle", "pending", "processing", "stopping"],
    "turn_phase": "string",
    "stream_cursor": "integer",
    "progress_path": "string",
    "queued_request_ids": "array[string]"
  }
}
```

## `GET /status`：可信等级与模型摘要

`/status` 保持 public 路由，不得为新增模型字段调用会直接发送 403 的 `require_trusted_source`。有鉴权中间件时，由原 `check_trusted(headers, peer_ip)` 的布尔结果判定可信；没有中间件的原回环单机场景按可信处理。不能把普通身份认证、客户端自报 owner 或请求正文当作模型名称公开许可。

始终返回：

| 字段 | 类型 | 语义 |
|---|---|---|
| `model_configured` | bool | 默认 profile 可解析、有 `model_name` 且原模型公开投影的 `available` 为真；不是网络连通性或模型推理探测 |
| `default_model` | string | 可信来源可见默认 profile 的 `model_name`；非可信来源固定为 `""` |
| `model_summary_error` | string，可选 | 配置来源不可用为 `MODEL_SUMMARY_SOURCE_UNAVAILABLE`，共享目录不可读为 `MODEL_SUMMARY_CATALOG_UNAVAILABLE`，其它读取/解析失败为 `MODEL_SUMMARY_READ_FAILED` |

读取失败时 `model_configured=false`，名称清空，附结构化错误码；不因摘要读取失败返回 500，不输出原始异常、路径、密钥或服务商连接。空目录表示未配置，不能因部署配置中存在名称而误报可用。可用性复用 `public_model_profiles.available`（经原模型选择投影），包含用途、凭据及认证规则；不自行实现第二套“有 API key 就可用”的逻辑。

只读配置目录与监听器既有依赖，不初始化后端、不构造 Agent、不探针、不迁移落盘。单进程使用原 `server.agent` 配置资源；coordinator 的 `agent=None` 时使用原 `control_plane.config/home_root` 和规范 owner 解析，不能为状态查询物化业务宿主。摘要描述 Gateway 默认配置，不是任意浏览器用户的私有会话选择。

## `POST /client/models`：真实 profiles schema

操作请求 `{"operation":"list"}` 可以不带 `conversation_id`；原 HTTP 会话解析将其归入默认显示会话。显式提供 `conversation_id` 时仍走原 owner/thread 解析。两种情况都不应仅因缺少活跃聊天而返回 `MODEL_NOT_CONFIGURED`。模型选择和配置修改继续使用既有权限与操作合同。

返回 `profiles` 是 **`list[dict]`，不是按名称索引的字典**。元素的真实键包括：

| 键 | 类型/语义 |
|---|---|
| `id` | string，模型档案引用；不是 `name` |
| `model_name` | string，模型名称；不是 `model` |
| `api_base` | string，公开配置地址 |
| `provider_name` | string，服务商展示名 |
| `has_key` | bool，仅表示有无密钥，不返回密钥 |
| `auth_mode` | string，原认证模式 |
| `available` | bool，当前聊天用途是否可用 |
| `available_for` | list[string]，可用用途 |

还可能带原档案能力、上下文窗口、输入模态等字段，不应丢弃未知增量字段。`operation=list` 额外含 `model_execution: object`。`selected`、`default_selected` 与 `thread_id` 含义沿原选择投影；默认配置与已有线程选择不能混淆。

## `GET /result/{request_id}`：终态与正文

客户端终态枚举为 **`done | failed | cancelled | stopped | interrupted`**。完成态是 `done`，不是 `completed`。当前 `/result` 的待处理请求返回 `queued`，执行中返回 `processing`（HTTP 202）；文档或界面所说的 `pending` 是待处理概念、history 的状态或队列统计，不应误写成该结果路由当前输出的字面状态。

当前请求写者定位：`request_execution.py`（成功 `done`、取消 `cancelled`、可恢复打断 `interrupted` 与失败），`request_errors.py`、`recovery.py`（失败路径；恢复模块另有 pending 队列写入，不是终态）。适配器 outbox 的 timeout/unknown、实验记录的 completed 和服务进程的 stopped 不属于此请求结果写者集合。canonical writer `recovery._terminal_gateway_request_payload` 保留 `terminal_response.status`，缺值才按 `ok` 派生 `done/failed`。`stopped` 的保留与已实现写者差异见可机读摘要。

最终 Markdown 位于**顶层 `response`**，交付正文同时位于 **`channel_delivery.content`**；不是 `result.response`，也不是 `result` 字段。成功返回示意：

- `status: "done"`
- `response: "最终公开正文"`
- `channel_delivery.content: "最终公开正文"`

正文服从原通道交付投影。已提供 `channel_delivery` 时，其空 `content` 也是权威结果，不能回退展示内部候选正文。canonical 归档是结果权威；仅有 response 文件或 done/failed 展示投影不能把请求提升为终态。非成功态可含空正文，客户端应同时读取结构化状态与错误码。

## `POST /ask`：本回合 verbose

请求可带 `verbose_level: "off" | "on" | "full" | "stream"`。非法值在任何准备工作之前返回 HTTP 400 与 `error_code="INVALID_VERBOSE_LEVEL"`。不传时沿用原会话档位；传入值经排队 payload 保留，覆盖**仅本回合**的 writer 档位，不写回线程设置。已有 `/control /verbose full` 路径仍要求唯一 `metadata.message_id`，并核对 `control_state=completed`；这里的 completed 是控制回执，不是 `/result` 成功态。

`full` 档位可追加完整脱敏输入 `input`，沿原 `detail` 的参数键投影；单字段上限默认为 16384 字符，超限标记 `input_truncated=true`。原 `detail` 继续不超过 100 字符；`on` 档位没有完整 `input`。非空 `call_id` 在同次调用各 phase 保持相同，空时省略键，不发送空字符串。

## `GET /progress/{request_id}`：游标与 opt-in kinds

请求：`GET /progress/{request_id}?since=N&include=assistant_text_delta,workflow_step_progress`。返回 `request_id`、`events: list`、`next: int`。`next` 是源记录游标，不是已展示事件数；过滤后的 events 可能为空但游标仍推进。客户端按返回的 next 继续，不可用 events 长度推算游标。

新 kind 为 `assistant_text_delta`、`assistant_text_reset`、`assistant_text_done` 与 `workflow_step_progress`。新 kind 必须 opt-in；请求正文流订阅 `include=assistant_text_delta` 时应同时处理该调用的 reset/done 生命周期。逗号列表可以同时订阅 workflow；非法 include 返回 HTTP 400 与 `INVALID_PROGRESS_INCLUDE`。不带 include 的 IM 等旧轮询不接收这些新 kind，只新增原工具行的顶层 `kind="tool_progress"` 与非空 `call_id`。

| kind | 关键字段与处理 |
|---|---|
| `assistant_text_delta` | `model_call_id`、`seq`、脱敏 `text`、`level="stream"`；按调用身份与 seq 去重 |
| `assistant_text_reset` | `model_call_id`、`reason=retry/cancel/tool_call/superseded`；丢弃对应旧候选，不能拼接到新调用 |
| `assistant_text_done` | `model_call_id`、最后一个 delta 的 `seq`（没有 delta 时为 0）；本调用正文流收口，不代替请求终态 |
| `workflow_step_progress` | `workflow_run_id`、`workflow_id`、`step_id`、`name`、`status`、`duration_ms`、`description`、`next_step_ids`、`seq`，公开行展开 workflow_step |
| `tool_progress` | 既有 tool/phase/detail 等字段，顶层 kind 不可被嵌套 progress 同名键覆盖 |

正文增量只在有效档位 `stream` 持久化，经跨块 holdback 脱敏；重试、取消和工具边界产生 reset，旧调用迟到数据不能污染新调用。不能复用 TUI 原始计量流 `model_delta` 作为公开正文。完整 stdout/stderr 不内联进度；沿原 owner/thread 权限通过 `display_archive_ref` 和 `/client/display-page` 分页读取。

候选缓冲默认保留末尾 256 字符，累计 128 字符后合批投影（小 holdback 会相应缩小合批阈值）。同调用保留完整投影上下文，未闭合标签属性不提前发送；合法但投影为空的 terminal 也会撤回旧候选并发 done。单调用最多保留 1,048,576 字符，累计处理字符预算默认为 16,777,216，切点扫描、投影与前缀比较按每次候选长度的三倍计费。容量或预算耗尽时发送 `superseded` reset，本调用停止候选投影，最终只发 done；客户端须继续读取权威 `/result`，不能把该回退解释为请求失败。这是工作量上限，不是延迟或外部吞吐保证。

**真实调用绑定：** `agent_core/tool_model_generation.py` 在原账本分配物理 `call_id` 后、发送提供方请求前，通过 writer 的 `bind_model_call` 冻结同一个编号到流回调。每次重试和后续工具轮重新分配并绑定，旧回调始终携带旧编号；该编号与最终 sealed-call 的 `model_source.call_id` 一致。普通回调与不支持绑定的旧出口保持原行为。

## `POST /client/history`：发现并续订阅在途回合

history 增量字段均沿用历史接口的 owner/thread 鉴权，不暴露其他用户请求；单进程和 coordinator 使用相同公开形状：

| 字段 | 空闲默认值 | 语义 |
|---|---|---|
| `active_request_id` | `""` | 当前活动请求 |
| `in_flight_status` | `"idle"` | `idle/pending/processing/stopping` |
| `turn_phase` | `""` | 当前结构化回合阶段 |
| `stream_cursor` | `0` | 活动请求 chunks 当前行游标，有界读取；不是已展示文本长度 |
| `progress_path` | `""` | 如 `/progress/{id}?since={cursor}` |
| `queued_request_ids` | `[]` | 同 scope 排队请求列表 |

前端打开或重连流程：

1. 先调用 history，展示已提交历史，读取 `active_request_id`、`in_flight_status` 和 `stream_cursor`。
2. 若状态不是 idle 且 active_request_id 非空，从返回的 `progress_path` 续轮询；需要正文/工作流时追加显式 include，不擅自清零 stream_cursor。
3. 每轮将游标更新为 `/progress.next`。新增行即使被过滤也应前移；按 model_call_id/seq 去重并处理 reset/done。history 游标之前的在途候选未承诺回填；若需要完整正文，必须读权威 result，不能将续订阅片段当作全文。
4. 同时查询 `/result/{active_request_id}`；收到上述终态后以顶层 response 更新最终正文，清除旧候选。重新读取 history，发现接续请求或排队变化。
5. 断线后重新发现当前 scope，不能用旧请求身份去附着到其他 owner/thread。停止中继续观察，不提前把 stopping 当作终态。

本合同提供 HTTP 轮询发现和续读，不声称新增了独立 SSE 端点。若外部 BFF 使用 SSE，应在保持游标与身份语义的前提下桥接该轮询。

### 列出当前 owner 的会话（operation: "list"）

仍使用已存在的 `POST /client/history` 及其可信来源认证。仅当 `operation` 精确等于字符串 `"list"` 时进入列表；其它值或缺省时保持原历史语义。列表不需要 `conversation_id`，也不使用历史的 `before_message_cursor`。

| 请求字段 | 类型、默认与限制 |
|---|---|
| `operation` | 固定字符串 `"list"` |
| `limit` | 正整数，默认 50，超过 100 截为 100；布尔、非整数或小于 1 返回 400 `invalid session list limit` |
| `cursor` | 上一页 `next_cursor` 或 null；最长 2048 字符，格式、版本、类型或 owner/viewer 不符返回 400 `invalid session cursor` |
| `include_inflight` | 布尔，默认 true；其它类型返回 400 `invalid include_inflight` |

owner 只由原认证 scope 和 owner resolver 决定，正文不能选择其它 owner。存在认证中间件时忽略正文伪造的 `user_id/channel`；没有中间件时保留原 `_gateway_control_scope` 的正文覆盖规则。列表额外字段不筛选会话，也不提升权限。未初始化服务仍返回原 500；不可信来源在读取前沿原认证门拒绝。

| 可信请求身份示例 | 选中的 owner | 可见绑定 |
|---|---|---|
| 不带身份头（原默认身份为 admin/chat） | `local/main` | `all_user_access=true` 时可见该库所有绑定，包括 TUI 的 `chat / sess_* / local-agent` |
| `X-User-Id: alice`、`X-Channel: local` | `local/alice` | 普通用户仅看该库 `local / alice` 的绑定 |
| `X-User-Id: alice`、`X-Channel: web` | `providers/web/users/alice` | 普通用户仅看该库 `web / alice` 的绑定 |

即使有跨用户显示权，也只枚举**本次解析得到的 owner 库**，不聚合其它 owner。权限判断只取 `scope.all_user_access`，不依据用户名字符串。

| 响应字段 | 类型与含义 |
|---|---|
| `ok`、`operation` | bool；固定字符串 `"list"` |
| `owner_id` | 本次解析的规范 owner 标识 |
| `items` | 当前页会话绑定数组，最多 100 项 |
| `next_cursor` | 后续候选的同 owner/viewer 游标；没有后续候选为 null |
| `coverage` | 正常 `complete`；条目/claim 读取错误为 `partial`；绑定目录不可读或 coordinator 来源失效为 `unknown`，此时 `ok=false, items=[]` |
| `inflight_coverage` | `complete`；请求窗口超预算为 `partial`；关闭在途投影为 `skipped`；在途来源不可用为 `unavailable` |
| `load_errors` | 安全错误数组：`code` 为 `CLIENT_SESSION_LOAD_FAILED` 或 `CLIENT_SESSION_CATALOG_UNAVAILABLE`；可含 `conversation_id` 和原安全 `error` 投影，不含路径或异常原文 |

| `items` 字段 | 类型与含义 |
|---|---|
| `conversation_id`、`thread_id` | 当前绑定的会话 ID、线程 ID |
| `owner_id`、`channel`、`channel_user_id` | 当前 owner 和真实绑定身份 |
| `title`、`preview` | 线程标题；热消息文件最近用户/助手正文预览，空白折叠、最多 160 字符 |
| `created_at`、`updated_at` | 线程记录的 epoch 秒数；排序另使用线程文件 mtime |
| `message_count` | 原 canonical marker 的消息总数（冷段计数加热文件消息）；超剩余扫描预算或 marker 失败为 null，不是 0 |
| `status`、`in_flight` | `running / queued / failed / idle`；仅前两种为 true |
| `active_request_id` | 当前开放或最先排队的 Gateway 请求 ID；无对应请求、claim-only 或多活动冲突为 null |
| `active_run_id` | 开放 Gateway 主回合的 `runtime_authority.run_id`，否则 null |

状态优先级为：Gateway 运行中 > 有效运行 claim（`status=running` 且 `float(expires_at)>now`）> Gateway 排队 > failed claim > idle。过期 claim 不算运行；claim-only TUI 回合没有 Gateway 请求 ID，不能据此拼 `/progress` 地址。多活动恢复冲突仅报告 running，不猜一个活动请求。claim 读错时保留条目、按空 claim 推导状态，并增加错误使 coverage 为 partial。

Gateway 投影复用原 owner、精确 scope、线程/claim 绑定、非终态与开放/停止中/closing 规则；排队按原 `_queue_order` 取首项。`active_run_id` 是**主回合 run ID，不是 agent-view 子代理 run ID**；不能直接用于子代理视图下钻。

排序键为 `(线程文件 st_mtime_ns, conversation_id, channel, channel_user_id)` 降序。游标是无填充的 base64url JSON 数组 `["s1", owner_id, viewer_digest, mtime_ns, conversation_id, channel, channel_user_id]`，viewer_digest 为 `sha256(channel + "\x1f" + user_id + "\x1f" + 权限位).hexdigest()[:16]`，权限位为 `"1"` 或 `"0"`。下一页取严格小于游标的键。并发更新可能使绑定上移，重新从第一页可见，不承诺跨页快照一致性。

成本与被动读取边界：

- 每次只读一次 `channel_bindings.json`，只对可见绑定逐一 lstat；不枚举线程、消息或 claim 目录，不使用旧会话列表索引。
- 按 2026-11-05 的 W1 裁决，每页最多检查 limit 个线程候选：直接读取候选线程，核对其 `channel_bindings` 三元组。缺失、非普通文件、归属不符或 archived 记录跳过；跳过后本页可能不足 limit，游标仍推进到本页末个已检查候选，避免重复空页。
- 预览每项最多读取热文件尾部 65,536 字节，并拒绝符号链接与非普通文件。消息计数每次共享最多 8,388,608 字节实际热文件读取预算，包含 canonical marker 的尾界、前缀校验与扫描；余额不足返回 null，不接受截断计数。按 `(st_dev, st_ino, st_size, st_mtime_ns)` 缓存，最多 1024 项。
- Gateway 在途状态每页只读取一次 processing/inbox 请求窗口，沿原预算最多 512 个目录项、1,048,576 字节；超预算不读取请求正文，仍应用 claim 状态。
- 所有读取均被动打开 owner Store，不创建文件、冷 owner 目录或 Agent；新 owner 返回 `ok=true, items=[]`。coordinator 读取前后核验同一个 control/runtime/paths 和 HTTP transport，来源失效丢弃整页，不回退单进程。

### 打开列表中的会话（target_thread_id）

将列表项中的会话 ID 和线程 ID 一同发往原 `POST /client/history`。这让拥有当前 owner 跨用户显示权的调用方打开该库的 TUI 绑定，而不是把 admin/chat 强行当成 local-agent/chat。

| 请求字段 | 类型与限制 |
|---|---|
| `conversation_id` | 列表项的 `conversation_id` |
| `target_thread_id` | 列表项的 `thread_id`；必须为去空白后非空字符串，原长度不超过 256，否则 400 `invalid target_thread_id` |
| `limit` | 沿原历史 `_request_limit`：默认 20，上限 200 |
| `before_message_cursor` | 原历史向前游标，非负整数或 null；仍先执行原校验，非法返回 400 `invalid history cursor` |

`target_thread_id` 仅在当前 owner 库中查找。候选绑定必须匹配请求的 conversation；普通用户还必须匹配原 channel/user。拥有 `all_user_access` 时优先当前精确绑定，否则按 `(channel, channel_user_id)` 选取稳定绑定，并经原 `resolve_report` 确认它仍指向目标线程。无权、缺失、非法路径 stem 或绑定已切换时返回原空历史形状，不跨 owner 查找。

成功和失败响应字段均保持原 `GatewayClientHistoryResult`：`ok/thread_id/turns/load_errors/display_events/message_cursor/before_message_cursor/has_older` 及上文在途字段，不加入 `operation/items`。有活动请求时可按返回的 `progress_path` 续读，进度与结果仍沿原鉴权。

旧 `thread_id/session_id` 继续只是 conversation ID 的别名，不是新增定点 selector；没有 list 或 target 字段的调用保持原行为。外部消费者的 live E2E 应在集成后独立复验，后端回环测试不代表外部浏览器或另一台机器已联通。

## 外部跟进与验证边界

- **#58-2 外部跟进**：新标签页自动挂载 SSE/轮询的前端不在本仓库；需按上述 history → stream_cursor → progress → result 流程接线。
- **#58-3 外部跟进**：第二窗口后台巡检流与底部状态栏同步，需消费 in_flight_status/turn_phase 及 `/status`；不因后端字段齐全宣称界面已联通。
- 测试分为组件检查、本地回环真实 Gateway Client → 路由 → Client 往返，以及使用合成后端的真实 Agent 生成 → 调用账本 → writer → HTTP progress 链路；具体通过范围以同版本的测试回执为准。合成后端和回环通过不等于真实供应商推理、外部浏览器或其他机器可达。
