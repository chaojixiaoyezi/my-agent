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

## 0. Development Iron Rules / 开发铁律

本节是所有开发工作的总纲。后面的合同、状态、写入、测试和注释规则都必须服从这里。

### 0.1 Natural Language Boundary / 自然语言边界

- 自然语言只表达意图，不当机器事实源。用户 prompt、模型 summary、报告正文、
  guidance、角色描述和展示文案都不能直接变成任务状态、验收结果、权限、路由或硬门结论。
- 硬判断必须来自结构化证据。运行时决策只能读取工具返回、exit code、schema 字段、
  refs、artifact registry、coverage ledger、run/subagent status、显式配置、文件系统事实
  或结构化 control event。
- 禁止用关键词猜用户意思。不要用“完成 / 失败 / 停止 / 不要停 / blocked / done”
  这类中英文词表改变状态；反话、错别字、多语言和上下文都会让它失真。
- 软提示可以有，硬门不能靠提示词。prompt 可以提醒模型补证据、看子代理和运行测试；
  但底层必须通过结构化状态和工具记录保证链路可恢复、可审计。
- 停止、取消、暂停、恢复、接管和授权这类控制动作必须走结构化入口；
  不能靠解析一句普通自然语言来触发。
- 模型应基于当前 run 的产物记录、工具记录、测试结果和状态记录判断并说明完成情况；runtime 不解析
  “完成了”“已验证”等正文来驱动机器状态。
- 子代理模板、role description、skills 描述只影响 LLM 工作风格；runtime 不能从这些
  prose 里推断权限、能力、验收门或状态机。
- 当前用户消息优先于旧记忆、旧上下文和旧报告。历史内容只能辅助理解，不能覆盖本轮
  用户明确要求。

### 0.2 Code Elegance and Main Chain / 代码优雅和主链路

- 主链路优先，兜底最后。主链路没跑顺之前，不加 fallback、旧路径兼容、影子入口或
  “还能跑”的旁路。
- 一个概念只允许一个权威位置。task workspace、memory、artifact、subagent state、
  compact ledger、config 都必须有唯一 canonical path / canonical schema。
- `--no-save` 不赋予工具绕开当前 owner 权限的能力。运行身份与用户业务目录分离，
  宿主记录使用 canonical runs 根；不因一次运行强制创建 tasks/output/work 业务目录。
- 主代理和普通子代理的文件权限范围是自己的 owner home；所有普通相对路径以真实 cwd 解析，
  output/work/tasks 名称不作魔法映射。文件整理与目录命名由稳定内置提示说明，用户可覆盖整理习惯。
- `output_files` / `output_refs` / `artifact_refs` 是明确的文件引用，不推导额外写权限，
  也不触发 task 回绑或参数改写。跨 owner、控制面、SOUL 确认与 exact Audit 继续服从结构化授权。
- 不同时保留新旧两套路由。旧字段、旧目录、旧 facade、旧 fallback 确认不用就删；
  迁移必须短期、显式、有删除条件。
- 配置必须单一来源。用户配置、默认 YAML、dataclass 默认值和测试覆盖不能互相打架；
  配置改了必须真实影响运行链路。
- 状态机要小而明确。状态字段使用有限、结构化、可审计的值；别名和自然语言说明放
  notes/summary，不进入机器状态。
- 状态别名不做隐式兼容。`completed`、`succeeded`、`ok`、`ERROR`、多语言词表或
  历史标签不能自动升格为当前协议的 `DONE` / `FAILED` / `final`；需要兼容时必须先写
  显式迁移或结构化转换记录，默认按未知值 fail closed。
- 状态协议值必须精确匹配。`done`、`timeout`、`verified`、`ok` 这类大小写变体也不能在
  验收、恢复、auto-resume 或 transition 合同里自动改写成 `DONE` / `TIMEOUT` / `VERIFIED` / `OK`。
- 错误正文不是错误码。工具、runner、gateway 或子代理没有显式 `error_code` /
  `error_type` 时，运行时只能写 `UNKNOWN_ERROR` 或对应结构化本地失败类型；不能从
  message、stdout、stderr、summary 里用关键词反推出硬错误码并影响状态、恢复或验收。
