# TESTS

这份文档记录当前推荐的测试方式。

测试策略约定：
- 后续验收级、冒烟和回归测试默认直接调用真实 API，不再把 echo/fake backend 的结果当作最终通过依据。
- 纯解析、纯函数和局部单元测试可以作为定位辅助，但收口时必须补跑真实 API 路径。
- `agent_py_agent/tests/run_tests.py` 是当前标准完整冒烟入口，会按当前配置请求真实模型 API，使用临时配置隔离 memory/subagent 数据，并自动发现运行 `agent_py_agent/tests/test_*.py` 里的所有 `test_` 函数。
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
python3 -m agent_py_agent subagents-patches --help
python3 -m agent_py_agent subagents-dispatch --help
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
- patch 审核 dry-run / apply。
- applied patch 必须先有 `review_status=APPROVED` 才能通过验收。
- planned / blocked / 未知状态 patch 会阻断验收。
- 父代理 dispatch dry-run / apply。
- dispatch apply 后的 runner 执行和验收闭环。
- evidence / capability_requests / artifacts / tests / patches / lessons / next_actions 写回。

### Tools

```bash
python3 -c "from agent_py_agent.tests.test_tools import test_tool_loop_and_prompt_transcript, test_tool_catalog_and_recommended_sections, test_tool_allowlist_limits_prompt_and_execution, test_write_and_append_file_tools, test_replace_in_file_tool, test_fetch_url_and_http_request_tools; test_tool_loop_and_prompt_transcript(); test_tool_catalog_and_recommended_sections(); test_tool_allowlist_limits_prompt_and_execution(); test_write_and_append_file_tools(); test_replace_in_file_tool(); test_fetch_url_and_http_request_tools(); print('TOOL_TEST_PASS')"
```

覆盖：
- 工具目录。
- 推荐工具详情。
- 工具调用循环。
- 主代理自然语言派工工具：`create_subagents` / `subagent_board` / `dispatch_subagents`。
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

安装入口验证：

```bash
python -m pip install -e .
my-agent --help
my-agent subagents-dispatch --watch --max-cycles 1 --interval 0
my-agent daemon --max-cycles 1 --interval 0 --max-runners auto --no-planner
my-agent gateway run --max-cycles 1 --interval 0 --max-runners 0 --no-planner
my-agent gateway start
my-agent gateway ask "真实 API gateway ask 冒烟"
my-agent chat --gateway --no-save
my-agent
my-agent gateway stop --kill
```

这条命令应直接调用真实 API。若失败，先看真实 API 错误、模型输出协议、工具调用和 Windows/UTF-8 捕获问题，不要直接降级到 echo 后端作为通过结论。

完整冒烟脚本必须做到：
- 编译所有 agent 模块。
- 跑 CLI 真实入口。
- 跑一次真实模型 `run`。
- 创建隔离临时配置，避免污染默认 `data/memory.jsonl` 和 `data/subagents/`。
- 跑一次真实 API `subagent-run --execute`，要求真实后端、工具调用、结构化输出、证据写回和父代理验收闭环。
- 检查 `subagents-dispatch --help`，并跑一次隔离的 `subagents-dispatch --watch --max-cycles 1 --interval 0`。
- 跑一次隔离的 `subagents-dispatch --watch --planner --max-cycles 1 --interval 0`，确认父代理 planner 能通过真实 API 唤醒。
- 跑一次隔离的 `daemon --max-cycles 1 --interval 0 --max-runners auto --no-planner`，确认配置驱动常驻入口可安全退出，并确认 `auto` 参数能解析。
- 跑一次隔离的 `gateway status` 和 `gateway run --max-cycles 1 --interval 0 --max-runners 0 --no-planner`，确认 gateway 控制面文件可写且单轮安全退出。
- 跑一次隔离的 `gateway start -> gateway ask -> gateway status -> gateway stop`，确认本地 inbox/response 通道会触发真实 API。
- 跑一次隔离的 `chat --gateway --no-save`，确认 chat 可以作为 gateway 客户端投递普通消息。
- 跑一次隔离的无子命令 `my-agent`，确认会自动启动 gateway 并进入 gateway chat。
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
