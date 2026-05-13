# Development Rules / 开发规则

These rules keep the my-agent codebase maintainable, auditable, and safe for
multi-agent workflows.  Every contributor (human or LLM) must check these rules
before changing code.

本文件是项目工程治理的核心约定。所有代码变更（人工或 LLM 生成）都必须遵守。

---

## 1. Python Version Compatibility / Python 版本兼容性

- **Target**: Python 3.10+ (`requires-python = ">=3.10"` in pyproject.toml).
- Do **not** use backslashes inside f-string expressions (PEP 701, only valid in 3.12+).
  Use temporary variables or parenthesised sub-expressions instead.
- For `tomllib`: use a guarded fallback to `tomli` on Python < 3.11:
  ```python
  try:
      import tomllib
  except ModuleNotFoundError:
      import tomli as tomllib  # type: ignore[no-redef]
  ```
- Do not use `match` statements with guards that rely on 3.10+ structural pattern
  matching edge cases — keep patterns simple.

---

## 2. Import Discipline / 导入纪律

- **No `import *` allowed.** Every import must name the symbols explicitly.
  The architecture guardrails test (`test_architecture_guardrails.py`) freezes the
  current star-import baseline at zero and will fail if new ones appear.
- Prefer absolute imports from the package root (`from agent_py_agent.agent...`).
- Keep third-party imports minimal: the runtime dependency list in `pyproject.toml`
  is intentionally small (`prompt_toolkit`).  New runtime deps require a design note.

---

## 3. Naming Conventions / 命名规范

- **No generic filenames.** The following names are banned for new files:
  `utils.py`, `helpers.py`, `common.py`, `misc.py`, `temp.py`, `new.py`, `final.py`.
  Use specific names that describe the single responsibility, e.g.
  `task_repository.py`, `memory_route_matcher.py`, `log_field_accessor.py`.
- Existing junk-named files are tracked in the `JUNK_NAME_BASELINE` set inside
  `test_architecture_guardrails.py`.  Do not add new entries.
- Module names: `snake_case`.  Class names: `PascalCase`.  Functions/variables:
  `snake_case`.  Constants: `UPPER_SNAKE_CASE`.

---

## 4. Code Size Limits / 代码尺寸限制

| Dimension       | Limit   | Enforcement                                   |
|-----------------|---------|-----------------------------------------------|
| New file        | <= 400 lines (test <= 700) | `scripts/check_code_size.py` |
| New function    | <= 100 lines | `scripts/check_code_size.py`             |
| New class       | <= 250 lines (Mixin <= 200) | `scripts/check_code_size.py` |
| Function params | <= 8 (use dataclass bundling if more) | `scripts/check_code_size.py` |
| Entry-point file | frozen baseline | `test_architecture_guardrails.py` |

- These limits apply to **new** code.  Existing files that exceed limits are tracked
  as baselines; they must not grow further without a refactoring plan.
- If a function approaches 80 lines, start decomposing it into named helpers.
- Params over 8 must use dataclass bundling: `def f(*, params: SomeParams)`.

## 4.1 Bundle Interface Standard / 统一 Bundle 接口规范

- Product service / domain / repository / gateway / subagent / memory / tool /
  skill / delegation interfaces must not use loose `*args` or `**kwargs` as
  business parameter entry points.
- When a function reaches the code-size high-risk band because related values
  keep growing, split those values into a named dataclass bundle instead of
  raising limits. Recent examples include `ResumeGuidanceRequest`,
  `ProviderTrashRequest`, and chat/TUI render request objects.
- Service-facing APIs should accept one typed dataclass bundle, usually named
  `Params`, `Options`, `Context`, `Request`, `Command`, or `Query` according to
  intent.
- CLI functions may read `argparse.Namespace`, but must normalize it at the command
  boundary before calling agent/core/manager code.
- Business services should prefer `def execute(*, request: SomeRequest)` or
  `def run(*, options: SomeOptions)`. Compatibility wrappers may keep old
  explicit keyword fields, but they must immediately convert those fields into
  the same bundle; service-facing product code must not expose function-level
  `**kwargs`.
- Do not add new behavior flags as loose kwargs to an existing service method.
  Extend the existing bundle and update focused tests instead.
- Bundles should stay small and domain-specific. If a bundle starts mixing unrelated
  concerns, split it rather than passing a generic dict.
