# TESTS

当前测试文档只保留常用入口。完整文件清单以 `agent_py_agent/tests/` 为准，不再手工维护旧表格。

默认按改动范围运行 focused tests，不重复运行全仓 pytest。只有本轮生产代码和测试代码新增、删除累计
约 10,000 行以上，或用户明确要求时，才追加一次 `python3 -m pytest -q --tb=short`。静态检查、文档同步、
严格代码尺寸、diff 和 clean-package 守卫仍按发布风险执行，不用全仓 pytest 代替。

测试机上的所有真机用例只使用一个真实 Gateway。需要并行时启动多个独立 TUI/会话并统一连接该实例，
不得为 input、render、lifecycle、isolation、replica 或 steer 等用例另开端口和 Gateway。每轮真机测试前先
确认只有一个 Gateway 进程和一个配置端口；故障恢复用例顺序重启或中断这个实例。合同单测中的 fake
Gateway 可以并行，但不能作为“单 Gateway 多客户端”验收的替代证据。

2026-08-21 子代理自然收口与 TUI 可观察性回归分三层执行：第一层覆盖 `turn_end`、普通自然完成、
递归 `create_subagents` 自动启动、模型工具表不含 dispatch/schedule、父级 guidance/cancel 和资源上限；
第二层覆盖 TUI Working 动画、thinking 灰色增量、条件式 follow-tail 与 Compact typed progress；第三层只在
`192.0.2.7` 的单 Gateway/真实 TUI 输入一次目标 prompt，测试者只观察产物、日志、child 树和 8080
监听，不旁路补代码。当前 backend focused 144 项、context/protocol focused 75 项已通过。由于本轮累计
增删超过 10,000 行，发布前追加一次且仅一次全仓 pytest；若发现失败，修复后只重跑失败项和相关 focused。

本轮唯一一次有效全仓测试使用仓库 `.venv` 运行到 100%，暴露 39 个失败：真实缺陷是窄终端闭合思考行
全角括号宽度漏算和 finalize 轻量参数缺少 `prompt` 时的防御读取，其余主要是测试仍断言已删除的
`SUBAGENT_RESULT`、手动 dispatch/schedule 与机器验收。修复后，对这些失败来源收集到的 468 项 focused
组合只剩 2 个测试期望/导入问题，二者精确复测 2/2 通过；动作协议/CLI 121 项、状态投影 36 项另行通过。
按用户约定不再重复全仓 pytest。

严格 code-size 初次被当前提交 `31f30fe` 自身的 25 个未登记 hard finding 阻断。为避免把存量债务冒充本轮
回归，先从 `git archive HEAD` 纯净快照生成 baseline，再修掉本轮唯一新增的 `_progress_payload` 深嵌套；
当前 strict gate 通过，原始报告中的 4 个 hard 均能在纯净基线复现，本轮新增 hard 为 0。baseline 不取
当前脏工作树，因此没有把本轮新增问题写成豁免。递归创建的同配置每次上限与新建后代空验收字段又以
44 项 hierarchy/orchestration focused 复测通过。

`.7` 真实 TUI 首轮按原样植物大战僵尸 prompt 请求 3 个 child 时，结构化回执显示
`owner_active=12/available=0`，证明旧实现把其它会话的历史未终态 run 算进当前会话容量；同时该确定性
整批拒绝被误包成 `TOOL_OPERATION_OUTCOME_UNKNOWN`。回归应锁定：当前 root 没有 child 时，即使 owner
另有 12 个可恢复 run，仍可按单次上限获得 4 个槽；容量不可读/超限均返回
`effect_outcome=not_started`，不进入副作用未知收口。

2026-08-20 TUI 灰色层级与 tmux 复制修复在本地、`192.0.2.7` 各运行 renderer/view/input/ANSI/PTY/chat
6 文件 focused 组合，均为 105 项通过。测试机仅有一个 Gateway（8420），10 个 TUI 共享；真实中文请求
约 3.09 秒出现回答，ANSI capture 证明思考为 246 灰、助手正文为 231，`tmux load-buffer -w` 中文探针
完整。外层系统剪贴板必须在用户 attach 的终端执行一次真实粘贴才可标最终通过。全仓 Ruff 20 项、strict
code-size 29 项均为修复前基线已有且本提交未新增；用户在获知后明确授权推送和测试部署，因此不能把本轮
记录写成“严格发布 gate 全绿”。

