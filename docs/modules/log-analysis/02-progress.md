## 2026-05-06 code-size guard cleanup
- Split work-order planning dataclasses from `dispatch/work_orders/planning.py` into `dispatch/work_orders/models.py`.
- Split detector entity comparison and entity extraction helpers from `analytics/detectors/classifiers.py` into `analytics/detectors/classifier_entities.py`.
- Split trace-case query assembly from `tools/query_functions.py` into `tools/query_trace.py` while keeping the public tool API stable.
- Current strict code-size gate reports `hard=0`, `soft=0`; near-soft items are now visible as high-risk warnings.
# Log Analysis：开发推进记录

## 已完成

- SecurityAlertV1 CSV / JSONL 接入、checkpoint、dedup、dead letter 已有基础实现。
- 本地 JSONL store、受控 query、hunt、trace 已接入 CLI 和工具层。
- 软检测器、case merge、route draft、first response report、forensic package 已有基础链路。
- `logs/security` capability 控制安全工具暴露，普通任务默认隐藏安全工具。
- runtime capability、query limit、storage audit、Live Lab replay、LOG analyst work orders 已有落地记录。
- analyst/reviewer work-order plan 现在可以显式 `apply=True` 落成真实 `SubAgentTask` 记录，但不会调用 runner、模型或自动验收。
- `tools.py`、`doctor.py`、`config.py` 已补齐 `LLM:` / `新手说明:` / `参数说明:` / `返回说明:` 风格的中文注释。
- `storage/__init__.py` 使用 lazy import 解决 `query.py → cases.evidence → storage.base` 循环依赖。

## 解决的问题

- LOG case 不再停留在“有一份派工计划”：父会话确认后可以生成真实、可追踪、可复核的子代理任务记录。
- 默认仍是 dry-run，避免用户没确认时就创建任务。
- 无 evidence refs 的 case 会拒绝创建 analyst/reviewer 任务，减少无证据分析。
- 创建出的任务带有 allowed tools、evidence refs、quality contract、context pack、父级最终验收门，解决 worker 自己宣布完成的问题。
- 任务保持 `PLANNING` / `UNVERIFIED`，解决“创建任务”和“真正执行/验收”混在一起的风险。
- LOG 的工具边界、体检边界和配置边界现在更适合小白学习，也更方便后续 LLM 在不重新扫全代码的情况下理解参数含义。
- 注释明确了 security tools 只返回摘要和 evidence refs、doctor 不加载重依赖、配置坏值会 warning 后回退安全默认值。

## 下一步

- 用受控 evidence-ref reader 替换 placeholder `evidence_read`。
- 给 LOG work-order apply path 增加 CLI 或父会话确认入口。
- 在真实 analyst/reviewer 执行前，把 reviewer decision 和 parent acceptance report 的持久化格式定下来。
- 增加更丰富的安全 fixture 和回归场景。
- 继续明确 JSONL dev/local 后端与未来 DuckDB / Parquet / SQLite 后端的边界。
- 继续给 `models.py`、`storage/query.py`、`ingest/`、`analytics/` 等核心文件补同等级中文注释。

## 已跑测试

- 历史记录显示 LOG 专项、CLI、tools、detector、model、dispatch、Live Lab replay 都已有多轮测试记录。
- 相关旧记录见 [docs/design/log-analysis.md](../../design/log-analysis.md)、[ACCEPTANCE.md](../../../ACCEPTANCE.md) 和 [EVIDENCE.md](../../../EVIDENCE.md)。
- 本轮 focused 验收已覆盖 LOG dispatch：`python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py` -> `18 passed`。
- 父会话 focused 组合验收：`python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `26 passed`。
- 父会话全量回归：`python -m pytest` -> `236 passed`。
- 空白检查：`git diff --check` -> passed。
- 本轮注释同步 focused 验收：`python -m pytest agent_py_agent\tests\test_log_analysis_models.py agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_cli.py agent_py_agent\tests\test_tools.py agent_py_agent\tests\test_doc_sync.py` -> `45 passed`。
- 本轮同步门检查：`python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`。
- 本轮全量回归：`python -m pytest` -> `241 passed`。

## 未跑测试

- 本轮没有真实执行 analyst/reviewer runner，因为当前目标只是创建受控任务记录。
- 本轮注释工作没有重新跑真实 LOG Live Lab replay；如后续改 replay 行为，需要补跑。
- 后续如果修改 storage 后端或 CLI 行为，需要补跑 LOG CLI、Live Lab replay 和全量 pytest。

## 风险

- 第一版四件套还没有搬入旧文档全文，查细节仍要跳转到旧 design/backlog/evidence。
- 当前 apply path 只创建任务记录，不代表 analyst 已经工作，也不代表父级验收通过。
- `evidence_read` 仍是待落地的受控读取能力，真实 analyst 执行前必须补齐。
- JSONL 本地后端适合开发和小样本，不应被误解为生产 SIEM 存储。
## 2026-05-06 code-size cleanup
- Refactored log-analysis prompts, ingestion, dispatch, parser, and security helpers into smaller internal units.
- Cleared current log-analysis 80% code-size high-risk findings while preserving public call surfaces and test behavior.
- Verified with log-focused pytest, ruff, and the global code-size report.
