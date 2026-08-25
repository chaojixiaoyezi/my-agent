# Verification：开发推进

## 2026-08-25 后台续片不再丢失 succeeded operation

- 真机 root 首次 `create_subagents` 的 canonical 结果已经有 succeeded operation，但外置工具索引没有保存
  该字段。child 完成后的 background slice 只能恢复 `ok=true`，按现有 fail-closed 规则正确归为
  unverified；最终回复因此是 unfinished，active task link 和 TUI Working 均不会关闭。
- 当前在工具输出第一次 externalize 前注入 host-owned execution，并对白名单 operation identity/status/
  action/replay 做大小输出一致持久化。carried record 恢复现有 flat verification 字段；没有 typed status 的
  旧记录继续 unverified，绝不把所有 `ok=true` mutation 放宽为成功。
- background focused 回归已直接证明 child terminal wake 后 operation verification 为 succeeded、root link
  为 completed；externalizer 回归同时证明任意 diagnostic/private envelope 字段不会进入耐久 index。

## 2026-08-18 Fiber 143 收口冲突返工

- Fiber→TypeScript 前台最后一条 E2E `run_command` 实际完成 HTTP 请求后由清理命令收到 SIGTERM，权威
  operation 仍明确落为 `FAILED`：`COMMAND_FAILED / failure_stage=execution / handler_executed=true /
  effect_outcome=failed / return_code=143`。模型看到了完整回执，却依据 stdout 的成功片段输出“完成”；因此
  根因不是工具结果丢失，而是 completion 把冲突草稿直接切到无工具表达轮，模型没有机会修正命令并复验。
- 对照 会话运行时 `session/turn.rs`、`hook_runtime.rs`、`protocol/items.rs` 和 Stop-hook E2E 后，已把明确
  `failed/not_started` 的冲突改为同一 active turn 的 `completion_conflict.v1` continuation。原工具面保留，
  同时给模型最近 typed 终态和被拒绝草稿；全 turn 最多两次。修复产生新的 succeeded operation 后自然收口，
  两次仍只口头完成才进入 `OPERATION_INCOMPLETE`。
- `unknown/cancelled/incomplete/unverified` 继续直接 fail-closed，不因这次体验修复恢复自动副作用重放。
  本地 fake provider 回归已证明：已知失败可在第一个返工轮调用修复工具并以 `runtime_status=ok` 完成；连续
  两次忽略冲突后才进入无工具未完成回复；unknown 从未收到 completion repair 工具轮。真实 `.13` 候选部署
  与同类 143/非零退出返工仍待执行，因此当前只记本地机制通过，不记真机 E2E。

## 2026-08-18 aiohttp 真机闭环与 no-effect 尾部裁决

- aiohttp→Go 请求在最终源码复制后自主重新 build、通过 29 项行为测试并完成 HTTP 200 E2E，证明
  `handler_details` 中的 durable verification state 能跨 read/search 保持 stale，并由新的真实验证收口。
  EXEC-44 从待真机复验升级为已验证；测试者没有修改任务文件。
- 同一请求最后一个可选清理命令在 handler 前被安全策略拒绝，旧逻辑却让 `not_started` 覆盖此前成功
  mutation，并把完整 operation 历史误投影成整项未完成。completion 现以最后一项 effect-bearing mutation
  为权威：连续 no-effect 尾部仍留审计，但不推翻最近 succeeded；known failed/not_started 冲突走上述有界
  同轮返工，unknown 等不确定终态仍直接 fail-closed。
- current-turn focused 回归覆盖成功后 no-effect 尾部放行、只有 no-effect 仍阻断和 unknown 尾部仍阻断；
  本地与 `.13` py_compile、Ruff、测试均通过。当前 Fiber→TypeScript 请求只用于观察真实收口，开发者不
  旁路补产物。

## 2026-08-18 写后验证新鲜度与真实代码任务反例

- Tornado→Go 真机请求在核心 build/test 成功后又修改 examples，随后只有 grep/read 就尝试收口；最终
  examples 未复测，因此整体交付不通过。该反例证明“最后一条工具是不是 write”不能代表验证新鲜度。
- `record_tool_verification` 现在把 `verification_evidence/state` 合并进 canonical
  `metadata.handler_details`，与 archive/model/closeout 读取位置一致；文件修改保留最近 durable
  verification event 的 ID/状态并把根置 stale，read/search 不会清除。
- `project_facts` 按 `go.mod/Cargo.toml` manifest 增加 Go/Cargo 的规范 build/test 分类。completion 只按
  root + verification event ID/status 去重软核对；同一周期不循环，新的真实验证后才能重新武装。
- 本地和 `.13` 的 verification runtime/project facts/current-turn/runtime-guidance focused tests 到
  100%，py_compile 与 Ruff 通过。修复已部署；下一真实代码任务负责 E2E 证明最后修改后确实重新验证。

## 2026-07-31 审计结果送达引用

