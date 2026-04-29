# Codebase Tree

这份文档做两件事：
- 在目录树上直接给关键文件加一句“这是干嘛的”，方便你扫一眼就知道位置。
- 在后面的详细说明里把职责再展开，方便后续继续扩展工具、记忆和循环智能体能力。

## Tree

```text
simple-python-agent-v0.3/                      # 项目根目录，放代码、说明文档和验证记录
|-- pyproject.toml                             # Python packaging 配置，提供 my-agent console script
|-- CLI_REFERENCE.md                           # 完整 CLI 参数手册，说明每个命令和参数
|-- GATEWAY_DESIGN.md                          # gateway 常驻形态、外部方案对比和本项目目标设计
|-- GATEWAY_RESEARCH.md                        # gateway 大调研，比较 daemon、任务队列、workflow、Notebook 和 AI gateway 方案
|-- agent_py_agent/                            # Python 包目录，核心代码主要都在这里
|   |-- __init__.py                            # 安装包初始化文件，记录包版本
|   |-- __main__.py                            # CLI 入口，负责 run/chat/记忆/subagent 看板与巡检命令
|   |-- README.md                              # 包级说明文档
|   |-- agent/                                 # 智能体核心模块目录
|   |   |-- __init__.py                        # 包初始化文件
|   |   |-- backend.py                         # 模型后端适配层，负责对接 echo / OpenAI 兼容 / Anthropic 兼容接口
|   |   |-- capabilities.py                    # 统一能力路由模块，把 skill card 和 tool card 放到同一检索入口
|   |   |-- capability_config.py              # 能力路由配置结构，管理 skill/tool 授权和子代理上抛参数
|   |   |-- config.py                          # 配置结构和简化 YAML 加载器
|   |   |-- core.py                            # 智能体主调度器，把 prompt、记忆、后端、工具循环和 subagent runner 串起来
|   |   |-- memory.py                          # 本地 JSONL 记忆系统，负责写入和检索历史内容
|   |   |-- prompting.py                       # prompt 拼装器，负责把人格、记忆、工具信息和用户任务合成最终上下文
|   |   |-- skills.py                          # Skill Card 扫描和读取模块，负责把 SKILL.md 变成轻量索引
|   |   |-- subagent.py                        # 子代理运行树模块，记录父子关系、能力请求、授权、缺口、看板和 due-check
|   |   `-- tools.py                           # 工具注册、工具元数据、工具检索和工具执行入口
|   |-- config/                                # 配置目录
|   |   |-- agent_config.yaml                  # 运行配置文件，控制模型、记忆、工具和检索参数
|   |   `-- capability_config.yaml            # 能力路由配置文件，控制 skill/tool 授权、上抛和候选数量
|   |-- data/                                  # 运行时数据目录
|   |   |-- memory.jsonl                       # 长期记忆文件
|   |   `-- subagents/                         # 子任务记录输出目录
|   |-- extensions/                            # 预留扩展目录
|   |-- prompts/                               # prompt 规则文件目录
|   |   `-- default.md                         # 默认动态 prompt 规则
|   `-- tests/                                 # 本地测试目录
|       |-- run_tests.py                       # 一键冒烟测试入口
|       |-- test_agent.py                      # 核心 agent 行为测试
|       |-- test_backends.py                   # 后端适配测试
|       |-- test_capabilities.py               # skill/tool 统一能力路由测试
|       `-- test_tools.py                      # 工具目录、工具调用和工具能力测试
|-- .gitattributes                             # 跨平台文本编码和换行约定
|-- .gitignore                                 # Git 忽略规则
|-- ACCEPTANCE.md                              # 验收记录
|-- AGENTS.md                                  # AI 开发规范，约束后续开发、配置、文档和自学习改动
|-- CODEBASE_TREE.md                           # 当前这份目录树说明
|-- DESIGN_LEDGER.md                           # 设计思路台账，记录新想法、落地状态和后续方向
|-- EVIDENCE.md                                # 过程证据记录
|-- RESULT.md                                  # 结果记录
|-- RUNLOG.md                                  # 运行日志说明
|-- SKILL_SPARK.yaml                           # 项目任务描述
|-- SPEC.md                                    # 原始需求规格
|-- STATUS.md                                  # 当前阶段状态说明
|-- SUBAGENT_RUNBOOK.md                        # subagent、capability 和 runner 详细运行手册
|-- TESTS.md                                   # 测试说明
|-- TEST_CHECKLIST.md                          # 测试检查清单
`-- validation/                                # 验证输出目录
```

## 关键文件说明

### `agent_py_agent/agent/tools.py`

这是本轮最重要的基础设施文件，负责四块能力：
- 注册有哪些工具可用。
- 保存每个工具的元数据。
  例如类别、关键词、适用场景、不适用场景、参数说明和示例。
- 生成两层工具提示。
  一层是常驻的工具目录，一层是按当前任务筛出来的少量候选详情。
- 执行工具调用。
- 支持 `allowed_tools` 白名单，给 subagent runner 限制可见和可调用工具。

这次额外预留了混合检索框架：
- 当前真正生效的是关键词检索。
- 向量检索接口已经留好，后续接 embedding 时不用重写核心流程。

当前内置工具包括：
- `list_files`：列目录，适合先摸清项目结构。
- `read_file`：读文本文件，适合查看代码、配置和文档。
- `search_text`：搜索文本，适合先找函数名、配置项和关键字。
- `write_file`：写入或覆盖整个文本文件，适合创建新文件或完整重写。
- `append_file`：向文件末尾追加内容，适合补日志、补文档和补配置片段。
- `replace_in_file`：精确替换文件中的一段已有内容，适合小范围改代码、改配置和改说明。
- `fetch_url`：抓取网页或文本接口内容，适合查在线文档。
- `http_request`：发送 HTTP 请求，适合测试 REST API、Webhook 和普通接口。

### `agent_py_agent/agent/core.py`

这是智能体主循环，也就是“真正驱动程序跑起来”的地方。

它现在的工具流程是：
1. 先根据用户问题检索相关记忆。
2. 生成工具目录。
3. 再根据当前任务挑出最相关的几个工具详情。
4. 把这些内容一起拼进 prompt。
5. 如果模型发出 `[TOOL_CALL]`，就执行工具并把结果回填给模型继续推理。

它现在的子代理 runner 流程是：
1. 读取或刷新 `execution_context.json`。
2. 生成 runner prompt。
3. 默认 dry-run，只写 prompt 和报告。
4. 显式 `--execute` 时调用模型，并只注入 `allowed_tools`。
5. 执行结果写回工单，状态进入 `AWAITING_ACCEPTANCE`，等待独立验收。

简单说，`core.py` 负责把“会想”变成“会做”。

### `agent_py_agent/agent/prompting.py`

这个文件负责 prompt 的装配顺序。

当前结构已经从“全量工具手册”升级成：
- `# Tools`
  常驻工具目录，让模型始终知道手里有哪些工具。
