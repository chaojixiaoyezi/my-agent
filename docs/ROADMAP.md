# ROADMAP

状态标记：

```text
设计中       已形成方向，还没写代码
部分落地     已有基础结构，但还没完整接入运行链路
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

每条记录必须包含"解决问题"，说明它解决哪个用户痛点、系统风险或交付缺口。

更新时机：开工前从这里领取任务，收工后更新状态或移到 COMPLETED.md。

---

## 下一版优先级

来自 STATUS.md，当前最急迫的任务已清空，下面保留中长期项。

### 单 Gateway 多 TUI 项目目录与正式提示词 2

状态：`993ce4f` 已部署并由原样提示词 2 真机证明前后台 cwd 一致；转入持续回归

解决问题：从非 Gateway 启动目录运行 TUI 时会误找另一套 cwd-hash 队列，等待一分钟后退出；即使只放宽
readiness，模型和工具仍可能在 daemon cwd 工作，造成项目写错位置。

当前进展：服务目录已固定为 owner 级唯一 Gateway，客户端 cwd/roots 改为 thread v6 的 typed 状态；
前台、后台、Tool Gateway、任务交付和 child 输出必须共用该值。`a091b72` 部署后原样提示词 2 已在一个
Gateway 中创建 child，但首个 ask 因薄 TUI 的 audit Agent 为空而漏传 workspace，产物写到 `/root/bbb`。
后续候选把 audit 与 workspace 分参，首个 inbox JSON 回归已证明携带 client cwd；同时 child 行改为职责
短标题、实时上下文 token 和单行宽度预算。`0ffbfe4` 部署后的正式 TUI 已证明 workspace 与三条 child 行
正确，但 Todo 仍重复完整 child goal；`669c228` 已按 exact run id 只在视图隐藏重复 seed。继续观察又证明
自动 wake 的 `run-*` 丢失 thread cwd，relative child output 回落 `/root`；`993ce4f` 在后台 turn 构造处
恢复同一 thread 快照。全新 tmux `dsh-p2-mario-993ce4f` 已证明首次 ask、后台 main、6 个 child 与工具
working_dir 全部保持 `/root/dsh-tui-p2-993ce4f`，最终 `bbb/` 产物齐全且 HTTP 200。该项不再等待专项
补丁；后续四个正式任务仍持续对账，发现回归时只修 thread/turn workspace 主链，不增 cwd fallback。

### TUI 活动状态、跟随滚动与 Compact 真机复验

状态：直属 child 活动区已部署并完成真实 TUI smoke；其余交互矩阵和 process-local lane 自愈待验证

解决问题：旧 TUI 在长任务运行时把普通 Enter 当成下一轮队列，用户补充消息要等当前任务结束才执行；
queue preview 又位于可滚动 transcript，离开尾部后看不见。并行实验室若完成后长期空闲，也会浪费四路
换时间的测试目标。

当前进展：ordinary input 和控制操作已使用稳定 message/operation ID、exact expected turn、持久 outbox、
accepted/rejected/unknown 三态和 GET-only reconciler。2026-08-20 的灰色思考、灰色 `Ctrl+O` 提示、
tmux 左键复制已部署 `.7`；右键直接复制仍是本地候选。2026-08-21 本地又补齐 Working 动画、仅在用户
原本位于页底时自动跟随、后续 thinking 增量持续显示，以及 typed Compact 活动块和 5/15/52/78/92/100
阶段进度。输入后续消息时也会保留可见回执；离开页底浏览历史时不会被强拉回去。
真实植物大战僵尸复验又暴露同 thread 旧后台 run 错挂新任务权威链、后台 notice 复用 block id 被丢、
用户目录只写在 child goal 而未进入结构化权限，以及创建后周期性 LLM 巡场。当前已改为 task-bound
后台 run、notice 立即/每秒独立块、`items[].output_files` 完整 schema，并删除自动巡场与 wait 工具；
`send_guidance` 也收为只能向一个直接 child 插入消息。
首轮原样任务还证明 OPEN capability request 会被通用 completed 收尾误写为 DONE，grant 后原 run 没有
重新排队；模型因此误以为 child 已结束并自己接管。当前已改为结构化 OPEN 优先、grant/deny 后同 run
自动续跑，普通 child 逐层继承父级工作区；旧 `inspect_agent_tree` 工具类与 Schema 也已彻底删除，内部
代理树只服务 `/status`、TUI 和恢复诊断。
`e321483` 原样 TUI 又确认模型首轮看不到 `create_subagents`：0 child、13 次 Bash、10 次 Write，
导致“嘴上派 5 个，实际自己写”。本地已从默认渐进披露中移除 `orchestration`，按 会话运行时
multi-agent v2 使递归代理控制首轮直出；待二次部署和全新 TUI 复验。
`c2c0235` 二次部署后已真实创建 4 个 child，但其中 2 个因路径规则误判反复写失败超过 17 分钟；
宽 `/root` forbidden 错误压过 task output 的窄 allow，grant 同一路径也无法改变当前尝试。状态投影又读错
ToolResult 字段，只有空 `RUNNING/0%`。当前候选按 会话运行时 最具体路径优先修复，并记录不含正文的模型/工具
活动；同时彻底删除重复宿主生命周期的 `raise_event` 模型工具，历史 grant 也不能复活它。

`d257dfb` 已推送并部署 `.7`。新轮 3 个 child 全部自然 `DONE`，实际写出 8 个游戏文件，
没有 `WRITE_FORBIDDEN` 或重复 capability request，活动状态也已显示模型/工具阶段。但最后 child wake
排队超过 150 秒仍未唤醒主代理：同 `local/main` 的另一旧 TUI 正在跑长
`ordinary_task_resume`，owner 级唯一 tick 把所有会话错当成一条车道，并且旧任务还抢走了一个 child 名额。
当前本地候选已对照 会话运行时 的 per-thread active turn，拆成无模型准备、ready-thread 规划和单 thread 消费，
以 `(owner, thread)` 去重、同 thread 持久 claim 单飞、不同 thread 有界并发。真实 scheduler 阻塞对照回归已通过；
待严格 gate、推送部署和全新 TUI 最终复验。

`bea6fed` 的后续原样轮已拿到四名 child 全部 `DONE`、完整产物和 HTTP 200，但仍耗时 422.849 秒、
32 次模型调用，累计输入估算约 19.47M tokens。新定位的主因是 task-local 父级在等孙代理时被孤儿器
误复活，以及孙代理成功事件越级逐条唤醒根会话。当前本地候选改为精确直属等待和递归事件链：创建后
typed 让出，同批成功只恢复一次，失败立即恢复，父级直接收到 `direct_children` refs；同时删除
`task_progress` 普通任务自动续跑，使 open 软清单不再制造额外模型轮。待严格 gate、推送部署后，用
同一原样 TUI prompt 比较模型调用数、耗时、子代理终态、产物与 8080。

`d928d77` 后续原样轮已把递归事件链验证到位：四名 child 都只运行一次并自然 `DONE`，最后一个完成会
自动唤醒主代理，无需用户发“继续”；受管服务真实监听 `0.0.0.0:8080`，loopback 与局域网 HTTP 都为
200。仍未通过的是交付位置和可观察性：`/root/abc` 为空，页面实际落在 task 内部 `output/abc`；child
上下文混入旧 `/root/kill-ws/...` read pack；前台让出后 TUI 没有持续 Working；普通 leaf 还收到四个
无法使用的下级管理工具。当前本地切片已改为裸相对路径继承 cwd、只有显式 output/work 前缀进入 task
内部目录，shared context 只取当前轮，Gateway/TUI 用 canonical active task count 显示一个可移除的
Working 块，并按 `can_spawn_children` 裁剪 leaf/coordinator 工具面。
当前轮继续对照 终端交互 `CoordinatorTaskPanel` 收口：Gateway 从 active task link 和
canonical subagent run 生成有界直属 child 快照，TUI 在输入框下固定显示每个 child 的名称、状态、
职责短标题、耗时、当前上下文 token 与 Compact。固定后缀先占宽度，短标题按剩余列截断且永不换行；
该区域不进 transcript，不影响用户上翻，也不作完成或重试权威。

`f5dc695` 的新鲜 Prompt 2 轮证明 durable wake 创建到后台 claim 约 7.56 秒，长等待主要来自 main 被
唤醒后又做了已委派实现。当前候选继续收口：批量顶层 description 不复制给每个 child，逐 item schema
明示独立职责；main/child 都严格单行；final notice 与 live Todo 共用 exact run-id 去重；会话运行时 式
coordinator 在委派后只整合、测试和汇报，缺口继续交给 child。8 个直接相关 focused 文件已通过，待严格
gate、部署和下一条原样 MiniMax-M2.7 TUI 复验。

`931ee20` 已完成上述展示复验：四条短职责、实时 context、单行 main 和终态标记均正确。新阻塞转到底层
完成交接：旧 wake 没带 child 最终回复和 `final_report.md`，main 因而猜错目录。当前候选按 会话运行时 标准
completion message 补 `subagent-completion.v1`，同根 DONE 信封一次合批，并让 active wake 在上下文压力
下优先保留 metadata/报告 refs。`fd7d2b9` 部署后的全新 Prompt 3 已证明首批结果能被 main 读取并继续
派第二批，但最终只创建 6/8 个 child，漏 轻量运行时/通道运行时，并误报已有 终端交互 结果缺失，正式任务仍失败。
下一切片先从结构化批次覆盖、完成事件与 result refs 查清原因，不用用户插话或任务名专项提示补救。

该 Prompt 3 的下一层事实已经查清：6 个 completion envelope 都完整且分别被 main 消费，遗漏不是 wake
丢失；canonical `task_progress` 以 `task-path:<目录指纹>` 保存 8 项计划，后台上下文却按 durable request id
读到了另一份空账。当前本地候选把路径指纹算法收口到 `runtime/task_identity.py`，后台 Task Runtime
State、child 终态同步、Goal 续跑和 TUI 投影全部复用同一编号。普通清单依旧不充当机器验收或自动续跑门，
只确保模型每个后台轮都看得到自己尚未完成的计划。待 focused/严格 gate 后部署，再用全新 tmux 原样重跑
Prompt 3，重点验收 8 个目标全覆盖、最终横向汇总及 Todo 终态。

`ac1f4dc` 已按 终端交互 `TaskListV2`/全局 animation frame 收口默认任务摘要并部署 `.7`：固定四条，优先
最近完成、当前运行和下一待办；`Ctrl+T` 展开全部。常驻 Context 去掉不直观的协议构成，只保留总量、
占比、canonical Compact 次数和明确命名的自动压缩点；prompt/messages/tools 留给 `/context`。定向回归
已通过，待严格 gate、部署及正式任务中的动画/按键/次数真机复验。全新 Prompt 3 已证明默认 `4/9`、展开/收起和
`compact 0` 生效；原始 Todo 仍全是 pending，因为模型派工没有传 typed `covers`，后续应加强结构化绑定
而不是按标题猜任务对应关系。

同轮四名 child 中 代理运行时 在读取内部 child 状态文件时被 `WRONG_STATUS_SURFACE` 正确拒绝，但旧
ShellTool 没声明“进程未启动”，operation coordinator 因 run_command 的潜在副作用把它保守升级为
`TOOL_OPERATION_OUTCOME_UNKNOWN`，导致整个 child 回合 interrupted/PENDING。当前候选只在这一条
执行前拒绝上补 `effect_outcome=not_started`，保持安全门不变，让错误回到模型换正式结果引用；真正未知
副作用仍 fail-closed。待 focused gate、部署后用新的原样 TUI 任务复验 child 不再因此挂起。

`714c0c8` 已推送并部署 `.7`，本地 117 项 focused、远端 11 项投影/渲染 focused 与提交前严格 gate
均通过，按用户约定未重跑全仓 pytest。真实 TUI 已证明两个 child 一次 attempt 自然完成，固定活动区能
逐步显示状态、动作、耗时并在根任务收口后移除。该轮也抓到旧 Gateway 进程保留 process-local thread
lane 占位的样本：durable wake 与 ready 事实均正常，但重启前未领取，优雅重启后立即领取并约 30 秒自动汇总。
待做：补齐 lane queued/running age 诊断与有界自愈，再验证 child 完成后父会话无需重启且不受其它 TUI 长回合阻塞；继续验证 Working 动画、thinking 连续增量、页底自动跟随、
离底不抢滚动、Compact 百分比、后续消息可见、右键复制和输入框粘贴。外层系统剪贴板仍须用户在已 attach
的本机终端亲自粘贴确认，自动化不能冒充这一步通过；同一普通中文游戏 prompt 还要证明 child 自动
完成后父级无需用户发“继续”，能读到产物、整合到 `/root/abc` 并实际监听 `0.0.0.0:8080`。

边界：当前测试机为 `192.0.2.7:/root/my-agent`；保持单 Gateway、多 TUI、tmux 观察会话和远端
key/config/runtime 数据，不触碰其它项目。较早大切片已按约定执行过一次全仓 pytest；当前切片远低于
10,000 行，只跑相关 focused tests 与远端提交前严格 gate，不重复浪费时间跑全仓。

### 主/子/孙代理统一 Conversation Compact

状态：设计中；当前先修真实次数展示，持久主链迁移尚未开始

解决问题：子代理也可能连续工作数小时。当前主代理用 owner/thread 的 Conversation Compact，task-local
子代理仍用 run workspace 的旧 compact continuation，回合内工具 IR 又会独立裁剪；这会让次数、恢复、
checkpoint 和失败熔断表现不一致，并已经出现 child 实际裁剪后仍显示 `compact 0`。

当前进展：已对照 会话运行时 `session/turn.rs` 与 multi-agent spawn：每个 child 都是独立 thread，但所有 thread
共用 pre-sampling/mid-turn Compact 状态机。本地过渡候选把 exact child 的 native IR reduction 以纯数字、
幂等事件累计进 canonical run 展示投影，并与既有 durable apply 行相加；main Context 同时显示准确
`压缩点 90%`。该切片不冒充架构统一。

待做：child 创建时建立稳定 `agent_thread_id`，把每轮消息、摘要、checkpoint、generation/CAS 和 resume
接到现有 `ConversationStore/compact.py`；随后让 TUI 只读每个 agent thread generation，并删除
`finalization_compact_auto`、task-local compact apply/continuation 与过渡计数。切换期间不得长期双写，
也不得把父、子、兄弟正文合并到一个 thread。

### 用户直控子代理的共享控制面

状态：设计中，直属 child 只读状态投影已部署并完成真机 smoke

解决问题：当前用户只能给主代理插话或停整个任务，无法定位一个正在跑的 child；现有
`cancel_subagents` 又是终态取消，不是“打断当前一轮后仍能继续”。如果 TUI 和未来 Web 各自直改账本，会导致状态、
权限和恢复逻辑分裂。

当前进展：已有 owner/thread 范围的 active root + direct-child 只读投影。待做是 Gateway/domain 唯一
typed protocol，支持查看树、给选中 run 发消息、interrupt 当前 turn、resume/start 同一 session、
cancel/close 和 capability 裁决。root owner 可操作自己树内任意后代，模型代理仍只能管直属下级。
每个写操作必须带幂等 `operation_id`、exact target、expected version/state 和 accepted/rejected/unknown 回执，
TUI/Web/IM 都只调用这一份服务。

边界：本阶段不做 Web 页面，不开放任意数据库修改或越 owner 操作；不能用终态 cancel 伪装成可恢复 interrupt。

### npm 缓存全新 Node 任务真机复验

状态：待验证

解决问题：Fiber→TypeScript 因 npm 默认写只读 `$HOME/.npm` 先失败一次，让模型浪费调用自行探索环境
workaround；需要一项全新 Node 任务证明底座默认值首次就生效。

当前进展：EXEC-45 已由 Fiber 真实请求闭环；owner-scoped shell 增加标准
`NPM_CONFIG_CACHE=/tmp/.cache/npm`，不修改 `HOME` 或写边界，并已部署 `.13`。本地和远端 focused tests、
Ruff、py_compile 已通过；Fiber 自身早于部署启动，只能证明模型 workaround，不算底座 E2E。测试者在下一
Node 任务仍只观察，不能修改任务产物。

边界：仍只修改 `/Users/example/my-agent`、部署 `192.0.2.13:/root/my-agent`；不触碰青禾项目，
不输出测试机 key，小于 10,000 行的本轮修复只跑 focused tests。

### Memory v2 单一主链收敛

状态：代码迁移完成，最终全量与真实模型验证中

解决问题：旧实现把候选正文、每日镜像、learning draft、task-local memory gate 与正式长期记忆分散在
多套账本和状态机中，后台提炼也缺少统一触发、证据核验、失败恢复和真实 Gateway 验收，容易产生双写、
旧事实复活、跨 scope 覆盖和模型绕过审核的问题。

当前进展：已落地 owner 唯一 Candidate/Daily/Curator/Promotion/Lesson/HOT/Recall 主链、一次性 v1→v2
Migration、Retention v2、统一管理员 CLI 与 Gateway owner-maintenance 接线；旧 learning/memory-gate/daily
mirror 生产路径已删除。一次性 `my-agent run` 也已在模型前接入唯一 ConversationStore user 原文和
Memory message ref，并保持 standalone workspace 生命周期；focused 回归通过，隔离 testbox B5R3 已用
真实 DeepSeek 完成 remember、Candidate、Promotion、正式记忆、workspace 终态和同 request 存储重放，
群内独立审查通过且无 P0/P1。最终全量/静态门禁、剩余真实 Gateway 触发矩阵、真实双模型、重启恢复和超长阅读仍待完成，
因此尚未标记完成。

设计与证据：`docs/modules/memory/03-purpose.md`、`04-structure.md`、`05-memory-v2-layout.md`、
`06-runtime-memory-requirements.md`。

### Agent 基础能力单一主链收敛

状态：待验证

解决问题：能力自述、Shared/Skill、Memory/Persona、Scheduler、Workflow 和验证证据存在重复事实源或
主链接入不完整，导致模型能力与真实运行状态漂移，并增加多用户串权和长任务不可靠风险。

当前进展：用户已确认 `docs/design/FEATURE-20260718-agent-foundation-convergence.md`；owner/channel
registry、能力自述、Skill 单一逐轮 snapshot、Memory 稳定 ID CRUD/batch、Persona 单一 repository/
版本/CAS/回滚、owner-scoped 持久 Scheduler，以及 Workflow 收敛为 Skill + 当前 task plan + 原生
tools/subagents 均已完成聚焦验证；旧 workflow package/mode/config/CLI/extension/index 已删除。
被动验证证据也已接入公共工具出口，能保留真实命令/exit/targeted/full 并在文件写后过期。
工具参数也已收敛到单一 `ToolModelSpec.input_schema`：provider 展示、文本/native 入口、MCP、运行门和 handler
前校验不再各保留 required/type 副本；保守强类型纠正、嵌套/枚举/范围/额外字段校验及协议同名参数
碰撞已完成本地回归，并由本地 8899 与 MiniMax-M2.7 的隔离工具失败恢复链验证。当前后续切片又把
缺失参数限制为逐字段明示的安全默认值或 Registry 可信上下文绑定，并为每个有效输入保留脱敏
`source/source_ref`；旧 `read_artifact` 运行身份和进程工作目录专项补参已删除，待完整门禁、模型、
1.10 与正式双用户复验后发布。
外部多写已继续复用唯一 operation store，没有新增 Saga 或自动回滚。提供方结果只有在权威终态保存
成功后才能作为成功返回；保存失败降级为 unknown，结构化状态会穿过 archive、control-plane event 与
compact，语义摘要另保留中段非成功副作用事实。完整门禁、本地 8899 基础 CLI、MiniMax 长链/极端
CLI、1.10 双 Feishu owner scope 与真实出站均已通过；新的桌面客户端入站仍按产品事实页保留为外部
验收边界。
Fiber 真实长任务新增一个待发布切片：末尾 `return_code=143` 已被 operation 正确标为 failed，但模型仍按
stdout 片段口头完成。当前候选已对照 会话运行时 active-turn/Stop-hook，把明确 failed/not_started 的 final 冲突
改为最多两次的同轮带工具返工；unknown 等不确定副作用仍直接 fail-closed。本地结构化回归已通过，待
`.13` 部署后用普通中文真实任务证明模型确实修正命令、产生新的 succeeded 终态且 TUI 不再先退回空输入框，
通过后再移入完成事实。
极端 MiniMax CLI 发现并删除了旧“绝对路径写飞后静默搬进 task output”兼容层。显式绝对路径现在保留
原目标身份，由唯一写边界返回明确成功或 `WRITE_FORBIDDEN`；相对 `output/`、`work/` 任务落位不变。
修复后的真实链已在 CLI 和 1.10 正式 owner scope 验证“成功—拒绝—继续成功”及模型准确部分结果。
`max_active_agents` 与结构化 owner 写入口已接同一 quota lock；Gateway 已按有界 owner page 自动执行
基于结构化终态、二次校验、trash tombstone、legal hold 和审计的 retention。应用门不能覆盖任意
Shell/PTY/LSP 进程写盘，正式规模部署仍需 filesystem/project quota。完整本地 CI、严格 code-size、
本轮 Schema 改动现已通过完整 pytest、Ruff、import/offline/code-size/doc-sync、compile、diff、
distribution boundary 和干净 wheel artifact gate。未跟踪运行数据继续由 worktree clean-package
正确阻断且不得进入 wheel。Schema 这一切片已随精确提交进入远程 `main`，部署 1.10 唯一正式
Gateway/Feishu，并完成本地 8899、MiniMax-M2.7 和两个既有真实飞书 owner 的只读工具调用复验；
当前 Compact 收口进一步删除了漏算原生 ToolCall 参数的字符预算，改由主代理、Gateway conversation
与子代理共用完整 provider-visible token 估算和既有 recent-tail；真实旧版长链又证明配对删除本身
会丢路径、checkpoint 和未完成状态，当前候选因此复用既有语义摘要后端，在同一 native IR 中用最多
一条 replacement item 承接旧段，不新增 task/chat Compact。本地 8,371 项完整 pytest、静态门禁与
干净 wheel 已通过；仍待提交、精确 wheel 部署和同一真实飞书项目的 200K/90% 多次续作复验后移入完成项。
更大范围的基础能力长稳、真实群组和规模验证仍属于本路线项。

验收：完整本地 CI 通过，推送远端 main，部署 1.10，并完成既有飞书双用户、多长任务真实 LLM 标准。

补充记录：2026-05-05 已完成一轮 CI 回归修复，解决 gateway 测试互相污染、日志证据测试缺导入、runner 解析失败结果写回不一致等问题；细节已移入 `docs/COMPLETED.md`。

---

## 设计中（未开工）

### Subagent 自然收口与递归控制面

状态：本地已落地，待 `.7` 真实 TUI 验证

解决问题：旧链同时存在创建、手动派工、层级调度和机器验收，多套控制面让模型重复创建、等待或被宿主
粗暴打断。当前已收敛为递归一致的父子关系：所有层级只用 `create_subagents` 创建并自动启动；父级只需
接收生命周期事件、补充 guidance、取消/中断和自然汇总。主代理、子代理、Gateway 共用六类 `turn_end`，普通完成不再
依赖 `acceptance_checks`、`VERIFIED` 或模型生成的专用结果壳。

待做：部署 `.7` 后只从真实 TUI 输入一次植物大战僵尸任务，观察 child 数量、自动启动、父级汇总、失败
恢复、Compact 和最终 0.0.0.0:8080 可访问事实；测试者不得旁路修改任务产物。

设计台账：`DESIGN_LEDGER.md` “2026-08-21 子代理递归控制面与自然收口”

### 注释重构规范

状态：设计已记录，待分阶段落地

解决问题：大量函数 docstring 是一句话说明，缺少 LLM/人类双层说明，新接手的 AI 或开发者难以理解副作用和边界。

待做：按模块分阶段补齐双层注释（config → tools → core → CLI → subagent）。

设计台账：DESIGN_LEDGER.md "注释重构规范"

---

## 部分落地（有骨架但未完整接通）

### Subagent Channel Probe

状态：部分落地

解决问题：通道故障和任务失败必须分开判断，接管前先确认 workdir、机器 JSON、写入现场是否正常。

已有：本地工单现场 probe、channel_status 状态机、CLI 命令。

待做：模型接口 probe、ACP/adapter/session probe、派工前自动 probe 和失败阻断。

### Grant 注入执行上下文

状态：部分落地，grant 后自动唤醒已落地

解决问题：capability grant 不能只停留在运行记录里，必须能变成子代理实际可读取的执行包。

已有：`SubAgentExecutionContext`、`build_execution_context()`、`write_execution_context()`、CLI 命令；capability grant 后会创建 observation/wake signal，并把 `capability_grant_wake` 写回 task。

待做：按 token 预算裁剪 execution context；子代理写 capability request 统一入口继续精简。

### 层级能力上抛

状态：部分落地，授权后下发唤醒已落地

解决问题：子代理遇到问题不应自己全局搜索 skill/tool，应描述能力缺口，由父代理发现和下发。

已有：`CapabilityRequest`/`CapabilityGrant`/`CapabilityGap`、单个子代理 runner 入口、capability request 路由、grant 后 wake signal 和同 run follow-up。

待做：跨多层级的批量上抛汇总；tool failure mode 和显式 alternative group 参与排序。

### 本地恢复、诊断、worker 和 adapter 第一版

状态：部分落地，runner session heartbeat 已落地

解决问题：LocalStore 一致性诊断、gateway 请求崩溃恢复、adapter 文件协议。

已有：`local-doctor`、`local-rebuild`、gateway failed 归档、processing lease、保守 worker pool、runner session heartbeat 账本、`adapter file`、启动恢复结构化检测错误、后台 dispatch 启动标记错误报告；普通回复/主动消息/附件已共用 DeliveryService 与 adapter registry；adapter 入站现已在媒体和 Gateway POST 前落盘可信 channel/user/conversation/provider message identity 与 canonical digest，由单 worker 恢复精确 POST body、输入/结果/控制回执、占位和回送，ingress/reply row 都以 owner/epoch/expiry 短租约实现跨进程接管和旧结果 fencing，冲突正文隔离，429/5xx 保持等待，auth/config 与 input terminal-unknown 进入无正文持久终态；`/btw` unknown 按独立 operation ID 恢复而不拿目标 turn 去重，首次 GET 可单向绑定空 target，控制回执 watcher 冻结并发送 channel/user/conversation/chat type/chat id 身份供 Gateway 精确鉴权，stop rejected/terminal-unknown 也有明确或 durable unknown 收口；当前会话的 typed slash dispatcher 已统一 `/status`、`/btw`、`/stop`、`/goal`、`/verbose` 与 `/audit` 入口，未知 `/XXXX` fail-closed，命令词不进入模型；`/stop` 已改为优先中断当前窗口精确 live request，再持久清理 task/subagent。普通派工/等待/完成新增瘦身 LLM 回复轮与最终 delivery snapshot；后台 claim 具备 90 秒 fail-safe 和同进程域死 owner 立即接管，Gateway signal shutdown 有 typed forensics。

待做：LocalStore compact/backup/export、gateway 请求优先级、独立子进程隔离版 runner worker、adapter HTTP/WebSocket 版，以及第二个生产 IM 的真实 API/媒体/重启复验。系统命令与窗口级 `/stop` 已随 `7776a03f` 部署 1.10，并由两个既有真实飞书客户端在运行中完成中断、无迟到回复、停止后续聊和 transcript 不含命令的复验。

### Runtime 配置层与错误报告全链路

状态：部分落地，子代理 run/task overlay 与主要 best-effort 结构化已落地

解决问题：运行时 overlay、owner/task/run scoped 配置、错误报告和恢复摘要必须成为一条真实链路，不能只停在合同测试或局部 helper。

已有：基础配置来源链、`RuntimeConfigLayer`、CLI runtime overlay 环境入口、子代理 `config_overlay_ref` 创建继承/接管记录/worker 装载、后台启动和启动恢复错误可见化、gateway/audit 旁路错误结构化。

待做：远端 session/ACP 入口接入 scoped config；继续扩展少数低频 gateway adapter/supervisor 旁路异常的统一持久化。

### 可见真实环境测试台 Live Lab

状态：部分落地

解决问题：开发 agent 不能只靠单元测试，需要能在用户看得见的终端里跑真实 runtime。

已有：`live_agent_lab.py`、隔离配置、命令执行、transcript 和 summary、`open_live_lab.sh`。

待做：memory 长任务、tools 边界任务、问题任务和多轮恢复任务场景。

### 并行开发 Workstream 工作台

状态：部分落地

解决问题：多个 AI 可以并行，但必须先把目录、分支、职责边界和交接格式定清楚。

已有：`WORKSTREAMS.md`、`HANDOFF_TEMPLATE.md`、worktree 创建/状态/打开脚本。

待做：`workstream_sync.sh`、`workstream_handoff_check.sh`、主线集成固定 checklist。

### 外部痛点文档：DISPATCH_PAIN_POINTS / PAIN_POINTS

状态：已记录，部分约束已落地

解决问题：workdir 不可靠、子代理启动后不建工单、completion notification 不等于完成、BLOCKED 不能当终点等系统性痛点。

已落地：标准工单目录模板、`ACTION_RECEIPTS.md`/`TEST_CHECKLIST.md`/`BUGS.md`/`SKILL_USAGE.md`/`HANDOFF.md`。

未落地：顶层任务现场模板、SPEC→TEST_CHECKLIST 追踪链、Tool failure log、Browser/PWA/Game 自动验收器、数据规模断言器、安全任务隔离策略。

---

## 待验证

### 普通 Feishu Agent 对话与工作真实验收

旧部署的双 owner 真测确认身份与产物隔离，但也暴露 `/btw` 会被辅助回复轮提前消费、以及 task-scoped
history 让补充要求在续轮中丢失。当前候选已禁止辅助轮消费 active-turn input，并按 会话运行时 的 transcript
commit 语义把成功接收的 `/btw` 作为真实 UserTurn 幂等写入唯一 thread history；compact 续轮使用 typed
carrier，不再维护 task-id guidance history。focused tests 已通过，待发布后在 1.10 重新做两用户真实
Feishu 长任务、连续 `/btw` 和独立产物验收。

解决问题：同一用户多轮忘记、旧任务污染闲聊、首条消息被密码卡吞掉，以及长任务超过同步等待窗后真实结果无法送达。

已验：1.10 MiniMax M2.7 双 owner/thread 长任务、compact、旧聊天检索、跨用户/跨话题隔离、
`/verbose`、人设、USER 偏好、SOUL 确认卡与长任务回送；生产 compact 阈值已恢复 90%。

本轮待部署后复验：`/goal` 同 thread 持续续跑/暂停/恢复，`/audit` 显式结构化激活，
`/btw` 写入并在原 live turn 下一安全点真正消费且不创建后台重复执行器，`/stop` 能立即打断模型流，
之后模型以精确结构化 task id 重开原 workspace，失败选择不能新建旁路任务，以及前台聊天和后台
长任务并行时不串 prompt/workspace。还需验证前台让出后两个用户的普通聊天都能在后台主代理继续工作时及时回答、
后台可按任务结构自主派工、最终消息包含真实目录/功能/测试结果且不泄露内部协议。

`/audit` 高频候选当前已完成本地机制收口：等待时间、累计条数或累计数据量任一条件触发无历史
批次；正常记录保持完整，极端超窗单条才生成显式头尾视图；原文、哈希、分数、结论和模型来源均
可按 `source_ref/ack_id` 查回。focused 与完整 pytest 已通过，待 1.10 正式 CLI 五源 10 分钟实测
确认吞吐、召回、误报、重复和排空情况后再从本节移入部署事实。

### chat 交互体验：中文输入和后台输出

状态：待验证

解决问题：`input()` 和后台线程同时输出时破坏输入行，中文删除有视觉残留。

待做：确认 `prompt_toolkit` + `patch_stdout()` 在各终端下稳定。

### 裸 TUI 启动与单 Gateway 恢复权威

状态：已完成；`06b84e1` 已部署 `.7` 并完成真实 TUI 复验

解决问题：直接运行 `my-agent` 会扫描全机历史任务、阻塞在没有真实恢复动作的 `[Y/n]`；同时旧的
0 字节派工锁会让 Gateway 的 runner supervision 永久跳过，真正失联任务反而得不到调和。

已有：裸启动走轻量 fresh chat；旧 session 只允许显式 resume；status 只读；普通 stale attempt 移到
Gateway 启动恢复；subagent supervision 使用内核 advisory lock，空/坏元数据不再形成永久锁。

结果：`.7` 保持单 Gateway 与 MiniMax-M2.7，裸 TUI 在 1 秒采样点已出现输入框且无全局恢复提问；旧
0 字节锁被同一 Gateway 重写为 v2 内核锁元数据。首轮 supervision 结构化记录 4 条复活、21 条父会话
关闭取消和 5 条失联 runner 回收，随后 3 条真实 RUNNING 自然完成，没有手工批量改账。
