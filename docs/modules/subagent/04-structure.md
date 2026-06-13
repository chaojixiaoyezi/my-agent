# Subagent Structure

本文只描述当前子代理主链路。

## 核心链路

```text
SimpleAgent orchestration tool
  -> SubAgentManager
  -> services/base.py creates SubAgentTask
  -> services/persistence writes canonical state
  -> runner worker runs model/tool loop
  -> services/runner_result_service.py records result/artifacts/status
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
- `cli/subagents.py`：子代理 CLI 命令和注册入口，包含基础、监控、层级和 leadership recovery 命令；不再通过单独 registration / hierarchy 注册文件跳转。

## 状态和路径

- 权威状态：当前 task workspace 的 `work/agents/<run_id>/canonical_state.json`。
- 模型可见的子代理工作根：当前 task workspace 的 `work/agents/<run_id>/`。旧
  `.my_agent/subagents/<run_id>` 只做 locator / owner projection / 查找索引，不是
  `task_dir`、write root 或 artifact root。
- 状态机：完成只写 `DONE`；失败/阻塞只写当前协议枚举，不把旧标签、大小写变体或自然语言别名提升为机器状态。
  `failure_type` 也一样：runner/action 原始结果可以留作审计文本，但写入 `task.failure_type`、
  重试、恢复和验收前必须是当前已知枚举；未知值不能靠小写化或旧标签兼容变成机器状态。
- 状态判断走 canonical state 和 `subagents.models` 中的 `TaskStatus` /
  `VerificationStatus` helper；旧 `subagents/state_machine.py` 私有转换表已删除，
  避免 `WAIT_CHILD` 等历史状态绕过当前协议。
- 恢复候选、agent tree bucket、due-check、leadership recovery 和 runner 结果
  payload 不再各自维护失败/完成状态集合；这些机器判断从 `subagents.models`
  读取当前协议集合，未知旧标签只保留为审计文本。
- 恢复模式和 capability 等待状态也只认当前结构化枚举。未知 `rerun_*` / `takeover_*`
  前缀、`NEEDS_TOOL` 这类旧别名、工具错误正文，都不能触发自动重跑、接管、授权或验收状态变更。
- 子代理 runner 默认复用主代理当前 `AgentConfig`，包括 `model_context_window_tokens`、
  `memory_compact_auto_trigger_percent`、`runner_timeout_seconds`、runner 并发和工具预算。
  只有任务自己携带结构化 `config_overlay_ref` 时才形成 run/task layer 覆盖；不要为
  子代理 compact 或常规真实测试另建第二套参数。
- 自适应重试拆分父任务时不再写历史自定义状态 `SPLIT`；父任务进入当前协议
  `TAKEN_OVER`，拆分关系只记录在结构化 `attributes.split_into` 和 `child_ids`。
- capability request 的打开/终态判断集中在 `model_capabilities.py`。`OPEN` 代表待处理，
  `GRANTED` 代表已授权且可避免重复申请，`GAP` 和 `CLOSED` 是当前终态；旧
  `RESOLVED`、`APPROVED`、`REJECTED` 不再被 kernel、protocol、runner、board 或
  runner context 静默当成当前终态。
- capability route 自动匹配只读结构化能力字段：`needed_capability`、requested tool/skill/mcp/
  command、constraints 和 scope。任务目标、问题描述、期望输出、证据摘要这类自然语言
  只用于人类审计和模型理解，不能参与自动 grant query。
- 子代理过程文件：`work/agents/<run_id>/...`。
- 用户最终交付：主代理汇总后写当前 task `output/`，或用户显式指定的输出目录。
- 当前 run 没有用户显式指定输出目录时，`output_files` / `output_refs` / `artifact_refs`
  里的相对路径默认归一到当前 task `output/`；项目文件写入必须来自明确项目路径、
  修复合同、目标 refs 或 `extra_write_roots` 等结构化授权。
- owner projection：`owner_home/agents/<run_id>/` 只保存 refs，用于 tree、compact、恢复和跨 session 查找。
- task rollup：`work/compact/task_rollup.json` 汇总子代理状态和 refs，父代理恢复时先读这里。
- 输出路径合同只接受真实结构化路径。`[name]/file.md` 或 `【name】/file.md`
  这类括号占位符路径段会被过滤出 required refs、declared refs、write roots 和 artifact
  roots；普通自然语言说明可以留给模型阅读，但不能成为机器写入授权。
- `agent_name` 是展示名，不是层级或角色事实。默认展示名使用 `agent-d<depth>-<role>-<index>`；
  深度、权限、模板和状态仍只读结构化字段，不能从显示名、中文叫法或英文别名里反推。
- `role` 选择角色模板时只认明确模板 id；不做“字符串里包含 tester/worker 就套模板”的宽匹配，
  也不再把旧层级别名静默映射成当前模板。
- 层级继承状态写在 `attributes.inherited_parent_context`；`goal` 只承载给模型阅读的任务说明和
  父级边界摘要，不承担机器状态判断。

## Services

| Service | 负责什么 |
|---|---|
| `base.py` | create_run/split、owner 继承、runtime config scope |
| `persistence/` | canonical state 读写、projection、global index、LocalStore 投影 |
| `dispatch/` | dispatch/watch/parent planner 报告 |
| `runner_context_service.py` | 执行上下文、写入边界、任务配置、runtime guidance、runner allowed tools |
| `runner_result_service.py` | runner 输出解析、状态和 artifact refs 写回 |
| `board/` | board、due-check、action-plan（直接导入 `board.service` 等实现模块） |
| `actions/` | action-plan 应用、取消、接管动作记录 |
| `hierarchy/` | 多层级 child scheduling、recovery packet、leadership recovery（直接导入 `hierarchy.service` 等实现模块，包 `__init__` 不再转发） |
| `patch_apply/` | patch review/apply/report/rollback |
| `capability_service.py` | capability request/grant/gap 路由 |
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

主代理可以用 `cancel_subagents` 按 run_id/root/status 取消下级。取消会写 CANCELLED/ABANDONED、废弃 active attempt、尽量 interrupt/terminate 已知 pid/session，并写审计记录。该工具只处理能被当前 canonical loader 正常读取的 run；账本损坏时返回结构化 load error，不私自扫描旧 locator 或其他目录兜底。主代理说明取消/接管原因后，可以继续汇总和验收。

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

## 2026-06-10 Facade 清理

- `services/__init__.py` 不再 re-export 各 service 类；`SubAgentManager` 与所有调用方直接导入实现模块。
- 单模块包 `services/capabilities|runner_context|runner_result/` 打平为 `capability_service.py` / `runner_context_service.py` / `runner_result_service.py`。
- `services/board/__init__.py`、`services/hierarchy/__init__.py`、`parsing/__init__.py` 尾部 re-export 删除；envelope/hierarchy 调用方直连实现模块，消除模块级循环导入。

## 2026-06-10 合约身份去重合并

- `idempotency_contract_identity.py` 与 `repair_contract_identity.py` 合并为
  `contract_identity.py`：两类合约共用同一套 pack 遍历与字符串归一化 helper
  （`_iter_packs` / `_string_tuple` / `_normalized_path`），原来逐字重复三份；
  合并后各自身份计算用 `_idempotency_identity` / `_repair_identity` 区分，
  共享 helper 留一份。调用方（create_context / create_constraints /
  hierarchy/schedule_idempotency）改导入合并后模块，行为不变。

## 2026-06-11 产物落点投影层

- 新增 `services/output_alignment.py`：纯投影、不改 task。`anchored_output_refs(task)`
  产出 OutputAnchoring（anchored_refs / delivery_map / warnings）；
  `anchor_refs_for_execution(task, refs)` 给合同投影层批量翻译目标 refs。
  消费方：`context_bundle_contracts.task_packet/output_contract`（执行合同）、
  `result_artifact_evidence.deliver_anchored_outputs_to_declared`（收尾搬运）、
  `delivery_closeout/subagent_aggregation`（声明对账复用 looks_like_output_path）。
- `agent_core/orchestration/tools/capability.py`：`resolve_capability_requests` 工具
  实现（grant/deny + 安全围栏 + wake）；注册链 core.py → orchestration_tools.py。
