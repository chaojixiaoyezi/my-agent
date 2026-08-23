# COMPLETED

本文件不再保存历史流水。当前完成项以 git 历史和模块 `02-progress.md` 为准。

最近收口重点：

- 2026-08-22 Prompt 4 r6 的真实 TUI 样本推动四个通用底层候选：`context_scope=isolated` 的用户回执轮
  继续进入模型成本/调用账本，但不再公开私有 thinking 或覆盖主任务 context；系统生成的一项 `items`
  返工也沿 exact parent 的历史 sibling 序号，显式自定义名保持不变；Todo 标题显示 typed
  `完成 X/Y · 进行中 Z`，默认四行仅是可展开视窗。实现对照 会话运行时 active-turn reasoning 与 终端交互
  `TaskListV2`，200 项直接相关 focused 回归已通过；推送、部署与 r7 真机结论仍以 ROADMAP/STATUS 为准。
- 2026-08-22 本地候选已把 Prompt 4 r5 的“明知未完成仍 final”收口为 会话运行时 式模型执行纪律，而不是恢复
  吃过多次亏的宿主质量验收：发布 YAML 与 dataclass 共用同一个通用默认 prompt，删除 Go 专项骨架；
  主代理、子代理和 lifecycle wake 共用“已知缺口且仍可推进就继续”的软提示。`task_progress` open 项只
  返回非阻断 guidance，`covers` exact id 现在同时绑定普通 Todo 与 coverage，并在 canonical child DONE
  后打钩；单个、批量和递归 child 共用连续显示编号。直接相关 focused 回归已通过，真机状态仍以
  `STATUS.md` 和 `docs/ROADMAP.md` 为准。
- 2026-08-22 `680e209` 已完成并部署 main/child/grandchild 统一 Conversation Compact 账本：每个 delegated run 在创建
  或旧任务首次恢复时物化独立 `agent_thread_id`，每个 attempt 幂等落 user/assistant；轮前 transcript 与
  运行中 native IR 都复用同一 owner/thread checkpoint-before-CAS 主链。live-tool 来源保存精确 ToolCall ID，
  不推进 transcript cursor，只累计 `compact_source_tool_pairs`；provider overflow 也不再账外丢 20%。
  摘要/checkpoint/CAS 失败恢复原 IR 并进入同一失败熔断。task-local 权限与 Memory 隔离保留，旧
  apply/continue 不再由正式 child runner 生成；TUI/Web/SQLite 只读 child thread generation。大型项目真机已
  证明 generation/checkpoint/窗口下降与继续运行生效；同时发现并在本地修正摘要请求顺序：真实任务 user
  在前、native history 居中、synthetic Compact user 指令最后，对齐 会话运行时，避免 MiniMax 普通续写末尾动作。
- 2026-08-22 本地底座已把 Compact 的“配置压缩点”与“单次回合是否允许持久
  apply”分开：presentation/no-save 回合不再把 Context 行的 90% 错写成 100%；真实
  preflight 仍保持 save=False 不压缩落盘，并由完整模型窗口守最后硬限。回归同时锁定
  `115.2k/128k` 的展示及 `120k` no-save 不误触发。
- 2026-08-22 本地底座已修复后台主代理读取第二本空进度账的问题：task-path 指纹算法集中到
  `runtime/task_identity.py`，工具写入、Task Runtime State、child seed 终态同步、Goal 续跑和 TUI 投影
  使用同一 ledger id。负向回归证明 request-id 下的旧账不会覆盖真账。普通 Todo 仍只作模型可见工作笔记，
  没有恢复机器验收或普通任务自动续跑；部署和正式 Prompt 3 真机结论仍以 STATUS/ROADMAP 为准。
- 2026-08-22 本地 TUI 已把 Todo 默认摘要收为固定四条状态窗口：最近完成、当前运行和下一待办优先，
  运行项复用 Working 动画，`Ctrl+T` 只展开/收起完整 canonical 清单。常驻 Context 显示总量、窗口占比、
  主 ConversationThread 已提交 Compact 次数和明确命名的自动压缩点；协议相关 prompt/messages/tools
  分类留给 `/context`。该轮曾用 exact-run 数字投影补过渡期次数；这条过渡现已被上方独立 child
  ConversationThread generation 主链删除。部署/真机状态仍以 `STATUS.md`、`docs/ROADMAP.md` 为准。
