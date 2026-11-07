# S2-W6 责任映射与删除闭包

基线：`0d0519e7ebde7a436e16da5bdff41c7401e3abdf`。本表覆盖基线全部 **134** 个 `multiprocess_*.py`，而非从一次运行猜删。

## 决策与边界

- 生产部署 count=0 的依据：`owner_confirmed_all_deployments_count_0_2026_11_06`（Controller/Owner 确认，不是本机外推的部署探测）。
- 公开 `cmd_gateway_run` 的正数配置现在先执行 durable journal，再冻结本次运行的 count=0；没有修改 YAML、settings、validator 或 parameter registry。
- `_cold_gateway_paths`、`coordinated_refusal`、HTTP birth gate、standalone sources、generation retirement，以及原公共参数和签名适配均保留。
- 本轮只删除唯一静态调用者闭合的 policy 模块，把其两个定义并入已有调用者；没有重命名剩余 MP 模块。
- 旧协调器私有生命周期/元数据函数仍被白名单外测试及共享 reader 调用，保持原名；本提交**不是** 134 个文件整包删除，也没有声称所有内部旧拓扑入口被删除。
- 仅 journal 使用新离线模块；count=0 无 journal 的正常启动，以及已经完成迁移后的新 count=0 进程不导入这些离线成员。新模块名由安装的包对象枚举，不把本表当测试 oracle。

## 证据与可复现入口

隔离证据目录：`/private/tmp/my-agent-home-s2-w6/`。原静态扫描为 `closure-before.json`、`scan-before.json`；当前汇总为 `closure-summary.json`。静态匹配保守计入延迟导入和字符串形式。删除前后对整个 tracked Python 文件集合复核，未以动态未见替代静态闭包。

- D0/D1：`test_s2w6_count0_module_closure.py` 的新进程真实 CLI -> HTTP `/status` -> 正常停止；分别是新 home 和迁移后的旧 home。`trace-test_count0_serves_http_withou0.json` / `...1.json`。观察不外推真实用户或外网。
- D2：`trace-half_migration.json`，移动/manifest 中断后实际续跑。
- D3：`trace-stop_restart.json`，原 safe-restart 测试入口。
- D4：`trace-native_plugin_subagent.json`，PTY、MCP transport、controlled subagent 测试；若 rc 非零，仅作为真实导入观察，不当作整链通过。
- D5：`trace-pending_recovery.json`，原 processing-recovery 测试入口。
- 新迁移测试：`test_s2w6_single_process_migration.py`。已覆盖每阶段 durable 后中断、rename/manifest 中断、空目录不能补证、活/未知 worker 拒绝、未结取消拒绝、重复 no-op、终态后响应投影中断修复和 count0 不能绕过未完成 journal。
- Golden 全量回放有两项 UTC 日期路径漂移；同两个节点在未修改 merge-base 重现（`golden.log` / `golden-base.log`），未改 golden baseline 或放宽断言。故不把原始 `golden_rc=1` 写成通过。

当前机器计算汇总：

```json
{
  "total_before": 134,
  "deleted": 1,
  "prod_importers_before": 190,
  "prod_importers_after": 192,
  "test_importers_before": 204,
  "test_importers_after": 205,
  "traces": {
    "D0": {
      "rc": 0,
      "modules": 1788
    },
    "D1": {
      "rc": 0,
      "modules": 1788
    },
    "D2": {
      "rc": 0,
      "modules": 1457
    },
    "D3": {
      "rc": 0,
      "modules": 1937
    },
    "D4": {
      "rc": 1,
      "modules": 1254
    },
    "D5": {
      "rc": 0,
      "modules": 1845
    }
  }
}
```

## 逐模块处置

### `multiprocess_active_input`

- 职责（原模块声明）：LLM: 前台适配器只消费原invocation/source/Agent/claim；v3运输仅定位，不造执行权；probe纯观察、drain原T/P3短门追加，确认仍归原reserve→submit→ACK。 / 模块用途: 真实C/W安全点接收原文字；同一deadline贯穿每ref和原guidance/queue末写，过期不假idle或普通failed。
- 定义：`pending_turn_input_source`, `OriginalPendingTurnInput`, `finish_pending_turn_inputs`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/request_execution.py`; `agent_py_agent/tests/test_gateway_active_input_budget_fences.py`; `agent_py_agent/tests/test_gateway_active_input_closeout_pipeline.py`; `agent_py_agent/tests/test_gateway_active_input_identity.py`; `agent_py_agent/tests/test_gateway_active_input_publication.py`; `agent_py_agent/tests/test_gateway_active_input_resource_retry.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_request_execution_unknown.py`; `agent_py_agent/tests/test_host_summary_phase_lifetime.py`; `agent_py_agent/tests/test_v15_w9_quote_expansion.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=loaded, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_active_input_closeout_pipeline.py`; `agent_py_agent/tests/test_gateway_request_execution_unknown.py`; `agent_py_agent/tests/test_gateway_active_input_identity.py`

### `multiprocess_admission`

- 职责（原模块声明）：LLM: B1b P3 的 C 侧持久准入：单一临界区里先把预留原子写+fsync 持久化，再发布可认领的盖章路由；占用只认未释放预留，释放只凭规范终态。 / 跨代转换只走共同授权门（接手证明 + 屏障结果 + 持锁 C 上下文），pending 运输半态在重盖章前先核 inbox/terminal 同名冲突。 / 模块用途: 提供原准入预留、盖章、规范终态释放和认领校验；新责任遵守原Root关闭门，控制面不充当业务Agent。
- 定义：`reservations_dir`, `reservation_file`, `session_scope_for_request`, `_valid_session_scope`, `_read_coordinator_run_id`, `_write_record_durable`, `_ensure_durable_parent_chain`, `_write_bytes_durable`, `_write_all_bytes`, `_write_once`, `_fsync_directory`, `_replace_with_retry`, `_unlink_tmp_file`, `_read_reservation`, `_session_key`, `_session_lookup_keys`, `_legacy_scope_matches`, `_legacy_session_parts`, `_ownership_projection`, `_route_without_generation`, `CoordinatorAdmissionScope`, `PersistentAdmission`, `_expire_pending_steer_locked`, `_runtime_decision_scope`, `_runtime_commit_scope`, `_ordinary_admission_commit_scope`, `_consume_processing_recovery`, `_runtime_context_rejection`, `_remember_runtime_commit`, `_commit_created_record`, `_commit_ordinary_created_record`, `_persist_created_record`, `_commit_reused_record`, `_persist_reused_record`, `_scan_reservation`, `_read_scan_reservation`, `_write_gate_record_durable`, `_count_reservation`, `_count_reservation_record`, `_limit_rejection`, `_reservation_record`, `_blocked`, `_takeover_proof_rejection`, `_takeover_authority_rejection`, `_authority_proof`, `_authority_barrier`, `_authority_run`, `_runtime_authority_rejection`, `_runtime_owner_rejection`, `_takeover_scope_rejection`, `_proof_covers_worker`, `_takeover_barrier_rejection`, `_release_settled_if_terminal`, `release_reservation_if_terminal`, `_release_reservation_locked`, `_finish_runtime_released`, `_runtime_remembers_reservation`, `release_settled_reservations`, `_terminal_payload`, `verify_claimable_reservation`, `coordinator_admission_gate`, `_processing_recovery_decision_locked`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_curator_accounting.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_input_reconcile.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_authority.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_operation_reclaim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_terminal.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_tree.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_unroutable_closeout.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`; `agent_py_agent/agent/gateway_parts/queued_steer_expiry.py`; `agent_py_agent/agent/gateway_parts/request_binding.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/cli/gateway_coordinator_request_pump.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/support/multiprocess_dispatch_cases.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_approval_retirement_worker_future.py`; `agent_py_agent/tests/test_approval_waiter_retirement.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_background_execution_ports.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_curator_uncertain_outcome.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_dual_owner_concurrent_canary.py`; `agent_py_agent/tests/test_gateway_active_input_budget_fences.py`; `agent_py_agent/tests/test_gateway_active_input_closeout_pipeline.py`; `agent_py_agent/tests/test_gateway_active_input_publication.py`; `agent_py_agent/tests/test_gateway_active_input_resource_retry.py`; `agent_py_agent/tests/test_gateway_active_input_retry_contract.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_admission_closing.py`; `agent_py_agent/tests/test_gateway_background_active_input.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_background_fresh_queue.py`; `agent_py_agent/tests/test_gateway_background_lifecycle.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`; `agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py`; `agent_py_agent/tests/test_gateway_c_startup_recovery.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_cold_processing_recovery.py`; `agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_gateway_durable_dispatch_scope.py`; `agent_py_agent/tests/test_gateway_heartbeat_request_unknown.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_gateway_prepared_owner_identity.py`; `agent_py_agent/tests/test_gateway_prepared_route_immutability.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_queue_transport_recovery.py`; `agent_py_agent/tests/test_gateway_request_execution_unknown.py`; `agent_py_agent/tests/test_guidance_nonblocking_append.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_local_ask_owner_transport.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_guard.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`; `agent_py_agent/tests/test_owner_maintenance_execution_ports.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_runner_thread_start_boundary.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_skill_learning_execution_ports.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`; `agent_py_agent/tests/test_v13_w4_conversation_steer_stop.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`

### `multiprocess_admission_fs`

- 职责（原模块声明）：LLM: P3 文件系统观察辅助；缓存和耐久确认只属于一个 gate 的固定目录，不保存业务状态或共享跨 owner 结论。 / 改动必须联测 review26 的 ctime 碰撞、首次/重试链同步、稳态成本；记录原子提交仍由 admission 模块负责。 / 模块用途: 给 released 扫描提供文件身份与 racy 判据，并按实例确认整条预留目录链。
- 定义：`reservation_signature`, `released_cache_is_stable`, `_ctime_precision_ns`, `confirm_reservation_chain`, `_directory_identity`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/support/multiprocess_dispatch_cases.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`

### `multiprocess_agent_contract`

- 职责（原模块声明）：LLM: P4/P5 的真实构造与已构造 Home 检查点；只核原冻结事实，不建 Agent、池或第二身份源。 / 模块用途: 防止上游 launch 校验与实际构造参数、返回对象的归属脱节；生产接线另做。
- 定义：`AgentConstruction`, `require_agent_constructor`, `_require_worker_owner`, `require_agent_home`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/agent_core/runner/worker.py`; `agent_py_agent/agent/gateway_parts/managed_owner_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_agent.py`; `agent_py_agent/agent/owner_scoped_pool.py`; `agent_py_agent/agent/user_space/worker_home.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/_owner_services_preimage.py`; `agent_py_agent/tests/ft/test_ft_context_propagation.py`; `agent_py_agent/tests/ft/test_ft_shared_state.py`; `agent_py_agent/tests/golden/test_golden_normalize.py`; `agent_py_agent/tests/run_tests.py`

### `multiprocess_background_discovery`

- 职责（原模块声明）：LLM: 策展按原owner实际配置/本地时钟发现，在_submit重采与原commit门内末核；失格不新造队列或消费状态。 / LLM: 原C同backgroundThread只读分页发现文件源和RuntimeDB到期行；明确ro且无Agent，T/C/lease/Root末门仍入唯一P3/P5。 / 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。 / 模块用途: 将原会话、调度、学习、维护与watch来源接到生产请求泵，C撤权时停止新提交，原来源保留。
- 定义：`coordinator_background_required`, `_OwnerBackgroundBlocked`, `_owner_only_blocked`, `_CuratorNotDue`, `_curator_submission_config`, `CoordinatorBackgroundRequests`, `_source_page`, `_directory_source_items`, `target_owner`, `_scheduler_has_due_work`, `_policy_has_due_work`, `_maintenance_has_due_work`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_background_execution.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_curator_dispatch_eligibility.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`; `agent_py_agent/tests/test_gateway_coordinator_loops.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_owner_maintenance.py`; `agent_py_agent/tests/test_plan_w2_recovery_budget.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_recovery.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_skill_learning.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_owner_maintenance.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`; `agent_py_agent/tests/test_curator_dispatch_eligibility.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_recovery.py`

### `multiprocess_background_execution`

- 职责（原模块声明）：LLM: 原BG claim定位投影须在同一短T内发布；只组合原P3/P5，窗口只认原lifetime当前同对象token，同步事务/claim/撤权测试。 / 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。 / 模块用途: 在原请求执行域内调用后台业务或精确DB字条消费；期限事务无模型，运输身份仍接原短门。
- 定义：`BackgroundScopeLifetime`, `BackgroundTransaction`, `BackgroundRequestExecution`, `background_request_scope`, `background_transition`, `current_background_request`, `background_child_scope`, `background_short_operation`, `background_short_scope`, `require_background_transaction`, `_original_background_lifetime_active`, `_holds_original_background_window`, `_uncertain_execution`, `require_background_model_admission`, `BackgroundRuntimeBinding`, `BackgroundRequestTransition`, `bind_background_run_params`, `execute_background_request`, `_execute_learning`, `_execute_maintenance`, `_execute_wake`, `requeue_pending_background_request`, `_execute_process_completion`, `_execute_runtime_wake`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/agent_core/orchestration/dispatch/orphan_request.py`; `agent_py_agent/agent/conversation/background_execution.py`; `agent_py_agent/agent/conversation/claim_execution_binding.py`; `agent_py_agent/agent/conversation/host_notification_scope.py`; `agent_py_agent/agent/conversation/im_delegation_delivery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_child_execution_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_subagent_execution.py`; `agent_py_agent/agent/gateway_parts/private_approval_control.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/request_execution.py`; `agent_py_agent/agent/gateway_parts/request_transactions.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/agent/ingestion/harvest_execution.py`; `agent_py_agent/agent/memory_store/curator_backend.py`; `agent_py_agent/tests/test_b1r3_retention_config_wiring.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_background_execution_ports.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_curator_uncertain_outcome.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_host_notification_transport.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_owner_maintenance.py`; `agent_py_agent/tests/test_owner_maintenance_execution_ports.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_owner_maintenance.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_b1r3_retention_config_wiring.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`

### `multiprocess_background_input`

