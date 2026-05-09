# Codebase Tree

这份文档做两件事：
- 在目录树上直接给关键文件加一句“这是干嘛的”，方便你扫一眼就知道位置。
- 在后面的详细说明里把职责再展开，方便后续继续扩展工具、记忆和循环智能体能力。

## Tree

> 2026-04-29 更新：大文件已经按职责拆分。旧入口文件仍保留兼容导入，新实现优先看 `cli/`、`agent_core/`、`tooling/`、`gateway_parts/`、`local_storage/`、`subagents/`。
> 第二轮更新：`agent/` 根目录散落实现已继续归位。目录含义详见 `agent_py_agent/agent/DIRECTORY_GUIDE.md`。
> 2026-05-07 更新：功能代码已统一补齐定义上方双层注释。每个 product module / class / function / method 都必须包含 `LLM:` 和 `模块用途:` / `函数用途:` / `类用途:`；新增入口时同步更新这里的文件树和开发规范。

### 当前功能代码地图

这张小树是后续架构评审和 LLM 开发优先看的版本；下面的大树保留更细文件入口和历史说明。

```text
agent_py_agent/
|-- cli/                                      # 命令行入口层，只做参数解析、展示和调用服务
|   |-- chat_parts/                           # 交互 chat 的 UI、历史、gateway client 和 fallback worker
|   |-- commands/                             # 命令行分组入口和小型 command helpers
|   |-- memory_commands/                      # memory route/query/doctor/compact/archive 子命令
|   |-- scenario_cases/                       # gateway、subagent、runner、repair 等确定性 scenario
|   |-- daemon.py                             # 前台常驻 subagent/watch 调度入口
|   |-- local_doctor.py                       # 本地状态体检、修复建议和重建入口
|   |-- local_status_payload.py               # status payload 组装
|   |-- local_status_view.py                  # status 用户可读展示
|   `-- parser.py                             # argparse 命令树和 main()
|-- agent/
|   |-- adapter/                              # 外部聊天/消息通道适配，例如 QQ、飞书和协议管理
|   |-- agent_core/                           # SimpleAgent 主循环、tool loop、dispatch、planner、runner、watch
|   |   `-- services/                         # 主循环可复用服务，例如 notification/watch
|   |-- audit/                                # 审计日志、任务访问记录和查询
|   |-- auth/                                 # 认证、权限中间件和用户身份模型
|   |-- backends/                             # 模型后端适配和流式解析
|   |-- capability/                           # skill/tool/capability 配置、路由和 card 解析
|   |-- concurrency/                          # 乐观锁、任务锁和透明重试
|   |-- extensions/                           # 插件/扩展声明和加载边界
|   |-- gateway_parts/                        # 文件协议 gateway：路径、队列、HTTP、worker、恢复、supervisor
|   |-- io/                                   # 底层 JSONL/文件 IO 原语
|   |-- local_storage/                        # LocalStore schema、records、events、search、maintenance、agent runtime control-plane
|   |-- log_analysis/                         # 日志分析接入、解析、查询、检测、case、派工和报告
|   |   |-- agents/                            # 日志分析子代理合同和 prompt
|   |   |-- analytics/                         # 检测器、规则、基线和特征提取
|   |   |-- cases/                             # case store、证据和调度
|   |   |-- config/                            # 日志分析配置加载和 coercion
|   |   |-- dispatch/                          # work order、queue、budget 和健康检查
|   |   |-- ingest/                            # parser pipeline、checkpoint、dedup、dead letter
|   |   |-- parsers/                           # 外部日志格式解析器
|   |   |-- security/                          # 安全实体图和检测辅助
|   |   |-- services/                          # 日志分析服务层通用 coercion/normalization
|   |   |-- storage/                           # 日志存储和查询 projection
|   |   `-- tools/                             # agent 可调用的日志查询/trace/hunt 工具
|   |-- memory_archive/                       # raw archive、task/run workspace、runtime facts、compact apply/resume/handoff/action-guard/auto/suggest/subagent-owner refs、memory gate
|   |   |-- query/                             # archive 查询/filter/payload 构造
|   |   |-- runtime/                           # run-time archive hook 和事件同步
|   |   `-- snapshots/                         # 快照模型和 snapshot IO
|   |-- memory_routing/                       # 长期规则索引、匹配、上下文注入和 receipt
|   |-- memory_store/                         # 长期 memory JSONL 存储和 LocalStore 索引
|   |-- model_speed/                          # 模型速度 benchmark 和模型描述
|   |-- notification/                         # 通知收件箱、过滤和用户可见摘要
|   |-- prompting_parts/                      # prompt 构造、工具 transcript 和上下文片段
|   |-- session/                              # 跨通道会话和 admin 查询
|   |-- settings/                             # 配置 schema、normalize、服务化 coercion
|   |   `-- services/                         # 配置字段归一化和 runtime/subagent 子配置
|   |-- subagent_workflows/                   # 子代理 workflow route/compile/plan 和内置模板
|   |-- subagents/                            # 子代理 manager facade、服务、验收、patch、runner、persistence
|   |   |-- acceptance_helpers/                # 父级验收 evidence/artifact/readiness helpers
|   |   |-- patch/                             # patch review/apply/render 服务
|   |   `-- services/                          # lifecycle/dispatch/indexing/actions/persistence 等服务
|   |-- task_registry/                        # 任务注册表和查找入口
|   |-- tooling/                              # 工具模型、文件/HTTP/shell 工具、注册表、写边界
|   |-- user_space/                           # 用户数据隔离路径和迁移
|   |-- clients/ repositories/ security/ validators/
|   |                                          # 预留边界目录；新增实现前先补设计说明
|   |-- core.py                               # SimpleAgent 兼容组合入口
|   |-- tools.py / subagent.py / gateway.py    # 兼容 re-export 入口，新逻辑在对应子目录
|   `-- startup_recovery.py                   # 启动时恢复检测和用户可读摘要
|-- config/                                   # 默认配置样例
|-- prompts/                                  # 默认 prompt 规则
`-- tests/                                    # 回归测试；不强制每个测试函数双层注释
```

