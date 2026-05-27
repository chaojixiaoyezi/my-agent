# Subagent Progress

父验收旧链路已经移除。子代理只负责执行自己的任务、写结果和证据引用；上级通过任务树、状态、refs 和普通 closeout 继续推进。

## 2026-05-27 调度卡点清理

- 删除 orchestration 最终回答硬门、dispatch 本地收口、runner artifact integrity 收尾改写和 hierarchy scope/duplicate 硬门。
- `dispatch_subagents` 只把索引、状态和 refs 交回给父级；系统不再替父级生成完成/失败结论。
- 层级 scheduler 只负责按父级显式 spec 创建子任务，不再因为领域、重复、QA 顺序或写根漂移直接阻断。

## 2026-05-27 产物缺失不再改写 runner 状态

- 删除 `result_artifact_integrity.py`。子代理在结构化结果里声明了某个本地产物 ref，但文件暂时不存在时，runner 不再直接改成 `BLOCKED/missing_artifact_refs`。
- 删除 artifact integrity 专用 repair advice。调度返回里不再生成 `artifact_integrity_repair_advice` 或“创建专门修复子代理”的固定建议。
- 产物缺失、路径不对、内容损坏和格式不合格，统一回到普通 closeout / 父级模型判断 / 任务树状态里处理。

## 2026-05-27 最近工具轨迹

- runner 工具观测会把最近 5 条工具调用摘要写入任务 `attributes.recent_tool_trace`。它只记录工具名、是否成功、时间、摘要和可选路径，不保存大正文。
- `inspect_agent_tree` 会把这些摘要放进节点的 `recent_tool_trace` 和 `evidence_layer.recent_tool_trace`。父代理可以据此判断子代理是否还在真实推进，而不用读取完整模型对话或大产物。
- 这不是新门，也不改变任务状态；工具失败、产物缺失和返工仍由普通状态、blockers、refs 和 closeout 处理。
