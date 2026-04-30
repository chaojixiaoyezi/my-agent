# Codebase Tree

这份文档做两件事：
- 在目录树上直接给关键文件加一句“这是干嘛的”，方便你扫一眼就知道位置。
- 在后面的详细说明里把职责再展开，方便后续继续扩展工具、记忆和循环智能体能力。

## Tree

> 2026-04-29 更新：大文件已经按职责拆分。旧入口文件仍保留兼容导入，新实现优先看 `cli/`、`agent_core/`、`tooling/`、`gateway_parts/`、`local_storage/`、`subagents/`。
> 第二轮更新：`agent/` 根目录散落实现已继续归位。目录含义详见 `agent_py_agent/agent/DIRECTORY_GUIDE.md`。

```text
simple-python-agent-v0.3/                      # 项目根目录，放代码、说明文档和验证记录
|-- pyproject.toml                             # Python packaging 配置，提供 my-agent console script
|-- CLI_REFERENCE.md                           # 完整 CLI 参数手册，说明每个命令和参数
|-- ARCHITECTURE_GUIDE.md                      # 架构边界、拆分顺序和两层注释规则，给人和 LLM 都看
|-- GATEWAY_DESIGN.md                          # gateway 常驻形态、外部方案对比和本项目目标设计
|-- GATEWAY_RESEARCH.md                        # gateway 大调研，比较 daemon、任务队列、workflow、Notebook 和 AI gateway 方案
|-- MEMORY_BACKLOG.md                          # 记忆系统痛点、去重分类和后续设计讨论入口
|-- WORKSTREAMS.md                             # 并行开发工作台说明，定义 worktree、职责边界和集成流程
|-- HANDOFF_TEMPLATE.md                        # 并行开发线完成后的交接模板
|-- docs/                                      # 长篇项目文档目录
|   `-- design/                                # 模块设计文档，承载 DESIGN_LEDGER 的长篇细节
|       |-- README.md                          # 模块设计文档索引和拆分规则
|       |-- log-analysis.md                    # 日志分析模块验收状态、剩余缺口和 worker 切片
|       `-- subagent-quality-contract.md       # subagent 质量契约、受控施工队和用户少说派工设计
|-- scripts/                                   # 开发辅助脚本，放可见测试台和 workstream 管理入口
|   |-- live_agent_lab.py                      # Live Lab 薄入口，启动可见真实环境测试台
|   |-- live_lab/                              # Live Lab 参数解析、运行器、case 和常量
|   |-- open_live_lab.sh                       # macOS 新开可见 Terminal 跑 Live Lab
|   |-- workstream_common.sh                   # workstream 脚本共享路径、命名和校验函数
|   |-- workstream_create.sh                   # 创建 git worktree 并行开发线
|   |-- workstream_status.sh                   # 查看主仓库和所有 worktree 的分支与脏文件状态
|   `-- open_workstream.sh                     # 打开某条 workstream 的可见 Terminal
|-- agent_py_agent/                            # Python 包目录，核心代码主要都在这里
|   |-- __init__.py                            # 安装包初始化文件，记录包版本
|   |-- __main__.py                            # CLI 兼容入口，真实命令实现已拆到 cli/
|   |-- README.md                              # 包级说明文档
|   |-- cli/                                   # CLI 命令层，按 common/local/subagents/gateway/scenario/chat/parser 拆分
|   |   |-- common.py                          # 配置加载、创建 SimpleAgent、能力路由和通用格式化
|   |   |-- local_doctor.py                    # LocalStore/gateway/subagent 体检和重建规则
|   |   |-- local_commands.py                  # status/timeline/run/memory/local-search/local-doctor/local-rebuild 命令
|   |   |-- memory_commands.py                 # memory-route/memory-doctor 可见诊断命令
|   |   |-- subagents.py                       # 子代理看板、动作、路由、验收、dispatch、runner 命令
|   |   |-- daemon.py                          # daemon 配置合并和前台常驻调度
|   |   |-- gateway_process.py                 # gateway 进程生命周期、run loop、heartbeat 和 request worker
|   |   |-- gateway_client.py                  # gateway ask/result/default 客户端命令
|   |   |-- adapter.py                         # 文件 adapter：外部 inbox/outbox JSON 与 gateway ask 转换
|   |   |-- scenario.py                        # scenario-test 命令入口和 happy path
|   |   |-- scenario_cases.py                  # verification/gateway-restart/structured-repair/runner-retry 专项场景
|   |   |-- scenario_utils.py                  # scenario fixture、隔离配置、子进程和摘要工具
|   |   |-- chat.py                            # 交互 chat 队列和斜杠命令
|   |   |-- logs.py                            # 日志分析 logs status/ingest/query/hunt/trace CLI 命令
|   |   `-- parser.py                          # argparse 命令树和 main()
|   |-- agent/                                 # 智能体核心模块目录
|   |   |-- __init__.py                        # 包初始化文件
|   |   |-- DIRECTORY_GUIDE.md                 # agent 目录职责地图，后续新增文件先看这里
|   |   |-- backend.py                         # 模型后端兼容入口，真实实现已拆到 backends/
|   |   |-- backends/                          # 模型后端适配层，负责 echo / OpenAI 兼容 / Anthropic 兼容接口
|   |   |-- capabilities.py                    # 能力路由兼容入口，真实实现已拆到 capability/
|   |   |-- capability_config.py              # 能力配置兼容入口，真实实现已拆到 capability/
|   |   |-- capability/                        # 能力路由、能力配置、Skill Card、Tool Card 统一治理
|   |   |-- clients/                           # 未来非模型外部服务客户端目录，目前用 README 定义边界
|   |   |-- config.py                          # 配置兼容入口，真实实现已拆到 settings/
|   |   |-- settings/                          # 配置结构、简化 YAML 加载器和环境变量覆盖
|   |   |-- core.py                            # SimpleAgent 兼容组合入口，真实实现已拆到 agent_core/
|   |   |-- agent_core/                        # 主循环、子代理 runner、planner、dispatch、编排工具、runner 规则
|   |   |-- file_io.py                         # 文件 I/O 兼容入口，真实实现已拆到 io/
|   |   |-- io/                                # 无业务含义的底层文件 I/O 原语，例如 locked JSONL append
|   |   |-- gateway.py                         # gateway 兼容入口，真实协议实现已拆到 gateway_parts/
|   |   |-- gateway_parts/                     # gateway 路径、IO、进程控制、恢复、运行时、adapter、索引日志
|   |   |-- local_store.py                     # LocalStore 兼容组合入口，真实实现已拆到 local_storage/
|   |   |-- local_storage/                     # LocalStore models/schema/records/search/events/maintenance
|   |   |-- log_analysis/                      # 可选日志分析底座，负责安全日志接入、解析、查询、检测、case、报告和 analyst 派工
|   |   |-- memory.py                          # 记忆兼容入口，真实实现已拆到 memory_store/
|   |   |-- memory_settings.py                 # memory 配置安全解析兼容入口，真实实现已拆到 settings/memory.py
|   |   |-- memory_store/                      # 长期记忆存储，当前是 JSONL + LocalStore 索引
|   |   |-- memory_archive/                    # 压缩前 hook 快照和 raw 冷归档 JSONL 存储骨架
|   |   |-- memory_routing/                    # 长期规则索引化路由，负责 MEMORY -> index -> authority file 的确定性匹配
|   |   |-- observability/                     # 未来 request_id、耗时、状态、错误码、metrics、trace 目录
|   |   |-- prompting.py                       # prompt 兼容入口，真实实现已拆到 prompting_parts/
|   |   |-- prompting_parts/                   # prompt 构造、工具 transcript、未来上下文预算策略
|   |   |-- repositories/                      # 未来领域仓储接口目录，目前用 README 定义边界
|   |   |-- security/                          # 未来权限、输入净化、可信度、安全默认策略目录
|   |   |-- skills.py                          # Skill Card 兼容入口，真实实现已拆到 capability/skills.py
|   |   |-- subagent.py                        # 子代理兼容入口，真实实现已拆到 subagents/
|   |   |-- subagent_workflows/                # 子代理 workflow 模板模型、加载器和内置模板资源
|   |   |-- subagents/                         # 子代理模型、报告、manager mixin、验收、dispatch、runner、索引等
|   |   |-- tools.py                           # 工具兼容入口，真实实现已拆到 tooling/
|   |   |-- tooling/                           # 工具模型、文件工具、HTTP 工具、解析器、注册表、写边界
|   |   `-- validators/                        # 未来跨领域校验规则目录，目前用 README 定义边界
|   |-- config/                                # 配置目录
|   |   |-- agent_config.yaml                  # 运行配置文件，控制模型、记忆、工具和检索参数
|   |   |-- capability_config.yaml            # 能力路由配置文件，控制 skill/tool 授权、上抛和候选数量
|   |   `-- log_analysis_config.yaml          # 日志分析模块独立配置，默认关闭高影响能力
|   |-- data/                                  # 运行时数据目录
|   |   |-- memory.jsonl                       # 长期记忆文件
|   |   |-- gateway/                           # gateway pid/state/heartbeat/log/stop request、inbox 和 response 输出目录
|   |   |-- local_store/                       # SQLite 本地事实源、正文文件和追加式事件流水
|   |   `-- subagents/                         # 子任务记录输出目录
|   |-- extensions/                            # 预留扩展目录
|   |-- prompts/                               # prompt 规则文件目录
|   |   `-- default.md                         # 默认动态 prompt 规则
|   `-- tests/                                 # 本地测试目录
|       |-- run_tests.py                       # 一键冒烟测试入口
|       |-- test_agent.py                      # 核心 agent 行为测试
|       |-- test_backends.py                   # 后端适配测试
|       |-- test_capabilities.py               # skill/tool 统一能力路由测试
|       |-- test_cli_reference.py              # CLI_REFERENCE 与 argparse 命令/参数覆盖测试
|       |-- test_local_store.py                # SQLite/FTS5/JSONL/记忆索引回归测试
|       |-- test_log_analysis_cli.py           # 日志分析 logs CLI 状态、接入和查询测试
|       |-- test_log_analysis_detectors.py     # 日志分析软检测器、case、route 和报告测试
|       |-- test_log_analysis_dispatch.py      # 日志分析 analyst/reviewer 合同和 dispatch 测试
|       |-- test_log_analysis_ingest.py        # SecurityAlertV1 CSV/JSONL 接入、checkpoint、dedup 和 dead letter 测试
|       |-- test_log_analysis_models.py        # 日志分析模型和配置测试
|       |-- test_log_analysis_query.py         # 本地日志查询、evidence 和 hunting tool 测试
|       |-- test_memory_archive.py             # 压缩前快照、raw 冷归档、留存策略和 token 估算测试
|       |-- test_memory_archive_runtime.py     # run turn 冷归档 helper、稳定 event_id/hash 和工具元数据测试
|       |-- test_memory_cli.py                 # memory-route / memory-doctor CLI 可见诊断测试
|       |-- test_memory_config.py              # memory 配置安全默认、非法值回退和 warning receipt 测试
|       |-- test_memory_routing.py             # 长期规则 route 加载、匹配、soft/strict 解析和索引诊断测试
|       |-- test_memory_routing_context.py     # runtime rule routing context 安全读取和 prompt 片段测试
|       |-- test_memory_runtime.py             # SimpleAgent.run 接入 routed memory 与 raw archive 的回归测试
|       |-- test_packaging.py                  # console script、workspace_root 等安装与配置行为测试
|       |-- test_subagent_quality_contract.py  # 子代理 QualityContract / ContextManifest 落盘和执行上下文测试
|       |-- test_subagent_workflow_config.py   # subagent workflow auto/manual/off 配置规范化测试
|       |-- test_subagent_workflow_templates.py # workflow 模板加载、覆盖和校验测试
|       `-- test_tools.py                      # 工具目录、工具调用和工具能力测试
|-- .gitattributes                             # 跨平台文本编码和换行约定
|-- .gitignore                                 # Git 忽略规则
|-- ACCEPTANCE.md                              # 验收记录
|-- AGENTS.md                                  # AI 开发规范，约束后续开发、配置、文档和自学习改动
|-- CODEBASE_TREE.md                           # 当前这份目录树说明
|-- DESIGN_LEDGER.md                           # 设计思路台账，记录新想法、落地状态和后续方向
|-- DISCUSSION_BACKLOG.md                      # 功能开发之外的系统问题讨论清单
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
    `-- security_fixtures/                     # 日志分析 SecurityAlertV1 最小回放样本
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
- 解析模型输出里的工具调用块。
  标准格式是 `[TOOL_CALL]...JSON...[/TOOL_CALL]`，同时兼容 Qwen/OpenClaw 常见的 XML-ish `<tool_call><function=...><parameter=...>` 方言。
  如果 XML-ish 工具调用只有半截，解析器会返回 `__parse_error__`，让主循环继续可恢复，而不是直接崩掉。

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

