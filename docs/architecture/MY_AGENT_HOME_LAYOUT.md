# My-Agent 多用户 Memory / Compact 家目录设计 V2

```text
schema_version: my-agent-home.v2
status: design
updated_at: 2026-06-01
```

当前代码落地状态：

```text
已落地：
- home 初始化会创建 shared / owners / identity / global_index / system 五个顶层分区。
- 本地 CLI 主 owner 默认落在 owners/local/main。
- owner 级 permissions / quota / retention / skill_policy / tool_policy 会生成种子文件。
- system/schema_version.json 会写入 my-agent-home.v2 元信息。
- 保存型主代理 run 在第一轮模型调用前就会创建 task workspace，并把当前 `output/` / `work/` 作为软运行状态注入 prompt；`output/` 只放最终交付物，`work/` 放任务状态、日志、任务级 compact、协作、草稿和下级代理账本。主代理自己的 memory/compact/logs 仍归当前 owner，不进入 `work/agents`。
- daily memory 增加了独立的每日工作记忆事件 API，raw archive 仍保留黑盒流水。
- owner resolver 已有第一片：local/main、provider user、provider group 都能解析到 V2 owner home。
- 旧 `data/users` 路径推导已收敛到 `legacy_user_paths.py`，只服务迁移和关闭 owner-home runtime 后的兼容模式；正常运行入口不再暴露 `user_space.paths` 这种容易误解成新模型的名字。
- prompt 家目录上下文优先读取 owner_home 下的 AGENTS/SOUL/USER/memory，旧顶层文件只做兼容。
- SimpleAgent 的 daily memory mirror 默认写入 owner memory/daily。
- fresh install 下，SimpleAgent 的活跃运行事实源默认使用当前 owner home。任务交付和过程文件进入 `owner_home/tasks/<date>/<task-slug>/{output,work}/`；workspace 级运行账本按当前 checkout 分区到 `owner_home/workspace/runtime/workspaces/<workspace-scope>/`，例如 LocalStore、gateway、conversation、collaboration、adapter 和默认 subagent locator。SimpleAgent 启动后会把这些解析后的 owner-home 路径回写到 `AgentConfig`，让旧 session/gateway/notification 入口也读取同一套事实源，而不是继续使用 `data/*` 默认值。`owner_home/agents/<run_id>/` 只保存 refs-only projection，方便 tree/compact/跨 session 查找，不作为全局活跃工单池。repo 内默认 `data/*` 不再是活跃事实源，只作为关闭 home runtime 或历史迁移时的兼容入口；如果用户/测试显式配置成非默认运行路径，则按显式配置落盘。运行时如果回退到旧路径，`SimpleAgent.using_legacy_paths` 和 `runtime_path_resolution.reason` 必须可见，并写 warning。
- live raw archive、runtime_fact、收尾归档和 token ledger 已优先写入当前 owner home。
- memory-doctor / home-status 会报告 V2 owner、shared、identity、system、schema、legacy 迁移提示、悬空 index、owner compact 指针断裂和 retention 候选；doctor 现在会把 finding 归成 `auto_repair` / `warn` / `manual` 三类 repair plan，给出显式维护命令或处理说明，但不会自动修改文件、不会阻断普通任务。
- home-status 会直接显示当前 owner identity；memory-resume / memory-archive-list / memory-archive-search 会按当前 owner home 查 archive，provider user/group 不读 local/main 归档。
- home-migrate 已有非破坏性复制命令：把旧 long-term `data/memory.jsonl`、daily、raw、hooks、task workspace 复制到 owner home，不删除、不覆盖旧数据。
- home-retention 已有显式维护命令：默认 dry-run，只列候选；`--apply` 才删除过期文件并写 owner audit log。
- provider identity 已有按 provider 分片的 JSONL 索引；canonical user profile 使用目录，不和绑定记录文件混用。
- task/run/agent compact 包已有共享基础布局 helper，基础文件统一为 compact_context、handoff_summary、work_state_snapshot、continue_packet、refs、metadata。
- compact 注入模板已有代码入口，会把 compact_context 和 continue_packet 渲染成同一份续接提示，避免三层 compact 各自拼 prompt。
- owner policy / quota / retention 已有 bundle、磁盘用量统计、effective policy 快照和 retention 计划/显式清理入口；工具注册表会尊重 owner 显式禁用工具和网络开关。
- temporary_grants 已有 owner 级账本，过期只改状态保留审计，不直接改永久 permissions。
- capability_requests 已有过期生命周期，子代理结束后请求仍留在 owner 账本，后续由父代理/用户处理。
- global_index 已有 owner/task/run/agent 最新引用读取和悬空引用检查；索引仍是可重建地图，不替代 task 正文。
- SimpleAgent 启动会登记 owner 轻量索引；保存型 run 会把 owner_id、owner_home 写进 task.yaml/state/timeline/artifact manifest，并同步 owner_home/tasks 下的任务工作区。
- ConversationThread 已可记录 owner_id/owner_home；外部通道创建 thread 时会从当前主代理 owner 注入 owner 归属。
- 子代理任务会继承当前 owner_id、owner policy 快照和父级 shell 权限上限；owner_home/agents/<run_id>/ 会保存 refs-only projection，方便跨 session/tree/compact 查找。
- 长期记忆新写入以 owner 为主：主 JSONL 写 `owner_home/memory/long_term/memory.jsonl`，按天摘要写 `owner_home/memory/daily/`；旧 `memory_path` 只作为兼容读取源。
- task 级 compact rollup 已有落地点：新任务目录统一更新 `tasks/{date}/{task_slug}/work/compact/task_rollup.json`，里面包含 status_counts、pending/completed/blocked run ids 和 artifact refs；同目录还会写 `rollups/branch_main_rollup.json`，compact 包 metadata/ledger 会记录 `branch_id`、`parent_compact_id`、`branches.json` 和 `current_branch.txt`。旧 `tasks/<root_id>/compact/...` 只作为迁移期读取兼容。父代理恢复时可以先看任务级汇总，再按 child run refs 深入。
- task workspace 查询和 `memory-resume` 推荐读取路径会直接暴露 `work/compact/task_rollup.json`、latest compact `continue_packet.json` 和 `work_state_snapshot.json`；这只是恢复索引，不是验收门。
- global index 已覆盖 owner/task/run/agent 轻量 refs，并有 dangling ref 检查 helper；索引只做发现，不替代正文。
- owner capability resolver 已有第一版：按 owner/shared/builtin 优先级解析能力短名，同一 run 内缓存解析结果，避免重复确认。
- system/backups 已有 manifest 和 snapshot 两种入口。manifest 只记录保护范围；snapshot 会复制 owner memory/tasks/runs/agents、identity 和 global_index 的元数据/状态文件，并支持 restore dry-run 先列出会覆盖的路径。
- owner 私有 skill candidate 已有草稿账本，只记录候选，不自动安装、不自动提升。
- HOT/路由/lessons 已有第二片：`ensure_my_agent_home()` 会创建 `memory-hot.md`、`memory/routing/INDEX.md` 和 `memory/lessons/*.md`；普通主代理 prompt 会读取 `memory-hot.md`，task-local/control-plane 隔离；`home_memory_notes.py` 提供 HOT 去重追加和 lesson + route index 同步写入；`memory-doctor`、`memory-route`、运行时路由和 auto resume 在项目没有显式 route index 或旧 workspace archive 时，会回退读取当前 home 的索引/owner archive。
- owner 隔离已接到记忆和任务读取面：local/main 继续兼容旧顶层 memory/tasks，provider user/group 默认只读写自己的 owner home；保存型 provider run 只登记 owner_home/tasks，HOT、lesson 和 route index 也优先落当前 owner。
- 配置默认值入口已收敛：只有 `agent/settings/defaults.py` 构造 schema 默认 `AgentConfig()`；其它运行模块必须优先使用加载后的 `agent.config`，兼容兜底只能通过 defaults helper 取默认值。`load_config()` 现在会给加载后的 `AgentConfig` 附上 `config_sources/config_layers` 账本：schema 默认、配置文件和环境变量覆盖都能解释来源。`settings/runtime_scope_config.py` 已提供 owner/workspace/task/run/agent/runtime 作用域覆盖合并器：低优先级 owner/task 覆盖不会覆盖 env 等高优先级来源，运行时强制覆盖必须显式走 runtime layer。运行门数字通过 `RuntimeGuardPolicy` 快照进入 SimpleAgent，主模型回合、工具 gate pipeline、runner retry、子代理 repair 等真实入口优先读取当前 agent policy，不再各自偷偷读默认 YAML。后续各业务入口接 owner/workspace/task/run/agent override 时必须复用同一套 `ConfigLayer` / `EffectiveConfig` 来源账本，不能另起第二套配置解释机制。

未完全接入：
- provider 身份合并的冲突仲裁、能力提升审核仍是后续迁移阶段。
- 部分旧路径兼容字段仍保留；新增状态/索引/投影已经写 owner_home，旧路径只作为历史查找入口。
```

上面是本文档版本。真实落盘的家目录 schema 版本应另存为：

```text
~/.my-agent/system/schema_version.json
```

建议内容：

```json
{
  "schema_version": "my-agent-home.v2",
  "created_at": "2026-05-31T00:00:00+08:00",
  "updated_at": "2026-05-31T00:00:00+08:00",
  "migration_level": 0,
  "compatible_read_versions": ["my-agent-home.v1", "my-agent-home.v2"],
  "writer_version": "my-agent-home.v2"
}
```

这个文件是系统级元数据，不属于任何 owner。升级目录结构时先更新 migration report，验收通过后再更新这里。

## 1. 核心原则

这版设计修正了一个关键点：

```text
公共能力和用户私有数据必须分层。
```

共享的是：

```text
管理员批准的 shared tools
管理员批准的 shared skills
管理员批准的 shared workflows
管理员批准的 shared role templates
policy templates
通用运行框架
```

隔离的是：

```text
memory
raw archive
compact
sessions
tasks
runs
agents
workspace
artifacts
secrets
permissions
trash
```

一句话：

```text
shared 放公共能力，owners 放人、群、他们自己的记忆/任务/私有能力。
```

更准确地说：

```text
shared 不是唯一能力库。
shared 是管理员批准后给大家用的公共货架。
每个 owner 仍然可以有自己的 skills、tools、workflows、role_templates。
```

用户不需要手工维护这些目录。用户只说“以后这类任务按这个办法做”“帮我记住这个工作流”“这个工具以后给这个群用”，由 agent 判断写进 owner 私有能力、项目能力，还是提交给管理员提升到 shared。

## 2. 顶层目录

建议最终目录：

```text
~/.my-agent/
  shared/
    builtin/
    tools/
    skills/
    optional_skills/
    workflows/
  role_templates/
  policy_templates/
  scripts/
  indexes/
    tools.jsonl
    skills.jsonl
    workflows.jsonl
    role_templates.jsonl
    optional_skills.jsonl

  owners/
    local/
      main/

    providers/
      feishu/
        users/
        groups/
      wechat/
        users/
        groups/
      qq/
        users/
        groups/

  identity/
    canonical_users/
      canonical_user_001/
        identity.json
        profile.json
        memory/
    linked_identities.jsonl
    provider_identity/
      feishu.jsonl
      wechat.jsonl
      qq.jsonl

  global_index/
    owners.jsonl
    active_tasks.jsonl
    active_runs.jsonl
    active_agents.jsonl

  system/
    schema_version.json
    config/
    audit/
    metrics/
    doctor/
    backups/
    migrations/
```

说明：

```text
shared/ 是公共能力层，不是所有能力的唯一来源。
owners/ 是每个 owner 自己的家。
identity/ 解决同一个真实人跨飞书/微信/CLI 的绑定。
global_index/ 只做地图，不放正文。
system/ 放系统级配置、审计、迁移和健康检查。
```

`shared/indexes/` 和 `global_index/` 不一样：

```text
shared/indexes/：
  只索引公共能力，例如 shared tools、skills、workflows、role_templates。
  不放用户、任务、run、agent 的私有状态。
  典型文件是 tools.jsonl、skills.jsonl、workflows.jsonl、role_templates.jsonl、optional_skills.jsonl。

global_index/：
  只索引 owner/task/run/agent 的位置和活跃状态。
  不放公共能力正文，也不放 owner 私有正文。
```

大白话：

```text
shared/indexes 是公共货架目录。
global_index 是全系统地图。
```

默认假设本地文件系统，例如 APFS、ext4、xfs。

```text
如果部署到 NFS、SMB、对象存储、分布式文件系统，锁、rename、mtime、原子写都要重新评估。
这类部署不应该直接套用本地文件系统假设。
```

`system/schema_version.json` 的唯一字段定义以前文为准。这里不重复定义，避免字段漂移。

```text
实现时所有 reader/writer 都应该读同一个 system/schema_version.json。
不要在各模块里再写一套 schema 字段名。
```

shared 下面建议分清：

```text
shared/builtin/：系统内置基础能力，随版本发布。
shared/tools/：管理员批准的公共工具。
shared/skills/：管理员批准的公共技能。
shared/optional_skills/：可安装但默认不启用的技能。
shared/workflows/：管理员批准的公共工作流。
shared/role_templates/：管理员批准的公共角色模板。
```

## 3. 主账号也应该是一个 owner

不要让主账号私有目录和公共能力混在同一层。

主账号建议放这里：

```text
~/.my-agent/owners/local/main/
  AGENTS.md
  SOUL.md
  USER.md
  memory.md
  memory-hot.md
  permissions.json
  quota.json
  retention.json
  skill_policy.json
  tool_policy.json
  skills/
  tools/
  workflows/
  role_templates/
  sessions/
  memory/
    routing/
      INDEX.md
    lessons/
      real-tests.md
      compact.md
      subagents.md
      artifacts.md
      open-world.md
  workspace/
  tasks/
  runs/
  agents/
  compact/
  artifacts/
  data/
  logs/
  cache/
  tmp/
  trash/
```

`memory-hot.md` 只放最高频、最短的提醒；详细教训放 `memory/lessons/`，再由 `memory/routing/INDEX.md` 用关键词指向。普通用户主代理可以复用这一套 owner 记忆入口；主代理创建的子代理不另开长期用户记忆，它继承所属 owner 的入口，并把自己的运行记忆、compact 和交付摘要写到自己的 task/run/agent 目录。写入时优先走 `home_memory_notes.py`：短教训去重追加到 HOT，长教训写 lesson 并同步 route index。

公共能力放这里：

```text
~/.my-agent/shared/builtin/
~/.my-agent/shared/tools/
~/.my-agent/shared/skills/
~/.my-agent/shared/optional_skills/
~/.my-agent/shared/workflows/
~/.my-agent/shared/role_templates/
```

这样代码不需要记住“顶层有些目录是公共的，有些目录是主账号私有的”。

主账号自己写的能力默认不要直接放 shared。

```text
owners/local/main/skills/        主账号私有技能
owners/local/main/tools/         主账号私有工具
owners/local/main/workflows/     主账号私有工作流
owners/local/main/role_templates/主账号私有角色模板
```

只有明确发布/审核/提升后，才进入：

```text
shared/skills/
shared/tools/
shared/workflows/
shared/role_templates/
```

大白话：

```text
管理员自己用的东西也是私有的。
管理员批准给大家用的东西才叫 shared。
```

## 4. Owner 类型决策矩阵

| 场景 | owner 类型 | memory 写哪里 | 是否能用 shared 能力 | 是否能读别人 memory |
| --- | --- | --- | --- | --- |
| 本地 CLI 主账号 | `local_user` | `owners/local/main/` | 可以 | 不可以 |
| 飞书个人用户 | `provider_user` | `owners/providers/feishu/users/<id>/` | 按权限可以 | 不可以 |
| 微信个人用户 | `provider_user` | `owners/providers/wechat/users/<id>/` | 按权限可以 | 不可以 |
| 飞书群 | `provider_group` | `owners/providers/feishu/groups/<id>/` | 按群权限可以 | 不可以 |
| 微信群 | `provider_group` | `owners/providers/wechat/groups/<id>/` | 按群权限可以 | 不可以 |
| 子代理 | `agent_run` | 所属 owner 的 task/agent workspace | 继承父级允许能力 | 不可以 |
| 孙代理 | `agent_run` | 所属 owner 的 task/agent workspace | 继承父级允许能力 | 不可以 |

群空间规则：

```text
群 memory 只写群级事实。
群内某个人的私人偏好不能直接写群 memory。
群里多人意见冲突时，记录成 group finding 或 decision，不覆盖个人 memory。
```

群 memory 写入规则：

```text
preference 类：append 多条，带 user/provider/source/time，不自动互相覆盖。
decision 类：必须带 decision_by、decision_method、effective_at。
finding 类：必须带 evidence_ref 或来源说明。
policy 类：按群管理员/owner policy 更新，普通成员不能直接覆盖。
```

例子：

```text
A 说喜欢简洁回复，B 说喜欢详细回复。
不要写成 group_style=简洁 或 group_style=详细。
应写成两条 preference，并在当前 session 由 agent 根据发言人/上下文选择。
```

## 5. 跨 Provider 身份绑定

同一个真实用户可能同时使用飞书、微信、CLI。

默认不自动合并，必须显式绑定。

目录：

```text
~/.my-agent/identity/
  canonical_users/
    canonical_user_001/
      identity.json
      profile.json
      memory/
  linked_identities.jsonl
  provider_identity/
    feishu.jsonl
    wechat.jsonl
    qq.jsonl
```

示例：

```json
{
  "canonical_user_id": "canonical_user_001",
  "linked": [
    {"provider": "feishu", "provider_user_id": "ou_xxx"},
    {"provider": "wechat", "provider_user_id": "wx_yyy"},
    {"provider": "cli", "provider_user_id": "local-main"}
  ],
  "created_by": "user_confirmed",
  "created_at": "2026-05-31T12:00:00+08:00"
}
```

`identity/provider_identity/<provider>.jsonl` 是 provider 身份索引，不是 provider 配置。

每行建议：

```json
{
  "provider": "feishu",
  "provider_user_id": "ou_xxx",
  "provider_group_id": "",
  "owner_id": "owner_feishu_user_ou_xxx",
  "owner_home": "owners/providers/feishu/users/ou_xxx",
  "canonical_user_id": "canonical_user_001",
  "status": "active",
  "updated_at": "2026-05-31T12:00:00+08:00"
}
```

用途：

```text
收到飞书/微信/QQ/CLI 请求时，先通过 provider_identity 找 owner_home。
如果绑定了 canonical user，再按 canonical 规则读取可共享偏好。
如果 provider_identity 缺失但 owner home 存在，doctor 可从 owner home 反向重建索引。
```

原则：

```text
未绑定时，不同 provider 用户完全隔离。
绑定后，可以共享 canonical user 级偏好和长期记忆。
provider 自己的会话、群消息、附件和权限仍保留在 provider owner home。
解绑必须写审计。
```

绑定后不要把各 provider 的 memory 互相覆盖。

canonical user 级绑定记录和 memory 放在同一个目录下，避免一处是文件、一处是同名目录。

```text
identity/canonical_users/canonical_user_001/
  identity.json
  profile.json
  memory/
    long_term/
      facts.jsonl
      preferences.json
      lessons.jsonl
  conflicts/
    memory_conflicts.jsonl
```

合并规则：

```text
provider owner memory 继续保留原样。
canonical memory 只保存已经确认可跨 provider 复用的偏好、事实和经验。
冲突不自动覆盖，写入 conflicts/。
agent 可以用自然语言提示用户确认，以后以哪个为准。
```

例子：

```text
CLI 里 language=zh，飞书里 language=en。
不要互相覆盖。
写成冲突候选，等用户确认：
  - 全局偏好用 zh
  - 飞书会话里用 en
  - 或者按会话语言自动选择
```

解绑规则：

```text
解绑不删除 provider owner memory。
canonical memory 默认保留在 canonical user 下并标记 inactive link。
如用户要求拆分，可导出 canonical 记忆副本到各 provider owner，但必须写审计。
```

权限不随 canonical identity 自动取最高。

```text
canonical user 只解决“是不是同一个人”和“哪些偏好可共享”。
实际执行权限仍按当前 session 的 provider owner / group owner / task owner 解析。
```

## 6. 用户 home 内部结构

每个 owner home 都长这样：

```text
owner_home/
  AGENTS.md
  SOUL.md
  USER.md
  memory.md
  permissions.json
  quota.json
  retention.json
  skills/
  tools/
  workflows/
  role_templates/
  skill_policy.json
  tool_policy.json

  sessions/
  memory/
  tasks/
  runs/
  agents/
  compact/
  workspace/
  artifacts/
  data/
  logs/
  cache/
  tmp/
  trash/
```

这些顶层文件的职责：

```text
AGENTS.md：这个 owner 的长期工作规则和偏好入口。
SOUL.md：可选的角色风格，不是安全边界。
USER.md：用户画像、称呼、常用语言、常用工作方式。
memory.md：给人和 agent 快速读的记忆摘要入口，正文事实仍以 memory/ 下结构化文件为准。
```

`workspace/` 的用途：

```text
owner_home/workspace/ 是 owner 级临时工作区和默认草稿区。
不绑定具体 task 的临时文件可以先放这里。
一旦进入具体任务，产物、状态、恢复事实应迁到 tasks/<task_id>/、runs/<run_id>/、agents/<agent_id>/。
它不是最终产物库，也不是长期 memory。
```

`compact/` 的用途：

```text
owner_home/compact/ 不保存 task/run/agent 的 compact 正文。
正文 compact 在 tasks/、runs/、agents/ 各自目录下。
owner_home/compact/ 只做 owner 级索引和视图，方便快速查找。
```

建议：

```text
owner_home/compact/
  by_task/
    <task_id>.json
  by_run/
    <run_id>.json
  by_agent/
    <agent_id>.json
```

`owner_home/compact/` 和 `memory/indexes/` 的关系：

```text
owner_home/compact/ 是 compact 专用视图，适合 compact/recovery 直接查询。
memory/indexes/ 不再维护 compact.jsonl，避免同一个 compact 索引双写分裂。
memory/indexes/ 只管 memory/task/run/agent/artifact 的通用地图。
compact 正文仍在 tasks/runs/agents 的 compact 包里。
如果 owner_home/compact/ 指针断裂，以正文 compact 包和 compact_ledger 为准，doctor 只报告并提示重同步，不自动伪造 rollup。
```

Owner 私有能力目录也要和 shared 保持同构，避免以后提升到 shared 时重写结构。

建议：

```text
owner_home/skills/<skill_id>/
  versions/
    1.0.0/
  latest -> versions/1.0.0
  manifest.json

owner_home/tools/<tool_id>/
  versions/
    1.0.0/
  latest -> versions/1.0.0
  manifest.json

owner_home/workflows/<workflow_id>/
  versions/
    1.0.0/
  latest -> versions/1.0.0
  manifest.json
```

大白话：

```text
私有能力和公共能力长得一样。
私有提升成 shared 时，只是复制/审核/改来源，不需要变形迁移。
```

群可以没有 `SOUL.md`，但应该有：

```text
AGENTS.md
GROUP.md 或 USER.md
permissions.json
retention.json
skills/
tools/
workflows/
skill_policy.json
tool_policy.json
```

## 7. Session 和 Task 的关系

必须区分：

```text
session = 聊天窗口 / 飞书 thread / 微信会话
task = 工作项目
run = 某次代理执行
agent = 主代理、子代理、孙代理执行体
```

关系：

```text
一个 session 可以引用多个 task。
一个 task 可以跨多个 session。
一个 task 可以有多个 run。
一个 session 可以同时看到多个 run 的进度摘要。
session 删除不等于 task 删除。
task 完成后仍可被另一个 session 找回。
```

session 目录只放引用和会话状态：

```text
sessions/session_001/
  messages.jsonl
  summary.md
  active_tasks.json
  linked_tasks.jsonl
  latest_state.json
```

`active_tasks.json` 只放 task id 和状态摘要，不嵌套完整 task：

```json
{
  "active_task_ids": ["task_001", "task_002"],
  "last_focus_task_id": "task_002"
}
```

task 正文在：

```text
tasks/task_001/
```

`linked_tasks.jsonl` 和 `active_tasks.json` 的区别：

```text
active_tasks.json：
  当前仍在进行、等待用户、后台运行、或用户最近关注的 task。

linked_tasks.jsonl：
  这个 session 曾经提到、创建、恢复、查看过的所有 task 历史引用。
```

run 与 session 的关系：

```text
run 不必须只属于一个 session。
run 必须属于一个 task。
run 可以由某个 session 触发，也可以由后台 watcher/automation 触发。
session 只保存 run 摘要和引用，不保存 run 正文。
产物挂 task/run/artifact registry，再通过 session 展示给用户。
```

大白话：

```text
session 是聊天窗口。
task 是事情。
run 是某次干活。
产物和恢复事实跟 task/run 走，聊天窗口只负责看见它们。
```

跨 session 找 task 的流程：

```text
1. 先用当前 provider/channel/user/group 找 owner_home。
2. 读当前 session 的 active_tasks.json。
3. 如果当前 session 没有，读 owner_home/memory/indexes/tasks.jsonl。
4. 按 status、updated_at、last_focus、title/goal 相似度排序。
5. 只返回当前 owner 有权限看的 task。
6. 如果候选多个，让 agent 用自然语言问用户要恢复哪一个。
```

`memory/indexes/tasks.jsonl` 不能只有 `task_id -> path`。

建议每行至少带轻量摘要：

```json
{
  "task_id": "task_001",
  "path": "tasks/task_001",
  "status": "running",
  "updated_at": "2026-05-31T12:00:00+08:00",
  "last_focus_at": "2026-05-31T12:00:00+08:00",
  "title": "研究项目架构",
  "goal_summary": "阅读多个项目并写对比报告"
}
```

如果索引缺 title/goal，恢复时先按状态和时间取有限候选，再读取 task.yaml，不要全量打开所有旧任务。

例子：

```text
用户昨天在飞书开了 task_001，今天从 CLI 继续。
CLI session 不一定知道 task_001。
但如果 CLI 身份绑定到同一个 canonical user，agent 可以通过 owner/canonical 索引找到可恢复的 active task。
```

### session summary 生命周期

`sessions/<session_id>/summary.md` 不应该无限堆旧任务。

建议拆成：

```text
sessions/<session_id>/
  summary.md
  current_topic.md
  topic_history.jsonl
  archived_summaries/
```

规则：

```text
当前 task 还没结束：
  summary.md 保留当前工作、下一步、活跃 task/run 引用。

当前 task 已结束：
  把该 task 的 summary 归档到 archived_summaries/。
  从 active_tasks.json 移除已完成 task。
  linked_tasks.jsonl 继续保留历史引用。

用户开启全新话题：
  current_topic.md 切换到新话题。
  summary.md 只保留新话题需要的上下文。
  旧话题不再作为“当前工作/下一步”注入。
```

大白话：

```text
summary 是当前聊天窗口的短期工作台。
task 完成或用户开新话题后，旧工作台要归档，不能一直跟着模型跑。
```

## 8. Memory 目录结构

```text
memory/
  daily/
    2026-05-31.jsonl
  raw/
    2026-05-31.jsonl
  hooks/
    2026-05-31.jsonl
  indexes/
    tasks.jsonl
    runs.jsonl
    agents.jsonl
    artifacts.jsonl
  long_term/
    facts.jsonl
    preferences.json
    lessons.jsonl
  runtime_refs/
    by_request.jsonl
    by_task.jsonl
    by_run.jsonl
```

### daily

轻量日账本。

用途：

```text
今天有哪些任务、run、agent、事件。
```

### raw

接近原始黑匣子。

用途：

```text
出问题后复盘模型和工具过程。
```

### hooks

关键节点快照。

用途：

```text
compact 前、恢复点、gateway 收到请求、runner 收尾。
```

### indexes

轻量地图。

用途：

```text
task_id -> task path + status/title/goal_summary/updated_at
run_id -> run path + task_id/status/updated_at
agent_id -> agent path + task_id/run_id/status/updated_at
compact_id -> compact path + owner/task/run/agent/created_at
artifact_id -> artifact path + owner/task/run/kind/status
```

### long_term

真正长期记忆。

必须带：

```text
scope
source
evidence_refs
confidence
status
ttl / retention
```

### runtime_refs

运行中事实的索引，不放正文。

正文位置：

```text
runs/<run_id>/runtime_facts.json
tasks/<task_id>/state.json
tasks/<task_id>/timeline.jsonl
```

`memory/runtime_refs` 只记录：

```text
request_id -> run_id/task_id/path
task_id -> current run refs
run_id -> runs/<run_id>/runtime_facts.json
```

这里的 `request_id` 指一次外部或内部请求入口：

```text
用户发来的一条消息。
飞书/微信/CLI/gateway 的一次请求。
后台 watcher 唤醒的一次请求。
父代理给子代理的一次调度请求。
```

它不是 HTTP 专用词，也不等同于 task/run。一个 request 可以创建新 task，也可以恢复旧 task，或者只触发一个新 run。

原则：

```text
run 级事实只写一份。
不要同时写 memory/runtime_facts/by_run 和 runs/run_x/runtime_facts.json。
memory 下只放索引，避免双写分裂。
```

一致性规则：

```text
by_request.jsonl 是写入入口索引，记录 request_id 最初绑定到哪个 task/run。
by_task.jsonl 和 by_run.jsonl 是可重建视图。
如果三者冲突，以 runs/<run_id>/runtime_facts.json 和 tasks/<task_id>/state.json 为正文事实源。
doctor 可以从正文事实源重建 runtime_refs。
```

## 9. Task Workspace

```text
tasks/2026-05-31/task_slug/
  output/
    # 最终交付物；复制走这个目录就拿到全部成果。
  work/
    task.yaml
    state.json
    timeline.jsonl
    agent_tree.json
    logs/
    runtime/
    scratch/
    refs/
      artifacts/
        manifest.jsonl
    summaries/
      current_summary.md
    collab/
      blackboard.md
      messages.jsonl
      findings.jsonl
      evidence_packets/
        index.jsonl
    compact/
      compact_ledger.jsonl
      latest -> compact_0003/
      compact_0001/
        compact_context.md
        handoff_summary.md
        work_state_snapshot.json
        continue_packet.json
        refs.json
        metadata.json
      rollups/
        task_rollup.json
```

说明：

```text
output/：最终交付区，代码、PDF、表格、报告等用户要拿走的东西都放这里。
work/task.yaml：任务目标、范围、约束、预算、retention。
work/state.json：任务当前状态。
work/timeline.jsonl：任务级事件流水。
work/agent_tree.json：主/子/孙代理树。
work/collab/：任务内协作白板和证据流水。
work/refs/artifacts/manifest.jsonl：任务级产物索引和引用账本，不替代 output/ 里的交付物。
work/compact/：统一 compact 包 + task 级 rollup。
```

## 10. Task Collaboration Workspace 的边界

```text
tasks/task_001/collab/blackboard.md
```

当前共享决策和约束摘要，适合父代理快速看。

放这里的内容应该是：

```text
用户明确要求
当前目标
当前输出路径
当前只读参考目录
已确认的任务决策
不需要每条都带证据的工作约束
```

```text
tasks/task_001/collab/messages.jsonl
```

append-only 事件流水，记录谁说了什么、谁提交了什么。

```text
tasks/task_001/collab/findings.jsonl
```

结构化发现。

放这里的内容应该是：

```text
观察
推断
结论
需要证据引用的发现
跨代理协作收集到的证据摘要
```

```text
tasks/task_001/collab/evidence_packets/
```

证据包。

大白话：

```text
blackboard 是当前能直接用的任务白板。
messages 是流水。
findings 是带来源/证据的观察和结论。
evidence_packets 是证据。
```

区分标准：

```text
不需要证据引用、只是当前任务怎么做 -> blackboard。
需要说明从哪里看出来、谁提交、哪条日志/文件支持 -> findings。
```

blackboard 到 long_term 的晋升规则：

```text
当前任务约束先放 blackboard。
用户明确说“以后都这样”“记住这个偏好”时，生成 long_term 候选。
同一偏好连续 3 次出现且无冲突时，agent 可以建议写入 long_term。
有冲突时写 memory_conflicts，不直接覆盖。
```

默认阈值：

```text
用户明确说“以后/记住/默认都这样”：1 次即可生成候选。
普通偏好被反复观察到：默认 3 次无冲突后生成候选。
安全、权限、身份、外部系统操作类偏好：不能靠次数自动晋升，必须显式确认。
群偏好：默认记录来源和时间，不直接覆盖成唯一值。
```

例子：

```text
“这个报告用中文写” -> 当前 task blackboard。
“以后你跟我都用中文回复” -> long_term/preferences 候选。
“这个群里安全告警都发中文简报” -> group owner long_term/preferences 候选。
```

## 11. Run Workspace

```text
runs/run_001/
  run.json
  raw.jsonl
  runtime_facts.json
  task_progress.json
  tool_calls.jsonl
  artifacts.jsonl
  compact/
    compact_ledger.jsonl
    latest -> compact_0002/
    compact_0001/
      compact_context.md
      handoff_summary.md
      work_state_snapshot.json
      continue_packet.json
      refs.json
      metadata.json
  final_summary.md
```

run 是某次主代理执行。

同一个 task 可以有多个 run：

```text
run_001 = 第一次开始
run_002 = 后台唤醒继续
run_003 = 用户追加要求后继续
```

`runtime_facts.json` 是 run 级运行事实的唯一正文位置。

```text
不要再在 memory/ 下保存同一份 run facts 正文。
memory 只能索引到这里。
```

## 12. Agent Run Workspace

每个子代理、孙代理等下级执行体都有自己的 agent run workspace。主代理不放在任务 `work/agents/` 里；主代理自己的长期 memory、compact 和日志归当前 owner，例如 `owners/local/main/`。

```text
tasks/2026-06-01/example-task/
  output/
  work/
    agents/agent_sub_001/
      agent.yaml
      parent.json
      state.json
      task.md
      timeline.jsonl
      progress.json
      checkpoint.json
      summary.md
      final_report.md
      findings.jsonl
      artifacts/
        manifest.jsonl
      compact/
        compact_ledger.jsonl
        latest -> compact_0001/
        compact_0001/
          compact_context.md
          handoff_summary.md
          work_state_snapshot.json
          continue_packet.json
          refs.json
          metadata.json
      inbox/
      outbox/
      memory_gate/
        candidates.jsonl
        review_queue.jsonl
  SKILL_SPARKS.md
```

说明：

```text
summary.md：当前工作摘要，运行中会更新。
final_report.md：结束时给父代理的最终交付。
inbox/outbox：agent-local 消息/事件队列，不是用户 session 消息。
memory_gate/SKILL_SPARKS.md：学习候选，不自动进入长期 memory 或正式 skill。
```

`compact/handoff_summary.md` 和 `final_report.md` 的关系：

```text
handoff_summary.md：
  compact 时生成的中间交接快照。
  作用是让接手 agent、父代理或人快速知道“压缩前做到哪了”。

final_report.md：
  agent 完成任务时的最终交付。
  作用是给父代理收口和汇总。
```

如果 agent 跑完前发生过 compact，final_report 可以参考最新 handoff_summary，但不能把它当最终交付。
如果 agent 没发生 compact，可能没有 handoff_summary，但仍应该有 final_report。

### agent inbox/outbox

`inbox/` 和 `outbox/` 是 agent-local 消息队列，不是用户聊天记录。

消息建议统一 JSONL：

```json
{
  "message_id": "msg_001",
  "task_id": "task_001",
  "from_agent_id": "agent_parent",
  "to_agent_id": "agent_sub_001",
  "kind": "instruction | observation | evidence | question | result | handoff",
  "body": "自然语言正文",
  "refs": ["artifact:abc", "finding:def"],
  "created_at": "2026-05-31T12:00:00+08:00",
  "status": "queued | read | handled | archived"
}
```

推送还是轮询由 runtime 决定，但落盘语义不变：

```text
inbox 是别人给我的。
outbox 是我发出去的。
父代理、兄弟代理、任务 shared workspace 都可以是消息目标。
```

compact 后不要复制旧消息正文。

```text
compact 只保留关键消息摘要和 refs。
原始消息仍在 inbox/outbox 或 task collab/messages.jsonl。
```

积压和清理规则：

```text
未读 / 未处理消息不能直接清理。
已处理消息可按 retention 归档。
compact 前应把关键消息摘要写入 compact_context 或 handoff_summary。
如果 refs 指向的原消息会被清理，必须先保留 message_id、摘要、时间、发送者和关键 refs。
```

建议状态：

```text
queued -> read -> handled -> archived
queued/read 不清理。
handled 可归档。
archived 可按 retention 删除原文，但 compact/handoff 中的摘要要保留。
```

### memory_gate / SKILL_SPARKS

触发条件：

```text
任务结束时
重复出现同类操作时
用户说“以后就这么做”时
agent 发现某个做法值得沉淀时
```

审核者：

```text
owner 私有能力：owner 或父代理确认即可。
shared 公共能力：管理员/策略审核。
```

积压处理：

```text
长期没人确认的候选标记 stale。
超过 retention 后归档到 .archive，不直接删除。
```

agent 默认是 task-scoped。

```text
子代理不跨 task 复用。
可复用的是 skill / workflow / template，不是 agent 本体。
```

子代理完成后的保留策略：

```text
长期保留：
  summary.md
  final_report.md
  artifacts/manifest.jsonl
  compact/latest
  关键 findings/evidence refs

按 retention 归档：
  timeline.jsonl
  tool_calls/raw refs
  inbox/outbox 历史

可清理：
  tmp/
  cache/
  scratch
  中间大文件副本
```

原则：

```text
不能为了省空间断掉 compact/recovery 链。
也不能永久保留所有 scratch，避免 10000 个子代理后磁盘膨胀。
```

## 13. Task 级 Compact Rollup

如果一个 task 下有 100 个子代理，父代理恢复时不能逐个读 100 个子代理 compact。

所以需要任务级 rollup：

```text
tasks/task_001/compact/
  compact_ledger.jsonl
  latest -> compact_0003/
  compact_0001/
    compact_context.md
    handoff_summary.md
    work_state_snapshot.json
    continue_packet.json
    refs.json
    metadata.json
  rollups/
    task_rollup.json
    branch_main_rollup.json
```

生成时机：

```text
子代理完成一批后
父代理准备汇总前
后台 watcher 发现 task 状态变化后
任务超过一定时间或上下文压力时
人工/主代理显式要求时
```

生成者：

```text
父代理
后台 compactor
专门汇总子代理
```

内容：

```text
已完成 agent
仍运行 agent
阻塞 agent
关键产物 refs
关键 findings refs
下一步建议
```

它是任务级视图，不替代每个子代理自己的 compact。

规则：

```text
task 级 compact 和 run/agent compact 使用同一套 base compact 包。
task_rollup.json 是 task 层专属扩展，不替代 base compact 包。
```

三层 compact 一致性矩阵：

| 层级 | compact_ledger | latest | compact_context | handoff_summary | work_state_snapshot | continue_packet | refs | metadata | rollup |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| task | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 |
| run | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 否 |
| agent | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 是 | 否 |

说明：

```text
rollup 是 task 层专属。
run/agent 可以有 summary，但不叫 task_rollup。
agent compact 不可以少 base 包字段，否则子代理 compact 后续接会和主代理不一致。
```

## 14. Compact 包职责边界

每个 compact 包建议：

```text
compact_0001/
  compact_context.md
  handoff_summary.md
  work_state_snapshot.json
  continue_packet.json
  refs.json
  metadata.json
```

职责：

```text
compact_context.md：
  给 LLM 读的压缩上下文。

handoff_summary.md：
  给人、父代理、接手者快速看的摘要。

work_state_snapshot.json：
  机器事实快照，记录目标、状态、进度、blockers、已知 refs。

continue_packet.json：
  下一轮续接指令包，告诉模型“现在先做什么、不要重复什么”。

refs.json：
  外部事实源引用清单，例如 raw、hooks、artifact、tool output、task state。

metadata.json：
  compact_id、parent_compact_id、branch_id、created_at、owner、trigger。
```

Compact 注入 prompt 时必须走统一模板，不允许各处自己拼。

读取顺序：

```text
1. 先注入 compact_context.md：
   让模型知道当前任务背景、用户要求、已有进展、重要引用。

2. 再注入 continue_packet.json 的自然语言渲染：
   告诉模型下一步先做什么、哪些不要重复、哪些产物/证据仍缺。

3. 最后注入 refs.json 的短索引：
   只列可继续读取的引用，不展开大文件。
```

实际注入模板建议：

```text
你正在继续一个被压缩过的任务。不要从头重做，先接着上次进度往下做。

【压缩上下文 compact_context.md】
{compact_context_md}

【下一步续接包 continue_packet】
- 下一步先做：{continue_packet.next_action}
- 已完成：{continue_packet.completed_items}
- 还没完成：{continue_packet.pending_work}
- 不要重复：{continue_packet.avoid_repeating}
- 目标产物：{continue_packet.target_outputs}
- 当前阻塞：{continue_packet.known_blockers}
- 用户中途补充要求：{continue_packet.user_updates}

【可按需读取的引用 refs】
{refs_short_index}

请先用这些信息恢复工作状态。如果信息不够，再按 refs 去读原始材料。
```

`continue_packet.json` 最小 schema：

```json
{
  "schema_version": "continue-packet.v1",
  "next_action": "下一步先做什么",
  "pending_work": ["仍未完成的事项"],
  "completed_items": ["已经完成的事项"],
  "avoid_repeating": ["不要重复做的事项"],
  "active_refs": ["artifact:abc", "finding:def", "raw:ghi"],
  "target_outputs": ["最终要交付的产物或输出位置"],
  "known_blockers": ["当前阻塞原因，没有则为空"],
  "user_updates": ["用户中途追加或修改过的要求"]
}
```

边界：

```text
compact_context.md 是给模型读的故事化上下文。
continue_packet.json 是机器可读的续接计划和防重复清单。
prompt 里可以同时出现二者，但来源和职责必须清楚。
```

blackboard 和 compact_context 的关系：

```text
blackboard 是当前 task 的活白板，任务还在运行时继续独立维护。
compact_context 是某次 compact 时从 blackboard、runtime_facts、progress、refs 等材料提炼出的快照。
compact_context 不替代 blackboard。
compact 后继续运行时，新的任务决策仍写回 blackboard。
下一次 compact 再吸收最新 blackboard 摘要。
```

恢复时优先读 compact_context 和 continue_packet。需要最新任务约束时，再读 task collab/blackboard.md。

如果后续发现 `compact_context.md` 和 `handoff_summary.md` 长期重复，可以合并。但当前先保留，因为消费者不同：

```text
compact_context 给模型继续干活。
handoff_summary 给人和上级快速判断。
```

## 15. Compact 链和分叉

默认 compact 是线性链：

```text
compact_0001 -> compact_0002 -> compact_0003
```

metadata：

```json
{
  "compact_id": "compact_0003",
  "parent_compact_id": "compact_0002",
  "branch_id": "main",
  "cycle_index": 3
}
```

如果从旧 compact 分叉恢复，不能覆盖主链。

应该生成新 branch：

```text
branch_main:
  compact_0001 -> compact_0002 -> compact_0003

branch_recovery_a:
  compact_0002 -> compact_0002a -> compact_0002b
```

大白话：

```text
正常一路往下是线性。
从中间恢复走另一条路，就开新分支。
旧链不删。
```

分叉触发条件：

```text
从 latest compact 继续：不分叉，继续 main branch。
从旧 compact 手动/自动恢复：自动开新 branch。
恢复时发现当前 branch 已经有后续 compact：自动开新 branch，避免覆盖历史。
```

分支状态建议：

```text
compact/
  branches.json
  current_branch
  branch_main/
  branch_recovery_a/
```

`branches.json` 至少记录：

```json
{
  "current_branch": "main",
  "branches": [
    {"branch_id": "main", "head": "compact_0003", "status": "active"},
    {"branch_id": "recovery_a", "head": "compact_0002b", "status": "archived"}
  ]
}
```

rollup 规则：

```text
task_rollup 默认看 current_branch。
分支自己的进展写 branch_<id>_rollup.json。
切换 branch 必须写 timeline/audit。
```

清理规则：

```text
active branch 不清理。
archived branch 按 retention 清理大 raw refs，但保留 metadata、handoff_summary、关键 refs。
```

## 16. 权限模型

权限不能只靠一个扁平布尔值。

建议：

```text
permissions.json
temporary_grants/
capability_requests/
audit_log.jsonl
```

路径归属：

```text
owner_home/temporary_grants/
owner_home/capability_requests/
```

它们是 owner 级账本。每条记录内部必须带 task_id/run_id/agent_id/scope，表示它属于哪件事。

permissions 示例：

```json
{
  "filesystem": {
    "access_mode": "workspace-write",
    "dangerous_paths": ["/", "/etc", "~/.ssh"]
  },
  "shell": {
    "access_mode": "workspace-write"
  },
  "network": {
    "access_mode": "enabled"
  },
  "subagents": {
    "can_create": true,
    "can_manage_children": true
  },
  "tools": {
    "enabled_sources": ["builtin", "shared", "owner", "workspace"],
    "installed_optional_tools": [],
    "allow_owner_override": false
  },
  "skills": {
    "enabled_sources": ["builtin", "shared", "owner", "workspace"],
    "installed_optional_skills": [],
    "allow_owner_override": false
  }
}
```

继承规则：

```text
父级权限是上限。
子代理只能等于或更小。
每个字段分别比较，不用一个整体 trust level 糊住。
```

例如：

```text
父级 shell=workspace-write，子级最多 workspace-write。
父级 network=disabled，子级不能 network=enabled。
父级 subagents.can_create=false，子级不能创建孙代理。
```

`permissions.json`、`quota.json`、`skill_policy.json`、`tool_policy.json` 分工：

```text
permissions.json：管能不能做，是能力边界。
quota.json：管最多能做多少，是资源上限。
skill_policy.json：管哪些 skill 启用、禁用、pin 版本、是否可覆盖。
tool_policy.json：管哪些 tool 启用、禁用、pin 版本、是否可覆盖。
```

数量上限不放在 permissions 里，放 quota。

例如：

```json
{
  "max_subagents": 50,
  "max_depth": 4,
  "max_active_agents": 1000,
  "max_daily_model_calls": 10000
}
```

大白话：

```text
permissions 管有没有资格。
quota 管最多用多少。
两者同时存在时取更严格的结果。
```

最终执行时取更严格的组合：

```text
permissions 说不能做 -> 不能做。
permissions 说能做，但 quota 到上限 -> 暂停/降级/提示。
skill_policy/tool_policy 禁用 -> 不启用。
```

群会话权限解析：

```text
provider 默认权限
个人 owner 权限
群 owner 权限
task 临时授权
```

最终取交集：

```text
群只能收紧，不能放宽个人没有的权限。
个人没有 shell，群不能给他打开 shell。
群禁用了某个外发工具，个人允许也不能在该群会话里用。
临时授权只在指定 task/scope/expires_at 内生效。
```

管理员角色：

```text
默认 owners/local/main 是本机管理员 owner。
可以配置多个 admin owner。
admin 可以审核 shared 能力、默认策略、迁移、backup/restore。
admin 的私人 skills/tools 仍在自己的 owner home，不能天然等同 shared。
```

## 17. 临时授权和能力申请

用户可能临时说：

```text
这次允许你删除这个目录里的临时文件。
```

不要永久改 `permissions.json`。

应该写：

```text
owner_home/temporary_grants/grant_001.json
```

示例：

```json
{
  "grant_id": "grant_001",
  "scope": "task_001",
  "action": "delete_file",
  "path_prefix": "/Users/example/tmp/demo",
  "expires_at": "2026-05-31T13:00:00+08:00",
  "approved_by": "user",
  "status": "active"
}
```

子代理能力不足时，写：

```text
owner_home/capability_requests/request_001.json
```

父代理或用户决定批准、拒绝、改派或让它换方法。

如果 task 需要快速看到自己的申请，可以在 task 目录保留索引：

```text
tasks/task_001/capability_requests.jsonl
```

但正文仍以 owner_home/capability_requests/ 为准，避免同一请求两套事实。

所有权限变化必须写：

```text
audit_log.jsonl
```

能力申请不等于让用户去填技术字段。

正常应该是：

```text
agent 发现缺能力
  -> 生成“我需要什么、为什么、风险是什么”的自然语言说明
  -> 系统把它落成 capability_request
  -> 用户只回答同意/拒绝/换办法
```

如果用户只是普通聊天，不应该要求用户知道 `capability_request`、`skill_policy`、`tool_policy` 这些内部名词。

capability request 生命周期：

```text
open：等待用户、父代理或管理员处理。
approved：已批准，生成 temporary grant 或启用能力。
rejected：明确拒绝。
expired：超过有效期未处理，默认不批准。
superseded：被新的请求替代。
archived：已归档，仅保留审计。
```

默认规则：

```text
请求必须有 expires_at。
用户不回应时不能永久挂起；到期后标 expired。
expired 后 agent 应换方法、降级执行，或向用户说明缺能力。
积压请求由 doctor/retention 汇总，不自动批准。
```

如果发起请求的子代理已经结束：

```text
请求不跟着子代理消失。
它继续保留在 owner_home/capability_requests/ 正文账本里。
task 下如有 capability_requests.jsonl，只是引用索引。
父代理或后续接手 agent 可以看到它，决定忽略、归档、重新申请或换办法。
如果 task 已结束且请求仍 open，doctor 应把它标为 superseded 或 expired。
```

temporary grant 生命周期：

```text
grant_id 由系统生成，建议含 owner_id/task_id/时间/随机后缀。
同一 scope 有多个 grant 时，执行前按 action/path/tool 匹配。
互相冲突时取更严格、更短期的那个。
过期 grant 不删除事实，先标记 expired。
doctor/retention 定期归档 expired grants。
```

grant 只能扩大当前 task 的临时能力，不能永久改 owner 默认权限。

## 18. Shared Skill / Tool 版本

公共能力不能只是一个无版本目录。

建议：

```text
shared/skills/<skill_id>/
  versions/
    1.0.0/
    1.1.0/
  latest -> versions/1.1.0
  manifest.json
```

manifest 至少有：

```text
skill_id
version
owner
source: builtin / shared / owner / workspace / optional
trust_level
required_config
required_permissions
required_tools
compatible_agent_versions
status: active / deprecated / disabled
created_at
updated_at
```

用户可以：

```text
使用 latest
pin 到某个版本
禁用某个 shared skill
```

用户自己创建的 skill 默认在：

```text
owners/.../<owner>/skills/
```

只有审核后才能提升到：

```text
shared/skills/
```

Owner 私有 skill/tool 也使用同样结构：

```text
owners/.../<owner>/skills/<skill_id>/
  versions/
  latest -> versions/<version>
  manifest.json
```

这样 shared 提升只是复制版本目录、改 manifest 来源、补审核记录，不需要转换目录形态。

## 19. Owner 私有能力和 Shared 公共能力

能力来源应该至少有五类：

```text
builtin：系统内置基础能力。
shared：管理员批准的公共能力。
optional：公共可安装能力，默认不启用。
owner：某个用户/群/组织自己的私有能力。
workspace：某个项目目录自己的局部能力。
```

每个 owner 都可以有：

```text
owner_home/skills/
owner_home/tools/
owner_home/workflows/
owner_home/role_templates/
owner_home/skill_policy.json
owner_home/tool_policy.json
```

这些不是让用户手动管理的。正常流程应该是：

```text
用户自然语言提出需求
  -> agent 判断是否已有 builtin/shared/owner/workspace 能力可用
  -> 没有就临时完成任务
  -> 如果值得沉淀，写入 owner 私有 skill/workflow 候选
  -> 如果用户或管理员确认可共享，再提升到 shared
```

大白话：

```text
用户不需要知道 skill 放哪。
用户只说“以后这类事你就这么办”。
agent 决定是记成个人习惯、群工作流、项目局部能力，还是提交公共能力审核。
```

## 20. 能力解析规则

能力 ID 建议带命名空间：

```text
builtin:read_file
shared:github_research
optional:browser_automation
owner:weekly_report_style
workspace:project_release_flow
```

不要靠同名目录静默覆盖。

解析原则：

```text
1. builtin 基础能力不能被静默覆盖。
2. shared 公共能力不能被 owner 私有能力静默覆盖。
3. owner 能力默认只对该 owner 生效。
4. workspace 能力默认只对该 workspace/task 生效。
5. 如果确实要覆盖 builtin/shared，必须有显式 override 记录和审计。
```

如果模型只说了一个短名，例如 `weekly_report`：

```text
先查当前 owner 显式启用的能力。
再查当前 workspace 显式启用的能力。
再查 shared 里是否有唯一匹配。
多处同名时，不猜，返回候选让 agent/用户确认。
```

注意，这不是硬门；它只是避免能力串线。

解析结果要缓存，避免确认风暴。

```text
runs/<run_id>/runtime_facts.json:
  capability_resolution_cache:
    weekly_report:
      resolved_id: owner:weekly_report_style
      version: 1.0.0
      resolved_at: 2026-05-31T12:00:00+08:00
      reason: current owner explicit match
```

规则：

```text
同一 run 内，同一短名解析结果复用。
shared/owner/workspace 有版本更新时，新 run 生效，运行中的 run 继续用已解析版本。
高风险能力即使缓存命中，也要按权限/审批检查。
```

能力风险等级：

```text
low：只读、无外部副作用。
medium：写 workspace 内文件、生成本地产物。
high：发消息、改外部系统、创建工单、调用付费/限额 API。
dangerous：删除、封禁、执行 shell 高危命令、改权限、动生产系统。
```

缓存失效规则：

```text
同一 run 内默认复用解析结果。
如果用户中途明确修改 skill_policy/tool_policy，当前 run 的相关缓存失效。
如果 shared/owner/workspace 能力被 disabled/revoked，当前 run 相关缓存立即失效。
high/dangerous 能力每次执行前都重新过权限/审批，不只看缓存。
普通版本升级默认新 run 生效，不打断当前 run。
```

## 21. Owner 级启用 / 禁用

每个 owner 可以配置哪些 shared/optional 能力可用。

示例：

```json
{
  "enabled_shared_skills": ["shared:github_research"],
  "disabled_shared_skills": ["shared:external_email_sender"],
  "installed_optional_skills": ["optional:browser_automation"],
  "pinned_versions": {
    "shared:github_research": "1.2.0"
  },
  "allow_owner_override": false
}
```

`skill_policy.json` 最小 schema：

```json
{
  "schema_version": "skill-policy.v1",
  "enabled_sources": ["builtin", "shared", "owner", "workspace"],
  "enabled_shared_skills": [],
  "disabled_shared_skills": [],
  "installed_optional_skills": [],
  "pinned_versions": {},
  "allow_owner_override": false
}
```

`tool_policy.json` 最小 schema：

```json
{
  "schema_version": "tool-policy.v1",
  "enabled_sources": ["builtin", "shared", "owner", "workspace"],
  "enabled_shared_tools": [],
  "disabled_shared_tools": [],
  "installed_optional_tools": [],
  "pinned_versions": {},
  "allow_owner_override": false
}
```

默认策略来自：

```text
system/config/default_owner_policy.json
```

建议默认：

```text
builtin 基础能力启用。
shared 安全基础能力启用。
optional 默认不安装、不启用。
owner 私有能力默认只对 owner 可见。
workspace 能力只在对应 workspace/task 生效。
owner override 默认关闭。
```

群也可以有自己的策略：

```text
群 A 可以用安全巡检 skill。
群 B 不能用外部消息发送 tool。
飞书用户 C 可以用自己的私有周报 skill。
```

这点借鉴 长期助手 的 profile/platform 思路：同一套公共能力，不代表每个 profile / provider / 群都默认启用。

## 22. Agent 自动管理能力

用户默认不会主动整理这些目录，所以 agent 要负责：

```text
发现当前任务反复出现的做法。
生成 owner 私有 skill/workflow 草稿。
记录这个能力适合什么场景。
记录用了哪些工具、权限、配置。
提示用户是否以后复用。
用户确认后启用。
长期不用的私有能力归档，不直接删除。
```

建议目录：

```text
owner_home/skills/.drafts/
owner_home/skills/.archive/
owner_home/skills/.usage.json
owner_home/workflows/.drafts/
owner_home/workflows/.archive/
```

`.usage.json` 记录：

```text
use_count
view_count
last_used_at
last_modified_at
status: draft / active / stale / archived / pinned
```

这样用户不用知道 skill 生命周期，但系统能自己保持整洁。

### 各自学习 Skill 的口子

必须保留各自学习 skill 的入口，但不能让它们串线。

学习层级：

```text
agent-local：
  某个主代理/子代理/孙代理这次任务里临时总结的方法。
  位置：agents/<agent_id>/memory_gate/、SKILL_SPARKS.md。
  默认只给这次任务参考，不跨 task 自动复用。

task-local：
  某个 task 中多个代理共同沉淀出的临时做法。
  位置：tasks/<task_id>/collab/findings.jsonl 或 skill_candidates.jsonl。
  默认只给这个 task 和它的后续 run 参考。

owner-private：
  某个用户、群、组织自己的私有 skill/workflow。
  位置：owner_home/skills/.drafts/ 或 owner_home/workflows/.drafts/。
  用户确认后可变成 owner 私有 active skill。

workspace-local：
  某个项目目录自己的工作流或技能。
  位置：workspace 局部能力层。
  只在该 workspace/task 生效。

shared：
  管理员批准后的公共 skill/workflow。
  位置：shared/skills/、shared/workflows/。
  不能由 agent 自动提升。
```

主代理、子代理、孙代理都可以提出 skill 候选，但权限不同：

```text
主代理：
  可以给 owner 生成 skill/workflow 候选。
  可以建议用户确认启用。

子代理/孙代理：
  可以写 agent-local SKILL_SPARKS。
  可以把候选提交给父代理或 task collab。
  不能直接把自己总结的东西写成 owner active skill。
  不能直接提升 shared。

其他用户 / 其他 owner 的 agent：
  只能写自己 owner 的候选。
  不能读写别人的 owner-private skill。
  只能通过 shared 使用管理员批准过的公共能力。
```

候选最小字段：

```json
{
  "candidate_id": "skill_candidate_001",
  "scope": "agent-local | task-local | owner-private | workspace-local | shared-proposal",
  "proposed_by_agent_id": "agent_sub_001",
  "owner_id": "owner_feishu_user_ou_xxx",
  "task_id": "task_001",
  "source_refs": ["run:run_001", "artifact:artifact_001"],
  "when_to_use": "适用场景",
  "steps_summary": "做法摘要",
  "required_tools": ["read_file", "web_search"],
  "required_permissions": ["network:enabled"],
  "risk_notes": "风险说明",
  "status": "draft | proposed | active | rejected | stale | archived"
}
```

大白话：

```text
每个 agent 都能“学到东西”。
但学到的东西先是草稿或候选。
谁能复用，要看它被提升到了哪一层。
别人的私有 skill 不能因为名字一样就被我拿来用。
公共 skill 必须经过 shared 提升流程。
```

## 23. Shared 提升流程

owner 私有能力提升到 shared，不能自动发生。

流程：

```text
owner skill/workflow 草稿
  -> agent 自检
  -> 用户确认“这个可以共享”
  -> 管理员/策略审核
  -> 写 shared/<type>/<id>/versions/<version>/
  -> 更新 shared manifest
  -> 可选同步给其他 owner，但默认不强制启用
```

提升到 shared 后，其他 owner 看到的是公共能力，但是否启用仍由自己的 policy 决定。

共享能力热更新：

```text
新 run 默认解析 latest。
运行中的 run 固定使用启动时解析到的 skill/tool 版本。
如果必须中途切新版，需要显式记录 capability_resolution 变更和原因。
旧版本 deprecated 不等于立刻中断正在运行的任务。
```

紧急撤销 / 安全更新：

```text
如果 shared/owner/workspace 能力被标记 revoked_for_security：
  新 run 禁止解析到该版本。
  正在运行的 run 下次使用该能力前必须重新解析。
  如果继续使用会越权或有安全风险，返回自然语言说明并要求换能力/等待审批。
```

大白话：

```text
普通升级不打断正在干活的 agent。
安全撤销必须立即生效，不能让长任务继续用有漏洞的能力。
```

## 24. 全局索引一致性

全局索引只做地图，不是事实源。

原则：

```text
先写正文，再写索引。
索引写失败，不代表正文失败。
索引必须可重建。
定期 doctor 扫悬空引用。
并发写入要文件锁或原子 append。
```

目录：

```text
global_index/
  owners.jsonl
  active_tasks.jsonl
  active_runs.jsonl
  active_agents.jsonl
  repair_log.jsonl
```

如果索引指向的文件不存在：

```text
doctor 标记 dangling_ref
能从 owner home 重建就重建
不能重建就保留错误记录，不伪造事实
```

doctor 修复策略：

```text
auto_repair：
  索引缺失但正文存在。
  active index 里有已完成任务，能从 task state 确认完成。
  provider identity 分片缺失但 linked_identities 可重建。

warn_only：
  索引指向正文不存在，但不影响当前 active task。
  旧 raw/archive 缺失，但 compact 和 task state 仍能恢复。
  usage/index 统计不一致。

manual_approval_required：
  需要删除 owner 正文。
  需要合并 canonical memory 冲突。
  需要恢复 backup 覆盖当前数据。
  需要把 private skill 提升到 shared。
```

doctor 不应该擅自伪造任务完成状态。

```text
能重建索引就重建索引。
不能确认事实就写 repair_log.jsonl，并给 agent/管理员一个自然语言修复建议。
```

性能规则：

```text
1000 用户以内，JSONL + provider 分片通常够用。
超过 10000 用户或频繁查询，应切 SQLite/嵌入式 KV。
不要把所有 provider identity 都放进一个无限增长的 provider_identity_index.jsonl 后每次全表扫。
provider identity 正文索引放 identity/provider_identity/<provider>.jsonl。
```

## 25. 并发写入控制

同一个 owner 可能多个 session 同时写。

建议：

```text
每个 JSON/JSONL 文件写入走 atomic write 或 append lock。
state.json 这类覆盖写必须带 version / updated_at。
timeline.jsonl 这类流水只 append。
agent_tree.json 可以由 tree service 统一生成，避免多个写者抢写。
```

tree service 定义：

```text
tree service 是任务级状态投影器。
它从 agents/<agent_id>/state.json、timeline.jsonl、run state、heartbeat、artifact manifest 读取事实。
它统一生成 tasks/<task_id>/agent_tree.json。
子代理不直接抢写整棵树，只更新自己的 state/timeline/heartbeat。
```

触发方式：

```text
agent 状态变化时触发。
子代理完成/阻塞/产生产物时触发。
父代理 inspect_agent_tree 时可按需刷新。
后台 watcher 可定时刷新。
```

大白话：

```text
每个代理只写自己的小账本。
整棵树由一个服务汇总，避免大家同时改同一个 agent_tree.json。
```

默认机制：

```text
JSON 覆盖写：同目录 temp file -> fsync -> atomic rename。
JSONL 追加写：append lock -> 写一行 -> fsync 可配置。
temp file 命名：.<filename>.<pid>.<random>.tmp，避免并发冲突。
```

文件系统假设：

```text
本地 APFS/ext4/xfs 上 rename 可作为原子替换基础。
NFS/SMB/对象存储不默认保证同样语义，必须单独评估锁和原子写。
```

最小规则：

```text
append-only 优先。
覆盖写必须原子替换。
索引可重建。
锁粒度按 owner/task/run/agent 文件，不要全局大锁。
```

## 26. Retention / Quota / Trash

每个 owner 有：

```text
retention.json
quota.json
trash/
```

retention 示例：

```json
{
  "raw_days": 90,
  "hooks_days": 180,
  "compact_days": 365,
  "task_completed_days": 365,
  "subagent_scratch_days": 30,
  "trash_days": 30
}
```

quota 示例：

```json
{
  "max_disk_mb": 102400,
  "max_active_tasks": 100,
  "max_active_agents": 1000,
  "max_daily_model_calls": 10000,
  "max_daily_network_calls": 100000
}
```

磁盘配额计入范围：

```text
计入：
  memory/
  sessions/
  tasks/
  runs/
  agents/
  compact/
  artifacts/
  workspace/
  data/
  logs/
  trash/

可按低优先级清理：
  cache/
  tmp/
  old raw
  old subagent scratch

默认不计入或单独统计：
  shared/ 公共能力库
  system/backups/，因为它有独立备份保留策略
```

如果部署方希望总磁盘严格控制，可以把 backups 也纳入系统级 quota，而不是 owner quota。


超配额行为：

```text
先 warning。
再限制新任务或新子代理。
不删除正在运行任务的关键恢复数据。
清理优先级：cache/tmp -> old trash -> old raw -> old subagent scratch。
```

Provider 用户生命周期：

```text
created：首次出现或管理员创建 owner home。
active：正常可用。
suspended：暂停新任务和新 run，但 memory/task 仍保留。
archived：长期不用，停止后台唤醒，保留恢复包和关键 memory。
deleted_soft：移入 trash，可在 retention 内恢复。
deleted_hard：显式确认后清除正文，只保留最小审计记录。
```

本地 CLI 不只有 `main`。

```text
owners/local/main/ 是默认本地 owner。
同一台机器多个本地用户可以是：
  owners/local/user_a/
  owners/local/user_b/
```

不要把 `main` 写死成唯一 CLI 用户。

## 27. 恢复 fallback

恢复不能假设所有文件都完好。

恢复 owner：

```text
1. 通过 provider identity index 找 owner home。
2. 找不到时查 linked identities。
3. 再找 global_index。
4. 仍找不到，返回 owner_not_found，不猜。
```

恢复 session：

```text
1. 读 sessions/<session_id>/latest_state.json。
2. 坏了就读 messages.jsonl 最新尾部。
3. 再读 active_tasks.json。
4. 还不行就让用户选择要恢复哪个 task。
```

恢复 task：

```text
1. 读 tasks/<task_id>/state.json。
2. 坏了读 timeline.jsonl。
3. 再读 compact/latest/compact_context.md 或 handoff_summary.md。
4. 再读 agent_tree.json 或从 agents/ 重建。
5. 再从 memory/raw、memory/daily、global_index 交叉重建。
6. 最后尝试 system/backups 中最近可用备份。
```

恢复 agent：

```text
1. 读 agents/<agent_id>/state.json。
2. 坏了读 checkpoint.json。
3. 再读 compact/latest continue_packet。
4. 再读 timeline.jsonl / raw.jsonl。
```

恢复原则：

```text
能部分恢复就部分恢复。
不能恢复就结构化说明缺什么。
不伪造完成状态。
全部损坏时，不猜；给出可恢复证据、缺失文件和建议人工选择。
```

## 28. 数据备份和迁移

需要支持三类迁移：

```text
单用户旧布局 -> owner home 布局
旧 provider id -> canonical identity 绑定
本机 -> 新机器
```

迁移不是备份。备份/恢复单独处理。

备份目录：

```text
system/backups/
  backup_20260531_120000/
    manifest.json
    owners/
    shared/
    identity/
    global_index/
    checksum.json
```

备份规则：

```text
backup 默认只复制元数据、memory、compact、task state、artifact manifest。
大 artifacts/raw 可以按策略选择是否包含。
restore 必须先 dry-run，列出会覆盖什么。
restore 后跑 doctor 校验索引和 owner home。
```

备份触发条件：

```text
schema_version 升级前。
大规模迁移前。
shared 能力批量更新前。
用户/管理员手动要求。
可选定时备份。
```

定时备份默认建议：

```text
单用户本地模式：
  默认不做完整定时备份。
  schema 升级、迁移、手动要求时备份。

团队/服务模式：
  每天一次轻量备份：metadata、memory、compact、task state、manifest。
  每周一次完整备份：按策略包含 raw/artifacts。

配置位置：
  system/config/backup_policy.json
```

迁移触发条件：

```text
schema_version 不兼容。
代码升级要求迁移 owner home。
provider id 结构变化。
单用户旧布局迁到 owner home。
本机迁到新机器。
```

迁移目录：

```text
system/migrations/
  migration_0001/
    plan.json
    report.json
    rollback_refs.json
```

迁移原则：

```text
先 dry-run。
再 copy，不直接 move。
写 migration report。
校验通过后再标记旧路径 deprecated。
保留 rollback refs。
```

## 29. 迁移阶段验收标准

每个阶段必须有完成标准。

```text
阶段 1 文档和配置收口：
  文档、默认配置、目录 schema version 对齐。

阶段 2 owner resolver：
  CLI、provider user、provider group 都能解析到 owner_home。
  新入口不得绕过 resolver 写全局状态。

阶段 3 memory/raw/hooks 按 owner 写：
  新写入进入 owner_home/memory。
  旧路径只读兼容，有迁移报告。

阶段 4 session/task/run/agent 全部挂 owner：
  session 只存 task/run 引用。
  task/run/agent 正文都在 owner home 下。

阶段 5 task-level compact rollup：
  task/run/agent 都生成统一 base compact 包。
  父代理可从 task_rollup 恢复全貌。

阶段 6 权限和能力申请：
  permissions/quota/policy 分工落地。
  temporary_grants 和 capability_requests 有生命周期。

阶段 7 索引、doctor、retention：
  global_index 可重建。
  doctor 能输出 auto_repair/warn/manual 三类结果。

阶段 8 能力解析和 agent 自动管理：
  builtin/shared/optional/owner/workspace 命名空间可解析。
  同 run 缓存和安全撤销生效。

阶段 9 identity / backup / lifecycle：
  canonical identity、provider owner 生命周期、backup/restore 都可 dry-run 验证。
```

## 30. 加密和敏感数据

memory、sessions、artifacts 可能有敏感信息。

第一版可以先不强制加密，但必须预留：

```text
owner_home/encryption.json
system/keyring/
```

`encryption.json` 最小契约：

```json
{
  "enabled": false,
  "provider": "none",
  "algorithm": "",
  "key_ref": "",
  "scope": ["memory", "sessions", "artifacts"],
  "rotated_at": null
}
```

字段含义：

```text
provider：none / local_keyring / kms / enterprise。
algorithm：例如 age、AES-GCM，未启用时为空。
key_ref：本机 keyring 或 KMS 的引用，不直接放密钥。
scope：哪些目录启用加密。
```

`system/keyring/` 是接口占位，不要求第一版自己实现 KMS。

敏感字段：

```text
token
password
cookie
authorization
api_key
secret
```

进入模型前必须脱敏。

落盘策略：

```text
普通 owner 默认明文本地文件。
企业 / provider 可配置加密层。
secrets 不进 raw archive 正文。
```

隔离等级：

```text
单用户本地模式：
  主要是应用层逻辑隔离。
  owner home 防串线，但不声称能防本机管理员或 root。

可信多用户模式：
  可以共用同一个 my-agent runtime。
  必须按 owner home、provider identity、权限策略隔离 memory/task/session。
  适合同一团队、同一信任边界。

不互信多用户 / 公共服务模式：
  不应只靠 owner home。
  建议拆 OS user、container、gateway、credential、workspace。
  每个信任边界独立 runtime，shared 能力通过发布/同步机制分发。
```

大白话：

```text
目录隔离是防串线，不是防 root。
如果用户之间不互信，要用进程/系统/容器级隔离，不能只靠应用逻辑。
```

## 31. 可观测性

每个 owner 应有轻量指标：

```text
logs/
metrics/
health.json
```

指标包括：

```text
active_tasks
active_agents
disk_usage
raw_archive_size
compact_count
failed_compact_count
pending_wake_signals
index_dangling_refs
quota_usage
```

doctor 应能检查：

```text
owner home 是否完整
索引是否悬空
compact 链是否断
task/agent 状态是否矛盾
权限文件是否非法
quota 是否超限
```

## 32. 当前系统差距

当前已经有：

```text
MY_AGENT_HOME layout
provider space 骨架
memory daily/raw/hooks
task workspace
agent run workspace 雏形
compact apply / continue packet
artifact manifest
shared workspace
conversation / wake signal
```

仍然不足：

```text
1. owner home 已进入主要新写入和恢复读取链路；保存型 local/main run 也写入 `owner_home/tasks/...`，旧顶层 task 目录只作为读取/迁移兼容。仍需继续审计少数旧入口，避免未来新增功能绕过 owner resolver。
2. 旧 raw/archive 数据已有迁移计划；local/main 仍保留兼容读取，provider user/group 默认只读自己的 owner archive。
3. provider users/groups 的 memory/task 读取面已有隔离测试；session/runs/agents 的外部通道真实接入还没完整验收。
4. task-level compact rollup 已能聚合 child status/artifact refs，并同步 owner 级 `compact/by_task|by_run|by_agent` 指针；compact base 包已经记录 branch/current_branch/parent compact，task rollup 会写 `branch_main_rollup.json`。doctor 会检查 owner compact 指针是否还指向存在的 rollup/package，断裂时只给 manual repair 提示。后续主要补归档策略和更丰富的父级展示。
5. global_index 已有悬空引用 doctor helper 和显式 `home-index-rebuild` 维护命令；doctor repair plan 会提示 `home-index-rebuild --apply`、`home-retention --apply` 等可显式执行的维护动作。后续主要补更丰富的一致性指标展示。
6. 权限临时授权和 capability request 已有 owner 账本、过期生命周期和 doctor 展示，后续要接更多真实工具审批入口。
7. retention 已有 owner 过期文件计划/显式清理；backup/restore 已有 snapshot、restore dry-run 和 doctor 摘要。加密仍主要是设计层。
8. owner 私有 skills/tools/workflows 和 shared 公共能力已有 resolver 第一版，后续要接 usage、archive、promotion。
9. skill/tool 的 usage、archive、promotion 流程还没接到 agent 自动管理。
```

## 33. 当前代码映射表

这张表用于把设计落到代码，不代表这些模块已经全部实现了目标形态。

| 设计概念 | 当前相关代码位置 | 当前差距 |
| --- | --- | --- |
| owner home / provider space | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/user_space/home_layout.py`、`provider_space.py`、`owner_resolver.py` | provider space 和 owner resolver 已接主要新写入/读取链路，后续继续查旧入口 |
| memory daily/raw/hooks | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/memory_store/jsonl.py`、`memory_archive/`、`agent/user_space/home_migration.py`、`cli/memory_archive_commands.py` | 新写入和 archive resume/list/search 已 owner 分层；local/main 仍保留旧 archive 兼容读取 |
| compact / continue packet | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/memory_archive/compact.py`、`agent/user_space/task_compact_rollup.py` | task/run/agent 基础包和 task rollup 已有第一版；rollup 已有 status/artifact 聚合，并写 owner 级 compact 指针；compact branch/归档还未完整 |
| session | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/session/manager.py`、`conversation/` | session/thread 有管理能力，但 provider owner/session/task/run 映射还要收敛 |
| task registry | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/task_registry/` | 需要与 owner home、task workspace、artifact registry 完整挂接 |
| agent tree / subagents | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/subagents/`、`subagent.py` | 子代理运行状态已存在，但 agent workspace/compact/cleanup 策略还未完全统一 |
| artifact registry | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/artifacts/` | 已有 registry 能力，但 owner/task/run 路径和最终展示链路要统一 |
| capability / skill routing | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/capability/`、`agent_py_agent/config/capability_config.yaml`、`agent/user_space/capability_resolver.py` | owner/shared/builtin 解析、run 内缓存和安全撤销检查已有第一版，workspace/optional/promotion 仍需继续 |
| collaboration / wake | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/collaboration/`、`conversation/store_wake.py` | 有协作/唤醒底座，仍需 owner/task/agent 权限边界统一 |
| config / runtime guard | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/config.py`、`agent_py_agent/config/runtime_guard_config.yaml` | 需继续清理配置外写死默认值 |
| doctor / observability | `/Users/example/my_agent/my-agent-main/agent_py_agent/cli/memory_doctor.py`、`agent/user_space/home_doctor.py`、`home_runtime_status.py`、`home_index_rebuild.py` | owner 迁移/index/schema/retention/backup/capability request/temporary grant doctor 已接入，home-status 已显示当前 owner identity，home-index-rebuild 可显式重建索引；doctor repair plan 已区分 auto_repair/warn/manual，后续补指标展示 |
| permissions / quota / retention | `agent_py_agent/agent/user_space/owner_policy.py`、`agent/user_space/home_retention.py`、`cli/home_runtime_commands.py` | owner policy bundle、retention 计划和显式 home-retention 命令已有，quota enforcement/加密仍待补 |
| temporary grants | `agent_py_agent/agent/user_space/temporary_grants.py` | owner 级账本、过期状态和 doctor 摘要已有，后续接更多工具执行前提示和审批入口 |
| capability requests | `agent_py_agent/agent/user_space/capability_requests.py` | owner 级生命周期、过期状态和 doctor 摘要已有，后续接父代理/用户审批界面 |
| canonical identity | `agent_py_agent/agent/user_space/identity_store.py`、`owner_lifecycle.py` | provider 身份分片索引、canonical link、canonical memory note 和 provider owner lifecycle 已有第一版；解绑、冲突合并和 UI 仍需继续 |
| compact branches | `agent_py_agent/agent/user_space/compact_layout.py`、`task_compact_rollup.py` | branches.json/current_branch/parent compact 和 branch_main_rollup 已有第一版；后续补归档策略和分支切换展示 |
| tree service | `/Users/example/my_agent/my-agent-main/agent_py_agent/agent/subagents/` | 需要从各 agent state/timeline 投影统一生成 agent_tree.json |
| backup / restore | `agent_py_agent/agent/user_space/home_backup.py` | 已有 manifest、snapshot、restore dry-run、restore copy 和 doctor 摘要第一版；后续补 CLI restore 命令、定时备份和 restore 后自动 doctor |

## 34. 从 通道运行时 / 长期助手 借鉴的点

通道运行时 值得借鉴：

```text
信任边界要讲清楚。
一个 Gateway 不应该服务互不信任的人。
真正不互信时，应拆 gateway / OS user / host / container。
workspace 级 AGENTS/SOUL/TOOLS/skills 可以作为项目局部能力层。
```

长期助手 值得借鉴：

```text
profile 有自己的 长期助手_HOME，里面包括 config、keys、memory、sessions、skills、gateway。
所有状态路径必须通过 profile-aware home resolver，不能硬编码 ~/.长期助手。
builtin skills 和 optional skills 分开。
用户 skills 在自己的 home 下。
shared/bundled 能同步到 profiles，但 profile 可以禁用或不启用。
技能生命周期要有 usage、archive、restore。
```

我们不照搬的点：

```text
不要求用户自己管理 skills/tools 目录。
不让同名能力静默覆盖公共能力。
不把 shared 当成管理员私人目录。
不把目录隔离误当成强多租户安全。
```

## 35. 迁移路线

建议分阶段：

### 阶段 1：文档和配置收口

```text
确认 shared/owners/identity/global_index/system 顶层布局。
把旧文档里的顶层主账号私有目录说法更新成 owner home。
补 owner 私有 skills/tools/workflows/role_templates。
补 shared/builtin/optional/owner/workspace 能力来源说明。
```

### 阶段 2：owner resolver

```text
新增 owner resolver。
输入 provider/channel/user/group/session，输出 owner_home。
本地 CLI 输出 owners/local/main。
```

### 阶段 3：memory/raw/hooks 按 owner 写

```text
新写入走 owner_home/memory/。
旧路径保留只读兼容。
run 级 runtime_facts 正文只写 runs/<run_id>/runtime_facts.json。
memory 下只保留 runtime refs/index。
```

### 阶段 4：session/task/run/agent 全部挂 owner

```text
session 只引用 task。
task 可跨 session。
agent workspace 归属 owner + task。
run 归属 task，可由 session、后台 watcher、automation 触发。
```

### 阶段 5：task-level compact rollup

```text
统一 task/run/agent compact base 包。
实现 task_rollup.json。
父代理恢复优先读 task rollup，再按需读子代理 compact。
定义 continue_packet schema 和 compact injection template。
```

### 阶段 6：权限、临时授权、capability request

```text
permissions.json 字段化。
temporary_grants 接入工具执行。
capability_requests 接入父代理/用户审批。
skill_policy/tool_policy schema 落地。
群/个人/provider 权限取交集。
```

### 阶段 7：索引、doctor、retention

```text
global_index 可重建。
doctor 查悬空引用、迁移待办、schema 和 retention 候选。
retention 按 owner retention.json 清理 cache/tmp/old raw/daily/hooks/trash。
provider identity index 分片或 SQLite 预留。
```

### 阶段 8：能力解析和 agent 自动管理

```text
统一 capability resolver。
能力 ID 支持 builtin/shared/optional/owner/workspace 命名空间。
owner policy 控制 shared/optional 能力启用和禁用。
agent 能把重复工作沉淀成 owner 私有 skill/workflow 草稿。
长期不用的 owner 私有能力进 archive，不直接删除。
shared 提升必须显式审核。
同 run 能力解析结果缓存，避免确认风暴。
```

### 阶段 9：identity / backup / lifecycle

```text
canonical user memory 和 provider memory 合并规则落地。
provider owner created/active/suspended/archived/deleted 生命周期落地。
backup/restore 独立于 migration。
shared skill 热更新策略落地：运行中的 run 固定使用启动时解析版本，新 run 才默认新版本。
```

## 36. 最终一句话

最终形态应该是：

```text
shared 是公共能力库。
owners 是每个用户/群/主账号自己的家。
owner 自己也有私有 skills/tools/workflows，不需要用户手工维护。
shared 只放被批准共享的能力，不放管理员私有能力。
identity 负责跨 provider 绑定同一个真实人。
session 是聊天窗口，task 是工作项目，run 是一次执行，agent 是执行体。
memory 和 compact 全部跟 owner/task/run/agent 走。
子代理不默认拥有长期 memory，只在自己的 run workspace 留状态、产物、compact 和总结。
全局 index 只做地图，可重建，读正文前必须过 owner 权限。
能力解析要带来源，不能同名静默覆盖，不能因为用户不懂目录就让能力串线。
```
