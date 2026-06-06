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
|   |-- daily/YYYY-MM-DD.jsonl       # 每日工作记忆
|   |-- lessons/*.md                 # 较长经验
|   `-- routing/INDEX.md             # 人类可读路由索引
|-- audit/YYYY-MM-DD.jsonl           # raw turn/tool/gateway 黑盒流水
|-- blobs/tool_outputs/              # 大工具输出正文和索引
|-- tasks/<date>/<task-slug>/
|   |-- output/                      # 最终交付物；显式外部交付时保存索引/验收记录
|   `-- work/                        # 状态、timeline、compact、草稿、子代理账本
|-- agents/<run_id>/                 # refs-only projection
|-- compact/                         # by_task/by_run/by_agent 轻量指针
|-- capability_requests/             # owner 级能力/工具/权限申请
|-- temporary_grants/                # 临时授权账本
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
|-- output/                          # 最终交付物
`-- work/
    |-- run_workspace.json           # 当前任务目录身份；复用目录只认它和 task.yaml
    |-- state.json                   # 主代理 task 状态
    |-- timeline.jsonl               # 本任务多轮运行时间线
    |-- refs/artifacts/manifest.json # 当前任务最终产物索引和验收引用
    |-- compact/                     # task rollup 和 compact 包
    |-- artifacts/                   # manifest 和任务级 artifact refs
    |-- guidance/                    # 运行中补充提示投影
    `-- agents/<run_id>/             # 子代理 task-local 家
```

如果用户明确指定普通输出目录，最终报告可以写到用户目录；当前 task `output/` / `work/` 仍记录本轮索引、验收和过程证据。
`work/state.json` 只是运行状态，不再作为旧目录身份兜底，避免旧 run 污染当前任务目录。

## Subagent Workspace

```text
work/agents/<run_id>/
|-- canonical_state.json             # 子代理权威状态
|-- state.json                       # 当前可读状态镜像
|-- timeline.jsonl                   # 子代理本轮事件
|-- events.jsonl                     # 子代理审计事件
|-- artifacts.jsonl                  # 子代理产物 refs
|-- compact/                         # 子代理 compact 包
|-- context_bundle.json              # 执行上下文 refs
|-- summary.md                       # 可读摘要
`-- final_report.md                  # 子代理内部报告；不是用户最终交付
```

子代理没有长期个人记忆。值得保留的经验先进入 task-local candidate，由父级/root 显式 review/export。

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
- global index 和 owner projection 可重建，不替代正文事实。
- raw audit 和 tool output 只给审计、恢复和检索，不直接进入 prompt 大正文。
- `output/` 放最终交付；`work/` 放过程、日志、compact、子代理和验收记录。
- dangerous roots 继续由安全策略拦截；普通用户指定输出目录不靠 allowed-write-roots 白名单兜住。
