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
- For remote CI pushes, use the strict remote-submit profile before pushing:
  focused tests for touched areas, full pytest when feasible, ruff, doc sync,
  strict code-size, and `git diff --check`. If not pushing remote, use the
  smaller local checklist appropriate to the change risk.

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

---

## 12. Reversibility / 可逆性

- Prefer small, reversible changes over broad rewrites.
- Preserve public CLI names and existing data formats unless an ADR (Architecture
  Decision Record) in `docs/decisions/` approves the breaking change.
- When deprecating, add a compatibility shim and a removal timeline.
