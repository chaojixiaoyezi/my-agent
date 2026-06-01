## 2026-05-06 adapter code-size cleanup
- 中文说明：这一段是 adapter 入口拆分记录。它把 `cli/adapter.py` 里的文件循环、gateway 可用性检查、daemon 启动、前台注册、状态和停止流程拆成小 helper；用户命令和文件协议路径保持不变。
- Split `cli/adapter.py` file-adapter loop, gateway availability checks, daemon startup, foreground registration, status, and stop flows into focused helpers.
- Daemon flags remain cross-platform: Windows uses process-group/no-window flags, macOS/Linux keep the POSIX new-session path.
- User-visible adapter CLI behavior and file protocol paths stay stable.
# Gateway：开发推进记录

## 已完成

- 2026-05-29 Gateway request 恢复事实源已并入 `runtime_fact`：worker 调用 `SimpleAgent.run()` 时不再要求主运行链路写 `recovery_snapshot`，响应 JSON 也不再暴露空的 recovery snapshot 字段；保存型请求会写 `memory_archive/runtime_facts/<request_id>/task.json`，供 compact/resume 和人工排查读取。
- 2026-06-02 Gateway lease 配置默认入口收敛：`gateway_parts/lease.py` 和 `lease_service.py` 仍从运行时 `agent.config` 读取 heartbeat/processing timeout；非法值只回退到 `settings/defaults.py` 的 schema 默认入口，不再由 lease 模块各自裸建 `AgentConfig()` 或隐藏本地数字。
- 2026-05-27 Gateway lease 时间预算已回到主配置：processing lease heartbeat 间隔和 processing timeout 只从 `agent_config.yaml` / `AgentConfig` 读取，lease 模块不再写第二份 5 秒 / 900 秒本地默认。
- 2026-05-26 Gateway JSON 文件锁已落地：`gateway_parts/io.py` 的 JSON read/write 和 read-modify-write 更新现在共用进程内锁 + 平台文件锁，新增 `update_json_file_atomic()`，用于协作/会话等并发 tick 避免 last-write-wins 覆盖。
- `agent/gateway.py` 已作为兼容入口，真实协议实现拆到 `gateway_parts/`。
- gateway 控制面已有 pid、state、heartbeat、stop request、log 等文件。
- request 队列已有 pending、processing、done、failed、responses 目录语义。
- CLI 已有 gateway process/client 相关命令，包括 start/status/stop/restart/logs、ask/result、run。
- processing 请求恢复、LocalStore 索引重建、adapter file 协议已有基础实现和测试记录。
- gateway 运行时已有可选本机 HTTP 控制服务骨架；主协议和恢复事实源仍是本地文件队列。
- gateway 请求跨天恢复已通过 memory resume 链路验证：request/response JSON 会作为事实源进入恢复推荐路径。
- `scenario-test --case gateway-cross-day-resume` 已能启动真实后台 gateway、投递 ask、模拟跨天恢复线索，并验证 `memory-resume` 能把终态 request/response JSON 找回来。
- gateway request worker 在 processing lease 写入失败时会降级继续处理请求，避免 Windows 深路径或临时文件失败把请求卡死在 processing。
- `scenario-test --case gateway-stale-lease` 已能模拟 worker 中断留下旧 processing lease，验证恢复会重排到 pending，并由活跃 worker 完成请求。
- `scenario-test --case gateway-multi-worker` 已能启动两个并发 request worker，验证多条 pending 请求只会各自完成一次，不重复响应或归档。
- `scenario-test --case gateway-delayed-response` 已能模拟 response 先到、pending 请求副本迟到，验证 worker 不重复调用模型，只把请求归档到 done。
- **Round 5 Gateway 常驻稳定性**已实现：
  - 长期助手 风格 PID 记录（start_time tracking + scoped locks）
  - Watchdog Supervisor 自动监控并重启崩溃 gateway
  - Adapter 守护进程模式（`--daemon` + PID 文件）
  - `gateway start-all --adapter` 一键启动 gateway + 适配器
- 系统服务安装（`gateway install` systemd/launchd）
- 2026-05-07 Gateway / Adapter CLI bundle 边界已落地：gateway run/start helper、gateway worker thread 请求、adapter file/start/status/stop helper 先把 `argparse args` 收敛成 `GatewayRunOptions` / `GatewayRunContext` / `AdapterOptions` 等 bundle；`args` 不再深传到这些 helper。

