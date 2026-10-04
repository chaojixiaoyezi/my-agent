# 子代理维护状态

2026-10-04（rco，分支 `worker/run-closeout`，基于 step17i 头 `ce646f783`，已实现待复审）：子代理收口 `closeout_target_run_status` 补失败族映射——BLOCKED/CHANNEL_ERROR/TIMEOUT（与 `SUBAGENT_FAILURE_STATUSES` 同口径）随 FAILED 收口 run=failed，杜绝 runner 已退出而 run 停在 created；PENDING/RUNNING/PLANNING/PAUSED 等可恢复形态仍不收口。同批主代理侧 `_settle_main_agent_run_status` 非终态统一分族（不可续跑族收口 failed、可续跑族保留），见 TESTS.md 顶部与设计台账。

2026-10-03（mtc，3a 挑入）：子代理解析缓存的指纹与 2 秒窗口阈值改为引用 `common/cache_freshness` 的共享实现；命中与“窗口内读到的不入缓存”行为不变（luna6 口径）。

## 私有写只动自己建的东西（pdp，2026-10-03，分支 `worker/private-dirs-policy`，基于集成头 `3a42f457d`，待复审）

- 子代理宿主数据的私有写改口径（3a 裁定，与锁收私 ds8 同口径）：缺失目录按 0700 新建、已存在的目录一律不改。
- 跟着改的建目录点：`subagents/debug_trace`（debug details 目录）、`subagents/execution/report`（测试执行报告目录）、`subagents/manager_work_orders`（任务各目录）、`subagents/services/actions/records`（work log 目录）、`subagents/task_trash`（回收站目录）——统一 `mkdir(..., mode=0o700)`。
- `subagents/shell_gateway_execution` 的 artifact 目录（用户可见）保持不变：私有审计写只新增 0600 文件，不再收紧它。
- 顺带修复：pw2 引入的 `subagents/execution/report` 导入点数错误（`....common` → `...common`），该模块此前无法导入、CLI 构建解析器会崩；已在基线 `3a42f457d` 复核确认是既有失败。

## 子代理工作区文件私有写入第二批（pw2，2026-10-03，分支 `worker/private-writes-batch2`，待复审）

- 子代理工作区里剩下的整份报告/任务文件写点全部改走 `common/json_io` 私有原语（文件 0600、目录 0700、存量宽权限下次写入收紧，内容逐字节不变）：
  `patch/patch_service`（补丁复审 Markdown、`output.json`）、`patch/patch_apply_task`（`output.json`）、`execution/report`（测试执行报告 JSON/MD）、
  `services/hierarchy/service`（领导恢复计划/应用报告 JSON/MD）、`result_processors`（runner 提示词/回复/结果 JSON）、`probe`（通道探针证据文件）、
  `task_trash`（回收站清单）、`manager_work_orders`（接管文件，经 `utils` 模板）、`shell_gateway_execution`（审计 JSONL）、`runner_context_bundle_files`（上下文快照 JSON/MD）。
- `patch_file_ops` 写的是用户补丁的目标文件，按口径**不动**。
- 用例 `tests/test_private_writes_batch2.py`（C 组 5 条）：umask 0o022 下新建 0600/0700、预置宽权限目录写一次收紧、与公开版本内容一致；两个退回跟随 umask 的变异均被拦截。验证命令见 [TESTS](../../../TESTS.md)。

2026-10-03（luna6i，ae 二次复看补强，`worker/luna6-idem-flake`）：修正规则为 mtime 窗口内的 cache miss 只返回当前 canonical 读取结果、不入缓存并清掉同 run 旧项，2 秒窗口外才保存；增加同指纹写入新状态后跨窗重读的 ABA 正式探针。缓存时钟由 persistence service `_now()` 隔离，DONE 复用测试不再清空缓存；缓存命中类夹具 mtime 调到窗口外。具体先红后绿和门禁回执见 TESTS.md 顶部。

2026-10-03（luna6i，分支 `worker/luna6-idem-flake`，ae 复审通过，并入 step17i）：解决子代理列表缓存只看 `task.json` 的 `st_mtime_ns`，导致同 mtime 的原子替换未使缓存失效、items 复用保存可将 RUNNING 旧快照写回 PLANNING 的竞争。缓存改核对 `(st_dev, st_ino, st_size, st_mtime_ns, st_ctime_ns)`；受控同 mtime 回归先红后绿，定向和 guards9 已跑过，完整命令及边界见 TESTS.md 顶部。

2026-10-03（luna6i，ae 复审补强，`worker/luna6-idem-flake`）：同文件指纹的 mtime 距当前不足 2 秒时绕过解析缓存重读，2 秒外仍复用；两项幂等派发测试用真实启动桩固定状态，并集合精确检查 DONE/RUNNING 排除、PLANNING 全部派发。阈值与 Gateway 粗 mtime 保护同为 2 秒，不新增配置或跨层依赖；定向 13 个测试文件已过，静态门禁和提交结果见 TESTS.md 顶部。


2026-10-02（第 14 条，ef，分支 `claude/ef-subagent-media`，基于 `claude/3a-step16z` `5e972003e`，本地已实现、待集成，默认关）：派子代理时可以把父会话的
图片/视频按结构化引用交给子代理。capability 开关 `subagent_input_media_enabled`（`config/capability_config.yaml`，运行时经
`capability_config_for_agent` 读，管理员 `/settings` 可开关；首版误放主配置，已挪）打开后：带附件的回合在当前回合 IR 末尾多一条宿主事实
`[INPUT_MEDIA_MANIFEST]`（每个附件一个 `media_ref`=sha256，不含路径）；`create_subagents` 顶层与 `items[]` 多 `input_media_refs`；
宿主 `orchestration/input_media_refs.bind_subagent_input_media` 只在父级本轮附件和父级 transcript 的 canonical 用户媒体块里按哈希查找，
再按 owner 附件根与配置上限重验，写进 child 任务属性 `input_media`（与主会话同键）；未知/重复/格式错/超限整批 `not_started`
（`SUBAGENT_INPUT_MEDIA_INVALID`），开关关闭传了引用整批 `SUBAGENT_INPUT_MEDIA_DISABLED`，模型塞进 attributes 的同名键丢弃。
child 首个请求的用户轮经原 typed media 管线带上媒体，首轮选模的冻结请求按 J11 同一函数判定模态；child 线程 canonical 行保留 `local_file`，
压缩/恢复沿主会话同一路径。仓库 fake/组件链已验；真实带图 child 待 MiniMax M3 隔离核对。