- `message_tool_delivery.v1` 的归档白名单现在可以保留有界的 `evidence_refs`。引用来自已持久化的
  Audit 逐条结论，只用于证明某次用户送达对应哪些耐久记录；原始日志、判断正文和工具私有结果不会
  被复制进 Compact 或最终回复上下文。
- 引用不能制造工具成功、改变验证状态或扩大 owner 权限。只有真实成功的消息工具结果才能形成送达
  证据，随后由同一 owner 的 Audit ledger 把引用记录标记为 reported。

## 2026-07-29 五套 CLI 等条件对照与六项底座回归

- 五套 CLI 使用同一只读 corpus manifest、相同普通中文任务和 `MiniMax-M2.7` 串行运行。独立验收不只
  看退出码，还核对工具调用、页面/链接、源路径是否存在、子代理 run/state、语料是否被修改和最终正文。
  五套都生成了站点，但都存在不同程度的语义过度断言；没有任何一套被记录为“自动语义验收通过”。
- my-agent 的六项可归因底座反例已变成结构化回归：额外读取根可读但不可写；过程写入不因未来页面
  尚未存在而返回 side-effect 失败，最终静态检查仍失败；两批系统默认 child refs 不冲突而显式 refs
  仍冲突；A/B/AB/ABC 混合流不重复且相同合法 delta 不丢失；CLI 只补印未流出的程序尾注；持有后台
  claim 的当前轮可使用自己的 workspace，而真正的第二执行者仍被拒绝。
- 1.10 Linux/bwrap 已实测只读根可读不可写且源 hash 不变。MiniMax 修后长链的文件和命令主链通过；
  独立流式测试中正文与核验各只出现一次；两批协作严格产生 2+2 共 4 个 `DONE + VERIFIED` child，
  后台第二批创建没有再被自己的 claim 拦截。
- 最终新增后台/会话聚焦 129 项与完整 pytest（100%、退出 0）通过；Ruff、
  import/offline/code-size/doc-sync/compile/diff、干净 wheel boundary/clean-package 和 fresh
  install/CLI 均通过。wheel 共 1,001 个成员，SHA-256 为
  `cd980d72e1d8e7939116152dae7188b9f398393a547823ccc79818022b71bb99`，未含运行数据或 cache。
  1.10 唯一正式 Gateway/Feishu 已部署该 wheel，active、`NRestarts=0`，8420 仅 loopback。

## 2026-07-29 普通 active turn 不再受历史进度账控制

- `tool_call_runtime` 仍是文件、命令、浏览器、PTY、LSP、派工和其他工作工具的唯一 task promotion /
  workspace binding 入口，但模型不再通过 `task_progress select/start` 控制它。当前 thread 的 sticky cwd
  自动进入本轮；精确结构化写路径可无歧义绑定同 thread 的既有目录，模糊路径、跨 task、跨 owner 或
  已有 live executor 时继续在 handler 前拒绝。
- 普通 `task_progress` 只保留 `read/update`，open item 不触发 completion nudge、后台 continuation 或
  verification hard gate。只有 exact `/goal` 的 active goal/task 同时成立时，open plan 才有耐久续跑权。
- 回归覆盖终态 cwd 上建立新 execution 并刷新 canonical workspace identity、普通 open progress 直接结束
  本轮、显式 `/goal` 继续、无 live executor 的 `/stop` 不改历史状态、live request 的 `/stop` 立即中断，
  以及内部 conversation task id 只留在 JSON 事实、不再进入模型提示。
- 全量 pytest 跑到 100% 后仅发现两条仍断言已删除旧语义的测试；修正期望后，本次相关 308 项回归全部
  通过。Ruff、import boundary、offline matrix、strict code size、doc sync、contract pyramid、
  replay 9/9、compileall、diff check、干净 artifact distribution boundary 与 clean-package 均通过。
- 1.10 MiniMax 正式链验证同一 CLI thread 的短期记忆和 idle `/stop`、可信 Feishu scope live request
  的精确 `/stop`，以及两个真实飞书客户端账号的 A/B 隔离入站和出站。A 请求
  `req_1785323824294_2105796_8` 只回复 `客户端-A729`，B 请求
  `req_1785323962358_2105796_9` 只回复 `客户端-B729`；两边 transcript、audit 和 sent receipt 均落在
  各自 owner，operation count 为 0，未产生工具副作用。

## 2026-07-28 原生大参数与重复压力 Compact 回归

- 同一真实飞书 owner/conversation/task 的 320 组 `rg` 对照长任务在已部署旧窗口上持续 200 余工具轮，
  没有复制项目或跨 owner，但在 180K 阈值后出现工具轮跳跃、错误 checkpoint 路径和重复环境定位；
  最终请求 `req_1785204963849_1599831_0` 在 6,508 秒、294 个工具轮时仍重新寻找已知 `rg`。
  真实飞书 `/stop` 将其精确结束为 `interrupted / INTERRUPTED`，该结果记为 Compact 连续性失败样本。
