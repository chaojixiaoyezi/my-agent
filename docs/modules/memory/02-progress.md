# Memory Progress

## 2026-09-04 Compact 最低水位与缓存安全辅助调用

- native live Compact 的资格预估改为在 IR 副本上执行与提交阶段相同的完整工具对回收，不再用空历史假设
  UserTurn、RuntimeFacts 和 carried summary 可删除。真实不可删前缀已达到 recovery target 时跳过 live 摘要，
  由 transcript Compact 处理旧会话；低于 trigger 的有效候选仍可提交。
- live 摘要按 会话运行时 的“完整 history 后追加 compact 指令”顺序，并采用 终端交互 的 cache-safe fork 原则：
  保持主请求结构化 prompt、provider history、当前 IR、system、tools、model 和 thinking 配置，只增加末尾
  volatile 指令。辅助 wrapper 没有工具执行循环；返回 native tool block 时改用 typed 机械摘要。
- provider 空/非法正文的机械摘要独立封顶 4K，不再复用 12K 输入预算。辅助账本输入估算同步计入 system/tools。
  77 项直接测试及 196 项 Compact/cache/provider 相邻 focused 通过。R166 `.10` 单 Gateway + MiniMax-M2.7
  真 TUI 通过：main 2 代、child 1 代均带进度动画并继续原任务，provider 请求账本持续有 cache-read，最终
  8 个 child 与 main 全部收口；child live-tool `118,696→73,217`，main live-tool `85,161→726`。

## 2026-08-29 自主维护与 owner 隔离收口

- `promotion_mode` 改为宿主按 typed target/type/origin/action 重算，旧 batch、replace/remove 或历史
  `manual_required` 不再把非 SOUL 候选永久黏在人工队列；模型也不能通过输出该字段取得或撤销写入权。
- `remember` 的 add/batch/replace/remove 统一走 Candidate → Promotion，同样执行 message/tool evidence、
  exact entry ID、scope、冲突、CAS、quota 与注入扫描；只有真实 `PROMOTED` 才报告本轮记忆已改变，
  `ALREADY_PROMOTED` 幂等重放不再误报二次修改。
- USER/AGENTS 由当前 owner 的 Agent 经唯一 `update_persona` 自主维护；只有 SOUL 的写、删、回滚进入用户
  确认。飞书待确认记录也只接受 SOUL，基础文件、patch、shell 与管理员 full-access 不能形成旁路。
- lesson/HOT 继续依赖结构化证据和阈值自主晋升：单次模型推断不会直接成为正式教训，跨任务/运行/日期的
  独立证据满足门槛后才提交。每个 owner 使用自己的候选、正式记忆、Persona、daily、lesson 与 HOT 路径。
- focused 回归覆盖自主 CRUD、候选旧值升级、SOUL 保持人工、Persona effect 审批、飞书 SOUL-only、
  owner A/B 自主写入互不串线和幂等重放；真实 MiniMax 多 owner TUI 仍是最终产品验收。

## 2026-08-28 后台权威 Compact 与 carried 大参数收口

- `RuntimeCompactPolicy` 不再用 `save` 单独判断持久资格：exact ConversationThread 的 authoritative 后台片
  即使 `save=False`，也能通过原 checkpoint/CAS 提交代次；普通辅助轮保持临时裁剪且不能写账。
- 后台恢复的大 `write_file.content` 复用现有 live prompt reducer，正文改为 chars/bytes/hash/preview；有界
  工具索引保留开头和最新尾部，中间写明确 omission。focused 覆盖大正文不回灌、最新动作仍可见和
  no-save policy 正反面。
- live-tool Compact 现在从语义摘要前到 CAS 后发布真实阶段进度，与 transcript Compact 共用同一公开 schema；
  summary 很慢时 spinner 继续刷新但百分比停在当前 milestone，不以墙钟伪造推进。callback 只读、fail-open，
  不改变失败回滚、checkpoint 或 generation。
- live-tool 候选计量后优先达到 recovery target；达不到目标但已低于真实 trigger 时仍正式提交，避免摘要
  已付费却 generation 永远为 0。只有 `after >= trigger` 才回滚原 IR 并发布中性的
  `superseded/candidate_discarded`；failure count/generation 不变，同代 transcript operation 可以继续。
  摘要 transport、checkpoint 或 CAS 的真实异常仍保留红色 `failed`。
- 工具归档现在显式分开 provider 原始 `model_parameters` 与宿主执行 `parameters`。run/task/request/cwd 等
  host binding 仍可用于审计、幂等和恢复，但 carried 摘要、native replay 与后续模型可见投影只读取前者；
  旧索引只在 value-free `input_sources` 能证明字段来自模型/调用方时才回退提取，不能把宿主默认值带回模型。
