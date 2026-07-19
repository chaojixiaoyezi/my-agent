# Gateway Structure

Gateway 负责把外部请求落成可审计队列，并由 worker 调用 SimpleAgent。它不负责模型业务决策。

2026-07-09 P0 维护仅清理 gateway 文件的 import/type lint，不新增入口或结构层。

## 核心文件

- `agent/gateway_parts/io.py`、`agent/conversation/store.py`：文件锁与线程锁组合的
  `locked_file_transition` / `task_transition_guard` / `goal_transition_guard` 是单任务或单目标状态迁移临界区；`/btw`、`/stop`、`/goal` 与完成关闭共用，不各自维护竞态规则。
- `agent/gateway_parts/control_service.py`：当前 processing turn 已绑定 task 时，`/status`、`/btw`、`/stop`
  以该精确绑定为 expected-task guard；没有活跃 turn 时才按 owner/thread 解析 active 根任务。失去当前任务
  竞态的 guidance 当场退休，既不进入旧任务也不污染新任务。linked live turn 存在时
  `/btw` 只写持久 guidance，由该 turn 在下一安全点消费；只有没有 live turn 的空闲根任务才
  发布 wake，不允许引导启动第二个主执行器。
- `agent/gateway_parts/http_handlers.py`、`agent/adapter/manager.py`：普通 `/ask` 先按可信 owner/channel/thread
  查询 live processing turn；命中时复用上方同一 guidance 主链并返回 typed `status=steered`，adapter 不再
  新建或覆盖第二份待回送记录。只有 durable task、没有 live turn 时保持普通入队，使这条消息拥有正常回复。
  该决定只看结构化身份和运行状态，不检查消息文字，也没有 Feishu 专用分支。
- `agent/concurrency/interrupt.py`：线程级 typed interrupt 除了供工具安全点轮询，还允许
  正在阻塞的传输注册短命、幂等的关闭回调。回调在共享锁外执行，执行线程退出时连同中断旗
  一起清理，避免线程复用携带旧任务状态。
- `agent/backends/gateway_helpers.py`：模型 JSON/SSE 响应读取在真正发请求时惰性挂接中断回调；
  `/stop` 会关闭正在读取的响应并报为 `InterruptedError`，不得包装成可重试的 provider 网络故障。
  与 concurrency 的依赖保持请求时惰性解析，避免 backend/runtime 初始化环。
- `agent/agent_core/tool_model_generation.py`：外层 Gateway/background worker 持有任务中断身份，
  真正的 provider 请求运行在 wall-timeout guard 子线程。模型调用边界必须注册一次中断转发，
  将外层 `/stop` 精确传给该子线程，并有界等待其收回；不得只在 provider helper 的子线程
  登记回调，否则任务名中断无法到达真正连接。
- `agent/agent_core/runtime/guidance.py`：`/btw` UserTurn 与精确匹配 durable task 的子代理生命周期 wake
  共用 active-turn 安全点。当前 tool-loop state 保证每条输入只注入一次；provider 生成前后检查新输入，
  丢弃过期响应。用户输入和运行事件都只在模型成功读取其 prompt 后确认，provider 失败时保持可重试；
  只负责模型自然回执的 auxiliary round 显式禁止消费 active-turn input，新输入会先淘汰旧回执草稿，再由
  真实任务轮读取。前台 request 已结束但输入尚未确认时，后台轮按精确 durable task id
  继续读取原 request inbox；确认后的输入已经写入同一 thread transcript，不再作为“新输入”重复注入。启动当前后台轮的 wake id
  留给 scheduler 确认，避免双消费。
- `agent/gateway_parts/request_execution.py`：live turn 注入新的真实用户输入时，只重新开放一次模型自然
  commentary 段；普通工具轮仍不会反复把碎碎念送给用户。旧模型片段先丢弃，新的 commentary/final 继续
  由同一个 request 的原回复投递链送出。
- `agent/agent_core/tool_loop/natural_user_reply.py`：非阻塞 `wait` 等需要中途回复时，短回复仍由模型按
  结构化事实自然撰写；它不建立第二个执行上下文，也不改变 thread/task 状态。
  presentation-only 参数明确把 `allowed_tools` 和 native tool IR 置空；部分兼容模型仍可能违规返回
  结构化 tool call，因此第一轮仍拒绝并重试。有界重试后只丢弃这份未授权机器调用，保留通过统一协议
  边界的自然正文；没有安全正文则返回 `user_reply_unavailable`，不能伪装成正常空回复。
