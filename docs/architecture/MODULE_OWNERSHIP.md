# Module Ownership Table
# 模块归属表：每个模块的唯一职责、禁止范围、大小状态

LLM: Use this table to decide where new code belongs.
给人看的解释：模块归属表让后续重构有路线，不需要每次靠猜。新增代码时必须查此表确认归属。

---

## 1. CLI Layer / CLI 层 (`cli/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `cli/main_entry.py` | argparse 顶层入口，分发到子命令 | 业务逻辑、配置加载 | 28 | OK |
| `cli/parser.py` | 根解析器，委托给 commands/ | 命令执行逻辑 | 71 | OK |
| `cli/parser_subcommands.py` | 子命令注册辅助 | 命令执行 | 38 | OK |
| `cli/chat.py` | 交互式聊天主循环 | 已部分拆分到 chat_parts/ | 1017 | FROZEN |
| `cli/chat_parts/history.py` | 会话历史管理（MAX_HISTORY_TURNS, append/build） | 模型调用、渲染 | 48 | OK |
| `cli/chat_parts/rendering.py` | 终端渲染（颜色、进度条、banner、折叠） | 业务逻辑、会话管理 | 64 | OK |
| `cli/chat_parts/slash_commands.py` | 公共斜杠命令处理 | 业务执行、渲染 | 89 | OK |
| `cli/commands/core.py` | 注册 /chat, /memory, /status 等核心命令 | 命令执行细节 | 21 | OK |
| `cli/commands/gateway.py` | 注册 /gateway 子命令 | gateway 业务逻辑 | 25 | OK |
| `cli/commands/subagents.py` | 注册 /subagent 子命令 | 子代理业务逻辑 | 12 | OK |
| `cli/commands/tasks.py` | 注册 /task 子命令 | 任务业务逻辑 | 48 | OK |
| `cli/commands/learning.py` | 注册 /learning 子命令 | 学习逻辑 | 34 | OK |
| `cli/commands/bench.py` | 注册 /bench 子命令 | 基准测试逻辑 | 21 | OK |
| `cli/commands/operations.py` | 注册 /ops 子命令 | 运维逻辑 | 37 | OK |
| `cli/gateway_client.py` | gateway HTTP 客户端封装 | gateway 服务端逻辑 | 215 | OK |
| `cli/gateway_service.py` | gateway 本地服务封装 | HTTP 处理 | 547 | SOFT |
| `cli/gateway_process.py` | gateway 进程管理 CLI | 进程监管逻辑 | 539 | SOFT |
| `cli/gateway_loops.py` | gateway 交互循环 | 模型调用 | 134 | OK |
| `cli/local_commands.py` | /memory, /status, /doctor 本地命令实现 | 持久化逻辑 | 336 | OK |
| `cli/memory_commands.py` | 记忆管理命令实现 | 记忆存储逻辑 | 608 | SOFT |
| `cli/memory_archive_commands.py` | 归档命令实现 | 归档逻辑 | 312 | OK |
| `cli/memory_doctor.py` | 记忆诊断命令 | 存储操作 | 346 | OK |
| `cli/subagents.py` | 子代理 CLI 命令实现 | 子代理管理逻辑 | 525 | SOFT |
| `cli/adapter.py` | 适配器 CLI 命令 | 适配器业务逻辑 | 355 | OK |
| `cli/daemon.py` | 守护进程 CLI | 进程管理逻辑 | 166 | OK |
| `cli/logs.py` | 日志查看 CLI | 日志分析逻辑 | 229 | OK |
| `cli/scenario.py` | 场景测试框架入口 | 测试执行 | 236 | OK |
| `cli/supervisor.py` | 监管 CLI 命令 | 监管逻辑 | 232 | OK |
| `cli/task_commands.py` | 任务命令实现 | 任务管理逻辑 | 160 | OK |
| `cli/thinking_spinner.py` | 思考动画 | 模型交互 | 114 | OK |
| `cli/thinking_phrases.py` | 思考短语 | 无 | 193 | OK |
| `cli/common.py` | CLI 公共工具（make_agent, resume_context） | 业务逻辑 | 125 | OK |
| `cli/models.py` | CLI 数据模型（ChatJob） | 业务逻辑 | 37 | OK |

**CLI 层总行数**: ~8,643 行
**归属原则**: CLI 只负责解析命令参数、调用 agent 层的公开 API、格式化输出。不得直接操作文件系统或数据库。

