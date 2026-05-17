# Main Agent Kernel Design / 主代理内核设计

本文定义 my-agent 后续优先打硬的主代理内核。这里说的主代理，是用户直接用 `my-agent` 命令聊天时看到的那个 agent。后续子代理要尽量复用这套内核，只换记忆边界、任务目录和父子关系，不再另写一套更脆的“小工人系统”。

## 目标

主代理内核要做到：

- 能从短平快任务跑到长任务，不因为上下文、路径、工具结果变多就乱。
- 能把每一轮任务的关键上下文做成结构化交接，而不是靠模型从自然语言里猜。
- 能把大文件、大输出、大产物放到外置 artifact（外置产物引用），prompt 里只放 refs（引用路径）。
- 能手动 compact（压缩上下文）和 resume（恢复继续）时稳稳接住任务。
- 能让 skill（可复用能力）、tools（工具）、memory（记忆）、workspace（工作目录）和后续 subagent（子代理）都复用同一个底座。

## 不做什么

- 不把用户提示词里某几个中文词写死成代码规则。
- 不靠越来越多 guard（保护规则）把代理绑死。
- 不让配置项爆炸到用户看不懂。
- 不把子代理当低配工具人。子代理以后应该是换了记忆和任务目录的小主代理。

## 一次主代理任务怎么跑

下面是用户发一条消息后，主代理一轮运行的标准链路：

1. `run()` 收到用户任务。

   `run()` 是主入口。它把旧式散参数统一成 `RunParams`（运行参数包），例如 `request_id`、`run_id`、`task_id`、`save`、`context_scope`。

2. 准备上下文。

   `_prepare_runtime_context()` 会准备几类信息：

   - related memory（相关记忆）：从长期 memory（记忆）里搜索和当前任务相关的内容。
   - routed memory（路由记忆）：根据规则找应该读的记忆路径。
   - auto resume context（自动恢复上下文）：如果上一轮有恢复线索，会生成恢复提示。
   - main context bundle（主代理上下文包）：本阶段新增的结构化交接包。

3. 生成 `Main Agent Context Bundle v1`。

   context bundle（上下文包）是给模型看的机器字段集合。它告诉模型：

   - 当前真实工作区在哪里。
   - `~/.my-agent` 家目录在哪里。
   - 这轮的 `request_id`、`run_id`、`task_id` 是什么。
   - 可用的 memory refs（记忆引用）有哪些。
   - compact/resume 的恢复 refs（恢复引用）在哪里。

   它不复制大正文。大正文要通过路径或 artifact ref（产物引用）再读。

4. 构建 prompt。

   `PromptBuilder` 会按顺序拼：

   - System（系统提示）：主代理基本行为。
   - Related Memory（相关记忆）：搜索到的长期记忆。
   - Dynamic Prompt Files（动态提示文件）：`AGENTS.md`、`SOUL.md`、`USER.md`、`memory.md` 等。
   - Runtime Injection（运行时注入）：context bundle、resume 提示、路由记忆提示。
   - Workspace Context（工作区上下文）：真实路径和日期。
   - Tools（工具目录）：可用工具。
   - User Task（用户任务）。

5. 工具循环。

   `ToolLoopService`（工具循环服务）把 prompt 发给模型。如果模型要调用工具，就执行工具，再把工具结果放回模型上下文，直到模型给最终回答或达到工具循环边界。

6. 收尾保存。

   `FinalizationService`（收尾服务）负责：

   - 写 legacy memory（兼容旧记忆）。
   - 写 daily memory（按天流水）。
   - 写 run archive（运行归档）。
   - 写 recovery snapshot（恢复快照）。
   - 写 task workspace（任务工作区）。
   - 估算 token（上下文用量）。
   - 触发 compact suggestion/auto cycle（压缩建议或自动压缩链路）。

7. 返回 `AgentRunResult`。

   `AgentRunResult`（运行结果对象）会带回最终回答、prompt、工具轮数、压缩状态、恢复状态，以及本轮 context bundle 的 JSON/Markdown 路径。

## Context Bundle v1

本阶段落地的是主代理 `Context Bundle v1`。

### 文件位置

普通保存运行会写：

```text
~/.my-agent/memory_archive/snapshots/context_bundles/YYYY-MM-DD/<request-or-run>.json
~/.my-agent/memory_archive/snapshots/context_bundles/YYYY-MM-DD/<request-or-run>.md
```