- `# Recommended Tools`
  只放当前任务更相关的少量工具详情，减少误判。
- `# Tool Transcript`
  放工具调用记录和执行结果，让模型能在下一轮接着推理。

这样做的目的就是：
- 比全量注入更省 prompt
- 比只给工具名更不容易选错工具

### `agent_py_agent/agent/config.py`

这个文件定义项目的配置总表，并提供一个轻量 YAML 读取器。

本轮新增了与工具检索相关的配置项：
- `tool_catalog_limit`
  控制目录层最多展示多少工具。
- `tool_retrieval_limit`
  控制每次最多注入多少个高相关工具详情。
- `tool_vector_search_enabled`
  向量检索预留开关，当前默认关闭。

本轮新增了自学习预留配置项：
- `enable_self_learning`
  控制是否允许后续自学习模块生成学习候选草稿，默认关闭。
  即使开启，也要求先生成草稿并等待用户确认，不能直接改正式 skill。

### `agent_py_agent/agent/capability_config.py`

这个文件定义能力路由相关配置。

它负责的不是模型后端或普通 chat，而是后续多层代理系统里的能力治理：
- 子代理能力请求最大长度。
- 能力请求最多上抛几层。
- 父代理每次最多下发多少候选 skill/tool card。
- 子代理单轮能力包 token 预算。
- 同一能力失败后最多尝试多少个替代工具。

这里的数字限制项统一约定：`0` 表示不限制。

### `agent_py_agent/agent/skills.py`

这个文件负责扫描和解析 skill。

它只把磁盘上的 `SKILL.md` 解析成轻量 Skill Card，不在扫描阶段加载完整正文。

