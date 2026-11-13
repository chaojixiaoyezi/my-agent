# 工作流有界重新规划

当前为本地候选实现，部署版本及集成验收由交付回执记录。

## 显式元数据合同

工作流通过 `descriptor.metadata.workflow_rework_policy` 选择启用。原 ORCH 审核的结构化 `changes_requested` 触发此能力。

```json
{
  "workflow_rework_policy": {
    "replan_step_id": "architecture",
    "max_rounds": 3,
    "max_no_progress_rounds": 2
  }
}
```

原 `WorkflowDefinition` 在构造和恢复时校验策略。

- `replan_step_id` 引用同一冻结定义中的无状态 `llm_skill_analysis` 步骤，显式设置 `analysis_isolation` 为 `stateless_skill`。该步骤须独立于强制审核步骤，且只包含分析能力。
- 两项轮数均为 1～100 的整数，缺省分别为 3 和 2。非法类型、数值及未知字段返回具体校验错误。
- 策略随原运行快照冻结。内置开发阶段草案选择该策略，简单分析模板沿原分析流程执行。

## 原恢复链中的行为

恢复时先核验取消、当前权限及未知副作用。审核反馈为 `changes_requested` 且失败步骤尚未启动副作用时，才进入重新规划。选定分析步骤继续通过原启用状态和 Skill 权限检查。

每次显式恢复最多执行一轮原无状态分析 handler，使用原 core 执行轮、全局模型准入和输入预算。分析上下文来自原运行输入及已成功步骤输出。结果保存于失败步骤的 `escalation_result.rework_state`，随后暂停并要求修订真实候选、取得新审核。

调用分析器前持久化轮数和准确 attempt。原控制操作的幂等重放复用已保存结果。已成功步骤的输出与执行回执继续有效。

无进展计数使用候选 hash、冻结范围及业务上下文的结构化摘要。首次建立基线，随后每次相同摘要累计一轮。达到上限时，返回 `WORKFLOW_REWORK_LIMIT_REACHED` 或 `WORKFLOW_REWORK_NO_PROGRESS`，保持暂停并给出修订候选的下一步动作。

模型失败以 `WORKFLOW_REPLAN_FAILED` 保留脱敏原因、具体错误码和已消耗轮数。规划成功以 `WORKFLOW_REPLAN_READY` 表示计划已保存，业务执行仍须通过原审核与工具授权。

## 原审核与执行证据

`patched_step_output` 用于原修复流程。对待审且尚未执行的步骤使用它时，返回 `WORKFLOW_REVIEW_PATCH_FORBIDDEN`。可信操作的已提交结果继续沿原核验路径恢复。

审核候选和最低要求取自冻结定义。候选、范围或 attempt 变化后须重新审核。工具权限和生产凭证沿原授权入口核验。程序修改、DAG 编排和发布通过各自已有资产与工具入口执行。

## 验证范围

确定性测试覆盖总轮数与无进展上限、成功写入前缀只执行一次、控制幂等重放、取消、未知副作用核验、启用状态、策略校验、补丁绕审拒绝以及冻结审核策略。

规划测试使用受控分析器。原生审核入口另外通过真实 loopback HTTP 和原 ToolCall 往返测试。真实部署后的组合流程验收仍由集成回执记录。
