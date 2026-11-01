# MEGA 唯一派工与集成状态台账

更新日期：2026-11-01。状态观察点：`a1e23366c9ab57bb16041cf1431498b0488caf71`，不是滚动 main，也不是部署版本。

本轮业务空间 v1.2、Cache v2、27 个 Issue 合并为同一套 4 波/17 片计划。此页是当前人类/agent 导航；写权限仍以本轮冻结 `workstreams.json` 的结构化白名单为准，不解析本页 prose 赋权或决策。`STATUS.md`、`docs/ROADMAP.md`、`docs/COMPLETED.md`、`docs/WORKSTREAMS.md` 只指向本页，不再各维护一套活跃分片清单。

## 1. 候选身份与观察边界

| 项目 | 实际观察 |
| --- | --- |
| 设计基线 | `d071df60eda3f90d6b6da6e61dd193a80adbbecc` |
| Wave0 冻结 | `bdaa38c93f3e16f1d8477782096788121137505f` |
| Wave1 冻结 | `0b05a7cdd0ee2d422d6f2388c804eaaec6d98962` |
| Wave2 / W16 authored_base | `a1e23366c9ab57bb16041cf1431498b0488caf71` |
| Wave2 tree | `1a3216fcc236b0adde22ee1d645614b8d8cff79f` |
| 当前权限来源的文件 SHA256（W16-fix修订） | `4d9c7c49d3764d6e7adad989c5008798f35d35c373811df02dc6c67747ca27f6` |
| 最终组合 candidate | 尚未提供；不能把 Wave2 或本片提交冒充最终候选 |
| W16 自身 head | 由提交后的仓外 `handoff.json`/`review.json` 记录，本文不循环写入自身未来 SHA |

**当前结论：W16 文档/投影及明确批准的待整改基线收口，不是产品或发布通过。** 原生成器 `--write` 和 `--check` 成功（1286项）。修订前原守卫11节点10P/1F/0E/skip，原因是下表10个上游定义缺说明/量纲；总控审阅证据并通过 `plan-w16-fix.md` 正式追加fixture写权、明确接受仅这10项欠账后，原11节点全部通过、0失败/错误/跳过。源处元数据尚未整改；新提交的统一机器门以仓外最新 `review.json` 为准，旧失败另存 `evidence/before-w16-fix/`，不据此虚构生产漏洞或修复。

W15 观察时只有 authored_base 开工记录、无交付 head/review；本片同样尚未进入总控最终组合。最终矩阵、全仓、封包、双审均为后续冻结阶段，当前 NOT_RUN。

## 2. 逐片提交与局部门复核

来源：仓外各片 `handoff.json`、`review.json` 与真实 Git ref。表中局部门只绑定 `review_head`，**不能继承为 Wave2 或新 candidate 的 PASS**。T/F/E/S 为原报告的 tests/failures/errors/skipped 原数，不将 skip 计通过。

| 分片 | 波次 | authored_base | 实际分支尖端 | 集成事实 / 历史局部门 |
| --- | --- | --- | --- | --- |
| W0 | 0 | `d071df60eda3f90d6b6da6e61dd193a80adbbecc` | `bdaa38c93f3e16f1d8477782096788121137505f` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=214/F=0/E=0/S=0 |
| W1 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `e9c1d8dd8bc631e63619d268c3f9ef15c00d6d61` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=66/F=0/E=0/S=0 |
| W2 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `f7ff6976bc34db4c0b502b211527a43fec0c9ea3` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=348/F=0/E=0/S=0 |
| W3 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `d9c6d0921735ed4bd2676534d1b47b52332e0978` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=167/F=0/E=0/S=0 |
| W4 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `921cfaa0a8f367273e48406ff388a373400e0461` | 祖先在 Wave2; AUTOMATED_GATES_PASSED_WITH_SKIPS; T=185/F=0/E=0/S=1 |
| W5 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `9960a1d483e5e7f2b752b9d95cd8b0489ce83ee6` | 祖先在 Wave2; AUTOMATED_GATES_PASSED_WITH_SKIPS; T=437/F=0/E=0/S=4 |
| W6 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `31248a67c98c2b976a3561e1395c523943d6fd7d` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=160/F=0/E=0/S=0 |
| W7 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `ad12a1e46ba01b8eb7762d25abeb3dfb8445b4b8` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=269/F=0/E=0/S=0 |
| W8 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `c0f2f3101b9eea3b8c8e452ec3af3e369b2f55fa` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=204/F=0/E=0/S=0 |
| W9 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `778785f16177dd3112d5a199d0b1f8d34d687ed2` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=89/F=0/E=0/S=0 |
| W10 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `5773d3a391d8abb5f53ae39421ef60d3a3e60ec7` | 非祖先，但全部 owned 路径后像与 Wave2 一致; AUTOMATED_GATES_PASSED_WITH_SKIPS; T=139/F=0/E=0/S=20 |
| W11 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `aae6f00822fd7abc96e9bc5c25fb72518c9f9ed3` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=147/F=0/E=0/S=0 |
| W12 | 1 | `bdaa38c93f3e16f1d8477782096788121137505f` | `5a560c5a9e176f1b117318b57766a44dfbf7068b` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=24/F=0/E=0/S=0 |
| W13 | 2 | `0b05a7cdd0ee2d422d6f2388c804eaaec6d98962` | `d647f2c37cc2edc3fdd12db5e69d6b2da030cbb4` | 祖先在 Wave2; AUTOMATED_GATES_FAILED; T=465/F=1/E=0/S=0 |
| W14 | 2 | `0b05a7cdd0ee2d422d6f2388c804eaaec6d98962` | `8db75eda70d37a70ffbf47fb33850b6cb4f171e0` | 祖先在 Wave2; AUTOMATED_GATES_PASSED; T=37/F=0/E=0/S=0 |
| W15 | 3 | `a1e23366c9ab57bb16041cf1431498b0488caf71` | 未交付；不能把开工 base 当 head | NOT_RUN：未收单 |
| W16 | 3 | `a1e23366c9ab57bb16041cf1431498b0488caf71` | 修订提交后见仓外交接 | 生成器一致；W16-fix接受10项欠账，原守卫11P/0F/E/skip；统一review绑定新head |