- 大输出归档发生在模型预览裁剪之前；即使正文没达到全局阈值，只要工具结果声明
  `requires_recovery_artifact=true`，完整结果也先写 owner 私有 artifact，再给模型短 preview 和逻辑 ref。
  `read_artifact` 的 slice/head/tail 明确返回 `total_chars/has_more_before/has_more_after`；tail 已到真实 EOF，
  不再因“省略了前缀”误发下一页读取。

## 2026-08-28 Compact 辅助模型调用纳入统一用量账本

- carried archive 与 live native history 的语义摘要不再绕过主模型调用账本；它们现在复用
  `ModelCallLedger`、供应商 attempt observer、全局并发准入和同一成本指标，真实 Compact 开销能够随
  对应 request/run/task 一起核算。
- 已删除 Compact 自己的 daemon-thread 20 秒截止与对应配置。慢模型摘要只服从供应商传输层的有界超时，
  不会出现主线程已回退、后台 HTTP 仍继续消耗额度的“孤儿请求”。供应商超时/调用异常仍恢复原历史并进入
  失败熔断；请求正常完成但正文为空时不重复调用模型，而从 typed IR 构造有界机械摘要后提交，不删除原生
  工具事实，也不把空正文升级成主任务失败。
- 连续 Compact 只替换上一代 thread summary；带稳定 schema marker 的 active-turn carried handoff 会继续
  保留，当前真实任务在 wire 上只发送一次。聚焦回归覆盖二次 Compact、慢摘要和辅助调用用量登记。

## 2026-08-29 live Compact 摘要形状校验

- 真实 MiniMax-M2.7 长链证明，仅在 prompt 里写“不要调用工具”不够：摘要请求曾返回原始
  `<minimax:tool_call>`，程序又把它当交接正文写入 generation 5；下一轮因此误以为报告已写并从头重读。
- live 摘要现在必须以 `compact-live-handoff.v1` 开头，并按顺序包含当前进度、用户约束、完成、失败、未决和
  下一步六栏。缺栏、过短动作句或工具协议全部视为不可用摘要，从同一 typed IR 生成有界机械交接；不会重复
  请求模型，也不会执行或回放摘要中的伪动作。
- 该校验只守上下文完整性，不判断任务质量或完成。archive、operation ledger、artifact 与真实文件继续是
  精确事实源；摘要 transport 异常仍回滚并进入原 Compact failure circuit。

## 2026-08-25 后台续片保留副作用操作终态

- 真机 `create_subagents` 的原始 tool artifact 已有 `tool_operation.status=succeeded`，但旧 index 没保存该
  nested typed fact；child 完成唤醒后的 carried record 只有 `ok=true`，按 fail-closed 核验被判 unverified，
  使 root 已回复却仍保持 active/Working。
- 当前 externalizer 对大小输出统一白名单保存 `tool_execution` 与 `tool_operation`，恢复时展开为现有
  operation verification 字段。`diagnostic/private` 等任意 envelope 字段不落 index；没有 operation 终态的
  旧记录仍保持 unverified，不用 `ok` 或输出正文补猜。
- externalizer、carried refs 与 background root completion 定向回归已证明成功 operation 跨片保持
  succeeded 且 task link 关闭；真 Gateway/TUI 复验待部署后执行。
- `4106025` 真机复验发现索引虽已有 operation，后台仍按 durable task id 查 foreground request 行。
  当前 externalizer 显式保存 `conversation_request_id`，child completion 信封传递同一 exact turn，
  carried refs 只按它恢复；字段落盘前的旧行仅在 `request_id` 同值时精确兼容。

## 2026-08-22 root active-turn 工具索引续接

- child lifecycle wake 现在复用 task `work/blobs/tool_outputs/index.jsonl` 的 typed 行，按 completion 信封的
  exact `conversation_request_id` 还原同一 active turn 已经执行过的工具；durable task id 只定位索引，
  不能当 turn id。小输出 `tool_call` 和大输出 `tool_output`
  共用 scoped call id 去重，后者只携带 artifact ref，不把正文整体塞回 prompt。
- r11 证明只恢复调用行仍不够：旧参数投影把 list 内 dict 丢掉，使 Todo/批量派工 `items=[]`；native 又
  不消费机械 tool-context。当前索引对 JSON 参数限深、限宽并递归脱敏，保留 items/covers 等结构关系；
  未知对象不 stringify。native 跨进程续跑把 carried 轨迹作为唯一有界 `CompactionSummary` 放回 IR，
  后续真实 UserTurn 保持在其后，不伪造 provider tool-use，也不重放副作用。
- 当前 Task Runtime State 另投影唯一 task_progress ledger 的 existing/open exact ids、完整 read 参数和
  `create_subagents.items[].covers` 精确绑定字段；代码不从标题或 goal 猜映射，也不自动合并同义清单。
