# Gateway Structure

Gateway 负责把外部请求落成可审计队列，并由 worker 调用 SimpleAgent。它不负责模型业务决策。

2026-07-09 P0 维护仅清理 gateway 文件的 import/type lint，不新增入口或结构层。

## 核心文件

- `agent/gateway_parts/request_execution.py`：执行单个 request，并读取/写回同一 conversation 的
  累计消息历史；复用 runtime compact policy/token estimator/backend 在 owner+thread 内自动 compact，
  raw transcript 保留，thread summary/message+byte cursor/generation 是唯一 compact 状态；首次 compact
  后从 byte cursor 读取新增尾部，不重复扫描旧前缀。当前消息始终是独立 root
  prompt，普通请求不会自动续接旧任务。assistant 写回前将用户正文和近期产物 metadata 分栏；公开
  response 使用同一用户投影且不暴露服务器 path。typed tool progress 与 model delta 分栏写 chunk。
- `agent/gateway_parts/request_worker.py`：worker loop、认领、完成、失败写回；准入按同会话单飞、
  每用户上限、全局上限三层记账，远程 owner 建立失败终态 fail-closed。
- `agent/gateway_parts/queue_service.py`：request/response/history/index 文件队列。
- `agent/gateway_parts/lease_service.py`：processing lease 和 heartbeat。
- `agent/gateway_parts/adapter.py`：文件 adapter 到 gateway ask 的转换，直接调用 `request_worker`。
- `agent/gateway_parts/recovery.py`：processing 恢复，直接读取 `lease_service` 判断 heartbeat。
- `agent/gateway_parts/http_handlers.py`：HTTP 入口；`/result/<request_id>` 的 USER 权限始终从请求记录
  读取 owner，排队/执行态查 pending/processing，完成态查 done/failed，禁止把 response 正文当身份源；
  `/progress/<request_id>?since=` 复用同一 owner 权限并只返回 thread 已启用的 typed progress。
- `agent/gateway_parts/response_renderer.py`：响应渲染、响应文件结构化读取、客户端轮询状态去重。
- `agent/delivery/registry.py`：channel adapter、懒工厂、capabilities 和 target validator 的唯一注册表；
  新增 IM 通过注册扩展，不修改投递服务。
- `agent/delivery/service.py`：普通最终回复、后台主动消息和显式发送的统一出口；组合可信
  `DeliveryContext` 与无收件人的 `ReplyEnvelope`，净化正文后走原生 text/reply/image/file API，
  返回 `DeliveryReceipt` 并对相同失败做有界去重。
- `agent/conversation/channels.py`：通道 typed context/envelope/attachment 与统一 user-facing reply projection。
  内部完成/运行协议在此转换成人话，产物 path 只保留在内部结构化引用。
- `agent/capability/channel_message_tool.py`：主代理唯一 `send_message` 工具。收件人由 scoped owner
  决定，附件必须通过 task registry、owner 边界、ready 状态与 hash 校验，并保存幂等回执。
- `agent/adapter/delivery.py`：交互消息提交后的持久化异步回送；pending/sent receipt 支持重启恢复，
  只轮询既有 request_id，不重新运行 Agent；同一 pending 记录保存 progress cursor，进度和最终答复均
  通过统一 DeliveryService 回送。
- `agent/conversation/compact.py`、`history_index.py`、`directives.py`：分别承载 owner/thread 自动 compact、
  owner-local 旧聊天检索投影，以及 per-thread `/verbose off|on|full` 状态；都不从自然语言推断 owner。
- `agent/conversation/authority.py`、`task_promotion.py`：普通 transcript 唯一权威标记，以及任务候选的
  结构化选择、提升和完成关闭。
- `agent/gateway_parts/supervisor.py`：gateway supervisor 的启动、停止、重启、heartbeat 健康判断和
  runtime status 写入；不拆成 facade/operation 影子文件。
