# LLM_GUIDE

这份文档是给 LLM/AI 开发者读的项目入口指南。

**开工前必须读，收工后必须改。**

---

## 铁律

1. **开工前**：读 `docs/ROADMAP.md`，确认你要做的事在列表里，了解它的"解决问题"和当前状态。
2. **收工后**：更新 `docs/ROADMAP.md`（改状态或删除已完成条目），把已落地的功能写入 `docs/COMPLETED.md`。
3. **每个功能必须写"解决问题"**：不能只罗列模块名和 workflow 名，必须说明它解决哪个用户痛点、系统风险或交付缺口。
4. **不改设计文档就不能改设计**：如果实现方向与 DESIGN_LEDGER.md 有冲突，先更新设计文档再写代码。
5. **每次汇报必须带"建议下一步"**：父代理、子代理、其他线程和交接文档都要说明推荐后续动作、并行边界和风险守门点。
6. **自然语言不当机器事实源**：用户 prompt、模型 summary、报告正文、guidance、角色描述只能做沟通和软引导；状态、权限、验收、恢复、派工和产物归属必须来自结构化字段、refs、工具结果、显式配置或文件系统事实。
7. **主链路优先，兜底最后**：主链路没跑顺前不要加 fallback、旧路径兼容、影子入口、只转发 facade 或“还能跑”的旁路；确定不用的旧兼容要删。
8. **参考成熟项目先于自己发明**：底座以 会话运行时 为第一参考；会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导只要 会话运行时 有明确实现，就适配本项目既有 owner/thread/task 事实源，不再并行造一套。长期助手 只补 会话运行时 覆盖较少的长期 Memory、Persona、多用户持久调度和被动验证；通道运行时 只补 IM adapter、通道健康和投递边界。

---

## 2026-08-23 当前运行基线

- 普通工具的 `workspace:*` 只是当前 turn 调度/审计事实，不进入跨 run 持久
  `resource_locks`；父子代理可共享 cwd 和交付路径。operation 幂等/replay、精确
  `logical:*` 互斥、active turn、owner 墙、write boundary 和沙箱仍保留。本地可信
  CLI/TUI 使用启动 cwd；远程用户仍强制进入各自 `owner_home`，两者不得混同。
- 模型成本统计复用唯一 `ModelCallLedger`，按 request/run 累计 provider input/output/
  cache-read/cache-creation，明细裁剪不截断总账。无 provider usage 时按结构化估算
  单独计数。TUI `ctx` 只表示当前上下文压力，不得当成任务累计消耗。
- provider 的 typed `socket.gaierror` 与 会话运行时 `ConnectionFailed` 一样先走现有 2/5/15 秒有界 HTTP 退避；
  DNS 瞬断不能一跳终止数小时 child。三次耗尽后仍返回 typed transient failure 供既有模型轮恢复，畸形
  URL、认证、代理配置和普通错误字符串不因此取得重试权；每个物理 attempt 继续进入唯一调用账。

- 根代理的默认 `system_prompt` 采用 会话运行时 Default 的 assumptions-first 软纪律：明确目标下先从 cwd、代码、
  用户约束和工具事实补信息；安全可逆的次要选择由模型采用合理默认并继续。只有无法从上下文取得、且任何
  合理假设都会造成实质偏离、越权或不可逆风险的关键缺口才问一个简短问题。该纪律不解析用户正文、不生成
  机器状态、不恢复完成验收；发布 YAML 与 dataclass 默认值必须逐字一致，用户仍可通过唯一
  `system_prompt` 配置入口覆盖。
- 子代理完成、阻塞或能力申请唤醒不是一条新用户任务，而是原 root active turn 的后续工作片。必须从
  exact thread/task link 恢复原始 objective 和 task path：原始 objective 继续占据 `User Task` /
  `root_user_prompt`，结构化 wake 只作为 runtime continuation 注入。该 root 自己的 canonical tool-output
  index 按 child 完成信封携带的 exact `conversation_request_id` 恢复已执行工具、一次性派工去重和有界执行
  轨迹；durable task id 不能冒充 active turn id，字段落盘前的旧行只可由同值 `request_id` 精确兼容；嵌套 Todo/派工参数
  必须以限深、限宽、凭据脱敏的 JSON 保留，不能退化成空数组。跨进程索引不能伪造原始 ToolCall/ToolResult
  配对；native 续跑用唯一 `CompactionSummary` handoff 持续携带这批事实，精确副作用仍以 archive、operation
  ledger、artifact refs 和当前文件为准。child 私有索引、其它 task 和 detached Audit 事件不得混入。
  后台每工作片的新增工具额度在恢复历史后保持不变；现有 Todo 更新必须复用 exact id。派 child 时只有
  调用方显式提供的 `create_subagents.items[].covers` 才建立映射，不从标题或 goal 猜关系；未绑定 child
  以真实 run id 形成独立进度行，不关闭现有 Todo。
