# Codebase Tree

## 发布与开发入口

```text
|-- LICENSE                              # 主项目许可证
|-- NOTICE                               # 适用的第三方版权与许可说明，随包发布
|-- STATUS.md                            # 当前能力、开放问题与证据边界
|-- DESIGN_LEDGER.md                     # 当前架构决策及模块设计导航
|-- TESTS.md                             # 开发测试、真实 TUI 与发布 gate
`-- docs/design/
    |-- MAINTAINABILITY_AND_JEV_REVIEW.md # 可维护性评估、渐进重构建议及 Computer Use/Jev 能力边界
    |-- PLUGIN_LIFECYCLE.md              # 可装卸插件、动态命令、版本切换与故障回收的待实施方案
    |-- PLUGIN_PACKAGES.md               # 本地包静态校验与待接线的安装事实、隔离和撤销边界
    |-- PLUGIN_WORKSPACE_CONTEXT.md      # 逐次只读工作区协议、路径裁决与轻量 SDK 构建边界
    |-- PLUGIN_ACTIVATION.md             # 唯一安装表的激活 CAS、撤销、显式迁移及待接线资源边界
    |-- MANAGED_PROCESS_STDIO.md         # 原 host 字节管道、激活资源归属及旧版本恢复边界
    |-- HOST_COMMAND_EXECUTION.md        # 显式命令复用原运行链的请求身份、重送和结果回读合同
    |-- PLUGIN_SAMPLE_ACCEPTANCE.md      # 10 个自有简易插件的来源、功能范围及真实 TUI 验收计划
    |-- TUI_DESIGN.md                    # 终端布局、事件、输入与生命周期规范
    |-- SUBAGENT_PARALLEL_EXECUTION.md   # 父子独立工作、逐项交付与慢任务诊断边界
    `-- TUI_BEHAVIOR_CHECKLIST.md        # 不依赖历史流水的 TUI 验收场景
```

十步重构的执行状态和逐批验收入口：`docs/tasks/REFACTOR_PLUGIN_GOAL.md`。

这份树只描述当前主链路。旧迁移入口、过渡计划和已删除模块不在这里保留。

