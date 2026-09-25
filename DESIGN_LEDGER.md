# 设计台账

决策分支已在本地吸收 main `66a598cf3`，尚未合入 main，也未部署：
- Compact 工具来源统一为 v3 checkpoint 加 run/attempt/turn/call 四元身份，主线三元 `tooling/call_ref.py` 已删除。
- 未知来源保持可见，并返回结构化的 uncertain 结果。
- 模型轮通过 `ModelTurnRequest`/`ToolLoopRunResult` 显式交回实际采用的参数。

兼容后果：`66a598cf3` 写出的 v2 行按 legacy 读取。合并版部署前做过运行中压缩的旧调用会重新进入模型上下文，只多占上下文，不丢失、不误隐藏。

回滚边界：旧版运行时读不了 v3，回滚必须把运行时和数据成对核对并保留新账。详见[依赖拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

决策设置的宿主非阻塞读取改为无锁读取已提交版本（已合入 main `d69f30cf3` 并部署双机）：原先读取也拿排它锁，同一 owner 的并发决策互相挤成 `settings_busy` 静默回退；两份设置文件都是原子替换写、单次写事务只改一个文件，旧建议仍由调用前后版本复核与在途取消挡住。同一分支用本机 HTTP 故障矩阵钉住断网/DNS/TLS/额度/计费/5xx/慢响应的冷却与恢复。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#42-已实现的可选服务边界)。

- **宿主关闭时主动取消在途决策，及 Curator 与插件点并发组合（P4-F）**（2026-09-25，已实施：分支 `claude/decision-shutdown-cancel`，待合入；主线 owner 已同意在 `cli/gateway_process.py` 加一行）：
  - **关闭取消**：`decision_policy.cancel_active_decisions_for_shutdown()` 复用设置撤销那张进程内在途索引。等待中的调用立即回原方案（`stale/host_shutdown`），不冒充用户停止、不进冷却，调用账记 `DECISION_CANCELLED`；关闭后不再登记新决策。Gateway 收尾在置位停止事件后调用它，出错只记异常类型。
  - **并发组合**：后台 `curator` 慢响应不拖住前台 `skill_tool`；线程变更只撤销前台，owner 级改 `curator` 只提前撤销后台。
  - **两处已知取舍**（不改，登记在此）：采用前复核按整份策略版本判断，所以 owner 级任何设置改动会让同 owner 其他点的在途建议返回后作废为 `policy_changed`；冷却按连接共享，后台超时会让同连接的前台点在冷却期直接保留原方案。两者都只会少一条建议，不会采用过期建议或卡住。
  - 停止时仍未结束的模型调用已由主线 owner 结清为结构化"被中断"（`db4d46398`，已部署）：排空窗口之后，Gateway 进程账本里的在途调用记为 failed / `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`，用量保持未报告、不补零；runner worker 的账本另行跟进。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)第 4.2 节。
- **决策实验自动晋升后没有主动提示**（2026-09-25，F1 正向晋升真实样本发现；未实施）：授权内晋升把线程的 `points.skill_tool.mode` 由 off 改为 apply，但 TUI 当轮没有任何提示，用户只能在决策设置里看到线程覆盖。方向：晋升回执已是结构化的 `gateway_decision_experiment_promotion.v1`（带前后版本），由 Gateway 终态响应带出、TUI 按结构化字段展示一行提示，不从文字判断。
- **自学习的 lesson 来源在当前产品里是死路**（2026-09-25，真实验收发现；已由 `record_lesson` 修复并做端到端真实验收，已合入 main `52e0190e1`）：
  - 子代理提示要求"像普通协作者一样回复、不输出状态 JSON"，`output.json` 由宿主生成，没有结构化通道填 `lessons`。所以真实子代理即使在回复里写了经验，也不会产生 `subagent_lesson` 候选，S1 提案与 S2 排序都无法触发。
  - 宿主不能从自然语言回复抽取经验。修复为仿照 `record_finding` 的独立结构化工具 `record_lesson`，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 15 节。
- **自学习 lesson 的结构化来源 `record_lesson`（第 15 项 P5-C，S1/S2 的上游）**（2026-09-25，已实施并真实验收，已合入 main `52e0190e1`；端到端真实验收见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)「15 自学习 S1/S2 端到端真实验收」一节）：
  - **工具**：子代理专属，参数只有 `title`/`when_to_use`/`procedure`/`applies_to` 四个必填字段（上限 120/500/1000/200 字，Schema 禁止额外字段）。run/attempt 取当前 runner 上下文，task 取该 run 的任务记录，模型不能自报身份。写本 run 工作区的 `lessons.jsonl`：与 `findings.jsonl` 同目录，路径登记在 `AgentRunWorkspacePaths.lessons_jsonl` 与 `SubAgentTask.agent_run_lessons_jsonl`。
  - **账本规则**：沿用 record_finding 的账本口径，锁内先读再判再写，有坏行就拒绝追加；另加 O_NOFOLLOW，防止借符号链接写到工作区外。id 取四字段内容 hash，同 run 相同参数只记一次，返回 `already_recorded`。每 run 最多 5 条、16 KiB，超限返回结构化拒绝（`LESSON_LIMIT_REACHED`/`LESSON_LEDGER_BYTES_EXCEEDED`，`effect_outcome=not_started`），不静默丢弃。拒绝码借用已登记的 `TOOL_GUARDRAIL_DENIED`/`TOOL_INVALID_ARGUMENTS`，因为错误分类表不归本线维护。
  - **暴露**：与 `capability_request` 同一路径。注册表默认隐藏，主线程工具面、tool_search、list_tools 都看不到；直属子代理的 coding/read_only 预设、角色默认工具和层级调度缺省候选都带上它。主线程没有 child run，调用返回 `TOOL_UNAVAILABLE`。要关闭，在 owner 工具策略 `disabled_tools` 里加 `record_lesson`，子代理的 allowed_tools 和提示行会随之去掉；不另加配置项。
  - **结果收口**：`runner_result_service` 在提取阶段读回账本，逐行复核版本、字段、id 与 run 归属，最多采用 5 条。之后与结构化输出的 `lessons` 去重合并（结构化在前），写进 `output.json` 和 runner result 的 `lessons`/`lesson_count`。自然回复没有结构化输出时，账本经验也会记成 `subagent_lesson` 候选：正文用固定四行模板，`applies_when` 取 `when_to_use`，证据引用 `lessons.jsonl#<lesson_id>` 并带 task/run/attempt。
  - **下游**：`enable_self_learning` 开启时，S1 照原链为每条经验生成一个待确认提案。重放不重复：候选靠 observation_id，提案靠 O_EXCL。候选记录失败只写工作日志，不阻断结果交付。
  - **提示**：Runner Contract 只在授权含 `record_lesson` 时多一条可选软引导，不恢复任何状态 JSON 要求；宿主从不解析回复正文。
  - **留给后续**：主线程的经验记录；被取消 run 账本的收取（账本保留，但取消路径不经结果收口）；S1 草稿 `when_to_use` 仍写"来源任务目标："，而账本候选的场景其实是 `when_to_use`。证据见 [TESTS](TESTS.md) 顶部本节。
- **插件层"观察候选"结构已实施（main `c577ed185`，已部署 `runtime-step11d-16b108bd`；manifest v5、代理结果路径、runtime_events 新鲜度、browser-lite）；决策线 `action_candidate` 点已接入并真实验收（2026-09-25，本机隔离 owner 上用 browser-lite 做 off/observe/apply/过期四档，随分支 `claude/decision-action-candidate` 合入，基于 main `6a50d84aa`）**（原稿 2026-09-25，第 15 项剩余点，依据[动作候选审计](docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)；完整设计稿见[插件观察候选结构](docs/design/PLUGIN_OBSERVATION_CANDIDATES.md)；决策侧见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-动作候选-action_candidate)）：
  - **设计稿要点**：manifest v5 在只读工具上声明 `observation`、在动作工具上声明 `observation_ref`；插件在 `structuredContent.my_agent_observation` 给出目标、代次与有限候选；宿主整份校验后铸 `observation_id`/`candidate_id`，写进该次调用原归档的 `tool_result_envelope.observation`（唯一权威），并在模型可见投影里改写为带 candidate_id、隐去插件 key 的有界候选；新鲜度按 `tool_operations` 调用序，run/task 内的后台续跑共享观察；动作执行前宿主按调用序查新鲜度、经 `_meta` 附代次与 key，插件再按页面代次复核；决策点 `action_candidate` 只选一个别名并追加软提示。
  - **现状**：插件线已合入 browser-lite 与 desktop-lite。browser-lite 的 `read` 会返回有限元素清单（标签、文字、name/id、是否可见），`click`/`fill` 按唯一匹配的选择器执行；但宿主没有经过验证的 `observation_id`/`candidate_id`，也没有观察内容哈希与代次。
  - **原则**：不能为 browser-lite 写专项解析，这会违反禁止专项合同的铁律；也不能用截图坐标、自由文本或工具名推荐冒充动作候选。
  - **方向**：
    - 在插件 SDK 的工具结果合同里增加可选的通用观察候选字段（插件声明哪个只读工具会产出候选），宿主校验形状后生成本地 `observation_id`（绑定 run/task/调用、结果哈希与代次）和有限的 `candidate_id`。
    - 决策点只从这些 ID 里选一个、给软提示；真正执行仍由原工具按原审批执行。
    - 执行前按页面或窗口代次复核候选是否仍然有效，失效即丢弃。
    - 这需要主线 owner（插件线）先确认 SDK 字段，再由决策线接入点。
  - **真实验收的发现**（2026-09-25）：
    - 已修复：只读工具的归档没有 `runtime_gate`，`persist_tool_runtime_ledger` 原先因此提前返回，`tool_completed` 事件一条不写，观察新鲜度恒为 False。插件线已在 main `6a50d84aa` 改为完成事件总写，决策侧集成测试也改走这个真实写入口。
    - 插件线已处理（2026-09-24 深夜，详见[插件观察候选结构](docs/design/PLUGIN_OBSERVATION_CANDIDATES.md)第 6 节）：三处宿主行为都是既有设计，只改了 browser-lite 工具描述与 README（写路径不写 `file://`、相对路径按会话工作区根、http(s) 需插件与宿主两道门）。显式 `file://` 的正确入口是将来 manifest 通用的 `local_file_url_parameters` 声明，不为单个插件放松 URL 门。
    - 主线评估（2026-09-24 深夜，已核对源码，未实施，待用户拍板默认值）：模型经后台进程起的 `python3 -m http.server` 默认监听所有网卡，Gateway 停止后仍在运行。事实：(a) 受管后台进程按设计脱离 Gateway 进程组、跨工作片存活，只有显式 `/stop` 按 owner/task/run 身份回收；`gateway stop` 不触碰任何 process session，存活是设计行为不是泄漏；(b) 监听地址事实只在 `background_process` 的 `status` 动作按端口查询时由 `process_network_status` 观测，启动回执不带监听范围，模型和用户在启动时看不到"绑了所有网卡"；(c) 该模块非目标已写明不按命令正文判断进程状态，所以不能按 `http.server` 文本拦。可选方案：① 启动握手完成后一次观测监听并把 `network.reachability` 写进启动回执与 runtime 事件（只记事实）；② owner 配置 `background_process_listen_scope`（`loopback_only`/`any`），按 socket 事实在启动后结构化停止越界会话并返回稳定错误码；③ Gateway 停机把仍存活的后台会话（数量、监听范围）写进停机事件与 `my-agent status`。**③ 已实现**（2026-09-24 深夜：事件 `gateway_background_sessions_surviving`、state `surviving_background_sessions`、status `background_sessions_after_stop`，见[受管后台进程会话](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md)）；① 因启动瞬间服务往往还没绑定端口而不可靠，不做；**② 已按用户决定实现**（2026-09-25）：默认 `loopback`，模型要开放局域网必须在 `run_command` 里结构化声明 `background_listen_scope=lan`，首次由用户在审批面板确认并可选"本用户长期允许"（新增通用机制 owner operation grants：`ApprovalPolicy.owner_grant_parameters` → binding.grant_key → decision `approved_owner` → owner `tool_policy.json.operation_grants`，自主模式不放行未授权的这类调用）；host 每 2 秒按真实 socket 表核对，越界即回收并留证据（配置 `background_process_listen_scope_enforce`）。见[受管后台进程会话](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md)「监听范围」。
- **自学习 S2：待确认 Skill 提案的审核顺序 `skill_proposal_review`（第 15 项 P5-C）**（2026-09-24，已合入 main `1132fd9d0`；2026-09-25 随 `record_lesson` 做了真实 Jev 验收，端到端真实验收见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)「15 自学习 S1/S2 端到端真实验收」一节）：
  - **接线与开关**：新增独立 `owner_background` 接入点，默认 off，只在 `my-agent skills proposals list` 运行。AgentConfig/YAML 三字段 `decision_skill_proposal_review_mode/_timeout_seconds/_profile_id` 与原设置服务、TUI 菜单共用，只允许用户长期（owner）设置。配置归 AgentConfig：此点只排展示、不授予 Skill/工具权限，和 `enable_self_learning` 同属主配置，CLI 也只加载主配置。审计里暂称 `self_learning`，改名以表明只管审核顺序。
  - **触发与材料**：待确认提案 2—30 条且本点 observe/apply、总开关开启时才请求；0—1 条或关闭时零请求、输出逐字节不变。不要求 `enable_self_learning`（它只管生成）。外发只有 `proposal_i` 别名、创建顺序、来源计数，以及经 `external_data/default` 投影的 description、when_to_use 和 240 字经验摘录；提案/候选/任务/运行编号、路径与目标 Skill 名只进本地版本摘要，草稿 hash 不符则不发。
  - **采用**：每条一道 choice（`review_first/normal/review_later/possible_duplicate`，非选择 `not_needed/need_data/abstain`），缺题、多答、逐题错误或任一非选择整体保留原序。采用前核对候选版本、配置与期限，并重读待确认提案比对编号、版本、状态和草稿 hash。稳定排序 review_first→normal→review_later/可能重复（同组），只动待确认提案的位置，附宿主固定标签；`--json` 增加 `review_order` 块。
  - **边界**：observe 只记账不改输出；任何失败、冷却或变化都保留原输出，取消与中断上抛。从不确认、拒绝或写提案/Skill，没有模型可调用入口。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-自学习-s2待确认-skill-提案的审核顺序-skill_proposal_review)。
- **决策实验授权入口、经验输入上界与发送硬门**（2026-09-24，已合入 main `dfa8e498b`；2026-09-25 两次真实授权发送的结算额都等于供应商计费，比例 0.43；结算快照尚未持久化，列入 E2）：用户于 2026-09-24 批准接受**经验（非供应商保证）**的实验输入上界，并要求明确标注为经验值。三部分：① `/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>` 与 `/audit … prepare` 同一任务命令机制，参数冻结进排队请求、模型只见任务正文；主轮发布 run/attempt 后、首个模型调用前在同一精确回合锁内写 `experiment_grant` 回执并调用 E1 授权原语，重放不再授权、Compact 再入与重启分别以身份/账本代次失效；信封升级 v2，必带 `input_bound_policy="empirical:jev_wire_bytes.v1"`，缺者永不发送。② 经验上界 `C = ceil(B/2) + 256×Q + 1024`（B 为最终 wire 字节），只在 skill_tool、Q≤64、state≤4096 字节、C≤57,600 内使用，越界不预留不发送；原账只接受带 `kind=empirical` 标签的上界对象，估算/上界/实际分开记。③ 传输层在最终字节生成后、任何 DNS/连接/遥测前调用单次发送许可，复核绑定、撤销/期限、设置/身份/账本代次/口径与连接代次后在原账锁内消费；拒绝不重试、不触发连接退避。结算：成功按实际扣减，发送前拒绝与未知结果都不退款并关闭预算，无许可的 HTTP 记 `gate_bypassed`。`experiment_enabled` 默认仍关闭，首次真实授权发送尚未进行。合同见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-e1-有界自测授权入口经验输入上界与发送硬门2026-09-24本地实施待审)，交接见 [E1 交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)。
- **决策实验对照记录、证据评估与授权内自动晋升（P5-E2/F1）**（2026-09-25，已合入 main `84d873c9b`；拒绝与正向晋升两条路径都已真实验收，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 17 项两节）：① E2：实验调用经原账结算后，`settle_input_budget` 返回的结算视图（快照＋结算码/调用编号/原估算/声明上界）经实验调用对象带回，挂到 `DecisionOutcome.experiment`；只观察路径据此生成 `decision_experiment_record.v1`（身份 refs、授权/设置/策略/连接版本、基线=点关闭时实际展示的工具名集合、候选=Jev 回答按 apply 同一规则投影的短名单/延迟名单、结算视图），经能力观察出口拆出写进同一请求记录的 `experiment_records`（按原调用编号去重、最多 8 条、盖执行代次，回合关闭/停止时不写）；回合正常收尾才按结构化工具账补写实际调用工具名，非 completed 或工具账不完整记 known=false。没有 token 表、旁路恢复文件或第二本账，普通请求零 I/O。② F1a：只读评估器只读这些条目（按 owner/thread 核对）；可比较样本≥3、窗口（最近 8）内全部 charged、每个可比较样本短名单召回=1.0（快照外工具不计分母）且延迟数>0 时才提出 `points.skill_tool.mode off→apply`，否则 keep_observing 与原因码；阈值是审计规则常量，不设配置。跨请求证据只沿授权回执 v2 的 `previous_request_id` 回读原请求记录（至多 16 条），`user_config decision_read` 在已有授权信封时附只读 `experiment_evaluation`。③ F1b：`/experiment apply skill_tool …` 的信封 operations 为 observe+apply，实验调用仍只观察；回合收尾在 apply 授权内于精确回合锁复读设置，核对授权仍为本请求、active、未到期、revision 与授权时一致、点仍 off，再经原设置 patch 的完整 CAS 写 thread 覆盖；冲突或任何用户后改都跳过不覆盖。回执权威选本请求记录的 `experiment_records.promotion`（与证据同处；信封是纯授权且会被下一次授权整份替换）：先写 promoting 再改设置，已有回执即不再试，崩溃遗留 promoting 表示不确定、不重试不恢复。到期/撤销不回滚已晋升设置，reset 恢复继承；Jev 回答与模型工具都没有授权或晋升路径。合同见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-e2f1-对照记录证据评估与授权内自动晋升2026-09-25本地实施待审)，交接见 [E1 交接第三片](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)。
- **交付复核焦点真实样本暴露的四个缺口**（2026-09-25；第 2—4 项已在分支 `claude/verification-exit-scope-chains` 实施、待审，第 1 项只写了设计，见下一条；样本见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)第 15 节）：
  1. **触发时机**：本点只在新的验证事件上触发，而"最后一次验证之后又改文件、未复核就交付"才是最该提示的场景。方向：在写入工具使已有验证变为 stale、或回合即将收尾时，按结构化的验证状态提示一次；仍只作软提示，不增加强制续跑或完成门。
  2. **`&&` 串联的验证命令**：返回码 0 其实证明每条都通过，可以逐条记为 passed；非 0 无法归属，仍不记。
  3. **返回码 126/127**：命令没有执行（找不到命令或不可执行），应记为"未运行"而不是测试失败。
  4. **范围判断**：`pytest tests/` 被记成 targeted。已按主线 owner 的决定改为按参数形状判定：目录或不给路径为 full，文件、`::node` 或筛选开关为 targeted。