- lifecycle wake 恢复的耐久工具索引必须同时保留 bounded `tool_execution` 与 `tool_operation`；副作用
  工具不能因跨后台工作片丢失 operation 终态而从 succeeded 退化为 unverified，也不能只凭 `ok=true`
  反推成功。系统生成的精确 `work/agents/<run>/final_report.md` 是完成信封的有界交接投影，可由
  `read_file` 读取；若模型保留 exact run id 却拼入过期 task 目录，读取层只可经同 owner 的 canonical agent
  projection 找回这一片叶子并再次经过读权限门。同目录其它状态文件、目录枚举和 shell 读取继续拒绝，
  报告正文不参与完成裁决。
- 当前 canonical `task_progress` 已有计划时，`create_subagents` 在任何 child 落盘前执行一份原子结构预检：
  `covers` 与 `output_files` 都是可选结构化提示。提供的 covers 必须绑定仍 open、且未被同批其它 item
  占用的 exact id；提供的 output 必须位于父级 workspace。同批多项主动声明的 output 完全相同，或存在
  祖先/子目录关系时也在创建前整批退回；省略 output 时宿主不猜实际写集。省略 covers 时 child 按真实 run id 记进度，
  不会给现有 Todo 打勾。无效的显式值统一返回 `effect_outcome=not_started + required_repairs`，整批零创建。
  这个合同不读 goal/标题/代码量，不判断质量或完成，也不能靠 capability grant 扩到兄弟目录；模型修正
  结构化参数后重试原任务。它的控制报码必须
  在唯一 `error_taxonomy` 登记为可修参数错误，不能让外层降成 `UNKNOWN_ERROR` 后误导模型报告阻塞。
  历史“goal 正文里恰好出现 Todo id 就自动补 covers”分支已经删除；`i18n`、`config` 这类既可能是目录名
  又可能是计划 id 的文本不能取得绑定权威，缺少显式 `covers` 时保持未绑定并走真实 child 进度行。
  已关闭项需要返工时，先用 `task_progress` 对原 id 传 `status=in_progress, correction=true`，再选择绑定；
  也可省略 covers，但不能拿无关的下一个 open id 顶替。
- 普通主代理和子代理共用 `turn_end.reason`：`completed` / `blocked` /
  `max-tokens` / `aborted` / `error` / `interrupted`。它只表示一轮为什么结束，
  不从模型正文、验收清单、产物数量或测试描述反推完成。
- 递归协作只有一个创建入口 `create_subagents`，创建成功后宿主立即自动启动。
  模型不再看到 `dispatch_subagents` 或 `schedule_child_subagents`；内部 dispatcher
  只是启动、恢复与有界重试引擎，不是人工推动工具。
- `create_subagents.items` 只放可立即并发、彼此不等待未来结果的工作；同批 child 不会因为 goal 写了
  “先 A 后 B”而串行。B 必须读取 A 的修复、产物或结论时，先只创建 A，等 typed lifecycle wake 后再创建
  B。该边界沿用 会话运行时 的独立 sidecar 软纪律，宿主不解析 goal/role、不新增依赖状态机或机器质量验收。
- 并行代码 child 必须按 会话运行时 的 disjoint write set 软纪律拆分：每个 item 的 goal 同时写清共同目标目录和
  该项独占的文件/模块范围，职责宽到会覆盖兄弟项或会修改同一文件/模块时不得同批创建。`output_files`
  可以辅助说明交付范围，但仍不是完整写集、权限或机器锁；宿主不解析 goal 猜路径，也不恢复目录锁。
  唯一硬反馈只来自调用者自己提供的结构化 output：同批相同或祖先/子目录声明原子拒绝，要求模型缩窄或分批。