- 职责（原模块声明）：LLM: 仅实际BG binder/current claim安装本栈opaque来源；v2投影只定位，所有追加和最终claim结算仍过原Gateway T/P3/P5，Count0不查询来源。 / 模块用途: 为主后台会话提供纯文字安全点与仍活claim的最终收件结算，不关闭整个Gateway或新建恢复队列。
- 定义：`bind_background_input`, `BackgroundPendingInput`, `finish_background_claim_inputs`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/conversation/background_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_execution.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_b1r3_retention_config_wiring.py`; `agent_py_agent/tests/test_background_claim_attempt_observer.py`; `agent_py_agent/tests/test_background_claim_execution.py`; `agent_py_agent/tests/test_background_claim_interrupt_scope.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`

### `multiprocess_background_request`

- 职责（原模块声明）：LLM: curator冻结可接C显式时间配置作资格筛选，ref/schema/读取默认语义不变，配置不成为运输授权。 / LLM: 后台运输原ref/摘要与同步末门；长读在门外，原输入T先取，生产C/lease/Root只包短写；P3/P5唯一，callback不成新许可。 / 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。 / 模块用途: 只读冻结原文件或RuntimeDB行来源，按原输入幂等合同提交后台请求；不复制正文作模型prompt。
- 定义：`BackgroundSourceChanged`, `source_digest`, `source_owner`, `validate_source_ref`, `_validate_learning_ref`, `_validate_wake_ref`, `read_source`, `_observation_ids`, `_validate_source_payload`, `_validate_learning_payload`, `freeze_source`, `curator_input_revision`, `enqueue_source`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_curator_accounting.py`; `agent_py_agent/agent/gateway_parts/multiprocess_subagent_execution.py`; `agent_py_agent/agent/gateway_parts/process_completion_source.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/agent/gateway_parts/runtime_wake_source.py`; `agent_py_agent/agent/ingestion/harvest_execution.py`; `agent_py_agent/agent/ingestion/harvest_parent.py`; `agent_py_agent/tests/test_curator_dispatch_eligibility.py`; `agent_py_agent/tests/test_gateway_background_active_input.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_background_fresh_queue.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_owner_maintenance.py`; `agent_py_agent/tests/test_param_w2_gateway_and_pool.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_recovery.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_skill_learning_execution_ports.py`; `agent_py_agent/tests/test_v14_r4_multiprocess_observation_acks.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_v14_r4_multiprocess_observation_acks.py`; `agent_py_agent/tests/test_owner_maintenance.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_gateway_background_fresh_queue.py`

### `multiprocess_background_sources`

- 职责（原模块声明）：LLM: 只读原会话观察账及其唯一handled回执；不创建source账，不按summary/模型正文派工，坏源不当空成功。 / 原learning FIFO与唯一registry维护共用原来源/短写门；mtime仅CAS，维护不消费新FIFO/调用模型或确认候选。 / 模块用途: 将原无wake观察来源解析为有限的metadata事实，供原Gateway运输桥复读。
- 定义：`observation_handled`, `_observation_journal_identity`, `observation_payload`, `maintenance_policy`, `maintenance_due`, `learning_registry_snapshot`, `source_recovery_block`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_request.py`; `agent_py_agent/agent/owner_wake_discovery.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`; `agent_py_agent/tests/test_v14_r4_multiprocess_observation_acks.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_v14_r4_multiprocess_observation_acks.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`

### `multiprocess_bootstrap`

- 职责（原模块声明）：LLM: P4 只冻结已有 WorkerLaunch，不建立第二份进程模式；接线由启动组合根后续完成。 / 模块用途: 在任何 Agent/owner 初始化前核对受信启动合同，不做文件初始化。
- 定义：`WorkerBootstrapError`, `WorkerBootstrap`, `freeze_worker_bootstrap`, `require_worker_bootstrap`, `validate_worker_launch`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_agent_contract.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_publish.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_ready.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_resources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_agent.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_worker_bootstrap_scope.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_worker_workspace_transport.py`; `agent_py_agent/tests/test_iface_w5_subagent_worker_type.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_private_approval_worker_trylocks.py`; `agent_py_agent/tests/test_public_agent_home_initialization_order.py`; `agent_py_agent/tests/test_res_w11_owner_home_rebuild.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_worker_terminal_heartbeat.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`

### `multiprocess_bootstrap_publish`

- 职责（原模块声明）：LLM: P4沿原registry唯一当前登记；真实C原lease内可归档immutable generation三字段历史metadata，只定位count、不授alive/P2/ready；None原格式路径零extraIO。 / 模块用途: 用原账本规范编码和原子原语发布当前代次、实际端口及实例，完整旧记录防止迟到覆盖。
- 定义：`WorkerRegistryError`, `read_worker_registry`, `publish_worker_registry`, `_publish_locked`, `_registry`, `_require_root`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/supervisor_generation.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_s2w6_single_process_migration.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`

### `multiprocess_bootstrap_ready`

- 职责（原模块声明）：LLM: P4 合同消费原 P0 记录与 B1a 健康门；不派发、不启动进程、不把 ready 当接手授权。 / 模块用途: 构造并核对当前 worker 实例的心跳，拒绝迟到实例和旧代。
- 定义：`WorkerReadyError`, `require_current_worker`, `current_worker_identity`, `registered_worker_heartbeat`, `_record`, `_current_generation`, `_healthy_readback`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/agent/gateway_parts/supervisor_generation.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_private_approval_worker_trylocks.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`

### `multiprocess_bootstrap_resources`

- 职责（原模块声明）：LLM: P4 共享初始化只读合同；原 no-follow/dir_fd 能力缺失必须拒绝，不降级为普通路径打开。 / 模块用途: 在冻结根内检查声明资源；不调用 HomePaths 初始化、种子、锁创建或 SQLite 连接。
- 定义：`WorkerSharedResourceError`, `SharedResource`, `require_shared_resources`, `_validate_resources`, `_validate_resource`, `_check_resource`, `_check_beneath`, `_check_descriptor`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/logging.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_resources.py`; `agent_py_agent/agent/user_space/worker_home.py`; `agent_py_agent/tests/test_admin_self_gateway_diagnostics.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_home_initializer_permissions.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`; `agent_py_agent/tests/test_public_agent_home_initialization_order.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_home_initializer_permissions.py`; `agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_admin_self_gateway_diagnostics.py`; `agent_py_agent/tests/test_public_agent_home_initialization_order.py`

### `multiprocess_bootstrap_schema`

- 职责（原模块声明）：LLM: 原 records 唯一入口已核字段全集；此处复用其校验函数，不重新定义字段、身份或默认值。 / 模块用途: 核对 P4 原 registry 与实例心跳的语义，不读取 OS、不发布状态或授予 ready。
- 定义：`validate_bootstrap_record`, `_process_identity`, `_registry`, `_registry_entry`, `_heartbeat`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_records.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=loaded, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_gateway_closed_generation_progress.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_semantics.py`

### `multiprocess_child_execution_binding`

- 职责（原模块声明）：LLM: 只捕获原dispatch返回对象和本次独立child请求；原T/P3/P5/RuntimeDB仍唯一授权，纯内存receipt不建账或补权，同步child/native源测试。 / 模块用途: 把实际子代理与原认领的协调者来源绑定，让同一child工具登记保留原C、claim和canonical谱系。 / LLM: 默认构造没有来源；只有原builder在真实T内捕获后挂到准确worker，origin/self/PID/同worker指针拒copy/ctor/fork。
- 定义：`ChildDispatchReceipt`, `ChildExecutionBinding`, `_child_identity`, `_require_receipt_transport`, `capture_original_child_dispatch`, `capture_child_execution_binding`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/agent_core/runner/worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_subagent_execution.py`; `agent_py_agent/tests/test_mcp_lifecycle.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_mcp_lifecycle.py`

### `multiprocess_commit_channel`

- 职责（原模块声明）：LLM: 在C内将实际Unix连接绑定原issuer；不是完成的native协议、耐久租约或W业务许可。 / 模块用途: 只让同一原连接登记与结清它的精确原提交责任。
- 定义：`PeerCommitChannel`, `_kernel_stream_hangup`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_commit_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_commit_session`

- 职责（原模块声明）：LLM: 仅包装原PeerCommitChannel传输；线handle仅引用原pin，不构成许可或第二耐久账。 / 模块用途: 在C内把真实W连接的begin/finish转到原准入与精确责任门。
- 定义：`CommitServerSession`, `_request_identity`, `_wire_owner`, `_read_exact`, `read_commit_frame`, `write_commit_frame`, `_unique_object`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_commit_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`

### `multiprocess_control_forward`

- 职责（原模块声明）：LLM: 转发共用原控制回执；cold/ready HTTP在完整raw CAS及原owner门内先物化规范owner再发布prepared+dispatch；Count0/legacy及v5私卡保持。 / 模块用途: 为远端控制保存一次固定分区和代次，重试/状态只读原操作。
- 定义：`read_control_operation_raw`, `prepare_coordinator_remote_control`, `_control_dispatch_for_receipt`, `control_dispatch_binding`, `validate_control_dispatch`, `_validate_control_executor`, `prepare_remote_control`, `_coordinator_context_paths`, `read_remote_control_status`, `_seal_dead_control`, `_orphan_control_sealable`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_operation_service.py`; `agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`; `agent_py_agent/agent/gateway_parts/multiprocess_host_command_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_local_control.py`; `agent_py_agent/agent/gateway_parts/private_approval_control.py`; `agent_py_agent/tests/test_control_producer_receipts.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_ready_control_receipt_source.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_ready_control_receipt_source.py`; `agent_py_agent/tests/test_control_producer_receipts.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`

### `multiprocess_control_identity`

- 职责（原模块声明）：LLM: 仅同进程域及精确出生核对；kill(0)只查询存在，不发送信号，不可见/拒绝保持unknown。 / 模块用途: 对原控制认领的执行者做三态存活查询，不把心跳超时当死亡。
- 定义：`control_executor_live`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_mp02_switchover_self_heal.py`

### `multiprocess_control_poller`

- 职责（原模块声明）：LLM: 控制轮询独立于前台/后台队列和心跳；组合根须先保存lane再start，ACK异常仍持原Thread收尾，不冒充控制业务完成。 / 模块用途: 为worker提供半秒控制轮询通道，启动前保留句柄，停止时真实join同一线程。 / LLM: 只轮询已经冻结的控制泵，不改路由/容量/owner；泵内部短锁防重复认领。 / 类用途: 即使前台队列慢也及时读取同分区控制。
- 定义：`ControlPollingLane`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`

### `multiprocess_control_reconcile`

- 职责（原模块声明）：LLM: 原W对账仅读原guidance规范终态，复用原transition锁且可产生锁侧文件；不修改正文/投影，不认领、不续租，不创建owner或第二账本。 / 模块用途: 在原锁内观察插话已确认的消费或拒绝事实，不能把未知写成已交付。
- 定义：`read_original_steer_delivery`, `_read_guidance_observation`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_worker_steer_observation.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_steer_observation.py`

### `multiprocess_control_target`

- 职责（原模块声明）：LLM: C只读认证owner原canonical Store；冷路径显式config/home和资源约束，不造Agent替身；默认ready读取与目标算法保持。 / 模块用途: 异步stop/btw首次入队固定真实执行目标，无法固定时明确拒绝。
- 定义：`freeze_control_execution`, `_read_authenticated_execution_from_config`, `_read_authenticated_execution`, `refuse_unfreezable_control`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_control_producer_receipts.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`

### `multiprocess_control_worker`

- 职责（原模块声明）：LLM: 原控制回执是唯一状态机；W登记/冻结分区是权威，三池保原出生refs及submit故障；主线pin固定实际Agent而非owner，短锁认领后锁外运行，同步stop独立槽及回收合同。 / 模块用途: 消费所属分区原控制和规范私卡点击（不恢复执行源），不占前台模型槽，不重放已跨副作用边界的命令。
- 定义：`control_worker_authority`, `WorkerControlPump`, `_try_private_control_write`, `_reconcile_private_control`, `_reap_private_control`, `_transition_private_control`, `_write_control_claim`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_mp01_worker_poll_cpu.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_private_approval_worker_trylocks.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`

### `multiprocess_coordinator`

- 职责（原模块声明）：LLM: 仅内部多进程 C 持有生命周期锁，默认单进程无任何新增 I/O；锁不继承给 worker/接班进程。 / 模块用途: 在恢复和派发前争抢协调者单例，撤权/关FD后才等仅只读的后台收尾。
- 定义：`coordinator_executor_required`, `CoordinatorLease`, `CoordinatorUnavailable`, `coordinator_guard`, `_close_coordinator_lease`, `_withdraw_coordinator_lease`, `_acquire_coordinator_lock`, `current_coordinator`, `bind_coordinator_generation`, `coordinator_for_root`, `coordinator_role_present`, `_CoordinatorCommitPin`, `_is_registered_coordinator_lease`, `pin_coordinator_commit`, `retire_coordinator_commit`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_plane.py`; `agent_py_agent/agent/gateway_parts/control_service.py`; `agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`; `agent_py_agent/agent/gateway_parts/coordinator_owner_management.py`; `agent_py_agent/agent/gateway_parts/http_handlers.py`; `agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_curator_accounting.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_host_command_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_http_transport.py`; `agent_py_agent/agent/gateway_parts/multiprocess_input_reconcile.py`; `agent_py_agent/agent/gateway_parts/multiprocess_local_control.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/private_interaction_transaction.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/standalone_migration.py`; `agent_py_agent/agent/gateway_parts/standalone_migration_errors.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/cli/gateway_restart_handover.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_control_producer_memory_ingress.py`; `agent_py_agent/tests/test_control_producer_receipts.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_c_http_birth.py`; `agent_py_agent/tests/test_gateway_c_http_wait.py`; `agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py`; `agent_py_agent/tests/test_gateway_c_state_publication.py`; `agent_py_agent/tests/test_gateway_client_history_healthy20.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_control_plane_construction.py`; `agent_py_agent/tests/test_gateway_control_plane_frozen_home.py`; `agent_py_agent/tests/test_gateway_coordinated_entry.py`; `agent_py_agent/tests/test_gateway_coordinated_lifecycle.py`; `agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_gateway_coordinator_client_memory.py`; `agent_py_agent/tests/test_gateway_http_prepare_boundary.py`; `agent_py_agent/tests/test_gateway_input_owner_boundary.py`; `agent_py_agent/tests/test_gateway_multiprocess_commit_pins.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_http_transport.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_state.py`; `agent_py_agent/tests/test_gateway_prepared_route_immutability.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_queue_transport_recovery.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_standalone_lifecycle.py`; `agent_py_agent/tests/test_gateway_standalone_migration.py`; `agent_py_agent/tests/test_gateway_standalone_status_digest.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_worker_workspace_transport.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_mp01_worker_poll_cpu.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_guard.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`; `agent_py_agent/tests/test_ready_control_receipt_source.py`; `agent_py_agent/tests/test_s1w1_native_stream_pool.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_v13_r2_admin_coordinator.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`; `agent_py_agent/tests/test_v13_w5_standalone_inventory.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`

### `multiprocess_coordinator_claim`

- 职责（原模块声明）：LLM: C认领仍沿原T锁和原claim；只复读原请求/预留，生命周期门覆盖rename/fence，不新建权威记录。 / 模块用途: 阻止C拿盖章前的内存返回值认领已经漂移的请求。
- 定义：`CoordinatorClaimSnapshot`, `freeze_coordinator_claim`, `coordinator_claim_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/queue_service.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_background_active_input.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_background_fresh_queue.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_request_execution_unknown.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`

### `multiprocess_coordinator_execution`