- 2026-08-21 `714c0c8` 已把单一后台计数扩展为 终端交互/模型助手 Code 式的固定子代理
  活动区：Gateway 从 active task link 与 canonical run 账本生成有界直属 child 投影，输入框附近原位
  显示名称、状态、职责短标题、耗时和 attempts。它不进 transcript，不暴露工具输出/路径/权限，也不参与
  完成、重试或验收。代码已推送并部署 `.7` 单 Gateway；两个真实 child 分别在 46 秒、52 秒一次 attempt
  `DONE`，活动区完整显示状态变化并在主代理自动汇总后收起。父级 wake 在旧进程卡 process-local lane、
  优雅重启后立即恢复的尾项仍留在 ROADMAP，未冒充为全部生命周期验收通过。
- 2026-08-21 当前本地切片按 `d928d77` 真机失败样本收口四处底层语义：普通相对交付路径继承当前可信
  cwd，显式 output/work 才进入 task 内部目录；child 共享阅读包不再跨任务缓存；只有根主代理与结构化
  coordinator 持有 create/guidance/cancel/resolve，普通 leaf 无下级管理工具；TUI 从 canonical active
  task count 显示可移除的后台 Working。188 项定向回归和本地严格 gate 已通过；这里不代表已推送、部署
  或真机交付通过，当前状态仍以 ROADMAP/STATUS 为准。
- 2026-08-21 本地实现已补上递归直属事件链：task-local 父代理创建下一层后以
  `interrupted/SUBAGENTS_ACTIVE` 让出，精确 child ids 的耐久标记会阻止孤儿器误复活；同批成功收齐只
  恢复一次，失败或 capability 阻塞立即恢复，孙代理不再越级唤醒根会话。父级新工作片获得有界
  `direct_children` 状态和结果 refs，不需要 inspect/wait/shell sleep。旧 `task_progress` 自动 continuation
  也已删除，软清单不再决定续跑或完成。这里只表示本地实现完成，发布和真机结论仍以 ROADMAP/STATUS
  为准。
- 2026-08-21 本地底座已把 Gateway 后台整合从 owner 级 single-flight 收细为
  `owner + durable thread_id` 车道。同会话仍共用既有持久 run claim 单飞，不同 TUI/会话按 owner
  公平、在全局池和 `background_threads_per_owner` 两层显式上限内并发。真实 scheduler
  回归已证明同 `local/main` 的一条模型回合挂起时，另一条会话仍能入模。
  这解决的是“旧窗口占住用户后台通道，新任务 child 全完成也叫不醒主代理”；
  `.7` 真机交付结论仍以 ROADMAP/STATUS 为准。
- 2026-08-21 本地实现已把主代理与任意层级子代理收敛为同一递归关系：模型只有统一
  `create_subagents` 创建入口，创建后由宿主自动启动；手动 `dispatch_subagents` / child scheduler 工具已从
  模型控制面删除，废弃 dispatch action envelope 也已移除；命令行 dispatch 仅作为宿主后台进程入口，
  普通用户无需调用。主代理、子代理与 Gateway 共用六类 `turn_end`，普通完成不再依赖机器质量验收、
  `acceptance_checks`、`VERIFIED` 或专用结果包装。TUI 同批补齐 Working 动画、条件式页底跟随、持续 thinking
  增量和 Compact 百分比。当前只代表本地实现完成，发布与 `.7` 真机状态以 `docs/ROADMAP.md`、`STATUS.md`
  为准。
- 同日递归控制面继续按 会话运行时 收口：删除 create 后周期性 LLM 巡场和 wait helper；`send_guidance` 只允许
  当前代理给一个直接 child 发送 `target + message`，用户插入主代理仍走 active turn。后台续跑身份改为
  task-bound，TUI 后台 notice 改为立即/每秒独立块，批量 child 的 `output_files` 获得完整嵌套 schema。
  这解决了同 thread 旧任务串权和后台更新丢失；随后普通 child 工作区继承又消除了“父级能写、child
  却必须重复声明权限”的断层。真实 8080 交付仍在 ROADMAP，未提前标记通过。
- 同日首轮 `.7` 原样任务定位并修复 capability 断链：OPEN 请求现在优先于 provider 的普通 completed
  收尾，child 会保持 `BLOCKED`；直属父级 grant/deny 后原 run 自动回到 `PENDING`，由同一生命周期链
  续跑，不再需要模型巡检或推动。普通 child 逐层继承父级结构化工作区上界，`output_files` 回归交付身份
  与冲突锁职责。未注册的 `InspectAgentTreeTool`、Schema、公开导出和专用测试已删除；内部状态投影继续
  服务 `/status`、TUI 与恢复。当前只代表本地实现和定向回归完成，真机结论仍以 ROADMAP 为准。