### 必须保留的身份差异和旧失败

- W2：handoff 的 head 为 `83db9f7ebb31587715f5e29eb1c5699da8517d89`，但实际尖端及最新 review 为 `f7ff6976bc34db4c0b502b211527a43fec0c9ea3`。最新 review 348 节点、F/E/S 均 0；交接的旧状态/旧 head 不被自动当作事实更新。
- W10：最新报告 `5773d3a391d8abb5f53ae39421ef60d3a3e60ec7` 不在 Wave2 祖先链，但本片全部生产/测试白名单路径与 Wave2 `git diff` 为空。这是后像一致，不是“代码未集成”的证据；准确挑入/重写来源由总控记账。旧矩阵 139 节点、20 skipped，不能当原生 macOS/Linux 已验。
- W13：`d647f2c37cc2edc3fdd12db5e69d6b2da030cbb4` 的历史 review 为 AUTOMATED_GATES_FAILED，465 节点中 1 失败。原失败为 `test_plugin_host_gateway_composition.py::test_production_gateway_wires_all_actions` 的 available 集合断言；本轮未在最终组合上重跑，不能把旧失败直接断言为当前生产缺陷，也不能写成 PASS。保留原日志，不放宽断言。
- W4/W5 的旧报告分别有 1/4 skipped；先按原原因补平台证据，不能把它们与普通通过混算。
- 所有已交付片的实际 branch head 相对其 authored_base 的写集均未越过本片白名单；这不证明跨片行为闭环。

## 3. 本轮唯一分工投影

各波取生产+测试写集的实际交集均为 0。read_only_tests 不是写集。跨波共享文件按冻结的 ownership_transfers 接力，不允许并行回写。

| 分片 | 席位 | 职责 | 覆盖 | 依赖 |
| --- | --- | --- | --- | --- |
| W0 | c1 [SOL] | 共享合同、配置与动作目录冻结 | Cache-W0, Cache-W1, F0, F1, F3, F4, F6 |  |
| W1 | c1 [SOL] | 业务空间、界面绑定、资产引用与访问治理 | F2, F8 | W0 |
| W2 | c2 [SOL] | 后台主会话、任务持久化、会话索引与唤醒衔接 | F3, #15, #22, #25, #36 | W0 |
| W3 | c3 [SOL] | Workflow范围、异步节点、脚本输入与交付恢复 | F4, F8, #22, #23 | W0 |
| W4 | c4 [SOL] | 任务定位、子代理健康检查与诊断CLI | #14, #20, #21 | W0 |
| W5 | c5 [SOL] | 运行事实分源快照、Compact与完成事件桥接 | Cache-W1, Cache-W2, F6, #25, #29 | W0 |
| W6 | c6 [SOL] | 挂载能力优先、工具投影与可信Skill学习 | F6, #24, #25, #26, #27, #30 | W0 |
| W7 | c7 [SOL] | 用量归一、调用账本、读预算与离线缓存分析 | Cache-W3, Cache-W4, #16, #17 | W0 |
| W8 | c8 [SOL] | 存储争用测量、治理配置缓存与限流桶维护 | F1, #18, #31, #37 | W0 |
| W9 | c9 [SOL] | Provider、MCP与工具归档诊断及连接恢复 | F1, #33, #34, #35, #36 | W0 |
| W10 | c10 [SOL] | 跨平台沙箱组合与live_lab环境修正 | #32, #40, #41 | W0 |
| W11 | c11 [SOL] | 管理员口令流程与配置读取诊断 | #28, #38 | W0 |
| W12 | c12 [SOL] | 命令消费、TUI导航与参考Web界面 | F5 | W0 |
| W13 | c1 [SOL] | 公共入口接线、Plugin Host与终态查询 | F1, #35, #39 | W0, W1, W2, W3, W4, W5, W6, W7, W8, W9, W10, W11, W12 |
| W14 | c2 [SOL] | 执行全树、来源订阅与NDJSON观察流 | F7 | W0, W1, W2, W3, W4, W5, W6, W7, W8, W9, W10, W11, W12 |
| W15 | c1 [SOL] | 独立组合验收、性能复验与跨平台证据 | Cache-W0, Cache-W1, Cache-W2, Cache-W3, Cache-W4, F0, F1, F2, F3, F4, F5, F6, F7, F8, F9, #14, #15, #16, #17, #18, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #35, #36, #37, #38, #39, #40, #41 | W13, W14 |
| W16 | c2 [SOL] | 集成台账、常量目录生成与最终双审封包 | Cache-W0, F0 | W13, W14 |