2026-10-02（J11，分支 `worker/sol56-j11-modality`，本地已实现、待集成）：子代理首轮自动选模现在从冻结的
`ToolLoopRequestInput` 读取 canonical `UserTurn.media` / provider `local_file` 媒体块，并与模型档案
`input_modalities` 共用 Gateway 同一判定函数。image/video 只允许显式声明支持的候选；未声明、缺失模态和全无兼容候选都有
固定结构化原因，全无兼容项时保留继承模型。纯文本继续兼容旧档案；历史、未知块或跨模型 reasoning 无可靠模态事实时仍为
`history_modality_unknown`，不从任务正文、模型名、文件名或模型自述猜能力。仓库 fake/组件链与三个变异已验；真实带图 child 未验证。

2026-10-02（分支 `worker/ds1-p10-batch2`）：常数整改第二批。subagents 目录 10 个文件的常数补齐上方中文说明，缺单位的按生成器后缀表改名（如 `PARENT_CHAIN_LIMIT`→`PARENT_CHAIN_COUNT`、`MAX_LESSONS_PER_RUN`→`MAX_LESSONS_PER_RUN_COUNT`），数值一律不变；待整改白名单 685→600，目录重建后 --check 一致。

## C7：子代理八档与首轮投影（2026-10-02，sol，本地已实施，待集成）

顶层及逐项 effort schema 从唯一八档定义派生，xhigh/ultra 显式或继承时仍保存用户值；不保存父模型已降档的值。
child 首业务请求与候选投影依自身后端声明换算，原冻结属性、线程优先、采用门与关闭思考优先不变。
原首请求真实产品调用链配假 HTTP 的用例覆盖新档，组件证据不代表真实子代理模型验收。详见 TESTS 与智能程度第 9 节。