### `WORKSTREAMS.md`

这是并行开发工作台的规则入口。

它定义：
- 哪些开发线可以并行。
- 每条线默认对应哪个 `git worktree` 和分支。
- 每条线负责什么、不应该碰什么。
- 主线如何读取 handoff、检查 diff、跑测试并统一集成。

当前预置线包括：
- `memory`
- `framework-runtime`
- `tools-boundary`
- `live-lab-test`

### `MEMORY_BACKLOG.md`

这是记忆系统的专门讨论入口。

它记录：
- 记忆没有真实落盘的问题。
- 临时上下文、长期偏好、任务状态、lesson 和 skill 种子混杂的问题。
- HOT 层、Daily、Task、Lesson、Archive、Skill Draft 等建议分层。
- RAG 召回不稳定、压缩前 flush、任务状态同步、历史污染、子代理成果收束等问题。
- 自学习暂不开发，只预留 Skill Draft / Learning Candidate 的边界。

### `scripts/live_agent_lab.py` 和 `scripts/live_lab/`

这是可见真实环境测试台。

它负责：
- 创建隔离 fixture 项目。
- 写入隔离配置，避免污染主仓库数据。
- 打印并保存发给 `my-agent` 的 prompt。
- 打印实际命令、stdout、stderr、退出码和耗时。
- 调用现有 `scenario-test` 跑健康、坏天气、真实 gateway ask 和真实 subagent 长链路。

