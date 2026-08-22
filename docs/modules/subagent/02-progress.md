# Subagent Progress

## 2026-08-22 固定代理区、职责短标题与实时上下文投影

- `conversation/agent_activity.py` 的公开 schema 升为 v4：直属 child 只投影 typed lifecycle、创建时的
  职责短标题、每次真实模型调用前的当前总上下文 token，以及 Compact apply 次数。它不再把累计计费
  token 或“模型响应中/正在使用工具”等运行碎片当成这一行的主文案。终态 child 不残留旧动作，第一次
  attempt 不展示，只有实际重试才显示次数。
- `create_subagents.description` 对照 终端交互 `AgentTool` 的 3--5 word description，作为可选短标题进入
  canonical `SubAgentTask.description`；主/子/孙创建链共用。单 child 可使用顶层 description；批量
  `items[]` 时，顶层 description 只描述整批派工，绝不能复制给每个 child，每项应写自己的职责短标题。
  省略时只读投影只退回该 item 的 goal 开头。renderer 按当前终端剩余列截断；每个 child 永远只占一行，
  固定后缀优先保留耗时、`ctx`、Compact 和真实重试。
- 后台 main 通过同一 agent 进程内的易失 sink 投影最近 thinking/tool/provider-retry/waiting；它只回答
  “主代理现在在干什么”，不保存正文、不拥有生命周期。最终可交付正文仍由 transcript 与 delivery contract
  持有，并以普通 assistant 消息显示。一次模型轮结束只进入 `waiting`，不等于整个任务进入 finalizing。
- `create_subagents` 成功回执携带结构化 `task_progress_seed.items`，通用工具进度 adapter 将它提升为
  `task_progress_items`，且有界 seed 同时存入 `result_envelope`，大输出归档后也不会丢实时 Todo 事件。
  `task_progress` 先晋升 task 再选账本；有显式 `covers` 的 child 沿用已有项，并以
  `progress_item_ids` 与 typed child status 原位勾选；只有未绑定 child 才按 exact run id 新建项。
- exact child run id 自动生成的 seed 项继续保留在 canonical 进度账本，供恢复和状态关联使用；TUI 发现
  同一个直属 child 已在输入框下方面板展示时，只在 view 层隐藏这条重复 Todo。模型自己创建的普通任务项
  和显式 `covers` 项仍留在上方并按 child status 打标，不解析“子代理”标题文字；隐藏发生在展示上限
  计算前，最终后台 notice 也使用同一个 exact-id 过滤，不能在 child panel 收起后让长 seed Todo 重新出现。
- 参照 终端交互 `SpinnerWithVerb` 和 `CoordinatorTaskPanel`，main 的动态 `Working` 行在消息区末尾/
  Context 前，输入框下方只保留 child 行。main 的动作摘要和 child 的职责短标题都必须严格单行，长文本
  只截断、不折行。当前候选相关 focused tests 已通过；功能验收仍以 `.7` 单 Gateway、MiniMax-M2.7 和
  四个原样真实 TUI 任务为准。
- 对照 会话运行时 orchestrator：一旦实际实现已委派给 child，main 的角色就保持为协调者，只能读取/整合现有
  产物、执行用户允许的测试并汇报；缺口要精确指导原 child 或另派 replacement child。容量不足、参数错误、
  child 失败或终态都不构成 main 静默接管实现的授权。这是通用分工提示，不参与机器状态或质量验收。

## 2026-08-21 递归创建、自动启动与自然收口

- 当前本地 TUI 已从单一 active-task 计数收细为固定直属 child 活动区：
  `conversation/agent_activity.py` 从 thread 的 active task link 和 canonical run 账本选取 depth=1
  child，输出名称、status、职责短标题、耗时和实时上下文 token。它不进 transcript，不携带工具输出/
  路径/权限，也不驱动完成、重试或验收。只读投影与 renderer focused 已通过，`.7` 真机待部署复验。
- `d928d77` 的 `.7` 单 Gateway 原样 TUI 轮已实现 4 个 child 各一次自然 DONE、直属事件自动唤醒和
  `0.0.0.0:8080` loopback/LAN HTTP 200，同时抓到三个新底层缺口：裸 `abc/...` 被误投到 task
  `output/abc`，跨任务进程缓存把旧 `/root/kill-ws/...` 阅读包塞给新 child，前台让出后 TUI 看起来空闲。
  当前本地切片把普通相对交付路径改为继承可信 cwd，仅显式 `output/...`/`work/...` 使用 task 内部目录；
  删除跨轮 shared-context cache；Gateway notice 快照增加 canonical active-task count，TUI 投影为一个可移除
  Working 活动块。188 项定向回归和本地严格 gate 已通过，推送、部署和同 prompt 真机复验仍待完成。
- 子代理工具面进一步按结构化角色裁剪：根主代理和 `can_spawn_children=true` coordinator 才有
  create/guidance/cancel/resolve 四个直属控制入口；worker/researcher/tester/writer/bug-finder 等 leaf
  一个都不带，只保留执行工具和自身 `capability_request`。直接创建与内部层级调度共用同一减法规则，
  goal、agent 名和历史 grant 都不能让 leaf 获得管理面。
- 根/子/孙代理只保留一个模型可见创建入口 `create_subagents`；创建后宿主自动启动。
  `dispatch_subagents`、`schedule_child_subagents`、`wait` 与 `inspect_agent_tree` 的模型工具、schema、注册和
  专用测试已删除。代理树仍是 `/status`、TUI、恢复与诊断的内部 projection。
- 重复宿主生命周期的 `raise_event` 模型工具、schema、注册、实现和专用测试也已删除；历史工具快照会
  统一过滤它。内部 observation/wake 仍由 runner、capability、Audit 和 Gateway 宿主服务直接写入。
- 内部 dispatcher 仍保留为 Gateway/runner 的自动启动、租约、恢复与有界重试引擎；
  父代理只接收 lifecycle event，必要时给直属 child 发补充消息或取消，不再手工查看/推进已创建 run。
  这等价于 会话运行时 `spawn_agent_internal` 内部的容量保留、thread 创建、首条输入和 status watcher；
  旧 dispatcher 名称/入口可继续折叠，但这些 create 后必须动作不能删除。
- 新增公共 `turn_end.reason` 六种轮结束原因，主代理、子代理、Gateway 和父级 wake 共用。
  模型自然最终回复不再被交付扫描、完成 marker、产物数或机器验收改写。
- 删除普通子代理 acceptance ledger/verifier 与交付收口旁路。历史
  `acceptance_checks` / `verification_status` 只保留持久化可读兼容，已从 TaskEnvelope、
  runner 模型摘要、父级 wake、树摘要和完成计算中移除。
- 默认资源上限收紧为同 owner 最多 6 个未结束 child、每次创建最多 4 个、
  auto runner 并发最多 4 个，用资源护栏抑制一次创建十几个 child 的慢与不稳定。
- 本地已通过 turn-end、统一创建、生命周期、工具规格、树投影和 TUI/Compact 定向回归；
  真机单 Gateway + 真实 TUI 任务证据在发布后补入。
- 对照 会话运行时 `send_input` 后，模型消息工具收成单个 `target + message`，并增加递归授权：当前代理只能给
  直接 child 插话，不能广播、越层代管孙代理或向 thread/task/case 写模型消息。用户对主代理的插入仍走
  active-turn 输入链。
- 删除创建后每 180 秒调用模型“巡场”的 `dispatch_supervision_auto` 与 `wait_tool.py`；child 的真实
  生命周期事件直接唤醒父级，底层 heartbeat/orphan/retry 继续只守进程可靠性。旧 policy 会被自动退休。
- 历史配置、grant、角色模板和后代继承的工具快照共用一个退休过滤器，旧账本不能复活已删除的
  查树/手动推进工具。显式空 `allowed_tools` 仍是零权限，不会被角色默认值悄悄扩权。
- `e321483` 真机确认前台延迟 `orchestration` 会让首轮模型看不到创建工具；默认延迟类别已移除
  `orchestration`，递归 Agent 创建与直属控制从第一次 provider 调用就作为 native Schema 直出。
- 修复同一 thread 多任务的后台权威串线：后台 run_id 优先使用 exact task id；历史线程级 run 属于旧任务
  时不再创建跨任务 attempt。普通 child 现在逐层继承父级结构化工作区上界；嵌套
  `items[].output_files` 继续记录交付身份和冲突锁，不再承担父级已有目录的重复授权。
- 首轮 `.7` 原样任务实锤 child“挂掉”是状态断链：OPEN capability request 被通用 completed 收尾覆盖为
  DONE，grant 后原 run 又没有重新排队。当前 runner 以 OPEN 结构化事实优先投影 BLOCKED；直属父级
  grant/deny 后同 run 回到 PENDING、恢复 task link，并由裁决 event 触发内部 dispatcher 续跑。
- `c2c0235` 二次原样任务真实启动 4 个 child，其中 2 个自然完成，另 2 个超过 17 分钟仍运行。工具账
  证明根因是 `/root` 宽 forbidden 误伤更窄 task output allow，导致连续 `WRITE_FORBIDDEN` 和重复申请
  已 grant 权限；状态投影又把真实 `ToolResult.tool_name` 当成 `tool` 读取，长期只显示空 `RUNNING/0%`。
  当前写边界按 会话运行时 最具体条目优先、同层 deny 胜出；runner 模型/工具边界保存不含正文的有界活动短状态。
- `d257dfb` 真机复验后 3 个 child 都自然 `DONE`，`/root/abc` 的 8 个文件均由 child 写出；
  未继续整合的根因不在 child，而是同 owner 的另一条旧后台会话占住了 owner 级唯一 tick。
  Gateway 现已按 durable thread 分后台车道，同 thread 单飞、不同 thread 有界并发；真机最终整合与
  `0.0.0.0:8080` 仍需新部署轮验证，未提前标记完成。
- `bea6fed` 下一轮虽让 4 个 child 全部 `DONE` 并得到 HTTP 200，仍耗时 422.849 秒、32 次模型调用，
  累计输入估算约 19.47M tokens。底层原因是 task-local 父级创建孙代理后被通用孤儿续跑立即复活，
  而嵌套 child 又把成功事件越级投到根会话。现新增 exact direct-child wait：任意父级创建下一层后
  `interrupted/SUBAGENTS_ACTIVE` 让出，同批成功收齐只恢复一次，失败或 capability 阻塞立即恢复；
  孙代理事件只恢复直属父级，父级上下文直接带有界 `direct_children` status/result refs。
- `task_progress` 现在只是软记事账本。普通任务的自动 continuation 模块、配置和深度状态已经删除；
  open 项不会开新模型轮或阻止 final。新项必须有稳定 `id/title/status`，更新返回前以 canonical child
  run id 重新对账，避免模型旧状态覆盖真实 DONE。
