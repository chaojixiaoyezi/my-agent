# 测试文档

## 近期新增验证

- `agent_py_agent/tests/test_runner_session_pool.py`：覆盖 runner worker session lease 心跳、进程 id、完成状态和 task attributes 写回。
- `agent_py_agent/tests/test_subagent_security_reserve.py::test_create_run_inherits_parent_config_overlay_ref`：覆盖 child 从 parent 继承 `config_overlay_ref` 并写入 runtime config scope。
- `agent_py_agent/tests/test_capability_runtime_config.py::test_task_config_overlay_ref_loads_as_runtime_layer`：覆盖 task `config_overlay_ref` 被加载为真实 runtime config layer。
- `agent_py_agent/tests/test_manager_patch.py::TestValidatePatchTestCommand::test_patch_apply_record_includes_owner_policy_and_batch_validation`：覆盖 patch apply 的 owner policy、批量验证和 failure recovery 审计字段。
- `agent_py_agent/tests/test_gateway_heartbeat.py::test_gateway_side_effect_error_is_structured`：覆盖 gateway best-effort side-effect 异常输出结构化 report。
- `agent_py_agent/tests/test_real_run_review_contract.py`：覆盖真实运行复盘合同，验证 acceptance/report 结构化错误码、runtime bracketed marker、非失败 marker 过滤、失败聚类和 Markdown 输出。
- `agent_py_agent/tests/test_real_run_review_script.py`：覆盖 `scripts/review_real_runs.py`，验证脚本会写 `real-run-review.json`、`real-run-review.jsonl` 和 `real-run-review.md`，并在发现失败 run 时返回非零码。
- `scripts/review_real_runs.py --runs-root ... --glob '*20260521*' --out-dir docs/reports`：用于把真实任务输出转成可追踪的失败样本候选，真实外部系统只作为输入来源，日常修复仍回到离线合同测试。

## 测试概览

| 指标 | 数值 |
|------|------|
| 总测试数 | 5072 |
| 测试文件数 | 250 |
| 通过率 | 99.88% (5066 passed, 6 failed) |
| 失败测试 | 6 (分布在 4 个文件中) |
| 参数化测试文件 | 4 |
| Fixture 总数 | 15 |

**注意**: 有 3 个测试文件存在收集错误 (`test_scenario_gateway_resume.py`, `test_subagent_learning.py`, `test_subagent_workflow_planner.py`)，收集时会计入错误但不影响整体通过率。

## 测试文件清单

### adapter (7 文件, 87 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_adapter_base.py | adapter.base | 15 |
| test_adapter_base_class.py | adapter.base | 10 |
| test_adapter_feishu.py | adapter.feishu | 9 |
| test_adapter_late.py | adapter.late | 4 |
| test_adapter_manager.py | adapter.manager | 11 |
| test_adapter_manager_class.py | adapter.manager | 14 |
| test_adapter_qq.py | adapter.qq | 11 |
| test_channel_adapter.py | adapter.channel | 7 |

### agent_core (6 文件, 88 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_dispatch_loop.py | agent_core.dispatch | 26 |
| test_dispatch_loop_class.py | agent_core.dispatch | 10 |
| test_dispatch_mixin.py | agent_core.dispatch | 16 |
| test_supervisor.py | agent_core.supervisor | 25 |
| test_runtime_mixin.py | agent_core.runtime | 12 |
| test_attack_chain.py | agent_core.attack_chain | 20 |

### archive (8 文件, 218 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_archive_event_builders.py | archive.event_builders | 45 |
| test_archive_interfaces.py | archive.interfaces | 16 |
| test_archive_io.py | archive.io | 28 |
| test_archive_migration.py | archive.migration | 33 |
| test_archive_query_logic.py | archive.query_logic | 24 |
| test_archive_snapshots.py | archive.snapshots | 32 |
| test_archive_tokens.py | archive.tokens | 21 |
| test_archive_turn_archiver.py | archive.turn_archiver | 19 |

### auth (2 文件, 50 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_auth.py | auth | 33 |
| test_auth_class.py | auth | 17 |

