# Memory：结构树和详细说明

## 模块结构

```text
agent_py_agent/agent/
|-- memory.py                         # 旧兼容入口，真实存储已拆到 memory_store/
|-- memory_settings.py                # 旧兼容入口，真实配置解析在 settings/memory.py
|-- settings/memory.py                # memory 配置、默认值、warning、安全归一化和参数边界
|-- memory_store/                     # 长期记忆 JSONL 事实流水，可选同步索引到 LocalStore，并提供 daily work journal API
|   `-- daily.py                      # 每日工作记忆事件，记录摘要、引用、教训和下一步，不替代 raw archive
|-- memory_routing/                   # route index、匹配、required/candidate path、read receipt
|-- contracts/                        # 错误分类、状态机、幂等键和真实 E2E 矩阵合同
|-- user_space/home_runtime_query.py   # home daily memory、tasks 和 home status 只读查询入口
|-- user_space/owner_resolver.py       # V2 owner home 解析，支持 local/main、provider user/group
|-- user_space/identity_store.py       # provider identity 分片索引和 canonical user profile 目录
|-- user_space/home_indexes.py         # global_index 的 owner/task 轻量引用写入
|-- user_space/home_migration.py       # 旧 daily/raw/task workspace 到 owner home 的非破坏性复制计划
|-- user_space/home_doctor.py          # owner home 迁移、索引、schema、retention 的非阻断体检报告
|-- user_space/home_retention.py       # owner retention.json 驱动的过期文件计划和显式清理
|-- user_space/home_backup.py          # owner home manifest/snapshot 备份、restore dry-run 和恢复复制
|-- user_space/home_memory_notes.py    # HOT 去重追加、lesson 写入和 route index 同步入口
|-- user_space/owner_policy.py         # owner permissions/quota/retention/policy 读取和磁盘用量统计
|-- user_space/compact_layout.py       # task/run/agent 共享 compact 包基础文件布局
|-- user_space/compact_injection.py    # compact_context + continue_packet 的统一续接提示渲染
|-- user_space/task_compact_rollup.py  # task 级 compact rollup，汇总子代理 run refs 和最新恢复入口
|-- user_space/capability_resolver.py  # owner/shared/builtin 能力短名解析和 run 内缓存
|-- user_space/capability_requests.py  # owner 级能力/工具/权限申请账本，不阻断普通任务
|-- user_space/temporary_grants.py     # owner 级临时授权账本，过期只改状态不删审计
|-- user_space/owner_lifecycle.py      # owner_status 和 owner audit 事件
|-- user_space/skill_candidates.py     # owner 私有 skill 候选草稿账本，不自动提升
|-- user_space/context_bundle.py       # 主代理 Main Agent Context Bundle v1 生成和 prompt 摘要
|-- user_space/context_bundle_contracts.py # RunScope/ToolManifest/Acceptance/self-check 等合同字段
|-- user_space/context_bundle_artifacts.py # 保存型 run 收尾后按 scope 回填 artifact refs
|-- user_space/context_bundle_rendering.py # context bundle prompt/Markdown 渲染
|-- agent_core/runtime_context_bundle.py # runtime loop 到主代理 context bundle 的桥接层
|-- agent_core/runtime_live_archive.py  # 工具循环到 live raw archive 的非阻塞桥接层
`-- memory_archive/                   # hook snapshot、raw archive、留存、token 估算、compact 预演
    |-- agent_run_workspace.py         # Phase 1 task-local agent run workspace 骨架
    |-- artifact_registry.py           # Phase 3 artifact manifest summary/hash/path 和 workspace 边界
    |-- compact_action_guard.py        # 自动 compact/resume 前的动作守门报告
    |-- compact_apply.py               # 非破坏性 memory-compact --apply 编排入口
    |-- compact_apply_ids.py           # apply_id / plan_id / scope hash / 风险摘要 helper
    |-- compact_apply_io.py            # compact apply JSON/Markdown/JSONL 写入 helper
    |-- compact_apply_lineage.py       # 重复 compact 的 cycle/previous apply 链路 helper
    |-- compact_apply_payloads.py      # restore refs / apply bundle / ledger payload helper
    |-- compact_apply_rendering.py     # compact apply 恢复上下文 Markdown 渲染 helper
    |-- compact_artifact_read_hints.py # tool-output artifact 的 read_artifact 分片读取提示 helper
    |-- compact_apply_self_check.py    # compact apply post-check 和失败阻断 payload
    |-- compact_apply_work_state.py    # compact apply work_state_snapshot 恢复基线，含软 runtime_handoff
    |-- compact_context_bundle_match.py # compact apply 绑定主任务卡前的 scope match helper
    |-- compact_context_bundle_refs.py # 主代理 context bundle 引用读取和恢复摘要 helper
    |-- compact_gate_bridge.py        # compact apply/resume 接入共享 compaction gate 的状态桥
    |-- compact_state.py              # compact apply 的机器交接状态和模型可读 handoff summary
    |-- compact_work_state_sources.py  # workspace 内 task/run 事实源、进度账本、运行交接来源扫描
    |-- compact_auto.py                # 自动 compact/resume 安全协调器，默认只计划不继续执行工具
    |-- compact_continue_packet.py     # compact resume 后的继续工作包，固定目标/约束/验收/guard 状态
    |-- compact_resume.py              # memory-resume --from-compact 手动恢复上下文
    |-- compact_resume_blocked.py      # compact apply 缺失时的 schema-compatible 阻断 payload
    |-- compact_resume_completion.py   # 缺失 work_state 字段时的半自动补全提示模板
    |-- compact_resume_handoff.py      # compact resume 交接包和可粘贴上下文块
    |-- compact_resume_payloads.py     # compact resume handoff/continue packet 派生输出打包 helper
    |-- compact_runtime_handoff.py     # compact 运行交接摘要，读取 guidance 和下级状态
    |-- compact_subagent_owner.py      # compact resume 的子代理 owner/run workspace 只读引用解析
    |-- compact_suggest.py             # 半自动 compact 风险提示和命令建议
    |-- compact_tool_output_refs.py    # compact apply/resume 的 tool-output artifact refs 扫描 helper
    |-- compact_chain.py               # Phase 4 checkpoint-first compact chain ledger
    |-- control_plane.py               # 统一只读查询 daily/task-run/compact/tool-output 轻量索引
    |-- daily_ledger.py                # Phase 2 daily/YYYY-MM-DD/events.jsonl 事件索引
    |-- memory_gate_export.py          # Phase 6 显式长期 memory / skill draft 导出
    |-- memory_gate_retention.py       # Phase 6 保守 retention 计划和 active queue 压缩
    |-- memory_gate_verifier.py        # Phase 6 no-auto-promotion 边界检查
    |-- runtime/live_archiver.py       # 运行中增量写 raw archive 的助手工具轮、工具结果和 checkpoint 事件
    |-- runtime_fact_source.py         # 真实 run 保存时写入显式验收/约束/测试事实源
    |-- runtime_workspace_outputs.py   # 从交付合同或 Current Task Workspace 提取 output/ 软目标
    |-- schema.py                      # Runtime memory schema v2 和 reserved 字段统一 helper
    |-- shared_workspace.py            # Phase 5 task-local append/merge blackboard/messages/findings/evidence
    |-- task_workspace.py              # Phase 0 文件系统 task workspace 骨架和旧 subagent run adapter
    |-- task_workspace_roots.py        # 解析 task root；子代理继承当前 task_root，不用 run_id 另开任务目录
    |-- task_workspace_rendering.py    # task workspace YAML/Markdown 小文件渲染 helper
    |-- tool_output_externalizer.py    # runtime 大工具输出 artifact 化和 index
    |-- ../../agent_core/tool_context_reducer.py # 大工具输出进入 live prompt 前的摘要化边界
    |-- ../../agent_core/compact_auto_continuation.py # 主 agent 自动 compact 后的一次受控续跑桥
    |-- ../../agent_core/subagent_compact_continuation.py # 子代理 task-local compact 接续 prompt 片段
    |-- ../../agent_core/finalization_compact_auto.py # run 收尾 compact auto 字段投影和续跑跳过策略
    |-- snapshots/_helpers.py          # snapshot 字段裁剪、hash、工具调用 metadata 归一化 helper
    `-- query/task_sources.py          # task/run 恢复入口推荐，不把子代理内容写入主 memory

agent_py_agent/cli/
|-- memory_commands.py                # memory-route / memory-doctor 等可见诊断命令
|-- home_runtime_commands.py          # home-status / memory-daily-list / task-workspace-list / home-index-rebuild 调试维护命令
|-- memory_archive_commands.py        # memory-archive-list/search/resume 命令
|-- memory_archive_roots.py           # memory archive CLI 的 owner-scoped archive 根目录解析和多根合并
|-- memory_archive_rendering.py       # memory archive CLI 的 list/search/resume 输出渲染
|-- memory_resume_compact_rendering.py # memory-resume --from-compact 人类输出渲染
|-- memory_compact_commands.py        # memory-compact dry-run 和非破坏性 apply 命令
`-- context_bundle_commands.py        # context-bundle latest 只读观测命令
```

## 核心文件