- `be531a8` 后的原样 TUI 样本一次创建 4 个 child，但所有 child 继承的 `/root`
  又被默认 home deny 同层拒绝，导致 `WRITE_FORBIDDEN -> capability_request`。候选修复不改
  通用“同层 deny 胜出”规则，而是在创建 local/unmanaged task 时调和精确的 inherited workspace
  冲突；远程 owner 和更窄禁止路径不放宽。

## 2026-08-12 子代理候选 scope 统一规范

- `services/memory_candidates.py::_task_scope` 是子代理候选的唯一 scope 构造点：`scope_type=project`、
  `scope_key=f"project:{safe_id}"`（task/root ID 净化后截断 140，空则 digest）。
- 新持久化一律 `project:<id>` 单一权威；`task:<id>` 只保留为旧账本召回兼容的 read alias，
  curator 侧经 `canonical_scope_key` 归一，不允许再以 `task:<id>` 新持久化（避免 project/task 双正式身份）。
- task 的 goal 只进入 `applies_when` 作为人类说明，不参与 scope 判定或归属。
- 回归：curator observation_id 归一（test_curator_observation_id_normalizes_task_alias_scope）+ 重放幂等
  （test_curator_task_alias_replay_does_not_duplicate_occurrence）。

## 2026-08-04 子代理经验统一进入 owner Memory Candidate

- 删除 `SubAgentLearningService`、task-local `memory_gate` 及各自审核/导出状态机；子代理 workspace 只保留
  原始 result、finding、lesson、evidence 和 artifact refs，不再拥有第二套正式候选事实源。
- `SubAgentManager` 由 owner composition root 注入唯一 `CandidateService`，runner 结果通过
  `SubAgentMemoryCandidateService` 将 lesson/finding 批量写入 `memory/candidates.jsonl`。该适配器不审核、
  不晋升，也不能直接写 long-term、Persona、正式 lesson 或 HOT。
- observation id 使用结构化 run/finding/content 身份保持重放幂等；`subagent_finding` 与
  `subagent_lesson` 默认待审，不能因 child 自报 confidence 或较新时间获得事实权威。

## 2026-07-30 子代理启动上下文只要求执行必需事实

- 子代理的 `acceptance_checks` 是可选的质量说明，不是每个任务都具备的执行前提。旧门把它与
  `goal/output_contract/permissions/constraints/workspace_refs` 一起列为必填，导致明确的持续
  Audit 工作者因没有人工验收清单而在启动前直接 `BLOCKED`。
- 参考 会话运行时 v2 spawn 只要求结构化任务名称与消息、其余上下文按能力补充的边界，当前门只保留
  真正影响执行和权限的必需事实；没有 acceptance 清单仍可运行，有清单时仍原样继承并用于质量核对。
- 没有增加 Audit 特例、角色分支或 prompt 关键词。缺 goal、权限、约束、输出合同或 workspace
  引用仍 fail-closed；相关 context bundle、runner、编排与 Audit 回归已通过。

## 2026-07-29 系统默认 child 输出引用按 run 隔离

- 同一长任务分两批创建 child 时，旧系统默认引用会重复使用
  `work/child_outputs/01-*.md`、`02-*.md`。预创建冲突账本把第二批当成业务输出冲突；绕过后又可能由
  已存在文件触发跳过或覆盖。真实五套 CLI 对照中的 my-agent 运行复现了该问题。
- 当前没有增加另一套命名器。`services/base.py` 继续使用已有的“run 创建后重绑定 output ref”入口，
  只把 `system_default_output_ref=true` 的内部默认引用规范化为
  `work/child_outputs/<run_id>/01-*.md`。同一 run 重放结果稳定，不依赖展示名或任务正文。
- `create_constraints` 的批量预检查只跳过这种尚无 run id 的系统内部默认槽位。用户显式
  `output_ref`、artifact ref 和真实业务输出仍进入原有 shared-output lock，冲突继续 fail-closed。
- 回归覆盖两批默认 child、同一 run 幂等重绑定、显式路径冲突不放宽以及 canonical state/artifact
  ref 一致性。完整 pytest 已运行到 100% 并退出 0；最终 MiniMax 长任务和发布证据见
  `PRODUCT_FACTS` 与 Gateway progress。

## 2026-07-29 后台续跑轮持有自己的执行租约

- 修后两批真模型测试首次唤醒时复现：scheduler 已取得当前 thread/task 的独占 background claim，
  但构造的 `RunParams` 没有携带“当前轮就是该任务执行者”的 per-turn typed fact。工具网关因此把
  第二批 `create_subagents` 误判为第二个并发执行器并拒绝。
- `_background_task_attributes` 现在只对 scheduler 已绑定的当前 task 设置既有
  `conversation_task_turn_active=true`。该值不持久化、不来自 prompt，也不按工具名放行；缺少该值的
  其他 turn 继续由 live claim fail-closed。
- 1.10 MiniMax 普通中文复验先后创建两批各 2 个 child，总数严格为 4，四者都为
  `DONE + VERIFIED`。第二批 `create_subagents` 的后台 run 真实返回成功，主代理随后读取四份摘要并
  写出汇总；没有重复创建、自己锁死或第二套调度器。
- 一次性 `my-agent run` 仍按既有 nonblocking wait 契约结束当前进程；没有常驻消费端时需由现有
  background-main-agent 入口续跑。正式 Gateway/IM 自带该常驻调度器。本轮没有为了 CLI 对照复制
  会话运行时 的进程内 wait 或 长期助手 的 one-shot 同步 fallback。

## 2026-07-28 子代理工具继承只减不增

- 层级 scheduler 继续使用现有 role template 和 `allowed_tools`，但二者都不再产生新权限：最终 child
  工具集必须落在父 run 当前工具快照内。显式 child allowlist 只是进一步收窄，不是扩权入口。
- 普通 leaf 固定移除 create/guidance/cancel/resolve 四个直属控制入口；coordinator 也只有父代理
  原本拥有对应工具时才能保留。角色只决定继承后留下什么，不会按任务正文、agent 名或工具说明猜权限。
- child 的父 conversation id 只用于 lineage、wake 和归档；`context_scope=task_local` 继续使用独立
  runner lane，不参加主会话“当前是否已有执行器”的工作工具准入，因而不会把正常并行 child 当成
  第二个主代理执行器。
- 旧默认 leaf 列表中无条件附带的直属下级控制项已删除，没有增加第二套子代理类型或派工 runtime。
  聚焦回归覆盖 worker、coordinator、显式 allowlist、未知工具别名和多层继承。

## 2026-07-28 子代理复用完整原生工具输入预算

- 子代理没有新增专属窗口或恢复包；`task_local` 继续走与主代理完全相同的
  `build_tool_loop_prompt -> model_visible_context_tokens -> compact_native_ir_to_token_budget`。
- 原来的 `_window_native_ir_to_budget` 字符近似已删除。当前共享入口会统计工具 Schema、ToolCall
  参数和 ToolResult，并按同一个 RuntimeCompactPolicy 保留近期尾部；子代理工作目录、
  canonical state、checkpoint 和通用 `compact_applies` 不变。
- 大工具参数与连续两次压力回归在同一公共测试中验证，因此不能再为 child 添加第二阈值、
  task compact 或专用摘要器。

## 2026-07-27 恢复共享工具历史窗口

- 回退 `a4d65568` 引入的 live tool-context compactor。该实现会在 90% 阈值附近逐对删除
  native tool IR，并接受几乎没有下降的结果，真实压力轮因此出现反复 Compact。
- 主代理和子代理现在重新在每轮模型调用前共用既有 `window_tool_context_params`；原生工具历史
  已由 2026-07-28 的完整 token 入口取代旧 `_window_native_ir_to_budget` 字符近似。工具调用/结果
  仍成对保留，owner/thread 的持久 transcript Compact 不变。
- 删除只服务于错误路线的 `live_context_compaction` runner/result/finalization 遥测字段和传递链，
  避免留下一个已无执行语义的第二状态面。新增 conversation scope 回归，防止正式会话再次绕过
  共享窗口。

## 2026-07-27 删除子代理专用 Compact 路线

- 删除 `agent_core/subagent/compact_continuation*`、`session_continuation`、
  `subagents/services/subagent_session_compact`、子代理专用 continue-packet service、
  task/agent 专属 compact 索引和对应测试；新 run 不再创建旧 `compactions/` 或 `recovery/`。
- 主代理和 `context_scope=task_local` 的 child 都调用通用 Compact。child 只把通用
  `memory_archive/runs/<run_id>/compact_applies` 写入自己的 agent run workspace，不写 owner 长期
  Memory，也不把内部摘要交给父代理当用户上下文。
- work-state 恢复以当前 task 的结构化 goal/next action 为权威；归档工具游标只保留为事实。只有显式
  `full_source_read` 合同才可把未完成游标提升为下一动作，避免多次 Compact 后沿旧文件无限续读而偏离任务。
- 聚焦回归共 151 项通过。1.10 现有 child 压力运行留下 9 次通用 Compact，加一次候选 wheel 的只读
  apply 复核；最新 work-state 复原为原 会话运行时 审计目标，旧 通道运行时/LangChain 游标没有进入
  `next_actions`。旧 `compactions/` 文件仅是升级前运行证据，当前生产代码已无写入引用，未作为代码兼容
  路线保留。

## 2026-07-17 终态 runner 不再被重启恢复重放

- 1.10 最终文档快照切换时发现一个旧 child 的 canonical task 仍残留 `RUNNING`，但耐久
  `runner_session.status=completed` 且每次 Gateway 重启都会产生一条新的 completed session；这会重复执行
  已经结束的子代理并浪费模型调用。
- dead-worker reclaim 现在只处理 `starting/running` 且心跳过期的 runner session。`completed/failed` 等
  显式终态不会再被“宿主死亡”恢复器重放；这项判定只读取 session status、heartbeat、process epoch 和
  PID，不解析 goal、聊天文字或模型产物。终态 runner 与残留 task status 的进一步调和仍走单独生命周期链。
- 聚焦回归增加“canonical task 残留 RUNNING + completed runner session”反例，要求零 reclaim、零
  auto-start；真正的 stale running session 重启续跑用例继续通过。
- 修复提交 `80a0527d` 的 35 项恢复相关测试通过，选中 8,003 项的本地 fast suite 退出码为 0；远端 Lint
  `29595681946`、Cross-platform guard `29595681894`、Test `29595681792` 全绿。1.10 精确部署后，旧
  child 在启动扫描与至少一次周期扫描前后均保持同一个 completed session、10 条 session history 和原
  updated_at，没有再创建 runner；Gateway/Feishu `NRestarts=0`。

## 2026-07-17 `a7d6044e` 等待后自动整合真模型复验

- 1.10 新 Feishu-scoped 合成用户以普通中文要求两个协作者分别实现解析器和测试。MiniMax M2.7 只创建
  两个不同 child，27.563 秒首轮回执准确说明两项工作并明确会自动继续，没有索要进一步指示。