- `orchestration` 不进渐进披露折叠区；`create_subagents`、`send_guidance`、
  `cancel_subagents` 和 `resolve_capability_requests` 必须从前台首次模型调用就直接可见。
  `tool_search` 继续用于 /goal、外部协作、web、vision、meta 和 MCP 等延迟能力。
- 子代理生命周期事件直接唤醒父级；不再为每批 child 登记周期性 LLM 巡场或 `wait` 推进。
  `send_guidance` 只接受一个直接下级 `target` 和一段 `message`，不支持广播、跨层催办或验收。
- 子代理创建下一层后，宿主立即以 `interrupted/SUBAGENTS_ACTIVE` 结束当前工作片，
  并用精确直属 run ids 的耐久等待记录排除孤儿误复活。成功兄弟收齐后只唤醒一次；
  失败、缺状态或 capability 阻塞立即唤醒直属父级。父级新工作片直接获得有界
  `direct_children` 状态、结果 refs 和待裁决请求，不需要查树或 shell `sleep`。
- 每个 child 的断点与续跑只归自己的 runner/agent thread；`context_scope=task_local` 不得创建
  `ordinary_task_resume` 或租用主代理后台回合。任何误指向 child task id 的旧后台 policy/wake 都在
  调用模型前关闭并由 child runner 接管，主代理不能携带 child 身份或权限运行。一次 runner 结果重新
  落为 `PENDING` 时只释放该 run 的启动占位；共享批次宿主 PID 仍活着不能阻止这个 child 立即续派。
- `task_progress` 是当前模型的软计划/记事账本，不是业务质量验收器。普通模型准备自然 final 时，若自己
  留下 `pending/in_progress/unknown` exact 项，宿主按 会话运行时 stop hook 在同一 active turn 有界核对一次；
  模型继续使用原工具，或按原 id 关项/标记真实 blocked。核对耗尽仍 open 时本轮 typed blocked，不能把
  durable task 写成 DONE；它不扫描正文、代码、测试或产物，也不创建 `ordinary_task_resume`。显式
  `/goal`、Compact 和 typed 子代理/控制事件仍各走自己的既有生命周期。新账本项必须有稳定
  `id + title + status`，避免空白行。
- `task_progress(action=read)` 默认只读当前 active turn 的 canonical 账本。模型若把当前结构化
  `task_id` 显式填进 `run_id`，该值只作为当前账本别名并经过共享 task-path resolver；只有与当前任务
  不同的明确 run id 才按历史账本精确读取。不能让 request/task id 在后台续片中旁生一份空 Todo。
- canonical `task_progress.v1` 继续保存同一长期 task-path 的完整软计划历史；TUI/Web 的当前清单另读账本内
  host-owned `display_plan(generation_id/revision/item_ids)`。普通用户回合以 exact conversation request id
  换代并只展示本代明确更新/派工的 ids，同一 lifecycle wake/child 续片沿原代补项。前端在 dequeue 时先
  登记期望代次，旧后台轮或最终 notice 的迟到快照不能把上一阶段 Todo 顶回来；该投影不删除历史项、不参与
  完成、恢复或验收。没有 `display_plan` 的升级中任务仍兼容展示完整旧账本。
- 模型侧旧 `raise_event` 已删除。进展、工具活动、阻塞、权限申请和终态都由宿主从真实 runner/thread
  事件写入；模型不能靠自报事件证明自己还活着。子代理 canonical state 会保存有界的“模型响应中 / 正在
  使用工具 / 工具成功或失败”短状态，不保存 prompt、response、工具输出或隐式推理正文。
- 父代理对下级的日常控制面只剩创建、给一个直属下级发补充消息、打断/取消直属下级，以及处理
  直属下级的结构化 capability 请求。模型没有查树、等待、巡场或“再推动一次”工具；`/status`、TUI
  和恢复逻辑仍读取宿主内部代理树投影。模型的取消合同也不暴露 dry-run、进程参数或整树回执，
  防止把打断工具变成另一个巡检入口。
