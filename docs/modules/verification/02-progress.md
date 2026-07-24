# Verification：开发推进

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