- 该恢复只用于 root 后台工作片的执行连续性与 one-shot 去重，不改变 Compact generation、长期 Memory、
  child 私有 archive 或机器完成判断。r11 已证明原 objective/语言连续性；嵌套参数与 native handoff 的
  fresh r12 真机验证仍待严格 gate 和部署。

## 2026-08-22 运行中工具历史 Compact 统一账本

- 主代理、子代理和孙代理在同一运行 turn 内缩减 native 工具历史时，语义摘要会把
  ConversationThread 的上一代完整摘要与本次待回收工具往返合并成下一代替代摘要；
  不再只生成一份脱离会话代次的临时摘要。
- 持久、会话正文权威的真实 turn 必须在摘要成功后写 Compact checkpoint，并通过同一
  ConversationThread CAS 推进 generation；摘要 transport/调用异常、checkpoint 失败或 CAS 冲突时恢复
  原 native IR，并累计同一 Compact 失败熔断事实。只有调用已经正常结束但 text 为空时，才由 typed
  UserTurn/ToolCall/ToolResult/refs 形成有界机械替代摘要，不重试且不进入失败熔断。
- raw archive、operation ledger、artifact registry 和真实文件仍是执行事实源；语义摘要
  只负责让下一模型轮知道已经做过什么、还缺什么，不能单独证明任务完成。
- `.7` 原样 Prompt 4 真机已证明 worker-3 在 119,295 tokens 提交 generation 1 并降到 36,586；但摘要模型
  普通续写最后工具动作。根因是 backend 将摘要 prompt 放在 history 最前。当前按 会话运行时 改为原任务 user
  在前、native history 居中、synthetic Compact user 指令最后；位置敏感 fake 回归会在顺序倒退时直接失败。

## 2026-08-17 测试债清理：home 优先级修复恢复记忆链路测试

- 根因：`_configured_home_root` 曾改为"环境变量无条件优先"，但测试 conftest 的
  隔离 fixture 给每个测试注入 MY_AGENT_HOME，导致 30+ 文件中显式
  `my_agent_home` 配置全部失效——memory runtime/basics/archive 等一整簇测试挂掉
  （看似记忆模块问题，实为 home 解析优先级）。
- 修复：显式配置值 > MY_AGENT_HOME 环境变量 > ~/.my-agent 兜底；配置 YAML 默认
  改空（未固定 home 时环境变量注入生效）。home 4 文件 54 项 + 记忆簇 ~38 项转绿。
- 记忆模块本身无行为变更；测试恢复覆盖 raw archive / runtime fact / auto-resume
  context / compact 续接等既有契约。

## 2026-08-17 EXEC-31b 测试适配（记忆路径）

- `tool_protocol="text"` 配置全部移除（30 文件）；协议测试快照默认改 native。
- 记忆相关假后端补 native 探针 + `**kwargs`（native 传 tools/messages/tool_choice）。
- compact 自动续接链在 native 工具轮下第二次续接的 ready 判定差异标 xfail 记录
  （`test_run_auto_compact_apply_can_repeat_when_continuation_makes_tool_progress`），
  待后续按 native 语义适配断言。

## 2026-08-16 阶段三 gorm 复刻实验（记忆侧观察）

- 双线（ma-a/ma-b）长任务复刻全程走 memory raw archive + runtime facts + auto
  resume context；未发现记忆模块新增缺陷。
- resume 链（run --resume）在多次 429 配额中断下跨进程恢复，memory archive 的
  recovery snapshot 与任务事实源保持一致性，无记忆污染。
## 2026-08-30 Persona 写入限频 owner 隔离

- `.10` 双 owner 同时维护 `USER.md` 时，旧进程级 30 秒写入计数发生串扰：一个 owner 的合法写入消耗了
  另一个 owner 的额度。被拒调用虽有 `PERSONA_UPDATE_RATE_LIMITED`，但没有明确副作用终态且 taxonomy
  漏登记，运行层将它升级成 `TOOL_OPERATION_OUTCOME_UNKNOWN`。
- 当前按 canonical owner home 保存独立时间窗，旧 owner bucket 每个窗口有界清理；同一 owner 仍保留 30 秒
  最多 3 次写保护，跨 owner 永不共享额度。限频结果明确为 `not_started`，错误 taxonomy 支持退避，推荐同一
  用户消息内的多个事实使用一次 operations 合批。
- 38 项 Persona/错误/恢复/幂等 focused 与相关 Ruff 已通过；唯一 Gateway 下 u310/u311 并发各写三项的六条
  operation 全部成功，各自 `USER.md` 无交叉；同 owner 新 TUI 无工具读取准确召回三项，真实验收通过。