同日用户确认外层仍不能通过右键复制后，本地新增正文/输入框“已有选区时右键按下直接复制”回归：右键
事件必须在原控件前被拦截，中文宽字符全文只复制一次，配对 release 不得重复写入或清除高亮；lost right
release 仍可由无按键 motion/下一次其它 press 解锁。上述六文件 focused 组合现为 107 项通过；测试机部署
和用户本机系统剪贴板粘贴在完成前仍不得标成通过。

2026-08-18 活动输入/控制回执提交候选先运行 12 个原失败文件的 focused 组合，再运行一次修复后的完整
`pytest -q --tb=short`，两者均到 100% 且退出 0。changed-file Ruff、doc sync 和 diff check 通过；全仓
Ruff 的 112 项属于当前 HEAD 存量扫描结果，本轮变更文件为 0。strict code-size 仍有 25 个 hard finding，
因此本轮只允许部署到 `.13` 测试机，不得据此推远端分支或宣称严格发布 gate 全绿。clean-package 必须在
新增源码/测试先进入 Git index 后重跑；未跟踪正式源码被它拦住属于预期行为，不能用排除规则绕过。

2026-08-18 活动回合普通输入与上下文可观察性追补使用以下 focused 组合：

```bash
python3 -m pytest -q --tb=short \
  agent_py_agent/tests/test_chat_client_context.py \
  agent_py_agent/tests/test_tui_input.py \
  agent_py_agent/tests/test_tui_runtime.py \
  agent_py_agent/tests/test_tui_renderer.py \
  agent_py_agent/tests/test_runtime_guidance.py \
  agent_py_agent/tests/test_gateway_verbose_progress.py
```

必须分别证明：薄客户端 `/ask` payload 带精确 `message_id/expected_turn_id` 和完整执行选项；运行中 Enter
在真实 Gateway ID 已知后才进入活动回合，提交窗口的本地 `chat-*` 不得冒充 turn；pending
receipt 在真实注入事件前不进入稳定历史；外部 ID 不得移除本地 pending；Gateway 拒绝只撤销同一 receipt
并保留 follow-up queue；pending/queue 位于 fixed input-status pane 而非 transcript；rich/non-rich 客户端的
确认事件边界不扩大；active→queued 必须保留 inject/files/save/resume/client capabilities 且 chat-style 只
出现一次；guidance receipt 必须重算 embedded entry digest；队列文件名与正文 ID 冲突必须按文件名失败
归档且 provider 零调用；context usage 与 active-turn compaction 事件
不携带正文。`.13` 还必须用真实长回合
覆盖“连续两条补充、滚离尾部、注入确认、结束竞态”四步，单元测试不能替代真机通过。

Gateway chunk JSONL 的两个本机 reader 必须共享 byte-offset 合同：只消费换行已完整落盘的 UTF-8 行，末尾
半行保留原 offset，下一次补齐后恰好交付一次；暂时读取失败不得把 offset 清零造成重复。focused 使用
`test_gateway_client.py + test_gateway_streaming.py` 同时覆盖富 TUI 与普通 CLI。

## Focused Commands

