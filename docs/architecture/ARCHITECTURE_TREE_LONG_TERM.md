# Architecture Tree: Long-Term Target
# 长期目标目录结构与分层规划

LLM: Use this as the definitive target module map when placing new code.
给人看的解释：本文件定义长期目录树。不要求一次性搬完历史代码，但新增代码必须优先放到目标边界内。

---

## 1. Current Directory Structure / 当前目录树

以下为 `agent_py_agent/` 的实际目录结构、职责说明与当前违规状态：

```
agent_py_agent/
├── cli/                          # 接口层：CLI 入口、交互、命令注册
│   ├── main_entry.py             #   argparse 顶层入口
│   ├── parser.py                 #   根解析器（已瘦身，委托给 commands/）
│   ├── parser_subcommands.py     #   子命令注册辅助
│   ├── chat.py                   #   [HARD 989行] 交互式聊天主循环 — 已部分拆分
│   ├── chat_parts/               #   已拆出的聊天子模块
│   │   ├── history.py            #     会话历史管理（MAX_HISTORY_TURNS, append/build）
│   │   ├── rendering.py          #     终端渲染（颜色、进度条、响应折叠、banner）
│   │   └── slash_commands.py     #     公共斜杠命令处理
│   ├── commands/                 #   按功能族的命令注册
│   │   ├── core.py               #     /chat, /memory, /status, /doctor 等
│   │   ├── gateway.py            #     /gateway start|stop|status|ask|submit
│   │   ├── subagents.py          #     /subagent list|board|dispatch|patch
│   │   ├── tasks.py              #     /task create|list|abandon|pause|resume
│   │   ├── learning.py           #     /learning enable|disable|report
│   │   ├── bench.py              #     /bench model|compare
│   │   └── operations.py         #     /ops log|audit|snapshot
│   ├── gateway_client.py         #   gateway HTTP 客户端封装
│   ├── gateway_service.py        #   gateway 本地服务封装
│   ├── gateway_loops.py          #   gateway 交互循环
│   ├── gateway_process.py        #   gateway 进程管理 CLI
│   ├── local_commands.py         #   /memory, /status, /doctor 等本地命令实现
│   ├── scenario.py               #   场景测试框架入口
│   ├── scenario_cases/           #   场景用例
│   ├── scenario_utils.py         #   场景工具
│   ├── daemon.py                 #   守护进程 CLI
│   ├── logs.py                   #   日志查看 CLI
│   ├── adapter.py                #   适配器 CLI
│   └── ...
│
├── agent/                        # 核心层：所有运行时逻辑
│   ├── agent_core/               #   SimpleAgent 主类 + 调度 mixin
│   │   ├── runtime_mixin.py      #     运行时能力（工具调用、模型交互、上下文管理）
│   │   ├── subagent_mixin.py     #     子代理相关公开入口 facade
│   │   ├── subagent/              #     子代理创建/运行生命周期组合服务
│   │   │   ├── lifecycle_service.py
│   │   │   ├── params.py
│   │   │   ├── run_flow.py
│   │   │   ├── spawn_flow.py
│   │   │   ├── finalize_helpers.py
│   │   │   ├── compact_continuation.py
│   │   │   ├── session_continuation.py
│   │   │   ├── session_compact_payload.py
│   │   │   ├── progress_closeout.py
│   │   │   └── attempt_guard.py
│   │   ├── tool_stream/           #     模型流式输出里的工具协议边界
│   │   │   ├── boundary.py
│   │   │   ├── models.py
│   │   │   └── write_abort.py
│   │   ├── failure_introspector.py  # 失败自省引擎（LLM 分析失败原因）
│   │   ├── failure_analyzer.py   #     失败模式分析
│   │   ├── planner.py            #     调度规划（runner 指令组合）
│   │   ├── runner/               #     runner 执行、提示、超时和 refs 边界
│   │   │   ├── dispatch.py       #     runner 分发（并发控制、重试、超时）
│   │   │   ├── gate.py           #     runner 执行入口和失败续跑提示
│   │   │   ├── prompts.py        #     runner 提示词模板
│   │   │   ├── ref_fields.py     #     runner 输入/输出 refs 解析
│   │   │   └── timeout_policy.py #     runner 超时策略
│   │   ├── dynamic_timeout.py    #     动态超时计算
│   │   ├── adaptive_retry.py     #     自适应重试
│   │   ├── task_complexity.py    #     任务复杂度评估
│   │   ├── watchdog.py           #     看门狗（daemon 巡检）
│   │   ├── orchestration_tools.py #    编排工具执行入口
│   │   ├── orchestration/        #     编排工具短规格与后续编排子包
│   │   │   ├── tool_specs.py     #     模型可见 ToolSpec builder
│   │   │   ├── tool_spec_data.py #     短参数说明数据
│   │   │   ├── tool_grants.py    #     子代理基础工具授权名单
│   │   │   ├── workflow_mode.py  #     编排工具 workflow_mode 归一化
│   │   │   ├── lineage_names.py  #     子代理显示名和序号归一化
│   │   │   ├── summary_action_lines.py # 编排工具结果上下文摘要
│   │   │   ├── create_config.py #       编排创建配置读取
│   │   │   ├── dispatch/
│   │   │   │   ├── mixin.py #      SimpleAgent dispatch facade
│   │   │   │   ├── facade.py #     watch/dispatch facade helpers
│   │   │   │   ├── service.py #    dispatch step record builders
│   │   │   │   ├── params.py #     dispatch/watch 参数对象和执行计划
│   │   │   │   ├── loop.py #       dispatch_loop 循环
│   │   │   │   ├── lock.py #       dispatch watch 文件锁
│   │   │   │   ├── no_progress.py # dispatch 空转识别
│   │   │   │   ├── limiter.py #    runner 启动限流
│   │   │   │   ├── runner_batches.py # runner 候选批量执行
│   │   │   │   ├── runner_candidates.py # runner 候选筛选
│   │   │   │   ├── runner_records.py # runner 记录生成
│   │   │   │   ├── runner_selection.py # runner scope 可见性
│   │   │   │   ├── collection_records.py # dispatch 记录收集
│   │   │   │   ├── collaboration_candidates.py # 协作请求候选
│   │   │   │   ├── capability_followup.py # 授权后续跑提示
│   │   │   │   ├── workflow_records.py # workflow 规划记录
│   │   │   │   ├── run_ids.py #    dispatch run_id 参数归一化
│   │   │   │   ├── load_errors.py # 子代理账本读取失败行
│   │   │   │   ├── payload.py #    dispatch_subagents 返回 payload
│   │   │   │   ├── state_contract.py # 当前回合状态摘要 payload
│   │   │   │   ├── refs.py #      子代理结果 refs-first 索引
│   │   │   │   ├── scope.py #     dispatch 作用域裁决
│   │   │   │   ├── progress_payload.py # runner-context child 进度摘要
│   │   │   │   ├── tool.py #      dispatch_subagents 模型工具入口
│   │   │   │   └── tool_helpers.py # dispatch 工具入口 helper
│   │   │   ├── replacements.py   #       replacement/takeover 记录
│   │   │   ├── run_scope.py      #       编排 run_id 轻量记忆
│   │   │   ├── work_scope.py     #       work_scope_key 派生
│   │   │   ├── background/
│   │   │   │   ├── dispatch.py # 后台自动启动 dispatch
│   │   │   │   └── marks.py #    后台启动标记
│   │   │   ├── tools/
│   │   │   │   ├── event.py #   raise_event 工具入口
│   │   │   │   └── status.py #  inspect_agent_tree 工具入口
│   │   │   ├── recovery_batches.py #    恢复策略分组
│   │   │   ├── runner_instruction.py #  runner_instruction 占位符解析
│   │   │   ├── create_payload.py #      create_subagents 返回 payload
│   │   │   ├── create_constraints.py #  create/schedule 写入根与约束冲突解析
│   │   │   ├── create_context.py #      create context manifest/context packs
│   │   │   ├── create_conversation.py # create 会话/thread 继承
│   │   │   ├── create_idempotency.py #  create 结构化幂等复用裁决
│   │   │   ├── create_items.py #        create items 批量参数解析
│   │   │   ├── create_policy.py #       CreateRunParams 组装
│   │   │   ├── create_target_roots.py # create/schedule 产品写入根推导
│   │   │   ├── shared_context.py #      父级小型读取 brief 传递
│   │   │   ├── lifecycle.py #           创建后发布、会话绑定和自动启动
│   │   │   ├── write_guard.py #         create/schedule 写入目标预检
│   │   │   ├── quality_advice_payload.py # QA 建议 payload
│   │   │   ├── quality_payload.py #      QA 失败信号 payload
│   │   │   ├── child_result_index.py #    子代理结果 refs 索引
│   │   │   ├── scope_resolution.py #      编排工具身份/scope 裁决
│   │   │   └── sibling_roster.py #        同批子代理 roster
│   │   ├── parameters.py         #     调度参数
│   │   ├── runtime/
│   │   │   └── capabilities.py #       运行时能力
│   │   └── models.py             #     核心数据模型
│   │
│   ├── subagents/                #   子代理管理（历史 mixin + services 拆分）
│   │   ├── manager.py            #     SubAgentManager 入口（纯组合类，无业务逻辑）
│   │   ├── manager_base.py       #     [HARD 744行] 基础CRUD + 卡片管理
│   │   ├── manager_patch.py      #     [HARD 794行] patch审核 + 应用
│   │   ├── manager_dispatch.py   #     调度派工逻辑
│   │   ├── services/board/service.py     #     看板核心服务
│   │   ├── services/board/facade.py #   旧 board API 兼容入口
│   │   ├── manager_acceptance.py #     验收流程
│   │   ├── manager_acceptance_findings.py  # 验收发现处理
│   │   ├── manager_runner_context.py  # runner 上下文注入
│   │   ├── manager_runner_results.py  # runner 结果处理
│   │   ├── manager_lifecycle.py  #     生命周期（暂停/恢复/放弃）
│   │   ├── services/capabilities/    # 能力路由集成
│   │   ├── manager_actions.py    #     动作 facade
│   │   ├── services/actions/     #     action apply 服务与 handler
│   │   ├── manager_indexing.py   #     索引管理
│   │   ├── services/indexing/    #     索引 service 与 report/local-record helper
│   │   ├── services/dispatch/    #     调度报告 service 与 params/helper
│   │   ├── services/leadership_recovery/ # 领导权恢复 plan/apply
│   │   ├── manager_learning.py   #     学习反馈
│   │   ├── manager_channel_probe.py   # 通道探测
│   │   ├── manager_normalize.py  #     数据归一化
│   │   ├── services/             #     [已启动] 服务层提取
│   │   │   ├── lifecycle.py      #       生命周期服务（能力请求/授予/缺口/心跳）
│   │   │   └── persistence/      #       持久化服务（load/list_runs/save）
│   │   ├── models.py             #     子代理数据模型
│   │   ├── policies.py           #     策略规则（动作映射、权重、路由）
│   │   ├── policy_checks.py      #     策略校验
│   │   ├── parsing.py            #     解析工具
│   │   ├── rendering.py          #     渲染工具
│   │   ├── reports.py            #     报告模型
│   │   ├── runner_rendering.py   #     runner 渲染
│   │   ├── result_processors.py  #     结果处理器
│   │   ├── utils.py              #     通用工具
│   │   └── probe.py              #     探测工具
│   │
│   ├── gateway_parts/            #   Gateway 子系统
│   │   ├── runtime.py            #     gateway 运行时（请求处理主循环）
│   │   ├── http_service.py       #     HTTP 服务（Flask/内置）
│   │   ├── supervisor.py         #     进程监管（自动重启、健康检查）
│   │   ├── daemon_control.py     #     守护进程控制（start/stop/restart）
│   │   ├── lease.py              #     租约机制（防止重复启动）
│   │   ├── adapter.py            #     gateway 适配器
│   │   ├── io.py                 #     gateway I/O
│   │   ├── paths.py              #     gateway 路径管理
│   │   ├── logging.py            #     gateway 日志
│   │   ├── process_control.py    #     进程控制
│   │   └── recovery.py           #     恢复机制
│   │
│   ├── memory_store/             #   JSONL 记忆存储
│   │   └── jsonl.py              #     追加写入 JSONL 格式记忆
│   │
│   ├── memory_routing/           #   记忆路由
│   │   ├── context.py            #     路由上下文
│   │   ├── loader.py             #     路由规则加载
│   │   ├── matcher.py            #     路由匹配引擎
│   │   ├── models.py             #     路由数据模型
│   │   └── validator.py          #     路由校验
│   │
│   ├── memory_archive/           #   记忆归档
│   │   ├── query.py              #     [HARD 839行] 归档查询
│   │   ├── runtime.py            #     归档运行时
│   │   ├── runtime/              #     归档运行时子模块
│   │   ├── snapshots.py          #     快照管理
│   │   ├── storage.py            #     归档存储
│   │   ├── tokens.py             #     token 计量
│   │   ├── resume_brief.py       #     恢复摘要
│   │   ├── resume_context.py     #     恢复上下文
│   │   └── models.py             #     归档数据模型
│   │
│   ├── log_analysis/             #   日志分析扩展
│   │   ├── analytics/            #     分析引擎
│   │   │   ├── baselines.py      #       基线管理
│   │   │   ├── security_rules.py #       安全规则
│   │   │   └── detectors/        #       检测器
│   │   │       └── rules.py      #         [HARD 747行] 检测规则
│   │   ├── dispatch/             #     日志分析调度
│   │   │   ├── engine.py         #       调度引擎
│   │   │   ├── queue.py          #       任务队列
│   │   │   ├── budgets.py        #       预算控制
│   │   │   ├── health.py         #       健康检查
│   │   │   └── work_orders/      #       工单管理
│   │   ├── storage/              #     日志存储
│   │   │   ├── base.py           #       存储基类
│   │   │   ├── local_store.py    #       本地存储实现
│   │   │   └── query.py          #       存储查询
│   │   ├── security/             #     安全分析
│   │   │   ├── attack_chain.py   #       攻击链分析
│   │   │   ├── correlation.py    #       关联分析
│   │   │   ├── entity_graph.py   #       实体图谱
│   │   │   └── hunting.py        #       威胁狩猎
│   │   ├── parsers/              #     日志解析器
│   │   ├── ingest/               #     日志摄入
│   │   ├── agents/               #     分析代理
│   │   ├── cases/                #     案例管理
│   │   ├── tools/                #     分析工具
│   │   ├── config/               #     分析配置
│   │   ├── config.py             #     分析配置
│   │   ├── interfaces.py         #     接口定义
│   │   ├── models.py             #     数据模型
│   │   ├── capabilities.py       #     能力定义
│   │   ├── bounded_query.py      #     有界查询
│   │   ├── work_order.py         #     工单
│   │   ├── reports.py            #     报告
│   │   ├── tools.py              #     工具
│   │   └── doctor.py             #     诊断
│   │
│   ├── tooling/                  #   工具层
│   │   ├── filesystem.py         #     文件系统读取工具
│   │   ├── filesystem_write.py   #     文件系统写入工具
│   │   ├── shell.py              #     Shell 执行工具
│   │   ├── web.py                #     Web 请求工具
│   │   ├── registry.py           #     工具注册表
│   │   ├── write_boundary.py     #     写入边界控制（安全校验）
│   │   ├── models.py             #     工具数据模型
│   │   └── parser.py             #     工具参数解析
│   │
│   ├── settings/                 #   配置
│   │   ├── config.py             #     [HARD 751行] AgentConfig dataclass + 加载逻辑
│   │   ├── config_normalize.py   #     配置归一化（未知字段过滤）
│   │   └── memory.py             #     记忆配置归一化
│   │
│   ├── session/                  #   会话管理
│   │   ├── manager.py            #     SessionManager（会话CRUD）
│   │   ├── models.py             #     会话数据模型
│   │   ├── cross_channel.py      #     跨通道接续
│   │   ├── context_sync.py       #     上下文同步
│   │   ├── admin_query.py        #     管理查询
│   │   └── resume.py             #     会话恢复
│   │
│   ├── capability/               #   能力路由
│   │   ├── config.py             #     能力配置
│   │   ├── router.py             #     能力路由器
│   │   └── skills.py             #     技能定义
│   │
│   ├── adapter/                  #   通道适配器
│   │   ├── base.py               #     适配器基类
│   │   ├── protocol.py           #     适配器协议
│   │   ├── manager.py            #     适配器管理器
│   │   ├── qq.py                 #     QQ 适配器
│   │   └── feishu.py             #     飞书适配器
│   │
│   ├── extensions/               #   插件系统
│   │   └── plugin.py             #     插件注册与加载
│   │
│   ├── repositories/             #   [占位] 仓库层（当前仅 README.md）
│   │
│   ├── task_registry/            #   任务注册表
│   │   ├── registry.py           #     任务注册
│   │   └── query.py              #     任务查询
│   │
│   ├── audit/                    #   审计
│   │   ├── logger.py             #     审计日志写入
│   │   └── query.py              #     审计查询
│   │
│   ├── concurrency/              #   并发控制
│   │   ├── optimistic_lock.py    #     乐观锁
│   │   ├── task_lock.py          #     任务锁
│   │   ├── retry.py              #     重试机制
│   │   └── exceptions.py         #     并发异常
│   │
│   ├── auth/                     #   认证
│   ├── notification/             #   通知
│   ├── security/                 #   安全
│   ├── observability/            #   可观测性
│   ├── clients/                  #   外部客户端
│   ├── backends/                 #   模型后端
│   ├── model_speed/              #   模型速度管理
│   ├── prompting_parts/          #   提示词组件
│   ├── subagent_workflows/       #   子代理工作流
│   ├── user_space/               #   用户空间
│   ├── validators/               #   校验器
│   ├── local_storage/            #   本地存储（SQLite）
│   └── io/                       #   I/O 工具
│
├── memory/                       # 全局记忆文件（JSONL）
│   ├── hooks/                    #   记忆钩子
│   └── raw/                      #   原始记忆
├── memory_archive/               # 全局记忆归档
│   └── tokens/                   #   token 记录
├── config/                       # 配置文件模板
├── data/                         # 运行时数据（.gitignore）
│   ├── subagents/                #   子代理运行时数据
│   ├── local_store/              #   本地存储数据
│   ├── notifications/            #   通知数据
│   ├── gateway/                  #   gateway 运行时数据
│   └── log_fixtures/             #   日志测试数据
├── prompts/                      # 提示词模板
└── tests/                        # 测试
    ├── test_tools/               #   工具测试
    └── test_agent/               #   代理测试
```

