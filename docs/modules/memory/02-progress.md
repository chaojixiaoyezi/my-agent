# 记忆与上下文维护状态

子代理 run 工作区登记 `lessons.jsonl`（2026-09-25，分支 `claude/subagent-lesson-ledger`，待合入；已端到端真实验收）：`AgentRunWorkspacePaths` 新增 `lessons_jsonl`，与 `findings.jsonl` 同目录，由子代理 `record_lesson` 工具独占追加；工作区同步不创建也不覆盖它。账本经验经子代理结果收口进入 owner `candidates.jsonl`，成为 `subagent_lesson` 候选：适用场景取 `when_to_use`，证据引用账本条目。晋升规则不变，正式 lesson 仍要 approved、重复独立证据（occurrence≥2）并通过威胁扫描。

召回证据落上下文包（本地分支 `claude/decision-recall-evidence`，待审）：上下文包 `memory_refs` 新增本轮实际注入记忆的来源清单与记忆决策发现码，区分原完整召回与召回前补充；提示段字节不变。为第 14 项 P5-A 的真实收益实验提供结构化证据。

child历史说明在不展示正文时不再提前读取完整来源或计算展示窗口，保留原线程说明及核验；三文件31项通过。三宿主seed物化峰值已定位，后续延后/释放尚未实施，12.4未完成。

12.4来源生命周期首片已本地实现：完整选中消息保留固定文件地址视图，摘要数组按原编码顺序重放；共享token估算提取同一chars/UTF8/结构开销公式，旧小JSON优化保持。load→摘要→CAS内存红绿验证完整来源及精确ID，取消/文件变化/重放隔离及宿主机械类型兼容已独立复核，不能算三宿主全链完成。真实缓存12.7已使用固定旧安装包限定验收，见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4原生历史投影保留一次canonical隔离复制，已隔离副本直接用于模型/摘要，匿名重复输出仍独立；嵌套容器测试峰值约4.89MB降至3.03MB。复用主线b4ffb3475的小JSON有界直接编码修复，估算口径不变；三文件72项通过。来源正文/覆盖ID仍驻留，12.4及11/18不变。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4摘要分段本地15文件316项通过：复用原循环顺序读取JSON字符、消费后释放窗口；共享估算器改流式累计且数值保持。修复提示纳入预算，发送及来源EOF后复查取消；writer/CAS不变。全链仍有原消息/覆盖驻留，11/18不变。见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

后台Compact已本地接同一scope/view的摘要注入和精确覆盖，局部来源/提交不改全线程摘要和游标；18文件联合420项通过，最终验证见TESTS。此片不证明完整恢复payload，Gateway/child准备同view、初次/手动和真实缓存仍待验；唯一TODO的12.4保持未完成。

工具输出原索引与artifact现保留实际run/attempt/模型turn/call身份；Compact恢复按完整身份去重，旧未知不借当前runner补值。同正文同call跨执行轮的新产物路径包含身份摘要，旧引用不迁移。此为恢复来源底座，不代表后台范围恢复已接通。

决策分支本地合入已提交插件基线后，Curator 前置标注与正式记忆召回的取消异常已直接使用唯一 `common.cancellation`；交叉定向九文件 298 项通过。关闭、超时和普通失败仍保留原提取/召回，用户或宿主取消仍传播；完整合并后全仓及真实故障组合尚待验收。

## 召回前可选补充查询（本地首片）

正式 JSONL 检索已有不提前 touch 的 scoped 候选入口和最终访问确认；宿主也可从完整问题产生至多四个有限查询片段，
现已接到独立、默认关闭的 `pre_recall` 决策点与原正式上下文。完整问题的原召回始终先执行，Jev 只能在剩余数量和字符预算内建议一次同 scope 补充；关闭、观察、非选择和故障不改原材料。
候选检索仍可使用原语义 embedding；此处的“无 touch”仅指未注入候选不产生长期记忆访问信号。
六个相关文件联合136项本地通过；受控 JSONL 漏召回样本证明补充查询可只追加并确认原基线遗漏事实。隔离 Jev 的两轮 off/observe/apply 均建议第二查询，但词面基线已命中全部两条事实，应用没有新增，不能算真实质量收益。真实 Jev 漏召回、Gateway 最终来源和净输入 token 尚未验。

## 召回后可选重排（本地已验，未部署）

