# Memory Structure

本文只描述当前 owner-home 主链路。

compact summary 是上下文压缩能力，不是任务验收能力。它保存 transcript cursor、工具 refs、读取范围、
task identity 和 goal state；不会扫描 `output/`、生成完成 marker、调用提交工具或恢复已删除的 closeout
状态。摘要调用只需要 backend 的轻量 `generate(prompt)` 形态，echo/fake backend 不必实现工具协议。

模型正文也不是执行事实源。每一轮工具 prompt 尾部的 `current_turn_execution.v1` 只投影当前 request
已经形成的 canonical tool records；`successful_mutating_calls` 为空时，没有结构化事实支持“已保存、
已删除、已发送”等结论。该投影用于帮助模型如实回答，不替代 Registry、operation store、权威文件和
回读验收；模型即使忽略它写出错误正文，也不能改变底层状态。最终结果另由同一批记录生成
`operation_verification.v1`，成功必须同时具有 `ok=true` 和 operation `succeeded`。内部逐操作事实
进入 `AgentRunResult`；公开投影删除 call/operation ID、参数、路径和 refs 后进入 Gateway/HTTP、
assistant transcript、后台回复、历史索引和 compact。零操作回复同样保存 `operation_count=0`，
compact 不需要解析中文尾注或模型正文。存在副作用调用时用户正文后才附程序核验块，普通聊天不添加
固定文案。该结构证明实际执行事实，不声称能在不理解自然语言时删除自由正文中的每一句错误自述。
模型生成的 compact summary 同样没有执行权：`conversation_thread.v4` 在同一次 cursor CAS 中另存
有界 `compact_operation_evidence`，记录已覆盖 assistant 数、coverage、操作计数和最近程序终态。
后续轮在摘要之后独立注入该 JSON；摘要即使把 `remember/list` 错写为 remove，也不能覆盖程序字段。
旧 transcript 缺 metadata 时只标 partial，不用摘要补齐。

## Tool failure diagnostic durability

- 工具记录只白名单保存 `error_code/failure_stage/handler_executed/duration_ms`。其中
  `failure_stage` 只能是 protocol、authorization、validation、runtime_gate、execution、
  effect_reconciliation、persistence；未知值不进入恢复上下文。
- `handler_executed` 表示当前这一次是否进入真实工具实现，不表示历史操作从未执行。精确幂等重放会把
  当前值记为 false，同时在受限的 original execution facts 中保留首次执行证据。
- compact 对这些字段只做机械投影；不得根据错误正文重新分类，也不得把 timeout/effect unknown 摘要为
  可安全重试。`duration_ms` 只接受非负整数，畸形旧归档回退为 0，不能让恢复链中断。

## Runtime fact lifecycle

- `memory_archive/runtime_facts/<request_id>/task.json` 是同一运行的恢复事实，不是另一份会话或任务状态。
  live archive 首次写 start，工具轮只更新 progress；正常完成仍由既有 finalization 路径写终态。
- 只要 start/progress 事实已经存在，模型调用、工具循环或 finalization 抛出的终止异常必须经同一
  terminal updater 原子合并：普通异常写 `run_status.status=failed`，`InterruptedError` 写
  `cancelled`，两者都设置 `response_present=false` 并保留已有 progress、artifact 和 next actions。
  如果根本没有创建 runtime fact，terminal updater 不凭空造一份孤立事实。
- provider 的 incomplete 响应不直接写成成功：没有耐久工具结果时立即失败；已有耐久工具结果时最多
  继续采样一次。partial text 和 partial tool arguments 从不进入 transcript、执行器或 runtime fact，
  只有已经完成的 canonical tool records 可以跨这次继续保留。
- `/btw` 仍由 conversation typed mailbox 掌权；pending input 只让当前空/incomplete 旧采样失效，
  不改变 owner、task、run、工具权限或 compact 的事实源。

## Compact threshold authority

