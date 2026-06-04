# Development Rules / 开发规则

## Contract Hard Rules / 合同硬规则

- 禁止做任何专项合同。
- 合同层只能抽象通用能力，例如：状态机、执行、恢复、交付、验收、工具清单、错误分类、权限边界、ArtifactRef。
- 任务样例、测试样例、行业样例、格式样例可以存在，但它们只能作为数据或注册项，不能反向长进底层合同代码。
- 如果某个修复只能解释为“为了家具页/购物站/xlsx/PDF/某个 case 特判”，默认不允许进入合同层。
- 允许的做法是：把该问题上升为通用契约、通用状态、通用 validator 注册项、通用 recovery action，或者通用执行策略。
- 代码不得依赖普通自然语言文本作为机器事实来源。
- 中文或其他自然语言提示只允许作为软约束；真正的机器判断必须基于结构化字段、状态、refs、schema、工具记录、文件系统事实或显式配置。
- 开放世界禁止封闭枚举。文件格式、产物类型、协议、MIME type 等持续增长的概念，不能让写死映射表成为唯一判定路径；映射表只能作为已知类型优化。格式类交付应优先读取结构化 `file_extensions` / `acceptable_extensions` / `artifact_intent` / `mime_type`，未知但明确的后缀可从 key 自身推导，大类标签如“CAD 图纸”必须先物化成可接受扩展名，底层 locator 不靠行业知识猜一万种格式。

These rules keep the my-agent codebase maintainable, auditable, and safe for
multi-agent workflows.  Every contributor (human or LLM) must check these rules
before changing code.

本文件是项目工程治理的核心约定。所有代码变更（人工或 LLM 生成）都必须遵守。

---

## 1. Python Version / Python 版本

- **Target**: Python 3.10+ (`requires-python = ">=3.10"` in pyproject.toml).
- Do **not** use backslashes inside f-string expressions (PEP 701, only valid in 3.12+).
  Use temporary variables or parenthesised sub-expressions instead.
- For `tomllib`: use an import guard for `tomli` on Python < 3.11:
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

## 4. Code Shape / 代码形态

| Dimension       | Limit   | Enforcement                                   |
|-----------------|---------|-----------------------------------------------|
| File length     | advisory only | `CODE_SIZE_REPORT.md` trend report |
| New function    | <= 100 lines | `scripts/check_code_size.py`             |
| New class       | <= 250 lines (Mixin <= 200) | `scripts/check_code_size.py` |
| Function params | <= 8 (use dataclass bundling if more) | `scripts/check_code_size.py` |

- Whole-file line count is not a hard gate. Merge or split files based on
  call-path clarity, responsibility boundaries, and debugging cost.
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
- The same rule applies to tests: split near-soft test files into focused files
  or named assertion helpers. Do not treat test bloat as harmless, because it
  hides behavior boundaries from later humans and LLM agents.
- Service-facing APIs should accept one typed dataclass bundle, usually named
  `Params`, `Options`, `Context`, `Request`, `Command`, or `Query` according to
  intent.
- CLI functions may read `argparse.Namespace`, but must normalize it at the command
  boundary before calling agent/core/manager code.
- Business services should prefer `def execute(*, request: SomeRequest)` or
  `def run(*, options: SomeOptions)`. Old explicit keyword fields should be
  migrated into the same bundle and removed from product-facing service code;
  service-facing product code must not expose function-level `**kwargs`.
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
- The write boundary no longer treats `workspace_root` as the only valid output
  location.  `workspace_root` is the default cwd and relative-path base; ordinary
  absolute user output paths are allowed unless they fall under
  `path_dangerous_roots` while `path_access_mode=normal`.  See
  `FILE_WRITING_RULES.md` for the full policy.
- Agent shell access should use the single model-facing `run_command` tool.
  Command permissions come from the runtime `access_mode` config, not from
  model-authored `grant_id`, `command_allowlist`, `path_scope`, `apply`, or output
  budget fields. Historical controlled-exec internals must not be introduced
  into ordinary task prompts or default tool catalogs.
  Hidden/internal tools must also be blocked at execution time unless the caller
  supplies an explicit internal `allowed_tools` scope; hiding a tool from the
  catalog is not enough.