- 根因回归直接构造工具结果只有 `written`、但每个 `write_file` 参数含 10–12K 内容的原生历史。
  修复前字符口径漏算参数；修复后完整模型输入在 90% 前被整对收敛，最新状态和 UserTurn 保留，
  preflight 不再返回 `context_overflow`。
- 第二组回归在同一 params 上再次加入十轮大写入并第二次跨阈值，验证旧调用退出、最新调用保留、
  唯一窗口标记、无孤儿以及第三次无新增输入时不重复裁剪。文字协议与 provider overflow 最终保险
  保持既有行为。

## 2026-07-27 长任务工作区续接原子化

- 真实飞书同一会话的长任务复验暴露：项目本身 63/63，但后续纠错轮偶发回到旧任务目录，写入被正确
  拒绝。根因不是模型判断，而是消息、摘要、通道或后台 observation 使用较早 thread 快照整份回写，
  可能覆盖刚更新的 sticky workspace。
- ConversationStore 的这些投影入口现已统一为“锁内读取最新记录、只更新所属字段”。任务索引、
  `workspace_task_id`、Compact checkpoint 和其他并发状态不会再被迟到写入回滚。
- 工具公共入口继续只接受结构化路径事实：绝对路径以及 durable `owner_home` 下规范的 `tasks/...`
  可在同一 thread 唯一选择旧 task；普通相对路径、路径跳转、歧义或跨 task 仍保持 fail-closed。
- 新增旧快照回写与 owner-relative 精确重绑回归；会话、工作区、控制和 Compact 相关 137 项定向测试
  通过，完整 pytest 跑到 100% 且退出 0。部署 wheel
  `8efa97f7ada56d1046c139078118473837bc6ccf2f3e89b84fc1244738009b04` 的 distribution boundary 与
  artifact clean-package 通过，1.10 正式 Gateway/Feishu active、`NRestarts=0`。
- 同一真实 Feishu thread 的 `pyripgrep` 工程没有新建或复制项目。306 工具轮请求
  `req_1785151939712_1489561_4` 和 147 工具轮请求 `req_1785174187665_1569565_0` 虽然都继续到终态，
  但 93/40 次 live replacement 是 Compact 抖动证据，不是成功证明：末次只从
  `180026→179626`、`180989→178570`，没有获得有效余量。
- 当前修复候选移除该 live replacement，恢复回归前统一工具窗口；文本工具历史按固定窗口保留近期部分，
  原生工具调用和结果整对回收。持久 transcript 的 200K×90% 自动 Compact、checkpoint 和 raw archive
  保持不变，不恢复 task compact 或子代理专用 Compact。
- 后续独立验收发现的深度边界、JSON 和清理问题都沿同一 conversation/task 纠正。最终源码测试
  64/64，独立系统 `rg` 黑盒对照 28/28，fresh install/导入/CLI 通过；唯一 wheel SHA-256 为
  `5722fa23b75ee403f48ad1b8b31f741cab9fe436c5bbebab689491f8d760b855`。远端项目无
  `.pytest_cache`、`__pycache__`、pyc、egg-info、build、work 或 symlink。

## 2026-07-27 Compact 后的工具事实一致性

- 工具归档保留有界 `model_summary`，按结构化策略生成并脱敏；持久会话 Compact checkpoint 和恢复状态
  优先使用这份投影，不再重新从任意工具正文或自然语言猜一次摘要。
- 每条工具记录保存原本就存在的 round/index，`tool_search` 加载的 deferred Schema 因而只对尚未消费的
  下一模型轮有效；历史搜索不能在 Compact 后重新扩大工具表。
- 该投影只保存模型所需摘要，完整工具原文仍在当前 owner/task artifact。外部数据不会因为 Compact
  变成可信指令，operation、failure stage、handler 是否执行和 artifact refs 继续来自结构化记录。
- 相关 archive/reducer、工具范围、子代理 Compact 与恢复回归已通过；最终完整发布结果以本轮
  PRODUCT_FACTS 和发布制品门为准。

## 2026-07-25 工具失败分层与真实 owner 权限回归

- 新增聚焦回归逐层断言 protocol、authorization、validation、runtime_gate、execution、
  effect_reconciliation、persistence，同时断言 `handler_executed` 和非负 `duration_ms`。相同事实还
  覆盖协议 envelope、stage trace、audit、tool index、runtime ledger、compact/recovery、长输出
  record/artifact/index 与幂等重放。
- Schema/native/MCP、path/effect gate、文件/命令/PTY、idempotency/timeout、owner privacy 和
  `apply_patch` 邻接测试已通过；900 余项工具相关回归跑到 100%。本地 8899、1.10 Linux/bwrap 与
  MiniMax-M2.7 均做了真实 CLI 调用，不以 mock 成功代替运行边界。
