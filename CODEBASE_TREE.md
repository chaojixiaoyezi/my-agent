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
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- gateway_process.py              # gateway 进程入口
|   `-- _*.py                           # CLI 子命令实现
|-- skills/builtin/<category>/<name>/   # 内置知识型 skill 树：目录即分类（research/documents/…），递归扫描，类目索引常驻 prompt，skill_search 工具按需检索（千级地基）
|-- agent/
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- agent_core/                     # 主代理运行时、工具循环、编排工具、closeout
|   |   |-- runtime/                    # guidance、wait policy、loop support
|   |   |-- tool_loop/                  # 工具轮次执行、恢复、完成判断
|   |   |-- tool_context/               # 工具结果上下文：reducer、窗口、microcompact、PTL 单轮重试
|   |   |-- orchestration/              # create/dispatch/cancel/inspect 子代理工具实现
|   |   |-- delivery_closeout/          # 交付验收和收口
|   |   |-- delivery_closeout/expected_outputs_gate.py # 产物类型/数量对账门：声明驱动核对交付区实存
|   |   |-- delivery_closeout/source_volume.py # 来源比例观测：检索量 vs 交付量并排数字（纯观测零判定）
|   |   |-- tool_loop/final_exit_contract.py # run 出口合同：未收口任务态必走 closeout+续航双闸
|   |   |-- tool_loop/failure_only_exit.py # 当前 request 全工具终态阻断时丢弃无证据结论
|   |   |-- run_learning_review.py     # run 收尾自学习复盘钩子（教训进 drafts 待审，默认关闭）
|   |   |-- tool_loop/exit_orphan_recovery.py # 出口孤儿回收：未收口退出前终止后台子代理进程并 requeue
|   |   `-- runner/                     # 子代理 runner prompt/worker/session/timeout
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
|   |-- user_space/                    # owner home、task workspace、policy、doctor、retention
|   |-- memory_store/                  # 长期记忆 JSONL 和 daily memory
|   |-- memory_archive/                # compact、audit、tool output artifact、task workspace refs
|   |-- local_storage/                 # SQLite/FTS/文件事实源
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |-- conversation/                  # 通道会话账本、权威 transcript、结构化任务关联/续接
|   |   |-- authority.py               # 标记会话 transcript 为当前多轮对话唯一事实源
|   |   `-- task_promotion.py          # 任务工具触发提升、候选选择与完成关闭
|   |-- adapter/
|   |   `-- delivery.py                # 通道长任务结果的持久化异步回送与重启去重
|   |-- settings/                      # AgentConfig、加载、来源账本、runtime scope config
|   |-- common/                        # 跨域小权威：safe_id、path_normalize、json_io、日志脱敏、结构化输出批处理
|   |-- concurrency/                   # 重试/退避（jittered backoff）、锁、per-thread 协作中断
|   |-- owner_object_store.py          # scale owner PG/RLS manifest + versioned S3，Pod 盘只作缓存
|   |-- scale_runtime.py               # scale role/release channel/S3 配置 fail-closed
|   |-- continuous_monitor_entry.py    # 真实 wall-clock 异构来源 proof 长守入口
|   |-- contracts/                     # 稳定协议、错误分类（taxonomy+provider 九类分类器）、验收合同
|   |-- tooling/                       # 工具注册、执行、写入边界、结构化错误出口
|   |   |-- _persona_write_guard.py   # SOUL/USER/AGENTS 统一强制走 update_persona
|   |   `-- sandbox.py                # bwrap 唯一策略、自检、worker/K8s readiness 硬门
|   |-- capability/                    # 能力配置、技能树扫描/路由、skill_search 工具
|   |-- prompting_parts/               # prompt 构造
|   |-- scale_downstream.py            # scale worker 复用普通 gateway 会话执行主链
|   `-- backends/                      # 模型后端适配、原生工具历史、JSON/JSON Schema 结构化生成
|-- tests/                             # 单元、集成、真实链路回归
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
|   `-- work/                           # 状态、日志、compact、子代理账本、过程产物
|-- agents/<run_id>/                    # 子代理 refs-only projection
|-- workspace/runtime/workspaces/<scope>/# LocalStore、gateway、conversation 等 workspace 账本
`-- global_index/                       # 可重建轻量索引
```

普通运行不读写 repo 根 `data/*` 作为事实源；测试 fixture 或用户显式配置路径除外。
