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
|   |-- chat_parts/                     # TUI、gateway client、stream/render worker
|   |   `-- control_runtime.py          # CLI 对共享会话 status/steer/stop 协议的运行适配
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- gateway_process.py              # gateway 进程入口
|   `-- _*.py                           # CLI 子命令实现
|-- skills/builtin/<category>/<name>/   # 内置知识型 skill 树：目录即分类（research/documents/…），递归扫描，类目索引常驻 prompt，skill_search 工具按需检索（千级地基）
|-- agent/
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- agent_core/                     # 主代理运行时、工具循环、编排工具、closeout
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
|   |   |-- run_learning_review.py     # run 收尾自学习复盘钩子（教训进 drafts 待审，默认关闭）
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
|   |   |   `-- memory_gate/           # task-local 经验候选审核
|   |   |-- patch/                     # patch review/apply 底层实现
|   |   |-- execution/                 # 测试执行和记录
|   |   `-- static_site/               # 静态站点检查
|   |-- user_space/                    # owner home、task workspace、policy、quota、doctor、自动 retention
|   |   |-- owner_quota.py             # 结构化写入口的 owner 跨进程配额锁与整批最终字节准入
|   |   |-- home_retention.py          # 结构化终态/时间清理、二次校验、trash tombstone 与 legal hold
|   |   `-- owner_maintenance.py       # owner 维护间隔、状态记录与自动执行控制
|   |-- memory_store/                  # owner JSONL 权威长期记忆、候选/操作审计与派生索引
|   |   |-- jsonl.py                  # 稳定 ID CRUD/batch、去重/冲突、hard delete 与统一召回
|   |   |-- operations.py             # ops.jsonl 模型候选和无正文操作审计
|   |   `-- security.py               # Memory/Persona 共用持久内容威胁扫描
|   |-- memory_archive/                # compact、audit、tool output artifact、task workspace refs
|   |-- local_storage/                 # SQLite/FTS/文件事实源；ledger_redaction.py 精确擦除已删事实但保留幂等身份
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |   |-- control_service.py         # owner/thread 持久根任务的即时状态、纠偏和中断
|   |   |-- channel_health.py          # adapter PID/heartbeat/逐通道状态的 fail-closed 健康投影
|   |   `-- goal_control_service.py    # 同 thread 持续目标的创建/修改/暂停/恢复/清除
|   |-- conversation/                  # 通道会话账本、权威 transcript、结构化任务关联/续接
|   |   |-- task_runtime_state.py      # 后台续轮读取精确任务进度的结构化运行事实
|   |   |-- control_commands.py        # CLI/IM 共用 typed status/btw/stop/goal 与状态渲染
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
|   |-- tooling/                       # 工具注册、执行、写入边界、结构化错误出口
|   |   |-- capabilities_tool.py      # 从真实工具目录与唯一 channel registry 投影模型能力
|   |   |-- _persona_write_guard.py   # SOUL/USER/AGENTS 统一强制走 update_persona
|   |   |-- process_registry.py       # 前后台命令完整后代树终止的唯一进程入口
|   |   |-- shell.py                  # run_command、超时/中断与有界 pipe drain
|   |   |-- tool_input_completion.py # 明示安全默认值、可信上下文补参与脱敏 source/source_ref
|   |   |-- tool_spec_schema.py       # ToolSpec→provider/runtime 唯一 Schema 与协议同名字段分层
|   |   `-- sandbox.py                # bwrap 唯一策略、自检、worker/K8s readiness 硬门
|   |-- capability/                    # 单一 SkillsService、逐轮 snapshot、能力路由与 capability tools
|   |   |-- skill_service.py           # bounded builtin/shared/owner/workspace discovery、policy 与缓存
|   |   |-- skill_snapshot.py          # 不可变稳定引用、正文 hash/guard 校验与子代理收窄
|   |   |-- persona_repository.py      # owner SOUL/USER/AGENTS 受控加载、版本/CAS/回滚唯一入口
|   |   `-- channel_message_tool.py    # 当前 owner 的统一 send_message；登记产物经原生通道发送
|   |-- prompting_parts/               # prompt 构造
|   |   `-- memory_context.py          # 非权威、可转义且可统一剥离的召回记忆信封
|   |-- scale_downstream.py            # scale worker 复用普通 gateway 会话执行主链
|   `-- backends/                      # 模型后端适配、原生工具历史、JSON/JSON Schema 结构化生成
|-- tests/                             # 单元、集成、真实链路回归
|   |-- test_current_turn_execution.py # 当前轮成功/失败副作用事实投影回归
|   |-- test_memory_hardening.py       # 来源证据、候选、并发去重、hard delete 与信封安全回归
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
|-- memory/long_term/memory.jsonl       # 主代理长期记忆
|-- memory/daily/YYYY-MM-DD.jsonl       # 每日工作记忆
|-- audit/YYYY-MM-DD.jsonl              # raw turn/tool/gateway 黑盒流水
|-- blobs/tool_outputs/                 # 大工具输出正文
|-- tasks/<date>/<task-slug>/           # 当前任务工作区
|   |-- output/                         # 最终交付物
|   `-- work/                           # 状态、日志、子代理账本、过程产物
|-- agents/<run_id>/                    # 子代理 refs-only projection
|-- workspace/runtime/workspaces/<scope>/# LocalStore、gateway、conversation 等 workspace 账本
`-- global_index/                       # 可重建轻量索引

~/.my-agent/shared/                     # 管理员显式发布的公共 skills/tools/role templates；不放 owner 私有资料
```

普通运行不读写 repo 根 `data/*` 作为事实源；测试 fixture 或用户显式配置路径除外。