```text
simple-python-agent-v0.3/                      # 项目根目录，放代码、说明文档和验证记录
|-- ONBOARDING.md                             # 新 LLM / 新开发者开工指南（先读这个）
|-- LLM_GUIDE.md                               # LLM/AI 开发者总入口，含开工前/收工后清单和编码规范
|-- AGENTS.md                                  # LLM 开发者行为规范（语言、注释、配置、自学习约束）
|-- pyproject.toml                             # Python packaging 配置，提供 my-agent console script
|-- CLI_REFERENCE.md                           # 完整 CLI 参数手册，说明每个命令和参数
|-- CODE_SIZE_POLICY.md                        # 代码尺寸限制政策
|-- CODE_SIZE_REPORT.md                        # 自动生成的代码规模报告
|-- CODE_SIZE_BASELINE.json                    # 严格模式基线（历史违规不阻断）
|-- ARCHITECTURE_EXEMPTIONS.md                 # 大文件/大类临时豁免登记
|-- REFACTORING_BACKLOG.md                     # 待拆文件优先级排序
|-- TESTING_POLICY.md                          # 测试分层和规范
|-- CLEAN_PACKAGE_POLICY.md                    # 打包洁净度规范
|-- docs/                                      # 长篇项目文档目录
|   |-- ROADMAP.md                             # 待做/进行中功能清单，开工前必读
|   |-- COMPLETED.md                           # 已落地功能清单，收工后必改
|   |-- architecture/                          # 架构文档
|   |   |-- BOUNDARY_RULES.md                  # 分层导入矩阵
|   |   |-- MODULE_OWNERSHIP.md                # 模块职责归属表
|   |   |-- CHAT_REFACTOR_PLAN.md              # chat.py 重构计划
|   |   `-- SUBAGENT_SERVICE_REFACTOR_PLAN.md  # SubAgent 服务化重构计划
|   |-- development/                           # 开发规范
|   |   |-- DEVELOPMENT_RULES.md               # 编码规则
|   |   |-- TESTING_RULES.md                   # 测试规则
|   |   `-- CODE_REVIEW_CHECKLIST.md           # Review 检查清单
|   |-- decisions/                             # 架构决策记录 (ADR)
|   `-- design/                                # 模块设计文档
|       |-- log-analysis.md                    # 日志分析模块设计
|       `-- subagent-quality-contract.md       # subagent 质量契约
|-- scripts/                                   # 开发辅助脚本和治理检查工具
|   |-- code_size_report.py                    # 代码规模检查 Markdown 报告渲染辅助模块
|   |-- check_code_size.py                     # 代码规模检查（warn/strict 模式，支持 baseline）
|   |-- check_clean_package.py                 # 脏文件检查（目录和 tar.gz）
|   |-- check_doc_sync.py                      # 文档同步检查
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
|   |   |-- scenario_cases.py                  # verification/gateway 坏天气/recovery/structured-repair/runner-retry 专项场景
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
|   |   |   |-- task_complexity.py            # 任务规模预判：基于 goal 关键词、plan 步骤数、工具数量估算轮数
|   |   |   |-- automation_guard.py            # 主代理代劳防护：根据自动化级别判断是否应派子代理
|   |   |-- file_io.py                         # 文件 I/O 兼容入口，真实实现已拆到 io/
|   |   |-- io/                                # 无业务含义的底层文件 I/O 原语，例如 locked JSONL append
|   |   |-- gateway.py                         # gateway 兼容入口，真实协议实现已拆到 gateway_parts/
|   |   |-- gateway_parts/                     # gateway 路径、IO、进程控制、恢复、运行时、adapter、索引日志
|   |   |   |-- daemon_control.py            # 守护进程控制：fork、 PID 文件、优雅关闭
|   |   |   |-- http_service.py              # HTTP 服务：POST /ask、GET /result/<id>、GET /status、POST /stop
|   |   |-- local_store.py                     # LocalStore 兼容组合入口，真实实现已拆到 local_storage/
|   |   |-- local_storage/                     # LocalStore models/schema/records/search/events/maintenance/control-plane
|   |   |-- log_analysis/                      # 可选日志分析底座，负责安全日志接入、解析、查询、检测、case、报告和 analyst 派工
|   |   |   |-- startup_recovery.py               # 启动时恢复检测，检测未完成的子代理任务和遗留 gateway 请求
|   |   |   |-- models.py                      # 核心数据模型：Case/LogWorkOrder/SecurityCase/QueryResult/SecurityAlertV1 等
|   |   |   |-- work_order.py                  # LogWorkOrder -> SubAgentTask 转换器，桥接日志补查到子代理系统
|   |   |   |-- bounded_query.py               # 受控查询工具，提供时间窗口和结果数量限制的日志查询功能
|   |   |   |-- agents/                        # 日志分析子代理合同和 runner 模板
|   |   |   |-- analytics/                     # 检测、ML、统计和特征提取
|   |   |   |-- cases/                         # Case 创建、更新、状态流转和检索
|   |   |   |-- config/                        # 日志分析配置加载和验证
|   |   |   |-- dispatch/                      # 日志分析任务调度和子代理派遣
|   |   |   |-- ingest/                        # 日志接入、解析、去重和 checkpoint
|   |   |   |-- parsers/                       # 日志解析器注册表和实现
|   |   |   |-- security/                      # 安全检测器和规则
|   |   |   |-- storage/                       # 日志分析专用存储接口
|   |   |   `-- tools/                         # 日志分析专用工具
|   |   |-- memory.py                          # 记忆兼容入口，真实实现已拆到 memory_store/
|   |   |-- memory_settings.py                 # memory 配置安全解析兼容入口，真实实现已拆到 settings/memory.py
|   |   |-- memory_store/                      # 长期记忆存储，当前是 JSONL + LocalStore 索引
|   |   |-- memory_archive/                    # raw/hook/snapshot、runtime workspace/facts、schema v2、control-plane query、tool-output artifacts 和非破坏性 compact apply/resume/continue packet/subagent-owner refs
|   |   |-- memory_routing/                    # 长期规则索引化路由，负责 MEMORY -> index -> authority file 的确定性匹配
|   |   |-- observability/                     # 未来 request_id、耗时、状态、错误码、metrics、trace 目录
|   |   |   `-- user_space/                    # 用户数据隔离：路径解析、目录管理、迁移工具
|   |   |       |-- __init__.py               # 模块导出
|   |   |       |-- paths.py                 # UserPaths 数据类和 get_user_paths() 函数
|   |   |       |-- manager.py               # UserSpaceManager：用户目录创建、访问控制、列表
|   |   |       `-- migration.py              # migrate_to_user_space() 迁移工具
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
|   |   |   `-- registry_execution.py          # ToolRegistry 的工具调用解析、授权检查和执行分发 helper
|   |   `-- validators/                        # 未来跨领域校验规则目录，目前用 README 定义边界
|   |-- config/                                # 配置目录
|   |   |-- agent_config.yaml                  # 运行配置文件，控制模型、记忆、工具和检索参数
|   |   |-- capability_config.yaml            # 能力路由配置文件，控制 skill/tool 授权、上抛和候选数量
|   |   `-- log_analysis_config.yaml          # 日志分析模块独立配置，默认关闭高影响能力
|   |-- data/                                  # 运行时数据目录
|   |   |-- memory.jsonl                       # 长期记忆文件
|   |   |-- gateway/                           # gateway pid/state/heartbeat/log/stop request、inbox 和 response 输出目录
|   |   |-- local_store/                       # SQLite 本地事实源、正文文件和追加式事件流水
|   |   |-- log_fixtures/                      # 日志分析测试用 fixture，包含 sample_cases.json 等
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
|       |-- test_log_analysis_first_loop.py    # 日志分析第一轮循环：SecurityCase/LogWorkOrder/work_order->subagent/bounded_query
|       |-- test_log_analysis_ingest.py        # SecurityAlertV1 CSV/JSONL 接入、checkpoint、dedup 和 dead letter 测试
|       |-- test_log_analysis_models.py        # 日志分析模型和配置测试
|       |-- test_log_analysis_query.py         # 本地日志查询、evidence 和 hunting tool 测试
|       |-- test_memory_archive.py             # 压缩前快照、raw 冷归档、留存策略和 token 估算测试
|       |-- test_memory_archive_runtime.py     # run turn 冷归档 helper、稳定 event_id/hash 和工具元数据测试
|       |-- test_compact_parent_acceptance_flow.py # compact resume continue packet 与 parent acceptance auto-policy 联调边界测试
|       |-- test_memory_compact.py              # compact apply/resume、handoff、completion prompt 和手动 fact-write 闭环测试
|       |-- test_memory_compact_auto.py        # 自动 compact/resume 协调器的 plan-only 和 action guard 停车测试
|       |-- test_memory_cli.py                 # memory-route / memory-doctor CLI 可见诊断测试
|       |-- test_memory_config.py              # memory 配置安全默认、非法值回退和 warning receipt 测试
|       |-- test_memory_routing.py             # 长期规则 route 加载、匹配、soft/strict 解析和索引诊断测试
|       |-- test_memory_routing_context.py     # runtime rule routing context 安全读取和 prompt 片段测试
|       |-- test_memory_runtime.py             # SimpleAgent.run 接入 routed memory 与 raw archive 的回归测试
|       |-- test_packaging.py                  # console script、workspace_root 等安装与配置行为测试
|       |-- test_subagent_quality_contract.py  # 子代理 QualityContract / ContextManifest 落盘和执行上下文测试
|       |-- test_subagent_workflow_config.py   # subagent workflow auto/manual/off 配置规范化测试
|       |-- test_task_complexity.py            # 任务规模预判测试
|       |-- test_automation_guard.py           # 主代理代劳防护测试
|       |-- test_gateway_http.py               # gateway HTTP 服务测试
|       `-- test_user_space.py                 # 用户数据隔离测试
|       |-- test_subagent_workflow_templates.py # workflow 模板加载、覆盖和校验测试
|       `-- test_tools.py                      # 工具目录、工具调用和工具能力测试
|-- .gitattributes                             # 跨平台文本编码和换行约定
|-- .gitignore                                 # Git 忽略规则
|-- ACCEPTANCE.md                              # 验收记录
|-- AGENTS.md                                  # AI 开发规范，约束后续开发、配置、文档和自学习改动
|-- CODEBASE_TREE.md                           # 当前这份目录树说明
|-- DESIGN_LEDGER.md                           # 设计思路台账，记录新想法、落地状态和后续方向
|-- DISCUSSION_BACKLOG.md                      # 功能开发之外的系统问题讨论清单（已移至 docs/）
|-- EVIDENCE.md                                # 过程证据记录
|-- RESULT.md                                  # 结果记录
|-- RUNLOG.md                                  # 运行日志说明
|-- SKILL_SPARK.yaml                           # 项目任务描述
|-- SPEC.md                                    # 原始需求规格
|-- STATUS.md                                  # 当前阶段状态说明
|-- SUBAGENT_RUNBOOK.md                        # subagent、capability 和 runner 详细运行手册（已移至 docs/modules/subagent/）
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
- `agent_py_agent/agent/tooling/registry_execution.py` 承接工具调用块解析、payload 规范化、授权检查、写边界检查和异常格式化，避免注册表类继续膨胀。

当前内置工具包括：
- `list_files`：列目录，适合先摸清项目结构。
- `read_file`：读文本文件，适合查看代码、配置和文档。
- `search_text`：搜索文本，适合先找函数名、配置项和关键字。
- `write_file`：写入或覆盖整个文本文件，适合创建新文件或完整重写。
- `append_file`：向文件末尾追加内容，适合补日志、补文档和补配置片段。
- `replace_in_file`：精确替换文件中的一段已有内容，适合小范围改代码、改配置和改说明。
- `fetch_url`：抓取网页或文本接口内容，适合查在线文档。
- `http_request`：发送 HTTP 请求，适合测试 REST API、Webhook 和普通接口。

### `WORKSTREAMS.md`（已移至 `docs/WORKSTREAMS.md`）

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

### `MEMORY_BACKLOG.md`（已移至 `docs/MEMORY_BACKLOG.md`）

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
- `DispatchRecord` / `DispatchReport`：记录父代理一轮调度中的 due-check、路由、runner、patch 审核和验收步骤；acceptance record 还可携带 parent acceptance auto-policy 的 refs-only 摘要字段。
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
- `write_dispatch_report()` 会写出 `subagent_dispatch_report.json` 和 `SUBAGENT_DISPATCH.md`，apply 时追加调度审计日志；当 acceptance 记录生成 parent auto-policy dry-run 时，报告只展示 policy ref 和摘要，不执行 policy 动作。
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
- 父级验收真实测试执行：`acceptance_execute_tests` 默认关闭，`acceptance_test_timeout_seconds` 控制真实执行 tests 的单条超时。
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

### `SUBAGENT_RUNBOOK.md`（已移至 `docs/modules/subagent/SUBAGENT_RUNBOOK.md`）

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

### `LOG_ANALYSIS_BACKLOG.md`（已移至 `docs/LOG_ANALYSIS_BACKLOG.md`）

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

## 2026-05-08 Tree Update: Acceptance Progress CLI Split

- `agent_py_agent/cli/acceptance_progress.py`: 新增父级验收进度展示 helper，负责 `Acceptance Plan` 和 `Acceptance Next Action` 的 refs-only payload/人类输出；只做投影，不执行 tests、不读取 artifact 正文。
- `agent_py_agent/cli/shared_progress.py`: 保留共享进度面板和 takeover view 组装职责，并复用 `acceptance_progress.py` 的验收投影 helper，避免 status/board 展示层继续膨胀。

## 2026-04-30 Tree Update: Workflow Plan CLI and Replay Gates

- `agent_py_agent/cli/subagents.py`: adds `cmd_subagents_workflow_plan`, a dry-run workflow preview that prints selected template, worker specs, parent acceptance checklist, and issues.
- `agent_py_agent/cli/parser.py`: adds the `subagents-workflow-plan` command with `goal`, `--template-id`, and `--json`.
- `agent_py_agent/__main__.py`: re-exports `cmd_subagents_workflow_plan` for compatibility with the split CLI entrypoint.
- `validation/security_fixtures/security_alert_v1_no_findings.jsonl`: readable negative fixture that should ingest but fail at detector stage with no findings.
- `scripts/live_lab/log_analysis_replay.py`: replay summary now includes fixture format, parsed/stored/dead-letter/duplicate/skipped counts, and structured error fields.
- `README.md` and `CLI_REFERENCE.md`: document LOG quickstart, ordinary-language runtime capability behavior, offline replay, and `subagents-workflow-plan`.

## 2026-04-30 Tree Update: LOG Work Orders and Preview Persistence

- `agent_py_agent/agent/log_analysis/dispatch/work_orders.py`: dry-run planner that turns one LOG case into analyst/reviewer work-order specs without creating real subagents.
- `agent_py_agent/agent/log_analysis/dispatch/__init__.py`: exports LOG work-order planning helpers.
- `agent_py_agent/agent/subagent_workflows/planner.py`: adds audit-friendly `WorkflowPlanningResult.to_dict()` and `write_workflow_plan_preview()`.
- `agent_py_agent/cli/subagents.py`: `subagents-workflow-plan` can write JSON/Markdown previews with `--output-dir`.
- `scripts/live_lab/log_analysis_replay.py`: validation-only `simulate_failure_stage` can exercise evidence/report failure gates from tests.
## 2026-04-30 文档结构补充

新增模块文档入口：`docs/modules/`。这里按功能模块保存四件套文档，不移动旧文档，只做新入口和逐步补齐。

```text
docs/
|-- README.md                         # docs 顶层导航，说明 design 与 modules 的关系
|-- design/                           # 旧有长篇设计文档索引，继续承接 DESIGN_LEDGER 的长设计细节
`-- modules/                          # 按功能模块组织的四件套文档
    |-- README.md                     # 四件套规范、命名、更新时机和测试记录要求
    |-- _template/                    # 新模块可复制的 01-04 模板
    |-- subagent/                     # subagent 首批四件套索引
    |-- log-analysis/                 # log-analysis 首批四件套索引
    |-- memory/                       # memory 四件套索引
    |-- gateway/                      # gateway 四件套索引
    `-- live-lab/                     # Live Lab 四件套索引
