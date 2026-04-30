# EVIDENCE

- 配置文件：`agent_py_agent/config/agent_config.yaml`
- CLI 入口：`agent_py_agent/__main__.py`
- 核心模块：`agent_py_agent/agent/*.py`
- README：`agent_py_agent/README.md`
- 测试脚本：`agent_py_agent/tests/run_tests.py`
- 测试日志：`validation/test-report.txt`
- API 配置回归测试：`validation/test-report-api-config.txt`
- HTTP 后端测试：`agent_py_agent/tests/test_backends.py`
- HTTP 后端回归日志：`validation/test-report-http-backends.txt`
- 循环智能体测试：`validation/test-report-chat-loop.txt`

## 2026-04-30 / 日志分析第一版验收证据

- 阶段提交：`97b8bc4 Add log analysis foundation`
- 全量测试：`python -m pytest`，初验 `155 passed`，修复后复验 `164 passed`。
- LOG 专项测试：`python -m pytest agent_py_agent\tests\test_log_analysis_models.py agent_py_agent\tests\test_log_analysis_ingest.py agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_log_analysis_detectors.py`，初验 `24 passed`，修复后 `33 passed`。
- 语法检查：`python -m py_compile` 覆盖 `agent_py_agent/agent/log_analysis/**/*.py`，通过。
- 父验收复现失败：`ingest_file(fixture, root=td)` 默认写入 3 条事件后，`security_query(root=td, start_time=..., end_time=...)` 返回 `row_count=0`，说明 ingest/query 默认路径断裂。
- 修复后复现：同一脚本返回 `stored=3`、`events_path=<td>/events.jsonl`、`query_rows=3`、`preview_rows=3`。

## 2026-04-30 / 日志分析 CLI、工具注册和结构化查询验收证据

- LOG CLI / tools / dispatch / detector / model 组合测试：`python -m pytest agent_py_agent\tests\test_log_analysis_cli.py agent_py_agent\tests\test_cli_reference.py agent_py_agent\tests\test_tools.py agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_log_analysis_detectors.py agent_py_agent\tests\test_log_analysis_models.py`，结果 `52 passed`。
- 全量测试：`python -m pytest`，结果 `172 passed`。
- 手工命令：`python -m agent_py_agent logs status --json`，结果显示 `enabled=false`、`state=disabled`、`heavy_dependencies_loaded=false`。
- 手工闭环：`python -m agent_py_agent logs ingest validation/security_fixtures/security_alert_v1.jsonl --root <temp> --source-id cli-fixture --json` 后，`python -m agent_py_agent logs query --root <temp> --start-time 2026-04-30T00:00:00Z --end-time 2026-05-01T00:00:00Z --limit 5 --json` 返回 `row_count=3`。

## 2026-04-30 / Subagent Workflow Phase 1 验收证据

- 三路 worker：
  - 配置与开关：`agent_py_agent/agent/settings/config.py`、`agent_py_agent/config/agent_config.yaml`、`test_subagent_workflow_config.py`。
  - 模板 store：`agent_py_agent/agent/subagent_workflows/`、`test_subagent_workflow_templates.py`、`pyproject.toml` package data。
  - QualityContract / Context Pack：`agent_py_agent/agent/subagents/*`、`test_subagent_quality_contract.py`。
- 专项组合测试：`python -m pytest agent_py_agent\tests\test_subagent_workflow_config.py agent_py_agent\tests\test_subagent_workflow_templates.py agent_py_agent\tests\test_subagent_quality_contract.py`，结果 `14 passed`。
- 父会话相关回归：`python -m pytest agent_py_agent\tests\test_packaging.py agent_py_agent\tests\test_agent.py agent_py_agent\tests\test_tools.py`，结果 `63 passed`。
- 全量测试：`python -m pytest`，结果 `186 passed`。
- 格式检查：`git diff --check` 通过。
## 2026-04-30 / Subagent Workflow Phase 3 and Log Storage Audit Evidence

