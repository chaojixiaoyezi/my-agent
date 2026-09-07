# Codebase Tree

这份树只描述当前主链路。旧迁移入口、过渡计划和已删除模块不在这里保留。

```text
.dockerignore                           # Docker 生产源码允许列表，排除本地运行状态和测试产物
install.sh                              # 默认一键容器安装；生成透明 my-agent CLI，--host 为开发模式
TUI_EXTREME_TEST_MATRIX.md              # 五路 TUI 持续轮转、边界/组合/fuzz/soak 用例与客观证据权威账本
agent_py_agent/
|-- __main__.py                         # python -m agent_py_agent CLI 入口
|-- config/                             # 默认 YAML 配置
|-- cli/                                # 命令行、chat/TUI、gateway 管理、诊断维护命令
|   |-- chat.py                         # 本地 chat 入口
|   |-- chat_client_context.py          # 轻量 Gateway TUI 客户端、活动输入三态与 input-status 查询
|   |-- memory_admin_parser.py          # Memory v2 唯一管理员命令树与中文参数帮助
|   |-- memory_admin_commands.py        # Candidate/Curator/Retention/Doctor/Migration 共用正式 Service 的 CLI 适配
|   |-- chat_parts/                     # TUI、gateway client、stream/render worker
|   |   |-- chat_prompt_queue.py        # 可按 request identity 原子回取且保持 FIFO/task_done 账的聊天任务队列
|   |   |-- tui_agent_navigation.py     # TUI 精确子代理选择栈、详情游标与父子视图切换状态
|   |   |-- tui.py                      # chat TUI 生命周期、唯一 runtime/worker/preflight 接线与返回码
|   |   |-- tui_block_renderer.py       # typed snapshot 到欢迎/消息/思考/工具/权限/队列/footer formatted lines
|   |   |-- tui_input.py                # 真实 slash/path 补全、菜单、history suggest 与排队占位投影
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
|   |   `-- control_runtime.py          # CLI 对共享会话控制协议及窗口级精确中断的运行适配
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- gateway_process.py              # gateway 进程入口
|   `-- _*.py                           # CLI 子命令实现
|-- skills/builtin/<category>/<name>/   # 内置知识型 skill 树：目录即分类（research/documents/…），递归扫描，类目索引常驻 prompt，skill_search 工具按需检索（千级地基）
|-- agent/
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- turn_end.py                     # 主/子代理共用的六种宿主轮结束原因
|   |-- model_guidance.py               # 完整 Prompt 与有副作用工具共用的验证/授权软提示唯一正文
|   |-- task_progress_guidance.py        # Todo exact-id 最终回复前核对的可配置软合同；不自动判定或打勾
|   |-- agent_core/                     # 无副作用包入口；主代理运行时、工具循环、编排与自然回合收口实现
|   |   |-- cli_run_conversation.py     # 一次性 CLI 的权威 user/assistant transcript、幂等身份与失败分级
|   |   |-- runtime/                    # 单 child guidance、active-turn compact carrier、sleep 闹钟与 loop support
|   |   |   |-- conversation_state.py  # 主/子代理当前 Compact 代次的结构化模型事实投影
|   |   |   |-- sleep_tool.py           # clock.sleep 工具：模型主动定时等待，写 wake_queue 字条、事件提前醒取消
|   |   |-- model/                      # 主工具循环的统一模型调用账、动态超时、上下文压力与成本统计
|   |   |-- tool_loop/                  # 工具轮次执行、恢复与自然结束
|   |   |-- tool_context/               # 工具结果上下文：reducer、窗口、microcompact、PTL 单轮重试
|   |   |-- orchestration/              # 创建、只读状态、消息、取消、授权五个递归直属工具与内部自动启动/恢复引擎；无兄弟 goal 广播，进展事件由宿主写入
|   |   |   |-- capacity.py           # 根/子/孙代理共用的会话树与 owner 容量事实；超限整批拒绝
|   |   |   |-- coordinator_policy.py # 主代理/多层 coordinator 共用的 会话运行时 式派工后职责软合同
|   |   |   |-- tools/list_agents.py  # 会话运行时 式只读代理树查询；复用 canonical 投影，不推进或取消下级
|   |   |   `-- planned_delegation.py # 已有 Todo 时，创建前原子校验 active exact covers 与父级 workspace 上界
|   |   |-- agent_tree/status.py        # `/status`、TUI、恢复与诊断共用的内部代理树投影（不是模型工具）
|   |   |-- _finalization_service.py   # 保留模型最终正文并记录 turn_end.reason
|   |   |-- tool_loop/natural_user_reply.py # 派工/续跑/完成共用的无工具 LLM 用户回复出口
|   |   |-- tool_loop/completion.py     # 工具上限、截断与递归 child 创建后的结构化让出
|   |   `-- runner/                     # 子代理 runner prompt/worker/session/timeout；context.py 也隔离共享 Agent 的 thread-local 运行态
|   |-- subagents/
|   |   |-- manager.py                  # 子代理 root manager：初始化、基础生命周期、服务组合
|   |   |-- kernel.py                   # 子代理树快照
|   |   |-- manager_work_orders.py      # 工单路径、默认文件、校验
|   |   |-- models.py                   # 子代理数据模型
|   |   |-- process_control.py          # 后台进程治理原语：存活探测/两阶段终止（SIGTERM→SIGKILL）
|   |   |-- direct_parent_lifecycle.py # 直属父子等待、事件唤醒、同批合并与结果上下文
|   |   |-- tool_failure_ledger.py      # 系统级工具失败账本：archive ok=False 摘要 -> attributes/对账投影
|   |   |-- services/                   # 子代理业务服务
|   |   |   |-- base.py                 # create_run/split/owner/runtime config scope
|   |   |   |-- output_alignment.py    # 声明产物 -> 子代理可写落点投影 + delivery_map
|   |   |   |-- persistence/            # canonical state、projection、index 同步
|   |   |   |-- dispatch/               # dispatch/watch/parent planner 报告
|   |   |   |-- runner_context_service.py # 执行上下文和边界文件
|   |   |   |-- runner_result_service.py # runner result 写回和副作用
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
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |   |-- approval_session.py        # owner/thread/cwd/权限精确作用域的有界进程内工具审批缓存
|   |   |-- bounded_http_server.py     # 单 Gateway 固定 daemon worker、128 在途上限与过载 503 背压
|   |   |-- control_service.py         # owner/thread 持久根任务的即时状态、纠偏和中断
|   |   |-- control_operation_service.py # slash 控制副作用前置回执、幂等重放与 unknown 对账
|   |   |-- input_delivery_service.py  # 普通消息 active/queued 去向的唯一持久回执与后台对账
|   |   |-- request_client.py          # 薄客户端 ask 载荷和不可变执行选项合同
|   |   |-- channel_health.py          # adapter PID/heartbeat/逐通道状态的 fail-closed 健康投影
|   |   |-- permission_bridge.py       # TUI/Gateway 工具审批 request binding 的原子决定文件桥
|   |   `-- goal_control_service.py    # 同 thread 持续目标的创建/修改/暂停/恢复/清除
|   |-- conversation/                  # 通道会话账本、权威 transcript、结构化任务关联/续接
|   |   |-- compact_progress.py       # transcript/live-tool/turn-local Compact 来源与提交权的唯一公开进度协议
|   |   |-- agent_activity.py          # active task link + canonical child run 到 TUI/Web 共用有界活动投影
|   |   |-- agent_transcript.py        # 子代理跨进程公开过程事件的 owner 存储、游标和有界裁剪
|   |   |-- agent_control.py           # owner 树内代理详情、运行中 guidance 与精确停止的通道中立控制面
|   |   |-- agent_tool_approval.py     # child exact ToolApprovalRequest 的 owner 耐久记录、consumer 租约与决定等待
|   |   |-- background_transcript.py  # 后台 main/child 的有界 typed 过程事件环与 child 工具审批 sink
|   |   |-- background_history.py     # 后台完整展示块快照，随 canonical final 保存并用于恢复重基
|   |   |-- auxiliary_model_call.py   # Compact 等会话辅助模型调用的统一记账、退避、并发闸和成本统计
|   |   |-- tool_context_window.py    # text/native 共用的有界工具历史窗口与稳定前缀投影
|   |   |-- tool_input_progress.py     # provider 大工具参数生成期的脱敏临时展示合同
|   |   |-- agent_thread.py            # child/grandchild 独立 thread、逐 attempt transcript 与统一 Compact 适配
|   |   |-- agent_thread_store.py      # agent thread 精确 ID 物化、身份冲突与运行目录校验
|   |   |-- closeout.py                # 收口状态机 decide_closeout(四改之 2): 终态 done/cancelled/wait_human/wait_handoff/resume_round
|   |   |-- compact.py                  # 唯一 thread compact：候选验证、一次 CAS 提交与近期 raw tail
|   |   |-- compact_provider_surface.py # transcript Compact 复用普通轮 stable prompt/system/tools/messages 的缓存面
|   |   |-- compact_guard.py            # 结构化完整回合选择、连续失败冷却与 typed compact 错误
|   |   |-- compact_checkpoint.py       # owner-scoped 完整 compact 恢复点与代际引用
|   |   |-- active_turn_compact.py      # 跨工作片工具 archive 到同一 checkpoint/CAS 的恢复压缩与模型投影
|   |   |-- live_tool_compact.py        # 运行中原生工具历史到同一 thread checkpoint/CAS 的适配层
|   |   |-- native_history.py           # 完成回合的 provider 原生消息信封、校验与按请求替换式恢复
|   |   |-- history_display.py          # 从 canonical 消息投影只读恢复事件，不把问答预览代替正文
|   |   |-- history_page.py             # canonical 字节边界向前分页和完整工作片分组
|   |   |-- message_stream.py           # 从同一消息账本分页投影后台 final，恢复与实时共用 ID/游标
|   |   |-- task_runtime_state.py      # 后台续轮读取精确任务进度的结构化运行事实
|   |   |-- runtime.py                  # 后台主代理调度热循环：wake_queue 到期消费、三源对账(5min)、事件提前醒取消闹钟
|   |   |-- control_commands.py        # CLI/IM 共用 typed slash dispatcher、task command 与状态渲染
|   |   |-- goal_tools.py              # 持续目标轮精确 scoped 的 get_goal/update_goal
|   |   |-- authority.py               # 标记会话 transcript 为当前多轮对话唯一事实源
|   |   `-- task_promotion.py          # 任务工具触发提升、完成/中断候选选择与关闭
|   |-- scheduler/                     # owner 持久 at/every/cron、CAS job/run 账本、claim/heartbeat 与同 thread 唤醒
|   |-- verification/                  # owner 被动验证事件、targeted/full 投影与文件写后 stale
|   |-- delivery/                      # 多 IM 统一投递：registry、可信 context、reply envelope、receipt
|   |   |-- registry.py                # adapter/配置/健康/绑定/capabilities/target validator 唯一注册表
|   |   `-- service.py                 # 普通回复、主动消息、原生附件的统一发送出口
|   |-- adapter/
|   |   |-- delivery.py                # 通道 input_receipt/request_result 三态回送、CAS 与重启去重
|   |   `-- ingress.py                 # POST 前 durable ingress、冲突隔离与单线程全链恢复
|   |-- settings/                      # AgentConfig、加载、来源账本、runtime scope config
|   |-- common/                        # 跨域小权威：safe_id、path_normalize、json_io、日志脱敏、结构化输出批处理
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
|   |   |-- computer_use_profile.py   # MIT 开源桌面执行器的 local/main + Full Access MCP 薄装配与 effect 边界
|   |   |-- computer_use_server.py    # 启动开源执行器并复用其 PyAutoGUI 补齐滚轮工具
|   |   |-- models.py                 # ToolModelSpec、ToolRuntimePolicy、ToolRuntime/Snapshot 与 handler outcome
|   |   |-- runtime_contracts.py      # 唯一 canonical ToolCall/ToolResult、ToolChoice、协议与 operation 合同
|   |   |-- input_schema.py           # 唯一 input_schema 规范化、强类型纠正和完整执行前校验
|   |   |-- action_policy.py          # 副作用前唯一 allow/ask/deny 聚合决策
|   |   |-- executor.py               # approval、sandbox、handler、账本、核对、持久化与投影状态机
|   |   |-- runtime_boundary.py       # task 相对路径归一与精确读边界检查
|   |   |-- capabilities_tool.py      # 从真实工具目录与唯一 channel registry 投影模型能力
|   |   |-- _filesystem_display.py   # 文件工具共用的有界 diff/write 富终端展示事实构造器
|   |   |-- _persona_write_guard.py   # SOUL/USER/AGENTS 统一强制走 update_persona
|   |   |-- background_process_host.py # 脱离 one-shot runner 的后台命令托管、日志上限与退出事实
|   |   |-- process_registry.py       # 受保护记录的进程缓存、水合、PID 身份核对与完整后代树终止
|   |   |-- process_session_store.py  # owner 沙箱外的后台 session 权威记录、锁和单调终态
|   |   |-- process_network_status.py # exact 受管进程树监听、防火墙显式规则与外部探针边界的只读投影
|   |   |-- process_sessions.py       # owner+TUI 会话隔离的后台命令查询、等待与停止工具
|   |   |-- gateway_status.py         # 本机管理员读取唯一 Gateway 身份、端点、队列和本生命周期日志摘要
|   |   |-- shell.py                  # run_command、超时/中断与有界 pipe drain
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
|       |-- anthropic_prompt_cache.py  # Anthropic tools/system/最新 history 断点与追加式 user 投影
|       |-- base.py                    # 各 provider HTTP 请求、流式解析与统一 Backend 工厂
|       `-- tool_protocol_adapter.py   # native 事件或显式完整 text 帧到 canonical ToolCall 的唯一适配口
|-- tests/                             # 单元、集成、真实链路回归
|   |-- fixtures/tui/                   # 固定尺寸/时间线的非敏感 TUI PTY 动作 fixture
|   |-- test_adapter_ingress.py         # adapter POST 前落盘、幂等/隔离、响应丢失与崩溃恢复回归
|   |-- test_agent_transcript.py        # 子代理公开过程事件的增量游标、隔离和有界裁剪回归
|   |-- test_chat_prompt_queue.py       # canonical chat Queue 精确回取、FIFO 与 unfinished-task 对账
|   |-- test_owner_home_workspace.py    # 主/子代理家目录范围、跨 owner 拒绝、运行记录分离与软整理指南
|   |-- test_tui_ansi_snapshot.py       # ANSI offset 重放、样式/背景、Unicode、resize 和坏账 fail-closed 回归
|   |-- test_tui_agent_navigation.py    # 子代理选中/进入/返回、详情过程、只读终态与 footer 回归
|   |-- test_tui_events.py              # TUI event 信封、sequencer、cursor、重复/冲突/乱序与有界重放
|   |-- test_tui_markdown.py            # CommonMark 标题/列表/引用/代码/表格、样式角色与 Unicode 宽度换行
|   |-- test_tui_runtime.py             # 本地/Gateway 流式、工具、queue、终态和全局事件顺序 adapter 回归
|   |-- test_tui_view.py                # UIContent formatted lines、frame/block cache、follow anchor 和 resize 重排
|   |-- test_tui_history_paging.py      # 更早页渲染顺序、锚点、冻结页、子页/过期响应和单在途读取
|   |-- test_conversation_history_paging.py # 中文长行字节边界、完整工作片、坏游标与追加竞态
|   |-- test_tui_input.py               # slash/path 补全、菜单选择、queue 回取与 bracketed paste 输入回归
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
|   |-- test_memory_promotion_v2.py    # 证据/冲突/Persona/lesson/HOT 晋升边界
|   |-- test_memory_recall_v2.py       # 正式来源、scope、陈旧索引、owner 隔离与信封安全
|   |-- test_memory_migration_v2.py    # v1→v2 dry-run、备份、回滚、幂等与坏数据关闭式失败
|   |-- test_memory_retention_v2.py    # 保留期、legal hold、终态保护、重验证与 hard delete
|   |-- test_memory_admin_cli_v2.py    # 统一 Memory 管理命令复用正式服务
|   |-- test_tool_input_completion_provenance.py # 有限补参、来源账目、伪造拒绝和旧旁路删除回归
|   |-- test_tool_input_schema.py      # 强类型纠正、嵌套/组合/边界规则与显式 Schema fail-closed
|   |-- test_process_sessions.py       # 后台命令有界等待、进程树停止与 owner/TUI 会话隔离回归
|   |-- test_gateway_status_tool.py    # Gateway 权威身份、端点与生命周期日志诊断回归
|   |-- test_sandbox.py                # bwrap argv、自检协议、owner-scoped fail-closed
|   |-- test_container_install.py      # 假 runtime 验证一键 build/probe/透明包装器
|   `-- test_check_clean_package.py    # untracked、运行目录和 tar/wheel 制品门
scripts/
|-- live_lab/                          # 真实链路 harness；真实 preflight、main-artifact、tool-recovery
|-- tui_ansi_snapshot.py               # pyte 开发工具：从 raw ANSI/offset 账还原文本、样式、光标和标题快照
|-- tui_reference_fixture_server.py    # loopback 确定性 Anthropic 服务：驱动 TUI Markdown/思考/权限/错误黑盒场景
|-- tui_pty_recorder.py                # argv-only PTY 黑盒录制器：按键/粘贴/resize、原始 ANSI、事件索引与脱敏 manifest
`-- check_clean_package.py             # 工作树与真实发布制品的结构化干净度检查
deploy/
|-- Dockerfile                         # 内置系统 bubblewrap+tini，构建期 binary probe
|-- seccomp-bwrap.json                 # 固定 Moby 默认 profile，仅放行 bwrap namespace/mount 系统调用
|-- seccomp-bwrap.PROVENANCE.md        # 上游 commit、许可、本地增量和升级验收说明
`-- k8s/                              # stable/canary、Gateway route、migration、monitor 与 DR 清单
docs/
|-- PRODUCT_FACTS.md                    # 当前能力状态唯一权威：稳定/部分可用/实验性/仅设计
|-- audits/TUI_FUNCTION_AUDIT_20260828.md # 真实 TUI 逐功能结果、问题根因、修复影响与复验证据账
|-- design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md # child→owner 具体工具审批的身份、租约、FIFO 与失败语义
|-- design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md # 后台命令 host 所有权、跨进程记录与安全回收设计
|-- design/FEATURE-20260818-终端交互-tui-parity.md # 终端交互 TUI Python 原生复刻的用户行为、事件架构与验收规格
|-- design/TUI_终端交互_PARITY_MATRIX.md # 启动、消息、输入、权限、生命周期和命令映射逐项证据账
|-- tasks/completed/TASK-20260818-终端交互-tui-parity.md # 已完成 TUI 复刻实施、测试机边界和验收记录
|-- design/FEATURE-20260804-tool-runtime-unification.md # 工具唯一主链的用户行为、需求与验收规格
|-- design/tool-runtime-unification.md  # 工具参考证据、架构、迁移删除表与并行边界
|-- tasks/completed/TASK-20260804-1913-tool-runtime-unification.md # 工具唯一主链实施与验收记录
|-- design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md # 自我描述、Shared、Memory、Persona、Compact、Skill、Workflow、配额、隐私和完成质量审计
|-- design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md # 灰度/灾备/Owner store/24h proof 事实
|-- architecture/BOUNDARY_RULES.md      # 分层和写入边界
|-- architecture/MODEL_CATALOG_SNAPSHOT.md # 当前模型型号与未来声明式目录边界的可审计参考快照
|-- architecture/MODULE_OWNERSHIP.md    # 当前模块归属
|-- architecture/MY_AGENT_HOME_LAYOUT.md# owner home 布局
|-- modules/                            # 分模块结构和进展
`-- development/                        # 开发规则、review、写文件规则
```

## Current Storage Roots

### 关键文件说明

- `agent/conversation/history_page.py`：显示专用倒读页与工作片身份，双游标不回灌模型。
- `cli/chat_parts/tui_history.py`：每个 TUI 单一的按需旧页读取器，不持有后台任务或历史副本。

- `agent_py_agent/tests/test_conversation_message_stream.py`：canonical 消息分页、恢复竞态、损坏游标和实时/历史幂等验证。
- `agent_py_agent/tests/test_background_history_snapshot.py`：完整后台块快照、慢客户端补帧、损坏快照拒绝重基、
  Compact 重调工具身份和 provider 历史不变验证。
- `agent/conversation/message_stream.py`：后台 final 的唯一公开增量投影，复用 ConversationStore，不另存 notices 正文。
- `agent/conversation/background_history.py`：按稳定块 ID 保存后台完整终态展示，逐 token 增量不重复存储；
  canonical final 提交前不授权 TUI 丢弃旧事件，不进入 provider history 或 Compact 输入。

- `agent_py_agent/tests/test_owner_home_workspace.py`：家目录主/子代理读写、跨 owner/符号链接拒绝、控制文件保护、普通目录名与缓存稳定整理指南。


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
