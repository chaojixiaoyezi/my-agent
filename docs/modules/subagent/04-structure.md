## 2026-05-06 structure update
- `subagents/models.py` is now a compatibility facade over `model_capabilities.py`, `model_records.py`, `model_runtime.py`, and `model_task.py`.
- `subagents/result_processors.py` delegates structured output handling to `result_structured.py` and output payload assembly to `result_payloads.py`.
- `subagents/manager_base.py` delegates work-order filesystem concerns to `manager_work_orders.py`.
# Subagent：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- subagent.py                         # 兼容入口
|-- subagents/                          # subagent 任务、manager、报告、runner、解析和渲染
|-- subagent_workflows/                 # workflow 模型、模板加载、路由、编译和验收规划
|   |-- builtin/                        # 内置 workflow 模板
|   |-- models.py                       # workflow / 任务 / 质量契约相关模型
|   |-- store.py                        # 模板加载和覆盖
|   |-- router.py                       # 根据目标选择 workflow
|   |-- compiler.py                     # 把 workflow 编译成 worker 派工规格
|   |-- planner.py                      # dry-run 规划门面
|   `-- acceptance.py                   # 父级验收计划
`-- agent_core/                         # 主循环、dispatch、runner prompt 等接入点
```

## 核心文件

- `agent_py_agent/agent/subagent_workflows/router.py`：回答”这个任务适合哪种 workflow”。
- `agent_py_agent/agent/subagent_workflows/compiler.py`：把抽象模板变成具体 worker 任务说明。
- `agent_py_agent/agent/subagent_workflows/acceptance.py`：生成父会话要检查什么。
- `agent_py_agent/agent/subagents/`：保存真实 subagent 管理、运行、报告和验收相关代码。
- `agent_py_agent/agent/subagents/models.py`：定义 `SubAgentTask`、`TaskStatus`、`DISPATCH_INELIGIBLE_STATUSES` 等核心数据结构。
- `agent_py_agent/agent/subagents/manager_indexing.py`：实现 `_select_runs()` 等索引和过滤逻辑，同时提供公开别名 `select_runs()`、`index_task()` 等。
- `agent_py_agent/agent/subagents/manager_acceptance_findings.py`：实现验收发现逻辑，公开别名 `acceptance_findings()`。
- `agent_py_agent/agent/subagents/acceptance_helpers.py`：验收发现子模块的兼容导出入口，真实实现已拆到 `acceptance_helpers/` 目录。
- `agent_py_agent/agent/subagents/acceptance_helpers/evidence_acceptance_findings.py`：承接证据存在性和 read/write_file 工具证据 findings 构建逻辑，保持 `evidence.py` 作为薄兼容入口。
- `agent_py_agent/agent/subagents/manager_patch.py`：实现补丁操作，公开别名 `resolve_patch_target()`。
- `agent_py_agent/agent/subagents/manager_runner_results.py`：负责 runner 结果写回、状态降级和 `output.json` / `runner_result.json` 持久化；结构化解析失败时会把最终结果统一降级成 `BLOCKED` / `ok=False`。
- `agent_py_agent/agent/subagents/manager_runner_result_payload.py`：承接 runner result payload/status 构建 dataclass 和纯 helper，让 manager facade 保持短小。
- `agent_py_agent/agent/subagents/capability_route_service.py`：承接 capability route record 构建、gap 包装、summary 和报告落盘。
- `agent_py_agent/agent/subagents/services/indexing_records.py`：承接 LocalStore dataclass record 索引 helper，让 indexing service 只保留编排入口。
- `agent_py_agent/agent/subagents/policies.py` / `policy_checks.py`：负责 due-check 风险规则和下一步建议命令；用户可见命令统一使用 `my-agent` 控制台入口。
- `agent_py_agent/agent/memory_push.py`：实现记忆推模式，在决策点自动注入相关记忆。
- `agent_py_agent/cli/subagents.py`：用户从 CLI 预览或操作 subagent 的入口。

## 数据流

1. 用户输入目标，例如“帮我做一个高质量文档交付”。
2. router 根据目标和配置选择 workflow。
3. store 加载内置或用户覆盖的模板。
4. compiler 生成 worker 派工规格，包含写入范围、证据要求和不能自验收的规则。
5. acceptance planner 生成父级验收清单。
6. runner 根据 execution context 调模型和工具，把 `RUNNER_RESULT.md`、`reports/runner_result.json`、`output.json` 写回任务目录。
7. `memory-resume` 在跨天恢复时用 archive/LocalStore 作为线索，最终推荐读取任务目录里的事实源，再由父级决定是否验收。
8. 目前 workflow dry-run CLI 可以展示计划；通用 workflow apply path 仍在推进中，LOG 专项 apply path 和 runner 恢复 scenario 已先行验证真实任务记录。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/subagents.py`，理解用户命令怎么进入程序。
2. 再看 `planner.py`，理解一个“规划结果”包含哪些部分。
3. 再看 `router.py`，学习如何把自然语言目标映射到模板。
4. 再看 `compiler.py`，学习模板怎样变成具体工作单。
5. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py::test_scenario_parent_subagent_cross_day_resume_uses_runner_task_facts`，理解 runner 写回后如何跨天恢复到任务事实源。
6. 最后看 `agent_py_agent/tests/test_subagent_workflow_*.py`，理解怎么证明路由、模板和编译没有坏。

## 当前第一版索引 / 待补齐

本页先解释主结构和学习路径。更细的类字段、状态机和通用 workflow apply 链路，需要等后续 apply path 落地后补齐。
## 2026-05-06 structure update
- Workflow routing and subagent manager internals now separate decision fields, rendering sections, patch normalization, and service actions.
- Compatibility modules still re-export the existing public model and rendering names for callers.