- Three-worker batch:
  - Router worker: `agent_py_agent/agent/subagent_workflows/router.py`, `agent_py_agent/tests/test_subagent_workflow_router.py`.
  - Compiler worker: `agent_py_agent/agent/subagent_workflows/compiler.py`, `agent_py_agent/tests/test_subagent_workflow_compiler.py`.
  - Parent gate worker: `agent_py_agent/agent/subagent_workflows/acceptance.py`, `agent_py_agent/tests/test_subagent_workflow_acceptance.py`.
- Parent integration:
  - Exported router/compiler/acceptance APIs from `agent_py_agent.agent.subagent_workflows`.
  - Added Chinese routing regressions for quality delivery and log-analysis development tasks.
  - Fixed a `tooling.registry` / `log_analysis.tools` import cycle by lazily importing security tools during registry construction.
- Log storage audit:
  - Added `JsonlReadAudit`.
  - `LocalLogStore` records last read audits and persists corrupt/non-object JSONL samples to `corrupt_lines.jsonl`.
  - Query summaries and persisted query records include `storage_read_audit`, `skipped_storage_lines`, and `corrupt_storage_lines`.
- Focused verification:
  - `python -m pytest agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_ingest.py` -> `9 passed`.
  - `python -m pytest agent_py_agent\tests\test_subagent_workflow_router.py agent_py_agent\tests\test_subagent_workflow_compiler.py agent_py_agent\tests\test_subagent_workflow_acceptance.py agent_py_agent\tests\test_subagent_workflow_templates.py agent_py_agent\tests\test_subagent_workflow_config.py` -> `29 passed`.
- Full verification:
  - `python -m pytest` -> `207 passed`.

## 2026-04-30 / Runtime Capability, Query Limits, Live Lab, and Workflow Planner Evidence

- Three-worker batch:
  - Runtime capability worker: `agent_py_agent/agent/agent_core/runtime_mixin.py`, `agent_py_agent/agent/agent_core/runtime_capabilities.py`, `agent_py_agent/tests/test_runtime_capabilities.py`.
  - Query-limit worker: `agent_py_agent/agent/log_analysis/storage/base.py`, `agent_py_agent/agent/log_analysis/storage/query.py`, `agent_py_agent/agent/log_analysis/tools.py`, `agent_py_agent/cli/logs.py`, `agent_py_agent/tests/test_log_analysis_query.py`, `agent_py_agent/tests/test_log_analysis_cli.py`.
  - Live Lab replay worker: `scripts/live_lab/log_analysis_replay.py`, `scripts/live_lab/cases.py`, `scripts/live_lab/constants.py`, `agent_py_agent/tests/test_live_lab_log_analysis_replay.py`, `TESTS.md`.
- Parent integration:
  - Added `plan_workflow_for_goal()` in `agent_py_agent/agent/subagent_workflows/planner.py`.
  - Exported the planner from `agent_py_agent.agent.subagent_workflows`.
  - Added `agent_py_agent/tests/test_subagent_workflow_planner.py`.
  - Added Chinese runtime-capability regression coverage for security-log prompts.
- Focused verification:
  - `python -m pytest agent_py_agent\tests\test_runtime_capabilities.py agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_cli.py agent_py_agent\tests\test_live_lab_log_analysis_replay.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `23 passed`.
  - Inline runtime check: `resolve_runtime_capabilities("<Chinese security-log prompt>")` -> `["logs/security"]`.
- Live Lab verification:
  - `python scripts\live_lab\log_analysis_replay.py --output-root $env:TEMP\openclaw-log-replay-codex` -> `ok=true`.
  - Replay summary on a fresh output root: `scenario=SecurityAlertV1`, `dry_run=true`, `total_events=3`, `stored_events=3`, `case_count=1`, `finding_count=1`, `evidence_paths=3`.
  - Replay artifacts: `case.json`, `route.json`, `first_response_report.md`, `forensic_package.json`, and `replay_summary.json`.
- Full verification:
  - `python -m pytest` -> `223 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / Workflow Plan CLI and LOG Replay Gate Evidence