- MiniMax 长链为 16 轮、21 条工具记录。独立复制核对缺文件、危险根、非零退出、超时效果未知、长输出
  外置、写/改/补丁与 owner 写边界；timeout 没有自动重试，越界文件不存在。Qwen 在 1.10 补丁语法上
  需要 7 次自纠正，MiniMax 第一次成功，该差异如实保留。
- 两个真实飞书客户端分别沿既有 owner/conversation 执行工具长链。A 请求
  `req_1784986368980_1301799_1`，B 请求 `req_1784986266050_1301799_0`；后者暴露中央路径门漏传
  owner scope、拒绝虽安全但落到 handler 的缺口。修复后真实 B 客户端请求
  `req_1784987075421_1304872_0` 精确断言
  `PATH_CROSS_OWNER_BLOCKED/runtime_gate/handler_executed=false`。
- 全量套件首次运行还发现统一 owner scope 误伤 runtime 显式授权的临时/CLI workspace。修复后的回归
  同时断言：结构化 `workspace_roots` 外部项目可用，custom dangerous root 仍在实现前拒绝，cross-owner
  仍返回专用错误码，admin-grant 路径不会因 workspace 例外放行。
- A/B 最终只有各自 `output/feishu-A` 或 `output/feishu-B`，无交叉文件、逃逸文件、Persona/Memory
  改写或出站协议泄露。最终完整 pytest 到 100% 且退出 0；Ruff、import/offline、strict code-size、
  doc-sync、compileall 与 diff 同轮通过。worktree clean-package 正确拒绝 84 个保留项，并独立报告
  数 GB 运行数据。
- 最终 wheel `e7a77182df0e79d9e8dda08d296d06017b3a6e19969539cbae63509faa468a1a`
  的 distribution boundary 与 artifact clean-package 均通过，已精确安装到 1.10 唯一正式服务环境。
  Gateway/Feishu active、`NRestarts=0`、8420 loopback、队列为空且 WebSocket connected。

## 2026-07-25 工具结果投影与归档再进入回归

- 当前等价主链为 `ToolRuntimePolicy.output_policy -> canonical ToolResult -> tool-context reducer`；这是模型可见工具结果的唯一
  合同。测试覆盖 spec 基线、handler 只可收紧、runtime/source-code 与 external/default 合并、
  外部边界标签中和、结构化/文本凭据脱敏，以及错误/状态/archive ref 仍留在可信结构层。
- 同一投影已覆盖 live prompt、外置 preview、compact semantic input、机械恢复、父子代理 shared
  context、recent handoff；完整原始正文只在 owner/task artifact 中保留。`read_artifact` 固定为
  external data；`read_file/search_text` 读取 `work/blobs/tool_outputs/` 的 JSON 或纯文本成员时按
  结构化路径收紧，广域搜索只有实际命中该目录的结果才收紧。
- 聚焦 redaction/Registry/reducer/externalizer/compact/filesystem/MCP 回归通过。本地 8899 Qwen 的
  `MCP -> list_files -> search_text -> read_file` 四轮链和 MiniMax-M2.7 的 `MCP -> read_artifact`
  两轮链都准确提取首尾事实、拒绝归档中的伪指令，且没有产品文件或外部副作用。
- 1.10 双真实客户端复验覆盖 `web_fetch`、归档再进入、首次路径失败后的结构化恢复及最终投递。A 请求
  `req_1784968159181_1290822_0`，B 请求 `req_1784969134442_1290822_1`、
  `req_1784975003351_1290822_2` 与修复后 `req_1784975858195_1299659_0` 均使用原 owner/conversation。
  最后一项在桌面端可见 5 条进度/说明和 final，证明 progress/final 幂等键已从 request 级错误身份收敛为
  每个逻辑消息的稳定身份；测试未执行写文件、命令或主动发信。
- 最终完整 pytest 到 100% 且退出 0；68 项通道/投递聚焦回归、Ruff、import boundary、offline
  contract、strict code-size、doc-sync、compileall、diff、distribution boundary 与干净 wheel artifact
  gate 均通过。最终 wheel SHA-256 为
  `5564877d0cebc8ffcb60139391740c705588ce6825cf0af4cf9f6bdfd914928c`。

## 2026-07-24 工具参数来源归档发布

- 统一 Registry 参数入口已增加逐字段 `input_sources`：只记录 JSON 路径、来源类别和结构化
  `source_ref`，不复制参数原值。模型输入、安全默认值与可信 run/write/workspace 补参因此可以在
  gate、工具结果和恢复归档中区分，但不能凭归档内容扩大权限。
- `tool_call_archive_record.py` 的显式白名单同步保留 `input_sources`、既有 `input_coercions` 和
  不可逆 `input_facts`。三者都只含字段名、类型、引用或 hash；命令、正文、凭据和参数值不进入 compact
  envelope。任意工具私有字段仍默认丢弃；同一来源白名单还进入短输出 index 和长输出
  record/artifact/index，重启后不依赖内存对象。
