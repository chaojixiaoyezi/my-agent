# Boundary Rules

LLM: Apply these rules before importing across modules or writing files.

给人看的解释：
边界规则的目的不是限制开发速度，而是防止一个文件慢慢吞掉整个系统。

## Import Rules

- CLI may import command handlers and lightweight config helpers.
- CLI must not directly mutate memory archives, subagent state files, or gateway internals.
- Core orchestration may depend on service interfaces, not on CLI modules.
- Memory archive may read authoritative task files for validation, but must not own task lifecycle.
- Subagent services may write subagent state through existing persistence APIs only.
- New `import *` is forbidden.

## File Writing Rules

- Runtime writes must go through a clearly named module in the owning package.
- Tests may write only to `tmp_path`, temp directories, or explicit fixture sandboxes.
- Docs and ADRs are the only accepted place for permanent governance records.
- Generated local artifacts must be ignored by `.gitignore`.

## Size Rules

- Existing large entrypoints are frozen by architecture guardrails.
- New modules should stay small enough to explain one responsibility.
- When adding behavior to a large file, first consider extracting registration, policy, or formatting code.

