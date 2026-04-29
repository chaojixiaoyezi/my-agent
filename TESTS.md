# TESTS

这份文档记录当前推荐的测试方式。

测试策略约定：
- 后续验收级、冒烟和回归测试默认直接调用真实 API，不再把 echo/fake backend 的结果当作最终通过依据。
- 纯解析、纯函数和局部单元测试可以作为定位辅助，但收口时必须补跑真实 API 路径。
- `agent_py_agent/tests/run_tests.py` 是当前标准完整冒烟入口，会按当前配置请求真实模型 API。
- 运行前确认 `AGENT_API_KEY`、`api_base`、`model_name` 指向本轮要验收的真实后端。

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

## 标准完整冒烟测试

命令：

```bash
python3 agent_py_agent/tests/run_tests.py
```

这条命令应直接调用真实 API。若失败，先看真实 API 错误、模型输出协议、工具调用和 Windows/UTF-8 捕获问题，不要直接降级到 echo 后端作为通过结论。

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

这些记录的旧证据文件在 `validation/` 下。当前开发判断以真实 API 完整冒烟和本文件的定向测试共同为准；最终收口以真实 API 路径为准。