- A small number of low-level infrastructure utilities may keep `*args` /
  `**kwargs` only when they must transparently forward arbitrary callable
  signatures, such as retry/decorator/adapter/wrapper code. These utilities must
  not become product service APIs.
- Test mocks, fixtures, and helpers may keep `**kwargs`, but tests must not use
  them as the reference style for product interfaces.
- Every product-code exception must be registered in
  `BUNDLE_VARARG_FUNCTION_EXEMPTIONS` or `BUNDLE_KWARG_FUNCTION_EXEMPTIONS` in
  `test_architecture_guardrails.py` with a reason. New function-level `*args` /
  `**kwargs` service interfaces fail the guardrail unless the exception is
  reviewed and documented first.

---

## 5. Entry Points Stay Thin / 入口保持精简

- CLI entry points (`__main__.py`, `cli/parser.py`, `cli/chat.py`) must delegate to
  services.  Parsing logic and business logic must not live in the same function.
- Pattern: `parse_args()` -> `resolve_service()` -> `service.execute()`.
- New CLI commands: register the parser in one place, execute in a separate service
  module under `agent/` or `cli/`.

---

## 6. State Changes Must Be Auditable / 状态变更必须可审计

- Any mutation of persistent state (task status, memory, configuration) must produce
  an audit trail entry — either a log line, an event record, or a task record update.
- The `Audit` class and the archive system are the canonical audit backends.
- Do not silently swallow state-changing errors; surface them to the caller or log
  them with enough context for post-mortem analysis.

---

## 7. Write Boundary / 写入边界

- All file writes must go through a repository or the write-boundary utility
  (`tooling/filesystem.py`).  Business code must **not** call `Path.write_text()` or
  `open(..., "w")` directly.
- The write boundary enforces: workspace root containment, no path traversal, no
  system path writes.  See `FILE_WRITING_RULES.md` for the full policy.
- Subagent shell/exec access must go through `controlled_exec`, and `controlled_exec`
  must read authority from `write_boundary.controlled_exec_grants`.  Do not add
  product interfaces where a child agent can self-authorize `command_allowlist`,
  `path_scope`, `network_scope`, or output budget from its own tool params.
- Delete-like child-agent operations must route to task-local trash.  Do not expose
  `rm`/`rmdir`/`unlink` as direct shell execution for subagents.

## 7.1 Guard Boundary / 守卫边界

- Guards are safety rails, not project managers.  They may hard-block red-line
  risks such as writing outside authorized roots, path traversal, system directory
  deletion, unapproved shell authority, recursive self-destruction, and operations
  that make my-agent unable to boot or recover.
- Guards should not hardcode normal workflow preferences.  Duplicate coordinator
  domains, multiple QA/review agents inspecting the same deliverable root, repeated
  repair workers, and shared product files should be warnings/audit facts unless
  they cross a real write/safety boundary.
- Follow the 长期助手/通道运行时 split: execution/tool/path/self-termination
  safety belongs in the hard guard layer; planning order, QA wave timing, repair
  strategy, and role selection belong to LLM role templates, workflow templates,
  refs-only advice, and final acceptance checks.
- Parent/root agents that delegated work should stay refs-only by default, but
  may read orchestration artifacts such as dispatch summaries, subagent boards,
  due-check reports, status refs, and acceptance/test refs.  Product bodies and
  large child artifact bodies stay blocked until a real acceptor finishes or the
  current user prompt explicitly asks the parent to inspect or accept the work.
- Root runs do not write `capability_request`.  A root has no parent to ask, so
  ordinary task-local capability gaps must be handled by creating/routing lower
  agents, using existing tools, or reporting that the requested action is not
  currently supported.  Root self-termination/uninstall policy is a future
  design topic and must not be modeled as an OPEN child capability request.
- Cleanup is allowed inside authorized workspaces when it matches the task:
  temporary files, task trash, generated artifacts, task-local memory, drafts,
  templates, tools, and skills may be removed.  The hard line is uninstalling or
  disabling my-agent itself.  If a user asks to uninstall my-agent, explain manual
  steps; do not execute the uninstall or delete required runtime/core files.
- Provider user/group spaces must be resolved through `agent.user_space.provider_space`.
  External users and groups may write only inside their own provider space. Group
  owners/admins may destructively manage their own group space, but destructive
  actions must go to scoped trash and write an audit event. They must not write
  owner home, another user, another group, or provider root metadata unless an
  explicit admin migration command owns that change.