- 两个 child 依次进入 canonical `DONE` 后，root 的 background claim 持续心跳并自动运行约 12 分钟；
  主代理读取两个 child 报告、落成 `parser.py` / `test_specs.py`、根据真实失败多轮修复，最终同一 thread
  自然回复 23/23。没有第三个 child，没有第二个 task/history，也没有子代理命令或内部协议进入用户正文。
- 独立验收从干净临时目录重新 `py_compile` 并运行两个入口，确认 23/23、自测正常、零 symlink；root link
  为 `completed`，两个 child 都是 `DONE`。这证明等待回执后的自动续跑链成立，不把模型自报测试数当作
  独立验收结果。

## 2026-07-17 单一历史发布后 A/B 协作反证

- 1.10 精确部署 `acb1cfc5` 后，A 的 `log-lens` 按 5 个不同工作项只创建 5 个 child，最终全为 `DONE`；
  B 的 `tree-diff` 只创建 3 个 child，用户 `/stop` 后三者均为 `cancelled`，自然续作和两轮返修都复用原
  task/workspace，没有创建第 4 个 child。两个 root link 最终 `completed`，任务状态均 `DONE`。
- A/B 共 5 条当前任务引导各投递一次、零 pending；子代理命令/内部评论未进入用户 transcript。A/B 各只有
  一个任务目录，口令、owner ID、产物与 symlink 扫描零交叉。A 独立安装和 119 项测试通过，B 独立安装和
  64 项测试通过；额外外部断言分别覆盖时间/来源统计与二进制/ignore/CLI/plan reason，最终交付区无运行缓存。
- 真机也证明“子代理完成”和“主代理已正确交付”不是一回事：A/B 初次最终回复都漏过外部行为，主代理在
  同一 task 收到普通用户复核结果后继续修正。因此底座不恢复目录扫描验收器，也不把模型自报测试数当完成
  证明；外部独立验收留在用户/部署流程，普通任务仍由主代理根据实际工具事实自然收口。
- `acb1cfc5` 的父 conversation lifecycle 门已通过 CI 和 1.10 重启反证：关闭父任务的旧 child 取消，准确
  active lineage 可恢复，missing/corrupt link fail-closed hold。它不执行 LLM，也不解析任务文字。

## 2026-07-17 两用户真实任务与同任务修复验收

- A/B 两个 Feishu-scoped 合成用户分别完成 5 个长任务。普通用户没有指定子代理数量时，模型仍可按
  独立工作项自主拆分；模型入口删除的是同一 `goal` 的 `count` 克隆，不是普通任务派工能力。一个 goal
  创建一个 child，多个 child 必须提交不同 items；结构化重复项整批拒绝，不留下半创建状态。
- 子代理的工具调用、命令、评论和内部协议只写各自 run；用户只收到主代理基于结构化结果生成的自然汇总。
  本轮没有发现子代理原始碎片进入 A/B transcript。17 次 `/btw` 由主执行轮消费，也没有被展示回执或子代理
  抢先确认。
- B 的 Miniserve 修复沿用原 task `req_1784264255535_1355192_2`，没有建立新 task 或新子代理。独立黑盒
  验收从 54/58 收敛到 58/58，证明主代理可以把外部发现继续送回同一运行现场，而不是重做整项工作。
- 重启孤儿发现、分页 owner 扫描、每 manager 监督锁和 in-process 取消边界是这轮发现后的通用底座候选；
  它们只消费 task/run/session 的结构化事实，不执行 LLM，也不从任务文字、进程名或展示状态猜测权限。
- 部署前进一步发现，孤儿 run 仍缺父 conversation 生命周期授权。`acb1cfc5` 恢复门要求 parent task link 与 run link
  同为 active；终态或 interrupted 使用与 `/stop` 相同的 canonical 取消链，缺失/损坏/未知链接 fail-closed。
  这份判定同时覆盖 auto-start、dispatch、watch 补岗和周期孤儿回收，避免挡住一个入口后从另一路复活。
- 根任务的 task compact/rollup package 与 owner task/run/agent compact 索引已经删除。保留的是唯一主 thread
  history/compact、结构化 task 状态/进度/产物/agent tree，以及每个独立子代理在自身目录调用的通用
  Compact；不再存在子代理专用 Compact 实现。
  `ccb8d7f8` 已随 `acb1cfc5` 进入远端 main 和 1.10；上述 A/B 续作没有生成第二套根任务 history/compact。

## 2026-07-16 删除重复 task-node closeout 投影

- 删除未被主链消费的 `closeout_for_all_task_nodes` 配置和 `task_node_closeout` feedback-only 副本。
  runner 结果仍由 `runner_result_service.py` 一次写入 canonical status、verification、blockers、findings、
  artifact refs 和 next actions；不再同时维护第二份易漂移快照。
- 子代理完成仍要求可解析的结构化 result；声明的 `output_files/output_refs` 仅用于精确路径对账和恢复，
  不参与普通主代理任务的完成判定。主代理读取子代理结果后由模型自然汇总。
- 这项删除与普通任务的 会话运行时 式完成边界一致：没有 task-node 验收器、没有主代理完成 marker，也不从
  子代理自然语言摘要反推机器状态。

## 2026-07-10 runner 心跳窄写与 takeover 结构化 handoff

- 真实 900 秒 takeover run 产生约 197 份 compact 快照。根因不是模型反复 compact，
  而是 `runner_session_lease` 每 5 秒把 heartbeat 送进完整 `manager.save()`；完整保存
  会同步 task workspace、artifact manifest、运行投影、memory gate、daily ledger
  和 owner projections。
- `SubAgentPersistenceService.save_runner_session` 现只在 run-local guard 内更新
  `canonical_state.json` 的 runner-session/heartbeat 事实，并刷新 locator mtime 使
  `list_runs` 缓存失效。业务完整保存使用同一 guard，防止两个写者并发覆盖；heartbeat
  不再产生 compact/projection 副作用。
- takeover 创建时写入有界 `subagent_takeover_handoff.v1`，把来源 run 的状态、
  current_step、latest_summary、blockers 和 refs 注入 `context_bundle.takeover`。
  runner 先用嵌入 handoff 接续；受管状态面不能通过通用 `read_file/list_files` 读取，
  refs 仅在摘要不足且具体产物有读取授权时使用。
- 回归：runner lease 多次心跳和 completed 后 compact ledger 字节不变，`list_runs`
  可见最新 session；takeover prompt/context 含来源结构化 handoff 和读取边界。
- 第 5 个真实 runner 进一步发现：原始响应和 structured repair 都无结果块时，旧
  `record_finalized_runner_result` 会把 `found=false` 显式回填为 DONE/VERIFIED。
  现已 fail-closed 为 BLOCKED/UNVERIFIED/structured_output_parse_error；该失败类型
  保持 retryable，可在提高输出预算或 provider 恢复后重跑同一个 run，而不是制造假成功。

## 2026-07-03 持续型委派语义:service_window_seconds 端到端(底座提升 A4)

- **实锤**:盯守(无终态持续任务)派给子代理后,子代理按"做完即退"产出首批发现即
  DONE 提前收口,整任务停摆(真机 u-t1b 只 18 条;wake_queue 有 subagent-finished
  DONE)。`long_running` 此前唯一消费者是 compact 深度豁免,生命周期语义没下沉。
- **声明端**:`create_subagents` 新增 `service_window_seconds`(int,配合 long_running,
  声明最短值守窗口秒数),经 `create_policy._POSITIVE_INT_ATTRIBUTE_FIELDS` 透传
  task.attributes。唯一事实源 `agent/subagents/service_window.py`
  (`service_window_remaining_seconds`,锚 created_at——接管/重派生成新任务窗口重新起算)。
- **子代理端**:`agent_core/subagent/progress_closeout.py` 窗口未走完 → 不因"落了一次
  产物"被系统自动 DONE(收口抑制);窗口走完/未声明行为与旧完全一致。
- **父代理端（历史实现，当前固定 watch 补岗路线已删除）**:`runner_completion_wake.py`
  子代理终态且窗口未走完 → observation 概要
  + metadata 带结构化事实 `service_window_incomplete=true` 与剩余秒;整合提示词补 6b 条
  (重派或接管,别当完成)。当前只保留 task-level durable wake；是否继续、接管或委派由模型决定，
  不再由 `subagent_runner_finished` 创建固定 watch-lane 补岗。
- 决策(重派 dispatch_subagents / 自己接管)归模型;不禁止派子代理。钉子:
  `tests/test_service_window_semantics.py`(窗口语义/收口抑制/透传/工具→create_run
  端到端/wake 载荷两态)。

## 2026-06-12 来源比例观测 + 启动孤儿检测(backlog Active 清零)

- **来源比例观测**(R8b 隐蔽编造实锤:静态列表复用 24 周+公式造数,验收照过):
  `delivery_closeout/source_volume.py`——closeout 报告新增
  `source_volume_observation`(网络成功调用数/交付文件数与字节/声明 min_count
  总数并排),挂双路径。检索侧按 `ToolModelSpec.hints.category=="web"` 结构化判定零白名单;
  **设计裁决:纯观测零 finding**——"数据类 vs 分析类产物"是机器判不了的内容
  语义,任何阈值必误伤零网络的本地分析任务;比例判断留给把关者。钉子 4 条。
- **启动孤儿检测**(异常崩溃兜底,孤儿回收第二期):`startup_recovery.
  _detect_orphan_processes`——任务终态+落盘 pid 进程仍活+cmdline 含本系统特征
  (防 pid 复用误报)三条件才报告;observability 先行绝不自动 kill,启动文案
  提示用户手动处置。钉子 5 条(含防误杀三连)。

## 2026-06-12 交付写权限墙:声明产物目录围栏直授 + 拒因账本留痕(R8a 实锤)

- **实锤与取证**:R8a 主跑+接力共 17 次"子代理写自己声明的交付文件"被系统
  WRITE_FORBIDDEN(A1 账本铁证非幻觉),同子代理同文件"先拒后成"(capreq grant
  救场)证明拒因是瞬态边界状态;canonical 终态重放(含清空 grant)一律 ALLOWED
  ——瞬态不可复现,暴露"边界决策不留痕"根缺口。可控对照实验实锤独立通用缺陷:
  `delivery_root` 只认环境字段(attrs.run_workspace 等),创建链未透传时(worker
  孙派/CLI/gateway 等任何非主 run 上下文)为空 → **写边界不含声明位置,声明驱动
  语义断裂**——"output_files 声明了产物在哪,哪里却不可写"。
- **修复①声明目录围栏直授**:`output_alignment.declared_output_write_roots`——
  output_files/output_refs 声明的绝对路径父目录,过围栏(task_workspace_dir/
  task_dir/manager workspace 根,与 capability grant `_safe_grant_roots` 同款
  基准)后并入写边界(`_build_write_boundary` 接线);围栏外声明不自动授权
  (防自我扩权,仍走 capreq)。声明驱动的对偶:声明交付到哪,哪里就可写——
  不再依赖任何环境字段透传。
- **修复②拒因原文留痕**:A1 失败账本条目新增 `message`(系统拒绝/错误原文
  截断 240 字)。决策时刻的拒因(边界/锁/危险目录)随账本落盘,下次此类取证
  直读账本,不再终态重放考古。
