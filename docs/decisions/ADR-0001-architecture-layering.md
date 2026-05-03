# ADR-0001: Architecture Layering

LLM: Prefer layered dependencies over direct cross-module mutation.

给人看的解释：
本 ADR 记录项目长期分层方向。

## Status

Accepted

## Decision

The project will keep CLI, orchestration, subagent state, memory routing, memory archive, and tooling as separate ownership areas. New code should depend inward through explicit functions or services, not by reaching into another module's runtime files.

## Consequences

- CLI registration can migrate incrementally into `cli/commands/`.
- Large compatibility facades may remain temporarily, but new behavior should be placed behind named services.
- Architecture guardrails enforce no new star imports, no committed runtime artifacts, and no new vague filenames.