- `agent/agent_core/runtime/task_identity.py`：区分一次 request/run 与持久 conversation task，为 guidance、
  进度账本、派工 seed、wait 和监督提醒提供唯一的结构化任务/账本键解析；task_local 子代理
  保持自己的 run 隔离。
- `agent/agent_core/parameters.py`、`tool_call_runtime.py`、`runtime/loop_support.py`：一次性编排工具同时使用
  exact payload key 和结构化 child intent key 去重；同一 assistant turn 的 batch + overlapping singles
  只执行首份副作用，compact continuation 重建相同 key 集合。
- `agent/agent_core/tool_call_runtime.py`、`agent/tooling/write_boundary.py`：写文件、编辑和补丁调用若声明的
  所有绝对变更路径只落在同一 thread 的一个旧 task 内，可在 effect 执行前结构化选回该 task；读操作、
  相对路径、多个候选或 lifecycle 阻塞均不自动选择，也不解析自然语言。
- `agent/agent_core/_finalization_service.py`、`agent_core/subagent_outputs.py`：普通任务最终回复直接来自模型；
  子代理结果和 artifact refs 只作为当前 request/run/task 的结构化事实交给主代理汇总，不再生成完成 marker
  或独立验收报告。后台轮按真实 `params.task_id` 认领子代理，任务目录的可读标题只作旧数据兼容。
- `agent/conversation/user_visible_text.py`：所有用户出口共用的内部协议净化器，覆盖 bracket tool block、
  XML function/tool envelope、模型以工具名直接降级成 XML 标签以及截断尾块；不得由各 IM adapter 另建
  deny list。
- `agent/gateway_parts/request_execution.py`：执行单个 request，并读取/写回同一 conversation 的
  累计消息历史；复用 runtime compact policy/token estimator/backend 在 owner+thread 内自动 compact，
  raw transcript 保留，thread summary/message+byte cursor/generation 是唯一 compact 状态；首次 compact
  后从 byte cursor 读取新增尾部，不重复扫描旧前缀。当前消息始终是独立 root
  prompt，普通请求不会自动续接旧任务。活跃任务和最近完成任务分栏注入；只有模型按用户明确续接意图
  调用结构化 `task_progress select` 后才重新打开原 task workspace，普通闲聊仍不绑定。存在候选时，
  另开 workspace 还必须在 `task_progress start` 中显式给 `new_task=true`；提示词只解释选择，真正拒绝
  未确认 start 的硬门位于 task tool。运行中的候选只提供结构化 task id/status/goal/path 索引，模型需要
  工作时再精确 select；它们不替换或过滤同一 thread history。根 task workspace 不再保存 recovery compact
  指针、continue packet 或第二份对话恢复包；主 thread 的 summary + raw tail 是唯一主会话 compact，
  独立子代理只使用各自 session compact。thread 创建与 compact 准备由独立 loader 报告各自错误，避免
  主组装函数吞掉边界。assistant 写回前将用户正文和近期产物 metadata 分栏；公开
  response 使用同一用户投影且不暴露服务器 path。typed tool progress、真实 model delta 与 runtime notice
  分栏写 chunk；工具事件以 `phase` 做机器判断、`status` 只做本地化展示。第一次工具开始前已有的模型
  正文只投影一条 `assistant_commentary`，不改变请求终态，
  provider/runtime notice 不得进入。执行轮
  初始已有 active task，或模型随后结构化 `select`/晋升 task 时，会把 `thread_id/task_id/task_path` 原子写入
  当前 processing record；多用户 Gateway 无法保存该绑定时阻断工作工具，不能继续产生一个控制不到的任务。
- `agent/conversation/task_promotion.py`、`agent/conversation/store.py`：模型用精确 `task_id` 选择旧任务时，
  只切换结构化 task/workspace lineage；本轮用户消息早已属于同一权威 thread transcript，不复制到 task
  guidance ledger。普通聊天没有结构化 select，因而不会获得任务写权限；正文不参与任务身份判断。
  conversation task link 是生命周期权威，`work/state.json`
  只投影同一 task path 的状态；状态文件最终解析目标必须仍在该 task 根内，符号链接越界直接拒绝。
  完成、停止或重新打开后同步更新。workspace 懒建通过共享
  `durable_task_id` 读取已选择任务，不能把新的 gateway request id 写成另一个任务身份。