- 同日 `e321483` 真机失败样本证明已注册工具不等于模型首轮看得到：默认延迟整个
  `orchestration` 造成 0 child 且主代理自写。本地配置已按 会话运行时 multi-agent v2 改为递归代理控制
  首轮直出，并以默认 `model_visible_specs()` 回归锁定；本项只表示本地修正完成，真机复验尚未通过。
- 同日 `c2c0235` 真机让 4 个 child 真正自动启动，并定位出“进程活着但像挂死”的两层根因。本地已按
  会话运行时 最具体路径规则修复宽 forbidden 误伤窄 task output allow，真实 ToolResult 活动改读
  `tool_name`，runner 离散模型/工具阶段会写有界短状态；旧 `raise_event` 模型工具、Schema、注册、实现和
  专用测试已删除，内部 observation/wake 生命周期服务保留。活跃内置 Skill 的过期调用说明也已清理，
  并新增退休工具名扫描回归。这里只表示本地实现与严格 gate 已通过，发布与真机重跑见 ROADMAP。
- TUI 灰色层级已按 终端交互 的容器级 `dimColor` 语义修正：思考 Markdown 的普通文字、粗体、代码和
  链接以及 `Ctrl+O` 折叠提示都由最终浅灰 role 接管，不再只染括号或行前缀。鼠标左键拖选松手继续
  自动复制；tmux 路径按 会话运行时 统一使用 `load-buffer -w`，iTerm2 不再退化成只写内部 buffer。
  `167c98d5` 已推送并部署到用户更新后的 `.7` 测试机；本地/远端 focused 105 项、单 Gateway、多 TUI、
  MiniMax-M2.7、真实回复、ANSI 灰色层级与 tmux 中文写穿已验。外层系统剪贴板粘贴仍以用户手动验收为准，
  详细证据见 `STATUS.md`。
- 用户随后确认全屏 mouse tracking 下右键不会出现宿主菜单；正文和输入框已新增“已有选区时右键按下直接
  复制”，配对 release 不重复复制且不清高亮。本地六文件 focused 107 项通过；部署和宿主系统剪贴板
  粘贴仍在 ROADMAP，未提前写成完成。
- EXEC-44 写后验证新鲜度已由 aiohttp→Go 真机任务闭环：最后一次源码复制后，被测 Agent 自主重新
  build、运行 29 项行为测试并完成 HTTP 200 E2E，随后才输出 final。验证事实只来自 canonical
  `handler_details` 与 durable event；read/search 不清 stale，Go/Cargo manifest 分类不读项目名或 prompt。
  证据保存在 `.13:/root/tui-parity-evidence/rich-transcript-20260818/aiohttp-closeout/`，测试者未旁路修改产物。
- SANDBOX-02 已由 Tornado→Go 真机任务闭环：约 122MB 构建缓存/临时文件留在 canonical
  `work/.sandbox-tmp`，约 240KB 最终 output 未发现 `.sandbox-tmp/.cache/.gocache/gomodcache`。这证明
  `task_work_dir` 优先 sandbox root 与 `TMPDIR/XDG_CACHE_HOME=/tmp` 的通用修复生效，不依赖 Go/Tornado
  特判。旧 Click→Go 的 137MB 污染证据继续保留，不改写历史。
- 模型调用账本现把“最多 128 条明细”和“完整 request/run 累计数”分开：logical turn、物理 model
  attempt、provider HTTP attempt/retry 与终态分布在明细裁剪后仍准确，且不保存 prompt、response、key。
  超限 focused 回归、本地/`.13` Ruff 与测试通过；旧任务 response 中的 128 已更正为 retained-detail 下限。
- 终端交互 TUI 可观察行为复刻已在 `my-agent` 以 Python/prompt_toolkit 单一 typed 状态链完成：
  typed journal/reducer、stable/active blocks、Markdown/code/diff、spinner/tool/permission、输入/history/
  search/completion/paste/queue、scroll/transcript/mouse/resize、interrupt/exit 和 canonical history resume
  均已接通，旧字符串 lexer/transcript/stream 路径已删除。85 项矩阵结案为 38 `VERIFIED`、38
  `MAPPED_VERIFIED`、9 `NOT_APPLICABLE`。`192.0.2.13:/root/my-agent` 最终部署 70 个文件、删除
  5 个废弃文件并保留回滚包；MiniMax-M2.7 Gateway/TUI 健康，secret 实值扫描 0 命中。详细任务见
  `docs/tasks/completed/TASK-20260818-终端交互-tui-parity.md`。