- 钉子:test_subagent_output_alignment 增 2、test_subagent_tool_failure_ledger
  增 1(既有全字典断言按新契约更新 2)。

## 2026-06-11 R7 真实轮四通用缺陷修复（交付链路与孤儿回收的实战补强）

R7 三任务（写死产物要求）实锤暴露并当轮修复，详见
docs/audits/R7-three-tasks-20260611.md 与 REFACTORING_BACKLOG 同日条目：

- **①pid 保留权威化**：`process_control.build_background_start_record`
  （BackgroundStartUpdate 入参包）成为 background_start 记录构造唯一权威，
  agent 侧与 CLI 侧（cli/dispatch_background）共用——CLI 手写 dict 抹 pid 的
  问题根治，孤儿回收进程层恢复弹药。
- **②合同空壳回落**：closeout 的 `_no_required_artifact_response` 在合同零
  required artifact 时,有真实产物即回落 uncontracted 验收链（gate 全跑,
  含 expected_outputs 对账门）,真零产物才打回。
- **③单次运行环境事实**：`_workspace_prompt_section(single_shot=True)` 对
  source=cli_run 注入"中途请示无人应答、不要以提问收尾、结束前交付或写
  不可行报告"软约束；gateway/chat 不注入。
- **④产物候选扫描兜底（最重）**：`_current_run_task_output_artifacts` 在写入
  记录为空时回落任务交付目录文件系统扫描（仅 task_output scope、上限 200、
  每文件照常过 validate_artifact）；出口合同 `_has_final_closeout_candidate`
  contract 分支同步回落同一产物事实链。修复 compact 后/run_command 生成文件
  对 closeout 完全不可见的问题（r7b：24 个真实 xlsx 曾全链路失明）。
- 钉子：test_exit_orphan_recovery +2、test_expected_outputs_gate +2、
  test_run_task_workspace_writer +1。

## 2026-06-11 检索完备性软引导：同工具连续失败即引导枚举未试渠道（R5b/R6c 实锤专项）

- **实锤**：R5b web_search 系统失败 2 次（TOOL_UNAVAILABLE×2，A1 账本确认真失败）
  后，模型断言"数据根本不存在"口头放弃——把关者核验 OSS Insight 等渠道实可得；
  R6c 同构（单一查询口径失败即下绝对结论）。
- **落地（纯软提示，零硬门零拦截）**：`tool_guard/loop_hints.py` 新增
  `append_tool_failure_channel_hint`：
  - 触发（全结构化）：同一工具的 archive ok=false 累计 ≥
    `tool_failure_channel_hint_threshold`（主配置三同步，默认 2，0=关闭）。
    按 tool name 通用计数，零工具类型枚举；当时的 `__parse_error__` 不计；只认系统事实
    （与 A1 失败账本同源），绝不解析模型文本。
    当前实现已删除该伪工具；协议错误直接作为 host-owned violation，仍不会计成 handler 失败。
  - 动作：tool_context 注入一条枚举引导——下"数据不存在/不可行/找不到"绝对结论
    之前，先枚举已试渠道（含失败证据）与已知未试渠道（其他工具/数据源/查询字段），
    换渠道再验证；确认不可行则把枚举写进结构化不可行报告
    （tried_channels/untried_channels_known，作为可审计的不可行说明字段）。
  - 幂等：每工具最多提示一次（tool_context 标记去重）。
  - 接线：`_tool_loop_service._run_tool_round`（与 guardrail 拦截回显并排），
    主代理与 worker 子代理共用此链路，子代理同样受益（R5b 失败发生在子代理）。
- **边界诚实声明**：R6c"检索成功但只用单一署名字段"的形态，机制层无法结构化判定
  （判断查询参数的语义完备性=解析自然语言，违铁律）；这部分留给教训记忆
  （P5-2 trigger_conditions）与把关层。
- 钉子：`test_tool_failure_channel_hint.py` 5 条（阈值触发/分工具计数/幂等/
  关闭开关/成功与解析错误不计数）。

## 2026-06-11 产物类型/数量对账门：声明驱动核对交付区实存（R6b/R6c 实锤专项）

- **实锤**：R6c prompt 要求"每篇论文一个 PDF"，实交 0 PDF（仅 1 个 md 检索报告）；
  R6b 要求 1–24 周每周一份，实交 1 份——closeout 只查"有没有产物文件"，两案均
  ok=true。缺口：声明的**类型与数量**无人对账。
- **设计（纯声明驱动，守三条铁律）**：自然语言产物要求不进机器决策——模型负责把
  prompt 要求翻译成结构化声明（spec 引导），机制只对账声明 vs 文件系统实存：
  - 声明侧：`task_progress` 新增 `expected_outputs` 字段（`{pattern, min_count,
    note}`）。pattern 是相对任务交付目录的文件名或 glob（`*.pdf`），扩展名天然
    携带类型（开放世界：不写任何格式专项分支）；min_count 声明数量（默认 1）。
    归一化（坏条目/路径逃逸丢弃）、合并（同 pattern 覆盖、新 pattern 追加、
    不带声明的更新不丢已有声明）、summary 投影见
    `task_progress.normalize_expected_outputs`。
  - 对账侧：新增 `delivery_closeout/expected_outputs_gate.py`——closeout 时逐条
    glob 交付区，实存文件数（目录不算）< min_count 即
    `EXPECTED_OUTPUTS_MISSING`（medium，**repair 非硬卡死**，附 declared/actual
    对照与 required_actions）。挂 contract 路径（gates.attach_closeout_gates，
    报告字段 expected_outputs_gate）与 uncontracted 路径双入口。
  - 零声明零影响（unchecked allow 带原因）；交付要求中途变化时更新声明即可
    （同 pattern 覆盖语义）。
- 钉子：`test_expected_outputs_gate.py` 9 条（R6c 类型错配形态 / R6b 数量缺口形态 /
  目录不算交付物 / 声明持久化与合并 / 坏条目丢弃 / uncontracted 端到端打回 + 补齐
  放行）。
- 对真实任务的使用提示：测试 prompt 应把产物要求写死（"必须 .xlsx / 每篇一个
  .pdf / 共 N 份"），模型把要求声明进 expected_outputs 后，对账门才有声明可对。

## 2026-06-11 孤儿子代理回收：主代理退出前回收后台派工进程（R6a 实锤专项）

- **根因实锤**：真实模型路径的子代理派工是 `subprocess.Popen(start_new_session=True)`
  独立进程（durable 设计，`orchestration/background/dispatch.py`），不随主代理 run
  进程退出而停止——R6a 主代理 12:39 RUN_EXIT 后，后台 dispatch 进程活到 13:00
  （+21 分钟）继续往交付区写占位符。两处观测断链：①pid 只进内存 registry，落盘的
  `background_start` 属性无 pid（cancel_subagents 已预留的 `background_start.pid`
  终止路径永远 no_pid）；②CLI dispatch 进程从不更新 background_start.status
  （永远 launching），进程死活只能验 pid 不能信 status。
- **对照组**：通道运行时（级联 kill+SIGTERM→SIGKILL）/ 长期助手（ProcessRegistry pid
  落盘+kill -0 活性探测+树形终止）/ 会话运行时（SIGTERM→2s 宽限→SIGKILL 升级+进程组）
  三家全部显式 kill，不靠自然死亡。
- **落地**：
  - `subagents/process_control.py`（新）：进程治理原语唯一权威——`is_pid_alive`
    （kill -0 + 先非阻塞 reap 自己的僵尸子进程，防 zombie 误判活）、
    `terminate_pid_with_escalation`（SIGTERM 进程组优先→宽限→SIGKILL，结构化报告
    永不抛异常）。cancel_subagents 工具同步收敛到此原语（删三个私有重复，获得
    SIGKILL 升级能力）。
  - pid 落盘：`mark_background_start` 加 pid 字段——进程确认启动后写
    `background_start.pid`（status=running），后续无 pid 的 mark 不抹掉已落盘 pid。
  - `agent_core/tool_loop/exit_orphan_recovery.py`（新）：出口回收——任务工作区第一
    层子代理 + manager BFS 子树（覆盖孙代理自己 spawn 的进程）→ 非终态任务收集
    pid 去重终止（同 launch 共享进程只杀一次）→ **仅 RUNNING 任务** requeue
    （abandon attempt → PENDING，保住 resume 可重派；ABANDONED 终态会让 dispatch
    默认不捡）→ `orphan_recovery` 属性留痕（previous_status/pid_report/requeued）
    + background_start.status=terminated + work log。BLOCKED/WAITING 等状态语义与
    进程无关，只留痕不动状态。
  - 出口合同接线：`_unfinished_exit_response`（闸断放行+确有未收口）先回收再追加
    RUN_UNFINISHED_EXIT，resume 块带 `orphan_recovery` 报告；修正旧文案"进程退出后
    运行中的子代理会停止"（与事实相反）。新增 `unfinished_exit_passthrough` 直通口
    覆盖**非 break 系统截停出口**（工具轮数耗尽，R5a 形态）——不续航但同样回收+
    带 RUN_UNFINISHED_EXIT。
  - 配置三同步：`run_exit_orphan_recovery_enabled`（默认 true；false=不动进程且
    退出声明如实标注"后台进程仍在运行"）。
- 钉子：`test_exit_orphan_recovery.py` 15 条（真实 sleep 进程终止/zombie 容错/
  requeue 语义/BLOCKED 不动/孙代理子树/共享 pid 杀一次/出口接线开关双路/轮数耗尽
  直通/pid 落盘+保留）。

## 2026-06-11 锁生命周期 + 读边界 grant 闭环 + 引导前移 + 占位符明示（任务完成力底座第二/三批）

- **P3-1 锁生命周期**（R5a 实锤：23 次 WRITE_FORBIDDEN 锁的是子代理自己的交付目标）：
  `output_alignment.sanitize_self_locked_delivery_targets`——save 唯一权威口
  （persistence.save）统一剔除"锁住自己声明交付目标"的派工矛盾（含目录覆盖形态），
  结构化留痕 `attributes.locked_files_sanitized`；`record_locked_files_change` 给锁
  增删记流水账 `locked_files_changes`（R5a"中途谁动了锁"取证盲区的修复）。
  与自身目标无关的锁（兄弟产物/敏感文件）原样保留。锁来源无论模型参数
  （dispatch_subagents 的 locked_files）、takeover 透传还是 save 合并，一律过此口。
- **P3-2 读边界 grant 闭环**（R5a 实锤：子代理读不到分析材料、grant 后读边界不扩）：
  capability grant 的 path_scope 一律并入 `allowed_read_roots`（读是 grant 的最低
  权限；目录条目经执行端 workspace_roots 子树语义天然授权整棵树）。
- **P4-1 引导前移**（R5 三案实锤：A3 引导挂 closeout 不提交就看不到）：kernel 树
  快照的 `tool_contract` 存在 OPEN capreq 时直接带
  `recommended_tool="resolve_capability_requests"` + `open_capability_request_ids`，
  主代理在 inspect_agent_tree/watch 运行中即可照做。