---

## 2. Target Layered Architecture / 目标分层架构

长期目标：从当前"按功能目录"迁移为严格分层的六层架构。

```
agent_py_agent/
│
├── interfaces/                   # 接口层：用户/系统交互入口
│   ├── cli/                      #   命令行界面
│   │   ├── parser.py             #     根解析器
│   │   ├── commands/             #     命令注册（core, gateway, subagents, tasks...）
│   │   ├── chat/                 #     交互式聊天（command_loop, tui, slash_router, renderer）
│   │   └── gateway_client.py     #     gateway HTTP 客户端
│   ├── http/                     #   HTTP 接口
│   │   └── service.py            #     gateway HTTP 服务
│   └── adapters/                 #   通道适配器
│       ├── base.py               #     适配器协议（Protocol）
│       ├── qq.py                 #     QQ 适配器
│       ├── feishu.py             #     飞书适配器
│       └── manager.py            #     适配器管理器
│
├── application/                  # 应用层：用例编排、流程控制
│   ├── dispatch/                 #   调度编排
│   │   ├── planner.py            #     调度规划
│   │   ├── runner.py             #     runner 分发
│   │   ├── loop.py               #     调度循环
│   │   └── audit.py              #     调度审计
│   ├── subagent_services/        #   子代理服务
│   │   ├── lifecycle.py          #     生命周期服务
│   │   ├── dispatch.py           #     派工服务
│   │   ├── acceptance.py         #     验收服务
│   │   ├── patch.py              #     patch 服务
│   │   ├── board.py              #     看板服务
│   │   ├── runner_context.py     #     runner 上下文服务
│   │   └── runner_result.py      #     runner 结果服务
│   ├── session/                  #   会话管理
│   │   ├── manager.py            #     SessionManager
│   │   ├── cross_channel.py      #     跨通道接续
│   │   └── resume.py             #     会话恢复
│   ├── task_registry/            #   任务注册表
│   │   ├── registry.py
│   │   └── query.py
│   └── learning/                 #   学习反馈
│
├── domain/                       # 领域层：核心业务规则（无外部依赖）
│   ├── subagent/                 #   子代理领域
│   │   ├── models.py             #     SubAgentTask, SubAgentCard, QualityContract...
│   │   ├── policies.py           #     策略规则（纯函数）
│   │   └── ports.py              #     端口接口（Repository Protocol）
│   ├── memory/                   #   记忆领域
│   │   ├── routing_rules.py      #     路由规则
│   │   ├── archive_policy.py     #     归档策略
│   │   └── ports.py              #     存储端口
│   ├── capability/               #   能力领域
│   │   ├── skill.py              #     技能定义
│   │   └── router.py             #     路由规则
│   ├── log_analysis/             #   日志分析领域
│   │   ├── detection_rules.py    #     检测规则
│   │   └── security_rules.py     #     安全规则
│   └── audit/                    #   审计领域
│       └── events.py             #     审计事件定义
│
├── infrastructure/               # 基础设施层：技术实现
│   ├── persistence/              #   持久化
│   │   ├── jsonl_store.py        #     JSONL 记忆存储实现
│   │   ├── sqlite_store.py       #     SQLite 本地存储实现
│   │   ├── file_store.py         #     文件系统存储实现
│   │   └── repositories/         #     仓库实现
│   │       ├── task_repo.py      #       TaskRepository 实现
│   │       ├── artifact_repo.py  #       ArtifactRepository 实现
│   │       └── index_repo.py     #       IndexRepository 实现
│   ├── llm/                      #   LLM 后端
│   │   └── backends/             #     各模型后端实现
│   ├── gateway/                  #   Gateway 进程管理
│   │   ├── runtime.py
│   │   ├── supervisor.py
│   │   ├── daemon_control.py
│   │   └── lease.py
│   ├── tooling/                  #   工具实现
│   │   ├── filesystem.py
│   │   ├── shell.py
│   │   ├── web.py
│   │   └── write_boundary.py
│   ├── concurrency/              #   并发原语
│   │   ├── optimistic_lock.py
│   │   └── task_lock.py
│   ├── notification/             #   通知通道
│   └── security/                 #   安全基础设施
│
├── extensions/                   # 扩展层：插件系统
│   ├── plugin.py                 #     插件注册与加载
│   └── log_analysis/             #     日志分析扩展（整体作为插件）
│
└── shared/                       # 共享层：跨层通用
    ├── config/                   #   配置
    │   ├── agent_config.py       #     AgentConfig dataclass
    │   ├── normalize.py          #     配置归一化
    │   └── memory_config.py      #     记忆配置
    ├── models/                   #   通用数据模型
    │   └── common.py             #     跨模块共享的值对象
    ├── exceptions.py             #   统一异常定义
    └── utils.py                  #   通用工具函数
```

