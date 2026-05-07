# REFACTORING BACKLOG

LLM: Keep this backlog concrete and executable. Each entry must have a verification command.

给人看的解释：
这里列出最应该拆的文件，按严重程度排序。每项包含问题、目标结构、步骤、风险和验收命令。当前 strict 快照：277 findings (0 hard, 277 high-risk, 0 soft)。

注意：部分条目保留的是历史拆分计划；执行前必须重新运行 code-size 检查并核对当前文件大小，避免按旧行数做不必要的大拆。

---

## Priority 1: Historical hard-limit split plans (revalidate before acting)

### 1. `cli/chat.py` -- 1017 lines

- **Current Problem**: 1017 lines, 15+ functions, handles TUI loop, fallback loop, streaming, session state, gateway client, slash commands, and rendering. Single file owns the entire interactive chat experience.
- **Function Count**: ~18 functions, 4 over 100-line hard limit.
- **Target Structure**:
  - `chat_parts/tui.py` -- TUI input loop and prompt_toolkit integration
  - `chat_parts/fallback.py` -- fallback worker for non-streaming responses
  - `chat_parts/session_state.py` -- session lifecycle and context tracking
  - `chat_parts/gateway_client.py` -- gateway request/response streaming
  - `chat_parts/input_loop.py` -- user input handling and history
  - `chat.py` -- thin orchestrator that composes the above (target: < 200 lines)
- **Split Steps**:
  1. Extract `fallback_worker()` and its helpers into `chat_parts/fallback.py`
  2. Extract TUI setup, keybindings, and prompt loop into `chat_parts/tui.py`
  3. Extract gateway streaming into `chat_parts/gateway_client.py`
  4. Extract session state tracking into `chat_parts/session_state.py`
  5. Reduce `chat.py` to orchestration only
- **Risk**: High -- interactive behavior, streaming output, and prompt_toolkit compatibility are fragile. Each extraction needs manual TUI testing.
- **Verification**: `python3 -m pytest agent_py_agent/tests/test_cli_chat.py agent_py_agent/tests/test_chat_parts.py -q`

### 2. `agent/agent_core/dispatch_mixin.py` -- 895 lines

- **Current Problem**: 895 lines, distributed god mixin handling dispatch orchestration, planner logic, runner gate, acceptance gate, and audit logging. Mixin pattern makes it hard to test in isolation.
- **Function Count**: ~22 functions, 6 over 100-line hard limit.
- **Target Structure**:
  - `dispatch_service.py` -- dispatch orchestration and step sequencing
  - `planner_service.py` -- task planning and decomposition
  - `runner_gate.py` -- runner execution and timeout handling
  - `acceptance_gate.py` -- acceptance criteria evaluation
  - `dispatch_mixin.py` -- thin facade delegating to services (target: < 200 lines)
- **Split Steps**:
  1. Extract pure decision functions (no side effects) into `planner_service.py`
  2. Extract dispatch step result tracking into `dispatch_service.py`
  3. Extract runner execution and timeout into `runner_gate.py`
  4. Extract acceptance evaluation into `acceptance_gate.py`
  5. Convert mixin to facade pattern
- **Risk**: High -- dispatch ordering and audit log sequence must not change. Gateway and subagent integration paths are complex.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "dispatch or gateway"`

### 3. `agent/memory_archive/query.py` -- 838 lines

- **Current Problem**: 838 lines, handles query construction, filter predicates, result parsing, pagination, and CLI rendering. Query logic is interleaved with presentation.
- **Function Count**: ~16 functions, 4 over 100-line hard limit.
- **Target Structure**:
  - `query_models.py` -- query request/response dataclasses
  - `query_service.py` -- query execution and filter application
  - `filter_policy.py` -- filter predicate evaluation
  - `rendering_adapter.py` -- CLI output formatting
  - `query.py` -- thin entry point (target: < 200 lines)
- **Split Steps**:
  1. Extract filter predicates into `filter_policy.py`
  2. Extract query result dataclass and pagination into `query_models.py`
  3. Extract query execution into `query_service.py`
  4. Extract CLI rendering into `rendering_adapter.py`
- **Risk**: Medium -- CLI search compatibility and recovery paths must remain stable.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "memory_archive"`

### 4. `agent/subagents/manager_patch.py` -- 794 lines

- **Current Problem**: 794 lines, handles patch review, diff rendering, apply logic, audit logging, and file write boundary enforcement. Mixin pattern hides the complexity.
- **Function Count**: ~14 functions, 3 over 100-line hard limit.
- **Target Structure**:
  - `patch_service.py` -- patch review and approval workflow
  - `patch_apply.py` -- patch application with write boundary
  - `patch_renderer.py` -- diff display and summary
  - `manager_patch.py` -- thin facade (target: < 150 lines)
- **Split Steps**:
  1. Extract review workflow into `patch_service.py`
  2. Extract apply logic with write boundary into `patch_apply.py`
  3. Extract diff rendering into `patch_renderer.py`
