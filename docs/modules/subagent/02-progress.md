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
