# Gateway Progress

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