当前支持：
- Markdown frontmatter 里的 `name`、`description`、`when_to_use`。
- `tags`、`capabilities`、`tools_required` 等路由字段。
- `load_body()` 按需读取完整 `SKILL.md`。

### `agent_py_agent/agent/capabilities.py`

这个文件负责统一 skill/tool 能力路由。

它把两类信息合并成同一种 Capability Card：
- `SkillCard`：来自 `skills.py`。
- `ToolSpec`：来自现有 `tools.py`。

当前职责：
- 把 tool 映射成 Tool Card，并补基础风险分类。
- 把 skill 映射成 Skill Capability Card。
- 按自然语言能力缺口检索候选 card。
- 支持 `capability_candidate_limit=0` 表示不限制。

它暂时不负责执行工具，也不负责展开 skill 正文；这些仍由调用方按需处理。

### `agent_py_agent/agent/subagent.py`

这个文件负责子代理运行树。

当前它还不启动真正独立进程，但已经把后续多代理执行需要的记录结构打好了：
- `SubAgentCard`：描述子代理角色、默认工具边界和输出契约。
- `SubAgentTask`：兼容旧名字的轻量 SubAgentRun，记录父子关系、深度、状态和能力边界。
- `CapabilityRequest`：下级代理遇到问题时向上抛的能力请求。
- `CapabilityGrant`：上级代理向下级下发的 skill/tool 授权。
- `CapabilityGap`：最终找不到能力时留下的缺口记录，供后续自学习使用。
- `VerificationEvidence`：记录命令、文件、URL、截图或日志等验收证据，防止假完成。
- `SubAgentBoard`：面向 100+ 多层子代理的红绿灯看板。
- `ChannelProbeCheck` / `ChannelProbeResult` / `ChannelProbeReport`：记录通道健康检查结果，区分任务失败和运行现场故障。
- `ActionPlanItem` / `ActionPlanReport`：把 due-check 问题转成 dry-run 动作计划。
- `ActionApplyRecord` / `ActionApplyReport`：执行或 dry-run 执行动作计划，并留下审计记录。
- `CapabilityRouteRecord` / `CapabilityRouteReport`：把 open capability request 路由到 skill/tool card。
- `AcceptanceReviewFinding` / `AcceptanceReviewRecord` / `AcceptanceReviewReport`：记录父代理验收检查、决策和写回结果。
- `PatchReviewRecord` / `PatchReviewReport`：记录 runner patch 输出的审核、决策和写回结果。
- `DispatchRecord` / `DispatchReport`：记录父代理一轮调度中的 due-check、路由、runner、patch 审核和验收步骤。
- `DispatchWatchRecord` / `DispatchWatchReport`：记录父代理 watch 模式的循环、心跳和退出状态。
- `ParentPlannerParsedOutput` / `ParentPlannerRecord` / `ParentPlannerReport`：记录父代理 LLM planner 的结构化输出、gate 结果和审计证据。
- `SubAgentExecutionContext`：把授权后的 skill/tool、能力卡、写入边界和验收要求打成子代理执行上下文。
- `SubAgentRunnerResult`：记录一次子代理 runner 调用的模式、结果、状态和证据文件。

保存时会同时写：
- `task.json`：兼容旧记录。
- `run.json`：面向后续运行树语义。
- `thought.md`：给人看的计划和能力边界摘要。
- `execution_context.json` / `EXECUTION_CONTEXT.md`：按需生成的子代理执行上下文包，不包含全局能力宇宙。
- `RUNNER_RESULT.md` / `reports/runner_result.json`：runner dry-run 或 execute 后的审计结果。
- `PATCH_REVIEW.md` / `reports/patch_review.json`：patch 审核后的单任务证据。
- `logs/runner_prompt.md` / `logs/runner_response.md`：runner 实际使用的 prompt 和模型回复。

当前已经加入第一层 Fake Done 防护：
- 子代理记录有 owner / supervisor / final_owner。
- 子代理记录有 acceptance checks 和 evidence。
- `set_status(..., require_evidence=True)` 会禁止没有证据的任务标记为 DONE。

