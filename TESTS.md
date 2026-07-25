# TESTS

当前测试文档只保留常用入口。完整文件清单以 `agent_py_agent/tests/` 为准，不再手工维护旧表格。

## Focused Commands

```bash
python3 -m pytest agent_py_agent/tests/test_owner_resolver.py agent_py_agent/tests/test_config_normalize.py -q
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q
python3 -m pytest agent_py_agent/tests/test_lease.py agent_py_agent/tests/test_gateway_heartbeat.py -q
python3 -m pytest agent_py_agent/tests/test_planner.py agent_py_agent/tests/test_agent/test_dispatch_capability_followup.py -q
python3 -m pytest agent_py_agent/tests/test_code_size_script.py agent_py_agent/tests/test_architecture_guardrails.py -q
python3 -m pytest agent_py_agent/tests/test_sandbox.py agent_py_agent/tests/test_owner_scoped_pip_env.py agent_py_agent/tests/test_path_access_owner_scope.py agent_py_agent/tests/test_graceful_shutdown.py -q
python3 -m pytest agent_py_agent/tests/test_shell_orphan_kill.py agent_py_agent/tests/test_process_registry_tools.py agent_py_agent/tests/test_shell_bg_log_cap.py -q
python3 -m pytest agent_py_agent/tests/test_container_install.py agent_py_agent/tests/test_check_clean_package.py -q
python3 -m pytest agent_py_agent/tests/test_mcp_registration.py agent_py_agent/tests/test_offline_contract_matrix_gate.py -q
python3 -m pytest agent_py_agent/tests/test_tool_input_completion_provenance.py agent_py_agent/tests/test_tool_input_schema.py agent_py_agent/tests/test_tool_call_policy_contract.py agent_py_agent/tests/test_tool_call_parameter_gate_wiring.py agent_py_agent/tests/test_runtime_gate_integration.py agent_py_agent/tests/test_backends_tool_schema.py agent_py_agent/tests/test_backends_tool_schema_precise.py agent_py_agent/tests/test_mcp_registration.py -q
python3 -m pytest agent_py_agent/tests/test_owner_object_store.py agent_py_agent/tests/test_scale_runtime.py agent_py_agent/tests/test_runtime_schema.py agent_py_agent/tests/test_deploy_manifests.py agent_py_agent/tests/test_continuous_monitor.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_adapter_feishu.py agent_py_agent/tests/test_asgi_ingress.py agent_py_agent/tests/test_scale_downstream.py agent_py_agent/tests/test_session_lock.py agent_py_agent/tests/test_persona_write_guard.py -q
python3 -m pytest agent_py_agent/tests/test_delivery_service.py agent_py_agent/tests/test_channel_message_tool.py agent_py_agent/tests/test_background_main_wake_recall.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_main_agent_delivery_closeout.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_conversation_compact.py agent_py_agent/tests/test_gateway_verbose_progress.py agent_py_agent/tests/test_session_search_tool.py agent_py_agent/tests/test_gateway_per_user_scoping.py -q
python3 -m pytest agent_py_agent/tests/test_gateway_identity_trust.py agent_py_agent/tests/test_gateway_http.py agent_py_agent/tests/test_gateway_http_runtime_errors.py agent_py_agent/tests/test_gateway_per_user_scoping.py -q
python3 -m pytest agent_py_agent/tests/test_conversation_control_commands.py agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_adapter_manager.py agent_py_agent/tests/test_thread_interrupt.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_tool_model_generation.py -q
python3 -m pytest agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_runtime_gate_ledger.py agent_py_agent/tests/test_orchestration_tool_specs.py agent_py_agent/tests/test_wait_tool_self_wake.py -q
python3 -m pytest agent_py_agent/tests/test_conversation_goal_tools.py agent_py_agent/tests/test_conversation_control_commands.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_audit_activation.py agent_py_agent/tests/test_watch_audit_guarantee.py -q
python3 -m pytest agent_py_agent/tests/test_orchestration_create_subagents_tool.py agent_py_agent/tests/test_orchestration_create_subagents_items.py agent_py_agent/tests/test_final_exit_contract.py agent_py_agent/tests/test_path_access_owner_scope.py -q
python3 -m pytest agent_py_agent/tests/test_ingestion_harvester.py agent_py_agent/tests/test_ingestion_puller_cursor.py agent_py_agent/tests/test_watch_spool_takeover.py -q
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_structured_output.py agent_py_agent/tests/test_live_lab_model_preflight.py -q
python3 -m pytest agent_py_agent/tests/test_tool_operation_idempotency.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tool_unresolved_runtime_issue_guard.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py agent_py_agent/tests/test_runtime_gate_ledger.py -q
python3 -m pytest agent_py_agent/tests/test_log_redaction.py agent_py_agent/tests/test_registry_resilience_contract.py agent_py_agent/tests/test_tool_context_reducer.py agent_py_agent/tests/test_tool_output_externalizer.py agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_memory_artifact_read.py agent_py_agent/tests/test_tooling_filesystem.py agent_py_agent/tests/test_mcp_client.py agent_py_agent/tests/test_mcp_registration.py -q
```

