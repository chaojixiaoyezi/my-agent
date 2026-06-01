## 2026-05-06 structure update
- 中文说明：adapter CLI 只负责启动、状态、停止和生命周期编排；真正的 inbox/outbox 文件协议仍在 `gateway_parts/adapter.py`。这能避免命令行入口继续变厚。
- `cli/adapter.py` now keeps command entrypoints small and delegates file-loop, daemon, registration, status, and stop concerns to local helper functions.
- `gateway_parts/adapter.py` still owns the inbox/outbox file protocol; CLI helpers only orchestrate startup and process lifecycle.
# Gateway：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- gateway.py                         # 兼容入口，重新导出 gateway_parts 的主要能力
`-- gateway_parts/
    |-- paths.py                       # gateway / adapter 的目录和文件路径模型
    |-- io.py                          # JSON 文件读写、请求文件流转
    |-- process_control.py             # pid、heartbeat、stop request、进程启动停止
    |-- logging.py                     # gateway 日志和 LocalStore 事件镜像
    |-- recovery.py                    # processing 请求恢复、失败归档
    |-- runtime.py                     # 请求 worker、agent.run 调用、响应写回
    |-- http_service.py                # 可选本机 HTTP 控制服务
    |-- http_handlers.py               # HTTP 控制面 handler
    |-- supervisor.py                  # gateway / adapter 健康检查和重启监督
    `-- adapter.py                     # inbox/outbox 文件 adapter

agent_py_agent/cli/
|-- gateway_process.py                 # gateway start/status/stop/restart/logs/run
|-- gateway_client.py                  # gateway ask/result/default 客户端入口
`-- adapter.py                         # 文件 adapter CLI
```

## 核心文件

- `gateway_parts/paths.py`：集中描述所有 gateway 文件路径，避免路径散落各处。
- `gateway_parts/io.py`：负责请求 JSON 的读写和目录迁移；`update_json_file_atomic()` 在同一文件锁内完成读-改-写，避免并发后台 tick 丢更新。
- `gateway_parts/process_control.py`：负责后台进程生命周期。
- `gateway_parts/recovery.py`：处理卡在 processing 的请求。
- `gateway_parts/runtime.py`：真正执行 request worker，从 pending 取请求、调用 agent、写 response。
- `cli/gateway_process.py`：用户管理后台进程的命令。
- `cli/gateway_client.py`：用户或 chat 客户端投递消息和读取结果的命令；默认入口也在这里把 `my-agent --app` 映射为 gateway chat + 应用内滚动历史 UI。
- `cli/models.py`：承接 gateway/adapter CLI 的 bundle 数据结构，例如 `GatewayRunOptions`、`GatewayRunContext`、`GatewayThreadsRequest`、`GatewayRunCleanupRequest` 和 `AdapterOptions`；cmd 层解析 `argparse args` 后再传给 helper。
- `cli/adapter.py`、`cli/_gateway_state_helpers.py`、`cli/gateway_loops.py`：运行期 helper 接收 options/context bundle，不再把 argparse namespace 深传到线程和内部 helper。

## 数据流

1. 用户运行 `gateway ask` 或 `chat --gateway`，客户端把请求写入 `requests/pending`。
2. 后台 gateway worker 抢占 pending 请求，移动到 `requests/processing`。
3. worker 调用 `SimpleAgent.run()` 或相应处理逻辑。
4. 成功后写 response 文件，并把请求归档到 done。
5. 失败或超时后，根据 attempts 和 lease 退回 pending 或归档 failed。
6. status/local-doctor/timeline 从 gateway state、heartbeat、history 和 LocalStore 读取可观察状态。
7. `memory-resume` 或自动恢复命中 gateway_request 时，会把 request/response JSON 作为事实源推荐阅读。
8. gateway/adapter CLI 在进入运行 helper 前会先构造 options/context bundle；线程启动、cleanup、watch 和 adapter loop 从 bundle 读取字段，避免新增 CLI 参数时污染内部协议。

### runtime fact 恢复事实源

gateway 保存型请求现在跟普通主代理 run 使用同一套恢复事实源：

