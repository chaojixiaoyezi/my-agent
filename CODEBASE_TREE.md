# Codebase Tree

这份树只描述当前主链路。旧迁移入口、过渡计划和已删除模块不在这里保留。

```text
agent_py_agent/
|-- __main__.py                         # python -m agent_py_agent CLI 入口
|-- config/                             # 默认 YAML 配置
|-- cli/                                # 命令行、chat/TUI、gateway 管理、诊断维护命令
|   |-- chat.py                         # 本地 chat 入口
|   |-- chat_parts/                     # TUI、gateway client、stream/render worker
|   |-- home_runtime_commands.py        # owner home 状态、daily/task workspace/index 维护命令
|   |-- gateway_process.py              # gateway 进程入口
|   `-- _*.py                           # CLI 子命令实现
|-- agent/
|   |-- core.py                         # SimpleAgent 组合入口
|   |-- agent_core/                     # 主代理运行时、工具循环、编排工具、closeout
|   |   |-- runtime/                    # guidance、wait policy、loop support
|   |   |-- tool_loop/                  # 工具轮次执行、恢复、完成判断
|   |   |-- orchestration/              # create/dispatch/cancel/inspect 子代理工具实现
|   |   |-- delivery_closeout/          # 交付验收和收口
|   |   `-- runner/                     # 子代理 runner prompt/worker/session/timeout
|   |-- subagents/
|   |   |-- manager.py                  # 子代理 root manager：初始化、基础生命周期、服务组合
|   |   |-- kernel.py                   # 子代理树快照
|   |   |-- manager_work_orders.py      # 工单路径、默认文件、校验
|   |   |-- models.py                   # 子代理数据模型
|   |   |-- services/                   # 子代理业务服务
|   |   |   |-- base.py                 # create_run/split/owner/runtime config scope
|   |   |   |-- persistence/            # canonical state、projection、index 同步
|   |   |   |-- dispatch/               # dispatch/watch/parent planner 报告
|   |   |   |-- runner_context/         # 执行上下文和边界文件
|   |   |   |-- runner_result/          # runner result 写回和副作用
|   |   |   |-- board/                  # board、due-check、action-plan
|   |   |   |-- actions/                # action-plan 应用、取消/接管动作
|   |   |   |-- hierarchy/              # 多层调度和恢复包
|   |   |   |-- patch_apply/            # patch review/apply/report
|   |   |   |-- capabilities/           # 能力请求、grant、gap、路由
|   |   |   `-- memory_gate/           # task-local 经验候选审核
|   |   |-- patch/                     # patch review/apply 底层实现
|   |   |-- execution/                 # 测试执行和记录
|   |   `-- static_site/               # 静态站点检查
|   |-- user_space/                    # owner home、task workspace、policy、doctor、retention
|   |-- memory_store/                  # 长期记忆 JSONL 和 daily memory
|   |-- memory_archive/                # compact、audit、tool output artifact、task workspace refs
|   |-- local_storage/                 # SQLite/FTS/文件事实源
|   |-- gateway_parts/                 # gateway request/worker/lease/http/renderer
|   |-- settings/                      # AgentConfig、加载、来源账本、runtime scope config
|   |-- contracts/                     # 稳定协议、错误分类、验收合同
|   |-- tooling/                       # 工具注册、执行、写入边界
|   |-- capability/                    # 能力配置、技能、路由
|   |-- prompting_parts/               # prompt 构造
|   |-- backends/                      # 模型后端适配
|   `-- log_analysis/                  # 日志分析子域
|-- tests/                             # 单元、集成、真实链路回归
docs/
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