- 职责（原模块声明）：LLM: 只传递原C准入上下文和原claim；本地工具登记沿同一原T/P3/P5，不建请求/租约，不借W身份，改动同步C执行/P1合同。 / 模块用途: C认领后的owner/模型/工具检查沿同一冻结代次和原预留，安装本地主体工具登记，默认单进程无磁盘读。
- 定义：`CoordinatorExecution`, `coordinator_execution_scope`, `current_coordinator_execution`, `check_coordinator_transition`, `require_coordinator_execution_agent`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_active_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_child_execution_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_http_transport.py`; `agent_py_agent/agent/gateway_parts/multiprocess_subagent_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_tool_global_write.py`; `agent_py_agent/agent/gateway_parts/request_binding.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/request_errors.py`; `agent_py_agent/agent/gateway_parts/request_transactions.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_background_execution_ports.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_admission_closing.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_background_lifecycle.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_heartbeat_request_unknown.py`; `agent_py_agent/tests/test_gateway_learning_discovery.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_request_execution_unknown.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_runner_thread_start_boundary.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_tool_coordinator_execution_ports.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_tool_coordinator_execution_ports.py`; `agent_py_agent/tests/test_gateway_heartbeat_request_unknown.py`

### `multiprocess_coordinator_executor_scope`

- 职责（原模块声明）：LLM: 只有原CoordinatorExecution安装本地主体工具登记，零模式不创建source/账本/域；C-host只用原C native强source，拒legacy丢源delegate；同步C/P1/host合同。 / 模块用途: 将本地已认领请求的工具调用送入原 P1 两阶段启动，Linux 命令使用同一原 C 创建的内核域。
- 定义：`_CoordinatorExecutorLauncher`, `coordinator_request_executor_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_background_execution_ports.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`

### `multiprocess_coordinator_executor_source`

- 职责（原模块声明）：LLM: 原 CoordinatorExecution/T/P3/P5 是本地主体工具的唯一权限来源；P1 仍落原按 owner 哈希分区的账本，不能借 W 身份或从 JSON 重建 C 权。 / 模块用途: 把本地 main 的真实 C 认领接到执行者预留、短物理动作和原租约责任，撤权后只允许读回和精确结清。
- 定义：`CoordinatorExecutorSource`, `_require_source_agent`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_authority.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`

### `multiprocess_curator_accounting`

- 职责（原模块声明）：LLM: 原quota是唯一会计，run去重/P3占用保持；裸RMW叶限原同期PID/thread单次消费，callback同步进同Root关闭门，不授权或建reserve账。 / LLM: ControlPlane只显式投影其canonical home_root对应的原typed路径，不动态补Agent字段；原quota/P3短门与全局会计权威不变。 / 模块用途: 为C提供跨worker策展额度与幂等核算，cold准入沿同一根读取；Count0保留v2身份，模式切换不能重扣。
- 定义：`configured_curator_daily_quota`, `CuratorQuotaUnavailable`, `quota_date`, `_read_quota`, `read_quota_count`, `owner_consumed_records`, `_owner_records_for_date`, `_inventory_version`, `_all_consumed_records`, `_reconcile_unlocked`, `reconcile_quota`, `_prepare_quota_step`, `_commit_quota_candidate`, `record_legacy_consumption`, `unsettled_curator_requests`, `_occupied_request`, `curator_quota_admission`, `_quota_host_paths`, `_write_quota_durable`, `_sync_quota`, `prepare_quota_admission`, `_terminal_curator_run`, `prepare_quota_release`, `account_completed_terminal`, `_history_path`, `_read_history`, `_archive_quota_day`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/tests/test_gateway_admission_closing.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_param_w2_gateway_and_pool.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_admission_closing.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_param_w2_gateway_and_pool.py`

### `multiprocess_dispatch`

- 职责（原模块声明）：LLM: P5 只提供认领前的无写入合同；队列移动、执行和终态仍由原链负责。 / 模块用途: 在 worker 消费请求前统一核验冻结分区、盖章和原准入预留。
- 定义：`WorkerDispatchError`, `require_claimable`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_queue.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop.py`; `agent_py_agent/agent/gateway_parts/multiprocess_tool_global_write.py`; `agent_py_agent/agent/gateway_parts/request_binding.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_owner_maintenance_execution_ports.py`; `agent_py_agent/tests/test_rework_r4_script_delivery.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_rework_r4_script_delivery.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_owner_maintenance_execution_ports.py`

### `multiprocess_dispatch_binding`

- 职责（原模块声明）：LLM: P5 执行检查只运输原 ClaimSnapshot/GatewayClaim；不替代原 lease、执行、历史和规范终态链。 / 模块用途: 为 owner/模型/工具/取消提供同一冻结绑定门，不读最新配置或创建 Agent。
- 定义：`ExecutionBinding`, `bind_claimed_request`, `require_execution_binding`, `require_cancellation`, `_require_claim_fence`, `_require_claim_identity`, `_require_complete_claim`, `require_execution_agent`, `require_terminal_execution_agent`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop.py`; `agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`

### `multiprocess_dispatch_claim`

- 职责（原模块声明）：LLM: P5 锁内比较只消费原队列与准入事实，不移动请求；调用方须持原 T 锁至原 claim 写完。 / 模块用途: 保存临时不可变扫描快照，并在认领移动之前只读复核。
- 定义：`ClaimCandidate`, `ClaimSnapshot`, `freeze_claim_snapshot`, `recheck_claim_locked`, `_read_locked_request`, `require_unchanged_claim`, `_pending_paths`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_queue.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`; `agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_drain_darwin_waitid`

- 职责（原模块声明）：LLM: 仅原C直接child的P6 waitid公开LP64 ABI；同原WNOWAIT zombie完整sysctl核出生/parent后才能消费actualreap，不读Popen缓存、不证明外native整树；同步ABI/CLI drain tests。 / 模块用途: 用标准库调用macOS原内核waitid，给已有直接worker观察保留真实退出状态。
- 定义：`_DarwinSigInfo`, `DarwinWaitInfo`, `darwin_waitid_available`, `_waitid_query`, `darwin_waitid`, `darwin_zombie_birth`, `_darwin_reap_child`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_process.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_mp03_exited_partition_visibility.py`; `agent_py_agent/tests/test_supervisor_generation_recovery.py`

### `multiprocess_drain_process`

- 职责（原模块声明）：LLM: P6只观察原C直接W退出；R前P4出生与原waitid/reap同源，Darwin公开ABI和zombie出生缺失未知，缓存/PID不补历史、不证明外native整树；同步ABI/CLI tests。 / 模块用途: 给协调者保留直接worker的真实内核waitstatus，避免把returncode缓存当退出证据。
- 定义：`OriginalWorkerExitObservation`, `_waitstatus_supported`, `_original_waitid`, `_waitstatus_birth`, `_exited_code`, `_waitstatus_matches`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_start.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_mp03_exited_partition_visibility.py`; `agent_py_agent/tests/test_supervisor_generation_recovery.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_mp03_exited_partition_visibility.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_supervisor_generation_recovery.py`

### `multiprocess_drain_resources`

- 职责（原模块声明）：LLM: P6仅保留本次trusted组合根已经创建的原Thread/Executor；不按线程名称、全局枚举或future.done推退出，不建权限/持久状态，同步实际held资源及Count0测试。 / 模块用途: 保留实际线程池与线程，停止出生后用原对象判断物理收尾是否完成。
- 定义：`executor_threads`, `join_original_threads`, `DrainResource`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_http_transport.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_approval_retirement_worker_future.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`

### `multiprocess_drain_runtime`

- 职责（原模块声明）：LLM: P6生产组合根只保原C/control/lease和出生资源，停止准入→门外join→原源稳定双读→六类评估；count0不构造，不造权限/账，原native整树由独立回执消费，同步held/复制/旧代/生命周期测试。 / retain_admission只运输原gate/tuple引用；ordinary新责任末发表与本Root关闭共门，旧责任不靠停止洗掉。 / 模块用途: 汇集实际资源及原责任，停止新准入后核完整排空才封存v2；未结束/缺证据或提交失败仍保阻塞。
- 定义：`GatewayDrainRuntime`, `_role_assessment`, `_local_call_items`, `_require_generation_roles`, `_original_abnormal_worker_exit`, `_generation_tree_evidence`, `_generation_digests`, `_generation_final_roles`, `_observe_generation_trees`, `_original_target_stop_scope`, `_require_generation_observation`, `_prepare_original_generation_closure`, `_require_drain_role_sources`, `_require_inherited_target`, `_require_generation_closing_roles`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_http_transport.py`; `agent_py_agent/agent/gateway_parts/multiprocess_input_reconcile.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_drain.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_c_http_birth.py`; `agent_py_agent/tests/test_gateway_c_http_wait.py`; `agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py`; `agent_py_agent/tests/test_gateway_c_startup_recovery.py`; `agent_py_agent/tests/test_gateway_c_state_publication.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_client_history_healthy20.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_coordinated_lifecycle.py`; `agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_gateway_coordinator_client_memory.py`; `agent_py_agent/tests/test_gateway_coordinator_loops.py`; `agent_py_agent/tests/test_gateway_durable_dispatch_scope.py`; `agent_py_agent/tests/test_gateway_http_current_work.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_http_transport.py`; `agent_py_agent/tests/test_gateway_partial_startup_drain.py`; `agent_py_agent/tests/test_gateway_prepared_owner_identity.py`; `agent_py_agent/tests/test_gateway_prepared_route_immutability.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_s1w1_native_stream_pool.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_v13_r2_admin_coordinator.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_http_current_work.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`

### `multiprocess_drain_sources`

- 职责（原模块声明）：LLM: 默认P6完整目录/raw不变；generation仅完整末核证实的typed名称集冲突可有限重读；首读None须确认名称已离开，叶异常/旧expected仍硬拒。 / 模块用途: 读取停止持久责任，并为generation区分原辅助流/暂存；两者均不授C/W、物理或封存权。
- 定义：`original_directory_snapshot`, `_generation_namespace`, `_generation_canonical_name`, `_generation_chunk_name`, `_generation_source_entry_kind`, `_generation_facility_identity`, `_generation_source_names`, `_generation_directory_chain`, `_PublishedNamesChanged`, `_remember_generation_facilities`, `_published_source_rows`, `_published_directory_snapshot`, `_descriptor_names`, `_descriptor_child_bytes`, `durable_drain_snapshot`, `durable_items`, `_closed_control_source`, `_scope_route`, `same_durable_snapshot`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_drain.py`; `agent_py_agent/agent/gateway_parts/standalone_inventory.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_generation_facility_second_open.py`; `agent_py_agent/tests/test_gateway_generation_facility_successful_replace.py`; `agent_py_agent/tests/test_gateway_generation_source_namespace.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_generation_source_missing_publication.py`; `agent_py_agent/tests/test_generation_source_stable_names.py`; `agent_py_agent/tests/test_rework_r9_gateway_drain_auxiliary.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_generation_source_stable_names.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_generation_facility_successful_replace.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_gateway_generation_source_namespace.py`

### `multiprocess_drain_trees`

- 职责（原模块声明）：LLM: P6只消费原C首绑定source/actualDomain/HostLane与独立完整P1，Linux用户全域沿原pidfd/ns屏障；无原历史/旧v1/旧代或Mac无匹配P0方法保持unknown，不从Popen缓存/JSON重建，同步全树与遗漏/换壳测试。 / 模块用途: 汇集原内核排空证据；启动缺会话保留未知，绝不以空来源替代原P2证明。
- 定义：`_original_objects`, `_record_stopped`, `_tree_observation`, `original_tree_observations`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/tests/test_gateway_partial_startup_drain.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_partial_startup_drain.py`

### `multiprocess_execution`

- 职责（原模块声明）：LLM: 只在进程内运输原P5绑定和实际Agent；不新增请求、租约、身份登记或恢复权限。 / 模块用途: 让原模型/工具的T锁检查点保留worker执行归属，跨线程回调也不能改绑。
- 定义：`WorkerExecution`, `worker_execution_scope`, `current_worker_execution`, `check_worker_transition`, `check_worker_terminal`, `execute_worker_transition`, `execute_worker_claim`, `_execute_managed_claim`, `_frozen_legacy_wake`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_active_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_subagent_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`; `agent_py_agent/agent/gateway_parts/private_approval_control.py`; `agent_py_agent/agent/gateway_parts/request_binding.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/request_errors.py`; `agent_py_agent/agent/gateway_parts/request_transactions.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/agent/ingestion/harvest_execution.py`; `agent_py_agent/agent/memory_store/curator_backend.py`; `agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_approval_retirement_worker_future.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_dual_owner_concurrent_canary.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_gateway_heartbeat_request_unknown.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_request_execution_unknown.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_owner_maintenance_execution_ports.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_gateway_heartbeat_request_unknown.py`

### `multiprocess_execution_lifetime`

- 职责（原模块声明）：LLM: 纯内存同源scope限制，原current_execution仍唯一包装；token不授P3/P5许可、不做进程/磁盘I/O，不用threadID阻止原模型设施线程。 / 模块用途: 原C/W作用域共享同一活性对象，退出/fork/复制对象后不能借未结processing重新执行。 / LLM: origin/self和exact execution只用于拒绝重绑/复制；实际claim、source、lease、peer仍由原C/W门裁决，不能序列化成持久权威。
- 定义：`ExecutionScopeLifetime`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_local_control.py`; `agent_py_agent/agent/gateway_parts/owner_management_control.py`; `agent_py_agent/agent/gateway_parts/private_interaction_transaction.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/request_errors.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_approval_retirement_worker_future.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_background_execution_ports.py`

### `multiprocess_executor_active_index`

- 职责（原模块声明）：
- 定义：`index_for`, `current_index`, `_directory`, `_names`, `ExecutorActiveIndex`, `_main_owner_sha`, `_executor_key`, `_file_stamp`, `_require_holder`, `maintain_controller_index`, `capture_coordinator_descriptor`, `capture_worker_descriptor`, `_retire_candidate`, `_observe_domain`, `_retirement_request`, `_json_digest`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_curator_accounting.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_gateway_source_refusal_diagnostics.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_source_refusal_diagnostics.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`

### `multiprocess_executor_bootstrap`

- 职责（原模块声明）：LLM: 本文件仅由登记设施 -I 隔离加载；固定随包根路径不使用命令 PYTHONPATH 或工作目录。 / 模块用途: 从私有 FD 恢复可信后台宿主归属，不提供任意模块执行器；保持原FD/身份校验和失败退出125，同步核对委托调用方。 / LLM: 固定模块先核对完整登记和自身身份，错误退出125且不打印交接正文。 / 函数用途: 调用内部受信委托入口。
- 定义：`main`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_delegation.py`; `agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_executor_delegation`

