# Subagent：灵感碰撞 / 功能讨论记录

## 为什么想做

subagent 的核心问题不是“能不能多开几个 worker”，而是多 worker 很容易各自判断完成、各自相信自己的结果。真实使用里，用户常常只想表达目标，不想手写复杂派工模板；系统却必须把质量标准、写入边界、测试要求和最终验收权传清楚。

## 讨论过什么

- 子代理应该从“独立负责人”降级为“受控施工队”，只提交待审核材料。
- 父会话保留最终验收权，不能直接相信 worker 的 `PASS` 或自述完成。
- `QualityContract`、`ContextManifest` 和 workflow 模板要把目标质量、上下文边界、证据要求结构化。
- `auto/manual/off` 让用户可以少说，也可以关闭自动派工。

## 痛点

- worker 拿到动作目标，却没拿到“什么才算好”的标准。
- 只看文件存在、命令成功、页数匹配，容易漏掉真实交付质量。
- 多个 worker 并行时，如果没有写入范围和父级验收门，会互相覆盖或重复判断。
- 成功样本只留在聊天里，后续任务难以复用。

## 方向

第一版方向是先让派工可解释、可审查、可回放。当前 dry-run 规划已经接到真实 subagent 创建和 worker 子工单物化路径；下一段重点是把解释、验收报告、失败样本和高风险确认继续做厚。

## 相关旧文档

- [docs/design/subagent-quality-contract.md](../../design/subagent-quality-contract.md)
- [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)
- [SUBAGENT_RUNBOOK.md](../../../SUBAGENT_RUNBOOK.md)
- [TEST_CHECKLIST.md](../../../TEST_CHECKLIST.md)

## 当前第一版索引 / 待补齐

本页只是把散在旧文档里的讨论整理成入口。后续需要把真实失败样本、成功样本和每轮 workflow 取舍补成更细的时间线。
