# REFACTORING BACKLOG

LLM: keep this file current. Do not copy old split plans back in.

当前方向：

- 不按行数强拆文件。
- 优先合并只转发调用、隐藏主链路、保留历史路径名或历史字段的层。
- 只在一个文件同时承担无关职责时拆分。
- 每次改主链路后运行对应 focused tests，再刷新 `CODE_SIZE_REPORT.md`。

---

## Active Items

## Completed Cleanup

- 2026-06-06: 删除第一批只转发/影子入口。
  - `delivery_contract_prompting_recovery_bool.py` 并入唯一调用方 `delivery_contract_prompting_staged.py`。
  - `coordinator_seed_tools.py` 并入 `orchestration/create_policy.py`。
  - `orchestration/runner_instruction.py` 并入 `orchestration/dispatch/tool.py`。
  - `tooling/filesystem_write.py` 删除，测试和调用改走 `tooling/filesystem.py` 主入口。
  - `cli/memory_commands.py` 删除；实际 Python 导入一直走 `cli/memory_commands/__init__.py`，该文件只是同名影子入口。
  - `conversation/store.py` 和 `collaboration/store.py` 删除，公开 Store 类放回真实实现文件。
  - 已验证：focused pytest、py_compile、doc sync 均通过。

1. `agent_py_agent/agent/subagents/manager.py`
   - 当前定位：子代理管理主入口，允许比以前更大。
   - 下一步只在职责明显分叉时拆；不要再拆出基础 manager 薄层。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q`

2. `agent_py_agent/agent/gateway_parts/request_execution.py`
   - 当前定位：gateway 请求执行主链路。
   - 下一步优先排查慢响应和上下文膨胀；只有出现无关职责才拆。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_gateway_request_runtime_errors.py agent_py_agent/tests/test_gateway_chat_conversation_context.py -q`

3. `agent_py_agent/agent/agent_core/orchestration/dispatch/mixin.py`
   - 当前定位：主代理 dispatch/watch 入口。
   - 下一步保留一条清晰调用链，避免新增转发层或历史参数层。
   - 验证：`python3 -m pytest agent_py_agent/tests/test_dispatch_mixin.py agent_py_agent/tests/test_orchestration_dispatch_subagents_tool.py -q`
