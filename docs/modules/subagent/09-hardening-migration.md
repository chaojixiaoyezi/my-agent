# Subagent Hardening Migration

中文说明：这份文档记录 2026-05-15 开始的子代理硬化迁移。目标不是继续加一堆流程限制，而是把子代理做成“不同记忆、不同权限、但能力接近主代理的执行者”。默认本地模式下，子代理、协调者、测试者、验收者都应该能读、能写、能汇报、能在任务目录内工作；角色只追加职责，不应该把代理变成没手没脚的空模板。

## Reference Lessons

- 会话运行时: 子代理创建和执行尽量走结构化参数、角色应用、agent path 和事件流。不要让下级从自然语言摘要里猜工具名、路径和父子关系。
- 通道运行时: 子代理有 session/run registry、日志、send/kill/info 等控制面；父子权限和可见性靠运行登记和命令边界，不靠大段提示词碰运气。
- 长期助手: delegate 子任务是干净的新对话，有自己的 task id、terminal/cache 和 sandbox；父级只拿 summary/ref，大输出外置，避免父级上下文越来越重。

## Current Migration Principles

- 子代理默认像主代理：除记忆和权限边界不同外，基础读写、报告、任务目录工作能力保持可用。
- 用户可见子代理配置少于 10 个；调度细节、上下文长度、工作流微参数由系统内部策略处理。
- root 不走 `capability_request`，因为 root 没有上级；下级仍可向父级申请工具、skill、MCP、网络或 shell 能力。
- 路径、工具名、文件合同走结构化 `task_packet` / `context_bundle` / typed envelope；提示词只解释职责，不作为唯一事实来源。
- 工作完成后再派 QA/验收；是否提前派测试、找茬、验收由 LLM 根据任务依赖决定，不能写死成所有任务一开始就创建一堆空转角色。
- 安全底线保留：不能越权写别人的目录，不能删除系统目录，不能自毁 my-agent 关键文件，不能无限创建/恢复。

## 24 Steps

1. 精简默认配置，只保留少量用户能理解的子代理入口。
2. 保留旧配置字段兼容，但不再作为新功能扩展入口。
3. 所有内置和用户模板都叠加基础读写/报告工具，避免空模板。
4. root runner 上下文隐藏 `capability_request`，下级仍保留申请通道。
5. 给 context bundle 增加 `subagent_task_packet.v1`，固定 role、goal、文件合同、写入合同和工具合同。
6. 让 runner prompt 明确优先读 `context_bundle.task_packet`，不要从摘要里重新猜路径。
7. 标准 JSON 工具调用也做工具名/参数名别名归一，例如 `write/file_path` -> `write_file/path`。
8. 冲突参数显式报错，不静默猜测，例如 `path` 和 `file_path` 不一致时拒绝。
9. 把 create/schedule/dispatch 的工具输出继续 typed envelope 化，父级只读 run ids 和 refs。
10. 将 parent planner、coordinator planner 和 runner 继续隔离，planner 不读产品正文。
11. 将 QA/tester/checker 的创建策略改成“work 完成后按依赖触发”为主。
12. 允许 LLM 决定 QA 覆盖范围：一个 QA 可以检查多个 worker，不强制一一对应。
13. 强化 direct child 进度摘要，父级先看状态和 refs，不直接读正文。
14. 优化 takeover packet，确保原 run 挂死后新 run 接管同一任务目录和 artifacts。
15. 优化 leadership recovery，coordinator 挂掉后新 leader 能接住原孩子。
16. 批量失败时做 bounded batch recovery，不把 3 个失败扩成 30 个新代理。
17. packet 损坏/缺失/过期时降级读 checkpoint、summary 和 context bundle。
18. no-progress fuse 只阻止死循环，不阻止正常重试和人工/父级纠偏。
19. 子代理 compact 只读 task-local refs，不读取主代理 SOUL/USER/memory。
20. 多次 compact 后继续从 progress/continue packet 接任务，避免重复写同一部分。
21. 真实 E2E 使用小白自然语言任务，不在普通任务提示词里塞 dispatch/run_id 等术语。
22. 真实 E2E 必须由主代理派第一层，后续由下级继续派下级；测试者只观察日志和产物。
23. 把发现的问题持续追加到 `06-real-e2e-findings.md`，记录问题、修法、是否解决。
24. 每轮提交前跑 focused tests、ruff、strict code-size；推远端前按“无 CI 时本地最严格测试”执行。

