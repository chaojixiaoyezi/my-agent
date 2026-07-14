# Gateway Progress

## 2026-07-14 普通会话即时状态、纠偏与停止

- 新增 adapter-neutral conversation control protocol。只有精确 `/status`、`/btw <内容>`、`/stop`
  能进入硬控制面；普通自然语言仍是软消息，不通过语义猜测获得取消或运行时改写权限。
- `ChannelManager` 在附件下载和 `/ask` 前识别控制命令，直接调用 Gateway `POST /control` 并通过既有
  `DeliveryService` 回复。它不占同会话普通请求单飞队列，因此长任务运行时仍能即时查询、纠偏和停止；
  新 IM 复用 manager/HTTP 协议，不增加平台分支。
- `/btw` 写当前 request id 的一次性 guidance。工具循环每轮构建 prompt 时消费并标记 delivered；若
  guidance 在 provider 生成途中到达，旧响应不执行工具也不直接结束，下一轮先纳入新要求。任务不存在
  或刚结束时不保存到 thread/下一任务；旧 `/btw` 列表、永久 runtime injection 与 `/btw-clear` 删除。
- `/stop` 先在 processing 请求原子写 `cancel_requested`，再按 request 登记名递协作中断，并异步取消
  活跃子代理树。新建子代理以 typed `conversation_request_id` 继承发起请求，状态与取消不从 goal/
  agent name 猜归属。工具循环在模型前后和工具前检查；前台 shell 每 200ms 检查并终止整个进程组。重启
  读到停止标记会直接归档 `cancelled/CANCELLED`，不会重新执行。管理员 Gateway 生命周期 `POST /stop`
  保持原义，用户任务使用 `POST /control`，两者没有混用。
- `/status` 只从当前 owner/channel/conversation 的 request、typed progress、subagent state 和 thread
  compact/verbose 事实渲染；不显示内部工具名、命令、路径、引导内容或“最近一次引导”。跨用户和损坏
  请求无法证明 owner 时 fail-closed。
- 设计对照：通道运行时 `src/status/status-text.ts` / `src/status/status-message.ts` 的确定性状态投影；会话运行时
  `会话运行时-rs/core/src/session/mod.rs` 的 typed interrupt/steer 与 expected turn 边界、
  `会话运行时-rs/tui/src/chatwidget/status_controls.rs` 的独立状态渲染。复用的是边界，不复制其上下文实现。

## 2026-07-13 普通飞书对话、工作与定时共用常规主链

- 普通最终回复、后台主动消息和显式 `send_message` 已收敛到同一 `DeliveryService`。可信
  `DeliveryContext` 持有 channel/target/reply_to，`ReplyEnvelope` 只持正文和 typed attachment；
  模型不能提供或覆盖收件人。`ChannelAdapterRegistry` 统一注册 adapter、capabilities 和 target
  validator，第二个 fake IM 契约证明接入无需修改投递主流程。
- Gateway response、飞书最终回复、后台自动续跑报告和 assistant transcript 共用
  `project_user_reply`：内部 `MAIN_AGENT/RUN/SUBAGENT` 协议只留在运行时，用户只看到简短正文；
  后台轮写 transcript 前先投影，防止机器协议进入后续 compact/旧聊天检索。公开 response 不包含
  服务器 path，owner transcript metadata 才保存最小产物引用。
- 主代理注册唯一通道无关 `send_message`。目标固定取当前 scoped owner 的真实 Feishu `open_id`，
  附件在副作用前核对 task artifact registry、owner 真实路径边界、ready 状态和 SHA-256，再走 adapter
  原生 `send_image/send_file`；发送回执持久化去重。同会话下一轮“发我”直接复用最近产物 path，
  不再搜索、复制或重新生成。用户可用 per-thread `/verbose on|full` 显式开启逐工具进度；更高层低频
  阶段汇报仍是后续项。
- 飞书适配器不再把 `user_id` 当会话：普通消息使用真实 `chat_id`，话题消息使用
  `chat_id:thread:<thread_id/root_id>`；该值从 adapter 一直传到 `/ask` 的结构化 conversation。
