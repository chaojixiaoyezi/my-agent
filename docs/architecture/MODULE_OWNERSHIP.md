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
| `agent_core/subagent_mixin.py` | 子代理相关公开入口 facade | 具体子代理管理 | 200+ | OK |
| `agent_core/subagent/lifecycle_service.py` | 子代理 spawn/run/probe/finalize 生命周期组合服务 | repair/planner 细节、持久化实现 | 90- | OK |
| `agent_core/subagent/params.py` | 子代理创建/运行 lifecycle 参数对象 | dispatch/watch 参数、业务状态 | 90- | OK |
| `agent_core/subagent/run_flow.py` | 单个子代理 runner 从 prompt 到 finalize 的顺序流程 | prompt 细节、repair 策略 | 150- | OK |
| `agent_core/subagent/spawn_flow.py` | 子代理创建数量、角色种子和自动拆分流程 | persistence 实现、runner 执行 | 100- | OK |
| `agent_core/subagent/finalize_helpers.py` | 子代理 runner 结果持久化和恢复快照写入 | lifecycle 调度、模型调用 | 90- | OK |
| `agent_core/subagent/compact_continuation.py` | 子代理 task-local compact 接续提示 | 主代理 memory、工具执行 | 300+ | OK |
| `agent_core/subagent/session_continuation.py` | 子代理本地 session compact 后自动续接 | 任务拆分、父代理调度 | 120- | OK |
| `agent_core/subagent/progress_closeout.py` | 子代理 task-local 产物进度收口提示 | 主任务 closeout、产物验收策略 | 180- | OK |
| `agent_core/subagent/attempt_guard.py` | runner attempt 过期/废弃保护 | 通用工具权限、任务终止策略 | 80- | OK |
| `agent_core/tool_stream/boundary.py` | 模型流式输出中工具协议截断、畸形协议和超长内联写入边界 | 工具执行、写入权限 | 220- | OK |
| `agent_core/tool_stream/models.py` | 工具流边界异常和 payload 模型 | 协议解析、业务策略 | 60- | OK |
| `agent_core/tool_stream/write_abort.py` | write_file 流式内联内容过长的 abort payload 解析 | 文件写入执行、产物验证 | 150- | OK |
| `agent_core/failure_introspector.py` | 失败自省引擎（LLM 分析失败原因） | 调度决策 | 150+ | OK |
| `agent_core/failure_analyzer.py` | 失败模式分析 | 自省 | 100+ | OK |
| `agent_core/planner.py` | 调度规划（runner 指令组合） | 执行 | 150+ | OK |
| `agent_core/runner/dispatch.py` | runner 分发（并发控制、重试、超时） | 调度规划、runner gate | 200+ | OK |
| `agent_core/runner/gate.py` | runner 执行入口、失败续跑提示和 worker 参数收口 | runner dispatch、memory push | 200- | OK |
| `agent_core/runner/prompts.py` | runner 提示词模板 | runner context summary、compact continuation | 300- | OK |
| `agent_core/runner/ref_fields.py` | runner 输入/输出 refs 解析 | create_subagents、write roots | 200- | OK |
| `agent_core/orchestration/dispatch/mixin.py` | SimpleAgent dispatch 公开 facade | runner 实现、watch 实现 | 270- | OK |
| `agent_core/orchestration/dispatch/params.py` | dispatch/watch 参数对象和执行计划 | 业务执行、CLI 渲染 | 250- | OK |
| `agent_core/orchestration/dispatch/loop.py` | dispatch_loop 循环推进 | 子代理创建、模型调用 | 270- | OK |
| `agent_core/orchestration/dispatch/lock.py` | dispatch watch 文件锁 | 业务状态判断 | 90- | OK |
| `agent_core/orchestration/dispatch/runner_batches.py` | runner 候选收集、限流和批量执行衔接 | runner 内部执行、模型 prompt | 240- | OK |
| `agent_core/orchestration/dispatch/service.py` | dispatch step record builders | watch 循环、runner 执行 | 250- | OK |
| `agent_core/dynamic_timeout.py` | 动态超时计算 | 无 | 80+ | OK |
| `agent_core/adaptive_retry.py` | 自适应重试 | 无 | 80+ | OK |
| `agent_core/task_complexity.py` | 任务复杂度评估 | 无 | 100+ | OK |
| `agent_core/watchdog.py` | 看门狗（daemon 巡检） | 无 | 80+ | OK |
| `agent_core/orchestration_tools.py` | 编排工具 | 无 | 100+ | OK |
| `agent_core/orchestration/tool_specs.py` | 编排工具模型可见短规格 | 编排执行逻辑、长文档说明 | 160- | OK |
| `agent_core/orchestration/tool_spec_data.py` | 编排工具短参数说明数据 | 角色模板路径、开发者长说明 | 120- | OK |
| `agent_core/orchestration/tool_grants.py` | 子代理基础工具授权名单与工具预设解析 | workflow 私有工具名单、权限执行 | 70- | OK |
| `agent_core/orchestration/workflow_mode.py` | 编排工具 workflow_mode 归一化 | workflow 执行、配置加载 | 20- | OK |
| `agent_core/orchestration/lineage_names.py` | 编排创建时的子代理显示名和序号归一化 | 子代理执行、角色选择 | 70- | OK |
| `agent_core/orchestration/summary_action_lines.py` | 编排工具结果的上下文摘要行渲染 | 工具执行、状态裁决 | 60- | OK |
| `agent_core/orchestration/create_config.py` | 编排创建参数读取当前 agent 配置 | 配置加载、默认值事实源 | 50- | OK |
| `agent_core/orchestration/dispatch/run_ids.py` | dispatch run_id 参数归一化 | dispatch 执行、scope 裁决 | 50- | OK |
| `agent_core/orchestration/replacements.py` | create 后记录显式 replacement/takeover 关系 | 子代理执行、接管裁决 | 80- | OK |
| `agent_core/orchestration/run_scope.py` | 当前主代理已见/已推进 run_id 的轻量记忆 | tree 构建、持久化状态 | 50- | OK |
| `agent_core/orchestration/work_scope.py` | 输入/输出 refs 派生 work_scope_key | 路径授权、产物验收 | 60- | OK |
| `agent_core/orchestration/background/dispatch.py` | create/schedule 后台自动启动 dispatch | create payload、runner 生命周期、tree 状态 | 280- | OK |
| `agent_core/orchestration/background/marks.py` | 后台启动标记和标记失败报告 | 后台执行、runner 生命周期 | 50- | OK |
| `agent_core/orchestration/tools/event.py` | raise_event 模型工具入口 | conversation store、wake signals、lineage | 240- | OK |
| `agent_core/orchestration/tools/status.py` | inspect_agent_tree 模型工具入口 | agent tree status payload | 50- | OK |
| `agent_core/orchestration/dispatch/load_errors.py` | 子代理账本读取失败的结构化行 | 子代理加载、结果聚合 | 50- | OK |
| `agent_core/orchestration/recovery_batches.py` | 恢复策略分组和建议 dispatch payload | 恢复裁决、dispatch 执行 | 130- | OK |
| `agent_core/orchestration/runner_instruction.py` | runner_instruction 工作区占位符解析 | prompt 生成、dispatch 执行 | 30- | OK |
| `agent_core/orchestration/create_payload.py` | create_subagents 工具返回 payload 组装 | 子代理创建执行、idempotency 事实源 | 210- | OK |
| `agent_core/orchestration/create_constraints.py` | create/schedule 写入根和委托约束冲突解析 | 自然语言目标推断、路径授权执行 | 190- | OK |
| `agent_core/orchestration/create_context.py` | create_subagents context manifest/context packs 归一化 | runner 启动门、路径存在性验收 | 220- | OK |
| `agent_core/orchestration/create_conversation.py` | create_subagents 会话/thread 继承属性 | conversation store 实现、消息投递 | 80- | OK |
| `agent_core/orchestration/create_idempotency.py` | create_subagents 复用已有 child 的结构化幂等裁决 | 目标自然语言相似度裁决、状态机定义 | 230- | OK |
| `agent_core/orchestration/create_items.py` | create_subagents items 批量参数解析 | 子代理创建执行、全局 plan 复制 | 220- | OK |
| `agent_core/orchestration/create_policy.py` | create_subagents CreateRunParams 组装和 role/workflow 归一化 | 工具执行、子代理持久化 | 300- | OK |
| `agent_core/orchestration/create_target_roots.py` | create/schedule 产品写入根推导 | 安全策略裁决、文件写入 | 210- | OK |
| `agent_core/orchestration/shared_context.py` | 父级小型读取 brief 传给子代理 context_packs | 工具归档、runner context | 300- | OK |
| `agent_core/orchestration/lifecycle.py` | 子代理创建后发布、会话绑定和自动启动 | conversation store、background dispatch | 90- | OK |
| `agent_core/orchestration/write_guard.py` | create/schedule 写入目标预检 | 路径策略、产品写入根 | 130- | OK |
| `agent_core/orchestration/dispatch/payload.py` | dispatch_subagents 记录和恢复 payload 组装 | dispatch 执行、runner 选择 | 80- | OK |
| `agent_core/orchestration/dispatch/state_contract.py` | 当前回合子代理状态摘要和下一步建议 payload | 状态机定义、子代理加载 | 180- | OK |
| `agent_core/orchestration/dispatch/refs.py` | dispatch 子代理结果 refs-first 索引 | 子代理账本、artifact refs | 180- | OK |
| `agent_core/orchestration/dispatch/scope.py` | dispatch 目标作用域、apply/dry-run 裁决 | 参数解析、runner scope | 180- | OK |
| `agent_core/orchestration/dispatch/progress_payload.py` | runner-context child 进度摘要 | recovery、QA 建议、refs | 180- | OK |
| `agent_core/orchestration/dispatch/tool.py` | dispatch_subagents 模型工具入口 | dispatch 参数、scope、payload、guidance | 260- | OK |
| `agent_core/orchestration/dispatch/tool_helpers.py` | dispatch 工具入口辅助函数 | runtime config、scope、refs | 180- | OK |
| `agent_core/orchestration/quality_advice_payload.py` | QA/验收建议对象的模型可见 payload | QA 策略生成、子代理调度 | 40- | OK |
| `agent_core/orchestration/quality_payload.py` | QA 子代理失败信号扫描和修复建议 payload | QA 执行、修复执行 | 200- | OK |
| `agent_core/orchestration/child_result_index.py` | 子代理结果索引和模型可见 artifact refs | 读取 artifact 正文、状态加载 | 180- | OK |
| `agent_core/orchestration/scope_resolution.py` | 编排工具身份/scope 裁决 payload | 树构建、dispatch 执行 | 180- | OK |
| `agent_core/orchestration/sibling_roster.py` | 同批子代理 roster context pack | 子代理执行、未来产物预测 | 90- | OK |
| `agent_core/parameters.py` | 调度参数 | 无 | 80+ | OK |
| `agent_core/runtime/capabilities.py` | 运行时能力解析 | 无 | 80+ | OK |
| `agent_core/models.py` | 核心数据模型 | 无 | 100+ | OK |

