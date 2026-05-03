# TASK-20260503: Architecture Governance Pass

LLM: This task records the governance cleanup performed on 2026-05-03.

给人看的解释：
这不是业务功能开发，而是一轮架构边界和工程卫生整理。

## Problem Solved

Runtime artifacts, CLI parser growth, undocumented module boundaries, and missing architecture guardrails made future refactors risky.

## Scope Changed

- Strengthened ignored runtime artifact rules.
- Added Python tooling configuration.
- Converted the CLI parser into a thin delegating entrypoint.
- Extracted chat history/rendering primitives into `cli/chat_parts/`.
- Extracted subagent task persistence into `SubAgentPersistenceService`.
- Added architecture guardrail tests.
- Added architecture, development, and ADR documentation.

## Tests

See `docs/audits/ARCHITECTURE_CLEANUP_REPORT.md` for final command results.
