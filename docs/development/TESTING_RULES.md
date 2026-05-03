# Testing Rules

LLM: Match test scope to risk and run architecture guardrails for structural work.

给人看的解释：
测试不只是证明功能对，也要证明架构没有继续变坏。

## Test Layers

- Unit tests for pure helpers and services.
- CLI parser tests for command compatibility.
- Filesystem tests for write boundaries.
- Architecture guardrails for import, artifact, naming, and entrypoint drift.

## Required Commands For Governance Work

- `python -m compileall -q agent_py_agent scripts`
- `python -m pytest agent_py_agent/tests/test_packaging.py agent_py_agent/tests/test_cli_parser.py agent_py_agent/tests/test_tooling_filesystem.py -q`
- `python -m pytest agent_py_agent/tests/test_architecture_guardrails.py -q`
- `git diff --check`

