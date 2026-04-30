# Log Analysis：开发推进记录

## 已完成

- SecurityAlertV1 CSV / JSONL 接入、checkpoint、dedup、dead letter 已有基础实现。
- 本地 JSONL store、受控 query、hunt、trace 已接入 CLI 和工具层。
- 软检测器、case merge、route draft、first response report、forensic package 已有基础链路。
- `logs/security` capability 控制安全工具暴露，普通任务默认隐藏安全工具。
- runtime capability、query limit、storage audit、Live Lab replay、LOG analyst work orders 已有落地记录。
- analyst/reviewer work-order plan 现在可以显式 `apply=True` 落成真实 `SubAgentTask` 记录，但不会调用 runner、模型或自动验收。

## 解决的问题

- LOG case 不再停留在“有一份派工计划”：父会话确认后可以生成真实、可追踪、可复核的子代理任务记录。
- 默认仍是 dry-run，避免用户没确认时就创建任务。
- 无 evidence refs 的 case 会拒绝创建 analyst/reviewer 任务，减少无证据分析。
- 创建出的任务带有 allowed tools、evidence refs、quality contract、context pack、父级最终验收门，解决 worker 自己宣布完成的问题。
- 任务保持 `PLANNING` / `UNVERIFIED`，解决“创建任务”和“真正执行/验收”混在一起的风险。

## 下一步

- 用受控 evidence-ref reader 替换 placeholder `evidence_read`。
- 给 LOG work-order apply path 增加 CLI 或父会话确认入口。
- 在真实 analyst/reviewer 执行前，把 reviewer decision 和 parent acceptance report 的持久化格式定下来。
- 增加更丰富的安全 fixture 和回归场景。
- 继续明确 JSONL dev/local 后端与未来 DuckDB / Parquet / SQLite 后端的边界。

## 已跑测试

- 历史记录显示 LOG 专项、CLI、tools、detector、model、dispatch、Live Lab replay 都已有多轮测试记录。
- 相关旧记录见 [docs/design/log-analysis.md](../../design/log-analysis.md)、[ACCEPTANCE.md](../../../ACCEPTANCE.md) 和 [EVIDENCE.md](../../../EVIDENCE.md)。
- 本轮 focused 验收已覆盖 LOG dispatch：`python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py` -> `18 passed`。
- 父会话 focused 组合验收：`python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `26 passed`。
- 父会话全量回归：`python -m pytest` -> `236 passed`。
- 空白检查：`git diff --check` -> passed。

## 未跑测试

- 本轮没有真实执行 analyst/reviewer runner，因为当前目标只是创建受控任务记录。
- 后续如果修改 storage 后端或 CLI 行为，需要补跑 LOG CLI、Live Lab replay 和全量 pytest。

## 风险

- 第一版四件套还没有搬入旧文档全文，查细节仍要跳转到旧 design/backlog/evidence。
- 当前 apply path 只创建任务记录，不代表 analyst 已经工作，也不代表父级验收通过。
- `evidence_read` 仍是待落地的受控读取能力，真实 analyst 执行前必须补齐。
- JSONL 本地后端适合开发和小样本，不应被误解为生产 SIEM 存储。
