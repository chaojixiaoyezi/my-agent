# Subagent：开发推进记录

## 已完成

- workflow 配置和开关已落地：`auto`、`manual`、`off`。
- 内置 workflow 模板、模板加载、覆盖和校验已有基础实现。
- `QualityContract`、`ContextManifest`、`context_packs` 已进入 subagent 任务结构。
- workflow router、compiler、parent acceptance planner 已有 dry-run 规划链路。
- `my-agent subagents-workflow-plan "<goal>"` 可预览 worker 拆分和父级验收清单。
- LOG 模块已经验证了一条专项 apply path：把受控 work-order plan 落成真实 `SubAgentTask`，但不自动执行。

## 解决的问题

- workflow 不再只是“多开 worker”的口头约定，而是能把质量标准、上下文包、写入边界和父级验收显式写出来。
- 用户可以少说任务怎么拆，系统先用模板和 dry-run 预览补足常见派工结构。
- worker 自述完成不会直接变成最终完成，父级验收门被放进计划和任务结构里。
- 文档四件套给后续模块讨论、推进、初心、结构说明提供固定位置，减少散乱文档继续膨胀。

## 下一步

- 把通用 workflow dry-run 规划接入真实 subagent 创建路径；LOG 专项 apply path 已先行验证，但还不是通用入口。
- 将已保存的 workflow preview 附到真实 dispatch 记录。
- 持久化结构化 acceptance report，区分 worker 自述和父级验收结论。
- 补更多内置 workflow 模板和失败样本回归。

## 已跑测试

- 历史记录显示 subagent workflow 专项测试已覆盖配置、模板、质量契约、router、compiler、planner、CLI preview 等路径。
- 相关旧记录见 [docs/design/subagent-quality-contract.md](../../design/subagent-quality-contract.md) 和 [DESIGN_LEDGER.md](../../../DESIGN_LEDGER.md)。
- 本轮 subagent 模块本身只新增文档索引；代码变更发生在 LOG dispatch 的专项 apply path。
- 父会话 focused 组合验收：`python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `26 passed`。
- 父会话全量回归：`python -m pytest` -> `236 passed`。
- 空白检查：`git diff --check` -> passed。

## 未跑测试

- 当前尚未为通用 workflow apply path 增加测试，因为本轮只做 LOG 专项任务创建。
- 后续如果接入真实 dispatch，需要补跑 subagent workflow 专项测试和全量 pytest。

## 风险

- 旧文档里已有大量 subagent 设计细节，第一版索引还没有逐段拆入四件套。
- dry-run 到真实创建之间仍有产品风险：什么时候需要用户确认、怎么展示自动选择理由，还需要继续验证。
- 并行 worker 可能同时补文档，后续需要以模块四件套为主入口，避免再次分散。