## 15 Test Groups

1. 单 worker 自然语言任务：能读写、能汇报、能验收。
2. 3 worker 并行：不抢同一文件，不误用同一 runner_instruction。
3. 5 worker 并行：父级只看 refs 和状态，不读产品正文。
4. 1 主、2 子、4 孙：层级创建由上级逐层完成。
5. 1 主、4 子、16 孙、48 孙孙：压力测试层级、命名、refs 和日志。
6. 单个 worker 超时：优先 packet-first 续跑或 takeover。
7. coordinator 超时：新 leader 接管下级。
8. 多个 worker 同时失败：批量恢复不无限扩容。
9. `latest_continue_packet.json` 损坏：降级 checkpoint/summary。
10. 连续失败：触发 no-progress fuse，不死循环。
11. 购物网站 E2E：worker 完成后 QA、修复、验收闭环。
12. 高端家具单文件 HTML：普通小白提示词不含专业术语。
13. 多次主代理 compact：能自动接住原任务状态。
14. 多次子代理 compact：只读 task-local refs，不污染主 memory。
15. 工具调用漂移：`write/file_path`、XML-ish、缺结束标记、长 content 分块都能恢复或给出可执行纠偏。

## First Slice Implemented

- 默认配置已经只暴露 8 个子代理相关用户项。
- 旧子代理微参数进入 `HIDDEN_COMPAT_CONFIG_FIELDS` 兼容层：老配置仍能被代码识别，但默认样例和新开发规范不再鼓励用户调这些细碎开关。
- 隐藏兼容限制项默认改为不限制：层级深度、单次 child 数和 takeover 链深度都采用 `0=unrestricted`，只有显式正数才作为测试或严格工作流的限制。
- 角色模板/契约改成职责叠加，所有角色都有基础读写、汇报和能力申请工具；root 执行上下文会隐藏能力申请工具。
- `context_bundle` 增加 `task_packet`，runner prompt 明确优先按结构化任务包执行。
- 标准 JSON 工具调用增加别名归一，减少模型把工具名或路径字段写错造成的硬失败。

## Step 9 Dispatch Envelope

- 中文说明：`create_subagents` 和 `schedule_child_subagents` 已经有 `typed_envelope`；本轮把 `dispatch_subagents` 也补成 `subagent_dispatch` typed envelope。
- envelope 只放稳定控制字段：`dry_run`、`summary`、`actionable_run_ids`、`recovery_run_ids`、`dispatch_json`、`dispatch_md`、`record_count` 和 scope。父级要继续推进或恢复时读这些字段，不从自然语言 `message` 里猜 run id。
- `dry_run` 只表示本次工具调用是否真实推进 runner。`summary` 里的逐记录计数使用 `record_dry_run_count` / `record_applied_count`，`records[]` 里的逐条状态使用 `record_dry_run` / `record_applied`，避免模型把“有几条记录是预览”误读成“这次整体没有执行”。
- `inspect_agent_tree` 会返回 `running_seconds`、`seconds_since_progress` 和 `aggregation_readiness`。这些都是父级观察字段，只帮助判断谁还在跑、谁太久没进展、汇总前还缺哪些 run，不新增子代理专用验收门。
- 旧报告里 `summary` 可能是字符串；新桥接会把它包成 `{"text": "..."}`，保持兼容。
- 这一步的目的不是增加流程，而是减少“模型把摘要当工具/把路径说错/把 run id 读漏”的机会。结构化字段是事实来源，自然语言只负责让人看懂。