---

## 8. Comments and Docstrings / 注释和 docstring

```python
# BAD — restates the code
x = x + 1  # increment x

# GOOD — explains the reason
x = x + 1  # skip the sentinel row that the legacy exporter always emits
```

- Every product-code module, class, function, and method must have a two-layer
  comment block above the definition. For decorated definitions, place it above
  the first decorator:
  ```python
  # LLM: contract, callers, side effects, and invariants for future models.
  # 函数用途: plain-language purpose, call timing, and edit notes for humans.
  def example(...):
      ...
  ```
- Modules use `模块用途:`. Classes use `类用途:`. Functions and methods use `函数用途:`.
- `LLM:` is for future coding agents: mention module ownership, stable contract,
  side effects, caller expectations, and tests/docs to check after edits.
- `模块用途:` / `函数用途:` / `类用途:` is for humans: explain what it does, when it
  is called, and what a beginner should inspect before changing it.
- Do not use mechanical filler such as “handles this small logic block”; comments
  must describe the actual responsibility visible in the code.
- Existing `新手说明:` / `参数说明:` / `返回说明:` blocks may remain as additional
  detail, but they do not replace the required `LLM:` + human-purpose line.
- Inline comments are for non-obvious decisions, workarounds, and domain constraints.
- Code-size spans count implementation lines, not comment/docstring lines. Do not weaken
  required comments to satisfy size checks; split real implementation when the
  implementation itself approaches the limit.
- Frontend files under `frontend/` are excluded from the Python strict code-size
  guard. They must instead pass the frontend gates: `npm run check:config`,
  `npm run lint`, and `npm run build` from `frontend/`. This keeps Python
  architecture guardrails focused while still making UI changes testable.

---

## 9. Guard Clauses Over Deep Nesting / 优先使用守卫子句

```python
# BAD
if user is not None:
    if user.is_active:
        if user.has_permission("write"):
            do_write()

# GOOD
if user is None:
    return
if not user.is_active:
    return
if not user.has_permission("write"):
    return
do_write()
```

- Early returns reduce cognitive load and make error paths explicit.
- Maximum nesting depth: 3 levels.  Beyond that, extract a helper.

---

## 10. Feature Spec Required / 新功能需要功能规格

- Any user-visible behavior change requires a Feature Spec document before
  implementation.  See `FEATURE_SPEC_RULES.md`.
- Mechanical refactors, documentation-only changes, and test-only guardrails are
  exempt.

---

## 11. Pre-Commit Checklist / 提交前检查清单

1. `python -m compileall -q agent_py_agent scripts` — syntax check.
2. `python -m pytest agent_py_agent/tests/ -q` — all tests pass.
3. `ruff check agent_py_agent` — no lint errors.
4. `python scripts/check_code_size.py` — no size limit violations.
5. `git diff --check` — no whitespace errors.

## 11.1 Subagent Token / Model-Call Budget

- Status, board, startup recovery, due-check, action-plan, acceptance-plan,
  shared-progress, and memory-resume views must be deterministic local reads.
  They must not call an LLM, run a runner, execute tests, or expand artifact
  bodies unless the command name/flag explicitly says it will execute.
- Explicit model-call entry points must stay opt-in, such as
  `subagent-run --execute`, `subagents-dispatch --execute-runners`, and
  planner paths that are clearly named as planner/model execution.
- Explicit command execution must stay opt-in, such as
  `subagents-tests --re-run`, `subagents-acceptance --execute-tests`, or
  `subagents-dispatch --execute-acceptance-tests`.
- Default lookup surfaces must be refs-only: show ids, status, summaries,
  counts, hashes, sizes, and file refs. Do not read or inline
  `logs/runner_prompt.md`, `logs/runner_response.md`, externalized tool
  outputs, artifact bodies, or large report bodies in status/board/startup
  paths.
- New subagent files should be classified as hot metadata or cold body data.
  Hot metadata may be read by status and scheduler code; cold body data should
  be opened only by an explicit detail, inspect, resume, or acceptance command.
- Any new status/search/scheduler feature must add or reuse a regression test
  proving it does not read cold runner logs or artifact bodies on the default
  path.
- When users ask for progress or quality checks, prefer refs-only reporting and
  checker subagent roles over expanding the main agent context with large
  artifacts. Reporter/checker roles may collect ids, summaries, status, and
  evidence refs, but they should not auto-read cold bodies or promote facts
  into main memory without an explicit gate.