- Shell policy must be controlled rather than name-banned: ordinary cleanup such
  as `rm file`, `rm -rf build`, `rmdir tmp`, or `chmod 777 scratch` may run when it
  stays inside the configured access boundary.  Catastrophic actions such as
  deleting `/`, deleting system/home roots, writing raw disks with `dd`,
  formatting disks, or shutting down/rebooting remain hard-blocked even when
  `access_mode=full-access`.
- `run_command` must protect already-ready deliverables generically. Before shell
  execution, snapshot every `ready` artifact registry record that still points to
  a file. After shell exits, compare path existence, size and hash; if a file
  changed, re-run objective format lint when available. Valid changed files update
  the same `artifact_id`; missing/empty/format-broken files are registered
  `invalid` with `backup_ref`. Unknown formats are not treated as broken only
  because the system cannot parse them.

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
  refs-only advice, and final closeout checks.
- Parent/root agents that delegated work should stay refs-only by default, but
  may read orchestration artifacts such as dispatch summaries, subagent boards,
  due-check reports, status refs, and acceptance/test refs.  Product bodies and
  large child artifact bodies stay blocked until a real checker finishes or the
  current user prompt explicitly asks the parent to inspect the work.
- Self-authorized root/coordinator/lead runs do not write `capability_request`.
  They have no parent to ask, so ordinary task-local capability gaps must be
  handled by creating/routing lower agents, using existing tools, or reporting
  that the requested action is not currently supported.  Main-agent-created
  top-level workers/researchers/writers are different: their parent is the main
  agent even when their subagent `parent_id` is empty, so they may use the
  normal child capability request lane.  Root self-termination/uninstall policy
  is a future design topic and must not be modeled as an OPEN child capability
  request.
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
  explicit admin maintenance command owns that change.

## 7.2 Structured Contract Boundary / 结构化合同边界

- Iron rule: product code must not depend on ordinary natural-language prose as a
  machine fact source. User prompts, model summaries, reports, and display text
  may guide an LLM or a human, but routing, permission, acceptance, recovery,
  dispatch, artifact ownership, and state transitions must read structured
  fields, status codes, refs, schemas, tool records, or filesystem facts.
- Product code must not make hard business decisions from broad natural-language
  keyword lists. Examples of banned behavior: "用户说了测试就必须创建 tester",
  "goal 里出现修复就强制 repair", "summary 里有没有/不存在就反转验收结果",
  or "prompt 里提到下级就把 worker 改成 coordinator".
- Deterministic runtime decisions must read machine facts instead: protocol fields
  (`required_files`, `forbidden_files`, `required_read_paths`, `output_files`,
  `dependencies`, `workflow_task_type`, `workflow_template_id`, `delegate_only`,
  `refs_only`, `parent_body_read=allow`), role/template ids, status fields,
  failure codes, refs, task/workspace metadata, or explicit tool grants.
- Natural language is still allowed in user prompts, LLM-facing instructions,
  role/workflow template descriptions, user-visible messages, and test prompts.
  It may guide the model, but product code must not treat a prose phrase as the
  only source of truth for routing, permission, acceptance, or recovery.
- Tool syntax and error syntax are different from business intent. It is OK to
  parse command forms such as `cat file`, XML-ish tool markers, Python traceback
  names, path strings, file extensions, and protocol tokens because those are
  machine syntax or diagnostics, not guesses about what the user meant.
- 读路径不存在不是权限缺口，也不是任务终止信号。`read_file`、
  `list_files`、`search_text` 这类只读工具必须优先返回结构化
  `path_not_found` 和工作区内 `candidate_paths`，让模型自己确认候选或继续搜索。
  系统不能自动读取候选，也不能因为缺路径直接把任务卡死。