### Layer Dependency Rules / 层间依赖规则

```
interfaces ──> application ──> domain ──> shared
     |              |             |
     +--------------+-------------+
                    |
              infrastructure
              (实现 domain 定义的端口)
```

| 层 | 可依赖 | 不可依赖 |
|---|---|---|
| interfaces | application, shared | domain（直接）, infrastructure（直接） |
| application | domain, shared | infrastructure（直接，必须通过 domain 的端口） |
| domain | shared | 其他所有层 |
| infrastructure | domain, shared | application, interfaces |
| shared | 无 | 所有其他层 |
| extensions | domain, shared | application, interfaces, infrastructure（直接） |

---

## 3. Migration Path / 迁移路径

### Phase 1: Shared Layer Extraction（提取共享层）
**目标**: 建立零依赖的共享基础
**预计工作量**: 2-3 天

1. 创建 `shared/config/agent_config.py`，将 `settings/config.py` 中的 `AgentConfig` dataclass 移入
2. 将 `settings/config_normalize.py` 和 `settings/memory.py` 移入 `shared/config/`
3. 创建 `shared/exceptions.py`，统一 `concurrency/exceptions.py` 等分散的异常定义
4. 保留 `settings/` 作为转发层（`from shared.config import AgentConfig`）保持兼容
5. 更新所有 `from agent.settings.config import AgentConfig` 为 `from agent.shared.config import AgentConfig`

