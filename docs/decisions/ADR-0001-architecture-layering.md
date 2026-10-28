# ADR-0001: Architecture Layering

LLM: Prefer layered dependencies over direct cross-module mutation.

给人看的解释：
本 ADR 记录项目的长期分层方向。新代码必须遵循层间依赖规则。

## Status

Accepted

2026-10-28 状态补充：本 ADR 的分层、职责和状态所有权决策继续有效；当时引用的代码尺寸数值门与 `CODE_SIZE_POLICY.md` 已退役，不再作为当前执行指令。旧引用保留为历史来源，现行要求见 [开发规则](../development/DEVELOPMENT_RULES.md)、[边界规则](../architecture/BOUNDARY_RULES.md) 与 [设计台账](../../DESIGN_LEDGER.md) 当日 Wave 8 条目。

## Context

The project has grown into a multi-module system with CLI, orchestration, subagent management, memory, gateway, and tooling. Without explicit layering, modules reach into each other's internals, creating circular dependencies and making refactoring risky. CLI modules directly mutate agent core state; subagent mixins reach into memory internals; gateway depends on CLI formatting.

## Decision

Adopt a layered architecture with downward-only dependency flow:

```
Interfaces (contracts: ExtensionPlugin, WriteBoundary, typed service payloads)
    v
Application (cli/, gateway.py -- thin entry points)
    v
Domain (agent_core/, subagents/, memory*, session/ -- business logic)
    v
Infrastructure (local_storage/, memory_store/, backends/, io/)
    v
Extensions (log_analysis/, plugins -- optional capabilities)
    v
Shared (models, config, constants -- no business logic)
```

### Dependency Rules

1. Dependencies flow downward only.
2. Shared must not import from any other layer.
3. Domain must not import from application or extensions.
4. Extensions register via interfaces; domain calls them through interface methods.
5. Infrastructure implements domain-defined repository interfaces.

## Consequences

- Clear ownership: new features belong to a specific layer.
- Testability: domain logic can be tested without CLI or gateway.
- Refactoring cost: existing cross-cutting imports need to be untangled at the
  current boundary.
- Do not keep long-lived shims for old boundaries.
- `scripts/check_architecture_boundaries.py` enforces import rules; violations tracked in ARCHITECTURE_EXEMPTIONS.md.

### References

- ARCHITECTURE_BOUNDARY.md, CODE_SIZE_POLICY.md, REFACTORING_BACKLOG.md