- 产物交付必须先进入统一 artifact registry。工具、子代理结果、closeout
  或后续修复链路只要确认一个用户交付物存在，就要登记为
  `artifact_id + path + hash + run/task/agent` 的机器记录。父代理、任务树、
  看板、closeout 和最终汇报优先读取 registry 记录；模型文本里的路径只能作为
  搜索/恢复提示，不能成为最终产物事实。
- 同一个产物移动、重建、修复或格式转换时，应更新同一个 `artifact_id` 的最新
  registry 记录，而不是制造一串互相竞争的“口头路径”。新增逻辑只能把
  registry 作为交付物事实源。
- 一个逻辑产物可以是一组文件。比如静态网站可以由 `index.html`、CSS、JS 和本地数据组成；
  这类产物要登记为同一个 `artifact_id` 的 file group。closeout 只读取
  `data/artifacts/registry.jsonl` 里的结构化文件组，不允许再靠扩展名扫描后
  把“多文件产物”误判成“候选太多”，也不允许新增第二套 artifact manifest 账本。
- `.agent_delivery/closeout.json` 是系统验收报告，不是产物账本；模型手写的
  `closeout.json`、`.artifact_manifest.json`、`artifacts_manifest.json` 都不能成为交付事实源。
- 如果任务明确不需要落盘产物，应使用 `requires_artifact: false` 或
  `delivery_mode: message`。这类任务可以通过 closeout 结束，但不能把“无产物”
  误报成 `ARTIFACT_REF_MISSING`。
- 最终交付物的系统硬验收只守客观事实：路径边界、存在性、非空、文件签名、
  文件包是否能被真实 reader 打开、hash/registry 状态和工具运行错误。文档厚度、
  覆盖比例、证据充分性、推荐理由质量、字段是否“有用”等业务质量，只能作为
  `warning` / advisory 返给模型或人工；不能直接把任务硬挡死。
- “全部 / 每个 / 所有 / 每周 / 每个项目”这类业务覆盖要求默认属于进度账本和
  coverage ledger 的软管理范围。系统可以提醒哪些条目缺证据、缺引用或只到
  README 级，但不能把这类业务质量塞进 closeout 变成硬门。
- 当用户明确要求“完整读完 / 完整读取 / 全文读完 / 从头到尾”某个源文件时，
  delivery materializer 可以补出 `target_coverage_contract`，把该源文件登记为
  required `full_source_read`。closeout 只用工具读文件记录里的客观
  `offset/chars/total_chars` 或 `start_line/end_line/total_lines` 区间验收：从开头
  连续覆盖到 EOF 才算完成；只读到部分不能因为最终报告存在就通过。这个规则
  只适用于可机器证明的来源读取覆盖，不能扩展成“报告质量/分析深度”的通用硬门。
- `task_progress` 里的普通 evidence/coverage 缺口是 advisory。它可以提醒模型补证据、
  补来源或继续完善报告，但不能在没有结构化 required 读取合同时，把“done 项证据
  不够多”或“覆盖清单没填满”升级成 closeout 硬阻断。
- closeout 返工上下文只能在模型明确调用 `submit_for_acceptance` 且本次验收未通过时
  注入一次。普通工具循环、长任务续跑、compact 续接和后续读写轮次不得反复复读旧
  closeout 失败，避免模型被历史验收噪音带偏。
- `write_file` 写入常见二进制交付物时必须先写临时文件并做客观格式验证，验证
  通过后再原子替换目标文件。验证失败时保留旧文件，并返回结构化错误让模型
  自己换方法修复；不要用坏候选覆盖上一次可打开的交付物。
- `run_command` 和 `write_file` 的保护边界不同：`write_file` 可以在覆盖前验证候选；
  shell 脚本可能自己原地改文件，所以必须在执行前备份 registry 里的 ready 产物，
  执行后把变化、坏包和备份位置写回 registry，而不是相信模型口头路径。
- If a feature needs a new hard requirement, add a structured field/schema first,
  document it, and add a regression that proves the same natural-language phrase
  alone does not trigger the hard behavior.