- 聚焦 provenance/archive/Schema/Registry/MCP/native/shell/PTY/artifact 回归已通过；完整 pytest、
  Ruff、import/offline、strict code-size、doc-sync、compile/diff、distribution boundary 与干净 wheel
  artifact gate 同轮通过。本地 8899 Qwen 的真实漏参 Shell 账目已同时出现两条 safe_default 和一条
  trusted_context；macOS 无 bwrap 时工具在实现前拒绝且没有写文件。短输出无 artifact 时的错误读取
  提示也已删除，复测不再出现 `ARTIFACT_NOT_EXTERNALIZED`。随后 Qwen 与 MiniMax-M2.7
  分别完成真实只读工具调用，标记、源文件 hash 和工作区文件清单均独立核对通过。
- `df00ec6af8acdf92197a0c28a9889e315943b94c` 和 wheel
  `e095a083a6ab07b87171893d75a9f31a6486534d6466b86173db304f42616d50` 已分别进入远程
  `main` 和 1.10。真实飞书 A/B 请求 `req_1784884678795_420766_0` /
  `req_1784885085848_420766_1` 各只有一条成功的 `run_command` 工具记录；参数来源完整、无原值，
  owner index 不交叉，任务目录数量未变化，最终用户正文无绝对路径或内部协议。

## 2026-07-23 工具参数合同收敛

- 当前 `ToolModelSpec.input_schema` 是外部/MCP 完整 Schema 的唯一权威；旧 builtin 字段和 compiler 已删除，
  provider definition、文本/native 入口、参数 gate 和最终 handler 前执行消费同一结构。
- 参数入口先做有限、无歧义的类型纠正，再完整检查必填、类型、枚举、嵌套、额外字段、长度/数值边界、
  组合规则和本地引用。结构问题发生在路径/effect/审批/真实工具实现前，错误证据不保存原值。
- 外层协议字段与工具参数按 typed envelope 分层；Schema 声明的 `kind/run_id/status/metadata/
  artifact_refs` 保持工具参数身份。限流、重复保护与执行使用同一 Schema 感知参数哈希。
- MCP 不再把完整 inputSchema 压成浅层 properties；畸形或未支持 assertion 不注册，合法嵌套参数在
  client call 前经过相同门。旧 flatten、unknown-parameter 独立门和重复 protocol 参数过滤已删除。
- 纯合同、registry hot path、text/native、MCP 与协议同名参数回归已通过；本地 8899 和
  MiniMax-M2.7 的隔离真实工具失败恢复链也均通过。完整 pytest、Ruff、import/offline/code-size/
  doc-sync、compile/diff、distribution boundary 和干净 wheel artifact gate 同轮通过。

## 2026-07-24 工具范围与真实通道复验

- 当前 run 的 `ToolRuntimeSnapshot` 已成为目录、推荐、原生 Schema、`list_tools`、
  `tool_search`、`list_capabilities` 和最终执行的共同事实源；未授权名称不进入搜索结果或能力自述，
  已授权但掉线的实现只能报告 unavailable，执行前仍二次复检。
- MCP 恢复只在新 run 边界执行，聚焦回归覆盖已退出进程、首次启动失败、目录变更、重连退避和
  builtin 保留。availability 查询本身没有启动进程、浏览器、LSP 或网络请求。
- 改动对应的 Ruff 与 MCP/runtime-scope/capability/browser 聚焦测试通过；浏览器跳过项仅来自当前测试
  宿主缺少可选运行依赖。全部正式注册工具均已映射到各自直接合同测试；真实副作用测试按隔离目录、
  bwrap、临时进程、只读网络或显式 grant/revoke 运行，不用一个“调用成功”冒充所有工具语义。
- 1.10 本地模型的真实飞书平台请求在错误 owner/date 路径上分别得到
  `TOOL_INVALID_ARGUMENTS/PATH_NOT_FOUND/WRITE_FORBIDDEN`，没有越界写入；随后只在当前 owner 原 task
  写出唯一文件并读取核对。`/btw` 在同一 request 仅消费一次，用户 transcript 无工具协议块。
- 切回 MiniMax-M2.7 后，两个既有 Feishu owner 并发只读工具账本分别为
  `list_capabilities/list_tools/read_file = true/true/true` 和
  `list_tools/read_file = true/false(TOOL_INVALID_ARGUMENTS)`；A 文件 SHA-256 在 MiniMax 只读轮前后不变。
  后一轮是正式 Gateway 的 Feishu scope 请求，不等于又一次平台客户端消息。
- 最终 wheel
  `e07e9b9ec75d846be683c6b3a711c88e1044690d00f90be0b631976309f65acd` 通过 distribution boundary 与
  artifact clean-package 并精确部署。部署后同一正式 Feishu owner/conversation 的只读请求
  `req_1784828035414_319820_0` 实际调用 `list_capabilities/list_tools` 均成功，工具索引和私有审计记录
  一致；没有文件、Memory、消息或其他副作用调用。