- **P2-2 占位符明示**（R5a 实锤：交付区多数"分析文件"是兜底摘要占位）：
  materialize 兜底渲染的产物带 `placeholder: true` + 独立账本
  `attributes.placeholder_artifacts`；closeout 投影单列 `placeholder_artifact_count`。
  只观测明示，不改对账判定。
- 钉子：`test_subagent_lock_lifecycle.py`（5 条）+ capability 闭环测试读边界对偶断言
  + kernel 引导钉子 + 占位符投影钉子。

## 2026-06-11 run 出口合同：口头放弃走门 + 修复续航（任务完成力底座第一批）

- **P2-1 出口走门**：新增 `agent_core/tool_loop/final_exit_contract.py`——模型给最终
  回复（主循环 break）时，存在未收口任务态（非终态子代理 / open capreq / 派过子代理
  / 产物可验）即先走 delivery closeout；触发条件全部是结构化事实，纯问答 run 零影响。
  修复 uncontracted 的"零产物无条件早退"（R5b/R5c 口头放弃绕过所有 gate 的根因）与
  `_missing_contract_closeout_response` 用 non_terminal 报告覆盖完整阻断报告的问题。
- **P1-1 修复续航**：closeout 阻断（rework 已注入 tool_context）时在双闸内打回模型
  继续修——预算 `run_repair_max_continuations`（主配置三同步，默认 3，0=关闭）+
  进展签名闸（open 子代理数/open capreq 数/progress open 计数完全不变即停，防死循环）。
  续航状态挂 ToolLoopService 实例（每 run 新建，天然隔离）。
- **P1-2 余留合同**：REWORK 最终回复必带结构化 `resume` 块（task_root/progress_ref/
  open_count/恢复入口命令），非终态退出不再只有一段口头返工文本。
- **当前交付边界（2026-07-15 修正）**：“派过子代理”不等于“必须生成文件”。
  closeout 仍会强制聚合子代理、进度、capability 和证据事实；无显式 artifact contract
  的纯分析/问答以 `delivery_mode=message` 收口。只有显式声明 output/expected output 时，
  缺失对应文件才结构化返工。旧 `UNCONTRACTED_EMPTY_DELIVERY` 路径、schema 和恢复分支已删除。
- 行为变化：出口检查会让"未收口即收尾"的 run 多一轮续航（两个既有 closeout 测试的
  backend.calls 断言 3→4，已按新语义更新注明）。
- 钉子：`test_final_exit_contract.py` 覆盖纯问答零影响、子代理已收口时的 message
  交付、显式 artifact contract 缺失返工、open ledger 续航和 resume 块。

## 2026-06-11 确定性优先三件套：系统级工具失败账本 + 写边界一致性钉子 + capability 软引导（开发计划 A1-A3）

- **A1 系统级工具失败账本**（根治 R4b 模型归因幻觉）：新增
  `subagents/tool_failure_ledger.py`——子代理一轮 `agent.run` 的
  `archive_tool_calls`（canonical ToolResult 的 ok/error_code 投影，系统事实）
  提取 ok=False 摘要（tool/call_id/error_code/target），经
  `RecordRunnerResultParams.tool_failures` 写进
  `task.attributes["tool_failure_ledger"]`。语义：`[]`=系统确认零失败（强事实，
  拆穿模型口头归因），`None`（超时/worker 异常拿不到数据）不覆盖旧账本。closeout
  的 `unresolved_children` 投影新增 `tool_failure_codes`（error_code→次数）——模型
  summary 声称 WRITE_FORBIDDEN 而系统账本为空时，矛盾在报告里直接可见。
  纯观测，不做硬门。钉子：`test_subagent_tool_failure_ledger.py`（11 条，含 R4b
  幻觉对照形态）。
- **A2 写边界一致性钉子**：`test_subagent_output_alignment.py` 增三条——R4b
  "祖先级 forbidden（/Users/<user>）不拦已授权交付区 + allowed 内 forbidden 子树
  必须收窄"双向钉死 narrowing 语义；boundary 的 forbidden/locked 恒等于 task
  字段（同源无漂移）；首轮 attempt 零 grant 即含交付区且重复构造稳定（排除
  时序窗口）。
- **A3 capability 闭环结构化软引导**（R4b 主代理 0 次调用 resolve 的针对性修复）：
  `SUBAGENTS_CAPABILITY_REQUESTS_OPEN` finding 的 evidence 新增
  `recommended_tool="resolve_capability_requests"` + `open_capability_request_ids`
  （主代理直接拿去调用，不必从文本猜）；`required_actions` 里的
  `resolve_open_capability_requests` 改为真实工具名（防诱导调用不存在的工具）。
  软引导，不拦主链路。

## 2026-06-11 失败自省 split 建议生产→消费打通（自动拆分闭环）

- 打通点：`FailureIntrospector` 产出的 `split_suggestions` 此前无人消费（死路）。
  现在 `_handle_failure_introspection` 在调参后调用新增的 `_apply_introspection_split`：
  `should_split` + 建议非空 + 配置开启 + 深度未超限时，复用
  `failure_analysis_service.split_task` 把失败任务真实拆成子任务（子任务 PLANNING
  先落盘、原任务 TAKEN_OVER 后落盘），拆分账本记进
  `failure_introspection_data.split_applied/split_into`，跳过原因结构化记录在
  `split_skipped_reason`（auto_split_disabled / depth_limit:N）。
- 配置（capability_config 三同步）：`subagent_failure_auto_split_enabled`（默认 false，
  拆分会创建新任务需显式开启）+ `subagent_failure_split_max_depth`（默认 2；0=不限制）。
  运行时读取统一走新增的 `capability.runtime_config_reload.capability_config_for_agent`
  （快照→缓存加载的唯一权威；context_compactor 原私有重复实现已收敛到它）。
- 消费 key 与生产端对齐：`new_timeout_seconds`（已有钉子）、`max_tool_rounds`
  （消费链真实存在，留给 LLM 自省/人工注入）；删除 `split_goal` 字符串拼接分支——
  它是无人生产的影子拆分路径，拆分唯一权威是 split_task 子任务。
- 自省吞异常修复：load 失败仅日志；apply 段失败把 `runtime_error_report` 写进
  `task.attributes["failure_introspection_error"]` 并补落盘，留痕再失败才降级日志；
  任何情况不向 runner 主链路抛异常。
- 钉子：`test_real_class_integration.py` 两条真实链路钉子（开关开→子任务真实落盘
  可派工；默认关→只记 skip 原因行为不变）；`test_dispatch_mixin.py` 两条留痕钉子
  + 救活 4 个曾被错误缩进成嵌套 def 的死测试（pytest 收集数 1→23）。

## 2026-06-10 合约身份 helper 去重

- 幂等/修复合约身份两个文件合一，消除 3 个逐字重复的私有 helper；纯内部去重，
  公开入口函数名和行为不变，focused 合约测试全绿。

## 2026-06-10 服务层 facade 清理与链路拉直

- 删除 `services/__init__.py` 的 11 个 service re-export；manager 与调用方全部直连实现模块，导入链不再经过包枢纽。
- `services/capabilities|runner_context|runner_result/` 三个单模块包打平为同级 `*_service.py` 文件，相对导入深度同步减一。
- `parsing/__init__.py` 尾部对 `envelope` 的反向 re-export 删除，`services/hierarchy/__init__.py` 清空转发；两处模块级循环导入消除（AST 级检测确认全包无真循环）。
- 行为不变：仅导入路径调整，focused tests（manager/board/hierarchy/parsing/capability requests）全绿。

## 2026-06-09 状态投影降噪

- `status --json` 的 `subagents.hot` 只显示当前可行动的近期风险项；超过 72 小时没有更新或进展的
  风险项计入 `historical_hot_count`，不再占满默认状态输出。
- 历史风险项不被删除，仍可通过 subagent board、`subagents --all` 和审计文件查看。这个变化只影响
  人和 gateway/chat 默认状态投影，不改变子代理调度、验收或历史记录。
- `SubAgentBoardItem` 现在携带结构化 `created_at`，启动恢复检测不再因为缺字段把 active work
  误报为 0；如果检测失败，`status --json.active_work.detection_errors` 会显式暴露结构化错误。
- `status` 展示层会截断长 goal，并拆出 `current_summary` / `history_summary`；
  原始 goal 和完整历史仍在 task/board/detail 事实源里，状态页不再承担大报告或历史审计职责。
- `subagents` 默认命令同样只展示当前 hot 或最近项；历史 hot 只计入 `historical_hot`。
  需要看完整历史时显式使用 `--all` 或 status/owner/root 过滤。

## 2026-06-07 真实 all-agent 子代理对照

- 真实运行目录：
  `validation/real_runs/20260607-000144-subagents-all-agent`。
- Prompt 使用普通中文：主代理找几个帮手分头阅读 `/Users/example/study-agent/all-agent`
  下的项目，最后由主代理核对、合并报告并提交验收。
- 运行完成并 `submit_for_acceptance` 通过：`tool_rounds=51`、
  `ctx_tokens≈137464`，任务内触发真实 compact 1 次，compact 后继续读取子代理产物并完成提交。
- 子代理链路可跑通：task workspace 下产生 14 个子代理目录，最终均为 `DONE`；
  `output/` 下生成综合报告和 12 份项目分报告。
- 暴露的真实问题：
  - 主代理会高频重复 `inspect_agent_tree` / `list_files output`，虽然已有 `wait` 工具，
    但 `inspect_agent_tree` 的重复查看 cooldown 默认只有 30 秒，模型一轮往返常常刚好越过该窗口。
  - `read_file` 读目录时返回过泛的失败面，真实 run 中表现为 `UNKNOWN_ERROR`，不利于自动改用
    `list_files`。
  - 无结构化交付合同时，uncontracted closeout 只能验收“本轮写了报告”，不能证明用户列出的
    每个项目都被等质量覆盖；本 run 中 `letta-main` 路径不存在，最终 note 仍说“13 个项目全部完成”，
    但独立分报告实际是 12 份。
  - `my_agent_main.md` 只有 25 行，说明父代理汇总时会过度相信子报告存在，缺少覆盖质量结构化索引。