**归属原则**: agent_core 负责主代理的核心调度循环和运行时执行。不负责 CLI 渲染、不负责具体的子代理状态管理（委托给 subagents/）。

---

## 3. Subagents Layer / 子代理层 (`agent/subagents/`)

| 模块 | 唯一职责 | 不可承担 | 当前行数 | 状态 |
|---|---|---|---|---|
| `manager.py` | SubAgentManager 入口（组合 root + 兼容薄 facade） | 业务逻辑 | 100+ | OK |
| `services/budget.py` | runner refs-only 预算报告生成和落盘 | 调度、验收、模型调用 | 80- | OK |
| `manager_base.py` | 基础 CRUD + 卡片管理 | patch 审核、看板渲染 | 751 | FROZEN |
| `manager_patch.py` | patch 兼容 facade / helper re-export | patch review/apply 实现、基础 CRUD、看板 | 220- | OK |
| `manager_dispatch.py` | dispatch 兼容 facade / helper re-export | dispatch/watch/parent planner 报告实现 | 80- | OK |
| `services/board/service.py` + `services/board/facade.py` | 看板核心服务 + 兼容 facade | 渲染、状态投影 | - | OK |
| `manager_acceptance.py` | 验收流程 | 调度、patch | 293 | OK |
| `manager_acceptance_findings.py` | 验收发现处理 | 验收主流程 | 390 | OK |
| `manager_runner_context.py` | runner 上下文兼容 facade | runner 上下文实现、runner 结果处理 | 50- | OK |
| `services/runner_context/` | execution context、context bundle、write boundary、grant 注入 | runner 结果处理、主代理调度 | - | OK |
| `manager_runner_results.py` | runner 结果兼容 facade / dataclass re-export | runner result 实现、runner 上下文 | 80- | OK |
| `services/runner_result/` | runner result 记录、structured output 处理、debrief、session compact、completion wake | runner 上下文构建 | - | OK |
| `manager_lifecycle.py` | 生命周期兼容 facade / dataclass re-export | 生命周期实现、调度 | 80- | OK |
| `services/capabilities/` | OPEN capability request 到 skill/tool card 的路由和 report 写入 | 生命周期记录、主代理调度 | - | OK |
| `manager_actions.py` | action apply 兼容 facade / dataclass re-export | 动作 handler 细节、审计落盘 | 130- | OK |
| `services/actions/` | 动作 apply 服务 | 看板、派工 | - | OK |
| `manager_indexing.py` | 索引管理兼容 facade / helper re-export | 索引实现 | 120- | OK |
| `services/indexing/` | 任务索引、LocalStore 事件、报告索引 | 持久化事实源、调度、模型调用 | - | OK |
| `manager_learning.py` | 学习反馈兼容 facade / helper re-export | 学习候选存储实现 | 60- | OK |
| `services/learning.py` | 学习候选草稿存储、去重、确认、统计 | 正式 skill 安装、长期记忆写入 | 200+ | OK |
| `services/memory_gate.py` | memory gate 候选列出、审核、导出、retention、verifier | 自动提升长期记忆或安装 skill | 120- | OK |
| `manager_channel_probe.py` | 通道探测兼容 facade / helper re-export | 通道探测实现 | 60- | OK |
| `services/channel_probe.py` | 通道健康探测、probe 证据写入、批量报告 | 调度、验收、模型调用 | 200- | OK |
| `services/workflow.py` | workflow 规划和 worker materialize | 主代理调度、模型调用 | 300- | OK |
| `services/hierarchy/facade.py` | child scheduling、hierarchy recovery、leadership recovery report | 普通 dispatch、模型调用 | 160- | OK |
| `services/hierarchy/` | 子代理树调度、角色继承、恢复包细分模块 | 普通 dispatch、模型调用 | - | OK |
| `manager_normalize.py` | 数据归一化 | 无 | 143 | OK |
| `services/persistence/` | 持久化服务（load/list_runs/save） | 生命周期 | - | OK |
| `services/dispatch/` | dispatch/watch 服务和 parent planner report 服务 | agent_core dispatch 主循环、验收、模型调用 | - | OK |
| `services/leadership_recovery/` | 领导权恢复服务 | 恢复编排 | - | OK |
| `services/lifecycle.py` | 生命周期服务（能力请求/授予/缺口/证据/状态/心跳） | 持久化、runner attempt 细节 | 300- | OK |
| `services/lifecycle_runner_attempts.py` | runner attempt 开始/放弃和恢复预检属性更新 | 能力记录、状态验收 | 100- | OK |
| `services/patch_apply/facade.py` | patch review/apply 对 SubAgentManager 的组合 facade | 具体文件写入实现、索引实现 | 200- | OK |
| `patch/` | patch review/apply/renderer 核心实现 | manager 继承、主代理调度 | - | OK |
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
| `agent_core/orchestration/dispatch/` | 已拆包 | 继续保持 facade / runner / loop / record 职责分离 |
| `memory_archive/query/` | 已拆包 | 继续保持 query builder / executor / formatter 职责分离 |
| `subagents/manager_patch.py` | 220- | 已从 manager mixin 继承链移出，主链路通过 `services/patch_apply/facade.py` |
| `settings/config.py` | 751 | shared/config/ + 加载逻辑 |
| `subagents/manager_base.py` | 751 | subagent_services/persistence/ + board |
| `log_analysis/analytics/detectors/rules.py` | 745 | rule_engine + rule_loader |

