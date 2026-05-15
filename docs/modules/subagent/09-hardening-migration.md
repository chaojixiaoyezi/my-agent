# Subagent Hardening Migration

中文说明：这份文档记录 2026-05-15 开始的子代理硬化迁移。目标不是继续加一堆流程限制，而是把子代理做成“不同记忆、不同权限、但能力接近主代理的执行者”。默认本地模式下，子代理、协调者、测试者、验收者都应该能读、能写、能汇报、能在任务目录内工作；角色只追加职责，不应该把代理变成没手没脚的空模板。

## Reference Lessons

- Codex: 子代理创建和执行尽量走结构化参数、角色应用、agent path 和事件流。不要让下级从自然语言摘要里猜工具名、路径和父子关系。
- OpenClaw: 子代理有 session/run registry、日志、send/kill/info 等控制面；父子权限和可见性靠运行登记和命令边界，不靠大段提示词碰运气。
- Hermes: delegate 子任务是干净的新对话，有自己的 task id、terminal/cache 和 sandbox；父级只拿 summary/ref，大输出外置，避免父级上下文越来越重。

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
11. 将 QA/tester/acceptor 的创建策略改成“work 完成后按依赖触发”为主。
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
- 旧报告里 `summary` 可能是字符串；新桥接会把它包成 `{"text": "..."}`，保持兼容。
- 这一步的目的不是增加流程，而是减少“模型把摘要当工具/把路径说错/把 run id 读漏”的机会。结构化字段是事实来源，自然语言只负责让人看懂。

## Steps 10-12 Planner And QA Verification

- 中文说明：第 10-12 步的重点是“父级别变重、QA 别空转”。parent planner 使用 control-plane 提示和空工具列表，只消费状态快照；父级委托下级后，在 acceptor 完成前只读 refs、报告和运行元数据。
- QA/tester/acceptor 不再一开始固定创建。系统先返回 `quality_advice`，等 worker/writer/leaf 有可验收产物后，再让 LLM 决定 QA 范围、数量和顺序。
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
- 已加固：`dispatch_subagents` 验收记录以父级真实 tests/follow-up 为准。测试失败、测试为空但需要 rescue、或 follow-up 指向 `plan_rescue` 时，dispatch record、aggregate report 和单 run `acceptance_review.json` 都写 `REJECT`，不再混入“验收通过”。
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
- 已调整：真实执行 dispatch 时，`limit` 可作为 `max_runners` 的模型友好别名，减少父级因为字段名不熟而意外串行。
- 迁移原则：继续把专业字段变成容错的机器接口，不让小白用户或父级 LLM 被内部字段名绊倒。

## Group 3e-3g Board And Target Semantics Pre-Fix

- 中文说明：看板、收口和文件合同必须共用同一种机器事实。父级不能从自然语言摘要里猜“哪个 HTML 修好了”，也不能因为旧失败 run 还在 task.json 里就永远不收口。
- 已调整：`无/没有/不存在 xxx 引用` 进入缺席/禁止语义，不再污染 `required_files`。
- 已调整：看板 payload 顶层给 `completion_status`，包括 `must_not_report_done`、`blocking_run_ids` 和建议继续 dispatch 的结构化调用。
- 已调整：`target_tokens` 成为 board row 的 refs-only 字段；父级可以知道 run 关联的具体产物名，但不读取产物正文。
- 已调整：`task_actual_target_tokens()` 统一目标识别，优先结构化 `output.json` 和 `[SUBAGENT_RESULT]`，再回退自然语言 goal；英文引用词 `use/include/import/load/link to` 需要词边界，避免 `/Users/...` 被误切。
- 已调整：最终 closeout 和 board completion 都接受“旧失败/待验收 run 的目标文件已被后续 DONE/VERIFIED sibling 覆盖”这一事实，避免重复修和假阻塞。
- 迁移原则：把事实从 prompt 里抽出来，放到 typed refs 和目标 token；prompt 可以自然，状态机必须稳定。

## 2026-05-15 Code-Size Zero Refactor

- 中文说明：本轮不新增流程限制，主要把已经跑通的子代理硬化代码拆成更稳的长期结构，目标是“子代理像换了记忆/任务空间的主代理一样能干活”，而不是继续靠大文件和细碎参数走钢丝。
- 已拆分：`result_structured_evidence.py` 承接 evidence / evidence_packets / findings 解析；`filesystem_read_file.py` 承接 `read_file` 执行；`registry_payload_normalize.py` 承接工具 JSON 容错归一；`runner_timeout_policy.py`、`subagent_finalize_artifact_integrity.py`、`subagent_dispatch_closeout_*`、`context_bundle_*`、`static_site_*` 小模块承接各自边界。
- 已清零：strict code-size 报告达到 `hard=0 high-risk=0 soft=0`。后续新增功能不允许靠调高阈值通过；接近 high-risk 时要优先拆模块、用 bundle，或把纯数据表移出控制流文件。
- 已复验：focused 子代理/工具/配置测试 `157 passed`；自然语言层级基线和恢复相关 focused tests `29 passed`。
- 开发要求：后续继续少写死流程。工具、路径、执行、自毁红线由系统守；角色选择、QA 范围、修复顺序、是否继续派工尽量交给 LLM + 模板 + workflow + 验收事实决定。