- 不同结构化协议之间可以做显式映射，但映射源必须是当前协议枚举。例如 subagent
  `failure_type=runner_timeout` 可以映射到 error taxonomy 的 `RUNNER_TIMEOUT`；
  `runner_last_error`、stdout/stderr、报告正文和用户提示词不能参与这个映射。
- `failure_type` 进入任务状态前必须经过当前枚举校验。模型结果包、action envelope、
  runner response 可以保留原始 `failure_type` 供审计，但 `task.failure_type`、重试策略、
  恢复策略和验收只能消费 `known_failure_type()` 认可的当前协议值；未知值按无结构化失败类型处理。
- recovery mode 和 capability status 只认当前协议枚举，不能用字符串前缀、英文词片段、
  中文词片段或旧别名来触发自动重跑、接管、授权或任务状态迁移。
- collaboration case/request status 也只走当前协议枚举和 normalize helper；`resolved`、
  `done`、`rejected` 这类展示词只能保留为文本，不能驱动关闭、完成、阻塞或唤醒。
  写入协作账本时，非协议状态只能落到 metadata 的 raw/status_protocol_error 字段，
  `case.status` / `request.status` 必须保持当前协议值不变；想 reopen 或完成必须显式
  写 `open` / `completed` 等当前协议值。底层 store update 方法也不能绕过这条规则。
- owner capability request 的状态只认当前协议值；未知值必须报结构化错误，不能自动兜底成
  `closed` / `expired` / `approved` 这类终态。
- 工具执行权只能来自 `allowed_tools`、owner tool policy、默认隐藏策略和现有结构化 grant
  账本；一次 run 的目录、搜索、Schema 与执行必须复用同一 runtime snapshot。不存在通用
  `granted_capabilities` 字符串旁路；用户 prompt 里的普通文本、协议样短语或工具名都不能自动扩权。
- 子代理 capability grant 不能替代一次具体工具批准。child 遇到 ToolExecutor `ask` 时必须保留完整
  `ToolApprovalRequest` 并经所属 owner 的标准 TUI/Web 审批队列处理；批准只按 exact
  `tool_name/run_id/operation_id/idempotency_key/args_hash` 续跑原 ToolCall。无交互 consumer、断线、取消、
  终态、损坏或写回失败一律 fail closed，不能从 child 名称、展示行、模型回复或 guidance 猜批准。
- capability request 的自动路由只能消费结构化能力字段，例如 `needed_capability`、
  `requested_tools`、`requested_skills`、`requested_mcp_tools`、`requested_commands`、
  `constraints`。`task.goal`、`problem`、`expected_output`、summary、evidence 文本可以给
  父代理阅读和审计，但不能混进自动 grant 的匹配 query。
- 错误要显性，不要糊成“还能跑”。启动失败、通道断开、子代理挂掉、artifact 丢失、
  config 未生效，都应暴露 typed failure，而不是伪装成 planning/running。
- 工具不要重复造。已有工具能表达的能力，优先修底层语义或扩展明确参数；只有交互模式
  真的不同才新增工具。
- 安全门可以硬，业务判断交给模型。危险路径、危险命令、越权写入、破坏运行时可以硬拦；
  深度不足、子代理未汇总和报告质量问题应作为真实运行事实提供给模型，不得变成隐藏完成硬门。
- 结构化 delivery contract 明确列出的 artifact 格式要求可在对应写入/验证工具中检查，例如
  `required_columns`、`required_fields` 或 MIME/文件签名；它们不能自动成为普通任务完成状态机。
  普通自然语言里的报告维度、分析角度和对比口径更不能自动升级成格式硬门。
- 跨平台从第一天考虑。路径用 `pathlib` 和配置解析，不写死 macOS 家目录；Windows、
  Linux、macOS 都应能解释用户目录、相对路径和工作目录。
- 大输出和 compact 是底层能力，不是 prompt 技巧。大文件、大工具输出、长任务必须依赖
  chunk、cursor、coverage ledger、archive、resume summary，不靠模型口头记忆。