- 正式默认 90% 由 settings/runtime/standalone compact options 与 `config/agent_config.yaml` 对齐；部署级
  压力值必须显式配置，不能写成另一套代码默认。
- 触发判据使用 active turn，不使用累计账本；provider usage 低于本地完整 prompt 估算时保守取较大值。
- 百分比直接换算为 `context_window * percent`，达到该 token 边界即进入同一 compact/resume 链；不预留
  尚未生成的 `max_tokens`，也没有覆盖配置值的 95% 二级天花板。因而配置 90 就以 90% active context
  为触发点；provider 没有返回 usage 时仍只能使用本地 tokenizer 估算，观测值可能近似但阈值数学不漂移。
- context window（上下文窗口）容量的唯一优先级是：provider 模型 metadata API 的显式字段 →
  `model_context_window_tokens` 本地配置 → 200K 通用兜底。provider 值一旦存在，无论比本地配置大或小都
  直接采用；本地配置不得伪装成 backend/provider metadata。provider 探测按 backend 实例缓存，失败只
  回退配置，不改变 90% 阈值、token 估算、compact summary/cursor/generation 或原始 transcript。
- MiniMax 的 `/v1/models` 与 `/v1/models/MiniMax-M2.7` 当前只返回模型身份和创建时间，没有返回
  context window 字段；因此运行时按上述合同使用本地配置。MiniMax 官方文档另声明 M2.7 总上下文为
  204,800 tokens，但文档声明不冒充本次 API 响应字段。

2026-07-09 P0 维护只调整 `compact_semantic_summary.py` 的类型导入，不改变本页结构或事实源。

## Owner Home

```text
~/.my-agent/owners/<provider>/<owner>/
|-- memory-hot.md                         # 很短的高频偏好/规则
|-- memory.md                             # 入口说明，可引用下方文件
|-- memory/
|   |-- long_term/memory.jsonl            # 主代理长期记忆，显式 remember/export 才写
|   |-- ops.jsonl                         # 候选/无正文操作审计，不参与召回
|   |-- daily/YYYY-MM-DD.jsonl            # 每日工作记忆
|   |-- lessons/*.md                      # 较长经验/教训
|   `-- routing/INDEX.md                  # 人类可读路由索引
|-- audit/YYYY-MM-DD.jsonl                # raw turn/tool/gateway 黑盒流水
|-- tasks/<date>/<task-slug>/
|   |-- output/                           # 最终交付物或索引/验收记录
|   `-- work/                             # 过程状态、子代理账本、草稿
|-- agents/<run_id>/                      # refs-only projection
|-- compact/conversations/                # 主代理 thread compact 事件账本
|-- global_index/                         # 可重建 owner/task/run/agent 索引
`-- workspace/runtime/workspaces/<scope>/ # LocalStore、gateway、conversation 等
```

## 主代理记忆

- `memory/long_term/memory.jsonl` 是正式长期记忆机器库，适合 grep、RAG、向量索引和审计。
- `memory/ops.jsonl` 不是第二份长期记忆：只存模型推测候选和无正文操作事实，search、prompt、
  compact 都不会把候选当作 active memory。
- `memory/daily/YYYY-MM-DD.jsonl` 是每日流水摘要，记录当天的工作片段、引用、决定和下一步。
- `memory-hot.md` 只放极短规则，避免长期 JSONL 或 lessons 被整个塞进 prompt。
- `memory.md` 可以作为入口和索引，引用更具体的 lesson 或 routing 条目。

### MemoryRecord 行结构

- 基础键：`role / content / kind / tags / created_at`。
- 可修正操作键：`entry_id / action / version / source / updated_at / expires_at`。`action` 为
  `add/replace/remove`；读取时按稳定 ID materialize 当前 active 版本，remove 是 tombstone。
- 旧行没有 operation 键时不改写文件，而是按原记录内容、角色、类型、时间等字段推导稳定
  `memory-legacy-*` ID，并按 version=1/add 兼容读取。
- 可选 `attributes`：开放结构化扩展位——教训记忆的 `trigger_conditions`
  （结构化触发条件），以及 `origin/evidence_refs/subject_key` 挂在这里。旧行没有该键，
  读取按 legacy 处理；空 attributes 不写键，旧行格式不变。
- 写入口：`JsonlMemory.add`/`add_record`、`replace`、`remove` 与 `apply_batch`，最终都写同一 JSONL
  operation ledger。`apply_batch` 持锁重读后全量验证并一次原子替换，禁止半批提交。
- 完全相同内容按 role/kind/规范化正文幂等；有 `subject_key` 的不同事实必须用稳定 ID replace。
  remove 会清除该 ID 的历史正文并只留无正文 tombstone；daily、FTS/内容文件、向量、关联候选和
  结构化 remember 工具账本正文同步清理。conversation transcript、gateway audit 和 task facts
  记录的是用户真实交互与运行历史，属于另一条 retention（留存）边界，不因长期记忆删除而改写。
- 持久提交先取得 owner quota admission，再取得 Memory file lock；锁内计算权威 JSONL 与所有 daily mirror
  的完整最终/append 字节并整批检查，随后才写。索引仍是 commit 后的派生层，不能反过来成为权威。
- 消费：`memory_push.trigger_conditions_match` 按结构化事实匹配
  （列表=任一命中 / `min_` 前缀=数值阈值 / 标量=相等），匹配的教训在推送时
  排到最前（软提权，不淘汰未声明条件的记忆，绝不解析正文）。
- 生产端：失败自省调参后自动写一条带条件教训（failure_type + min_attempts），
  同型失败再现时自动提权注入。
- 普通召回先按当前 owner 权威源取有界候选，随后确定性排序与 subject 去重；投给模型时使用
  非权威 `<memory-context>` 数据信封。历史内容不能制造工具调用、覆盖当前用户消息或从用户出口泄露。

### Persona owner-local authority

```text
<owner-home>/
|-- SOUL.md
|-- USER.md
|-- AGENTS.md
`-- persona/
    |-- versions.jsonl
    `-- backups/<target>/<version>-<sha256>.md