同一天目录下还会更新：

```text
latest_context_bundle.json
latest_context_bundle.md
```

### `save=False` 边界

如果用户或 CLI 使用 `save=False` / `--no-save`，主代理仍然可以在 prompt 里临时看到 context bundle，但不会把 bundle 写到硬盘。这和旧的 no-save 语义一致：不保存就是不落盘。

### `task_local` 边界

如果运行是 `context_scope="task_local"` 或 `context_scope="control_plane"`，不会注入主代理 context bundle。原因是这些上下文常用于子代理、本地任务或控制面，不能污染主代理长期记忆和主账号入口文件。

### 当前字段

```text
schema                         # main_context_bundle.v1
identity.owner_type            # main_agent
identity.owner_id              # 当前 owner 标识，主代理默认 root
identity.root_run_id           # 预留：子代理复用内核时指向根 run
identity.parent_run_id         # 预留：子代理复用内核时指向父 run
scope.request_id               # 本次请求 id
scope.run_id                   # 本次运行 id
scope.task_id                  # 任务 id
schema_policy                  # 结构版本迁移和缺字段降级规则
owner_model                    # 主代理/子代理 owner 字段模型
run_scope                      # 可写根、禁写根、锁定文件和路径风格
workspace_refs                 # 工作区、家目录、任务根目录、产物根目录
task.user_prompt_preview       # 用户任务短预览，不复制全文
acceptance_contract            # 本轮怎么才算完成、约束和最近测试状态
memory_refs                    # 记忆数量、路由路径、daily/key memory/lessons/indexes
recovery_refs                  # compact apply、snapshot、tokens 等恢复入口
tooling                        # 工具上下文计数和工具网关类型
tool_manifest                  # 模型可见工具、实际可执行工具和失败分类
artifact_refs                  # 本轮重要外置产物引用，只放 ref 不放正文
prompt_budget                  # 注入 prompt 的 bundle 摘要最大长度
self_check                     # bundle 写完后对关键引用和字段做自检
reserved                       # 以后扩展字段
```

### 合同完整性

`Context Bundle v1` 现在不只是“路径摘要”，而是一份主代理运行合同。这里的合同不是法律合同，而是系统内部各模块都能理解的机器字段：

- scope match（范围匹配）：`memory-compact --apply` 自动绑定最近 context bundle 前，会检查 request/run/task scope 是否一致；不一致时不会把无关任务卡塞进恢复包。用户显式传 `--main-context-bundle-ref` 时仍保留引用，但会记录 `explicit_scope_mismatch` 警告。
- RunScope（运行范围）：记录 primary workspace、allowed write roots、forbidden write roots、locked files 和 path style，恢复时不只知道“在哪”，也知道“哪里能写、哪里不能写”。
- ToolManifest（工具清单）：记录 visible tools（模型看得到的工具）、executable tools（实际可执行工具）、permission mode（权限模式）、failure taxonomy（工具失败分类），避免恢复后把工具失败误判成模型失败。
- self-check（自检）：bundle 写完后检查 required fields、workspace root、my-agent home、memory root、compact applies root 和 prompt budget；软缺失会进 warning，关键缺失会标记 `ok=false`。
- schema migration policy（结构版本迁移策略）：明确 `main_context_bundle.v1` 的 required fields、optional fields、缺字段降级方式和 unknown fields 处理方式，后续加字段不需要推倒旧包。
- prompt size budget（上下文包注入预算）：完整 JSON 留在文件里，prompt 只注入核心摘要，默认 capped，避免以后字段越加越多把上下文撑大。
- ArtifactRef（产物引用）：保存型 run 收尾后会按同 scope 把 tool-output artifact refs 回填到 context bundle；恢复时仍只给 ref/hash/size/call id，不读正文。
- Acceptance（验收合同）：记录 acceptance、constraints、latest_tests；没有明确事实源时保持空，不从模型回复里猜。
- subagent-compatible owner model（子代理兼容归属模型）：主代理现在是 `owner_type=main_agent`，同时预留 `subagent`、`parent_run_id`、`root_run_id` 等字段，后续子代理复用同一内核时不用再长出另一套上下文包。
- observability surface（可观察入口）：新增 `my-agent context-bundle latest --json`，可以只读查看最新任务卡、scope、run scope、tool manifest、验收合同和自检状态。

## 为什么这一步重要