- When a parent/root agent has delegated work to child agents, it must stay
  refs-only until a real acceptor finishes or the current user prompt explicitly
  authorizes parent inspection, for example "you inspect/accept it yourself".
  Parent agents may read hot runtime metadata (`task.json`, status reports,
  acceptance/test refs, handoff/takeover packets), but must not read product
  bodies or externalized artifact bodies as a substitute for tester,
  bug_finder, acceptor, repair, and retest children.
- Do not turn workflow preferences into hardcoded product behavior. The durable
  hard red line is self-system destruction risk: when the user asks agents to
  uninstall or break the agent system itself, delete system directories, remove
  required runtime/config files, or make the toolchain unrecoverable, require
  explicit safety handling. Normal cleanup is allowed: agents may delete their
  own temporary files, task trash, stale generated artifacts, or test
  directories inside the authorized workspace when that matches the task. For
  normal user work, prefer user intent, LLM planning, role templates, scoped
  permissions, audit logs, and acceptance facts over rigid scheduler rules. If
  users explicitly authorize a parent/root agent to inspect or accept work
  itself, that current-run instruction should override the default delegation
  preference while still staying inside filesystem/tool boundaries.
- For remote CI pushes, use the strict remote-submit profile before pushing:
  focused tests for touched areas, full pytest when feasible, ruff, doc sync,
  strict code-size, and `git diff --check`. If not pushing remote, use the
  smaller local checklist appropriate to the change risk.
- Tool-call budget is per agent run, not per task tree and not per conversation.
  Default policy is `tool_agent_budget_window_seconds=600` and
  `tool_agent_budget_max_calls=50`, keyed by `run_id`. Calls without a `run_id`
  are treated as ordinary main-agent chat and are not limited by this guard.
- Artifact body reads have their own per-run character budget. Default policy is
  `tool_artifact_read_budget_window_seconds=600` and
  `tool_artifact_read_budget_max_chars=240000`, keyed by `run_id`; `0` disables
  the budget. Read these two fields as one policy: "within this many seconds,
  this run may read up to this many artifact body chars." Prefer
  `read_artifact mode=search/head/tail` or small slices over full artifact reads.
  `read_file` must never be used to open
  `memory_archive/artifacts/tool_outputs/*.json` wrapper files.
- A budget hit must be a recoverable self-check/handoff signal: return a bounded
  tool result asking the agent to summarize current progress, detect repeated
  tool use, and escalate to its parent if more tools are needed. Do not silently
  kill the runner, and do not charge sibling agents or the whole task tree.
- Do not add task-wide or conversation-wide tool budgets unless a future spec
  explicitly reopens that decision. Long-lived root/main-agent behavior should
  be handled by gateway/daemon/supervisor lifecycle, not by this per-run budget.
- Future shell/exec access for subagents must go through a controlled gateway:
  workspace-bound paths, no raw destructive commands for lower agents, `trash`
  instead of direct `rm`, bounded output capture, and audit records. Directory
  permission can make read/write commands low-friction, but it must not bypass
  path containment, output-size guards, or tool/skill request escalation.
- Subagent exec requests must be grant-backed. The model may request a command,
  cwd, or output intent, but parent `CapabilityGrant` must supply the actual
  command allowlist, path scope, network scope, and output budget before the
  request reaches shell execution. Do not let tool parameters become self-issued
  authorization.
- Large generated file bodies must not travel as one giant tool-call JSON
  argument. `write_file` / `append_file` content goes through
  `content_transport_policy.py`; the default trial inline hard limit is 12,000
  characters and can be tuned with `tool_write_inline_max_chars`. If content
  exceeds that configured limit, the caller must use a short skeleton plus
  bounded `append_file` chunks, a small
  `replace_in_file`/patch edit, or a grant-backed `controlled_exec` path that
  writes inside the allowed workspace and returns only refs/audit metadata.
  Streaming stdout/stderr can improve observability, but it is not a fix for an
  oversized or malformed tool-call JSON block.
- Tool prompt budgets must be long-term config-backed. If a tool/catalog/search
  threshold affects runtime behavior, put it in `agent_config.yaml`,
  `AgentConfig`, the normalizer, and the frontend runtime config together; do
  not leave a second hardcoded default in UI/store/tool code.