```text
request_id=<id>
  -> SimpleAgent.run(save=True, source=gateway)
  -> memory_archive/runtime_facts/<id>/task.json
```

response JSON 只保留响应、模型、token、resume context 等运行结果，不再写 `recovery_snapshot_*` 空字段。后续 compact/resume 要恢复 gateway 请求时，先读 request/response JSON 和对应 `runtime_facts/<id>/task.json`，raw archive 仍作为黑匣子补充。

### 多 worker 抢占

gateway request worker pool 通过文件 rename 抢占 pending 请求，而不是靠共享内存锁：

```text
worker-0 sees pending/a.json
worker-1 sees pending/a.json

one worker wins:
  pending/a.json -> processing/a.json

the other worker gets an OSError and skips that file
```

每个 worker 有独立 `SimpleAgent` 实例。`scenario-test --case gateway-multi-worker` 会同时启动两个 worker，投递多条 pending 请求，并验证每条请求只产生一个 response 和一个 done archive。

### 迟到 response 去重

如果 response 文件已经存在，但同一个 request 的副本稍后才出现在 pending，worker 会把请求移到 processing 后先检查 response：

```text
pending/<id>.json
  -> move to processing/<id>.json
  -> responses/<id>.json already exists
  -> do not call agent.run()
  -> archive request to done
```

`scenario-test --case gateway-delayed-response` 会把 `agent.run()` 改成“被调用就失败”的测试桩，确保这个分支不会重复调用模型。

### processing lease 降级

worker 抢占 pending 后会尝试写 processing lease，方便 local-doctor 判断请求是否卡住。这个 lease 是观测层，不是任务本体：

```text
pending/<id>.json
  -> move to processing/<id>.json
  -> try write lease heartbeat
  -> if lease write fails, continue agent.run without lease refresh
  -> write responses/<id>.json
  -> archive request to done/failed
```

这样 Windows 深路径、临时文件写入失败或监控层抖动不会把用户请求卡死在 processing。

lease heartbeat 间隔和 processing timeout 来自 `AgentConfig`。如果配置非法，lease helper 只回退到配置 schema 默认；不要在 `gateway_parts/lease.py` 或 `gateway_parts/lease_service.py` 里再写一套隐藏数字。

这层 schema 默认只允许通过 `settings/defaults.py` 读取。gateway lease 模块本身不再裸建 `AgentConfig()`，这样配置权威只剩“运行时 agent.config 优先，schema 默认兜底”一条线。

### stale lease 恢复

如果 worker 中断，`processing/<id>.json` 里的 `lease_heartbeat_at` 会停住。恢复逻辑按 lease 新鲜度判断下一步：

```text
fresh lease
  -> 保持 processing，不抢正在工作的请求

stale lease 且 attempts < max_attempts
  -> 写 last_error / requeued_at
  -> move back to pending
  -> 由活跃 worker 重新处理

stale lease 且 attempts >= max_attempts
  -> 写 responses/<id>.json 失败响应
  -> archive request to failed
```

`scenario-test --case gateway-stale-lease` 是这个流程的可观察入口。

## 跨天恢复

gateway 的跨天恢复和 subagent 恢复遵循同一个原则：LocalStore 只负责帮忙找到 request_id，真正要读的是 gateway 文件事实源。