- `agent/gateway_parts/request_worker.py`：worker loop、认领、完成、失败写回；准入按同会话单飞、
  每用户上限、全局上限三层记账。owner 只从 adapter 的结构化 channel identity 构造：
  `p2p/private -> provider_user(user_id)`，群聊 -> `provider_group(chat_id)`；远程 owner 建立失败终态
  fail-closed，不从 conversation 字符串或首个发言人猜归属。
- `agent/conversation/control_commands.py`：CLI/IM 共用的 `/status`、`/btw`、`/stop`、`/goal` typed command、状态
  DTO 与确定性用户文本；自然语言不参与硬控制判断。
- `agent/gateway_parts/control_service.py`：按可信 user/channel/conversation 解析同一 thread；有 processing
  record 时优先读取其精确 task binding，避免更新但无关的旧 task link 抢走控制权；没有绑定时才选择
  user-selectable active 根 task，尚未晋升则回落 processing request。`/stop` 同时持久中断根 task 和当前
  turn，并向两种 interrupt id 发信号；`/status` 显示当前 turn 的时长/进度、子代理与唯一 thread compact
  generation，不展示 task workspace recovery package 为第二种上下文。
- `agent/gateway_parts/goal_control_service.py`：按已解析的 owner/thread 执行持续目标的查看、创建、修改、暂停、恢复和清除；每 thread 只允许一个未结束目标，复用同一根 task/workspace。
- `agent/agent_core/tool_runtime_ledger.py`、`agent/tooling/write_boundary.py`：远程普通 owner 在已有结构化
  task workspace 时，把文件工具、shell、PTY、LSP 的可写域统一收窄到当前 `task_root`；同 owner 旧任务
  可读不可写，子代理窄授权不放大，畸形根或空解析结果 fail-closed。local 与显式 admin bypass 不自动
  收窄；任务身份只读 runtime facts，不读自然语言。
- `agent/tooling/registry_invoke.py`、`agent/tooling/shell.py`：`run_command` 和 PTY start 没有显式
  `working_dir` 时使用结构化 `task_root` 作为进程 cwd；显式目录优先，ShellTool 仍按注入的 workspace roots
  验证。LSP 和已存在 PTY 的后续动作不被重写，命令正文不参与判断。
- `agent/conversation/goal_tools.py`：持续目标轮的 `get_goal` / `create_goal` / `update_goal`；工具只能读写
  当前结构化 thread+task 绑定，模型只能通过 update 写 `complete` 或 `blocked` 终态。
- `agent/conversation/runtime.py`：每个后台续接 turn 都读取同一 thread 的 compact summary 与完整 raw tail；
  task id 只约束 wake、progress、workspace 和子代理树等运行事实，不能过滤消息或建立 task-scoped history。
  持续目标轮携带精确 goal id，未进入 complete/blocked/paused/cleared 才发布一个去重续跑 wake。scheduler
  只有在没有 linked live turn 时才能启动续接；定时或生命周期 wake 在精确 task 已终态时直接退休。
- `agent/agent_core/runtime/guidance.py`、`agent/agent_core/runtime/active_turn_input.py`、
  `agent/backends/tool_ir.py`、
  `agent/backends/message_adapter.py`：`/btw` 在安全点按 typed guidance id 进入当前执行 turn；内容以
  `UserTurn` 留在 provider-neutral 历史的真实时间位置，并映射为 provider `role=user`。模型成功接收后，
  guidance id 幂等追加到同一 thread transcript；它不伪装成 runtime injection。compact 创建新工具循环时
  通过 typed active-turn packet 续接，不解析自然语言、不建立任务专属历史。
- `agent/common/audit_activation.py`、`agent/gateway_parts/request_execution.py`、`agent/ingestion/watch_tool.py`：`/audit` 只在请求前缀显式激活，并把 guarantee/window 写入 task attributes；watch 不再从 prompt、goal 或 summary 重新猜测。
- `agent/agent_core/runner/context.py`：前台聊天与后台任务共用 Agent 时，当前 prompt/run/task/tool-loop 按线程与 agent 弱引用身份隔离；对象销毁即清理，禁止 Python object id 复用把旧工作区带给新 Agent。
- `agent/adapter/manager.py`：把 `channel_chat_type/channel_chat_id` 与 user/message/conversation identity
  一起写入 gateway ask metadata；provider 专有字段在 adapter 边界归一，request worker 不依赖 Feishu
  payload 细节。控制命令在 `/ask` 前走 `/control`，不进入普通单飞队列。