- 职责（原模块声明）：LLM: 只把已登记可信 host 的冻结归属经私有 FD 交出；不从用户环境/请求恢复权限，不新建第二份账本。 / 模块用途: 在独立后台宿主内恢复同一登记门，宿主命令仍须另行登记。
- 定义：`bootstrap_argv`, `_check_host_argv`, `launch_delegate`, `_send_handoff`, `_failed_delegate`, `_read_exact`, `_unique_pairs`, `receive_context`, `validate_context`, `run_delegate`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_bootstrap.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_executor_domain_schema`

- 职责（原模块声明）：LLM: 只校验P1 v2原执行域结构，实际namespace/source/Popen及整树退出由C内核适配验证；不默迁移v1或用磁盘标记重建权限，同步records/P1/P6 tests。 / 模块用途: 为原P1增加显式Linux namespace事实格式，保持旧v1字节不变。 / LLM: namespace身份是dev/ino各自严格整数；纯记录不证明引用仍活，调用方必须持原内核引用而非再open数字路径。 / 函数用途: 校验原namespace或C父namespace的固定格式。
- 定义：`_namespace`, `validate_execution_domain`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_records.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_gateway_closed_generation_progress.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_semantics.py`

### `multiprocess_executor_fds`

- 职责（原模块声明）：LLM: 裸 FD 号可被控制管道复用；创建前先核验全部源，再持有独立 CLOEXEC 副本，副本只由本次启动关闭。 / 模块用途: 冻结标准流与明确继承的真实描述符对象，不扩大调用方的 FD 白名单。 / LLM: owned 是父端必须关闭的全部副本；allowed 只含显式继承项的副本到原号映射，不包含控制描述符。
- 定义：`ExecutorDescriptors`, `pin_descriptors`, `_pin_sources`, `_source_descriptor`, `_identity`, `_pin`, `_pin_floor`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_process.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_executor_identity`

- 职责（原模块声明）：LLM: P1a 出生身份只读内核；未知绝不退回 PID，不调用 ps，不改变 daemon_metadata 旧合同。 / 模块用途: 为等待垫片读取可跨 exec 保持的进程身份，未知由 P0 校验拒绝。 / LLM: Linux 为 stat 第22字段 ticks，macOS 为 proc_bsdinfo 的秒:微秒；任何取值失败保持 None。
- 定义：`process_birth`, `linux_stat_birth`, `_ProcBSDInfo`, `_darwin_birth`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/agent_core/runner/session_pool.py`; `agent_py_agent/agent/conversation/process_events.py`; `agent_py_agent/agent/gateway_parts/daemon_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_ready.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_identity.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_command_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_reference.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_darwin.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/supervisor_generation.py`; `agent_py_agent/agent/ingestion/harvest_execution.py`; `agent_py_agent/agent/workflows/concurrency_policy.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_control_producer_receipts.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`; `agent_py_agent/tests/test_gateway_background_process_identity.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_pid_record_identity.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_process_completion_events.py`; `agent_py_agent/tests/test_wake_attempt_store.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_background_process_identity.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`

### `multiprocess_executor_ledger`

- 职责（原模块声明）：LLM: P1a 唯一权威为 workers/executors/w<index>/<sha256(reservation_id)>.json；沿 P0 v1/FSM。 / 构造无 I/O，修改持私有分区锁；文件与目录 fsync，未知/旧代记录不能被覆盖或删掉。 / 模块用途: 保存分区执行者预留，不创建第二个账本或复制命令正文。
- 定义：`ExecutorLedger`, `_binding`, `_durable_namespace`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_publish.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_delegation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_original_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_scope_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_ledger.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`

### `multiprocess_executor_original_source`

- 职责（原模块声明）：LLM: 只持原C的不可替换引用；所有写入/实际动作在原controller短门内再次核原来源，字段和ACK无授权作用。 / 模块用途: 为内部执行scope提供不可变的原C P1提交来源，不提供W/native传输或用户命令权限。
- 定义：`OriginalCExecutorSource`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_scope_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_authority.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`

### `multiprocess_executor_process`

- 职责（原模块声明）：LLM: P1a 只创建可信等待垫片，不拥有账本/请求权限；放行须由 start 的持久读回门调用。 / 模块用途: 封装等待管道、极小垫片启动及自建进程的有界失败回收。
- 定义：`WaitingExecutor`, `spawn_waiting`, `_native_birth`, `_command_context`, `await_waiting`, `_wait_ready`, `_read_ready`, `_close_child_fds`, `prepare_waiting_context`, `release_prepared_waiting`, `release_waiting`, `_write_context`, `_write_frame`, `_descriptor_events`, `_wait_event`, `abort_waiting`, `_reap`, `_close_descriptors`, `_try_cleanup`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_delegation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`

### `multiprocess_executor_runtime`

- 职责（原模块声明）：LLM: 内部组合根显式冻结 C/W 分区和请求绑定；四类工具只拿无权限推断的共用接口，不能解析用户测试标志。 / 模块用途: 将已经完成授权的真实入口接到 P1a 唯一账本与两阶段放行协议，不承担请求调度或 P2 回收裁决。
- 定义：`_reservation`, `_command`, `_RegistrationRoot`, `executor_registration_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_delegation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`

### `multiprocess_executor_scope_source`

- 职责（原模块声明）：LLM: 冻结定位不抹原C门；可变facade不是权限来源，实际方法只能来自不可变原C provider。 / 模块用途: 把scope元数据和原活来源分开，在原C实际短门内复核facade未换来源。
- 定义：`_BoundExecutorSource`, `freeze_executor_source`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_runtime.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`

### `multiprocess_executor_shim`

- 职责（原模块声明）：LLM: P1a 可信垫片以空环境和 -I -S 独立加载；只有父管道 R 及完整上下文帧才应用命令环境并 exec。 / 模块用途: 等待宿主完成登记，不读取 Home、命令文件或凭据，不打印命令正文。 / 上下文帧最多2MiB：覆盖操作系统常见 argv/env 总量，同时避免私有通道无界占内存。
- 定义：`main`, `_descriptor_map`, `_restore_descriptors`, `_close_unneeded`, `_await_release`, `_read_exact`, `_validated_context`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_process.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_executor_start`

- 职责（原模块声明）：LLM: 两阶段启动接口由四类 P1b 入口共用；默认 count=0 必须零新增 I/O。 / 模块用途: 在用户命令执行之前持久预留、登记并读回，红测先冻结接口。
- 定义：`ExecutorIO`, `ExecutorCommand`, `ExecutorStartObserver`, `ExecutorStartResult`, `_StartAttempt`, `start_executor`, `_release_registered`, `_physical_effect`, `_spawn_and_register`, `_registration`, `_failed_start`, `_validate_command`, `_valid_text_policy`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_delegation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_command_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`

### `multiprocess_generation_metadata`

- 职责（原模块声明）：LLM: v1只提供count；v2封存仍需原完整稳定范围，消费区分immutable旧责任与本代自然推进；身份/P1/fresh P2、原P4和Count0不变。 / 模块用途: 沿唯一generation路径保存旧count与显式关闭事实，原raw CAS/读回失败不宣称完成，不给JSON执行权。
- 定义：`generation_metadata_parts`, `read_generation_metadata`, `generation_publication_scope`, `require_generation_source`, `archive_registry_generations`, `_publish_generation_once`, `_publish_generation_path`, `_write_complete_generation`, `generation_source_files`, `generation_role_files`, `seal_generation`, `require_closed_generation`, `_require_closed_source_progress`, `_require_closed_files`, `captured_generation_sources`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_publish.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/standalone_sources.py`; `agent_py_agent/agent/gateway_parts/supervisor_generation.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_closed_generation_progress.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_generation_facility_second_open.py`; `agent_py_agent/tests/test_gateway_generation_facility_successful_replace.py`; `agent_py_agent/tests/test_gateway_generation_source_namespace.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_standalone_sources.py`; `agent_py_agent/tests/test_generation_source_missing_publication.py`; `agent_py_agent/tests/test_generation_source_stable_names.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_s2w6_single_process_migration.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_supervisor_generation_recovery.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_generation_source_stable_names.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`

### `multiprocess_generation_retirement`

- 职责（原模块声明）：LLM: 启动前持原生命周期锁逐项证明旧C/W/P1死亡；只移动设施，不写closure或业务请求，未知来源整组拒绝。 / 模块用途: 把已死的多进程代次移到私有退役目录，让两种拓扑可以重新启动。
- 定义：`_old_generations`, `_retirement_required`, `_executor_identities`, `_collect_identities`, `_prove`, `_require_executor_source`, `_tree_stamp`, `_worker_staging_target`, `_source_paths`, `_archive_dir`, `_write_manifest`, `_move_sources`, `_require_lock`, `_retained_directories`, `_require_snapshot`, `retire_dead_multiprocess_generations`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/legacy_mp_migration/journal.py`; `agent_py_agent/agent/gateway_parts/legacy_mp_migration/sources.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/cli/gateway_process.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_s2w6_single_process_migration.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`

### `multiprocess_global_write`

- 职责（原模块声明）：LLM: B2 仅定义请求合同，不接现有写入口；身份来自 P0/P3/P5，网关另从认证上下文裁决。 / 模块用途: 严格解析 worker 的全局写请求，不把请求正文当授权。
- 定义：`GlobalWriteError`, `validate_global_write`, `_validate_write_identity`, `_json_types`, `_json_object`, `encode_global_write`, `decode_global_write`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_operation_service.py`; `agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`; `agent_py_agent/agent/gateway_parts/coordinator_owner_management.py`; `agent_py_agent/agent/gateway_parts/http_handlers.py`; `agent_py_agent/agent/gateway_parts/http_input_receipt.py`; `agent_py_agent/agent/gateway_parts/input_delivery_service.py`; `agent_py_agent/agent/gateway_parts/model_control_dispatch.py`; `agent_py_agent/agent/gateway_parts/model_metadata_transaction.py`; `agent_py_agent/agent/gateway_parts/model_profile_service.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_host_command_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_maintenance.py`; `agent_py_agent/agent/gateway_parts/multiprocess_tool_global_write.py`; `agent_py_agent/agent/gateway_parts/private_approval_control.py`; `agent_py_agent/agent/gateway_parts/private_interaction_service.py`; `agent_py_agent/agent/gateway_parts/private_interaction_transaction.py`; `agent_py_agent/agent/user_space/home_index_compact.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_control_status_readonly.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_model_card_delivery.py`; `agent_py_agent/tests/test_coordinator_model_card_transactions.py`; `agent_py_agent/tests/test_coordinator_model_menu.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_private_approval_worker_trylocks.py`; `agent_py_agent/tests/test_private_interaction_safe_diagnostic.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_coordinator_model_card_delivery.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`

### `multiprocess_host_command_scope`

- 职责（原模块声明）：LLM: 本地主体宿主命令沿原RuntimeDB/ToolExecutor claim和原C短写门；不伪造ask/P3或W，不建立第二操作账。 / 模块用途: 将真实管理员冷配置短提交绑定到原持锁C、原宿主命令和已认证身份。
- 定义：`capture_host_command_writer`, `CoordinatorHostCommandWriter`, `OriginalHostCommandWriteSource`, `host_command_transaction_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/request_transactions.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_active_input_transport.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_background_lifecycle.py`

### `multiprocess_http_transport`

- 职责（原模块声明）：LLM: 原HTTP设施出生和实际finish_request区间各守原对象；通用upper/lower只读观察与领域wrapper共用同对象校验，不授业务/owner/P3权，Count0不构造新来源。 / 模块用途: 核协调者HTTP设施与本次真实请求的同源生命周期，阻止晚回调借旧handler写元数据。
- 定义：`require_http_transport`, `require_http_worker_source`, `HTTPBirthGateBusy`, `original_http_birth`, `_HTTPRequestWork`, `current_http_request_work`, `require_http_request_work`, `capture_http_request_work`, `HTTPHandlerWorkChanged`, `require_http_handler_work`, `capture_http_handler_work`, `http_request_work_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/admin_api.py`; `agent_py_agent/agent/gateway_parts/bounded_http_server.py`; `agent_py_agent/agent/gateway_parts/client_inflight_projection.py`; `agent_py_agent/agent/gateway_parts/client_session_list.py`; `agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`; `agent_py_agent/agent/gateway_parts/coordinator_owner_management.py`; `agent_py_agent/agent/gateway_parts/http_handlers.py`; `agent_py_agent/agent/gateway_parts/http_service.py`; `agent_py_agent/agent/gateway_parts/model_metadata_transaction.py`; `agent_py_agent/agent/gateway_parts/multiprocess_input_receipt.py`; `agent_py_agent/agent/gateway_parts/multiprocess_local_control.py`; `agent_py_agent/agent/gateway_parts/private_interaction_transaction.py`; `agent_py_agent/agent/plugin_host/http_transport.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/tests/test_coordinator_client_memory_source.py`; `agent_py_agent/tests/test_coordinator_control_status_readonly.py`; `agent_py_agent/tests/test_coordinator_owner_management_first_fault.py`; `agent_py_agent/tests/test_gateway_http_current_work.py`; `agent_py_agent/tests/test_mp00_lock_order.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_http_current_work.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_coordinator_owner_management_first_fault.py`; `agent_py_agent/tests/test_coordinator_control_status_readonly.py`; `agent_py_agent/tests/test_coordinator_client_memory_source.py`

### `multiprocess_input_receipt`

- 职责（原模块声明）：LLM: C仅在实际HTTP work内提交固定receipt元数据，不申请/释放P3或写guidance；原input T→controller→lease→Root，整份CAS且JSON只试原锁。 / 模块用途: 保存唯一输入身份及固定去向；同一纯deadline贯穿原CAS末写，actual HTTP source仍唯一来源。
- 定义：`_missing`, `_store_directory_exists`, `_pending_replay_matches`, `CoordinatorInputReceiptSource`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/http_input_receipt.py`; `agent_py_agent/tests/test_gateway_active_input_budget_fences.py`; `agent_py_agent/tests/test_gateway_active_input_publication.py`; `agent_py_agent/tests/test_gateway_background_active_input.py`; `agent_py_agent/tests/test_gateway_background_fresh_queue.py`; `agent_py_agent/tests/test_gateway_c_input_receipt_source.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_local_ask_owner_transport.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_c_input_receipt_source.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_background_fresh_queue.py`; `agent_py_agent/tests/test_gateway_background_active_input.py`; `agent_py_agent/tests/test_gateway_active_input_budget_fences.py`

### `multiprocess_input_reconcile`