`scripts/open_live_lab.sh` 会在 macOS 新开 Terminal，让用户能直接看到测试过程。

### `scripts/workstream_*.sh` 和 `scripts/open_workstream.sh`

这是并行开发线管理脚本。

它们负责：
- `workstream_create.sh`：创建 `git worktree` 和 `workstream/<name>` 分支。
- `workstream_status.sh`：列出主仓库和所有 worktree 的分支、HEAD 和脏文件数量。
- `open_workstream.sh`：打开某条开发线的可见终端。
- `workstream_common.sh`：统一路径计算、名称校验和分支命名。

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

### `agent_py_agent/agent/gateway.py`

这是 gateway 文件协议层，从 `__main__.py` 里拆出来。

它现在负责：
- `GatewayPaths` / `AdapterPaths`：集中描述 gateway 和 adapter 的所有文件路径。
- gateway 请求队列：pending、processing、done、failed、responses 的文件流转。
- gateway request 执行：读取请求、调用 `agent.run()`、写响应、写 history。
- adapter 文件协议：inbox JSON 转 gateway ask，结果写到 outbox JSON。
- processing 恢复：gateway 重启或请求超时后，重排或失败归档。
- gateway LocalStore 重建索引：从 history、队列和 response 文件恢复可搜索记录。

拆出来以后，`__main__.py` 不再需要知道每个 gateway 文件怎么命名、怎么归档。
CLI 只负责启动命令和打印结果；协议细节交给 `gateway.py`。