- 后续四路真机观察已重开其中 C17：旧版运行中普通 Enter 实际等待为下一回合，且 queue preview 会随
  transcript 滚走。该回归不改写本条历史验收事实；当前修复与 `.13` 复验状态以 `docs/ROADMAP.md`、
  `STATUS.md` 和 parity matrix 的 `IMPLEMENTED` 行为准，完成前不得继续引用旧 EV-QUEUE 宣称已通过。
- 用户后续体验指出的软折输入 Up/Down 与不可点击 unseen pill 已按 终端交互 的视觉行/回尾行为补齐；
  `/context` 读取自动 compact 同一估算，`/compact [instructions]` 复用唯一 checkpoint/CAS 主链，自动
  compact 继续按 90% 阈值在每轮前运行。`/effort` 入口只报告后端真实能力；当前 MiniMax-M2.7 没有可调
  effort 参数，设置会明确拒绝而不伪造生效。
- `.13` 真实普通问候、`/context` 与 `/compact` 已把 generation 从 0 推进到 1 并复查 summary/pending；
  同一问候还发现本地 owner 没有主动通道却暴露 `send_message`。当前以工具自身 availability 在每轮 snapshot
  前核对结构化通道事实，修复前 2 次失败调用、修复后 0 次；本地和远端 12 项消息工具 focused 回归通过。
- Fiber→TypeScript 最终请求为 188 次 logical/model/provider attempt、0 retry、186 工具轮和 `done/ok`。
  但产物仅 1,093 功能源码行，约为原版 27,354 行的 4.0%；18 项 Jest 直跑虽过，仍有 open-handle 警告、
  stub/no-op 中间件和大量公开能力缺失，因此只算可运行核心子集，不算完整等价复刻。
- 用户截图追补的鼠标松开后选区失控、终端兔耳少女头像、逐轮 commentary、可折叠 provider thinking、
  红蓝行号 diff、写入预览和命令 stdout/stderr/退出码已在 `.13` 真机可见。首个 Click→Go 请求因旧
  `ECONNREFUSED` 分类失败，修复双层有界退避后，独立续作以 197 个工具轮完成 3,021 行 Go 源码和
  可执行文件；当时 response 的 128 是账本明细上限，不再表述为精确调用总数。`[no test files]` 仍不能
  说成自动测试覆盖，旧缓存污染由随后 Tornado→Go 真实任务完成通用复验。
- 工具运行时已收敛为唯一链：`required_actions -> ToolRuntimeSnapshot -> ToolChoice ->
  provider adapter -> canonical ToolCall -> ActionPolicy -> ToolExecutor -> operation/reconcile -> canonical
  ToolResult -> settlement -> CompletionGate`。旧重复 Schema/审批/effect/执行入口、native 正文提升、
  protocol-v2、parser/JSON repair 和陈旧测试已删除。focused/full/static/package 门已通过；
  MiniMax-M2.7 四条原始普通中文真实验收证据在
  `validation/real_runs/tool-runtime-20260805T141123Z/report.json`。macOS 无 owner-scoped `bwrap` 的执行请求
  按合同阻断，没有降级到宿主执行。详细任务见
  `docs/tasks/completed/TASK-20260804-1913-tool-runtime-unification.md`。
- `/audit` 高频判读没有新增专用 Agent/Memory/Compact 路线，而是在现有 durable spool 主链加入
  时间、条数和按模型上下文计算的数据量三条件合批；自动判完即逐条签收，低风险原文不再进入长命
  子代理重复判。正常记录不截断，单条自身超窗才产生显式头尾模型视图，完整原文与逐条结论可由
  owner-scoped `source_ref/ack_id` 复查；安全上限已经扣除稳定提示词、请求信封和输出余量。spool
  只在完整落盘记录边界组批，避免 48+1 形成一次单条补判。1.10 正式 CLI 五路 10 分钟实测对
  3,000/3,000 条完成逐条结论并保留完整可追溯原文；但 20 条隐藏真样本只命中 14 条、48 条 hit 中
  34 条误报，机制通过不等于模型质量通过。部署后 smoke 还发现积压长日志的 HTTP 整页会超过 1MB；
  现在只按 typed 过大错误缩小完整记录页数，最少一条，单条仍过大才显式失败。最终发布状态仍以
  产品事实页为准。