```text
.dockerignore                           # Docker 生产源码允许列表，排除本地运行状态和测试产物
install.sh                              # 默认一键容器安装；生成透明 my-agent CLI，--host 为开发模式
TUI_EXTREME_TEST_MATRIX.md              # 五路 TUI 持续轮转、边界/组合/fuzz/soak 用例与客观证据权威账本
agent_py_agent/
|-- __main__.py                         # python -m agent_py_agent CLI 入口
|-- config/                             # 默认 YAML 配置
|-- vendor/bubblewrap/                  # 随 Linux 离线二进制携带的许可、对应源码 RPM、构建及来源说明
|-- cli/                                # 命令行、chat/TUI、gateway 管理、诊断维护命令
|   |-- chat.py                         # 本地 chat 入口
|   |-- chat_client_context.py          # 轻量 Gateway TUI 客户端、活动输入三态与 input-status 查询
|   |-- scenario.py                     # 隔离诊断场景注册、命令参数来源及 suite 调度
|   |-- scenario_cases/                 # 各类诊断场景及其专用替身
|   |   |-- runner_retry_case.py        # runner 重试场景，保留现有收口断言及其失败证据
|   |   `-- runner_retry_backend.py     # 只供该场景注入的离线失败后端
|   |-- memory_admin_parser.py          # Memory v2 唯一管理员命令树与中文参数帮助
|   |-- memory_admin_commands.py        # Candidate/Curator/Retention/Doctor/Migration 共用正式 Service 的 CLI 适配
|   |-- chat_parts/                     # TUI、gateway client、stream/render worker
|   |   |-- chat_prompt_queue.py        # 可按 request identity 原子回取且保持 FIFO/task_done 账的聊天任务队列
|   |   |-- tui_agent_navigation.py     # TUI 精确子代理选择栈、详情游标与父子视图切换状态
|   |   |-- tui_goal_editor.py          # 主子代理共用 Goal 草稿编辑、明确保存、版本冲突与放弃退出
|   |   |-- tui.py                      # chat TUI 生命周期、唯一 runtime/worker/preflight 接线与返回码
|   |   |-- tui_block_renderer.py       # typed snapshot 到欢迎/消息/思考/工具/权限/队列/footer formatted lines
|   |   |-- tui_complete_detail.py      # 完整原文有界分页、长行分片与稀疏页索引
|   |   |-- tui_display_archive.py      # 异步读取原文归档页，缓存限额与失败重试
|   |   |-- slash_commands.py          # CLI 命令分派与公共声明的帮助投影
|   |   |-- plugin_command_client.py   # 显式宿主模式、会话目录缓存与原版本提交
|   |   |-- command_interaction.py     # 单次命令的编号、审批消费者及独立取消引用
|   |   |-- plugin_command_stream.py   # 持续读原命令流并异步回写完整审批绑定
|   |   |-- tui_plugin_commands.py     # 输入候选版本绑定与不阻塞输入的插件命令分派
|   |   |-- slash_command_types.py     # CLI 命令处理器的可信上下文，不另设命令目录
|   |   |-- tui_input.py                # 真实 slash/path 补全、菜单、history suggest 与排队占位投影
|   |   |-- tui_model_menu.py           # /model 新增/选择/退出浮层，私密密钥与显式上下文窗口
|   |   |-- tui_model_auth.py           # 私密设备码登录、通用参数编辑、取消及退出账号
|   |   |-- tui_model_metrics.py        # Context 下方模型轮、工具数、最近缓存、会话累计与输出速度的一行统计
|   |   |-- tui_shared_model_menu.py    # 管理员逐模型显式共享/撤销，普通用户只选已开放模型
|   |   |-- tui_permissions_menu.py     # /permissions 与 F4 三档权限菜单、保存/取消及管理员确认
|   |   |-- tui_provider_menu.py        # 服务商、多模型编辑、启停、目录发现和明确短连接测试
|   |   |-- tui_input_delivery.py       # 活动回合输入的持久 outbox、同 ID 对账与排队接管
|   |   |-- tui_control_delivery.py     # slash 控制命令的持久 outbox、稳定操作 ID 与只读状态对账
|   |   |-- tui_interaction.py          # stash、Ctrl-R、paste 与 `?` help 的线程安全输入状态机
|   |   |-- tui_markdown.py             # CommonMark/table token 到 Unicode 宽度换行、语法色和 prompt_toolkit fragments
|   |   |-- tui_paste.py                # 大文本粘贴显示引用与提交时精确展开合同
|   |   |-- tui_preflight.py            # alternate screen 内真实 Gateway readiness 等待、typed 连接事件与 worker 启动门
|   |   |-- tui_permission_queue.py     # 主代理与多个 child 的工具审批统一 FIFO、原 request 回写和展示去重
|   |   |-- tui_runtime.py              # session/queue/turn/model/tool/Gateway typed rows 到稳定 TuiEvent 的唯一 adapter
|   |   |-- tui_terminal.py             # OSC 终端标题、活动帧与退出清理
|   |   |-- tui_transcript.py           # 详细 transcript 冻结视图、全文搜索与命中导航状态
|   |   |-- tui_view.py                 # prompt_toolkit typed transcript control、frame/block cache、scroll anchor 与 overlay/footer
|   |   |-- tui_history.py              # 上翻异步读取更早页、同页面响应校验和冻结视口衔接
|   |   |-- tui_events.py               # TUI 唯一 versioned event 信封、单调 sequencer 与幂等有界 journal
|   |   |-- tui_view_model.py           # typed event reducer：稳定/活动 block、权限 overlay、输入队列与状态快照
|   |   |-- tui_ui_setup.py             # alternate-screen prompt_toolkit 布局、控件、style 与 focus 接线
|   |   |-- tui_keybindings.py          # 输入、帮助、权限、队列、滚动、transcript、中断与退出 typed key intents
|   |   |-- tui_clipboard.py            # 单应用有界复制顺序与分通道结果，防止旧选区覆盖新复制
|   |   `-- control_runtime.py          # CLI 对共享会话控制协议及窗口级精确中断的运行适配
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- gateway_process.py              # gateway 进程入口
|   |-- gateway_lane_retry.py           # 后台 owner/thread 配置等待与普通冷却；有界、线程安全、不另存任务状态
|   `-- _*.py                           # CLI 子命令实现
|-- skills/builtin/<category>/<name>/   # 内置知识型 skill 树：目录即分类（research/documents/…），递归扫描，类目索引常驻 prompt，skill_search 工具按需检索（千级地基）
|-- agent/
|   |-- command_catalog.py             # 核心命令声明、别名、会话词法、保留命名空间与帮助/补全事实
|   |-- command_arguments.py           # 参数不可变声明、类型校验与保留原文范围的公共词法
|   |-- command_binding.py             # 同源参数绑定、缺值位置、帮助与选项分界
|   |-- command_declarations.py        # 包和宿主目录共用的严格 JSON 声明读取器
|   |-- plugin_commands.py             # 插件命名空间、宿主描述解析与无副作用的静态回执
|   |-- plugin_command_catalog.py      # 可验证的不可变目录快照、JSON 合同及内容版本
|   |-- plugin_command_service.py      # 宿主作用域目录与旧版本拒绝，不拥有安装和执行权
|   |-- plugin_completion.py           # 用公共词法和绑定事实生成只编辑输入的候选
|   |-- plugin_manifest.py             # 静态包描述、不可变 schema 与默认停用的命令投影
|   |-- plugin_package.py              # 有界 ZIP 读取、成员与摘要核对，不安装或导入插件
|   |-- plugin_wheels.py               # wheel 标准元数据、RECORD、平台与本地依赖闭包预检
|   |-- plugin_wheel_layout.py         # 环境内文件计划、引导文件保护与宿主只读核对
|   |-- plugin_environment.py          # 固定地址的 owner 独立 venv 准备，尚不发布激活
|   |-- plugin_environment_plan.py     # 原 operation 领取前冻结包、配置版本、解释器与候选身份
|   |-- plugin_environment_process.py  # 原 claim 精确复查、托管准备命令与有界取消清理
|   |-- plugin_installation.py         # 安装请求、私有配置事实、通用提交回执与安装准入
|   |-- plugin_installation_state.py   # 唯一安装表 v3 编解码及 v1/v2 明确迁移来源
|   |-- plugin_configuration.py        # 完整配置替换、原请求重放与版本 CAS 纯裁决
|   |-- plugin_activation_record.py    # 固定环境计划、同代准备/发布/撤销身份及严格读回
|   |-- plugin_activation_ref.py       # 可信 owner 与原代次引用，跨进程复查唯一安装表
|   |-- plugin_activation.py           # 原安装版本上的激活迁移、阶段重放与旧代拒绝
|   |-- plugin_runtime.py              # 固定代次的 MCP 服务、完整目录校验与原工具代理
|   |-- workspace_read_context.py      # 宿主与插件共用的冻结读取协议及逐项路径检查
|   |-- plugin_invocation.py           # 显式业务选择摘要、原执行器组装及单次 MCP 连接收尾
|   |-- plugin_deactivation.py         # 撤销原代、关闭准备执行权并清理两类精确资源
|   |-- plugin_release.py              # 原准备执行器退出与完整资源证据核验，不改写 UNKNOWN
|   |-- plugin_cleanup.py              # 原结果成功读回后消费退出引用、回收无人引用包，重送不重跑
|   |-- plugin_removal.py              # 固定安装删除 CAS 与卸载回执，不新增墓碑或持久历史
|   |-- plugin_install_store.py        # owner 唯一安装表、包内容保存及锁内 CAS/提交裁决
|   |-- plugin_install_tool.py         # 经原执行器读取授权包快照并保存默认停用记录
|   |-- plugin_configure_tool.py       # 经原执行器读取和验证私有配置，结果不含配置值
|   |-- plugin_enable_tool.py          # 原操作内准备环境、核对目录并确认候选退出后发布
|   |-- plugin_disable_tool.py         # 原宿主链中的隐藏停用工具，区分撤销与资源清理结果
|   |-- plugin_remove_tool.py          # 原宿主链中先停用释放再卸载，保留产物及确定/未知提交事实
|   |-- plugin_sources.py              # 安装包与配置共用的有界授权文件读取
|   |-- plugin_management.py           # 原权限、线程与目录的组合，管理/业务分派及原结果查询
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- turn_end.py                     # 主/子代理共用的结束原因及技术续跑判据
|   |-- model_guidance.py               # 完整 Prompt 与有副作用工具共用的验证/授权软提示唯一正文
|   |-- task_progress_guidance.py        # Todo exact-id 最终回复前核对的可配置软合同；不自动判定或打勾
|   |-- agent_core/                     # 无副作用包入口；主代理运行时、工具循环、编排与自然回合收口实现
|   |   |-- cli_run_conversation.py     # 一次性 CLI 的权威 user/assistant transcript、幂等身份与失败分级
|   |   |-- runtime/                    # 单 child guidance、active-turn compact carrier、sleep 闹钟与 loop support
|   |   |   |-- conversation_state.py  # 主/子代理当前 Compact 代次的结构化模型事实投影
|   |   |   |-- sleep_tool.py           # clock.sleep 工具：模型主动定时等待，写 wake_queue 字条、事件提前醒取消
|   |   |-- model/                      # 主工具循环的统一模型调用账、动态超时、上下文压力与成本统计
|   |   |-- tool_loop/                  # 工具轮次执行、恢复与自然结束
|   |   |-- tool_loop/display_archive.py # 执行当时的公开工具原文归档与轻量预览引用
|   |   |-- tool_context/               # 工具结果上下文：reducer、窗口、microcompact、PTL 单轮重试
|   |   |-- orchestration/              # 创建、只读状态、消息、取消、授权五个递归直属工具与内部自动启动/恢复引擎；无兄弟 goal 广播，进展事件由宿主写入
|   |   |   |-- create_context.py     # 显式资料与合同装配；已移除 work_scope.py 的自动 IO 身份，引用不合并派工
|   |   |   |-- capacity.py           # 根/子/孙代理共用的会话树与 owner 容量事实；超限整批拒绝
|   |   |   |-- coordinator_policy.py # 主代理/多层 coordinator 共用的 会话运行时 式派工后职责软合同
|   |   |   |-- tools/list_agents.py  # 会话运行时 式只读代理树查询；复用 canonical 投影，不推进或取消下级
|   |   |   `-- planned_delegation.py # 已有 Todo 时，创建前原子校验 active exact covers 与父级 workspace 上界
|   |   |-- agent_tree/status.py        # `/status`、TUI、恢复与诊断共用的内部代理树投影（不是模型工具）
|   |   |-- agent_tree/model_view.py    # 同一授权快照的模型状态/结果视图；超长摘要保留逻辑归档入口
|   |   |-- _finalization_service.py   # 保留模型最终正文并记录 turn_end.reason
|   |   |-- tool_loop/natural_user_reply.py # 派工/续跑/完成共用的无工具 LLM 用户回复出口
|   |   |-- tool_loop/completion.py     # 工具上限、截断与递归父级自然让出后的依赖等待
|   |   |-- tool_loop/deliverable_closeout.py # 子代理声明了交付物却缺产物时的有界收口门（只认宿主声明的清单）
|   |   `-- runner/                     # 子代理 runner prompt/worker/session/timeout；context.py 也隔离共享 Agent 的 thread-local 运行态
|   |       `-- activity_diagnostics.py # 复用心跳的 exact attempt 阶段提醒，不强杀慢模型或自动重派
|   |-- subagents/
|   |   |-- manager.py                  # 子代理 root manager：初始化、基础生命周期、服务组合
|   |   |-- coordination.py             # 原创建文件锁的同线程重入，统一创建与执行轮短事务
|   |   |-- runner_start.py             # 准确 pending 接纳与原身份核对，共用创建锁
|   |   |-- file_runner_start.py        # 显式文件模式的一次性启动与撤销，整任务保存保留原接纳事实
|   |   |-- cancellation.py             # 原子树权限关闭和资源冻结，锁外仅清理固定批次
|   |   |-- cancellation_hosts.py       # 原执行轮协作中断与宿主退出只读观察，不杀共享或接续进程
|   |   |-- runner_control.py           # 原 RuntimeDB/canonical 取消判据，供心跳和执行前复核
|   |   |-- kernel.py                   # 子代理树快照
|   |   |-- manager_work_orders.py      # 工单路径、默认文件、校验
|   |   |-- models.py                   # 子代理数据模型
|   |   |-- process_control.py          # 子代理宿主存活/启动事实，复用公共进程树终止并保留核对回执
|   |   |-- direct_parent_lifecycle.py # 直属父子等待、事件唤醒、同批合并与结果上下文
|   |   |-- tool_failure_ledger.py      # 系统级工具失败账本：archive ok=False 摘要 -> attributes/对账投影
|   |   |-- result_registered_artifacts.py # 自然/结构化收口共用的 exact run 工具产物投影
|   |   |-- services/                   # 子代理业务服务
|   |   |   |-- base.py                 # create_run/split/owner/runtime config scope
|   |   |   |-- output_alignment.py    # 声明按可信 cwd 解析，不重定位、不搬运、不增权
|   |   |   |-- persistence/            # canonical state、projection、index 同步
|   |   |   |-- dispatch/               # dispatch/watch/parent planner 报告
|   |   |   |-- runner_context_service.py # 执行上下文和边界文件
|   |   |   |-- runner_result_service.py # runner result 写回和副作用
|   |   |   |-- runtime_closeout.py    # runner 终态收口的可恢复 WAL + 一致终态重入 + 恢复扫描
|   |   |   |-- executor_recovery.py   # 执行器确证退出后的失败/未知副作用投影与父级通知
|   |   |   |-- board/                  # board、due-check、action-plan
|   |   |   |-- actions/                # action-plan 应用、取消/接管动作
|   |   |   |-- hierarchy/              # 多层调度和恢复包
|   |   |   |-- patch_apply/            # patch review/apply/report
|   |   |   |-- capability_service.py   # 能力请求、grant、gap、路由
|   |   |   `-- memory_candidates.py   # 子代理 lessons/findings 只经父级写入 owner 唯一候选账本
|   |   |-- patch/                     # patch review/apply 底层实现
|   |   |-- execution/                 # 测试执行和记录
|   |   `-- static_site/               # 静态站点检查
|   |-- user_space/                    # owner home、task workspace、policy、可选 quota、doctor、自动 retention
|   |   |-- owner_access.py            # 完整代理与冷管理共用的 owner 目录墙及权限裁决
|   |   |-- approval_mode.py           # owner 显式审批模式与权限快照映射，子代理同源读取
|   |   |-- owner_quota.py             # 显式非零磁盘上限的跨进程配额锁；0 时退出热路径
|   |   |-- home_retention.py          # 结构化终态/时间清理、二次校验、trash tombstone 与 legal hold
|   |   `-- owner_maintenance.py       # owner 维护间隔、状态记录与自动执行控制
|   |-- memory_store/                  # owner 长期事实、候选、每日经历、策展、晋升与维护的唯一主链
|   |   |-- candidate_models.py       # Candidate v2 Schema、来源/scope/状态枚举与稳定 ID
|   |   |-- candidates.py             # owner candidates.jsonl 唯一候选账本和唯一状态机
|   |   |-- curator.py                # 后台策展统一 Service、reason、lease、增量 cursor 与有界重试
|   |   |-- curator_backend.py        # 无工具辅助模型调用适配；只返回严格结构化结果
|   |   |-- curator_commit.py         # Daily/Candidate/state/run audit 整批提交与崩溃恢复
|   |   |-- curator_*.py              # Curator 输入、Schema、正式记忆快照、状态与运行审计辅助模块
|   |   |-- daily.py                  # v2 DailyMemoryEvent 稳定序列、幂等合并与按天账本
|   |   |-- jsonl.py                  # 正式 long_term 稳定 ID CRUD/batch、冲突、hard delete 与召回
|   |   |-- lessons.py                # 正式 lesson/HOT 唯一正文、阈值验证与确定性 routing index
|   |   |-- lifecycle.py              # close/reset/task-complete/pre-compact 等统一策展请求入口
|   |   |-- migration.py              # Memory v1→v2 只读计划、完整备份、幂等迁移和失败回滚
|   |   |-- operations.py             # ops.jsonl 无正文正式 Memory 操作审计
|   |   |-- promotion.py              # Candidate 证据、冲突、Persona、lesson/HOT 的唯一晋升服务
|   |   |-- recall.py                 # active long-term/正式 lesson/HOT 的 owner+scope 安全召回
|   |   |-- retention.py              # 统一 Retention service facade
|   |   |-- retention_*.py            # 策略/计划/重验证/可恢复删除和 hard-delete 辅助模块
|   |   `-- security.py               # Memory/Persona 共用持久内容威胁扫描
|   |-- memory_api.py                  # CLI/外层包使用的公开 Memory façade；只转发正式 Service/DTO，不建第二权威
|   |-- memory_push.py                 # planner/runner 决策点只召回正式 Lesson/HOT，并复用唯一 memory-context 信封
|   |-- memory_archive/                # compact、audit、tool output artifact、task workspace refs
|   |-- local_storage/                 # SQLite/FTS/文件事实源；ledger_redaction.py 精确擦除已删事实但保留幂等身份
|   |-- runtime_db/                     # SQLite 运行事实源：Task 身份、TaskRun/AgentRun/Attempt 生命周期、wake 与投递账本
|   |   |-- run_creation.py             # 普通代理及宿主命令共用的同连接运行树创建
|   |   |-- host_commands.py            # 显式宿主请求与原始 pending 运行的唯一绑定及严格回读
|   |   |-- host_command_execution.py   # 原 pending 精确执行、原运行收口和不重跑的只读查询
|   |   |-- host_command_approval.py    # 同一宿主执行区间的精确审批与拒绝，不复用会话批准
|   |   |-- operation_resources.py      # 同次只读核对原操作、holder/代数/epoch 与已领取资源锁
|   |   |-- run_cancellation.py         # 精确 task/run/attempt 的共用取消权限合同，UNKNOWN 保留锁与恢复障碍
|   |   `-- executor_liveness.py        # exact attempt 执行区间和 OS 退出事实；慢模型不按时长判死
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |   |-- plugin_command_service.py  # 原管理员授权、可信 owner 目录与插件 HTTP 命令入口
|   |   |-- command_stream_protocol.py # 命令有界消息、规范 owner 握手与原审批路径
|   |   |-- command_stream.py          # 原 HTTP 线程执行、审批运输和断连取消
|   |   |-- owner_conversation_store.py # 模型配置与插件管理共用的原 owner 会话 Store 组装
|   |   |-- request_execution.py        # 单个已领取请求的租约、模型执行、超窗恢复与收尾编排
|   |   |-- request_context.py          # 原车道内的会话快照、Compact 与工作目录准备
|   |   |-- request_binding.py          # 精确请求与运行身份绑定、执行车道和原子更新
|   |   |-- request_history.py          # 公开正文、canonical 历史提交、去重与延迟补交
|   |   |-- request_prompt.py           # 已准备会话投影的模型输入渲染与历史种子
|   |   |-- stream_writer.py            # 请求级文本缓冲、typed 流事件和显示投影的有序出口
|   |   |-- stream_events.py            # 公开事件白名单、插话/重试投影和原思考归档载荷
|   |   |-- stream_approval.py          # 精确作用域审批缓存绑定与真实客户端交互
|   |   |-- display_archive_service.py # 按可信 owner 与主子会话归属读取一页完整原文
|   |   |-- approval_mode_service.py   # /client/permissions 认证与冷 owner 控制入口
|   |   |-- main_activity.py            # 前台 typed chunk 到共用 main 数字/阶段的只读投影，不复制正文
|   |   |-- foreground_transcript.py    # 前台公开过程复用会话 mapper，候选与 canonical final 精确交接
|   |   |-- approval_session.py        # owner/thread/cwd/权限精确作用域的有界进程内工具审批缓存
|   |   |-- bounded_http_server.py     # 单 Gateway 固定 daemon worker、128 在途上限与过载 503 背压
|   |   |-- control_service.py         # owner/thread 持久根任务的即时状态、纠偏和中断
|   |   |-- control_operation_service.py # slash 控制副作用前置回执、幂等重放与 unknown 对账
|   |   |-- workspace_scope.py         # 普通请求和控制入口共用的宿主目录与 owner 权限校验
|   |   |-- input_delivery_service.py  # 普通消息 active/queued 去向的唯一持久回执与后台对账
|   |   |-- request_client.py          # 薄客户端 ask 载荷和不可变执行选项合同
|   |   |-- channel_health.py          # adapter PID/heartbeat/逐通道状态的 fail-closed 健康投影
|   |   |-- permission_bridge.py       # TUI/Gateway 工具审批 request binding 的原子决定文件桥
|   |   `-- goal_control_service.py    # 同 thread 持续目标的创建/修改/暂停/恢复/清除
|   |-- conversation/                  # 通道会话账本、权威 transcript、结构化任务关联/续接
|   |   |-- store.py                    # 同源领域组件组装、跨领域上下文与账本维护
|   |   |-- store_io.py                 # 无 Store 依赖的 JSONL 读取、错误报告、路径与文件归档原语
|   |   |-- store_layout.py             # 统一持久目录和路径、只读打开及扫描上下文组装
|   |   |-- store_threads.py            # 线程身份、通道绑定、默认模型解析与 Compact 原子更新
|   |   |-- store_messages.py           # 消息幂等追加、展示检查点及字节游标读取
|   |   |-- store_tasks.py              # 任务关联、活动索引、工作区状态投影及终态进度关闭
|   |   |-- store_audits.py             # Audit 准备、发布修订、终态重开与运行代提交
|   |   |-- store_guidance.py           # 插话入队、精确认领、权威回执查询与组件组装
|   |   |-- store_guidance_records.py   # 插话回执格式、迁移构造与身份校验
|   |   |-- store_guidance_ledger.py    # 插话回执读取、回合锁及队列与索引修复
|   |   |-- store_guidance_submission.py # 模型提交批次、执行前拒绝及回执投影修复
|   |   |-- store_guidance_acknowledgements.py # 模型消费确认批次与幂等消息投影
|   |   |-- store_guidance_recovery.py  # 插话终态结算、释放、失效改绑与网络重试准确预留
|   |   |-- store_index.py              # 有界扫描投影、惰性注册、文件指纹与权威读回
|   |   |-- store_usage.py              # 模型用量领域、累计增量去重及线程数字显示的原子更新
|   |   |-- store_claims.py             # 执行租约的领取、续租、精确终态、恢复归属与旧账归档
|   |   |-- store_goals.py              # 目标集合、内容版本 CAS、预算结算及共享时钟依赖
|   |   |-- store_observations.py       # 观察事件构造、追加、待处理扫描与原子确认
|   |   |-- store_wakes.py              # 唤醒发布/去重、观察链接、投递冻结与消费确认
|   |   |-- store_progress.py           # 进度策略、到期投影、失败退避落账与旧策略归档
|   |   |-- process_events.py           # 受管后台命令终态到原会话 wake 的去重交接
|   |   |-- compact_progress.py       # transcript/live-tool/turn-local Compact 来源与提交权的唯一公开进度协议
|   |   |-- compact_carry.py           # 前后台同片超窗重试的工具快照与插话携带纯计算
|   |   |-- context_usage.py          # 主/子 preflight 数字的 canonical thread 保存、代次检查与无正文展示
|   |   |-- model_metrics.py          # 主/子模型调用账与历史用量的只读显示投影，不回灌模型上下文
|   |   |-- agent_activity.py          # active task link + canonical child run 到 TUI/Web 共用有界活动投影
|   |   |-- agent_transcript.py        # 子代理跨进程公开过程事件的 owner 存储、游标和有界裁剪
|   |   |-- agent_control.py           # owner 树内代理详情、运行中 guidance 与精确停止的通道中立控制面
|   |   |-- agent_tool_approval.py     # main/child exact ToolApprovalRequest 的唯一耐久记录、consumer 租约与决定等待
|   |   |-- tool_approval_scope.py     # 从 canonical 任务、线程和 claim 读取审批归属，隔离主代理换轮
|   |   |-- background_transcript.py  # 后台 main/child 的有界 typed 过程事件环与共用工具审批 sink
|   |   |-- background_history.py     # 后台完整展示块快照，随 canonical final 保存并用于恢复重基
|   |   |-- display_checkpoint.py     # canonical逐块公开过程校验/写入、恢复未知占位与显示去重
|   |   |-- display_archive.py        # 不可变显示原文、无路径引用与有界页文件，不进入模型上下文
|   |   |-- auxiliary_model_call.py   # 会话辅助模型调用的统一账、退避、并发闸；独立压缩用量持久结算
|   |   |-- tool_context_window.py    # text/native 共用的有界工具历史窗口与稳定前缀投影
|   |   |-- tool_input_progress.py     # provider 大工具参数生成期的脱敏临时展示合同
|   |   |-- agent_thread.py            # child/grandchild 独立 thread、逐 attempt transcript 与统一 Compact 适配
|   |   |-- agent_thread_store.py      # agent thread 精确 ID 物化、身份冲突与运行目录校验
|   |   |-- closeout.py                # 收口状态机 decide_closeout(四改之 2): 终态 done/cancelled/wait_human/wait_handoff/resume_round
|   |   |-- compact.py                  # 唯一 thread compact：候选验证、一次 CAS 提交与近期 raw tail
|   |   |-- compact_provider_surface.py # transcript Compact 复用普通轮 stable prompt/system/tools/messages 的缓存面
|   |   |-- compact_request_budget.py   # 按当前模型窗口顺序分段摘要，完整覆盖历史且失败不推进游标
|   |   |-- compact_tool_refs.py        # 从匹配原生工具往返保留原样路径线索，不靠模型摘要记忆目录
|   |   |-- compact_guard.py            # 结构化完整回合选择、连续失败冷却与 typed compact 错误
|   |   |-- compact_checkpoint.py       # owner-scoped 完整 compact 恢复点与代际引用
|   |   |-- active_turn_compact.py      # 跨工作片工具 archive 到同一 checkpoint/CAS 的恢复压缩与模型投影
|   |   |-- live_tool_compact.py        # 运行中原生工具历史到同一 thread checkpoint/CAS 的适配层
|   |   |-- native_history.py           # 完成回合的 provider 原生消息信封、校验与按请求替换式恢复
|   |   |-- history_projection.py       # 前后台共用完整历史行选择、范围过滤和原生 metadata 保留
|   |   |-- history_display.py          # 从 canonical 消息投影只读恢复事件，不把问答预览代替正文
|   |   |-- history_page.py             # canonical 字节边界向前分页和完整工作片分组
|   |   |-- message_stream.py           # 同账本正文与显式协商的过程检查点投影，共用 ID/字节游标
|   |   |-- task_runtime_state.py      # 后台续轮读取精确任务进度的结构化运行事实
|   |   |-- runtime.py                  # 后台主代理调度热循环：wake_queue 到期消费、三源对账(5min)、事件提前醒取消闹钟
|   |   |-- background_progress_policy.py # 无副作用的进度策略计数与确定性失败退避
|   |   |-- background_supply_backoff.py  # 会话供应冷却的进程内状态、消费守卫及恢复日志
|   |   |-- background_goal.py          # 后台 Goal 异常结算与续跑裁决，只持有精确领域和回调能力
|   |   |-- background_routing.py       # 线程及 owner 投递地址的只读选择，不消费来源或执行发送
|   |   |-- background_claim.py         # 后台执行领取、最终准入、共享心跳与精确 claim 结算
|   |   |-- background_recovery.py      # 按次查询权威恢复阻断，只去重日志、不缓存执行资格
|   |   |-- background_tool_policy.py   # 无副作用的后台工具目录、owner/task 收紧及展示投影
|   |   |-- background_context.py       # 后台有界上下文、任务范围与模型可见事实准备
|   |   |-- background_history_seed.py  # 后台原生历史种子、任务隔离与读取失败合同
|   |   |-- background_execution.py     # 后台单片执行、Compact 重试、原生历史保存与具名结果
|   |   |-- background_delivery.py      # 后台投递、canonical 回复提交、整封冻结与耐久去重
|   |   |-- control_commands.py        # CLI/IM 共用 typed slash dispatcher、task command 与状态渲染
|   |   |-- task_resources.py          # 从正式主链绑定关闭权限并组合固定子树清单，清理不再重新查树
|   |   |-- local_run_control.py       # direct 单次调用的身份发布与中断门，沿原任务锁冻结主资源
|   |   |-- goal_tools.py              # 默认可见的持续目标创建、读取与精确收口
|   |   |-- goal_binding.py            # 当前代理及直属下级的精确目标身份解析，拒绝借用父目标
|   |   |-- goal_delegation.py         # 显式子目标初始化、正常轮续接及授权停止/恢复的状态同步
|   |   |-- goal_editing.py            # 目标草稿版本比较保存，保持身份、状态、用量并投递引导
|   |   |-- goal_control.py            # 复用用户会话树授权的主子目标完整读取和编辑 API
|   |   |-- goal_recovery.py           # 用户显式恢复时修复旧目标共享任务冲突，保留源记录与任务现场
|   |   |-- goal_clock.py              # 同一会话存储的前后台共享单调时钟，避免重复累计目标耗时
|   |   |-- authority.py               # 标记会话 transcript 为当前多轮对话唯一事实源
|   |   `-- task_promotion.py          # 任务工具触发提升、完成/中断候选选择与关闭
|   |-- scheduler/                     # owner 持久 at/every/cron、CAS job/run 账本、claim/heartbeat 与同 thread 唤醒
|   |-- verification/                  # owner 被动验证事件、targeted/full 投影与文件写后 stale
|   |   |-- runtime.py                 # 公共工具出口读取结构化执行结果，记录验证或文件修改
|   |   |-- repository.py              # 按 owner/thread/task 保存事件和过期状态，不升级验证范围
|   |   `-- project_facts.py           # 从项目显式声明识别验证命令，不按报告文字猜测已验证
|   |-- delivery/                      # 多 IM 统一投递：registry、可信 context、reply envelope、receipt
|   |   |-- registry.py                # adapter/配置/健康/绑定/capabilities/target validator 唯一注册表
|   |   `-- service.py                 # 普通回复、主动消息、原生附件的统一发送出口
|   |-- adapter/
|   |   |-- delivery.py                # 通道 input_receipt/request_result 三态回送、CAS 与重启去重
|   |   `-- ingress.py                 # POST 前 durable ingress、冲突隔离与单线程全链恢复
|   |-- settings/                      # AgentConfig、加载、来源账本、runtime scope config
|   |   |-- user_config_capability.py  # 用户可自助修改配置的唯一白名单/校验/生效时机；安全边界结构性拒绝
|   |   |-- model_profiles.py           # owner 私有模型配置唯一文件源、脱敏列表及子代理创建时引用
|   |   |-- model_oauth.py              # owner 登录代次、取消与刷新并发控制，复用私有配置源
|   |   |-- model_oauth_schema.py       # 授权配置、凭据目的地、私有状态与绑定校验
|   |   |-- model_oauth_wire.py         # 设备码、兑换和刷新协议的有界无重定向 HTTP
|   |   |-- thread_model_selection.py   # canonical 会话模型编号、默认初始化与逐工作片解析
|   |   |-- shared_model_catalog.py     # 管理员显式共享引用目录，不复制私有连接凭证
|   |   |-- model_provider_schema.py    # provider/model v2 校验、v1 显式迁移与单份连接快照解析
|   |   |-- model_provider_operations.py # 锁内服务商/模型管理，密钥保留与显式清除
|   |   |-- model_provider_network.py   # 用户主动目录 GET/短问候，不执行工具或创建任务
|   |   |-- model_scope.py              # 主工作片冻结 config/backend/prompts，切换不热改在途执行
|   |-- common/                        # 跨域小权威：safe_id、path_normalize、json_io、日志脱敏、结构化输出批处理
|   |   |-- directory_lock.py           # 原后台与安装 Store 共用的永久目录系统锁，不降级为仅线程互斥
|   |   |-- nofollow_fs.py              # 受信根内的文本/二进制读写及锁文件打开，拒绝链接路径
|   |   |-- nofollow_tree.py            # 固定目录树递归删除，拒绝顶层链接且不沿树内链接越界
|   |   |-- strict_json.py              # 包与安装表共用的唯一键、有限数及严格 UTF-8 JSON 读取
|   |   |-- text_file_window.py          # 普通/按行/字符读取共用的有界编码索引、seek 游标和版本失效
|   |   |-- file_version.py              # 观察版本与显式写前置条件；不建立任务锁或冒充内核 CAS
|   |   |-- audit_activation.py        # 显式 `/audit` 前缀 -> guarantee/window 结构化激活
|   |   `-- tool_output_paths.py       # Memory/工具共用的 owner/task 输出归档与索引路径权威
|   |-- concurrency/                   # 重试/退避（jittered backoff）、锁、per-thread 协作中断
|   |-- owner_object_store.py          # scale owner PG/RLS manifest + versioned S3，Pod 盘只作缓存
|   |-- scale_runtime.py               # scale role/release channel/S3 配置 fail-closed
|   |-- continuous_monitor_entry.py    # 真实 wall-clock 异构来源 proof 长守入口
|   |-- contracts/                     # 安全/协议/错误分类与格式校验合同；无旧任务质量 acceptance 判官
|   |   |-- subagent_completion.py     # Gateway/后台续片共用的中立 child 完成信封与直属结果投影
|   |   |-- tool_approval.py           # 工具审批 request/decision/binding 与跨层调用身份协议
|   |   `-- tool_input_schema.py       # 工具参数有限 JSON Schema 纠正/完整校验与脱敏问题路径
|   |-- tooling/                       # 唯一 ToolRuntime/ActionPolicy/ToolExecutor、写入边界与结果投影
|   |   |-- mcp_client.py             # MCP 配置、握手、当前连接及永久关闭；目录发布核对同一连接
|   |   |-- mcp_transport.py          # 固定进程与出生身份、请求队列、读写线程和原进程树清理
|   |   |-- mcp_managed_process.py    # 插件 MCP 接原托管管道、资源锁准入和原生清理回执
|   |   |-- mcp_protocol.py           # 每连接独立响应箱、有界诊断、期限与结构化协议错误
|   |   |-- plugin_registration.py    # 新运行中同步原插件连接，各权限视图分别投影已验工具
|   |   |-- process_output_capture.py   # stdout/stderr 有界保留与持续排空，公开截断和完整性
|   |   |-- computer_use_profile.py   # MIT 开源桌面执行器的 local/main + Full Access MCP 薄装配与 effect 边界
|   |   |-- computer_use_server.py    # 用公开 MCP 接口组合上游桌面工具、滚轮及文本输入
|   |   |-- computer_text_input.py    # 按平台提交可靠文本，校验 Unicode 并保留后置验证边界
|   |   |-- models.py                 # ToolModelSpec、ToolRuntimePolicy、ToolRuntime/Snapshot 与 handler outcome
|   |   |-- runtime_contracts.py      # 唯一 canonical ToolCall/ToolResult、ToolChoice、协议与 operation 合同
|   |   |-- tool_search_state.py      # 主/子/Gateway 共用的纯工具发现归档投影，不导入执行循环
|   |   |-- input_schema.py           # 唯一 input_schema 规范化、强类型纠正和完整执行前校验
|   |   |-- action_policy.py          # 副作用前唯一 allow/ask/deny 聚合决策
|   |   |-- executor.py               # approval、sandbox、handler、账本、核对、持久化与投影状态机
|   |   |-- runtime_boundary.py       # task 相对路径归一与精确读边界检查
|   |   |-- workspace_read_scope.py   # 沿原 exact 与墙外授权生成本次 cwd 内的读取上界
|   |   |-- capabilities_tool.py      # 从真实工具目录与唯一 channel registry 投影模型能力
|   |   |-- _filesystem_display.py   # 文件工具共用的有界 diff/write 富终端展示事实构造器
|   |   |-- _persona_write_guard.py   # SOUL/USER/AGENTS 统一强制走 update_persona
|   |   |-- background_process_launch.py # v3 预留、原权威复查及 v4 日志/stdio 启动交接
|   |   |-- background_process_host.py # 独立绑定 child、直接继承管道、检查寿命并提交真实终态
|   |   |-- process_scope.py          # 访问、任务执行与共享激活的独立身份合同
|   |   |-- process_registry.py       # 受保护记录的进程缓存、水合、PID 身份核对与完整后代树终止
|   |   |-- process_session_store.py  # 原 session 地址的互斥读写、版本 CAS、精确停止清单与裁剪
|   |   |-- process_cleanup_evidence.py # 原退出证明与固定身份摘要，原锁下消费前核验
|   |   |-- process_session_records.py # v1/v2 原版本读取及 v3 任务/激活归属、实例与单调状态合同
|   |   |-- process_session_commit.py # 原版本 v2/v3 批次的 redo 预检、安装恢复及提交回执
|   |   |-- process_session_cleanup.py # 冻结实例的精确清理、原账完整退出证明与提交异常回执
|   |   |-- process_resource_stop.py   # 主控制冻结后台清单和 PTY 请求，锁外清理只消费原回执并保留部分错误
|   |   |-- process_network_status.py # exact 受管进程树监听、防火墙显式规则与外部探针边界的只读投影
|   |   |-- process_sessions.py       # owner+TUI 会话隔离的后台命令查询、等待与停止工具
|   |   |-- gateway_status.py         # 本机管理员读取唯一 Gateway 身份、端点、队列和本生命周期日志摘要
|   |   |-- user_config_tool.py        # main_agent 专用：读生效值/来源，写白名单项并报告生效时机
|   |   |-- shell.py                  # 非交互 run_command、独立 stdin、超时/中断与有界 pipe drain
|   |   |-- shell_syntax.py           # 外层及字面 Shell -c 的后台语法检查，不解释普通字符串或 heredoc 正文
|   |   |-- tool_input_completion.py # 明示安全默认值、可信上下文补参与脱敏 source/source_ref
|   |   `-- sandbox.py                # bwrap 唯一策略、自检、worker/K8s readiness 硬门
|   |-- capability/                    # 单一 SkillsService、逐轮 snapshot、能力路由与 capability tools
|   |   |-- skill_service.py           # bounded builtin/shared/owner/workspace discovery、policy 与缓存
|   |   |-- skill_snapshot.py          # 不可变稳定引用、正文 hash/guard 校验与子代理收窄
|   |   |-- persona_repository.py      # owner SOUL/USER/AGENTS 受控加载、版本/CAS/回滚唯一入口
|   |   `-- channel_message_tool.py    # 当前 owner 的统一 send_message；登记产物经原生通道发送
|   |-- prompting_parts/               # prompt 构造
|   |   |-- builder.py                 # 完整 prompt 与 native 三段追加式缓存布局构造
|   |   |-- cache_layout.py            # typed 稳定 system/user、动态尾部与完整字符串投影
|   |   `-- memory_context.py          # 非权威、可转义且可统一剥离的召回记忆信封
|   |-- scale_downstream.py            # scale worker 复用普通 gateway 会话执行主链
|   `-- backends/                      # 模型后端适配、run 固定协议/tool_choice、原生工具历史与结构化生成
|       |-- oauth.py                   # 原三种模型协议的 OAuth 认证层，不新建执行循环
|       |-- oauth_transport.py         # 带凭据请求的重定向拒绝，登录和模型共用
|       |-- provider_headers.py        # 自定义头保护、owner/thread 稳定会话头及 endpoint 拼接
|       |-- request_scope.py           # 前台模型端点占用与后台单次预算；不保存正文和持久状态
|       |-- cache_diagnostics.py       # 出站请求摘要及前缀变化诊断，不存正文或改变缓存布局
|       |-- sampling.py                # top_p 校验与精确端点 V4 Flash 采样默认，不改变身份或重试
|       |-- responses.py               # Responses 协议生成入口，复用正式 HTTP/取消/超时主链
|       |-- responses_wire.py          # typed SSE/items 与既有工具历史映射、加密 reasoning 回放
|       |-- anthropic_prompt_cache.py  # Anthropic tools/system/最新 history 断点与追加式 user 投影
|       |-- base.py                    # 模型响应、冻结选项和公共后端接口；本地 echo/缺配置实现
|       |-- http.py                    # HTTP 传输、请求局部控制、工具探针与模型目录读取
|       |-- openai_chat.py             # Chat Completions 请求、原生历史、思考和流式结果转换
|       |-- anthropic.py               # Messages 请求对象、缓存布局、思考及工具结果转换
|       |-- factory.py                 # 显式配置构造唯一后端，缺配置判据与调度共享
|       `-- tool_protocol_adapter.py   # native 事件或显式完整 text 帧到 canonical ToolCall 的唯一适配口
|-- tests/                             # 单元、集成、真实链路回归
|   |-- test_subagent_process_control.py # 公共进程树终止覆盖后代、升级、宿主保留及未确认回执
|   |-- test_subagent_resource_stop.py  # 固定原子树、终态资源、恢复隔离及 Goal/creation 锁序
|   |-- test_runner_stop_relay.py       # 独立 Python 宿主心跳转交精确取消、共享隔离及旧配置投影
|   |-- test_runtime_module_boundaries.py # 公共后端合同和纯策略不加载执行器/HTTP 的导入边界回归
|   |-- test_computer_text_input.py     # 文本事件 UTF-16、显式替换与不支持字符零副作用回归
|   |-- test_subagent_activity_diagnostics.py # 阶段提醒、慢流不误杀、执行代与消息去重回归
|   |-- test_r223_audit_regressions.py   # 外部审计的编码、版本、并发、MCP、输出、网络和恢复故障注入
|   |-- test_timeout_recovery_delivery.py  # 门槛5 探针的无损交付：两枪合法正文按序保留、归属同一轮、截断/预算/零工具轮语义不变
|   |-- test_store_scan_indexes.py      # wake/观察/策略读取侧索引：条目+记录双预算有界、删名清理、目录不可读不误清、枚举与 glob 同口径
|   |-- test_tui_injected_input_states.py  # 插话三段状态(排队/已提交/已确认)：提交边界不消费、已提交即入历史、慢流/失败/重连不丢不重
|   |-- test_slow_model_liveness.py     # 慢模型长任务活性：流式不按总时长判死、客户端只按机器活动续期、租约心跳与长工具续租
|   |-- test_background_claim_execution.py # 后台领取后竞态、异常收尾顺序及运行中真实续租
|   |-- test_background_recovery.py     # 恢复阻断每次重读、不可读判据与日志去重边界
|   |-- test_runtime_db_stable_mutation_generation.py # 正常换代、活动接管和升级调和保留已确认资源
|   |-- test_host_command_registration.py # 并发请求唯一登记、输入冲突、事务回滚与原链损坏检查
|   |-- test_host_command_operation_replay.py # 原执行器终态回读、身份隔离及线程退出 UNKNOWN 保留
|   |-- test_host_command_execution.py # 同请求并发、规范输入守门、终态收口故障及只读查询
|   |-- test_host_command_approval.py  # 明确批准、拒绝、取消、迟到决定与并发重复请求
|   |-- test_host_command_stream.py    # 临时 HTTP/MCP 与 TUI 审批往返、身份映射和取消隔离
|   |-- test_host_command_resource_reference.py # 原操作反查、坏链拒绝及资源集合回读
|   |-- test_gateway_admission_wait.py  # 合法排队等准入的结构化等待信号：只写等待事实、有节流与总预算、客户端持续收到且停写/取消/终态收口
|   |-- test_scheduler_scan_costs.py    # waiting 投影缓存三重校验、runtime_snapshot 锁外解析与旧实现逐字一致、owner 事实缓存失效回归
|   |-- fixtures/tui/                   # 固定尺寸/时间线的非敏感 TUI PTY 动作 fixture
|   |-- test_adapter_ingress.py         # adapter POST 前落盘、幂等/隔离、响应丢失与崩溃恢复回归
|   |-- test_agent_transcript.py        # 子代理公开过程事件的增量游标、隔离和有界裁剪回归
|   |-- test_chat_prompt_queue.py       # canonical chat Queue 精确回取、FIFO 与 unfinished-task 对账
|   |-- test_owner_home_workspace.py    # 主/子代理家目录范围、跨 owner 拒绝、运行记录分离与软整理指南
|   |-- test_shell_stdin.py             # 普通/受控/attempt 命令不抢读宿主 stdin，显式管道仍可传入
|   |-- test_tui_ansi_snapshot.py       # ANSI offset 重放、样式/背景、Unicode、resize 和坏账 fail-closed 回归
|   |-- test_tui_agent_navigation.py    # 子代理选中/进入/返回、详情过程、只读终态与 footer 回归
|   |-- test_agent_goals.py            # 单代理单 Goal、主子隔离、版本冲突、停止和同执行轮持续工作回归
|   |-- test_tui_events.py              # TUI event 信封、sequencer、cursor、重复/冲突/乱序与有界重放
|   |-- test_tui_markdown.py            # CommonMark 标题/列表/引用/代码/表格、样式角色与 Unicode 宽度换行
|   |-- test_tui_runtime.py             # 本地/Gateway 流式、工具、queue、终态和全局事件顺序 adapter 回归
|   |-- test_tui_view.py                # UIContent formatted lines、frame/block cache、follow anchor 和 resize 重排
|   |-- test_tui_history_paging.py      # 更早页渲染顺序、锚点、冻结页、子页/过期响应和单在途读取
|   |-- test_conversation_history_paging.py # 中文长行字节边界、完整工作片、坏游标与追加竞态
|   |-- test_tui_input.py               # slash/path 补全、菜单选择、queue 回取与 bracketed paste 输入回归
|   |-- test_plugin_command_catalog.py # 声明跨进程往返、版本变化及损坏载荷拒绝
|   |-- test_plugin_package.py         # 静态包篡改、归档预算、危险成员及不执行代码的合同检查
|   |-- plugin_wheel_fixtures.py       # 合成标准 wheel 与导入陷阱，仅用于开发组件检查
|   |-- test_plugin_wheels.py          # 固定依赖、extras、平台、摘要与归档预算检查
|   |-- test_plugin_wheel_layout.py    # 跨 wheel 与引导文件冲突、安装布局和入口脚本检查
|   |-- test_plugin_environment.py     # 临时真实 venv/pip、导入陷阱、原配额及候选排他检查
|   |-- test_plugin_environment_process.py # 托管准备取消、正常退出交错与未知状态替身
|   |-- test_plugin_preparation_operation.py # 原 claim、资源锁故障与完整临时宿主执行回读
|   |-- plugin_environment_fixtures.py  # 临时 RuntimeDB 原操作与计划的开发测试组装
|   |-- test_plugin_install_store.py   # 默认停用、原请求重放、失败裁决与独立进程安装竞争
|   |-- test_plugin_configuration.py   # 私有配置 CAS、显式迁移、矛盾状态和提交异常裁决
|   |-- test_plugin_activation.py      # 激活/撤销竞争、配额隔离、跨进程 CAS、迁移及坏记录拒绝
|   |-- test_plugin_activation_ref.py  # 规范用户引用、独立进程复查和 host 创建前撤销拒绝
|   |-- test_plugin_mcp_transport.py   # 托管协议、队列撤销、调用隔离和原操作账结果集成
|   |-- plugin_deactivation_fixtures.py # 原执行器内的假启用夹具与真实两类资源，精确 finally 清理
|   |-- test_plugin_deactivation.py   # 管理停用、原请求重放、准备取消、权限和未知清理保留
|   |-- test_plugin_deactivation_races.py # 阻塞业务停用、另一插件/任务隔离与独立 host 创建交错
|   |-- plugin_activation_fixtures.py # 实际临时 wheel/MCP、原管理链和业务工具执行夹具
|   |-- test_plugin_enable.py         # 实际启用、坏目录、原执行器调用与旧快照停用验证
|   |-- test_plugin_release.py        # 原 handler 退出、环境删除、结果落账与重送消费边界
|   |-- test_plugin_removal.py        # 管理卸载、权限、旧请求重放、准备未退与持久成功后包回收
|   |-- test_plugin_removal_store.py  # 安装删除 CAS、提交故障、符号链接与并发重新安装隔离
|   |-- test_plugin_registry.py       # 共享视图、可信 owner 注入、关闭登记交错与未知保留
|   |-- test_process_cleanup_evidence.py # 完整清理证明、自然终态保持、单调合并与 redo 恢复
|   |-- test_process_cleanup_consumption.py # 精确引用消费、部分删除、同 ID 换代及原记录保留
|   |-- test_nofollow_tree.py          # 环境目录与父链链接拒绝、树内链接不越界
|   |-- test_directory_lock_wait.py   # 原线程/系统目录锁等待的取消与释放验证
|   |-- test_plugin_configure_management.py # 原配置执行链、值不外泄、过期请求与 UNKNOWN 重放
|   |-- test_plugin_management.py      # 真实原执行链、来源消失、配置关闭、路径权限和配额检查
|   |-- test_plugin_invocation.py      # 原生 MCP 显式调用、单次审批、撤销交错、取消及原结果重放
|   |-- test_nofollow_binary_io.py      # 二进制预算、私有原子写入、锁链接和 portable 创建竞争
|   |-- test_gateway_plugin_commands.py # 冷 owner、可信身份、HTTP 插件命令分流与过期拒绝
|   |-- test_gateway_plugin_management.py # 原认证管理角色、冷用户拒绝及安装结果回读
|   |-- test_plugin_command_client.py  # 三模式、异步响应隔离、原候选版本及显式 Tab 读取
|   |-- test_tui_plugin_directory_pipe.py # 完整键盘链的 Tab 读取、候选接受、暂存恢复和原版本提交
|   |-- test_tui_interaction.py         # stash、Ctrl-R、help 与 paste refs 状态机回归
|   |-- test_tui_paste.py               # 大小 paste 的折叠/展开和占位符安全回归
|   |-- test_tui_preflight.py           # Gateway readiness 瞬态成功、typed 失败与 worker 只启动一次回归
|   |-- test_tui_terminal.py            # OSC 标题、活动动画、去重与清理回归
|   |-- test_tui_transcript.py          # 详细 transcript、全文搜索、命中导航和 resize 回归
|   |-- test_tui_renderer.py            # 欢迎/消息/spinner/tool/permission/queue/help 的固定时钟 golden
|   |-- test_tui_pty.py                 # PTY 动作时间线、ANSI 录制、resize、超时回收和 manifest 脱敏回归
|   |-- test_tui_reference_fixture_server.py # loopback Anthropic 参考场景协议与审计脱敏回归
|   |-- test_tui_view_model.py          # active→stable、工具权限、队列、状态和未知事件 fail-closed reducer 回归
|   |-- test_gateway_agent_control_service.py # owner 树内详情、幂等插话、停止和终态拒绝回归
|   |-- test_gateway_bounded_http_server.py # 固定 HTTP worker 复用、容量 503 与停机排队回收回归
|   |-- test_current_turn_execution.py # 当前轮成功/失败副作用事实投影回归
|   |-- test_cli_run_conversation.py   # CLI transcript、Memory 消息证据、任务链接与 workspace 收口回归
|   |-- test_closeout_machine.py      # 收口状态机 truth table 穷举测试(全组合+场景)
|   |-- test_wake_queue.py             # wake_queue 字条 CRUD、到期弹出、跨进程唤醒、对账与清理边界
|   |-- test_scheduler_wake_tick.py    # 调度器 tick 新契约: 到期字条→wake_queue_due、对账分频、EXEC-39 门、audit 跳过
|   |-- test_sleep_tool.py             # clock.sleep 参数边界、字条落盘、唤醒取消与错误码回归
|   |-- test_memory_hardening.py       # 来源证据、候选、并发去重、hard delete 与信封安全回归
|   |-- test_memory_candidate_daily_v2.py # Candidate/Daily v2 身份、状态、顺序、并发与大输出边界
|   |-- test_memory_curator_v2.py      # Curator 触发、模型配置、权限、失败恢复与整批提交
|   |-- test_provider_request_scope.py # 同端点前台优先、后台预算传递、取消和不重叠重试
|   |-- test_memory_promotion_v2.py    # 证据/冲突/Persona/lesson/HOT 晋升边界
|   |-- test_memory_recall_v2.py       # 正式来源、scope、陈旧索引、owner 隔离与信封安全
|   |-- test_memory_migration_v2.py    # v1→v2 dry-run、备份、回滚、幂等与坏数据关闭式失败
|   |-- test_memory_retention_v2.py    # 保留期、legal hold、终态保护、重验证与 hard delete
|   |-- test_memory_admin_cli_v2.py    # 统一 Memory 管理命令复用正式服务
|   |-- test_tool_input_completion_provenance.py # 有限补参、来源账目、伪造拒绝和旧旁路删除回归
|   |-- test_tool_input_schema.py      # 强类型纠正、嵌套/组合/边界规则与显式 Schema fail-closed
|   |-- test_process_sessions.py       # 后台命令有界等待、进程树停止与 owner/TUI 会话隔离回归
|   |-- test_process_activation_scope.py # 共享激活归属、旧 v2 恢复、任务隔离及退出证据保留
|   |-- test_background_stdio.py       # 实际字节管道、EOF、交接失败及精确资源隔离组件验证
|   |-- test_process_completion_events.py # 后台完成通知的重启补发、去重和停止边界
|   |-- test_cache_diagnostics.py       # 请求前缀诊断、无正文存储与线程隔离
|   |-- test_gateway_status_tool.py    # Gateway 权威身份、端点与生命周期日志诊断回归
|   |-- test_sandbox.py                # bwrap argv、自检协议、owner-scoped fail-closed
|   |-- test_container_install.py      # 假 runtime 验证一键 build/probe/透明包装器
|   `-- test_check_clean_package.py    # untracked、运行目录和 tar/wheel 制品门
scripts/
|-- bench/                             # GW-03/慢模型配对基准：锁内解析成本、owner 事实缓存各路径（配对交替，比值只在组内）
|-- live_lab/                          # 真实链路 harness；真实 preflight、main-artifact、tool-recovery
|-- tui_ansi_snapshot.py               # pyte 开发工具：从 raw ANSI/offset 账还原文本、样式、光标和标题快照
|-- tui_reference_fixture_server.py    # loopback 确定性 Anthropic 服务：驱动 TUI Markdown/思考/权限/错误黑盒场景
|-- tui_pty_recorder.py                # argv-only PTY 黑盒录制器：按键/粘贴/resize、原始 ANSI、事件索引与脱敏 manifest
`-- check_clean_package.py             # 工作树与真实发布制品的结构化干净度检查
`-- reproject_model_usage.py           # 历史用量账本的只读重算投影（exact/partial/incomplete，不覆盖原账本）
deploy/
|-- Dockerfile                         # 内置系统 bubblewrap+tini，构建期 binary probe
|-- seccomp-bwrap.json                 # 固定 Moby 默认 profile，仅放行 bwrap namespace/mount 系统调用
|-- seccomp-bwrap.PROVENANCE.md        # 上游 commit、许可、本地增量和升级验收说明
`-- k8s/                              # stable/canary、Gateway route、migration、monitor 与 DR 清单
docs/
|-- PRODUCT_FACTS.md                    # 当前功能与边界说明；发布风险统一指向 STATUS
|-- audits/r223-report/                 # R223 中文只读 HTML 报告的独立公开目录，不放配置或任务产物
|-- design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md # child→owner 具体工具审批的身份、租约、FIFO 与失败语义
|-- design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md # 后台命令 host 所有权、跨进程记录与安全回收设计
|-- design/TUI_DESIGN.md # 终端交互 TUI Python 原生复刻的用户行为、事件架构与验收规格
|-- design/TUI_BEHAVIOR_CHECKLIST.md # 启动、消息、输入、权限、生命周期和命令映射逐项证据账
|-- tasks/completed/TASK-20260818-终端交互-tui-parity.md # 已完成 TUI 复刻实施、测试机边界和验收记录
|-- tasks/REFACTOR_PLUGIN_GOAL.md       # 同版发布部署、十步重构状态与逐步多 TUI 验收
|-- design/FEATURE-20260804-tool-runtime-unification.md # 工具唯一主链的用户行为、需求与验收规格
|-- design/tool-runtime-unification.md  # 工具参考证据、架构、迁移删除表与并行边界
|-- design/LONG_RUNNING_EXECUTION.md    # 慢模型、后台长等待与缓存诊断统一合同及验收矩阵
|-- design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md # 灰度/灾备/Owner store/24h proof 事实
|-- architecture/BOUNDARY_RULES.md      # 分层和写入边界
|-- architecture/MODEL_CATALOG_SNAPSHOT.md # 模型目录、配置覆盖及元数据维护边界
|-- architecture/MODULE_OWNERSHIP.md    # 当前模块归属
|-- architecture/MY_AGENT_HOME_LAYOUT.md# owner home 布局
|-- modules/                            # 分模块结构和进展
`-- development/                        # 开发规则、review、写文件规则
```