---

## 8. Comments and Docstrings / 注释和 docstring

```python
# BAD — restates the code
x = x + 1  # increment x

# GOOD — explains the reason
x = x + 1  # skip the sentinel row emitted by the exporter
```

- Do not add mechanical template comments such as `# LLM:` / `# 函数用途:` just to
  satisfy a pattern. They make future agents treat filler as architecture.
- Add a comment only when it answers a real maintenance question: why this
  boundary exists, which data source is authoritative, what side effect must not
  be moved, or which failure mode the code is protecting against.
- Prefer short docstrings or ordinary comments near the non-obvious decision.
  A function whose name and types already explain the behavior does not need a
  banner comment.
- Existing generated comments should be removed when touching nearby code unless
  they contain a concrete invariant that is still true.
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
4. `python scripts/check_code_size.py` — no local-complexity hard violations; whole-file length is advisory.
5. `git diff --check` — no whitespace errors.

## 11.1 Subagent Token / Model-Call Budget

- Status, board, startup recovery, due-check, action-plan, acceptance-plan,
  shared-progress, and memory-resume views must be deterministic local reads.
  They must not call an LLM, run a runner, execute tests, or expand artifact
  bodies unless the command name/flag explicitly says it will execute.
- Explicit model-call entry points must stay opt-in, such as
  `subagent-run --execute`, `subagents-dispatch --start-runners`, and
  planner paths that are clearly named as planner/model execution.
- Explicit command execution must stay opt-in, such as
  `subagents-tests --re-run`, `subagents-tests --execute-tests`, or
  `subagents-dispatch --start-runners`.
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
  refs-only until a real checker finishes or the current user prompt explicitly
  authorizes parent inspection, for example "you inspect it yourself".
  Parent agents may read hot runtime metadata (`task.json`, status reports,
  acceptance/test refs, handoff/takeover packets), but must not read product
  bodies or externalized artifact bodies as a substitute for tester,
  bug_finder, checker, repair, and retest children.
- Do not turn workflow preferences into hardcoded product behavior. The durable
  hard red line is self-system destruction risk: when the user asks agents to
  uninstall or break the agent system itself, delete system directories, remove
  required runtime/config files, or make the toolchain unrecoverable, require
  explicit safety handling. Normal cleanup is allowed: agents may delete their
  own temporary files, task trash, stale generated artifacts, or test
  directories inside the authorized workspace when that matches the task. For
  normal user work, prefer user intent, LLM planning, role templates, scoped
  permissions, audit logs, and acceptance facts over rigid scheduler rules. If
  users explicitly authorize a parent/root agent to inspect work
  itself, that current-run instruction should override the default delegation
  preference while still staying inside filesystem/tool boundaries.
- For remote CI pushes, use the strict remote-submit profile before pushing:
  focused tests for touched areas, full pytest when feasible, ruff, doc sync,
  strict code-size, and `git diff --check`. If not pushing remote, use the
  smaller local checklist appropriate to the change risk.
- Runtime, memory, compact, tool, orchestration, contract, or subagent behavior
  changes must update the matching project docs in the same patch. Do not leave
  behavior changes only in code or tests; future agents use the docs to avoid
  repeating old wrong designs.
- Tool-call budget is per agent run, not per task tree and not per conversation.
  Default policy is `tool_agent_budget_window_seconds=600` and
  `tool_agent_budget_max_calls=200`, keyed by `run_id`. Calls without a `run_id`
  are treated as ordinary main-agent chat and are not limited by this guard.
- Artifact body reads have their own per-run character budget. Default policy is
  `tool_artifact_read_budget_window_seconds=600` and
  `tool_artifact_read_budget_max_chars=240000`, keyed by `run_id`; `0` disables
  the budget. Read these two fields as one policy: "within this many seconds,
  this run may read up to this many artifact body chars." Prefer
  `read_artifact mode=search/head/tail` or small slices over full artifact reads.
  `read_file` may read registered `blobs/tool_outputs/*.json` wrappers as
  artifact content.