- `memory_archive/resume_context.py`：除恢复上下文构造外，公开 `has_resume_trigger()` 作为轻量意图判断 helper；它只看当前用户 prompt 是否明确继续旧任务、恢复上次、或点名 run/request/subagent 等恢复目标，不加载历史正文。普通任务里说“继续往下做/继续整理/继续完成”不会触发旧任务恢复，避免一次性 CLI 任务串入历史工作。
- `memory_store/jsonl.py`：读写长期记忆 JSONL，是最朴素的事实落盘层；LocalStore 只是索引，不替代 JSONL。owner-home agent 的新写入会落到 `owner_home/memory/long_term/memory.jsonl`。旧 `memory_path` 只给 `local/main` 作为兼容读取源；provider user/group 不再默认读取本地 CLI 旧记忆，避免不同用户主代理串记忆。`SimpleAgent` 传入 home daily mirror 后，同一条记录也会追加到 `owner_home/memory/daily/YYYY-MM-DD.jsonl`，便于以后按天恢复和查询；读取侧会按 owner 读取 daily mirror，并对 local/main 做旧数据兼容去重。
- `memory_store/daily.py`：提供 `DailyMemoryEvent`、`append_daily_memory_event()` 和 `daily_memory_path()`；这一层面向 通道运行时 式每日工作记忆，记录进展摘要、引用、教训和下一步。它不是新硬门，也不取代 raw archive，只让 owner memory 除了黑盒流水外还有可读工作日记。
- `contracts/error_taxonomy.py`、`contracts/state_machine.py`、`contracts/idempotency.py`、`contracts/tool_protocol_v2.py`、`contracts/model_call_ledger.py`、`contracts/e2e_matrix.py`、`contracts/e2e_matrix_runner.py`：主代理执行合同层。它们分别定义稳定错误代码/恢复建议、运行状态事实判断、幂等键/操作编号、工具调用/工具结果 envelope、模型调用 started/first-token/finished/timeout 账本、真实端到端测试矩阵和 deterministic runner；这些模块只输出机器可读事实，不直接阻断工具或固定工作流。当前 `create_subagents` 已写 `operation_contract`，`current_turn_run_state` 已写 `state_machine_contract` 和 `recovery_recommendations`，`ToolExecutionResult` 已带错误合同字段，registry 会同步镜像 `tool_protocol_v2` 结构化结果。
- `user_space/home_runtime_query.py`：提供 `DailyMemoryQuery`、`TaskWorkspaceQuery`、`read_daily_memory_records()`、`list_task_workspaces()`、`home_task_workspace_payload()` 和 `home_runtime_status()`；CLI、doctor 和 resume 通过这一层读取 home runtime，不在各自模块里散扫目录。它会按当前 owner 过滤 daily/task workspace：local/main 可看旧顶层兼容目录，provider user/group 只看自己的 owner home。
- `user_space/owner_resolver.py`：把 local CLI、provider user、provider group 解析到 V2 owner home。它会按需初始化 owner 的入口文件、memory、tasks/runs/agents、compact、workspace、capability_requests 和策略文件；这是运行时进入 owner 隔离的第一层桥。
- `user_space/identity_store.py`：把外部 provider 身份写入 `identity/provider_identity/<provider>.jsonl`，查询时按 provider 分片读取，不全局扫一个大文件；canonical user profile 使用目录 `identity/canonical_users/<id>/profile.json`，避免文件和目录同名冲突。canonical link 和 canonical memory note 已有第一版，绑定后的共享偏好写在 canonical 用户目录里，不覆盖 provider owner 自己的 memory。
- `user_space/home_indexes.py`：写入并读取 `global_index/owners.jsonl`、`active_tasks.jsonl`、`active_runs.jsonl`、`active_agents.jsonl` 这类轻量地图。它只记录 owner/task/run/agent 的路径和状态摘要，真实事实仍在 owner/task/run/agent 目录里；读取时按身份返回最新一条引用，旧 append-only 历史行不会继续误导恢复或 doctor。
- `user_space/home_index_rebuild.py`：从当前 owner home 正文扫描 task workspace、run state 和 agent projection，生成可审计的索引重建计划；默认只预览，显式 `home-index-rebuild --apply` 才追加新的 global index 行。它不删除旧索引、不修改任务正文、不作为任务硬门。
- `user_space/home_migration.py`：生成和执行 V1 到 V2 owner home 的迁移计划。当前复制 legacy long-term `data/memory.jsonl`、daily、raw、hooks 和 task workspace 到 owner home，目标已存在就跳过，不删除旧文件，也不改任务状态。
- `user_space/home_doctor.py`：汇总 home runtime 状态、schema version、迁移待办、悬空 global index、retention 候选、snapshot backup、open capability requests 和 active temporary grants。它只给报告和建议，不改变退出码，不阻断普通任务。
- `user_space/home_retention.py`：读取 owner `retention.json`，对 raw/daily/hooks/compact/cache/tmp/trash 生成过期文件计划；显式 apply 时只删除过期文件，不删目录，配置为 0 表示无限保留，并把实际删除动作写入 owner audit log 方便复盘。
- `user_space/home_backup.py`：为 schema 迁移或大规模 owner 调整生成 manifest 或 snapshot。manifest 只列出 owner memory/tasks/runs/agents/identity/index 等保护范围；snapshot 会复制这些元数据/状态文件，`plan_home_backup_restore()` 可先 dry-run 列出恢复会覆盖的路径，`restore_home_backup_snapshot()` 再实际复制回来。
- `user_space/home_memory_notes.py`：提供 `append_hot_note()` 和 `upsert_lesson_note()`。前者把一条短教训去重追加到当前 owner 的 `memory-hot.md`，后者写入当前 owner 的 `memory/lessons/<lesson_id>.md` 并补齐同一 owner 的 `memory/routing/INDEX.md`。local/main 继续兼容旧顶层入口；provider owner 不写本地主账号入口。它只是统一写入入口，不做任务验收、不提升 skill、不阻断普通任务。
- `user_space/owner_policy.py`：读取 owner 级 `permissions.json`、`quota.json`、`retention.json`、`skill_policy.json` 和 `tool_policy.json`，并统计 owner 关键目录磁盘用量。它供 doctor/状态页使用，不给普通任务新增硬门。
- `user_space/compact_layout.py`：创建 `compact_0001/` 这类基础恢复包，task/run/agent 三层共享同一组文件名，避免以后压缩恢复时出现三套格式。每个包会写 `branch_id`、`parent_compact_id`，compact 根会维护 `branches.json` 和 `current_branch.txt`。
- `user_space/compact_injection.py`：把 `compact_context.md` 和 `continue_packet.json` 渲染成同一份续接提示；它不读 raw archive 正文、不自动执行工具。
- `user_space/task_compact_rollup.py`：在当前任务目录的 `work/compact/` 写任务级 `task_rollup.json`、`task_rollup.md`、`rollup_ledger.jsonl`、`rollups/branch_main_rollup.json` 和一份共享 compact 包。它只收集子 run 的 refs、状态和摘要，不复制子代理大产物；同时在 `owner_home/compact/by_task|by_run|by_agent/` 写轻量指针，父代理恢复大任务时先读这里，再按需打开某个子代理细节。旧 `tasks/<root_id>/compact/` 只作为读取迁移兼容。
- `user_space/capability_resolver.py`：按 owner/private draft/shared/builtin 的优先级解析 skill/tool/workflow 短名，并把同一 run 的解析结果缓存到 `owner_home/memory/runtime_refs/capability_resolver/`。缓存只减少反复查目录，不绕过后续权限检查。
- `user_space/capability_requests.py`：写 owner 私有 `capability_requests/<id>.json`，记录能力、工具或权限申请的生命周期和过期状态；子代理结束后请求仍留在 owner home 里，后续由父代理、用户或管理员处理。它不自动授权，也不作为 closeout 硬门。
- `user_space/temporary_grants.py`：写 owner 私有 `temporary_grants/<id>.json`，记录本次临时授权的对象、能力、路径前缀、过期时间和理由；过期后保留记录，只把状态改成 `expired`。
- `user_space/owner_lifecycle.py`：写 owner 私有 `owner_status.json` 和 `audit_log.jsonl`，记录 provider/local owner 的 active、suspended、archived 等状态变化。它是恢复和运维元数据，不是普通任务硬门。
- `user_space/skill_candidates.py`：写 owner 私有 `skills/.drafts/skill_candidates.jsonl`。它只登记可学习经验、来源任务和证据引用，不安装正式 skill，也不写 shared 能力库。
- `user_space/context_bundle.py`：为主代理保存型 run 生成 `Main Agent Context Bundle v1` JSON/Markdown 和 bounded prompt 摘要；它只写结构化 refs，不复制大正文。
- `user_space/context_bundle_contracts.py`：集中生成 context bundle 的合同字段，包括 schema policy、owner model、RunScope、ToolManifest、ArtifactRef、Acceptance Contract、prompt budget 和 self-check。后续字段扩展优先落在这里，避免各处散拼 JSON。
- `user_space/context_bundle_artifacts.py`：run 收尾后按 request/run/task scope 从 tool-output index 回填 artifact refs 到本轮 context bundle；只登记 ref/hash/size/call id，不读取 artifact 正文。
- `user_space/context_bundle_rendering.py`：只负责 context bundle 的 prompt 摘要和 Markdown 镜像渲染；完整 JSON 生成仍在 `context_bundle.py`，这样结构字段和展示格式不会互相拖大。
- `agent_core/runtime_context_bundle.py`：把 runtime loop 的 request、memory、routing、resume 和工具规格转换成 `MainContextBundleRequest`；task-local/control-plane 会在这一层保持不注入主代理 bundle。
- `agent_core/model_call_runtime.py` / `agent_core/model_call_monitor.py`：记录真实模型调用账本，按输入 token、首 token 观测和配置生成动态超时预算；超时会落成结构化 `ProviderTimeoutError` 路径，不让卡住的 provider call 无限占住主代理。
- 大文件写入不再走 `file_write_session`。当前模型可见写入面收敛到 `write_file`、`apply_patch` 和授权命令；长正文、大表格、PDF/Word/图片等复杂产物由模型选择脚本或库生成，再通过 artifact registry / closeout 统一登记和验收。
- `settings/memory.py`：解析配置，处理非法值回退和 warning；会原地更新 AgentConfig-like 对象。
- `memory_routing/loader.py`：读取 route index；JSON 面向程序稳定性，Markdown 面向人工维护，并兼容常见中英文列表分隔符。
- `memory_routing/models.py`：定义 route、match、path resolution、read receipt 等票据结构。
- `memory_routing/matcher.py`：根据用户输入匹配可能需要读取的长期规则，并区分 required/candidate path。
- `memory_routing/context.py`：把命中的规则变成运行时可注入的上下文片段。
- `memory_archive/models.py`：定义 `CompressionSnapshot` 和 `RawMemoryEvent` 两类归档数据形状。
- `memory_archive/storage.py`：保存 raw archive、hook snapshot 和 `memory_archive/snapshots/*.json` 权威快照；写入都做 readback 校验。
- `memory_archive/runtime.py`：把 run turn 的用户、助手、工具元数据写成 raw archive 事件；收尾时会跳过已经带 `raw_archive_event_id/raw_archive_path` 的 live 工具事件，避免重复。
- `memory_archive/runtime/live_archiver.py` 与 `agent_core/runtime_live_archive.py`：在工具循环运行中增量写既有 raw archive。它记录助手工具轮可见文字和完成后的工具结果，写失败不阻断模型继续工作。运行中“当前进度/最近工具/产物引用”统一写到 `runtime_facts/<request_id>/task.json`，不再另建 `run_checkpoint`。
- `memory_archive/snapshots.py`：在 run/gateway/subagent 完成点写轻量恢复 snapshot，并提供压缩前必须成功的 `write_compression_snapshot()` hook。
- `memory_archive/query.py`：把 raw/hook JSONL 读成统一可搜索记录，并整理 resume 线索；旧 subagent 工单找不到时，会通过 home runtime query 回退到 `~/.my-agent/tasks/{date}/{task_slug}/work/state.json` 和 `work/timeline.jsonl`。
- `memory_archive/query/resume_guidance.py`：把 archive/local/task/gateway 线索整理成 `ResumeGuidanceRequest` bundle，输出推荐读取路径和下一步动作，避免恢复建议接口继续用散装参数。
- `memory_archive/query/task_sources.py`：集中维护 subagent 恢复事实源优先级；checkpoint artifacts 优先，传统 `STATUS.md` / `HANDOFF.md` 继续保留。
- `memory_archive/task_workspace.py`：创建文件系统版 task workspace 的最小骨架，并写 `work/agents/<run_id>/legacy_run_ref.json` 指向旧 subagent work-order 目录；这是 adapter，不迁移历史目录。主代理自己的 memory/compact/logs 不进入 `work/agents`。
- `memory_archive/task_workspace.py`、`agent_run_workspace.py`、`daily_ledger.py`、`artifact_registry.py`、`compact_chain.py`、`shared_workspace.py`：这些 runtime memory 写入入口统一提供 `*Request` bundle；旧参数形态只作为兼容 adapter，新增字段应进入 bundle，避免跨阶段继续拉长函数签名。
- `memory_archive/task_workspace_payloads.py`：承接 task workspace 的 `state.json`、`timeline.jsonl` payload 组装和小型 JSON/JSONL 读写 helper，让 workspace 主文件继续只负责编排。
- `memory_archive/task_workspace_rendering.py`：承接 task workspace 的 `task.yaml`、summary 和 blackboard 初始内容渲染，避免同步编排文件继续膨胀。
- `memory_archive/agent_run_workspace.py`：创建 `tasks/<task>/work/agents/<run_id>/` 下的下级代理 run workspace 骨架，包含 agent 身份、run state、任务说明、checkpoint、summary、final report、findings 和 inbox/outbox/artifacts/compactions 目录。
- `memory_archive/daily_ledger.py`：维护 `daily/YYYY-MM-DD/events.jsonl`，只追加 task/run 状态、摘要、duration、refs、artifact/evidence refs 和检索字段，不存完整上下文或工具输出。
- `memory_archive/artifact_registry.py`：把 `SubAgentTask.artifact_refs` 规范化为 task/run 两份 `artifacts/manifest.jsonl`，记录 ref、resolved path、exists、size、sha256、summary、kind 和 `resolution_status`；只会读取 legacy task dir、task workspace、agent run workspace 和当前 run 的 `allowed_write_roots` 内的文件，越界绝对路径只登记 blocked 状态，不复制正文也不计算 hash。
- `memory_archive/compact_chain.py`：在 run `compactions/` 下追加 checkpoint snapshot ledger，写每次 summary/metadata，并把最新 compact refs 回写到 run checkpoint；当前只做恢复链，不删除原始 timeline/artifact。
- `memory_archive/compact_apply.py`：把 `memory-compact --dry-run` 的计划显式落成非破坏性 apply 产物。每个 run/request/task/session scope 写到 `runs/<scope_id>/compact_applies/`，全局 `compact_applies/ledger.jsonl` 只做索引；metadata、apply bundle、restore refs、work state snapshot、self-check JSON、可选 self-check failed JSON 和 run-local ledger 都跟随同一 scope。它不删除或重写 raw/hook/snapshot/token/task/run 文件。新 apply 会登记最近一次主代理 `Main Agent Context Bundle v1`，让恢复先知道任务 scope、工作区和 refs。重复 apply 时会带 `lineage`，记录第几轮 compact 和上一包引用。
- `memory_archive/compact_apply_lineage.py`：只读当前 run-local `runs/<scope_id>/compact_applies/ledger.jsonl`，为当前 apply 推导 `cycle_index`、`previous_apply_id`、上一包 metadata/apply bundle 引用和当前包引用；`plan_id` 只作为审计线索，不再决定第几次 compact。坏 ledger 行会跳过，不阻断新的 compact apply。
- `memory_archive/compact_apply_payloads.py`：生成 compact apply 的 restore refs、apply bundle 和 ledger 行；它只组装 payload，不写文件，让 `compact_apply.py` 继续只负责编排顺序和自检结果。
- `memory_archive/compact_artifact_read_hints.py`：把 work state 里的 tool-output artifact refs 转成 `read_artifact` 参数提示；优先 scoped call id，保留 fallback path，不读取 artifact 正文。
- `memory_archive/compact_apply_rendering.py`：渲染 compact apply 的 Markdown 恢复上下文，让 `compact_apply.py` 继续只管编排、落盘和 self-check。
- `memory_archive/compact_context_bundle_refs.py`：读取、容错解析并裁剪主代理 context bundle；compact apply/resume 只拿任务卡摘要和路径引用，不复制完整长 prompt。
- `memory_archive/compact_context_bundle_match.py`：在 compact apply 绑定主代理 context bundle 前做 scope match；自动 latest ref 不匹配时跳过绑定，显式 ref 不匹配时保留但记录 warning，避免老任务 compact 串到最新任务。
- `memory_archive/compact_gate_bridge.py`：把 compact apply/resume 的 metadata、restore refs 和 work_state 转成共享 `compaction_gate` 能读的机器状态；apply 阶段写 pre 快照，resume 阶段做 post 对比，状态丢失时进入 consistency report 阻断。
- `memory_archive/compact_apply_ids.py`、`compact_apply_work_state.py`、`compact_work_state_sources.py`、`compact_apply_self_check.py`、`compact_apply_io.py`：分别负责稳定 apply/plan 标识、恢复状态基线、task/run 事实源字段读取、自检/失败报告和落盘 IO，让 compact apply 主流程继续保持薄编排，后续接 `memory-resume --from-compact` 时优先复用这些产物。`compact_apply_work_state.py` 优先读取权威 snapshot；没有 snapshot 文件时，只从本次 `restore_refs` 登记的 hook/raw JSONL 回填真实 run 的 goal 和工作级 next action，单个 `[TOOL_CALL]` 不会被升级成任务路线。`compact_work_state_sources.py` 还会读取同 scope 的 `task_progress` 软进度账本、`ConversationStore` guidance 和 task-local 下级状态，生成 `runtime_handoff`；这些只用于续接提示，不参与 closeout 或阻断。
- `compact_apply_work_state.py` 生成下一步时会优先采用最新运行中 guidance / wake signal，再回退到 `task_progress` 的旧 next action；这保证 compact 后续接的是“当前刚发生的事”，例如子代理完成后的汇总提醒，而不是压缩前较早的调研计划。
- `memory_archive/compact_resume.py`：只读读取 compact apply metadata、apply bundle、restore refs、work state snapshot、compact context、self-check 和主代理 context bundle refs，生成 `memory-resume --from-compact` 的恢复上下文、consistency report、推荐读取路径和 continue packet；它会把 `owner_type/owner_id` 传给子代理 owner resolver，但仍不读写 subagent runner。`--from-compact` 是手动/救援入口，自动 runtime compact 续接由运行时直接注入 continue packet。
- `memory_archive/compact_state.py`：为每次 compact apply 生成 refs-first 的 `compaction_state` 和短 Markdown `handoff_summary`。`compaction_state` 是机器可读事实边界，保存 compact 链路、source refs、artifact refs、work state、runtime handoff、next actions 和 summary 引用；`handoff_summary` 只帮助模型续接，不替代 task/run/artifact/tree 事实源。
- `memory_archive/compact_resume_blocked.py`：在 apply metadata 缺失时生成和正常 resume 同 schema 的阻断 payload，包含 action guard 和 subagent owner 边界；主恢复编排不承载错误 payload 细节。
- `memory_archive/compact_continue_packet.py`：把 handoff、work_state、runtime handoff、compaction_state、handoff summary、action guard、推荐读取路径、artifact read hints 和子代理 owner refs 组装成 `compact_continue_packet`，给手动、半自动和自动续跑流程一个共同继续契约；它只打包已有结果，不读 artifact 正文、不执行工具。
- `memory_archive/compact_resume_completion.py`：当 resume 发现 missing work_state 字段时，生成 `completion_prompt`，说明缺哪些字段、对应人类标签、可复制补全模板和 `memory-fact-write` / 重新 apply / auto resume 建议命令；它只提示，不写 runtime facts。
- `memory_archive/compact_resume_handoff.py`：把 compact resume 的 work state、runtime handoff、compaction_state、handoff summary、action guard、推荐读取路径、artifact read hints 和下一步动作整理成 `compact_resume_handoff`，并渲染可复制到新会话的上下文块。
- `memory_archive/compact_resume_payloads.py`：把 compact resume 的 handoff、completion prompt、context block 和 continue packet 派生输出集中打包；主恢复文件只负责读取、校验和 schema assembly。
- `memory_archive/compact_runtime_handoff.py`：从同一 scope 的当前 guidance 账本和 task-local 下级 `canonical_state.json` 生成运行交接摘要，并提供 handoff/continue packet 渲染 helper；它是软续接线索，不调度下级、不修改状态、不阻断验收。它不再读取旧 `data/conversations/guidance` 或旧 `tasks/<id>/agents/<run_id>/state.json` 作为模型可见 handoff 来源。
- `run_intent.py`：把明确的目标产物路径和显式参考目录整理成 `run_intent`。它只服务长任务续接和写入软提醒：compact 后帮助模型继续记住“读哪里”和“写哪里”，`write_file` 写入参考目录时会提示目标路径，但不会阻断，也不会在用户没要求落盘时制造目标文件。
- `memory_archive/runtime_workspace_outputs.py`：从显式 delivery contract 或 `Current Task Workspace` 注入块提取 `output/` 目标目录，写进 runtime facts / run_intent。它只是一条路径软锚点：提醒模型最终交付物优先放 `output/`，草稿和过程材料放 `work/`，不会强迫纯聊天任务落盘。
- `memory_archive/compact_subagent_owner.py`：为 `subagent_run` / `subagent_session` compact resume 只读解析 `tasks/*/work/agents/<run_id>/`、旧 `tasks/*/agents/<run_id>/` 和旧 `subagents/<run_id>/`，返回 run workspace、checkpoint、summary、timeline、artifacts、compactions、legacy adapter refs 和 `session_compact_ledger` / `latest_continue_packet` hook；当 `latest_continue_packet.json` 已存在时，父级 resume/status 会看到 `continue_packet_ready=true`，但仍明确 `memory_scope=task_local`、`writes_main_memory=false`、`automatic_tool_execution=none`。
- `memory_archive/compact_tool_output_refs.py`：只读扫描 `memory_archive/artifacts/tool_outputs/index.jsonl`，按 request/run/task scope 返回 tool-output artifact refs；compact apply 会把它们写进 restore refs，work state 会把它们写进 artifact_refs，resume 推荐路径会按这些 refs 回到完整工具输出。索引同时保存裁剪后的 `parameters/source_input/source_path`，用于告诉续接模型 artifact 来自哪个源文件、URL、查询或命令；它只是定位线索，不是新验收门。
- `memory_archive/compact_action_guard.py`：在 compact resume 后生成动作守门报告；`manual` 模式要求人工确认，`auto` 模式必须通过一致性、自检、refs、goal 和 next_step 等恢复硬条件，否则阻断为 `blocked_*`；acceptance/constraints/latest_tests 缺失只进入 `missing_fields` 提醒，不阻断普通任务续接；放行时返回 `allow_automated_continue` 机器信号，仍不执行工具。
- `memory_archive/compact_suggest.py`：根据当前活跃上下文 token、上下文窗口和 compact dry-run plan 生成半自动提示，返回 `status`、`message`、`recommended_commands`、`requires_confirmation` 和 `trigger`；正常阈值与兜底救场共用这一个 suggestion，只靠 `trigger.reason/source/forced` 说明原因。
- `memory_archive/compact_auto.py`：串起 compact suggestion、非破坏性 apply、auto resume、continue packet 和 action guard；保存型运行默认允许非破坏性 apply、auto resume 和 action guard。`save=False` 或恢复事实源损坏时只返回确认建议；guard 放行时，主 agent 会把 continue packet 注入下一轮 prompt 并继续同一个任务。自检失败或 refs 异常时仍停车；可选 work state 备注缺失只提示。上下文溢出等强制触发不会另建第二套 compact 包，仍走同一条 auto cycle。
- `agent_core/compact_auto_continuation.py`：主 agent 自动 compact 后的续跑桥。它把 `compact_continue_packet` 渲染成 `# Compact Auto Continuation` 注入块，要求模型从 `Next Step` 或当前目标继续、不重做已完成内容；续跑轮只作为模型可见提示，runtime_fact 仍保留原始用户目标；如果继续推进并再次达到阈值，可以继续 compact，不设最大续接深度。
- `agent_core/subagent_compact_continuation.py`：子代理 runner 的任务本地接续片段。它只从 `context_bundle.workspace_refs` 指向的 run workspace 读取 bounded checkpoint/summary/task/findings 和最新 continue packet 摘要，生成 `Task-Local Compact Continuation`；不读取主代理 home 关键文件、不写任何 memory、不执行工具。
- `subagents/services/compact_continue_packet.py`：子代理保存闭环的写入层。`SubAgentManager.save()` 会通过它把当前任务状态、下一步、blockers、`work_progress` 和恢复 refs 写成 `compactions/session/latest_continue_packet.json`，并在 `compactions/session/session_compact_ledger.jsonl` 追加去重后的状态行；父级重新 dispatch 时仍只是按 refs 接续，不直接执行工具。
- `subagents/services/session_progress.py`：子代理 runner 的 task-local 工具进度层。成功写文件后会在 `agents/<run_id>/progress/` 写 `latest_tool_progress.json` 和 `tool_progress.jsonl`，让 compact/retry 能看到最近写入路径、章节标题和下一步，避免重复写已完成部分。
- `subagents/services/subagent_session_compact.py`：子代理本地 session compact package 写入层。子代理 runner 的 compact 信号会写进当前 run workspace 的 `compactions/session/`、package refs、`latest_metadata.json` 和 `latest_summary.md`，不写主代理 `memory_archive/compact_applies`；continue packet 会引用这些 task-local refs。
- `agent_core/finalization_compact_auto.py`：从 finalization 主文件拆出的 compact auto 字段投影层；负责 run 收尾触发 auto cycle、把 continue packet 暴露到 `AgentRunResult`，以及续跑轮跳过再次 compact 的固定字段。
- `memory_archive/tool_output_externalizer.py`：在工具循环归档时把超过阈值的大工具输出写成当前 owner home 下的 `memory_archive/artifacts/tool_outputs/<tool>-<call>-<hash>.json`，并追加 `index.jsonl`；外置阈值和 preview 长度来自 `AgentConfig`，archive/tool event 只保存 preview/hash/path/size。旧 workspace 根下的 `memory_archive/` 不再作为新工具输出归档目标。
- `memory_archive/artifact_reader.py`、`memory_archive/artifact_read_modes.py` 和 `tooling/artifact.py`：`read_artifact` 只读已登记 artifact，默认读取长度和滚动读取预算来自 `AgentConfig`；调用方显式传 `max_chars=0` 才读取完整正文。
- `agent_core/tool_output_failsafe.py`：在调用 tool output externalizer 前写 recovery snapshot，保留工具名、hash、大小、run/task/request id 和下一步建议；完整输出正文仍只在 artifact 文件里。
- `agent_core/tool_context_reducer.py`：控制工具结果进入下一轮 live prompt 的形态；大输出只注入 preview、artifact path、hash、size 和 fail-safe checkpoint，小输出仍保留原始工具结果文本。
- `memory_archive/snapshots/_helpers.py`：把 snapshot 中的工具调用压成恢复安全 metadata；现在会保留 output hash、size 和 externalized 状态，不保存正文。
- `memory_archive/control_plane.py`：提供 `query_memory_control_plane()` 只读入口，汇总 daily event、task/run refs、compact apply ledger 和 tool output index；返回 preview/hash/path/counts，不读取 artifact 正文，也不写任何 workspace 文件。
- `memory_archive/schema.py`：定义 runtime memory schema v2 的统一版本号、`schema` 描述和 `reserved={schema_name,schema_version,extensions,compat,future}` 结构；daily ledger、task/run refs、compact apply 和 tool output index 先共用这套形状。
- `memory_archive/shared_workspace.py`：同步 task-local shared blackboard、status messages、findings 和 evidence packet 文件；messages 追加，findings/evidence index 按 id 合并，blackboard 从合并后的共享事实重建。这是 sibling 子代理协作面，不写入主代理长期 memory。
- `memory_archive/memory_gate_export.py`：只在显式命令触发时，把 `approve_memory` 候选写入主 JSONL memory，或把 `approve_skill` 候选写成 run-local skill draft；不会自动安装正式 skill。
- `memory_archive/memory_gate_retention.py`：生成 retention 计划，apply 时只从 active review queue 移除 closed 候选，候选、decision 和 export 审计日志继续保留。
- `memory_archive/memory_gate_verifier.py`：写 `verifier_report.json`，检查 decision/export 是否仍保持 `auto_promote=false` 和显式提升边界。
- `memory_archive/runtime_fact_source.py`：真实 `run --save` 会写 `memory_archive/runtime_facts/<request_id>/task.json`，记录 goal、next_actions、运行状态，以及用户 prompt 中明确标注的 acceptance/constraints/latest_tests；`memory-fact-write` 也会把用户确认的补全事实写入同类 `task.json`。普通模型回复不会被解析成验收事实。
- `docs/modules/memory/06-runtime-memory-requirements.md`：定义 memory 作为运行时档案系统的开发要求，明确主代理长期记忆、每日账本、task workspace、agent run workspace、artifact、checkpoint、compact 和 retention 的边界。
- `memory_archive/resume_brief.py`：把归档、LocalStore、任务事实源压成恢复简报。
- `memory_archive/resume_context.py`：在“继续/恢复”类提示里按配置构造自动注入的恢复上下文；自动恢复条数、归档扫描条数和文件扫描条数都来自 `AgentConfig`。
- `memory_archive/tokens.py`：为归档预算提供保守 token 估算，并维护 session 级 token 账本。
- `memory_archive/compact.py`：构建只读 compact plan，汇总 raw/hook、权威 snapshot、token ledger、风险和建议动作。
- `cli/memory_commands.py`：给用户和开发者看 route/doctor 结果。
- `cli/home_runtime_commands.py`：提供 `home-status`、`memory-daily-list`、`task-workspace-list`，让人和前端直接检查家目录、按天记忆流水和任务工作区。
- `cli/memory_archive_commands.py`：给用户查看归档列表、搜索归档、生成恢复简报，并提供 `memory-fact-write` 写入用户确认的 compact 补全事实。命令文件只做参数编排；当前 owner archive 根目录解析已拆到 `cli/memory_archive_roots.py`，输出格式拆到 `cli/memory_archive_rendering.py`。
- `cli/memory_archive_roots.py`：返回当前 agent 允许查询的 archive roots。provider user/group 只读自己的 owner home；local/main 先读 owner home，并在迁移期保留旧 workspace archive 兼容读取。
- `cli/memory_archive_rendering.py`：集中渲染 `memory-archive-list`、`memory-archive-search` 和 `memory-resume` 的 JSON/文本输出，避免命令入口因展示逻辑继续膨胀。
- `cli/memory_resume_compact_rendering.py`：承接 `memory-resume --from-compact` 的 handoff、continue packet、completion prompt 和推荐路径输出，避免 archive CLI 编排继续膨胀。
- `cli/memory_compact_commands.py`：把手动/救援 compact plan 暴露为 `memory-compact --dry-run`；显式 `--apply` 时只生成 compact context、metadata、apply bundle、restore refs、work state snapshot、ledger 和 self-check，不做 destructive rewrite。普通运行里的上下文压缩主路是 automatic runtime compact，不要求用户手动跑这个命令。
- `cli/context_bundle_commands.py`：提供 `context-bundle latest --json`，只读查看最新主代理任务卡、scope、RunScope、ToolManifest、Acceptance Contract、自检和 prompt budget；它不调用模型、不写文件。