## Current Storage Roots

### 关键文件说明

- `agent_py_agent/agent/runtime_db/run_cancellation.py`：在原 RuntimeDB 上核对 task/run/agent run/attempt 四个身份并关闭执行权；原 UNKNOWN 不恢复、不释放锁，旧控制不能追随新的执行轮。
- `agent_py_agent/agent/runtime_db/run_creation.py`：在调用方原事务内创建 Task→TaskRun→AgentRun→首次 Attempt 及委托/事件，普通调用与显式宿主命令共用，不能另开事务。
- `agent_py_agent/agent/runtime_db/host_commands.py`：原事件索引与 TaskRun 冻结请求共同绑定唯一运行；本身不授予权限、不启动模型或任务调度。
- `agent_py_agent/agent/runtime_db/host_command_execution.py`：只领取原 pending，强制原操作 Store，终态沿原运行收口；查询不初始化数据库，UNKNOWN 不重跑。
- `agent_py_agent/agent/runtime_db/host_command_approval.py`：只在原执行区间等待精确批准，回到原 ToolExecutor 重新核验；拒绝、取消或无消费者都不启动 handler。
- `docs/design/HOST_COMMAND_EXECUTION.md`：显式宿主请求的登记、终态只读重放、UNKNOWN 和现有执行器接线边界。
- `agent_py_agent/agent/conversation/task_resources.py`：主链身份适配与整任务固定清单；热请求带正式绑定，无热请求才读取唯一主链，组合原子树后锁外清理，不重新选择资源。
- `agent_py_agent/agent/subagents/cancellation.py`：原创建事务内选择后代、关闭原权限并冻结资源；模型和用户控制共用，业务终态与资源退出分别保留。
- `agent_py_agent/agent/subagents/cancellation_hosts.py`：精确 attempt 中断及冻结宿主身份的只读观察；PID/launch 不构成整棵进程树的取消许可。
- `agent_py_agent/agent/subagents/runner_control.py`：原执行轮持久取消的唯一读取判据；session 心跳与模型前复核共用，不创建第二份状态。
- `agent_py_agent/agent/conversation/local_run_control.py`：plain/TUI worker 每条消息的临时控制句柄；复用运行绑定和任务晋升确认接口，执行权仍归 RuntimeDB，旧句柄不覆盖下一条消息。
- `agent_py_agent/agent/tooling/process_resource_stop.py`：固定后台停止回执与 PTY 请求分开记录；提交后的 PTY 失败不丢清单，清理不重新扫任务，也不把异步请求当成全部退出。
- `agent_py_agent/agent/tooling/process_scope.py`：后台访问、PTY/任务执行及共享激活的身份类型；不从访问或工作目录补业务身份，激活引用不授予执行权，不持有资源或执行取消。
- `agent_py_agent/agent/tooling/process_session_store.py`：受保护进程记录的统一入口，读写与裁剪先恢复同一目录的未完成提交；持锁事务提供启动检查点和精确停止意图，实际进程信号仍由调用方负责。
- `agent_py_agent/agent/tooling/background_process_launch.py`：启动方先预留再交接；stdio 三路端点直接继承给 child，失败关闭未交出管道，host 保持原寿命和精确停止控制。
- `agent_py_agent/agent/tooling/process_session_cleanup.py`：只清理原 Store 冻结的 host/child 出生实例；完整证明保存到原 termination.cleanup，原命令终态不改写，未确认及待恢复回执保留。
- `agent_py_agent/agent/tooling/process_session_records.py`：纯数据校验与单调合并；`process_session_commit.py` 只安装固定记录，目录互斥归公共 `common/directory_lock.py`，原锁名不变。v1 不隐式升级或获得任务停止授权。
- `agent_py_agent/agent/command_catalog.py`：无 UI/执行依赖的公共命令声明；原控制参数仍归会话模块，插件后缀识别不等于身份校验或可执行授权。
- `agent_py_agent/agent/command_arguments.py` 与 `command_binding.py`：参数定义、字面词法及值绑定的权威实现；部分输入也使用同一协议，帮助不从展示文字反推规则。
- `agent_py_agent/agent/plugin_commands.py`：接收宿主提供的动作描述，统一参数解析和静态错误；安装表与实际执行由管理服务沿原链处理。
- `agent_py_agent/agent/plugin_command_catalog.py`：冻结及校验完整管理/插件声明，内容摘要绑定 owner 视图、版本和激活引用；不提供权限凭证。
- `agent_py_agent/agent/command_declarations.py`：命令 JSON 的唯一读取器，包和宿主目录共用，旧目录私有 decoder 已删除。
- `agent_py_agent/agent/plugin_manifest.py` 与 `plugin_package.py`：只读校验包并保留同一字节快照；不接受宿主身份，不代表已安装、已授权或已隔离。
- `agent_py_agent/agent/plugin_wheels.py` 与 `plugin_wheel_layout.py`：标准元数据和固定依赖集合预检、环境内目标保护及安装后宿主读回；不运行插件或替代 pip 安装器。
- `agent_py_agent/agent/plugin_environment.py`、`plugin_environment_plan.py` 与 `plugin_environment_process.py`：计划先进入原 operation，准备沿原 owner 配额与 ProcessSessionStore；只返回准备事实，不发布激活或重建进程账。
- `agent_py_agent/agent/runtime_db/operation_resources.py`：精确资源准入的同事务只读检查，不能领取新代次、续租或恢复 UNKNOWN。
- `agent_py_agent/agent/plugin_installation.py` 与 `plugin_install_store.py`：原 owner 插件目录中的唯一安装表和最后回执；先存包再提交，默认停用，异常读回区分提交结果，不拥有管理权限或执行历史。
- `agent_py_agent/agent/plugin_installation_state.py` 与 `plugin_configuration.py`：原表 v3 编解码、v1/v2 明确迁移来源和完整配置替换裁决；未清理激活阻止改配置，没有第二套权威。
- `agent_py_agent/agent/plugin_activation.py` 与 `plugin_activation_record.py`：同一原计划的准备/发布/撤销 CAS，旧快照只读原代；撤销状态不证明资源退出。
- `agent_py_agent/agent/plugin_activation_ref.py`：只定位原安装表的可信引用；严格核对原 owner/root/scope，启动和发送不缓存授权。
- `docs/design/PLUGIN_ACTIVATION.md`：激活身份、持久撤销、释放/证据消费及重新启用；卸载、显式业务命令及实际多 TUI 仍待完成。
- `docs/design/MANAGED_PROCESS_STDIO.md`：原托管器的字节通道、v4 显式保留和激活归属，旧 v2/v3 原版本恢复边界。
- `agent_py_agent/agent/plugin_configure_tool.py` 与 `plugin_sources.py`：隐藏管理工具通过原执行链读取授权来源，配置值只进 owner 私有安装表；包与配置共用有界安全读取。
- `agent_py_agent/agent/plugin_install_tool.py` 与 `plugin_management.py`：管理服务核对原授权并走唯一执行器；安装默认停用，配置与启停同源，查询只读原请求。
- `agent_py_agent/agent/plugin_enable_tool.py`：在原管理操作中准备环境、完整验证候选目录，确认退出后才发布同代 active。
- `agent_py_agent/agent/plugin_runtime.py` 与 `tooling/plugin_registration.py`：固定激活的 MCP 适配和新运行组合；原客户端共享连接，权限视图单独生成目录，不新建激活缓存权威。
- `agent_py_agent/agent/workspace_read_context.py` 与 `tooling/workspace_read_scope.py`：纯读取协议和宿主组装分开，插件复用唯一 `path_access_policy.py` 裁决；范围为空明确拒绝，不从隔离进程环境补权限。
- `agent_py_agent/agent/plugin_invocation.py`：显式业务调用固定原选择，工具输入不混入管理字段；原审批/执行链之外只管理本次 MCP 连接的建立与准确关闭。
- `agent_py_agent/agent/plugin_deactivation.py` 与 `plugin_disable_tool.py`：先关闭原激活与准备任务权限，再冻结两类准确资源并锁外清理；保留原退出记录，不能将撤销等同清理成功。
- `agent_py_agent/agent/plugin_release.py` 与 `plugin_cleanup.py`：前者从原 enable 执行器和资源账核验退出，后者只在 disable/remove 原结果严格成功读回后消费固定引用，并回收无人引用的旧包；不改写 UNKNOWN，不重新选择当前代。
- `agent_py_agent/agent/plugin_removal.py` 与 `plugin_remove_tool.py`：原停用释放返回完整记录后，沿原锁 CAS 删除安装；删除回执只存原操作，不建墓碑，不删除用户产物。
- `agent_py_agent/agent/tooling/process_cleanup_evidence.py`：原资源记录的最小身份摘要及完整退出证明，旧引用不能删除同 ID 新实例；不另建持久状态。
- `agent_py_agent/agent/common/nofollow_tree.py`：使用已验证父目录描述符递归删除固定目录，不沿链接越界；调用方负责先确认原进程和执行器已退出。
- `agent_py_agent/agent/common/directory_lock.py`、`nofollow_fs.py` 与 `strict_json.py`：分别维护永久互斥、受信根文件原语和严格 JSON；这些公共原语不裁决领域授权或替代操作账本。
- `agent_py_agent/agent/plugin_command_service.py`：从原安装表生成静态命令目录，旧或缺失版本明确拒绝；显式业务动作尚未接执行，普通工具贡献归 Registry。
- `agent_py_agent/agent/gateway_parts/plugin_command_service.py`：三个 HTTP 入口共用原管理员与可信 owner；只读不初始化冷用户，获授权安装才登记原独立运行。
- `agent_py_agent/cli/chat_parts/plugin_command_client.py`：TUI、plain Gateway 与 direct 共用模式、声明缓存及查询语义；传输失败保留原编号与未知，不降级或自动重送。
- `agent_py_agent/cli/chat_parts/command_interaction.py`：每次 Enter 单独绑定审批回调、取消令牌和原 Gateway 连接；并发命令不共享可变回调或借聊天身份。
- `agent_py_agent/cli/chat_parts/plugin_command_stream.py`：网络持续读流，原面板独立等待；只在本连接有效时写回完整决定，退出关闭本命令等待。
- `agent_py_agent/agent/gateway_parts/command_stream_protocol.py`：校验原编号、完整帧和固定规范 owner；在可信服务根内派生隐藏审批地址，不从 HTTP 接收路径。
- `agent_py_agent/agent/gateway_parts/command_stream.py`：原请求线程调用一次原服务；心跳只检测断连，结果和资源退出仍由原执行链裁决。
- `agent_py_agent/cli/chat_parts/tui_plugin_commands.py`：保存首次接受候选的目录版本，参数补全不升级；网络提交沿共享分派在 UI 线程外执行。
- `agent_py_agent/agent/plugin_completion.py`：仅建议能够绑定到当前参数的值；停用插件只给静态帮助，文件枚举由宿主按显式路径声明提供。