### Phase 2: Repository Pattern（建立仓库模式）
**目标**: 所有持久化操作经过仓库接口
**预计工作量**: 3-5 天

1. 在 `domain/subagent/ports.py` 定义 `TaskRepository`, `ArtifactRepository`, `IndexRepository` Protocol
2. 在 `infrastructure/persistence/repositories/` 实现这些 Protocol
3. 将 `memory_store/jsonl.py` 的直接文件操作封装为 `JsonlMemoryRepository`
4. 将 `local_storage/` 的 SQLite 操作封装为 `SqliteLocalRepository`
5. 将 `tooling/write_boundary.py` 保留为校验层，但写入操作委托给仓库
6. 更新 `subagents/services/persistence/` 使用仓库接口

### Phase 3: God Class Decomposition（拆解巨型类）
**目标**: 消除所有 HARD 违规
**预计工作量**: 5-7 天

1. `chat.py` (989行) -> 继续 `chat_parts/` 拆分（见 CHAT_REFACTOR_PLAN.md）
2. dispatch 主体已迁入 `agent_core/orchestration/dispatch/`，继续保持 facade/runner/loop/record 职责分离。
3. `manager_base.py` (744行) -> 基础 CRUD 移入 `subagent_services/persistence/` + `subagent_services/board/service.py`
4. `manager_patch.py` (794行) -> 移入 `subagent_services/patch.py`
5. `memory_archive/query/` 已拆成查询子包；继续保持 builder / executor / formatter 职责分离，不再恢复平铺 `query.py`
6. `log_analysis/analytics/detectors/rules.py` (747行) -> 拆为 `detectors/rule_engine.py`, `detectors/rule_loader.py`, `detectors/rule_matcher.py`
7. `settings/config.py` (751行) -> `AgentConfig` 移入 `shared/config/`，加载逻辑移入 `infrastructure/`

