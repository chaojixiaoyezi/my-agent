# Long-Term Architecture Tree

LLM: Use this as the target module map when placing new code.

给人看的解释：
本文件定义长期目录树，不要求一次性搬完历史代码，但新增代码必须优先放到目标边界内。

## Top-Level Ownership

- `agent_py_agent/cli/`: command-line parsing and presentation only.
- `agent_py_agent/agent/agent_core/`: main orchestration loop and dispatch policy.
- `agent_py_agent/agent/gateway_parts/`: gateway request lifecycle, persistence, and path helpers.
- `agent_py_agent/agent/subagents/`: subagent state machine and compatibility facade.
- `agent_py_agent/agent/memory_store/`: append-only long-term memory JSONL store.
- `agent_py_agent/agent/memory_routing/`: memory route index, validation, and context selection.
- `agent_py_agent/agent/memory_archive/`: snapshots, compression archive, token accounting, and resume.
- `agent_py_agent/agent/tooling/`: filesystem, shell, and other external tool adapters.
- `agent_py_agent/agent/extensions/`: plugin contracts for optional capabilities such as log analysis, BAS, and code review.
- `agent_py_agent/config/`: default config templates only.
- `agent_py_agent/tests/`: repo tests, including architecture guardrails.
- `docs/`: design, governance, task records, ADRs, and audit reports.

## Target CLI Shape

- `agent_py_agent/cli/parser.py` should only build the root parser and call command registration modules.
- `agent_py_agent/cli/commands/` owns argparse registration by command family.
- Existing command implementation modules may stay in place during migration.
- New CLI commands must add a small `add_<name>_command()` registration function.

## Current Migration State

- `agent_py_agent/cli/parser.py` is now a thin root parser.
- `agent_py_agent/cli/commands/` is the registration namespace for core, gateway, subagent, task, learning, bench, and operations commands.
- `agent_py_agent/cli/chat_parts/` owns extracted chat history and rendering primitives.
- `agent_py_agent/agent/subagents/services/` owns extracted SubAgentManager services, starting with persistence.

## Target Subagent Shape

- `SubAgentManager` remains the public compatibility facade.
- New behavior should move behind explicit services before being exposed through the facade.
- Service candidates are lifecycle, dispatch, acceptance, patch application, reporting, and capability routing.

## Target Runtime State Shape

- Runtime state must live under ignored data or memory directories.
- No cache, generated report, local DB, model profile, or raw memory artifact should be committed.
- Durable project knowledge belongs in `docs/`, not in runtime JSONL files.