- 最终 fast/slow pytest、架构守卫、Ruff、compileall、import/offline/strict code-size/doc-sync 全绿；
  worktree clean-package 仅拒绝明确保留且未进入 wheel 的运行 `data/` 与 handoff 文档。1.10 正式
  Gateway/Feishu 均 active、`NRestarts=0`，8420 只监听 loopback，队列为空且 WebSocket connected。

## 已完成

- 新增 owner-local SQLite 验证事件与当前状态投影。
- 从项目真实 manifest 发现规范测试命令，按精确 shell token 识别 targeted/full。
- 在主代理和子代理共用的工具执行入口记录 `run_command` 结果。
- 成功的 `write_file`、`edit_file`、`apply_patch` 会使同根任务的旧证据 stale。
- 工具 live context 和归档保留精简结构化验证事实，用户回复仍由模型自然生成。
- 共用工具归档新增白名单式 `delivery_evidence` 压缩：只保留成功、当前 owner、带 receipt 的消息送达事实；
  它与验证证据一样由真实工具结果产生，并可携带不含正文的 `evidence_refs`；用途仅是 scheduled source
  reply 去重和 Audit reported 对账，不改变测试通过状态。
- 共用工具归档对白名单增加 `tool_search.loaded_tool_names`。它只证明本轮真实搜索结果让哪些已注册工具在
  下一模型调用可见，不保存检索正文、不授予能力，也不改变验证通过状态。
- 工具共用出口现在携带 run 开始时固定的 `ToolRuntimeSnapshot`；模型看到的目录/Schema、真实搜索结果和
  最终执行使用同一 `allowed_tools + owner policy + availability` 交集。执行前 readiness 复检发生在
  验证记录之前，不可用工具不会运行实现、不会生成虚假成功证据，也不会产生业务副作用。

## 解决的问题

- 子代理跑过的测试现在按结构化 `root_task_id` 归到同一任务树。
- 任意命令、链式命令、失败写入不能伪造或污染验证状态。
- 不同 owner 使用不同数据库，不会互读验证记录。

## 下一步

- 后续若启用 PostgreSQL scale profile，再按同一 schema 迁移 owner 数据，不改变语义。

## 已跑测试

- project facts：Python/package manifest、full/targeted、任意/链式命令拒绝。
- repository：passed/failed/stale、不升级 scope、owner/task 隔离。
- runtime：结构化根任务归属、写后过期、失败写入不改变状态。
- live reducer：结构化事实进入模型工具上下文，原工具输出不被改写。
- archive/finalization：消息送达证据按 receipt 去重，公开工具输出不含 owner 路径，失败/伪造 envelope
  不能升级为已投递；scheduled transcript 镜像幂等且不会二次调用通道。
- progressive disclosure：只有成功 `tool_search` 的结构化结果进入归档；普通模型文字、直接猜工具名和
  不在当前 registry/policy 内的名字不能伪造下一轮可见工具集合。
- runtime scope：受限 `list_tools/tool_search` 不泄露未授权名称；快照后新就绪工具不在本 run 扩张，
  快照后掉线工具在实现前返回 `TOOL_UNAVAILABLE`；视觉/LSP/浏览器/MCP availability 检查不启动资源。

## 本轮发布门

- 2026-07-23 的工具范围发布已完成定向回归、普通 CLI、本地 8899、MiniMax-M2.7 与 1.10 正式
  Feishu 双 owner 真测；实际部署 wheel `fd9e6a1c…adcfd` 的 distribution boundary 与 artifact
  clean-package 通过。真实工具记录与保护文件哈希复核没有发现额外副作用。
- 完整 pytest（含单独 slow）、架构守卫、Ruff、compileall、import/offline/code-size/doc-sync、
  distribution boundary 与干净 wheel artifact gate 已通过。
- worktree clean-package 因保留的未跟踪运行 `data/` 正确失败；实际 wheel 无发布阻塞项。
- 1.10 两个 Feishu-scoped owner 的真实长任务已执行：Chi 在 MiniMax 阶段完成，Chalk 在供应商不可用时
  立即切到本地 8899 并沿原 task 分阶段完成；最终产物不采信模型自述，分别复制到本机独立验收为
  51/51 与 42/42，Chalk 另完成 fresh install 和关键行为断言。
- 最终整套 pytest 收集 8,172 项并以退出码 0 完成；门禁发现的 fake 心跳旧参数和未注册
  `MODEL_INCOMPLETE_RESPONSE` 均按通用合同修正。最终 wheel `4b882778…bdcb` 通过 distribution/artifact
  clean-package 并精确部署；worktree clean-package 仍只把保留的未跟踪运行数据/handoff 作为 error，
  这些内容不在 wheel 中。

## 2026-07-19 精确 workspace 重绑定与错误分类已验证

