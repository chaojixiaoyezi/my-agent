# Memory Progress

## 2026-09-13 R285 后台历史种子三态 + 任务范围 + 递归等待对称

1. **读不到历史不再静默降级**：`_background_conversation_history_seed` 返回三态
   （`ready` / `unreadable` / `disabled`），异常与 load_errors 都带结构化错误；
   `_background_history_seed_or_raise` 对 `unreadable` 抛 `BackgroundHistoryUnavailableError`
   （error_code `BACKGROUND_HISTORY_UNAVAILABLE`，带 load_errors/detail）→ 本片失败、唤醒不确认、可重试。
   以前 `except Exception: return None` 会让调用方 `include_recent_messages=True` 退回有界摘要继续跑模型，
   把"历史读取失败"伪装成"上下文骤降"。
2. **复用既有结构化任务范围**：种子的行选择改为先取 `_context_bundle` 的任务范围投影
   （detached named task 的创建锚点 + 精确 lineage），再按 `message_id` 过滤未压缩行；
   范围为空即合法空历史。此前直接吞全 thread 未压缩行会把创建锚点之后、属于别的任务的消息带进
   detached 工作。投影只走历史两步（行选择 + provider 消息），不牵入 recent_artifacts。
3. **递归等待与 root 对称**：`task_local_wait_response_for_open_subagents` 保留模型真实正文与真实
   turn-end（不再强制 `interrupted`），等待只作为 `direct_child_wait` 依赖事实 + `SUBAGENTS_ACTIVE`
   状态；删除已无用的 `queue_interim_reply_for_open_subagents` 空壳入口、导出与旧注释，不再维持两套语义。

## 2026-09-13 R283.1 迁移错位修复（真机验收发现）

第一版 LF 边界迁移用正则批量替换，有两处包装错位：`jsonl_lines(reversed(text))`（storage 写后回读校验）
与 `jsonl_lines(enumerate(text), start=1)`（control_plane 只读投影）。前者让 `jsonl_lines` 收到 reversed 对象，
抛 `AttributeError: 'reversed' object has no attribute 'split'`；.10 真机 TUI 多子代理验收里，子代理
`live_archive` 写后回读走这条路径 → runner 直接 FAILED（`runner 执行失败: 'reversed' object has no attribute 'split'`）。
两处已改为"先按 LF 切记录、再 reversed/enumerate"，并为两条路径补 NEL 守卫测试。
**教训**：批量迁移必须逐点复核包装层次，不能只看替换后的文本是否"像对的"；修复后必须真机重放。

## 2026-09-13 R283 JSONL 记录边界统一为物理 LF（真实事故修复）

**事故**：子代理 transcript 的 JSON 字符串里含 U+0085(NEL)，`path.read_text().splitlines()` 在 NEL
处把一条完整记录切成两条 → 2026-09-13 三个 child（`subagent-1789309101-d6832a68/-72be5b55/-ec2d50a9`）
的 `runner_result`/`final_report` 报 `conversation transcript is unreadable`，`append_message_once`
因 `recent_messages_report` 的 load_errors 抛 `DataCorruptionError`，整个 child 判 FAILED。
同一批文件按物理 LF 读：175/162/154 条记录、0 错误；按 `splitlines()` 读：190/163/158 行、19/2/5 个 JSON 错误。

**修复边界**：唯一实现 `common/json_io.py::jsonl_lines()`——只按物理 LF 切记录（末尾容忍一个 `\r`），
保留字符串内的 NEL/U+2028/U+2029/VT/FF/FS 等字符；**不清洗字符、不吞坏行**。真正的半行、截断、
非法 JSON、非对象行仍然逐条产生结构化 `load_errors`（守卫测试两侧都锁）。所有 JSONL 读取点统一改用它，
包括会话账本全量/尾部倒读、协作账本、审计账本读取与重写、memory_archive 各分片、gateway history/late/
http 事件、takeover readiness、CLI resume、identity/daily memory。会按行重写文件的路径（审计清理、
task workspace 摘要同步）同样改用它，避免"读时切开、写回落成 LF"的静默改写。

## 2026-09-12 R264 策展超时的真根因（更正 R262 的缩批假设）+ 失败路径尝试形状可观测

- **更正**：R262 把"策展超时"归因为输入规模并加了缩批，真机证据不支持这个方向。
  `processed_messages=0 / processed_audit_events=0` **不是**"空批"证据——那两个字段只在成功提交时写
  （`curator.py:582-583`），失败记录用的是 dataclass 默认值；而且空批在 `curator.py:420-423` 直接短路成功、
  **根本不调模型**，所以"空批 + ProviderTimeoutError"本身自相矛盾。
- **真根因（两层）**：①可观测缺陷——缩批只在成功路径落盘（`curator_backend.py:188-189` 失败时把尝试账整个丢掉），
  失败账只有一条 `failure_diagnostic=`，无法判断缩批是否被触发；②运行环境——策展跑在**单槽本地 llama-server**
  （`--parallel 1`、3bit GGUF、Metal、~16-22 tok/s）上，与主对话**争同一槽位**，90–270s 的挂钟超时必然先到；
  实测同形状请求走远端真实端点 **2.0s 成功**，消掉"schema/prompt 形状本身超时"的假设。故缩批改不动结果。
  今天 17 轮失败里有 8 轮时长集中在 484–489s（极稳定指纹）。