- 已修复：
  - `inspect_agent_tree` 重复查看 cooldown 默认改为读取
    `subagent_watch_interval_seconds`，仍允许显式 `cooldown_seconds: 0` 关闭；这是软提示和缓存摘要，
    不是硬门。
  - `inspect_agent_tree` 在 cooldown 内先比较 task-local `task.json` 的 mtime/size 指纹；
    树状态没变时直接返回上次的紧凑提示，不再完整读取和渲染整棵树。任一 task 状态文件变化时仍回到完整
    kernel snapshot，避免隐藏新进展。
  - `read_file` 遇到目录返回 `PATH_IS_DIRECTORY` 和建议的 `list_files` 调用，不再给泛化未知错误。
  - 默认配置 `workspace_root` 改回空值，保持“未配置时使用启动目录”的主链路语义。
  - `create_subagents` 不再向模型暴露 `count` 克隆模式。多个 child 只能用不同的
    `items` 显式派工，重复项整批拒绝；顶层 `output_files` / `output_refs`
    不会复制给所有 child。
  - 子代理 runner finalize 现在优先识别当前 run 的
    `[MAIN_AGENT_DELIVERY_COMPLETE]` 成功块：如果 runtime closeout 已经验收 task
    output 产物，就合成标准 `DONE` / `VERIFIED` 子代理结果，不再进入
    `SUBAGENT_RESULT` repair 轮把 task output 误判成源目录缺文件。

## 2026-06-09 子代理路径合同收敛

- 真实 live lab 暴露子代理执行上下文同时暴露旧 `.my_agent/subagents/<run_id>` locator
  和当前 task workspace，导致模型把旧目录当自己的 `task_dir`，artifact 聚合也会接受旧目录产物。
- 当前修复把模型可见 `task_dir`、write boundary 和 protocol write contract 收敛到
  `tasks/<date>/<task>/work/agents/<run_id>/`。旧 locator 只保留为 owner/index 查找面，
  不再作为模型写入根或 artifact 候选根。
- `output_files` / `output_refs` 中带括号占位符路径段的值，例如 `[任务目录]/report.md`
  或 `[workspace]/report.md`，会被视为非真实文件合同；不会进入 required refs、
  allowed write roots、declared output refs 或自动物化路径。
- 这不是按中文词判断，而是结构规则：括号占位符路径段不是当前 run 的真实文件路径。
  真正的输出目录仍必须来自结构化参数、当前 task workspace、授权写入根或用户明确给出的普通路径。

## 2026-06-06 主链路小跳转清理

- 删除只服务单一调用点的 facade/helper 文件，把能力请求解析、action rescue 渲染、runner
  guidance 注入、runner tool 过滤和 root task policy 折回当前权威模块。
- 真实主代理 all-agent 阅读任务暴露了工具协议示例污染：目录示例仍写
  `parameter_name` 占位字段，模型会照抄成错误工具参数。当前工具协议示例改为真实
  `read_file` 顶层参数；registry 统一拒绝未知顶层参数并返回 `TOOL_INVALID_ARGUMENTS`，
  不再让 `list_files` 这类有默认值的工具静默把错参当成功。内部字段通过
  `ToolRuntimePolicy.input_policy.internal_parameters` 隐藏声明，不展示给模型。
- persistence 保存链路继续收直：`identity`、`security`、`status_report`、`failure_handoff`、
  `inheritance`、`output_load_errors`、`recovery_outputs` 等只服务持久化保存的私有 helper
  已折回 `persistence/service.py`；`thought.md` 渲染折回 projection 写入点。
- session progress 写入链路继续收直：工具结果路径提取不再放单独 `paths.py`，而是跟
  `record_subagent_tool_progress` 保持在同一入口里，方便排查“工具写了什么、进度如何投影”。
- subagent markdown 渲染继续收直：dispatch/watch/parent planner 和 patch review/apply
  渲染不再通过 `rendering_dispatch.py`、`rendering_patch.py` 两个中转文件跳转，统一由
  `subagents/rendering.py` 持有。
- `runner_context` 现在直接构造执行上下文、写入边界、runtime guidance 和 runner allowed
  tools；角色模板相关判断留在 `role_templates`。
- `subagent_mixin.py` 现在直接持有 run/finalize、结构化修复、recovery snapshot 和 parent
  planner 记录链路；旧 `_subagent_repair_mixin.py`、`_subagent_planner_mixin.py`
  两个私有跳转层已删除，跨模块参数类统一放在 `agent_core/subagent/params.py`。
- 这轮清理不新增工具、不新增硬门，只减少跨文件跳转和旧入口。
- 父代理汇总子代理结果时，优先读取创建/树快照返回的 `child_output_read_order`、
  `primary_artifact_refs` 和 `expected_outputs`。没有声明产物路径的子代理会获得
  task-local `work/child_outputs/...` 默认产物路径；`items` 里各 child 声明的
  `output_files` / `output_refs` 依然各自独立。`work/agents/<run_id>/`
  继续作为内部状态、审计和恢复目录。这里记录的是 2026-07-29 的旧控制面；2026-08-21 起公开
  `inspect_agent_tree` / `wait` 已退休，父级由直属生命周期事件恢复并从上下文 refs 读取结果。

## 2026-06-06 状态精确化

- 子代理运行、恢复、tree、closeout 统一按当前协议状态判断；`COMPLETED`、`SUCCESS`、`ERROR`
  等旧标签只保留为原始审计文本，不再隐式兼容成 `DONE`、`FAILED` 或 `CHANNEL_ERROR`。
- 显式写入子代理状态时只能使用当前 `TaskStatus` 协议值；`completed`、`succeeded`
  这类旧成功别名会 fail closed，不会静默改写任务状态。
- dispatch 候选和 runner 子结果摘要继续收敛到当前 `TaskStatus` /
  `DISPATCH_INELIGIBLE_STATUSES`；`CANCELLED`、`ABANDONED`、`TAKEN_OVER`
  不再被漏判成未完成子代理，`PAUSED` 仍按未完成保留给父代理处理。
- remembered run unfinished、parent-timeout recovery、checkpoint/state recovery 和 board risk
  也改为调用 `subagents.models` 的共享状态 helper；旧大小写/别名状态不会在这些链路里
  被各模块单独解释成完成、失败或可收口。
- agent tree 展示、due-check、leadership recovery、recovery orchestration、runner
  payload 和 QA repair payload 也不再维护本地失败状态集合；机器判断统一走
  `TaskStatus` / `SUBAGENT_FAILURE_STATUSES`，展示文案只消费已经归一的状态。
- 派发状态投影遇到旧标签或未知状态时，仍保留原始 status 供审计，但不会给父代理
  `summarize_or_report_verified_runs` 这类收口建议；必须先检查 agent tree 或人工处理。
- 恢复状态机不再把 `PLANNED`、`QUEUED`、`WAIT_CHILD` 旧别名提升成当前协议状态；旧状态进入
  `manual_review`，避免跨版本残留污染当前 run。
- 缺少结构化 `failure_type` 时，恢复快照不再从 `runner_last_error` 或自由文本错误里猜恢复码；
  工具错误文本分类只保留在工具结果诊断层，不能替代任务状态事实。
- 恢复 mode 只认当前显式枚举：`rerun_from_checkpoint`、`takeover_from_checkpoint`；
  不再保留子代理专属 continue-packet 分支，也不再用 `rerun_*` / `takeover_*`
  前缀把未知旧值提升成自动重跑或接管。
- capability 等待状态只认当前协议 `PENDING_CAPABILITY_REQUEST`，不再把 `NEEDS_TOOL`、
  `WAITING_FOR_TOOL` 等旧/模糊状态别名自动升级成能力申请。
- capability request 自身只认当前状态：`OPEN` 是待处理，`GRANTED` 是已授权，
  `GAP` 是没有可用能力，`CLOSED` 是本轮已关闭。历史 `RESOLVED`、`APPROVED`、
  `REJECTED` 不能静默当成已处理终态；它们会继续作为需要人工/路由处理的状态暴露出来。
- 工具结果没有显式 `error_code` / `error_type` 时，机器错误码统一是 `UNKNOWN_ERROR`；
  日志里的错误正文可以给模型看，但不能反推出结构化错误码、任务状态或验收结论。
- Tool Gateway 自己产出的结构化 finding 不属于“没有显式码”：实际强制执行管线及 path/command、owner scope、
  rate limit/circuit、approval binding、idempotency 等直接子门的稳定码必须注册到统一 taxonomy。门禁已识别
  `COMMAND_PARSE_FAILED` 时要原样保留并给出 `repair_tool_call`，不能再次降级为 `UNKNOWN_ERROR`。
- 本地运行时失败的模型提示只读取结构化 `context` code，例如 `*.subagents.load`
- 子代理 task workspace 的身份只来自当前 `root_id` / `id` / `parent_id` 和明确的
  `run_workspace.task_root`；已有 `work/state.json` / `work/task.yaml` 不再反推本轮
  task_id，避免同名旧目录污染当前子代理树。
  或 `conversation.guidance.*`。普通错误文本或自由格式 context 里出现
  `subagent/load/guidance` 这类词，不会改变失败类型、任务状态或验收语义。
- `DONE` 仍是唯一已完成状态；`FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`BLOCKED`
  是可恢复/阻塞状态，恢复器和 strategy 只扫描这些结构化状态。
- offline subagent closeout contract 也只认当前 `TIMEOUT`；`TIMED_OUT` 这类旧别名
  只能作为异常/未知状态处理，不能触发 `CHILD_TIMEOUT` 语义。
- `blocked_reason` 只是解释字段：它可以写入 blockers、报告和父代理提示，但不能单独把
  `DONE`、`RUNNING` 或未知状态改成 `BLOCKED`，也不能把 `failure_type` 猜成
  `capability_request`。需要阻塞时必须写结构化 `status=BLOCKED`、`failure_type`
  或正式 `capability_requests`。
- `output.json`、checkpoint 和 QA payload 只读结构化字段、`ok` 布尔、blockers 和 refs；
  summary、角色描述、旧状态词只作为展示或软上下文。
- 子代理结果产物只从当前结构化 schema 进入 artifact refs：`artifacts`、
  `artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
  `deliverables`、`files`、`output_files`、`files_modified`、顶层 `path/file_path`
  等旧结果别名不再被悄悄恢复成产物；派任务时的 `output_files` 仍是创建子代理的目标路径字段。
- `create_subagents` 不再用 `working_buttons`、`verified_images`、`no_comments`
  这类专项交付约束做入口硬拦。它们可以作为结构化任务上下文传给子代理，最终由父代理验收、
  QA 或 closeout 证据判断，不在派工前阻断主链路。
- test failure classification 不再从 stdout/stderr 文本里的 `SyntaxError`、`AssertionError`
  或超时词猜恢复类别。机器分类只读 `executed`、`passed`、`validation_method`、
  `validation_result.reason` 等结构化字段；输出尾部只保留为人类审计摘要。
- 全局错误 taxonomy 不再用多语言正则从普通错误正文猜 `TOOL_TIMEOUT`、`WRITE_FORBIDDEN`
  等机器码。工具/后端/runner 必须产出显式 `error_code` 或 `failure_type`；没有结构化码时就是
  `UNKNOWN_ERROR`。
- 测试准备不再把 `command: static_site_check` 解释成验证器选择。验证器入口只认
  `validation_method`；`command` 是实际执行命令，不承担 schema 选择。
- 文件存在性验证只认 `validation_method: file_check`。旧的 `file_exists`、
  `path_exists`、`artifact_exists` 不再作为可执行验证方法别名。
