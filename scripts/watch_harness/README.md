# Watch/Audit 数据与诊断工具

本目录只保留可复用的数据生成、来源协议、覆盖对账和局部诊断工具。它们可以帮助复现边界问题，
但不能代替正式产品验收。

正式 `/audit` 验收必须从 my-agent 的 CLI 或真实 IM 入口创建任务。产品底座按每个实际 `watch_id`
幂等建立一个逻辑来源工作者，保证主 Agent 不直接消费原始队列；除此之外，由同一个 Agent runtime
自主决定调查、复核与汇总是否继续委派、委派多少子代理以及如何分析。测试程序不得直接调用供应商
接口充当 Agent，也不得另造固定角色、固定调查拓扑或固定处理顺序。

## 当前可用工具

- `multi_source_simulator.py`：生成多个异构 HTTP 游标源和独立 answer key。
- `messy_source_simulator.py`：生成字段和记录边界不规整的数据源。
- `content_source_simulator.py`：生成需要读取完整正文才能判断的中性样本。
- `watch_feeder.py`：向文件持续追加完整记录。
- `fleet_score.py`、`watch_report_audit.py`、`miss_attribution.py`：独立对账模型报告与隐藏答案，
  不参与模型判断。
- `content_engine_harness.py`、`restart_recovery_harness.py`、`latency_and_overload_harness.py`：
  只验证采集、完整记录、积压和恢复等数据面边界。
- `batch_vs_single_probe.py`、`content_judge_probe.py`、`few_shot_learning_probe.py`、
  `real_model_judge_probe.py`：模型质量诊断探针，不是产品 Agent 的端到端证明。

## 正式验收边界

1. 测试样本和 answer key 可以由本目录生成，但 answer key 不得进入 Agent 上下文。
2. 任务只能通过正式 CLI/Gateway/IM 入口提交。
3. 模型根据用户目标和工具事实自主决定分析、复核、委派与汇报。
4. 程序只验收 Schema、权限、owner 隔离、完整记录、游标、幂等、持久化、取消、恢复、
   `source_ref`、覆盖账和最终对账。
5. `/audit` 必须复用统一 Agent、Memory、Compact、工具和子代理系统。
6. 性能或模型质量结果必须同时报告吞吐、积压、覆盖、召回和误报，不能把“没有丢账”等同于
   “判断准确”或“实时”。

当前验收计划和已证实事实见
[`docs/tasks/AUDIT_GUARANTEE_ACCEPTANCE.md`](../../docs/tasks/AUDIT_GUARANTEE_ACCEPTANCE.md)。