当前已经加入标准工单目录：
- 创建子代理运行时自动生成 `data/`、`output/`、`tests/`、`reports/`、`logs/`、`scratch/`。
- 自动初始化 `STATUS.md`、`WORK_LOG.md`、`ACCEPTANCE.md`、`DEBRIEF.md`。
- 支持写入 `TAKEOVER.md`，记录接管者、原因和 locked files。
- 自动初始化 `output.json` 和 `dependencies.json`，给机器读取任务状态和依赖。
- 记录 `allowed_write_roots` 和 `forbidden_write_roots`，先把写入边界落到运行记录里。
- `validate_work_order()` 可以检查关键目录和文件是否存在。
- `record_takeover()` 可以把任务置为 `TAKEN_OVER`，并把 final owner 切给接管者。
- `write_board()` 会生成 `subagent_board.json` 机器事实源和 `SUBAGENT_BOARD.md` 人类摘要。
- 看板默认突出 hot list，避免 100+ 子代理时淹没异常任务。
- `due_check()` 会生成父代理巡检报告，把假完成、超时、心跳停滞、待处理能力请求和能力缺口转成 P0/P1/P2 问题。
- `write_due_check()` 会写出 `subagent_due_check.json` 机器事实源和 `SUBAGENT_DUE_CHECK.md` 人类摘要。
- `python3 -m agent_py_agent subagents-due-check` 可以从 CLI 触发巡检。
- `probe_channel()` 可以检查单个子代理的工单现场、机器 JSON 和写入通道是否健康。
- `write_channel_probe_report()` 会写出 `subagent_channel_probe.json` 和 `SUBAGENT_CHANNEL_PROBE.md`。
- `python3 -m agent_py_agent subagents-probe` 可以从 CLI 触发通道健康检查。
- `plan_actions()` 会把 due-check issue 映射成接管、重派、修复工单、能力路由等 dry-run 动作。
- `write_action_plan()` 会写出 `subagent_action_plan.json` 和 `SUBAGENT_ACTION_PLAN.md`。
- `python3 -m agent_py_agent subagents-plan-actions` 可以从 CLI 查看 dry-run 动作计划。
- `apply_actions()` 默认 dry-run，只有显式 apply 时才会执行低风险动作。
- `write_action_apply_report()` 会写出 `subagent_action_apply_report.json` 和 `SUBAGENT_ACTION_APPLY.md`。
- 真正 apply 时会追加 `subagent_action_apply_log.jsonl` 和 `ACTION_APPLY_LOG.md` 审计日志。
- `python3 -m agent_py_agent subagents-apply-actions --apply ...` 可以执行受限动作。
- `route_capability_requests()` 会检索 Capability Router，命中时生成 grant，未命中时生成 gap。
- `write_capability_route_report()` 会写出 `subagent_capability_route_report.json` 和 `SUBAGENT_CAPABILITY_ROUTE.md`。
- 真正 apply 时会追加 `subagent_capability_route_log.jsonl` 和 `CAPABILITY_ROUTE_LOG.md` 审计日志。
- `python3 -m agent_py_agent subagents-route-capabilities` 可以 dry-run 或 apply 路由 open capability request。
- `review_acceptances()` 会检查等待验收的 run 是否具备证据、无 blocker、无未处理 capability request/gap、测试和 patch 状态可接受。
- `write_acceptance_review_report()` 会写出 `subagent_acceptance_report.json` 和 `SUBAGENT_ACCEPTANCE.md`。
- 真正 apply 时会追加 `subagent_acceptance_log.jsonl` 和 `ACCEPTANCE_REVIEW_LOG.md` 审计日志。
- `python3 -m agent_py_agent subagents-acceptance` 默认 dry-run；显式 `--apply` 才会把通过验收的 run 标记为 `DONE/VERIFIED`。
- `review_patches()` 会检查 runner 输出的 patch 记录，只允许已应用且状态合法的 patch 进入审核通过。
- `write_patch_review_report()` 会写出 `subagent_patch_review_report.json`、`SUBAGENT_PATCH_REVIEW.md` 和单任务 `PATCH_REVIEW.md`。
- `python3 -m agent_py_agent subagents-patches` 默认 dry-run；显式 `--apply` 才会写回 patch `review_status` 和审计日志。
- `SimpleAgent.dispatch_subagents()` 会执行一轮父代理调度：due-check、action apply、capability route、runner、patch review、acceptance。
- `SimpleAgent.watch_subagents()` 会用运行锁持续执行 dispatch，并写 heartbeat / watch log。
- `SimpleAgent.run_parent_planner()` 会在 gate 发现待处理事项时触发完整父代理 LLM turn，并阻断空心 `HEARTBEAT_OK`。
- `write_dispatch_report()` 会写出 `subagent_dispatch_report.json` 和 `SUBAGENT_DISPATCH.md`，apply 时追加调度审计日志。
- `python3 -m agent_py_agent subagents-dispatch` 默认 dry-run；`--watch` 可常驻循环；`--planner` 可触发父代理 LLM planner；`--apply --execute-runners` 才会真实调用 runner 模型。
- `python3 -m agent_py_agent daemon` 会读取主配置里的 `daemon_*` 配置，作为配置驱动的前台常驻入口；`daemon_max_runners: "auto"` 当前映射成保守值 1。
- `build_execution_context()` 会把当前 run 的授权、能力卡、验收要求和写入边界压成最小上下文。
- `write_execution_context()` 会写出 `execution_context.json` 和 `EXECUTION_CONTEXT.md`。
- `python3 -m agent_py_agent subagent-context <run_id>` 可以生成单个子代理执行上下文包。
- `record_runner_result()` 会把 runner 输出写回 `output.json`、`RUNNER_RESULT.md` 和任务日志。
- `parse_subagent_runner_output()` 会解析 `[SUBAGENT_RESULT]...[/SUBAGENT_RESULT]` JSON 块。
- 结构化 runner 输出里的 evidence 会自动写入验收证据。
- 结构化 runner 输出里的 capability request 会自动写成 open `CapabilityRequest`。
- 结构化 runner 输出里的 artifacts / tests / patches / lessons / next_actions 会写入 `output.json` 和 `DEBRIEF.md`。
- `python3 -m agent_py_agent subagent-run <run_id>` 默认 dry-run；显式 `--execute` 才会调用模型。
- `SimpleAgent.run(..., allowed_tools=[...])` 会限制 prompt 里的工具目录和实际工具调用。