### Phase 4: Layer Migration（层迁移）
**目标**: 目录结构对齐目标分层
**预计工作量**: 3-5 天

1. `cli/` -> `interfaces/cli/`（渐进式，通过 `__init__.py` 转发保持兼容）
2. `agent/gateway_parts/` -> `infrastructure/gateway/`
3. `agent/adapter/` -> `interfaces/adapters/`
4. `agent/tooling/` -> `infrastructure/tooling/`
5. `agent/memory_store/` -> `infrastructure/persistence/jsonl_store.py`
6. `agent/subagents/models.py` -> `domain/subagent/models.py`
7. `agent/subagents/policies.py` -> `domain/subagent/policies.py`

### Phase 5: Dependency Inversion（依赖反转）
**目标**: domain 层定义端口，infrastructure 实现
**预计工作量**: 2-3 天

1. 在 `domain/memory/ports.py` 定义 `MemoryStorePort`, `MemoryArchivePort` Protocol
2. 在 `domain/capability/ports.py` 定义 `CapabilityRouterPort` Protocol
3. infrastructure 层实现这些 Protocol
4. application 层通过构造函数注入依赖
5. 配置 `import-linter` 强制执行分层规则

---

## 4. Module Dependency Direction Rules / 模块依赖方向规则

### 4.1 Forbidden Import Patterns / 禁止的导入模式