- 真实测试优先于漂亮单测。runtime、gateway、subagent、compact 这类能力必须用真实
  LLM、真实 PTY、真实长任务验收；单测只能证明局部。
- 参考成熟项目先于自己发明。底座以 会话运行时 为第一参考：会话、active turn、Compact、Skill、
  工具、计划、子代理、停止和引导只要 会话运行时 有明确代码路径，就适配现有 owner/thread/task
  事实源，不另造平行主链。长期助手 只补长期 Memory、Persona、多用户持久调度和被动验证，
  通道运行时 只补 IM adapter、通道健康和投递边界。
- 代码可以长，但链路要直。文件行数不是硬门；比起十几个只转发的 facade，一个清楚的
  核心文件更容易排查。
- 每次改语义，文档同步。路径、状态、compact、验收、工具、配置、架构边界或长期规则
  有变化时，同一轮 diff 必须更新对应文档；没有文档变更也要说明原因。

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
| File length     | no limit / no finding | governed by architecture clarity, not line count |
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
- Test files are report-only for strict code-size: test findings may remain as
  advisory signals, but they must not create strict blockers or hard/high-risk
  pressure. Any path with a `tests/` segment is test scope for this gate. Clean
  test structure when it helps readability, not to satisfy the strict gate.
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

- All file writes must go through a repository, common JSON/text writer, or the current
  write-boundary implementation (`agent.tooling._filesystem_write`).  Business code must **not** call `Path.write_text()` or
  `open(..., "w")` directly.
- The write boundary no longer treats `workspace_root` as the only valid output
  location.  `workspace_root` is the default cwd and relative-path base; ordinary
  absolute user output paths are allowed unless they fall under
  `path_dangerous_roots` while `path_access_mode=normal`.  See
  `FILE_WRITING_RULES.md` for the full policy.
- When a runner, subagent, or internal caller supplies non-empty
  `allowed_write_roots`, those roots are an execution boundary, not just prompt
  context. Writes outside them, including sibling task/date directories reached
  through an absolute path or symlink, must be rejected. User-requested output
  directories remain valid by being explicitly included in that boundary.
- The same structured boundary must reach every process-launching surface. Shell,
  background shell, PTY, and LSP use the bwrap mount graph (read-only owner base plus
  exact writable overlays); do not inspect command text or natural language to infer
  write intent. Persistent PTY/LSP processes must not be reused across owner/task
  boundary identities.
- Agent shell access should use the single model-facing `run_command` tool.
  Command permissions come from the runtime `access_mode` config, not from
  model-authored `grant_id`, `command_allowlist`, `path_scope`, `apply`, or output
  budget fields. Historical controlled-exec internals must not be introduced
  into ordinary task prompts or default tool catalogs.
  Hidden/internal tools must also be blocked at execution time unless the caller
  supplies an explicit internal `allowed_tools` scope; hiding a tool from the
  catalog is not enough.
- Persistent commands must use the same tool's structured `run_in_background=true`
  parameter and the returned process session. Do not append shell `&`, combine
  `nohup ... &`, or otherwise create an unregistered background child; readiness
  checks must run in a later tool call against that managed session.
- A managed background process is a dangerous effect even when its command parser
  would otherwise classify the foreground command as mutating or read-only. The
  current bwrap policy shares host networking and the process outlives one handler
  call, so `sandbox=required` cannot bypass its exact tool approval binding. A new
  `terminal_session(action=start)` has the same boundary and must request approval;
  subsequent write/read/close calls only transport or close that already-approved
  session, matching 会话运行时 `write_stdin`. These decisions use only structured tool
  parameters declared by `SandboxPolicy`, never prompt semantics or tool-name branches.
