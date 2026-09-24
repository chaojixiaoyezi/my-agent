# 18-A 决策线吸收 main 交接

## 基本信息

- workstream：决策模型 P1—P5 第 18 项的 18-A（本分支吸收 main 并调和两套 Compact 来源设计）
- branch：`claude/decision-merge-main`，由 `codex/decision-model-integration`（`6b555e1ed`）出发
- worktree：`../my-agent-worktrees/decision-merge-main`
- owner：决策线接手代理
- date：2026-09-23
- 已吸收的 main：`66a598cf3` → `0d02bb272` → `7c467a4d3` → `3b72c4d7b`
- 合入方式：由主线 owner 审阅后合进 main；本线不直接推 main，也不部署主线环境

## 本线目标

决策线和第 8 步各自修了"旧 Compact 用裸 call_id 误隐藏新工具结果"，两套来源身份设计互相冲突：主线是 v2 加三元 ToolCallRef，本线是 v3 加四元 refs。本线要吸收 main，把来源身份统一成一套，同时保留第 8 步的结构和主线独有的行为，让合并版可以交给主线 owner 合入。

## 实际完成

- **来源身份**：只保留 `conversation/compact_tool_identity.py` 的四元 refs 和 `conversation_compact_checkpoint.v3`，删除 `tooling/call_ref.py`，不保留转发。
- **保留的主线行为**：
  - 未知来源保持可见；
  - 全部未知时 `compacted=False`，带 `source_resolution`、`uncertain_call_count`；
  - `runtime_facts` 与 `tool_process` 耐久索引不变；
  - 第 8 步的 closeout、segment_planning、model_turn、请求周期不变；
  - 持久 CAS 成功后投影失败不回滚。
- **模型轮参数显式交回**：
  - `ModelTurnRequest(prompt, response, params)` 由实际产出响应的那次尝试交回；
  - `_model_turn_or_retry` 返回具名结果，`execute_tool_loop` 显式写 `params = turn.params`，并返回 `ToolLoopRunResult`；
  - 不使用回调或 nonlocal 暗改参数。
- **原生 Compact 提交**：`_commit_native_ir_generation` 返回 `_NativeCompactCommit`，同范围视图刷新挪到回滚边界之外。
- **合并后修复**：
  - 决策线在接手前就有的 9 项失败清零：1 个产品缺陷、1 个合同缺口、5 项过时替身，架构守卫另修；
  - review 发现 1 单独提交。
- **文档**：DESIGN_LEDGER 顶部摘要、`docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md` 合并节（含兼容与回滚边界）、模块文档、Goal、ROADMAP、TESTS。

## 改动文件

生产代码中与合并直接相关的入口：

- `agent_core/_tool_loop_service.py`：
  - 以 main 版为基础，重新应用本线改动；
  - `next_tool_loop_model_response` 在每次瞬断尝试内选模；
  - `request_owns_compact` 领取门；
  - 提交结果与提交后刷新。
- `agent_core/tool_loop/model_turn.py`：只新增 `ModelTurnRequest` 并改回调签名，仍不持有 Agent、不 import 业务层。
- `agent_core/runtime/loop_support.py`：从 `ToolLoopRunResult` 显式取回参数。
- 采用本线 v3 版本的文件：
  - `conversation/active_turn_compact.py`，并补 uncertain 字段；
  - `conversation/compact_checkpoint.py`；
  - `conversation/live_tool_compact.py`，编号不一致时改抛带类型的错误；
  - `memory_archive/compact_tool_output_refs.py`，按四元去重；
  - `memory_archive/tool_output_externalizer.py`。
- 合并后修复：
  - `gateway_parts/request_execution.py`：无会话来源时不安装首次恢复宿主；
  - `contracts/error_taxonomy.py`：登记 `INPUT_MEDIA_INVALID`；
  - `agent_core/compact_request_recovery.py`：来源身份无法证明时报 `COMPACT_TOOL_COVERAGE_UNKNOWN`；
  - `conversation/message_replay.py`：去掉可变位置参数。
- 删除：`tooling/call_ref.py`。

## 测试命令和结果

