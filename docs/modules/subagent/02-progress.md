# Subagent Progress

父验收旧链路已经移除。子代理只负责执行自己的任务、写结果和证据引用；上级通过任务树、状态、refs 和普通 closeout 继续推进。

## 2026-05-27 调度卡点清理

- 删除 orchestration 最终回答硬门、dispatch 本地收口、runner artifact integrity 收尾改写和 hierarchy scope/duplicate 硬门。
- `dispatch_subagents` 只把索引、状态和 refs 交回给父级；系统不再替父级生成完成/失败结论。
- 层级 scheduler 只负责按父级显式 spec 创建子任务，不再因为领域、重复、QA 顺序或写根漂移直接阻断。