## Steps 10-12 Planner And QA Verification

- 中文说明：第 10-12 步的重点是“父级别变重、QA 别空转”。parent planner 使用 control-plane 提示和空工具列表，只消费状态快照；父级委托下级后，在 checker 完成前只读 refs、报告和运行元数据。
- QA/tester/checker 不再一开始固定创建。系统先返回 `quality_advice`，等 worker/writer/leaf 有可检查产物后，再让 LLM 决定 QA 范围、数量和顺序。
- 一个 QA 可以检查多个 worker，也可以只检查高风险 refs；系统只挡明显错误，例如没有产物时创建空 QA、QA 失败却直接收口、repair 覆盖无关文件。
- 复验命令覆盖 planner 隔离、body-read guard、QA 后置、dispatch child refs 和 planner/watch；当前 focused tests 通过。

## Steps 13-18 Recovery Verification

- 中文说明：第 13-18 步验证“出故障时不要越修越乱”。父级 dispatch 返回 direct child 状态和 refs；继续跑优先看 `latest_continue_packet.json`，坏包/旧包/缺包降级 checkpoint 和 summary。
- worker 挂死走 takeover run，保留原任务目录、artifacts 和恢复 refs；coordinator/leader 挂死且带孩子时走 leadership recovery，把孩子转交给新 leader，不创建空接管节点。
- 多个 child 同时失败时，payload 给 batch recovery 摘要和 action counts；自动 gate 会拦太大的 refs-only 批次，防止无限扩容。
- 连续恢复没有进展时触发 no-progress fuse，写 blocker/worklog 并停止本轮自动恢复，不把失败说成完成。

## Steps 19-24 Natural E2E Verification

- 中文说明：第 19-24 步改成真实小白提示词验证，不在普通任务里塞 `dispatch/run_id` 这类术语。外部测试者只给主代理一句自然任务，后续必须由主代理创建和调度小傻妞。
- 已加固：明确文件交付 worker 即使命中全局 workflow auto，create/dispatch 也会关闭通用 producer/critic/repair 扩展；质量波次等 worker 完成后再由 LLM 按 refs 决定。
- 已加固：`dispatch_subagents` 收口交给上级真实 tests/follow-up 为准。测试失败、测试为空但需要 rescue、或 follow-up 指向 `plan_rescue` 时，dispatch record、aggregate report 和单 run `runner_result.json` 都写 `REJECT`，不再混入“验收通过”。
- 已加固：refs-only 委托期不只挡 `read_file/read_artifact`，也挡 `run_command` 里的 `cat/tail/head/sed/rg` 等产物正文读取；控制面元数据仍可读，避免恢复和调度卡死。
- 真实复验：`subagent_hardening_e2e_20260515_step24d` 通过，root 自然语言派工，1 个小傻妞 worker 写出家具网站首页，最终 `done_verified=1`。

## Group 3 Parallel Natural E2E Pre-Fixes

- 中文说明：三文件家具站测试暴露的是调度和工具层问题，不是用户提示词问题。主代理能自然派 3 个小傻妞，但 `auto` 并发退成了 1；长 HTML 内容已经作为合法工具参数到达，工具却硬拒，迫使模型反复重试。
- 调度修正：`runner_concurrency: "auto"` 改为内部有界并发，按任务数并发但最多 8 个，避免普通 fan-out 被静默串行化。
- 写入修正：`tool_write_inline_max_chars` 只作为推荐运输尺寸。内容已经被工具解析出来时，先写入，再提示后续大内容建议分块或 artifact 化。
- 迁移原则：这些都不新增用户可见配置；默认行为应该更接近主代理正常干活，而不是要求用户调一堆细碎参数。

## Group 3b Acceptance Repair Pre-Fix