```python
# ---- cli/ 禁止直接导入 infrastructure ----
# 错误：
from agent.memory_store.jsonl import JsonlMemoryStore
from agent.tooling.filesystem import read_file
# 正确：通过 application 层的服务间接访问

# ---- infrastructure 禁止导入 application ----
# 错误：
from application.dispatch.planner import DispatchPlanner
# 正确：infrastructure 实现 domain 定义的端口

# ---- domain 禁止导入 infrastructure ----
# 错误：
from infrastructure.persistence.jsonl_store import JsonlStore
# 正确：domain 定义 Protocol，infrastructure 实现

# ---- domain 禁止导入 application 或 interfaces ----
# 错误：
from application.session.manager import SessionManager
from interfaces.cli.chat import ChatSession

# ---- shared 禁止导入任何其他层 ----
# 错误：
from agent.session.manager import SessionManager
```

### 4.2 Allowed Import Patterns / 允许的导入模式

```python
# cli/ -> application 层的服务
from agent.application.session.manager import SessionManager
from agent.application.dispatch.loop import DispatchLoop

# application -> domain
from agent.domain.subagent.models import SubAgentTask
from agent.domain.subagent.policies import action_for_issue
from agent.domain.subagent.ports import TaskRepository

# application -> shared
from agent.shared.config import AgentConfig
from agent.shared.exceptions import DispatchError

# infrastructure -> domain（实现端口）
from agent.domain.subagent.ports import TaskRepository

# infrastructure -> shared
from agent.shared.config import AgentConfig

# 所有层 -> shared（允许）
from agent.shared.utils import safe_json_loads
```