```text
gateway request archive/raw clue
  -> request_id / prompt / status

LocalStore gateway_request
  -> 可搜索索引，metadata 里保留 request_path / response_path

gateway/requests/.../<request_id>.json
gateway/responses/<request_id>.json
  -> 请求和响应事实源

memory-resume 或 run(auto resume)
  -> Recovery Brief 推荐读取 request/response JSON
```

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/gateway_client.py`，理解用户如何提交一个请求。
2. 再看 `gateway_parts/paths.py`，理解 gateway 用哪些目录表达状态。
3. 再看 `gateway_parts/io.py`，学习请求文件怎么从 pending 移到 processing/done。
4. 再看 `gateway_parts/runtime.py`，理解 worker 如何处理请求并写响应。
5. 再看 `gateway_parts/recovery.py`，理解程序崩溃后怎么恢复。
6. 再看 `agent_py_agent/tests/test_memory_archive_cli.py::test_memory_resume_cross_day_gateway_request_uses_response_fact_source`，理解 gateway 请求如何进入跨天恢复。
7. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py`，理解真实后台 gateway 进程、多 worker、迟到 response 和 stale lease 如何被场景测试启动、投递、恢复和验收。
8. 再看 `agent_py_agent/agent/gateway_parts/process_control.py`，理解 Windows process handle 和 macOS/Linux POSIX signal 探测如何统一成 `is_pid_alive()`。
9. 最后看 `agent_py_agent/tests/test_gateway_client.py`，理解怎样证明协议边界和失败降级没坏。

## 当前第一版索引 / 待补齐

本页先描述单机文件协议。后续应补充真实目录样例、请求 JSON schema、response JSON schema、失败恢复时序图和 gateway chat 的用户路径。
## 2026-05-06 structure update
- 中文说明：gateway request worker 现在只管队列认领和归档流转，单个请求的执行、chunk streaming、lease 刷新和响应完成拆到 `request_execution.py`。Windows 仍走带锁和重试 replace 的文件 IO，macOS/Linux 保持原子替换路径。
- `gateway_parts/request_worker.py` now owns queue claiming and archive flow, while `gateway_parts/request_execution.py` owns one-request execution, chunk streaming, lease refresh, and response completion.
- Gateway file IO keeps per-path process-local locks and retrying replace logic for Windows; POSIX behavior remains the normal atomic replace path.

## 2026-05-07 bundle structure update
- 中文说明：gateway 文件队列协议没变，但 helper 入口改成 typed bundle 或明确字段，避免用开放式 kwargs 承载产品行为。日志、队列合并、supervisor、adapter、audit、HTTP 启动和 recovery 都按小上下文拆边界。
- Gateway execution continues to use file request/response facts, but option-heavy service helpers now expose typed bundles or explicit fields before they touch persistence.
- `gateway_parts/logging.py`, `queue_service.py`, and `supervisor.py` are part of the bundle sweep; they no longer rely on open-ended keyword option bags for product behavior.
- `gateway_parts/adapter.py`, `audit_service.py`, `http_service.py`, `logging.py`, `recovery.py`, and `request_execution.py` keep protocol fields in small context records so queue IO, audit indexing, HTTP boot, and stale-request recovery can evolve independently.

## 2026-05-07 request execution size update
- 中文说明：请求执行状态被收进小 context，完成审计也拆成 helper，所以 gateway request-execution 这条路径不再有高风险体积项。
- `gateway_parts/request_execution.py` keeps request execution state in small context records and delegates completion audit into a helper, leaving no high-risk code-size entry in the gateway request-execution slice.

## 2026-05-07 annotation structure update
- 中文说明：结构文档把代码里的双层注释也当成架构一部分。新增文件、服务、bundle 或 facade 方法时，需要同时更新结构页和代码注释，避免 LLM/人类读到旧契约。
- Module structure docs now treat the definition-level double-layer comments as part of the code architecture: `LLM:` records model-facing contract/caller/side-effect notes, and `函数用途:` / `类用途:` records beginner-readable purpose and edit guidance.
- New files, services, bundles, or facade methods must update both this structure page and the in-code comments at the same time.
- The global file tree in `CODEBASE_TREE.md` now includes a current architecture map for CLI, agent core, gateway, memory, log-analysis, subagent, tooling, and settings boundaries.

## 2026-05-13 backend config structure update
- 中文说明：gateway 相关默认等待、join timeout、service command timeout 进入 `AgentConfig`，由 `agent_config.yaml` 作为真实后端配置源。
- CLI 层应在 `make_agent()` 后解析这些默认值；不要在 `gateway_client.py`、`chat.py` 或 gateway process helper 里再写一套独立数字策略。
- 结构边界不变：gateway 文件队列协议、request/response JSON 和 worker 认领流程没有因为配置抽取改变。