- Three-worker batch:
  - Replay gate worker: `validation/security_fixtures/security_alert_v1_no_findings.jsonl`, `scripts/live_lab/log_analysis_replay.py`, `agent_py_agent/tests/test_live_lab_log_analysis_replay.py`, `TESTS.md`.
  - User quickstart worker: `README.md`, `CLI_REFERENCE.md`, `TESTS.md`, `docs/design/log-analysis.md`.
  - Workflow-plan CLI worker: `agent_py_agent/cli/subagents.py`, `agent_py_agent/cli/parser.py`, `agent_py_agent/tests/test_subagent_workflow_planner.py`.
- Parent integration:
  - Added compatibility re-export for `cmd_subagents_workflow_plan` in `agent_py_agent/__main__.py`.
  - Verified shared doc edits in `TESTS.md` kept both replay negative-fixture notes and quickstart commands.
- Focused verification:
  - `python -m pytest agent_py_agent\tests\test_live_lab_log_analysis_replay.py agent_py_agent\tests\test_subagent_workflow_planner.py agent_py_agent\tests\test_cli_reference.py` -> `9 passed`.
- Manual verification:
  - `python -m agent_py_agent subagents-workflow-plan "Fix API bug and add tests" --json` -> `selected_template_id=code_feature_split`, `worker_count=3`, `parent_acceptance_check_count=8`.
  - `python scripts\live_lab\log_analysis_replay.py --output-root <fresh temp>` -> `ok=true`, `fixture_format=jsonl`, `parsed_events=3`, `stored_events=3`, `case_count=1`, `finding_count=1`, all stages pass.
- Live Lab verification:
  - `python scripts\live_agent_lab.py --suite log-analysis --runs-dir <temp> --run-id <id>` -> `LIVE_LAB_PASS`.
- New negative replay coverage:
  - `security_alert_v1_no_findings.jsonl` is readable and stores 2 events, then fails at `failed_stage=detector` with `error_type=ReplayStageError`.
- Full verification:
  - `python -m pytest` -> `226 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / LOG Work Orders and Workflow Preview Persistence Evidence

- Three-worker batch:
  - LOG work-order bridge: `agent_py_agent/agent/log_analysis/dispatch/work_orders.py`, dispatch exports, and dispatch tests.
  - Workflow preview persistence: `agent_py_agent/agent/subagent_workflows/planner.py`, `subagents-workflow-plan --output-dir`, and planner tests.
  - Replay later-stage gates: `scripts/live_lab/log_analysis_replay.py` and replay tests.
- Parent integration:
  - `subagents-workflow-plan --json --output-dir <dir>` now reports the written preview JSON/Markdown paths in JSON output.
  - CLI reference documents `--output-dir`.
- Focused verification:
  - `python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_subagent_workflow_planner.py agent_py_agent\tests\test_live_lab_log_analysis_replay.py` -> `28 passed`.
- Manual verification:
  - `python -m agent_py_agent subagents-workflow-plan "Fix API bug and add tests" --output-dir <fresh temp> --json` -> `preview_paths.json` and `preview_paths.markdown` exist.
  - `python scripts\live_lab\log_analysis_replay.py --output-root <fresh temp>` -> `ok=true`, `parsed_events=3`, `stored_events=3`, `case_count=1`, `finding_count=1`.
- Live Lab verification:
  - `python scripts\live_agent_lab.py --suite log-analysis --runs-dir <temp> --run-id <id>` -> `LIVE_LAB_PASS`.
- Full verification:
  - `python -m pytest` -> `233 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / LOG SubAgentTask Apply Path and Module Docs Evidence

- Two-worker batch:
  - LOG apply worker `019ddd12-4cd2-7f62-9a6d-2a7a3f87d054`: `agent_py_agent/agent/log_analysis/dispatch/work_orders.py`, dispatch exports, and dispatch tests.
  - Module docs worker `019ddd12-8e7d-77b1-bdfa-f47251b8d3b5`: `docs/README.md`, `docs/modules/`, `DESIGN_LEDGER.md`, and `CODEBASE_TREE.md`.
- Parent integration:
  - Added `create_subagent_tasks_from_work_order_plan(..., apply=True)` for explicit LOG task materialization.
  - Verified default dry-run behavior creates no tasks.
  - Verified not-ready/no-evidence plans refuse creation.
  - Updated module progress docs to include solved problems, current test status, and remaining risks.
