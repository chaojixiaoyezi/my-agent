# FEATURE-20260511-per-agent-tool-budget

Status: Implemented

## Background / 背景

真实子代理 E2E 暴露过一种风险：某个代理卡住后会反复读文件、查 board、重试工具，既烧 token/API，也拖慢整棵任务树。用户明确要求先只限制“单个代理”的工具次数，不做任务整体预算，也不做单次对话预算。

## Goal / 目标

为每个有 `run_id` 的代理运行增加可配置滚动工具调用预算；当前默认由 `agent_py_agent/config/runtime_guard_config.yaml` 决定，是 600 秒 / 200 次。超过后让该代理自检、总结进展并向父级上报，而不是继续无限调用工具。

## Non-Goals / 非目标

- 不做整棵任务树的总工具预算。
- 不做单次对话或主代理普通聊天的工具预算。
- 不因为预算触发而直接杀死 runner 或改写任务状态。
- 不在本片实现 shell/exec 网关、trash 目录或长期主代理 daemon。

## Scenarios / 场景

- 一个 leaf 在配置窗口内重复读同一个文件超过配置次数：下一次工具调用被拦截，模型收到自检/上报提示。
- 两个兄弟 leaf 同时工作：它们的预算按各自 `run_id` 隔离，互不消耗。
- 普通主代理聊天没有 subagent `run_id`：不受这个预算限制。
- 长期任务超过 10 分钟：旧调用会滚出窗口，健康代理不会永久背负历史次数。

## Requirements / 需求

| ID | Description | Priority |
|----|-------------|----------|
| FR-001 | The budget shall be keyed by agent `run_id`. | Must |
| FR-002 | The default budget shall be read from `runtime_guard_config.yaml` (`tool_agent_budget_window_seconds` / `tool_agent_budget_max_calls`). | Must |
| FR-003 | Calls without `run_id` shall not be limited by this guard. | Must |
| FR-004 | Sibling agents shall not share or consume each other's budget. | Must |
| FR-005 | Budget hits shall return a bounded self-check/handoff tool result. | Must |
| FR-006 | Setting max calls or window seconds to `0` shall disable the guard. | Should |

## Constraints / 约束

- 不引入新依赖。
- 接口继续使用 bundle/dataclass 风格。
- 预算状态先放在当前长存活 agent 对象内，不写磁盘、不跨进程共享。
- 不改变已有 `max_tool_rounds` 语义；两者互补。

## Impact / 影响

- `agent_core/tool_guard/agent_budget.py`：新增预算 helper。
- `agent_core/_tool_loop_service.py`：执行工具前检查预算。
- `settings/runtime_guard_config.py` 和 `config/runtime_guard_config.yaml`：集中保存运行门默认值，避免 AgentConfig 和运行门 YAML 双写。
- Subagent 文档、开发规范和代码树同步说明。

## Architecture / 架构

工具循环解析出工具调用后，先拿到当前运行作用域的 `run_id`。若 `run_id` 为空，直接放行。若存在 `run_id`，预算 helper 会清理窗口外时间戳，判断当前 run 是否已达到阈值；未达到则登记本次调用并放行，达到则返回 `ToolExecutionResult(ok=False)`，工具本身不执行。

## Data Model / 数据模型

`ToolAgentBudgetRequest` 是预算检查 bundle：

- `agent`: 当前 agent 对象，持有 config 和内存预算表。
- `run_id`: 当前代理运行 id。
- `tool_name`: 当前工具名，用于返回结果。
- `now`: 可选测试时间戳。

运行时内存字段：

- `agent._tool_agent_budget_events`: `dict[str, list[float]]`，按 `run_id` 记录滚动窗口内的工具调用时间戳。

## State Transitions / 状态转换

- under_budget -> record call -> execute tool.
- over_budget -> return budget result -> model self-check/handoff.
- no_run_id -> execute tool without this guard.

## File Writes / 文件写入

本功能不新增运行时文件写入。配置文件只新增用户可调字段。

## Test Plan / 测试计划

- 直接 helper 单测：无 `run_id` 不限流、同 run 达阈值阻断、不同 run 隔离、窗口外调用过期。
- 工具循环单测：同一 `run_id` 第二次工具调用被预算拦截，真实工具不执行，模型能看到自检提示并收口。
- 回归：ruff、doc sync、strict code-size、full pytest。

## Acceptance Criteria / 验收标准

- [x] 默认配置从 `runtime_guard_config.yaml` 读取；当前为 600 秒 / 200 次。
- [x] 普通主代理无 `run_id` 路径不被限制。
- [x] 同 run 超预算时返回自检/上报提示。
- [x] 兄弟 run 预算互不影响。
- [x] 工具循环集成后不会执行被预算拦截的工具。

## Risks / 风险

- 当前预算表是进程内内存；进程重启后计数清空。可接受，因为本片目标是防单次卡死复读，不是计费系统。
- 如果未来多个 worker 进程跑同一个 run，预算不会天然共享；后续可用 runtime store 或 gateway 统一计数。
- 超大输出、shell/exec、tool/skill 申请需要独立网关和输出上限，不能靠本预算解决。

## Rollback / 回滚方案

把 `tool_agent_budget_max_calls` 或 `tool_agent_budget_window_seconds` 设为 `0` 即可关闭守卫；代码层如移除预算检查，也要同步删除配置、测试和文档。