- 中文说明：验收器不能把模型带偏。安全可选 DOM 钩子不应触发硬修复；坏掉的完整 HTML 骨架必须优先暴露出来。
- 已调整：自动推断的 `static_site_check` 会设置 `require_complete_html=true`，并报告 `html_structure_hits`。
- 已调整：`getElementById` 的可选保护用法不会再被误报为缺失 DOM id。
- 已调整：dispatch payload 带 `repair_hints`，让父级派修复时有“先修骨架/再修控件”的明确方向。

## Group 3c Negative Acceptance Pre-Fix

- 中文说明：自然语言验收里的否定词必须被结构化理解。`无 index4.html 引用` 代表 `not_contains(index4.html)`，不能被执行成 positive contains。
- 已调整：`content_check` 支持 `match_mode=not_contains`、`expect_absent`、`negate` 和 `should_not_contain`。
- 已调整：当测试名里有“无/不包含/不得出现/must not contain”等否定语境，并且包含待匹配 pattern 时，执行器会自动采用 absent 语义。

## Group 3d Closeout Semantics Pre-Fix

- 中文说明：子代理恢复链路不能被旧状态拖死。旧 worker 被 takeover 后，如果接管者已经 DONE/VERIFIED，旧 worker 应该显示为被覆盖，而不是继续当 blocker。
- 已调整：dispatch final/limit closeout 使用 `DONE/VERIFIED` 或 `TAKEN_OVER -> verified replacement` 作为 resolved 判定。
- 已调整：模型写出的“无空 href”这类负向证据，如果 `ok=false` 表示坏模式没有命中，会在结构化结果入口规范成“需求通过”。
- 已收敛：真实执行 dispatch 时，`max_runners` 才控制 runner 数量，`limit` 只控制报告/记录条数，避免一个数字两个含义。
- 迁移原则：继续把专业字段变成容错的机器接口，不让小白用户或父级 LLM 被内部字段名绊倒。

## Group 3e-3g Board And Target Semantics Pre-Fix

- 中文说明：看板、收口和文件合同必须共用同一种机器事实。父级不能从自然语言摘要里猜“哪个 HTML 修好了”，也不能因为旧失败 run 还在 task.json 里就永远不收口。
- 已调整：`无/没有/不存在 xxx 引用` 进入缺席/禁止语义，不再污染 `required_files`。
- 已调整：看板 payload 顶层给 `completion_status`，包括 `must_not_report_done`、`blocking_run_ids` 和建议继续 dispatch 的结构化调用。
- 已调整：`target_tokens` 成为 board row 的 refs-only 字段；父级可以知道 run 关联的具体产物名，但不读取产物正文。
- 已调整：`task_actual_target_tokens()` 统一目标识别，优先结构化 `output.json` 和 `[SUBAGENT_RESULT]`，再回退自然语言 goal；英文引用词 `use/include/import/load/link to` 需要词边界，避免 `/Users/...` 被误切。
- 已调整：最终 closeout 和 board completion 都接受“旧失败/待收口 run 的目标文件已被后续 DONE/VERIFIED sibling 覆盖”这一事实，避免重复修和假阻塞。
- 迁移原则：把事实从 prompt 里抽出来，放到 typed refs 和目标 token；prompt 可以自然，状态机必须稳定。

## 2026-05-15 Code-Size Zero Refactor