- **本轮修复（只补可观测，不改语义）**：`CuratorModelAttempt` 每次模型调用记一条
  （attempt / prompt_chars / schema_chars / granted_seconds / elapsed_ms / shrunk / outcome=异常类名），
  ContextVar `_LAST_MODEL_ATTEMPTS` + `last_model_attempts()`；`curator.py` 的 `_attempt_shape_warnings()`
  把每条编码成 `curator_model_attempt={...}` 走既有 warnings（键序固定、自限 300 字符/32 条），
  原 `failure_diagnostic=` 仍是最后一条；另加同口径 logging.warning 便于真机即时定位。
  **绝不记 prompt/schema/响应正文**；未新增 run 账 dataclass 字段（有 `set(to_record())==set(__dataclass_fields__)`
  断言，防再次踩 `CURATOR_RUN_AUDIT_FAILED`）。语义护栏：只有 `call_backend_with_timeout` 的失败算"尝试失败"，
  `parse_curator_extraction` 的宿主契约异常仍直接上抛（不缩批、不多打调用）。
- 证据：`test_curator_timeout_observability`（新增 8 例）等三套 71 passed；扩展回归 57 passed；ruff 通过。
- **未做（需决策）**：候选修法 ①给策展显式指定 provider/model（不走 owner selected profile，指向真实网络端点；
  实测 2.0s）——属"用哪个模型"的用户选择，未擅自改；②`--parallel` ≥2 或让长请求不占满槽位——环境改动；
  ③端点占用**快速失败**并如实留证（不空转 8 分钟、不伪装超时）——会改变 run 行为与 warnings，先给方案未实现。
  真机生效需重启网关（不在本轮职责内）。

# Memory Progress

## 2026-09-12 R262 策展超时改为有界自适应缩批

- 真机诊断（r260 上线后首轮 run 落盘）：`failure_diagnostic={"error_type":"ProviderTimeoutError"}` —— 第三个原因
  是**模型调用超时**（非 schema、非鉴权）。对照实验：同 profile 手工小 prompt 调 `generate_structured` 秒级成功，
  问题出在策展的大批量输入（batch 80 条 / 40k 字符）。
- 修法（不靠加长 timeout）：一次调用超时后**缩小输入重试** —— 消息与审计各自独立地"向上取半"截断**前缀**
  （8→4→2→1），下限 1（`_TIMEOUT_SHRINK_FLOOR`，到下限不再发调用，绝不空批）；独立上界
  `_TIMEOUT_SHRINK_LIMIT=3` 与 `config.max_retries` **互不污染**（超时只扣缩批计数，非超时错误只扣同输入重试）；
  另加 lease 时长护栏（缩批产生的总调用预算不得超过租约，防租约中途过期）。
- 游标/事务安全：缩批只做内存输入整形，不写游标/账本；只截前缀，`next_cursors` 仍只推进连续 processed 前缀，
  被丢尾部留到下一轮重放；提交仍只有 `_commit_batch → committer.commit` 一条路径。成功且真发生缩批时，
  run 账追加 warning `memory_curator_input_shrunk:N`（便于真机核对）。
- 证据：`test_curator_timeout_adaptive` 新增 12 例；连同 curator 相关套件 70 passed；附加回归 152+34 passed；
  变异验证（`_TIMEOUT_SHRINK_LIMIT=0` → 4 例红；去掉护栏 → 预算 2520s > lease 1530s）。
- 未做：真机确认（需等下一轮 curator 出成功行 + shrink warning）；"单条输入本身即超时"属有意边界（缩到 1 条
  仍超时则按原语义 typed 失败）。

# Memory Progress

## 2026-09-12 R257 策展失败账记录可判定形状
- **回归与更正（同日）**：第一版给 `CuratorRunRecord` 新增 `failure_diagnostic` 字段，触发两处连锁故障，
  真机把失败记账本身变成 `CURATOR_RUN_AUDIT_FAILED`：① `from_record` 要求存盘键集合与 v2 schema **完全一致**，
  新增字段让所有历史行校验失败；② 回退字段后 `warnings=(X if cond else ())` 是分组不是元组，传入字符串命中
  `warnings must be an array`。最终实现：**schema 一字段不加**，诊断以 `failure_diagnostic={json}` 写进既有
  `warnings`（v2 字段、≤300 字符/≤32 条、可解析）；测试锁定"不得新增字段 + 编码可解析 + 真实 14 行历史分片可追加"。

- 真机故障：占位模型修好后，run 账从 `echo/gpt-4o-mini` 变成真模型（`anthropic_compatible/qwen3.8-flash`），
  但失败码换成 `CURATOR_MODEL_FAILED`。该码是 `_failure_code` 的兜底分支，而供应商异常正文按红线不落盘，
  于是账本只剩"失败了"，无法区分超时、限流还是请求被拒——这正是本轮卡住的地方。