- `agent_py_agent/cli/scenario.py`：诊断场景注册与参数目录；`scenario_cases/runner_retry_case.py` 和 `runner_retry_backend.py` 只负责保留的离线重试场景，旧结果块修复场景已删除。
- `agent_py_agent/agent/verification/runtime.py`：主子代理共用的被动验证入口；它消费工具执行事实，不裁决任意报告的语义正确性。
- `scripts/check_offline_contract_matrix.py`：开发文件完整性检查，验证证据项指向实际运行模块与测试；已删除无生产调用的旧 verifier integrity 合同及其自造数据测试。

- `docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md`：热点源码与参考阅读证据、未实施的重构顺序、Computer Use 当前条件及 Jev 可选接入方案。
- `docs/design/PLUGIN_LIFECYCLE.md`：可选 Python 插件的核心边界、命令目录、隔离依赖、版本绑定和卡死卸载；提案与现有实现明确区分。
- `docs/design/PLUGIN_PACKAGES.md`：本地包格式、读取预算、静态校验及待实现的安装提交和隔离撤销合同。
- `docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md`：社区候选抽样与热度快照、10 个简易插件的最小功能、分批实现顺序和组合卸载验收；不代表已实现。
- `docs/tasks/REFACTOR_PLUGIN_GOAL.md`：发布部署前置条件、十步执行状态、每步真实多 TUI 矩阵、证据与推进条件。