```

- 三个 Markdown 文件仍是逐轮人格正文权威；versions 与 backups 只负责审计、CAS 和回滚，不形成第二份当前人格。
- `PersonaRepository` 是 PromptBuilder、`update_persona`、确认回调和 profile seed 的统一入口。
- load 只接受 owner 根内普通 UTF-8 文件，拒绝 symlink/越界/超过 2 MiB；威胁行被替换成安全占位，
  其原文不会进入 prompt 或工具 list 输出。
- prompt 注入按配置预算保留 75% 头部和 25% 尾部，并输出结构化 truncated/blocked/security/io 诊断。
- USER add/replace/remove 必须锚定当前用户原话；SOUL/AGENTS 保留确认边界。写入支持 stable entry ID、
  `expected_sha256`、history 和 rollback，确认期间发生并发修改时 fail closed。
- Persona mutation 同样先取 owner quota lock，再取 target lock；target 最终正文、新 backup snapshot 和
  versions.jsonl append 是一个 quota batch，拒绝时三者都不改变。
- 公开 mutation 参数先归一成不可变 request；锁内严格按 prepare → quota admission → commit 三段执行，
  CAS/rollback/no-op、版本 record 和最终工具结果各自有单一 helper，不在工具入口复制 repository 逻辑。

## 工具输出归档

- 当前 task 的工具输出写入 `work/blobs/tool_outputs/`；较大正文进入独立 artifact，索引只保留可检索摘要、
  hash、大小与路径，短输出也写一条 tool-call index。
- 完整 artifact 是 owner-scoped 审计事实，不直接等于模型正文。模型可见的 live、compact、恢复、
  shared context 和 handoff 共用一份 ToolSpec/结果投影：所有正文先脱敏，外部来源再进入不可信数据
  包装；状态、error code、hash、大小和读取 ref 保持结构化，不放进外部正文包装里。
- 从 `work/blobs/tool_outputs/` 重新读取时，来源跟随 canonical 目录而不是文件扩展名或正文。JSON
  wrapper 会解出其中的 `content`，纯文本 resilience archive 正常分页读取；两者经
  `read_artifact/read_file/search_text` 回到模型时都继续按 external data 投影。
- 失败记录的 `error_code` 是统一错误 taxonomy 的控制码，`reported_error_code` 是工具/provider 的原始报码；
  record、artifact 与 index 三层都保留这两个字段，compact/恢复可以使用控制码，诊断不会丢失真实原因。
- 参数来源 `input_sources` 与 read/page window 使用同一个显式索引投影。每项只允许
  `path/source/source_ref`，不保存参数值；短输出和外置输出都可在服务重启后追溯模型输入、安全默认值或
  Registry 可信上下文。
- 副作用记录另投影 operation id/status/action/replayed、idempotency scope 与
  effect outcome/source ref。权威终态保存失败时，表面成功必须先降级为 unknown；原工具报告只以
  value-free `reported_tool_result` 旁证进入白名单，不复制正文、诊断私有字段或任意 envelope。
- 失败诊断另投影 `failure_stage/handler_executed/duration_ms`。error code 表示失败类型，stage 表示
  生命周期位置，两者不能互相推断；当前重放与首次执行的 handler 事实分层保存，compact/recovery
  不会把重放写成又一次真实副作用。
- scoped call id 本身不证明存在可读 artifact。内联短输出继续写审计 index，但不会进入
  `read_artifact` 提示；只有 `artifact_ref/source_artifact_ref` 确实存在时才向模型提供恢复读取入口。

## 子代理记忆

子代理不写长期记忆。它只在自己的任务周期内写：

- `work/agents/<run_id>/canonical_state.json`
- `work/agents/<run_id>/events.jsonl`
- `work/agents/<run_id>/artifacts.jsonl`
- `work/agents/<run_id>/memory_archive/`（与根代理共用通用 Compact）
- `work/agents/<run_id>/checkpoint.json`（任务断点）
- `memory_gate/` 候选经验，等待父级或 root 显式导出

## Compact

- 主代理 compact 读取当前 task workspace、owner memory、tool-output refs 和当前 run 状态。
- 子代理 compact 读取自己的 task-local canonical state、events、artifact refs 和父级可见 guidance。
- active turn 的 native 工具历史达到同一配置阈值时，不创建另一套 compact：先由既有
  `compact_semantic_summary` 后端读取同一 IR 的原生消息视图，再用最多一条
  `CompactionSummary` 替换被整对回收的旧 ToolCall/ToolResult。该 item 在内部与真实
  `UserTurn` 分型，出站时按 provider 接受的 user-role summary 编码；后续再次压缩原位替换旧摘要，
  并与保留的近期完整工具尾部一起计入同一 token 预算。
- native summary 只提供工作续接语义，不能成为执行事实或权限来源。raw archive、operation ledger、
  artifact、workspace 文件、thread transcript 与真实 UserTurn 保持权威；摘要调用失败或返回空时
  退回既有有界机械 handoff，不能推进第二份 cursor/generation，也不能删除上述事实源。
- task workspace 查询只认当前 owner 下的 `tasks/<date>/<task-slug>/work/state.json` 和
  `work/run_workspace.json`；根目录旧 `tasks/*/state.json`、旧 daily memory 和旧 root route index
  不再作为当前 owner 的事实源。
- `read_file` / `read_artifact` 的恢复游标来自结构化工具记录；旧状态词、summary 和人工描述不能证明某段已经读过。
- compact work-state 的 `read_coverage` 同时保留 `primary` 主游标、`sources` 多源覆盖摘要和
  `incomplete_sources` 未完成来源队列；多文件/多项目任务恢复时先从 `incomplete_sources`
  续接下一段，再用 `sources` 审计每个 source_path 的覆盖范围，不能只按一个最大文件游标续接。
- `list_files`、`find_files`、`search_text` 的分页续接来自结构化 `page_window`；
  `next_offset` 可以继续展示在工具正文里给人看，但 compact/resume 只能从 `page_window` 恢复下一页位置。
- compact handoff 的 final/running/terminal 判断只读当前协议状态：`DONE` 是 final，
  `FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`CANCELLED`、`ABANDONED` 是 terminal；
  不能用 `succeeded/completed` 这类别名补齐。
- carried tool context 逐条恢复 operation/effect 字段。语义摘要折叠中段时，失败、运行中或 unknown
  的副作用另保留精确 authoritative facts block；摘要不能覆盖该块，也不能凭该块自动重试。成功记录
  继续由语义摘要、artifact refs 与唯一 operation store 承载，不把每次成功复制成第二份长账本。
- Compact/handoff 的机器统计桶只使用当前协议状态或 `unknown`；旧状态原文只留在 child row / refs
  里做证据展示，不能扩散成新的机器状态。
- compact apply 的 id、metadata、restore refs、bundle、ledger、self-check 和 context markdown 属于同一条
  apply 链路，集中在 `compact_apply/__init__.py`；`compact_apply/work_state.py` 只负责构建续接所需的
  work-state snapshot。
- compact 连续失败熔断在 `compact_circuit_breaker.py`：状态持久化在
  `workspace/compact/circuit_breaker.json`，连续失败达阈值即 open，冷却期内 `run_memory_compact_auto_cycle`
  跳过 apply 返回 `blocked_circuit_open`，避免 thrash loop 空烧；一次成功清零回 closed。
- compact resume 的入口集中组装 consistency、recommended paths、handoff、completion prompt 和 continue
  packet；`completion.py`、`handoff.py`、`focus.py`、`failsafe.py`、`blocked.py`、`io.py` 分别保留为真实职责边界。
- `compact_resume/focus.py`、`compact_state.py` 和 `compact_work_state/archive.py` 只消费结构化工具记录、
  coverage/cursor/refs；普通 summary、next_action 或多语言状态词不能证明读取范围完成。
- task-local fact 文件只读当前模板名：`ACCEPTANCE.md`、`CONSTRAINTS.md`、`TEST_CHECKLIST.md`、`failing_tests.json`。文件名必须精确匹配，不能因为 mac/Windows 大小写行为把旧小写文件当成当前事实源。

## Artifact And Raw Output

- raw turn/tool/gateway 流水写 `audit/YYYY-MM-DD.jsonl`。
- 大工具输出写当前 task `work/blobs/tool_outputs/`，prompt 中只放摘要和 refs。
- 用户最终交付写当前 task `output/`，或用户显式指定目录；task `output/work` 仍记录索引和验收。

## Design Rules

- 普通运行不从 repo `data/*` 读取事实源。
- 新字段优先建明确业务字段，不使用通用保留槽承载业务语义。
- 索引可以重建，不能替代正文事实。
- memory 只提供事实和检索，不做任务质量硬门。
- 状态别名必须 fail closed；需要迁移旧数据时写显式迁移记录，不在 compact 读取链路里临时猜。
- main context bundle 的结构化验收字段只认 `acceptance`、`constraints`、`latest_tests`；中文字段名和旧别名只作为普通用户文本保留，不进入机器验收合同。

## 2026-06-10 compact_context_bundle 合并

- `memory_archive/compact_context_bundle/`（match.py / refs.py / `__init__` 转发）合并为单模块
  `memory_archive/compact_context_bundle.py`；对外导入路径不变（`from ..compact_context_bundle import ...`），
  匹配判定与 refs 读取在同一权威文件内。
