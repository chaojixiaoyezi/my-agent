# FEATURE-20260718-agent-foundation-convergence

状态：Implemented Locally / Local CI Passed / Pending Deploy and Real Validation

用户确认：2026-07-18，用户要求按既定顺序逐项实现、完成本地 CI、推送 `main`、部署
192.0.2.10，并按既有标准完成真实飞书多用户、多任务 LLM 验证。

## Background / 背景

当前 my-agent 已有 owner 隔离、通道投递、Memory、Persona、Skill、wait、子代理和任务状态等基础，
但部分能力存在“索引有、主链未接”“配置有、运行时没有统一消费”或重复事实源。详细代码审计见
`docs/design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md`。

本功能参考 会话运行时 的不可变 Skill snapshot 与 typed task、长期助手 的 per-profile cron/Memory 锁/被动验证
证据、通道运行时 的通道配置事实与 workspace Persona 加载。适配只使用 my-agent 已有 owner、thread、task、
tool、artifact 和 conversation 事实，不增加自然语言硬判断或 IM 专属底座分支。

## Goal / 目标

让普通用户在同一持续会话中可靠使用公共和私有 Skill、Memory、Persona、持久定时任务、工具与子代理；
模型只能描述当前真正安装、配置、健康且绑定的能力；所有持久状态与执行权限都受同一 owner 边界约束，
任务完成说明只依据真实且新鲜的执行证据。

## Non-Goals / 非目标

- 不建立第二套聊天、Memory、Compact、任务或 Workflow 状态机。
- 不为飞书或任何单一 IM 修改通用任务语义。
- 不通过普通自然语言关键词触发权限、路由、完成、调度或状态迁移。
- 不把 长期助手 profile 直接当作十万用户 tenant 模型。
- 不把局部测试自动升级成全仓通过，也不恢复普通任务目录验收器。
- 不允许 shared Markdown 直接变成未经管理员安装的可执行代码。

## Scenarios / 场景

1. 飞书用户询问 Agent 能做什么时，只看到当前真实通道、工具、Skill 和绑定状态。
2. 用户和群组分别使用 builtin/shared/owner/workspace Skill，任何私有内容不能跨 owner 泄漏。
3. 用户通过自然语言要求明天提醒或周期执行，模型调用 typed scheduler 工具，任务归属当前 owner。
4. Agent 可修正或删除错误长期记忆；并发会话不会互相覆盖 Memory 或 Persona。
5. 普通长任务继续使用同一 thread/task/subagent 主链，可复用 Skill，不进入第二套 Workflow engine。
6. 代码任务完成时，最终说明区分 targeted 与 full 验证，文件修改后旧证据自动失效。

## Requirements / 需求

| ID | Description | Priority |
| --- | --- | --- |
| FR-001 | 通道 installed/configured/health/current-bound 必须由统一 registry snapshot 投影 | Must |
| FR-002 | Skill 必须由唯一服务构建逐轮不可变的 builtin/shared/owner/workspace snapshot | Must |
| FR-003 | Memory 保留 owner JSONL 权威源，并支持稳定 ID 的 add/list/replace/remove/batch | Must |
| FR-004 | Persona 保留 USER 自主更新、SOUL/AGENTS 确认边界，并补版本、并发和加载诊断 | Must |
| FR-005 | Scheduler 必须 owner-scoped、持久、可恢复，并支持 create/list/update/pause/resume/delete/run-now/history | Must |
| FR-006 | 可复用 Workflow 迁移为 Skill + 普通 typed task/subagent 组合，删除重复 Workflow 执行链 | Must |
| FR-007 | 所有工具、Skill、Memory、Persona、Scheduler 与子代理继承同一不可扩大的 owner 权限 | Must |
| FR-008 | 验证证据被动记录真实命令、scope、exit、freshness；修改后旧证据失效 | Must |
| FR-009 | active-agent 与结构化 owner 写入口必须执行同一 owner quota；策略/用量不可读时 fail-closed | Must |
| FR-010 | retention 必须按结构化终态和时间执行，支持二次校验、trash tombstone、legal hold、审计与自动有界扫描 | Must |
| FR-011 | 完整本地 CI 通过后才允许推送 main、部署 1.10 和真实飞书多任务验收 | Must |