## 数据流

1. 用户对话或命令触发记忆写入。owner-backed agent 的长期记忆先落到 `owner_home/memory/long_term/memory.jsonl`；旧 `memory_path` 不再作为新写入目标，也只给 local/main 做 fallback read path，外部 provider owner 不默认读取它。
2. LocalStore 可以为 memory JSONL 补建索引，让搜索和 timeline 能看到它；daily mirror 和旧 memory_path 现在都是读取侧补充事实源：`memory-search` / `agent.recall()` 在主 JSONL 缺失或索引命中不足时会回到这些 fallback 事实源。
3. `memory-daily-list` 直接读 `memory/daily/YYYY-MM-DD.jsonl`，可按 date/role/kind/query 查当天流水；它是调试入口，不会调用模型、不改记忆。
4. 当新任务需要规则时，memory routing 根据 query 匹配 route index。
5. 匹配到的 authority path 会被安全读取成上下文片段。
6. token 预算逼近阈值时，run 主链路先写 `memory_archive/snapshots/*.json` 权威快照，再做组合压缩；快照内容必须带上 routed memory context 和 auto resume context，保证 compact 后恢复能回到同一批 authority path 和恢复线索。
7. 长任务和普通保存路径都会继续写 raw event / hook snapshot，方便恢复和审计；工具循环还会在运行中把助手工具轮、工具结果和周期 checkpoint 增量写入同一个 raw archive。
8. raw event、hook snapshot 和权威快照写完后都会读回校验，确保恢复线索真实落盘；live raw archive 写入失败只进入工具记录提示，不中断当前任务。
9. subagent 保存时会同步当前任务根 `work/state.json`、`work/timeline.jsonl`、`work/summaries/current_summary.md` 和 legacy run adapter；完整权威状态写入 `work/agents/<run_id>/canonical_state.json`，旧 `subagents/<run_id>/task.json` / `run.json` 镜像同一 payload，只作为兼容定位入口。
10. 主代理普通 run 会创建 `tasks/{date}/{task_slug}/`；新写入的权威位置统一是当前 `owner_home/tasks/...`，包括 local/main。旧顶层 `~/.my-agent/tasks/...` 只作为 local/main 历史读取和迁移兼容。根目录只有 `output/` 和 `work/` 两块。`task-workspace-list` 可直接列出当前 owner 的这些目录，`memory-resume --task-id/--run-id` 在旧 subagent 工单不存在时会回退读取其 `work/state.json` 和 `work/timeline.jsonl`。
11. 同一保存流程会同步 `tasks/{date}/{task_slug}/work/agents/<run_id>/` 的 agent run workspace skeleton，先写恢复和接管需要的 run 文件、`canonical_state.json` 和 refs，不搬迁旧工单目录；随后更新 `tasks/{date}/{task_slug}/work/compact/task_rollup.json`，让父代理恢复时先看任务级总摘要，而不是乱翻每个子代理 compact。旧 `tasks/<root_id>/agents/...` 和 `tasks/<root_id>/compact/...` 只作为读取迁移兼容，不再作为新写入位置。子代理保存时还会把 task/run/agent 三层 refs 写进 owner global index，父代理、tree、doctor 只靠这套轻量索引发现入口，不再另建一套事实账本。
12. 同一保存流程会追加 `daily/YYYY-MM-DD/events.jsonl`，作为主代理按天查 task/run/event/artifact refs 的轻量索引。
13. 同一保存流程会写 task/run artifact manifest，并让 daily ledger refs 指向 manifest；需要正文时再读 workspace 边界内的 artifact 文件本身，越界路径只保留 blocked manifest 记录。
14. 同一保存流程会追加 run `compactions/compaction_ledger.jsonl`，写 checkpoint snapshot summary/metadata，并让 run `checkpoint.json` 指向最新 compact refs；这不是删除上下文的 compact apply。
15. 同一保存流程会同步 `shared/blackboard.md`、`messages.jsonl`、`findings.jsonl` 和 `evidence_packets/`，让 sibling 子代理共享任务局部 facts；其中 messages 追加，findings/evidence 按 id 合并，避免最后一次保存覆盖其他 sibling 事实。这仍然不是主 memory 写入。
16. `subagents-memory-gate` 默认只列出候选或写 `decisions.jsonl`；只有显式 `--export-memory` 才写主 JSONL memory，只有显式 `--export-skill` 才写 skill draft，`--retention-apply` 也只压缩 active queue，不删除审计日志。
17. 用户说“继续/恢复”时，resume context 可以按配置从 archive、LocalStore、daily ledger、旧 subagent 工单和 home task workspace 生成恢复块；跨天时会同时扫描最近 raw/hook 文件。archive 扫描按当前 owner home 限定：provider user/group 不读 local/main，local/main 在迁移期可同时读 owner home 和旧 workspace archive。subagent 任务会先推荐 `reports/checkpoint.json`、`status_report.json`、`progress.md` 等 compact recovery artifacts，再推荐 `STATUS.md`、`HANDOFF.md` 和 `output.json`。主代理 task workspace 会推荐 `work/state.json`、`work/timeline.jsonl` 和 `work/task.yaml`。`memory-resume --from-compact` 会从某次 compact apply 产物生成恢复块、consistency report 和 action guard。这只是恢复入口推荐，不代表把子代理内容写入主代理长期 memory。
18. doctor 命令检查 home runtime、配置、route index、hook/raw/snapshot 目录和层级一致性 warning；`memory-doctor` 还会嵌入 `home_doctor`，报告 owner home 迁移待办、悬空 task/run/agent index、schema version、snapshot backup、capability request、temporary grant 和 retention 候选。悬空 index 只看每个 owner/task/run/agent 身份的最新引用，历史旧路径不会把当前健康状态误报成坏。这些都只是体检信息，不在普通任务里硬挡。
19. runtime 工具循环遇到大工具输出时，会先写 metadata-only recovery snapshot，再把完整输出外置到 `memory_archive/artifacts/tool_outputs/`，并在 archive_tool_calls / raw tool event 中保存 preview/hash/path/size。
20. tool context reducer 会在下一轮 live prompt 注入前再次检查 archive record：如果输出已外置，只注入 preview、artifact path、hash、size 和 fail-safe checkpoint；完整正文必须通过 artifact 文件显式读取。
21. recovery snapshot 的工具调用 metadata 会保留 output hash、size 和 externalized 状态，方便接管代理知道大输出存在且需要读 artifact；snapshot 不保存完整工具输出正文。
22. `memory-compact --dry-run` 在真实压缩前只读扫描上述事实源，输出计划和风险，不修改文件。
23. `memory-compact --apply` 把同一 scope 的计划落成非破坏性 compact apply 记录：`compact_context`、metadata、apply bundle、restore refs、work state snapshot、post-compact self-check、`compaction_gate` pre 快照和 apply ledger。当前成功状态为 `applied_non_destructive`，只建立恢复入口，不裁剪历史内容；如果 self-check 失败，状态会变成 `blocked_self_check_failed` 并写失败报告；如果 compaction gate 发现关键恢复字段缺失，会写 `blocked_compaction_gate_failed`。每个 run/request/task/session 都有自己的 `runs/<scope_id>/compact_applies/`，全局 ledger 只做查找索引；同一 scope 多次 apply 时，每一包会写 `lineage.cycle_index` 和 `lineage.previous_apply_id`，让第 2 次、第 20 次、第 300 次 compact 都能审计和恢复前后关系。自动绑定最新主代理 context bundle 前会做 scope match；scope 不匹配时不会把无关任务卡写进 restore refs。
24. control-plane query 只读扫描 daily ledger、task/run refs、compact apply ledger 和 tool output index，给 compact/resume/debug 返回统一引用视图；正文核实仍必须回到 task/run workspace、artifact 文件或 raw archive。
25. `run` 收尾会基于 token ledger 触发默认 auto-apply 的 auto compact cycle；达到 `memory_compact_auto_trigger_percent` 时会进入 compact。默认会尝试自动 compact；`save=False` 或 guard 不通过时退回确认建议。若模型后端返回结构化上下文溢出状态，也会强制进入同一个 cycle，并在 `trigger` 里记录兜底原因。
26. `run_memory_compact_auto_cycle()` 是自动 compact/resume 的第一层协调器：保存型运行会尝试非破坏性 apply、auto resume 和 continue packet 检查；`save=False` 或恢复事实源损坏时只生成 plan 和人工确认建议。主 agent 在 action guard 放行、refs/self-check 等恢复硬条件正常时，会把 continue packet 注入下一轮 prompt 并继续同一任务；字段备注缺失只作为 `missing_fields` 提醒。正常触发和兜底触发只差 `trigger` 字段，不差执行链路。
27. compact apply 生成 `work_state_snapshot` 时，只读当前 scope 对应的 task/run 事实源，例如 `subagents/<run_id>/ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md`、`task.json`、`memory_archive/runtime_facts/<request_id>/task.json`、`memory_archive/runtime_facts/<session_id>/task.json` 和 `tasks/*/work/agents/<run_id>/`；旧 `tasks/*/agents/<run_id>/` 只作迁移兼容。不会扫描 workspace 根目录的 `TEST_CHECKLIST.md`，避免把仓库开发清单错当成本轮任务验收。没有权威 snapshot 文件的普通 `run --save` 场景，会从 `restore_refs` 指向的 hook/raw JSONL 回填 goal/next action，但不会从模型回复里猜验收或测试状态。同一步还会把 `task_progress.quality_hints`、最近 guidance 和下级状态汇总成 `runtime_handoff`，帮助压缩后继续聊天、继续看树或继续长任务，但不做硬门。
28. `memory-resume --from-compact` 会把 work state、action guard、fail-safe checkpoint refs、completion prompt、推荐读取路径、compact lineage、compaction gate post 检查和下一步动作整理成 `compact_resume_handoff` 和 `compact_continue_packet`。`compact_continue_packet` 里有两层关键字段：`resume_focus` 用来告诉下一轮“先接着做什么”，`captured_refs` 用来告诉下一轮“哪些读写、产物和 artifact refs 已经捕获过”。上下文块中仍会展示目标、阶段、验收、约束、最近测试、fail-safe checkpoints 和下一步动作，但推荐读取路径是备用证据，不是每次续接都必须先读的清单。若 post 检查发现 apply-time 快照中的 artifact refs、recovery packet 或 pending actions 在恢复包中丢失，则 `compaction_gate_ok=false`，自动继续被阻断。
29. compact apply 在没有权威 snapshot 时，会从 restore refs 指向的 raw/hook JSONL 回填最小工作状态；其中 live `assistant_tool_round` 只能提供下一步续接提示，不能提供验收、约束或测试事实。运行中进度事实以 `runtime_fact` 为准。
30. compact 的 `allowed_to_continue=true` 只代表恢复上下文自检和 work_state guard 允许主 agent 继续下一步，不代表子代理业务验收通过；最终收口、测试执行、apply acceptance 和 rescue 仍由 closeout controller / auto-policy 单独判断。
31. 当 compact resume 指定 `owner_type=subagent_run|subagent_session` 时，系统只读解析 task-local run workspace 和 legacy run adapter refs，给未来子代理会话 compact/resume 留稳定 owner 坐标；这一步不把子代理内容写入主代理长期 memory，也不自动执行工具。
32. Runtime memory 轻量索引记录使用 schema v2：顶层 `version=2`，旁边写 `schema.name/version/reserved_keys`，`reserved` 固定保留 `extensions`、`compat`、`future` 三槽；正式业务字段仍应显式命名，不能把 reserved 当成万能垃圾桶。
33. runtime memory 的跨模块写入入口先把 CLI/manager 参数收敛成 `*Request` / `*Options` bundle，再进入具体 service；这保证后续 memory gate、compact chain、artifact refs、shared workspace 继续扩展时，不影响既有调用方。