### capabilities (3 文件, 63 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_capabilities.py | capabilities | 8 |
| test_capabilities_class.py | capabilities | 25 |
| test_capability_config.py | capability_config | 17 |
| test_capability_config_class.py | capability_config | 13 |

### cli (21 文件, 400+ tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_cli_chat.py | cli.chat | 28 |
| test_cli_parser.py | cli.parser | 28 |
| test_cli_reference.py | cli.reference | 1 |
| test_config_commands.py | cli.config | 20 |
| test_config_normalize.py | cli.config_normalize | 30 |
| test_config_validation.py | cli.config_validation | 15 |
| test_dispatch_background.py | cli.dispatch_background | 2 |
| test_gateway_commands.py | cli.gateway | 15 |
| test_local_store_commands.py | cli.local_store | 11 |
| test_log_analysis_cli.py | cli.log_analysis | 6 |
| test_log_analysis_commands.py | cli.log_analysis | 12 |
| test_memory_archive_cli.py | cli.memory_archive | 7 |
| test_memory_archive_cli_query.py | cli.memory_archive | 3 |
| test_memory_archive_cli_route.py | cli.memory_archive | 4 |
| test_memory_cli.py | cli.memory | 3 |
| test_memory_commands_cli.py | cli.memory | 22 |
| test_parser_subcommands.py | cli.parser | 18 |
| test_startup_commands.py | cli.startup | 12 |
| test_subagents_tests_command.py | cli.subagents_tests | 4 |
| test_task_cli_ux.py | cli.task | 22 |
| test_workstream_commands.py | cli.workstream | 12 |

### config (3 文件, 61 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_settings_config.py | config.settings | 28 |
| test_settings_memory.py | config.memory | 34 |
| test_parameters.py | config.parameters | 27 |

### concurrency (3 文件, 44 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_concurrency.py | concurrency | 21 |
| test_dispatch_lock.py | concurrency.lock | 11 |
| test_watchdog.py | concurrency.watchdog | 12 |

### e2e (5 文件, 90 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_e2e_dispatch_flow.py | e2e.dispatch | 17 |
| test_e2e_gateway_flow.py | e2e.gateway | 17 |
| test_e2e_memory_workflow.py | e2e.memory | 17 |
| test_e2e_subagent_workflow.py | e2e.subagent | 22 |
| test_e2e_task_lifecycle.py | e2e.task | 17 |

### gateway (10 文件, 97 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_gateway_adapter.py | gateway.adapter | 15 |
| test_gateway_client.py | gateway.client | 2 |
| test_gateway_heartbeat.py | gateway.heartbeat | 6 |
| test_gateway_helpers.py | gateway.helpers | 11 |
| test_gateway_http.py | gateway.http | 12 |
| test_gateway_io.py | gateway.io | 19 |
| test_gateway_logging.py | gateway.logging | 8 |
| test_gateway_paths.py | gateway.paths | 8 |
| test_gateway_streaming.py | gateway.streaming | 3 |
| test_lease.py | gateway.lease | 21 |

### local_storage (9 文件, 181 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_local_storage_models.py | local_storage.models | 11 |
| test_local_storage_models_class.py | local_storage.models | 18 |
| test_local_storage_records.py | local_storage.records | 25 |
| test_local_storage_schema.py | local_storage.schema | 20 |
| test_local_storage_store.py | local_storage.store | 18 |
| test_local_storage_store_class.py | local_storage.store | 19 |
| test_local_store.py | local_storage | 16 |
| test_local_store_basics.py | local_storage.basics | 10 |
| test_local_maintenance.py | local_storage.maintenance | 14 |