- 主代理“虚报全部完成”不再通过自然语言分类或普通任务完成硬门处理：参考 终端交互 的结构化核验
  提醒，持久 `task_progress` 仍有 open item 时丢弃第一版 plain final，并在原工具循环中给同一模型
  `open_count` 软核对；下一轮仍可读/更新清单或继续工作，提醒随后移除。同一工具进展段不重复提示，
  但提醒后若产生了新的真实工具动作，后续收口可再核对一次；没有新增工具动作时允许普通任务结束。只有
  显式持久 `/goal` 的 open plan 保持 `unfinished` 并续跑。终态普通 task 与 sticky cwd 也已解耦，
  下一次工作在同一目录创建新的执行 task id；子代理工具严格继承父 run 并只能减权，模型/provider
  物理重试进入同一线程安全账本。发布和真机状态以产品事实页为准。
- 当前 turn 副作用事实已从同一 canonical tool archive/operation store 生成内部
  `operation_verification.v1` 与不含内部 ID/路径/参数的公开投影，贯穿 CLI、Gateway、HTTP、
  transcript、后台任务、历史索引和 compact。1.10 双 owner 真测中，MiniMax 一次零工具调用却声称
  完成，程序精确标为 `status=none`；同会话纠正后才出现四条 succeeded operation。最终两个 owner
  的临时 Memory 均清理、两条真实飞书发送 receipt 均为 sent；自由正文仍不是执行权威，不新增
  自然语言分类器。
- Compact 的可读摘要与操作事实已分栏：真实 MiniMax 反例会把 `remember/list` 错总结成“成功删除”，
  所以 `conversation_thread.v4` 在同一 cursor CAS 中另存有界 `compact_operation_evidence`，后续轮
  在摘要之后消费程序证据。相同反例续问已正确回答未删除；无中文关键词、无第二套会话。
- Memory 第二批恢复场景不再作为未开工路线项：route 冲突/缺权威文件、损坏或缺失恢复包、Gateway、
  subagent 与 local doctor 的既有主链测试已联合复验。长期 Memory 写入是锁内原子 write-through，
  没有 长期助手 外部异步 provider 的 pending queue，因此不复制 `pre-compact flush` 或第二个
  memory provider。只被自身测试调用、可绕过统一 Memory/配额/来源合同直接改 HOT/lesson 的
  `home_memory_notes` 旧写入口与测试已删除；`memory-hot.md` 只保留为用户/管理员现有提示文件。
- 工具结果不再由 live、compact、恢复和子代理共享链各自处理。`ToolRuntimePolicy.output_policy` 的最低输出信任与脱敏策略在
  Registry 执行结果上形成唯一投影，handler 只可收紧；外部网页/浏览器/MCP/视觉/watch 和
  `work/blobs/tool_outputs/` 归档正文统一按不可信数据进入模型，完整正文仍留 owner-scoped artifact，
  prompt 只保留脱敏有界 preview 和恢复引用。JSON/纯文本归档再经 `read_artifact/read_file/search_text`
  读取时继续继承来源，不以扩展名或正文关键词判定。MCP 的重复凭据正则已删除并复用统一 redactor。
  本地 Qwen、MiniMax 和两个真实飞书客户端链路均已验证；真客户端发现的 request 级消息幂等键碰撞也已
  在统一投递入口修复为 per-logical-message 稳定身份，不增加飞书专用分支。
- 多外部写继续使用唯一 operation store，没有新增通用 Saga 或自动回滚。权威 completion 保存失败时，
  provider 的成功回报会降级为 unknown 并阻止盲重做；operation/effect 状态贯穿归档、控制面、模型恢复
  和 compact。显式绝对路径的旧 escape-relocate 兼容链及专属死代码已删除，目标只能按原路径明确成功
  或被统一写边界拒绝。本地 Qwen 基础 CLI、MiniMax 长链/极端 CLI、完整本地门禁和 1.10 双 owner
  Feishu scope/真实出站均通过；新的桌面客户端入站因 macOS 锁屏未冒充完成，精确边界见产品事实页。
- 工具漏参不再由各 handler 或主循环分散补救：`ToolRuntimePolicy.input_policy` 逐字段声明安全默认值或 Registry 可信上下文
  binding，统一入口在 Schema/effect/path/审批前补入并写脱敏 `source/source_ref`。Schema `default`
  注解本身不获得执行权，显式模型字段不被覆盖，其余必填参数仍精确失败。`run_command`、PTY start 和
  `read_artifact` 已迁移，旧 cwd 末端补参和 artifact scope 特判删除；发布与真机证据以产品事实页为准。