## 跨天恢复链路

```text
2026-04-29 raw event
  -> 记录用户当时的任务意图、request_id、run_id、task_id

2026-04-30 hook snapshot
  -> 记录压缩/交接后的恢复锚点、next_actions、task_refs、content_paths

LocalStore subagent_run
  -> 保存可搜索的任务索引，不作为最终事实，只帮助找到 run_id

LocalStore gateway_request
  -> 保存可搜索的 gateway 请求索引，不作为最终事实，只帮助找到 request_id

subagents/<run_id>/
  -> reports/checkpoint.json / status_report.json / progress.md 是 compact-first 恢复入口
  -> STATUS.md / WORK_LOG.md / HANDOFF.md / ACCEPTANCE.md / TEST_CHECKLIST.md 是最终核对事实源

gateway/requests/done/<request_id>.json 或 gateway/requests/failed/<request_id>.json
  -> gateway 请求的终态请求事实源；如果 LocalStore 里还是 processing 路径，resume 会尽量纠偏到这里

gateway/responses/<request_id>.json
  -> gateway 请求的响应事实源；通常要和 request JSON 一起读

memory-resume 或 run(auto resume)
  -> 输出 Recovery Brief，把推荐阅读路径和下一步动作带回父会话
```

## Runtime Memory 目标结构

