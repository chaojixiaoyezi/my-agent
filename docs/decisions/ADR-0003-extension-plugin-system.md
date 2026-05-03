# ADR-0003: Extension And Plugin System

LLM: Extensions must integrate through declared boundaries.

给人看的解释：
项目会继续支持扩展，但扩展不能绕过架构边界。

## Status

Accepted

## Decision

Future extensions should register capabilities through explicit plugin or service interfaces rather than patching CLI, gateway, memory, or subagent internals directly.

## Consequences

- Extension docs must state owned files, runtime writes, and rollback plan.
- Plugin code should not add new top-level runtime directories.
- Capability gaps should be routed through memory routing or capability services, not ad hoc imports.