- 每轮执行前读取同一 owner、同一 channel conversation 的累计 user/assistant 历史；当前用户消息
  保持原文和最高当轮权威，回答后双方消息按 request/message ID 幂等写回。用户消息落账失败会在
  模型前拒绝；assistant 落账失败会先交付真实结果并进入持久化 repair，下轮幂等修复。
- 固定最近轮数不再是遗忘边界。Gateway 复用现有 runtime compact 阈值、token 估算和模型 backend：
  阈值前注入完整未 compact tail；到点后把较早段总结进同 thread 的 summary+cursor+generation，原始
  transcript 永不删除，保留近期 raw tail 后继续累计；message ID + byte cursor 让后续轮直接读取新增尾部，
  不随整份历史线性重扫。旧消息幂等投影到 owner-local LocalStore，
  `session_search` 可跨 compact 找回，但不同 owner 的索引物理分离。
- `/verbose off|on|full` 按 thread 持久化。工具循环把 typed progress 与 model delta 分栏写 chunk；
  `/progress/<request_id>` 复用 result owner 鉴权，delivery worker 按 cursor 回送且不重提任务。
  `full` 输出先做凭证脱敏、owner path 替换和长度限制。
- 删除“自动选择 active task 并把旧 goal 包住当前 follow-up”的默认行为。普通聊天即使同 thread
  有旧 active task，也不注入 goal/workspace；只有结构化内部 task ref 或显式特殊模式才续接。
- 普通“帮我做事/明早提醒我”不需要关键词和斜杠命令。模型仍在同一常规对话链上自然选择工具；
  `task_progress`、`create_subagents`、`wait` 真正执行时才把当前 run 绑定为后台任务。
- 同一会话准入上限固定为 1，确保第二条消息读取历史前第一条已落库；同一用户的不同会话仍可并行，
  每用户 8 / 全局 500 的原有公平上限不变。
- per-user owner 默认开启。远程 channel 身份缺失、owner home/agent 创建失败时返回
  `OWNER_SCOPE_UNAVAILABLE` 并终态归档，禁止回落到共享 main agent。
- adapter 现在把结构化 `channel_chat_type/channel_chat_id` 一并送入 Gateway：私聊按发送用户进入
  `users/<user_id>`，群聊按真实会话进入 `groups/<chat_id>`；同一群成员变化不会改 owner，群数据也
  不会落进首个发言人的私人目录。
- 远程 scoped agent 的 prompt、文件工具和 shell 共用 owner home 这一份工作区事实；文件写工具拒绝
  owner 外路径，shell 拒绝把公共 `service-cwd` 作为额外可写挂载，只有 delivery contract/write
  boundary 明确声明的外部输出根会临时加入本轮写入范围。
- root systemd 进程的宿主 home 放宽只用于无 owner scope 的本地管理员；Feishu 等远程 owner 保留
  `/root` dangerous-root 拒绝边界，只以精确 owner home 白名单读取自己的数据，避免宿主身份扩大租户权限。
- Feishu 默认长连接、私聊密码卡默认开启；SOUL/AGENTS 修改必须由发起人点击确认卡片，USER 偏好
  仍可由 Agent 直接维护。首次设置卡不吞首条消息。默认 prompt 使用包内 `builtin:` 资源，不受
  service cwd 影响。
- 普通对话 transcript 不再重复写 owner-global dialogue memory；历史 dialogue 在检索层先扩量后
  排除，避免把有效 preference/lesson 挤出 top-k。
- active task 只作为只读候选；模型用 `task_progress action=select` 结构化选择后，update/wait/子代理
  继承同一 task id/workspace。完整任务历史由 `task_ids` 保存，候选热索引由 `active_task_ids` 保存；
  结构化 closeout 完成后只从热索引移除，后台策略和审计仍能读取历史链接。子代理继承该引用只为
  归账，`subagent_*` run source 的完成块不能关闭父会话任务。最近完成项另作为有界、非默认候选；
  `subagent-*` 与 `bg-main-*` 内部链接不进入普通用户的 active/completed 候选，也不触发工作区选择；
  用户明确要继续/修改时模型必须在文件操作前结构化 select，原工作区才重新打开，误建的新任务链接
  标为 `superseded`。候选加载和 compact 加载各自报告错误，不会把残缺上下文伪装成正常空历史；后台
  主代理也携带精确 thread/task 引用，closeout 后不会继续唤醒已交付任务。同会话存在候选时，首次
  progress update 必须先结构化 `select` 或 `start`，普通用户无需特殊命令。
  select 后统一工具轮会把仍指向本轮占位目录的结构化路径改写到所选任务根，派工目标、输入输出引用
  和命令中的完整目录保持同一工作区，不会只切进度账本。
  子代理迟到完成时若其 root task 已非 active，wake 只归档不执行，避免 superseded/completed 旧任务
  回流污染当前聊天。
