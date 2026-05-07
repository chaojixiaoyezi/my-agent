## 2026-05-06 structure update
- `subagents/models.py` is now a compatibility facade over `model_capabilities.py`, `model_records.py`, `model_runtime.py`, and `model_task.py`.
- `subagents/result_processors.py` delegates structured output handling to `result_structured.py` and output payload assembly to `result_payloads.py`.
- `subagents/manager_base.py` delegates work-order filesystem concerns to `manager_work_orders.py`.
- `SubAgentTask` now carries task-tree control-plane fields: `StatusReport`, progress/current step/latest summary, blockers, artifact/evidence refs, evidence packets, findings, and checkpoint refs.
- Each persisted task can write `reports/status_report.json`; report board items include child status counts, evidence/finding counts, progress, latest summary, and blocker count.
- Acceptance review records now expose layered fields: worker claims, evidence facts, parent conclusions, and verifier checks.
- Action plan/apply records now expose rescue/escalation metadata: trigger, strategy, escalation target, and context refs.
- Each persisted task now writes compact/checkpoint recovery artifacts under `reports/`: `checkpoint.json`, `decision_ledger.json`, `progress.md`, `failing_tests.json`, and `next_actions.json`.
- Each persisted task now creates `SKILL_SPARKS.md` as a task-local skill learning candidate file; it is not main memory and is not auto-promoted.
- Each persisted task now mirrors a Phase 0 runtime memory task workspace under `tasks/<root_id>/`, with `state.json`, `timeline.jsonl`, shared/artifact folders, and a legacy run adapter pointing back to the old work-order directory.
- Each persisted task now mirrors a Phase 1 runtime memory agent run workspace under `tasks/<root_id>/agents/<run_id>/`, with agent identity, run state, task brief, timeline, checkpoint, summary, final report, findings, inbox/outbox, artifacts, and compactions skeletons.
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
- `agent_py_agent/agent/subagents/model_task.py`：承接 `SubAgentTask`、`EvidencePacket`、`Finding`、`StatusReport` 等任务树和证据合同模型，`models.py` 继续作为兼容导出入口。
- `agent_py_agent/agent/subagents/manager_indexing.py`：实现 `_select_runs()` 等索引和过滤逻辑，同时提供公开别名 `select_runs()`、`index_task()` 等。
- `agent_py_agent/agent/subagents/manager_acceptance_findings.py`：实现验收发现逻辑，公开别名 `acceptance_findings()`。
- `agent_py_agent/agent/subagents/acceptance_review_service.py`：对单个任务做父级验收，生成分层 acceptance record，并执行只读 verifier checks。
- `agent_py_agent/agent/subagents/acceptance_helpers.py`：验收发现子模块的兼容导出入口，真实实现已拆到 `acceptance_helpers/` 目录。
- `agent_py_agent/agent/subagents/acceptance_helpers/evidence_acceptance_findings.py`：承接证据存在性和 read/write_file 工具证据 findings 构建逻辑，保持 `evidence.py` 作为薄兼容入口。
- `agent_py_agent/agent/subagents/manager_patch.py`：实现补丁操作，公开别名 `resolve_patch_target()`。
- `agent_py_agent/agent/subagents/manager_runner_results.py`：负责 runner 结果写回、状态降级和 `output.json` / `runner_result.json` 持久化；结构化解析失败时会把最终结果统一降级成 `BLOCKED` / `ok=False`。
- `agent_py_agent/agent/subagents/manager_runner_result_payload.py`：承接 runner result payload/status 构建 dataclass 和纯 helper，让 manager facade 保持短小。
- `agent_py_agent/agent/subagents/services/persistence.py`：负责 task/run/status report 落盘和旧任务兼容归一化，生成 `reports/status_report.json`。
- `agent_py_agent/agent/subagents/services/checkpoint_artifacts.py`：从 task facts 和 `output.json` 构建 compact 可读恢复包，包含 checkpoint、decision ledger、progress、failing tests 和 next actions。
- `agent_py_agent/agent/subagents/services/task_workspace_adapter.py`：把 runtime memory task workspace 路径同步回 `SubAgentTask`，避免 persistence 保存函数继续膨胀。
- `agent_py_agent/agent/memory_archive/task_workspace.py`：subagent 保存路径调用的 runtime memory adapter；创建 `tasks/<root_id>/` task workspace skeleton 和 `agents/<run_id>/legacy_run_ref.json`，但不移动旧工单目录。
- `agent_py_agent/agent/memory_archive/agent_run_workspace.py`：创建 `tasks/<root_id>/agents/<run_id>/` 下的 agent run workspace skeleton；旧工单目录仍是兼容读写面，run workspace 先承接恢复、接管、finding 和 compact 链的后续入口。
- `SKILL_SPARKS.md`：子代理目录里的经验火花候选，只记录可复用步骤、触发条件、证据引用、限制和反例；后续提升为 skill 必须经过单独 gate。
- `agent_py_agent/agent/subagents/services/board.py`：把任务树节点转成 report board item，并汇总 child status、progress、summary、evidence/finding/blocker 计数。
- `agent_py_agent/agent/subagents/services/rescue_policy.py`：根据 due-check issue 给 action plan 添加 rescue/escalation 元数据，保持建议可审计但不自动越权执行。
- `agent_py_agent/agent/subagents/result_structured.py`：解析 runner structured output 中的 evidence packets、findings、artifacts、tests、blockers，并写回任务事实。
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
6. runner 根据 execution context 调模型和工具，把 `RUNNER_RESULT.md`、`reports/runner_result.json`、`reports/status_report.json`、`reports/checkpoint.json`、`reports/progress.md`、`SKILL_SPARKS.md`、`output.json` 写回旧 run 工单目录。
7. structured output 中的 `evidence_packets` / `findings` 会进入任务事实源；acceptance 会检查完成态结果是否有 evidence chain。
8. acceptance report 分层记录 worker 自述、证据事实、父级结论；verifier checks 只读 evidence packets / findings 并能阻断未解决风险。
9. due-check 把 blocked、timeout、stale heartbeat、capability request/gap 等问题转成 action plan，并附带 rescue/escalation 元数据。
10. persistence 同步 `tasks/<root_id>/state.json`、`timeline.jsonl`、`summaries/current_summary.md`、`shared/`、`artifacts/` 和 `agents/<run_id>/legacy_run_ref.json`，为后续正式 agent run workspace 做兼容桥。
11. persistence 同步 `tasks/<root_id>/agents/<run_id>/agent.yaml`、run `state.json`、run `timeline.jsonl`、`task.md`、`checkpoint.json`、`summary.md`、`final_report.md`、`findings.jsonl` 和 inbox/outbox/artifacts/compactions 目录，先形成 agent run workspace skeleton。
12. `memory-resume` 在跨天恢复时用 archive/LocalStore 作为线索，最终推荐读取任务目录里的事实源和 checkpoint artifacts，再由父级决定是否验收；这只是恢复入口推荐，不代表子代理写入主代理长期 memory。
13. workflow preview 仍可通过 CLI dry-run 展示；真实路径已接入 `create_run(... workflow_mode="plan|auto")` 和 `subagents-dispatch --apply --workflow-mode auto`，可把父任务上的 `workflow_plan` 物化为 worker 子工单。LOG 专项 apply path 和 runner 恢复 scenario 继续作为真实任务记录的先行验证样本。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/subagents.py`，理解用户命令怎么进入程序。
2. 再看 `planner.py`，理解一个“规划结果”包含哪些部分。
3. 再看 `router.py`，学习如何把自然语言目标映射到模板。
4. 再看 `compiler.py`，学习模板怎样变成具体工作单。
5. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py::test_scenario_parent_subagent_cross_day_resume_uses_runner_task_facts`，理解 runner 写回后如何跨天恢复到任务事实源。
6. 再看 `agent_py_agent/tests/test_subagent_persistence_service.py` 和 `agent_py_agent/tests/test_result_processors_edges.py`，理解 status report、evidence packets 和 findings 如何写回。
7. 最后看 `agent_py_agent/tests/test_subagent_workflow_*.py`，理解怎么证明路由、模板和编译没有坏。

## 当前第一版索引 / 待补齐

本页先解释主结构和学习路径。更细的状态机、workflow 子工单依赖、status report 索引范围和验收阻断细节，后续仍需要继续补齐。
## 2026-05-06 structure update
- Workflow routing and subagent manager internals now separate decision fields, rendering sections, patch normalization, and service actions.
- Compatibility modules still re-export the existing public model and rendering names for callers.