### `agent_py_agent/agent/file_io.py`

这是本地文件 I/O 小工具层。

当前只有一个核心职责：带锁追加 JSONL。

为什么需要它：
- memory、LocalStore、gateway、subagent 都会写 JSONL 审计流水。
- gateway worker / runner / watch 以后可能并发写同一个文件。
- 如果没有锁，两条 JSON 可能互相插到一起，账本就坏了。

`append_jsonl()` 会先拿同进程线程锁和文件旁边的 `.lock`，再写完整一行。

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

### `agent_py_agent/agent/local_store.py`

这是第一版本地事实源。

它把本地数据分成四层：
- SQLite 普通表：保存记录卡片、来源、路径、元数据和审计事件。
- SQLite FTS5：保存标题和正文全文索引；如果运行环境不支持 FTS5，会退回 LIKE 检索。
- 文件系统：保存正文和未来大 artifact，避免数据库越来越臃肿。
- JSONL：保存追加式审计流水，方便人直接排查，也方便以后做上传同步。

当前已接入记忆系统：新记忆会继续写 `memory.jsonl`，同时索引到 LocalStore。
旧记忆可以通过 `my-agent local-index-memory` 补建索引。

当前也已接入主要运行日志：gateway request / gateway lifecycle、subagent run、
work log、runner result、execution context、acceptance、patch review、dispatch、
dispatch watch、parent planner、capability route、action apply 和 channel probe。
这些原本的文件报告仍然会照常写出，LocalStore 只额外提供可搜索账本和统一审计事件。
`my-agent timeline` 会读取 SQLite 事件表并 join 记录标题，`my-agent status`
会汇总 gateway、LocalStore、subagent 看板和最近事件。

