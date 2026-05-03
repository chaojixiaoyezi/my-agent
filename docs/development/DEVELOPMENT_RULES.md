# Development Rules

LLM: Check these rules before changing code.

给人看的解释：
开发规则用于保持项目可维护，而不是替代判断。

## Required Workflow

- Read the nearest module guide or architecture doc before editing unfamiliar code.
- Prefer small, reversible changes over broad rewrites.
- Preserve public CLI names and existing data formats unless an ADR approves a break.
- Add or update tests when behavior changes.
- Run focused tests first, then broader tests when touching shared infrastructure.

## Code Shape

- Do not add `import *`.
- Do not introduce vague filenames such as `utils.py`, `helpers.py`, or `common.py`.
- Do not add runtime artifacts to git.
- Keep command registration separate from command execution for new CLI work.
- Use explicit service names when extracting manager behavior.