- 协作能力匹配只读取显式 capability 字段和真实工具名；不再从工具名里的
  `read/search/write/dispatch` 等字样自动生成 `query/write/delegate` 这类抽象能力。
- 工具动作布尔参数只认 JSON 布尔、数字和 `true/false/1/0`；`yes/on/apply/run/full`
  这类普通词不能改变执行行为。

## 2026-06-04 收敛

- 删除旧 manager mixin 和过渡转发文件，`SubAgentManager` 现在直接拥有初始化、基础生命周期和工单路径。
- `SubAgentBoardService`、`SubAgentPatchService`、`SubAgentHierarchyService` 直接作为当前服务入口，不再保留单独转发文件。
- CLI 子代理命令注册合并到 `cli/subagents.py`，不再保留单独的 registration / hierarchy 注册 facade。
- 子代理状态继续以 task-local canonical state 为权威；owner projection 和 global index 只做查找。
- runner 终态通知不再分别调用 observation/wake 两个写入口；统一由 conversation store 先发布
  durable wake，再追加带 wake ID 的 observation。这样调度器看见 observation 时，对应 wake 已存在，
  同一次 DONE 不会触发两轮后台主代理；wake 队列失败时仍保留 observation fallback。
- `cancel_subagents` 是父代理处理卡住下级的控制面：可取消、废弃 attempt、记录审计，再由父代理接管或汇总；如果 canonical loader 读不到该 run，会返回结构化 load error，不用旧路径扫描假装取消成功。
- 当时的 `inspect_agent_tree` 重复查看只给紧凑提示，并用 `wait` 登记间隔；这条历史工具面已于
  2026-08-21 退休，当前等待由宿主直属事件链完成，不再让模型轮询。
- `create_subagents` 不再因为已经有活跃子代理就默认拒绝第二批；父代理可以先派一批，后面按需要继续派。
- 子代理可以写 task workspace 里的协作产物；最终交付由主代理汇总到 `output/` 或用户指定目录。

## 2026-07-14 IM 批量派工、交付路径与完成投递收口

- 原生模型在同一轮返回多个 `create_subagents` 时，运行时按顺序执行全部创建；依赖上一调用返回 ID 的
  dispatch/inspect 等编排动作仍延到下一轮。延后结果使用 `ORCHESTRATION_CALL_DEFERRED`，避免真实原因
  落成 `UNKNOWN_ERROR`。公开派工回执聚合这一轮全部 lifecycle envelope，只报告账本确认的
  recorded/accepted/running 数量。
- 已绑定当前任务且用户未显式指定输出目录时，模型生成的 owner-home 任务外绝对交付路径会归入当前
  `task/output/`，goal/thought/plan 中相同引用同步改写；显式用户目录仍按 capability 与路径边界执行。
- 后台自动续跑和成功子代理终态只在统一用户回复投影为 `delivery_complete` 时外发；监督、等待、内部
  整合和占位文字不会写入普通聊天。失败、阻塞和需决策继续按结构化事件及时投递。
- 完成轮同时有 findings delta 时，以结构化 closeout 为投递权威；IM 信封使用投影后正文，不把内部协议
  或 findings 的原始宿主路径传给 adapter。定向钉子覆盖“同轮完成+delta”只产生一条干净最终回复。

## 2026-07-16 主代理与子代理工具运行身份分界

- `tool_loop.recovery.runtime_run_scope` 不再对任意 `run_id` 调用 `subagents.load`。只有 runner 的线程级
  subagent context，或显式 `context_scope=task_local`，才具备查询子代理 canonical ledger 的结构权限；
  不从 `bg-main-*` 名字或普通 prompt 推断身份。
- 后台主代理的工具 envelope 继续记录本轮 `run_id`，但 `root_task_id` 使用会话绑定的持久任务 ID；
  因此不会每轮产生“子代理记录不存在”的假异常，也不会把临时唤醒轮误当成根任务事实源。
- 真正的子代理仍从 canonical task 恢复 parent/root/depth；账本不可读时仍输出结构化 load error。
  root、load-failure child、grandchild lineage 及相邻后台/runtime envelope 专项回归均已通过。
- `raise_collaboration` 不再要求后台主代理重复传 `task_id/thread_id`：显式参数仍优先，真实子代理仍优先用
  runner context，后台根任务则读取工具执行链注入的 `RunScope.root_task_id` 并反查会话。该字段来自结构化
  envelope，不从 `bg-main-*` 名字或自然语言推断。缺作用域、未知 thread、存储读取失败分别映射到已注册的
  parameter/arguments/execution 控制码，原始协作域报码继续作为 `reported_error_code` 写入结果 envelope、
  tool-output artifact 和 index。
- 参考实现核验：会话运行时 `multi_agents/spawn.rs` 在创建时直接写 `parent_thread_id`，后续通信使用明确的
  receiver thread ID；通道运行时 的 subagent registry 持久化 requester/controller/child session key。
  本实现复用同一模式——父任务身份在创建/执行边界结构化携带，协作工具只消费该身份，不自行猜测。
- 1.10 真机自然回执曾把结构化键 `runner_confirmed_running` 直接翻译为“runner 尚未确认”。表达层 facts
  现改为 `work_items_planned/ready/started/failed_to_start`；内部 lifecycle envelope 与运行裁决保持不变，
  用户回复继续由模型生成，不使用术语正则或固定模板。通道运行时 的 internal announce→parent wording 与
  会话运行时 的 contextual subagent notification 均采用相同的内部事件/用户表达分界。

## 2026-07-17 明确派工与重启后独立回收

- 模型侧 `create_subagents` 删除 `count` 克隆入口：单个 `goal` 只创建一个 child；需要并行时，
  模型必须在 `items` 中列出不同的具体工作。相同 `goal/role/replacement_for_run_ids` 且没有不同
  `input_refs/artifact_refs/output_files/output_refs/covers` 边界的 item 会整批拒绝，不留下半创建记录。
  管理员 CLI 的低层 `spawn-subagents --count` 不属于模型入口，本轮没有改动。
- 该收口来自 1.10 双用户真实长任务：模型先用 `count=3` 克隆同一可写目标，又派两个明确 item，
  导致五个 child 中三者覆盖同一批文件。新入口保留模型按真实拆解自主决定数量的能力，但只能通过
  明确的不同 items 表达，不能用数量字段制造相同 worker。
- Gateway 新增不执行 LLM 的 orphan reconciler（孤儿回收器）线程。它按
  `orphan_supervision_interval_seconds` 独立扫描 base 与 owner-scoped manager，调用现有结构化
  `supervise_stalled_orphans`，不会因为某个后台主代理正在做数分钟模型/工具调用而停止第二次巡查。
- owner 磁盘发现改读真实的 `owners/.../agents/<run_id>/state.json` 投影；旧代码读取并不存在的
  owner-level `task.json`，进程重启后会漏掉未完成子代理。冷启动回归使用全新的 Gateway agent 和
  owner pool，仅凭磁盘投影成功找回旧 `RUNNING` run、回收为 `PENDING` 并精确重新派发。
- 磁盘发现与 owner registry 都受 `owner_agent_pool_max_agents` 约束，但容量只限制单轮常驻量，不能
  决定谁永远没有恢复机会。发现器保存结构化分页游标，每轮从上次位置继续；页大小约束的是实际检查的
  owner 数，不只是本页命中的待恢复 owner 数，因此前面即使全是空闲 owner，也不会一次打开其后的全部
  状态目录。70 个待恢复 owner、每页 16 个的回归在 5 轮内无遗漏；另有 10 个空闲 owner 后接 1 个待恢复
  owner 的稀疏回归，确认每页扫描不超过 3 且最终可达，registry 仍保持有界 LRU。分页边界对照 会话运行时
  thread store 的 `page_size + next_cursor` 以及 长期助手 ACP `list_sessions` 的服务端页上限；本轮没有把
  “找到多少命中”误当成“扫描成本有界”。
- 调度器、后台主代理和独立回收器可能同时触发同一巡查，因此每个 manager workspace 通过
  `subagent_orphan_supervision.lock` 串行化。竞争者只返回结构化 `skipped_locked=1`；锁内真实异常仍
  按原路径上报，不能被误当成“有人正在处理”。
- 同一轮真机收口还确认了取消边界缺陷：进程内 runner 的 `worker_pid` 是 Gateway 宿主 PID，旧
  `cancel_subagents` 把它当独立子进程发送 SIGTERM/SIGKILL，实际杀掉了整个 Gateway。现在
  `runner_session.in_process=true` 是结构化拓扑事实，只走线程协作中断；只有
  `in_process=false` 的独立 runner 才允许走进程信号路径。该分界对照 会话运行时
  `AgentControl::interrupt_agent -> Op::Interrupt`、通道运行时
  `killSubagentRun -> abortEmbeddedAgentRun -> handle.abort()` 与 长期助手
  `interrupt_subagent -> agent.interrupt()`，不从任务正文或进程名猜测。

## 运行约定

- 子代理没有长期个人记忆，只保留 task-local 状态、事件、artifact refs、compact 和候选经验。
- 子代理模板可以定义简短 persona、description、skills 和机器能力字段；当前 runner 仍走现有执行链路。
- `role` 是模板选择字段，`agent_name` 只是展示名。创建任务时会把模板能力快照写入
  `attributes.role_template`，后续调度、恢复、timeout 和 runner prompt 只读这个快照或模板字段，
  不从显示名或普通中文/英文描述里猜角色。
- 默认 `agent_name` 也只作为展示标签，格式为 `agent-d<depth>-<role>-<index>`；多层级调度只用
  `depth` / `parent_id` / `root_id` 等结构化字段，不再从默认名或用户叫法里解析层级。
- 层级继承标记只写 `attributes.inherited_parent_context=true`；给模型阅读的 `goal`
  不再塞 `inherited_parent_context=true` 这类内部机器标记。
- capability request/grant/gap 是可观察工作项，不是默认阻断任务的硬门。
- 普通中文任务不进入隐藏 workflow mode；可复用方法由 Skill 提供，计划和执行继续使用同一 thread、`task_progress` 与原生子代理工具。

## 2026-06-11 R4 交付链路四子项落地

- 执行合同产物落点对齐（R4 子项①）：attributes 里的 `output_files`/`output_refs`
  保持"最终交付意图"不动；`services/output_alignment.py` 投影层把执行合同
  （task_packet/output_contract）的目标 refs 翻译成子代理自己 `output_dir` 下的可写
  落点，`write_contract.output_delivery_map`（落点→意图位置）渲染进 runner prompt；
  落点被 locked_files 盖住记结构化 `OUTPUT_TARGET_LOCKED` warning，不静默。
- capability 处理回路（子项②）：新增模型工具 `resolve_capability_requests`
  （grant/deny 显式裁决；grant 落 path_scope+写工具即时生效到写边界，目录围栏=
  任务工作区+主代理 workspace，越界结构化拒绝；deny 走协议终态 CLOSED+
  denial_reason）；`capability_request` 提交后立刻向父级线程发 requires_main_agent
  观察+wake（`runner_completion_wake.notify_parent_on_capability_request`）。