- `agent_py_agent/agent/agent_core/agent_tree/model_view.py`：保留 run 身份、状态、原因与真实 read_order；不暴露恢复目录，省略内容可沿原工具归档完整读取。
- `agent_py_agent/tests/test_agent_tree_model_view.py`：模型状态投影、终态报告可达性、状态不被省略及超长归档回读合同的定向验证。
- `agent_py_agent/agent/subagents/result_registered_artifacts.py`：从 exact run 的工具产物账本投影真实文件；自然最终回复与结构化收口共用，不扫描目录或搬运文件。
- `agent_py_agent/agent/subagents/runner_start.py`：在原创建锁内预留准确 pending 并核对原身份；启动记录实际写入归既有 lifecycle 服务，CLI 与进程内入口直接调用服务，不保留旧转发函数。
- `agent_py_agent/agent/subagents/file_runner_start.py`：显式无数据库模式在原 canonical 启动记录中预留和消费准确身份；停止可撤销，旧快照不能覆盖新预留或激活，普通保存只可回收同一身份。
- `agent_py_agent/agent/subagents/coordination.py`：复用原 owner 创建锁协调 canonical 创建、执行轮变更和插话预留；只记录本线程持锁事实，不保存任务状态，启动和退出等待不得持锁。
- `agent_py_agent/agent/agent_core/runner/activity_diagnostics.py`：复用现有心跳与调用账，阶段长等待只通知直属父级，不强杀或自动重派。
- `agent_py_agent/tests/test_subagent_activity_diagnostics.py`：慢流、阶段诊断、通知去重、旧执行代与并发进度保存的定向验证。

