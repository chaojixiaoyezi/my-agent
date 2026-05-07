## 2026-05-06 structure update
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
- `gateway_parts/io.py`：负责请求 JSON 的读写和目录迁移。
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
- `gateway_parts/request_worker.py` now owns queue claiming and archive flow, while `gateway_parts/request_execution.py` owns one-request execution, chunk streaming, lease refresh, and response completion.
- Gateway file IO keeps per-path process-local locks and retrying replace logic for Windows; POSIX behavior remains the normal atomic replace path.

## 2026-05-07 bundle structure update
- Gateway execution continues to use file request/response facts, but option-heavy service helpers now expose typed bundles or explicit fields before they touch persistence.
- `gateway_parts/logging.py`, `queue_service.py`, and `supervisor.py` are part of the bundle sweep; they no longer rely on open-ended keyword option bags for product behavior.
