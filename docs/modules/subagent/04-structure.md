# Subagent Structure

本文只描述当前子代理主链路。

## 核心链路

```text
SimpleAgent orchestration tool
  -> SubAgentManager
  -> services/base.py creates SubAgentTask
  -> services/persistence writes canonical state
  -> runner worker runs model/tool loop
  -> services/runner_result records result/artifacts/status
  -> parent inspects tree / cancels / takes over / summarizes
```

## 入口

- `agent/subagents/manager.py`：子代理 root manager，负责初始化、基础生命周期、工单路径和服务组合。
- `agent/subagents/kernel.py`：从 canonical state、projection 和 task workspace 生成稳定树快照。
- `agent/subagents/parsing/`、`agent/subagents/rendering.py`、`agent/subagents/role_templates.py`：
  放当前子代理协议解析、展示渲染和模板策略；不再保留单独一跳 facade。
- `agent/agent_core/orchestration/`：主代理模型可见的 `create_subagents`、`dispatch_subagents`、`inspect_agent_tree`、`cancel_subagents` 等工具实现。
- `agent/agent_core/runner/`：子代理 worker、prompt、session heartbeat、timeout policy。

## 状态和路径

- 权威状态：当前 task workspace 的 `work/agents/<run_id>/canonical_state.json`。
- 状态机：完成只写 `DONE`；失败/阻塞只写当前协议枚举，不把旧标签或自然语言别名提升为机器状态。
- 子代理过程文件：`work/agents/<run_id>/...`。
- 用户最终交付：主代理汇总后写当前 task `output/`，或用户显式指定的输出目录。
- owner projection：`owner_home/agents/<run_id>/` 只保存 refs，用于 tree、compact、恢复和跨 session 查找。
- task rollup：`work/compact/task_rollup.json` 汇总子代理状态和 refs，父代理恢复时先读这里。

## Services

| Service | 负责什么 |
|---|---|
| `base.py` | create_run/split、owner 继承、runtime config scope |
| `persistence/` | canonical state 读写、projection、global index、LocalStore 投影 |
| `dispatch/` | dispatch/watch/parent planner 报告 |
| `runner_context/` | 执行上下文、写入边界、任务配置、runtime guidance、runner allowed tools |
| `runner_result/` | runner 输出解析、状态和 artifact refs 写回 |
| `board/` | board、due-check、action-plan |
| `actions/` | action-plan 应用、取消、接管动作记录 |
| `hierarchy/` | 多层级 child scheduling、recovery packet、leadership recovery |
| `patch_apply/` | patch review/apply/report/rollback |
| `capabilities/` | capability request/grant/gap 路由 |
| `memory_gate/` | 子代理 task-local 候选经验，不自动写长期记忆 |
| `workflow.py` | workflow mode、模板计划、worker tool 选择 |

## Recovery And QA Signals

恢复器只根据 `BLOCKED`、`FAILED`、`TIMEOUT`、`CHANNEL_ERROR` 等结构化状态和 refs 行动。
QA 失败只来自任务状态、结构化 `ok: false`、`passed: false`、blockers、测试记录或读取错误；
`ERROR`、`FAILED` 这类写在 summary/旧 payload 里的普通词不会自动触发 repair wave。

## Guidance

运行中补充提示统一落 conversation guidance 账本。目标可以是 agent run、thread、task 或 case。子代理下一轮执行上下文会读取点名给自己的 guidance，并在 runner prompt 中显示。它只是补充上下文，不是机器验收门，也不自动替模型完成任务；停止、取消、接管和验收要走对应结构化状态或控制入口，不能靠解析自然语言 guidance 来改写任务合同。

## Cancel And Takeover

主代理可以用 `cancel_subagents` 按 run_id/root/status 取消下级。取消会写 CANCELLED/ABANDONED、废弃 active attempt、尽量 interrupt/terminate 已知 pid/session，并写审计记录。主代理说明取消/接管原因后，可以继续汇总和验收。

## Artifact Rule

子代理可以写自己的过程产物和协作文件，但最终用户交付由主代理汇总。子代理 `final_report.md` 这类内部文件只作为证据/引用，不会自动变成用户最终交付。