### log_analysis (40 文件, 1200+ tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_log_analysis_case_store.py | log_analysis.case_store | 44 |
| test_log_analysis_detectors.py | log_analysis.detectors | 9 |
| test_log_analysis_detectors_helpers.py | log_analysis.detectors | 3 |
| test_log_analysis_detectors_rules.py | log_analysis.detectors | 6 |
| test_log_analysis_dispatch.py | log_analysis.dispatch | 18 |
| test_log_analysis_dispatch_basics.py | log_analysis.dispatch | 13 |
| test_log_analysis_dispatch_work_orders.py | log_analysis.dispatch | 5 |
| test_log_analysis_entity_graph.py | log_analysis.entity_graph | 38 |
| test_log_analysis_field_access.py | log_analysis.field_access | 119 |
| test_log_analysis_first_loop.py | log_analysis.first_loop | 26 |
| test_log_analysis_ingest.py | log_analysis.ingest | 5 |
| test_log_analysis_models.py | log_analysis.models | 5 |
| test_log_analysis_pipeline.py | log_analysis.pipeline | 52 |
| test_log_analysis_query.py | log_analysis.query | 8 |
| test_log_analysis_security_hunting.py | log_analysis.security_hunting | 56 |
| test_log_attack_chain.py | log_analysis.attack_chain | 23 |
| test_log_baselines.py | log_analysis.baselines | 17 |
| test_log_budgets.py | log_analysis.budgets | 10 |
| test_log_checkpoint.py | log_analysis.checkpoint | 25 |
| test_log_classifiers.py | log_analysis.classifiers | 34 |
| test_log_config_coercers.py | log_analysis.config | 23 |
| test_log_config_loading.py | log_analysis.config | 35 |
| test_log_config_model.py | log_analysis.config | 26 |
| test_log_contracts.py | log_analysis.contracts | 40 |
| test_log_correlation.py | log_analysis.correlation | 20 |
| test_log_detectors_helpers.py | log_analysis.detectors | 47 |
| test_log_detectors_rules.py | log_analysis.detectors | 25 |
| test_log_dispatch_engine.py | log_analysis.dispatch_engine | 39 |
| test_log_dispatch_health.py | log_analysis.dispatch_health | 30 |
| test_log_dispatch_queue.py | log_analysis.dispatch_queue | 39 |
| test_log_evidence.py | log_analysis.evidence | 14 |
| test_log_hunting.py | log_analysis.hunting | 21 |
| test_log_pipeline.py | log_analysis.pipeline | 25 |
| test_log_query_functions.py | log_analysis.query | 23 |
| test_log_scheduler.py | log_analysis.scheduler | 18 |
| test_log_storage_base.py | log_analysis.storage | 72 |
| test_log_summaries.py | log_analysis.summaries | 36 |
| test_log_tool_classes.py | log_analysis.tool_classes | 27 |
| test_log_work_order_creation.py | log_analysis.work_order | 30 |
| test_live_lab_log_analysis_replay.py | log_analysis.replay | 5 |

### manager (12 文件, 232 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_manager_acceptance.py | manager.acceptance | 9 |
| test_manager_acceptance_findings.py | manager.acceptance | 14 |
| test_manager_actions.py | manager.actions | 9 |
| test_manager_base.py | manager.base | 22 |
| test_manager_board_class.py | manager.board | 12 |
| test_manager_capabilities.py | manager.capabilities | 15 |
| test_manager_channel_probe.py | manager.channel_probe | 17 |
| test_manager_dispatch.py | manager.dispatch | 18 |
| test_manager_indexing.py | manager.indexing | 20 |
| test_manager_learning.py | manager.learning | 25 |
| test_manager_lifecycle.py | manager.lifecycle | 22 |
| test_manager_normalize_class.py | manager.normalize | 30 |
| test_manager_patch.py | manager.patch | 25 |
| test_manager_runner_context.py | manager.runner_context | 14 |
| test_manager_runner_results.py | manager.runner_results | 17 |

### memory (16 文件, 431 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_memory_archive.py | memory.archive | 8 |
| test_memory_archive_commands.py | memory.archive | 10 |
| test_memory_archive_models.py | memory.archive | 23 |
| test_memory_archive_runtime.py | memory.archive | 4 |
| test_memory_archive_storage.py | memory.archive.storage | 18 |
| test_memory_config.py | memory.config | 4 |
| test_memory_doctor_class.py | memory.doctor | 26 |
| test_memory_first_loop.py | memory.first_loop | 5 |
| test_memory_push.py | memory.push | 33 |
| test_memory_push_integration.py | memory.push | 36 |
| test_memory_route_commands.py | memory.routing | 17 |
| test_memory_routing.py | memory.routing | 8 |
| test_memory_routing_context.py | memory.routing | 32 |
| test_memory_routing_loader.py | memory.routing | 26 |
| test_memory_routing_matcher.py | memory.routing | 42 |
| test_memory_routing_models.py | memory.routing | 24 |
| test_memory_routing_validator.py | memory.routing | 53 |
| test_memory_runtime.py | memory.runtime | 10 |
| test_memory_runtime_archive.py | memory.runtime | 2 |
| test_memory_runtime_basics.py | memory.runtime | 8 |
| test_memory_store_jsonl.py | memory.store | 24 |
| test_memory_store_jsonl_class.py | memory.store | 32 |
| test_memory_store_models.py | memory.store | 28 |

