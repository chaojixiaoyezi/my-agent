# Module Ownership

这份表只记录当前主链路。旧拆分计划、过渡入口和历史迁移层不在这里保留。

## Runtime

| 区域 | 负责什么 | 不负责什么 |
|---|---|---|
| `agent/core.py` | `SimpleAgent` 运行入口，连接配置、prompt、模型、工具循环、子代理 manager | 子代理内部状态实现、LocalStore 细节 |
| `agent/agent_core/runtime*` | 主代理模型轮次、工具轮次、运行中 guidance、wait policy | 子代理 task 持久化 |
| `agent/agent_core/tool_loop/` | 单轮工具执行、恢复、完成判断、工具输出处理 | 业务工具本身 |
| `agent/agent_core/orchestration/` | 主代理可见的 create/dispatch/cancel/inspect 等编排工具 | 子代理 canonical state 的具体保存 |
| `agent/gateway_parts/` | gateway request/response、worker、lease、HTTP handlers | 模型业务决策 |

## Subagents

| 区域 | 负责什么 | 不负责什么 |
|---|---|---|
| `agent/subagents/manager.py` | 子代理 root manager：初始化、基础生命周期、工单路径、服务组合、公开入口 | 具体 patch/board/index/runner 细节 |
| `agent/subagents/kernel.py` | 从 canonical state 和 projection 生成稳定树快照 | 修改任务状态 |
| `agent/subagents/services/base.py` | 创建子代理 run、继承 owner/runtime config、生成 task identity | 看板、调度结果、patch |
| `agent/subagents/services/persistence/` | 读写 canonical state，同步 owner projection/global index/LocalStore 投影 | 决定业务是否完成 |
| `agent/subagents/services/dispatch/` | dispatch/watch/parent planner 报告和日志 | 启动模型 worker 的底层 runner |
| `agent/subagents/services/runner_context_service.py` | 构造子代理执行上下文、写 boundary 和 config scope | 解析模型最终报告 |
| `agent/subagents/services/runner_result_service.py` | 写 runner result、状态、产物引用和后续副作用 | 创建子代理 |
| `agent/subagents/services/board/` | board、due-check、action-plan 的只读投影和报告 | canonical state 权威保存 |
| `agent/subagents/services/actions/` | 应用 action-plan、取消/接管相关动作记录 | 新建调度树 |
| `agent/subagents/services/hierarchy/` | 多层子代理调度、恢复包、leadership recovery | 普通 dispatch/watch 报告 |
| `agent/subagents/services/patch_apply/` | patch review/apply/report/rollback/test command 校验 | 子代理基础生命周期 |
| `agent/subagents/services/capability_service.py` | 能力请求上抛/授权/缺口记录和路由报告 | owner 永久策略变更 |
| `agent/subagents/services/memory_gate/` | 子代理 task-local 经验候选审阅和导出 | 自动写长期记忆 |
| `agent/subagents/services/workflow.py` | workflow mode、模板计划和 worker tools 选择 | 模型执行 |

## Memory And Home

| 区域 | 负责什么 | 不负责什么 |
|---|---|---|
| `agent/user_space/owner_resolver.py` | 把 local/provider 身份解析到 `~/.my-agent/owners/...` | repo 内临时目录猜测 |
| `agent/user_space/runtime_paths.py` | 当前 owner/workspace/task 运行路径解析 | 模型决策 |
| `agent/user_space/run_workspace.py` | 主代理 task workspace 的 `output/`、`work/`、timeline、state | 子代理个人长期记忆 |
| `agent/user_space/home_doctor.py` | 当前 home 状态、索引、retention、backup 的诊断和建议 | 自动改写历史数据 |
| `agent/memory_store/jsonl.py` | 当前 owner 长期记忆 JSONL 读写 | raw audit、工具大输出 |
| `agent/memory_archive/` | compact、audit、tool output artifact、task workspace 辅助索引 | owner 身份解析 |
| `agent/local_storage/` | SQLite/FTS/文件事实源和控制面索引 | 任务权威状态 |

## CLI

| 区域 | 负责什么 | 不负责什么 |
|---|---|---|
| `cli/chat.py` + `cli/chat_parts/` | 本地交互界面、gateway client、流式渲染 | gateway worker 内部状态 |
| `cli/home_runtime_commands.py` | home-status、memory daily、task workspace、index rebuild 等维护命令 | 运行时自动迁移 |
| `cli/_gateway_*` / `cli/gateway_*` | gateway 启停、状态、进程管理、客户端请求 | 模型任务规划 |
| `cli/_dispatch.py` / `_inspection.py` / `_hierarchy.py` | 子代理编排命令行入口 | 子代理服务实现 |

## Tests

| 区域 | 负责什么 |
|---|---|
| `tests/test_subagent_manager_core.py` | root manager 基础路径和工单文件 |
| `tests/test_subagent_*` | 子代理生命周期、树、compact、恢复、协议 |
| `tests/test_orchestration_*` | 主代理编排工具和合同 |
| `tests/test_home_*` / `tests/test_memory_*` | owner home、memory、compact、archive |
| `tests/test_gateway_*` / `tests/test_chat_parts.py` | gateway、chat/TUI、本地端到端可见行为 |

## Change Rule

新增能力先落在上表已有 owner 区域。只有出现新的事实源、持久化根、模型可见工具或外部协议时，才新增模块；新增后同步这份表和对应 module docs。