### `agent_py_agent/config/agent_config.yaml`

这是给人改的配置文件，不是给代码看的结构定义。

你后续调工具策略时，优先会改这里：
- 用户层任务规模：`task_max_subagents`、`task_max_grandchildren`，0 表示不设硬上限。
- 未来 gateway 自适应策略：`scheduler_mode`、`runner_concurrency`、`runner_start_rate`、`runner_timeout_seconds` 和 `runner_failure_policy`，默认都是 `auto`。
- 当前前台 daemon 高级参数：`daemon_*`，用于在 gateway 完整实现前控制 watch 调度。
- 工具返回长度限制
- 工具详情注入数量
- 是否打开向量检索开关
- 是否打开自学习候选草稿生成

### `agent_py_agent/config/capability_config.yaml`

这是给人调能力路由策略的配置文件。

它和 `agent_config.yaml` 分开维护，避免主配置文件混入过多子代理和授权细节。

默认只给几项关键限制设置正常值：
- 子代理能力请求最大 token。
- 能力请求最多上抛层数。
- 候选 skill/tool card 数量。
- 子代理能力包 token 预算。
- 替代工具最大尝试次数。

其他数字限制项默认设为 `0`，表示不限制，由用户按需要自己收紧。

### `agent_py_agent/tests/test_tools.py`

这个测试文件重点验证：
- 工具目录和候选工具详情是否真的出现在 prompt 里。
- 工具调用循环能不能正常执行。
- 工具 allowlist 是否能限制 prompt 目录和实际工具调用。
- 写文件、追加文件、局部替换、抓网页、测接口这些基础能力有没有回归。

### `agent_py_agent/tests/test_capabilities.py`

这个测试文件重点验证：
- `SKILL.md` 能否解析成 Skill Card。
- Skill Registry 能否扫描 skill 目录。
- 现有 ToolSpec 能否映射成 Capability Card。
- Capability Router 能否同时检索 skill 和 tool。
- `0` 是否按“不限制”处理。

### `agent_py_agent/tests/test_agent.py`

