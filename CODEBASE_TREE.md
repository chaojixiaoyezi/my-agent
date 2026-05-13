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
|   |-- chat_parts/                           # 交互 chat 的 UI、历史、gateway client、fallback worker、TUI activity/scrollback helpers
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
|   |-- backends/                             # 模型后端适配、流式解析和 ProviderTimeoutError 等错误边界
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
|   |   |   `-- resume_guidance.py             # 恢复建议 bundle，生成 refs-only 推荐读取路径和 next actions
|   |   |-- runtime/                           # run-time archive hook 和事件同步
|   |   `-- snapshots/                         # 快照模型和 snapshot IO
|   |-- memory_routing/                       # 长期规则索引、匹配、上下文注入和 receipt
|   |-- memory_store/                         # 长期 memory JSONL 存储和 LocalStore 索引
|   |-- model_speed/                          # 模型速度 benchmark 和模型描述
|   |-- notification/                         # 通知收件箱、过滤和用户可见摘要
|   |-- prompting_parts/                      # prompt 构造、工具 transcript 和上下文片段
|   |-- session/                              # 跨通道会话和 admin 查询
|   |-- settings/                             # 配置 schema、normalize、服务化 coercion
|   |   |-- home_config.py                     # 家目录、外部知识库、provider 空间字段组，避免 AgentConfig 类体膨胀
|   |   `-- services/                         # 配置字段归一化和 runtime/subagent/home/provider 子配置
|   |-- external_knowledge/                   # 外部知识库配置 bundle；后续接目录/API/数据库查询
|   |-- subagent_workflows/                   # 子代理 workflow route/compile/plan 和内置模板
|   |-- subagents/role_template_resolution.py # 自然 role 名到模板 id 的运行时解析
|   |-- subagents/role_templates.py           # 子代理广义角色模板加载、校验和查询
|   |-- subagents/role_template_catalog/      # 内置外置 JSON role templates
|   |-- subagents/workflow_template_catalog/  # workflow 模板外置化预留目录
|   |-- subagents/                            # 子代理 manager facade、服务、验收、patch、runner、persistence
|   |   |-- capability_request_identity.py      # 能力申请 scope 签名和去重
|   |   |-- acceptance_helpers/                # 父级验收 evidence/artifact/readiness helpers
|   |   |-- patch/                             # patch review/apply/render 服务
|   |   `-- services/                          # lifecycle/dispatch/indexing/actions/persistence 等服务
|   |-- task_registry/                        # 任务注册表和查找入口
|   |-- tooling/                              # 工具模型、文件/HTTP/shell 工具、注册表、写边界
|   |-- user_space/                           # 用户数据隔离、my-agent 家目录、provider 用户/群空间和迁移
|   |-- clients/ repositories/ security/ validators/
|   |                                          # 预留边界目录；新增实现前先补设计说明
|   |-- core.py                               # SimpleAgent 兼容组合入口
|   |-- tools.py / subagent.py / gateway.py    # 兼容 re-export 入口，新逻辑在对应子目录
|   `-- startup_recovery.py                   # 启动时恢复检测和用户可读摘要
|-- config/                                   # 默认配置样例
|-- prompts/                                  # 默认 prompt 规则
`-- tests/                                    # 回归测试；不强制每个测试函数双层注释
frontend/
|-- config/                                   # 前端集中配置；backend-config-catalog.json 由后端 YAML 生成
|-- scripts/                                  # 前端同步/检查脚本，例如 sync-backend-config.mjs
|-- src/
|   |-- api/                                  # mock API 边界；后续替换为真实后端 API
|   |-- components/                           # 配置表单、设置页、通用 UI 组件
|   |-- data/                                 # 读取生成配置和 runtime config 的轻包装
|   |-- pages/                                # Dashboard/Config/Subagents/Memory/Tools/Logs/Templates/Settings 页面
|   |-- stores/                               # Zustand 状态；不再硬编码后端默认参数
|   `-- types/                                # 前端配置 schema 和 UI 类型
`-- test-settings.mjs                         # Playwright 页面 smoke；默认检查 http://localhost:3000
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
|   |   |-- MY_AGENT_HOME_LAYOUT.md            # ~/.my-agent 家目录、provider 空间、外部知识库和 memory 区域约定
|   |   |-- CHAT_REFACTOR_PLAN.md              # chat.py 重构计划
|   |   `-- SUBAGENT_SERVICE_REFACTOR_PLAN.md  # SubAgent 服务化重构计划
|   |-- development/                           # 开发规范
|   |   |-- DEVELOPMENT_RULES.md               # 编码规则
|   |   |-- TESTING_RULES.md                   # 测试规则
|   |   `-- CODE_REVIEW_CHECKLIST.md           # Review 检查清单
|   |-- decisions/                             # 架构决策记录 (ADR)
|   |-- modules/                               # 分模块长文档和持续追加台账
|   |   `-- subagent/                          # subagent 讨论、进度、结构、计划、真实 E2E 问题台账
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
|   |   |-- home_runtime_commands.py           # home-status / memory-daily-list / task-workspace-list 只读调试命令
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
|   |   |   |-- runtime_config.py              # capability_config 运行期补丁和热加载兼容导出
|   |   |   |-- runtime_config_models.py       # capability_config 补丁、结果和 snapshot 数据模型
|   |   |   |-- runtime_config_patch.py        # capability_config 结构化补丁、allowlist、审计和通知
|   |   |   |-- runtime_config_patch_io.py     # capability_config YAML 写入、审计 JSONL 和通知 I/O
|   |   |   `-- runtime_config_reload.py       # capability_config 默认路径、版本校验和热加载 snapshot
|   |   |-- clients/                           # 未来非模型外部服务客户端目录，目前用 README 定义边界
|   |   |-- config.py                          # 配置兼容入口，真实实现已拆到 settings/
|   |   |-- settings/                          # 配置结构、简化 YAML 加载器和环境变量覆盖
|   |   |-- core.py                            # SimpleAgent 兼容组合入口，真实实现已拆到 agent_core/
|   |   |-- agent_core/                        # 主循环、子代理 runner、planner、dispatch、编排工具、runner 规则
|   |   |   |-- capability_config_patch_tool.py # 模型可调用的 capability_config 安全补丁工具
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
|   |   |-- memory_store/                      # 长期记忆存储，当前是 JSONL + LocalStore 索引 + home daily mirror
|   |   |-- memory_archive/                    # raw/hook/snapshot、runtime workspace/facts、schema v2、control-plane query、tool-output artifacts 和非破坏性 compact apply/resume/continue packet/subagent-owner refs
|   |   |-- memory_routing/                    # 长期规则索引化路由，负责 MEMORY -> index -> authority file 的确定性匹配
|   |   |-- observability/                     # 未来 request_id、耗时、状态、错误码、metrics、trace 目录
|   |   |   `-- user_space/                    # 用户数据隔离、owner home、provider 空间和 run workspace
|   |   |       |-- __init__.py               # 模块导出
|   |   |       |-- home_layout.py            # ~/.my-agent 路径、初始化和任务目录模板
|   |   |       |-- home_runtime_query.py     # daily memory、task workspace 和 home status 只读查询服务
|   |   |       |-- run_workspace.py          # 普通主代理 run 的 outputs/runtime/agents 任务工作区
|   |   |       |-- provider_space.py         # provider 根、用户/群空间和配额统计
|   |   |       |-- provider_trash.py         # provider scoped trash、审计和 retention 清理
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
|       |-- test_home_runtime_query.py         # home daily memory、task workspace、doctor 和 CLI 读取侧迁移测试
|       |-- test_memory_config.py              # memory 配置安全默认、非法值回退和 warning receipt 测试
|       |-- test_memory_routing.py             # 长期规则 route 加载、匹配、soft/strict 解析和索引诊断测试
|       |-- test_memory_routing_context.py     # runtime rule routing context 安全读取和 prompt 片段测试
|       |-- test_memory_runtime.py             # SimpleAgent.run 接入 routed memory 与 raw archive 的回归测试
|       |-- test_packaging.py                  # console script、workspace_root 等安装与配置行为测试
|       |-- test_subagent_quality_contract.py  # 子代理 QualityContract / ContextManifest 落盘和执行上下文测试
|       |-- test_subagent_capability_request_tool.py # 正式 capability_request 工具、自身 run 约束和默认暴露测试
|       |-- test_agent/test_dispatch_capability_followup.py # dispatch 内部能力申请 route/grant/rerun 闭环测试
|       |-- test_manager_runner_capability_requests.py # runner pending capability 兜底申请和 BLOCKED 状态测试
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
- `AgentConfig.subagent_allowed_tools=[]` 表示子代理工具由角色模板、任务目标和调度器自动判断；非空列表才作为全局受限白名单。`subagent_role_template_dirs=[]` 默认使用工作区 `.agent/subagents/roles` 作为用户外置角色模板目录；角色模板摘要会进入工具规格和 coordinator prompt，帮助模型按角色派工。
- `spawn-subagents --role coordinator --agent-name <name>` 是层级 E2E 的 root seed 入口：外层只创建主节点，主节点再创建子代理，子代理再创建孙代理，孙代理再创建孙孙代理；coordinator seed 默认有调度、看板、读取和 task-local 报告写入工具，但模型额外传入的 shell/web 工具会被过滤。显式 root/coordinator 只保留产物路径上下文，不继承最终产物写入根，最终业务产物仍由 worker/writer/leaf_worker 写入。
- 解析模型输出里的工具调用块。
  标准格式是 `[TOOL_CALL]...JSON...[/TOOL_CALL]`，同时兼容 Qwen/OpenClaw 常见的 XML-ish `<tool_call><function=...><parameter=...>` 方言。
  如果 XML-ish 工具调用只有半截，解析器会返回 `__parse_error__`，让主循环继续可恢复，而不是直接崩掉。

这次额外预留了混合检索框架：
- 当前真正生效的是关键词检索。
- 向量检索接口已经留好，后续接 embedding 时不用重写核心流程。
- `agent_py_agent/agent/tooling/registry_execution.py` 承接工具调用块解析、payload 规范化、授权检查、写边界检查和异常格式化，避免注册表类继续膨胀；解析失败会通过 `parse_error_hint.py` 返回标准 `[TOOL_CALL]` JSON 重试提示，不回显坏工具正文。

当前内置工具包括：
- `list_files`：列目录，适合先摸清项目结构。
- `read_file`：读文本文件，适合查看代码、配置和文档。
- `search_text`：搜索文本，适合先找函数名、配置项和关键字。
- `write_file`：写入或覆盖整个文本文件，适合创建新文件或完整重写。
- `append_file`：向文件末尾追加内容，适合补日志、补文档和补配置片段。
- `replace_in_file`：精确替换文件中的一段已有内容，适合小范围改代码、改配置和改说明。
- `content_transport_policy.py`：写入类工具共用的长内容传输策略；大文件正文不能一次塞进 `write_file.content` / `append_file.content`，需要短骨架、分块追加、patch 小 diff 或受控 exec refs。
- `content_recovery_mode.py`：长内容写入失败后的下一轮恢复策略；parse error 或 inline 上限拒绝后，live prompt 会进入 `long_content_recovery_mode`，要求只发一个小块写入调用。
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

运行期配置不能靠 agent 直接改 YAML。`capability/runtime_config_patch.py` / `runtime_config_reload.py` 提供
结构化补丁、内容版本校验、安全字段 allowlist、审计和 hot reload snapshot；
`agent_core/capability_config_patch_tool.py` 把这套能力暴露给模型。安全字段可
自动写入，`enable_capability_routing` 这类全局行为开关只返回人工确认建议。

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
- `DispatchRecord` / `DispatchReport`：记录父代理一轮调度中的 due-check、路由、runner、patch 审核和验收步骤；acceptance record 还可携带 parent acceptance auto-policy / auto-execution / follow-up 的 refs-only 摘要字段。
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
- `status` 和 startup recovery 使用轻量看板选项，只读热元数据和 refs，不读取 runner
  prompt/response 或 artifact 正文；完整看板的 child status counts 通过已加载 task
  索引计算，避免按 child id 二次加载。
- `due_check()` 会生成父代理巡检报告，把假完成、超时、心跳停滞、待处理能力请求和能力缺口转成 P0/P1/P2 问题。
- `write_due_check()` 会写出 `subagent_due_check.json` 机器事实源和 `SUBAGENT_DUE_CHECK.md` 人类摘要。
- `python3 -m agent_py_agent subagents-due-check` 可以从 CLI 触发巡检。
- `probe_channel()` 可以检查单个子代理的工单现场、机器 JSON 和写入通道是否健康。
- `write_channel_probe_report()` 会写出 `subagent_channel_probe.json` 和 `SUBAGENT_CHANNEL_PROBE.md`。
- `python3 -m agent_py_agent subagents-probe` 可以从 CLI 触发通道健康检查。
- `plan_actions()` 会把 due-check issue 映射成接管、重派、修复工单、能力路由等 dry-run 动作；支持 root_id 作用域，避免跨树问题混入当前动作计划。
- `write_action_plan()` 会写出 `subagent_action_plan.json` 和 `SUBAGENT_ACTION_PLAN.md`。
- `python3 -m agent_py_agent subagents-plan-actions --root-id <root_run_id>` 可以从 CLI 查看指定任务树的 dry-run 动作计划。
- `apply_actions()` 默认 dry-run，只有显式 apply 时才会执行低风险动作。
- `recover_coordinator_leadership` 需要显式 `--apply` 和现有 leader run id，成功后旧 coordinator 进入 `TAKEN_OVER`，其直接子任务会重挂到新 leader，并同步 parent_id/depth/supervisor/final_owner。
- `plan_leadership_recovery()` / `write_leadership_recovery_plan()` 会为批量失联 coordinator 生成只读分摊计划，把直接孩子按候选 leader 容量拆批；当前只写 `subagent_leadership_recovery_plan.json` 和 `SUBAGENT_LEADERSHIP_RECOVERY_PLAN.md`，不自动改树。
- `apply_leadership_recovery()` / `write_leadership_recovery_apply()` 只按显式 child 子集执行重挂；默认 dry-run，`apply=True` 才移动子树，并写 `subagent_leadership_recovery_apply_report.json` / `SUBAGENT_LEADERSHIP_RECOVERY_APPLY.md`。
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
- `SimpleAgent.dispatch_subagents()` 会执行一轮父代理调度：due-check、action apply、capability route、runner、patch review、acceptance；多个 runner 同批执行时会忽略共享 `runner_instruction` 并记录原因，避免子任务专属提示串线。
- `SimpleAgent.watch_subagents()` 会用运行锁持续执行 dispatch，并写 heartbeat / watch log。
- `SimpleAgent.run_parent_planner()` 会在 gate 发现待处理事项时触发完整父代理 LLM turn，并阻断空心 `HEARTBEAT_OK`。
- `write_dispatch_report()` 会写出 `subagent_dispatch_report.json` 和 `SUBAGENT_DISPATCH.md`，apply 时追加调度审计日志；当 acceptance 记录生成 parent auto-policy dry-run 时，报告只展示 policy ref 和摘要，不执行 policy 动作。若本轮显式执行了 parent tests，dispatch 会先刷新 acceptance dry-run 展示和 aggregate acceptance report，避免报告继续显示测试前旧结论。
- `python3 -m agent_py_agent subagents-dispatch` 默认 dry-run；`--watch` 可常驻循环；`--planner` 可触发父代理 LLM planner；`--apply --execute-runners` 才会真实调用 runner 模型。
- `python3 -m agent_py_agent daemon` 会读取主配置里的 `daemon_*` 配置，作为配置驱动的前台常驻入口；`daemon_max_runners: "auto"` 当前映射成保守值 1。
- `python3 -m agent_py_agent gateway start/status/stop/restart/logs` 会管理第一版后台 gateway 进程。
- `python3 -m agent_py_agent gateway ask/result` 会通过 `data/gateway/requests` 和 `data/gateway/responses` 与常驻 gateway 交换消息。
- `build_execution_context()` 会把当前 run 的授权、能力卡、验收要求、质量契约、context manifest、context packs 和写入边界压成最小上下文。
- `write_execution_context()` 会写出 `execution_context.json` 和 `EXECUTION_CONTEXT.md`。
- `python3 -m agent_py_agent subagent-context <run_id>` 可以生成单个子代理执行上下文包。
- `record_runner_result()` 会把 runner 输出写回 `output.json`、`RUNNER_RESULT.md` 和任务日志。
- `record_runner_result(..., actual_tools=[...])` 会把系统真实记录的工具执行落成验收证据，避免模型 evidence 换写法时误判缺少 `read_file/write_file`。
- `parse_subagent_runner_output()` 会解析 `[SUBAGENT_RESULT]...[/SUBAGENT_RESULT]` JSON 块；缺结束标记但 JSON 完整、或成功态尾部截断但前置 `evidence_packets` 已完整且带 refs 时，可恢复最小结果。没有 refs 的截断成功态仍会阻断。
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
- 未来 gateway 自适应策略：`scheduler_mode`、`runner_concurrency`、`runner_start_rate` 和 `runner_failure_policy` 默认走 `auto`；`runner_timeout_seconds` 默认 `off`，表示 runner 不套外层超时，真实 E2E/长任务不会被固定时间墙打断，用户需要上限时可改秒数或 `auto`。
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
    |-- subagent/                     # subagent 四件套、真实 E2E 记录和角色选择策略
    |-- log-analysis/                 # log-analysis 首批四件套索引
    |-- memory/                       # memory 四件套索引
    |-- gateway/                      # gateway 四件套索引
    `-- live-lab/                     # Live Lab 四件套索引