- `docs/design/THREAD_GOAL_LIFECYCLE.md`：Goal 可见性、单一事件续跑、状态边界与旧冲突显式恢复的开发合同。
- `docs/design/SUBAGENT_PARALLEL_EXECUTION.md`：逐项交付、递归等待与活动诊断的现行设计和验收要求。
- `agent_py_agent/agent/conversation/goal_recovery.py`：精确恢复旧共享任务 Goal，不迁移运行中的执行，不清除源记录。
- `agent_py_agent/agent/conversation/background_tool_policy.py`：后台目录计算的唯一实现，输入结构化事实，结果供 runtime 消费；不读写会话或启动执行器。
- `agent_py_agent/agent/conversation/background_progress_policy.py`：仅接收计数和策略身份，计算无进展次数与失败退避；状态读取、租约及持久写入仍由后台调度负责。
- `agent_py_agent/agent/conversation/background_supply_backoff.py`：按会话维护供应冷却，三类消费及就绪扫描共享同一实例；配置组装归 runtime，额度与持久策略分路不进入组件。
- `agent_py_agent/agent/conversation/background_goal.py`：从原 Goal、任务和时钟领域结算异常或发布后续唤醒；精确身份、事务、CAS 与写账顺序保持，不持有调度器或完整 Agent/Store。
- `agent_py_agent/agent/conversation/background_routing.py`：按原优先级惰性读取线程与 owner 路由；普通唤醒、观察批次、冻结重投及额度通知共用，不另建路由状态或投递链。
- `agent_py_agent/agent/conversation/background_claim.py`：仅持有原 claims 域及精确能力；领取后重查终态/恢复，运行中复用共享心跳，退出先停心跳再结算本 claim，普通失败最后记账。
- `agent_py_agent/agent/conversation/background_recovery.py`：每次解析并查询当前权威恢复入口；指纹只抑制重复日志，unknown/不可读阻断不消费原来源。
- `agent_py_agent/agent/conversation/background_context.py`：显式请求接口连接原会话事实、任务范围和有界模型投影；既有任务进度对账仍沿原调用顺序执行。
- `agent_py_agent/agent/conversation/background_history_seed.py`：复用 canonical 未压缩历史和 provider 投影；区分可用、禁用与不可读，不把读取失败变成空历史。
- `agent_py_agent/agent/conversation/background_execution.py`：显式接收执行、存储与参数准备能力，保留同片取消、Compact 和原生历史；不选择唤醒、不投递外部消息。
- `agent_py_agent/agent/conversation/background_delivery.py`：显式交付能力连接原渠道和唯一 store；外发、过程/final 提交、审计回执与整封冻结保持原顺序，不运行模型或拥有调度状态。
- `agent_py_agent/agent/gateway_parts/workspace_scope.py`：普通消息和首次 Goal 共用目录校验；只接受宿主已存在的合法路径，声明本身不增加权限。
- `agent_py_agent/agent/gateway_parts/request_context.py`：先领取车道再按 repair、索引、Compact、历史、任务顺序准备当前快照；不拥有独立任务状态。
- `agent_py_agent/agent/gateway_parts/request_binding.py`：精确请求、task/run/attempt 和 claim 的持久桥接；保持原 T 锁、原子 JSON 更新与恢复身份。
- `agent_py_agent/agent/gateway_parts/request_history.py`：正常、停止和异常共用 canonical 历史提交及原样 repair；按 request/part 去重，索引仅作投影。
- `agent_py_agent/agent/gateway_parts/request_prompt.py`：纯渲染已有上下文和历史种子，不重新读盘或通过文字裁决权限。
- `agent_py_agent/agent/conversation/history_projection.py`：完整行窗口供正文和原生回放共用，后台历史准备无需导入 Gateway 请求执行器。
- `agent_py_agent/agent/gateway_parts/stream_writer.py`：维护请求级缓冲与事件顺序，组合 `stream_events.py` 的公开投影及 `stream_approval.py` 的审批交互；不拥有 canonical 历史或执行权。
- `agent_py_agent/agent/conversation/compact_carry.py`：前后台共享完整工具归档替换和结构化插话合并；mailbox 释放仍由各运行器在原位置执行。
- `agent_py_agent/agent/conversation/store_usage.py`：显式接收原用量目录和线程读取/原子更新能力，持有用量事件、累计增量及数字显示，不继承消息、任务或 Goal 存储。
- `agent_py_agent/agent/conversation/store_io.py`：各领域与 transcript 共用 JSONL 读取、结构化错误和文件归档；不导入 Store，不吞坏行，不新增持久数据源。
- `agent_py_agent/agent/conversation/store_layout.py`：`store.storage` 的唯一目录和路径上下文；初始化可只读，路径方法不授权业务操作、不改变原文件名。
- `agent_py_agent/agent/conversation/store_threads.py`：`store.threads` 保存唯一线程元数据和通道索引，提供同一线程锁内的 CAS；新会话模型解析器由本领域持有。
- `agent_py_agent/agent/conversation/store_messages.py`：`store.messages` 保持原 append-only 账本、幂等锁和字节游标；只通过显式能力校验线程及更新活动时间。
- `agent_py_agent/agent/conversation/store_tasks.py`：`store.tasks` 保存原任务关联和线程活动索引，沿原顺序更新工作区投影并关闭终态进度；不建立第二套任务状态。
- `agent_py_agent/agent/conversation/store_audits.py`：`store.audits` 直接实现 Audit 准备、发布和重启操作，共用 `tasks` 的命名锁、任务锁和索引更新；无旧方法转发。
- `agent_py_agent/agent/conversation/store_guidance.py`：`store.guidance` 组装插话领域并提供入队、认领和查询；唯一外部写能力是注入消息幂等追加。
- `agent_py_agent/agent/conversation/store_guidance_records.py`：插话回执及校验的唯一格式定义，保留显式旧数据迁移规则，不负责落盘。
- `agent_py_agent/agent/conversation/store_guidance_ledger.py`：`guidance.ledger` 共用原目录与精确回合锁，读取回执并修复队列、回合及输入反查投影。
- `agent_py_agent/agent/conversation/store_guidance_submission.py`：`guidance.submissions` 管理模型调用提交批次及明确拒绝后的恢复，保留先批次后回执的顺序。
- `agent_py_agent/agent/conversation/store_guidance_acknowledgements.py`：`guidance.acknowledgements` 提交消费确认，幂等修复回执和原消息账本。
- `agent_py_agent/agent/conversation/store_guidance_recovery.py`：`guidance.recovery` 先修批次再结算或恢复；网络重试按旧/新 turn 排序及原回执锁，只为最新 pending 预留候选，逐条改绑共用原持久写入顺序。
- `agent_py_agent/agent/conversation/store_index.py`：观察、唤醒和策略共用有界扫描缓存；失效回读权威文件，目录不可读不能被当成空目录。
- `agent_py_agent/agent/conversation/store_claims.py`：`store.claims` 复用同一 storage 和原子文件更新；按结构化宿主、TTL、claim/task ID 领取与释放执行权，已结束旧租约由原维护周期归档。
- `agent_py_agent/agent/conversation/store_goals.py`：`store.goals` 持有原目标集合和 CAS，显式接收线程校验、任务读取及共享时钟；不建立新的执行或恢复状态。
- `agent_py_agent/agent/conversation/store_observations.py`：`store.observations` 管理观察账与确认回执；同一事件构造供唤醒联合发布复用，线程活动沿原子回调更新。
- `agent_py_agent/agent/conversation/store_wakes.py`：`store.wakes` 保持先唤醒后观察、去重锁、投递冻结和处理回执的原顺序；只通过注入能力确认观察。
- `agent_py_agent/agent/conversation/store_progress.py`：`store.progress` 负责策略 CRUD、到期读取及失败退避事实落账；策略/claim 跨域归档仍由 Store 原维护入口顺序协调。
- `agent_py_agent/agent/conversation/goal_clock.py`：前台、后台及控制视图共用同 owner 会话存储的目标时钟，四个计时操作直接归共享对象，整数结算保留小数余量。
- `agent_py_agent/agent/conversation/goal_binding.py`、`goal_delegation.py`：按代理自身 thread/run 归属目标和用量；显式子目标沿同一运行器续接，不另建执行通道。
- `agent_py_agent/agent/conversation/goal_control.py`、`goal_editing.py`：用户修改自己会话树的目标；内容版本防止并发覆盖，不把保存当作恢复。
- `agent_py_agent/cli/chat_parts/tui_goal_editor.py`：方向键选择 Goal、Enter 编辑、Ctrl+S 保存、Ctrl+G 放弃退出，Esc 保留停止。