## Constraints / 约束

- Python 3.10/3.11/3.12 兼容；主链优先标准库，不新增第三方依赖。
- 文件系统仍是当前 owner 事实源；索引只能作为派生投影。
- 结构化 owner/thread/task/run/tool facts 是唯一机器权威。
- 所有写入遵守 owner path policy、原子写边界、bwrap fail-closed 和 artifact registry。
- 普通 IM 回复仍由模型基于真实事实生成，控制命令除外。

## Impact / 影响

- `agent/delivery/`：通道注册与状态 snapshot。
- `agent/tooling/`：能力自述、验证证据公共执行出口。
- `agent/capability/`：Skill、Memory、Persona 工具与加载服务。
- `agent/user_space/`：shared/owner/workspace 解析、策略、版本和持久任务路径。
- `agent/conversation/`、`agent/agent_core/`：逐轮 snapshot、Scheduler 唤醒与 typed task 复用。
- `agent/subagent_workflows/`：迁移后删除。
- 文档、测试、部署 wheel 与 1.10 真实运行配置。

## Architecture / 架构

1. Composition root 创建当前 Agent 唯一 channel registry、Skill service、Memory/Persona repositories
   和 Scheduler service；工具只接收这些实例或只读 provider。
2. 每轮开始按当前 owner、workspace 与 policy 生成不可变 capability/Skill/Memory/Persona snapshot。
3. Scheduler job 保存 owner/thread/task/delivery/skill/tool policy 的结构化引用；执行时重新验证当前权限。
4. Workflow 内容归 Skill，实际运行继续使用普通 thread/task/subagent/scheduler。
5. 工具执行与文件修改写入同一 verification evidence ledger，最终回复只读取投影。
6. owner quota 统一使用 `quota -> repository/file lock -> mutation` 锁序；所有结构化持久写入口在同一
   owner lock 内计算整批最终字节。Shell/PTY/LSP 的任意进程写盘由正式部署的文件系统 quota 兜底。
7. Gateway maintenance 按 cursor 有界扫描 owner，只根据结构化 terminal authority 和 retention policy
   清理；状态变化、legal hold 或损坏 policy 均跳过。

## Data Model / 数据模型

- `ChannelRuntimeSnapshot`：channel、installed、configured、health、checked_at、error_code、
  current_bound、capabilities。
- `SkillSnapshot`：scope、stable_id、name、metadata、path、enabled、load_error、content fingerprint。
- Memory operation record：entry_id、action、content/kind/tags、source、version、created_at、expires_at。
- Persona version record：target、version、sha256、source_quote、confirmed、created_at、backup_ref。
- Scheduler job/run：job_id、owner、thread、schedule、status、next_run、claim、delivery、run history。
- Verification evidence：owner/thread/task/root、command、scope、exit、changed paths、created_at、stale_at。

具体 schema 在对应切片实现前写入各模块设计文档；不得在多处复制同一事实。

## State Transitions / 状态转换

- Channel health：`not_probed -> healthy|unhealthy`，配置变化回到 `not_probed`。
- Skill snapshot：构建后本轮不可变；显式 reload/文件版本变化只影响下一轮。
- Scheduler job：`active <-> paused -> deleted`；run 使用 `queued -> claimed -> running -> done|failed|cancelled`。
- Verification evidence：`fresh -> stale`；新验证产生新 fresh 记录，不改写旧历史。

## File Writes / 文件写入

- Memory/Persona 继续写当前 owner home，使用锁、临时文件和原子替换或 append-only op。
- Scheduler 与 verification evidence 写当前 owner 的 `data/`/runtime 子目录；路径由 home layout 暴露，
  不从模型参数拼接。
- shared 只能由管理员发布入口写，普通 owner 只读。
- 不写 `data/` 工作树样本，不修改现有未跟踪交接文档。

## Test Plan / 测试计划