### 4.3 Dependency Validation / 依赖验证

项目应配置静态分析工具强制执行分层规则：

```toml
# pyproject.toml
[tool.importlinter]
root_package = "agent_py_agent"

[[tool.importlinter.contracts]]
name = "cli-only-depends-on-application"
type = "layers"
layers = [
    "agent_py_agent.interfaces",
    "agent_py_agent.application",
    "agent_py_agent.domain",
    "agent_py_agent.shared",
]

[[tool.importlinter.contracts]]
name = "domain-only-depends-on-shared"
type = "layers"
layers = [
    "agent_py_agent.domain",
    "agent_py_agent.shared",
]
```

---

## 5. Current Migration State / 当前迁移进度

| 阶段 | 状态 | 说明 |
|---|---|---|
| Phase 1: Shared Layer | 未开始 | `settings/config.py` 仍在原位 |
| Phase 2: Repository Pattern | 占位已建 | `repositories/` 目录存在但仅有 README.md |
| Phase 3: God Class Decomposition | 部分完成 | `chat_parts/` 已拆 3 个文件；`subagents/services/` 已拆 2 个服务 |
| Phase 4: Layer Migration | 未开始 | 目录结构仍为原始布局 |
| Phase 5: Dependency Inversion | 未开始 | 尚无 Protocol 定义 |