以前很多问题来自“模型从自然语言里猜事实”：

- 猜错路径。
- 把摘要当成工具参数。
- 不知道自己这轮的 `run_id`。
- 忘记保存边界。
- 不知道恢复时该先读哪个 packet（继续包）或 snapshot（快照）。

context bundle 把这些变成机器字段。大白话说，就是每次开工前给主代理一张“任务身份证 + 地图 + 恢复入口卡片”。

## 和其他模块的关系

### Memory（记忆）

memory 负责存长期事实和按天流水。context bundle 不替代 memory，它只告诉模型 memory 的入口在哪里、当前搜索到了多少条。

### Compact（上下文压缩）

compact 会在上下文快满或用户手动触发时，把当前任务状态压缩成恢复包。context bundle 给 compact/resume 提供稳定 scope（范围）和 refs（引用），避免恢复时找错任务。

### Artifact（外置产物）

artifact 是大输出、大文件、工具结果的外置存放。context bundle 只放 artifact 根目录或引用，不把正文塞进 prompt。

### Tool Gateway（工具网关）

工具网关负责统一执行读写、搜索、shell、HTTP 等工具。context bundle 只记录当前工具上下文形状，不复制工具目录全文。

### Skill（技能）

skill 是以后主代理可复用的能力包。context bundle 会成为 skill 选择和 skill 学习的基础输入之一，但本阶段不做 skill 自动学习。

### Subagent（子代理）

子代理以后应该复用这套主代理内核。区别是：

- owner_type 从 `main_agent` 换成 `subagent`。
- my-agent home 从主账号空间换成 task-local 或 provider-local 空间。
- memory refs 不读主代理长期记忆，只读任务交接 refs。
- parent/child 关系会额外进入 bundle。

## Execution Contracts / 执行合同层

为了避免继续靠“发现一个问题就加一个 guard”，主代理内核新增底层合同层：