- 工具参数合同已从“模型 Schema、拍平 required/type、MCP 投影、handler 各管一段”收敛为一条主链：
  `ToolModelSpec.input_schema` 同时驱动 provider 与副作用前运行门；只做无歧义强类型纠正，完整检查嵌套、
  枚举、范围和额外字段，并返回不含原值的 JSON 路径问题。外层信封与工具参数已明确分层，修复了
  `kind/run_id/status/metadata/artifact_refs` 既是正式参数却被旧协议名单跳过或误判的缺陷。MCP 不再
  压平 Schema，畸形/未支持断言在注册时 fail-closed；旧 unknown-field、MCP protocol-field 过滤和拍平
  required/type 支路已删除。本地 8899 与 MiniMax-M2.7 均已在隔离目录完成真实
  `PATH_NOT_FOUND -> 替代读取 -> write_file` 恢复链；完整 pytest、静态/合同门和干净 wheel
  发布门同轮通过。
- 会话运行时 式当前 turn 引导已经接入：`/btw` 绑定精确当前执行，与 `/stop`、完成共用迁移锁和 active CAS；
  辅助回执无权消费 active-turn input。输入在模型成功接收后幂等写入唯一 thread transcript，compact 续轮用
  typed carrier 保留，不再复制成 task guidance/history。再次发布与 1.10 产物复验前不把 `/btw` 列为稳定完成。
- 前台 request 归档与 durable task 后台接管之间的窄窗不再等同“任务切换”：linked request 的 retired
  状态会继续核对同 thread 当前 TaskRun 并发布幂等 wake；mismatch 和不可读状态仍 fail-closed。该候选
  已有同任务交接接受与真实切换拒绝回归，待 1.10 发布复验。
- 普通用户正文统一收口到模型表达：除显式控制命令外，聊天、派工回执、进度和最终回复都由 LLM 根据
  结构化事实撰写；统一出口只按机器可判定的空正文、真实工具调用和 bracket/XML/native 内部协议拒绝，
  不再用中英文正则猜“完成”、ETA 或大小语义，也不使用固定“正在运行”兜底。任务是否完成只认 typed
  runtime event；开放进度和未聚合子代理不能提前关根任务。
- 1.10 MiniMax M2.7 两个 Feishu-scoped 合成用户最终实测通过：A 精确 5 子任务、`/btw` 后完成，B 精确
  4 子任务、`/stop` 后取消且重启不复活；迟到投递为 0，A/B 记忆口令隔离，最终 `failures=[]`。该证据
  是服务器侧真实 owner/channel/conversation 链，不等同真实 Feishu 客户端入站；37.3/50.7 秒首次自然
  回执延迟仍按产品事实页列为性能缺口。
- 普通任务的派工/等待/完成正文继续由 LLM 生成；回执短轮已剥离旧工具历史，最终完成新增不可变
  `delivery_snapshot`。与快照一致的原模型摘要直接保留；有冲突时才带 draft 进入修订短轮。系统不会
  回退到“任务正在处理”等模板；内部协议、真实工具调用或空正文会重试一次，仍不合格则抑制正文。
  ETA、完成与大小由结构化 facts 约束表达，但用户文字不反向裁决任务状态。
- 普通任务不依赖用户输入 `/goal` 才能自主派工。当前工作树已加入前台安全 quantum：精确绑定的
  Gateway/chat task 到安全点后以同一 task/workspace 转入耐久后台续作，释放普通聊天入口；后台可按任务
  结构继续自做或创建子代理，停止/终态任务不会被该续跑策略复活。该部分已随 `047e24f7` 部署并在 1.10
  观察到两个长任务按 quantum 让出、一个任务自主派 3 个子代理、另一个任务完成 47 项测试；并行聊天的
  第二执行器缺口与尚未部署候选仍以产品事实页为准。
- 后台 claim 的 0 心跳语义、90 秒默认 TTL、同进程域死 owner 立即接管和跨 Pod fail-safe 已闭环；
  Gateway SIGTERM/SIGINT 也进入 typed stop/drain/forensics 主链。真实 1.10 发布复验仍以产品事实页为准。