- 中文说明：本轮不新增流程限制，主要把已经跑通的子代理硬化代码拆成更稳的长期结构，目标是“子代理像换了记忆/任务空间的主代理一样能干活”，而不是继续靠大文件和细碎参数走钢丝。
- 已拆分：`result_structured_evidence.py` 承接 evidence / evidence_packets / findings 解析；`filesystem_read_file.py` 承接 `read_file` 执行；`registry_payload_normalize.py` 承接工具 JSON 容错归一；`agent_core/runner/timeout_policy.py`、`context_bundle_*`、`static_site_*` 小模块承接各自边界。早期的 `subagent_finalize_artifact_integrity.py`、`subagent_dispatch_closeout_*` 已在后续协作简化中删除，避免 runner 收尾和 dispatch 本地收口变成额外卡点。
- 已清零：strict code-size 报告达到 `hard=0 high-risk=0 soft=0`。后续新增功能不允许靠调高阈值通过；接近 high-risk 时要优先拆模块、用 bundle，或把纯数据表移出控制流文件。
- 已复验：focused 子代理/工具/配置测试 `157 passed`；自然语言层级基线和恢复相关 focused tests `29 passed`。
- 开发要求：后续继续少写死流程。工具、路径、执行、自毁红线由系统守；角色选择、QA 范围、修复顺序、是否继续派工尽量交给 LLM + 模板 + workflow + 验收事实决定。

## Stage 2 Kernel Boundary Slice

- 中文说明：继续最初 1-7 阶段里的第 2 阶段，新增 `SubagentKernel` 只读内核视图。它不是新的调度器，也不是新的事实源；旧 `task.json`、agent run workspace 和控制面投影仍是事实来源。
- `SubagentKernelQuery` / `SubagentKernelSnapshot` 固定 root tree、own subtree、状态桶、workspace refs、recovery refs、artifact/evidence refs 和 takeover candidates 的读取形状。父级检查后续优先读这个快照，不再各模块自己拼状态。
- `SubAgentManager.kernel_snapshot()` 是当前公开入口；它只读 `manager.list_runs()` 和 run 记录，不调度、不恢复、不执行测试、不读取 artifact 正文。
- 迁移原则：这是“把发动机仪表盘统一起来”，不是继续加 guard。后续第 3 阶段协议层、第 4 阶段工具网关、第 5 阶段恢复接管都要尽量消费 kernel snapshot 或它的后续扩展。

## Stage 3 Board Kernel Envelope Slice

- 中文说明：继续第 3 阶段协议结构化，`inspect_agent_tree` 输出现在会在可确定单棵 root tree 时附带 `kernel_snapshot`。
- `kernel_snapshot` 只包含状态桶、run rows、workspace refs、recovery refs、artifact/evidence refs 和 blockers，不读取业务产物正文。父级模型要判断“谁还在跑、谁失败、谁可接管、恢复入口在哪里”时，可以先读机器字段，不再从看板自然语言摘要里猜。
- 如果看板混入多棵 root tree，或 manager 没有 kernel 入口，则不附加该字段，避免把无关任务树混到当前决策里。
- 2026-06-03 补强：`inspect_agent_tree` 的节点、liveness 和 progress layer 会带 `not_done_reason`、`running_seconds`、`seconds_since_progress`，让父级能看到“为什么还没完成”，而不是只看到 RUNNING/PLANNING。相同范围的短时间重复查看会返回 `cooldown_active` 缓存快照和 `inspect_agent_tree_recent_duplicate` warning，避免父代理高频轮询继续放大状态扫描；真正要接管、验收或出现新事实时仍可继续读取。
- 2026-06-03 污染修复：kernel `source_refs` 选择最新可见 workspace，不再因为同 slug 旧任务排在前面而把 `root_task/root_work/root_output` 指到旧目录。子代理内部 `final_report.md` 仍属于 `work/agents/<run_id>/`，不能被父级或 closeout 当作用户最终交付物，除非它被注册为本轮产物并通过 provenance。
- 验收：`test_orchestration_board_payload.py` 和 `test_subagent_kernel.py` focused tests 通过；strict code-size 仍为 `hard=0 high-risk=0 soft=0`。

## Stage 4 Tool Contract Readiness Slice