- 统一工具入口在执行副作用前可依据同 owner、同 conversation 的唯一精确绝对写路径选择 workspace。
  主代理仍走全局 task select；子代理只写 host 生成的 runner-local rebase 事实，不能改变父 task link。
  `write_boundary_with_runtime_ledger` 只在该 rebase 的 task id/root 与当前结构化 workspace 完全一致时，
  把 child 写根切到这个精确任务；没有 marker、marker 漂移或 owner/task 根非法时继续 fail-closed。
- `capability_request` 的缺参、root 不允许调用和 run 不存在现在都有注册错误码；tool archive、verification
  和模型恢复逻辑不再看到缺失分类后生成的 `UNKNOWN_ERROR`。
- 精确重绑定后仍由同一个 `execute_traced_tool_call` 执行并记录真实最终 payload/result，没有新增 IM
  hook、旁路执行器或第二套验证账本。聚焦回归、完整本地 CI、制品门和 1.10 双 owner/四 child 反证
  均已完成；四个 child 全部 `DONE` 且没有改变父 task link。

## 风险

- shell 内部自行改文件不经过文件工具时，当前版本不会自动标记 stale；最终发布测试必须在最后一次代码修改后执行。
- 项目没有声明可识别的规范验证命令时保持 `unverified`，不会猜测。

## 2026-07-24 副作用幂等验证与发布

- 新增权威 `tool_operations` 表与统一 coordinator；runtime gate ledger 保留审计用途，但已删除
  `runtime_idempotency_ledger` 投影和旧 `tool_idempotency_ledger` gate，执行权只剩一处。
- 定向验证覆盖 SQLite 重开后的精确成功/失败重放、相同参数的新操作仍可执行、同 operation 改参数拒绝、
  线程并发只进入一次 handler、同/跨 owner、显式 business key、活/死/远程 holder、lease 到期、错误
  holder completion、终态损坏和存储前后故障。副作用结果存储失败会标为 unknown，不会把已发生的动作
  伪装成可安全重试。
- 裸 Registry 默认 fail-closed 后，所有直接 Registry/ExecuteRegistryCall 合同测试已逐个复验；确实只
  验证工具本体或 dry-run 的测试显式声明不需要 operation store。正式 agent 与真实通道不使用该豁免。
- native provider call id、文本回落 call id、同名工具参数、task_progress、cancel、MCP、channel message、
  文件/shell 与 tool-search/availability 受影响测试均已通过。完整 pytest 到 100% 且退出 0；最终一轮
  还发现并修复 6 个 owner 同时首启时全局 Scheduler SQLite 切 WAL 的真实锁竞争，复用既有通用
  busy-timeout/退避实现后，专项测试和 10 轮并发隔离复验均通过。
- 全部静态门与干净 wheel 制品门通过；8899 Qwen 和 MiniMax-M2.7 各自实际产生一条唯一的 succeeded
  `write_file` operation 并回读唯一标记文件。提交 `9f03140e` 和精确 wheel
  `f702784aa7a548261ff0d164beae836f00aadf1d2d2fcbc698aae2cd959e448b` 已分别进入远程 `main` 和
  1.10。
- 两个既有真实 Feishu owner 的并发正式请求各产生唯一 `write_file/send_message` operation，文件和
  sent receipt 均按 owner 分离；B 对 A 文件的实际 `read_file` 被 `TOOL_INVALID_ARGUMENTS` 拒绝。
  安装版精确重放/输入冲突 smoke 也确认 handler 只调用一次。请求入口是可信 localhost Feishu scope，
  消息真实送达 Feishu；因 macOS 锁屏，本轮没有重新取得两个桌面客户端的入站证据。

## 2026-07-25 工具超时未知态、核对与真实消息复验

- 通用 operation coordinator 现在区分 `failed/not_started` 与 `unknown`：mutating/dangerous 工具返回
  timeout 或显式 `effect_outcome=unknown` 时，原错误码只作为 `reported_error_code` 保留，权威状态写为
  `TOOL_OPERATION_OUTCOME_UNKNOWN`。同 operation、同业务键或换模型 call id 都不能再次进入 handler。
- 工具可选实现只读 `reconcile_operation`。只有目标系统返回
  `succeeded/failed/not_started + source_ref` 才能收口；`not_started` 通过 SQLite generation CAS
  原子重开，两个核对者并发最多一个获准执行。核对证据在重开占位和最终结果中持久化，后续重放不会
  丢失。无核对器、核对异常、非法 outcome、缺 source_ref 或保存竞态都保持 unknown。
- Feishu 文本、引用回复、长消息分片和媒体消息使用由可信 DeliveryContext 派生的稳定 UUID。只对
  408/429/500/502/503/504 与传输错误做 1/2 秒有界重试，且同一内容/分片所有尝试复用同一 UUID；
  4xx 明确错误、无 UUID 请求和媒体上传本身不盲重试。