```text
agent_py_agent/agent/contracts/
|-- error_taxonomy.py   # 统一错误分类和恢复建议
|-- state_machine.py    # 统一运行状态事实和调度/收口判断
|-- idempotency.py      # 统一幂等键和操作编号
`-- e2e_matrix.py      # 真实 E2E 矩阵的机器可读定义
```

这些文件不是写死工作流。它们只提供事实和合同：

- Error Taxonomy（错误分类体系）：把路径错误、写入禁止、工具不可用、模型上游失败、产物缺失、验收失败、compact 引用缺失等失败分成稳定代码，并给出恢复建议。它不自动替模型决定重试，只告诉上层“这类失败是什么”。
- State Machine（状态机）：把 `PLANNING`、`RUNNING`、`WAITING_FOR_TOOL`、`BLOCKED`、`FAILED`、`DONE`、`VERIFIED` 等状态变成统一事实判断。它不规定每个任务必须走固定流程，只回答“现在能不能 dispatch、能不能 closeout、该等、修、接管还是人工看”。
- Idempotency Contract（幂等合同）：给 create/schedule/dispatch/compact/resume/artifact write 这类动作提供稳定 key。模型多调用一次工具时，系统可以复用已有 run 或跳过已完成项，而不是无限创建重复任务。
- Real E2E Matrix（真实端到端测试矩阵）：把必须长期跑的真实链路做成数据合同，例如中文路径写文件、大工具输出 artifact、compact 后 resume、工具失败分类、验收失败后修复、长任务中断恢复、单代理完整任务、子代理复用主代理 kernel。

大白话说：这四个合同不是给代理套枷锁，而是让系统在出问题时有统一语言。它们回答“坏在哪、现在是什么状态、这个操作是不是重复、真实链路有没有测过”。

当前接入点：

- `create_subagents` 输出 `operation_contract`，包含 `idempotency_key`、`operation_id`、created/reused/dispatch run ids。它不阻止重复调用，只把“这是同一个操作”的事实写出来，方便后续调度层复用。
- `dispatch_subagents` / `create_subagents` 的 `current_turn_run_state` 复用统一 State Machine，输出 `state_machine_contract` 和 `recovery_recommendations`。父级能看到 blocked/failed run 的错误类型、建议动作和中文恢复提示，不必从自然语言摘要里猜。
- 显式命名的小傻妞现在把名字当作结构化身份；默认泛名仍用 goal/write-root 等字段区分。这避免“同一个小傻妞目标文字稍微变了就重复创建”，也避免默认 worker 把不同任务误合并。

## 当前已落地

- 新增 `agent.user_space.context_bundle`。
- `SimpleAgent.run()` 的准备上下文阶段会生成 `Main Agent Context Bundle v1`。
- 普通保存运行会写 JSON 和 Markdown 镜像。
- `save=False` 不落盘。
- `task_local` / `control_plane` 不注入主代理 bundle。
- `AgentRunResult` 增加 `main_context_bundle_path` 和 `main_context_bundle_markdown_path`。
- focused tests 覆盖保存、no-save、task-local 三个边界。
- `memory-compact --apply` 会自动使用最近一次主代理 context bundle；直接 API 也可以通过 `main_context_bundle_ref` 显式传入。
- `memory-resume --from-compact` 会把主代理 context bundle 放进 `main_context_bundle`、`recommended_read_paths`、handoff（交接包）、context block（可粘贴恢复上下文）和 continue packet（继续工作包）。
- `memory-compact --apply` 会从 tool-output index（工具输出索引）读取同 scope（同任务范围）的外置工具输出 artifact refs；`memory-resume --from-compact` 会把这些 artifact 路径放进推荐读取路径，恢复时不需要重新扫长日志或把大输出塞回 prompt。
- `memory-resume --from-compact` 会为 tool-output artifacts 生成 `artifact_read_hints`，给出可直接用于 `read_artifact` 的 `artifact_ref/offset/max_chars`；优先使用 scoped call id（带 run 作用域的调用编号），长路径只作为 fallback。
- 同一个任务范围多次执行 `memory-compact --apply` 时，每个 apply 包会带 `lineage`：第几次压缩、上一包 apply id、上一包 metadata/apply bundle 引用、当前包引用。这样长任务经历多次 compact 后，resume 不需要靠自然语言猜“刚刚那次压缩是哪一包”。
- `memory-compact --apply` 自动取最近任务卡时会做 scope match；如果用户 compact 老任务而最新任务卡属于另一个任务，系统会记录 mismatch 并跳过自动绑定，避免串任务。
- `Context Bundle v1` 已补齐 RunScope、ToolManifest、Acceptance Contract、ArtifactRef、自检、schema migration policy、prompt budget 和 owner model；这些字段都走结构化 JSON，给后续 compact/resume/subagent 复用。
- ToolManifest 的 failure taxonomy 已改为复用统一 Error Taxonomy，例如 `PATH_INVALID`、`WRITE_FORBIDDEN`、`TOOL_UNAVAILABLE`、`MODEL_UPSTREAM_FAILED`、`ARTIFACT_MISSING`、`ACCEPTANCE_FAILED`、`COMPACT_REF_MISSING`。
- 新增 `context-bundle latest --json` 只读命令，方便 CLI、前端和调试脚本直接查看最新主代理任务卡，不需要手扫 `memory_archive/snapshots/context_bundles/`。

## Compact / Resume 对齐

手动 compact/resume（压缩/恢复）现在按下面的 refs-first（先看引用，不先读正文）流程走：

1. 主代理普通保存 run 先写 `Main Agent Context Bundle v1`。
2. 用户或系统执行 `memory-compact --apply`。
3. compact apply 会把最近的 context bundle 路径登记到 metadata、restore refs 和 apply bundle。
4. 用户执行 `memory-resume --from-compact <apply_id>`。
5. resume 输出会优先推荐读取这张 context bundle，再读 compact context、work state、restore refs、自检和源事实文件。
6. 如果这是同一任务的第 2 次、第 3 次或更多次 compact，resume 会同时带出 lineage（压缩链路），让调用方能沿着上一包继续审计，不覆盖旧包。

这一步解决的是：恢复时不能只看一段摘要，也不能让模型重新猜任务范围。恢复链路必须先知道“这是谁的任务、在哪个工作区、哪一轮 run、有哪些恢复入口”。

旧 apply 包没有 context bundle 也能继续恢复；新字段是兼容增强，不是硬阻断条件。

旧 apply 包没有 lineage 也能继续恢复；新 lineage 是 append-only（只追加）的审计增强，不会重写历史 apply 文件。

## 后续阶段

1. 把 skill 选择做成 refs-first（先给索引，再按需读正文）。
2. 做主代理真实 E2E：短任务、中任务、长任务、大文件、大代码库、失败恢复。
3. 主代理打硬后，再让子代理复用主代理内核。