- **设计（未实施）：交付复核焦点在"改后未复核"时触发**（2026-09-25，主线 owner 要求先写设计、下一片再落）：
  - **问题**：现在只在新的验证事件上触发，而"最后一次验证之后又改文件、然后直接交付"才是最该提示的场景。
  - **触发事实来源**：写入工具的结构化 `verification_state`，即其中带 `status=stale` 与 `last_verification_id` 的行。它由 `record_tool_verification` 在成功写入后产生，与现有焦点来自同一份归档信封；不读正文，也不看模型"我已经测过"之类的说法。触发条件：当前写入记录让某个已有焦点变成 stale，且本轮焦点满足现有 2—12 个的门槛。只有一个 stale 焦点时，不需要 Jev 挑选。
  - **与完成门的关系**：宿主没有、也不增加机器完成门。提示仍只追加在当前工具结果之后，可忽略；不强制续跑，不改最终回复，不写 Todo 或收口状态。模型可见的运行事实本来就带 stale 状态，本增强只负责在多个 stale 焦点里挑先复核哪个。
  - **边界**：每条写入记录至多一次请求，并与 run_command 触发点共用"每条记录一次"的约束；子代理、重复失败或未知副作用收口时不触发；设置、来源或期限变化时丢弃建议，与首片一致。
- **验证账只认一次返回码能证明的单条命令，并放行开头的 cd 前缀**（2026-09-25，已实施：分支 `claude/verification-command-shapes`，待审）：真实 TUI 里模型最常写 `cd <项目> && python3 -m pytest …`，原分类把它当链式命令整体拒绝，验证账漏记真实测试，交付复核焦点与交付前核对都看不到；单个管道又没被拆段，`pytest | head` 会按 `head` 的返回码记成 passed。现改为：未加引号的 `|`、`|&`、`&` 一律不算证据；只放行开头一个 `cd <可进入的现有目录> &&` 并以其为 cwd；其余链式写法仍拒绝。详见 [verification 进度](docs/modules/verification/02-progress.md)。
- **TUI 决策菜单的接入点清单改为取 schema 登记**（2026-09-25，已实施：分支 `claude/decision-tui-points`，待审）：菜单原先自带一份接入点清单，漏了 `pre_recall`，界面无法设召回前补充查询，且已有该点覆盖时"恢复继承"列表会抛 KeyError。现在清单直接取 `decision_settings_schema.POINTS`（唯一权威），本地只保留中文显示名，缺显示名时显示原键。今后新增接入点只需在 schema 登记，菜单自动出现；各分支若新增接入点，只需补显示名。
- **交付复核焦点 `delivery_quality`（第 15 项 P5-C 质量提示首片）**（2026-09-24，已实施：本地分支 `claude/decision-delivery-quality`，待审）：`run_command` 刚产生新验证事件、本轮同 run/task 有 2—12 个验证焦点（每个 project/kind/scope 只留最新一条）且至少一个 failed 或其后有修改时，可选地请 Jev 选一个交付前最值得先复核的焦点，宿主只把该焦点的编号/kind/scope/status/其后修改渲染成一句追加提示（≤512 字符），text/native 共用。外发材料只有脱敏当前请求和焦点别名事实，不含路径、命令或输出；默认 off，observe 只记账不追加，任何非成功、选中本次事件或来源/配置变化都保留原展示；ToolResult、归档、验证账、Goal、Todo 与收口不变，取消照常上抛。接线沿外部材料首片：`_record_tool_call` 的原展示接缝改为 `_optional_result_hints` 依次调用两个按工具名互斥的点。未做真实 Jev/TUI 验收。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-质量提示首片交付复核焦点-delivery_quality)。
- **自学习 S1：子代理 lesson 生成待用户确认的 Skill 提案**（2026-09-24，已合入 main `e9ead5ae3`；2026-09-25 随 `record_lesson` 做了端到端真实验收）：`enable_self_learning`（默认 false，YAML、AgentConfig 与布尔规范化同步）开启时，组合根给子代理 manager 接上 `capability/skill_proposals.py`；runner 结果记录 lesson Candidate 之后，只把 `subagent_lesson`、同时带 task/run 来源、状态有效且未脱敏的候选按固定模板（不调模型）渲染成提案，O_EXCL 幂等写入 owner 路径解析器登记的 `<owner_home>/data/skill_proposals/<proposal_id>.json`（`proposal_id` 为 sha256(candidate_id + content_hash) 前 24 位；目标 `lesson-<正文 hash 前 12 位>`、`before=absent`；提案写明来源任务/运行、触发原因、拟保存内容和适用场景），生成失败只写工作日志、不影响结果交付。正式 Skill 只能由用户 `my-agent skills proposals confirm <id> --expected-revision N` 写入：owner 锁内复核版本与待确认状态、草稿 hash、来源 Candidate（存在、未脱敏、hash 未变、未被拒绝/替代/过期/阻塞）、目标不存在，再在临时目录经 `parse_skill_file(require_frontmatter=True)`、`scan_skill(source="agent_generated")` 与 `install_decision`（不 force，caution/dangerous 均拒）后 `os.replace` 到 `<owner_home>/skills/<name>/` 并标 committed（revision+1）；任何失败不写目标、提案保持待确认并返回结构化错误码，写回执失败会删掉刚装的目标。目录刻意不叫 `learning_drafts`：Curator 每次持 lease 前的 Memory 迁移会递归迁走并删除该名字的目录。没有任何模型可调用的确认工具；开关只控制自动生成，已有提案仍可在 CLI 查看/确认/拒绝。S2（Jev 对待确认提案的审核排序）见本台账顶部“自学习 S2”条目；两者的端到端真实验收见 `record_lesson` 条目。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)自学习段与 [TESTS](TESTS.md)。
- **召回前补充查询与关系提示的真实收益，以及随之发现的四个缺口**（2026-09-25，真实验收已完成：main `ab23a2666` 在测试机隔离目录，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#14-p5-a-召回前补充查询语义召回下的真实收益2026-09-25main-ab23a2666)；四个缺口均**未实施**，只记录方向）：
  - P5-A：在语义召回下，3 条已知漏召回样本中 2 条稳定补回（4/4 次），答复从"查不到"变成准确事实。
  - P5-B：关系提示让真实 M2.7 提取把"换车"稳定归为 `long_term_fact replace` 并指向原条目（off 两次都是 `user_profile`）；其余两类无稳定差异。
  - 两点都默认关闭。发现的缺口如下：
    1. **补充片段选择缺客观材料**：第 3 条样本 Jev 两批都选了主题已被基线覆盖的片段。方向：宿主把"各片段能否新增记录"作为结构化事实交给 Jev，或在有空槽时按阈值确定性补位。先评估额外嵌入开销和弱相关事实混入的风险。
    2. **主模型没有长期事实检索工具**：工具面只有 `remember`、`session_search`、`search_text` 等，正式召回漏掉的事实对主模型不可达，off 轮它自查也查不到。是否补一个只读、按 owner/scope 约束的检索工具需要单独设计，与自动召回的权威边界一并考虑。
    3. **语义检索每次对全部事实重新嵌入**：`_search_scoped` 对 active 列表整体调嵌入端，补充查询使嵌入量翻倍。事实多时需要按正文哈希缓存向量；缓存只能是派生索引，正文仍以 JSONL 为准。
    4. **关系对按顺序截取前 32 对**：`decision_curator_relation` 取消息×正式条目笛卡尔积的前 32 对，不按相关度，后面的消息比不到。方向：按词面或语义相似度挑对，覆盖范围仍如实声明为"仅展示的对"。
  - 同一实验还发现一个与决策无关的 Memory 问题：新 owner 首次整理时，v2 迁移把 memory.md/HOT 模板的标题行生成两条 `migrated_legacy` 待审候选。已告知主线 owner，归 Memory 模块处理。
- **召回前补充查询的证据与收益前提**（2026-09-24，证据已合入 main `ab23a2666`；语义召回真实实验见下一条）：只用词面检索时补充查询按构造补不回任何事实（片段词一定在整句里、BM25 已返回全部正分文档），真实收益只可能出现在语义召回下；上下文包 `memory_refs` 新增 `recalled_refs` 与 `recall_findings`，只写文件、不进提示。详见[召回前审计](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)。
- **主会话选模候选补用户授权的用途说明**（2026-09-24，用途标签已合入 main `4ec0e11f3`；采用模式问题说明修正在分支 `claude/decision-selection-question`，待审；带标签的第五个真实样本仍选当前模型，修正问题说明后的第六个样本真实跨模型采用成功并答对；实现口径见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)第 4 节 Stage C 段。字段只登记在 `validate_model` 与快捷新增白名单，不进 `resolved_model`：其结果会整体覆盖 AgentConfig，而运行时没有标签消费者；主线 owner 已同意字段形状，`input_modalities` 将按同一格式并列）：四个真实样本里 Jev 都没有建议换模型，其中一个任务明显超出当前模型的声明窗口。原因在于候选材料只有模型名、后端和声明窗口，指令又明确要求"候选声明不是实际能力证明"，Jev 缺少比较语义匹配的依据；而容量本就由宿主判断。方向：模型档案增加由用户显式填写、结构化的用途标签（例如长文档、视觉、推理、低成本），只作为 Jev 的语义材料，不作为容量或授权的证明，也不由模型自述或名称推断。这一项与媒体屏障的"视觉能力事实"都会动 model_profiles schema，须与其实施者（重构线）统一字段后再落。证据见[主会话真实交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。
- **能力推荐的结论与展示结果写进请求记录**（2026-09-24，已实施：本地分支 `claude/decision-capability-observation`，待审；主线 owner 已同意位置与形状，实现见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)能力推荐段）：在第 13 项的多 owner 真实样本中发现，`recommend_capabilities` 算出的 `finding`（决策状态，例如 `skill_tool_decision:apply:cooldown`）以及展示短名单、延迟名单只存在于运行内存里。已落盘的只有调用层终态（线程 model_usage 的 decision 分项：failed/timed_out/finished 与输入用量）；没有发出调用的结果（冷却、关闭、过期）和采用结论都不在请求记录、线程指标或归档上下文快照中。真实样本因此无法核对结论是否被采用、为什么保留原样（挂起样本里只能从系统提示摘要的变化间接看到采用痕迹）。方向：把这组结构化事实（状态码、候选版本、短名单与延迟名单的名称或摘要）写进 Gateway 请求记录，位置和口径与主会话选模的 `model_selection_observation` 相同；只用于观察和展示，不参与路由或采用判定。证据见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#13-多-owner-与断网真实样本2026-09-24main-4163e66c0)。
- **子代理自动选模提交阶段记录结构化原因码**（2026-09-24，已实施：本地分支 `claude/decision-child-commit-reasons`，待审）：提交阶段此前把目录锁占用、目录代次变化、父线程锁占用或缺失、设置/task/权限变化、期限和 child 线程冲突都记成 `selection_changed`，第四轮真实样本因此无法归因。现每个失败点各有原因码，写进 child thread 建议的 `retained` 记录；只改观察口径，采用/保留的判定与锁序不变。原因码清单见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)第 4 节子代理选择段。
- **决策连接连续失败时逐步加长冷却**（2026-09-24，已合入 main `3933b1db1` 并随 step10k 部署双机；测试机真实复测通过，见[退避复测](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#13-冷却退避的真实复测2026-09-24main-3933b1db1)）：第 13 项挂起样本中，超时和瞬时失败的冷却固定 30 秒，只能挡住窗口内的轮次；供应商持续挂起时，冷却一过的第一轮又要付一次完整期限（样本中多等约 6–7 秒）。实现：同一连接键（owner、profile、连接修订）冷却过期后的重试再失败时冷却翻倍（30→60→120→240 秒，封顶 300 秒，与额度一致）；冷却期内才返回的并发失败算同一次故障、不加级；连接返回过响应、连接测试成功或显式重试即复位；配置错误仍等配置修订，额度仍 300 秒。只改 `decision_policy` 的进程内冷却表，不新增持久状态或配置；合同见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#42-已实现的可选服务边界)。证据见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#13-决策端挂起的真实样本2026-09-24main-4163e66c0)。

12.4 第二片 2a 已由主线 owner 审阅合入 main `931f83739`，已随 `d69f30cf3` 同版部署双机：
- `ConversationHistorySeed` 的具体历史与只读来源严格二选一。来源只冻结同次地址视图、宿主原单行规则和投影时刻，只在原 native/text 准备边界解析（文本协议每轮一次，原生在建立循环和运行内重建时各一次），多次解析结果相同；capture 和纯投影合同不变。
- 代价：canonical 文件在整个运行期间都不能改写（追加合法），改写、截短、替换或删除时下一次解析明确失败；每次解析按地址重读，4.2M 字符下文本约 34ms、原生约 65ms。
- 4.2M 字符来源下，种子准备后驻留从约 8.5MB 降到 32–54KB；运行结束后的驻留减少 8.4–9.4MB；Gateway 首次发送前峰值从 29.8MB 降到 21.3MB。
- 摘要期峰值约 10–11MB 不变，属于 2b（旧请求释放）。

后台上下文预算只估算将渲染的节（已合入 main `5dcdfd463`）：渲染开关作为结构化 `rendered_keys` 传给预算；有历史种子时不渲染的最近消息不再挤占总预算，也不再保留正文。后台回合各阶段约少 1.34MB，可见运行事实不再被过度截断。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#后台上下文预算只估算渲染节有种子时不保留最近消息2026-09-24本地)。

决策线能力推荐按插件分组出题（已合入 main `a2e26178b`）：只按结构化 `ToolModelHints.provider_id` 把同一插件的工具合成一题；选中展开全部成员，未选中整体延迟；内置工具与 Skill 仍逐项。内置插件 21 个工具下题数 21→8、题目大小 −36%。详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md)。

12.4 第二片 2b 已由主线 owner 审阅合入 main `911d0d14d`，已随 `d69f30cf3` 同版部署双机：恢复宿主决定摘要后解绑旧请求的完整原生历史（原参数与 frozen，只解绑这一对象，不原地清空）。4.2M 字符下摘要期峰值从 10.2–11.7MB 降到 1.7–3.3MB。建循环时的首次物化峰值仍在；实测每次请求只读一次完整历史，改为按需物化只会挪动峰值、不降峰，所以不做（方案 B 结论，主线 owner 已同意）。Gateway 的 21.3MB 峰值另有来源：索引、近期产物和追加去重三处按行数而不按字节整块读取消息文件，行大时等于整份文件；方案是字节有界的倒读流式行迭代器（已实施：本地分支 `claude/decision-gateway-message-reads`，主线 owner 有条件同意，待审；Gateway 准备期峰值 21.3→0.8MB，全程 22.7→10.0MB，与 child、后台持平），见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#gateway-213mb-峰值来自整块读取消息文件2026-09-24已实施待审)。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#摘要期释放旧请求历史2b2026-09-24本地)。