- TUI 只读可观察面从 active task link 和 canonical child run 生成有界直属快照，在输入框附近固定显示
  子代理名称、status、当前活动、耗时和 attempts。它不进 transcript，不暴露 goal/工具输出/路径/权限，也不参与
  完成、重试或验收。未来用户直控必须先落一份 TUI/Web/IM 共用的 owner-scoped typed
  protocol；不允许前端直改子代理账本。
- `context_scope=isolated` 只用于把结构化事实改写成一句用户可见回执：该物理模型调用继续进入成本与调用
  账本，但不得把自己的 thinking 或小上下文数字投影成主任务状态。Todo 标题显示 typed 完成数/总数与
  运行数，默认四行只是视窗；系统生成的单项 `items` 补派也必须沿同一父级 sibling 历史连续编号。
- 主代理和 child lifecycle wake 对用户完整目标负责；子代理只把直接父级交给自己的当前 `goal` 当作完整
  工作边界，不能因为 root 目标更大而实现未交给自己的兄弟计划项。两层共用 会话运行时 式持续完成与验证软纪律：
  骨架/空壳/最小欢迎页只能算阶段；验证要覆盖实际入口，安装、构建、启动或关键路径失败后必须修正并重跑。
  能重现当前缺陷的有效测试不得为了变绿而删除、skip、放宽断言或降成存在性检查。这些仍然只是模型执行
  提示，不读取代码量、不解析完成文案，也不恢复宿主机器质量验收。
- 单 Gateway 内后台回合按 `owner + thread_id` 分车道：同会话仍由持久 run claim 串行，
  同 owner 的不同 TUI/会话在显式全局上限和 `background_threads_per_owner` 上限内并发。
  一个旧会话的长 policy 回合不能占住整个用户的子代理完成唤醒。
- 普通 child 自动继承直接父级的结构化工作区上界，孙代理逐层继承同一上界；不要求模型重复声明父级
  本来就能写的目录。`output_files` 可记录用户明确交付目标和冲突线索，批量时可由负责写入的 item 分别
  声明；它不是完整写集或写权限，也不能把父级工作区外的自然语言路径变成权限。同批显式声明若相同或
  互为祖先/子路径，创建入口只退回这一批让模型重新分工；模型没有声明产物时，
  编排器不得凭空生成 Markdown
  业务交付合同；child 的 typed status、最终回复与系统 `final_report_ref` 已构成 会话运行时 式完成交接。
  历史 `system_default_output_ref=true` 只作旧账恢复，不进入模型可见文件合同或父级 expected outputs。
  命名 Audit/exact-scope worker 继续只用精确授权。
- 写边界使用 会话运行时 同款“最具体路径条目优先、同层 deny 胜出”：宽泛的 `/root` 保护不能误伤其下更窄的
  task output 授权，但同路径或更窄的禁止规则仍然拒绝。
- 历史 `acceptance_checks` / `verification_status` 字段仅为旧账本可读兼容，不进入当前
  TaskEnvelope、runner 模型摘要、父级 wake 或树摘要，也不参与启动/完成判定。
- 普通 shell 不允许绕过直属生命周期事件去读 `work/agents/*` 内部状态文件；该门发生在进程启动前，
  必须返回 `WRONG_STATUS_SURFACE + effect_outcome=not_started` 给模型改用正式结果引用，不能误判成
  `TOOL_OPERATION_OUTCOME_UNKNOWN` 后中断整轮。真正已经启动且副作用终态未知的命令仍保持 fail-closed。
- Compact 的当前语义是每个 main/child/grandchild 各有独立 ConversationThread，并只认同一 owner/thread
  checkpoint + generation CAS。transcript 旧段由 `conversation/compact.py` 提交；运行中 native IR 由
  `conversation/live_tool_compact.py` 适配到同一账本，provider overflow 也不得账外删工具对。TUI/Web/SQLite
  只读 generation；presentation/no-save 临时窗口事件不计数，旧 durable apply/attribute 不回读。live 摘要
  请求必须保持“原任务 user 在前、native history 居中、synthetic Compact user 最后”的 会话运行时 顺序。