---

## 2. Agent Core Layer / 代理核心层 (`agent/agent_core/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `agent_core/runtime_mixin.py` | 主代理模型/工具执行循环（run 方法） | 子代理调度、CLI 渲染 | 350+ | OK |
| `agent_core/dispatch_mixin.py` | 父代理调度主循环（due-check, 派工, 验收） | 模型调用、CLI 输出 | 895 | FROZEN |
| `agent_core/subagent_mixin.py` | 子代理相关入口 mixin | 具体子代理管理 | 200+ | OK |
| `agent_core/failure_introspector.py` | 失败自省引擎（LLM 分析失败原因） | 调度决策 | 150+ | OK |
| `agent_core/failure_analyzer.py` | 失败模式分析 | 自省 | 100+ | OK |
| `agent_core/planner.py` | 调度规划（runner 指令组合） | 执行 | 150+ | OK |
| `agent_core/runner_dispatch.py` | runner 分发（并发控制、重试、超时） | 调度规划 | 200+ | OK |
| `agent_core/runner_prompts.py` | runner 提示词模板 | 无 | 100+ | OK |
| `agent_core/dispatch_lock.py` | 调度锁 | 无 | 50+ | OK |
| `agent_core/dispatch_loop.py` | 调度循环 | 无 | 100+ | OK |
| `agent_core/dynamic_timeout.py` | 动态超时计算 | 无 | 80+ | OK |
| `agent_core/adaptive_retry.py` | 自适应重试 | 无 | 80+ | OK |
| `agent_core/task_complexity.py` | 任务复杂度评估 | 无 | 100+ | OK |
| `agent_core/watchdog.py` | 看门狗（daemon 巡检） | 无 | 80+ | OK |
| `agent_core/orchestration_tools.py` | 编排工具 | 无 | 100+ | OK |
| `agent_core/parameters.py` | 调度参数 | 无 | 80+ | OK |
| `agent_core/runtime_capabilities.py` | 运行时能力解析 | 无 | 80+ | OK |
| `agent_core/models.py` | 核心数据模型 | 无 | 100+ | OK |

**归属原则**: agent_core 负责主代理的核心调度循环和运行时执行。不负责 CLI 渲染、不负责具体的子代理状态管理（委托给 subagents/）。

---

## 3. Subagents Layer / 子代理层 (`agent/subagents/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `manager.py` | SubAgentManager 入口（纯组合类） | 业务逻辑 | 49 | OK |
| `manager_base.py` | 基础 CRUD + 卡片管理 | patch 审核、看板渲染 | 751 | FROZEN |
| `manager_patch.py` | patch 审核 + 应用 | 基础 CRUD、看板 | 794 | FROZEN |
| `manager_dispatch.py` | 调度派工逻辑 | 验收、patch | 423 | SOFT |
| `manager_board.py` | 看板渲染（HTML + 终端） | 调度、验收 | 471 | SOFT |
| `manager_acceptance.py` | 验收流程 | 调度、patch | 293 | OK |
| `manager_acceptance_findings.py` | 验收发现处理 | 验收主流程 | 390 | OK |
| `manager_runner_context.py` | runner 上下文注入 | runner 结果处理 | 181 | OK |
| `manager_runner_results.py` | runner 结果处理 | runner 上下文 | 302 | OK |
| `manager_lifecycle.py` | 生命周期（暂停/恢复/放弃） | 调度 | 257 | OK |
| `manager_capabilities.py` | 能力路由集成 | 无 | 293 | OK |
| `manager_actions.py` | 动作执行 | 无 | 439 | SOFT |
| `manager_indexing.py` | 索引管理 | 无 | 382 | OK |
| `manager_learning.py` | 学习反馈 | 无 | 255 | OK |
| `manager_channel_probe.py` | 通道探测 | 无 | 230 | OK |
| `manager_normalize.py` | 数据归一化 | 无 | 143 | OK |
| `services/persistence.py` | 持久化服务（load/list_runs/save） | 生命周期 | 100+ | OK |
| `services/lifecycle.py` | 生命周期服务（能力请求/授予/缺口/心跳） | 持久化 | 100+ | OK |
| `models.py` | 子代理数据模型 | 业务逻辑 | 428 | SOFT |
| `policies.py` | 策略规则（纯函数） | I/O、状态变更 | 383 | OK |
| `policy_checks.py` | 策略校验 | 无 | 287 | OK |
| `parsing.py` | 解析工具 | 无 | 273 | OK |
| `rendering.py` | 渲染工具 | 无 | 490 | SOFT |
| `reports.py` | 报告模型 | 无 | 406 | SOFT |
| `runner_rendering.py` | runner 渲染 | 无 | 307 | OK |
| `result_processors.py` | 结果处理器 | 无 | 358 | OK |
| `acceptance_helpers.py` | 验收辅助 | 无 | 364 | OK |
| `utils.py` | 通用工具 | 无 | 75 | OK |
| `probe.py` | 探测工具 | 无 | 130 | OK |

