# Verification：开发推进

归档信封白名单（2026-09-24 晚）新增 `observation` 与 `observation_rejected`：只读插件工具的观察候选记录由 `plugin_observation.parse_observation` 按形状与数量夹界后写进 `tool_result_envelope`，是候选内容的唯一权威；`tool_completed` 事件只带查找投影。验证账、副作用证据与其它白名单字段不变。

后台Compact已本地接同一scope/view的摘要注入和精确覆盖，局部来源/提交不改全线程摘要和游标；18文件联合420项通过，最终验证见TESTS。此片不证明完整恢复payload，Gateway/child准备同view、初次/手动和真实缓存仍待验；唯一TODO的12.4保持未完成。

Compact来源覆盖现沿实际执行的run/attempt/模型turn/call四元身份；归档不再用当前runner覆盖原ToolCall。原摘要检查点v3封印scope/base/精确覆盖，旧未知身份保留。只验证来源和检查点合同，完整后台摘要应用与HTTP恢复仍待接。

## 2026-09-24 可选交付复核焦点（本地分支 `claude/decision-delivery-quality`，待审）

`delivery_quality` 只读同 run/task 归档信封中的验证事件与 stale 状态，默认关闭；原 owner/thread 设置、配置工具与 TUI 共用
独立模式、超时和模型绑定。开启后 `run_command` 新事件使本轮有 2—12 个焦点且含 failed 或其后修改时，才请 Jev 选一个复核焦点，
text/native 共用一句宿主事实提示；原结果、归档、验证账和收口不变。离线合同、真实验证账到 `_record_tool_call` 的 fake 后端组合
与 59 项变异验证见 TESTS；没有真实 Jev/TUI 验收，不证明交付质量提升。

## 2026-09-22 可选外部材料阅读优先级

`external_material_order` 仅消费原 `web_fetch mode=extract` 已归档页，默认关闭；原 owner/thread 设置、
主代理配置工具与 TUI 共用独立模式、超时和模型绑定。开启后按原安全投影发送有界摘录与当前问题，
text/native 共用附加提示；原页、失败项、refs、下载顺序、账本与权限不变。