- A subagent capability grant limits tool, command, path, and network scope; it is
  not user approval for a concrete side effect. Every execution surface still uses
  the parent-inherited exact approval policy. `controlled_exec` currently calls
  `subprocess.Popen` without an OS sandbox, so it must declare `sandbox=none` and
  require exact approval for `apply=true`. Change that declaration only when the
  executor is demonstrably sandboxed, never because a capability grant exists.
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
- Follow the 会话运行时 split first: execution/tool/path/self-termination
  safety belongs in the hard guard layer; planning order, QA wave timing, repair
  strategy, and role selection belong to Skills, LLM role templates and typed
  task state. Consult 长期助手 only for persistent multi-user gaps that 会话运行时 does
  not cover; an IM adapter must never create a second guard or planning runtime.
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
- Subagent `capability_request.status` is an exact current protocol field:
  `OPEN`, `GRANTED`, `GAP`, or `CLOSED`.  Historical aliases such as
  `RESOLVED`, `APPROVED`, or `REJECTED` must not silently close, grant, or route
  a request as if they were current schema values. Case variants such as `open`
  or `granted` are not protocol values for this field.
- Subagent task lifecycle decisions must use the shared `TaskStatus` /
  `VerificationStatus` helpers in `subagents.models` for done/verified,
  failure, ended, dispatch-ineligible, and handled terminal checks.  Do not
  recreate local status alias tables or prose-based state transitions in
  dispatch, recovery, compact, board, or closeout modules.
- Machine status protocols must be exact per field.  Main/subagent run states
  use uppercase protocol values such as `DONE`; task progress, collaboration,
  tool protocol, and compact health use their documented lowercase protocol
  values such as `done`, `completed`, `succeeded`, or `ok`. Do not call
  `.upper()` or `.lower()` to make a status participate in completion,
  recovery, dispatch, compact resume, or closeout decisions.
  Unknown task-progress status values normalize to `unknown` and preserve the
  original text only in `raw_status` / `status_protocol_error`.
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
  `dependencies`, `task_id`, `run_id`, `delegate_only`,
  `refs_only`, `parent_body_read=allow`), role/template ids, status fields,
  failure codes, refs, task/workspace metadata, or explicit tool grants.
- Natural language is still allowed in user prompts, LLM-facing instructions,
  Skill bodies, role template descriptions, user-visible messages, and test prompts.
  It may guide the model, but product code must not treat a prose phrase as the
  only source of truth for routing, permission, acceptance, or recovery.
- Tool syntax and error syntax are different from business intent. It is OK to
  parse current protocol markers, Python traceback names, path strings, file
  extensions, and protocol tokens because those are machine syntax or
  diagnostics, not guesses about what the user meant. Runtime code must not
  execute misspelled tool names, old tool aliases, wrapped parameter bundles, or
  shell-like command phrases as if they were current tool calls.
- 读路径不存在不是权限缺口，也不是任务终止信号。`read_file`、
  `list_files`、`search_text` 这类只读工具必须优先返回结构化
  `path_not_found` 和工作区内 `candidate_paths`，让模型自己确认候选或继续搜索。
  系统不能自动读取候选，也不能因为缺路径直接把任务卡死。
- 产物交付必须先进入统一 artifact registry。工具或子代理结果只要确认一个用户交付物存在，就要登记为
  `artifact_id + path + hash + run/task/agent` 的机器记录。父代理、任务树、
  看板、附件发送和最终汇报优先读取 registry 记录；模型文本里的路径只能作为搜索/恢复提示，不能成为
  最终产物事实。registry 不决定普通任务是否完成。
- 同一个产物移动、重建、修复或格式转换时，应更新同一个 `artifact_id` 的最新
  registry 记录，而不是制造一串互相竞争的“口头路径”。新增逻辑只能把
  registry 作为交付物事实源。
- 一个逻辑产物可以是一组文件。比如静态网站可以由 `index.html`、CSS、JS 和本地数据组成；
  这类产物要登记为同一个 `artifact_id` 的 file group。不得扫描整个 task/output 目录推断任务完成，
  也不得新增第二套 artifact manifest 或验收账本。
- 普通任务与 会话运行时 一样由模型基于当前对话、真实工具结果、测试结果和子代理结果判断是否完成，随后以
  自然最终回复结束当前回合。不得新增 `submit_for_acceptance`、完成 marker、目录验收器或回复后的第二次
  总结模型调用。
- 分析、问答和建议任务不要求文件产物。需要文件的任务由模型按用户要求使用工具完成；文件格式、路径、
  权限和原子覆盖校验属于工具安全边界，不是普通任务完成状态机。