- `agent/gateway_parts/queue_service.py`：request/response/history/index 文件队列。
- `agent/gateway_parts/request_worker.py`：认领、执行和终态归档。归档请求的 `status` 以最终 response 为
  权威，不能让 processing lease 的旧状态覆盖 `done/interrupted/failed`；lease 只保留运行期计数与心跳。
- `agent/gateway_parts/lease_service.py`：processing lease 和 heartbeat。
- `agent/gateway_parts/adapter.py`：文件 adapter 到 gateway ask 的转换，直接调用 `request_worker`。
- `agent/gateway_parts/channel_health.py`：把 adapter daemon 的 PID、heartbeat 与逐通道 JSON 状态投影为
  registry health；状态缺失、损坏、进程死亡或心跳过期均 fail-closed，不读取日志正文。
- `agent/core.py`、`agent/owner_scoped_pool.py`、`agent/gateway_parts/request_worker.py`：adapter health 是
  基础 Gateway 进程的只读事实，由 composition root 显式传给 scoped owner；各 owner 仍持有独立 registry、
  凭据配置和 conversation binding，禁止从 owner 私有 gateway 目录重新推导共享进程是否运行。
- `agent/gateway_parts/recovery.py`：processing 恢复，直接读取 `lease_service` 判断 heartbeat。
- `agent/gateway_parts/http_handlers.py`：HTTP 入口；`/result/<request_id>` 的 USER 权限始终从请求记录
  读取 owner，排队/执行态查 pending/processing，完成态查 done/failed，禁止把 response 正文当身份源；
  `/progress/<request_id>?since=` 复用同一 owner 权限并只返回 thread 已启用的 typed progress；
  `POST /control` 是用户会话任务的即时控制入口，管理员 `POST /stop` 仍只停止 Gateway 服务。
- `agent/gateway_parts/response_renderer.py`：响应渲染、响应文件结构化读取、客户端轮询状态去重。
- `agent/delivery/registry.py`：channel adapter、懒工厂、capabilities、配置状态、运行健康、当前结构化
  binding 和 target validator 的唯一注册表；新增 IM 通过注册扩展，不修改投递服务或能力工具。
- `agent/delivery/service.py`：普通最终回复、后台主动消息和显式发送的统一出口；组合可信
  `DeliveryContext` 与无收件人的 `ReplyEnvelope`，净化正文后走原生 text/reply/image/file API，
  返回 `DeliveryReceipt` 并对相同失败做有界去重。
- `agent/user_space/owner_quota.py`：owner 结构化写入口的跨进程准入锁；在锁内扫描当前 logical bytes，
  按完整 multi-file mutation 的最终字节判断，策略/usage/lock 不可读时 fail-closed。
- `agent/user_space/home_retention.py`：只按 typed retention policy、terminal authority 和时间生成/执行
  plan；task/scratch 使用执行前状态复验、trash tombstone、legal hold 和 audit。
- `agent/user_space/owner_maintenance.py`：记录 owner 上次维护尝试/成功和结果；损坏 policy 不执行删除。
- `cli/gateway_loops.py::_GatewayOwnerMaintenanceController`：用 owner discovery cursor 有界轮询，不创建
  scoped Agent；全局扫描频率与每 owner policy 的实际维护间隔分离。
- `agent/conversation/channels.py`：通道 typed context/envelope/attachment 与统一 user-facing reply projection。
  内部运行协议在此从外部正文中移除；宿主绝对路径只在真实通道出口显示 basename，内部 transcript
  保留原路径供后续工作续接。
- `agent/agent_core/tool_loop/natural_user_reply.py`：派工与 wait 的辅助自然回复出口；不携带旧 tool
  context/native IR/runtime injection，只按 typed runtime status、结构化工具调用、空正文和内部协议
  做机器形态校验；不再用自然语言正则猜完成、ETA 或大小。普通任务最终回复不经过第二次验收或摘要
  重写，直接使用主模型自然正文。Gateway 对 `user_reply_unavailable + 空正文` fail-closed，不写空
  assistant 修复队列，也不把请求标成成功；通道运行时 的空 final/streamed text 回退与 长期助手 的
  commentary/final-delivery 分离只作为边界对照，my-agent 仍由同一个模型回复投影负责正文安全。