- 职责（原模块声明）：LLM: 修原queued/cursor及完整v3 locator投影；locator不是消费/授权，旧漏ref晚于封口仍defer；C短门内JSON只尝试原锁，busy同对象停止本行；原C/Root/lease与receipt末门不放宽，不授P3/W/Store权。 / 模块用途: 让冷或健康协调者补原收件队列；未支持的绑定输入明确延期，不创建foreign Agent或第二账本。
- 定义：`_identity_bytes`, `_missing_source`, `CoordinatorInputDeferredError`, `_QueuedInputSource`, `reconcile_coordinator_queued_inputs`, `_reconcile_input_row`, `require_coordinator_input_progress`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/http_input_receipt.py`; `agent_py_agent/agent/gateway_parts/input_delivery_service.py`; `agent_py_agent/agent/gateway_parts/input_target_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_active_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_input.py`; `agent_py_agent/agent/gateway_parts/multiprocess_input_receipt.py`; `agent_py_agent/cli/gateway_coordinator_request_pump.py`; `agent_py_agent/cli/gateway_loops.py`; `agent_py_agent/tests/test_gateway_active_input_publication.py`; `agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py`; `agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`; `agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py`; `agent_py_agent/tests/test_gateway_active_input_publication.py`

### `multiprocess_kernel_fd_darwin`

- 职责（原模块声明）：LLM: fileport仅复制真实FD；同消息AUDIT证据仍由内核给出，FD/port持有不授业务权限。 / 模块用途: 为Darwin原endpoint增添有限FD消息，坏消息和失败转换必须释放copyout资源。
- 定义：`_configure`, `send_fds`, `_sender`, `_body`, `receive_fds`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_kernel_message_fds.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_kernel_message`

- 职责（原模块声明）：LLM: 原payload/PID/FD形状不变，Darwin同次AUDIT附不可变pidversion事实，仅供原EXIT后末回送认证；locator/正文/环境不授信，同步kernel/host/exec探针。 / 模块用途: Darwin Mach AUDIT trailer与Linux SCM_CREDENTIALS消息transport。
- 定义：`KernelMessageError`, `_seconds`, `_milliseconds`, `_address`, `_payload`, `_mach_result`, `_AuditToken`, `_DarwinMessage`, `_LinuxMessage`, `_backend`, `_Endpoint`, `KernelReceivedMessage`, `KernelMessageReceiver`, `KernelMessageSender`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/logging.py`; `agent_py_agent/agent/gateway_parts/multiprocess_kernel_fd_darwin.py`; `agent_py_agent/agent/gateway_parts/multiprocess_kernel_message_fds.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_control_receive.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`

### `multiprocess_kernel_message_fds`

- 职责（原模块声明）：LLM: FD只随同次内核消息运输，不授业务写权限；失败关闭已收到资源，原endpoint creator门仍在公开层。 / 模块用途: 供生产执行桥传递真实标准流，保留Linux逐包凭据和Darwin AUDIT来源。
- 定义：`validate_fds`, `close_fds`, `send_fds`, `receive_fds`, `_decode_linux_controls`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_kernel_fd_darwin.py`; `agent_py_agent/agent/gateway_parts/multiprocess_kernel_message.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_control_receive.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`

### `multiprocess_kernel_peer`

- 职责（原模块声明）：LLM: 原生Unix连接的内核peer观察；仅PID事实，不提供C租约/owner准入，也不接受正文声明。 / 模块用途: 为后续原issuer接线提供可信连接进程观察点。 / LLM: Darwin sys/un.h实际SDK预处理读回SOL_LOCAL=0、LOCAL_PEERPID=0x002；不从配置或请求修改。 / 参数用途: macOS内核Unix peer PID选项，Python未导出相应符号。
- 定义：`PeerCredentialsUnavailable`, `kernel_peer_pid`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_commit_channel.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_local_control`

- 职责（原模块声明）：LLM: client/管理共用同source与首native槽；client只当前owner且无receipt端口，管理memory仅factory栈保原seal至父unknown结束，不借给client/Count0。 / LLM: memory固定binder须同对象保留原来源与原ingress两类实际拒绝，恢复事实不能洗掉首次失败；登记资格仍唯一53谓词。 / LLM: cold原HTTP source仅保首个实际producer native失败对象；后继固定writer/正常出口拒绝，当前physical/common outer优先；Count0不使用该槽。 / LLM: C控制pin只保责任；每次文件RMW消费同原executing/C/实际资源owner的P2短门，管理不借main权限，网络/退役门外。 / LLM: 原ready Agent/lease-only只走固定receipt叶且原T由caller持有；fullcontroller和通用perform不回退，不造P2或关闭证明。 / 模块用途: 同类真实HTTP来源另持原操作T；本机原插话对账另走固定expected/result叶，普通receipt状态图保持；把本机控制绑定到同一原C及原控制回执，撤权后禁止新的配置叶子写，原责任仍精确结清。
- 定义：`_retire_http_management`, `_control_write_binding`, `_ClientMemoryHTTPFacts`, `_original_http_failure_producer`, `_initialize_original_source_refs`, `_capture_client_memory_facts`, `_ClientMemorySourceMode`, `_close_management_memory_stack`, `_LocalControlWriteSource`, `local_control_operation_scope`, `_require_receipt_origin`, `_require_local_private_approval_receipt`, `_commit_original_private_approval_reconciliation`, `_require_private_approval_reconciliation`, `_commit_original_steer_reconciliation`, `_require_local_steer_reconciliation`, `_commit_original_control_receipt`, `_write_original_control_receipt`, `_ready_receipt_source`, `_require_ready_receipt_window`, `_confirm_ready_receipt_write`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_operation_service.py`; `agent_py_agent/agent/gateway_parts/coordinator_actor_resources.py`; `agent_py_agent/agent/gateway_parts/coordinator_client_memory.py`; `agent_py_agent/agent/gateway_parts/coordinator_memory_candidates.py`; `agent_py_agent/agent/gateway_parts/coordinator_owner_management.py`; `agent_py_agent/agent/gateway_parts/memory_preview_transaction.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/owner_management_control.py`; `agent_py_agent/agent/gateway_parts/private_approval_control.py`; `agent_py_agent/tests/test_coordinator_agents_domain_faults.py`; `agent_py_agent/tests/test_coordinator_agents_domain_review_edges.py`; `agent_py_agent/tests/test_coordinator_client_memory_source.py`; `agent_py_agent/tests/test_coordinator_memory_end_to_end.py`; `agent_py_agent/tests/test_coordinator_memory_http_current_review.py`; `agent_py_agent/tests/test_coordinator_owner_management_fault_domains.py`; `agent_py_agent/tests/test_coordinator_owner_management_first_fault.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`; `agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_receipts.py`; `agent_py_agent/tests/test_group_control_lock_birth.py`; `agent_py_agent/tests/test_local_control_reconciliation_writer.py`; `agent_py_agent/tests/test_local_store_coordinator_io.py`; `agent_py_agent/tests/test_memory_j1_write_ports.py`; `agent_py_agent/tests/test_owner_write_errors.py`; `agent_py_agent/tests/test_ready_control_receipt_source.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_owner_write_errors.py`; `agent_py_agent/tests/test_ready_control_receipt_source.py`; `agent_py_agent/tests/test_coordinator_owner_management_fault_domains.py`; `agent_py_agent/tests/test_coordinator_memory_http_current_review.py`

### `multiprocess_maintenance`

- 职责（原模块声明）：LLM: 原共享四索引扫描不授写权；replace运输叶限同次PID/thread单次窗口，默认controller及生产Root末门保留，不改变due/sidecar/ref合同。 / 模块用途: 让owner私有维护下沉W后，共享压缩仍由同一个C写者完成，不建维护状态账。
- 定义：`CoordinatorIndexCompact`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_global_index_compact.py`

### `multiprocess_mode`

- 职责（原模块声明）：LLM: 进程模式只在最早CLI建立；导入把同一冻结查询接到executor及global-write拒门，不授C/管理员权，不构造Home/Agent或增加Count0 I/O；同步两拒门与worker启动测试。 / 模块用途: 保存C核验的worker分区，供执行登记和全局写入口拒绝受限进程。
- 定义：`WorkerLaunch`, `establish_worker_mode`, `worker_environment`, `_launch_payload`, `_valid_launch`, `current_worker_partition`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_service.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_request.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap.py`; `agent_py_agent/agent/gateway_parts/multiprocess_curator_accounting.py`; `agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_maintenance.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_queue.py`; `agent_py_agent/agent/gateway_parts/multiprocess_state.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_agent.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/queue_service.py`; `agent_py_agent/agent/gateway_parts/request_entry_admission.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/agent/memory_store/retention.py`; `agent_py_agent/agent/user_space/home_index_compact.py`; `agent_py_agent/agent/user_space/owner_maintenance.py`; `agent_py_agent/cli/gateway_worker.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_c_http_wiring.py`; `agent_py_agent/tests/test_gateway_claim_transition_scope.py`; `agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_gateway_global_admin_write_boundaries.py`; `agent_py_agent/tests/test_gateway_global_write_current_consumers.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_http_transport.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_state.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_worker_bootstrap_scope.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_global_write_boundaries.py`; `agent_py_agent/tests/test_gateway_worker_workspace_transport.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_private_approval_worker_trylocks.py`; `agent_py_agent/tests/test_public_agent_home_initialization_order.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_shared_seed_write_guard.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap.py`; `agent_py_agent/tests/test_gateway_current_requeue.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`

### `multiprocess_native_command_domain`

- 职责（原模块声明）：LLM: Original C installs this observer on a trusted waiting command shim before R. It proves ONLY a never-forked singleton's exit, not containment of an arbitrary tree. / 模块用途: 由无FD workloop保存真实Darwin根FORK/EXIT和原队列关闭历史；fork、缺口或非原C使用均不结清。
- 定义：`NativeSingletonDomain`, `NativeCommandDomainObserver`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_process_completion_events.py`; `agent_py_agent/tests/test_shell_orphan_kill.py`

### `multiprocess_native_commit_client`

- 职责（原模块声明）：LLM: 原W只读取原父进程的同次kernel reply；返回值报告C已完成的事，绝不授权W侧文件/工具副作用。 / 模块用途: 提供无重试的W客户端；strict原父同seq负回执只清本RPC phantom，未知交付及业务责任保持。
- 定义：`NativeCommitNotStarted`, `NativeCommitClient`, `_decode_reply`, `_reply`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_native_commit_session`

- 职责（原模块声明）：LLM: C只消费同一次native receive的内核sender，原issuer/lease/P0/P4核验仍是唯一责任来源；ACK不是W写许可。 / 模块用途: 把原生传输接到原责任门；同次producer可证pre-pin拒绝回一次strict NACK，其它失败保未知，不新增P1/P3、准入或重试。
- 定义：`NativeCommitSession`, `_decode_packet`, `_native_request_identity`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`

### `multiprocess_native_control_receive`

- 职责（原模块声明）：LLM: 控制RPC只接无FD帧；内核FD领取与拒绝清理必须同一路，结果不授业务许可。 / 模块用途: 为C责任消息和W回复共用严格无资源控制信封，不能用普通receive漏掉复杂消息资源。 / LLM: 先完成同次native receive并保留真实sender；多余FD副本仅清理，不解释正文、更新序号或登记责任。
- 定义：`receive_control_packet`, `receive_control_packet_with_instance`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_commit_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`

### `multiprocess_native_coordinator_host`

- 职责（原模块声明）：LLM: 本地C主体背景host只走原CoordinatorExecutorSource/T/P3/P5、P1/bootstrap/kernel sender；root仅留原refs无第二权威，设施不误进用户namespace，不能legacy裸ledger，同步C工具/P5 tests。 / 模块用途: 让原C已批准后台工具复用原native host协议，并给原controller保留真实宿主与责任收尾引用。
- 定义：`CoordinatorNativeHostRoot`, `launch_coordinator_native_host`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_source_closure.py`

### `multiprocess_native_coordinator_process`

- 职责（原模块声明）：LLM: C后台host句柄只观察原HostLane实际Popen，完成末回送前WNOWAIT保sender出生；只复用原标准流通信实现，不fake W transport或恢复C权限，同步真正短host竞态/生命周期测试。 / 模块用途: 给C主体原后台工具提供Popen兼容句柄，让poll/wait不能先回收尚待认证的宿主。
- 定义：`CoordinatorNativeHostProcess`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_host.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_source_closure.py`

### `multiprocess_native_executor_client`

- 职责（原模块声明）：LLM: W仅运输资源并消费原C事实，不授予W创建进程、写P1或写ACK的权限，原内核对端校验和执行围栏保持。 / 模块用途: 串行管理原生执行器RPC，提供W持有的兼容Popen标准流。
- 定义：`NativeExecutorClient`, `_binding`, `NativeExecutorRequest`, `_wire`, `_remaining`, `_write_context`, `NativeExecutorProcess`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/tests/test_tui_cpu_wakeup_optimizations.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_tui_cpu_wakeup_optimizations.py`

### `multiprocess_native_executor_io`

- 职责（原模块声明）：LLM: Standard-stream resources only; no RPC, command execution, authority or ledger lives here. / 模块用途: Prepare W-owned stdio copies for native FD transport and decode C's received resources.
- 定义：`NativeExecutorIO`, `prepare_native_executor_io`, `_source_fd`, `_pipe`, `decode_native_executor_io`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_tui_cpu_wakeup_optimizations.py`

### `multiprocess_native_executor_lane`

- 职责（原模块声明）：LLM: Separate resource endpoint, same original issuer; names only locate, every packet keeps its kernel sender. / 模块用途: Own the C resource receiver and independent executor consumer without creating a second responsibility system.
- 定义：`require_lane_issuer`, `executor_addresses`, `NativeExecutorLane`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_gateway_partial_startup_drain.py`; `agent_py_agent/tests/test_gateway_source_refusal_diagnostics.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`

### `multiprocess_native_executor_process`

- 职责（原模块声明）：LLM: 只操作本session已登记的实际C子进程；token/PID文本不授信，查询/清理不创建执行者或改P1/P3。 / 模块用途: 给W进程代理提供原生只读poll和精确原实例signal，信号清理在请求停止后仍可进行。
- 定义：`require_process_request`, `handle_process_request`, `_original_returncode`, `_signal_original`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_native_executor_scope`

- 职责（原模块声明）：LLM: 原P5绑定只提供定位；真实登记、P3准入、原T与C短门仍由原native执行session裁决。 / 模块用途: 把正式W认领请求及规范工具调用接到同一个持久资源客户端，不启动W进程、不重试。
- 定义：`_ClaimExecutor`, `_native_command`, `native_request_executor_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_approval_retirement_worker_future.py`; `agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`

### `multiprocess_native_executor_session`

- 职责（原模块声明）：LLM: 独立执行资源端点；同包kernel sender和原T/P3/C门唯一授信，命令仅经匿名FD，不放宽旧无FD责任端点。 / 模块用途: 由原C实际预留、创建、登记、放行执行者，回执只报告事实，不授予W写权限。
- 定义：`NativeExecutorSession`, `_executor_request_identity`, `_read_executor_command`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_gateway_source_refusal_diagnostics.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`

### `multiprocess_native_host`

- 职责（原模块声明）：LLM: 仅原C持宿主来源/P1/Popen，定位名不授权，每个消息核实际宿主；固定模块origin兼容.py/.pyc，同步核对发行闭包。 / 模块用途: 保持原生后台宿主的固定入口，来源、会话、通道和客户端仍由原模块各自负责。
- 定义：`_native_host_bootstrap_argv`, `start_native_host`, `run_native_delegate`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_bootstrap.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_host_authority`