- 聚焦 85 项覆盖精确重放、输入冲突、线程并发、死/活/远程 holder、lease、store 故障、timeout、
  not-started、成功/失败核对、缺证据、核对并发、跨 run business key、owner 隔离、Feishu UUID、
  通道附件与后台投递。真实 timeout 没有在正式飞书上故意制造；那会把用户可见副作用置于不确定状态，
  该分支由无外部副作用的 fake transport 和 SQLite 合同测试验证。
- 候选 wheel 下，本地 8899 的两个既有真实 Feishu owner 请求
  `req_1784936863406_1278006_0`、`req_1784937016519_1278006_1` 各只有一条 succeeded
  `send_message`，generation=1、retry_attempts=0，owner、正文和 receipt 分离。A 的 Feishu 历史 API
  反查精确正文为 1 条。
- MiniMax-M2.7 下，B 请求 `req_1784937145845_1278720_1` 同样只有一条 succeeded operation。
  A 首轮 `req_1784937117671_1278720_0` 没有工具记录，却由模型在正文里误称已发送；operation 账本和
  Feishu 历史 API 都证明实际精确正文为 0 条，没有把模型自述升级为副作用事实。沿同一 owner/thread
  普通中文纠正后的 `req_1784937202165_1278720_2` 产生唯一 succeeded operation，Feishu 历史精确正文
  为 1 条。B 的既有 conversation id 不是 Feishu 可查询的 chat container，故 B 的外部证据边界是
  open_id 发送 API 成功与 owner operation ledger，不冒充历史反查。
- 本地与 MiniMax 轮前后，两个 owner 的 SOUL、USER、长期 Memory 哈希均未变化。正式 Gateway/Feishu
  保持单实例、active、`NRestarts=0`、8420 loopback、队列为空，最终配置恢复 MiniMax-M2.7。
- 最终完整 pytest 到 100% 且退出 0；Ruff、compileall、import/offline、strict code-size、doc-sync 和
  diff gate 均通过。worktree clean-package 正确拒绝 83 个保留的未跟踪项，并明确报告 `data/`、
  `live-agent-runs/` 等大体积运行数据；这些内容不得进入最终 wheel。

## 2026-07-25 多外部写部分结果发布

- 没有新增跨工具 Saga、自动补偿或第二执行账本。同一模型轮仍按原顺序逐项调用；一个失败不会在没有
  typed 依赖关系时机械阻断独立后续调用，每项 operation 结果分别留痕。
- 修复了“handler 回报成功、权威 completion 写入失败仍返回 `ok=true`”的问题。现在统一降级为
  `TOOL_OPERATION_OUTCOME_UNKNOWN`，保留脱敏 `reported_ok` 旁证，同 operation 的后续调用仍由原 claim
  阻止，不能因为模型换说法或重试而再进 handler。
- archive、control-plane event、模型 tool context 与 compact 续跑共用 operation/effect 字段；语义
  compact 对中段非成功副作用保留精确事实块。聚焦回归以及完整 pytest、Ruff、import/offline、
  strict code-size、doc-sync、compile/diff、distribution boundary 和干净 wheel artifact gate 已通过。
- MiniMax 极端 CLI 首轮发现旧 escape-relocate 会把未授权绝对路径静默搬进任务 output，安全墙没穿透
  但返回语义失真。已按 会话运行时/长期助手 的真实路径身份做法删除该分支及专属死代码。修复后 run
  `run-1784955984463073000` 精确得到 `succeeded / failed(WRITE_FORBIDDEN) / succeeded`，三项
  generation 均为 1；原绝对目标和 output 下替代目标都不存在，两个合法文件内容独立复验正确，模型
  也准确报告中间失败和失败后继续成功。
- 本地 8899 基础 CLI run `run-1784955312577204000` 完成 3 写 3 读，MiniMax-M2.7 长链 run
  `run-1784955411428676000` 完成 6 个阶段文件、manifest、summary 的 8 写 8 读；两轮写 operation
  均 generation=1，独立文件内容检查通过。
- 运行时代码提交 `2bd8622f` 的精确 wheel
  `01ab7d15df60b1db613e7d52e211ce46bb3fe04bb52b7a0ccb43dd835b587614` 已部署到 1.10。
  既有 Feishu owner A 请求 `req_1784957378024_1282076_0` 的三条写账本为
  `succeeded/failed/succeeded`；绝对 `/tmp` 目标不存在，合法前后文件内容准确。既有 owner B 请求
  `req_1784957456380_1282076_1` 的跨 owner 读取在实现前拒绝，随后自己的写入和回读成功。A/B 的
  `req_1784957557622_1282076_2` / `req_1784957582493_1282076_3` 又各自产生唯一 succeeded
  `send_message`，generation=1；Gateway 原生通道日志和 sent receipt 证明分别发往各自 open_id。
- 两项服务复验 active、`NRestarts=0`、8420 只监听 loopback、队列为空。本轮 Feishu 请求来自可信
  localhost scope，真实出站不等于新的客户端入站；macOS 锁屏阻止桌面入站补证，因此文档明确保留这一
  外部验收边界。