长期目标以 `06-runtime-memory-requirements.md` 为准：

```text
主代理 = 长期记忆 + 每日事件账本 + 全局索引
任务 = task workspace + state + timeline + summaries + shared + artifacts
子代理 = task 内 agent run workspace + checkpoint + compact chain + findings
共享 = blackboard + messages + evidence packets + artifact refs
```

当前 Phase 0/1/2/3/4/5/6 已创建 task workspace 外壳、agent run workspace 外壳、daily event ledger、artifact manifest、checkpoint-first compact chain、shared 协作面和 run-local memory gate。新任务目录根部只保留 `output/` 与 `work/`，其中子代理账本、compact、runtime 状态都写入 `work/`；旧 `tasks/<root_id>/agents/...`、`tasks/<root_id>/compact/...` 只作为历史读取兼容。全局 `memory-compact --apply` 已能生成 `memory_archive/compact_applies/` 下的非破坏性 apply context、apply bundle、restore refs、work state snapshot、self-check 和失败阻断报告，`memory-resume --from-compact` 已能从这些产物生成手动恢复上下文、consistency report、handoff、continue packet 和 action guard；action guard 在 auto 模式恢复硬条件通过时可以返回 `allow_automated_continue`，acceptance/constraints/latest_tests 缺失只作为提示保留，但仍标记不自动执行工具。`run` 已能在上下文风险达到阈值时触发默认 auto-apply 的 auto compact cycle，并可在显式配置下做非破坏性 apply + auto resume + continue packet 停车。大工具输出已能进入 `memory_archive/artifacts/tool_outputs/`，control-plane query 已能统一查 daily/task-run/compact/tool-output refs，这几类轻量索引已统一到 schema v2/reserved 结构，但还不会删除、重写或裁剪历史内容，也不会自动继续执行工具。`memory_gate/` 保存 review 候选、decision log、export log、retention report 和 verifier report。只有显式 export 命令才会写主代理长期记忆或生成 skill draft。