- 职责（原模块声明）：LLM: 精确原W来源或原CoordinatorExecutorSource各消费同原issuer/lease/T/P3，角色由实际C对象决定；不造W issuer/新authority，不从wire/JSON授信，同步host/P5/C scope tests。 / 模块用途: 在原native host协议内区分C主体和W来源的原pin、原准入及原生命周期锁。
- 定义：`coordinator_host_source`, `begin_host_pin`, `finish_host_pin`, `host_admission`, `host_lifecycle_gate`, `host_pin_holder`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_reference.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/ft/_import_probe.py`

### `multiprocess_native_host_bootstrap`

- 职责（原模块声明）：LLM: 仅原C选择这个固定-I入口，不接受任意模块或运输source对象；保持原FD、身份校验和失败退出125合同，同步核对native host调用方。 / 模块用途: 通过原固定native host入口关闭匿名交接FD、认证真实宿主，再进入原后台业务。 / LLM: 保持原五字段v1帧与上限，只加载同包固定模块；失败退出125且不打印正文，不能增加授权或备用加载入口。 / 函数用途: 从可信包根加载唯一native host委托入口，参数不完整时直接退出。
- 定义：`main`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_host_client`

- 职责（原模块声明）：LLM: Closed native host client binds one original request; every process operation returns to the original C-held child. / 模块用途: Preserve host-only background_command SDK restrictions, no-reap observation and strict v2 cleanup receipts.
- 定义：`NativeHostClient`, `NativeHostProcess`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_host_lane`

- 职责（原模块声明）：LLM: Lane captures original C resources; unknown host startup/EOF/death never infers a responsibility finish. / 模块用途: Own separate native host endpoints, authenticated connection and lifetime consumer with exact drain rules.
- 定义：`NativeHostLane`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_executor_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_host_observation`

- 职责（原模块声明）：LLM: Native-only read observations/cleanup use actual kernel instances, never ps fallback or new execution authority. / 模块用途: Pair Store birth observations with original C's real child handle and bounded known-tree cleanup.
- 定义：`native_observation_active`, `store_birth`, `kernel_terminated`, `_darwin_info`, `_darwin_members`, `command_snapshot`, `stop_command_tree`, `_reap_observed_root`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/conversation/process_events.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/tooling/process_registry.py`; `agent_py_agent/tests/test_process_completion_events.py`; `agent_py_agent/tests/test_shell_orphan_kill.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_shell_orphan_kill.py`; `agent_py_agent/tests/test_process_completion_events.py`

### `multiprocess_native_host_session`

- 职责（原模块声明）：LLM: 原host实际sender/Popen独立认证，原W issuer或精确C source及原P1均只保存在C；不继承W-only服务或由wire角色授信，同步host/C/P5测试。 / 模块用途: Consume native host commands, exact responsibility finishes and C-owned process observations.
- 定义：`NativeHostSession`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_host_source`

- 职责（原模块声明）：LLM: This wrapper captures the original C source; no wire payload or bare ledger reconstructs write authority. / 模块用途: Recheck original C/W/T/P3 and independent actual-host identity at each background command effect.
- 定义：`_HostSource`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_command_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`

### `multiprocess_native_host_start`

- 职责（原模块声明）：LLM: This C-local wrapper adds only lane lifetime checks around the original C/T/P1 gates; it is not a new writer or authority. / 模块用途: 在真实host启动全程保留原attempt，物理spawn/R与本地退役同门，阻止process为空时错误排空。
- 定义：`NativeHostStartSource`, `NativeHostStartObserver`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_host_wire`

- 职责（原模块声明）：LLM: Addresses locate original C-selected resources only; the kernel sender and captured source still grant authority. / 模块用途: Preserve the native host address derivation and unchanged READY/CONNECTED byte protocol.
- 定义：`host_addresses`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_bootstrap_discovery_review.py`; `agent_py_agent/tests/test_gateway_bootstrap_package_closure.py`

### `multiprocess_native_kernel_history`

- 职责（原模块声明）：LLM: Darwin原C的无FD workloop保留R前PROC/FD关闭历史；guard仅为条件清理，不能认证代次。SPI失败保持unknown，调用者须核原source/attempt；同步native域及原故障测试。 / 模块用途: 让全部数字FD复用之后仍能读取原内核历史，并原子拒绝关闭复用的陌生FD。
- 定义：`_Event`, `_ExtInfo`, `_Params`, `_Event64`, `_native`, `GuardedQueue`, `GuardedPipeDescriptor`, `KernelHistory`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_command_domain.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_native_linux_domain`

- 职责（原模块声明）：LLM: 仅原C的正count执行者实际spawn线程建立独立PID namespace；Popen直系init/P1/R不换链，无CAP_SYS_ADMIN失败关闭，无用户正文/argv判断；同步原launcher、P0/P1/P6和Linux真probe。 / 模块用途: 为Linux用户命令持有R前原namespace/init事实，借内核init退出屏障证明整个fork域结束。
- 定义：`NativeNamespaceUnavailable`, `_namespace_identity`, `_birth_descriptor_identity`, `_require_capability`, `prepare_native_domain`, `NativePidNamespaceDomain`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_reference.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`

### `multiprocess_native_linux_reference`

- 职责（原模块声明）：LLM: 仅原C正count Linux用户域使用同进程私有fdtable设施；原init必须仍等R，不增加进程/权限/账；只有实际pidfd waitid结果可结屏障，联测domain/P5及foreign清理。 / 模块用途: 在原C隔离的描述符表中持有namespace引用，并保留原子进程真实回收事实。
- 定义：`_require_reference_creation`, `NativeNamespaceReference`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`; `agent_py_agent/tests/ft/_import_probe.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/ft/_import_probe.py`

### `multiprocess_native_materialize`

- 职责（原模块声明）：LLM: 固定C物理初始化RPC；正文仅定位原请求/attempt，不提供owner、路径、代码或写许可，原P3/T/lease仍唯一权威。 / 模块用途: 在原C短门内物化原请求用户目录及种子，让W不再负责这一首写。
- 定义：`require_materialize_request`, `register_owner_materialization`, `materialize_coordinator_owner_home`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_native_operation_reclaim`

- 职责（原模块声明）：LLM: 只回收原C内存定位表；canonical必须在原T读取并匹配原P3/attempt，不结清pin、不改预留或终态。 / 模块用途: 让短提交表随原请求终态回收，未知事实保留防重放标记。
- 定义：`reclaim_terminal_operations`, `_reclaim_candidate`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`

### `multiprocess_native_reclaim_io`

- 职责（原模块声明）：LLM: 机会GC专用只读入口；原T仍由调用方持有，JSON锁竞争跳过，严格no-follow/普通文件与实际字节上限不授予权限。 / 模块用途: 避免旧FIFO或终态JSON忙锁堵住原生消息；不改变业务准入的保守读取。
- 定义：`read_json_file_report`, `read_reservation`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_operation_reclaim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_native_refusal_consume`

- 职责（原模块声明）：LLM: 仅原execute_worker_claim的materialize词法catch消费，ACK/token不授权限；同canonical及原fullclaim才结束Future，本模块不写账/结输入/P6。 / 模块用途: 核收原C已经提交的本请求失败，让原_reap按True正常收槽；wire-only/错来源仍保原异常。 / LLM: token是调用栈归属而非业务许可；客户端原父出生、creator、原异常和同次seq都独立核，不借finish或之后异常清槽。
- 定义：`require_materialize_rejection`, `consume_first_refusal`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_execution.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_native_refusal_source`

- 职责（原模块声明）：LLM: 只在原C首次materialize拒绝时消费原operation/claim/P3出处，不创建准入、身份、恢复或执行权。同步native短操作与终态核收测试。 / 模块用途: 严格核对同一原请求的未执行来源；缺失、历史及任何未知均保原拒绝，不把wire NACK当业务证明。
- 定义：`original_terminal_gate`, `claim_bytes`, `require_first_claim`, `require_no_native_effect`, `require_captured_claim`, `read_claim_payload`, `require_no_owner_pin`, `_original_pin_issuers`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_terminal.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_native_refusal_terminal`

- 职责（原模块声明）：LLM: 仅原first materialize/pre-pin事件可达；原T→controller→lease固定writer，无owner清算不借隔离发业务权。同步wire、P3及终态回归。 / 模块用途: 把可证未开始的原请求交原FAILED/P3/展示链；输入结算明确保欠账，不造Agent、恢复source、成功或重放。
- 定义：`terminalize_first_refusal`, `require_refusal_terminal`, `project_first_refusal`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_first_materialize_refusal.py`

### `multiprocess_native_runtime`

- 职责（原模块声明）：LLM: 正count组合根独占原endpoint/issuer/session；定位名不授信，bootstrap逐消息核原P0/P4，不写第二账本。 / 模块用途: 连接生产C与W的两个独立原生责任/资源桥，closing时保持接收直到原责任逐项结清。
- 定义：`native_addresses`, `NativeWorkerLane`, `_WorkerNativeClient`, `connect_worker_native`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_partial_startup_drain.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_worker_terminal_heartbeat.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_partial_startup_drain.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_worker_terminal_heartbeat.py`

### `multiprocess_native_short_operation`

- 职责（原模块声明）：LLM: 只保存原C注册的callback/请求回合引用；定位key不能授信，原T与原C生命周期门必须包住实际C副作用。 / 模块用途: 一次性C侧短提交，并显式运输本次dispatch拒绝归属；不在W执行回调，不创建第二P1/P3或名额。
- 定义：`require_original_native_admission`, `NativeShortOperation`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_original_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_authority.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`

### `multiprocess_processing_recovery`

- 职责（原模块声明）：LLM: 正MP stale恢复只消费当次原P2停止/P3补章/currentC与同owner RuntimePolicy；此source只准元数据，不授P5/模型/工具，Count0无新查询，修改同步CAS/ports/终态回归。 / 模块用途: 把原processing恢复动作封闭在真实旧域停止后的同步短提交窗口；冷C只投影原配置与路径，未知保留原占用和原文件。
- 定义：`processing_recovery_required`, `_rejection`, `recover_stale_processing`, `_frozen_ownership`, `OriginalProcessingRecoverySource`, `_require_runtime_policy`, `_recovery_base_home`, `_recovery_runtime_paths`, `_recovery_store`, `current_processing_recovery`, `processing_metadata_scope`, `_bytes`, `_RecoveryWindow`, `_held_recovery_window`, `_request_metadata_scope`, `_associated_receipt`, `_receipt_identity`, `_require_window_receipt`, `_receipt_peer`, `original_processing_target`, `_complete_transport_source`, `_complete_processing_claim`, `stopped_requeue_evidence`, `_requeued_stop_records`, `_requeued_stop_key`, `_RequeuedStopObservation`, `_prepare_requeued_stop`, `requeued_closeout_scope`, `require_stopped_requeued_source`, `processing_authority_rejection`, `_require_published_recovery_binding`, `original_claim_covers_reservation`, `_require_store_thread`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/input_delivery_service.py`; `agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_unroutable_closeout.py`; `agent_py_agent/agent/gateway_parts/owner_retention.py`; `agent_py_agent/agent/gateway_parts/recovery.py`; `agent_py_agent/agent/gateway_parts/request_transactions.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/tests/test_gateway_cold_processing_recovery.py`; `agent_py_agent/tests/test_gateway_input_transport_codec.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_v13_w4_capability_request_closeout.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_v13_w4_capability_request_closeout.py`; `agent_py_agent/tests/test_gateway_cold_processing_recovery.py`

### `multiprocess_queue`

- 职责（原模块声明）：LLM: 仅在原 queue_service 的 T 锁内检查 P5 快照；不获取第二把请求锁、不移动文件或释放预留。 / 模块用途: 把已有 P4/P5 只读合同接到真实认领动作之前，拒绝 worker 从旧单进程入口绕过。
- 定义：`require_worker_claim_locked`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/queue_service.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_future.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_channel_owner_isolation.py`

### `multiprocess_record_schema`

- 职责（原模块声明）：LLM: P0 v1/golden不变；P1 v2域字段不授死亡权；generation v2独立关闭范围仅由原producer封存，不复活lease/native或签P2。 / 本模块只定义结构，无文件、锁、进程、模型或配置副作用。同步 records 和协议17节。 / 模块用途: 定义唯一记录字段及显式版本，原代次关闭事实与旧count记录分开，旧版原字节保留。
- 定义：`CoordinatorGeneration`, `CoordinatorGenerationV2`, `ProcessIdentity`, `ExecutorReservation`, `ExecutorReservationV2`, `AdmissionReservation`, `AdmissionReservationV2`, `TakeoverProof`, `TakeoverProofV2`, `WorkerRegistry`, `WorkerHeartbeat`, `CancellationRecord`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_records.py`; `agent_py_agent/tests/test_gateway_multiprocess_schema.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_multiprocess_schema.py`

### `multiprocess_records`

- 职责（原模块声明）：LLM: 唯一纯记录接口；generation v2只校完整关闭范围，不认证producer或签P2；旧v1/P1/P3字节和校验保持。 / 放行、整树死亡、终态来源均由未来 P1/P2/P3 在真实锁和持久边界证明，本接口不替代它们。 / 模块用途: 提供版本记录、规范编码和状态转换签名，不启动执行者或创建目录。
- 定义：`MultiprocessRecordError`, `validate_record`, `_bootstrap_record`, `_stop_record`, `_closed_generation`, `_closure_digests`, `serialize_record`, `record_digest`, `deserialize_record`, `advance_executor`, `release_admission`, `_canonical_bytes`, `_shape`, `_integer`, `_identifier`, `_sha`, `_expect_schema`, `_coordinator`, `_identity`, `_executor`, `_executor_v2`, `_admission`, `_admission_scope`, `_route`, `_canonical_owner`, `_terminal_fact`, `_canonical_terminal`, `_proof`, `_proof_scope`, `_proof_role`, `_proof_worker_index`, `_identifiers`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_publish.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_ready.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_schema.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_identity.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_binding.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_domain_schema.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_ledger.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_global_write.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_authority.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_operation_reclaim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_reclaim_io.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_consume.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_session_key.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_drain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_schema.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_tree.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_ledger.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_recheck.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_unresolved_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_dispatch.py`; `agent_py_agent/agent/gateway_parts/standalone_sources.py`; `agent_py_agent/agent/gateway_parts/supervisor.py`; `agent_py_agent/agent/gateway_parts/supervisor_generation.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_gateway_closed_generation_progress.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_semantics.py`; `agent_py_agent/tests/test_gateway_multiprocess_records.py`; `agent_py_agent/tests/test_gateway_multiprocess_session_key.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_standalone_sources.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_supervisor_generation_recovery.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_semantics.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`

### `multiprocess_route`