- `task_progress`、coverage ledger 和读文件游标是模型工作记忆与 compact/resume 事实，不是完成硬门。
  runtime 可以把缺项、未读范围和失败测试呈现给模型，但不得据此覆盖模型最终回复或自动启动返工循环。
- 普通自然语言中的“全部”“完整”“不要漏”等要求由模型理解；产品代码不能把这些词升级成隐藏合同、
  权限、路由或完成状态。默认 chat/CLI/Gateway 也不得从路径或文件系统扫描自动物化验收合同。
- 主代理 run 或 current attempt 的结构化 `unknown` 表示执行结果/副作用尚未核清。后台 scheduler 必须
  保留对应 wake、observation、policy 并停止模型和工具挂载；不得捕获冲突后按异常文案重试，也不得新建
  平行主 run。显式恢复必须核对 current attempt、复原崩溃调和写入的 run 状态并只释放该 attempt 的锁。
  被阻塞的旧任务不得占满同线程其它 task 的事件消费窗口。
- 外部调用方若显式提供结构化 `delivery_contract`，它只约束明确给出的路径、格式和权限事实，并随
  runtime/compact 保存；它不得恢复旧的普通任务验收器或要求模型调用提交工具。
- compact work state 必须保存大文件读取游标和当前 task/run 的结构化事实，不能把“文件名出现过”当作
  完整读取，也不能用另一个 workspace 或旧 run 的工具记录给当前工作背书。
- `/goal` 的持久目标工具只有 `get_goal`、`create_goal`、`update_goal`。create 只能来自用户或系统显式
  请求；update 只允许 `complete` 或 `blocked`。目标预算、用量限制、暂停和自动续跑只读结构化 goal state，
  不从模型正文或文件存在性推断。
- `task_progress.items[].status` 是机器状态字段。工具入口只接受
  `pending` / `in_progress` / `done` / `skipped` / `blocked`；`completed`、
  “已完成”“已验收”“read”“ok”这类自然语言或自定义标签必须写到
  `notes` / `summary`，不能写入 `status`。读取旧账本时，非协议值会进入
  `unknown` 统计桶并保留 `raw_status`，不会被补猜成 pending 或 done。
- `task_progress.action` 是工具协议字段，只接受 `read` / `update`；不能把
  `create` / `init` / `begin` / `write` 等旧别名自动兜底成写入。
- `write_file` 写入常见二进制交付物时必须先写临时文件并做客观格式验证，验证
  通过后再原子替换目标文件。验证失败时保留旧文件，并返回结构化错误让模型
  自己换方法修复；不要用坏候选覆盖上一次可打开的交付物。
- `run_command` 和 `write_file` 的保护边界不同：`write_file` 可以在覆盖前验证候选；
  shell 脚本可能自己原地改文件，所以必须在执行前备份 registry 里的 ready 产物，
  执行后把变化、坏包和备份位置写回 registry，而不是相信模型口头路径。
- If a feature needs a new hard requirement, add a structured field/schema first,
  document it, and add a regression that proves the same natural-language phrase
  alone does not trigger the hard behavior.
- Tool-call validity is a structured contract, not a prompt convention. Model-facing
  examples must use real top-level fields from a real tool; placeholder keys such
  as `parameter_name` are forbidden. The registry must reject unknown top-level
  tool parameters with `TOOL_INVALID_ARGUMENTS` unless the field is declared as an
  internal, non-rendered parameter for CLI/test/runtime plumbing.

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
- Do not push small cleanup slices to remote `main` just because combined churn
  is large. A remote push is allowed only when the current diff has more than
  8000 insertions or more than 8000 deletions as separate counters; additions
  and deletions must not be added together to meet this threshold. User
  overrides and urgent fixes still require the strict remote-submit profile.
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
  `read_file` may read registered task `work/blobs/tool_outputs/*.json` wrappers as
  artifact content.