### SOFT Warning Files (consider splitting)
| 文件 | 行数 | 建议 |
|---|---|---|
| `log_analysis/tools.py` | 672 | 插件化拆分 |
| `memory_archive/runtime/` | 已拆包 | 继续保持 live archiver / turn archiver / event builder 分层 |
| `cli/memory_commands.py` | 608 | 拆分记忆子命令 |
| `gateway_parts/runtime.py` | 551 | 拆分请求处理 |
| `cli/gateway_service.py` | 547 | 拆分服务封装 |
| `cli/gateway_process.py` | 539 | 拆分进程管理 |
| `tooling/filesystem.py` | 530 | 拆分读写操作 |
| `cli/subagents.py` | 525 | 拆分子代理命令 |
| `subagents/services/board/facade.py` | - | 已从 manager mixin 继承链移出，后续只做兼容 facade 收敛 |
| `gateway_parts/daemon_control.py` | 492 | 拆分控制逻辑 |
| `memory_routing/context.py` | 440 | 拆分上下文构建 |
| `subagents/manager_actions.py` | 439 | 已改成 facade；动作类型在 `services/actions/` |
| `subagents/manager_indexing.py` | 382 | 已改成 facade；索引类型在 `services/indexing/` |
| `subagents/manager_dispatch.py` | 80- | 已改成兼容 facade；dispatch/watch 与 parent planner 报告通过 `services/dispatch/` |
| `gateway_parts/supervisor.py` | 425 | 拆分监管逻辑 |
| `gateway_parts/http_service.py` | 402 | 拆分 HTTP 处理 |

### Zero Star Import Policy
项目已消除所有 `import *` 使用。CI 中有 guardrail 测试强制执行零 star import。新代码一律禁止使用 `import *`。