这个测试文件覆盖核心 agent 行为和子代理框架：
- 记忆写入和基础运行。
- 子代理拆分数量限制。
- 子代理运行记录是否落盘。
- 父子关系、能力请求、能力授权和能力缺口是否能保存并读回。
- 没有验收证据时是否禁止标记 DONE。
- 标准工单目录和关键文件是否自动创建。
- 删除关键文件后，`validate_work_order()` 是否能报告缺失。
- 接管时是否写入 `TAKEOVER.md` 和 locked files。
- 子代理看板是否能生成 JSON / Markdown，并把异常任务打上 risk flags。
- due-check 是否能发现工单缺失、假完成、未验收 DONE、心跳停滞、运行超时、待处理能力请求和能力缺口。
- channel probe 是否能识别健康通道和坏通道，并写入单任务证据与全局报告。
- action plan 是否能把 due-check 问题去重合并为 dry-run 动作，并写入机器 JSON 和 Markdown 报告。
- action apply 是否默认 dry-run，显式 apply 时是否能重开缺证据任务、接管超时任务、修复工单现场，并写入审计日志。
- capability route 是否默认 dry-run，显式 apply 时是否能给 request 生成 grant 或 gap，并写入审计日志。
- execution context 是否只包含已授权 skill/tool、granted card、任务边界和验收规则。
- subagent runner 是否默认 dry-run，显式执行时是否只注入授权工具并把结果写回工单。
- subagent runner 是否能解析结构化输出，并自动生成 evidence 和 capability request。
- subagent runner 是否能把 artifacts / tests / patches / lessons / next_actions 落进机器结果和 debrief。
- subagent patch 审核是否能批准 applied patch，并阻断 planned / blocked / 未知状态 patch。
- subagent dispatch 是否能把 runner、patch review 和 acceptance 串成一轮父代理调度。
- subagent dispatch watch 是否能安全循环、写 heartbeat，并用 lock 阻止双父代理。
- parent planner 是否能在 gate 非空时调用 LLM，并阻止空心 HEARTBEAT_OK。

### `.gitattributes`

这个文件负责把文本文件的跨平台规则写清楚。

当前约定是：
- 源码、Markdown、YAML、JSON 等文本文件都按 UTF-8 管理。
- 仓库内自动规范文本文件换行。
- Windows 脚本保留 CRLF，shell 脚本保留 LF。

这样 Windows / macOS / Linux 来回开发时，中文注释和文档不容易因为编码或换行差异变乱。

### `AGENTS.md`

这是后续 AI 开发者进入项目时必须遵守的开发规范。

它明确了：
- 新增配置项要同步更新 YAML 和 dataclass。
- 用户可见文案、配置注释和项目文档优先使用中文。
- 新增重要文件要更新 `CODEBASE_TREE.md`。
- 自学习功能默认关闭，开启后也只能先生成学习候选草稿，不能自动改正式 skill。
- skill 体系后续优先采用“索引 → 正文 → 附件”的渐进加载方向。

### `SUBAGENT_RUNBOOK.md`

这是当前 subagent / capability / runner 的详细手册。

它说明：
- 为什么要做工单化子代理。
- capability request / grant / gap 的职责。
- 标准工单目录每个文件的用途。
- due-check / probe / action plan / action apply / capability route / runner 的命令流。
- `[SUBAGENT_RESULT]` 结构化输出协议。
- evidence、capability request、artifacts、tests、patches、lessons、next_actions 的写回规则。
- runner 的安全边界和后续缺口。

后续改 subagent 主链路时，除了 `DESIGN_LEDGER.md`，也要同步检查这份 runbook 是否需要更新。

### `DESIGN_LEDGER.md`

这是项目的设计思路台账。

它专门记录交流中形成的新想法、是否已经落地、落地位置和后续方向。

后续 AI 如果听到用户提出新的架构想法，例如 skill 路由、tool 授权、能力上抛、自学习策略、命令语义变化，都要更新这份文件。

## 跨平台兼容性

当前代码已经按“尽量兼容 Windows / Linux / macOS”收口，主要依据是：
- 路径统一使用 `pathlib.Path`
- 网络请求统一用标准库 `urllib`
- 工具系统没有依赖 PowerShell、cmd 或 macOS 专用命令
- 配置与数据文件统一按 UTF-8 读写
- `.gitattributes` 明确了 UTF-8 文本和跨平台换行规则

这意味着：
- 在三大平台上都能直接跑纯 Python 主链路
- 后续如果要加命令执行类工具，需要继续保持这一层跨平台约束
