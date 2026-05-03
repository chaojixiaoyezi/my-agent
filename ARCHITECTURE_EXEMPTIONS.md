# ARCHITECTURE EXEMPTIONS

LLM: No exemption is permanent. Update this register whenever code-size debt changes. Every exemption must have an expiry date.

给人看的解释：
下面是历史大文件/大类的临时豁免。新增代码不能借这些豁免继续膨胀。豁免到期后，相关文件必须拆分或重新申请豁免（需说明新阻塞原因）。

---

## Exemption Rules

1. Every exemption must list: file/function, current size, why it cannot be split now, risk, planned split, owner, and expiry date.
2. No permanent exemptions. Maximum initial exemption period is 90 days.
3. An exemption can be renewed once (30 days) if the split is actively in progress and blocked by a concrete dependency.
4. Exemptions that pass their expiry without action become merge blockers in `--mode strict` mode.
5. New code added to an exempted file must not increase its size beyond the next threshold (e.g., if exempted at 700 lines, new code must not push it past 900).

---

## Active Exemptions

| # | Item | Current Size | Why Not Fully Split Now | Risk | Split Plan | Owner | Expires |
|---|------|-------------:|------------------------|------|------------|-------|---------|
| E-001 | `cli/chat.py` | 989 lines | TUI/fallback worker extraction needs integration tests covering streaming, prompt_toolkit keybindings, and gateway fallback paths. Existing test coverage is insufficient for safe extraction. | Interactive streaming regression; prompt_toolkit compatibility breakage | Extract `tui.py`, `fallback.py`, `session_state.py`, `gateway_client.py`, `input_loop.py`; target < 200 lines orchestrator | architecture owner | 2026-06-30 |
| E-002 | `agent/agent_core/dispatch_mixin.py` | 889 lines | Dispatch orchestration spans gateway, subagent, and acceptance paths. Extracting requires re-verification of audit log ordering and dispatch sequence across 3 integration test suites. | Dispatch ordering change; audit log sequence breakage | Extract `dispatch_service.py`, `planner_service.py`, `runner_gate.py`, `acceptance_gate.py`; target < 200 lines facade | architecture owner | 2026-07-15 |
| E-003 | `agent/memory_archive/query.py` | 839 lines | Query filter paths are numerous (12+ filter predicates) and tightly coupled with CLI rendering. Needs filter predicate test suite before safe extraction. | Search result format change; recovery path breakage | Extract `query_models.py`, `query_service.py`, `filter_policy.py`, `rendering_adapter.py`; target < 200 lines entry point | architecture owner | 2026-06-30 |
| E-004 | `agent/subagents/manager_patch.py` | 794 lines | Patch apply logic enforces file write boundary. Extraction must preserve security audit trail and write boundary enforcement. Security-sensitive code requires extra review. | Write boundary bypass; audit trail gap | Extract `patch_service.py`, `patch_apply.py`, `patch_renderer.py`; target < 150 lines facade | architecture owner | 2026-06-30 |
| E-005 | `agent/settings/config.py` | 751 lines | Config fields have deep cross-dependencies. Splitting requires establishing domain config test suites to catch default value regressions. Silent breakage risk for existing users. | Config default value regression; backward compatibility breakage | Extract `model_config.py`, `memory_config.py`, `gateway_config.py`, `subagent_config.py`, `adapter_config.py`, `normalize.py`; target < 250 lines | architecture owner | 2026-07-15 |
| E-006 | `agent/subagents/manager_base.py` | 744 lines | Base manager state machine has 8 states and 12 transitions. Extraction requires state machine test coverage. State file format is a compatibility constraint. | State file format change; CLI behavior change | Extract `lifecycle_service.py`, `state_machine.py`; target < 200 lines facade | architecture owner | 2026-07-15 |
| E-007 | `agent/memory_routing/rules.py` | 747 lines | Routing rules are data-driven but rule loading has caching implications. Low risk but needs loader test suite. | Routing behavior change | Extract `rule_models.py`, `rule_evaluator.py`, `rule_loader.py`; target < 200 lines | architecture owner | 2026-07-15 |
| E-008 | `agent/log_analysis/tools.py` | 666 lines | Log analysis is scheduled for plugin-ization (ADR-0003). Splitting now would create throwaway code. Blocked on ExtensionPlugin interface definition. | Extension interface not yet defined; throwaway refactor | Implement ExtensionPlugin interface, create LogAnalysisPlugin, move handlers | architecture owner | 2026-07-15 |
| E-009 | `cli/memory_commands.py` | 609 lines | Memory CLI commands are growing. Low risk to split but not yet at hard limit. | None significant | Split into `memory_query_cmd.py`, `memory_archive_cmd.py`, `memory_doctor_cmd.py` | architecture owner | 2026-06-30 |
| E-010 | `agent/gateway_parts/runtime.py` | 657 lines | Gateway runtime involves queue, worker, recovery, and chunk streaming. Complex state management. | Gateway lease/chunk regression; recovery path breakage | Extract `request_worker.py`, `response_renderer.py`, `audit_service.py`; target < 300 lines | architecture owner | 2026-06-30 |

---

## Historical Exemptions (Resolved)

| # | Item | Was | Resolution | Date |
|---|------|-----|------------|------|
| (none yet) | -- | -- | -- | -- |

Add entries here when an exemption is resolved (file split or reduced below hard limit).

---

## Exemption Renewal Log

| # | Item | Original Expiry | New Expiry | Reason |
|---|------|----------------|------------|--------|
| (none yet) | -- | -- | -- | -- |

---

## Summary

- **Active exemptions**: 10
- **Expiring by 2026-06-30**: 4 (E-001, E-003, E-004, E-009, E-010)
- **Expiring by 2026-07-15**: 5 (E-002, E-005, E-006, E-007, E-008)
- **Renewals**: 0
- **Target**: reduce active exemptions to < 3 by 2026-07-31