### `agent_py_agent/agent/config.py`

这个文件定义项目的配置总表，并提供一个轻量 YAML 读取器。

本轮新增了本地事实源配置项：
- `local_store_path`
  SQLite 数据库路径。
- `local_store_files_dir`
  正文和未来 artifact 的文件目录。
- `local_store_events_path`
  追加式审计 JSONL 路径。
- `local_store_fts_enabled`
  是否尝试启用 SQLite FTS5；关闭后仍可用 LIKE 检索。

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
- 自动初始化 `STATUS.md`、`WORK_LOG.md`、`ACTION_RECEIPTS.md`、`ACCEPTANCE.md`、`TEST_CHECKLIST.md`、`BUGS.md`、`SKILL_USAGE.md`、`HANDOFF.md`、`DEBRIEF.md`。
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
- `python3 -m agent_py_agent gateway start/status/stop/restart/logs` 会管理第一版后台 gateway 进程。
- `python3 -m agent_py_agent gateway ask/result` 会通过 `data/gateway/requests` 和 `data/gateway/responses` 与常驻 gateway 交换消息。
- `build_execution_context()` 会把当前 run 的授权、能力卡、验收要求、质量契约、context manifest、context packs 和写入边界压成最小上下文。
- `write_execution_context()` 会写出 `execution_context.json` 和 `EXECUTION_CONTEXT.md`。
- `python3 -m agent_py_agent subagent-context <run_id>` 可以生成单个子代理执行上下文包。
- `record_runner_result()` 会把 runner 输出写回 `output.json`、`RUNNER_RESULT.md` 和任务日志。
- `record_runner_result(..., actual_tools=[...])` 会把系统真实记录的工具执行落成验收证据，避免模型 evidence 换写法时误判缺少 `read_file/write_file`。
- `parse_subagent_runner_output()` 会解析 `[SUBAGENT_RESULT]...[/SUBAGENT_RESULT]` JSON 块。
- 结构化 runner 输出里的 evidence 会自动写入验收证据。
- 结构化 runner 输出里的 capability request 会自动写成 open `CapabilityRequest`。
- 结构化 runner 输出里的 artifacts / tests / patches / lessons / next_actions 会写入 `output.json` 和 `DEBRIEF.md`。
- `python3 -m agent_py_agent subagent-run <run_id>` 默认 dry-run；显式 `--execute` 才会调用模型。
- `SimpleAgent.run(..., allowed_tools=[...])` 会限制 prompt 里的工具目录和实际工具调用。