### 精确白名单（原 workstreams.json 的只读投影）

<details>
<summary>W0：共享合同、配置与动作目录冻结</summary>

- `agent_py_agent/agent/agent_core/runtime/loop_models.py`
- `agent_py_agent/agent/contracts/diagnostics.py`
- `agent_py_agent/agent/conversation/business_contracts.py`
- `agent_py_agent/agent/conversation/models.py`
- `agent_py_agent/agent/plugin_host/action_specs.py`
- `agent_py_agent/agent/plugin_host/business_action_specs.py`
- `agent_py_agent/agent/plugin_host/contracts.py`
- `agent_py_agent/agent/plugin_host/schemas.py`
- `agent_py_agent/agent/plugin_manifest_host_v9.py`
- `agent_py_agent/agent/plugin_permissions/host_grants.py`
- `agent_py_agent/agent/prompting_parts/runtime_injection.py`
- `agent_py_agent/agent/settings/config.py`
- `agent_py_agent/agent/settings/config_diagnostics.py`
- `agent_py_agent/agent/settings/parameter_registry.py`
- `agent_py_agent/agent/settings/services/_normalize.py`
- `agent_py_agent/agent/workflows/models.py`
- `agent_py_agent/config/agent_config.yaml`
- `agent_py_agent/tests/test_business_v12_f1_contracts.py`
- `agent_py_agent/tests/test_mega_business_grant_catalog.py`
- `agent_py_agent/tests/test_mega_shared_contracts.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_plan_w12_public_interfaces.py`
- `agent_py_agent/tests/test_settings_config.py`

</details>

<details>
<summary>W1：业务空间、界面绑定、资产引用与访问治理</summary>

- `agent_py_agent/agent/conversation/business_access.py`
- `agent_py_agent/agent/conversation/business_assets.py`
- `agent_py_agent/agent/conversation/business_bindings.py`
- `agent_py_agent/agent/conversation/business_context.py`
- `agent_py_agent/agent/conversation/business_spaces.py`
- `agent_py_agent/agent/tooling/action_policy.py`
- `agent_py_agent/agent/tooling/business_spaces_tool.py`
- `agent_py_agent/tests/test_business_v12_f2_spaces.py`
- `agent_py_agent/tests/test_business_v12_f8_governance.py`
- `agent_py_agent/tests/test_mega_space_isolation.py`

</details>

<details>
<summary>W2：后台主会话、任务持久化、会话索引与唤醒衔接</summary>

- `agent_py_agent/agent/agent_core/orchestration/create_policy.py`
- `agent_py_agent/agent/agent_core/orchestration/dispatch/capability_auto_sweep.py`
- `agent_py_agent/agent/agent_core/runtime_mixin.py`
- `agent_py_agent/agent/conversation/background_agents.py`
- `agent_py_agent/agent/conversation/background_claim.py`
- `agent_py_agent/agent/conversation/background_team_lifecycle.py`
- `agent_py_agent/agent/conversation/runtime.py`
- `agent_py_agent/agent/conversation/session_list_index.py`
- `agent_py_agent/agent/conversation/session_messaging.py`
- `agent_py_agent/agent/conversation/session_task_dispatch.py`
- `agent_py_agent/agent/conversation/session_tasks.py`
- `agent_py_agent/agent/runtime_db/repository.py`
- `agent_py_agent/agent/session/manager.py`
- `agent_py_agent/agent/subagents/services/base.py`
- `agent_py_agent/agent/subagents/services/hierarchy/scheduler.py`
- `agent_py_agent/agent/subagents/services/run_authority.py`
- `agent_py_agent/agent/tooling/background_agents_tool.py`
- `agent_py_agent/tests/test_background_claim_execution.py`
- `agent_py_agent/tests/test_background_main_agent_runtime.py`
- `agent_py_agent/tests/test_background_reply_after_foreground_failure.py`
- `agent_py_agent/tests/test_business_v12_f3_agents.py`
- `agent_py_agent/tests/test_capability_auto_grant.py`
- `agent_py_agent/tests/test_mega_background_context_restore.py`
- `agent_py_agent/tests/test_mega_background_delivery_retry.py`
- `agent_py_agent/tests/test_mega_business_task_run_lineage.py`
- `agent_py_agent/tests/test_mega_trusted_completion_origin.py`
- `agent_py_agent/tests/test_session.py`
- `agent_py_agent/tests/test_session_task_store.py`
- `agent_py_agent/tests/test_unified_session_list_index.py`
- `agent_py_agent/tests/test_unified_wake_lane_progress.py`