定向覆盖原页面生产/Executor/归档、设置与 worker 用量、来源失效和超时取消边界。真实页面链仅替换
网络响应，真实决策链仅替换供应商；尚无真实任务证明质量或效率提高，不作为验收门。
联合定向 349 项通过；原 TUI pipe CAS 的固定短等待改为真实绘制状态的有界等待，仅修改测试 helper。
命令、结果及两次原时序失败证据见
[P5-C 首片交接](../../tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。
第8步索引恢复补齐：externalizer 保存有界 `tool_process`，carried reader 恢复原 process 信封；投影唯一位于 `tooling/runtime_facts.py`，旧索引不推定清理成功。组件验证与真实 TUI 分开。

第8步逐调用 Compact 来源修复：归档索引显式保留实际 canonical call 的 run/attempt/turn，当前 runner 不再改写来源。精确已压调用隐藏、跨 request/attempt 同号保留、混合来源、旧未知保留及同 ID orphan 覆盖均有定向因果测试；未知身份不会被升级为执行或过滤成功。详见 [独立交接](../../tasks/HANDOFF_STEP8_COMPACT_CALL_REFS.md)。

第8步本地补齐工具清理事实：当前结果与跨工作片恢复共用runtime_facts，命令成功和资源清理不互相推断；fake executor到native及MCP来源负例通过，与唯一循环入口16文件组合349 passed／20项既有xfail；真实TUI待组合包。

## 仅思考续跑保存

真实成功响应生成约 3.7 万字符思考但未给正文/工具，旧空响应重试丢弃了该轮历史及正常用量结算。
已保留 typed 思考、签名/密文，并在零工具 continue 前追加独立原生轮；未执行工具块不回放。
联合定向 455 passed、23 项既有 xfail；真实 SSE 37,118 字符及原用量复放保留。
新正常 TUI 16 模型轮/22 组工具自然结束，8 项产物测试及原始/当前样例独立复算通过；
新慢 TUI 21 请求、30 组工具，20 个完成响应思考逐字保留，但约 30 分钟仍无产物变化，
已由测试者取消留证。该新会话未自然触发仅思考分支，不能把离线回放称为该分支真实模型通过。

## 渠道失败诊断分类

真实思考模式慢任务中，两个普通命令非零结果触发“系统失败、换渠道”引导，和原排错目标不符。
已按 canonical 错误分类过滤，只累计网络可重试/能力不可用；同一 tool/call_id 只消费最新回执。
参数、状态、权限、取消和未知失败均保留原结果而不加该提示，339 项相关定向通过。
真实新包正常 TUI 66 组工具/回执完整、无错误渠道提示并自然收尾；8 项测试及原始/现有数据独立复算通过。
该模型仍有调整样例的绕路，效率不记通过；数据与命令工作目录按原生调用身份另行核对。
思考开启的新任务仍未交付，不能将请求字段正确、长流稳定或状态 done 当作业务成功。

## 有界工具结果完整性

真实慢模型请求中发现读取页被生产归档入口二次截成日志预览，文件尾部、文件版本及继续游标丢失。
现按归档内联/外置事实与显式保留策略选择正文；文件和归档读取维持工具自己的分页上限。
生产执行/归档回归复现旧错误并验证修复，大输出引用、脱敏和外部数据边界继续保留。
原任务单路慢模型 TUI 已确认完整尾部实际出站，但仍重复读取，未交付、不计通过。
同时移除 Shell Schema/handler 的人工字符上限；正常 TUI 执行 8,468 字符命令成功，
连续两任务独立 10 项测试及统计复算通过。慢模型相同请求完整冷算仍重复，不关闭产品缓存。

## 长时间运行验收

长等取消、届满不杀进程、通知耐久去重/重启补发、停止不重启、请求摘要隔离和有界均定向通过。
联合定向 951 passed、2 项既有 xfail。真实正常 TUI 已验完成通知、插话、Goal/Compact 连续任务；
产物独立 18/21/51 项测试通过。自然收尾后的补报入口被旧终态检查误挡，修复后真实采样自动报告。
慢模型一路、不派子代理；长流可导航/输入/中断，但编辑冲突及成功 Shell 循环使长任务未通过。
按 [长时间运行](../../design/LONG_RUNNING_EXECUTION.md) 区分传输稳定、模型行为和产物质量。

## 受管后台进程的稳定进展观测

真实慢模型出现 33 次相同 wait，只有 uptime 变化，旧完整输出哈希因此从未给成功重复提醒。
status/wait 现在由宿主另算进展指纹，排除耗时和等待届满，保留状态、退出码、日志尾部与字节数。
仅供软提醒，原始摘要和执行前动作门不变，不把进程沉默当死亡；定向与真实 TUI 分别验收。
相关联合 155 项通过；新包正常/慢模型两路真实 TUI 自然完成，独立产物 11 测试与金额复算通过。
正常模型的内存结论外推仍不成立：十万行测试不能证明百万行低于同一阈值；不为此恢复机器完成门。

## 六路真实组合验收

单 Gateway、独立 owner 同时跑四路正常模型与两路本地慢模型。Goal 的 task/request 混淆在全新派工中复现，
底层修复后原现场完成，同会话第二批孩子也自动回到主代理并完成。输入/目标联合定向 313 passed、2 项已有 xfail。
自动/手动压缩与追加任务已实测；健康慢流持续约 45 分钟，能翻页、输入及中断。
失败仍保留：慢模型 schema 编译、单槽排队、成功 Shell 复读、产物截断以及用量漏记/重记。
测试者未代写任何业务产物；过程失败、链路完成与内容质量各自记录，不相互替代。
追加追问复现消息编号误作 run 的工具拒绝；成对绑定补修后相关两组定向 574 passed、2 项既有 xfail。
原仓库会话恢复并实际测试 14 项通过；另一会话等待两名孩子时追问能执行命令。自定义隔离身份通道
会脱敏正文路径，不能由本轮 basename 直接推断普通本机 TUI 的绝对路径输出坏了。

## 重复拒绝不再伪装成进展

真实成功 Shell 循环在原门拒绝后又放行：自身拒绝被记作另一失败类，且执行前哈希查找遇任意失败即清空。
现只排除两类精确门码且未执行的拒绝，不扩大到真实工具失败或其它权限拒绝；完整回执仍在工具账。
重复拒绝不挤占有界观测窗口；模型回执补上原计数、未执行事实和换路提示。文本解码失败统一说明能力发现。
阈值、正常变化和写入后的读取、权限及 UNKNOWN 边界不变；相关定向与真实 TUI 分开记录，不宣称模型永不复读。

## 多文件补丁的实际文件交接

追加真实任务发现补丁成功后仅返回显示名，通用归档没有 artifact_refs，子代理自然收口漏掉修改文件。
补丁现以预检绝对路径和已提交目标生成结构化引用；移动/删除有显式墓碑，部分失败只登记已提交部分。
统一归档消费 refs 并按当前文件事实落原 registry；不从成功正文推导，不改变审批、执行终态或恢复权限。
同一回执同路径状态去重，旧不同 artifact_id 的同名 ready 不覆盖新删除；定向回归与新包 TUI 分开验收。

## R184 删除业务目录执行门（focused 与跨目录真 TUI 通过）

工具主链删除 `conversation_workspace_execution_blocker`、写路径驱动的绑定失败门及参数重写层。
本轮停止的 active-turn transition、权限、操作幂等、typed handler 结果和被动验证保留；不恢复机器质量验收。
FT-158/160 两份旧业务文件直接修改，未再出现路径回绑或改写导致的错误目标。FT-161 模型遗漏恢复文件、
把已恢复说成从未误改，仍按真实内容失败登记 BUG-115；运行终态 DONE 不作为内容正确的证据。


## 2026-09-01 工具批准只认 exact applied approval（本地候选）

- 旧 archive 投影可能把 handler 私有 metadata 中类似 approval 的字段当成“用户已批准”，让后续
  current-turn/Compact/恢复看到未经权威控制面确认的批准事实。当前只有 Registry/approval gate 生成、带
  exact permission/call/operation 绑定的 `AppliedToolApproval` 能进入 canonical ToolResult 与 archive；
  handler 自报字段、模型正文和展示标签一律忽略。
- current-turn、工具归档和 tool-loop 回归覆盖合法 applied approval 保留、伪造 metadata 丢弃、批准后原
  ToolCall 原地续跑以及未知/拒绝不升级。provider stream idle timeout 同时改用 typed stage 交给外层有界
  退避，不把连接抖动包装成工具批准或任务失败。相关 focused 已通过，待 R121 真 TUI 的真实 transient 与
  approval 场景继续取证。

## 2026-08-30 首个工作工具立即使用 canonical task root

- 真 TUI 首个 `run_command` 曾在 conversation task promotion 已成功后仍沿用外层启动 cwd；第二次工具调用
  才进入 `<owner_home>/tasks/<task_path>`。根因是 mutable 外层 `RunParams` 已更新，当前工具循环冻结出的
  `ToolLoopExecuteParams.task_attributes` 仍是晋升前投影，不是权限或任务创建失败。
- 当前只在结构化 promotion 成功后，把外层权威 attributes 原位同步到本次工具快照，再由既有 cwd、roots、
  write-boundary 和 operation 链解析。模型参数、自然语言路径和 handler 结果都不能反向改写外层权威；
  取消、未创建 thread 与 promotion 失败时不做同步。
- focused 覆盖第一条命令、第一条写入和同轮后续工具落在同一 canonical task root；这不增加机器验收门，
  只修复已有工作区事实的消费时机。

## 2026-08-26 主/子代理共用验证证据与动作授权边界（第四候选）

- r38 真实任务证明 canonical 工具事实没有丢：测试机本机服务监听 `0.0.0.0:8765` 且 localhost 200，开发机
  跨机连接被 firewalld 拒绝；问题是模型把局部证据外推成更大结论。明确纠正的普通中文 follow-up 仍复现，
  因此不靠继续堆用户提示解决。
- 首版 `57e91cb` 的正确 cwd fresh TUI 证明抽象提示不够：模型先说无法外部验证，随后仍把
  `0.0.0.0 + localhost 200` 标为局域网可用，最终又要求外部电脑复核；同时擅自启服务，正文和授权都失守。
- 第二候选把同一 `Verification Evidence Boundary` 放到完整 Prompt 最末，列出跨观察点反例和只读核对边界。
  主代理和所有使用 `system_prompt_override` 的 child 都经过该入口，Compact 后续轮自然保留。
- `4247582` 部署后的 `ma-evidence-r41-readonly` 已不再把本机结果写成跨机可达，但仍擅自启动服务；因此
  “证据表述”通过，“只读行动”失败，不能整体报通过。
- 第三候选 `4796cfb` 已通过本地 gate 并部署，但 `ma-evidence-r42-tool-auth` 仍在只读核对请求中调用
  `run_command` 启动 HTTP 服务；canonical operation 为 succeeded，所以不是展示误判，必须记为真失败。
- 代码对照定位到旧宿主规则虽名为 `# System`，实际仍塞进单条 `role=user`；终端交互 使用 Anthropic 顶层
  system，会话运行时 使用独立 developer instructions。第四候选把 `model_guidance.py` 全量边界送进 provider
  真实高优先级通道：OpenAI-compatible 为首条 system，Anthropic-compatible 为顶层 system；初始 user 和
  native history 顺序不变。工具投影仍只给结构化 effect 可能有副作用的工具追加动作段，纯只读工具不变。
  不支持 system 的旧 fake 不接收新关键字，context/Compact 也不虚算该段。该规则不识别 HTTP、防火墙或
  项目类型，不解析完成正文，不写状态，也不恢复机器完成门；供应商控制由 typed request options 集中承载。
  backend/prompt/root/child/native/context focused 共 366 项，结果为 359 passed、7 个既有 xfail，严格 gate 与
  真机待办。

## 2026-08-25 后台续片不再丢失 succeeded operation

- 真机 root 首次 `create_subagents` 的 canonical 结果已经有 succeeded operation，但外置工具索引没有保存
  该字段。child 完成后的 background slice 只能恢复 `ok=true`，按现有 fail-closed 规则正确归为
  unverified；最终回复因此是 unfinished，active task link 和 TUI Working 均不会关闭。
- 当前在工具输出第一次 externalize 前注入 host-owned execution，并对白名单 operation identity/status/
  action/replay 做大小输出一致持久化。carried record 恢复现有 flat verification 字段；没有 typed status 的
  旧记录继续 unverified，绝不把所有 `ok=true` mutation 放宽为成功。
- background focused 回归已直接证明 child terminal wake 后 operation verification 为 succeeded、root link
  为 completed；externalizer 回归同时证明任意 diagnostic/private envelope 字段不会进入耐久 index。
- 后续真机证明 durable task id 与 foreground request id 不同，旧恢复范围仍会漏掉已持久化的 succeeded
  operation。当前把 exact `conversation_request_id` 从 child canonical attributes 送到 completion wake，
  background verification 只恢复该 turn；旧索引只在 row request id 同值时兼容，不放宽 fail-closed。

## 2026-08-18 Fiber 143 收口冲突返工【状态：2026-08-28 已退役】

- Fiber→TypeScript 前台最后一条 E2E `run_command` 实际完成 HTTP 请求后由清理命令收到 SIGTERM，权威
  operation 仍明确落为 `FAILED`：`COMMAND_FAILED / failure_stage=execution / handler_executed=true /
  effect_outcome=failed / return_code=143`。模型看到了完整回执，却依据 stdout 的成功片段输出“完成”；因此
  根因不是工具结果丢失，而是 completion 把冲突草稿直接切到无工具表达轮，模型没有机会修正命令并复验。
- 当时把已知失败升级为 `completion_conflict.v1` 隐藏返工，误把 会话运行时 可选 Stop hook 当成默认完成判官。
  2026-08-28 `.7` 真 TUI 用用户明确要求的 `exit 7` 证明该门会额外触发 5 次模型调用、42.6 秒和
  141,905 cache-read tokens，随后仍遗留 Working。现已删除该返工链；模型仍可在看到失败结果后的正常下一次
  采样中自主修复，但 plain final 不再被机器覆盖。UNKNOWN 副作用的执行期安全收口保持不变。

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
  workspace binding 入口，但模型不再通过 `task_progress select/start` 控制它。exact request、未结束 Goal
  或 active task 自动进入本轮；精确结构化写路径可无歧义绑定同 thread 的既有 canonical 目录，同根多代
  terminal execution 按最新一代续作。模糊路径、跨 task、跨 owner 或
  已有 live executor 时继续在 handler 前拒绝。
- 普通 `task_progress` 只保留 `read/update`，open item 不触发 completion nudge、后台 continuation 或
  verification hard gate。只有 exact `/goal` 的 active goal/task 同时成立时，open plan 才有耐久续跑权。
- 回归覆盖精确回绑旧 cwd 后建立新 execution 并刷新 canonical workspace identity、普通 open progress 直接结束
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

## 2026-08-28 首个工作工具 cwd 原子化候选

- 真实 TUI 发现首个 `write_file` 仍按 owner 根执行，而任务晋升后的 `apply_patch` 已按 task root 查找，
  形成同一会话两套工作目录。根因是会话 promotion 位于 canonical executor 的 late pre-handler gate，
  但 write boundary、policy cwd 与参数规范化已经使用旧目录冻结。
- 候选修复在唯一 traced-tool seam 先完成结构化 promotion，再同步
  `run_workspace/execution_cwd/runtime roots/rebase source` 并重建当前 call；预算、重复调用、stale runner
  与 authority guard 仍在 canonical executor 规范化后只运行一次。取消在准备前命中时不产生晋升副作用。
- 相对路径与晋升前 owner-root 绝对路径两条 focused 回归均已通过；真实 MiniMax-M2.7 TUI 复验仍是
  把 BUG-010 标为完成的必要条件。

## 2026-09-04 Shell 工具输出前像膨胀收口

- R167 单 Gateway 真 TUI 的 8 子代理长调研暴露：普通 `run_command` 会把累计 `tool_output` registry 行全部
  当作 ready 交付物复制，一条命令已出现 11 个无意义前像，owner 私有区很快累积约 29 份 manifest。
- 工具输出登记现带结构化 archive role 与 shell exclusion；旧 `kind=tool_output` 同样迁移兼容，未知产物 kind
  仍默认保护。通用 operation coordinator 在 succeeded/failed 权威落库后通知 handler 回收 crash-window
  manifest，UNKNOWN 不清；幂等 replay 再通知一次。
- shell、artifact registry、tool archive、LocalStore 幂等主链 focused 已通过。下一步部署到 `.10` 唯一
  Gateway，用 fresh MiniMax-M2.7 TUI 连续产生归档和命令，核对 snapshot/manifest 数量不再随调用数增长。

## 2026-09-25 验证命令分类：cd 前缀与管道

- **发现**：在测试机隔离真实 TUI 样本（交付复核焦点）里，MiniMax-M2.7 跑的 3 次 pytest 都写成 `cd <项目绝对路径> && python3 -m pytest tests/ -v`。验证账 `verification_events` 一条都没记：`cd … &&` 被当作两段链式命令整体拒绝。这类写法在模型里最常见，依赖验证账的交付复核焦点与交付前核对都因此看不到这些真实测试。
- **同时发现**：单个管道没有被拆段，`python -m pytest -q | head -60` 会按 `head` 的返回码 0 记成 passed，违背"一次返回码只能证明一条命令"的原设计。
- **修复**（分支 `claude/verification-command-shapes`）：
  - 按带引号语义的 shell 记号检查，未加引号的 `|`、`|&`、`&` 一律不算证据；引号内的 `|` 只是参数。
  - 只放行开头一个 `cd <可进入的现有目录> && <单条命令>`：`&&` 在 cd 失败时短路，所以返回码只可能来自后一条命令。项目根与记录的 cwd 都改用 cd 目标。
  - 其余链式写法（`;`、`||`、多段 `&&`）仍整体拒绝；cd 目标不存在或不可进入时也不算证据。
- **已知限制，未改**：`pytest tests/` 这类带目录参数的写法仍按原规则记为 targeted，即使该目录就是全部测试。
- **真实复测**（main `e489ffff0` 前的 `e7178a22c`）：同一示例两档共 6 次 `cd … && python3 -m pytest …` 全部入账（修复前为 0 次）。另外发现两处未改的口径：返回码 127（工具环境里没有 `python`）被记成测试失败；`make test && make lint` 返回 0 时整体不记。方向见设计台账。
- 回归与变异见 [TESTS](../../../TESTS.md)。

## 2026-09-25 验证分类：返回码 126/127、pytest 范围与 && 串联

按真实样本暴露的口径问题，经主线 owner 确认后实施（分支 `claude/verification-exit-scope-chains`）：
- **返回码 126/127**：按 shell 约定表示命令不可执行或找不到，验证命令根本没有运行。现记为独立状态 `environment_unavailable`，不再算测试失败；只按返回码判定，不解析输出。
- **pytest 范围按参数形状判定**：
  - 含 `::`、以 `.py` 结尾，或带 `-k/-m/--lf/--last-failed/--deselect`（含 `-kEXPR`、`--deselect=…` 连写）时为 targeted。
  - 只给目录（包括子目录）或不给路径时为 full。这是按参数形状的约定，不再逐一核对目录是否覆盖全部测试。
  - 其它生态沿用原规则，例如 `go build ./cmd/demo` 仍是 targeted。
- **`&&` 串联**：依"一次返回码只能证明一条命令"——
  - 整条返回 0 时，每一段都成功，所以逐段把其中的规范验证命令记为 passed；非验证段（如 `echo`）不记。
  - 返回非 0 时无法归属到哪一段，整体不记。
  - `;`、`||`、管道、后台仍整体拒绝。
  - 信封里 `verification_evidence` 保持单条形状（最后一条）；串联时另附完整有序的 `verification_evidence_chain`。归档投影、模型可见的运行事实和交付复核焦点都读取它。

## 2026-09-26 验证分类：换行与不执行检查的参数

- **反例**：Codex 在 main `7179f12e4` 上复现了三个反例，返回 0 时都被记为 passed/full。
  - `pytest\necho done` 的返回码来自 `echo`。
  - `pytest --help`、`pytest --collect-only` 根本没跑测试。
- **修复**：只减少证据，不读输出。
  - 整条命令出现换行或回车就不算证据，cd 前缀部分也一样。shell 把换行当命令分隔符，shlex 和 cd 前缀的正则却会把它当空白吞掉。`&&` 后换行这类合法续行也一并不算，只会少记。
  - 匹配到规范验证命令后，其后的参数命中已知"不真正执行检查"的参数就丢弃，`--flag=值` 按 flag 比对。已知参数有：
    - 通用：`-h`、`-help`、`--help`、`--version`。
    - pytest：`-V`；只收集（`--co`、`--collect-only`、`--collectonly`）；只列夹具或标记；`--setup-only`、`--setup-plan`、`--cache-show`。
    - cargo：`cargo test --no-run`、`--list`。
    - go：`go test -list/-c/-n`、`go build -n`。
    - make：演练（`-n` 等）、问询 `-q`、只 touch 的 `-t`、忽略失败的 `-i`、打印版本的 `-v`。
  - 表按规范命令分开，因为同一个短参数在不同命令里含义不同：pytest 的 `-v` 是啰嗦输出，make 的 `-v` 是版本；pytest 的 `-q` 是安静，make 的 `-q` 是问询。查不到规范命令时按首个词查，例如 `make`。
  - 这是已知形状的优化：不认识的参数仍按原规则记证据。
- **已知限制，未改**：
  - 只看命令行参数，不看 `PYTEST_ADDOPTS`、`MAKEFLAGS`、`-o addopts=…` 或 ini 里的 addopts 这类配置通道。项目测试配置里跳过全部用例同样看不出来。
  - make 的合并短参数（如 `-nk`）和 go 的双横线写法（如 `--list`）不在表里。
  - 其它生态的范围判断没动：`cargo test foo`、`go test -run X ./...` 只跑部分用例，仍按原规则记为 full。
- 回归与变异见 [TESTS](../../../TESTS.md)。

## 2026-09-24 深夜：余量不足的工具输出立刻外置并要求先压缩

- 真实验收样本：单个活动回合的工具循环把 69 万字节报告读完，上下文冲到窗口 129%，恢复压缩报 `COMPACT_CANDIDATE_TOO_LARGE`。根因不是单条输出过大（`read_file` 分页 16k 字符、通用输出超预览即外置），而是同一轮多条结果在下一次预检之前全部内联进入上下文，压缩候选保留区随之放不下。
- 修法（通用，不看工具名）：`tool_call_archive_record._headroom_forces_externalize` 在归档时用 preflight 同一口径 `model_visible_context_budget` 取距自动压缩点的剩余余量，本条输出估算 token 不小于余量时给 `ExternalizeToolOutputRequest.force_externalize`（`read_file` 分页也外置，正文落 artifact，模型只看预览与恢复锚点，记录带 `output_externalized_reason=tool_result_headroom`），并在 `live_archive_state` 登记 `tool_context_window_overflow(reason=tool_result_headroom)`，让下一次预检必走统一 Compact 链。开关 `tool_output_externalize_on_low_headroom`（默认开）；配置阈值为 0、已达阈值、输出不长于预览、窗口未知或预算计算出错时不介入。
- 测试：`test_tool_output_headroom_externalize.py`（外置 + 溢出登记 + 预检消费、同轮累加、余量够时内联、开关关、预算未知回退）；`test_compact_native_ir_recovery.py` 的超预算原生历史夹具改为显式关掉该开关，继续验证"历史已经超预算时恢复宿主先压缩再发业务请求"的既有合同。