```bash
python3 ~/.my-agent/decision-evidence/tools/shard_pytest.py . --out <dir> --shards 8   # 全量
python3 -m pytest <受影响文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

结果：

- 首次合并后全量：20,623 passed，29 failed。其中 24 项在决策或 main 基线已失败；新增的 5 项已修复。
- 最终全量（`11a3412b5`，8 分片并行）：
  - 20,592 passed，65 failed。
  - 65 项全部是本地 HTTP、流式或期限类用例超时（TimeoutError、URLError timed out、连接重置、deadline）。当时机器负载约 5—6，主线 owner 也在同机跑套件。
  - 把这 65 项单进程重跑：65 passed。
  - 结论：没有确定的代码失败。接手前决策基线 10 项、main 基线 15 项的失败都已清零。
- 新增的三条主线 owner 要求的测试，以及发现 1 的用例，都做了变异或修复前后对照，详见 TESTS.md 的 18-A 一节。
- 未运行真实模型或 TUI；线上 CI 未作为验收来源。

## 原场景 → 新测试对照（test_compact_tool_call_refs.py）

| 主线 `66a598cf3` 原用例 | 合并版对应用例 | 说明 |
|---|---|---|
| `test_exact_source_does_not_hide_new_same_number[run,attempt,request_id]` | 同名 `[run,attempt,turn]` | 身份维度换成四元；request_id 不属于身份 |
| `test_one_checkpoint_can_hide_mixed_sources_and_retain_same_bare_id` | 同名 | 四元 refs |
| `test_legacy_bare_source_is_uncertain_and_cannot_hide_any_domain` | `test_legacy_bare_source_cannot_hide_any_domain` | 逐条 `compact_source_resolution` 标注没有消费者，不移植；隐藏断言保留；全未知时的 uncertain 结果见下方 `old_unscoped` 一行 |
| `test_record_without_origin_is_kept_even_when_scoped_string_matches` | 同名 | 未知来源保持可见 |
| `test_orphan_ref_never_hides_current_call` | 同名 | |
| `test_carried_boundary_selects_records_not_bare_ids` | 同名 | |
| `test_carried_boundary_allows_source_and_tail_to_share_bare_id` | 同名 | |
| `test_externalized_and_inline_indexes_preserve_exact_attempts[0,10000]` | 同名 | 索引保留 attempt/turn |
| `test_legacy_carried_rows_cannot_dedupe_by_scoped_string` | 同名 | 只按完整四元去重 |
| `test_orphan_with_same_legacy_hash_inputs_cannot_replace_committed_refs` | `test_orphan_with_same_bare_inputs_cannot_replace_committed_refs` | v3 编号对 refs 做内容寻址，不再有 legacy 哈希 |
| `test_orphan_cannot_replace_committed_unknown_tail_ids` | `test_orphan_cannot_replace_committed_retained_boundary` | 保留区 refs 纳入 v3 编号 |
| `test_commit_keeps_duplicate_bare_ids_counts_and_submitter_semantics` | 同名 | |
| `test_invalid_exact_boundary_never_writes_candidate[4 种]` | 同名，参数带期望错误码 | 重叠、重复、不一致报 `COMPACT_TOOL_BOUNDARY_INVALID`，缺身份报 `COMPACT_TOOL_COVERAGE_UNKNOWN` |
| `test_old_unscoped_large_archive_is_explicitly_uncompactable` | 同名 | 断言 `compacted is False`、`source_resolution == "uncertain"`、`uncertain_call_count == 1` |
| `test_mixed_known_and_unknown_archive_only_replaces_known_source` | 同名 | 只压已知来源 |
| `test_malformed_ref_never_acquires_filter_authority[field,value]` | `test_malformed_legacy_ref_never_acquires_filter_authority` 与 `test_tampered_v3_ref_fails_closed_instead_of_hiding` | 拆成两条：畸形 v1 引用按 legacy 处理、不隐藏；被篡改的 v3 行内容寻址校验失败，拒绝读取 |
| `test_legacy_checkpoint_id_inputs_keep_original_digest` | `test_main_66a598cf3_v2_ref_rows_read_as_legacy_without_hiding_or_losing_rows` | 旧摘要函数已不存在；改用 `66a598cf3` writer 实际写出的 v2 行做回归，验证可读、按 legacy 处理、不报错、不丢行 |
| `test_native_plan_freezes_call_origin_before_summary_and_commits_exact_tail` | 同名 | 四元 refs |

原 19 个场景全部有对应断言；新增 2 条（v2 实际行回归、篡改 v3 拒读）。

## 受影响测试文件清单（相对 main `3b72c4d7b`）

**main 已有、合并版内容与 main 不同，且在合并中合成或改写过的 22 个：**

- 模型轮具名结果带来的机械改写：
  - `execute_tool_loop` 返回 `ToolLoopRunResult`：`test_provider_timeout_acceptance.py`、`test_provider_timeout_continuation.py`、`test_provider_timeout_resume_narrowing.py`、`test_provider_timeout_resume_probe.py`、`test_runtime_guidance.py`、`test_subagent_runtime_guards.py`、`test_timeout_recovery_delivery.py`、`test_tools/test_tool_loop.py`；
  - 替身返回 `_ModelTurnOutcome`：`test_thread_interrupt.py`；
  - `next_tool_loop_model_response` 返回 `ModelTurnRequest`：`test_tool_context_ptl_retry.py`。
- 重写或新增用例：
  - `test_compact_tool_call_refs.py`：四元重写，见上表；
  - `test_tool_loop_model_turn.py`：回调带参数，新增瞬断重选模用例。
- 冲突合成：
  - `test_native_tool_ir_compact_and_orphan_sweep.py`：夹具取本线版，main 新用例改读 v3 `source_tool_refs`；新增提交后刷新失败用例；b4ffb3475 三用例与 main 逐字一致；
  - `test_gateway_conversation_compact.py`：main 的非文本用例换到本线 `conversation_compact_provider_source` 接缝，断言不变；
  - `test_tui_input.py`：main 的 `transcript_area` 与本线的 `media_importing=False` 合并；
  - 自动合成（main 新用例加本线小改）：`test_agent_goals.py`、`test_background_main_agent_runtime.py`、`test_gateway_chat_conversation_context.py`、`test_resolve_capability_requests_tool.py`。
- 合并后修过时替身：`test_chat_client_context.py`、`test_gateway_request_runtime_errors.py`、`test_memory_runtime_compact_auto_continuation.py`。

**main 已有、合并版直接取决策线版本的 46 个**（都是决策线原有改动）：
`test_audit_activation.py`、`test_audit_source_worker.py`、`test_backends.py`、`test_background_context_runtime_errors.py`、`test_background_main_agent_cli.py`、`test_background_owner_delivery_commit.py`、`test_compact_request_budget.py`、`test_compact_semantic_summary.py`、`test_compact_text_source.py`*、`test_context_pressure_native_trigger.py`、`test_conversation_message_scan.py`*、`test_conversation_store.py`、`test_gateway_conversation_control.py`、`test_llm_admission.py`、`test_llm_hot_path_admission.py`、`test_memory_condense_v2.py`、`test_memory_first_loop.py`、`test_memory_recall_v2.py`、`test_memory_routing_context.py`、`test_model_call_ledger.py`、`test_model_profiles.py`、`test_model_provider_management.py`、`test_native_tool_use_ir_messages_flow.py`、`test_orchestration_cancel_subagents_tool.py`、`test_orchestration_create_subagents_output_refs.py`、`test_orchestration_create_subagents_tool.py`、`test_orchestration_create_subagents_tool_workspace.py`、`test_orchestration_tools.py`、`test_provider_request_scope.py`、`test_r223_audit_regressions.py`、`test_runtime_context_pressure.py`、`test_runtime_module_boundaries.py`、`test_shared_model_catalog.py`、`test_stream_timeout_contract.py`、`test_subagent_effective_runtime_context.py`、`test_subagent_manager_core.py`、`test_subagent_runtime_compact.py`*、`test_subagent_skill_inheritance.py`、`test_thread_model_selection.py`、`test_timeout_budget_locked.py`、`test_timeout_gate2_stages.py`、`test_tool_model_generation.py`、`test_tui_model_metrics.py`、`test_tui_worker_paths.py`、`test_user_config_capability.py`、`test_watch_audit_guarantee.py`。

标 * 的 3 个在冲突中取决策版。已逐名核对，main 的用例名全部保留：
- `test_compact_text_source.py` 与 `test_conversation_message_scan.py`：本线原本就是从 main 移植的超集；
- `test_subagent_runtime_compact.py`：main 增补的夹具身份，本线已改由真实工具循环产生。

**决策线新增、main 上没有的 82 个**（其中 4 个在本轮修改过：`test_applied_compact_context.py`、`test_decision_capability_consumer.py`、`test_compact_native_ir_recovery.py`、`test_mixed_compact_contract.py`）：
`fixtures/decision/jev_capability_rounding.json`、`test_active_turn_compact_projection.py`、`test_applied_compact_context.py`、`test_background_capability_compact.py`、`test_background_compact_recovery.py`、`test_background_prepared_context.py`、`test_background_scoped_compact.py`、`test_bounded_call.py`、`test_compact_active_projection.py`、`test_compact_checkpoint_stream.py`、`test_compact_media_recovery.py`、`test_compact_message_source.py`、`test_compact_native_ir_recovery.py`、`test_compact_output_reserve.py`、`test_compact_request_projection.py`、`test_compact_retained_history.py`、`test_compact_scoped_checkpoint.py`、`test_compact_scoped_transcript.py`、`test_compact_source_lifetime.py`、`test_compact_tool_partition.py`、`test_compact_tool_provenance.py`、`test_compact_tool_source.py`、`test_compact_transcript_media_partition.py`、`test_conversation_message_selection.py`、`test_decision_call_resources.py`、`test_decision_capability_consumer.py`、`test_decision_capability_http.py`、`test_decision_curator.py`、`test_decision_curator_relation.py`、`test_decision_experiment_authorization.py`、`test_decision_external_material_order.py`、`test_decision_gateway_transport.py`、`test_decision_model_call.py`、`test_decision_model_operations.py`、`test_decision_model_profiles.py`、`test_decision_owner_scope.py`、`test_decision_planning.py`、`test_decision_pre_recall.py`、`test_decision_protocol.py`、`test_decision_recall.py`、`test_decision_service.py`、`test_decision_service_http.py`、`test_decision_settings.py`、`test_decision_settings_notifications.py`、`test_decision_settings_scope.py`、`test_decision_skill_projection.py`、`test_decision_skill_tool_settings.py`、`test_decision_subagent.py`、`test_decision_usage_metrics.py`、`test_external_material_order_integration.py`、`test_gateway_capability_compact.py`、`test_gateway_child_compact_scope_application.py`、`test_gateway_compact_deferred_source.py`、`test_gateway_compact_recovery.py`、`test_gateway_compact_recovery_continuation.py`、`test_gateway_model_adoption.py`、`test_gateway_model_observation.py`、`test_gateway_strict_request.py`、`test_input_media.py`、`test_mixed_compact_contract.py`、`test_mixed_compact_recovery.py`、`test_model_call_input_budget.py`、`test_model_call_ledger_partitions.py`、`test_model_profile_catalog_generation.py`、`test_model_scope_dependencies.py`、`test_model_selection_isolation.py`、`test_model_turn_identity.py`、`test_native_compact_carry.py`、`test_native_history_projection_memory.py`、`test_native_user_input_identity.py`、`test_request_content_capacity.py`、`test_subagent_capability_compact.py`、`test_subagent_compact_recovery.py`、`test_subagent_compact_recovery_continuation.py`、`test_subagent_first_request_selection.py`、`test_thread_model_selection_revision.py`、`test_tool_presentation_projection.py`、`test_tool_request_projection.py`、`test_tui_decision_menu.py`、`test_typesafe_decision.py`、`test_user_config_decision_operations.py`、`test_user_config_owner_scope.py`。

没有删除任何 main 测试文件。

## 影响范围

- Compact 工具来源身份、checkpoint 读写和活动归档压缩：Gateway、子代理、后台三个宿主都受影响。
- 模型轮与工具循环的参数传递；首请求选模和完整恢复宿主的安全点。
- Gateway 首次恢复宿主的安装条件；无会话来源的 ask 不再失败。

## 需要主线重点复查

- 数据兼容：`66a598cf3` 写出的 v2 行按 legacy 读取。合并版部署前做过运行中压缩的旧调用会重新进入模型上下文，只多占上下文，不丢失、不误隐藏。
- 回滚边界：旧版运行时读不了 v3。回滚必须把运行时和数据成对核对，并保留新账。
- `request_owns_compact` 领取时跳过共享预算回收，改由宿主在发送前收进窗口；新测试覆盖了自动和强制两种模式。
- 合入 main 后，由主线 owner 在合并版上重跑真实 TUI 矩阵：长任务 Compact 前后事实读回、跨请求同号、中断恢复。

## 需要其他线协调

- tui-scalability 由主线 owner 整合进 main。本线已整合其媒体提交 `3adb61904`（本线 `319004926`），`input_media.py`、`responses_wire.py` 逐字节相同。与本线重叠的函数已发给主线 owner；再次吸收 main 时，TUI 性能与资源提交按 main 原样保留，本线独有的函数取决策版。

## 剩余风险

- 旧的无身份大历史不能安全 Compact：强制恢复报 `COMPACT_TOOL_COVERAGE_UNKNOWN`。这是兼容限制，不算已兼容。
- 真实模型与 TUI 未运行；合并版的线上 CI 未运行。
- Gateway overflow 入口在确实没有可压来源时，仍提示"当前会话无法继续压缩，请稍后重试"；重试同样无效，这一处另行处理，不在本次合并内。

## 后续建议

- 主线 owner 审阅后合入 main，并在合并版上跑真实 TUI 矩阵。
- 本线随后在合并基线上做 12.4 第二片（2a/2b），两个共享解析函数动手前先与主线 owner 确认。然后推进 13—17，最后做 18 的整体集成。