```bash
python3 -m pytest agent_py_agent/tests/test_tui_reference_fixture_server.py agent_py_agent/tests/test_tui_events.py agent_py_agent/tests/test_tui_view_model.py agent_py_agent/tests/test_tui_ansi_snapshot.py agent_py_agent/tests/test_tui_markdown.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_view.py agent_py_agent/tests/test_tui_input.py agent_py_agent/tests/test_tui_interaction.py agent_py_agent/tests/test_tui_paste.py agent_py_agent/tests/test_tui_preflight.py agent_py_agent/tests/test_tui_terminal.py agent_py_agent/tests/test_tui_transcript.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_tui_pty.py agent_py_agent/tests/test_chat_prompt_queue.py agent_py_agent/tests/test_cli_chat.py agent_py_agent/tests/test_chat_parts.py agent_py_agent/tests/test_gateway_client.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_channel_message_tool.py agent_py_agent/tests/test_runtime_gate_ledger.py agent_py_agent/tests/test_tool_loop_recovery_scope.py agent_py_agent/tests/test_tool_round_execution.py -q
python3 -m pytest agent_py_agent/tests/test_owner_resolver.py agent_py_agent/tests/test_config_normalize.py -q
python3 -m pytest agent_py_agent/tests/test_subagent_manager_core.py agent_py_agent/tests/test_manager_board_class.py agent_py_agent/tests/test_subagent_coordinator_due_check.py -q
python3 -m pytest agent_py_agent/tests/test_lease.py agent_py_agent/tests/test_gateway_heartbeat.py -q
python3 -m pytest agent_py_agent/tests/test_planner.py agent_py_agent/tests/test_agent/test_dispatch_capability_followup.py -q
python3 -m pytest agent_py_agent/tests/test_code_size_script.py agent_py_agent/tests/test_architecture_guardrails.py -q
python3 -m pytest agent_py_agent/tests/test_registry_resilience_contract.py agent_py_agent/tests/test_attempt_sandbox.py agent_py_agent/tests/test_sandbox.py agent_py_agent/tests/test_tooling_shell.py agent_py_agent/tests/test_owner_scoped_pip_env.py agent_py_agent/tests/test_path_access_owner_scope.py agent_py_agent/tests/test_graceful_shutdown.py -q
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
python3 -m pytest agent_py_agent/tests/test_compact_semantic_summary.py agent_py_agent/tests/test_native_tool_ir_compact_and_orphan_sweep.py -q
python3 -m pytest agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_subagent_hierarchy_scheduler.py agent_py_agent/tests/test_subagent_hierarchy_scheduler_tool_roles.py agent_py_agent/tests/test_subagent_hierarchy_write_policy.py agent_py_agent/tests/test_subagent_capability_request_tool.py agent_py_agent/tests/test_subagent_natural_language_e2e.py agent_py_agent/tests/test_local_collaboration_subagent_integration.py agent_py_agent/tests/test_gateway_chat_conversation_context.py agent_py_agent/tests/test_tools/test_tool_loop.py agent_py_agent/tests/test_background_main_agent_runtime.py agent_py_agent/tests/test_gateway_orphan_reconciler.py -q
python3 -m pytest agent_py_agent/tests/test_cli_run_conversation.py agent_py_agent/tests/test_cli_run_provider_timeout.py agent_py_agent/tests/test_runtime_mixin.py agent_py_agent/tests/test_run_task_workspace_writer.py agent_py_agent/tests/test_memory_tool.py::test_remember_user_explicit_goes_through_candidate_and_promotes agent_py_agent/tests/test_memory_tool.py::test_remember_single_add_tool_verified_authorized_auto_but_evidence_gate_blocks agent_py_agent/tests/test_gateway_chat_conversation_context.py::test_gateway_returns_answer_and_repairs_assistant_transcript_on_next_turn -q
python3 -m pytest -q --tb=short agent_py_agent/tests/test_verification_runtime.py agent_py_agent/tests/test_verification_project_facts.py agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_model_call_ledger.py
```