- `cli/chat_parts/tui_complete_detail.py`、`tui_display_archive.py`：Ctrl+E 完整浏览的有界分页和异步读取，长行不因终端宽度丢字。
- `agent/conversation/display_archive.py`、`agent/gateway_parts/display_archive_service.py`：完整原文以 owner 私有不可变页保存，前端只有引用；跨 owner、无关会话或任意磁盘路径均拒绝。
- `agent/agent_core/tool_loop/display_archive.py`：在工具显示投影裁剪之前存真实执行快照；不读取当前文件重建历史，也不改变 LLM 工具输入。

- `agent_py_agent/cli/chat_parts/tui_clipboard.py`：串行投影显式选区，最新代次回执与退出清理，不读取或持久化系统剪贴板。

- `agent/conversation/compact_tool_refs.py`：checkpoint 的历史路径投影；仅认结构化原生调用和成功回执，不解析命令/摘要或赋予权限。
- `agent/conversation/compact_request_budget.py`：当前模型窗口内的摘要请求预算和连续分段；不持有历史游标或另建状态源。
- `agent_py_agent/vendor/bubblewrap/`：离线 bwrap 的第三方许可与对应源码材料；升级二进制时同步更新并验包。
- `agent/common/text_file_window.py`：64 KiB 流式索引、有限检查点与页面 cookie；编码和字符坐标只保留一个实现。
- `agent/common/file_version.py`：read_file 返回观察版本，write/edit/patch 明确携带前置条件；外部写入者不被强制纳管。
- `agent/tooling/process_output_capture.py`：前台进程每流最多保留 4 MiB，仍持续排空并公开不完整事实，不假装完整大输出归档。
- `agent_py_agent/agent/tooling/shell_syntax.py`：后台语法的唯一判断入口，复用公共命令分析器识别嵌套 Shell；动态变量与任意脚本不做静态安全证明。
- `agent/tooling/computer_text_input.py`：文本事件的唯一适配入口；macOS 使用 Quartz Unicode，其他平台使用 PyAutoGUI 支持的字符，结果不代替应用状态验收。
- `agent_py_agent/tests/test_r223_audit_regressions.py`：本轮底层故障样本；真实 TUI 另记在 R223 审计报告，不相互冒充。
- 已删除 `agent/agent_core/_compression_service.py` 的旧记忆拼接路径；正式 Compact 仍由 ConversationStore/checkpoint/CAS 负责。