- 普通回合工具终态折叠只追加一次确定性、脱敏投影，未发生真正 Compact 时旧模型历史必须保持稳定前缀；
  缓存命中只读取 provider usage 账，不以 Context 估算冒充。手动 `/compact` 成功后，Gateway 控制结果通过
  `task_status.compact_generation` 返回 canonical 代数，TUI 立即发布同一 typed boundary 并撤下压缩前的
  provider-visible Context 快照；下一次真实模型调用再刷新精确数字，不能继续显示旧 `compact 0`，也不能
  为刷新界面额外调用模型或从成功文案反解析代数。
- TUI transcript 的 follow/离尾状态属于每个 main/child 页面各自的 process-local viewport。提交有效消息、
  首次进入新页面或显式回到底部时，prompt_toolkit `Window.get_vertical_scroll` 必须把 control 的尾部锚点
  落到真实窗口；手动滚轮/PgUp 离尾后保持原阅读位置，不因后台输出或切换页面复用旧 Window scroll。

---

## 项目结构速览

```
my-agent/                          ← 项目根目录
├── LLM_GUIDE.md                   ← 【你正在读的文件】LLM 入口
├── DESIGN_LEDGER.md               ← 设计台账（所有设计决策的来源）
├── STATUS.md                      ← 当前状态、测试基线、推荐下一步
├── GATEWAY_DESIGN.md              ← Gateway 架构设计
├── MEMORY_BACKLOG.md              ← 记忆系统痛点和设计原则
├── DISCUSSION_BACKLOG.md          ← 系统问题讨论
├── docs/
│   ├── ROADMAP.md                 ← 【必读】待做/进行中功能清单
│   ├── COMPLETED.md               ← 【必读】已落地功能清单
│   ├── design/                    ← 模块设计文档
│   │   ├── README.md              ← 模块设计文档索引
│   │   └── subagent-quality-contract.md
│   └── modules/                   ← 模块四件套（discussion/progress/purpose/structure）
│       ├── README.md
│       ├── subagent/
│       ├── memory/
│       ├── gateway/
│       └── live-lab/
├── agent_py_agent/                ← Python 包源码
│   ├── README.md                  ← 包级使用说明
│   ├── __main__.py                ← CLI 入口
│   ├── agent/                     ← 核心 agent 代码
│   │   ├── DIRECTORY_GUIDE.md     ← 【必读】目录职责地图
│   │   ├── agent_core/            ← 主代理编排
│   │   ├── backends/              ← 模型后端适配
│   │   ├── capability/            ← 能力路由
│   │   ├── gateway_parts/         ← Gateway 文件协议
│   │   ├── io/                    ← 底层 I/O 原语
│   │   ├── local_storage/         ← SQLite 本地事实源
│   │   ├── memory_store/          ← 记忆存储
│   │   ├── memory_archive/        ← 记忆冷归档
│   │   ├── memory_routing/        ← 长期规则路由
│   │   ├── prompting_parts/       ← Prompt 构造
│   │   ├── settings/              ← 配置
│   │   ├── subagents/             ← 子代理领域
│   │   ├── subagent_workflows/    ← Workflow 模板
│   │   └── tooling/               ← 工具实现
│   ├── cli/                       ← CLI 子命令
│   ├── config/                    ← YAML 配置文件
│   ├── data/                      ← 运行时数据和 prompt 模板
│   ├── tests/                     ← 测试
│   └── prompts/                   ← Prompt 模板
└── scripts/                       ← 工具脚本
```

---

## 开工前：必读清单

按顺序读，每个文件解决一个问题：

| 顺序 | 文件 | 解决什么问题 |
|------|------|-------------|
| 1 | `LLM_GUIDE.md` | 你在哪、该读什么、怎么改 |
| 2 | `docs/ROADMAP.md` | 要做的事在不在列表里、当前状态 |
| 3 | `DESIGN_LEDGER.md` | 设计决策来源、是否有冲突的设计原则 |
| 4 | `agent/DIRECTORY_GUIDE.md` | 代码放哪里、边界是什么 |
| 5 | `docs/design/` 对应模块 | 模块级详细设计（如果改动涉及特定模块） |
| 6 | `docs/modules/<module>/02-progress.md` | 模块最近推进了什么 |
| 7 | `docs/modules/<module>/04-structure.md` | 模块结构和核心文件 |

如果改动涉及测试：
- 读 `TESTS.md` 了解测试策略
- 读 `TEST_CHECKLIST.md` 了解收口检查项