## 解决的问题

- 把后台进程、请求队列、响应文件、恢复逻辑从巨大 CLI 入口中拆出来，降低维护风险。
- 前台 chat 可以作为 gateway 客户端，向后台投递普通消息。
- 默认入口新增 `my-agent --app`，复用 gateway client 路径并进入应用内滚动历史 UI；普通 `my-agent`/`chat --gateway` 仍保留终端滚动历史体验。
- gateway 崩溃遗留的 processing 请求不再只能人工猜状态，可以按 attempts 和超时退回或归档。
- LocalStore 能看到 gateway request 和生命周期事件，方便 status/timeline/local-doctor 统一观察。
- gateway request 的 LocalStore 命中现在不再只是“可搜索摘要”，还能把 request/response JSON 带回 `memory-resume` 和自动恢复上下文。
- 真实后台进程演练解决了“fixture 证明恢复可行，但未证明 gateway start/ask/worker/response 真能贯通”的问题。
- lease 降级解决了“监控心跳文件写失败会放大成用户请求失败”的问题；lease 是可观测性，不应比请求本身更重要。
- stale lease 场景解决了“只在单元测试里证明旧 processing 可恢复，缺少可观察 scenario 入口”的问题。
- multi-worker 场景解决了“配置已有 worker pool，但缺少并发抢占不重复的可观察验证”的问题。
- delayed-response 场景解决了”响应已经落盘但队列里还有迟到请求副本时，可能重复执行模型”的回归风险。
- PID tracking 解决了”旧 PID 可能被系统复用导致误判进程存活”的问题。
- scoped locks 解决了”多实例同时启动导致文件冲突”的问题。
- Windows 进程存活探测已统一走 gateway process-control helper：Windows 使用 Win32 process handle，macOS/Linux 保留 `os.kill(pid, 0)` 的 POSIX 路径。
- supervisor 解决了”gateway 崩溃后无人重启”的问题。
- adapter daemon 解决了”适配器需要前台运行，无法后台常驻”的问题。
- 系统服务解决了”需要手动启动/停止，无法随系统自动启动”的问题。
- Gateway / Adapter bundle 边界解决了“命令行 args 被 helper/thread 深传，后续新增字段容易散落”的问题；现在 cmd 层负责解析，运行 helper 接明确的 context/options。

## 下一步

- 给 gateway 核心函数补齐和 LOG work-order 同级别的 `LLM:` / `新手说明:` / 参数说明。
- 把更多 gateway 场景加入隔离测试：取消/优先级、HTTP 控制面、长时间 adapter watch。
- 稳定默认入口体验，让普通 `my-agent` 更自然地确保 gateway 存活并进入 gateway chat。
- 如果 gateway 协议路径或数据流变化，同步更新本文件和 `04-structure.md`。

## 已跑测试