```

模块四件套固定为：`01-discussion.md`、`02-progress.md`、`03-purpose.md`、`04-structure.md`。其中 `02-progress.md` 必须保留“已完成 / 解决的问题 / 下一步 / 已跑测试 / 未跑测试 / 风险”六段，方便并行 worker 和初学者快速判断模块状态。subagent 还额外维护 `06-real-e2e-findings.md` 记录真实压测问题，`08-role-selection-strategy.md` 记录 root/coordinator/lead 如何选择角色模板。

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

- `agent_py_agent/agent/agent_core/runner_dispatch.py`: runner 候选选择、重试和角色阶段排序；coordinator/worker/tester/bug_finder/acceptor 会按“先拆/先做/再测/再验收”的阶段顺序进入 `max_runners`，并且同一轮只放行当前最低阶段，避免 QA/test/review/acceptance 抢在 producer/coordinator 前面运行；阶段判断优先信任 `role` / `agent_name`，只有身份不明确时才读 goal，避免继承的父级 QA 合同污染 coordinator；dispatch record 会保留 runner 创建的 child 状态摘要、未完成 child ids 和 partial-success 标记。
- `agent_py_agent/agent/agent_core/runner_gate.py`: 集中计算 runner timeout；`off/none/disabled/0` 表示不限制，`auto` 表示按动态 timeout 配置计算，固定数字表示秒数。
  - 支持从任务 attributes 读取动态超时。

- `agent_py_agent/agent/agent_core/dispatch_no_progress.py`: 父级 dispatch no-progress 判断 helper；连续重复的 due-check / record-only action 且无状态、验收或 child 创建变化时，通知 `dispatch_loop` 自然停止，避免无人值守时无限记账。
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
- `agent_py_agent/agent/local_storage/control_plane.py`: 给 LocalStore 增加控制面 API，支持 upsert run、记录事件、重建 rollup、查询 root task 树、查询子树、blocked runs、takeover candidates 和带 requester/scope 的 runtime query；`takeover_candidates` 覆盖 BLOCKED / FAILED / ERROR / TIMEOUT，避免超时孙代理漏出接管视图。
- `agent_py_agent/agent/local_storage/control_plane_panel.py`: 给 LocalStore 增加共享进度面板查询，把 runtime query、rollup、blocked runs 和 inheritance refs 组合成上级/接管代理可读状态包。
- `agent_py_agent/agent/local_storage/control_plane_codec.py`: 集中维护控制面 SQLite SQL、参数组装和行转换，避免公开 mixin 因 SQL 细节膨胀。
- `agent_py_agent/cli/shared_progress.py`: 给 `status` 和 `subagents` CLI 生成共享进度摘要，展示 blocked、failure handoff refs 和 takeover packet refs 数量，不读取正文。
- `agent_py_agent/agent/agent_core/tool_output_failsafe.py`: 大工具输出写 artifact 前写 fail-safe recovery snapshot，只记录工具名、hash、大小和恢复建议。
- `agent_py_agent/agent/agent_core/tool_context_reducer.py`: 大工具输出进入下一轮 live prompt 前只注入 artifact 摘要和 checkpoint refs，小输出仍保留原工具结果；调度类输出会交给 orchestration summary 只保留 next_action/run refs。
- `agent_py_agent/agent/agent_core/tool_context_orchestration_summary.py`: externalized dispatch/schedule/read_artifact 调度输出的 live-prompt 摘要层，保留状态、建议工具调用和 refs，不默认诱导父级读 artifact 正文。
- `agent_py_agent/agent/agent_core/dispatch_acceptance_records.py`: 承接 dispatch acceptance record 构建、parent acceptance auto-policy/auto-execution 摘要和显式 tests 后的 refresh 调用，让主 dispatch service 保持薄编排。
- `agent_py_agent/agent/agent_core/dispatch_acceptance_refresh.py`: 本轮显式 parent tests 写入 `test_execution.json` 后重新 dry-run acceptance，并刷新 dispatch 展示、单 run 审计和 aggregate acceptance report；不 apply、不 rescue、不修改 task 状态。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_payload.py`: 承接 runner-context `dispatch_subagents` 工具返回 payload 的单条 record 构造，输出 test/follow-up refs 和摘要，不展开正文；错 run_id 时顶层 recovery 会给 `valid_run_ids`。
- `agent_py_agent/agent/agent_core/runner_stage_trace.py`: 把子代理 runner 的模型请求/响应/失败和工具调用开始/结束写入 debug trace；level 3 只记录长度、backend、工具名、payload keys 和 ok，level 4/5 才追加短预览或完整 detail 文件 ref。
- `agent_py_agent/agent/subagents/services/control_plane_projection.py`: 在 subagent 保存时把 task 当前状态投影到 LocalStore 控制面；它只做查询索引，不替代旧工单目录或 runtime workspace 事实源。
- `agent_py_agent/agent/subagents/debug_trace.py`: 子代理正式调试追踪开关的写入层；`subagent_debug_trace_level=0` 时完全静默，level 1-3 只把 bounded refs-only 事件写入内部 `debug_traces/subagent_trace.jsonl`，level 4 加短预览，level 5 把完整 prompt/response/tool payload/tool output 写入内部 `debug_traces/details/` 并在 JSONL 里留 ref。
- `agent_py_agent/agent/subagents/context_bundle.py`: 生成 runner-facing `context_bundle.json` / `CONTEXT_BUNDLE.md`，包含目标、计划、验收、权限、写入边界、输出合同、lineage 和 Context Gate；多层传递只保存当前/父级 bundle refs，不展开父级正文。
- `agent_py_agent/agent/subagents/result_structured.py`: 解析 runner structured output 并写回 artifacts、tests、evidence packets、findings 和 capability requests；artifact 证据合成委托给小模块，保持解析主流程薄。
- `agent_py_agent/agent/subagents/result_artifact_evidence.py`: 从 runner artifact metadata 合并 `artifact_refs`，并在模型漏写 `evidence_packets` 时合成 refs-only artifact evidence packet，不读取 artifact 正文。
- `agent_py_agent/agent/subagents/parsing_partial.py`: 从 runner 结果块恢复被截断但仍有可追溯 `evidence_packets` 的成功结果；只接受 refs-only 证据链，避免把无证据长文本误当完成。
- `agent_py_agent/agent/subagents/parsing_values.py`: 子代理结果解析共用的 list/dict/int 归一化 helper，让 `parsing.py` 保持薄层并保留旧 private import 兼容。
- `agent_py_agent/agent/subagents/capability_status.py`: 统一判断 runner 结构化状态是否仍在等待 tool/skill/shell/MCP/capability，供 parser、policy 和 acceptance 共用。
- `agent_py_agent/agent/subagents/parsing_capability_requests.py`: 当模型写出 pending capability 状态但漏填 `capability_requests` 时，从结构化 pending steps 恢复父级可路由申请；推不出具体工具或命令时不生成空泛 generic request，避免产生无用 grant。
- `agent_py_agent/agent/subagents/services/persistence.py`: 负责 task/run/status report 落盘和旧任务兼容归一化；保存时会合并磁盘已有 `child_ids`，避免旧父/子快照覆盖新派生的层级链接。
- `agent_py_agent/agent/subagents/services/persistence_rendering.py`: 承接 persistence 写 `thought.md` 的 Markdown 内容组装，让持久化主流程继续保持薄编排。
- `agent_py_agent/agent/subagents/services/inheritance_manifest.py`: 创建 child task 时生成继承清单，记录 inherited / overridden / dropped 项；它是 audit-only，不自动扩大子代理上下文。
- `agent_py_agent/agent/subagents/services/persistence_inheritance.py`: 负责继承清单的读取归一化和 `reports/inheritance_manifest.json` 写入，避免 persistence 主流程继续膨胀。
- `agent_py_agent/agent/subagents/services/failure_handoff.py`: 生成失败交接记录，保存 warning、risk level、checkpoint refs、artifact/evidence refs、避坑建议和推荐下一步。
- `agent_py_agent/agent/subagents/services/persistence_failure_handoff.py`: 负责失败交接记录的读取归一化和 `reports/failure_handoff.json` 写入，避免 persistence 主流程继续膨胀。
- `agent_py_agent/agent/subagents/services/persistence_recovery_outputs.py`: 集中写入 checkpoint artifacts 和 takeover readiness 文件，让 persistence 主流程保持薄编排。
- `agent_py_agent/agent/subagents/services/takeover_readiness.py`: 生成接管前必读包 `reports/takeover_readiness.json` 和 `TAKEOVER_READINESS.md`，只保存 context bundle refs、checkpoint refs、artifact manifest 元数据和读取顺序，不读取大正文。
- `agent_py_agent/agent/subagents/rendering_rescue.py`: 渲染 rescue packet 的 refs-only 摘要，避免主 `rendering.py` 因接管/救援展示继续膨胀。
- `agent_py_agent/agent/memory_archive/compact_resume_failsafe.py`: 从 compact restore refs 指向的 hook JSONL 中提取工具输出外置前 fail-safe checkpoint，保持 memory-resume refs-only。
- `agent_py_agent/agent/memory_archive/compact_continue_packet.py`: 把 compact resume 后的 work_state、action guard、推荐读取路径和 subagent owner refs 固定成继续工作包；它只表达恢复上下文可继续，不执行工具或业务验收。
- `agent_py_agent/agent/memory_archive/compact_resume_blocked.py`: 生成 compact metadata 缺失时的 schema-compatible 阻断 payload，让主 resume 编排保持薄。
- `agent_py_agent/agent/memory_archive/artifact_reader.py`: 按 tool output index 显式读取外置 artifact 正文切片，并校验路径边界和 sha256；路径前缀抄错但 artifact 文件名唯一时，可修复到登记记录；支持 `slice/head/tail/search` 窄读和 index-only size 预判。
- `agent_py_agent/agent/memory_archive/artifact_read_modes.py`: `read_artifact` 正文窄读模式实现，负责 slice/head/tail/search 的内容 shaping，让 `artifact_reader.py` 只管 index、边界和 hash。
- `agent_py_agent/agent/path_recovery_hints.py`: 共享 URL span 和工作区路径 typo 恢复提示，供派工预检、读文件和列目录等入口复用。
- `agent_py_agent/agent/tooling/_filesystem_read.py`: `read_file` 读取工作区文本文件；读/列/search 同时接受顶层参数和 `filesystem.*` bundle 参数；遇到疑似工作区路径拼写错误时返回 `suggested_target`；误读 tool-output artifact 包装的判断拆到 `filesystem_artifact_guard.py`。
- `agent_py_agent/agent/tooling/filesystem_artifact_guard.py`: 拒绝 `memory_archive/artifacts/tool_outputs/*.json` 外置工具输出包装经由 `read_file` 读取，提示改用 `read_artifact` 分片；当当前上下文未授权 `read_artifact` 时提示上报 `capability_request`。
- `agent_py_agent/agent/tooling/artifact.py`: 注册 `read_artifact` 工具，给模型提供受控 artifact slice/head/tail/search 读取入口。
- `agent_py_agent/agent/tooling/artifact_read_budget.py`: `read_artifact` 的单 run 正文读取预算器，按 `run_id` 统计滚动窗口字符数，避免子代理反复展开大 artifact。
- `agent_py_agent/agent/tooling/controlled_exec.py`: 注册 `controlled_exec` 工具包装；只从 `write_boundary.controlled_exec_grants` 读取父级 shell grant，dry-run 返回 plan，显式 apply 才调用 bounded shell execution 或 task trash；`apply/execute/run/full` 字符串也会被识别为执行意图，delete-to-trash dry-run 作为有效计划返回，但 prompt/验收会要求真实 stdout/audit/trash refs 才算完成。执行后的 shell decision/audit 会标记 `dry_run=false`，避免模型把真实执行误读成计划。
- `agent_py_agent/agent/tooling/registry_params.py`: 工具执行参数准备 helper，给 `read_file` 注入只用于恢复提示的 allowed_tools 上下文，让主 registry execution 继续保持薄。
- `agent_py_agent/agent/tooling/registry_tool_dispatch.py`: 工具已解析、授权和写边界校验后的最终分发层；普通工具走 `tool.execute()`，registry-aware 工具如 `controlled_exec` 在这里读取注入上下文。
- `agent_py_agent/cli/memory_artifact_commands.py`: 提供 `memory-artifact-read` 命令，保持 artifact 正文读取和 archive resume/search CLI 分离。
- `agent_py_agent/cli/memory_resume_compact_rendering.py`: 输出 `memory-resume --from-compact` 的 handoff、continue packet、completion prompt 和推荐路径。
- `agent_py_agent/agent/subagents/services/persistence_security.py`: 负责 `SecuritySignal` 预留字段的读取归一化，避免 persistence 主流程继续膨胀；当前不执行安全策略。
- `agent_py_agent/agent/subagents/services/persistence_identity.py`: 负责 `RuntimeIdentity` 预留字段的读取归一化，保证员工/会话/配置 scope 只作为审计元数据进入 task 记录。
- `agent_py_agent/agent/subagents/model_task.py`: 新增 `SecuritySignal` 和 `security_review_required` 安全预留字段，用于记录安全劫持、安全欺骗、prompt injection、工具权限异常等可疑信号；当前只审计不拦截。
- `agent_py_agent/agent/subagents/execution_records.py`: 新增 `TestExecutionRecord`，定义真实验收执行证据、输出截断和通过结果派生。
- `agent_py_agent/agent/subagents/execution_executor.py`: 新增最小 `TestExecutor`，执行 command/file/content/static_site 四类检查并产出 `TestExecutionRecord`；当前不接 acceptance 自动写回。
- `agent_py_agent/agent/subagents/static_site_validator.py`: 父级验收的静态站点检查器，扫描 workspace 内 HTML 必需文件、本地 href/src/action、`${...}` 占位符和明显无动作控件，不执行 JS、不访问网络。
- `agent_py_agent/agent/subagents/execution_test_items.py`: 新增测试项预处理 helper，根据 runner artifacts 安全推断 command 测试工作目录，避免父验收在 workspace 根目录误跑相对测试命令；也会把 workspace 内安全的 `cd <dir> && pytest` 拆成 `working_dir + 纯命令`，不放开 shell；当 artifacts 显示静态 HTML 且缺少同类测试时，会追加 `static_site_check`，并能把调用方传入的 task-level required files 合进 required_files。
- `agent_py_agent/agent/subagents/static_required_files.py`: 从 task goal/thought/description/acceptance_checks 提取 `index.html`、`style.css`、`app.js` 等静态 Web 必需文件名，不读取产物正文。
- `agent_py_agent/agent/subagents/required_file_terms.py`: 层级 handoff、context bundle 和静态站验收共用的文件契约提取器，把正向交付文件放进 `required_files`，把 `禁止改名/禁止文件名/禁止文件名（...）/禁止内部文件（...）/禁止创建文件（forbidden_files）：/不要创建/不写output.json` 等反例放进 `forbidden_files`，并处理 `product.html/old-product.html` 这类斜杠分隔反例列表；文件名边界按 ASCII 处理，中文紧贴文件名或 `RUNNER_RESULT.md等` 也能识别；负向标题后的 bullet 或纯文件列表都会继承负向语境；`禁止 style.css/app.js 放进子目录` 这类位置约束不会把必需资源误标成 forbidden。
- `agent_py_agent/agent/subagents/parent_acceptance_preflight.py`: 父级验收预检 helper，负责准备测试项和命令安全预检，保持 controller 决策文件更薄。
- `agent_py_agent/agent/subagents/execution_static_site_items.py`: 根据 output artifacts 推断静态站点机器验收项，只读 HTML 路径引用，生成 site_root 和 required_files，并合并父级传入的 task-level required files，不读取页面正文。
- `agent_py_agent/agent/subagents/execution_content_checks.py`: 从测试项预处理拆出的内容验收归一化 helper，把模型常写的 `cat <workspace文件>` 转成受控 `content_check`，只接受明确期望内容，不放开 `cat` 命令。
- `agent_py_agent/agent/subagents/execution_executor_helpers.py`: 新增 `TestExecutor` 命令解析、记录构造和时间戳 helper，保持执行器主文件只负责 bounded execution。
- `agent_py_agent/agent/subagents/execution_report.py`: 新增 `test_execution.json` / `test_execution.md` 报告写读入口；JSON 是机器事实源，Markdown 只做展示。
- `agent_py_agent/agent/subagents/services/acceptance_machine_evidence.py`: 新增父级真实测试报告读取 helper；通过的 `test_execution.json` 可在无 worker evidence packet 时作为机器证据链。
- `agent_py_agent/agent/subagents/parent_acceptance_controller.py`: 新增父级验收 dry-run 决策器和 refs-only 决策落盘 helper，读取 `output.json`、`test_execution.json` 和 handoff refs，返回 execute_tests / review_patches / inspect_only / request_human / rescue；显式写入生成 `parent_acceptance_decision.json`，不读取 artifact 正文；预检前复用 `prepare_test_items()` 归一化安全 cwd 包装，空测试报告配合 traceable artifact/evidence refs 会走 inspect_only，不误判为 rescue。
- `agent_py_agent/agent/subagents/parent_acceptance_empty_report.py`: 拆出可执行 test 筛选和空 `test_execution.json` 的 inspect-only 判定，只检查 evidence/artifact refs 元数据，不展开正文。
- `agent_py_agent/agent/subagents/parent_acceptance_apply.py`: 新增显式 apply 结果模型、拦截/应用结果构造和 `parent_acceptance_apply.json` 落盘 helper；非 inspect_only 决策只留下拦截审计，不改任务状态。
- `agent_py_agent/agent/subagents/parent_acceptance_next_action.py`: 新增父级下一动作建议模型，把当前决策/apply 审计映射成 run_tests / review_patches / request_human_confirmation / plan_rescue / apply_acceptance；只返回 refs 和建议命令，不执行。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_policy.py`: 新增父级自动策略 dry-run 模型和 `parent_acceptance_auto_policy.json` 审计落盘；第一版只判断 allow/blocked、would_execute、manual-only 半自动计划和 preflight 检查，不执行命令、不改状态。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_execution.py`: 新增父级自动执行 dry-run facade 的 Request/Result bundle 和 `parent_acceptance_auto_execution.json` 审计落盘；第一版固定 hard guard，只有显式确认路径执行 tests，执行前同样归一化安全 cwd 包装，仍不 apply、不 rescue、不改状态。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_execution_reports.py`: 拆出显式测试执行后的 report/follow-up 写入 helper，让 executor facade 保持薄层和 bundle 入口。
- `agent_py_agent/agent/subagents/parent_acceptance_auto_followup.py`: 新增显式验收测试后的 follow-up 审计包，写 `parent_acceptance_auto_followup.json`，归类人工 apply / patch review / 人工 rescue / 人工确认 / 继续测试；只保存 refs 和失败摘要，不改 task 状态。
- `agent_py_agent/agent/subagents/parent_acceptance_followup_control.py`: 新增 follow-up 受控入口模型和审计文件，支持预览、坏 JSON 阻断、显式 apply、patch review 命令提示和复用 action handler 的接管 rescue。
- `agent_py_agent/agent/subagents/parent_acceptance_followup_consistency.py`: 新增 follow-up apply 前的一致性检查 helper，集中处理 run_id、test report ref、失败数、新鲜度和状态驱动 rescue 例外。
- `agent_py_agent/agent/subagents/parent_acceptance_rescue_followup.py`: 新增已失败/阻塞任务的 rescue follow-up helper，让无 `test_execution.json` 的 runner 失败也能进入受控接管路径。
- `agent_py_agent/agent/subagents/services/hierarchy_scheduler.py`: 新增层级调度器 v1，使用 `HierarchyScheduleRequest` / `HierarchyChildSpec` 显式预览或创建 child/grandchild run，并统一限制深度和 fan-out；当真实模型把 `role=child`、具体角色写进 `agent_name` 时，会先恢复 researcher/tester/acceptor/bug_finder/writer/worker 等角色再套权限策略；模型漏传 `acceptance_checks` 时会通过 `hierarchy_acceptance.py` 从 child goal/角色派生最小验收项；父任务明确点名 tester/bug_finder/acceptor 时，会返回 `quality_advice` 提示缺失角色和红线，但不替 LLM 固定创建 QA child。
- `agent_py_agent/agent/subagents/services/hierarchy_agent_names.py`: 层级展示名统一生成器，负责 `小傻妞-*` / `小小傻妞-*` / `小小小傻妞-*` 前缀；真实模型只写 `小小傻妞` 这种无后缀名字或把 `*` 占位符原样传入时，会回退到 role 后缀，避免裸异常和模板占位符泄漏。
- `agent_py_agent/agent/subagents/services/base.py`: 子代理基础创建服务；`_extract_write_dirs()` 从目标文本中提取本地写入根，并跳过 URL 范围，避免图片/API 地址被误当成可写目录。
- `agent_py_agent/agent/subagents/services/hierarchy_acceptance.py`: 从 scheduler 拆出的验收兜底策略，只在模型没有显式 `acceptance_checks` 时派生最小验收项。
- `agent_py_agent/agent/subagents/services/hierarchy_tool_policy.py`: 从 scheduler 拆出的工具策略，统一处理 coordinator/leaf 的工具继承、写文件工具补齐和 `write`/`read` 等模型工具名别名修正；coordinator 显式 allowed_tools 会补回内置编排工具，避免模型漏传后失去派工能力。
- `agent_py_agent/agent/subagents/execution_test_items.py`: 预处理父级验收 tests，推断 workspace 内 `working_dir`，拆安全 `cd <dir> && pytest`，并把带明确期望内容的 `cat <workspace文件>` 改成受控 `content_check`，避免为真实模型输出放开 `cat` 命令；多页 HTML artifacts 会自动补 `static_site_check`。
- `agent_py_agent/agent/subagents/execution_executor.py`: `content_check` 支持 `content_pattern` 包含匹配，也支持 `content_equals` / `expected_content` + `match_mode=exact`，用于严格验证文件内容没有额外字符；`static_site_check` 用于机器验收购物站这类静态产物的页面存在性、坏链接、占位符和明显失效控件。
- `agent_py_agent/agent/agent_core/_tool_loop_service.py`: 主代理和 subagent 共用的工具循环；到达 `max_tool_rounds` 后给模型一次收口机会，如果模型仍吐工具调用，返回确定性停止说明而不是把新 `[TOOL_CALL]` 当最终回答；执行真实工具前会检查 per-run 工具预算和委托期读正文守卫，预算触发时只拦截当前 run 的工具并给模型自检/上报提示。
- `agent_py_agent/agent/agent_core/orchestration_body_read_guard.py`: 委托期 refs-only 工具守卫；父级已有 child 且 acceptor 未完成时阻断 product `read_file` 和大正文 `read_artifact`，运行元数据和调度类小 artifact 可读，用户显式要求父级亲自验收时临时放行。
- `agent_py_agent/agent/agent_core/orchestration_tools.py`: 顶层 `create_subagents` / `subagent_board` 工具入口；显式 root/coordinator seed 会从当前原始用户 prompt 补回模型摘要漏掉的 required/forbidden 文件合同和 4层/depth 命名约束，并以增强后的 `CreateRunParams.goal` 创建 root。
- `agent_py_agent/agent/agent_core/orchestration_root_contract.py`: root/coordinator seed 合同修复 helper；从原始用户 prompt 提取 required/forbidden 文件和精确层级命名合同，避免自然语言摘要把机器合同改写或漏传。
- `agent_py_agent/agent/agent_core/subagent_finalize_helpers.py`: 子代理 runner 收尾持久化 helper；coordinator 已真实创建并验收 child 时可合成等待父级验收的收口，同时会阻断“没工具调用、没 child refs，只说下一步要 schedule_child_subagents”的假完成，转成 `BLOCKED / needs_child_creation` 让 LLM 继续派工。
- `agent_py_agent/agent/backends/base.py`: Anthropic-compatible 非流式响应解析会在 thinking-only/no-text 内容块时重试一次，避免真实 E2E 被可恢复的厂商响应形状直接打成 runner 失败；普通空响应仍报错。
- `agent_py_agent/agent/agent_core/tool_agent_budget.py` / `tool_agent_budget_stage.py`: 单个代理滚动工具预算 helper 和工具循环集成层；默认按 `run_id` 做 10 分钟 50 次限制，没有 `run_id` 的主代理普通聊天不受限，且不做任务树或单次对话的全局预算。
- `agent_py_agent/agent/agent_core/orchestration_progress_payload.py`: runner-context `dispatch_subagents` 的直接 child 进度摘要；提示继续调度、恢复阻塞 child 或最新 acceptance review 为 REJECT 的 child，只有 child 都已等待验收/完成且没有 rejected acceptance 时才用 refs-first summary 收口，避免上层反复读取子产物正文。
- `agent_py_agent/agent/agent_core/tool_round_execution.py`: 单轮工具执行 helper；负责记录 assistant tool round、执行/记录工具调用、检测子代理 `output.json` 收口，并把同轮 `schedule_child_subagents` 后依赖真实 run id 的 `dispatch_subagents` 等编排调用延后到下一轮，避免模型使用脑补 run id；`output.json` 收口检测支持 flat `path` 和 bundle `filesystem.path`。
- `agent_py_agent/agent/subagents/services/hierarchy_role_identity.py`: 从 scheduler 拆出的角色 identity 兜底策略，根据 `agent_name` / `goal` 恢复模型漏填的 researcher/tester/acceptor/bug_finder/writer/worker 等角色。
- `agent_py_agent/agent/subagents/services/hierarchy_scheduled_role.py`: 从 scheduler 拆出的下一层 role 推断策略，把 child/general/worker 这类模型模糊角色修正成 coordinator 或 leaf_worker。
- `agent_py_agent/agent/subagents/services/qa_role_contract.py`: tester / bug_finder / acceptor 角色合同识别 helper；调度器和验收发现共用它来判断父任务是否要求真实 QA 后代、某个 persisted child 是否真正覆盖 QA 角色。
- `agent_py_agent/agent/subagents/services/hierarchy_qa_scheduler.py`: 调度阶段 QA advice helper；根据父任务合同、本轮 specs、已存在后代和实现进度生成 `quality_advice`，提示缺失 tester/bug_finder/acceptor、候选 child spec 和红线；不替 LLM 固定创建 QA，避免核心 scheduler 继续变胖。
- `agent_py_agent/agent/subagents/services/hierarchy_context.py`: 层级派工的父级上下文继承层；保留产物路径、文件合同、层级合同，以及 `controlled_exec` / capability request / path_scope / output_budget / task_trash / refs 等不能丢的能力安全合同。
- `agent_py_agent/agent/subagents/services/hierarchy_scope_guards.py`: 从 scheduler 中拆出的层级 scope guard，集中处理空计划、深度/数量限制、禁止 sibling 领域、同批混建 coordinator/leaf、QA 早于实现完成、child 写入根漂移和 domain mismatch；重复 coordinator 领域和已验证 leaf 具体目标文件现在只进入 `scheduling_warnings`，不再硬阻断 QA/修复/协作写入；forbidden scope 会过滤 depth/layer/level/child/worker 这类层级或角色词，避免“不要创建 depth>=4”误挡正常孙代理；去重会过滤 generated id 片段、`run/id/ref/refs/qa` 等结构词和泛化编号词，goal 兜底会先剥离绝对路径、文件名、继承块以及 `users/claude/code/shop/tests/css/js` 等路径脚手架词，避免误挡 recovery checker siblings、自动 QA children 或共享 `/Users/.../my-claude-code/...` 的不同 child。
- `agent_py_agent/agent/subagents/services/hierarchy_leaf_targets.py`: 从 scope guard 拆出的已验证 leaf 目标文件去重 helper；只读 direct child 元数据和 `output.json.artifacts` 路径引用，不读取 artifact 正文。
- `agent_py_agent/agent/subagents/services/hierarchy_context.py`: 层级目标继承 helper；把父级 required/forbidden 文件合同、4层/depth/命名合同和能力安全合同补进下级 goal，防止模型总结时把关键边界缩水。
- `agent_py_agent/agent/subagents/services/hierarchy_write_policy.py`: 层级写入根策略，区分 task-local 报告写入和最终产品写入；coordinator/researcher/tester/bug_finder/acceptor 可保留产品路径上下文但不继承产品写入根。
- `agent_py_agent/agent/subagents/services/hierarchy_recovery.py`: 多层恢复包服务，从 root run 只读扫描子树，返回需要恢复的后代、当前/父级 context bundle refs、接管入口和 checkpoint refs；现在也可按 capability 阈值标记 stale `RUNNING` 后代，不读取 artifact 正文。
- `agent_py_agent/agent/subagents/services/board.py`: due-check 和 plan-actions 支持 root_id 作用域，真实 E2E 多棵任务树共用 workspace 时可以只看当前 root 并只生成当前树动作。
- `agent_py_agent/agent/subagents/services/board_due_models.py`: due-check 共享参数包和 issue 构造 helper，避免巡检谓词文件继续膨胀。
- `agent_py_agent/agent/subagents/services/board_due_checks.py`: due-check heartbeat/run-timeout 规则把已派生 child runs 的 parked `PLANNING` coordinator 转成 `coordinator_heartbeat_stale` 领导权恢复问题，避免误当普通 runner 接管，同时不会静默漏掉失联 coordinator。
- `agent_py_agent/agent/subagents/services/leadership_recovery.py`: 批量 coordinator 领导权恢复 dry-run 计划器；只读取 root 作用域 due-check 问题和显式候选 leader，按容量输出 assignments/unassigned，不修改任务树。
- `agent_py_agent/agent/subagents/services/leadership_recovery_apply.py`: 分批领导权恢复 apply 服务；校验 root、leader、直接 child、容量，成功后只移动指定 child 子树并递归刷新后代 depth。
- `agent_py_agent/agent/subagents/controlled_exec_gateway.py`: 受控 exec 授权桥；把父级 `CapabilityGrant` 编译成 shell gateway plan，兼容 `shell` grant 和显式包含 `controlled_exec` 的 `tool` grant，阻止子代理自填 allowlist 自授权，并把删除类命令导向 task trash；grant refs 暴露 `delete_policy.mode=task_trash` 和完成条件，帮助模型理解 rm 不需要进入 shell allowlist；相对删除目标按命令 `cwd` 解析。
- `agent_py_agent/agent/subagents/capability_request_identity.py`: 能力申请去重 helper；按 capability/tool/skill/MCP/命令/path/network/output budget 生成稳定签名，避免同一 runner 通过工具调用和结构化结果重复写 capability request。
- `agent_py_agent/agent/subagents/task_trash.py`: task-local trash 管理器；删除替代会把父级授权 `path_scope` 内的源文件/目录移动到当前 run 的 `trash/`，默认无授权根时只允许 task_dir 内部来源。
- `agent_py_agent/agent/subagents/services/hierarchy_scope_guards.py`: 层级调度守卫；识别四层/孙孙/depth=3 合同，阻止 depth<2 提前创建 leaf，并在写根漂移检查中区分用户产物根和内部 task/run workspace。
- `agent_py_agent/agent/subagents/services/persistence.py`: 普通保存默认合并已有 child_ids 防止旧快照覆盖层级边，`save_hierarchy_links()` 场景会关闭合并以支持受控子树重挂。
- `agent_py_agent/agent/subagents/manager_hierarchy.py`: 新增 `SubAgentManager.schedule_child_runs(params=...)` facade，保持层级创建只走 bundle 入口和既有 `create_run` 持久化路径。
- `agent_py_agent/agent/subagents/role_template_resolution.py`: 从当前模板目录把 `child_coordinator`、`qa_tester`、用户自定义前后缀 role 等自然名字解析到模板 id，避免自由 role 落成空工具代理。
- `agent_py_agent/agent/subagents/role_templates.py`: 加载内置和用户 JSON role templates，要求广义角色、中文说明和多目标适用，坏模板记录 issue；提供轻量 `role_template_index_text()` 给主代理和派工类角色常驻使用，索引包含适用/不适用场景、能力标签和模板位置但不展开默认工具；按需 `role_template_detail_text()` 给派工 coordinator 展开完整角色提示。
- `agent_py_agent/agent/subagents/role_template_catalog/builtin/*.json`: 内置 `coordinator/worker/bug_finder/tester/acceptor/researcher/writer` 角色模板。
- `agent_py_agent/agent/agent_core/tool_call_context_reducer.py`: 大段 assistant tool-call 参数摘要层，避免 `write_file(content=<large html>)` 原文反复进入下一轮 prompt；保留工具名、路径、字段大小、hash 和短预览。小 dict payload 也渲染为摘要行，避免模型把历史 dict 复制成新工具调用。
- `agent_py_agent/agent/tooling/content_transport_policy.py`: 长内容工具参数硬门；`write_file` / `append_file` 在写入前统一检查 inline content 长度，过长时返回分块、patch 或 grant-backed `controlled_exec` 的恢复提示，不写磁盘。
- `agent_py_agent/agent/tooling/content_recovery_mode.py`: 长内容工具失败后的自动降级策略；工具循环检测到截断 parse error 或 inline 上限拒绝后，只把短恢复模式放回下一轮 prompt，不回灌正文。
- `agent_py_agent/agent/backends/errors.py`: 模型后端错误类型；`ProviderTimeoutError` 让 CLI、runner 和恢复逻辑能把 provider 超时从普通崩溃里分出来。
- `agent_py_agent/agent/agent_core/coordinator_seed_tools.py`: 显式 root/coordinator seed 的工具过滤策略；模型额外传入 shell/web 工具时收敛回内置 coordinator 工具包。
- `agent_py_agent/agent/agent_core/spawn_role_seed.py`: CLI 显式 role seed 入口；root/coordinator seed 保留 goal 里的产品路径给下层派工，但自身 allowed write roots 只保留 task-local 协调目录。
- `agent_py_agent/agent/agent_core/orchestration_progress_payload.py`: runner-context dispatch 的直接 child 进度摘要；含状态计数、unfinished ids、recovery ids、rejected acceptance ids、`needs_more_dispatch` / `needs_recovery` 和带 `run_ids` 的建议继续调度或恢复工具调用。
- `agent_py_agent/agent/agent_core/orchestration_board_payload.py`: subagent board 输出整形 helper；把可继续处理的 run id 按状态放到顶层，归一 `status=ALL/*/ANY` 为不过滤，并截断长 goal，避免看板响应挤占模型上下文。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_tool.py`: `dispatch_subagents` 模型工具类；把模型参数收敛成 `DispatchParams`，返回 refs-first 调度报告和错误 run id 恢复提示。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_scope.py`: 集中维护 dispatch_subagents 的 apply/execute 默认、parent scope、self-exclude、workflow-off 和验收收口策略；顶层 active root/coordinator 在 `apply=true` 时强制 workflow off，避免全局 auto workflow 先生成 producer/critic/repair 子工单并绕过 root 自己派工。
- `agent_py_agent/agent/agent_core/capability_request_tool.py`: `capability_request` 模型工具类；runner 缺工具、skill、MCP、网络或 shell 时写正式 OPEN `CapabilityRequest`，只允许当前 run 自己申请，父级后续 route/grant/rerun。
- `agent_py_agent/agent/agent_core/capability_config_patch_tool.py`: `capability_config_patch` 模型工具类；把配置修复请求收敛成 `CapabilityConfigPatchRequest`，只自动应用安全字段，危险字段返回建议，并写审计/通知。
- `agent_py_agent/agent/agent_core/orchestration_workflow_mode.py`: create/dispatch 共用 workflow mode 归一化 helper，维持 `off` / `plan` / `auto` 兼容语义。
- `agent_py_agent/agent/agent_core/orchestration_dispatch_payload.py`: dispatch 工具返回 payload 压缩层；单条 record 只保留 refs 和关键字段，错 run_id 时把 `runner_selection_recovery.valid_run_ids` 放到顶层。
- `agent_py_agent/agent/agent_core/dispatch_runner_selection.py`: runner 候选范围和显式 `include_run_ids` 预检；错 id 会返回 `runner_selection/invalid_run_ids`、可用 direct child ids 和保守纠正提示。
- `agent_py_agent/agent/agent_core/dispatch_runner_batches.py`: runner 候选收集和执行批处理；调用 selection helper 精确推进父节点给定的直接孩子。
- `agent_py_agent/agent/agent_core/services/watch_config_reload.py`: dispatch watch 的 capability_config 热加载层；每轮开始前检测配置 hash，变化只影响后续 dispatch，不改已经运行中的 runner。
- `agent_py_agent/agent/tooling/json_repair.py`: 工具调用 JSON 的窄口修复 helper；目前只修有效对象后多余右花括号，避免模型因 parse error 把完整任务 goal 越改越短。
- `agent_py_agent/agent/subagents/role_contracts.py`: 新增 reporter/checker 角色契约和 analyst/reviewer 兼容映射；同时把模板角色接入默认工具、输出契约和 parent final gate。
- `agent_py_agent/agent/subagents/automation_gate.py`: 新增半自动/自动执行门，默认只放行 refs-only 查询动作，跑工具或改状态动作继续需要人工确认。
- `agent_py_agent/agent/agent_core/dispatch_limiter.py`: 新增 runner 启动限流 bundle/helper，统一处理 start-rate 和可选 role budgets，供 dispatch runner batch 调用。
- `agent_py_agent/cli/_hierarchy.py`: 新增 `subagents-hierarchy` 和 `subagents-recovery-tree` CLI；前者默认 dry-run、`--apply` 才创建下一层子代理，后者查询 refs-only 多层恢复包并通过 capability config 判断 stale `RUNNING` 后代。
- `agent_py_agent/cli/_leadership.py`: 新增 `subagents-leadership-recovery-plan` CLI，写批量领导权恢复 dry-run JSON/Markdown 报告；不会执行 future apply 命令。
- `agent_py_agent/agent/agent_core/dispatch_service.py` / `dispatch_mixin.py`: dispatch/watch 遇到需要领导权恢复的父节点时写 `leadership_recovery_plan` refs-only 记录和报告引用，但不调用重挂 apply。
- `agent_py_agent/agent/subagents/manager_parent_acceptance.py`: 新增 manager 父级验收桥接函数，把 plan/write/apply/next-action/auto-policy/follow-up 流程从 `manager_acceptance.py` 类体拆出，保持 manager facade 轻量，并在 follow-up apply 前校验测试报告新鲜度；当前任务已失败/阻塞时可生成 rescue follow-up，不会误导去跑 tests。
- `agent_py_agent/agent/subagents/manager_acceptance_parent_facade.py`: 新增父级验收 manager facade 方法集合，让 `manager_acceptance.py` 继续只承接普通 acceptance review 流程。
- `agent_py_agent/agent/subagents/acceptance_test_execution.py`: 新增显式验收测试执行桥接，把 `AcceptanceReviewOptions(execute_tests=True)` 转成真实测试报告和阻断 findings；默认不运行。
- `agent_py_agent/agent/subagents/services/acceptance_findings.py`: 普通验收 finding 汇总层；已有 `reports/test_execution.json` 时优先以机器执行报告判断 tests_passed；pending capability finding 防止半成品被验收；`required_child_spawned` 会在目标要求创建下级时核对真实 `task.child_ids`，`descendant_health` 会阻断仍有未完成/失败后代的父级验收，但不会把 leaf/self 的“真实创建 leaf_worker”自述误判为还要继续创建 child。
- `agent_py_agent/agent/subagents/services/acceptance_descendant_health.py`: 父级验收的后代健康门；沿真实 `child_ids` 有界读取后代 `task.json`，后代未完成、未验收、失败、阻塞或缺失时返回 P0 finding。
- `agent_py_agent/agent/subagents/services/acceptance_controlled_exec_findings.py`: controlled_exec 专属验收 finding；当 goal/acceptance 声明受控 shell 时，要求实际工具记录和 stdout/audit/trash refs，并会在 task_dir/allowed_write_roots 内按固定小文件名查找 refs/summary；若 refs 文件显式指向 `controlled_exec-*.json` tool-output artifact，会按 64KB 上限精确读取该小 artifact 补齐 stdout/audit refs，防止伪 refs 报告通过且避免扫描目录或读取大日志。
- `agent_py_agent/agent/settings/config.py`: 新增 `acceptance_execute_tests`、`acceptance_test_timeout_seconds`、`subagent_allowed_tools`、`subagent_role_template_dirs`、`tool_agent_budget_*` 和 `tool_artifact_read_budget_*`，真实测试执行默认关闭，子代理工具默认自动判断，单代理工具预算默认 10 分钟 50 次，artifact 正文读取预算默认 10 分钟 240000 字符。
- `agent_py_agent/agent/settings/services/_normalize_runtime_fields.py`: 校验真实验收执行配置，布尔开关走 bool coerce，超时限制在 1 到 300 秒；`subagent_allowed_tools` 和 `subagent_role_template_dirs` 归一成去空白字符串列表。
- `agent_py_agent/config/agent_config.yaml`: 新增父级验收真实执行配置注释，说明默认关闭和单次命令覆盖方式。
- `agent_py_agent/cli/_acceptance_plan.py`: 新增 `subagents-acceptance-plan` 命令入口；默认只展示父级 dry-run 决策和 refs，`--write` 写入决策审计，`--apply` 只允许 inspect_only，`--next-action` / `--auto-policy` / `--auto-execution` / `--followup` / `--apply-followup` 都走显式分支。
- `agent_py_agent/cli/_acceptance_plan_renderers.py`: 拆出 acceptance-plan 的 JSON 转换和人类输出渲染；只展示 refs 和摘要，不读取引用正文。
- `agent_py_agent/cli/_review.py`: 新增 `subagents-tests` 命令入口并兼容导出验收命令；tests 命令默认只读已有 `test_execution.json` 摘要，`--re-run` 才显式执行 `output.json.tests` 并写回报告。
- `agent_py_agent/cli/subcommands_agents.py`: 注册 `subagents-tests <run_id> [--re-run] [--timeout]` 和 `subagents-acceptance-plan <run_id> [--json] [--write] [--apply] [--next-action] [--auto-policy]`，并给 `subagents-acceptance` 增加 `--execute-tests` / `--no-execute-tests` / `--test-timeout`。
- `agent_py_agent/cli/shared_progress.py`: 在共享进度 payload 中新增 `acceptance_plan_entries`，供 `status --json`、人类 `status` 和 `subagents` 看板展示父级验收 dry-run 决策；不执行 tests、不读取 artifact 正文。
- `agent_py_agent/cli/local_status_view.py`: 新增 `Acceptance Plan` 人类可读区块，渲染 shared-progress 里的父级验收决策摘要。
- `agent_py_agent/cli/_board.py`: `subagents` 看板新增 `Acceptance Plan` 区块，和 status 共用同一套 refs-only 渲染。
- `agent_py_agent/tests/test_local_store_control_plane.py`: 覆盖 LocalStore 控制面表、rollup 计算、上级/中间子代理 runtime query、timeout takeover candidates，以及 `SubAgentPersistenceService.save()` 的投影写入和 stale snapshot 保留 `child_ids` 路径。
- `agent_py_agent/tests/test_local_store_shared_progress_panel.py`: 覆盖共享进度面板如何组合 runtime query、task rollup、blocked runs、inheritance manifest refs 和 takeover readiness refs。
- `agent_py_agent/tests/test_subagent_inheritance_manifest.py`: 覆盖 parent/child 创建时的继承、覆盖、裁剪记录和 manifest JSON 落盘。
- `agent_py_agent/tests/test_subagent_failure_handoff.py`: 覆盖失败/阻塞 run 保存时的 failure handoff JSON 落盘和 LocalStore metadata refs。
- `agent_py_agent/tests/test_subagent_context_bundle.py`: 覆盖 Context Bundle v1 字段、Context Gate、runner prompt 接入、agent run workspace 镜像和四层 lineage refs。
- `agent_py_agent/tests/test_subagent_takeover_readiness.py`: 覆盖接管前必读包生成、context bundle refs、落盘和不读取 artifact 正文的边界。
- `agent_py_agent/tests/test_subagent_security_reserve.py`: 覆盖安全信号预留字段随 task 持久化，并投影到 LocalStore metadata。
- `agent_py_agent/tests/test_subagent_test_execution_record.py`: 覆盖真实验收执行记录模型的序列化、stdout/stderr 截断和 `passed` 语义。
- `agent_py_agent/tests/test_subagent_test_executor.py`: 覆盖最小真实验收执行器的 command、危险字符拦截、file_check、content_check 和 exact content 行为。
- `agent_py_agent/tests/test_subagent_test_item_preparation.py`: 覆盖父级验收 tests 预处理，包括 artifact 工作目录推断、安全 `cd &&` 拆分、pytest artifact fallback，以及 `cat <file>` 到 exact `content_check` 的归一化。
- `agent_py_agent/tests/test_subagent_test_item_preparation.py`: 覆盖父验收测试项如何从 artifact 路径推断工作目录、保留显式 working_dir 并拒绝越界 artifact。
- `agent_py_agent/tests/test_subagent_test_execution_report.py`: 覆盖 test execution JSON/Markdown 报告的汇总字段、记录恢复和人类摘要。
- `agent_py_agent/tests/test_parent_acceptance_controller.py`: 覆盖父级验收 dry-run 决策、refs-only 审计落盘、显式 apply 边界、next-action 建议和 auto-policy dry-run，包括缺少真实测试报告时建议执行、危险命令要求人工确认、已有通过报告时只需 inspect、patch 未审核时先 review_patches，以及非 inspect_only apply 不改任务状态。
- `agent_py_agent/tests/test_parent_acceptance_safe_cd.py`: 覆盖真实 runner 常见的 `cd <workspace内目录> && pytest` 输出漂移；父级 dry-run 预检和手动 auto-execution 都应归一化后继续走受限命令执行。
- `agent_py_agent/tests/test_parent_acceptance_patch_gate.py`: 覆盖真实测试通过但 applied patch 未审核时的 `review_patches` next-action 和 follow-up 命令。
- `agent_py_agent/tests/test_agent/test_subagent_acceptance.py`: 新增显式真实测试执行 dry-run 验收用例，覆盖 runner 假 PASS 被真实命令失败阻断且任务状态不被 dry-run 改写。
- `agent_py_agent/tests/test_subagents_tests_command.py`: 覆盖 `subagents-tests` 查看已有报告、显式 `--re-run` 写回报告、`subagents-acceptance-plan` 展示/写入/显式 apply/next-action/auto-policy 父级决策，以及 `subagents-acceptance` 从配置读取真实执行默认值并被 CLI 覆盖。
- `agent_py_agent/tests/test_config_validation.py`: 覆盖 `acceptance_execute_tests` 默认关闭、布尔 coerce 和 `acceptance_test_timeout_seconds` 范围校验。
- `agent_py_agent/tests/test_status_shared_progress.py`: 覆盖 `status` / `subagents` CLI 展示共享进度、failure handoff refs、takeover packet refs 和父级验收 dry-run 摘要，并断言大 artifact 正文不会内联。
- `agent_py_agent/tests/test_tool_round_execution.py`: 覆盖单轮工具执行 guard，确保 `schedule_child_subagents` 后的同轮 `dispatch_subagents` 会延后到下一轮读取真实 run ids。
- `agent_py_agent/tests/test_tooling_orchestration_parser.py`: 覆盖真实模型常见的 `orchestration` 参数包展开，保持 schedule/dispatch 等工具 bundle 入口兼容。
- `agent_py_agent/tests/test_orchestration_write_guard.py`: 覆盖派工写入预检，确保 UI 文案、HTML 标签和图片 URL 不会被误判为外部绝对路径，并验证疑似工作区路径拼写错误会返回可重试的 `suggested_target`。
- `agent_py_agent/tests/test_tool_output_externalizer.py`: 覆盖大工具输出外置 artifact 和外置前 fail-safe recovery snapshot。
- `agent_py_agent/tests/test_memory_compact_failsafe.py`: 覆盖 `memory-resume --from-compact` 如何展示 fail-safe checkpoint refs 且不读取 artifact 正文。
- `agent_py_agent/tests/test_memory_artifact_read.py`: 覆盖 CLI 和 `read_artifact` 工具如何显式读取已登记 artifact，并拒绝未登记普通文件；同时覆盖 `read_file` 不能直接读取 tool-output artifact JSON 包装、artifact head/tail/search 模式、单 run artifact 读取预算和缺 `read_artifact` 权限提示。