- Feishu adapter 提交后立即返回，持久化 delivery worker 负责长任务最终回送和重启恢复；scale worker
  也复用同一 gateway 对话主链，ASGI 卡片 action 不再进入普通消息队列。
- Feishu 主动发送和引用回复共用 8000 字符分片边界；长任务结果逐片引用原消息，所有分片都会尝试
  发送，任一失败则保留 delivery 失败态供既有重试链恢复。
- Gateway typed progress 与被认领的权威请求记录放在同一队列目录；per-owner Agent 只负责执行，不能
  把过程流写进自己的私有 Gateway 目录，否则 HTTP `/progress` 与 Feishu delivery worker 看不到。
- 长任务交付扫描不把 `node_modules`、`.venv/venv`、`_deps`、`site-packages`、`.tox`、测试/lint/Python cache 当用户产物，避免有界
  artifact 清单先被数千个依赖文件占满、真正代码和测试证据反而不可见；文件本身不会被删除。
- `/result/<request_id>` 的 USER 授权在所有状态都读取请求记录 owner：热请求读 pending/processing，
  完成请求读 done/failed 归档；响应正文不充当身份来源。归档缺失、损坏或多份身份不一致时默认拒绝，
  同一用户完成态可读且跨用户仍为 403。USER 的完成 JSON 另走字段白名单，默认不公开新增运行字段，
  且移除 request/chunk path、lease、prompt 和内部错误细节；可信管理员仍可读完整诊断记录。
- 参考核对：长期助手 用稳定会话重放；会话运行时 用统一 Regular 主链和结构化 tool call；claw 用持久
  per-user session。未照搬 会话运行时 goal 自动续跑、claw 群聊首发言人归属或双 persona 路径。
- 原生附件发送另核对 通道运行时 Feishu typed media dispatcher/outbound，以及 长期助手 的通用
  `send_message` + adapter native media；复用统一工具、结构化附件和当前通道 adapter 三个边界。
- 累计上下文另核对 通道运行时 stable sessionKey/per-session verbose/compaction handler，以及 长期助手
  stable gateway_session_key 贯穿 session、compression、memory provider 的作用域；只复用作用域传递，
  不复制它们的 compact 算法或 profile-wide memory 默认值。

## 2026-07-10 外部通道目标与日志边界加固

- 主动外呼在构建 adapter/发网络请求前按 channel 声明校验目标类型；Feishu `open_id` 只接受合法
  `ou_` 目标，无效目标返回 `CHANNEL_TARGET_INVALID`，不再让同一错误持续打到外部 API。
- `DeliveryReceipt` 提供结构化 delivery status/error code；adapter 不可用、明确发送失败和发送异常
  分别使用已注册恢复合同。相同 channel/target/error 的日志在进程内有界去重，日志只保留目标长度。
- service adapter 启动即安装统一日志脱敏；第三方 SDK 打出的 URL query、Bearer、secret assignment 等
  在进入 stderr/journald 前清理，避免 Feishu WebSocket ticket/access key 出现在运维日志。

## 2026-07-09 P0 lint 收敛

- `request_worker.py` 与 `gateway_process.py` 仅做 Ruff 的导入归属、排序和前向注解清理；gateway 入口、队列、lease、审批和执行语义没有变化。

## 2026-06-11 scoped lock 语义定性：进程级单例，非线程互斥（方案A）