### `agent_py_agent/agent/subagent_workflows/`

这是第一版 subagent workflow 模板库骨架：
- `models.py` 定义 `WorkflowTemplate`、`WorkflowPhase` 和 `WorkflowLoadIssue`。
- `store.py` 负责加载内置 JSON 模板、加载用户 JSON 模板、校验必填字段并让用户模板覆盖同 id 内置模板。
- `builtin/*.json` 当前包含 `single_worker_verified`、`code_feature_split` 和 `producer_critic_repair`。
- 每个模板必须写 `solves`，说明它解决哪些用户痛点或系统风险。

### `agent_py_agent/config/agent_config.yaml`

这是给人改的配置文件，不是给代码看的结构定义。

你后续调工具策略时，优先会改这里：
- 用户层任务规模：`task_max_subagents`、`task_max_grandchildren`，0 表示不设硬上限。
- Subagent workflow：`subagent_workflow_mode` 支持 `auto/manual/off`，`subagent_builtin_workflows` 控制是否加载内置模板，`subagent_user_workflow_dirs` 指向用户可覆盖模板目录。
- 未来 gateway 自适应策略：`scheduler_mode`、`runner_concurrency`、`runner_start_rate`、`runner_timeout_seconds` 和 `runner_failure_policy`，默认都是 `auto`。
- 第一版 gateway 控制面：`gateway_workspace`、`gateway_heartbeat_interval`、`gateway_stale_seconds`、`gateway_stop_timeout`、`gateway_request_timeout` 和 `gateway_request_poll_interval`。
- 当前前台 daemon 高级参数：`daemon_*`，用于在 gateway 完整实现前控制 watch 调度。
- 工具返回长度限制
- 工具详情注入数量
- 是否打开向量检索开关
- 是否打开自学习候选草稿生成

### `agent_py_agent/tests/test_subagent_workflow_config.py`

这个测试文件重点验证：
- workflow 开关默认是 `auto`。
- `manual/off` 可以被配置。
- 非法 mode、非法用户模板目录和非法 review rounds 会回退默认值并生成 warning receipt。

### `agent_py_agent/tests/test_subagent_workflow_templates.py`

这个测试文件重点验证：
- 内置模板能加载。
- 用户模板同 id 能覆盖内置模板。
- 坏模板会生成 validation issue。
- 每个内置模板都有 `solves` 字段。

### `agent_py_agent/tests/test_subagent_quality_contract.py`

这个测试文件重点验证：
- `create_run()` 能把 `QualityContract`、`ContextManifest` 和 `context_packs` 落进 `task.json`。
- 旧 `task.json` 缺少这些字段时仍能读取。
- `execution_context.json` 和 `EXECUTION_CONTEXT.md` 会包含质量契约、context manifest 和“子代理不能自判完成”的规则。

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
- subagent runner 的 `actual_tools` 是否会转成系统验收证据，避免真实工具调用被模型自然语言 evidence 写法影响。
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

后续改 subagent 主链路时，除了 `DESIGN_LEDGER.md` 和 `docs/design/subagent-quality-contract.md`，也要同步检查这份 runbook 是否需要更新。