- **Risk**: High -- file write boundary and patch audit trail must not be bypassed. Security-sensitive.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "patch or subagent"`

### 5. `agent/settings/config.py` -- 751 lines

- **Current Problem**: 751 lines, monolithic config with model, memory, gateway, subagent, adapter, and UI settings all in one file. Default values and normalization logic interleaved.
- **Function Count**: ~20 functions, 2 over 100-line limit.
- **Target Structure**:
  - `model_config.py` -- model selection and parameters
  - `memory_config.py` -- memory routing and archive settings
  - `gateway_config.py` -- gateway connection and retry settings
  - `subagent_config.py` -- subagent limits and policies
  - `adapter_config.py` -- adapter channel settings
  - `normalize.py` -- config validation and default normalization
  - `config.py` -- composition and backward-compatible exports (target: < 250 lines)
- **Split Steps**:
  1. Extract pure dataclass domain views
  2. Extract normalize/validation helpers
  3. Make lower modules accept only the config slice they need
  4. Keep backward-compatible re-exports in `config.py`
- **Risk**: High -- config compatibility and default value changes break existing users silently.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "config or packaging"`

### 6. `agent/subagents/manager_base.py` -- 751 lines

- **Current Problem**: 751 lines, base manager with state machine, lifecycle hooks, tool registration, and compatibility shims.
- **Function Count**: ~18 functions, 3 over 100-line limit.
- **Target Structure**:
  - `lifecycle_service.py` -- start/stop/health/recovery
  - `state_machine.py` -- subagent state transitions
  - `manager_base.py` -- facade with backward-compatible API (target: < 200 lines)
- **Split Steps**:
  1. Extract state machine transitions into `state_machine.py`
  2. Extract lifecycle hooks into `lifecycle_service.py`
  3. Reduce manager to delegation layer
- **Risk**: Medium -- state file format and CLI behavior must stay stable.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "subagent"`

### 7. `agent/log_analysis/analytics/detectors/rules.py` -- 745 lines

- **Current Problem**: 745 lines, routing rule evaluation, match scoring, and rule loading all in one file.
- **Function Count**: ~12 functions, 2 over 100-line limit.
- **Target Structure**:
  - `rule_models.py` -- rule dataclasses and enums
  - `rule_evaluator.py` -- rule matching and scoring
  - `rule_loader.py` -- rule file loading and caching
  - `rules.py` -- entry point (target: < 200 lines)
- **Split Steps**:
  1. Extract rule dataclasses into `rule_models.py`
  2. Extract evaluation logic into `rule_evaluator.py`
  3. Extract loading into `rule_loader.py`
- **Risk**: Low -- routing rules are well-tested and data-driven.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "memory_routing"`

---

## Priority 2: Near-hard-limit violations (500-600 lines)

### 8. `agent/log_analysis/tools.py` -- 672 lines

- **Current Problem**: 672 lines, tool registration and handler implementations for log analysis extension. Should be plugin-ized.
- **Target Structure**: `LogAnalysisPlugin` with `register_tools()`, separated handler functions.
- **Split Steps**:
  1. Define `ExtensionPlugin` interface (see ADR-0003)
  2. Create `LogAnalysisPlugin` implementing the interface
  3. Move tool handlers into plugin methods
  4. Keep registration thin
- **Risk**: Medium -- log analysis commands and test fixtures are numerous.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "log_analysis"`

### 9. `cli/memory_commands.py` -- 608 lines

- **Current Problem**: 608 lines, memory CLI commands with query, archive, routing, and doctor subcommands.
- **Target Structure**: Split into `memory_query_cmd.py`, `memory_archive_cmd.py`, `memory_doctor_cmd.py`.
- **Split Steps**:
  1. Extract query commands
  2. Extract archive commands
  3. Extract doctor commands
- **Risk**: Low -- CLI commands are thin wrappers.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "memory_command"`

### 10. `agent/gateway_parts/runtime.py` -- 657 lines (was 551, growing)

- **Current Problem**: 657 lines, runtime worker, request processing, output display, queue management, and recovery.
- **Target Structure**: `gateway_queue_service.py`, `request_worker.py`, `response_renderer.py`, `audit_service.py`.
- **Split Steps**:
  1. Extract request execution helper
  2. Extract queue iteration logic
  3. Extract response rendering
- **Risk**: High -- gateway recovery, lease management, and chunk streaming must not regress.
- **Verification**: `python3 -m pytest agent_py_agent/tests -q -k "gateway"`

---

## Priority 3: Soft-limit violations (400-600 lines)

These files should be monitored. New features should be placed in new modules, not added to these files.

- `cli/parser.py` -- CLI argument parser (split by subcommand domain)
- `agent/memory.py` -- memory orchestration (split by memory operation type)
- `agent/core.py` -- agent core (split into services)
- `agent/tools.py` -- tool registry (split by tool domain)
- `agent/notification/*.py` -- notification system (split by channel)
- `agent/session/*.py` -- session management (split by concern)

---

## Tracking

| Metric | Baseline (2026-05-03) | Target (2026-07-31) |
|--------|----------------------:|--------------------:|
| Hard findings | 149 | < 50 |
| Soft findings | 262 | < 150 |
| Files over 600 lines | 11 | 0 |
| Functions over 100 lines | 48 | < 10 |
| Classes over 350 lines | 15 | < 5 |

Update this table as refactoring progresses.