- Tool outputs are externalized only when they are large enough to threaten the
  live prompt. Moderate extraction/search results should stay inline so the next
  model turn can immediately use the facts, while blob/archive refs still keep
  the full output auditable for compact and recovery.
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
  warning; future calls should use smaller `write_file` writes, explicit
  `write_file mode=append` chunks for long reports, `WRITE_FILE_RAW
  mode="append"` blocks for raw text chunks, `apply_patch` for local diffs, or
  `run_command` under the current `access_mode` to generate the file and return
  only paths/summaries. Streaming stdout/stderr can improve observability, but
  it is not a fix for an oversized or malformed tool-call JSON block.
- For current task output/work files, repeated `write_file` calls to the same
  existing path are treated as continuation when `mode` is omitted: the registry
  rewrites that call to append. This only protects task-scoped artifacts; ordinary
  workspace files still use overwrite-by-default unless `mode="append"` is set.
- Implicit task output/work append must return model-visible soft feedback that
  names the rewrite and says explicit `mode="overwrite"` is required for a clean
  replacement. Do not make the model infer this from file contents.
- Never commit an unclosed `write_file.content` as a partial artifact. Incomplete
  JSON content in any directory, including current task output/work, is a host-owned
  protocol violation with zero canonical calls, zero handler executions, and zero
  operations. Never salvage a content prefix, invent a pseudo tool, or register a
  partial artifact. The bounded repair turn must emit a new, complete overwrite or
  append call before any file mutation is authorized.
- Artifact registration must use the latest successful canonical ToolResult for each
  output path. There is no partial-write success marker: an incomplete call has no
  ToolCall/ToolResult pair and therefore cannot become a ready attachment.
- Streaming tool-call boundaries are an observability and cleanup layer, not a
  one-tool execution limiter. If a model streams a complete `[TOOL_CALL]` and
  then continues with more machine blocks in the same assistant turn, the
  runtime must wait for the assistant turn to finish and execute the full parsed
  batch only when the entire response is a sequence of complete standalone blocks.
  Live UI filtering may suppress machine frames, but the protocol adapter must inspect
  the whole final response; leading/trailing prose, Markdown, bad JSON or a missing
  close marker invalidates the whole proposed batch. The runtime must not abort the
  provider request just because the first complete block arrived.
- Tool prompt budgets must be long-term config-backed. If a tool/catalog/search
  threshold affects runtime behavior, put it in `agent_config.yaml`,
  `AgentConfig`, the normalizer, and the frontend runtime config together; do
  not leave a second hardcoded default in UI/store/tool code.
- Error taxonomy is keyed by explicit machine codes. A tool, backend, or runner
  must return structured `error_code` / `failure_type` when it wants recovery
  routing. Do not classify ordinary stderr, traceback text, provider prose,
  multilingual phrases, or human summaries into machine error codes.
- Long-content recovery must be policy-driven and structured. If a write-like
  tool parse error or inline-limit abort needs to guide the next model turn, the
  parser/tool-stream layer should return compact machine fields such as
  `previous_write_committed=false`, `write_recovery.strategy`, and concrete
  overwrite/append tool-call shapes. Do not copy another long Chinese hint into
  the tool loop or infer recovery from ordinary prose.
- `write_recovery.max_chunk_chars` is a retry recommendation, not the streaming
  hard stop. A complete medium-sized `write_file`/`WRITE_FILE_RAW` block above
  the recommended chunk size should reach the normal tool boundary and either
  execute with a warning or fail through the shared inline hard limit.
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
- When changing child startup or completion behavior, document the boundary
  between the model-visible recursive control surface and the internal host
  dispatcher. Tests must prove that `create_subagents` auto-starts children at
  every supported level and that no model-visible dispatch/schedule tool is
  required for progress.
- Ordinary coordinator/root completion follows the shared structured
  `turn_end` reason and natural model result. Machine facts remain authoritative
  for tool success, permissions, paths, cancellation and interruption, but must
  not be turned into a second `DONE/VERIFIED` quality-acceptance state.
- Subagent role templates must stay external and broad. Built-ins live under
  `agent_py_agent/agent/subagents/role_template_catalog/builtin/*.json`; user
  templates live under `.agent/subagents/roles/*.json` by default. A template
  should describe a reusable role such as bug finding, testing, acceptance,
  coordination, writing, or research, not a one-off action like checking one
  button. Include Chinese fields (`name_zh`, `summary_zh`, `use_when_zh`,
  `output_contract_zh`) so humans and LLMs can both read it.