- `agent/conversation/runtime.py`：后台唤醒继续使用内部协议做运行裁决，但在写普通 assistant transcript
  和返回后台 report 前必须经过同一 user-facing projection；原始内部协议只交投递服务做抑制判定，
  不得进入 compact 或 owner-local 会话搜索。自动派工监督使用 `progress_fingerprint.py` 的结构化状态
  指纹；无 material delta 时只顺延 policy，不调用 LLM，显式 wait/数据巡检不受影响。后台根任务轮按
  durable task id 注册协作中断，发送前抑制 cancelled/abandoned/superseded 任务的迟到正文。
- `agent/capability/channel_message_tool.py`：主代理唯一 `send_message` 工具。收件人由 scoped owner
  决定，附件必须通过 task registry、owner 边界、ready 状态与 hash 校验，并保存幂等回执。
- `agent/adapter/delivery.py`：交互消息提交后的持久化异步回送；pending/sent receipt 支持重启恢复，
  只轮询既有 request_id，不重新运行 Agent；同一 pending 记录保存 progress cursor，进度和最终答复均
  通过统一 DeliveryService 回送。commentary/工具进度失败时仍前移 cursor，防止重复刷屏或阻塞最终答复；
  最终答复继续由独立耐久 receipt 保证。
- `agent/conversation/compact.py`、`history_index.py`、`directives.py`：分别承载 owner/thread 自动 compact、
  owner-local 旧聊天检索投影，以及 per-thread `/verbose off|on|full` 状态；都不从自然语言推断 owner。
- `agent/conversation/authority.py`、`task_promotion.py`：普通 transcript 唯一权威标记，以及任务候选的
  结构化选择、已完成或已中断任务重开、误建占位任务 supersede、提升和完成关闭。
- `agent/gateway_parts/supervisor.py`：gateway supervisor 的启动、停止、重启、heartbeat 健康判断和
  runtime status 写入；不拆成 facade/operation 影子文件。
- 旧 `chunk_service.py` / `context_tokens.py` facade 已删除；请求正文压缩、上下文显示和响应渲染走当前 request execution / renderer 主链路。
- `cli/gateway_loops.py`：gateway request worker 池、后台主代理 tick、heartbeat loop。
- `cli/gateway_process.py`、`cli/gateway_client.py`：
  启动、停止、状态和客户端命令；`gateway_process.py` 直接承载公开 gateway 命令实现，不再转发到 `_gateway_commands.py`。
  watch 返回必须分类为计划 stop、signal shutdown、有限轮完成或意外返回；SIGTERM/SIGINT 先落 typed
  stop request/forensics 再走同一 drain，意外返回非零退出，cleanup 另记 drain 结果。
- `cli/chat_parts/control_runtime.py`：终端 Gateway 模式调用同一 `/control`；本地直跑模式使用同一 typed
  command/状态渲染并以当前 `RunParams.request_id` 控制本进程任务。
- `cli/gateway_service.py`：systemd/launchd service unit 生成和安装/卸载入口；不再拆成私有 facade helper。
- `agent/gateway_parts/process_control.py`：进程存活、终止和等待退出的唯一进程控制模块。
  `daemon_control.py` 只处理 PID record、后台化、锁和 shutdown request，不再作为进程控制转口。
- `agent/tooling/process_registry.py`、`agent/tooling/shell.py`：模型命令进程的独立生命周期权威。前台超时、
  用户中断、后台 kill 和日志上限都复用 registry 的完整后代树终止；POSIX 会快照后代及进程出生标识，覆盖
  bwrap `--new-session` 建出的嵌套 session。shell 只负责 2 秒有界 pipe drain，不能用无界
  `communicate()` 等待可能被孙进程继承的 stdout/stderr。
- `agent/gateway_parts/scoped_locks.py`：机器级【进程单例】锁（pid+进程启动时间判归属，
  长期助手 风格）。用于"同一台机器同一 scope+identity 只有一个活进程持有"的网关身份独占；
  持有进程重复 acquire = 刷新心跳，死进程残留锁自动接管。`daemon_control.py` 是它的
  公共 API 转口。