- 全仓调用点排查实锤：`acquire_scoped_lock`/`release_scoped_lock` 生产代码零运行时调用
  （`daemon_control.py` 仅作公共 API 转口；`supervisor.py` 的 import 是死引用，已删；
  CLI/scripts/动态引用为零）；gateway 单进程多线程路径（heartbeat/request/background/
  worker 池）均未把它当临界区用，无存量数据竞争。
- 对照组核查（长期助手 ProcessRegistry）：进程内并发一律 `threading.Lock`，pid+start_time
  只做进程身份。据此定性"同进程线程重入=刷新心跳"是契约特性而非缺陷，采用方案A：
  文档化语义 + 模块注释禁止线程临界区用法；不给锁记录加 thread id（方案B 会破坏
  supervisor 重入刷新），也不新增无调用方的线程锁原语（线程互斥直接用
  `threading.Lock`，参考 `agent/io/jsonl.py` 双层锁先例）。
- 原 strict xfail 钉子（8 线程计数互斥）按正确语义改写为
  `test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex`：
  锁定同进程线程重入刷新、真实子进程抢锁必败、非持有进程 release 不误删、
  release 后干净重持有四条契约。`scoped_locks.py` 补齐 LLM/人类双层中文注释。

## 2026-06-10 空闲扫描门 + recover 节流 + 队列年龄观测

- 空闲 gateway 的每 0.2s 轮询不再做 inbox 全量 glob 和 processing 恢复扫描（mtime 门 + 节流）；
  请求拾取延迟上界不变（mtime 变化即扫描）。
- heartbeat 增加 queue_ages 结构化观测；jsonl 路径锁内存泄漏修复（引用计数回收）。
- chat 消息读取改尾部倒读（conversation/store.py `read_jsonl_tail_report`），5000 行账本取最近
  20 条实测 41x 提速、结果与全量读逐位一致。
- 评估后推迟：subagent tree 投影缓存（≤10 子代理的真实场景全量扫描仅数毫秒，待 R2 真实并发
  测出瓶颈再做，避免给可变 task 对象引入缓存别名风险）；lane 化并发同理待 R2 数据。

## 2026-06-09 Gateway ready、日志和后台提醒降噪

- `gateway start` 不再只等 PID 文件出现；启动确认会等当前 pid 对应的 `gateway_state.json`
  或 `gateway_heartbeat.json` 写入 `status=running`。子进程秒退或超时未 ready 时，start
  返回失败，避免 CLI 看到“pid 活着但 state 还是上轮 killed”。
- `status` 里的 `gateway.request_counts` 只表示当前 pending/processing 热队列；
  历史 done/failed/responses 进入 `gateway.archive_request_counts`。当前建议只看热队列，
  不会因为旧 failed 归档提示“现在要排查”。
- gateway run 会在 stdout/log 中写当前运行分隔线：
  `[gateway-run] status=starting ...`、`[gateway-run] status=running ...` 和停止 marker。
  `gateway logs` 优先从最近一次 starting marker 往后显示，因此旧 run 的
  `[gateway-background-main]` 行不会污染当前 tail。
- 同步 `gateway ask` 如果已经把流式正文打印给用户，最终 response 渲染只补结构化状态行，
  不再把同一段回复打印两次。`gateway result` 和 `--json` 仍按完整 response 输出。
- 后台主代理的 periodic progress policy 不做陈年追补：错过窗口太久的周期提醒只被 snooze
  到下一次，不唤醒模型；同一 thread/task/route 的重复 policy 同轮只跑一条，其余写入
  `progress_policy_suppressed` 诊断事实。这个降噪只基于 `ProgressPolicy` 和
  `ThreadTaskLink.status` 等结构化字段，不解析自然语言。

## 2026-06-09 活跃请求状态可观测

- CLI/gateway status 会显示 `requests/processing/` 中活跃 request 的结构化事实：
  `id`、`status`、`lease_owner`、`attempts`、lease age、heartbeat age、updated age，以及仍存在的
  `chunk_stream_path`。这只用于观察和排障，不改变调度、不新增硬门。
- processing request JSON 损坏时会显示 `gateway processing_load_error=...`，避免慢请求、坏记录或
  worker owner 丢失时只看到队列计数。