原授权召回、去重与预算先选好全部材料，决策仅重排长期事实原槽位，HOT/lesson 不移动，任何非选择或错误保留原序。
采用前复读原来源和范围，过期/删除/撤销的旧记录不被建议复活；复用本轮 PreparedRuntimeContext，不另建记忆库或缓存。
原准备→上下文→工具循环组合已验证，真实 Jev 效果尚未验；见 [09 交接](../../tasks/DECISION_MODEL_P3_RECALL_HANDOFF.md)。
正式记忆读取现在先遵守原请求的 `task_local`/`control_plane` 范围与 owner `memory_enabled=false`；
这些模式不扫描正式项目记忆。已有历史或本地任务事实并不因此被删除，原控制平面材料保持原合同；三个入口场景已本地验证。

## 来源与正式记忆的可选关系建议（P5-B 第一片，本地实现）

独立用户后台接入点 `curator_relation` 默认关闭，与分类/优先级使用同一阶段和原 lease 头寸。
完整消息与带真实版本、哈希可核验的短 long-term 正文可获可能重复/更新/冲突提示；发送前和采用前复读原仓库。
lesson/HOT、截断、缺版本或 audit 正文覆盖未知时保留原批次并给 `need_data` 诊断。
提示只进 Curator 临时输入，原候选、证据、晋升、人格确认及游标不变，不做全库语义合并。
原仓库/提取/提交组合已本地离线验证；真实 Jev 的关系识别质量与实际业务收益尚未验。
边界与文件交接见 [P5-B 交接](../../tasks/DECISION_MODEL_P5B_HANDOFF.md)。

## 子代理工作区的只读路径投影（本地已验）