- `agent/gateway_parts/daemon_metadata.py`：统一提供 process-domain（machine/hostname + PID namespace）、
  PID 和 start_time 身份。后台会话 claim 与 gateway PID record 共用，不复制一套存活判定。

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

流式响应 chunk 写入被认领请求所在的 `requests/processing/<request-id>.chunks.jsonl`；即使执行者是
per-owner Agent，也必须跟随基础 Gateway 的权威队列记录，不能改从 owner Agent root 推导。请求结束时
随 request 归档到 `requests/done/` 或 `requests/failed/`，最终 response 会记录 `chunk_stream_path`。
移动前必须把最终 response 的 `done/interrupted/failed` 写回 request JSON，确保目录、请求记录、
`/result` 与 `/status` 不会一边终态、一边仍显示 processing。
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
  all-user 权限的可信管理员可读取无请求归档的孤立 response。普通 USER 的完成 response 必须再经
  顶层字段白名单投影，新增内部字段默认不公开；损坏请求记录不得把 load report 路径返回给 USER。
- status/doctor 要能看到当前 processing request 的结构化租约事实，包括 request id、
  lease owner、attempts、lease/heartbeat/update age 和 chunk stream 路径；这些只用于观察，
  不作为调度或验收硬门。
- worker 秒退、参数错、import 错要立即标记失败状态，不能伪装成 processing/planning。
- 无限 gateway watch 没有 stop request 却返回时必须写 `GATEWAY_WATCH_UNEXPECTED_RETURN` 并返回非零；
- 后台主代理 claim 默认 TTL 90 秒，`background_claim_heartbeat_interval_seconds=0` 表示按 TTL 自动取安全
  间隔（默认 30 秒），不是 50ms 热写。同一进程域旧 owner 已死可立即接管；跨 Pod/旧 claim 无法证明时
  等待 TTL，保证 RWX 事实源上不会双执行。
  三个后台线程任一未在 drain deadline 内结束时写 `GATEWAY_DRAIN_INCOMPLETE`。计划停止与有限轮完成
  保持 exit 0，但 termination kind/reason 必须持久化。
- CLI `gateway ask` 和 HTTP `/ask` 都必须写 `conversation` 结构化字段；本地 CLI 默认使用
  `gateway-cli/default`，HTTP 使用请求体里的 `conversation_id` / `session_id` /
  `thread_id`，缺省为 `default`。Feishu 必须传真实 `chat_id`，话题再叠加 `thread/root`，不得退化成
  user id 或“该用户最近 thread”。
- ordinary channel input 始终走同一 thread：是否调用文件、派工或定时工具由模型决定，不预先根据
  文本分“聊天/任务”，也不接受外部 lane/task selector。`/audit`、`/goal` 只是同一 thread 上的显式 overlay。
- 持久提醒由 `agent/scheduler/` 的 owner job/run 事实源和 `schedule` action tool 管理。
  到期时以 typed wake metadata 回到创建时的同一 thread，不读取用户文本推断身份或会话；
  `wait` 只负责 active task 内让出，两者不共享第二份 transcript/compact。
- `SchedulerRepository` 仍是唯一公开 facade；内部按 job CRUD、run claim/lifecycle、store/quota 三个职责
  mixin 组合，避免一个超大类同时承担全部状态转换，但不会产生第二份 store 或替代入口。
- 普通 turn 不预建 task workspace；只有注册表 `promotes_task` 或结构化任务动作能惰性晋升。派出子代理
  本身不自动结束当前 turn；模型可继续协调，只有显式非阻塞 `wait` 或正常最终回复结束本轮。用户正文由
  LLM 自然表达，子代理执行和命令记录留在 TaskRun，不直接写普通 transcript。
- conversation context 只包含同 thread 已完成的 user/assistant raw tail 与该 thread 的 compact summary，
  并明确是历史参考；当前 `# User Task` 优先。固定 `conversation_history_max_turns` 只决定 compact 后
  优先保留多少近期 turn，不得在 compact 前截断累计历史。工具执行产生后台任务时用结构化 task link，
  不把旧 goal 拼进普通消息。
- assistant 历史正文不得保存或重放 `MAIN_AGENT/RUN/SUBAGENT` 内部协议；完成轮次的产物引用写入
  message metadata。后续“发我”使用 `Recent Artifact Refs.path` 调 `send_message`，不得重做旧任务。