</details>

<details>
<summary>W3：Workflow范围、异步节点、脚本输入与交付恢复</summary>

- `agent_py_agent/agent/assets/workflow_store.py`
- `agent_py_agent/agent/conversation/background_delivery.py`
- `agent_py_agent/agent/scheduler/tool_work.py`
- `agent_py_agent/agent/tooling/workflows_tool.py`
- `agent_py_agent/agent/workflows/agent_steps.py`
- `agent_py_agent/agent/workflows/business_scope.py`
- `agent_py_agent/agent/workflows/engine.py`
- `agent_py_agent/agent/workflows/governance.py`
- `agent_py_agent/agent/workflows/models.py`
- `agent_py_agent/agent/workflows/orphan_maintenance.py`
- `agent_py_agent/agent/workflows/release_gate.py`
- `agent_py_agent/agent/workflows/review_receipts.py`
- `agent_py_agent/agent/workflows/run_store.py`
- `agent_py_agent/agent/workflows/script_runner.py`
- `agent_py_agent/tests/test_background_owner_delivery_commit.py`
- `agent_py_agent/tests/test_business_v12_f4_workflows.py`
- `agent_py_agent/tests/test_mega_workflow_scope_resume.py`
- `agent_py_agent/tests/test_scheduler_origin_delivery_recovery.py`
- `agent_py_agent/tests/test_script_large_input_transport.py`
- `agent_py_agent/tests/test_workflow_script_engine.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_plan_w7_execution_receipts.py`
- `agent_py_agent/tests/test_plan_w7_workspace.py`

</details>

<details>
<summary>W4：任务定位、子代理健康检查与诊断CLI</summary>

- `agent_py_agent/agent/conversation/task_locator.py`
- `agent_py_agent/agent/owner_wake_discovery.py`
- `agent_py_agent/agent/subagents/services/channel_probe.py`
- `agent_py_agent/agent/user_space/home_indexes.py`
- `agent_py_agent/cli/_inspection.py`
- `agent_py_agent/tests/test_mega_probe_patch_race.py`
- `agent_py_agent/tests/test_owner_wake_discovery.py`
- `agent_py_agent/tests/test_owner_wake_discovery_task_run_no_link.py`
- `agent_py_agent/tests/test_subagent_commands.py`
- `agent_py_agent/tests/test_subagent_probe_closeout_race.py`
- `agent_py_agent/tests/test_unified_task_locator.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_home_global_index.py`

</details>

<details>
<summary>W5：运行事实分源快照、Compact与完成事件桥接</summary>

