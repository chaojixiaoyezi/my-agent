# Subagent Structure

当前结构不再包含独立父验收层。`create_subagents` 默认创建并立即启动任务节点；只有显式 `defer_start=true` 才只建不跑。`dispatch_subagents` 用于运行中追加提示、人工催办、推进卡住项、重跑指定 run、查状态并尝试恢复；任务完成后的产物检查统一由普通 closeout / 交付检查处理。

`create_subagents` 的“立即启动”是后台启动：工具调用本身只负责创建 run、写入后台启动标记、拉起 runner 调度线程，然后立刻把 `run_ids`、启动状态和任务树快照返回给父代理。父代理不会同步等待所有子代理完成，因此可以继续和用户对话、继续规划，或稍后用 `inspect_agent_tree` / `subagent_board` 查看进展。

## 任务树

任务树是系统账本，不是子代理自己写出来的产物。`SubAgentManager.save()` 每次保存任务时都会从 `SubAgentTask` 的机器字段重建 `attributes.system_tree`，包括 `run_id`、`root_id`、`parent_id`、`child_ids`、状态、进度、artifact refs 和 evidence refs。即使模型在结果里写了一个假的 `system_tree`，保存链路也会覆盖成系统派生值。

子代理应该做的是写自己的结果、证据引用和必要的工作文件；父级或主代理通过 `inspect_agent_tree`、`subagent_board`、`output_json` 和 refs 看状态，不要求子代理手动维护树。

调度层只创建和推进任务节点，不再用额外 scope/duplicate/QA 硬门替父级做流程裁决。需要流水线、去重或重试时，由父级根据树状态和任务目标显式安排下一步。

## 三层可观察状态

`inspect_agent_tree` 的每个节点都提供同一套只读三层状态：

- `liveness`：状态、心跳时间、更新时间，用来判断代理是否还活着。
- `progress_layer`：进度、当前工具、当前步骤、最近进展摘要，用来判断是否真的在推进。
- `evidence_layer`：产物 refs、证据 refs、blockers、能力缺口和最近工具轨迹，用来判断结果在哪里、哪里卡住、需要谁补能力。

这三层都是观察事实，不触发调度、不执行验收、不阻断任务。父代理看到异常后可以自己决定催办、补派、接手、汇报或等待。

## Closeout

默认配置 `closeout_for_all_task_nodes: false`，表示只对主/root 交付做普通 closeout，子代理结果只更新状态、refs 和任务树。

如果显式改成 `true`，每个子代理、孙代理等任务节点完成时会写 `attributes.task_node_closeout`，内容来自同一份 runner 结果事实：状态、verification、artifact refs、evidence refs、blockers、findings 和 next actions。它只是返工提示和可观察事实，不创建父代理验收状态，不引入 acceptance 角色，也不把子代理卡进新的等待态。

## Runner 结果

runner 结构化结果只负责把状态、summary、artifacts、evidence packets、findings、能力申请和 next actions 合并回任务账本。它不再额外执行“声明的本地产物路径必须存在，否则改写为 BLOCKED”的专用完整性门。

父级看到子代理结果后，可以按 refs 读取产物、继续调度、要求返工或提交 closeout。系统不再在 runner 层单独制造 `artifact_integrity_repair_advice`，避免和统一 closeout 形成两套验收/返工机制。