### model_speed (2 文件, 53 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_model_speed_benchmark.py | model_speed.benchmark | 24 |
| test_model_speed_models.py | model_speed.models | 29 |

### notification (3 文件, 57 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_notification.py | notification | 28 |
| test_notification_class.py | notification | 12 |
| test_notification_router.py | notification.router | 17 |

### prompting (2 文件, 66 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_prompting.py | prompting | 34 |
| test_prompting_builder.py | prompting.builder | 32 |

### security (5 文件, 76 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_automation_guard.py | security.guard | 12 |
| test_automation_guard_class.py | security.guard | 10 |
| test_security_alert_v1_class.py | security.alert | 21 |
| test_security_rules.py | security.rules | 19 |
| test_policy_checks.py | security.policies | 43 |
| test_backends.py | security.backends | 6 |
| test_backends_base.py | security.backends | 33 |

### session (4 文件, 79 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_session.py | session | 26 |
| test_session_class.py | session | 16 |
| test_session_resume.py | session.resume | 11 |
| test_resume_brief.py | session.resume | 37 |

### startup_recovery (3 文件, 50 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_startup_recovery.py | startup_recovery | 22 |
| test_startup_recovery_class.py | startup_recovery | 16 |
| test_local_doctor_class.py | startup_recovery.doctor | 20 |

### stress (4 文件, 65 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_stress_dispatch.py | stress.dispatch | 16 |
| test_stress_gateway.py | stress.gateway | 19 |
| test_stress_log_pipeline.py | stress.log_pipeline | 11 |
| test_stress_memory.py | stress.memory | 19 |

### subagent (24 文件, 450+ tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_subagent_commands.py | subagent.commands | 15 |
| test_subagent_mixin.py | subagent.mixin | 17 |
| test_subagent_models.py | subagent.models | 16 |
| test_subagent_test_execution_record.py | subagent.execution_records | 3 |
| test_subagent_test_executor.py | subagent.execution_records | 4 |
| test_subagent_test_execution_report.py | subagent.execution_report | 2 |
| test_subagent_parsing.py | subagent.parsing | 27 |
| test_subagent_policies.py | subagent.policies | 37 |
| test_subagent_quality_contract.py | subagent.quality | 5 |
| test_subagent_rendering.py | subagent.rendering | 18 |
| test_subagent_reports_class.py | subagent.reports | 24 |
| test_subagent_utils.py | subagent.utils | 25 |
| test_subagent_workflow_acceptance.py | subagent.workflow | 5 |
| test_subagent_workflow_compiler.py | subagent.workflow | 6 |
| test_subagent_workflow_config.py | subagent.workflow | 5 |
| test_subagent_workflow_planner.py | subagent.workflow | 10 |
| test_subagent_workflow_router.py | subagent.workflow | 10 |
| test_subagent_workflow_templates.py | subagent.workflow | 5 |
| test_subcommands_agents_class.py | subagent.commands | 19 |
| test_subcommands_basic_class.py | subagent.commands | 34 |
| test_subcommands_gateway_class.py | subagent.commands | 22 |
| test_planner.py | subagent.planner | 16 |
| test_result_processors_class.py | subagent.processors | 16 |
| test_runner_dispatch.py | subagent.runner | 34 |
| test_runner_prompts.py | subagent.runner | 18 |
| test_runner_rendering_class.py | subagent.runner | 20 |
| test_runtime_capabilities.py | subagent.runtime | 46 |