- `agent_py_agent/agent/agent_core/_finalization_service.py`
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`
- `agent_py_agent/agent/agent_core/compact_request_recovery.py`
- `agent_py_agent/agent/agent_core/runtime/guidance.py`
- `agent_py_agent/agent/agent_core/runtime/live_archive.py`
- `agent_py_agent/agent/agent_core/runtime/loop_support.py`
- `agent_py_agent/agent/agent_core/runtime/turn_trigger.py`
- `agent_py_agent/agent/agent_core/tool_ir_history.py`
- `agent_py_agent/agent/agent_core/tool_request_projection.py`
- `agent_py_agent/agent/conversation/compact.py`
- `agent_py_agent/agent/conversation/compact_summary_view.py`
- `agent_py_agent/agent/prompting_parts/builder.py`
- `agent_py_agent/agent/prompting_parts/runtime_injection.py`
- `agent_py_agent/tests/test_cache_opt_frozen_projection.py`
- `agent_py_agent/tests/test_cache_opt_runtime_sources.py`
- `agent_py_agent/tests/test_cache_opt_snapshot_contract.py`
- `agent_py_agent/tests/test_compact_source_consistency.py`
- `agent_py_agent/tests/test_compact_source_lifetime.py`
- `agent_py_agent/tests/test_mega_cache_business_recovery.py`
- `agent_py_agent/tests/test_mega_learning_finalization_bridge.py`
- `agent_py_agent/tests/test_runtime_guidance.py`
- `agent_py_agent/tests/test_unified_compact_validation_cost.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_compact_active_projection.py`
- `agent_py_agent/tests/test_compact_native_ir_recovery.py`
- `agent_py_agent/tests/test_compact_recovery_release.py`
- `agent_py_agent/tests/test_ctxopt_goal_prompt_revisions.py`
- `agent_py_agent/tests/test_gateway_compact_recovery.py`
- `agent_py_agent/tests/test_lifecycle_wake_host_event.py`
- `agent_py_agent/tests/test_native_runtime_guidance_forwarding.py`
- `agent_py_agent/tests/test_plan_w2_recovery_budget.py`
- `agent_py_agent/tests/test_prompting_builder.py`
- `agent_py_agent/tests/test_tool_request_projection.py`

</details>

<details>
<summary>W6：挂载能力优先、工具投影与可信Skill学习</summary>

- `agent_py_agent/agent/capability/business_capabilities.py`
- `agent_py_agent/agent/capability/decision_candidates.py`
- `agent_py_agent/agent/capability/package_provider.py`
- `agent_py_agent/agent/capability/router.py`
- `agent_py_agent/agent/capability/skill_learning.py`
- `agent_py_agent/agent/capability/skill_learning_host_feedback.py`
- `agent_py_agent/agent/capability/skill_learning_publish.py`
- `agent_py_agent/agent/capability/skill_learning_request.py`
- `agent_py_agent/agent/capability/skill_learning_store.py`
- `agent_py_agent/agent/capability/skill_learning_versions.py`
- `agent_py_agent/agent/capability/skill_search_tool.py`
- `agent_py_agent/agent/capability/skill_service.py`
- `agent_py_agent/agent/capability/task_learning_bridge.py`
- `agent_py_agent/agent/conversation/background_tool_policy.py`
- `agent_py_agent/agent/tooling/registry.py`
- `agent_py_agent/agent/verification/runtime.py`
- `agent_py_agent/tests/test_business_v12_f6_capabilities.py`
- `agent_py_agent/tests/test_skill_learning.py`
- `agent_py_agent/tests/test_skill_learning_host_feedback.py`
- `agent_py_agent/tests/test_skill_learning_integration.py`
- `agent_py_agent/tests/test_skill_learning_versions.py`
- `agent_py_agent/tests/test_tool_registry.py`
- `agent_py_agent/tests/test_tool_search_state.py`
- `agent_py_agent/tests/test_unified_tool_projection_cache.py`

</details>

<details>
<summary>W7：用量归一、调用账本、读预算与离线缓存分析</summary>

- `agent_py_agent/agent/agent_core/model/usage.py`
- `agent_py_agent/agent/contracts/model_call_ledger.py`
- `agent_py_agent/agent/conversation/model_metrics.py`
- `agent_py_agent/agent/tooling/artifact.py`
- `scripts/analyze_cache_ledger.py`
- `agent_py_agent/tests/test_artifact_read_lookup_plan.py`
- `agent_py_agent/tests/test_cache_opt_ledger.py`
- `agent_py_agent/tests/test_cache_opt_usage.py`
- `agent_py_agent/tests/test_mega_usage_ledger_consistency.py`
- `agent_py_agent/tests/test_memory_artifact_read.py`
- `agent_py_agent/tests/test_model_call_ledger.py`
- `agent_py_agent/tests/test_model_call_ledger_open_calls.py`
- `agent_py_agent/tests/test_model_call_ledger_partitions.py`
- `agent_py_agent/tests/test_unified_artifact_budget_window.py`
- `agent_py_agent/tests/test_unified_ledger_incremental_index.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_conversation_goal_tools.py`
- `agent_py_agent/tests/test_f7_usage_accounting_contract.py`
- `agent_py_agent/tests/test_model_usage_tags.py`

</details>

<details>
<summary>W8：存储争用测量、治理配置缓存与限流桶维护</summary>

- `agent_py_agent/agent/common/json_io.py`
- `agent_py_agent/agent/conversation/compact_threshold_policy.py`
- `agent_py_agent/agent/conversation/session_pair_rate.py`
- `agent_py_agent/agent/gateway_parts/io.py`
- `agent_py_agent/agent/io/sqlite_connection.py`
- `agent_py_agent/agent/io/sqlite_lease.py`
- `agent_py_agent/agent/io/sqlite_scope.py`
- `agent_py_agent/agent/local_storage/schema.py`
- `agent_py_agent/agent/user_space/home_retention.py`
- `agent_py_agent/agent/user_space/managed_user_governance.py`
- `agent_py_agent/agent/user_space/owner_maintenance.py`
- `agent_py_agent/tests/test_compact_threshold_unification.py`
- `agent_py_agent/tests/test_gateway_sqlite_reuse.py`
- `agent_py_agent/tests/test_managed_user_governance.py`
- `agent_py_agent/tests/test_mega_retention_scope_safety.py`
- `agent_py_agent/tests/test_private_lock_permissions.py`
- `agent_py_agent/tests/test_rework_sqlite_private_files_locks.py`
- `agent_py_agent/tests/test_session_pair_rate_limit.py`
- `agent_py_agent/tests/test_sqlite_connection_scope.py`
- `agent_py_agent/tests/test_unified_compact_policy_cache.py`
- `agent_py_agent/tests/test_unified_session_pair_retention.py`
- `agent_py_agent/tests/test_unified_storage_contention.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_plan_w4_storage_errors.py`

</details>

<details>
<summary>W9：Provider、MCP与工具归档诊断及连接恢复</summary>

- `agent_py_agent/agent/agent_core/tool_call_archive_record.py`
- `agent_py_agent/agent/backends/responses_failure.py`
- `agent_py_agent/agent/gateway_parts/request_errors.py`
- `agent_py_agent/agent/runtime_errors.py`
- `agent_py_agent/agent/tooling/executor.py`
- `agent_py_agent/agent/tooling/mcp_protocol.py`
- `agent_py_agent/agent/tooling/mcp_registration.py`
- `agent_py_agent/tests/test_mcp_diagnostic_projection.py`
- `agent_py_agent/tests/test_mcp_registration.py`
- `agent_py_agent/tests/test_mega_diagnostic_end_to_end.py`
- `agent_py_agent/tests/test_provider_diagnostic_projection.py`
- `agent_py_agent/tests/test_responses_failure.py`
- `agent_py_agent/tests/test_tool_archive_diagnostics.py`
- `agent_py_agent/tests/test_unified_provider_connection_resume.py`

</details>

<details>
<summary>W10：跨平台沙箱组合与live_lab环境修正</summary>

- `agent_py_agent/agent/attempt/sandbox.py`
- `agent_py_agent/cli/scenario.py`
- `agent_py_agent/cli/scenario_cases/gateway_cases.py`
- `agent_py_agent/cli/scenario_cases/runner_retry_backend.py`
- `agent_py_agent/cli/scenario_cases/runner_retry_case.py`
- `agent_py_agent/cli/scenario_cases/subagent_cases.py`
- `agent_py_agent/cli/scenario_utils.py`
- `agent_py_agent/cli/scenario_workspace.py`
- `scripts/live_lab/session.py`
- `agent_py_agent/tests/_sandbox_capabilities.py`
- `agent_py_agent/tests/test_attempt_sandbox.py`
- `agent_py_agent/tests/test_live_lab_home_isolation.py`
- `agent_py_agent/tests/test_live_lab_runner_interface.py`
- `agent_py_agent/tests/test_plugin_host_gateway_composition.py`
- `agent_py_agent/tests/test_sandbox_capabilities.py`
- `agent_py_agent/tests/test_sandbox_ci_contracts.py`

</details>

<details>
<summary>W11：管理员口令流程与配置读取诊断</summary>

- `agent_py_agent/agent/settings/config.py`
- `agent_py_agent/agent/settings/config_io.py`
- `agent_py_agent/agent/user_space/admin_password.py`
- `agent_py_agent/cli/admin_identity_commands.py`
- `agent_py_agent/cli/config_cmd.py`
- `docs/design/ADMIN_CHANNEL_IDENTITY.md`
- `agent_py_agent/tests/test_admin_identity_store.py`
- `agent_py_agent/tests/test_admin_password_change_flow.py`
- `agent_py_agent/tests/test_config_commands.py`
- `agent_py_agent/tests/test_config_parse_diagnostics.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_plan_w10_config_entries.py`

</details>

<details>
<summary>W12：命令消费、TUI导航与参考Web界面</summary>

- `agent_py_agent/cli/chat_parts/tui.py`
- `agent_py_agent/cli/chat_parts/tui_agent_navigation.py`
- `agent_py_agent/cli/chat_parts/tui_background_agents.py`
- `agent_py_agent/cli/chat_parts/tui_block_renderer.py`
- `agent_py_agent/cli/chat_parts/tui_business_spaces.py`
- `agent_py_agent/cli/chat_parts/tui_input_delivery.py`
- `agent_py_agent/cli/chat_parts/tui_keybindings.py`
- `agent_py_agent/cli/chat_parts/tui_preflight.py`
- `agent_py_agent/cli/chat_parts/tui_ui_setup.py`
- `agent_py_agent/cli/chat_parts/tui_worker_paths.py`
- `agent_py_agent/cli/space_commands.py`
- `agent_py_agent/cli/team_commands.py`
- `agent_py_agent/cli/workflow_commands.py`
- `examples/business_space_ui/app.js`
- `examples/business_space_ui/index.html`
- `agent_py_agent/tests/test_business_v12_f5_tui.py`

</details>

<details>
<summary>W13：公共入口接线、Plugin Host与终态查询</summary>

- `agent_py_agent/agent/command_catalog.py`
- `agent_py_agent/agent/conversation/control_commands.py`
- `agent_py_agent/agent/core.py`
- `agent_py_agent/agent/gateway_parts/business_api.py`
- `agent_py_agent/agent/gateway_parts/business_artifacts_http.py`
- `agent_py_agent/agent/gateway_parts/http_routes.py`
- `agent_py_agent/agent/plugin_host/action_specs.py`
- `agent_py_agent/agent/plugin_host/authorization.py`
- `agent_py_agent/agent/plugin_host/business_action_specs.py`
- `agent_py_agent/agent/plugin_host/business_actions.py`
- `agent_py_agent/agent/plugin_host/composition.py`
- `agent_py_agent/agent/plugin_host/conversations.py`
- `agent_py_agent/agent/plugin_host/gateway_assets.py`
- `agent_py_agent/agent/plugin_host/gateway_runtime.py`
- `agent_py_agent/agent/plugin_host/http_transport.py`
- `agent_py_agent/agent/plugin_host/interactions.py`
- `agent_py_agent/agent/plugin_host/model_settings_v2_handlers.py`
- `agent_py_agent/agent/plugin_host/requests.py`
- `agent_py_agent/agent/tooling/executor.py`
- `agent_py_agent/agent/user_space/owner_maintenance.py`
- `agent_py_agent/cli/argv_compat_v1.py`
- `agent_py_agent/cli/chat_parts/slash_commands.py`
- `agent_py_agent/cli/parser.py`
- `agent_py_agent/tests/test_mega_business_api_actions.py`
- `agent_py_agent/tests/test_mega_business_plugin_compatibility.py`
- `agent_py_agent/tests/test_mega_media_transport.py`
- `agent_py_agent/tests/test_plugin_host_requests_and_streams_v2.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_plan_w11_plugin_scope.py`
- `agent_py_agent/tests/test_plugin_host_gateway_composition.py`

</details>

<details>
<summary>W14：执行全树、来源订阅与NDJSON观察流</summary>

- `agent_py_agent/agent/conversation/business_observability.py`
- `agent_py_agent/agent/conversation/managed_subscriptions.py`
- `agent_py_agent/agent/gateway_parts/business_event_ingress.py`
- `agent_py_agent/agent/gateway_parts/business_event_stream.py`
- `agent_py_agent/agent/gateway_parts/remote_push_service.py`
- `agent_py_agent/tests/test_business_v12_f7_observation.py`
- `agent_py_agent/tests/test_mega_business_stream_resume.py`

</details>

<details>
<summary>W15：独立组合验收、性能复验与跨平台证据</summary>

- `scripts/verify_business_spaces_v12.py`
- `scripts/verify_mega_acceptance.py`
- `agent_py_agent/tests/test_business_v12_f9_e2e.py`
- `agent_py_agent/tests/test_cache_opt_provider_invariants.py`
- `agent_py_agent/tests/test_mega_aging_and_cache_invariants.py`
- `agent_py_agent/tests/test_mega_three_package_integration.py`

只读回归（无写权）：
- `agent_py_agent/tests/test_backends_native_tool_use.py`
- `agent_py_agent/tests/test_backends_provider_history_reload_identical.py`
- `agent_py_agent/tests/test_cache_diagnostics.py`
- `agent_py_agent/tests/test_cache_hit_report.py`
- `agent_py_agent/tests/test_native_tool_use_ir_messages_flow.py`
- `agent_py_agent/tests/test_plan_w0_integration.py`
- `agent_py_agent/tests/test_plan_w10_config_entries.py`
- `agent_py_agent/tests/test_plan_w11_plugin_scope.py`
- `agent_py_agent/tests/test_plan_w12_public_interfaces.py`
- `agent_py_agent/tests/test_plan_w1_lifecycle.py`
- `agent_py_agent/tests/test_plan_w2_recovery_budget.py`
- `agent_py_agent/tests/test_plan_w3_memory_batch.py`
- `agent_py_agent/tests/test_plan_w4_storage_errors.py`
- `agent_py_agent/tests/test_plan_w5_channel_lifecycle.py`
- `agent_py_agent/tests/test_plan_w5_host_delivery.py`
- `agent_py_agent/tests/test_plan_w5_topic_progress.py`
- `agent_py_agent/tests/test_plan_w6_error_projection.py`
- `agent_py_agent/tests/test_plan_w6_execution_card_projection.py`
- `agent_py_agent/tests/test_plan_w7_execution_receipts.py`
- `agent_py_agent/tests/test_plan_w7_workspace.py`
- `agent_py_agent/tests/test_plan_w8_definition_generation.py`
- `agent_py_agent/tests/test_plan_w8_schedule_target.py`
- `agent_py_agent/tests/test_plan_w9_transcript_retention.py`
- `agent_py_agent/tests/test_responses_cache_key.py`
- `agent_py_agent/tests/test_responses_websocket.py`

</details>

<details>
<summary>W16：集成台账、常量目录生成与最终双审封包</summary>

- `CODEBASE_TREE.md`
- `DESIGN_LEDGER.md`
- `STATUS.md`
- `TESTS.md`
- `agent_py_agent/config/constants_catalog.json`
- `agent_py_agent/tests/fixtures/constants_catalog_pending_fixes.json`
- `docs/COMPLETED.md`
- `docs/ROADMAP.md`
- `docs/WORKSTREAMS.md`
- `docs/design/MEGA_ARCHITECTURE_A4BC8B818.md`
- `docs/tasks/MEGA_ACCEPTANCE.md`
- `docs/tasks/MEGA_DISPATCH_PROGRESS.md`

只读回归（无写权）：
- `agent_py_agent/tests/test_constants_catalog.py`

</details>


## 4. 已批准待整改元数据的准确归属（原失败保留）

下表是源处仍存在的说明/量纲欠账，不是当前守卫失败清单。总控明确批准它们进入一次性待整改基线，W16只追加指定fixture组，未代改原owner源码或数值。后续由原owner补真实用途/量纲，需要内部改名时全调用方同步并核原值/语义；不要求双层注释标签，整改后按原守卫移除对应欠账。

| 常量 | 冻结源码位置 | owner | 缺口 |
| --- | --- | --- | --- |
| _REBUILD_BATCH | `agent_py_agent/agent/conversation/session_list_index.py:22` | W2 | 量纲后缀 / 上方中文说明 |
| TASK_LOCATOR_SCAN_MAX_FILES | `agent_py_agent/agent/conversation/task_locator.py:24` | W4 | 上方中文说明 |
| _ENUM_CACHE_CAPACITY | `agent_py_agent/agent/conversation/task_locator.py:25` | W4 | 量纲后缀 / 上方中文说明 |
| UPLOAD_MAX_BYTES | `agent_py_agent/agent/gateway_parts/business_artifacts_http.py:33` | W13 | 上方中文说明 |
| _CONFIG_PARSE_DIAGNOSTIC_LIMIT | `agent_py_agent/agent/settings/config_io.py:17` | W11 | 量纲后缀 / 上方中文说明 |
| _TOOL_PROJECTION_MAX_SNAPSHOTS | `agent_py_agent/agent/tooling/registry.py:80` | W6 | 量纲后缀 / 上方中文说明 |
| _COMPACT_POLICY_CACHE_MAX | `agent_py_agent/agent/user_space/managed_user_governance.py:28` | W8 | 量纲后缀 / 上方中文说明 |
| BUSINESS_MAINTENANCE_MAX_ROWS | `agent_py_agent/agent/workflows/orphan_maintenance.py:16` | W3 | 上方中文说明 |
| BUSINESS_ORPHAN_AFTER_DAYS | `agent_py_agent/agent/workflows/orphan_maintenance.py:15` | W3 | 上方中文说明 |
| SCRIPT_ENVIRONMENT_MAX_BYTES | `agent_py_agent/agent/workflows/script_runner.py:51` | W3 | 上方中文说明 |

当前动作以 W16-fix 为准：只接受这10项明确批准的基线，原组原样保留、无重复，不授权后续自动追加或修改测试/生成规则。原拟owner返工草稿未派发，当前不执行该旧草稿；将来需要补元数据时由总控另定互斥写集。修订前review/JUnit/日志、准确来源与批准指纹分别在仓外 `evidence/before-w16-fix/` 与 `w16_fix_approval.json`，不抹去历史失败，也不把fixture登记当源处修复。

## 5. 跨片 closure 与最终阶段

- #22：W2+W3；canonical 持久完成后仅补交付，不重跑 script。
- #25：W5 候选→W2 canonical 持久完成→W6 可信入队；save=False 不变。
- #35：W9 结构化诊断→W13 实际接线与结果透传。
- #36：W2 runtime 三项→W9 provider 连接上限恢复，两者同时满足。
- F7：W13+W14 真实 native/Plugin 入口、observation actor/catalog、精确 reader、流 EventView/NDJSON、订阅接纳与原幂等账共同验收。

上述最终同源 closure 当前均 NOT_RUN；逐 Issue/81 项标准见 [MEGA_ACCEPTANCE](MEGA_ACCEPTANCE.md)。不将相邻代码变动或分片旧 PASS 作为闭环结论。

当前设计基线到 Wave2 的 `agent_py_agent/`、`scripts/` 中 `.py` 增删实际为 **21672 行**，已超过 AGENTS 的 10000 行阈值。最终组合必须重算并跑完整 pytest；W15 未合入前不提前声称全矩阵或最终全仓完成。

总控先收 W16 修订提交和 W15，再冻结组合 SHA。W16 随后只读验证、仓外封包并交同源 Opus 5.5 max / GPT-6 Pro 独立双审；原生平台、真实模型、soak、未知副作用仍按实际证据分开记 NOT_RUN。无自动 push/部署/发消息/关闭 Issue。

## 6. 来源与证据索引

- 冻结派工包：`/private/tmp/my-agent-mega/{workstreams.json,interface_contracts.json,plan-w0.md…plan-w16.md,next_round_audit_prompt.md}`。
- 本轮逐项核对：`/private/tmp/my-agent-w16/integration_inventory.json`；含原 handoff hash、完整 SHA、每片 scope、gate exit code、路径后像差异与每波 overlap。
- 原失败与定义：同目录 `focused.xml`、`constants_gate_findings.json`、`gate_reconciliation.md`；后续提交审查为 `review.json`。
- 调用方横扫：同目录 `caller_hits.txt` / `caller_classification.json`；W16 没有生产 Python 接口修改，常量展示 reader 单独核对。

## 7. 历史记录界限

原 STATUS/ROADMAP/COMPLETED/WORKSTREAMS/DESIGN_LEDGER/TESTS 正文继续保留，旧日期、旧 W0–W12 编号、旧失败和平台限制不删除。旧编号不是本轮 17 片的同义映射，也不能覆盖本页引用的本轮结构化权限。
