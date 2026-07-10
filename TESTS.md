# TESTS

当前测试文档只保留常用入口。完整文件清单以 `agent_py_agent/tests/` 为准，不再手工维护旧表格。

## Focused Commands

```bash
python3 -m pytest agent_py_agent/tests/test_owner_resolver.py agent_py_agent/tests/test_config_normalize.py -q
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q
python3 -m pytest agent_py_agent/tests/test_lease.py agent_py_agent/tests/test_gateway_heartbeat.py -q
python3 -m pytest agent_py_agent/tests/test_planner.py agent_py_agent/tests/test_agent/test_dispatch_capability_followup.py -q
python3 -m pytest agent_py_agent/tests/test_code_size_script.py agent_py_agent/tests/test_architecture_guardrails.py -q
python3 -m pytest agent_py_agent/tests/test_sandbox.py agent_py_agent/tests/test_owner_scoped_pip_env.py agent_py_agent/tests/test_graceful_shutdown.py -q
python3 -m pytest agent_py_agent/tests/test_container_install.py agent_py_agent/tests/test_check_clean_package.py -q
python3 -m pytest agent_py_agent/tests/test_mcp_registration.py agent_py_agent/tests/test_offline_contract_matrix_gate.py -q
python3 -m pytest agent_py_agent/tests/test_owner_object_store.py agent_py_agent/tests/test_scale_runtime.py agent_py_agent/tests/test_runtime_schema.py agent_py_agent/tests/test_deploy_manifests.py agent_py_agent/tests/test_continuous_monitor.py -q
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_structured_output.py agent_py_agent/tests/test_live_lab_model_preflight.py -q
```

容器节点真验收不能只看单测：最终镜像必须运行
`python -m agent_py_agent.agent.tooling.sandbox --quiet` 并退出 0。工作树检查使用
`python3 scripts/check_clean_package.py --mode worktree .`；wheel/tar 发布前再以
`--mode artifact <制品>` 检查实际成员和大小预算。

## Full Command

```bash
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts
```

真实主代理/子代理链路通过后，再提交和推送。

真实本地模型回归示例：

```bash
python3 scripts/live_agent_lab.py --suite main-artifact --real-llm --timeout 900
python3 scripts/live_agent_lab.py --suite tool-recovery --real-llm --timeout 900
```

真实模式会先发起一次模型调用；key 仅存在但不可用、响应为空或 endpoint 失败都会直接终止，不会把专项
harness 的离线结果冒充真实模型结果。