恢复候选提交后的同请求重试（已合入 main `c9794b9ca`，已随 `d69f30cf3` 部署双机）：候选发送瞬断后，重试原样复用已提交的（候选参数，prompt），不再在压缩前的原参数上重建。记录按原参数对象身份命中，换参数即清除。`_model_turn_or_retry` 的空响应修复和插话取代两个重跑分支也改为先换成候选参数，不再在原参数上重建或注入。详见[依赖拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#恢复候选提交后的同请求重试决策分支2026-09-24本地)。
候选参数与原参数共享同一个 `tool_context` 列表（`replace_recovery_history` 的既有行为）；2b 已把它写成显式合同并加断言测试，行为不变。

详见[接入设计](docs/design/DECISION_MODEL_INTEGRATION.md#todo124-宿主历史来源与请求生命周期2026-09-23实施中)和[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子只读来源2a2026-09-23本地)。

三宿主来源视图仍在seed准备时重新物化：相同约4.2M字符输入的准备峰值约8.6–9.0MB，第二片需延后物化并处理旧请求持有者。child无正文展示已移除提前全读；范围、纯投影和完整发送边界保持，详见[宿主基线](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子生命周期基线2026-09-23第二片进行中)。

12.4选中正文生命周期首片已本地实现：在原canonical消息文件上复用固定尾界和行hash，只保存临时地址视图，贯穿来源、分区与摘要读取；不新增持久索引、摘要权威或截断策略。宿主完整请求的冻结与释放另作伴随片，纯请求投影仍禁止读文件。真实文件到摘要/CAS内存红绿及独立审查已完成，不能视为三宿主全链内存验收，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#选中正文生命周期方案2026-09-23首片本地验收)。

原生历史只在canonical读取边界做必要深拷贝，后续只读投影接收该独占副本；匿名重复输出各自隔离。token估算复用主线有界小JSON直接编码/大JSON流式选择，异常顺序和数值保持；本地已实现，无持久状态或新配置。第12.4全链有界仍待完成，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4检查点读取使用同次描述符内的临时ID→行地址/hash，原文件及thread head仍唯一权威；全部行先解码检查，已提交链逐条验封后只保留适用摘要及覆盖元数据。无持久索引、缓存或新开关，已本地实现，整体有界来源仍未完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.6本地组合验收已完成，原目录/线程CAS/容量门和校准账仍唯一；扫描修复保持原Unicode空白与损坏分类，不增加配置或持久索引。12.4全链有界与12.7真实缓存未完成，见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Compact通用底座按函数级闭包向主线移植，自动选模、Jev、菜单及decision配置不是必要依赖。扫描/估算/摘要窗口候选已在主线临时副本验证；scoped checkpoint、摘要基础链、宿主同源恢复与容量门须成套审查，完整保留投影最后接线。当前为交接候选、未合并，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4摘要字符来源采用只读两遍编码：总字符/hash与顺序当前窗口，不落临时文件或新增索引。共享tokens沿原估算语义流式累计；分段修复提示统一预留并在发送前复验。已实现并通过316项联合，全链来源及覆盖尚未有界；见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4保留历史完整投影已本地实现：显式Compact来源及候选不再套普通字符窗口，三个宿主共用原容量门；超量/未知拒绝而不删原文放行。普通展示规则保留，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4固定来源范围筛选已本地实现、待主线接口集成：两遍同EOF原字节校验，先解析完整锚点再保留范围内未覆盖正文，后台operational和native共用谓词；ID位置与未压正文仍常驻，完整有界Compact尚未完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

超大canonical历史有界化已完成依赖审计，原消息固定尾界/字节页和幂等流式扫描底座已本地153项通过：须同时约束原消息页、scope筛选、checkpoint覆盖链与幂等扫描，不能仅改limit或截断来源。后续沿原游标与CAS设计连续范围证明，详情见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

决策模型媒体整合本地1419项联合与严格gate通过：自动选模/Compact共用原内容完整性检查，未知模态保留原模型和原始历史；强制恢复不凭附件引用取得容量或摘要覆盖。transcript只覆盖安全文字前缀，完整媒体后缀保留。媒体原提交的M3证据与当前集成版分开，12.4仍开放，详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4 集成版媒体真实验收已通过（main `5dcdfd463`，.9 官方 M3，2026-09-24）：图片原样进入请求；手动压缩只摘要图片之前的文字前缀，图片轮原样保留；压缩后模型仍能依据原图作答。见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#124-媒体与-compact-集成版真实验收2026-09-24main-5dcdfd463)。

媒体会话越过压缩点的修复（已合入 main `d69f30cf3` 并部署双机；测试机真实验收已通过）：
- 问题：未压历史带图时，估算一越过自动压缩点，请求就整轮失败，即使离窗口还远。原因是 preflight 仍按压缩点报溢出，而自动和轮内压缩遇未知模态会跳过、强制恢复又拒绝。
- 修复：请求的文字容量不可知时，preflight 只守窗口硬上限；越过窗口时，强制恢复报结构化码 `COMPACT_REQUEST_NON_TEXT`，不再与内部投影失配共用 `COMPACT_REQUEST_PROJECTION_UNKNOWN`，客户端文案据此说明是图片等非文本内容所致。`save=false` 与纯文字会话行为不变。
- 详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md#媒体会话越过压缩点2026-09-24本地修复)。

- **媒体屏障已定方向**（2026-09-24，用户拍板，决策线已评审并入；片 A 归档引用主链已本地实现，片 B/C 待做）：A（旧媒体降级为可重新附上的归档引用、只摘要文字）是主链与默认；B（含图轮次随文字进摘要）只在模型视觉能力事实为 supported 时启用，事实只能来自档案字段 `input_modalities` 或宿主的结构化纯色图探针，不来自模型自述；B 失败不在同一请求内退回 A，只由线程上持久化的失败码在下一次压缩切换。三处宿主门把"能否摘要"与"能否计量"分开判定。详见[媒体压缩策略](docs/design/COMPACT_MEDIA_POLICY.md)。

长对话验收方法已按用户要求调整：用 my-agent 自主完成真实 GitHub 项目跨语言实现产生自然历史；合成大文件仅保留存储边界定位用途。官网 M2.7 的 fd→Python 原生 TUI 验收待完成，方法见 [TESTS](TESTS.md#真实开发长任务验收方法)。

TUI 原生媒体输入已实现、专用测试机官网 M3 验收通过：内容寻址原件、owner 校验、草稿 refs、发送边界编码与历史媒体预算复用原主链。旧纯文本记录无需迁移。详细合同见 [TUI 图片视频](docs/design/TUI_INPUT_MEDIA.md)；候选未默认部署。

- **插件声明式面板与只读订阅（第 9 步，实施中）**：包描述 v2 可选声明至多 2 个面板（text/table/status），只订阅核心公开主题 activity/run_state；无面板的包仍按 v1 序列化，已安装包描述字节不变。Gateway 进程内唯一展示服务只读已有活动投影，经固定激活代次的插件连接调用只读 `my-agent/display.render`，每个激活 1 条连接、1 个在途请求、输入只保留最新，结果按类型校验截断；渲染前后复核代次，停用/换代立即丢弃并关闭连接，空闲 120 秒关闭。TUI 只渲染核心校验结果，插件代码不进 TUI。详见 [插件展示](docs/design/PLUGIN_DISPLAY.md)。

- **委派与交付核对软引导（2026-09-23，已采用，实现中）**：第8步新版 TUI229/233 中，部分孩子没有用工具计算，而是心算出合计，写出错误数字却标注"通过"；父级也没有回核，最终报告失实。同一需求由主代理用工具实际计算时（TUI228）完全正确。参照 Codex 的 `spawn_agent`/worker 合同（默认不委派、委派要收窄到具体产出、孩子交回后审阅再整合），只调整软引导文字，不加完成门、不解析回复文字、不按 CSV 或提示词加专项分支：①父级：用户或项目说明没有要求委派时，优先自己完成，只把边界清楚、能并行的部分交出（能力和默认行为不变）；派工时写清要交回的具体产出和核对方式；收到结果后，先用工具对照原始资料抽查关键数字或改动，再整合，不直接转述下级的"通过"。②孩子：最终回复中的数字、统计和核对结论必须来自本轮实际执行的工具输出，并说明出处；没有执行过的检查明确标为"未核对"。没有采用结构化工具用量统计（Codex 也没有），软引导不足时再评估。复测（7c467a4d3）：TUI238 与 233 同题，24 个孩子都用工具计算，全部产物正确（上一轮 10/24 出错）；TUI239 与 229 同题，孩子和父级回核都正确，但父级自己新算的状态小计仍靠心算出错，并编造理由解释矛盾。所以第③条放进所有代理共用的 `prompts/default.md` 证据段：数字来自工具输出；表内不一致时回到工具重算，不编造解释。"没有要求时优先自己做"的折中引导没有阻止自发委派（238 仍派了 24 个孩子），目前只作观察，不升级为默认禁止。

- **第8步耐久进程事实恢复本地修复**：投影下移 tooling，大小输出索引保存同一有界 process；旧行不制造确认。真实索引恢复已复现缺口并修复；末审补齐未知尾部 ID 的候选内容地址，组合检查已过，待发布／原生 TUI；见[依赖拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#耐久索引恢复补齐第8步本地候选)。

- **第8步Compact逐调用来源已本地集成、待组合验收**：已确认裸call_id跨请求过滤会误隐藏新工具结果；沿现有live-tool账增加精确来源引用，保留既有ID／无refs旧编号结果／提交者语义／CAS，新候选将精确来源纳入内容地址，存量不确定来源保留并显式标记。工具并发段窄依赖独立并行，详见[职责拆分](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#发布前缺口compact逐调用来源已确认修复中)。尚未发布验收。决策分支合并版已改用 v3 四元身份，取代这里的三元引用，见[合并节](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

第8步 Compact 边界在本地开发：候选只借原三个列表和估算回调；checkpoint/CAS 成功后投影失败不得回滚内存历史。C 顺序来源保持原持久格式与降级语义，不隐式引入 v3/scope 迁移；旧跨 request 调用编号过滤风险仍待处理。决策分支合并版改为显式采用 v3 与四元身份，这个风险随之关闭，兼容和回滚边界见合并节。详见[模型与工具循环](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#第8步-compact-候选与提交边界)。

第8步模型／工具循环已本地分离采纳与请求周期：只绑定原操作，不接完整Agent／params，不移动pre-I/O提交；有界消息扫描和等值token估算作为独立底座移植，未启用scope摘要链或第二执行器。18文件456 passed／24既有xfail，真实TUI未验，详见[职责和移植边界](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md)。

第7.10修复随7280c5b3e发布main，同一wheel（SHA256前缀c3f1242f）双机各1,274文件一致，默认入口与唯一Gateway同版。六文件304项、相邻三文件61项及严格gate通过。新版原生TUI225—227均仅派出回执加一条最终回复，任务／attempt结束、无锁、准确宿主退出；227实际命中旧attempt已结算而宿主未退出期间的grant，随后同run第三attempt执行，且能力事件completed最终回复公开。7.9／7.10本轮框架验收收口；7.10原瞬时快照交错由确定性红转绿用例证明，不把本轮无重复扩大为该交错必然命中。225最终摘要被孩子错误覆盖、226缺汇总合计、227检查程序及能力工具选择问题保留为业务失败／限制。线上CI无运行记录，未作为验收来源。

待处理唤醒快照时效（7.10 已发布并完成当前框架复验）：TUI220显示恢复／能力扫描可在候选选择之后改变原通知状态；执行决策必须以原队列当前pending／handled事实为准，不能把先前快照的历史BLOCKED再次当新事件。保持原通知身份、持久历史与当前run/attempt唯一事实源，未处理的新DONE仍按既有交付语义执行；不能依据回复文字去重，也不能统一丢弃合法的后续回复。

授权与旧工作片结束的接续（7.9 修复已部署，真实复验中）：TUI217中grant遇到fresh runner时不启动重复执行是正确约束；但旧attempt结束后必须按同一run的当前授权和控制事实决定是否续接。不能把“申请已处理／wake已消费”当作“续接已完成”，也不能依据模型文字或长期PENDING猜活执行。沿既有执行权和调度事实修复：通知和接续读取同run当前canonical状态，原结果仍作历史；唤醒记录以原mutate窄写，避免旧全快照覆盖结束记录。不新增状态副本、定时器或绕过生命周期门的例外；来源修复已集成为911a53245，原历史保持、旧入口及无用result参数已删除；定向合同通过，新版真实验收待完成，见TESTS。

完成交付与唤醒原因（7.8 已集成，待原生验收）：TUI212 暴露能力请求事件唤醒后，原生工作片已完成却被旧唤醒原因抑制公开 final。交付裁决应读取本轮结构化完成与投递事实，唤醒原因不能替代本轮结果；BLOCKED相关运行账沿原合同保留，不从模型正文判状态，也不强制结算。详见 TESTS 和 Goal 7.8；最小修复fc17def5f已集成，未完成能力事件、取消／中断、空载荷与重放规则保持，待同版部署与原生验收。

工具清理事实的模型可见性（第8步本地修复，待真实验收）：TUI213缺失的process清理回执已沿统一runtime_facts投影进入当前及恢复上下文；同一有界结构保留未知、退出码和数量，不改原结果／执行器／持久schema，不从正文推断成功。源码和组件验证边界见[工具执行事实投影](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#第8步工具执行事实投影)，真实TUI待组合包。

前台自然退出资源边界（第 7 步验收新增缺口，本地候选已集成）：TUI204 暴露命令普通返回 0／1 后嵌套后代仍存活，后续 `/stop` 不可见。前台完成须在启动归属仍可核验时收回所属后代，原命令退出码、业务副作用与清理确认分别记录；显式后台能力独立，不凭已消失组长的 PID 猜归属。详见 [长任务合同](docs/design/LONG_RUNNING_EXECUTION.md) 和 Goal 7.7；源码9330ee385已通过215项定向并同包部署双机，默认双机路径原生复验已核对，未覆盖的路径单列TESTS，不外推。

第 7 步父终态通知已在本地集成，将实际逻辑归入 `RunnerCompletionNotifier`：只持任务关联、WakeStore、
读取父任务和保存错误四项依赖，完成／受控取消共用原投递路径；结果服务和外部停止端负责装配。
保留 exact attempt、直属父级、文件模式及内部监督者信号语义；阶段提醒和能力申请入口不扩改。
已与恢复扫描接口片组合，原 sweep 和恢复测试均绑定同一通知器；旧接口调用删除；组合包 a067baddd 已双机部署，实际验收进行中，详见 STATUS。
见 [子代理迁移边界](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

第 7 步结果提交依赖已在本地候选收窄：初次提交只接原 RuntimeDB、canonical task、结构化结果、
保存回调和绑定本轮身份的交付回调；WAL 原语只接 save，运行结算与诊断只接原 RuntimeDB。
结果服务装配 trace→父通知，仍在 WAL→运行账之后执行；不新增状态副本或兼容转发。
恢复扫描已集成为显式 repo/load/save/list/notify，原 sweep 绑定窄父通知器；恢复模块不再持有完整 manager。
本片不新增扫描器或业务重跑入口，不声称第 7 步完成；新版部署和原生验收分列 STATUS。
见 [收口依赖边界](docs/design/closeout_state_machine.md)。

第 7 步既有 wake／去重回执半写缺口已本地修复，尚未发布：沿原 dedupe 记录先冻结完整观察／信号，
按固定 ID 补齐原文件后确认交付；通用 pending 合并与完成通知保留 handled 显式区分，不截断同键 Goal 后续轮。
查询纯读，v1 只在写入口显式迁移；坏账报错、无 key 不承诺重试幂等、有 key 仍依赖调用方重试，未新增后台扫描。
设计、参考和验收边界见[发布恢复合同](docs/design/closeout_state_machine.md#唤醒配对发布的半写恢复第-7-步本地实现)。

第 7 步依赖收窄（本地候选）：runner 结果准入只接收 canonical task、结果参数和原 RuntimeDB，
不接收完整 manager；文件模式仍显式传 None，诊断仍写原账。已部署结果链与此候选分开验收，
边界见[子代理迁移设计](docs/design/SUBAGENT_PARALLEL_EXECUTION.md#第-7-步结果链迁移边界进行中)。
TUI 绘制合并已改为 20 Hz、周期动画 4 Hz，事件与模型执行不变。同一 fd 真任务的并行原生 TUI 对照 CPU 16.02%→10.23%，候选翻页中位 38 ms；长任务仍在验收，见 TESTS。
稳定历史缓存补充已实现并经等历史真 TUI 验证：发布快照共享、稳定/活动版本键分开、静态前缀按预算复用，安全行组仍在构造时净化。fd 自然开发约 110 分钟/Compact 3 已终态，首次详细展开约 1.2 秒仍是边界；见 [资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md) 与 TESTS，不外推任意历史/时长。

TUI 绘制合并保持 20 Hz、周期动画 4 Hz，事件与模型执行不变。此前同一 fd 真任务的并行原生 TUI 对照 CPU 16.02%→10.23%，最终缓存片另做等历史对照，见 TESTS。

TUI 观察超时与业务终态分离已实现：截止点先查 canonical terminal，原页存活时按同一请求/游标退避续等，退出仅释放观察；plain 有限等待保留。官网 M2.7 原生 TUI 已通过真实短等待窗口和暂停客户端后接收终态，见 [资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md)。

长对话验收方法已按用户要求调整：用 my-agent 自主完成真实 GitHub 项目跨语言实现产生自然历史；合成大文件仅保留存储边界定位用途。官网 M2.7 的 fd→Python 已自然结束，框架与项目内容分开记录，见 [TESTS](TESTS.md#真实开发长任务验收方法)。

TUI 原生媒体输入已实现、专用测试机官网 M3 验收通过：内容寻址原件、owner 校验、草稿 refs、发送边界编码与历史媒体预算复用原主链。旧纯文本记录无需迁移。详细合同见 [TUI 图片视频](docs/design/TUI_INPUT_MEDIA.md)；候选未默认部署。

第 5 步当前状态：使用卡按现有插件包声明与设置 schema 即时投影，详情和成功启用共用格式；列表、动作帮助及补全仍读同一目录，不新增卡片缓存、权限或执行链。本地实现与 300 项相关回归已完成，发布及原生 TUI 仍待验；详见 [插件生命周期](docs/design/PLUGIN_LIFECYCLE.md#从安装完成到真正可用) 和 [唯一 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单)。

重复启用的空资源声明已本地修复：无新环境计划时不声明候选资源，使原 unchanged／缺失拒绝路径真正可达；不更改激活状态机。见 [激活权威](docs/design/PLUGIN_ACTIVATION.md)，待发布真实复验。

显式插件连接收尾已本地移入原 HostCommand 执行区间，释放调用结束后才登记 executor 退出与运行终态；未启动拒绝同样延后，重送只读。详见 [宿主操作与结果](docs/design/HOST_COMMAND_EXECUTION.md#操作与结果)。状态：相关验证中，未发布，不等于所有清理均成功。

MCP 完整失败回执结算已本地修复、待发布验收：合法 `isError=true` 沿原操作账记失败，保留正文、释放逻辑锁；不表示零副作用，不重写历史 UNKNOWN。传输未知仍保留，详见 [连接与结果边界](docs/design/MCP_TRANSPORT_LIFECYCLE.md#完整工具失败与未知结果本地修复待发布验收)。

通用启动观察竞态修复已发布部署，383 项相关回归通过，新 TUI158／161 已实际启用复验：观察 host 退出后复读同一 session 的权威终态，保持原交接事务、身份、取消及退出码裁决。详见[托管进程合同](docs/design/MANAGED_PROCESS_STDIO.md#生命周期与通道)。

发布前边界修正已本地实现：进程内取消令牌从 tooling 迁入 common，保留唯一类型与上下文，不新增兼容入口或持久状态。详见[宿主命令边界](docs/design/HOST_COMMAND_EXECUTION.md#解决问题)，完整发布验收仍待通过。

十步重构的具体 TODO 已落地于 [原 Goal 台账](docs/tasks/REFACTOR_PLUGIN_GOAL.md#当前-todo唯一执行清单)。仅细化交付顺序与汇报，不改变架构范围；当前第 4 步的本地实现、发包部署与真实 TUI 验收分开标记。

第 9 步[插件面板](docs/design/PLUGIN_DISPLAY.md)已发布并经本机真实 TUI 验收；已知缺口：未调用工具的纯模型回合不进入宿主活动投影，
面板与其他窗口显示空闲，是否让未晋升前台回合进入投影待单独设计。

第 10 步已实施（本地）：[插件宿主只读 API](docs/design/PLUGIN_HOST_API.md)（包描述 v4 `host_api=["read"]`），让网页控制台、桌面窗口等界面型插件读取公开运行状态；写入与控制类权限未开放。

第 10 步已实施（本地）：随包 Skill（包描述 v3，来源 `plugin:<ID>`，优先级低于工作区 > 用户 > 共享 > 内置），详见 [可装卸插件方案](docs/design/PLUGIN_LIFECYCLE.md)。

第 10 步审批前复核已实现（2026-09-24 用户批准，分支 `claude/tool-precheck`，代理工具 opt-in 复核、两码登记、停用链改报激活失效，待真实复验）：[工具调用审批前的有效性复核](docs/design/TOOL_CALL_PRECHECK.md)——同一回合内插件被停用后仍先弹审批、批准后才失败；
设计为统一权限门在 `ask` 之后、审批事件之前，以及审批通过后 claim 之前，用处理器自己的 `availability()` 复核，失效按 `TOOL_UNAVAILABLE` 拦下。
改动统一权限门，须用户确认并经决策线评审后实现。插件数据清理命令、SDK 的"一致才替换"写入原语仍在待设计列表。

第 10 步新增[插件逐次工作区写入上下文](docs/design/PLUGIN_WORKSPACE_WRITE.md)，状态为本地已实施、真实 TUI 未验：
只对协商扩展且声明写效果的工具下发冻结写入范围，裁决与内置写工具一致且只可能更严，由逐项比对测试强制。

第 4 步新增[插件逐次工作区读取上下文](docs/design/PLUGIN_WORKSPACE_CONTEXT.md)，状态为开发中：
仅对固定连接明确支持扩展的自有插件传递冻结 cwd 与原读取权限，不改参数、共享进程或安装配置。
通用运输已在本地接通；唯一源码构建期投影的轻量 SDK 与 workspace-peek 已有实际标准构建和独立 MCP 组件验证。
真实多 TUI 装卸未验，不进入第 5 步。
SDK 构建开始实施：原字节投影读取合同、路径策略及通用 no-follow I/O；分页读取持有原文件描述符，
不另写链接校验旁路。workspace-peek 的命令、工具、设置从包内一份声明生成，版本和 wheel 摘要来自标准构建结果。

显式业务命令本地执行链已接通：原 HostCommand v2 分别冻结工具参数摘要和宿主选择摘要，v1 身份索引不变；
等待审批仍在原 executor 区间，经现有批准 binding 恢复同一调用，不新增审批服务或执行器。
协议与恢复边界见 [宿主命令](docs/design/HOST_COMMAND_EXECUTION.md#请求与运行)，尚未完成端到端验收。

**可选决策模型：P1—P5 完整实施已授权，P1 与 Curator 已本地验收，子代理/召回已本地验收，原设置入口、原生测试与 agent 代操作已本地联合验收，10 能力减量已本地验收，12窗口/缓存进行中。**

第 12 项本地计量已复用原出站配对清扫、guidance 与 ToolChoice，纯投影计量不读取宿主或校准；旧观测 v2 明确失效。三宿主同 turn 展示接续、失效清除与后台原执行身份回传已本地验收；不新增持久展示状态。完整恢复请求与 Compact 候选接受边界仍未闭合，细节见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
12.5的已知输出预留已接原transcript触发/候选接受门，本地72项通过；完整计量仍依赖12.4的原恢复准备一次化，不得多次召回/重放摘要副作用或把上一失败轮IR直接冒充恢复输入。
后台准备已分离prepare/render，历史范围及摘要投影从同次成功读取冻结；这只是完整恢复前置，不改变持久Compact的作用域权威。detached任务与窄审计的全局摘要/工具隐藏关系须先收口，不以省略历史后得到的小容量作为成功。
审查已用原store/checkpoint复现三类恢复材料丢失。唯一checkpoint链的v3 scope/base/精确覆盖与局部CAS保留全线程投影已实现底座；旧v1/v2显式读取，旧工具身份不全不命中精确覆盖。后台作用域选择和同一视图的摘要注入/隐藏已本地接线；原transcript与活动归档提交使用同scope/base，后台完整请求与Gateway/child同视图准备已本地接线，初次/手动与混合超大来源仍待实施，详见[容量审计末节](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4的Gateway overflow接缝已本地实施：原render/select冻结完整恢复请求，候选按结构化位置更新历史/证据，原CAS成功后继续同次生成。原来源defer只跳过压缩，保留repair/索引；无来源的活动回合压缩仍先于恢复准备。摘要失败不进入普通业务重试，token取消与边界事件沿原合同；child overflow现已接通并共用core恢复器与请求捕获，Gateway只保留宿主投影；后台transcript及carried活动归档已接公共完整恢复入口，初次/手动仍待接通，详见容量审计末节。
P5-B 首片已本地接入独立 `curator_relation`：只比较本批完整消息与有真实版本、完整短正文的正式 long-term 条目，提示可能重复/更新/冲突；原提取、候选、验证和晋升仍唯一，缺版本/截断/变更保留原流程。默认关闭、owner 后台设置、同一阶段期限；隔离真实 Jev 的12对短样本符合预设、超时保留原输入，后续提取仅本地替身，更广质量和真实晋升未验，详见[交接](docs/tasks/DECISION_MODEL_P5B_HANDOFF.md)。
P5-A 召回前省略普通会话记忆仍缺可保证用户显式查历史不被跳过的可信结构化意图；普通用户回合继续原召回。默认关闭的 `pre_recall` 首片已接正式上下文：原完整查询、HOT、lesson 和既选长期事实先保留，Jev 只从至多四个有界片段中选一次补充查询；仅用原 scope、剩余 top_k/字符预算追加经正式源确认的事实，未注入候选不记访问，P3 排序与本点共用阶段期限。离线136项通过，其中受控 JSONL 漏召回样本可只补确认缺失事实；隔离 Jev 两轮均建议查询，但词面基线已覆盖两条事实，应用无新增并标注 `no_addition`，真实质量收益未证，默认维持关闭，见[审计与实施记录](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)。P5-C 的已归档 web_fetch 多页阅读顺序首片默认关闭，只向原 text/native 展示追加有界页序；隔离真实 Jev 首轮 not_needed 保留原展示、第二样本自动追加2→3→1，原结果/refs/归档/账本不变。本地页面源不证明互联网检索总体质量，其余接入点未验，见[交接](docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。
Jev官方64k整包/32k单题容量与声明的更小窗口现做发送前估算筛查，原 JSON 256 KiB 仍是资源硬帽。全仓回归曾发现把 UTF-8 字节数当 token 上界误拒 78 KB 合法批量候选并使 HTTP 零请求，现改复用原 `estimate_tokens` 且留一成余量；估算不冒充供应商精确 tokenizer 或硬容量保证，供应商超窗错误沿原业务回退。完整子代理模型容量、Compact面和真实缓存仍待核对。
召回后排序已移除旧的固定64题拦截；原记录完整性不变，过量请求由共用协议资源帽与Jev窗口门拒绝，失败沿原顺序。Curator每批最多标注32个来源是本地延迟保护，不是供应商题数限制。
子代理执行模型候选现能在同一决策设置服务按 profile ID 缩小；空列表沿原授权目录。当前用户指定的隔离验收范围是官方 MiniMax-M2.7、官方 MiniMax-M3 和 OpenCode DeepSeek-V4-Flash，不按名称猜来源；实际改选执行仍待验。
用户明确：主代理收到派工需求后，子代理模型建议、首轮请求能力核验、采用或回退及继续运行都由宿主自动完成，不设置逐子代理用户选择、确认或补资料步骤。显式模型设置只提供已有的结构化约束与竞态优先级；创建时的 pending 建议尚无执行效力，不可算作实际切换验收。
子代理首次采用所需的模型连接来源现有原私有目录 v5、共享发布 v2 的随机持久代次；所有原保存、凭据/OAuth 刷新及共享撤销均使旧快照失效，已启用准备才初始化旧目录，普通读取不写。原锁 guard 可覆盖最终 child thread CAS，缺代次或非管理员旧 shared 自动保留原模型；当前仅目录合同通过142项，首次发送消费与真实异模仍待验，见[交接](docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md)。
P5-D 主会话自动选模只读审计及原线程选择版本首片已完成：同值显式选择也前进单调版本，旧线程三字段全缺归一未知、不伪造历史；Gateway 仍先于准确会话车道冻结模型，真实首请求容量和跨 provider 历史可回放事实尚缺。须在获车道后、首次模型相关准备前自动选择一次，未知保留原合法模型，不要求逐片用户确认。版本首片不等于已自动采用，详见[审计](docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md)与[交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md)。
P5-D 后续 Stage C 已在准确 Gateway 车道内用原完整请求准备、同源 provider payload、候选目录代次和原线程 CAS 实现首请求自动采用；发送前明确拒绝才沿原模型一次，已提交或结果未知不跨模型重发，未知模态/容量保留原模型。容量仅是 UTF-8 字节加协议余量的工程估计，不是供应商精确 token 保证；本地 fake HTTP 联合302项、最新本片34项通过，真实主会话跨供应商仍待验，见[采用交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md)。
P4-B 普通 user owner 的原 `user_config` 现仅展示/执行 decision-only 动作，主 Gateway 回合从宿主当前 RunParams 取可信 thread，子代理只能用自己 runner 身份；原设置回执保持 `revision` 出现在有界模型预览内，CAS 不放宽。隔离真实中文第二轮由模型自主 `decision_read(thread)`、`decision_patch(thread,14/0)`，原事务回执与线程文件显示 14/1 和预期三项生效，模型准确回复；没有第三次独立 read。首轮失败、底层修复、私有恢复及单 Gateway 证据见[交接](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md)。
隔离主会话 `apply` 三条普通中文新会话已验短期限、`need_data`、选择当前 M2.7 三种真实保留：5 次官方 Jev HTTP（1 次超时输入未知）、4 次官方 M2.7 HTTP 200，三轮都完成且无异模业务请求。调整期限只作用私有测试设置，最终原 CAS 恢复为 off；**真实跨 provider 自动采用仍未出现**，不能用 fake 阳性代替，见[真实交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。
P5-E/F/G/H 的只读审计已完成：原 Goal 事后 token 口径不是决策 input-only 的请求前预算，ModelCallLedger/验证证据也不能自行授予实验权或证明业务收益。首片在原设置事务加入内部 `restore`：同一层 set/unset、完整 owner/thread CAS、一次版本前进和读回；后改冲突保留用户值。实验授权、请求前限额、对照质量及自动应用尚未实现，详见[审计](docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md)。
E1 原语已把默认关闭的实验开关、有界 thread 授权信封与撤销放入原决策设置 v2，并在原 ModelCallLedger 同锁内串行预留请求数/声明的完整输入量；本地组合 356 项通过。当前没有宿主用户授权入口及原操作提交证据，声明输入量也不是可靠 token 上界，发送前硬门尚未接通，因此实验联网依旧失败关闭，不能称完整 E1；后续 E2/F/G/H 未实现。见[交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md)。
E1 后续只读核验发现官方示例的 `usage.input_tokens` 高于该请求 JSON 字节数，公开 64k/32k 上下文说明也没有明确绑定完整服务端包装后的实际输入账。因此不能把 UTF-8 大小、原估算器或自填整数当硬预算证明；优先取得供应商固定 endpoint/模型/计数口径的完整输入上限，再于原实际发送接缝绑定最终 wire、授权回执、业务身份与账本预留。原观察器吞异常，不能承担硬门；在此之前实验继续关闭，详见同一 E1 交接。
P5-C 自学习点的[只读审计](docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md)确认：现行 runner lesson 进入 owner CandidateService，旧 learning_drafts 只有迁移读；没有受用户确认约束的 Skill 提案/正式写入链。Jev 可在来源明确时观察候选，不能凭评分写 `SKILL.md`；apply 须先建唯一提案与确认入口。旧 README/指南的 `enable_self_learning` 和 `my-agent learn` 已实现说法已按当前代码校正，AGENTS.md 的确认规则仍为后续实现约束。其后 S1 提案/确认链已在本地分支实施待审，见本文顶部“自学习 S1”条目。
P5-C 规划点的[审计](docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md)区分现有 Todo、workflow plan、Goal 与 create_subagents 的权威。默认关闭的 `planning` 首片已接当前主代理原 `task_progress(read)`：只对2–24个 exact open Todo ID 提示一个优先评估项，版本/本轮问题失效不采用；原账本、Goal、派工均不变。离线133项通过；隔离 Jev 先遇4秒真实超时并保留原回执，8秒对照成功建议精确ID且只追加软提示。Gateway TUI 质量和多次读取延迟待验，见[交接](docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md)。
P5-C 交付质量点的[只读审计](docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md)限定 Jev 只能从本轮原 verification 或已确认 ready artifact 的精确引用中建议一个复核焦点，供主模型参考；不得评分设硬门、造产物、改 Goal/child/最终回复状态。原归档后的 text/native 共用展示接缝可复用，候选 ready 来源与独立设置尚未实施。
P5-C 动作候选的[只读审计](docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)发现本机 Browser ref 模块尚无生产工具引用，Computer Use 经原 MCP/审批链但 OCR 内容没有宿主验证的 observation/candidate ID 与代次；Jev 暂不能直接选坐标、命令或输入。先让现有工具来源产生可绑定、可失效的结构化候选，再做归档后软提示，不重复 `skill_tool` 工具路由。
早期全仓集成 gate 发现本线新增 13 处跨层导入：Gateway 模型采用/观察直接调用 `agent_core`，Compact 重建从 Gateway 反向读 core 运行类型，`user_config` 从 Tooling 反向读 runner context。现已把同一请求选择及可信运行上下文移至 `agent/` 共用层、跨 Gateway/core 的采用和 Compact 编排移至应用层，删除旧模块，没有扩宽 import 白名单或新增只转发 facade；守卫 **0 findings**，相关 10 文件 **268 passed、4 xfailed**。完整严格 gate 仍未通过，见[测试总览](TESTS.md)。

最新用量合同：决策用量沿原 LLM 用量行增加输入 token，输出位置预留且当前留白；原 usage、请求状态、期限与资源预算保留。决策请求不进入 USD 价格估算或 owner/run 成本累计，本地决策模型沿用同一合同；普通生成模型原有成本机制不在本设计变更范围。
用户要求可见进度：完整 Goal 顶部维护 18 项验收 TODO，组件与可用产品分开；先打通可用链路，再扩接业务，不用测试数量代替整体完成度。
开发调度按任务难度选模型：普通实现、测试和文档可用 Astra high 或 GPT-6 Sol high；较难的独立开发可用 GPT-6 Sol xhigh，复杂跨模块状态、并发与模型切换审查用 Astra max（最高档，不使用 ultra）。这是开发代理的算力分配，不进入 my-agent 子代理逐次模型选择的人机流程。
已本地接入工作片展示投影，减少Skill名卡/工具目录及明确可选类别schema；原权限/快照身份保持，被收起项由原搜索找回，本地HTTP及原运行接线组合已验。
metadata作为对照，progressive才覆盖额外direct插件schema减量；默认总开关关闭，完整细节见模块计划。
策略/开放类别列表复用原设置、CAS和菜单；真实Jev否定跨题选择槽，已改每候选独立适用性题，候选说明只发一次，超限沿原输入。
真实API概率会按百分位舍入，wire按量化边界检查并保留原值；原失败样本replay及实际复测见真实验收记录。隔离 Gateway TUI 已跑通普通回合与保留原模型的子代理批次，完整窗口和跨模型切换未通过。
缺目标/合同/步骤/环境保留原输入，明确无需才可空短名单；采用前复核原Skill范围和固定插件代次，不自动补资料或扩大授权。
设置范围现以同一登记表约束投影与运行：Curator 仅 owner 后台，旧线程后台覆盖仅可清理。原菜单和 agent 共用显式探测，读取/保存不发模型请求。

原模型目录当前私有 v5、共享发布 v2，会话当前 v10，显式迁移旧记录并保存原 owner/thread 决策覆盖；不新增配置文件。
共用设置服务与原 user_config 的读取/修改/恢复继承已本地接线，双层版本 CAS、时间校验、失效服务仍可关闭。
原账本增加互斥用途分区、逐字段真实用量、单调终态及 worker 精确保留；原用量增量及同一行决策输入已接线。
原生 Jev、严格 HTTP、有界调用与取消/准入已接入实际决策服务；配置/本地 HTTP/原账本/活动显示联合通过。
阶段预算、连接冷却、在途关闭及迟到建议拒绝已本地验证；Curator 已接通，原设置菜单/原运输/本地原生HTTP已组合接通；实际安装版 TUI 尚未验收。
短决策只刷新当前显示，不等待会话写锁；原持久用量和持久显示继续由原调用边界/收尾负责。
这些组件不代表当前安装版 Jev 已可用；当前已验/待验边界见完整 Goal。
首个业务消费者 Curator 已本地接通：用户后台显式绑定原 run，不借历史会话；临时标注不删材料、不写游标。
缺数据/无需/无匹配/弃权逐题区分；可调决策时间受原 lease 剩余头寸约束。子代理选择与召回重排已本地接通；原权限/幂等/记忆预算保持权威，完整窗口与真实模型验证未完成。

按最新讨论，Jev 作为原模型体系的 decision 用途接入，复用 owner 配置、预算、取消、账本和原业务入口；
不以独立 MCP 判断工具作为主要产品方案。默认关闭，逐点观察/应用；建议前台单次 2 秒、阶段累计 4 秒、
后台标注 4 秒，默认零重试，报错或到期保留基础方案，迟到结果无提交权。主模型与权限合同不变。
2/4 秒只是可调默认值，支持按接入点覆盖；用户设置与 my-agent 按明确指令代操作首版即共用配置服务，
读取有效值与来源、按版本修改并读回，不依赖 Jev 在线。配置热生效于后续请求，关闭阻止旧建议应用，不改原业务结果。
首批是子代理模型选择与原 Curator 前置标注，随后扩展召回和能力推荐；完整窗口/缓存、缺数据和并行边界见
[决策模型接入计划](docs/design/DECISION_MODEL_INTEGRATION.md)。完整清单见[执行 Goal](docs/tasks/DECISION_MODEL_GOAL.md)，
另一任务已确认 P1-A 范围无重叠；本线独立实施，P5 纳入总完成条件，不改变对方 Goal。

卸载已本地接通，完整真实验收待显式业务调用接线后进行；设计见 [卸载边界](docs/design/PLUGIN_ACTIVATION.md#卸载与重新安装边界本地实现未发布验收)。
显式调用复用原 HostCommand/ToolExecutor/MCP 与审批请求链，命令运输已携带自己的审批生命周期；
显式 slash 不构成自动批准，旧激活的会话批准不能复用到新代，见 [业务接线](docs/design/PLUGIN_ACTIVATION.md#显式业务调用的待接线边界)。

第 4 步本地源码已接通安装、配置、实际启用、普通工具组合、停用释放、重新启用及卸载。
卸载使用释放返回的完整记录做原锁 CAS；原成功结果持久化并严格读回后，才消费退出证明和回收无人引用的旧包。
原准备仍在执行时保留安装与环境；同包重装保留其包，旧请求不控制新安装，UNKNOWN 不因后来清理成功被改写。
公开目录 v3 派生原提交安装引用，避免同包卸载重装后旧目录再次有效；安装表和原操作历史不新增权威副本。
业务调用和 TUI/Gateway 交互审批运输已本地接通；同机连接沿原 GatewayPaths 和服务端规范 owner 固定审批地址。
原 HTTP 请求线程执行命令，消息流只运输审批和原结果；退出只取消自己的等待，不新增执行或审批账本。
完整多 TUI 装卸仍待完成；运输及恢复边界见 [宿主命令](docs/design/HOST_COMMAND_EXECUTION.md#请求与运行)。
本片未推送部署、未新增实际 TUI，不进入第 5 步。
详细顺序、原 executor 校验、v4 保留及消费合同见 [激活释放](docs/design/PLUGIN_ACTIVATION.md#管理停用的组合边界)。

以下前片约定中的待实施状态以本段为准。

第 4 步实际 enable 与新运行工具组合已有本地实现：沿原操作准备环境，完整验证 MCP 目录，确认候选退出后提交 active。
Registry 只从 core 注入的可信 owner 读取已启用代次，构造不启动服务；共享连接缓存同代代理，各权限视图分别投影。
私有设置仅交给子进程，插件 effect 自述不降低审批；永久关闭与迟到客户端登记双向复查，避免关闭后启动。
原 termination 新增可选 cleanup 保存完整 session 证明，单调保留且不改原命令退出事实；UNKNOWN 不以 PID 消失消除。
完整装卸和实际多 TUI 仍待完成，详见 [启用组合](docs/design/PLUGIN_ACTIVATION.md#启用与新运行的贡献组合)。

第 4 步本地管理停用已接线：安装表先撤销，原准备 attempt 关闭后按完整执行归属冻结，共享连接按原激活冻结。
两类资源均锁外使用原清理器；终态准备命令也核对 host，坏原操作或未知清理不能报告完成。
退出证据仍保留在原资源账，激活保留 revoked；后续 release 须先核验资源再 CAS，原操作结果持久化后才消费退出证据。
不能先删证据再清激活；删除环境还须确认原准备执行器退出，不能只看 cancelled。详见 [激活合同](docs/design/PLUGIN_ACTIVATION.md#管理停用的组合边界)。

第 4 步当前内部实现：固定 PluginActivationRef 沿唯一安装表跨进程复查，插件 MCP 接原托管管道与资源回执。
发送的原资源短锁在连接准入锁前，等待可取消；不持锁等服务端。未发送拒绝和可能已发送的失败分开进入原操作账。
旧代理固定连接，未知清理/启动禁止替代；完整管理装卸与实际 TUI 仍待完成，详见 [连接合同](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。
以下早期切片保留当时设计边界，当前组合进度以本段与执行 Goal 为准。

第 4 步原 host 的[标准字节管道](docs/design/MANAGED_PROCESS_STDIO.md)与 v3 激活归属已有本地实现：
端点直接继承，launcher 退出收回原资源；共享连接不借用业务任务身份，旧 v2 原版本恢复，完整 MCP 准入仍待接线。

第 4 步唯一安装表的激活 CAS 已本地实现：准备、发布、撤销同源，旧快照不得改绑新代；资源退出证明仍沿原进程账。
状态、显式 v3 迁移、撤销与配额边界见 [激活权威](docs/design/PLUGIN_ACTIVATION.md)，完整装卸尚未开放。

本地开发：MCP 已分离客户端、固定连接和协议响应箱；永久关闭先撤销，临时断连只清理原连接，未知清理阻止替代进程。
目录发布复查同一连接，权限视图不再复活已关闭客户端；跨进程插件激活/撤销仍待接线，见 [连接合同](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。
本地修复：模型工作片权限视图已迁移到原共享 owner_access；删除 helper 后的遗漏调用方已修正，不增加权限别名或兼容层。

本地实施中：插件环境准备复用原 operation 的完整 logical 资源声明和原 ProcessSessionStore；不新增进程账或操作 checkpoint。
固定计划及原 claim 先于写入；准备进程绑定原 owner/thread/task/run/attempt、宿主寿命及截止时间，普通后台仍按旧默认独立运行。
完整激活及 MCP 撤销仍待实施，见 [插件环境合同](docs/design/PLUGIN_ENVIRONMENTS.md)。

本台账保留当前决策、设计入口和未落地边界。逐次排障流水不作为产品规范；实际实现以代码、配置和结构化协议为准。

## 已采用的原则

第 4 步[配置接线](docs/design/PLUGIN_PACKAGES.md#配置接线第-4-步本地开发中)已进入本地实现：
明确 `/plugins configure <插件> --file <JSON>` 沿原管理员、路径权限、宿主请求和工具账执行，值只写 owner 私有安装表。
安装表 v2 在同一次提交保存配置、版本与回执；原 v1 显式读取并在首次修改时记录源摘要，查询不迁移写入。
目录 v2 加安装版本使配置变更也能拒绝旧请求，客户端和宿主须同版；原未知结果不重跑。启用、发布与撤销仍待完成。

第 4 步[本地插件包](docs/design/PLUGIN_PACKAGES.md)正在开发：静态校验与默认停用安装事实已有本地源码，包不可携带 owner/启用/激活身份。
显式管理请求的[宿主执行身份](docs/design/HOST_COMMAND_EXECUTION.md)已有本地接线：原 RuntimeDB 绑定独立运行，原工具执行器执行；HTTP/direct 复用原管理员权限，重送不重复执行，查询只读原结果。
本地取消记录已保留原领取元数据；执行权关闭与结果可读性分开，损坏原文不覆盖，严格查询明确拒绝。该开发修复不代表新一轮真实 TUI 资源停止验收。
唯一安装表沿 canonical owner data，版本与最后提交回执原子保存；原请求重试不覆盖新状态，清理异常不抹掉提交事实。
公共目录锁复用原后台系统锁，保留锁名/顺序并拒绝链接；原配额准入先于插件锁，没有第二套操作历史。
本地安装只保存停用包；开关关闭后仍能查询原请求，超时保持未知。独立环境、激活及撤销尚未完成，完整装卸未发布验收。
独立环境按[环境合同](docs/design/PLUGIN_ENVIRONMENTS.md)已有内部准备实现：固定地址标准 venv、本地 wheel 闭包、文件覆盖检查和安装后宿主读回；原配额非阻塞准入，进程沿原取消与退出回执。未接启用命令，原安装表独占发布权限。

第 3 步参数合同源码已接入：公共目录的动作声明驱动解析、帮助与补全，Windows 反斜杠保留，
完整引号错误不降级，部分输入和候选使用同一绑定规则。CLI/HTTP 的管理帮助只读可用，装卸和业务仍未开放；
owner 目录与提交版本已由后续宿主片接线，真实激活代次和执行权限留生命周期实现。设计与参考边界见 [参数合同](docs/design/PLUGIN_LIFECYCLE.md#第-3-步参数声明与解析)。
参数与补全修复已发布同版双机；TUI 146 复验 Tab 后正常 Enter，两端各 13 类命令检查通过，143 原失败保留。
150 已验活动请求的静态分流与普通正文保留；不代表动态插件目录或整个第 3 步完成。
已发布同版双机的[宿主目录与提交绑定](docs/design/PLUGIN_LIFECYCLE.md#第-3-步宿主目录与提交绑定)：
沿原可信 owner 生成声明快照，客户端只持展示缓存和首次选择的 revision，过期提交不自动重放。TUI 151—153 所测框架入口通过，第 3 步本轮范围收口；实际贡献为空，安装与业务执行未实现，不能用本片代替真实插件换代验收。

模型归因新增四组对照：my-agent 分别使用官方 M2.7 与 OpenCode Flash，Codex、Free-Code 使用同一官方 M2.7。
状态：用户已明确选择框架功能与模型交付质量分开记录，Goal 恢复 active；第 1 步框架基线收口，第 2 步进度纯计算、供应状态与唤醒/Goal 路由已分离并部署，相关实际链路已验；租约/恢复与稳定资源换代修复也已同版部署，相关实际验收和资源收尾完成，第 2 步本轮框架范围收口，第 3 步公共声明与命名空间首片已发布并同包部署；实际 TUI 137 暴露的资源停止缺口已修复并由新版 TUI 138—142 分项复验；参数与宿主目录片也已发布部署和复验，第 3 步本轮框架范围收口，第 4 步静态包与安装事实已进入本地开发，装卸链仍待完成。命令公共声明、文件队列守门和旧控制分派的扩展边界已记入[首片接线核对](docs/design/PLUGIN_LIFECYCLE.md#第-3-步首片接线与实现边界)；宿主目录已接通，真实安装/激活贡献与装卸仍待做。my-agent 两模型短题通过，Flash 核心长题通过但报告有保留项，M2.7 长题跨框架失败保留。

任务资源停止边界已实现、发布并完成本轮分项实际验收：显式停止覆盖主代理和原子树启动的后台/PTY，暂停 Goal 与回合中断仍各守语义。
先沿原 task/run/attempt 关闭执行和派工权限，再冻结原清单，锁外只清理固定实例；停止不能追随后来恢复的新任务。
访问范围、完成通知与执行归属分开；v1 不猜迁移，v2 以同一 Store 的目录锁、CAS 与 redo 持久化启动预留、host/child 绑定及交接。
已交接独立资源不受旧回合 token 控制。终态业务历史与 UNKNOWN 锁保留，独立 runner 用原心跳转交精确取消，宿主只读核对退出。
子代理创建、换轮、放弃、授权恢复及取消共用原 creation guard；managed 运输准确 pending ID，文件模式一次性消费原非空 launch/attempt。
旧快照不回滚新启动；再次停止撤销终态上的新预留。插话沿排序 turn 锁修复原批次、复读 pending 后准确预留，网络重试只接续本条消息。
direct/local 与未晋升请求使用正式绑定及原锁；读取失败、错代次或已晋升不能降级猜测清理。
参考本地 Codex `578c1b2` 的独立控制和关闭入口/固定集合做法；不创建第二套状态，不按共享宿主 PID 强杀。
设计细节见[任务资源停止](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md#任务资源停止修复)及[整任务控制合同](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md#整任务控制接线)。
TUI 138—142 的框架证据、模型脚本失败及真实覆盖限制见 [TESTS](TESTS.md#整任务停止发布与实际-tui-138142)；137 失败原样保留，后续宿主目录验收另见本页首段。

Working 图标间歇消失：状态为**用户明确延期、尚未复现**。当前只记反馈，不修改活动计数或动画策略；
在本轮十步目标完成后与用户一起采集真实状态时间线，再区分正常展示切换与活动事件丢失，不作为当前目标的验收阻塞。
推进依据是本步实际框架证据；模型已核实的程序、计算和报告失败单列，不改原整轮结果。框架缺陷、必测覆盖缺失及未明归因不豁免。
同题、同初始输入和独立会话的结果与原生上下文共同定位根因，不以模型口头成功或通过组数代替证据。
具体控制变量、协议适配边界及当前覆盖见 [模型与框架对照](TESTS.md#模型与框架对照)。

runner 失败分类已修：复用 `turn_end.py` 的唯一映射，正常让出保留 `PENDING / interrupted / ok=False`，
但不补 `runner_error` 或写入最近错误；正常完成同样清当前旧错误。显式失败、未知原因、状态冲突和授权阻塞保持原判据，
不改调度、尝试历史或持久格式。状态合同与验收边界见 [轮结束设计](docs/design/closeout_state_machine.md)。

线程、消息、任务关联与 Audit 的显式组合已实现并验证主要运行路径；Store 的领域继承链现已全部移除。
`threads` 唯一持有新会话模型解析器；消息依赖线程校验/原子更新，任务终态显式关闭进度策略；
Audit 共用同一任务账本、命名锁与任务锁，移除原转发包装。定向 2,206 项通过，尺寸基线未扩大。
插话事务分层已实现：回执格式与校验独立，账本读写及索引修复共用同一 storage，提交批次、确认批次和恢复各自持有明确依赖。
组装入口已删除最后一层继承；回合锁、回执锁、批次先提交后修复的次序及原持久迁移保持。
新版真实 TUI 已验完整运行中插话和并行长命令。用户明确三类控制后，修正此前将 Goal 暂停视为停止资源的错误预期：
Goal pause 只关闭目标自动续跑；interrupt 只停止当前模型/工具执行链；明确停止任务资源才关闭归属进程、终端和子代理。
代码已移除 Goal 暂停/清除的中断回调；有无 Goal 或 Goal paused 都不能让 interrupt 落入任务停止，子代理自然完成也不伪标中断。
已实现并通过真实控制验证：PTY 执行归属与访问权限分别保存；明确任务停止按精确 owner/thread/root task 或 run/attempt 收口。
启动预留在停止时标记，句柄冻结后异步终止，旧停止不重新扫描恢复轮；进程树退出需实际核对。
不按整段会话、命令文字或文件路径批量杀进程；参考和语义边界见 [长任务执行](docs/design/LONG_RUNNING_EXECUTION.md#pty-的执行归属与任务停止)。
后台续跑默认工具目录现复用统一命令/会话工具组，修复恢复时遗漏交互终端的缺口；显式配置及 owner/task 限制仍有效。
该目录修复与三类控制分开验收，不改变暂停/中断/资源停止语义；267 项定向及真实工具准入、子代理续采和主代理 PTY 保留已验。
后台启动仍经审批门，主代理恢复轮使用文件查询；旧句柄的后台读取与报告质量不因目录修复而自动算通过。
后台主代理审批桥已实现并通过本机真实 TUI：复用原主任务审批目录、完整工具请求和 TUI FIFO，不新建权限或持久状态源。
主代理额外绑定当前 claim；换轮、取消、坏账与无交互接收方均关闭式失败，Goal paused 不撤销当前回合的审批。
只为当前会话选中的主任务开放审批写回，其余子代理查看、插话和停止的授权边界保持。
428 项定向通过；暂停 Goal 后原回合仍可审批，中断后新 claim 读取同一 PTY 并审批输入，程序只启动一次且正常退出。
拒绝样本的 handler 未执行，未产生替代调用。合同和边界见 [后台工具审批桥](docs/design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md)。
安装版长任务发现父会话任务关联被误当成子代理执行身份，审批在发布前失效；已在原归属读取处修正并同版部署双机。
实际 TUI 已验子代理/孙代理批准后继续原调用，以及拒绝后 handler 未执行；数据与报告质量另记，不把审批通过当作整体验收通过。
canonical child 记录优先于会话任务投影，仅不存在时读取 main claim；坏账不回退，主子孙仍复用同一审批账本。

存储组合首片已实现并验证主要运行路径：模型用量通过 `model_usage` 领域对象访问，只接收原账本目录、线程读取和原子更新能力；
Goal 时钟由 `goal_clock` 共享对象直接持有操作与状态，不再沿继承链暴露计时方法或复制锁/字典引用。
JSONL 读取与错误报告下沉无 Store 依赖的 IO 模块，调用方同步迁移；文件格式、锁路径、CAS 与提交顺序保持。
详见 [第四批重构](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第四批存储能力由深继承转为明确组合)。

存储公共上下文与 claim 迁移已实现并验证主要运行路径：目录与路径统一由 `store.storage` 持有，扫描投影归其 `indexes` 对象；
基础类中的 `wake_delivery_receipt` 回归唤醒领域。各调用方同步改用显式上下文，不保留旧根目录字段或路径方法转发。
执行租约经 `store.claims` 领取、续租和提交终态；目录初始化、路径、索引失效、TTL、恢复归属及原子锁语义保持。
旧基础类和 claim 继承类删除，归档维持原策略后租约的调用顺序，不新增长期任务。
本片定向 1,065 项通过、4 项既有 xfail，双 TUI 的暂停静止、压缩重连、真实续采、并行插话及原生投递去重已验。

Goal、观察、唤醒与进度策略已改为显式组合，并完成本片主要运行路径验收。唤醒接收观察领域的确认能力，
保留先发布 wake 再追加观察的顺序；Goal 接收共享时钟、线程校验和任务读取能力，不反向导入 Store。
跨领域旧账维护留在组装入口，依次调用策略归档和 claim 归档；通用 JSON 对象读取下沉既有 IO 模块。
原 208 个函数/方法及 32 个生产调用文件核对，无非预期逻辑变化；1,236 项定向通过、28 项既有 xfail，尺寸基线不变。
新版双 TUI 验证暂停/压缩/重连续采和并行批量中文数据，报告取消原因错误与临时脚本选错目录的样本保留。

Gateway 请求职责拆分已实现并验证主要运行路径：上下文读取与准备、模型输入渲染、历史提交/补交、请求身份与执行车道分别归位，
请求执行器保留编排和结果映射。前后台完整历史行选择下沉 conversation 领域，删除后台对 Gateway 执行器的反向导入。
不新建存储副本或状态机，不改变 claim、Compact、追加/补交及终态顺序；无生产调用的历史入口直接删除。
详见 [第三批重构](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第三批收窄-gateway-请求适配)。

子代理跨进程停止已修、定向及本机真实 TUI 已验：dispatch 进程退出后，新会话命令仍写入的缺陷已复现并修复。
历史发布版的子代理取消和失联回收共用工具层进程树终止原语，保留完整性、出生标识及未确认 PID 回执；
现行发布版已将明确取消改为固定工具资源清理和 worker 内协作中断，上述整树停止合同优先。
仍由调用方先证明宿主独占，共享宿主和 Gateway 不允许按 PID 终止。已删除仅杀进程组的重复实现。
无句柄的 POSIX 退出核对补充内核僵尸状态，不回收他人 Popen；无法核对时仍返回未确认。
OS 强制终止不能保证子代理补写原生历史，已有取消账和 attempt fence 仍为恢复依据，不能伪造缺失工具结果。
详见 [重构设计](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第三批收窄-gateway-请求适配)。

仅思考响应保留已实现、真实复验中：有正文、工具或有效 typed 思考都表示响应有内容；
思考不是公开答复，不因无正文在适配器内偷偷重采样。既有零工具有界续跑先追加独立原生轮，
不按未增加的工具轮号合并；未执行工具块不回放为调用，签名/密文和用量保留。
不新增模型完成判官或长思考时限，慢模型单次输出自身复读仍单独排查。
详见 [响应历史](docs/design/LONG_RUNNING_EXECUTION.md#仅思考响应与续跑历史)。

工具正文二次裁剪已修、定向及真实出站已验：归档日志预览不再代替已分页的模型结果，
完整尾部和继续读取参数共同保留；大输出外置、脱敏和权限合同不变，不改旧历史/缓存前缀。
详见 [工具结果投影](docs/design/tool-runtime-unification.md#重复工具观测与恢复)。

Shell 人工长度门已删除、真实长命令已验：合法脚本不再因 2,000 字符上限被前置拒绝；
Schema/handler 同步，原安全及进程预算不放宽，不自动拆脚本或扩大权限。
两项修复不等于慢模型复读已解决：新会话仍重复读取，相同失败输入关闭服务缓存也未换路。
不据此关闭产品缓存。用户开启思考后，原生思考已收发保留，真实新任务仍重复读取未交付；
不能把“开启思考”当成已经解决。服务端采样单变量对照单独留证，不修改日常连接或静默切模型。

渠道故障软提示误分类已修、定向已验：只统计 canonical 错误分类中的网络可重试/工具不可用，
按 tool/call_id 消费最新回执。测试/编译非零、参数、状态、权限、取消和未知失败不再触发换渠道提示。
原错误、工具执行、审批和恢复合同保持不变；真实正常 TUI 66 组工具后自然结束，
没有误加渠道故障提示，8 项测试及原始/现有数据独立复算通过；不据此关闭慢模型复读项。

后台缺模型等待已实现、真实 TUI 已验：本地 `ModelNotConfiguredError/ModelProfileError` 等待该会话配置恢复，
不因 30 秒届满反复执行。普通错误仍按原配置冷却；网络限流与远端请求拒绝不误当本地未配置。
进程内退避有界且线程安全，不消费持久唤醒、不改 Goal、不选择默认模型；模型选择仍以 canonical thread 为准。
详见 [长时间运行](docs/design/LONG_RUNNING_EXECUTION.md#后台异常退避)。
真实验收补齐 Goal 的旧错误收口分支；缺配置不消费 wake 或终结目标，选好模型后原任务自动继续并完成。

TUI 持久请求回执优先于 Gateway 瞬时存活检查：已经取得规范 request ID 的消息沿原终态等待，
不能因重启间隙 PID 不可见而宣告未执行。未提交请求保留服务检查，超时/取消不变、不自动重发。

进度跨轮补充已实现、原会话 TUI 已验：`task_progress` 显式 `run_id` 可以指向同 owner/thread 的已存在计划，
默认仍写本轮；不以最近读取、正文或同名 item 猜目标，不选择工作区或改变旧任务生命周期。
子代理仅写自身，独立后台工作不混入；接口和运行权限由同一解析器校验。
已完成项的 notes 属于可维护备注，后续验证可直接更新；状态/结果的显式更正规则保留，证据仍追加去重。
本地队列展示补修已实现、原会话 TUI 已验：CLI source 与 owner provider 分工明确，owner/鉴权不变，
本地前台消息按私有通道保存和重放。HTTP/IM 未因 rich transcript 开关扩大路径可见性。

已实现、真实串行对照已取证：三种模型接口统一遵守显式温度开关；未启用时使用提供方默认，
不再由 Chat/Messages 隐式注入低温。模型级显式值、工作片冻结与摘要调用的显式覆盖保留。
采样差异是已确认的请求问题，不能据此断言所有复读都由温度造成；不增加循环硬停或历史改写。
见 [模型采样](docs/design/TUI_MODEL_PROFILES.md#采样参数)。

执行事实投影瘦身已实现、定向及真实出站已验：原生工具回执已带完整顺序，下一轮只追加最近完成工具批次的权威状态，
不再每轮重抄近期成功/失败明细和聚合账本。旧历史、原始账本与已有缓存前缀不改；最终操作核验仍保留全轮汇总。
批次由宿主 turn_id/tool_round 标定，不从模型文字推断；不新增质量验收或循环中止门。
代理树轮询的稳定进展补修已实现、定向已验：复用已有成功软观察，计时和心跳变化不当成真实进展，
不追加新的等待器、杀进程策略或完成门；完整状态、权限与生命周期仍保持原样。
子代理查询自己的子树时按规范范围裁决排除查询者自身；显式查询其它代理不排除目标节点。
进度部分更新补修已实现、真实续写已验：Todo 与覆盖目标未传状态时保留原值，新建才默认 pending。
更正开关不是隐式重置状态的授权；显式状态、完成事实保护和参数校验不变，不猜测或重写旧账。
Todo 项的原生 Schema 同步声明 ID 必填，消除生成与执行合同不一致；不以错误重试代替必要字段约束。
验证软提示不再要求逐动作重新检查：相同版本、输入和观察点复用有效证据，信息足够后推进实现，
针对改动验证并如实汇报；权限硬门不变。此项修正冲突引导，不宣称单靠提示可治愈模型循环。

已实现、定向及真实递归协作已验：子代理创建和续派按其 canonical thread 模型引用构造后端，
删除从共享调度宿主隐式捕获连接的路径；显式测试/嵌入注入按线程隔离，并行工具仅传本线程快照。
见 [会话模型](docs/design/SESSION_MODEL_SELECTION.md)。不增加部署默认模型或失败后换模型兜底。

已实现、定向与真实 TUI 复验中：停止不投递回复，但保留中断前原生工具历史；异常先交同宿主 canonical
出口再抛回原错误。前台、子代理及 CLI 共用运行回调；后台工作片也先保存 native，再按原规则投递公开 final，
两者用独立宿主回合编号关联，冻结重投不改编号。不建影子会话；缺结果只标未知，不伪称压缩或成功。
边界见 [上下文](docs/design/CONVERSATION_CONTEXT_DESIGN.md)，历史缺失与慢模型自身复读分别验收。

已实现、定向及正常 TUI 主链已验：受管命令可取消长等及原 wake 队列终态通知；自然收尾后的欠报通知
必须在队列和执行准入共同核验，回执未写成功暂不消费。请求摘要只诊断客户端前缀，不推断服务端缓存、
不改历史。旧子树阶段快照不再拥有完成否决权，实际子树和未读邮箱保持权威。
慢模型长任务仍复读，不能以健康流或高缓存宣称任务完成。配置和边界见 [长时间运行](docs/design/LONG_RUNNING_EXECUTION.md)。

已实现、定向回放与新包 TUI 主链通过，长循环采纳效果另验：后台进程将状态、日志增长与耗时分开，稳定进展摘要只进入
成功重复软观察；原始结果、权限与终态不变，不因沉默强杀。见 [工具恢复](docs/design/tool-runtime-unification.md)。

已实现、定向及真实 TUI 已验：客户端不活跃等待使用单调时钟，绝对 deadline 仅在边界转换一次；
系统校时不能伪造请求超时，客户端脱离不等于后台任务失败，不自动重跑。见 [TUI 恢复](docs/design/TUI_DESIGN.md)。

已实现、真实慢模型复验中：成功重复计数采用宿主完整结果摘要和固定间隔软提醒，不按 Shell 文本猜只读、
不增加任务完成硬门。同端点前台工作期间延后普通记忆策展，保留 pending/游标；单次后台预算传入 HTTP，
超时取消旧连接后再决定缩批。详见 [工具恢复](docs/design/tool-runtime-unification.md) 与
[记忆结构](docs/modules/memory/04-structure.md)，外部应用占用与服务端缓存不由本 Gateway 保证。

已实现、真实账号待授权验收：`/model` 认证复用 owner provider 唯一存储；支持订阅设备码登录和显式填写的
RFC 8628 参数，不导入其他应用凭据、不默认选模型。登录取消/退出/配置变更由代次及请求 CAS 约束，
刷新跨进程串行；OAuth 不发布给其他用户。协议、边界及验证见 [模型账号登录](docs/design/MODEL_OAUTH.md)。

长思考折叠提示展示已接收的原文行数与字符数，排版行与原文行分开；不把计数当作
剩余工作进度，不为隐藏文本全量排版。定向回归与新包真实 TUI 连续增量均通过，
字符数超过预览上限后仍刷新，见 [完整原文](docs/design/TUI_COMPLETE_DETAIL.md)。

状态查询与界面共用同一授权快照，但模型只接收精确身份、状态、原因和真实产物读取顺序。
恢复摘要、检查点和内部目录不作为模型结果引用；超长结果复用原归档与逻辑引用，并明确省略范围。
状态查询的协作说明复用派工纪律，不再另外要求父级先结束回合。完成后无活动目录的查询绑定
本会话；主请求不在子代理表时仍沿精确 parent_id 查子树。定向与双真实 TUI 文件读取已验证。

派工快照与执行建议分开：状态面只陈述各 run 的事实，不根据“仍有孩子运行”推导父级必须等待。
根与递归协调者共享同一分工说明；当前角色只加载自己的冻结行为与可选角色索引，不注入其它角色全文。
清除协调者全部外包、结束后也不得接手等旧说明；保留用户限制、活动写集分工与真实依赖等待。
本项已完成定向与三路真实 TUI 验收：独立工作和依赖等待可正常完成；同会话追加有真实孙级交接。
分工质量仍开放：模型仍会转交本应留给自己的入口、反复查询状态，不能把链路完成等同于效率达标。

产物交接补修已实现并通过真实新包主链验收：自然回复从本 run 工具账本交回真实文件，声明与工具使用同一 cwd；
删除内部 output 重定位与隐式搬运。补丁新增精确文件引用和删除墓碑，沿原工具 registry 交接。
实际分层基线已完成，分工选择不冒充运行时故障。详见
[父子并行与交接](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

| 决策 | 当前约束 | 设计入口 |
|---|---|---|
| 用户家目录是默认工作区 | 不以启动终端目录改变默认归属；家目录内整理依靠提示约定，权限是硬边界 | [目录规范](docs/architecture/MY_AGENT_HOME_LAYOUT.md) |
| 单一身份与事实源 | owner/thread/task/run/attempt 显式传递；索引仅用于查找展示 | [架构](docs/design/ARCHITECTURE_GUIDE.md) |
| 单 Gateway、多客户端 | 客户端关闭与后台任务生命周期分开；恢复不能重复执行 | [Gateway](docs/design/GATEWAY_DESIGN.md) |
| 会话独立模型配置 | 会话选择不覆盖其他会话；用户默认只影响按规则继承的新会话 | [模型选择](docs/design/SESSION_MODEL_SELECTION.md) |
| 软件不预选模型 | 模型/协议/地址默认留空；未配置只开放设置和历史，用户已保存选择不变 | [模型配置](docs/design/TUI_MODEL_PROFILES.md) |
| 模型统计与上下文分离 | 轮次取调用账，缓存取服务商用量，累计按当前代理会话；展示字段不回灌模型输入 | [统计口径](docs/design/TUI_DESIGN.md#模型统计口径) |
| 单代理单持续目标 | 每个代理至多一个未结束 Goal，主子分别归属；active Goal 在安全边界续跑，不依赖工具数量或 Todo；旧冲突仅显式迁移，不自动激活 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 目标计时只有一份基线 | 同一 Gateway 的同源会话存储共享单调时钟；模型轮次、后台工作片及控制查询不重复累计同一秒；旧多记耗时不从日志推测回写 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| Goal 终态不冒充执行终态 | 完成目标只停止该目标的续跑；当前回合、消息和子树完成后由统一 finalization 关闭任务，不在目标工具中提前关父任务 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 名称不决定执行通道 | Goal 名称不创建额外执行器；已有未结束目标时新名字返回冲突。后台数量不授予前台执行权，父级取精确任务身份 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 目标编号不证明仍可续跑 | 轮限交接重读当前 Goal 状态与任务绑定，旧编号、已完成或暂停记录不产生自动续跑承诺；不新增完成质量判断 | [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md) |
| 主子代理共享会话能力 | 历史、插话、停止、压缩、终态以相同底层协议处理 | [子代理](docs/modules/subagent/04-structure.md) |
| 逐项交付与阶段诊断 | 创建不强制等待、成功不等全树；原执行车道接收结果，长静默只诊断、不强杀或自动替代 | [并行执行](docs/design/SUBAGENT_PARALLEL_EXECUTION.md) |
| 终态与通知可恢复 | 先持久化结论，再提交账本，最后按精确 attempt 去重通知 | [收口](docs/design/closeout_state_machine.md) |
| 执行器退出是独立事实 | 进入/退出登记属于 exact attempt；没有 session 可核对 OS 身份，未知副作用保留封存 | [收口](docs/design/closeout_state_machine.md) |
| 恢复游标不等于消费 | 未消费事件分页轮转；成功写入 canonical WAL 后才记回执，失败项可重试 | [收口](docs/design/closeout_state_machine.md) |
| 完整历史与展示窗口分离 | 完整未压缩历史来自 canonical 消息，分页不能裁掉模型记忆 | [上下文](docs/design/CONVERSATION_CONTEXT_DESIGN.md) |
| 权限硬、任务组织软 | 自然语言不决定运行状态、越权、任务归属或验收结果 | [开发规则](docs/development/DEVELOPMENT_RULES.md) |
| 分层验证 | 确定性合同/替身/回放用于开发反馈，真实 TUI 用于最终验收 | [测试分层](docs/design/main-agent-contract-testing.md) |

## 代码体检与后续拆分计划

下一轮已将插件方案并入“拆清依赖”计划，按以下顺序推进：明确依赖与核心边界 → 后台调度拆分 →
公共命令声明/解析 → 最小插件装卸 → TUI 发现/使用 → 并发长任务与故障验收 → 子代理生命周期/交接 →
模型工具循环 → 剩余 TUI → 约 10 个简易插件与组合验收。纯结构批保持行为等价，新能力单独验收。
按用户新范围移除本轮 Audit/摄取重构，现有功能保留；第 9 步补声明式展示和可撤销只读订阅，第 10 步用自有插件检验能力组合。
具体切片、理由与完成标准见 [下一轮结构整理顺序](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#下一轮结构整理顺序待实施)。
用户已授权先将当前基线推送 main、统一部署本机及确认的测试机，再创建十步执行 Goal；详细矩阵见 [执行记录](docs/tasks/REFACTOR_PLUGIN_GOAL.md)。
第 1 步已记录调度、租约、子代理、工具循环、命令和扩展注册的[依赖与副作用清单](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-1-步依赖与副作用清单)，框架基线按执行 Goal 的实际证据收口。
第 2 步首片将无进展计数和确定性失败退避移至 `conversation/background_progress_policy.py`；只输入标量事实，运行后读取、持久记账、租约与供应退避状态保留原位。旧函数删除，不保留转发层；新切片验收独立记录。
供应退避次片已独立为 `background_supply_backoff.py` 并同版部署，TUI 114—119 已验；状态、执行守卫和事件日志同属一个模块，runtime 构造时读取配置并持有三类消费共用的唯一实例，详见[迁移边界](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-2-步次片进程内供应退避)。
第 2 步路由切片已在源码分为 `background_goal.py` 与 `background_routing.py`：前者只接收原 Goal/任务/时钟领域和精确查询、发布能力，后者只接收三个只读回调；删除混合职责的 GoalMixin，观察执行保留为原调度器内的独立编排函数，能力预扫归 Wake。353 项相关回归及独立审阅通过，同包部署双机后的 TUI 120—123 已验长 Goal 暂停续采/Compact、并行普通任务、三子代理和定时投递；交付失败及未覆盖分路见 TESTS，具体结构边界见[路由切片](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-2-步路由切片目标续跑与投递地址)。
租约/恢复片已同版部署：执行 claim 编排迁入 `background_claim.py`，权威恢复查询与日志指纹归 `background_recovery.py`；共享心跳和三类租约各守原边界，324 项定向回归与独立复核通过，真实续租、排队、有序恢复及控制后的原资源复用已验。领取后竞态、各时钟、异常顺序及引用范围见[本片合同](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#第-2-步租约与恢复切片)。
恢复入口的现有边界也须保留：客户端 `resume` 只重连会话，`/goal resume` 只恢复目标续跑；它们不能裁决通用 UNKNOWN。当前只有原请求、精确执行身份、旧进程已死及工具副作用可确定时的 recorded-active-turn 自动恢复链；通用 UNKNOWN 尚无 TUI 人工裁决入口，不通过私调 API 或修改数据库冒充验收。活动 Gateway 恢复只在排除其它工作后有序停启应用，不改变系统网络；不能把空闲部署当成活动恢复。
新版真实 TUI 暴露换代清理的既有缺陷：已确认 STABLE 资源被旧轮清理误置 DIRTY。本地修复只保留 STABLE 与原 DIRTY 记录，未确认资源仍保守标脏，不按整个旧 attempt 已结束而跳过清理；正常续轮、活动接管和升级调和共用原事务。旧数据库中已存在的 DIRTY 不自动修复。三项旧实现失败的合同已固定，186 项相关回归及严格 gate 通过，修复已发布并同包部署。TUI 130 已验零工具副作用的原请求有序恢复；133 已验取消后续代保留原 STABLE 全字段、继续使用原累计值并自行退出原解释器，完整覆盖边界见 TESTS。通用 UNKNOWN 不由目标恢复命令裁决。
插件接线已补当前源码核对：工具快照已有 handler 绑定，但冻结可用性不承担热撤销；权限视图共享 MCP 连接，Skill 快照只校验原文件。
后续沿原执行链补激活代次、准入与资源登记的原子边界，覆盖审批/锁等待、重试及重连后的停用；仍待实施，见[接线约束](docs/design/PLUGIN_LIFECYCLE.md#第-1-步接线核对与迁移约束)。
真实长任务发现拒绝记忆在子代理 Goal 自动续轮重建参数时丢失，同一参数再次弹出审批；已发布部署，实际 TUI 70 已验证同参续轮拒绝。
同一执行链的拒绝列表由宿主持有并跨子代理自动续轮/前后台 Compact 传递，仅复用已有工具名和参数哈希判据；不扩大批准、不解析自然语言或添加报告质量裁决。
确切作用域和新调用边界见 [Goal 生命周期](docs/design/THREAD_GOAL_LIFECYCLE.md)，不宣称跨进程重启持久拒绝已经实现。
验证清理已落地：删除无生产调用的旧 verifier integrity 合同及仅检查自造字典的测试，开发矩阵改指实际 verification 运行入口和存储测试；不新增报告评分或隐藏返工轮。
诊断入口清理（已发布部署）：移除已停用结果块协议的 `structured-repair` 场景、专用模拟后端及调用清单，
普通 runner 的结束仍读取宿主 `turn_end`。保留独立的 runner 重试场景及其已有失败记录；不恢复 JSON 修复回合，也不把这项删除算作长任务验收通过。
TUI 70 后续嵌套 Shell 后台执行及前台超时残留进程已发布部署、对应真实 TUI 复现已验：后台语法集中到 shell_syntax.py，管道结束前不回收组长，终止快照纳入出生标识仍相同的独立组成员。
已复现旧实现失败，并核对 TERM 忽略、另一进程组隔离及 PID 复用边界；不按命令文字或个人路径批量停止，也不把静态语法分析当成任意程序的安全证明。
Shell 输出行数修复已发布并同版部署、双 TUI 事实复验通过：末尾 LF 不产生额外空行；正文、截断说明和展示记录共用一个纯计数入口，不改变采集或执行。
TUI 71 已读到源文件第 51 行仍在报告中写成含表头 50 行，属独立模型表述失败；不能以本次计数修复宣布质量问题解决。
前版 TUI 85 再次确认自然子代理终态与独立后台进程终态须分开读取：A/C 孩子结束后程序继续运行，A 最终因生成程序异常退出。
父级有原会话范围内的进程查询能力但没有调用，错误报告不支持添加第二套状态或最终质量裁决；该轮模型交付失败保留，原架构合同不变。
继续核对原生上下文发现投影缺口（已发布部署，实际后台消费已验）：完成事件已有 `service_window_incomplete` 和冻结的剩余秒数，
但活动回合事件及后台完成清单未保留它们，当前唤醒切换后便不再可见。沿既有中性完成合同统一筛选、传递这对字段；
不重新计时，不改变 DONE、Goal 或重派策略，也不把声明窗口当作进程运行时长或质量验收。测试与报告见执行 Goal。

“装备”采用可选插件包：稳定核心保留完整能力，Python 工具优先独立进程，经现有工具/MCP 与权限链执行。
停用不加载代码、不起后台工作、不占模型上下文；卸载撤销命令/工具/重连并有界回收专属资源，保留用户产物与真实操作历史。
安装与启用分离，动态 slash 命令、版本快照、原子发布与失败回退均待实施，不能把已有启动插件当成热卸载完成。
详细生命周期、DeepSeek Harness/OpenClaw 参考、先行切片与真实 TUI 矩阵见 [可装卸插件方案](docs/design/PLUGIN_LIFECYCLE.md)。
方案中的日志、表格、文档、网页、桌面适配、数据库、测试机和代码检查仅为候选功能示例；不代表已实现，也不将业务特判加入通用核心。
已补 GitHub 社区 15 个项目的 README/包声明调查：插件管理、流程可视化、浏览器/桌面执行、复核与故障恢复等；
阅读证据和取舍见插件方案的社区调查小节，未安装实测，不将社区多实例方案或模型评分门直接搬入本项目。
插件工作聚焦自有 Python 体系：安装后按贡献提供中文调用、slash、Skill 或设置入口，并给出使用卡；不是接入 DSH 插件。
发现目录、分发与撤销必须来自同一有效贡献集；完整交互流程和现有静态 TUI 的差距已写入插件方案，状态仍为待实施。
命令主入口采用 `/plugins <管理动作>` 与 `/plugins@<插件ID> [动作] [参数]`：前者列出/帮助/配置/启停/装卸，后者使用插件。
参数、帮助和补全由声明生成，第一版不分散顶层插件命令；停用仍可查静态帮助，业务调用不可用，不误转聊天或 Shell。
首批用本地包和只读 Python 示例验证完整装卸链，依赖隔离与撤销一并实现；在线安装、更新/回退另排小批。
后台拆分所测框架范围已收口，现推进公共命令首片；详细步骤与完成标准统一在上述合并计划维护。
已按公开 GitHub 关注度筛候选并随机抽取 10 个功能参考，范围与证据见 [插件样本验收计划](docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md)。
只读文件预览、上下文查看、工作台、活动条、状态宠物、图表、设计文件、OCR、文件快照和浏览器操作均为待实现简易版；不复制第三方运行时或素材。
常规模型调用用官方 MiniMax-M2.7，视觉改用官方 MiniMax-M3，复用同一私有密钥引用；样本不新增第二套任务/历史/记忆状态，不按展示文案决策。

本次独立重构 Goal 的四批实现与本机验收已完成；auth 与 Jev 不属于完成条件。Gateway 分离流式出口与请求编排，
前后台 Compact 携带共用纯计算，mailbox 释放保留原副作用入口；设计和验证状态见可维护性评估。
流式切片已通过定向、事件回放及真实子代理/插话/审批缓存；历史取消后进程继续写入的样本保留。
当前 Goal 暂停不停止资源，资源停止另经精确归属核对；不把子代理取消标签当作进程退出事实，强制终止的 native 收口缺口仍保留。
请求上下文、绑定、历史与输入渲染继续拆分后，666 项定向与真实暂停/压缩/重连、程序续采及并行插话已验；
报告数值遗漏与心算错误按质量失败留证。存储的用量与共享 Goal 时钟已迁为显式对象，629 项定向通过，
新版双 TUI 已验暂停计时、压缩重连、真实续采、并行插话及主子费用归属；报告一处汇总与明细矛盾单独留证。
其余领域已共享原路径、锁与 CAS 完成迁移，没有增加转发 facade。
执行/调度、历史提交/投递、Gateway 适配及存储组合四批均保持权限、身份、取消、恢复和事务合同，
真实验收只走官方 MiniMax-M2.7 的多路 TUI。
执行拆分已落地：同片模型运行、Compact 重试和原生历史保存独立，使用明确的参数准备接口与具名结果；
技术续跑判据归入已有 `turn_end.py`。定向与真实 TUI 的暂停恢复、压缩重连、进程停止续做已验；
子任务 UTC 错误、定时采样不满足间隔及清理参数错误保留，不能把全部任务质量记为通过。
历史提交与投递已拆分，保持外发、canonical 提交和冻结重投的原顺序；调度准入及原存储事务边界保持。
提交/投递批次已落地：投递模块以显式能力和请求字段独立加载，实时任务状态仍在原抑制位置读取；
迁移调用方并删除旧私有转发入口，历史 v1 冻结载荷继续按已有持久数据合同读取。
全仓检查补修（已落地）：同 task 新请求会复用 canonical run，归档必须在权威绑定之后创建；
request 保持消息身份，归档、工具与收尾共用绑定后的 run。归档准备失败要关闭本次新 attempt，不能遗留执行权。
同时收窄已有宽泛参数接口、登记历史读取/任务绑定错误码，并按当前配置与身份合同修复旧测试夹具。
全仓复查 17,070 passed、0 failed，既有 skip/xfail 保留；新版 TUI 的 canonical 身份和返回已验，
模型改用文件工具补记录、时间间隔失真等质量失败保持开放，不能把程序续做宣称通过。

2026-09-19 热点源码评估已完成，用户已授权分批重构，后端协议拆分已通过本地定向验收；Jev 接入仍待实施。先拆后端协议，
再分离会话执行/调度和 Gateway 适配，最后整理深层存储继承；保留唯一状态源与事务边界。
Computer Use 已安装既有可选执行器并完成本机真实 TUI 英文、中文输入与读回；空白控件定位仍有 OCR 局限。
Jev 当前接入方向见[决策模型计划](docs/design/DECISION_MODEL_INTEGRATION.md)：作为可选模型用途逐点接入，仍不担任生成主模型、授权或完成判官。
本轮真实验收仅通过实际 TUI、官方 MiniMax-M2.7；本机桌面与登录认证分别留证，私有材料不进入仓库。
公共 `base` 已收窄到合同与本地后端，HTTP/Chat/Messages/工厂各有唯一实现；旧实现和错误注释已移除。
Messages 长参数入口改为冻结请求对象，后台纯工具策略从会话运行时独立，未放宽 code-size 基线。
后台上下文与历史种子已分别迁入 `background_context.py`、`background_history_seed.py`；
保留任务范围、原生工具记录、Compact 代次及读取错误合同。上下文内既有进度对账可能写任务账，
保持其调用顺序；不迁移锁、存储、唤醒消费或投递事务，删除被后定义覆盖的策略快照重复函数。
并行真实 TUI 已覆盖长任务、取消续做、客户端重连、PTY、后台等待和 Goal 暂停恢复；详细通过边界见验收矩阵。
真实测试发现并修复后台状态地址随 Full Access 漂移、PTY 旧方法调用及桌面输入丢字。
后台状态按宿主 canonical owner home 存取，权限墙独立；桌面输入复用既有 Quartz/PyAutoGUI 并要求后置读回。
auth 表单取消和参数拒绝已验，官方设备码在两处环境被 HTTP 530/403 拒绝，真实账号确认与刷新退出仍待验。
增量真实 TUI 发现首次 `/goal` 未保存客户端工作目录。已修并复验：控制请求携带结构化 workspace，
复用普通消息的目录权限校验，并签入持久回执摘要；只初始化尚无目录的线程，不用控制命令重定向既有任务。
旧回执按显式摘要版本读取，旧版本不得携带未签名的 workspace；不从目标文字提取路径。
全新真实 TUI 的实际目录与线程记录一致；启动后替换空目录为外部符号链接时，Gateway 在创建 Goal 前拒绝。
源码统计、参考阅读、适用边界及分批方案见 [可维护性与 Jev 评估](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md)。

已落地第一步：后台重试策略独立为 `cli/gateway_lane_retry.py`，调度器只负责编排，
会话模型解析只负责编号与 owner 验证，后端工厂与恢复准入共用缺配置判据。
删除 worker/planner 中分散的退避字典兼容初始化与重复判断，避免只加转发壳。

后续热点可继续评估 `conversation/runtime.py` 的恢复及 `cli/gateway_loops.py` 的维护车道；
本次 Gateway 请求职责已按上述边界拆分，存储深继承已按直接调用方和显式领域依赖改为组合。
不在修行为的同一批进行大文件移动，不放宽尺寸基线，不把通过尺寸检查说成可读性已经理想。
每批保留 focused 回归、文档同步和真实 TUI 验收；模型请求、权限、持久状态是不可意外改变的边界。

## 当前待落地或待复验

- 已实现、待真实停机复验：Gateway 停止排空窗口后，仍在途的模型调用由唯一账本批量记为 failed/`MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`
  （用量按缺报，不补零），并写 `gateway_model_calls_interrupted` 结构化事件；只覆盖 Gateway 进程 agent 自己的账本，
  子代理 runner worker 各自持有的账本尚未纳入。下一片是启动时对遗留 `running` attempt 的结构化对账（非正常退出的补救路径），
  与本项一起构成"停机结清 + 启动对账"两段自愈，见 [Gateway 结构](docs/modules/gateway/04-structure.md)。

- 已决定并实现（2026-09-24 晚，用户第 5 项"小问题 my-agent 自己搞定"）：无进程身份的悬挂运行轮**不自动判死**——同一 owner 权威库会被多个运行版本写入，"没有身份"不是死亡证明；产品改为在 Gateway 启动时把它们列进状态与事件，并提供显式结构化命令 `runtime-stale-attempts --settle` 按阈值结清为 unknown（记结清来源）。自愈的边界是"看得见 + 一条命令"，不是猜。启动对账仍只对能证实进程死亡的行自动生效。
- 已决定并实现（2026-09-24 深夜，用户第 5 项"单回合超窗渐进压缩"）：单个活动回合多条工具结果在下一次预检前全部内联，会把上下文冲过窗口再撞 `COMPACT_CANDIDATE_TOO_LARGE`（真实样本 129%）。修法不按工具名、不改压缩器：归档入口用 preflight 同口径余量判断，本条输出估算 token 不小于距压缩点的剩余余量就立刻外置（`read_file` 分页也外置，模型只看预览+恢复锚点）并登记 `tool_context_window_overflow(reason=tool_result_headroom)`，下一次预检必走统一 Compact；开关 `tool_output_externalize_on_low_headroom` 默认开。见[验证模块进展](docs/modules/verification/02-progress.md)。
- 想法、未落地：模型档案窗口目前靠人工实测（2026-09-24 用产品后端探到 MiniMax-M2.7=262,144、M3=1,048,576，
  官方文档分别写 204,800/1,000,000，口径都是输入+输出合计）。后续可把供应商 400 "context window exceeds limit"
  的结构化事实回灌成档案窗口的自动校准候选：只提示、需用户确认，不静默改档案，也不从错误文案猜数字。

- 已实现、真实增量已验：模型累计容器生成唯一 `usage_scope_id`；来源切换不更换，进程重启或有界容器重建才换代。
  正常、错误和取消复用唯一结算入口，异常不会凭估算补费用。历史无代次的旧账维持原口径，不自动重写。
  认证已实现、真实账号待授权：沿 owner 私有 provider 管理登录凭据，会话仍只保存模型引用；订阅认证不冒充通用 API Key。
  仅接入已核对的服务商授权流程，不读取其他应用私有凭据或静默更改用户日常模型。

- 已实现、真实工具复验通过：普通插话继续已有任务时，主执行绑定必须同时回填真实 run 与 attempt；
  `request_id` 继续标识当前用户消息，不可被持久 run 覆盖。宿主发布仍携带原消息与真实执行绑定，
  Compact 换代不得混用两者。工具权威门保持精确 run/task/attempt 验证，不新增身份别名或绕过。
  原失败会话恢复后整合通过 14 项测试；另一会话等待两个孩子时插话，真实命令执行成功且孩子继续运行。

- 已修、验收中：真实本地模型任务出现同参数、同结果的成功 Shell 循环；重复门自身的未执行拒绝
  不再重置观测，也不追加到有界诊断窗口挤掉原结果。完整拒绝仍进入工具回执、模型历史及审计。
  执行前只查相同调用的真实结果；无关工具失败不清空该哈希。拒绝正文携带原门的计数和换路说明。
  不新增任务完成门或慢流时限，原阈值、零值不限制、不同结果及真实写入的进展边界不变。
  文本工具解码失败提示能力发现途径，不自动转图或调用额外模型；图片误用只是现场前置事件，
  不能据此断言所有模型复读都已修复。见 [工具协议](docs/design/tool-runtime-unification.md#重复工具观测与恢复)。

- 已实现、主代理真实 TUI 已验：Esc 只中断当前执行轮，有 active Goal 时沿原去重 wake 安全续接；
  `/stop`、`/goal pause` 才明确暂停，普通聊天和目标正文编辑不自动恢复暂停状态。
  同一 Goal 的前台、后台续接共用任务与执行代次，旧取消记录不得覆盖新执行。
  用户补充和纠偏保留在 canonical 会话与压缩摘要中，不强制逐条改写 Goal；
  后台插话须有精确 running claim，并共用原输入回执；合法恢复在换代事务内同步重开 TaskRun，历史关闭事件不删。
  后台模型参数不得另造输入回合编号；先后完成的历史 Goal 不构成未结束目标冲突，恢复不无故改绑。
  新鲜多子代理验收发现生命周期信封中的 Goal task 编号被误当普通 user request 查找；补修先核对本线程
  Goal 归属，再排除该持久输入编号。普通消息编号仍逐项校验，缺失/跨线程/损坏不回退到旧目标；
  原现场与同会话第二个多子代理目标真实复验已过。

- 已修、真实增量验收通过：用量 source 只说明入口来源；成功/异常/取消复用结算，累计容器代次参与去重，
  来源交接不重计，进程重启/容器淘汰重建另计。真实中断后追加消息累计与持久账一致；旧账没有自动迁移。
  本地慢模型工具语法编译、流不完整和单槽排队是不同兼容问题；不通过无界重试、猜补 JSON 或取消权限门解决。
  是否调整目标由模型结合上下文决定，状态变化仍需结构化控制。详见 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

- 慢模型增加显式每模型排队预算（默认 0，真实组合验收中）：只加到首事件预填充预算，不改流静默、取消或重试。
  单处理槽位排队不能靠模型输入速率推断，需用户明确配置。另有长思考耗尽 16,314 输出 token
  后无正文，正确保留 `MODEL_RESPONSE_TRUNCATED / unfinished`，不能靠延长时间解决。
  下一步需分别验证提供方容量/排队预算和推理输出预算；不能按本地地址硬编码协议或无界重试。

- 已实现，主链已验收、复杂组合待补：父子孙继续独立工作，成功通知只保留有界合批窗口，不等待整树终态。
  模型自然让出才进入直属等待，任一新结果可解除等待；复用 canonical 事件与原执行权。
  停滞诊断区分慢首 token、流活动、长工具与已退出执行器，不引入静默超时强杀或自动替代。
  四路真实 TUI 已验证快结果接入、父级独立工作和低阈值提醒后继续完成；后续已完成真实孙级交接，重复实现仍需改进。
  设计和验收边界见 [并行执行](docs/design/SUBAGENT_PARALLEL_EXECUTION.md)。

- 已落地并完成真实 TUI 主流程验证：每个代理会话至多一个未结束 Goal；主代理与每个子代理独立保存。新增第二个名字不再启动另一个执行器。
  派工可只给 prompt，也可显式附持续目标；Todo 按代理复用已有账本、完全可选，不参与完成门。
  TUI 通过方向键和 Enter 打开目标草稿，明确保存才生效，退出丢弃；保存使用内容版本比较，计费刷新不制造编辑冲突。
  修改内容不隐式恢复暂停目标，不改变 run/task、历史和权限。主子目标保存、放弃、停止以及父子消息隔离已实测；孙级、IM 和旧数据迁移仍待专项验证。详见 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

- 已实现并真实验收通过（2026-09-24，main `a1fea9e54`，本机 workspace-peek 0.1.1→0.1.2→enable 成功）：`/plugins update <插件> <新包路径>` 首片——同 ID 新包替换已停用安装，install+可选 configure 两步回执保留兼容配置，不新增持久动作；不做双版本准备切换与 rollback（需安装记录持有候选版本字段，另开一片）。见 [插件方案](docs/design/PLUGIN_LIFECYCLE.md#管理与使用分开的命令语法待实施)。
- 已实现并真实验收通过（2026-09-24 晚，片 C，main `94a2d0b4d`，双机 runtime-step11c；M2.7 阈值自动压缩 checkpoint `vision_summary/declared/summarized=1`）：随图摘要改为“先看图后总结”两步——含图回合按摘要预算打包成若干看图小请求（`compact_vision_digest_max_requests`），
  要点文字进普通文字摘要请求，图块统一投影为归档引用；准入按最大的一次小请求判断，因此阈值自动压缩与手动 /compact 走同一条路；
  部分成功记 `vision_digest_partial` 与双计数，一次都没成功同次回落 A。见 [媒体压缩策略](docs/design/COMPACT_MEDIA_POLICY.md#片-c先看图后总结2026-09-24用户决定第-2-项随图摘要必须在自动压缩里生效)。
- 已实现并真实验收通过（2026-09-24，main `8c6d29c5f`，双机 runtime-step10r；M3 手动 /compact 走随图摘要，阈值压缩按预算门回落并记原因——片 C 已解决这一回落）：媒体压缩策略片 B。auto 下由结构化事实选随图摘要：档案 `input_modalities` 声明，
  或宿主一次 8×8 纯色图探针（进程级缓存、只认结构化工具回答）；强制恢复、同代次 B 曾失败、视频、字节/摘要预算不满足都落归档引用并在 checkpoint
  记 reason。B 单请求失败统一 typed `COMPACT_VISION_SUMMARY_FAILED`，只写线程代次标记不进熔断。见 [媒体压缩策略](docs/design/COMPACT_MEDIA_POLICY.md#片-b-实现记录与偏差2026-09-24按代码事实调整不改原则)。
- 已修并真实复验通过（2026-09-24，main `4ec0e11f3`，双机 runtime-step10o；本机两段式派工/唤醒任务与测试机停用重启用均通过，见 TESTS）：后台唤醒续跑的工具目录不再是封闭名单。默认 profile 决策带 `extension_tools=inherit`，
  运行构造方按注册表代理类型事实并入当前已启用插件/MCP 工具；显式配置或任务白名单标 `none`。停用撤销与禁用表仍在注册表/快照
  fail-closed。同批：停止重试可按 PID 出生标识结清实例已消失的旧 unknown 进程记录，插件停用不再卡在 `activation_unsettled`。
  见 [后台工具策略边界](docs/modules/gateway/04-structure.md#后台工具策略边界) 与 [受管后台进程](docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md#重试结清旧未知记录)。
- 执行器退出与积压分页修复已落地并通过定向回归，真实 TUI 故障组合待验；不按静默时长判死。
- 长会话的模型输入、展示估算、累计用量和缓存费用统一核算，并保留压缩前后的可恢复历史。
- 旧会话目标冲突的显式迁移、小时级慢模型并发操作、IM 环境能力仍需专项验证。
- Goal 提前关闭任务及命名后插话身份断裂已通过真实 TUI；多目标去重分工和重复正文仍未解决，模型自行填写 Goal 时限仍是开放边界，
  尚未把模型的时间参数改为必须由用户控制面授权的方案，不通过解析用户正文判断。
- 详细现象和优先级以 [STATUS](STATUS.md) 与 [ROADMAP](docs/ROADMAP.md) 为准。

## TUI 阅读位置与插话时序

本轮 TUI 原地阅读与插话时序修复已实现，验收按 [交接记录](docs/tasks/TUI_READING_HANDOFF.md) 的精确版本与范围核对。普通/详细/原文共用阅读锚点，
内部有界分页连续滚动；插话提交边界、显示检查点与跨片历史排序共用精确输入身份。
Goal scope 同时公开宿主续跑机制事实，不把 active 或单轮 final 当作长期运行证明。
边界和验收见 [完整原文](docs/design/TUI_COMPLETE_DETAIL.md) 与 [目标控制](docs/design/THREAD_GOAL_LIFECYCLE.md)。

## 发布资料约定

产品统一命名为 my-agent。文档只保留使用、部署、功能和开发资料；示例使用保留域名或虚构用户。真实凭据、个人数据、临时评估产物、机器现场配置不进仓库。

运行必需的服务商名称、模型 ID、HTTP header、兼容文件名、依赖名、正式仓库地址保留。第三方许可和版权说明见 LICENSE、NOTICE 及对应 vendor 目录，发布包必须携带。

决策模型12.4混合来源恢复本地切片已验：在原transcript候选中合并活动工具精确分区和完整模型可见材料，v3双覆盖、单writer/CAS及原完整容量门；不增加持久状态。细节见[容量审计末节](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)，738项定向与本片严格gate通过；初次/手动、真实IR组合和供应商验收未完成。

决策模型12.4首次准备切片进行中：完整请求压缩放在 child/主代理候选选模及发送前拒绝回退基线之前；无压缩也只消费同次冻结输入，不重复准备。手动 Compact 没有下一轮业务输入，只证明当前会话历史估算与原覆盖/CAS，下一轮独立验证完整容量，不能把手动成功展示为未来请求容量通过。

决策模型12.4真实IR来源切片进行中：同一临时工具来源扩展保存完整原AssistantTurn/ToolResult分区及保留IR，覆盖仍取原ToolCall四元ref，不以archive preview代替真实模型可见正文。未知/不完整/媒体组保留，关联archive也不获得覆盖；同ref原IR优先归档投影。机械回退须保留全部所选来源，分段退化无法证明完整时拒绝提交，仍用原writer/CAS及完整容量门。

决策模型12.4外层overflow原生IR接续进行中：同一宿主回合只在context_overflow返回临时冻结的typed IR/工具上下文/已转发指引，沿原RunParams回入；新请求、跨任务、跨scope/view不得沿用。主代理attempt由原DB轮换，旧ToolCall的原四元身份保持，carrier不授予执行权；权限/工具快照/provider历史前缀仍重新准备。插话UserTurn增加仅内部input_ids，与原mailbox packet同源，释放只按ID剔除，禁止正文匹配。保留原用户IR和媒体引用，不重复初始化用户轮。仍只有原archive恢复执行预算、原Compact writer/CAS提交。

外层原生IR接续补充（已本地实现，验收中）：宿主冻结的typed AppliedCompactContext是摘要线程来源；taskless后台缺task属性时不补写以免误升任务，已有当前线程声明仍按child优先核对。request_id同逻辑回合稳定；强制恢复必须有消息或完整工具来源，carry本身不证明可压。详细边界见容量审计末节。

# 客户端资源寿命与低配置并发（2026-09-23，候选本片验收通过）

解决长历史逐帧处理、旧测试客户端驻留和状态入口拥塞；区分 IM 身份、持久排队、
执行槽与 HTTP 连接，保持既有任务权威。资源合同与验收边界见
[资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md)。已在独立测试机分层验收，最终统计见 TESTS；未发布默认环境，不将有限采样外推为无限耐久承诺。