- Long-content recovery must be policy-driven. If a write-like tool parse error
  or inline-limit rejection needs to guide the next model turn, put that rule in
  `content_recovery_mode.py` and append a compact `[tool-system]` recovery mode;
  do not copy another long Chinese hint into the tool loop, parser, or individual
  tool class.
- Provider/network timeouts must be typed and recoverable. HTTP backends should
  raise `ProviderTimeoutError` for request/stream timeouts, runners should record
  `failure_type=provider_timeout`, and CLI entry points should print a compact
  recovery handoff rather than exposing a raw traceback or staying silent.
- New write-like tools must reuse `content_transport_policy.py` or document a
  reviewed exception. Do not create a second hardcoded chunk-size or parse-error
  hint in a separate module.
- Model-facing tool descriptions are product contracts, not casual comments.
  If a rule is shared by more than one tool or prompt, put it behind a named
  helper/policy function and reference that helper from the tool spec, parser
  hint, and runner prompt. Avoid copying long Chinese/English guidance strings
  into each tool class; duplicated prose drifts and makes later model-behavior
  fixes unreliable.

## 11.2 Real E2E Findings Ledger

- During real subagent E2E runs, append every discovered product or workflow
  issue to `docs/modules/subagent/06-real-e2e-findings.md`.
- Each entry should include the scene, discovery time, symptom, root cause,
  fix or routing decision, verification command/result, current status, and
  remaining risk.
- Keep the ledger append-only. Do not rewrite old findings except for narrow
  typo/path corrections.
- When changing runner-context dispatch or acceptance behavior, document the
  difference between top-level manual apply and runner-context auto-closure.
  Tests must prove top-level dispatch remains non-mutating while an active
  parent runner can close only its own direct children after explicit tests pass.
- Coordinator/root acceptance must be tied to machine facts. If a coordinator
  has direct children and no executable tests, use a deterministic
  child-acceptance check against direct child `DONE/VERIFIED` state instead of
  trusting model-written completion text.
- Subagent role templates must stay external and broad. Built-ins live under
  `agent_py_agent/agent/subagents/role_template_catalog/builtin/*.json`; user
  templates live under `.agent/subagents/roles/*.json` by default. A template
  should describe a reusable role such as bug finding, testing, acceptance,
  coordination, writing, or research, not a one-off action like checking one
  button. Include Chinese fields (`name_zh`, `summary_zh`, `use_when_zh`,
  `output_contract_zh`) so humans and LLMs can both read it.
- Role selection guidance is part of the delegation contract. Root and
  coordinator/lead prompts should keep a short index of when to use each broad
  role, then load details only when dispatching. Worker/writer produce real
  artifacts; researcher gathers facts; tester verifies behavior; bug_finder
  searches for defects and counterexamples; acceptor prepares final acceptance
  recommendations; coordinator/lead splits, broadcasts, corrects, rescues, and
  summarizes refs without defaulting to writing final product artifacts.
- Explicit QA role requirements are machine contracts, not prose suggestions.
  If a parent goal or acceptance check names `tester`, `bug_finder`, or
  `acceptor` (including the Chinese role names), detection must flow through
  `qa_role_contract.py`. QA roles themselves are terminal reviewer roles and
  must not be forced to spawn another same-role child just because their own
  goal contains `tester`, `bug_finder`, or `acceptor`. Scheduling should expose
  quality advice for the LLM to choose scope/order, while acceptance verifies
  real persisted descendant roles instead of trusting summaries.
- Runner-context dispatch suggestions must not accidentally re-enable generic
  workflow expansion. When a parent is merely continuing existing direct
  children, the suggested `dispatch_subagents` call should use
  `workflow_mode=off`; broad `workflow_mode=auto` is for deliberate top-level
  workflow planning, not for child closeout/retry loops.
- Empty user-facing subagent tool config means automatic policy. Keep
  `subagent_allowed_tools=[]` as “role/template/task decides tools”, not “no
  tools”. Only use a non-empty global list for deliberately restricted test
  environments.

## 11.3 Subagent Debug Trace Levels / 子代理调试追踪等级

- `subagent_debug_trace_level` is a formal test observability switch, not a
  temporary `print` habit. Default `0` must stay completely silent.
- Levels `1-5` are for real E2E and fault-injection runs. They must write only
  to the internal runtime workspace, currently
  `debug_traces/subagent_trace.jsonl`, and must not pollute user deliverables.