终端交互 TUI parity 的 focused 命令覆盖 typed event/reducer、Markdown/diff、spinner、权限续跑、输入、
history/search/paste/completion/queue/stash、follow/unseen、session-history、真实 Gateway readiness、title、
鼠标选择/OSC52、PTY recorder 和 ANSI replay。
鼠标回归还必须覆盖：prompt_toolkit 传入的是源字符索引，中文宽字符不得再次按显示列换算或只复制一半；窗口外丢失 mouse-up 后，首个
`MouseButton.NONE` motion 或下一次 fresh press 只结束旧拖动，后续 hover 不再扩展；一次 settled selection
只自动复制一次，并同时保留 prompt_toolkit、OSC52 与 `tmux load-buffer -w` 外层剪贴板路径；iTerm2
也不得退化成只写 tmux 内部 buffer。输入回归还要覆盖鼠标松手自动
复制、Ctrl-C/右键复制且保留输入高亮、右键 press/release 只复制一次、Ctrl-V/终端 bracketed paste 替换选区，以及 marker 普通空格不会触发
`nbsp` 下划线。运行中普通输入还要证明下一次
真实模型调用能看到该输入；若 exact turn 已结束，TUI 只能挂接 Gateway 返回的 canonical queued request，
不得再次提交正文。
富 transcript 追补还必须覆盖：未声明能力的 Gateway 不公开 thinking/display 且继续按 verbose 裁剪；TUI
声明能力后逐轮 commentary、provider 明示 thinking、edit/overwrite/patch diff、write preview、命令
stdout/stderr/exit code 均走结构化事件；思考 Markdown 与 `Ctrl+O` 折叠提示的每个可见 fragment 都必须
以最终 muted/thinking role 覆盖正文前景色，不能只断言行前缀是灰色。失败后最后一次 workspace mutation 的软续跑只触发一次，简单写入
和已有后续检查不触发。供应商网络回归还要用真实 `ConnectionRefusedError/ECONNREFUSED` 证明传输层
`2/5/15` 秒三次退避、模型回合层 `10/25/45/100/180` 秒五次恢复和富 TUI typed retry 提示；普通
`"connection refused"` 字符串、DNS 与无效地址不得取得重试权。常用定向命令为：

```bash
python3 -m pytest -q --tb=short agent_py_agent/tests/test_provider_connection_error.py agent_py_agent/tests/test_provider_transient_auto_resume.py agent_py_agent/tests/test_runtime_error_reports.py agent_py_agent/tests/test_gateway_helpers.py agent_py_agent/tests/test_gateway_verbose_progress.py agent_py_agent/tests/test_gateway_streaming.py agent_py_agent/tests/test_gateway_client.py agent_py_agent/tests/test_tool_model_generation.py agent_py_agent/tests/test_tui_runtime.py agent_py_agent/tests/test_tui_renderer.py agent_py_agent/tests/test_tui_worker_paths.py agent_py_agent/tests/test_tool_round_execution.py agent_py_agent/tests/test_tools/test_edit_file_tool.py agent_py_agent/tests/test_tooling_filesystem_write.py agent_py_agent/tests/test_tools/test_shell_tool.py agent_py_agent/tests/test_current_turn_execution.py agent_py_agent/tests/test_runtime_guidance.py agent_py_agent/tests/test_tui_pty.py agent_py_agent/tests/test_tui_ansi_snapshot.py agent_py_agent/tests/test_tui_view.py
```
`test_tui_pty.py` 必须保存固定 TERM/locale、
终端尺寸和 reference/target fixture 身份；golden 只允许脱敏后的 ANSI/结构化片段。矩阵项目只有在
对应 test 与测试机 evidence run 同时存在时才能标为 `VERIFIED`。外部 终端交互 provider 健康不属于
UI 验收前提，参考客户端使用 loopback deterministic Anthropic fixture；MiniMax-M2.7 只用于 my-agent
真实链路，不得把 key 写入 pytest output、录屏或 fixture。