LocalStore / sqlite / 搜索索引只帮助定位事实源，不替代 task/run 目录里的权威文件。

## My-Agent Home / Provider 空间

新的家目录约定记录在 `docs/architecture/MY_AGENT_HOME_LAYOUT.md`。大白话说：`~/.my-agent/` 顶层放系统和共享能力，CLI 主账号 owner 住在 `~/.my-agent/owners/local/main/`；每天记忆放 owner 的 `memory/daily/`，高频小提醒放 owner 的 `memory-hot.md`，详细教训放 owner 的 `memory/lessons/`，路由表放 owner 的 `memory/routing/INDEX.md`，任务放 owner 的 `tasks/{date}/{task_slug}/`。普通保存型 run 现在只在任务根目录创建 `output/` 和 `work/` 两块，`output/` 是可以复制走的最终交付物，`work/` 保存任务状态、时间线、日志、compact、草稿和下级代理账本；主代理自己的 memory/compact/logs 仍属于 owner，不放进任务的 `work/agents`。`PromptBuilder` 会每轮按 `AGENTS.md`、`SOUL.md`、`USER.md`、`memory.md`、`memory-hot.md` 顺序读取入口文件，并只按文件名匹配少量 lesson，不会每轮全量读教训库；task-local/control-plane 不注入这些 owner 入口。`memory-doctor`、`memory-route` 和运行时路由优先使用项目显式 `memory/routing/INDEX.md`，项目没有时回退到当前 home 的索引。QQ/飞书这类外部平台接入后，才在 `providers/<provider>/users|groups/<id>/` 下给对应用户或群开独立空间。外部用户/群可以有自己的 tools、skills、role_templates、workflows、workspace、memory、trash，但不能写主账号家目录，也不能越权碰别人的空间。

