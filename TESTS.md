# TESTS

这份文档记录当前推荐的测试方式。

测试策略约定：
- 后续验收级、冒烟和回归测试默认直接调用真实 API，不再把 echo/fake backend 的结果当作最终通过依据。
- 纯解析、纯函数和局部单元测试可以作为定位辅助，但收口时必须补跑真实 API 路径。
- `agent_py_agent/tests/run_tests.py` 是当前标准完整冒烟入口，会按当前配置请求真实模型 API，使用临时配置隔离 memory/subagent 数据，并自动发现运行 `agent_py_agent/tests/test_*.py` 里的所有 `test_` 函数。
- `my-agent scenario-test` 是当前推荐的可观察全流程入口：它会新建临时 fixture，走 gateway ask、主代理派工、真实 runner、父代理验收，并把所有状态关进 `workspace_root`。
- `my-agent scenario-test --case verification`、`--case gateway-restart`、`--case gateway-cross-day-resume`、`--case gateway-multi-worker`、`--case gateway-stale-lease`、`--case parent-subagent-cross-day-resume`、`--case structured-repair` 和 `--case runner-retry` 是坏天气/恢复场景入口，分别覆盖验收防作弊、gateway processing 请求恢复、gateway 跨天恢复、多 worker 并发抢占、stale lease 重排恢复、parent/subagent runner 跨天恢复、结构化输出损坏修复和 runner 临时失败重试。
- 运行前确认 `AGENT_API_KEY`、`api_base`、`model_name` 指向本轮要验收的真实后端。

## 推荐快速检查

## 可见真实环境测试台

第一版 Live Lab 已经加到 `scripts/`：

```bash
# 在当前终端跑，不调用真实模型，适合开发后快速回归
python3 scripts/live_agent_lab.py --suite smoke

# 在 macOS 新开一个可见 Terminal，跑真实 LLM gateway + 长链路场景
scripts/open_live_lab.sh --suite real --real-llm --timeout 300 --count 1 --max-cycles 2
```

它会把每轮测试关进 `validation/live_lab/<run-id>/fixture_project`，并打印/保存：
- 发给 `my-agent` 的 prompt。
- 实际执行的 CLI 命令。
- stdout / stderr / exit code / 耗时。
- transcript、summary、response、scenario summary 等证据路径。

`--suite smoke` 默认会把后端临时覆盖成 `echo`，不烧真实 API；`--suite real` 和 `--suite all` 必须显式传 `--real-llm`。

中途想停下一轮长测试时，在另一个终端执行输出里提示的：

```bash
touch validation/live_lab/<run-id>/STOP
```

### Log Analysis Live Lab replay

```bash
# SecurityAlertV1 离线 replay；不调用真实 LLM 或网络。
python3 scripts/live_agent_lab.py --suite log-analysis

# 只验收日志分析产物时可直接跑这个入口。
python3 scripts/live_lab/log_analysis_replay.py --output-root /tmp/通道运行时-log-replay
```

预期 JSON 摘要：`ok=true`、`dry_run=true`、`total_events=3`、`stored_events=3`、`case_count=1`，`stages` 全部为 `pass`，并打印 `parsed_events`、`failed_stage`、`error_type`、`error_message`、`case_path`、`route_path`、`report_path`、`evidence_paths`。失败时 `failed_stage` 会指出卡在 ingest、detector、case、route、evidence 或 report。默认 fixture 是正向 replay；`validation/security_fixtures/security_alert_v1_no_findings.jsonl` 是可读负向 fixture，应该在 `detector` 阶段因无 finding 失败。

CLI quickstart 验收：

```bash
my-agent logs status
my-agent logs ingest validation/security_fixtures/security_alert_v1.jsonl --source-id fixture --format jsonl
my-agent logs query --start-time 2026-04-30T09:30:00Z --end-time 2026-04-30T10:30:00Z --attacker-ip 198.51.100.23
my-agent logs hunt-ip 198.51.100.23 --start-time 2026-04-30T09:30:00Z --end-time 2026-04-30T10:30:00Z
my-agent logs trace-case case-1 --start-time 2026-04-30T09:30:00Z --end-time 2026-04-30T10:30:00Z
```

