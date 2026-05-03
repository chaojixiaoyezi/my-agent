# SubAgent Service Refactor Plan

LLM: Preserve SubAgentManager behavior while extracting services behind it.

给人看的解释：
这是一份低风险迁移计划。不要一次性重写状态机，先把边界切出来。

## Goals

- Keep existing CLI and tests compatible.
- Move cohesive behavior out of large manager mixins.
- Keep JSONL and state-file formats stable.
- Make acceptance, dispatch, patching, and reporting independently testable.

## Proposed Services

- `SubAgentPersistenceService`: load, list, and save subagent task records.
- `SubAgentLifecycleService`: create, pause, resume, abandon, due checks.
- `SubAgentDispatchService`: route tasks to workers and record dispatch decisions.
- `SubAgentAcceptanceService`: evaluate completion, findings, and handoff quality.
- `SubAgentPatchService`: inspect and apply worker patches.
- `SubAgentReportService`: render boards, summaries, and work logs.
- `CapabilityRoutingService`: connect capability gaps to memory routes.

## Migration Steps

1. Add service classes that receive existing manager paths/config as constructor data.
2. Move pure helpers first, with tests around existing public methods.
3. Route one public manager method at a time through a service.
4. Keep old method names as facade methods until all callers migrate.
5. Remove star imports only after service boundaries are stable.

## Implemented

- `agent_py_agent/agent/subagents/services/persistence.py` now owns `load`, `list_runs`, and `save` logic.
- `agent_py_agent/agent/subagents/services/lifecycle.py` now owns capability requests, grants, gaps, evidence, heartbeat, and status mutation.
- `SubAgentBaseMixin` delegates public persistence methods to `self.persistence`.
- `SubAgentLifecycleMixin` delegates its public lifecycle mutation methods to `self.lifecycle`.
- `agent_py_agent/tests/test_subagent_persistence_service.py` verifies create/save/load/list compatibility.
- `agent_py_agent/tests/test_subagent_lifecycle_service.py` verifies lifecycle mutation compatibility.

## Stop Conditions

- Do not change subagent state-file schema in this refactor.
- Do not rename public CLI commands.
- Stop and add an ADR if a service needs to own persistence format changes.