`test_main_agent_delivery_closeout.py` 同时覆盖显式 `submit_for_acceptance` 与模型自然结束两条收口路径：
机器完成协议替换最终回复时，必须保留非权威用户摘要，并由通道投影清除内部状态块和宿主绝对路径。

`test_final_exit_contract.py` 区分内部 closeout 与用户交付形式：纯分析可以 message
收口，显式 artifact contract/expected output 缺文件才必须返工。持续目标测试覆盖一会话
一个未完成 goal、暂停/恢复、`/stop` 暂停 goal、去重续跑和精确 thread/task 工具边界。

Gateway 会话控制回归还必须覆盖：已有 linked live turn 时 `/btw` 只写 guidance、不发布
第二个 wake；`/stop` 在线程阻塞于模型 JSON/SSE 读取时主动关闭响应，并以用户中断结束，
不误判为 provider 网络故障或等完整超时。测试必须使用生产同样的 wall-timeout guard 子线程，
不能只证明在任务登记线程内直接调 provider 的简化情况。

Gateway/IM 投递回归还必须覆盖：同一进度批次重试使用稳定 provider 幂等键，不同 progress cursor 与
最终回复使用不同键。身份只取可信 message ID、request ID、phase 和 cursor，不能从回复正文猜测；
否则平台可能把同一请求后续的真实进度或最终回复当作重复消息吞掉。

停止后续接回归必须覆盖：新 gateway request 的 `task_id` 与原持久 task 不同时，`/btw` 仍从
`task_attributes.conversation_task_id` 消费一次；interrupted 候选以精确 `task_id/status/path` 进入模型
上下文；错误 select 后所有 `promotes_task` 工具仍返回 `CONVERSATION_WORKSPACE_DECISION_REQUIRED`，
不得懒创建本轮任务目录；只有精确 select 或显式 start 后才允许工作。

容器节点真验收不能只看单测：最终镜像必须运行
`python -m agent_py_agent.agent.tooling.sandbox --quiet` 并退出 0。工作树检查使用
`python3 scripts/check_clean_package.py --mode worktree .`；wheel/tar 发布前再以
`--mode artifact <制品>` 检查实际成员和大小预算。

## Full Command

```bash
python3 -m pip install -e ".[dev,secrets,scale]"
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts
```

默认测试集覆盖 secrets 加密与 scale 存储/队列，因此 CI 和全新开发环境必须显式安装三套正式
extras；不能依赖宿主机碰巧已有 cryptography/SQLAlchemy，也不能用 skip 把缺依赖伪装成通过。
生产 wheel 使用 PEP 517 默认隔离构建，让 `[build-system].requires` 独立决定构建后端。

真实主代理/子代理链路通过后，再提交和推送。

多个外部写的专项回归必须覆盖：同轮保持模型原顺序；每项结果独立留痕；权威 operation 终态写入失败
时把表面成功降级为 unknown 且同操作不再执行；archive、runtime event、机械 compact 续跑和语义 compact
都保留 operation/effect 事实。该回归不要求通用 Saga，也不能用自然语言猜依赖或补偿动作。

路径极端回归还必须覆盖：显式未授权绝对路径保持原目标身份并返回 `WRITE_FORBIDDEN`，原目标与任务
`output/` 下的替代路径都不得生成；失败前后的独立合法写仍能按顺序完成。相对 `output/...`、`work/...`
的结构化任务落位继续生效，不能用绝对路径静默搬运兼容层冒充成功。

真实本地模型回归示例：

```bash
python3 scripts/live_agent_lab.py --suite main-artifact --real-llm --timeout 900
python3 scripts/live_agent_lab.py --suite tool-recovery --real-llm --timeout 900
```

真实模式会先发起一次模型调用；key 仅存在但不可用、响应为空或 endpoint 失败都会直接终止，不会把专项
harness 的离线结果冒充真实模型结果。