- 当前 owner home 路径成为唯一默认运行路径。
- 文件大小硬门已改成报告提示。
- 多个旧转发层和历史路径模块已删除。
- owner-scoped 前后台 shell 已改为 bwrap fail-closed，并由 worker/K8s 复用真实 readiness 自检。
- 默认一键安装已进入透明容器 CLI；工作树与 wheel/tar 发布干净度使用同一结构化检查器。
- P0 收敛已完成本地验收：产品事实页、根目录 pytest、配置同步、Ruff、真实 blocker/advisory 报告语义、MCP effect 硬门、sandbox fail-closed 与未跟踪运行数据检查均已闭环；发布状态仍以 `docs/PRODUCT_FACTS.md` 为准。
- P1 主链收敛已完成本地验收：完整 import 矩阵进入 CI，生产 wheel 剥离测试/offline harness，默认 gateway/正式入口/显式插件链收口，真实 embedding 工具检索、POSIX PTY、stdio LSP 与 OpenAI native tools 已接主链；真实生态与规模承诺仍按 `docs/PRODUCT_FACTS.md` 的部分可用/实验性边界描述。
- P2 scale 主链接线已在当前工作树完成：显式 fail-closed profile、PG/ASGI/RLS、Redis 共享准入、OTLP、独立在线迁移 Job、真实 Agent worker 和 continuous-monitor proof 机制均已接线并做本机真依赖 smoke；十万用户、目标集群灰度和 24 小时真实异构来源仍未证明。
- 普通 Feishu 对话与工作主链已收口：真实 chat/topic 多轮 transcript、跨会话隔离、同会话顺序执行、结构化任务选择/提升/完成、内置默认 prompt、USER 自主画像与 SOUL/AGENTS 卡片确认、首条消息不被密码 onboarding 吞掉、长任务异步可恢复回送均已落地；scale worker 复用同一执行链。
- 普通会话累计上下文已接入 owner/thread scope：复用现有 compact 阈值、token 估算和模型后端生成 thread summary，raw transcript 保留；旧聊天进入 owner-local `session_search` 索引。`/verbose off|on|full` 及 typed 工具进度复用持久化回送链，不重提任务。
- 多 IM 投递底座已在当前工作树收敛：普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`；收件上下文与回复信封分离，adapter/capabilities/target validator 统一注册，第二个 fake IM 契约无需修改投递主流程即可接入。生产第二平台与正式部署复验仍按产品事实页标注。
- 普通会话即时控制已在当前工作树收敛：CLI/Feishu 共用 `/status`、`/btw` 和 `/stop`。`/btw` 作为当前
  active turn 的真实 UserTurn 在下一安全点进入模型并幂等写回同一 thread；`/stop` 中断当前 turn 与子代理
  但保留 transcript、workspace 和 memory。旧永久注入与 `/btw-clear` 已移除；真实 1.10 复验仍按产品事实页标注。
- 前台、后台和 compact 后续轮共用同一 owner/thread 的 summary + raw tail；task link、workspace、progress、
  wake 和子代理树只提供结构化运行事实，不再形成 task-scoped transcript、平行聊天历史或第二种 compact。
- 完成回复投影已保留结构化用户摘要：模型自然结束时写出的最终说明，以及显式验收提交里的测试结果、
  主要功能和限制，都会经过统一清洗后进入回复信封与会话历史；验收权威仍是结构化产物/工具事实，
  内部协议和宿主绝对路径不会外泄。
- root 部署的 owner 路径边界已收紧：宿主 home 危险根豁免只给无 owner scope 的本地管理员，远程 Feishu owner 保留 `/root` 拒绝边界，同时继续以精确 owner home 白名单访问自己的数据；Linux root 场景已加入确定性回归。
- 普通聊天和工作共用同一持续 thread；结构化任务工具只在真实工作开始时建立 workspace/进度记录，
  text-tool envelope 统一净化，自动监督 material-delta 去空转，子代理命令日志不进入普通 transcript。
- 运行故障事实已在当前工作树加固：shell 管道失败不能假绿，UNKNOWN_ERROR 保留原始报码和脱敏输入形状，
  Gateway 意外 watch 返回非零、清理 drain 不完整另记失败；persona replace/remove 使用精确 entry ID，
  临时密码/OTP 不进入 durable memory。
- Tool Gateway 已补齐已知门禁报码到统一错误 taxonomy：命令解析、owner/path 隔离、工具协议、限流熔断、
  审批绑定、幂等和管线配置不再因漏注册降级为 `UNKNOWN_ERROR`；防漏测试按真实强制管线及其直接子门扫描，
  动态 artifact-ref finding 先归一为稳定协议码。专项回归已通过，1.10 真机复验待当前长任务自然收口。
- 工具调用运行身份不再把后台主代理的临时轮次误认成子代理：只有线程级 subagent context 或显式
  `task_local` scope 才查询子代理 canonical ledger；主代理保留持久 `root_task_id`，真实子代理 load error
  仍原样进入结构化审计。`606fe20a` 已部署 1.10，两个全新 Feishu-scoped owner 的后台工具事件零假错。
- `raise_collaboration` 已接入工具 envelope 的持久 `root_task_id`，后台主代理无需让模型补传任务身份；
  协作域报码保留为 `reported_error_code`，运行时另用已注册的 parameter/arguments/execution 控制码，
  工具输出 artifact 与 index 也同时保存两层错误事实，不再把真实原因压成单独的 `UNKNOWN_ERROR`。
- 后台最终回复不再使用唤醒轮开始时间伪装成消息完成时间；会话消息仍按 append-only 顺序读取，thread
  活跃时间只允许向前，避免长任务完成后反而把会话排序回旧时间。
- 派工自然回执的模型可见 facts 已从 runner/recorded 等内部生命周期名投影为 planned/ready/started/
  failed-to-start 用户语义；运行权威账本不变，模型仍按真实数字自由组织回复，不靠关键词拦截或固定句子。
- 后台完成事件已在当前工作树分层：成功兄弟完成信号短窗合并，部分成功只内部整合，最终/失败/阻塞/
  需决策才由父代理写普通会话并经正式投递链外呼；后台续跑严格复用原 task link 的
  goal/workspace/index 标题，不再被定时提示覆盖。旧的子代理固定完成通知旁路已经删除。
- 真机复验发现并修复 observation/wake 双写竞态：完成事件改为 wake-first 单入口，内部 wait/自动续推的
  运行中占位正文不再污染普通聊天；wake 失败仍保留 observation 兜底。
- 真机复验发现父任务 goal 仍可被 wait reason 覆盖；现已把 task identity 不变量下沉到 store 的原子
  bind 入口，保留首次 goal/workspace/created_at 并拒绝跨 thread 重绑。
- 真机双用户长任务复验暴露的批量派工与完成投递缺口已在当前工作树收口：同一模型轮里的多次
  `create_subagents` 按序全部执行并合并为一份事实回执，依赖前序结果的编排调用仍延后且返回专用错误码；
  模型臆造的 owner-home 交付路径在未获用户显式授权时归回当前任务 `output/`；后台自动续跑只有形成
  结构化最终交付时才进入普通会话，内部监督、等待和占位正文保持内部可见。
- 真机复验继续暴露“结构化完成与 findings delta 同轮出现”时完成信号被 delta 遮住；当前工作树改为
  结构化完成优先，并且 IM 回复信封只携带已投影的人话，不再把内部完成块、结论账标记或宿主路径交给
  adapter。这样既不恢复内部碎碎念，也不会在产物已验收后静默吞掉父代理最终回复。

## 2026-08-22 裸启动与恢复控制面收口（`.7` 已部署复验）

- 解决了普通用户运行 `my-agent` 被全机历史任务扫描和虚假 `[Y/n]` 阻塞的问题：裸启动与 `chat` 共用
  轻量 fresh-session 路径，只有 `resume <session_id>` 能恢复明确会话。
- `status` 已成为无开关、无副作用的显式投影；旧的两个启动恢复配置删除。普通 run 的 stale attempt
  由 Gateway 启动调和，subagent 由单 Gateway 周期 supervision 调和。
- 派工巡查锁已改为 POSIX `flock` / Windows `msvcrt` 内核锁。空文件、坏 JSON、旧 PID 和 PID 复用不再
  决定锁归属，进程死亡由内核自动释放；focused parser/startup/status/lock/recovery tests 通过。
- `311b8db` 与配置统一补丁 `06b84e1` 已进入远端 `main` 和 `.7`。真实裸 TUI 1 秒内显示输入框与
  `MiniMax-M2.7`，普通中文请求经同一 Gateway 返回；单监听 PID 为 `1918200`，锁元数据持有者同 PID。
- 首轮 Gateway supervision 记录 `orphans_revived=4`、`parent_closed_cancelled=21`、
  `running_reclaimed=5`；3 条恢复 RUNNING 随后自然 DONE。连续两次显式 status 均只读返回相同近期计数。

## 2026-08-22 内部状态面拒绝的副作用分类

- 解决问题：shell 在执行前拒绝模型读取内部 child 状态文件时，旧结果被误当成“命令可能已经产生未知
  副作用”，导致整个 child 回合被硬停并留在 PENDING。
- 落地内容：保留 `WRONG_STATUS_SURFACE` 安全拒绝，增加 canonical `effect_outcome=not_started`；模型
  可读取结构化原因后改用直属生命周期事件或结果引用，真正的 unknown 仍 fail-closed。
- 验证方式：ShellTool 直接回归与 authorized dispatch/operation status 集成回归均通过；远端真实 TUI
  复验留到候选部署后执行。
