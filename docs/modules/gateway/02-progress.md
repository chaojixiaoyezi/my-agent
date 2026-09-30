# Gateway 维护状态

/model 五项菜单与其他用户的初始模型（分支 `claude/3a-chatgpt-browser-login`，2026-09-30）：`/client/models` 不改路由，新操作
`add_models` / `set_initial` 与带 `connection` 的 `discover` 都经 `execute_model_profile_operation`。`model_profile_service.render_model_choices`
的「默认」行按 `default_source=admin_initial` 标「管理员指定的初始模型」，空列表提示改指「默认模型与共享」。
设计见 `docs/design/TUI_MODEL_PROFILES.md`「菜单结构」与 `docs/design/SHARED_MODEL_CATALOG.md`「其他用户的初始模型」。

唤醒毒丸第 3 步 C6（step16m，3a）：后台 supervisor 停机时先给本进程在途的唤醒尝试打停机标记
（`wake_attempt_tracking.mark_inflight_attempts_stopping`，写失败逐条吞掉），再关执行池；死进程那一片认领过的补充消息按尝试账里
持久化的回合号退回（`wake_domain_closeout.settle_abandoned_turn`）；结案顺序改为写记录 → 删 pending → 移坏账 → 删尝试账。

唤醒毒丸第 4 步复审跟进（step16m，3a）：`/wakes` 的领域终态改用 C4 的 `wake_domain_status` / `wake_domain_terminal`（唯一判定），
删掉本地版本，只在这里把裸状态拼成提示；重放来源的结案记录还原不成同 ID 信封时，重放与预览都返回 `WAKE_REPLAY_SOURCE_UNREADABLE`，
不再抛异常；归档清理须连同同键去重回执一起删（WAKE_POISON_PILL 第 8 节）。

出站协议合同的错误文案（分支 `claude/3a-wire-contract`，2026-09-30）：
- `request_errors.gateway_client_error_message` 新增 `PROVIDER_REQUEST_SHAPE_INVALID`：后端出口在发送前查出消息结构违规，
  请求没有发出、本轮停止，文案请用户反馈运行诊断。错误本身与规则见 `docs/design/PROVIDER_WIRE_CONTRACT.md`。

维护回收的错误码与缓存不可读时的行为（分支 `my-agent/self-dev-2-vcache`，2026-09-29）：
- **第五轮补充（2026-09-29）**：回收的错误码此前抓不到最可能出的错——缓存文件读不出内容时
  `_load` 静默返回空、`retain_matching` 把异常吞进 `last_write_error`，维护状态里的错误字段仍是空串
  （探针 V5）。现在 `_load` 对坏 JSON 记 `last_read_error`，`reclaim_text_cache_orphans` 把读/写错误
  一并带出来，`_reclaim_text_vector_cache_orphans` 直接透传它的 `(回收数, 错误说明)`。
  回归见 `test_v5_reclaim_reports_error_when_cache_corrupt`；变异 MY11/MY12 必须被杀掉。
- `_reclaim_text_vector_cache_orphans` 现在返回 `(回收数, 错误码)`。建缓存/读记忆失败同样返回 0，
  但与"确实没有孤儿"分得开——错误码写进维护状态 `text_vector_cache_reclaim_error`。
  此前只有 `text_vector_cache_reclaimed: 0`，两种情况看起来一样，又是一条假的结构化事实。
- 正文哈希缓存的"构造宽松、写入严格"见 memory 模块 04-structure。

owner 维护回收正文哈希缓存孤儿：改为 canonical 路径 + 不依赖 embedder（分支 `my-agent/self-dev-2-vcache`，2026-09-29）：
- `user_space/owner_maintenance._reclaim_text_vector_cache_orphans` 此前手拼 `owner_home/memory/memory.jsonl`（**生产实际在
  `home.owner_memory_long_term_jsonl`**，文件不存在 → 直接返回 0），且新建的 `JsonlMemory` 没有 embedder
  → `_text_vector_cache()` 为 None → 回收数永远是 0。后果是 `maintenance.json` 每天写
  `text_vector_cache_reclaimed: 0`，**看起来像"跑过、没有孤儿"，实为假的结构化事实**。
- 改用 canonical 路径；回收改走 `JsonlMemory.reclaim_text_cache_orphans()`，
  它按缓存键里的**正文哈希**（`DataTextVectorCache.retain_content_hashes`）比对 active 记录的
  `index_text` 哈希，不需要 embedder、不联网。
- 回归见 `test_memory_vector_cache.py::test_maintenance_reclaims_orphan_on_real_layout`；
  变异 MY1（回收函数开头直接 `return 0`）必须被杀掉——见 `scripts/mutate_text_vector_cache.py`。
global_index 只追加索引自动压缩（分支 `my-agent/self-dev-2-index`，2026-09-29，基于 `64f7ee64e`）：
- `user_space/home_index_compact.py`：四份 `global_index/*.jsonl` 在维护 tick 里按 key 内部压缩
  （纯投影，**不读任何权威源**；手动 `home-index-rebuild --apply` 仍是权威修复工具，不变）。
- **两遍流式**：第一遍只按 **LF** 切行、记下每个 key 最后一次出现的行号；第二遍按原顺序把那些行
  流式写进临时文件，**原字节照抄、不重新序列化**。读取方 `_latest_unique_refs` 是「先 reversed、
  再遇首次出现即取」，所以压缩后读取结果按构造逐条相同。
- **每个文件用自己的 key_fields**（owners 只有 owner_id；tasks/runs/agents 各自带自己的 id）。
  统一成一套 fields 会让 runs 的 task_id 版本被当成两个 key，出现"旧状态复活"。
- 峰值内存实测：**145.4 MB 文件 → 峰值增量 22.9 MB、0.7 秒**（第一版把整份前缀解析成 dict，
  同一文件是 952 MB、约 6.5 倍）。旧文档里"峰值 42 MB"是在小文件上量的数，已在 04-structure 更正。

38 复审跟进（2026-09-29）：
- **必须改：文件身份核对原本在锁外**，`_still_same_file` 通过之后才拿锁，而 rebuild 恰好能落在
  「核对通过 → 拿到锁」之间，把陈旧前缀盖到 rebuild 的新内容上（实测读回 `['B','A']`，
  rebuild 写的 `REBUILT` 丢了）。现在拿到锁之后**再核一次**，不一致就 `identity_changed` 并丢 tmp。
  锁外那次保留——它挡的是扫描期间换文件，两道作用不同。
- **失败原因单列**：`_compact_global_indexes` 原先只记 `compacted=True` 的结果，
  `io_error` / `identity_changed` 被静默丢掉，`maintenance.json` 分不清「没到期」和「压失败」。
  现在返回 `(成功摘要, 失败摘要)`，失败进 **`indexes_compact_failed`**；
  「没动手」的四个原因（`below_min_bytes` / `below_growth_ratio` / `in_cooldown` / `missing`）不算失败。
- **键定义单一来源**：`home_indexes.INDEX_KEY_FIELDS_BY_FILE` 是唯一权威，写入侧与压缩侧都从它派生。
- 触发用**零扫描判据**：当前大小 ≥ `max(64 MB, 2 × 上次压缩后大小)`；冷却 `COMPACT_COOLDOWN_SECONDS = 6 小时`。
  上次压缩结果**持久化到索引同目录的 `compact_state.json`**，否则 Gateway 每次重启后第一次 tick
  都会不受冷却限制地压一遍。
- 并发按长度切：锁内记 `(st_ino, prefix_len)` → 放锁压缩前缀 → 重拿锁核对 inode 与长度，
  把前缀之后新追加的字节接上，再原子替换。坏行计数上报在结果与维护状态 `indexes_compacted` 里，不静默。

停机时等 observe 后台执行器落账（分支 `claude/9a-jev-observe-async`，2026-09-28，dsh-ae 复审跟进）：
- `cli/gateway_process._cancel_active_decisions` 在 `cancel_active_decisions_for_shutdown` 之后，经 `wait_nonblocking_idle` 最多等 `_NONBLOCKING_DRAIN_SECONDS`（2 秒）。
- 取消与等待各在自己的 try 里（ae 复核建议）：取消的 try 与原来一致；执行器模块的导入与等待放在第二个 try，出错只记异常类型事件 `gateway_decision_drain_failed`，不会跳过取消，也不会被误记成 `gateway_decision_cancel_failed`。回归见 `test_gateway_decision_shutdown_cancel.py::test_gateway_cleanup_still_cancels_when_the_observe_drain_fails`（等待抛异常、执行器模块导入失败两组）。
- 被取消的调用不再等网络（排队中的不发送，在途的立即停止等待），通常几毫秒就写完；这一步只是防止丢掉最后一行结果和一条用量，等不到也照常收尾。
- 回归见 `test_decision_observe_nonblocking.py::test_gateway_shutdown_waits_briefly_for_background_rows`（写行被放慢时，返回前结果行已落盘）。
- 另核实：请求收口（terminalize）读的是盘上文件，Gateway 没有用内存副本整体回写请求记录的路径。`record_capability_presentation_observation` 与 `complete_deferred` 里同步更新内存副本，只是让同一请求里之后读内存的代码看到同一事实，注释已改正。

选模型观察转后台后的补记（分支 `claude/9a-jev-observe-async`，2026-09-28）：
- 决策设置 `observe_nonblocking_enabled` 打开时，选模型的 observe 当场返回 deferred；请求记录的观察标记先记 `deferred`，主模型不再等 Jev。
- 后台完成后，由 `GatewayModelObservation._complete_deferred` 经新增的 `GatewayModelObservationWriter.complete_deferred` 补记建议编号：
  - 只替换同一观察（op/claim 相同）的 started/deferred 标记，`adopted` 恒为 false；
  - 写入走原 active-turn 事务，回合已结束就不回写。
- 回归见 `test_decision_observe_nonblocking.py` 的三组选模型用例。
owner 维护顺带回收正文哈希缓存的孤儿键（分支 `my-agent/self-dev-2-vcache`，2026-09-28）：
- `user_space/owner_maintenance.run_owner_retention_if_due` 在既有维护事务（默认 24 小时一次、已在 `locked_json_path` 内）里
  多调一次 `_reclaim_text_vector_cache_orphans`，回收 `memory_text_vectors.json` 中不属于任何 active 记忆的键。
- 动因：这些孤儿键（换模型、迁移、绕过写入路径改正文留下）原先只有手动 `home-index`/`index_all` 才会清，
  挂进维护循环后不跑手动命令也能自动收口。回收只读 active 记忆算保留集合，不加载嵌入模型、不联网。
- 失败只返回 0 并照常写维护状态，绝不影响 retention 结果；新增字段 `text_vector_cache_reclaimed`。
- 回归见 `test_memory_vector_cache.py::test_index_all_reclaims_even_without_local_store`（无 LocalStore 时也必须回收）。

`/endtask` 结束卡在等待中的定时会话任务（分支 `claude/be-end-session-task`，2026-09-29）：
- 来源：2026-09-28 两个会话的定时运行里 `run_command` 结果未知，工作片停下而会话任务仍 active，定时运行停在 waiting 永不结算，同一 job 的到期派发被一直跳过。
- 新增会话控制 `/endtask`，由 `control_service` 按 kind 分派到 `gateway_parts/end_task_control.py`，TUI 与飞书共用；仅本机管理员可用。无参数列候选、只给任务 ID 只读预览、带 confirm 才写。
- 放行只认结构化事实：定时账本里是 waiting、会话任务链接是 active、运行库整棵执行树没有未结束的 attempt（运行库读不到按无法确认拒绝）。确认只写两处：`tasks.update_status(cancelled, expected_status=active)`，再对同一任务调 `reconcile_waiting_run`。
- 回归见 `test_end_task_control.py`。根因修复（只在确有后续工作时才进 waiting）另排，见 DESIGN_LEDGER。
- 9a 复审跟进：6 个 `END_TASK_*` 拒绝码登记进 `ERROR_CONTRACTS`（全仓守卫 `test_recovery_code_policy` 转绿，字典里的码另有模块测试钉住）；预览和确认结果固定写明“结束任务不会停止它启动的后台命令；这些命令结束后的通知会落到已取消的任务上”。
- 后续工作事实码（分支 `claude/be-endtask-follow-up-facts`，基于 `e850ceb04`）：列表每行和预览都附“后续工作”，直接调 owner 的 `scheduler_service.follow_up`（与定时执行收口同一个判定），只显示事实码与读不出的“项目:错误码”，没有写“无”，判定未注入写“判定不可用”；`GRACE_BOUND_FACTS` 里的码后面加“（宽限期内才算）”。