### `LOG_ANALYSIS_BACKLOG.md`

这是长期后台日志分析助手的规划入口。

它记录第一版 SecurityAlertV1、安全日志接入、软检测器、case、analyst subagent、3 分钟第一响应、0day 弱信号和未来 ML/集群后端的路线。当前代码已经有第一版底座，但父会话验收仍保留若干 P0/P1 修复项，详见 `ACCEPTANCE.md`。

### `DESIGN_LEDGER.md`

这是项目的设计思路台账。

它专门记录交流中形成的新想法、是否已经落地、落地位置和后续方向。

主台账只放导航和决策摘要。超过约 100 行或明显属于单个模块的详细设计，应拆到 `docs/design/`，并在这里保留链接。

后续 AI 如果听到用户提出新的架构想法，例如 skill 路由、tool 授权、能力上抛、自学习策略、命令语义变化，都要更新这份文件。

### `docs/design/`

这是模块级设计文档目录。

它负责承载会长期扩展的设计细节，例如模块背景、痛点、schema、配置开关、分阶段开发计划和验收策略。当前已有 subagent 质量契约设计和 log analysis 模块设计，后续 memory、gateway 等模块如果设计内容继续膨胀，也应按同样方式拆出独立文档。

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
## 2026-04-30 Tree Update

- `agent_py_agent/agent/subagent_workflows/router.py`: routes task text and explicit template ids to workflow templates under auto/manual/off config.
- `agent_py_agent/agent/subagent_workflows/compiler.py`: compiles template phases into worker dispatch specs with dependencies, boundaries, evidence rules, and `cannot_self_accept`.
- `agent_py_agent/agent/subagent_workflows/acceptance.py`: builds parent final-gate acceptance plans from workflow templates and quality contracts.
- `agent_py_agent/agent/log_analysis/storage/base.py`: now includes `JsonlReadAudit` for local JSONL read auditing.
- `agent_py_agent/agent/log_analysis/storage/local_store.py`: records last read audits and writes corrupt/non-object JSONL samples to `corrupt_lines.jsonl`.

## 2026-04-30 Tree Update: Runtime LOG Wiring

- `agent_py_agent/agent/agent_core/runtime_capabilities.py`: infers runtime capabilities from normal user prompts, currently focused on explicit or obvious `logs/security` tasks.
- `agent_py_agent/agent/agent_core/runtime_mixin.py`: passes inferred/granted capabilities into tool catalog rendering, recommendation rendering, and tool execution.
- `agent_py_agent/agent/log_analysis/storage/base.py`: `normalize_limit()` now accepts an optional configured `max_limit`.
- `agent_py_agent/agent/log_analysis/storage/query.py`: `execute_security_query()` passes the configured max limit into storage normalization.
- `agent_py_agent/agent/log_analysis/tools.py`: security query/hunt/trace tools accept optional `max_limit`.
- `agent_py_agent/cli/logs.py`: LOG CLI query commands honor `query_max_limit` from log-analysis config.
- `scripts/live_lab/log_analysis_replay.py`: offline SecurityAlertV1 replay command that emits case, route, report, forensic package, and replay summary artifacts.
- `scripts/live_lab/cases.py`: adds the `log_analysis_replay` Live Lab case.
- `scripts/live_lab/constants.py`: adds the `log-analysis` suite and includes it in the `all` suite.
- `agent_py_agent/agent/subagent_workflows/planner.py`: composes workflow routing, compilation, and parent acceptance into one dry-run planning facade.
- `agent_py_agent/tests/test_runtime_capabilities.py`: verifies ordinary prompts hide security tools, explicit grants expose them, and English/Chinese security-log prompts auto-grant them.
- `agent_py_agent/tests/test_live_lab_log_analysis_replay.py`: verifies offline replay artifacts and failure-stage semantics.
- `agent_py_agent/tests/test_subagent_workflow_planner.py`: verifies the planner facade for auto, manual, and off workflow modes.