- Focused verification:
  - `python -m pytest agent_py_agent\tests\test_log_analysis_dispatch.py agent_py_agent\tests\test_subagent_workflow_planner.py` -> `26 passed`.
- Full verification:
  - `python -m pytest` -> `236 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / Code Docs Comment Sync Gate Evidence

- Added `scripts/check_doc_sync.py`:
  - Checks current git diff or staged diff.
  - Covered modules in the first slice: `log-analysis` and `subagent`.
  - Requires matching `docs/modules/<module>/02-progress.md` and `04-structure.md` when covered module code changes.
  - Requires same-file comment/doc additions when covered implementation Python code adds implementation lines.
- Added `agent_py_agent/tests/test_doc_sync.py` for pure evaluation cases.
- Updated docs and checklist:
  - `docs/modules/README.md` documents the sync gate.
  - `TEST_CHECKLIST.md` includes `python scripts/check_doc_sync.py`.
- Verification:
  - `python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `3 passed`.
  - `python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`.
  - `git diff --check` -> passed.

## 2026-04-30 / Module Docs Expansion and Sync Gate Coverage Evidence

- Added module four-piece docs:
  - `docs/modules/memory/01-discussion.md` through `04-structure.md`.
  - `docs/modules/gateway/01-discussion.md` through `04-structure.md`.
  - `docs/modules/live-lab/01-discussion.md` through `04-structure.md`.
- Extended `scripts/check_doc_sync.py`:
  - New covered module: `memory` for memory store, routing, archive, settings, and CLI paths.
  - New covered module: `gateway` for gateway compatibility layer, `gateway_parts`, gateway CLI, and adapter CLI paths.
  - New covered module: `live-lab` for `scripts/live_agent_lab.py`, `scripts/live_lab/`, and `scripts/open_live_lab.sh`.
- Extended `agent_py_agent/tests/test_doc_sync.py`:
  - Verifies required docs exist for every module rule.
  - Verifies memory/gateway/live-lab paths are covered by the sync gate.
- Updated navigation:
  - `docs/modules/README.md`, `CODEBASE_TREE.md`, and `DESIGN_LEDGER.md`.
- Verification:
  - `python -m pytest agent_py_agent\tests\test_doc_sync.py` -> `5 passed`.
  - `python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`.
  - `python -m pytest` -> `241 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / LOG Tool Config Doctor Teaching Comments Evidence

- Updated LOG core boundary files:
  - `agent_py_agent/agent/log_analysis/tools.py`: module, query/hunt/trace helpers, tool wrappers, execute methods, and parameter filtering helpers now include `LLM:` / `新手说明:` / parameter and return explanations.
  - `agent_py_agent/agent/log_analysis/doctor.py`: doctor collection and path status helpers now document lightweight status behavior and failure fallback.
  - `agent_py_agent/agent/log_analysis/config.py`: config dataclasses, loaders, coercion helpers, warning helper, and path resolution now document safe defaults and parameter meaning.
- Updated module docs in the same diff:
  - `docs/modules/log-analysis/02-progress.md`.
  - `docs/modules/log-analysis/04-structure.md`.
- Verification:
  - `python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`.
  - `python -m pytest agent_py_agent\tests\test_log_analysis_models.py agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_cli.py agent_py_agent\tests\test_tools.py agent_py_agent\tests\test_doc_sync.py` -> `45 passed`.
  - `python -m pytest` -> `241 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / Memory Settings Store Routing Teaching Comments Evidence

- Updated memory core boundary files:
  - `agent_py_agent/agent/settings/memory.py`: memory settings, warning payloads, normalization, config mutation, lookup, warning, bool/choice/int coercion now include field/parameter/return explanations.
  - `agent_py_agent/agent/memory_store/jsonl.py`: MemoryRecord, JsonlMemory, JSONL read/write/search/index helpers now document JSONL fact stream, LocalStore index fallback, side effects, and return values.
  - `agent_py_agent/agent/memory_routing/models.py`: route, match, path resolution, read receipt, and dedupe helper now explain route tickets and receipt fields.
  - `agent_py_agent/agent/memory_routing/matcher.py`: match, score, resolve, receipt, normalize/token/path helpers now document deterministic scoring and strict/soft path behavior.