定时执行 waiting 死锁（分支 `claude/75-scheduler-waiting-deadlock`，2026-09-29）：一轮定时执行结束后任务仍是 active 时，
`_finish_scheduler_wake_claim` 改调 `scheduler/active_run_closeout.close_active_run`：`conversation/task_follow_up` 判定有结构化后续工作才进 waiting；
没有就把任务 CAS 成 blocked、排 `scheduler:<job_id>` 宿主提示、run 记 failed（工具结果无法确认为 `SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN`，
其它为 `SCHEDULED_TASK_UNFINISHED`），job 下一周期照常派发。`blocked` 进入任务终态映射；存量 waiting 停满 600 秒且无后续工作时由对账按
`SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP` 结算。报告新增 `runtime_status/runtime_reason`，来自 `AgentRunResult`。
设计见 `DESIGN_LEDGER.md` 同名条目，回归见 `test_scheduler_waiting_deadlock.py`、`test_tool_unknown_reason_preservation.py`。
复审修复 A：后续工作某一项读不出来时先按记录归属限定，剩下的继续等并按 run 节流打 `scheduler_follow_up_unreadable` 告警，
停满宽限期 6 倍按 `SCHEDULED_TASK_FOLLOW_UP_UNREADABLE` 结算；`_finish_scheduler_wake_claim` 收口没成（CAS 失败只释放租约）时退避 30 秒。
管理员的人工出口是 `/endtask`。
复审 A 的跟进：同类读错误不再遮住本任务确认存在的唤醒、进度策略或后台命令；后台命令坏记录的 `completion_target: {}` 按不欠通知跳过；
告警节流表清理先拍快照再遍历。
复审修复 B：已终态但完成结果还没交回父级的子代理算后续工作（修掉收口时子代理刚结束被误标受阻）；宽限期改为
`max(600, 5 × orphan_supervision_interval_seconds)`；Goal、guidance、进度策略只在宽限期内算；存量判定每 run 60 秒最多一次；
读不出从首次观察到“只剩读不出”起计时；子代理血缘记录按 id 精确读，缺失或损坏记为读不出。
复审 B 跟进：按“没有后续工作”结算要隔 60 秒两次确认（健康长 Goal 的续跑空隙不再被误结算）；本任务血缘子代理的坏完成唤醒按可能属于本任务处理。

capability 配置缺文件用默认值（分支 `claude/9a-capcfg-missing-defaults`，2026-09-28）：
- `/settings` 与 `/settings all` 的配置告警原本从主配置对象上找 `capability_config`，但 AgentConfig 没有这个属性，所以 capability 文件里没生效的键在生产上从来显示不出来。
- 现在由 `execute_settings_control` 经 `capability_config_for_agent(base_agent)` 取得 capability 配置，作为关键字参数只交给这两个视图。
- 回归见 `test_settings_config_warnings_display.py`：告警来自真实的 capability 文件。

压缩触发来源与校准口径（分支 `claude/38-compact-calibration`，2026-09-28）：`request_execution` 首次准备按 `preflight` 安装恢复宿主；
溢出循环把结果的 `runtime_source`（`preflight` / `provider_error` / `tool_context_overflow`）写进 `RunParams.compact_trigger_source`，
`_gateway_compact_overflowing_turn` 再经 `prepare_gateway_compact_recovery(trigger_source=…)` 交给公共恢复器，压缩进度事件（含 started）
带 `trigger_source`。恢复器在 `select` 冻结校准观测后，候选与压缩前都按同一口径折算，压缩开始时写进线程快照的 `before_tokens`
不再是未校准值。设计见 `DESIGN_LEDGER.md` 同日一节，回归见 `test_compact_calibrated_candidate_gate.py`（含两回合假 LLM 复现）。

TUI 插话丢失修复（分支 `claude/be-steer-loss`，2026-09-28）：生产结构化事实显示，插话随模型调用提交后，这次调用以
`ProviderTransientError` 失败。当时两层重试都因为“有未确认插话”不重发，attempt 失败；新 attempt 又不重发“提交不明”的插话，
入口回执永远停在 `active_pending`，TUI 每 2–3 秒轮询。修复：失败调用按调用编号把插话退回预留，同一 attempt 的重试重新提交；
重试守卫只看在途提交；没有请求文件的后台目标（定时任务 `srun_*`）在会话任务终态后，未认领的插话转成下一轮。
结构见 `04-structure.md` 同日一节，回归见 `test_steer_delivery_recovery.py`。

`/settings` 列表隐藏加载器元数据（分支 `claude/9a-batch3-e`，2026-09-27）：`settings_control_service._overview`、`_all` 改用`parameter_registry.listed_parameters()`，总数、“改过”统计与分类清单都不再包含 `config_path/config_sources/config_layers/config_warnings/memory_config_warnings`（加载器写进 AgentConfig 的元数据，不是参数）；参数搜索与 user_config 的可改数量同口径。字段与登记表项不删，`/settings show config_warnings` 仍可查看。回归见 `test_settings_chat_control.py`。
参数减量第 3 批 B 组（分支 `claude/38-internal-constants-bd`，2026-09-27）：Gateway 心跳节奏、心跳陈旧判定、request worker 空闲轮询、后台普通异常冷却、磁盘级 owner 唤醒重扫间隔、控制命令 HTTP 等待上限、一次 tick 消费的未处理观察上限七项不再是配置项，降级为读取点旁的具名常量（值不变：`lease_service.GATEWAY_HEARTBEAT_INTERVAL_SECONDS`=5、`status_rendering.GATEWAY_STALE_SECONDS`=120、`gateway_loops.GATEWAY_REQUEST_POLL_INTERVAL_SECONDS`=0.2、`gateway_loops.BACKGROUND_MAIN_ERROR_BACKOFF_SECONDS`=30、`gateway_loops.BACKGROUND_OWNER_WAKE_RESCAN_SECONDS`=120、`chat_parts/control_runtime.GATEWAY_SERVICE_COMMAND_TIMEOUT_SECONDS`=30、`conversation/runtime.CONVERSATION_UNHANDLED_OBSERVATION_LIMIT`=20）；用户配置里残留的旧键只在加载时告警。场景测试与 Live Lab 生成的配置不再写 `gateway_request_poll_interval: 1`（这两处的 Gateway 改按默认 0.2 秒轮询）。相关测试改为 patch 常量（`test_lease`、`test_gateway_loops_resilience`、`test_slow_model_liveness`、`test_chat_control_runtime`、`test_background_main_agent_runtime`）。

宿主提示（分支 `claude/be-host-notices`，2026-09-27）：前台回合在用户消息落账后、模型执行前发布会话待送达的 `host_notice` 流事件；正常回复提交时按编号取走（提交即已读），写进最终消息元数据与 `channel_delivery.host_notices`，`/result` 白名单放行、`/progress` 不转发；停止或失败不取走。飞书在同一条回复正文前加“【提示】”，TUI 与同会话窗口画成灰色系统行，历史回放排在用户消息之后。设计见 `docs/design/HOST_NOTICES.md`，回归见 `test_host_notices.py`。

`/settings` 值文字与其它回显出口统一（分支 `claude/9a-mask-value-display`，2026-09-27）：`settings_control_service._value` 删掉为绕开旧 `mask_value` 写的布尔、数字特判，值文字只由 `user_config_capability.mask_value` 给出（非凭据布尔 true/false、数字照实、None/空串/空列表/空映射为空串、凭据遮住），聊天里空值仍显示“（空）”。`mask_value` 根修后 user_config 的运行值/默认值/查看报告、改参回执的 `effective` 与命令行 `config get` 不再把 False、0 给成空串。回归见 `test_value_display_parity.py`。
`/effort` 智能程度检测（分支 `claude/be-effort-probe`，2026-09-27）：`_execute_effort_control` 在设置档位后调用 `settings/reasoning_probe.effort_probe_lines`（先执行再渲染回执）：档位设成 auto 以外、当前模型未声明且解析为不支持时后台自动检测一次；`/effort probe` 手动检测，`/effort revert <编号>` 撤销检测写入的档案修改，`/effort` 显示进度或结论。检测在后台线程跑，控制命令不等网络。回归见 `test_reasoning_probe.py`。
`/settings` 默认只看常用参数（分支 `claude/9a-settings-common-view`，2026-09-27）：`settings_control_service._overview` 只列参数中心
常用层级（`parameter_registry.COMMON_KEYS`，21 项）的当前运行值、是否改过、说明第一句，改了没重启注明“发 /restart 后生效”，常用以外
改过的只报个数；新子命令 `/settings all`（`_all`）是原总览加按分类的全部参数清单，［改过］［安全边界］标记。解析器 `all` 与不带参数同样
不接受多余参数，TUI 文本还原为 `/settings all`，命令目录补了帮助条目；IM 长回执由飞书适配器按行分片。回执里的布尔与数字不再经
`mask_value`（原来 False、0 显示成“（空）”；该特判已由最上面一条在 `mask_value` 根修后删除）。回归见 `test_settings_chat_control.py`、`test_parameter_registry.py`。
停止收尾补写决策点到达计数（分支 `claude/9b-owner-path-scope`，2026-09-27）：`_cmd_gateway_run_cleanup` 在结清在途模型调用、列出存活后台会话之后调用 `_flush_decision_reach_counts`，把 `conversation/decision_reach_counts` 里还没落盘的计数写出，部署重启不再丢最后一段；只接正常停止路径，不注册 atexit，出错只记 `gateway_decision_reach_flush_failed{error_type}`。原先内联的“取消在途决策”抽成 `_cancel_active_decisions`，行为与事件名不变。同分支修复 Gateway 用户读到本机主用户`memory_policy.json` 的作用域问题（`owner_resolver`）。回归见 `test_gateway_decision_shutdown_cancel.py`、`test_gateway_per_user_scoping.py`。
参数减量第 2 批（分支 `claude/9a-merge-config`，2026-09-27）：后台会话执行权只剩 `background_claim_ttl_seconds` 一个旋钮，续约心跳始终由 `run_claim.claim_heartbeat_interval_seconds(ttl_seconds=…)` 推导（90 秒时 30 秒）；手动 Compact 车道（`control_service._manual_compact_lane`）和前台请求车道（`request_binding`）不再读 `background_claim_heartbeat_interval_seconds`（已删除，旧键只告警并忽略），子代理 runner 会话心跳固定 5 秒。默认行为不变。

`/settings` 回显的结构脱敏（分支 `claude/be-structured-masking`，2026-09-27）：`show`、`search`、总览经 `mask_value` 结构脱敏，请求头与 MCP 服务器 env 的值只留键名、args 里凭据开关的值、名字是凭据的 `名字=值`、`--header`/`--env` 的值与网址密码遮值；`history` 行先经 `parameter_changes.displayed_change` 再遮一次，旧记录也不漏明文。回归见 `test_structured_masking.py`。

`/settings` 回执与脱敏补全（同一分支第二个提交，2026-09-27）：`show` 那一行由“实际使用值”改名为“实际效果”；`reset`、`revert`
回执与 `set` 同一口径附“按新值在默认模型上的实际效果”（回到默认时按登记默认值算，`parameter_changes.applied_after_change`）；
回显脱敏改用唯一的凭据判定 `user_config_capability.is_credential_key`，原先 `api_key`、`gateway_auth_token` 等 5 个凭据在
`/settings show` 与 `search` 里是明文。回归见 `test_settings_chat_control.py`、`test_parameter_registry.py`。

`/settings set` 附上新值的实际效果（分支 `claude/be-param-descriptions`，2026-09-27）：`settings_control_service._set` 对登记了派生规则的参数（`parameter_registry._APPLIED_RULES`：max_tokens、model_reasoning_effort）多一句“按新值在默认模型上的实际效果”，与 `show` 同一口径（Gateway 启动配置即默认模型，注明 /model 切换过的会话可能不同）；推理强度在不支持调节的模型上如实说“不改变请求”。计算经 `applied_value_with` 的只读新值视图，不另写判断；没有派生规则的参数回执不变。回归见 `test_settings_chat_control.py`。

参数中心同名常数收敛（分支 `claude/param-center-dup-constants`，2026-09-27）：流式 chunk 单次读取上限只在
`gateway_parts/io.STREAM_CHUNK_READ_MAX_BYTES` 定义（8 MiB），CLI 与 TUI 两个网关客户端改为导入；适配器入口不再另写领取时限，
直接用 `GatewayClaimLeaseConfig` 的默认值。数值不变。

`/settings show` 显示实际使用值（分支 `claude/settings-view-facts`，2026-09-27）：配置值会在运行时按规则派生的参数（目前只有
max_tokens，按模型窗口 ÷ 4 夹取）多一行“实际使用值”，按 Gateway 启动配置即默认模型计算，并注明 /model 切换过的会话可能不同；
派生函数来自参数中心 `parameter_registry.applied_value`。测试见 `test_settings_chat_control.py`。

聊天 `/settings`（参数中心阶段 2，分支 `claude/param-center-phase2`，2026-09-27）：用户希望 my-agent 与自己都能改更多参数、
改错能回滚，且用户几乎不用命令行。`control_service` 新增 `settings` 分派（在 steer/stop 默认路径之前），交给
`settings_control_service.execute_settings_control`：只有管理员可用；查找、查看、修改、恢复默认、修改记录与回滚都走参数中心，
修改写入当前加载的用户配置并记入 `settings-changes.jsonl`，重启 Gateway 后生效。TUI 本地模式明确拒绝，文本还原保留原值。
测试见 `test_settings_chat_control.py`，设计见 `docs/design/PARAMETER_CENTER.md`。

