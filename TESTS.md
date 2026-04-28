# TESTS

## 已运行

命令：
```bash
python3 agent_py_agent/tests/run_tests.py
```

结果：PASS。

覆盖：
- `py_compile`
- `python3 -m agent_py_agent --help`
- `run` + `--inject` + `--no-save`
- `remember`
- `memory-search`
- `spawn-subagents`
- 错误入口 `unknown-command` 返回非 0，不是静默失败

证据：`validation/test-report.txt`

## API 配置回归
命令：`python3 agent_py_agent/tests/run_tests.py`
结果：PASS。
证据：`validation/test-report-api-config.txt`

## HTTP 后端测试
- `test_backends.py` 离线验证 OpenAI-compatible payload/header/解析：PASS。
- `test_backends.py` 离线验证 Anthropic-compatible payload/header/解析：PASS。
- CLI 回归：`validation/test-report-http-backends.txt`，PASS。

## 循环智能体测试
命令：`python3 agent_py_agent/tests/run_tests.py`
结果：PASS。
覆盖：`chat`、`/help`、`/remember`、`/memory`、`/inject`、`/subagents`、普通对话、`/exit`。
证据：`validation/test-report-chat-loop.txt`