- `agent/tooling/tool_search_state.py`：只读最新结构化工具轮的未消费工具名称；不修改授权、缓存归档或运行状态。
- `agent_py_agent/tests/test_tool_search_state.py`：验证当前轮、旧尾记录、损坏信封和自然语言不能授予临时工具。

- `agent/gateway_parts/main_activity.py`：前台请求与后台共用 main 标量；绑定当前 task、隔离迟到工作片，不驱动执行。
- `agent/gateway_parts/foreground_transcript.py`：同会话公开过程、候选增量与 final 快照，不拥有运行或审批权限。
- `agent_py_agent/tests/test_gateway_foreground_transcript.py`：前台 writer、消息顺序、缺帧恢复、候选接替与隔离验证。
- `agent/gateway_parts/model_profile_service.py`：authenticated owner 的模型菜单接口，不经聊天队列或模型。
- `agent/gateway_parts/owner_conversation_store.py`：沿原配置与 owner 路径组装模型菜单和插件管理共用的轻量会话 Store，不初始化完整 Agent。
- `agent/gateway_parts/approval_mode_service.py`：认证 owner 的权限菜单服务，不允许正文伪造管理员。
- `agent/user_space/approval_mode.py`：既有 owner 工具策略里的唯一用户审批模式读写与运行快照映射。
- `agent/user_space/owner_access.py`：原 core 路径权限裁决的唯一实现；冷管理与完整代理共用，不能替代 HTTP 管理角色认证。
- `agent/settings/model_profiles.py` 与 `model_scope.py`：用户模型存储和运行快照；敏感配置位于宿主 config/model-profiles，非业务目录。
- `agent/settings/model_oauth.py`、`model_oauth_schema.py`、`model_oauth_wire.py`：账号登录的状态、校验与协议；说明见 `docs/design/MODEL_OAUTH.md`。
- `agent/backends/oauth.py`、`oauth_transport.py` 和 `cli/chat_parts/tui_model_auth.py`：认证与原模型后端的适配及私密登录交互。
- `agent/settings/thread_model_selection.py`：按 canonical thread 固定模型，同 owner 多 TUI 不串配置，默认值只初始化新会话。
- `agent/settings/shared_model_catalog.py` 与 `cli/chat_parts/tui_shared_model_menu.py`：管理员逐模型发布共享引用；秘密留在原 provider 文件，撤销后明确提示而非换模型。
- `cli/chat_parts/tui_model_menu.py`：真实 TUI 模型菜单，保存/返回与模型执行分离。
- `cli/chat_parts/tui_provider_menu.py`：同一个 provider 管理多个模型；敏感字段仅表单暂存，短测试明确提示消耗。
- `agent/settings/model_provider_*.py`：v2 存储 schema/锁内修改/用户主动网络操作，配置只在 owner 私有文件存在一份。
- `agent/backends/provider_headers.py`、`responses.py`、`responses_wire.py`：统一身份与三种协议；不复制其他产品认证身份。
- `agent/backends/base.py`、`http.py`、`openai_chat.py`、`anthropic.py`、`factory.py`：公共合同、网络传输、协议适配和构造分工；包级公开导入指向唯一实现，内部调用方不依赖旧文件转发。
- `agent/backends/request_scope.py`：同 Gateway 前台端点占用和请求局部预算；外部程序及代理别名不作推断。
- `agent/backends/cache_diagnostics.py`：真实 HTTP 请求的无正文摘要，不修改模型请求或记忆。
- `agent/conversation/process_events.py`：原进程记录到原 wake 队列的耐久终态通知；不新增任务状态机。
- `agent/backends/sampling.py`：YAML/profile/backend 共用 top_p 数值校验；已知 Flash 方言默认与任意端点显式覆盖分开。
- `agent_py_agent/tests/test_gateway_main_activity.py`：前后台数值/阶段共享、任务晋升、跨会话拒绝、迟到关闭和显示故障验证。
- `agent_py_agent/tests/test_shell_stdin.py`：使用独立宿主管道验证批处理 EOF、输入不串和显式管道，不读取真实用户输入。
- `agent/conversation/history_page.py`：显示专用倒读页与工作片身份，双游标不回灌模型。
- `cli/chat_parts/tui_history.py`：每个 TUI 单一的按需旧页读取器，不持有后台任务或历史副本。

- `agent_py_agent/tests/test_conversation_message_stream.py`：canonical 消息分页、恢复竞态、损坏游标和实时/历史幂等验证。
- `agent_py_agent/tests/test_conversation_display_checkpoint.py`：未final主/子过程恢复、输入不变、精确覆盖与未知卡片补齐。
- `agent_py_agent/tests/test_background_history_snapshot.py`：完整后台块快照、慢客户端补帧、损坏快照拒绝重基、
  Compact 重调工具身份和 provider 历史不变验证。
- `agent_py_agent/tests/test_background_owner_delivery_commit.py`：后台答复 canonical 落账与外部投递解耦、
  未注册渠道 fail-closed、未送达正文冻结重投不重跑模型、审计回执不冒领和后台报告日志字段守卫。
- `agent/conversation/message_stream.py`：正文及显式协商检查点的唯一公开增量投影，复用 ConversationStore，不另存 notices 正文。
- `agent/conversation/display_checkpoint.py`：公开完整块的typed检查点，保存到同thread会话账本并排除模型/Compact/Memory。
- `agent/conversation/context_usage.py`：各代理最近上下文的统一数字投影；与 Working、模型输入、计费和校准分离。
- `agent/conversation/model_metrics.py`、`cli/chat_parts/tui_model_metrics.py`：按当前代理会话显示累计消耗，按工作片显示模型轮和当轮工具；缺报、重试、重放、压缩与线程隔离按统一数值协议处理。
- `agent/conversation/background_history.py`：按稳定块 ID 保存后台完整终态展示，逐 token 增量不重复存储；
  canonical final 提交前不授权 TUI 丢弃旧事件，不进入 provider history 或 Compact 输入。

- `agent_py_agent/tests/test_owner_home_workspace.py`：家目录主/子代理读写、跨 owner/符号链接拒绝、控制文件保护、普通目录名与缓存稳定整理指南。
- `agent_py_agent/tests/test_timeout_recovery_delivery.py`：门槛5 超时探针的无损交付合同——两枪已产生的合法答复按到达顺序逐字保留（不做长度二选一、不读正文做语义判定）、交付对象与 prompt/usage 同属终答那一轮、截断/零工具轮/预算用尽轮语义不变。
- `agent_py_agent/tests/test_store_scan_indexes.py`：唤醒队列/观察分片/进度策略三类台账读取侧索引的安全验收——有索引、硬关索引、索引损坏三态结果逐字一致；条目数与记录数两条预算各自独立生效（LRU）、目录删名清理不误删不可读目录、枚举成员与 `Path.glob` 同口径（断链符号链接等非普通条目照旧产生 load_error）。
- `agent_py_agent/tests/test_tui_injected_input_states.py`：插话三段的展示合同——排队(等待送入当前回合)/已提交(已进入本次提供方调用的 prompt，立即按原提交位置进入可见历史，但未获模型确认)/已确认(唯一能清标记的边界)；身份只用 client message_id/request_id/provider_call_id，重复事件与重连重放按块 ID 去重，不丢不重。
- `agent_py_agent/tests/test_slow_model_liveness.py`：慢模型/长任务活性合同——提供方层流式只看空闲(总墙钟守卫让位、非流式才显式超时)、客户端等待只由机器可观测活动续期且无总上限、服务端租约心跳在静默期持续推进、长工具滚动续租；四类(连接/首包、流式无进展、总时长、明确服务失败)分开断言。
- `agent_py_agent/tests/test_gateway_admission_wait.py`：合法排队等待的合同——被准入限流或恢复退避时worker 在请求文件写结构化 admission_wait_*（不写 status/lease/模型字段）、有节流与总预算、超预算留一次性过期事实；客户端仅凭该活动续期，停写/取消/终态/崩溃恢复仍按空闲窗口收口，含真实 worker+客户端端到端与负向对照。
- `scripts/bench/`：GW-03 与慢模型 A 项的长期可复跑配对基准（`measure_scheduler_reads.py` 锁内解析成本、`bench_owner_fact_kind.py` 各路径单测、`paired_owner_fact_kind.py` 两 checkout 交替比较）。只含结构化夹具，无真实会话内容；README 说明"锁更短 ≠ 整体更快"与"跨组数字不可相除"的证据边界。
- `agent_py_agent/tests/test_scheduler_scan_costs.py`：调度账本读取成本的合同——waiting 投影缓存的锁外探针 + 锁内三重校验（命中/外部改写/同 stat 不同内容/缓存清空都不得改变调用方结果）、`runtime_snapshot` 的锁外解析与旧实现参考投影逐字一致且锁内不再做 JSON/逐 run 解析、owner 事实判定缓存的签名失效与 TTL 边界。


- `agent/conversation/history_display.py`：同一 canonical message/native envelope 的只读公开显示转换入口，
  Gateway 与本地恢复共用；不拥有模型上下文、Compact、工具执行或第二份会话账本。

```text
~/.my-agent/owners/<provider>/<kind-or-id>/
|-- SOUL.md                             # AI 人格；用户明确确认后经 PersonaRepository 更新
|-- USER.md                             # 当前用户明确表达的稳定画像与偏好
|-- AGENTS.md                           # 长期合作方式；模型经 PersonaRepository 自主维护
|-- memory-hot.md                      # 每轮必读的少量高频短规则与精确 lesson 引用
|-- memory.md                          # 短导航，不保存第二份事实或教训正文
|-- memory/
|   |-- long_term/memory.jsonl         # 当前 active 正式长期事实唯一权威
|   |-- daily/YYYY-MM-DD.jsonl         # Curator 提炼的经历摘要和 refs，不是正式事实
|   |-- candidates.jsonl               # owner 唯一候选账本与状态机
|   |-- ops.jsonl                      # 无正文正式 Memory 操作审计
|   |-- curator/state.json             # 所有触发共用的 lease、cursor 与失败状态
|   |-- curator/runs/YYYY-MM-DD.jsonl  # 后台运行审计
|   |-- lessons/*.md                   # 可复用正式教训唯一详细正文
|   `-- routing/INDEX.md               # 由 lesson metadata 确定性重建的路由索引
|-- audit/YYYY-MM-DD.jsonl              # raw turn/tool/gateway 黑盒索引；非完整对话权威
|-- tasks/<date>/<task-slug>/           # 模型自行整理的用户工作；命名仅为软提示，旧目录不搬迁
|   |-- inputs/                         # 按需保留来源材料
|   `-- output/                         # 用户成品；代码按实际项目结构组织
|-- runs/<date>/<runtime-key>/          # 宿主运行归档，不作为 cwd 或普通文件权限根
|   |-- output/                         # 宿主兼容交接区，不是新业务默认落点
|   `-- work/                           # 状态、日志、代理交接与大输出归档
|-- agents/<run_id>/                    # 子代理 refs-only projection
|-- data/artifact_backups/v1/           # 前台 shell 改动 ready 产物时保留的 owner 私有哈希恢复 blob
|-- workspace/runtime/workspaces/<scope>/# LocalStore、gateway、conversation 等 workspace 账本
`-- global_index/                       # 可重建轻量索引

~/.my-agent/shared/                     # 管理员显式发布的公共 skills/tools/role templates；不放 owner 私有资料
```

普通运行不读写 repo 根 `data/*` 作为事实源；测试 fixture 或用户显式配置路径除外。

- `agent_py_agent/agent/tooling/mcp_transport.py`、`mcp_protocol.py`：请求和线程永远绑定原连接；临时清理与永久关闭分开，不持有插件激活权威。
- `agent_py_agent/agent/tooling/mcp_managed_process.py`：插件连接只走原托管启动，输出交给唯一文本读取端，原资源锁内复查原激活，清理保留完整 Store 回执与异常。
- `docs/design/MCP_TRANSPORT_LIFECYCLE.md`：MCP 的关闭、重连、发布顺序与未知清理边界；对应开发用例为 `agent_py_agent/tests/test_mcp_lifecycle.py`。
