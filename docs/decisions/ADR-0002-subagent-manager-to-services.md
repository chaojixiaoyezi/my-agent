# ADR-0002: SubAgentManager To Services

LLM: Keep SubAgentManager as facade while extracting services.

给人看的解释：
这个决定避免一次性重写子代理状态机。

## Status

Accepted

## Decision

`SubAgentManager` remains the public compatibility layer. Cohesive behavior should be extracted into services such as lifecycle, dispatch, acceptance, patching, reporting, and capability routing.

## Consequences

- Existing state files and CLI behavior stay stable.
- New tests can target services before public callers migrate.
- Star imports in current manager modules are frozen as historical debt and should be removed gradually.

