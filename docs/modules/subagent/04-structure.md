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
- `agent/agent_core/subagent_mixin.py`：子代理生命周期入口，包含 run/finalize、结构化修复、
  recovery snapshot 和 parent planner 记录；旧私有 repair/planner mixin 不再作为跳转层存在。
- `agent/agent_core/subagent/params.py`：子代理生命周期和 parent planner 参数类的权威位置。
- `agent/agent_core/orchestration/`：主代理模型可见的 `create_subagents`、`dispatch_subagents`、`inspect_agent_tree`、`cancel_subagents` 等工具实现。
- `agent/agent_core/runner/`：子代理 worker、prompt、session heartbeat、timeout policy。

## 状态和路径

- 权威状态：当前 task workspace 的 `work/agents/<run_id>/canonical_state.json`。
- 状态机：完成只写 `DONE`；失败/阻塞只写当前协议枚举，不把旧标签或自然语言别名提升为机器状态。
- 状态判断走 canonical state 和 `subagents.models` 中的 `TaskStatus` /
  `VerificationStatus` helper；旧 `subagents/state_machine.py` 私有转换表已删除，
  避免 `WAIT_CHILD` 等历史状态绕过当前协议。
- 恢复候选、agent tree bucket、due-check、leadership recovery 和 runner 结果
  payload 不再各自维护失败/完成状态集合；这些机器判断从 `subagents.models`
  读取当前协议集合，未知旧标签只保留为审计文本。
- 恢复模式和 capability 等待状态也只认当前结构化枚举。未知 `rerun_*` / `takeover_*`
  前缀、`NEEDS_TOOL` 这类旧别名、工具错误正文，都不能触发自动重跑、接管、授权或验收状态变更。
- capability request 的打开/终态判断集中在 `model_capabilities.py`。`OPEN` 代表待处理，
  `GRANTED` 代表已授权且可避免重复申请，`GAP` 和 `CLOSED` 是当前终态；旧
  `RESOLVED`、`APPROVED`、`REJECTED` 不再被 kernel、protocol、runner、board 或
  runner context 静默当成当前终态。
- 子代理过程文件：`work/agents/<run_id>/...`。
- 用户最终交付：主代理汇总后写当前 task `output/`，或用户显式指定的输出目录。
- owner projection：`owner_home/agents/<run_id>/` 只保存 refs，用于 tree、compact、恢复和跨 session 查找。
- task rollup：`work/compact/task_rollup.json` 汇总子代理状态和 refs，父代理恢复时先读这里。
- `agent_name` 是展示名，不是层级或角色事实。默认展示名使用 `agent-d<depth>-<role>-<index>`；
  深度、权限、模板和状态仍只读结构化字段，不能从显示名、中文叫法或英文别名里反推。
- 层级继承状态写在 `attributes.inherited_parent_context`；`goal` 只承载给模型阅读的任务说明和
  父级边界摘要，不承担机器状态判断。

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
dispatch、runner summary、parent-timeout recovery、compact continue packet 和 board risk
使用同一组状态 helper 判断 done/verified、failure、ended、dispatch-ineligible 和 handled terminal，
不在各自模块维护额外的状态别名表。
QA 失败只来自任务状态、结构化 `ok: false`、`passed: false`、blockers、测试记录或读取错误；
`ERROR`、`FAILED` 这类写在 summary/旧 payload 里的普通词不会自动触发 repair wave。

## Guidance

运行中补充提示统一落 conversation guidance 账本。目标可以是 agent run、thread、task 或 case。子代理下一轮执行上下文会读取点名给自己的 guidance，并在 runner prompt 中显示。它只是补充上下文，不是机器验收门，也不自动替模型完成任务；停止、取消、接管和验收要走对应结构化状态或控制入口，不能靠解析自然语言 guidance 来改写任务合同。

## Cancel And Takeover

主代理可以用 `cancel_subagents` 按 run_id/root/status 取消下级。取消会写 CANCELLED/ABANDONED、废弃 active attempt、尽量 interrupt/terminate 已知 pid/session，并写审计记录。主代理说明取消/接管原因后，可以继续汇总和验收。

## Create-Time Boundaries

`create_subagents` 只负责结构化派工、目标路径、写入安全和 lineage 记录。业务质量约束
（例如按钮是否可用、图片是否可验、注释是否允许）可以随任务上下文传递，但不能变成
派工入口硬门；父代理应在读取子代理 refs 后验收或安排 QA。

## Collaboration Capabilities

子代理模板或创建属性可以显式声明 `capabilities`、`collaboration_capabilities` 或
`provided_capabilities`。协作路由按这些字段和真实工具名匹配，不从工具名子串、角色说明
或 summary 自动推断 `query`、`write`、`delegate` 等抽象能力。

## Artifact Rule

子代理可以写自己的过程产物和协作文件，但最终用户交付由主代理汇总。子代理 `final_report.md` 这类内部文件只作为证据/引用，不会自动变成用户最终交付。

子代理结果回报里的产物入口只认当前结构化字段：`artifacts`、顶层
`artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
`deliverables`、`files_modified`、顶层 `file_path/path` 这类历史别名不会被恢复成
artifact refs。创建任务时给子代理的 `output_files` 是目标路径合同，不是结果回报别名。

`create_subagents` 的 `count > 1` 模式会复制同一份任务说明；如果模型同时给了共享
`output_files` / `output_refs`，运行时会把原共享目标登记为 `shared_requested_output_*`，
并给每个 child 分配 task-local `work/child_outputs/...` 独立目标，避免多个 worker
覆盖同一个文件。需要多个 child 精确写不同业务文件时，优先用 `items` 给每个 child
显式声明自己的输出路径。