- Trace records must be refs-only and bounded by default: ids, hierarchy depth,
  role, status, verification status, short previews, counts, and file refs.
  Do not copy prompt bodies, response bodies, artifact bodies, or tool output
  bodies into trace records.
- Level guidance:
  - `1`: lifecycle checkpoints such as task creation.
  - `2`: runner close-out, hierarchy scheduling, parent acceptance
    decision/next-action, and other critical state transitions.
  - `3`: report and loop summaries such as due-check, action-plan,
    recovery-tree, dispatch, dispatch-watch, bounded status snapshots,
    normalization counts, and guard decisions.
  - `4`: bounded prompt/response/tool refs only; still no body expansion.
  - `5`: maximum local diagnostics for short controlled test windows.
- Current approved event families are lifecycle (`task_created`), runner
  close-out (`runner_result_recorded`), hierarchy fan-out
  (`hierarchy_schedule_result`), parent acceptance control-plane events
  (`parent_acceptance_decision`, `parent_acceptance_next_action`), and bounded
  report summaries (`due_check_report`, `action_plan_report`,
  `hierarchy_recovery_packet`, `dispatch_report`, `dispatch_watch_report`).
- Any new trace event must have focused tests proving `0` writes nothing and
  enabled levels write bounded internal records.
- Trace expansion must happen only at stable lifecycle boundaries. Avoid
  logging inside tight loops or per-token/per-tool-output paths; use counts,
  ids, and refs instead.
- When a real E2E exposes a new trace need, record the reason in
  `docs/modules/subagent/06-real-e2e-findings.md` before expanding trace detail.

## 11.4 Backend Config Ownership / 后端配置所有权

- Product behavior defaults must live in backend config, not as scattered
  literals in service code. Use `AgentConfig` plus
  `agent_py_agent/config/agent_config.yaml` for runtime defaults that affect
  memory, compact/resume, tools, subagents, dispatch, gateway, chat, CLI
  limits, budgets, timeouts, preview sizes, and scan windows.
- New backend parameters need three pieces at the same time: an `AgentConfig`
  field, a documented YAML entry, and normalization in the settings services
  when the value is numeric/bool/list-like. Call sites should read the resolved
  config value after `make_agent()` or through a bundle that already carries
  config.
- Argparse defaults for behavior-affecting numbers should be `None` when the
  real default comes from config. A literal `0` is allowed only when it is an
  explicit user meaning such as “unlimited”, “disabled”, or “do not execute”.
- Fallback literals are allowed only at bootstrap or compatibility boundaries:
  dataclass defaults, parser help text, tests, transparent wrappers, and code
  paths that must survive missing/broken config. They must mirror YAML defaults
  and must not become a second independent policy source.
- Config fields that are intentionally internal do not need frontend exposure
  yet, but they still belong in backend config if changing them affects
  runtime behavior. User-facing frontend config should later read these backend
  fields instead of copying hardcoded frontend defaults.
- Before pushing a change that adds or changes backend config, run at least:
  `python -m py_compile`, focused tests for touched modules, `ruff check`, and
  a `load_config(agent_config.yaml)` smoke test. When pushing to remote without
  GitHub Actions, run the strict local gate first.

## 11.5 Runtime Config Self-Healing / 运行期配置自修复

- Agents must not raw-edit `agent_py_agent/config/capability_config.yaml`.
  Runtime adjustments go through `CapabilityConfigPatchRequest` and the
  `capability_config_patch` tool so field validation, allowlist policy,
  version checks, audit, and reload behavior stay centralized.
- Safe capability fields may be auto-applied only when they are listed in the
  runtime config allowlist. Global behavior switches and unknown fields must
  return suggestions such as `manual_approval_required` instead of mutating
  config silently.
- Every runtime config write needs a content-version check when the caller has
  a version, plus a JSONL audit record. Version mismatch means “stop and
  re-read”, not “overwrite”.
- Config reload affects future dispatch/watch/retry/takeover cycles only. Do
  not mutate already-running subagent prompts, grants, or execution contexts
  in-place.
- User-facing docs and tool specs should tell the model how to use the patch
  lane, so ordinary users do not need to know YAML field names to recover from
  obvious config limits.

---

## 12. Reversibility / 可逆性

- Prefer small, reversible changes over broad rewrites.
- Preserve public CLI names and existing data formats unless an ADR (Architecture
  Decision Record) in `docs/decisions/` approves the breaking change.
- When deprecating, add a compatibility shim and a removal timeline.