原 task workspace 的 root/run/task 身份和路径计算已提取为保存与上下文准备共用的只读入口。
预览不创建目录，不写共享状态、artifact 索引或日账事件；真实物化仍沿原 `ensure_subagent_task_workspace`。
默认与显式工作区下的 root/child/grandchild 路径、写入边界及原父状态合并已纳入 136 项定向测试并通过。
这是创建前容量的路径切片，尚不涵盖选中模型引用重新冻结、完整首请求展示或逐候选输出预算；
交接与限制见 [容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
第8步索引恢复补齐：externalizer 保存有界 `tool_process`，carried reader 恢复原 process 信封；投影唯一位于 `tooling/runtime_facts.py`，旧索引不推定清理成功。组件验证与真实 TUI 分开。

## 第8步逐调用 Compact 来源修复

新 live-tool checkpoint 显式追加 `tool_call_ref.v1` 来源与尾部数组，使用原始 `run_id/attempt_id/call_id` 匹配；裸编号允许跨域重号，计数按记录保存。新 refs 参与新候选内容地址，防止同号 orphan 覆盖已提交来源。旧无 refs ID 和账本不迁移。
工具输出索引现在保留 canonical attempt/turn，恢复去重不再使用 scoped 字符串。旧无身份记录保持完整并标为 `uncertain`；全未知大历史返回未压缩，provider overflow 可能无法恢复，这是实际兼容限制。旧 reader 不可直接读取新混源 checkpoint；回滚须保留新账并匹配运行时与数据快照。定向证据见 [独立交接](../../tasks/HANDOFF_STEP8_COMPACT_CALL_REFS.md)。
决策分支吸收 main 后（本地，未合入 main），上述三元 `tool_call_ref.v1` 由 v3 四元身份取代：`tooling/call_ref.py` 已删除；主线写出的 v2 refs 行按 legacy 读取、不隐藏任何记录，未知来源的可见性和 uncertain 结果仍保留。见[依赖拆分合并节](../../design/TOOL_LOOP_DEPENDENCY_SPLIT.md#两线合并后的来源身份与模型轮结果决策分支吸收-main2026-09-23)。

本地开发 C 顺序摘要来源：两遍长度/hash 与可释放字符窗口替代整份 JSON 副本。密集 iterencode 估算闭包循环积累已由有界小载荷编码修复；14文件组合333 passed、20项既有xfail，真实TUI尚未验收。

## 第8步本地候选

消息分页与幂等扫描已按完整LF边界拆出，固定尾界／字节预算由调用方显式传入；原锁、目录、游标及错误事实保留。token估算按原JSON顺序流式计数，原数值、结构开销与异常优先级不变。七文件186项通过，模型链组合18文件456 passed／24既有xfail；未部署和真实TUI验收，不代表Compact scope来源链已完成。


## 边界

出站摘要诊断已接入原模型调用账本，区分前缀改动与只追加消息，不记录正文/密钥或删减历史。
服务端缓存淘汰仍不可由客户端证明，诊断返回 unknown；真实 TUI 缓存值与调用记录另行对账。

记忆按 owner 隔离，由该用户代理维护。会话历史、持久记忆、展示归档和摘要检查点是不同概念，不能混用；人格文件确认规则与普通记忆写入规则分别处理。

## 当前实现

- 运行中的原生工具历史 Compact 摘要已在本地改为复用原有界分段发送，并在每段前后读取同一 run/线程停止信号。超大参数、结果和推理历史须完整覆盖；失败或取消只放弃候选，不回收 IR、不提交 Compact 代次。定向回归已通过，真实长上下文与模型切换仍待验。

- 本地 P2 Curator 前置决策已接原批次提取入口，默认关闭；owner 设置支持 off/observe/apply。后台设置现已与线程前台覆盖严格分开，新线程后台覆盖拒绝，旧值只允许恢复继承清理。
  observe 只留调用事实，apply 仅在原输入中附临时分类和优先级，原材料不丢弃、不重排；
  错误、超时、过期或提示超预算继续原提取。原验证、整批提交、晋升和游标仍持唯一权限。
  后台使用真实 Curator run 和 owner 范围，不冒用历史会话；调用进入原模型账本，
  尚不代表已有独立后台用量持久结算或用户会话用量归属。已用 fake 决策/原 worker 和原 Curator
  提取提交链验证，未跑真实 Jev、实际 TUI 或部署验收。
  注释整理完成后再次调用公共消费复核，服务返回后的关闭、配置修改或期限到达也不能采用旧建议。

- 本地 Curator 的有界模型等待已迁入共用 `backends/bounded_call.py`，删除旧线程/队列副本；
  到期不再额外等待清理，准确 worker 或 cleanup 未退出时保持 still-running，禁止重叠重试。
  原缩批、游标与记忆提交规则不变；与取消/HTTP/准入联合 315 项通过，尚未实际 TUI 验收或部署。
- 后台整理的模型调用现在自带宿主会话：`_execute` 按 owner_id + run_id 走与前台同一 `provider_session_scope`
  包住整次提取，同 run 重试/缩批同值、run 结束复位，不冒用前台线程会话。此前要求会话头的服务商在发请求前
  就抛 ValueError，主 owner 自 2026-09-13 起零提取，且被记成 `CURATOR_SCHEMA_INVALID`。供应商调用阶段的
  ValueError/TypeError 现由 `CuratorModelCallError` 标记阶段并归 `CURATOR_MODEL_FAILED`，解析失败仍是
  `CURATOR_SCHEMA_INVALID`；失败诊断附脱敏截断正文。本地三文件 76 项通过，真机 Gateway 尚未部署复验。
- 存储组合后，Curator 通过 `threads.list_report` 和 `messages.after_report` 读取；
  Promotion 通过 `messages.by_id_report` 核验精确消息。原游标、坏账处理和证据匹配不变，不保留旧方法回退。
- 普通后台策展让出正在工作的同模型端点，pending 和记忆游标保留；pre_compact 屏障不被延后。
- 后台自适应预算已传入 HTTP；超时取消连接，旧调用未退出前不叠加重试。资源范围限本 Gateway，
  外部应用抢占及模型服务缓存上限另查，不能误报为会话历史丢失。

- canonical 未压缩历史参与前后台模型输入；recent display window 只作展示。
- 历史读取区分 ready/unreadable/disabled，失败不能静默退成“历史为空”。
- JSONL 按物理 LF 分记录，字符串内 NEL/U+2028/U+2029 保持完整。
- 主子代理共用压缩状态与代次；自动/手动压缩、归档引用和恢复摘要复用统一路径。
- 展示当前上下文、累计用量、缓存读写与真实请求长度不能混为一个数。

## 后续重点

长会话多任务续接、切小模型后的压缩预算、跨工作片的缓存前缀，以及小时级慢模型下的记忆连续性仍需真实验收。不得仅因显示 token 下降就推断丢记忆，也不得未查请求事实就宣称完全保留。结构见 [04-structure](04-structure.md)，开放项见 [STATUS](../../../STATUS.md)。

恢复摘要来源扩展至同一次冻结的真实原生工具往返，与归档同ref去重后沿原adapter和有界分段器处理。严格恢复空回复/工具调用的机械回退完整保留旧摘要与原模型可见材料；分段修复失败拒绝提交，避免截断摘录获得完整coverage。不新增记忆库或持久状态，不读取外置全文。此片本地联验中，外层重跑传递原生IR仍待实现；见决策模型容量审计末节与TESTS。

后续外层overflow接续已本地实现：三宿主保留完整原生工具IR，释放失败经原partial出口保存完成事实；prefix接管摘要时移除旧applied_compact，避免transcript-only重复。无任务后台以宿主冻结视图校验线程，不补task属性误建任务。联合验收及剩余边界见TESTS；媒体、超大历史和真实缓存仍未收口。
