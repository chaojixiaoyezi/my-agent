# TESTS

这份文档记录当前推荐的测试方式。

注意：当前默认 `agent_config.yaml` 可能配置为远端模型后端。完整 `agent_py_agent/tests/run_tests.py` 里包含 `agent_py_agent run`，如果直接执行，可能会触发真实模型 API。日常开发优先跑定向测试。

## 推荐快速检查

语法检查：

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
```

CLI 入口：

```bash
python3 -m agent_py_agent --help
python3 -m agent_py_agent subagent-run --help
python3 -m agent_py_agent subagents-route-capabilities --help
python3 -m agent_py_agent subagents-acceptance --help
```

Git 空白检查：

```bash
git diff --check
```

## 定向功能测试

### Subagent / Capability / Runner

```bash
python3 -c "from agent_py_agent.tests.test_agent import test_subagents, test_subagent_capability_records, test_subagent_fake_done_requires_evidence, test_subagent_work_order_validation, test_subagent_takeover_records_locked_files, test_subagent_board_scales_and_flags, test_subagent_due_check_report, test_subagent_channel_probe_records_status, test_subagent_channel_probe_report, test_subagent_action_plan_dry_run, test_subagent_action_apply_dry_run_and_apply, test_subagent_action_apply_repairs_work_order, test_subagent_capability_route_grants_tool, test_subagent_capability_route_grants_skill, test_subagent_capability_route_creates_gap_when_no_match, test_subagent_execution_context_uses_only_grants, test_subagent_runner_dry_run_and_execute, test_subagent_runner_parses_structured_output, test_subagent_runner_parser_uses_last_parseable_fenced_block, test_subagent_acceptance_dry_run_and_apply, test_subagent_acceptance_rejects_missing_evidence_without_apply; test_subagents(); test_subagent_capability_records(); test_subagent_fake_done_requires_evidence(); test_subagent_work_order_validation(); test_subagent_takeover_records_locked_files(); test_subagent_board_scales_and_flags(); test_subagent_due_check_report(); test_subagent_channel_probe_records_status(); test_subagent_channel_probe_report(); test_subagent_action_plan_dry_run(); test_subagent_action_apply_dry_run_and_apply(); test_subagent_action_apply_repairs_work_order(); test_subagent_capability_route_grants_tool(); test_subagent_capability_route_grants_skill(); test_subagent_capability_route_creates_gap_when_no_match(); test_subagent_execution_context_uses_only_grants(); test_subagent_runner_dry_run_and_execute(); test_subagent_runner_parses_structured_output(); test_subagent_runner_parser_uses_last_parseable_fenced_block(); test_subagent_acceptance_dry_run_and_apply(); test_subagent_acceptance_rejects_missing_evidence_without_apply(); print('SUBAGENT_TEST_PASS')"
```

覆盖：
- 子代理工单创建。
- 父子关系。
- capability request / grant / gap。
- Fake Done 防护。
- 标准工单目录。
- takeover。
- board。
- due-check。
- channel probe。
- action plan。
- action apply。
- capability route。
- execution context。
- runner dry-run / execute。
- `[SUBAGENT_RESULT]` 结构化输出解析。
- 真实模型常见的 Markdown fenced JSON 结构化输出解析。
- subagent 验收 dry-run / apply。
- evidence / capability_requests / artifacts / tests / patches / lessons / next_actions 写回。

### Tools

```bash
python3 -c "from agent_py_agent.tests.test_tools import test_tool_loop_and_prompt_transcript, test_tool_catalog_and_recommended_sections, test_tool_allowlist_limits_prompt_and_execution, test_write_and_append_file_tools, test_replace_in_file_tool, test_fetch_url_and_http_request_tools; test_tool_loop_and_prompt_transcript(); test_tool_catalog_and_recommended_sections(); test_tool_allowlist_limits_prompt_and_execution(); test_write_and_append_file_tools(); test_replace_in_file_tool(); test_fetch_url_and_http_request_tools(); print('TOOL_TEST_PASS')"
```

覆盖：
- 工具目录。
- 推荐工具详情。
- 工具调用循环。
- 工具 allowlist。
- 文件写入 / 追加 / 替换。
- fetch_url / http_request。

### Capability Router

```bash
python3 -c "from agent_py_agent.tests.test_capabilities import test_skill_card_parsing, test_skill_registry_and_capability_router, test_tool_specs_become_capability_cards, test_zero_limit_means_unlimited; test_skill_card_parsing(); test_skill_registry_and_capability_router(); test_tool_specs_become_capability_cards(); test_zero_limit_means_unlimited(); print('CAPABILITY_TEST_PASS')"
```

覆盖：
- `SKILL.md` 解析。
- Skill Registry 扫描。
- ToolSpec -> CapabilityCard。
- skill/tool 统一检索。
- `0` 表示不限制。

## 完整冒烟测试

命令：

```bash
python3 agent_py_agent/tests/run_tests.py
```

运行前请确认：
- 当前模型后端是否是 `echo`。
- 或者你接受它按配置请求真实模型 API。

如果默认配置是远端模型，不建议睡前直接跑完整脚本。

## 历史记录

早期验证曾覆盖：
- `py_compile`
- `python3 -m agent_py_agent --help`
- `run` + `--inject` + `--no-save`
- `remember`
- `memory-search`
- `spawn-subagents`
- chat `/help`、`/remember`、`/memory`、`/btw`、`/subagents`
- 错误入口 `unknown-command` 返回非 0
- HTTP 后端 payload/header/解析

这些记录的旧证据文件在 `validation/` 下。当前开发判断以本文件的推荐定向测试为准。
