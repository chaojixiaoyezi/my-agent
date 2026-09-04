# My-Agent Home Layout

当前主链路只使用 owner home。repo 内 `data/*` 不作为普通运行事实源；测试 fixture 或用户显式配置路径除外。

## Root

```text
~/.my-agent/
|-- shared/                         # shared skills/templates/policies
|-- owners/
|   `-- <provider>/<owner>/          # 每个用户或本地主代理一个 owner home
|-- identity/                        # provider identity 和 canonical user profile
|-- global_index/                    # 可重建全局轻量索引
`-- system/schema_version.json       # home schema 元信息
```

## Owner Home

```text
owners/<provider>/<owner>/
|-- AGENTS.md                        # owner 可读开发/行为说明
|-- SOUL.md                          # owner persona/长期偏好
|-- USER.md                          # 用户资料
|-- memory.md                        # 记忆入口，可引用 lessons/routing
|-- memory-hot.md                    # 极短高频规则
|-- memory/
|   |-- long_term/memory.jsonl       # 正式长期记忆
|   |-- ops.jsonl                    # 模型候选与无正文操作审计，不参与召回
|   |-- daily/YYYY-MM-DD.jsonl       # 每日工作记忆
|   |-- lessons/*.md                 # 较长经验
|   `-- routing/INDEX.md             # 人类可读路由索引
|-- audit/YYYY-MM-DD.jsonl           # raw turn/tool/gateway 黑盒流水
|-- tasks/<date>/<task-slug>/
|   |-- output/                      # 最终交付物；显式外部交付时保存索引/验收记录
|   `-- work/                        # 状态、timeline、草稿、子代理账本
|-- agents/<run_id>/                 # refs-only projection
|-- compact/conversations/           # 主代理 thread compact 事件账本
|-- capability_requests/             # owner 级能力/工具/权限申请
|-- temporary_grants/                # 临时授权账本
|-- data/artifact_backups/v1/        # 前台 shell 真正改动 ready 产物时保留的 owner 私有恢复 blob
|-- workspace/runtime/workspaces/    # LocalStore/gateway/conversation/collaboration
|-- permissions.json                 # owner 权限
|-- quota.json                       # owner 额度
|-- retention.json                   # owner 留存策略
|-- skill_policy.json                # owner skill 策略
`-- tool_policy.json                 # owner tool 策略
```

## Task Workspace

```text
tasks/<date>/<task-slug>/
|-- .agent_delivery/                 # 当前任务系统验收报告，如 closeout.json
|-- data/artifacts/registry.jsonl     # 当前任务产物结构化账本；可登记外部用户目录产物
|-- output/                          # 最终交付物
`-- work/
    |-- .agent_delivery/             # 只读/本地进展等运行中软提示状态
    |-- run_workspace.json           # 当前任务目录身份；复用目录只认它
    |-- state.json                   # 主代理 task 状态
    |-- timeline.jsonl               # 本任务多轮运行时间线
    |-- refs/artifacts/manifest.json # 当前任务最终产物索引和验收引用
    |-- blobs/tool_outputs/          # 当前 run 的大工具输出正文
    |-- artifacts/                   # manifest 和任务级 artifact refs
    |-- guidance/                    # 运行中补充提示投影
    `-- agents/<run_id>/             # 子代理 task-local 家