**子代理层总行数**: ~8,300+ 行
**归属原则**: subagents 负责子代理的完整生命周期管理。不负责主代理的调度循环（那是 agent_core）、不负责 CLI 渲染（那是 cli/）。

---

## 4. Gateway Layer / Gateway 层 (`agent/gateway_parts/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `runtime.py` | gateway 运行时（请求处理主循环） | 进程管理、HTTP 细节 | 551 | SOFT |
| `http_service.py` | HTTP 服务 | 运行时逻辑 | 402 | SOFT |
| `supervisor.py` | 进程监管（自动重启、健康检查） | HTTP 处理 | 425 | SOFT |
| `daemon_control.py` | 守护进程控制（start/stop/restart） | 监管逻辑 | 492 | SOFT |
| `lease.py` | 租约机制（防止重复启动） | 无 | 165 | OK |
| `adapter.py` | gateway 适配器 | 无 | 242 | OK |
| `io.py` | gateway I/O | 无 | 163 | OK |
| `paths.py` | gateway 路径管理 | 无 | 112 | OK |
| `logging.py` | gateway 日志 | 无 | 160 | OK |
| `process_control.py` | 进程控制 | 无 | 73 | OK |
| `recovery.py` | 恢复机制 | 无 | 278 | OK |

**归属原则**: gateway_parts 负责 gateway 的进程生命周期和请求处理。不负责子代理管理（那是 subagents/）、不负责 CLI 输出（那是 cli/）。

---

## 5. Memory Layer / 记忆层

### 5.1 Memory Store (`agent/memory_store/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `jsonl.py` | JSONL 格式记忆存储（追加写入） | 路由匹配、归档策略 | 340 | OK |

**归属原则**: memory_store 只负责记忆的物理存储。不负责路由匹配（那是 memory_routing/）、不负责归档（那是 memory_archive/）。

### 5.2 Memory Routing (`agent/memory_routing/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `context.py` | 路由上下文构建 | 存储操作 | 440 | SOFT |
| `loader.py` | 路由规则加载 | 匹配逻辑 | 233 | OK |
| `matcher.py` | 路由匹配引擎 | 规则加载 | 316 | OK |
| `models.py` | 路由数据模型 | 无 | 191 | OK |
| `validator.py` | 路由校验 | 无 | 99 | OK |

**归属原则**: memory_routing 负责记忆路由规则的加载、匹配和校验。不负责记忆的物理存储（那是 memory_store/）、不负责归档（那是 memory_archive/）。

### 5.3 Memory Archive (`agent/memory_archive/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `query.py` | 归档查询 | 存储操作 | 838 | FROZEN |
| `runtime.py` | 归档运行时 | 查询 | 657 | SOFT |
| `snapshots.py` | 快照管理 | 查询 | 150+ | OK |
| `storage.py` | 归档存储 | 查询 | 100+ | OK |
| `tokens.py` | token 计量 | 无 | 100+ | OK |
| `resume_brief.py` | 恢复摘要 | 无 | 100+ | OK |
| `resume_context.py` | 恢复上下文 | 无 | 100+ | OK |
| `models.py` | 归档数据模型 | 无 | 100+ | OK |

**归属原则**: memory_archive 负责记忆的归档、压缩、快照和恢复。不负责子代理状态管理（那是 subagents/）。

---

