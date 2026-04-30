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