日期窗口说明：`--since YYYY-MM-DD` 从当天 00:00 开始；`--until YYYY-MM-DD` 包含当天全天。这样用户按自然日期查跨天交接时，不会漏掉当天白天的 hook snapshot。

## 给初学编程学生的学习路径

1. 先看 `agent_py_agent/cli/memory_commands.py`，理解用户如何运行 `memory-route` 和 `memory-doctor`。
2. 再看 `agent_py_agent/agent/settings/memory.py`，学习配置如何设置默认值、warning 和安全回退。
3. 再看 `agent_py_agent/agent/memory_store/jsonl.py`，理解 JSONL 事实流水和 LocalStore 索引的区别。
4. 再看 `memory_routing/models.py`，认识 route、match、required/candidate path 和 read receipt 的数据形状。
5. 再看 `memory_routing/matcher.py`，理解关键词和别名如何命中规则。
6. 再看 `memory_routing/loader.py`、`validator.py` 和 `context.py`，理解人工索引怎么读入、怎么校验冲突和死链、authority 文件怎么安全注入。
7. 再看 `memory_archive/models.py`、`storage.py`、`runtime.py` 和 `snapshots.py`，理解归档保存什么、怎么写入、怎么验收、压缩前 hook 为什么必须先成功。
8. 再看 `memory_archive/query.py`、`resume_brief.py` 和 `resume_context.py`，理解“继续任务”时怎么找回线索。
9. 再看 `memory_archive/tokens.py` 和 `agent_core/runtime_mixin.py`，理解 session token 账本和压缩触发点。
10. 再看 `agent_py_agent/agent/memory_archive/task_workspace.py` 和相邻的 runtime sync 模块，理解 `*Request` bundle 如何把 task/run/artifact/checkpoint/shared refs 打包传递，同时保留旧 subagent work-order 兼容路径。
11. 再看 `agent_py_agent/agent/memory_archive/control_plane.py` 和 `agent_py_agent/tests/test_memory_control_plane.py`，理解 daily、task/run、compact apply 和 tool output index 如何先汇成轻量引用视图。
12. 再看 `agent_py_agent/agent/memory_archive/schema.py`，理解 v2 schema 和 reserved 三槽如何让未来字段扩展有明确位置。
13. 再看 `agent_py_agent/agent/memory_archive/memory_gate.py`、`memory_gate_retention.py`、`memory_gate_export.py`、`memory_gate_verifier.py` 和 `agent_py_agent/agent/subagents/manager_memory_gate.py`，理解 lesson/finding 为什么先进入 review queue，以及 reviewer decision 为什么仍然不等于正式提升。
14. 再看 `agent_py_agent/cli/_memory_gate.py`，理解 `subagents-memory-gate` 如何显式写回 decision、导出 approved 候选、生成 skill draft 和跑边界检查。
15. 再看 `agent_py_agent/tests/test_memory_archive_cli.py::test_memory_resume_cross_day_handoff_uses_task_fact_sources` 和 `test_memory_runtime.py::test_auto_resume_context_recovers_cross_day_handoff_task`，理解 subagent 跨天恢复如何从线索回到事实源。
16. 再看 `agent_py_agent/tests/test_scenario_gateway_resume.py`，理解真实 gateway 请求和 parent/subagent runner 结果如何通过跨天恢复回到文件事实源。
17. 最后看 `agent_py_agent/tests/test_memory_*.py` 和 `test_memory_first_loop.py`，用测试反推每一层必须保证的行为。

## 当前第一版索引 / 待补齐

本页先讲主结构和阅读路径。后续需要补真实 route index 样例、raw archive 样例、doctor 输出样例、LocalStore 命中样例，以及更长时间的真实跨午夜恢复链路图。
## 2026-05-06 structure update
- 中文说明：memory archive query 和 memory routing matcher 内部已经拆成过滤、payload 构造、gateway payload 收集、resume guidance 渲染、评分规格和 receipt 创建等小职责；公开输出保持稳定。
- Memory archive query logic now delegates filtering, task payload construction, gateway payload collection, and resume guidance rendering to focused helpers.
- Memory routing matcher internals separate scoring specs, score accumulation, and receipt creation while keeping public model outputs stable.

## 2026-05-07 bundle structure update
- 中文说明：memory archive query、resume brief、snapshot helper 和 routing read receipt 现在用显式 Params/Options 字段表达输入；这不改变权威来源模型，task/run/gateway 文件仍是事实源，LocalStore/archive 只是线索。
- `memory_archive/query/query_logic.py` now exposes explicit archive collection/filter fields, and higher-level query service code converts compatibility inputs before filtering.
- `resume_brief.py`, snapshot helpers, and memory route read receipts now use explicit Params/Options-style fields rather than var-keyword option bags.
- Memory remains a fact-source locator: bundle cleanup does not change the authority model where task/run/gateway files stay authoritative and LocalStore/archive records are clues.

## 2026-05-07 high-risk structure update
- 中文说明：task workspace 的 payload/JSON/JSONL IO 从编排入口拆出去，archive query、resume brief、memory gate review 和 routing matcher 也改用 request/params bundle；文件形状和事实源权威关系不变。
- `memory_archive/task_workspace_payloads.py` now owns task workspace state/timeline payload construction and JSON/JSONL helper IO.
- `memory_archive/task_workspace.py` remains the adapter/orchestration entry point and keeps the same public `EnsureSubagentTaskWorkspaceRequest` / `TaskWorkspacePaths` surface.
- `memory_archive/query/query_logic.py`, `resume_brief.py`, `memory_gate_review.py`, and `memory_routing/matcher.py` now use request/params bundles for collection, filtering, reviewer write-back, and read receipts without changing the fact-source authority model.

## 2026-05-07 annotation structure update
- 中文说明：结构文档把 memory 代码里的双层注释纳入同步要求。新增文件、服务、bundle 或 facade 方法时，要同时更新结构页和 in-code 注释。
- Module structure docs now treat the definition-level double-layer comments as part of the code architecture: `LLM:` records model-facing contract/caller/side-effect notes, and `函数用途:` / `类用途:` records beginner-readable purpose and edit guidance.
- New files, services, bundles, or facade methods must update both this structure page and the in-code comments at the same time.
- The global file tree in `CODEBASE_TREE.md` now includes a current architecture map for CLI, agent core, gateway, memory, log-analysis, subagent, tooling, and settings boundaries.

## 2026-05-08 compact resume fail-safe structure update
- `memory_archive/compact_resume_failsafe.py` owns metadata-only tool-output fail-safe checkpoint extraction from restore refs that point to hook JSONL files. The extractor records path, line number, snapshot id, source/status, request/run/task ids, output hash, output size, and next actions.
- `memory_archive/compact_resume.py` now calls that extractor before building recommended read paths, so the main resume orchestration file does not keep growing with JSONL scanning details.
- `memory_archive/compact_resume_handoff.py` now carries the same checkpoint refs into `compact_resume_handoff` and renders a `Fail Safe Checkpoints` section in the context block.
- The structure remains refs-only: `memory-resume` may inspect hook checkpoint metadata, but it does not read or inline externalized tool artifact bodies. Artifact bodies stay behind explicit artifact path reads.

