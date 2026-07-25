# Verification：开发推进

## 2026-07-25 工具结果投影与归档再进入回归

- `ToolSpec -> ToolExecutionResult.tool_output_policy -> tool-context reducer` 是模型可见工具结果的唯一
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

- `ToolSpec.input_schema` 是外部/MCP 完整 Schema 的权威；builtin 旧字段只在一个 compiler 中转换，
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
  它与验证证据一样由真实工具结果产生，但用途仅是 scheduled source reply 去重，不改变测试通过状态。
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
