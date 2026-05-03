# Module Ownership

LLM: Use this table to decide where new code belongs.

给人看的解释：
模块归属表让后续重构有路线，不需要每次靠猜。

| Area | Owns | Must Not Own |
| --- | --- | --- |
| `cli/parser.py` | root parser and top-level defaults | business logic, persistence, command registration details |
| `cli/commands/` | argparse registration by command family | command execution internals |
| `cli/chat_parts/` | extracted chat history, slash commands, and terminal rendering helpers | model execution and gateway request handling |
| `agent_core/` | run loop, dispatch decisions, compression hooks | CLI output formatting |
| `subagents/manager*.py` | compatibility surface during migration | unrelated memory/archive policy |
| `subagents/services/` | extracted manager services such as persistence | parser registration |
| `memory_store/` | JSONL memory persistence | route matching policy |
| `memory_routing/` | route indexes, matching, validation | memory archive snapshots |
| `memory_archive/` | snapshot/archive/tokens/resume | subagent state machine |
| `tooling/` | safe adapters around external effects | task orchestration policy |
| `docs/decisions/` | ADRs | transient notes |

## Current Debt Baseline

- `agent_py_agent/cli/chat.py` is still large, but history, rendering primitives, and common slash commands now live in `cli/chat_parts/`.
- `agent_py_agent/cli/parser.py` is now a thin delegating entrypoint.
- `agent_py_agent/agent/subagents/manager_base.py` and mixins remain a compatibility cluster, with persistence and lifecycle mutations now behind services.
- Historical `import *` use has been removed; guardrail tests now require zero star imports.