- 声明产物对账（子项③）：closeout 的 subagent_aggregation gate 新增
  `SUBAGENTS_DECLARED_OUTPUTS_MISSING`——DONE 子代理声明产物在声明位置缺失即
  NEED_REPAIR；同时修复 gate 把 runner 自己算成未完成子代理的自指拦截
  （评估时排除 closeout 当事人 run_id）。
- 汇总搬运（子项④）：runner result 写回时 `deliver_anchored_outputs_to_declared`
  按 delivery_map 把锚定落点的真实产物搬到声明位置；缺失声明不生成 summary 占位文件。结果进
  `attributes.output_delivery_results`（delivered/skipped_existing/source_missing/
  target_outside_workspace）。

## 2026-06-11 产物落点改回家目录交付区方案

按用户确认调整子项①的落点方向：默认交付区 = 任务工作区/output（用户主目录下
tasks/<日期>/<任务>/output，即用户拿走的东西），而非子代理家 work/agents/<id>/output。
- `output_alignment.delivery_root(task)`：优先 attributes.run_workspace.output_dir
  （主代理/用户显式指定），否则 task_workspace_dir/output。
- `output_write_grant_roots(task)`：交付区授权进真实写边界
  （runner_context_service._build_write_boundary）和模型可见 allowed_write_roots
  （context_bundle_contracts.allowed_write_roots），子代理直接写交付区、用户拿走即可。
- delivery_map 收窄：只记"声明落在交付区外、被重定位"的条目（reason=relocated_*）；
  相对声明锚到交付区、已指向交付区的绝对声明都不进 delivery_map、无需搬运。
- 搬运（deliver_anchored_outputs_to_declared）降级为兜底，只处理任务外/任务内非
  交付区的少数声明；锁冲突 OUTPUT_TARGET_LOCKED 警告与声明对账全部保留。

## 2026-06-11 声明对账加结构化豁免出口

- `resolve_capability_requests` 复用扩展第三个 decision `accept_output_gaps`（不新增
  工具，按参数复用）：把声明产物缺失豁免登记到子代理
  attributes.output_delivery_exemptions（{ref, reason, accepted_by, at}）。
  exempt_refs 指定具体声明，缺省登记通配 "*"（整体豁免，用于纯汇报任务/已确认接受）。
- closeout 的 `_missing_declared_refs` 先扣除豁免再算缺失：通配 "*" 直接返回空缺失；
  具体 ref 豁免逐条扣除。SUBAGENTS_DECLARED_OUTPUTS_MISSING 拦截因此可被显式解除。
- 豁免是结构化、可审计记录，不是静默放水、不中断任务；gate 文案与 required_actions
  指向 resolve_capability_requests(decision=accept_output_gaps)。

## 子代理产物写区兜底(batch3 C3/G4 实锤,2026-06-13)

- 实锤:子代理 `allowed_write_roots` 只含自己的 agent 目录(没声明 output_files、
  declared_output_write_roots 未生效)时,`task_product_write_roots` 过滤掉自己
  目录后为空 → `run_tool_preflight` 报 `missing_allowed_write_roots` → 子代理
  写不了产物 → BLOCKED → 主代理空等、未收口(C3 主代理 112 轮 0 write)。
- 修复:`runner_context_service.task_product_write_roots` 在结果为空时回退到任务
  工作区 `task_workspace_dir/{output,work}`——子代理总有产物写区(交付事实优先,
  减少"必须先声明才能写"的过度约束),围栏在本任务工作区内、安全。
- 钉子:test_subagent_output_alignment.test_product_write_roots_fallback_when_only_agent_dir
  (只自己目录→回退 / 有声明→用声明不回退 / 无工作区→空不崩)。

## 2026-07-18 capability grant 同 run 续跑与互斥状态事实

- 1.10 真任务证明：child 已记录 capability grant 并扩充 `allowed_tools` 后，conversation task link 仍可能
  保持 `blocked`；下一次 runner selection 会被 conversation lifecycle gate 拒绝，父任务随后只能取消该 child。
- `services/lifecycle.py::record_capability_grant` 现在保存 grant 后，只对同一个 `BLOCKED +
  failure_type=capability_request` child link 做 `expected_status=blocked` 的 `active` CAS。若 `/stop` 或其他
  并发动作已把 link 改为 cancelled/terminal，CAS 返回空，不会复活旧任务。更新失败以结构化 runtime report
  留在 task attributes，不把写失败伪装成授权成功后的可运行状态。
- 回复层不再同时提供 `active/finished/issues` 三组重叠统计；每个 child 只进入一个 canonical status，模型
  只看到 `total + status_counts`。事件 lineage 优先读取结构化 `source_agent_id`，避免拿 root task 当 child
  加载产生假错误。
- 对照 会话运行时 `AgentStatus::Interrupted` 的“同 agent 可继续接收输入”、`WaitAgentResult` 的 per-agent 精确
  status map 和 `SubagentNotification(agent_reference,status)`，my-agent 保持自己的 run/link 事实源与 CAS，
  不从用户文字、child 摘要或显示名推断恢复权限。
- 聚焦回归覆盖授权后同 worker 第二次执行并 DONE、`/stop` 后 grant 不复活、互斥状态计数总和恒等于
  child 总数，以及 child/root lineage 精确投影。

## 2026-07-19 声明产物不再自动物化

- 1.10 的真实差旅任务复现了确定性假产物：子代理只实际写出一个政策 JSON，但旧
  `materialize_missing_declared_output_artifacts` 把该文本复制到另外七个声明位置，CSV、测试文件和
  README 因而具有相同内容与 SHA；找不到可复制文本时，旧链还会把结构化 summary 渲染成占位文件。
- 当前主链删除了整个缺失声明物化入口及同名递归搜索、按文本后缀挑源、summary 占位、placeholder 账本
  等死代码。`result_structured` 现在只登记真实存在的 runner artifact；声明路径保持交付预期，缺失事实由
  现有声明对账暴露。只有 typed `output_delivery_map` 明确给出的现存 source 才能搬到受围栏约束的 target。
- 代码级参考是 会话运行时 `会话运行时-rs/core/src/apply_patch.rs` 与 protocol 的 `PatchApplyEndEvent`：文件变化来自
  真实 patch action 和 success 状态；通道运行时 `src/agents/subagent-announce-output.ts`：父代理读取并复核
  子会话真实 assistant output，不根据预期文件名生成宿主文件。my-agent 只适配这条事实边界，没有新增
  IM 分支、自然语言判断或另一套验收器。
- 回归覆盖“一份真实 JSON + 七份缺失声明”不会产生克隆、工作区内同名文件不会被搜索搬运、明确 delivery
  map 仍可交付真实 source；全部 subagent/result/artifact 相关测试已通过。

## 2026-07-19 completed parent 与真实执行态交接已验证

- 真实 A 根任务仍由后台 continuation 执行时，task projection 已先变为 `completed`；旧 lifecycle gate
  只看 projection，导致同一执行轮新建的两个 child 被取消，原因均为 `parent_link_closed`。主代理后来自己
  写出文件不能替代 child 完成，因此该轮不作为子代理成功证据。
- 当前 gate 对 `completed/done` parent 额外读取与 Gateway 控制同源的 per-thread execution snapshot；
  exact live claim/policy 存在时允许 active child，执行态结束后仍按原规则取消。`interrupted` 不走该例外；
  execution state 不可读时 HOLD，不做破坏性取消。多个 child 共用一次 thread snapshot。
- background continuation 的默认 child output ref 现在先用现有线程局部 task workspace；该值为空时读取
  `task_attributes.run_workspace.task_root`。每个 child 得到 task-local、按 index/slug 分离的默认输出，
  不再因后台线程局部变量缺失而没有声明产物位置。
- 聚焦回归覆盖 completed+live allow、completed+expired cancel、interrupted cancel、unreadable hold、双 child
  单次快照，以及只存在结构化 run workspace 的默认 output refs；完整本地门禁与 wheel 制品门均通过。
- 1.10 真实反证使用两个隔离 Feishu owner。A 保持 `thread-74479991be1c4144` 与 persistent root
  `req_1784447361003_69208_1`，B 保持 `thread-2cadb8bf610a41ff` 与 persistent root
  `req_1784447360982_69208_0`；两边各只创建两个新 child，四个 child 都以精确 parent/root 进入 `DONE`。
  旧版留下的 cancelled child 仅作历史缺陷证据，没有混入本轮成功计数。
- A 的多次 typed `/btw` 按序进入同一 active task；任务已结束后的 `/btw` 返回没有运行中任务并拒绝保存，
  后续普通纠错仍由 task selection 选回原任务/项目。A/B 独立副本最终分别 59/59、51/51；用户出口新消息
  无 child 工具碎片或内部协议。该轮没有从模型摘要反推状态，child 数量和终态均来自 canonical ledger。
## 2026-08-21 会话运行时 式直属控制与 cwd 收口

- 模型侧不再存在查树、等待、巡场或手动推进工具；`create_subagents` 自动启动，运行状态由宿主事件送到直属父级。`cancel_subagents` 经结构化父子关系授权后即可打断，自动重试资格不能否决父级的显式中止。
- 新 child 不再附加全量 `sibling_roster`，旧任务恢复渲染时也过滤这类历史上下文。每一级只持有自己的目标、父级指令、显式引用和直属孩子状态，避免代理树扩大后上下文按平方增长。
- child 的 `write_boundary.execution_cwd` 固定继承父级项目 cwd；隐藏 task/run 状态目录与显式 work/output 目录继续各守职责，Tool Registry 只从可信 `execution_cwd` 选择相对路径基准，读写围栏仍独立裁决权限。

## 2026-08-22 Gateway 调度锁失效恢复

- 内部 dispatcher/watch 的互斥权威已从“锁文件 JSON 里的旧 pid”改为操作系统持有的文件描述符锁；空文件、
  半写 JSON 或进程崩溃留下的元数据不再永久挡住 Gateway 孤儿调和。
- `--force-lock` 只保留命令兼容和诊断语义，不能抢占仍被活进程持有的内核锁。Gateway 继续是唯一自动恢复
  执行者，TUI 和 `/status` 都不会因为看见旧任务而另启一套调度。

## 2026-08-22 子代理继承客户端 cwd 与 runner 异常收口

- `create_subagents` 的相对 `output_files/output_refs` 现在从当前 thread 的 Gateway-validated cwd 解析，
  不再从单 Gateway 守护进程启动目录解析；任务交付提示中的 source/relative root 使用同一字段。
- 并发 runner future 抛异常时，`RecordRunnerResultParams` 改为运行时导入，原异常会落成
  `BLOCKED/UNVERIFIED/runner_worker_error`。旧代码只在 TYPE_CHECKING 导入，异常分支本身会再触发
  `NameError`，从而留下看似永久 RUNNING 的失联记录。
- focused 回归覆盖客户端 cwd 的 child 相对输出和 future exception 结构化落账；正式长任务复验待部署。