真实代码任务的交付验收必须把“命令退出 0”和“有测试覆盖”分开：`go test ./...` 出现 `[no test files]`
只能证明 package 可装载，不能证明行为测试通过。还要检查 output 总大小、隐藏目录和 cache/debug 文件；
process sandbox 回归需证明 canonical `task_work_dir/.sandbox-tmp` 承载 `/tmp`，项目 cwd 不出现
`.sandbox-tmp`，owner-scoped 环境的 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`。

写后验证新鲜度回归必须覆盖“verify 成功→workspace mutation→read/search→plain final”仍产生一次软核对；
同一 verification event 不循环提醒，执行新的真实 verify 后才能形成新周期。验证 envelope 必须从
`metadata.handler_details` 读回。模型调用账本还必须证明明细超过 `max_records` 后 request/run 的 logical、
physical、provider attempt/retry 和 status 累计数不截断；旧任务 response 的 128 不能再当精确总数。

完成收口与 operation 审计要分层回归：`succeeded mutation -> not_started tail` 仍保留 partial 审计，
但 no-effect 尾部不能触发整项 `OPERATION_INCOMPLETE`。末尾 `failed/not_started` 与 plain final 冲突时，
必须先把 `completion_conflict.v1` 和被拒绝草稿送回同一 active turn，原工具 schema 仍可调用；至少覆盖
“第一轮返工调用修复工具并形成 succeeded”“连续两次只口头完成后才安全收口”“新的失败 call id 不重置
全 turn 返工预算”。`unknown/cancelled/incomplete/unverified` 不得进入通用带工具返工，显式 required action
仍使用自己的结构化 gate，不能借该路径绕过。owner-scoped shell 环境还必须证明 `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache`、
`NPM_CONFIG_CACHE=/tmp/.cache/npm`，同时 `HOME` 只读边界和凭据擦洗不变。

终端交互 命令/输入追补至少覆盖：`/context` 使用自动 compact 同一估算而不写状态；手动 `/compact` 取得
同一 run lane、写 checkpoint 并推进 generation，live turn 时拒绝；可选摘要要求不能覆盖 operation evidence；
`/effort` 在 backend 没有结构化能力时查询成功但设置失败且不改参数；Up/Down 先走 ASCII/CJK/恰好满行的
软折视觉行；`N new messages ↓` 左键释放后恢复 follow-tail。对应 focused 文件为
`test_conversation_control_commands.py`、`test_chat_control_runtime.py`、`test_gateway_conversation_compact.py`、
`test_gateway_conversation_control.py`、`test_tui_input.py` 和 `test_tui_view.py`。

外部消息工具还要覆盖两个对称面：无 proactive owner route 时，`send_message` 实现仍注册但必须出现在
`runtime_snapshot.unavailable_tools`，且不进入 specs/retrieval；有真实 provider、target、owner root 与
proactive capability 时仍进入可见/可执行快照。对应 `test_channel_message_tool.py`，真机再用同一句普通中文
问候比较修复前后的工具块数量。

2026-08-18 命令/输入追补最终本地 89 项 focused tests、`.13` 精确 23 项到 100%；changed-file Ruff、
py_compile、doc-sync、strict code-size 与 diff check 通过。改动远小于 10,000 行，未重复全仓 pytest。
远端输出、TUI capture、进程与部署 hash 保存在
`/root/tui-parity-evidence/context-controls-20260818T1630CST/`。

2026-08-18 本轮只运行相关 focused 文件：本地与 `.13` 均到 100%（保留预期 xfail），changed-file
Ruff 与 py_compile 通过。改动远低于 10,000 行，按用户约定没有重复运行全仓 pytest。Tornado 真机任务
只证明核心 10 项测试曾通过；examples 在其后修改却未复测，所以整体结果明确记为未通过。后续
aiohttp→Go 在最终修改后重新 build、通过 29 项测试和 HTTP E2E，作为 EXEC-44 的真实闭环证据；其末尾
no-effect 清理又形成 EXEC-45 的独立反例，二者不能混写成同一结果。

测试机 evidence 分轮保存在 `/root/tui-parity-evidence/`：reference、mainchain、input-state、transcript、
interrupt、permission 和 final run。最终 `long-session-10k-optimized.json` 在 120×29 下建立 10k 回合/
20k stable block、39,999 rendered lines；idle animation tick 复用同一 frame，平均 6.4ms，真实可见状态
变化重绘平均 45.8ms。该压测与 `test_idle_animation_tick_reuses_static_long_transcript_frame` 一起证明“不在
空闲时全量重建”，不能只引用首帧时间。

本轮 TUI focused suite 327 项运行到 100%。代码与测试变更超过 10,000 行，因此额外执行一次全仓 pytest：
缓存记录收集 24,663 项，运行到 100% 且退出 0；此后不重复执行。最终 `.13` 部署后的 reducer/view/
renderer/runtime 回归 49 项和 changed-file Ruff 均通过。全仓 Ruff 尚有 119 项历史问题，而独立 clean
`main@2d5a964f` 为 122 项；本任务没有新增 lint debt。doc sync、strict code-size、diff 与 staged
clean-package 守卫通过。

`CODE_SIZE_BASELINE.json` 在本轮只登记 clean `main@2d5a964f` 已存在的 10 个严格 size finding；登记前在
独立 HEAD archive 上复跑并得到同一身份集合。新 TUI/审批/中断代码不得借该 baseline 隐藏新增 blocker，
每轮仍执行 `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json`。

CLI Memory 入口回归必须证明：user 原文在首个模型调用前进入 ConversationStore，`remember(user_explicit)`
取得唯一 user `source_message_ref` 并按统一 Promotion 晋升；相同 request/role 重放不重复，内容或
run/task lineage 漂移 fail-closed；assistant 尾部落账失败只产生 typed degradation。one-shot thread 不得
预填或遗留伪造/active task link：无 task-promoting tool 时 links 为空；真实工具晋升时只允许留下
`completed` link，且 active ids/links 为空。standalone workspace 必须进入终态，Gateway 的既有会话 lifecycle 不变。
真实 testbox 仍需另外保留 ConversationStore、candidate/formal memory、workspace、runtime event 与重放证据；
定向单测不能替代真实 provider 运行。

2026-08-12 的隔离 B5R3 已完成上述真实 provider 验证：`anthropic_compatible/deepseek-v4-flash`、CLI
RC=0、ConversationStore user/assistant=2、Candidate/formal=1/1、唯一 user message ref、workspace
`DONE`、`status_conflict=0`；同 request 纯存储重放前后全部语义计数和 ID 不变。真实工具晋升产生的
唯一 link 为 `completed`，active ids/links 为空。脱敏原始证据位于 testbox
`/root/memory-evidence/会话运行时-MEM-20260812-B5R3/`；该结果不替代 Goal 中尚未闭合的 Gateway/Curator、
第二模型、重启恢复与长文本验收。

`test_tools/test_tool_loop.py` 覆盖普通任务不会被旧进度清单劫持、显式 goal 的 open-plan 生命周期、工具轮数上限和
后台 continuation；`test_turn_end.py`、`test_subagent_finalize_helpers.py`、`test_subagent_protocol_contracts.py`
覆盖六类结束原因、自然结果保存与递归控制面；`test_conversation_goal_tools.py` 覆盖一会话一个未完成 goal、
精确创建/更新/完成边界。

主代理完成表达回归必须覆盖：普通 `task_progress` 即使仍有 open item，也只是一份可恢复的进度笔记，
不能拦截模型本轮回复、追加隐藏提醒、自动唤醒后台执行或要求下一轮先选择/关闭旧任务。只有显式持久
`/goal` 的 open plan 才保持 `unfinished` 并由既有 continuation 续跑。该行为不得解析“完成”等自然语言、
扫描任务目录、执行验证命令或给普通 task 增加完成硬门。终态普通 task 续作必须保留旧终态和 cwd、
创建新执行 task id；只有精确持久 `/goal` 可以原 id 恢复。子代理普通工具必须是父 run 快照的严格子集；
coordinator 可通过统一 `create_subagents` 继续递归创建，worker 不得获得 child-creation 工具。模型调用账本必须区分 logical turn、
物理 model attempt 和 provider HTTP attempt，并覆盖并发首次请求、重试、失败、超时和迟到 finish。
task-local child 即使携带父 conversation id，也必须证明可在自己的 runner lane 正常写入授权产物。
sticky workspace 回归还必须覆盖：新 execution 复用旧 task path 时，四份当前执行投影同步换成新
request/run/task，旧 output 与 artifact 列表保留，timeline 只追加一次；相同 task 恢复时保留既有
progress/evidence 并回到 RUNNING。损坏投影不得在激活时被静默洗掉，旧 task link 也不得覆盖新投影。

Gateway 会话控制回归还必须覆盖：已有 linked live turn 时 `/btw` 只写 guidance、不发布
第二个 wake；`/stop` 在线程阻塞于模型 JSON/SSE 读取时主动关闭响应，并以用户中断结束，
不误判为 provider 网络故障或等完整超时。测试必须使用生产同样的 wall-timeout guard 子线程，
不能只证明在任务登记线程内直接调 provider 的简化情况。本地 CLI 还必须用 worker 与界面共享的
精确 request id 测试，不能读取 thread-local `agent._current_run_params` 假装跨线程可见。所有
支持的 `/XXXX` 必须证明命令词不进入 transcript/guidance/模型；未知命令必须 fail-closed。

Gateway compact 回归必须覆盖：候选只有在包含 summary、近期 raw tail、已压缩与近期工具事实及当前
用户输入的完整投影低于精确配置阈值时才能提交；checkpoint 写失败、CAS 冲突和过大候选都不得推进
summary/cursor/generation。近期尾部只能按 user/assistant role 选择完整回合，不能分析正文；旧 v4 thread
安全加载为空的 v5 guard 字段；连续三次失败进入冷却，冷却后成功半开并清零；连续两代 checkpoint
能按 previous pointer 串联，raw transcript 始终不删。

原生工具长链还必须覆盖完整 provider-visible 计量：prompt、工具 Schema、ToolCall 参数、
ToolResult、UserTurn 与尚未转发的运行引导都要进入同一 token 估算。大 `write_file/edit_file`
参数不能因工具结果很短而漏算；达到配置阈值后只能整对移除最旧调用/结果，保留最新往返与全部
运行中用户输入，并按既有 recent-tail 预算一次取得余量。被回收旧段必须由同一 IR 中最多一条
非权威语义 summary 承接；下一次跨阈值要把前代 summary 作为输入再原位替换。摘要调用失败必须回退
机械 handoff；摘要、handoff marker 与近期尾部都要纳入同一预算。连续两次再次跨阈值时不能堆叠
summary/窗口标记、遗失最新用户纠正、留下 tool-use/tool-result 孤儿或退回 Gateway 同 turn 重启。

Gateway/IM 投递回归还必须覆盖：同一进度批次重试使用稳定 provider 幂等键，不同 progress cursor 与
最终回复使用不同键。身份只取可信 message ID、request ID、phase 和 cursor，不能从回复正文猜测；
否则平台可能把同一请求后续的真实进度或最终回复当作重复消息吞掉。
终态权威回归必须另外制造合法、截断和陈旧的孤立 `responses/<id>.json` 以及
`done/failed` 投影，证明它们不会让 provider 跳过执行、让 stale 请求消失、让活动输入误判
终态或让 CLI/TUI/plain 提前结束；只有 schema 和内外层 request ID 都匹配的
`requests/terminal/<id>.json` 可以返回最终答复。
必须再覆盖 provider 已成功、紧接着 `/stop` 先把同 attempt 写成 closing/cancel 的顺序：ACK 仍应把
已进入模型的 guidance 收成 consumed，不得永久卡在 submitted；相反 stop 在 provider admission 之前
先赢时必须零次调用 provider。audit clear、linked task stop 和 window stop 均要通过同一 T 锁路径。
同 ID 恢复回归还要在 canonical 已存在后人为放回一份 owner/prompt/options 不同的 hot
processing 文件，即使两者最终文案相同也必须保留 hot 并报冲突；只有重算后的
`gateway_request_fingerprint.v1` 一致才能幂等退役热文件。
同时在 inbox 文件已移入 processing、attempt/lease/fingerprint 栅栏尚未成功的精确写入点注入
`OSError`，验证 claim 返回失败、原请求回到 inbox，且 processing/canonical/response 都不留半成品。

Adapter durable ingress 的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_adapter_ingress.py agent_py_agent/tests/test_adapter_manager.py -q --tb=short`。
必须证明 route callback 在媒体和 POST 前已经落盘可信身份与 canonical digest；same-id/same-body 幂等，
diff-body 隔离；媒体瞬时失败停在 prepared 且 POST 尚未发生；Gateway 响应丢失按原 body 重试，
submission 落盘后崩溃不重 POST；唯一 worker 继续推进
input-status/result/control-status/placeholder，IO callback 不持 store lock；ingress/reply 两类持久 row 必须覆盖
A/B store 同时读取、租约未过期拒绝 B、过期后 B 以更大 epoch 接管、A 的旧 epoch CAS 失败。还要断言
POST 直接发送持久 `gateway_payload`，progress handle 可变，429/5xx 保持 WAIT，auth/config 隔离且不发送
伪终态正文，input `terminal_unknown` 清占位并留下 durable unknown receipt。`/btw` unknown 必须保存
`operation_id/receipt_id/control_state`，以 operation ID 而非目标 `request_id` 建 `control_receipt` watcher。
同一 target turn 的两条 `/btw` 必须生成两个 pending 文件和两份独立终态；目标初始为空时，首次 GET 可在
同 epoch CAS 绑定，后续不同 target 必须 quarantine。stop rejected 必须有明确回复，stop
`terminal_unknown` 必须清占位并留下 durable unknown receipt，二者都不能静默完成。control pending 还要
冻结 channel/user/conversation/chat type/chat id，逐项路由漂移均 fail-closed；`/control-status` GET 必须
发送这五项结构化身份头，不能把可猜的 operation ID 当鉴权能力。