验收重点：`ingest` 存入 3 条 fixture event；`query` 和 `hunt-ip` 能返回可读摘要与 evidence path；`trace-case` 能围绕 case seed 返回 evidence。普通 `run/chat` 任务不应出现安全工具；明显安全日志话术或显式 `logs/security` grant 才会出现 `security_query`、`security_hunt_ip`、`security_trace_case`。

## 并行开发工作台检查

workstream 脚本不直接修改主仓库代码；用于创建、打开和检查隔离 `git worktree`：

```bash
bash -n scripts/workstream_*.sh scripts/open_workstream.sh
scripts/workstream_status.sh
scripts/workstream_create.sh --help
scripts/workstream_create.sh memory --dry-run
scripts/open_workstream.sh --help
```

语法检查：

```bash
python3 -m py_compile agent_py_agent/agent/*.py agent_py_agent/__main__.py
```

CLI 入口：

```bash
python3 -m agent_py_agent --help
python3 -m agent_py_agent status
python3 -m agent_py_agent timeline --limit 5
python3 -m agent_py_agent subagent-run --help
python3 -m agent_py_agent subagents-route-capabilities --help
python3 -m agent_py_agent subagents-acceptance --help
python3 -m agent_py_agent subagents-patches --help
python3 -m agent_py_agent subagents-dispatch --help
python3 -m agent_py_agent local-doctor
python3 -m agent_py_agent local-rebuild --source fts
python3 -m agent_py_agent adapter file --help
python3 -m agent_py_agent local-store-status
python3 -m agent_py_agent local-search "表格" --source-type memory
```

Git 空白检查：

```bash
git diff --check
```

## 定向功能测试

### Subagent / Capability / Runner

