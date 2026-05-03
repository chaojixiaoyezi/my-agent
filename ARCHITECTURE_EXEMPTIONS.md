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
| E-001 | `cli/chat.py` | 1017 lines (now split: 167 lines) | Split complete 2026-05-04: `chat_parts/` subpackage with `tui.py` (541 lines), `fallback.py` (401 lines), `session_state.py` (79 lines), `gateway_client.py` (118 lines), `input_loop.py` (122 lines), `renderer.py` (83 lines), `history.py` (43 lines), `__init__.py` (118 lines). Main orchestrator at 167 lines < 200 target. | N/A | N/A | N/A | N/A |
| E-002 | `agent/agent_core/dispatch_mixin.py` | 895 lines (now split) | Split complete: `dispatch_service.py` (247 lines), `planner_service.py` (172 lines), `runner_gate.py` (194 lines), `acceptance_gate.py` (67 lines), `dispatch_mixin.py` (195 lines). Facade delegates to services. Exemption resolved 2026-05-04. | N/A | N/A | N/A | N/A |
| E-003 | `agent/memory_archive/query.py` | 838 lines (now split) | Split complete: `query_models.py`, `query_service.py`, `filter_policy.py`, `rendering_adapter.py`, `query.py` (100 lines). Exemption resolved 2026-05-04. | N/A | N/A | N/A | N/A |
| E-004 | `agent/subagents/manager_patch.py` | 109 lines (split 2026-05-04) | Split complete: `patch/patch_service.py` (328 lines), `patch/patch_apply.py` (490 lines), `patch/patch_renderer.py` (165 lines), `patch/patch_apply_helpers.py` (77 lines), `patch/__init__.py` (30 lines). Facade at 109 lines. Security boundary enforcement preserved. | N/A | N/A | N/A | N/A |
| E-005 | `agent/settings/config.py` | 751 lines (now split) | Split complete: `normalize.py` (681 lines, contains all normalize logic), `config.py` (248 lines, thin composition entry), `model_config.py` (23 lines), `gateway_config.py` (24 lines), `subagent_config.py` (28 lines), `adapter_config.py` (22 lines), `tool_config.py` (24 lines), `memory.py` (403 lines, pre-existing). Exemption resolved 2026-05-04. | N/A | N/A | N/A | N/A |
| E-006 | `agent/subagents/manager_base.py` | 751 lines | Split partially complete: `state_machine.py` (184 lines) extracted. `manager_lifecycle.py` (197 lines) refactored. Base mixin still contains core task creation logic. | State file format change; CLI behavior change | Further extract task creation, work order building to separate service; target < 200 lines facade | architecture owner | 2026-07-15 |
| E-007 | `agent/log_analysis/analytics/detectors/rules.py` | 745 lines (now split) | Split complete: `rule_models.py` (32 lines), `rule_evaluator.py` (469 lines), `rule_loader.py` (52 lines), `rule_helpers.py` (199 lines), `rules.py` (57 lines). Exemption resolved 2026-05-04. | N/A | N/A | N/A | N/A |
| E-008 | `agent/log_analysis/tools.py` | 672 lines | Log analysis is scheduled for plugin-ization (ADR-0003). Splitting now would create throwaway code. Blocked on ExtensionPlugin interface definition. | Extension interface not yet defined; throwaway refactor | Implement ExtensionPlugin interface, create LogAnalysisPlugin, move handlers | architecture owner | 2026-07-15 |
| E-009 | `cli/memory_commands.py` | 608 lines (now split: 15 lines) | Split complete 2026-05-04: `memory_commands/` subpackage with `memory_query_cmd.py` (216 lines), `memory_doctor_cmd.py` (276 lines), `__init__.py` (39 lines). Main entry at 15 lines < 100 target. | N/A | N/A | N/A | N/A |
| E-010 | `agent/gateway_parts/runtime.py` | 657 lines | Gateway runtime involves queue, worker, recovery, and chunk streaming. Complex state management. | Gateway lease/chunk regression; recovery path breakage | Extract `request_worker.py`, `response_renderer.py`, `audit_service.py`; target < 300 lines | architecture owner | 2026-06-30

---

## Historical Exemptions (Resolved)

| # | Item | Was | Resolution | Date |
|---|------|-----|------------|------|
| E-002 | `agent/agent_core/dispatch_mixin.py` | 895 lines | Split complete: `dispatch_service.py`, `planner_service.py`, `runner_gate.py`, `acceptance_gate.py`, `dispatch_mixin.py` (facade at 195 lines) | 2026-05-04 |
| E-003 | `agent/memory_archive/query.py` | 838 lines | Split complete: `query_models.py`, `query_service.py`, `filter_policy.py`, `rendering_adapter.py`, `query.py` (100 lines) | 2026-05-04 |
| E-004 | `agent/subagents/manager_patch.py` | 794 lines | Split complete: `patch/` subpackage with 5 modules; facade at 109 lines | 2026-05-04 |
| E-005 | `agent/settings/config.py` | 751 lines | Split complete: `normalize.py`, `config.py` (thin facade), plus domain config modules | 2026-05-04 |

Add entries here when an exemption is resolved (file split or reduced below hard limit).

---

## Exemption Renewal Log

| # | Item | Original Expiry | New Expiry | Reason |
|---|------|----------------|------------|--------|
| (none yet) | -- | -- | -- | -- |

---

## Summary

- **Active exemptions**: 3
- **Expiring by 2026-06-30**: 1 (E-010)
- **Expiring by 2026-07-15**: 2 (E-006, E-008)
- **Renewals**: 0
- **Resolved this session**: 7 (E-001, E-002, E-003, E-004, E-005, E-007, E-009)
- **Target**: reduce active exemptions to < 3 by 2026-07-31