- A budget hit must be a recoverable self-check/handoff signal: return a bounded
  tool result asking the agent to summarize current progress, detect repeated
  tool use, and escalate to its parent if more tools are needed. Do not silently
  kill the runner, and do not charge sibling agents or the whole task tree.
- Do not add task-wide or conversation-wide tool budgets unless a future spec
  explicitly reopens that decision. Long-lived root/main-agent behavior should
  be handled by gateway/daemon/supervisor lifecycle, not by this per-run budget.
- Model-facing shell access should go through `run_command`. Runtime config
  decides the boundary with `access_mode`: `restricted`, `workspace-write`, or
  `full-access`. The model should not have to understand grant ids, command
  allowlists, path scopes, or apply flags just to run an ordinary command.
  Historical controlled-exec internals must not be introduced into ordinary
  prompts or default tool catalogs.
- Large generated file bodies must not travel as one giant tool-call JSON
  argument. `write_file.content` goes through `content_transport_policy.py`; the
  default recommended inline size is 12,000 characters and can be tuned with
  `tool_write_inline_max_chars`. If a valid parsed tool call exceeds that
  configured recommendation, the tool should preserve the content and return a
  warning; future calls should use smaller `write_file` writes, `apply_patch`
  for local diffs, or `run_command` under the current `access_mode` to generate
  the file and return only paths/summaries. Streaming stdout/stderr can improve
  observability, but it is not a fix for an oversized or malformed tool-call
  JSON block.
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
- Provider rate limits or temporary overloads must also be typed and recoverable.
  HTTP 429/529/503 and temporary 5xx should retry with bounded backoff first.
  During model turns, delivery-contract materialization, and subagent structured
  repair turns, retry the current model call with the shared provider transient
  delay schedule before surfacing `ProviderTransientError`.
  Do not restart the whole run for this path, do not repeat already completed
  tool calls, do not treat provider flakes as delivery-quality failures, and do
  not let raw provider JSON tracebacks become the final user-facing answer.
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
  searches for defects and counterexamples; checker prepares final closeout
  recommendations; coordinator/lead splits, broadcasts, corrects, rescues, and
  summarizes refs without defaulting to writing final product artifacts.
- Explicit QA role requirements are machine contracts, not prose suggestions.
  If a parent goal or closeout check names `tester`, `bug_finder`, or
  `checker` (including the Chinese role names), detection must flow through
  `qa_role_contract.py`. QA roles themselves are terminal reviewer roles and
  must not be forced to spawn another same-role child just because their own
  goal contains `tester`, `bug_finder`, or `checker`. Scheduling should expose
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
  - `2`: runner close-out, hierarchy scheduling, closeout
    decision/next-action, and other critical state transitions.
  - `3`: report and loop summaries such as due-check, action-plan,
    recovery-tree, dispatch, dispatch-watch, bounded status snapshots,
    normalization counts, and guard decisions.
  - `4`: bounded prompt/response/tool refs only; still no body expansion.
  - `5`: maximum local diagnostics for short controlled test windows.
- Current approved event families are lifecycle (`task_created`), runner
  close-out (`runner_result_recorded`), hierarchy fan-out
  (`hierarchy_schedule_result`), closeout control-plane events
  (`final_closeout_decision`, `final_closeout_next_action`), and bounded
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
- Behavior defaults belong in config. Bootstrap code, parser help text, and
  tests may repeat defaults only when they mirror YAML and cannot become a
  second independent policy source.
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
  Runtime adjustments go through the admin/runtime `CapabilityConfigPatchRequest`
  service so field validation, allowlist policy, version checks, audit, and reload
  behavior stay centralized. Do not expose this as an ordinary model-facing task
  tool; configuration changes belong to explicit user/admin or system repair paths.
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
- Preserve user-facing CLI names and active data formats unless the user
  explicitly approves a breaking change.
- When deleting an old path or field, delete its callers, tests, and docs in the
  same change. Do not add a new shim or temporary bridge.