- 职责（原模块声明）：LLM: C/W 路由只投影 main 认证入口唯一的 host_request_owner.v1；不得另建 owner、actor 或准入权威。 / 只读纯盖章/校验，不启动 worker；受保护 metadata 断言须与宿主 owner 一致，admission 另核原 receipt，不能从变更队列自证归属。 / 模块用途: 将已有宿主归属、请求内容和启动分区绑定为路由，校验只返回结构化事实，不读配置、写盘或执行模型工具。
- 定义：`OwnerRouteMismatchError`, `RouteStamp`, `WorkerPartition`, `owner_digest`, `owner_worker`, `freeze_request_route`, `validate_worker_route`, `validate_coordinator_route`, `validate_published_route`, `_validate_route`, `_route_rejection`, `_complete_route`, `_request_owner`, `_owner_parts`, `_ingress_identity`, `_request_binding`, `_digest`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_plane.py`; `agent_py_agent/agent/gateway_parts/coordinator_actor_resources.py`; `agent_py_agent/agent/gateway_parts/coordinator_control_transport.py`; `agent_py_agent/agent/gateway_parts/curator_quota_index.py`; `agent_py_agent/agent/gateway_parts/http_handlers.py`; `agent_py_agent/agent/gateway_parts/input_target_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_agent_contract.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap.py`; `agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_ready.py`; `agent_py_agent/agent/gateway_parts/multiprocess_control_forward.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_claim.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_curator_accounting.py`; `agent_py_agent/agent/gateway_parts/multiprocess_dispatch.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_delegation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_original_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_host_command_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_local_control.py`; `agent_py_agent/agent/gateway_parts/multiprocess_maintenance.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_materialize.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_records.py`; `agent_py_agent/agent/gateway_parts/multiprocess_state.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_drain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_schema.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_scope.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_ledger.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_unresolved_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/private_interaction_transaction.py`; `agent_py_agent/agent/gateway_parts/queued_steer_expiry.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/agent/gateway_parts/supervisor_generation.py`; `agent_py_agent/cli/gateway_worker.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/support/multiprocess_agent_cases.py`; `agent_py_agent/tests/support/multiprocess_dispatch_cases.py`; `agent_py_agent/tests/support/multiprocess_processing_cases.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_control_receipt_delivery_closure.py`; `agent_py_agent/tests/test_coordinator_agents_domain.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_owner_management_source.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_dual_owner_concurrent_canary.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_c_http_wiring.py`; `agent_py_agent/tests/test_gateway_c_queued_input_reconcile.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_global_write_current_consumers.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap.py`; `agent_py_agent/tests/test_gateway_multiprocess_bootstrap_ready.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_http_transport.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_multiprocess_route.py`; `agent_py_agent/tests/test_gateway_multiprocess_state.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_gateway_prepared_owner_identity.py`; `agent_py_agent/tests/test_gateway_prepared_route_immutability.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_receipts.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_standalone_migration.py`; `agent_py_agent/tests/test_gateway_worker_bootstrap_scope.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_global_write_boundaries.py`; `agent_py_agent/tests/test_group_control_lock_birth.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_guard.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`; `agent_py_agent/tests/test_public_agent_home_initialization_order.py`; `agent_py_agent/tests/test_rework_r9_harvester_request_cleanup.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_shared_seed_write_guard.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`

### `multiprocess_session_key`

- 职责（原模块声明）：LLM: 准入只投影现有顺序键，不读取 owner 的会话仓库；规范版本独立于预留记录。 / 模块用途: 冻结 C 与 W 共用的纯会话键投影接口。
- 定义：`project_admission_session`, `session_text`, `validate_session_scope`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_records.py`; `agent_py_agent/tests/test_gateway_multiprocess_session_key.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_multiprocess_session_key.py`

### `multiprocess_state`

- 职责（原模块声明）：LLM: worker 就绪只证明当前代 C 状态及本进程端口登记；坏时间含不可转float的巨大数均不健康，投影不授予执行权，更不是死亡证明。 / 模块用途: 校验就绪门、构造心跳和查询可盖章分区，不认领请求、不加载 owner 数据。
- 定义：`prepare_worker_readiness`, `_coordinator_rejection`, `_not_ready`, `worker_heartbeat`, `stampable_workers`, `_stampable`, `worker_control_action`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_bootstrap_ready.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_state.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`; `agent_py_agent/tests/test_gateway_worker_component.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`; `agent_py_agent/tests/test_gateway_multiprocess_state.py`

### `multiprocess_stop`

- 职责（原模块声明）：LLM: P6 只运输原 C/P5 取消事实；原 processing/T 锁仍是唯一权威，不写文件或发中断；纯取消/时间校验必须导入B1唯一schema实现。 / 模块用途: 定义精确取消格式与 worker 消费门。
- 定义：`cancellation_record`, `require_cancel_record`, `_require_cancelled_processing`, `require_stop_partition`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_stop_scope.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`; `agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_multiprocess_record_boundaries.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`

### `multiprocess_stop_drain`

- 职责（原模块声明）：LLM: P6只消费原权威冻结的C/W观察，不扫描/等待/取消/写drained；原W API保持，C明确独立坐标，同步生产collector及六类拒绝测试。 / 模块用途: 用六类完整在途清单和原P2整树证明判定准确角色能否排空。
- 定义：`InFlightItem`, `DrainInventory`, `DrainBlocker`, `DrainAssessment`, `assess_drain`, `_require_inventory`, `_require_item`, `_tree_blockers`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_drain.py`; `agent_py_agent/cli/gateway_worker_runtime.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`

### `multiprocess_stop_schema`

- 职责（原模块声明）：LLM: P0/P6共用唯一取消结构校验；从固定来源stop完整搬出纯函数以隔离dispatch运行依赖，未来stop须导同一对象。 / 模块用途: 只检查精确取消记录和观察时间，不取得取消授权、不写盘、不发中断或证明退出。
- 定义：`validate_cancellation_record`, `require_stop_time`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_records.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_source_closure.py`

### `multiprocess_stop_scope`

- 职责（原模块声明）：LLM: P6角色只描述原C/W观察范围，不授执行或接手权；原W分区API保持，C明确没有W编号，同步原六类排空/P2范围测试。 / 模块用途: 给停止观察增加真实协调者坐标，避免把C冒充为某个worker。
- 定义：`CoordinatorStopScope`, `require_stop_scope`, `stop_scope_fields`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_drain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_tree.py`; `agent_py_agent/tests/test_rework_r9_gateway_drain_auxiliary.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_rework_r9_gateway_drain_auxiliary.py`

### `multiprocess_stop_tree`

- 职责（原模块声明）：LLM: P6复用原P2证明/屏障及C/W独立范围，不探测进程/改账；原W API保持，C明确无W编号，同步生产collector与范围拒绝测试。 / 可信来源/冻结期间禁止新执行/证据时效由原调用方保证；规范字节相等不是证据真实性的认证。 / 模块用途: 冻结并核对一次执行树停止观察。
- 定义：`TreeStopObservation`, `freeze_tree_observation`, `require_stopped_tree`, `_decode_observation`, `_require_tree_scope`, `executor_records`, `_require_p1_coverage`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_stop_drain.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`

### `multiprocess_subagent_execution`

- 职责（原模块声明）：LLM: 只消费独立原child request/P3/P5与canonical谱系；未知在原scope内观察既有账，退出后不重绑、不另建run/attempt/queue。 / 模块用途: 将实际child请求绑定到原C/W执行与工具平台门；孤儿业务由agent_core自身消费。
- 定义：`ChildRequestTransport`, `capture_child_transport`, `_require_child`, `_require_original_child_ref`, `child_worker_scope`, `_observe_original_child_unknown`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/agent_core/orchestration/dispatch/orphan_request.py`; `agent_py_agent/agent/agent_core/runner/session_pool.py`; `agent_py_agent/agent/agent_core/runner/worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_child_execution_binding.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`; `agent_py_agent/tests/test_mcp_lifecycle.py`; `agent_py_agent/tests/test_runner_thread_error_reports.py`; `agent_py_agent/tests/test_runner_thread_start_boundary.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_mcp_lifecycle.py`; `agent_py_agent/tests/test_runner_thread_error_reports.py`; `agent_py_agent/tests/test_gateway_background_orphan_request.py`; `agent_py_agent/tests/test_runner_thread_start_boundary.py`

### `multiprocess_supervisor_policy`

- 职责（原模块声明）：LLM: P7只运输原RuntimeDB阻塞投影与请求字节，不拥有unknown处置或恢复计数。 / 模块用途: 在原恢复链外增加失败关闭的只读门，任何输出都不授权重放工具。
- 定义：`RecoveryPolicyReadback`, `require_resume_policy`
- 处置：**deleted; responsibility moved**。唯一静态调用者仍是 multiprocess_processing_recovery；两项定义原样并入它，无转发层。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_cold_processing_recovery.py`; `agent_py_agent/tests/test_gateway_input_transport_codec.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`

### `multiprocess_takeover`

- 职责（原模块声明）：LLM: P2只读核实与结构化诊断分离，unknown仍优先；后续组合根绑定持锁C，不发信号/接P3。 / 模块用途: 输出五字段屏障和保留活成员的独立明细，不修改P1账本。
- 定义：`verify_takeover`, `_checked_scope`, `_collect_evidence`, `_bounded_observations`, `_takeover_budget`, `_stop_sampling`, `_observation_signature`, `_observe`, `_verify_record`, `_missing_record`, `_persist_evidence`, `_proof`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/legacy_mp_migration/sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_queue_transport_recovery.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_gateway_queue_transport_recovery.py`

### `multiprocess_takeover_darwin`

- 职责（原模块声明）：LLM: Darwin全用户sysctl枚举，逐PID核对不减；只把函数绑定移出循环，同源birth和消失拒绝不改。 / 模块用途: 获取macOS整树身份，运输消失最后事实但不放宽不一致拒绝。
- 定义：`darwin_process_rows`, `_append_snapshot_row`, `_sysctl_table`, `_read_sysctl_table`, `_sysctl_row`, `_sysctl_birth`, `_sysctl_query`, `_birth_reader`, `_kernel_birth`, `_darwin_row`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_takeover_probe.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_takeover_darwin_types`

- 职责（原模块声明）：LLM: 按Darwin SDK sys/proc.h、sys/sysctl.h、sys/vm.h声明公开LP64 ABI；字段偏移由ctypes对齐，不用字节切片魔数。 / 模块用途: 描述KERN_PROC_ALL完整记录；调用方须先检查LP64和sizeof，不能把未知ABI当完整表。 / LLM: sys/time.h的timeval，Darwin long秒与int微秒；保留尾部自然对齐。 / 类用途: 描述内核出生时间，转换时与P1秒:六位微秒一致。
- 定义：`Timeval`, `ProcUnion`, `Itimerval`, `ExternProc`, `Pcred`, `Ucred`, `Vmspace`, `Eproc`, `KinfoProc`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_darwin_waitid.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_darwin.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_takeover_ledger`

- 职责（原模块声明）：LLM: P2只读包装P1 canonical读接口，不建立登记源、不创建锁或删除未知记录；先冻结测试签名。 / 模块用途: 核对完整目录、旧代owner/角色/分区及明确执行者引用。
- 定义：`read_ledger_scope`, `_validate_scope`, `_read_namespaces`, `_read_partition`, `_read_record`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`

### `multiprocess_takeover_linux`

- 职责（原模块声明）：LLM: Linux组长Z不代表线程组退出；只读status的严格Threads/Pid/Tgid，外层stat复核身份后才接受。 / 模块用途: 保留组长原始Z状态，单独记录线程组是否仍可能执行，读取失败不冒充停止。 / LLM: pthread_exit后task可能不可遍历，使用任务允许的Threads替代；大于1保守live，不虚构线程stat。 / 函数用途: 读取Z组长的线程数，任何坏字段/权限失败都沿外层unknown路径。
- 定义：`thread_group_live`, `_status_field`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_takeover_probe.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：通过原调用者的集成测试及 count0 closure gate；没有声称独立单测覆盖

### `multiprocess_takeover_probe`

- 职责（原模块声明）：LLM: P2只读平台接口，无信号；消失携birth但仍失败，部分行只诊断；同源复核防PID复用。 / 模块用途: 枚举内核整树及校验部分观察，不拿消失关系或僵尸掩盖活成员。
- 定义：`UnstableProcessSnapshot`, `DisappearedProcessSnapshot`, `linux_process_row`, `observe_processes`, `_linux_visibility`, `_linux_rows`, `_read_linux_row`, `valid_process_table`, `_valid_disappearance`, `_valid_lost_row`, `_valid_row`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_takeover.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_darwin.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_cold_recovery_consumer.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_queue_transport_recovery.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`

### `multiprocess_takeover_recheck`

- 职责（原模块声明）：LLM: 此模块只给受信持锁C状态转换，不读取文件、不探测、不停止进程、不解除实际隔离。 / 模块用途: 定义隔离复核退避和新鲜回执的纯决策入口，接线由组合根负责。
- 定义：`_VerificationReceipt`, `_verified_result`, `_digest`, `_checked_time`, `quarantine_backoff`, `new_quarantine_state`, `_checked_scope`, `_checked_state`, `_checked_state_ids`, `quarantine_recheck`, `_fresh_verdict`, `_receipt_verdict`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_unresolved_source.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`

### `multiprocess_takeover_recovery`

