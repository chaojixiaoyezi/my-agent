# ADR-0002: SubAgentManager To Services

LLM: Keep SubAgentManager as facade while extracting services.

给人看的解释：
SubAgentManager 是分布式 god class，通过 mixin 拼凑而成。本 ADR 决定逐步提取独立服务。

## Status

Accepted

## Context

SubAgentManager behavior spans 3,500+ lines across mixin files: `manager_base.py` (744 lines, state machine/lifecycle), `manager_patch.py` (794 lines, patch review/apply), `dispatch_mixin.py` (889 lines, dispatch/planner/runner), plus capability and reporting mixins. Mixins share state through `self`, making isolation testing impossible. Security-sensitive patch logic is mixed with UI rendering.

## Decision

Extract cohesive behavior into standalone services. SubAgentManager becomes a thin facade.

| Service | Responsibility | Source |
|---------|---------------|--------|
| LifecycleService | Start, stop, health, recovery, state files | manager_base.py |
| StateMachine | State transitions, validation, events | manager_base.py |
| DispatchService | Orchestration, step sequencing, audit | dispatch_mixin.py |
| PlannerService | Task decomposition, complexity estimation | dispatch_mixin.py |
| RunnerGate | Runner execution, timeout, result collection | dispatch_mixin.py |
| AcceptanceGate | Acceptance criteria evaluation | dispatch_mixin.py |
| PatchService | Patch review, approval workflow | manager_patch.py |
| PatchApply | Patch application with write boundary | manager_patch.py |
| CapabilityRouter | Capability matching, tool routing | manager_subagents.py |

Migration order: LifecycleService -> StateMachine -> PatchService/Apply -> PlannerService -> DispatchService/RunnerGate/AcceptanceGate -> CapabilityRouter.

## Consequences

- Services are independently testable without mixin state.
- Security-sensitive patch logic is isolated and auditable.
- Temporary duplication during migration; SubAgentManager public API stays stable.
- Existing state files and CLI behavior must not change during migration.
- Star imports in current manager modules are frozen as historical debt; remove gradually.

### References

- REFACTORING_BACKLOG.md items 2, 4, 6; ARCHITECTURE_EXEMPTIONS.md E-002, E-004, E-006