如果改动涉及 subagent/capability/runner：
- 读 `SUBAGENT_RUNBOOK.md` 了解运行协议

---

## 收工后：必改清单

### 1. 更新 `docs/ROADMAP.md`

- 如果你完成了某个条目：把状态改为"已落地"，或直接删除条目（已移入 COMPLETED.md）。
- 如果你在做某个条目但没做完：更新"当前进展"和"待做"。
- 如果你发现了新问题或新需求：在 ROADMAP.md 末尾新增条目，必须写"解决问题"。
- 如果你发现设计冲突：先更新 DESIGN_LEDGER.md，再更新 ROADMAP.md。

### 2. 更新 `docs/COMPLETED.md`

- 新增一条记录，包含：解决问题、落地内容、验证方式。
- 不要写太长，每条 10-20 行足够。

### 3. 更新 `DESIGN_LEDGER.md`（如果涉及设计决策）

- 新增条目写清楚：日期、状态、摘要、已落地、后续方向。
- 超过 100 行的细节拆到 `docs/design/<module>.md`。

### 4. 更新 `STATUS.md`（如果改动影响测试基线或可用能力）

- 更新测试状态（通过数量）。
- 更新"当前可用能力"或"当前主要限制"。
- 更新"最新恢复入口"（如果改动影响恢复链路）。

### 5. 更新模块四件套（如果改动涉及特定模块）

- `docs/modules/<module>/02-progress.md`：记录本次推进。
- `docs/modules/<module>/04-structure.md`：如果结构变了。

### 6. 写清本轮汇报的下一步

- 最终回复、handoff、子代理报告都要包含 `建议下一步`。
- 建议要能直接指导下一位开发者行动：先做什么、能不能并行、不要碰哪些用户本地改动、需要跑哪些验证。
- 如果已经完成到可暂停，也要明确建议是等待评审、合并、观察 CI，还是进入下一阶段。

---

## 编码规范

### 注释格式（两层注释）

```python
# LLM: technical summary of the contract, side effects and invariants.
# 函数用途: 人能看懂的用途、调用时机和修改注意事项。
def example(...):
    ...
```

- 每个功能代码里的 module / class / function / method 都要有这类双层注释。
- module 使用 `模块用途:`，class 使用 `类用途:`，function 和 method 使用 `函数用途:`。
- class / def 有装饰器时，注释放在第一行装饰器上方。
- `LLM:` 给后续模型读，写清 contract / invariant / side effects / caller expectations。
- `模块用途:`、`函数用途:` 或 `类用途:` 给人读，用大白话说明用途、调用入口、修改时要检查什么。
- 有副作用的函数必须写清：会不会写文件、调用 API、启动进程、改状态。
- 测试函数不强求长注释。
- 批量补注释后必须跑 `compileall`、`ruff` 和 `scripts/check_code_size.py`；注释不计入实现行数，但语法位置和装饰器位置必须正确。

### 测试要求

- 新增测试用 `test_*.py` 或 `test_` 前缀，`run_tests.py` 会自动发现。
- 完整冒烟默认调用真实 API，echo/fake backend 只用于纯函数定位。
- 冒烟测试必须隔离 workspace，不污染开发仓库。
- 测试通过后更新 `STATUS.md` 的测试数量。

### 代码放置决策

按这个顺序问：

1. 外部协议客户端？→ `backends/` 或 `clients/`
2. 主代理流程编排？→ `agent_core/`
3. 子代理领域规则？→ `subagents/`
4. 工具实现或安全边界？→ `tooling/`
5. Gateway 文件协议？→ `gateway_parts/`
6. 本地事实源持久化？→ `local_storage/`
7. 记忆存储？→ `memory_store/`
8. Prompt 上下文构造？→ `prompting_parts/`
9. 配置 schema？→ `settings/`
10. 底层无业务含义的 I/O？→ `io/`

都不是？先写清楚变化原因，再决定是否需要新目录。不要塞进 `utils/common/shared`。

---

## 文档体系

### 设计层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `DESIGN_LEDGER.md` | 设计决策主台账，只放摘要和导航 | 每次设计决策变更 |
| `docs/design/*.md` | 模块级详细设计 | 模块设计变更时 |
| `GATEWAY_DESIGN.md` | Gateway 专项设计 | Gateway 架构变更时 |
| `*_BACKLOG.md` | 痛点收集和设计原则 | 发现新痛点时 |