- 中文说明：继续第 4 阶段工具网关统一化的前置工作。kernel run row 现在带 `tool_contract`，把 allowed tools、used tools、open capability request count、grant count、gap count 和 controlled exec grant ids 变成机器字段。
- 这一步不授予新权限，也不引入新的限制；它只是让父级和接管者知道“这个 run 手里有什么工具、用过什么工具、还缺什么工具”。后续受控 exec、大输出分片和 tool/skill 申请可以基于这些字段继续做。
- `inspect_agent_tree.kernel_snapshot.rows[].tool_contract` 会把这组字段带给父级模型，减少从 prompt 或 summary 里猜工具状态。

## Stages 1-6 Protocol Contract Slice

- 中文说明：这一步把“少限制、强协议”的 1-6 步收成第一版可执行合同。目标不是把流程写死，而是让父级检查都读同一份机器字段。
- `TaskAddress` 是子代理地址：包含 run、root、parent、depth、lineage、attempt 和 workspace ref。它解决“谁是谁的孩子、接管哪个目录、恢复哪个 run”这些不该靠自然语言猜的问题。
- `TaskEnvelope` 是子代理任务包：包含 goal、role、plan、tool contract、write contract、acceptance、context refs 和 audit。它会出现在 kernel snapshot、recovery strategy 和 closeout decision 里。
- `write_contract` 区分 `internal_task_root` 和 `product_write_roots`。子代理始终可以写自己的任务日志/报告；但要写用户产物目录，必须有明确 product root，避免“能写自己屋子”误判成“能交付项目文件”。
- `run_tool_preflight()` 是工具预检：缺工具、缺产物写入根、缺 controlled exec 授权时返回结构化 `ToolContractError`。它不把基础读写工具关掉，也不新增用户可见微参数。
- 恢复链路仍然 packet-first：如果 `latest_continue_packet.json` 已准备好，推荐从 packet 接；否则再降级 checkpoint/summary。区别是现在推荐动作同时带 address 和 envelope，后续接管者不需要重新读自然语言摘要猜任务。
- 收口交给父级检查条件，而不是从输出摘要里反推。
- 后续迁移要求：dispatcher 和 runner 下一步要优先消费 `TaskEnvelope`，tool gateway 要优先消费 preflight issue；真实 E2E 要继续使用普通用户自然语言，不在 prompt 里塞内部字段名。

## Stages 1-6 Runner Consumption Slice

- 中文说明：协议现在进入 runner 开工前路径。`context_bundle` 会同时写旧 `task_packet` 和新 `task_envelope`，模型仍能看中文说明，但真正的地址、工具、写入和验收事实优先读 envelope。
- `tool_preflight` 随 `context_bundle` 一起写入，runner prompt 只展示短 issue codes。它用于早发现缺口，例如“只有内部 task_dir，没有产物写入根”或“allowed_tools 里有 controlled_exec 但没有 grant”。
- preflight 不做硬阻断：它不删除工具、不改任务状态、不自动发 grant。父级/runner 可以根据 issue codes 决定是继续写报告、上抛 capability request，还是先补产物目录。
- recovery payload 现在用完整 `all_tasks` 构建 `TaskAddress.lineage`。当 child 失败时，父 runner 看到的是 parent -> child 链路，而不是一个孤立 run id。
- 测试边界：本片先用 focused tests 验证协议进入 context bundle、prompt 和 recovery payload；真实 MiniMax E2E 是下一片，继续用普通话任务，不使用 `dispatch/run_id/TaskEnvelope` 等专业词。

## Real Runner E2E Hardening Slice