- 旧 `chunk_service.py` / `context_tokens.py` facade 已删除；请求正文压缩、上下文显示和响应渲染走当前 request execution / renderer 主链路。
- `cli/gateway_loops.py`：gateway request worker 池、后台主代理 tick、heartbeat loop。
- `cli/gateway_process.py`、`cli/gateway_client.py`：
  启动、停止、状态和客户端命令；`gateway_process.py` 直接承载公开 gateway 命令实现，不再转发到 `_gateway_commands.py`。
- `cli/gateway_service.py`：systemd/launchd service unit 生成和安装/卸载入口；不再拆成私有 facade helper。
- `agent/gateway_parts/process_control.py`：进程存活、终止和等待退出的唯一进程控制模块。
  `daemon_control.py` 只处理 PID record、后台化、锁和 shutdown request，不再作为进程控制转口。
- `agent/gateway_parts/scoped_locks.py`：机器级【进程单例】锁（pid+进程启动时间判归属，
  长期助手 风格）。用于"同一台机器同一 scope+identity 只有一个活进程持有"的网关身份独占；
  持有进程重复 acquire = 刷新心跳，死进程残留锁自动接管。`daemon_control.py` 是它的
  公共 API 转口。

## 路径

gateway 运行态写当前 owner workspace runtime：

```text
owner_home/workspace/runtime/workspaces/<workspace-scope>/gateway/
|-- requests/
|-- responses/
|-- history/
|-- workers/
|-- leases/
`-- index/
```

流式响应 chunk 写入 `requests/processing/<request-id>.chunks.jsonl`；请求结束时随 request
归档到 `requests/done/` 或 `requests/failed/`，最终 response 会记录 `chunk_stream_path`。
客户端补读 chunk 时按 processing -> done -> failed 的结构化候选路径查找，不靠日志文本猜测。

## 规则

- `scoped_locks.py` 只提供进程对进程互斥，【不提供】进程内线程互斥：同进程任意线程
  acquire 同一把锁都是持有者重入（刷新心跳），任意线程 release 都按进程维度删锁。
  单进程多线程的临界区（如 request worker 池内共享状态）禁止复用这把锁，应使用
  `threading.Lock`（参考 `agent/io/jsonl.py` 的"线程锁 + flock"双层模式；对照组
  长期助手 ProcessRegistry 同样将进程身份锁与线程互斥锁语义分离）。钉子：
  `tests/test_real_io_concurrency.py::test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex`。
- request/response/history 损坏要显式报告 load_error，不能渲染成“没有记录”。
- USER 读取完成响应必须先用 pending/processing/done/failed 中的请求记录校验 owner；同 request id
  出现多份记录时必须全部归于同一 owner。归档缺失、损坏或任一身份不匹配时 fail-closed；只有具备
  all-user 权限的可信管理员可读取无请求归档的孤立 response。
- status/doctor 要能看到当前 processing request 的结构化租约事实，包括 request id、
  lease owner、attempts、lease/heartbeat/update age 和 chunk stream 路径；这些只用于观察，
  不作为调度或验收硬门。
- worker 秒退、参数错、import 错要立即标记失败状态，不能伪装成 processing/planning。
- CLI `gateway ask` 和 HTTP `/ask` 都必须写 `conversation` 结构化字段；本地 CLI 默认使用
  `gateway-cli/default`，HTTP 使用请求体里的 `conversation_id` / `session_id` /
  `thread_id`，缺省为 `default`。Feishu 必须传真实 `chat_id`，话题再叠加 `thread/root`，不得退化成
  user id 或“该用户最近 thread”。
- ordinary channel input 始终走常规对话链：是否调用文件、派工或定时工具由模型决定，不预先根据
  文本分“聊天/任务”，也不要求用户提供 `task_ref`。`/audit`、`/goal` 才是显式特殊入口。
- conversation context 只包含同 thread 已完成的 user/assistant raw tail 与该 thread 的 compact summary，
  并明确是历史参考；当前 `# User Task` 优先。固定 `conversation_history_max_turns` 只决定 compact 后
  优先保留多少近期 turn，不得在 compact 前截断累计历史。工具执行产生后台任务时用结构化 task link，
  不把旧 goal 拼进普通消息。