### task (4 文件, 67 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_task_commands.py | task.commands | 16 |
| test_task_complexity.py | task.complexity | 10 |
| test_task_complexity_class.py | task.complexity | 9 |
| test_scenario_commands.py | task.scenario | 10 |

### tooling (5 文件, 113 tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_tooling_base.py | tooling.base | 23 |
| test_tooling_filesystem.py | tooling.filesystem | 23 |
| test_tooling_filesystem_write.py | tooling.filesystem | 25 |
| test_tooling_shell.py | tooling.shell | 20 |
| test_tooling_web.py | tooling.web | 22 |

### 其他 (18 文件, 400+ tests)

| 文件 | 覆盖模块 | 测试数 |
|------|----------|--------|
| test_adaptive_retry.py | adaptive_retry | 18 |
| test_adaptive_retry_class.py | adaptive_retry | 13 |
| test_audit.py | audit | 20 |
| test_audit_class.py | audit | 13 |
| test_boundary_cases.py | boundary_cases | 15 |
| test_case_store.py | case_store | 27 |
| test_cross_channel.py | cross_channel | 21 |
| test_cross_channel_class.py | cross_channel | 11 |
| test_daemon_control.py | daemon_control | 28 |
| test_dead_letter.py | dead_letter | 18 |
| test_doc_sync.py | doc_sync | 5 |
| test_doctor_class.py | doctor | 17 |
| test_dynamic_timeout.py | dynamic_timeout | 13 |
| test_dynamic_timeout_class.py | dynamic_timeout | 13 |
| test_entity_graph.py | entity_graph | 15 |
| test_error_scenarios.py | error_scenarios | 16 |
| test_failure_analyzer.py | failure_analyzer | 17 |
| test_failure_introspector.py | failure_introspector | 12 |
| test_field_access.py | field_access | 64 |
| test_field_extractors.py | field_extractors | 49 |
| test_file_io.py | file_io | 17 |
| test_file_io_class.py | file_io | 10 |
| test_gateway_store.py | gateway_store | 6 |
| test_log_analysis_security_hunting.py | security_hunting | 56 |
| test_orchestration_tools.py | orchestration | 23 |
| test_packaging.py | packaging | 2 |
| test_pipeline_enrich.py | pipeline_enrich | 24 |
| test_process_control.py | process_control | 12 |
| test_regression_fixes.py | regression | 30 |
| test_scenario_gateway_resume.py | scenario | 6 |
| test_scenario_utils.py | scenario | 2 |
| test_startup_recovery.py | startup | 22 |
| test_status_commands.py | status | 8 |
| test_thinking_spinner.py | thinking | 6 |
| test_user_space.py | user_space | 19 |
| test_user_space_migration.py | user_space | 17 |
| test_write_boundary.py | write_boundary | 38 |

## 测试策略

### 单元测试
- 覆盖所有公开 API
- 每个模块有对应的 `test_<module>.py` 文件
- 使用 mock 隔离外部依赖
- 重点：`test_adaptive_retry.py`, `test_auth.py`, `test_capabilities.py`

### 集成测试
- 跨模块交互验证
- 文件：`test_dispatch_loop.py`, `test_manager_dispatch.py`, `test_memory_push_integration.py`

### 端到端测试
- 完整用户流程覆盖
- 目录：`test_e2e_*.py` (5 个文件, 90 tests)

### 压力测试
- 大数据量、高并发场景
- 目录：`test_stress_*.py` (4 个文件, 65 tests)
- 标记：使用 `@pytest.mark.stress` 或压力测试专用文件

### 回归测试
- 已知 bug 复现
- 文件：`test_regression_fixes.py` (30 tests)

## 共享 Fixture

`conftest.py` 提供的共享 fixtures：