- 中文说明：真实 MiniMax E2E 证明协议进入 prompt 后还需要控制两类成本：启动上下文不能太胖，最终收口不能被模型参数关掉。
- runner prompt 现在只带 slim execution context summary。完整 context bundle、TaskEnvelope、tool preflight、output refs 仍落盘，prompt 只放短摘要和 refs，避免真实模型因 30K+ 开场提示词超时。
- `required_file_contract()` 会从文件级 product write root 推导 required files。这样 `/.../deliverables/furniture-home/index.html` 既是写权限事实，也是验收合同事实。
- 质量角色判断收窄：普通“主代理验收 / 汇报验收结果”不再强制 checker；只有显式 `checker`、`验收子代理`、`验收代理`、`派验收` 才要求独立验收角色。
- 模型面对的 `dispatch_subagents` 在 `dry_run=false` 时固定执行父级检查。
- 迁移原则：不是加新 guard，而是把“启动轻、事实硬、验收必须机器可证”收进协议边界。下一步做允许 repair 的真实 E2E，让 root 基于失败报告重新派修复 worker。

## Repair Loop / Tool Gateway Hardening Slice

- 中文说明：这片继续把真实 E2E 中暴露的“系统边界不硬”问题归到协议、记忆和工具网关，而不是继续往 prompt 里补口号。
- Closeout repair：`orchestration_final_closeout_repair.py` 把 `runner_result.json=REJECT`、`test_execution.json` 和 parent follow-up refs 转成机器字段 `final_closeout_repair_advice`。runner-context dispatch 的下一步会明确说“按最终收口 refs 派修复 child”，不是让 root 读正文猜。
- Top-level repair handoff：顶层 `dispatch_subagents` 的 acceptance reject record 也会附带 `final_closeout_repair_advice` 和 `create_subagents` 建议工具调用；失败 refs 包含 test/follow-up/output/run，小傻妞修复任务会继承原 child 的 product write roots。
- Stale runner stop：`agent_core/subagent/attempt_guard.py` 现在同时服务工具前拦截和模型前停止。runner attempt 被 timeout/abandon 后，旧线程下一轮不会再调用模型。
- Memory isolation：LocalStore memory hit 带 `memory_path`，搜索只接受当前 `JsonlMemory.path` 的命中，避免干净 E2E 或未来多用户 workspace 被旧任务记忆污染。
- Tool gateway：registry 在单次文件工具调用内把父级授权的 product roots 并入 `workspace_roots`。读、列、搜、写都能访问用户指定产物目录；调用结束后恢复，避免授权根外泄到别的工具调用。
- Artifact integrity：runner 结构化输出里的相对产物 ref 可能已经包含 product root 尾部，例如 `deliverables/furniture-home/index.html`。完整性检查会先做 root suffix 对齐，再检查真实文件，避免误拼路径后把已写成功的产物标成 `artifact_missing`。
- Task-local progress：写 HTML 后的 `latest_tool_progress.json` 不再永远说“继续写”。它会带 `artifact_integrity` 小字段；未闭合就继续分块，已闭合就提示写 `output.json` / `SUBAGENT_RESULT` 收口，发现 `href="#"` 这类假链接就先修复再验收。这样把 runner 收口方向放进机器字段，而不是靠 prompt 猜。
- Parser schema tolerance：runner 可以把产物 refs 写成 `deliverables` / `output_files` / `files`，也可能把产物路径放进 `evidence.kind=artifact.path` 或 `evidence_packets.artifact_refs`。解析层会把这些带 path/id 的条目统一转成 canonical `artifacts`。后续所有验收、typed envelope 和恢复逻辑继续只读 `artifacts`，不把同义词扩散到业务层。
- Artifact repair lane 已废弃：过去 `artifact_integrity_failed` 有独立信号层和 repair 建议层，会让父级优先派专门修复子代理。现在这类产物问题回到统一 closeout / 普通 runner 状态里处理，不再单独生成 `artifact_integrity_repair_advice`。
- Real E2E baseline：`real-e2e-20260515-180300-repair-loop4` 通过自然语言 root -> worker -> closeout；产物在 `/Users/example/my-终端应用/.../deliverables/furniture-home/index.html`，状态 `DONE/VERIFIED`。
- 迁移原则：子代理是有任务边界的小主代理。父级给了产物目录，就必须能读写；runner 超时，就必须停止；记忆隔离，就不能串旧索引。