- assistant 历史正文不得保存或重放 `MAIN_AGENT/RUN/SUBAGENT` 内部协议；完成轮次的产物引用写入
  message metadata。后续“发我”使用 `Recent Artifact Refs.path` 调 `send_message`，不得重做旧任务。
- transcript 持久化对 user 消息 fail-closed；assistant 消息失败走持久 repair。conversation-backed
  run 禁止再自动写 owner-global dialogue memory，稳定偏好继续由 USER/preference authority 提供。
- task link 只有显式内部 `task_ref` 或当前特殊模式才能在入站时注入；普通请求即使存在 active link
  也不自动注入。普通请求只展示只读候选，结构化 select 后才能续接；真正调用任务工具后可在运行中
  绑定当前 run，结构化 closeout 成功后从 active 热索引移除。
- 同一 `canonical_user_id + channel + channel_conversation_id` 同时最多执行一条。必须在 claim 前
  占位、完成/提交失败/claim race 时成对释放；不同 conversation 不共用此单飞槽。
- 开启 per-user owner（发布默认）后，远程 channel 的 owner 解析/创建失败不得回退基础 agent；
  必须写 `OWNER_SCOPE_UNAVAILABLE` 失败响应并归档，避免重试期间或故障时串户。
- gateway 内部实现直接引用 owner 模块：ask 队列走 `request_worker`，lease/heartbeat 走
  `lease_service`，不保留单独的 `runtime.py` re-export 层。
- gateway ask 请求 ID 由 `new_gateway_request_id()` 生成；所有 CLI/chat/adapter 入口都应走
  `submit_gateway_ask()` 或同一生成器，不能用时间戳截断值自行拼 ID。ID 是队列、response、
  chunk stream 和审计记录的结构化关联键。
- gateway request worker 空闲轮询间隔由 `gateway_request_poll_interval` 控制，单位秒，可填小数；
  默认 `0.2`，配置小于 `0.05` 会回到默认值。
- 多 chat/gateway client 共享同一队列时，本地 IO 不应成为瓶颈；慢点应主要来自模型或外部服务。
- processing 目录只表示当前正在处理的 request；完成后的 request JSON 和 chunk stream 都必须进入
  done/failed 归档，便于多客户端观察和后续排障。
- 外部 channel 的目标结果使用 `conversation/channels.py` 的 typed decision 表达，provider validator
  由 `delivery/registry.py` 注册；投递服务不得把任意字符串交给 provider 后再依赖 HTTP 400 纠错。
  Feishu 当前使用 `receive_id_type=open_id`，因此主动外呼目标必须是 `ou_`。
- 外部附件发送不得接受模型指定的任意 channel/target，也不得只凭现存 path 发送；必须命中当前 owner
  的 artifact registry，发送前重新核对真实路径和 hash。公开 Gateway response 只返回文件名，不返回
  绝对路径；跨轮内部引用只存 owner transcript metadata。
- adapter service 日志必须安装公共 log redaction factory/formatter；SDK 日志不因来自第三方模块而绕过
  secret 清理。投递失败日志禁止打印完整目标或消息正文。

## 2026-06-10 空闲 IO 与队列观测

- `GatewayInboxScanGate`（gateway_parts/request_worker.py）：inbox 目录 mtime 未变且上轮扫描为空时
  跳过 glob+逐文件读；带 2 秒粗粒度文件系统保护与 deferred（not_before_at）例外。每个 worker
  持有自己的门，空闲时单轮成本从全目录扫描降为一次 stat。
- worker-0 的 stale lease 恢复扫描改为按 `gateway_processing_timeout_seconds/3`（至少 2 秒）节流
  （cli/gateway_loops.py `_RecoverThrottle`），不再每个轮询周期全量扫 processing 目录。
- heartbeat 新增 `queue_ages`（gateway_parts/io.py `gateway_queue_ages`）：最老 pending 等待秒数、
  最老 processing lease 年龄，只读文件 mtime，仅用于观测展示，不参与调度或恢复决策。
- `agent/io/jsonl.py` 路径锁改为引用计数 + 容量水位回收，长驻 gateway 进程不再无限增长；
  Windows（无 fcntl）下线程锁仍是唯一互斥，引用计数保证不会出现双锁并行写。
