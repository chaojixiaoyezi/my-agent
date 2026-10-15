# S07 UNKNOWN 恢复权限审查修复

## 基本信息

独立工作树 s07-authority-review-20261014，基于根当时 c0cabbb21 及未提交 S07 代码快照；不改根工作树，不提交、推送或部署。

## 目标与实际完成

修复两个由生产 API 隔离复现的问题：同 attempt UNKNOWN 核对后取消仍能结算或重开；已有终态 operation 只比工具参数和幂等键，另一 root 能重放原任务结果。

`assert_reconciliation_completion` 现在对同 attempt、跨 attempt 和无核对标记的原结算统一校验 current attempt/generation、workspace epoch、TaskRun 终态，并调用原 `_require_running_authority` 检查 AgentRun/Attempt。跨 attempt 的只读恢复标记与原执行身份保持不变。`reopen_tool_operation_after_reconciliation` 在修改任何行、锁或 mutation 前调用同一 authority 门；失败保持原 UNKNOWN。

`_existing_operation_decision` 返回任何已有操作前，通过原 operation→agent_run→task_run→task 和调用 attempt JOIN，核对真实 owner/run/root 与同一 agent_run；不能用 request.owner/run/task 给旧结果重贴身份。合法同 root 当前新 attempt 仍可重放原终态。

## 文件范围

- runtime_db/operation_reconciliation_authority.py：共享活跃 authority 与已有操作真实归属校验。
- runtime_db/managed_operation_store.py：重开复用 authority，已有行先核真实归属。
- tests/test_managed_operation_recovery_fences.py：生产 API 回归，不用 SQL 写造身份状态。
- 本交接和 gateway 模块进度文档。

补丁必须相对 `/tmp/s07-before` 快照生成，不包含独立树借用的其它根改动。

## 验证

最初 `/tmp/test_s07_independent_audit.py` 的 3 个安全预期由失败转为通过。新增 19 个回归覆盖 AgentRun/TaskRun 的 done/failed/cancelled 后 settle/reopen 拒绝，原 UNKNOWN 与 generation 不变；成功/失败终态的跨 owner/root 与同名 run 不同执行链拒绝；同 root 新 attempt 合法终态重放成功。

无真实供应商或外部请求，所有写入位于临时 RuntimeDB。原未知 append 恢复分支仍不返回 safe_to_retry，不能换 call/operation 编号重发；明确供应商拒绝与未知结果的分类未修改。

## 建议下一步

根应用相对快照的最小增量后，重跑 S07 focused tests 和最初 3 个独立反例，再跑严格 gate。其它代理可并行只读复核共享 authority 的取消时序；不要以自然语言、旧响应或签名引用代替当前运行权，也不要改动未知 append 防重发规则。
