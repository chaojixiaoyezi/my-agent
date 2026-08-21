# Subagent：灵感碰撞 / 功能讨论记录

## 为什么想做

subagent 的核心问题不是“能不能多开几个 worker”，而是怎样让父子关系递归一致、创建后自动运行、结果能
回到直接父级，同时不让多套调度和验收状态互相打架。真实使用里，用户只表达目标；运行时负责身份、权限、
写入边界、并发和结束原因。

## 讨论过什么

- 主代理对子代理、子代理对孙代理都使用同一个 `create_subagents`；创建后宿主自动启动。
- 父级可以查看、发 guidance、取消/中断；不需要模型再调用一次派工或调度工具来“推动”。
- 普通完成由模型自然结束并写统一 `turn_end`；工具、权限、路径和产物存在性继续由结构化事实保证。
- 普通用户只说目标；模型在同一 thread 里按需使用 Skill、`task_progress` 和原生子代理工具，不靠隐藏模式。

## 痛点

- 旧模型工具同时存在 create、dispatch、schedule，容易重复创建、空等或卡在“还没推进”。
- 机器质量验收会与模型结论相悖，历史上多次把已完成任务粗暴打断。
- 多个 worker 并行时，如果没有写入范围和身份边界，会互相覆盖。
- 成功样本只留在聊天里，后续任务难以复用。

## 方向

当前方向是让协作可解释、可审查、可回放。方法知识由 Skill 提供，计划保存在当前任务的
`task_progress`；所有层级只用 `create_subagents` 创建不同分工，宿主自动启动并把完成事件送回直接父级。
不再维护模型可见的 dispatch/schedule 或会复制计划和上下文的 workflow 模板执行器。

## 相关旧文档

- [docs/design/subagent-quality-contract.md](../../design/subagent-quality-contract.md)
- [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)
- [SUBAGENT_RUNBOOK.md](../../../SUBAGENT_RUNBOOK.md)
- [TEST_CHECKLIST.md](../../../TEST_CHECKLIST.md)

## 当前第一版索引 / 待补齐

本页只是把散在旧文档里的讨论整理成入口。后续真实失败样本和成功样本继续进入模块进展与回归测试，不另建 workflow 历史事实源。