Slash 控制操作回执的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_gateway_control_operation.py agent_py_agent/tests/test_gateway_conversation_control.py agent_py_agent/tests/test_gateway_http.py -q --tb=short`。
必须覆盖副作用前 prepared、执行前 executing、结果落盘 completed 和崩溃 terminal_unknown 四个切点；同一
message ID/正文只执行一次，同 ID/异正文零次新增副作用并返回冲突。`/control-status/<operation_id>` 只可
读取 authenticated owner 的回执；`/btw` 可以从已有 guidance receipt 推进 accepted/rejected，其他控制和
没有 guidance 证据的 unknown 均不能因 GET、TUI 重连或 adapter 重启而再次执行。若 wrapper 已越过
executing、但 guidance 尚未落盘就崩溃，只能在 exact turn 终态证据下收为 rejected；active、recoverable、
corrupt、absent 四种非终态证明都必须保持 terminal_unknown。群聊回执必须冻结
`channel_chat_type/channel_chat_id`；同 message ID 改群身份应在副作用前冲突，GET 对账仍进入原 group owner。
回执还必须冻结首次解析的 canonical owner ref；在 `prepared` 落盘后切换 per-owner 配置再重试，effect、steer、
stop、task link 和子代理取消仍只能命中原 owner，不能按新配置重算。
TUI 控制 outbox 的 focused 命令为
`python3 -m pytest agent_py_agent/tests/test_tui_control_delivery.py agent_py_agent/tests/test_chat_control_runtime.py agent_py_agent/tests/test_tui_input.py -q --tb=short`。
必须证明 persist-before-POST、两次传输请求复用同一 message ID、收到 operation ID 后永久 GET-only、
terminal_unknown/conflict 不换 ID 重做、重启恢复原行，以及未绑定 exact turn 时 `/btw`/`/stop` 零次发送。
长控制正在执行时，重复 POST 与 GET 还必须在有界短时间内返回同一 `executing` 回执，effect 调用次数仍为
一；原 worker 释放 C 后才能出现 completed 或确证中断后的 terminal_unknown。Gateway IO focused 还要模拟
无 fcntl 的 Windows 分支，证明 `msvcrt` lock/unlock 成对发生，而不是仅发告警后无锁运行。
还要证明明确 lock contention 才返回未领取，坏句柄等错误会上抛；两种跨进程原语都缺失时必须 fail-closed。

停止后续接回归必须覆盖：新 gateway request 的 `task_id` 与原持久 task 不同时，`/btw` 仍从
`task_attributes.conversation_task_id` 消费一次；`/stop` 只中断当前真实 live turn，没有运行内容时
不得修改旧 task/goal；停止后的下一条普通消息无需 select/start/close 命令即可聊天或在 sticky cwd
继续工作。若命中一个已终态工作目录，首个 `promotes_task` 工具自动创建本轮执行身份，旧终态保持不变。

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