- 单元：registry snapshot、Skill scope/错误/缓存、Memory CRUD/并发、Persona CAS/回滚、Scheduler
  claim/restart、verification stale。
- 合同：owner/group/shared 黑名单、子代理权限非扩张、普通自然语言不能触发硬状态。
- 集成：同一 thread compact 后继续、小任务连续累积、定时唤醒复用同一 thread。
- 本地 CI：pytest 全量、Ruff、compileall、import/offline/code-size/doc-sync/clean-package/artifact gates。
- 真实：1.10 飞书双 owner、多长任务、并发聊天、`/btw`、`/stop`、compact、Memory/Persona/Skill
  隔离与无协议泄漏。

## Acceptance Criteria / 验收标准

- [x] 能力自述四层状态来自实际 registry 与当前结构化 binding；聚焦回归和完整本地 CI 通过，待部署真测。
- [x] builtin/shared/owner/workspace Skill 同一 snapshot 主链通过，旧重复加载链已删除；231 项相关聚焦回归、新增隔离/继承测试和完整本地 CI 通过，待部署真测。
- [x] Memory 保留 owner JSONL 单一权威源，稳定 ID CRUD/batch、并发、过期、索引过滤与 owner/group 隔离通过聚焦回归和完整本地 CI，待部署真测。
- [x] Persona 保留 USER 自主与 SOUL/AGENTS 确认边界，版本/CAS/回滚、受控加载、确认后并发冲突、owner/group 隔离通过聚焦回归和完整本地 CI，待部署真测。
- [x] Scheduler 重启恢复、去重 claim、用户/群组隔离和同 thread 唤醒已通过聚焦回归；全局 due-owner
  投影在 1.10 重启与 135-owner 场景完成时延反证，真实 Feishu 自动兜底和消息工具主动投递均为单 run/
  单出站/单 transcript。owner JSON 账本仍是唯一权威；完整本地 CI 已通过，待精确提交 wheel 部署。
- [x] `subagent_workflows` 旧执行链完成迁移并删除；Skill + 当前 `task_progress` + 原生子代理工具为唯一主链，普通任务无第二上下文；聚焦回归和完整本地 CI 通过，待部署真测。
- [x] 验证证据在公共工具出口按真实命令/exit/root-task 记录，能区分 targeted/full；文件工具成功修改后旧证据失效，owner/task 隔离通过聚焦回归和完整本地 CI，待部署真测。
- [x] `max_active_agents`、文件工具、Memory、Persona、Scheduler 与 Skill draft 已接同一 owner quota
  准入；并发投影、整批拒绝、删除/不增长修复和策略损坏 fail-closed 聚焦回归通过。Shell/PTY/LSP
  任意写盘的总量硬保证明确留给部署层 filesystem/project quota。
- [x] retention 已消费 task/subagent/raw/daily/hooks/compact/cache/tmp/trash 的结构化策略，支持
  terminal authority、执行前二次校验、trash tombstone、owner/task legal hold、审计与 Gateway 自动
  有界 owner 扫描；聚焦回归通过。
- [x] 全部本地严格门禁与干净 wheel artifact gate 通过；保留的未跟踪 `data/` 被 worktree clean-package 正确阻断，未进入 wheel。
- [ ] 远端 `main`、1.10 wheel/服务状态和真实飞书多任务证据一致。

## Risks / 风险

- 多入口迁移时保留旧 fallback 会重新形成双事实源：按切片完成调用点迁移后立即删除旧入口。
- Skill/Persona/Memory snapshot 缓存键不完整会串 owner：缓存键必须包含 owner、policy 和 workspace。
- Scheduler 重复执行外部副作用：claim 与幂等回执必须在执行前持久化。
- 大范围一次修改难定位回归：每个切片先 focused tests，再进入下一切片，最后只跑一次完整本地 CI。

## Rollback / 回滚方案

部署保留上一份已验证 wheel。若 1.10 真实验证失败，停止新服务、恢复上一 wheel
和配置，保留 owner 数据与新 schema 记录，不删除用户数据；修复后通过迁移/兼容读取恢复，而不是重置 home。