```

模块四件套固定为：`01-discussion.md`、`02-progress.md`、`03-purpose.md`、`04-structure.md`。其中 `02-progress.md` 必须保留“已完成 / 解决的问题 / 下一步 / 已跑测试 / 未跑测试 / 风险”六段，方便并行 worker 和初学者快速判断模块状态。

## 2026-04-30 文档同步门补充

- `scripts/check_doc_sync.py`: 检查 covered module 的代码改动是否同步更新 `docs/modules/<module>/02-progress.md` 和 `04-structure.md`，并要求实现代码改动同文件维护双层注释。
- 当前 covered module：`log-analysis`、`subagent`、`memory`、`gateway`、`live-lab`。
- `agent_py_agent/tests/test_doc_sync.py`: 覆盖同步门规则、必需文档存在性和缺注释/缺文档的失败路径。

## 2026-05-02 Tree Update: Model Speed Profiling and Adaptive Retry

- `agent_py_agent/agent/model_speed/` (新目录): 模型速度基准测试和速度配置文件管理
  - `models.py`: SpeedProfile、SpeedSample 数据类，支持对数线性插值
  - `benchmark.py`: run_speed_benchmark() 函数，运行速度测试
  - `storage.py`: 保存和加载 speed_profile.json
  - `__init__.py`: 模块导出

- `agent_py_agent/agent/agent_core/dynamic_timeout.py`: 动态超时计算模块
  - `calculate_dynamic_timeout()`: 根据输入大小和速度模型计算超时
  - `estimate_tokens_from_text()`: 粗略估算文本 token 数
  - `estimate_task_tokens()`: 根据任务描述和计划估算 token 数

- `agent_py_agent/agent/agent_core/failure_analyzer.py`: 失败分析器模块
  - `FailureAnalysis` 数据类，包含失败根因、建议动作和重试/拆分标志
  - `SubAgentFailureAnalyzer` 类，支持超时、能力缺口、解析错误、工具失败、模型错误、通道损坏、验收失败等多种失败类型
  - `_suggest_splits()`: 根据任务内容生成拆分建议

- `agent_py_agent/agent/agent_core/adaptive_retry.py`: 自适应重派模块
  - `adaptive_retry()`: 根据分析结果决定重试、拆分或停止
  - `split_task()`: 把大任务拆分成多个小任务
  - `should_auto_split()`: 判断是否应该自动拆分
  - `estimate_split_count()`: 估算应该拆分成几个子任务

- `agent_py_agent/cli/bench_model.py`: 模型速度测试 CLI 命令
  - `cmd_bench_model()`: 运行速度测试或查看已有速度模型

- `agent_py_agent/agent/subagents/models.py`: SubAgentTask 新增 `attributes: dict[str, object]` 字段
  - 用于存储动态超时、拆分信息等运行时属性

- `agent_py_agent/agent/agent_core/runner_dispatch.py`: 集成动态超时
  - 新增导入 `calculate_dynamic_timeout`
  - 支持从任务 attributes 读取动态超时

- `agent_py_agent/agent/agent_core/dispatch_mixin.py`: 集成失败分析和自适应重派
  - 新增导入 `adaptive_retry`、`calculate_dynamic_timeout`、`SubAgentFailureAnalyzer`
  - 在 runner 失败后调用失败分析和自适应重派逻辑
  - 新增动态超时计算函数 `_get_task_timeout()`

- `agent_py_agent/cli/parser.py`: 添加 `bench-model` 子命令
  - 新增导入 `cmd_bench_model`
  - 支持 `--show` 参数查看已有速度模型

- `agent_py_agent/__main__.py`: 导出 `cmd_bench_model`

- `agent_py_agent/config/agent_config.yaml`: 新增动态超时和自适应相关配置项
  - `subagent_automation_level`: 子代理自动化级别
  - `dynamic_timeout_safety_margin`: 动态超时安全边际
  - `dynamic_timeout_min`: 动态超时下限
  - `dynamic_timeout_max`: 动态超时上限
  - `max_auto_split_depth`: 失败后最大自动拆分次数
  - `model_speed_profile_path`: 模型速度配置文件路径
  - `auto_bench_model_on_first_use`: 是否在首次使用时自动运行速度测试

- `agent_py_agent/agent/settings/config.py`: AgentConfig 新增对应字段

- `agent_py_agent/tests/test_dynamic_timeout.py`: 动态超时测试（13 tests）
  - 测试 token 估算、任务 token 估算、动态超时计算、边界情况、自定义参数

- `agent_py_agent/tests/test_failure_analyzer.py`: 失败分析器测试（15 tests）
  - 测试各种失败类型的分析结果、拆分建议生成

- `agent_py_agent/tests/test_adaptive_retry.py`: 自适应重派测试（18 tests）
  - 测试自适应重派策略、任务拆分、自动拆分判断、拆分数量估算

## 2026-05-08 Tree Update: Agent Runtime Control Plane

- `agent_py_agent/agent/local_storage/control_plane_models.py`: 定义 agent run、agent event、task rollup、runtime query context 和任务树查询结果的数据结构，保留 `metadata` / `reserved` 给后续继承策略、共享面板和失败交接扩展。
- `agent_py_agent/agent/local_storage/control_plane.py`: 给 LocalStore 增加控制面 API，支持 upsert run、记录事件、重建 rollup、查询 root task 树、查询子树、blocked runs 和带 requester/scope 的 runtime query。
- `agent_py_agent/agent/local_storage/control_plane_panel.py`: 给 LocalStore 增加共享进度面板查询，把 runtime query、rollup、blocked runs 和 inheritance refs 组合成上级/接管代理可读状态包。
- `agent_py_agent/agent/local_storage/control_plane_codec.py`: 集中维护控制面 SQLite SQL、参数组装和行转换，避免公开 mixin 因 SQL 细节膨胀。
- `agent_py_agent/cli/shared_progress.py`: 给 `status` 和 `subagents` CLI 生成共享进度摘要，展示 blocked、failure handoff refs 和 takeover packet refs 数量，不读取正文。
- `agent_py_agent/agent/agent_core/tool_output_failsafe.py`: 大工具输出写 artifact 前写 fail-safe recovery snapshot，只记录工具名、hash、大小和恢复建议。
- `agent_py_agent/agent/agent_core/tool_context_reducer.py`: 大工具输出进入下一轮 live prompt 前只注入 artifact 摘要和 checkpoint refs，小输出仍保留原工具结果。
- `agent_py_agent/agent/subagents/services/control_plane_projection.py`: 在 subagent 保存时把 task 当前状态投影到 LocalStore 控制面；它只做查询索引，不替代旧工单目录或 runtime workspace 事实源。
- `agent_py_agent/agent/subagents/services/inheritance_manifest.py`: 创建 child task 时生成继承清单，记录 inherited / overridden / dropped 项；它是 audit-only，不自动扩大子代理上下文。
- `agent_py_agent/agent/subagents/services/persistence_inheritance.py`: 负责继承清单的读取归一化和 `reports/inheritance_manifest.json` 写入，避免 persistence 主流程继续膨胀。
- `agent_py_agent/agent/subagents/services/failure_handoff.py`: 生成失败交接记录，保存 warning、risk level、checkpoint refs、artifact/evidence refs、避坑建议和推荐下一步。
- `agent_py_agent/agent/subagents/services/persistence_failure_handoff.py`: 负责失败交接记录的读取归一化和 `reports/failure_handoff.json` 写入，避免 persistence 主流程继续膨胀。
- `agent_py_agent/agent/subagents/services/persistence_recovery_outputs.py`: 集中写入 checkpoint artifacts 和 takeover readiness 文件，让 persistence 主流程保持薄编排。
- `agent_py_agent/agent/subagents/services/takeover_readiness.py`: 生成接管前必读包 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md`，只保存 refs、artifact manifest 元数据和读取顺序，不读取大正文。
- `agent_py_agent/agent/subagents/rendering_rescue.py`: 渲染 rescue packet 的 refs-only 摘要，避免主 `rendering.py` 因接管/救援展示继续膨胀。
- `agent_py_agent/agent/memory_archive/compact_resume_failsafe.py`: 从 compact restore refs 指向的 hook JSONL 中提取工具输出外置前 fail-safe checkpoint，保持 memory-resume refs-only。
- `agent_py_agent/agent/memory_archive/compact_continue_packet.py`: 把 compact resume 后的 work_state、action guard、推荐读取路径和 subagent owner refs 固定成继续工作包；它只表达恢复上下文可继续，不执行工具或业务验收。
- `agent_py_agent/agent/memory_archive/compact_resume_blocked.py`: 生成 compact metadata 缺失时的 schema-compatible 阻断 payload，让主 resume 编排保持薄。
- `agent_py_agent/agent/memory_archive/artifact_reader.py`: 按 tool output index 显式读取外置 artifact 正文切片，并校验路径边界和 sha256。
- `agent_py_agent/agent/tooling/artifact.py`: 注册 `read_artifact` 工具，给模型提供受控 artifact slice 读取入口。
- `agent_py_agent/cli/memory_artifact_commands.py`: 提供 `memory-artifact-read` 命令，保持 artifact 正文读取和 archive resume/search CLI 分离。
- `agent_py_agent/cli/memory_resume_compact_rendering.py`: 输出 `memory-resume --from-compact` 的 handoff、continue packet、completion prompt 和推荐路径。
- `agent_py_agent/agent/subagents/services/persistence_security.py`: 负责 `SecuritySignal` 预留字段的读取归一化，避免 persistence 主流程继续膨胀；当前不执行安全策略。
- `agent_py_agent/agent/subagents/services/persistence_identity.py`: 负责 `RuntimeIdentity` 预留字段的读取归一化，保证员工/会话/配置 scope 只作为审计元数据进入 task 记录。
- `agent_py_agent/agent/subagents/model_task.py`: 新增 `SecuritySignal` 和 `security_review_required` 安全预留字段，用于记录安全劫持、安全欺骗、prompt injection、工具权限异常等可疑信号；当前只审计不拦截。
- `agent_py_agent/agent/subagents/execution_records.py`: 新增 `TestExecutionRecord`，定义真实验收执行证据、输出截断和通过结果派生。
- `agent_py_agent/agent/subagents/execution_executor.py`: 新增最小 `TestExecutor`，执行 command/file/content 三类检查并产出 `TestExecutionRecord`；当前不接 acceptance 自动写回。
- `agent_py_agent/agent/subagents/execution_report.py`: 新增 `test_execution.json` / `test_execution.md` 报告写读入口；JSON 是机器事实源，Markdown 只做展示。
- `agent_py_agent/agent/subagents/parent_acceptance_controller.py`: 新增父级验收 dry-run 决策器和 refs-only 决策落盘 helper，读取 `output.json`、`test_execution.json` 和 handoff refs，返回 execute_tests / inspect_only / request_human / rescue；显式写入生成 `parent_acceptance_decision.json`，不读取 artifact 正文。
- `agent_py_agent/agent/subagents/parent_acceptance_apply.py`: 新增显式 apply 结果模型、拦截/应用结果构造和 `parent_acceptance_apply.json` 落盘 helper；非 inspect_only 决策只留下拦截审计，不改任务状态。
- `agent_py_agent/agent/subagents/parent_acceptance_next_action.py`: 新增父级下一动作建议模型，把当前决策/apply 审计映射成 run_tests / request_human_confirmation / plan_rescue / apply_acceptance；只返回 refs 和建议命令，不执行。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_policy.py`: 新增父级自动策略 dry-run 模型和 `parent_acceptance_auto_policy.json` 审计落盘；第一版只判断 allow/blocked、would_execute、manual-only 半自动计划和 preflight 检查，不执行命令、不改状态。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_execution.py`: 新增父级自动执行 dry-run facade 的 Request/Result bundle 和 `parent_acceptance_auto_execution.json` 审计落盘；第一版固定 hard guard，不执行命令、不改状态。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_execution_reports.py`: 拆出显式测试执行后的 report/follow-up 写入 helper，让 executor facade 保持薄层和 bundle 入口。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_followup.py`: 新增显式验收测试后的 follow-up 审计包，写 `parent_acceptance_auto_followup.json`，归类人工 apply / 人工 rescue / 人工确认 / 继续测试；只保存 refs 和失败摘要，不改 task 状态。
- `agent_py_agent/agent/subagents/manager_parent_acceptance.py`: 新增 manager 父级验收桥接函数，把 plan/write/apply/next-action/auto-policy 流程从 `manager_acceptance.py` 类体拆出，保持 manager facade 轻量。
- `agent_py_agent/agent/subagents/acceptance_test_execution.py`: 新增显式验收测试执行桥接，把 `AcceptanceReviewOptions(execute_tests=True)` 转成真实测试报告和阻断 findings；默认不运行。
- `agent_py_agent/agent/subagents/services/acceptance_findings.py`: 普通验收 finding 汇总层；已有 `reports/test_execution.json` 时优先以机器执行报告判断 tests_passed。
- `agent_py_agent/agent/settings/config.py`: 新增 `acceptance_execute_tests` 和 `acceptance_test_timeout_seconds`，让真实测试执行可配置但默认关闭。
- `agent_py_agent/agent/settings/services/_normalize_runtime_fields.py`: 校验真实验收执行配置，布尔开关走 bool coerce，超时限制在 1 到 300 秒。
- `agent_py_agent/config/agent_config.yaml`: 新增父级验收真实执行配置注释，说明默认关闭和单次命令覆盖方式。
- `agent_py_agent/cli/_acceptance_plan.py`: 新增 `subagents-acceptance-plan` 命令渲染；默认只展示父级 dry-run 决策和 refs，`--write` 写入 `parent_acceptance_decision.json`，`--apply` 只允许 inspect_only 进入普通验收 apply，`--next-action` 只展示上级动作建议，`--auto-policy` 写策略 dry-run 审计，`--auto-execution` 写执行 facade 审计；不执行 tests、不读取 artifact 正文。
- `agent_py_agent/cli/_review.py`: 新增 `subagents-tests` 命令入口并兼容导出验收命令；tests 命令默认只读已有 `test_execution.json` 摘要，`--re-run` 才显式执行 `output.json.tests` 并写回报告。
- `agent_py_agent/cli/subcommands_agents.py`: 注册 `subagents-tests <run_id> [--re-run] [--timeout]` 和 `subagents-acceptance-plan <run_id> [--json] [--write] [--apply] [--next-action] [--auto-policy]`，并给 `subagents-acceptance` 增加 `--execute-tests` / `--no-execute-tests` / `--test-timeout`。
- `agent_py_agent/cli/shared_progress.py`: 在共享进度 payload 中新增 `acceptance_plan_entries`，供 `status --json`、人类 `status` 和 `subagents` 看板展示父级验收 dry-run 决策；不执行 tests、不读取 artifact 正文。
- `agent_py_agent/cli/local_status_view.py`: 新增 `Acceptance Plan` 人类可读区块，渲染 shared-progress 里的父级验收决策摘要。
- `agent_py_agent/cli/_board.py`: `subagents` 看板新增 `Acceptance Plan` 区块，和 status 共用同一套 refs-only 渲染。
- `agent_py_agent/tests/test_local_store_control_plane.py`: 覆盖 LocalStore 控制面表、rollup 计算、上级/中间子代理 runtime query，以及 `SubAgentPersistenceService.save()` 的投影写入路径。
- `agent_py_agent/tests/test_local_store_shared_progress_panel.py`: 覆盖共享进度面板如何组合 runtime query、task rollup、blocked runs、inheritance manifest refs 和 takeover readiness refs。
- `agent_py_agent/tests/test_subagent_inheritance_manifest.py`: 覆盖 parent/child 创建时的继承、覆盖、裁剪记录和 manifest JSON 落盘。
- `agent_py_agent/tests/test_subagent_failure_handoff.py`: 覆盖失败/阻塞 run 保存时的 failure handoff JSON 落盘和 LocalStore metadata refs。
- `agent_py_agent/tests/test_subagent_takeover_readiness.py`: 覆盖接管前必读包生成、落盘和不读取 artifact 正文的边界。
- `agent_py_agent/tests/test_subagent_security_reserve.py`: 覆盖安全信号预留字段随 task 持久化，并投影到 LocalStore metadata。
- `agent_py_agent/tests/test_subagent_test_execution_record.py`: 覆盖真实验收执行记录模型的序列化、stdout/stderr 截断和 `passed` 语义。
- `agent_py_agent/tests/test_subagent_test_executor.py`: 覆盖最小真实验收执行器的 command、危险字符拦截、file_check 和 content_check 行为。
- `agent_py_agent/tests/test_subagent_test_execution_report.py`: 覆盖 test execution JSON/Markdown 报告的汇总字段、记录恢复和人类摘要。
- `agent_py_agent/tests/test_parent_acceptance_controller.py`: 覆盖父级验收 dry-run 决策、refs-only 审计落盘、显式 apply 边界、next-action 建议和 auto-policy dry-run，包括缺少真实测试报告时建议执行、危险命令要求人工确认、已有通过报告时只需 inspect，以及非 inspect_only apply 不改任务状态。
- `agent_py_agent/tests/test_agent/test_subagent_acceptance.py`: 新增显式真实测试执行 dry-run 验收用例，覆盖 runner 假 PASS 被真实命令失败阻断且任务状态不被 dry-run 改写。
- `agent_py_agent/tests/test_subagents_tests_command.py`: 覆盖 `subagents-tests` 查看已有报告、显式 `--re-run` 写回报告、`subagents-acceptance-plan` 展示/写入/显式 apply/next-action/auto-policy 父级决策，以及 `subagents-acceptance` 从配置读取真实执行默认值并被 CLI 覆盖。
- `agent_py_agent/tests/test_config_validation.py`: 覆盖 `acceptance_execute_tests` 默认关闭、布尔 coerce 和 `acceptance_test_timeout_seconds` 范围校验。
- `agent_py_agent/tests/test_status_shared_progress.py`: 覆盖 `status` / `subagents` CLI 展示共享进度、failure handoff refs、takeover packet refs 和父级验收 dry-run 摘要，并断言大 artifact 正文不会内联。
- `agent_py_agent/tests/test_tool_output_externalizer.py`: 覆盖大工具输出外置 artifact 和外置前 fail-safe recovery snapshot。
- `agent_py_agent/tests/test_memory_compact_failsafe.py`: 覆盖 `memory-resume --from-compact` 如何展示 fail-safe checkpoint refs 且不读取 artifact 正文。
- `agent_py_agent/tests/test_memory_artifact_read.py`: 覆盖 CLI 和 `read_artifact` 工具如何显式读取已登记 artifact，并拒绝未登记普通文件。