| Fixture | 用途 |
|---------|------|
| `sample_evidence_ref` | 创建示例 EvidenceRef 对象 |
| `sample_finding` | 创建示例 Finding 对象 |
| `make_finding_func` | Factory 函数：创建自定义 Finding |
| `sample_case_record` | 创建示例 CaseRecord 对象 |
| `sample_attack_chain_step` | 创建示例 AttackChainStep 对象 |
| `sample_subagent_task` | 创建示例 SubAgentTask 对象 |
| `sample_dispatch_budget` | 创建允许调度的 DispatchBudget |
| `disabled_dispatch_budget` | 创建禁用的 DispatchBudget |
| `mock_connection_ctx` | 可配置 mock 连接上下文管理器 |
| `mock_local_store` | FakeStore 实例（用于 LocalStoreMaintenanceMixin） |
| `make_fake_store_func` | Factory 函数：创建自定义 FakeStore |
| `sample_waf_event` | 创建示例 WAF 事件字典 |
| `sample_vpn_event` | 创建示例 VPN 事件字典 |
| `sample_auth_failure_events` | 创建认证失败事件列表 |
| `sample_process_event` | 创建示例进程事件字典 |
| `mock_db_conn` | 配置好的 mock 数据库连接 |
| `db_conn_with_records` | 包含记录数据的 mock 数据库连接 |

## 运行测试

### 常用命令

```bash
# 运行所有测试
python3 -m pytest -q

# 运行所有测试（详细输出）
python3 -m pytest -v

# 查看收集的测试（不执行）
python3 -m pytest --co

# 忽略有收集错误的文件
python3 -m pytest --ignore=agent_py_agent/tests/test_scenario_gateway_resume.py \
                 --ignore=agent_py_agent/tests/test_subagent_learning.py \
                 --ignore=agent_py_agent/tests/test_subagent_workflow_planner.py
```

### 单模块测试

```bash
# 运行单个测试文件
python3 -m pytest agent_py_agent/tests/test_auth.py -v

# 运行单个测试函数
python3 -m pytest agent_py_agent/tests/test_auth.py::TestAuth::test_login_success -v

# 运行匹配关键词的测试
python3 -m pytest -k "dispatch" -v
```

### 压力测试

```bash
# 运行所有压力测试
python3 -m pytest agent_py_agent/tests/test_stress_dispatch.py \
                 agent_py_agent/tests/test_stress_gateway.py \
                 agent_py_agent/tests/test_stress_log_pipeline.py \
                 agent_py_agent/tests/test_stress_memory.py -v

# 运行特定压力测试
python3 -m pytest agent_py_agent/tests/test_stress_dispatch.py::TestStressDispatch -v
```

### 查看覆盖率

```bash
# 生成覆盖率报告
python3 -m pytest --cov=agent_py_agent --cov-report=html

# 查看覆盖率摘要
python3 -m pytest --cov=agent_py_agent --cov-report=term-missing
```

### 运行特定类别

```bash
# 运行端到端测试
python3 -m pytest agent_py_agent/tests/test_e2e_*.py -v

# 运行回归测试
python3 -m pytest agent_py_agent/tests/test_regression_fixes.py -v

# 运行 CLI 测试
python3 -m pytest agent_py_agent/tests/test_cli_*.py -v
```

## 更新规范

### 新增模块时补测试

1. 在 `agent_py_agent/tests/` 创建 `test_<module_name>.py`
2. 测试文件命名遵循 `test_<source_module>.py` 或 `test_<source_module>_class.py`
3. 每个公开 API 至少有一个测试用例
4. 使用 conftest.py 中的共享 fixture

### 测试模板

```python
"""测试 <module> 模块。"""
import pytest
from agent_py_agent.<module> import <ClassOrFunc>

class Test<Feature>:
    """测试 <feature> 功能。"""

    def test_<scenario>(self):
        """描述测试场景。"""
        ...

    @pytest.mark.parametrize("input,expected", [
        ("case1", "result1"),
        ("case2", "result2"),
    ])
    def test_<parametrized>(self, input, expected):
        """参数化测试示例。"""
        assert <ClassOrFunc>(input) == expected
```

### 修改测试后同步文档

1. 如果新增测试文件，更新 `## 测试文件清单` 表格
2. 如果修改 fixture，更新 `## 共享 Fixture` 表格
3. 如果修改测试策略，更新 `## 测试策略` 章节
4. 运行 `python3 scripts/check_doc_sync.py` 验证

### CI 检查

```bash
# 本地运行文档同步检查
python3 scripts/check_doc_sync.py

# CI 应包含
- 文档与代码一致性检查
- 测试数量变化检测
- Fixture 同步验证
```