### 状态层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `STATUS.md` | 当前状态、测试基线、推荐下一步 | 每次测试基线或能力变更 |
| `docs/ROADMAP.md` | 待做/进行中功能清单 | 开工前读后、收工后改 |
| `docs/COMPLETED.md` | 已落地功能清单 | 功能完成时 |

### 运行层

| 文档 | 用途 | 更新频率 |
|------|------|---------|
| `SUBAGENT_RUNBOOK.md` | subagent/capability/runner 运行协议 | 主链路变更时 |
| `TESTS.md` | 测试策略 | 测试策略变更时 |
| `TEST_CHECKLIST.md` | 收口检查项 | 新增检查项时 |
| `CLI_REFERENCE.md` | CLI 命令参考 | 新增/变更命令时 |
| `CODEBASE_TREE.md` | 代码树说明 | 结构变更时 |

### 模块四件套（docs/modules/<module>/）

| 文件 | 用途 |
|------|------|
| `01-discussion.md` | 灵感和讨论 |
| `02-progress.md` | 推进、解决的问题和测试 |
| `03-purpose.md` | 初心和设计想法 |
| `04-structure.md` | 结构树、核心文件、数据流 |

同步门：`scripts/check_doc_sync.py` 会检查 covered module 的文档是否与代码同步。

---

## 设计原则

从 DESIGN_LEDGER.md 提炼的核心约束：

1. **递归协作者模型**：主代理、子代理和孙代理共用同一轮模型/工具循环；差异只来自结构化父子身份、权限上界和工作区。
2. **自然完成**：模型不再请求工具时结束本轮；宿主只记录 `turn_end.reason`，不在回复后追加机器质量验收。
3. **事实与质量分开**：路径、权限、工具终态和产物存在性仍是结构化事实；“做得够不够好”由模型/用户继续复核，不设通用硬门。
4. **patch 不自动应用**：patch 审核器只审核状态，不自动应用 diff（除非有独立审核链路）。
5. **自学习必须用户确认**：学习候选必须经过用户确认才能变成正式 skill。
6. **默认 dry-run**：所有调度/执行命令默认 dry-run，只有显式开关才改状态。
7. **不删文件**：action apply 不删除文件，不覆盖已有内容。
8. **文件第一事实源**：机器判断必须读 JSON/JSONL，Markdown 台账不是事实源。
9. **每个功能写"解决问题"**：不能只罗列模块名。
10. **通道故障 ≠ 任务失败**：runtime/session/adapter 故障不等于任务本身失败。

---

## 常见场景

### 我要加一个新功能

1. 读 `docs/ROADMAP.md`，确认是否已有相关条目。
2. 读 `DESIGN_LEDGER.md`，确认没有设计冲突。
3. 读 `agent/DIRECTORY_GUIDE.md`，确认代码放哪里。
4. 写代码、写测试。
5. 跑 `python3 -m pytest -q` 确认全量通过。
6. 更新 `docs/ROADMAP.md`（改状态）和 `docs/COMPLETED.md`（新增记录）。
7. 如果涉及设计决策，更新 `DESIGN_LEDGER.md`。

### 我要修一个 bug

1. 读相关代码和测试。
2. 修 bug、补测试。
3. 跑 `python3 -m pytest -q` 确认全量通过。
4. 如果 bug 揭示了设计问题，更新 `DESIGN_LEDGER.md`。

### 我要重构代码

1. 读 `agent/DIRECTORY_GUIDE.md`，确认边界。
2. 读 `DESIGN_LEDGER.md` "代码体检与后续拆分计划"条目。
3. 只移动一个低耦合区域，同步迁移调用点，不新增旧入口转发壳。
4. 跑完整测试后再继续。
5. 不在同一轮同时改行为和大移动文件。

### 我不确定该不该改

1. 读 `docs/ROADMAP.md`，看优先级。
2. 读 `DESIGN_LEDGER.md`，看设计原则。
3. 读 `STATUS.md` "当前主要限制"，看是否在限制列表里。
4. 如果都不确定，在 `DISCUSSION_BACKLOG.md` 里记录问题，等确认后再动。