2026-09-30（分支 `worker/ds1-takeover-event`）：接替已结束或阻塞的子代理时，追加式事件日志新增专门审计事件 `subagent_takeover_recorded`
（`services/takeover/record.py` 在落盘核对通过、TAKEOVER.md 写完后追加，复用 `manager.log_local_record` 通道），payload 带
`source_run_id`／`successor_run_id`／`disposition`（superseded 或 taken_over）／`record_id`／`created_at`；只读投影，不改状态语义，
写入失败与 `subagent_run_saved` 一致（吞异常只记 warning），二次接替被预检拒绝时不产生新事件。结构见
[04-structure](04-structure.md#接替关系的唯一落账入口2026-09-28)，测试见[测试记录](../../../TESTS.md)。

2026-09-30（分支 `worker/ds2-capability-guide`）：list_agents 与代理树节点新增 `model`、`reasoning_effort` 两个只读字段，展示子代理实际使用的模型与智能程度。权威来源是已物化的子代理线程（`ConversationThread.model_profile_id` / `reasoning_effort`），线程未物化/读取失败时回退 kernel 快照里创建时冻结的任务属性（`host_model_profile.v1` / `host_reasoning_effort.v1`）。模型显示为“名称（编号）”，default 显示“继承会话默认”，档案删除或读取失败显示“未知”；档位未设置显示“默认”；输出绝不含密钥、地址或请求头。对应实现：`subagents/kernel.py`（SubagentKernelRun 冻结值回退源）、`agent_core/agent_tree/node_rendering.py`（投影与名称解析）、`agent_core/agent_tree/model_view.py`（白名单）。测试见 TESTS 顶部本节。

2026-09-29（分支 `claude/75-scheduler-waiting-deadlock`）：`runner_completion_wake.has_persisted_subagent_parent` 改为公开函数（原私有名
`_has_persisted_subagent_parent`，行为不变），供 `conversation/task_follow_up` 判定“直属会话的子代理才会给会话发完成唤醒”时复用同一判据。

2026-09-28（分支 `claude/9a-capcfg-fallback-cleanup`）：巡检阈值不再自带兜底数字。
- `due_check_settings` 没收到配置时用 `CapabilityConfig()`。
- 删掉看板里从未命中的 `_make_default_capability_config` 分支：没有任何类定义这个方法。
- `DueCheckSettings.no_progress_attempt_limit` 去掉默认值 4。

行为不变，见 TESTS 顶部本节。

2026-09-28：`SubAgentBaseService` 压到 200 行以下（分支 `claude/75-subagent-base-slim`，行为不变）。建 run 的权威写入`_write_authority_records` 与链身份回存移到新模块 `services/run_authority.py`（`write_create_run_authority`），任务落盘登记 `_finalize_task` 改为同文件模块函数 `_finalize_created_task`；`create_run` 的调用顺序与异常不变。类长 239→189，code-size 只消失该类的 high-risk、无新增；依赖原方法的崩溃重试测试改为 patch 新函数。

2026-09-28：进入被接替的子代理页后，头部也标出“已被 X 接替”（分支 `claude/ae-tui-superseded-marker` 第 3 个提交），数据仍只来自 kernel 的 `replaced_by_view`；CLI board 按集成方决定不做。

2026-09-28：TUI 子代理名册对被接替的子代理标出“已被 X 接替”（分支 `claude/ae-tui-superseded-marker` 第 2 个提交），数据只来自 kernel 的 `replaced_by_view`，不另外推断；CLI board 暂未标注。

2026-09-28：`subagent_run_saved` 事件的 payload 在有值时带上 takeover_by／superseded_by（分支 `claude/ae-tui-superseded-marker` 第 1 个提交），不新增事件类型，时间线与审计投影能看到接替关系；权威仍是任务记录里的 takeover_records。

2026-09-28：已结束子代理被显式接替时终态不再被静默还原（分支 `claude/ae-done-takeover-fix`，本地回归与变异通过，待集成）。G03 脚本模型端到端发现：接替 DONE 子代理时回执报 recorded、写了 TAKEOVER.md，权威状态却没变。现在已关闭来源只追加 `superseded_by` 与一条 TakeoverRecord、终态保持，未关闭来源照旧 TAKEN_OVER；落账入口与回执两道落盘核对，未落盘报 `not_persisted`；预检、接管 run 去重统一读 takeover_by／superseded_by，第二次接替被拒；代理树与 list_agents 节点带 `replaced_by`。结构见[04-structure](04-structure.md#接替关系的唯一落账入口2026-09-28)，测试见[测试记录](../../../TESTS.md)。

2026-09-28（分支 `claude/9a-lark-and-capcfg`）：能力配置删除了 12 个运行时没有效果的字段。与子代理相关的有两个：
- `subagent_due_check_interval`：没有任何读取方。
- `subagent_min_evidence_for_done`：原先只被读进巡检阈值快照 `DueCheckSettings.min_evidence`，之后没有任何检查使用它。现在连同这个快照字段和 `due_check_settings` 里的透传一起删掉。

心跳停滞、运行超时、无进展熔断三个阈值不变；随包模板现在把它们写全了。用户配置里残留旧键只告警、不报错。见 04-structure“巡检阈值来源”和 TESTS 顶部本节。

2026-09-28：子代理同一调用同一失败的回合硬上限（分支 `claude/75-repeat-failure-halt-subagent`，基于主代理同名切片 `b81b6f938`/`40df0c86f`，本地回归与变异通过，未部署）。之前子代理只在授权阶段连续失败时收口，非授权类的同调用死循环会一直空转。现在复用主代理的 `identical_failure` 模块和同一阈值：命中后以 blocked＋`REPEATED_IDENTICAL_TOOL_FAILURE` 结束本轮，runner 落 BLOCKED；收口事实（工具、错误码、阶段、次数、参数名）经原 `tool_failure_halt` 账本、完成信封和 wake metadata 交给直属父级，唤醒摘要写明是“以相同参数反复调用”。同一次调用两种条件都满足时按授权阶段收口。fake LLM 子代理端到端、7 项变异和脚本模型端到端见 [测试记录](../../../TESTS.md)。

2026-09-27：显式包申请不再被语义路由提前GAP或局部授予后结清。保留原owner路径判断，含capability:引用的请求整条交原直属父级grant/deny；不从前缀推断权限，不改包refs持久化或首请求marker。5例旧4红1绿，修后相关111项通过，已串联原创建/申请/父级授予/孩子私有方法读取及重复裁决；真实模型采用待验，见[测试记录](../../../TESTS.md#c17显式包申请组件链2026-09-27)。

2026-09-27：G05暴露首次提示准备直接抛路径ENAMETOOLONG。现对单个资料候选exists/resolve的OSError沿原unresolved投影处理，保留其它引用及InterruptedError传播；新8例旧6红2绿、修后全绿，四文件65项通过，独立窄审无阻断。未改创建可见性或包授权，尚未安装；申请链及真实私有读取仍待收口，见[测试记录](../../../TESTS.md#c17子代理资料路径异常隔离2026-09-27)。

子代理可观测与授权失败即停（分支 `claude/subagent-observability`，2026-09-27，本地回归与变异通过，未部署）：真实使用中
4 个只读子代理读 owner home 外的工作树，list_files/read_file/search_text 全在授权阶段被 `PATH_OWNER_SCOPE_BLOCKED` 拦下，
各卡约 20 分钟，父代理只看到“最近成功调用工具: search_text”。三处修复，全部读结构化事实：
- 父级可见：代理树节点新增 `recent_tool_failure`（工具、错误码、失败阶段、同码连续次数、最近时间、`ongoing`），读取时从 owner
  权威 `runtime_events` 的 `tool_completed` 现算，不另存状态；`tool_completed` 载荷补 `failure_stage`/`handler_executed`。
  `last_progress_summary` 在最近一次调用失败时写“最近一次工具调用失败：<工具>（<错误码>，连续 N 次）”，失败不刷新 `last_progress_at`。
- 授权阶段即停：查明原重复失败机制对子代理同样生效，但默认只返工提示、每 15 次清段；显式硬门默认关，即使打开也收成
  unfinished→`PENDING` 并被立即重派、计数清零。现 `task_local` 子代理同一错误码在授权阶段连续失败达 `repeated_failure_halt_threshold`
  即以 blocked + `REPEATED_TOOL_AUTHORIZATION_FAILURE` 收口，runner 落 `BLOCKED`；finalize 从同一 archive 复算收口事实写进工具失败账本
  `halt`，完成信封 `tool_failure_halt` 经原生命周期事件交直属父级（原因码、工具、错误码、次数、参数名，不带参数值）。
- 创建前预检：`create_subagents`（根与递归）对显式输入路径用子代理运行时同一判定链检查，看不到就整批 `not_started` 拒绝
  （`SUBAGENT_INPUT_PATH_NOT_VISIBLE`），goal 正文路径不参与。handler 的墙外已授权根计算提升为 `path_access_policy.granted_external_work_roots`
  供两处共用。细节见 04-structure 同名节，证据见 TESTS 顶部本节。

参数减量第 1 批（分支 `claude/38-delete-dead-config`，2026-09-27）：`CreateRunParams` / `SubAgentManager.create_run` 去掉 `memory_retention_policy`、
`memory_delete_after_days`、`destroy_summary_required` 三个只写不读的参数，对应配置 `subagent_memory_*` 与
`subagent_destroy_summary_required` 一起删除；任务记录 `attributes.memory_scope` 升为 `subagent_memory_scope.v2`，只写 namespace 与
auto_promote_to_parent_memory。旧记录里残留的三个键没有任何读取方（recall 只按 scope_type/scope_key 取，进度展示只读 namespace），
也没有摘要、哈希或签名覆盖这份记录（决策选模的 `_child_fingerprint` 只在进程内比较，不落盘）。详见 04-structure “memory_scope 记录”。

协作状态更新按 case 串行（分支 `claude/collab-request-race`，2026-09-27）：请求与 case 的状态读改写改在按 case 的更新锁内，修掉“旧快照最后写入、
把已完成的请求写回 open”的丢失更新；原全仓分片偶发失败有了确定性复现与回归用例。细节见 04-structure 同名节。

2026-09-27：能力配置缓存读取仅接受原CapabilityConfig实例，避免占位mock的真值误开子入口准备；原文件加载、真实开关、prepared run及首请求授权合同保持。原74项创建失败已在19文件399项相关回归中通过，包含配置开/关、子入口和决策消费者；新结果不替代组合全仓或真实验收。见[回归记录](../../../TESTS.md#c16全仓回归修复2026-09-27验证中)。

能力包子入口（2026-09-27，本地已实现，组合验收中）：新建child可复用原首请求marker准备已显式授权且同代pin的包入口，
入口沿原RuntimeFacts进入业务请求，不新增辅助选包模型。旧无marker线程不回填，关闭配置不加载，方法仍按需读取。
原25文件570项通过；显式选模令Jev建议retained后入口资格被错误跳过的缺口已修，marker CAS独立领取包准备，pending advice只约束建议采用，相关十文件254项及独立11项/6组探针通过。claim提取早返回helper后，新冻结源码的三片35文件977项再次通过，Ruff与严格尺寸通过。
文档及最终组合由root负责；真实私有运行仍旧f6，原四子代理任务质量失败不回填。见[能力包合同](../../design/CAPABILITY_PACKS.md#主任务子代理和长任务)。

自学习 S1 改为自动确认（分支 `claude/skill-auto-summary`，2026-09-26，用户决定自学习不逐条审批）：`runner_result_service._skill_proposal_note`
在生成提案后立即对每条新提案调用 `SkillProposalService.confirm(..., actor="auto")`，走原来的全部复核（版本、草稿 hash、来源 Candidate、
目标不存在、解析、guard），回执记 `confirmed_by=auto`；被拒的提案保持待确认。工作日志改为 `skill_proposals=<新建数> skill_proposals_committed=<安装数>`，
异常仍只写 `skill_proposals_error=<类型>`，不影响结果交付。manager 仍只在 `enable_self_learning` 开启时注入服务。

子代理 lesson 结构化来源 `record_lesson`（2026-09-25，已合入 main `52e0190e1`；已端到端真实验收）：真实 TUI 发现子代理按提示自然回复、不出状态 JSON，`output.json` 的 `lessons` 永远为空，S1 提案无从触发。现在子代理可调用专属工具 `record_lesson`，填 title/when_to_use/procedure/applies_to 四个有界字段，写入本 run 的 `lessons.jsonl`。同 run 相同参数只记一次；每 run 最多 5 条、16 KiB，超限返回结构化拒绝；run/attempt/task 身份只取宿主上下文。结果收口读回账本，逐行复核后合并进 `lessons`，没有结构化输出也会记成带账本引用的 `subagent_lesson` 候选。候选失败只写工作日志（`memory_candidates_error=<类型>`），不再阻断结果交付。工具由注册表默认隐藏，只随子代理授权下发；Runner Contract 在有授权时多一条可选软引导。离线证据见 TESTS 顶部本节；真实验收未做。
取消的 run 也进账（2026-10-01，J15）：`subagents/cancellation.py` 取消收口时按结构化状态 `cancelled` 写一条 `lessons.jsonl` 的 run 状态行（`kind=run_status`，独立于经验行的条数与字节上限、同 run 同状态幂等），不再依赖模型文字判断；读回按 `kind` 字段分流，状态行不占经验条数。

自学习 S1（2026-09-24，本地分支 `claude/self-learning-skill-proposals`，待审）：`enable_self_learning` 开启时，runner 结果记录 lesson Candidate 之后，会把本批候选交给 owner Skill 提案服务生成待用户确认的提案；提案失败只写工作日志（`skill_proposals_error=<类型>`），不影响结果交付。默认关闭时不注入服务、不建目录。确认只走 `my-agent skills proposals confirm`，子代理链不安装 Skill。证据见 TESTS 顶部自学习 S1 节。

child overflow完整恢复已本地接入：原来源延迟至真实请求render/select，候选只改独立线程历史和第0注入，原CAS成功后同次生成。共享恢复器沿run token停止与摘要错误边界；首请求选模、权限和attempt不变。初次/手动与后台入口尚待接入；证据见TESTS及决策容量审计。

## 可选决策选模型（本地已验，未部署）

根/递归创建复用原准备与物化流程，整批 Jev 建议只在创建锁外等待，回来后重核权限、配置/连接、设置版本和持久复用。
2026-09-22 hybrid 首个合同切片改为仅在新 child 的 canonical thread 保存 `host_subagent_model_advice.v1/pending`。
原 prepared/refreeze/create 携带 typed 初始化材料，task attributes 不保存或恢复建议权威；现有线程不重新植入 pending。
显式模型优先，重复请求不重新建议；用户即使选中同一个 profile，也在原 thread 原子更新中把 pending 终结为 retained。
创建时容量和工具支持仍可未知；现已接真实首请求自动核对，只有原目录代次、工具探针、完整出站容量与线程 CAS 同时通过才采用。
最终选择必须由主代理派工流程自动完成：Jev 建议→宿主核验→采用或保留→执行；pending 不等待用户选择、确认或补步骤。
首次资格由新线程标记和原 attempt 一次领取；准备中崩溃不重放建议，发送前写入持久栅栏。旧 pending、进程重启或没有回复都不构成采用许可。
原幂等身份相同时，两个宿主自动名字可忽略展示序号；修复身份相同但幂等身份不同仍不能放宽名字比较。
本地联合验证与剩余窗口边界见 [07 交接](../../tasks/DECISION_MODEL_P2_SUBAGENT_HANDOFF.md)。
隔离 Gateway TUI 又完成六轮普通中文派工，10 个 child 均 DONE：三候选自然建议官方 MiniMax-M3 与
仅限 OpenCode DeepSeek 候选时的合法建议，各被宿主自动采用并完成实际工具后续轮；Jev 超时/冷却时
自动保留官方 M2.7。两条异模成功只是各自条件的小样本，DeepSeek 不代表三候选默认偏好。
P2-B 已于 2026-09-25 复核勾选：窗口/Compact/故障组合已由第 12、13 项验收。当时媒体一律保留原模型；2026-10-02 的 J11 已在本地补齐按结构化附件事实和 `input_modalities` 过滤、采用或结构化保留。真实带图 child 自动换模仍未验证。以下为当时记录：
其中一次 `selection_changed` 的精确提交分支没有当时观测，不能补推原因；提交阶段现已按失败点记录结构化原因码
（目录/父线程锁占用、目录代次或设置变化、task/权限变化、期限、child 线程冲突，本地分支 `claude/decision-child-commit-reasons`，待审），
同类情况再现即可直接归因。详见
[子代理真实交接](../../tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md)。

## 创建准备与只读上下文投影（四个有界切片，本地已验）

`base_service.prepare_run` 只读准备原 `SubAgentTask`；`create_run(prepared=...)` 在原创建锁内复核输入、
父任务代次、身份和权限，然后提交同一个对象。准备不建立线程或任务记录，重复提交在首次写入前拒绝。
runner 将事实收集与纯上下文投影分开；已存在任务和未物化准备对象共用 `project_execution_context`，
重复投影不读取 manager 或时间，也不经返回值改写源任务。

第二切片已让准备请求消费原 workspace adapter 的只读路径投影；默认工作区与显式 task root 均复用正式
`ensure` 的身份、目录及写根算法，正式保存仍是唯一物化入口。预览不改准备任务、不写空目录或账本，也不生成日账事件。
路径字段可计算不代表文件已存在；`unresolved_fields` 只报告缺字段，空值不能证明完整首请求。

第三切片已将同一准备对象接入根及递归创建。锁外建议返回后沿原规范化和权限检查重新冻结，提交前仍先查原幂等复用；
同批兄弟更新父代次时保留原 run ID、创建时间、父/root/thread/session 身份，不创建第二对象，已发布任务拒绝重新冻结。
建议指纹覆盖真实准备身份与有效权限，当前目录、用户候选范围、配置、期限和取消仍在原入口复核。

第四切片复用原 selected profile、task overlay 和后端构造，读取逐候选最终配置窗口及原发送合同可证明的输出 cap；
只读 overlay 投影不应用进程日志设置，worker 仍在原入口唯一应用。接纳 pending 前重核最终配置版本；
未知 cap 不当作零，工具支持未知不能因窗口通过而进入自动改选。公开缺口只作本批建议材料，不持久化为容量证明。

完整容量检查放在真实 child 首轮准备后：复用原工具展示、历史、系统提示及 Anthropic/Chat 实际 payload；使用原 token 估算与真实输出 cap 预检。创建前不再把粗估冒充证明；供应商实测与未支持协议仍须单列。
原精确 child thread 的已有记录补齐现已改在 `ThreadStore.update_atomic` 读取最新版本，防止并发模型选择或 Compact 后被物化入口的旧快照覆盖；`test_conversation_store.py` 用读后并发更新复现并验证，52 项通过。它是后续首请求原子采用的必需前置。
本地 427 项联合定向测试通过，含 root/child/grandchild 同对象提交、父代次更新、并发幂等复用、权限变化、部分取消，
以及前两片的默认/显式工作区、无落盘投影和原工作区状态回归。模型采纳绑定测试显式使用测试容量事实，未冒充真实容量；
新增配置投影与 worker 等价、无日志/文件副作用、配置变更撤销、原协议请求体 cap 及 OAuth 未知上限回归。
设计、证据和下一切片见 [容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
授权与旧工作片收口交错的本地候选已修正三个来源：完成通知保留原 attempt 的 `BLOCKED` 历史，
当前会话关联复读 canonical 后用 CAS 投影；session 退出后按同 run 与原 attempt 核对接续；授权 wake
只窄写自身账本，不覆盖新 session。三个旧红灯分别验证，另覆盖 grant/stop 交错、新 attempt 与重放。
真实 RuntimeDB/dispatcher 合同从旧 AgentRun/attempt 都 `done` 开始，只替换线程入口，确认第二轮
实际登记并激活且重复交付不多开；父关联 `interrupted/cancelled` 仍禁止接续。原 TUI217 保留失败证据，
本候选不等于同版真实验收通过；细节见 [runbook](SUBAGENT_RUNBOOK.md#capability-阻塞与续跑)。

第 7 步父终态通知已在本地集成，将实际逻辑归入 `RunnerCompletionNotifier`：只持任务关联、WakeStore、
读取父任务和保存错误四项依赖，完成／受控取消共用原投递路径；结果服务和外部停止端负责装配。
保留 exact attempt、直属父级、文件模式及内部监督者信号语义；阶段提醒和能力申请入口不扩改。
已与恢复扫描接口片组合，原 sweep 和恢复测试均绑定同一通知器；旧接口调用删除；组合包 a067baddd 已双机部署，实际验收进行中，详见 STATUS。

第 7 步结果提交依赖已在本地候选收窄：初次提交只接原 RuntimeDB、canonical task、结构化结果、
保存回调和绑定本轮身份的交付回调；WAL 原语只接 save，运行结算与诊断只接原 RuntimeDB。
结果服务装配 trace→父通知，仍在 WAL→运行账之后执行；不新增状态副本或兼容转发。
恢复扫描已集成为显式 repo/load/save/list/notify，原 sweep 绑定窄父通知器；恢复模块不再持有完整 manager。
本片不新增扫描器或业务重跑入口，不声称第 7 步完成；新版部署和原生验收分列 STATUS。

第 7 步依赖复核：结果准入的参数已在本地从完整 manager 收窄为原 RuntimeDB；
换代、同轮重入、终态冲突诊断和文件模式的既有合同保持，两个直接受影响测试文件通过。
此接口收窄尚未发布或安装，不由测试机 TUI195—197 的旧包实测覆盖；完整状态见唯一 Goal 台账。

Shell 网关执行的取消查询现引用公共 `common/cancellation.py`，原令牌和退出语义不变；子树持久取消仍在本模块原合同中。

## 第 7 步展示与完成交接投影小片（本地开发）

配对通知半写恢复已独立本地实现：完成 wake 显式传 `retain_handled=True`，由会话原 dedupe 锁裁决，
不再以前置查询跳过未完成观察安装。原 pending-closeout WAL 继续负责重试，RuntimeDB／标记／清 WAL 顺序不变；
通用 Goal 仍在上一代 handled 后新发。原两个重复通知红灯已绿，故障矩阵与边界见[发布交接](../../tasks/HANDOFF_STEP7_WAKE_PUBLICATION.md)。
此片尚未发布、未覆盖本版本真实 TUI，也不扩大成通用自动恢复服务。

已把子代理当前活动的中文标签映射移到 `runner_display_projection.py`，只读取已裁决的状态码和失败类型。
`runner_result_state.py` 仍在原结果写回时机设置 `current_step/current_tool`，不让模型正文或标签反向决定任务状态。
`RUNNING` 继续保留当前活动，正常让出、能力等待、失败和取消的原裁决与持久收口顺序不变。
同片还修复终态冲突诊断的依赖引用：结果服务原从自身读取不存在的 `runtime_db`，导致拒绝结果后
没有写出约定的 `closeout_blocked` 事件；现在读取 manager 的原 RuntimeDB，不改变拒绝裁决。
新增回归先在旧代码复现零事件，再验精确 attempt／run 的原事件落账；对应 42 项状态／续跑／服务窗口
定向回归通过，另 91 项执行器恢复、结果持久化、直属父交接和工具失败账本回归通过、1 项既有跳过。
这是源码小片，尚未发布或计入真实 TUI 验收。

完成正文截断、已登记产物引用和版本化交接信封也已从通知模块移到
`runner_completion_payload.py`。根通知、递归父级安全点和直属父等待读取同一只读投影；
`runner_completion_wake.py` 继续负责原会话状态更新、通知去重、wake 原子发布和错误回执。
旧函数定义和旧导出已删除，没有兼容转发；既有 53 项直属父／窗口／结果状态回归通过。
持久收口的事务顺序未改；新版真实 TUI 和发布仍待完成。

第三小片把接管、废弃轮、换代和 RuntimeDB 终态冲突准入移到
`runner_result_admission.py`。结果服务在读出 canonical task 后立即调用准入，拒绝时仍返回原快速结果；
冲突诊断仍写原运行账，允许同一 exact current attempt 的一致终态重入。
旧类方法和同文件私有判据已删除，原事故与派工恢复两个测试文件共 60 项结束（1 项既有跳过），
未改变结果文件、WAL 或父级 wake 的提交顺序。

第四小片把初次结果的持久收口与父通知编排移到 `services/runner_result_commit.py`，
结果服务只在业务结果和任务投影保存后调用它。原 WAL／RuntimeDB／通知原语及恢复扫描保持唯一；
首次通知后写 `delivered` 标记若失败，现在保留 pending WAL，恢复时依据原回执去重并清账。
故障注入覆盖标记失败后的持久事实、单次 wake 和恢复，不把本地测试冒充新版真实 TUI。

## 创建、换轮与停止协调（已发布）

创建、attempt 准备/放弃、授权后排队及失联重排复用原 owner 创建锁；嵌套只复用本进程本线程持有的同一路径锁。
旧 abandon 通过 canonical mutation 合并废弃轮编号，不用旧快照抹掉新活动轮或并发字段；授权后排队先复读控制终态。
插话按 admission→creation 的顺序预留回合与写邮箱，释放 creation 后才启动和探测宿主。
managed 启动已贯穿原 pending ID，覆盖后台/CLI/顺序与并行 runner/插话；原 DB 事务拒绝失效身份，旧启动回执仅条件更新原记录。
worker 激活后才发布 session，首次未确认持久化不得进入执行；旧心跳不能覆盖新轮。
固定原子树及终态资源清理已进入源码，整树在创建锁内关闭权限、冻结清单，再到锁外清理。
独立 runner 沿原心跳转交精确 attempt 中断，宿主 PID 只观察退出，避免误杀新接续。
开发回归范围见 [TESTS](../../../TESTS.md)。回执重放已限定最新 pending 和准确候选；无数据库启动已补原文件接纳与一次性消费，
旧快照保护、二次停止撤销及正常结果回收均接入原保存边界，累计源码全仓已通过；启动标记的实际写入归原 lifecycle 服务，CLI 不越层导入；
已同版部署双机，实际 TUI 140 验三个并行孩子、141 验交互 PTY、142 验真实孙代理；原进程退出、文件稳定和历史保留分别核对。
历史 TUI 137 原失败保留；无数据库实际 TUI、Windows 及其它未命中旁支不据这些结果宣称通过。

## 正常让出与当前错误分离

runner 回写不再把 `ok=False` 一律视作执行失败。显式回合原因与既有映射的状态、`ok` 一致，且没有
显式失败时，正常 `interrupted/PENDING` 和 `completed/DONE` 清除当前错误投影；历史记录保留。
原因缺失、未知、状态冲突、能力阻塞与取消仍保留失败诊断，调度和父级唤醒逻辑未改。
237 项定向通过、1 项已有 xfail；官方 MiniMax-M2.7 的真实 TUI 已核对统筹两次让出、逐项接收孙代理结果
后完成，以及停止后进程树退出、采样文件持续静止。另一路扁平派工和报告时间与 CSV 不符仍记为未通过，
不能用状态链路通过代替完整任务验收，详见 [STATUS](../../../STATUS.md)。

## 宿主终止共用进程树事实

历史发布版的取消与失联回收曾统一使用工具层后代快照、出生标识及退出核对。
当前取消源码改为精确归属后台/PTY 清理及 worker 内协作中断；宿主可能派生新任务，不能仅凭出生标识强杀整树。
独占检查仍由调用方执行；共享宿主、Gateway 和身份未知时不得按 PID 杀整组。
263 项定向通过、1 项跳过，本机真实 TUI 验证取消后文件静止、跨会话隔离及压缩重连后的原程序续采。
OS 强制终止仍可能缺已完成的 native 信封，取消回执不证明历史完整；旧失败样本保留，不伪造工具结果。

## 收口接口收窄

无操作结果只接收状态和原因，权威读取失败单独构造可重试结果；删除可任意覆盖结果的宽泛参数。
既有 WAL、exact attempt、终态冲突与父级通知顺序保持。生命周期和 owner 唤醒回归已覆盖原结果字段。

## 进度部分更新

真实慢模型任务暴露 Todo 只补备注却被重置为 pending，覆盖清单也有同源问题。
已区分补丁规范化与新建默认值；普通/更正更新、空状态、新项默认值、完成保护及工具展示投影分别回归。
旧账不推测回填；真实 TUI 恢复继续验收，不把计划计数当任务完成裁决。

## 状态与结果读取

模型查询改用同一授权快照的紧凑视图；界面私有恢复路径不再进入正文或模型工具归档。
交付索引只提供实际产物或已存在的终态报告，慢代理、没有产物与未知状态均不自动判失败。
查询复用统一分工说明；不再要求父级先结束回合。完成后查询使用规范会话 ID，主请求根沿 parent_id 查树。
153 项定向与双真实 TUI 已通过：两个孩子的文件，以及协调者/两名孙代理的文件或实际终态报告可读。

## 分工说明去冲突

`current_turn_run_state` 保留运行与故障事实，删除其另发的父级动作建议，避免盖过创建回执的独立工作指导。
协调者与根共用分工纪律，只加载自身冻结角色正文及角色索引；不再把所有角色行为混成当前身份。
删除全部外包、终态后也不能接手与亲手重写下级成果等旧约束；真实权限、自然等待及结果交接链不变。
定向回归 312 项中 308 项成功、4 项已有 xfail，覆盖混合终态、自定义角色冻结和主子等待边界。
新包三路真实 TUI 与同会话追加均完成，根独立写模块、依赖等待自动接续、协调者与两名孙代理均有实际证据。
模型仍出现扁平派工、转交父级预留入口和反复查状态；这些不因最终完成而算已修，详见 STATUS。

## 父子并行与阶段提醒

已移除创建后的隐式让出、成功通知的全树等待；自然让出的父级按精确孩子记录等待，
先完成的先交接，重复结果不会反复唤醒。父级忙时在原安全点接收，根和递归父级都不另开执行器。
长等待诊断复用心跳、模型活动账和工具阶段；持续慢流不因总时长被判失败，静默提醒不自动重跑。
四个真实 TUI 已完成 CLI 工具与连续采样任务，出现逐项交接、父级独立工作和低阈值提醒后自然完成。
测试覆盖文件已撤销；孙级真实链路、重复实现与故障组合仍开放，不能把这些成功当作所有边界通过。
配置与验收见 [并行执行](../../design/SUBAGENT_PARALLEL_EXECUTION.md)。

## 当前合同

自然最终回复也从 exact run 工具产物账本收集交接引用，不再依赖模型输出特定 JSON。
输出声明只按实际 cwd 解析，旧内部 output 重定位、声明增权和收口搬运已删除。
真实分层基线已跑出统筹和两个孙代理；新包复验与分工质量分别记录，不混称全面通过。

每个子代理持有独立运行身份、会话与 attempt，保留 parent/root 谱系。创建、状态查询、插话、取消和能力申请走结构化工具；主代理与子代理都不能通过模型正文冒充另一个 run。

## 已实现

- 创建时继承用户家目录和权限上界；显式模型覆盖与主代理默认继承分开。
- 子代理详情展示初始需求、正文、思考、真实工具、历史与输入队列，控制命令作用于精确目标。
- runner 结果先保留恢复事实，再提交终态与父级通知；重复恢复按 attempt 去重。
- 已终态的一致结果可重入，冲突结果和旧 attempt 不覆盖当前运行。
- JSONL 的 Unicode 分隔符读取问题已修，公开展示分页不代替模型历史。
- exact attempt 执行器进入/退出记录已接入；无结果退出按失败通知，未知副作用保留封存并显示阻塞。
- 未落盘收口按未消费身份分页轮转，已消费记录不因超过历史窗口重复出现，失败项不阻塞后续页。

## 当前开放问题

两项修复已通过本地定向回归；真实 TUI 多子代理和故障注入仍需复验。旧版活进程中未登记身份的未知线程不能
凭空判死；长期存储同时不可写时仍需明确告警。详见 [STATUS](../../../STATUS.md)。

## 回归要求

验证创建、启动、工作中插话、停止、正常完成、异常退出、写回失败、恢复重入及父级唤醒；包含超过扫描窗口的积压。不能只断言一个最终 DONE 字段，必须核对真实工具/结果/消息身份。详见 [结构](04-structure.md) 和 [运维手册](SUBAGENT_RUNBOOK.md)。

- 首次请求合同续片：新增 thread 的宿主 pending 才初始化首次发送资格，原 runner 激活后捕获真实 child 请求输入；
  provider I/O 前同一 thread 原子写发送意图，旧 pending 不补资格，取消/写入失败不发请求。原模型依赖 scope 与
  Anthropic/Chat 同源组包已验证实际出站等价；目录/设置撤销、工具或容量未知自动保留。M2.7、M3、DeepSeek 的 fake provider 首轮与后续工具轮自动采用已验，隔离真实服务验收仍待主线执行。

## 2026-09-25 能力包引用接线（本地组件阶段）

子代理创建与能力授予复用 allowed_skills／skill_snapshot_refs，把 capability:<id> 固定到整包摘要和激活代次。
层级继承只取父任务 canonical attrs 和 grants，忽略 spec 自报引用；后授予能力不再被旧运行参数中的初始列表再次裁掉。
本地定向测试已覆盖包内读取隔离、父子权限、伪造孙任务 refs、后授予接续；真实多 TUI 尚未开始。
详见[能力包 Goal](../../tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

## 被接替的子代理在运行账里补终态（2026-09-30，分支 `claude/38-agent-run-closeout-status`，基于 main `10041de02`）

- **起因**：G03 验收第二条观察，BLOCKED 后被接替的子代理 runtime.db agent_run 永远停在 created。
- **改动**：新增 `runtime_db/run_takeover.py`，`services/base.record_takeover` 接替落账后调用；只改运行账，不动 `takeover/record.py`。
- 测试与变异见 TESTS.md 同名节。

## 子代理管理器回答“是否已被接替”（2026-09-30，分支 `claude/38-recover-child-import-boundary`，基于 step16v `d9abcb5ec`）

- **起因**：O1 的 `/recover` 子代理分支需要知道被中断的子代理有没有被接替，但网关按分层边界不能导入 `subagents`。
- **改动**：`SubAgentManager.taken_over_successor(run_id)` 一行委托到模块级 `_taken_over_successor`：只认 status=TAKEN_OVER 且
  `task_replacement_successor` 判为 taken_over；记录缺失或读坏返回 None，只记 superseded_by 的不算接管。只读，不改记录。
- 测试与变异见 TESTS.md“/recover 子代理分支的分层边界修正”节。

## 执行器退出后父级唤醒回执里的结构化接替提示（C4，2026-10-02，分支 `claude/38-c4-takeover-hint`，基于 step16z `e0a6d53af`）

- **起因**：O1 复测，被 SIGKILL 的子代理只以 BLOCKED 通知父级，父级 4 次唤醒都没有声明 `replacement_for_run_ids`。
- **改动**：开关 `subagent_takeover_hint_enabled`（默认关）打开时，执行器退出收口附 `takeover_hint`（哪个 run、退出原因码、是否有未确认效果、
  怎么用 `replacement_for_run_ids` 声明接替），经完成合同交给所有父级消费方；只是提示，不派工、不改状态。
- **真实模型**：MiniMax-M2.7 一次有效运行，接替声明未命中，模型按提示先核对、发现文件已写好后结束。见设计台账 C4 节，测试与变异见 TESTS.md 同名节。

## 执行器退出且效果未知时保留宿主给的失败类型（2026-10-02，分支 `claude/38-executor-failure-type`，基于 step16z `00ec92b77`）

- **起因**：C4 真实核对发现，`recover_exited_runner` 给的 `executor_effects_unknown` 不在 `FailureType` 枚举里，被结果状态改写成
  可自动重跑族里的通用 `runner_error`，父级唤醒分不出“效果未知、要先核对”。
- **改动**：枚举正式登记 `EXECUTOR_EFFECTS_UNKNOWN`（不进可自动重跑族），`recover_exited_runner` 改用枚举值。测试与变异见 TESTS.md 同名节。

## 子代理回合被停机准入拒绝时记准确的停止原因（sol2 复审 J17 栅栏，2026-10-02，分支 `claude/38-fence-callers`，基于 step16z `00bcf7d45`）

- **起因**：栅栏关门后，子代理回合的新模型调用被拒。`_subagent_run_failure_type` 不认识这个异常，兜底成可自动重跑的 `runner_error`，并写进恢复快照的 error_code。
- **改动**：
  - 枚举登记 `MODEL_CALL_ADMISSION_CLOSED`（不进可自动重跑族）；
  - 失败分类最先认它，含显式原因链里的；
  - 结果和快照都写它，界面标签“宿主停机中断”。
  - 设计见台账“停机准入拒绝的调用方收尾”节，测试与变异见 TESTS.md 同名节。

## 停机后不再从两条自动入口续派子代理（sol2/75 复审 38d7c8615，2026-10-02，分支 `claude/38-audit-redispatch`，基于 step16z `c6f28b150`）

- **起因**：Audit 来源被结果归并改回 PENDING 后，session 收尾在停机进程里续派；嵌套子代理收尾时，先删父级等待标记，再在停机进程里启动父级。两者都绕过了“停机后不自动重跑”，新 attempt 会被拒、白占尝试次数。
- **改动**：两处都只读 `model_call_admission_closure()`：
  - 自动派发入口 `auto_start_tasks` 关门后返回 `blocked/host_shutdown`；
  - 等待调和关门后不释放父级等待标记。
  - 结果归并保留来源的停机原因；恢复提示改成和实际一致。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 自然停机带走的子代理重启后按“宿主停机中断”收尾（step17c 预演观察 1，2026-10-02，分支 `claude/38-shutdown-label-v2`，基于 `5a56714dc`）

- **起因**：自然停机时网关进程里的子代理随进程消失，重启后被收尾成 `runner_error`，看不出原因是停机。
- **改动**：
  - Gateway 关门结清后，给本进程在跑的执行器在各自 attempt 上打 `executor.host_shutdown` 记号，个数写进收尾载荷 `host_shutdown_executors`；
  - 重启收尾看到记号，记 `host_shutdown_interrupted`（不自动重跑），有未确认工具效果时仍先核对。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 子代理被宿主停机打断时界面显示“宿主停机中断”（2026-10-02，分支 `claude/9b-shutdown-label`，基于 step17e `2e5a36af0`）

- `runner_display_projection` 新增 `runner_failure_label`：失败类型专属标签的唯一出口（宿主停机中断、额度不足等），`runner_display_label` 复用它。
- Gateway 名册行带出 `failure_label`，TUI 名册、子代理页头部与终态活动文字都用它；IM `/status` 的“异常”按同一权威细分。设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 已预留未启动的子代理：停机不启动、重启照样拉起（I5，2026-10-02，分支 `claude/9b-runner-admission`，基于 step17e `20125c9d2`）

- 根修：宿主死在“已预留、未激活”窗口里留下冻住的启动记录，重启后被 `existing_runner_launch` 原样复用、永久卡住；现在过期记录不复用并先收掉（受管标 reclaimed 沿用同一 pending attempt，文件撤销旧预留重新预留），过期判定的唯一权威挪到 `process_control.background_start_record_stale`。
- 计数：`orphans_revived` 只算真派出去的，复用现存启动另记 `orphan_launches_reused` / `launch_reused`。
- I5：`worker._run_subagent_worker` 入口读准入关门，顺序与并行批次都不启动、不写 FAILED、保留预留。设计见台账两节，测试与变异见 TESTS.md 同名节。