- 职责（原模块声明）：LLM: 只在原C租约内运行只读P2复核，不调P3/启动写者；首次实际Thread在start前保原引用/ACK未知，P6在原业务结清后停止并门外join，未知不从空表/is_aliveFalse结清，同步原启动/关闭与P6测试。 / 模块用途: 串行观察隔离旧域，保留本代原复核线程给实际停止消费者。 / 前台保留原五次/一秒；后台提高尝试上限和接受期限，不抹失败或硬中断系统调用。
- 定义：`serialized_takeover`, `_guard_prepare`, `_guard_restart`, `_guard_launch`, `_guard_owner_launch`, `_guard_restamp`, `_guard_authority`, `_guard_admission`, `start_takeover_rechecks`, `_run_rechecks`, `_recheck_once`, `_next_delay`, `stop_takeover_rechecks`, `wait_owner_recovery`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`

### `multiprocess_takeover_runtime`

- 职责（原模块声明）：LLM: 完整原范围直接调P2；缺旧N责任仅原调度只读复核，未知永不解除，P3仍只原同步授权窗口；同步partition及recheck来源。 / 模块用途: 沿原租约核旧范围/fresh P2，持有v2关闭原字节及固定回执raw增量；增量不刷新P1/P3基线，未知不授执行权。
- 定义：`CoordinatorTakeover`, `_launch_current`, `_perform_request_restamp`, `_prepare_request_authority`, `prepare_coordinator_takeover`, `_current_receipt`, `_queue_target_recheck`, `_mark_target_stopped`, `_recheck_target`, `_begin_recheck`, `_observe_recheck`, `_finish_recheck`, `_accept_recheck`, `bind_current_worker_source`, `retire_idle_worker_source`, `_takeover_targets_only`, `_current_coordinator_target`, `collect_coordinator_executor_domains`, `original_drain_sources`, `_refresh_targets`, `_fixed_role_refresh_sources`, `_refresh_unresolved_sources`, `_pending_restriction_keys`, `_sources_unchanged`, `_log_source_refusal`, `_source_snapshot`, `_target_signature`, `_verify_worker_partition`, `_remember_restamped`, `bind_admission_takeover`, `_await_cold_index`, `_start_cold_rechecks`, `_run_cold_rechecks`, `spawn_coordinator_worker`, `_partition_blocked`, `_scope_keys`, `_authority_bytes`, `_request_target`, `_blocked`, `_processing_request_target`, `_current_receipt_sources`, `require_current_receipt_write`, `require_current_receipt_window`, `remember_current_receipt_write`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/control_plane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_background_discovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_local_control.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_commit.py`; `agent_py_agent/agent/gateway_parts/queued_steer_expiry.py`; `agent_py_agent/cli/gateway_coordinated_run.py`; `agent_py_agent/cli/gateway_restart_handover.py`; `agent_py_agent/cli/gateway_worker_runtime.py`; `agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_background_curator_transactions.py`; `agent_py_agent/tests/test_gateway_c_state_publication.py`; `agent_py_agent/tests/test_gateway_client_history_healthy20.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_cold_request_pump.py`; `agent_py_agent/tests/test_gateway_control_plane_construction.py`; `agent_py_agent/tests/test_gateway_control_plane_frozen_home.py`; `agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_gateway_coordinator_client_memory.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`; `agent_py_agent/tests/test_gateway_http_prepare_boundary.py`; `agent_py_agent/tests/test_gateway_input_owner_boundary.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_prepared_route_immutability.py`; `agent_py_agent/tests/test_gateway_processing_recovery_source.py`; `agent_py_agent/tests/test_gateway_queue_recovery.py`; `agent_py_agent/tests/test_gateway_queue_transport_recovery.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_old_control.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_source_refusal_diagnostics.py`; `agent_py_agent/tests/test_gateway_standalone_migration.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_worker_workspace_transport.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_guard.py`; `agent_py_agent/tests/test_nofollow_atomic_snapshot.py`; `agent_py_agent/tests/test_ready_control_receipt_source.py`; `agent_py_agent/tests/test_scale_curator_quota_owner_budget.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`; `agent_py_agent/tests/test_v13_r2_admin_coordinator.py`; `agent_py_agent/tests/test_v13_w4_capability_closeout_multiprocess.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_coordinator_client_history.py`; `agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`

### `multiprocess_takeover_sources`

- 职责（原模块声明）：LLM: P1不是停止证明；默认CountNone不变。仅原闭包旧W到固定当前P4的完整发表可typed整组最多三读，根/父链/C/P4/旧闭包跨轮固定，其它变化硬拒。 / 模块用途: 发现原旧域及角色缺口；v2完整关闭范围可解除角色拒绝，非空P1仍需fresh P2，缺旧历史不补封或授信。
- 定义：`require_role_coverage`, `_role_marker_names`, `_check_role_marker`, `_ClosedWorkerPublished`, `_role_directory_anchors`, `_closed_role_coverage`, `_closed_role_coverage_once`, `_require_stable_roles`, `_current_worker_role`, `_role_snapshot`, `_require_closed_markers`, `TakeoverTarget`, `takeover_namespace_state`, `indexed_role_coverage`, `_namespace_state`, `_same_namespace`, `discover_takeover_targets`, `uncovered_admission_owners`, `_read_admission_source`, `_discovery_registry`, `_discover_partition`, `_discover_record`, `_original_generation_count`, `_target`, `confirmed_evidence`, `_read_evidence`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_drain_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_sources.py`; `agent_py_agent/agent/gateway_parts/multiprocess_drain_trees.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_metadata.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_gateway_c_state_publication.py`; `agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_role_heartbeat_stability.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_gateway_role_marker_snapshot.py`; `agent_py_agent/tests/test_scale_native_executor_retirement.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_closed_worker_publication.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_successor.py`; `agent_py_agent/tests/test_dead_worker_role_closure.py`; `agent_py_agent/tests/test_gateway_role_marker_snapshot.py`; `agent_py_agent/tests/test_gateway_role_lifecycle_closure.py`

### `multiprocess_takeover_tree`

- 职责（原模块声明）：LLM: P2纯三态及独立诊断，失败表不完整、关系不证明排除；不发信号或把单PID退出当整树退出。 / 模块用途: 核实原执行树，保留不完整观察中的活成员和消失最后事实。 / LLM: 失败表保持unknown，只补同主机已观测成员及消失关系；诊断绝不变成完整枚举或排除证明，无I/O。 / 函数用途: 返回执行者三态，枚举失败也保留活成员及最后事实供定位。
- 定义：`evaluate_tree`, `_evaluate_tree`, `_incomplete_detail`, `_lost_relation`, `observed_live_members`, `_birth_known`, `_leader_status`, `_members`, `_instance`, `_tree_status`, `_detail`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_takeover.py`; `agent_py_agent/tests/test_mp05_generation_retirement.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_mp05_generation_retirement.py`

### `multiprocess_takeover_unresolved_source`

- 职责（原模块声明）：LLM: 旧代缺原count的P1仅能限制启动；来源是原nofollow文件/目录双读，绝不构造ledger、P2范围或死亡/执行权；同步partition/readback tests。 / 模块用途: 保存能够定位但不能证明完整旧范围的原责任，并沿唯一复核调度只读检查它。
- 定义：`UnresolvedExecutorSource`, `read_executor_source`, `capture_unresolved_source`, `unresolved_recheck_state`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_generation_retirement.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_sources.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_cold_recovery_optional_index.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_coordinator_global_index_compact.py`; `agent_py_agent/tests/test_coordinator_post_write_revision_recovery.py`; `agent_py_agent/tests/test_coordinator_private_credential_transactions.py`

### `multiprocess_tool_global_write`

- 职责（原模块声明）：LLM: 只消费原CoordinatorExecution和唯一ToolExecutor实际领取；原T/P3/P5/C/P2/lease末次核后才同步写，不造操作账或W身份。 / 模块用途: 把本地主体工具的全局叶子接到已有短提交门，长模型/审批/handler仍在门外。
- 定义：`CoordinatorToolWriteFactory`, `CoordinatorToolWriteSource`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_coordinator_execution.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_background_claim_transport_reference.py`; `agent_py_agent/tests/test_background_execution_ports.py`; `agent_py_agent/tests/test_coordinator_approval_decision.py`; `agent_py_agent/tests/test_curator_original_transport.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`

### `multiprocess_unroutable_closeout`

- 职责（原模块声明）：LLM: 只在真实C原T/租约末门收口无P3旧请求，保留原route/绑定/指纹；Count0只认实际生命周期锁，不授模型、工具或新执行权。 / 模块用途: 终结拓扑切换遗留请求并修复原响应、输入回执和会话占用。
- 定义：`unroutable_reason`, `resume_unroutable_closeout`, `standalone_lifecycle_held_for`, `unroutable_closeout_response`, `unroutable_executor_dead`, `UnroutableCloseoutSource`, `_closeout_source`, `_committed_closeout`, `close_out_unroutable_pending_locked`, `_settle_queued_input`, `project_unroutable_closeout`, `_project_unroutable_closeout`, `_require_pending_metadata`, `_finish_closeout_claim`, `_finish_closeout_projection`, `settle_unroutable_terminal`, `close_out_unroutable_processing`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/legacy_mp_migration/requests.py`; `agent_py_agent/agent/gateway_parts/multiprocess_admission.py`; `agent_py_agent/agent/gateway_parts/multiprocess_processing_recovery.py`; `agent_py_agent/agent/gateway_parts/request_transactions.py`; `agent_py_agent/agent/gateway_parts/request_worker.py`; `agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_mp00_lock_order.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=loaded, D3=not_seen, D4=not_seen, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_deterministic_simulation_replay.py`; `agent_py_agent/tests/test_mp02_switchover_self_heal.py`; `agent_py_agent/tests/test_mp00_lock_order.py`

### `multiprocess_worker`

- 职责（原模块声明）：LLM: 模式必须先由内部入口冻结；配置默认路径直接用common纯实现，home仍复用唯一解析及CLI05快照；原P5链与退出未知不变。 / 模块用途: 维持worker心跳和请求消费，停止接单后保留未结清执行责任。
- 定义：`run_worker`, `wait_worker_registration`, `require_worker_registration`, `create_request_pump`, `_worker_execution_loop`, `_refresh_due_heartbeat`, `_publish_heartbeat`, `_arm_parent_exit`, `_install_stop_handlers`, `_restore_stop_handlers`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/cli/gateway_worker.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_workspace_transport.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_worker_terminal_heartbeat.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`

### `multiprocess_worker_agent`

- 职责（原模块声明）：LLM: 只组合原冻结启动合同与原owner池；不建立模式、认领请求或安装执行登记作用域。 / 模块用途: 给真实worker入口提供惰性SimpleAgent池及构造前检查点。
- 定义：`require_worker_config`, `require_worker_workspace`, `require_worker_construction`, `create_worker_pool`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/agent_core/runner/worker.py`; `agent_py_agent/agent/core.py`; `agent_py_agent/agent/gateway_parts/managed_owner_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/agent/owner_scoped_pool.py`; `agent_py_agent/agent/user_space/worker_home.py`; `agent_py_agent/tests/test_coordinator_control_forward.py`; `agent_py_agent/tests/test_gateway_active_input_worker.py`; `agent_py_agent/tests/test_gateway_global_admin_write_boundaries.py`; `agent_py_agent/tests/test_gateway_global_write_current_consumers.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_workspace_transport.py`; `agent_py_agent/tests/test_home_initializer_permissions.py`; `agent_py_agent/tests/test_iface_w5_subagent_worker_type.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`; `agent_py_agent/tests/test_public_agent_home_initialization_order.py`; `agent_py_agent/tests/test_res_w11_owner_home_rebuild.py`; `agent_py_agent/tests/test_rework_r9_managed_dispatch_runtime.py`; `agent_py_agent/tests/test_runtime_input_deadline_bridge.py`
- 动态观察：D0=loaded, D1=loaded, D2=not_seen, D3=loaded, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_agent.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_forward.py`; `agent_py_agent/tests/test_im_delegation_single_speaker.py`; `agent_py_agent/tests/test_multiprocess_process_completion.py`

### `multiprocess_worker_commit`

- 职责（原模块声明）：LLM: 仅C内使用原Popen、P0出生、P4登记、原controller及lease；peer_pid必须由后续内核传输适配器提供。 / 模块用途: 为真实W记录原在途责任；仅本次native调用可取得未进入/无pin的明确拒绝事实，不授JSON权限或重试。
- 定义：`WorkerCommitRejected`, `WorkerCommitNotStarted`, `WorkerCommitIssuer`, `_process_identity`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/logging.py`; `agent_py_agent/agent/gateway_parts/multiprocess_commit_channel.py`; `agent_py_agent/agent/gateway_parts/multiprocess_commit_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_coordinator_executor_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_active_index.py`; `agent_py_agent/agent/gateway_parts/multiprocess_executor_original_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_command_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_commit_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_control_receive.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_coordinator_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_process.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_executor_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_authority.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_client.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_lane.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_observation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_session.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_host_start.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_domain.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_linux_reference.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_materialize.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_source.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_refusal_terminal.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_runtime.py`; `agent_py_agent/agent/gateway_parts/multiprocess_native_short_operation.py`; `agent_py_agent/agent/gateway_parts/multiprocess_takeover_runtime.py`; `agent_py_agent/tests/test_gateway_coordinator_source_closure.py`; `agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_guard.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`
- 动态观察：D0=loaded, D1=loaded, D2=loaded, D3=loaded, D4=loaded, D5=loaded。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_refusal_lease_pin_ownership.py`; `agent_py_agent/tests/test_gateway_worker_runtime.py`; `agent_py_agent/tests/test_gateway_safe_native_diagnostics.py`; `agent_py_agent/tests/test_rework_v14_admission_io_recovery.py`

### `multiprocess_worker_dispatch`

- 职责（原模块声明）：LLM: 原审批纯退休未结只同对象传播和核收；普通同码不升级，物理未知优先，不以清理完成签发终态或释放资源。 / LLM: W只消费C已准入pending；原池绑定出生观察，保actualThread和submit故障；坏时间须结构化拒绝，不从_threads清表猜退出，不重排/释放预留，同步原槽数和P5消费者测试。 / 模块用途: 把实际worker队列扫描、原claim和业务执行接在一起；异常只记claim摘要，仍保留未结执行。
- 定义：`WorkerRequestPump`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/agent/gateway_parts/multiprocess_worker_drain.py`; `agent_py_agent/tests/test_approval_retirement_finish_transport.py`; `agent_py_agent/tests/test_approval_retirement_lifecycle.py`; `agent_py_agent/tests/test_approval_retirement_worker_future.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_dispatch.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_drain_birth.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_worker_control_pins.py`; `agent_py_agent/tests/test_mp01_worker_poll_cpu.py`; `agent_py_agent/tests/test_mp03_worker_survival.py`; `agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_native_first_materialize_refusal.py`; `agent_py_agent/tests/test_native_transport_diagnostic.py`; `agent_py_agent/tests/test_worker_terminal_heartbeat.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_native_commit_rejection_receipt.py`; `agent_py_agent/tests/test_gateway_worker_component.py`; `agent_py_agent/tests/test_gateway_control_poller_component.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_entry.py`; `agent_py_agent/tests/test_gateway_multiprocess_control_worker.py`

### `multiprocess_worker_drain`

- 职责（原模块声明）：LLM: W只能结清自己真实shutdown/join和原空P1范围；任何P1树必须由原C/kernel P6证明，W不凭cache/心跳/空future假fulltree；Count0不调用，同步原current drain/held负例。 / 模块用途: 给已停止认领的原worker核进程内资源和唯一持久责任。
- 定义：`worker_pump_drain_assessment`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/gateway_parts/multiprocess_worker.py`; `agent_py_agent/tests/test_gateway_multiprocess_worker_drain_birth.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_multiprocess_worker_drain_birth.py`

### `multiprocess_worker_resources`

- 职责（原模块声明）：LLM: W复用scheduler索引业务实现，但绝不创建/迁移共享DB；正常带锁业务更新仍沿原类。 / 模块用途: 将共享scheduler数据库的existing-only初始化接到真实消费点。
- 定义：`WorkerSchedulerDueIndex`
- 处置：**retained-in-place**。仍有静态调用者；本轮没有把有限动态未见当作删除证明。
- 静态来源/调用者：`agent_py_agent/agent/core.py`; `agent_py_agent/tests/test_gateway_global_write_current_consumers.py`
- 动态观察：D0=not_seen, D1=not_seen, D2=not_seen, D3=not_seen, D4=not_seen, D5=not_seen。`not_seen` 只说明这些输入未导入，不证明不可达。
- 验证入口：`agent_py_agent/tests/test_gateway_global_write_current_consumers.py`
