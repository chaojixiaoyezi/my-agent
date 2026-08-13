# Codebase Tree

这份树只描述当前主链路。旧迁移入口、过渡计划和已删除模块不在这里保留。

```text
.dockerignore                           # Docker 生产源码允许列表，排除本地运行状态和测试产物
install.sh                              # 默认一键容器安装；生成透明 my-agent CLI，--host 为开发模式
agent_py_agent/
|-- __main__.py                         # python -m agent_py_agent CLI 入口
|-- config/                             # 默认 YAML 配置
|-- cli/                                # 命令行、chat/TUI、gateway 管理、诊断维护命令
|   |-- chat.py                         # 本地 chat 入口
|   |-- memory_admin_parser.py          # Memory v2 唯一管理员命令树与中文参数帮助
|   |-- memory_admin_commands.py        # Candidate/Curator/Retention/Doctor/Migration 共用正式 Service 的 CLI 适配
|   |-- chat_parts/                     # TUI、gateway client、stream/render worker
|   |   `-- control_runtime.py          # CLI 对共享会话控制协议及窗口级精确中断的运行适配
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- gateway_process.py              # gateway 进程入口
|   `-- _*.py                           # CLI 子命令实现
|-- skills/builtin/<category>/<name>/   # 内置知识型 skill 树：目录即分类（research/documents/…），递归扫描，类目索引常驻 prompt，skill_search 工具按需检索（千级地基）
|-- agent/
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- agent_core/                     # 主代理运行时、工具循环、编排工具、closeout
|   |   |-- cli_run_conversation.py     # 一次性 CLI 的权威 user/assistant transcript、幂等身份与失败分级
|   |   |-- runtime/                    # guidance、active-turn compact carrier、wait policy、loop support
|   |   |-- tool_loop/                  # 工具轮次执行、恢复、完成判断
|   |   |-- tool_context/               # 工具结果上下文：reducer、窗口、microcompact、PTL 单轮重试
|   |   |-- orchestration/              # create/dispatch/cancel/inspect 子代理工具实现
|   |   |-- delivery_closeout/          # 交付验收和收口
|   |   |-- delivery_closeout/expected_outputs_gate.py # 产物类型/数量对账门：声明驱动核对交付区实存
|   |   |-- delivery_closeout/source_volume.py # 来源比例观测：检索量 vs 交付量并排数字（纯观测零判定）
|   |   |-- delivery_closeout/snapshot.py # 最终交付事实快照：文件名/字节数/hash/gate 状态与指纹
|   |   |-- tool_loop/natural_user_reply.py # 派工/wait/完成共用的无工具 LLM 用户回复出口
|   |   |-- tool_loop/final_exit_contract.py # run 出口合同：未收口任务态必走 closeout+续航双闸
|   |   |-- tool_loop/failure_only_exit.py # 当前 request 全工具终态阻断时丢弃无证据结论
|   |   |-- current_turn_execution.py # 当前 request canonical 工具事实的有界 prompt-tail 投影
|   |   |-- tool_loop/exit_orphan_recovery.py # 出口孤儿回收：未收口退出前终止后台子代理进程并 requeue
|   |   `-- runner/                     # 子代理 runner prompt/worker/session/timeout；context.py 也隔离共享 Agent 的 thread-local 运行态
|   |-- subagents/
|   |   |-- manager.py                  # 子代理 root manager：初始化、基础生命周期、服务组合
|   |   |-- kernel.py                   # 子代理树快照
|   |   |-- manager_work_orders.py      # 工单路径、默认文件、校验
|   |   |-- models.py                   # 子代理数据模型
|   |   |-- process_control.py          # 后台进程治理原语：存活探测/两阶段终止（SIGTERM→SIGKILL）
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
|   |-- user_space/                    # owner home、task workspace、policy、quota、doctor、自动 retention
|   |   |-- owner_quota.py             # 结构化写入口的 owner 跨进程配额锁与整批最终字节准入
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
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |   |-- control_service.py         # owner/thread 持久根任务的即时状态、纠偏和中断
|   |   |-- channel_health.py          # adapter PID/heartbeat/逐通道状态的 fail-closed 健康投影
|   |   `-- goal_control_service.py    # 同 thread 持续目标的创建/修改/暂停/恢复/清除
|   |-- conversation/                  # 通道会话账本、权威 transcript、结构化任务关联/续接
|   |   |-- compact.py                  # 唯一 thread compact：候选验证、一次 CAS 提交与近期 raw tail
|   |   |-- compact_guard.py            # 结构化完整回合选择、连续失败冷却与 typed compact 错误
|   |   |-- compact_checkpoint.py       # owner-scoped 完整 compact 恢复点与代际引用
|   |   |-- task_runtime_state.py      # 后台续轮读取精确任务进度的结构化运行事实
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
|   |   `-- delivery.py                # 通道长任务结果的持久化异步回送与重启去重
|   |-- settings/                      # AgentConfig、加载、来源账本、runtime scope config
|   |-- common/                        # 跨域小权威：safe_id、path_normalize、json_io、日志脱敏、结构化输出批处理
|   |   `-- audit_activation.py        # 显式 `/audit` 前缀 -> guarantee/window 结构化激活
|   |-- concurrency/                   # 重试/退避（jittered backoff）、锁、per-thread 协作中断
|   |-- owner_object_store.py          # scale owner PG/RLS manifest + versioned S3，Pod 盘只作缓存
|   |-- scale_runtime.py               # scale role/release channel/S3 配置 fail-closed
|   |-- continuous_monitor_entry.py    # 真实 wall-clock 异构来源 proof 长守入口
|   |-- contracts/                     # 稳定协议、错误分类（taxonomy+provider 九类分类器）、验收合同
|   |   `-- tool_input_schema.py       # 工具参数有限 JSON Schema 纠正/完整校验与脱敏问题路径
|   |-- tooling/                       # 唯一 ToolRuntime/ActionPolicy/ToolExecutor、写入边界与结果投影
|   |   |-- models.py                 # ToolModelSpec、ToolRuntimePolicy、ToolRuntime/Snapshot 与 handler outcome
|   |   |-- runtime_contracts.py      # 唯一 canonical ToolCall/ToolResult、ToolChoice、协议与 operation 合同
|   |   |-- input_schema.py           # 唯一 input_schema 规范化、强类型纠正和完整执行前校验
|   |   |-- action_policy.py          # 副作用前唯一 allow/ask/deny 聚合决策
|   |   |-- executor.py               # approval、sandbox、handler、账本、核对、持久化与投影状态机
|   |   |-- runtime_boundary.py       # task 相对路径归一与精确读边界检查
|   |   |-- capabilities_tool.py      # 从真实工具目录与唯一 channel registry 投影模型能力
|   |   |-- _persona_write_guard.py   # SOUL/USER/AGENTS 统一强制走 update_persona
|   |   |-- process_registry.py       # 前后台命令完整后代树终止的唯一进程入口
|   |   |-- shell.py                  # run_command、超时/中断与有界 pipe drain
|   |   |-- tool_input_completion.py # 明示安全默认值、可信上下文补参与脱敏 source/source_ref
|   |   `-- sandbox.py                # bwrap 唯一策略、自检、worker/K8s readiness 硬门
|   |-- capability/                    # 单一 SkillsService、逐轮 snapshot、能力路由与 capability tools
|   |   |-- skill_service.py           # bounded builtin/shared/owner/workspace discovery、policy 与缓存
|   |   |-- skill_snapshot.py          # 不可变稳定引用、正文 hash/guard 校验与子代理收窄
|   |   |-- persona_repository.py      # owner SOUL/USER/AGENTS 受控加载、版本/CAS/回滚唯一入口
|   |   `-- channel_message_tool.py    # 当前 owner 的统一 send_message；登记产物经原生通道发送
|   |-- prompting_parts/               # prompt 构造
|   |   `-- memory_context.py          # 非权威、可转义且可统一剥离的召回记忆信封
|   |-- scale_downstream.py            # scale worker 复用普通 gateway 会话执行主链
|   `-- backends/                      # 模型后端适配、run 固定协议/tool_choice、原生工具历史与结构化生成
|       `-- tool_protocol_adapter.py   # native 事件或显式完整 text 帧到 canonical ToolCall 的唯一适配口
|-- tests/                             # 单元、集成、真实链路回归
|   |-- test_current_turn_execution.py # 当前轮成功/失败副作用事实投影回归
|   |-- test_cli_run_conversation.py   # CLI transcript、Memory 消息证据、任务链接与 workspace 收口回归
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
|   |-- test_sandbox.py                # bwrap argv、自检协议、owner-scoped fail-closed
|   |-- test_container_install.py      # 假 runtime 验证一键 build/probe/透明包装器
|   `-- test_check_clean_package.py    # untracked、运行目录和 tar/wheel 制品门
scripts/
|-- live_lab/                          # 真实链路 harness；真实 preflight、main-artifact、tool-recovery
`-- check_clean_package.py             # 工作树与真实发布制品的结构化干净度检查
deploy/
|-- Dockerfile                         # 内置系统 bubblewrap+tini，构建期 binary probe
|-- seccomp-bwrap.json                 # 固定 Moby 默认 profile，仅放行 bwrap namespace/mount 系统调用
|-- seccomp-bwrap.PROVENANCE.md        # 上游 commit、许可、本地增量和升级验收说明
`-- k8s/                              # stable/canary、Gateway route、migration、monitor 与 DR 清单
docs/
|-- PRODUCT_FACTS.md                    # 当前能力状态唯一权威：稳定/部分可用/实验性/仅设计
|-- design/FEATURE-20260804-tool-runtime-unification.md # 工具唯一主链的用户行为、需求与验收规格
|-- design/tool-runtime-unification.md  # 工具参考证据、架构、迁移删除表与并行边界
|-- tasks/completed/TASK-20260804-1913-tool-runtime-unification.md # 工具唯一主链实施与验收记录
|-- design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md # 自我描述、Shared、Memory、Persona、Compact、Skill、Workflow、配额、隐私和完成质量审计
|-- design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md # 灰度/灾备/Owner store/24h proof 事实
|-- architecture/BOUNDARY_RULES.md      # 分层和写入边界
|-- architecture/MODULE_OWNERSHIP.md    # 当前模块归属
|-- architecture/MY_AGENT_HOME_LAYOUT.md# owner home 布局
|-- modules/                            # 分模块结构和进展
`-- development/                        # 开发规则、review、写文件规则
```

## Current Storage Roots

```text
~/.my-agent/owners/<provider>/<kind-or-id>/
|-- SOUL.md                             # AI 人格；用户明确确认后经 PersonaRepository 更新
|-- USER.md                             # 当前用户明确表达的稳定画像与偏好
|-- AGENTS.md                           # 长期合作方式；用户明确确认后更新
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
|-- tasks/<date>/<task-slug>/           # 当前任务工作区
|   |-- output/                         # 最终交付物
|   `-- work/                           # 状态、日志、子代理原始结果与过程产物
|       `-- blobs/tool_outputs/         # 大工具输出完整正文；Memory 只保存 preview/hash/size/ref
|-- agents/<run_id>/                    # 子代理 refs-only projection
|-- workspace/runtime/workspaces/<scope>/# LocalStore、gateway、conversation 等 workspace 账本
`-- global_index/                       # 可重建轻量索引

~/.my-agent/shared/                     # 管理员显式发布的公共 skills/tools/role templates；不放 owner 私有资料
```

普通运行不读写 repo 根 `data/*` 作为事实源；测试 fixture 或用户显式配置路径除外。