- Role behavior is template data, not runtime magic. Runtime code may read
  structured template fields such as `can_spawn_children`, `can_run_tests`,
  `depends_on_outputs`, and `output_contract`, but it must not hard-code role
  id lists or infer role behavior from `agent_name` or user prose. `role`
  selects the template; `agent_name` is only a display name. Template selection
  may use exact template ids and documented aliases, but not substring matching
  such as treating `qa_tester` as `tester`.
- Explicit QA role requirements are machine contracts, not prose suggestions.
  They must come from structured fields such as `required_qa_roles` and role
  template capability snapshots, not from scanning parent goals or closeout
  prose. Scheduling should expose quality advice for the LLM to choose
  scope/order, while acceptance verifies real persisted descendant roles and
  template snapshots instead of trusting summaries.
- Runner-context dispatch continues only explicitly persisted direct children.
  It must not expand a hidden workflow or create extra children from a mode flag;
  planning stays in the current thread through Skills, `task_progress`, and the
  native subagent tools.
- Empty user-facing subagent tool config means automatic policy. Keep
  `subagent_allowed_tools=[]` as “role/template/task decides tools”, not “no
  tools”. Only use a non-empty global list for deliberately restricted test
  environments.
- Subagent result artifacts must come from the current result schema only:
  `artifacts`, `artifact_refs`, `evidence kind=artifact`, or
  `evidence_packets[].artifact_refs`. Do not reintroduce old result aliases such
  as `deliverables`, `files`, `output_files`, `files_modified`, top-level
  `path`, or top-level `file_path` as machine artifact facts. Task-creation
  `output_files` remains a target-path contract, not a result alias.
- Parent agents should summarize child work from the model-visible result
  surface: `child_output_read_order`, `primary_artifact_refs`,
  `expected_outputs`, and artifact refs. Task-local `work/agents/<run_id>/`
  files are internal audit/recovery state, not the normal parent status or
  aggregation surface. Child state changes arrive through host lifecycle
  events; `/status` and TUI may read the internal tree projection. Do not
  reintroduce model-facing inspect/wait/raise-event tools, shell sleeps, or
  directory scraping as control flow.
- Background subagent dispatch must inherit the parent agent's current
  `workspace_root` as a structured runtime field. Do not let the background
  process cwd pick the subagent tree; cwd is only for loading code.
- `create_subagents` must not reject ordinary delegation because of
  domain-specific quality constraints such as button/image/comment rules. Pass
  those constraints as structured task context and validate them through child
  refs, QA, or closeout evidence. Keep create-time hard stops for true runtime
  boundaries only, such as invalid schema, dangerous write roots, disabled
  subagents, or missing required tool arguments.
- Test/runner failure routing must use structured execution facts such as
  `executed`, `passed`, `exit_code`, `validation_method`, and
  `validation_result.reason`. Stdout, stderr, traceback text, and human error
  summaries may be persisted as bounded audit snippets, but they must not decide
  recovery category, takeover, closeout, or retry behavior.
- Test validation kind must come from structured `validation_method`. Do not
  infer validators from `command` strings such as `static_site_check`; command
  text is execution input, not a schema selector. File existence validation uses
  the canonical `validation_method="file_check"`; do not reintroduce
  `file_exists`, `path_exists`, or `artifact_exists` as executable validator
  aliases.
- Collaboration capability matching must use explicit capability fields from
  task attributes/templates plus exact tool names. Do not infer abstract
  capabilities such as `query`, `write`, `evidence_submission`, or `delegate`
  from substrings in tool names or role prose.
- Tool/action boolean parameters must be JSON booleans, numbers, or the exact
  strings `true`/`false`/`1`/`0`. Do not treat natural-language words such as
  `yes`, `on`, `apply`, `execute`, `run`, or `full` as hard action switches.

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
- Security-sensitive env/config switches must prefer explicit machine tokens
  such as `true/false` or `1/0`. Do not let everyday words such as `yes` or
  `on` widen network, filesystem, or execution boundaries.
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