```bash
python3 -c "from agent_py_agent.tests import test_agent; tests=[fn for name, fn in sorted(vars(test_agent).items()) if name.startswith('test_') and callable(fn)]; [fn() for fn in tests]; print(f'AGENT_TEST_PASS total={len(tests)}')"
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
- 验收按真实工具执行记录核对 `read_file/write_file`；runner 的 `actual_tools` 会落成系统证据，避免模型 evidence 换写法时误判，并检查本地 artifact 路径是否存在。
- patch 审核 dry-run / apply。
- applied patch 必须先有 `review_status=APPROVED` 才能通过验收。
- planned / blocked / 未知状态 patch 会阻断验收。
- 父代理 dispatch dry-run / apply。
- dispatch apply 后的 runner 执行和验收闭环。
- evidence / capability_requests / artifacts / tests / patches / lessons / next_actions 写回。

### Subagent Workflow

```bash
python -m pytest agent_py_agent/tests/test_subagent_workflow_config.py agent_py_agent/tests/test_subagent_workflow_templates.py agent_py_agent/tests/test_subagent_quality_contract.py
```

覆盖：
- `subagent_workflow_mode` 的 `auto/manual/off` 配置和非法值回退。
- 内置 workflow 模板加载、用户 JSON 模板覆盖和坏模板校验。
- 每个模板必须声明 `solves`，避免只写 workflow 名称而不说明解决问题。
- `QualityContract`、`ContextManifest` 和 `context_packs` 能落进 `task.json`。
- 旧子代理 task 缺少新字段时仍能读取。
- `execution_context.json` 和 `EXECUTION_CONTEXT.md` 包含质量契约、上下文清单和父会话 final gate 规则。

### Tools

```bash
python3 -c "from agent_py_agent.tests.test_tools import test_tool_loop_and_prompt_transcript, test_tool_catalog_and_recommended_sections, test_tool_call_parser_accepts_subagent_call_alias, test_tool_call_parser_accepts_qwen_xmlish_read_call, test_tool_call_parser_accepts_qwen_xmlish_write_call, test_tool_call_parser_reports_incomplete_qwen_xmlish_call, test_tool_allowlist_limits_prompt_and_execution, test_write_and_append_file_tools, test_replace_in_file_tool, test_fetch_url_and_http_request_tools; test_tool_loop_and_prompt_transcript(); test_tool_catalog_and_recommended_sections(); test_tool_call_parser_accepts_subagent_call_alias(); test_tool_call_parser_accepts_qwen_xmlish_read_call(); test_tool_call_parser_accepts_qwen_xmlish_write_call(); test_tool_call_parser_reports_incomplete_qwen_xmlish_call(); test_tool_allowlist_limits_prompt_and_execution(); test_write_and_append_file_tools(); test_replace_in_file_tool(); test_fetch_url_and_http_request_tools(); print('TOOL_TEST_PASS')"
```

覆盖：
- 工具目录。
- 推荐工具详情。
- 工具调用循环。
- 主代理自然语言派工工具：`create_subagents` / `subagent_board` / `dispatch_subagents`。
- 工具调用解析兼容 `[SUBAGENT_CALL]` 错标记、Qwen/通道运行时 常见的 XML-ish `<tool_call><function=...><parameter=...>` 方言，并拦截同轮重复编排调用。
- 半截 XML-ish 工具调用会变成 `__parse_error__` 工具结果，避免把整轮主代理流程炸掉。
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

### Local Store

```bash
python3 -c "from agent_py_agent.tests import test_local_store as t; [fn() for name, fn in sorted(vars(t).items()) if name.startswith('test_') and callable(fn)]; print('LOCAL_STORE_TEST_PASS')"
```

覆盖：
- SQLite 记录表和审计事件写入。
- FTS5 可用时走全文检索，不可用时自动退回 LIKE。
- 正文落到文件系统。
- 审计事件追加到 JSONL。
- `timeline()` 能按 source_type / event_type 过滤事件。
- `JsonlMemory` 新记忆自动索引到 LocalStore。
- 旧 `memory.jsonl` 可通过 `index_all()` / `local-index-memory` 补建索引。
- subagent 工单、execution context 和 runner result 会索引到 LocalStore。
- gateway ask 请求从 queued 到 completed 会索引到 LocalStore。

## 标准完整冒烟测试

命令：

```bash
python3 agent_py_agent/tests/run_tests.py
```

安装入口验证：

```bash
python -m pip install -e .
my-agent --help
my-agent scenario-test
my-agent scenario-test --case verification
my-agent scenario-test --case gateway-restart
my-agent scenario-test --case structured-repair
my-agent scenario-test --case runner-retry
my-agent subagents-dispatch --watch --max-cycles 1 --interval 0
my-agent daemon --max-cycles 1 --interval 0 --max-runners auto --no-planner
my-agent gateway run --max-cycles 1 --interval 0 --max-runners 0 --no-planner
my-agent gateway start
my-agent gateway ask "真实 API gateway ask 冒烟"
my-agent chat --gateway --no-save
my-agent status --json
my-agent timeline --limit 5
my-agent local-store-status
my-agent local-index-memory
my-agent local-doctor
my-agent local-rebuild --source fts
my-agent adapter file --help
my-agent local-search "表格" --source-type memory
my-agent local-search "gateway" --source-type gateway_request
my-agent local-search "subagent" --source-type subagent_run
my-agent
my-agent gateway stop --kill
```

这条命令应直接调用真实 API。若失败，先看真实 API 错误、模型输出协议、工具调用和 Windows/UTF-8 捕获问题，不要直接降级到 echo 后端作为通过结论。

完整冒烟脚本必须做到：
- 编译所有 agent 模块。
- 跑 CLI 真实入口。
- 跑一次真实模型 `run`。
- 跑一次 `scenario-test` 或等价隔离场景，观察 gateway ask -> 主代理派工 -> dispatch runner -> 验收闭环。
- 跑 `scenario-test --case verification`，确认伪造 artifact / 模型自称完成不会通过验收。
- 跑 `scenario-test --case gateway-restart`，确认 gateway 崩溃遗留的 processing 请求会退回 pending。
- 跑 `scenario-test --case structured-repair`，确认坏 `[SUBAGENT_RESULT]` 会触发修复回合并被验收。
- 跑 `scenario-test --case runner-retry`，确认临时 runner 失败会在下一轮 dispatch 有限重试并最终验收。
- 创建隔离临时配置，避免污染默认 `data/memory.jsonl` 和 `data/subagents/`。
- 跑一次真实 API `subagent-run --execute`，要求真实后端、工具调用、结构化输出、证据写回和父代理验收闭环。
- 检查 `subagents-dispatch --help`，并跑一次隔离的 `subagents-dispatch --watch --max-cycles 1 --interval 0`。
- 跑一次隔离的 `subagents-dispatch --watch --planner --max-cycles 1 --interval 0`，确认父代理 planner 能通过真实 API 唤醒。
- 跑一次隔离的 `daemon --max-cycles 1 --interval 0 --max-runners auto --no-planner`，确认配置驱动常驻入口可安全退出，并确认 `auto` 参数能解析。
- 跑一次隔离的 `gateway status` 和 `gateway run --max-cycles 1 --interval 0 --max-runners 0 --no-planner`，确认 gateway 控制面文件可写且单轮安全退出。
- 跑一次隔离的 `gateway start -> gateway ask -> gateway status -> gateway stop`，确认本地 inbox/response 通道会触发真实 API。
- 跑一次隔离的 `chat --gateway --no-save`，确认 chat 可以作为 gateway 客户端投递普通消息。
- 跑一次隔离的无子命令 `my-agent`，确认会自动启动 gateway 并进入 gateway chat。
- 跑一次 `status --json` 和 `timeline --limit 5`，确认统一观察入口可读。
- 跑一次 `local-store-status`、`local-index-memory`、`local-doctor`、`local-rebuild` 和 `local-search`，确认本地事实源路径隔离、旧记忆可补建、SQLite/FTS5/LIKE 查询可用。
- 跑一次 `adapter file --help`，并用局部测试覆盖 inbox -> gateway -> outbox 文件适配器。
- 确认 gateway request、subagent run、runner result、dispatch/acceptance 等关键流程都有对应 LocalStore 记录或事件。
- 自动发现测试会覆盖主代理从工具调用创建子代理、读取子代理看板、dry-run 调度，以及防止 `execute_runners=true` 在未 `apply=true` 时误触发真实 runner。
- 通过自动发现测试覆盖 dispatch 规划、父代理 planner、runner、patch 审核、验收、watch 循环和 watch lock。
- 跑 chat 真实模型路径。
- 自动发现并运行所有 `test_*.py` 中的 `test_` 函数。
- 不允许手写测试清单漏掉新增测试。

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
## 2026-04-30 Runtime LOG Integration Checks

Focused integration:

```bash
python -m pytest agent_py_agent\tests\test_runtime_capabilities.py agent_py_agent\tests\test_log_analysis_query.py agent_py_agent\tests\test_log_analysis_cli.py agent_py_agent\tests\test_live_lab_log_analysis_replay.py agent_py_agent\tests\test_subagent_workflow_planner.py
```

Expected result: `23 passed`.

Offline Live Lab replay:

```bash
python scripts\live_lab\log_analysis_replay.py --output-root %TEMP%\通道运行时-log-replay-会话运行时
```

Expected result: JSON summary with `ok=true`, `dry_run=true`, `total_events=3`, `stored_events=3` on a fresh output root, `case_count=1`, and all stages marked `pass`.

Full regression:

```bash
python -m pytest
git diff --check
```

Expected result for this landing: `223 passed`, then no whitespace errors.