2026-09-27：新定时运行在原claim成功后、模型前准确绑定原TaskStore，旧任务/pins/marker保持。合法冻结回复经当前pending回读后优先走原交付；定时工作使用原终态映射，无法确认状态或结算CAS失败时不消费wake。独立复核发现的claim接手反例已复现并修复，最终14文件375项通过；完整组合全仓与原生验证仍待完成，见[回归记录](../../../TESTS.md#c16全仓回归修复2026-09-27验证中)。

`/skills` 异常回执修正（分支 `claude/skills-receipt-fix`，2026-09-27，Codex 静态复核发现）：`execute_skill_control` 的普通异常
原来一律回“原记录没有改动、请稍后重试”，但回滚/删除是先改目录和登记表、再追加账本，账本追加抛 OSError 时改动已经生效。
现在按子命令是否写入选回执：写子命令（confirm/reject/learned_revert/learned_remove）只说结果没能完整确认、先查当前状态，
只读子命令只说暂时读不到；可预期失败的回执不变。不加事务或新状态层。测试见 `test_skill_chat_control.py` 的提交后失败用例。

能力包第七候选：新Goal绑定任务时可初始化原TaskLink的一次选包pending（默认关闭）；只读当前owner的元数据资格，旧任务不补字段，不新增模型调用或Goal状态。组件与主流程组合验收中，未发布。

聊天 `/skills`（分支 `claude/skill-proposals-tui-im`，2026-09-27）：用户几乎不用命令行，技能提案与自动总结 Skill 原来只有
`my-agent skills …` 入口。`control_service.execute_gateway_conversation_control` 新增 `skills` 分派（在 steer/stop 默认路径之前），
交给 `skill_control_service.execute_skill_control`：按控制范围解析 owner，提案确认/拒绝必须带用户看到的版本号并由服务端锁内复核，
列提案时调用自学习审核顺序点，自动总结 Skill 走 `capability/skill_learning_report.py`（与 CLI 共用）。TUI 本地模式明确拒绝，
文本还原带上参数。测试见 `test_skill_chat_control.py`，设计见 `docs/design/SKILL_AUTO_SUMMARY.md` 第 10 节。

`/effort` 从空壳改为真实会话设置（分支 `claude/reasoning-effort`，2026-09-26）：`control_service._execute_effort_control`
读写当前 thread 的 `reasoning_effort`（auto/off/low/medium/high/max，`default` 清除回全局默认），回执说明当前会话模型的实际效果；
与 `/verbose` 共用新抽出的 `_settings_thread`（行为不变）。每轮请求在 `tool_model_generation._provider_request_options` 现读线程
档位，网关自动选模 `gateway_model_adoption._payload` 用同一函数投影（参数收进 `_PayloadSurface`），逐字核对保持一致。
设计见 `docs/design/REASONING_EFFORT.md`，测试见 `test_reasoning_effort.py`。

自学习 S3 接入后台策展车道（分支 `claude/skill-auto-summary`，2026-09-26）：`cli/gateway_loops.py` 的策展车道在记忆整理之后
处理自动总结 Skill 请求。owner 有待处理学习请求时，即使记忆总闸关闭或当日记忆配额用完也会被准入，但那时只跑自动总结、
不跑记忆整理（准入后按原条件重算）；`memory_curator_enabled=false` 而 `enable_self_learning=true` 时车道照常运转。
学习失败只写 `gateway_skill_learning.iteration` 诊断，不影响记忆整理与其它 owner。设计见 `docs/design/SKILL_AUTO_SUMMARY.md`，
测试见 `test_skill_learning_integration.py`。

Gateway 安全重启第三批：TUI 续跑边界与确认框作废（分支 `claude/turn-resumed-boundary`，2026-09-26）。
被重启或执行超时打断的回合由接班进程按原请求号续跑，TUI 读同一个 chunk 文件，以前没有任何“上一代已结束”的信号：
上一代开着的确认框让新确认在 `_TuiPermissionController.open` 报“已有待确认”被吞，回合可能一直等；旧回复、旧思考和续跑文本拼在一起；
死掉那一代的执行中工具卡一直显示运行中；续跑轮号按已落账调用数接着数，被打断那一轮没有落账时新卡与旧卡同号。现在：
- `request_execution._handle_gateway_request` 对 `request_binding.gateway_request_is_active_turn_recovery` 成立的请求，
  在创建 chunk writer 之后、执行本代之前经 `BufferedChunkStreamWriter.write_turn_resumed` 写一次 `{"kind": "turn_resumed", "cause": ...}`，
  cause 原样取 `active_turn_recovery.cause`（旧形式为空）；认领时已被停止的请求不执行也不写。
- TUI `tui_runtime._close_resumed_turn_generation`：旧审批按 cancelled 本地关闭、不写回；活动思考与回复按 interrupted 冻结；
  参数临时行与进行中的 Compact 进度收口；adapter 登记的未终态工具卡按中断收口，已终态的不重发；按 cause 显示“已自动续跑”提示；
  续跑代次的工具卡与审批块号追加 `:resume<代次>`。
- 旧版 TUI、普通 CLI 与 `gateway ask` 对该行投影为空；IM `/progress` 白名单忽略它。
- 未处理：同会话其它窗口经 `bg-main:` 后台显示流看到的旧代活动块；恢复放弃续跑时 TUI 终态仍不关闭运行中的工具卡。
回归见 `test_tui_runtime.py`、`test_tui_stateful.py`、`test_gateway_safe_restart.py`、`test_gateway_streaming.py`、
`test_gateway_client.py`、`test_gateway_verbose_progress.py`，设计见 `docs/design/GATEWAY_SAFE_RESTART.md` 文末第三批。

`/admin` 指引修正与审计软提示（主线，2026-09-26，真实验收发现）：执行飞书请求的是 owner 池里按用户隔离的 agent，其 owner 字段被改成该用户，原判定里的“基础 owner 是 local/main”永远不成立，指引没有出现；`admin_binding_hint_for_request` 改为只看开关、全局数据根的密码文件与绑定表。`audit_records` 的 requests 主题给管理员附软提示（查飞书用 all_owners、已设密码未绑定时发 /admin），`MODEL_NOT_CONFIGURED` 处理建议补上 `/admin`。真实验收里 my-agent 两轮工具即给出正确结论。

审计工具新增 `requests` 主题（主线，2026-09-26，用户要求“这种东西以后 my-agent 能帮我解决”）：每个请求开始执行时响应带 `owner_id`（宿主解析的执行 owner 规范编号，`request_execution._executing_owner_id`），`request_audit_records.request_outcome_records` 按 `OutcomeQuery` 读窗口内请求结果（状态、错误码与错误分类表的处理建议、渠道、私聊/群聊、耗时），归属优先 `terminal_response.owner_id`、旧记录退回会话，都没有的列为 `unattributed`；经 `GatewayTaskBindingWriter.request_audit_outcomes` 供 `audit_records`（`tooling/audit_requests_topic.py`）使用，跨用户沿用管理员两道门。回归见 `test_audit_requests_topic.py`。

未绑定管理员时的 `/admin` 指引（主线，2026-09-26，用户真实使用中发现）：管理员设好密码后在飞书私聊直接发消息，因为还没 `/admin` 绑定，按飞书普通用户运行得到 `MODEL_NOT_CONFIGURED`，提示只提 `/model`。现在 IM 私聊、开关生效、已设管理员密码且该私聊未绑定时，失败回复（`request_execution._gateway_user_error`）和 `/model` 没有可选模型的回复（`model_profile_service._admin_hint`）追加 `/admin <管理员密码>` 指引；判定唯一入口 `request_worker.admin_binding_hint_for_request`。回归见 `test_admin_identity_gateway.py` 末尾两例。

IM 管理员身份与聊天内审批（主线，2026-09-26 合入，用户决定“支持注册飞书账号为管理员、确认用管理员密码”）。
以前管理员只有本机 local/main：飞书用户永远是自己的 owner，IM 请求也不带审批能力，需要确认的工具一律被拒。现在：
- 本机 `my-agent admin-password set` 保存 scrypt 管理员密码（`config/admin-password.json`，0600）。
- 飞书一对一私聊发 `/admin <密码>`，把 `(channel, user_id)` 精确绑定为管理员（`config/admin-channel-identities.json`）。
  之后这个私聊的请求与控制作用域都经 `admin_channel_identity_for_request` 解析为 local/main。
- 同一渠道身份 10 分钟内错 5 次锁 10 分钟，节流记录持久化，拒绝文案不区分原因。
- Gateway 服务端为这些私聊开启原 `StreamApproval`。`/progress` 投影待确认工具，适配器提示 `/approve`、`/deny`。
  `/approve <密码>` 只批准本会话唯一待决的一次，`/deny` 拒绝；决定都写原 permission bridge 的精确决定文件。
- 密码不进回执（`/admin ******`）、会话记录、请求队列、日志、适配器持久入站或 TUI 输入历史；终端本地拒绝这三条命令。
- 开关 `admin_channel_identity_enabled`；新增错误码 `ADMIN_PASSWORD_REJECTED` 等 6 个。
- 已知边界：后台续跑与子代理的审批在 IM 里仍无人接收；飞书保留原消息，需要用户撤回。
回归见 `test_admin_identity_store.py`、`test_admin_identity_gateway.py`、`test_admin_identity_clients.py`，
设计见 `docs/design/ADMIN_CHANNEL_IDENTITY.md`。

Gateway 安全重启第二批（主线，2026-09-26）：终端 `gateway restart` 默认经 `cli/gateway_restart_handover.safe_restart_from_cli` 写 kind=cli 请求并按状态文件等新进程号 running 或本请求 cancelled（托管自己的工具进程仍拒绝），`--force` 保留先停后起；排空期间 `_RequestDispatcher` 以 `hold_reason=gateway_restart_draining` 调 `dispatch_pending_requests`，只给待处理请求写 `admission_wait_*` 等待事实、不认领，客户端据此续期；TUI `tui_upgrade_follow` 读 `restart_drain.phase` 在页脚提示正在安全重启。回归见 `test_gateway_safe_restart.py`、`test_tui_upgrade_follow.py`、`test_gateway_commands.py`（旧先停后起用例改为显式 `--force`）。

审计只读决策观察（决策线，2026-09-25，本地分支 `claude/decision-audit-controls`，待合并）：新增 `gateway_parts/request_audit_records.py`，
为统一审计工具 `audit_records` 只读扫描请求记录里的 `model_selection_observation` 与 `capability_presentation_observation`，
按 `conversation_claim.thread_id` 归属调用方给出的 owner 会话，字段白名单投影（不含 prompt、工具名清单等），
只看窗口内修改过的记录、一次最多读 300 份，超出标 `truncated`，不写任何文件。入口是 `GatewayTaskBindingWriter.decision_audit_observations`：
队列位置只取写入器自己的请求路径，与实验证据读取同一做法，不信任 agent 自身的 Gateway 配置、不接受模型参数。
回归见 `test_decision_audit_controls.py`，设计见[决策开关、超时自调与审计](../../design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#5-统一审计工具-audit_records)。

Gateway 安全重启第一期（主线，2026-09-26，用户批准“代理要能自己重启且不出事”、full access 下免确认）：重启请求、两段排空、换进程、续跑与通知落地。`gateway_parts/restart_service.py` 是唯一状态源：`gateway_restart.request`（目标进程号、发起方结构化身份、原因；同一目标的重复请求合并为 `additional_requesters`）、进程内排空阶段、`gateway_restart.completed` 标记与 `gateway_restart_state.json`（冷却起点、近期请求）。服务主循环（`cli/gateway_restart_handover.drain_for_requested_restart`）发现指向本进程的请求后：第一段置 `restart_draining`，请求派发器不再认领新请求（留在 pending）、后台 supervisor 只回收已完成车道，等本进程 admission 在飞数归零，上限 `gateway_restart_turn_wait_seconds`；第二段关闭 `concurrency/restart_gate`，`tool_operation_coordinator` 的新副作用工具停在领取之前（被中断则按 not_started 的 `CANCELLED` 收口），等执行中的工具归零，上限 `gateway_restart_drain_timeout_seconds`（0 不限），超时撤销请求、恢复服务并给发起会话写取消通知。排空成功后写完成标记，按 `planned_restart` 收尾，再由旧进程拉起带 `--after-pid` 的接班进程（systemd 单元 cgroup 或 launchd 标签托管时改为退出码 75 交给管理器）；接班进程等旧进程退出后先消费标记，启动恢复以 `gateway_safe_restart` 原因立即重排旧回合（不加 10 秒延迟，排在新请求之前），再给发起会话写“重启已完成、不要再次重启”的持久唤醒。入口：管理员主代理的 `restart_gateway` 工具（effect=dangerous，只写请求立刻返回）与管理员 `/restart` 控制命令；托管自停闸的拒绝文案改为指向它们。冷却 `gateway_restart_cooldown_seconds`（默认 30 秒），同一会话 10 分钟内最多安排 3 次。回归见 `test_restart_gate.py`、`test_gateway_safe_restart.py`、`test_gateway_restart_tool.py`。未做（后续分期）：终端 `gateway restart` 与部署工具改走安全重启、重启窗口内 `/stop` 找续跑回合、TUI/IM 的重启提示与确认框作废。

托管自停闸与聊天 `/model`（主线，2026-09-25，用户确认 `/recover` 事故的两项后续）：`cli/gateway_host_guard.py` 让 Gateway 服务进程启动时把自身进程号写进 `MY_AGENT_HOSTING_GATEWAY_PID`，工具子进程经 `_subprocess_text_env` 继承（凭据擦洗不删它）；`gateway stop`、`restart`、`start --force` 在写停止请求前比对目标进程号，相等即拒绝并以 2 退出，回合不再被自己切断。`/model` 在命令目录增加会话后缀，成为 kind=model 的聊天控制：`control_service` 延迟导入 `model_profile_service.execute_model_text_control`，与 `/client/models` 菜单共用抽出的 `_scoped_model_host`（owner/线程解析）和 `execute_model_profile_operation`；只做 list/select/set_default，编号取同一列表的可选行，渲染不含接口地址和密钥。TUI 单独 `/model` 在 `_tui_submit_control_operation` 让回本地菜单。回归见 `test_gateway_host_guard.py`、`test_model_text_control.py`。

结果未确认的执行轮有了会话内显式出口 `/recover`（主线，2026-09-25，用户批准第 1 项）：真实会话里模型在回合中执行 `my-agent gateway restart`，Gateway 自杀后启动恢复把该回合 run/attempt 记为 unknown，那条 `run_command` 停在 EXECUTING；自动续跑按 `recover_recorded_active_turn_attempt` 拒绝（`ACTIVE_TURN_OUTCOME_UNCERTAIN`），之后每条新消息都续同一个 active 工作任务，在 `create_attempt` 的 unknown 闸被拒，而唯一人工出口 `recover_attempt_unknown` 没有任何用户入口，会话永久卡死。现在 `gateway_parts/turn_recovery_control.py` 按已认证 scope 解析 owner/thread，读 `thread.workspace_task_id` 的 `main_agent_recovery_block_for_task` 投影：`/recover` 只读列出 `unsettled_attempt_operations`（工具名、状态、开始时间，不读参数和结果正文）；`/recover recorded|confirmed_noop|abandoned` 以用户选定的结构化处置调用 `recover_attempt_unknown`，释放阻塞，下一条消息接着原任务新开一轮。处置取值表 `ATTEMPT_EFFECT_DISPOSITIONS` 放在 `runtime_db/operations.py`，解析器与仓储共用。`create_attempt` 的两处 unknown 闸改抛 `RuntimeRecoveryRequiredError`（`RuntimeConflictError` 子类，`error_code=RUN_RECOVERY_REQUIRED`），客户端文案指向 `/recover`，不再是“请稍后重试”；`ACTIVE_TURN_OUTCOME_UNCERTAIN` 文案同样指向 `/recover`。不自动重做旧操作，不改聊天记录，不按正文猜目标；状态不是 unknown attempt 的阻塞返回 `RUN_RECOVERY_REJECTED`。回归见 `test_turn_recovery_control.py`。

无进程身份的悬挂运行轮改为可见并可显式结清（主线，2026-09-24 晚，用户决定第 5 项自愈）：Gateway 启动的 `_recover_gateway_stale_attempts` 在原进程死亡证明之外，另经 `RuntimeRepository.unidentified_stale_attempts()` 列出 metadata 里没有 `runner_pid` 的 current attempt（旧版本写入、永远无法证实死活），写进 `state.json`/`gateway_run_started` 的 `unidentified_stale_attempts` 计数与 `gateway_stale_attempts_reconciled` 事件的 `unidentified` 列表；不自动判死（同一 owner 库可能被别的运行版本写入，原 RUN-01 合同保持）。结清走显式命令 `my-agent runtime-stale-attempts --settle [--older-than-days N]`，经 `settle_unidentified_attempts` 的 current_attempt CAS 记为 unknown 并在 metadata 记 `recovery_reason=no_runner_identity`。测试 `test_runtime_db_recover_stale.py`、`test_startup_commands.py`。

Gateway 停止时结清在途模型调用（主线，2026-09-24，用户决定第 4 项，已合入）：`_cmd_gateway_run_cleanup` 收完三条循环后调用 `agent_core/model/call_runtime.settle_open_model_calls_for_shutdown()`，把进程 agent 账本里仍是 started/first_token 的调用一次性记为 failed，错误码 `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`、类型 `HostShutdownInterrupted`，用量继续按缺报处理（不补零）；有在途调用才写事件 `gateway_model_calls_interrupted`（call/request/run 身份、后端、模型、账本用途桶、是否见首 token、耗时、估算输入、HTTP 尝试数、是否探针、原因码，不含正文），收尾事件与控制台行新增 `interrupted_model_calls` 条数。账本模块出错只记 `gateway_model_call_settlement_failed{error_type}`，不中断收尾。边界：只覆盖 Gateway 进程 agent 自己的账本；子代理 runner worker 的账本在其自身生命周期内结算，非正常退出（kill -9、断电）留给启动对账切片。测试 `test_gateway_model_call_shutdown_settlement.py`。

线程中断标志不再随 ident 复用串到新线程（已合入 main `761ef2ab2`，双机已部署 `runtime-step11b-240d0f70`）：`concurrency/interrupt.py` 的中断标志改为记住立旗时的线程对象（弱引用）。线程已退出或 ident 换了主人即视为过期并清除；给已退出线程立旗直接落空，关闭竞态里晚到的立旗不再残留。此前长期运行的 Gateway 里，新线程可能复用带脏标志的 ident，被静默、随机地取消。公共接口不变。

Gateway 停止时主动取消在途决策（已合入 main `25650830d`，主线 owner 同意的一行）：`_cmd_gateway_run_cleanup` 置位停止事件后，立即调用 `decision_policy.cancel_active_decisions_for_shutdown()`，让正在等待决策模型的前台/后台调用回到原方案，关闭后不再发新决策。它只取消本进程内登记的决策句柄，不读写持久状态；用 try/except 包住，出错只记异常类型事件 `gateway_decision_cancel_failed`，不中断后续清理。停止时后台模型请求的结构化"被中断"记录由主线 owner 紧接着另加。详见[接入设计](../../design/DECISION_MODEL_INTEGRATION.md)第 4.2 节。

决策实验对照记录与授权内自动晋升（本地分支 `claude/decision-experiment-records`，待审）：只观察实验调用经原账结算后，结构化对照条目（身份、配置版本、基线/候选名单、结算视图）经能力观察出口拆出写进同一请求记录的 `experiment_records`；回合正常收尾时按结构化工具账补写实际调用工具名，停止/关闭的回合不补写。`/experiment apply skill_tool …` 另授权宿主在证据规则（≥3 可比较样本、全部 charged、短名单召回 1.0、有节省）满足时，于回合收尾在精确回合锁内经原设置 CAS 把本会话 skill_tool 改为 apply，用户后改、撤销、到期、被替换都跳过不覆盖；回执写在 `experiment_records.promotion`，已有即不重试。普通请求零 I/O、请求字节不变。详见[E1 交接第三片](../../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)。

决策实验授权入口（本地分支 `claude/decision-experiment-send-gate`，待审）：HTTP `/ask` 与文件队列沿 `/audit … prepare` 同一任务命令机制接收 `/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>`，参数冻结进排队请求的 `system_task`、模型只见任务正文；新增 `request_experiment.py` 在主轮绑定后、首个模型调用前于精确回合锁内写 `experiment_grant` 回执并调用 E1 授权原语，重放/重启不再授权，失败只提示用户、不阻断业务。发送硬门、经验输入上界与结算归决策服务和传输层，详见[E1 交接](../../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)。

能力推荐观测写进请求记录（本地分支 `claude/decision-capability-observation`，待审）：真的发起过能力推荐决策时，结构化观测（码、版本、名称与计数，无正文）经独立 observer 追加到 `capability_presentation_observation.entries`，最多 8 条；与模型观察同一 active-turn 事务，回合终结时照原语义抛中断，其它写盘失败只放弃这一条，内存请求同步更新。原展示回调、已有键不变。

Gateway 消息文件流式读取（本地分支 `claude/decision-gateway-message-reads`，待审）：建索引、近期产物、追加与补写去重不再按行数整块物化尾部，改为与原实现逐项等价的字节有界流式读取；4.2M 字符夹具上准备期峰值 21.33→0.82MB、全程 22.65→10.03MB，每次请求三次读取约 122→19ms。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#gateway-213mb-峰值来自整块读取消息文件2026-09-24已实施待审)。

媒体会话越过压缩点（本地分支 `claude/decision-media-preflight`，.9 真实验收已通过，待审）：未压历史带图时，preflight 只守窗口硬上限，越过压缩点也不再整轮失败；越过窗口时强制恢复报 `COMPACT_REQUEST_NON_TEXT`，客户端文案说明是图片等非文本内容使压缩不可用。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#媒体会话越过压缩点2026-09-24本地修复)。

12.4来源生命周期首片仅机械兼容显式只读Sequence：`_gateway_conversation_refs`按是否给出来源启用完整投影，防止换容器后误走普通展示窗口；history_projection接受非字符串Sequence。宿主的完整请求冻结/释放尚未重构，相邻回归另行记录，不把本片当全链内存收口。12.7固定旧包的同会话压缩/显式跨模型真实缓存另有证据，详见[真实验收](../../tasks/DECISION_MODEL_REAL_VALIDATION.md)。

12.4 第二片 2a（本地分支 `claude/decision-12.4-2a`，未合入）：Gateway 上下文与恢复候选只保存只读历史来源，移除 `_gateway_conversation_refs` 和具体副本；4.2M 字符下种子准备驻留约 54KB，首次发送前峰值从 29.8MB 降到 21.3MB，摘要期峰值留给 2b。独立评审后：来源冻结投影时刻，终态折叠不随解析时间变化；删除 `_conversation_prompt_section` 生产不可达的 `include_transcript` 正文/摘要分支，原先断言该分支的两个用例改为断言生产路径（摘要只在种子历史段，操作证据只在上下文段）。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子只读来源2a2026-09-23本地)。

12.4保留历史完整投影已本地实现：Gateway、后台和child的Compact来源/候选不再套普通字符窗，完整材料统一进入原容量门；普通展示保持原规则。73项联合及416项相邻回归通过（含重叠，不累加），整项12.4及11/18不变。见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Gateway Compact的原visible范围规则现编译成逐行selector，公共message_selection沿固定完整尾界两遍验证/筛选；后台通过原Store延后正文，共用同次任务范围。writer/CAS与执行身份不变。12文件326项通过，4项主线独占后台fake Store签名待集成，整体gate未通过；12.4仍开放，详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

决策模型第12.4媒体整合本地1419项联合与严格gate通过：普通媒体沿原模型发送，未知模态不自动切模型或提交强制Compact；摘要覆盖只到完整文字前缀，原生媒体后缀保留。原媒体M3验收不替代集成版证据；11/18和旧全仓八项失败状态不变，详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Gateway/child的无transcript活动归档已移除先粗估提交再重新准备的旁路，统一复用完整请求候选与同次发送。混合transcript+carried在公共恢复器同时替换原历史及已标记工具交接，单次CAS封印双来源；普通摘要失败、未知IR、超量及取消不发送恢复业务。本片32文件738项与严格gate通过，准确边界见TESTS；初次/手动与真实native工具IR组合仍未完成。

Gateway/child现通过原canonical loader绑定同一Compact scope/view，真实恢复参数在CAS后取得获胜checkpoint。后台也已接公共完整请求恢复及活动归档纯投影；定向验收见TESTS。初次/手动、其它宿主活动归档和混合超大来源仍待统一，12.4保持未完成，未部署。

后台Compact已本地接同一scope/view的摘要注入和精确覆盖，局部来源/提交不改全线程摘要和游标；18文件联合420项通过，最终验证见TESTS。此片不证明完整恢复payload，Gateway/child准备同view、初次/手动和真实缓存仍待验；唯一TODO的12.4保持未完成。

Compact检查点底座已写v3，区分提交前驱与摘要基础；局部CAS保留全线程摘要/游标，新工具恢复按完整执行身份处理。后台实际选择scope并将同一摘要view交给注入和隐藏的接线尚未完成，12.4仍不关闭。

后台上下文的 `prepare_background_context` 保留原事实读取与进度对账，`render_background_context` 只消费冻结值并调用原预算器；`BackgroundHistoryProjection` 保存同次任务范围与摘要投影，纯种子投影不重读任务。完整后台Compact接线仍待作用域检查点边界闭合，不能把全局新摘要给detached或窄审计事件。

Gateway恢复协调已抽到 `agent_core/compact_request_recovery.py` 与child共用，原payload/CAS/取消证据保持；Gateway模块只负责自己的历史投影和边界事件。

- 第 12.4 项 Gateway overflow 已本地接通完整恢复请求计量：只读保留原来源，真实请求准备后生成摘要候选，原 checkpoint/CAS 成功后直接发送获选材料。两协议、工具开关、取消/代次竞争/摘要错误及后续工具轮等 16 文件联合 337 项通过；HTTP 为内存替身，未部署。子代理、后台、初次加载及手动 Compact 仍待接入，完整进度见 `docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md`。

- 主会话自动模型选择的 Stage A 已建立原线程版本事实：`model_selection_revision/source/last_explicit_revision`
  随原线程一次原子更新，显式同值选择也前进版本并终结 pending 子代理建议；旧数据全缺才归一为未知，坏字段拒绝。
  本片只提供宿主并发/恢复事实，尚未启用主会话自动采用，也不增加逐片确认或永久固定模型。
- P5-D Stage C 在后续独立片已把 Gateway 请求级建议接到主会话首次真实发送前：原完整请求与候选 provider payload 验证、目录代次→准确车道 T→线程 CAS；发送前明确拒绝才回原模型一次，HTTP 后不跨模型重发。fake HTTP 本片34项、联合302项通过，真实供应商验收待做；工程容量估计不能称为精确 token 上界，详见 `docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md`。
  详见 `docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md`；Gateway 的 lane/模型作用域重排仍待后续片。

- Jev 能力推荐的同一 Gateway 请求展示复用已核对的内存选择：原执行回调保存是否评估过和采用的 Skill/工具展示，transcript Compact 与超窗重试按当前身份、权限和连接重新核验。无效则清除本回合旧值，已评估回合不再次调用 Jev；新请求从基础面开始。122 项本地组合覆盖 DB attempt 轮换、动态名卡/schema、失效和单次建议；原溢出及更多回归仍在收口，尚非真实 TUI 验收。

TUI 观察超时不再标记业务失败：canonical terminal 优先检查，同请求/同游标退避续等，页面退出收口；plain 有限等待保留。官网 M2.7 原页迟到回复及暂停恢复对照通过，详见 TESTS。

独立资源线新增连接寿命和空闲 owner 回收：16 个 HTTP worker / 128 在途不变，socket 空闲 5 秒、排队 2 秒后明确拒绝；状态计数改为目录类型复用。请求持有 owner 精确实例租用，维护不续空闲期，60 秒空闲且无持久硬事实才退池；不改执行状态和原队列。100 身份/50 执行槽已在受限测试机用假模型验证，官方真实 TUI 单列；尚未合并默认环境，见 [资源合同](../../design/TUI_RESOURCE_LIFETIME.md)。
- 插件面板入口 `/client/plugin-panels`（第 9 步，本地开发）：可信来源检查先于读正文，owner 只由 Gateway 作用域解析，冷 owner 不加载实例；
  活动投影复用 `conversation_agent_activity` 只读结果，交给进程内唯一展示服务；服务随 HTTP server 停止关闭。合同测试见 test_gateway_plugin_panels。
  第 10 步：另附本 owner 会话列表的惰性读取函数（只在面板订阅 `sessions` 主题时调用，白名单字段，读取失败按空列表）。
- 插件宿主只读 API `/plugin-host/query`（第 10 步）：回环来源 + 按插件激活发放的令牌，只读主题白名单；服务启动时登记回环地址、停止时清空全部令牌。

插件命令流的取消原语现直接引用 `common/cancellation.py`，不再越层依赖 tooling。保持逐请求取消和原审批运输，发布前回归进行中。

- 独立插件命令的交互审批已有本地实现：原 HTTP 请求线程执行，消息流运输原审批和结果，心跳仅检测本连接离开。
  同机 TUI 使用原 GatewayPaths 及服务端规范 owner 派生审批地址，覆盖按用户隔离关闭时的 owner 映射。
  复用 StreamApproval 和原文件桥且禁用会话批准缓存；每次 Enter 的令牌不借主/子任务，断连不自动重发。
  临时 HTTP/MCP 与 TUI 控制器组件已验证，完整实际多 TUI 装卸仍待验收，未发布部署；详见 TESTS。

- 配置命令已有本地接线：HTTP/direct 共用原管理员、来源权限、宿主请求和 ToolExecutor；正文只含文件引用，值只写私有安装表。
  安装表 v2 同次保存配置与版本，旧 v1 显式迁移；目录 v2 带安装版本使旧配置请求过期。Gateway/客户端开发回归已覆盖原入口。
  查询仍只读原结果，UNKNOWN 不重跑；没有新增队列、Agent 初始化或后台进程。启用、撤销与真实 TUI 装卸尚未完成。

- 第 4 步环境准备已有内部源码与临时 venv/pip 组件验证，尚未接 Gateway 启用动作。
  原 owner 配额锁增加显式非阻塞准入，旧调用默认等待不变；环境竞争时返回配额不可用，不另建锁或后台队列。
  原安装状态不因候选目录存在而启用；激活、撤销和实际多 TUI 验收仍待完成，详见环境合同及 TESTS。

- 第 4 步管理适配已在本地接线、未发布部署：`/client/plugins` 与 ask/control 复用原管理员授权，安装进入独立原运行及唯一工具执行器。
  查询按原请求只读，不初始化冷 owner 或重跑操作；超时保留未知及查询编号，目录刷新失败不抹掉已知结果。
  模型配置和插件管理共用 `owner_conversation_store.py`；完整代理和冷入口共用 owner 路径权限裁决。独立环境、激活和撤销仍待完成。

- 宿主目录片已发布同版双机，TUI 151—153 所测框架入口通过：`/client/plugins` 和 ask/control 共用原 owner 解析，返回不可变声明及 revision。
  冷用户不创建 Agent/thread；无中间件本机与群聊 metadata 保留原规则，不重做 auth。
  旧版本或缺失业务版本明确拒绝，不自动重放，不进入原控制或队列；实际插件贡献仍为空，装卸尚未实现。

- 参数次片已发布同版双机，所测 TUI 入口已复验：`ask/control` 在原鉴权后消费公共插件静态帮助或结构化错误。
  不新增 ControlKind，不触发旧控制回执持久化、guidance、模型或普通队列；后续宿主片已接 owner 声明投影，实际插件执行身份留生命周期实现。
  483 项相关回归及严格 gate 通过；TUI 146 的 Tab→Enter、两端各 13 类命令检查通过，143 原失败及普通任务质量单列。
  TUI 150 的活动请求首秒已验证静态命令分流，原 attempt 自然完成；未进入原生正文、guidance 或 Shell。

- 插话网络重放已收紧到原 mailbox 的旧/新 turn 排序锁与准确回执：先修提交/确认批次，只有最新 pending 才预留后继。
  回应显示实际回执状态；候选 ID 与 DB current 成对 CAS，半写失败沿原 pending 重试，模型与 runner 启动留在锁外。
  文件模式准入也已实现并发布，累计源码全仓与严格 gate 通过；TUI 138—142 分项复验和未实测旁支见 TESTS。

- 当前停止源码在主 Goal/task 外层锁下短读 Gateway T，释放 T 后进入 creation→子 Goal；主/子资源在同一控制边界固定。
  异步清理只接收冻结批次，不持 agent、不重扫后来恢复的孩子；PTY 或子树准备错误仍保留其它已提交清单。
  终态孩子保留业务结果，独立 runner 沿原心跳转交原轮取消。已同版部署双机，实际主后台、孩子、PTY、孙代理与另一会话隔离已验。

- 主资源停止已发布：持久主任务按正式 task/run/attempt 关闭权限，再冻结 v2 后台清单。
  原热请求绑定过期时不向恢复轮发任务中断；停止准备与 Goal 显式恢复串行，旧后台片在绑定新 attempt 前检查中断。
  进程清理在锁外消费原清单，部分失败保留已提交回执；后台退出确认与 PTY 异步请求分开。
  该片 20 个相关文件 735 项开发回归通过；后续源码已补 direct/local 与无持久任务热请求的主链，完整子树后台资源现已在源码接通固定清单，TUI 137 原失败未关闭。
- 无持久链接的热请求在原 T 锁内读取正式运行绑定，释放 T 后关闭精确权限并冻结主资源；已晋升、会话/绑定不可读和过期代次返回未确认。
  不补建 task link 或按请求编号扫描历史 main；此旁支本轮仅有开发回归，不能借 Gateway Goal 验收声称真实命中。

- 公共命令首片已发布并同包部署：HTTP ask/control、普通文件提交和旧队列执行统一读取公共命名空间判据。
  `/plugins@` 的缺 ID、异常后缀与正文参数均明确拒绝；原请求、中断回调和旧 guidance 保持不变。
  不新增插件控制类型或执行器，187 项相关定向（含文档测试）通过；实际命名空间与普通工具链已验，TUI 137 暴露的后台资源停止缺口已修复并有新版独立复验，参数与宿主声明目录随后独立发布验收，第 3 步本轮框架范围收口；真实插件装卸与业务权限仍待后续实现。

- 同一前台请求和后台工作片跨 Compact 保留宿主的精确拒绝列表，修复重建运行参数后再次询问已拒绝调用的问题。
  与子代理 Goal/Compact 接续共用运行参数链，不新增持久表、不提升批准、不改变控制语义。
  529 项相关定向及本地严格 gate 通过；当前为本地候选，双机安装版复验尚未完成。

- 请求适配已独立上下文、绑定、历史和输入渲染，后台从会话领域共享完整历史行；原锁、持久路径和提交顺序不变。
  请求执行文件 3,630→1,182 行，110 个定义/常量逻辑一致、666 项定向通过；新版双 TUI 暂停、压缩、重连、
  70 秒原程序续采和双子代理插话路径通过。报告初次遗漏合计、错误心算和未完全修正的间隔范围保持失败记录。

- 请求编排已分离流式缓冲、事件投影及审批组件，恢复直接沿原 chunk 地址发布终态。
  前后台 Compact 携带共用纯计算，释放和提交时机保持；定向 510 passed、3 skipped、3 xfailed，
  严格尺寸 hard=0、基线不变。真实双 TUI 的子代理/插话、审批缓存、压缩重连已验；暂停后工具仍写入的
  失败已沿公共进程树终止修复并复验：暂停静止、压缩重连和真实程序续采通过，另一会话未中断。
  强制终止的 native 信封缺口保留为边界；请求适配后续拆分见上项，下一批进入存储组合。

- 后台单片执行、历史提交与交付已经分别独立；调度器保留准入、唤醒确认和退避。
  交付沿原顺序外发、canonical 提交及整封冻结，状态在原抑制位置实时读取；没有新队列或旧入口转发。
  四路真实 TUI 已覆盖返回、停止续做和 Goal 压缩重连；路径和时间戳产物错误按失败留证。
- 同任务新请求的归档先使用绑定后的 canonical run，避免 workspace 与收尾身份冲突而残留 RUNNING。
  request 保持当前消息编号；准备目录失败只结算本次新 attempt，既有 CAS、权限和未知副作用门不变。

- 后台工具策略已从 `runtime.py` 独立到纯计算模块，目录、owner/task 收紧及投递限制语义保持。
- 后台 Goal 状态处理与地址选择分别归 `background_goal.py`、`background_routing.py`；原事务、读取时机和租约顺序保持，删除混合职责 GoalMixin。353 项相关回归通过，新安装版实际 TUI 尚待验。
  现有后台运行、观察、进程测试及独立导入边界共 `201 passed`；真实多 TUI 长任务矩阵已留证，边界见可维护性评估。
- 后续上下文与历史种子拆分保留同一 canonical 历史、任务范围和失败合同；不迁移执行权、锁及投递事务。
  当前候选真实双子代理返回与插话通过，Goal 暂停、压缩、重连后恢复到 8 条唯一记录，平方合计 204。
  同轮发现首次 `/goal` 丢 TUI 工作目录，已沿控制回执与共用校验修复；355 项定向通过。
  新 TUI 的实际 pwd、线程 cwd 和产物一致；启动后替换为空目录外部链接，在创建 Goal 前被拒绝，无外部写入。

## 当前事实源

单 Gateway 管理请求、会话控制、调度与交付；多个 TUI 是独立客户端。owner/thread/task/run/attempt 必须保持显式，展示与扫描索引不拥有执行权。

## 已落地约束

- 真实多 owner TUI 补验发现第二层标识遮蔽仍改写路径中的 owner/request；已让标识投影使用同一
  私有通道事实并保留路径，独立编号继续遮蔽。最终回复/流式/历史与外部通道分别验证。

- 本地队列前台消息按宿主 `cli_chat/cli_gateway` 来源选择私有展示通道；自定义 owner provider
  继续只负责身份，不再导致完整绝对路径被砍成文件名。流式、final、repair 和落账 channel 同源，
  历史回放不二次缩短路径；HTTP/IM 和未知来源保持原脱敏，rich 开关不扩大可见性。

- 停止结果不投递空回复，但保留已经发生的原生工具往返；模型/工具循环抛错也先通过精确会话回调
  提交历史，再返回原错误。空正文异常沿正常 final/repair 去重，结果未知不能假称成功或已压缩。
  前台正常/慢模型及子代理真实 TUI 已验证 5/7/10 组中断前工具往返进入下一轮投影。
  后台静默工作片补齐 native 写入；公开 final/投递重试按独立宿主回合去重，不补造旧版本缺失历史。

- 旧会话未配置模型仅冷却仍会反复报错；已改为 typed 配置依赖等待，当前会话选择可用模型后恢复。
  普通错误保持原 30 秒单调时钟冷却，健康会话不受阻。退避独立、有界、线程安全，原持久事件和 Goal 不变。
  后端构造与恢复检查复用同一缺配置判据；384 项联合定向通过，真实 Goal 菜单选模型后原任务自动完成。
  原有 Goal 错误收口不再消费配置依赖的 wake；其它错误与显式暂停仍保持原边界。

- 主会话后台命令完成通知进入原 wake 队列；owner 硬事实发现覆盖未发布记录，Gateway 重启可补发。
  活动回合消费同一事件，不另起主代理。自然收尾补报共用队列和 claim 准入校验，回执未落盘先等待。
  新版真实 TUI 首轮结束后收到约 91 秒采样退出通知，自动读取日志并公开汇报；配置可关闭。
- 长后台片已接收完子代理结果后，开始时冻结的 child phase 不再抑制 final 或要求无事件的额外轮次；
  当前子树、未读邮箱和 Goal 状态仍生效。定向竞态覆盖，新双子代理 TUI 有最终回复和 completed 记录。

- 客户端等待补修：系统时间前跳导致 TUI 误报等待超时、后台却继续执行。轮询入口将 deadline 转为单调时钟，
  活动续租也使用同一时钟；前跳、回拨、真实无活动超时及队列租约定向验证通过。
  正常模型真实 TUI 注入客户端一小时校时跳变后，七轮任务自然完成并收到唯一终态；未修改主机时钟。

- 新中断语义及后台插话已过真实 TUI：主代理 `/interrupt` 仅中断当前轮，active Goal
  保留原任务身份并使用现有去重 wake；`/stop` 才暂停目标并回收子树。普通聊天不暗中恢复暂停目标。
  新 attempt 与关联变更使用同一转换锁；旧取消结论不能覆盖新一轮，绑定冲突不伪称历史损坏。
  后台普通输入不再只认 processing 请求；精确 running claim 可沿原 guidance/输入回执消费，裸活动链接仍不授予插话权。
  暂停后合法恢复在 attempt 换代事务重开 TaskRun，历史关闭记录保留，避免最终完成仍挂旧 cancelled。
  后台模型输入编号与原 task 一致，回执真实消费后撤下等待提示；先后完成的历史目标不触发冲突迁移。
  六路增量验收发现子代理完成信封把该 Goal task 编号当普通 user request 查询；现先验证 Goal 归属，
  排除不对应用户消息的持久编号，混合普通请求保持严格历史校验。原现场和第二个新目标真实复验已过。
  追加任务的主代理追问又暴露新消息编号误作执行 run；现主入口回填真实 run/attempt，保留消息 ID。
  真实权限门、Compact 再入、旧代次拒绝定向已过；原会话恢复 14 项测试通过，另一次等待两名孩子时
  追问可执行真实命令且孩子继续运行，未放宽权限门。

- 前后台使用同一 canonical 历史，展示摘要的 recent limit 不裁掉模型历史。
- 后台答复先落权威会话记录；模型 provider 名不能冒充消息通道。
- 请求重复、控制 outbox、父级唤醒和交付回执使用稳定身份去重。
- 就绪检查、监听地址与鉴权分开；后台扫描不能阻塞客户端输入线程。
- 模型统计通过原有有序事件和会话活动快照投影；主/子独立保存最近数字，渲染和逐 token 到达都不扫描用量文件。
- 正常回合、持久目标与依赖等待使用不同结构化语义，不因临时沉默自动宣告完成。
- 持续 Goal 的安全续跑不依赖工具次数或 Todo，统一使用去重 wake；每个命名目标有精确任务身份。
  目标回合快照区分兄弟目标，前后台 Store 共用目标时钟。单目标真实完成，多目标最终表现继续复验。
- 未配置模型时 Gateway 仍可承载设置与历史；生成入口返回 `MODEL_NOT_CONFIGURED` 并提示 `/model`，不选择占位或离线模型。`gateway_status` 只投影当前调用方模型，不再向模型展示另一份启动默认模型。

## 未关闭

执行器无结果退出、恢复积压公平性、旧目标冲突与小时级慢模型组合仍需修复或验收，见 [全局状态](../../../STATUS.md)。不要将进程存活等同于每条 runner 存活。

## 修改入口与验证

结构见 [04-structure](04-structure.md)。改生命周期或恢复时，检查运行账本、队列、wake 回执和 TUI 可见终态。最终验收按 [TESTS](../../../TESTS.md) 走真实 TUI；生产用户会话、秘密配置及私有日志不纳入仓库。

首次自动Compact已本地接公共完整请求准备：原会话加载暂缓提交，PromptBuilder冻结后先压缩再自动选模。手动Compact在车道内按全线程来源判空，回执只报告历史估算。组合验收见TESTS，真实供应商与完整IR边界仍待验。

首次恢复宿主只在请求带有会话来源时安装（2026-09-23 修复）：未绑定 thread 的 ask 此前会在 `compact_source` 为空时抛 AttributeError，导致无会话请求全部失败，Gateway 场景测试因此失败。现在与 overflow 入口共用同一判定，没有来源就不安装宿主；入站附件无效的 `INPUT_MEDIA_INVALID` 也已登记到唯一错误合同。

外层typed overflow已接同宿主原生IR carry；真实循环释放未提交插话，Gateway按ID过滤后交下一次完整准备，原ToolCall身份和完整正文保留。恢复权限和模型前缀重新准备；无可压来源显式拒绝。此片隔离联验中，实际安装版与供应商证据仍待补。

<!-- 媒体来源片 3adb61904 的既有记录；不代表当前 Compact 集成已验。 -->
TUI 媒体请求已接通：input_media refs 与 ask 执行选项及幂等指纹同行，worker 在 owner 解析后验证路径/大小，再进入原 native user history。官网 M3 图片、视频与重连续问通过；官网 M2.7 100 请求/50 槽全部完成。详细资源口径见 TESTS。
- 客户端错误文案新增 `COMPACT_VISION_SUMMARY_FAILED`（随图摘要本次失败，下一次压缩自动改走归档引用，不必换模型），先于通用 `COMPACT_` 前缀匹配；见 [媒体压缩策略](../../design/COMPACT_MEDIA_POLICY.md)。
- 2026-09-24 深夜：`my-agent status` 与 `gateway status` 在 Gateway 启动写进 state.json 的 `unidentified_stale_attempts` 大于 0 时出一行提醒并指向 `runtime-stale-attempts`；只读投影，不查库、不结清。同批：预检与 `_automatic_noop` 的上下文估算按已知图块数 × `input_media_token_reserve` 加预留，多图上下文不再被低估（见[媒体压缩策略](../../design/COMPACT_MEDIA_POLICY.md)）。
- 2026-09-25：审批链新增 owner 长期授权——工具 runtime policy 用 `ApprovalPolicy.owner_grant_parameters` 声明"可长期允许的一类操作"，审批请求 binding 带 `grant_key`，面板多出 `approved_owner`；`StreamApproval` 先经宿主提供者核对 owner `operation_grants`（已授权直接 `permission_resolved`，不发面板），用户选长期允许时由 `request_execution` 注入的 `grant_recorder` 写 owner `tool_policy.json`。首个使用方是后台服务 `background_listen_scope=lan`。
- 2026-09-24 深夜：Gateway 停机收尾在模型调用结清之后只读列出 owner 后台会话权威目录里仍未终态的受管进程（`gateway_parts/background_sessions.py`），写事件 `gateway_background_sessions_surviving`（条数 + session_id/status/uptime/command/监听范围事实），收尾后把条数并进 state.json 的 `surviving_background_sessions`；`my-agent status` 在 Gateway 未运行时显示 `background_sessions_after_stop=N`。这些进程按设计跨 Gateway 存活，停止仍走显式控制，不在停机时杀进程；监听地址边界（是否只允许 loopback）待用户拍板，见 DESIGN_LEDGER。
- 2026-09-25：实验授权回执 `request_experiment._receipt` 的可选字段改为显式关键字参数（`authorization_id`、`code`，非空才写入），回执形状不变。原先的 `**fields` 违反架构守卫 `test_product_code_has_no_var_keyword_service_interfaces`，是全仓回归发现的。
- 2026-09-25：插件面板服务 `plugin_display_service` 按配置 `plugin_process_sandbox` 给面板连接的插件客户端带上同一沙箱开关（与业务连接、启用候选一致），默认关时行为不变；见[插件进程 OS 沙箱](../../design/PLUGIN_PROCESS_SANDBOX.md)。
- 2026-09-25：Gateway 状态文件 `gateway_state.json` 与 `/status` 新增结构化 `runtime_prefix`（进程 `sys.prefix`），供 TUI 判断自己是否与 Gateway 同一安装并在空闲时自动重启到同版客户端；见 [TUI 交互规范](../../design/TUI_DESIGN.md)。发布工具切完 Gateway 后会检测并重启同机 IM 适配器守护进程（仓库外 claude-tools）。

## 2026-09-25 能力包调度与长任务（本地组件阶段）

定时任务的 skill_refs 现在保留包类型、包 ID、摘要与激活代次；后台启动前必须全部匹配。
主会话首次读取能力包，在原 ThreadTaskLink.skill_snapshot_refs 固定版本；重读幂等，原锁内并发合并。
新轮验证已用版本并保持发现其他包，升级或重装不会静默替换旧任务；普通临时轮继续使用逐轮快照。
本地引用／调度／会话／能力授予七文件114项组合通过，真实原生 TUI 与发布尚未开始。
详见[能力包 Goal](../../tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

- 2026-09-26：owner 唤醒发现 `owner_wake_discovery._has_pending_memory_curator_work` 的失败退避改为与 Curator 自身同源的 `curator_failure_retry_seconds`。普通失败仍是 300 秒；`CURATOR_MODEL_NOT_CONFIGURED`（owner 没选模型）等一小时，发现层不再按维护周期反复种回登记表、重建 owner 实例（真机：两个未选模型的 owner 每约 7 分钟失败一次）。见 [memory 进度](../memory/02-progress.md)。
- 2026-09-27：`/settings` 与 `/settings all` 的总览新增“配置告警 N 条”：把 `AgentConfig.config_warnings` 与 `CapabilityConfig.config_warnings` 合并列出，每条注明来源（agent 主配置 / capability 配置），N 为 0 时不显示这一行。此前两个来源都没有展示点，参数减量忽略掉的已删/未知键用户看不到。取告警只读字段，不解析消息文字；memory doctor 的 `memory_config_warnings` 出口未动。
- 2026-09-27 后续两项同批：①`settings/services/_normalize.py` 新增 `describe_raw_value(key, value)`，告警回显配置值统一走它——凭据键不回显（写“已隐藏”），其他键截到 80 字符并标“已截断”，覆盖原 6 处 `got {value!r}`；②`_config_warning_lines` 改成先压平两个来源再过滤空项（原实现是 for→for→if 三层嵌套，违反“新代码不超过两层”的项目约定，code-size 报 `nesting:...:_config_warning_lines`）。
- 2026-09-28：凭据类字符串配置补类型校验（T3 真实 TUI 验收发现写错类型时没有任何告警：`embedding_api_key` 写成列表会原样进入运行配置，`feishu_app_secret` 写成列表会被静默变成空串）。
  - `settings/services/_normalize.py` 新增 `CredentialFieldsService`，排在归一服务最前面。
  - 字段名单由 `credential_string_fields(AgentConfig)` 从配置类声明推出：键名是凭据且声明为 `str` 的字段，新增凭据字段自动纳入。
  - 统一口径：
    - 字符串原样保留；
    - 整数沿用“纯数字没加引号也按字符串还原”；
    - `None` 与留空读成的 `[]` 按没填取默认值，不告警；
    - 其它类型告警 `<键>: expected a string, got 已隐藏（凭据不显示原值）; using default`，并回落默认值。
  - `/settings` 总览的“配置告警 N 条”因此会列出这些键，原值不出现。
- 2026-09-28：插件命令执行前被拒（rejected）时，回执在中文说明后另起一行“错误码：X”（如普通用户执行仅管理员子命令得到
  `PLUGIN_PERMISSION_DENIED`），Gateway 普通回执与交互命令流两条路径都按 WARNING 记 `PLUGIN_COMMAND_REJECTED error_code=… action=…
  request_id=…`。此前服务层已有码，但只转成中文说明、TUI 只打印说明、Gateway 不记日志，面板与日志都看不到码（R16 实测）。
  管理员执行行为与执行后的失败/成功文案不变；合同测试见 test_plugin_management、test_gateway_plugin_management、test_host_command_stream。


## gateway stop 列出遗留后台进程，并可显式一并停止（2026-09-28，分支 `my-agent/self-dev-4`）

托管后台进程不随 Gateway 退出而停止（生产部署依赖这一点），但原先既看不到它们、也没有回收入口：
回合被 /interrupt 后后台进程继续运行，用户再 /stop 只会得到"当前没有运行中的内容"。
`gateway stop` 现在默认按结构化事实列出本 gateway 登记且仍在运行的后台进程（所属 root task/run、pid、启动时间、状态），
**默认不停止**；加 `--stop-background` 才走既有资源停止路径（`ProcessSessionStore.request_stop` 冻结停止意图，
原 host 的 `terminate_process_tree` 按进程组回收，孙进程随之结束——只给登记 pid 发信号会漏掉它们），
`--background-timeout` 控制等待确认的秒数。停止失败按非零退出码如实报告，不谎报已停。
重启（`gateway restart`）不隐式停止后台资源，只有用户显式要求才停。本地六项合同单测通过，真机未复验。

## 会话内 /stop 回收被中断任务的后台资源（2026-09-28，分支 `my-agent/self-dev-4`）

回合被 /interrupt 后托管后台进程按设计继续运行，但原先同会话再执行 /stop 只会得到"当前没有运行中的内容"，
用户没有入口回收。现在 `/stop` 在**没有运行中回合**时会再退一步：按本会话精确 thread_id 从受管登记表里
筛出仍在运行的资源并停止。选扩展 `/stop` 而不是新命令的理由是——用户第一反应就是 /stop，新命令要求用户
先知道"有遗留资源"；两条路径（`/stop` 与 `gateway stop --stop-background`）复用同一实现，不新增第二套停止逻辑。
没有登记仍如实回"没有运行中的内容"；登记表不可读或停止未确认时报 `TASK_RESOURCE_STOP_UNCONFIRMED`。
本地四文件通过，真机未复验。

## 派发线程不再被错误打印杀死，心跳与 /status 暴露派发存活（2026-09-28，分支 `claude/38-gateway-dispatcher-resilience`，基于 `c101d325a`）

生产 step15t 从 15:11 启动后一个前台请求都没处理：15:13 磁盘写满时，tick 里某段出错走到 `_print_gateway_loop_error`，
它直接 `print` 到 stderr 没有任何保护，打印本身抛 OSError 逃出 except 块；`_gateway_request_loop` 的 while 外没有 try，
派发线程静默退出，连死因都没能写进日志，之后 pending 一直涨，`/status` 只看得到 pending 数。
- `_print_gateway_loop_error` 现在绝不抛：打印失败只在进程内账本 `gateway_parts/loop_health.py` 记一笔（次数、上下文、错误类型），
  不做 IO。这个入口被所有后台循环共用，心跳、后台主循环的错误打印同样受益。
- 派发循环每拍走 `_guarded_dispatch_tick`：从 tick 逃逸的任何 Exception 记账本、打印，按"本轮没派发"继续；派发者构造/关闭失败与
  BaseException 逃逸经 `_record_request_loop_exit` 留下退出事实（账本 + 打印 + `gateway_request_loop_exited` 事件），
  Exception 按原语义吞掉返回，BaseException 原样上抛。
- 心跳载荷与 `/status` 响应并入账本快照：`dispatcher_alive`、`dispatcher_state`（not_started/running/vanished/exited）、
  `last_dispatch_tick_at`、`last_dispatch_tick_started_at`、`dispatch_tick_count`/`dispatch_tick_errors`、`dispatcher_exit_error`、
  `loop_error_print_failures` 等。`/status` 直接读内存，磁盘写满时也能看出"有 pending 但派发已死或卡住"。
- 扫描门 `GatewayInboxScanGate` 改在扫描**之前**取样 inbox mtime，并以它为"已见过"的基线；扫描期间目录再变就强制下一轮重扫。
  原来在扫描结束时取样：请求先写 .tmp 再 rename，rename 落在 glob 之后、record_scan 之前时被记成已见过，负载高、一轮超过
  2 秒粗粒度保护窗口时该文件被永久跳过。
- 合同测试 `test_gateway_dispatcher_resilience.py`：注入 ENOSPC 的 stderr、段内/段外异常、卡住后抛 SystemExit 的 tick、glob 之后的
  rename，全部确定性构造。真机未复验，部署后在生产心跳里核对这些字段。
- 同一事故的第二个受害者是飞书适配器：`cli/adapter.py` 每 5 秒写一次 `adapter_state.json`，15:13 写入撞 ENOSPC，异常没人接，
  进程整个退出，状态文件却还写着 running。现在周期写入走 `_write_adapter_state_guarded`：OSError 只记进程内账（次数、最近错误）并打
  WARNING，下一轮重试，恢复后把 `state_write_failures`/`last_state_write_error` 写进状态文件；状态文件改为原子写，失败不留半截 JSON。
  启动/等待阶段任何异常逃出都先 `_finish_adapter_process`（停适配器 → 删 pid 文件 → 状态写 failed 带 reason/error）再上抛；
  状态写不进去时 pid 文件已经没了（删文件不占空间），另记 `adapter_state_write_failed_at_exit` ERROR 日志。
- `/status` 新增适配器事实（`channel_health.adapter_process_facts`）：`adapter_alive` 只按 `adapter.pid` 记录对应的进程是否真活着
  判定（含启动指纹防 PID 复用），状态文件只给 `adapter_state`/`adapter_state_updated_at`/`adapter_state_stale`；读取失败单列
  `adapter_state_error`/`adapter_pid_error`，只读、不清理陈旧 pid 文件。合同测试 `test_adapter_state_write_resilience.py`。
- 9a 复审跟进（同分支第二个提交）：tick 持续出错时原来每 0.2 秒打一条 `[gateway-loop-error]`（stdout 追加进 paths.log，一天两百多 MB），
  正好能把刚腾出的磁盘再写满。派发循环与后台主循环共用 `cli/gateway_loop_backoff.LoopErrorBackoff`：连续出错等
  min(0.2·2^k, 30) 秒、成功一次清零；同种错误（context + 异常类型）只打第 1 次、之后每 10 次打 1 次，次数仍全部记进
  `dispatch_tick_errors`。其余：`dispatcher_alive` 改为保存线程对象用 `is_alive()`（ident 在 macOS 上会立即复用）；
  `dispatcher.shutdown()` 抛错记成 `gateway_request_pool.shutdown` 阶段；`/status` 的 `adapter_pid_error`/`adapter_state_error`
  改为结构化子集（category、context，不带路径）；账本与适配器状态里的错误消息先过 `redact_sensitive_text`。
  9a 建议的 hdiutil 小磁盘镜像写满真机复现留作后续验收。

## 后台循环退避覆盖到全部循环，包装异常按原因链归类（2026-09-29，分支 `claude/38-loop-backoff-everywhere`，基于 `64f7ee64e`）

ENOSPC 真机验收（releases/step16e-50651d81/enospc-acceptance/）里 scheduler_due 的 tick 在磁盘写满时打了一条 `SchedulerDueIndexError`
且被归为 programmer_bug；盘点发现 `LoopErrorBackoff` 只接了派发循环与后台主循环。本轮：
- **盘点**（见 04-structure 的表）：维护循环、调度器到期循环、孤儿恢复循环改走 `_run_loop_with_backoff`（成功等 interval 并清零退避，
  出错记账、限流打印、等 `max(interval, 退避)`——退避只能放慢，不能把 60 秒一次的维护变成 0.2 秒重试）；心跳循环记账、限流但**不退避**
  （节奏本身就是限流，退避只会把"磁盘腾出后重新被看见"推迟到 30 秒）；派发 tick 的四个段（派发/恢复/终态投影/输入回执调和）的段内错误
  也进同一份账本与限流，但只有派发段失败（没请求可派时）让循环退避；恢复/终态投影/输入回执调和三个后台修复段各自一份退避，失败只推迟
  自己的下次到期（`max(0.75, 该段退避)`、`_RecoverThrottle.defer`），派发轮询保持 0.2 秒——9a 复审指出一张坏回执不能让新消息等 30 秒才被认领。请求租约心跳、supervisor 进程、后台主循环内的 owner 车道不接（各有自己的
  停机/冷却/一次性语义）。
- **账本**：`loop_health.note_loop_error(loop, context, exc)` 按 loop id 计数，快照新增 `loop_error_counts`、`last_loop_errors`；
  `dispatch_tick_errors`/`last_dispatch_tick_error` 现在只投影派发循环（9a 之前指出的"后台错误混进派发计数"就此拆开）。
- **归因**：`runtime_error_report` 对不认识的包装异常只沿显式 `__cause__` 找环境类根因（`OSError`、`sqlite3.OperationalError`），
  **不改 category**（仍是 programmer_bug，因为 category 参与取消工具等控制流），另加 `cause_type`/`cause_category` 两个字段，账本记录
  （`loop_health._error_record`）同样带上；只看类型，不看消息文本，不特判 errno；不沿 `__context__`（except/finally 里顺带发生的
  KeyError/AttributeError 会误归环境）。`SchedulerDueIndexError` 本来就 `raise ... from exc`，没改 scheduler。顺手把
  `cancel._explicit_target_is_absent` 收紧到它注释里的契约：只认 `error_type == "FileNotFoundError"`（原来 `category == "io"` 会把
  PermissionError、被包装的 sqlite 锁定判成"run 不存在"）。
- 合同测试 `test_gateway_loop_backoff_coverage.py`（9 条，确定性构造，含"库文件路径是目录"的真实 sqlite 环境错误）。真机未复验。

## 插话幂等回执指纹兼容旧口径，坏账回执不再被静默重写（2026-09-29，分支 `claude/38-guidance-receipt-digest-compat`，基于 `bac2f176d`）

生产实锤（ae 核对 step16g 时发现）：input_receipts 里一条 09-27 发给已结束定时 run 的插话回执 state=terminal_unknown，
reconcile_error 是 DataCorruptionError「input digest mismatch」，每 15 秒被 reconcile_gateway_input_receipts 重写一次，不进任何计数。
根因：a646a4885 在 `_guidance_input_digest` 里加了 `canonical_metadata.pop("expected_turn_id")`，改了已落盘指纹的口径却没兼容旧回执；
插话的元数据一定带 expected_turn_id，所以之前写的幂等回执被 `_validate_guidance_once_receipt` 重算时全部对不上。
- **指纹分版本**：`GUIDANCE_INPUT_DIGEST_VERSION = 2`；`_guidance_input_digest(request, version=…)` 保留 v1（只去 dedupe_key）与 v2
  （再去 expected_turn_id）两种计算；回执新增 `input_digest_version`（新写的记 2）。校验统一走 `guidance_receipt_input_matches`：记了版本
  按版本严格重算；没记版本的旧回执（a646a4885 前后各写过一种口径）两种任一匹配即视为同一条。`append_once` 的同键重试也走它。
- **对账**：`_write_terminal_unknown` 在状态已是 terminal_unknown 且错误的结构化字段（error_type/category/context）没变时不重写；
  带对账错误的终态未知计入 summary 的 `terminal_unknown_errors` 并记 `last_terminal_unknown_error`，派发者的对账段经
  `raise_if_input_reconcile_unsettled` 抛 `GatewayInputReconcileUnsettledError`（category 取持久化错误的类别），进 loop_health 计数与限流打印，
  只推迟对账段自己的下次到期。
- **收敛**：修好部署后那条回执不用手工改：旧口径校验通过 → 目标任务已终态 → guidance 被拒绝 → 回执转排队并进 inbox（既有语义；
  生产上那条 09-27 的旧回执由集成者在部署前挪到备份目录，不让两天前的指令迟到送达，见 DESIGN_LEDGER 同日裁定）。`test_guidance_receipt_digest_compat.py` 用冻结的旧算法写真实形状回执证明这一点。
- **规矩**：持久化指纹改口径必须加版本号并保留旧版本的计算，不能原地改；固定样本的指纹十六进制钉在测试里。

## 会话互通取消：停止后台派活回合 + 消息唤醒不再空转（2026-09-28，分支 `my-agent/self-dev-3`）

`control_service.execute_gateway_conversation_control` 的 `stop` 分派新增一条后台回合停止路径：
前台窗口请求集合里找不到精确 `expected_turn_id` 时，改按**会话任务的绑定**去后台定位这一轮
（`task.conversation_request_id == expected_turn_id` **且** 目标会话后台认领仍在 `running`），
再打这一片自己注册的**专用可中断名** `session-task-turn:{turn_id}`（与前台 `conversation-request:` 不同空间）；
拿不到绑定、认领已不在跑，或有其它来源占着这个回合，都如实返回未确认（fail closed），不改任务状态、不伪造成功。

为什么要专用名：派活回合跑在后台片里，`task_id` 为空，原逻辑在 `run_claimed` 里退化成 `nullcontext()`
——**这一轮从来没注册过可中断名**，取消永远打不到东西。`task_id` 在 Skill 快照绑定/持久任务处有既定语义，
不得挪用（实测挪用会让整片因 `SKILL_TASK_BINDING_INVALID` 开不出来）。

同时补两处交付前的取消判定：`background_claim.run_with_heartbeat` 在 `run_once` 返回后再查一次本线程中断旗，把认领结算成 cancelled；
`runtime._run_agent` 在交付前按**持久的结构化事实**（该会话任务是否已 `cancelled`）丢弃答复——这一处**不能用本线程的 `is_interrupted()`**：
它是 per-thread 的，停止旗不在本线程时读不到。交付前的持久检查是**纵深防线**：常规停法有两道在它之前——
①停止旗立到本线程后，在途模型调用在等待关卡里看到旗就丢掉结果并抛中断
（`tool_model_generation._wait_for_generation_result`）；②`run_with_heartbeat` 的二次检查把认领结算成 cancelled。
（注：早先注释里的"嵌套注册退出会清旗"说法**不成立**，集成方用 60 段轨迹否掉了它。）
`run_with_heartbeat` 的二次检查只对**绑定了会话任务**的回合生效（`_host_delivery_bound_turn` 非空）：
普通后台运行能被普通 `/stop` 打到，对它套这道检查会丢掉已交付的答复、认领记成 cancelled。

消息唤醒：本片带上会话身份（回合号取唤醒自己的 `wake_signal_id`），消息在唤醒回合里被认领、确认，不会到下一轮再注入一遍；
目标忙时消息已在它自己的前台回合里被消费（回执为 `consumed`）的，领取后准入 `session_message_consumed` 不开回合，
并经 `retire_source` 把唤醒结案（2026-09-29 接手收尾时补上：原实现只结 claim，唤醒每拍被重新认领）；
`submitted`/`rejected`/`reserved` 都不算已消费，照常开回合把消息交给目标（ae 在真实链路里复现了被 /stop 后 `rejected` 的消息）。

回归：`test_session_task_real_chain.py` 的 `HOLD-FIRST` / `HOLD-AFTER-TOOL` 两个窗口，以及忙碌目标消费后断言唤醒已结案；
另加 `test_background_claim_interrupt_scope.py`（绑定回合 vs 未绑定普通后台的正向对照）、
`test_session_message_consumed_admission.py`（逐状态判据、读取抛异常放行、只在已消费时结案）。
2026-09-29 由 75 从 `my-agent/self-dev-3-cancel-final` 接手，压成干净提交落在分支 `claude/75-cancel-line-finish`。真机复验由集成方跟进。

## 环境级故障按车道暂停（2026-09-29，分支 `claude/9b-lane-env-pause`，基于 `89af6b07a`，唤醒毒丸第 3 步 C5）

- **一个权威**：`backends/errors.is_provider_environment_fault`：整数 HTTP 状态 401/402/403/404/407，或除 `ProviderRequestRejectedError`
  以外的 `ProviderConfigurationError`。状态码集合从 `wake_poison` 搬来，毒丸 `_is_uncounted` 改为引用它，判定逐字不变。
- **车道第三类**：`BackgroundLaneRetry` 增加 waiting_for_environment。分类顺序固定：先等模型配置（`ModelNotConfiguredError` 也命中环境判定，
  必须先走这条，配好模型立即恢复），再环境暂停，其余仍按 30 秒冷却（400/413 等请求本身的问题不变）。暂停到两件事之一：
  - `thread_model_fingerprint` 变了：暂停后第一次就绪检查记下基线，之后每次规划比较，变了立即放行并删掉暂停（之后再失败从 60 秒重新计）；
    失败到下一次规划之间发生的改动不算变化，只能等探测放行（最长 60 秒）。
  - 到了探测时刻：`LANE_ENVIRONMENT_PROBE_BASE_SECONDS = 60` 起，探测再失败翻倍，`LANE_ENVIRONMENT_PROBE_MAX_SECONDS = 900` 封顶（内部常量）；
    到点放行一次真实尝试，暂停记录留到探测有结果，成功由 `succeeded` 清除。
  指纹在锁外读，锁内只推进同一条记录（读指纹期间被成功清除或换成新失败，就不写回旧记录）。取指纹出错（OSError、DataCorruptionError、
  ModelProfileError 等任何 Exception）按未知处理，只按探测时刻放行，暂停和翻倍保持，不再经 Gateway 记成新失败把暂停冲掉
  （9a 复审建议 1，C5 补充三）。状态只在进程内，重启即清。
- **日志**：`[gateway-lane-retry]` 一行 JSON：`lane_environment_paused`（owner 标签、thread_id、`probe_in_seconds`、异常类名、状态码）与
  `lane_environment_resumed`（reason 为 `model_fingerprint_changed` 或 `probe_succeeded`）；不含异常正文或配置，打印失败只记 `loop_health`。
- **指纹**：`thread_model_fingerprint(agent, thread_id)` 直接 `threads.load`，不经会给空引用迁移写库的 `thread_model_profile_id`；由
  model_profile_id、model_selection_revision 和所选模型的连接字段（协议、模型名、接口地址、密钥、密钥环境变量名、请求头、会话头、登录引用）
  组成，用 `decision_policy.connection_revision` 的进程盐 HMAC 摘要，不落盘、不打印、不构造后端、不联网。空引用按 owner 默认推算；
  模型引用失效（删除、停用、撤销共享）记作不可用，指纹随之变化。改 owner 的新会话默认值不影响已选模型的旧会话。
- **额度耗尽核对（结论，没改代码）**：`ProviderQuotaExhaustedError` 不会被两套进程内机制重复处理。供应退避
  `_absorb_provider_supply_failure` 只吸收 `is_provider_transient_error`（含 `ProviderUsageLimitError`），额度耗尽原样抛出、不记账；
  唤醒路径由 `_run_wake_signal` 就地转成额度通知（Goal 转 usage_limited，未送达的只重投通知、不再调模型），tick 正常返回，车道记成功；
  观察路径抛到 `_safe_thread_tick`，车道按 30 秒普通冷却（不是环境暂停）；策略路径先在 `run_with_heartbeat` 记持久策略失败账（300 秒起、
  连续 3 次退休），再抛到车道 30 秒冷却——这是持久策略账与进程内车道两层，不是供应退避加车道冷却。
  跟 9a 改判相邻的影响：每周额度用完的 429 以前是 `ProviderUsageLimitError`，三条路径都走供应退避（30→900 秒）、策略不记失败；改判成额度耗尽后，
  唤醒路径改走额度通知（预期），观察路径变成车道每 30 秒固定重试一次注定失败的模型请求（直到额度重置），策略路径开始记失败账、3 次后退休。
  **3a 裁定（2026-09-29）并入车道暂停**：车道分类改为 `is_provider_environment_fault(exc) or is_provider_quota_exhausted_error(exc)`，
  额度只在车道这层显式加，共享判定 `is_provider_environment_fault` 不变（毒丸那边额度本来就按瞬时类不计数）。观察和策略路径遇到额度用完
  也按车道暂停（60→900 秒探测，换模型立即放行），不再每 30 秒空打一次；唤醒路径仍就地转额度通知，不受影响。
  **额度共用判定（2026-09-29，9a 复审 be 的压缩额度修复后补充，分支 `claude/be-quota-predicate`）**：大线程请求前先压缩，压缩调用撞额度时
  抛的是 `ConversationCompactError`（`COMPACT_PROVIDER_QUOTA_EXHAUSTED`），车道分类、策略失败账和毒丸原来都用 isinstance，认不出。
  现在四处（含唤醒额度分路）都只读 `conversation/compact_guard.is_provider_quota_failure`；后端层的 isinstance 判定不变。
- **策略失败账不记环境级故障（3a 裁定，C5 补充二）**：现状核实（草稿探针）——401/407/连接失败/额度用完都原样穿过真实后台回合，
  每次记进持久策略失败账（`failure_count` 加 1、退避约 300 秒），连续 3 次策略退休。改为 `background_claim._counts_as_policy_failure`：
  供应瞬时、配置暂缺、环境级故障与额度用完都不记，不因此退休；重试节奏交给车道暂停。普通程序错误 3 次退休不变。只改记账条件，
  不动持久账 schema。
- 测试与变异见 TESTS.md 同名节。

会话消息去重键按单条消息区分（分支 `claude/75-session-message-dedupe`）：键 = 会话对 + 这次发送的身份（模型工具的 `__operation_id`、
`/tell` 每次新生成），唤醒 metadata 的 `message_dedupe_key` 带上它，"已消费"判据按它查回执；同一对会话连发多条各自入队、各自送达，
工具结果返回回执真实状态。

会话消息在目标回合没消费就结束（/stop、报错、崩溃）时释放给下一回合（方案 A）：`reject_pending` 的唯一收尾规则
`settle_unconsumed_receipt` 对会话消息改为释放回 pending 并记 `released_turn_ids`，下一回合认领时改绑到自己名下；
释放满 5 次后转 rejected（`SESSION_MESSAGE_RELEASE_LIMIT_REACHED`），领取后准入按来源已处理完结案唤醒；submitted、插话、派活正文行为不变。

后台片没有正常结束时同样收尾（`runtime._settle_unconsumed_background_turn_input`）：定时 run、派活、会话消息唤醒这三种自带精确
回合号的片，模型调用失败、取消或中断时对本片回合号 `reject_pending(reject_reserved=True)`，Compact 公平让出除外；此前消息唤醒
回合认领后失败，消息卡在 reserved。释放改为按次计数（回执 `migration.release_count`），同一条唤醒重跑（同一回合号）反复失败也会
到上限。（更正：123f6f3b4 另写的「同一回合重新认领时补回回合索引」不需要，读回执时的投影修复会补回，下一提交已还原。）

派活正文在后台片非取消的失败后退回：后台收尾按结构化任务状态传 `release_task_body`，任务没取消时派活正文退回 pending、
按同一个 `release_count` 计次，不授权跨回合，只有同一 `session_task_id` 的重跑能认领；任务已取消照旧 rejected，前台终态不变。
取消路径同步撤回：先失败、后取消时正文已退回 pending，`_apply_cancel` 推进到 cancelled 后把它撤成 rejected，重试不再注入已取消任务的正文。
已取消派活任务的唤醒在开跑前结案（`_retire_cancelled_task_wake`），重试等待中被取消的任务不再重跑一片。
开跑前判定按唤醒自带的 `session_task_id` 直接读任务状态（R-a）：排队中就取消、从未绑定回合的任务也在开跑前结案，不再开一个
没有正文的空派活回合、向目标会话交付答复。
派活正文只许它自己的派活回合认领（R-b）：目标正忙时前台回合不再在安全点领走派活正文，任务留在队列，前台结束后由派活唤醒开
派活回合、收口并回报发送方；派活回报（带 `SESSION_TASK_STATUS_FIELD`）不受影响。

释放只计会让回合崩溃的失败（3a 裁定 (a)）：`reject_pending(failure=...)` 按唤醒毒丸的 `verdict_for_error` 判定，超时、429、连接、
环境故障、/stop 与取消只释放不计次；前台 `_handle_gateway_request` 的收尾与后台片收尾都把回合抛出的异常交进来。


唤醒毒丸第 3 步 C2/C3 接线（分支 `claude/75-wake-poison-wiring`，step16m）：`run_claimed` 支持领取观察者（`begin`/`settled`），
唤醒车道按尝试写尝试账：计数失败按 30/60/120/240 秒持久退避、同因 5 次按 `failed_permanently` 结案（之后不再领取、换进程也生效），
瞬时/环境类不计数只退避；批次里有带账成员时逐条执行；进程死亡的在途尝试在跳过阶段补记；尝试账读不出按 `attempt:ledger_corrupt`
结案。在途记录带这一片的回合号，供 C6 收尾死进程认领的补充消息。

唤醒毒丸第 3 步 C4（同分支）：结案后派活任务收成 failed（`SESSION_TASK_WAKE_QUARANTINED`）并回报发送方；会话消息回执不动，只留注明
仍待投递的宿主提示（2026-09-29 3a 以做法 2 取代原裁定 b，消息去留只由回执层上限决定）；派活正文已放弃时准入先收任务再结案唤醒；
宿主提示按原因码合并。

派活回合的取消判定统一按任务号读（窄窗口，step16m）：开跑前、交付前、后台收尾用同一个判据，开跑后、第一次认领正文之前到的
取消也会在交付前丢弃答复、结案唤醒（那一次没有正文的模型调用是已知代价）；不再全量扫描会话任务。

## 维护状态说清「执行了没有」：只加字段、不改旧口径（2026-09-29，分支 `claude/9b-maintenance-apply-outcome`，基于 step16l `5ec2db2e0`）

- **起因**：step16i 首跑积压时，local/main 其实执行了 11742 个动作、失败 0。但 `maintenance.json` 记的是 `policy_unavailable`、
  `last_success_at` 为空，Gateway 摘要也把它算成 `failed`；同一轮的审计事件却写 `ok=true`、`errors=[]`。
  - 前两者的原因：R4 之后路径级错误只隔离重叠动作，而 `_maintenance_status` 只要有错误就记 `policy_unavailable`。
  - 审计的原因：它在 `execute_retention_plan` 里只看到隔离后剩下的可执行部分。
- **修法**（3a 裁定，持久化字段只加不改）：
  - `MemoryRetentionReport.isolated_errors`（末尾、带默认值）：`apply()` 用 `replace(_without_errored_subtrees(plan), isolated_errors=plan.errors)`
    把扫描期路径级错误随可执行计划带进执行器，合并回执时原样带出；整份拒绝、法律保留时为空。
  - 审计事件 `owner_retention_applied` 新增 `isolated_error_count` 与 `isolated_error_codes`（按码计数，不含路径）。
  - `maintenance.json` 新增：
    - `apply_outcome`（`applied` / `refused` / `legal_hold`，由 `_apply_outcome` 只看 `report.applied` 与 `legal_hold` 推出）；
    - `isolated_error_count`；
    - `last_applied_at`（最近一次 `applied`，被拒时沿用上次，旧文件按 0）。
    - `status` / `last_success_at` 不变。
  - `OwnerMaintenanceResult` 新增 `apply_outcome` / `isolated_error_count`，`not_due` 时 `to_dict` 与旧版逐字相同。
  - Gateway 摘要：`failed` 改为只算整次被拒与执行期动作失败，「执行了、只有隔离错误」不算（3a 据 9a 对 355 条的诊断补充裁定；
    摘要只打印、没有持久化读取方）；另加 `refused` 与 `isolated`。`OwnerMaintenanceResult` 同步带出 `failed_action_count`。
- **不改**：/status、TUI 不读维护状态，展示维护状况属于新功能，记台账待定。
- **计数口径**：`isolated_error_count` 是错误条数。解析不了（坏 JSON）的 `state.json` 会被 completed_task 与 tool_output 两个扫描器
  各报一次（既有行为），这时不等于子树数，按 (错误码, 路径) 去重待定；生产 local/main 那 355 条是能解析、只缺 status 的旧格式，
  只由 completed_task 报一次、路径不重复，上线后预期 `isolated_error_count` 约为 355。
- **同轮处理的两个 v1 策略 owner**（3a 裁定）：`93b8c3ffffb8`（local/users，测试名）用产品 `_migrate_retention_policy` 一次性迁移，
  原文件备份进证据目录；`ebd40e6fc3ec`（feishu/users，像真实用户）不动、交用户决定，状态修复上线后它每天会记为 `refused`。
- 38 复审跟进（同分支补充提交，纯抽取、语义不变）：代码尺寸身份比对新增了 3 条（`execute_retention_plan` 越 soft 线，
  `run_owner_retention_if_due` 与维护 `tick` 进 high-risk），分别抽出：
  - `retention_apply._apply_candidate_phase`；
  - `owner_maintenance._outcome_fields(retention, previous, current)`，算 status / last_success_at / 动作计数 / 三个新键；
  - `gateway_loops._maintenance_summary(reports, scanned)`。

  抽取后对比基线 `5ec2db2e0` 的身份比对：新增 0、消失 0。
- 测试与变异见 TESTS.md 同名节。

- 2026-09-29 唤醒毒丸第 4 步运维面（分支 `claude/be-wake-ops`，待复审）：管理员 `/wakes`、`/wakes replay <ID> [confirm]`（TUI 与飞书共用 Gateway 控制入口），`/status` 与 `gateway_status` 显示已结案唤醒条数，账本整理把满 14 天的结案留档移进 `quarantine/archive/`（发布语义不变，重放拒绝 `WAKE_REPLAY_ARCHIVED`）。测试与变异见 TESTS.md 同名节。