- Updated module docs in the same diff:
  - `docs/modules/memory/02-progress.md`.
  - `docs/modules/memory/04-structure.md`.
- Verification:
  - `python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`.
  - `python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `50 passed`.
  - `python -m pytest` -> `241 passed`.
  - `git diff --check` -> passed.

## 2026-04-30 / Memory Archive Routing CLI Teaching Comments and Readback Evidence

- Updated memory archive files:
  - `agent_py_agent/agent/memory_archive/models.py`: field-level explanations for snapshots and raw events.
  - `agent_py_agent/agent/memory_archive/storage.py`: write-path, retention, readback, and date-coercion comments; raw event readback verification.
  - `agent_py_agent/agent/memory_archive/runtime.py`: run-turn, message, tool, hash, preview, and compatibility helper comments.
  - `agent_py_agent/agent/memory_archive/snapshots.py`: recovery snapshot result, write behavior, tool snapshot, preview, ID, and hash comments.
  - `agent_py_agent/agent/memory_archive/query.py`: archive collection, filtering, resume clue, task fact-source, and malformed-line comments.
  - `agent_py_agent/agent/memory_archive/resume_brief.py`: brief synthesis, ID collection, task status, context block, and dedupe comments.
  - `agent_py_agent/agent/memory_archive/resume_context.py`: auto resume trigger, query selection, non-throwing result, and injection comments.
  - `agent_py_agent/agent/memory_archive/tokens.py`: conservative estimation helper comments.
- Updated routing and CLI files:
  - `agent_py_agent/agent/memory_routing/loader.py`: loader/parser/coercion comments and separator coverage.
  - `agent_py_agent/agent/memory_routing/context.py`: context bundle, safe path resolution, authority read, receipt, and finding comments.
  - `agent_py_agent/cli/memory_commands.py`: route/doctor report comments.
  - `agent_py_agent/cli/memory_archive_commands.py`: list/search/resume report comments.
- Added tests:
  - `test_append_raw_event_readback_failure_is_reported`.
  - `test_markdown_routes_split_common_human_list_separators`.
- Verification:
  - `python -m pytest agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_routing.py` -> `16 passed`.
  - `python -m py_compile <touched memory archive/routing/CLI files>` -> passed.
  - `python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`.
  - `python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `67 passed`.
  - `python -m pytest` -> `243 passed`.

## 2026-04-30 / Memory Cross-Day Resume Handoff Evidence

- Updated memory archive query behavior:
  - `filter_archive_records()` now treats date-only `until` values as inclusive through the end of that date.
  - Added `_is_date_only()` with teaching comments so the behavior is explicit.
- Added cross-day resume fixture coverage:
  - `test_memory_resume_cross_day_handoff_uses_task_fact_sources` creates a previous-day raw event, next-day hook snapshot, LocalStore-indexed subagent task, and real `STATUS.md` / `HANDOFF.md` fact files.
  - `test_auto_resume_context_recovers_cross_day_handoff_task` verifies `SimpleAgent.run()` injects an Auto Recovery Context for a cross-day handoff prompt.
- Verification:
  - `python -m pytest agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_memory_runtime.py` -> `14 passed`.
  - `python scripts\check_doc_sync.py` -> `DOC_SYNC_PASS`.
  - `python -m pytest agent_py_agent\tests\test_memory_config.py agent_py_agent\tests\test_memory_routing.py agent_py_agent\tests\test_memory_routing_context.py agent_py_agent\tests\test_memory_runtime.py agent_py_agent\tests\test_memory_cli.py agent_py_agent\tests\test_memory_archive.py agent_py_agent\tests\test_memory_archive_runtime.py agent_py_agent\tests\test_memory_archive_cli.py agent_py_agent\tests\test_local_store.py agent_py_agent\tests\test_doc_sync.py` -> `69 passed`.
  - `python -m pytest` -> `245 passed`.