- transcript 持久化对 user 消息 fail-closed；assistant 消息失败走持久 repair。conversation-backed
  run 禁止再自动写 owner-global dialogue memory，稳定偏好继续由 USER/preference authority 提供。
- 入站请求不预选 task。普通请求只展示带精确 status/path 的只读候选，结构化 select(task_id) 后才能
  续接；存在候选时显式 `start + new_task=true`，或无候选时的首个任务工具才可绑定当前 run，结构化
  终态后从 active 热索引移除。
  `subagent-*` 和 `bg-main-*` 内部链接不进入普通用户可选择候选；工作区决策只针对用户可见的根任务。
  所有 `promotes_task` 工具共享同一个决策门，失败 select 不得降级为懒晋升。select 会同步 run workspace，
  公共工具轮负责把本轮占位根的结构化参数重定向到所选根。该决策不解析用户自然语言。
- 同一 `canonical_user_id + channel + channel_conversation_id` 同时最多执行一条。必须在 claim 前
  占位、完成/提交失败/claim race 时成对释放；不同 conversation 不共用此单飞槽。
- 子代理完成 wake 在消费前校验结构化 root task link；已 completed/superseded 的根只归档迟到信号，
  不再启动后台主代理或写普通会话。
- 成功完成 wake 可短暂按 thread 合并，但失败/阻塞必须立即处理；后台主代理的内部整合回复和用户通知是
  两个不同结果面。部分成功只更新内部任务事实，全部结束/异常/需决策才写普通 transcript 和外呼 IM。
- observation + wake 的生产顺序必须由 store 统一封装为 wake-first 发布；消费者不得依赖两个独立文件
  “通常会挨着写完”。内部 continuation policy 只有在 root 不再存在运行中子任务时才允许进入公开结果面。
- 后台 task record 必须从 `ThreadTaskLink` 恢复权威 goal 与 workspace；任何 background prompt、wait reason
  或继承父 conversation task id 的子代理 prompt 都无权覆盖这两个持久字段。
- `bind_task` 是 task identity 的唯一创建/补空入口：已有 goal/task_path/created_at 不可覆盖，跨 thread
  重绑 fail-closed；状态变化走显式 status/update 接口。调用者不再各自实现“记得保留旧字段”的软约定。
- 开启 per-user owner（发布默认）后，远程 channel 的 owner 解析/创建失败不得回退基础 agent；
  必须写 `OWNER_SCOPE_UNAVAILABLE` 失败响应并归档，避免重试期间或故障时串户。
- 私聊 owner 固定落 `owners/providers/<provider>/users/<user_id>`；群聊固定落
  `owners/providers/<provider>/groups/<chat_id>`。群聊必须同时有结构化 group chat type 和 chat id；
  缺少群 id 时不能凭 conversation id 猜一个 group owner。
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

## 2026-07-18 owner quota 扫描竞态边界

- `agent/user_space/owner_quota.py::owner_logical_usage_bytes` 是结构化 owner 写入口共用的逻辑用量事实。
  原子 JSON writer 与 SQLite 会在目录枚举后删除临时/WAL 文件；单 entry 随后 `stat` 返回
  `FileNotFoundError` 表示它当前已不占配额，应只跳过该 entry，不能让整个 owner 的 Scheduler/Memory/
  Persona/文件写入都误报 `OwnerQuotaUnavailable`。
- 仅 `FileNotFoundError` 可被当成并发消失：owner 根权限错误、非目录、目录遍历错误和单文件
  `PermissionError` 继续 fail-closed；regular file 只做一次 `stat(follow_symlinks=False)`，symlink 不计入也
  不跟随。锁内最终用量与整批变化的准入语义不变。
- 该分流对照 会话运行时 `会话运行时-rs/exec-server/src/local_file_system.rs::read_directory` 对枚举后 metadata 已失效
  entry 的跳过，以及 通道运行时 `src/security/installed-plugin-dirs.ts` 对 `ENOENT/ENOTDIR` 与其他读取错误
  的区分；没有按文件名、运行日志或自然语言猜“这是临时文件”。
- 回归分别制造枚举后原子删除、单文件权限失败和 owner 根权限失败，证明只放过真正不存在的 entry。