### Already Completed / 已完成项
- `cli/parser.py` 已瘦身，委托给 `cli/commands/`
- `cli/chat_parts/history.py` 已提取会话历史管理
- `cli/chat_parts/rendering.py` 已提取终端渲染
- `cli/chat_parts/slash_commands.py` 已提取公共斜杠命令
- `subagents/services/persistence/` 已提取持久化服务
- `subagents/services/lifecycle.py` 已提取生命周期服务
- `subagents/manager.py` 已成为纯组合类（14个mixin拼合）
- 已消除所有 `import *` 使用

---

## 6. Key Design Principles / 关键设计原则

### 6.1 Single Responsibility / 单一职责
每个模块只有一个变化原因。当前 `manager_base.py` 承担了 CRUD、卡片管理、策略执行、渲染等多种职责，必须拆分。

### 6.2 Dependency Inversion / 依赖反转
高层模块不依赖低层模块的实现细节。`application/dispatch/` 不直接 import `infrastructure/persistence/jsonl_store.py`，而是通过 `domain/subagent/ports.py` 定义的 Protocol。

### 6.3 Interface Segregation / 接口隔离
不强迫模块依赖它不使用的接口。当前 `SubAgentManager` 的 14 个 mixin 拼合导致所有调用者都能访问所有方法，应拆为多个窄接口。

### 6.4 Open-Closed / 开闭原则
通过 `extensions/plugin.py` 的插件机制和 `capability/router.py` 的能力路由实现扩展开放、修改关闭。新增通道适配器不需要修改核心调度逻辑。

### 6.5 Write Boundary / 写入边界
所有文件写入必须经过 `tooling/write_boundary.py` 的校验和 `repositories/` 的封装，防止任意路径写入。这是安全边界的核心。

### 6.6 永不停机原则
迁移过程不得中断现有功能。所有变更通过 facade 保持向后兼容，直到所有调用者迁移完毕。
