# Subagent Structure

当前结构不再包含独立父验收层。`create_subagents` 创建任务节点，`dispatch_subagents` 推进 runner 并返回索引和状态；任务完成后的产物检查统一由普通 closeout / 交付检查处理。

## 任务树

任务树是系统账本，不是子代理自己写出来的产物。`SubAgentManager.save()` 每次保存任务时都会从 `SubAgentTask` 的机器字段重建 `attributes.system_tree`，包括 `run_id`、`root_id`、`parent_id`、`child_ids`、状态、进度、artifact refs 和 evidence refs。即使模型在结果里写了一个假的 `system_tree`，保存链路也会覆盖成系统派生值。

子代理应该做的是写自己的结果、证据引用和必要的工作文件；父级或主代理通过 `inspect_agent_tree`、`subagent_board`、`output_json` 和 refs 看状态，不要求子代理手动维护树。

## Closeout

默认配置 `closeout_for_all_task_nodes: false`，表示只对主/root 交付做普通 closeout，子代理结果只更新状态、refs 和任务树。

如果显式改成 `true`，每个子代理、孙代理等任务节点完成时会写 `attributes.task_node_closeout`，内容来自同一份 runner 结果事实：状态、verification、artifact refs、evidence refs、blockers、findings 和 next actions。它只是返工提示和可观察事实，不创建父代理验收状态，不引入 acceptance 角色，也不把子代理卡进新的等待态。