- 修法：run 账新增 `failure_diagnostic`（只含 `error_type` 异常类名，以及异常自带时的
  `provider_http_status`）；无状态码时不写该键，不伪装成已知。prompt、供应商正文、记忆内容仍不落盘。
- 测试：`test_memory_curator_v2` 新增一条，锁定字段内容且断言供应商文本不出现在诊断里。
- 未做：拿到诊断后对第三个原因（`CURATOR_MODEL_FAILED`）的修复；需下一次 run 落盘诊断后再定性。

# Memory Progress

## 2026-09-12 R256 迁移预检的稳态短路：后台维护不再每次递归整棵 owner home

- 现场：用户本机网关（runtime-r255）空队列空闲时仍稳定烧 **27%~45% 单核**（10 分钟窗口均值 26.8%、
  中位 21.4%、峰值 70.3%；另有 90/100 秒窗口 16.58%/19.09% 与瞬时尖峰 ~50% 的独立采样）。
- 归因（采样 + 一手计时）：热点在 `memory-curator_*` 线程的 `lstat`/`opendir` 递归遍历。链条是
  策展 `run_if_due → run → _preflight_migration → MemoryMigrationService.apply() → _scan() →
  _new_migration_scan_state → _safe_named_dirs(owner_home, "memory_gate"/"learning_drafts")`，
  而 `_safe_named_dirs` 对**整个 owner home** 做 `rglob`。本机实测该 home 规模：`agents/` 42.4 万条目
  （90 秒未走完）、`tasks/` 17.6 万（90 秒未走完）、`data/` 16.0 万（57.7 秒）。
  **`rglob("memory_gate")` 单次 428.0 秒、`rglob("learning_drafts")` 单次 157.9 秒，合计约 586 秒**，
  与当日一次策展 run 的实际耗时 10 分 30 秒吻合。
- 触发频率：策展状态长期带 `pending_reasons: [interval, task_complete, session_close]`，且
  `last_failure_code=CURATOR_SCHEMA_INVALID`（最近一次成功在 09-09），于是每约 8 分钟重试一次，
  每次都先走一遍上述分钟级遍历 → 线程几乎一直在走文件系统。
- 修法（**只改热路径 `apply()`，不动迁移语义**）：marker 读到同一迁移版本的 `complete`、且
  `_legacy_sources_at_contract_paths` 在契约位置（owner home 根、`data/`、`tasks/<date>/<task>/work/<name>`、
  管理员显式传入的工作区根、以及上次 complete 扫描记录过的位置）没有发现遗留目录时，直接返回
  `already_current`，不再 `_scan()`。marker 缺失/读坏/schema 版本不同/任一契约位置命中 → 一律回落完整扫描。
  marker 新增记录本次确认过的 `legacy_gate_dirs`/`learning_dirs`，供下次点名复查。
  `plan()`（dry-run）保持完整扫描不变，诊断完整性不受影响。
- 真机成本对照（只读实测同一 home）：稳态探针中位 **443 ms**（冷启动首测 4.6 秒），
  对完整扫描的 **586 秒**。契约形状取自迁移测试与历史写入方：
  `tasks/<date>/<task>/work/memory_gate`。
- 边界与未做：`memory_gate`/`learning_drafts` 的**写入方已被删除**（`subagents/manager.py:367`），
  所以"complete 之后不会长出新遗留目录"是当前事实，不是永久假设；若将来恢复写入方，必须同时复核
  本探针的契约形状。**另发现（未修，属模型解析，需与该线负责人协调）**：策展后端由
  `_build_memory_curator_backend` 从基础 config 解析，实测拿到 `provider=echo / model=gpt-4o-mini`
  占位默认（run 账可见），因此抽取输出永远过不了 schema → `CURATOR_SCHEMA_INVALID`；后台主代理链路
  已按 canonical thread 走 `selected_model_scope`，只有策展没有。

## 2026-09-09 R223 记忆、历史与 Compact 复核

- add 删除 LCS 相似度覆盖，只做精确重复去重；更新依赖明确 entry_id/version。记忆仍由各 owner 的
  Agent 自主维护，只有 SOUL 等待用户确认。两个服务端口的真 TUI 独立保存及精确更新均通过。
- 嵌入模型构建和向量读写失败公开 degraded/operation/error_type，日志不写密钥或异常正文；
  JSONL 正式记忆保留，某个操作恢复不能掩盖另一个操作仍失败。
- `session_search` around 模式按锚点 thread_id 限定上下文；旧记录没有身份时明确是时间邻居，
  不再把并发会话记录包装成同一段对话。
- 删除按输出 max_tokens 触发的旧 `_compression_service` 拼接路径及无调用包装，保留唯一正式
  ConversationStore/checkpoint/CAS 压缩链；pre_compact 策展接到真实摘要入口。
- native IR 使用单调预算估算器二分寻找最短可退休前缀，保留用户原话、当前事实和工具配对。
  128 对记录的回归估算次数不超过 10；不新增模型请求、不承诺任意 provider 的缓存 TTL。

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