## 6. Tooling Layer / 工具层 (`agent/tooling/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `filesystem.py` | 文件系统读取工具 | 写入操作 | 530 | SOFT |
| `filesystem_write.py` | 文件系统写入工具 | 读取操作 | 268 | OK |
| `shell.py` | Shell 执行工具 | 无 | 162 | OK |
| `web.py` | Web 请求工具 | 无 | 250 | OK |
| `registry.py` | 工具注册表 | 无 | 408 | SOFT |
| `write_boundary.py` | 写入边界控制（安全校验） | 实际写入 | 147 | OK |
| `models.py` | 工具数据模型 | 无 | 255 | OK |
| `parser.py` | 工具参数解析 | 无 | 248 | OK |

**归属原则**: tooling 负责外部工具的安全封装。不负责任务调度（那是 agent_core/）、不负责子代理管理（那是 subagents/）。

---

## 7. Settings Layer / 配置层 (`agent/settings/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `config.py` | AgentConfig dataclass + 加载逻辑 | 业务逻辑 | 751 | FROZEN |
| `config_normalize.py` | 配置归一化（未知字段过滤） | 配置加载 | 100+ | OK |
| `memory.py` | 记忆配置归一化 | 无 | 80+ | OK |

**归属原则**: settings 只负责配置的定义、加载和归一化。不负责业务逻辑。

---

## 8. Other Modules / 其他模块

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `session/` | 会话管理（创建、恢复、跨通道接续） | 子代理管理、模型调用 | ~1,100 | OK |
| `capability/` | 能力路由（技能定义、路由规则） | 子代理管理 | ~560 | OK |
| `adapter/` | 通道适配器（QQ, 飞书） | 核心调度 | ~1,420 | OK |
| `extensions/` | 插件系统 | 核心逻辑 | ~60 | OK |
| `repositories/` | 仓库层（占位） | 无 | 0 | PLACEHOLDER |
| `task_registry/` | 任务注册表 | 子代理管理 | ~200 | OK |
| `audit/` | 审计（日志写入、查询） | 业务逻辑 | ~300 | OK |
| `concurrency/` | 并发控制（乐观锁、任务锁、重试） | 业务逻辑 | ~200 | OK |
| `auth/` | 认证 | 授权 | ~100 | OK |
| `notification/` | 通知 | 无 | ~150 | OK |
| `security/` | 安全 | 无 | ~100 | OK |
| `log_analysis/` | 日志分析扩展 | 核心调度 | ~3,000+ | OK |

---

## 9. Current Debt Summary / 当前债务汇总

### FROZEN Files (must refactor before adding code)
| 文件 | 行数 | 拆分目标 |
|---|---|---|
| `cli/chat.py` | 1017 | chat_parts/ 继续拆分 |
| `agent_core/dispatch_mixin.py` | 895 | dispatch/planner + runner + loop |
| `memory_archive/query.py` | 838 | query_builder + query_executor |
| `subagents/manager_patch.py` | 794 | subagent_services/patch.py |
| `settings/config.py` | 751 | shared/config/ + 加载逻辑 |
| `subagents/manager_base.py` | 751 | subagent_services/persistence + board |
| `log_analysis/analytics/detectors/rules.py` | 745 | rule_engine + rule_loader |

### SOFT Warning Files (consider splitting)
| 文件 | 行数 | 建议 |
|---|---|---|
| `log_analysis/tools.py` | 672 | 插件化拆分 |
| `memory_archive/runtime.py` | 657 | 拆分归档运行时 |
| `cli/memory_commands.py` | 608 | 拆分记忆子命令 |
| `gateway_parts/runtime.py` | 551 | 拆分请求处理 |
| `cli/gateway_service.py` | 547 | 拆分服务封装 |
| `cli/gateway_process.py` | 539 | 拆分进程管理 |
| `tooling/filesystem.py` | 530 | 拆分读写操作 |
| `cli/subagents.py` | 525 | 拆分子代理命令 |
| `subagents/manager_board.py` | 471 | 拆分 HTML/终端渲染 |
| `gateway_parts/daemon_control.py` | 492 | 拆分控制逻辑 |
| `memory_routing/context.py` | 440 | 拆分上下文构建 |
| `subagents/manager_actions.py` | 439 | 拆分动作类型 |
| `gateway_parts/supervisor.py` | 425 | 拆分监管逻辑 |
| `gateway_parts/http_service.py` | 402 | 拆分 HTTP 处理 |
| `subagents/manager_dispatch.py` | 423 | 拆分派工逻辑 |

### Zero Star Import Policy
项目已消除所有 `import *` 使用。CI 中有 guardrail 测试强制执行零 star import。新代码一律禁止使用 `import *`。