## 2026-06-07 本地轮询降噪和入口唯一化

- `gateway_process.py` 统一导入 `cli/gateway_loops.py`，删除旧的 `_gateway_process_service.py` 副本，
  避免 gateway worker loop 两份实现漂移。
- gateway 命令实现并回 `gateway_process.py`，删除 `_gateway_commands.py` 私有转发层；测试也改为 patch
  公开入口，避免真实入口和测试入口分裂。
- gateway run 状态、线程装配、恢复记录和 stop/kill helper 都并回 `gateway_process.py`；
  gateway 命令主链路不再绕同目录私有 helper。
- systemd/launchd service unit 生成和安装/卸载收回 `gateway_service.py` 一个公开边界文件，删除
  `_gateway_service_handlers.py` / `_gateway_service_unit_gen.py` 私有 facade helper。
- `agent/gateway_parts/supervisor.py` 保持一个直接 supervisor 边界文件；启动、停止、重启、
  heartbeat 健康判断和 runtime status 写入在同一文件内组织，不再保留中段 facade 注释或重复模块
  import 块。
- 进程控制入口收敛到 `agent/gateway_parts/process_control.py`；`daemon_control.py`
  不再顺手转出口 `is_pid_alive` / `wait_for_pid_exit`，只保留 PID record、后台化、
  scoped lock 和 shutdown request。
- chat/TUI 和 `gateway ask` 的响应等待改为按响应文件 `(mtime,size)` 状态读取：
  响应文件不存在或未变化时不反复 JSON 解析，出现或变化时仍走统一 load_error 结构化报告。
- `gateway_request_poll_interval` 改为小数秒配置，默认 `0.2`；后台 request worker 空闲时更快发现新请求，
  但仍保留最小 `0.05s` 防止误配造成空转。
- 流式 chunk 不再长期留在 `requests/processing/`：request 完成时随 request 一起归档到
  `requests/done/` 或 `requests/failed/`，response 记录 `chunk_stream_path`；
  chat/TUI 和 `gateway ask` 会从 processing/done/failed 候选路径补读，避免 response
  先出现时丢最后一段流式输出，也避免 processing 被历史 chunk 污染。
- `submit_gateway_ask()` 统一使用 gateway UUID 请求 ID 生成器，不再用毫秒时间戳拼 `gw-*`。
  多个 chat/TUI/gateway client 同一毫秒提交时，请求文件不能互相覆盖。
- 普通 CLI `gateway ask` 不再提交孤立 request：默认带 `gateway-cli/default` conversation；
  HTTP `/ask` 也会从结构化会话字段生成 conversation。worker 执行 follow-up 时可直接注入
  active task root/output/work，避免续接请求新建空任务目录后反复找不到上一轮产物。

## 2026-06-06 入口收敛

- 删除 `agent/gateway_parts/runtime.py` 聚合层。
- adapter 直接调用 `request_worker`，processing 恢复直接调用 `lease_service`。
- `agent/gateway_parts/__init__.py` 仍是包级公开入口；内部实现不再通过 `runtime.py` 多跳转发。
- CLI/gateway/TUI 状态行的当前上下文 token 展示归 `response_renderer.py`，不再通过单函数
  `context_tokens.py` 跳转。

## 2026-06-06 会话上下文精确化

- gateway conversation follow-up 只把 `status=active` 的 task link 当作活跃任务。
- `completed`、`done`、`closed` 等旧/展示状态不会再通过“非关闭即活跃”的逻辑污染后续请求。

## 2026-06-04 收敛

- gateway 文档改为只描述当前 `gateway_parts/` 队列、worker、lease、HTTP 和 renderer。
- request/response/history/status/PID 读取错误必须结构化显示，不能吞成空状态。
- gateway heartbeat 和 HTTP `/status` 只统计 pending/processing 活跃队列；status/doctor
  等显式诊断命令才读取 done/failed/responses 归档计数，避免历史响应目录变大后拖慢本地热路径。
- 后台启动失败需要早期健康确认并写明确失败状态。
- chat/gateway 多客户端共享队列时，应减少本地膨胀和重复读写，避免本地成为模型之外的瓶颈。
