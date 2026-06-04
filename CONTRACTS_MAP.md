# CONTRACTS MAP

当前合同地图只记录主链路关口，不保留历史入口说明。

## Runtime Gates

| Area | Primary Files | Purpose |
| --- | --- | --- |
| Tool gateway | `agent_py_agent/agent/contracts/gates/tool/`, `agent_py_agent/agent/tooling/` | 工具调用、路径、命令和副作用检查 |
| Delivery closeout | `agent_py_agent/agent/agent_core/delivery_closeout/` | 收口验收、产物证明、子代理聚合状态 |
| Task workspace | `agent_py_agent/agent/memory_archive/task_workspace/` | 当前 run 的 output/work 目录、索引和验收记录 |
| Owner home | `agent_py_agent/agent/user_space/` | `~/.my-agent/owners/...` 布局、owner identity、运行路径 |
| Subagent state | `agent_py_agent/agent/subagents/manager.py`, `agent_py_agent/agent/subagents/services/` | 子代理创建、状态、取消、接管和审计 |
| Gateway | `agent_py_agent/agent/gateway_parts/` | 请求队列、lease、执行、渲染和恢复 |
| Memory | `agent_py_agent/agent/memory_archive/`, `agent_py_agent/agent/memory_store/` | 日流水、长期记忆、compact、artifact refs |

## Rule

新代码必须接当前主链路。不要新增只转发调用的层，也不要恢复历史目录/历史字段旁路。