## 2026-05-08 artifact explicit read structure update
- `memory_archive/artifact_reader.py` owns indexed tool-output artifact body reads. It treats `index.jsonl` as the authority, validates the registered path boundary, verifies sha256, and returns explicit slices.
- `cli/memory_artifact_commands.py` exposes `memory-artifact-read`, keeping failed reads metadata-only and successful reads clearly marked with `reads_artifact_body=true`.
- `tooling/artifact.py` keeps `read_artifact` as the strict recovery/audit reader for indexed tool-output refs; ordinary files, explicit large-output paths, and safe tool-output wrapper paths go through `read_file`.

## 2026-05-11 artifact copied-prefix recovery structure update
- `memory_archive/artifact_reader.py` 现在在精确 path/hash/call_id 匹配失败、且 ref 看起来像路径时，会尝试用唯一 artifact 文件名回到已登记 index 记录；这是为了修复模型复制路径前缀时把 workspace 根写错的真实 E2E 问题。
- 这个 fallback 仍然是 index-first：同名不唯一、未登记 artifact、目录越界或 sha256 不一致都会失败；成功读取仍只返回显式 slice，不把 artifact 正文塞进普通恢复上下文。

## 2026-05-08 artifact explicit read CI follow-up
- `artifact_reader.py` 的路径根、目录边界、path-like ref 判断和 sha256 helper 现在都有定义级用途说明，后续维护者能直接看到这些 helper 是任意文件读取防线的一部分。
- `__main__.py` 的 CLI 导入顺序已按 ruff 统一格式整理；结构和命令语义不变。

## 2026-05-08 compact/resume safety structure update
- `agent_core/_finalization_service.py` 的 auto compact apply 入口现在必须同时满足配置允许和当前 run `do_save=true`，把 `--no-save` 保持为硬持久化边界。
- `memory_archive/runtime_fact_source.py` 的显式段落解析遇到未知标题会重置 active bucket，避免后续实施说明被升级成 work-state facts。
- `memory_archive/compact_work_state_sources.py` 按字面 id 查找 task/run/runtime_fact 目录，`request_id` / `session_id` 中的 glob 元字符不再参与文件系统模式匹配。
- `memory_archive/compact_resume_completion.py` 渲染 suggested commands 时保留原 compact scope flags，用户复制命令不会意外跑无范围 `memory-compact --apply`。

## 2026-05-11 scoped artifact-ref structure update
- `memory_archive/tool_output_externalizer.py` 写 tool-output artifact 时会同时登记 `request_id`、`run_id`、`task_id` 和 `scoped_call_id`。`scoped_call_id` 的形态是当前 run/task/request scope 加短调用号，用来避免不同子代理、不同轮次都叫 `17-1` 时互相串线。
- `memory_archive/artifact_reader.py` 现在把 `index.jsonl` 当成带作用域的登记表：先按 path/hash/scoped_call_id/call_id 找候选，再优先选择当前 `run_id/task_id/request_id` 匹配的记录；没有 scope 的 legacy 调用只回退到最新候选，不再读第一条旧记录。
- `agent_core/_tool_loop_service.py` 会把当前 subagent run scope 注入 `read_artifact` 参数和工具输出归档记录；`agent_core/subagent_run_flow.py` 也会把 subagent run/task id 传进 `agent.run()`，让工具链能从调用栈拿到稳定 scope。
- `agent_core/tool_context_reducer.py` 的 live prompt 只给模型 scoped 读取线索：artifact path、hash、`output_scoped_call_id`、run/task/request flags 和 preview；完整 artifact 正文仍必须通过 `read_artifact` 显式读取。
- 这条结构规则用于修复真实 E2E 的 prompt/路径误传问题：模型看到的引用不能只是人类可读短号，必须能绑定到当前任务、当前 run、当前 workspace。

## 2026-05-13 artifact read mode and budget structure update
- `memory_archive/artifact_reader.py` 继续是严格 artifact ref 读取入口；现在 `ReadToolOutputArtifactRequest` 增加 `mode` 和 `query` 字段，支持 `slice/head/tail/search` 四种窄读方式。所有模式仍先查 `tool_outputs/index.jsonl`、校验目录边界和 sha256。模型已经拿到明确安全路径时，不必绕到这里；普通读取统一用 `read_file`。
- `memory_archive/artifact_read_modes.py` 承接正文 shaping：slice/head/tail/search 的 offset、截断、匹配行和附加元数据都在这里处理，避免 artifact index 读取层继续增长。
- `memory_archive/artifact_reader.py` 还提供 `estimate_tool_output_artifact_size()`，只读 index 的 `size_bytes`，不打开正文，用于 `max_chars=0` 这类无界读取的预算预判。
- `tooling/artifact_read_budget.py` 是 read_artifact 的单 run 正文读取预算器；它记录 `run_id -> [(timestamp, chars)]`，只限制带 run scope 的子代理读取，不限制普通主代理聊天。
- `tooling/artifact.py` 把工具参数转成 `ReadToolOutputArtifactRequest` bundle，再先做预算 preflight，成功读取后按实际 `content_chars` 计费。这样预算逻辑不散落到 reader 或 tool loop 里。
- `tooling/filesystem_artifact_guard.py` 负责识别 `memory_archive/artifacts/tool_outputs/*.json` 包装文件；`read_file` 会读取其中 `content` 正文并按普通文件分页。只有短 call id、hash、compact/resume 这类需要防串 run 的场景继续走 `read_artifact`。

## 2026-05-14 compact multi-hop and subagent owner structure update
- `agent/action_protocol.py` 现在作为 typed protocol facade 导出 `CompactContinuePacketEnvelope` 和 `PathRef`，具体 compact envelope 在 `action_protocol_compact.py`，共享 refs 在 `action_protocol_core.py`；`memory_archive/compact_continue_packet.py` 会把旧 continue packet 同步包装成 `typed_envelope`，推荐读取路径进入结构化 `path_refs`，避免后续自动恢复解析自然语言说明。
- `agent_core/runtime_mixin.py` 现在把自动 compact continuation 当成一个同任务续接循环：每一轮都读上一轮 result 里的 continue packet，调用 `_compact_auto_continue_params()` 注入恢复块；不再有最大续跑深度配置，能不能继续只看恢复包、自检、保存边界和运行错误。
- `agent_core/finalization_compact_auto.py` 通过 `model_context_window.py` 解析 compact 阈值窗口：优先读取当前模型后端暴露的真实元数据，拿不到时用 128K 通用窗口兜底；provider 报 context overflow 时仍走同一套 compact 兜底。它仍先检查 `ctx.do_save`，所以 `save=False` 不会写 compact apply。
- `agent_core/_finalization_service.py` 给 auto compact 传真实 per-run request id，并把 `# Compact Auto Continuation` runtime injection 传给 runtime fact source，保证后续 apply 能继承显式验收、约束和最近测试字段。
- `memory_archive/runtime_fact_source.py` 同时解析用户 prompt 和 compact continuation 注入，但只接受明确的 `Acceptance`、`Constraints`、`Latest Tests` 段落；其它恢复说明不会被升级成事实。
- `memory_archive/compact_apply_work_state.py` 读取 restore refs 指向的 shared raw/hook JSONL 后，会重新用 `session_id/request_id/run_id/task_id` 过滤记录。raw 事件从顶层字段取 scope；hook snapshot 从 `turn_range` / `dispatch_events` 取 scope。
- `memory_archive/compact_work_state_sources.py` 现在能从 task/run fact source 读取 `goal` 和 `next_actions`，用于补足 auto continuation 后的 work_state；续接轮会保留 `root_user_prompt`，不会把“继续执行 Compact Auto Continuation”写成本轮真实目标。
- `memory_archive/compact_subagent_owner.py` 是 `memory-resume --from-compact` 的只读子代理 owner resolver。它只搜索 active workspace 和配置里的 `subagent_workspace`，owner id 按字面路径段处理，并可从 legacy `task.json` 升级到新的 agent-run workspace。
- `memory_archive/compact_continue_packet.py` 会把 subagent owner 的 `recommended_read_paths` 带进 continue packet；这些路径只指向 agent-run workspace 的 packet/checkpoint/summary/task/timeline/findings，不复制主代理长期 memory。
- `cli/memory_archive_commands.py` 负责把当前 agent 的 `subagents.workspace` 传给 resume options，CLI 不再让 compact owner resolver 猜默认路径。

## 2026-05-15 LocalStore scoped memory structure update
- `memory_store/_jsonl_indexing.py` 现在把当前 `JsonlMemory.path` 写入 LocalStore 记录的 `metadata.memory_path`，并在搜索返回前按同一个路径过滤 memory hits。
- 这个 scope 只约束 `source_type=memory` 的 LocalStore 命中；task/run/archive/tool-output 这些其它索引仍按各自 refs 和 scope 规则处理。
- 结构原则：LocalStore 是加速索引，不是跨空间权威记忆池。临时 E2E、多用户空间或不同 `memory_path` 不能互相读到对方的旧 hit；需要恢复旧事实时必须回到对应 JSONL/daily/task/run 文件。