```

如果用户明确指定普通输出目录，最终报告可以写到用户目录；当前 task `output/` / `work/` 仍记录本轮索引、验收和过程证据。
用户让主代理读取、分析或扫描的相对源码/资料路径默认相对真实 workspace/cwd；task workspace 不是输入路径默认根。
`task-slug` 来自结构化 `task_title/task_name` 或当前用户请求的短标题；`request_id/task_id/run_id`
只作为机器身份和撞名后缀，永不直接成为目录标题。默认确定性提取保留中文、项目名和路径 basename；
可显式开启 通道运行时 风格的一次性 LLM 短标题（最多读取 2000 字符、JSON 短输出、严格清洗），任何
超时、空答或坏格式都回退确定性标题。模型取名默认关闭，避免普通 IM 首次响应多一次隐藏模型调用。
`work/state.json` 和 `work/task.yaml` 只是当前任务状态/可读说明，不再作为旧目录身份兜底，避免旧 run 污染当前任务目录。

## Subagent Workspace

```text
work/agents/<run_id>/
|-- canonical_state.json             # 子代理权威状态
|-- state.json                       # 当前可读状态镜像
|-- timeline.jsonl                   # 子代理本轮事件
|-- events.jsonl                     # 子代理审计事件
|-- artifacts.jsonl                  # 子代理产物 refs
|-- memory_archive/                  # 本 run 的工具归档/恢复事实；不再保存 durable Compact generation
|-- memory/hooks/YYYY-MM-DD.jsonl    # task-local recovery snapshot；不进入长期记忆
|-- context_bundle.json              # 执行上下文 refs
|-- checkpoint.json                  # 当前任务断点
|-- summary.md                       # 可读摘要
`-- final_report.md                  # 子代理内部报告；不是用户最终交付
```

子代理没有长期个人记忆。它的任务状态、工具归档和候选经验仍落在自己的
`work/agents/<run_id>/`；会话历史与 Compact 则按 `agent_thread_id` 落在同 owner 的 canonical
`workspace/runtime/.../conversations/`，与父/兄弟 thread 隔离。值得长期保留的经验仍先进入 task-local
candidate，由父级/root 显式 review/export。

## Runtime Workspace

```text
workspace/runtime/workspaces/<workspace-scope>/
|-- local_store/                     # SQLite/FTS/files
|-- gateway/                         # gateway request/response/lease/worker 状态
|-- conversations/                   # thread/guidance/wake events
|-- collaboration/                   # 协作控制面
`-- adapters/                        # 外部通道运行态
```

## Rules

- owner home 是普通运行唯一事实源。
- 远程私聊 owner 固定为 `owners/providers/<provider>/users/<user_id>`；群聊 owner 固定为
  `owners/providers/<provider>/groups/<chat_id>`。群聊归属只读 adapter 提供的结构化 `chat_type/chat_id`，
  不按首个发言人建用户目录，也不从自然语言或 conversation 字符串猜。
- prompt 的 `primary_workspace_root`、文件工具和 shell 共用同一有效工作区。owner-scoped 写入只能落在
  owner home 或本轮结构化授权的外部输出根；`~/.my-agent/service-cwd` 等顶层公共读取区不能被绝对路径
  或 shell `working_dir` 升级成可写挂载。
- root 部署下，本地管理员为读取 `/root/my-agent-src` 可在无 owner scope 时使用宿主 home；远程 owner
  不能继承这项放宽，读取 `/root/secret` 等 owner home 之外的宿主路径仍由 dangerous roots 拒绝。
  它自己的 `~/.my-agent/owners/...` 先经过精确 owner 白名单放行，不依赖扩大 `/root` 权限。
- global index 和 owner projection 可重建，不替代正文事实。
- raw audit 和 tool output 只给审计、恢复和检索，不直接进入 prompt 大正文。
- shell 产物恢复副本只落 `data/artifact_backups/v1/`，registry 保存 owner-local opaque ref；不得放进 task
  项目树或保留可被测试/打包工具识别的原文件名。无变化命令的预备份必须在当次复核后删除。
- `output/` 放最终交付；`work/` 放过程、日志、结构化任务状态、子代理和验收记录。
- dangerous roots 继续由安全策略拦截；普通用户指定输出目录不靠 broad allowed-write-roots
  白名单兜住。子代理 runner 或内部工具调用一旦显式传入 `allowed_write_roots`，该字段就是
  当前 run 的正向写入边界，用户指定输出目录需要被明确放入边界后才能写。
- 同一正向边界同时进入文件工具和进程工具。对子代理，owner home 在 bwrap 中只读，只有
  `allowed_write_roots` 精确列出的 task/output/work 根可写；`run_command`、后台命令、PTY 与 LSP
  不能借 shell 重定向或长驻进程写到 sibling task、owner 根项目或其他未授权目录。