- 历史记录显示 gateway client、scenario-test gateway-restart、local-doctor、gateway ask/result/chat 相关路径已有测试或手工验收记录。
- 相关记录见 [TESTS.md](../../../TESTS.md)、[ACCEPTANCE.md](../../../ACCEPTANCE.md)、[EVIDENCE.md](../../../EVIDENCE.md)。
- 同步门 focused 验收：`python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`。
- 同步门手工检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 父会话全量回归：`python -m pytest` -> `241 passed`。
- 空白检查：`git diff --check` -> passed。
- gateway 跨天恢复 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_memory_runtime.py` -> `16 passed`。
- gateway 跨天恢复宽 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_doc_sync.py` -> `72 passed`。
- gateway 跨天恢复全量回归：`python -m pytest` -> `247 passed`。
- 真实 gateway 跨天恢复场景 focused 验收：`python -m pytest agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py` -> `16 passed`。
- 真实 gateway 跨天恢复宽 focused 验收：`python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_gateway_client.py agent_py_agent\tests\test_scenario_gateway_resume.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_doc_sync.py` -> `76 passed`。
- 真实 gateway 跨天恢复全量回归：`python -m pytest` -> `250 passed`。
- gateway stale lease scenario focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py -q` -> `3 passed`。
- 本轮 gateway/scenario/doc focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py agent_py_agent/tests/test_cli_reference.py agent_py_agent/tests/test_doc_sync.py -q` -> `9 passed`。
- 本轮同步门验收：`python3 scripts/check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python3 -m pytest -q` -> `252 passed`。
- gateway multi-worker scenario focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py -q` -> `4 passed`。
- gateway multi-worker focused 组合验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py agent_py_agent/tests/test_cli_reference.py agent_py_agent/tests/test_doc_sync.py -q` -> `10 passed`。
- gateway multi-worker 全量回归：`python3 -m pytest -q` -> `253 passed`。
- gateway delayed-response scenario focused 验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py -q` -> `5 passed`。
- gateway delayed-response focused 组合验收：`python3 -m pytest agent_py_agent/tests/test_scenario_gateway_resume.py agent_py_agent/tests/test_cli_reference.py agent_py_agent/tests/test_doc_sync.py -q` -> `11 passed`。
- gateway delayed-response 全量回归：`python3 -m pytest -q` -> `254 passed`。

## 未跑测试

- 暂未做多 gateway 组织、跨机器通信或长时间真实跨午夜等待；当前跨天通过固定 archive 时间模拟。

## 风险

- gateway 连接 CLI、后台进程、文件协议、LocalStore、chat、adapter，变化面大，文档很容易再次散开。
- 文件队列并发写入需要持续测试，否则 worker pool 扩大后容易出现重复处理或状态覆盖。
- 普通用户入口和开发者命令层级需要继续打磨，避免体验被内部协议细节淹没。

## 2026-05-13 backend config extraction
- 中文说明：gateway/chat 入口的默认等待和默认 memory 展示数量继续收回后端配置；`cmd_default` 不再写死 chat memory limit，而是交给 `cmd_chat` 从 `AgentConfig` 解析。
- `gateway_ready_timeout_seconds` 现在控制前台 chat/gateway client 等待 gateway 存活确认的默认时间；显式 CLI timeout 仍优先。
- worker join、service command/stop timeout 也已在 `agent_config.yaml` 定义为后端配置字段，后续继续把剩余 service wrapper 调用切到这些字段。
## 2026-05-06 code-size cleanup
- 中文说明：这一轮把 gateway 请求执行、IO、lease、恢复、supervisor 和 adapter helper 继续拆薄。用户可见行为不变，主要是降低 gateway 大文件继续膨胀和并发路径难调试的风险。
- Split gateway request execution out of the request worker and flattened gateway IO, lease, recovery, supervisor, and adapter helpers.
- Strengthened request ID generation by keeping the full UUID suffix, eliminating stress-test collisions seen with the previous short suffix.
- Preserved macOS/POSIX process handling while keeping Windows-native locking and file replacement safeguards.

## 2026-05-07 bundle interface completion
- 中文说明：这一轮把 Gateway / Adapter 的接口继续收敛成明确的 options/context bundle，避免 `args` 或散参数一路传到深层 helper。以后新增字段时，优先扩展 dataclass 上下文，不要把函数签名越拉越长。
- Gateway logging, queue response merge, and supervisor control options were aligned with explicit option records/fields instead of product-level var-keyword service signatures.
- Gateway helper calls remain stable for CLI/scenario callers while watch/daemon/gateway paths continue to enter core through `WatchParams` and context bundles.
- Focused verification covered gateway helper and gateway logging tests.
- 2026-05-07 high-risk cleanup continued across adapter, audit, HTTP startup, logging, recovery, and request execution helpers; new fields stay behind focused dataclass contexts rather than widening gateway request handling.
## 2026-05-07 LLM annotation coverage update
- 中文说明：这一轮是 gateway 模块的注释同步，不改变协议、文件格式、工作流语义和公开接口。以后改模块/类/函数行为、bundle 字段或副作用，要同步维护这些注释。
- Product-code modules, classes, functions, and methods in the active module now carry the required `LLM:` plus `函数用途:` / `类用途:` definition-level double-layer comments format.
- This is a documentation-only maintainability pass: behavior, file formats, workflow semantics, and public interfaces are intended to stay unchanged.
- Future module changes must keep these comments current when changing module/class/def behavior, side effects, bundles, or caller expectations.
