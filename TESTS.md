# 测试与发布验收

## 向量缓存第五轮：「跳过写」真的实现 + 回收错误可见（2026-09-29，分支 `my-agent/self-dev-2-vcache`，基于 `bac2f176d`）

**起因**：9b 复核 `74da81687` 发现「盘上没变就跳过写」**根本没实现**——`_flush` 仍无条件整文件重写
（探针 V4 实测删一个盘上不存在的键时 inode 照样变），而注释、TESTS.md、模块文档都写着已实现。
dev 裁定**真的实现它**，并顺手修 V5。**这一轮的第一件事是承认：一份和代码对不上的汇报比不做还糟。**

- **V4 实现**：`_flush` 在**文件锁内**记下改动前的盘上内容，改动后若完全一致就跳过原子替换，
  但仍用盘上内容刷新 `_items`（本实例内存视图与盘上保持一致）。
  判据必须读盘上内容，**不能用本实例内存**——另一个实例写的键本实例内存里没有、盘上有（P5d）。
  - `test_v4_remove_absent_key_does_not_rewrite_file`：删盘上没有的键，断言 **inode 与 mtime 都不变**。
  - `test_v4_remove_present_key_still_rewrites`：对照，删真有的键必须真的落盘。
  - `test_v4_noop_remove_still_refreshes_memory_view`：跳过写时内存视图仍要跟上盘上。
- **V5 实现**：缓存文件读不出内容时（坏 JSON），`_load` 记 `last_read_error`；
  `reclaim_text_cache_orphans` 改为返回 `(回收数, 错误说明)` 并把读/写错误带出来，
  维护状态里的 `text_vector_cache_reclaim_error` 不再是永远的空串。
  - `test_v5_reclaim_reports_error_when_cache_corrupt`：坏缓存时错误说明必须非空。
  - `test_v5_reclaim_empty_error_on_clean_run`：对照，正常时必须是空串（否则"分得开"没意义）。
- **注释与文档改成与实现一致**：`remove` 的注释、TESTS.md 上一版那段"自我纠错"、
  `docs/modules/memory/{02-progress,04-structure}.md` 都补了更正说明，不悄悄删掉旧说法。
- **验证**：定向 **50 passed**；变异 `scripts/mutate_text_vector_cache.py` **23/23 KILLED, 0 survived**
  （新增 **MY9** 去掉跳过写、**MY10** 跳过写判据改成只看内存、**MY11** 回收不带错误、**MY12** 坏 JSON 不记读错误）。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_memory_vector_cache.py agent_py_agent/tests/test_owner_maintenance.py -q`
  与 `python3 scripts/mutate_text_vector_cache.py`（cwd 都必须是工作树根）。

## 唤醒毒丸：二次复审跟进（2026-09-29，分支 `claude/75-wake-poison-design`，在 `019e1d9af` 之上，仍未接线）

- **时钟回拨**：`test_wake_poison.py::TestClockRollback` 回拨 60 秒后再记失败、只重投失败、不计数与批次失败、提醒，
  状态都能经 `from_dict` 读回，"最近一次"时间不倒退（失败与只重投的退避也从不倒退的时间算）；提醒时间不早于段起点，
  也不早于上一次提醒。存储层 `test_a_60_second_clock_rollback_keeps_the_ledger_readable` 用真实尝试账连记三次（1000、940、900），
  `state_report` 无错误、同因次数照常累加。
- **空原因**：计数失败、批次失败、只重投失败的原因为空或全空白都抛 ValueError（6 组），不计数与成功不需要原因；
  存储层空原因被拒且尝试账逐字节不变。
- **写前校验**：`ensure_writable_state` 与读回同一口径（3 种矛盾状态被拒）；把判定层替换成返回矛盾状态后，
  `record` 与 `begin`（上一次尝试进程已死、需补记 abandoned）都抛 ValueError，尝试账逐字节不变。
- **环境级**：402、404 加进不计数用例（共 7 种）；400/413/422 仍计数。
- **批次失败推进不计数段**：批次失败带原因推进段次数与起点，一直批次失败满 24 小时照样给出提醒；原"其它类型不结束该段"
  用例按新语义更新（批次失败后段次数为 2）。
- **变异**：18 个（时间单调 5、退避起点 2、原因必填 5、批次推进 1、环境 2、写前校验 3）全部被抓住。
- 两个文件现为 122 + 22 = 144 例。

## 唤醒毒丸：三条复审设计修改（2026-09-29，分支 `claude/75-wake-poison-design`，基于 `12a532f8b`，仍未接线）

- **环境级故障不计数**：HTTP 401/403/407（`provider_error_http_status`）与 `ProviderConfigurationError` 基类（含连接错误、
  缺模型配置）归为不计数；`ProviderRequestRejectedError` 的 400/413/422 照样计数。原因码有结构化 HTTP 状态时加 `:http_<状态>`。
- **批次隔离**：`verdict_for_batch` 把批大小 > 1 的计数失败改写为批次失败（不计数，`batch_failures` 加 1，30 秒后逐条再试，
  `needs_isolation` 为真直到成功）；尝试账 in_flight 记 `batch_size`，批次中途进程死亡也只记批次失败。
- **连续不计数的远端提醒**：不计数结果带原因码并组成连续段（次数、起点、最近原因）；满 24 小时（>=，与只重投同一窗口）
  `stall_alert` 给出提醒，`record` 在同一把锁里记账（`mark_stall_alerted`）并随结果返回，之后每满 24 小时再提醒；
  计数失败结束该段，成功清空，永不结案。
- **测试**：`test_wake_poison.py` 与 `test_wake_attempt_store.py` 共 125 例，新增环境级 5 种不计数、3 种请求级计数、
  批次改写与校验、批次不结案且隔离到成功、不计数段推进、提醒恰好在窗口触发并按窗口重复、计数失败结束该段、
  窗口常量与只重投窗口相同、4 种新的一致性破坏；存储层批次中途死亡只隔离、提醒写盘且同一窗口只提醒一次。
- **变异验证**：本次 28 个全部被抓住；第 1 步全集 43 个、第 2 步全集 21 个在新代码上重跑，全部被抓住
  （`ModelNotConfiguredError` 现在也被配置错误基类挡下，用例补了 `ModelProfileError` 以区分模型配置判定）。
- **回归**：唤醒、发布层、观察、保留扫描、存储布局、快照与 skill_search 相关 89 个测试文件：2545 passed、1 skipped、
  25 xfailed。code-size 与 origin/main 逐条比对新增 0。

## 唤醒毒丸与 error_code：9a 复审小改（2026-09-29，分支 `claude/75-wake-poison-design`，基于 `12a532f8b`）

- **error_code**：
  - 结构化错误码的形状只在一处定义：`runtime_errors.is_structured_error_code`（大写字母开头，只含大写字母、数字、下划线）。
  - `SkillSnapshotError` 构造时运行时再核一次形状，不合格回落到 `SKILL_SNAPSHOT_ERROR_CODE_INVALID`，原值只放进给人看的消息。
  - `skill_search` 的 `get` 读正文失败时，回执也带 `details.error_code`。
  - AST 守卫：扫描整个 `agent_py_agent`（排除 tests）；别名导入与子类迭代并入族名再扫；码参数认第一个位置参数或
    `error_code=` 关键字；变量码的放行收窄到（文件，函数，变量）。
- **wake_poison**：8 个函数补 LLM 层注释；原因码里的 `error_code` 用同一条形状规则过滤（空白、小写、带明细的
  回落到 category+类名）；`next_poison_state` 遇到未知 kind 抛 ValueError；"执行了但没报告"改由
  `verdict_for_missing_report` 单独表达，`verdict_for_report(None)` 抛 ValueError；`from_dict` 拦 NaN/inf、未知键，
  以及计数与时间互相矛盾（同因大于总数、原因码与次数不一致、没失败却有时间、首次晚于最近一次）。
- **测试**：`test_skill_snapshot_error_codes.py` 26 例（守卫正反样例含别名、子类、关键字、同文件他函数；运行时 6 种坏码；
  `get` 回执）；`test_wake_poison.py` 增加形状过滤、未知 kind、missing report、失败与重投时间逐步推进、10 种坏状态；
  类型用例建立在自洽的基础状态上，避免被一致性检查盖住。
- **变异验证**：wake_poison 全集 43 个（原 26 个 + 本次 17 个，含 9a 列出的只重投退避推进、首末失败时间、只有空白的
  error_code）与 error_code 9 个，全部被抓住。

## 唤醒毒丸第 2 步：尝试账、结案、重放与发布层第三位置（2026-09-28，分支 `claude/75-wake-poison-design`，基于 `12a532f8b`）

- **范围**：新文件 `agent/conversation/store_wake_attempts.py`，以 `store.wakes.attempts` 挂在 WakeStore 上（`mark_handled`
  的签名与行为不变）；`store_layout.py` 加尝试账／结案／留档路径；发布层把结案当作第三个安装位置，接受
  `failed_permanently` 状态，同键再发布返回原结案信号；会话删除时的保留扫描一并收走这三类文件。仍未接线。
- **测试**：`test_wake_attempt_store.py` 16 例，全部走真实 `ConversationStore`：失败累计到上限给出结案判定、成功删账、
  不计数只清 in_flight；上一次尝试的进程确认已死时补记 `attempt:abandoned`，存活或无法判断（别的主机）都不补记；
  坏账报数据损坏且原文件不动；结案后 pending 消失、冻结内容不变、关联观察不再出现在待处理观察里；同键再发布不复活
  （对照：`mark_handled` 后同键会开新一代）；列表只含结构化字段、坏记录进 load_errors；重放写回原信封、留档
  `replayed/<id>/1.json`、清掉残留旧账，二次结案再重放留档到 2；三种拒绝（领域已终态、pending 冲突、找不到）不改任何文件；
  会话删除只收本会话的尝试账和留档。
- **变异验证**：21 个变异体全部被抓住（不结观察、不删 pending／旧账、无法判断当成死亡、不补记 abandoned、成功不删账、
  不清 in_flight、坏账静默清零、重放忽略领域／冲突、不留档、不删结案记录、列表泄露摘要、重放次数写死、发布层三处、保留扫描三处）。
- **回归**：涉及唤醒队列、发布层、观察、保留扫描、存储布局和快照错误的 66 个测试文件加护栏：1885 passed、1 skipped、25 xfailed。
  code-size 与基点逐条比对：新增 0；保留扫描的唤醒文件收集拆成 `_thread_wake_files`／`_wake_file_thread` 后，
  `_conversation_related_paths` 原有的两条 high-risk 一并消失。

## 唤醒毒丸第 1 步：纯函数判定模块（2026-09-28，分支 `claude/75-wake-poison-design`，基于 `12a532f8b`）

- **范围**：新文件 `agent/conversation/wake_poison.py`，只做判定、不接线：admission／异常／报告三类结果分类，
  同因连续段与总次数，计数失败与只重投两类退避，结案判定（同因 5 次、总 12 次标 mixed、只重投满 24 小时）。
  常量是内部安全兜底，不进配置。接线要等 my-agent-3 的取消修复合入。
- **测试**：`test_wake_poison.py` 60 例，正反成对：预期等待 admission 逐个不计数且集合与合同完全相等、未知码默认计数；
  15 种瞬时类型逐个不计数（含 SQLite 扩展码 517）；其余异常按 `error_code` 或 category+类名计数，换文案不改变原因；
  上限恰好在第 5／12 次触发、换因重计而总数照加、中间夹不计数结果不打断、成功清账；退避 30/60/120/240/300 与
  重投 30…900；重投失败不进失败计数、满 24 小时恰好结案；状态读写严格校验；最后用纯函数重放 `7b83c8730`
  （5 次领取、间隔合计 450 秒后按 `error:SKILL_TASK_BINDING_INVALID` 结案）和 `204f4ddf9`。
- **变异验证**：26 个变异体全部被抓住（含预期等待集合少一项、瞬时类型各删一项、SQLite 不取低 8 位、忽略 error_code、
  换因不重计、不计数结果清账、退避按同因而非总数、上限用 > 代替 >=、mixed 标错、重投计入失败、各封顶值改动、状态校验放宽）。

## 插话幂等回执指纹兼容旧口径，坏账回执不再被静默重写（2026-09-29，分支 `claude/38-guidance-receipt-digest-compat`，基于 `bac2f176d`）

- **来源**：ae 核对 step16g 时发现 input_receipts 里一条 09-27 的插话回执 terminal_unknown + DataCorruptionError「input digest mismatch」，每 15 秒被重写、
  不进任何计数；根因是 a646a4885 改了 `_guidance_input_digest` 的口径没兼容旧回执。
- **用例** `test_guidance_receipt_digest_compat.py`（6 条，结构化判定）：固定样本的 v1/v2 指纹十六进制钉死（v1 用冻结在测试里的旧算法独立算一遍，
  产品的 v1 或 v2 再被改动都变红）；用旧口径写的真实形状回执（无版本字段）能读出、同键重试回同一条、同键异文仍报错；a646a4885 之后本修复之前
  写的无版本 v2 回执也能读；记了版本 2 却存 v1 值的回执被严格拒绝；对账遇到旧口径回执且目标任务已结束时自然收敛为排队并进 inbox、
  `terminal_unknown_errors=0`；真坏账（正文被改）保持 terminal_unknown，第二轮对账文件字节不变、`terminal_unknown_errors=1`、
  `raise_if_input_reconcile_unsettled` 抛出 category=data_corruption 的结构化异常。
- **负向验证**（改坏产品语义，独立子进程运行，逐字节恢复核哈希）：校验只认当前口径；记了版本也任一匹配；v1 口径漂移；同键重试只认当前口径；
  新回执不记版本；终态未知每轮重写；带错误的终态未知不计数；不抛给循环守卫。结果见提交说明。
- **门禁**：新文件 + runtime_guidance + steer_delivery_recovery + session_message_busy_target + gateway_control_operation + tui_control_delivery
  + chat_client_context + adapter_manager + tui_input + dispatcher/loop 韧性 + session_task turn_binding/body_replay + 九个全仓守卫；
  ruff、doc sync、strict code-size、`git diff --check`、clean package；`CODE_SIZE_REPORT.md` 不入提交。真机未复验，部署后看那条回执是否转排队。

## 前台命令已退出、只是清理未确认（2026-09-29，`84e8db619` + `067d2dd3e`，单独集成）

- `test_shell_foreground_cleanup.py`：原用例 `test_cleanup_unknown_keeps_original_command_result` 按新合同改为 `test_cleanup_unconfirmed_returns_the_real_result_with_a_warning`（退出码 0 → 成功，7 → `COMMAND_FAILED`/`failed`；`cleanup_confirmed=false`、`termination` 回执和正文提示），另加清理已确认不带告警的对照。
- 确定性复现：真实子进程正常跑完，只把 `terminate_process_tree` 的核对结果固定成未确认（模拟高负载下清理确认超时），`effect_outcome` 不是 unknown；再经工具操作账本走一遍，记录按真实结果结算为 `succeeded` / `failed`，没有 `unknown_reason`。这四条在旧代码上全部失败。
- 真正未知的对照沿用 `test_shell_orphan_kill.py::test_generic_timeout_does_not_claim_termination`（超时且没有终止回执仍是 unknown）。
- 变异 7 个（含整段恢复成旧的 UNKNOWN 行为）全部被抓住。
- 措辞分两种（`067d2dd3e`，9a 复审跟进）：`identity_unavailable`、`identity_changed` 时一个信号都没发，参数化断言“[进程清理未尝试]”措辞和进程号上限（前 8 个加“等”，第 9 个不出现）；通用“清理未确认”用例改用发过信号的 `SIGTERM->SIGKILL` 回执；另有没有进程号时不列的对照。变异 8 个全部被抓住，`84e8db619` 原 7 个按新锚点重跑也全部被抓住。
- 复现：`python3 -m pytest agent_py_agent/tests/test_shell_foreground_cleanup.py agent_py_agent/tests/test_shell_orphan_kill.py -q`

## 会话互通真实链路门禁：同一对会话连发三条（2026-09-29，分支 `claude/ae-pair-message-window`，基于 `9a71d8cd5`，只改测试与文档）

- **起因**：复审 my-agent-3 的观察项②修复时，用探针发现同一对会话之间只能送达一条消息（见 DESIGN_LEDGER 会话间消息一节的已知缺陷）。
- **新增窗口** `test_repeated_messages_between_the_same_pair_are_all_delivered`：A 给空闲的 B 连发三条，第二条内容不同，第三条与第一条相同；每次发完都排空一次。断言：
  - 三次发送都成功，没有结果未知，发送方这一轮不收口；
  - 三个消息 id 互不相同，各有独立回执；
  - 工具返回的 status 等于这条回执在发送当时的真实状态；
  - B 在各自的唤醒回合里看到了每一条。
  - 前提“第一条送达”用 `_require`：第一条在 main 上本来就应当送达，缺了就是链路断了，不能被 xfail 吞掉。
  - 假线路的调用记录多了 `notes` 字段，即本回合出现的 RC-NOTE 标记，用来区分看到的是哪一条。
- **自证**：
  - main（`9a71d8cd5`）上是 XFAIL。`--runxfail` 挂在第一条断言，三次发送的错误码是 `['', 'TOOL_OPERATION_OUTCOME_UNKNOWN', '']`。
  - 在导出里注入按单条消息去重（去重键带随机后缀）后是 XPASS(strict)，`--runxfail` 1 通过。
  - 叠加 my-agent-3 的 `fd28c2a7a`（含空闲消息确认与已消费判据修复）：不注入仍是 XFAIL，注入后转正。可见只修第 16g 批转不了正，必须修去重。
  - 全文件 main 上 7 通过、7 xfail，连跑 3 次一致。
- **未覆盖**：同一次工具调用的重试去重。修去重时要保留“同一次调用重试返回同一条”，这一窗管不到。
## global_index 只追加索引的内部压缩（2026-09-29，分支 `my-agent/self-dev-2-index`，基于 `9a71d8cd5`）

### 复审跟进（38）：锁内再核身份、键定义单一来源、失败原因单列（2026-09-29，基于 `bac2f176d`）

- **读取侧也要从注册表派生（9b 第二轮）**：上一轮「键定义唯一权威」只覆盖了写入侧和压缩侧，
  **读取侧仍各写一份**——`_IndexSpec` 三处（dangling 检查）与 `latest_*_refs_report` 两处。
  9b 的变异 **MH12**（把读取侧 runs 的键改成带 `task_id`）在 18 个相关文件下**全部存活**。
  这条特别要紧：**压缩的正确性取决于「压缩键」和「读取键」一致**——读取键一旦变宽，
  压缩会把读取侧视为不同的行合并掉，而没有测试会发现。
  - 修法：读取侧全部改成 `key_fields_for_index_file(...)`；把 dangling 的三处抽成
    `_dangling_index_specs()` 让测试能直接核对。
  - 新增 `test_reader_side_key_fields_equal_registry`：用 spy 从真实读取入口反查实际传下去的键，
    并核对 `_dangling_index_specs`。**MH12 现在真被杀掉**。
  - 变异脚本顺手加 `Mutant.path`：变异体可落在 `home_indexes.py`（此前只支持默认文件，
    MH12 的锚点在别的文件里，第一次跑报的是 ANCHOR MISS 而不是静默放过）。
  - 卫生：脚本跑 pytest 加 `-p no:cacheprovider`，不在树根留 `.pytest_cache`。

- **必须改：文件身份核对原本在锁外，rebuild 换文件的窗口没关严。**
  `_still_same_file` 通过之后才进 `_append_tail_and_replace` 拿锁，而 rebuild
  （`home_indexes.replace_home_index_snapshots`，同在 `locked_json_path` 里原子替换）恰好能落在
  「核对通过 → 拿到锁」之间：实测 `compacted=True`、读回 `['B','A']`，rebuild 写的 `REBUILT` 被陈旧前缀盖掉。
  上一版的 `test_rebuild_replacing_file_aborts_compaction` 把换文件注入在**扫描阶段**，
  只覆盖核对之前的窗口；MH8 也只能杀「完全不核对」。
  - 修法：把 `_still_same_file` 挪进 `_append_tail_and_replace` 的**锁内**（拿到锁之后、`_copy_tail` 之前），
    不一致就返回 `identity_changed` 并丢弃 tmp。锁外那次保留——它挡的是扫描期间被换文件，两道作用不同。
  - `test_rebuild_between_identity_check_and_replace`（38 探针转正）：只在前缀快照后的第一次核对里换文件；
    修前 `['B','A']`，修后 `compacted=False / identity_changed / ['REBUILT']`。
  - 变异 **MH10**（锁内不再核对）必须被杀掉。
- **建议改 a）失败原因单列**：`_compact_global_indexes` 原来 `if not result.compacted: continue`，
  `io_error` / `identity_changed` 这类"试了但失败"被静默丢掉，`maintenance.json` 分不清
  「没到期」和「压失败」。现在返回 `(成功摘要, 失败摘要)`，失败进 `indexes_compact_failed`；
  「没动手」的原因（`below_min_bytes` / `below_growth_ratio` / `in_cooldown` / `missing`）不算失败。
- **建议改 b）键定义单一来源**：四份索引的 key_fields 原先在 `home_indexes` 调用点、
  `home_index_compact._INDEX_KEY_FIELDS`、测试常量三处各写一份，spec 一改就漂移。
  现在 `home_indexes.INDEX_KEY_FIELDS_BY_FILE` 是唯一权威，写入侧与压缩侧都从它派生；
  加 `test_key_fields_come_from_home_indexes_spec` 逐一比对。变异 **MH11** 必须被杀掉。
- **变异 12/12 KILLED, 0 survived**（新增 MH10 / MH11）。

### 上线说明（生产实测尺寸，38 只读 stat 得到）

生产 `~/.my-agent/global_index/` 下四份索引：`active_tasks` **354.8 MB**、`active_agents` **284.3 MB**、
`active_runs` **282.2 MB**、`owners` **53.9 MB**。部署后第一个维护 tick 会依次压前三份
（都超过 64 MB 阈值且无压缩记录），按实测约 200 MB/s 合计约 **4.5 秒**；
**临时文件需要与压缩后大小相当的空间**（压缩后远小于原文件，实测 1 GB → 4.1 MB，
但峰值仍需容纳临时文件 + 读缓冲）。

- 压缩路径实测：1.00 GB 假 jsonl（494 万行、2 万 key）→ 输出 2 万行 / 4.1 MB，耗时 **16.3 s**、峰值内存 **42 MB**；
  145.4 MB 文件版本峰值增量 **22.9 MB / 0.7 秒**（第一版 952 MB）。

- **来源**：dsh-9b 的产品持久数据盘点（`9e6cd738e`，`docs/design/STORAGE_RETENTION.md` 第 5 项）
  指出 `global_index/{active_tasks,active_agents,active_runs,owners}.jsonl` 合计 0.93 GB、只追加、
  只有手动 `home-index-rebuild` 才会压缩。
- **做法**：不读任何权威文件，只在索引文件内部按 key 保留最后一行；挂在既有 owner 维护 tick 上，
  不新开线程。触发是零扫描判据：`当前大小 ≥ max(64 MB, 2 × 上次压缩后大小)` + 6 小时冷却，
  上次压缩后的大小持久化到 sidecar（否则 Gateway 每次重启后第一次 tick 都会不受冷却限制地压一遍）。
  并发窗口按字节长度切：锁内记前缀长度 → 放锁做重扫 → 重拿锁把窗口期新追加的字节原样接上再原子替换。
- **新增** `agent_py_agent/agent/user_space/home_index_compact.py`、`tests/test_home_index_compact.py`（20 条）。
- **行序这条踩过坑**：读取侧 `_latest_unique_refs` 是「先 `reversed`，遇到某 key 首次出现就取」，
  所以压缩后文件行序必须按「每个 key 最后出现的先后倒序」写，读取结果才逐条相同。
  第一版按正序写，测试立刻打回——**测试拦得对**。
- **内存测试踩过坑**：第一版用 `bytearray` 攒整份输出仍是 O(文件)，峰值 25.9 MB > 文件 21.7 MB。
  改成流式写临时文件后降为 O(key)，实测 145.4 MB 文件 → 峰值增量 22.9 MB、0.7 秒
  （第一版 952 MB / 约 6.5 倍）。
- **⚠️ 命名空间的坑（dev 2026-09-29 01:42 提醒的同源问题）**：`test_peak_memory_bounded` 用
  `subprocess.run([sys.executable, "-c", ...])` 量 `ru_maxrss`，原来只设 `cwd=pkg_root`。
  `agent_py_agent` 是 **namespace package**：子进程只要 cwd 不在工作树根，就落到别处的 editable 安装
  （本机是 6 月那份旧源码树）。实测同一条导入语句：cwd=工作树根 `rc=0`；cwd=`/tmp`
  `rc=1 ModuleNotFoundError: No module named 'agent_py_agent.agent.user_space.home_index_compact'`。
  注意坏掉的是**子模块**导入，顶层 `import agent_py_agent` 照样成功，所以这种失败特别容易静默。
  修法：子进程 env 显式塞 `PYTHONPATH=<工作树根>`，`pkg_root` 改用 `Path(__file__).resolve().parents[2]`
  而不是 `Path.cwd().parent`（不依赖调用者 cwd）。
- **变异验证**：`scripts/mutate_home_index_compact.py`（11 个变异体，锚点/预期/说明都在脚本里）
  跑出 **10/10 KILLED, 0 survived**。四条曾经存活、暴露的是**测试盲区**而不是实现没问题：
  - **MH4（不接窗口期尾部字节）**：注入点原本打在**第一次**拿锁时，新行被算进前缀长度，
    `_copy_tail` 无事可做 → 断言假绿。改成在前缀快照**之后**注入才真正覆盖那条窗口。
  - **MH8（不做文件身份核对）**：原测试没构造「rebuild 换掉文件（inode 变）」，补了
    `test_rebuild_replacing_file_aborts_compaction`。
  - **MH9（坏行不计数）**：原测试只看读取结果，没看 `bad_lines`，补了计数的直接断言。
  - **MH3（空行当合法记录）**：**这个变异体本身是语义等价的**——去掉空行守卫后，空串仍会被
    `json.loads` 拒绝，走同一个 `except` 分支，行为不变。诚实结论：空行守卫是防御性的、不是承重的。
    换成两个真有机制可杀的变异体（非 JSON 对象的行、key 字段缺失时不填空串），并补上对应测试。
    **教训**：存活的变异体有两种——"测试没钉住机制"和"变异体没改变语义"，要分开判定，
    不能一律当成测试有问题。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_home_index_compact.py -q`
  与 `python3 scripts/mutate_home_index_compact.py`（两者 cwd 都必须是工作树根）。

## 进程工具只读查询在权威读不出时不再记成「结果未知」（2026-09-29，分支 `my-agent/self-dev-proc-effect`，基于 `9a71d8cd5`）

- **来源**：dev 09-29 01:15 任务。ae 复审 47bd37d20 时发现既有问题：`process_session` 在读不出后台进程权威时对所有动作一律记 `effect_outcome=unknown`，
  而 `unknown` 会让 `contracts/required_actions.settle_required_action` 把动作标成 `blocked`，主代理据此收口、整轮停止调用工具 ——
  可 `status/wait/network_status` 这类只读查询根本没有副作用，结果其实已知（就是没开始）。
- **做法**（错误码保持 `TOOL_OPERATION_OUTCOME_UNKNOWN` 不变，只改 effect；判定只看**结构化事实**，不看错误文案）：
  - 新增模块常量 `_READ_ONLY_ACTIONS = frozenset({"list", "status", "wait", "network_status"})`；
  - 只读动作 → `not_started`；
  - `stop` 在读记录阶段就失败（`ProcessSessionAuthorityError`，即 `_visible_record_locked` 一开始就抛）→ 也是 `not_started`（进程树还没被碰过）；
  - `stop` 在可能已发信号之后失败（`ProcessSessionCleanupError`，或 stop 回执未确认）→ 仍是 `unknown`。
- **测试**：新增 `agent_py_agent/tests/test_process_read_only_not_started.py`（11 项），含一组**经真实收口函数**的验证
  （`build_effective_contract_snapshot` + `settle_required_action`，断言 `not_started` 不会把动作标 `blocked`），
  并配一条对照：`unknown` 仍然会落 `blocked`（收口机制本身没被放宽）。
- **变异验证**：把 `not_started = read_only or (...)` 改回 `not_started = False`（等价于一律 unknown）后，
  **6 条测试同时变红**，改回即绿。
- **既有用例更新**：`test_process_unknown_reason_codes.py::test_process_session_failures_report_their_cause`
  的 `authority-unreadable` 分支期望从 `unknown` 改成 `not_started`（这是本次任务要改的行为本身），
  `cleanup-unresolved` 分支保持 `unknown`。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_process_read_only_not_started.py agent_py_agent/tests/test_process_unknown_reason_codes.py -q -k "not terminal_close"`（20 passed）。
  注：该文件里两条 `terminal_close` 用例在**未改动的基线上同样失败**（沙箱内 pty 子进程退出确认拿不到），与本改动无关。
- **门禁**：dev 指定的 9 个全仓守卫 **134 passed**；ruff `All checks passed!`；`DOC_SYNC_PASS`；`git diff --check` 干净；`check_code_size.py` hard=0、blocked=False。

## 后台进程与终端会话结果未知时带出具体原因码（2026-09-29，分支 `claude/75-process-unknown-codes`，基于 `64f7ee64e`）

- **`test_process_unknown_reason_codes.py`**（先写测试，在旧代码上 9 条失败、2 条对照通过，再改）：
  - `process_session` 停止时注册表抛权威读不出 / 清理结果未定：错误码与 effect 仍是未知，`reported_error_code` 分别为
    `PROCESS_SESSION_AUTHORITY_UNREADABLE` / `PROCESS_SESSION_CLEANUP_UNCONFIRMED`，`load_error` 原样保留；
  - 停止回执未确认 → `PROCESS_STOP_UNCONFIRMED`，回执保留在 `process`；已确认的停止对照不带原因码；
  - 真实 PTY 会话关闭时把终止回执固定成未确认 → `PTY_CLOSE_UNCONFIRMED`；正常关闭对照不带原因码；
  - 四个新码都登记在错误合同里、不可自动重试、人工核对；
  - 经工具操作账走一遍：记录停在 unknown，`unknown_reason` 为 `effect_outcome_unknown:PROCESS_STOP_UNCONFIRMED`。
- **变异**：8 个（两处原因码丢失、类型判断互换、终端 `_error` 不传原因码、合同缺失或改成可重试）全部被抓住。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_process_unknown_reason_codes.py -q`

## 后台循环退避覆盖到全部循环，包装异常按原因链归类（2026-09-29，分支 `claude/38-loop-backoff-everywhere`，基于 `64f7ee64e`）

- **来源**：ENOSPC 真机验收观察 1——scheduler_due 的 tick 在磁盘写满时打了 `SchedulerDueIndexError` 且归为 programmer_bug；盘点后只有
  派发循环与后台主循环接了 `LoopErrorBackoff`。
- **用例** `test_gateway_loop_backoff_coverage.py`（9 条，确定性构造，`_RecordingStop` 记录每次等待、包一层 `_print_gateway_loop_error` 计数）：
  - 维护循环 interval 60s 连续 11 次出错：等待全是 60（退避不快于自身间隔），打印 2 次，`loop_error_counts={"owner-maintenance": 11}`；
  - 调度器到期循环 / 孤儿恢复循环 interval 0.2s 连续 11 次出错：等待 0.2/0.4/…/25.6/30/30/30，打印 2 次，各自计数，`dispatch_tick_errors` 仍为 0；
    孤儿循环结束后线程池仍回收；
  - 心跳循环连续 11 次写失败：等待全是 5.0（不退避），打印 2 次，`loop_error_counts={"heartbeat": 11}`；
  - 派发 tick 段内错误：空闲时派发段每拍失败 → 退避序列、打印 2 次、`dispatch_tick_errors=11`、tick 起止照记；恢复段每拍失败 → 派发轮询仍是
    0.0/0.2/0.2、恢复段自己的到期按 0.2/0.4/0.8 推迟；终态投影段每次都失败（假时钟每拍 +1 秒）→ 6 拍等待全是 0.2，投影到期间隔
    0.75/0.75/0.8/1.6/…（9a 必改 1：一张坏回执不能让新消息等 30 秒）；
  - `_wait_after_loop_error` 下限；
  - 归因（9a 必改 2）：`SchedulerDueIndexError from OSError(ENOSPC)` → category 仍 programmer_bug，另带 cause_type=OSError、cause_category=io；
    `from PermissionError`、`from sqlite3.OperationalError` 同样带 io 归因（不特判 ENOSPC）；`from ValueError` 与无根因没有归因字段；
    except OSError 分支里的 KeyError、finally 里的 AttributeError（`__context__` 是 OSError）不带归因；真实场景：库文件路径是个目录，
    `SchedulerDueIndex()` 抛出的 `__cause__` 是 `sqlite3.OperationalError`，报告与账本都带 cause_category=io；取消工具
    `_explicit_target_is_absent`：FileNotFoundError → 不存在，被包装的 `database is locked` 与 PermissionError → 存在。
- **改动的既有用例**：后台主循环退避序列改为 `max(1.0, 退避)`；派发用例的段内错误现在也计入 `dispatch_tick_errors`；`note_tick_error` →
  `note_loop_error(loop, …)`；`test_tool_scope_resolution` 的取消判定口径未变（FileNotFoundError / 损坏账本）。
- **负向验证**（改坏产品语义，独立子进程运行，逐字节恢复核哈希）：出错等待改 min；循环跑器不退避；记账入口每次都打；心跳绕过记账直接打；
  派发段错误不记账；空闲派发段失败不退避；各循环计数混在一起；不看原因链；sqlite 运行期错误不算环境类；丢 cause_category；后台修复段失败
  拖慢派发轮询；沿 `__context__` 归因；取消工具重新信 category==io。13/13 被抓出。
- **门禁**：新文件 + dispatcher/adapter 韧性 + loops_resilience + readiness + runtime_error_reports + model_unconfigured + message_scan +
  two_tier + http_runtime_errors + status_tool + background_main_agent_cli + lane_retry + 九个全仓守卫；ruff、doc sync、strict code-size
  （identity 对 64f7ee64e 无新增）、`git diff --check`、clean package；`CODE_SIZE_REPORT.md` 不入提交。真机未复验。
## 向量缓存第四轮：构造宽松、写入严格（2026-09-29，分支 `my-agent/self-dev-2-vcache`）

第三轮把 `_load` 改成"只有 `FileNotFoundError` 才当空"之后，**读错误从构造函数抛了出去**。
而 4 个懒建缓存的调用点都在各自 `try` 之外建缓存，于是派生缓存读不了会放大成业务失败：

| 探针 | 缺陷 | 修法 | 钉住它的测试 |
|---|---|---|---|
| **V1** | `mem.add` 在权威 JSONL **已提交之后**抛 `PermissionError` —— 调用方会以为写入失败而重试 | `__init__` 捕获 `OSError`、内存视图从空开始、记 `last_read_error` | `test_v1_add_succeeds_when_cache_unreadable` |
| **V2** | `search_scoped` 直接抛异常，违背"读失败等价于没有缓存"的合同 | 同上 + 4 个调用点把 `_text_vector_cache()` 挪进各自 `try` | `test_v2_search_degrades_when_cache_unreadable` |
| — | 读错误时把读不出来的缓存当空再写回，会清空整份缓存 | `_flush` 保持严格：读不出就不写盘 | `test_flush_does_not_overwrite_when_disk_unreadable` |
| **MY8** | 写失败只停在缓存对象上，不反映到 `JsonlMemory` 健康状态 | 断言健康状态真的变 `degraded` | `test_w1_write_failure_reaches_jsonl_health` |

**关键区分**：`_load` **不能**整个改回吞掉所有 `OSError`（那会让写路径在读错误时清空整份缓存）。
正确形状是**构造宽松、写入严格**。

**顺手改**：删掉挤进 `_content_hash_of` return 之后的死代码 `keys()`、无调用方的 `_flush_unlocked`；
维护回收返回 `(回收数, 错误码)`。

**⚠️ 更正（2026-09-29，dev 复审 9b 抓出）**：上面这段原本还写着「`remove` 在盘上本来就没有时跳过整文件重写」
和一段"自我纠错"。**那是错的**：第一版撤掉之后，替代实现没有写进去，`_flush` 仍然无条件整文件重写；
9b 的探针 V4 实测删一个盘上不存在的键时 inode 照样变。而注释、本节和模块文档都写着已实现——
**一份和代码对不上的汇报比不做还糟，它让人以为事情已经处理过了。**

本轮真的实现了它（见下面的第四轮小节），并补了 inode/mtime 不变的用例。
教训记在这里：**汇报里写「已做」的每一项，交之前对着代码核一遍。**

**验证**：定向 **97 passed**；变异 `scripts/mutate_text_vector_cache.py` **19/19 KILLED**
（含 dev 点名的 **MY7** 构造宽松、**MY8** 健康上报）；五项 gate 全过。

## 检索侧正文哈希向量缓存（第四版：第三轮复审返工，2026-09-29，分支 `my-agent/self-dev-2-vcache`，基于 `64f7ee64e`）

第三轮复审（9b，探针 `review_probes_vc3/`）确认 P5b/X1 已修好，但**又抓到两处"已删事实的向量残留"**，
其中一条是**假的结构化事实**：

| 编号 | 缺陷 | 修法 | 钉住它的测试 |
|---|---|---|---|
| **M1** | 维护里的孤儿回收**空转**：路径手拼 `O/memory/memory.jsonl`（生产实际是 `owner_memory_long_term_jsonl`），文件不存在直接 0；且新建的 `JsonlMemory` 无 embedder → 仍是 0。后果是 `maintenance.json` 每天写假的 `text_vector_cache_reclaimed: 0`，**像"跑过、没有孤儿"** | 用 canonical 路径；回收**不依赖 embedder**——按 key 里的正文哈希比对 active 记录的 `index_text` 哈希（新增 `text_content_hash` / `retain_content_hashes`） | `test_maintenance_reclaims_orphan_on_real_layout` |
| **P5d** | `remove` 只在"本实例内存里有这个键"时才落盘；父代理与每个 worker 各有缓存实例 → 子代理删的事实永久留孤儿 | `remove` 一律在文件锁内从磁盘减掉请求的键 | `test_p5d_remove_subtracts_from_disk_even_if_not_in_memory` |
| **P5c** | `keep()` 在实例锁里算、不在跨进程文件锁里 → 复核后、落盘前别的实例删除仍会写回 | `keep()` 挪进 `_flush`，在 `locked_json_path` 之后求值 | `test_p5c_keep_is_evaluated_inside_the_file_lock` |
| **W1** | 主路径 rename 失败（磁盘满）不留错误，健康状态仍 configured | 失败时设 `last_write_error`，反映到健康状态 | `test_w1_primary_write_failure_records_error` |
| **X1b** | 拿锁失败的回退路径仍整份写回 → 复活别的实例已删的键 | 拿锁失败**不写盘**，只记错误 | `test_x1b_lock_failure_does_not_write` |
| L1 | 文档说"`index_all` 会清掉 `textcache:v1:` 项"——实际回收只动 `memory_text_vectors.json` | 改正文档 | （文档项，无测试） |

**测试**：`tests/test_memory_vector_cache.py` 共 **38 条**（本轮新增 6 条）。
定向：`cd agent_py_agent && python3 -m pytest tests/test_memory_vector_cache.py -q` → 38 passed。

**变异验证**：`python3 scripts/mutate_text_vector_cache.py` → **killed=17 survived=0 skipped=0**，
含 dev 点名的 **MY1**（回收函数开头 `return 0`）、**MY2**（主路径写失败不留错误）、**MY3**（`keep()` 挪到文件锁外），
以及本轮新增的 MYD（`remove` 只看内存）、MYX（拿锁失败仍写盘）。

**9b 的第三轮 10 条探针**：修复前 10/10 全部"缺陷存在"；修复后 5 条已修复的（P5c/M1/W1/X1b/P5d）全部不再复现。
R-P5b / R-X1 仍然正确（它们是"已验证修好"的断言，继续通过）。M1b 探针刻意用手拼路径，属预期不复现。

**本轮的两条自我批评**：
1. **维护入口那条链路我上一轮从没测过**——只测了 `index_all` 的回收，就把"挂进维护"写进交付和文档。
   结果它天天报 0。这与之前的教训完全同源：**"纯函数/单测全绿"不等于"真实链路能跑"**。
2. 顺手把 `keys()` 删掉了（替换类尾时截断），被自己的测试立刻打回并补回——
   这次是测试救了，但提醒我改大块代码后要整文件复核，不能只看工具回显的片段。

## 2026-09-29 retention 隔离过滤：修复「错误是动作的祖先」方向（第 7 批）

**来源**：dsh-9b 复核 `28783102c`（dev 2026-09-29 02:13 转达）。9b 探针 S1：错误路径是
`.../e`、动作是 `.../e/work/blobs/tool_outputs`，结果动作没有被剔除；变异 MR16（整个删掉
这一方向）在 36 个相关文件下存活，印证它是死代码。

**根因**：`retention.py` 的 `_without_errored_subtrees` 里写的是

```python
error_self = _path_keys(error.path for error in plan.errors)
```

`_path_keys` 只接受**单条**路径，收到生成器后走 `str(path)`，得到
`"<generator object ...>"`，于是 `error_self` 永远是只有这一个垃圾字符串的集合，
`_path_and_ancestor_keys((action.path,)) & error_self` 恒为空——「错误是动作的祖先」这一半
判断从未生效（注释写的双向语义只成立一半）。

**做法**：改成逐条解析后取并集
（`frozenset().union(*(_path_keys(error.path) for error in plan.errors))`）。判据仍是单向的
「错误是动作的祖先」或「动作是错误的祖先」，不改成两边都取祖先集合求交——那会命中公共祖先，
把兄弟目录误判成同一棵树（MR18）。`if not error_self` 的提前返回保留为快速路径，语义上不改变
结果：错误路径为空串时两个键集合都为空，逐条判断本来也不会剔除任何动作。

**新测试**（`test_memory_retention_isolation.py`，17 项）：
- `test_action_under_error_directory_is_dropped`：错误是目录时，它下面的
  `work/blobs/tool_outputs` 动作必须被剔除——**修复前红、修复后绿**，就是 S1 的形状；
- `test_errors_without_paths_do_not_drop_actions`：错误路径为空串时不误删动作，
  顺带确认 `if not error_self` 这个快速路径的行为。

**变异结果**（`_mutate_batch7.py`，跑完自动还原并核对逐字节一致）：

| 变异 | 结果 |
|---|---|
| M-A：`error_self` 退回「把生成器传给 `_path_keys`」的原始 bug | **杀死**（1 条红，就是守门用例） |
| M-B（= MR16）：整个删掉「错误是动作的祖先」这一方向 | **杀死**（4 条红，含守门用例） |

**验证**：定向 216 项通过（含 `test_memory_retention_isolation/v2/runs`、
`test_home_maintenance`、`test_gateway_owner_retention`、`test_audit_*`、
`test_packaging`、`test_decision_curator*`）；九个全仓守卫 9/9 通过；五项静态 gate 全过
（ruff、`DOC_SYNC_PASS`、code-size `hard=0 blocked=False`、`diff --check` 干净、
clean package OK）；生成物 `CODE_SIZE_REPORT.md` 已还原，不入提交。

**文档同步**：`docs/modules/memory/02-progress.md`、`docs/modules/memory/04-structure.md`
补上扫描根推导、三处 `rglob` 深度过滤、audit 类别跳过、隔离判据与性能口径。

**未覆盖**：没在生产 owner home 上跑 `--apply`/`--cleanup`（会真动生产数据），只在临时 home
上验证；隔离过滤本身是纵深防御，合法输入构造不出「坏子树里带动作」的计划。

## R4 隔离：坏的那一棵单独保护，不连累其他类别（2026-09-28，分支 `my-agent/self-dev-4`，基于 `c101d325a`）

- **9b 复核第 6 批（dev 2026-09-29 转来）**：三条必须改 + 两条建议，全部落地。
  1. **隔离过滤性能回退（我引入的真问题）**：原实现对每一对「动作 × 错误」现场构造 Path，生产 8161 动作 × 355 错误时 9b 实测 188 秒，而字符串版只要 0.35 秒。现改成：每条路径只解析一次，错误路径及其祖先进集合，动作只做两次 O(深度) 查表。
     - 实测：8161 × 355 = **0.059 秒**（旧字符串版 0.271 秒；修复前的坏版 188 秒）；8161 × 50 = 0.057 秒。保留条数逐项一致。
     - 新增规模化断言 `test_isolation_filter_is_fast_at_production_scale`（8000 动作 × 400 错误 < 1 秒）。
     - **踩过两次方向错误**，都用真实数据定位：① 先写成「双方祖先集合求交」，会命中公共祖先 `/home/tasks/2026-01-01`，把兄弟目录误判成同一棵树；② 正确的单向判据是「错误自身 ∈ 动作的自身+祖先集合」或「动作自身 ∈ 错误的自身+祖先集合」。
     - 生产 plan 的 `report.errors` 实测 **355 条**（全部 `MEMORY_RETENTION_TASK_STATE_INVALID`），动作 11742 条。
  2. **audit-log 端到端用例**：新增 `test_audit_log_cleanup_end_to_end_uses_the_writer_file` —— 按 Agent 方式注入运行时路径 → 用写入端 `AuditLogger` 真写一行超期、一行新鲜 → 调真实 `cmd_audit_log(cleanup=True, days=30)` → 同一文件只剩新行。
     - 变异：让写入端与 CLI 各算各的路径（退回"再拼一次 audit.jsonl"）→ 该用例与另一条共 2 条变红。
  3. **MR12（去掉 subagent_scratch 的深度过滤）此前存活**：已有嵌套用例只造了 blobs/tool_outputs，没有 work/agents。新增 `test_nested_agents_dir_is_not_treated_as_subagent_scratch`（嵌套 tasks 根 + 健康超期子代理 + inbox）→ **杀死 MR12**（1 条红）。
  4. **MR6 补行为断言**：新增 `test_apply_refuses_everything_when_candidates_ledger_is_unreadable` —— 候选账本损坏时 `apply` 返回 `applied=False`、`actions` 为空、磁盘上到期任务原样保留。
  5. **双语义简化**：默认只用本平台 Path 语义，Windows 语义由调用方显式注入（`_path_keys` / `_path_and_ancestor_keys` / `_path_overlaps` 都接受可选 flavour）。Windows 用例改成显式传 `PureWindowsPath`，顺带消掉"文件名里合法的反斜杠被当分隔符"的多判。

- **来源**：dev 2026-09-28 两轮裁定。起因是生产只读预演发现 `O/tasks` 下有 355 个坏 `state.json`，而 **main 上 `apply` 只要 `plan.errors` 非空就拒绝整份计划**——真机数据核实：`owner_retention_applied` 事件 29 条、`applied` 全为 `False`、`actions` 全为 0，`maintenance.json` 的 `last_success_at` 恒为 0.0。**生产的保留清理从来没真正执行过**，每天被同一批错误整份拒掉。
- **做法**：
  - `retention.py` 的 `apply` 守卫改成 `plan.legal_hold or _has_policy_level_error(plan.errors)`。只有"读不懂策略本身"才整份拒绝（`_POLICY_LEVEL_ERROR_CODES` = `POLICY_INVALID`/`POLICY_UNREADABLE`/`CANDIDATES_UNREADABLE`）；
  - 其余错误按**路径前缀重叠**（`_path_overlaps`，含相等；`/a/b` 与 `/a/bc` 不算同一棵）把坏子树从可执行计划里剔除（`_without_errored_subtrees`），被隔离保护的错误原样并入回执，执行过什么、还护着什么都能看见；
  - 隔离**只按路径**，不按类别名或目录名放宽，也不因为某个类别有错就整类跳过。
- **审核裁定同步落到代码**：`retention_scan` 的 `audit` 类别（`O/audit/*.jsonl`）**本次一律跳过**，类别名与实际数据不符（那是主代理每轮的记忆归档原始事件、Curator 要读，不是审计日志，删了不可恢复），注释里写明等存储登记表定了语义再处理。
- **规范根从唯一权威推导**：新增 `conversation/workspace_paths.canonical_task_root(owner_home, path)`，把三类规范任务根与各自深度（`runs/<date>/<key>` 第二层、`tasks/<date>/<slug>` 第二层、`audits/<audit_id>` 第一层）一次说清；`retention_scan` 的恢复根改成 `(durable_work_root(task), owner/tasks, durable_work_root(audit))` 去重，`_iter_task_states` 用 `canonical_task_root` 校验深度——深一层或浅一层都不算任务根，调用方不再自己数层数。tool output 侧维持只扫旧 `O/tasks`，本次不扩。
- **新增/更新测试**：
  - `test_memory_retention_isolation.py`（新，5 项）：坏子树被剔除、健康子树保留；祖先/后代两个方向的路径重叠都识别、`/a/b` 与 `/a/bc` 不误判；无错误时计划原样返回（`is` 同一对象，不借隔离之名放宽）；不相关错误不误伤动作；策略级错误码分类。
  - `test_memory_retention_runs.py`：补 `audits` 首层是任务根、`audits` 深一层不是、`runs` 浅一层不是。
  - `test_memory_retention_v2.py`：`test_corrupt_policy_fails_closed_but_single_bad_task_is_isolated`——policy 级失败仍 `applied=False`；单棵坏 state 改成 `applied=True` 且坏目录原地保留、`audit` 文件不动。
- **变异验证**（`_mutate_r4.py`，就地文本替换、跑完自动只还原被变异的那个文件）：
  - `ignore-errors`（守卫退回"有错误就整份拒绝"）→ 杀死 1 条；
  - `drop-isolation-semantics`（把错误路径列表清空）→ 杀死 1 条（`test_only_errored_subtree_actions_are_dropped`）；
  - `ignore-audit`（把 `audit` 类别加回扫描）→ 杀死 2 条；

  - **9b 复审（2026-09-29）补的回归**：4 条关键安全点此前没有测试锁住，4 个变异在相关测试下全部存活。现已补齐并把 4 个变异全部杀死：
    - MR2（去掉 `_iter_task_states` 的规范深度过滤）→ 杀 2 条（`test_nested_task_like_dir_is_not_treated_as_task_root`、`test_audits_root_deeper_level_is_not_a_task_root`）：运行中任务 output 里嵌套一份带终态 state.json 的「像任务」目录时，它不能被当成任务根整棵移走。
    - MR4（`_recovery_roots` 丢掉 `O/audits`）→ 杀 1 条（`test_audits_root_is_scanned_for_terminal_tasks`）。
    - MR5（`audits` 深度 1 改成 2）→ 杀 3 条（audits 首层是任务根 / 深一层不是 / `canonical_task_root` 深度）。
    - MR6（把 `CANDIDATES_UNREADABLE` 移出整份拒绝集合）→ 杀 1 条（`test_candidates_unreadable_is_importable`）。
  - **tool_output / subagent_scratch 两处的深度收窄（dev 2026-09-29 第 4 条）**：这两处的 `rglob` 原来不限深度，嵌套的「像任务」目录会让它下面的 `blobs/tool_outputs` 或子代理 `inbox` 被规划移走。现两处都过 `canonical_task_root`，只收窄、不改「tool output 只扫 O/tasks」的范围。
    - 生产 plan 纯结构化核对（只跑 `plan()`，未 apply）：`tool_output` 71 + `subagent_scratch` 8090 = **8161 条动作，涉及 256 个任务根，任务根不在规范深度上的 0 条**。
  - **Windows 路径下的隔离（dev 2026-09-29 第 5 条）**：`_path_overlaps` 原来把分隔符写死成 `/`，Windows 路径识别不到祖先/后代、隔离会静默失效。现改成同时用 `PurePath` 与 `PureWindowsPath` 比一遍（POSIX 上 `PurePath("C:\\tasks\\a")` 是单个文件名，这正是原缺陷），`test_path_overlaps_handles_windows_separators` 钉住。
  - **`test_home_maintenance.py` 按 audit 跳过改写**：它原来用 audit 清理验证「只清超期的」，audit 跳过后就红了（我先前定向没包含它，全仓会红——已修正）。现改由 `daily` 类别承担同一断言，audit 侧改为反向断言「超期也原地保留、计划里不出现 audit 动作」。
  - **文档补充**：`_without_errored_subtrees` 注释写明它是纵深防御、只比较 `action.path` 不比较 `related_paths`；`docs/design/STORAGE_RETENTION.md` 写明一处保留的设计残留——subagent_scratch 不看父任务是否已结束。
  - **`no-isolation`（`executable = plan`）的覆盖情况（dev 2026-09-29 要求核实后更新）**：它**杀不掉**，而且原因是结构性的，已核实清楚：
    - 用合法输入构造不出"坏子树里带动作"的计划。三类扫描在读不懂父任务 `state.json` 时都整棵跳过：`_task_actions` 记错误后 `continue`；`_tool_output_actions` 同一个 `_read_state` 拿到 `None` 就 `continue`；`_subagent_scratch_actions` 先读父任务 state，`None` 就 `continue`（父状态是授权前提），**连它下面健康且超期的子代理 scratch 也不会进计划**。
    - 所以 apply 的路径隔离在本仓库里是**纵深防御**：即使把过滤整段拿掉，能进 `apply` 的合法计划里本来就没有落在坏根下的动作。
    - 新增 `test_unreadable_task_root_yields_no_actions_underneath` 把这条反证钉住：坏任务根 + 健康超期子代理 scratch + 超期 tool output，断言 plan 与 apply 的动作里**没有一个**落在坏根之下，且原件全部保留。
    - 另一条相关变异的杀法（顺带）：把 `_subagent_scratch_actions` 的父状态校验拿掉（越权放行），上面这条用例**会红**——实测 `FAILED test_unreadable_task_root_yields_no_actions_underneath`，也就是父状态这条授权前提真的有测试守着。
- **定向回归**：`test_memory_retention_runs.py` + `test_memory_retention_v2.py` + `test_memory_retention_isolation.py` + `test_gateway_owner_retention.py` 共 28 项通过。
- **复现**：
  ```
  cd <worktree>
  python3 -m pytest agent_py_agent/tests/test_memory_retention_runs.py agent_py_agent/tests/test_memory_retention_v2.py agent_py_agent/tests/test_memory_retention_isolation.py agent_py_agent/tests/test_gateway_owner_retention.py -q
  python3 <工作目录>/_mutate_r4.py ignore-errors    # 期望 1 条红
  ```
- **未覆盖**：没在生产 home 上跑 `--apply`（会真移/真删生产数据）；预演只验证"会规划什么"。

## retention 扫描覆盖新版运行根 O/runs（2026-09-28，分支 `my-agent/self-dev-4`，基于 `80b4afed8`）

- **来源**：dsh-9b 的产品持久数据盘点（`docs/design/STORAGE_RETENTION.md`）零风险缺口之二——`MemoryRetentionService` 的扫描范围写死在 `O/tasks`，新版运行工作区 `O/runs` 不在范围内。
- **做法**：新增 `_recovery_roots(home)` 返回 `(owner_tasks_dir, owner_runs_dir)`，`_iter_task_states(home)` 逐个产出「根 + 该根下的 `work/state.json`」，`_task_actions` 与 `_tool_output_actions` 都改用它。两个根共用同一套 `work/state.json` 合同，所以判定逻辑一行没改——它也**本来就是结构化状态驱动**：读 `state.json` 的 `task_id`/`status`/`updated_at`，状态不在 `TASK_TERMINAL_STATUSES` 就跳过，不看 mtime、不看目录名。保留天数沿用现有 `completed_task_days`，未新增参数。
- **新增测试**：`test_memory_retention_runs.py`（9 项）——runs 里超期终态被规划回收、两个根同时被扫、未完成的不动、未知状态的不动、**把终态任务所有文件 mtime 改成"刚刚"仍被回收**（证明不是按 mtime 判）、legal hold 的不动、cutoff 边界内外行为、保留期 0 时不动、runs 根缺失不报错。
- **变异验证**：`_mutate_retention_runs.py`（工作目录 `tasks/2026-09-28/storage-retention-fixes/`）两处，先 `git diff` 存补丁、`git checkout -- .` + `git apply` 还原：
  - 扫描根退回只有 `O/tasks`（回到缺口状态）→ 4 条红；
  - 终态判断放宽成"状态非空即终态"（丢掉白名单）→ 2 条红（未完成/未知状态被误清）；
  - 还原后 9 项全绿。
- **定向回归**：`test_memory_retention_runs.py` + `test_memory_retention_v2.py` + `test_gateway_owner_retention.py` 共 23 项通过。
- **复现**：
  ```
  cd <worktree>
  python3 -m pytest agent_py_agent/tests/test_memory_retention_runs.py agent_py_agent/tests/test_memory_retention_v2.py agent_py_agent/tests/test_gateway_owner_retention.py -q
  python3 <工作目录>/_mutate_retention_runs.py only-tasks     # 期望 4 条红
  ```
- **未覆盖**：没在生产 home 上跑 `home-retention --apply`（会真移生产数据）；本片只验证 plan 的规划范围与判定，apply 路径沿用既有实现未改。

## audit-log --cleanup 按 owner canonical 路径解析（2026-09-28，分支 `my-agent/self-dev-4`，基于 `80b4afed8`）

- **第 6 条修复（dev 2026-09-29）**：`AuditQuery` 拿到运行时权威路径后**又拼了一次** `/audit.jsonl`，而写入端 `audit/logger.py` 用的是 `resolve_audit_paths`（带后缀就是文件本身）。配置成 `.../custom-audit.jsonl` 时，CLI 查的是 `<文件>/audit.jsonl`——查询为空、清理空转。现在查询端复用同一个解析器（经一个只暴露 `audit_log_path` 的最小适配器），两边规则一致。
  - 新增 2 条断言：带后缀的权威路径就是文件本身（不再拼）；查询端与写入端对同一带后缀路径解析一致。
  - 变异：退回「无条件拼 `/audit.jsonl`」→ 杀死 2 条，报错现场正是 `custom-audit.jsonl/audit.jsonl`。

- **补充（2026-09-29，同一分支）**：dev 要求把"基准"从配置默认值那条隐式链换成运行时路径的权威值。现在 CLI 除了传 owner home 基准，还把 `runtime_paths_for_config(config)["audit_log_path"]` 交给 `AuditQuery(runtime_audit_path=...)`——这一份与 Agent 启动时注入 config 的值同源（实测都指向 `O/logs/audit/`）。`runtime_paths_for_config` 是新增的 CLI 侧小函数，只做定位、不建目录、不读内容。
  - 新增 3 条断言：给了运行时路径就以它为准（不再落到 `data/audit`）；两套入口解析出的审计文件是同一个；省略运行时路径时保持既有行为。
  - 变异 `ignore-runtime-path`（`AuditQuery` 忽略运行时权威路径）→ 杀死 2 条。
  - **CLI 入口的缺口已补**（dev 2026-09-29 要求）：原来的用例都直接构造 `AuditQuery`，等于只测了查询视图、没测 CLI 那一行——而那一行就是修复本身。新增 `test_cli_entry_uses_runtime_audit_path_regardless_of_cwd` 与 `test_cli_runtime_audit_path_stays_inside_owner_home`：
    - 走真实入口 `cmd_audit_log`（`--cleanup` 分支），用 `MY_AGENT_HOME` 隔离 owner home，顶替 `AuditQuery` 只记录 CLI 传下来的 `runtime_audit_path`；
    - 从**两个不同 cwd** 各跑一次，断言两次拿到的权威路径都是同一个 `owner_logs_dir/audit`，且落在 owner home 之内；
    - **变异验证**：删掉 CLI 里 `runtime_audit_path=...` 那一行 → 该用例变红（`assert None == .../logs/audit`），确认修复本身有测试守着。

- **来源**：dsh-9b 的产品持久数据盘点（`docs/design/STORAGE_RETENTION.md`）零风险缺口之一——`audit-log --cleanup` 按相对路径解析，指到了错误的文件。
- **根因**：`audit/paths.py:resolve_audit_paths` 把配置里的相对值（默认 `data/audit`）直接 `Path(...)` 展开，于是相对**进程 cwd**。Agent 启动时 `core.py:450 apply_runtime_paths_to_config` 会把 canonical 的绝对 `audit_log_path`（`owner_logs_dir/audit`）注入 config，所以写入端没问题；但 CLI 的 `audit-log` 只 `load_config`、没有走 runtime paths，于是拿到空串 → 落到 `data/audit`，随启动目录漂移。在不同 cwd 下执行会清到不同文件，甚至可能清掉工作区里恰好同名的 `data/audit/audit.jsonl`。
- **做法**：`resolve_audit_paths(config, *, root=None)` 新增可选基准；给了基准且配置值是相对路径时，按基准展开，绝对路径原样尊重；不传时保持旧的相对语义（不影响既有测试与旧调用方）。`AuditQuery.__init__` 同样接受 `root`。CLI 用 `workspace_resolution.owner_home_workspace_root(config)` 取 canonical owner home 传入，替换原先取到就没用上的 `resolve_workspace_root`。
- **新增测试**：`test_audit_cleanup_path.py`（9 项）——相对路径按基准解析、绝对路径不被改写、无基准保持旧语义、**两个不同 cwd 下清的都是 canonical 那一个且工作区同名文件绝不被碰**、保留期 ≤ 0 不删任何记录、清理不越出给定 owner home、查询视图的 root 与 file 两个字段都跟着基准、带后缀的相对路径、生产同形路径落在 owner home 内。
- **变异验证**：`_mutate_audit_path.py`（工作目录 `tasks/2026-09-28/storage-retention-fixes/`）两处，先 `git diff` 存补丁、`git checkout -- .` + `git apply` 还原：
  - 完全去掉基准解析（回到随 cwd 漂移）→ 6 条红；
  - 假装支持基准但实际用 `Path.cwd()` 兜底 → 同样 6 条红；
  - 还原后 9 项全绿。
- **定向回归**：`test_audit_cleanup_path.py` + `test_audit_class.py` + `test_audit_redaction.py` 共 26 项通过。
- **复现**：
  ```
  cd <worktree>
  python3 -m pytest agent_py_agent/tests/test_audit_cleanup_path.py agent_py_agent/tests/test_audit_class.py agent_py_agent/tests/test_audit_redaction.py -q


## 非决策调用“缺报”同口径统一，及 ae 复审跟进（2026-09-28，分支 `claude/be-llm-missing-unify`，在 `d802380d8` 之上重做 `dc7fdd8a3`）

- **先核实，再改显示**（没有补记任何账）：
  - 主模型建记录时 `input_tokens` 取发送前的可见上下文快照，辅助调用取请求材料的估算；
  - 两处 HTTP 尝试观察者都写入账本；
  - 模型 HTTP 全部经 `http.py` 进入 `_gateway_request_attempt`；
  - 账本的未完成估算桶不分用途，所以 model_usage 的根与 main/auxiliary 分区早已带 `estimated.unfinished_*`。
- **`test_decision_stats_display.py` 新增或改写**：
  - LLM 段与决策段同一口径：
    - 发出去后超时的，显示“LLM 估算 N token（未完成）”；
    - 没发出去的，显示“LLM 未发出 M”；
    - 分不清的旧账，才显示“LLM 缺报 K”；
    - 成功但没回报用量的，计入“会话累计 ~N”，不再算缺报。
  - LLM 段扣掉决策份额：全部用途减去决策分区，同一次调用不在两段重复出现。数值取不会被 k 单位抹平的，
    失败、已发出未完成、分不清是否发出、估算 token 四项漏减任何一项都会看得出来。
  - 端到端：用真实原账本生成主模型、决策、旧账三条 model_usage 事件，经 `publish_model_metrics` 累加后渲染，
    锁定全部用途的失败构成逐事件累加（账本摘要生成器改名 `_ledger_calls`，可按用途生成）。
  - 部分成功调用回报了输入：按已报平均值外推、带 ≈，不挂“未回报用量”说明（那只留给一次都没回报的情况）。
  - 决策段次数互不重叠（ae 建议）：旧账失败显示为“失败 3（其中 3 次分不清是否发出）”，成功却一次都没回报的显示为
    “成功 2（其中 2 次未回报用量）”，整行不再出现会被加总的“缺报”。
- **已有测试按新口径更新**：
  - `test_tui_model_metrics.py`：原来断言 `unreported_calls` 的两处（该字段已删除）改为：没回报用量的算估算、不算缺报；
    没发出 HTTP 就失败的压缩调用显示“LLM 未发出”；
  - `test_decision_usage_metrics.py`：两个“成功却一次都没报”的标签改为括号说明写法。
- **在 `d802380d8` 之上重做时新增**：
  - 手写 fixture 全部显式带上 `decision_unfinished_calls` / `decision_unknown_failures`，否则会按旧快照规则把失败记为分不清；
  - 生产形状快照回归改为新措辞：“失败 2（其中 2 次分不清是否发出）”，且旧快照没有全部用途的失败构成时 LLM 段留空；
  - MU1（9b 复审）：端到端测试断言未完成调用的发送前估算不进“会话累计”（`会话累计 0`，`unfinished_tokens` 为 84）。
- **重做前的原始验证**（`dc7fdd8a3`，基于旧分支）：105 个相关文件 2629 passed；30 个变异全部杀死。
- **重做后定向回归**：122 个相关文件（主线上统计行、线程显示副本、Gateway 推流、TUI 渲染、审计及
  `test_architecture_guardrails`、`test_constant_names_unique`）2975 passed。
- **重做后变异验证**：33 个变异全部杀死（按 pytest rc==1 判定）：原 30 个，加上 MU1（会话累计混入未完成估算）、
  去掉旧快照默认规则、对新格式快照也套用默认规则。

## 会话互通真实链路门禁：修掉取消用例的收尾竞态（2026-09-29，分支 `claude/ae-real-chain-cancel-window`，基于 `130dee0ac`，只改测试）

## 会话互通真实链路门禁：修掉取消用例的收尾竞态，补“后端不响应停止”“停止旗丢失”两窗（2026-09-29，分支 `claude/ae-real-chain-cancel-window`，基于 `130dee0ac`，只改测试）

- **起因**：在取消修复 `542139f95` 上，两条 HOLD 取消用例时过时不过。
  - 量化（证据：`~/.my-agent/decision-evidence/session-task-chain-e2e/flaky-542139f95/`）：原样导出 80 次，HOLD-AFTER-TOOL 失败 1 次，HOLD-FIRST 0 次。
  - 唯一挂住的断言是"取消后目标的唤醒没有结案"，残留的是发给 A 的取消通知唤醒。
- **根因在测试自身**：
  - `cancel_session_task` 在 A 的前台取消回合还没结束时，就给 A 发取消通知并唤醒 A。
  - 后台 `drain()` 只有 10 轮 tick，每轮在 A 的忙通道上空转一次（11–38 ms），实测用掉 6–9 轮。
  - A 的回合稍慢，预算就会先用完。给 A 的最后一次回复加 0.5 秒延迟，两条用例 5/5 失败。
  - 生产调度器持续 tick，前台结束后会处理这条唤醒，所以不是产品缺陷。
- **修法**：join 之后：
  1. 先单独断言 B 的派活唤醒已结案，这是原断言真正要保护的；
  2. 再在主线程 `chain.drain()` 一次，排空 A 的通知唤醒；
  3. 保留原来的"无 pending"断言。

  同样的延迟下，修法 5/5 通过。
- **main 上的表现不变**：两条取消用例仍按 strict xfail 挂在 `stop_confirmed`（`--runxfail` 可以确认），新断言和补排空在 main 上都通过。
- **新增两窗（第二个提交）**：两窗都复用同一套取消流程（`_cancel_bound_task_during_hold`），两条原取消用例也改用它。
  - `test_cancel_discards_the_reply_when_the_backend_ignores_the_stop`：
    - 场景：挂起点忽略停止（`hold(honor_stop=False)`），放行后照常返回答复；断言停止确实送到了在途调用并被忽略、没有交付、任务 cancelled、唤醒结案。
    - 在叠加 542139f95 的导出上，先由调用方线程等模型结果时的中断检查（`tool_model_generation._wait_for_generation_result`）挡住；旗还在时还有既有防线，所以没有哪个单一变异能让这一窗失败。
  - `test_cancel_discards_the_reply_when_the_stop_flag_is_lost`：
    - 场景（故障注入）：停止控制照常回报确认，但不给目标线程立旗；在途调用没被打断，放行后正常返回。
    - 只有交付前按任务已取消的持久检查（`_session_task_turn_was_cancelled`）能挡住交付。
  - 两窗在 main 上都按 strict xfail 挂在 `stop_confirmed`。
  - 两窗的“前提不成立”检查用 `_require`（be 复审 must-fix，第三个提交）：注入被改坏时报 RealChainBroken，不会被 strict xfail 吞成 XFAIL；位置在停止确认断言之后，main 上仍先按 xfail 挂在停止确认。
  - 叠加 542139f95 后都转正：strict xfail 报 XPASS，`--runxfail` 下真实通过。
  - 叠加导出上的变异结果：
    - 去掉持久检查：只有“停止旗丢失”一窗失败。
    - 去掉等待关卡、持久检查和 post-run_once 二次检查三者：“不响应停止”仍通过，旗在时还有既有防线。
    - 只去掉 post-run_once 二次检查：四窗全过。它在答复落账之后，构造不出它是唯一防线的场景，所以不另加用例。
    - 停止永不确认：四窗全部失败。

## `/endtask`：管理员结束卡在等待中的定时会话任务（2026-09-29，分支 `claude/be-end-session-task`，基于 `130dee0ac`；已随 step16g（`bac2f176d`，2026-09-29 02:11 PDT）部署，生产只读验收通过）

- **生产只读验收（2026-09-29，step16g 切换后）**：状态为已部署、生产只读验收通过。
  - 前置核对：Gateway 进程跑在 `runtime-step16g-f41b3532` 上；验收脚本用同一 runtime 的 `python -I` 运行，确认导入的模块都在该 runtime 内。
  - 调用路径：产品自带的 `post_gateway_json(8420, OwnerIdentity.local_main(), "/control", …)`，和 TUI 同一路径；回环来源免令牌，不读配置。
  - 只看结构化字段，不带 confirm：
    - `/endtask` 列表：HTTP 200，`ok=true`，空列表，0 条候选。
    - `/endtask <不存在的任务ID>` 预览：HTTP 200，`ok=false`，`error_code=END_TASK_NOT_WAITING_RUN`。拒绝码不是 `END_TASK_ADMIN_ONLY`，说明管理员通路生效。
  - “不停后台命令”提示只出现在有效候选的预览和确认结果里。生产上没有候选，改用替代证据：
    - 切换后复核，生产 runtime 中 `end_task_control.py`、`control_commands.py`、`control_service.py`、`control_runtime.py` 与 `bac2f176d` 逐字节一致；
    - `bac2f176d` 上 `test_end_task_control.py` 7 项全过，预览与确认结果两处都断言了这句提示。
  - 副作用只有两条控制回执，与用户在 TUI 里敲两次 `/endtask` 相同。
  - 证据目录：`~/.my-agent/decision-evidence/endtask-acceptance/`，含 `endtask_acceptance.py` 与 `result-step16g-20260929.jsonl`。
  - `confirm` 没有在生产上执行过。第一次真正执行，应在确实出现卡住的等待中定时执行时，由管理员按预览提示操作。
- **新增** `test_end_task_control.py`（6 项）。夹具用真实 `SimpleAgent`：经 `create_job` / `reserve_due_runs` / `claim_run` /
  `park_run_waiting` 与 `record_run_creation`、`settle_agent_attempt` 造出事故形态，即定时执行 waiting、会话任务 active、
  attempt 已结束而 AgentRun 未关。
  - 解析：只认任务 ID 和 confirm；多余参数、非法字符都判无效；目录里有 `endtask`。
  - 列表与预览只读，只渲染 ID、状态和等待起点，不带任务正文。
  - 确认结束：会话任务记为 `cancelled`，waiting 运行立即结算为 `cancelled`；结束前同一 job 的到期派发被挡住，结束后恢复派发。
  - 拒绝且不改状态：执行树里还有未结束的 attempt（列表显示“还有执行在跑”）；非管理员（带真实存储，拦住它的是管理员判定本身）；
    运行库缺失、运行库读取报错；不是等待中的定时执行；会话任务已不是 active。
  - CAS：核对之后任务被别的路径改成终态时，确认结束不覆盖（`END_TASK_STATE_CHANGED`）。
  - Gateway 分派按 scope 解析 owner（飞书 scope）；TUI 转发文本保留任务 ID 和 confirm，本地直连模式拒绝并提示用 Gateway。
- **仓库级守卫抓到一次**：新模块的 `_LIST_LIMIT` 与 `settings_control_service.py` 同名，`test_constant_names_unique` 失败，已改名 `_CANDIDATE_LIMIT`。
- **9a 复审跟进（新提交）**：
  - 6 个 `END_TASK_*` 码登记进 `ERROR_CONTRACTS`。原先 `test_recovery_code_policy::test_all_used_error_codes_are_registered`
    在 `f4a861d3f` 上失败（`END_TASK_ADMIN_ONLY`、`END_TASK_STATE_CHANGED` 未登记）；另外 4 个写在 `_REFUSALS` 字典里，守卫扫不到，
    新增 `test_every_end_task_error_code_is_registered` 一并钉住。
  - 预览和确认结果固定写明：结束任务不会停止它启动的后台命令，这些命令结束后的通知会落到已取消的任务上；两处都加了断言。
  - 教训：聚焦门禁必须带全仓扫描守卫（`test_recovery_code_policy`、`test_architecture_guardrails`、`test_constant_names_unique`、
    `test_config_field_readers`、`test_parameter_registry`）。上一轮的 80 个文件里漏了第一个。
  - 跟进提交的回归：原 80 个文件，加上引用错误合同的测试和全仓守卫，共 97 个文件，2424 passed、1 skipped、1 xfailed（均为原有标记）；
    其余 3 个全仓扫描守卫（`test_main_agent_has_no_case_runtime`、`test_skill_snapshot_error_codes`、`test_subagent_config_inheritance`）另跑，20 passed。
- **定向回归**：80 个相关文件（控制命令解析、命令目录、Gateway 控制、TUI 控制、运行库、定时服务，含
  `test_architecture_guardrails`、`test_constant_names_unique`）2035 passed、1 skipped、1 xfailed，后两项为原有标记。
- **变异验证**：19 个全部被抓住（按 pytest rc==1 判定）。覆盖范围：管理员判定、预览误写、CAS、立即结算、三条放行条件、
  运行库缺失与读取报错、终态判定、列表结论、confirm 识别、参数校验、解析登记、目录登记、Gateway 分派、TUI 本地拒绝与转发文本。
  其中“绕过管理员判定”一开始是因为替身缺少存储才报错，杀死理由不硬；已改成带真实存储的非管理员并断言状态不变。

## 旧显示快照的决策失败不再显示成“未发出”（2026-09-28，分支 `claude/be-legacy-unknown`，基于 `c101d325a`）

- **问题**（9b 复审）：失败构成出现前写下的快照没有 `decision_unknown_failures` 键，`public_model_metrics` 补成 0，
  `split_unsent_failures` 就把旧失败全算成“未发出”，例如“失败 0 · 未发出 2”。
- **修复**：缺这个键时按旧用量行同一规则，把失败整体记为分不清是否发出。
- **测试** `test_decision_stats_display.py`：
  - 新增生产形状快照回归（改写自 9b 探针 U3a）：直接渲染与经 `model_metrics_from_thread` 读回落盘快照两条路径，
    都显示“失败 2”，不出现“未发出”；
  - 手写的新格式快照 fixture 显式带上 `decision_unfinished_calls` / `decision_unknown_failures`。
- **反向验证**：去掉默认规则、或对新格式快照也套用默认规则，两个变异都被测试抓住。
- **定向回归**：78 个相关文件（统计行、线程显示副本、Gateway 推流、TUI 渲染及 `test_architecture_guardrails`、
  `test_constant_names_unique`）1722 passed。

## Gateway 派发线程不被错误打印杀死、存活进心跳与 /status、扫描门扫描前取样（2026-09-28，分支 `claude/38-gateway-dispatcher-resilience`，基于 `c101d325a`）

- **来源**：集成者转述 dsh-be 的只读排查：生产 step15t 从 15:11 起没处理过一个前台请求；15:13 磁盘写满时错误打印本身抛错逃出 tick，
  派发线程静默退出；另有扫描门在扫描结束时取 mtime 的竞态隐患。
- **用例** `test_gateway_dispatcher_resilience.py`（5 条，全部确定性构造，不靠负载）：
  - stderr 替身第一次写抛 `OSError(ENOSPC)`；tick 段内错误触发的打印撞上它，下一拍段外错误直接逃出 tick：线程不死，第三拍新入队的
    请求被认领；账本记 1 次打印失败、1 次 tick 错误、3 次 tick、正常退出无错误；磁盘恢复后打印照常。
  - `_print_gateway_loop_error` 打印失败不抛、只记账本；第二次正常打印。
  - tick 卡住时心跳载荷 `dispatcher_alive=True` 且 tick 起始晚于结束；放行后 tick 抛 `SystemExit` 线程退出：账本 exited、退出错误
    类型/阶段、`gateway_request_loop_exited` 事件，`/status`（state 仍 running）报 `dispatcher_alive=False`。
  - 账本单测：未登记 not_started；登记线程结束但没记退出 → vanished、不算活；当前线程登记 → running；记退出 → exited。
  - 扫描门：inbox mtime 先拨旧 10 秒、关掉 2 秒粗粒度保护，`_iter_pending_requests` 包一层在 glob 之后 rename 进一份请求：本轮认领 0，
    `should_scan` 必须为 True，下一轮认领到它。
- **适配器用例** `test_adapter_state_write_resilience.py`（4 条）：周期状态写入第一次撞 ENOSPC 不杀等待循环，下一轮恢复并把
  `state_write_failures=1` 与最近错误写进状态文件、有 WARNING 日志；`start_all` 抛错时先停适配器、删 pid 文件、状态写 `failed`
  （reason/error 结构化）再上抛；磁盘一直满时状态文件不存在但 pid 文件一定已删、有 `adapter_state_write_failed_at_exit` ERROR 日志；
  `/status` 适配器事实：无 pid 文件不算活，状态文件新鲜 running 但 pid 指向已退出子进程仍不算活且不清理 pid 文件，本进程记录算活，
  状态文件损坏只单列错误。
- **9a 复审跟进用例**（同文件追加）：派发循环连续 12 次出错时等待序列精确为 0.2/0.4/…/25.6/30/30/30/30，成功一次回到轮询间隔、
  新一段故障从头退避，15 次错误只打印 3 次（第 1、第 10、新一段第 1）；后台主循环同样序列、11 次错误打印 2 次；`LoopErrorBackoff`
  单测（打印裁决 1/10/20、另一种错误第一次也打、成功清零）；`dispatcher.shutdown()` 抛错记成 `gateway_request_pool.shutdown`；
  账本与适配器错误消息脱敏（`api_key=…` 变 `<redacted>`）；`/status` 的 pid/状态读取错误只含 category/context 且不含 tmp 路径。
- **负向验证**（改坏产品语义，独立子进程运行，逐字节恢复核哈希）：打印入口去掉保护；tick 守卫去掉；退出不记账本；`/status` 不并入快照；
  心跳不并入快照；存活不看线程是否还在；扫描门改回扫描后取样；适配器周期写入不守卫；异常退出不收尾；收尾不删 pid 文件；
  适配器存活不看进程；退避工具不退避；退避工具每次都打；派发循环不用退避时长；后台主循环不用退避时长；关闭阶段记成 run；
  `/status` 错误泄露整份报告；账本不脱敏；适配器不脱敏。19/19 被抓出。
- **门禁**：两个新文件 + `test_gateway_loops_resilience.py` + `test_gateway_two_tier_admission.py` + `test_gateway_readiness_generation.py`
  + `test_gateway_http_runtime_errors.py` + `test_gateway_status_tool.py` + `test_adapter_daemon_cli.py` + `test_channel_health.py`
  + `test_supervisor.py` + 七个全仓扫描守卫；ruff、doc sync、strict code-size
  （identity 对 main 无新增）、`git diff --check`、clean package；`CODE_SIZE_REPORT.md` 不入提交。真机未复验。

## 被放弃的模型调用不再迟到提交插话（2026-09-29，分支 `claude/38-abandoned-call-steer-race`，基于 `f7a4cc909`）

- **来源**：改稳插话墙钟用例时在满载复现里看到：第一次调用被 0.05 秒墙钟放弃并转入重试后，它的工作线程才走到发出前登记，
  把插话提交到作废的调用编号上，重试提交报 `DataCorruptionError: guidance submission was not reserved`。
- **确定性复现**：钩住 `_generate_backend_response` 把第一次调用的工作线程停在发出前，墙钟 0.05 秒放弃并让重试用 10 秒完成，
  再放行迟到线程；改前它会调用 `mark_submitted`（作废编号），改后不调用。
- **改法（最小）**：`_ModelGenerationState.liveness`（每次物理调用一份，带锁）；`_wait_for_generation_result` 在墙钟超时和用户停止
  两处放弃前置位；`_invoke_backend_generate` 把三步发出前登记收进 `_mark_call_before_send`，只在 `run_if_current` 锁内复核通过时
  执行，已放弃则记账本事件 `submission_skipped_after_abandon`（新增 `ModelCallLedger.note_event`，不改终态/用量/尝试计数）并抛
  `ModelCallAbandonedError` 结束工作线程，不发请求。不吞 `DataCorruptionError`。
- **9b 复审跟进**：去掉 `getattr(state, "liveness", None)` 兜底（缺字段不再静默跳过复核；四个既有测试的替身 state 改为带 `_CallLiveness`）；
  被放弃的调用不再记成一次失败的 LLM 调用（RED 指标不失真）；注释写明用户停止/墙钟超时会等一次有界的进行中登记做完，这是正确性所需。
- **新测试** `test_abandoned_call_steer_race.py`（4 项）：迟到线程不提交、不发请求、账本有事件、重试正常提交且批次/回执指向重试；
  用户停止路径也标记放弃；liveness 只执行一次；`note_event` 终态后可追加并去重。
- **负向验证**（改坏产品语义，独立子进程，逐字节恢复核哈希，3/3 被抓出）：发出前不复核；墙钟超时不标记放弃；跳过时不记事件。
- **回归**：本文件、`test_steer_delivery_recovery`、`test_tool_model_generation`、`test_slow_model_liveness`、`test_runtime_guidance`、
  `test_model_call_ledger(_partitions)`、`test_subagent_first_request_selection`、`test_llm_hot_path_metrics`、`test_concurrency_metrics`、
  `test_model_profiles`、`test_architecture_guardrails` 共 285 passed、4 xfailed；新文件满载 6 进程 × 20 次全过。
- **错误码登记（跟进提交）**：`MODEL_CALL_ABANDONED_BEFORE_SEND` 此前只在 `ModelCallAbandonedError` 上声明、未进 `ERROR_CONTRACTS`，全仓守卫 `test_recovery_code_policy::test_all_used_error_codes_are_registered` 在 step16c 全量里拦下（单独跑 0/3），两个提交被撤出紧急批次。
  现按真实语义登记：category=model、retryable=True、recommended_action=continue，提示写明请求根本没发出、墙钟重试由放弃方自动做、
  用户停止按停止处理。教训：定向门禁固定带七个全仓扫描守卫（architecture_guardrails、recovery_code_policy、constant_names_unique、
  config_field_readers、recovery_actions、main_agent_has_no_case_runtime、subagent_config_inheritance）。
- **门禁**：ruff、doc sync、strict code-size（identity 对 main 无新增）、`git diff --check`、clean package；`CODE_SIZE_REPORT.md` 不入提交。

## 记忆策展失败归因：解析失败带响应形状、包装异常带根因（2026-09-29，分支 `claude/ae-curator-diagnostics`，基于 `c101d325a`）

- **起因**：只读排查生产主 owner 自 9/26 以来的 8 次 `CURATOR_SCHEMA_INVALID` 和 3 次 `CURATOR_COMMIT_FAILED`。
  - 每次失败后，下一轮都从同一游标重做并成功，没有丢批次。
  - 但运行账分不清解析失败是截断、空内容还是格式坏，提交失败也只剩外层异常。
  - 事实与取舍见 `DESIGN_LEDGER.md` 记忆 Curator 条目的“事实 3”。
- **新增** `test_curator_failure_attribution.py` 7 项：
  - 截断响应：`truncated=true`、`stop_reason=length`、`output_tokens`，`cause_pos` 等于 `response_chars`。仍记 `CURATOR_SCHEMA_INVALID`，只调用一次，游标不动。
  - 空内容：字符数 0、出错位置 0、未截断。
  - 正向对照：下一轮给合法输出即成功提交，游标从同一起点推进。
  - 提交时注入 `ENOSPC`：记 `cause_type=OSError`、`cause_errno=28`，诊断里不含路径；恢复写入后重做成功。
  - 根因只沿显式 `from` 链查找：`from None` 和隐式上下文不算，三层链取最底层。
  - 响应形状只收短码与整数：布尔不当 token 数。
  - 字段全带上时不超过 300 字符；类名异常长时退回只含类名的形状。
- **改动的既有用例**：`test_curator_timeout_observability.py` 里模型调用失败那条逐字断言，多了 `"cause_type":"ValueError"`。
- **验证**：
  - 12 个单点变异全部被新用例抓住：去掉根因、只取一层、改走隐式上下文、去掉 errno、去掉出错位置、不并入响应形状、去掉超长回退、解析失败原样抛出、不遮蔽非码 stop_reason、布尔当 token、截断恒假、字符数恒零。
  - 相关与全仓扫描类用例共 56 个文件全过。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_curator_failure_attribution.py agent_py_agent/tests/test_curator_timeout_observability.py -q`

## observe 不挡主链路（2026-09-28，分支 `claude/9a-jev-observe-async`，基于 `80b4afed8`）

- **新增** `test_decision_observe_nonblocking.py`（首个提交 29 例，复审跟进后 33 例）。链路是原设置 → 决策服务 → 后台执行器 → 真实本地 HTTP → 结果日志与用量账：
  - **主链路先返回**：
    - 本地服务卡住时，decide 立刻拿到 deferred；放行后后台写一行 `blocking=false` 的成功行，用量事件的 source 为 `decision_observe`。
    - 发起 run 的请求与 run 累计容器里都没有这次调用，会话汇总只算一次；在途登记已注销，回合的 live 状态和输出流都没被写。
    - 对照：开关关闭时 observe 仍同步，用量记在 run 里；开关打开时 apply 仍同步。
  - **期限**：
    - 后台按 `background_timeout_seconds` 等到了前台单次上限挡不住的回答。
    - 前台阶段预算已耗尽时后台照常发出；开关关闭时照旧返回 budget_exhausted（两者成对）。
    - 后台超时照同步口径记 timed_out。
  - **有界**：同一时刻只有一个后台调用。排队满了记 `skipped/observe_nonblocking_busy` 行和 reach 内部码；上限常量钉为 8。
  - **停止与撤销**：
    - 发出前停止的不发送、也不记账；已发出的自然结束并记账（两者成对）。
    - 设置撤销与宿主关闭时，排队中的调用不发送、不建调用记录。
  - **身份**：后台线程没有 runner 上下文，靠发起时捕获的 run 身份通过复核。
  - **资源键**：后台 observe 在途时，同会话的同步 apply 照常发出并成功。
  - **召回主链**：本地服务卡住时，`rerank_recalled_memories` 立刻返回原顺序和 `observe:deferred`。
  - **选模型**：
    - 主模型开始时标记是 deferred；后台在回合内完成后补记成 observed。
    - 回合结束后不再回写。
    - 写入器只替换同一观察的 started/deferred 标记，`adopted` 恒为 false。
  - **设置视图与计数口径**：后台点位标 `blocking=false`，上限显示后台单次等待；忙码不计入 reached/not_called。
- **修改**：
  - `test_decision_outcome_log.py`：结果行多了一个 `blocking` 字段。
  - `test_tui_decision_menu.py`：新增 `scope_index`，按 `_GENERAL` 的口径计算范围菜单的行位置，替换写死的下标，以后再加通用字段不必逐个改；`test_decision_skill_tool_settings.py` 同步改用它。
- **变异验证**：设 `PYTHONDONTWRITEBYTECODE=1`，每次跑完按 sha256 还原源文件；25 个变异体全部被抓住：
  - 永不转后台、apply 也转后台、忽略开关；
  - 预算改用前台单次上限、阶段预算仍然生效；
  - 不装捕获身份、账本保留 run、共用 run 的请求范围、不结算用量；
  - 队列上限差一、每条任务起一个线程、在途登记不注销；
  - 忽略回合取消、发送前不复核撤销；
  - 占位结果写行、占位结果或最终结果或结果行没标非阻塞；
  - 共用同步资源键；
  - 补记只认 deferred、丢完成回调、选模型不挂回调；
  - 忙码计入诊断、忙码不记 reach、设置视图恒为阻塞。
- **定向回归**：77 个文件，1834 passed、0 skipped。范围是全部 decision 测试、Gateway 选模型/采用/停机测试、设置与 YAML 同步测试、TUI 决策菜单，以及扫描全仓源码的护栏（architecture_guardrails、constant_names_unique 等）。新文件另外连跑 5 遍、3 进程并发各跑 1 遍，都全部通过。
- **复审跟进**（dsh-ae 复审 `ec3732f49` 后的同分支提交）：
  - 补杀两个存活变异体：
    - worker 不恢复 runner 身份：新例在完成回调里读 `current_subagent_run_id`，应当为空；
    - 补记不核对 claim：补记用例参数化加一组 deferred、同一 op、别的 claim，期望仍是 deferred。
  - 身份快照真拷贝：发起方在请求已发出、尚未返回时改自己的任务属性，后台身份复核仍通过、照常成功。
  - 停机有界等待：写行被放慢 0.3 秒时，`gateway_process._cancel_active_decisions` 返回前，`stale/host_shutdown` 行已经落盘。
  - 开关关闭时决策的逻辑与时序不变，但结果日志每行多一个附加字段 `blocking`，所以不再写“逐字不变”。
  - 变异验证扩到 29 个，全部被抓住。新增的 4 个是：不恢复身份、补记不核对 claim、身份不拷贝、停机不等待。
  - 定向回归：同样 77 个文件，1838 passed、0 skipped。新文件另外连跑 5 遍、3 进程并发各跑 1 遍，都全部通过。
- **ae 复核后的补丁**：停机时的取消与等待拆成两个 try。新增 `test_gateway_decision_shutdown_cancel.py::test_gateway_cleanup_still_cancels_when_the_observe_drain_fails`，分两组：等待抛异常、执行器模块导入失败（`sys.modules` 置 None）。两组都要求取消照常执行，并且只记 `gateway_decision_drain_failed`。把函数换回单个 try 的旧写法后，这两组都失败。
- **rebase 到 step16b 后的语义合并**（与 be 的链路计时、统计口径合并）：
  - 后台行同样带 transport：成功行 call_status=finished，有一次尝试的分段毫秒；后台超时行 timeout_phase=first_byte，本地估算输入进独立用量范围的 `estimated.unfinished_*`。
  - 统计口径：忙码 `skipped/observe_nonblocking_busy` 与发出前取消的 `stale/turn_cancelled` 都不是失败类，照原状态计数，`not_sent` 为空；后台超时算 Jev 失败。
  - `test_decision_transport_timing.py` 的最近行字段集合补上 blocking：最近行的固定字段现在含 blocking，没有 transport 的行仍不带 transport 键。
  - 回归（rebase 到 `c101d325a` 后）：
    - 定向 77 个文件加 be 的两个新测试文件，共 79 个文件，1870 passed；
    - step16b 改过的测试文件（真实链路、桌面防线、账本、会话存储等 6 个）127 passed、4 xfailed，xfail 是约定的严格 xfail；
    - rebase 后的第一个提交单独检出，定向 6 个文件 88 passed。

## 定时执行 waiting 死锁与未知结论保留（2026-09-29，分支 `claude/75-scheduler-waiting-deadlock`，基于 `c101d325a`）

- **来源**：my-agent-2/4 的 300 秒定时任务永久停摆。前台 `run_command` 回报通用 `TOOL_OPERATION_OUTCOME_UNKNOWN` → 回合 unfinished、任务仍 active → 无条件 `park_waiting`；waiting 只在任务终态时对账、同一 job 有 run 就不派发，于是死锁。两条操作的 `unknown_reason` 只剩通用码、`outcome_json.result` 为空。
- **`test_scheduler_waiting_deadlock.py`**（真实 SimpleAgent、真实调度账本和会话存储）：
  - 无后续工作 + 工具结果未知：任务 blocked、run 记 failed（`SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN`）、排一条 `scheduler:<job_id>` 宿主提示（写明需人工确认是否重做），下一周期照常预约新 run；
  - 正向对照：挂着一条真实子代理生命周期唤醒时照常 waiting，任务仍 active、没有提示，下一周期被 active run 挡住（原行为）；
  - 其它 unfinished 原因用 `SCHEDULED_TASK_UNFINISHED`；正在处理的唤醒不算自己的后续工作；任务被并发改掉或任务权威读写失败时只释放租约；服务没注入判定时 fail closed 保持 waiting；
  - 存量 waiting 出口：599 秒不动、600 秒结算为 `SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP` 并放行下一周期；有后续工作时停再久也不动；任务已 blocked 的 waiting 按 `SCHEDULED_TASK_BLOCKED` 结算；
  - 后续工作判定逐项正反例：活跃/受阻 Goal、guidance、未终态/已结束子代理、唤醒（含定时触发和忽略 id 的排除、别的任务）、后台命令（真实进程记录，完成通知发出前后、别的会话存储、权威读失败抛错）、进度策略（含别的任务、读失败）、缺存储或任务 id；
  - 提示按 job 合并：同一 job 连续受阻只留一条，不同 job 各留一条；
  - 端到端：真实 `BackgroundMainAgentRuntime` + 调度器 tick，模型尝试替换为 `runtime_status=unfinished / runtime_reason=TOOL_OPERATION_OUTCOME_UNKNOWN` 的结构化结果，验证报告带出原因、run 记 failed、任务 blocked、提示入队，下一次 tick 照常执行。
- **`test_tool_unknown_reason_preservation.py`**：受阻重放保留 `TOOL_TIMEOUT`；handler 回报具体码时 `unknown_reason` 带具体码，只回报通用码时保持原样；`_unknown_claim_result` 取码顺序四组参数；受管账本 UNKNOWN 写入 result 与 error_code、空结果不写；后台 attach 失败 / 状态未确认 / 启动清理未确认各自的具体码及对照。
- `test_scheduler_runtime.py` 原 waiting 用例改为先放一条真实生命周期唤醒：进 waiting 现在必须有后续工作事实。
- **变异**（`PYTHONDONTWRITEBYTECODE=1`、每个变异独立 pycache 前缀，跑完逐字节还原）：64 个，首轮 59 个被抓住；补了进度策略读失败/跨任务、后台命令跨会话存储、取码顺序两组测试后，5 个存活全部被抓住，64/64；收口逻辑移到 `scheduler/active_run_closeout.py`（`SchedulerService` 触到类长度硬线）后按新位置重跑，仍 64/64。
- **回归**：46 个相关测试文件（调度、工具操作账本、后台命令、后台运行时、宿主提示、恢复合同及全仓扫描守卫）；`test_recovery_code_policy` 先抓到两个新码未登记进 `ERROR_CONTRACTS`，已补登记。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_scheduler_waiting_deadlock.py agent_py_agent/tests/test_tool_unknown_reason_preservation.py -q`
- **复审 B 的跟进（be 的 M-B1 与三条应改）**：
  - be 的探针 P3 改写为回归（`TestGoalContinuationGap`）：对账落在 Goal 续跑空隙里只记下，下一条续跑唤醒出现即清掉，任务与 Goal 都不受影响；
    健康长 Goal 跨 12 片、每片之间都有一次空隙对账，从头到尾不被结算；真正没有后续工作时第一次只记下、59.9 秒不结算、60 秒结算，提示写明
    “只有进度策略……不能只靠它们续命”；
  - 原 7 条“单次对账即结算”的用例改为隔 60 秒对账两次（`_reconcile_confirmed`）；
  - be 的存活变异：坏唤醒一直在、中间出现一次在跑的子代理，6 倍计时从那之后重新起算；
  - 本任务血缘子代理的坏完成唤醒（`root_task_id` 为子代理树根）记读不出，别的子代理的、以及前缀不同的去重键不计入；
  - 变异 9 个全部被抓住（含 be 列出的“只在读全时清零”）；以上新用例与改写用例在提交 B 的代码上全部失败（跨类计时用例除外，它专门杀那个变异）。
- **复审修复 B（be 的 S1–S5）**：
  - 真实血缘（S5）：`agent.subagents.create_run` 建真实子代理、`attrs.conversation_task_id` 为 srun；子代理在跑 → waiting；
    子代理结束、完成唤醒待处理 → 过宽限期仍 waiting；唤醒处理、任务收尾 → 对账按 done 结算；
  - 刚终态（S1/S2）：收口时子代理刚结束、唤醒未发 → waiting 而不是受阻；已终态子代理的“刚终态窗口 / 待处理完成唤醒 /
    收口 WAL 未交付 / 已交付”逐项正反；取消、放弃等不发完成唤醒的状态和嵌套孙代理不计入；
  - 宽限期（S1）：`waiting_grace_seconds` 五组取值；组合根把配置 200 秒接成 1000 秒宽限期和同值窗口；宽限期更长时退出相应推迟；
  - 宽限期内事实（S4）：只有 Goal / guidance / 进度策略时，宽限期前一秒仍 waiting、满宽限期结算；集合钉死为这三项；
  - 节流（S3，`test_scheduler_scan_costs.py`）：过宽限期后 0、1、30、59.9 秒四次对账只算一次，60 秒再算一次；
  - 3a 转来的三条：孤儿巡查 1200 秒时推导宽限期 6000 秒，6×6000 前仍 waiting、满 6×6000 结算；读不出从首次观察起计时——
    中途出现会自己推进的唤醒会让计时重来、读全则按“没有后续工作”正常结算、宽限期内事实（活跃 Goal）不清零计时；
    血缘里有、记录缺失的子代理记为读不出（`FileNotFoundError`），不当成“还有子代理在跑”；对账时刻一路传到“刚终态”窗口。
- **复审 A 的跟进修复（be 的 M-A1、M-A2 与 nit）**：
  - 同类“确认存在优先”：本任务有待处理唤醒、另有归属不明的坏唤醒时，满 6 倍宽限期仍 waiting（be 探针 P2 翻转）；
    唤醒 / 进度策略各加“同类坏记录 + 确认存在 → 仍算存在”；后台命令加三种坏记录（解析不出 / 本任务 / 空目标）下确认存在仍为 True；
  - 空目标：坏记录的 `completion_target` 为盘上规范空形状 `{}` 时按“不欠任何任务的通知”跳过（be 探针 P1 翻转）；
    原归属用例改为底记录先发完通知、坏记录按生产形状（合法记录改坏实例字段）写入，并补缺键与 `{}` 两例；
  - 以上 7 条新用例在修复前的代码上全部失败；变异 5 个全部被抓住；
  - 告警节流表清理改为先拍快照再遍历（并发插入不再抛 RuntimeError），未单独写并发用例。
- **复审修复 A（be 的 M1、M2）**：
  - be 的两个反例（坏唤醒文件让收口永远 waiting、CAS 失败后没有退避）在修复后都翻转为失败，已改写成回归：
    `TestUnreadableFollowUp::test_an_unattributable_corrupt_wake_waits_then_settles_at_six_grace_periods`
    （6 倍宽限期前 1 秒仍 waiting，满 6 倍结算为 `SCHEDULED_TASK_FOLLOW_UP_UNREADABLE`、任务受阻、提示入队、下一周期派发），
    `test_unreadable_task_authority_only_releases_the_claim`（释放后 `_wake_retry_after` 为 1003+30）与成功收口不设退避的对照；
  - 归属限定：坏唤醒（解析不出 / 属于本任务 / 属于别的任务）、坏进度策略（本任务 / 别的任务）、坏后台命令记录
    （解析不出 / 本任务 / 别的任务 / 别的会话存储 / 没有 completion_target）逐一正反；别的任务的坏唤醒不会拖住本任务收口；
  - 读不出与确认存在并存时，确认存在的后续工作优先；子代理目录整体读不出记为读不出而不是“有子代理”，原收尾判据仍 fail closed；
  - 结构化告警 `scheduler_follow_up_unreadable` 带项目与错误码，按 run 每 600 秒最多一条；新结算码登记在错误合同。
  - 变异：本次 30 + 5 个（含把逐项检查拆成 `_run_check` 后重跑）全部被抓住；原 64 个中锚点仍在的 50 个重跑全部被抓住（其余 14 个的语义由本次新变异覆盖）。

## 会话互通真实链路测试：补 list_owner_sessions 用例（2026-09-29，分支 `claude/ae-session-real-chain-test`，基于 `6c2fad4da`）

- **新增用例**：`test_session_task_real_chain.py` 加了 `test_admin_lists_owner_sessions_and_reaches_the_listed_target`，按派活、发消息参数化成两条。
  - 正向对照：
    - A（管理员）在真实 Gateway 前台回合里调用 `list_owner_sessions`；
    - 假线路只凭返回的清单挑目标：不是当前会话、最近活动、`allowed_kinds` 含所需类型，A 的提示里不出现 B 的 id；
    - 随后派活或发消息，由 Gateway 同款后台调度器执行；
    - 派活时断言任务目标是 B 且状态 done；发消息时断言 B 的唤醒回合看到了这条消息。
  - 清单断言：
    - 恰好是本 owner 的 A、B、C 三个会话，`current_thread_id` 是 A；
    - A 行 `is_current=true`、`allowed_kinds=[]`，B 行允许所需类型；
    - `unreadable_records=0`，每行只有 6 个结构化字段。
  - 跨 owner：同一个 home 下再建一个普通用户 owner（feishu/user），经真实前台 ask 建出会话，这个会话不出现在清单里。
    - 真实部署里每个 owner 各有自己的会话存储，所以本用例保护的是“只读本 owner 的存储”。
    - 同一存储里混入别的 owner_home 记录的情形，由 `test_list_owner_sessions_tool.py` 覆盖。
  - 不含正文：B 和其它 owner 的会话里各写一个 RC-SECRET 标记，清单序列化后既不含这两个标记，也不含提示原文。
  - 测试文件里留了待办：将来 `list_owner_sessions` 对普通用户开放时，要补权限用例。
  - 夹具顺手抽出 `_agent_config`、`_ready_gateway_paths`、`RealChain.open_session` 三个小函数，行为不变。
- **验证**：
  - 基于 `6c2fad4da`（已含 `list_owner_sessions`）全文件 7 通过、4 xfail，4 条 xfail 与原来相同。
  - 上一个提交单独跑也全部符合预期（5 通过、4 xfail），两个提交可以分开并入。
  - 变异（只改 scratch 导出里的 6933f4c97），两个都被抓住：
    - 清单行多带 title → 两条都失败在“清单行只能有结构化字段”；
    - `is_current` 恒为假 → 失败在 A 行的断言。
  - 测试防线 e9be5de14 上预跑原有 9 条，零拦截；防线尚未并入 main。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_task_real_chain.py -q -rxX`

## SkillSnapshotError 整族结构化错误码（2026-09-28，分支 `claude/75-snapshot-error-code`，基于 `6c2fad4da`，单独先并）

- **改动**：`SkillSnapshotError(error_code, detail=, *, reason=)`，`error_code` 是唯一机器可读码，消息仍是
  "码 + 明细"原格式；技能快照、包读取、包资源、任务引用各抛出点改为传码常量。引用形状错误改抛
  `SkillReferenceError(ValueError)`（带 `error_code`），包装时读属性不再 `str(exc)`。`skill_search` 的快照失败回执在
  `details.error_code` 带出结构化码。供唤醒毒丸分类按 `error:<error_code>` 精确归因（见 `docs/design/WAKE_POISON_PILL.md`）。
- **测试**：新增 `test_skill_snapshot_error_codes.py`：AST 守卫要求产品代码里整族每个抛出点的第一个参数是码常量或
  结构化码属性（正反样例各一组，证明守卫不空跑），以及码与消息分离、引用错误仍是 ValueError、包装保留码。
  原来用 `match=` 或消息前缀断言码的 10 处测试改读 `error_code`，失败回执的整包断言补上 `details.error_code`。
- **定向回归**：涉及快照、skill_search、任务引用、包读取的 33 个测试文件加护栏：963 passed。

## 两条负载敏感用例改稳：网关排队续期、TUI 插件目录管道（2026-09-29，分支 `claude/38-deflake-shell-adapter`，基于 `6c2fad4da`，只改测试）

- **来源**：集成者报告 12 分片里 `test_gateway_admission_wait.py::test_real_worker_signal_keeps_real_client_waiting` 与
  `test_tui_plugin_directory_pipe.py::test_native_keybindings_refresh_accept_stash_and_submit_original_revision[manual]`（现象 `requests 为空`）
  各偶发失败，单独重跑通过。
- **满载复现**（8 个 CPU 忙循环 + 6–8 个 pytest 进程各循环 12 次）：两条都未复现（排队用例 72 + 96 次、TUI 用例 72 次全部通过），
  改法按代码里读出的时序窗口做。
- **排队用例的窗口**：原来 worker 每 0.04 秒写一次等待信号、写满 5 轮就落终态，客户端不活跃窗口 0.15 秒；满载下客户端一次采样
  （最小间隔 0.1 秒加读盘）就可能超过 0.15 秒而收口，或 worker 五轮在客户端第一次采样前就写完。
- **排队用例改法**：worker 每写一次等待信号都等客户端真的续期过一次（给 `_extend_active_request_deadline` 包一层，续期成功放行信号量）
  再写下一次，第 8 轮才写终态；初始 deadline 0.5 秒远小于至少 0.8 秒的完成时间，续期一失效客户端就在 0.5 秒内收口。断言改为
  `rounds == 8`、`admission_wait_count >= 2`，终态正文不变；不活跃窗口 2 秒只是满载采样抖动的余量，过期语义仍由紧邻的负向用例锁定。
- **TUI 用例的窗口**：Tab 之后 prompt_toolkit 先同步建一个空的 `complete_state`，再到线程里拉插件目录；原用例一看到 `complete_state`
  非空就断言目录请求已发出，线程晚一点起步就是 `requests 为空`。起跑、送键后的固定 0.06 秒睡眠在负载下也不够。
- **TUI 用例改法**：全部改成 `wait_app` 结构化等待（3 秒只是防挂起上限）：起跑等 `is_running` 且首帧已画；送键后等正文到达且
  边打边补全不留候选；Tab 后等候选真的出现，再断言只发过一次 `catalog` 请求；接受候选、Ctrl-S 暂存、Ctrl-R/Ctrl-C 切换焦点、
  回车提交都等对应的 buffer/焦点/请求事实。被测行为与断言不变。
- **负向验证**（改坏产品语义，独立子进程运行，逐字节恢复核哈希，7/7 被抓出）：客户端不续期；worker 不写等待信号；Tab 不拉目录；
  接受候选丢 revision；提交用当前目录 revision；Ctrl-S 不恢复暂存；Ctrl-C 不交还焦点。
- **修复后满载**：两条各 6 进程 × 20 次并行（8 个忙循环）全部通过（TUI 用例两个参数化变体一起跑）；两个文件与
  `test_architecture_guardrails` 单跑通过。
- **门禁**：ruff、doc sync、strict code-size（TUI 用例两个 nesting 项由 hard 降为 soft，无新增 identity）、`git diff --check`、
  clean package；`CODE_SIZE_REPORT.md` 不入提交。
- 观察（未改产品、未改用例）：满载复现时 `test_tools/test_shell_background.py::test_background_immediate_failure_is_not_reported_started`
  1/72 失败：立即退出的子进程在 0.5 秒 settle 内还没退出就被判成 `started`，属于启动观察窗口的设计取舍，交产品作者判断。

## 两条负载敏感用例改稳：后台命令不阻塞、通道路由立即返回（2026-09-29，分支 `claude/38-deflake-shell-adapter`，基于 `f7a4cc909`，只改测试）

- **来源**：集成者报告 step15z 的 12 分片里 `test_tools/test_shell_background.py::test_background_does_not_block` 与
  `test_adapter_manager.py::TestChannelManagerDurableDelivery::test_route_returns_immediately_and_worker_keeps_polling_past_sixty_misses`
  各偶发 1 次，单独重跑 3/3。
- **满载复现**（8 个 CPU 忙循环 + 6 个 pytest 进程各循环 12 次）：后台用例 40/47 失败，全部是 `耗时 < 1.2 秒` 的墙钟断言
  （实测 1.21–2.25 秒：宿主启动加 0.5 秒 settle 的启动观察本身就超过它）；路由用例本轮 0/72 未复现，集成者记录的失败是
  `monotonic 差 < 0.1` 的墙钟断言（入口落盘含 fsync）。
- **后台用例改法**：子进程改为 15 秒任务，判定只看结构化事实——回执 `status=started`、无 `exit_code`、返回那一刻登记表会话
  `running`；用例结束经登记表 kill。若 execute 等子进程退出，回执会是 exited（负向验证抓出）。
- **路由用例改法**：答案由主线程放行（Event）：`route_message` 返回后先断言尚未送达、finalize 未调用，再放出答案等待送达；
  轮询超过 65 次 + 放行才给答案，10 秒安全网只让"route 阻塞"这类回归有界失败。`poll_count > 60`、只送达一次等断言不变。
- **负向验证**（改坏产品语义，独立子进程运行，逐字节恢复核哈希，2/2 被抓出）：后台模式等子进程退出后才返回；route 阻塞 12 秒再返回。
- **修复后满载**：两条各 6 进程 × 20 次并行（8 个忙循环）全部通过；两个文件与 `test_architecture_guardrails` 单跑通过。
- **门禁**：ruff、doc sync、strict code-size（identity 对 main 无新增）、`git diff --check`、clean package；`CODE_SIZE_REPORT.md` 不入提交。

## 两条负载敏感用例改稳：交互式停止接纳、插话墙钟重试（2026-09-29，分支 `claude/38-deflake-stop-steer`，基于 `f7a4cc909`，只改测试）

- **来源**：集成者报告 step15y 在 Mac 12 分片时 `test_gateway_agent_control_service.py::test_interactive_stop_freezes_before_ack_and_does_not_wait_for_cleanup`
  与 `test_steer_delivery_recovery.py::test_wall_timeout_retry_resubmits_the_steer_under_the_retry_call` 各失败 1 次，单独重跑 3/3。
- **满载复现**（8–10 个 CPU 忙循环 + 6 个 pytest 进程各循环 12 次，宿主同时跑集成分片，负载 50–70）：停止用例 68/72 失败，全部是
  `assert elapsed < 0.5`（实测 0.53–0.81 秒，停止准备阶段落盘变慢）；插话用例 4/72 失败，都是重试调用也被 0.05 秒墙钟判超时，
  其中一次还观察到第一次调用的工作线程在墙钟放弃之后才提交插话，重试提交时报 `guidance submission was not reserved`。
- **停止用例改法**：不再用墙钟代理“不等待清理”。清理替身被测试扣住（`release` 未放行）时接纳回执已返回，且 `finished` 未置位、
  状态已冻结为 `CANCELLED`、清理线程看到的状态、去重回执均为结构化断言；两个 Event 的等待上限只是防挂起安全网。
- **插话用例改法**：墙钟只对第一次物理调用生效，且在假后端真正进入（插话已随该次调用提交）后才开始计 0.05 秒；重试调用沿用
  配置的 10 秒。被测行为（墙钟超时 → 插话退回 → 重试重新提交、`calls == 2`、批次 `rejected → submitted`）与断言不变。
- **负向验证**（改坏产品语义，独立子进程运行，逐字节恢复核哈希，5/5 被抓出）：接纳等待清理线程；回执前不冻结；去重失效；
  失败调用不退回插话；超时后不重试。
- **修复后满载**：两条各 6 进程 × 20 次并行（8 个忙循环、集成分片同时运行）全部通过；两个文件与 `test_architecture_guardrails` 单跑通过。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package；basetemp 全部放在本会话 scratchpad 并在跑完后删除。
- 观察（未改产品）：`request_timeout` 极短时，被墙钟放弃的第一次调用的工作线程仍会在放弃之后提交插话，使重试提交撞上“未预留”；
  生产超时为秒级，线程启动延迟远小于超时，正常配置下达不到这个窗口，留给产品作者判断是否在提交前复核调用是否仍是当前调用。
## 检索侧正文哈希向量缓存（第三版，2026-09-28，分支 `my-agent/self-dev-2-vcache`，基于 `f7a4cc909`）

第三版是二审后的返工：**分支重建**（先前把新提交叠在被退回的提交上，第二次犯）、**X1**（跨进程合并复活别人删掉的键）、
**P5b**（复核与写入之间的窗口）三条必须改，外加 6 条建议改。

### 二审新增的缺陷与修法

| 编号 | 缺陷 | 修法 | 钉住它的测试 |
|---|---|---|---|
| **X1** | `_flush` 把整份内存快照叠到磁盘，会复活别的进程已删掉的键（连带复活已删除事实的向量） | `_flush(added=..., removed=...)` **只写本次增量** | `test_x1_flush_does_not_resurrect_key_deleted_by_other_instance`（B 必须先在内存里持有旧键）、`test_x1_flush_keeps_key_added_by_other_instance` |
| **P5b** | 复核在锁外、`put` 在锁内，复核之后发生删除仍会把已删向量写回 | `put(rows, keep=...)`：把身份复核**收进缓存锁**；同时删掉锁外那套等价复核（冗余判据测不到） | `test_p5_delete_during_embed_leaves_no_orphan`、`test_p5b_put_rechecks_validity_inside_lock` |
| 建议 | 坏值让整个缓存作废 | `_load` 逐键跳过坏值 | `test_load_skips_bad_value_but_keeps_other_keys` |
| 建议 | 写失败静默吞掉 | `last_write_error` + 反映到健康状态 | （随 `cache_write` 健康路径覆盖） |
| 建议 | 孤儿回收只在手动命令里 | 挂进 owner 维护（默认 24h） | `test_index_all_reclaims_even_without_local_store` |

### 变异验证（脚本已提交进仓库：`scripts/mutate_text_vector_cache.py`）

运行：`python3 scripts/mutate_text_vector_cache.py`（在仓库根目录，对源码做精确替换后跑 `tests/test_memory_vector_cache.py`）。
**当前结果：killed=12 survived=0 skipped=0。**

| 编号 | 文件:锚点 | 原文 → 替换 | 预期变红的测试 |
|---|---|---|---|
| M1 | `agent/memory_store/jsonl.py:_live_cache_keys` | `cache.put(rows, keep=lambda: self._live_cache_keys(fingerprint))` → `cache.put(rows)` | `test_p5_delete_during_embed_leaves_no_orphan` |
| M2 | `agent/memory_store/jsonl.py:_text_vector_cache` | `TextVectorCache(... "memory_text_vectors.json")` → `... "memory_vectors.json"` | `test_cache_lives_in_its_own_file_and_never_rewrites_plaintext_store` |
| M3 | `agent/retrieval/hybrid.py:_vector_order` | 长度守卫 `len(cache[doc_id]) == qlen` → 去掉 | `test_cache_vector_length_mismatch_is_rembedded_not_used` |
| M4 | `agent/retrieval/text_vector_cache.py:embedder_fingerprint` | payload 去掉 `api_base` | `test_fingerprint_differs_on_endpoint_and_never_leaks_key` |
| M5 | `agent/memory_store/jsonl.py:_forget_cached_vectors` | `index_text(content, attributes)` → `content` | `test_p1_keywords_record_cache_is_removed` |
| M6 | `agent/memory_store/jsonl.py:superseded_versions` | `record.action == "replace" and ...` → `False` | `test_p2_replace_drops_old_text_key` |
| M7 | `agent/memory_store/jsonl.py:index_all` | 删掉无 `local_store` 分支里的回收调用 | `test_index_all_reclaims_even_without_local_store` |
| M8 | `agent/retrieval/text_vector_cache.py:_flush(removed)` | 删掉 `on_disk.pop(key)` 循环 | `test_x1_flush_does_not_resurrect_key_deleted_by_other_instance` |
| M9 | `agent/memory_store/jsonl.py:_cached_vectors_for` | 删掉成功时的 `_record_semantic_health("cache_read")` | `test_h1_cache_read_success_restores_health` |
| **MX** | `agent/retrieval/text_vector_cache.py:_flush(merged)` | `on_disk.update(added)` → `on_disk.update(self._items)` | `test_x1_flush_does_not_resurrect_key_deleted_by_other_instance` |
| MP5B | `agent/retrieval/text_vector_cache.py:put(keep)` | 删掉 `keep` 判据应用 | `test_p5b_put_rechecks_validity_inside_lock` |
| MLOAD | `agent/retrieval/text_vector_cache.py:_load` | 删掉坏值 `try/except` | `test_load_skips_bad_value_but_keeps_other_keys` |

### 本轮的自我修正（都由变异验证打回，不是靠推理）

1. **MX 第一版存活**：我的 X1 测试里 B 实例的内存快照是**空的**（`get` 不填充 `_items`），
   所以"只写整份内存快照"这个变异体没有东西可复活 → 测试假绿。改成让 B 先 `put` 一次、内存里真的持有旧键后才 KILLED。
   **教训：构造测试时必须让变异体真的有机会犯错，否则测的是空气。**
2. **M1 第一版存活**：锁外的 `live` 复核与锁内的 `keep` 复核等价，前者永远在后者之前挡掉问题 → 前者不可达。
   删掉锁外那套、只留锁内单一权威判据后 M1 才 KILLED。
   **与上一轮同源：两套等价判据并存 ⇒ 其中一套永远测不到。**
3. **M7 第一版存活**：`index_all` 在无 `local_store` 时提前 `return 0`，回收被跳过；我原来的测试带了 local_store，走不到那条路。
   改为断言 `mem.local_store is None` 的路径后才 KILLED。

### 历史：第二版（`657b485bf`，基于 `f7a4cc909`）

- **来源**：DESIGN_LEDGER「召回前补充查询」缺口 3 的第一版实现（`9c6c3cc50`）被 dsh-9b 独立复审驳回，必须改 5 条。复审探针在
  `/private/tmp/claude-501/-Users-xiaoyezi-my-agent-dsh/c92775e1-d4e5-4379-b32a-247934f7a565/scratchpad/review_probes/`。
- **第一版的真实缺陷**（每条都有探针复现）：
  - P6（**隐私**，最优先）：缓存与权威向量同处 `memory_vectors.json`，只做检索的进程用启动时旧快照整文件写回，
    把已删除事实的**明文**重新写进文件；
  - P4：缓存项进入 `VectorStore.search` 的全库线性扫描，挤掉真实条目的 top_k；
  - P3：同名模型换了实际向量长度时，旧长度向量喂进 `mean_center` 越界抛 `IndexError`，**整轮对话失败**；
  - P1/P2：写入键含 `keywords_en`、清理用裸正文 → 带关键词的事实删不掉；`replace` 不清被覆盖的旧正文键；
  - P5：取缓存与回写之间事实被删，检索回写把已删事实的向量复活。
- **做法**：新建 `agent/retrieval/text_vector_cache.py`（独立文件 `memory_text_vectors.json`）。
  键 = `sha256(实现类名 + api_base + model)[:16]` 指纹 + 正文 SHA-256；
  `index_text(content, attributes)` 统一检索/写入/清理的文本口径；
  `_vector_order` 增加"长度必须等于当轮 query 向量"的命中条件；
  `_remember_cached_vectors` 回写前按 active 身份复核；
  `_forget_cached_vectors` 接收记录级 `(content, attributes)`，已删取历史全部版本、`replace` 取被覆盖的旧版本；
  `index_all` 回收孤儿键；跨进程写走 `locked_json_path` sidecar 锁并在锁内重读合并（删除时显式排除被删键，
  否则磁盘旧值会把刚删的键加回来——这个坑第一轮实打实踩到了）。
- **新测试**：`tests/test_memory_vector_cache.py` 共 26 条，含 dsh-9b 建议的
  E1（直接比 rank 输出的 `(id, score)`，全量与部分缓存两种）与 E2（冷热两次对比、缓存必须经文件往返），
  以及 P1–P6、H1 转成的正式回归测试。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_memory_vector_cache.py -q`
  → 26 passed。连同 semantic recall / retrieval / embedding service / minimax / integration 共 **77 passed**。
- **变异验证 9/9 KILLED**（脚本 `/tmp/mutate_t9b.py`，对源码做精确替换后跑同一文件）：
  回写不做 active 身份复核 / 缓存写回权威向量库 / 去掉长度守卫 / 键不含端点指纹 / 清理键退回裸正文 /
  `replace` 不清旧版本 / `index_all` 不回收孤儿 / 删除不回读磁盘 / 成功时不清除健康错误。
- **本轮两次真实自我修正**（都靠测试/变异打回，不是靠推理）：
  1. 初版在 `_cached_vectors_for` 里算 `superseded`，但删掉的记录已不在 active，判据恒空；实际竞态发生在
     **嵌入期间**（`_cached_vectors_for` 已经跑完），必须挪到回写点用 active 身份复核。
  2. 两处判据并存时，"回写跳过 superseded" 的变异体**存活**（被身份复核提前挡掉）——说明它是不可达的冗余防御。
     删掉冗余判据、只留单一权威检查后，M1 才被杀死。教训与上一轮同源：**变异体必须与目标实现有可观测差异**，
     两套等价判据并存会让其中一套永远测不到。

## 相近文件名建议：修好"owner 墙"用例空跑，并用 MG8 钉住（2026-09-28 第三轮复审，分支 `my-agent/self-dev`，基于 `b409c1b99`）

- **来源**：dsh-9b 20:11 复审。**代码是对的，只改测试。**
- **空跑根因（我已独立复现）**：`_owner_walled_tool` 把 **`owner_home` 当成了工具根**，而文件在
  `owner_home/workspace/docs` 下。请求 `docs/...` 相对工具根解析成 `owner_home/docs/...`，**父目录不存在**，
  于是近名逻辑压根没跑 —— 断言"候选里没有泄露"自然恒真。
  实测证据：`path_not_found=True` 但 `candidate_paths=[]`、`suspected_typo=None`，
  且 `owner_home/docs` 不存在、`owner_home/workspace/docs` 存在。
- **修法**：工具根改成 `inner_root`（请求真正解析到的地方），owner 墙仍是 `owner_home`；
  并新增 `_assert_near_name_logic_actually_ran()` 作为**防空跑闸门**（断言回执里
  `path_not_found is True` 且 `expected_kind` 存在）。已反向验证：把工具根改回旧写法，
  该闸门立刻报错（以前是静默通过）。
- **MG8 验证（9b 指定的关键一步）**：变异 `admits` 里 `return item.is_symlink() or self.access.allows(...)`
  —— 即让符号链接绕过裁决。修复前它在 25 条测试下**存活**；修复后**被 4 条用例同时杀掉**
  （三条原有 N1 用例 + 新增的 G1）。MG2 按 9b 意见不再算变异（跟随链接现在是设计本身），由 MG8 替代。
- **新增 G1 回归**：`test_outside_target_existence_is_indistinguishable_across_full_receipt`
  —— 不只比 `candidate_paths`，而是比 **ok / error_code / output / envelope 四项完整回执**，
  临时路径归一化后要求**逐字相同**；并断言任何一段都不出现墙外真实路径。
- **两处按现行策略更正的期望值**（实测确认，非照抄结论）：
  - **`.env` 在 owner 墙下是放行的**（`check_path_access(.env).allowed is True`），
    所以"owner 墙下不建议 .env"这条**期望值本身是错的**。改为同时钉住两种模式的现行行为：
    owner 墙下会被建议、普通模式下被拒（`PATH_CREDENTIAL_FILE_BLOCKED`）且不被建议。
    **这是策略缺口不是本模块 bug**：远程多用户的 owner 墙模式下 .env 正文会进模型上下文并
    发给模型服务商；已按 9b 要求记入 `DESIGN_LEDGER.md` 作为「待用户决策」。
  - **MG4 的"已杀"说法更正**（见下面权限闸门那节）：补普通模式用例
    `test_sibling_scan_reports_entry_path_not_resolved_target_in_normal_mode`，
    直接断言兄弟扫描报告**条目自身路径**而不是 resolve 后的目标。
- **`edit_file` 的裁决口径**：候选走的是**读取**裁决（`check_path_access`），不是写入范围；
  这是既有设计，只补注释说明、不改代码。
- **门禁**：定向 27 passed（含新增 2 例）；ruff / `DOC_SYNC_PASS` / `git diff --check` / clean-package 见交付说明。

## 参数减量的清理提交：删键留下的过时文档与死写入（2026-09-29，同分支 `my-agent/self-dev-params`）

- **来源**：dev 09-29 00:53 复审 `92e771b8b` 后的要求（4 处），按「文档同步要和删键一起进 main」的约定补一个提交。
- **做法**（全是文案/注释/死代码，**行为不变**，没有新增或修改任何断言）：
  1. `tests/test_subagent_capability_compact.py` 删掉 `agent.config.tool_context_ptl_retry_max = 0` 这行死写入（键已不存在），换成一句说明；
  2. `agent_py_agent/config/agent_config.yaml` 里的「见下方 memory_compact_auto_continue_max_depth」改成「内部具名常量，不再暴露为配置项」；
  3. `docs/design/LONG_RUNNING_EXECUTION.md` 与 `docs/modules/memory/04-structure.md` 里把 `cache_diagnostics_enabled` 从「配置开关」改写成「已降为读取点旁的具名常量」，并去掉「关闭开关不计算摘要」这种已不存在的运行时路径说法；
  4. `frontend/src/pages/settings/SettingsTools.tsx` 那个写死的标签 `tool_retrieval_limit（检索限制）` 改掉 —— 它绑的是前端自己的 `settingsStore.retrieval_limit`，不写后端键，也不在后端 catalog 里。
- **验证**：`agent_py_agent/config/agent_config.yaml` 一改，前端 catalog 需要同步重新生成 —— 跑 `node frontend/scripts/sync-backend-config.mjs`（265 fields）后 `--check` 报 `Config catalog is in sync (265 fields).`，并确认 catalog 里已无这 7 个键名。
- **复现门禁**：`ruff check agent_py_agent/`（All checks passed）、`python3 scripts/check_doc_sync.py`（DOC_SYNC_PASS）、`git diff --check`（干净）、相关定向测试 **161 passed**、四个守卫 **98 passed**、`check_code_size.py`（hard=0、blocked=False）。

## 参数减量：7 个内部实现参数降为读取点旁的具名常量（2026-09-28，分支 `my-agent/self-dev-params`，基于 `f7a4cc909`）

- **来源**：dev 18:42 / 20:03 的参数减量任务。`parameter_registry.listed_parameters()` 从 **221 降到 214**（正好 -7）。
- **做法**：值全部保持不变，只把「没人该去调、调了也没意义」的内部实现参数从配置面挪到读取点旁的模块级具名常量：
  | 参数 | 常量（位置） | 值 |
  |---|---|---|
  | `tool_retrieval_limit` | `agent/core.TOOL_RETRIEVAL_LIMIT` | 12 |
  | `tool_context_microcompact_keep_recent` | `agent_core/tool_context/microcompact.DEFAULT_MICROCOMPACT_KEEP_RECENT`（原有，改读它） | 8 |
  | `tool_context_ptl_retry_max` | `agent_core/tool_context/ptl_retry.DEFAULT_PTL_RETRY_MAX`（原有，改读它） | 3 |
  | `tool_failure_channel_hint_threshold` | `agent_core/tool_guard/loop_hints._CHANNEL_HINT_THRESHOLD`（新增） | 2 |
  | `memory_compact_auto_continue_max_depth` | `agent_core/finalization_compact_auto._DEFAULT_MAX_COMPACT_AUTO_CONTINUE_DEPTH`（原有，改读它） | 50 |
  | `cli_resume_max_rounds` | `cli/resume_loop._RESUME_MAX_ROUNDS`（新增） | 8 |
  | `cache_diagnostics_enabled` | `agent_core/tool_model_generation._CACHE_DIAGNOSTICS_ENABLED`（新增） | True |
- **三处同步**：`AgentConfig` 字段、随包 `config/agent_config.yaml` 键、前端目录（`node frontend/scripts/sync-backend-config.mjs` 重新生成 → 265 fields，`--check` 报 in sync）。
- **`cache_diagnostics_enabled` 的确认条件（dev 要求）**：核对 `backends/cache_diagnostics.py` —— 只对出站请求算**不可逆摘要**（SHA256），不修改请求、不记录正文或密钥；`provider_attempt_observer` 的注释也写明「Observability must never alter the provider request outcome」，出错被吞掉。满足「只做本地诊断、不改变模型请求内容、不产生额外调用或文件写入」→ 与其余 6 个一并降级。
- **动手前的 pre-check**：7 个键在生产配置里 `grep -c "^<键>:"` **全部为 0**（没有显式写过，可安全删）。
- **测试改动**（原用例的前提「这个值可由配置调」已不存在，逐条改写成钉常量，没有放宽或删除断言）：
  - `test_tool_context_ptl_retry.py::test_tool_loop_always_retries_because_the_limit_is_a_named_constant` —— 原 `config 0 = 关闭` 语义已不存在；改为断言常量值 3、`AgentConfig` 已无该字段、溢出时确为 `1 + DEFAULT_PTL_RETRY_MAX` 次调用且回收确实发生。
  - `test_tool_context_microcompact.py::test_builder_uses_the_named_constant_for_keep_recent` —— 钉常量生效：5 条结果在常量 ≥5 时全保留；再把**定义模块**的常量改成 2，断言正好回收 3 条（证明拼装层读的是这个常量，而不是别的路径）。
  - `test_memory_runtime_compact_auto_continuation.py::test_compact_auto_continuation_hard_cap_is_a_named_constant` —— 断言返回的深度等于 `_DEFAULT_MAX_COMPACT_AUTO_CONTINUE_DEPTH`，并保持「达到深度即 `returned_after_depth_cap`」的行为断言。
  - `test_tool_failure_channel_hint.py` —— 去掉配置项，改用 autouse fixture 在用例前后保存/还原模块常量，避免污染其它用例。
  - 纯删除式收尾（配置项已无读者）：`test_background_compact_recovery.py`、`test_subagent_runtime_compact.py`、`test_subagent_compact_recovery.py`、`test_cli_manual_resume.py`、`test_subagent_first_request_selection.py`。
- **一个自查出来的真实差错**：删 `cache_diagnostics_enabled` 的注释块时，误把 `dynamic_timeout_min` 顶到了新注释下面（该键在原 YAML 里本来**没有**注释），于是 `test_parameter_registry.py::test_empty_descriptions_only_come_from_the_reasoned_baseline` 报 `dynamic_timeout_min` 应保留在空说明基线里。**这个失败是本次改动引入的**：用 `git worktree add /tmp/ma-params-base f7a4cc909` 在干净基线上跑同一用例**通过**，对比后确认。修法是把 `dynamic_timeout_min` 恢复成原样的无注释状态（而不是给它补一句注释去迁就断言）。
- **门禁**：受影响 9 个模块 + 参数登记表/字段读者 + 四守卫全部通过；ruff `All checks passed!`；`DOC_SYNC_PASS`；`git diff --check` 干净；`check_clean_package.py` OK；code-size 身份差集（基线取**本工作树 HEAD**，不是 origin/main）见交付说明。
- **复现**：`/opt/homebrew/bin/python3 -m pytest -q -p no:cacheprovider agent_py_agent/tests/test_parameter_registry.py agent_py_agent/tests/test_config_field_readers.py agent_py_agent/tests/test_tool_context_ptl_retry.py agent_py_agent/tests/test_tool_context_microcompact.py agent_py_agent/tests/test_tool_failure_channel_hint.py agent_py_agent/tests/test_memory_runtime_compact_auto_continuation.py agent_py_agent/tests/test_cli_manual_resume.py`；计数用 `python3 -c "from agent_py_agent.agent.settings.parameter_registry import listed_parameters; print(len(listed_parameters()))"`。

## 测试不再打开真实浏览器，browser-lite 不再残留 Chrome 签名克隆（2026-09-28，分支 `claude/75-no-real-browser-in-tests`，基于 `80b4afed8`）

- **弹窗根因**：`test_web_board_package.py::test_stdin_eof_ends_process_and_releases_port` 直接起插件进程，却把 `MY_AGENT_PLUGIN_SETTINGS` 从环境里删掉了。插件于是按默认 `open_browser=true`，在用户的默认浏览器里打开了带令牌的本机链接。现在这一例显式传 `{"open_browser": false}`，并断言 `served["browser_opened"] is False`；它测的是 stdin EOF 后退出，与默认设置无关。
- **全仓核查**：只有三个插件自己会打开东西：web-board、harness-console（`open_in_browser`、desktop 回退）、desktop-lite（`open`/`xdg-open`/`osascript`/剪贴板）。测试里没有直接调用 `webbrowser`。
  - 核查方法：在 PATH 最前面放只记日志、令牌打码的假 `open`/`xdg-open`/`osascript`/`pbcopy`/`notify-send`/`wl-copy`/`xclip`，跑插件相关的 62 个测试文件。MCP 客户端的安全环境会保留 PATH，所以这些假程序覆盖插件子进程。
  - 修复前，web-board 那一例恰好抓到 1 次 `open`，调用方是 `python -I -m web_board`；修复后全部 921 项通过，假程序调用 0 次。
- **Chrome 签名克隆**：browser-lite 优先使用 `/Applications/Google Chrome.app`（专属 profile、无头）。macOS 版 Chrome 默认开启 `kMacAppCodeSignClone`，启动时把 `.app` 克隆到 `/var/folders/.../X/com.google.Chrome.code_sign_clone/`，只在正常关闭时由清理子进程删除，被强杀就会残留（依据：Chromium `chrome/browser/mac/code_sign_clone_manager.mm`，`BASE_FEATURE(kMacAppCodeSignClone, FEATURE_ENABLED_BY_DEFAULT)`，清理在析构时拉起 `--type=code-sign-clone-cleanup`）。
  - 实测：只跑一次 `test_browser_lite_package.py`，克隆从 737 增加到 746（+9），期间没有别的 headless Chrome 或 pytest 在运行。
  - 修复：launcher 启动参数加 `--disable-features=MacAppCodeSignClone`。修复后同一文件再跑一次新增 0 个，62 个插件测试文件整体再跑一次也是 0 个。
  - 新增 `test_browser_lite_launcher.py` 合同单测，锁住这个启动参数。
  - 只数、没有删除现有的克隆；用户日常 Chrome（pid 4415）全程没碰。
- 只跑相关定向测试，basetemp 跑完即删，测试未生成 pycache。

## audit-log --cleanup 按 owner canonical 路径解析（2026-09-28，分支 `my-agent/self-dev-4`，基于 `80b4afed8`）

- **来源**：dsh-9b 的产品持久数据盘点（`docs/design/STORAGE_RETENTION.md`）零风险缺口之一——`audit-log --cleanup` 按相对路径解析，指到了错误的文件。
- **根因**：`audit/paths.py:resolve_audit_paths` 把配置里的相对值（默认 `data/audit`）直接 `Path(...)` 展开，于是相对**进程 cwd**。Agent 启动时 `core.py:450 apply_runtime_paths_to_config` 会把 canonical 的绝对 `audit_log_path`（`owner_logs_dir/audit`）注入 config，所以写入端没问题；但 CLI 的 `audit-log` 只 `load_config`、没有走 runtime paths，于是拿到空串 → 落到 `data/audit`，随启动目录漂移。在不同 cwd 下执行会清到不同文件，甚至可能清掉工作区里恰好同名的 `data/audit/audit.jsonl`。
- **做法**：`resolve_audit_paths(config, *, root=None)` 新增可选基准；给了基准且配置值是相对路径时，按基准展开，绝对路径原样尊重；不传时保持旧的相对语义（不影响既有测试与旧调用方）。`AuditQuery.__init__` 同样接受 `root`。CLI 用 `workspace_resolution.owner_home_workspace_root(config)` 取 canonical owner home 传入，替换原先取到就没用上的 `resolve_workspace_root`。
- **新增测试**：`test_audit_cleanup_path.py`（9 项）——相对路径按基准解析、绝对路径不被改写、无基准保持旧语义、**两个不同 cwd 下清的都是 canonical 那一个且工作区同名文件绝不被碰**、保留期 ≤ 0 不删任何记录、清理不越出给定 owner home、查询视图的 root 与 file 两个字段都跟着基准、带后缀的相对路径、生产同形路径落在 owner home 内。
- **变异验证**：`_mutate_audit_path.py`（工作目录 `tasks/2026-09-28/storage-retention-fixes/`）两处，先 `git diff` 存补丁、`git checkout -- .` + `git apply` 还原：
  - 完全去掉基准解析（回到随 cwd 漂移）→ 6 条红；
  - 假装支持基准但实际用 `Path.cwd()` 兜底 → 同样 6 条红；
  - 还原后 9 项全绿。
- **定向回归**：`test_audit_cleanup_path.py` + `test_audit_class.py` + `test_audit_redaction.py` 共 26 项通过。
- **复现**：
  ```
  cd <worktree>
  python3 -m pytest agent_py_agent/tests/test_audit_cleanup_path.py agent_py_agent/tests/test_audit_class.py agent_py_agent/tests/test_audit_redaction.py -q
  python3 <工作目录>/_mutate_audit_path.py ignore-root      # 期望 6 条红
  ```
- **未覆盖**：没在真实生产 home 上跑 `audit-log --cleanup`（会真删生产审计记录），只在小规模临时目录里验证。

## 会话互通：list_owner_sessions 模型工具（2026-09-28，分支 `claude/75-list-owner-sessions`，基于 `80b4afed8`）

- **范围**：新文件 `orchestration/tools/list_owner_sessions.py`（工具 + 可见性判定）；`core._register_orchestration_tools`
  加 3 行注册；`SESSION_TASK_WAKE_ALLOWED_TOOLS` 加一项。`conversation/session_messaging.py` 与另外三个会话工具文件未改。
- **测试**：`test_list_owner_sessions_tool.py` 共 32 例，正反成对：
  - 可见性：管理员在任一管理员开关打开时可见，两个都关不可见；user / group / 空身份即使全开也不可见；
  - 真实 `SimpleAgent` 注册：缺配置文件与坏配置文件都按 dataclass 默认值注册；两个开关都关时不注册；
    普通用户打开用户消息开关后拿到 `send_session_message`（对照）但拿不到本工具；
  - 真实 `ConversationStore`：按最近活动倒序；每行字段集合固定，标题和摘要不出现在输出里；
    自己的会话 `is_current=true` 且 `allowed_kinds=[]`，其它本地会话为 `[message, task]`，飞书会话列出但为空；
    `allowed_kinds` 跟随两个开关；子代理线程不列；`limit` 截断与非法值（0、101、-1、字符串、布尔、小数）；
    损坏记录只计数不回显；
  - 跨 owner：记录里写了别的 owner 家目录的会话不列出也不计数，同一记录换成自己的家目录就列出；
    两个 owner 各自只看到自己的存储；身份三元组缺任一项返回 `SESSION_IDENTITY_UNAVAILABLE`、不列任何会话。
- **变异验证**：25 个变异体全部被抓住，覆盖去掉管理员判断、各开关、`or CapabilityConfig()`、无条件注册、
  唤醒档条目、子代理线程过滤、owner 家目录核对、当前会话标记、泄露标题、忽略渠道、排序、截断、损坏计数、
  limit 类型与上限、身份 fail closed、当前会话编号来源。
- **定向回归与守卫**：会话互通全部测试、工具注册与协议、后台工具档、配置默认值、护栏等 66 个文件：
  1207 passed、4 xfailed。ruff、doc sync、strict code-size（发现项与基点逐条一致，无新增）、
  `git diff --check`、clean-package 全部通过。
  会话互通在生产上仍然关闭，本工具只在测试里验证。

## 测试全局防线：任何测试都不能打开用户的浏览器或桌面程序（2026-09-28，分支 `claude/75-test-open-guard`，基于 `80b4afed8`）

- **修复：防线自己的读写不再被测试的 IO 哨兵和打桩碰到（2026-09-28，e9be5de14 之后一个提交）**
  - 问题：全量分片里 5 条误报。`test_gateway_model_observation` 的关闭观察用例（off、disabled）在类级别把
    `Path.read_text/write_text` 换成“不得读写盘”的哨兵；`test_shell_foreground_cleanup` 的 zombie 用例把
    `Path.read_text` 换成假 `/proc` 内容。防线用 `Path.read_text` 读记录文件，检查又发生在测试的打桩窗口之内，
    于是撞上哨兵（测试 FAILED、teardown 再 ERROR），或把假内容当成一条违规记录。
  - 修法两步：
    1. 防线读写只用导入时抓好的 `os.open/read/write/fstat/lseek/close`，记录路径安装时固定成字符串；游标改为记录文件的
       字节长度，文件没变长就不读；只消费完整行，写了一半的行留到下次。
    2. 游标在 `pytest_runtest_setup` 最早记下；检查挪到 `pytest_runtest_teardown` 之后，也就是测试的 fixture 与
       monkeypatch 全部撤销之后，违规测试报为 teardown ERROR。放行标记改用防线自己的 MonkeyPatch，不再借测试的
       monkeypatch。
  - 顺带修掉一处失效：会话级 fixture 在最后一条测试收尾时就把全局实例清空，`pytest_sessionfinish` 读到 None，
    “会话结束报未归属记录”从未生效；现在保留一份不清空的引用。
  - 验证：
    - 在 e9be5de14 上复现：observation 两条 FAILED 加 teardown ERROR，zombie 一条 FAILED；修复后三条通过。
    - 自检新增 4 例：把 `Path` 的 7 个 IO 方法、`builtins.open` 和 `os` 的 7 个函数全换成报错哨兵，防线照样记录与读出；
      写了一半的行等换行；会话结束检查读得到已结束的防线（有记录让会话失败，无记录不动）。
    - 负例（临时探针文件，已删）：直接调 `open`；先把 `Path.read_text` 换成假内容、`Path.open` 换成哨兵，再调
      `webbrowser.open` 和 `osascript`；两者都在 teardown 被拦下并列出每条调用。只打桩 IO、不调桌面程序的测试通过。
      web_board 原样那条（本分支不含 `8bf9fdcff`）仍被拦下，报 `open http://127.0.0.1:<port>/?token=<redacted>`。
    - 全仓搜出打桩 `Path.read_text/open/stat/exists/is_dir`、`builtins.open`、`os.open/read/stat/fstat` 等接口或断言“零 IO”
      的 21 个测试文件，加插件相关 91 个文件（其中 4 个与前者重叠）、3a 点名的 `test_shell_background`、`test_adapter_manager`、
      `test_gateway_agent_control_service` 和三个护栏，共 114 个文件：2325 项通过，唯一的 ERROR 是上面那条预期负例，误报 0 次。

- **做法**：`agent_py_agent/tests/conftest.py` 在会话开始时把 `_desktop_open_guard.py` 生成的 shim 目录放到 PATH 最前，并把 `webbrowser.open/open_new/open_new_tab` 换成只记录的替身。
  - shim 只追加一行记录（程序名、打码后的参数、调用方进程、当前测试名），不执行任何动作。
  - 记录文件路径在安装时写死进 shim 脚本。原因是插件 MCP 子进程经 `build_safe_env` 只继承 PATH、HOME 等白名单变量，靠环境变量传日志路径会漏记。
  - 测试体跑完立即检查本测试期间的记录，有就让这条测试失败，报错写明程序、打码参数、调用方命令和测试名。夹具收尾或插件退出时才出现的调用在 teardown 报出。测试之外的后台调用在会话结束时列出，并把会话退出码设为失败。
- **拦截范围与理由**：
  - 会打开浏览器、文件或应用的：`open`、`xdg-open`、`gio`、`gnome-open`、`kde-open(5)`、`wslview`、`sensible-browser`、`x-www-browser`、`www-browser`。Python `webbrowser` 在 Linux/WSL 上就按这些名字查找。
  - `osascript`：macOS 的 `webbrowser` 经它打开网页，它还能弹窗、控制应用。
  - 通知：`notify-send`、`terminal-notifier`。
  - 剪贴板读写：`pbcopy`、`pbpaste`、`wl-copy`、`wl-paste`、`xclip`、`xsel`。写会改掉用户剪贴板，读会把用户私有内容带进测试。
  - 现有测试的情况：desktop-lite 用例经设置注入假程序；TUI 剪贴板用例 stub 了 `subprocess.run` 或 `_run_clipboard_tool`；没有测试依赖“找不到 open”一类分支。所以一起拦截不会误伤。
- **放行**：确需真实调用的测试必须加 `@pytest.mark.real_desktop_programs`，这时该测试的 PATH 去掉 shim、`webbrowser` 恢复原实现。目前没有测试需要它，只有防线自检用它核对放行后的环境，不调用任何程序。
- **边界**：测试自己把 PATH 改成不含 shim 的值，或者给子进程一个不含 PATH 的全新环境时，不在防线覆盖内；Python 进程内的直接调用只覆盖 `webbrowser`。
- **验证**：
  - 自检 `test_desktop_open_guard.py` 5 项通过，包括只给 PATH 的剥离环境下照样记录。
  - 在本分支基础 `80b4afed8` 上，web_board 那条 stdin EOF 用例仍是原样（未含 `8bf9fdcff` 的修复），防线让它失败，报出 `open http://127.0.0.1:<port>/?token=<redacted>`，调用方是 `python -I -m web_board`。
  - 临时把 `start()` 改成 `open_browser=true`，经 MCP 子进程起的插件调用同样被拦下并归到对应测试。
  - 在工作区临时带上 `8bf9fdcff` 的修复（不提交），把插件相关 62 个测试文件、TUI 剪贴板和输入、guardrails、`constant_names_unique`、`code_size_script` 一起跑，共 68 个文件、1071 项全部通过，防线拦截 0 次（0 误报），耗时约 15 分钟；basetemp 跑完即删，测试未生成 pycache。
- **合并顺序**：本分支必须和 `8bf9fdcff` 一起或在它之后集成，否则 web_board 那一条会按设计失败。

## 决策统计口径：讲清“缺报”，没发出去的单列（2026-09-28，分支 `claude/be-jev-transport-timing`，在 `b792340e0` 之上）

- **新增测试** `test_decision_stats_display.py`：
  - TUI 三种数据来源分开：
    - 供应商有回报：只显示已报；
    - 只有估算：显示“估算 N token（未完成）”，也不算缺报；
    - 两者都没有：当时保留“缺报”，整行只出现一次；后续提交按 ae 建议改为挂在所属次数上的括号说明，见“非决策调用‘缺报’同口径统一”一条。
  - 没发出去的失败单列“未发出”，不算失败。
  - 总行“缺报”扣掉决策那部分；没有决策调用时，总行口径与原来相同。后续提交把非决策调用统一成 LLM 段，见“非决策调用‘缺报’同口径统一”一条。
  - 拆分按跨事件累加后的原始次数推导。逐条推导会把迟到的尝试误判为“没发出去”；旧账整体仍算失败。
  - 结果日志按点位汇总，12 种行：
    - 带 transport 的行，按有没有 HTTP 尝试判定；
    - 旧行按原因码判定（budget_exhausted、cooldown、admission_busy、backoff 等）；
    - 成功、跳过、作废照原样计数；最近行不丢。
  - 审计用量行：用真实账本加一条旧账事件，核对 `jev_failures`/`not_sent_calls`/`failures_send_unknown` 与估算；原始计数不变。
- **已有测试按新口径更新**：
  - `test_decision_usage_metrics.py`：
    - 标签从“≈N / ≈?”改为已报、估算、缺报、未发出；
    - 原“超时和失败都算失败并放宽约数”的用例，改成“发出去后超时算失败并显示估算，没发出去的单列”，并断言总行不再出现缺报。
  - `test_decision_outcome_log.py`：冷却从 points 移到 not_sent。
- **定向回归**：105 个测试文件，2628 passed：
  - 引用 model_metrics、TUI 统计、决策审计、结果日志、audit_records 的测试；
  - 全部 `test_decision_*`；
  - TUI runtime、块渲染、stream_writer 相关的测试；
  - guardrails、constant_names_unique。
  补强断言后，另把 3 个目标文件单独跑了一遍，通过。basetemp 已删。
- **变异验证**：scratchpad 里的 `mut_disp.py` 做了 15 个变异，按 rc==1 判定，全部被抓到。变异点：
  - 旧账不记 unknown；拆分时忽略 unknown，或忽略 unfinished；
  - 汇总时不累加未完成次数，或不累加决策在缺报里的份额；
  - 成功但没报输入的不算缺报；外推改按全部调用算；隐藏“未发出”；总行重复计决策；
  - 审计不累加旧账失败；审计行不做拆分；
  - 结果日志忽略链路事实、忽略冷却状态、漏掉 configuration_required、漏掉 budget_exhausted。

## Jev 决策调用的链路分段计时（B 第 0 步）（2026-09-28，分支 `claude/be-jev-transport-timing`，基于 `80b4afed8`）

- **新增测试** `test_decision_transport_timing.py`（22 项，含 9b 复审后补的 2 项）不访问任何真实服务，替身有三个：
  - 本机假 CONNECT 代理：可设回复延迟；
  - 假 TLS：握手处按设定时长等待后原样返回 socket，不加密；
  - 假服务端：首字节和正文可以分别延迟。
- **传输层**：
  - 代理 + HTTPS 全程：
    - 六个阶段按顺序出现，各段注入的延迟都体现在对应阶段的毫秒数里；
    - 事件里 started 一次、response_opened 一次、progress 六次；
    - CONNECT 一次，POST 一次。
  - 直连 HTTP 只有 connect、request_send、first_byte、body_read 四段；没开计时的调用，事件仍是原来的两条，不带 transport。
  - proxy_connect、tls_handshake、first_byte、body_read 四个阶段分别超时：
    - 最后一份快照停在该阶段，之前的阶段都已计时；
    - 零重试，超时后不补发。
- **账本**：
  - progress 只换计时：状态、事件、活动时间、尝试数都不变，未知尝试不新建；
  - 状态事件不带快照时，保留上一份计时；
  - 超时记 `timeout_transport_phase`；
  - 估算入账分四种情形：超时或失败、且已发出请求的计入，没发出的和成功的不计，供应商桶不受影响；
  - 超时后迟到的尝试也会补记估算。
- **全链路**（原配置 → `decide()` → 本机代理 / 假 TLS / 服务端）：
  - 成功时，结果日志行带齐六段毫秒；
  - proxy_connect、first_byte 两种超时下：
    - 行里的 `timeout_phase` 正确；
    - 估算输入进了 model_usage 快照（线程汇总和 decision 分区都有），`audit_records` 用量行也带出；
    - 供应商输入为 0，发送次数不变；
  - 调用前就结束（没建调用记录）的结果行不带 transport；旧行的最近行形状不变。
- **复审跟进**（9b，复审通过后的建议项）：
  - `test_stdlib_private_hooks_the_timing_relies_on_still_exist`：钉住计时依赖的标准库私有接口，升级 Python 缺了会直接变红；
  - `test_a_timing_failure_right_after_connect_closes_the_new_socket`：推进阶段时抛 KeyboardInterrupt，还没交给连接的新 socket 被关掉。
- **形状断言更新**：`test_model_call_ledger.py` 3 处、`test_conversation_store.py` 1 处，estimated 的期望值补上两个值为 0 的新键。
- **定向回归**：共 98 个测试文件：引用改动模块的 92 个，加 5 个扫描守卫（architecture_guardrails、constant_names_unique、
  config_field_readers、recovery_code_policy、orchestration_tool_constants）和本次新文件。
  - 结果：2572 passed、1 xfailed、1 xpassed、1 failed。
  - 失败的是 `test_gateway_model_adoption.py::test_noncooperative_probe_timeout_retains_without_late_adoption`：
    - 它只给决策 1 秒预算，当时机器 5–15 分钟平均负载 33–34；
    - 它的 HTTP 发送和决策后端都是替身，走不到本次的传输计时；
    - 单独连跑 3 次，以及最终复跑，都通过。
  - 第一轮暴露两个问题，已修：
    - 测试替身按 `_gateway_urlopen(req, request)` 签名写，接不住新增的计时参数：计时器改挂在本次尝试的 Request 上，签名不变；
    - `*args/**kwargs` 被架构守卫拦下：响应类改为每次尝试生成的计时子类，建连包裹改用标准库调用时的三个位置参数。
  - 另在 3.10、3.11 上跑了传输相关的 4 个文件，均通过。
  - basetemp 跑完即删，未生成 pycache。
- **变异验证**：scratchpad 里的 `mut_jev.py` 逐个就地改 21 处，跑新测试文件后按哈希核对还原
  （`PYTHONDONTWRITEBYTECODE=1`，独立 pycache 前缀），21 个全部被抓到。变异点如下：
  - 传输层：
    - 去掉阶段推进门（`_tunnel` 读代理回复时会误记首字节）；
    - 忽略隧道；首字节不推进；把发送阶段挪到取响应之后；毫秒写成秒；
    - 读完正文不收尾；忽略开启开关；HTTPS 不记 TLS。
  - 账本：忽略 progress；状态事件抹掉计时；超时不记阶段；没发出也计估算；成功也计估算。
  - 落盘与审计：
    - 线程汇总缺新键；不收集链路事实；worker 不开计时；
    - 结果不挂 transport；日志行丢 transport；审计最近行丢 transport；审计不加估算；call_runtime 丢快照。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_decision_transport_timing.py -q`。

## 会话互通真实链路集成测试（2026-09-28，分支 `claude/ae-session-real-chain-test`，基于 `80b4afed8`）

- **为什么做**：会话互通连续两次交付都是“单测全绿、真实链路坏掉”：
  - 7b83c8730 让派活回合全部开不出来（SKILL_TASK_BINDING_INVALID）；
  - 204f4ddf9 把所有派活和消息唤醒都当作“已消费”跳过。
  - 原因是既有单测要么替换了线程、任务关联、唤醒，要么手工拼后台回合参数，绕过了真实的 唤醒 → 认领 → run_claimed → 回合装配。
  - 集成方要求：以后会话互通的每一次交付，都先用这组测试把关。
- **做法**：`agent_py_agent/tests/test_session_task_real_chain.py`，单文件，全部走产品入口，进程内运行，不走网络。
  - 前台回合：真实 Gateway ask（`request_execution._run_gateway_ask`），落 processing 文件并收尾，与真实 worker 相同。
  - 后台回合：Gateway 自己构造的调度器（`gateway_loops._build_background_scheduler`）逐轮 tick。
  - 模型：只替换供应商传输（`backends.http.post_json`），请求组装、响应解析、工具协议走真实 openai_compatible 后端。
  - 假线路按结构化标记出招；挂起时登记与真实传输相同的停止回调；同一回合调用超过 40 次按空转打断，不会把测试挂死。
  - home 放在 tmp_path 下。
  - 判定只看结构化事实：会话任务记录、guidance 一次性回执、唤醒队列、工具操作账本、模型调用计数。
- **覆盖场景**：
  1. 派活唤醒开出回合并完成（正向对照）：任务 done、绑定到派活回合、带回报摘要，派活方收到 done 回报。
  2. 给空闲会话发消息，唤醒能开出回合并看到消息（正向对照）。
  3. 空闲目标的消息被确认消费、下一回合不再重复收到：**strict xfail**，是新发现的缺陷。唤醒回合只在后台上下文里看到消息，回执仍是 pending，下一回合会再收到一遍。
  4. 忙碌目标在自己回合里消费了消息之后，唤醒不再多跑空回合：**strict xfail**（观察项②，第 8 片重做后转正）。
     - 同一条用例里用前置条件把住：B 的请求不失败，消息在本回合被消费。
  5. 绑定后在模型调用期间取消，分“第一次调用中”和“调过工具之后”两个窗口：**strict xfail**（场景 5，取消修复通过后转正）。
     - 断言：stop_confirmed=true，目标不交付放行后的答复，任务保持 cancelled 且没有摘要，唤醒结案，工具操作账本里有这次取消。
  6. 空转回归：A 的邮箱挂着别的任务的回报时，A 的派活回合只调 1 次模型。
  7. 空转兜底：人为让“有待处理输入”与“能否认领”判据不一致，调用次数不超过 `PENDING_TURN_INPUT_INVALIDATION_LIMIT + 1`，唤醒结案。
  8. 默认配置（不放 capability 配置文件）：A→B→C→A→B 放行，第 5 次派活返回 `SESSION_TASK_CHAIN_LIMIT`（depth=4，limit=4）。
- **xfail 约定**：
  - 只接受 `AssertionError`；链路本身断掉（没开出回合、后台 tick 抛错、前置条件不成立）抛 `RealChainBroken`，不会被 xfail 吞掉。
  - 缺陷修好后 strict xfail 会以 XPASS 失败，提醒把标记去掉转正。
- **验证结果**：
  - main `80b4afed8`：5 通过、4 xfail，连跑 3 次结果一致，每次 17–33 秒。用 `--runxfail` 核对过，每条 xfail 都失败在预期断言上。
  - `7b83c8730`：派活正向对照失败，报 `RealChainBroken：后台第 1 轮 tick 在真实链路上抛错（唤醒没能开出回合）：SkillSnapshotError: SKILL_TASK_BINDING_INVALID`；两条取消、空转回归、空转兜底、链深 4 也以 RealChainBroken 失败，没有被 xfail 掩盖；消息正向对照仍通过。
  - 与真实复验结论一致。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_task_real_chain.py -q -rxX`

## capability 兜底值清理：只认 dataclass 默认值（2026-09-28，分支 `claude/9a-capcfg-fallback-cleanup`，基于 `54880f8e9`）

- **范围**：6 处调用点改为 `capability_config_for_agent(...) or CapabilityConfig()` 后直接读字段，删掉各自写的兜底值：
  - 选包判定、子代理包入口开关；
  - 流式活动投影（开关、间隔，以及异常分支里的 15 秒）；
  - 活动提醒（开关，以及首 token、流静默、长工具三种阈值）；
  - 失败自动拆分（开关、深度，以及非法深度时的兜底）；
  - 看板巡检阈值（另删一条从未命中的分支，并去掉 `DueCheckSettings` 的默认值 4）。
  - 会话互通三个工具文件与 `session_messaging.py` 这次没碰。行为不变。
- **测试**：`test_capability_config_single_default_source.py` 共 23 例，每处都覆盖三种情况：
  - 缺文件取到 dataclass 默认值；
  - 坏文件（统一入口返回 None）也取到默认值；
  - 文件里的非默认值照常生效。
  - 流式投影用可控时钟证明节流间隔正好是默认的 15 秒：早半秒不发，到点就发。
- **变异验证**：12 个变异体全部被抓住，包括每处去掉 `or CapabilityConfig()`、写死数值、忽略开关、阈值映射错位。
- **定向回归**：引用这些函数的 30 个测试文件加护栏测试：470 passed、2 skipped（`--basetemp` 28 字符）。
  - `test_subagent_debug_trace.py::test_subagent_debug_trace_level_five_writes_detail_refs` 在 116 字符的长 basetemp 下，基点和本分支都失败，换短路径后两边都通过，属于已知的路径长度问题，与本改动无关。

## SubAgentBaseService 压到 200 行以下（2026-09-28，分支 `claude/75-subagent-base-slim`，基于 `54880f8e9`，行为不变）

- **改动**：`_write_authority_records` 及其链身份回存移到 `subagents/services/run_authority.py::write_create_run_authority`；`_finalize_task` 改为同文件的模块函数 `_finalize_created_task`。`create_run` 的调用顺序、异常与日志内容不变，只是告警日志的 logger 名随新模块变化。类长 239 → 189。
- **code-size**：本片涉及的三个文件逐文件比较发现项，只消失 `SubAgentBaseService` 的 high-risk，没有新增。
- **测试**：`test_decision_subagent.py` 的“线程创建后、权威写入前崩溃再重试”用例改为 patch `services.base.write_create_run_authority`。子代理相关 117 个测试文件（含 `test_runtime_db_main_chain`、`test_decision_subagent`、`architecture_guardrails`、`constant_names_unique`、`code_size_script`）共收集 1345 项，全部通过。按磁盘约束没有跑全仓；basetemp 已删除，测试未生成 pycache。

## curator 关系对按词面相关度挑选（2026-09-28，分支 `my-agent/self-dev-4`，基于 `54880f8e9`）

- **来源与问题**：DESIGN_LEDGER「召回前补充查询」条缺口 4——`decision_curator_relation` 取消息×正式条目笛卡尔积的**前 32 对**，不看相关度，相关的那一对若排在第 33 位之后就永远比不到。
- **做法**：新增 `_select_pairs`，用标准库给每一对算 BM25 词面相似度（正文档词频由该对两侧正文的并集构成，`_TOKEN_PATTERN` 为小写拉丁串 + 单个汉字），按 `(-score, 原枚举序)` 稳定排序取前 32；对数不超过上限时原样全取。上限仍是 32，不引入嵌入调用、不增加网络请求和费用。挑选是纯函数，同批两次构造必须得到同一 `revision`（`annotate_curator_relations` 第 49 行的陈旧复核依赖这一点）。`state.coverage` 新增 `total_pair_count`（全批笛卡尔积对数）、`selection`（规则标识 `bm25_then_source_order`）与 `selection_limit`，明确声明"只展示了对中的一部分"。
- **新增测试**：`test_decision_curator_relation_selection.py`（9 项）
  - `test_relevant_pair_beyond_sequential_cutoff_is_selected`：2 消息 × 32 条目 = 64 对，相关的那一对是顺序枚举下的第 33 对（`message-1 × entry-31`），旧逻辑取不到，新逻辑必须取到；
  - `test_selection_is_deterministic_across_calls`：同一 batch 两次构造得到同一挑对结果与同一 `revision`；
  - `test_empty_message_or_formal_yields_no_questions`：消息为空、条目为空、两者都为空三种边界都返回空题集与零展示数，不抛异常；
  - `test_coverage_declares_total_presented_and_rule` / `test_all_pairs_presented_when_under_limit`：覆盖声明能区分"全部比过"与"只比了一部分"；
  - `test_ties_keep_original_enumeration_order`：全部同分时退回原枚举顺序（消息外层、条目内层）；
  - `test_selection_has_no_embedding_or_network_calls`：打分是本地算术，没有嵌入入口。
- **变异验证**：用 `_mutate_relation_selection.py`（工作目录 `tasks/2026-09-28/curator-relation-relevance/`）做两处变异，都先 `git diff` 存现场补丁、还原用 `git checkout -- <文件>`+`git apply`：
  - 把排序键 `(-scores[...], index)` 翻成 `(scores[...], index)`（分数方向反了）→ 正好 1 条红：`test_relevant_pair_beyond_sequential_cutoff_is_selected`；
  - 把稳定排序换成 `sorted(..., key=-score, reverse=True)`（去掉同分原序兜底）→ 同样 1 条红；
  - 还原后 53 项全绿。
- **定向回归**：`test_decision_curator_relation_selection.py` + `test_decision_curator_relation.py` 共 53 项通过（9 + 44）。
- **复现**：
  ```
  cd <worktree>
  python3 -m pytest agent_py_agent/tests/test_decision_curator_relation_selection.py agent_py_agent/tests/test_decision_curator_relation.py -q
  python3 <工作目录>/_mutate_relation_selection.py reverse      # 期望 1 条红
  ```
- **未覆盖**：本批没做真实 Jev 样本对比（挑选规则只影响哪些对进入请求，语义质量仍由原 Curator 与原验证把关）；真机效果按 dev 安排等 Jev 复测。
## 读取不存在的文件时给出相近文件名建议（2026-09-28，分支 `my-agent/self-dev`，基于 `80b4afed8`）

- **来源**：集成者派的任务——路径不存在时，如果用户只是把文件名写错（少字/多字/串位），
  应该给出同目录里最像的名字，而不是直接报"文件不存在"。
- **做法**：在既有共享模块 `filesystem_path_recovery.py` 里新增 `suggest_near_name_paths`，
  由 `suggest_missing_path_candidates` 调用并把结果排在候选最前，因此 read_file / list_files /
  search_text / find_files / edit_file 五个入口自动共用同一份实现，无需逐个改。
  只在"目标不存在 + 父目录存在 + 同目录内"成立时触发；越权目录、超大目录直接不扫。
- **边界怎么定的（实测，按更准确的单次查找口径）**：距离上限 **2**（上限 1 时实测 1413 个真实
  文件名全无候选，等于给不出建议）；最多 **2** 条；目录条目上限 **512**（单次查找 10000 条约 37ms、
  512 条约 2ms，真实目录最大 133 条）。早先写的"整表两两比较"是错口径，已在给集成者的回复里更正。
  集成者建议的"长度差预筛"实测无收益，未采用。

### 安全修复（2026-09-28 复审 N1 / N3 / N5）

复审用独立探针报了三个问题，前两个已实测复现：

- **N1 符号链接越墙泄露**：owner 墙模式下，工作区里一个指向别的 owner 家文件的链接会让
  `candidate_paths` 出现对方文件的**完整绝对路径**；指向不存在文件的链接则不给——于是能用来
  探测别人家里有什么文件（`stat` 由 Gateway 进程执行，绕过工具沙箱的读拒绝）。
  探针复现：目标存在 → 泄露 `/…/bob/bobs_secret_notes.md`；目标不存在 → `[]`。
  修法：`AccessGate` 把工具自己的 `check_path_access` 带进候选生成，**每个候选先 resolve 再过审**；
  报告条目**自身**路径而不是链接目标；类型判断改用 `follow_symlinks=False`。
  同一约束补在既有的跨目录扫描上（`_score_candidate`），因为 **main 上本来就有同一个口子**（N1b）。
  修复后重跑探针：两种情形都是 `[]`，不可区分。
- **N3 上限在读完整个目录之后才判断**：原先先 `list(iterator)` 再比 512，违反同文件
  "Never materialize all children"。探针实测 5000 条目录**遍历了 5000 条**。
  改为 `itertools.islice(iterator, cap + 1)`；修复后同样场景**只读 513 条**（= 上限+1，用于判定溢出）。
- **N5 最坏耗时（已用便宜办法解决）**：曾试着改成带状 DP 提速，**但实测它算错**——与朴素实现
  随机对拍 **20000 例里 1306 例不一致**，因此**回退**到原先的两行滚动数组（回退后 8000 例对拍
  0 不一致）；这条教训写进了代码注释，避免下一个人再尝试同样的重写。最终按集成者建议改用
  **不改算法的便宜解法**：请求的文件名超过 `_NEAR_NAME_MAX_NAME_LENGTH`（**64**）就完全不做
  近名匹配。目录条目最多 513 条、名字长度也有上限，最坏耗时因此被卡住。补了两条测试：
  超长名字不触发匹配（放一个距离 1 的兄弟文件，断言它不被建议出来，证明是"跳过"而非"没找到"），
  以及长度正好等于上限时仍正常匹配（防 off-by-one）。变异验证：去掉长度上限 → 恰好那一条变红。

### 权限闸门修复（2026-09-28 第二轮复审，B1/B2/B3/B4/B6）

**这是本轮最重要的修复**：上一版加的 `AccessGate` **什么都没拦**。

- **根因**：`PathAccessDecision` 是普通 frozen dataclass，**没有 `__bool__`**，所以
  `bool(self.decide(path))` **恒为真**。实测确认：`PathAccessDecision(allowed=False)` 的
  `bool()` 返回 `True`。因此闸门形同虚设——`read_file(".evn")` 仍会建议 `.env`，
  危险根里写错一个字母也照样被建议。
  **N1/N1b 当时之所以"关上"，靠的是 `follow_symlinks=False` 把链接整个排除，不是这道闸门。**
- **修法**：`AccessGate` 改读 `.allowed`（兼容直接返回布尔），`MissingPathRequest.decide_access`
  的类型注解同步改为 `Callable[[Path], Any]`。
- **顺带修回一个功能回退（B4）**：上一轮把**所有**链接都排除了，连指向工作区**内部**的合法链接
  也不再被建议。现在类型判断跟随链接、裁决看 **resolve 之后的路径**：`check_path_access` 只看
  解析后的路径、不看目标是否存在，所以指到墙外的不论目标在不在都会被丢弃，不会重新变成探测口。
- **测试怎么钉住的**（上一轮这些变异全部存活）：
  - 闸门整个关掉（**MG1**）→ 被杀；
  - `_score_candidate` 改成报告 resolve 后的路径（**MG4**）→ **当时**报"被杀"，但**这个说法是错的**：
    我全部用例都跑在 owner 墙下，而 MG4 在**普通模式**下没有任何裁决差异、只有报告出来的路径会变，
    所以它在普通模式下**存活**（dsh-9b 复审指出）。已补普通模式用例
    `test_sibling_scan_reports_entry_path_not_resolved_target_in_normal_mode`，现在才真的被杀。
  - 去掉 2 条上限（**MN1**）→ 被杀（放 3 个**真的**在距离 2 以内的名字，先断言前提成立）；
  - 近名排到候选末尾（**MN6**）→ 被杀（让同目录与跨目录候选**都真的进候选**再比顺序）。
- **我自己两处测试写错，都如实改了而不是放宽断言**：
  1. 上一轮的 N1b 回归用 `project_note.md` 对 `project_notes.md`，两者词干不同、`_name_score`
     为 **0**，所以它**根本没进候选逻辑，是空跑通过**（复审 B3 指出的）。改成 `probe.csv` 对
     `probe.xlsx`（实测打分 90），并先断言"词干命中确实给正分"。
  2. 用 `.env` 当"被策略拒绝"的样本站不住：**凭据拒绝只作用于未受 owner 墙约束的普通模式**，
     owner 墙下 `.env` 是放行的（实测 `allowed=True`）。改成用**危险根**造真实拒绝。
- **`_read_tool` 辅助函数修掉一个不存在的字段**：`workspace_roots` 是 `ReadFileTool` 的参数，
  不是 `FileSystemAccessOptions` 的字段（复审 B3 指出）。

- **新增测试**：`test_filesystem_near_name.py` 共 14 例——
  - 原有五类：命中 / 不命中 / 超上限不扫 / 越权不出现 / 条数上限；
  - **N1**：同名细链接不得进入候选，且"目标存在"与"目标不存在"两种情形**结果必须一样**；
  - **N1b**：跨目录候选扫描同样不得泄露链接目标；候选只报同目录内的自身路径；
  - **N3**：用计数版 `scandir` 断言最多只读 `cap + 1` 条（且远小于目录总数）；
  - 大小写不敏感、多候选排序稳定、同目录近名排在跨目录候选之前；
  - 编辑距离与**朴素 DP 对拍**（这条正是用来抓"N5 带状 DP 算错"那类回归的）。
- **变异验证**：注入 5 个变异并确认**全部被抓住**，随后恢复原文件：
  - 距离上限 2 → 1；建议条数 2 → 0；改成大小写敏感；近名排到候选末尾；越权过滤失效。
  （上一轮同样这 4 个变异**全部存活**，说明当时的用例没有区分度；这次补的用例把它们都杀掉了。）
## 检索侧向量按正文哈希缓存（2026-09-28，任务 9）

来源：dev 通过开发交流板派的任务，对应 DESIGN_LEDGER「召回前补充查询」缺口 3。

做法：`_search_scoped` 检索前的混合排序不再无条件现嵌全部正文。
- `HybridRetriever.rank` 新增 `cached_vectors`，`_vector_order` 只对缺失项调 `embed`；
  本轮现嵌结果存 `last_fresh_doc_vectors`，由 `_remember_cached_vectors` 回写。
- `retrieval/vector_store` 加 `text_cache_key(model, dim, text)`（前缀 `textcache:v1:`）与
  `get_text_vectors` / `put_text_vectors` / `remove_text_vectors`，与按 entry_id 的向量共用
  `memory_vectors.json` 但键空间独立。
- `apply_batch` 提交后调 `_forget_cached_vectors(removed_contents)`，删除/替换不留孤儿向量。
- OpenAI 兼容与 MiniMax 两个生产 embedder 暴露只读 `model`。

新测试 `agent_py_agent/tests/test_memory_vector_cache.py`（7 条）：第二次检索只为新增事实嵌入、
部分缓存仍补嵌缺失项、有/无缓存结果逐条一致、缓存键绑定模型与维度、损坏文件退全量、
非 list 值按未命中、旧 entry_id 文件不被新键命中、删除后缓存项被清。

复现：
```
python3 -m pytest agent_py_agent/tests/test_memory_vector_cache.py -q
python3 -m pytest agent_py_agent/tests/test_memory_vector_cache.py agent_py_agent/tests/test_memory_semantic_recall.py \
  agent_py_agent/tests/test_retrieval.py agent_py_agent/tests/test_embedding_service.py \
  agent_py_agent/tests/test_minimax_embedder.py agent_py_agent/tests/test_semantic_recall_integration.py -q
```

变异验证 5/5 KILLED（脚本 `/tmp/mutate_t9.py`）：忽略缓存全量现嵌 / 缓存命中仍全量嵌入 /
缓存键不含模型标识 / 删除时不清缓存 / 缓存损坏不退回全量。

**踩过的坑**：第一版变异体把判据改成 `doc_id not in cache`，结果存活——因为 `get_text_vectors`
已过滤非 list 值，两条写法在真实输入下等价，那段 `isinstance` 防御不可达。改成「缓存命中仍全量
嵌入」这种有真实行为差异的变异体后才 5/5；教训是变异体必须与目标实现有可观测差异。

## capability 配置缺文件用默认值（2026-09-28，分支 `claude/9a-capcfg-missing-defaults`，基于 `025573d5e`）

- **新增或修改的测试**：
  - `test_capability_config_missing_defaults.py`：
    - 缺文件拿到默认实例（链深 4、每对 60），且不缓存，事后建文件即生效；
    - owner home 根目录下没有配置文件时同样是默认值；
    - 格式错误仍返回 None；
    - 决策默认值读的是 capability 文件，不是 router 上的 AgentConfig；
    - 注入的 capability_config 仍然优先。
  - 会话工具三组（`test_create_session_task_tool.py`、`test_send_session_message_tool.py`、`test_tell_command.py`）对缺文件和损坏文件两种情况做参数化，断言链深按 4 拒绝、每对限额按 60 拒绝。
  - 发消息工具的测试改为经运行时快照注入配置，并新增“读的是文件、不是 agent 属性”一例。
  - `/settings` 告警测试改为从真实 capability 文件取告警。
  - `test_capability_runtime_config.py` 原“缺文件返回 None”一例，改为断言返回默认实例，另补“坏文件返回 None”。
- **变异验证**：做了 9 个变异体，全部被抓住，每次跑完按 sha256 核对恢复原文件：
  - 源头缺文件时退回返回 None；
  - 三个会话调用点去掉 `or CapabilityConfig()`；
  - 发消息工具退回读不存在的属性；
  - 决策设置退回读 router.config；
  - `/settings` 两处退回旧读法；
  - 默认实例被写入缓存。
- **定向回归**：132 个文件，2758 passed、1 skipped。范围是引用 capability 配置的全部测试、会话、settings、决策设置相关测试，以及架构护栏、`constant_names_unique`、`test_parameter_registry`。

## 会话互通第一期派活链：复验 a646a4885（2026-09-28，分支 `claude/ae-session-task-recheck`，仅文档）

- **性质与结论**：用同一套脚本模型 harness，在真实 Gateway 和真实 TUI 上复验第 6 片的修复，**未通过**。
  - 场景 1–4、6 通过，场景 5 未通过，另发现 2 个新缺陷；没有改产品代码。
  - 被测为 `a646a4885` 的 `git archive` 导出，当时它就是 origin/main。
  - 集成方决定：生产上继续暂停会话互通，`a646a4885` 不部署。
- **环境**：
  - Gateway 8437、脚本假模型 8447，全部用 `env -i` 启动，假 HOME 跑完为空，没有模型密钥；
  - 链深上限写在 `<owner home>/config/capability_config.yaml`：复制产品自带的 YAML，只把 `session_task_max_chain_depth` 改成 2。
    再按 `resolve_workspace_roots` → `default_capability_config_path` → `capability_config_for_agent` 确认运行时读到 2；
  - 同一个 local/main 管理员 home 里开 A、B、C 三个真实 TUI 会话，渠道都是 chat。
- **结果**（判据同上一轮，只读结构化事实）：
  1. A 派给 B：通过。唤醒 reason=session_task，B 的回合开头是宿主事件；正文回执 consumed，expected_turn_id 为任务号；任务状态 done。
  2. B 再派给 C：通过。派活回合有 21 个工具，其中包含 4 个会话工具；新任务的 origin_task_id 指向上一层任务。
  3. C 再往下派：通过。返回 `SESSION_TASK_CHAIN_LIMIT`，details 为 depth=2、limit=2，没有新建任务记录。
  4. 自动回报：通过。每层都写出 origin_kind=session_task、session_task_status=done 的回报。
     回报不唤醒派活方，要到派活方的下一回合才被消费。
  5. B 执行中 A 取消：**未通过**，两种情况都停不下 B 的回合。
     - 绑定回合之前取消：只能撤队列，但正文已是 submitted，返回 `withdrawn_from_queue=false`。
     - 绑定回合之后取消：返回 `stop_confirmed=false`，回执说“目标回合已结束或切换”，而这个回合实际仍在执行。
     - 两种情况下控制操作记录都为 0；B 都把活做完、交付了输出，任务却显示 cancelled。
  6. 给正在执行前台请求的 B 发普通会话消息：通过。B 的请求为 done，没有错误码；消息回执 consumed，expected_turn_id 就是 B 的这条请求。
- **新缺陷**（已报集成方，未修）：
  - **派活回合对模型无上限空转**。
    - 触发条件：会话处在派活回合里，邮箱中挂着别的任务的完成回报（回报的 metadata 带 `session_task_id`）。
    - 根因：`claim_for_turn` 按任务归属拒绝认领这条回报，而 `available_for_turn` 不看归属，仍判定“有新输入”。
      模型的最终回复因此每次都被作废，然后再请求一次，没有上限，只能停 Gateway。
    - 链深 2 下的复现：A 先派一个快速任务给 B，B 的回报挂在 A 的邮箱里；B 再派一个根任务给 A。
      A 的派活回合 3.2 秒内请求了 88 次模型。
    - 默认链深 4 时，A→B→C→A 这条链也会触发：95 秒内请求了 3849 次模型。
  - **capability 配置文件缺失时，守卫按“不限制”处理**。
    - agent 根目录下读不到配置时，`capability_config_for_agent` 返回 None。
    - 这时 `create_session_task` 把链深和每对限额都当成 0（不限制），而不是默认值 4 和 60。
    - `workspace_root` 为空时，Gateway 的 agent 根目录是 owner home，默认就会落进这条路径。
  - 场景 5 的根因：
    - 任务要等第一次模型调用返回、正文被确认消费之后才绑定回合，在这之前取消只能撤队列；
    - 绑定之后，停止控制只在会话窗口的活动 Gateway 请求里按 expected_turn_id 找回合，而派活回合是后台主代理片，所以永远找不到。
- **观察**：
  - done 任务的 `summary` 一直为空；
  - 完成回报不唤醒空闲的派活方，而取消通知会唤醒；
  - 目标正忙时发出的会话消息和取消通知的唤醒，会在目标已经消费内容之后，再多跑一轮空的后台回合；
  - 绑定前取消的回执文案前后矛盾：message 说“还没有开始执行”，withdraw_note 说“目标已经开始处理这条正文”。
- **harness 更正**：
  - 上一轮把链深配置放在了 gateway.yaml 旁边；`workspace_root` 为空时，Gateway 的 agent 根目录是 owner home，所以运行时并没有读到那份文件。
  - 上一轮“已用产品的加载函数确认读到 2”这句不准确。那一轮链路没有走到第三层，结论不受影响。
  - 本轮第一次运行也因此没有拦住第三层，空转缺陷就是这样暴露的；场景判定以配置正确的第二次运行为准。
- **本轮验证**：
  - 只改 Markdown，运行了五项静态门禁，提交前还原了 CODE_SIZE_REPORT.md，没有跑 pytest。
  - 证据批次 `session-task-chain-e2e/recheck-a646a4885` 保存在本机验收证据目录，不进仓库。

## 会话互通：第 6 片——真实 gateway 测出的 4 个缺陷修复（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：Claude 会话 dsh-ae 用脚本模型在真实 Gateway 上跑三会话派活链（被测 `b938b2a98`），结论未通过，
  共 4 个缺陷 + 3 个观察。我的既有单测没覆盖，因为它们没走真实的后台唤醒路径。
- **修复与证据**（每条都走真实入口，并做了变异验证）：
  1. **缺陷 4（最严重）**：目标正忙时注入正文会让目标自己的请求失败（提交批次校验要求回执带 `expected_turn_id`，
     而宿主投递写入时没有接收回合）。修法：`claim_for_turn` 在**认领那一刻**给宿主投递补记目标回合号，
     并同步补精确回合索引；`_guidance_input_digest` 排除该字段；队列/回执校验只新增"队列无值、回执补记"这一种形状。
     测试 `tests/test_session_message_busy_target.py`（11 项，真实 store + 真实注入/提交）。
     **变异**：关掉认领补记 → 8 项变红。
  2. **缺陷 2**：派活唤醒没有回合号，导致正文永不认领、收尾直接返回、发送方收不到回报。修法：新增
     `_session_task_run_id(request)` 只从唤醒信封 `metadata.session_task_id` 取（缺值 fail closed），
     `_run_params` 的 `request_id/run_id/task_id` 采用它，`_background_task_attributes` 据此写会话身份，
     guidance 主代理分支回退读 `task_attributes.conversation_thread_id`，收尾用同一回合号。
     测试 `tests/test_session_task_turn_binding.py`（5 项）。**变异**：撤销接线 → 3 项变红。
  3. **缺陷 1**：后台工具策略没有 `session_task` 档，派活回合只有 17 个工具。修法：新增
     `SESSION_TASK_WAKE_REASON` + `SESSION_TASK_WAKE_ALLOWED_TOOLS` + `_is_session_task_wake` 分支。
     注：当时设计与分片规格点名的 `list_owner_sessions` 未实现，只放行了实际存在的 4 个会话工具；
     该工具已于 `claude/75-list-owner-sessions` 补上并加入本档（见本文件同名条目）。
  4. **缺陷 3**：未确认正文会在后来的回合里被再次注入。修好缺陷 2 后仍有**跨回合抢正文**：任何回合都能认领
     线程邮箱里任意未绑定的宿主投递。修法：正文写入时自带 `metadata.session_task_id`，派活回合用
     `owning_task_id` 只在编号一致时认领。测试 `tests/test_session_task_body_replay.py`（3 项）。
     **变异**：关掉归属核对 → 2 项变红。
- **顺手修的观察项**：
  - 派活正文之前渲染成"来自未知来源"，现在按 `origin_thread_id` 显示来源会话（文案区分"消息/任务"）；
  - `create_session_task` 的幂等键不再嵌入完整正文（改摘要入键）；
  - 取消通知一直 pending：`_notify_sender` 只写消息箱、**从不唤醒发送方**；现在比照派活投递在发送方空闲时唤醒它。
    测试 `tests/test_session_task_cancel_notice.py`（4 项）。**变异**：关掉唤醒 → 1 项变红。
- **回归**：会话互通全部定向测试 + `test_runtime_guidance` + `test_lifecycle_wake_host_event` +
  `test_conversation_store` + `test_packaging` + `test_architecture_guardrails` + 错误码守卫全绿；
  五项静态门禁通过（ruff / DOC_SYNC_PASS / strict code-size `blocked=False` / clean package / `git diff --check`）。
- **复现**：
  ```
  python3 -m pytest agent_py_agent/tests/test_session_message_busy_target.py \
    agent_py_agent/tests/test_session_task_turn_binding.py \
    agent_py_agent/tests/test_session_task_body_replay.py \
    agent_py_agent/tests/test_session_task_cancel_notice.py -q
  ```
- **未验证**：真实 Gateway 双会话端到端由 dsh-ae 用它的 harness 复跑（dev 安排），本片未做真机验证。
  2026-09-28 已复验，结论是未通过，见本文件开头的“复验 a646a4885”一节。

## 会话互通第一期派活链：脚本模型端到端验证（2026-09-28，分支 `claude/ae-session-task-e2e`，仅文档）

- **性质与结论**：用脚本模型在真实 Gateway 和真实 TUI 上做机制验证，**未通过**，共发现 4 个产品缺陷，未改代码。
  - 被测为 `b938b2a98` 的 `git archive` 导出；当时它就是 origin/main。
  - 之后 main 前进到 `9af85833e`，新增提交没有改动会话消息相关目录，结论同样适用。
- **环境**：
  - Gateway 8437、脚本假模型 8447，全部用 `env -i` 启动，假 HOME 跑完为空，没有模型密钥；
  - 链深上限写在运行根的 `config/capability_config.yaml`（`session_task_max_chain_depth: 2`），已用产品的加载函数确认读到 2；
    **更正**：这次确认用错了根目录，运行时其实没有读到这份文件，见“复验 a646a4885”一节。本轮链路没有走到第三层，结论不受影响；
  - 同一个 local/main 管理员 home 里开 A、B、C 三个真实 TUI 会话，渠道都是 chat。
- **判据**：只读结构化事实，不读会话正文：
  - SessionTaskStore 的状态、origin_task_id、conversation_request_id；
  - guidance 条目的 origin_kind 与一次性回执状态；
  - 唤醒队列、请求记录的状态与错误码、控制操作记录；
  - runtime.db 的 tool_completed（Gateway 停止后以 immutable 只读打开）；
  - 脚本模型实际收到的回合开头与工具清单。
- **结果**：
  - A 派给 B：投递成功，唤醒元数据和宿主事件开头都正确；但任务始终停在 queued，没有接单和回合号，正文回执是 pending。
  - B 再派给 C：B 的派活回合只有 17 个工具，没有 create_session_task。原因是后台工具策略没有 session_task 档，落到 default 档。
  - C 触发 SESSION_TASK_CHAIN_LIMIT：依赖上一步，走不到。
  - 自动回报：派活方没有收到任何回报。收尾按后台请求的 task_id 匹配，而这个 task_id 为空。
  - 取消：空闲路径只能从队列撤回，控制操作记录为 0；正忙路径中，目标的前台请求在安全点注入正文时以 `DATACORRUPTIONERROR`
    失败，原因是正文条目缺少 `expected_turn_id`，正文回执卡在 reserved，请求记录也没有 cancel 字段。
  - 另见：没确认的旧正文会在后来的回合里被再次注入。
- **环境事件**：第 1 轮后段本机数据卷曾被写满（`No space left on device`）。受影响的“目标正忙”段已作废，
  第 2 轮在新的 home 里重跑，复现了上述结果。
- **本轮验证**：只改 Markdown，运行五项静态门禁，提交前还原 CODE_SIZE_REPORT.md，没有跑 pytest。
  证据批次 `session-task-chain-e2e` 保存在本机验收证据目录，不进仓库。

## 会话间消息与派活：修复两个“没真正生效”的接线缺陷（2026-09-28，分支 `my-agent/self-dev-3`，基于 main `7bfe52778`）

- **来源**：2026-09-28 待命前做真实链路核对时发现并报给 dev，dev 批准（高优先级）后修复。两条都属于第一期已上线范围。
- **缺陷 1：派活链深度守卫完全不生效**。`create_session_task._origin_task_id()` 读 `agent.current_session_task_id`，
  但全仓生产代码**没有任何写入点**（只有测试手工赋值），`SimpleAgent` 也没有动态属性兜底 → 恒返回空串
  → `SESSION_TASK_CHAIN_LIMIT` 永不触发。测试之所以绿，是替身补了生产没有的东西。
- **缺陷 2：派活回合没有 `session_task` 的 `TurnTrigger`**。`TurnTrigger` 生产里唯一构造点只产 `lifecycle_wake`；
  派活唤醒 `reason="session_task"` 不在任何已知 reason 集合里 → 落到兜底英文 prompt + `turn_trigger=None`
  → 回合开头被落成用户轮，`loop_support` 的 `SESSION_TASK_FACTS_SOURCE` 分支是死代码。
  （任务正文**没有**冒充用户原话：`guidance.py` 的“未知 origin_kind → 宿主事件”分支兜住了，所以这条是呈现缺口。）
- **做法**：
  - `conversation/authority.py` 新增结构化属性 `CONVERSATION_SESSION_TASK_ID_ATTR`（`conversation_session_task_id`）。
  - `conversation/runtime.py`：`_session_task_id_from_wake()` 只读 `wake_signal.metadata.session_task_id` 并写进 `task_attributes`；
    `_background_model_inputs()` 最前面按该信封分流到派活触发。
  - `agent_core/runtime/turn_trigger.py`：新增 `session_task_turn_trigger(wake_signal)`——只从 metadata 取
    `session_task_id`/`origin_thread_id`，**缺 id 返回 None**（fail closed，退化成普通后台片，不凭空造派活回合）。
  - `create_session_task._origin_task_id()` 改为读 `current_conversation_task_attributes(agent)` 里的该属性。
- **新测试**：`test_session_task_wake_wiring.py`（9 项）——全部走真实入口
  （真实唤醒信封 → `_background_task_attributes` / `_background_model_inputs`），**不手工给 agent 赋值**；
  `test_create_session_task_tool.py` 新增 `test_chain_depth_accumulates_across_hops`（A→B→C 两跳真实累积，
  limit=2 拒 / limit=3 放行），并把原链深用例改为写结构化 `task_attributes`。
- **变异验证**：把两处接线改回旧实现（读手工属性 / 取消分流）后 **4 项测试变红**；恢复后全绿。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_task_wake_wiring.py agent_py_agent/tests/test_create_session_task_tool.py -q`
- **顺带排查（只报清单不改代码）**：全仓找“只被 `getattr` 读、无写入点”的 agent 属性，7 个候选逐个定类——
  `gateway_request_identity` / `request_agent_permission` / `request_agent_view` / `request_background_notices`
  是按设计的能力探测（对象未实现时走回退）；`_current_request_id` / `_heartbeat_at` / `_last_progress_at`
  是可选快照字段（缺失有默认值、语义就是“没有就不显示”）；另一个是 `lease_heartbeat_at` 的误匹配。
  均**不属于**本次那类“守卫读不到东西而静默失效”的缺陷。

## 会话间消息与派活第 4 片：code-size 压回 + 每对会话限额真生效 + 文档收尾（2026-09-28，分支 `my-agent/self-dev-3`，基于 main `059f2bf2e`）

- **来源**：dev 13:15 的第 4 片要求——①压回第 3 片新增的 code-size 发现项（soft 清零、high-risk 尽量清，
  参数 ≤4、嵌套 ≤2、类 ≤200 行）；②每对会话每小时限额必须真的生效，**回报消息也计入**；
  ③配置注释一致性；④设计文档状态改成"第一期已实现"。
- **做法（行为不变的重构）**：
  - `session_tasks.py`：`SessionTaskDraft` / `SessionTaskUpdate` 打包参数；拆出 `_replay_existing`、
    `_write_lookup_indexes`、`_bind_one_task_body`、`_session_task_body_key`，`create`/`bind_session_task_turns`
    的嵌套与参数降下来。
  - `session_task_control.py`：`execute` 从 59 行降到 13 行——拆出 `_resolve_cancel_target`、
    `_terminal_task_outcome`、`_cancel_and_describe`、`_apply_cancel`；失败形状收进 `_TaskFailure`
    （原 `_error` 的 5 个参数降到 1 个），取消结果收进 `_CancelOutcome`。
  - `session_task_report.py`：新增 `TaskTurnOutcome` 打包回合结果，`finish_task_for_turn` / `close_out_turn`
    参数 5→2（`runtime.py` 调用点同步）。
  - `create_session_task.py`：新增 `_DispatchPlan` / `_origin_task_id` / `_dispatch_plan` / `_queue_body`。
  - `loop_support.py`：把原生 IR 开头项拆成 `_native_turn_opener` + `_host_event_source`（行为不变，
    仍是"生命周期唤醒/派活 → 宿主事件，普通回合 → 用户轮"）。
  - `slash_commands.py`：拆出 `_received_message_label`。
  - `test_session_task_e2e_flow.py`：`_Threads.resolve` 拆出 `_binding_matches`。
- **code-size 证据**：以 `e726d03ad`（第 3 片之前）为基线，按"发现项身份 + 级别"求差，
  第 3 片带来的新增发现项从 **13 项（9 high-risk + 4 soft）降到 0 项**。
- **限额真生效（关键修复）**：`session_pair_hourly_limit` 之前**只有定义、没有任何读取点**——限额其实没生效。
  新增 `agent_py_agent/agent/conversation/session_pair_rate.py`（`SessionPairRateLimiter` + `pair_limit_reached`
  + `record_pair_message`），在 `ConversationStore` 组装、`store_layout` 里建 `session_pair_rate/` 目录；
  接线点四处：
  - `send_session_message`（模型工具）与 `/tell`（TUI）：投递前判断，超限返回 `SESSION_TASK_RATE_LIMIT` 且**不投递、不占配额**；
  - `create_session_task`（模型工具）：建记录前判断，同上；
  - **任务回报（`session_task_report`）与取消通知（`session_task_control`）**：宿主自动消息**不被拒**，但**计入配额**——
    这是 dev 明确要求的"回报也计入"，否则回报可以绕过限额。
- **新测试**：
  - `test_session_pair_rate_limit.py`（7 项）：分桶独立、窗口跨小时归零、`limit=0` 不限制、空 thread id 不计数、
    没有计数器组件时 helper 不报错。
  - `test_send_session_message_tool.py` 新增 3 项：超限被拒且不入队不占配额、到上限前放行、`0` 不限制。
  - `test_create_session_task_tool.py` 新增 2 项：超限不建记录不投正文、派发后占配额。
  - `test_tell_command.py` 新增 2 项：TUI 入口同样受限、成功后占配额。
  - `test_session_task_report.py` 新增 2 项：**任务回报**与**取消通知**都计入配额。
- **变异验证**：把 `over_limit` 临时改成恒 `False`（即限额失效）后，7 项相关测试变红；恢复后全绿。
- **复现**：
  `python3 -m pytest agent_py_agent/tests/test_session_pair_rate_limit.py agent_py_agent/tests/test_send_session_message_tool.py agent_py_agent/tests/test_create_session_task_tool.py agent_py_agent/tests/test_tell_command.py agent_py_agent/tests/test_session_task_report.py -q`

## user_config 的 decision_patch 通道：多带字段时回执写明是哪个（2026-09-28，分支 `claude/be-decision-patch-fix`，基于 `fc494da3f`）

## 会话间消息与派活第 3 片 F：端到端（派活 → 执行 → 回报 / 执行中取消）（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：第 3 片 F：把前面各片串成真实链路验证，包含 dev 要求的"派活 → 执行中取消"。
- **做法**：新增 `test_session_task_e2e_flow.py`，不启动 Gateway、不发真实模型请求，只用真实组件：
  - 发送方用**真实** `CreateSessionTaskTool` 派活（真落 `SessionTaskStore`、真投 guidance、真 `wake.raise_signal`）。
  - 目标会话按派活回合开跑：**真实** `TurnTrigger(kind=session_task)`；接手回合走
    `SessionTaskStore.bind_turn`（与 guidance ack 的 `bind_session_task_turns` 同一权威入口）。
  - 回合结束用**真实** `close_out_turn`（D 的收口）推进 `done` 并把结构化回报投回发送方队列。
  - 取消分两条：未开始时撤队列（`withdrawn_from_queue=True`，目标队列里该正文消失）；
    已绑定时报告 `stop_confirmed=false` 且文案写"尚未确认"——因为**本测试没有 Gateway 队列**，
    停止控制在无队列时按设计 fail closed（这一点很关键：不得谎称已停止）。
- **真实 Gateway 上的补充验证**：用隔离 Gateway（临时 home + 独立配置 + 8510 端口，不碰生产 8420）
  启动成功（`/status` 返回 `running`、`http_port=8510`），用它验证了"目标回合正在执行时取消"走
  真实停止控制链路的那一段（请求记录被标记、目标线程被中断）。
- **新测试**：5 项；`python3 -m pytest agent_py_agent/tests/test_session_task_e2e_flow.py -q`。
- **遗留**：F 使用隔离 Gateway 的那一段是一次性实测（未固化成自动化用例，因为需要真实后台进程）；
  收尾按 dev 要求停掉该进程并确认端口空闲。

## 会话间消息与派活第 3 片 E：接收方 TUI 展示（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：dev 第 6 点"接收方 TUI 显示收到的消息及来源"（已同意与第 3 片一起做）。
- **做法**：新增 `/sessions inbox`（`_print_received_inbox`）：
  - 只读结构化字段：guidance 的 `origin_kind`（`session_message` / `session_task` / `session_task_result`）
    与 `SessionTaskStore` 的 `target_thread_id`；**不解析正文猜归属**。
  - 列出消息/任务/回报的来源会话、状态与"已注入/待注入"；别的会话的内容不显示。
  - 普通用户插话不列为"收到的会话消息"。命令登记进 `command_catalog.py`。
- **新测试**：`test_sessions_inbox_command.py`（7 项）；`python3 -m pytest agent_py_agent/tests/test_sessions_inbox_command.py -q`。

## 会话间消息与派活第 3 片 D：任务正常结束自动回报（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：第 3 片 D：任务结束时把结构化结果作为 message 回给发送方（此前只接了取消路径）。
- **做法**：新增 `agent/conversation/session_task_report.py`：
  - `task_for_turn`：只按**结构化** `conversation_request_id` 找回属于本回合的任务（不读正文、不猜）。
  - `close_out_turn`：只在任务非终态时推进 `done`/`failed`，并把状态/摘要/产物 refs 作为一条
    `origin_kind=session_task` 的消息投回发送方（`guidance.append_once`，幂等键含任务 id + 终态）。
  - 幂等：终态后再次收口不再推进、不再写第二条回报；**读坏账时不猜归属**（宁可不回报）。
  - `runtime.py` 在目标回合收口处调用；**回报属于附加交付，失败不影响本回合本身**。
- **新测试**：`test_session_task_report.py`（9 项）；`python3 -m pytest agent_py_agent/tests/test_session_task_report.py -q`。

## 会话间消息与派活第 3 片 C 补丁 + 派活回合宿主事件（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：dev 12:08 指出"取消只改了账本"；另外自查发现派活回合在原生 IR 里被当成普通用户轮。
- **做法**：
  - 新增 `gateway_parts/session_task_stop.py`：按目标会话的**结构化渠道身份 + expected_turn_id**
    复用 `/stop` 同一条控制通道；拿不到渠道身份或 Gateway 路径就 fail closed（不猜回合）。
  - `CancelSessionTaskTool` 分流：已绑定回合 → 发精确停止控制；未开始 → 撤目标队列正文
    （把正文 guidance 回执标 `rejected`，注入层不再取到它）；回执按实际结果写
    "已停止 / 未开始已撤销 / 已是终态未改动"，**只在确认生效时才说"已停止"**。
  - `SessionTaskStore` 新增 `body_dedupe_key` / `load_by_body_dedupe_key` / `bind_turn`；
    guidance ack 时（`bind_session_task_turns`）把被消费的正文绑到该回合，写 `accepted` + request id。
  - **真实缺陷修复**：`loop_support.py` 构造原生 IR 开头项时只认 `lifecycle_wake`，
    `session_task` 会被落成 `UserTurn`（任务正文冒充用户原话）；现在用 `SESSION_TASK_FACTS_SOURCE`
    单独成片，`_current_turn_opener_count` 同步认两种宿主来源。
- **新测试**：`test_session_task_cancel_stop.py`（6 项）、`test_lifecycle_wake_host_event.py` 新增 2 项。
- **复现**：
  `python3 -m pytest agent_py_agent/tests/test_session_task_cancel_stop.py agent_py_agent/tests/test_lifecycle_wake_host_event.py -q`。
- **变异验证怎么做（踩过的坑）**：这里用过两种方式——
  ① 临时改坏一行实现、跑测试看是否变红、再改回来（本例验证了 stop_confirmed 恒真、
  忽略绑定回合、不撤队列、绑定不写回合号四处都会让测试变红）；
  ② 在测试里内置"变异用例"。**结论：只用①**。②需要在测试运行期改写被测源文件，
  一旦路径或开关写错就会把实现文件清空（本项目实际发生过一次，靠 git 恢复），
  代价远大于收益，已移除。

## 会话间消息与派活第 3 片 C+D：任务查询与取消（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：第 3 片 C（取消）与 D（结果回报的控制侧）。
- **做法**：
  - 新增 `agent_core/orchestration/tools/session_task_control.py`：
    - `GetSessionTaskTool`（只读）：返回任务状态、正文引用（`body_guidance_id`）与结果 refs；
      任务不存在返回 `SESSION_TASK_NOT_FOUND`。
    - `CancelSessionTaskTool`（结构化控制）：只在非终态推进到 `cancelled`（复用 `SessionTaskStore` 的状态机，
      不重复实现迁移规则）；已是终态时**幂等返回当前状态、不改写**；成功后把"已取消"作为一条来源明确
      的消息排队回发送方（`guidance.append_once`，`origin_kind=session_task`）。
  - 新增错误码 `SESSION_TASK_NOT_FOUND` 并登记；`core.py` 在同一 `session_task_tool_visible` 条件下注册三个工具。
- **新测试**：`test_session_task_control_tool.py`（6 项）——查询返回结构化状态、任务不存在、
  缺参数失败、取消非终态生效并把通知排队、取消终态任务不改写（幂等）、取消不存在任务。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_task_control_tool.py -q`（6 项）。
- **未完成**：结果回报的自动接线（任务结束时把结构化结果回给发送方）、接收方 TUI 展示（E）、
  fake LLM 端到端（F）。

## 会话间消息与派活第 3 片 B：TurnTrigger 新 kind + create_session_task 工具（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：第 3 片 B（管理员派任务），dev 要求功能完整（派任务 + 结果回报 + 取消 + 两侧可见）。
- **做法**：
  - `agent_core/runtime/turn_trigger.py` 新增第二种触发 kind `session_task`：
    `session_task_trigger()`、`SESSION_TASK_FIRST_LINE`（写明"另一个会话派来一个任务，不是当前用户原话"）、
    `SESSION_TASK_FACTS_SOURCE`、推荐短名单与理由；`current_turn_text()` 增加派活分支（**绝不让任务正文落在用户原话位置**）。
    `turn_trigger_recommendation()` 改为支持两种 kind。**lifecycle_wake 行为保持字节不变**。
  - 新增 `agent_core/orchestration/tools/create_session_task.py`：`CreateSessionTaskTool`。
    - 与发消息**共用同一权限判定**（`kind=task`），并额外落一条 `SessionTaskStore` 权威记录。
    - **正文只存一份**：先投 guidance（`origin_kind=session_task`）拿到 `guidance_id`，记录里只引用它。
    - **链深守卫**：按 `origin_task_id` 结构化计算（读 `current_session_task_id`，不接收模型参数）；
      达到 `session_task_max_chain_depth` 拒绝（0 表示不限制）。
    - 目标 `status=active` 时 `wake.raise_signal`（`reason=session_task`）。
  - `session_messaging.py` 新增常量 `SESSION_TASK_CHAIN_LIMIT` / `SESSION_TASK_RATE_LIMIT` / `SESSION_TASK_ORIGIN_KIND`；
    `error_taxonomy` 登记前两个错误码；`core.py` 按 `session_task_tool_visible` **条件注册**。
- **新测试**：`test_create_session_task_tool.py`（7 项）——缺参数、目标不存在越界、IM 目标拒绝、派活开关关闭、
  成功建记录 + 投正文 + 唤醒、链深超限拒绝、链深 0 表示不限制。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_create_session_task_tool.py -q`（7 项）；
  与前序共 98 项全绿（含 `test_lifecycle_wake_host_event` 回归 20 项）。
- **未完成**：取消（C）、结果回报（D）、接收方 TUI 展示（E）、fake LLM 端到端（F）。

## 会话间消息与派活第 3 片 A：SessionTaskStore 权威存储（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：第 3 片（管理员派任务）。设计文档要求 task 状态只有**一个权威**、**单一写入方**，正文只存一份。
- **做法**：
  - 新增 `conversation/session_tasks.py`：`SessionTask` 值对象 + `SessionTaskStore`。
    - 状态机唯一权威：`queued → accepted → done | failed | cancelled`；终态不可被普通生命周期改写；
      `queued` 可直接取消；同状态重复写入视为幂等。
    - **正文只存一份**：`body_guidance_id` 引用既有 guidance 条目，本 store 不复制正文。
    - **状态推进走 `update_json_file_atomic` 锁内重读**再校验迁移，避免两个写入方互相覆盖（双账）。
    - **幂等**：同 `dedupe_key` 同输入返回原记录；同键异文抛错。
    - **链深**：`chain_depth()` 按 `origin_task_id` 结构化遍历（带环保护），**不信任模型传入的深度**。
  - `store_layout.py` 新增 `session_tasks_dir`（含 `dedupe` 子目录）并入 `managed_dirs`；
    `store.py` 组装 `self.session_tasks`。
- **新测试**：`test_session_task_store.py`（12 项）——创建、幂等重放、同键异文报错、合法/非法迁移、
  queued 直接取消、终态保护、同状态幂等、链深（含环）、缺失返回 None、列表排序。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_task_store.py -q`（12 项）；
  回归 `test_conversation_store.py`（52 项）通过。
- **未完成**：本片只做权威存储；派活工具、TurnTrigger 新 kind、取消、结果回报、接收方 TUI 展示、
  fake LLM 端到端在后续片。

## 会话间消息与派活第 2 片：TUI 命令 /tell 与 /sessions threads（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：生产 runtime-step15c 上，my-agent 调 Jev 等待时间，`decision_patch` 连续被拒三次；dsh-9b 早先也遇到过。
  回执都是 `TOOL_INVALID_ARGUMENTS`「决策设置请求包含未知字段」，`handler_executed=true`、`effect_outcome=not_started`。
- **复现与根因**：
  - 合法请求一直能用：经真实工具执行器（schema 校验、授权门、handler），只改 `background_timeout_seconds` 的干净请求在旧代码上就能落盘，不是红的。
  - 同样的请求多带一个 schema 允许、但属于别的动作的字段（`reason`、`fields`、`profile_id`、顶层 `timeout_seconds`）时，
    得到与生产完全一致的回执。可选字段全填 null 会在 schema 层就被拦下，回执不同，已排除。
  - 根因：`user_config` 各动作共用一份扁平 schema，工具把除 action 外的字段全部转给设置服务；服务按操作严格拒收多余字段，
    但报错不写字段名。模型只会改 `changes`，所以改什么都失败。生产那三次都多带了 `reason`（来源：my-agent-1 作为调用方的记录，
    三次同因；audit 只记工具名和状态、不记参数）。
- **改动**：
  - 设置服务改抛 `DecisionSettingsUnknownFields`（仍是 `ModelProfileError`，原捕获点不变），带未知字段和本操作接受的字段。
  - 工具回执与 `handler_details` 写明 `unknown_fields`、`allowed_fields`；效果仍是 not_started，什么都不写。
  - 工具说明写清 patch、reset 各接受哪些字段。不放宽校验，也不替模型删字段。
- **测试**（`test_user_config_decision_patch.py`，8 项）：
  - 合法 patch 经真实执行器落盘，owner revision 加一；
  - 4 种多带字段仍被拒：与生产同形（handler 已执行、效果未开始），回执写明 `unknown_fields`、`allowed_fields`，消息里有字段名，什么都没写；
  - reset 多带 `changes` 时同样指名；
  - 设置服务本身仍严格拒绝，异常仍是 `ModelProfileError`；
  - 假模型按“把后台决策等待时间调到 15 秒”行事：先按生产写法多带 `reason` 被拒，按回执删掉该字段重试后落盘。
- **旧代码对照**：同一测试文件（去掉新异常的导入）放到 `fc494da3f` 上跑，干净请求通过；4 种多带字段、reset 和假模型各项失败。
  旧回执是纯文本、没有字段名，假模型无从纠正，设置也没有落盘。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_user_config_decision_patch.py -q`。
- **变异验证**：10 个全部抓出，每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，跑完逐字节恢复并核对哈希。
  - 服务端：拒绝时不写字段名；放宽字段检查；异常不再是 `ModelProfileError`；消息不含字段名。
  - 工具端：替模型删掉多带字段；不捕获新异常；回执缺 unknown_fields；allowed_fields 写错；信封丢失；效果状态写错。
- **相关回归**：253 个测试文件 5680 passed、1 skipped、24 xfailed、5 xpassed。清单包括所有涉及 user_config、决策设置与服务、
  工具目录与 schema 的测试，以及全部扫描产品代码的守卫测试（含 `test_architecture_guardrails.py`、`test_constant_names_unique.py`）。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package 均通过；code-size 身份差集相对基线新增 0、减少 0。

## 前端目录生成器不再把 YAML 文件头算进第一个键（2026-09-28，分支 `claude/9a-catalog-header`，基于 `ca756b5db`）

- **问题**：`frontend/scripts/sync-backend-config.mjs` 取键上方注释时空行不打断，所以每份 YAML 的第一个键都吸进了文件头，
  例如 `agent_name`、capability 的第一个键、log_analysis 的 `enabled`。
- **规则**：
  - 第一个键出现前遇到空行，就丢弃已累积的注释。以空行结束的首段注释说明的是整份文件，不算进第一个键。
  - 第一个键之后，归属规则不变。
  - 后端 `parameter_registry._descriptions_from_lines` 遇空行即清空注释块，本来就没有这个问题，对第一个键的结果与新规则一致，
    所以后端代码没改；只在 `test_parameter_registry.py` 补一例文件头用例，钉住两边共同的规则。
- **验证**：
  - 用旧、新生成器对同一份 YAML 各生成一次，逐叶比对：只有 3 个第一个键的描述不同，其余字段（包括排序号）完全一致。
    - `agent_name` 和 capability 的第一个键：只剩自己的说明；
    - log_analysis 的 `enabled`：它没有自己的注释，回落成通用占位文字。
  - node 和 bun 的 `--check` 都是 rc=0，两者输出逐字节相同。
  - main 上已提交的目录原本就过期（`--check` rc=1）。这次重新生成顺带补进了 main 上新增的 5 个 `session_*` 键。

## 当日新增 code-size 发现项清理（2026-09-28，分支 `claude/75-codesize-cleanup`，基于 `ca756b5db`，行为不变）

- **比较口径**：用 `check_code_size.py --write-baseline` 按发现项身份和级别比较今早的 main `0c340fe29` 与 `ca756b5db`。strict 口径下 high-risk 从 1518 升到 1524，soft 从 708 降到 705。
- **已清掉 6 项**：相对 `ca756b5db` 新增 0 项、消失 6 项；strict high-risk 降到 1519。
  - `agent_tree/status.py::agent_tree_status_payload`：函数 52 行。把快照解析拆到 `_resolved_kernel_snapshot`。
  - `agent_tree/status.py::_unmatched_explicit_run_query`：参数 5 个。删掉未使用的 `params` 参数。
  - `plugin_management.py::_reply`：函数 57 行、嵌套 3 层。拆出 `_attach_catalog`、`_reply_message`、`_release_message`、`_enable_use_card`，两张说明表提为模块常量，elif 链改为提前返回。
  - `tooling/artifact.py::ReadArtifactTool.__init__`：函数 60 行。运行时策略提为 `_read_artifact_runtime_policy`，可信绑定提为常量。新旧 `runtime_policy` 的 repr 摘要一致。
  - `tests/test_background_main_agent_runtime.py::test_failed_subagent_completion_wake_is_not_delayed_by_success_coalescing`：函数 49 行。只压缩构造语句的排版。
- **跳过 4 项**：
  - 有人在改，跳过：`session_messaging.decide_session_messaging`、`send_session_message._queue_and_wake`、`runtime/guidance._render_guidance_user_input` 的嵌套（my-agent-3 在改）。
  - 改善但未继续拆：`subagents/services/base.py::SubAgentBaseService` 今天从 soft（大于 250 行）降到 high-risk（239 行），属于改善；压到 200 行以下要移出约 40 行建 run 权威写入逻辑，本片不动。
- **验证**：相关 33 个测试文件共 675 项通过，覆盖 agent_tree、插件管理与命令、artifact 读取、后台主代理、architecture_guardrails、constant_names_unique、code_size_script。五项静态 gate 全部通过。

## task_progress / cancel_subagents 的范围裁决（2026-09-28，分支 `my-agent/self-dev-2`，基点 `ca756b5db`）

- **来源/做法**：dev 派活任务 5。沿用 my-agent-4 在 list_agents 里定的裁决码（`30c3b98d4`），把两处遗留的静默行为补成结构化裁决。
- **裁决码只有一份**：`agent_core/orchestration/scope_resolution.UNMATCHED_RUN_SCOPE_CODE
  = "requested_run_id_not_in_visible_scope"`；`agent_tree/status.py` 从它导入，不再自带副本。
- **task_progress**（`agent_core/task_progress_tool.py`）：新增 `_unmatched_explicit_run_target`，在 `TaskProgressTool.execute`
  取到 run_id 之后立刻判定——显式 id 既不是当前身份（run/task/durable/当前账本键）也不是磁盘上已存在的账本时，
  返回 `_scope_rejection_outcome`：`ok=false`、`error_code=TOOL_INVALID_ARGUMENTS`、`effect_outcome=not_started`，
  `scope_resolution.explicit` 保留请求 id、`effective` 为空、`scope_warnings` 带裁决码。读和写都走这一关，**不新建账本**。
- **cancel_subagents**（`agent_core/orchestration/tools/cancel.py`）：显式点名的 id 必须在当前可见树里；不在就
  在解析阶段返回同一裁决码（不再一路带到取消执行层报成无意义的 programmer 错误）。`_cancel_error_code` 把该 payload
  映射成 `TOOL_INVALID_ARGUMENTS`，不再兜底 `UNKNOWN_ERROR`；没给任何目标时仍是 `TOOL_PARAMETER_REQUIRED`。

### 复现

```bash
python3 -m pytest agent_py_agent/tests/test_tool_scope_resolution.py \
  agent_py_agent/tests/test_list_agents_scope_resolution.py agent_py_agent/tests/test_agent_tree_model_view.py \
  agent_py_agent/tests/test_agent_tree_three_layer_status.py agent_py_agent/tests/test_orchestration_tools.py -q
```

新增 12 条用例；连同 list_agents / 树视图 / 编排工具共 72 passed。

### 变异验证（8/8 KILLED）

| 变异体 | 结果 | 被杀它的用例 |
|---|---|---|
| task_progress 入口不做可见性裁决 | KILLED | `test_tool_progress_entry_point_returns_the_denial_not_a_silent_write` |
| 只对写路径裁决、读路径放行 | KILLED | `test_tool_progress_entry_point_read_is_also_denied` |
| 判据改成“永远不可见”（连当前账本也拒） | KILLED | `test_current_ledger_and_existing_ledger_are_not_denied` |
| 拒绝回执丢掉裁决码 | KILLED | `test_scope_rejection_payload_names_the_shared_decision_code` |
| 请求 id 冒充成 effective 范围 | KILLED | 同上 |
| cancel 不校验显式目标可见性 | KILLED | `test_cancel_invisible_explicit_target_is_judged_at_resolution` |
| cancel 去掉 unmatched 过滤 | KILLED | 同上 |
| 错误码映射丢掉裁决码分支 | KILLED | `test_cancel_error_code_maps_scope_denial_to_invalid_arguments` |

**一次真实迭代**：第一版没有 entry-point 接线用例，只测判定函数——把 `execute` 里那两行裁决改成 no-op，整批仍全绿（M1 存活）。
补了走 `TaskProgressTool.execute` 的读/写两条端到端用例后才杀掉。**只测 helper 不算证明接线生效。**

### 排查记录（供复用）

- `cancel_subagents` 原来把不可见的显式 id 一路带到 `_filter_existing_targets`，最终以 `programmer_b...` 类错误收场；
  只在“空列表”分支加说明是不够的，必须在解析阶段判定可见性。
- 写 `pytest` 探针时注意：`agent_py_agent` 是 editable 安装的命名空间包，在仓库根目录外跑脚本会 import 到别处的安装包；
  探针写成临时 `tests/test_*.py` 再跑，和现有测试同一环境。

## 任务 5 回归修复：cancel 的"存在"判据与 task_progress 的当前身份链（2026-09-28，分支 `my-agent/self-dev-2`）

- **来源**：dev 派活任务 6。`aba69ef47` 在全量集成里弄坏 2 个既有测试，我的定向测试范围太窄没覆盖它们。
- **缺陷 1（cancel）**：原判定把"可见集合"算成 `{t.id for t in list_runs()}`。账本损坏的 run **存在但加载失败**
  （实测抛 `JSONDecodeError`），此时 `list_runs()` 甚至整批返回空，于是它被误判"不可见"拦掉，回执丢了 `failed` 段。
  **修法（dev 确认的口径）**：判定只依据既有加载结果，不另算第二套可见集合。按 `authorization_gate` 的既定契约，
  `manager.load` 对不存在的 run 透传 `FileNotFoundError`（→ 给裁决码），其余加载错误（损坏/无权）一律**存在**，
  原样放行到既有 failed 路径。实测对照表：
  | 情形 | error_type / category | 判据结果 |
  |---|---|---|
  | 真不存在 | `FileNotFoundError` / `io` | 不存在 → 裁决码 |
  | 账本损坏 | `JSONDecodeError` / `data_parse` | 存在 → failed 路径 |
  | 健康 | 能加载出 task | 存在 → 正常取消 |
- **缺陷 2（task_progress）**：判定集合只从 `_current_run_params` 派生；宿主只有 `_main_agent_run_id` 时它为空，
  合法的当前目标被拒成 `task_progress_scope_mismatch`。**修法**：当前账本键统一由
  `_target_run_id(allow_explicit=False)` 解析（其回退链已覆盖 scoped/子代理/`_main_agent_run_id`/`_current_request_id`），
  不再另拼一套同源集合——第一次实现里确实多写了一段冗余集合，变异验证暴露它没被任何测试钉住（M5/M6 存活），
  删掉后改用单一权威判据才 8/8 KILLED。
- **复现**：
  ```bash
  python3 -m pytest agent_py_agent/tests/test_orchestration_cancel_subagents_tool.py \
    agent_py_agent/tests/test_task_progress_advisory.py agent_py_agent/tests/test_tool_scope_resolution.py -q
  ```
  dev 要求另按调用点全量跑：grep 出构造 `TaskProgressTool` / 调 `cancel_subagents` 的 **29 个测试文件**，共
  **810 passed / 1 xfailed**。
- **既有失败（非本次引入，已在干净 `origin/main` 复核）**：
  `test_gateway_chat_conversation_context.py::test_first_gateway_shell_keeps_explicit_working_dir`
  在 `origin/main` 的独立 worktree 上同样失败，与本次改动无关。
- **变异验证 8/8 KILLED**：存在性判据忽略 FileNotFoundError / 判据恒 False / 解析阶段不裁决 /
  判据退回 `list_runs` 可见集合 / 当前身份改用 `_current_run_params` / 回退链被截断 / 判定恒不拒绝 / 判定恒拒绝。
- **排查坑（供复用）**：这两个既有测试用 `SimpleNamespace(subagents=...)` 只桩 `list_runs`，改判定后必须在桩上补
  真实的 `load` 契约（不存在 → `FileNotFoundError`）；否则加载器整体不可用会退化成 `programmer_bug`，
  判据失去可分辨信号。**测桩也要反映真实契约，不能只桩被测函数恰好用到的那一个方法。**


## Jev observe 采样开关：成功满 6 次后本小时不再调用（2026-09-28，分支 `my-agent/self-dev-2`，基点 `ca756b5db`）

- **来源/做法**：dev 派活任务 4（从 my-agent-1 队列转来）。背景是 11 个点位全 observe、48 小时 276 次调用只换来观察记录。
- **开关**：通用字段 `observe_sampling_enabled`（默认 false），走 decision_settings 的 GENERAL_FIELDS，与 `enabled`/
  `experiment_enabled` 同一套布尔校验；`AgentConfig`、随包 YAML（中文注释）、`services/_normalize` 运行布尔表同步。
- **上限**：`conversation/decision_point_limits.OBSERVE_SAMPLED_SUCCESS_LIMIT = 6`，内部常量、不进用户参数。
- **判定**：`decision_service._sampled_outcome` 只对 `observe_sampling_enabled=true` 且点位 `effective_mode == "observe"` 生效；
  命中返回 `DecisionOutcome("observe", "skipped", reason="observe_sampled_out")`（`may_apply=False`，保留原业务方案）。
  apply 与实验路径不进（`_decide_outcome` 里 `not stage.experiment and ...` 两个前置条件）。
- **只统计成功**：记账在 `_invoke_call` 的 success 分支（`mode == "observe" and not stage.experiment`），调
  `decision_reach_counts.note_observe_sample_success`，与到达计数共用同一账本/节流/保留期，用独立内部码
  `SAMPLE_SUCCESS="observe_sample_ok"`；`_point_row` 把它排除，`reached/called/not_called` 诊断口径不变（0=不限等旧语义也保持）。

### 复现

```bash
python3 -m pytest agent_py_agent/tests/test_decision_observe_sampling.py \
  agent_py_agent/tests/test_decision_reach_counts.py agent_py_agent/tests/test_decision_settings_scope.py \
  agent_py_agent/tests/test_decision_model_call.py agent_py_agent/tests/test_decision_background_deadline.py \
  agent_py_agent/tests/test_decision_service.py agent_py_agent/tests/test_packaging.py -q
```

10 条采样用例 + 相邻决策/打包用例共 116 passed（rebase 到 `ca756b5db` 后重跑）。

### 变异验证（每条用例都能杀掉一个变异体，8/8 KILLED）

| 变异体 | 结果 | 被杀它的用例 |
|---|---|---|
| 判定忽略开关（关着也按点数跳） | KILLED | `test_sampling_off_never_skips_even_with_many_successes` |
| 判定忽略点位模式（apply 也被采样） | KILLED | `test_apply_point_is_not_sampled` |
| 上限调小 1（阈值写错） | KILLED | `test_observe_sample_limit_is_the_frozen_six` |
| 判定改用全部到达数（失败也吃名额） | KILLED | `test_failures_and_timeouts_do_not_consume_the_quota` 等 3 条 |
| 失败原因码也写进成功样本键 | KILLED | `test_seventh_successful_call_is_skipped_and_reported_with_a_reason` |
| 样本计数混进 reached 诊断 | KILLED | `test_sample_counter_does_not_change_reach_diagnostics` |
| 跳过不返回结构化原因（标成普通 error） | KILLED | 同上第 7 次用例 |
| 跳过结果误标 `may_apply=True` | KILLED | 同上第 7 次用例 |

两次迭代：第一版只有 5 个变异体，其中「阈值差 1」和「失败也吃名额」**活下来了**——说明用例只测了“第 7 次会跳”，
没钉住“第 N 次必须不跳”和“失败事件不落到成功样本键”。补了 `test_sample_quota_boundary_is_exactly_the_named_constant`、
`test_observe_sample_limit_is_the_frozen_six`，并加强失败用例的键断言后，8 个变异体全部被杀。

### 写决策用例的坑（dev 要求记下，供其他人省时间）

- `_decide_outcome` 会把内部异常吞成 `error/enhancement_failed`，表面看像采样逻辑坏了，其实是入参不合法。定位方法：
  临时把 `_failure_outcome` 换成把 `type(exc).__name__` 带进 `reason` 的替身，一条命令就能看到真实异常。
- `DecisionRequest` 的三个硬约束：`state` 必须是 JSON 数据（裸 `object()` 报 `invalid_input`）、`questions` 必须非空、
  `DecisionResponse` 必须补齐 `(binding, input_digest, requested_model, model, answers, _usage_json)` 六个位置参数。
- 真实 `_stale → _snapshot` 会读盘上的会话设置（需要 `conversation_store`）。只验采样这类窄逻辑时，要把
  `_snapshot`/`_stale`/`connection_revision`/`cooldown_state` 固定住，否则替身宿主会在无关步骤上炸。
- 实验路径不做采样：产品代码用 `not stage.experiment` 明确排除，用例断言“判定函数在 apply/实验路径上不会被问”即可，
  不要为了跑通去伪造 `decision_experiment` 的独立路由。

## 参数减量 C 组合入后重新生成前端参数目录（2026-09-28，分支 `claude/9b-frontend-catalog-c`，基于 `3d76ac687`）

## /stop 回执列出实际停掉的资源（pid + task/run 归属）（2026-09-28，分支 `my-agent/self-dev-4`，基点 `fc494da3f`）

- **来源**：dev 在开发交流板 11:22 的要求——"停止之后要把实际停掉的资源（task/run、PID）列给用户看，别只回一个 ok"。
  核对结果：`gateway stop --stop-background` 侧本来就打印结构化事实（所属 task/run、pid、启动时间），
  但会话内 `/stop` 只回"已停止本会话遗留的 N 个后台资源。"，只有数量、没有 pid 与归属。
- **做法**：`cli/chat_parts/control_runtime._background_resource_lines(processes, results)` 把
  `session_background_processes` 过滤出的行（含 `pid` / `root_task_id` / `run_id`）与 `stop_background_processes`
  的回执按 `session_id` 配对，输出 `- pid 54321（task task-1 / run run-1）` 形态的明细；只有确认未退出时才追加
  "尚未确认退出"。配对不上的行不编造停止状态；pid 缺失写"pid 未知"、不写 0。停止逻辑与如实报未确认的判定分支未改。
- **新测试/断言**：`test_stop_without_running_turn_reclaims_session_resources` 增断言（pid、task、run、"尚未确认退出"）；
  新增 `test_stop_lists_stopped_resources_with_pid_and_ownership` 覆盖全停成功路径（此前没有用例覆盖 ok=True 分支）。
- **复现**：`PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_session_stop_background_resources.py -q`
- **变异验证**：把 `_background_resource_lines` 改成返回空列表（等价"只给数量"）→ 上述 2 条用例变红（`.FF..`），
  实测消息退回 `已停止本会话遗留的 1 个后台资源：\n`；还原后 6 项全绿。

## 会话内 /stop 回收被中断任务的后台资源（2026-09-28，分支 `my-agent/self-dev-4`）

- **来源**：my-agent-4 开发交流板任务 3 后半截（dsh-be 的 R10 深度验收）。回合被 /interrupt 后托管后台进程
  按设计继续运行（这一点不能改），但用户在**同一会话**再执行 /stop 只会收到"当前没有运行中的内容"，
  没有任何入口能回收它们。
- **做法（为什么选扩展 /stop 而不是新命令）**：新增独立命令会让用户先知道"有遗留资源"才能想到用第二条命令，
  而 /stop 本来就是该会话的"停止"入口、用户的第一反应就是它；因此让 /stop 在**没有运行中回合**时
  再退一步回收本会话登记的资源。代价是 /stop 语义从"停止本回合"扩展到"停止本会话在跑的东西"，
  与 gateway 侧 `--stop-background` 是同一条资源路径，不存在第二套停止实现。
- **实现**：`cli/chat_parts/control_runtime._stop_session_background_resources` 在无 `request_id` 时：
  按 `process_session_store_root(effective_workspace_root, owner_home)`（与 ShellTool 写入端同源，
  `tooling/shell.py:1199`）定位登记表 → 用 `session_background_processes` 按**精确 thread_id** 筛本会话资源
  → 复用 gateway 侧同一 `stop_background_processes`（登记表冻结意图，实际回收由原 host
  `terminate_process_tree` 按进程组完成）。没有登记就如实回"没有运行中的内容"；登记表损坏或读不了、
  停止未确认都报 `TASK_RESOURCE_STOP_UNCONFIRMED`，不伪报已停。
- **新测试**：`agent_py_agent/tests/test_session_stop_background_resources.py` 4 项——会话过滤只保留精确
  thread（空 thread 不当"全部"）、无回合时真的回收本会话资源（不再回"没有运行中的内容"；没有真实 host
  时如实报未确认退出）、无资源时仍回"没有运行中的内容"、登记表不可读时报 unknown。
- **复现**：`PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_session_stop_background_resources.py -q`
- **变异验证**：①把无 `request_id` 分支改回直接返回"当前没有运行中的内容" → 2 条红；
  ②把 `session_background_processes` 放宽成不过滤 → 会话隔离用例变红。
- **环境说明**：与 gateway 侧同一约定——沙箱拿不到本进程出生身份，测试对 `capture_process_birth_token`
  打桩，其余字段走真实校验器与真实 Store。

## gateway stop 的遗留后台进程事实与停止入口（2026-09-28，分支 `my-agent/self-dev-4`）

- **来源**：my-agent-4 开发交流板任务 3（dsh-be 的 R10 验收观察）。Gateway 退出不停止托管后台进程是设计行为，
  但用户既看不到遗留进程、也没有回收入口（/interrupt 后再 /stop 只会得到"当前没有运行中的内容"）。
- **做法**：新增 `agent_py_agent/agent/gateway_parts/background_resource_report.py`——只读 `ProcessSessionStore`
  （由 `process_session_store_root(workspace, owner_home)` 单一口径算出，与写入端同源），把记录投影成结构化事实
  （session/status/pid/host_pid/started_at 与所属 thread/root task/run/attempt；命令正文、cwd、输出路径不进投影）；
  停止复用既有 `request_stop`（按精确执行身份冻结意图，空目标被拒绝而非通配），实际回收仍由原 host 的
  `terminate_process_tree` 按进程组完成。`gateway stop` 默认只列出并提示"如需一并停止，加 --stop-background"，
  加该参数才停止；`--background-timeout` 控制等待确认秒数；停止未确认时按非零退出码报告。
  `gateway restart` 的停止阶段显式传 `stop_background=False`（重启保留后台资源是设计行为）。
- **为什么选扩展 `gateway stop` 而不是改 TUI 的 /stop**：/stop 是会话级"停止当前回合"入口，语义是回合控制；
  遗留后台资源是 owner 级进程事实，归 `gateway stop` 更贴切，也避免让 TUI 承担跨会话的资源回收。
- **新测试**：`agent_py_agent/tests/test_gateway_stop_background_resources.py` 6 项——只列运行中记录（终态被排除）、
  投影不含命令正文、跨 root task/run 不被一起停、空执行身份被拒绝（不通配）、未进入终态时如实报 stopped=False、
  按进程组停止时孙进程一起结束（真实 fork 出父子孙进程验证）。
- **复现**：`PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_gateway_stop_background_resources.py -q`
- **变异验证**：去掉 `target.matches(target)` 的执行身份校验 → 空目标用例变红（返回 still_running_after_request
  而不是 scope_incomplete，即变成了通配停止）。
- **环境说明**：本机沙箱拿不到本进程出生身份（`ps -p` 不可用），测试按"fake 进程"约定给 `capture_process_birth_token`
  打桩，其余字段仍走真实校验器与真实 Store。`test_background_handoff.py` / `test_background_stdio.py` 在**基线提交**
  上同样失败（需要在沙箱内真实 spawn 托管 host），与本改动无关，已用基线工作树对照确认。
## list_agents 显式 run_id 的范围裁决（2026-09-28，分支 `my-agent/self-dev-4`）

- **来源**：my-agent-4 开发交流板任务 2（Claude 会话 dsh-9b 的 R16 跨 owner 隔离验收随附发现）。隔离本身通过，
  但显式传入超出当前 owner 可见范围的 run_id 时，`list_agents` 只返回 `nodes=[]`、`root_id=""`，`scope_resolution`
  还把请求的 id 回显成 `effective` 范围，没有任何范围告警或拒绝码。
- **做法**：`agent_tree/status.py` 在显式 run_id 于整棵可见树无匹配行时（且不是 main run、当前无子 runner 身份），
  把查询折成 `root_tree` 并追加唯一裁决码 `requested_run_id_not_in_visible_scope`；请求的 id 只留在 `explicit`。
  `orchestration/scope_resolution.py` 的 `scope_warnings` 恒为列表；`agent_tree/model_view.py` 转发该顶层字段。
  "不存在"和"无权看"共用同一分支、同一个码、同一响应形状，不泄露目标是否存在。
- **新测试**：`agent_py_agent/tests/test_list_agents_scope_resolution.py`（同 owner 可见且无告警；跨 owner 空+告警；
  run_id 不存在也空+同样告警，并断言答复里不含目标 id）。
- **复现**：`PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_list_agents_scope_resolution.py -q`
  （改前 2 条红）。回归 `test_agent_tree_model_view.py` / `test_agent_tree_three_layer_status.py` /
  `test_orchestration_tools.py` 全过。
- **变异验证**：摘掉 `status.py` 的新判定后，跨 owner 与不存在两条用例重新变红。
- 同类静默清单（`task_progress`、`cancel_subagents`）见交流板；该项已转由 my-agent-2 统一收口。

## Jev curator invalid_input 快速失败修复（2026-09-28，分支 `my-agent/self-dev-4`）

- **node 与 bun 的差别**：没有差别。同一基线上 `node frontend/scripts/sync-backend-config.mjs` 与 `bun …` 生成的目录逐字相同，
  `--check` 两种都通过；脚本只读仓库里三份 YAML，用自带解析器，没有环境、排序或 locale 依赖。“大量无关差异”其实是目录自
  `8ba5bf013`（278 项）以来没再生成：相对它，新目录只有 C 组删掉的 14 个键、其后 196 个字段的全局 `order` 顺移（77 个组内位置
  变化）；说明没有变化。
- **孤立分组标题**：杂项批 `35259e86c` 删掉 `conversation_pending_wake_limit` 时，留下的分组标题注释与
  `background_context_max_total_tokens` 自己的注释连成一段；前端生成器（空行不打断注释）与后端
  `parameter_registry._descriptions_from_lines` 都把整段并进说明。第二个提交按集成者决定删掉这段标题：后端 222 个键里只有这一个
  键的说明变回它自己那句，目录相对 `8ba5bf013` 的说明变化归零；说明基线是空说明名单、不含此键，未改。只加空行不行：
  后端会恢复，前端生成器不会。
- **设置页**：14 个键里只有 `memory_resume_auto_context_limit` 还有表单项（`SettingsMemory.tsx`），已删；store 默认值暂留
  （沿 `3af7c94df`）。
- **验证**：两个提交后各跑一次，`node … --check` 与 `bun … --check` 均 rc=0；参数注册表与说明基线等 55 个相关测试文件
  1075 passed；`bun build frontend/src/main.tsx --packages external --target browser`
  rc=0（`@apply` 为既有 CSS 提示）；Bun TSX 转译器转译 `frontend/src` 下 47 个 TS/TSX 全部通过；静态门禁 ruff、doc_sync、
  strict code_size、diff --check、clean_package。

## 能力配置随包模板与 dataclass 逐项一致，删除 12 个死字段（2026-09-28，分支 `claude/9a-lark-and-capcfg`）

- **起因**：
  - G07 验收发现，Gateway 只读 agent 根目录下的 `config/capability_config.yaml`（没有指定工作区时就是 owner home），找不到文件时用 dataclass 默认值；而 daemon、subagents 等 CLI 子命令默认读随包模板。
  - 对齐前，模板比 dataclass 少 16 个字段，`enable_capability_routing` 在模板里是 true、在 dataclass 里是 False。
- **改动**：
  - 模板补上 5 个在用的键。
  - 删除 12 个运行时没有效果的字段：10 个没有读取方；`enable_capability_routing` 不控制能力申请链路；`subagent_min_evidence_for_done` 只被透传进巡检快照，从未使用。
  - 读取逻辑不变。
- **测试**：
  - `test_capability_config.py::TestShippedCapabilityTemplate` 锁定：模板里每个键都被认识，值等于 dataclass 默认值，加载无告警；dataclass 的每个字段都在模板里。
  - `test_capability_config_unknown_keys.py` 对 12 个已删键做参数化：用户配置里残留时只告警、照常加载。
  - 变异验证做了三个：改模板里的一个值、加一个未知键、给 dataclass 加一个模板里没有的字段。三个都被测试抓住。
- **验证**：能力配置相关测试、`test_parameter_registry`、架构护栏、`constant_names_unique`，以及五项静态 gate，结果见交接回报。前端目录已用 `node frontend/scripts/sync-backend-config.mjs` 重新生成，`--check` 通过。

## 飞书启停用例改用 webhook，消除 shard-1 退出时的 lark ExpiringCache 回溯（2026-09-28，分支 `claude/9a-lark-and-capcfg`）

- **原因**：
  - `test_adapter_feishu.py` 里 `TestFeishuLifecycle` 的两个用例只给了空的 app_id/secret，默认走长连接。
  - daemon 线程第一次导入 lark_oapi 时会新建一个模块级 loop；`lark.ws.Client` 的 `ExpiringCache` 在这个 loop 上挂了一个清理任务。
  - `stop()` 关不掉 lark 客户端，这个 loop 从没被关闭。进程退出时先报 "Task was destroyed but it is pending!"，再报
    "RuntimeError: Event loop is closed"。
  - Docker 分片按文件大小降序、`n % 12` 轮转分配，这个文件落在 shard-1。
- **改动**：
  - 两个用例的 config 加 `"feishu_connection_mode": "webhook"`。它们本意就是测 webhook 回调服务的启停（`callback_port=0`）。
  - 长连接分支的选择仍由 `test_adapter_feishu_ws.py::test_adapter_connection_mode_selects_long_or_webhook` 覆盖。
  - 不改产品代码。
- **验证**：
  - 本机跑这两个文件：38 passed。
  - `my-agent-linux-test:py312`（`--network none`）加一个"pytest 收尾时等 5 秒"的临时插件，模拟整片运行时进程存活更久：
    - 修改前：29 passed 之后出现上述两段回溯；
    - 修改后：38 passed，lark 相关报错行为 0。
  - 复现材料在 `~/.my-agent/decision-evidence/lark-expiring-cache-20260928/`。

## 子代理页头部标出“已被 X 接替”（2026-09-28，分支 `claude/ae-tui-superseded-marker` 第 3 个提交）

- **来源**：集成方要求进入被接替的子代理页后，头部也标出接替者；数据同样取 `replaced_by_view`。CLI board 按决定不做。
- **实现路径**：
  - 导航行白名单 `tui_agent_navigation._ROW_SCALAR_FIELDS` 放行 `replaced_by_run_id`／`replaced_by_disposition`；
  - 导航快照新增 `active_replaced_by_run_id`；
  - 渲染上下文工厂（`tui_ui_setup._make_render_context_factory`）把它传成 `TuiRenderContext.focused_agent_replaced_by`，
    该字段放在末尾，并进入 `tui_render_context_key`；
  - 终态头部标签复用名册的 `_with_replacement_marker`。
- **新增 3 项**（`test_tui_superseded_marker.py` 现共 7 项）：
  - 真实接替后的名册行进入导航，进入来源子代理页，经真实上下文工厂渲染，100 列和 50 列下头部都有
    “已完成 · 已被 X 接替”，快照与上下文都带出接替者；
  - 渲染缓存键随接替者变化。
- **变异**：5 个变异都变红：导航白名单漏字段、快照不带接替者、上下文工厂不传、头部不标、缓存键漏字段。
- **回归**：定向 69 个文件：引用导航、渲染器、上下文工厂、渲染上下文与缓存键的 TUI 测试，加上全仓扫描类测试和读文档的测试。结果 1368 passed、1 skipped，229.2 秒；没有跑全仓 pytest。
- **门禁**：ruff、doc_sync、strict code-size、`git diff --check`、check_clean_package 全部退出 0，CODE_SIZE_REPORT.md 在提交前已还原。

## TUI 子代理名册标出“已被 X 接替”（2026-09-28，分支 `claude/ae-tui-superseded-marker` 第 2 个提交）

- **来源**：集成方要求对被接替的 DONE 子代理在 TUI 展示面标出“已被 X 接替”，数据取 kernel／视图里的 replaced_by，
  不另外推断。
- **实现路径**：
  - `kernel.replaced_by_view` 是唯一投影；
  - Gateway 名册行（`conversation/agent_activity._subagent_row`）把它摊平成 `replaced_by_run_id`／`replaced_by_disposition`，
    因为 TUI 白名单只收标量；
  - TUI 的 runtime 白名单（`tui_runtime._SUBAGENT_ACTIVITY_FIELDS`）与视图模型白名单（`tui_view_model._PUBLIC_SUBAGENT_FIELDS`）
    放行这两个字段；
  - 渲染器 `_render_subagent_activity_row` 在状态标签后加“已被 <接替者 run_id 末段> 接替”，标注与状态一起预留宽度。
- **新增 `test_tui_superseded_marker.py`（4 项）**：
  - Gateway 行对 DONE 与 BLOCKED 来源摊平出接替者与处置，接替者本身的行不带这两个字段；
  - 真实接替后的两行经 `TuiRuntime.update_background_activity` 进入渲染，只有来源行出现“已完成 · 已被 X 接替”；
  - 72 列窄屏下标注仍在。
- **变异**：5 个变异都变红：Gateway 行不带字段、kernel 投影为空、runtime 白名单漏字段、视图模型白名单漏字段、
  渲染器不标。
- **未做**：CLI `subagents board` 仍只带 takeover_by，没有标 superseded。
- **回归**：定向 143 个文件：引用 TUI 渲染器、运行时、视图模型、agent_activity、kernel、代理树与 list_agents 的测试，加上全仓扫描类测试和读文档的测试。结果 3069 passed、1 skipped、21 xfailed，443.9 秒；没有跑全仓 pytest。
- **门禁**：ruff、doc_sync、strict code-size、`git diff --check`、check_clean_package 全部退出 0，CODE_SIZE_REPORT.md 在提交前已还原。

## 保存事件带上接替关系（2026-09-28，分支 `claude/ae-tui-superseded-marker` 第 1 个提交，基于 `3e58de8a0`）

- **来源**：修复 `1d44c9f8f` 的脚本模型复核发现，追加式事件日志里看不到接替关系。集成方定的做法是不新增事件类型，
  只在 `subagent_run_saved` 的 payload 里带上 takeover_by／superseded_by，有值才带。
- **测试**：`test_subagent_done_supersede.py` 新增 2 例（参数化）。用真实 LocalStore 挂到 SubAgentManager 上：
  - 接替前的保存事件都不含这两个键；
  - 接替 DONE 来源后，最新一条保存事件只带 `superseded_by`；接替 BLOCKED 来源后，只带 `takeover_by`。
- **变异**：2 个变异都变红：不带任何接替键；无论有没有值都带上两个键。
- **回归**：11 个文件，包括新测试、本地存储与 local_doctor 相关测试、test_architecture_guardrails 和 test_constant_names_unique；结果 146 passed。
- **门禁**：ruff、doc_sync、strict code-size、`git diff --check`、check_clean_package 全部退出 0，CODE_SIZE_REPORT.md 在提交前已还原。

## 会话间消息与派活第 2 片：TUI 命令 /tell 与 /sessions threads（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：开发交流板第 2 片（TUI 命令：列会话、发消息）。dev 指出 `/sessions`（SessionManager 的 CLI 恢复记录）
  与会话消息目标 `ConversationThread` 不是同一套，需要处理映射。
- **做法**：
  - `command_catalog.py` 新增 `tell` 命令声明（`/tell <目标会话> <消息>`），并给 `/sessions` 加
    `/sessions threads` 用法变体；帮助文案自动带上。
  - `slash_commands.py` 新增 `_handle_tell_command` + `_tell_message` + `_thread_channel` + `_print_message_targets`；
    注册进 `handlers` 元组。
  - **映射处理**：`/sessions`（无参数）保持原样，仍列 SessionManager 的 CLI 恢复记录；
    新增 `/sessions threads` 专门列 canonical `ConversationThread`（消息目标的权威），
    两套记录不混用，也不再从会话正文猜标题。
  - `/tell` 与模型工具**共用同一权限判定与投递语义**：读 `home_paths` 结构化身份 → 权限判定
    → `guidance.append_once` 幂等入队 → 目标 active 时 `wake.raise_signal`；
    失败返回带稳定错误码的文案（`SESSION_IDENTITY_UNAVAILABLE` / `SESSION_MESSAGING_DISABLED` /
    `SESSION_TARGET_OUT_OF_SCOPE` / `SESSION_TARGET_CHANNEL_UNSUPPORTED`）。
- **新测试**：`test_tell_command.py`（8 项）——命令已声明、缺参数给用法、身份缺失 fail closed、
  开关关闭返回 DISABLED、目标不存在返回越界码、成功入队并唤醒、IM 目标拒绝、`/sessions threads` 列目标。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_tell_command.py agent_py_agent/tests/test_session_messaging_permissions.py agent_py_agent/tests/test_send_session_message_tool.py agent_py_agent/tests/test_session_message_rendering.py agent_py_agent/tests/test_recovery_code_policy.py -q`（66 项）。
- **测试设计踩坑**：`capability_config_for_agent` 只认真正的 `CapabilityConfig` 实例，替身对象会被跳过并回落到
  真实文件加载（于是测试改开关无效）；测试改用真正的 `CapabilityConfig` 后恢复正常。
- **未完成**（如实标注）：dev 要求的"目标 TUI 显示收到的消息/任务及来源、发送方 TUI 显示投递与任务状态"
  中，**显示部分只在 /tell 回执里做了发送方一侧**；接收方 TUI 的展示与任务状态显示要等第 3 片（派任务）一起做。
- 五项静态 gate（ruff/doc_sync/strict code-size/diff --check）通过。

## 参数减量杂项批：compact 语义摘要 4 键 + 唤醒消费/合并窗口 2 键降为常量（2026-09-28，分支 `my-agent/self-dev-2`）

- **来源/做法**：dev 在 my-agent-2 开发交流板派的任务 3（做法照 `87e025bb6`）。
- **compact 语义摘要 4 键**：`memory_compact_semantic_summary_protect_head`(2)、`_protect_tail`(6)、`_min_middle`(4)、
  `_max_input_chars`(12000) 直接复用 `agent/memory_archive/compact_semantic_summary.py` 里既有的 `_DEFAULT_*` 常量；
  `semantic_summary_config` 只从 config 读 `enabled`，配套删掉不再使用的 `_int_field`。
- **唤醒消费与合并窗口 2 键**：`conversation_pending_wake_limit`(100)、`background_completion_coalesce_seconds`(5) 降为
  `agent/conversation/runtime.py` 的 `PENDING_WAKE_CONSUME_LIMIT` / `COMPLETION_COALESCE_WINDOW_SECONDS`；消费上限 0 表示不限、
  合并窗口 0 表示不合并的原语义不变，只替换读这两个键的三行（`_consume_pending_wake_signals`、
  `_successful_completion_waiting_for_batch`、`_enqueue_scheduler_runs`），没有重构唤醒逻辑。
- **配置面同步删除**：`AgentConfig` 6 字段、随包 `agent_config.yaml` 6 行与相应注释、`services/_normalize` 6 条规格、说明基线 3 个条目。
- **测试改动**：`test_compact_semantic_summary.py` 的配置读取用例改成「只有 enabled 生效」；`test_runtime_parameter_config.py`
  删除该键归一化用例；`test_background_main_agent_runtime.py` 三处改为用默认常量或直接 patch `COMPLETION_COALESCE_WINDOW_SECONDS`（0/30 秒场景）。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_compact_semantic_summary.py tests/test_runtime_parameter_config.py
  tests/test_background_main_agent_runtime.py tests/test_memory_config.py tests/test_settings_memory.py tests/test_parameter_registry.py
  tests/test_constant_names_unique.py tests/test_architecture_guardrails.py tests/test_config_field_readers.py -q --tb=short`（396 passed）；
  仓库根跑 Ruff、`scripts/check_doc_sync.py`、`scripts/check_code_size.py --mode strict`、`git diff --check`、`scripts/check_clean_package.py .`。

## 能力包 G05 补测：跨回合 Goal 恢复与工具操作 UNKNOWN（2026-09-28，被测 `9f88e4905`，仅文档）

- **结论**：脚本模型端到端机制已验，真实模型未覆盖。两项都通过，结果表见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#g05补测跨回合-goal-恢复与工具操作-unknown2026-09-28)。
- **方法**：沿用上一条 G05 的隔离环境（8438/8448、`env -i`、不设模型密钥、两个真实 TUI）。
  - Goal 场景：脚本模型 `create_goal`，读取并 pin v1 后标记 blocked；用 `/plugins` 更新到 v2；用户执行 `/goal resume`。
  - UNKNOWN 场景：测试侧暂停点放在 `write_file(source_ref)` 的 handler 返回之后，然后 SIGKILL 本测试 Gateway；重启后按产品提示用 `/recover` 查询，再选 `/recover recorded`。
- **判据**：只看结构化事实，包括 Goal/task 身份与 `task_run.reopened`、pin 与 get 回执码、hook 记录的 write handler 进入次数、文件 mtime、操作表行数与状态、attempt 状态序列、阻塞期间的模型调用次数。
- **附带实测**：普通 `write_file(content)` 在 ask 下也不弹审批，与 README 权限模式表的说明一致。
- **观察**：脚本模型在同一续跑轮里重复同一个失败的 get，共 393 个模型轮，宿主只给提示、没有硬停，最后由测试者暂停。这个问题留给产品线判断，本条不改产品。证据在 `~/.my-agent/decision-evidence/g05-followup-9f88/`（仓库外）。

## C25 空选后能力包对主线程的可见性核对（2026-09-28，分支 `claude/38-empty-selection-visibility`，基于 `b1d382307`，只改文档）

- 只读核对固定 `9f88e4905`：主线程快照来自 owner 全部已启用包（`core.py:514-520`、`skill_service.py:87-109`），`skill_snapshot_for_run_scope`（`core.py:531-555`）对主线程只剔除失效 pin，不按选择裁剪；`_commit_selection` 空选只写回执，不写 pins、不改工具面；`skill_search` search/get 无 pin 前置，get 成功即写 pin（`package_read.py:52-105`、`task_references.py:120-154`）。
- 本地合同测试 2 项通过（真实 `SimpleAgent` + 安装表，假 backend 空选后 search 命中两包、get 成功并 pin；直接 get 亦 pin）；文件不入仓，保存在证据目录。
- 8435 脚本模型端到端：选择请求答 `{"selected_ids": []}`，任务 `outcome=empty`；主轮 search 命中两包，按 `next_read` get 到 B 入口 2651 字符并写 pin；工具账 2/2 成功。首个请求因假模型未答工具探针失败（`TOOL_PROTOCOL_CAPABILITY_UNAVAILABLE`），修正后重开会话一次提交。
- 结论 (a)：空选后包仍可按需发现、读取、pin，C24 是模型行为，不是设计问题；无产品改动。详见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c25空选后能力包对主线程的可见性只读核对与脚本复现2026-09-28)，证据在 `~/.my-agent/decision-evidence/empty-selection-visibility-9f88/`。
- 本轮 doc sync、`git diff --check`、clean-package 按纯文档范围执行；不跑仓内 pytest，线上 CI 未作来源。建议下一步：与 C24 一起合入；空选再现时按模型行为记账，不改提示凑证据。

## C24固定9f88的131072自然长任务两代压缩（2026-09-28，分支 `claude/38-compact-131072-acceptance`，基于 `60f6f485a`，只改文档）

- 被测 `9f88e4905`（与生产 step14w 产品代码相同）由 git archive 构建 wheel `d3a6e0fd…` 装进新 venv；隔离 home、owner local/main、私有 Gateway 127.0.0.1:8435；catalog 沿用 C21/C22 那份副本（600、未打印、跑完删除），只由脚本把 `selected` profile 窗口 262144 → 131072；A0.3.0/B0.1.4 用原生 `/plugins install`/`enable` 装入，安装账本运行前后一致。
- 只提交一次 C21 原需求（提交正文 sha256 `888080a2…` 与 C21 记录相同），request `gwreq-1790614047-a53daa92…`，1672.347 秒自然 `done`；86 次物理模型请求、0 重试/失败/超时。
- 两代自动 Compact 均提交并成链：第 1 代 88347 → 39157，第 2 代 89492 → 46017，进度事件 `trigger_source=tool_context_overflow`，线程 head `compact_generation=2`；可见估算峰值 89492 未越 117964，与 C21 同类触发。
- 压缩后原资源读取/执行未覆盖：包选择 `outcome=empty`、全程 0 次 `skill_search`，提交后 9 次 `run_command` 只跑模型自建程序；五份输入保持。业务判“未通过”只据“未原样使用原检查程序”这一结构化事实，其余业务质量未独立审阅。
- 本轮无产品/测试/配置改动，不跑 pytest；doc sync、`git diff --check`、clean-package 按纯文档范围执行，线上 CI 未作来源。详见[C24 分项](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c24固定9f88的131072自然长任务两代压缩2026-09-28)，证据在 `~/.my-agent/decision-evidence/compact-131072-9f88/`。建议下一步：由整合者核对后合入文档；下一次自然任务先核包选择非空再判压缩后原资源链，不改提示或阈值凑证据；产品作者可只读核对空选后包对主线程的可见性。

## Jev 后台点位改用独立期限（2026-09-28，分支 `claude/be-jev-bg-deadline`，基于 `60f6f485a`）

- **来源**：my-agent-1 实测近 48 小时，后台点位超时 39%，前台 22%。
  - 根因：`decision_service._decide_outcome` 的点位期限是 `min(stage.deadline, 点位开始 + 点位预算, 调用方期限)`。
  - 后果：同一后台阶段里，排在后面的 `curator_relation` 只拿到阶段倒计时的残值，19 次超时的中位耗时只有 1930ms。
- **改动**：
  - `_point_deadline`：普通后台（owner_background）阶段不再并入 `stage.deadline`，点位期限 = 点位开始 + 点位预算，再与调用方期限取更小。
    调用方期限有两个：建阶段时给的（新字段 `DecisionStage.caller_deadline`）和本次调用给的。前台与实验阶段仍并入阶段上限，公式不变。
  - `_adoption_deadline`：采用前复核同口径，普通后台建议只看自带的点位期限；前台与实验阶段仍取阶段上限与点位期限的较小值。
  - 设置视图：后台点位的 `max_request_seconds`、`limiting_field` 改为点位自己的预算，与服务一致。没有改任何设置值。
- **测试**：新增 `test_decision_background_deadline.py`，共 6 项，用假时钟加假调用边界记录发送期限：
  - 后台：阶段倒计时只剩 2 秒时，第二个点位仍拿到完整 8 秒（发送期限 114，不是 108）。它的建议在阶段预算到点后、点位期限前仍可采用；
    第一个点位的建议按它自己的期限到期。
  - 前台：点位 5 秒仍被阶段上限截到 101，采用期限不超过阶段上限。
  - 调用方期限：建阶段时给的期限（如租约）对后面的点位同样有效；单次调用给的更小时取更小，前台同样。
  - 纯函数矩阵：只有普通后台阶段去掉阶段上限；实验后台阶段与前台阶段保留。
  - 既有 `test_decision_settings_scope.py` 的两项按新语义改写：点位覆盖 12 秒、后台缺省 8 秒时，投影和服务都按 12 秒（原来被 8 秒封顶）。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_decision_background_deadline.py agent_py_agent/tests/test_decision_settings_scope.py -q`。
- **变异验证**：12 个全部抓出，每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，跑完逐字节恢复并核对哈希。
  - 期限口径：后台仍受阶段上限；前台丢掉阶段上限；采用仍用旧口径；后台采用忽略点位期限。
  - 调用方期限：丢掉阶段级期限；丢掉调用级期限；阶段不保存调用方期限。
  - 实验阶段：实验后台阶段的发送、采用各丢掉阶段上限。
  - 接线：投影仍按后台阶段封顶；decide 调用点仍用旧公式；采用门调用点仍用旧公式。
- **相关回归**：rebase 到 `60f6f485a` 后重跑，126 个测试文件 2677 passed、1 skipped（rebase 前 98 个文件 2158 passed、1 skipped）。
  清单包括所有涉及决策服务、设置、阶段、curator 的测试，以及全部扫描产品代码的守卫测试
  （含 `test_architecture_guardrails.py`、`test_constant_names_unique.py`）。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package 均通过；code-size 身份差集相对基线新增 0、减少 0。

## 子代理同一失败调用的回合硬上限（2026-09-28，分支 `claude/75-repeat-failure-halt-subagent`，实现提交 `4aadedf79`）

- **依赖**：基于主代理切片 `b81b6f938`、`40df0c86f`，这两个提交还没进 main。
- **合同单测与 fake LLM**：新增 `agent_py_agent/tests/test_subagent_identical_failure_halt.py`，共 6 项，另改写主代理测试里原先“跳过子代理”的那一项。
  - 优先级：经 `_mark_tool_call_halts` 的真实顺序，同一个授权阶段失败调用同时满足两种条件时，按授权阶段收口；非授权失败按同调用收口。
  - 子代理收口回复为 blocked，结束原因为 blocked，由宿主自写，不调模型。
  - finalize 从归档同口径复算收口事实：原因码、工具、错误码、阶段、次数、参数名；末尾已经成功时不带事实。
  - 唤醒摘要按 `reason_code` 区分说法；非 BLOCKED 状态不附带。
  - 真实子代理 runner 里，同一个工作区内缺失文件的 `read_file` 在阈值 3 时模型只被调用 3 次；结果为 BLOCKED，账本 halt 与 wake metadata 一致，观察摘要写明原因码，不带参数值。
- **变异**：7 个全部被杀：子代理重新被跳过、优先级对调、子代理收口改成 unfinished、finalize 忽略该原因、唤醒说法不看原因码、归档复算不因成功清零、子代理文案改成等用户。
- **定向回归**：上一条的全部范围，加上子代理生命周期、runner 结果、工具失败账本与连续段、运行引导与转发等测试，共 6126 项通过、4 项原有 xfail。
- **脚本模型端到端**：被测代码是 `4aadedf79` 的 git archive。真实 Gateway 8438 加真实 TUI，脚本模型 8448，`env -i` 启动，不使用模型密钥。
  - 父代理用 `create_subagents` 派出一个孩子，孩子每轮原样发同一个失败的 `skill_search get`。
  - 孩子的模型请求恰好 15 次（默认阈值）后收口。父级只被唤醒一次：reason 为 `subagent_runner_finished`，status 为 BLOCKED，turn_end 为 blocked。
  - wake metadata 的 `tool_failure_halt` 为：`reason_code=REPEATED_IDENTICAL_TOOL_FAILURE`、工具 `skill_search`、错误码 `SKILL_SNAPSHOT_UNAVAILABLE`、阶段 `execution`、次数 15、参数名 `action`/`package_id`。摘要写明“以相同参数反复调用”。
  - 之后 40 秒内没有新的模型请求，孩子没有被重派。
  - 证据在 `~/.my-agent/decision-evidence/identical-failure-halt-subagent-4aad/`（仓库外）。
- **静态 gate**：ruff、doc_sync（已同步子代理模块文档）、strict code-size（相对 `b2bfa75af` 没有新的 finding 身份）、`git diff --check`、clean_package 全部通过。真实模型下的效果没有验证。

## 主代理同一失败调用的回合硬上限（2026-09-28，分支 `claude/75-repeat-failure-halt`，实现提交 `b81b6f938`）

- **合同单测与 fake LLM**：新增 `agent_py_agent/tests/test_identical_tool_failure_halt.py`，共 14 项。
  - 连续段只在三元组（工具名、参数摘要、错误码）完全相同时累加；换任一项或成功都清零。
  - 第 N 次恰好命中；同批后到的成功撤销收口；阈值 0 关闭；`task_local` 子代理跳过；原强返工提示不清掉收口。
  - 收口为 `unfinished`＋`REPEATED_IDENTICAL_TOOL_FAILURE`：不可续跑，结束原因为 `interrupted`，任务不收成完成。
  - 主代理路径读到默认阈值 15。
  - 真实 `SimpleAgent` 工具循环里，同一 `read_file` 连续失败时模型被调用 15 次就结束，没有额外的收口调用；换参数或中间成功一次的 20 轮都正常收尾。
- **变异**：10 个全部被杀，包括：不调用标记、阈值差一、成功不清零、忽略参数、忽略错误码、同批成功不撤销、原因被列为可续跑、不跳过子代理、收口改回调模型、状态改成 ok。
- **定向回归**：工具循环、收口、Goal、guardrail、运行门配置和参数相关测试，加 `test_architecture_guardrails`、`test_constant_names_unique`、`test_code_size_script`，共 5971 项通过。其中有一个旧测试的替身缺少新字段，所以实现改用 `getattr` 读可选字段。
- **脚本模型端到端**：真实 Gateway 8438 加真实 TUI，脚本模型 8448，`env -i` 启动，不使用模型密钥。被测代码是 `b81b6f938` 的 git archive 导出。
  - H1 前台：同一个失败的 `skill_search get` 模型被调用 15 次后结束。请求记录为 `runtime_status=unfinished`、`runtime_reason=REPEATED_IDENTICAL_TOOL_FAILURE`、`turn_end_reason=interrupted`、`tool_rounds=15`，TUI 显示宿主的收口说明。
  - H2：换 20 个不同参数后正常 completed，没有被拦。
  - H3：第一轮 `create_goal` 后正常结束；宿主排了 1 次 Goal 续跑，续跑回合同一失败 15 次后以 interrupted 结束，收口说明作为后台消息送达。随后 90 秒内模型请求为 0，唤醒队列仍只有那 1 条，Goal 与任务都保持 active。
  - H3B：用户再发一条消息，新回合从 0 计数，15 次后再次结束；之后 45 秒内同样没有自动续跑。
  - 证据在 `~/.my-agent/decision-evidence/identical-failure-halt-b81b/`（仓库外）。
- **静态 gate**：ruff、doc_sync、strict code-size（与基线相比没有新的 finding 身份）、`git diff --check`、clean_package 全部通过。真实模型下的效果没有验证。

## R16 补测：按包选择偏好与 global_index 可读性（2026-09-28，被测 `9f88e4905`，文档分支 `claude/9b-r16-followup`）

- **范围**：上一轮 R16 留下的两项：跨 owner 的按包选择偏好（`host_capability_selection.v1`），以及主机层
  `global_index`（active_runs、active_tasks）能否被普通用户的工具面读到。
- **环境**：与 R16 相同；另在两个 owner 的能力配置里打开默认关闭的一次选包。
- **结果**：两项都通过，未发现隔离缺陷，逐项证据见
  [CAPABILITY_PACK_ACCEPTANCE.md](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#r16-补测按包选择偏好与-global_index-可读性2026-09-28)。
  A 形成了一条选中 1 个包的选择标记，只存在 A 这边；B 没有包、没有任何选择标记。B 的 `list_agents`、`audit_records`、
  `schedule` 输出都不含 A 的标识，跨用户审计返回 `AUDIT_ACCESS_DENIED`，直接读索引返回 `PATH_OWNER_SCOPE_BLOCKED`，
  inspect 类工具和 `gateway_status` 不在普通用户的工具面上。
- **复现**：`~/.my-agent/decision-evidence/r16-followup-9f88/harness/`（stage_b → run_gateway / run_tui → send →
  selection / canary / scan）；判定只读结构化标记、错误码和标识命中计数，不读正文。

## 能力包读取失败的结构化原因（2026-09-28，分支 `claude/75-snapshot-reason`，基于 `53477806d`）

- **背景**：G05 脚本模型端到端中，在途切代和普通停用都只返回 `CAPABILITY_RESOURCE_UNAVAILABLE`。原因是 `read_in_package` 读的是 `exc.code`，而 `PluginInstallationError` 只有 `reason`，内部原因因此丢失。
- **改动**：`read_capability_member` 在读后复核失败时（撤销或代次变化）改报 `activation_changed_during_read`，读前失效仍是 `activation_unavailable`。`SkillSnapshotError` 增加可选的 `reason`。`skill_search` 的失败回执保持 `SKILL_SNAPSHOT_UNAVAILABLE` 和原 `error` 文本，另带 `details.reason`。
- **测试**：`test_capability_package_discovery.py` 新增 2 项，分别在读取器层和工具层区分读前失效与读中撤销；两项先红后绿。能力包、快照、Skill 服务相关的 18 个文件，加上仓库级守卫（架构、常量唯一、代码尺寸、guardrail）共 459 项通过。
- **变异**：5 个变异全部被杀：读后仍报旧原因、读前误标为读中、`read_in_package` 丢掉 reason、工具不输出 details、去掉读后复核的包装。
- 没有覆盖的路径：`write_file.source_ref` 的来源失败仍以 `TOOL_UNAVAILABLE` 的文本报告，没有带出 reason。

## 参数减量 C 组第 3 批：Curator 批大小与重试 2 键降为常量（2026-09-28，分支 `my-agent/self-dev-2`）

- **来源/做法**：dev 在 my-agent-2 开发交流板派的任务 2（做法同 C 组第 2 批 / `87e025bb6`）。`memory_curator_batch_message_limit`(80)
  与 `memory_curator_max_retries`(1) 数值不变，降为 `agent/memory_store/curator_models.py` 的 `CURATOR_BATCH_MESSAGE_LIMIT` /
  `CURATOR_MAX_RETRIES`；`MemoryCuratorConfig` 默认值改为这两个常量，`from_agent_config` 不再读 `agent.config`。
- **保留项**：curator 的 interval、turn_threshold、max_input_chars、timeout、workers、daily_finalize_hour 以及
  `memory_rule_auto_read_limit` 仍是用户参数，本批未动。
- **配置面同步删除**：`AgentConfig` 2 字段、随包 `agent_config.yaml` 2 行、`services/_normalize` 的 2 条 `_FieldSpec`、
  `_memory_types.MemorySettings` 2 字段、说明基线 2 个条目；用户配置残留只按未知键告警。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_memory_curator_v2.py tests/test_curator_*.py tests/test_collect_curator_evidence.py
  tests/test_decision_curator*.py tests/test_memory_config.py tests/test_settings_memory.py tests/test_parameter_registry.py
  tests/test_constant_names_unique.py tests/test_architecture_guardrails.py tests/test_config_field_readers.py -q --tb=short`（369 passed）；
  仓库根跑 Ruff、`scripts/check_doc_sync.py`、`scripts/check_code_size.py --mode strict`、`git diff --check`、`scripts/check_clean_package.py .`。
- **环境注记**：整目录 `pytest tests` 在本机因缺 dev extra（`hypothesis`、`pyte`）无法收集，本次用显式文件列表；与本批改动无关。

## R10 深度切片：长前台工具中途中断与 `/goal resume` 对照（2026-09-28，分支 `claude/be-r10-depth`，基于 `53477806d`，只改文档）

- **来源**：补 R10 实测留下的两项未覆盖，环境与判定方式同上一轮（`9f88e4905` 的 wheel、8434、Codex 那份官方 MiniMax-M2.7 档案、`env -i`）。
  详见 `docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md` 的“R10深度切片”一节。
- **长前台工具执行中途 `/interrupt`：通过**。就绪条件为本任务后台进程存活 7 秒以上，其后的前台 run_command 已执行 3 秒以上。
  中断后请求 interrupted，run 与 attempt cancelled；执行中的工具记为 UNKNOWN、未结算，也没有残留的 sleep 进程；此后没有新的工具操作。
  本任务后台进程仍 running、`stop_requested=false`，心跳继续增长。
- **`/goal resume` 对照：通过**。在原会话恢复上一轮暂停的目标后，目标回到 active，同一 agent_run 起了第 2 代 attempt，并实际执行了工具。
  随后用已验证的 `/goal pause` 截住后续续跑，这一步不计入判定。
- **观察**：被中断任务留下的后台进程，在同一会话里用 `/stop` 回收不了（ok=false，“当前没有运行中的内容”），交集成方判断。
- **更正上一轮收尾**：上一轮只结束了 B1、B2 登记的 pid 和 child_pid，写心跳的孙进程一直运行到本轮收尾，才按登记进程组结束。
  残留进程的核对已从按命令行匹配改为按工作目录核对，本轮收尾后测试根目录下没有进程。
- **用量**：8 次主模型调用（MiniMax-M2.7），决策与辅助调用为 0。
- **证据**：`~/.my-agent/decision-evidence/r10-depth-9f88/`（仓库外）。`r10-depth-results.json` 摘要
  `5dc55097ecad5c481f0783bd7d237a028eb51a2ba09c75833fd40971d6462a4e`，`MANIFEST.sha256` 摘要
  `24fe9dfe1671754c8f56036155d06280a00c0cce0cf77a7958fbec60c85416b9`。
- **门禁**：只改文档，跑了 doc sync、`git diff --check`、clean package；没有产品或测试代码变更，不跑 pytest。

## 插件命令被拒时带结构化码（2026-09-28，分支 `claude/9b-plugins-denial-code`，基于 `53477806d`）

- **根因**：普通用户执行仅管理员的 `/plugins` 子命令时，服务层其实返回了 `PLUGIN_PERMISSION_DENIED`（TUI 命令路径和 Gateway 路径
  用的是同一个 `PluginManagement`），但 `_reply` 只把码换成中文说明放进 `message`，TUI 只打印 `message`，Gateway 也不记日志，
  所以面板和日志里都看不到码（R16 实测）。
- **做法**：`plugin_management._outcome_message` 统一生成默认说明句，结果为 rejected（没有开始执行）且带码时在中文说明后另起一行
  “错误码：X”，TUI、IM 与 Gateway 回执共用这一句；执行后的失败与成功文案不变。Gateway 的普通回执与交互命令流两条路径都经
  `plugin_command_service._log_rejection`，对 rejected 结果按 WARNING 记 `PLUGIN_COMMAND_REJECTED error_code=… action=… request_id=…`
  （Gateway 进程不配置日志级别，只有 WARNING 及以上会进 gateway.log），不记命令原文或路径。
- **新测试**：
  - `test_plugin_management.py`：普通用户执行 install、configure、enable、disable、remove、update、status 七个仅管理员子命令，都返回
    同一个 `PLUGIN_PERMISSION_DENIED`，文案带码，且不建 owner 目录（没有安装账本、插件文件或 host_command）；只有 rejected 附码：
    管理员目录过期被拒带码，无效包执行失败（failed）与安装成功都不带。
  - `test_gateway_plugin_management.py`：Gateway 普通路径被拒时回执带码、日志恰好一行 WARNING；管理员安装成功不记。
  - `test_host_command_stream.py`：真实 HTTP 交互路径上业务调用审批被拒，回执带码，日志按同一格式记一行。
  - `test_plugin_command_client.py`：TUI 命令层用真实普通用户 Agent 输入 `/plugins install`，面板打印的就是“中文说明 + 错误码”，
    不产生插件目录。
- **变异验证（9 种全部被抓住，逐个字节级还原）**：去掉码行、所有状态都附码、普通路径不记日志、交互路径不记日志、日志降到 INFO、
  每个结果都记日志、日志回显命令原文、去掉非管理员守卫、TUI 只打印第一行。

## 已结束子代理接替修复的脚本模型端到端复核（2026-09-28，分支 `claude/ae-done-takeover-fix`，仅文档）

- **性质**：脚本模型端到端机制已验，真实模型未覆盖。被测为修复 `1d44c9f8f` 的 `git archive` 导出，用上一轮同一套工具：
  真实 Gateway（8437）、脚本假模型（8447）和 tmux 里的真实 TUI，全部以 `env -i` 启动，假 HOME 跑完为空，不设模型密钥。
- **BR**：唤醒片接替一个已 DONE 的子代理，得到 `recorded`＋`superseded`；来源保持 DONE，`superseded_by` 指向接替者，
  有一条 TakeoverRecord 和 TAKEOVER.md。再对同一来源接替一次，得到 `SUBAGENT_REPLACEMENT_INVALID`／`source_already_taken_over`，
  不产生新子代理。
- **HI**：测试者把 owner 工具索引改为只写 7.568 秒。唤醒片的普通重派先得到 `TOOL_ONE_SHOT_HISTORY_INCOMPLETE`，
  带 `replacement_for_run_ids` 的接替随后放行，结果同 BR；第二次接替同样被拒。
- **LS**：之后的前台 list_agents 对两个来源都给出 `replaced_by`（接替者、superseded）。
- **判据**：只读结构化事实，包括 tool_completed 的错误码与 seq、create_subagents 外置输出的 replacement_records、
  canonical 状态、TAKEOVER.md 是否存在、wake 队列，以及 Gateway 停止后以 immutable 只读打开的 runtime.db；
  另从脚本模型收到的工具结果里抽取拒绝原因和 replaced_by 的结构字段。
- **事件日志**：local_store 与 run 时间线里没有专门的接替条目，已记为后续项。
- **本轮验证**：只改 Markdown；运行五项静态门禁，并在提交前还原 CODE_SIZE_REPORT.md；没有跑 pytest。

## 已结束子代理被接替时终态不改写（2026-09-28，分支 `claude/ae-done-takeover-fix`，基于 `53477806d`）

- **来源**：G03 脚本模型端到端验证（9f88e4905）发现，接替 DONE 子代理时回执报 `recorded`、写了 TAKEOVER.md，但持久化边界把
  TAKEN_OVER 静默还原，canonical 仍为 DONE、takeover_by 为空。语义由集成方定，见 DESIGN_LEDGER 同名条目。
- **新增 `test_subagent_done_supersede.py`（13 项）**：
  - DONE／CANCELLED／ABANDONED 来源被接替后状态不变，`superseded_by` 指向新 child，有一条 TakeoverRecord 和 TAKEOVER.md，
    回执为 `recorded`＋`disposition=superseded`；
  - BLOCKED 来源照旧 TAKEN_OVER，回执 `disposition=taken_over`；
  - 保存被丢弃时，落账入口抛 `TakeoverNotPersistedError` 且不写 TAKEOVER.md；写入入口不报错却没落盘时，回执为 `not_persisted`，
    新 child 被取消；
  - 第二次接替同一来源（DONE 与 BLOCKED 各一例）被预检拒绝：`SUBAGENT_REPLACEMENT_INVALID`、`source_already_taken_over` 带
    接替者与 disposition，不产生新 child；
  - 已关闭记录被不同状态写回时，只保留白名单里新的 superseded_by 及随它追加的 TakeoverRecord；来源读到时还没关闭、
    写入前已收口为 DONE 的接管（输掉竞态）不回报成功，不留悬空接管记录，也不写 TAKEOVER.md；同状态旧快照冲不掉已落盘的
    接替关系；
  - 接管 run 对 DONE 来源同样记 superseded，关掉按来源引用的全量扫描后，重复发起仍按 superseded_by 找回原接管 run；
  - kernel 节点、代理树节点与 list_agents 模型视图都带 `replaced_by`。
- **修改的测试**：
  - `test_orchestration_create_subagents_tool.py`：显式接替回执断言补 `disposition=taken_over`；
  - `test_lifecycle_wake_host_event.py`：场景可指定子代理状态；新增 1 项走真实后台链路的假模型回归。owner 根索引读不到时，
    普通重派被 `TOOL_ONE_SHOT_HISTORY_INCOMPLETE` 拦下，随后写明 `replacement_for_run_ids` 接替 DONE 子代理的派工放行，
    来源仍 DONE 且 `superseded_by` 指向新 child。测试只把后台启动换成桩，不真跑新 child。
- **变异**：15 个变异全部变红，改动文件都按 sha256 逐字节还原，每次使用新的 PYTHONPYCACHEPREFIX。覆盖：
  - 处置恒为 taken_over，以及恒为 superseded；
  - 不写 superseded_by；
  - 落账入口不核对落盘；回执不重读；
  - 预检只读 takeover_by；预检不查接替者；
  - 去掉已关闭记录白名单；白名单不带 superseded_by 也追加接管记录；同状态合并丢 superseded_by；同状态合并丢接管记录；
  - kernel 不给 replaced_by；模型视图白名单漏 replaced_by；
  - 接管 run 查找只读 takeover_by；
  - 历史不完整门连接替出口也拦。
- **回归**：定向 144 个文件：引用接管、接替、代理树、持久化与改动模块的 100 个文件，加上新文件、43 个全仓扫描类测试和 3 个读文档的测试。结果 2655 passed、1 skipped、5 xfailed，716.8 秒；没有跑全仓 pytest。
- **门禁**：ruff、doc_sync、strict code-size（hard=0；`SubAgentBaseService` 从 264 行降到 239 行）、`git diff --check`、check_clean_package 全部退出 0。线上 CI 不作为验收来源。

## G07 Skill 共存与成本对照原生验收（2026-09-28，被测 `9f88e4905`，文档分支 `claude/9a-skill-cost-acceptance`）

- **范围**：CAPABILITY_PACK_ACCEPTANCE 第 7 组（G07），包括两部分：
  - 旧 v3 随包全局 Skill 与能力包共存；
  - 开关关闭／单包／多包用同一原始需求（A01）做成本对照，每种配置跑一次。
- **环境**：
  - 生产 wheel（sha256 `9be2b59f…`），新建 Python 3.12.13 venv。
  - 每组全新隔离 home，私有 127.0.0.1:8440；被测进程 `env -i` 启动。
  - 模型目录副本 600、不打印、每组结束删除。
  - design-lite（v3，随包 Skill design-card）三组都装。
- **结果**：
  - v3 随包 Skill 三组都能列出和读取，内容 sha256 与源码一致。
  - 三组无关包调用都是 0；宿主都没做一次选择，模型都自选 A。
  - provider 输入：开关关闭 607102、单包 1364685、多包 507355（主调用 14／22／10），差异主要来自轮数。
  - 与包相关的每轮提示词开销（产品渲染估算）：901／967／1244 token。
  - 详见 [CAPABILITY_PACK_ACCEPTANCE.md](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#g07-skill-共存与成本对照原生验收2026-09-28)。
- **偏差**：
  - 前两次开关文件放错了位置（应放在 owner home 的 `config/`），实际用的是默认开关；已按实际配置归档，没有重跑。
  - 一次选择开启臂未覆盖。
- **收尾**：每组都执行了 `/exit` 和 `gateway stop`，PID 已消失，端口空闲；目录副本和 venv 已删除。
- **证据**：`~/.my-agent/decision-evidence/skill-cost-9f88/`
- **验证**：纯文档改动；`python3 scripts/check_doc_sync.py`、`git diff --check`、`python3 scripts/check_clean_package.py .`。

## R16 跨 owner 隔离原生验收（2026-09-28，被测 `9f88e4905`，文档分支 `claude/9b-r16-acceptance`）

- **范围**：CAPABILITY_PACK_ACCEPTANCE 第 6 组（G06）的跨 owner 部分：包、设置、task、偏好。同 owner 多 TUI 与子授权已有证据，
  不在本轮。
- **环境**：git archive 构建 wheel（tree `32e0c545…`，wheel sha256 `1ba17efe…`），新 Python 3.11.15 venv；scratchpad
  隔离 home、私有 127.0.0.1:8436；local/main 管理员与 local/user 普通用户两个 owner，都有 owner 墙；模型目录副本 600、
  不打印、结束删除；被测进程 `env -i` 启动。
- **结果**：四项均通过，未发现隔离缺陷，逐项结构化证据见
  [CAPABILITY_PACK_ACCEPTANCE.md](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#r16-跨-owner-隔离原生验收2026-09-28)。
  B 读取 A 私有包返回 `SKILL_SNAPSHOT_UNAVAILABLE`；B 申请 Full Access 未生效；B 的 `list_agents` 看不到 A 的子代理；
  A 的偏好写入只落在 A 的人格文件。
- **复现**：`~/.my-agent/decision-evidence/r16-isolation-9f88/harness/`（stage → run_gateway / run_tui → send → collect /
  final_evidence）；判定只读结构化事实，不读对话、人格、记忆正文。
- **未覆盖**：按包选择偏好（两边都未形成 capability_selection 记录）、实际 profile 核对、主机层 global_index 的可读性探测。

## R10 三类控制独立原生实测（2026-09-28，分支 `claude/be-r10-acceptance`，基于 `7249a1ebd`，只改文档）

- **来源**：能力包收口缺项 G04。暂停 Goal、中断当前回合、明确停止资源三类控制，之前只有 `/stop` 组合状态和 REOPEN03 的旧证据；
  集成方把 R10 的真实验收从 Codex 转给 Claude。判据沿用 R10 原行，详见 `docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md` 同名一节。
- **被测**：`9f88e4905`（与生产 step14w 产品代码相同，与当前 main 的 Python 代码相同）。`git archive` 构建 wheel，摘要
  `3c34d1c4a2a71572d6e67642bd06ac995b1d5de29413ecdb1ebc235580c6a24b`，clean-package 通过，装入全新 Python 3.11 venv。
  隔离 home，Gateway 只在 127.0.0.1:8434；模型为 Codex 27 次矩阵所用的私有档案（官方 MiniMax-M2.7，窗口 262144）。
- **做法**：每项一个新 TUI 会话、一条原始中文需求，控制命令按用户方式在 TUI 里输入，只在结构化就绪条件满足后发出。
  判定只读控制记录、请求状态、runtime.db（只读 immutable）、后台进程登记与 PID 存活、心跳文件行数，不读对话正文。
- **结果**：
  - 暂停 Goal：通过。在跑的 attempt 以 done 结束，暂停时正在执行的工具 SUCCEEDED；目标 paused 后 12 分钟内没有新 attempt。
  - 中断当前回合：通过。请求 interrupted，run 与 attempt cancelled，之后没有新的工具操作；本任务后台进程保持 running，
    心跳继续增长，没有停止请求。
  - 明确停止资源：通过。本任务后台进程被回收（`reason=stop_requested`、SIGTERM、confirmed），任务关闭为 cancelled；
    其它任务的后台进程和已暂停的目标不受影响。
- **未计判定**：B1 是测试侧就绪条件写错路径，控制没有发出；C1 发出了 `/stop`，但本任务的后台进程在冻结前已自行退出，
  资源停止分项未覆盖。两次都如实保留，C2 重跑时加严了就绪条件。长前台工具执行中途的 `/interrupt` 与 `/goal resume` 对照未覆盖。
- **用量**：23 次主模型调用（MiniMax-M2.7），决策与辅助调用为 0。
- **证据**：`~/.my-agent/decision-evidence/r10-controls-9f88/`（仓库外）。`r10-results.json` 摘要
  `93642c201a66d0620e70981df4ee50ecc8a5e3de0f3edb7819f1c2cee8d4a3e8`，`MANIFEST.sha256` 摘要
  `e25c67ea2567a255af25439a0bb6b7283bb248f945a8554f615d589efd127484`。
- **门禁**：只改文档，跑了 doc sync、`git diff --check`、clean package；没有产品或测试代码变更，不跑 pytest。

## 后台子代理四条特殊路径：脚本模型端到端（2026-09-28，分支 `claude/ae-bg-subagent-e2e`，仅文档）

- **性质**：脚本模型端到端机制已验，真实模型未覆盖；不是真实模型验收，不改判能力包 G03。详见
  [验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#g03后台子代理四条特殊路径的脚本模型端到端2026-09-28)。
- **被测与环境**：
  - 源码 `9f88e4905`，与生产 step14w 同源，经 `git archive` 导出；专用 venv 用 `.pth` 引入导出源码和 ci-venv 依赖。
  - 脚本化 OpenAI 兼容假模型（回环 8447）、真实 Gateway（8437）和 tmux 里的真实 TUI，三者都用 `env -i` 启动。
  - HOME 指向假家目录，跑完为空；保留代理变量，`NO_PROXY=127.0.0.1`，不设模型密钥。
- **剧本只看结构**：
  - 判定依据是测试者提示标记、当前回合开头（`# User Task`／`# Host Event`）、Host Event 的 JSON 事实、工具集
    （叶子子代理没有 create_subagents）和本回合工具结果。
  - 子代理状态只认宿主事实，模型输出 `[SUBAGENT_RESULT]` 不能让子代理变 BLOCKED；BLOCKED 由宿主授权停机
    （15 次同码授权失败）产生。
- **历史不完整的触发**：
  - 测试者在隔离 home 里把 owner 工具索引改为只写（0200），读取报 PermissionError；
  - 然后放行被脚本挂起的子代理，等父代理收到拒绝后恢复原权限，窗口 6.04 秒；
  - 不完整事实只存在于该唤醒片的内存里，所以靠拒绝码 `TOOL_ONE_SHOT_HISTORY_INCOMPLETE` 和注入账本判定。
- **判据**：只读以下结构化事实，不读会话正文：
  - tool_completed 的错误码、failure_stage 和 handler_executed；
  - replacement_records；
  - canonical 状态与 takeover_by；
  - wake 队列、请求终态和 TaskRun；
  - Gateway 停止后以 immutable 只读打开的 runtime.db。
- **结果**：ONE_SHOT 拒绝、历史不完整、BLOCKED 后接替、后台续接后的前台新请求四条全部通过。5 个请求全部 done，
  8 条 wake 全部 handled。
- **发现（未修，已报集成方）**：接替 DONE 子代理时，回执报 `recorded` 并写出 TAKEOVER.md；但
  `_restore_newer_closed_state` 把 TAKEN_OVER 静默还原为 DONE，takeover_by 为空。共复现两次。
- **本轮验证**：只改 Markdown；运行 doc_sync、`git diff --check` 和 `check_clean_package`，没有跑 pytest。

## 能力包 G05 恢复边界：脚本模型端到端（2026-09-28，被测 `9f88e4905`，仅文档）

- **结论**：脚本模型端到端机制已验，真实模型未覆盖。五个场景全部通过：审批期间停用＋迟到批准、读取进行中切代、UNKNOWN 原操作查询零重放、非空设置不兼容、内容 SHA 变化后旧任务续读。结果表和边界见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#g05脚本模型端到端机制验证2026-09-28)。
- **方法**：git archive 导出被测提交，在独立 venv 中运行。Gateway 用 8438，脚本 OpenAI 兼容模型用 8448（只在本机回环）。进程都经 `env -i` 启动，HOME 指向测试根内的空目录，不设模型密钥。两个真实 TUI：一个发需求并按 y/n 处理审批，一个执行 `/plugins` 管理命令。换代、更新和配置只走 `/plugins disable|enable|update|configure`，不改数据库。
- **受控故障**：Gateway 进程加载测试侧 `sitecustomize`，暂停点由一次性 armed 文件开启，不改变产品返回值。读中切代暂停在包成员读取前后两次安装核对之间；UNKNOWN 场景暂停在内容启用提交之后，再 SIGKILL 本测试 Gateway 的 PID。
- **判据**：只看结构化事实，包括工具回执的 status、error_code、effect_outcome，安装表的 revision、activation_id 和设置，任务 `skill_snapshot_refs`，runtime.db 的操作行数、generation 和 attempt（只读），以及在 runtime.db 副本上调用产品 `query_host_command` 得到的 `outcome_unknown`。不看模型文字。
- **覆盖边界**：纯内容包的 source_ref 写入在 ask/auto 下不弹审批，所以审批场景用同一回合里另一个只读审批作为等待点。旧任务续读只验证了同一回合。管理操作以外的工具 UNKNOWN 没有覆盖。脚本与证据在 `~/.my-agent/decision-evidence/recovery-edges-9f88/`（仓库外）。
- 本条只改文档，不涉及产品代码或测试，没有跑 pytest。

## 参数减量 C 组第 2 批：恢复/归档 6 键降为常量（2026-09-28，分支 `my-agent/self-dev-2`）

- **来源/做法**：dev 在 my-agent-2 开发交流板派的任务 1，做法照 main 上的 `87e025bb6`。6 键数值不变，降为读取点旁的具名常量：
  `agent/memory_archive/resume_context.py` 的 `RESUME_AUTO_CONTEXT_LIMIT`(5)、`RESUME_RECOMMENDED_READ_PATHS_LIMIT`(20)、
  `RESUME_ARCHIVE_SCAN_LIMIT`(0，0 表示不限)、`QUERY_CONTENT_PREVIEW_CHARS`(500)，`agent/memory_archive/artifact/read_modes.py` 的
  `ARTIFACT_DEFAULT_READ_CHARS`(4000)；归档检索文件上限直接复用 `query/archive_io.py` 既有的 `ARCHIVE_SEARCH_FILE_LIMIT`(30)，
  避免同一概念两份定义（`test_constant_names_unique` 会拦）。
- **配置面同步删除**：`AgentConfig` 6 字段、随包 `agent_config.yaml` 6 行与相关说明注释、`services/_normalize.py` 6 条规格、
  `_memory_types.MemorySettings` 与 `_memory_coercion` 里的 `memory_resume_auto_context_limit`、说明基线 5 个条目、
  `CLI_REFERENCE.md` 的示例行；用户配置里残留旧键只按未知键告警，不迁移。
- **测试改动**：`test_memory_config.py`、`test_settings_memory.py`、`test_memory_runtime{,_basics,_archive}.py`、`test_parameter_registry.py`
  删除或改写旧键用例（这些键已不是参数，对应归一化/边界用例随之移除）。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_memory_*.py tests/test_artifact_*.py tests/test_settings*.py tests/test_config*.py -q --tb=short`
  （1447 passed），另跑 `test_parameter_registry / test_config_field_readers / test_constant_names_unique / test_architecture_guardrails`；
  仓库根跑 Ruff、`scripts/check_doc_sync.py`、`scripts/check_code_size.py --mode strict`、`git diff --check`、`scripts/check_clean_package.py .`。
- **未包含**：`frontend/config/backend-config-catalog.json` 是随包 YAML 的生成物，本批没有重新生成（重新生成会带出 189/303 行与本批无关的历史差异）；
  该目录在本基线上已 stale，前端线需单独跑 `npm run sync:config`。

## 会话间消息与派活第 1 片第二补丁：错误码登记 + 身份 fail closed + 渠道白名单 + 未知来源（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：dev 10:37 / 10:41 两条审阅（其中两条我上轮漏读，本轮补齐）。
- **做法**：
  1. **错误码登记**：`SESSION_NO_CURRENT_THREAD`、`SESSION_IDENTITY_UNAVAILABLE`、
     `SESSION_TARGET_CHANNEL_UNSUPPORTED` 全部登记进 `contracts/error_taxonomy.ERROR_CONTRACTS`；
     定向测试带上守卫 `test_recovery_code_policy.py`。
  2. **身份 fail closed**：`_sender_context` 删除 `or "main"` / `or "local"` 缺省兜底；
     身份三元组任一为空返回 `SESSION_IDENTITY_UNAVAILABLE`。判定层也加了 `_identity_complete` 二次校验。
     顺手核查其他位置，确认没有同类"拿不到就当管理员"的兜底。
  3. **渠道白名单**：`IM_CHANNELS`（黑名单）改成 `LOCAL_TARGET_CHANNELS`（白名单：chat/cli/local/tui/gateway-cli/http）；
     空渠道按本地处理；不在白名单的（含以后新增渠道）一律 `SESSION_TARGET_CHANNEL_UNSUPPORTED`，fail closed。
  4. **未知来源不冒充用户**：`_render_guidance_user_input` 改三分支——`origin_kind` 缺失→按用户插话原样（兼容旧数据）；
     `=session_message`→宿主事件并写来源；**非空但未知→也按宿主事件呈现（来源"未知"）**，不渲染成用户原话。
- **新测试**：权限文件增 4 项（未知新渠道 fail closed、空渠道按本地、身份缺 3 参数化 fail closed）；
  渲染文件增/改 2 项（未知 origin_kind 走宿主事件、缺失 origin_kind 仍原样）。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_messaging_permissions.py agent_py_agent/tests/test_send_session_message_tool.py agent_py_agent/tests/test_session_message_rendering.py agent_py_agent/tests/test_recovery_code_policy.py -q`（58 项）。
- **变异验证**：身份检查改恒假 → 3 条身份测试红；白名单退回黑名单 → 未知渠道测试红；恢复后全绿。

## 会话间消息与派活第 1 片补丁：关闭可见性 + 呈现不冒充用户 + IM 目标拒绝（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：dev 审阅第 1 片后提的 3 处要求（每处要测试）。
- **做法**：
  1. **关闭时工具不可见**：新增纯函数 `session_messaging_tool_visible` / `session_task_tool_visible`
     （`conversation/session_messaging.py`），只读 `home_paths.owner_kind` 与开关；`core.py` 的
     `_register_orchestration_tools` 改为**条件注册**——判定为 False 时工具根本不进 registry，
     模型工具列表里不出现（不是"调用时才拒绝"）。配置经既有 `capability_config_for_agent(agent)` 读取。
  2. **呈现不冒充用户**：改 `agent_core/runtime/guidance.py` 的 `_render_guidance_user_input`：
     带 `metadata.origin_kind=session_message` 的 guidance 渲染成宿主事件
     `[SESSION_MESSAGE_HOST_EVENT]` 并写明来源会话（`origin_thread_id`），用户插话保持原样。
     分类只看结构化 metadata，不看正文。
  3. **IM 目标拒绝**：新增错误码 `SESSION_TARGET_CHANNEL_UNSUPPORTED`（已注册进 `error_taxonomy`）；
     判定层新增 `target_channel` 入参，命中 `IM_CHANNELS`（feishu/qq/wecom/dingtalk）即拒绝；
     工具用 `_thread_channel` 从 canonical thread 的 `channel_bindings` 取渠道。
  - `test_session_messaging_permissions.py` 增 8 项：IM 渠道拒绝（含普通用户优先级）、本地渠道放行、
    工具可见性 5 项（管理员开关开/关、普通用户默认、用户开关开、派任务仅管理员）。
  - `test_session_message_rendering.py`（5 项，新增文件）：会话消息渲染成宿主事件并带来源、用户插话保持原样、
    未知 origin_kind 按用户处理、混合条目顺序、缺来源仍有标记。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_messaging_permissions.py agent_py_agent/tests/test_send_session_message_tool.py agent_py_agent/tests/test_session_message_rendering.py -q`（36 项）。
- **变异验证**：把 `_render_guidance_user_input` 的会话消息分流改成恒假 → 3 条渲染测试全红；恢复后全绿。
- **回归**：`test_runtime_guidance.py`、`test_wake_queue.py` 通过。修复过程中发现真实缺陷
  （`SimpleAgent` 没有 `capability_config` 属性，正确入口是 `capability_config_for_agent(agent)`），
  已被回归测试捕获并修正。
- 五项静态 gate（ruff/doc_sync/strict code-size/diff --check/clean_package）全过。

## 会话间消息与派活第 1 片：权限判定 + 管理员发消息工具（2026-09-28，分支 `my-agent/self-dev-3`）

- **来源**：开发交流板任务「会话之间的消息与派活（第一期只开放给管理员）」；设计经 dev 审过，6 处补充已并入
  `docs/design/SESSION_MESSAGING.md`。
- **做法**：
  - 新增 `agent_py_agent/agent/conversation/session_messaging.py`：权限判定纯函数 `decide_session_messaging`，
    只读结构化字段（发送方 `OwnerIdentity` 的 owner_kind、kind、是否同 owner/同 thread、三个开关），
    返回 `(allowed, error_code, scope_warnings)`。目标不存在与跨 owner 返回同一码 `SESSION_TARGET_OUT_OF_SCOPE`（不泄露存在性）；
    自派任务拒绝 `SESSION_TASK_TARGET_SELF`，自消息允许。
  - 新增 `agent_py_agent/agent/agent_core/orchestration/tools/send_session_message.py`：`SendSessionMessageTool`，
    身份只从 `agent.home_paths` 的 owner 三元组与当前会话 thread_id 取；通过后向目标 thread 的 guidance 队列
    `append_once`（幂等 dedupe_key，来源 `origin_kind/origin_thread_id` 落 metadata），目标 `status=active` 时再 `wake.raise_signal`。
  - 配置同步：`capability_config.yaml` + `capability/config.py` 加 5 个键（管理员发消息/派活默认开，普通用户发消息默认关，
    链深上限、每对会话每小时上限，0 表示不限制）。
  - 错误码注册：`contracts/error_taxonomy.py` 新增 `SESSION_MESSAGING_DISABLED`、`SESSION_TASK_NOT_ALLOWED`、
    `SESSION_TARGET_OUT_OF_SCOPE`、`SESSION_TASK_TARGET_SELF`（未注册会被归一成 `UNKNOWN_ERROR`，这是既有合同）。
  - 注册位置：`core.py` 的 `_register_orchestration_tools`，放在 `enable_subagents` 门控**之前**（会话间消息是 owner 级能力）。
  - `agent_py_agent/tests/test_session_messaging_permissions.py`（18 项）：8 格权限矩阵、不泄露存在性、
    自派任务/自消息、开关关闭、kind 非法。
  - `agent_py_agent/tests/test_send_session_message_tool.py`（5 项）：缺参数失败、目标不存在返回统一越界码、
    成功写入来源结构化消息并唤醒空闲目标、非活跃目标只排队不唤醒。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_session_messaging_permissions.py agent_py_agent/tests/test_send_session_message_tool.py -q`。
- **变异验证**：把跨 owner 判定改成恒真 → 5 条跨 owner 测试全红；把自派任务判定改成恒假 → 自派测试变红；恢复后全绿。
- **回归**：`test_runtime_guidance.py`、`test_wake_queue.py`、`test_orchestration_tool_constants.py` 通过。
- 尚无真实 Gateway 双会话端到端验收（第 3 片用 fake LLM 做；真实环境由 dev 安排）。
## Jev curator invalid_input 快速失败修复（2026-09-28，分支 `my-agent/self-dev-4`）

- **来源**：my-agent-4 开发交流板任务 1。owner 的 `data/decision/outcomes.jsonl` 里 point=curator 有 7 条
  `status=error, reason=invalid_input`、耗时 5–9ms（从未调用到模型）。dev 裁决按"丙（一次请求里同一份材料只放一次）
  为主修 + 甲（token 级合同）兜底"，不做乙（不改消息优先的提取缩批契约）。
- **根因**：`decision_curator._decision_material` 把整批材料放进决策请求 `state`，同时**每个问题都内联整段候选释义**，
  同一批材料的候选说明按题重复；实测 payload 73480 字符 / 31920 token，超过 32768 窗口的 90% 上限（29491），
  被 `typesafe_decision_wire.validate_typesafe_request_window` 拒绝 → `decision_service` 映射成 `invalid_input`。
  另：audit 事件投影里 11 个字段在全部行都是空值，每行仍付约 240 字符的键名开销。
- **做法**：①候选释义/非选择/need_data 语义上移 `state.annotation_criteria`，每题只留身份引用与 `criteria_key`，
  wire 层接受 `criteria` 为共享引用字符串（仍用 `_valid_key` 限制长度与控制字符）；②audit 引用的空字段不再发送
  （证据校验读 dataclass 属性，不读这些键）；③新增窗口兜底：按实际决策模型的 `model_context_window_tokens`
  （读本点位已授权连接，读不到则不裁）**按整条来源从尾部裁题面**，被裁来源仍在 `state.batch` 原始快照里，
  留在原游标之后下一轮重放；裁掉时用既有固定码 `memory_curator_input_fitted:decision_window` 报告，条数不进文本。
  `curator` 也纳入 `_JEV_BOUND_POINTS` 的有界点位。
- **新测试**：`agent_py_agent/tests/test_decision_curator_window_budget.py`（候选只发一次；请求不再超窗；
  窗口足够时不因预算丢来源；窗口不足时按 items 前缀整条裁、被裁来源只在题面消失且仍在批次快照里）。
- **复现**：`PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_decision_curator_window_budget.py -q`
  （修复前红：payload token 36308 > 29491）。回归另跑 `test_memory_curator_v2`、
  `test_decision_curator_plugin_concurrency`、`test_curator_input_budget`、`test_decision_owner_scope` 全过。
- **变异验证**：①去掉 `_fit_items_to_window` → 2 条红；②把 `criteria` 改回逐题内联 → 2 条红（且 token 回到 36308）。

## 前端 import 链恢复：补回 `frontend/src/data/runtimeConfig.ts` 与 `mockConfig.ts`（2026-09-28，分支 `claude/9a-frontend-runtimeconfig`，基于 `3e23d2da8`）

- **根因（误删）**：2026-08-15 建独立仓库的初始化提交 `0b6252590` 没带 `frontend/src/data/` 两个文件，而同一提交里的
  `settingsStore.ts` 仍在引用；09-09 的合并 `83bc92860` 采用了新仓库一侧的树（官方历史一侧 `4e276ed21` 仍有这两个文件）。
  引用方一直在用：`settingsStore`、`pages/Tools`、`pages/Templates`、`api/mockApi`。
- **验证方式**（本机没有 `frontend/node_modules`，不装依赖、不跑 tsc/vite build）：
  - import 链：`bun build frontend/src/main.tsx --packages external --target browser --outdir <临时目录>`，第三方包记为外部依赖，
    只解析相对导入与 JSON。修复前 rc=1，恰好 5 处 `Could not resolve`（4 × `../data/runtimeConfig`、1 × `../data/mockConfig`）；修复后 rc=0。
  - 语法：用 Bun 的 TSX 转译器（`new Bun.Transpiler({loader: "tsx"}).transformSync`）转译 `frontend/src` 下全部 47 个 TS/TSX，全部通过；只查语法，不查类型。
  - 配置目录：`node frontend/scripts/sync-backend-config.mjs --check`。本基线上目录已过期，先重新生成（278 项）后 rc=0；
    `mockConfig` 只从这个生成目录派生。
  - 类型：`runtimeConfig` 的类型由 `frontend-runtime-config.json` 推导（`resolveJsonModule` 已开）；该 JSON 自 08-15 未改，与丢失前
    官方历史版本逐字相同，store 的展开赋值口径不变。
- 没有检查前端文件的 pytest；仓库静态门禁（ruff、doc_sync、strict code_size、diff --check、clean_package）照常跑。

## 根内路径不再误报"拼写错误"（2026-09-28，dev 派活任务 2，my-agent 实现）

- **来源**：dev 06:22 裁定——读工具拿不到"不同且有纠正价值"的拼写建议时，应如实抛
  `PathAccessError(decision.message, decision.code)` 走权限码，而不是退化成 `TOOL_INVALID_ARGUMENTS`；
  并补"不在任何根下、且建议等于原路径"也报权限码的用例。
- **实测结论（先复现再改）**：旧行为在"路径已落在某个 workspace_root 之下、但被 owner 墙拒绝"时，
  会把拼写建议（此时指向另一个路径）当提示抛出：`ValueError: 路径疑似拼写错误…请使用 suggested_target 重试`，
  既盖住真实权限拒绝、又诱导调用方原地重试。子代理端到端造不出该组合（未授权路径一律先被墙拦成
  `PATH_OWNER_SCOPE_BLOCKED`），因此核心用例改用工具层直接构造该组合。
- **做法**：`agent/tooling/_filesystem_read.py` 把拼写分支收进 `_raise_for_typo_hint`，先判
  `any(_path_is_under(candidate, root) for root in self.workspace_roots)`：根内路径直接返回，交由
  `PathAccessError` 报权限码；`agent/path_recovery_hints.py` 的 `suggest_workspace_typo_target` 增加
  `Path(suggested) != candidate` 过滤——建议等于原路径时没有纠正价值，返回空。
- **新增用例**（`tests/test_create_subagents_input_read_scope.py`）：
  `test_blocked_path_inside_a_known_root_reports_the_permission_code`（核心场景，工具层构造）、
  `test_path_typo_outside_every_known_root_keeps_the_spelling_hint`（真拼写仍保留提示）、
  `test_uncorrectable_suggestion_falls_back_to_the_permission_code`（建议无纠正价值时按权限码上报）。
  断言只读结构化字段 `error_code` / `target` / 有无 `suspected_path_typo`——账本条目本来就没有
  `failure_stage`（它只出现在渲染后的回执文本头里）。
- **为什么"真拼写"那条只能做函数级断言**：拼写分支针对的是"工作区根的**位置**写错"（根名出现在路径
  中段），不是"文件名拼错"；且它只在"策略已拒绝 + 不在任何 workspace_root 下 + 建议非空且不同"三者
  同时成立时才抛出。子代理端到端能构造的只有那个窄组合（即核心用例），"根内拼错、旁边有真文件"这条
  路在现有实现里走不通（`check_path_access` 对根内路径直接放行、不看文件是否存在），所以真拼写用
  函数级断言钉语义，不假装端到端验证过。
- **变异验证**：①去掉 `_raise_for_typo_hint` 的根内判断 → 核心用例红（旧行为抛拼写错误 ValueError）；
  ②去掉 `Path(suggested) != candidate` → 核心用例与"无纠正价值"用例同时红；③重构后重跑变异①仍红。
  三次均还原后复绿。
- **门禁**：定向 72 项、四守卫测试 99 项全绿；ruff、`check_doc_sync.py`、`git diff --check`、
  code-size 身份差集 ADDED 0 / REMOVED 0、`check_clean_package.py . --mode worktree` 全通过。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_create_subagents_input_read_scope.py -q`。

## 包入口读取判定补结构断言用例（2026-09-28，dev 派活任务 3，my-agent 实现）

- **来源**：dsh-9b 做变异时发现 `capability/package_selection_authority.py` 的墙判定没有用例——把
  `package_entry_policy` 里两处"从冻结边界取值"改成忽略冻结值，测试全绿，没人抓得住。
- **实测结论**：该变异在 `skill_search` 入口上本来就不可观测（`skill_search` 参数表里没有任何命中
  `_PATH_KEYS` 的键，且 schema 门排在路径门之前），所以按集成者的裁定改用最简形态：直接对
  `package_entry_policy` 产出的策略做结构断言，不改产品语义、不动 `_PATH_KEYS`。
- **做法**：`tests/test_capability_selection_authority.py` 新增 `_capture_policy_request`（用只做记录的包装类
  替换模块内 `ActionPolicy`，真类提前抓进闭包再调用，避免自递归）与
  `test_entry_policy_uses_frozen_boundary_not_registry_scope`：把 `write_boundary_with_runtime_ledger` 打桩成带
  `execution_workspace_roots` + `effective_owner_scope_root` 的冻结边界，断言传给 ActionPolicyRequest 的
  `workspace_roots` 含冻结的墙外根、`owner_scope_root` 等于冻结值，且不等于 registry 自己的根集合。
- **变异验证**：把两处改回"忽略冻结值"，该用例两次都红（`AssertionError`：冻结根不在 workspace_roots 里）；
  还原后 42 项全绿。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_capability_selection_authority.py -q`。

## step14w Linux 容器 wheel 冒烟（2026-09-28，Linux 真机仍未部署（测试机下线）；容器内 wheel 安装、入口、gateway 健康已验证，非真机部署）

- **来源**：`git archive 9f88e490576f5c2ffc37393fb2d9b5ebd9fc8b06`（与生产 step14w 同源），按 `build_release.py` 的做法 `pip wheel --no-deps --no-build-isolation` 重建，rc=0。
- **wheel 对比**：重建 sha256 `f42c3ed3b8096b85669c5d28ae2eaa6cc66d5b09ca90a06ebc2c77e13eaf3c66`，生产 `9be2b59f704f3c2a347063c55b2c25ab8e38fb0e8ed8d0b25a233f644e032136`，不一致；1440 个条目同名同字节，只有 7 个 dist-info 条目的 zip 时间戳不同（构建未设 `SOURCE_DATE_EPOCH`）。
- **容器**：`my-agent-linux-test:py312`（arm64、Python 3.12.13），`--network none --rm --entrypoint bash`；容器内 `python3 -m venv --system-site-packages`，`pip install --no-deps --no-index` 装 wheel，依赖用镜像已有的；隔离的 `MY_AGENT_HOME`，无模型配置、不复制 catalog。
- **结果**：三个入口 `--help`（`python -m agent_py_agent`、`my-agent`、`my-agent chat`）、`gateway start`、容器内 `127.0.0.1:8420/status`、`gateway status`、`gateway stop` 全部 rc=0；`/status` 原文 `{"status": "running", "pid": 26, "uptime": 0.65, "requests": {"pending": 0, "processing": 0}, "runtime_prefix": "/tmp/venv"}`，停止后 `stopped`、`exit_code` 0、`planned_stop`。
- **证据**：`~/.my-agent/releases/step14w-9be2b59f/linux-lane/wheel-smoke/`（README、commands.txt、rc.txt、status_raw.json、wheel_compare.txt、gateway 日志）。
- **边界**：arm64 容器不是真机，只验安装、入口与 gateway 健康，不含模型请求与 TUI 交互；不代替 Linux 真机部署。

## 能力包收口缺项复核（2026-09-28，仅文档）

十二步骤对照及 77 项冻结引用摘要复核已完成；原 27 次结果不改判，也不重跑。完整七组未覆盖／部分覆盖范围见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#收口审计与七组未覆盖范围2026-09-28)。原 R10/R11/R12/R14/R16 的未齐分支不能被最新四项摘要遮掉；普通 Skill、零包核心任务和目录规模夹具也不能外推全部组合通过。

C22 的 65536 只证明受控机制。历史生产 v2 导出保持原摘要：77 个已提交检查点的配置窗口均至少 200000，压缩前估算仅 43 个达到 128K；17 个手动、60 个触发未知。没有当前包 pins 和后续原资源执行，不能拼成当前能力包生产规模验收。配置窗口、投影估算与供应商实际输入分别记账。

Mac 两版发布记录和 wheel 摘要已核，Linux 两版均未部署；测试机下线是外部阻塞，两版 Docker editable 源码测试不能代替 wheel 发布。本轮只改现有 Markdown，没有产品／测试／配置或文件树变更；按文档范围运行静态门，不重复 focused/full pytest 或真实模型，线上 CI 未作来源。建议下一步由 Claude 核对并集成缺项文档，未覆盖项先记录不排新运行；只读可并行，产品和发布继续单人负责。

## 能力包固定 0c 最终矩阵（2026-09-28，27 次执行结束）

固定源码 `0c340fe295ebe1d725aea1ebb8dd570a360a5183` 独立 229 项定向通过；Ruff、doc sync、strict code-size、diff、clean-package 均退出 0。另只读核作者 12 份原全仓日志，合计 23872 passed、21 skipped、32 xfailed、5 xpassed、0 failed/errors。线上 CI 未作为验收来源，两类验证不混作 root 全仓重跑。

Linux 两版原 12 分片另计，各 23833 passed、60 skipped、32 xfailed、5 xpassed，pytest 无 failed/errors，退出码全 0；shard-1 汇总后有 ExpiringCache 清理回溯，不能用 tail -1 提取结果，也不能把 0 failed/errors 扩称日志全无异常。此次只读归因核对三例原生错误回执及后继动作，没有重跑模型、重做业务任务或新增测试。

9 个冻结语义各 3 次，共 27 个新原生 TUI／工作区，每次一条原需求，官方 MiniMax-M2.7、原 262144 配置和同一 A0.3.0/B0.1.4 保持；没有补提示或代跑检查器。最终全树终态、269 次完整 NativeIR 工具调用、254 次物理模型 attempts、255 次 HTTP attempts／1 次 retry，27 次 memory 均 0。归档索引不含 read_artifact，不能作为完整工具计数。

最终固定 0c340fe29 的 27 次原生执行已全部结束，原输入、配置、安装表、1433 个安装成员及包 pins 保持；普通任务 9/9 通过，制作类业务 7/9 通过，改编类 0/9 通过，合计业务 16/27 通过、11/27 失败。原资源链 12/18 成立，第三轮改编任务的混合写入参数及随后自建检查器另列为通用使用／恢复稳定性缺口，不能全部归为剧情质量。Goal active；Mac 两版部署记录与 wheel 摘要已核，Linux 明确未部署（测试机下线），只有容器源码测试证据。最终矩阵／发布文档已集成到 ee4c0ae6b，远端推送按 Claude 回报记录。 原普通三题的重复只证明已观察结果，不外推一般成功率。本轮仓库只同步文档，没有产品／测试代码或配置变更；文档交付的 Ruff、doc sync、strict code-size、diff、clean-package 全部退出 0，五项静态 gate 已通过，线上 CI 未作来源；不重复全仓。门禁记录 final-0c340fe29-docs-gates.json 摘要 57e4a9deec256874e79e22e05433abebb0b29e08ed55fb148576dc85aaf54fb4。建议下一步先完成分项交接，发布由 Claude 负责；详见[结果和证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#固定版本最终矩阵2026-09-28)。

## 参数减量第三批 C 组 · 第 1 批：归档预览档位与摘要长度降为常量（2026-09-28，dev 派活，my-agent 实现）

- **来源**：dev 06:12 派 C 组（记忆相关的内部参数降级为常量，做法照 A 组 `f82e0e9f6` / E 组 `796a43e02`）：值不变、降到读取点旁
  具名常量；从 AgentConfig、随包 YAML、归一化表、字段规格表、说明基线、测试里删掉；用户配置里残留的旧键只告警、不迁移。
- **本批范围（5 键）**：`memory_archive_preview_level_0_chars` / `_1` / `_2` / `_3`、`memory_archive_summary_chars`。
- **做法**：
  - 常量落在读取点旁：`agent/agent_core/runtime/live_archive.py` 新增 `ARCHIVE_SUMMARY_CHARS = 96` 与
    `ARCHIVE_PREVIEW_LIMITS = {0: 2048, 1: 1024, 2: 512, 3: 160}`，活动归档两处调用点直接使用常量。
  - 原先 `live_archive` 与 `agent/agent_core/_finalization_service.py` 各写一份同值默认值，本批**统一成一处**：
    `_finalization_service` 从 `live_archive` import 常量，本地 `_memory_archive_preview_limits` 删除。
  - 配置面按 E 组口径清理：`AgentConfig` 五个字段、`config/agent_config.yaml` 五行、`services/_normalize.py` 五条规格、
    `tests/fixtures/parameter_description_baseline.json` 五个条目全部删除（`memory_archive_level` 保留，它是用户可见项）。
  - `tests/test_memory_archive_runtime.py` 的归档等级用例改为 patch 常量（原先给 config 赋那两个键，已失效）。
- **复现/门禁**：`cd agent_py_agent && python3 -m pytest tests -k archive -q`（645 passed）；守卫
  `test_parameter_registry.py` / `test_config_field_readers.py` / `test_constant_names_unique.py` / `test_architecture_guardrails.py` 全绿；
  `ruff check`、`check_doc_sync.py`、`git diff --check`、`check_clean_package.py . --mode worktree` 全部通过。
- **剩余**：C 组还有 15 键（恢复与检索条数 5 个、curator 内部批次 8 个、`memory_artifact_default_read_chars`）与保留项确认，
  清单见交流板 2026-09-28 06:20 那条。

## 后台唤醒片工具轮预算：写 0 回落全局时同样按携带条数平移（2026-09-28，分支 `claude/be-wake-budget`，基于 `315cd81ef`）

- **来源**：B（唤醒片续接前台回合的工具账）留下的例外。`background_max_tool_rounds` 写 0 时不写片上限，
  `_effective_max_tool_rounds` 回落全局 `max_tool_rounds`。全局是正数 M 时，整条活动回合共用 M，且不加携带条数，
  前台已用满 M 轮时唤醒片一开始就触顶。集成者定的语义：写 0 时仍回落全局 M，但唤醒片同样按“基线 + 携带条数”起算，
  新增额度就是 M；全局也是 0 或留空时行为不变。这样正数与 0 两支都是“每片新增额度”。
- **改动**：只改 `_apply_internal_background_tool_budget`。后台额度 ≤ 0 时读 `AgentConfig.max_tool_rounds`，是正数就写成
  片上限，随后 `_extend_background_slice_tool_budget` 照常加上携带条数；0、留空或非法值不写，原口径不变。外部消息与
  到点计划任务仍不套本预算。`runtime_guard_config.yaml` 注释写明两支语义。
- **测试**（`test_background_active_turn_carry.py` 新增 5 例矩阵，走真实 `_run_params` 链路，本布局携带 2 条）：
  - 后台额度 7 → 片上限 9；写 0、全局 40 → 片上限 42；
  - 写 0、全局 0 → 不写片上限，工具循环按不限制；写 0、全局留空 → 不写，按宿主缺省 5000；写 0、全局非法值 → 不写，按 5000；
  - 每例同时用 `_effective_max_tool_rounds` 核对工具循环实际生效的上限。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_background_active_turn_carry.py -q`。
- **变异验证**：6 个全部被抓出（写 0 不回落；留空当成 5000；全局盖掉正数后台额度；0 被写成片上限；不加携带条数；
  非法值不防护），每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，跑完逐字节恢复并核对哈希。
- **相关回归**：257 个测试文件 5681 passed、2 skipped、28 xfailed、5 xpassed。清单包括与改动路径有关的文件
  （含 `test_background_main_agent_runtime.py`、`test_runtime_guard_config_shared.py`），也包括全部扫描产品代码的守卫测试
  （含 `test_architecture_guardrails.py`、`test_constant_names_unique.py`、`test_config_field_readers.py`）。回归在 rebase 前的
  同一份代码上跑完；rebase 到 `315cd81ef` 后只有 Markdown 不同。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package 均通过；code-size 身份差集相对 `315cd81ef`
  新增 0、减少 0。

## 生命周期续跑读错分支 fail-closed（2026-09-28，分支 `claude/be-wake-fix`，基于 A `c035e9812`）

- **来源**：Codex 审查 B（证据 `capability-validation/evidence/pending-wake-B-read-error-probe.json`）：
  `_background_active_turn_tool_calls` 外层捕获 OSError/RuntimeError/ValueError 后直接返回空。两处索引在同一个生成器里
  先后读取，owner 索引一抛 OSError，任务索引根本不会被访问，已知的任务工具事实跟着丢掉，一次性编排去重变成空集合（fail-open）。
- **改动**：
  - 读取：`carried_tool_call_records_for_requests` 改收 `CarriedIndexSource`（索引根、结构化来源名、是否只用于运行时状态），
    每个来源独立读取、各自捕获 OSError，读到一半失败的来源整份丢弃；返回 `CarriedToolCallRead`（records 加 unreadable_sources）。
  - 来源判定：身份文件存在却给不出包含任务根的 owner 时，记为 owner_index 不完整（`run_workspace_identity_unusable`）；
    没有请求编号的旧式唤醒读错记为 task_index 不完整；其它意外（RuntimeError/ValueError）记为 active_turn_carry 不完整。
    都不再静默返回空。
  - 结构化事实：不完整时写进本片 task_attributes 的 `conversation_active_turn_carry_incomplete`（列表，每项 source 与
    error_type），读全时不写这个键。压缩重试沿用首次读取的结论：首次没读全时，之后每次尝试都写回同一事实。
  - fail-closed：不完整时只要有一个待建子代理没有有效的 `replacement_for_run_ids`，这次 create_subagents 就在运行时门拦下，
    错误码 `TOOL_ONE_SHOT_HISTORY_INCOMPLETE`（已登记错误合同与恢复提示）；全部写明接替关系的照常走原去重，非一次性工具不受影响。
    “父级结构化确认”目前没有现成机制，这次只实现 replacement_for_run_ids 这一条结构化出口，留作后续。
  - 与创建边界同口径（Codex 复核修补后又指出两种空值绕过：`[" "]` 去空白后为空；item 的空列表会盖掉顶层默认）：
    守卫改用 `create_payload.effective_replacement_ids_per_child`，它直接调用创建边界的 `create_items_from_params`
    （item 覆盖顶层），编号走 `replacements.replacement_source_ids`（去空白后非空）；创建前预检与接管落账也改用这一个
    归一化函数，不再各判各的。创建边界会整批拒绝的调用同样拦下。
- **测试**：
  - `test_background_active_turn_carry.py` 新增 6 项，读错都用真实文件系统错误（把索引换成同名目录，产生 IsADirectoryError）：
    owner 索引读不到时任务索引照常读到并报告来源；本片 task_attributes 写入结构化不完整事实；身份文件给不出 owner 时记为
    owner_index 不完整；旧式唤醒读错记为 task_index 不完整；fail-closed 只作用于一次性编排且认 replacement_for_run_ids
    （批量里有一项没写就拦）；压缩重试沿用首次读取的不完整结论（重试时重读成功也不改）。原有两项改用新接口，并补“读全时
    不写不完整事实”的断言。
  - 同口径矩阵 6 例：两个反例（空白编号、item 空列表覆盖顶层）和创建边界会拒绝的批次被拦；无标记时不拦、有效顶层默认、
    有效 item 照常放行；同时锁住共用函数给出的每个子代理有效接替编号。
  - `test_lifecycle_wake_host_event.py` 新增 1 项端到端：owner 索引读不到时，一律派工的脚本化假模型在真实后台链路里被
    `TOOL_ONE_SHOT_HISTORY_INCOMPLETE` 拦下，根任务的子代理仍只有一个。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_background_active_turn_carry.py agent_py_agent/tests/test_lifecycle_wake_host_event.py -q`。
- **变异验证**：17 个全部被抓出，每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，跑完逐字节恢复并核对哈希。
  - 读取：owner 读错后停止读任务索引；读不到的来源不记录；不完整事实不写；身份文件给不出 owner 时不标；旧式唤醒读错吞掉；
  - 守卫：不看不完整事实；接替出口去掉；对所有工具生效；批量只要一个写了接替就放行；守卫没接进运行时门；新错误码没登记；
  - 同口径：编号不做归一化；item 不覆盖顶层；归一化保留空白项；创建边界会拒绝的批次放行；
  - 压缩重试：不冻结首次结论；后续重读成功就改成完整。
- **相关回归**：与改动路径有关的 225 个测试文件（含 `test_background_main_agent_runtime.py`、
  `test_architecture_guardrails.py`）5211 passed、2 skipped、28 xfailed、5 xpassed。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package 均通过；code-size 身份差集相对 `aa97491f5`
  新增 0、减少 1。

## 生命周期唤醒片记为宿主事件（2026-09-28，分支 `claude/be-wake-turn`，基于 `aa97491f5`）

- **来源**：T3 真实 TUI 验收的观察 2（A 部分）。round2 的唤醒片请求里：
  - 原任务又被渲染成一条 `# User Task`（第 22 条与第 11 条相同），成了"最新的用户消息"；
  - 推荐节按原任务文字召回，第一个就是 `create_subagents`；
  - 30,436 字的后台上下文注入写进会话历史，下一轮前台请求原样重放。
- **改动**：
  - 回合触发类型：新增 `agent_core/runtime/turn_trigger.TurnTrigger`，字段为 kind、reason、wake_signal_id、source_agent_id、
    origin_request_ids 和 event_facts。它只由 `_background_model_inputs` 在生命周期续跑时构造，
    经 background_execution → RunParams → RuntimeLoopParams → ToolLoopExecuteParams 传递。None 表示普通用户轮。
    宿主决策只读 turn_trigger 与 wake_signal 的结构化字段。
  - 唤醒事实：`conversation/lifecycle_wake_event` 从唤醒信封的结构化字段做确定性投影：
    - 字段有白名单，嵌套字典按键排序；
    - 字符串最多 600 字，列表最多 8 项，嵌套最多 3 层；
    - 不收 runner_result_json、output_json、去重键和证据引用。
  - 渲染：原生 IR 的当前回合以 `RuntimeFactsTurn(source=host.lifecycle_wake)` 开头。缓存布局的 canonical 和文本协议任务节
    都用 `# Host Event`，下一行是固定首句（"宿主生命周期事件：……不是新的用户请求……"）。普通回合的字节不变。
    `user_prompt` 仍是原任务，召回、根任务和压缩照旧使用；`[active-turn-continuation]` 注入保留为软提示。
  - 推荐节：生命周期唤醒改用固定短名单，依次为 `list_agents`、`read_file`、`resolve_capability_requests`、`send_guidance`、
    `cancel_subagents`。只列本轮可见的工具，不按任务文字检索，也不追加能力包推荐。
  - 原生历史保存：唤醒片保存时去掉本片的 `prompt.runtime_injection` 快照（即后台上下文注入）。宿主事件、工具往返和其它快照照常保存，
    下一轮不再重放这 3 万字。代价是下一轮对这一片之后的内容有一次缓存未命中。
  - 旧数据不迁移：已经持久化的重复 `# User Task` 和注入照原样重放。
  - 原任务来源：`origin_request_ids` 只放会话历史里真有用户消息的请求。已核实的 Goal 任务来源（`_goal_runtime_context`
    校验过 Goal 属于本线程、任务编号与唤醒相同，不论是否 active）不是历史请求，不进 `origin_request_ids`，目标原文走
    `origin_task`；唤醒没有任何请求编号时，解析出的原任务也走 `origin_task`。附了 `origin_task` 时首句换成“原任务见下方
    origin_task……”，不声称原任务在历史里。这一条是 Codex 审 WIP `51361723d` 时发现的：Goal 子代理唤醒的
    `conversation_request_id` 可以是持久 Goal.task_id，原实现把它当成历史请求引用并丢了 origin_task
    （证据 `pending-wake-A-51361723d-goal-origin-probe.json`）。
- **测试**（`test_lifecycle_wake_host_event.py`，18 项）：
  - 事实投影：输入顺序不同，输出字节相同；字符串、列表有界；不收原始结果 JSON、去重键和证据引用；固定开头两行；
    `origin_task` 只在调用方传入时附上，首句随之切换。
  - Goal 来源矩阵（照 Codex 的复现，active/complete × 有/无普通请求，共 4 例）：`origin_request_ids` 只含真实历史请求，
    Goal 目标原文走 `origin_task`，首句不声称原任务在历史里；另有一例走真实后台链路的 Goal 唤醒，锁住调用链把已核实的
    Goal 来源传进来。
  - 触发类型只按结构化原因与原任务选择：生命周期原因（含能力申请）才有，没有原任务或非生命周期原因为 None。
  - 原生 IR：当前回合以宿主事件开头，交接摘要紧跟其后，没有 UserTurn；普通回合仍是 `# User Task`。
  - prompt：原生与文本协议都用宿主事件作为当前回合，缓存布局的 canonical 一致；普通回合字节不变。
  - 推荐节：固定短名单，按顺序列出且只列可见工具（owner 禁用 list_agents 时第一项变为 read_file）；普通回合仍按任务文字检索。
  - 原生历史保存：唤醒片去掉本片注入，保留宿主事件、其它快照和工具往返；普通回合不变。
  - 脚本化假模型走真实后台链路（前台派工记录按 T3 round1 布局写在 owner 根索引，子代理阻塞）：
    - 按“最新 User Task”行事的假模型不再派工；它收到的消息里当前回合以宿主事件开头，推荐节第一项是 list_agents，
      诊断 prompt 同口径；会话历史里存的是宿主事件，没有第二条用户任务，也没有后台注入。
    - 一律派工的变体被 `TOOL_ONE_SHOT_ALREADY_EXECUTED` 拦下；两种情形下根任务的子代理都只有一个。
  - 既有测试同步：`test_subagent_wake_keeps_objective_in_user_task_slot` 断言三元组；
    `test_subagent_wake_provider_prompt_keeps_original_task_after_runtime_injection` 改为新设计（不再有 `# User Task`，
    有原回合编号时原任务在历史里、只引用编号，没有编号时宿主事件附原文）。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_lifecycle_wake_host_event.py -q`。
- **变异验证**：27 个全部被抓出，每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，跑完逐字节恢复并核对哈希。
  - 传递链：不构造触发类型；执行层不写进 RunParams；RunParams→RuntimeLoopParams、RuntimeLoopParams→ToolLoopExecuteParams
    各自丢掉触发类型；推荐区请求不带触发类型；循环 prompt 请求不带触发类型；
  - 渲染：原生开头仍是用户任务；开头项位置不认宿主事件；缓存 canonical 与文本任务节各自忽略触发类型；首句丢失；
    首句不随 origin_task 切换；
  - 推荐：不用固定短名单；list_agents 不在第一位；不按本轮可见工具取交集；
  - 历史保存：唤醒片仍保存注入；普通回合也被去掉注入；
  - 事实投影：收进原始结果 JSON；不做长度上限；丢掉原回合编号；有历史请求时不再附 origin_task；
  - 任务来源：Goal 任务编号仍当历史请求；Goal 目标原文不附；active 或非 active 目标的上下文丢掉已核实来源；
    调用链不传已核实来源；没有请求编号时不附原任务。
- **相关回归**：与改动路径有关的 219 个测试文件（含 `test_background_main_agent_runtime.py`、
  `test_architecture_guardrails.py`）5043 passed、2 skipped、27 xfailed、5 xpassed。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package 均通过；code-size 身份差集相对基线新增 0、
  减少 1。`run_background_turn_with_compact` 曾因本次两行越过 100 行硬线，已把每次尝试的上下文准备和溢出携带合并抽成
  两个小函数，行为不变。
- **补修（集成者全仓发现）**：`lifecycle_wake_event` 的上限常数原名 `_TEXT_LIMIT`、`_LIST_LIMIT`，去掉前导下划线后与
  `host_notices.TEXT_LIMIT`、`settings_control_service.LIST_LIMIT` 撞名（概念不同），`test_constant_names_unique.py` 失败；
  三个上限统一改名为 `_WAKE_FACT_TEXT_LIMIT`、`_WAKE_FACT_LIST_LIMIT`、`_WAKE_FACT_DEPTH_LIMIT`，行为不变。原门禁清单漏了
  这类全仓扫描测试；之后的门禁固定带上扫描产品代码的守卫测试（含 `test_constant_names_unique.py`、
  `test_config_field_readers.py`、`test_main_agent_has_no_case_runtime.py`、`test_orchestration_tool_constants.py`）。

## 两条负载抖动用例改稳：桌面插件超时、慢流存活续期（2026-09-28，分支 `claude/38-stabilize-load-flakes`，基于 `8e82edcc0`，只改测试）

- **来源**：集成者报告 12 片全仓 5 遍里 1 遍出现 `test_desktop_lite_package.py::test_timeout_kills_program`（读不到
  `fake-record.json`）和 `test_slow_model_liveness.py::test_slow_stream_renews_wait_beyond_total_deadline`（慢流被判死）。
- **满载复现**（12 个 CPU 忙循环 + 6 个 pytest 进程并行各循环跑同一条）：桌面 7/36 失败，慢流 4/90 失败（3 次“被判死”，
  1 次终态文件读到半截被投影成 load_error 回执、`response` 为空串）。
- **桌面超时用例被打破的假设**：假程序原来是 Python 脚本，满载下解释器启动慢过 1 秒超时，被杀时还没写下 pid 记录；换成
  sh 先写记录再 `exec sleep`、超时放到 2 秒后仍 5/36（都发生在 6 个进程同时构建 wheel 那一刻，假程序还没被调度就已超时）。
  改法：杀掉并回收的直接证据改为“超时后插件服务进程名下没有任何子进程（含僵尸）”，按父 pid 用 `ps` 精确过滤，不依赖
  假程序是否来得及跑起来；记录存在时再按记录的 pid 复核一次。不 kill 会剩下还在睡的假程序、kill 后不 wait 会剩僵尸，两种
  都被抓到（负向验证）。
- **慢流用例被打破的假设**：不活跃窗口 0.12 秒只比轮询采样间隔 0.1 秒多 20 毫秒，另一个线程每 60 毫秒写一条，满载下生产者
  线程被饿几十毫秒、或轮询一次的文件 IO 超过 20 毫秒，就被判死。改法：流的推进由上一条 chunk 被消费触发（同一线程里写
  下一条），每次采样只能读到一条，8 条至少 8 个采样周期（约 0.8 秒），远超 0.05 秒的初始 deadline；续期语义仍由“初始
  deadline 远早于完成时间”证明——续期一失效，第二次采样就因过期返回空（负向验证）。不活跃窗口改为 2 秒只给采样抖动留
  余量，过期语义由 `test_no_activity_expires_after_inactivity_window` 单独锁定。终态文件 helper `_terminal` 改为先写临时
  文件再 `replace`，与 Gateway 的原子写法一致，本文件其它用另一线程写终态的用例也不会再读到半截。
- **修复后满载**：慢流 0/90（改前 4/90）；桌面 0/36（改前 7/36，sh 版中间稿 5/36）。最终代码上慢流满载再跑 0/90；
  12 片全仓（1204 个文件）1 遍 0 失败、315 秒；我起的进程全部退出。
- **负向验证**（改坏产品语义，独立子进程、逐字节恢复核哈希，4/4 被抓出）：活动不续期；把 chunk 推进当成不活跃；超时报成
  COMMAND_FAILED；kill 后不 wait（僵尸）。
- **门禁**：两个文件 ruff、doc sync、strict code-size、`git diff --check`、clean package。

## 入站队列 PostgreSQL 真测按 pytest 进程隔离 schema（2026-09-28，分支 `claude/38-stabilize-ingress-tests`，基于 `72f23d0d9`，只改测试）

- **来源**：集成者报告 main `72f23d0d9` 上 12 片并行时三条用例偶发失败、单独跑 3/3 通过：
  `test_ingress_queue_load.py::test_load_exactly_once_and_lane_serial_under_concurrency`、
  `test_ingress_queue_load.py::test_load_retry_and_dlq_under_concurrent_failures`、
  `test_ingress_queue.py::test_heartbeat_extends_lease_prevents_recovery[postgres]`。
- **满载复现**（8 个 CPU 忙循环 + 6 个共用本机 postgres 的测试文件各起一个 pytest 进程并行，3 轮）：修复前 3/3 轮
  `test_load_exactly_once…` 失败，日志里 worker 线程报 `relation "ingress_messages" does not exist`，只 drain 到 60/800。
  被打破的不是租约/心跳间隔，也不是连接数（本机 max_connections=100，峰值几十）：两个文件的 PG 用例都在 `public`
  里建同一张 `ingress_messages`——`test_ingress_queue.py` 的 fixture 每个用例前后 `drop_all`，`test_ingress_queue_load.py`
  每个用例开头 `DROP TABLE`——落到不同分片同时跑时互相删表。心跳用例用注入的 `now_ms`，本身没有计时假设，同样是被删表。
- **改法**：新增测试专用 `agent_py_agent/tests/_postgres_test_schema.py::isolated_postgres_url()`：每个 pytest 进程建一个
  `pytest_ingress_<pid>_<8hex>` schema，通过连接 URL 的 libpq `options=-csearch_path=…` 让产品侧不带 schema 前缀的建表、
  迁移、索引语句都落进本进程 schema；退出时 `atexit` 删本进程 schema，建 schema 时顺手清掉 pid 已死的同前缀残留；
  `TEST_POSTGRES_URL` 已带 `options=` 时原样返回。两个文件的 PG 后端都改走它，连不上仍按原来的方式 skip。
  产品代码、表名、迁移都没动。
- **drain 判定**：精确一次用例原来等内存里的 `processed` 计数到 800 就 `pool.stop()`，而 `complete()` 在 handler 返回后才
  落库，最后一条可能还在 claimed；改为等队列自己的 `completed` 终态计数（结构化事实），断言本意不变（不丢、不重、
  同 lane 串行、无残留）。90 秒超时保留：修复后满载下整文件 4 项 11 秒跑完，余量足够。
- **修复后满载**：同一复现脚本 3 轮全过（`test_ingress_queue_load.py` 4 passed ≈ 11 s，`test_ingress_queue.py` 15 passed）；
  12 片全仓并行（1204 个测试文件按大小轮转分片，本机 Mac）连跑 5 遍：三条目标用例及其所在文件 5/5 全绿，5 遍全仓 0 失败
  4 遍、1 遍有 2 个与入站队列无关的负载抖动（`test_desktop_lite_package.py::test_timeout_kills_program` 超时子进程没来得及写
  记录文件、`test_slow_model_liveness.py::test_slow_stream_renews_wait_beyond_total_deadline` 慢流被判死），单遍 292–383 秒。
  跑完本机 postgres 没有残留 `pytest_ingress_%` schema，我起的进程全部退出。
- **负向验证**（改坏产品契约，独立子进程、逐字节恢复核哈希，5/5 被抓出）：跳过 lane advisory 锁（同 lane 并发被抓）、
  心跳不续租（被回收被抓）、失败直接进死信不重试、失败永远重试不进死信、`complete` 不校验 claim token。
- **门禁**：三条用例所在文件、`_postgres_test_schema` ruff、doc sync、strict code-size、`git diff --check`、clean package。

## 后台整合档与 goal 子代理档补齐直属下级管理面（2026-09-28，分支 `claude/be-wake-turn`，基于 `2163629df`）

- **来源**：T3 真实 TUI 验收的观察 2（C 部分）。子代理生命周期唤醒走 `subagent_integration` 档，也就是手写的
  `DEFAULT_BACKGROUND_ALLOWED_TOOLS` 这 17 个工具；直属下级管理面 `DIRECT_CHILD_CONTROL_TOOLS`（创建、只读状态、插话、
  取消、权限答复）只进了 4 个，唯独缺 `list_agents`。原因是 08-21 为防“查树 + sleep”轮询删掉 `inspect_agent_tree` 后，
  08-30 新增的 `list_agents` 进了前台和协调者目录，后台表没跟上。
- **改动**：`background_tool_policy` 的整合档与 goal 子代理两档（active/terminal）改由 `_with_direct_child_controls`
  从 `DIRECT_CHILD_CONTROL_TOOLS` 派生：原目录顺序不变，只在末尾按原顺序补缺，现在补的是 `list_agents`。`list_agents` 有给
  模型的用途说明（只读、不等待、不推进，变化仍由宿主唤醒送达）。其它档（默认、紧急、定时、审计、无子代理的 goal）不变。
  owner 禁用、任务白名单、显式配置和退休过滤仍只做减法。
- **测试**（`test_background_child_control_tools.py`，8 项）：
  - 三个档在策略收紧前都含全部直属管理工具、没有重复，每个工具都有用途说明，提示行里有 `list_agents`；
  - 三个档都保持原目录前缀，只在末尾补上缺的管理工具；
  - 收紧只做减法：owner 禁用能去掉 `list_agents`（`removed_tools` 如实记录），任务白名单只取交集（名单外的名字不会加进来），
    显式配置是精确名单；
  - 真实后台 `_run_params` 与注册表 `runtime_snapshot` 里，生命周期唤醒片确实能用 `list_agents`。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_background_child_control_tools.py -q`。
- **变异验证**：9 个全部被抓出（同样在独立字节码缓存的子进程里跑，按字节恢复并核对哈希）：
  三个档分别不派生；管理工具加在前面（改了原顺序）；删掉 `list_agents` 的用途说明；任务白名单改成并集；
  忽略 owner 禁用；显式配置也补管理工具；`list_agents` 被列为退休工具。
- **相关回归**：引用后台工具策略、后台提示词、生命周期唤醒或 `list_agents` 的 33 个测试文件（含
  `test_architecture_guardrails.py`）1084 passed、1 skipped、4 xfailed。
- **门禁**：ruff、doc sync（`--base 2163629df`）、strict code-size、`git diff --check`、clean package 均通过；
  code-size 身份差集相对 `2163629df` 新增 0、减少 0。

## TaskRun 收口允许静止但未终态的子 run（2026-09-28，分支 `claude/38-taskrun-settle`，基于 `d0318486e`）

- **来源**：dsh-be 的 TaskRun 收口分析（集成方认可）：子代理 BLOCKED 后 `agent_runs.status` 停在 created 是设计，唯一受影响的是
  `settle_task_run_if_agent_tree_terminal` 要求整棵树终态，父 TaskRun 永远不关，`open_task_runs` 只增不减。
- **新测试** `test_task_run_settle_quiescent_children.py`，10 项，全部走真实 `RuntimeRepository`（子 run 按真实派工路径：登记 pending →
  `create_attempt(reuse_pending=True)` 激活并持锁；BLOCKED 用 `settle_agent_attempt` 关 attempt、删锁）：
  - 根 done + BLOCKED 子 run：TaskRun 关闭并写一条 `task_run.closed`，payload 带 `quiescent_agent_run_count=1` 与子 run ID，
    `open_task_runs` 清空，子 run 本身仍是 created；
  - 该子 run 随后 `create_attempt`：写 `task_run.reopened`（attempt/agent_run 绑定、previous_status/closed_at），再次收口返回
    `agent_tree_active`，子 run 真终态后第二条 `task_run.closed` 的静止计数为 0；
  - attempt 正在跑、attempt 已终态但执行锁还在、根未终态：三种都仍返回 `agent_tree_active`，不写事件；
  - unknown 且锁已不在：仍 `agent_tree_active`（结果不明要等显式恢复，集成方定）；recovered 且锁已不在：算静止；
    没有任何 attempt 的子 run 保持开放；
  - 发现扫描 `unfinished_task_ids` 能把存量 open TaskRun 关掉，幂等，operator 记 `wake-discovery-task-run-reconcile`；
  - pending 激活的 `agent_run.started` 事件 status 与 `agent_runs` 列一致（created），`agent_attempt.started` 仍记 running。
- **先红后绿**：修复前 10 项中 6 项失败（含发现扫描与事件修正），另外 4 项（活跃形状、unknown 无锁、无 attempt 子 run）本来就绿。
- **旧用例**：`test_run_audit_terminal.py`、`test_r103_ledger_selfheal.py`、`test_host_command_execution.py`、
  `test_host_command_operation_replay.py`、`test_runtime_db.py`、`test_runtime_db_main_chain.py` 原样通过。
- **变异验证**：13 个变异全部被抓出。每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，
  跑完逐字节恢复并核对哈希：去掉锁检查、去掉 attempt 结算检查、根按子 run 判、终态 run 不跳过、不记静止 ID、计数多一、
  只认 done attempt、unknown 也算静止、活跃判定取反、锁 scope 不带前缀、活跃原因改名、run started 事件仍写 running、
  无 attempt 也算静止。

## 生命周期唤醒片续接前台轮的工具事实（2026-09-28，分支 `claude/be-wake-turn`，基于 `d0318486e`）

- **来源**：T3 真实 TUI 验收的观察 2。round1 归档复现：前台 Gateway 轮成功的 `create_subagents`（call_ac_3）只写在
  owner 根自己的工具索引 `blobs/tool_outputs/index.jsonl`；唤醒片只读任务 `work/blobs/tool_outputs/index.jsonl`，
  续跑重建的一次性编排去重集合里没有它，唤醒片里同样内容的派工照样成功，多出一个子代理。
- **改动**：
  - 查找根：`_background_active_turn_tool_calls` 有请求编号时同时读 owner 根索引和任务 work 索引。owner 根只取任务
    `run_workspace.json`（`run_workspace.v1`）的 `owner_home`，且任务根必须在它下面（`run_workspace_owner_home`），
    否则不读 owner 索引。
  - 范围：`carried_tool_call_records_for_requests` 按精确 `conversation_request_id` 过滤（旧记录沿用 request_id
    同值规则）。每个根只读自己的 index.jsonl，不展开到其它 runs；逐行流式读取，先按请求编号做字节预筛（只省解析），
    再按结构化 scope 精确匹配，不把整份索引读进内存。
  - 用途分层：owner 索引的记录带 `carried_runtime_only=True`，只进运行时状态重建（一次性编排去重、已执行工具、
    已加载工具、工具轮数），不进本片 `archive_tool_calls`、模型可见交接和压缩来源；前台轮的原生工具对已经在会话
    历史里重放。片内溢出压缩替换携带内容时，这些记录原样保留（`compact_overflow_carry`）。
  - 没有请求编号的旧唤醒仍按 task_id 只读任务 work 索引，行为不变。
  - 拦截提示：去重范围变成整个活动回合后，`TOOL_ONE_SHOT_ALREADY_EXECUTED` 的拦截文字与错误合同恢复提示补一句：
    确需另派子代理接替已有 run 时，在 `replacement_for_run_ids` 里写明被接替的 run_id。放行只看结构化意图键。
- **工具轮预算**：
  - 工作片起点 `tool_rounds` 等于携带条数（现在含前台轮记录）；`_extend_background_slice_tool_budget` 把本片绝对上限
    加上同样的条数，新增额度仍是 `background_max_tool_rounds`（随包 5000，代码缺省 32），不会一开始就触顶。
    测试锁定上限 = 基线 + 携带条数。
  - 长会话：每次生命周期唤醒都顺序读一遍 owner 索引，内存只放命中行，读取时间随索引大小线性增长；前台轮工具越多，
    起点和绝对上限一起抬高，新增额度不变。重复调用门、完成提醒看到的已执行工具包含前台轮，这是同一回合的本来语义。
  - 例外（按代码推断，未单独跑）：`background_max_tool_rounds` 显式写 0 时不写片上限，`_effective_max_tool_rounds`
    回落到全局 `max_tool_rounds`；它是正数 N 时整条活动回合（前台加各唤醒片）共用 N，且不加携带条数，前台已用满
    N 轮时唤醒片会一开始触顶。这个口径在本次之前就存在（多次唤醒累计也会触发），本次让单个前台轮就能触发。
    修它要改 `_apply_internal_background_tool_budget`，不在本次允许改动的函数里，记为后续项（已由同日预算条修掉）。
- **测试**（`test_background_active_turn_carry.py`，5 项）：
  - 精确范围：owner 索引与任务索引只取本请求的记录；别的请求、只在参数里提到本请求编号的记录、坏行、别的 run 的
    索引都不进来；owner 记录带运行时标记，任务记录不带；空请求编号返回空。
  - owner 根：取自 `run_workspace.json`；指向别处、版本不符、字段为空、文件缺失都返回 None。
  - 真实 `_run_params` 链：携带记录顺序和标记正确，本片上限 = 基线 + 2。
  - 工具循环参数：前台那次 `create_subagents` 进一次性编排去重（同内容派工判为重复），写明 `replacement_for_run_ids`
    的重派不算重复，拦截结果带错误码并指出这条路径；工具轮数和已执行工具都计入；本片工具账和 tool_context
    只有唤醒片自己的记录。
  - 溢出压缩携带：结果归档替换旧快照时运行时记录保留在最前；结果为空时原样返回。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_background_active_turn_carry.py -q`。
- **变异验证**：17 个全部被抓出，每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，
  跑完逐字节恢复并核对哈希。
  - 读取：丢掉 scope 精确匹配；字节预筛取反；owner 查找改用会展开到所有 runs 的旧读取；关掉去重（由既有
    `test_memory_compact_tool_output_refs.py` 抓出，去重口径随读取入口拆分后不变）；
  - 标记：不加运行时标记；owner 记录不标；任务记录也标；不读 owner 索引；
  - owner 根：去掉“任务根必须在 owner 下”；去掉版本检查；
  - 循环：本片工具账不过滤；运行时状态只从本片工具账重建；
  - 压缩携带：替换时丢掉运行时记录；替换时整份旧携带都留下（重复）；
  - 预算：上限只按非运行时记录加；
  - 拦截：意图键忽略 `replacement_for_run_ids`（接替重派也被拦）；拦截文字去掉接替路径。
- **相关回归**：与改动路径有关的 142 个测试文件（含 `test_background_main_agent_runtime.py`、
  `test_architecture_guardrails.py`，以及引用一次性编排去重、错误合同的测试）3301 passed、1 skipped、27 xfailed、
  5 xpassed。
- **门禁**：ruff、doc sync、strict code-size、`git diff --check`、clean package 均通过；code-size 身份差集相对
  `d0318486e` 新增 0 条、减少 1 条（`carried_tool_call_records` 的嵌套项随拆分消失）。

## 凭据类字符串配置补类型校验（2026-09-28，分支 `claude/be-credential-types`，基于 `8ef68c5fd`）

- **来源**：T3 真实 TUI 验收。凭据键写错类型时没有任何配置告警：`embedding_api_key` 写成列表会原样进入运行配置，
  `feishu_app_secret` 写成列表会被静默变成空串。`/settings` 的“配置告警 N 条”只列出了已删的键。
- **改动**：`settings/services/_normalize.py` 新增 `CredentialFieldsService`，排在归一服务最前面。
  - 名单：`credential_string_fields(AgentConfig)` 从配置类声明推出（键名是凭据且声明为 `str`），不另写。
  - 口径：
    - 字符串原样保留；
    - 整数沿用“纯数字没加引号也按字符串还原”；
    - `None` 与留空读成的 `[]` 取默认值、不告警；
    - 其它类型告警并回落默认值，告警经 `describe_raw_value` 输出，凭据一律写“已隐藏”。
  - 之后的飞书/QQ 字符串还原只会看到字符串，不再静默吞掉错误类型。
- **测试**（`test_config_warning_no_credential_echo.py`）：
  - 名单锁：已知的 7 个凭据字段都在，且都是凭据键。
  - 每个凭据字段各测四种错误类型（列表、字典、布尔、浮点）：只出一条告警，文字固定为“已隐藏”，原值不出现，运行值等于默认值。
  - 整数按字符串还原；None 和 [] 取默认值且不告警；字符串原样保留。
  - 端到端：真实 `load_config` 后运行值回落默认值，`/settings` 总览显示“配置告警 2 条”，两个键都写“已隐藏”，原值不出现。
  - 旧用例 `test_settings_overview_shows_a_credential_warning_as_hidden`：原来只断言“不泄露”，现在补上它说明里写的“要留下痕迹”。输入改为带引号的列表，因为项目自带的 YAML 读取只把 `["..."]` 解析成列表，`{nested: ...}` 会读成合法字符串。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_config_warning_no_credential_echo.py -q`。
- **变异验证**：9 个全部被抓出。每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，跑完逐字节恢复并核对哈希。
  - 服务没注册；非字符串直接放行；告警回显原值；类型不符仍保留原值；
  - 整数也按类型不符处理；留空也告警；名单推导为空；
  - 服务排在飞书/QQ 字符串还原之后（错误类型会被先静默变成空串）；布尔被当成整数还原。

## C23真实委派及正式315门禁（2026-09-28）

C23实际运行330b：一条原生需求、三名直属子代理并行145.891秒，父三attempts自然结束，总430.178秒；一次create、两次后台续接、三份记录完整读回及汇总落盘已验。脚本不完整preview未恢复、交接误述仍为失败；不是整个样例通过，不抵扣最终0/27。六配置、354旧文件、140旧任务绑定、安装表及1433安装成员保持。证据摘要与未覆盖项见[C23验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c23三助手后台续接与交接2026-09-28)。

正式315相对运行版仅产品常量同值改名，修正作者原330b常量唯一性守卫失败。root直接读取12份正式原日志，23867 passed、21 skipped、32 xfailed、5 xpassed、0 failed/errors；五项静态严格gate均0，线上CI未作为验收来源。六文件272项独立组件结果及330b五静态gate分别记账，不相加或倒换版本。Mac step14t日志已核，Linux同版未独立确认。

建议下一步：固定正式运行字节后执行原冻结27次；只读证据审阅可并行，主线发布仍由Claude负责。本线本轮仅验收文档与私有观察证据，运行doc sync、diff和clean-package，不改产品或测试代码，不重跑全仓。

## 后台唤醒修订独立复核（2026-09-28）

固定 A `c4972cbd0` 的 `test_lifecycle_wake_host_event.py` 独立运行 18 项通过，0 failures/errors/skipped，2.624 秒；实际 import 来自固定独立 checkout，前后源码干净，使用私有 home/basetemp、假模型及 `-q --tb=short -p no:cacheprovider`。新增合法 Goal 来源矩阵及后台组件链断言覆盖原来源问题；不与旧 224 项累加为新组合结果。

B `9882db061` 的新 guard 经固定 AST 纯函数探针确认参数口径不一致：空白接替列表、item 显式空列表覆盖顶层接替，两例均未被拦截，但创建归一化后有效接替 ID 为空，接替预检也未读取任何来源；另三个正常对照保持。没有实际派工、模型调用或真实故障。旧失败证据保持。

修订 `24f8accb9` 复用创建层 item 合并和统一 ID 归一化，独立窄审确认两例修复。root 在固定 checkout 以隔离 home/basetemp 运行六文件：`test_lifecycle_wake_host_event.py`、`test_background_main_agent_runtime.py`、`test_background_active_turn_carry.py`、`test_background_child_control_tools.py`、`test_orchestration_create_subagents_items.py`、`test_orchestration_create_subagents_items_policy.py`；272 项通过，0 failures/errors/skipped，26.884 秒，实际 import 来自固定 checkout，源码前后干净。报告摘要 `ba66092fa00fdf57d196fa09e190f6726fa93c82c2436ca749cf603b7784536e`；只覆盖合同及假模型组件，不替代最终整组门禁或原生验收。

12:32 UTC 核实移植 `0714b250c` 的产品目录与两份相关修改测试同字节，作者仍在补 carry 测试。详见[证据摘要与范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#后台唤醒修订复核2026-09-28组合待完成)。建议下一步整组固定及门禁后由 root 做 C23；只读复核可并行，最终仍 0/27。

## 后台唤醒 A 草稿组件与 Goal 来源投影（2026-09-28）

固定 WIP `51361723d`（父 `72f23d0d9`）上运行原作者 A 测试、后台主代理 runtime 及 B/C 两文件，共 224 项通过、0 failure/error/skip；实际 runtime import 与测试文件均在固定独立 checkout，前后干净。调用 `pytest.main`，参数为上述四文件、`-q --tb=short -p no:cacheprovider`、私有 `--basetemp` 和 JUnit 文件。没有产品/测试代码修改或模型请求。

独立审阅另发现合法 Goal 的 task ID 被新事件误标为历史 request 引用。真实隔离 store 的纯解析复核中，4 个合法 Goal 场景误标，2 个缺失普通请求场景仍按原合同拒绝；原目标仍在 user_prompt，不声称整个请求丢目标。224 项没有断言这处角色区别，不能用组件通过覆盖它。详见[固定草稿、证据摘要和范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#后台唤醒-a-草稿来源角色审阅2026-09-28未正式交付)。建议下一步由原作者修复 A 来源角色及 B 读失败，root 等正式固定组合后做 C23；只读复核可并行，最终仍 0/27。

## 后台唤醒 B 读账异常离线复核（2026-09-28）

固定 `43cd72e3d` 的原运行入口和三个 reader 函数 AST 未改写，以合成 iterator 复核两种情况：正常返回 owner/task 两条 carry；只有 owner 抛 OSError 时，外层返回空列表且 task 根未被访问。确认读账事实丢失，不声称真实重复派工或停止失效；没有制造真实故障、发模型或改产品。

C `72f23d0d9` 首轮只作静态审阅；随后在固定该提交的独立 checkout，实际调用 `pytest.main`，参数包含下列两文件、`-q --tb=short -p no:cacheprovider` 及私有临时 `--basetemp`：

- `agent_py_agent/tests/test_background_active_turn_carry.py`：5 项通过。
- `agent_py_agent/tests/test_background_child_control_tools.py`：8 项通过。

合计 13 项、退出 0，runtime import 路径已核，前后 checkout 干净；仅覆盖原作者已有组件用例，不含 A 和读账异常修复，不代表原生委派或整组通过。私有报告摘要 `b2f0d9bddc86b1fcc3cbdef02830e36a11c70f1eeeda7bce90d22258ad5c70d5`。A 未固定，C23 仍零提交，详见[边界与原作者交接](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#后台唤醒前置补片审阅2026-09-28整组未交付)。建议下一步由 Claude 补读失败边界并固定整组，root 再做必要组合与原生委派；只读审阅可并行。

## C22连续压缩后的原资源执行复验（2026-09-28）

- 固定 8ef 的 G02 修复复验，沿用原输入/普通需求和既有 65536 profile，新 CAP06 仅提交一次，336.314 秒自然终态，18 HTTP/0 重试；原始账本及产物摘要经独立审阅和 root 核对。
- 同请求四代自动提交，0→1→2→3→4 链及 thread head 一致。第 2 代后两次 get、完整 source_ref 复制及原检查器执行通过；两份脚本与固定 A/B 同字节，pins、输入、六配置和安装表保持。第 4 代后没有工具调用，不补算默认 262144 长任务。
- 实际命令退出 1/2/1，捕获完整；A 是未知来源引用，B 是先错参数后错输入 schema。保存报告 JSON 与 stdout 值相等，A 重排、B 仅尾换行不同；检查程序执行通过不等于检查数据合格。
- 交接错误经独立确认，业务未过；既有 C17/C21 失败与最终 0/27 保持。本轮无产品/测试/配置变更，固定 8ef 的相关 135 项结果保持，不新增镜像测试或重复全仓。本轮 Ruff、doc sync、strict code-size（hard=0）、diff、clean-package 均退出 0；线上 CI 未作为验收来源。
- 详见[真实分项和私有证据摘要](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c22固定校准后的连续压缩与原资源执行2026-09-28)。建议下一步核 Claude 固定唤醒补片并完成委派复验，再执行最终矩阵；只读审阅可并行，私有运行归 root，发布归 Claude。

## C21固定8ef组合及原生开发分项（2026-09-28）

- 隔离固定源码的 `test_compact_calibrated_candidate_gate`、`test_compact_capacity_host_chain`、`test_compact_capacity_facts`、`test_active_turn_compact_projection`、`test_compact_request_projection`、`test_compact_source_lifetime`、`test_mixed_compact_contract`、`test_runtime_context_pressure` 共 135 项通过；独立只读复核未发现确定阻断。
- Claude step14m 的 12 份原始日志合计 23761 passed、21 skipped、32 xfailed、5 xpassed、0 failed；Ruff、doc sync、strict code-size（hard=0）、diff、clean-package 均 0。本地严格 gate 已通过，线上 CI 未作为验收来源；root 没有另跑全仓。
- 私有安装 1431 个文件与固定源码/wheel 一致，pip check 通过；停稳备份后的 15106 个旧文件在重启前保持。运行后六配置及安装表不变，日用默认未改。
- 单次 C21、官方 M2.7/131072、576.204 秒自然终态。第 1 代由 `tool_context_overflow` 自动触发，102708→39581；B pin 跨一代保持，提交后继续 19 次工具调用。连续两代、压缩后包 get/原程序执行仍未覆盖。
- B checker 副本非原字节、A 未采用；输入 project 被代理调试命令损坏。最后 11/11 是被测代理自建测试的实际输出，但不足以证明正确映射、摘要验证、完整报告或可搬迁交接，业务未通过；观察者没有代跑或修复产物。
- 本轮 root 只更新验收文档和私有证据，不修改产品/测试代码，不推送或部署主线。最终仍 0/27，详见[完整分项](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c21固定8ef的离线交接开发验证2026-09-28)。建议下一步先交接真实缺口，再补必要自然覆盖；只读审阅可并行，私有运行归 root，产品及发布归 Claude。

## Compact 候选接受门按预检的校准口径计量（2026-09-28，分支 `claude/38-compact-calibration`，基于 `59fdcabbf`）

- **来源**：集成者对压缩异常②的决定（候选投影与预检同一校准口径；失败路径不用原始值覆盖线程快照；started 事件带触发来源）
  和 Codex 的三条生命周期接缝审阅（本轮追加口径不会压小候选；提交后清观测会让下一次预检换口径；校准水合有副作用、投影须保持纯函数）。
- **新测试** `test_compact_calibrated_candidate_gate.py`，26 项：
  - 纯校准函数：候选比观测小按比例折算（8_000 → 5_600）、下限 50%、比观测大沿追加口径；与预检 `_provider_calibrated_context_tokens`
    对同类输入逐项相等，并明确记下本轮口径对压小请求原样返回的分歧；无观测/坏观测原样返回。
  - 宿主边界冻结：同 fingerprint 的本轮观测、同代次的耐久观测才冻结；表面不符、代次不符、缺观测都是 None，冻结不改写状态。
  - transcript 接受门（`_transcript_case`，上限 6_000）：估算偏高 43% 时原始 8_000 的候选被接受，checkpoint/进度记 5_600、压缩前 9_000，
    原始纯投影保留；无校准仍按 8_000 拒绝；折算后恰好等于上限（8_571 → 6_000）拒绝；折算后低于触发线但超过输出预留线仍拒绝。
  - 活动回合接受门（`carried_case`）：原始 9_500 折算 6_650 接受、压缩前 20_000 沿追加口径 17_000；比观测大的候选按追加口径
    11_999 → 8_999 接受、12_000 → 9_000 拒绝；输出上限 7_000 时 4_284/4_285/4_286 → 2_999 接受、3_000 拒绝、3_001 拒绝。
  - 触发来源：白名单只收 `preflight` / `provider_error` / `tool_context_overflow`，未知与缺失整项不带；两条链的 started 事件带它；
    工具窗口溢出的 `runtime_source` 是结构化的 `tool_context_overflow`。
  - Gateway 全链两回合假 LLM 复现（隔离 home，窗口 90K、输出预留 10K、上限 80K，供应商 usage 改成本地估算 × 0.7）：
    第一回合放得下并留下耐久观测；第二回合大段新需求越线。`calibrated_fit`：压缩前原始 ≈ 104K/折算 ≈ 85K、候选原始 ≈ 92K/折算 ≈ 73K，
    候选被接受，只多一次业务发送，正文带摘要不带旧资料，提交后真实预检写下的线程快照正好等于接受时的折算值（接缝 2）；
    `calibrated_too_large`：折算后仍超上限，失败事件 `candidate_tokens` 是折算值，线程快照是折算后的压缩前大小而不是原始估算；
    `no_observation`：全部原始口径，行为与修复前相同。修复前 `calibrated_fit` 同样报 `COMPACT_CANDIDATE_TOO_LARGE`（先红后绿）。
  - 探针复核（提交前删除的临时用例，同一夹具）：第一回合原始 63,152 → 观测 44,206；两回合之间要按 Gateway 终态同一结构化调用
    `claims.finish` 释放请求钉住的执行车道，否则第二个请求会一直等车道（这是测试直接调 `_run_gateway_ask` 的约束，不是产品问题）。
- **改写的旧断言**：`test_runtime_context_pressure.py::test_preflight_context_pressure_uses_tool_context_window_signal` 的
  `runtime_source` 从 `preflight` 改为 `tool_context_overflow`（结构化来源单列，detail 文本不变）。
- **门禁**：`test_compact_calibrated_candidate_gate.py`、`test_compact_capacity_facts.py`、`test_compact_output_reserve.py`、
  `test_active_turn_compact_projection.py`、`test_compact_capacity_host_chain.py`、`test_runtime_context_pressure.py`、`test_compact_progress.py`、
  三宿主恢复与原生 IR 压缩用例、`test_memory_runtime_compact_auto_continuation.py`、`test_architecture_guardrails.py`；
  ruff、doc sync、strict code-size（对 main 的发现身份不新增）、`git diff --check`、clean package。


## Compact 容量计量改走宿主“只计量、不提交”入口（2026-09-28，分支 `claude/be-compact-capacity`，基于 `f5036c15a`）

- **来源**：集成者转来 Codex 的离线复现（`compact-2214052`）和对中间补丁的复核（`compact-author-patch-review-20260928T080805Z`）。
  - 真实宿主里 `fixed_tokens` 恒为 0：固定开销那一版是空摘要，撞上 `replace_recovery_active_tools` 的非空摘要合同，
    投影报 `COMPACT_REQUEST_PROJECTION_UNKNOWN`，然后被记成 0。
  - `retained_ir_*` 按工具来源保留区计，把候选会整体替换的旧会话摘要和旧工具交接也算了进去：来源 7 条 220 tokens，
    候选实际发送 5 条 148 tokens。
  - 中间补丁还有两处：活动回合仍记 0；保留 IR 把所有 `CompactionSummary` 都排除了，漏掉来源为空、实际照常发送的那条。
- **改动**：
  - 新增显式的“只计量、不提交”入口 `ConversationCompactView.measure_only`，`PreparedCompactRecovery` 两个入口都接上：
    - transcript 入口：投影器见到 `measure_only` 视图就走 `_measure_only_projection`。它不进候选替换，不参与接受门，
      交回的是不可提交的占位材料。
    - 活动回合入口：经新字段 `ActiveTurnArchiveCompactRequest.fixed_request_projector` 传入同样的只计量投影；
      候选投影器不再被拿去投影空摘要。
    - `replace_recovery_active_tools(measure_only=True)` 只在这条路上放宽空摘要，普通候选的非空摘要合同不变。
  - 固定开销只在真正抛出 `COMPACT_CANDIDATE_TOO_LARGE` 时量一次：从 `_RejectedCandidates._capacity()` 挪到 `capacity()`。
    前者每次拒绝都会走，后者只在抛出前调用，所以成功路径不再多投影。
  - 测不出时 `fixed_tokens=None`：`compact_failure_progress_fields` 整项省略，normalizer 和 TUI 失败行都不显示。
    两条压缩链一致，活动回合原来的“记 0”也改掉了。
  - 保留 IR 按候选实际要发送的材料计：
    - 宿主投影把候选材料里的原生 IR 放在 `ConversationCompactProjection.retained_ir_history` 交回；
    - `compact_tool_summary.sent_retained_ir` 只去掉候选会整体替换的 `applied_compact` / `carried_tool_handoff` 两种载体，
      其它来源（包括来源为空）的 `CompactionSummary` 照算；
    - 投影器没交回时，按同一口径从来源保留区推出。
- **新测试** `test_compact_capacity_host_chain.py`，14 项，走真实 `PreparedCompactRecovery`。只替身摘要和末端 HTTP，
  需要时把输入上限压到 1：
  - transcript 入口，三宿主（gateway / child / background）都由输出预留自然拒绝候选：
    - 固定开销经真实联合替换入口只量一次，0 < 固定开销 < 最小候选；
    - 保留 IR 等于最小候选实际发送的 IR；
    - 最小候选不留原话尾部时，候选 − 固定开销 − 摘要 − 保留 IR 只剩几百 token 的摘要包装。
  - 联合来源（历史 + 携带归档）和活动回合入口（原生 IR、携带归档两种）：
    - 失败时断言同上；
    - 成功提交时零次只计量投影；
    - 来源保留区带旧交接时，按来源计会多算，实际计量不算它。
  - Codex 复核用的同一夹具：来源为空的 `CompactionSummary` 照算，共 5 条，token 也包含它。
  - 真实联合替换入口不带 `measure_only` 时仍拒绝空摘要。
  - `sent_retained_ir` 与 `_replace_compact_history` 等价：投影原样保留（按对象身份）的，正好是它留下的条目。
- `test_active_turn_compact_projection.py` 新增 2 项：只计量入口缺失或抛错时字段缺失。
- **改写的旧断言**（逐条说明，都不是放宽）：
  - `test_compact_capacity_facts.py::test_fixed_overhead_projection_failure_keeps_zero_and_the_original_error`
    改名为 `..._leaves_the_field_missing`。按新规则“测不出不写 0”，`fixed_tokens == 0` 改成 `is None`，
    并加断言：公开进度没有该字段，TUI 失败行不显示固定开销；其它计量照旧。
  - `test_active_turn_compact_projection.py::test_complete_request_at_or_above_trigger_cannot_commit`：
    原先断言候选投影器被拿去投一次空摘要，现在断言它只见真实摘要，固定开销走只计量入口一次。
  - `test_compact_request_projection.py`、`test_compact_source_lifetime.py`、`test_mixed_compact_contract.py` 三条都是成功提交路径。
    原来断言多一次固定开销投影（4 / 6 / 4 次），现在改为不多投（3 / 5 / 3 次）；前两条另断言没有 `measure_only` 视图。
  - `test_compact_capacity_facts.py` 与 `test_compact_output_reserve.py` 的其余用例原样通过。
- **变异验证**：最终代码上 22 个变异全部被抓出。每个都在 `PYTHONDONTWRITEBYTECODE=1`、独立 `PYTHONPYCACHEPREFIX` 的子进程里跑，
  跑完逐字节恢复并核对哈希。
  - 只计量入口：不放宽空摘要、对所有候选都放宽、宿主投影器忽略 `measure_only`、活动入口不传只计量回调、
    活动入口计量时保留工具记录、计量交回可提交材料、计量不做工具/IR 替换、transcript 计量视图不带 `measure_only`；
  - 计量时机：`capacity()` 丢掉固定开销；每次拒绝都量（成功路径也量）；每次拒绝量一次、失败时再量一次；
  - 缺失语义：transcript 测不出写 0；活动回合测不出写 0；活动回合没入口写 0；进度字段保留 None；
  - 保留 IR：`sent_retained_ir` 不过滤；漏掉交接来源；漏掉摘要来源；过滤所有 `CompactionSummary`；
    transcript 失败忽略实际发送 IR；两个入口共用的候选计量 `_candidate_projection` 不交回实际发送 IR；活动失败退回来源口径。
  - 过程说明：第一轮“活动失败退回来源口径”存活，因为原生 IR 夹具的来源保留区里没有旧交接；补了携带归档的活动入口用例后被抓出。
    第一轮还有 1 个等价变异：只让活动宿主不附带实际发送 IR。活动入口一定有工具来源（没有就在 `select` 里 noop 或报错），
    计划里的保留 IR 就是来源保留区；真实替换原样保留其中的非载体条目，回退口径算出的结果与实际发送相同，由等价用例锁定。
  - 为守住代码尺寸基线，两个入口的“计量 + 附带实际发送 IR”随后收进共用的 `_candidate_projection`，上面那个单入口变异点随之消失；
    活动入口的只计量回调挪成模块级 `_active_fixed_projector`。最终的 22 个变异按新位置重跑。
- **复现**：`python3 -m pytest agent_py_agent/tests/test_compact_capacity_host_chain.py agent_py_agent/tests/test_compact_capacity_facts.py agent_py_agent/tests/test_active_turn_compact_projection.py -q`。

## TUI 插话终态未确认时停止轮询（2026-09-28，分支 `claude/be-steer-terminal`，基于 `d786e14bb`）

- **来源**：`claude/be-steer-loss` 的尾巴。Gateway 入口回执收成 `terminal_unknown`（目标回合已结束、无法证明模型确认过）以后不会再变，
  TUI 却一直按“未知”重试查询。生产上那条丢失的插话已经查询了上千次。
- **改动**：解码函数看到 `input_state=terminal_unknown` 时给出新的终态 `UNCONFIRMED`；对账器收到后删掉待发箱这一行、停止查询；
  界面撤下等待项，在历史里写一行“插话未获模型确认，已停止等待，不会自动重发”，附简短原文。不排队、不重发、不碰 guidance 账本。
- **新测试**：
  - `test_chat_client_context.py::test_input_status_terminal_unknown_is_a_final_unconfirmed_result`：`terminal_unknown` 解成
    UNCONFIRMED，`active_pending` 仍是 UNKNOWN。
  - `test_tui_input.py::test_terminal_unknown_active_input_stops_polling_and_shows_final_state`：真实对账线程只查询一次，
    等待项撤下，历史有终态行，待发箱清空，不产生排队任务。
- **变异验证**：6 个全部被抓出，包括解码忽略终态、所有未知都当终态、对账器继续轮询、不写终态行、不撤等待项、当成拒绝重新排队。
  每个都在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑，并逐字节恢复。

## TUI 插话丢失修复（2026-09-28，分支 `claude/be-steer-loss`，基于 `f7851cec8`）

- **来源**：集成者派活，用户反馈 TUI 插话经常没进去。按规定只读结构化事实：插话回执的状态、编号和时间，模型提交与确认批次，
  运行库的 attempt 和事件类型，Gateway 入口回执，TUI 本地待发箱的编号和次数。不读会话或记忆正文。
  - 生产近 14 天主工作区共 80 条插话：70 条模型确认，9 条在回合结束后按设计拒绝、排到下一轮并已跑完，
    1 条是 09-27 22:54 的 `guidance-31b4…`。
  - 这 1 条随 `attempt-1790574840` 的模型调用提交，调用 `ProviderTransientError` 失败，attempt 失败；
    `attempt-1790575040` 只带了后来的那条插话。它的入口回执至今停在 `active_pending`，TUI 待发箱已查询上千次。
  - 结构化证据在 scratchpad `steer/evidence/`，汇报时附归档路径。
- **新增** `test_steer_delivery_recovery.py` 6 项，假后端，零网络（端到端只连进程内 127.0.0.1 临时端口）：
  - 失败调用把本批插话退回预留，批次记 `rejected`，守卫放行；下一次调用按新编号提交，确认后消费；
    拿过期调用编号退回是空操作。
  - 墙钟超时后物理重试重新提交；被放弃的旧调用不被确认。
  - 工具循环里瞬断后模型轮重试：两次出站各含一次插话，最终回复按插话调整，`active_turn_user_inputs` 只记一次。
  - 流中止（换成合成回复）不退回，普通失败才退回。
  - 隔离 Gateway 端到端：用生产同款鉴权中间件起进程内 HTTP 服务，真实 TUI 客户端 `GatewayChatClientAgent` 在模型调用进行中
    插话；带插话的调用瞬断后本轮重试成功，入口回执收成 `consumed`，`/input-status` 返回 accepted。
  - 定时任务目标（没有 Gateway 请求文件）：任务运行中回执保持 `active_pending`；任务 `completed` 后插话被拒绝，
    转成下一轮请求（inbox 出现同编号请求，状态接口 queued/accepted）。
- **改写** 2 个旧用例，它们锁的是旧规则“只要注入过插话就拒绝重试”：`test_tool_loop_model_turn.py` 的 pending 参数改为可重试；
  `test_provider_timeout_acceptance.py` 拆成“在途提交拒绝重试”和“只有预留允许重试”两条。
- **复现**：同一测试文件放到基线 `f7851cec8` 的只读导出上跑，6 项全部失败。端到端用例在基线上回合整体失败，
  和生产上 attempt 以 ProviderTransientError 失败一致。
- **结果**：用到插话、生成层、Gateway 入口、瞬断重试的 141 个测试文件（含架构守卫），共 8379 passed、1 skipped、5 xfailed、1 xpassed。
  xpassed 的是 `test_timeout_budget_locked.py::test_native_protocol_unified_counts_ir`（token 计量存量漂移），在基线上同样 xpass，
  与本改动无关。ruff、doc sync、strict code-size（与基线逐条比较，新增 0、少 1 项 high-risk）、`git diff --check`、clean-package
  见提交前检查。
- **变异验证**：11 个全部被抓出。
  - 重试守卫把预留也算在途；失败调用不退回；过期编号也退回；包装层不退回；流中止也退回；退回出错盖住原异常。
  - 不回退到任务状态；任何任务都算终态；没有记录算终态；读不出算终态；周期对账仍用旧判定。
  每个都在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑，并逐字节恢复。两个等价变异没列入：
  - 清在途编号时不核对是否仍是这次调用：失败路径进入前已核对过；
  - 超限错误也在包装层退回：原恢复链会再退回一次，结果相同。

## 压缩失败诊断增加实测"固定开销"与保留 IR 计量（2026-09-27，dev 派活，my-agent 实现）

- **来源**：G2 复验里第二代自动 Compact 报 `COMPACT_CANDIDATE_TOO_LARGE` 时，只有候选总量、输入上限和摘要占比，
  分不清"是摘要太长"还是"碰不到的固定部分太大"。dev 在本板派活（07:50 / 08:01 两条）要求补三个数：
  实测固定开销、非工具归档保留 IR 的条数与 token，且**都不许用两个估算相减**，只在失败路径算。
- **做法**：`CompactCapacityFacts` 增加 `fixed_tokens` / `retained_ir_items` / `retained_ir_tokens`，字段名同步进
  `compact_progress.COMPACT_CAPACITY_PROGRESS_FIELDS`（旧事件缺这些字段时照旧不出现，不补零不推断）。
  - `compact.py` 新增 `_fixed_request_tokens(request)`：用**同一投影器**把"空摘要、无任何保留"那一版完整下一请求再计量
    一次（宿主没给投影器时走同一本地估算），结果缓存在 `_RejectedCandidates`；投影异常记 0 而不抛，
    **绝不盖住原候选过大失败**（用户中断仍继续上抛）。
    （2026-09-28 已改：真实宿主里这一版恒记 0，现走宿主只计量入口、只在抛出时量一次，测不出为缺失，见本文件同日条目。）
  - `compact_tool_summary.py` 新增 `retained_ir_facts(ir_history)`：只读入参，返回条数与模型可见投影的 token 估算，
    与只统计工具/会话保留的 `retained_items` 分开；空与缺失都返回 0。
  - 活动回合链（`active_turn_compact.py`）：`_ActiveTurnArchiveCompactPlan` 只携带 `retained_ir_history` 供失败诊断，
    不参与覆盖判定、不写 checkpoint；`_active_turn_fixed_tokens` 用同一 projector 的空摘要、无保留那一次实测。
  - TUI（`tui_block_renderer.py`）：失败行在原有"候选 / 上限（摘要约 X）"后追加" · 固定开销约 X"，
    **只在实测值大于 0 时**出现；缺字段保持旧文案。
- **新测试**（`tests/test_compact_capacity_facts.py` 新增 5 项，并同步 normalizer 用例）：走真实
  `prepare_conversation_context` 的端到端用例确认固定开销是一次实测投影（空摘要、无保留、不计入候选），
  且只在失败路径多投一次；投影不可用时记 0 且错误码不变；`retained_ir_facts` 的三种入参；TUI 文案含固定开销。
- **同步的旧用例**：`test_active_turn_compact_projection.py`、`test_compact_request_projection.py`、
  `test_compact_source_lifetime.py`、`test_mixed_compact_contract.py` 按"失败路径多一次固定开销投影"更新
  投影次数与视图序列断言（这是本改动必然带来的调用次数变化，不是放宽断言）。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_compact_capacity_facts.py -q`；
  相关范围 `python3 -m pytest tests -q -k compact`（本机 `hypothesis`/`pyte` 缺失，需忽略 3 个收集失败文件）。
- **变异**（3 次，都命中预期用例）：①`_fixed_request_tokens` 恒返回 0 → 4 项失败；
  ②`retained_ir_facts` 恒返回 `(0, 0)` → 2 项失败；③TUI 不渲染固定开销 → 1 项失败。还原后补丁逐字一致。

## 工具说明按调用方实际路径范围给出（2026-09-27，分支 `claude/9b-read-file-scope`，基于 `f3398da7c`）

- **口径**：read_file / list_files 的说明末句按本 run 的有效 owner 墙三选一（有墙 / 无墙 full / 无墙 normal），
  与 `PathAccessPolicy.check` 一一对应；有效墙只由 `path_access_policy.effective_owner_scope_root` 算出（写边界、执行门、
  快照冻结共用）。说明在 `_tool_snapshots_for_run` 冻结快照时选定，`schema_hash` / `snapshot_hash` 不变。
- **新测试**（`tests/test_tool_path_scope_descriptions.py`，6 项）：
  - 三种末句：只换末句，full 与原说明逐字相同，有墙版本写出 PATH_OWNER_SCOPE_BLOCKED 且不提 capability_request，schema_hash 相同；
  - 文本与路径门一致：对 owner home、shared、墙外、危险目录、凭据文件取样，门返回的每个拒绝码都写在对应范围的说明里；
  - 权威函数的优先级（子代理墙 > 自身墙 > 冻结值 > 注册表墙）；
  - 执行门：Full Access 父注册表上带冻结子代理墙的 read_file 在授权阶段被拒（PATH_OWNER_SCOPE_BLOCKED），不带墙的主 run 能读；
  - 哈希不变，快照不传范围时按注册表自己的墙渲染；
  - 事故回归（假 LLM）：Full Access 父代理的 task_local 子代理，线上发出的 tools 是墙内说明；子代理 compact 缓存面与模型轮
    逐字一致、两次渲染字节相同；同一注册表的主 run（带“受限”字样的自然语言）仍是原句。
- **变异验证（12 种，11 种被抓住，逐个字节级还原）**：快照改用注册表的墙、说明算进 schema_hash、有墙/full 文本互换、
  normal 漏掉例外句、list_files 不按范围、compact 绕过冻结点、快照默认忽略注册表墙、权威函数把冻结值排到自身墙前、
  丢掉 task_local 墙、执行门忽略冻结值（原有 15 个墙相关测试文件都没抓住，靠新增的执行门测试抓住）、写边界不看子代理上下文。
  没抓住的 1 种是包入口读取判定忽略冻结值：该处只给 skill_search 用，现有参数里没有会碰墙的路径，属于原有覆盖缺口；
  本次对它只是行为等价的替换。
- **顺带发现（未改）**：写边界只带冻结墙、不授予外部工作目录时，子代理读取“工作区根目录下但在 owner home 外”的文件，
  处理器的“路径疑似拼写错误”提示先触发，返回 TOOL_INVALID_ARGUMENTS，并建议用同一路径重试，与“墙外路径反复重试”
  是同一类问题；生产中子代理是否总会继承父代理工作区授权尚未核实，建议另开切片确认。

## 配置告警不再回显凭据原值（2026-09-27，dev 审 a95746edc 时发现，my-agent 修）

- **来源**：`/settings` 总览开始显示配置告警后，dev 指出 `settings/services/_normalize.py` 里几处告警把用户写的原始值
  原样带进文字（`got {value!r}`）。用户把凭据类参数写成错误类型时，值就会经 `/settings` 显示出来，飞书上也能看到。
- **做法**：新增 `settings/services/_normalize.py: describe_raw_value(key, value)`——**在源头**判类型（键在生成告警时
  本来就知道，不在展示层解析告警文字）：键是凭据（`user_config_capability.is_credential_key`）时不回显值，写"已隐藏"；
  其他键照旧回显但截到 80 字符并标"已截断"。四处回显值的地方改调它：`_normalize_home_strings`、
  `_normalize_user_id`、`_normalize_path_access_fields`、`_normalize_runner_timeout_by_role`，以及
  `_normalize_timeout_value` 的两条分支。
- **新测试**（`tests/test_config_warning_no_credential_echo.py`，16 项）：helper 对每个凭据键都隐藏原值；
  普通键照旧显示；超长值截短且标注、短值不加标注；再加真实端到端——用 `load_config` 加载一份含错误类型的用户配置，
  走真实 `/settings` 总览，确认凭据原值不出现在回执里、普通键告警照旧出现。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_config_warning_no_credential_echo.py -q`。
- **变异**：把 helper 里的凭据判断去掉，6 项"隐藏凭据"用例失败、其余仍绿。

## `/settings` 总览显示配置告警（2026-09-27，dev 派活，my-agent 实现）

- **来源**：参数减量后，已删/未知的配置键会被忽略并记进 `AgentConfig.config_warnings` 或
  `CapabilityConfig.config_warnings`，但**两个都没有展示点**，用户看不到自己配置里哪些键没生效。
- **做法**：`gateway_parts/settings_control_service.py` 新增 `_config_warning_lines(config)`，把两个来源的告警合并，
  输出“配置告警 N 条（下面这些配置键没生效，只是被忽略了）：”加逐条 `- [来源] 告警`，来源写明是
  `agent 主配置` 还是 `capability 配置`；`_overview`（`/settings`）与 `_all`（`/settings all`）各插一行，
  **N 为 0 时不出现这一行**。只读字段，不解析消息文字；配置对象没有这些属性时照常出总览；
  memory doctor 那几处只读 `memory_config_warnings` 的出口**未动**。
- **新测试**（`tests/test_settings_config_warnings_display.py`，6 项）：两个来源的告警都出现且条数正确；
  逐条能看出来源；0 条时不出这一行（`/settings` 和 `/settings all` 各一条）；`/settings all` 也显示；
  老配置对象缺字段时不报错。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_settings_config_warnings_display.py -q`。
- **变异**：把两处 `_config_warning_lines` 调用去掉（等同不展示），3 项“显示告警”用例失败，
  3 项“0 条不显示”用例仍绿。

## capability 配置遇到已删键只告警不拒绝（2026-09-27，用户插队派活，my-agent 实现）

- **来源**：Codex 在私有环境发现——owner 的 `capability_config.yaml` 里只要还写着 13u 已删的决策点位键
  （`*_timeout_seconds`、`*_profile_id`，即使值是空的），loader 就直接拒绝加载；而 agent 主配置对未知键只告警并忽略。
  按参数减量的约定，已删的键应该"只告警、不迁移"，两边行为要一致。
- **做法**：`agent/capability/config.py` 的 `load_capability_config` 不再对未知键 `raise ValueError`，改为把
  `unknown capability config key: 'x'; ignored` 记进 `CapabilityConfig.config_warnings`（该 dataclass 新增
  `config_warnings` 字段，只供诊断展示、不参与路由判断），已知键照常加载。**真校验没有放宽**：
  `validate_config_decision_fields` 仍在过滤之前跑，仍是当前登记点位的字段值非法时照旧抛错；缺文件仍是
  `FileNotFoundError`。已删键因不在当前点位登记表里，自然落进"未知键告警"这一条，两条路径互不越界。
- **新测试**（`tests/test_capability_config_unknown_keys.py`，7 项）：已删决策键能加载且逐条告警（4 个键 → 4 条）；
  已知键照常生效且不产生告警；已知+已删混写时各走各路；完全没见过的键也不拦加载；
  仍是当前登记点位的字段值非法时照旧报错；已删键只走告警、不走点位校验；缺文件仍硬错误。
- **改掉的旧用例**（3 处，均为"拒绝未知字段"的旧约定）：`test_capability_config.py`
  `test_load_capability_config_unknown_fields_rejected`、`test_capability_config_class.py`
  `test_load_capability_config_rejects_unknown_fields`（已改名为 `..._warns_on_unknown_fields`）、
  `test_decision_settings.py` 里断言"已删点位期限会被明确拒绝"那段。现在断言告警并忽略。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_capability_config_unknown_keys.py -q`。
- **变异**：把告警列表改回空（等同"吞掉不告警"），4 项告警用例失败，3 项回落/校验用例仍绿。

## handler 层路径拒绝改报权限码（2026-09-27，用户派活，my-agent 实现）

- **来源**：dev 指出——父项目目录等路径先被路径门放行，再由工具自己的 `PathAccessPolicy` 拒绝，报的是
  `TOOL_INVALID_ARGUMENTS`（execution 阶段），而“授权阶段连续失败即停”只统计 `authorization` 阶段的失败，
  子代理因此会在这类路径上继续空转。记在 `docs/ROADMAP.md`。
- **做法**：`PathAccessError` 增加结构化字段 `access_code`（`PathAccessError.__init__`），`resolve_path` 抛错时传入
  `decision.code`；新增 `_filesystem_helpers.path_resolution_error_outcome(tool_name, exc)` —— 只读异常上的
  `access_code`，码已登记在 `ERROR_CONTRACTS` 且 `category == "permission"` 时按原码上报并带
  `failure_stage="authorization"`，其余仍报 `TOOL_INVALID_ARGUMENTS`。放 helpers 里是因为
  `filesystem_read_file` 被 `_filesystem_read` 导入，放别处会循环导入。四个读类工具
  （`filesystem_read_file` / `_filesystem_list` / `_filesystem_find` / `_filesystem_search`）包住 `resolve_path` 的
  `except ValueError` 分支改调这个 helper；读窗口、正则等其他 `ValueError` 分支未动。
- **新测试**（`tests/test_path_access_error_codes.py`，14 项）：
  - helper 单测 7 项：登记的五种 permission 码原样上报且带 authorization；普通 `ValueError`、未登记的码、
    已登记但非 permission 的码（`PATH_SYMLINK_ESCAPE_BLOCKED`）、完全没有 `access_code` 属性的异常，
    四种都回落 `TOOL_INVALID_ARGUMENTS` 且不带阶段；错误消息原样保留。
  - 真实工具端到端 7 项：用真实 owner 墙（owner A 读 owner B 的家）分别打四个工具，都报
    `PATH_CROSS_OWNER_BLOCKED` + `authorization`；四个工具的 (code, stage) 集合必须完全一致（否则即停统计不到）；
    自己家照常读到（改造没把合法读取弄坏）；真正的参数错误（path 传整数）仍报 `TOOL_INVALID_ARGUMENTS`。
- **复现**：`cd agent_py_agent && python3 -m pytest tests/test_path_access_error_codes.py -q`。
- **变异**：把 helper 里 `contract.category == "permission"` 判据改成恒假（等同改动前行为），9 项失败、5 项仍绿
  （正是那些断言"回落"的用例），证明这批测试真的钉住了新行为而不是恒真。变异后已还原并复跑通过。

## Compact 计量补片与校准合同只读复核（2026-09-28）

修复复验：固定 `b6dafe22ad5f3ef2ce09317a80fbe87a767749ad` 的隔离源码副本运行 `test_compact_capacity_host_chain.py`、`test_compact_capacity_facts.py`、`test_active_turn_compact_projection.py`、`test_compact_request_projection.py`、`test_compact_source_lifetime.py`、`test_mixed_compact_contract.py`，6 文件 **85 passed**。真实恢复宿主的 transcript、活动轮与 mixed callee 均由替身摘要/HTTP 驱动；只计量材料不可提交、普通空摘要仍拒绝、失败才测固定开销、未知省略及非交接摘要保留已覆盖。作者 22 项变异测试未独立重跑，不叠加为本线计数；这不是校准或 C21 通过。

修复前的离线复现保留：冻结当时未提交补片的生产 helper，transcript 未知固定开销已为缺失值，活动轮仍输出零；原有 fixture 实际保留 5 条 IR、估算 148 tokens，当时统计只算 4 条、115 tokens。另在固定 f785 上验证校准生命周期：本轮请求缩小时回原始估算；持久观察对同一候选校准为 105000，清除观察后回 150000。两项均是合成合同检查，零真实模型/网络/Gateway 操作，不是校准修复通过或 C17 原因证明。

建议下一步：原作者交付候选接受到提交后实际预检的一致性验证；Claude 交固定组合和 gate，root 再核对并运行 C21。只读审阅可并行，产品代码仍由原作者独占。证据摘要与范围见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#compact-计量与校准待修复边界2026-09-28)。

## C21离线交接工具开发验证准备（2026-09-28）

已准备五份业务输入、一次普通中文需求及分层判据，尚未提交原生任务。作者只核输入 JSON、摘要、显式 ID 和引用/时长，没有运行包检查程序或补业务产物。待 Claude 固定组合交付后，以既有独立 131072 profile 做一次自然开发；不改阈值、不要求模型堆上下文、不手动压缩。业务交付与连续 Compact 分别判定，未触发不能算通过；最终仍 0/27。详见[准备记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c21离线交接工具开发验证准备2026-09-28未提交)。

建议下一步：核固定版本及安装前提，再由 root 提交和观察；Claude 可并行交付计量修复，其他审阅保持只读。

历史结构化补证 v2 已复核：25 次手动控制中 23 次成功全部精确关联，主 owner 的 77 次提交分为手动 17、触发未知 60；进度为 534 条 Compact 加 8 条 IR，共 542。v1 保留，检查点共有字段未变，同请求同 attempt 的第 1–5 代提交事实保留；`forced` 不证明自动触发，记录完整性未证。导出没有当前源码/包 pins/get/执行，不能替代 C21。本轮仅解析已有元数据，未重跑提取器；详见[证据范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#历史-compact-证据复核2026-09-28)。

## C20原生128K分项（2026-09-28）

- 固定 f785 私有安装的 1430 个成员与源码/wheel 一致，pip check 通过；14932 份旧数据和配置保持，日用默认不变。
- CAP06 单次普通需求、官方 M2.7、独立 131072 窗口：250.573 秒自然终态，29 次工具、14 次 HTTP、零重试。两包 pins/选择及两份输入、六配置和安装表均保持。
- 原 A 检查器通过 source_ref 同字节物化并实际执行一次；返回 1 项引用错误和 8 条 warning。保存 JSON 语义相同但重新排版；交接把场次外引用误述成段落不存在，且误报字段归属，整体业务未通过。
- 峰值上下文估算 91718，低于 117964 触发点，generation=0。连续压缩及压缩后执行均未覆盖；旧 C17 失败、最终 0/27 保持。
- 本轮只追加原生证据及文档，没有产品/测试代码改动；固定版本的发行 gate 来自已核 Claude 14f 原日志，不能替代本轮真实分项。详见[完整范围](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c20固定f785的128k原生验证2026-09-28)。

建议下一步：交接本轮覆盖范围，Claude 继续 Compact 固定补片，root 核必要长任务证据；只读审阅可并行，不修改已结束用例或靠手动 Compact 补通过。

## C19原生三助手分项（2026-09-27）

- 固定cb40cc4a8已私有安装，1430源码/wheel/安装成员一致，pip check通过。停止态14353份非`.DS_Store`文件保持；备份时10份目录显示缓存变化有原始失败与差异记录，未覆盖原快照。无日用环境或主线部署操作。
- 新CAP02仅一次原普通需求；taskrun整体264.981秒、三子attempt重叠88.800秒、自然终态、父背景读回三记录。A/B两名显式授权孩子已get当前同代方法/模板并保存记录，包引用与父级相同。
- 第三子没有包授权，两次search被拒后没有请求能力，转读旧源码A0.1.1/B0.1.0。实际只落三记录和B示例，父声称的汇总及A两示例不存在；整体交接未过。未触发未声明成员get纠错、父resolve或Compact，不把本轮计为这些分支的真实通过。
- 原工具索引与canonical分别核对；首父response为43.631秒等待回复，不作为整个任务或最终回复。私有`candidate-19-general-delegation-observation.json`摘要`ed75515958ce38c4e561faec3469481594fe9c7b23a7985b56fe3db241c35190`保留父子身份/调用/文件/分线程用量。321旧文件、133旧绑定、六配置及安装账保持。
- 独立审计`candidate-19-general-delegation-independent-audit.json`确认A/B分别9/4次同代get，B示例source_ref复制字节一致；A检查器首资源页及preview均未续读，不算完整阅读。三子原用量账辅助logical/HTTP均0，主HTTP分别13/8/12；真实结果仍以各分项和整体交付分别判定。
- 本轮没有继续修改产品或新增测试；119项及严格gate属于已安装cb40补片的本地证据。最终0/27保持，原C18失败和当前部分成功分别记账，详见[当前验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c19三助手资料交接2026-09-27分项通过整体未过)。
- 后续责任核定未发现新的宿主缺陷：原派工遗漏授权，实际子提示和错误回执均保留申请入口；三个结构化子交付路径全部存在，父额外声称不是结构化声明。仅运行既有`test_deliverable_closeout_gate.py`，8项通过、源码指纹保持、零真实模型调用；证据`candidate-19-closeout-contract-check-01.json`摘要`016369fbc5d9c73cc0d6f4c1b9c17c85e8482523edf6ddb48d02b165ce348c4e`，不计为C19业务通过。

建议下一步：保留当前候选，按通用机制与模型交付限制分别收口；Claude可并行完成Compact，收到固定修复后再验连续压缩及最终矩阵，不追加需求救本轮产物。

## C18包成员参数纠错（2026-09-27）

- 原生C18失败保留；旧产品最小反例4/4失败，均把未声明成员报为`SKILL_SNAPSHOT_UNAVAILABLE`。真实ToolExecutor同样失败，红证据`candidate-18-resource-path-red-01`未改写；其中第三路径为当时的`../CAPABILITY.md`，最终覆盖改用`inputs/story.json`。中断吞异常是另一个静态发现，不冒充这4项的失败原因。
- 产品只改`skill_search_tool.py`：原包/代次核验之后按声明判成员，返回原参数错误及同代入口建议；保留原reader、任务pin与取消语义，明确重抛`InterruptedError`。无新增状态、配置、依赖、权限或模型循环。
- `.venv/bin/python -B -m pytest agent_py_agent/tests/test_capability_package_resource_scope.py agent_py_agent/tests/test_capability_package_selector_recovery.py agent_py_agent/tests/test_capability_package_read.py agent_py_agent/tests/test_capability_package_discovery.py agent_py_agent/tests/test_capability_package_runtime_binding.py agent_py_agent/tests/test_capability_package_native_pipeline.py -q --tb=short`：119 passed，0失败/错误/跳过，6.083秒。实际使用原checkout的Python 3.12 venv、当前工作树源码、隔离MY_AGENT_HOME和临时目录，无真实模型/Gateway。
- 新10例覆盖三个错成员路径、任意声明入口名、同代建议的显式重读、真实ToolExecutor恢复动作及原TaskStore pin、旧代拒绝、真实缺字节/摘要错误和取消。原未知/未授权例加强断言；不声称失败get不会沿原策略晋升任务。
- 测试前后所有受核Python源码指纹一致，私有证据`candidate-18-resource-path-focused-01.json/.xml/.log`保留准确argv与摘要，日志SHA256 `3967a3470f7eac359388222b353644d0c76efe416477cf10067638da7e665570`。独立只读末审无阻断；成熟参考与边界记录在`candidate-18-resource-path-readonly-review.json`。
- 本地严格gate已通过：Ruff、doc-sync、strict-size（hard=0）、diff和clean-package全部退出0，记录为`candidate-18-resource-path-strict-01.json`；生成尺寸报告已恢复原字节。随后只更新通过记录并复核doc-sync/diff，未重跑组件。线上CI未作为验收来源。无新增重要文件或配置，不改CODEBASE_TREE、YAML/dataclass；未改变子代理模块，模块四件套无需更新。当前私有运行仍def648397，真实模型纠错和委派待固定新版复验，最终0/27保持。

建议下一步：固定补片交Claude集成，root在私有新版用新会话验证原需求；Claude可并行完成Compact，双方不同时写产品入口，不补救C18旧业务产物。

## C17显式包申请组件链（2026-09-27）

- 真实隔离安装/创建/申请组件先证明旧产品5例4失败1通过：合法包申请在无候选及真实包卡命中时均提前GAP，未知包也提前GAP，混合申请只授工具却GRANTED；裸包名不会自动变成包授权。
- 修复仅在原owner路径判断之后，将含`capability:`引用的请求保留OPEN/PARENT_RESOLUTION_REQUIRED；原父级resolve负责快照解析、完整ref与canonical grant。两个模型参数说明明确包stable_id和批量item.allowed_skills，不新增配置、权限账、首请求marker或兼容别名。
- `.venv/bin/python -m pytest agent_py_agent/tests/test_capability_package_task_refs.py agent_py_agent/tests/test_agent/test_subagent_action_and_route.py agent_py_agent/tests/test_subagent_capability_request_tool.py agent_py_agent/tests/test_resolve_capability_requests_tool.py agent_py_agent/tests/test_capability_auto_grant.py agent_py_agent/tests/test_orchestration_tool_specs.py agent_py_agent/tests/test_manager_runner_capability_requests.py -q --tb=short`：最终111项通过，0失败/错误/跳过，15.315秒。使用原checkout的Python 3.12 venv、当前工作树源码与隔离home，无真实模型或Gateway。
- 新合法链已走到父级grant、七字段引用、孩子同代私有方法get和重复resolve无重复授权；混合申请不局部结清，未知包批准失败后可deny关闭。测试前后源码指纹一致。
- 私有`candidate-17-package-request-focused-02.json/.xml/.log`保留最终准确命令与指纹，日志SHA256为`e9e93bf6ce99e361311c2f70ba62d9ecf3e7ce0c72664f23da96aebf4f90392e`。旧红例及首轮绿验另存，不覆盖。独立末审无阻断，明确mixed仅验自动路由、原search仍运行、批量顶层默认值保持；澄清参数说明后重跑上述最终111项。严格gate首轮仅doc-sync要求补模块结构文档，补齐后通过；Ruff、strict-size（hard=0）、diff和clean-package通过，最终组合记录为candidate-17-package-request-strict-02.json，生成的size报告恢复原字节。线上CI未作为依据，原G05和最终0/27不改判。
- 建议下一步：固定补片交Claude集成，与其Compact修复可并行；root在新固定运行版本做一次原生委派复验，再进入最终矩阵。

## C17子代理资料路径异常隔离（2026-09-27）

- G05首子请求被长input_refs的ENAMETOOLONG中断，原始失败及包授权缺项见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c17并发资料交接与输入引用修复2026-09-27)。本地旧builder用临时文件及超长引用再次复现，没有真实模型或运行环境操作。
- 生产仅修改`prompt_context_summary._resolve_read_paths`：逐候选隔离exists/resolve的OSError，沿原unresolved计数；保留其它根/引用并重抛InterruptedError。无新授权、状态、配置或依赖；expanduser和根目录规范化仍沿原行为，不宣称覆盖所有路径异常。
- 新8例旧实现6失败/2通过，修后8通过：required/hint各覆盖exists/resolve异常、多根中的可用替代候选及中断传播。
- 完整相关命令：`.venv/bin/python -m pytest agent_py_agent/tests/test_runner_prompts.py agent_py_agent/tests/test_orchestration_create_subagents_tool_workspace.py agent_py_agent/tests/test_create_subagents_input_read_scope.py agent_py_agent/tests/test_subagent_context_bundle.py -q --tb=short`，65项通过，7.054秒，两份变更源码前后指纹相同。实际使用原checkout的Python 3.12 venv执行，源码来自本能力分支。
- 成熟参考仅核本地Hermes `agent/context_references.py`逐引用独立展开和异常警告；本仓复用原unresolved，仅捕获对应文件系统异常，不引入解析器或宽泛Exception捕获。
- 私有`candidate-17-readrefs-focused-01.json/.log`保留命令、源码及日志摘要；日志SHA256 `55c823246eb78aa7433a1726dc60eafc4b2d57bcf5526ee5a7307de2bfb3651b`。独立窄审无阻断；最终Ruff、doc-sync、strict code-size（hard=0）、diff和clean-package全部通过，记录为candidate-17-readrefs-strict-02.json。首轮Ruff的长导入排版错误已修，测试AST与已测版本相同；首轮失败记录保留。线上CI未作为依据，私有Gateway仍51dac。
- 建议下一步：固定本地补片交Claude，再补合法包申请链的确定性覆盖；原生复验绑定新固定版本，旧G05失败和最终0/27保持。

## B0.1.4字段映射资料纠正（2026-09-27）

- 本地候选只修改B方法与来源说明，并将声明版本从0.1.3升为0.1.4；两个既有构建版本断言同步更新，没有新增测试、schema、校验规则或宿主逻辑。组件阶段原安装版B0.1.3及G04业务文件保持，随后原生更新另记如下。
- 既有`test_capability_package_b_template.py`、`test_capability_package_drama_workflow_duration.py`及`test_samples_build_reproducibly_and_expose_only_one_package[drama-workflow-b]`共56项通过，0 failed/errors/skipped，8.260秒；源码指纹前后相同。输入为公开合成资料，未运行真实模型或业务检查器。
- 两次原CLI构建逐字节一致，ZIP为35776字节，SHA256 `7b9b77c67f6b67e6133154d7d03f84b9cb9505a39b5e5e9c77b761a061e5edd5`。11份资源均与源码及声明摘要相符，全部为私有非执行资源；相对811bdc7d3仅`methods/workflow.md`和`PROVENANCE.md`变化，脚本、模板、示例和许可保持。
- 独立静态窄审未发现阻断：保留一对多/多对一、每镜所属场次、阶段地址和结构/语义边界。报告`candidate-17-b014-content-review.json`摘要`07fe35cec4427423e38daf1cfe561d29146673dc5027eb2a785cbed0cebb1138`；本线另把README的源码候选与已安装版本表述分开，不改变包内容。
- 组件证据为私有`candidate-17-b014-focused-01.json/.log/.xml`、`candidate-17-b014-build-verification.json`及上述窄审记录；组件阶段未安装新包，不补算G04或最终27次。
- Ruff、doc-sync、strict code-size、diff-check、clean-package全部退出0，尺寸hard=0、blocked=False，既有非阻断发现保留。**本地严格gate已通过，线上CI未作为验收来源**；命令、退出码和日志摘要见`candidate-17-b014-strict-gates-01.json`。随后仅补本文和交接记录，重新核doc-sync与diff，不重复模型或56项组件。

### B0.1.4原生管理更新

- 固定补片`a8dc2fc32aee6770c6e7852ce7333675ff6aae61`，私有运行仍51dac。新原生CAP01只提交管理命令，按disable→update→enable完成三个写请求；B安装revision 13→15→16→17，最终0.1.4、active，形成新activation。安装ZIP与已测构建逐字节一致，11资源摘要全部相符，设置及settings_revision保持。
- 281个旧用例文件、127份既有Task的pins/selection、6份配置及A/C两份完整安装记录均保持；Gateway进程及启动身份保持，未重启。未新增Task文件，未提交业务prompt，新版本模型采用继续未验，最终仍0/27。
- 更新命令的首个Enter只接受原生路径补全；观察到原命令仍在输入框、安装revision仍15后，核对TUI既有按键语义，仅按一次Enter提交同一草稿。没有重输命令或重放未知结果，三次写请求的真实安装提交和终端记录分别保留。
- 原生冻结记录`candidate-17-b014-native-before.json`及`candidate-17-b014-native-observation.json`；后者SHA256为`65b55d9aaf4c018bbcc1498acbeeda3d7875df1372a5ea26a9dbc28da4c88eb3`。新包安装管理通过不改变G04内容失败或65k连续Compact失败。

建议下一步：将固定补片交Claude合并；其Compact补片独立推进，root按固定组合准备原生复验。文案纠错不证明模型已正确采用，不扩展领域规则或重跑同一故事。

## C17能力包组合证据核对（2026-09-27）

- 固定源码`51dac1b815acf74a76fa79ef7f93f2f4228b3d4b`，本线工作树与Claude组合工作树一致；本次只核已有日志，不重复运行全仓或发起模型请求。
- 全仓递归收集1192个测试文件、12分片：23639 passed、0 failed、0 errors；另记21 skipped、32 xfailed、5 xpassed。核对各`out-*.txt`中的真实pytest汇总，未把某分片末尾的SDK退出噪音当pytest失败；`failed.txt`为空、`shards_rc=0`。
- Ruff、doc-sync、strict code-size、diff-check、clean-package各退出0；尺寸报告hard=0且blocked=False，软告警仍在。线上CI未作为验收来源。
- Claude已双机部署`runtime-step13w-f95e5d04`。本线核对部署日志的SWITCH_DONE及本机发行清单，源码SHA相同，wheel SHA256为`f95e5d04ca1cba46876b5e4bfd89623d9703178b6d795720fd90c7517503cb8b`；本轮未另做远端在线健康探测。
- 随后私有环境已停稳、备份并安装固定51dac；新wheel摘要`e5a0069864fd36e5ba30ecf2fd465819cf60205b24e3cb610d10971549f8532d`，1430个包成员与固定源码及实际安装逐字节一致，pip check通过。仅移除4个未设值的旧配置键，其余13291个停止态文件保持；保留旧wheel及停止态备份。
- 原始日志摘要、摘要哈希、分片计数及核对边界见私有`candidate-17-combined-evidence-verification.json`；保留先前62c全仓失败、各候选原生失败及0/36历史记录。

### C17固定版原生开发对照

- G02沿原输入和同一官方M2.7/65536 profile，只提交一次原普通需求。第1代自动Compact 77724→47484提交后，同代6次包资源get成功；第2代报`COMPACT_CANDIDATE_TOO_LARGE`，candidate_tokens=51342、input_ceiling_tokens=49152、summary_tokens=1091、retained_items=0、candidates_tried=1。request失败且线程仍generation1，无原checker物化/执行及业务输出，整体失败。130.619秒、9 HTTP/0重试、provider总236092 tokens；5 main及4 auxiliary含Compact，不能把辅助总量归给选包。
- `retained_items=0`只说明plan中的工具归档保留记录为空，不代表完整候选只剩不可变字段；摘要单独估算与完整请求估算的差也不是已实测fixed_tokens。本次未持久第二代完整失败投影，不能用第1代checkpoint或最近preflight分项替代，重复投影与具体固定开销成因尚未确认。原生五字段及计量边界已交Claude；产品计量由其唯一实施。
- G03/N05在原默认262144窗口终态done，空选、0 pins、无宿主私有入口、无包get/物化/执行，仅一次原输入read_file。三项行动和期限正确，但遗漏两位负责人，按冻结分工判据业务质量失败；不能用空选通过覆盖此结果。14.302秒、3 HTTP/0重试，选包辅助1132 tokens，总54419；没有off/无包同条件对照或完整最终请求投影，目录独占token及包机制净增开销未测。
- G02/G03原输入、六份配置及安装表摘要保持；均属开发对照，不计最终保留集。原生证据分别冻结为私有`candidate-17-general-compact-observation.json`和`candidate-17-general-negative-review.json`。
- G04在新原生CAP04、默认262144窗口只提交一次同类资源交接需求，275.085秒后request/task/run/attempt均done。19次包资源get、原生40对工具调用/结果；模型以source_ref复制原检查器，23750字节与固定包逐字节一致，并自行执行。两个输入、两包pins/selection、六份配置及安装表摘要保持。检查器rc1准确表示输入引用错误，原结果1 error/8 warnings得到忠实表达；两次命令失败仍保留在operation_verification=partial，不改判全操作成功。
- G04保存的检查JSON与原stdout解析后相等，但1320字节与1429字节不同，不称原始输出逐字节保存。独立审阅确认交接存在A/B字段混淆、空数组规则绝对化及限制遗漏；B原方法也有镜头ID与场次外键的映射歧义，未认定全部是本轮模型或宿主新造。整体交接未全通过，业务产物未由观察者补写。
- G04未触发Compact，generation0；最后上下文估算114457/262144，不能覆盖65k失败。21 HTTP/0重试（20 main+1 auxiliary），辅助1177 tokens，总1571471，包含1399718 cached input，不二次相加或推算净增成本。执行和内容证据分别为`candidate-17-general-default-long-observation.json`及`candidate-17-general-default-long-content-review.json`，摘要见[验收矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c17固定发布与原生开发验收2026-09-27)。
- 最终九例各三次的27次允许范围验收仍未启动，原0/36历史保持；其余九次按当前范围不启动。

建议下一步：Claude先完成失败候选分项计量及通用Compact修复，root在固定版本上验连续压缩后执行，再推进27次最终矩阵。只读审阅可并行；默认窗口原资源执行已留证，不反复重跑挑选成功，不扩大领域功能范围，双方产品写入保持互斥。

## 进程停止：host 在停止期间自行退出不再误报 unknown（2026-09-27，Codex 全仓复现，集成者修）

- **来源**：能力包全仓运行里 `test_process_sessions.py::test_background_session_outlives_one_shot_launcher_and_is_rehydrated`
  失败过一次，单独复跑通过。当时记录是 host 写的 `killed`，child 回执也已确认，外层清理却留下 `identity_changed / confirmed=false`，
  停止因此返回 unknown。原因是 host 在调用方“预检之后、采快照之前”按停止意图自行退出，出生身份读不到。
- **修正**：`process_session_cleanup._host_exit_settled_receipts` 只在两条事实同时成立时，把这张回执改写成已确认的
  `host_exited_during_stop`：①记录是 host 写的终态，且顶层回执已确认；②两级实例都按出生身份证明已消失。
  身份核对不放宽，其它未确认回执照旧。
- **新测试**（`test_process_session_retry_settles_unknown.py`，5 项）：
  - 竞态收敛为确认，且改写后的回执能通过存储校验并落盘；
  - host 回执未确认、child 仍在跑、非终态记录、信号后仍有残留，这四种都不确认、不改写。
  - 用替身回执固定时序，Store 事务和实例判定走真实代码。
- **变异**：7 个全部被抓住（去掉实例消失、终态、host 确认任一条件，放宽到任意未确认回执，丢观测数，不改写，返回原回执）。

## Compact 候选过大留下容量计量（2026-09-27，Codex G2 复验请求）

- **来源**：G2 复验里第二代自动 Compact 报 `COMPACT_CANDIDATE_TOO_LARGE`。failed 进度只有 after_tokens=0（未计量默认值），
  候选总量、输入上限、摘要占比都没留下，无法判断是摘要过长还是固定开销过大。
- **做法**：会话 transcript（`compact._RejectedCandidates`，取最小被拒候选）和活动回合（`_project_active_turn_request`）
  两条压缩链的错误带 `CompactCapacityFacts`。failed 进度经 `compact_failure_progress_fields` 取字段，再经
  `COMPACT_CAPACITY_PROGRESS_FIELDS` 白名单外发。TUI 失败行显示“候选 X / 上限 Y tokens（摘要约 Z）”。
- **新测试**：
  - `test_compact_capacity_facts.py`（9 项）：
    - 字段与白名单同步；
    - 只有带计量的错误才出计量；
    - normalizer 缺失不补零、负数夹 0；
    - 两个候选按两种顺序都取最小的一个，含保留条数与摘要估算；
    - 摘要失败、接受候选都不带计量；
    - TUI 有计量才显示、无计量保持原文案。
  - `test_active_turn_compact_projection.py` 的上限用例断言活动回合的计量与 failed 事件字段。
- **复现**：`python -m pytest agent_py_agent/tests/test_compact_capacity_facts.py agent_py_agent/tests/test_active_turn_compact_projection.py -q`。

## 审计保留期 0 不再删光全部审计（2026-09-27，my-agent 提交 `c2d88a0b0`，集成者补注释）

- **来源**：my-agent 补参数说明时核对 `cli_audit_cleanup_days` 的 0 语义：`agent/audit/query.py` 的截止时间是 `now - days*86400`，
  0 天等于“现在”，执行清理会删掉全部审计记录，与项目“数量上限 0 = 不限制”的约定相反。复现：旧算法 0 天删 2/2 条，1 天删 1 条，默认不删。
- **修正**：天数 ≤ 0 表示永久保留，清理直接返回 0；命令行回执写明“保留期设为 0（永久保留），未清理任何审计记录”。
- **测试**：`test_audit.py` 新增 0 与负数都不删、默认保留期不删；原有 1 天删一天前的用例不变。

## 到达计数接上子代理选模型（2026-09-27，分支 `claude/9b-three-point-reach` 第 3 个提交）

- **口径**（集成方已确认）：每次非 dry_run 的 create_subagents 算一次到达（整批孩子一个决策）；create_subagents 自身校验
  没过时直接返回，不进入点位。原来吞掉一切的 `except Exception: return None` 拆开：`_prepare` 交回结构化原因
  （`configuration_unavailable`、`point_off`、`models_given`、阶段原因码、`no_candidates`、`nothing_to_ask`，材料经
  `counted_material` 记 `bad_material`），发网前复核不通过记 `candidate_scope_changed`，真正调用前记 `called`；意外异常仍返回
  None 不计入，用户停止原样上抛不计入（`InterruptedError` 是 `OSError` 子类，读设置处先单独上抛，不能记成设置读不出）。
- **新测试**（`test_decision_subagent.py`）：`test_each_create_call_counts_one_reach_outcome` 一次跑遍 8 种原因和一次调用，
  dry_run、校验没过、意外异常都不计；`test_settings_reads_propagate_user_cancel` 参数化两次读设置（准备时、发网前复核）被用户
  停止都原样上抛、不建任务、不计数。审计与菜单测试断言 12 个点位都已统计。
- **变异验证（16 种全部被抓住，逐个字节级还原，并核对每个失败都落在对应原因上）**：8 个原因各自不计、`called` 缺失、意外异常
  误计、dry_run 误计、吞掉用户停止、准备时停止误计、复核时停止误计、校验没过误计、覆盖列表漏掉点位（展示口径）。
- **顺带修掉的测试垃圾文件**：接上这个点位后，用 MagicMock 假宿主的编排测试会走到计数，`note_decision_reach` 把 mock 属性当路径，
  在工作目录写出 `<MagicMock …>` 文件（clean-package 检查拦下 188 个）。现在只认 HomePaths 字段的真实 `Path`；
  `test_switch_off_or_missing_path_records_nothing` 在临时目录里用 MagicMock 宿主断言不写文件（改回旧判断时该测试失败）。

## 到达计数接上技能工具推荐（2026-09-27，分支 `claude/9b-three-point-reach` 第 2 个提交）

- **口径**（集成方已确认）：每次新评估算一次到达；本片已评估过、携带选择的恢复都不算。原因码：`tools_disabled`、
  `isolated_scope`、`no_run_context`、阶段原因码/`point_off`、`nothing_to_recommend`、`bad_material`。普通模式关闭但实验放行时，
  这一次到达只记实验的结果：实验阶段拒绝记阶段码或 `experiment_forbidden`，否则同样是没有题或 `called`。
- **结构**：`recommend_capabilities` 原本 60 行（正好到软上限）。入口原因抽成 `_context_miss_reason`，返回点统一用
  `_skip(original, agent, carried, reason)`（只在新评估时计数），已采用纯值的构造抽成 `_selection`，函数缩到 56 行，嵌套不加深。
- **新测试**（`test_decision_capability_consumer.py`）：按顺序跑调用、携带恢复、携带时点位被关、已评估、隔离、没有编号、工具关闭、
  点位关闭、没有可推荐、材料超限，核对每种新评估各记一次且恢复/已评估不记；实验路径被实验阶段拒绝时只记 `experiment_forbidden`，
  不另记 `point_off`。
- **变异验证（10 种全部被抓住，逐个字节级还原）**：恢复误计、已评估误计、上下文原因不计、阶段原因不计、没有可推荐不计、
  材料不合格不计、普通路径漏记 `called`、实验拒绝不计、实验路径多记 `point_off`、`_selection` 里两个名单写反。

## 到达计数接上选模型（2026-09-27，分支 `claude/9b-three-point-reach` 第 1 个提交，基于 main `eb7c639a1`）

- **口径**（集成方已确认）：每个 Gateway ask 通过 `_eligible` 算一次到达，之前的机制性退出不算。模式关闭记 `point_off`，
  但只进内存（`note_decision_reach(..., flush=False)`）：这个钩子每个请求都跑，原有测试要求关闭时零读写。
  其余结果为阶段原因码、`no_candidates`、`bad_material`、`turn_closed`；真正调用前记 `called`。
- **新测试**：
  - `test_gateway_model_observation.py`：正常调用、没有候选、阶段出错、材料不合格各算一次且只记一个结果；关闭时 `point_off`
    只进内存待写队列、仍然零 I/O；恢复/已有标记/控制命令等机制性退出不算；提交时轮次已关记 `turn_closed`。
  - `test_decision_reach_counts.py`：`flush=False` 的计数不落盘，下一次到期计数时一起写出；诊断行带 `note`。
  - 审计与菜单：model_selection 改为已统计并带适用范围说明，“未统计”的例子换成 skill_tool。
- **变异验证（11 种全部被抓住，逐个字节级还原）**：关闭不计、关闭时落盘（被原零 I/O 测试拦下）、阶段原因不计、没有候选不计、
  材料不合格不计、轮次已关不计、`called` 提到提交之前、机制性退出误计、诊断丢 `note`、菜单不显示 `note`、忽略 `flush` 参数。
## 宿主提示 host notices（2026-09-27，分支 `claude/be-host-notices`，基于 `eb7c639a1`）

- **来源**：集成者派活（方案一，已读时机 B）。智能程度检测在后台跑完后，没有地方告诉用户结论。设计见 `docs/design/HOST_NOTICES.md`。
- **新增** `test_host_notices.py` 7 项，全部用 echo 后端或假传输，零网络，不读真实配置：
  - 存取：去掉控制字符、合并空白、正文最长 500 字；同一来源替换；最多 5 条，超出丢最旧的；只取走给定编号；按来源清除；
    缺编号、来源或正文的条目丢弃；线程不存在时返回 False，不抛异常。
  - 线程记录：往返一致，旧记录读出为空。完整上下文包和后台精简上下文包都不含待送达提示。
  - Gateway 端到端：先排好一条提示，下一轮在任何模型输出之前发出一条 `host_notice` 流事件。回复正文不拼提示。
    最终消息元数据、`channel_delivery` 和 `/result` 都带这条提示，提交后待送达清空。模型收到的输入、模型可见历史投影里都没有提示原文。
    历史回放中提示紧跟在用户消息后面。第三轮不再重复。
  - 回合失败（后端抛错）时不取走，下一次回复还会附上。
  - `/progress` 不转发 `host_notice`。飞书在同一条回复正文前加“【提示】”并空一行；适配层轮询结果（假 `urlopen`）返回渲染后的正文。
  - TUI：发起窗口画成灰色 `◇` 系统行，重放同一事件不多出一行，两条提示显示两行。同会话其他窗口实时同步一行，按快照回放不重复。
  - 智能程度检测：结论排进发起检测的会话；`/effort` 查看后清掉；重新开始检测时清掉旧结论。
- **结果**：新测试 7 passed；Gateway、会话存储、历史回放、TUI、IM 适配层、智能程度检测等相关的 115 个旧测试文件共 2788 passed、2 xfailed。
  ruff、doc sync、strict code-size（与 `eb7c639a1` 逐条比较）、`git diff --check`、clean-package 见提交前检查。
- **变异验证**：37 个全部被抓出：存取与渲染 10 个；线程字段与模型隐藏 5 个；Gateway 发布、取走、元数据、交付字段与 `/result` 白名单 8 个；
  飞书渲染与流事件名 2 个；TUI 与同会话窗口 5 个；历史回放 3 个；智能程度检测排队、清除与会话目标 4 个。每个都在
  `PYTHONDONTWRITEBYTECODE=1` 子进程里跑，并逐字节恢复。
## 决策点位的期限与模型引用只留覆盖层（2026-09-27，分支 `claude/decision-point-fields`）

- **来源**：参数减量分类里的“决策点位 20 项”：12 个点位在 agent/memory/capability 三份配置里各有 `timeout_seconds`/`profile_id`
  （共 24 个字段，默认全是空＝继承），与用户长期设置、会话设置里的按点位覆盖是同一概念的两个家。
- **做法**：`decision_settings_defaults.decision_config_fields` 不再为点位的这两个字段映射配置字段（`_OVERRIDE_ONLY_POINT_FIELDS`），
  删除 AgentConfig/MemorySettings/CapabilityConfig 字段与两份随包 YAML 条目；投影在没有覆盖时按通用值继承（原有逻辑）。
- **测试**：`test_decision_settings` 改为断言点位期限继承通用值且来源为 `inherit:...`、主配置里残留旧键只告警、能力配置按原合同拒绝
  未知键、覆盖层仍能按点位设期限；三个点位的“配置默认关闭”用例改为断言期限与模型字段不存在；curator_relation 用例同步。
- **在用配置**：两台机器的配置里这 24 个键均为 0 处；两台机器都没有用户自己的能力配置文件。

## 子代理工具失败对父级可见、授权阶段反复失败即停、创建前可见性预检（2026-09-27，分支 `claude/subagent-observability`，基于 main `eb7c639a1`）

- **来源**：集成者派活。TUI 里 Full Access 的 my-agent 派 4 个只读子代理读 owner home 外的工作树，list_files/read_file/search_text
  全在授权阶段 `PATH_OWNER_SCOPE_BLOCKED`（handler 未执行），各卡约 20 分钟，父代理只看到“最近成功调用工具: search_text”。
- **新增测试（假模型、假工具、真实 owner 权威库，零网络）**：
  - `test_subagent_tool_failure_streak.py` 10 项：连续段按 (错误码, 阶段) 计、跨工具累计、成功或不同原因打断、重复门拦截透明、
    窗口打满标下限、文案只由结构化字段拼；经产品入口 `persist_tool_runtime_ledger` 写真实 RuntimeRepository 后，父代理 `list_agents`
    节点与大树预览都带 `recent_tool_failure`（4 次、`ongoing=true`、工具列表）；`last_progress_summary` 在失败后写
    “最近一次工具调用失败：read_file（PATH_OWNER_SCOPE_BLOCKED，连续 3 次）”且不刷新 `last_progress_at`，再成功后恢复；
    没有权威库时不输出摘要、只写不带次数的失败说明。
  - `test_subagent_authorization_failure_halt.py` 13 项：阈值内外、阈值 0 关闭、主代理与执行阶段失败不收口、成功/他码打断、
    同批后到成功撤销、收口后不再追加相反返工提示；收口回复即使被供应商截断（`stop_reason=max_tokens`）也判 blocked，显式硬门仍走原 unfinished；finalize 只在 `REPEATED_TOOL_AUTHORIZATION_FAILURE` 时从 archive 复算收口事实；
    合同投影拒绝旧版本/非正整数次数、不复制参数值；完成信封只在 BLOCKED 时带收口事实；前台事件、后台完成清单、递归父级行都保留它。
    端到端：真实 SimpleAgent 子代理 runner + 每轮换墙外路径的假模型，3 次被拦 + 1 次收口共 4 次模型调用后落 `BLOCKED`，账本 `halt`
    与根父级 wake 元数据一致、摘要含原因码与参数名、不含路径值，收口请求里没有“系统不会因此结束当前任务”。
  - `test_create_subagents_input_read_scope.py` 7 项：真实 SimpleAgent + 真实 `CreateSubagentsTool`（`defer_start`）：
    Full Access 父代理把墙外工作树作 `input_refs` 整批 `not_started` 拒绝、回执带大白话提示与可见根；工作区、shared、相对路径正常创建；
    批量任一不可见整批不建（含 `input_files` 他人 home 与 `context_manifest.required_read_paths`）；只在 goal 正文里出现的路径单个与批量都不预检；
    递归（孙代理）创建同样拦截；预检“看不到”与子代理真实 `read_file` 失败集合一致；宿主已声明的会话工作目录被继承为墙外已授权根，
    预检放行且子代理真实可读，只有墙外工作树被拦。
- **查明（写进文档，未改行为）**：父项目目录是子代理的网关根，路径门放行后由 handler 按 owner 墙拒绝，运行时报码是路径笔误提示的
  `TOOL_INVALID_ARGUMENTS`（执行阶段），不会触发授权阶段即停；预检回执给出的是底层裁决码 `PATH_OWNER_SCOPE_BLOCKED`。
- **变异验证（36 个全部被抓住，逐个在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑并逐字节还原、哈希核对）**：
  - A 父级可见 8 个：连续段不按错误码断开、成功后仍标进行中、重复门拦截不透明、失败不改摘要、摘要永不带次数、节点丢字段、
    模型视图白名单漏字段、事件丢 `failure_stage`。
  - B 即停通知 17 个：主代理也收口、任意阶段收口、阈值差一、收口仍是 unfinished（两种写法）、截断的收口回复改判 max-tokens、
    硬门分支走错、成功不撤销、两个判定调换顺序、连续段漏当前调用、finalize 丢收口事实、信封不带、信封不看状态、
    前台事件/后台清单/递归父级行各自丢字段、wake 摘要不写。
  - C 创建预检 11 个：永不拒绝、忽略合并前记下的显式输入、把 goal 路径记进显式输入、忽略墙外已授权根、相对路径按进程 cwd 解析、
    批量/单个/递归入口各自不预检、漏读 manifest、原因码被抹平、有 owner 墙也跳过预检。
- **code-size**：与 `eb7c639a1` 逐条比较 identity，新增 0。

## CI 工作流点名的测试文件必须存在（2026-09-27，集成分支 `claude/integrate-13v`）

- **来源**：dsh-9b 的 CI 监视报告 main `8f73a512c` 的 Cross-platform guard 在 macOS 与 Windows 都失败：参数减量第 1 批删掉了
  `test_watchdog.py`，但 `.github/workflows/cross-platform-guard.yml` 的测试清单还列着它，pytest 因文件不存在直接用法错误退出，
  两个作业一个用例都没跑。本地全量分片按文件扫描，发现不了。
- **修正**：从工作流清单删掉这一行；新增 `test_ci_workflow_paths.py`，工作流 YAML 里出现的每个 `agent_py_agent/tests/...py` 都必须存在
  （不在源码检出里运行时跳过）。
- **验证**：修正后通过；把那一行放回去，测试失败并点名 `cross-platform-guard.yml: agent_py_agent/tests/test_watchdog.py`。

## 智能程度自动检测（2026-09-27，分支 `claude/be-effort-probe`，基于 `946a26783`）

- **来源**：集成者派活。my-agent 为确认 opencode.ai 是否支持 `reasoning_effort`，3 次把用户 API key 写进 `web_fetch` 请求头；
  产品缺“用宿主保存的凭据检测模型能力”的入口。设计见 `docs/design/REASONING_EFFORT.md` 第 8 节。
- **新增** `test_reasoning_probe.py` 27 项，全部假传输（真实 OpenAI 兼容后端只替换 `request_json`），零网络：
  - 判定：用 2026-09-26 实测回放标定（DeepSeek 官方判支持；OpenCode 中转、MiniMax M2.7、完整主代理上下文的 DeepSeek 判不支持）；
    阈值边界（正好 1.5 倍且多 200、1.44 倍、只多 199、不重叠、相等即重叠、低档为 0）；拒绝字段、全部失败、没有用量、全为 0、混合计量。
  - token 提取：两种推理 token 位置、布尔与负数不算、输出 token。
  - 自动检测：`/effort high` 触发一次，出站按 low / max / 不带字段交替各 3 次、只带固定题目、后端控制方式临时为 effort；
    写入档案并记账（actor `reasoning_probe`、带 target）；`/effort` 显示中位数与撤销编号；`/effort revert` 恢复且回执反映撤销后状态；
    已有记录不再检测。开关关、auto 档、显式 none、已知服务商、Responses 协议都不自动检测。
  - 手动检测：显式 none 也能检测；不支持（输出 token 计量并注明）、有请求失败、拒绝字段三种结论都不改档案；Responses 协议说明无法检测；
    已知服务商判支持但不重复写。
  - 进行中只跑一个、显示 0/9 进度，跑完可再检测且重新计时；指纹变了、running 超时的记录失效；写不了记录就不开始、之后可重试；
    共享模型只记录；两个档案的记录共存。
  - 部署默认模型：管理员触发时写全局配置（重启生效、`/settings revert` 撤销），普通用户只提示请管理员写入。
  - 解析与 TUI：`/effort probe`、`/effort revert <编号>`（至少 6 位字母数字、只一个编号）往返。
  - 档案字段修改：白名单、档案不存在、非法值（都不记账）、原值为 none 时回滚恢复 none、5 位编号拒绝、同一配置目录下的另一用户查不到、
    只有脱敏值的记录不能回滚。
  - 开关：dataclass 与随包 YAML 默认开，非法值告警回默认。所有回执、检测记录与账本里都没有假密钥。
- **结果**：推理强度、命令解析与 TUI 转发、Gateway 控制、参数中心与脱敏、配置、模型档案与会话选模、架构守卫等相关测试共 1058 passed；
  ruff、doc sync、strict code-size（与 `946a26783` 逐条相同）、`git diff --check`、clean-package 见提交前检查。
- **变异验证**：58 个全部被抓出：判定阈值与各条规则 17 个；自动触发条件、防重复、过期、指纹、出站字段与顺序、写入与管理员门槛、共享、
  状态码、进度展示、撤销路由、记录合并、重新计时与失败释放 24 个；档案字段白名单、校验、原值、脱敏、编号长度、按用户账本 7 个；
  解析、命令目录、TUI 转发 5 个；Gateway 先执行后渲染 2 个；开关规范化与默认值 3 个。每个都在 `PYTHONDONTWRITEBYTECODE=1`
  子进程里跑并逐字节恢复。
## owner 路径按用户作用域、停机补写到达计数、数量界限统一（2026-09-27，分支 `claude/9b-owner-path-scope`，基于 main `946a26783`）

- **来源**：9b 审查结论。`home_paths_with_owner` 没有按用户重设 `owner_memory_policy_json`，Gateway 里其它用户读到的是本机主用户的
  `memory_policy.json`；到达计数的尾巴在部署重启时会丢；todo_count 等大白话写死了 2/12/24/30 这些阈值，常量一改就过时。
- **新测试**：
  - `test_gateway_per_user_scoping.py::test_every_owner_path_of_every_scope_stays_in_that_owners_home`：按 `MyAgentHomePaths` 字段全集，
    本机主用户与两个飞书用户各自的全部 owner_* 路径都必须在自己的 home 内，以后新加的字段也逃不掉（替换原先只查决策路径的那条）。
  - `test_gateway_decision_shutdown_cancel.py`：停止收尾在停 HTTP 之后、写心跳之前把未落盘的到达计数写出（3 次到达，盘上从 1 变 3）；
    落盘模块出错只记 `gateway_decision_reach_flush_failed{error_type}`，收尾照常完成。
  - `test_decision_reach_counts.py::test_threshold_labels_are_built_from_the_shared_limits`：九个界限改成互不相同的数字，每个数量类标签
    的下限必须出现在“不到”后、上限必须出现在“超过”后，且这些标签不在静态表里。
  - 六个点位各一项“改一个界限，判定与标签同时变”：delivery_quality、action_candidate、external_material_order、planning、
    skill_proposal_review、recall。
  - `conftest.py` 新增自动夹具：每个测试用自己的到达计数待写队列，测完丢弃，Gateway 收尾测试里的真实落盘不会写别的测试的临时 home。
- **变异验证（14 种全部被抓住，逐个字节级还原）**：作用域不重设 memory_policy、收尾不落盘、落盘提到停 HTTP 之前、落盘异常外泄、
  去掉取消在途决策、标签写死上限、标签上下限写反、数量类标签放回静态表、六个点位各自写回本地数字。
- **code-size**：`_cmd_gateway_run_cleanup` 抽出 `_cancel_active_decisions` 后从 54 行降到 50 行；和 main 比身份新增 0。
## TUI 权限/模型菜单测试改为按状态等待（2026-09-27，分支 `claude/9b-tui-permission-wait`，基于 main `946a26783`）

- **来源**：线上 CI `723c424d6` 的 test(3.12) 里，`test_tui_permissions_menu.py::test_f4_menu_keeps_focus_while_tool_approval_is_pending`
  读到 `ask`（期望 `auto`）。这个提交只改了注释，前一个提交三个 Python 版本全过。
- **根因**：两类时序问题叠加。
  - 权限与模型菜单的读写都在 `asyncio.to_thread` 里。测试按键后固定 sleep 0.2 秒就读盘，慢机器上保存还没完成；
    打开菜单也要先等读线程返回，固定 sleep 后发键时对话框可能还没出来。
  - prompt_toolkit 每次重绘之后才登记新浮层的父子关系，浮层上的回车保存、Esc 取消在那之前找不到，只有 RadioList
    自己的上下键能用。对话框刚出现就发“↓回车”，回车会丢；旧写法是靠那 0.2 秒里碰巧重绘过一次才通过的。
- **改动**：
  - `test_tui_permissions_menu.py`、`test_tui_model_menu.py` 去掉全部固定 sleep。
  - 发键前用 `wait_dialog_ready` 等对话框文字出现、并且焦点窗口已登记父级。
  - 保存后等菜单关闭（保存完成后才清菜单标志），或等回到主菜单的结果提示，再断言落盘。
  - 断言“保持不变”时，先等二次确认出现或菜单关闭，再断言没变。
  - `test_tui_plugin_directory_pipe.py` 里 `/plugins help` 之后固定等 0.12 秒的那处，改成等目录刷新且草稿恢复。
  - 等待辅助 `wait_app`、`wait_dialog_ready` 和 `wait_ui` 放在一起（`test_tui_decision_menu.py`），`wait_ui` 超时会写明在等什么。
  - 两个菜单测试都加了 `slow_io` 参数：读写线程各慢 0.5 秒，长期守住“慢机器也能过”。
- **复现对比**：用只在 `-p` 显式加载时生效的临时插件注入延迟。
  - 旧写法（main 版本临时拷贝）在权限读写与模型配置读写各慢 0.5 秒时，4 项全部失败：权限菜单浮层数 0≠1 两项、
    `'ask' == 'auto'`（正是线上那条）、模型配置条数 0≠1。新写法在同样延迟下全过，不加延迟也全过。
  - `/plugins` 命令处理慢 0.3 秒时，旧写法第 95 行快照断言失败，新写法通过。
- **稳定性**：最终代码上三个文件（18 项）不加延迟、加延迟（两个插件）各连跑 10 遍，全过；3.12 跑全部 40 个 TUI 相关文件，
  888 项全过；3.10、3.11 跑改动的 4 个文件，各 46 项全过。
- **同类检查**：相邻 TUI 测试里其余 `sleep`，要么在有截止时间的轮询循环里，要么只读按键同步更新的内存状态
  （输入框、光标、任务队列、主界面导航列表），不涉及线程读写或浮层对话框，未改。
## 摘要阶段内存测试排除解释器驻留表扩容（2026-09-27，集成分支 `claude/integrate-13s`）

- **现象**：参数减量第 1、2 批集成后，全仓 12 分片里 `test_host_summary_phase_lifetime.py::...[background]` 稳定失败：摘要入口计到 4.19MB，
  正常约 1.15MB。单独跑、只跑它前面的文件、只跑它后面的文件都通过；main 上同一份 94 个文件也通过。
- **定位**：在测量点取 tracemalloc 快照（25 帧），第一名是一整块 3.84MB，调用链为技能扫描 `rglob` → `pathlib` 调 `sys.intern`，
  也就是解释器的字符串驻留表在测量窗口内扩容。何时扩容取决于同一进程此前导入与运行过的测试；本批删减测试后分片组成变化，
  扩容恰好落进窗口。这与被测链路有没有驻留旧历史无关；关闭 GC 时两边也都通过，说明也不是引用环。
- **修正**：`_run_owned_bytes` 在测量点遍历快照，只扣除“由调用 `sys.intern` 的那一行分配”的记录，其余一律照算。
- **验证**：原失败分片 1543 passed；在 `compact_request_recovery` 里人为留一份旧历史引用（released 仍为 True），三种宿主全部失败，
  测到 8.8–9.7MB，说明内存断言仍然有效。

## 脱敏边界补齐（2026-09-27，分支 `claude/be-masking-edges`，基于 `946a26783`）

- **来源**：集成者对上一批四个已知边界的决定——都往“宁可多遮”的方向补。规则见 `docs/design/PARAMETER_CENTER.md` 的同名条目。
- **测试**：`test_structured_masking.py` 增至 6 项，`test_parameter_registry.py` 加 1 项并扩充凭据名正反例，全部用假值：
  - 列表：任意开关（含 `-H`、`-e`）后的“名字: 值”“名字=值”只留名字，开关后的网址不被当成请求头行；`--pass` 的值、`DB_PASS=…`、
    嵌套的 `SSH_PRIVATE_KEY`/`BASIC_AUTH`、`auth` 映射（保留键名）、列表项里的 libpq 连接串、`--header=名字: 值`、
    `--password=带空格的值`、`--headers` 后的 JSON 都遮住，并经 user_config、`/settings`、回执与记账、历史、`config-get` 各出口确认无明文。
  - 自由文本：libpq 连接串、引号括起的值、引号里的连接串、修改记录的外层引号、文字中间的网址、点分隔的名字；没有凭据名的原样。
  - 文本参数写入连接串：回执与账本无明文，标 `masked`，回滚被拒。
  - 登记表：补写法后被脱敏的仍只有 8 个真凭据（逐个核对新写法没有误伤普通参数）。
- **结果**：与上一批相同的相关测试集 575 passed；ruff、doc sync、strict code-size、`git diff --check`、clean-package 见提交前检查。
- **变异验证**：39 个全部被抓出。上一批 20 个按新代码更新匹配文本（2 个随实现删除）；新增 19 个：任意开关后的形状规则、
  开关后的网址被当成请求头行、没有开关也按形状遮、七个新凭据写法逐个去掉、点不当分隔符、文本不拆 `名字=值`、
  非凭据的值不再检查、单引号与双引号括起的值、`名字=值` 从词中间开始、文本里的网址不遮、凭据名下的映射整个换成 `***`、
  没有 `=` 的项也按角色处理。每个都在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑并逐字节恢复。

## 字典与列表参数的结构脱敏（2026-09-27，分支 `claude/be-structured-masking`，基于 `f824b6c10`）

- **来源**：集成者派活。`mask_value` 只按顶层键名判断再整值 `str()`，`model_custom_headers`（Authorization、x-api-key 等）与
  `mcp_servers`（每个服务器的 env、args 里的令牌）在 `/settings show`、user_config view/search 里整段明文，模型经 user_config 就能看到。
  这是早就存在的漏洞，不是上一批引入的。`model_auth_ref` 核对为引用，不含凭据。
- **测试**：新增 `test_structured_masking.py` 5 项，全部用假值，覆盖以下几方面：
  - 结构规则：请求头与环境变量只留键名，嵌套凭据键、`--凭据名 值`、`--凭据名=值`、docker 的 `-e 凭据名=值`、网址密码与凭据查询参数
    都遮住，`--header`/`--env` 的值只留名字，`--开关=带密码网址` 也遮；普通字典、列表、元组、`LOG_LEVEL=debug` 与纯变量名照常；不改原值。
  - user_config view/search 看不到明文，普通字典 `runner_timeout_by_role` 照常显示。
  - `/settings` 总览、查看、搜索看不到明文。
  - 修改回执、修改记录、user_config 与 `/settings` 的历史回显都看不到明文（含脱敏收紧前写下的明文旧记录），
    脱敏过的记录回滚被拒绝，原记录不被改写。
  - 命令行 `config-get` 看不到明文。
- **结果**：新文件与参数中心、设置、命令行配置、推理强度、架构守卫、字段读取方、user_config、配置加载、审计控制、管理员身份、
  MCP 注册与日志脱敏相关文件共 572 passed；ruff、doc sync、strict code-size、`git diff --check`、clean-package 见提交前检查。
- **变异验证**：22 个全部被抓出（去掉请求头/环境变量容器、嵌套凭据键、`--凭据名 值`、`名字=值` 的角色判定、网址密码、凭据查询参数、
  记录文本剥引号、`mask_value` 退回只看顶层、记账只遮凭据键、脱敏记录可回滚、user_config 历史不遮、`/settings` 历史不遮、
  `config-get` 先 `str()`、遮挡函数原样返回、元组变列表、`--header`/`--env` 的值不遮、文本请求头原样保留、纯变量名被遮、
  `名字=值` 不查名字形状、`--开关=网址` 不遮密码、下一项是开关也当值遮、去掉凭据开关角色），每个都在
  `PYTHONDONTWRITEBYTECODE=1` 子进程里跑并逐字节恢复。
## 决策审计说清“每个点位最近为什么没触发”（2026-09-27，分支 `claude/9b-decision-miss-reasons`，基于 main `f824b6c10`）

- **来源**：审计里某个点位调用 0 次时，用户只看到“0 次”，分不清是没打开、没到触发点，还是每次都被条件挡下。
- **新测试** `test_decision_reach_counts.py`（9 项）：首次到达即落盘、60 秒内只在内存累加但读取时照样算进去；到期合并写盘不覆盖
  另一进程已写的计数；超过 7 天的小时桶被修剪、坏文件按空；开关关闭或没有规范路径时不计；写失败把计数放回、下次补写；
  阶段原因码；大白话与未登记原因码原样显示；未接入点位 `covered=false`；`counted_material` 只把输入错误记成 `bad_material`，
  隐私跳过与意外异常不记。文件还导出各点位测试共用的 `reach_counter`。
- **各点位**：delivery_quality 与 action_candidate 的全部不合格场景（27 个与 31 个）逐个核对“只记一个且是对的原因码、没有调用”；
  external_material_order 9 个未证明场景加非抓取、关闭、材料超限；planning 4 个范围场景加关闭、空原话、非主对话、材料不合格；
  skill_proposal_review、recall、pre_recall、curator、curator_relation 各一组原因加 `called`。
  四个热路径点位（三个工具回执点位与 planning）和 recall 另核对“资格不过时不打开决策阶段”。
- **展示**：`test_decision_audit_controls.py` 核对 `audit_records` 的 `point_diagnostics`（点位全集、是否开启、次数、大白话、未接入点位）
  与 TUI 走的 `decision_read` 附同一份诊断；`test_tui_decision_menu.py` 核对菜单行文案，并在真实 prompt_toolkit pipe 里打开
  “逐接入点设置”看到“近24小时检查3次、调用1次，最多是因为：……（2次）”和“未统计未触发原因”。
- **作用域**：`test_gateway_per_user_scoping.py` 核对 Gateway 按用户作用域时，结果日志与到达计数都落在该用户自己的 home，
  两个用户互不相同、也不是基础 owner 的文件（写这条时发现 `owner_resolver` 漏了新路径，已补）。
- **变异验证（30 种全部被抓住，逐个字节级还原）**：开关失效、去掉节流、合并改覆盖、不修剪、读取不含未落盘计数、隐私也记成
  `bad_material`、阶段不比 run、未接入点位当已接入、delivery_quality 不记 `called` / 数量原因记错 / 坏来源不计、
  action_candidate 失败记成对不上、external_material_order 阶段原因不记、planning 空原话不计、skill_proposal_review 条数不计、
  recall 材料不合格不计、pre_recall 名额记成字数、curator 阶段原因不记、curator_relation 记忆变更不计、作用域路径不重设、
  隐私跳过不计、decision_read 不附诊断、菜单把未接入点位显示成 0 次、审计不附诊断；资格不过也打开阶段（四个点位各一）、
  recall 点位关闭也报 unavailable、recall 输入不满足也建阶段。
- **触发行为不变**：各点位原有测试全部原样通过（只把三处替身目标从 `_eligible` 改名为 `_miss_reason`）。
- **集成复核补测**（集成者）：独立变异抽查 5 种，“汇总忽略时间窗（7 天数据冒充近 24 小时）”与“读取不校验格式版本”两种存活；补 `test_summary_counts_only_hours_inside_the_window_and_ignores_foreign_schemas` 后两种都被抓住。全仓 12 分片 22,234 passed、0 失败。

## Compact：带归档引用的工具回执可以移入摘要来源（2026-09-27，分支 `claude/compact-archived-refs`，基于 main `54a384147`）

- **来源**：Codex G02 真实验收（通用能力包，65k 窗口）。五组工具往返各含外置输出或 read_artifact 的引用回执；分区把任何带引用回执都判为不完整，
  整组留在保留区、同四元归档被连带排除，`partition_recovery_tool_source` 返回 None，强制恢复报 `COMPACT_TOOL_COVERAGE_UNKNOWN`。
- **做法**：引用值等于同一四元身份原归档自己写下的输出位置（`output_path`/`artifact_ref`/`source_artifact_ref`）才算由归档保存；
  其余引用（工具自报、媒体、未记录）和 json/数据块照旧整组保留；`CarriedToolCompactSource` 用自身 source_records 重算同一判据。
- **新测试**：
  - `test_compact_tool_partition.py` 新增 9 项：外置输出引用可移入；read_artifact 来源引用按值匹配；6 种保留情形（未记录、工具自报、媒体、
    json 块、带数据的文本块、无归档记录）；来源对象按自身归档复核。
  - 新文件 `test_compact_tool_ref_archive_chain.py`（4 项），零网络组合复现：沿真实外置/归档投影、与 executor 同形的回执、归档记录、
    reducer 实时投影、原生 IR 记录器和真实 read_artifact 读取器。外置与内联同组、read_artifact 来源引用、恢复 attempt 的调用整组移入，
    工具自报引用的组保留；摘要素材等于原 IR。同步用例钉住：投影给出的完整输出引用总是归档自有字段，工具自报引用从不属于这些字段。
- **复现**：组合用例放到修复前的 `54a384147` 上运行，分区返回 None，与 G02 现场一致。
- **变异验证**：12 个变异抓到 11 个（改回“有引用即不完整”、接受未记录引用、没有归档也接受、去掉媒体判断、去掉数据块判断、计入顶层
  tool_result_refs、计入信封引用、去掉 source_artifact_ref、引用块不核对值、来源对象跳过归档复核、分区不用归档）。去掉 `artifact_ref`
  字段的变异存活：真实记录里它总与 `output_path` 或 `source_artifact_ref` 同值出现，属等价变异。
- **相关测试**：压缩、归档与投影相关的 34 个测试文件加新文件、架构守卫、常数名守卫，600 passed、4 xpassed（原有非严格 xfail）。
## 参数中心第二批：凭据脱敏与边界收紧、说明纠正、回执补全（2026-09-27，分支 `claude/be-param-descriptions` 第二个提交）

- **来源**：集成者转来分类子代理的发现。`input_media_token_reserve` 等 8 个数量/上限参数因名字里有 token/prompt/path/owner/home/audit
  被当成边界项（模型不能改），前者还被当成凭据（修改记录标脱敏、不能回滚）；`additional_write_roots` 拿到了 prompt_files 的说明；
  `lease_stale_without_heartbeat_seconds` 的说明写成 Gateway 租约。复核时另发现：回显脱敏只认 3 个飞书键，`api_key`、
  `gateway_auth_token`、`qq_app_secret`、两个 `*_embedding_api_key` 在 `/settings show`、user_config 查看与 `config-get` 里是明文。
- **测试**：
  - `test_parameter_registry.py`：14 个数字旋钮放开且显示真实值；8 个真凭据仍脱敏且是边界；凭据名只按完整末尾片段认；
    名字像凭据的数字键仍是边界；路径、写入范围、飞书/QQ 凭据、owner 身份、访问锁、审计保留期等 20 个键仍是边界；
    纠正后的三条说明与代码实际用途一致。
  - `test_parameter_changes.py`：user_config 的 reset/revert 回执附实际效果，max_tokens 恢复默认按登记默认值算。
  - `test_settings_chat_control.py`：标签改为“实际效果”，`/settings reset`、`revert` 回执附实际效果。
  - `test_cli_config.py`：`config-get api_key` 脱敏。
- **结果**：上述文件与 `test_reasoning_effort.py`、`test_architecture_guardrails.py`、`test_config_field_readers.py`、`test_user_config_*.py`、
  `test_config_*.py`、`test_decision_audit_controls.py`、`test_admin_identity_*.py`、`test_gateway_status*.py` 等共 475 passed；
  ruff、doc sync、strict code-size、`git diff --check`、clean-package 见提交前检查。
- **变异验证**：本批 14 个全部被抓出（脱敏退回只认飞书键、凭据按子串认、指向类记号也拦数字、凭据不强制边界、“锁”降为指向类、
  审计保留期移出名单、登记时不传值类型、`config-get` 不脱敏、prompt 注释放回错位、删掉审计 worker 说明、标签改回、
  `/settings reset` 与 user_config reset/revert 不附效果、恢复默认用 None 算）；第一批 11 个在当前代码上重跑也全部被抓出。

## 参数中心：说明读行尾注释与推理强度如实生效值（2026-09-27，分支 `claude/be-param-descriptions`，基于 main `54a384147`）

- **来源**：集成者派活。用户要求“不用猜参数是干啥的”，并按“把用户当什么都不懂来设计”：默认就合理，命令保留。
  实测登记表 389 个字段里 216 个说明为空，其中 39 个其实写了行尾注释（`key: 值  # 说明`），只是没被读到；`/settings show` 与
  `user_config view` 对它们只显示“（没有说明）”。另外用户的模型接口是 opencode.ai，不在推理参数已确认名单里，`model_reasoning_effort=max`
  一个字段都不发，但 my-agent 经 `user_config` 修改时回执只说写入成功。
- **测试**：
  - `test_parameter_registry.py`：行尾注释取法 7 例（与加载器同一引号规则，引号里的 `#` 不算）；键正上方注释优先、空行隔断、同名键取第一次；
    说明为空的字段只允许在 `tests/fixtures/parameter_description_baseline.json`（按原因分组）里，名单外新增或名单里的已有说明/已删除都失败；
    推理强度实际效果与 `/effort` 回执同源（`resolved_reasoning_control` + `describe_reasoning_effect`）；新值视图不改原配置；新值按登记类型转换。
  - `test_parameter_changes.py`：`user_config` 的 set 回执对推理强度与 max_tokens 附上新值在会话模型上的实际效果，普通参数不多给字段。
  - `test_settings_chat_control.py`：`/settings set` 附“按新值在默认模型上的实际效果”，`/settings show` 如实说明。
- **结果**：上述文件与 `test_reasoning_effort.py`、`test_architecture_guardrails.py`、`test_config_field_readers.py`、`test_user_config_*.py`、
  配置加载相关、恢复码合同共 323 passed；ruff、doc sync、strict code-size、`git diff --check`、clean-package 见提交前检查。
- **变异验证**：11 个全部被抓出（去掉行尾注释兜底、反转优先级、改用不认引号的切分、撤掉推理强度规则、绕过控制方式解析、新值视图不替换、
  新值不按类型转换、`user_config` 回执不附效果、`/settings set` 不附效果、基线名单多一个已有说明的键、少一个空说明的键）；
  每个都在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑并逐字节恢复。

## 决策点长原话改为首尾节选、不再整点跳过（2026-09-27，分支 `claude/integrate-self-dev`，接在 my-agent 的 1b69ce34b 之后）

- **来源**：用户要求参数默认就合理、99% 的人不会手动调（“把用户当什么都不懂来设计”）。原来超出上限的原话让 planning 等三个点位整点跳过，
  用户粘贴的 3186 字指令因此用不上这些决策。
- **做法**：`backends/decision_protocol.decision_request_excerpt` 取首尾节选并返回完整性标注，三个点位把节选与
  `current_request_completeness` 一起放进 state，指令说明节选时信息不足选 need_data；预算默认 2000（`decision_request_max_chars`）。
- **测试**：三个决策测试文件的超长用例改为“决策照常发出、发出的是带标注的首尾节选”（含 3186 字真实长度），预算可调、`0` 不截取、
  非法值回落默认 2000；原“超长不调用”的参数项删除；隐私用例允许新增的计数型标注键。
- **变异验证**：改回超长就跳过、节选只留开头、去掉标注、预算写死，四种都被抓住。

## 决策请求字符上限收成参数 `decision_request_max_chars`（2026-09-27，分支 `my-agent/self-dev`，基于 main `111d32baa`，集成到 main `84c29b39d` 之上）

- **来源**：planning、delivery_quality、action_candidate 三个决策点位各自写死一份 `_MAX_REQUEST_CHARS = 1024`，
  参数中心阶段 3 要合并的同名常数；真实 TUI 里 3186 字粘贴会被整点跳过且只有一行 `request_too_long`。
- **新测试**：三个模块的测试各加 `test_configured_request_limit_controls_skip`（6 组参数：上限 2000 时 1025 字不再超长、
  2001 字仍跳过、`0` 表示不限制、非整数/负数/缺字段回落默认 1024）。原有 1025 字触发 `request_too_long` 的用例保持不变。
- **变异验证**：把 `settings/defaults.py::decision_request_max_chars` 临时改回写死 1024，三个模块共 12 个新用例失败
  （每模块 4 个：上限 2000 的两组和 `0` 那组），恢复实现后 24 项全过。
- 相关模块：`agent_core/decision_planning.py`、`agent_core/tool_context/decision_delivery_quality.py`、
  `agent_core/tool_context/decision_action_candidate.py`、`settings/defaults.py`、`settings/config.py`、`config/agent_config.yaml`。

## 正在运行的安装目录写保护（2026-09-27，分支 `claude/runtime-write-guard`，基于 main `fbf3ddfef`）

- **来源**：用户问 my-agent 改自己代码会不会出事。Full Access 下文件工具与命令都能写本机任何位置，包括正在运行的安装目录。
- **新测试** `test_runtime_write_guards.py`（7 项）：
  - 安装目录只按进程事实认定：虚拟环境整体；site-packages 只保护包目录；源码检出不保护。
  - 开关开/关。
  - 文件工具命中安装目录就拒，更深的允许目录也不能穿过；只有这个键时不会把不限范围的写入变成全拒。
  - Shell 与终端的沙箱只读路径包含安装目录。
  - 端到端：本机管理员 Full Access 主会话仍能写外部开发工作树，写不了安装目录；关掉开关后可写。
- **变异验证**：去掉文件工具检查、去掉 Shell 投影、忽略开关、改为借用 `forbidden_write_roots`（会让 Full Access 全部拒写），四种都被抓住。

## 参数中心阶段 3 第一批：同名常数收成一处（2026-09-27，分支 `claude/param-center-dup-constants`，基于 main `035f9f57b`）

- **新测试** `test_constant_names_unique.py`：扫描 `agent_py_agent/agent` 与 `agent_py_agent/cli` 的模块级数值常数（导入不算定义），
  同名只允许一个模块定义；确属不同含义的 19 个名字列白名单并写原因，名单里的名字不再重复同样失败。
- **变异验证**：在另一个模块补回一份同名常数、从白名单删掉仍在重复的名字、白名单多一个已不重复的名字，三种都失败。
- **行为不变**：只删死常数、改为导入或按真实含义改名，所有数值不变；推送前全仓 12 分片。

## 参数查看显示实际使用值、去掉旧 tunable 字段（2026-09-27，分支 `claude/settings-view-facts`，基于 main `111d32baa`）

- **来源**：my-agent 在开发交流板上提问：一是 `user_config view` 同时给出 writable=true 与 tunable=false，它以为 max_tokens 改不了；
  二是配置 64K 在 128K 窗口模型上实际只用 32768，查看时看不出来。
- **测试**：
  - `test_parameter_registry.py::test_applied_value_uses_the_single_output_cap_formula`：128K 窗口得 32768；窗口未知等于配置值；
    没有派生规则的参数、没有配置对象时都返回 None。
  - `test_parameter_changes.py`：view 结果不再含 `tunable`；按本片模型窗口给 `applied_value`，同时 running_value 仍是配置值。
  - `test_settings_chat_control.py::test_show_reports_the_applied_output_cap_for_the_default_model`：show 多一行“实际使用值：32768”
    并提到 /model；没有派生规则的参数没有这一行。
- **变异验证**：去掉工具里的 applied_value、去掉 show 的那一行、把 tunable 加回来，三种都被抓住。

## 协作状态更新按 case 串行（2026-09-27，分支 `claude/collab-request-race`，基于 main `b7fe42a90`）

- **来源**：12 分片全仓运行时 `test_concurrent_request_status_update_no_corruption` 偶发失败，终态停在 `open`。原因是请求状态
  读改写没有锁：`rerouted` 不是协议状态，这类更新保留读到的状态，很早读到 `open` 的线程最后写入，就把已完成的请求写回 `open`。
- **新测试**（`test_collaboration_concurrency.py`）：`test_stale_request_snapshot_cannot_overwrite_a_newer_status`、
  `test_stale_case_snapshot_cannot_overwrite_a_newer_status` 用事件把慢线程卡在“已读未写”，确定性复现旧快照覆盖；
  去掉锁两项都失败（终态回到 `open`），加锁后通过。

## 参数中心阶段 2b：管理员自身开发工作树约定（2026-09-27，分支 `claude/self-dev-worktree`，基于 main `c9142d09a`）

- `test_prompting_builder.py` 新增 6 项：本机管理员 + Full Access + 真实 git 工作树时 Owner Scope 写明正在运行的包目录与开发工作树；
  非 Full Access、远程 owner、没有 `.git`、相对路径、未配置五种情况都不出现这段。
- `test_parameter_registry.py`：`self_dev_worktree` 是边界项，模型不能改（键名记号新增 `worktree`）。
- 变异验证：分别去掉 Full Access、管理员、`.git`、绝对路径四个条件，各自被对应用例抓住（相对路径用例把 cwd 切到临时目录，
  否则会被 `.git` 检查顺带挡掉，测不到绝对路径条件）。

## 直接展示工具结果只保留逻辑续读锚点（移植自 Codex b496e1a0c，2026-09-27，基于 main `49b169e3e`）

- 只移植 `agent_core/tool_context/reducer.py` 与 `test_tool_context_reducer.py`（及两份能干净应用的设计/结构文档）；能力包原生管线测试和包文档
  留在能力内化分支，随整线合并。main 上重跑：reducer 直接用例与外置/headroom/消息适配/compact 引用/原生 IR 相邻测试 182 项通过，
  变异（恢复渲染完整结果）2 项失败被抓住；推送前全仓 12 分片。

## 输出上限回归修正：按任意已知窗口夹取（2026-09-27，分支 `claude/output-cap-regression`，基于 main `ea0b539cb`）

- **来源**：dsh-9b 的 CI 监视发现 9207d54e5 起 8 个 compact 用例稳定失败（test_compact_native_ir_recovery 5 个、test_subagent_compact_recovery 2 个、
  test_subagent_runtime_compact 1 个）。原因：64K 默认值只夹显式窗口；没有 max_tokens 的替身估算回退到配置原值 65536；
  `compact_request_budget` 用“窗口×0.8−输出上限”，8 万以下窗口时变成 1；两个夹具在构造后缩窗口却沿用大窗口的后端上限。
- **修正**：`output_cap_for_window` 为唯一公式（已知窗口即夹取，不再要求显式）；工厂构造时用它算 `backend.max_tokens`（就是发送值）；
  `call_runtime.max_output_tokens` 对没有 max_tokens 的后端按同一公式估算。三个构造后缩窗口的夹具（compact_native_ir_recovery、
  subagent_compact_recovery、host_summary_phase_lifetime，后者在 main 上同样失败但不在 CI 清单里）按同一公式重算后端上限；
  `test_manual_compact_reports_typed_failure_instead_of_generic_retry` 改为显式声明未夹取的替身上限（原来靠回退漏洞造出失败形态）。
- **曾试过但放弃**：在后端每次发送时再按当前窗口夹取。全仓运行发现它打破“backend.max_tokens 就是发送值”的既有约定，
  另有 9 个直接设置 backend.max_tokens 的预留/预算用例失败，于是改回构造时夹取。
- **测试**：`test_model_output_cap.py` 14 项（任意已知窗口夹取、请求体等于后端上限、替身估算被夹取）；推送前按 12 个分片跑全仓 pytest
  （约 2.1 万项；第一版只跑了 focused 测试，漏掉 compact 恢复与子代理预检），分片用短 basetemp（长路径会让 subagent_debug_trace 因文件名过长失败）。

## 参数中心阶段 2：登记表、唯一写入口、`/settings`（2026-09-27，分支 `claude/param-center-phase2`，基于 main `89af9bbd3`）

- **新测试** `test_parameter_registry.py`：登记表覆盖全部字段、说明取自 YAML、安全相关键（凭据、端点、提示词、审计、工具开关、
  写根、服务、执行模式、锁等）一律不可由模型改、普通参数可改、凭据仅显式放行且脱敏、搜索完全匹配优先。
- **新测试** `test_parameter_changes.py`：按类型写入（`enable_self_learning: false` 不加引号，加载后是 False）并真正生效；各类拒绝不改文件、
  不记账；加载器会改值时恢复原文件；记录倒序、编号前缀回滚、连续回滚回到默认；凭据脱敏不可回滚；工具用当前加载的配置文件。
- **新测试** `test_settings_chat_control.py`（20 项）：解析、Gateway 分派、TUI 还原与本地拒绝、仅管理员（飞书普通用户与空身份拒绝且看不到值）、
  总览/搜索/详情/修改/记录/回滚完整流程、边界拒绝、普通异常只说没能完整确认。
- **变异验证**：去掉管理员判断、去掉 TUI 本地拒绝、去掉 Gateway 分派，三种都被抓住。

## 参数中心阶段 1：删除死配置并加读取方扫描（2026-09-27，分支 `claude/param-center-phase1`，基于 main `9207d54e5`）

- **新测试** `test_config_field_readers.py`：扫描产品代码的属性名与字符串键（加决策设置映射），要求每个 AgentConfig 字段都有读取方；
  0.3 秒。变异验证：临时加一个无人读取的字段，测试失败。
- 删除 `lsp_servers`、`scheduler_mode`、`extensions_dir`、`continuation_reminder_seconds`、`task_max_grandchildren`（YAML、dataclass、
  CLI_REFERENCE 示例、README 说明、`test_config_normalize.py` 的字段清单同步）；用户配置残留时只告警。

## 参数中心阶段 0：输出上限统一 64K、验证“未计入”说明（2026-09-27，分支 `claude/param-center-phase0`，基于 main `c71b1353d`）

- **新测试** `test_model_output_cap.py`：常量、随包 YAML、AgentConfig 默认值同为 65536；窗口已明确时上限 = min(配置, 窗口 ÷ 4)，
  未明确时按原值（7 组参数）；后端工厂对默认模型也夹取；`vision_*` 字段与 YAML 键已删除，用户配置残留时只告警。
- **扩充** `test_verification_runtime.py`：接 `| tail`、用 `;`、`||` 或后台串联的 pytest 不计入证据但带 `verification_skipped`，
  并出现在 `[runtime-verification-facts]`；非验证命令的管道、引号里的 `|`、未正常退出的命令都不给这条说明。
- **真实短测**（上线前，主用户 15 个模型档案，新上限经后端工厂）：deepseek-v4-flash/pro/vision-exp、v4.1-flash、MiniMax-M2.7（26 万窗口）、
  MiniMax-M3 全部正常回复；本机 127.0.0.1:8901 与 18881 两个本地端点未运行（与本改动无关）；Jev 决策后端不读 max_tokens。
  opencode 类服务商要求绑定宿主会话，短测需在 `provider_session_scope` 内调用。
- focused：60 个相关测试文件（配置、模型档案、后端、网关选模、输出预留、子代理选模、验证、交付质量、运行事实等）加架构守卫。

## C16全仓回归修复（2026-09-27，验证中）

固定62c4d4478在Python 3.12全仓运行23288项，86 failures、0 errors、53 skipped，耗时2603.763秒，运行前后Python源码指纹相同。86项分为74项子代理创建、10项后台任务绑定、1项错误码登记及1项后台进程停止；不把它们描述为86个独立缺陷。原日志/JUnit和失败索引保留，旧529项定向通过不代表全仓通过。首次Python 3.14尝试因缺hypothesis/pyte而在collection结束，另列环境记录；没有安装依赖或改日用配置。

配置补片6fae6e4af精确吸收为2e4b9f606，产品同字节核对后补模块说明。新增配置缓存反例及原选包失败恢复断言在修前11项中5项失败；修后19文件399项全部通过、0失败/跳过，相关源码前后摘要相同。覆盖原74项创建失败、真实typed配置的开/关和原磁盘读取、子入口/决策消费者、取消传播、选择失败恢复及错误码注册。证据为私有`candidate-16-regression-root-red-01`与`candidate-16-regression-root-focused-01`，各自命令、日志、JUnit、源码摘要单列。

后台已有任务夹具改用真实TaskLink，保留旧任务身份/pins/marker。新定时运行在原claim后、模型前绑定；合法冻结回复优先使用原交付，定时claim直到原回复处置后按唯一终态映射结算。原14文件373项通过后，独立复核发现准入期间claim被接手、finish返回None仍把wake标为handled；新增两条真实repository替换过期claim的确定性反例在修前2/2失败，修后仅实际结算成功才返回stale，否则busy并保留pending。最终相同14文件**375 passed、0 failed、0 errors、0 skipped**，65.756秒，源码前后摘要相同。覆盖原回复拒收后再投只调用一次模型、external_sent只补canonical、取消抑制、无有效缓存终态结算、无法确认pending/状态保留待处理及原claim接手边界。证据为私有`candidate-16-admission-cas-red-01`和`candidate-16-regression-final-focused-02`；旧373与本次375不相加。

本批最终字节的Ruff、doc-sync、strict code-size、diff和clean-package全部通过，本地严格gate已通过，线上CI未作为验收来源。独立只读末审关闭上述CAS发现，范围内未见其它确认阻断；报告SHA256为`20729f2f7ab4da3c945c99379fa6cfe934355dcc17dca497e0a116a14bcf55cf`，审阅与实际测试分别记账。

原进程停止失败由Claude继续调查，孤立复跑不能替代全仓验证。两组399项/375项分别记账，固定组合仍须Claude重跑全仓与严格gate。私有运行仍9d5786952，新最终原生系列未启动；本批回归不修改保留集0/36、C16真实失败或发布状态。

## 第十六候选本线组合（2026-09-27，本地通过，原生部分通过）

固定f824窄组合为8f53ee9b2，31个相关文件529 passed、0 failed/skipped，68.107秒；JUnit逐项核对，前后Python源码指纹一致。两产品/两测试文件与原提交同字节；真实归档/投影/reader/IR组合和三宿主恢复、包入口/版本接续分别验证，未运行真实模型。Ruff、doc-sync、strict-size、diff和clean-package全部rc0，本地严格gate已通过，线上CI未作为验收来源。原0545的951项、下述作者侧600项和本轮529项不累计；私有运行版未切换，G02失败仍待原生复验。完整命令/日志/JUnit在私有candidate-16证据中，见[验收边界](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十六候选compact归档引用修复组合2026-09-27本地通过)。

随后仅吸收作者两行LLM注释为14e49949d，AST与8f53相同；独立只读末审无确认功能阻断。文档/文件树及尺寸报告收尾只复核必要静态检查，不重复529项，也不把代码复核替代原生验收。

后续固定9d5786952已构建/安装，1424成员逐一一致。原G02需求在新CAP06、已有官方M2.7/65536 profile仅发送一次：自动第1代67140→46998真实提交、同代7份资源get成功；第2代摘要后完整候选报`COMPACT_CANDIDATE_TOO_LARGE`，没有原脚本物化/执行或交付。最终10 HTTP/0重试、pins/marker/输入/6配置/安装账保持。本轮是自动提交与同代读取分项通过、整体需求失败，不覆盖旧记录、不计保留集；见[原生证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#c16原生自动提交与资源续读2026-09-27部分通过)。

最终重复验收原矩阵0/36保持；仅准备当前允许范围内A/B/N九例×3次的元数据安排，C九次不启动，未提交新prompt。正式系列须先绑定准确整合候选及原开发验收，不跨候选累计，不以文档预览代替产品组合测试。 本线相对共同底本的Python生产/测试增删共14284物理行；最终整合diff须重计，按当前规模应在focused tests及严格gate之外追加全仓pytest，原529项不代表整套集成验收。

以下修复段保留Claude固定提交`f824b6c1064309a6300bc894abf386194cf891d3`的作者侧证据；其中测试数不代表本线0545组合或私有原生验收。

## Compact：带归档引用的工具回执可以移入摘要来源（2026-09-27，分支 `claude/compact-archived-refs`，基于 main `54a384147`）

- **来源**：Codex G02 真实验收（通用能力包，65k 窗口）。五组工具往返各含外置输出或 read_artifact 的引用回执；分区把任何带引用回执都判为不完整，
  整组留在保留区、同四元归档被连带排除，`partition_recovery_tool_source` 返回 None，强制恢复报 `COMPACT_TOOL_COVERAGE_UNKNOWN`。
- **做法**：引用值等于同一四元身份原归档自己写下的输出位置（`output_path`/`artifact_ref`/`source_artifact_ref`）才算由归档保存；
  其余引用（工具自报、媒体、未记录）和 json/数据块照旧整组保留；`CarriedToolCompactSource` 用自身 source_records 重算同一判据。
- **新测试**：
  - `test_compact_tool_partition.py` 新增 9 项：外置输出引用可移入；read_artifact 来源引用按值匹配；6 种保留情形（未记录、工具自报、媒体、
    json 块、带数据的文本块、无归档记录）；来源对象按自身归档复核。
  - 新文件 `test_compact_tool_ref_archive_chain.py`（4 项），零网络组合复现：沿真实外置/归档投影、与 executor 同形的回执、归档记录、
    reducer 实时投影、原生 IR 记录器和真实 read_artifact 读取器。外置与内联同组、read_artifact 来源引用、恢复 attempt 的调用整组移入，
    工具自报引用的组保留；摘要素材等于原 IR。同步用例钉住：投影给出的完整输出引用总是归档自有字段，工具自报引用从不属于这些字段。
- **复现**：组合用例放到修复前的 `54a384147` 上运行，分区返回 None，与 G02 现场一致。
- **变异验证**：12 个变异抓到 11 个（改回“有引用即不完整”、接受未记录引用、没有归档也接受、去掉媒体判断、去掉数据块判断、计入顶层
  tool_result_refs、计入信封引用、去掉 source_artifact_ref、引用块不核对值、来源对象跳过归档复核、分区不用归档）。去掉 `artifact_ref`
  字段的变异存活：真实记录里它总与 `output_path` 或 `source_artifact_ref` 同值出现，属等价变异。
- **相关测试**：压缩、归档与投影相关的 34 个测试文件加新文件、架构守卫、常数名守卫，600 passed、4 xpassed（原有非严格 xfail）。

能力包验收按用户2026-09-27澄清，以通用功能为主，短剧等只是样例。通用机制、代表包真实迁移/执行、领域内容质量分栏保留证据；不得把组件通过当自然使用，也不得因情节质量问题持续扩展短剧专项功能。旧失败和0/36原始计数不改写，下一轮先补功能所需的证据缺口，详见[唯一Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

## G03普通任务对照（2026-09-27）

沿用原N05输入/提示词、当前0545产品及既有262144profile，在新原生CAP05只提交一次。官方M2.7三次HTTP（2主调用、1辅助空选）、0重试，15.873秒后原任务completed；仅成功读取一次会议文本，三条待办与原文一致，无包get/物化/执行。4092旧文件、6配置及安装表摘要保持。该私有选包开启臂通过本例，不改判早期N05、不计保留集、不覆盖G02自动Compact；[完整证据边界](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#g03普通任务对照2026-09-27开发例通过)单列。

本轮发布资料按当前源码补齐开关、缓存与新任务资格；产品未变，仅运行doc-sync和diff检查，不重复951项。独立审阅未运行新模型或管理操作。

## 第十五候选受控自动 Compact（2026-09-27）

产品仍0545，配置臂通过原生`/model`为新CAP06线程建立独立65536窗口profile、沿用官方MiniMax-M2.7 provider；原262144默认、其他profile及5份其他配置保持。只提交一次普通资源盘点并末阶段执行原checker的需求，没有手动Compact或测试者补任务。此臂只验证受控预算下的自动路径，不能算默认窗口自然长任务覆盖。

G02共5次主模型及1次选择辅助HTTP、0重试，46.508秒后请求因`COMPACT_TOOL_COVERAGE_UNKNOWN`失败。generation0、checkpoint空、没有摘要HTTP或checker/产物；Task仍active，原A/B pins和marker不变。14对工具与12条索引差异来自有界reader原合同，不能判为丢记录。独立复核以真实归档/投影/IR/carry源码链解释refs整组保留及archive排除冲突，未捕获完整历史typed IR；小窗口是触发条件，不归因容量不足。

观察汇总SHA256为`b260e10697d6257fa75f839cbd072790f2030344ac25099e6d59d23eab32e00c`，独立复核SHA256为`76d61b124ba8af01ee694a9a1007db83ec7a63055473098a8a312556048d92c9`，完整身份和引用角色见[验收记录](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十五候选受控自动-compact2026-09-27通用机制失败)。本轮产品未变，不重复此前951项，文档收尾运行doc-sync和diff检查。修复方应先用真实archiver/projection建立零网络组合用例，再验证carry后的来源选择，不能仅用无refs的手建ToolResult成功fixture代替本例。

## 第十四候选通用功能组合与真实资源消费（2026-09-27）

固定0545源码的951项focused tests通过（0失败、0跳过，61.09秒）；Ruff、doc-sync、strict code-size、diff及clean-package全部通过，本地严格gate已通过，线上CI未作为验收来源。离线wheel1424成员与源码和实际安装一致，pip check及Python3.12导入通过；目录额外`.DS_Store`保留单列。测试/构建/安装证据不相互累计成全仓结果。

新原生G01一次普通检查需求，官方M2.7、8HTTP/0retry/60.92秒。原source_ref复制23750字节脚本与包同SHA并实际执行，检测出已知输入错误；两输入和4092旧文件、6配置、安装表保持。完整stdout与保存JSON均8条warning，最终文字误报9条，故原资源链通过而报告准确性部分通过。未观察分页归档恢复/自动Compact，未替原A02改判，保留集0/36。详见[范围与原始证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十四候选通用资源消费2026-09-27)。本轮仓库仅文档变化，后续文档收尾只复跑doc-sync/diff及必要静态守卫，不重复951项。

以下为历史候选各自的源码和运行证据。

## A0.3.0普通原生复验（2026-09-27）

包源码固定bcbf40e8，宿主仍938（1422安装成员重新核对一致）。同一Gateway的三次原生管理写均成功；新CAP01一次原A02需求，19次HTTP、0重试、250.032秒，自然结束。原checker错误拼接JSON信封后执行失败，模型使用简化检查；独立审阅确认7镜60秒及人物字段的有限事实，同时判交接、取舍说明与跨场来源未过。运行时ok与业务失败分记，新字面诊断未成功执行，不借用下节141项或旧轮checker成功证明采用。4067旧文件及6配置无变化，测试者未补产物/代执行。详见[原生证据](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a030原生开发复验2026-09-27)；本轮只补文档，按文档同步与diff检查收尾，不重跑未改产品测试，保留集0/36保持。

## A0.3.0人物依据与字面诊断（2026-09-27）

只运行A专用 `test_capability_package_drama_text_visibility.py`、`test_capability_package_drama_text_basis.py`、`test_capability_package_drama_text_duration.py`，以及examples中`test_samples_build_reproducibly_and_expose_only_one_package[drama-text-a]`和全部`test_text_*`明确节点：**141 passed，5.88秒**。通过原隔离CLI检查字节不变、引用、名字边界/歧义/退出、预算null与完整扫描后警告裁剪；未运行其它样包或全仓pytest，组件结果不代替模型采用/语义质量。

两次真实builder CLI可重复，15资源逐字节/摘要一致，ZIP SHA256 `98e21e0accb101b1d4c3640a1d5cc5de0bad8bf58ae4f1b79e9ca7dc3b6fa68e`。定向源码前后同字节；精确节点、stdout、JUnit、构建与来源指纹保存在私有candidate-13证据，摘要见[本轮验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a030人物依据与字面诊断2026-09-27)。独立末审及本地严格gate已通过（线上CI未作为验收来源）；随后原生热更新及业务失败见本页首节。

## 第十二候选：普通工具结果逻辑引用（2026-09-27）

最终冻结的reducer、对应单测和native包链测试共25项通过（4个测试文件的明确节点，未跑全仓pytest）。覆盖直接正文/live正文/通用外置摘要、canonical与业务refs不变、源逻辑引用、原生模型消息及原链read_artifact多窗口→source_ref复制。替身读完的是当前5000字符资源页，随后复制完整原字节，未读取整份长资源或调用真实模型。

前一实现24项通过后，独立审阅发现总入口过滤会改变JSON摘要选择；新增普通next_tool_call JSON＋ref内容块回归先1 failed，过滤移到直接展示入口后最终25项通过。前后计数分别留证，不累加。本次未运行安全类测试或C任务。Ruff、doc-sync、strict code-size、diff、clean-package全部通过，独立末审无新增确认阻断；线上CI未作为验收来源。结果记录在[本轮验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第十二候选模型引用投影2026-09-27)，此前真实质量失败和保留集0/36保持。

## A0.2.2作者修订方法候选（2026-09-27）

只改A包已有方法/声明，原checker、分表和模板未变。选择已有A专用`test_samples_build_reproducibly_and_expose_only_one_package[drama-text-a]`，1 passed；两次真实builder CLI输出同字节，34881字节，SHA256 `fc053c1227727f47a7d7e511c61ba524e04de18d0c67ff91a824572bd91b1ca7`。14资源、入口2633字符，冻结源码无漂移。未运行其他样包或重跑未改脚本，原65项和343项不算本轮新通过数。
独立方法窄审无确认阻断；本地严格gate已通过（Ruff、doc-sync、strict code-size、diff、clean-package），线上CI未作为验收来源。随后原938 Gateway的停用/更新/启用均成功；新原生A02只发一次原普通需求，最终6镜/1场/60秒结构通过，业务语义失败。模型取得完整workflow，修引用、完整回读当前稿后仍遗漏场次说明和人物动作矛盾；三分表未get，checker使用旧轮同字节副本，不算本轮source_ref物化通过。终态后4048个保护文件中4047字节不变，唯一变化为`runs/.DS_Store`目录元数据；旧业务产物/canonical记录与六份配置保持。详细证据见[本轮验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#a022原生开发复验2026-09-27)，旧失败和保留集0/36保持。

## c71回执修复与A0.2.1独立方法复验（2026-09-27）

本片本地严格gate已通过：343项定向测试、Ruff、doc-sync、strict code-size、工作区/暂存区diff和clean-package；线上CI未作为验收来源。doc-sync首次指出Gateway结构文档未同步，补明结构化读/写失败回执的责任后复查通过；未改变生产字节或重跑业务。旧尺寸报告仅生成时间变化，已保留原时间，不提交无意义报告差异。

791组合已保存为5ac8c8789，随后吸收固定c71b1353d；只出现一处Gateway历史文档冲突，保留双方记录。新冻结与前轮2786项指纹只差技能控制服务及其测试两文件；12个受影响/相邻文件 **343 passed（15.15秒）**，新源码无漂移，独立窄复核确认原P2关闭。覆盖删除已经生效但记日志失败时不再声称未改动，以及原读命令、提案、学习、Gateway/TUI分派和配置组合。精确命令与JUnit见私有candidate-10-c71-frozen.json / focused.xml；不把原929或Claude100项计入本轮。

另一路保持已验证938运行版，用原生TUI热更新A0.2.1：停用/更新/启用三项SUCCEEDED，唯一Gateway进程、其他包及配置不变；709个旧canonical文件在新任务结束后仍同字节。更新命令首次Enter留在路径补全后的输入框，观察确认没有新operation后只对原输入再次Enter，原安装账只有一次update，没有重发命令或重放未知结果。

CAP01新A02只提交一次原中文需求/原输入。已确认get三份新审阅方法，原checker物化同字节并在两次结构失败后由任务自己修正，最终7镜/3场/60秒通过。语义业务失败：SH05放入口袋到SH06直接手持缺过渡，最终却称接续一致；SH07原文已有的收棚动作又被总说明列为新增。原操作核验保持partial，读取方法不等于审阅正确；未由测试者改产物或代跑检查。保留集0/36，未启动新的C任务。

观察器修正单列：预备时对task_runs只查closed_at IS NULL，漏计三个旧created/0.0投影；该“全局均关闭”断言已在私有correction记录纠正。实际复用两席及上一轮五个任务均独立核为done/closed_at>0，操作前Gateway队列为空；没有清理或改写那三个旧投影。此观察错误不改写本轮原生结果。

## 第十候选组合791（2026-09-27，定向通过／独立发现未关闭）

组合A方法提交8645daced与固定main 791f5d14b，3处文本冲突保留双方文档历史及布尔字段。原YAML组合测试扩为4组，覆盖全部关闭及语法反馈、Compact回查、决策跳过记录各自单开，三个实际字段均为bool且预算字段不变。
冻结2786个源码/测试/配置/包资源后，用独占短basetemp统一执行30个定向文件：决策点/结果记录5文件、skills聊天与原学习/提案消费者、聊天解析/Gateway/TUI控制、配置、包发现/选择/读/任务引用/首次入口/原生链/Compact等，**929 passed（50.85秒）**，结束后冻结指纹无差异。完整命令及JUnit留私有evidence/candidate-10-main-combine-frozen.json、candidate-10-focused.xml；不是新增真实模型请求，未运行C业务或自定义探针。
绿数未覆盖的新发现：skill_control_service普通异常回“原记录没有改动”，但remove/revert在append_event之前已改文件和registry；追加日志I/O失败会把已生效事实误报成未改动。已交Claude原作者最小修复，静态调用顺序证据不冒充故障注入实测。未取得修复SHA前不宣称组合已验收，也不将旧938的1301项累加到本轮。
同一冻结源码的Ruff、doc-sync、strict code-size、工作区及暂存区diff检查、clean-package均通过；尺寸报告只有生成时间变化，已还原该无意义差异。**本地严格gate已通过，独立发现仍未关闭**；线上CI未作为验收来源，929项不替代新版原生TUI验收。

## A0.2.1 方法候选验证（2026-09-27，本地组件）

基线5cdf8eabdb，A候选0.2.1；只选择现有 `test_capability_package_examples.py::test_samples_build_reproducibly_and_expose_only_one_package[drama-text-a]` 及 `test_capability_package_drama_text_basis.py`、`test_capability_package_drama_text_duration.py`，共65项通过。未增加镜像实现的文本断言，也未运行其他样包测试或真实任务。
官方builder两次构建字节相同：14资源、32484字节，ZIP SHA256 `de5fd3c4596331a79c7e063c2545d7f4e1f63ea32530def4ca73ac5c6d96722f`。入口2546字符，小于旧2915；入口/方法/模板的包内路径均有声明，原checker、JSON模板、两份合成资料及许可证与基线同字节。
首次静态引用扫描误将PROVENANCE中的上游脚本路径算为包成员，已收窄到实际包内导航后通过；首次未构建或安装包。该观察脚本问题不算产品失败。候选未安装，原5例业务结论与最终保留集0/36保持；新版真实采用待验，旧938的1301项不作为本候选的新测试数。
L01独立只读归因只核原生上下文/文件/现有代码，不执行旧业务检查器补证：模型已公开承认42镜525秒与目标的差距，随后只写交接而最终误报48/600；operation核验仍partial。宿主合同仅作文档校正。
Ruff、doc-sync、strict code-size（hard=0）、diff和clean-package均通过，连同上述65项构成本候选的本地严格gate；线上CI未作为验收来源。clean-package首次只报告4个尚未暂存的新资源，精确登记后复查通过；未删除资源。独立方法审阅摘要`0e106ff06b680d4740952394fea86934604cb12a01f3838a4e235d022a457302`；本地验证记录摘要`be40a6ca1ff7bd6936bccc0c4e0406504947ad691f51b59061d72e2f44dd9ab5`。

## 能力包第九候选普通业务复验（2026-09-27，分项通过／质量未收口）

固定`938d04aaee292aa5004d252af21122e5d97be7e3`的wheel SHA256为`51800d87874e68bb69ba9120b31b601ae99146ff9aeaf7b92820ead85fbac8b3`；1422个包成员与源码归档、wheel及私有安装逐项一致。先备份原私有环境后切换原唯一Gateway；默认环境未动。B0.1.3通过原生TUI停用、更新、启用，三次操作均一次成功，管理期间Gateway身份不变，231个旧业务文件、108份旧任务记录及A/C包保持。
五席官方MiniMax-M2.7新会话各提交一次原冻结需求，输入摘要保持；未代做、补产物、代执行业务检查器或追加修复提示。本轮C02按用户要求未运行，旧结果不覆盖，最终保留集仍0/36。

| 用例 | 业务结论 | 已核实与限制 |
| --- | --- | --- |
| A02 | 失败 | 原A检查器字节一致，实际纠错后60秒结构通过；首镜“柜台空无一物”与随后发现怀表矛盾，部分新增未声明 |
| B01 | 冻结范围通过 | project与输入字节相同，关系报告及两项planned参考准确；实际用内联改写检查，未验原B检查器执行 |
| L01 | 失败 | 四孩子完成，四份阶段JSON可解析；最终7场42镜525秒，却报48镜600秒；原检查器未执行 |
| X01 | 失败 | 两包方法及同代引用齐全，修复后A、B/project、handoff结构回执通过；B脚本为AST相同而字节不同的手抄副本。交接段落映射说明与文件不符，场次漏列已使用工具道具 |
| E01 | 失败 | 原A检查器字节一致，实际纠错后60秒通过；新增所有权未声明，跨镜怀表持有者跳变 |

真实改进证据：节奏孩子首次write成功且附JSON_INVALID（94行33列），自行edit后JSON_VALID；写入结果未被诊断翻转。四孩子canonical规格均有A授权和同代引用，首请求标记submitted；首个assistant之前均有4398字符入口消息，包hash和activation一致，孩子辅助选择调用均0。四孩子私有方法get均0，不把自动入口算成自主方法采用。
X01模型手抄B脚本后实际检测时长与过期摘要并自行修复；静态AST比较相同不等于原字节物化/执行身份验证。该限制与业务结构通过分列，不由观察者重跑原脚本补证。
仓库外观察记录SHA256为`c8a412a2a80c5156133e6b3d00f54b69a0cdb8135674394ec8e45da8ddb509a1`，语义复核记录为`4405fc426b83382bd3ae08908c794c91fb6f75cc666a70f9aad6e31b843bef2d`。完整原生消息、任务/子任务编号、工具与产物摘要留私有证据目录；本地48文件1301项及严格gate为独立集成门禁证据，线上CI未作为验收来源。
建议下一步：先审计包内审阅方法和既有closeout对客观检查结果的采用，再确定通用修复；可并行只读审阅来源语义与报告事实，生产写入保持单一归属，不重跑本轮安全类任务或用最终保留集调试。

## 能力包第九候选组合固定558（2026-09-27，本地严格门通过／真实待验）

组合本线三片固定`898e64634`与主线`558eb65df`，后者直接修复`72cbb0411`历史读取的owner越界。
合入前在固定558源码快照独立跑会话原文读取/搜索两文件，Python3.14.4下36项通过；本轮未重跑原自定义ToolExecutor全链探针，不将它计为新版证据。
仅测试记录和bool归一化列表出现文本冲突，保留双方完整记录及语法诊断、原文预算、原文回查三配置；新增3种带引号的真假组合，核对实际YAML加载后两个开关独立生效。
冻结2978个源码、测试及包资源指纹后，48文件统一消费者focused为1301 passed（165.38秒），前后无源码漂移；Compact使用本线独有短临时路径。Ruff、doc-sync（898e64634基准）、strict-size、diff、clean-package均通过，本地严格gate已通过，strict hard=0。合入前977项和实施方4885项不相加、不替代本次组合结果。
此节记录安装前门禁，当时私有运行仍f6；后续938安装与原生结果见上节，旧记录不回填。线上CI未作为本线验收来源。

## 能力包三片收口（2026-09-27，统一本地严格门通过／主线组合与真实待验）

文件语法反馈六文件215项通过：独立注入`record/discard`普通异常的红例12失败已转绿，新增27个边界用例；独立窄复核39项通过，取消与真实partial保持。
B0.1.3交接检查四文件183项通过：快照改按原CLI Path绑定复用，新增8项反例防止`..`和中间链接误合并；独立窄复核21项及6个合成探针通过。
子入口准备原25文件570项通过（含26项新用例与两项因果变异）；显式选模将Jev建议保留后漏包入口的独立反例已修，红4失败/4通过，修后十文件254项通过；独立窄复核11项与6组合成交错通过。
三片首次冻结2843个源码/测试/包资源指纹，35文件统一focused为977 passed（122.94秒），前后无源码漂移；Ruff和doc-sync通过。strict-size首次失败仅一项：`subagent_first_request_scope`嵌套超限，旧失败日志保留。
原作者将claim提取成早返回helper、调整原因果变异定位，两文件直接78项通过；其余源文件未变。新冻结指纹下35文件再次977 passed（90.91秒），Ruff、doc-sync（66b93647a基准）、strict-size、diff、clean-package均通过，hard=0；完整门禁结果按这次修复后的源码单列，不相加测试数。生产和测试新增/删除合计3052行，按仓库规则未重复全仓pytest；线上CI未作为验收来源。
各片结果不相加。固定main `72cbb0411`合入前，在两套合成owner数据上经原ToolExecutor/ActionPolicy复现绝对及父目录跳转thread_id越界读取；正常同owner读取通过，其他owner普通ID未找到。没有访问真实会话。已交Claude修复，收到新固定SHA并复验前不合入该版本。
组合时Compact使用本轮独立创建的短`--basetemp`复验，避免路径长度掩盖预算边界；配置保留语法诊断和Compact两个新增键。
当前运行仍为旧f6，第八候选六席旧结论不改写，最终12类保留例仍各0/3。

## 能力包组合固定压缩前计量修复（2026-09-26，本地通过）

固定`8419fb762`已合入为`422979e6c`，纯文档修正`e6a46bdcc`随后吸收为`66b93647a`；没有追逐后续其他产品改动。
独立审阅核过完整请求先计量后释放、transcript/live共用值及Gateway状态条消费者；额外自动路径估算属原实施方明确接受的成本。
压缩释放、原生IR、Gateway/子/后台/混合恢复、媒体预留、会话用量及输出预留十文件共140项通过，另以收集结果核对计数。
Ruff、doc-sync（d00fde8基准）、strict-size、diff及clean-package通过；静态尺寸报告记录PreparedCompactRecovery退出原近阈值提示。
当前私有运行仍`f6f93e4f3`，本片没有新真实模型调用；前述六席不能回填为新计量版本验收，未推送或部署日用环境。

## 聊天 `/skills`：TUI 与 IM 里处理技能提案和自动总结的 Skill（2026-09-27，分支 `claude/skill-proposals-tui-im`，基于 main `4aa73d756`）

- **来源**：用户要求“所有都能 TUI 和 IM 来”；技能提案与自动总结 Skill 原来只有命令行入口。
- **新测试** `test_skill_chat_control.py`（24 项）：
  - 异常回执（`claude/skills-receipt-fix`，Codex 静态复核发现）：删除已生效后追加账本抛 OSError，回执不再说“原记录没有改动”，
    而是提示结果没能完整确认、先查当前状态；只读子命令异常只说暂时读不到。变异验证：恢复旧文案、写操作也回只读文案，两种都被抓住。
  - 解析：提案编号前缀（6—24 位十六进制）、版本号（正整数）、Skill 名（生成合同的安全名字）都拒绝式校验，`../` 之类直接无效。
  - 命令目录把 `/skills` 交给 Gateway；TUI 文本还原后重新解析得到同一命令；TUI 本地模式明确拒绝，不落进停止分支。
  - Gateway 分派到技能服务，不走 steer/stop；无效命令回用法。
  - 真实提案服务：列表只给待确认提案带版本的确认/拒绝命令、不含本机路径；版本号对不上时拒绝且提案仍待确认；带当前版本确认后安装并改为已确认；已处理的再操作提示“不是待确认”；前缀找不到、前缀对应多条都给出可操作的提示。
  - 真实自动总结 Skill：列表、详情、登记表外的名字被拒、删除后不再列出。
- **CLI 同步**：`skills learned` 的状态推导与回滚/删除抽到 `skill_learning_report.py`，CLI 输出不变；相关测试（技能提案、自动总结、审核顺序点共 150 项）全部通过。
- **变异验证**：6 种变异全部被抓住——TUI 本地模式不拒绝（落进停止）、Gateway 不分派、文本还原丢参数、提案编号不校验、确认时忽略用户给的版本、前缀多条时取第一条。

## 决策点“触发了但被挡下”也留审计记录（2026-09-27，分支 `claude/decision-skip-records`，基于 main `558eb65df`）

- **来源**：用户真实 TUI 里 planning 等点位没有任何记录，被 my-agent 误判为“未接线”。结构化核对：那一轮由 3186 字粘贴开启，19:11:08 的 task_progress read（3 条待处理）因原话超过 1024 字整点跳过；同一轮 external_material_order 正常记录。隔离 Gateway（127.0.0.1:8432，复制同一份决策设置，600 权限，用后删除）里原话在上限以内时，同样的操作记下 `planning success`；观测启动器与记录在 `~/.my-agent/releases/planning-repro-20260927/`（只有结构化字段）。
- **新测试**：
  - `test_decision_outcome_log.py`：skipped 行只在点位开启、阶段正常、开关打开时写，只带原因码；`material_or_skip` 对隐私跳过返回 None 并记录，其它输入错误照常上抛。
  - `test_decision_planning.py`：原话 1025 字时不调用 Jev、记一行 `request_too_long`；空原话不记。
  - `test_decision_delivery_quality.py`、`test_decision_action_candidate.py`：超长原话与带查询串 URL 的原话分别记 `request_too_long`、`privacy_url`，不含正文；点位关闭或开关关闭时不写。空原话仍在扫描归档之前放弃（原“超长请求扫描前放弃”的用例改为只测空原话）。
  - `test_decision_external_material_order.py`：页面标题、预览或原话含带查询串的 URL 时记 `privacy_url`，日志里没有 URL 内容。
- **变异验证**：6 种变异全部被抓住——planning 不记跳过、delivery_quality 退回扫描前放弃、external_material_order 与 action_candidate 仍抛普通输入错误、跳过记录不查点位是否开启、配置开关不生效。

## 会话原文读取的 thread_id 越界修复（2026-09-27，分支 `claude/history-owner-scope`，基于 main `72cbb0411`）

- **来源**：Codex 在 `72cbb0411` 的隔离副本里用两套合成 owner 数据复现。`session_history_read` 把模型给的 `thread_id` 直接交给 `by_id_report`，存储层按 `messages_dir / f"{thread_id}.jsonl"` 拼路径。绝对路径形态和 `../../../owner-b/conversation/messages/<thread>` 两种写法都能读到只在 owner-b 里的标记，动作策略放行（`thread_id` 当时不是声明的资源参数）；普通 owner-b 线程编号则返回 found=false。没有涉及真实用户数据。
- **改动**：
  - 工具入口 `_owned_thread`：显式 `thread_id` 先过 `validate_path_segment`（拒绝式，只允许字母、数字、`-`、`_`），再必须能在本 owner 线程登记里 `load_report` 到；任一不过都返回 `TOOL_INVALID_ARGUMENTS`，读错误返回 `TOOL_EXECUTION_FAILED`。
  - 存储层 `_thread_stem`：`thread_path`、`message_path`、`observation_path` 对非空编号同样校验，不合法抛 `OpaqueIdError`；空编号保持原映射。
  - `thread_id` 加入工具资源参数（logical），与 `message_id` 一致。
  - 上线前核对：Mac 2148 个、测试机 849 个现存会话文件名全部符合校验格式。
- **测试**：`test_session_history_read.py` 新增越界用例。对绝对路径、相对 `../` 跳转和普通 owner-b 编号三种输入，都要求拒绝且输出里没有 owner-b 标记；同 owner 其它会话照常可读；存储层对 `../`、绝对路径、`a/b`、`..`、`thread.x` 抛错，空编号仍读为空。
- **红绿**：只退回工具层、只退回存储层、两层都退回，新用例都失败；还原后通过。
- **回归**：引用会话存储、线程路径或 session_search 的全部测试文件加架构守卫，共 210 个文件，在 25 字符 basetemp 下 4885 passed、1 skipped、25 xfailed。

## 原话备份改为“候选超目标就收缩”（2026-09-26，分支 `claude/curator-budget`，基于 main `fcd23952f`）

- **来源**：dsh-9b 报告 main CI 每次都红（`527bcdd99` 的运行 36267438519、`fcd23952f` 的运行 36267615914，都是 3.11 失败，其余版本被 fail-fast 取消）。`test_mixed_compact_recovery.py::test_mixed_replacement_fits_when_transcript_only_exceeds_real_input_ceiling` 断言计量两次，实际只有一次。它按 basetemp 路径长度确定性失败：25、45 个字符失败，65 个字符以上通过；CI 是 30 个字符，本机 macOS 默认临时路径 100 多个字符，所以本机严格门能过。
- **原因**：原规则只在收缩能改变判定时才缩小原话备份。这个用例的 1.6 万窗口里，第一次联合候选约 1.43 万 token，离输入上限 1.44 万只差约 100 token，临时路径的长短决定它越没越过上限：越过就收缩再计量，没越过就带着整段备份提交。后者还有产品问题：候选贴着上限提交，下一轮又要压缩。
- **改动**：`_fit_landmarks_to_target` 改为候选超出恢复目标就按超出量缩小备份并重算一次；备份已是最小段、或缩到最小仍不低于上限时不重算。同一场景候选降到约 1.24 万；`test_background_first_request_compacts_after_complete_prepare`（1.95 万窗口）从 1.45 万降到 1.22 万。
- **测试同步**：
  - `test_compact_landmarks.py` 的收缩规则用例按新规则重写：新增“缩到最小也达不到目标仍收缩”；不重算的情况包括缩到最小仍超上限、已是最小段、没有备份段。
  - `test_mixed_compact_recovery.py` 加前提断言：第一次候选远高于目标，所以两次计量是确定的。
  - `test_background_compact_recovery.py` 的首请求用例改为两次候选计量，提交并发送更小的那份。
  - `test_compact_source_lifetime.py` 的两候选用例原先按调用次序给大小，改为按候选身份给（第一候选保留两条近期问答），摘要失败改为在第一候选计量之后触发，计量次数改为 5/3。
- **验证**：
  - 改动涉及的 4 个文件在 basetemp 25、52、156 个字符和默认路径下都通过。
  - 含 compact 字样的全部测试文件加原聚焦集，共 246 个文件，在 25 个字符的 basetemp 下 4762 passed、1 xfailed、4 xpassed。
  - 变异：退回旧规则时，新规则用例和混合恢复用例（短路径）都失败；去掉“缩到最小仍超上限”判断、去掉“已是最小段”判断时，新规则用例都失败。

## Gateway 恢复收割线程改用 registry 里的状态对象（2026-09-26，分支 `claude/watch-recovery-registry-state`，基于 `5bd163135`）

- **来源**：只读调查 `test_watch_audit_guarantee` 偶发时发现，Gateway 恢复循环每 15 秒（`gateway_loops.py:829`）调用一次 `recover_active_audit_harvesters`，它用 `load_state()` 直接读盘，绕开 registry，拿这个对象起收割线程。之后 source worker 的 pull 从 registry 取另一个对象，而 `ensure_harvester` 只按 watch_id 复用活线程，于是同一进程里同一 watch 有两份状态对象、各自落盘。registry 本身不会淘汰状态，这是唯一的生产入口。
- **改动（只改 `continuous_monitor.py`）**：改用 `watch_state.registry.get_or_load(owner_home, watch_id)`，恢复起的线程与工具共用同一个对象。取出后先在锁内 `refresh_scalars_from_disk`，保持原先 `load_state` 的盘上新鲜度，和工具路径 `get_or_load` 之后的做法一致。`ensure_harvester` 的对象告警按集成方决定不做。
- **测试**：
  - 新增 2 项：一是恢复循环交给 `ensure_harvester` 的就是 registry 里缓存的对象，并且游标已按盘上前进；二是真实 `ensure_harvester` 加上只记录状态对象的假收割循环，先由恢复循环起线程，再按 pull 入口的方式经 registry 取对象并 `ensure_harvester`，断言复用同一条线程，且线程持有的正是 pull 所用的对象。
  - 原恢复用例改为替换 registry 供状态，断言不变。
- **变异验证**：改回 `load_state` 时两条新测试都失败；去掉盘上刷新时第一条失败。每次都在 `PYTHONDONTWRITEBYTECODE=1` 下运行，结束后逐字节还原。

## watch 审计重载用例：模拟重启前先停旧收割线程（2026-09-26，分支 `claude/watch-audit-reload-race`，基于 main `e6a46bdcc`）

- **来源**：main `e6a46bdcc` 是纯文档提交，代码与全绿的 `892a7874a` 相同。它的 CI 3.11 上，`test_watch_audit_guarantee.py::test_contract_and_worker_binding_survive_registry_reload` 在第 4420 行拿到空候选。
- **原因（机制）**：测试只替换了 `ws.registry` / `wt.registry`，而收割线程登记在 `hv.harvesters`。模拟重启后，新工具的 pull 会复用重启前那条仍持有旧状态对象的线程。用只读诊断插件看到 `reuse=True`、`same_state=False`，两份状态对象并存、各自落盘。真实重启时旧线程已随进程消失。
- **改动（只改测试）**：第一次 pull 之后先确认旧线程还活着，再 `hv.stop_harvester(watch_id)`，join 到它退出，然后再换 registry。第二次 pull 之后加一条机制断言：当前收割线程必须不是重启前那条。
- **验证**：修复版连跑 3 次都通过；去掉 stop/join 的变异连跑 3 次都在这条机制断言（第 4425 行）处失败，不依赖线程交错。
- **未证实**：CI 上“候选为空”的具体交错，本机正常跑 6 次、后台 QoS 下 10 次、加收割或租约延迟都没有复现。所以只有机制证据，因果链最后一步没有实测证实。

## browser-lite 关闭时先等本 profile 的子进程退出再清 profile（2026-09-26，分支 `claude/browser-lite-helper-exit`，基于 main `b2ee13f1c`）

- **来源**：main `54f24ab94` 的 CI 3.11 上，`test_plugin_exit_closes_browser[eof]` 列出的残留是 `profile/Default` 和 `profile/Default/Network Persistent State`。这个文件由 Chrome 的网络服务写入，网络服务在单独的 utility 进程里运行。`BrowserProcess.stop()` 只等主进程退出就清 profile，网络服务随后落盘，和 `rmtree(ignore_errors=True)` 撞上后留下非空的 `Default/`。同一用例此前在 Linux runner 上还挂过 3 次。
- **改动**（只动 `launcher.py` 的 stop 路径，浏览器仍留在插件进程组）：主进程退出后，最多等 5 秒，让 argv 带本 profile `--user-data-dir` 的进程退出；等待只看 argv，误匹配只会多等。到期仍在的进程，只有 argv 仍匹配、且 `/proc/<pid>/stat` 里的 pgrp（从最后一个 `)` 之后切，因为 comm 可能含空格和括号）等于插件 `os.getpgrp()` 时才发 SIGKILL；这两项都在发信号前现读，防止 pid 被复用。stat 读不到或进程组不同就只等不杀，这是有意的保守取舍：`grep -- --user-data-dir=<profile>`，或者用户 Chrome 的 profile 路径恰好是“本 profile 加空格再加别的”，都可能碰巧匹配 argv。之后最多再等 1 秒，然后才 `clear_profile`。读 `/proc/*/cmdline` 读不到或没权限就跳过；`/proc` 不存在（macOS）时不等待。
- **匹配规则**：参数必须是 argv 里完整的一个元素，或者在以空格拼接的单串标题里作为以空格为界的完整一段出现；`<profile>2`、`<profile>/x` 这类前缀和别的 profile 都不算。之所以要认单串标题：按 Chromium 的 `base::SetProcessTitleFromCommandLine`，Linux 上 Chrome 进程会把整条命令行改写成一个用空格拼接的字符串，只比较完整元素就一个都匹配不上。这一点来自对源码的理解，没有在 Linux 上实测（testbox 没装 Chrome，磁盘占用 97%），真实 Linux 验收只能靠 CI。
- **本机佐证**：macOS 上 headless Chrome 的 8 个进程（主进程、gpu、网络服务、存储服务、通知服务、3 个 renderer）命令行都带 `--user-data-dir=<profile>`，也都和主进程同一进程组；主进程退出后 1 秒内全部退出。
- **新测试** `test_browser_lite_launcher.py` 3 项，用假 `/proc` 的 cmdline 和 stat：
  - 网络服务晚于主进程退出并重建 `Default/` 写文件，另一个 argv 匹配但不在插件进程组的进程更晚退出：stop 会把两者都等完再清理，而且不发信号。
  - 超时后只有两个同进程组的本 profile 进程（原始 argv 形式和单串标题形式各一个）各收到一次 SIGKILL。其中一个的 comm 含空格和括号，还伪造了一段 `S 1 999`。以下进程都不会被杀：不同进程组的、读不到 stat 的、`grep -- <参数>`、profile 路径为“本 profile 加空格再加别的”的用户 Chrome，以及前缀相同的别的 profile、用户自己的 Chrome、读不到的进程。
  - 没有 `/proc` 时立即清理。
- **变异验证**：6 种变异都被测试抓住——去掉等待、匹配改成子串、只比较完整元素、不等超时就直接杀、去掉进程组检查、stat 从第一个 `)` 之后切。每次都在 `PYTHONDONTWRITEBYTECODE=1` 下运行，结束后逐字节还原，删掉 `__pycache__` 再从干净字节码复跑，3 passed。审阅后按集成方要求补了进程组保护（SIGKILL 前核对 pgrp），以及相应的测试和最后两种变异。首版测试里的晚写线程没有重建 `Default/`，导致“去掉等待”时它照样通过；已改为先重建目录，与 CI 上的残留一致。

## 压缩后查回原话与原话备份（2026-09-26，分支 `claude/curator-budget`，基于 main `e6a46bdcc`）

- **新测试**：
  - `test_session_history_read.py`（9 项）：长消息分段读回后逐字拼回；offset 与 max_chars 的边界；助手消息取给用户的回复正文；会话身份只认宿主（没有可信会话时拒绝，显式 thread_id 如实标注来源，current_thread 不接受模型自报的会话）；没有派生索引也能读原文；当前会话浏览只列可见消息、翻页不重不漏、无效游标拒绝；当前会话检索不混入其它会话、能命中长消息中段；会话过滤先于条数上限生效、LIKE 兜底同样生效；翻看模式每条限 2000 字并指向 message_id，且带 current_thread 时显式锚点仍优先；schema 上下限与模块常量一致。
- **提交前复审补的四处边界**：跨代省略总数会缩水（超出 30 个的计数没继承）；机械回退附在备份段后的旧摘要与原文会被当成备份再继承；换行多的长消息刚好超预算时按原文折算、被整条丢掉；机械续接包带入整段旧摘要，备份标题落在包中间，收尾剥离时把原文摘录切掉。另把 `around_id` 调到 `current_thread` 之前（与代码注释一致）。
- **变异验证**：13 种变异全部被抓住——会话过滤去掉、遇长消息立即截头尾、第二遍渲染全部行、总是重算、最小段按 0 估算、current_thread 接受模型会话、翻看不设上限、摘要指令去掉需求清单，以及上面四处边界各退回原实现、current_thread 压过锚点。均在 `PYTHONDONTWRITEBYTECODE=1` 下运行，结束后逐字节还原。
  - `test_compact_landmarks.py`（12 项）：预算随窗口与配置上限变化；每条带编号，回查说明可选；短要求先整条放入、最新长消息保留头尾；省略编号按时间只列最近 30 个；新旧两种格式跨代继承、预算为 0 时编号全部转入省略行；没列出的省略数量逐代累加、只继承备份段本身（段后附带的原文与列表不继承）；换行多、刚好超预算的长消息保留头尾而不是整条丢掉；机械回退带上一代备份时续接包原文不被切掉；第二遍只按下标重读被选中的行；摘要指令单列全部需求与未完成请求；候选超目标就收缩（备份已最小或缩到最小仍超上限时不重算）；真实压缩链路收缩到目标、只多计量一次。
- **测试同步**：原话备份改为带编号的格式（`test_gateway_conversation_compact.py` 四项、`test_goal_lifecycle_recovery.py` 一项改用新接口）；`test_compact_source_lifetime.py` 中超上限的候选先收缩备份再放弃（计量次数 3→4）；`test_mixed_compact_recovery.py` 的前提改看收缩前的首次计量。
- **回归**：压缩、摘要、活动工具、上下文压力、辅助调用、恢复、会话检索、本地存储、配置相关测试文件，加 `test_agent_goals.py`、`test_goal_lifecycle_recovery.py`、`test_architecture_guardrails.py`，以及正文引用会话检索或原话备份的测试，共 139 个文件：1885 passed（复审修补后重跑）。另把两次真机验收提交的摘要原样再走一遍新的继承逻辑（不加新行、同样预算），备份段逐字不变（19,731、3,690 token）。严格门全部通过；code-size 总发现数 2196→2195，`_summarize` 97→84 行、`_build_compact_candidate` 78→67 行，没有新增发现。
- **真机验收**（隔离 8432，本分支代码，DeepSeek 官方 deepseek-v4-flash，1M 窗口）：
  - **接口层**：10 份合成需求，每份约 1.55 万字（服务商实测每份约 1.1 万 token，最后一轮输入 13.7 万）。`/compact` 后会话历史估算 165,443 → 30,401 token。
    - 模型摘要 1,631 token，含全部 10 条规则和 10 个关键细节。
    - 原话备份 19,731 token（上限 2 万）：第 10 份完整，第 9 份保留头尾，第 1–8 份列出编号，附回查说明。
    - 问第 2 份第 13 节第一句（压缩后上下文里没有）：模型先在当前会话检索，再翻看，最后按编号读回全文，逐字答对“郑州的物流跟踪……345 条，24 小时”。
    - 问第 7 份规则与第 10 份关键细节：均答对。
  - **原生 TUI**：配置 `compact_landmark_max_tokens=4000`（同时验证配置生效），粘贴 3 份需求后 `/compact`（56,313 → 13,351），备份段 3,690 token：第 3 份保留头尾，第 1–2 份列出编号。问第 1 份第 13 节第一句：模型按编号分段读取（按 next_offset 续读），答对“武汉的报表合并……734 条，31 小时”；工具卡片与结果在 TUI 正常显示。
  - 证据在 `~/.my-agent/releases/compact-recall-20260926/`（不进仓库）；隔离 home 与模型目录副本已删除，tmux 会话已关闭。

## Compact 强制恢复的“压缩前”计量改到解绑历史之前（2026-09-26，分支 `claude/curator-budget`，基于 main `04c339eb9`）

- **复现**：`test_gateway_compact_recovery` 的真实 Gateway 恢复链（只有 HTTP 是内存替身），历史放大到约 7 万 token。原请求带历史实测 69,961，旧代码写进 checkpoint 的 `projected_tokens_before` 却是 9,919（压后 13,290），与真机“35,915 < 压后 40,303”同一模式。
- **新测试**：
  - `test_compact_recovery_release.py::test_released_history_does_not_shrink_the_recorded_before_size`：Gateway transcript 路径，checkpoint 的“压缩前”等于解绑前量得的完整请求，且大于压后。
  - `test_compact_native_ir_recovery.py` 空 transcript 的活动回合成功用例：checkpoint 的“压缩前”等于同一次解绑前计量，只量一次。
- **变异验证**：把计量挪到解绑之后，Gateway 用例失败（9,920 不大于 13,293）；活动回合路径改回用已解绑输入重量，三个参数组合失败。还原后通过。
- **估算器核对**（原因排查）：`estimate_tokens` 对一份约 9 万字的合成资料估 63,562，DeepSeek 实测每份约 57,650，估算偏保守；之前记的 73,841 取自 `current_context_token_estimate`，它不含原生历史，不是估算器偏低。

## 能力包第八候选私有安装与撤销控制（2026-09-26，分项真实验收）

固定源码`f6f93e4f3`的1375个Python文件、1417个包成员与源码/wheel/实际安装一致；原CLI停旧Gateway、备份后升级，199个旧用户文件、97个Task文件及6份配置保持。
CAP05对A0.2.0/B0.1.2原生六次停用/更新/启用均成功、无未知或待清理状态；C包和Gateway身份保持。最终回执独立核验来自canonical再投影，非历史HTTP响应抓包。
CAP06/F01第三轮只发原冻结需求一次：实际读取B方法后单次stop停稳，再由管理席停用；同Goal/Task/Thread恢复后原pin/选择标记不变。
原source_ref物化被拒：write_file结构码TOOL_UNAVAILABLE、effect_outcome=not_started，原输出细因CAPABILITY_PACKAGE_NOT_AVAILABLE；恢复后没有skill_search get，读取拒绝保持未触发。
业务自然终态、资源停稳后B以同内容新activation启用，旧Task引用不被重写。17次实际模型调用、0重试，已报告773640 token；一次取消无供应商usage。独立审阅核官方配置和实际后端，但未取得逐次HTTP端点抓包。
观察脚本在发送resume前曾误用channel_bindings容器形状，修正前未发按键、未改canonical；错误单独保留。模型关于重新读取和错误码的陈述不代替工具事实。
新版A02/B01/C02/L01/X01/E01六个新会话已各提交一次原开发需求并自然终态；B01/C02冻结范围通过，A02/X01/E01/L01业务质量失败。详见[六席终审](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第八候选原生控制及六席质量终审2026-09-26)。12个最终保留例仍各0/3。

## 能力包固定分段摘要修复组合（2026-09-26，本地复验通过）

在样包提交`aaf055ee2`的69文件检查之上，合入固定main `04c339eb9`及Jev说明修正`6b331ee28`。
生产差异仅原Compact分段请求；该文件及五个直接测试与固定main逐字节一致，其余已验源码/测试摘要保持。
针对分段预算、完整来源、主/子/后台/live和包续读消费者补跑12文件289 passed；不与前轮1661重复相加，也不冒充同一次全量运行。
原生TUI已切上述固定runtime；集成方自身的大窗口换小窗口真实证据单列。分段本地通过不代替本线业务质量或最终保留集。

## 能力包第八候选组合（2026-09-26，本地严格检查通过）

首轮合并 `a3f058d35` 的 53 文件为 1340 passed、9 skipped；Ruff/strict-size/diff/clean-package通过，但doc-sync失败。
独立审阅发现当前会话Jev统计混入其他会话，以及Compact工具违规后的回退吞掉来源变化错误，均交回原实施方修复。
固定main `54f24ab94`（含修复 `a2604cb7f`）已合入本线；独立固定源码反例关闭两缺陷，五个直接相关测试文件与早期基线doc-sync通过。
原53文件结果不冒充新组合验证；新69文件清单覆盖该main直接CI修订、能力包主链、A来源/B模板及原时长/权限/架构守卫。
首轮1660 passed、9 skipped，仅一条旧B版本断言仍期望0.1.1而失败；同步为本次明确升级的0.1.2后重新冻结全部源码/测试，完整69文件1661 passed、9 skipped、0 failed。未降低业务断言，原失败记录保留。
Ruff、doc-sync（基准d00fde8）、strict-size、diff和clean-package均通过；未推送，本轮线上CI不作为验收来源。这是固定54f24ab94加样包修订的组件结论，后续生产改动另验。

A0.2.0升级其私有资料/检查报告为v2，每镜显式声明来源、新增和未知，结构检查仍不判语义真。
旧26条新合同用例先红，新实现连原时长/样包共106项通过；末审补三者共存及失败报告版本，现31条来源用例均在新组合内通过，不重复累计。
B0.1.2补完整项目模板、阶段/跨包交接资源及原派工授权示例；本片21项通过，原checker与公开例子字节不变。
组件只消费公开合成数据或隔离的假工具，不运行真实任务产物。当前私有TUI仍是第七候选，旧失败/未触发和保留集各0/3不改写。
F01观察器v2已通过23项私有离线校准；它区分早期展示读取与终态原生确认，尚未由新真实控制试验验证。

## 零能力包核心对照（2026-09-26，原生TUI通过）

ZERO01在CAP05原生卸载全部三包，canonical安装表确认为空后新开CAP03，只提交一次原N04的CSV求和需求。
模型实际读取原输入，准确回答300/3；官方M2.7主业务2次HTTP，无选包辅助和包采用。业务终态后精确重装三包并启用。
12次管理写均成功并完整收尾，195旧文件/42旧pins/6配置保持，Gateway未重启；新activation不使旧授权复活。
只覆盖零包CSV与原版本恢复，不等同全部核心或并发卸载通过；瞬时cleanup_consumption未持久，原成功回执与ZIP回收事实单列。
细项见[ZERO01矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#zero01全卸载后的核心csv与精确重装)。本轮无产品改动，最终保留集仍未开始。

## 能力包组合与控制补验（2026-09-26，质量和未触发分别记录）

固定第七候选宿主与A0.1.2/B0.1.1/C0.1.0，原生CAP01/X01、CAP02/E01、CAP06/F01第二轮各提交一次冻结需求。
官方M2.7实际45次HTTP：X01为19、E01为12、F01为14；与前批83次独立，不重复累计usage快照。
X01的两包组合与同名方法隔离、E01的明确选A及原脚本复制执行通过；结构检查纠错后通过，但来源语义、道具连续性与交接仍失败。
F01原B脚本实际exit0、报告准确，完整业务部分覆盖；观察器用了错误activation标识并解析截短display，未发停止或停用。
测试台另有Goal先于Task文件出现的观察竞态；接续只观察，未重发业务需求。保留该trial为控制未触发，不归为模型/产品故障。
F01原安装表、配置、输入及192个旧文件保持；自然终态不算停止验证。细项见[组合与控制矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#新包组合明确选择与撤销控制第二轮)。
后续控制观察须先用原冻结记录做零网络匹配校准，读取完整结构化归档；不能拿展示截短片段或另一个摘要字段充当权威身份。
本轮无产品代码变化；最终保留集未开始，全卸载核心对照另见ZERO01。

## 能力样包时长检查（2026-09-26，A0.1.2/B0.1.1组件及原生热更新通过）

A补逐场镜头与场次声明、总镜头与来源明确目标对账；B补跨场分集汇总与目标对账。两包用稳定求和与相对数值容差，保留原来源/外键拒绝。
四文件统一focused为142 passed：样包47、A时长33、B时长34、包协议28。反例包括各场/集错配但总数相同、缺目标/非法目标、极小合法数、上万小项求和、溢出和旧引用错误。
独立审阅发现B绝对误差额度、普通累加及旧文档容差问题，均先复现后修复；原真实任务产物未用于执行开发测试。
固定包源码为`cd186af573601b43206682725a40e447fe8c1533`；两个ZIP各重复构建一次，字节相同。
原生CAP05串行执行A/B停用、更新、启用，共六次写操作全部成功且收尾完整；A修订6→8→9→10，B修订8→10→11→12。
两个更新长命令最初停留输入框，核实未创建管理操作后仅补Enter；没有重发命令文本，不把首次空采样算更新成功。
同一Gateway身份保持，启用均为`runtime=none`；C安装行、旧包及七个旧业务工作区21个文件保持。没有新增插件进程，锁及未完成claim为0。
新版六席业务复验沿原冻结开发输入、全新会话完成，共83次官方M2.7 HTTP（主首轮selector6、其余主/子77，无重试）；原输入、安装代次和Gateway保持。
A02原脚本先报错后由模型修正并重跑exit0；B01只检查输入，输出虽语义相同，报告所写输出检查命令并未执行。
L01数值自修到600秒，但没有运行原脚本，来源摘要/覆盖与B字段仍失败；C02核心归并正确但摘要计算表述缺工具证据，N05漏一条待办，N03准确。
四个长任务孩子没有包授权：父省略字段，原权限链没有丢弃已授权refs。两次输入hash失败均是前台显式选择错误working_dir，不是handoff默认cwd漂移。
方法采用、脚本执行、权限和业务质量分别计量，详见[新包六席](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#热更新后的六席开发复验)；不新增宿主执行器或验证库，最终保留集仍未开始。

## 一次能力包选择（2026-09-26，第七候选六席采用已验，质量未全过）

分片已覆盖原TaskLink严格标记/CAS/旧缺键与损坏隔离、一次structured调用及三协议输出、真实共享reader原回执和取消、配置关闭与scope权限。
上述是组件证据，合并统计以本候选冻结后的统一focused清单为准，不相加重叠测试次数。相关测试由CODEBASE_TREE中的capability selection/read入口导航。
组合重点：普通任务及Goal初始化、只有主业务首轮准备、明确空选/失败继续、停止/换attempt迟到结果零pin与零注入、预算不足零pin、selector信封不进业务history，以及选模捕获与实际发送包含相同入口。
冻结源码后的31文件统一focused为901 passed，无skip/xfail；Ruff、doc sync、strict code-size、diff及clean-package通过。
clean-package首次因14个新增文件未登记Git而拒绝，登记后通过；不改检测规则。首次红结果保留，线上CI未作为验收来源。
真实组件证明会话successor链接与RuntimeDB执行task分别核验；取消不降级、预算不足不pin，原首请求捕获和发送包含同一入口且不包含selector信封。
固定`d843ebb17`已精确打包安装，1374个Python文件同源。六个官方M2.7原生TUI：四领域任务真实选包/入口/方法，两个普通任务明确空选；每前台新增aux1，四孩子无重复选择。
A02原脚本原字节物化与执行通过，但有未标创作事实；B01报告一处不实且未用原checker；L01镜头合计573秒却报600并有无效来源引用。C02核心归并、N05/N03普通业务通过。
方法使用、结构脚本与业务质量分列[真实矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第七候选真实开发集)；开发集未全过，保留集尚未开始。
纯问答晋升另补on/off首请求回归：开启并有授权候选时空选保留普通任务、Goal不变；关闭不晋升且无aux。该测试不模拟终态，真实N03的completed另列。
候选6 CAP06 的自然摘要独立复核通过；新checkpoint属于thread，已完成旧task没有后续业务请求，未验证该task消费新摘要或重用方法。

## Compact 分段请求改为片段在前、累计摘要与规则在后（2026-09-26，分支 `claude/curator-budget`，基于 main `54f24ab94`）

- **新测试**：`test_compact_request_budget.py::test_segment_request_puts_source_first_and_carry_with_rules_last`，锁定分段请求是单条文本、`messages=None`，顺序为 源片段 → 结束标记 → 此前摘要 → 摘要规则 → 合并要求；首段写明“无此前摘要”。
- **测试同步**：分段请求不再有 `messages`。新增共用解析 `segment_part`（放在 `test_compact_request_budget.py`）：按头部区间长度切出片段并核对紧随的结束标记。`test_compact_text_source.py`、`test_compact_source_lifetime.py`、`test_native_tool_ir_compact_and_orphan_sweep.py`、`test_subagent_runtime_compact.py` 改用它；原“prompt 等于压缩指令”的断言改为“摘要规则一节等于压缩指令，执行器稳定前缀不进入分段请求”。
- **回归**：压缩、摘要、活动工具、上下文压力、辅助调用相关的 70 个测试文件，加 `test_agent_goals.py` 与 `test_architecture_guardrails.py`：923 passed。严格门全部通过；改动模块的 code-size 发现与 main 逐项相同。
- **隔离真机对比**（8432，隔离 home，同一组合成资料：5 份各约 9 万字、各带一条处理规则；DeepSeek 官方 1M 累积到约 31.3 万 token 后 `/model` 切 MiniMax-M2.7 262K，再问 5 条规则）：
  - 部署版 `step12w`：压缩自动完成，两段辅助请求共 300,046 输入、344 输出；模型摘要 267 字，一条规则都没有；回答说第 1 份“未显示具体规则”，其余 4 条来自原文地标；另白写 171,960 token 缓存。
  - 本分支代码：两段共 300,475 输入、1,052 输出；模型摘要 602 字，5 条规则全在；回答逐条正确；无缓存写入。压缩后主请求分别为 28,607 与 28,812 token。
  - 证据在 `~/.my-agent/releases/compact-window-switch-20260926/`（不进仓库）；隔离 home 与模型目录副本验收后删除。

## Jev 审计按会话过滤、Compact 兜底收窄与 CI 修复合入（2026-09-26，main `a2604cb7f`，基于 `a53ab52d7`）

- **合入 CI 修复**：`claude/ci-fix` `251247edf`，由 my-agent-dsh-9b 完成，排查记录见下一节，集成方审阅后快进合入。
  - 集成方逐项核对了产品改动：按目录描述符逐层删除，子目录不跟随链接地打开并复核身份；证据库每次操作后显式关闭连接。
  - 测试改动没有放松断言，时限放宽后仍能抓住不守期限的实现。
- **新测试**：
  - `test_decision_outcome_log.py`：汇总可限定可信会话，后台点位没有会话编号，随之排除；审计 `scope=current_thread` 只剩当前会话的点位，`owner` 范围包含后台 curator。
  - `test_compact_request_budget.py`：工具调用改走分段链后，若分段期间来源变化（`COMPACT_SOURCE_CHANGED`），错误照常上抛，不交回旧回复。
- **补注释**：Codex 用旧 main 作 doc sync 基准时发现 `2b25e38b3` 一片的缺口：`curator.py` 的退避与失败分类补了真实职责注释，gateway 进度补了唤醒发现同源退避说明。`--base 2b25e38b3^` 与默认基准的 doc sync 都通过。
- **变异验证**：5 种变异各自使测试失败：审计不按范围传会话、汇总忽略会话过滤、兜底吞所有错误、白名单漏严格来源、白名单漏非文本。
- **回归**：170 个测试文件、3313 passed。范围覆盖 Jev、Compact、CI 修复改到的测试，以及引用 nofollow_tree、验证证据库的测试，含 `test_architecture_guardrails.py`。严格门全部通过，code-size 总数与 main 相同。

## GitHub CI 持续失败排查（2026-09-26，分支 `claude/ci-fix`，排查时基于 main `a3f5c17ec`，已变基到 `a53ab52d7`）

- **范围**：main 上最近 40 次失败运行（39 次 Test、1 次 Full Tests，2026-09-25 15:47Z 到 09-26 14:27Z），逐个拉失败 job 的日志归类。同期 Lint 和 Cross-platform guard 全部通过。
- **每次必挂的失败**：
  - 3.10：`common/nofollow_tree.py` 调用了 `shutil.rmtree(..., dir_fd=)`，而这个参数 3.11 才有，项目声明的是 `>=3.10`。插件停用、移除时删环境目录会抛 TypeError，宿主命令落成 `outcome_unknown`，每个 3.10 job 因此挂 29 项（`test_plugin_*`、`test_host_command_stream`、`test_workspace_peek_package` 等）。40 个失败 job 里 20 个是 3.10；3.11/3.12 先挂时 3.10 会被 fail-fast 取消，所以日志里不一定看得到。修法：改为按目录描述符逐层删除——子目录经 no-follow 打开并复核身份，再用 `os.scandir(fd)` 和 `unlink/rmdir(dir_fd=)`，不再依赖 `rmtree` 的 `dir_fd`，各 Python 版本走同一实现。`test_nofollow_tree` 补了嵌套的真实目录，覆盖递归路径。
  - 3.11/3.12：`test_gateway_compact_recovery*.py` 共 12 项报 `_payload() takes 3 positional arguments but 6 were given`。原因是 `7a15c9c91`（/effort）改了签名，测试没跟着改。已由集成窗口在 main `f230ab077` 修好，本分支不改这两个文件。
- **偶发失败**：除探针超时用例外，根因都在本地用临时 pytest 插件注入延迟或 GC 复现过，旧代码稳定失败、失败行与 CI 一致，修复后通过。
  - `test_adapter_manager` 投递线程用例，5 次。每次轮询都要持久领取和释放（含 fsync），65 次轮询在慢 runner 上超过 2 秒。更要紧的是断言失败后没停 manager：泄漏的投递线程继续轮询 127.0.0.1:8420 并写盘，连带挂了 `test_gateway_model_observation`（3 次）、`test_scheduler_heartbeat_resilience`（2 次），以及 `test_conversation_message_selection`、`test_compact_source_lifetime`、`test_gateway_model_adoption`、`test_browser_lite_package` 候选用例各 1 次，这些失败日志里都有泄漏线程的 `req_late` 告警。修法：等待上限改为 30 秒（送达即返回），并用 `try/finally` 在补丁仍生效时停线程。复现方式：每次写投递记录多 15 ms。
  - `test_ingestion_harvester::test_restart_resumes_harvest_from_disk_cursor`，3 次。测试停掉旧收割线程后没等它退出就模拟重启，新工具读到旧线程还没清掉的租约，按设计 fail-closed 成 remote，于是不起本地收割者。修法：先 join 旧线程再模拟重启。复现方式：旧线程清租约前睡 1 秒。
  - `test_decision_delivery_quality_integration`，2 次，出现在 3.11 和 3.12。这是产品问题：验证证据库的连接不关闭，3.11 起要等循环 GC 才关，WAL checkpoint 时机不定，决策时刻的文件快照和结束时对不上。修法：每次操作显式关闭连接，详见 verification 模块进度；新增回归测试在关闭自动 GC 的情况下检查 WAL 侧文件。复现方式：决策时刻快照之后手动 GC 一次。
  - `test_tui_control_delivery` 的 compact/stop 用例，1 次。`_finish` 先回调界面、再删 outbox 行，测试一见到回调就读 outbox。修法：等回调完成且 outbox 清空后再断言，上限 5 秒。复现方式：回调后晚 0.2 秒删行。
  - `test_decision_model_operations` 的探针超时用例，1 次。墙钟 0.895 秒超过了 0.8 秒上限，这个上限还包含设置读取和账本结算。本机加慢 fsync、降为后台 QoS 各跑多次都没复现，不知道 runner 上具体慢在哪一步。修法：上限改为 2 秒，仍低于测试服务端 3 秒的挂起时长；变异验证中让产品忽略期限，这条断言照样失败。
- **未解决**：`test_browser_lite_package::test_plugin_exit_closes_browser`，3 次，都在 Linux runner 上，插件退出后 profile 目录有残留。本机 macOS 跑 16 次没有复现：退出后没有带这个 profile 路径的 Chrome 进程，profile 3 秒后仍为空。疑似 Linux 上 Chrome 的子进程（例如 crashpad handler）在主进程退出后还在写 profile。本分支只让断言失败时列出残留条目，没有改产品；要在 Linux 上拿到残留文件名再定修法。
- **基础设施**：2026-09-25 13:26Z 到 14:31Z 之间的运行在 3 到 8 秒内失败，注解是 "recent account payments have failed or your spending limit needs to be increased"，属于账单/额度问题。仓库公开后恢复，最近 24 小时没有再出现这类失败。
- **验证**（变基后的最终树）：与改动直接相关的 40 个测试文件，含 `test_architecture_guardrails.py`、上面被连带的文件，以及 main 已修好的两个 compact 文件。用 3.10.20、3.11.15、3.12.13 各跑一遍，每个版本都是 712 passed；3.10 和 3.11 用 scratchpad 里的隔离 venv，依赖经本机代理安装。严格门的 6 条命令退出码全部为 0：pytest（3.12）、Ruff、doc sync、strict code-size（blocked=False，与 origin/main 按 identity 和 severity 逐项比对，3485 条一致、无新增）、`git diff --check`、clean-package。没有推送，线上 CI 未作为验收来源。
- **合入后线上 CI 跟进**（分支 `claude/ci-fix-2`，基于 main `8419fb762`）：
  - `a2604cb7f` 是第一个包含本修复的推送，它的 Test 3.10、3.11、3.12 全部通过。
  - `54f24ab94` 的 3.11 只挂了 browser-lite 退出用例（3.10/3.12 被 fail-fast 取消）。断言列出的残留是 `profile/Default` 和 `profile/Default/Network Persistent State`，这个文件由 Chrome 的网络服务写入，网络服务在单独的 utility 进程里运行。`BrowserProcess.stop()` 只等主进程退出就清 profile，网络服务随后才落盘，和 `rmtree(ignore_errors=True)` 撞上后留下非空的 `Default/`。本机 macOS 上 Chrome 的 8 个进程（含网络服务）命令行都带 `--user-data-dir=<profile>`，也和主进程同一进程组，主进程退出后 1 秒内全部退出，所以本机复现不了。产品暂未改，修法待定。
  - 修复前的 `a53ab52d7` 上，3.11 还出现过一次 `test_tui_prompt_toolkit_pipe` 鼠标协议用例偶发：固定 `sleep(0.08)` 之后读到的终端输出是空串。已改为有界轮询，直到预期序列出现（上限 5 秒）。临时插件让 pipe 输入晚 0.2 秒送达时，旧用例在 CI 同一行（267）失败，新用例通过。

## Compact 摘要请求禁止工具与违规兜底（2026-09-26，已合入 main `f230ab077` 并双机部署 `step12v-dff317e5`，基于 `756d4b9bb`）

- **来源**：
  - main 的摘要请求带工具、选择为 `auto`，模型调工具就退成机械摘要。
  - Codex 私有验收里，MiniMax-M2.7 带工具加 `none` 仍回 tool_use。
  - 集成方吸收 Codex `d4dd6c094` 的产品和测试部分，丢掉其中能力包相关文档，并在其上补违规兜底和日志关联键。
- **吸收的测试**（10 个文件，来自 `d4dd6c094`）：
  - 可容纳请求只多一个 `none` 选择。
  - Anthropic 协议实际请求体带 `tool_choice: {"type":"none"}`，并保留工具 schema 与缓存标记。
  - 三协议 `tools_for_choice` 在 `none` 时保留目录，响应里的调用仍被协议门拒绝。
  - 摘要响应形状诊断不含正文、思考、签名。
  - 另有若干夹具按新语义校准；`test_background_compact_recovery.py` 窗口保留集成方的 19500，并采纳 Codex 新增的"候选装得下"断言。
- **新测试**（`test_compact_request_budget.py`，共 3 项）：
  - 单次摘要回工具调用后改走分段链，由模型重写；分段请求空工具、`none`；诊断带请求/会话/用途编号和墙钟时间，不含原文。
  - 带图来源不能分段：交回原回复。
  - 严格来源下分段只能降级：交回原回复。
- **按新契约改写**：
  - `test_gateway_conversation_compact.py`：假后端始终调工具时，摘要为分段链的带标注摘录，工具始终未执行，首请求带工具、之后空工具。
  - 形状日志测试把关联键分开断言。
  - `test_compact_message_source.py`：实际请求只多 `none`。
- **修复 main 既有失败**：`test_gateway_compact_recovery.py` 与 `_continuation.py` 的 12 个用例自 `7a15c9c91`（/effort 把 `_payload` 改为收 `_PayloadSurface`）起失败。在干净的 main `756d4b9bb` 上复现为 12 failed，测试已改用新签名并传入候选的真实 params，23 passed。
- **变异验证**：6 种变异各自使测试失败：不设 `none`、`none` 清空工具、不走分段兜底、兜底失败不交回原回复、日志不带关联键、compact 不传请求。还原后逐字节一致。
- **真实验收**（隔离 Gateway 8432，运行时 `runtime-step12v-dff317e5`，MiniMax-M2.7 官方接口，Jev 观察模式，经 Gateway `/ask` 与 `/control`，每轮一条普通需求）：
  - 四轮对话：建文件并数行数；改第二条并读回；`/compact`；压缩后凭记忆回答。第四轮让模型用 `audit_records` 查决策点位。
  - 决策结果日志：`model_selection` 4 次成功、`pre_recall` 4 次成功、后台 `curator` 1 次成功、1 次超时（5002 ms，恰为后台 5 秒预算）。超时只冷却 curator，其它点位照常。
  - 模型据 `audit_records` 按点位如实报告，未触发的点位说成"窗口内未触发"，不再断言"没接线"。
  - 压缩：带工具加 `none` 的单次摘要一次成功，只有 1 次辅助请求，摘要为模型正文（1034 字，非机械回退）；历史 19,075 → 9,947 tokens，压缩后准确答出文件名与改后的第二条。
  - 本样本未出现违规 tool_use，分段兜底由单元测试覆盖。
  - 证据留在 `~/.my-agent/releases/compact-jev-acceptance-20260926/`（不进仓库）；隔离 home 与模型目录副本已删除。
- **原生 TUI 复验**（用户要求真实验收经原生 TUI）：新的隔离 home，tmux 里运行 `chat --gateway`，像用户一样逐条输入同样四轮，压缩用 TUI 里的 `/compact`。
  - 点位冷却：`pre_recall` 09:27:20 超时（5004 ms）。09:27:43 它以 `point_backoff` 跳过、不发请求，同一时刻 `model_selection` 照常成功（4842 ms）。30 秒后 `pre_recall` 恢复，两次成功。
  - 模型据 `audit_records` 按点位报告了成功、超时、`point_backoff` 与未触发。
  - 压缩：只有 1 次辅助请求，模型正文 1144 字，非机械回退；历史 17,920 → 10,076 tokens；压缩后答对文件名与第二条。
  - 收尾：TUI `/exit`、Gateway 停止、tmux 会话关闭、8432 释放，隔离 home 已删除。
- **回归**：涉及 Compact、摘要预算、辅助调用、工具选择的 66 个测试文件，含推理强度、结构化输出和 `test_architecture_guardrails.py` 组合回归：1601 passed。严格门全部通过；改动产品文件的 code-size 发现与 main 逐项相同。

## Jev 决策结果日志与点位冷却（2026-09-26，已合入 main `756d4b9bb`，基于 `a3f5c17ec`；真实验收见上节 Compact 同一次隔离运行）

- **来源**：用户 TUI 里的模型据 `audit_records` 断言 Jev"只接了选模型"。查实原因有三：Jev 经代理访问慢，单次约 2.5–5 秒；任一点位超时就冷却整条连接，选模型每轮最先超时，把其它点位全挡住；审计只看得到选模型和能力展示的观察。
- **新测试** `test_decision_outcome_log.py`，共 4 项：
  - 日志行只含结构化字段。
  - 只写 owner 规范路径，有条数上限；没有该路径的宿主不写文件。
  - 汇总按时间窗口按点位计数，坏行单独计数。
  - 经本地假决策服务走真实 `decide()`：超时、成功、点位冷却各落一行；请求材料不进日志；审计按点位给出次数。
- **改写测试** `test_decision_curator_plugin_concurrency.py`：原"后台超时冷却共享连接"的契约改为三项：
  - 后台超时只冷却本点位，前台照常请求并成功；同一点位换新阶段后 `point_backoff`，不发请求。
  - 冷却到期后本点位成功一次就清掉失败阶梯，下次超时从 30 秒重新计起。
  - 服务端 503 仍冷却整条连接，前台 `connection_backoff`、不发请求。
- **调整** `test_decision_fault_matrix.py`：慢响应（超时）那一格的原因改为 `point_backoff`，其余故障仍为 `connection_backoff`。
- **变异验证**：8 种变异各自使测试失败：
  - 日志与审计：不记日志、审计不带点位、汇总不按窗口。
  - 冷却分级：超时仍冷却整条连接、所有失败都只冷却点位、不查点位冷却、成功不清点位冷却、点位冷却原因写成连接。

  "成功不清点位冷却"最初没被杀死，补了阶梯重置测试才杀死。还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：
  - 引用决策服务、冷却策略、审计工具或 owner 路径布局的 100 个测试文件（含 `test_architecture_guardrails.py`）：1686 passed。
  - 构造 `SimpleAgent` 的 117 个测试文件：2278 passed、8 skipped、29 xfailed。
  - 严格门全部通过；code-size 总数与 main 相同。原 `decide` 的长度发现随函数体移到 `_decide_outcome`（50→53 行，仍为 high-risk）；参数打包后没有新增参数发现。


## macOS 沙箱读边界第二步：拒读用户家目录（开关默认关闭，2026-09-26，分支 `claude/curator-budget`，基于 main `e24aae8ab`）

- **改动**：
  - 拒读根从单个值改为一组：`host_private_roots` → `private_roots` → `AttemptSandboxSpec.private_read_roots`。
  - 开关 `shell_sandbox_hide_user_home` 打开时，非本机管理员的 owner 在 macOS 上多拒读用户家目录。
  - HOME 落在拒读根里时，前台、后台、终端三条路径的子进程 HOME 改指到 owner home。
  - 这个开关属于配置边界项，模型不能改。
- **新测试** `test_shell_hide_user_home.py`，共 8 项：
  - 开关在 YAML、dataclass 和规范化中默认都是 false，并已列入 `BOUNDARY_KEYS`。
  - 裁决：开关打开、非管理员、macOS 三者同时满足才加家目录；开关关闭、本机管理员、Linux 时都只拒读 my-agent 根。
  - 规则：全部拒读根在前，放行在后，上层目录元数据在最后；家目录这一级只放行元数据，`.ssh` 和其它项目不放行。
  - HOME 改指向：家目录被拒读时指到 owner home，包括 HOME 在拒读根之下的情形；只拒读 my-agent 根，或本机管理员时，HOME 不变。
  - 回执：家目录被拒读时说明读不到、HOME 已改指向，否则不这么说。
  - 装配：飞书 owner 的 `SimpleAgent` 在开关打开时，把家目录接进 Shell 工具。
  - 后台命令和终端会话的子进程同样拿到改指向后的 HOME。
  - 真实 Seatbelt（仅 macOS）：注册表装配的 `run_command` 读不到 `~/.ssh` 和其它项目，本 owner 文件可读，`$HOME` 是 owner home，`git init`、`git status` 成功；只拒读 my-agent 根时，其它项目仍可读。
- **既有测试**：5 个文件里的替身和参数改用新名字，断言不变。
- **变异验证**：14 种变异各自使测试失败，还原后逐字节一致：
  - 裁决：不看开关、不看管理员、不看平台。
  - HOME：不改 HOME、只认 HOME 与拒读根相等。
  - 装配：core 总说平台已隐藏、bootstrap 不传拒读根。
  - 环境构造：前台、后台、终端各自不把拒读根交给环境构造。
  - 回执、配置：回执不提家目录、开关不在边界项、不做布尔规范化、YAML 默认打开。
- **本机探针**（真实 `sandbox-exec`，拒读家目录，HOME 指向家目录外的临时 owner 目录）：
  - git init/status、python3、node、npm、uv 都正常；家目录列不出，`.zshrc` 读不到。
  - curl 与 pip 下载 3 轮都成功。第一次 curl 超时是代理偶发，与拒读无关：同一轮不拒读时也慢，拒读时 CONNECT 隧道与 TLS 握手完整。
  - 正是这次探针在 owner 目录位于家目录之内时暴露了 git 的 EPERM，才有了上面的热修复。
- **回归**：
  - 沙箱、Shell、终端会话、配置规范化、owner 权限与工具注册相关的 92 个测试文件（含 `test_architecture_guardrails.py`）：1840 passed、10 skipped、3 xfailed。
  - 另外 117 个构造 `SimpleAgent` 的测试文件：2278 passed、8 skipped、29 xfailed。
  - 严格门全部通过；改动文件的 code-size 发现与 main 相比没有新增或升级。

## macOS 沙箱拒读根的上层目录放行元数据（热修复，2026-09-26，分支 `claude/curator-budget`，基于 main `227fcd5b1`）

- **现象**：step12q 部署后，owner 工作区在拒读根 `~/.my-agent` 之下时，沙箱里的 `git init` 报 `fatal: Invalid path '<my-agent 根>': Operation not permitted`。`git add`、`python3 -m venv`、node 的 `fs.realpathSync` 同样失败。
  - 受影响的是 `~/.my-agent/owners/...` 下的工作区，TUI 默认工作区就在这里。
  - `--workspace` 指向 `~/.my-agent` 以外的项目目录不受影响。
- **原因**：拒读规则用 subpath，连根目录本身一起拒绝；放行本 owner 目录后，根与 owner 目录之间的各级目录仍拿不到元数据。这些工具规范化路径时要逐级 lstat，遇到 EPERM 就退出。第一步的真实沙箱用例只测了读文件和写文件，没覆盖到这一点。
- **修复**：对拒读根内、放行目录上层的各级目录，按 literal 放行 `file-read-metadata`。这些目录本身能 stat，但仍列不出内容；同级目录、其它 owner 和 config 仍 stat 不到。
- **测试**（`test_attempt_sandbox.py`）：
  - 规则单测：元数据放行规则排在最后，覆盖根、`owners`、`owners/local`、`shared` 这几级目录；不含 `providers` 和 `config`，也不用 subpath。
  - 真实 `sandbox-exec`（仅 macOS）：根和 `owners` 能 stat，但根列不出内容；其它 owner 的上层目录和 config 仍 stat 不到；owner 工作区里 `git init`、`git add` 成功。
- **变异验证**：3 种变异各自使测试失败：不放行上层元数据、literal 换成 subpath、漏掉根目录本身。还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：引用 attempt 沙箱、Shell 工具或终端会话的 25 个测试文件（含 `test_architecture_guardrails.py`）722 passed、9 skipped；跳过的是 Linux bwrap 用例。

## 验证分类：换行与不执行检查的参数（2026-09-26，已合入 main `76cb23c6c` 并双机部署 `step12r-d79ae184`，基于 `7179f12e4`）

- **来源**：Codex 在 main `7179f12e4` 上给出三个反例：`pytest\necho done`、`pytest --help`、`pytest --collect-only` 返回 0 时都被记为 passed/full。复核时又找到同类写法：`cd tests` 换行后接 `&& pytest`，`make test -i`（忽略失败），`go test -n` 和 `go build -n`（只打印命令）。
- **新测试** `test_verification_project_facts.py`：
  - 5 种换行写法 × 返回码 0/2，都不记证据。其中包括 cd 前缀部分出现换行，以及 `&&` 后换行的续行写法。
  - pytest 的 12 种参数都不算证据，它们只打印帮助或版本、只收集或只装夹具；`pytest -v` 仍记 passed。
  - cargo、go、make 的 14 种参数都不算证据，它们不执行检查或吞掉失败。`cargo test`、`go test ./...`、`go build ./...`、`make test`、`make test -k`、`cargo test -- --nocapture` 仍记 passed。
- **变异验证**：9 种变异各自使测试失败：
  - 去掉换行检查；把换行检查放回拆段处（由 `cd tests` 换行接 `&& pytest` 的用例杀死）。
  - 不过滤参数；去掉通用帮助参数；make 表去掉 `-i`；go build 不设表。
  - `--flag=值` 不拆等号；不按首词回退查表。
  - 各命令共用一张表：`pytest -q` 会被 make 的 `-q` 误伤。
  - 还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **部署核对**：两台机器的已装运行时对三个反例都不再记证据，普通 `pytest` 仍记 passed。
- **回归**：14 个测试文件 850 passed、24 xfailed，它们引用分类器、验证账或运行事实，并含 `test_architecture_guardrails.py`。严格门的 Ruff、doc sync、strict code-size、diff 和 clean-package 全部通过；改动文件的 code-size 发现与 main 相同。

## 后台首请求压缩用例窗口校准（2026-09-26）

- **现象**：`test_background_compact_recovery.py::test_background_first_request_compacts_after_complete_prepare` 两个协议在 main 上失败，报“压缩候选装不进输入触发线和输出预留”。之前的 focused gate 没有覆盖它。
- **定位**：git bisect 从 `81bdf9579`（通过）到 `c187f932f`（失败），第一个坏提交是 `f7029f54c`（新增 `manage_models` 工具）：工具目录变长后，用例固定的 15500 窗口装不下压缩后的候选。产品拒绝提交装不下的压缩是正确行为，所以只校准测试输入。
- **校准**：实测 16000–23000 都能先触发压缩、再装下候选，25000 起不再需要压缩；取中间值 19500，给工具目录增减留出余量，断言一条不放松。该文件 22 passed。

## macOS 沙箱拒绝读取 my-agent 私有目录（2026-09-26，分支 `claude/curator-budget`，基于 main `0624ab355`）

- **起因**：macOS Seatbelt 规则只有写拒绝，owner 隔离的 Shell 能读到其它 owner 的数据和 `~/.my-agent/config`（含密钥）；Linux bwrap 本来就不挂载这些路径。
- **规则语义实测**（本机 `sandbox-exec`）：后写覆盖先写。先拒绝根、再放行子目录时子目录可读；顺序颠倒时连本 owner 也读不到。
- **新增/调整测试**（`test_attempt_sandbox.py` 7 项、`test_sandbox.py` 1 项、`test_pty_sessions.py` 1 项；另外给 4 个既有测试的替身补上新参数或属性，断言不变）：
  - 规则顺序单元测试：先拒绝根，再放行本 owner、view、授权读根与写根，不放行其它 owner 与 config；Full Access 与未给根时不加读规则。
  - 真实 `sandbox-exec`（仅 macOS）：本 owner 文件与授权读根可读；其它 owner 与 config 读不到；一般宿主路径仍可读；view 可写。
  - 真实注册表装配（仅 macOS）：`ToolRegistryParams(host_private_root=…)` 构造出的 `run_command` 读不到 config 与其它 owner，本 owner 可读。
  - 传递链：`_sandbox_exec` 只在 owner 隔离时带根；前台、后台、终端会话三条路径都把根交给沙箱（终端会话经 `_PtySandboxInputs` 打包后原样转交）；`SimpleAgent` 把自身 home 根接进 Shell 工具；macOS 回执在配置了根时说明其它 owner 与配置读不到，没配置时不这么说。
- **变异验证**：11 种（不追加读规则、放行写在拒绝前、Full Access 也加规则、前台/后台/终端不传根、终端打包丢根、bootstrap 不传、core 不传、回执不看根、放行漏掉授权读根）全部使测试失败。
- **代码尺寸**：按“标识 + 严重级别”与 main 比对。`_spawn` 加参后一度触发硬性超限，改为把沙箱输入收进 `_PtySandboxInputs`，参数反而从超软上限降到接近上限；沙箱类的读规则判断移到类外，类长度不变。
- **回归**：工具注册、Shell、沙箱、终端会话相关 42 个测试文件 978 passed、9 skipped（按平台跳过）；构造 `SimpleAgent` 的 136 个测试文件 2670 passed、17 skipped、29 xfailed，另 2 个失败是 main 上既有问题：`test_background_first_request_compacts_after_complete_prepare` 两个协议，改动前同样失败，bisect 定位到 `f7029f54c`，下一个提交校准测试窗口。
- **事故记录**：第一次跑这 136 个文件时，按关键字挑文件误把 `agent_py_agent/tests/run_tests.py` 交给了 pytest。它在导入时就对真实 `~/.my-agent` 执行了 `status`、一次真实模型调用的 `run --no-save`，以及一次因参数不合法失败的 `remember`，留下运行时工作区 `main-a4e68bb5ead1`、运行目录 `runs/2026-09-26/861790a7…` 和全局索引条目；长期记忆未写入。按既有规则不手删、已报告用户。测试文件列表必须经 `grep '/test_[^/]*\.py$'` 过滤。

## Shell 沙箱回执按平台如实说明（2026-09-26，分支 `claude/curator-budget`，基于 main `2b25e38b3`）

- **起因**：Codex 验收 TUI-CAP06 发现，macOS 上 owner 隔离的 Shell 回执写 `external_host_paths_hidden=true`，而 Seatbelt 规则只拒绝写入、读取不受限，命令实际读得到宿主路径。
- **修改**：新增 `attempt/sandbox.sandbox_hides_host_paths()`，作为只读隔离的唯一事实来源（Linux bwrap 为 true，macOS Seatbelt 为 false）。回执文本与 `result_envelope.sandbox` 同源；macOS 版说明只限制写入，并写明工作区外的路径不在任务授权内。
- **测试**：`test_sandbox.py` 新增 2 项（平台事实，以及 macOS 回执的文本与结构化字段）；原 3 项 Linux 回执用例显式固定平台事实，断言保持原样。
- **变异验证**：4 种（不看平台、文本总说隐藏、平台判断总为真、结构化字段不跟平台）全部使测试失败。
- **回归**：Shell、沙箱、进程会话相关 28 个测试文件 662 passed、9 skipped（按平台跳过）。
- 读边界收紧（给 Seatbelt 加读拒绝）是安全缺口，另立项，见 DESIGN_LEDGER。

## 没配模型的 owner 不再反复失败（2026-09-26，分支 `claude/curator-budget`，基于 main `d00fde8be`）

- **真机现象**：飞书 owner 与测试 owner `tui-matrix/p1-r141` 未选模型，step12m 上 3 小时内各失败 28 次：每次重建 owner 实例、整批收集（p1-r141 每次都是同一批 47→46 条缩批），以 `ModelNotConfiguredError` 失败并原地重试一次，记成通用的 `CURATOR_MODEL_FAILED`。
- **新增** `test_curator_model_not_configured.py` 4 项：未配模型和 4xx 拒绝都只调用一次、不原地重试；服务级运行记 `CURATOR_MODEL_NOT_CONFIGURED`，游标不动，10 分钟后（原 5 分钟退避已过）不再调用，一小时后才重试；发现层对同样 10 分钟前失败的两个 owner，只唤醒普通超时失败的那个。
- **变异验证**：5 种（配置错误照旧重试、失败码不区分、待处理路径用原退避、发现层用原退避、退避函数不区分）全部使测试失败。
- **回归**：Curator、发现层、网关循环相关 29 个测试文件 600 passed。

## 结构化输出方式：DeepSeek 官方改用 JSON 对象（2026-09-26，分支 `claude/curator-budget`，基于 main `337a689ad`）

- **起因**：Curator 与自动总结 Skill 的结构化调用固定发 `json_schema`，DeepSeek 官方 OpenAI 兼容接口直接 400；默认模型设成它时，这两条后台链路会整体失效。
- **新增** `test_structured_output_mode.py` 17 项（传输全为本地 fake）：
  - 方式解析：显式声明优先；auto 只对 DeepSeek 官方 OpenAI 兼容接口给 json_object，OpenCode、未知域名、Anthropic 兼容、Responses 都是 native；非 OpenAI Chat 协议上声明 json_object 也不生效。
  - 真实组包：DeepSeek 上 `response_format` 为 `{"type": "json_object"}`，完整 schema 出现在提示开头、原提示在后；OpenCode 仍是严格 `json_schema` 且提示不变；声明可覆盖已知表；Curator 的 `call_backend_with_timeout` 在 DeepSeek 上同样发 JSON 对象。
  - 档案与配置：`structured_output` 保存、列出、解析进运行配置（未声明为 auto），auto 不写键；非法值、Anthropic 兼容上的 json_object、决策模型都被拒绝；改声明后缓存的后端会重建；`manage_models` 参数定义接受该字段；TUI 表单预选并保存；YAML 默认与规范化。
- **变异验证**：13 种（去掉已知表、结构化请求忽略方式、json_object 不写 schema、工厂不传方式、解析不带档案字段、auto 也写键、非 OpenAI 也接受 json_object、决策模型也接受、配置不规范化、缓存键漏字段、TUI 不保存、工具 schema 漏字段、非 OpenAI 协议也用声明）全部使测试失败。
- **回归**：原 `test_structured_output.py`（`common/structured_output` 的 6 项，本轮一度被误覆盖，已从 HEAD 恢复）通过；与结构化输出、后端组包、模型档案、TUI、配置、Curator、自动总结 Skill 相关的 65 个测试文件 1639 passed、4 xfailed（原有标记）。
- **真实验证**（工作区新代码 + 生产模型目录，只在进程内读取，不打印密钥与模型输出）：DeepSeek 官方 OpenAI 兼容档案强制 native 立即 `ProviderRequestRejectedError`（HTTP 400）；auto 解析为 json_object，服务商接受，8.9 s 返回，结果通过 Curator 严格解析，身份清单 3/3 覆盖，生成 2 个候选、1 条日记事件。

## 记忆 Curator 输入预算缩批（2026-09-26，分支 `claude/curator-budget`，基于 main `37cad88f7`）

- **真机现象**：生产本机 owner 自 2026-09-24T15:39Z 起每次都是 `CURATOR_INPUT_BUDGET_EXCEEDED`（9/25 共 180 次），`cursor_before` 始终同一个、每次处理 0 条、0 次模型调用；测试 owner `tui-matrix/p1-r141` 同样卡住。失败记录里还夹着别的 owner 留下的 `ModelNotConfiguredError` 尝试形状。
- **新增** `test_curator_input_budget.py` 7 项：
  - 服务级复现：真实 ConversationStore 80 条消息加 3 条真机形态审计（36 位编号、带预览）。先断言前置条件：按生产收集口径收满后最终提示超预算。修复后第一轮截尾、成功提交前缀，游标只推进到前缀末尾，运行结果带 `memory_curator_input_fitted`；第二轮重放剩余消息和审计，全部恰好处理一次。修复前同一场景连续三轮 `CURATOR_INPUT_BUDGET_EXCEEDED`、0 次模型调用、游标不动，失败诊断与真机记录逐字一致。
  - 缩批发生在可选决策标注之前：标注钩子只看到已裁进预算的批次。
  - 单元：先截消息后截审计、保留前缀、各留一条；未超预算原样返回（同一对象、无 warning）；预算小于模板时保底后仍超出，提取前检查保持原失败码。
  - 诊断：同一线程先成功提取一次，再遇预算失败，`last_model_attempts()` 为空；缩批时写一行只含条数的 WARNING 日志（成功运行的 warning 不进运行账，靠它在网关日志里看到缩批），未缩批时不写。
- **变异验证**：9 种（不调用缩批、缩批挪到标注之后、开头不清空形状、先截审计、允许截空、从头部截、不返回 warning、缩批不写日志、未超预算也换新对象）全部使测试失败。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行，并按 sha256 还原。
- **回归**：与 Curator 相关的 22 个测试文件加 `test_architecture_guardrails.py`，共 477 passed。
- **生产验证**（main `e47f60d0b`，双机 `step12l-5145e6cc`）：本机 owner 在 9/24 15:39Z 到 9/26 07:17Z 共 307 次 `CURATOR_INPUT_BUDGET_EXCEEDED`，全部是同一个 `cursor_before`。00:23 PDT 切换后第一次运行从同一游标开始，成功处理 44 条消息和 1 条审计，生成 2 个候选和 3 条日记事件，游标前进。缩批日志行随 `22b052fdb` 上线（双机 `step12m-cdda962b`）；切换后第二次运行（00:30 PDT）处理 24 条消息和 1 条审计，未触发缩批。测试 owner `tui-matrix/p1-r141` 当天修复前 56 次超预算，修复后变为 `CURATOR_MODEL_FAILED`（该 owner 未配模型，属配置问题）。

## 智能程度（推理强度）：`/effort` 与子代理 `effort`（2026-09-26，已合入 main `7a15c9c91`，基于 `84d9e50ac`）

- **新增** `test_reasoning_effort.py` 30 项（传输全为本地 fake）：
  - 换算：控制方式解析（显式声明优先；仅 DeepSeek 官方两种接口有默认；MiniMax、OpenCode、Responses、决策接口为 none）；档位与强制工具选择的优先级矩阵；两种协议的字段与思考预算夹紧。
  - 真实组包：OpenAI 兼容带 `reasoning_effort` 或关思考且不混发；DeepSeek 工具历史缺 `reasoning_content` 被迫关思考时去掉档位；显式声明控制方式的未知域名 `off` 真正写出 `thinking: disabled`，未声明的不写；Anthropic 兼容 `budget` 与关闭。
  - 档位解析：线程设置优先、清除后回全局默认、线程不可读时回默认；公用选项函数与网关自动选模投影载荷逐字一致，强制工具选择时不带档位。
  - 子代理：显式档位（大小写不敏感）、伪造宿主属性被覆盖、非法档位整批报错、省略时继承父级线程档位、`effort` 计入去重身份、子线程只在物化时写入一次档位。
  - `/effort`：真实 SimpleAgent + DeepSeek 档案下查看 / 设置 low / default 清除 / help，回执写明模型与“按推理强度档位发送：低”，不含密钥；echo 后端上如实回执“不支持调节、暂不改变请求”（改写原“永不假装已设置”用例）。
  - 档案与配置：`reasoning_control` 保存、列出、解析进运行配置（未声明为 auto），auto 不写键，非法值与决策模型拒绝；TUI 表单预选并保存；YAML 默认与规范化。
- **补充** `test_subagent_first_request_selection.py`：首轮自动换模的逐字比对改用同一 `request_reasoning_options` 计算期望载荷；新增用例在子线程档位 low、候选为 DeepSeek 官方接口时，断言确实采用候选模型且真实首轮请求带 `reasoning_effort: low`。
- **变异验证**：22 种中 21 种使测试失败（强制工具优先、已知表、none 不发、预算夹紧、DeepSeek 被迫关思考时去档位、声明方式的 off、Anthropic 关思考优先、选项丢档位、网关投影丢档位、子代理继承、线程覆盖、去重身份、子线程初始化、/effort 写入、档案映射、auto 不写键、配置规范化、TUI 表单、非法子代理档位、工厂控制方式、决策模型拒绝）。存活的 1 种是子代理首轮选模投影不带档位：该投影只用于容量估算、不做逐字比对，属于行为等价，代码仍保持与真实请求一致。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行并按 sha256 还原。
- **结果**：与改动直接相关的 39 个测试文件（含 `test_architecture_guardrails.py`）1074 passed；代码尺寸与 main 逐项对比无新增（`_do_backend_generate` 超软上限、`_payload` 参数两项消失）。
- **真实验收**（2026-09-26，main `7a15c9c91`，运行时 `step12k-e5b2f8bc`；本机隔离 home + 回环 8432 单 Gateway，默认模型为 DeepSeek 官方 OpenAI 兼容 flash；测试者只发 `/effort`、`/model` 控制命令和每个会话一条任务。题目是“1 到 2000 中既是 3 的倍数、各位数字之和又能被 7 整除的整数个数”，答案 64。token 取用量账本 `purpose_breakdown.main`，排除 Jev 决策调用）：
  - **DeepSeek 官方 OpenAI 兼容**：`/effort` 回执写明档位、来源和当前模型上的效果。出站请求：low/max 分别带 `reasoning_effort: low|max`，off 带 `thinking: {type: disabled}`，auto 不带任何字段；`/effort default` 清除后回到全局默认。主模型输出 token：low 4 次 1059–2185（均值约 1536），max 4 次 1494–5392（均值约 2847），auto 3 次 1234–5192，off 2 次 272 和 566。同一道题在完整主代理上下文里波动很大，只能看出 max 平均约为 low 的 1.9 倍，off 最低；off 两次中有一次答错。
  - **DeepSeek 官方 Anthropic 兼容**（auto 解析为 budget）：off 时助手原生消息只有正文、没有思考块，推导写进了正文，答案正确；high 和 auto 都有思考块，答 64。Anthropic 协议没有出站诊断摘要，这里以原生内容块作结构化证据。
  - **MiniMax M3**（在隔离目录里经产品 `save_model` 声明 `reasoning_control: budget`）：auto 没有思考块，答错；`/effort high` 后出现思考块（26713 字符），答对。输出 token 从 5609 升到 11999，用时从 21 s 升到 99 s。
  - **MiniMax M2.7**（none）：`/effort high` 回执如实说明“不支持调节……本设置暂不改变请求”，请求照常完成。
  - **子代理**：主会话先 `/effort medium`，再用一条任务让主代理一次创建三个子代理（effort=low、effort=max、不设）。子线程记录与任务宿主属性分别是 low、max、medium，不设的继承了父级 medium。出站请求逐条对上：low 子代理 4 轮都带 low，max 子代理 7 轮都带 max，继承的子代理 4 轮都带 medium；主会话前台 3 轮加后台续跑 5 轮都带 medium；3 次宿主能力探测不带档位。三个子代理都答 64，输出 token 为 low 1641 < medium 2373 < max 7305。
  - **附带发现**（与本改动无关，已登记 DESIGN_LEDGER“记忆 Curator 生产持续失败”）：隔离环境的记忆 Curator 给 DeepSeek 官方 OpenAI 兼容接口发 `response_format: json_schema`，被 400 拒绝（“This response_format type is unavailable now”），记为 `CURATOR_MODEL_FAILED`。
  - 证据在 `~/.my-agent/releases/reasoning-effort-acceptance-20260926/`，只含结构化摘要；隔离 home 与模型目录副本已删除。

## 自学习 S3：完成任务后自动总结 Skill（2026-09-26，分支 `claude/skill-auto-summary`，基于 main `839250728`）

- **新增** `test_skill_learning.py` 34 项（假 backend，不联网）：
  - 触发与材料：不落盘、task_local、后台回合、工具轮数不足、缺运行身份都不入队；请求有界（输入/回复截 3000 字、轨迹 80 条）、密钥脱敏、只收成功的 `skill_search get` 读过的 skill_id。
  - 发布：create 后下一次快照出现 `owner:<名字>`（category `learned`、content_sha256 等于登记值），账本、版本全文、每日计数正确，请求删除、暂存目录清空；skip 只记账。
  - 拒绝：7 种输出不合规（非 JSON、多字段、坏名字、坏标签、正文过短、正文带 frontmatter、未授权的 update）都记 `OUTPUT_INVALID` 且不重试；与用户 Skill 重名、guard 命中 `curl | sh`、数量上限、删过的名字各返回对应码且不写文件；直接调用发布接口时 3 种不能往返的 frontmatter 被 `DRAFT_INVALID` 拦下。
  - 所有权：update 只针对本轮读过、登记在册、磁盘 hash 未变的自学 Skill，提示词里给出其完整字段；用户手改后同样的 update 被拒、文件不动；未在本轮读过的 update 被拒。
  - 运行：每日上限顺延；模型超时先重试（attempts=1）、再失败则丢弃记 failed；运行锁被占与前台同端点忙都返回 busy 且零调用；队列 20 条上限、重复入队忽略；损坏请求丢弃、损坏登记表失败关闭且保留请求。
  - 回滚删除：v2 回滚到 v1 文本逐字一致，v1 再回滚等同删除并进禁用名单；用户改过的拒绝回滚但允许删除。
  - 宿主会话：模拟要求会话头的服务商（无会话即 `ValueError`），后台调用自绑 owner + 请求键的会话，重试共用同一会话值，调用结束后当前线程不再持有会话。这是真实验收首轮发现的缺陷（真实服务商要求会话头，两次尝试都失败后丢弃），修复前本用例失败。
- **新增** `test_skill_learning_integration.py` 8 项：组合根只在开关开启时装配、与 Curator 共用 backend、不预建目录；finalize 只在任务完成时入队；收口 helper 吞异常；Gateway 策展车道对“记忆关闭但有学习请求”的 owner 只跑学习、正常 owner 先整理后学习、学习异常隔离、`memory_curator_enabled=false` 时不跑整理；车道开关按两项配置之一；`skills learned list/show/revert/remove` 真实 CLI 往返（含手改后状态 `user_modified`、回滚被拒、删除入账）；S1 lesson 提案经 runner 自动确认（`confirmed_by=auto`）；四个新配置键的 YAML 默认与越界规范化。
- **真实验收发现并修复的两处**：① 服务商要求会话头，后台调用未绑宿主会话 → `ValueError`（已修，见上）；② 首个真实发布的 Skill 含“删除被拦截就改用 apply_patch 删”这种绕过安全拦截的做法 → 提示词“不要保存”清单加一条，更新时一并删除（软约束，用例断言提示词含该条）。
- **改写** 2 项旧断言（行为按用户决定改变）：`test_skill_proposals.py::test_runner_result_auto_confirms_proposal_when_service_attached`、`test_subagent_lesson_ledger.py` 的自学习开启用例，从“提案保持待确认”改为“自动确认并安装”。
- **变异验证**：40 种各自使测试失败——宿主会话绑定、组合根传 owner_id、触发的 task_local/后台/门槛/do_save、材料脱敏、只收成功读取、名字/更新目标/`#`/frontmatter 正文检查、重名/禁用名/上限/所有权 hash、发布脱敏、guard force、frontmatter 往返、回滚所有权、删除入禁用名单、每日上限、队列上限、去重、前台让路、重试次数、本轮读过过滤、可更新 hash、运行锁、车道准入/重算/总开关/整理开关、组合根开关、finalize 调用、S1 自动确认与确认方、CLI 状态、登记表损坏、来源 run。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行并按 sha256 还原，之后删除 `__pycache__` 重跑。
- **结果**：与改动直接相关的 52 个测试文件（含 `test_architecture_guardrails.py`）1015 passed；每次提交前严格 gate 全过（ruff、doc sync、strict code-size 与 main 持平 2200/1496/704、`git diff --check`、clean-package）。
- **真实验收**（2026-09-26，本机隔离 home + 回环 8432 单 Gateway，owner 真实选定模型，门槛配置为 4，测试者每轮只发一次 prompt）：
  - 第 1 轮（CSV 合并，3 轮工具）不到门槛，不入队；第 2 轮（多编码，6 轮）入队，但服务商要求会话头而后台调用没绑宿主会话，两次都 `ValueError` 后丢弃并记 `failed`——据此修复并发 step12h。
  - 第 3 轮（做成命令行工具，12 轮）入队后由策展车道处理，真实模型发布 `csv-merge-cli` 第 1 版，下一次快照出现 `owner:csv-merge-cli`（category `learned`，hash 与登记表一致）；正文含“删除被拦截就改用 apply_patch 删”，据此加提示词约束并发 step12i。
  - 第 4 轮（同类任务，3 轮）模型没读 Skill、也不到门槛；第 5 轮（用户说按之前总结的做法来，25 轮）模型先 `skill_search get owner:csv-merge-cli`，后台据此选 update，发布第 2 版（补了 UTF-16、¥、千分位三个新坑），正文不再含绕过拦截的内容。
  - `skills learned list/show/revert` 在真实数据上运行正常，回滚后文件与 `versions/v1.md` 逐字节一致；首日总结调用 4 次。验收中发现版本目录多出 `.lock` 文件，已改用不取锁的原子写。证据在仓库外 `~/.my-agent/releases/skill-learning-acceptance-20260926/`，隔离 home 与模型目录副本已删除。

## 决策开关、超时自调、统一审计与管理员管控（2026-09-25，分支 `claude/decision-audit-controls`，基于 main `07fa00fb3`）

- **新增** `test_decision_audit_controls.py` 20 项：注册范围（main 两个工具、user 只有审计、group 都没有，管理员工具审批为 always）；
  管理员控制默认值、坏块/坏文件失败关闭、写入保留其它策略字段与 0600；只允许管理员写（not_admin/admin_self_only/invalid_changes）；
  跨用户许可只在管理员名下生效；owner 编号解析拒绝非规范或不存在；管理员工具 list/set 与用户侧即时生效；
  审计的时间窗、本人范围、观察白名单（提示词与工具名清单不外泄）、跨用户许可、无 Gateway 上下文时观察不可用。
- **补充**：`test_decision_service_http.py`（管理员关闭后本机 HTTP 零请求、零调用记录、`admin_disabled`，重新允许立即恢复；坏策略失败关闭）；
  `test_user_config_owner_scope.py`（自调等待时间越界拒绝且不保存、范围回显、能力配置 0=不限）；
  `test_gateway_model_observation.py`（选模型输入去锚点段、按上限截断并标注、候选公共声明只写一次）；
  `test_decision_usage_metrics.py`（成功/失败计数、约数外推、全部缺报 `≈?`、旧账不计入）；
  `test_tui_decision_menu.py` 与四个接入点集成测试（两个勾选到三种模式的换算与真实按键保存）。
- **结果**（变基到 main `1ea760c0f` 后）：全部 `test_decision_*.py`、`test_tui_decision_menu.py`、`test_tui_model_metrics.py`、
  `test_user_config_*.py`、`test_gateway_model_*.py`、`test_gateway_compact*.py`、`test_capability_*.py`、配置/审批/owner 策略、
  `test_admin_identity_*.py`、工具面快照相关文件、恢复码/合同与 `test_architecture_guardrails.py` 共 3394 passed（24 xfailed、5 xpassed 为既有标记）；
  ruff、`check_doc_sync --base origin/main`、strict code-size（与 main 完全相同：2200/1496/704，新增 0）、`git diff --check`、clean-package 均通过。
  变异 24 个（管理员硬门、阶段提前返回、失败映射、审计本人/跨用户权限、观察归属与白名单、时间窗、自调上下限、成败计数、约数、
  锚点段、截断标注、审批 always、注册条件、执行复核、勾选换算、候选去重、连接测试说明）全部被抓出，
  每个都在 `PYTHONDONTWRITEBYTECODE=1` 子进程里跑并逐字节恢复。
- **真实 TUI 验收**：本机隔离 home、127.0.0.1:8431、真实 Jev，管理员与普通用户两个 TUI 各走一遍，
  见[决策开关、超时自调与审计](docs/design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#真实-tui-验收2026-09-25本机隔离-homegateway-只绑-12700018431)。

child不展示历史正文时的读取回归先复现1 failed/1 passed，修复后test_compact_retained_history、test_subagent_compact_recovery、test_gateway_child_compact_scope_application三文件31 passed（8.48秒）。三宿主seed仓外基线仅验证测量和完整性，不算内存目标通过；细节见容量审计的宿主生命周期基线。

## 第 10 步组合验收 combo6 与全部卸载后核心基线（本机 `runtime-step11l-2d00c734`→`step11m-f1cc8217`，2026-09-25 凌晨）

- **组合任务（真实 owner，一条 prompt，M3）**：5 个插件（genui-lite、image-text、savepoint-lite、browser-lite、design-lite）经真实 TUI 安装→配置→启用后，
  发一条 36 个月订单 CSV 的三年汇总长任务：3 个子代理按年统计→主代理独立重算核对→genui-lite 出三张柱状图→browser-lite 逐页核标题→savepoint-lite 快照→design-lite 出卡片→写报告。
  PROMPT_SENT 01:33:54，产物最后写入 01:39（约 5 分钟），无审批弹窗，3 个子代理终态均已完成，Compact 0（上下文约 105k/1.0m）。
- **产物核对（只读结构化事实，不读回复正文）**：`out/` 13 个文件；与测试者预先生成的真值文件逐项比对 18/18 一致——三年各年完成金额、各区域金额、
  36 个月度金额、三类非完成状态订单数、各年最高区域、2024/2025 同比、三年总额。子代理中途把输出路径解析成 `combo6/combo6/out/`，主代理用 `send_guidance` 澄清后两处一致；
  这是模型行为，不是框架缺陷，产物最终位置正确。
- **脚手架教训**：观察脚本用屏幕关键词判"安静"，报告正文含"子代理"等词导致 WAIT_END 迟到 40 分钟；属测试脚手架问题，与产品无关，下次改读结构化会话状态。
- **升级回归（本轮前置发现并修复）**：见上文"激活目录摘要随声明字段漂移"；修复部署（step11l）后本次安装、启用、调用全部正常。
- **全部卸载后核心基线（step11n 二进制，同一真实 owner）**：第一轮误用 `/plugins uninstall`（无此命令，安装表 last_commit 仍为 release，5 插件只是停用），基线 summary.txt=400.00 在 25 秒内写出、无审批，但不算“空表”。第二轮用 `/plugins remove` 移除全部 6 个插件（含 workspace-peek），空表下发一条核心任务（读 CSV 合计写文件）：REMOVE_DONE 02:37:19 → 文件写出 02:37:42（16 秒），值 1234.56 正确，无审批弹窗、无插件工具。随后从仓库重建的 workspace-peek 包 `/plugins install` + `/plugins enable`，安装表只剩 workspace-peek 0.1.1 active（revision 从 17 重置为 3，证明确经移除再装）；packages 目录保留 1 个旧包文件（内容寻址，不影响运行）。测试目录已归档到仓库外并从 owner home 删除。

## 内置方法论技能正文重写（开源准备，2026-09-25，分支 `claude/opensource-prep-decision`）

- **改动**：`agent_py_agent/skills/builtin/` 下 quality、planning、orchestration、review、meta 五类共 12 个 `SKILL.md` 的正文，按原意图用自己的话重写。这 12 个技能是 test-driven-development、systematic-debugging、verification-before-completion、brainstorming、writing-plans、executing-plans、define-goal、dispatching-parallel-agents、subagent-driven-development、requesting-code-review、receiving-code-review、writing-skills。
  - 结构、标题层级与示例都另写，不逐句转写旧文。
  - frontmatter 的 name、tags、scope、risk_level 逐字节不变。与上游同名的 11 个技能另改写了 description 与 when_to_use：意思不变，保留用户常用的检索词（路由按 tags 4 分、描述 1.5 分给词打分）。define-goal 的 frontmatter 未改。`documents/academic-word-pdf-layout` 原先引用旧口号，已改为直接写规则。
  - 正文里的工具名、技能引用、命令与路径约定保留。上游示例用的占位路径和示例函数名换成本项目自己的示例。
- **核对方法**：逐文件对比 main `97fe9d72a` 的旧正文。
  - frontmatter 逐字节相同。
  - 旧正文中的工具、命令、路径类标识符都还在；只少了上游示例名与占位路径，属有意替换。
  - 新旧正文去空白后的 8 字符片段重合率为 0.9%—4.1%。最长公共片段不超过 62 字符，全部是标识符列表、命令或路径。
- **测试**：读取内置技能、`SKILL.md` 或技能目录的 20 个测试文件 212 passed。覆盖技能加载与优先级、代表性任务路由、技能守卫扫描、打包、声明式索引、已退役工具名检查与架构守卫。
- **路由对照**：12 条贴近日常的中文请求（每个技能一条）用新旧 frontmatter 各检索一次，12 个技能新旧都排第一。这是临时的一次性对照，用例未入库。

## 动作候选 `action_candidate`（2026-09-25，分支 `claude/decision-action-candidate`，基于 main `6a50d84aa`）

- **改动**：
  - 新增 `tool_context/decision_action_candidate.py`；`_optional_result_hints` 追加第三个点。
  - `POINT_RUNTIME_SCOPES` 登记 `action_candidate: thread`；AgentConfig/YAML 三字段默认 off/null/null；TUI 决策菜单加"动作候选"。
  - 新鲜度只问插件线的 `plugin_observation.observation_is_current`；数量上限、`obs-`/`cand-` 编号、key/role 规则与 `OBSERVATION_SCHEMA` 直接复用 `plugin_observation`，`target_kind` 复用 manifest 规则，不另立第二份。
- **新测试** `test_decision_action_candidate.py` 82 项。单元用例只替换决策服务和模块内的新鲜度函数；另有两项集成用例用插件线真实的 `parse_observation` 铸观察，经产品写入口 `persist_tool_runtime_ledger`（带真实 LocalStore，归档没有 `runtime_gate`，与真实链路一致）记进权威事件流，再由真实 `observation_is_current` 判定。覆盖：
  - 未登记时严格空操作；off、阶段错误、他 run 阶段都不准备材料。
  - apply 只追加所选候选的 candidate_id 与 role。外发不含 key、目标引用、代次、激活、候选编号、动作名、请求密钥或工具输出；label 只出现在同一个外部数据块。
  - observe 以及 deadline/cooldown/error/stale 都保留原结果；四种非选择和坏答案同样保留。
  - 所选候选的动作工具不可用时不追加。
  - 32 种不合格来源零请求：schema 版本不符、数量越界、编号/类型/角色/label/key/动作不合规、归档不一致、失败或重放调用、两种收口、请求超长或为空、请求或 label 含 URL 查询串、没有可用动作。
  - 新鲜度：不新鲜时零请求并以 owner 库、run、task、observation_id 询问权威；等待期间变旧不追加；没有权威库、权威返回非 True 或抛错都安全关闭。
  - 集成：当前观察给出提示，同时门台账不写、完成事件照写；同一目标有了更新观察后旧候选不再提示。修复前的 `persist_tool_runtime_ledger`（无门即提前返回）会让第一项失败。
  - 子代理零请求；等待期间来源或工具快照变化、两种期限、响应绑定其他材料、策略变化都不采用；可选错误保留原结果，取消与中断上抛。
  - 经原 `_record_tool_call` 在 off/observe/apply 下 text/native/IR 为同一段；三点互斥；默认配置为 off 且 thread 可设；原 TUI 菜单可改线程模式。
- **变异验证**：19 种各自使新测试失败（新鲜度恒真、去掉采用前来源复核、渲染或资格忽略可用性、候选下限 2→1、去掉重复编号检查、外发 key、去掉 ok/子代理/URL/题号/策略/期限/label 上限/候选上限/归档 task/响应绑定/权威库/schema 检查）。子进程带 `PYTHONDONTWRITEBYTECODE=1`，结束后原文件逐字节还原。
- **回归**（rebase 到 main `6a50d84aa` 后）：全部 `test_decision_*`、引用 `_record_tool_call`/`_optional_result_hints`/设置 schema/TUI 决策菜单的文件，加插件观察、代理观察、运行门账本、browser-lite 包与架构守卫，共 57 个文件 1530 passed、1 xpassed（既有）。
- **真实验收**：本机隔离 owner 上用 browser-lite 做 off/observe/apply/过期四档，见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#15-动作候选-action_candidate-的-browser-lite-真实验收2026-09-25)。首次运行暴露宿主完成事件在无 `runtime_gate` 时不写的缺口（插件线已在 main `6a50d84aa` 修复）。

## 自愈两片：无身份悬挂运行轮可见可结清、导航种子不再误迁移（用户决定第 5 项，2026-09-24 晚）

- **无身份悬挂运行轮**：本机 owner 库有 9 条 8 月的 running/created attempt，metadata 为空、没有 `runner_pid`，原 RUN-01 进程死亡证明永远碰不到它们。不自动判死（同一 owner 库会被别的运行版本写入，原合同"无 pid 记录保持原态"不动）；改为 Gateway 启动列出并写进状态/事件（`unidentified_stale_attempts`、`gateway_stale_attempts_reconciled.unidentified`），新增显式命令 `my-agent runtime-stale-attempts [--settle] [--older-than-days 1] [--json]`，`--settle` 经 `RuntimeRepository.settle_unidentified_attempts` 的 current_attempt CAS 记为 unknown（metadata `recovery_reason=no_runner_identity`、`settled_by=explicit_control`）。
- **导航种子**：`owner_navigation_seeds()` 统一 memory.md/HOT 的种子文本（根模板或正式默认导航），`_scan_legacy_navigation` 把正式默认与本 home 种子都判 current；新 owner 初始化后不再产生模板标题候选。
- **测试**：`test_runtime_db_recover_stale.py` +1（列出、阈值内保留、显式结清与幂等、有身份行不受影响），`test_startup_commands.py` 两项断言含 `unidentified`，`test_memory_migration_v2.py` +1（种子/根模板副本/正式默认均 current，自定义正文才 legacy）。
- **真实验收**：部署后本机跑 `my-agent runtime-stale-attempts` 看到 9 条并用 `--settle --older-than-days 7` 结清，Gateway 事件与 status 载荷计数归零。
- **status 可见（2026-09-24 深夜）**：`my-agent status` 的 Gateway 段与 `--json` 的 `gateway.unidentified_stale_attempts`、`gateway status` 的
  `gateway unidentified_stale_attempts=N` 都只投影 Gateway 启动写进 state.json 的计数，大于 0 才出行并指向 `runtime-stale-attempts`；不查库、不结清。
  测试：`test_status_commands.py::TestStatusUnidentifiedStaleAttempts`、`test_gateway_status_runtime_errors.py` +1。
- **升级回归：激活目录摘要随声明字段漂移（2026-09-25 凌晨，真实 owner 发现）**：组合验收前 `/plugins install` 一律回"无法读取当前插件目录"，Gateway 日志"插件安装目录不可读：PluginInstallationError"。根因：manifest v5 给 `PluginToolDeclaration` 加了 `observation/observation_ref`，`plugin_catalog_digest` 直接哈希 `asdict(tool)`，升级后所有既有激活记录的 `catalog_sha256` 对不上，`_validate_activation` 把整张安装表判为不可读——用户真实 owner 上的 workspace-peek 从 step11d 部署起就没有插件工具/Skill/管理入口，直到 step11l 修复。修法：摘要只哈希有值字段（新可选字段为 None 不进摘要），既有记录的摘要逐字节复原；不改用户数据。测试 `test_plugin_catalog_digest_stability.py`（旧公式相等、声明观察改变摘要、顺序无关）。规则：以后给声明类加字段必须默认 None/空并跑这个测试。
- **任意语言插件（包描述 v6，2026-09-25，本地）**：`test_plugin_any_language.py` 35 项——v6 描述往返与 20 种非法声明、可复现打包与多余成员/篡改拒绝、解释器真实路径与摘要、确认回执内容与 12 位确认码、未确认不准备不启动、错码拒绝、带码启用后真实 MCP 调用（随包可执行文件与系统解释器两种入口，用 python3 脚本扮演，不依赖 node/go）、停用释放与卸载、解释器被换/消失/定位文件被改拒绝且 stat 未变不重算摘要、平台变化拒绝、准备阶段拒绝计划后被换的解释器、解包权限与随包 Skill 目录。`test_plugin_any_language_samples.py` 4 项——工作区读取一致性用例（74 条裁决 + 20 个畸形上下文）先由宿主参考实现校验、再由 Node 移植逐条比对；hello-node（读取上下文内可读、越界 `PATH_READ_SCOPE_BLOCKED`）与 hello-go（按本机平台交叉编译）经宿主安装→确认→调用；没有 node/go 时跳过。变异：宿主侧 12 个、Node 移植 8 个全部被抓出。兼容：14 份 v1–v5 声明在 main 与本分支 `to_payload()` 输出 sha256 相同，`test_plugin_catalog_digest_stability.py` 通过。开发中顺带发现的原有问题：Homebrew Python 3.14 默认 sysconfig 方案 `osx_framework_library` 的 purelib 忽略 `base`，Python 插件随包 Skill 目录会算错（`test_plugin_skills.py` 在这类宿主上失败）；已报插件线，插件线在 `b4b59158f` 修复，本分支 rebase 后合并了两边改动。真实 TUI 验收（构建自 `e895dadbf`，本机 macOS + 测试机 Linux，隔离 home、127.0.0.1:8431）：两种入口的安装→回执→带码启用→调用、错码拒绝、真实模型经插件工具读到工作区文件、解释器被换后拒绝并需重新确认、异平台包启用被拒均按预期；验收中发现平台不符与解释器被换时 TUI 只显示通用失败文案，已改为按原因码给出具体说明、显式调用结构化拒绝（`PLUGIN_RUNTIME_UNAVAILABLE`），补断言并做 3 个变异。
- **决策故障矩阵 DNS 用例只在本机通过（2026-09-25，CI 首次暴露）**：`test_decision_fault_matrix[dns]` 在开发机期望 deadline 并通过，在 ubuntu-24.04 runner 上得到 error。根因：开发机环境变量配了 HTTP 代理（127.0.0.1:7890），非回环请求先连代理，测试在进程内注入的 `getaddrinfo` 失败根本没被调用，代理解析拖过 0.3 秒期限才成了 deadline；决策请求本就零重试，直连时解析失败即 error。修法：本文件加 autouse 夹具清掉代理环境并 `NO_PROXY=*`，dns 期望改为与连接被拒相同（error、冷却 30 秒、仍失败则 60 秒），并断言注入的解析确实被走到；去掉夹具在有代理的机器上会失败（已验证）。设计文档故障矩阵同步更正。跟进（同日，CI 3.11 job）：dns 恢复步骤在 runner 上得到 deadline——整次决策调用（路由、记账、工作线程、传输）都算在 0.3 秒期限里，慢机器挤不下。现在只有“慢响应”用 0.3 秒制造超时，其余立即失败的故障给 5 秒；恢复一步统一换新决策阶段并把期限调到 6 秒（配置类故障靠这次修订变化重试，按时间冷却的故障不受修订影响）。用临时插件给每次传输加 0.35 秒模拟慢机器：旧测试 8 个故障用例全失败，新测试全过。再跟进（CI 3.10 job）：同文件并发用例 12 个会话全部 deadline——服务端等 13 个请求到齐才放行，到齐本身就可能超过 1 秒期限；改为期限 8 秒、阶段预算 15 秒并在改预算后重开本会话阶段，等待到齐上限 6 秒；目录写锁用例同样给足期限。模拟传输延迟 1.2 秒时旧并发用例复现 `deadline` × 12，新版本文件 10 项全过。
- **插件进程 OS 沙箱试点（2026-09-25，本地）**：`test_plugin_sandbox.py` 10 项——开关在 YAML/dataclass/规范化里默认关、配置真的传到管理上下文/模型工具注册/面板客户端、bwrap 整根只读参数布局（且不改既有形态）、`PluginMCPClient` 包装启动命令与 `TMPDIR` 指向数据目录、沙箱不可用时启用（准备前）与显式调用（建运行前）都以 `sandbox_unavailable` 结构化拒绝；真实平台沙箱下数据目录可写、工作区与插件环境写不进、工作区可读，沙箱内 hello-node 正常且停用后无残留进程。本机 macOS（Seatbelt）与测试机 Linux（bubblewrap 0.8.0）各实跑通过；原 `test_sandbox.py`、`test_attempt_sandbox.py` 在测试机全过（50 passed、5 个 macOS 专项跳过）。测试机首轮发现只读根模式挂私有 `/tmp` 会盖住放在 `/tmp` 下的 owner home 与插件环境，已去掉私有 `/tmp`，改由 `TMPDIR` 指向数据目录。变异 10 个（不包装、无数据写根、根可写、启用/调用跳过预检、core/注册/面板/管理上下文丢开关、无 TMPDIR）全部被抓出。真实 TUI 验收（开关打开，本机 macOS + 测试机 Linux）：workspace-peek、hello-go、hello-node 安装启用调用与真实模型一轮都正常；macOS 运行中插件进程经 `sandbox_check` 确认在沙箱内（Gateway 对照为否），Linux ps 可见 bwrap 整根只读包装；停用后两边都无残留进程。
- **后台服务监听范围与 owner 长期授权（2026-09-25，用户决定）**：`run_command` 新参数 `background_listen_scope`（默认 loopback）；`ApprovalPolicy.owner_grant_parameters` 声明 `lan` 为可长期允许的操作，审批面板多出 `approved_owner`，写进 owner `tool_policy.json.operation_grants`；自主模式不放行未授权的 lan。host 每 2 秒按 socket 表核对，越界即 `killed/listen_scope_violation` 并留 `listener_violation` 证据，`run_command` 返回 `BACKGROUND_LISTEN_SCOPE_VIOLATION`。测试 `test_background_listen_scope.py` 11 项（含真实进程：绑 0.0.0.0 被回收、绑 127.0.0.1 与 lan 放行、关闭 enforce 只告警）；launch spec 升 v6，既有夹具补两个字段。
- **停机后存活的后台会话（2026-09-24 深夜）**：Gateway 停机收尾只读列出仍未终态的受管后台进程，写事件 `gateway_background_sessions_surviving`、state 计数 `surviving_background_sessions`，`my-agent status` 在 Gateway 未运行时显示 `background_sessions_after_stop=N`；不停进程。测试 `test_gateway_background_sessions_shutdown.py`（真实 store + 活进程只列非终态并带监听事实、无注册表/无目录返回空且不建目录、收尾事件与失败扫描不阻塞、state 合并），`test_status_commands.py::TestStatusSurvivingBackgroundSessions`。

## 插件观察候选结构（第 15 项 P5-C 前置，2026-09-24 晚）

- **改动**：manifest v5（`observation` / `observation_ref`，安装校验与双向配对，构建脚本自动选 v5）；新模块 `agent/plugin_observation.py`（整份接受/拒绝、宿主铸 ID、模型投影、按 runtime_events 序判定新鲜度、动作候选复核）；`PluginProxyTool` 结果路径改写与发送前复核（宿主参数 `__operation_id`/`__run_scope`，`_meta["my-agent/observation"]`）；`MCPProxyTool._execute_with_meta` 让子类按本次参数附 _meta 而不缓存到共享实例；`tool_completed` 事件载荷附观察投影；`runtime_db.events_for_agent_run`；归档白名单加 `observation`/`observation_rejected`；`ToolRegistry` 构造参数 `plugin_runtime_repo`；browser-lite 输出观察候选并按候选执行。设计与偏差见 `docs/design/PLUGIN_OBSERVATION_CANDIDATES.md` 第 6 节。
- **新测试**：`test_plugin_observation.py` 19 项、`test_plugin_proxy_observation.py` 5 项；`test_plugin_package.py` +14 项（v5 往返、13 种非法声明）；`test_browser_lite_package.py` 描述断言 + 1 项真实浏览器候选流（本机无 Chrome 时跳过）。
- **真实链路缺口修复（2026-09-24 深夜，决策线在隔离 Gateway 上发现）**：只读工具的归档没有 `runtime_gate`，`persist_tool_runtime_ledger` 提前 return 导致零 `tool_completed` 事件、新鲜度恒 False。改为每次工具完成都进权威事件流（无门时 status 按 ok），legacy 门账本仍只在有门时写；`events_for_agent_run`/`events_for_attempt` 取最新窗口再升序。`test_plugin_observation.py` +2（走 `persist_tool_runtime_ledger` 的无门归档、真实 SQLite 库 2100 条填充后最新观察仍在窗口内且按 attempt 读也取最新），原观察测试全部改走真实持久化入口；`test_runtime_gate_ledger.py` 原 8 项不变（有门事件、无权威库跳过、写失败不崩）。
- **插件层拒绝提升为结构化码（2026-09-24 深夜）**：`PluginProxyTool._lift_observation_error` 把 isError 结果里的 `my_agent_observation_error.code`（stale/not_found）提升为 `reported_error_code` OBSERVATION_STALE/OBSERVATION_CANDIDATE_UNKNOWN、TOOL_INVALID_ARGUMENTS、not_started；`test_plugin_proxy_observation.py` +1（两种提升、其它插件错误沿原映射、非候选路径不提升）。
- **真实验收**：决策线已于 2026-09-25 在隔离 owner 用 browser-lite 完成 off/observe/apply/过期四档（见 `docs/tasks/DECISION_MODEL_REAL_VALIDATION.md`）；宿主侧以合同测试为准。

## Gateway 停止时结清在途模型调用（用户决定第 4 项，2026-09-24）
- **背景**：同伴观察到 Gateway 停止时一条后台非流式 M2.7 请求（约 26.6KB，Memory Curator）被切断，收尾 `drain_complete=true` 却没有任何结算事实。
- **改动**：`contracts/model_call_ledger.py` 新增 `ModelCallLedger.fail_open_calls()`（用途分区函数改为公开的 `model_call_purpose`）；`agent_core/model/call_runtime.py` 新增 `settle_open_model_calls_for_shutdown()`（只读已有账本，不新建）；`cli/gateway_process.py` 收尾拆出 `_join_gateway_loops()`，排空后由 `_settle_interrupted_model_calls()` 写 `gateway_model_calls_interrupted` 事件并在收尾载荷加 `interrupted_model_calls`。
- **新测试** `test_gateway_model_call_shutdown_settlement.py` 5 项：批量终态只动活动记录且幂等；facade 不新建账本、投影只含固定结构化字段、结清后不再算运行中；收尾在停 HTTP 与收循环之后、写心跳之前结清并写事件；没有在途调用（含没有账本的 agent）不写额外事件；账本模块抛错只记异常类型且收尾照常完成。
- **真实验收**：待下次部署后用一次真实 Curator 在途停机复验，看 Gateway 事件里的 `gateway_model_calls_interrupted` 与收尾载荷的 `interrupted_model_calls`。

## 线程中断标志与 ident 复用（2026-09-25，分支 `claude/interrupt-ident-reuse`，已合入 main `761ef2ab2` 并部署）

- **问题**：`test_subagent_first_request_selection.py` 的真实 child 用例只在跟一大批决策测试一起跑时失败。
  - 直接原因：`_wait_for_generation_result` 被停止时会给 worker 立中断旗；测试里的假 worker 退出时没有撤旗，之后复用同一 ident 的生成线程 `my-agent-model-generate-timeout-guard` 一开始检查就被判为已中断，整次 child 运行记为 cancelled。
  - 生产也有同样的竞态：worker 自行撤旗之后、真正退出之前被立的旗无人再撤。
- **修复**：`concurrency/interrupt.py` 的标志从 ident 集合改为"ident → 立旗时线程对象的弱引用"。检查时线程已退出或 ident 换了主人即视为过期并清除；给已退出线程立旗落空；句柄与命名登记语义不变，公共接口不变。测试里的假 worker 改为与生产一致，退出前撤旗。
- **新测试**（`test_thread_interrupt.py` 5 项）：
  - 给已退出线程立旗落空；
  - 模拟 ident 复用：新线程不继承旧旗，回调也不被触发；
  - 撤旗后晚到的立旗随线程退出失效；
  - 先 join 再新建线程、强制复用同一 ident，新线程不被中断（200 次内撞不上即跳过，本机未跳过）；
  - 跨线程给存活线程立旗照常生效。
- 反向验证：换回旧实现时前 4 项失败，第 5 项照常通过。
- **回归**：
  - 引用中断/有界调用的测试族加架构守卫共 29 个文件：760 passed、1 skipped（既有的 Linux /proc 限制）；
  - 原先稳定复现失败的 63 文件组合：1429 passed、2 skipped、3 xfailed、0 failed；
  - Ruff、doc sync、代码体量（与 main 相比无新增项）、diff、clean-package 全部通过。

## 宿主关闭取消在途决策与 Curator/插件点并发组合（P4-F，2026-09-25，分支 `claude/decision-shutdown-cancel`，已合入 main `25650830d`）

- **改动**：
  - `decision_policy.py`：新增关闭标记、`cancel_active_decisions_for_shutdown()` 与 `host_shutdown_started()`；`register_active` 在关闭后拒绝登记。
  - `decision_service.py`：登记被拒且宿主关闭中时返回 `stale/host_shutdown`；设置撤销与关闭撤销合并为 `_revoked`，设置撤销优先。
  - `cli/gateway_process.py::_cmd_gateway_run_cleanup`：置位停止事件后调用取消原语，用 try/except 包住，出错只记异常类型。
- **新测试** `test_gateway_decision_shutdown_cancel.py` 7 项（真实本地 HTTP 与原账本）：
  - 在途决策被取消后 0.8 秒内返回 `stale/host_shutdown`，调用账 failed 1、无 running；
  - 不新增冷却，复位后同一连接照常成功；关闭后新决策不联网；设置撤销与关闭同时发生时报设置撤销；索引满仍报 `notification_capacity`；
  - Gateway 收尾顺序为停止事件、取消决策、停 HTTP；取消模块抛错时收尾照常完成，只记 `{"error_type": "RuntimeError"}`。
- **新测试** `test_decision_curator_plugin_concurrency.py` 5 项（后台 `curator` 与前台 `skill_tool` 共用同一连接，本地服务按 lane 选择性阻塞）：
  - 后台慢时前台照常完成，两边各记一条原账；
  - 线程变更只撤销前台；owner 级改 `curator` 只提前撤销后台，前台返回后按整份策略版本作废为 `policy_changed`；
  - 后台超时后同连接前台返回 `cooldown/connection_backoff` 且不发请求；关闭时两者一起取消。
- 两个文件连跑 6 次 12/12 通过。
- **变异验证**：14 种各自使新测试失败（不置关闭标记、登记不看标记、不标记或不取消句柄、计数错、`_revoked` 忽略关闭、关闭优先于设置撤销、登记失败不区分关闭与容量两个方向、登记被拒后照常发送、收尾不调用/不包 try/记异常正文/在停 HTTP 之后才调用）。子进程带 `PYTHONDONTWRITEBYTECODE=1`，结束后还原原文件。
- **回归**（基于 main `6c3ffc2ad`）：全部 `test_decision_*`、引用 `gateway_process`/`decision_policy` 的测试与架构守卫共 63 个文件，1428 passed、2 skipped、3 xfailed、1 failed。
  失败的是 `test_subagent_first_request_selection.py::test_real_child_first_request_capture_matches_actual_provider_payload` 的第一组参数。它在不含本改动的 main 上同一组合里同样失败，单独或整文件运行 40/40 通过。二分表明不是单个前置文件触发，要前面约 29 个决策测试文件叠加才出现。根因后来查明是线程中断标志按 ident 复用，已由上一节的修复（main `761ef2ab2`）解决。

## 子代理 lesson 结构化来源 `record_lesson`（2026-09-25，已合入 main `52e0190e1`；已端到端真实验收）

- **改动**：
  - 新增账本合同 `subagents/lesson_ledger.py` 与子代理专属工具 `agent_core/runtime/record_lesson_tool.py`。
  - 路径登记在 `AgentRunWorkspacePaths.lessons_jsonl` 与 `SubAgentTask.agent_run_lessons_jsonl`。
  - coding/read_only 预设、`ROLE_BASE_TOOLS` 和层级缺省候选都带上该工具，`_DEFAULT_HIDDEN_TOOL_NAMES` 对主线程隐藏它。
  - `runner_result_service` 读回账本合并进 `lessons`；没有结构化输出时也记账本候选。
  - `memory_candidates` 为账本条目生成 `subagent_lesson` 候选：`applies_when` 取 `when_to_use`，证据引用账本条目。
  - runner 提示在有授权时多一条可选软引导。设计见 DESIGN_LEDGER 同名条目。
- **新测试** `test_subagent_lesson_ledger.py` 31 项，只用假件，不调 provider：
  - 工具：身份只取 runner 上下文，参数里伪造的 run/task/attempt 被忽略。主线程、未知 run、没有 manager 时都返回 `TOOL_UNAVAILABLE` 与 `not_started`，也不建账本。Schema 只有四个有界必填字段，`additionalProperties=false`。
  - 工具（续）：缺失、空白、非字符串、超长逐项结构化拒绝，边界值通过；空白规范成单行，渲染固定四行。同参数（含空白变体）只记一次。第 6 条报 `LESSON_LIMIT_REACHED`，重试已记条目仍按幂等返回。两条满长中文之后第 3 条报 `LESSON_LEDGER_BYTES_EXCEEDED` 且不写入。坏行与符号链接都 fail-closed，外部文件不变；底层追加使用 O_NOFOLLOW。
  - 读回：篡改、他 run、非规范、错版本（含 `true`）、重复与坏 JSON 行全部拒绝并计数。超过 5 条只取前 5 条，超字节整本不采用，加锁失败报 `unreadable` 且不阻断交付。
  - 收口：自然回复（没有结构化输出）时，`output.json` 与 runner result 的 `lessons`/`lesson_count` 含账本经验。结构化 lessons 在前、账本在后，按文本去重。没有账本时输出与工作日志保持原样；dry-run 不记候选。
  - 候选与提案：账本经验成为 `pending_review` 的 `subagent_lesson` 候选，带 task/run 来源，`applies_when` 取 `when_to_use`，证据引用 `lessons.jsonl#<lesson_id>` 并带 task/run/attempt。开启自学习时每条经验一个待确认提案；重放同一结果后候选 occurrence 仍为 1，提案文件不变。关闭自学习时只有候选。候选服务抛错时结果照常交付，工作日志记 `memory_candidates_error`。
  - 暴露与提示：coding/read_only 预设、角色默认和层级缺省候选都含该工具，并受父级上界约束；后台主代理默认目录不含。真实 SimpleAgent 注册表里，主线程快照、tool_search、list_tools 都看不到它，子代理快照可见；关闭子代理时不注册。提示只在有授权时多一条可选引导，并保留"不要输出 SUBAGENT_RESULT、状态 JSON"。
- **变异验证**：24 种全部被杀死。覆盖身份、不可用、条数/字节/字段上限、幂等、读回 id 与 run 归属、合并与去重、自然回复记候选、适用场景、账本引用、主线程隐藏、预设暴露、提示条件、候选失败隔离、符号链接与 O_NOFOLLOW、注册、`not_started`、重复纯文本、dry-run、读回异常。
  - 首轮有 2 种存活：他 run 行与合法行同 id，被去重先挡住；符号链接目标是坏 JSON，被坏行判定先挡住。补强测试后两者都被杀死。
  - 每种变异在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行，逐字节还原并用 sha256 核对；全部完成后删除 `__pycache__`，从干净字节码复跑。
- **收集 focused 文件时必须排除 `agent_py_agent/tests/run_tests.py`**：它在 import 阶段就执行真实 CLI 冒烟，包括一次真实 provider 请求，且 owner home 仍是 live 目录（conftest 的隔离夹具只作用于测试函数）。本轮按名字 grep 时误收过一次，影响见交接。
- **结果**（基于 main `019dd0dcd`）：新测试 31 passed。focused 共 262 个文件，按名字 grep 出引用改动面的全部 pytest 文件，排除 `run_tests.py`，另加 `test_architecture_guardrails.py`。合计 5001 passed、3 skipped、28 xfailed、4 xpassed。xfail/xpass 集合与 origin/main 相同，均为既有 EXEC-31b/AUDIT-02 标记。无需修改任何既有测试。
  - 其余门：Ruff、doc sync、import boundaries 通过；strict code-size blocked=False，与 origin/main 按 identity+severity 逐项比对无新增；`git diff --check` 与 clean-package 通过。产品代码与新测试都没有 `*args/**kwargs` 形参。线上 CI 没有作为验收来源。
- **未验**：没有真实 TUI 验收；被取消 run 的账本不经结果收口；主线程经验记录不在本片范围。

## 决策实验对照记录、证据评估与授权内自动晋升（2026-09-25，本地分支 `claude/decision-experiment-records`，待审）

- **改动**：只观察实验调用经原账结算后，结算视图随 `DecisionOutcome.experiment` 带回，生成 `decision_experiment_record.v1`（身份、配置版本、基线/候选名单、结算）写进 Gateway 请求记录 `experiment_records`；回合正常收尾按结构化工具账补写实际用量；只读评估（≥3 可比较、全部 charged、召回 1.0、有节省）沿授权回执指针回读原请求记录，经 `user_config decision_read` 暴露；`/experiment apply` 授权内经原设置 CAS 一次性晋升 `points.skill_tool.mode`，回执写在请求记录。
- **新测试** `test_decision_experiment_records.py` 27 项：复用 `test_decision_experiment_send_gate.py` 的本地 HTTP `lab` 夹具，断言记录结算字段逐项等于原账快照、record_id 等于原调用编号、基线等于原 Registry 实际展示、候选短名单/延迟名单、无用户正文/候选说明/模型回答/端点；未进入原账预留不写记录、普通决策无条目；真实请求文件与回合转换锁下的观察拆分与去重、8 条上界、关闭/停止/换代次时抛中断且不写、写盘失败吞掉；收尾实际用量来自工具账、6 类未知（未完成、无账、空名、非字符串名、非字典、混入坏记录）、只补写本执行代次、普通收尾零 I/O、停止收尾不补写。
- **新测试** `test_decision_experiment_evaluation.py` 33 项：规则矩阵（样本不足、召回<1 两种、零节省、四种非 charged）、六类不可比较样本不计不阻断、快照外工具不稀释召回、外来 owner/线程/点/schema/未完成/空编号忽略、去重与 8 条窗口、证据链跨四个目录按新到旧、越界/隐藏/缺失指针终止且加载器自身拒绝越界编号、跨会话/成环/16 条上限；`user_config decision_read` 暴露只读评估且紧跟授权信封、无授权时输出不变、读取失败给 `unavailable`。
- **新测试** `test_decision_experiment_promotion.py` 18 项：真实设置服务与真实 E1 授权入口；apply 语法与回执 v2 指针、一次 CAS 晋升（revision 只前进一次、前后值与证据编号）、重放与重启不二次晋升、崩溃遗留 promoting 不重试（内存与锁内两种）、observe 授权永不晋升但读路径给出建议、锁内读后用户改另一字段则 `settings_conflict` 且保留用户值、线程/用户层/默认配置三种后改跳过、撤销/到期/被替换跳过、四种弱证据（单样本、召回<1、usage_unknown、外来 owner）即使 Jev 候选完美也不晋升、被拒授权与模型工具均无授权/晋升路径。
- **新测试** `test_decision_experiment_gateway_turn.py` 4 项：真实 `_run_gateway_ask`（原 SimpleAgent.run/设置/Registry/RuntimeDB/请求文件），主模型为 typed 假答复并发起一次真实原生工具调用，Jev 连本地 HTTP：一次实验一条记录、结算 charged、实际工具名来自真实工具账；普通回合不写任何实验键；两条历史授权链上样本加本轮样本时 apply 晋升、observe 不晋升。
- **改动的原有测试**：`test_decision_experiment_command.py` 把 apply 从无效用例移出（改用 `promote`/`Apply`/`["apply"]` 等无效形态）；`test_model_call_input_budget.py` 断言结算视图额外的结算码/调用编号/估算/上界字段，快照部分与原值相同。
- **结果**：相关 10 文件从干净字节码 246 passed；全部 `test_decision_*.py` 与 `test_gateway_*.py` 90 文件（变基到 `1132fd9d0` 后，含主线新增的两个 S2 文件）2196 passed、2 skipped；相邻 20 文件 604 passed；`check_import_boundaries.py` 0 findings。变异 37 项 36 项被杀（清单与唯一等价变异说明见 [E1 交接第三片](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)），每项 `PYTHONDONTWRITEBYTECODE=1` 子进程运行、sha256 原样恢复。未启动 Gateway、未调用真实供应商；真实 `/experiment apply` 验收待做。

## 自学习 S2：待确认 Skill 提案审核顺序 `skill_proposal_review`（2026-09-24，已合入 main `1132fd9d0`）

- **改动**：新增 `capability/decision_skill_proposal_review.py`；`POINT_RUNTIME_SCOPES` 登记 `skill_proposal_review: owner_background`；AgentConfig/YAML 三字段默认 off/null/null；TUI 决策菜单加“Skill 提案审核顺序（用户长期）”；`skills proposals list` 在点返回采用结果时才重排展示、加标签和 `review_order` 块。设计见[接入设计 P5-C 自学习 S2](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-自学习-s2待确认-skill-提案的审核顺序-skill_proposal_review)。
- **新测试** `test_decision_skill_proposal_review.py` 42 项。用真实 S1 提案，只替换决策服务三个边界；替身签名与原服务的显式关键字参数一致。
  - 资格：0/1 条待确认、31 条、点未登记都不建阶段；2 条和 30 条各一次请求；已确认/已拒绝的提案不计数，位置也不动。
  - 关闭：阶段错误、点关闭、只开了别的点时不准备材料。
  - 绑定：阶段为 owner_background、操作编号绑定待确认集合、每次新 run 编号且无 thread，source_refs 精确。
  - 采用：observe 保留原序；apply 按 优先→普通→稍后/可能重复 分组，组内保持原序，标签和 `review_order` 块正确；全 normal 顺序不变、无标签。
  - 回答：6 种坏回答（缺题、多答、错题号、逐题错误、错类型、越界值）与 3 种非选择都保留原序；候选版本不符、配置复核失败、超期也保留原序。
  - 等待期间变化：6 种（被拒绝、版本号变、记录 hash 变、正文变而 hash 未变、新增待确认、文件损坏）都保留原序；正文被改而 hash 未更新的草稿不发请求。
  - 隐私：外发不含提案/候选/任务/运行编号、目标 Skill 名、路径或密钥，含 3 段不可信数据边界和 `<redacted>`；题目只有 7 个固定候选且说明“不决定确认或拒绝”；经验摘录与 S1 模板一致、不含来源段、按 240 字截断。
  - 零写入与取消：确认/拒绝/写入入口被替换成失败也不触发，提案、skills、候选账本逐字节不变；apply 与 observe 下等待期间的取消、配置复核中的取消都上抛；决策边界的 InterruptedError/ToolCancelled 上抛；普通错误回原序，但不掩盖同时发生的取消。
- **新测试** `test_decision_skill_proposal_review_integration.py` 19 项。真实 CLI、owner 解析、设置与模型目录、决策服务、worker、响应解析、冷却表和调用账，只替换 `post_json`（窗口门与请求头照常执行）。
  - 字节：点关闭、总开关关闭、1 条、0 条时零请求，`list` 与 `--json` 输出和按 S1 格式重建的期望逐字节相同。
  - observe：两次调用各一次请求，各有一条 purpose=decision、finished 的调用账，输出不变。
  - apply：`--json` 重排和 `review_order` 块、中文说明行与四种标签正确。
  - 真实请求：只含 state/questions/model，别名化、已脱敏、无编号和路径。
  - 失败路径：请求期间提案被拒绝、4 种真实非选择或越界回答、供应商错误后同进程冷却（第二次零请求）、0.2 秒超时都输出原列表。
  - 中断：命令以 `SKILL_PROPOSAL_CLI_INTERRUPTEDERROR` 失败，不打印列表。
  - 容量：30 条最长草稿通过真实窗口门并完成重排。
  - 设置与菜单：YAML/dataclass 默认 off；字段只允许 owner，线程写入报“作用范围”；超时继承后台期限；TUI 用户长期菜单可设 apply，线程菜单不显示本点。
- **一次性对照（未提交）**：在同一临时 home 上分别运行 origin/main（`d9a34fc76` 与变基后的 `4b9d684ea` 各一次）与本分支的 CLI 子进程，执行 `list`、`list --json`、`list --status pending_confirmation`。默认配置、点关闭、总开关关闭、1 条、0 条五种情形的输出逐字节相同。
- **变异验证**：38 种变异全部被杀死。涉及：
  - 资格：点登记、上下界 2/30、只计待确认。
  - 阶段：错误与关闭、observe 被采用、scope 改 thread、run 编号为空。
  - 采用前复核：候选版本、配置复核、重读提案、期限、重算草稿 hash、篡改拒发。
  - 材料与隐私：不可信边界、脱敏、摘录带来源段、摘录上限、别名换成真实编号。
  - 回答与排序：回答数量/逐题错误/类型、可能重复分组、标签、排序、已处理条目位置。
  - 取消：入口吞中断、可选错误掩盖取消、决策后不查取消。
  - CLI 与配置：不调用点、丢 `review_order`、丢标签、不重排；schema 改线程范围、dataclass/YAML 默认 apply、TUI 缺显示名。
  每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行，逐字节还原并用 sha256 核对；全部完成后删除被变异模块的 `__pycache__`，从干净字节码复跑 85 passed（含 S1 的 24 项）。
- **结果**（变基到 main `4b9d684ea` 后）：与改动直接相关的 56 个测试文件加 `test_architecture_guardrails.py` 共 1428 passed（决策全部测试、S1、TUI 菜单、外部材料组合、CLI 解析/参考/配置、配置校验、user_config、Gateway 选模观察/采用、打包、架构守卫）。本分支产品代码和新测试都没有 `*args/**kwargs` 形参。Ruff、doc sync、import boundaries、strict code-size（blocked=False；与 origin/main 按 identity+severity 逐项比对无新增）、`git diff --check`、clean-package 通过；线上 CI 未作为验收来源。
- **未验**：没有真实 Jev 或真实 CLI 验收，不证明审核顺序对用户有用；CLI 的决策调用账只在进程内，不进入会话展示或持久账本。

## 决策线收尾的全仓回归（2026-09-25，main `5fca6a194`）

- **结果**：`python -m pytest agent_py_agent/tests -p no:cacheprovider` 共 24 分 19 秒，21,406 passed、4 failed、21 skipped、32 xfailed、5 xpassed。
- **4 个失败逐一单独复跑归因**：
  - `test_architecture_guardrails.py::test_product_code_has_no_var_keyword_service_interfaces`：第 17 项实验授权回执 `_receipt(**fields)` 违反"产品代码不用 `**kwargs` 服务接口"。已在本分支改为显式关键字参数，守卫与实验 3 文件合计 118 passed。
  - `test_plugin_workspace_context.py` 中两项：单独运行稳定失败，报 `'MCPStdioClient' object has no attribute 'activation_ref'`。来源是插件调用前的激活复核（`58bb0441a`）；测试替身没有该属性。属插件线，已告知主线 owner，本分支不改。
  - `test_subagent_capability_compact.py::test_child_same_turn_reuses_selection_and_exact_provider_surface[None]`：单独运行 3/3 通过，判为全仓负载下的时序偶发。
- 按 AGENTS.md 的频率约定，全仓回归只在决策线收尾时跑这一次；各分支远端提交前的严格门仍以 focused tests 为准。

## 验证分类：返回码 126/127、pytest 范围与 && 串联（2026-09-25，本地分支 `claude/verification-exit-scope-chains`）

- **新测试** `test_verification_project_facts.py`：
  - 返回码 126/127 记 `environment_unavailable`，普通非零仍是 failed。
  - pytest 10 种参数形状：无参数、目录、子目录为 full；文件、`::node`、`-k`、`-kEXPR`、`-m`、`--lf`、`--deselect=` 为 targeted。
  - `make test && make lint` 返回 0 时两段都记 passed；带 cd 前缀时两段的 cwd 都是 cd 目标；`make test && echo done` 只记测试。
  - 返回 2、使用 `;` 或 `||`、串联后接管道时都不记。
- **新测试** `test_verification_runtime.py`：真实 `record_tool_verification` 为通过的串联落两条证据，信封 `verification_evidence` 为末条、`verification_evidence_chain` 为完整有序列表。
- **新测试** `test_decision_delivery_quality.py`：串联前面几段的事件同样成为焦点；串联与末项不一致时整点放弃、零请求。
- **原有测试**：分类测试改走新的列表接口；原"cd 后接 `&& echo done`"的拒绝用例按新规则移到串联测试里（返回 0 时证明测试通过）。
- **变异验证**：9 种变异各自使测试失败——126/127 记 failed、pytest 回退旧范围规则、不识别 `-kEXPR` 连写、不识别筛选开关、串联非零仍记、放行 `;`、不附串联字段、交付复核忽略串联、交付复核不核对末项一致。"交付复核忽略串联"最初只被不一致用例杀死；把正例的前段改为只在串联里出现的 `build` 后，正例也能杀死。还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：引用验证账、分类器、运行事实或归档投影的 17 个测试文件 345 passed、4 xpassed。4 个 xpass 都在 `test_tool_unresolved_runtime_issue_guard.py`，main 上同样出现。

## 决策实验授权入口、经验输入上界与发送硬门（2026-09-24，本地分支 `claude/decision-experiment-send-gate`，待审）

- **改动**：`/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>` 冻结参数、Gateway 主轮绑定后首个模型调用前单次授权；信封 v2 必带 `input_bound_policy="empirical:jev_wire_bytes.v1"`；经验上界 `C = ceil(B/2) + 256×Q + 1024`（只在 skill_tool、Q≤64、state≤4096 字节、C≤57,600 内）；原账只收带 `kind=empirical` 的上界对象并签发单次发送许可；传输层在最终字节生成后、DNS/连接/遥测前复核许可；终态后保守结算。
- **新测试** `test_decision_experiment_send_gate.py` 39 项，复用 `test_decision_capability_http.py` 的 `capability_http`/`surface` 夹具，在本地服务 `verify_request` 处统计 TCP accept、在服务端记录原始字节：9 类缺授权/缺预算（无授权、能力关、撤销、到期、账本代次变化、HTTP 用尽、C 超剩余、越标定两种、v1 信封）零连接且无调用记录；5 类预留后绕过（撤销、换密钥、篡改正文、篡改端点、阶段身份改变）加抛错遥测均 `ProviderSendRefused`、零连接、预算 `send_refused`、不进连接退避；默认关闭只建一次普通阶段、不建实验阶段、零连接无调用记录；授权发送单连接单 POST、服务端正文 sha256 等于预留摘要、`reserved_http=1`、`charged=provider=X`、上界 kind empirical、结果不采用且 prompt/schema 不变、能力推荐观测为 observe/未采用、第二次 `http_budget_exhausted` 不再连接；X=C+1 关闭为 `input_bound_violated`；挂起到期 `timed_out` 保留整个 C、迟到响应不重开；另有许可静态绑定/运行态顺序和 C 的样本余量、单调、越界与边界单测。`test_decision_experiment_command.py` 34 项：命令解析与冻结、HTTP 入口丢弃客户端 system_task、单次授权与提示、重放/重启/Compact 再入不再授权、拒绝与关闭回合、核心钩子时序。
- **扩展**：`test_model_call_input_budget.py`（带标签上界、单次许可、拒绝与绕过结算）38 项、`test_decision_experiment_authorization.py`（v2 口径、只发布普通模式 off 的点、改写旧“实验直连总拒绝”用例）35 项、`test_gateway_strict_request.py`（许可信封约束、拒绝在遥测与连接之前、普通请求字节不变）54 项。
- **结果**：五文件从干净字节码 200 passed；定向相关集 118 文件 3421 passed、2 skipped、4 xfailed、1 xpassed（基于 `55f72b40c`）；变异 36 项全部被杀（清单见 [E1 交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)）。未启动 Gateway、未调用真实供应商；首次真实授权发送仍待做。

## 验证命令分类：cd 前缀与管道（2026-09-25，本地分支 `claude/verification-command-shapes`）

- **新测试** `test_verification_project_facts.py`：
  - `cd <绝对路径> && python3 -m pytest …` 与 `cd "相对目录" && pytest` 都按 cd 目标归类，记录的 cwd 与项目根为该目录。
  - cd 到不存在的目录、cd 后再接多段、`cd …;`、`cd … ||` 都不算证据。
  - 管道、`|&`、后台 `&`，以及 cd 前缀后接管道，都不算证据；引号内的 `|` 仍能归类。
- **新测试** `test_verification_runtime.py`：真实 `record_tool_verification` 把 `cd` 前缀的失败测试记在 cd 目标项目下。
- **变异验证**：去掉管道检查、关闭 cd 剥离、去掉目录存在检查、记录原 cwd、放行后台 `&`、cd 前缀放行 `;`、项目根按原 cwd 查找，7 种变异各自使新测试失败。其中"去掉目录存在检查"最初未被杀死：测试用的缺失目录在项目外，本来就找不到项目。改为项目内的缺失子目录后被杀死。还原后逐字节一致（`PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：引用验证账或分类器的 8 个测试文件 193 passed，含交付复核焦点的单元与组合测试。

## TUI 决策菜单接入点跟随 schema（2026-09-25，本地分支 `claude/decision-tui-points`）

- **新测试** `test_tui_decision_menu.py::test_menu_points_follow_the_schema_registry_and_reset_can_list_pre_recall`：菜单接入点与 schema `POINTS` 逐项一致；owner 覆盖 `points.pre_recall.mode` 后，该字段可编辑，恢复继承标签不崩溃并显示中文名。
- 两个旧真实按键测试原先写死"召回后重排在第 4 项"，现改为按菜单顺序算位置。
- **变异验证**：菜单漏掉 `pre_recall`、缺中文显示名两种变异都使新测试失败；还原后逐字节一致（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **回归**：`test_tui_decision_menu.py` 与 `test_external_material_order_integration.py` 26 passed。

## 交付复核焦点 `delivery_quality`（2026-09-24，本地分支 `claude/decision-delivery-quality`）

- **改动**：新增 `tool_context/decision_delivery_quality.py`；`_tool_loop_service._record_tool_call` 的原阅读提示接缝改为 `_optional_result_hints`，依次调用 `external_material_order_hint` 与 `delivery_quality_hint`（按工具名互斥）；`POINT_RUNTIME_SCOPES` 登记 `delivery_quality: thread`；AgentConfig/YAML 三字段默认 off/null/null；TUI 决策菜单加“交付复核焦点”。设计见[接入设计 P5-C 质量提示首片](docs/design/DECISION_MODEL_INTEGRATION.md#p5-c-质量提示首片交付复核焦点-delivery_quality)。
- **新测试** `test_decision_delivery_quality.py` 102 项（只替换决策服务边界）：未登记严格空操作；off/阶段错误/他 run 阶段不准备材料；observe 与 deadline/cooldown/error/stale 保留原结果；apply 只渲染被选焦点的宿主事实（含其后修改、targeted 说明）；外发只有脱敏请求与焦点别名事实（无路径/命令/输出/时间/改动路径），不同项目根得到不同别名；28 种不合格来源零请求（单焦点、全通过、13 个焦点、非 run_command、归档 id/run/task/scoped/事件不一致、未归档、重复归档、他 run/task 来源、两种收口标记、超长/空请求、无事件、重放、重复事件号、坏 id/kind/退出码/root、坏 state、非 stale 状态、请求含两种 URL 查询串）；12 种身份不符在扫描归档前返回；子代理零请求；2/12 个焦点、仅修改、仅失败的边界各一次请求；同 project/kind/scope 只留最新事件；11 种非选择或坏答案（四种保留项、两种越界编号、多答、逐题错误、错类型、错题号、缺答）无提示；选中本次事件无提示；9 种等待期间来源/参数/收口变化、期限/候选版本/配置失效、配置复核期间来源变化与超期消费均丢弃；4 种可选错误保留原结果；取消在决策后、配置复核中和与可选错误同时发生时都上抛，中断上抛；原 `_record_tool_call` 下 off/observe/apply 的 text/native/IR 同一展示与原结果/归档不变；run_command 记录只触发本点一次请求。
- **新测试** `test_decision_delivery_quality_integration.py` 5 项：真实 `record_tool_verification`（局部测试失败→改文件→全量测试失败）逐步经原 `_record_tool_call`，原设置/模型目录/worker/调用账，只替换 `TypesafeDecisionBackend.decide`；off/observe/apply 下 text/native/IR 同一段、apply 恰好一段提示、决策请求与 purpose=decision 账各 0/1/1 条、请求绑定本 run/thread/归档引用且不含私有路径/命令/输出、决策时刻与结束时 owner 根文件字节及归档列表不变；YAML/dataclass 默认与 owner/thread 范围；原 TUI 菜单可把本会话模式改为采用建议。
- **变异验证**：59 种各自使新测试失败，改回后按 sha256 核对原文件：接线丢点/丢换行、未登记不空操作、非 run_command 扫描归档、接受重放/子代理/两种收口/超长请求/无请求、去掉归档事件/id/run/task/scoped 一致、焦点下限 2→1、上限 12→13 与 12→11、不要求当前事件、去掉或只看 failed/只看修改、接受重复事件号、保留最早而非最新、修改方向反转、修改不比项目根、非 stale 算修改、去掉短标识与 id 校验、扫描不按 run/task 过滤、外发项目根、请求不脱敏、不拒 URL 查询串、忽略阶段错误/关闭/他 run、observe 被采用、不查候选版本/当前配置/最终来源/参数版本/绝对期限、接受多答/错题号/错类型/逐题错误、渲染当前事件、提示丢修改事实/targeted 说明、去掉 512 字符预算、决策后与配置复核后不查取消、中断被吞、可选错误掩盖取消、点未登记、TUI 缺菜单、dataclass/YAML 默认非 off。每次均以 `PYTHONDONTWRITEBYTECODE=1` 运行；全部完成后删除被变异模块的 `__pycache__` 并从干净字节码重跑 107 passed。
- **结果**（基于 main `ab23a2666`）：与改动直接相关的 56 个测试文件 1443 passed、1 xpassed（`test_timeout_budget_locked.py::test_native_protocol_unified_counts_ir` 为既有非严格 xfail，main 上同样 xpass）。同组合共跑 8 次，第 1 次出现 1 failed，因输出被截断未记下用例名，其后 7 次全量均通过，新文件单独 25 次、时序敏感的 10 个既有文件 5 次也均通过，未能复现，暂按机器负载下的偶发记录；Ruff、doc sync、strict code-size（blocked=False；与 main 的 findings 按 identity+severity 逐项对比无新增，`_record_tool_call` 的临界长度项因接缝抽出而消失）、`git diff --check`、clean-package 通过。没有真实 Jev、真实主模型或 TUI 验收，不证明交付质量提升；线上 CI 未作为验收来源。

## 召回与记忆决策点的真实验收方法（2026-09-25，本地分支 `claude/decision-p5a-real`，只改文档）

- **已知漏召回样本先离线标定**：测试者用产品原函数（BM25 顺序、均值中心化余弦 ≥0.30、RRF、原片段生成函数）和同一嵌入类复算限定范围的混合检索，只挑"整句召回不到、某个片段能召回、基线留有空槽"的问题做 off/apply 对照，避免拿不可能有收益的样本下结论。标定只输出编号、条数和相似度，密钥在进程内读取、不打印。
- **旁观器只记结构化选择**：召回前决策点只记录所选片段 ID，关系点只记录每对的 ID 与关系标签，嵌入调用只记条数、耗时与成败；不保存问题、片段、记忆正文、密钥或请求头。
- **同一快照在原路径上重放**：Curator 对照必须从同一快照在原 home 路径上依次跑 off 与 apply。会话工作区按绝对路径建键，把 home 复制到别的目录会读不到原对话（本次作废的一次运行已留证）。
- 结果与证据见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#14-p5-a-召回前补充查询语义召回下的真实收益2026-09-25main-ab23a2666)。本分支没有代码改动，所以不跑 pytest；doc sync 与 diff 检查照常执行。

## 自学习 S1：子代理经验生成待确认的 Skill 提案（2026-09-24，本地分支 `claude/self-learning-skill-proposals`，待审）

- **改动**：新增 `capability/skill_proposals.py`（提案 schema `my-agent.skill-proposal.v1` 与 `SkillProposalService`）和 CLI `my-agent skills proposals list|show|confirm|reject`；配置 `enable_self_learning`（默认 false，YAML、AgentConfig 与布尔规范化同步）；owner 布局登记 `owner_skill_proposals_dir = <owner_home>/data/skill_proposals`（不进初始化目录清单，首次生成提案才创建）；组合根只在开关开启时给子代理 manager 注入服务，`runner_result_service` 在记录候选之后调用，异常只写工作日志。
- **新测试** `test_skill_proposals.py` 24 项：
  - 开关：随包 YAML 与 dataclass 默认 false，带引号的 "false" 规范化为布尔；默认关闭的 SimpleAgent 不注入服务、不建目录；开启后服务路径落在飞书用户 owner 自己的 `data/skill_proposals` 与 `skills`。
  - 生成：一条 lesson 恰好一个提案，同 run 重放和第二个 run 合并都不重复；提案含来源任务/运行、触发原因、拟保存内容和适用场景，ID 等于 sha256(candidate_id + content_hash) 前 24 位；finding、`model_inferred` 自省 lesson、无 task/run 来源、类型不是 lesson 的候选以及非 Candidate 对象都被忽略，且不建目录。
  - 迁移：真实 `MemoryMigrationService.apply()` 迁走并删除同级 `data/learning_drafts`，提案目录逐字节不变、提案仍可列出。
  - 确认拒绝矩阵 7 种：旧版本号、目标已存在、候选正文被改写、候选脱敏、候选被拒绝、候选已删除、草稿被篡改，各返回对应错误码，提案文件与目标逐字节不变、暂存目录清空；guard `caution`（chmod 777）被拒；`os.replace` 失败与写回执失败都不留下目标 Skill。
  - 成功路径：确认后 revision 2、`committed`、回执 guard `safe`；确认前取得的快照看不到新 Skill，下一次快照出现 `owner:lesson-*`，其 content_sha256 等于草稿 sha256 且正文可读；已提交后再确认返回 `NOT_PENDING`。
  - frontmatter：含 `#`、引号和 CRLF 的经验文本经 `parse_skill_file` 解析后 name/description/when_to_use 与草稿一致；非法 ID、不存在、损坏文件分别返回 `INVALID_ID`/`NOT_FOUND`/`CORRUPT`，list 遇损坏文件关闭式失败。
  - runner：提案服务抛异常时结果仍 DONE、候选照记、工作日志记 `skill_proposals_error=RuntimeError`；接上服务时生成一条待确认提案；未接时只记候选、不建目录。
  - CLI：经真实 `cli.parser.main` 与临时配置完成 list→show→confirm→reject→按状态 list→重复 confirm（退出码 1、`NOT_PENDING`）；中文输出含来源、触发原因、正文、场景和带版本号的确认命令。
- **变异验证**：26 种变异全部被杀死——资格三项（来源、task/run、类型）、O_EXCL 改 O_TRUNC、跳过版本/待确认/目标存在/来源 hash/脱敏/状态/缺失检查、guard 改 force、跳过草稿 hash、不回滚已装目标、不清暂存目录、不替换 `#`、不规范换行、runner 放任异常/不调用、组合根忽略开关、owner 投影沿用本地主用户目录、目录改名 `learning_drafts`（迁移测试失败）、开关不做布尔规范化、CLI confirm 调成 reject、CLI 退出码恒 0、YAML 默认改 true。每次在 `PYTHONDONTWRITEBYTECODE=1` 子进程运行并逐字节还原；之后删除被变异模块的 `__pycache__`，从干净字节码复跑 24 passed。
- **回归**（基点 main `d3d66e56c`）：runner 结果、候选、Skill/guard、Memory 迁移、CLI 解析、owner 布局、配置与组合根相关 136 个测试文件 2299 passed、3 skipped、6 xfailed。ruff、doc sync、strict code-size（blocked=False；与 origin/main 逐项比对 finding 身份和级别无新增）、`git diff --check`、clean-package 通过；线上 CI 未作为验收来源。
- **未验**：没有真实 TUI 验收（真实子代理产出 lesson→用户确认→新回合读到 Skill）；S2（Jev 对待确认提案的审核排序）未做；并发确认只由 owner 文件锁保证，未做多进程压测。

## 召回证据写进上下文包（2026-09-24，本地分支 `claude/decision-recall-evidence`）

- **新测试** `test_decision_pre_recall.py` 2 项：正式召回只把补充查询真正追加的记录编号记进 `supplement_entry_ids`，来源清单区分 baseline/supplement 且不含正文；上下文包写出 `recalled_refs`、`recall_findings`，提示段字节与不带证据时逐字相同。
- **变异验证**：4 种各自使测试失败——不记补充编号、来源恒为 baseline、丢发现码、提示段泄露来源清单。改回后通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **相关回归**：上下文包、召回决策、记忆路由、运行上下文相关 15 个文件 314 passed。

## 主会话选模采用模式的问题说明（2026-09-24，本地分支 `claude/decision-selection-question`）

- **新测试** `test_gateway_model_observation.py::test_question_explains_usage_tags_and_apply_asks_for_the_best_semantic_match`（observe/apply 两档）：从冻结的决策请求体读回问题说明，两种模式都含用途标签说明；采用模式要求按任务语义挑最合适的候选且不再写"本次只观察"，观察模式保留"本次只观察"。
- **变异验证**：采用模式说明改回旧文、观察模式丢掉用途标签说明，各使一档失败。改回后通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **相关回归**：Gateway 选模观察、Gateway 采用、模型用途标签 3 个文件 81 passed。
- **真实验收**：同一 325k 字任务，修正前 Jev 仍选当前 M2.7（答案错误），修正后 Jev 选 M3、宿主核对后自动采用、答案正确；见[主会话真实交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。

## 模型用途标签（2026-09-24，本地分支 `claude/decision-usage-tags`）

- **新测试** `test_model_usage_tags.py` 12 项：
  - 快捷新增的标签去重、排序（全角逗号也能分隔）、持久化并出现在公开模型列表；没填不写键。
  - 大写、连字符、数字开头、超过 40 字、非字符串、字典、超过 16 个都拒绝，且不落盘；决策模型带标签被拒。
  - 标签不进 `resolved_model`，选定带标签的模型后运行时配置正常、没有标签属性。
  - 主会话与子代理的决策候选只在填写了时带 `usage_tags`。
  - `/model` 编辑表单预填并原样回存标签，编辑其它字段不会丢。
- **变异验证**：7 种各自使测试失败——校验结果丢标签、快捷新增白名单漏标签、决策模型可带标签、允许大写、主会话候选不带、子代理候选不带、表单不发送。改回后全部通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。
- **相关回归**：模型目录、服务商与采样、共享目录、决策全部、Gateway 选模观察与采用、子代理首请求选模、TUI 模型与决策菜单共 63 个文件 1399 passed。
## 能力推荐观测写进 Gateway 请求记录（2026-09-24，本地分支 `claude/decision-capability-observation`）

- **新测试** `test_capability_presentation_observation.py` 6 项，走真实 Gateway 回合（只替换模型生成与决策后端）：
  - 采用与保留（abstain）两档：同一回合经过溢出重试仍只有一次决策、一条观测；字段只在白名单内，不含用户正文；采用时有短名单/延迟名单与 Skill 计数，保留时写 `retain_reason`；文件与内存请求一致，携带的展示值不落盘。
  - 决策失败记 `status=error, reason=provider_failed`；接入点关闭时没有观测、没有决策请求。
  - 写入器：保留其它键、最多 8 条、内存同步；回合已换代时抛 InterruptedError；写盘失败只放弃这一条。
- **改写断言**：`test_gateway_capability_compact.py` 原先用子串断言请求记录里没有 `capability_presentation`，新观测键包含该子串；改为按精确键断言"携带的展示值不落盘"，意图不变。
- **变异验证**：8 种各自使测试失败——不调用 observer、采用标记恒为假、丢保留原因、丢 outcome 原因码、不同步内存、不截上限、吞掉回合终结、写盘异常外抛。改回后全部通过（变异子进程带 `PYTHONDONTWRITEBYTECODE=1`）。

## 工具调用审批前/批准后复核（2026-09-24，本地分支 `claude/tool-precheck`）

- **改动**：`BaseTool.precheck_availability()` 默认 None（不复核），`MCPProxyTool` / `PluginProxyTool` 覆盖并给出 `MCP_CONNECTION_CLOSED` / `PLUGIN_ACTIVATION_UNAVAILABLE`；`ToolExecutor.execute` 在 ask 之后、返回 approval_required 之前复核一次，`_execute_authorized` 只对带 `approval_applied` 的裁决在 claim 前再复核一次；复核失败按 `TOOL_UNAVAILABLE` 拒绝、具体码进 `reported_error_code`、文案点明"审批前 / 批准后、执行前"；`ActionPolicy` 只在精确 binding 匹配时在 allow 证据里写 `approval_applied=true`；批准后拦下的结果不再贴"已批准"事实；两码登记进错误分类表；`mcp_managed_process.require` 先核对激活再看进程记录，停用导致的关闭报激活失效（→`TOOL_UNAVAILABLE`），`test_plugin_enable` 的旧期望 `TOOL_EXECUTION_FAILED` 同步改。
- **与设计稿的偏差**：内置工具的 `availability()` 会做 I/O（shell 沙箱探测等），复核改为代理工具 opt-in；`_execute_authorized` 参数已到上限，挂点 2 不加参数、放在函数入口按 `approval_applied` 判断。详见设计稿"实现偏差"。
- **新测试** `test_tool_call_precheck.py` 9 项：审批前拦下不弹框、批准后 claim 前拦下、复核通过则执行且复核恰好两次、内置工具 availability 不被再调、免审批调用不复核、复核异常按不可用、批准后拦下不带 applied_approval、两码登记且不可重试。
- **结果**：相关 59 个测试文件 1187 passed、20 xfailed（含新文件）；ruff 通过。已合入 main `42f7e57e1`，同一 wheel（dcc93407）双机 `runtime-step10m`。`test_host_command_stream::test_disconnect_cancels_only_original_wait_and_replay_cannot_execute` 在 59 文件并跑时偶发一次（rejected 变 outcome_unknown），单独复跑 main 与分支各 3 次均通过，属线程时序偶发，与本片无关，记录待观察。
- **真实 TUI 复验（测试机，默认确认模式，MiniMax-M2.7，2026-09-24）**：workspace-peek 的 tree 是只读工具不弹审批，改用临时安装的 savepoint-lite（save 为写操作）。场景一：模型调用 save 弹出"工具授权"框，另一 TUI 在等待期间 `/plugins disable savepoint-lite`（SUCCEEDED），随后点"允许一次"→ 结果为 `TOOL_UNAVAILABLE`，文案"工具在批准后、执行前复核时已不可用：原插件已停用或激活不可用"，该请求 `tool_operations` 为空、`agent_run.completed` ok、模型如实报告不可用并给出替代建议。场景二：发出请求 1 秒后停用插件，模型的调用到执行器时已被审批前复核拦下，没有弹出审批框，文案为"审批前复核时已不可用"，账上同样零操作。收尾已卸载 savepoint-lite、删除包文件、关闭 TUI。证据在仓库外 `releases/step10-combo3/precheck-acceptance-evidence.json`。
- **待办（测试机，插件生命周期）**：复验前用 `/plugins disable workspace-peek` 制造停用时，两次停用都返回 UNKNOWN（`effect_outcome_unknown:PLUGIN_CLEANUP_UNCONFIRMED`），随后 `/plugins enable` 以 `commit_state=not_committed, reason=activation_unsettled` 失败，插件停在 revoked。测试机上没有残留插件进程，进程会话表里该插件有一条 09-22 的 `status=unknown, stop_requested=true, exit_code=None` 旧记录，疑为清理无法确认的来源。按合同这是"未知不改成成功"，但缺少一条由结构化事实（进程确已不存在）结清旧未知记录的路径，需要另开一片处理；处理前测试机基线只剩 revoked 的 workspace-peek。 **已修（2026-09-24）**：重试停止时若记录已是 unknown、带上一次 cleanup 结果且两级实例按 PID 出生标识均不存在，则结清为 killed；见下文"后台续跑扩展目录与停用结清"，测试机真实复验待部署后进行。

## 插件来源结构化原因（2026-09-24，本地分支 `claude/plugin-source-errors`）

- **现场**：同伴在决策线测试机用 TUI 装 desktop-lite，相对路径失败、绝对路径成功，且报错和"包在 owner 墙外"是同一句"来源不可读、未获授权或格式无效"。核对合同：相对路径按 Gateway 校验过的会话工作区根（客户端随命令附带的 `workspace.cwd`，即 TUI 标题栏显示的目录）解析，不是终端所在目录——这一点本身正确，问题是三种失败混成一句、用户无从判断。
- **改动**：`plugin_sources.PluginSourceError(reason ∈ {not_found, unauthorized, symlink}, source, base)`；`read_plugin_source` 找不到时带出解析基准；安装与更新工具分别回执 `source_not_found`（文案含解析目录与"可改用绝对路径"）、`source_unauthorized`（不回显路径）、`package_<原因>`（格式无效），其余异常保留泛化文案。CLI_REFERENCE 与插件方案写明解析规则。
- **回归**：新增 `test_plugin_source_errors.py` 2 项（原因码、解析基准、链接、越权不回显路径；真实管理链上相对路径安装成功、缺失/坏包分别回执）；与 `test_plugin_management`、`test_plugin_update`、`test_plugin_install_store` 联合 61 passed。

## /plugins update 首片（2026-09-24，本地分支 `claude/plugin-update`）

- **改动**：目录新增 `update <插件> <新包路径>` 管理动作，映射管理工具 `plugin_update`（沿安装权限，模型不可见）。`plugin_update.plan_package_update` 纯计划：同操作重放 → 插件存在 → 新包 ID 一致 → 版本 CAS → 激活已结清 → 包确实不同；`PluginInstallStore.update_package` 在同一 quota→目录锁内保存新包 blob、一次写入安装表：install 回执（版本 +1，配置清空）后，旧配置能按新 `settings_schema` 规范化时紧接 configure 回执（版本 +2）恢复，结果带 `settings_restored/settings_reason`；只复用既有持久动作，安装表无新字段。
- **回归**：新增 `test_plugin_update.py` 4 项（真实临时 wheel：2.0 包替换并保留配置、版本 +2、复读；不兼容 schema 清空配置并报告 incompatible；已启用拒绝 `activation_unsettled`、未安装 `plugin_missing`、同包 `unchanged`；纯计划的版本/身份冲突不落盘）。与 `test_plugin_management`、`test_plugin_enable`、`test_plugin_commands` 联合 73 passed。
- **边界**：不停进程、不切激活，目标必须先 `/plugins disable`；不做双版本准备切换和 rollback；旧包 blob 保留。
- **真实验收（本机，runtime-step10q，2026-09-24 13:26）**：仓内源码临时把 workspace-peek 版本号改成 0.1.2 构建新包（不提交），放在 owner home 下。`/plugins disable workspace-peek` → 已停用并释放；`/plugins update workspace-peek <新包>` → 完成，installations.json 版本 0.1.1→0.1.2、revision 6、last_commit=install（该插件原本无私有配置，`settings_reason=none`）；`/plugins enable workspace-peek` → 完成，phase=active、revision 8；`/plugins@workspace-peek tree mediab --depth 1` 返回结构化目录结果。证据在仓库外 `releases/step10-combo4/plugin-update-acceptance.json`。

## 媒体压缩策略片 C：先看图后总结，让随图摘要在自动压缩里生效（2026-09-24，用户决定第 2 项）

- **根因**：两轮真实阈值压缩（M3、M2.7）的 checkpoint 都是 `probe_supported` + `summary_budget_exceeded`：阈值在窗口 90% 触发并整段压完，而片 B 的单次随图请求必须装进 80% 窗口减输出预留，结构上永远装不下；只有范围很小的手动 `/compact` 才走到过 B。
- **改动**：新增 `conversation/compact_media_digest.py`（含图回合分组、按摘要预算与 `compact_vision_digest_max_requests` 打包看图小请求、逐个发送并按 sha 前缀打标签、结果合成决定）；`compact.py::_summarize` 先看图再文字总结，图块统一投影为归档引用，要点进文字摘要指令；`_effective_media_decision` 预算门改为“最大的一次看图小请求”；`CompactMediaDecision.summarized_blocks` 与 checkpoint 双计数（`_media_blocks_archived/_media_blocks_summarized`）；一次都没成功才写 `compact_vision_failed_generation`，且不再让整次压缩失败。配置新增 `compact_vision_digest_max_requests`（YAML + dataclass，默认 4）。
- **余量不足立刻外置工具输出（2026-09-24 深夜，用户第 5 项"单回合超窗"）**：`tool_call_archive_record._headroom_forces_externalize` 用 preflight 同口径余量判断，本条输出估算 token 不小于剩余余量就给 `force_externalize`（read_file 分页也外置）并登记 `tool_context_window_overflow(reason=tool_result_headroom)`；开关 `tool_output_externalize_on_low_headroom`。`test_tool_output_headroom_externalize.py` 5 项（外置+登记+预检消费、同轮累加、余量够内联、开关关、预算未知回退）；`test_compact_native_ir_recovery.py` 超预算夹具显式关开关。
- **图块预留进估算（2026-09-24 深夜）**：`projected_model_context_components` 新增 `media_token_reserve`，预检、`_automatic_noop` 与恢复候选计量按已知图块数 × `input_media_token_reserve` 加进估算；`test_context_pressure_media_reserve.py`（3 项：加预留且分类加总不变、非展开集合与 0 预留不计、预检真实入口读配置）。
- **测试**：`test_compact_media_vision.py` 三项改为两步语义（准入按最大请求；看图小请求只带含图回合并保留图块、文字请求带引用与要点、checkpoint 计数；typed 失败同次回落、写同代次标记、不进熔断）；新增 `test_compact_media_digest.py` 6 项（分组顺序、按预算/次数打包与两种跳过原因、标签与首个 typed 失败停止并合成 partial 决定、非 typed 上抛与空回复算失败、部分成功写双计数与 `vision_digest_partial`、无图或非 B 决定零请求）。
- **真实验收（本机，runtime-step11c，main `94a2d0b4d`，2026-09-24 19:59—20:08，MiniMax-M2.7 官方，窗口 262,144）**：贴图问图后连续粘贴报告分片，上下文 65% → 部分四贴到压缩点，回合前自动压缩（checkpoint `forced=true` 是“整段压完”语义，非施压）。第一条会话 checkpoint 为 `archived_refs / probe_inconclusive`：M2.7 的结构化视觉探针两次都没给出正确颜色的工具回答（探针本身的既有波动，不缓存），策略在预算门之前就回落。随后经产品 `save_model` 给两条官方 M2.7 与 M3 档案声明 `input_modalities=[image,text]`（依据是片 B 第二轮两模型都拿到过 `probe_supported`），第二条会话 checkpoint 为 `media_policy=vision_summary`、`media_fact_source=declared`、`media_blocks_summarized=1`、`media_blocks_archived=0`、无 `media_policy_reason`，摘要正文含“附件内容要点”段——用户第 2 项“随图摘要在自动压缩里生效”成立。证据 `~/.my-agent/releases/step10-combo4/media-c-evidence.json`。

## 媒体压缩策略片 B：视觉摘要（2026-09-24，本地分支 `claude/compact-media-b`）

- **改动**：档案字段 `input_modalities`（开放小写标识符，`validate_model`/快捷新增白名单/`resolved_model`→`AgentConfig.model_input_modalities`，`/model` 表单新增"输入模态"，决策模型不接受）；`backends/vision_capability.py` 结构化探针（8×8 纯色 PNG + `my_agent_vision_probe(color)` 工具，只认颜色一致的 tool_use；typed 媒体拒绝→unsupported 缓存；答错/不调用→inconclusive 不缓存、最多 2 次；原生工具未证明→unavailable；网络/额度错不缓存；进程级缓存键 端点+模型+api_base、同键单飞）；`compact_media_policy` 骨架决定（auto 下 archived_refs + `vision_candidate`）→ 候选构造在范围内确有媒体块时才 `resolve_vision_candidate`（声明或探针）→ 摘要请求构造处 `vision_summary_admission`（视频/`input_media_max_bytes`/文字估算+图块×`input_media_token_reserve`≤摘要预算）；B 单请求经 `generate_bounded_compact_response(vision_summary=True)`，窗口错误/媒体拒绝/截断/超预算统一 typed `COMPACT_VISION_SUMMARY_FAILED`，只写线程 `compact_vision_failed_generation`（不进熔断），同代次下一次压缩选 A 并记 `media_policy_reason`；checkpoint 新增 `media_blocks_summarized`、`media_policy_reason`；强制恢复一律 A（`policy_forced`/`forced_recovery`）。错误码登记分类表与 Gateway 文案。
- **首版缺陷即改**：探针原放在策略解析处，两个 Gateway 压缩回归（`test_gateway_compact_deferred_source`、`test_gateway_conversation_compact`）的假后端多出 2 次调用——纯文字压缩也在探针。改为只有范围内确有媒体块才解析视觉事实后恢复 1 次调用。另修 `compact_generation=0` 被 `or -1` 吞掉导致失败标记失效的边界。
- **回归**：新增 `test_compact_media_vision.py` 13 项（探针阳性缓存/typed 拒绝缓存/瞬时错误不缓存/答错与不调用两参数化/原生工具前置、骨架不探针、声明优先、强制与失败代次回落、准入三门、字段校验持久化到运行时配置、B 路径请求保留图块并写 checkpoint、typed 失败后同代次回落且不进熔断、预算门回落记原因）；片 A 两处断言按新事实更新（强制恢复 fact_source=`policy_forced`、`MediaArchiveFacts` 增 bytes/videos）。压缩/线程/模型档案/错误码相关 70 个测试文件联合 1937 passed。ruff、doc sync、strict code-size 见提交前 gate。
- **保留偏差**：preflight、`_automatic_noop` 与请求投影器的候选接受估算仍未加图块预留（只在 B 准入与摘要请求发送前落地），见设计文档"片 B 实现记录与偏差"。
- **真实验收第一轮（本机，runtime-step10p，2026-09-24 12:58—13:18）**：M3 与 M2.7 各三条会话（先贴图问图，再让上下文越过压缩点）。发现两件事：(1) 单回合工具循环把 69 万字节报告一次读完，上下文冲到 129%，恢复压缩报 `COMPACT_CANDIDATE_TOO_LARGE`——单个活动回合超窗的既有边界，与媒体无关，任务改为多回合；(2) 多回合两轮共四条会话都成功压缩且模型压缩后仍能凭摘要答出图与章节事实（M3 全对，M2.7 答对章节、如实说看不到图），但 checkpoint 全是 `forced=true, media_fact_source=policy_forced, media_policy_reason=forced_recovery`：Gateway 恢复宿主对所有压缩都传 `force=True`（整段压完语义），首版把它当成供应商施压，B 在 Gateway 里不可达。已修：新增 `pressure_forced` 只在窗口上限/供应商施压时为真，媒体策略只按它关闭 B；`test_media_compact_preflight` 的假后端声明纯文字模态避免探针多出请求，新增 `test_pressure_forced_recovery_always_archives_media`。修后真实验收见下一条。证据在仓库外 `releases/step10-combo4/media-b-evidence.json`。
- **真实验收第二轮（本机，runtime-step10r，main `8c6d29c5f`，2026-09-24 13:38—13:42）**：(1) 阈值自动压缩（贴图 + 粘贴报告到 92k 以上）：M3 与 M2.7 两条会话的 checkpoint 都是 `media_fact_source=probe_supported`——结构化视觉探针在两个 MiniMax 模型上都拿到了正确颜色的 tool_use（M2.7 也支持看图，不再靠猜）；但 `media_policy=archived_refs, media_policy_reason=summary_budget_exceeded`：范围 92,605 token 加图块预留超过摘要预算（0.8×窗口 − 输出预留），按请求前准入落 A，reason 如实进 checkpoint。(2) 手动 `/compact`（M3，贴图 + 第 1–15 章后立即压缩，范围 74,765 token）：checkpoint `media_policy=vision_summary, media_fact_source=probe_supported, media_blocks_summarized=1, media_blocks_archived=0, forced=true`，历史 74,765 → 10,585 token；压缩后不用工具追问，模型答出图里华东目标 19,800,000 与第 5 章 RPT-005-8900 / 310,868（与真值一致）。结论：探针事实、B 采用、预算门回落三条路径都有真实样本；`forced=true`（整段压完）与 B 并存，符合修后语义。

## 后台续跑扩展目录与停用结清（2026-09-24，本地分支 `claude/process-stale-unknown`）

- **断链复现（本机，MiniMax-M2.7，combo4 72 个月 10 步任务）**：run3 `agentrun-1790272709-…` 主运行 attempt 1 在 10:58:29—11:03:02 用了 `plugin__image_text…read` 等 21 次工具后派工结束；attempt 2（子代理生命周期唤醒，11:04:15）留下 3 条 `protocol_violation`：`allowed_tools=available_tools=17` 个核心工具、0 个 `plugin__*`，模型连续调用前台刚用过的 `plugin__genui_lite…export` 被判 `TOOL_UNAVAILABLE`，第三条 `will_break=true` 整轮中断、任务未完成。近三天另有 3 个主运行的续跑 attempt 留下同样的 17 工具事件（09-21 00:17、09-23 20:43、09-24 03:36）。全部依据 runtime.db 结构化字段，未读会话正文。
- **根因（框架，不是模型）**：`background_tool_policy.DEFAULT_BACKGROUND_ALLOWED_TOOLS` 就是这 17 个名字，后台唤醒续跑的 allowed_tools 只能来自这张封闭名单，插件/MCP 代理工具结构上永远进不去；违反"开放世界禁止封闭枚举"。此前 TESTS 首节把 combo2 同类现象定性为模型行为，已在原条目改判。
- **修复**：决策增加 `extension_tools`（`background-tool-policy.v2`）：默认 profile 为 `inherit`，`runtime._background_run_allowed_tools` 经 `ToolRegistry.extension_tool_names()` 按注册对象类型（`MCPProxyTool` 代理，先做与新运行相同的 `prepare_for_run`）并入当前已启用插件/MCP 工具名；显式 `background_main_agent_allowed_tools` 或任务 `allowed_tools` 标 `none` 不并入；定时任务仍返回完整目录；注册表读取失败只记类型、退回核心目录。停用撤销、owner 禁用表、连接断开仍由注册表与快照 fail-closed。YAML 注释同步。
- **同批修复 1（测试机停用卡死）**：`stop_process_session` 重试时，记录已是 `unknown` 且带上一次 `termination.cleanup`、两级实例按 PID 出生标识均不存在，则结清为 `killed`；首次停止对已消失实例仍不凭空确认。根因是 09-22 遗留的 unknown 记录永远拿不到终止回执，`/plugins disable` 反复 `PLUGIN_CLEANUP_UNCONFIRMED`、`enable` 被 `activation_unsettled` 拒绝。
- **同批修复 2（免审批旧快照调用结果码摆动）**：75 文件联合回归中 `test_plugin_enable::test_actual_enable_and_registry_view_call_then_disable` 在未改动的 main `6b48b5270` 上也稳定失败：停用清理先把连接关掉，`MCPProxyTool._execute` 先取连接就报 `MCP_CONNECTION_CLOSED`（→可重试的 `TOOL_EXECUTION_FAILED`），只有清理慢时才轮到发送准入里的激活检查报 `TOOL_UNAVAILABLE`；上午 precheck gate 通过属于慢路径。现在 `PluginProxyTool._execute` 发送前先 `activation_ref.require()`，撤销固定 `TOOL_UNAVAILABLE` / `reported_error_code=PLUGIN_ACTIVATION_UNAVAILABLE` / `not_started`、不发送。 **补丁（2026-09-24，同伴全仓复跑发现）**：`test_plugin_workspace_context` 用普通 `MCPStdioClient` 组装插件代理，没有 `activation_ref`，三处复核抛 AttributeError；现统一为 `_activation_revoked()`——没有激活合同的代理不做激活复核、只走原 MCP 连接检查（产品里插件代理一律由 `PluginMCPClient` 持有合同，不是放宽撤销门）。
- **回归**：新增 `test_background_extension_tools.py`（5 项，含真实临时 wheel 的插件启停跟随）、`test_process_session_retry_settles_unknown.py`（2 项，真实已退出进程的出生标识）、`test_plugin_proxy_revoked_call.py`（2 项）。后台策略/插件注册相关 75 个测试文件联合 2303 passed、5 xfailed、1 failed（即上述修复 2 修前的既有失败）；停用清理相关 13 个文件 226 passed；修复 2 后 `test_plugin_proxy_revoked_call` + `test_plugin_enable` + `test_tool_call_precheck` + 两个新文件 30 passed。ruff、doc sync、strict code-size（hard=0）通过。
- **真实复跑（本机，M2.7）**：run3b（同一 combo4 任务，工作区 combo4b）单 attempt 11:14:38—11:28:40 共 14 分 02 秒完成，`verify_combo4.py` 全部数字与字段名匹配，0 条协议违规，主运行 66 次工具操作里插件工具 29 次（genui export 9、browser open 9、design create/edit 3、savepoint 3、workspace-peek 3、image-text 1），6 个子代理 5 成 1 败（失败者 `create_goal` 被拒后未产出，主代理补做）。它没有触发唤醒续跑，因此不构成本修复的真实验收；证据在仓库外 `releases/step10-combo4/rerun-evidence.json`。修复后的真实验收见下一条。
- **真实验收（本机，M2.7，runtime-step10o，main `4ec0e11f3`，2026-09-24 12:09）**：两段式任务（先用 workspace-peek tree、派一个子代理后让出；被子代理完成唤醒后用 genui-lite export 导出柱状图并再次 tree）。主运行 `agentrun-1790276946-…` attempt 1（12:09:06—12:09:48）tool_operations 为 `plugin__workspace_peek…tree` 1、`create_subagents` 1；attempt 2（12:10:28—12:10:49，唤醒续跑）为 `plugin__genui_lite…export` 1、`plugin__workspace_peek…tree` 1，全部 SUCCEEDED；0 条 `protocol_violation`；子代理产出的 summary.json 四个区域金额与真值全部一致，summary.html 2625 字节。修前同类续跑只有 17 个核心工具（run3），修后续跑 attempt 能直接调用插件工具。证据在仓库外 `releases/step10-combo4/combo5-evidence.json`。
- **真实复验（测试机，M2.7，runtime-step10o）**：此前卡在 revoked 的 workspace-peek 再执行 `/plugins disable` 返回"插件已停用并释放"，随后 `/plugins enable` 成功，installations.json 中 phase 由 `revoked`（revoke, rev 8）变为 `active`（activate, rev 11）；TESTS 首节待办已关闭。证据在仓库外 `releases/step10-combo4/testbox-plugin-cleanup-reverify.json`。

## 媒体压缩策略片 A：归档引用主链（2026-09-24，本地分支 `claude/compact-media-policy`）

- **改动**：新增 `backends/request_content.classify_nontext_content` / `compact_source_supported` / `is_local_media_block`、`conversation/compact_media_policy.py`、`tool_request_projection.compact_request_source_supported`；`compact._split_nontext_transcript_suffix` 按策略只保护 unknown 块，`conversation_compact_provider_source` 支持逐条只读投影，checkpoint 新增 `media_policy` / `media_fact_source` / `media_blocks_archived` / `media_refs`；preflight 与恢复宿主两处门改用"能否摘要"判定；配置 `compact_media_policy`（默认 `auto`，片 A 等价于 `archived_refs`）。`_summarize_segments` 与 `text_request_capacity_known` 保持严格语义不变。已知媒体严格等于运输层会展开的集合（顶层 user 行的 local_file image/video），嵌套或 assistant 侧一律 unknown。
- **新测试** `test_compact_media_policy.py` 22 项：分类矩阵（嵌套 / assistant 侧媒体计 unknown）、策略解析与非法值 fail closed、投影只改顶层 user 的 local_file 块且不含路径、来源多遍重放一致、后缀保护按策略切换、归档引用压缩覆盖含图回合并写 checkpoint 事实、`off` 下旧行为且不写媒体字段、unknown 块在 `archived_refs` 下仍保护。
- **改写用例**：`test_media_compact_preflight.py` 九项矩阵与 Gateway 四档按 `off`/`auto` 参数化，图片夹具改为 canonical 的 local_file 引用；`auto` 下带图历史越过压缩点先归档引用再压缩、业务请求 0 个图片块、摘要请求含"附件引用"且不含 local_file，越窗时强制恢复同样走归档引用后发送。`test_compact_media_recovery.py`、`test_compact_retained_history.py` 显式钉住 `off`。
- **结果**：直接相关 10 个文件 230 项通过；相关 90 个测试文件 2059 passed、1 skipped、3 xfailed、1 xpassed；ruff 通过。已合入 main `6bb0467b1`（含 `3fce97453`），同一 wheel（21c45e31）双机 `runtime-step10j`。
- **真实 TUI 验收（本机，MiniMax-M3，2026-09-24）**：贴图只看图读出 7 个目标值 → 读文件三句总结 → 手动 `/compact` → 再问图。checkpoint v3 generation 1、forced、source_messages 4、retained_tail 0，`media_policy=archived_refs`、`media_fact_source=vision_fact_unavailable`、`media_blocks_archived=1`、`media_refs` 与原图 sha256 一致；摘要含"附件引用"、不含 local_file 或 owner 路径；投影 token 18,975→10,299。压缩后模型按摘要给出正确数值并提示可重新附图或用 OCR 复核原件，未声称仍能看见图片。证据在仓库外 `releases/step10-combo3/media-a-evidence.json`。
- **组合长任务补跑两轮（本机，同版运行时前一版 step10i）**：第一轮 M2.7 单 prompt 7 步 36 个 CSV 三子代理约 7 分钟、同线程切 M3 贴图核对目标 16 秒；第二轮全程 M3 单 prompt 13 步（含只看图读目标、image-text OCR 对照、5 张图表、快照恢复、浏览器读三页）约 7 分钟。两轮区域/逐月/逐年/目标读取/达标判断全部与独立真值一致，插件工具均按协议调用（第二轮 create_subagents 首次因 covers 重复被拒后重试成功）；第二轮 JSON 字段名用了英文键（要求中文），记为模型内容偏差。两轮都远低于 15—30 分钟：MiniMax 模型完成该规模任务只需约 7 分钟，未满足 Goal 的时长条件。

## Gateway 消息文件流式读取（2026-09-24，本地分支 `claude/decision-gateway-message-reads`）

- **新测试** `test_message_tail_streaming.py` 124 项，以不改的 `read_jsonl_tail_report`、`recent_report` 作参照逐项比对：
  - 24 个种子，轮换三种模式（混合故障、完全干净、大量 display 行）：尾部行、窗口投影、全量访问、近期产物、去重判定五类等价。覆盖多块大文件、翻倍重读、坏行、解析失败、CRLF、NEL/U+2028、无换行或被截断的末行、非法 UTF-8（全量路径两边都抛 UnicodeDecodeError）。
  - 3 种块边界：多字节字符、LF、CRLF 恰好跨越 64KiB 边界。
  - 原实现的一个边角：窗口内有空行时返回的行数少于 limit，新实现一致。
- **变异验证**：去掉跨块拼接、去掉迭代器条数上限、投影超出上限、窗口不按换行数关闭、保留窗口首个不完整行、有坏行仍访问，6 种各自使 76、37、29、18、19、8 项失败。
- **峰值与耗时**（仓外探针，同一 4.2M 字符夹具，同一基点 main `3d2424786`）：Gateway 准备期峰值 21.33→0.82MB，全程峰值 22.65→10.03MB；三宿主阶段探针中 child、后台不变；每次请求三次尾读约 40ms→约 6ms；建索引峰值 16.96→0.72MB，耗时基本不变。
- **回归**（基点 main `3d2424786`）：171 个相关测试文件 4078 passed、1 skipped、25 xfailed。
- **合入与部署**（主线 owner，2026-09-24）：独立复跑 27 个相关测试文件 1106 项通过、ruff 干净后快进合入 main `0bb09a76b`；同一 wheel（SHA256 前缀 0eab62e0）本机先切、测试机后切，双机 `runtime-step10i`，回滚 step10h 保留。

## 决策线两片合入与 step10h 双机部署（2026-09-24，main `d69f30cf3`，wheel c52da295）

- **合入**：`claude/step10-batch1` 从 `41c66872e` 快进到 `ec821c90c`（媒体 preflight：代码 `e90d2ec60`，其余为文档），再合并 `5cb75eaf8`（决策设置无锁读取），得 `d69f30cf3` 推送 main。TESTS.md 唯一冲突为两节都在顶部新增，按两节都保留、共用标题取"已合入 main"版本解决。
- **独立复跑**（各在 detached worktree 上，不用对方的工作树）：媒体链 18 个相关测试文件 395 项通过；决策链 31 个文件 779 项通过；合并树上取并集 47 个文件 1120 项通过。ruff（agent_py_agent/scripts/plugins）、doc sync、strict code-size（hard=0）、`git diff --check`、clean-package 均通过。线上 CI 停用，未作为验收来源。
- **审阅要点**：无锁读取只走 `operation == "read" and not blocking` 分支；`_owner_operation` 的 read 路径只做投影不写文件，`read_model_profiles` 在文件不存在时返回默认值不落盘；两份设置文件都是原子替换写，读者最多读到已提交版本，调用方仍在调用前后复核 revision。媒体 preflight 把自动 noop、轮内跳过、强制拒绝三处口径统一为"压缩链不可用时只守窗口硬上限"，`COMPACT_REQUEST_NON_TEXT` 单列并配专门文案。
- **部署**：同一 wheel（SHA256 前缀 c52da295）在本机与测试机各自复制上一运行时后强制重装、逐文件核对；测试机先切换（本机当时 processing=1、有活动 attempt，被空闲检查拒绝），本机随后空闲时切换，两机各一个 Gateway、默认入口同版，回滚运行时 step10g 保留。测试机新旧 Gateway 日志各有 4 条 `model_not_configured` 后台迭代记录，属既有现象，非本版回归。
- **发布脚本教训**：一条龙脚本里本机切换的失败被管道掩盖而继续切测试机，造成短时双机不同版；脚本已加 `pipefail`，并要求推送 main 成功后才允许部署（`&&` 串接，不用 `;`）。
- **"模型绕过插件工具"定性**（combo2 A 任务 `gwreq-1790249102-…`，M2.7，主运行两个 attempt 共 77 轮）：全部依据结构化事实，未读任何会话或记忆正文。归档 context bundle 的 `tool_manifest`（tool_runtime_manifest v2）显示该 run `allowed_tools=None`、visible=executable=51，其中 18 个 `plugin__*` 工具 category 为 `plugins`，不在默认折叠类别（collaboration/web/vision/meta/mcp）内，即原生 schema 直出；model_usage 两条记录的 `purpose_breakdown.decision` 均为 0（决策关闭，无 shortlist/deferred 可查）；主运行 tool_operations 为 create_subagents 1、write_file 5、run_command 48（33 成功/15 失败）、edit_file 1，插件工具 0 次；同日 05:11 的最小复现在同样可见性下正常调用了 `plugin__genui_lite…export`。结论：模型行为（长上下文下自选 run_command 直连插件环境，并给出与事实不符的"不在快照"说法），不是可见性或决策线缺陷，记为模型能力边界。`tool_search`/`list_tools` 不进 tool_operations，不能由其缺席推断未搜索。 **2026-09-24 改判**：上述结论只核对了 attempt 1（前台）的 context bundle。同日 combo4 run3 在 attempt 2（子代理生命周期唤醒后的后台续跑）留下 3 条 `protocol_violation` 事件，`allowed_tools=available_tools=17` 个核心工具、无 `plugin__*`，违规码 `TOOL_UNAVAILABLE`，第三条 `will_break=true` 整轮中断；近三天另有 3 个主运行的续跑 attempt 留下同样的 17 工具事件。代码上 `background_tool_policy.DEFAULT_BACKGROUND_ALLOWED_TOOLS` 就是这 17 个名字，插件工具结构上进不去。combo2 的 attempt 2 同为 98.6 秒间隔后的生命周期唤醒续跑，因此模型说"插件工具不在快照"在 attempt 2 是事实，改判为框架缺陷（后台白名单封闭枚举），不是模型能力边界；修复见下文"后台续跑扩展目录与停用结清"。

## 媒体会话越过压缩点：preflight 只守窗口、越窗结构化拒绝（2026-09-24，本地分支 `claude/decision-media-preflight`）

- **新测试** `test_media_compact_preflight.py` 13 项：
  - preflight 9 项矩阵：纯文字按压缩点；带图时压缩点与窗口之间放行、到窗口拦截，诊断串带 `compact_capacity=non_text`；`save=false` 下纯文字与带图都按窗口（主线 owner 要求钉住的原行为）。
  - Gateway 真实链四档（只替换末端 HTTP，图片经原导入入口，窗口 60k、压缩点 50%）：带图在压缩点以下发送 1 个图片块；越过压缩点、低于窗口时带图发送且不压缩（修复前整轮失败、业务 HTTP 0 次）；越过窗口时报 `COMPACT_REQUEST_NON_TEXT`，业务 HTTP 0 次、原始记录原序保留，客户端文案点明是图片等非文本内容所致；纯文字对照照常先压缩再发送。
- **改期望码**：`test_compact_media_recovery.py`（供应商报溢出）和 `test_compact_retained_history.py`（大历史）的媒体强制恢复，从 `COMPACT_REQUEST_PROJECTION_UNKNOWN` 改为 `COMPACT_REQUEST_NON_TEXT`。
- **变异验证**：去掉 preflight 的媒体门槛，3 项失败（2 项矩阵、Gateway 越压缩点档）；恢复旧码，7 项失败（越窗档、2 项供应商溢出、4 项大历史）；去掉 `COMPACT_REQUEST_NON_TEXT` 的专门文案，越窗档失败。改回后全部通过。
- **结果**：72 个相关测试文件 6460 passed、2 skipped、2 xfailed、5 xpassed。5 个 xpassed 在同一 main 上完全一样，与本片无关。

## 压缩点与恢复目标的自助修改范围与运行时一致（2026-09-24，本地分支 `claude/compact-percent-config-sync`）

- **问题**：运行时把压缩点夹在 50–100%、恢复目标夹在 25–80%（配置解析同样如此），但 `user_config` 自助修改对两者都接受 25–95。于是压缩点 25–49、恢复目标 81–95 会被"成功"保存，却在运行时变成 50、80。这是在第 12 项请求准备阶段压缩真实样本中发现的。
- **修复**：两个范围改为 `_memory_coercion` 里的共享常量，配置解析与 `user_config` 校验同用；运行时夹取不变。
- **新测试** `test_user_config_capability.py` 1 项：对 −5 到 130 的每个取值，自助修改接受、运行时夹取后不变、配置解析后不变，三者必须同真同假。
- **变异验证**：4 种各自使该测试失败——压缩点校验改回 25–95、恢复目标校验改回 25–95、共享常量任一改成与运行时不一致。改回后全部通过。

## 子代理自动选模提交阶段的结构化原因码（2026-09-24，本地分支 `claude/decision-child-commit-reasons`）

- **扩展** `test_subagent_first_request_selection.py` 的自动验证失败矩阵：新增"父线程级设置变化"档，得 `settings_changed`；原有各档改为逐一断言原因码——改目录、关闭决策都得 `model_catalog_changed`（owner 级设置写在目录里），显式同值选择得 `explicit_model_selection`，此前前两档只会记成 `selection_changed`。
- **新测试** 同文件 3 项：
  - 目录锁或父线程锁在提交时被占用，分别记 `model_catalog_busy`、`source_thread_busy`，业务照常用继承模型发出。替身只在探针前换上，准备阶段不受影响。
  - `_adoption_conflict` 表：期限最先判定 `commit_deadline`；建议变化 `advice_changed`；首请求资格被消费（标记缺失、非 preparing、attempt 或 operation 不符）`first_request_consumed`；选择版本变化 `selection_revision_changed`；全部一致返回空串。
- **变异验证**：7 种各自使测试失败——收拢回 `selection_changed`（5 项失败）、两把锁的原因码互换（各 1 项）、`settings_changed` 记成目录变化、期限改到最后判定、资格被消费记成建议变化、忽略选择版本变化。改回后全部通过。
- **结果**：见本分支交接。

## 决策连接连续失败的冷却退避（2026-09-24，已合入 main `3933b1db1`）

- **新测试** `test_decision_cooldown_backoff.py` 6 项：
  - 冷却表：每次冷却过期后再失败，冷却依次为 30、60、120、240、300、300 秒；冷却期内才返回的失败不改截止时刻、不加级；成功和显式重试都从 30 秒重新计起；额度失败固定 300 秒并延长正在进行的冷却，但不加级；配置错误不随时间解除，修订变化后的失败从 30 秒计起。
  - 经 `decide()`：持续失败的连接依次冷却 30、60、120 秒，冷却期内零尝试；一次成功后再失败回到 30 秒。
- **扩展** `test_decision_fault_matrix.py`：故障仍在的两档（连接被拒、DNS）在冷却到期重试再失败后，经真实传输栈断言下一次冷却为 60 秒。
- **变异验证**：7 种各自使新测试失败——不翻倍、冷却期内的并发失败也加级、成功不复位、冷却过期即忘记次数、不封顶、额度不延长进行中的冷却、显式重试保留次数。改回后全部通过。
- **结果**：见本分支交接。

## 决策服务故障矩阵与同 owner 并发（2026-09-24，本地分支 `claude/decision-fault-matrix`）

- **新测试** `test_decision_fault_matrix.py` 10 项，全部经真实传输栈（本机 HTTP，不访问外网）：
  - 8 种故障各自首次结果、冷却期内零新尝试、到期（或设置修订变化）后恰好一次新尝试：连接被拒、DNS（只拦截一个保留域名的解析）、明文端口上发 https 的 TLS 失败、429 额度、429 限流、402、503、慢响应。
  - 同一 owner 12 个不同会话并发决策各自成功（12 次 HTTP）；同一会话同时两次，第二次 `admission_busy`。
  - 模型目录写锁被占用时，决策按已提交设置照常完成，不再是 `settings_busy`。
- **改写用例**：`test_decision_service_http.py` 与 `test_decision_service.py` 各一条原先钉住"写锁占用即 settings_busy"的用例，改为"不等待、照常完成"；后者补替换调用边界，避免真的解析保留域名。夹具增加可选错误体。
- **变异验证**：去掉无锁读取，3 项失败（并发、目录锁、线程锁）；额度冷却改成 30 秒，2 项失败（429 两档）；配置类故障改成定时冷却，2 项失败（402、TLS）。改回后全部通过；新文件连跑 5 遍稳定。
- **结果**：决策相关 39 个测试文件 908 passed。

## 后台上下文预算只估算渲染节（2026-09-24，已合入 main `5dcdfd463`）

- **新测试** `test_background_context_budget.py` 5 项：
  - 有种子时预算不计入不渲染的最近消息（shape_pass 为 0，12 条观察全留）；无种子时照常计入并收缩。
  - 渲染只在无种子时出现 Recent Messages。
  - 合同：预算估算的节集合等于实际渲染的节集合，覆盖普通有种子、普通无种子、审计窄事件三种情况。
- **改写用例**：`test_background_scoped_compact.py` 的独立任务用例。原先把就绪路径保留的 bundle 当作"运行视图"核对消息范围；现在断言 bundle 不带消息正文，并改在真实无种子路径的渲染结果上核对任务范围。
- **新增界限**：`test_host_summary_phase_lifetime.py` 的后台宿主，摘要入口驻留低于历史正文的 3/8（修复前约 2.48MB，修复后约 1.15MB）。
- **变异验证**：预算忽略 `rendered_keys`，4 项失败；渲染方不传，3 项失败；就绪路径保留消息，3 项失败。改回后全部通过。
- **结果**：后台相关 29 个测试文件，840 passed。

## 决策线能力推荐按插件分组出题（2026-09-24，本地分支 `claude/decision-plugin-grouping`）

- **新测试** `test_decision_capability_provider_grouping.py` 2 项：
  - 分组单测：只按结构化 `provider_id` 合并，题的位置取第一个成员；描述写着"插件 alpha"但没有 `provider_id` 的内置工具仍单独成题；任一成员版本变化时，合并行的版本随之变化；插件题带整体判断说明。
  - 消费者用例：在真实 Registry 与 Skill 快照上注册两个插件（alpha 2 个工具，beta 1 个）。3 个插件工具只出 2 题；alpha 选中时两个工具都进入短名单，beta 未选中时整体不在短名单，并进入延迟名单。
- **变异验证**：
  - `_material` 不分组：消费者用例失败。
  - `_project` 不展开：消费者用例失败。
  - 按描述文字归组：单测失败。
  - 改回后通过。
- **回归**：原能力推荐 4 个测试文件共 93 项通过，行为不变（这些夹具里没有插件工具）。
- **量化**：用仓库内置插件声明（8 个插件，21 个工具）按 `plugin_runtime` 同一格式生成候选。题数 21→8，题目序列化 30,691→19,520 字节。

## 第12.4项第二片 2b：摘要期释放旧请求历史（2026-09-24，已合入 main `911d0d14d`）

- **新测试**：
  - `test_host_summary_phase_lifetime.py` 3 项（`slow`，约 12 秒）：Gateway、child、后台三宿主各写入 128 行、约 4.2M 字符的历史，跑完整链。第一次进入摘要时，原参数的旧历史已解绑，驻留低于历史正文的 3/4；摘要逐条覆盖全部历史；首业务请求带当前任务。
  - `test_compact_recovery_release.py` 11 项：
    - Gateway 七种失败（代次冲突、取消、令牌取消、未知投影、摘要瞬断、摘要超时、候选超限）与后台两种失败（取消、候选超限）。解绑后换上"读取即报错"的替身，收尾全程不读旧历史，也不发业务请求。
    - 自动 noop 原样发送原请求，旧历史不解绑。
    - 候选与原参数共享 `tool_context` 的合同。
  - `test_conversation_history_seed.py` 新增 1 项：空的只读来源在原生边界只读一次，不走旧文本回退。
  - 审阅后补 2 项（`test_rows_native_drops_but_text_keeps_do_not_change_native_output`）：来源里只有 native 过滤、text 保留的行（后台空正文行、child 规则 display 行）时，跳过旧文本回退前后原生输出一致（均为空）。
- **变异验证**：
  - 去掉解绑：全链 3 项与 Gateway 7 项失败（后台夹具没有旧历史，不能区分）。
  - 在收尾路径加一次读取：9 项失败。
  - 把解绑挪到 noop 之前：noop 用例失败。
  - 只读来源改回旧文本回退：单测失败。
  - 改回后全部通过。
- **结果**：
  - 2a、retry 两片的相关文件加本片新测试，共 147 个文件：3,724 passed、1 skipped、24 xfailed、1 xpassed。
  - Ruff、doc sync、diff 检查通过。
  - strict code-size：hard=0；与 main `c18519d90` 逐项对照，本片没有新增发现。main 上 high-risk 已是 1468，仓库里的报告文件是旧的。
- **未覆盖**：没有跑真实模型；建循环时的首次物化峰值不在本片。

## 恢复候选提交后的同请求重试（2026-09-24，本地分支 `claude/decision-retry-committed`）

- **新测试**：
  - `test_gateway_compact_recovery.py::test_transient_retry_after_commit_resends_committed_candidate`（2 项）：Gateway 溢出 → 摘要 → CAS → 候选瞬断。
    - 重试成功：两次发送都是同一候选，两次之间没有重建，也没有共享预算回收。候选回的工具调用进入下一工具轮，下一轮按候选参数正常重建（带工具结果），不再命中。
    - 重试耗尽：每次都发送候选，最后原样抛 `ProviderTransientError`。
    - 两种结局都只有一次摘要、一行 v3 checkpoint，代次为 1。
  - `test_compact_native_ir_recovery.py` 三项，共用 `_committed_background_attempt`（后台宿主直接安装恢复宿主，首请求超预算，自动恢复提交候选）：
    - 瞬断重试：首个候选瞬断后重发同一候选，前后没有重建和共享预算回收，代次为 1。
    - 空响应修复：候选返回空响应后，重跑请求只比候选多一段修复提示；共享预算回收只落在候选参数上。
    - 插话取代：候选在途时到达插话，候选返回空响应。重跑请求带这条插话且只带一次，候选参数的 tool_context 里也只有一次；确认后邮箱清空；总共只有两次业务发送。
  - `test_tool_loop_model_turn.py` 两项：
    - 记录只对同一份原参数、同一 agent 命中；换参数对象即清除；未提交或宿主没有回调时不命中。
    - 命中时不调用过期自然回复丢弃、自然回复切换、build、插话注入、共享预算回收和 prepare，只做首请求选模、选模和发送。
- **变异验证**：
  - 取记录始终返回 None：Gateway 2 项、后台 1 项失败。Gateway 重试发回重建的旧请求；后台在原参数上做共享预算回收，抛 `compact summary base changed`。
  - 服务层不查记录：命中路径单测失败。
  - 换参数对象不清除记录：记录单测失败。
  - 去掉 `_model_turn_or_retry` 异常分支换成候选参数的几行：空响应修复用例在原参数上共享回收，抛 `compact summary base changed`；插话取代用例多出第三次发送（第二次是不带插话的旧候选）。
  - 改回后全部通过。
- **结果**：
  - 首个提交：引用改动模块或其入口的 57 个测试文件，1,612 passed、24 xfailed、1 xpassed。
  - 并入重跑分支修复后：再加上覆盖空响应修复、插话和模型轮采纳的测试，共 79 个文件，2,363 passed、1 skipped、24 xfailed、1 xpassed。
  - Ruff、doc sync、strict code-size（hard=0，高风险项与基线相同）、diff 检查通过。
- **未覆盖**：没有跑真实模型；Gateway 宿主没有会话任务邮箱，插话取代只在后台宿主上验证。设计见[依赖拆分同名节](docs/design/TOOL_LOOP_DEPENDENCY_SPLIT.md#恢复候选提交后的同请求重试决策分支2026-09-24本地)。

## 第12.4项第二片 2a：宿主历史种子只读来源（2026-09-23，本地分支）

- **新测试**：
  - `test_conversation_history_seed.py` 16 项：具体种子与只读来源在 `_native_provider_history_messages`、`_text_conversation_history_section` 两个边界逐项相等，并覆盖：
    - 下游 `project_native_provider_messages` 孤儿清扫的补位结果、PNG 媒体、匿名信封、终态工具折叠；
    - 当前请求行、Audit 投递、display 行的排除；
    - 磁盘地址视图与内存行，Gateway、后台（保留空正文行）、child 三种规则，三宿主真实入口逐项核对 messages；
    - 冻结时刻：终态折叠热尾窗口（300 秒）过后再解析，仍与准备时的具体种子相同，多次解析结果不变；
    - 冻结后合法追加不改变已选历史；
    - 互斥校验；源文件改写、截短、原子替换（内容相同也算）后抛 `DataCorruptionError`，删除后抛 `FileNotFoundError`，都不会变成空历史。
  - `test_host_history_seed_lifetime.py` 3 项：4.2M 字符种子准备驻留 32–54KB、峰值 270–294KB（修改前约 8.5MB/8.6–9.0MB），解析后完整 JSON hash 与行数不变。
- **变异验证**：解析时不传冻结时刻（改按解析时刻投影），时钟用例 3 项失败；后台改回过滤空正文，三宿主用例失败。改回后全部通过。
- **适配**：11 个相邻测试文件的种子/上下文断言改为经解析入口核对完整内容，没有删除内容断言。Gateway 的 5 个文件共用 `_gateway_history_helpers.py`，按生产单行规则从只读来源取历史。其中 deferred source 用例原先把被缩窄的展示投影当成 Gateway 历史来源；改动后历史与 Compact 来源是同一批完整行（24 行），并断言缩窄的展示投影从未被调用。
- **改写断言**：`test_gateway_conversation_compact.py` 两个用例原先断言 `_conversation_prompt_section` 默认分支（摘要在 Gateway 段内、位于操作证据之前），该分支生产不可达（所有调用方都传 `include_transcript=False`），已随评审删除。现改为断言生产路径：摘要只在会话种子的文本历史段，操作证据只在 Gateway 上下文段，两者互不混入。原排序断言只针对不可达分支；生产文本协议里 Gateway 段排在历史段之前，2a 没有改变。`test_gateway_child_compact_scope_application.py` 原先比较两个种子对象是否相等，现在两次准备各自冻结投影时刻，改为逐项比较摘要、代次和两个边界的解析结果。
- **结果**：
  - 相关 114 个测试文件 2,932 passed、24 xfailed、1 xpassed。
  - 接到 main `fae9d5855` 后，加上第 10 步改动过的测试文件共 118 个：2,976 passed、24 xfailed、1 xpassed。
  - 独立评审修复后，同一 118 个文件：2,985 passed、24 xfailed、1 xpassed（新增 9 项种子用例）。最后删掉 `freeze_history_source` 未使用的 `current_epoch` 参数（避免新增参数过多的 code-size 高风险项）后，13 个改动测试文件再跑 358 passed。
  - 变基到 main `1be5753ff` 后，上述 118 个文件加 main 新改动的 6 个测试文件共 124 个：3,087 passed、24 xfailed、1 xpassed；Ruff、doc sync、strict code-size（hard=0，高风险项与基线相同）、diff 检查、clean-package 全部通过。
  - 完整链前后对照见容量审计同名一节。
- **未覆盖**：没有跑真实模型或 TUI，线上 CI 未作为验收来源。2b 未开始。

## 18-A 吸收 main `66a598cf3` 合并回归（2026-09-23，本地）

- **基线**：合并前对两侧 git-archive 快照各跑一次 8 分片全量。决策 `46ac29601` 有 12 项失败，其中 10 项稳定；另 2 项分别依赖 git 仓库环境、对时序敏感。main `66a598cf3` 有 16 项失败，其中 15 项是旧夹具问题，已由主线 `d54fc0c98` 修复。
- **合并后全量**：20,623 passed，29 failed。24 项在任一基线中已经失败，逐项对照无新增原因。合并新引入 5 项，均已修复：
  - main 新增的 `test_nontext_segment_source_does_not_commit_mechanical_summary`（2 项）：本线摘要改走 `conversation_compact_provider_source` 流式来源，替身换到这个接缝，断言不变。
  - `test_shared_native_window_commits_main_or_child_conversation_compact`（2 项）：改读 v3 的 `source_tool_refs`，并补非空和四元字段断言。
  - `test_applied_compact_context`：全量启动后该用例才改名，属旧名残留，复跑通过。
- **定向结果**（重叠不累加）：
  - `test_compact_tool_call_refs` 28 项：原三元场景逐条迁到四元；用主线 `66a598cf3` writer 实际写出的 v2 行，验证按 legacy 读取、不隐藏、不丢行；篡改的 v3 行拒绝读取。
  - 机械改写的 5 个文件：120 passed、20 xfailed。
  - `test_applied_compact_context`：13 项。
  - 修复后的 nontext 与原生 IR 两个文件：全部通过。
- **Gate**：全目录 Ruff、doc sync、strict code-size（hard=0、blocked=False，基线不改）、diff 与 clean-package 全部通过。
- **未覆盖**：未运行真实模型或 TUI，线上 CI 未作为验收来源。原场景到新测试的对照清单随交接提交给主线 owner。
- **随后吸收 `0d02bb272`**：主线 15 项夹具修复已随之进入。决策线原有的架构守卫失败源于 `concatenate_message_rows(*parts)` 的可变位置参数，已改为显式元组；守卫及 7 个 Compact 分区/来源测试文件共 141 项通过。
- **主线 owner 要求的新测试**（每条都做了变异验证：把对应实现改坏后测试失败，改回后通过）：
  - `test_transient_retry_reselects_and_accepts_only_final_attempt_params`：首次尝试瞬断后重新选模，计量、恢复、确认只收到第二次尝试的参数，覆盖正常响应和供应商超限两种结果。变异：让选模参数不交回，2 项失败。
  - `test_owned_recovery_fits_over_budget_native_history_before_business_request`：构造 4 对真实 read_file 原生往返，冻结的原始完整请求超过共享预算。在自动和强制两种恢复模式下，都断言发送前共享预算回收没有介入、宿主已提交候选、候选的完整计量低于输入上界、业务线上没有原文。变异：去掉领取门，2 项失败；自动宿主不压缩，自动模式失败。
  - `test_post_commit_context_refresh_failure_keeps_committed_history`：CAS 之后同范围视图刷新失败时，异常原样上抛，不回滚已提交的 IR 与上下文，不记压缩失败，也不发布结果。变异：把刷新挪回回滚 try 内，2 项失败。
  - b4ffb3475 的三个用例（候选回收两项、提交后投影失败不回滚一项）正文和断言与 main 逐字一致，均通过。它们共用的 `_SummaryBackend` 只多接收两个关键字参数（`tool_choice`、`request_options`，本线摘要请求会传入），返回值不变。
- **决策线接手前已有的 9 项失败已清零**（在 `46ac29601` 快照上就已失败，main 上没有；不处理的话合入后会变成 main 的新失败）：
  - 产品缺陷（修复前 3 项失败，修复后通过）：首次恢复宿主在请求没有会话来源时仍读 `compact_source.thread`，导致未绑定 thread 的 ask 全部 AttributeError。受影响的是孤儿响应投影用例和两条 Gateway 场景（多 worker、延迟响应）。现与 overflow 入口共用同一判定：没有来源就不安装宿主。
  - 合同缺口（1 项）：`INPUT_MEDIA_INVALID` 已登记到唯一错误合同，分类为确定的用户输入失败：不可重试，请用户重新添加附件。
  - 过时测试替身（5 项）：本线改动后测试没有同步，断言意图不变。
    - Gateway 改为先加载来源再准备，替身补上来源桩；
    - 执行选项显式带出 `input_media: []`；
    - 接续插话保留 `input_ids`；
    - 摘要来源改走流式 `message_source`，替身物化同一来源后再检查覆盖。
  - 验证：上述 6 个文件及相邻 Gateway/媒体/恢复共 12 个文件，350 passed、1 skipped、3 xfailed。
- **来源身份不可证明的结构化原因**（review 发现 1，原场景：部署前写入的工具索引行缺 attempt_id；当前轮只剩这类记录时，原因在调用方丢失）：合并后三宿主共用的强制恢复把这种情况报成 `COMPACT_SOURCE_EMPTY`（"没有可压缩的源历史"），现改报 `COMPACT_TOOL_COVERAGE_UNKNOWN`，原记录保持可见，不发业务请求，也不提交。新用例 `test_forced_recovery_with_identityless_carried_records_reports_coverage_unknown` 在修复前失败（得到 EMPTY），修复后通过；所有涉及这两个码的恢复测试共 12 个文件，179 passed。
- **最终全量**（`11a3412b5`，8 分片）：20,592 passed，65 failed。65 项都是本地 HTTP、流式或期限类超时，当时同机负载约 5—6；单进程重跑这 65 项全部通过。接手前决策基线 10 项、main 基线 15 项的失败均已清零。Ruff、doc sync、strict code-size（hard=0、blocked=False）、diff 与 clean-package 通过。未运行真实模型或 TUI，线上 CI 未作为验收来源。

## 第12.4项选中来源到摘要生命周期（2026-09-23，本地）

解决选中canonical正文与完整摘要provider数组同时常驻的问题，复用原扫描、JSON、token估算、native/orphan规则及checkpoint/CAS。真实临时文件到load→摘要分段→提交的红绿测试：4,195,620字符旧峰值9,683,703 bytes，新峰值1,448,802；8,391,460字符新峰值1,532,904。完整JSON hash、95/189段连续覆盖和精确消息ID通过。该数来自专项独立进程，联合运行受分配器影响约1.19/1.53MB；只证明本Python路径，不等同RSS或三宿主峰值。

最终26文件 **530 passed（31.38秒）**，日志 `/tmp/decision_source_focused_final_20260923.log`；包含选择器/扫描、source生命周期、JSON估算、媒体、两候选、原后台162项及native IR29项。失败/取消/改写/CAS不推进覆盖、append下轮可见；原媒体fake补可选source参数，业务断言不改。独立只读末审无新确定缺陷。全目录Ruff、doc sync、strict code-size（hard=0、基线不改）、diff及登记新文件后的clean-package通过。

无scope旧入口、三宿主旧seed/frozen/回调仍待后续处理，精确ID和landmark仍随行数增长；没有部署、真实模型调用或全仓通过声明。12.4不勾选，线上CI未作为验收来源。

12.7固定5e5122b04安装候选：19个变更测试文件613 passed、1既有xpassed，Ruff/doc sync/strict size/diff/clean源码与wheel通过；不重新裁定旧全仓失败，线上CI未作证据。同一原生TUI七轮业务全部完成，覆盖M2.7 Compact前后、on4保留、官方M3及OpenCode DeepSeek显式切换的真实工具/缓存。原ledger逐字段来源区分未知和零，摘要独立计量；显式选模不算自动采用。详见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。

此前4项后台Compact测试接口缺口已按owner授权收口：先复现4 failed/158 passed，再补fake Store的include_messages及临时canonical消息域，整文件162 passed。原业务断言及生产路径不改；旧全仓八项历史问题另列，第12.4及11/18仍未完成。见[验收记录](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4原生历史投影保留一次canonical隔离复制，已隔离副本直接用于模型/摘要，匿名重复输出仍独立；嵌套容器测试峰值约4.89MB降至3.03MB。复用主线b4ffb3475的小JSON有界直接编码修复，估算口径不变；三文件72项通过。来源正文/覆盖ID仍驻留，12.4及11/18不变。详见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.4检查点读取已本地改为逐行校验：不再同时保留全账本及全部旧摘要，原writer/CAS和覆盖规则不变。9文件98项通过；约6.5MB账本的测试峰值由26.35MB降至0.69/1.23MB（orphan/已提交链）。选中消息正文及覆盖ID仍驻留，12.4和11/18不提前完成。见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.6本地组合已完成：250K/1M近窗口完整输入、同Agent双会话采用/保留隔离、未知协议/模态及连接变更校准失效；六文件123项通过。消息扫描Unicode空白与数值溢出兼容修复五文件145项通过。12.4全链来源及12.7真实缓存仍开放，11/18不变。见[本地验收记录](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

12.5本地组合已完成：三宿主完整候选保留当前要求、工具schema及输出cap，容量不足不提交；Responses未知cap单列。十文件187项通过，增强断言后新14项复验通过（重叠不累加）。当时12.4/12.6/12.7仍开放；12.6最新状态见首段，18项清单11/18不变。见[容量验收](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

主线最小移植闭包已核对：扫描、等值估算、摘要窗口三组在固定主线临时副本通过97项及相邻139项；无Jev配置依赖，尚未合入或发布。scoped来源/覆盖必须另按合同闭包接入，11/18及12.4不变。见[移植交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

## 第12.4项顺序摘要字符来源（2026-09-23，本地）

复用原摘要循环，移除整份JSON副本及二分半份来源复制，共享tokens改等值流式累计。定向初始43项、新源/估算62项、9文件176项、发送/响应取消补齐后15文件315项通过（24.59秒），这些计数重叠不累加；异常优先级审查补充后最终15文件 **316 passed（24.86秒）**，日志 `/tmp/decision_stream_verified_20260923.log`；它替代同范围315项中间结果。新1200条消息测试逐段hash全覆盖，tracemalloc峰值低于编码字符数一半；不是RSS或整个Compact绝对内存界。

三类摘要无效回复的纠正也必须保持同一预算，EOF取消、估算/响应后取消、UTF8错误和来源变动拒绝返回覆盖。Astra max独立只读复核的异常优先级问题已修：先前surrogate不能遮蔽后续JSON失败应有的str回退。实际模型调用仍为内存替身；未启动Gateway、未部署、未访问供应商，线上CI未作为证据。原4项主线fake Store签名适配失败仍开放，不以本轮通过宣称整体严格gate通过。

Ruff、doc sync、import boundaries零发现、strict code-size hard=0且原基线未改及diff均通过；clean-package在纳入新文件后通过。

建议下一步：继续原来源与覆盖链的正文常驻问题；只读审查可并行，writer/CAS保持单owner。

## 第12.4项保留历史完整投影（2026-09-23，本地）

六文件73 passed（15.88秒），日志 `/tmp/decision_retained_joint_20260923.log`：compact_retained_history、gateway_compact_recovery、subagent_compact_recovery、background_compact_recovery、gateway_child_compact_scope_application、compact_transcript_media_partition，均为test_前缀。新增16项中三宿主seed及同次真实冻结候选完整保留最早媒体/工具回合；两个原生协议small最终post_json载荷完整，large60万字符先完整捕获，后容量压力与未知模态明确失败、零业务HTTP和零CAS。HTTP为内存替身，未真实联网，不能当作供应商媒体容量验收。

初版测试4 passed/6 failed为超容量样本误期望发送，产品容量门未更改；修订后的可发送/应拒绝分组先16通过，再纳入73项联合。原4项主线独占fake Store签名失败仍待集成，不以本片测试覆盖它们，不称整体严格gate通过。10文件相邻回归416 passed（62.41秒），日志 `/tmp/decision_retained_adjacent_20260923.log`；包含新16项复验，不与前73累加。Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff通过；clean-package在新测试纳入版本管理后通过。仅本地候选，未推送部署；线上CI未作为验收来源。

建议下一步：沿原摘要分段接有界来源，再做授权测试机真实组合验收；只读审查可并行，writer与共享Gateway保持单owner。

## 第12.4项固定来源范围筛选（2026-09-23，本地）

12.4固定来源范围筛选已本地实现：完整尾界内两遍校验后只保留范围内未覆盖正文；后台先读任务事实，recent_limit=0也不提前全载正文。12文件326项通过，另4项主线独占测试的旧Store签名尚待集成适配，不能称整体gate通过；ID索引、未压正文和覆盖链仍非完全有界，11/18不变。

12文件清单：conversation_message_selection、conversation_message_scan、background_scoped_compact、background_context_runtime_errors、background_prepared_context、background_compact_recovery、runtime_module_boundaries、conversation_store、compact_scoped_transcript、gateway_child_compact_scope_application、gateway_compact_deferred_source、gateway_conversation_control（均为test_前缀）。命令使用pytest -o addopts= -q --tb=short，30.05秒；失败node及原日志见[容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。没有真实调用、部署或推送；线上CI未作为验收来源。
本片非pytest守卫现已通过：Ruff、doc sync、import boundaries零发现、strict code-size hard=0（未改基线）、diff和clean-package。首次Ruff发现新增测试的两处导入格式，doc sync发现Gateway注释/模块文档遗漏，clean-package发现两份新文件未纳入版本管理；均已修正。主线独占4项测试接口失败仍开放，因此整体本地严格gate未通过，不推送；线上CI未作为验收来源。


建议下一步：主线适配独占夹具后再联验，本线继续保留历史完整投影；只读审查可并行。

## 第12.4项消息扫描底座（2026-09-23，本地）

六个文件定向联合153 passed（3.96秒），日志 `/tmp/decision_message_scan_final_20260923.log`：message_scan/message_stream/history_paging/conversation_store/cli_run_conversation/background_owner_delivery_commit。真实临时文件验证固定EOF、页字节预算与Unicode、半行等待和幂等写前拒绝、坏行游标回滚、display过滤、截断与并发同key；2000行历史禁止全量recent_report，tracemalloc峰值低于文件体积三分之一。没有真实模型调用或Gateway重启，非Compact全链/绝对内存上限证明。

只读复核修正了合法JSON无LF仍可拼坏后续追加、非对象JSON错误分类和半行超预算判定；完整LF算法复用原history_page并移至store_io，未新建第二实现。首轮五文件130通过，扩展命令一次文件名错误导致零测试，修正后的最终联合才作验收。本片Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff及clean-package均通过；本地严格gate已通过，未推送部署，线上CI未作为验收来源。建议下一步沿原scope/writer/CAS接入有界来源，独立只读审查可并行，12.4整项仍开放。

## 集成安装版真实 TUI（2026-09-23）

319004926独立wheel/venv/HOME已在获授权测试机验三轮普通中文：关闭零Jev请求；2秒期限失败自动保留M2.7；4秒选模need_data及能力建议成功，均完成一次读取工具轮。新增3次Jev HTTP，累计57次；原供应商缓存读回有部分缺字段，总输入无节省结论。原终态观察字段完整，初次汇总遗漏已纠正，无生产修复。设置原CAS恢复关闭，TUI退出、队列清空、全部HTTP有终态后正常停止候选Gateway，8420无监听。完整请求ID、usage口径及局限见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md#集成安装版原生-tui-对照2026-09-23)。

本轮只证明集成文字链与失败保留，不抵充媒体、超大历史、Compact后缓存或跨模型主会话验收。建议下一步继续12.4有界来源读取；只读审核可并行，测试机与恢复writer单一owner。

## 短期限回归阶段分离（2026-09-23，本地）

旧全仓8项失败按原node ID复核：未修改前定向8 passed。原healthy-long失败堆栈明确在loopback socket.connect阶段超时；其余首行0或first_event结果不能证明发生了stream_idle错误。原loopback provider明确禁用代理，web_fetch走校验IP直连；web_fetch的5秒超时尚缺阶段证据，不归因为环境代理或直接宣称已修。两项决策探测原1秒期限发生worker尚未退出，不能据此推断协议结果。

四个测试文件现在区分连接/首事件与短idle：非首事件用例显式首事件10秒，原短idle、有效data数量及typed stage保持；时间断言从第一条data计算，失败附阶段和耗时。成功/invalid-answer探测显式5秒，专用timeout/cancel及非法参数仍用原短预算。没有修改生产默认期限、重试或失败保留逻辑。四文件85 passed、1既有xpassed；与原web_fetch文件联合 **91 passed、1既有xpassed（14.99秒）**，日志 `/tmp/decision_timeout_stages_20260923.log`。这消除了成功/idle测试对极短连接调度的依赖，不抵充全仓通过，web_fetch历史根因继续开放。 本片Ruff、doc sync、strict code-size（hard=0）、diff及clean-package通过；本地严格gate已通过，未推送远端，线上CI没有作为验收来源。

建议下一步：以本片分阶段断言检查下一次自然回归；web_fetch若再现，先采集连接/handler/首字节证据，不再循环重跑到绿。独立只读审查可并行，生产HTTP期限保持原owner负责。

## 第 12.4 项媒体集成与未知模态边界（本地切片已验）

解决媒体引用被当作完整文字计量、或未读附件却取得摘要覆盖的问题。以 `81bdf9579` 为基线整合媒体线 `3adb61904`；媒体原件仍在原owner内容寻址目录，UserTurn保留input_ids及media，三宿主原生carry不另建存储。原生发送和出站投影复用同一后端组包。

共享 `backends/request_content.py` 在适配器过滤前检查原IR和原始消息。当前模型可继续携带原文字思考；跨模型候选不搬运供应商推理签名。未知模态不额外探测候选，保留原模型；普通原模型媒体请求继续发送。自动Compact跳过未知计量，供应商已报overflow的强制恢复返回COMPACT_REQUEST_PROJECTION_UNKNOWN，不摘要、不提交checkpoint。字节预算只约束文件展开，不是视觉token或窗口容量证明。

transcript Compact只摘要首个非文本原生信封所在完整回合之前的安全前缀，媒体回合及其后所有行原序保留，游标不越过未读来源。分段器在JSON化前拒绝非文本块，不能通过引用字符串取得覆盖；工具原账和旧检查点事实不重写。无完整文字前缀时typed拒绝，不伪造空摘要。

媒体线此前官方M3图片/视频/重连证据属于原提交，不能当作本次集成版真实验收。本次pytest采用隔离HOME、真实本地媒体读取和业务工具，仅末端HTTP替身；尚未部署测试机或重启共享Gateway。12.4、真实缓存、超大历史及旧全仓八项失败仍开放，11/18清单数不变。

建议下一步：联合定向与严格gate后保留可审查本地提交，再协调决策线测试机验收窗口。测试和只读审查可并行，共享Gateway及恢复writer保持单一owner。

本片55文件联合 **1419 passed、4项既有xfail**，退出码0；清单 `/tmp/compact_media_joint_20260923.files`，日志同名 `.log`。pytest配置和命令各带一次-q，原日志只有逐项结果和进度；统计为1419个通过标记及4个预期失败标记，不补猜运行秒数。初次媒体兼容检查144 passed、2 failed来自UserTurn新增字段的旧位置参数，已改显式media关键字并纳入上述联验；容量/选模91项、媒体工具轮4项、transcript26项均被最终联合覆盖，不累加计数。首次guard的导入排序和深层嵌套已修，新增文件暂未登记的打包提示在纳入本片后消除。

Ruff、doc sync、import boundaries零发现、strict code-size hard=0且基线未改、diff和clean-package通过。本地严格gate已通过；没有push、部署、真实供应商调用或Gateway重启，线上CI没有作为验收来源。未知媒体的强制恢复仍明确拒绝，不能宣称已实现完整多模态容量计量；旧全仓八项失败保持。

## 第 12.4 项外层溢出原生历史接续（本地切片已验）

Gateway、后台及child的同宿主逻辑回合现在携带真实原生IR，不从归档短预览重建正文。原循环在typed overflow后按准确attempt释放未提交插话，携带精确input_ids；释放失败仍经原partial出口保存已完成事实。恢复重新准备权限和provider前缀，旧调用四元引用保持，私有tool_round归并标记清除。摘要由prefix或IR唯一承载，强制恢复没有可压来源报告COMPACT_SOURCE_EMPTY。

首轮44文件联合 **1131 passed、12 failed、4 xfailed，129.37秒**，日志 `/tmp/compact_carry_joint_20260923.log`。6项Gateway旧fixture绕过observer触发eager提交导致旧view身份拒绝；1项child旧断言把历史第一张卡当当前卡；4项mixed来源忽略carry新增真实运行事实；1项真实空来源被误报投影变化。分别迁移真实安全点、核对最新卡并保留历史、保持工具来源/覆盖不变而保留控制事实、修正typed空来源判定。没有放宽来源身份或容量门。

新增同owner/request/run/task/逻辑turn/view校验、主新attempt与child固定attempt、深复制、同文不同input_ids、部分释放拒绝、完整工具IR-only零archive、释放错误保存和transcript-only摘要唯一性回归。无任务后台另验证同轮request_id固定、冻结thread视图无须补task属性，不误建持久任务。最终44文件联合 **1154 passed、4 xfailed，135.39秒**；清单 `/tmp/compact_carry_final_20260923.files`，日志同名 `.log`。四项均为既有预期失败，本片未新增xfail；taskless不新建会话任务的断言补充后两协议另跑 **2 passed，5.92秒**。Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff和clean-package均通过；本地严格gate已通过，线上CI没有作为验收来源。

测试仅使用pytest隔离HOME、隔离文件和fake末端HTTP；没有真实供应商、部署或Gateway重启。决策线测试机已获用户授权用于后续隔离验收。媒体线合入、真实缓存、超大历史和旧全仓八项失败仍开放，12.4不勾选。

## 第 12.4 项原生工具 IR 来源（本地切片已验）

原生工具完整往返与archive按同一四元身份分区；摘要优先真实IR正文、保留IR原序回放。完整配对回放的归档不重复生成handoff，未完成组和未知身份继续保留；无旧handoff时插入尚未覆盖的归档。严格机械回退完整附旧摘要和本次原文，分段失败或供应商标明截断时typed拒绝，均沿原容量门和单CAS。

首轮38文件联合 **1027 passed、7 failed，125.15秒**，日志 `/tmp/compact_ir_joint_20260923.log`。四项旧宿主回调仍在公共IR投影前捕获候选导致wire断言失配，改捕获完整投影后的候选，保留实际HTTP逐字相等；两项旧断言预期归档纯文本后缀，现验证JSON原生消息完整还原；一项手工plan缺新增source_ir_history，补显式空元组。更早小组11项失败保留在 `/tmp/compact_ir_initial_20260923.log`：旧重复保留区拒绝、旧no-op签名、背景候选捕获点、初始+恢复各冻结一次的计数假设；未通过放宽容量或去掉实际wire核对消除失败。

新增5项真实工具安全点测试：原read_file执行器及recorder生成短归档预览与完整ToolResult，两协议同ref摘要仅一次；另Anthropic仅IR、零archive仍原单CAS后发送获选HTTP，取消及过大均零CAS/业务请求。HTTP末端使用fake供应商，工具实际读取隔离目录文件，不把此证据称为真实模型或外层自动接续。外层overflow重跑仍从archive重建，原生IR传递未完成；12.4、旧全仓八项失败、媒体、缓存和超大历史边界仍开放。

最终39文件联合 **1039 passed，136.66秒**，清单 `/tmp/compact_ir_final_20260923.files`，日志 `/tmp/compact_ir_final_20260923.log`。Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff及clean-package通过；打包首检只因三个新文件尚未纳入索引失败，纳入本片后复查通过。尺寸首检发现公共投影函数过长，按IR位置变换职责拆出纯函数后通过，未放宽阈值。没有push、部署、重启、真实供应商或线上CI证据；本地严格gate通过不抵充旧全仓八项失败。

## 第 12.4 项首次自动准备与手动来源（本地切片已验）

Gateway/child/后台首次只加载来源，在原PromptBuilder冻结完整输入后、选模及拒绝回退基线之前自动Compact；恢复仍必须真实提交。容量充足和已知无可覆盖来源保留原输入，未知投影/取消不伪装成no-op。无来源仍交原选模和发送压力门裁决，不修改原触发线来强行放行。手动按车道内thread-scope来源判空和估算，回执不包含未知的下一业务请求。

首次联合 **995 passed、7 failed**（155.03秒）：后台新增测试的窗口准备不当、旧假宿主把首次也强行置为已提交、旧能力统计把首次/摘要重入当恢复。修正精确测试时点后，另发现真实产品预算边界：首次auto已提交后再恢复八次会超额；现首次真实提交也计入同一八次上限，首次no-op仍不计。原失败日志保留 `/tmp/compact_initial_joint_20260923.log`。

最终联合 **1005 passed，144.08秒**，文件清单 `/tmp/compact_initial_joint_20260923.files`，日志 `/tmp/compact_initial_final_20260923.log`。手动估算helper收口后同文件 **128 passed**；补充已知空源位于原trigger与实际input ceiling之间、自动采用较大候选且零Compact/CAS的回归后Gateway文件 **16 passed**。这些计数包含重复覆盖，不累加为不同用例总数。双协议child验证首次no-op后工具轮、一次原准备/候选wire相等，以及压缩后Jev采用延续第二工具轮。

Ruff、doc sync、strict code-size hard=0（基线未变）、diff及clean-package通过。没有推送、部署、重启或供应商收费请求；本片不关闭真实IR混合来源、媒体、真实缓存或旧全仓八项失败。第12.4仍未整项完成。

现场隔离事故单独记录：独立诊断直接调用依赖pytest autouse的fixture，绕过临时MY_AGENT_HOME，误写本机owner模型目录及测试线程。已在原锁内隔离确证的12组测试模型/provider；根据迁移源码确认新增字段来源，显式反迁移并用真实安装版reader验证原有42模型/7provider及selected保持。原件和操作清单只存本机私有证据，不入仓库、不算产品验收。后续独立脚本必须进程启动前指定临时MY_AGENT_HOME；真实测试只使用已声明隔离home，禁止依赖导入fixture获得隐式隔离。 后续精确隔离测试线程/两条消息/用量/过期claim及测试快照，共7文件；后续按相同请求/任务身份及SHA另隔离1份runtime_facts/task.json，共8文件。两索引只移除仍指测试线程的值，其他项不变。新真实请求已更新latest快照，未触碰。独立误建local_store仅有1条该测试消息记录，确认无打开句柄后整目录隔离，日常工作区记忆库未动；不能把这次事故表述为“没有记忆写入”。共享runtime.db保留审计，原全树终态API将孤立测试TaskRun从created收口为failed，3个已failed的attempt及长期Task身份保持。

## 第 12.4 项混合来源与三宿主活动归档（本地切片已验）

Gateway/child无transcript活动归档已接完整恢复准备、候选计量、原CAS和同次发送；混合transcript+carried同时读取所选消息和每条工具的原模型投影，一次候选封印双来源。完整归档不裁剪、未知身份保留。空摘要/工具调用的机械回退仍携全部工具材料；过大、取消和代次冲突不能发送恢复业务，普通故障保留原可接受候选时必须连同其全部材料返回。

混合HTTP九项通过：两协议×普通/独立任务逐字核对获选候选与实际HTTP，单检查点含消息和工具双覆盖，未知身份保留、其他任务隔离；过大、摘要失败、取消和CAS冲突不发送恢复业务。另一个16K窗口用例直接从真实恢复安全点进入：同一短摘要、同一冻结材料，纯transcript候选15,174 tokens，联合替换14,084，原接受门14,400；同时验证1,024输出预留下前者超窗、后者可发，代次只从0到1。数字是本地估算，不是供应商usage。

小窗口用例最初从完整外层进入，被首轮正常自动Compact抢先提交，随后模拟overflow导致无新来源；已定位为测试准备错误，保留完整外层已有用例，新用例单独验证原恢复安全点，不通过改产品阈值绕过问题。

取消定向三文件43项通过：摘要/投影直接ToolCancelled、第二候选取消、92%提交回调取消均不发布候选或累计熔断；普通界面错误继续沿原容错。活动92%已落盘候选可以孤立保留，但原提交链不可见。日志 `/tmp/compact_mixed_cancellation_20260923.log`。

首轮31文件联合725通过、4失败（`/tmp/compact_mixed_joint_20260923.log`）：三项child旧fake-run绕过真实renderer/select或断言旧报错；一项活动摘要夹具仍提供旧visible_records而未提供准确source_records。后者已改用实际工具模型投影并额外断言原材料进入摘要，13项通过；child的1/9代改为真实runner/provider/工具链和原CAS，逐代验证同一attempt与原归档，12项通过。不将首轮失败删除。

最终32文件联合 **738 passed，75.48秒**，日志 `/tmp/compact_mixed_final_20260923.log`，文件清单为同名 `.files`。Ruff、doc sync、导入边界0发现、strict code-size hard=0（基线未改）、diff和clean-package通过。本地严格gate只覆盖本片，不关闭历史全仓八项失败，也不宣称第12项全部完成。

本片无供应商请求、安装版TUI、部署、重启或线上CI；初次/手动、真正native工具IR组合、媒体及真实缓存仍待验。前轮全仓八项失败独立保留。测试机可用于后续实际验收，目前没有安装本片。

## 第 12.4 项后台完整恢复与三宿主同视图（本地）

后台transcript、普通空transcript活动归档和narrow审计恢复已接公共PreparedCompactRecovery；Gateway/child共用原canonical loader的scope/view准备。后台同片范围冻结、候选完整计量、CAS后获胜view回填及原参数继续发送按同一链验收。原自动Compact不在完整捕获前抢先推进代次；实际业务请求不包含提交时才产生的内部checkpoint ID。

新增后台HTTP替身16例覆盖两协议×普通/独立任务transcript、两协议×普通/窄审计活动归档，以及摘要瞬时错误、未知IR、过大候选、取消、CAS冲突不恢复发送。候选payload与实际HTTP入口字节对照，不用模型正文判定摘要调用。活动入口23例使用真实Store/checkpoint/CAS，覆盖完整容量/输出预留、停止时点、原projection材料身份、未知四元身份保留和局部非空evidence不继承全局值。纯IR投影保持媒体、插话、未转发guidance及去重；相关六文件最终联合 **91 passed**，日志 `/tmp/background_complete_final_20260923.log`。

最终25文件联合 **657 passed，67.69秒**，日志 `/tmp/compact_complete_recovery_final_20260923.log`，覆盖三宿主准备/恢复/能力展示、后台运行、原生IR、完整请求投影和作用域检查点。Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0，基线未变）、diff和clean-package通过；首次打包检查仅因新增文件未纳入索引失败，明确纳入本片后复查通过。这里只证明本片定向与严格检查通过，不把旧全仓八项失败改写为通过。

旧fake agent.run测试绕开renderer/select，现按明确defer/宿主已提交语义验证控制流；不把fake声明提交算真实CAS证据，后者由上述及Gateway/child实际HTTP材料测试覆盖。初次14文件联合有2项child旧params对象identity断言失败，CAS回填正式view后参数确有新对象；已改为检验候选材料相等、获胜checkpoint、恢复及后续工具轮共用新params，原失败日志保留 `/tmp/compact_complete_recovery_joint_20260923.log`。

本片没有供应商真实请求、安装版TUI、部署、重启或线上CI证据。初次/手动、其它宿主活动归档完整计量和混合transcript+active联合候选仍待实现；超大历史无界读取及前轮全仓八项失败也未关闭。12.4保持未完成。测试机已授权且只读核对可达，尚未用其部署本片。

## 第 12.4 项后台实际摘要视图接线（本地）

后台普通/task/turn范围现把同一AppliedCompactContext传给历史种子、上下文、工具过滤和摘要器。局部transcript与live/carried均显式写scope/base，仍复用原检查点/CAS；完整归档与运行预算保留。原历史读取错误测试已改为真实canonical入口，旧无checkpoint的假摘要夹具改为真实writer/CAS。

18文件联合 **420 passed**，日志 `/tmp/background_scope_joint_20260923.log`。新增scoped transcript覆盖交错范围、无提交和竞争CAS；后台4项使用真实SimpleAgent/Store/checkpoint/CAS和原工具循环到模型消息投影，仅摘要替身，确认原三类材料丢失已修且正文只注入一次。应用视图测试另外覆盖旧view不跟随新链隐藏、错线程拒绝、未知身份保留、narrow无seed、native/text摘要消费和媒体块保持。

最后native/text分支和普通历史摘要去重补强后，应用视图、后台范围、native IR、Gateway/child恢复及接续七文件 **102 passed**；空工具记录也核对显式view线程后，应用视图/后台范围/能力展示三文件 **29 passed**。这些是后续定向复验，不与420相加。Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0，基线未变）、diff与clean-package均通过。

此片没有真实HTTP、供应商、部署、重启或线上CI证据。后台完整恢复候选与首个实际payload、narrow活动IR完整计量、Gateway/child准备边界的同view绑定仍未完成；不勾选12.4，不抵充前轮全仓8项失败。超大原文读取也仍未验通过。

## 第 12.4 项作用域检查点与工具来源底座（本地）

原writer现统一写v3，scope、摘要基础与精确覆盖沿同一提交链，原generation CAS保持唯一；局部提交可以保留全线程摘要/游标。新reader只收集实际适用摘要的base覆盖。旧v1/v2显式读取并核对原摘要hash，未知旧工具身份不伪装为精确引用。

21文件联合 **562 passed**：Gateway与child完整恢复/接续、原Compact和存储、native IR、后台准备及运行、工具归档、记忆续接、超时恢复和模型生成。版本/摘要完整性补强后，`test_compact_scoped_checkpoint.py`、`test_native_tool_ir_compact_and_orphan_sweep.py`、`test_model_turn_identity.py` 三文件 **74 passed**。不将重复运行累加成独立覆盖数。

新增回归使用临时原Store、writer/CAS及真实投影，覆盖thread与task交错的摘要base、局部CAS、孤立候选、竞争、旧版本、摘要hash损坏和schema降级；相同call_id跨run/attempt/模型轮仍可分列source/retained，未知旧记录保留。原工具artifact/index重载保留四元身份，相同正文跨执行轮不覆盖旧artifact。原模型轮生成也曾在同attempt重启工作片后碰撞，真实检查点过滤复现了误隐藏；在原turn_id加入唯一nonce后回归通过，原数字序号不变。

首次核心回归因旧schema断言和缺调用身份的夹具失败，已按真实新合同更新；原完整调用与未知旧记录分别测试。扩大回归发现未知重复记录被误算新增执行进展，已修原进展判断并保留未知材料。所有复现及本片测试均无真实供应商请求，未部署或重启；前轮全仓8项失败仍未收口，线上CI不作为证据。

边界：本片只完成scope/base/coverage与来源身份底座。后台宿主仍须选择并实际应用同一个view，再接完整请求恢复；原detached/narrow三类问题尚未关闭，初次/手动及真实缓存也待验。交接见[后台准备与后续底座](docs/tasks/DECISION_MODEL_BACKGROUND_PREPARATION_HANDOFF.md)。

本片Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0，尺寸基线未改）、diff与clean-package检查通过；仅保存本地提交，不推送、部署或把线上CI当验收来源。

## 第 12.4 项后台一次准备与纯投影（本地前置片）

`background_context.py` 将可能写任务进度的事实准备与无副作用渲染分离；`background_history_seed.py` 携带同次冻结的任务范围，正常种子与候选共用原历史投影。没有接通新的后台 Compact 恢复分支，也没有改变持久 schema。

后台主运行、上下文读取错误、owner 投递与能力展示四文件联合 **201 passed**。新增 `test_background_prepared_context.py` **6 passed**：重复渲染不读 store、不重复进度对账，借用请求/配置/wake 修改不影响冻结结果；detached 锚点与兄弟任务隔离、创建后摘要排除、窄审计无旧聊天，以及 unreadable/disabled 不伪造可用投影。此次未调用真实供应商、未部署或重启；不抵充前轮全仓 8 项失败。

Astra max 独立诊断使用真实 SimpleAgent、临时 ConversationStore 和原 checkpoint/CAS，仅摘要模型为替身，复现三个原有材料丢失边界：detached transcript 压缩后全局游标推进但新摘要被隔离；detached 与 narrow audit 的活动工具压缩后工具记录被隐藏，而该任务不消费新摘要。原始持久记录仍在，丢失发生于模型恢复材料投影；不是实际 HTTP 或供应商验收。后续必须先完善唯一 checkpoint 的作用域与替代关系，再接完整后台恢复。

本片 Ruff、doc sync、导入边界（0 发现）、strict code-size（hard=0，基线未改）、diff 与 clean-package 检查通过。打包检查首次因新增测试尚未进入 Git 索引失败，纳入本片后复查通过；没有忽略文件或绕过检查。并行边界与复现方式见[后台准备交接](docs/tasks/DECISION_MODEL_BACKGROUND_PREPARATION_HANDOFF.md)。

## 第 12.4 项子代理完整恢复与公共实现（本地）

Gateway 的完整恢复协调提到 `agent_core/compact_request_recovery.py`；Gateway、child 共享一次冻结、完整候选计量、摘要错误隔离及原 checkpoint/CAS，正常选模也共用 `tool_request_capture.py`。child overflow 在下一次真实 `agent.run` 准备后提交并同次发送，原独立历史、run/attempt 和权限保持。

19 文件联合 **408 passed**：新增 `test_subagent_compact_recovery.py` 8 项，加既有完整投影、Gateway 来源/恢复/工具接续/选模/错误、三宿主能力展示、child runtime/首请求、进度/熔断/预算及原生 IR 回归。新增 HTTP 替身测试覆盖 Anthropic/OpenAI × 工具开关，逐字核对候选和实际出站请求，准备恰好两次、候选一次、业务/摘要/恢复代次 0→0→1；另验 run token 取消、并发代次、摘要瞬时错误和来源加载失败均不发送恢复业务。

尺寸检查发现原子代理执行函数超限后，按职责拆出单次恢复作用域执行，未改尺寸基线；拆分后 child 三文件 **31 passed**。旧展示测试改为核对真实准备后的清除状态与实际 wire，不再要求 defer 前重做准备；摘要继承原 builder 的无候选占位段，明确断言旧推荐卡片未复活。没有真实网络、安装版 TUI、部署或重启；本片不关闭前轮全仓 8 项失败，也不证明真实缓存命中。

独立 Astra max 审查未发现具体阻塞问题；其两协议 × 两接续场景已保存为 `test_subagent_compact_recovery_continuation.py`，另 **4 passed**。恢复后实际 `read_file` 及最终业务轮保持同一新参数、历史与取消令牌；无旧历史时活动归档 CAS 先于第二次准备，真实读取仅一次，另一 child 代次不变。本片 Ruff、doc sync、导入边界零发现、strict code-size（hard=0、基线未改）、差异及 clean-package 检查通过；仅本地检查点，不推送或部署。

后台、初次加载和手动入口仍待接入完整恢复。后台须先冻结可能写任务进度的上下文，并保留 detached task 的历史锚点/lineage；不能补入 owner 历史来代替窄审计事件的空种子。文件与并行边界见[子代理恢复交接](docs/tasks/DECISION_MODEL_CHILD_COMPACT_HANDOFF.md)。

## 第 12.4 项 Gateway 完整压缩恢复请求（本地）

Gateway overflow 的恢复轮现在先保留原始历史来源，待真实提示、工具和运行材料准备完毕，再用完整下一请求计量候选；原 checkpoint/CAS 成功后直接使用获选材料发送。16 个相关测试文件联合 **337 passed**，新增四文件共 **24 项**覆盖只读来源、两种供应商协议、工具开关、关闭/应用模式、候选回退、并发代次、真实 run token 取消、未知投影、摘要瞬时/首事件超时，以及恢复后真实 `read_file` 工具接续。实际 HTTP 由内存替身捕获，未调用真实模型。

联合回归首次出现的两项失败分别是旧错误夹具缺少内部 `defer_compact` 字段，以及新接续测试错误地禁止 RuntimeFacts 保留原调用引用；已修正夹具和原生工具块断言，两个文件 65 项通过后，上述完整 16 文件组再次通过。摘要失败不得转入普通业务瞬时重试，未提交恢复不能重新发送旧请求。

本片不代表 12.4 整项完成：child、后台、初次加载及手动 Compact 尚未接入完整恢复输入；真实跨模型缓存与供应商窗口仍待验收。前轮全仓 8 项短期限失败保留未收口。没有推送、部署或重启测试机，线上 CI 不作为本片证据。

本片 Ruff、导入边界（0 发现）、strict code-size（hard=0）、差异及 clean-package 检查通过，尺寸基线未改；文档同步补齐 Gateway 模块进度与结构说明后验收。用户新增授权决策线测试机，当前仅核对既有运行环境；部署/重启仍须与占用该 Gateway 的并行任务协调，不能将可用机器计作实测完成。

## 第 12.4 项注入片段准备（本地前置片）

`test_tool_request_projection.py`、`test_gateway_model_adoption.py` 共 **65 passed**；`test_prompting_builder.py`、`test_prompting.py`、`test_subagent_first_request_selection.py` 共 **139 passed**，合计 **204 passed**。原 PromptRenderInput 保存注入片段元组，文本与原生路径保留原 join 和缓存布局；新增两协议用例验证重复正文、空片段、内嵌标题及调用方改动不会使候选误改其它片段，纯渲染不会重新准备提示或读取时间。没有实际模型调用；完整 Compact 候选与恢复请求的 payload 对照尚未接线，不计为12.4完成，也不覆盖前轮全仓失败。

本片 Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0）、diff check 和 clean-package 均通过；未新增配置或长期文件，尺寸基线不变。只在独立开发工作区保存，未推送、部署或重启测试机。

## 第 12 项输出预留接受门（本地首片）

`test_gateway_conversation_compact.py` 与 `test_runtime_context_pressure.py` 共 **72 passed**。transcript 自动触发和候选接受现在复用普通请求的已知输出预留；先过输入容量门，再选 recovery target 或原可接受候选，Context 输入显示不加未来输出。新增10种受控估算矩阵保留原checkpoint/CAS与失败记账，覆盖等于边界、不足输出空间、原保留候选、未知cap和OAuth无cap；没有真实网络调用。完整恢复请求计量仍待12.4接入，不把此片当供应商窗口或第12项完整验收。

本片 Ruff、doc sync、导入边界（0发现）、strict code-size（hard=0）、diff check、clean-package 均通过，尺寸基线未改。仅本地检查点，上一轮全仓8项短期限失败仍保持未收口；本片未重跑全仓、未推送或部署。

## 第 12 项原生输入计量共源与同轮接续（本地）

`test_tool_request_projection.py`、`test_runtime_context_pressure.py`、`test_tool_model_generation.py`、`test_context_pressure_native_trigger.py`：**83 passed**。预检与发送共用 guidance/孤儿配对清扫及 ToolChoice，原本用孤立 ToolResult 撑大容量的旧夹具改为合法工具往返，并新增“出站已移除的孤立结果不触发压缩”对照。`provider_context_observation.v3` 拒绝旧 v2 校准；实际 snapshot 仍可 hydrate 校准，只有抽出的投影计量是纯函数。

原 native Compact/消息流、子代理首请求、Gateway 采用/观察、上下文预算、工具统一与线程存储八文件 **266 passed**。没有联网、重启或部署；本片不覆盖完整恢复请求、供应商 tokenizer/缓存或前轮全仓短期限 HTTP 失败，完整严格 gate 仍未通过。

独立审查补强 payload 对照为原 `_do_backend_generate`→backend→内存 HTTP 捕获，覆盖 none/specific 的真实 `thinking_disabled`。Gateway 与 child 候选均保留原工具参数存在性，由原 backend 筛选；没有工具时不多传 choice 或关闭 thinking。子代理首请求文件 **34 passed**，包含 Anthropic/MiniMax-M3 与 OpenAI/DeepSeek-v4-flash 的 tools 开/关 × none 四种组合，逐一比较原容量估算与实际 wire payload；上述四文件加 Gateway/child 自动采用六文件联合 **153 passed**。此前只在两边直接调用 backend.generate 的对照不足以证明该包装字段，旧结论以本次证据收紧。

三宿主展示接续已覆盖新任务和已有前台主 run、同片一次决策、显式空选择、失败或失效后不重决策，以及下一片重置。后台实际身份沿原 `LocalRunControl`/core 发布回传，原确认回调拒绝和关闭仍阻止执行。后台完整 runtime、Gateway 展示 Compact、新后台12项及本地控制句柄四文件 **211 passed**；child 新展示11项、原 Compact12项、原同 attempt Goal 续轮2项 **25 passed**。child 二次 prepare 在配置恢复后仍读取已清除的原参数，不复活旧 frozen surface；缺 RunParams 不伪造已评估，None/空授权、取消不重试保持。三组定向回归合计389项通过；没有真实 Jev 网络请求、重启或部署，完整恢复输入和供应商缓存仍未验收。

本片 Ruff、暂存 diff 的 doc sync、导入边界（0发现）、strict code-size（hard=0、基线未改）、diff check 和 clean-package 均通过。仅保存隔离分支本地检查点，不推送/部署；下面保留的上一轮全仓失败尚未收口，不能将本片定向通过改写成全仓验收通过。

## 决策模型与插件基线合并后全仓回归（未通过）

在隔离决策分支合入插件已提交基线 `f04ec3a42`，并将决策线引用迁到唯一 `common.cancellation` 后，九文件交叉测试 **298 passed**，`test_decision_*.py` **685 passed**。全仓跑到终态为 **19,869 passed、8 failed、21 skipped、35 xfailed、5 xpassed**。八处失败均为本地 HTTP/流式首事件或显式决策探测的短期限测试；相同五文件第一次重跑 **6 failed、85 passed、1 xpassed**，第二次 **1 failed、90 passed、1 xpassed**，第三次 **91 passed、1 xpassed**。单独的流式正常用例与决策探测失败用例也通过；当前高负载下尚未证明确定根因，不能把第三次通过替代全仓 gate。未推送、部署或合并到 `main`，线上 CI 未作为验收来源。

## 决策模型早期全仓集成 gate（未通过）

隔离分支本地检查点 `ae539e38a` 与插件线已提交基线 `f04ec3a42` 从 `4974fe7` 分叉；在第7步负责人确认独立工作范围后，只在决策分支做未提交 merge，不动原仓库的 TUI 脏文件。六个实际冲突均为文档或生成的代码尺寸报告，双线记录保留并重新生成报告；生产代码由 Git 自动合并。随后交叉定向收集暴露插件线已删除 `tooling.cancellation`、决策线新增文件仍导入旧路径；17 个本线生产/测试文件改用唯一 `common.cancellation`，没有恢复 facade。插件命令、工具装配、子代理首轮采用、Gateway 模型选择及能力推荐九文件组合 **298 passed**；Ruff、compileall、导入边界零发现、文档同步、strict code-size、clean-package 与暂存差异检查通过。此证据不含完整合并后全仓回归，也不包含插件第7步和原仓库 TUI 未提交工作。

使用仓库现有虚拟环境（全局 Python 缺 dev extra 的 `hypothesis`、`pyte`）跑全仓 pytest。第一轮在 9,554 passed 时中断并定位六项 Jev 字节窗口误拦及一项插件命令用例；修复 Jev 后，排除该插件命令测试的第二轮在 10,490 passed 时定位普通 `task_local` 误入子代理发送栅栏；修复后第三轮在 11,840 passed 时定位旧记忆路由测试形参，三处相关 focused 均已通过。随后从记忆路由文件向后扫，在 1,542 passed 后由 `test_packaging.py::test_current_production_import_boundaries_have_no_unapproved_findings` 停止：本线新增的 Gateway 模型采用/观察、Compact 重建和 `user_config` 共有 **13 处**跨层导入。该守卫是架构硬门；不能加入白名单冒充通过。插件命令单测属并行“模块重构”线，本工作区没有修改对应实现或测试，待其 owner 交接后共同复核。当时 50 个新增文件未跟踪，`check_clean_package.py .` 因此失败；后续结构修复与暂存的结果见下文。**完整本地严格 gate 未通过，未提交/推送/合并/部署，也没有线上 CI 验收。**

后续结构修复把主会话选模应用编排移出 `gateway_parts`、同 turn Compact 跨层构造移到应用层，并将唯一 runner 线程本地上下文移到 `agent/runtime_context.py`；旧路径没有转发模块。`check_import_boundaries.py` 现为 **0 findings**。Gateway 观察/采用移位后 **65 passed**，Gateway/Compact **85 passed**，运行身份、设置、子代理、规划和 packaging 的 10 文件组合 **268 passed、4 xfailed**；Ruff 与生产模块 compileall 通过。插件命令用例仍待并行线基线对齐；打包门的后续结果见下文。

移位后全仓重跑曾在后台主代理 CLI 用例处看到空的中间消息；该用例单独重跑即通过。原等待条件只要任意消息出现就立即停止 worker，可能在最终正文持久化前读取占位消息。现等待期只以预期最终正文为完成条件，保留 5 秒有界期限；同用例独立重复 5 次均通过。此为测试时序修复，不改变后台主代理运行代码，也不把单独通过当作全仓通过。

该修复后的第一轮全仓在 **14,704 passed** 停于旧审计测试仍 patch 已拆走的 `_execute_create_subagents`；改为当前准备入口后，审计文件 **54 passed**。第二轮在 6,952 passed 遇到协作存储并发测试失败，该用例独立重复 30 次均通过；第三轮越过该位置，在 **14,789 passed** 停于恢复分类守卫：决策响应无效、探测失败、设置冲突及供应商响应超限四码未登记。现已按可选增强保留原主链、CAS 重读和响应缩小语义补入唯一 `ERROR_CONTRACTS`，恢复策略文件 **16 passed**，Ruff 与 strict code-size 通过。全仓尚需重跑到终态；并发偶发失败尚无稳定复现，不能当作已根除。排除并行插件命令用例的运行不等于完整严格 gate 通过。

补齐错误合同后的全仓回归（仅排除并行插件命令测试）跑到终态：**19,755 passed、1 failed、21 skipped、35 xfailed、5 xpassed、8 deselected**。唯一失败是旧 `test_tool_operation_managed_gate.py` 测试桩缺 `home_paths.root`，而已有插件 owner 装配需要这个可信根；与插件线负责人确认该测试不在其当前认领范围，且原仓库新版本已补同一字段。本隔离树同步测试桩后，该失败用例单独通过，工具装配、恢复策略与旧审计组合 **94 passed**。被排除的插件命令参数化用例独立执行为 **7 passed、1 failed**：旧基线对 `/plugins enable demo` 的期望文案与当前拒绝/查询回执不一致；不在本线修改插件产品语义。新增项目文件暂存后 `check_clean_package.py .` 与 `git diff --cached --check` 通过。完整全仓仍未在这些修复后再跑到终态，插件线基线尚未对齐，故完整严格 gate 仍未通过。

## 决策模型第 12 项 task_local 发送栅栏回归

全仓回归的新增定位：普通 `task_local` 带 `run_id` 却没有 canonical 子代理记录，首次发送栅栏误抛 `FileNotFoundError`。现只对这种未登记的局部运行跳过子代理专用标记；原局部运行、首次请求选模和 child 上下文三个文件联合 **55 passed**，Ruff 通过。全仓复跑仍在进行，不能因此宣称整体通过；详见[容量交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

同轮全仓回归发现旧 `test_memory_routing_context.py` 的唯一直接调用仍传 `_routed_memory_context_for_request(task_local=...)`，而 P5-A/记忆总闸已将该私有形参改为 `skip_formal_recall`；原测试期望的非隔离路由语义不变。同步参数后，路由、召回前、首轮记忆和原召回四文件 **57 passed**，Ruff 通过；不将此测试接线失败当成真实 Jev 质量问题。

## 决策模型 P5-E1 授权与输入预算原语（本地）

原 `decision_settings` v1→v2 保留 revision/overrides，增加默认关闭的实验能力和有界 thread 授权信封；原 `ModelCallLedger` 同锁内串行预留声明的完整输入量及请求数，未知发送/缺 usage 保守占用。新原语 52 项通过；设置、决策调用、账本、工具及 TUI 共 13 文件 347 项，加菜单索引集成 9 项，合计 **356 passed**。Ruff、doc sync、strict AST/code-size、diff 通过。TUI 只可开关能力，不建立实验许可；当前无可靠输入 token 上界、宿主用户授权入口与发送前硬门，实验联网仍失败关闭。本片不代表完整 E1/E2 或真实试验验收，见[交接](docs/tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md)。

## 决策模型 P5-D 主会话三轮隔离真实样本

原 Gateway TUI、隔离普通 user owner、`model_selection=apply` 做三次普通中文新会话任务。默认 2 秒 Jev 期限取消一次，自动保留官方 M2.7；原设置 CAS 临时延至单次 8 秒/阶段 10 秒后，Jev 分别给 `need_data`、当前 M2.7，主会话均完成且线程保持默认选择。真实 HTTP 旁观为 Jev 5 次（4 成功、1 取消）与官方 M2.7 4 次成功，无 M3/DeepSeek 请求；首轮决策输入未知，不能补零。测试后一次原 CAS 恢复三个字段，owner overrides hash 与开跑前相同，8431 停止，日常 8420 未动。此证据只证明安全保留，**没有**真实跨模型采用、容量或历史通过，见[交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_LIVE_HANDOFF.md)。

## 决策模型 P4-B 普通 user owner 中文配置复测

第一次隔离真实样本确认 `user_config` decision-only 已在主模型工具清单，但当前 Gateway 主回合拿不到可信 thread、约 8.9k 的 read 回执在原 4000 字符模型预览内缺 `revision`，实际五次 read 后 thread patch 被拒、四次猜测 owner revision 均 `STALE_VERSION`；设置未被修改。底层只修原工具的可信主回合 RunParams 线程来源和原投影序列化顺序，子代理缺自身线程不借父线程、legacy view/set 仍双门拒绝；相关两组本地 **91 + 63 passed**。第二次新 TUI 普通中文任务，模型自主 read(thread,14/0)→patch(thread,14/0) 成功，原持久回执及线程文件均为 14/1、enabled=true、stage_timeout=6、subagent_model=observe，最终回复准确；模型没有另发第三次 read。测试者仅在结束后用原 CAS reset 三项，thread 升至 14/2、overrides 清空，8431 停止，8420 不动。详见[交接](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_FIX_HANDOFF.md)。

主线加固缺失 owner 身份不能获得 legacy 动作的防护用例后，和设置、Gateway 模型采用及子代理首次请求的 13 文件组合 **353 passed**，相关 Ruff 通过；这是本地合同证据，不另算真实 Jev 样本。

## 决策模型 P5-C 自学习候选只读审计

[自学习审计](docs/tasks/DECISION_MODEL_SELF_LEARNING_AUDIT.md)核对现行 runner lesson Candidate、旧草稿迁移、Skill 快照和 guard；四个已有 focused 文件 **48 passed**。生产尚无 `enable_self_learning` / `my-agent learn`、Skill 提案/确认/写入服务，因此没有 Jev 自学习消费、正式 Skill 写入或真实验收；旧 README/指南的已实现说法已校正。正式 Skill 必须用户确认的开发规则仍有效。

## 决策模型原模型目录持久代次（本地）

私有目录 v5、共享发布 v2 在原保存事务轮换随机代次；已启用准备可在原锁自动初始化旧目录，普通读取与关闭不写。原快照/最终锁 guard 覆盖 provider/model/凭据/OAuth/shared 变化，缺来源保持未知。目录、Provider、OAuth、Gateway 八文件 focused **142 passed**，包括新 Python 进程读回、原 OS 锁竞争、保存失败和部分迁移；Ruff、strict AST、doc sync、diff 通过。此片尚无子代理自动采用或真实异模执行；见[目录代次交接](docs/tasks/DECISION_MODEL_CATALOG_GENERATION_HANDOFF.md)。
主线将该八文件与下方恢复原语五文件同跑，**280 passed**；原目录换代没有破坏设置 CAS 与恢复前值。

## 决策模型 P5-A 召回前补充查询首片

[P5-A 审计与实施记录](docs/tasks/DECISION_MODEL_PRE_RECALL_AUDIT.md)确认普通聊天尚无可信显式查历史结构化意图，Jev 不能直接跳过原召回。默认关闭的独立 `pre_recall` 点现已接正式上下文准备：原完整查询先召回；仅有剩余槽位和字符预算时，Jev 可建议一次有界补充查询，同原 scope 追加已确认的正式事实。候选检索不提前 touch，最终采用重读正式源；与 P3 召回后排序共用原阶段绝对期限。`test_decision_pre_recall.py`、`test_decision_recall.py`、`test_memory_condense_v2.py`、`test_memory_recall_v2.py`、`test_memory_first_loop.py`、`test_decision_settings.py` 联合 **136 passed**，相关 Ruff 通过；新增真实 JSONL 的受控漏召回样本证明可只追加第二条事实、只确认它的访问。隔离真实 Jev 两轮 off/observe/apply 共4次官方 HTTP，关闭零请求、每轮观察和应用各1次且实际版本 `jev-1.13.0`；应用样本原词面检索已命中两条事实，没有新增，诊断 `no_addition`，不可算召回质量收益。仍缺真实 Jev 已知漏召回与 Gateway 真实聊天对照，不计 P5-A 全项通过。

## 决策模型 P5-C 现有 Todo 优先建议首片

[规划审计](docs/tasks/DECISION_MODEL_PLANNING_AUDIT.md)核对现有 Todo、workflow plan、Goal、子代理创建的权威边界。默认关闭的 `planning` 首片现只在当前主代理 `task_progress(read)` 的原 canonical 回执后，对2–24个 open exact ID 建议一个优先评估项；观察、非选择、错误、超时或旧账本保持原回执，apply 也不改计划/Goal/派工。原 Todo 工具/设置/TUI 等六文件联合 **133 passed**，本线 Ruff 通过。隔离真实 Jev 两轮共3次HTTP尝试：4秒观察超时而原回执不变，8秒设置下关闭零请求、观察和应用成功且各输入783；应用仅追加 `todo-rollback` 软提示，原 Todo账未变。见[交接](docs/tasks/DECISION_MODEL_PLANNING_HANDOFF.md)。没有 Gateway TUI 或实际业务规划收益证明。

## 决策模型 P5-C 交付质量提示只读审计

[质量提示审计](docs/tasks/DECISION_MODEL_DELIVERY_QUALITY_AUDIT.md)核对原 verification、ready artifact、closeout、Goal 与 child 权威；推荐仅用原工具归档后的精确引用给主模型一个软复核焦点。六个现有测试文件 **4931 passed**，doc sync/diff 通过；这是原链回归，没有 Jev 生产接线或真实质量收益。

## 决策模型 P5-C 动作候选只读审计

[动作候选审计](docs/tasks/DECISION_MODEL_ACTION_CANDIDATE_AUDIT.md)核对现行 Computer Use/MCP/审批与尚未接生产的 Browser/OCR 候选；当前缺可信 observation/candidate ID 与失效代次，不接 Jev 生成坐标、命令或输入。原链 focused **128 passed**，doc sync/diff 通过；未接生产决策点或真实视觉任务。

扩大子代理工具循环回归发现旧 fake backend 的无约束 Mock `api_base` 不能被 JSON 编码，触发连接校准指纹异常。底层现只将标准 JSON 连接字段送入原加盐摘要；不透明值以进程内对象身份参与版本比较、不输出 `repr`。原失败测试与 context-pressure 整文件 **25 passed**，不涉及 Jev 网络或模型切换。

## 决策模型 P5-G 设置恢复基础原语（本地）

原 `decision_settings` 服务新增内部 `restore`，在原 owner→thread 锁序与完整两层 CAS 下同次写回 set/unset，成功只前进一次版本；后续用户修改、非法字段/范围及已删除模型引用不被旧恢复覆盖。设置、通知、作用范围、模型操作与主代理设置工具五文件联合 **138 passed**，Ruff、doc sync、diff 通过。它只是恢复原语，尚无自动实验、授权、请求前硬预算或真实收益验收；设计与缺口见[自实验审计](docs/tasks/DECISION_MODEL_SELF_EXPERIMENT_AUDIT.md)。

## 决策模型 P5-D 原线程选择版本首片（本地）

原 `ConversationThread` 增加单调模型选择版本、来源和最近显式版本；同 ID 显式选择也前进并终结 pending，旧 v10 全缺字段只在内存归一未知，部分或矛盾字段拒绝。与子代理新线程初始化联合 **170 passed**；Ruff、doc sync、diff 和原 strict AST 判据通过。尚无 Gateway 自动采用、真实异模主会话或持久恢复验收；见[交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md)。

Gateway 在准确会话车道后已有请求级 observe-only 建议首片：关闭时与原 Gateway 输入字节及文件读写路径等价，开启观察只记录建议与原回退事实，typed recovery/Compact 不重问。定向组合 **382 passed**，见[观察交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_OBSERVE_HANDOFF.md)；实际采用与跨模型执行仍待验。
Stage C 已把建议接到同一主请求的真实首次发送前：原 PromptBuilder 完整材料、IR/native 工具、候选 provider payload 与最终字节复核后，以目录代次→准确车道 T→原 thread CAS 提交；局部拒绝沿原模型一次，已提交/不确定不跨模型重发。最新本片 **34 passed**，此前 11 文件联合 **302 passed**，Ruff/doc sync/diff/严格 AST 通过；容量仍是有余量的工程估计，fake HTTP 不等于真实供应商验收。见[采用交接](docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md)。
普通中文配置请求的[P4-B 审计](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_AUDIT.md)曾用原工具、服务和 TUI 四文件联合 **86 passed**；该阶段只证明结构化 read/patch/reset、CAS 和菜单接线。首次[真实中文设置验收](docs/tasks/DECISION_MODEL_NATURAL_CONFIG_LIVE_HANDOFF.md)失败：隔离 user owner 原 manifest 中没有 `user_config`，原配置版本/hash 不变。其后修复及成功复测见本文件开头的 P4-B 记录；旧失败仍保留为定位证据，不代表当前状态。
子代理真实异模续验见[隔离交接](docs/tasks/DECISION_MODEL_CHILD_LIVE_HANDOFF.md)：六轮普通中文父任务均 completed，十个 child 均 DONE；三候选自然建议的官方 M3 和只允许 DeepSeek 作为替代候选时的 OpenCode DeepSeek 各有一条自动采用、真实首请求/工具后续轮及终态。该线新增20次 Jev HTTP，19次报告输入263,256，一次超时用量未知；和前序合计47次。原4秒下第五轮超时/冷却仍保留 M2.7，第六轮 DeepSeek 成功，无需用户逐 child 操作。第四轮 `selection_changed` 当时提交分支未留证、图片模态与多 owner 故障矩阵未验，P2-B/12/13 整项仍不关闭。

## 决策模型 P5-D 主会话选模只读审计

Gateway、原显式选模、model scope、history/Compact、250K/1M 窗口及 Responses 历史回放的源码边界已在 [P5-D 审计](docs/tasks/DECISION_MODEL_MAIN_MODEL_AUDIT.md)登记。原五文件 focused **75 passed**，doc sync/文档空白检查通过；这是已有合同回归，尚未实现主会话自动采用或真实异模切换。

## 决策模型子代理 pending 建议合同（本地）

创建前批量 Jev 建议仅作为 host-owned pending 原子写入新 child thread；有效模型保持继承，已存在 thread 不重植，显式同值/异值选模在原 CAS 内终结 pending。根/子/孙、refreeze、配置/连接变化、伪造属性及断点重试等 10 文件组合 **278 passed**，Ruff、doc sync、定向 diff 与原 strict AST 判据通过。旧 pending 仍可能对应已按继承模型执行过的 child，必须补首次请求资格与发送前栅栏后才能自动采用；详见[容量审计与交接](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

## 决策模型 P5-C 已归档网页阅读顺序首片（本地）

`external_material_order` 默认关闭，原 web_fetch extract 的 producer、Executor、归档、refs、账本及 text/native 展示接缝保持。独立设置与 TUI、完整来源、脱敏安全投影、期限/取消和原结果对照共 11 文件 **349 passed**；Ruff、doc sync、diff 与本片只读 AST strict 检查通过。通用 TUI 测试 helper 将固定 120ms 等待改为 3 秒内核对真实 UI 状态，两次旧 CAS 时序失败及最终复测均留证。随后隔离真实 Jev 5 次 HTTP 尝试：4 次成功，输入共8530，1次短期限用量未知；首轮 not_needed 保留原展示，第二样本完整建议自动追加2→3→1，原 archive/refs/hash/账均相同。页面源为本地受控材料，不证明真实互联网检索质量或安装版 TUI；详见 [P5-C 交接](docs/tasks/DECISION_MODEL_EXTERNAL_MATERIAL_ORDER_HANDOFF.md)。

## 决策模型只统计输入，不做价格计费

用户明确决策模型以后可用本地模型，因此原生决策调用已停止调用通用 USD 价格估算和 owner/run 成本累计；普通生成模型的原成本路径不变。`test_decision_model_call.py` 的成功调用断言决策价格指标和成本账均无写入，原请求终态与实际输入 token 仍在同一 ModelCallLedger；加原 HTTP 服务、设置探测和用量展示四文件联合 **63 passed**。Ruff、doc sync 与 `git diff --check` 通过。

## 决策模型 P5-B 正式记忆关系首片（本地）

新 `curator_relation` 默认关闭、限 owner 后台，和已有 Curator 标签共用一次阶段及原 lease 期限。完整消息与带真实版本的短 long-term 条目可得到仅供原提取参考的关系提示；截断、缺版本、正式条目变化、关闭和故障保留原批次。`test_decision_curator_relation.py`、`test_decision_curator.py`、`test_decision_settings_scope.py` 联合 105 项，原 Curator/Candidate/Promotion/设置/TUI 六文件 188 项，合计 293 项通过；随后隔离真实Jev的12对短样本符合预设，后续提取仍是本地替身，未证明广泛关系质量或全库覆盖。文件与命令见 [P5-B 交接](docs/tasks/DECISION_MODEL_P5B_HANDOFF.md)。

## 决策模型 TODO12 Jev容量门与实际接口（进行中）

`test_decision_protocol.py`、`test_typesafe_decision.py` 曾联合69项通过；后续全仓回归发现 UTF-8 字节直接比较 token 上限误拦 78,419 字节批量请求，使六项 localhost HTTP 用例无法发网。改为复用原 token 估算并留一成余量后，`test_typesafe_decision.py` 与 `test_decision_capability_http.py` 联合 **24 passed**，覆盖明显超窗提前拒绝、正常批量发送、超时、在途设置更改及 401/500 回退。该估算不是精确供应商 tokenizer 或硬容量证明，原 JSON 字节资源帽不变。
容量核对使用序列化UTF-8字节上界，不把它当实际token计数；这组测试不覆盖完整子代理换模、Compact或真实缓存。
隔离 owner 下官方 Jev 已累计20次实际HTTP尝试：前13次包含协议、Gateway TUI 普通中文回复及三子代理保留原模型派工；P5-B 新增一次成功和一次50ms期限取消；P5-C 新增五次，含一次完整页序自动追加与一次短期限取消。官方 M3 与 OpenCode DeepSeek 的短连接探针均成功，但 Jev 改选后的真实执行仍待验；完整记录见[真实验收](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。
子代理候选配置新增原 profile ID 列表，经同一设置服务和 TUI 字段校验；空数组沿授权目录，非空数组只缩小候选，变更使在途建议失效。设置复核时的用户取消须原样传出。`test_decision_subagent.py`、`test_decision_settings_scope.py`、`test_tui_decision_menu.py` 联合62项通过，不能替代三模型真实切换。
创建前容量旧粗估已撤销；子代理准备/逐候选最终配置和原输出 cap 复用同一生产入口，完整首请求尚缺激活后状态、child 历史和工具证明，因此当前不自动改选。28 个相关文件 427 项定向测试通过；离线抓到三候选实际适配器输出 cap，但这不证明完整 wire/schema 或真实换模，详见 [容量审计](docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。
另修 exact child thread 的旧快照写回竞态：`test_conversation_store.py` 含读后模型/Compact 状态更新的定向回归，52 项通过；现有记录复读原 thread 文件最新值并只补缺失身份，不覆盖用户选择。

## 决策模型 TODO10 能力推荐与上下文减量（本地）

8文件父侧联合127项通过：`test_decision_capability_consumer.py`、`test_decision_capability_http.py`、
`test_decision_skill_projection.py`、`test_tool_presentation_projection.py`、`test_decision_skill_tool_settings.py`、
`test_decision_settings_notifications.py`、`test_tui_decision_menu.py`、`test_decision_settings_scope.py`。
原运行准备入口→seed→params→真实PromptBuilder/native schema验证实际输入减量；重复渲染不重新调用决策。
本地HTTP验证成功、300ms超时、在途关闭/改策略、401/500不重试及迟到终态；原搜索可找回schema，插件撤销仍阻止旧绑定执行。
关闭/观察/不确定保持原输入；真实接口反馈后改为独立候选题，96题完整协议通过，原总字节/节点上限继续生效，不截断尾部。
另与`test_memory_runtime_compact_auto_continuation.py`、`test_compact_semantic_summary.py`联合回归通过。
真实Jev质量、输入token净收益、跨模型完整窗口和provider缓存仍待12/13，不能拿夹具字节量当收益。
命令、责任边界见 [TODO10交接](docs/tasks/DECISION_MODEL_CAPABILITY_HANDOFF.md)。

真实API首批7次调用发现并修复跨题选择槽与概率舍入两个问题；保留1次真实2秒超时，不计作判断成功。
修复后4秒配置下同需求0.728秒返回，4个相关能力include、4个无关能力not_needed；是合成材料的真实接口证据，不是完整TUI验收。
协议、能力消费者、本地HTTP及适配器联合92项通过，脱敏原始响应replay保留0.99概率总和，不归一化。
明细和缺口见[真实验收记录](docs/tasks/DECISION_MODEL_REAL_VALIDATION.md)。

## 决策模型 TODO11 设置与显式探测（本地）

最终14文件联合243项通过，包含原ToolExecutor→user_config→原生HTTP→原会话用量结算及失败真实工具状态。

原设置菜单/模型操作/薄客户端到 Gateway 的真实本机 HTTP，再到原生决策 HTTP 与账本已组合验证。
六文件菜单/运输/用量组合81项、七文件配置/范围/服务组合117项通过；后端构造共用与准确冷却恢复后，
`test_decision_model_operations.py`、`test_decision_gateway_transport.py`、`test_decision_service.py` 54项通过。
受控认证容器验证运输和owner隔离，不代替真实认证部署；真实按键表单不代替安装版TUI或收费模型质量。
文件、原接口、失败对照与剩余边界见 [TODO11交接](docs/tasks/DECISION_MODEL_SETTINGS_HANDOFF.md)。

## 决策模型 07/09 业务入口（本地）

子代理片联合 10 个 focused 文件 209 项通过；父侧追加同修复身份/不同幂等身份的名字边界，
与召回片联合执行 `test_decision_subagent.py`、`test_decision_recall.py`、`test_memory_recall_v2.py`、
`test_memory_first_loop.py`，83 项通过。实际 ToolExecutor + RunParams + 工具开启可持久采用建议模型；
召回覆盖原授权/预算后排序、来源撤销、完整准备入口与本轮上下文复用。
这里使用协议替身及原本地存储/worker，不代表真实 Jev 质量、完整窗口缓存或安装版 TUI 验收。

## 决策模型 Curator 与用户后台 scope（本地）

`test_decision_curator.py` 与原 Curator、自适应超时/缩批测试共 **117 passed**。
父侧 `python3 -m pytest agent_py_agent/tests/test_decision_owner_scope.py agent_py_agent/tests/test_decision_curator.py agent_py_agent/tests/test_decision_service_http.py -o addopts='' -q --tb=short`：**62 passed**。
覆盖 owner 后台明确 run/空 thread、拒绝活动会话冒充后台、后台预算冻结、原设置隔离、真实 localhost HTTP，
以及实际公共服务→原 worker/账本→临时标注→原提取/验证/游标；缺数据等四类业务结果与运行失败分开。
原 lease 剩余头寸限制极大后台超时，关闭不编码材料；提示超预算、失效和错误沿原链。
原 Curator 缺独立持久模型用量结算，本片不伪造前台统计或新建账本；真实质量/部署未验。
文件、命令和限制见 [Curator 交接](docs/tasks/DECISION_MODEL_P2_CURATOR_HANDOFF.md)。
消费复核补充：service、owner_scope、curator、service_http 四文件联合 **95 passed**；覆盖响应返回后关闭/观察/
修改预算失效、实际调用 deadline 不延长和用户取消传播，Curator 在正式附注释前使用公共复核。

## 决策模型 P1 实际服务组合（本地）

17 个直接相关文件联合 **399 passed**，覆盖设置/迁移、协议、worker/准入、策略通知、原账本与显示。
随后新增会话写锁争用检查，`test_decision_service_http.py` **8 passed**：使用真实 localhost HTTP、原配置、
原账本和活动 TUI sink，验证正常/观察、关闭零请求、401/500、超时、在途关闭及用量刷新不等待会话写锁。
决策结束自动发原活动统计帧；`usage_only` 不持久写显示，下一原模型边界沿原路径保存，原 finalizer 仍负责用量结算。
`test_decision_service.py` 验证阶段共用时间、配置变化、锁忙、逐题错误、冷却隔离、旧阶段未退出不可叠加。
`test_decision_settings_notifications.py` 验证通知逆序及会话覆盖恢复继承，关闭不能被较旧通知复活。
详细命令、严格 gate 和限制见 [P1 服务交接](docs/tasks/DECISION_MODEL_P1FG_HANDOFF.md)。
真实 Jev、真实 MiniMax、当前安装版 TUI 和实际业务消费者尚未验收；本地 HTTP 样本不代表模型决策质量。

## 决策模型共用设置与原用量展示（本地）

`test_decision_settings.py` 与原 `test_user_config_capability.py` 验证同一服务/工具的读取、修改、恢复继承、
有限正秒数、owner/thread 双版本冲突、共享撤销、缺凭据仍可关闭和可信线程身份。
迁移与配置相关 12 个文件联合 251 项通过；原模型目录当前 v4，线程当前 v10，不改原连接、历史和模型选择。

`test_model_call_ledger_partitions.py` 验证终态单调、裁剪后用途累计、逐字段来源、准确保留句柄及迟到 HTTP。
`test_decision_usage_metrics.py` 与原 conversation_store/tui_model_metrics 验证实际存储增量、快照重放、
部分输入缺报、真实零值、原一行展示以及后台追加后基数刷新；不以模型正文判断用量。
设置、账本、存储和显示 7 个文件联合 198 项通过；该组合未调用真实 Jev 或 MiniMax。
`usage_only` 接原活动 request/run 的显示，用途统计不增加普通生成轮数或覆盖最近生成指标。
跨独立活动范围的即时遥测与实际业务消费者仍随 TODO 08/12 验收，不能以静态渲染代替真实 TUI。

本片 focused 命令：
`python3 -m pytest agent_py_agent/tests/test_decision_settings.py agent_py_agent/tests/test_user_config_capability.py agent_py_agent/tests/test_model_call_ledger.py agent_py_agent/tests/test_model_call_ledger_partitions.py agent_py_agent/tests/test_conversation_store.py agent_py_agent/tests/test_tui_model_metrics.py agent_py_agent/tests/test_decision_usage_metrics.py -o addopts='' -q --tb=short`。

## 决策模型 P1-C/D 有界调用与原准入组件（本地）

18 个直接相关文件 **315 passed**，覆盖精确取消、启动前取消、迟到结果、慢清理与非协作调用的资源保留，
以及原 HTTP、Curator、准入/预算和模块边界。原 Curator 线程/队列实现已迁入唯一 `bounded_call.py`，没有第二份执行器。
父侧组合测试验证 caller 超时后 worker 仍持模型名额，同资源和额外可选调用被拒绝，普通 LLM 可取得保留名额；
worker 真正退出后才允许同资源新请求。只证明组件组合，没有声称实际决策服务或真实模型已接通。
Curator 超时不再额外 join 0.5 秒：返回时仍存活就记 still-running，退出前不缩批重试；原游标/缩批规则未改。
修复并覆盖清旗后的 late hook、线程构造/Context复制/启动失败、大期限等待溢出和 caller BaseException 收尾。
复核只读；没有实际模型、日常配置写入、部署或 Gateway 重启。完整命令及剩余边界见 [组件交接](docs/tasks/DECISION_MODEL_P1CD_HANDOFF.md)。

## 决策模型 P1-B 原生协议与传输组合（本地）

`test_decision_protocol.py` 与 `test_typesafe_decision.py` 合计 59 项通过，验证冻结输入、版本与来源绑定、
候选校验、逐题缺失/错误、原生三类问题、缺失 usage 不补零、迟到网络/解析结果拒绝、取消不重试。
本地 HTTP 服务器收到了真实 `/v1/systemone` 中文 state/questions 请求，并返回逐题结果与用量；没有调用真实 Jev。
这不能代替可选调用的资源准入、有界 worker、阶段预算、账本或用户实际 TUI 验收，这些仍未接线。
联合 7 个文件 **216 项通过**（零失败/错误/跳过）：上述两个文件，加 `test_gateway_strict_request.py`、
`test_gateway_helpers.py`、`test_provider_request_scope.py`、`test_compact_request_budget.py`、`test_runtime_module_boundaries.py`。
严格传输覆盖 46 项：真实慢 HTTP、302、零重试不读错误正文、解析前后同一期限、关闭竞态及 1 MiB 超深 JSON。
显式严格 JSON 在原解析器前做 64 层资源预检，普通请求仍用原解析/重试与 SSE 合同。
协议新增编码器调用哨兵：超量整数/字符串在序列化前拒绝；合法 JSON 内坏题不损坏好题。
首轮深 JSON 测试假设 1500 层必然触发 RecursionError，与仓库递归上限不符，已改为真正资源界限测试；
结构守卫发现的深嵌套已按校验职责拆分，未修改尺寸基线。命令详见 [P1-B 交接](docs/tasks/DECISION_MODEL_P1B_HANDOFF.md)。

## 决策模型 P1-A 本地配置合同验收

10 个相关文件 213 项通过：决策用途配置 17 项，以及原模型、服务商、共享、线程选择、Gateway、OAuth、
OAuth 传输、采样和模型菜单回归。覆盖保存无网络、公开字段无密钥、只读旧版本迁移、生成误选拒绝、同名用途隔离、
共享与 owner 权限/撤销；没有调用真实 Jev 或 MiniMax，没有修改日常配置、启动 Gateway 或部署。
首轮既有迁移测试仍预期 v2，已更新为当前 v3 后通过；新测试导入排序由 Ruff 修正。
独立复核发现旧表单会丢 decision 用途、旧 v1 迁移会丢自定义头，已修复并通过原表单与保存服务组合回归。
本片没有时间/请求/设置双入口实现，其验收不能算通过；完整剩余项见 [执行 Goal](docs/tasks/DECISION_MODEL_GOAL.md)。

命令：对 `test_decision_model_profiles.py`、`test_model_profiles.py`、`test_model_provider_management.py`、
`test_shared_model_catalog.py`、`test_thread_model_selection.py`、`test_gateway_model_profiles.py` 运行 focused pytest，
再覆盖 `test_model_oauth.py`、`test_model_oauth_transport.py`、`test_tui_model_menu.py`、`test_provider_sampling.py`；
最终联合命令使用 `python3 -m pytest <以上十个文件> -o addopts='' -q --tb=short`，213 passed in 4.29s。

## 第 9 步插件面板真实 TUI（本机，fae9d5855 / wheel 86339f65，模型 MiniMax-M2.7）

- 通过：TUI 内安装、启用、`/plugins@activity-line show` 打开；两个中文任务期间面板显示"工作中 · 正在使用 run_command"，结束回到空闲，两份产物数字经独立脚本核对正确；面板打开时停用 → 面板立即消失、插件进程回收；再启用后可再打开；面板打开时卸载 → 面板与进程都消失，列表只剩其他插件；面板打开时 Esc 中断 → TUI 显示已中断，面板同时回到空闲。
- 发现并修复：① `resume` 后未经补全直接输入面板命令，本地目录为空而落到宿主被拒；改为先显式读一次目录（`test_tui_plugin_panels.py` 新增回归）。② 只有展示动作的插件使用卡给出"请用插件打开面板"的中文示例，实测模型只能回答没有该能力；改为不给落空示例（`test_plugin_commands.py` 新增回归）。
- 已知缺口：纯模型思考阶段面板显示空闲，见 [插件展示](docs/design/PLUGIN_DISPLAY.md#已知缺口)。
- 断连重连：本轮样本中模型改用后台终端并误用参数，回合在断连前已按"结果无法确认"结束，未构成有效断连样本；断连/重连/停止仍以 TUI232 为准。

## 第 10 步视觉、界面型插件与组合验收（本机 1a8169887→a2e26178b，2026-09-24）

- M3 视觉：在 TUI `/model` 的官方 MiniMax 服务商（api.minimaxi.com，复用原密钥引用）下新增模型 MiniMax-M3，本会话切到 M3；`/attach` 附加 420×204 测试图后要求不调用工具描述图片，回答"3 行：HELLO FROM / IMAGE TEXT / PLUGIN READ，黑字白底"，全对且当轮工具 0。请求记录 input_media 的 sha256（68bc9caf…）与原图一致，模型用量记录 models=['MiniMax-M3']、backend anthropic_compatible。
- harness-console（宿主只读 API 的首个消费者）：中文请求后以 Chrome `--app` 独立窗口打开工作台；私有链接文件 0600；页面数据来自真实宿主（20 个线程、11 个插件启用状态逐项一致、Gateway 进程号与 /status 一致），截图见本机私有证据；停用后服务端口与窗口进程一起回收；再启用可用默认浏览器重新打开；卸载后端口关闭、无残留进程。
- 组合验收（单 Gateway，三路 TUI）：
  - A（插件长任务，03:33:59—03:43:26）：派 3 个子代理分段统计 12 个月 4,800 条订单，汇总与独立答案逐区域一致（全年完成 9,179,658 元 / 3,602 单）。本版（1a8169887，决策筛选逐工具判断）把 genui/design/browser 插件工具折叠，模型未用 tool_search，改用内置写文件并如实报告"插件工具不在快照"；savepoint-lite 当时本机未安装，如实报告。
  - B（内置任务，同时运行）：5 个文件的总和与最大值全部正确；C（管理）：A 运行中停用 status-pet（A 的面板立即消失、任务不受影响）与 web-board，再启用 status-pet。
  - 切到 a2e26178b（决策线按插件分组出题）后同一插件链在新会话中依次调用 genui export、design create、savepoint save、browser open，产物逐项正确（条形图每柱数值、卡片副标题、快照在插件数据目录、页面标题核对）。
  - 逐个卸载全部 11 个插件：浏览器进程与插件环境目录清空、列表为空；新会话内置工具任务正确（orders-01.csv 400 行、金额 1,015,997）；历史会话中的插件调用记录保留。
  - 未满足：组合长任务实际约 9.5 分钟，未达 15—30 分钟；组合中未包含 M3 图像理解（单独验证）。

## 第 10 步第二、三批真实 TUI（本机 6767bb4ed→4b8cdebbd，测试机同版；模型 MiniMax-M2.7）

- worktable-lite（本机）：面板列出最近会话，编号、顺序、相对时间和"当前"标记与会话记录逐项一致（246 个会话、1 个损坏记录被跳过）。
- status-pet（本机）：中文任务期间 空闲 → 正在工作（显示当前工具/思考中）→ 空闲；停用 → `/plugins configure --file`（style=whale、name=蓝蓝）→ 启用后面板变为鲸鱼"蓝蓝"。
- genui-lite（本机）：中文请求依次调用 table 与 export，生成的 sales.html 标题正确、无脚本、无外部资源、权限 644，表内 10 行与输入逐项一致、极值（10月最高、2月最低）正确。随包 Skill：启用时 `skill_search` 找到 `plugin:genui-lite:genui-table` 并读取；停用后只读探针与模型的 `skill_search` 都找不到（matches 为空）。
- design-lite（本机）：安装后中文请求调用 create 生成带 data-dl 标记与 #2f6feb 的海报；再次中文请求调用 edit，只改动两处标题行，其余字节不变。首轮失败原因为该包未安装成功（安装命令被模型选择弹窗吞掉），不是产品缺陷；补做的通用改进见下。
- image-text（本机）：显式命令 OCR 读出 "HELLO FROM / IMAGE TEXT / PLUGIN READ"，Gateway 环境找到 tesseract；（测试机）无 tesseract 时明确返回 OCR_UNAVAILABLE 并附图片元数据。
- browser-lite（本机）：显式命令逐条调用时第二步起 NO_PAGE——显式命令每次是一次性插件连接，命令结束浏览器随之回收，有状态插件不能跨显式命令串联（记录为显式命令语义边界）；改用一次中文请求由模型在同一连接内完成 open→fill→click→read，页面显示"已提交：李雷/B"。（测试机）无浏览器时审批后明确返回 BROWSER_UNAVAILABLE。
- web-board（本机）：中文请求开服务，无令牌 403、越界 403；发现宿主会从工具结果中脱敏 token 参数，模型只能给出"<令牌>"的链接，改为插件把完整链接写入 0600 私有文件并用默认浏览器打开（0.1.1）。
- desktop-lite：组件测试中真实 macOS 调用 osascript/pbcopy/open 退出码 0；（测试机）无桌面时审批后明确返回 DESKTOP_UNAVAILABLE。
- 通用改进（4b8cdebbd）：插件工具说明写成"插件 <ID>（<简介>）"、ID 进入检索关键词、`hints.provider_id=plugin:<ID>`；折叠提示按插件列出被折叠插件。

## 第 10 步第三批插件（本地，待发布）

- `test_plugin_host_api.py`：宿主 API 令牌只在 Gateway 服务时发放，换代/停用/Gateway 停止即失效且不复活；主题白名单外拒绝；线程只给公开字段；v4 描述往返与校验。
- `test_harness_console_package.py`（5 项）：从源码构建 harness-console 包，确认包描述 v4、`host_api == ["read"]`、三个工具均为 mutating；测试内假宿主 API（校验 `X-Plugin-Host-Token`，返回固定 threads/activity/plugins/gateway）经环境变量注入真实 MCP 进程：open（不开浏览器）→ 读 0600 的 last-link.txt → 无/错令牌 403 → 带令牌首页 200 并下发 HttpOnly+SameSite=Strict cookie、页面无外部资源、CSP 只放行 self 与内联 → `/api/state` 只认 cookie，返回白名单整理数据（默认选最近线程、切换线程、未声明字段不透传）且不含宿主令牌、1 秒缓存不重复打宿主 → POST/HEAD 405、再次 open 复用 → 假宿主改 403 后报"插件已停用或令牌失效" → stop 后端口连不上；缺宿主 API 环境变量时报"宿主 API 不可用"；desktop 用记录 argv 的假浏览器断言 `--app=<带令牌链接>` 与数据目录下独立 `app-profile`、再次 desktop 复用窗口、stop 与插件进程退出后假窗口进程都被结束；坏设置启动失败。

- `test_plugin_skills.py::test_plugin_tools_carry_plugin_identity_and_hidden_plugins_are_named`：插件工具说明含插件 ID 与简介、关键词含 ID 拆分词；折叠提示在短名单为空时仍按插件列出被折叠插件，非插件工具不列。

- `test_desktop_lite_package.py`（7 项）：从源码构建 desktop-lite 包并起真实 MCP 进程，系统程序全部用记录 argv/stdin 的假脚本经设置注入（不真弹通知、不开程序）；覆盖三个工具均为 mutating 且缺上下文拒绝、通知文本含引号/反斜杠/`& do shell script` 原样作为 argv 且 AppleScript 固定走 stdin、标题/内容超长与 NUL 拒绝、open 拒绝符号链接/目录/缺失/上溯/越界/.command/.APP/可执行位并把绝对路径传给打开程序、剪贴板文本经 stdin、程序缺失返回“不可用”、非零退出码、超时杀进程、坏设置启动失败。
- `test_image_text_package.py`（10 项）：从源码构建 image-text 包并起真实 MCP 进程；本机有 tesseract 时用测试内极简 PNG 编码器画的点阵英文图实识别（无 tesseract 时 skip），另覆盖 tesseract 缺失返回 OCR_UNAVAILABLE 且仍带元数据（PNG/伪装扩展名/GIF/JPEG/WebP 宽高）、非图片与损坏头部、超上限、链接/上溯/越界、非法语言名、语言包缺失列出已装语言、假 tesseract 超时被杀且临时文件删除、坏设置启动失败。
- `test_design_lite_package.py`（15 项）：从源码构建 design-lite 包，确认包描述 v3、`skills == ["design-card"]`、wheel 内 SKILL.md 可按 frontmatter 解析；起真实 MCP 进程：三种模板 create（无外部资源、无 script、标题副标题转义、权限 0644）、默认色与非法颜色/未知模板/非 .html 拒绝、已存在拒绝与 `--overwrite`、edit 只改目标字段（其余字节不变、保持原权限位、返回新旧值）、非本插件/非 UTF-8/标记被改的文件拒绝、字段缺失与未给字段、越界/上溯/链接/写入范围拒绝、缺写入上下文失败。
- `test_browser_lite_package.py`（20 项）：WebSocket 帧纯单测（客户端掩码、7/16/64 位长度分支、截断前缀视为数据不足、服务端掩码/保留位/未知操作码/控制帧分片与超长/超限拒绝、分片重组与非法序列、socketpair 上跨读拼帧、自动 pong 与 close）；从源码构建 browser-lite 包并起真实 MCP 进程 + 本机 Chrome（无浏览器时 skip）：工作区测试表单 open → read → fill（输入框、按文字选下拉）→ click → read 出现"已提交：张三/B"，同进程复用同一浏览器，close 后 pid 消失、profile 清空；选择器 0 个/多个/语法错、不可填元素、无此选项、未开页面、缺上下文；https://example.com、工作区外 file://、`../` 上溯、ftp/javascript 被拒，本机 302 到外部与点击外链被拦截并停到空白页，页面内外部 fetch 被拦、允许主机放行；allowed_hosts 设置生效；chrome_path 不存在时五个工具都报"浏览器不可用"；宿主 stop、stdin EOF、仅对插件 pid 发 SIGTERM 与空闲超时后浏览器 pid 均不存在（`os.kill(pid, 0)` 失败）；缺数据目录与坏设置。
- `test_web_board_package.py`（6 项）：从源码构建 web-board 包并起真实 MCP 进程，用 urllib 真实访问插件网页：只绑 127.0.0.1 随机端口；无/错令牌 403、查询令牌 200 并下发 HttpOnly cookie、cookie 单独可访问；目录列表与子目录；含 `<script>` 文本转义、Markdown `<pre>`、HTML 走 sandbox iframe、图片 `<img>`+`/raw`；`..`/绝对路径 400，指向外部或内部的符号链接 403，缺失 404；POST/PUT 405、HEAD 无正文；status 字段与请求计数；再次 serve 停旧端口，坏根目录不影响旧服务；stop 与空闲超时后端口连不上；MCP 客户端回收与 stdin EOF 退出后端口连不上；坏设置启动失败。

## 第 10 步第二批宿主补充（本地，待发布）

- `test_plugin_skills.py`：包描述 v3 往返与名单校验（空、非法名、重复均拒绝，v1 不带 skills）；只取已启用且声明 Skill 的插件目录；插件 Skill 来源为 `plugin:<ID>`、不覆盖同名用户 Skill；提供方不再返回后下一次快照即消失；总闸关闭时不出现。
- `test_genui_lite_package.py`（11 项）：从源码构建 genui-lite 包，确认包描述 v3、`skills == ["genui-table"]` 且 wheel 内有 SKILL.md；起真实 MCP 进程覆盖两种数据格式的 table、`--chart` 条形图等比长度、非数字/缺失列与坏 JSON/格式/列不一致报错、行数截断与大小上限；export 写出独立 HTML（无脚本、无外部资源引用、特殊字符转义、权限 0644）、输出已存在拒绝与 `--overwrite`、非 .html/上溯/越界/父目录链接拒绝、读写范围裁决、缺写入上下文失败。
- `test_plugin_display_service.py::test_sessions_topic_is_lazy_and_whitelisted`：会话列表提供方只在订阅时调用，坏行丢弃、metadata 不转发、读取失败按空列表。
- `test_worktable_lite_package.py`：从源码构建 worktable-lite 并在独立解释器真实进程渲染 sessions 面板：空列表、多条含当前会话与相对时间、`max_rows` 截断（20 条时让出一行给提示）、`hide_current`、时间缺失；设置经 `MY_AGENT_PLUGIN_SETTINGS` 注入，坏设置退出码 2 且不回显值；输出过 `normalize_display` 且不截断。
- 真实插件组件：`test_status_pet_package.py`（11 项）从源码构建 v2 纯展示包 status-pet，由真实 MCP 进程经展示服务渲染 cat/whale/robot × 工作/等待审批/空闲：三种状态图各不相同、等待审批以【等待审批】开头，`name` 设置生效，输出经核心校验不截断、不超限、无控制字符；未知外观、空名或超 12 字符、未声明字段被宿主 schema 与 `/plugins configure` 拒绝且原设置不变，进程收到坏设置以退出码 2 失败且不回显设置值。

## 第 10 步第一批真实 TUI（1be5753ff / wheel 2d049be8，模型 MiniMax-M2.7）

- 本机：新开 TUI 未经补全直接 `/plugins@activity-line show` 可打开面板（resume/新开修复生效）；纯展示插件使用卡改为"面板只能用命令打开"。
- context-inspector（本机）：发消息前面板显示"还没有快照"；一次中文请求后显示 23,981 / 200,000（12.0%）、触发线 180,000、消息 2,779、运行引导 0、工具目录 15,316、压缩 0，与 TUI 状态行一致；面板打开时停用 → 面板与进程消失；再启用重开有数字；卸载后面板与进程消失，数据目录保留。
- workspace-peek 0.1.1（本机，SDK 0.2.0）：卸载 0.1.0 后安装新包并启用；中文"请用 workspace-peek 插件预览 peek-note.txt，第二行的数字"经插件工具读出 4217，正确。`/plugins update` 尚未实现，升级按卸载后重装。
- savepoint-lite（测试机，默认确认权限）：包在 owner 家目录外时安装被读取授权拒绝（按设计），放入家目录后安装启用；中文请求保存快照 → 审批"允许一次" → 快照只写插件数据目录，内容与原文件逐字节一致；改坏文件后中文请求恢复 → 首次恢复 `--expect` 不符被拒（未写）→ 模型用返回的当前哈希重试 → 审批 → 恢复后 sha256 与快照一致、权限位 644 保持。另一个 TUI 在调用等审批时停用插件 → 批准后该调用记 TOOL_EXECUTION_FAILED，无新快照、无残留进程；再启用后显式 `list` 显示原快照仍在；卸载后进程消失、快照数据保留、用户文件不变。
- 发现：① 同回合内已停用插件的工具仍先弹审批（冻结目录 + 审批先于执行可用性检查），批准后才失败，零副作用但体验差，记入台账待设计；② 显式插件命令回执直接显示双重转义 JSON，已改为展示 content 文本并缩进 JSON（`test_plugin_management.py` 新增回归）；③ 首次渲染偶发"展示内容无效"一次后自愈、未能复现，已加失败类型日志。

## 第 10 步第一批宿主补充（本地，待发布）

- `test_plugin_display_service.py::test_context_topic_forwards_only_public_numbers`：`context` 主题只转发上下文公开数字白名单和压缩次数，缺快照标记未知。
- `test_plugin_enable.py`：真实启用的插件进程环境带 `MY_AGENT_PLUGIN_DATA_DIR`，目录已按 owner + 插件 ID 创建。
- 真实插件组件：`test_context_inspector_package.py` 从源码构建 v2 纯展示包 context-inspector，由真实 MCP 进程经展示服务渲染 `context` 主题：无快照只显示提示不编数字，有快照显示千分位用量 / 窗口百分比、触发线、三部分组成、压缩次数与"估算"标注，输出经核心校验不截断。
- `test_savepoint_lite_package.py`（13 项）：从源码构建 savepoint-lite 包并起真实 MCP 进程；覆盖 save→list→restore 往返（保持权限位、工作区无新文件、快照只在数据目录）、`--expect` 不符拒绝并返回当前 sha256、写入上下文 check 拒绝、符号链接/父目录链接/上溯/越界拒绝、保存后换成链接不被跟随、缺写入上下文时 restore 失败、大小与数量上限设置生效、损坏快照不恢复、缺数据目录与坏设置。

## 第 10 步插件写入上下文（本地，待发布）

- `test_workspace_write_context.py`（10 项）：2000 组随机写入边界 × 4 个目标路径，逐项断言「插件允许 ⇔ 宿主 `validate_write_boundary` 允许且目标在写入根之内」，并验证序列化往返后裁决不变；另覆盖无范围时只允许 cwd、owner 墙、畸形载荷拒绝、锚点取最具体根，以及只对协商且声明写效果的工具下发、缺上下文时发送前 `not_started`。

## 第 9 步 TUI 拆分首片：按键动作外移（本地，待发布）

- 纯搬移，行为不变：`tui_keybindings.py`（2656→1744 行）中的副作用动作移到新模块 `tui_actions.py`：记忆命令后台执行、本地/Gateway 排队、执行选项快照、控制命令提交与对账器、补充消息提交与对账器、子代理插话/停止、Esc 中断，以及只被它们使用或需被双方共享的 `_handle_command_params`、`_required_tui_runtime`、`_active_tui_runtime`、`_agent_guidance_sent_notice`、`_restore_failed_agent_input`、`_safe_http_status` 和两个重试间隔常量；`_run_clipboard_tool`、`_load_tmux_clipboard_buffer` 移到 `tui_clipboard.py`。
- `tui_actions.py` 运行时不导入 `tui_keybindings`；唯一延迟导入是 `_restore_failed_agent_input` 回调内的 `_set_input_draft`。`tui_plugin_commands.py` 的延迟导入改指 `tui_actions`。
- 测试只改 monkeypatch/调用路径到实际解析位置（`test_tui_input.py`、`test_tui_plugin_panels.py`、`test_host_command_stream.py`），断言不变；`CODE_SIZE_BASELINE.json` 两条既有函数豁免的路径随函数改到 `tui_actions.py`，数值不变。
- 验证：test_tui_*、test_chat_parts、test_cli_chat、test_chat_prompt_queue、test_chat_control_runtime、test_plugin_command*、test_architecture_guardrails、test_host_command_stream 共 42 个文件 862 passed；ruff、strict code-size、doc sync、diff check 通过。

## 第 9 步插件面板（本地，待发布）

- 协议与服务：`test_plugin_display_service.py`（19 项），覆盖声明校验、展示描述截断与控制字符过滤、包描述 v1 字节不变与 v2 往返、首次加载、同输入不重复渲染、最新输入合并、撤销与停用回收、无展示能力、失败退避、空闲关闭和请求数上限；测试中发现并修复"错误结果被当成最新、退避后永不重试"。
- Gateway 入口：`test_gateway_plugin_panels.py`（9 项），覆盖来源鉴权先于读正文、坏请求、总开关关闭、冷 owner 不加载实例以及伪造身份字段无效。
- 真实插件组件：`test_activity_line_package.py` 从源码构建 v2 纯展示包，由真实 MCP 进程经展示服务渲染，关闭服务时进程被回收。
- TUI：`test_tui_plugin_panels.py`（11 项），覆盖打开/关闭/上限、三种排版、宿主不可用即移除、传输失败退避、错误保留正文、正文行数上限、退出事件结束线程，以及展示动作本地拦截不经宿主；另有 675 项现有 TUI 相关测试通过。
- 真实 TUI 验收待新包部署后进行。

## TUI 可扩展性集成分支修复（claude/integrate-tui-scalability，本地）

- 背景：origin/main（含第 7/8 步）叠加 codex/tui-scalability 的 6 个提交后，出现 9 个稳定失败；另有 SSE 超时与 TUI fixture server 共 8 个用例在此前报告中失败，但在本分支原始 HEAD 与修复后均无法复现（单独、分组、4 路并发各跑通过），判为负载相关抖动，未改代码。
- 真实回归（改产品代码）：`poll_gateway_chunks` 新增退避后把睡眠贴合到 deadline，最小采样窗被缩短，租约心跳尚未落盘就判请求死亡；恢复 0.1s 最小采样间隔，退避只放大空闲间隔。`SafeFormattedLines.join` 改为显式序列参数，满足无 `*args` 服务接口守卫。`INPUT_MEDIA_INVALID` 按决策线同文登记为不可重试的用户输入校验错误。
- 替身过时（只改测试）：后台 supervisor 替身补 `_owner_pool/_next_owner_retire_at`；遗留巡检替身池接受 `touch` 并提供 `pin`；原生 IR 参数替身补 `task_attributes`；chat client 断言补 `input_media: []`（与决策线同文）。
- 验证：tui_*/gateway_*/timeout*/stream*/slow_model*/background_main*/native_tool*/input_media*/chat_client* 共 91 个文件加架构守卫与错误码策略：2,059 passed、2 skipped、1 xpassed；ruff、strict code-size、diff check 通过。

## 委派与交付核对软引导（本地，待发布）

- 改动：`coordinator_tool_boundary_text` 增加三条引导：没有要求委派时优先自己做；派工时写清要交回的产出和核对方式；收到结果后先用工具抽查，不直接转述"通过"。子代理 runner 的 Required Output 要求数字和核对结论必须来自本轮工具输出，未核对的如实标出。只改文字，能力、权限和完成判定不变；设计记录见 DESIGN_LEDGER。
- 复测（7c467a4d3，本机）：TUI238 与 233 同题，24 个孩子都执行了计算命令，核对文件、totals.csv 和 summary 全部正确（上一轮 10/24 出错）；TUI239 与 229 同题，孩子数字和父级回核都正确，但父级自己新算的状态小计仍靠心算出错，并编造理由解释矛盾。因此在共用的 prompts/default.md 证据段增加数字来源和矛盾重算规则（+2 条断言，相关 15 个文件 515 passed、3 项既有 xfail）。自发委派没有被折中引导阻止，只记录为观察。
- 再测（3b72c4d7b，TUI240 本机 / 241 测试机，与 229 同题）：240 所有数字正确（621 行明细），但缺少合计行；241 父级用工具回核，发现并如实披露了一组孩子的错误数字，combined.csv 已改正；它自己写的按状态分项仍有 3 个金额错误（和等于正确总额），但没有编造解释。结论：孩子错数未被发现、父级编造理由这两类问题已不再出现；父级在单张汇总表里偶发心算，接近模型能力边界，不再叠加提示词。
- 验证：新增 5 条断言。所有引用协调策略、runner 提示、create_subagents、稳定前缀或系统提示的 79 个测试文件：1,941 passed、28 项既有 xfail。真实效果需在新包上用 TUI229/233 同题复测。

## 第8步新版原生 TUI 矩阵（TUI228—237，已部署版本 66a598cf3）

- 环境：本机与测试机各一个 Gateway，默认入口同版；每个 TUI 都经 `/model` 选择官方 MiniMax-M2.7，并核对实际端点；每个任务只提交一次中文需求，测试者只操作 Esc、`/stop`、审批和客户端断连这类明确的控制。
- 长任务（本机 233）：连续约 27 分钟的实际工作；模型自行派出 24 个孩子，全部结清；主线程提交 2 次会话 Compact（约 19.4 万和 19.9 万 tokens 时触发，窗口 20 万），两份摘要都逐字保留了原需求、规则和目录；工具失败 3 次后模型自行换路；13 个工作片均为 done，无残留锁和进程。
- 多孩子（本机 229）：三个孩子并行、时间重叠，完成通知已消费；combined.csv 只有一行表头，TUI58 的重复表头问题未复现。
- 普通对照（测试机 228、230）：run、attempt、task_run 均结清，工具操作全部成功。
- Esc 中断（测试机 231）：前台命令执行中按 Esc，run 与 attempt 变为 cancelled，命令如实记为 UNKNOWN（`effect_outcome_unknown:CANCELLED`），无锁。
- 断连与停止（测试机 232）：只杀 TUI 客户端进程后，Gateway → bwrap → bash → python 这棵进程树继续运行；`resume` 后恢复实时状态；从重连后的客户端发 `/stop`，15 秒内整棵进程树被回收；runtime_reason 为 `conversation_user_stop`，来源是结构化控制。
- 审批（测试机 236/237）：审批框显示精确的命令。选拒绝时没有产生任何工具操作，模型也没有绕路；选允许一次时命令恰好执行一次，执行时刻就是批准时刻。删除请求（234/235）：Shell 删除按设计被硬门拦截，补丁删除工作区文件属于"修改"类，不弹审批。
- 未覆盖：单轮内实时工具压缩时的逐调用精确来源路径（本批任务均未触发）；被中断的前台调用没有持久化的进程清理事实，只有实时进程快照可作证据。
- 业务交付（与框架结论分开）：228 完全正确。230 的 totals.csv 缺日期维度。229、233 的部分孩子没有用工具计算，而是心算合计，写出了错误数字（233 中 24 个核对文件有 10 个出错，每个都写着"通过"），父级也没有用原始数据回核，最终报告失实。这与 TUI58、225—227 属于同一类通用交付核验缺口，不按 CSV 或提示词加专项分支。

## 第8步发布后全仓回归修复（本地，未发布）

- 起因：66a598cf3 发布前只跑了相关文件，没有跑全仓。之后全仓复验共 19,034 项，其中 15 项是真实失败（另有 1 项因复验用的快照不是 git 仓库而失败，属于环境原因，在真实 checkout 中通过）。
- 11 项 `test_manager_runner_capability_requests`：第 7 步把准入和收口依赖收窄为 `manager.runtime_db` 和 `manager.conversation_store` 以后，轻量 manager 替身没有跟着补上这两个属性。现在按生产 `_attach_runtime_db` 和构造默认值显式设为 None，走原来的非托管路径。
- 1 项 `test_silent_swallow_stage2`：911a53245 以后，发布账本改为经锁内 `mutate` 写入，但替身仍然让 `save` 失败，因此断言的错误日志从未触发。改为让 `mutate` 失败，断言意图不变。
- 1 项 `test_observation_route`：7.10 在执行前按 id 重读待处理信封，2707fbbe3 当时漏补了这一个替身。改为使用真实 `WakeSignal`，并由 `pending_one` 返回仍处于 pending 的原信封。
- 1 项架构守卫：`compact_text_source.py` 的 `__exit__` 改为显式三参数签名，行为不变（不吞异常）。
- 验证：上述文件、`test_cli_update` 以及所有引用 `compact_text_source` 的测试，共 68 项通过；另外 Ruff、doc sync、diff 检查通过。除一处签名外，生产行为没有改动。教训：远端发布前至少要把直接受影响模块的全部测试文件纳入，只跑定向文件会漏掉夹具。


## 第8步精确来源与耐久清理事实组合（本地，未发布）

- 合入逐调用来源：来源为 canonical run/attempt/call，旧无来源记录保留 uncertain；来源/尾部和同号跨请求不能混淆，旧无 refs 的编号算法不变。
- 耐久恢复缺口：原测试仅从内存 archive 恢复。新测试使用真实 executor → externalizer → 磁盘索引 → carried reader → 模型上下文；短／外置输出原有 2 项实际失败，修复后相关四文件106项通过。索引只保留共享有界 process，不含 PID；调用身份保持。
- 发布组合首轮：48个直接相关文件1212 passed、24项既有xfail（49.55秒）。不同轮次的数字不累计作验收总数。
- 独立末审发现新 ID 未包含未知尾部的逐位裸 ID，合法候选可碰撞；真实 Store 提交赢家后写入 CAS 败者，读取的尾部实际被改为另一值。新增用例红转绿，新 ID 纳入 retained IDs；旧 ID 不改。最终受影响四文件159 passed（5.24秒），覆盖来源、原生 Compact、Gateway Compact、清理事实恢复。
- 旧无身份大历史仍可能无法安全 Compact；不制造身份或误删，不声称 provider overflow 已兼容。旧 reader 不能直接读取新混源账本，回滚须保持新增数据与可读运行时。
- 本地严格 gate 已通过：全目录 Ruff、doc sync、strict code-size（hard=0、blocked=False，基线未变）、diff、clean-package。未运行真实模型或新版 TUI；本轮不是双机部署，线上 CI 未作为验收来源。

## 第8步并发段与收口组合

并发段片0c19b2e3c精选为8468998b6，两个直接测试文件65 passed，包含21项新增因果／交错用例；原取消、审批、线程执行和provider顺序记账未移动。当前组合另把no-action两项常量移到唯一执行模块，原数值和测试断言不变。

最终11文件组合 **283 passed／20项既有xfail（11.52秒）**：上一节收口九文件加tool_round_execution和tool_segment_planning。覆盖顺序屏障、冲突后继续串行且不丢结果、逐候选动态Compact查询、异常前零工具执行／零记账、配置按需读取和原批上限。原请求／响应、unknown与Goal收口合同同时验证。

本地Ruff、doc sync、strict-size hard=0、diff与clean-package全部通过，尺寸基线未改。无真实模型、TUI、Gateway操作，仍待Compact来源引用片后做发布及原生验收，线上CI不是本片证据。

## 第8步收口依赖

相同三文件基线81 passed／20既有xfail；新增窄收口及绑定用例后四文件95 passed／20既有xfail。最终九文件组合218 passed／20既有xfail（11.43秒）：tool_loop_closeout、cli_resume_contract、unknown_outcome_tool_halt、test_tools/test_tool_loop、no_action_gate_round、tool_call_guardrail_runtime、timeout_recovery_delivery、runtime_gate_ledger、conversation_goal_tools。

新增14项覆盖请求／响应与用量归属、build→generate→strip→原因读取顺序，四阶段普通异常与中断均不重试、不调用后续操作；四种宿主绑定保持同一Agent/params和轮号。unknown用例在strip时改变halt，证明读取发生在生成和剥离之后；合同读取失败不能产生可续跑结论。原taxonomy与should_continue_task断言仅迁调用入口，未放宽。

Ruff、doc sync、strict code-size hard=0通过，尺寸基线未改；完整发布验收仍待来源修复和并发段组合。本片未启动真实模型、TUI或Gateway，不代替第8步真实矩阵。

## 第8步工具事实与唯一循环入口组合

工具事实7482a40d6与去空转发c6f45425b合并后，16个相关文件运行退出码0，**349 passed、20项既有xfail**。仓库默认-q叠加命令-q不显示尾部汇总，此处按完整进度符号计数；没有重跑相同测试以补数字。

组合包含tool_context_reducer、mcp_registration、tool_output_externalizer、runtime_gate_ledger、memory_compact_runtime_handoff、subagent_runtime_compact、compact_semantic_summary、tool_call_guardrail_runtime，以及provider_timeout_acceptance／continuation／resume_narrowing／resume_probe、subagent_runtime_guards、thread_interrupt、timeout_recovery_delivery、test_tools/test_tool_loop。独立入口片另验runtime_guidance、native_tool_use_ir_messages_flow为104 passed／4既有xfail。各组不能累加为唯一覆盖数。

保持原执行／审批／中断顺序和所有既有断言，局部monkeypatch避免测试多次驱动串用旧闭包；唯一合并冲突仅是模块注释。全目录Ruff、doc sync、strict-size hard=0通过；完整发布与真实TUI仍待剩余第8步边界收口。

## 第8步工具执行事实与恢复投影

10项因果用例先红后绿：process清理事实在内联、指定live输出、外置摘要及最终脱敏中保持，命令非零／清理成功与清理未确认分开。只读审阅补出巨整数和非有限浮点导致异常／非标准JSON，三项红转绿；缺失、畸形类型不补成成功，PID／实例列表只投影数量。verification原块保持。
Fake handler经过产品executor、输出归档、循环记录和native适配，两种长度分支逐字比较同源投影；保留原status=failed与effect_outcome=unknown的独立合同。carried恢复再读同一有界process，数量不丢。MCP fake服务的structuredContent伪process只留外部正文，不被提升为宿主事实。测试准备曾遗漏MCP默认审批和误认unknown effect等于unknown status，按真实合同修正测试；没有改生产执行语义迁就断言。
最终8文件组合 **241 passed，8.45秒**：tool_context_reducer、mcp_registration、tool_output_externalizer、runtime_gate_ledger、memory_compact_runtime_handoff、subagent_runtime_compact、compact_semantic_summary、tool_call_guardrail_runtime。没有真实模型或TUI调用，没有启动／停止共享Gateway。
本地Ruff、doc sync、strict code-size hard=0已通过，尺寸基线未改；完整发布仍待与空转发清理组合，线上CI不作为本片验收来源。


## 第8步 Compact 顺序来源与提交边界组合

- 原生提交后投影抛 RuntimeError／InterruptedError 的两项因果用例先红后绿：thread generation 已为1时不恢复旧IR、不增加提交失败数。审阅补出无binding临时回合仍需回滚，新增两项先红后绿，未改变其原语义。
- C来源覆盖两遍hash、追加／截短／改写拒绝、Unicode切片、取消与迭代器关闭、重试预算及大历史峰值。最初组合63 passed／1 failed，峰值1,870,598字节高于原界；查明重复iterencode闭包循环积累，改为可证明有界的小结构直接编码，大来源仍流式。原断言保持，不调GC；等值原型5,005组数值／异常类型无差异。
- 两项非文本／未知媒体来源上层验证：摘要分段前拒绝，模型调用0，原消息、summary、generation、cursor及checkpoint保持；记录一次真实失败，不生成机械摘要冒充媒体覆盖。测试准备曾误用不存在的MessageStore.load及未指定原生model_surface，修正夹具后才进入目标路径，不记作产品红转绿。
- 最终14文件组合 **333 passed、20项既有xfail，20.14秒**：native_tool_ir_compact_and_orphan_sweep、gateway_conversation_compact、compact_text_source、compact_request_budget、archive_tokens、tool_loop_model_turn、tool_loop_recovery_scope、compact_circuit_breaker、compact_progress、compact_semantic_summary、memory_compact_context_bundle、subagent_runtime_compact、tools/test_tool_loop、r223_audit_regressions。
- 本地严格gate已通过：上述focused组合、全目录Ruff、doc sync、strict code-size hard=0、diff及clean-package；尺寸基线未改。Ruff中一次测试导入排序问题已修正，尺寸中间版本的嵌套红灯按职责拆helper后通过。线上CI未作为验收来源。
- 本片未调用真实模型、未启动TUI、未部署或修改共享Gateway。第8步真实矩阵仍待完整组合包，不用本片验证替代。


## 第8步请求周期与有界读取组合候选

模型周期新增五项顺序／失败用例，完整验证首次响应后读重试上限、provider超限先恢复再回收、回收失败不再请求、preflight不恢复、最后prompt与response配对、异常不消费临时工具。渐进工具两项测试迁到实际请求周期，不保留旧私有清理入口。五文件164 passed、4既有xfail。
A/B最小移植原71项及相邻112项通过；独立审阅后新增三个用例真实失败：Unicode空白行被拒及极大created_at错误分类。字节预算先调整为能容纳该行，确认失败发生在解码阶段后才修生产代码；七文件复验186 passed。没有吞错误或自动修复尾行，旧游标和原幂等锁保留。
最终18文件组合 **456 passed、24既有xfail，18.17秒**：前述模型采纳十文件，加native_tool_ir_compact_and_orphan_sweep、archive_tokens、conversation_message_scan、conversation_store、conversation_message_stream、conversation_history_paging、gateway_foreground_transcript、cli_run_conversation。分组结果有重叠；本次增删远低于全仓阈值，没有追加全仓pytest。
本候选Ruff、doc sync（补齐memory模块文档后）、strict code-size、diff和clean-package均通过；尺寸基线未改，生成报告保留仓库外。
候选ef355f822已集成本地主线57baa13cb，两者源码tree一致；集成没有改生产代码，不重复跑相同组合。
本轮没有新增真实模型验收；已部署版本仍为第7步包。完整Compact scope／摘要来源链未移植，不能以此声称第8步完成。

## 第8.2首片模型采纳：本地合同验证

新增 `test_tool_loop_model_turn.py` 十项：成功／中断结构化返回先计量再确认；provider超限恢复原输入、preflight不消费；请求或计量失败不确认；瞬断中实时读取submission及待确认ID，拒绝重发已提交输入；安全重试只计量最终响应。
三个空响应旧xfail已迁为native假后端：补齐generate关键字和空text字段、用实际工作目录准备文件、从messages读取工具结果与恢复引导、为第二次工具使用独立call id。保留原3／3／5次调用、单次read／write、产物内容和操作核验断言；最初失败来自过期夹具，没有为通过而改变生产行为。
十文件组合：模型采纳、tool_loop、runtime_guidance、tool_context_ptl_retry、thread_interrupt、tool_model_generation、provider_timeout_acceptance、provider_transient_auto_resume、subagent_runtime_guards、tool_progressive_disclosure，结果 **212 passed、24既有xfail，14.47秒**。基线两项中断替身错误已显式修复，三个空响应xfail转为真实通过，其余既有xfail未改。
诊断准备失误单列：一次直接Python诊断漏用了pytest的HOME隔离，触及日常模型配置；已终止该准确诊断进程并改在独立临时HOME执行。该调用不是原生TUI、不计验收证据，共享Gateway未操作。后续本片测试均为隔离假后端；本片Ruff、doc sync、strict code-size、diff和登记后的clean-package均通过；第8步新版原生TUI仍待组合实现。

## 第8步基线发现的中断测试替身遗漏

在85050017d开始模型／工具循环拆分前，四文件基线为112 passed、27既有xfail、2 failed。两项失败均发生于后台claim依赖装配：test_thread_interrupt中的两个最小Store未提供7.10已要求的wakes.pending_one，尚未进入中断／结束断言。仅为这两个替身补显式只读查询接口；同文件12项通过，生产代码与默认部署不变，不用默认旁路掩盖遗漏。此前365项相关测试没有覆盖这两个替身，保留失败记录；这不是新片重构造成的回归，也不宣称第8步验收完成。

## 新版 TUI225—227：第7步框架收口与业务失败分账

发布源码7280c5b3e、wheel SHA256前缀c3f1242f；双机各1,274包文件逐项一致，默认入口／唯一Gateway同版。常规Git HTTPS运输失败后，经GitHub Git Database API逐项核对原blob、tree、commit SHA，远端main非强制快进到相同提交；没有重写提交或跳过本地严格gate。线上CI没有对应运行记录，未作为验收依据。
三路真实原生TUI分别为225本机授权续跑、226本机保留OPEN接手、227测试机与225同题；各一次普通中文需求，原生/model选择并核对官方MiniMax-M2.7服务端点，日常默认不改。原始线程／请求／claim／工具账、提示词哈希、模型来源、最终产物归档及进程证据留仓库外。

- TUI225：224.71秒，同run两次grant、三次孩子attempt，主子共7个attempt均done，无锁，三个孩子宿主退出。两条公开final为派出回执和唯一root_subagents_terminal交付，7条wake全部handled；未出现220重复收尾。两个grant均在旧session退出后，不能算217时序命中。
- TUI225业务失败：300行CSV逐行正确，父级实际执行完整逐行校验，并写入正确汇总；约34.23秒后孩子再次覆盖summary，最终平方和9025450、立方和2038532250均错，正确值分别9045050、2038522500。原工具账与孩子原生write_file参数证实覆盖顺序；父级之后未读回最终摘要，仍报告全部通过。观察者保留错误文件，不补产物、不发修复提示；这是共享文件交接与验证时机样本，不据此新增CSV专项核心门。
- TUI226：92.97秒，真实正式申请保持OPEN／无grant，孩子BLOCKED，父级接手；3个attempt均done、无锁、准确宿主退出。初始回执后只有一次subagent_non_success_terminal交付，未命中能力completed分支。240行逐行正确，父级实际检查平方立方和哈希；summary遗漏汇总合计，完整业务验收仍失败，外部读回不能补算模型履约。
- TUI227：332.56秒，两次grant、同run三次孩子attempt，主子6个attempt均done、无锁、三个准确宿主退出，7条wake全部handled。第二次grant在旧attempt DB结束后0.328秒、executor退出前0.556秒、session退出前0.593秒；同run随后第三attempt真实执行，**已命中217的原始接续交错且未永久PENDING**。公开final为初始回执加唯一capability_lifecycle_completion，能力事件下的完成交付实际命中，未出现重复完成回复。
- TUI227业务限制：父级实际校验并修正孩子错误平方和、补全哈希，最终300行与全部摘要正确；但自写检查程序在发现summary不一致时仍输出“全部校验通过”，后由模型读取差异修正。孩子再申请不存在的shell工具形成GAP／BLOCKED，完整“孩子交付两文件后父级复核”流程未达成。父级提出cancel_subagents，经原生界面只批准一次；回执为preserved既有BLOCKED、原执行权已done、无活资源，不能说产生了新CANCELLED状态。

三路终态后均通过原生/exit退出，准确客户端与孩子宿主不存在；只停本轮只读观察进程，共享Gateway保留。第7.9精确时序由227证实，第7.10两个因果红转绿用例和本轮真实无重复结果共同支持当前框架收口；本轮未证明必然撞上220相同的瞬时旧快照交错。旧220／217失败证据和所有业务失败不改报成功。第7步既有长任务、递归、取消／隔离、恢复矩阵继续见后文；本次修复没有改变Compact／长任务执行机制，不用重复空跑时长替代针对性验证。

## 第 7.7 步前台自然退出清理（默认双机运行路径已核对）

前台自然退出修复已集成为 `9330ee385`：从新 Popen 冻结出生身份，内核退出探测不回收组长，沿原终止链清理可验证后代后再获取原退出码；清理未知单列，不扩大后台／PTY 归属。
12 项新增定向覆盖包含 macOS 实际进程、Linux `/proc` 替身、身份拒绝及清理结果；联合 8 文件 215 项通过。旧 stdin 断言改为所有进程均关闭宿主输入，未降低隔离要求。
本地候选与主线均已过 Ruff、文档、严格尺寸、diff 和打包边界；`ed20fd061` 已推送远端 main，同一 wheel 双机各 1,274 文件一致，默认入口和唯一 Gateway 已切换并保留回滚。测试机发布脚本在切换后因清单哈希字段名差异未写完报告，已只读核对新进程、旧进程退出及包哈希后补齐私有报告，没有再次重启。TUI210／213 已核对本机直接回收与原退出码，214 核对 Linux 默认沙箱的直接非零退出和精确后代退出，215 正常三孩子并行与产物正确。211 未覆盖到位和212工作区准备失误单列；Windows 沿原路径不作新增回收承诺。线上未查到本提交的 CI 运行，验收来源为本地严格 gate。

## 第 7.7 步新版原生复验（按实际运行路径和缺口分账）

- TUI210 官方 MiniMax-M2.7 本机 329.8 秒自然结束，四组合重复完成的原工具账分别确认 TERM／TERM→KILL 后代回收，外部只读检查在最后一组孩子的 120 秒自然寿命前确认 PID 已消失；100 行平方逐项正确，合计 338350，原 attempt done、无锁。场景命令末尾 echo 覆盖了工具退出码，不能证明工具直接收到 7；模型夹具错误、两次真实超时和一次未执行的删除拒绝均保留。
- TUI213 独立原生任务 86.0 秒自然结束：模型先加 echo 后自行去掉，实际前台原命令 `COMMAND_FAILED`／return_code=7；原清理回执 `SIGTERM->SIGKILL`、confirmed=true、observed_processes=2、unresolved_pids=[]。原生后续只读检查返回 ESRCH，再次前台计算写出 20100；attempt done、无锁，测试者没有执行或修补业务脚本。
- TUI214 官方 MiniMax-M2.7 Linux 默认 bwrap 任务 141.0 秒自然 done：第三次前台工具真实 return_code=7／COMMAND_FAILED、stderr 空；原回执 SIGTERM、observed_processes=1、confirmed=true。宿主只读采样核对本次孩子的出生标识、NSpid 和两条私有输出管道，其在 120 秒自然寿命前消失；26 个已采样实例最终均无、原锁清零，后续计算 20100 正确。该回执只证明 bwrap 根快照，不能外推宿主直接对子进程 TERM→KILL；前两次 FD 错误后模型沿用了旧 ready，本次忽略 TERM 就绪未验证。模型把另一命名空间 ps 的 PID 当孤儿的报告错误保留。
- TUI215 在正确的 owner 内部工作区完成独立三孩子对照：143.2 秒自然完成，三个孩子实际重叠 31.6 秒、主子共五个 attempt 均 done；900 行逐项正确，number 合计 405450、square 合计 243405150、SHA256 与 summary.json 一致。三条直属通知 published 并 handled，无 closeout WAL／锁，已知执行宿主 PID 已消失。初次重复表头由模型在同一任务内自行发现并修正，测试者没有补产物；与仍运行的 212 时间重叠。
- 两次报告的自动系统回收归因没有证据。原生 IR 读回进一步确认：模型收到 stdout／stderr／return_code，但没有收到原账 `process.termination`；结构化清理事实的模型可见投影缺口留给第 8 步，不把原账成功冒充报告正确。
- TUI211 Linux 480.3 秒自然结束，初始夹具和后续模型改写偏离私有管道／就绪要求，不能算完整自然退出复验；四次最终外层退出码均为 0，子 PID 属不同 bwrap 命名空间，跨调用检查不能证明同一实例。初次真实超时的宿主六个已知 PID 已消失，正常平方和 333383335000 正确；TUI214 另开独立直接非零场景。TUI212 已运行长任务与 Compact，但本轮测试者把外部 cwd 选在默认 owner 墙外，孩子相对路径触发 PATH_OWNER_SCOPE_BLOCKED；按 owner_access 合同这是测试准备失误，不放宽权限，也不算正常三孩子对照通过。原任务后续自行整合并修正样例，正常三孩子验证由独立 TUI215 补齐。212 的 BLOCKED 运行账保留已确认符合既有合同；公开 final 被抑制另列7.8，不提前勾第 7 步。

默认运行路径的验收结论：macOS 直接后代回收／TERM 升级和原 0／7 已实证；Linux 默认沙箱直接非零及本次后代退出已实证，不能归因于同一升级路径。Linux 非 PID 隔离／Full Access 与本次 Linux 忽略 TERM 就绪仍未实证，不为取得指定信号回执放宽默认沙箱。

## 第 7.8 步能力请求唤醒后的完成交付（已发布双机，原生复验中）

TUI212 的工作区准备失误与后续框架现象分开：原生历史已有模型完整 final、turn_end_reason=completed，ConversationTaskLink 已 completed，最后后台 claim finished；公开 assistant final 未落账。交付裁决对 capability_request_open／granted 原因无条件 suppress，初步定位为旧唤醒原因覆盖新的完成事实。原生 PTY 的确返回 exit_code=0／36 passed；最终三个样例各400行，合计1200行平方均已被模型修正正确。所有 attempt 已结束，最后 claim finished、无本线程排队唤醒；一个孩子因 thread_goal_blocked 保留 AgentRun created，整树 TaskRun 因此未闭，这是既有 BLOCKED 保留合同，不是仍活执行或另一结算 bug。只修公开完成交付，不强制改运行账终态，不补写旧212回复。最小候选979ed5c76已集成为fc17def5f。新增状态矩阵、同片孩子BLOCKED、空载荷和canonical重放共16项：旧实现6红，修复后全绿。候选组合后台运行／Gateway控制／终态检查三文件348项通过，严格gate通过；主线相同三文件348项通过，Ruff、doc sync、strict-size（hard=0、blocked=False）、diff、clean-package全部通过；已随6c5fe6f36推送main，同包930f59f6双机各1,274文件一致，默认入口与唯一Gateway同版；216／217／218从原生TUI选官方M2.7进行实际复验，未提前计为通过。

第 7.8 新版真实验收（216／218 完成，219 未命中，217 接续停滞）：

- TUI216 本机206.96秒：真实只读派工，孩子正式OPEN申请、父级grant，同一孩子第二attempt完成；300行平方／立方逐行正确，摘要和SHA256一致。父级实际独立工具核验并公开final；主子5个attempt全done、无锁、两次准确宿主均退出。最终reason为subagent_runner_finished／root_subagents_terminal，证明授权链与普通交付，**不计为能力事件完成分支命中**。
- TUI218 本机140.92秒：三个孩子实际重叠30.88秒；初次合并表头错误由被测对象自行发现修复，600行立方、数字和180300、立方和32508090000及SHA256均正确。主子5个attempt全done、无锁、三个配对通知、准确宿主退出；公开final可见，216同期继续。测试者没有补业务产物。
- TUI216 能力OPEN工作片确实调用父级grant，但同一background request没有公开final；孩子后续完成才由runner_finished交付。未完成能力事件保持静默的原生负向分支有证据，completed能力分支仍缺实际命中。
- TUI219 本机125.53秒自然结束：200行平方与汇总正确，父子3个attempt全done、无锁、准确宿主退出。孩子实际未调用capability_request，canonical申请／grant均为空，状态DONE；父级最终报告和README却声称正式申请、孩子等待授权。此为场景未命中与模型报告错误，不能计修复分支通过，测试者不补申请、不改报告。
- TUI217 测试机正式只读授权场景停滞，具体事实与后续归属见7.9；保留原TUI和状态，不额外提示、不人工恢复。

## 新版 TUI220—224：授权接续与能力事件最终交付

安装源码67bb7817e、wheel83975aff，普通测试均由原生TUI一次中文需求发起，实际会话选择官方MiniMax-M2.7；日常默认模型未改。原始模型、请求、工具、claim、产物与进程证据留仓库外。

- TUI223本机87.24秒：三个孩子原DB执行区间重叠21.08秒，主子5个attempt均done，资源锁为零，三条完成通知各发布并消费；原共享孩子宿主退出。模型自行修正合并脚本错误，最终600行编号及立方逐项正确，数字和180300、立方和32508090000，摘要哈希一致。完成后的公开final走原root_subagents_terminal。
- TUI222本机148.68秒：真实孩子capability_request保持OPEN且无grant，父级接手生成240行数据，实际逐行验证工具已执行，外部只读核对数据、汇总和哈希一致。父子2个attempt均done、无锁、孩子宿主退出；最终回复在原前台轮完成，因此不计后台能力事件分支命中。
- TUI224本机122.96秒：父级先回复已派出并结束前台轮，原capability_request_open唤醒后台处理；同一后台工作片完成后公开final带capability_lifecycle_completion。原任务done、3个attempt均done、无锁，孩子仍BLOCKED，正式申请OPEN／grant为空，准确宿主退出。两条公开final分别为派出确认与最终交付，无同一后台请求重复final。第7.8所修正的真实正向分支通过，未人工改wake、任务状态或旧回复。
- TUI224业务核验另列：外部只读检查240行平方立方均正确、CSV哈希与摘要一致；但被测对象实际只运行行数、首尾／中间抽样和哈希，没有执行需求中的逐行核验及汇总合计。不能用观察者的完整检查补算模型已履约，不把框架交付成功记成完整业务验收通过。
- TUI220本机215.42秒：正式授权后同一孩子第二attempt实际执行，5个父子attempt均done、无锁，300行正确，父级自行修正孩子错误平方和及缺失哈希。grant在旧session退出后约0.18秒，故正常接续成立，准确旧片退出交错未命中。原生历史另见先由subagent_non_success_terminal交付，再由root_subagents_terminal回复上一轮已完成；只读已确认：旧BLOCKED通知先在前台handled，后被后台旧pending快照再次选中；此时孩子已DONE，旧非成功分支直接公开final，新DONE通知后续又公开一次。该通用通知缺陷归7.10，不提前计完全通过。第二attempt为Gateway内进程执行，不能因共享Gateway仍活就判资源泄漏或停止服务。
- TUI221测试机同题376.47秒自然完成，同一孩子两次正式申请／授权、三轮attempt，最终DONE；300行、数字和45150、平方和9045050、立方和2038522500及哈希正确，父模型自行修正孩子错误平方和并执行16项复核。7条wake均handled、无锁／WAL、三个准确宿主退出。两次授权分别在旧session退出约13.28／11.48秒后，准确交错仍未命中。公开final恰两条：前台等待确认和唯一后台root_subagents_terminal最终交付，没有220的重复后台完成回复。

## 第 7.10 步旧唤醒快照与重复最终回复（已发布并完成当前框架复验）

基线67bb7817e的两个独立因果用例先红后绿：预扫期间前台已handled的旧BLOCKED信封不得再开模型轮；当前来源已DONE且另有未读DONE信封时，旧BLOCKED不得绕过原完成邮箱判断直接公开回复。当前实现仅在原队列读取时机、claim后的窄来源准入及当前子树交付裁决收口，不新建执行器或持久状态。批次中真实失败、来源读取错误、控制、冻结重投及第7.8能力完成分支已进入六文件304项通过的组合。集成前发现资源停止夹具遗漏新显式依赖，实测1失败／13通过；补齐夹具后14项通过并纳入304项，不增加生产默认旁路。候选6c63e8cfd／9afe0417e已集成为f7742dcf0／965f7cf86，主线额外授权、runner收尾和持久交付三文件61项通过。自动回归不能代替最终验收；后续新版TUI225—227事实见本文件首节。

部署准备只清理测试机两份确认无进程或默认入口引用的旧安装环境；当前环境、上一版回滚环境、仍被旧客户端引用的环境和全部测试原账保留。私有切换脚本等待旧进程退出时改核对出生身份与进程状态，避免退出过程中command变化被误当PID换代；启动前还须确认原端口无监听。准备完成后已切换c3f1242f双机默认Gateway，保留83975aff回滚及切换前真实队列／运行账副本。

## 第 7.9 步授权与旧工作片结束之间的接续（修复已部署，原生复验中）

TUI217 原生一次需求真实派read_only孩子；孩子OPEN能力申请，父级grant准确delivery写入目录。
原grant回执为continuation=already_running／fresh_runner_session；随后旧孩子attempt结束BLOCKED，canonical被写为PENDING。
只读核对时父2个attempt、孩子1个attempt均done，没有执行中工具或资源锁；准确宿主已退出。
OPEN、grant和BLOCKED finished三条wake均handled，原后台claim finished、无pending wake，但父任务仍active且未创建孩子第二attempt。
这是实际授权接续未完成，不能因所有当前attempt结束或没有锁而记为任务通过；也不能用模型“启动中”证明执行器仍活着。
冻结快照377秒后再次读回，原状态、attempt和wake均未变化；准确TUI客户端保留，原宿主不存在。
源码交错定位：grant进入时旧工作片已生成BLOCKED结果，canonical归约为已授PENDING，但结束通知用旧结果把child link写BLOCKED，worker又按旧结果跳过接续；原生命周期门因此持续HOLD。独立owner在隔离目录先做确定性交错红灯，再修通知投影、当前状态接续和唤醒窄字段持久化，不通过放宽启动门掩盖失配。
原账已私下归档，冻结和五分钟后复核阶段未人工改状态或重发业务请求。定位不再依赖活现场后，测试者通过原生/stop结束旧217：父link=interrupted，孩子仍PENDING/link BLOCKED，原attempt全部done、无锁；随后原生/exit，准确客户端与旧宿主都不存在。这是失败测试的控制收尾，不是修复通过或孩子已取消的证明。候选634d12daf已集成为911a53245；三个独立旧红灯转绿，17个交错用例通过，9文件组合236通过／1项既有Linux /proc跳过，严格gate通过。真实RuntimeDB合同确认旧done经原派工入口登记同run第二attempt，重放不多开；父任务interrupted／cancelled均禁止新登记。主线授权／worker／后台交付三文件组合通过，严格gate通过；源码随67bb7817e推送main；同一wheel（83975aff）双机各1,274包文件逐项一致，默认入口和Gateway同版。TUI220／221复验授权续跑，222覆盖前台接手，223为三孩子并行对照，224命中后台能力完成交付；当前结果及缺口见上节，不进入第8步。

部署操作证据纠正：旧私有切换脚本把本机conversation存储key用于远端，远端930f59f6的切换前wake归档实际为空。数据库与旧运行环境备份仍保留；补存的是测试后的当前wake快照，不能冒充切换前备份或覆盖新任务结果。后续切换脚本按各主机真实canonical目录发现claim与wake，禁止跨机复用目录key；这是部署脚本缺陷，不计产品框架失败或通过。

## 第 7 步真实孩子失败及父级接手 TUI207—209

TUI207 先用独立原生模型配置经私有 loopback 夹具透传到官方 MiniMax-M2.7，主子 11 条请求均返回 200，未注入；60.6 秒自然完成，120 行立方数据与摘要／哈希正确，三个 attempt 均 done。
夹具正式入口固定官方上游，正文和认证透传，只按宿主稳定会话哈希及一次性 nonce 选中请求；测试会话头不发上游，原始内容／凭据不入日志，不更改系统网络或日常模型默认。
TUI208 的观察脚本误取线程最后一个关联任务，没有通过主角色定位根运行，故一直安全透传；两孩子和主任务完成、1,600 行正确，但没有故障注入，不计失败场景通过。
修正测试观察器后，TUI209 精确选中已有真实官方业务响应的一个运行中 child attempt，只注入一次 HTTP 400；该孩子原账 FAILED，另一孩子 DONE，未将故障扩散到父级或其它会话。
父代理收到原失败通知后自行运行孩子已写的脚本、补齐缺失产物并合并；99.9 秒自然结束，四个 attempt（含一个 failed）终态明确，失败孩子没有被后台重跑。
两条直属观察及两个 v2 published／retain_handled 回执均各一条、wake 均 handled，canonical 无收尾 WAL、作用域内无锁，共用孩子宿主已退出。
最终 1,600 行连续数字和平方逐行正确，数字和 1,280,800、平方和 1,366,613,600，summary 与文件 SHA256 一致。观察者只读核对，不执行或修补业务文件。
成功透传与一次拒绝证据分别保存；HTTP 400 故障不证明断网、任意进程崩溃或所有供应商重试。夹具已恢复 relay，后续停用只关闭本测试夹具。

## 第 7 步三子代理长任务 TUI202（生命周期完成，业务未通过）

同一新版测试机 Gateway 上持续 43 分 11 秒，三个孩子实际同时运行 213.2 秒，主任务及三孩子均 done；主代理和测试孩子各提交一次 Compact，随后继续执行至自然结束。
三条 exact attempt 完成通知均 handled、v2 发布回执均 published，三个 canonical 无收尾 WAL，作用域无锁／root claim，已知共用 dispatch 宿主退出；不外推为任意后代进程都已扫描。
原生工具账有孩子 105 passed、主代理 115 passed／10 warnings；10 个 smoke 函数以 return bool 代替 assert，pytest 明确警告，不能把该 10 项算成有效断言。
后续复合验证先输出 115 passed，随后 IndexError 使命令退出 1；另三项函数检查退出 0，原失败保留。
最终源码只读审阅发现 CLI 缺少可调用入口、文件分支全量物化、时区重标和符号错误、stdin 计数快照时机错误；报告 21 份样例中 19 项行数与磁盘不符。
因此自然长任务、Compact 和子代理交接按实测范围通过，完整业务交付未通过；没有补发修复提示、代跑程序／测试或补产物。

## 第 7 步本机核心长任务 TUI204（运行完成，交付未全通过）

同一新版 Gateway 下原生官方模型任务持续 1,888.1 秒，主 TaskRun／AgentRun／唯一 attempt 均 done，期间 205 停止另一子树、206 制造发布故障未中断该 attempt。
原生历史完整保存 62 项 pytest 输出（62 passed，2.68 秒）及三个示例输出；最终文件确有 62 个测试函数。命令使用了管道，因此管道退出 0 本身不能代替 pytest 输出证据。
观察者只读源码与原账，未导入或执行被测程序／测试。最终源码静态分支追踪发现相减在右侧集合先耗尽后漏掉左侧剩余区间，另有布尔边界被当整数、双集合字典分支未解包等问题；自写测试通过不证明完整交付。
例如左侧 `[1,5), [10,15)` 减去 `[2,3)`，源码尾部只追加当前余段，遗漏 `[10,15)`。这是一项静态审阅结论，不冒充额外真实 TUI 执行。
终态后无该 attempt 的资源锁，但两个早期前台测试的嵌套 Python 子进程仍存活并占 CPU：一条业务脚本仅杀父进程后返回 0，另一条 communicate 超时未清孩子后返回 1。它们不是宿主 Shell 超时分支，也不是后续 PTY。
原生 `/stop` 返回没有运行内容，两进程仍存活；普通退出路径未清理前台进程组后代，作为生产框架缺口继续修复。私有证据保留出生时刻、命令、cwd、进程组和原工具账，历史父子关系没有直接持久记录，归属是多项事实强关联。
取证后仅向重新核对出生时刻／命令／cwd 的两个准确 PID 发 SIGTERM，确认退出；这是人工测试资源清理，不能算产品停止通过。
本机曾被独立决策模型测试夹具误写共享配置，造成随后新 TUI207 的模型菜单失败；按原迁移源码核对后精确反迁移并隔离测试项，已由实际安装版重新读取成功。未重启 Gateway、未修改日常默认；该操作是测试环境修复，不是产品验收通过。

## 第 7 步原生 TUI206 发布失败与自动恢复（通过）

新版 TUI206 在官方模型原生单孩子任务中命中受控发布失败：只在新 thread 原本不存在的观察 JSONL 路径建立空目录，未替换既有记录或业务产物。
孩子完成后留有 v2 prepared 与 canonical runtime_closeout_pending；保存证据后仅 rmdir 撤掉同一空目录，没有调用恢复 API、追加业务提示、重启 Gateway 或手动改状态。
原 sweep 自动将同一 signal ID 补成 published，只有一条 handled 通知及一条配对观察，WAL 清除；孩子仅一个 attempt，父级自然续接，三个 attempt 和 TaskRun 均 done。
最终 squares.csv 600 行、逐行数字与平方正确，数字和 180,300、平方和 72,180,100，summary 一致；无资源锁，原孩子宿主退出，普通对照 TUI204 同期继续。
该用例证明实际发布失败后的幂等补齐和父级交接，不冒充孩子 FAILED、硬断电、任意 JSONL 字节损坏或进程强杀恢复；初次失败时信号可能已安装，不能说通知从未可见。
观察者只读核验数据和运行账，除上述精确空目录故障夹具外未写被测数据；206 已正常退出客户端。


## 第 7 步新版递归协作 TUI203（已核对）

新版 TUI203 递归协作已独立核账通过：请求到 TaskRun 关闭 259.9 秒；主代理、协调代理和两孙代理共 6 个 attempt 全部 done，孙代理实际重叠 19.3 秒。
两分片各 1,200 行，合并 2,400 行、单表头、数字连续无遗漏重复，逐行平方、数字和 2,881,200、平方和 4,610,880,400 及三份 CSV 哈希全部正确。
主代理另有两次真实工具核验且退出 0。协调中途曾出现重复表头、命令退出 1／141，由被测对象自行修正，观察者没有补文件。
直属协调仅发布一条 exact attempt 的 handled wake，v2 回执 published、retain_handled=true，配对观察一条；孙代理没有越级通知根会话。
本树 canonical 全 DONE、无 WAL／锁／root claim，四个子代理宿主均有 exited 记录且准确 PID 已消失。
这项仅覆盖新版递归自然交接与任务交付，不覆盖发布半写、进程重启或长任务 Compact。


## 第 7 步新版双机部署与原生 TUI（进行中）

第 7 步组合源码 `a067baddd` 已构建同一 non-editable wheel（SHA256 前缀 `e36a9ee2`），
本机与测试机各 1,274 个安装文件逐项一致，默认入口和各自唯一 Gateway 已正常切换；旧环境及切换前记录保留。
新版使用 v2 唤醒发布记录后，不能直接换回旧写入进程或用快照覆盖新结果；回退须先协调数据格式和新任务事实。
本机 TUI204／205、测试机 TUI202／203 均从原生界面选择官方 MiniMax-M2.7，实际 provider/base 与会话绑定已私下读回；
本机另通过原生模型菜单新增本轮独立官方地址配置，日常默认和原配置不变。两端均已收到真实模型响应。
当前 202 项目长任务、204 普通对照继续运行；203 递归产物、生命周期和资源退出已核对通过；205 原生停止后 152.3 秒复核：两孩子仍取消、无新 attempt／锁，原宿主已退出，204 继续运行、Gateway 未变；205 已原生退出并保留画面。
本轮远端查询仍超时，最新代码未推送，线上 CI 未作为验收来源；不能把部署冒充第 7 步完整验收。


## 第 7 步恢复与父通知组合验证（本地）

第 7 步恢复扫描与父终态通知已组合到原 checkout：原 sweep 绑定同一窄通知器，恢复／清账故障测试迁到类方法，旧全局接口调用已清零。
27 个相关测试文件联合 **665 passed、2 项既有 skipped**（55.68 秒），覆盖发布恢复、父子交接、结果提交、调度、控制和资源停止；没有把两条独立测试计数相加。
Ruff、导入边界、doc sync、严格尺寸、diff、clean-package 已通过；此组合随后已双机部署，最新代码远端待推送，线上 CI 未作为验收来源。
本片不改变原 WAL→运行结算→通知→delivered→清账顺序；未扩改阶段提醒或能力申请。后续只做发布及本版本原生 TUI 验收。


父终态通知候选：直属父级／服务窗口／活动提醒 56 项，加外部代理控制／资源停止／结果状态 74 项通过，
共 6 个直接受影响文件 130 passed。待恢复扫描片组合后迁移其通知装配及故障替身，不能单独发布，
不以本地合同回归代替新版真实 TUI。

第 7 步通知配对修复与初次提交依赖收窄已合入原 checkout。18 个组合定向文件 **391 passed、2 项既有 skipped**，
包括原两项半写红灯、配对恢复矩阵、无 manager 提交的顺序与各写点失败、分页恢复、父级交接、
Goal／观察路由、结果状态、调试 trace、运行守卫、owner 唤醒发现、后台读回和调度。
这轮只证明本地组合源码；尚未发布／部署，TUI199—201 属于前一运行包，不能替代新版本验收。
本地严格 gate 已通过：相关 focused、Ruff、doc sync、strict code-size、diff、clean-package 均无阻塞；
新增行隐私模式扫描未命中。未运行全仓 pytest，线上 CI 没有作为验收来源；远端查询本轮超时。

### 恢复扫描显式依赖（独立本地候选）

基线 `9bbfc0a01` 上继续收窄 `restore/_restore/advance/recover`，唯一生产装配仍在原 capability sweep；
既有分页、半写、旧 attempt、通知与清账故障用例逐一迁移到显式依赖，不重跑业务。
新增 9 项无 manager 的恢复合同用例，覆盖 `repo=None`、精确通知负载、已交付只清账、坏结果不挡后项、
先 WAL 后事件消费及消费标记失败后重入；另有 1 项从原监督入口推进真实临时文件 WAL 的装配回归。
10 个直接相关文件联合 **291 passed、1 项既有 skipped**；Ruff、导入边界、doc sync、strict code-size、
diff、clean-package 通过，新增行隐私扫描无命中。命令与未覆盖范围见[本片交接](docs/tasks/HANDOFF_STEP7_CLOSEOUT_RECOVERY.md)。
前述 391 项组合结果属于基线，不冒充本片或并行父通知新实现的已发布验收。

## 第 7 步子代理结果链发布与验收

配对发布半写修复已在隔离线本地验收，尚未发布：`test_closeout_wake_receipt_half_write_does_not_duplicate`
两个参数用例在修前确定性得到 2 条通知而非 1 条；修后在 wake 安装后的最终回执原子替换处故障注入，
pending／handled 两种重试均保留一条 wake 和完整原观察。新顺序先预留再安装，未删原数量断言。
原已部署包不受本地修复影响；完整依赖收窄与本版本发布验收仍属第 7 步。

本片 11 个直接相关测试文件 **261 passed、1 项既有 skip**；其中新文件有 32 个通过用例：
8 个文件边界故障 × 是否消费、同键并发与重放、普通 Goal handled 后继续、旧 v1 纯读及显式迁移、
坏账保留、原投递冻结和无 key 失败不得伪造无链观察。查询还核对文件集合不变和发布中只读 prepared 快照。
`test_wake_publication_recovery.py` 之外，同跑 `test_dispatch_liveness_and_revive.py`、
`test_conversation_wake_events.py`、`test_closeout_recovery_paging.py`、`test_direct_parent_lifecycle.py`、
`test_subagent_runner_result_state.py`、`test_service_window_semantics.py`、`test_conversation_store.py`、
`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、`test_observation_route.py`。
具体命令、严格 gate 与未覆盖项见[配对发布交接](docs/tasks/HANDOFF_STEP7_WAKE_PUBLICATION.md)。
本片不做宿主自动恢复扫描；prepared 成功后仍需调用方重试，runner 复用原 WAL；无 key 不承诺重试幂等。
离线故障回归不等于真实 TUI、硬断电或所有通知入口自动恢复，线上 CI 未作为验收来源。
## 自然长任务结束与稳定历史缓存补充

官网 M2.7 的 fd→Python 真实任务已自然结束：canonical TaskRun/主代理/三个子代理均 done，
持续 6572.58 秒（约 109.5 分钟），自然 Compact 三次，末段 TUI 显示 348 个逻辑模型轮。
任务结束前 1180 次采样未见未同步或观察错误，状态 P95/最大 43.47/1770.05 ms；
1 CPU/2 GiB cgroup 峰值约 1425.78 MiB（包含文件缓存和多端对照），memory failcnt=0。
这一长任务跨越几个候选客户端版本，不能写成最后缓存补充片已单独运行满 110 分钟。

- 模型独立生成 Python 项目；原工具账保留完整 pytest 输出，24/24 passed、return_code=0。
  上游固定版本、源码和双许可证实际存在。这里只确认真实开发/测试/修复过程和自测结果，
  未宣称全部上游行为等价；当前自测没有覆盖所有 ignore/exec 等需求，已有工具失败记录也不改写。
- 长历史中发现额外显示成本：相同版本、同任务的累计/新恢复 TUI 同窗 CPU 26.49%/17.36%，
  采样定位到重复快照、全历史版本键和稳定行重组。本补充片复用发布快照、稳定/活动版本键、
  有界静态前缀及已净化行组；活动块与插话仍按同一事件顺序合流，不删历史、不再降低帧率。
- 最终对照先在任务结束后的同一历史恢复两个新 TUI，确认画面逐行相同，再经原端提交一次普通中文续聊需求。
  官网 M2.7 实际执行 `sleep 30` 并回答十七加二十五等于四十二；三个端都收到唯一完整回复。
  两个观察端同时采样 40.19 秒：基线/候选 CPU 4.68%/2.21%（约降 52.7%），RSS 101.19/100.87 MiB；
  Gateway 同窗约 5.77%，状态查询无失败。这是等待与回复窗口的对照，不外推到所有活跃输出或内存降幅。
- 在最终原生 TUI 中从阅读处 Ctrl+O/Ctrl+E 进入第 2384/2386 段附近；上下键、上下滚轮、跨段翻页、
  100↔120 列 resize 与回到最新都通过。首次详细展开约 1205 ms，原文展开约 72 ms，后续六次滚动约 28—32 ms；
  包含 tmux 注入/捕获开销，首次展开仍有明显重排成本，不宣称所有操作都在几十毫秒内完成。
- 289 项显示/状态/输入/历史回归通过；覆盖缓存失效、插话顺序、同块替换、宽度变化、字符预算和终端过滤。
  最后四个生产文件 SHA256 与测试机一致。本次全部测试 TUI 已 `/exit` 且准确 PID 消失，Gateway 未重启、running attempts=0。
  清理后 20 秒采样：Gateway CPU 2.43%、RSS 297.08 MiB，状态 P95 5.46 ms；cgroup 1117.76 MiB 包含文件缓存。

原始证据在私有 `fd-port/`：business-terminal/messages/thread、project-test-tool-evidence、port-artifact、
frame-cache-equal-history、business-observation-summary、cleanup-result 与 final-idle-summary。
其它阶段的对照记录保留，不能把历史不同或任务结束过渡窗口的数字替换成最终等历史对照。

## 真实开发中的 TUI 重绘对照

在同一官网 M2.7 的 fd→Python 真实任务上，只读恢复两个原生 TUI，未重复发送业务需求。
相同 120×36 终端、同一 1 CPU/2 GiB cgroup，两个客户端同时观察同一会话。
基线为 60 Hz 帧合并/8 Hz 周期动画，候选为 20 Hz/4 Hz；原始 TUI 和 Gateway 也仍在同一限制中。

- 30.78 秒同时采样：基线 TUI 单核 CPU 16.02%，候选 10.23%，下降约 36.1%；RSS 为 101.43/98.61 MiB。
  Gateway 同窗 CPU 12.93%、RSS 188.15 MiB，cgroup 峰值 1321.41 MiB，状态查询无失败。
  该结果来自真实压缩/开发阶段，不把降低帧率称为长期内存容量通过。
- Ctrl+O/Ctrl+E 在当时正在阅读的真实工具记录附近进入完整原文，第 125/128 段；两个视图内容一致。
  每端各 12 次上下翻页，均有可见移动；基线中位/最大 34.33/43.27 ms，候选 38.09/68.18 ms。
  测量包含 tmux 按键注入和画面捕获开销，不是人类屏幕刷新率测量。
- 候选原文上/下键和上下滚轮都实际移动，四次可见延迟 28—71 ms；100 列 resize 后可继续读取。
- 157 项 UI/输入/线程/reducer 定向测试通过。原始证据在私有 `fd-perf/` 的 samples、summary、
  key-latency、raw-events 和逐次终端帧。frames 合并只限制绘制，不丢消息、工具结果或思考记录。

## TUI 等待超时与迟到终态

并行真实测试发现原请求已经 done，而原 TUI 停在等待超时、只有重连才读到最终回复。
当前客户端补充片让 canonical terminal 优先于观察截止点，并保留同一请求和 chunk cursor 继续观察；
超时不创建新任务、也不把展示去重归属留给一个已放弃接收的 worker。无活动时轮询退避至 1 Hz，页面退出收口。
plain 等有限等待语义保持。Gateway client、TUI worker/threading、CLI parser 定向测试 **109 passed**。

官网 M2.7、专用测试机同一 Gateway 的真实对照：观察窗口设为 0.2 秒，普通中文要求实际等待三秒后回答。
基线服务端 11 秒后 done，原 TUI 始终停在等待超时，没有“十七”；候选同场景在原页显示工具过程和“十七”。
再只对测试客户端 SIGSTOP 9.28 秒，原任务在暂停期间完成，SIGCONT 后原页显示“四十二”，无重复提问或回复。
Gateway 全程同 PID，fd 开发任务与子代理没有停止；两条候选请求各只有一个 canonical request id。
原生 `/exit` 后测试客户端退出。私有证据在 `late-wait/` 的 baseline/fixed/paused terminal、TUI 与 timing 文件。

## 真实开发长任务验收方法

用户明确要求长对话验收使用真实项目开发过程，禁止把重复生成的大行数当作真实任务通过依据。
当前选择让官网 MiniMax-M2.7 的 my-agent 在原生 TUI 中把 GitHub `sharkdp/fd` 从 Rust 复刻为 Python，
自行读源码、实现、运行测试、修复并提交项目产物。测试者只提交一次普通中文需求并观察，不能代写或补交产物。
记录自然产生的模型/工具回合、Compact、TUI 状态、CPU/RSS、退出/恢复与任务结果；未实际发生的长历史边界不计为通过。
下文合成一万/千万行记录只作为存储边界和缺陷复现，不代表此类真实开发工作负载。
真实任务已自行获取源码、派出并收回三个子代理，自然触发 Compact generation=1 后继续开发；
当时仍在兼容性测试和修复；最终自然结束及验收边界见本页顶部。HTTP 请求交接为 done 不代表后台业务任务结束，
监控须继续看原 run 的 canonical attempts 和任务事实，不能据此停止 Gateway。
截至本次阶段记录，真实需求已运行约 57 分钟、168 个模型轮；累计 552 次只读采样未见未同步或采样错误，
状态查询中位/P95/最大约 4.23/28.49/1770.05 ms，cgroup 峰值约 1326 MiB（含文件缓存与对照客户端）。
原始 TUI 与 A 对照端经 `/exit` 正常退出，准确进程消失；B 端继续观察同一后台任务，Gateway 未重启。
保留原工具和项目失败记录，模型仍自行修复兼容性，不能把此阶段记录称为 fd 项目通过或任意时长保证。
后续已沿同一任务完成采样并核对原账与产物；原始证据在私有 `fd-port/`，不再补合成长行数充当真实任务。

## 官网真模型与TUI媒体验收

2026-09-23，独立候选线，专用测试机限制为 1 CPU / 2 GiB；官网直连，不通过中转，不使用假模型作为本轮验收。

- 官网 MiniMax-M2.7：100 独立用户身份各发送一次中文普通请求，100/100 terminal=done；处理槽峰值 50。
  同时一个原生 TUI 发问并正确回答 `5+6=11`。204.3 秒完成队列，44 个实拍终端帧未见“未同步/刷新失败”。
  cgroup 峰值 1047.1 MiB（含文件缓存），末次采样 Gateway RSS 313.8 MiB、TUI RSS 62.6 MiB。
  真实 `/status` 全部成功，但高峰 P95 3268 ms、最大 4897 ms；单核批量冷启动仍有排队和刷新延迟。
- 官网 MiniMax-M3：实际端点 `https://api.minimax.cn/anthropic/v1/messages`，现有私有 key 短请求确认返回 M3。
  原生 TUI `/attach` 添加 PNG，正确识别红圆、蓝方、绿三角及 `Q7N4`；终端 bracketed paste 拖入 MP4，
  正确识别红→蓝→绿和 1→2→3。测试提问未提供答案；未代模型执行视觉工具。
- 私有只读请求观察器确认真正外发 image/png 6484 字节与 video/mp4 5774 字节，SHA256 与素材一致；
  观察器调用原 HTTP 函数，不替换供应商、不改变请求/响应。模型工具轮为零。
- 真正 `/exit` 后重新启动同一会话，问图片和视频背景，M3 正确回答白色；请求再次带相同原件字节。
- macOS 隔离 Gateway + 原生 TUI，系统图片剪贴板经 Ctrl+V 成为附件，官网 M3 正确识别同图；原剪贴板完整恢复，
  本机隔离测试 TUI/Gateway 已退出。无改动用户日常模型/默认 Gateway。
- `input_media_max_bytes=16 MiB` 同时限制新输入和一次供应商请求的媒体展开；新近附件完整、超预算旧附件明确
  投影为归档引用，canonical refs 和原件不删除。owner 越界、符号链接、同长度内容变更、总量/数量超限均有合同验证。
- 相关组件矩阵当前为 1008 passed、1 skipped；跳过项仍为原 HTTP stop fixture 的 409，自行 skip 不计入通过。
  单测只验证协议/资源/输入边界，真实可用结论来自上述官网模型与原生 TUI。
- 无 checkpoint 的一万行历史：真实 TUI 续聊完成，自动压缩 generation=1 后正确回答 `4+4=8`。
  终态用时 410.81 秒；账本记录官网 M2.7 的 4 次供应商调用均 finished、0 retry，输入 294653 / 输出 1621 token。
  该用时不能算低延迟通过，也不能仅凭单次采样栈归因给 Compact 二分预算估算。

**未通过边界**：千万行浏览成功不等于千万行任意状态续聊成功。对 10,000,000 行、约 2.43 GB、
无 Compact byte checkpoint 的历史，隔离只读子进程在 384 MiB 地址空间上限下调用 `after_compact_report`
立即产生 `MemoryError`，还没有发起模型请求。`append_once` 的全量去重读取也需后续治理。
相关有界读取、分批 Compact 必须与另一开发线正在修改的 scope/checkpoint/CAS 合同合并验收。
本轮不声称无限时长、任意历史规模、100 个重工具或真实 IM 平台账号已通过。

证据保存在仓库外 `tui-real-media-20260923/`：`real-model/` 的 submissions/terminals/samples，
`media-*-tui.txt`、`media-provider-requests.jsonl`、`real-10k-history-*`、`uncompacted-10m-read.json`、本机截图粘贴验收。
旧假模型记录仍保留用于定位，不作为本轮通过依据。未推送、未替换用户默认环境。

## TUI 资源与空闲用户验收

独立资源线，未替换用户默认 Gateway。专用测试机始终只有一个真实 Gateway，TUI 为真实 tmux
终端；cgroup v1 对 Gateway 与所有测试 TUI 合计限制 1 CPU、2 GiB、无交换。模型与合成规模证据分开。

- 同一份 3035 条保存历史、2097 块、11824 展示行的只读 A/B 回放：基线稳定重绘中位数约 38 ms，
  候选约 4 ms，终端控制字符过滤保留。这不是按键端到端延迟或跨机 CPU 对比。
- 真实生成并读取 1 万条和 1000 万条 canonical JSONL；后一文件 2.43 GB。最近页和连续旧页各 80 条。
  一千万行原文通过产品 writer 生成 39063 页，首尾索引为 0 / 9999999，读取单页约 0.5—1.0 ms（热缓存）。
- 原生 TUI 实际恢复千万条会话，Ctrl+O/Ctrl+E 保留当前消息位置；下键进入原文、滚轮继续向下、
  上键跨回消息、120→100 列 resize 均有终端文本证据；`/exit` 后准确 PID 消失。
- 该长历史 TUI 与 Gateway 的 20 秒采样：cgroup 峰值约 164 MiB，Gateway/TUI 分别约 1.14% / 1.00%
  单核 CPU；40 次 `/status` 无失败，中位 2.62 ms、P95 3.61 ms。
- 官方 MiniMax-M2.7、`anthropic_compatible`、官方 MiniMax Messages 端点的真实 TUI 已完成普通中文问答；
  私有密钥与完整配置不入仓。最终 20 个变更生产文件 SHA256 与测试机一致：真实执行 `sleep 15` 完成后恢复对话；
  另一 TUI 在真实 `sleep 60` 执行中 `/exit`，终端进程消失而 Gateway 仍 processing=1；
  新 TUI 恢复同一会话后收到“后台继续执行验证完成”，未重新发送需求。
- 100 个独立 local owner，经真实 HTTP 鉴权、原 durable queue、真实 agent 初始化，使用仓库外假
  Anthropic 服务分别发送一次普通中文消息：两轮有效压测各 100 个终态均为 done，处理峰值 50。
  后一轮另有一个原生 TUI 真实排队并收到假模型答复，60 秒/30 帧未见未同步；总模型任务 101。
  假模型故意等待首批 50 个并发再释放，因此总耗时不能当真实模型延迟。不是 100 个真实 IM 平台账号验收。
- 后一轮 cgroup 峰值约 673 MiB（包含前轮累计文件缓存），任务结束后抽样匿名驻留约 238 MiB、
  文件缓存约 442 MiB；不得将 cgroup 总数等同独占堆。第一有效轮后 Gateway RSS 约 194 MiB、
  空闲 CPU 约 2.04%，状态 P95 4.20 ms。CPU 满载时会饱和，执行槽上限不等于立即响应承诺。
- 压测发现策展关闭仍构造两个软实例、完成回收受 60 秒派发节流影响；已经修复全局关闭与及时回收，
  针对性测试通过；最终同机实例池从 0 按原 owner 重建、请求 done，再在空闲期归零。
  轻量身份登记可保留 64 条软事实，不等于存在 64 个执行槽或 TUI。
- 初次假模型没有实现原生工具能力探针，100 条均被框架拒绝，记录保留但不算有效聊天验收。
  合成夹具两次构造失误（时间字段/显示身份）也未计入通过，未为了夹具改变产品协议。

最终定向矩阵 818 passed、1 skipped；跳过项是原 HTTP stop 测试夹具返回 409，未计入通过。
Ruff、doc sync、strict code-size、diff check、clean-package 均通过；未推送远端，线上 CI 未作为验收来源。
真实 HTTP 另有 8 个半请求在约 5.13 秒全部关闭，期间 35 次健康查询无失败。
现有强制网络故障仍必须显示未同步；没有以隐藏错误、扩大超时、删历史或取消用户任务来制造成功。
未验证 100 小时持续运行、任意单条超大 JSON、100 个真实 IM 同时操作及 50 个重浏览器/扫描进程。

原始日志、配置、终端快照和测试驱动位于仓库外 `tui-scalability-20260923` 私有证据目录；
设计和参考源码边界见 [资源寿命](docs/design/TUI_RESOURCE_LIFETIME.md)。

## 第 7 步子代理结果链已发布 main（未切换运行环境）

独立工作树已拆出 runner 状态展示、完成交接信封、exact attempt 准入与
“结果先落盘、再 WAL、再运行账、最后父通知”的初次提交编排；旧函数和导出已删除。
上述源码现已应用到原 checkout，与并行的 TUI／历史修复同源运行定向回归；
组合源码又跑过 17 个跨线定向文件及 9 个恢复／资源／层级文件，均无失败。
Ruff、导入边界、doc sync、strict code-size、diff、clean-package 均通过。
组合 wheel 发布边界检查为 forbidden／source_missing／source_mismatched／resource_missing 全部 0；
SHA-256 为 `6b66d2a4f9dc3f3df8902bdbd4181c733a9c15dc224c474c2c5ca9d36b62ce71`，
四个新模块与 TUI 阅读模块均在包内。原生 TUI 验收仍待完成。
首次推送后复查发现同文件注释插入 import 组触发 Ruff I001；该版本未切换任何 Gateway，
本次移正注释并重新通过 focused tests、Ruff、导入边界、doc sync、strict code-size、
diff、clean-package 与修正 wheel 的发布边界，以上 SHA 只指修正包。
终态冲突原先漏记 `closeout_blocked`，现写入 manager 的原 RuntimeDB。
首次父通知后若 `delivered` 标记落盘失败，保留 pending WAL；
故障注入验证恢复后同一 attempt 的 wake 仍恰好一条。

10 个直接受影响文件 **229 passed、4 项既有 xfail、1 项既有 skip**；
另 7 个多层恢复、资源停止、登记产物和离线工作流文件 **68 passed、1 项既有 xfail**；
再补 5 个 worker pool、作用域、创建幂等和层级合同文件 **47 passed**。
Ruff、doc sync、strict code-size、diff、clean-package 通过。
仓库外构建的候选 wheel 含四个新模块，包内 `agent_py_agent/` 文件共 1,271 个。
组合源码与注释排序修正已推送远端 main（`c9042f5f2`）；测试机安装的 1,273 个包文件与修正 wheel 逐项一致。
原 Gateway 在 pending／processing 均为 0 时正常终止，确认旧 PID 和端口监听均消失后，
从新版运行环境启动唯一 Gateway，并将默认入口指向同一运行环境；旧运行环境及入口回滚副本保留。
本机仍有原任务处理，尚未切换；
这些离线回归不能替代官方 MiniMax-M2.7 的第 7.6 项多路真实 TUI 验收。
同一修正版 wheel 已在本机独立候选环境安装，1,273 个文件逐项一致；默认入口和 Gateway 未切换。
本机后续核对发现 HTTP processing=0 时，canonical RuntimeDB 仍有由原 Gateway 执行的最新 running attempt。
因此切换前必须同时核对后台执行轮，不能单靠前台请求队列判空闲；测试机首次切换前未独立保存
后台 attempt 快照，恢复观测后须补查原运行账，不能据当时队列为 0 宣称全部后台任务空闲。
隔离线交接见 [第 7 步交接](docs/tasks/HANDOFF_STEP7_SUBAGENT_LIFECYCLE.md)，
正式验收矩阵见 [唯一 TODO](docs/tasks/REFACTOR_PLUGIN_GOAL.md#第-7-步当前-todo子代理状态和交接按序小片推进)。

### TUI200 有效长任务自然收尾（准入收窄运行包）

官方 MiniMax-M2.7 的原生 TUI200 从单次需求持续工作 1,952.8 秒（约 32.5 分钟），
三个并行孩子均 DONE，主代理在所有孩子结束后自然完成；期间发生一次 Compact，之后继续读文件、
修复偏移和导入错误并运行测试。原生 run_command 回执为退出码 0、`62 passed in 5.87s`，
磁盘测试文件确有 62 个测试函数，源码／README／RESULT 已读回；观察者未执行或修补被测项目。
三份 canonical 子任务均无待处理 WAL 或 runner_last_error，三条精确完成通知各一条且已 handled，
全部五个执行轮的资源锁为零，子代理宿主进程已退出，共享 Gateway 保持原 PID。
本轮证明有效长任务、工具错误自修、多孩子交接和一次压缩后继续工作，不替代第 8 步完整 Compact 合同。
交付仍有缺证：DESIGN 只写上游版本标签，未读回可追踪提交号／来源核对证据；
测试通过不证明全部兼容性说明正确，不能把这一项写成完整业务交付通过。
199／200／201 均核对终态后从原生 `/exit` 正常退出，仅保留 tmux 死窗及仓库外证据；未停止 Gateway。
原页超时的 197 保留排查，通知半写修复仍待新包验收，本结果不能代替该故障修复。

### 准入依赖收窄（测试机已部署，本机与远端待同步）

结果准入与终态冲突诊断只需要原 RuntimeDB，现改为显式接收该依赖，不再传入整个 manager。
结果服务在原调用位置取出依赖；canonical task、exact attempt 裁决、None 文件模式和写诊断边界不变。
两个直接受影响测试文件通过，覆盖原冲突诊断、旧轮拒绝、一致终态重入和持久恢复。
Ruff、导入边界、doc sync、strict code-size、diff 和 clean-package 均通过，未运行全仓 pytest。
该片以 `8ca9889aa` 本地提交，发布前重新通过上述严格 gate；构建源码为 `3b5f9943e`，
wheel SHA-256 为 `c751ee272dc66193d957c1a7802f16b8b00e6caadfdb0d37bbb64c21e1e2db90`。
仓库的 distribution boundary 四类结果全为 0。临时自写的宽泛路径检查曾把合法内置素材和非发布脚本
误计为禁入／漏带，随后改用仓库已有发布合同核对，没有因此修改包或放宽发布规则。
测试机已逐项核对 1,273 个文件，保留旧运行环境和入口回滚副本后正常切换唯一 Gateway 及默认命令。
切换前同时保存 HTTP pending／processing 为 0 和 RuntimeDB attempt 快照：本日没有未结束的执行轮；
旧历史未结算行单列保存，没有擅自改写为终态。原 Gateway 退出、端口释放后才启动新版。
本机原 Gateway 仍有活动后台 attempt，未切换；GitHub 连接失败，最新提交尚未推送，不把本地 gate 视为线上 CI。

TUI199／200 从新版默认入口分别发起父子孙文件清单项目和三孩子并行的 Python 十六进制查看器项目。
每个任务只提交一次业务需求；199 另在明确等待孩子时插入一次进度询问，以验收等待时插话。
两条 canonical thread 绑定同一已核对的官方 MiniMax-M2.7 配置，后端为 anthropic_compatible，
服务商地址为 `https://api.minimaxi.com/anthropic`，且已有实际模型响应；未记录或公开密钥。
证据为会话绑定、私有配置白名单与原生响应，不冒充网络抓包。

199 已自然完成：原生历史包含 20 项 pytest 通过的工具输出，磁盘独立只读重算得到
26 个样例文件、2,397,671 字节、两组各三个重复内容文件，与原报告一致。
权威树只有一个协调孩子、两个孙代理；三个下级的终态／pending closeout／执行进程退出均核对，
每一级最终完成时间晚于它的孩子。根会话只有协调孩子的一条完成通知，且已 handled，
没有把孙代理的完成越级广播给根。等待时进度询问在原生历史出现一次，模型回答进度后继续原任务，
原树没有重复派工。此任务约六分钟，不算所需长任务；测试使用 pytest 而非全标准库、
个别断言覆盖较弱、最终报告对测试编写者有不一致归属，交付限制与框架通过分开记录。
200 仍在整合代码和修复实际测试，已持续超过十五分钟；最终交付和长任务完整验收尚未收口。
201 由原生 TUI 派出两名孩子，确认两个原 attempt 均运行后通过 `/stop` 停止当前任务。
两孩子的 run／attempt 和 canonical 状态均取消，runner session 为 cancelled、无 pending closeout，
共同执行宿主已退出。约 139 秒后复核没有新增 attempt，三个原执行轮的资源锁均为 0；
200 仍由同一 Gateway、同一后台 attempt 持续运行。201 根的前一已完成执行轮保留 done，
不能改写成该历史轮被取消；原 TUI 正确显示两个孩子已停止。
TUI195—198 仍只覆盖前一发布包，不用于证明准入收窄片通过。

### 第 7 步首组真实 TUI（基础交接已核对，完整矩阵未收口）

TUI195、196、197 分别测试单孩子交接、三个并行孩子合并和独立主任务对照。
三个客户端均由新版默认入口启动，通过原生模型菜单为各自会话选择官方 MiniMax-M2.7，
服务商端点为 `api.minimaxi.com/anthropic`，没有改变日常默认模型。每个 TUI 只提交一次中文需求；
首轮画面已观察到 195 的 create_subagents(items=1)、196 的 create_subagents(items=3)，
197 则独立准备写入数据，真实模型响应和终端流留在仓库外。

提交后的 SSH 观察命令等待约七分钟仍未返回，随后只终止该本地只读观察进程；
另一次有界连接在横幅交换阶段超时。TCP 端口可连接不证明业务就绪，SSH 超时也不证明任务已结束。
SSH 恢复后读取原生运行账、父子通知和磁盘产物，Gateway 保持原 PID，没有重启任务或补写产物。
195 的 240 行数据、196 的三个分片及 1,200 行合并结果、197 的 600 条记录和各自统计均逐条读回正确。
196 三个孩子真实共同执行约 32.6 秒；四个孩子的 canonical 状态均为 DONE、错误为空、
没有残留 pending closeout，runner session 均 completed。每个 exact attempt 各有一条完成去重回执，
四条对应 wake 均为 handled；父级随后接续完成，两个原执行宿主 PID 均已退出。
这证明本组正常收口和消费，不等于已经在真实 TUI 注入并验证重复通知、写盘失败或重启恢复。

195／196 原生页面显示最终结果；197 原生页面停在“gateway 请求等待超时”，
但原请求 terminal 为 done／ok，原任务和实际产物均已完成。保留为客户端最终结果展示缺口，
不能用业务成功抵消。TUI198 只通过原生 resume 重连 197 原会话，未发新业务需求，
已显示完整工具历史和最终报告；重连可读不证明原页面能自动回补。
原始 ANSI 没有逐块时间戳，文件 mtime 不能作为精确超时时点，先后时序仍由 TUI 工作线定位。

请求耗时约 32 分钟主要包含测试机停顿；有用工作时长不足以证明 15—30 分钟长任务验收。
模型菜单及真实响应已核对，实际请求端点的单请求日志证明仍须与菜单证据分开。
本组覆盖发布包 `c9042f5f2`，不覆盖尚未部署的准入依赖收窄候选。
递归、等待时插话、失败／取消、重复通知、恢复和有效长任务仍待后续矩阵；
测试机内存和交换区紧张单列环境因素，没有进行真实断网注入。原日志、身份和产物留仓库外。

## TUI 阅读与插话并行修复（已发布 main，未双机切换）

这条线独立于十步 Goal 的第 7 步子代理结果链；它修复运行中插话的历史顺序、
后台 native 回复重复显示，以及普通／详细／原文视图的阅读锚点和连续滚动。
15 个相关测试文件 **381 passed**；Ruff、导入边界、doc sync、strict code-size、
diff、clean-package 和候选 wheel 的发布边界检查通过。全仓 pytest 与线上 CI
没有作为本轮验收来源。

真实验收在用户授权的独立测试环境，由原生 tmux 测试会话发起，
同机只用一个 Gateway。实际模型为官方 `MiniMax-M2.7`，请求端点
`api.minimax.cn/anthropic/v1`；密钥及原始运行身份保留在仓库外。
真实会话名和 `tmux attach -t <会话名>` 所需参数见私有证据账。
完整交互矩阵所用 wheel SHA-256 为
`218fe2bc4b52875b3cfbcd240a14ec0a9bb32d3f3f3b74e35b811e1ecda94d`；
最终交接 wheel SHA-256 为
`b2bd4258b66574e9d3d756b73c16939351296d6e84e4d7de1c912a2db649d330`。
后者只同步模块注释，运行实现相同；切换后另由新原生 TUI 的官方模型完成
`37 × 19 = 703`，并补验展开和滚动。34 段双向完整矩阵属于前一个功能相同的包，
不能写成最终包逐项重跑。

运行中插话提交时，原 claim 仍为 running；原生 TUI 顺序为原工具调用、
用户补充、后续思考、收到补充后的回答，重连后用户输入仅一份。
后台命令结束后自动追加的 native 回复也按精确回合身份只显示一次。
34 段长记录向下和向上分别采样 159 次，段号没有反向跳动；
普通／详细／原文展开收起、单次上下键、鼠标 SGR 滚轮、84↔120 列调整均保留当前消息位置。
独立短 Goal 在真实等待 45 秒后读回第 600 条并置为 complete，模型正确解释无需再次发送用户消息。
原始终端字节、截图和精确会话／请求 ID 留仓库外，候选失败记录未计为最终通过。

本轮只验证 root TUI 与已有历史恢复；没有覆盖全部子／孙代理、IM、长达 100 小时的耐久，
也不证明所有重复正文问题均已解决。代码已在原 checkout 单独本地提交 `c297c52f3`，
已随本轮快进推送远端 main，并随第 7 步组合包切换测试机默认 Gateway；本机尚未切换；
此候选的真实验收不能冒充第 7 步新版子代理 TUI 验收。
文件归属、逐项证据与未测边界见 [TUI 阅读交接](docs/tasks/TUI_READING_HANDOFF.md)。

## 第 6 步插件故障与并发长任务阶段记录（框架验收收口）

当前运行包仍为第 5 步已发布的同一 wheel，尚无第 6 步产品代码改动。故障插件是仓库外构造的标准本地包，原包、合成输入和原始运行账均保存在私有测试目录，不随仓库发布。该包独立验证了 `probe`、完整 `isError`、受控阻塞和非零退出；停用与恢复没有按插件 ID 加核心特判。受影响的停用竞态、发布、调用及宿主命令 focused tests 已通过；本段真实 TUI 结论单列，不以组件测试代替。

本机 TUI179—185、187—194 共用原 Gateway，均在各自会话选择并核对官方 `MiniMax-M2.7`，请求端点为 `api.minimax.cn/anthropic/v1`。测试机 TUI174—178、186 也共用该机原 Gateway，官方端点为 `api.minimaxi.com/anthropic`。查看本机管理、插件长任务、核心长任务、故障、逐页长任务、源码审阅、审批及卸载后基线，分别用 `tmux attach -t release-0920-step6-local-manage`、`release-0920-step6-local-plugin-long`、`release-0920-step6-local-core-long`、`release-0920-step6-local-fault`、`release-0920-step6-local-ledger-long`、`release-0920-step6-local-core-review-long`、`release-0920-step6-local-approval`、`release-0920-step6-local-post-clean`。188—194 的新会话后缀依次为 `local-triple-plugin`、`local-triple-core`、`local-final-plugin`、`local-final-core`、`local-random600-plugin`、`local-random600-core`、`local-random600-core-retry`，均加在 `release-0920-step6-` 后；测试机用 `ssh <测试机> 'tmux attach -t release-0920-step6-remote-manage'` 查看管理 TUI，原故障 TUI 已正常退出，重连 TUI 用 `release-0920-step6-remote-resume`，其它后缀为 `remote-core`、`remote-plugin-long`、`remote-core-long`。原生 TUI 发起业务或管理；测试者只准备合成输入、控制明确审批／中断并只读核对原账和产物。

| TUI | 已核对的原生事实 | 结论与边界 |
| --- | --- | --- |
| 174、175，测试机管理／故障 | 真 TUI 安装、启用、`probe`、`fail`、`crash`、`stall`、停用与卸载。完整错误回执使原业务 FAILED，独立 `probe` 随后成功；审批 Esc 的原事件 `CANCELLED` 且 `handler_executed=false`、无业务工具操作。获批后异常退出留下原操作 UNKNOWN，新调用成功但不回写旧 UNKNOWN。 | 退出、完整错误、审批取消与未知结果边界部分通过；再启用曾在 `preparation_launch` 失败，测试机当时可用内存极低且交换区已满，原因尚未确定，不记为产品通过。最终故障包已停用卸载。 |
| 176、177，测试机并发业务 | 首次长请求在实际工具轮前因测试者误删正在使用的派生检索索引而碰到缺表；重建 schema 后，新 TUI 可真实调用工具。177 后续被精确中断当前回合，未停 Gateway。 | 测试准备失误；旧索引内容未恢复。两条原失败及中断均保留，不计长任务通过。 |
| 178，测试机核心 | 16 批共 4,800 行输入，原任务约 221 秒并完成；明细 4,800 行的数值正确。 | 质量报告把跨三个文件的重复编号误写成同一文件三次，并把退款排除口径与实际汇总写得不一致；属于模型产物内容失败，不能以任务 done 代替交付通过。 |
| 179、180、181，本机三路并发 | 管理 TUI 安装／启用故障包时，180 通过插件读 24 份文件，26 条操作中 24 次 `show` 成功，2,880 行、金额 273,909、72 条 review 与输入一致；181 用核心工具生成 4,800 行明细、汇总、代码与测试，原任务约 310 秒，独立逐行语义核对 0 错误，区域／产品计数、退款和金额均一致。 | 首轮插件任务约 287 秒，核心任务约 310 秒；管理可并行响应，核心产物通过当前数据核验，但两个任务都不足 15 分钟，不能计为连续长任务。 |
| 179、182，本机故障与撤销 | `stall` 已进入原业务操作 EXECUTING；管理 TUI 在同一调用约 17 秒后停用并释放原激活。原业务结算 UNKNOWN／`effect_outcome_unknown:TOOL_EXECUTION_FAILED`；该停用原回执 `cleanup_confirmed=true`、`released=true`，同代 3 个准备 session 均 `exited`、2 个 activation session 均 `killed` 且退出确认。再次启用并从刷新目录调用 `probe` 成功，旧 UNKNOWN 保持。重复停用也返回已释放。另一次 `stall` 超时在停用前发生，旧操作 UNKNOWN／`TOOL_TIMEOUT`。 | 执行中撤销与超时后停用是两份不同证据；管理不被阻塞调用锁死，旧结果不复活且准确 session 的清理有原账证明。审批等待期间停用、断连与受控清理失败还要继续核对。 |
| 180，本机第二轮插件任务 | 对 80 份合成日文件的一次中文请求约 119 秒完成；原账只含 1 次 `tree`、6 次 `show`，其余数据由模型改走核心 `run_command`。汇总数值与输入一致。 | 明确要求逐份经插件读取，模型没有遵守；这是本轮模型履约失败，不能算插件长任务或 80 份插件覆盖通过。 |
| 181，本机第二轮核心任务 | 80 份合成日文件共 1,920 行，原任务约 207 秒完成；独立按输入逐行比对明细的来源、字段与金额，语义错误为 0；80 天和 4 类汇总与输入一致。 | 核心多工具任务的当前数据交付通过；时长不足 15 分钟，仍不计连续长任务。 |
| 183，本机插件逐页长任务 | 新会话一次中文需求；原任务 `taskrun-1790142148-31de9a66` 完成于 1,221.5 秒，`ledger_page` 1—80 页恰好各成功一次，另有 1 次目录调用；主代理与 3 个孩子 done、2 个孩子 cancelled。`per_page.csv` 80 页的行数、原始金额和反冲金额逐页与合成源数据一致。 | 原生插件长任务的时长、页覆盖和运行框架成立；交付内容失败：分类行 `gross` 合计 343,307、`net` 合计 333,816，表内 `TOTAL gross` 却是 352,798、`TOTAL net` 343,307，报告宣称无异常。原中间账还保留派工无效、参数缺失及取消引起的失败／UNKNOWN；不把最终 done 洗成全链路零错误。 |
| 184，本机核心源码审阅长任务 | 新会话一次中文需求；原任务 `taskrun-1790142450-1a62b2d7` 完成于 983.2 秒，8 个子任务均 done，四类主产物及 8 份链路子报告实际落盘；162 个已跟踪源码快照文件逐一核对未被修改。 | 核心长任务、八路子代理及产物持久化成立；报告内容失败：目录分组写成 20／58／84，实际是 31／51／80；`findings.md` 称环境准备无超时，源码已有 120 秒预算和明确 deadline；又称 preparation 错误可能仍使 `cleanup_confirmed=true`，与 `not report["errors"]` 条件相反。该报告不能作为重构依据。 |
| 179、185，本机清理失败与恢复 | 私有故障包 v0.3 的 `arm_cleanup` 在本激活安装环境内创建无权限测试目录，原业务成功。TUI179 首次停用的原操作 `0009702ae476466dbe5790c74a8b23fe` 为 UNKNOWN／`PLUGIN_CLEANUP_UNCONFIRMED`，安装激活保持 revoked；旧 TUI185 的 `probe` 被旧目录版本拒绝。测试者只恢复该夹具目录的权限，未替产品删除环境或改原账；第二次停用原操作 `5eac1255d68e4b43a2e8c97c2b985bcc` 成功，`cleanup_confirmed=true`、`released=true`、错误列表为空，环境才消失，旧 UNKNOWN 保留。随后新代启用的 `probe` 成功，最后经真实 TUI 停用并卸载。 | 仅证明安装环境回收失败及可恢复收尾，不冒充进程退出失败；未确认阶段没有把 revoked 当 released，旧代没有复活。 |
| 174、175，测试机审批等待撤销 | 在默认确认模式下，TUI175 原生 `/plugins@fault-lab crash` 出现“允许一次／拒绝”弹窗；审批等待期间 TUI174 停用同一激活成功，随后 TUI175 才选择允许一次。原任务 `taskrun-1790145592-b2d90dc8` 最终 failed，业务工具操作 FAILED／`effect_outcome=not_started`，输出为 MCP 连接已关闭或被替换；这里 `handler_executed=true` 仅指宿主代理 handler 被进入，不证明插件实际 `tools/call` 发送，亦没有发生预设的插件 crash。TUI174 随后卸载成功。TUI175 打开权限菜单后关闭菜单的 Esc 还曾中断一个既有模型轮，此为测试控制误操作，不能计作产品审批取消。 | 审批后复查阻止了已撤销代次的插件调用；不能写成“零宿主操作”，也不能把弹窗关闭时的中断当成计划内验收。该次 TUI 提示连接退出未确认，须将连接清理提示与安装资源释放分开核对。 |
| 175→186，测试机原会话断连重连 | TUI175 通过 `/exit` 正常退出客户端；TUI186 用原 session ID 在新 tmux `release-0920-step6-remote-resume` 恢复，同一 Gateway 未重启。原生 `/plugins list` 仅见 `workspace-peek`，随后 `/plugins@fault-lab crash` 在目录层回复当前无此插件，并给出查询编号。 | 已卸载包没有因会话重连重新进入目录；此处只证明 TUI 命令目录拒绝，不从界面文案推断历史原账改变。 |
| 187，本机卸载后核心基线 | 新工作区、新原生 TUI 在本会话模型菜单选中官方 `MiniMax-M2.7`；`/plugins list` 只显示 `workspace-peek`。原任务 `taskrun-1790147053-1e12bb93`、主 AgentRun 均 done，4 条工具操作均 SUCCEEDED。普通中文需求由核心工具生成 12 行平方表、实际读回并写报告；测试者独立核对每行 n²、行数及总和 650，均正确。双机原 Gateway PID 保持，测试机已卸载故障包的受管进程记录无该插件归属。 | 卸载后新会话的核心写入、读取和模型链路可用；这是一条短基线，不抵充长任务，也不证明前两份模型内容失败已修复。 |
| 179、188、189，本机三路真实重叠 | 两条新任务同在 00:27:14 建立并进入工具轮；188 用已启用的 `fault-lab`，189 以核心工具和子代理审阅独立源码快照。两者未结束期间，179 的独立 `control-lab` 安装、启用、`probe`、停用、卸载原工具操作分别在 00:27:56、00:28:28—33、00:29:08、00:29:22—23、00:29:41 完成，五条均 SUCCEEDED。控制包是仓库外独立校验的标准本地包，只管理自己的激活；两条业务任务没有因其装卸被停止。 | 原持久时间证明三路并发的装卸可响应；这段管理窗口约 2 分钟，不冒充三路都持续 15 分钟。此前 183／184 的两条长任务重叠约 15 分钟，另行证明长负载并发。 |
| 188，本机插件逐页新任务失败 | 官方模型新 TUI，一次中文需求；原任务 `taskrun-1790148434-864ed158`。80 次 `ledger_page` 原操作全部 SUCCEEDED，独立从每条工具输出解析，1—80 页各一次、24 行／页、所有字段与测试源 0 错，真实原金额 352,798、反冲金额 9,491、净额 343,307。模型随后试图把大量数据展开成单次 Bash 参数，界面报模型输出长度限制；原 attempt 已结束但 TaskRun 保持 created，原事件为 `runtime_status=unfinished`／`MODEL_RESPONSE_TRUNCATED`／`runtime_source=model_provider`，工作区无所求产物。 | 不能计任务完成或长任务通过。80 页读取链本身正确，模型中途打印的原金额 744,522 与原工具数据不符；供应商截断的 Bash 参数没有执行。当前轮内截断续写仅适用于进入最终答复裁决的路径，供应商级未闭合工具参数在该路径之前收口；此通用模型／工具循环缺口列入第 8 步，不按插件 ID 或本次数据样例特判。 |
| 189，本机核心多子代理长任务 | 原任务 `taskrun-1790148434-ce125910` done，运行 979.2 秒；主代理和 7 个 researcher 子代理均 done，42 条工具操作中 41 条 SUCCEEDED，1 条路径错误 `run_command` FAILED 后模型自行修正。162 个源 Python 文件的快照哈希未改变；`file_roles.csv` 162 行唯一、`call_graph.json` 162 节点，两个文件集合均与源码快照完全相同，7 份链报告及主报告实际落盘。 | 核心 16.3 分钟持续工作、子代理持久收口和产物覆盖成立；用户需求中的“至少 8 个子代理”只完成 7 个，模型总结仍称八链。`findings.md` 有 18 条表项，最终回复却称 1 高／7 中／5 低，仅合计 13；风险判断未逐条独立证实，不能直接当重构依据。框架成功与模型履约／报告内容失败分开记录。 |
| 179、190、191，再次三路重叠 | 原插件任务与核心任务同在 00:56:43 建立；管理 179 在它们运行中安装、启用、`probe`、停用、卸载独立 `control-lab`，五条原工具操作在 00:57:35—01:00:10 均 SUCCEEDED。190 随后在约 502 秒 done；191 在约 827 秒 done。管理过程没有取消两条业务任务，同一 Gateway 未重启。 | 原装卸并发通过；190／191 分别不足 15 分钟，不能用这段替代 183／184 已有的长任务重叠，也不能把两条最终报告称为质量通过。 |
| 190，插件页任务交付失败 | 原任务 `taskrun-1790150203-23adfd25` done，主代理仅成功调用 1 次 `ledger_catalog`，四个孩子的原账中 **0 次** `ledger_page`。其中一名子代理留下 OPEN 能力申请，工具回执明确要求不要伪造能力结果；主代理仍用子代理生成的确定性推算表交付 80 页报告，宣称逐页读取。独立比对 `per_page.csv`：80 页齐全但 59 页至少一个字段错误，其中 21—40 页的 20 页原金额全错；其它 39 页净额错误。 | 插件分页覆盖为 0，不能计插件任务通过。能力申请和子代理阻塞按原状态保留，主代理的完成宣称与原工具账矛盾；属于通用派工／交付核验缺口，后续第 7、8、10 步分别核对身份、事实与组合，不在核心按插件 ID 修补。 |
| 191，核心审阅近长任务 | 原任务 `taskrun-1790150203-6de8cfe6` done，运行 827 秒，主代理与 4 个孩子均 done；25 条原工具操作中 23 条 SUCCEEDED。`file_roles.csv` 覆盖 162 个源 Python 文件且哈希未变，调用图 2,188 个 AST 节点、11,898 条边与实际 JSON 相符。 | 13.8 分钟低于本步约 15 分钟长任务阈值；产物存在与索引覆盖成立，报告风险判断未逐条独立核实，不能当维护性问题事实源。 |
| 193，本机扩大源码样本派工失败 | 以 321 个已跟踪 Python 文件快照审阅，派工前发生供应商级 `MODEL_TOOL_ARGUMENTS_INVALID`；该工具调用未执行，原 TaskRun 留 created。 | 不计核心任务完成或长任务通过；新 TUI194 独立重做较小的 162 文件源码快照。 |
| 192，本机 600 页插件长任务 | 仓库外标准测试包 v0.4 独立验证 600 页、每页 24 条不可由页号推算的合成记录；原始包 SHA-256 为 `847aef3563983a0552b159c436a70aac9850a7044a6033a3a9a22360a40c45be`，仅私有测试目录保存。一次中文需求的原任务 `taskrun-1790151726-b6fdd70b` 为 done，持续 3,053.6 秒；主代理 done。600 次 `ledger_page` 原操作全为 SUCCEEDED，页 1—600 恰好各一次，24 行／页的原返回与包内合成源逐字节相同。24 个批文件、600 行 `per_page.csv`、分类汇总和报告均实际落盘。 | 插件执行、长任务持续、Compact 五次后的分页覆盖与持久产物链路成立；**模型内容交付失败**：600 页中 306 页的最终逐页金额不符，另有 50 页反冲额不符。实际原金额／反冲／净额为 3,702,951／103,431／3,599,520，交付为 3,225,970／88,815／3,137,155。批 1—9 各 50 页，批 10—24 各 10 页，报告却称每批 25 页、已读回核验；内部行数和页覆盖正确不能替代源数据准确性。测试者未补文件或改原结果。 |
| 179、192、194，本机长任务和管理同窗口 | 192 与 194 都在运行时，179 对独立 `control-lab` 实际执行 `probe`、停用、卸载；安装和启用则发生在 192／193 活动时。停用与卸载紧接提交触发一次 `PLUGIN_CATALOG_STALE`，随后停用原请求成功；179 重新查看管理帮助和安装目录后卸载，原回执成功。两条长任务自然完成后，179 也按刷新后的目录停用并卸载 `fault-lab` v0.4，最终原生目录只剩 `workspace-peek`。本机和测试机 Gateway 均保持原进程。 | 管理目录版本拒绝按原协议处理，不将首次过期请求算成功；管理未中断业务，两个受控测试插件都已卸载。192／194 各自超过 15 分钟且有实际工作，管理命令在两任务同时运行的窗口内执行；不声称全部管理操作与两条任务持续重叠 15 分钟。 |
| 194，本机核心长任务自然结束 | 新 TUI 原任务 `taskrun-1790152078-d95d6029` 为 done，持续 973.9 秒；主代理和 2 个 worker 均 done。162 个 Python 源文件快照与当前源码哈希逐一相同，`file_roles.csv` 有 162 个唯一源路径，`call_graph.json` 有 162 个文件节点，二者路径集合均与快照一致；四份主产物实际存在。 | 核心约 16.2 分钟持续工作、子代理收口和文件覆盖成立。报告的架构判断未逐条独立验证，不能直接作为重构事实。 |

测试机清理已确认的旧资料后，根分区非保留空间约 1.3 GiB，但可用内存不足 0.1 GiB，交换区满；不把 `preparation_launch` 的相关性直接写成因果。清理时误删当前工作区的派生检索索引，旧内容丢失；重建 schema 只证明后续新调用可继续，不证明历史已恢复。该失误独立于插件产品验收。TUI183／184 的 20.4／16.4 分钟任务、TUI192／194 的 50.9／16.2 分钟任务及管理重叠形成第 6 步长任务与并发框架证据；内容交付失败分别保留，不把它们当作质量验收通过。审批等待撤销、重连、可恢复清理失败及卸载后核心基线已补证；测试机原生业务连接退出未确认的提示与安装资源释放分开记录，旧结果仍不改写。第 6 步框架验收收口；本步未改产品代码，通用交付核验和模型／工具循环问题移交第 7、8 步对应合同，不能写成已修复。

## 第 5 步使用卡发布与原生 TUI 阶段验收

`689e83e85` 已推送 main；发布 wheel 的 SHA-256 为 `868f3882c23ca317abcfeef835c4fc5694c67a47c3bdf83db76b01c5b0b48d1f`。本机与测试机从同一包安装，各 1,267 个包文件逐项匹配，默认入口与每机唯一 Gateway 同版；切换前核对无活动任务并保留旧运行环境及回滚证据。测试机非保留磁盘可用空间为零，后续重负载优先本机。线上 CI 未作为验收来源。

新版原生 TUI168—170 在测试机共用一个 Gateway，TUI171 在本机另一台唯一 Gateway；171 和 169 分别退出客户端后按原会话恢复为 172 和 173，Gateway PID 均未变化。原菜单选择官方 `MiniMax-M2.7`，恢复后会话模型保持相同；测试机端点为 `api.minimaxi.com/anthropic`，本机为 `api.minimax.cn/anthropic/v1`。源会话、原操作、审批和产物的详细记录留在仓库外；下表只列已核对的事实，不把短任务计作长任务。

| TUI／tmux 会话 | 实际操作与读回 | 当前结论 |
| --- | --- | --- |
| 168／`release-0920-step5-remote-manage` | 原生 `/help` 更正显式入口；安装后的 `/plugins list` 展示简介，`/plugins info` 与启用回执展示相同来源的动作、两条中文示例和设置键名；随后原生停用、重新启用。 | 原管理账的安装、启用、停用、再启用四条 TaskRun／AgentRun 均 done，对应四条工具操作均 SUCCEEDED。 |
| 169／`release-0920-step5-remote-explicit` | 带中文及空格的路径以显式命令读三页，范围 0—31、31—63、63—95，三次原业务操作均 SUCCEEDED，合并文本与 95 字节源文件一致；每页经原生 TUI 明确审批。管理端停用后，旧目录版本提交被拒且没有新增 TaskRun／操作；Tab 只保留帮助候选，重新启用并刷新后动作候选恢复。随后四次原生审批读完 13,760 字节长文件，区间 0—4,094、4,094—8,188、8,188—12,282、12,282—13,760，四条原操作均 SUCCEEDED，拼接全文与源文件一致。 | 显式参数、连续分页、审批、旧候选拒绝、目录刷新和长输出通过。Tab 后原任务／工具操作数不变；无效选项仅显示命令错误，未转成聊天或 Shell。详细记录模式下全选复制把 tmux 缓冲从测试哨兵值替换为真实记录。 |
| 170／`release-0920-step5-remote-chinese` | 一次普通中文需求，模型自行调用 `workspace-peek show` 并通过 TUI 审批，再调用 `write_file`；原任务和主代理均 done，两项工具操作均 SUCCEEDED，116 字节结果包含源文件的两项安排。 | 普通中文到插件及内置写入的短任务通过。 |
| 171／`release-0920-step5-local-smoke` | 一次普通中文需求，模型调用 `write_file` 创建两行文件；另一次四行中文需求经终端 bracketed paste 输入，TUI 显示折叠占位符，模型写出的四行文件与粘贴正文逐行一致。两次原任务和主代理均 done，两条 `write_file` 操作均 SUCCEEDED。 | 本机新版 Gateway、真实模型／内置工具链及多行粘贴展开通过。 |
| 172／`release-0920-step5-local-resume` | 171 退出客户端后按同一会话恢复，原两次任务历史、模型选择和输入框可见；缩小窗口后 PageUp／PageDown 改变并恢复可见历史。 | 重连与滚动通过，Gateway 未重启。 |
| 173／`release-0920-step5-remote-resume` | 169 退出客户端后按同一会话恢复，`/plugins list` 仍显示启用插件；再次显式读取中文空格路径，经原生审批，新增的原 HostCommand／工具操作均成功，首段字节与源文件一致。 | 插件目录和实际调用跨 TUI 客户端重连通过，Gateway 未重启。 |

第 5 步的框架必测项已收口，未发现本片新增的框架失败。旧 TUI164 的模型文本失真继续归第 8 步通用交付核验，不由本片短任务覆盖；连续长任务、多子代理和受控故障属于第 6—10 步。测试机 TUI169、本机 TUI171 已正常退出并分别恢复为 173、172；查看仍运行的测试机 TUI 用 `ssh <测试机> 'tmux attach -t <上表会话名>'`，本机用 `tmux attach -t release-0920-step5-local-resume`。测试机磁盘边界仍未消除，后续重负载优先本机。

## 第 5 步使用卡与目录同源的本地验证

解决问题：旧版 TUI163 的 `/plugins info` 只有简介，顶层 `/help` 误说显式插件入口未开放；启用成功后也没有可直接使用的说明。当前片只修改公开展示投影，不改变安装表、授权或执行链。

本地源码已让列表展示包简介，详情和成功启用回执共用从当前包声明、动作及设置 schema 生成的使用卡；旧启用请求只有在激活代次仍相同时才附卡。公共动作用法由同一参数声明生成，顶层 `/help` 文案已纠正。设置仅展示公开键名，不读取或展示私有配置值。既有目录 v3、客户端 revision、Tab 刷新和提交链未新增状态或轮询。

`test_plugin_commands.py`、`test_plugin_command_catalog.py`、`test_plugin_command_client.py`、`test_plugin_management.py`、`test_plugin_configure_management.py`、`test_plugin_removal.py`、`test_plugin_removal_store.py`、`test_gateway_plugin_commands.py`、`test_gateway_plugin_management.py`、`test_tui_input.py`、`test_workspace_peek_package.py` 共 300 项通过。覆盖使用卡与帮助用法一致、无显式动作时不虚构 slash 入口、旧启用回执不宣传新代次、列表简介和原请求查询位置。发布前本地严格 gate 的全目录 Ruff、文档同步、严格尺寸、diff 和 clean-package 均通过；尺寸报告仅由检查脚本生成，不随本片提交。此处只记录源码验收；发布后的新版原生 TUI 证据见上节。

## 第 4 步三项修复发布后的原生 TUI 验收

解决问题：旧版实际验收中，完整插件错误被记为 UNKNOWN、目录第三页碰到连接释放窗口、重复启用被空资源域挡住；必须由安装版原生 TUI 证明修复，而非把定向回归当作交付。

`fb3857e46` 已推送 main；同一 wheel 摘要为 `56a7ae15385554ecdd70f952c030485e00a41d7e5cf74a6b2c4b37655d343baf`，双机各 1,267 个安装文件核对一致、各一台 Gateway。五个新 TUI 均从新版默认入口打开并在原菜单选择官方 `MiniMax-M2.7`；本机来源为 `api.minimax.cn/anthropic/v1`，测试机来源为 `api.minimaxi.com/anthropic`，两路核心任务均有真实模型调用。未改日常默认模型。原始会话、请求、工具账与截图留在仓库外。

| TUI | 本轮实际操作 | 原生证据与结论 |
| --- | --- | --- |
| 163，本机管理 | 已启用时重复 enable；链接路径 show 失败后再次 show 普通文件 | 重复启用原操作 SUCCEEDED／unchanged、激活未更换；链接原操作 FAILED／UNSAFE_PATH、原锁为零；后续 show SUCCEEDED，首 64 字节与输入一致。旧 158 UNKNOWN 不回写。 |
| 164，本机中文业务 | 一次普通中文需求，自行 tree／show 并写报告 | tree、三次 show 和 write_file 原操作均 SUCCEEDED；实际产物把源文 🙂 写成 😂，五处 CRLF 也变为 LF，却声称完整。框架调用通过，模型内容交付失败，不能计为文本保真通过。 |
| 165，本机核心 | 内置工具生成与读回 80 行整数平方 | 80 行和两列汇总正确，任务终态完成；属于短任务。 |
| 166，测试机管理 | 真实审批逐页目录、拒绝 show、坏包／配置、重新配置启用、显式 show、停用卸载，再从空表重新装卸 | 目录四页范围 0—2、2—4、4—6、6—7 均 SUCCEEDED，七项不重复；拒绝 show 的原任务 failed、无工具操作；坏 ZIP 与坏配置均 FAILED／TOOL_INVALID_ARGUMENTS；正常配置、再启用及获批 show 均成功。停用／卸载后安装表为空，五个 session 确认退出／清理，无未解决 PID；卸载后显式调用未登记原任务／工具操作。随后有效包安装→配置→启用→获批 show→停用→卸载在同一新版 TUI 均成功，安装表再次为空。 |
| 167，测试机核心 | 内置 60 行任务；166 卸载后再做 40 行任务 | 两次原任务完成，60 行与 40 行产物逐行正确；卸载后 40 行两列和为 820／22,140，核心仍可用。均为短任务。 |

新版真实 TUI 已覆盖 4.6d 的重复、失败恢复、连续分页、审批拒绝、坏输入与装卸释放。旧 159 的旧补全撤销和 160 的三孩子并发结果保持独立证据，不被本轮短任务替代。164 的内容错误归后续通用模型交付核验，不能通过重试碰运气宣布成功；第 6—10 步仍需按 Goal 做连续长任务、多级子代理及最终组合验收。远端线上 CI 没有作为本轮验收来源。

测试机重新准备隔离环境后，非保留磁盘可用空间降为 0；root 保留块尚有空间，本轮原启用、读取和清理仍成功。只清除了本任务新 runtime 的生成 `.pyc`，未动源码、配置、历史和回滚包。后续大体量长任务优先本机，测试机重负载前先恢复可用空间；不能把当前短任务通过推广为磁盘压力验收。

## 第 4 步剩余装卸与完整 MCP 失败回执

新版原生 TUI 的新增证据：158 重复安装／停用／卸载的原操作均 SUCCEEDED；重复启用 active 为准备阶段拒绝，尚不计通过。
后者已在原管理链复现：没有环境计划时仍构造空 declared 资源域，先于已有 unchanged 分支抛出 ValueError。
本地修为无计划时不声明资源，回归确认重复启用不改安装记录、不创建新连接，缺失插件返回明确 plugin_missing；旧实现失败，新实现通过。
159 实际 Tab 选择旧目录、暂存输入，158 卸载重装启用后恢复并提交旧输入；界面明确拒绝，原 HostCommand 登记和工具操作均不存在，业务任务数量不变。
161 目录前两页按 0—2、2—4 成功，第三页准备阶段 `HOST_COMMAND_PREPARATION_FAILED`，handler 未执行；原参数摘要核对一致，不重发原请求覆盖失败。
第二页 TaskRun 已关闭后第三页提交，而第二页完整连接清理证据在第三页拒绝后约 60ms 才落盘；原日志只含 ValueError 类型。
原资源门在该中间状态会拒绝新连接，证据高度支持清理窗口判断，不能声称保存了现场异常正文。UI/HTTP 最终回包并未提前；测试观察原任务账后提前发送了下一页。
本地已将本次连接释放纳入原 HostCommand 执行区间，业务 operation 保留确定结果，资源释放结束前运行不提前终态。
两个 Event 固定窗口的成功／审批拒绝用例旧实现均失败，修后通过；查询和重复提交不重开连接、不代替原执行者释放。
查询的中文文案同步保持“连接收尾尚未确认”，不被“调用已完成”覆盖；另验准备失败先释放本次资源再记录拒绝，重送不再次释放。
158 链接读取的原操作为 UNKNOWN、原因 `effect_outcome_unknown:TOOL_EXECUTION_FAILED`；原账没有保存正文，不能声称现场路径拒绝已经通过。
输入文件实际读回 180 字节、5 处 CRLF、无字面 `\\r\\n`，先前分页逐字节校验口径保持。

沿 MCP 合同、现有代理和操作协调器另行确定性复现：完整合法 `isError=true` 回执未提供失败结局，被改记 UNKNOWN。
本地修复只补完整回执的 `effect_outcome=failed`；不表示没有部分副作用，不回写历史 UNKNOWN，传输及清理异常仍保持未知。
新增测试使用原 ToolExecutor、临时 RuntimeDB 和宿主命令身份，核对失败正文、原结果只读重放、释放逻辑锁后的下一独立操作，
并以超时、断连、非法响应、代理异常和未发送拒绝作对照。修复前 1 项目标失败、5 项边界通过，修复后 6 项通过。
首次测试夹具的摘要格式和审批设置错误已修正，原日志单独保存，未把夹具错误当作产品复现。
MCP 注册／协议／生命周期、托管连接、操作幂等／受管门与宿主重放共 8 个文件 **231 passed，0 failed / 0 errors / 0 skipped**。
本片尚未发布部署，不抵充真实 TUI 复验；原安装版仍为 `d4d540c57`。

三项修复合并后的最终验收：17 个相关文件 **389 passed，0 failed / 0 errors / 0 skipped**，166.425 秒。
9 个本轮 Python 源码／测试文件的修改时间均早于测试开始，最终摘要已留私有证据。Ruff、文档同步、严格尺寸、diff 和 clean-package 均通过，尺寸基线不变。
clean-package 首次只因新增测试尚未纳入 Git 失败，显式加入后通过；首次相关命令含不存在的测试文件，未执行，已更正路径重跑，不计作产品失败。
本次改动未达到追加全仓阈值，未重跑全仓；线上 CI 未作为验收来源。下一交付为发布同包部署后真实 TUI 复验，原失败不覆盖。

## 第 4 步启动观察竞态修复

解决本机实际插件 enable 在准备命令已退出 0 后仍报 `preparation_launch` 的问题。
启动观察先读旧状态、后见 host 退出时，现立即复读同一 session，复用原取消／状态判定；不增加 sleep、命令重试或第二份状态。
最终交接仍在原事务内核对身份、执行权和取消，非零退出码继续交原调用方裁决。
确定性交错的 4 项正向用例在旧源码全部失败；修复后加上取消、撤销、未知、缺失和损坏等边界共 13 项通过。
后台交接、stdio、恢复、原 Store、Shell、插件准备／激活／装卸等 16 个相关文件共 **383 passed，0 failed / 0 errors / 0 skipped**，用时 88.343 秒，受测启动模块摘要不变。
本地严格 gate 已通过，尺寸基线未改。修复提交 `d4d540c57` 已发布 main，同一 wheel 双机安装的 1,267 个包文件均一致，默认入口与每机唯一 Gateway 同版；原 TUI154 失败保留。
Git 传输超时后，通过 GitHub Git data API 创建原对象，逐项核对 tree 与 commit SHA 完全一致，再 `force=false` 更新 main；未查询到该提交线上 CI，线上 CI 未作为验收来源。

新版实际 TUI158—162 均从默认入口创建并在原模型菜单选择官方 MiniMax-M2.7，核对 provider 和端点，只改变测试会话：

| TUI | 所测事实 | 结果边界 |
| --- | --- | --- |
| 158，本机管理 | 原正常插件启用成功；停用并释放后拒绝新调用，再次启用、卸载成功；用户产物哈希不变 | 停用与 160 主任务执行重叠；再次启用及卸载发生于该任务完成后，不能据动作标签声称全部并发 |
| 159，本机中文业务 | 原工具账确认 `tree`／`show` 成功，读到完整原文；模型自行写出结果文件 | 工具保留原表情，模型报告改成另一表情且称原文完整，交付失败；未由测试者修复 |
| 160，本机三子代理 | 三孩子共同执行 30.887 秒；各 3,000 行，合并 9,000 行逐条等于子文件，分组／类别汇总和总额一致 | 原检查脚本失败后由模型自行修正并执行，原回执为退出 0；本轮所测数据与报告通过，未做破坏性数据挑战，不算长任务 |
| 161，测试机管理 | 原停用插件在新版本启用成功，原操作为 SUCCEEDED、激活为 active | 完整远端装卸及目录分页继续验收 |
| 162，测试机核心 | 实际生成并读回 1—100 及平方，100 条数据和 JSON 检查一致，两列和分别为 5,050／338,350 | 普通核心任务通过，不抵充子代理或长任务 |

测试机首次候选安装因磁盘满失败，旧 Gateway 未立即切换；移除确认无进程引用的本次失败安装，仅清理本任务 runtime 的生成 `.pyc`，不删源码、配置、历史或回滚包。
原心跳文件因磁盘满为空；独立核对 HTTP 存活、空队列、全部相关 claim／调度／curator、插件无激活和无 CLAIMED／EXECUTING 操作，SQLite `quick_check` 通过后有序停止旧进程并启动新版。
新心跳恢复正常，空文件隔离回调实际未移动文件，不能声称人工补写了心跳；网络配置不变、未强杀进程。磁盘余量仍低，主要任务放本机；这次环境恢复不计作插件故障恢复验收。

## 第 4 步发布 gate 与双机安装

`6050ab600` 修复后的全仓：**18,623 passed、5 XPASS、35 xfail、21 skipped，0 failed / 0 errors**，
测试用时 1,026.408 秒，2,429 个受测源码/配置文件摘要前后不变。5 个 XPASS 与上一轮数量一致。
本地严格 gate 全部通过，尺寸基线未改；远端 main 已通过 GitHub API 确认同一提交，未查到该提交线上 CI 运行，线上 CI 未作为验收来源。

标准 non-editable wheel 已同包部署双机，各自 1,267 个包文件与 wheel 一致；旧取消文件不存在，新公共取消模块已包含。
每机一个 Gateway，默认入口同版；切换前核对请求、claim、调度与 curator 空闲，配置、环境及除 runtime 外的参数不变。
原 runtime 和私有回滚记录保留。实际 TUI154—157 已创建并经原模型菜单核对官方 MiniMax-M2.7；完整插件验收尚在进行，不能以安装或启动成功代替功能通过。
当前正常包测试机已 active，显式读取先验证一次拒绝，再逐次批准三页调用；原工具账的字节范围为 0—63、63—127、127—180，拼接与原 180 字节文件逐字节一致，末页 EOF。目录分页与完整装卸仍待验收。
本机 enable 原操作为 `preparation_launch`，三个准备进程均退出 0；已独立复现启动观察读到旧状态后遇到 host 退出而未重读终态的竞态，修复尚未实施。
原回执没有底层异常文本，不能声称已保存现场精确交错；原账与纯内存复现共同支持此定位。
本机故障包原启用为 `plugin_endpoint_failed`，卸载清理回执保留 activation 退出码 23，预设失败点已核对；TUI155 尚未发送业务需求。

TUI157 三名孩子各生成 3,000 条有效订单，编号与金额独立读回一致；主代理的合并文件保留两处重复表头，共 9,003 行。
其统一检查程序只读取三份子文件，没有验证交付的合并文件，却报告全部通过。该轮交付失败保留，不由测试者修产物，
也不增加 CSV 专项核心分支；并发执行与交接事实和交付质量分开记录。


本次全仓退出阶段另出现第三方 `lark_oapi.ExpiringCache` 在已关闭事件循环上取消任务的析构警告；
上一轮没有相同退出记录。本次 pytest 的失败/错误计数和退出码均为零，该警告单独保留，不宣称日志零警告，也不修改第三方依赖掩盖它。
原 Git HTTPS 查询曾空响应/超时；仅本次 Git 命令使用协议 v0 和 HTTP/1.1 后成功快进发布，未改全局 Git 或系统网络设置。

## 第 4 步发布前失败修复与当前 gate

首轮累计全仓在 `66435c9ac` 收集 18,684 项：5 项失败、35 项既有 xfail、21 项跳过，源码摘要前后不变。
失败记录保留，候选尚未发布；不能用此前的组件通过抵消此次发布失败。

- CLI 直接引用 tooling 取消令牌触发 1 项边界检查失败（3 个导入点）。唯一原实现迁到 `common/cancellation.py`，
  44 个直接消费者/测试导入同步迁移，删除旧文件；AST 核对逻辑不变，27 个生产消费者仅调整导入与相关说明。
- 静态 slash 测试把现已实现的 enable 当作未实现动作。改验 `enable --help`，保留不写文件、不调用模型或旧控制器的全部断言。
- 生产工具注册装配桩补齐真实 HomePaths 已有的 root，生产入口不添加缺字段兜底。
- 两项原生 prompt_toolkit 管道测试只替换旧 JSON 传输，交互命令实际已走消息流。
  补齐流式替身并校验工作线程、请求编号、服务路径与取消令牌；原 UI 断言和等待时长保持，不修改产品流程。

修复后 50 个相关文件 **1,195 passed、0 failed、0 error、0 skipped**，221.736 秒。
Ruff、导入边界、文档同步、严格尺寸、diff 与包干净度通过，尺寸基线未改。
该修复片随后完整发布结果见上一节；组件通过不代替完整实际 TUI 验收。

## 第 4 步 SDK 与 workspace-peek 实际包开发验证

本片以标准 setuptools 构建 SDK wheel、样本 wheel 和完整安装 ZIP；原包、RECORD 与依赖闭包校验均通过。
SDK 安装到没有宿主包的临时 Python 环境，逐字节核对三个共享源码及 LICENSE/NOTICE。
样本经原 MCP 客户端和原宿主管理/Registry/ToolExecutor 组件运行，不计真实 TUI 或真实模型验收。
最终 12 个相关文件 **254 passed、0 failed、0 error、0 skipped**，101.58 秒；本地 Ruff、文档同步、严格尺寸、diff 与包干净度检查通过。
发布累计范围的全仓测试另行执行，未将上述 focused 结果当作发布完整 gate。

- 4 字节小页读取中文、表情和 CRLF，拼回原字节；空文件有效，非 UTF-8/空字节明确失败。
- 文件和目录游标绑定当前对象及权限上下文；内容或目录变化后旧游标失效，空权限和缺上下文不退回进程 cwd。
- 路径链符号链接、多链接、FIFO、凭据文件及上溯被拒绝；错误后同连接仍可执行正常请求。
- 目录排序分页、深度、隐藏拒绝名称、扫描预算和实际配置均验证；超预算不生成假完整结果或续页游标。
- 三路并发 MCP 请求读取各自工作区；这只是组件并发，不能算三路真实 TUI。
- 实际构建包经原管理入口完成默认停用安装、配置、启用、审批后的显式调用、普通工具调用、停用、再启用、卸载。
  重复安装请求复用原操作，坏配置不提交；旧快照撤销、重新启用换代、卸载保留用户文件及内置读取继续可用均核对。
- 独立进程的坏配置不泄露值；坏 JSON 后正常初始化可继续；依赖未闭合不生成安装包。

开发期间修正了临时 venv 在 macOS 使用复制解释器导致动态库缺失、测试漏启动 MCP 客户端、误把宿主结果包装当作
插件正文等夹具问题。卸载后的内置工具检查改走原 Registry 调用入口：只读工具不属于插件管理操作查询，
不能因管理账内没有该只读操作而报告产品失败。另补显式开发构建依赖，避免依靠本机预装后端。
上述修正没有增加产品 fallback、放宽权限或替被测插件生成业务结果。

发布与部署另行验收：累计 Python 改动超过发布阈值，4.5 需追加全仓 pytest；完整实际多 TUI 仍在 4.6。
原始开发报告和构建产物保存在仓库外；本节不保存机器路径、私有配置或原始日志。

## 第 4 步逐次工作区读取上下文开发验证

本片仅本地合同与组件验证；真实临时 MCP/venv 经原执行器运行，不计产品实际 TUI 或真实模型验收。
最终 19 个相关测试文件 **316 passed、0 failed、0 error、0 skipped**，95.024 秒；受测 2,318 个 Python 文件前后摘要一致。

- 原 Registry 使用宿主的实际 cwd、exact 读取范围和明确外部授权；空交集拒绝全部，单文件授权不放行父目录。
- 两工作区共用固定 MCP 代理仍逐次携带自己的 `_meta`，业务参数不混入 cwd；普通 MCP 即使声明扩展也不收到宿主路径。
- 缺少上下文在发送前拒绝；旧连接必须先明确断开再重连，原代理不追随新连接，结果保留 `not_started`。
- 核心文件工具与插件合同共用路径裁决；覆盖 owner/full、跨 owner、控制目录、危险子项、凭据文件和符号链接。
- 宿主数据根及原 ownerless 外部策略分别冻结，插件环境变化不改原权限；默认危险根规范化后去重，修复系统路径别名导致的协议往返失败。
- 实际临时插件经管理安装、启用后，普通注册工具与显式命令均收到同一宿主上下文；仍沿原审批、激活和连接清理。

初轮默认根往返暴露了重复规范路径问题，已修复；组件夹具另修正 prepare_for_run 和原 close_mcp_clients 调用。
相关回归首轮仅新旧连接测试失败：对仍存活连接调用 reconnect 原本就是幂等复用，不能当成已换代；
修正测试先断开自己的临时 MCP 后再重连，没有修改产品重连语义或系统网络。
SDK 产物、workspace-peek 业务功能与实际多 TUI 装卸仍待验收，组件回显不能代替文件预览功能。

## 第 4 步独立命令交互审批开发验证

本片仅本地实现与组件验证：真实临时 HTTP、原执行器/MCP 和 TUI 控制器联通，不计产品实际 TUI 或模型验收。
最终相关 22 个测试文件 **511 passed、0 failed、0 error、0 skipped**，受测 2,318 个 Python 文件前后摘要一致。

- 每次 Enter 固定编号、审批消费者及取消位；审批与主子任务共用 FIFO，不创建聊天回合或更改其执行身份。
- 同机 Gateway 在原 HTTP 请求线程执行，客户端持续读心跳并独立等待面板；无消费者明确返回需要审批。
- 批准、拒绝、取消分别核对原工具调用次数；重放不再弹窗或执行，结果仍按原编号查询。
- 原服务端首帧固定规范 owner，覆盖关闭按用户隔离时非 main 客户端的审批往返；客户端不能提供审批路径。
- 断连只取消原命令；另一个请求在原连接清理完成后仍可执行，UNKNOWN 与连接清理事实分开。
- 显示前、已显示和 FIFO 排队时取消 TUI 后台 coroutine，均核对本命令等待退出、其它面板与前台回合保留。
- 单帧有界、缺失/异号消息拒绝；组件服务退出后核对心跳及审批线程无残留。

开发中保留并修正了临时夹具的工作目录权限、事件时间和取消令牌复用断言，以及旧输入测试的运输替身。
断连用例须等待原 HTTP 执行区间结束后再测新连接，不能把原业务终态误当成资源已经退出；没有放宽原资源准入。
只读复核发现的规范 owner 地址分叉已修复；既有通用控制器连续复用问题在本片单次审批链不可达，未混入修改。
本地 Ruff、文档同步、严格尺寸、diff 与 clean-package 均通过，尺寸基线未调整。
首次包检查因新增文件尚未纳管阻塞，明确纳管后通过；本片未推送部署，尚未新增实际 TUI，线上 CI 不作为验收来源。

建议下一步：准备首个 workspace-peek 可安装样本，检查并发布同一宿主安装包，再用管理、插件业务和核心任务三路实际 TUI 验收。
主代理负责实施和环境操作，适合子代理并行只读复核；Jev 工作区独立，发布和重启前另行同步。

## 第 4 步显式业务调用与原审批开发验证

本片只完成本地命令服务、原执行器及 MCP 组件接线；TUI/Gateway 的交互审批运输尚未完成，没有新增实际 TUI 或模型调用。
最终相关 17 个文件 **299 passed、0 failed、0 error、0 skipped**，受测 2,245 个 Python 文件前后摘要一致。

- `/plugins@插件ID` 使用公共参数绑定、原 HostCommand/ToolExecutor/MCP；临时 wheel 服务实际拒绝额外宿主参数，并记录真实调用次数。
- 宿主请求 v2 分开保存工具输入与目录/工作根/权限摘要，身份索引保持原算法；v1 严格只读，坏版本、缺字段和同编号换上下文拒绝。
- 明确批准只执行一次；拒绝、取消、无消费者、错误决定及不支持的会话批准都不执行，原请求重放不重新询问或打开服务。
- 审批等待时重复提交返回原 running；另一管理调用完成停用、释放和重新启用后，旧批准仍不能调用任何一代。
- 卸载后业务结果仍按原身份查询和重放；普通用户可读自己的业务请求，管理员装卸结果及其他操作者请求保持隔离。
- 取消本次业务连接时，另一个 Registry 的同代连接和内置工具仍可用；握手中的外部取消事实经原 token 及时结束等待。
- 仅宿主明确的自主模式可免交互批准；连接清理回执丢失不改写已保存的业务成功，也不重跑原 handler。
- 原管理、Gateway、客户端、停用、释放、卸载、旧资源准入及操作 UNKNOWN 路径一并定向回归；没有新增第二套执行或结果账本。

首轮保留两处失败记录：新增测试误查数据库列名，修正查询；普通用户的缺失请求查询曾由原权限拒绝变成 not_found，已恢复原拒绝语义。
只读复核另发现 MCP 准备未绑定命令 token，已复用原 `bind_cancellation_token` 并新增初始化等待中的取消回归。
这些组件通过不算完整插件真实验收；本地相关测试、Ruff、文档同步、严格尺寸、diff 和 clean-package 均通过，尺寸基线未调整。
本片未推送部署，线上 CI 不作为验收来源；没有因本片小切片重复运行无关全仓。

建议下一步：接原 TUI 审批队列和 Gateway StreamApproval，命令使用独立取消寿命；断连只查询原编号，不自动重发。
主代理负责写入与环境控制，可并行只读审阅运输边界；接线及发布检查通过后再做管理、插件、核心多路实际 TUI。

## 决策模型后续验证计划（配置合同以首节为准，其余待执行）

[可选决策模型计划](docs/design/DECISION_MODEL_INTEGRATION.md#11-验证矩阵与完成标准)列出后续合同、假服务、
replay 与少量真实 TUI 顺序，重点是 2/4 秒总期限、零叠加重试、未退出资源有界、故障沿原流程、
迟到结果失效、记忆游标、模型窗口与完整费用。全部是计划，不能计入现有通过数量或替代插件验收。
补充双入口配置验收：用户与 agent 修改同一时间/开关、按点覆盖及恢复继承、版本冲突、在途请求不延长期限、
关闭撤销旧建议，以及 Jev 不可用时仍能修改设置；必须核对读回值与后续实际请求生效值。
本段为最初规划记录；当前配置合同结果看首节，后续真实模型、Gateway 与部署尚未执行。

## 第 4 步卸载与重新安装开发验证

本片为本地开发验证，完整真实多 TUI 装卸尚未验收，未推送部署。最终相关 32 个测试文件
**716 passed、0 failed、0 error、0 skipped**，受测 Python 源码前后摘要一致。

- 原管理与工具链从固定目录快照执行卸载：先停用、确认退出并释放，再 CAS 删除准确安装；原 handler 未退时保留安装与环境。
- 同包重新安装后旧目录失效，原卸载重送只读旧操作并补旧清理，不执行新 handler，也不删新安装引用的包。
- 用户产物、来源包及其它插件保留；缺失目标在同一锁内确认，不由目录扫描推断成功。
- 删除提交前、替换后、不可读状态分别保留未提交、已提交与 UNKNOWN；已读回确定删除的刷盘/解锁异常附存储警告，原请求仍重放确定结果。
- 原管理结果提交失败或损坏不回收包；清理失败只标待收尾，查询和功能开关关闭后的重读均不消费，明确重送可补做。
- 包回收与重新安装共用原插件锁，引用检查及删除之间不能插入新安装；包叶子符号链接和坏安装表明确拒绝。
- 临时真实 wheel/venv/MCP 沿原链启用、读取、卸载；旧快照拒绝，隔离环境与包删除，原 Registry 内置读文件工具继续读取同一用户文件。
- Gateway 开/关认证的可信本机入口、管理员与工具禁用、公开目录 v3 严格读取均有组件验证；这不等于完整登录认证验收。

首轮两处测试写法问题保留：UNKNOWN 的公共查询不投影确定 details；内置只读工具须用原 Registry 执行入口，
不能套用依赖副作用操作账的插件管理测试 helper。修正调用和断言后通过，没有放宽产品 UNKNOWN 或权限边界。
首次尺寸检查因管理选择嵌套过深失败，已分离工具构造与执行器组装，原尺寸基线不变。
本轮未重跑无关全仓；Ruff、文档同步、严格尺寸、diff 与 clean-package 均通过，尺寸基线未调整。
首次 clean-package 只因四个新增源码/测试文件尚未纳入 Git 失败，纳入本次提交范围后通过；线上 CI 不作为验收来源。

建议下一步：接通显式 `/plugins@插件ID` 与原审批运输，再同版部署并开展管理、插件业务、核心任务多路实际 TUI。
主代理负责写入和环境操作；可以按明确模块并行只读审阅，不能把开发组件或源码入口调用算作真实 TUI。

## 第 4 步释放与重新启用开发验证

真实验收边界：使用临时原运行账、安装表、原工具执行器、独立 wheel/venv/MCP 与托管进程组件；没有启动真实产品 TUI 或请求模型。
原 enable 身份由首次管理运行和完整资源声明定位；原执行器退出、资源清理、环境删除、安装 CAS 与结果落账分别核验。

- 原 handler 已取消但仍阻塞时只关闭权限和清理已选资源，不删除环境；其退出后新管理请求可以完成释放。
- 实际临时包沿启用、读取、停用释放、重新启用、再次读取和停用闭环；旧工具快照与原 disable 重送不转投或停掉新代。
- 独立环境删除不沿树内符号链接越界，顶层和父链链接拒绝；删除失败保留原 revoked 计划，后续新请求续收尾。
- 原 executor 出生/退出字段的文本、bool、负数拒绝；取消状态或心跳不代替退出事实。
- 原管理结果未保存、后来损坏或仍 UNKNOWN 时保留资源记录；已成功结果的消费失败不翻转原操作，重送只补消费。
- 引用整批验证后才删除；部分 unlink 失败可幂等续做，同 ID 新身份、旧版本和坏证明均拒绝，其他资源保持。
- session v4 固定保留准备记录到显式消费，真实准备记录不会被普通历史裁剪；普通任务原裁剪不变，旧 v2/v3 原版本读写。

定向范围包含插件包/配置/管理/启用/释放/Registry、MCP 生命周期、host command、原生后台进程、通知、任务/子代理停止及 owner 权限。
首轮暴露旧停用断言仍要求保留 revoked/记录，以及新版通知夹具缺少保留字段，已按新合同修正；未给生产代码补隐式默认。
原完整清理证据测试保留，新增精确消费测试独立成文件。最终 50 个相关文件 **1,043 passed，零失败/错误/跳过**，约 144 秒。
运行前后 2,237 个 Python 文件摘要一致；随后仅同步三处既有模块的版本注释，语法树逐项相同。
Ruff、文档、严格尺寸、diff 与 clean-package 本地严格 gate 通过；尺寸基线未改，新增内容隐私检查未发现候选。
卸载、显式插件业务命令及真实多 TUI 尚待完成；本片未发布部署，线上 CI 不作为验收来源。


## 第 4 步实际启用与新运行工具组合开发验证

本片使用实际临时 wheel、venv/pip、托管 MCP 和原 HostCommand/ToolExecutor，不启动产品 Gateway/TUI 或模型。
启用不再依赖假工具发布激活；这仍是组件开发验证，不代替完整装卸及实际多 TUI 验收。

- 原 enable 同一操作完成 preparing、环境准备、完整目录匹配、候选清理和 active；重放不重复启动。
- 名称集合、说明或 schema 不匹配以及服务启动失败均不能启用；私有配置不进入公开结果和日志环境投影。
- 原 Registry 按可信 owner 惰性接入，共享权限视图各自投影；实际工具读文件、停用和旧快照拒绝沿原执行器验证。
- 插件/工具开关与管理员拒绝不创建环境；真正 scoped Agent 的 owner 注入及构造不启动插件分别验证。
- prepare 与 close 的两处登记交错中，迟到客户端保持关闭；持久未知资源阻止新连接，拒绝候选清除自己的退出回调。
- 自然退出码、时间和原 child 回执保持；完整 cleanup 原账保存，迟到写入不能擦除或降级。
- 清理证据写入前故障保留 UNKNOWN；已提交 redo 后的安装故障恢复原证明。PID 消失本身不证明整树退出。
- 旧连接选择及请求/写队列拒绝均记录 not_started，真正发出后的丢响应保留 unknown；原执行器不再留下虚假未知操作。

初测修正了夹具旧属性、公开结果/持久正文位置及快照 hash 绑定；没有放宽产品权限。
只读复核发现迟到登记与关闭交错，以及自然终态遗漏完整清理证明，均在原连接/资源边界修复。
故障测试原先要求保存失败后停用必定成功，已改为按原终态证据裁决；另用确定性原记录验证 UNKNOWN 保留和 redo 恢复。
首次 47 文件回归只有一处旧 MCP 注册夹具缺少关闭状态，已按真实初始化字段补齐，未给生产逻辑增加兼容回退。
最终同组 **1,018 passed，零失败/错误/跳过**，约 132 秒；运行前后 2,230 个 agent_py_agent Python 文件摘要一致。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 和 clean-package；尺寸基线不变，新增内容隐私检查通过。
释放/消费、重新启用、卸载及显式插件业务命令仍待完成；本片不推送部署、不新增实际 TUI，线上 CI 不作为验收来源。

## 第 4 步管理停用与双归属清理开发验证

真实验收边界：本片使用临时原 RuntimeDB、HostCommand/ToolExecutor 和托管 OS/MCP 组件。
假启用工具只提供精确原操作及激活夹具，不证明实际包启用、完整工具目录匹配或真实 TUI/模型验收。

- 管理 disable 先撤销原激活，按持久 operation 引用反查原首次准备运行，并核验完整资源声明；关闭准备权限后分别冻结两类资源。
- 准备中/已发布、原 handler 在途/已结束、原请求查询重放及权限/目录拒绝均通过组件验证。
- 准备资源的显式终态包含模式可核对仍可能存活的 host；普通 task stop 默认行为保持。
- 阻塞 MCP 调用期间管理停用完成；另一插件和独立业务任务继续运行，原资源和激活身份未换绑。
- 独立进程运行原 host，在实际 child 准入前后暂停：管理先提交 revoked，再等原资源锁；前者拒绝创建，后者新 child 被完整冻结清理。
- 原请求链/资源声明损坏不能改停其他运行；清理不确定保留原 UNKNOWN，后续新的明确清理成功不改写旧结果。
- 原资源集合读取覆盖乱序、重复、缺失和附加声明；不排序改写历史 claim。

初测修正两处夹具断言：运行权关闭实际抛 AuthorityContextMissing，关闭连接后应使用预先冻结的资源地址；未放宽产品行为。
新增测试一度导入错误的 holder helper 路径导致收集失败，已改回原 local_storage 定义。
只读复核发现新资源查询误按排序列表比较旧 claim，已改为严格字段检查后的原集合语义，并用真实原 claim 回归。
最终 39 个相关测试文件 **899 passed，零失败/错误/跳过**，约 96 秒；前后 2,223 个 agent_py_agent Python 文件摘要一致。
Ruff、文档同步、严格尺寸与 diff 检查通过，尺寸基线未改；首次 clean-package 仅报告本批新增文件尚未跟踪，纳入 Git 后通过。本地严格 gate 与新增内容隐私检查均通过。
原 revoked 激活和退出记录仍保留；release/consume、完整 enable/re-enable/remove 及实际多 TUI 尚未完成。
本片未推送或部署，未调用真实模型，线上 CI 不作为验收来源。

## 第 4 步托管 MCP 与鲜活准入开发验证

本片仅使用临时原安装表、原 RuntimeDB 和真实隔离进程，不启动产品 Gateway/TUI，也不连接模型。
静态清单与环境是组件夹具；它们不证明完整插件包启用或工具目录匹配，完整管理和多 TUI 仍待完成。

- 可信 owner/root/激活引用在独立进程复查原表，路径错配、缺失、损坏、旧代和撤销均拒绝。
- launcher 预留、启动及交接与 host 创建 child 前共用原资源锁检查；握手/发现允许同代 preparing，业务必须 active。
- 托管二进制管道由唯一文本读取端解码，实际 Unicode/大内容、stderr、原 host/child 精确退出验证。
- 原请求/写队列之后再次准入，撤销代不发帧；旧代理冻结原 transport，不追随重连。
- 单次权限拒绝不关闭共享连接；原 executor 已领取后仍保留未发送事实，writer 已启动的失败继续 UNKNOWN。
- 原目录锁的线程和跨进程等待可取消；取得锁后的 redo/提交不可被等待回调截断。
- 原 ProcessSessionCleanup 和提交异常继续传递；未接管启动的未知结果也阻止重启，不改报未开始。

初轮夹具拦截 Popen 时误拦了出生标识所需 ps，已限于目标 child；结果断言改为原 ToolHandlerOutcome 的 ok/output 字段。
stdio launcher 消失用例首次在 host 提交 child 终态后、host 自身退出中执行收尾，原终止回执保守返回身份未知。
该用例现另外观察 host 自然退出后再做收尾，没有放宽生产清理或把首次 UNKNOWN 改报成功。
只读复核指出未发送事实没有进入原操作账，已通过结构化 effect_outcome 传递，不能按超时/取消文案猜是否执行。
最终 28 个相关测试文件 **681 passed、零失败/错误/跳过**，约 72 秒；前后 2,217 个 agent_py_agent Python 源文件摘要一致。
原执行器用例核对同一 operation 从 EXECUTING 收口 FAILED，结构化结果为 not_started；writer 启动后的故障仍 unknown。
首次严格尺寸检查发现发送函数嵌套过深，已将本次权限异常分类提到协议模块，未改尺寸基线；之后重跑同组 681 项通过。
Ruff、文档同步、严格尺寸、diff 与 clean-package 全通过；新增内容隐私检查同步执行。
本片不发布部署、未新增实际 TUI，线上 CI 不作为验收来源；完整管理启停与多 TUI 仍待完成。

## 第 4 步托管 stdio 与共享资源归属开发验证

原后台启动/host 已有显式 stdio，session v3 区分任务与共享激活，旧 v2 原版本保留；首轮 6 个文件 **182 项通过**。
仅使用临时合同记录和隔离 OS 子进程，不运行真实模型或产品 TUI。

- 大于管道容量的任意字节原样往返，stdout/stderr 分开，协议字节不写任务日志。
- child 关闭输出后即使继续运行，读方仍可观察 EOF；host 释放重复端点，自然退出码保持。
- 交接前取消回收原 child 和未交出管道，交接后 launcher 消失由原 host 清理；不关闭其他任务。
- 两个激活代次与普通任务同时运行；按激活只停止准确代次，按任务不选择共享连接。
- v2 原 redo 恢复、原版本更新及新旧实例复用拒绝；v3 禁止混绑 owner/业务身份或替换激活。
- 普通模型即使知道精确 session 也不能查询或停止共享连接，普通历史裁剪保留其终态证据。

只读复核发现提取 Popen 后，日志上下文关闭失败可能发生在外层取得句柄之前。
已改为上下文只准备端点，外层先保存 child 再关闭日志；真实子进程的关闭故障注入通过，未增加清理旁路。
最终联测原 Shell、任务/子代理停止、环境准备、宿主操作与 MCP，共 20 个文件 **523 passed、零失败/错误/跳过**，约 59 秒。
运行前后 2,276 个 Python 文件摘要一致；旧 v2 和当前 v3 的 PID 复用、未知 host 丢失及清理分支分别覆盖。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与打包检查；尺寸基线未改，新增内容隐私检查通过。
本片未使用线上 CI，没有发布部署或新增实际 TUI。
完整安装表准入、调用及装卸仍待接通，不进入第 5 步；上述组件与回归不是实际多 TUI 验收。

## 第 4 步唯一激活权威开发验证

安装、配置与激活共用原表 v3、原插件锁和原子提交；领域首轮 **103 passed、零失败/错误/跳过**。
测试仅使用临时私有记录、线程和独立 Python 进程，目录摘要为合同夹具，不代表实际插件已启用。

- 同一计划预留、发布、撤销与逐次精确重放；旧快照拒绝、撤销不可恢复、未清理前不能改配置或准备新代。
- 发布和撤销争抢同一版本、两个独立进程竞争预留；一方成功，另一方明确冲突，不丢更新。
- 撤销可在另一线程持有原 owner quota 时完成，配额不足不妨碍关闭已有代次；新资源仍沿原配额准入。
- v1/v2 显式迁移，查询不写盘；v2 原配置及已有 v1 来源保留，坏 v3、错绑回执及错误目录摘要拒绝。
- 各阶段提交前、提交后和读回失败分别保留未提交、已提交与未知，不伪造清理成功。

首轮旧 v1 测试夹具因沿用新表字段失败，改为完整旧字段后通过，未放宽生产解码。
只读复核发现准备/发布的回执操作可与计划不一致，已补严格读回及两个持久反例。
最终联测安装、配置、原操作、环境准备、命令目录及 MCP，共 20 个测试文件 **549 passed、零失败/错误/跳过**。
使用项目现有 Python 3.12 开发环境，约 25 秒；本片尚无真实 TUI、模型调用或部署。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与打包检查；尺寸基线未改，新增内容隐私检查通过。
末次 Ruff 发现移除顶层依赖后的导入间距，修正后语法树未变；此前失败保留。线上 CI 未作为验收来源。
stdio 管道托管、activation 资源归属和完整启停调用链仍待接线；撤销记录不是 OS 清理证明，不进入第 5 步。

## 第 4 步 MCP 连接生命周期开发验证

本片 6 个相关文件 **163 项通过，0 失败、0 错误、0 跳过**。覆盖 MCP 协议、注册、
Computer Use 的 MCP 装配、原进程树清理和现有协议边界回归；均为开发验证，不是真实模型或 TUI 验收。

- 两层排队与在途请求在关闭后退出；旧连接队列、EOF、通知和超时不能操作重连后的新连接。
- 进程创建返回前关闭保留 `launch_pending`；握手完成但尚未发布时关闭，候选不得变为 ready。
- 永久 stop 拒绝 start/reconnect；临时故障保留原退避资格，清理未确认时保留原 transport，禁止创建替代进程。
- 临时真实 MCP 进程验证：自然退出的未回收组长仍能用于清理同组孩子；stdin/stdout/stderr 与读线程退出。
- 同一连接的分页目录才可发布；关闭或换连接后拒绝旧目录，共享权限视图关闭不会恢复原客户端。
- 发现失败且清理抛错时，其他服务和核心准备流程不被中断；异常清理仍保留未知，没有改用全部 stop。
- 原工具发现、完整内容与 Schema、调用超时、取消通知和写入背压回归保持。

只读复核指出并修复：未确认清理仍重连、可用性提前回收组长、管道漏关、排队期限计算窗口和清理异常隔离。
首轮新增测试因假进程缺少 stderr 字段失败，修正夹具后通过；原失败日志保留，未为夹具增加生产兼容分支。
累计未发布 Python 增删接近一万行，追加全仓验证。首次收集因系统 Python 缺少已声明的
hypothesis/pyte 失败，随后使用项目现有完整开发环境，未修改日常运行环境。
首轮全仓运行到 11,852 项时记录 191 项失败；定位到此前权限拆分后，工作片入口仍导入已删除的 core helper。
修复直接调用原 owner_access 权限裁决，不恢复兼容转发；正常、Full Access、子代理继承和退出恢复的
定向验证为 103 passed、2 项既有 xfail。另将两组旧命令夹具接到实际冷管理依赖，并断言非管理员安装的
结构化权限拒绝，相关 171 项通过；模型和旧控制器不调用、原会话不变、静态命令不建目录的检查保留。
原失败和中断记录保留；最终固定源码全仓 **18,265 passed、35 项既有 xfail、21 项 skip、
5 项既有标注 XPASS，0 failed/error**，共 18,326 项，约 828 秒。运行前后 2,275 个 Python 文件摘要一致。
本地严格 gate 通过，尺寸基线未改；新增内容隐私检查通过，线上 CI 未作为验收来源。
打包检查首轮因四个新增文件尚未纳入 Git 失败，将本片文件纳入后通过；未删除源码或降低检查规则。
本片未发布部署、未新增实际 TUI。插件持久激活、静态清单完整匹配、审批后撤销及完整装卸仍未完成，
不能进入第 5 步；边界见 [MCP 连接合同](docs/design/MCP_TRANSPORT_LIFECYCLE.md)。

## 第 4 步环境准备归属开发验证

本片 16 个相关测试文件合计 **420 项通过，0 失败、0 错误、0 跳过**。
覆盖原环境/管理组件、后台启动交接和 ProcessSessionStore、原操作权限及宿主命令重放；临时 OS 进程不等于产品 TUI 验收。

- 固定计划先作为一个 logical scope 写入原 operation，准备前核对包/解释器，运行中核对原 claim、owner/task/run/attempt/tool、epoch 和资源锁。
- 原 ToolExecutor + 临时 RuntimeDB 内实际完成 venv/探测/pip；三个进程使用同一原 attempt，原请求查询及重复提交不再次准备。
- 同步准备复用原托管进程账，不再裸启动；配置、安装状态及激活不因此改变，没有新增完成唤醒。
- 原后台链验证交接后启动方崩溃、host 自行实施截止时间、非法/过期期限；普通长期后台默认语义回归通过。
- 真实临时命令交接后撤销原 attempt，准备器只停止其精确 session，读回 child 退出；取消/清理未知和记录不可读仍保留未知。
- 复核发现并修复“第一次读取 running 后 host 刚完成退出”的竞态，退出后复读同一权威；另修复启动后超时丢失 started 事实。对应交错测试通过。
- 初次撤销组件断言把原 AuthorityContextMissing 错写为 RuntimeConflictError，修正测试期待后通过；生产错误类型和清理路径未因此改动。

本地严格 gate 已通过：相关 focused tests、Ruff、文档同步、严格尺寸（基线不变）、diff 与 clean-package；线上 CI 未作为验收来源。
完整启用命令、MCP 目录发布、精确撤销和装卸尚未接完；本片未发布部署、未重启 Gateway、未调用真实模型或新增实际 TUI。
下一次实际多 TUI 验收仍在第 4 步完整候选上执行；本片不关闭该验收项，不进入第 5 步。

## 第 4 步私有配置开发验证

配置命令沿原 ToolExecutor/ManagedOperationStore、RuntimeDB 和真实文件锁在临时 owner 验证，不启动插件或真实模型。
覆盖完整 JSON/schema 严格校验、64 KiB 预算、私有值不进入工具参数/结果/目录、重复请求、来源删除后的查询、
相同值不改版本、过期目录拒绝、锁内 CAS 竞争、配置与包/回执绑定、写入前/后及结果不可读的提交分类。
v1 只读不迁移，首次真实变更同次提交 v2 及原字节摘要；矛盾的 install 回执携带配置、坏迁移与缺失 v2 字段均拒绝。
HTTP 原认证和明确无认证回环、direct 客户端、单独禁用配置工具、功能关闭后的原查询均沿原入口验证。
源码复核发现 install 回执可搭配非空配置的坏账缺口，已拒绝并补定向回归；正常写链不会产生该组合。
首轮只有共享来源读取搬移后一个旧 monkeypatch 位置失败，已迁到实际公共入口；不归因为产品或模型失败。
扩大相关回归后，16 个 direct TUI 分流用例的旧替身缺少现行 owner/会话依赖；已补真实轻量上下文并增加配置帮助场景，
保持不进入普通聊天、插话、控制和任务队列的原断言。补夹具时一次不存在的路径属性已改为原 owner 解析，不改变生产入口。
本轮没有新增实际 TUI、真实模型或发布部署；全流程多 TUI 验收须等启用、调用、撤销和卸载接通。
最终 21 个相关测试文件共 614 项通过，无失败、错误或跳过；包含原宿主操作、工具 schema、owner 配额、
独立环境组件、HTTP/direct 和 TUI 输入分流。此前定向重跑不另加计数，不以组件或替身输出来声称实际 TUI 验收。
本地 Ruff、文档同步、严格尺寸、diff、clean-package 全部通过，尺寸基线未改；新增内容隐私扫描无命中。
累计未发布 Python 增删为 7,546 行，未达到全仓频率门槛，使用相关定向集；线上 CI 未作为验收来源。

## 第 4 步独立环境开发验证

内部准备器只处理原包快照和宿主操作身份，不发布启用；标准 venv/pip 的真实组件调用只发生在测试临时目录，不是真实产品 TUI 验收。
覆盖 wheel 元数据、Python/平台标签、直接 URL 拒绝、活动依赖/extras/循环依赖闭包、RECORD、目录/成员预算与跨依赖覆盖。
解释器、安装器、生成脚本和文件/目录冲突均在调用 pip 前拒绝；共享 namespace 的不同文件仍可安装。
中文空格路径下两份本地 wheel 的离线安装、headers 与入口脚本读回通过；模块和 `.pth` 陷阱未执行，原安装表保持停用。
同操作不重建候选，失败目录不冒充成功；原配额不足和锁竞争在候选创建前拒绝，旧锁的默认等待行为保留。
取消、超时、出生标识读取失败、通信异常、清理异常以替身验证原身份收尾，退出未知不改报成功。
初轮发现当前 macOS Python 的复制式 venv 子解释器 SIGABRT，采用标准链接式 venv 后组件复验通过；
两项新测试的冷 owner 目录夹具缺失已修正，不将夹具错误当作产品故障或隐去初次失败。
只读复核发现的 Unicode 等价路径别名和安装后 RECORD 扩张问题已修正；单 wheel/跨 wheel 别名均拒绝，
临时 pip 实际安装 200 个入口脚本与签名成员，独立记录预算及完整目标/摘要核对通过。
后续复核仍发现按声明长度估计不足以覆盖短名称；改为按实际允许目标数、路径字节及每行固定开销计算，
增加 4,000 个短入口的真实临时 pip 组件并通过。压缩平台标签在公共库展开前检查组合预算，安装器保留元数据及其大小写别名明确拒绝。
两路独立只读复核各守环境进程/锁和 wheel 内容/布局边界，代码写入由主代理单独负责。
最终 14 个相关文件 319 passed、0 failed/error/skip，含 6 项文档回归；受测 Python 文件摘要与最终源码一致，中间重复执行不累加。
本地严格 gate 与新增内容隐私检查通过；原尺寸基线未改，未发布 Python 增删累计 6,736 行，未达追加全仓阈值。
clean-package 首次因新文件尚未登记而失败；审查并加入 Git 后复验，未放宽包检查规则。线上 CI 未作为本片验收来源。
本片未调用真实模型、未新增实际 TUI、未发布部署；Windows、所有 Python 发行版、产品启用和撤销均未验。
原 `/stop` 的 TUI 138—142 结果保持独立；配置、激活、MCP 和完整多 TUI 装卸仍待完成，不能关闭第 4 步。

## 第 4 步管理执行与只读查询开发验证

本地 HTTP/direct 管理入口复用原认证、owner 路径、线程与工具执行器；临时 owner 上保存真实合成包，默认停用，不导入或运行插件。
覆盖原请求执行、并发重送、执行中查询、来源删除后的回读、普通用户拒绝、工具禁用、开关关闭及原结果仍可查询。
YAML 默认与 dataclass 一致，带引号的 false 经原布尔规范化后实际关闭管理；不只验证配置字段存在。
来源路径拒绝越权、链接、损坏包和缺失文件；一次字节快照不会在来源变化后重读，严格打开不支持时明确失败。
FIFO 替换检查非阻塞打开标记后再执行系统调用，不以可能永久阻塞的测试验证拒绝；普通 portable 文件行为保留。
配额满时不发布包或安装表；静态目录损坏不抹掉原执行结果，网络超时保留请求编号与未知，不自动重放。
隐藏管理工具仅由宿主免除模型可见性检查，模型默认仍拒绝；参数规范化改变摘要在原操作领取前拒绝。
原执行器使用同一 ManagedOperationStore；未知副作用保留 UNKNOWN 与锁，不重进 handler。
开发故障注入分别覆盖读原操作、关闭 AgentRun、关闭 TaskRun；已知工具结果附收尾未确认，显式重送只补原收口。
前置拒绝已落原事件但 TaskRun 关闭失败也可恢复；只读状态查询不初始化/迁移数据库，不激活 pending、不写收口或对账。
先前可见性、禁用策略与收尾遗漏已修正后复验；测试夹具导入错误保留为开发失败，不算产品故障或通过结果。
最终 33 个相关文件共 771 passed、1 skipped、0 failed/error，含 6 项文档回归；中间重复运行不累加。
跳过是原 `TestGatewayHTTPIntegration.test_stop_endpoint` 遇 HTTP 409 主动 skip，不算停止或服务退出验收。
本地严格 gate 全部通过：Ruff、文档同步、strict code-size、diff 和 clean-package；新增内容隐私扫描零命中。
尺寸 hard=0，原基线未改；累计未发布 Python 增删约 5,200 行，未达全仓阈值，没有追加全仓测试。
本片未发布部署、没有新增真实 TUI 或真实模型。完整独立环境、配置验证、激活和撤销尚未接通，不能以这些检查关闭第 4 步。
实际装卸仍按计划用多 TUI 并行核心任务验收；原 TUI 138—142 停止结果保持独立，线上 CI 未作为本片验收来源。

## 未启动取消记录的精确回读开发修复

源码复核发现原 Repository 在取消未启动 `CLAIMED` 占位时覆盖整个结果对象，丢失输入指纹与幂等身份。
已改为保留原领取字段，仅更新规范取消结果、错误字段和时间；未知扩展、持有者及资源声明原样保留。
运行结束、单轮结束及崩溃恢复三个真实事务入口之后，均通过原 ManagedStore 精确读取并重放取消回执。
重复停止不改原回执，其他运行/尝试、已开始、UNKNOWN 和已有终态操作不被改写。
损坏 JSON、未知版本、非对象、重复键、非有限数、过深 JSON 及 SQLite BLOB 保留原存储内容；
执行权可依据关系型未启动事实关闭，但完整与旧三参数查询均明确拒绝坏结果，不编造输入或成功回执。
当前 ManagedStore 的领取直接进入 `EXECUTING`；本片使用合法持久 `CLAIMED` 夹具，不能描述为新 TUI 停止失败复现。
最终 12 个相关文件 360 passed、0 failed/error/skip，含 6 项文档检查；初始失败与中间重复运行不累加计数。
只读复核提出的深层 JSON 与非文本存储边界已补齐；本地严格 gate、隐私检查及发布包检查通过，尺寸基线未改。
累计未发布 Python 增删未达全仓阈值，没有追加全仓测试，线上 CI 未作为验收来源。
本片仅本地源码与开发检查，未发布部署、未运行新的实际 TUI 或真实模型；原 TUI 138—142 停止验收和第 4 步未完成状态保持。

## 第 4 步宿主命令身份与结果回读开发验证

本地源码把显式管理请求绑定到原 RuntimeDB 的独立 pending 运行，普通代理和宿主命令共用同连接创建逻辑。
四个独立 Python 进程并发提交同一请求，只登记一棵运行树；同消息更换输入拒绝，登记事件写入失败整体回滚。
原链断裂、跨 owner/操作者/通道/线程、旧尝试与终态重送均有检查；事件索引依赖原 append-only 保留，不承诺识别手工删库。
假工具经过原 ToolExecutor、ManagedOperationStore 和实际执行区间登记；任务结束后只读原结果，不开新 attempt、不再次进入 handler。
原 owner/task/run/attempt、工具与参数摘要分别核对；失败与取消可重放，截断、错类型、状态矛盾及 UNKNOWN 均拒绝成功投影。
本次 handler_executed=false 与保存的原执行事实分开；原对账引用和嵌套结果不丢失、不被修改。
同进程执行器已退出但 Gateway 进程仍活着时，未决操作使原 attempt 保持 UNKNOWN、原锁保留，重送不重跑。
创建链只读复核确认普通 main 默认 running、子代理 pending、父子共享树及委托/事件顺序保持；没有新增任务调度投影。
审阅发现的重放执行标记和结果校验漏洞，以及既有对账引用回归失败，均已修复并纳入复验；早期测试夹具错误不计作通过。
最终 18 个相关文件共 429 passed、0 failed/error/skip，含 6 项文档检查；另两位代理各自只读审阅不同实现范围，未代跑产品或模型。
本地严格 gate 已通过：Ruff、文档同步、strict code-size、diff 与 clean-package；尺寸基线未改，新增内容隐私扫描零命中。
累计未发布 Python 增删为 3,353 行，未达全仓阈值；文档收尾后再次核对不累加测试计数。
本片没有 HTTP/TUI 管理授权、完整执行收口、环境安装或新实际 TUI；不能据开发检查宣称装卸链已验收。线上 CI 未作为验收来源。
继续接授权、来源读取、原操作执行与结果未知时的查询，再贯通隔离环境和完整多 TUI 装卸；十步 Goal 仍 active。

## 第 4 步安装事实开发验证

本地源码增加 owner 唯一安装表、默认停用记录、版本 CAS 和最后提交回执；包先保存，安装表再原子发布。
公共系统锁沿原后台锁名与顺序，严格 JSON 与受信根文件原语共用；没有管理命令、wheel 安装或插件进程。
合同覆盖冷查询不初始化 owner、跨 owner 拒绝、同请求重放、新请求无改动、旧版本冲突、同版本不同字节冲突、损坏状态保留，
以及提交前/替换后/不可读结果、提交与解锁双故障、已提交请求遇损坏包时原回执保留。
六组独立 Python 进程分别在 native 和强制 portable 路径竞争：不同插件、同插件不同请求、同一请求重放；不是只使用线程替身。
锁叶子链接/硬链接/非普通文件、目录在打开后被替换、portable 创建竞争后重验路径均有检查；没有实际 Windows 主机验收。
共享文件原语连带验证原产物注册、Shell 备份/恢复、后台进程记录与停止、owner 路径及插件目录客户端。
开发中首次并发检查失败已定位并修复：本机非排他 `O_CREAT` 同名竞争可返回 ENOENT，极小独立复现后采用排他创建、已存在则打开原锁。
只读审阅发现的提交分类覆盖、锁链接与 portable 建目录竞争也保留为针对性回归；失败没有作为通过结果。
最终 16 个相关文件共 425 passed、0 failed/error/skip，其中 6 项为文档回归；重复运行不累加计数。
本地严格 gate 已通过：Ruff、文档同步、strict code-size、diff 和 clean-package 全部通过，尺寸基线未改。
本片累计未发布 Python 增删未达到全仓阈值，没有重复全仓；线上 CI 未作为本地验收来源。
本片未发布部署、没有新增实际 TUI 或真实模型调用，不代表第 4 步验收完成；下一步先接管理授权与原操作链，再做隔离环境和完整多 TUI 装卸。

## 第 4 步静态包读取开发验证

本地源码首片包含包描述、有界 ZIP 读取和共用命令 JSON 读取器；未发布、未部署，也没有新增实际 TUI 编号。
7 个相关文件为 206 passed，另有 6 项文档检查通过，共 212 passed、0 failed/error；包括旧目录 wire/摘要、HTTP、客户端和 TUI 键盘管道回归。
静态包合同覆盖同一字节快照、摘要篡改、伪造宿主字段、动作目标、schema、路径逃逸、跨平台重名、链接和非普通文件，
以及重复 JSON 键、非法 UTF-8/孤立 surrogate、非有限数、归档/展开/单文件/描述/目录/成员预算。
损坏 DEFLATE、加密标记和非法文件名归一化为结构化错误；FIFO 在等待写入者前拒绝。
中央目录在 `ZipFile` 分配完整成员前有界预检，伪造较小 count 也不能绕过真实数量预算。
普通尾部未设置 ZIP64 哨兵也要拒绝实际 ZIP64 locator，防止标准库跳去另一段未预检目录；小型内存反例已覆盖。
这些是开发合同和组件证据，不证明 wheel 可安装、设置值有效、环境隔离、MCP 发布或真实热装卸。
该首片之后的安装事实进度见上一节；管理授权、激活和精确撤销仍待接通，再按第 4 步计划做真实多 TUI 验收。
首轮误用系统 Python 缺少已有开发依赖，未执行完整集合；切回项目 `.venv` 后完成上述检查，没有更改产品依赖。
本地严格 gate 已通过：相关回归、Ruff、文档同步、strict code-size、diff 和 clean-package 均通过，尺寸基线未改。
本片不触发全仓 pytest，也没有线上 CI 验收。

## 第 3 步宿主目录与实际 TUI 151—153

源码 `3730497fc` 已发布 main，并以同一 non-editable wheel 部署双机，每端 1,219 个包文件逐项一致。
默认入口与各自唯一 Gateway 同版，原配置、完整环境和除 runtime 外的参数不变，旧版本与回滚保留。
本机首次切换因记忆整理租约仍活动而推迟，待其自然结束后正常停启，没有强停；系统网络未改。
既有 143—150 属于前一发布版本；以下开发验证与新版实际验收分别记录。
合同回归覆盖声明 JSON 往返、类型/摘要损坏拒绝、owner/会话/版本/激活变化、冷用户不初始化及不写线程。
HTTP 替身覆盖可信 user/channel 覆盖伪造值、跨 owner 版本拒绝、旧版本/缺失业务版本拒绝，以及三个入口不进入原控制与模型。
这不是完整 auth 验收；原无中间件可信本机身份和群聊路由规则保留。
客户端覆盖 thin Gateway、完整 Agent 的 plain Gateway、direct 三模式；离线/损坏不本地兜底，旧响应和换作用域不污染缓存。
真实 prompt_toolkit Buffer 的开发用例覆盖候选版本跨参数编辑与刷新保留，后续 Tab 接受参数候选也不能换成新版本；
网络在事件线程外执行，逐字符与核心命令不请求目录，提交错误不进入聊天、guidance 或旧停止分派。
完整 Application 的键盘管道用例另验真实 Tab 接线、Ctrl-S 手动/提交后恢复、Ctrl-R 取消及清空后原版本提交；
草稿沿原 TuiDraft 携带版本，Buffer.reset 不发正文事件时显式清除绑定。组件测试仍不算实际模型 TUI 验收。
上述替身与组件测试不冒充实际 TUI；新版宿主帮助、输入编辑、活动任务隔离及普通工具链的实际证据见下表。
动态插件目录更改、在途撤销和实际业务权限留到第 4—6 步，不用空目录通过替代。
本片 22 个相关文件共 669 passed、1 skipped、0 failed/error；原生 HTTP 路由和现有控制回归一并通过。
跳过项是既有 `TestGatewayHTTPIntegration.test_stop_endpoint`，其遇 HTTP 409 后主动 skip；不把它计为服务停止验收。
首轮打包检查仅因 9 个新增源码/测试文件尚未登记 Git 而拒绝，登记后复验通过；没有以未跟踪文件绕过发布检查。
本地严格 gate 已通过：相关回归、Ruff、文档同步、strict code-size、diff 和 clean-package 均通过，尺寸基线未改。
未触发全仓测试阈值；开发验证不改变旧 TUI 或模型产物失败结论，线上 CI 未作为本片验收来源。

三路均使用真实 native TUI，每题只提交一次普通中文业务需求，测试者只操作原生按键与显式命令，未补写产物或执行模型生成的程序。
每路主/子线程绑定官方 MiniMax-M2.7 profile，结合原生往返与成功用量核对；没有逐请求 URL trace。

| TUI | 实际框架与工具证据 | 交付或覆盖边界 |
| --- | --- | --- |
| 151，本机命令与文件 | 15 份按键快照覆盖 Tab、Ctrl-S 手动/另一命令提交后恢复、Ctrl-R 取消及 Enter；5 次帮助/管理/错误提交均静态返回，线程内容不变、任务列表为空。随后唯一业务完成，7 次成功模型调用、6 对工具、锁 0 | 12 条立方数据及 78/6084 汇总、SHA256 正确，完整读回；说明漏掉要求原样保留的句号，模型却声称无误。框架通过，完整交付失败；按键发生在业务前 |
| 152，本机双子代理 | 两孩子各写、全文读自己的 10 行文件；attempt 区间重叠 14.163211 秒。父级在两人结束后才全文读取、合并及实际计算 SHA256，没有替孩子改写。主/子调用 9/3/3 次，工具 10/2/2 对，四个 attempt done、未决操作及锁 0 | 合并为单表头与 20 条数据，总和 210、平方和 2870；summary 的 21 行明确含表头，完整读回和交付通过。不扩大为整个工具阶段并行或长任务验收 |
| 153，测试机活动命令 | 主 attempt 启动后 10.872/12.016 秒提交帮助和错误命令；136.354 秒再次查看帮助时子代理仍在采样。三条命令不进入原生模型消息或 Shell，未取消或重开原业务；主/子成功调用 7/9 次，工具 7/10 对，三个 attempt 自然 done、锁 0 | 60 条真实记录跨度 119.3 秒，CSV 相邻时间差 2.0—2.2 秒，统计和 SHA256 一致，原句保留。脚本用了 `time.time`，未满足单调时钟要求；把可用空间为零解释成沙箱特性没有依据，完整交付失败 |

三路没有模型请求失败或重试，原生工具均配对；模型履约失败保留，不由测试者修正或改成全通过。
151/152 由不同只读审阅者分别复核，root 单独负责实现、部署和 TUI 控制；153 的原始采样、调用与控制时间独立读回。
实际插件贡献仍为空：空目录 Tab 不产生候选，也不提交任务；没有目录 HTTP 逐请求日志，不据此声称实测请求次数、动态贡献或 revision 更换。
真实插件版本变化、在途撤销及业务权限留到第 4—6 步；plain/direct 仍是本轮开发回归证据，不冒充新增实际 TUI 覆盖。
本片未重复旧十五分钟停止/调度用例，其证据与旁支限制按原轮次保留。第 3 步公共命令的本轮框架范围收口，第 4 步尚未开始。
测试机磁盘仍接近满，发布前须继续检查容量；未把模型的沙箱解释当作环境诊断。身份、路径、原始日志和产物仅留仓库外。

## 第 3 步参数合同与实际 TUI 143—150

参数初版 `c1484f7f2` 的真实 TUI 143 复现：输入 `/plugins help ins`、Tab 补全后按 Enter，
输入变成 `help install -h`，没有提交原命令。原失败保留；144/145 未发送业务需求，不计真实验收通过。
修复版 `faa12068a` 区分自动候选与显式 Tab：完整命令不自动补可选旗标，接受候选和 Enter 提交分开。
已发布 main，并以同一个 non-editable wheel 部署双机，各 1,214 个包文件一致、每端一个 Gateway。
默认入口同版，原配置、环境和启动参数除 runtime 外保持，回滚保留；本轮没有修改系统网络。

新增参数合同用例覆盖 Windows/UNC/尾反斜杠、中文与 Unicode 空白、空引用及引用片段拼接、未闭合引号、
长短参数、无值短组合、重复/缺值/错误类型、默认值、多值、保留帮助和 `--`。随机字面值经补全引用后再解析，核对逐值一致。
补全用同一词法/绑定状态，验证默认位置参数、停用插件、未闭合路径引用、等号值及负号字面量，不能建议最终落入其他选项的值。
CLI/TUI 在 idle、foreground、background、child 和 Gateway/direct 组合下消费帮助及错误，不产生聊天、插话、停止或任务队列副作用。
两个 HTTP 命令入口保留鉴权，回执结构一致；普通文件队列仍拒绝插件原文，核心自由正文、控制与模型选择沿既有回归。
修复版 10 个相关文件合计 483 项通过，零失败、错误或跳过；Ruff、doc sync、strict code-size、diff、clean-package 与新增行隐私检查均通过，尺寸基线未改。
475 项是修复前的开发结果，后续重跑不累加为独立覆盖。累计改动未触发全仓阈值，没有重跑无关全仓。
线上 CI 运行列表为空，线上 CI 未作为验收来源；HTTP 与竞态替身不冒充真实插件业务。

实际 native TUI 验收各业务只发送一条普通中文需求，测试者未写产物或执行生成的校验脚本：

| TUI | 实际观察 | 结论与边界 |
| --- | --- | --- |
| 146，本机 | 原生 Tab 接受 `install` 后 Enter 显示其用法，输入清空，未追加 `-h`；13 类帮助/错误均收到对应回执、输入清空且未创建任务 | 补全交互与静态入口通过；之前仅按旧屏幕文字匹配的观察另存，不作为最终证据 |
| 148，测试机 | 同样 13 类命令检查，包含未知插件/动作/选项、缺值、未闭合引号、中文空格路径、反斜杠、短开关、重复及 `--` | 两端合计 26 次检查静态返回；没有模型、任务或 Shell 副作用，不代表装卸可用 |
| 146，本机普通任务 | 12 条编号/平方正确，平方和 650，特殊字符串原样保存；模型保存并运行校验器，exit 0；4 次成功模型调用、5 对工具 | 数据和报告独立读回正确；校验器没有单独断言编号顺序和唯一性，覆盖限制保留 |
| 147，本机两孩子 | 两边各 8 条，合并 1—16、总和 136；父级读回两份源文件后合并；孩子 attempt 区间重叠 12.923484 秒 | 主/子模型调用为 6/3/3 次，工具为 5/2/2 对；三个 attempt 均 done、锁 0，原 worker 已退出；不计长任务 |
| 148，测试机普通任务 | 20 条奇偶标签正确，奇偶各 10、总和 210；5 次成功模型调用、6 对工具；conversation task/claim/run/attempt 收口、锁 0 | 首次校验器将 Markdown 粗体与普通字符串错配，输出失败但 exit 0；模型仅改校验器后再跑。最终数据正确，校验器仍漏检且 False 不转非零退出，不能把 exit 0 当强语义证明 |
| 149，本机普通任务 | 100 行平方表与报告正确，总和 5050/338350，报告原样保留插件示例句；3 次成功模型调用、4 对工具，保存的校验器实际运行退出 0 | 普通正文与工具链通过；两次控制在 final 后 6.49/6.83 秒才提交，只计空闲回执，不计运行中隔离 |
| 150，本机运行中命令 | 同一 attempt 启动后 0.017/0.331 秒提交错误命令及帮助，静态返回、输入清空；原 attempt 自然 done，无取消或换代 | 唯一用户正文未增加，原生记录、guidance 和 Shell 均无这两条命令；覆盖活动请求首秒，不扩大为长工具运行中覆盖 |
| 150，本机普通任务 | 100 行平方及报告正确，合计 100/5050/338350，示例句原样保留；3 次成功模型调用、4 对工具，校验器实际运行 exit 0 | 数据和工具链通过，收口后锁 0；校验器未核验报告，失败分支未转非零退出，模型质量限制保留 |

147 的原任务先于观察结束，运行中命令未发送；149 的观察器错误地等待线程持久 task link，
未能定位前台执行身份，错过活动窗口。这两项是测试准备/覆盖问题，原记录保留，不记产品缺陷或运行中通过。
146—150 的用户正文与原投递一致，原生工具成对，产物与原始写入/编辑一致；模型失败过程没有删除。
实际用量账均为 MiniMax-M2.7，结合每路 canonical profile 的官方 provider/端点核实来源，未获得逐请求 URL trace。
148 的 RuntimeDB `tasks` 身份实体仍为 active，与 conversation task completed 是不同层事实；本轮不扩大裁定该字段语义。
插件参数合同本片不改变停止语义；未重跑旧十五分钟用例，原长任务、停止竞态及未覆盖分支仍按原轮次单列。
动态 owner 目录、版本绑定、调用授权与插件装卸尚未实现，第 3 步整步未收口。原始屏幕、身份、路径和产物仅留仓库外。

## 整任务停止发布与实际 TUI 138—142

源码 `6713c769f` 已正常快进发布 main；同一 wheel 在本机和测试机各核对 1,210 个包文件，默认入口与各自唯一 Gateway 同版。
原启动参数仅更换 runtime，完整环境与私有配置不变，旧 runtime 和回滚证据保留。发布前全仓及严格 gate 见下节；线上 CI 列表为空，不作为验收来源。
测试机首次安装遇到已有磁盘容量不足，失败日志保留；仅清理此前本轮 runtime 中仍有对应源码的字节码缓存后重试。
旧心跳曾被空间不足写空，确认请求、claim、任务及原进程身份后正常停启同一 Gateway 恢复；这属于部署环境恢复，不计活动任务故障恢复验收。

五个独立 native TUI 均从默认安装入口进入，每题仅一次普通中文需求，实际 `/model` 选择官方 MiniMax-M2.7，核对私有 provider 地址与运行记录。
测试者仅操作可见的一次性批准和明确控制命令，没有改业务脚本、补采样文件或代做统计。原始身份、端点、进程出生标识、日志、产物及 tmux 保存在仓库外。
来源核对结合绑定的官方 provider/profile 与成功调用摘要及原生往返；没有逐请求 URL 明细，不把摘要称为逐请求网络账。

| 实际 TUI | 框架验收 | 交付或覆盖边界 |
| --- | --- | --- |
| 138，本机主任务 | `/goal pause` 后活动链及原采样继续；`/interrupt` 后原独立采样继续超过 90 秒；恢复领取第二代并复用原程序；`/stop` 后原 host、Shell、Python 均退出，两代 attempt cancelled、锁为零，69 秒后文件稳定 | 被控制中断，不作十五分钟完成验收；生成脚本读负载为 N/A，模型误归因沙箱，原失败保留 |
| 139，测试机独立长任务 | 140 停止期间原 session 与原进程保持、文件增长；之后自然退出零，未设置停止意图，Goal complete、task completed、claim finished | 程序运行 900.534 秒；299 条连续记录跨度 897.528 秒，间隔 3.004—3.155 秒；全部指标与最近 20 条统计独立重算一致。报告承认相对理想 300 条少 1 条；将最后样本称为结束时间、将采集耗时累积笼统解释为边界效应，属于模型交付偏差 |
| 140，测试机三个孩子 | 原三个活动后台 session 均确认 killed；Store 完成时间在停止提交后约 4.4—7.1 秒，后续 OS 探针确认六个登记进程及一个共享 runner 退出；三个 child attempts cancelled、锁 3→0；两次快照间隔 85 秒，16 项文件不变 | 停止后仍有约 5 秒收尾写入；Store 时间不当作精确 OS 死亡时刻。旧内存采样由模型自行停掉并重跑，排除旧 session；没有真实孙代理 |
| 141，本机交互终端 | 实际采样 PTY 的 Shell、Python 与共享 runner 退出，两个 child attempts cancelled；停止后 95 秒核对原进程消失、采样和进度文件稳定 | 模型实际创建两个兄弟，不计两层分工通过；REPL 和任务 ID 误用保留。已结束的主 attempt 历史不改写 |
| 142，本机真实递归 | RuntimeDB 核实 main→coordinator→worker；停止时孙代理采样运行，随后其原 runner、后台 host、Shell 与采样脚本退出，锁为零；停止后 66 秒快照的文件与前一次一致 | 统筹的 attempt 在停止前已 done，保留该历史；只验证尚活跃孙代理及所属资源取消。脚本颠倒 1/15 分钟负载标签，单列模型错误，不作完整业务通过 |

138、140、141、142 的测试 Goal 保持 paused，139 自然 complete；历史 TUI 与原始失败资料保留，未关闭系统网络、网卡、Wi-Fi 或调整路由和全局代理。
140/139 的固定证据由另一代理只读复核，测试控制和文件写入仍由主代理单独负责；139 的业务数据另做独立只读重算。
这些证据覆盖 Gateway 实际主链；direct/local、无数据库迟到启动、精确启动/停止交错、UNKNOWN 保留及旧轮不复活仍以本轮合同/替身/多进程回归为据。
本轮没有实际 Windows、无数据库 TUI 或恶意拒绝退出进程的验收，也没有把控制中断的样本算成长任务完成。
TUI 137 仍记历史 `FAIL_RESOURCE_STOP`；新修复的所测范围通过，可以继续第 3 步参数解析，不提前宣布插件装卸或整步完成。

以下源码验证各节保留当时的开发过程和覆盖限制；其中未发布、待实际复验的阶段性描述，以本页首节的最新发布记录为准。

## 文件模式启动接纳与旧快照保护（源码验证）

先通过正式取消入口复现两个问题：停止前排队的空身份工作能够重开 user-stop；同一次排队可被两个执行器分别激活。
修复后，显式无数据库模式在原 canonical 的 background_start 中预留非空身份，启动时一次性消费。
独立线程与独立 Python 进程验证同一身份只有一个执行器成功；这是开发测试，不运行真实模型。

覆盖显式恢复换代、业务仍为 CANCELLED 时再次停止、直接同步启动撤销旧排队、预览不预留、原候选身份运输，
以及消费后清空 active 指针和写入 running/finished/failed/reclaimed 标记仍不能重复激活。
旧快照分别来自预留前、激活前和旧 CANCELLED；新预留尚未激活时也不能被旧回收覆盖。
普通全量保存只允许同一原 launch/attempt 单调 reclaimed，原结果与回收的提交顺序不变；正常让出和失败后的新一轮均覆盖。
独立只读审阅未发现本片限定边界内的阻断问题，不能替代运行验收。

相关 60 个文件首轮共 1,251 项：1,245 项通过、5 项既有 xfail、1 项 Linux `/proc` 跳过。
随后补测并修正同身份启动失败诊断被过严拒绝的问题，最终启动接纳文件 50 项通过，重复项不累计为新增覆盖。
全仓首次收集被系统 Python 缺少已声明的 pyte/hypothesis 阻断；改用项目现有、依赖齐全的开发环境继续，未修改日常运行环境。
累计未发布 Python 增删超过约一万行，按仓库要求追加全仓验收。开发环境首轮发现 12 项失败：
CLI 参考缺少内部参数说明、并行 runner 的旧空身份夹具、CLI 越层导入、两项控制错误码未登记，以及 8 项旧 TUI 调用夹具。
启动标记的实际实现迁入原 lifecycle 服务，三个生产调用方直接使用；旧自由函数删除，没有新增转发或扩大导入白名单。
错误码明确区分未确认资源停止与未知控制，两者均不自动重试；其余按现行接口补齐说明与夹具。
修正后的两组定向分别 86、96 项通过；最终同一生产/测试源码完整重跑共 17,703 项：
17,642 passed、35 项既有 xfail、21 项 skip、5 项既有标注 XPASS，零 failure/error；不把 XPASS 再重复计入普通通过数。
未增加 xfail 或 skip，未调整尺寸基线。发布与安装版实际验收状态见 STATUS。
发布前文档 6 项、Ruff、doc sync、strict code-size、diff、clean-package 和全部未发布新增行的隐私扫描通过；
全仓结束后的 Python 文件哈希与受测源码一致。线上 CI 未作为本轮验收来源。
上述早期失败均保留，未通过新增 xfail 或跳过绕过；该源码已发布部署，新增实际 TUI 验收见本页首节，TUI 137 原失败不改写。

## 插话重放预留竞争（源码验证）

先用真实临时 RuntimeDB/邮箱和独立消费线程复现四种失败：初次读到 pending 之后，消息已被预留、
提交、消费或拒绝，旧重放仍按旧状态预留后继。修正后在旧/新 turn 排序锁和原回执锁内先修批次、复读状态，
仅 fresh pending 可预留；回应显示真实状态，通用失效恢复中 reserved 的既有规则保持。

定向覆盖提交/确认批次已落盘但投影失败、DB 预留成功后回执或索引写失败的同消息重试、
新轮 ID 排在旧轮之前/之后、只改绑被重试的一条消息、旧消费者在预留期间不能认领，以及 CAS 拒绝后来 current。
候选新 ID 只用于预先加锁；原 DB 事务提交后才成为持久事实，失败不能假装回滚或删除已经排队的轮次。
15 个相关文件共 398 项：394 项通过、4 项既有 xfail，无失败、error 或新增跳过。
原四项复现失败保留；这些是合同与受控并发验证，没有真实模型调用或新增 TUI。
文档 6 项另行通过；本地 Ruff、文档同步、严格尺寸、diff、clean-package 与新增行隐私扫描通过。
尺寸基线未调整；线上 CI 未作为验收来源。

该片完成时 LOCAL_UNMANAGED 旧排队启动仍缺少可撤销接纳；后续修复与验证见本页首节，源码尚未发布部署。
TUI 137 原失败不关闭，真实多 TUI 验收仍从 TUI 138 开始，提前公布实际 tmux。

## 固定子树停止与跨进程取消（源码验证）

`test_subagent_resource_stop.py` 使用临时真实管理器、RuntimeDB 和资源账本，覆盖主任务、孩子、孙代理及
已完成/失败/UNKNOWN 孩子的遗留资源；停止后恢复的新轮、新孩子、新 session 与其它任务不进入旧清理清单。
独立线程和屏障验证创建中停止、清理不占 creation 锁，以及主 Goal/task/creation 与子 Goal 的实际锁序。
部分权限、后台或 PTY 准备失败仍保留其它已提交成员；后台未确认不返回完整成功，PTY 请求不冒充已退出。

`test_runner_stop_relay.py` 在独立 Python 进程中启动两个受控 worker：主进程没有其本地令牌，原 session 心跳
读取持久取消并在 worker 进程中转交精确中断，另一个 run 保持运行；旧轮恢复后仍不能写新轮。
覆盖注册前取消、自然 done/failed、瞬时读取失败和旧配置投影拒写。
取消恰好发生在“读取取消状态→写心跳”之间时，心跳条件写失败仍继续检查，不能提前结束而漏掉中断。
这些 worker 和进程都是开发替身，不运行真实模型，不是 TUI 验收。

取消域改为窄 mutation 后，创建响应曾读取旧 task 对象而显示 PENDING；现改读最新 canonical。
相关创建、授权、取消、Gateway、本地控制、guidance、runner 和来源生命周期一起复验：
41 个文件共 901 项，895 项通过，5 项既有 xfail、1 项 Linux `/proc` 用例跳过，无失败或 error。
开发过程中旧测试入口/夹具、创建状态回执和心跳竞争的失败记录保留；没有用新 xfail 隐藏回归。
详情页取消不具有同 run 恢复资格，已撤掉基于相反假设的试验测试和实现，未放宽正式恢复合同。
文档 6 项另行通过；本地 Ruff、文档同步、严格尺寸、diff、clean-package 和新增行隐私扫描通过。
尺寸基线未改，线上 CI 未作为验收来源。diff 首轮发现三处空行尾部空白，删除后 AST 不变且复查通过。

此片未发布部署、未调用真实模型或新增 TUI，不能关闭 TUI 137 的 `FAIL_RESOURCE_STOP`。
该片结束时留下插话回执多预留与 LOCAL_UNMANAGED 旧排队启动两个发布阻断项；前者后续进度见本页首节。
下一轮实际验收从 TUI 138 开始，提前公布 tmux；每台单 Gateway，使用官方 MiniMax-M2.7。

## 子代理准确启动身份（源码验证）

`test_runner_start_admission.py` 使用临时真实 RuntimeDB/管理器、受控线程和执行替身：
覆盖旧 pending 在取消/换代后不能激活、UNKNOWN/终态拒绝、两个执行器竞争只有一个取得执行权、
迟到 launch 在 creation guard 外等待后重新核对、旧失败回执不覆盖新任务、回收标记不复活、
可显式恢复的 user-stop 不等于允许旧请求恢复、拒绝的 worker 不发布 session，以及旧心跳不覆盖新轮。
首次 session 写入失败不进入模型，并沿原结果门关闭本次已领取的执行权。
CLI 覆盖宿主真实 argv 构造至 parser/Options/Params 的闭环、普通 watch；后续文件模式补片已统一拒绝空身份。
缺值/重复/越界/watch 混用在构造宿主前拒绝，以及批量标记部分失败只收回自己的记录。
重复创建复用原接纳，宽泛创建参数替身与真实启动接纳测试分开，不为测试放宽生产身份条件。

本片仍未部署、未调用真实模型或新增 TUI，不关闭 TUI 137。完整子树冻结、终态资源、插话回执重放竞态，
以及无数据库模式完整停止/恢复在该片结束时仍待收口，后续进度见本页首节。42 个相关文件去重 757 项通过，2 项既有跳过（Linux `/proc` 与已移除 CLI）；
文档 6 项另列。原首轮旧替身错误、重复启动接纳回归及首次 session 写失败未收口的开发失败均保留，修正后复测通过。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与 clean-package；尺寸基线未改，新增行隐私扫描通过。线上 CI 未作为验收来源。

## 子代理协调基础（源码验证）

`test_subagent_coordination.py` 使用临时真实管理器/RuntimeDB、受控线程与独立 Python 进程，验证原路径锁的嵌套、
跨线程/进程互斥、不同 owner 独立、异常释放，以及创建/换轮/放弃等待同一短事务。
读取屏障覆盖 abandon 与新轮发布交错；旧 attempt 的废弃不能清掉新指针或并发字段。
授权后续做复读控制终态，旧失联快照不能重排已变更的 runner session。
子代理控制回归覆盖 admission→creation 的插话顺序、停止后的 fresh 拒绝、未新增 attempt/邮箱，以及用另一线程实际取锁证明启动发生在 creation 锁外。
在途孙代理测试改为独立创建/控制线程，避免用同线程重入掩盖真实并发边界。
24 个相关测试文件去重 441 项通过，1 项既有 Linux `/proc` 用例在本机跳过；文档检查另列。
文档 6 项通过；本地严格 gate 通过，尺寸基线未改，新增行隐私扫描通过，线上 CI 未作为验收来源。
新增读取屏障首轮挂在管理器转发入口而非实际 persistence 读取处，修正后重跑通过，早期失败保留。
以上不是实际 TUI 验收；该协调基础切片之后的启动身份补强见本页新条目。回执重放竞态、整树冻结和终态资源仍待完成；
未部署、未调用模型或新增实际 TUI，TUI 137 的停止失败保持，Windows 文件锁未获本片验证。

## 本地主链与未晋升热请求停止（源码验证）

`test_local_run_control.py` 使用临时真实 RuntimeDB/Store 和受控 worker，验证正式运行身份不同于消息编号、
启动前中断、Compact 发布与停止的 barrier 交错、旧句柄/新 job 隔离、已结束句柄不再发信号，以及部分冻结保留已提交清单。
plain/TUI 均传递原绑定回调；结构化中断收口为 interrupted；有 canonical attrs 时任务晋升仍可完成，未管理模式不伪造资源清理成功。
`test_gateway_unpromoted_stop.py` 通过真实 Gateway writer 验证 discovery 后发布的绑定、请求锁与任务锁互不反取、
固定主资源清单不追随恢复轮、错运输代次/过期 attempt 拒绝、未发布和坏绑定不借历史 main，以及纯中断保留独立资源。
已晋升任务读取失败和请求关闭后、获得任务锁前才晋升的交错均返回 unknown；不能声称已暂停 Goal 或已收停持久任务。
11 个相关文件整组 502 项通过；同一生产源码另补 1 项晚晋升 barrier 用例，并强化读取失败断言，相关重跑通过；合计 503 项功能回归，无 skip/xfail。
文档 6 项另行通过。首轮缺少正式绑定的旧夹具失败已修正并保留记录，没有用“只有消息编号”的替身绕过新身份合同。
本地严格 gate 通过：Ruff、文档同步、严格尺寸、diff 与发布清洁检查全部通过，尺寸基线未调整；新增行隐私扫描通过，线上 CI 未作为验收来源。
以上均为开发合同/替身验证，没有调用真实模型或新增实际 TUI；完整子树后台资源仍待接线，未发布部署，TUI 137 的原失败保持。
新一批真实验收继续使用官方 MiniMax-M2.7、多 TUI 和长任务；每台单 Gateway，预先公布实际 TUI 编号及 tmux 查看命令。

## 主执行权取消与持久主任务停止（源码验证）

`test_runtime_run_cancellation.py` 使用临时真实 RuntimeDB，验证原权限关闭、旧 attempt 不跟随新轮、pending 在提交时已激活就拒绝、
真实执行锁保留、attempt 单独 UNKNOWN 与崩溃调和的双 UNKNOWN、任意未知状态拒绝，以及完整子代理取消入口不覆盖 done/failed/UNKNOWN。
`test_task_resource_stop.py` 使用临时原 Store 和清理替身，验证主 run 与其它任务/孩子隔离、固定清单不追恢复轮、
领取后停止再恢复的 barrier 交错、迟到热请求不发新轮信号、Goal 恢复与停止准备串行、PTY 请求失败仍派发已提交后台清单。
本次提交与旧 redo 恢复故障分别注入；旧批次不被当成当前停止目标。PTY-only 回执只表示请求，不声称全部退出。
后台 claim 的早期异常释放中断登记，完成通知仍保留原准入例外；主/子取消、Gateway、Goal 与存储相关回归共同验证。
本片最终 20 个相关文件 735 项通过，无失败、跳过或 xfail；开发中早期夹具和接线失败单独保留，未覆盖事项不改为通过。
文档 6 项另行通过；本地 Ruff、文档同步、严格尺寸、diff 与发布清洁检查通过，尺寸基线未调整，线上 CI 未作为验收来源。
以上是开发合同、替身与并发验证，不运行真实模型，不是实际 TUI 验收；未运行 Codex 参考项目的测试。
该片结束时 direct/local、无持久任务热请求及完整子树仍待实现；前两项后续源码进度见本页首节，尚未发布部署，TUI 137 原失败保持。
跨进程未绑定旧工作片没有由本次线程中断测试证明；Windows 实机和完整停止树也不在本片已验证范围。

## 后台 v2 启动交接与精确句柄清理（源码验证）

实际 Shell 启动已经使用原 v2 Store：预留、host 绑定、child 创建标记和绑定、短命令观察、原执行权限与取消复查、明确交接。
新增 `test_background_handoff.py` 验证三个准入检查点、最后权限读取中的取消、交接前停止和启动者直接退出；host 消失但 child 仍活着时保持 UNKNOWN。
同任务两个后台 session 只停一个；冻结出生标识在终止快照入口再次核对。未确认后代不能因根进程退出而算清理通过。
日志上限测试改为直接验证唯一 host，旧 token 在交接后取消保留资源，显式 session 停止另验；不保留测试专用 watchdog。
完成通知同时覆盖 v1 数据与 v2 starting/running/unknown/not_started/killed/自然退出/明确停止，存储故障保留重试与去重义务。
提交后安装失败不发信号；信号后保存失败保留真实终止回执；其它事务恢复失败不能冒充当前停止已提交。
原 execution 的冻结复查不重复运行前置预算/审批门。上述是隔离开发进程、替身和持久合同验证，没有模型或真实 TUI，不宣称 Windows 已实机覆盖。
本片 18 个相关文件 533 项整组通过；同一生产代码另补 3 项网络只读投影用例，合计 536 项，无 skip/xfail，文档 6 项单列通过。
包含跨 owner 的坏 Store 不影响新交接；网络投影用内核替身验证 PID 复用前后拒绝扩范围、UNKNOWN 不冒充进程停止，不修改任何系统网络设置。
整任务控制、发布部署和安装版多 TUI 仍待完成，TUI 137 原失败保持；所有系统网络配置保持原状。

## 后台进程 v2 存储合同（源码验证）

`test_process_session_store.py` 验证字段及实例身份、版本 CAS、停止与交接的单向事实、旧 v1 不获得任务停止授权、精确身份选择及新恢复轮隔离。
新增 50 项存储合同与原相关回归合计 11 个文件 352 项通过，无 skip/xfail；文档 6 项单列通过。
文件故障覆盖提交前拒绝、发布后部分安装、日志删除失败、坏 redo 和后续版本冲突；全批预检失败时不返回健康子集或先覆盖前面记录。
独立 Python 进程在第一条记录安装后直接退出，下一 Store 读入口完成原批次；另一独立进程持锁时同根查询等待、异根读写继续。
这些进程只操作临时记录，不运行模型或产品任务。缺失系统锁明确拒绝；Windows 字节锁实现尚无实机验收，不声明掉电耐久。
上述存储片完成时 Shell/host 尚为 v1；后续源码启动接线见本页前节。整任务准入关闭和控制清理仍待接线，未发布部署，TUI 137 原失败保持。

## 资源身份与本地中断首片（源码验证）

当前源码的 10 个相关测试文件共 302 项通过，无 skip/xfail；覆盖身份、PTY、后台会话/完成通知、Shell 取消与 Gateway 控制。
新增两项本地中断回归先对基线真实控制函数运行，均因错误进入资源回收入口而失败；修复后通过。
其中同步 Shell 用自己的启动标记确认命令已运行，再从正式控制入口中断，最终工具回执为 `CANCELLED`。
另一项核对当前请求的精确信号、错误请求不被中断、插话和窗口快照保留；原 `/stop` 仍清退当前请求插话并派发子代理取消。
独立资源未回收由入口负断言验证，不能据此声称普通后台生命周期已经通过。

共享身份测试覆盖访问回退不补齐执行归属、输入字典变化不重绑资源、缺失选择身份拒绝及所有提供维度同时匹配。
原 PTY 实进程测试继续核对跨轮保留、整任务与精确 attempt 停止、其他任务及恢复轮隔离、启动期间取消。
上述为身份首片当时的开发反馈，不是真实 TUI 验收；后续启动 v2 接线见本页前节。整任务停止仍待实现及部署，TUI 137 失败保持。

## 公共命令首片验证

本片覆盖核心名称/别名、Compact 无空格与多行正文、btw 原多行边界、CLI memory 前缀和退出词范围。插件命名空间测试包括缺 ID、异常后缀、中文、引号、空格路径及 `--`；此阶段只验证明确拒绝，不声称已实现参数 schema。
TUI 使用实际 handler 的 fake 输入链检查空闲、前台、后台和子代理页面，确认无普通入队、插话、中断或退出；补全只编辑且不扫描路径。HTTP ask/control 另核对原处理中文件字节与旧 guidance 不变、中断回调未触发；文件提交在分配 ID 前拒绝，旧队列在追加用户历史和调用模型前拒绝。
定向测试与真正安装版多 TUI 验收分别记录；首片已发布并同包部署双机，以下实际测试只证明声明及命名空间边界，不代表插件可调用或热装卸通过。
首片 187 项定向通过（含 6 项文档测试），无 skip/xfail；7 个插件后缀分类用例先在旧实现失败。没有重复全仓 pytest。

安装版 `da3fdc422` 的 TUI 134—137 均从实际 `/model` 选择官方 MiniMax-M2.7，每个用例只提交一次普通中文需求；原始日志、身份、端点和产物保存在仓库外。

| 实际用例 | 框架与控制事实 | 交付及覆盖边界 |
| --- | --- | --- |
| TUI 134 本机、136 测试机 | 各 10 条命令含 7 种插件入口，均明确应答且未创建模型任务；随后普通文件任务分别有 11/4 对工具、9/4 次成功模型调用，自然收口 | 数据、汇总和完整哈希正确；校验器漏检、失败退出码及脚本完整读回有缺口，不记完整交付通过 |
| TUI 135 五分钟后台程序 | 单次启动，自然运行 300 秒；插件命令拒绝，Goal 暂停时正在进行的回合、Compact 和原程序继续；35 对工具、46 次主链加 2 次 Compact 调用 | 17 条记录与结果文件一致；未执行中断、恢复或资源停止。Goal 保持 paused，不能把保留的任务关联写成全体终态 |
| TUI 137 控制补验 | 单次后台启动；暂停保留执行，恢复后实际中断命中活动 claim，原程序继续；随后 `/stop` 已取消回合并暂停 Goal | **资源停止失败**：原后台进程在停止请求后至少 265 秒仍存活并追加数据；不能用任务状态或后续自然退出抵充停止通过 |

TUI 137 的首次中断发生在回合自然结束后，明确返回无活动回合；该条只记空闲响应，后续活动中断另有独立控制记录。
命令矩阵中部分首次截图早于重绘，后续有序帧与不同响应序号确认实际应答，原截图不改写。测试者未代写、代跑或修改业务产物。
TUI 135 的 `process_session` 调用为 list 一次、status 十四次，没有请求 wait；调用量含缓存，既不等于收费，也不证明长等待工具异常。
该片当时先定位 TUI 137 的任务资源停止缺口，第 3 步未收口；后续修复与新版验收见本页首节。实际控制测试不得改变系统网络、Wi-Fi、网卡、路由或系统代理。
后续只读核对原程序在 600 秒自然退出 0，原三个 PID 均消失，没有人工补停或补产物。失败保持；资源登记、停止入口及本地控制实现与命令首片之前字节相同。

## 原则

验证证据以 `test_verification_runtime.py`、`test_verification_repository.py` 和项目命令识别测试为准，覆盖真实工具出口、写后过期及 owner/task 隔离。
离线矩阵只检查实现/测试文件存在及尺寸报告，不能当作行为验收。无生产调用的旧 verifier integrity 模块及仅检查输入字典的测试已删除，不再计入运行时覆盖。
旧结果块修复场景及其跳过的验收已删除；`test_scenario_commands.py` 用正式参数解析器核对已删除/未知 case 被拒绝。
自然回复与宿主结束原因继续由 `test_subagent_finalize_helpers.py` 覆盖，保留相邻 runner 重试场景的已有 xfail，不能将删除旧协议测试计为修复该失败。

开发反馈优先定向合同、工具替身、模型替身和脱敏回放；真实 TUI 是最终验收最低要求。测试任务由被测代理完成，测试者不能代写产物后计为通过。
子代理审批回归必须包含真实创建生命周期的父会话关联，覆盖 child/grandchild 的批准与拒绝；仅裸 manager 创建不足以代表正常 TUI 派工。
归属记录读取失败不能退回主任务批准；具体审批与 capability grant 分开核验。对应 gateway control、background approval、owner policy 和 tool round 定向测试。
真实拒绝验收必须观察到具体审批及拒绝回执，并核对 handler 未执行；默认确认模式允许的普通命令没有弹窗时，该轮只能记为未覆盖拒绝路径。
同参拒绝回归必须跨实际子代理 Goal 续轮及 Compact，不能仅在同一个工具循环参数对象上重复调用。
`test_agent_goals.py` 使用真实生命周期、权限门和工具账，替换模型与用户决定；核对第二次同参不弹窗、不同参数仍申请、不同孩子独立、handler 始终未执行。
前后台活动回合的 Compact 夹具同时检查拒绝列表保持原对象，新调用为空；这些确定性用例不抵充安装版 TUI 验收。
审批与控制并发时，先确认退出审批模态及输入回显，再提交 `/stop`，以实际控制回执核对；不能把发送按键等同命令已执行。
长采样按实际启动/退出、追加数据与时间重叠验收；测试者处理审批的延迟、模型重跑和采样跨度须分别记录，不能用同一个 PID 数字证明没有重启。
前台 Shell 超时回归包括组长先退出、后代被重新挂到系统进程、忽略 TERM 需要升级终止，以及另一进程组不受影响；回执须核对成员退出和管道排空。
嵌套 Shell 后台检查同时覆盖字面 `-c`、常见包装命令、嵌套引用、普通字符串、重定向和 heredoc；启动前拒绝不得伪记为进程已经执行。
Shell 行数回归覆盖空输出、末尾有无 LF、空行、CRLF、裸 CR 和预览截断，联测真实工具出口的模型正文与展示事实；行数仅描述已采集文本，不表示业务数据条数或完整采集。

真实 TUI 输入多行需求时使用括号粘贴或整段提交，并从原生会话核对用户消息数；按键已发送不证明只提交了一条需求。测试者投递失误与模型任务失败分别留证，不计为通过。

普通需求用自然中文表达。权限、参数、隔离、状态和恢复由底座控制，不靠在提示词里写特殊限制规避缺陷。详见 [测试分层](docs/design/main-agent-contract-testing.md) 与 [测试清单](TEST_CHECKLIST.md)。

拟议结构调整、Computer Use 当前部署复验及 Jev 只读对照边界见
[可维护性评估](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md)。后端首批拆分已完成定向验证，既有测试与发布 gate 不变；
接入协议测试与真实桌面分别留证，Jev API 尚未验收。

本轮用户指定：所有真实验收通过实际 TUI；常规任务使用官方 MiniMax-M2.7，视觉任务使用官方 MiniMax-M3。
视觉会话复用 M2.7 的私有 provider 与密钥引用，只切换模型名；核对实际请求端点、模型及图片确已送达，不因同名模型推断来源。
测试覆盖长任务矩阵，历史日志只作案例来源；任务、工具、历史和产物须由被测代理自己完成。
登录认证也从 TUI 进入，其他账号的真实模型调用不混入本轮验收。截图、账号信息和原始日志只保存在仓库外。
每批可并开多个真实 TUI，必须公布 tmux 名称和查看命令，共用一个 Gateway；重连保留原会话身份。
新增回归覆盖 Full Access 的后台进程地址、跨权限视图恢复、PTY 共享解析及文本输入 Unicode 事件。
键盘事件替身和内存事件校验不计真实桌面通过，仍须由 TUI 模型操作后读取目标应用核对。
本轮已完成后台等待、PTY、取消续做、断连重连、Goal 暂停恢复、多子代理插话与手动/自动压缩；
本机英文和中文桌面输入读回一致。登录设备码被官方端点拒绝，真实账号确认/刷新/退出未通过。
具体通过范围与失败记录见 [本轮真实矩阵](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#本轮真实-tui-验收矩阵)。

## 模型与框架对照

本轮以下四组真实 TUI 对照已收口，未全部通过；常规验收默认仍为官方 MiniMax-M2.7：

| 组别 | 被测框架 | 模型与来源 | 用途 |
|---|---|---|---|
| A | my-agent | 官方 MiniMax-M2.7 | 当前失败基线 |
| B | my-agent | OpenCode 的 deepseek-v4-flash | 同框架比较不同模型接入 |
| C | Codex | 官方 MiniMax-M2.7 | 同模型比较框架 |
| D | Free-Code | 官方 MiniMax-M2.7 | 同模型的第二个框架对照 |

先逐组验证真实 TUI 模型调用及工具往返，再执行相同原始中文需求、相同初始文件和相同验收标准。
各组使用独立新会话、独立工作目录；优先在同一测试机对齐运行条件，保留真实工具和子代理能力差异。
三组 M2.7 核对同一官方上游、模型和可对齐的采样/输出/上下文设置；如必须经协议适配，单列适配路径，不能把同名模型当作同一链路。
记录安装版本、权限、额外系统指令、记忆/技能、工具声明、原生输入输出和程序生命周期；测试者只投递需求、观察及处理明确审批/控制。
进程事实使用宿主 PID、出生标识和 namespace PID 映射；CSV 中的沙箱 PID 不能直接当宿主 PID 查杀。
子代理 DONE、模型结束回复、后台程序退出分别取证；Shell 启动尝试次数也不等于成功采样进程数。
并行只用于独立目录和资源充足的任务；限流或资源竞争单列并做串行复核，不能混作任务执行失败。
覆盖普通文件、持续程序、多个子代理和最终汇总；关键失败用新会话重复，全部原始成功/失败均保留，不以重新运行覆盖失败。
如果同一 M2.7 在两个外部框架稳定通过，而 my-agent 的两种模型稳定失败，优先定位 my-agent 的上下文、工具协议和生命周期缺陷。
只有 my-agent 的 M2.7 失败时，也要区分该模型能力与本框架对它的协议/上下文适配，不能仅按通过组数宣布根因。

本轮按用户授权并行执行；四组均已在实际 TUI 验证模型调用和文件工具往返。Codex 直连官方 Responses，
Free-Code 与 my-agent 的 M2.7 使用官方 Messages 兼容接口；协议和宿主指令不同，不能把它们称为逐字相同的模型输入。
Free-Code 的 bare 预检限制工具面，不能当作多子代理验收；完整任务使用正常模式及独立配置目录。
Flash 初次配置缺少上游要求的会话请求头，补充 provider 的显式配置后新 TUI 通过；旧失败保留。
Free-Code 首次正常模式因测试者在密钥来源确认中选错选项而未发真实模型请求，归为测试准备失败；新会话经实际 UI 正确确认后重测。

| 真实 TUI | 框架功能 | 模型交付质量 / 原整轮结果 | 证据与边界 |
|---|---|---|---|
| 99：my-agent / M2.7 | 本场景通过 | 通过；自检覆盖限制保留 / 通过 | 三个孩子各自写源文件，主级完整读回；150 行数字、单表头、汇总和哈希一致；主级 11 组工具往返。孩子只检查行数和首尾，不宣称完整自动数值校验。 |
| 100：my-agent / Flash | 本场景通过 | 通过 / 通过 | 主子均使用 Flash；主级 17 组往返，孩子各自全量程序校验，汇总与真实文件一致。 |
| 101：Codex / M2.7 | 后续文件及父子链路已验；初始型号选择单列 | 失败 / 未通过 | 首次派工选到目录中但上游不支持的型号；后续真正生成数据的三个孩子均为 M2.7。一处平方值错误、合并保留三份表头，最终报告未修正数据。 |
| 104：Free-Code / M2.7 | 本场景通过 | 通过；最终读回覆盖限制保留 / 通过 | 正常模式父子均为 M2.7，主级 15 组往返；一个孩子经历三次工具错误后自行用 Python 校验成功，最终数字、单表头和哈希正确。主级完整读回源文件，最终文件的额外读回覆盖较弱，观察者审批耗时单列。 |
| 105：Codex / M2.7 | 文件及父子链路已验；额外路径行为单列 | 失败 / 未通过 | 单一 M2.7 目录、stock 指令及原工具模式下，平方错和重复表头均自行修正；summary 的一份源哈希抄漏字符，仍与磁盘不符。另有显式任务校验中间文件写到工作目录外并残留。 |

四组长题使用同一原始十五分钟三子代理采样需求。每路采样跨度须至少 900 秒；三路真实重叠时间如实计算，不额外要求共同重叠也达到 900 秒。

| 真实 TUI | 框架功能 | 模型交付质量 / 原整轮结果 | 证据与边界 |
|---|---|---|---|
| 106：my-agent / M2.7 | 工具往返、当时文件读回、自然进程收口通过 | 失败 / 未通过，已自然收口 | A/C 初版程序错误后重启，三路共启动五次。最终 A/B/C 为 63/62/62 条、915.0/900.9/900.0 秒；主级提前读取 A/C，却把中途 59/58 条、870/855 秒标为通过，旧哈希也未更新。主级 14 组原生往返完整，文件读取和当时前缀一致，无工具丢尾部证据。 |
| 107：my-agent / Flash | 持续并发、完整读回及自然收口通过 | 数据通过，报告覆盖受限 / 核心采样通过，报告有保留项 | 三路各只启动一次、62 条、至少 900 秒、自然退出 0；完整统计与哈希正确，共同记录跨度 876.811 秒。近零末间隔的绝对相等表述、首尾记录及恢复过的工具错误披露不足单列，不把四舍五入显示当计算错误。 |
| 108：Codex / M2.7 | 部分已验；自然完成未覆盖，精确资源清理另验 | 失败 / 观察者中断，未通过 | 多次启动/清空尝试与范围过宽的终止命令保留。通过实际 TUI 分别中断父子回合后，另外按出生标识精确停止本测试残留资源；不能把中断冒充资源已停止，亦无证据宣称其它测试受害。 |
| 109：Free-Code / M2.7 | 持续采样受宿主生命周期限制；A/B 提前被清理 | 失败 / 后续重跑被拒并中断 | A/B 提前结束回复，原生日志明确清理它们的后台任务，实际分别 41/42 条、约 600.548/615.189 秒，无结束记录；C 自然完成 61 条、900.000 秒。C 随后误读 A 文件并申请覆盖重跑，实际 TUI 拒绝并中断，未生成最终汇总；原文件哈希保持，资源已退出。 |

首尾记录的既定验收清单按每路首尾各两条；原需求“首尾两条”存在解释空间，只有首一末一时标明覆盖限制，不能借此认定底座故障。
TUI 109 的脚本重名被写版本守卫先拒绝，C 启动后 A 才读改脚本并启动；未观察到写错 CSV，不能把冲突申请直接当作覆盖已执行。
TUI 106 的父级完整读到 A/C 后台仍运行的报告及会话句柄，但零次调用进程查询；孩子 DONE 不等于采样结束。现有证据未确认新的交接字段丢失或查询越权缺陷，不因此增加第二套通知或专项判定。
本轮采样及被中断测试的精确所属资源均已退出，TUI 保留；观察者中断与资源停止分开留证，不冒充任务自然结束。
用户已明确选择分开记录：已核实框架功能后可继续结构重构，模型交付失败原样保留，不将整轮结果改为通过。
框架缺陷、必测功能未覆盖及可能影响框架的未知原因仍阻断相关步骤；没有发现缺陷不能单独充作功能通过证据。
每片分别列自动回归与实际 TUI 覆盖；未命中的内部策略分支不能靠普通长任务结果声称覆盖。私有凭据、配置和原始证据仍留仓库外。

第 1 步插件接线的只读核对另运行 17 项已有定向回归并通过，覆盖启动扩展、工具快照、MCP 重连、Skill 过期读取及命令补全。
其中冻结可用性的既有回归明确保证：工作片开始后可用性探针变化不改写旧快照；插件撤销须另读生命周期权威，不能改该断言假充热卸载。
测试入口为 `test_extension_plugin.py`，以及 `test_tool_runtime_scope.py`、`test_mcp_registration.py`、`test_skills_service.py`、`test_conversation_control_commands.py`、`test_tui_input.py` 的上述相关函数，均位于 `agent_py_agent/tests/`。
本项没有新增真实 TUI、插件实现或部署，也不是严格发布 gate；具体接入约束见 [插件方案](docs/design/PLUGIN_LIFECYCLE.md#第-1-步接线核对与迁移约束)。
同时更正 `registry_invoke.py` 的过期可用性注释，语法树与 HEAD 完全相同；原 17 项结果仍对应同一执行逻辑，不把注释修正描述成运行修复。

第 2 步进度策略首片：`test_background_main_agent_runtime.py` 与 `test_background_supply_backoff.py` 共 168 项通过，覆盖无进展退避/复原、失败退休、成功清账和供应/配置错误隔离。
迁移前后算法体经参数投影及常量改名后语法树一致，常量值未变；没有新增镜像测试。代码 `680b7b4e8` 已通过本地严格 gate 并同包部署双机，线上 CI 列表为空。

| 新版真实 TUI | 框架功能 | 模型交付质量 | 覆盖边界 |
| --- | --- | --- | --- |
| 110：三子代理十五分钟 | 观察范围通过；各启动一次、持续约 22—24 分钟、39 对工具匹配、全文读回相同、任务/claim/资源自然收口 | 失败；时钟和 CPU/最大内存采样含义错误，报告间隔计算错误；JSON 计数口径及首尾覆盖另记 | 原需求一次提交；审批准备等待不计采样时长；没有 progress policy 落账 |
| 111：测试机订单 | 通过；8 次模型请求、9 对工具、全文读回及自然收口 | 当前数据正确；哈希比对措辞及校验覆盖有问题 | 未触发 progress policy，不充作内部策略真机覆盖 |
| 112：本机单子代理 | 通过；主级 5 次、孩子 3 次模型请求，父子实际读写并自然收口 | 通过；数组 1—10、个数 10、总和 55 | 未触发 progress policy；通用 verification 的 UNVERIFIED 不冒充产物验收 |
| 113：本机订单 | 通过；12 次模型请求、13 对工具，完整读回且如实保留中途错误 | 当前数据正确；校验器只核总数、不逐组断言，错误也可能返回 0；报告覆盖措辞不实 | 未触发 progress policy；模型自行修正的初版数字和脚本错误保留 |

三路短题均为官方 MiniMax-M2.7，各一条原始需求，无请求失败/重试；测试者未代写文件或执行模型产物。
110 主子实际请求同为官方 M2.7，三个原采样程序自然退出 0。含 END 的记录为 91/91/97，报告此项正确；JSON 的 90/90/97 未统一说明 END 口径，不将歧义扩大为确定计数失败。
实际 A 的 seq 6→7 为 15.037948860 秒，B 的 seq 3→4 为 15.140295746 秒；报告分别写成 16.14/153.39 秒，按交付错误保留。
三份父级 read_file 返回均完整且与磁盘逐字相同，保存过的各阶段前缀未被清空。首尾仅各一条仍按原需求歧义记覆盖限制，不能指认为工具截断。
内部策略分支仅记算法等价及自动回归覆盖，真实持续链路单列；本首片收口不代表供应故障、唤醒、租约和恢复等整步验收完成。

供应退避次片：供应、后台运行及直属父级三文件 188 项，加观察消费、会话车道、配置恢复和未投递重发的 9 项，共 197 项通过。
时钟替身已指向新权威模块；原恢复用例增加真实 ready-thread 冷却筛选，新增 None 保留失败次数、非 None 假值清账的合同检查。
类、守卫、三个内部 helper 与剩余 runtime 的执行语法树等价，配置有效值未变；独立只读复核通过。代码 `ef9db100d` 经本地严格 gate 后正常发布，双机同包 1,189 项一致，配置原样且保留回滚；该提交线上 CI 列表为空，未作为验收来源。

| 供应状态版本真实 TUI | 框架功能 | 模型交付质量 | 覆盖边界 |
| --- | --- | --- | --- |
| 114：本机单子代理 | 通过；主级 5 次、孩子 4 次模型请求，6 对工具，父子全文读回、一个孩子自然完成并退出 | 通过；数组 1—10，个数 10、总和 55，与实际文件一致 | 无请求失败/重试，未命中供应退避 |
| 115：测试机订单 | 通过；9 次模型请求、10 对工具，失败回执与自行纠错完整保留，任务及 claim 自然结束 | 最终产物通过；30 行公式、三组统计、总计、哈希及首尾正确；初版金额错误保留 | 同一校验程序先退出 1，模型修正汇总后退出 0；校验器漏查行序、公式等约束，当前文件另经只读核验；未命中供应退避 |
| 116：透明代理基线 | 通过；3 次官方模型请求、2 对工具，实际写入与全文读回相同，自然收口 | 通过；原文精确一致 | 4 次 CONNECT 均到官方域名，不将 TCP 连接数当作模型调用数；未触发供应失败 |
| 117：定时工作连接失败与续接 | 通过；原 run/wake 保留，失败后冷却 30 秒，新 attempt 自然续接，任务、claim 与唤醒收口 | 最终通过；20 行平方表、平方和 2870、哈希及报告正确；模型自行修正时区与报告转义错误 | 只覆盖 typed 连接拒绝到达 wake 消费守卫；完整重试预算保留，故障及恢复期间 Gateway 未重启 |
| 118：冷却期间另一前台会话 | 通过；4 次官方请求、3 对工具、零重试，整个任务结束早于 117 的冷却截止 5.760 秒 | 通过；数组 1—5、个数 5、总和 15，全文读回一致 | 实际流、工具与终态时间有原始记录；没有持久化精确 HTTP admission 时刻，不扩大为另一后台任务或跨 owner 公平性 |
| 119：原设置回切对照 | 通过；3 次官方请求、2 对工具、零重试，写入和全文读回与磁盘相同，自然收口 | 通过；数组 2/4/6、个数 3、总和 12 | 原配置、启动参数和完整环境恢复后新开实际 TUI；专用代理端口已关闭 |

六路各一条原始普通中文需求，实际端点均为官方 MiniMax-M2.7；测试者没有补需求、改产物或代跑校验。TUI 保留，无本轮业务进程残留。旧 TUI 110—113 不计入此次切片。
117 的首个后台 attempt 持续 608.895 秒，保留 6 次模型尝试、24 次 HTTP 尝试、18 次 HTTP 重试及 5 次模型重试；这些失败由专用代理停用注入，不作为真实服务故障或模型能力失败。
typed `ProviderTransientError` 到达守卫后，原 scheduler run 回到 queued 并释放 scheduler claim，原 wake 待处理，失败的 background claim 与 attempt 保留；冷却从本次失败时刻起算，第二代 attempt 在截止后 0.290 秒领取同一工作，17.374 秒后完成并记录 `provider_supply_resumed`。
原一次性 job 保留为 `paused / one_shot_finished`，`next_run_at=0`；完成 run 进入 history 且只有一条，不将保留 job 误判为重复调度。执行 TaskRun/AgentRun/attempt 均结束；通用 task 实体标签仍为 active 不代表存在运行中的 attempt。
模型初次无时区定时被拒后自行修正，只建立一个 job；第一次 Shell 报告因反引号替换丢字段，模型改用文件工具修复。最终报告写入与磁盘一致，但修复后未再次调用 read_file；CSV 与汇总均完整读回。准备时的 CLI 参数错误、审阅时误以为 job 应删除的断言均另存，未改产品行为或冒充原始任务失败。

故障测试先经实际 TUI 验透明 TLS 透传，不安装证书或解密请求正文；只在已核实无其它待运行工作的单 Gateway 窗口中断本轮专用代理，不改模型、官方端点、密钥或重试预算。
不关闭网卡或 Wi-Fi，不修改路由器、系统路由和系统代理；故障只作用于本轮专用本地代理，系统网络和 SSH 保持可用。
自动记忆策展也可能发请求，不能只看前台队列为空；必要时使用私有临时启动配置关闭 `memory_curator_enabled`，原配置和策展待处理账本保留，逐项核对其它有效配置相同，验收结束后恢复原配置路径及进程环境。
本轮已核对原启动参数和全部环境相同、原配置字节未变、策展设置恢复、专用代理关闭、系统代理及默认路由前后相同。回切仅发生在原任务自然完成且 Gateway 无活动工作之后，不算活动任务的 Gateway 重启恢复验收。
本切片实际覆盖连接拒绝、同线程冷却、普通前台隔离与原定时工作恢复；未实测 SSE 中途断流、额度/配置错误、None/假值及其它后台消费入口。前述 197 项是自动化验收，不算未实测分支的真实通过；第 2 步其余路由、租约和重启恢复仍需各自验收，不能用本轮结果代替。

路由切片的源码验收：后台主执行、观察路由、唤醒召回、Goal 生命周期、Gateway 退避、供应退避、会话事件、定时运行/tick、模块边界、Goal 工具和文档共 12 个文件，另加投递、控制、进程完成与直属父级的 10 个精确用例，共 353 项通过。
观察路由既有用例直接调用新权威模块；新增覆盖线程绑定优先且不读 owner、路径命中后不预读后续来源、线程加载失败保留 owner 路由、local 不冒充外呼通道及默认目标选择。真实 wake 接线用例改为实际选择路由，不再替换已经删除的方法。
Goal 两段处理按领域/回调映射后与原函数执行语法树一致；能力预扫、单 wake 执行及默认目标选择保持，观察编排仅迁接收者和地址调用。两个新模块在独立进程导入不会加载 runtime，无新增循环依赖。
最初三个新夹具漏填 binding 必需字段、一处误改 frozen 线程已修正；尺寸检查曾发现观察方法使 ExecutionMixin 超限，现保留为 runtime 独立编排函数，严格尺寸无 hard，基线未放宽。以上为开发验证，不算实际模型任务失败。
路由源码 `f9a1088d3` 已发布并同包部署双机，各 1,191 个包文件一致，默认入口和唯一 Gateway 同版。启动环境、配置和除 runtime 外的参数保持，旧运行目录及回滚保留；该提交线上 CI 列表为空，验收来源为本地严格 gate 和本片新 TUI。
TUI 120—123 均从实际 `/model` 选择官方 M2.7，核对 provider、端点和成功调用，只提交一次原始需求；每台共用一个 Gateway，未抓取 HTTP 正文。四路均已自然结束，框架与模型交付分列如下；不复用 TUI 114—119 证明新版本。

| 实际 TUI | 本片框架证据 | 模型交付结果 |
| --- | --- | --- |
| 120：持续 Goal、暂停/恢复与 Compact | 一个程序采样 960.000 秒、97 条，同一 Goal/任务续接，3 次 Compact，82 对原生工具，全部 54 个执行代次结束，程序退出零 | 采样主体通过；报告的间隔统计、两条间隔明细和内存单位错误，交付失败 |
| 121：并行普通订单 | 11 对原生工具完整，最终 30 行数据、汇总和哈希正确，任务自然结束 | 校验脚本仍报告不一致，模型却报告全部通过，交付失败 |
| 122：三名并行子代理 | 三个孩子各一代执行，实际重叠约 29.234 秒；父子共 15 对工具，源数据与合并 120 行正确，归属执行和锁自然收尾 | 数值、哈希、报告通过；父级提前读取源文件，违反先等全部孩子结束的原需求，整项交付失败 |
| 123：定时工作与原会话投递 | 一次 scheduler run、一个 handled wake、一个完成代次，6 对工具，原会话只收到一次最终回复，一次性 job 正常退休 | 20 行平方表、平方和 2870、哈希和首尾记录均正确，通过 |

120 的暂停发生于活动 claim 内：同一 claim 继续执行后自然结束，采样在暂停期间保持同一进程并继续追加；152.668 秒后通过实际 `/goal resume` 恢复原 Goal/任务。没有把暂停当成中断或停止资源。最终原生读取完整返回 CSV 全部 98 个物理行，与磁盘逐字一致；退出零从同一工具调用的完整归档核验，预览截断不当作原回执缺失。
实际最小/最大采样间隔为 9.991213 / 10.008800 秒，报告为 9.993425 / 10.008858 秒，另两条间隔明细抄错。生成程序将本平台按字节返回的最大驻留内存直接标为 KB，报告将实际增加的 48 KiB 写成 48 MB；这些是生成程序/模型统计错误，工具没有篡改原值。模型关于内存增长原因的推断没有证据，亦保留。
120 共 121 次成功物理模型请求、零重试；35 次进程状态查询、33 次行数/尾部查询、2 次进程等待。高频轮询和费用相关用量原账保留，不把任务自然结束等同执行高效，也不据请求数推断模型内因。
121 模型修正了初版金额与 Shell 计算错误，但遗留校验脚本仍比较错误常量、没有读取汇总文件。两次实际命令都返回不一致警告，模型修正汇总后未重跑，却报告全部通过；不把脚本退出码零当作内容验收通过。
122 父级完整读回三份已写好的源文件，随后孩子才完成；实际合并发生在全部孩子结束后。只有读取顺序违反原需求，不能写成提前合并或结果错误。重叠来自 canonical attempt 时间，并不证明三个系统进程或模型 HTTP 请求同时运行；18 次模型调用均成功。
123 首次执行在到期后约 25.669 秒，全部业务写入在到期后；前台 2 次、后台 4 次模型调用均成功。原生历史载体的外层正文为空不等于零模型输出，其后普通最终回复在原 TUI 可见。任务、run、attempt 和 claim 已结束，一次性 job 保留为 paused/one_shot_finished，不误判为残留运行。
四路测试者均未补需求、修产物或代跑生成程序；业务自然退出后，双机活动 claim 和 pending/processing 请求为空，历史 TUI 保留。本片所测路由链路收口，继续租约/恢复拆分；其它通道 owner 回退、异常 Goal 分路和活动任务 Gateway 重启恢复不由本轮证明，第 2 步整体仍未完成。系统网络不作为故障开关。

租约/恢复源码片新增 `test_background_claim_execution.py`：25 项合同先在迁移前实现通过，
覆盖成功/None/假值、精确身份与时钟、领取后终态和 unknown/不可读变化、子代理归属、忙碌车道、
中断/Compact/普通及模型错误分类、启动/停止/事实读取/结算异常的原传播范围。
真实心跳用受控回合保持执行中，释放前核对实际 renew、延长的到期时间、另一持有者不可抢占；
不能用 finish 后的 heartbeat_at 代替续租发生证据。迁移后补全 wake/policy/observation 的阻断参数组合，
恢复守卫另验每次查询、当前 checker 替换、日志指纹及不可读判据。旧测试替换点直接迁至新权威入口，
模块独立导入检查不许加载 runtime/网络后端。最终 324 项相关回归通过，包含后台来源、调度、恢复、
前后台共享车道与精确 claim 释放；独立源码复核未发现阻断性语义差异。以上为无模型的合同回归，
不替代真实模型验收。源码 `e1797a67d` 已发布并以同一 non-editable wheel 部署双机，
各 1,193 个包文件一致，默认入口和每台唯一 Gateway 同版；原环境、私有配置及回滚保留。
部署时两端均空闲，活动任务恢复须另行验证。线上 CI 列表为空，发布验收来自本地严格 gate。

本片 TUI 124—127 均为官方 MiniMax-M2.7，各只给一次原始中文需求；核对实际 profile、端点、
成功用量和原生历史，没有抓取请求正文或改日常默认模型。以下实际结果与自动回归分列：

| 实际 TUI | 框架及实际产物事实 | 交付和未收口边界 |
| --- | --- | --- |
| 124：十八分钟 Goal | 单次采样 74 条、约 1080.003 秒、单进程自然退出零；74 对工具、74 个执行代次和 Goal 自然收口 | 报告最小/最大间隔错误，交付失败；不能用状态 complete 代替报告核验 |
| 125：并行订单 | 30 行、三组统计、总订单 1605、金额 14625 和哈希正确；7 对工具、8 次成功模型调用，运行及锁收口 | 最终校验器漏查分组和行数，失败分支仍退出零，未读回全部文件；完整交付失败 |
| 126：定时与长子代理 | 原定时工作一次执行，父子交接和进程自然退出；50 批算术正确，记录前缀未被改写 | 末 20 批约 0.003 秒内补齐，记录仅约 870.157 秒；十五分钟和节奏要求失败。能力审批的稳定资源记录在后续代次被改为 DIRTY，修复及新版验收见下文，原失败不改写 |
| 127：另一会话普通任务 | 1000 条数字和平方，无缺失/重复，总和 500500、平方和 333833500；3 对工具、4 次成功模型调用，任务及锁收口 | 数据与原需求交付通过；跨任务并发只按真实时间关联，不扩称全部后台公平性 |

124 通过实际 `/exit` 关闭客户端，约 98.767 秒后从 CLI 重连同一 session；原 Gateway、Goal、
采样进程及数据前缀保持，未重发业务。另暂停目标约 181.664 秒时，当前 claim 正常结束，采样继续；
再经实际 `/goal resume` 恢复同一目标。根开发 Goal 始终 active，没有把这两个目标或三种控制混为一谈。
同一 claim 的两次运行中快照显示心跳推进约 540.152 秒，后次心跳跨过前次到期时间，证明真实续租，
不是借 finish 写时间戳。124 的完整 CSV 已返回模型，142 次成功物理模型调用及高频轮询单列，不能据此声称高效。
实际全部单调时钟间隔最小约 0.000245 秒、最大约 15.008010 秒；即使排除末尾近邻记录，
最小仍约 14.990755 秒，均不同于报告的 14.996411 / 15.005516 秒。测试者未修统计或补产物。

126 的原前台 claim 在定时到期前已结束，因此本例没有命中共享车道竞争；不能只凭同一会话先后执行宣称竞争通过。
其两条资源记录先由成功的能力审批确认 STABLE，后续正常新代次却被旧轮清理标为 DIRTY；
对应源码与迁移前一致，属于新发现的既有框架问题。没有持有锁不等于资源全部干净，未人工清账。
本例父子执行闭环与这项资源状态缺陷分列，不能将框架整项写成全过。
TUI 128 的唤醒到期入队后，现场仍观察到原前台 claim 持有；原 claim 释放后约 0.947 秒，
定时工作才以 previous_finished 接手，最后正常收口。此为同车道排队和串行接手的实际证据，
没有持久记录证明内部 acquire 曾返回 busy，该分支仍由合同测试覆盖，不据推测宣称真机命中。
128 的程序单次生成 12 批但仅持续约 220.075 秒，未满足四分钟；未全量读回原始批次，
哈希只有批次文件的前缀，完整交付仍失败，测试者未补做。

活动恢复的两次控制准备没有改变 Gateway：128 错把 request.workspace 对象当字符串，
129 错读早期尚未持久化的 conversation_runtime，把自己的 claim 当成其它工作而保守退出。
两次均未执行停启，分别记为观察者准备错误与恢复 NOT_EXERCISED，原业务照常完成、没有重投。
129 的 24 条交易公式、四期收入/支出、全季 2388/2592/-204 和哈希正确，12 对工具及执行链收口；
模型自行修复汇总后实际重跑校验成功。报告首两条混入表头、脚本未全文读回，完整交付失败。
有序活动恢复和资源状态修复的安装版复验继续见下文。系统网络始终不作为故障开关；
故障只能作用于明确归属本轮的应用进程或测试专用代理，不操作网卡、Wi-Fi、路由或系统代理。

资源换代的修复另记：新增 `test_runtime_db_stable_mutation_generation.py`，正常续轮、活动接管、
升级调和三项先在旧实现失败，均复现 STABLE 被误置 DIRTY；修复后通过。测试同时验证原 STABLE
记录逐字段不变、新轮能重新开始写入、旧轮仍无权限、未完成写入转 DIRTY、既有 DIRTY 证据不变。
生产只收窄原事务中的资源更新条件，不修改锁、代次 CAS、UNKNOWN 工具记录或历史数据库。
相关仓储、工具、能力申请及 Gateway 恢复合计 186 项通过，另 6 项文档检查通过；无全仓重跑。
该修复已发布并用同一 wheel 部署双机，每端 1,193 项包文件逐项一致；本地严格 gate 已通过，
线上 CI 列表为空，未作为验收来源。以下四路均从实际 TUI 发起一条原始中文需求，
使用官方 MiniMax-M2.7；配置来源和实际成功用量已核对，不宣称完成逐请求抓包。

| TUI | 实际框架证据 | 交付结果和覆盖限制 |
| --- | --- | --- |
| 130 本机活动恢复 | 首轮模型流已有输出、尚无工具操作时有序停启 Gateway；原请求、run 和会话保持，旧轮 UNKNOWN 经 `confirmed_noop` 进入第二代完成。5 对原生工具、3 个成功持久操作，执行锁清理 | 24 条记录、四期汇总、总数 4068 和哈希正确；模型主动切到 owner 根写文件，未遵守当前目录。恢复上下文的 cwd 正确，归为模型交付失败；不据零操作恢复推断未知副作用可自动重放 |
| 131 测试机能力申请 | 一次执行自然结束，3 次成功模型调用、2 对工具；没有子代理、能力申请、能力裁决或真实审批 | 模型只写脚本并口头询问执行权限；计划分支 NOT_EXERCISED、交付失败。不追加提示或由测试者代执行 |
| 132 测试机普通对照 | 5 次成功模型调用、6 对工具，2 个成功持久操作；run、attempt、claim 完成，零资源写入记录及执行锁 | 18 条记录公式、三组数量 75/183/291、金额 525/1281/2037、总数和哈希正确。脚本未全文读回、校验器失败退出语义不足，完整交付失败；独立核对不冒充模型自检 |
| 133 本机交互终端 | 测试 Goal 暂停后当前 claim 仍运行；随后实际中断、恢复，同一 run 从第一代进入第二代。原 PTY 的 STABLE 记录跨代全字段相同，第二代成功再次使用原终端；旧等待命令 UNKNOWN 整行保留。27 对工具、26 次官方成功调用，14 个持久操作成功，Goal/run/claim 完成，锁为零，原解释器自行退出 0 | 三批累计 30/100/210，间隔约 516.6625/152.5351 秒，数据正确。覆盖 cancelled 后续代；自然 done 续代由确定性回归覆盖，131 的能力裁决仍未命中。读回时序与模型自修另见下文 |

130 控制器按精确 PID 与出生标识确认原进程退出，才用原参数、完整环境和配置恢复唯一 Gateway；
不使用强杀或产品 restart 的强制清理分支。控制器自己的 11 项替身检查只算测试准备验证，
不计入产品真实验收。133 的人工审批等待单列，不把整段墙钟时长当作连续计算工作量；
真实控制只针对测试 Goal，开发 Goal 不受其 pause/resume 影响，历史 DIRTY 和 UNKNOWN 均不改写。

133 按原需求交付通过：同一 PTY 的非截断结果在写 JSON 前完整包含三批数值与实际时间戳，
JSON 的累计和间隔核对正确，随后模型输入 `exit()` 并读到退出零，最后又完整读取 CSV/JSON。
原文要求完整读回数据，没有限定必须先以文件工具重读 CSV；不额外添加文件读取顺序作为失败条件。
两次交互 Python 语法错误及一次未定义变量错误已由模型在原解释器内自行修复，原回执保留。
测试者仅批准精确操作和执行既定控制，没有代写、代运行或补充业务需求；测试 Goal 完成不等于开发 Goal 完成。

第 2 步四个结构切片的本轮框架验收收口，下一步进入公共命令合同。真实用例覆盖长任务、
普通并行对照、父子执行、定时唤醒、冷却续接、运行中续租、重连、排队、有序恢复与控制语义；
内部 acquire 的 busy 分支、自然 done 后 STABLE 换代和其它错误分路只按对应确定性回归记账。
131 原计划未命中，133 用同一底层资源协议补验取消后换代，不将两者描述为相同业务场景。
两端最后只读核对均为唯一 Gateway、无活动 claim、无待处理请求或活跃定时工作；历史记录原样保留。
这些结论不关闭模型履约失败，也不承诺通用 UNKNOWN 的 TUI 手工裁决能力已经实现。

## 必测模块

发布前 HTTP 取消回归覆盖 JSON/GET/SSE：响应头尚未返回时先中断再出现连接/清理异常，必须保持中断且仅请求一次；
未中断的对照必须原样抛错。实际 socket 等待用例与确定性竞态用例同时保留，不能靠多次重跑偶然通过。

长时间运行增量矩阵见 [设计与验收](docs/design/LONG_RUNNING_EXECUTION.md)。普通验收并行运行官方 MiniMax-M2.7
TUI，共用单 Gateway；只有用户明确要求的慢模型专项才另用单路对照，定向回放和真实通过分别记账。

| 模块 | 验证要点 |
|---|---|
| 配置与模型 | 服务商协议、密钥引用、上下文容量、会话选择、用户默认、子代理继承与显式覆盖 |
| 身份与工作区 | 多用户同 Gateway、家目录隔离、管理员显式越界、工具权限与真实路径 |
| 主子代理 | 创建、插话、停止、恢复、换代、结果落账、父级唤醒、重复及乱序事件 |
| 历史与压缩 | 未压缩历史完整性、Unicode JSONL、展示分页、长输出引用、压缩计数、模型切换 |
| 工具 | 参数校验、成功/失败状态、文件读写、搜索、补丁、命令/PTY、网络、MCP |
| 记忆与技能 | owner 隔离、自主记忆维护、人格确认、索引发现与按需读取；技能选代表场景 |
| TUI | 输入回显、换行、粘贴、滚轮、复制、完整展开、到底部、主子代理视角、Todo、活动状态 |
| 调度与交付 | 普通回合与目标模式、挂起唤醒、断线、后台交付和恢复；IM 无环境时标明未测 |

## 插件装卸拟议验收（待实施）

插件方案尚未落地，本节不是通过记录。先用合同/替身/回放验证代次、权限、撤销、清理与失败状态，
再用官方 MiniMax-M2.7 的实际 TUI 共用单 Gateway 并发验收：一路插件长任务、一路正常内置任务、一路插件管理。
对应 [合并实施计划](docs/design/MAINTAINABILITY_AND_JEV_REVIEW.md#下一轮结构整理顺序待实施) 的第 3—6 步：
命令解析、装卸链、TUI 使用和故障验收分别留证，全部通过才计首批插件可用；不能仅以补全或单次成功调用收口。
首批从显式本地包和只读示例开始；在线安装、更新/回退留后续专项，不以未实施功能阻塞后续结构拆分。
三路会话是角色安排，实际 TUI 编号与 tmux 名称须在启动后报告；每轮核对官方 provider/端点，保留装卸前后的 Gateway 进程身份。
核对未安装/已装停用/启用/卸载的有效能力与模型配置，卡死卸载时管理入口和无关任务可用、Gateway 不重启、插件不被重连复活。
版本切换、旧任务恢复、命令冲突、owner 隔离及清理失败按真实事实单列；报告实际 TUI 编号与 tmux 查看方式。
命令合同须覆盖 `/plugins` 管理与 `/plugins@ID` 调用、同源帮助/补全、停用时只读帮助、未知目标/参数、缺值、短开关组合、引号与跨平台路径；
`@` 不误触发文件补全，命令错误不转聊天，参数文本不作为 Shell 执行。此项仍为待实施矩阵。
详细矩阵见 [插件设计](docs/design/PLUGIN_LIFECYCLE.md#实施顺序与验收)，不得用现有工具测试替代这些尚未执行的验收。

合并计划第 10 步新增 [10 个简易插件与组合验收](docs/design/PLUGIN_SAMPLE_ACCEPTANCE.md)，仍为待实施：
先按 3/4/3 三批验证各包的实际功能、命令/帮助/配置、启停与卸载，再并发覆盖普通任务、插件长任务和管理操作。
提供工具或模型任务的插件还须由普通中文需求触发；纯展示插件用 TUI 操作验证，不强行增加模型调用。
用量等显示遥测不进入模型请求或调度判据；事件、目录和文件读取均守 owner/run 权限。
常规调用使用官方 MiniMax-M2.7；图片文字可用本地 OCR，需要模型视觉理解时用同一密钥引用的官方 MiniMax-M3。
每个包均覆盖故障与清理，全部卸载后对照核心能力和新会话请求基线；任一失败保留原证据，不以另一个样本通过抵消。
样本只用合成文件、测试页面和专属资源，结果由被测 my-agent 自行产出；日志、截图与个人配置不进仓库。
Audit/摄取不列入本轮新增验收；共享模块既有回归按改动影响保留，不能因此删除既有功能测试。
十步逐批验收的场景、证据及进入下一步条件见 [执行 Goal 与测试矩阵](docs/tasks/REFACTOR_PLUGIN_GOAL.md)。

## 重点定向回归入口

- runner 正常让出：`test_subagent_runner_result_state.py` 通过真实 manager 写回和读盘，联测六种结束原因、
  旧错误清理、显式失败、缺失/未知原因及状态冲突；`ok=False` 的正常让出不应填 `runner_error` 或最近错误。
  联合直属等待、恢复、结果载荷、能力授权、来源工作者和 Compact 共 237 项通过、1 项既有 xfail。
  真实 TUI 核对递归父级的 `PENDING / interrupted`、空错误字段、精确等待身份及结果触发的续跑；
  另用真实 `/stop` 验证取消不被清成普通等待，模型分工质量单独记录。
- 探针端点：`test_backends_base.py::test_probe_endpoint_matches_transport_request` 截获实际 HTTP 出口信封，
  比较能力诊断与请求 URL；覆盖 Messages、Chat、Responses 的代理前缀、版本后缀、完整接口、尾斜杠及成功/未证明能力共 36 组。
  替身只提供协议响应，不替换地址计算；与原生工具、请求作用域和 OAuth 相邻回归共 228 项通过。
  新版真实官方 MiniMax-M2.7 TUI 完成写程序、执行和读回；一次多余参数被拒后自行修正，原失败保留。
  后台会话未持久化完整探针结果，因此地址合同与真实工具链分别留证，不把替身信封称作真实抓包。
- 后台审批桥：`test_background_tool_approval.py` 联合 Gateway 代理控制、owner 权限模式、TUI 队列与后台活动测试。
  核对原始请求批准/拒绝、跨会话拒绝、claim 换轮失效、无接收方关闭式失败、缓存隔离；主审批不能扩权到子代理控制入口。
  Goal 暂停仍允许当前审批；回合中断取消等待，明确任务停止另验资源收回。真实验收从 TUI 启动 Goal，
  未批准前核对无执行，正常面板批准后读取原调用结果；拒绝及旧 PTY 续用分别留证。
  本片 428 项定向通过。真实 TUI 已分别证明暂停后批准原调用、跨 claim 读写同一 PTY 和拒绝不执行；
  程序仅启动一次、原终端正常退出，30 项实际输出与报告逐项一致。测试者没有执行任务程序或修改产物。
- 插话存储组合：联合 `test_runtime_guidance.py`、Gateway 控制与输入交付、主子恢复、Compact 和终态测试。
  故障注入直接指向提交批次组件或账本投影；保留提交后部分回执写入失败、确认重放、旧格式迁移和改绑半写入样例。
  新组件的独立导入不得加载聚合 Store 或运行执行器。实际 TUI 必须在同次短输入操作内确认完整回显并提交，
  保存提交时的 running claim、精确输入回执、模型原生输入和最终消费状态，区分忙碌插话与任务结束后的追问。
  本片定向 2,362 项通过；综合全仓 17,116 项通过、35 项 xfail、22 项跳过；该全仓结果早于本轮控制修正。
  旧 Goal 暂停后 PTY 继续不算资源停止失败，当前控制语义另行验收，不能由历史结果替代。
- PTY 生命周期补修定向覆盖 `test_pty_sessions.py`、`test_gateway_conversation_control.py`、
  `test_orchestration_cancel_subagents_tool.py` 及公共进程终止、线程取消和导入边界：
  跨模型回合资源停止、同会话不同任务、跨 owner/thread、精确 attempt、Popen 前后取消竞态、自然退出 PID 不再发信号。
  Goal 暂停/清除保留当前执行；有无 Goal 或目标 paused 时的 interrupt 都不能停止独立资源。
  真实 TUI 分开验证：暂停 Goal 后当前链及 PTY 继续、回合结束后没有 Goal 自动续跑；
  中断回合后独立 PTY 保留；明确 `/stop` 后所属资源停止，恢复原程序保留原始记录前缀。
  另一窗口同期采样用于确认隔离。不得由测试者执行采样脚本或补报告；旧暂停后静止的观测仅是旧实现记录。
  后台恢复还须验证策略、冻结快照和原生工具 Schema 均含获准的终端工具；显式配置或 owner/task 禁用仍有效。
  `test_background_main_agent_runtime.py` 联合进程/PTY 测试覆盖 Goal、子代理、定时和 Audit 唤醒目录，真实复验接续原未完成任务。
  当前两组定向分别为 444 项控制与 267 项目录/工具测试；真实子代理续采保留 19 条后采满 30 条，主代理 PTY 中断后继续并收尾。
  后者恢复轮走文件查询，未覆盖旧 PTY 句柄的后台读取；模型自设时限耗尽及报告错误均留为失败，详见 STATUS。

- 线程、消息、任务关联与 Audit 组合：核对绑定并发、Compact CAS、幂等追加、游标分页、终态不复活、
  命名工作修订及进度退休，并联测 Gateway、子代理和 Memory 的实际领域接口。
  最新定向 2,206 项通过、28 项既有 xfail、1 项 Linux 平台跳过；故障替身指向新组件，不保留旧 API。
  实际 TUI 已验暂停/压缩/重连续采、两路长命令并行、普通后续指令和用量；失败及未测范围见 STATUS。
  实际输入必须核对 canonical 用户消息与原生请求。tmux 批量文本使用括号粘贴，确认完整回显后提交；
  授权弹窗可能改变输入焦点，拟发送字符串不等于已接收。提交前任务已结束时只计后续指令，不计运行中插话。

- Goal、观察、唤醒与进度领域：联合目标编辑/预算/恢复、发布顺序、观察扫描、策略失败/退休/GC、
  插话与子代理回传测试；工具 mock 和故障 monkeypatch 直接指向所属领域，不能沿旧 Store 补导出。
  独立导入检查组件不加载 Store 或调度执行器；跨域旧账归档保持同一时间、保留期与原顺序。
  定向 1,236 项通过、28 项既有 xfail；新版双 TUI 验证约 98 秒暂停静止、压缩重连、原程序真实续采，
  并行分批生成各 2,000 条 CSV/JSONL 后程序核对全部编号、数值、中文、时间、样本与 SHA-256。
  报告误述取消原因、临时脚本目录错误与被拒绝的状态面读取分别留证，不冒充完整任务质量通过。

- 存储上下文与执行租约：联合索引、账本 GC、线程中断、后台运行、Goal 恢复与 Gateway 错误测试，
  验证原子领取/续租/终态、同任务恢复、坏账报告、惰性指纹缓存、动态能力读取与原路径。
  `initialize=False` 初始化及缺失线程/租约读回不能创建目录；组件独立导入不能加载组装入口或执行器。
  本片联合 1,064 项通过、4 项既有 xfail，追加只读打开合同一项通过；259 个原函数/方法中两处构造函数单独审阅，其余逻辑比较一致。
  新版双 TUI 验证约 95 秒暂停静止、压缩一代重连、原程序实际续采到 12 条，另一会话两路各 10 条、重叠 104.01 秒。
  租约终态、子结果唤醒、插话、目录隔离、原生投递去重与费用入账分别核对；报告的采样路叙述错误保留。

- Gateway 请求组件：联合会话上下文、Compact、前台历史、请求错误、Goal 恢复、模型选择和后台运行测试。
  历史提交、写前绑定与输入渲染的替身直接注入各职责模块，不给旧导入增加转发；错误字段和持久格式保持。
  `test_runtime_module_boundaries.py` 实际调用共享历史选择器后检查未加载 Gateway，另核对新组件不加载网络或执行器。
  本次 666 项定向通过，110 个定义/常量的搬移前后逻辑比较一致；该证据不替代新版真实 TUI 的暂停、
  压缩、重连、后台交付、插话和目录隔离验收。原始快照、差异与真实日志保留在仓库外。
  新版双 TUI 已验证暂停四条后约 111 秒静止、Compact 一代并重连、原程序实际续跑至十二条；
  另一会话两路各八条采样重叠 84.021 秒，插话与后续更新未改原 CSV。原生历史按精确 request/part 无重复。
  初次报告遗漏合计和时间心算错误保留；后续程序复核只修正部分数字，两处间隔范围仍错，不计完整任务质量通过。

- Gateway 流式边界：联合 streaming、verbose_progress、foreground_transcript、thinking_archive、
  main_activity 和恢复测试，验证迟到读取、审批缓存、事件顺序、插话、脱敏和思考归档；猴子补丁须指向
  实际组件，不通过旧请求模块导出。前后台超窗继续由现有真实 store/替身运行器验证携带、代次与取消。
  纯 carry 与流模块能独立加载，不引入执行器；真实多 TUI 仍须逐路核对模型源、输出、控制和原生账本。

- 配置为空时不得隐式选择 echo；需要本地后端的夹具必须显式配置。默认值、工作区列表、Goal revision
  和界面文案断言与当前合同同步，不能恢复已删除的兼容语义来迎合旧测试。
  `test_home_runtime_bootstrap.py` 与 `test_r103_run_reuse_no_split.py` 联合验证同任务复用 canonical run、
  新请求身份、归档终态和目录准备失败的 attempt 收口；不放宽终态断言。
  历史不可读与任务绑定冲突在唯一错误表分别登记，保留原唤醒、禁止猜身份或重放未知副作用。

- 后台交付边界：`test_background_owner_delivery_commit.py` 联合后台运行时与历史快照，验证
  未声明路线的 canonical 交付、声明但不可用时保留外发义务、整封/纯附件冻结、已发送只补本地、
  v1 载荷重投、commentary/final 去重、终态抑制和精确审计回执。独立导入不加载调度器或网络后端。
  实际 TUI 核对父子结果返回、Goal 暂停/恢复、重连后最终回复与原生历史身份；外部 IM 的失败重投
  仍由已有替身合同验证，不冒充真实 IM 发送通过。

- 后台执行边界：联合 `test_background_main_agent_runtime.py`、`test_background_history_snapshot.py`、
  `test_background_owner_delivery_commit.py`、`test_thread_model_selection.py`、`test_cli_resume_contract.py`，
  验证同片溢出重试、八次压缩公平让出、正常/取消/异常原生历史、模型冻结和技术续跑来源约束。
  独立导入检查执行模块和纯续跑判据不反向加载调度器。真实 TUI 并行覆盖父子接续、Goal 压缩恢复、
  执行中停止及新目录隔离，不能用原候选版本的通过结果代替这批执行器验收。
  本批五路真实 TUI 已留证：Goal 保留暂停前四条、压缩重连后补齐六条；受管后台进程停止后不再写入，
  续做保留前六条并补齐十二条。定时作业独立于前台 `/stop`，不能把中断等待当作取消定时任务；
  此类任务须经 TUI 工具显式清理。字段计算通过与模型写错时间、采样间隔失败分别记账。

- 后台准备边界：`test_background_context_runtime_errors.py`、`test_background_main_agent_runtime.py`、
  `test_background_owner_delivery_commit.py` 联合 Gateway 控制、上下文用量、TUI 模型统计回归。
  保持未压缩历史完整、detached 任务创建锚点与 lineage、读取失败不调用模型、展示统计不进入上下文。
  `test_runtime_module_boundaries.py` 在独立进程检查合同、策略、上下文和历史模块加载不引入调度或网络后端。
  真 TUI 增量复测子代理返回后的后台接续与 Goal/Compact；文件搬迁的单测不替代真实验收。

- 首次 Goal 目录：TUI 控制传输、HTTP 持久回执、Goal 初始化、普通请求目录和 store 绑定联合验证。
  覆盖模型菜单先建空线程、目录与 roots 同步、相对/缺失/外部/远程 owner 拒绝、已有目录不变、
  同 ID 改目录冲突及 v1/v2/v3 摘要防篡改。实际 TUI 分两路验证 owner 内目录执行与 owner 外拒绝，
  拒绝必须发生在创建 Goal 和调用模型前；不能先发送普通聊天替首次 Goal 补目录再计为通过。

- 仅思考响应：Chat/Messages 的流式与非流式不得因无正文丢弃有效思考、用量或隐藏重试；
  真空白仍报错。`test_native_tool_use_ir_messages_flow.py` 验证两次无工具续跑逐条保存、
  OpenAI 实际出站回放、下一工具轮和最终保存不重复；`test_response_decision_native_tool_use.py`
  验证坏工具修复不回放未执行工具。联合原超时探针、截断、插话及中断历史回归。
  真实抓包先检查仅思考响应是否漏入下一请求，再评价真实任务完成，不能仅凭缓存高称通过。

- 渠道失败提示：`test_tool_failure_channel_hint.py` 联合错误语义与工具执行回归，覆盖测试/编译非零、
  参数/状态/权限拒绝、取消、未知、真实网络不可用、重复回执和新回执覆盖旧失败。
  错误正文不能提升为控制码，缺 call_id 不猜新事件；关闭阈值和每工具一次不变。
  真实 TUI 核对失败码、下一次请求是否误加换渠道提示及实际排错进展；不能把提示过滤通过当作模型不再循环。

- 后台失败退避：`test_gateway_lane_retry.py`、`test_gateway_loops_resilience.py`、
  `test_background_main_wake_recall.py`、`test_model_unconfigured.py` 与会话模型选择联合验证。
  覆盖缺配置长时间不重跑、模型引用删除/恢复、精确旧会话改选、默认选择不串会话、零值冷却、
  远端拒绝不误判本地缺配置、同 owner 健康车道、跨 owner、短锁与有界回收。
  联合 `test_background_supply_backoff.py` 和 Goal 测试核对 scheduler 不提前关闭目标/消费 wake；
  真实执行错误和额度限制仍受原保护，不把原已暂停或受阻目标无条件激活。
  真 TUI 在未配置会话设置目标，再通过 /model 选模型，核对原目标恢复、唯一最终回复及原 wake；
  另一路正常任务并行，不能把手工改任务文件或替身模型当作真实恢复验收。

- 重启与持久回执：`test_tui_worker_paths.py` 验证已提交消息在 PID 暂不可见时仍读取原 terminal；
  没有终态沿既有超时返回，不再入队；未提交请求仍报告服务停止。真实 TUI 将重启与消息投递交错，
  区分队列提交、实际执行、模型 final 和前端展示，不把服务启动命令退出当作已经就绪。

- 旧计划续写与多用户路径：`test_task_progress_advisory.py` 验证精确旧账更新、缺省状态保留、跨会话、
  子代理/独立后台目标拒绝和无隐式重绑；`test_gateway_chat_conversation_context.py` 验证本地队列的
  自定义 owner、外部/未知来源、final/实时/历史路径一致。真实 TUI 用原会话追加验证笔记并索要完整路径，
  对照 native final、canonical public row 和终端画面；不能把宿主脱敏误记成模型漏答。
  路径样例必须真实含 owner/request 标识，分别覆盖绝对路径、Windows 路径和相对目录；
  仅用不含标识的示例不能检出第二层替换。外部来源不因该修复暴露完整宿主路径。

- 进度部分更新：`test_task_progress_coverage.py`、`test_task_progress_advisory.py` 与派工对账测试，
  覆盖只补备注/元数据、空状态、各规范状态、更正标记、新项默认及模型/展示一致；没有 ID 仍按参数错误返回。
  已完成项须先写入旧备注，再更新并读回新备注；只断言状态未变或空备注成功不算覆盖。
  原生 Schema 必须明确 ID 必填，不能为了部分更新把全部字段都标成可选；标题/状态仍允许按需更新。
  真实慢任务的计划状态和原工具回执并行核对，旧数据不推测重写，不以勾选进度替代产物验收。
- 显式采样：`test_provider_sampling.py` 联合三种 backend 测试，覆盖默认省略温度、显式零值/范围端点、
  单次摘要覆盖、工作片冻结与子代理继承。慢模型客户端对照严格串行，切换前检查原请求及服务端槽位退出；
  真实出站诊断只写私有测试目录，不记录认证头、不改请求协议，不把参数回放当完整 TUI 任务通过。
- 批次执行事实：`test_current_turn_execution.py`、`test_native_tool_use_ir_messages_flow.py`，覆盖
  只追加当前批次、Compact 轮号重置后的身份区分、未知副作用、批准来源、有界省略及全轮核验保留。
  连续请求逐字节保留此前缀和全部工具对，旧会话不强制清理。真实出站核对新增事实大小及实际任务进展。
- 代理树重复查询：`test_agent_tree_model_view.py` 联合工具重复观测回归，验证仅时钟/心跳变化继续计数，
  实际工具进展、终态和产物改变重新计数；原查询结果、权限和生命周期不改变，不用耗时判死。
  子代理查自己的子树时，从规范范围裁决排除自身查询活动；主代理显式查询该孩子仍保留其真实进展。
  正常模型并行验收与慢模型串行对照同时进行，不能把正常模型的轮询浪费漏记为慢模型专属问题。

- 子代理模型续派：`test_orchestration_background_dispatch.py`、`test_model_profiles.py`、
  `test_thread_model_selection.py` 及 worker/timeout 测试。覆盖 Gateway 无默认模型、父子异模型、
  child thread 改选后的恢复、并发显式注入和并行工具线程的依赖传递；旧捕获函数回放须能重现配置/连接不一致。
  真实验收区分普通父子交接与 coordinator 等待孙代理后的重新派工；没有真正产生孙代理的不计后者通过。

- 中断历史：`test_native_tool_use_ir_messages_flow.py`、`test_cli_run_conversation.py`、
  `test_gateway_chat_conversation_context.py`、`test_subagent_runtime_compact.py`、
  `test_background_main_agent_runtime.py`、`test_background_owner_delivery_commit.py` 联合验证
  原生调用/结果保留、未知副作用占位、空正文与异常不改成功、后台静默/外发失败仍留事实而不伪造送达、
  原请求幂等、同一 repair 补交。真实 TUI 用执行中 Esc 后继续，核对下一轮真实输入和已发生的工具事实；
  一路慢模型不派子代理，正常模型并行验证父/子与普通后续轮。历史旧缺口不按显示文字补造成功。

- 客户端计时：`test_gateway_client.py`、`test_gateway_admission_wait.py`、`test_tui_worker_paths.py`，
  覆盖时钟前跳/回拨、失联超时和活动租约续期。真实 TUI 可隔离替换客户端模块时钟注入跳变，
  不修改系统时钟、不影响 Gateway/模型计时；单独记录注入已发生、真实终态及任务产物，不能把替身当真实模型。
- 账号认证：`test_model_oauth.py`、`test_model_oauth_transport.py`、`test_tui_model_menu.py`，
  联合模型配置/共享目录/会话选择/原后端测试。覆盖跨 owner、冻结引用、刷新轮换、取消和退出竞态、
  私密参数保留/清除、重定向拒绝及协议复用。真实 TUI 的设备码确认另验；替身不作为实际账号权益证明。
- 用量增量：`test_model_call_ledger.py`、`test_tui_model_metrics.py`、`test_reproject_model_usage.py`，
  成功/异常/取消共用结算；累计容器重建换代，来源切换不重复算，旧账与缺报不得估算重写。
  真 TUI 中断后追加、Goal 后台交接、子代理及 Compact 必须按 provider 分项对账。
- 慢模型额外排队：模型配置与首事件估算定向测试，默认 0、按模型覆盖、无穷大/布尔/非法值拒绝。
  真实单槽并发等待、滚动输入、Esc 分开验；额外预算不能修复 schema 编译错误或输出截断。

- 工具重复恢复：`test_tool_guardrail_gate.py`、`test_tool_call_guardrail_runtime.py`，覆盖 300 次自身拒绝
  与 400 次成功调用的持续计数/提醒、不同归档引用同正文及相同预览不同尾部。
  不清计数、不挤掉原观测、真实失败/不同结果/实际写入及零阈值；拒绝经真实归档和 native 投影后仍有
  计数及换路说明。`test_tooling_filesystem.py` 验证行/字符非文本失败提示及无额外文件转换。
  真实 TUI 复验单文件动画与正常连续工具任务；没有触发重复门的真实任务只算正常链路验收。

- 模型资源与后台策展：`test_provider_request_scope.py`、`test_memory_curator_v2.py`，覆盖同端点前台
  优先、退出释放、pending/游标保留、pre_compact 屏障、request-local 预算、取消连接及旧请求未退出不重试。
  真机只开一路本地慢模型且不派子代理；官网正常模型可并行对照。缓存核对需同时查推理服务槽位日志，
  外部请求/代理别名和缓存容量不能从 TUI 百分比推断。
- 后台策展会话头与失败分类：`test_memory_curator_v2.py` 用真实 OpenAI/Anthropic 兼容后端加本地 HTTP
  回放，验证策展 run 自带非空会话头、值只由 owner_id + run_id 派生、同 run 重试同值、不同 run/owner
  不同值、run 结束 ContextVar 复位；去掉 `_execute` 的会话绑定时这三条用例必须失败。
  `test_curator_timeout_observability.py`、`test_curator_timeout_adaptive.py` 锁定供应商调用阶段的
  ValueError 归 `CURATOR_MODEL_FAILED`（结果、state.json、失败诊断一致），解析失败仍是
  `CURATOR_SCHEMA_INVALID`；失败诊断附脱敏后 ≤200 字正文，整条 warning ≤300 字符且可解析。
  `test_curator_failure_attribution.py` 锁定包装异常的根因类名与 errno，以及解析失败的响应形状（长度、截断、
  结束原因、输出 token、JSONDecodeError 出错位置），均不含正文。

- 状态读取：`test_agent_tree_model_view.py`、`test_agent_tree_three_layer_status.py`、`test_orchestration_tools.py`，
  覆盖规范原状态、scope 裁决、恢复路径不外泄、实际报告与缺失报告、八节点直接可读及大树省略计数；
  与 `test_tool_context_reducer.py` 联合核对输出外置后仍保留状态和精确逻辑回读入口。
  真实 TUI 验证运行中查询、完成后交接和真实文件读取，不以最终 DONE 替代工具调用证据。
  无活动任务目录时验证当前会话过滤，显式主请求根验证 parent_id 子树；已有终态报告需实际读取。
- 思考预览：`test_tui_renderer.py` 覆盖流式折叠行数、接收字符数、无换行长段落和窄终端，
  与 `test_thinking_display_boundaries.py`、`test_tui_complete_detail.py` 联测；真实 TUI 需捕获多帧计数增长。

- 派工一致性：`test_orchestration_dispatch_state_contract.py`、`test_subagent_prompt_contract.py`、
  `test_subagent_role_templates.py`，覆盖启动/运行/终态混合快照不推导父级动作、角色正文隔离、冻结自定义角色、
  主代理保留自身分工与用户限制；`test_tool_context_reducer.py` 验证精简回执保留唯一动作建议。
  真实 TUI 分开记录父级独立工作、分层是否如实创建、活跃范围是否重复写、确实依赖结果时是否正常等待。

- 子代理交接：`test_subagent_registered_artifact_handoff.py`、`test_subagent_output_alignment.py`，
  覆盖自然/结构化结果、孙级身份、最新文件、删除、日志排除、账本链接拒绝、cwd 与相对/绝对路径一致、
  不从输出声明增权、不从内部同名文件隐式搬运。真实 TUI 另核对创建谱系与完成信封中的实际路径。
- 补丁交接：`test_artifact_registry.py`、`test_tools/test_filesystem_tools.py`，真实 handler 到归档再到自然收口，
  覆盖新增、修改、移动、删除、部分失败、同路径不同历史 artifact_id 与当前删除状态，保留权限和执行事实。

- 生命周期：`test_dispatch_liveness_and_revive.py`、`test_subagent_runner_result_state.py`、`test_direct_parent_lifecycle.py`。
- 宿主停止：`test_subagent_process_control.py`、`test_shell_orphan_kill.py`、`test_orchestration_cancel_subagents_tool.py`。
  受控进程验证另开 session 的写入者、忽略 TERM 的后代、独立兄弟保留和无句柄退出核对；
  未确认回执不得标记已终止或触发重派。它们不替代真实 TUI：还需在子代理长命令运行时暂停，
  观察文件保持不变，再从原会话恢复，分别核对原记录前缀和实际命令续跑，不由测试者补产物。
- 父子并行：`test_direct_parent_lifecycle.py`、`test_runtime_guidance.py`、`test_subagent_activity_diagnostics.py`、
  `test_runner_session_pool.py`，覆盖逐个完成、同时释放去重、模型答复/登记等待竞态、忙父级交接、
  慢流不误杀、阶段/审批诊断、旧 attempt、进度快照覆盖、通知重试和心跳回调失败；真实 TUI 组合另列。
- 退出与积压：`test_executor_exit_recovery.py`、`test_closeout_recovery_paging.py`，包含 exact attempt、慢执行存活、
  未知副作用封存、超过分页窗口、消费去重和重启游标；实际模型/故障注入仍需独立 TUI 证据。
- 历史：`test_conversation_store.py`、`test_background_history_snapshot.py`。
- 存储组合：`test_conversation_store.py` 另覆盖两个独立实例并发提交同一用量及累计快照，核对唯一行、首次时间和费用；
  联测 `test_conversation_context_usage.py` 的代次 CAS、`test_conversation_goal_tools.py` 的共享时钟/小数余量/重启，
  以及 `test_runtime_module_boundaries.py` 的领域独立导入。旧调用、getattr 与测试替身须一并迁移，不保留旧方法转发。
  本片真实 TUI 须核对暂停期间 Goal 秒数、恢复后的原程序续做、Compact 独立用量和主子账本归属；原始证据留仓库外。
- 目标：`test_conversation_goal_tools.py`、`test_goal_lifecycle_recovery.py`、
  `test_agent_goals.py`、`test_background_main_agent_runtime.py`、`test_gateway_conversation_control.py`、`test_run_audit_terminal.py`。
  中断增量另联测 `test_tui_input.py`、`test_tui_agent_navigation.py`、`test_r103_ledger_selfheal.py`：
  空白补全、前后台插话、Esc 与明确暂停分离、同任务换代、恢复总账及历史关闭事件保留。
  后台参数构造必须走到真实回执消费，不能仅断言邮箱写入；先后完成的历史目标不得误触发并行冲突迁移。
  子代理在 Goal 后台轮创建再回报时，持久 task ID 不需要伪造 user 消息；并测 active/complete 与混合普通请求，后者真实缺失仍报错。
  覆盖默认工具可见、无工具/无 Todo 的安全续跑、审批/暂停/错误边界、旧绑定显式迁移、
  命名目标的精确回合上下文、前后台共享时钟；普通模式不得因此自动续跑。
  另覆盖每代理一个未结束目标、父子计费与权限隔离、编辑版本冲突、暂停后保存不恢复、
  子 Goal 在同一 attempt 中跨轮与 Compact 续接、独立历史不覆盖；实际草稿键盘操作仍须 TUI 验收。
  当前真实 TUI 已覆盖主子保存、放弃、编辑中停止，以及旧版本冲突保留草稿；详情与未测组合见持续目标设计。
  `test_saved_goal_guidance_reaches_its_agent_and_can_cross_provider_boundary` 复现运行中改主目标被子代理误领，
  覆盖主/子消息隔离与提交模型、确认消费完整链路；共享 root task 不能授予父级邮箱。
- 模型：`test_model_profile_tool.py`（manage_models 工具）、`test_model_provider_management.py`、`test_provider_sampling.py`、`test_model_unconfigured.py`；
  未配置可进设置但不发请求，发布默认值为空，用户显式选择仍保留。
- TUI：`test_tui_interaction.py`、`test_tui_markdown.py`、`test_tui_pty.py`。
- 模型统计：`test_tui_model_metrics.py`，覆盖协议缓存分母、缺报、重放去重、明细裁剪、重试、主子隔离、重连和宽字符窄屏；独立压缩成功/失败均落账，绑定工作片的不重复结算；统计字段不得影响模型上下文。
- 开发检查：`test_contract_test_pyramid_gate.py`。

文件位于 `agent_py_agent/tests/`；改模块时补充对应边界用例，不以此短列表代替所有模块回归。

## 真实 TUI 记录

工具正文完整性：`test_tool_output_externalizer.py` 必须经过生产 `ToolExecutor` 与
`archive_tool_output_projection`，而不是仅手造完整 `ToolResult` 给 reducer；覆盖预览阈值以上的
完整文件、分页及继续游标、归档读取 JSON 和显式保留正文，同时保留大输出外置/脱敏回归。
慢模型复读验收沿原始任务和输入文件建立独立 owner/TUI，记录真实出站回执、重复调用、
产物与独立测试结果；不更改测试项目或用硬停计为通过，不并发占用慢模型。
`test_tools/test_shell_tool.py` 另从 Schema、规范执行入口及真实本地进程验证长命令，
与空输入、危险命令、owner 沙箱、超时和非零退出联测；不把旧长度拒绝当安全边界。

后台进程重复观测：`test_process_sessions.py` 回放 33 次 uptime 变化但状态/输出不变的等待，
并核对原始结果哈希、软提示频率、日志同尾增长和退出后重置；真实 TUI 单独记录模型是否采纳提示。

每次公布 tmux 名称；使用隔离测试用户和同一 Gateway。记录开始/结束、版本、供应商/接口、会话与请求身份、实际工具结果、最终产物、失败和未测边界。不写真实密钥或私人对话。

本轮慢模型只启用一路 TUI、不派子代理，优先验证长等待、流式、插话与停止；正常远端模型可多路并行。
本地缓存诊断同时核对界面最近一次比例、输入用量和推理服务实际预填充，不用延迟反推缓存，更不把缓存未命中当成上下文丢失。

验收分为启动/简单工具、连续多任务、多子代理、长上下文与慢模型组合。普通真实模型测试使用官方 MiniMax-M2.7；协议兼容测试按明确目标选择服务商，不静默改用户日常模型。

默认配置行为必须核对实际合并结果：旧安装若把完整默认 `system_prompt` 或工具延迟目录另存为显式
覆盖，仅升级 wheel 不会替换这些值。测试可在备份后移除测试配置中已确认是旧默认副本的字段，
不能直接覆盖用户定制提示。模型声明、界面 Goal、目标账本、任务绑定和最终工具结果分别取证。

## TUI 随 Gateway 升级原地切换与部署工具适配器重启（2026-09-25）

- 第一版（退出界面再 exec）真机验证时只核对了进程路径，漏掉两处：退出会闪回 shell；原命令行不带会话编号，`_setup_session` 会建新会话，界面回来是空对话。改为原地切换并用 `MY_AGENT_TUI_HANDOFF` 传会话与原始终端设置。
- `test_tui_upgrade_follow.py`：目标判定、七项空闲事实、只排到 UI 线程且两次复核、忙时撤回、exec 失败保留旧界面、会话与 termios 经环境变量传递、新进程暂存启动输出、未接管终端退出时补发复位序列、畸形或外来载荷忽略、Windows 只提示、Gateway 状态带 `runtime_prefix`。
- `test_cli_chat.py::TestChatCommandRuntime::test_in_place_handoff_keeps_session_and_loads_history_before_first_frame`：新进程沿用会话、就绪后同步读历史、跳过可见连接流程。
- 真实验收方法：在 tmux 里的 shell 中用新版 runtime 起一个空闲 TUI，再部署同一提交的下一版切 Gateway；每 0.1 秒记录进程路径变化与“正在切换”提示消失的时间，核对画面没出现退出横幅/shell 提示符、会话记录数不变、切换期间 send-keys 的字符出现在新界面输入框；最后退出 TUI，用 `stty -a` 核对 icanon/echo 已还原。
- 第二版真机（隔离 Gateway 127.0.0.1:8431、临时 home、两份同提交 runtime）首轮发现两处：新进程先把 stdout 换成缓冲再判断终端，误入 plain 模式卡在 `input()`，切换中键入的字符被它吞掉；启动器的启动页会清屏。已修并补回归（`test_in_place_handoff_detects_the_terminal_before_holding_output`、`test_boot_frame_is_skipped_during_an_in_place_handoff`）；同轮已确认：783 次采样全屏从未退出、无退出横幅、同一进程换到新 runtime。
- 部署工具（仓库外）：同机有 IM 适配器在跑时，先比对它实际加载的 agent_py_agent 模块在新旧安装间是否有变化，没变就不重启（IM 完全无感），变了才重启并核对同版。

## 未知执行轮的会话内恢复 `/recover`（2026-09-25）

- 真实触发：用户会话里模型在回合中执行 `my-agent gateway restart`，Gateway 自杀；启动恢复把该回合 run/attempt 记为 unknown，
  自动续跑报 `ACTIVE_TURN_OUTCOME_UNCERTAIN`，此后每条新消息续同一 active 工作任务都在 `create_attempt` 被拒，没有任何用户出口。
- `test_turn_recovery_control.py`：处置值只认结构化取值；`/recover` 只读列出未确认操作（已成功的不列）且不改库；
  unknown 挂载抛 `RuntimeRecoveryRequiredError`（`RUN_RECOVERY_REQUIRED`，仍是 `RuntimeConflictError`）且客户端文案指向 `/recover`；
  `/recover recorded` 后 attempt=recovered、run=created、事件带 operator 与处置、下一次 `create_attempt` 成功、再执行显示无需恢复；
  无 thread/无阻塞/非 unknown attempt 的阻塞分别返回无需恢复或 `RUN_RECOVERY_REJECTED`；Gateway 按已认证 scope 解析 thread 后分派；
  TUI 序列化与本地模式拒绝。
- 真实验收方法：部署后对卡住的会话先发 `/recover` 核对列出的工具与开始时间，再发一个处置值；随后发一条普通消息，
  核对该请求 done、运行库新 attempt 的 metadata 带 `recovered_from_attempt_id`，旧 attempt 的未确认操作没有被重做。
- 真实验收（2026-09-25，`62b3329cf`，双机 `runtime-step11z-cb3cb7a7`，用户批准对其卡住的会话执行）：经 Gateway `/control`
  以该会话身份发 `/recover`，只列出 1 条 `run_command`（执行中断，开始时间与卡住那一轮一致），运行库未变；外部事实核实为那次
  Gateway 确实已重启后发 `/recover recorded`，attempt=recovered、run=created、`attempt_recovered` 事件的 operator 与处置正确。
  用户 12:06 发下一条消息后：同一 agent run 开出 generation 2，metadata 带 `recovered_from_attempt_id`，旧 attempt 那条
  EXECUTING 操作在换代时转为 UNKNOWN、没有被重放；新一轮 12 条工具操作后 12:12 done，请求进入 done，期间 Gateway 进程号未变。

## 审计 `requests` 主题（2026-09-26）

- 背景：用户说“这种东西我希望以后是我的 my-agent 能帮我解决”——飞书没绑定管理员就发消息，全部 `MODEL_NOT_CONFIGURED`，当时靠开发者翻请求文件定位。
- `test_audit_requests_topic.py`：本人范围只看自己的请求，宿主写入的 `owner_id` 优先、旧记录按会话归属，同一请求两份去重，
  30 小时前的记录不进 24 小时窗口，输出不含 prompt/回复/用户文案；`all_owners` 未开许可被拒，开许可后含未归属记录与错误码计数，
  管理员附带密码已设与已绑定私聊（普通用户看不到）；`current_thread` 按会话过滤；不在 Gateway 回合里报告不可用；
  响应 `owner_id` 就是执行 owner 的规范编号。
- 真实验收方法：部署后在 TUI 里问 my-agent“我刚才在飞书发消息报错了，帮我查一下原因”，核对它调用 `audit_records`
  （topic=requests），在需要查飞书用户时先请你允许跨用户审计，再说出 `MODEL_NOT_CONFIGURED` 与“先在私聊发 /admin”的结论。
- 真实验收（2026-09-26，隔离 8432、真实模型、管理员已开跨用户审计）：第一轮暴露两处问题——未绑定飞书私聊的失败回复没带 `/admin`
  指引（执行 agent 是按用户隔离的，owner 字段被改写，原判定不成立），my-agent 只查本人范围后转去翻文件、结论只提 `/model`。
  修正后：失败回复带上 `/admin` 指引；my-agent 先查本人范围、按软提示改查 `all_owners`，两轮工具给出“2 条 MODEL_NOT_CONFIGURED、
  已设密码但未绑定、在飞书私聊发 /admin <密码>”。`test_admin_identity_gateway.py` 增加按用户隔离 agent 的指引用例，
  `test_audit_requests_topic.py` 增加软提示用例。

## Gateway 安全重启第一期（2026-09-26）

- 背景：用户要求代理能自己重启 Gateway 且 TUI/IM 不出事；此前代理在回合里执行 `gateway restart` 会切断自己，会话卡在 unknown。
- `test_restart_gate.py`：关口打开时准入并计数；关闭时工具停在领取前、重开后才开跑；等待中被中断则不准入，协调器返回
  not_started 的 `CANCELLED`（`action=gateway_restart_drain`）且不执行工具；执行中计数的等待可超时也可成功。
- `test_gateway_safe_restart.py`：同一目标的重复请求合并；排空完成后冷却拒绝、冷却 0 不限；同一会话 10 分钟 3 次后防循环、换会话或过窗放行；
  第一段等在飞回合、第二段等执行中工具，成功后关口保持关闭；第一段超时继续、第二段超时取消并重开关口；完成标记只在旧进程退出后消费一次，
  过期标记丢弃；续跑通知只写到数据根内的发起会话、按请求编号去重；`planned_restart` 恢复不加延迟、原因为 `gateway_safe_restart`、排在新请求前；
  服务循环排空返回重启报告、状态投影阶段；排空超时撤销请求并通知发起会话；`planned_restart` 分类为 stopped 不记失败；
  接班命令带 `--after-pid`、托管时返回 75 不拉进程；`/restart` 非管理员拒绝、管理员写入指向本进程的请求并合并重复。
- 第二批（同在 `test_gateway_safe_restart.py`）：排空时派发只给待处理请求写 `admission_wait_reason=gateway_restart_draining`、不认领；
  终端 `gateway restart` 在 Gateway 未运行时交回启动路径、在托管自己的工具进程里拒绝且不写请求、等到新进程号 running 返回 0、
  本请求被取消或冷却中返回 2；`test_gateway_commands.py` 的先停后起用例改为显式 `--force`；`test_tui_upgrade_follow.py` 核对
  排空阶段每次轮询都提示、cancelled/缺失不提示。
- 第三批（TUI 续跑边界与确认框作废）：`test_gateway_safe_restart.py::test_resumed_claim_writes_one_turn_resumed_boundary_before_new_output`
  从真实恢复标记出发（`planned_restart` 重排、接班认领同一请求号），核对 chunk 流只多出一条 `turn_resumed`、字段只有 t/kind/cause、
  排在续跑代次任何输出之前；普通请求不写，认领时已被停止的请求不执行也不写。`test_gateway_streaming.py` 核对边界先刷出缓冲进度、cause 去空白。
  `test_tui_runtime.py` 三组：旧确认框本地作废且不经 sink 写回；同轮同序号的新代工具卡用 `:resume1` 块号、新确认正常弹出并只写回一次；
  已终态的卡不重发终态（没有 `TERMINAL_*` 诊断）；旧回复、旧思考和进行中的 Compact 按 interrupted 冻结，参数临时行收起，续跑文本另起新块；
  提示文案只按 cause 选择，未知或空 cause 用通用提示，回合结束后再收到边界不发布。`test_tui_stateful.py` 状态机新增续跑规则，
  随机交错下块号仍唯一、旧卡只中断一次、新卡正常完成。`test_gateway_client.py` 用返回 False 的旧版 typed 消费者核对该行不显示、不报错；
  `test_gateway_verbose_progress.py` 核对 IM `/progress` 忽略它并照常前移游标。变异核对：新 TUI 用例在旧 adapter 上全部失败；
  去掉块号代次后缀、终态不移出未终态登记、去掉 Gateway 写入调用，各有用例失败。真实 TUI 验收未做：需在隔离 Gateway 上让回合停在确认框
  或长命令时安全重启，核对旧框关闭、提示出现、新确认能弹出、旧工具卡显示已中断。
- `test_gateway_restart_tool.py`：不在 Gateway 内拒绝且不写文件；Gateway 内立即返回 scheduled 并记录发起会话存储根；缺原因与冷却为 not_started；
  只注册给管理员主代理且可关闭；`test_user_config_owner_scope.py` 另核对普通用户与群看不到它。
- 真实验收方法：隔离 home 与 127.0.0.1:8432 的 Gateway 上，一次 prompt 让代理重启 Gateway，核对工具回执 scheduled、回合正常结束、
  Gateway 换了新进程号、发起会话收到“重启已完成”后没有再次重启；另开一个会话跑长命令时发 `/restart`，核对等它跑完才换进程、
  回合续跑完成、没有 unknown；重启期间发的消息在新进程里照常处理且只回复一次。
- 真实验收（2026-09-26，`07fa00fb3`，`runtime-step12b-3d81454b`，隔离 home、127.0.0.1:8432、管理员审批模式 full-access，真实模型）：
  A 轮一次 prompt“请安全重启一下 Gateway”：代理只调用一次 `restart_gateway`，本轮 13.8 秒 done；第一段等到发起回合自己结束，
  第二段无执行中工具，旧 10087 → 新 11073，停顿不到 1 秒；续跑唤醒写入 1 条并被处理，续跑回合不调工具、直接告诉用户重启完成，
  全程只有一次 `gateway_restart_requested`。B 轮会话在跑 45 秒命令时由另一会话发 `/restart`：第一段一直等到回合结束（52 秒）才换进程，
  回合在旧进程里 done（重排 0）。C2 轮第一段上限临时设 5 秒，回合先跑 20 秒写文件命令：第二段 `executing_tools=1`，等命令跑完才换进程，
  新进程立即续跑同一回合（重排 1、无 10 秒延迟）执行后两条命令并 done，文件里 step-one、step-two 各一次，副作用命令没有重跑。
  C 轮同样设置但第一条是只读命令（`sleep 20 && echo`）：只读命令不经过关口，进程在它跑到一半时退出，续跑时重跑了这条只读命令，结果正确，
  只是多花了时间。证据在 `~/.my-agent/releases/step12b-3d81454b/acceptance-safe-restart/`（不含模型目录，隔离 home 已删除）。

## 托管自停闸与聊天 `/model`（2026-09-25）

- 背景同上一节：模型在回合里重启了托管自己的 Gateway；飞书用户是另一个 owner，没有模型，飞书里发 `/model` 只得到“不支持的系统命令”。
- `test_gateway_host_guard.py`：Gateway 进程写入的托管进程号经 `_subprocess_text_env` 传给子进程，降权擦洗后仍保留；
  只有目标进程号与托管进程号完全相同才拒绝，缺失、坏值、别的 Gateway 都放行；`gateway stop`/`restart`/`start --force`
  拒绝时返回 2，且没有写停止请求、没有 kill、没有启动新进程；工具里停别的 Gateway 照常成功。conftest 清掉该变量，避免在托管工具里跑测试时串宿主。
- `test_model_text_control.py`：`/model`、`/model <编号>`、`/model default <编号>` 解析为结构化操作，多余正文无效；
  没有模型时返回“请管理员共享”的引导而不是不支持；管理员共享一个模型后 IM 用户看到它（不含管理员私有模型、密钥和接口地址），
  选中后本会话生效、新会话默认不变，再设默认后才变；越界编号拒绝；两个 IM 用户的会话选择互不影响；TUI 单独 `/model` 仍留给本地菜单。
- 真实验收方法：隔离 home 与 127.0.0.1:8431 的 Gateway 上，一次 prompt 让代理在对话里执行该 Gateway 的 `gateway restart`，
  核对工具结果为拒绝、回合正常结束、Gateway 进程号不变；再以 IM 身份经 Gateway `/ask` 发 `/model`、`/model 1` 和一条普通消息，
  核对列表、会话选择和普通消息使用所选共享模型完成。
- 真实验收（2026-09-25，`b3c86a697`，`runtime-step12a-13e1fc9e`，隔离 home、127.0.0.1:8431，管理员 full-access）：
  一次 prompt 让代理用 `run_command` 执行这台隔离 Gateway 的 `gateway restart`。工具记为 FAILED、退出码 2，拒绝文案原样回到回复里，
  回合 done/completed；Gateway 进程 12:23:16 启动后一直未变，日志没有任何停止事件。从普通终端执行同一 Gateway 的 `gateway stop` 照常成功。
  以飞书身份经 `/ask`（与适配器同一载荷与身份头）：共享前普通消息报 `MODEL_NOT_CONFIGURED`、文案引导发 `/model`，`/model` 提示请管理员共享；
  管理员经 `/client/models` 共享默认模型后，`/model` 列出 1 个带“管理员共享”的模型且不含接口地址，`/model 1` 选中，
  普通消息用该模型完成，再发 `/model` 显示当前会话模型。复制的模型目录随隔离 home 删除，证据在仓库外。

## IM 管理员身份与聊天内审批（2026-09-26 合入 main）

- 背景：管理员以前只有本机 local/main，飞书用户永远是自己的 owner，IM 客户端也无法确认工具。用户决定用管理员密码在飞书
  私聊里绑定管理员身份，并用密码批准工具。设计见 [IM 管理员身份](docs/design/ADMIN_CHANNEL_IDENTITY.md)。
- `test_admin_identity_store.py`：
  - 密码文件只有 scrypt 参数、盐和派生值；文件 0600、目录 0700；任何文件里都没有明文；重新设置会换盐。
  - 过短、首尾空白、含换行或制表符的密码被拒，不写文件。
  - 同一身份 10 分钟内错 5 次锁 10 分钟：锁定期内正确密码也拒绝，别的身份不受影响，到期后恢复，成功后计数清零；
    拒绝文案只有通用句子和剩余分钟数。窗口外的失败不累计。
  - 未设置或损坏的密码文件一律验证不通过；失败记录损坏时拒绝，不放行。
  - 绑定只按 `(channel, user_id)` 精确匹配，渠道名不分大小写、用户 ID 区分大小写；重复绑定不产生重复行；绑定文件损坏时
    查询 fail-closed、写入不覆盖。
  - CLI：两次输入不一致或密码过短时返回 2 且不写文件；status/list 输出不含散列、盐和明文；非 local/main 配置拒绝且不提示输入；
    命令组缺子命令时打印帮助，不落到默认聊天入口。
- `test_admin_identity_gateway.py`：
  - 绑定后，请求和控制作用域都解析为 local/main；群聊、缺私聊类型、其他用户、开关关闭、base owner 不是 local/main 时不变；
    解除绑定后恢复。
  - 真实 `handle_ask` 处理 `/admin <密码>`：回执 `command_text` 为 `/admin ******`，整个临时目录没有明文，不写请求队列；
    同一消息重投只重放原回执。
  - 错误密码、群聊（提示撤回并更换密码）、本机终端、开关关闭都被拒；`/admin status` 与 `/admin logout` 正常。
  - 服务端只对已绑定的管理员私聊、且执行 owner 为本机管理员时开启交互审批；显式声明能力的 TUI 不变。
  - `/progress` 的审批事件只有 `kind/tool/summary`，外部渠道收敛宿主路径；IM 渲染出 `/approve` 与 `/deny` 提示。
  - 真实 `BufferedChunkStreamWriter` 等待审批：错误密码不产生决定；正确的 `/approve` 经控制回执（正文为 `/approve ******`）
    让等待方得到 `approved`，再批准返回 `APPROVAL_NOT_PENDING`。`/deny` 不要密码，得到 `denied`；未绑定时 `/approve`
    返回 `ADMIN_IDENTITY_NOT_BOUND`，而且不校验密码。
  - 以下情形都拒绝，且不写决定文件：没有待决、同一回合两条待决（`APPROVAL_AMBIGUOUS`）、其他会话或其他用户、
    本次认领之前的旧执行事件。
- `test_admin_identity_gateway.py` 末尾两例（2026-09-26）：未绑定的管理员 IM 私聊遇到 `MODEL_NOT_CONFIGURED` 时回复追加
  `/admin <管理员密码>` 指引，其它错误码、群聊、开关关闭、未设密码、已绑定都不追加，且只看本私聊自己的绑定；`/model` 只在没有可选模型时追加。
- `test_admin_identity_clients.py`：
  - 适配器：`/admin`、`/approve`、`/deny` 不写持久入站记录，也不建回复 watcher，只直接 POST 一次并回复 Gateway 结果；
    Gateway 不可达时只回“服务暂时不可用”，不重试；普通消息照常入持久队列。临时目录里没有明文。
  - TUI 与终端：在写控制 outbox 与发 Gateway 之前本地拒绝；拒绝文案不含密码；这三条命令不写输入历史。
- 回归：本地严格 gate 共 54 个测试文件（3 个新增，加所涉模块既有测试与架构守卫）1468 passed、1 skipped；
  ruff、doc sync、strict code-size、diff check、clean package 均通过。
- 真实流程验收（2026-09-26，`0bbe68d55`，隔离 home、127.0.0.1:8432、真实模型；飞书侧以与适配器相同的 `/ask` 载荷与身份头模拟私聊/群聊）：
  私聊 `/admin status` 显示未绑定；群聊 `/admin <密码>` 返回 `ADMIN_IDENTITY_SCOPE_INVALID` 并提醒撤回；私聊绑定成功、status 显示绑定时间。
  注意：工作目录内 `write_file` 属于 mutating，默认确认模式本就不弹审批，不能用来测审批。改用 `restart_gateway`（dangerous）：
  `/progress` 出现 `permission_requested{tool: restart_gateway}`，`/approve <密码>` 后本轮 done、Gateway 换进程（85871 → 87163）；
  同样请求再发 `/deny`，工具未运行（`APPROVAL_REJECTED`）、进程号不变，代理没有重试。另一身份连错 5 次后锁 10 分钟，锁定期内正确密码也拒且文案不区分原因。
  绑定身份发 `/restart` 成功换进程（77505 → 85871），`/admin logout` 后 `/restart` 返回 `GATEWAY_RESTART_ADMIN_ONLY`。
  扫描隔离 home 全部 431 个文件，测试密码明文零命中；控制回执为 `/admin ******`、`/approve ******`。
  证据在 `~/.my-agent/releases/admin-identity-acceptance-20260926/`（隔离 home 与模型目录副本已删除）。真实飞书客户端上的验收待部署后由用户操作。

## 场景框架 Gateway 进程清理（2026-09-27）

`cli/scenario_utils.run_scenario_gateway_ask` 把 Gateway 生命周期闭合在函数内：`gateway start --force` 一经发出，无论就绪超时（exit 2）、ask 失败、
start 子进程超时还是其它异常，`finally` 都调用 `stop_scenario_gateway`——先走正式 `gateway stop --kill`，再按场景 Gateway 工作区 pid 记录核对，
进程仍活着就按 pid 升级终止（SIGTERM 后仍存活再 SIGKILL，只动这一个 pid）；停止事实以结构化字段 `gateway_stop`（pid、stop_returncode、terminated_by_pid、alive_after_stop）附在返回值里并进入
场景 summary。之前就绪超时会直接返回 `gateway start failed` 而不停进程，全仓分片高负载时残留后台 Gateway。就绪等待改为随 `--timeout` 换算的
10-60 秒（显式 `--ready-timeout`），避免默认 3 秒把“还在起来”报成启动失败。
回归：`test_scenario_utils.py` 用假的子进程入口模拟“就绪超时但进程已经起来”“start 子进程超时抛错”两条路径，断言 stop 被调用、按 pid 兜底后进程不再存活；
只断言返回值里有 error 不算覆盖。

## 提交前严格 gate

- **线上 CI runner 与 bwrap（2026-09-25）**：Actions 重新启用后 Test 工作流自 7 月以来一直失败，根因是 ubuntu-24.04 runner 预装 bwrap 但 AppArmor 禁止非特权用户命名空间，sandbox 自检 `BWRAP_ISOLATION_FAILED`（setting up uid map: permission denied）→ 全部 `run_command` 用例按设计 fail-closed。两个工作流增加
  “Prepare bubblewrap sandbox on the runner”步骤：`sysctl kernel.apparmor_restrict_unprivileged_userns=0` 并复核自检；产品代码与测试都不绕过沙箱。线上 CI 仍不作为验收来源。
  放开后首轮 3.12 暴露 7 项失败并分类：goal 续跑 prompt 英文断言过时（改结构标记）、随包 bwrap 二进制名 `bwrap.linux-x86_64`（断言前缀）、
  原生 IR 窗口测试前提被余量外置抵消（该测试显式关掉 `tool_output_externalize_on_low_headroom`）、merged-/usr 下 `/bin`→`/usr/bin` 绕过根级目录排除（产品修，`_uninheritable_root_forms`），
  以及两项只在 runner 上出现、本机通过的用例（`test_decision_fault_matrix[dns]`、`test_host_command_stream` 断连取消）待各线复查。
  后续：dns 项由决策线修正（其 Mac 的 HTTP_PROXY 掩盖了解析路径，runner 才是对的）；断连取消项根因是 `execute_host_command` 在 attempt_executor 退出事实之后才写
  未启动回执，并发 `query_host_command` 在窗口里投影成 outcome_unknown。回执改到执行器登记仍为 running 时写入，回归
  `test_host_command_execution.py::test_unstarted_receipt_is_written_before_executor_exit_fact`（去掉修复即失败）。
  第二轮 CI 发现回执不能早于连接清理：`test_plugin_invocation.py::test_connection_cleanup_finishes_before_host_attempt_closes[denied]` 要求资源释放期间 attempt 仍 running；
  现按“ExitStack 资源清理 → 未启动回执 → attempt_executor 退出事实”的顺序写入，两条合同同时成立。
  Full Tests 另暴露 `test_shell_orphan_kill.py::test_foreground_timeout_cleans_group_after_leader_exits[False]`：宽限期内已证明消失的后代 PID 在最终核对前被复用，
  `os.kill` 探测重新成功而出生标识读不到，被记回 unresolved。终止回执改为记住已证明消失的进程实例（同号 PID 换出生标识才重新纳入），
  回归 `test_termination_receipt_keeps_proven_dead_pid_resolved_after_pid_reuse`。
  沙箱真跑之后 fast suite 单 job 实测 40–45 分钟，45 分钟预算在 8cd7d01d0 的运行里被顶满整体取消；test.yml 预算放到 60 分钟。
  结果（2026-09-25 13:3xZ，main `88631648e`，run 36137357520）：test (3.11) 41 分钟 success、test (3.12) 44 分钟 success——7 月以来首次全绿的 fast suite，
  且 run_command 用例真在 bwrap 里执行；test (3.10) 在 45 分钟预算处被取消（非用例失败，60 分钟预算已在 6db8ef403）。随后 Actions 被账单/额度挡住，
  新 run 3 秒内以 billing 注解失败；线上 CI 复验待用户处理 Billing 后进行，本地严格 gate 仍是唯一验收来源。

```bash
python3 -m pytest <直接相关测试文件> -q --tb=short
ruff check agent_py_agent scripts
python3 scripts/check_doc_sync.py
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
git diff --check
python3 scripts/check_clean_package.py .
```

默认 focused tests。生产代码与测试代码累计增删约 10,000 行或明确另有要求时追加全仓 pytest；文档清理不算实施代码变动。线上 CI 未运行时如实说明，不替代本地严格 gate。

## 发布资料清理验证

注释与示例清理要比较生产 Python AST、默认配置值、协议与依赖标识。允许的人类展示字符串变化需单列；构建包检查 LICENSE/NOTICE、vendor 许可和不含秘密数据。历史重写须先备份、只改授权引用、带 lease 更新，验证发布树不变。

<!-- 媒体来源片 3adb61904 的既有记录；不代表当前 Compact 集成已验。 -->
## 真实开发长任务验收方法

用户明确要求长对话验收使用真实项目开发过程，禁止把重复生成的大行数当作真实任务通过依据。
当前选择让官网 MiniMax-M2.7 的 my-agent 在原生 TUI 中把 GitHub `sharkdp/fd` 从 Rust 复刻为 Python，
自行读源码、实现、运行测试、修复并提交项目产物。测试者只提交一次普通中文需求并观察，不能代写或补交产物。
记录自然产生的模型/工具回合、Compact、TUI 状态、CPU/RSS、退出/恢复与任务结果；未实际发生的长历史边界不计为通过。
下文合成一万/千万行记录只作为存储边界和缺陷复现，不代表此类真实开发工作负载。当前真实开发验收待完成。

## 官网真模型与TUI媒体验收

2026-09-23，独立候选线，专用测试机限制为 1 CPU / 2 GiB；官网直连，不通过中转，不使用假模型作为本轮验收。

- 官网 MiniMax-M2.7：100 独立用户身份各发送一次中文普通请求，100/100 terminal=done；处理槽峰值 50。
  同时一个原生 TUI 发问并正确回答 `5+6=11`。204.3 秒完成队列，44 个实拍终端帧未见“未同步/刷新失败”。
  cgroup 峰值 1047.1 MiB（含文件缓存），末次采样 Gateway RSS 313.8 MiB、TUI RSS 62.6 MiB。
  真实 `/status` 全部成功，但高峰 P95 3268 ms、最大 4897 ms；单核批量冷启动仍有排队和刷新延迟。
- 官网 MiniMax-M3：实际端点 `https://api.minimax.cn/anthropic/v1/messages`，现有私有 key 短请求确认返回 M3。
  原生 TUI `/attach` 添加 PNG，正确识别红圆、蓝方、绿三角及 `Q7N4`；终端 bracketed paste 拖入 MP4，
  正确识别红→蓝→绿和 1→2→3。测试提问未提供答案；未代模型执行视觉工具。
- 私有只读请求观察器确认真正外发 image/png 6484 字节与 video/mp4 5774 字节，SHA256 与素材一致；
  观察器调用原 HTTP 函数，不替换供应商、不改变请求/响应。模型工具轮为零。
- 真正 `/exit` 后重新启动同一会话，问图片和视频背景，M3 正确回答白色；请求再次带相同原件字节。
- macOS 隔离 Gateway + 原生 TUI，系统图片剪贴板经 Ctrl+V 成为附件，官网 M3 正确识别同图；原剪贴板完整恢复，
  本机隔离测试 TUI/Gateway 已退出。无改动用户日常模型/默认 Gateway。
- `input_media_max_bytes=16 MiB` 同时限制新输入和一次供应商请求的媒体展开；新近附件完整、超预算旧附件明确
  投影为归档引用，canonical refs 和原件不删除。owner 越界、符号链接、同长度内容变更、总量/数量超限均有合同验证。
- 相关组件矩阵当前为 1008 passed、1 skipped；跳过项仍为原 HTTP stop fixture 的 409，自行 skip 不计入通过。
  单测只验证协议/资源/输入边界，真实可用结论来自上述官网模型与原生 TUI。
- 无 checkpoint 的一万行历史：真实 TUI 续聊完成，自动压缩 generation=1 后正确回答 `4+4=8`。
  终态用时 410.81 秒；账本记录官网 M2.7 的 4 次供应商调用均 finished、0 retry，输入 294653 / 输出 1621 token。
  该用时不能算低延迟通过，也不能仅凭单次采样栈归因给 Compact 二分预算估算。

**未通过边界**：千万行浏览成功不等于千万行任意状态续聊成功。对 10,000,000 行、约 2.43 GB、
无 Compact byte checkpoint 的历史，隔离只读子进程在 384 MiB 地址空间上限下调用 `after_compact_report`
立即产生 `MemoryError`，还没有发起模型请求。`append_once` 的全量去重读取也需后续治理。
相关有界读取、分批 Compact 必须与另一开发线正在修改的 scope/checkpoint/CAS 合同合并验收。
本轮不声称无限时长、任意历史规模、100 个重工具或真实 IM 平台账号已通过。

证据保存在仓库外 `tui-real-media-20260923/`：`real-model/` 的 submissions/terminals/samples，
`media-*-tui.txt`、`media-provider-requests.jsonl`、`real-10k-history-*`、`uncompacted-10m-read.json`、本机截图粘贴验收。
旧假模型记录仍保留用于定位，不作为本轮通过依据。未推送、未替换用户默认环境。

## 能力包内化首片（2026-09-25，真实验收中）

完整计划与分层证据见 [能力包验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md)，
当前执行见[唯一 Goal](docs/tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。
已运行主链组合：`test_capability_package_task_refs.py`、`test_subagent_skill_inheritance.py`、
`test_scheduler_tool.py`、`test_scheduler_service.py`、`test_scheduler_repository.py`、
`test_conversation_store.py`、`test_resolve_capability_requests_tool.py`，114项通过。
覆盖原安装生成引用、主任务原锁 pin／重启读回／并发与换代、子代理隔离、层级继承、后授予接续、调度旧引用拒绝。
包生命周期及发现线的定向结果另在完成时合并记录，不能把交叉重复测试累加成独立用例。
这是组件测试，未启动真实模型／Gateway／原生 TUI，不能据此关闭真实验收。

能力包分线结果（均为本地组件，不与前项去重后累计）：生命周期16文件339项通过，包含v1—v6相邻回归；
独立发现、分页与旧Skill／decision组合111项通过；三样包构建与私有脚本39项通过；主链引用加固补充两文件11项通过。
架构与合同层级两个守卫文件13项通过。Ruff、doc sync、strict code-size通过；
clean-package 初跑因本轮新文件尚未纳入Git而拒绝，加入跟踪后复验通过。
样包只验证声明范围：A文本改编／引用，B跨表连续性／静态报告，C已授权证据整理；未迁移整套媒体生成或攻击执行。

后续联合 gate：44 个相关测试文件 858 项通过，无新增跳过；覆盖上述生命周期、旧插件、发现、主子任务、调度、精确写入、文件权限、配额、幂等、原生循环与相邻 Compact。
`test_capability_package_compact.py`、`test_prompt_scope_failure.py` 与原 runtime_guidance/tool_runtime_scope/model_scope_dependencies
五文件组合 97 passed、4 项既有 xfail；其替身仅代替模型传输，不代替原 Skill/安装/TaskStore 权威。
`test_capability_package_catalog_scale.py` 四种规模均通过，合成最多 1000 包、201000 私有成员；公开 Skill 不增长，发现阶段不读取正文，实际元数据字节和目录耗时存为测试属性。
完整原生替身 get→write_file.source_ref 验证了真实 provider 消息、任务晋升和原操作回执；与真实 TUI 证据分开。
失效主任务包隔离补片的13文件组合180项通过：保留旧pin、剔除同ID新代、有界诊断、空展示选择、混合包可用、主任务正常收口；坏身份/引用、child与scheduler继续严格。
真实Compact恢复前换代现在只拒绝这个包的后续读取，不阻止模型回复；本文先前“换代拒绝”指拒绝新内容，不再指主任务整轮失败。
补片后主线20文件230项通过（24.69秒）；Ruff、doc sync、strict code-size（hard=0，blocked=False）、diff check、clean-package均通过。
首候选本地严格 gate 已通过；尚未推送，线上 CI 没有作为验收来源。

首轮真实验收固定源码 `36439d633`，六个原生 TUI 共用一个私有 Gateway，均按官方 MiniMax-M2.7 的实际配置及调用账核对。
CAP05 从原管理命令安装启用三个 `0.1.0` 内容包，原激活记录不含运行环境/计划，未创建插件环境；Gateway 身份未变。
CAP01 只读冒烟通过（2次模型请求，8.206秒）；CAP06 写入再读回通过（3次，10.029秒），全新会话无关算术负例通过（1次，4.118秒）。
CAP02/B01 与 CAP03/C01 均为全新会话、一次冻结中文需求，分别6次/58.858秒、2次/18.037秒；实际 `skill_search` 调用为0、任务包引用为空，自然召回未通过。
B01 实际交付六份文件，关系和连续性通过，核对报告的原有图片与新增建议来源区分有偏差；C01 保留来源、隔离资产且没有发起新检查，但归并交付与来源表述有缺口。
CAP01/A01 同样未读包；该席先有只读冒烟历史，不计入全新会话成绩。原 TUI 不支持 `/new`，准备阶段该命令在本地被拒绝；新会话须正常退出后重开。
包资源/脚本读取因此属于未覆盖，不能据此断言底层读取失败；也不能把普通模型交付改报为内化通过。长任务、显式选择和可见索引正在分别核验。
完整真实编号、输入和工具原账保存在仓库外，产品代码不包含用例专用分支，未补提示或代做业务产物。

CAP04 长任务原四孩子均完成，主任务自然 completed；独立核验七份文件、八集/43镜头/600秒、输入未变。
主子共27次官方模型请求、零重试；所有包引用/读取均为空，Compact为0，不能把普通协作通过当成包继承或压缩通过。
内容仍有计数、因果解释、交接资料等六项问题，完整业务质量未通过。
CAP02/E02 显式包对照85.995秒/12次HTTP，读取包及原TaskLink绑定通过；脚本原样复用失败。
完整工具归档9668字符、source_ref在9138处；模型只读前6000字符且未续读，随后手写脚本删弱后半检查。
实际字节摘要与原包不同、写入回执无source_ref；不能把改写脚本的exit0计作原包校验成功。未代跑原脚本或补产物。
CAP06 在全新开发诊断会话使用原 `/show-prompt` 执行同一A01：三包摘要确实可见，默认展示selected=None，仍未读包；
此前缺的是包级采用指引，并非包目录未安装。诊断轮只说明该请求事实，不补成此前请求的wire证据。

修订片补包使用软规则和大输出下原样引用可见性；不新增执行器、状态账、强制领域路由或权限。
大资源原生循环用例先3失败/1通过（缺source_ref或预览截断JSON），修后0/200/1800字符预览均能从真实native tool_result取引用、逐字节复制整个含CRLF的资源及末尾不变量。
与发现、Compact、任务引用、来源权限、旧write_file和工具结果投影七文件组合114项通过；分线用例有重叠，不累计为独立总数。
通用软规则已有零包旧提示逐字节不变及大目录/空选择验证；修订后真实自然召回与原脚本执行尚待验收。
修订候选最终15文件274项通过，无失败或跳过；Ruff、doc sync、strict code-size、diff、clean-package与新增行隐私检查通过。
新1/10/100/1000包目录分别4075/4378/4379/4380字节，包含一次通用使用规则，私有成员仍不展开；这是组件目录预算结果，不是模型召回率。
此轮没有推送，线上CI未作为验收来源；六席正常退出后仅切换私有验收运行时，原失败和包安装代次保留。

第二候选 `f7d9a6caa77a` 的新会话复验：A01/B01/C01/L01仍零读包、主子引用为空；E02一次错误skill_id后放弃。
五用例均为原冻结中文提示、无追加指导；基础安装事实保持，不能据此宣布自然召回解决。
A01 4次HTTP/58.410秒，B01 3次/32.832秒，C01 3次/21.665秒，E02 4次/38.798秒；均由实际模型账确认官网M2.7。
B的制作资料及C的证据归并通过，A的计数、因果和核对声明有问题；普通业务与能力包采用分开记账。
L01主及四孩子共32次HTTP、零重试，canonical completed且无活动任务；七文件中三份最终JSON不能解析，未发生Compact。
第二次全新 `/show-prompt` 开发诊断实际含三包摘要和使用规则；该轮错误后能检索并读包，但没有执行原脚本。
同配置只读快照在六工作区均为三包/27公开Skill/零错误；这只是provider投影与合成prompt证据，不冒充wire抓包。
安装版原生出站前12组零网络序列化检查未发现包提示丢失；不将它当作上述旧请求的HTTP抓包。
显式RESOURCE-COPY-A对照47.199秒/9次HTTP，真实native结果保留完整source_ref及归档提示，write_file.source_ref可见；
模型拼错归档引用后改用Shell复制源码、最终执行成功，但没有沿source_ref物化，预期链仍失败。
新结果与原失败分别保存，完整细项见[能力矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md)。

CAP05管理席真实完成B包10次写请求：9成功、1坏包按digest_mismatch/not_started拒绝，原表保持；
停用、升级、指定旧包回退、卸载、同ID重装和启用均结清，未出现UNKNOWN、replay或Gateway重启。
A/C安装行及21个已声明既有文件保持；原始/升级/回退/重装四代激活均不同。
CAP01/N04在B停用时核心CSV读取正确（300/3条，2次HTTP/5.465秒）；没有与真实管理operation区间重叠，
不能计为执行中交错。旧canonical task换代读取、全卸载无包和权限失败的真实覆盖继续待验。

第三候选开发先补准确选择器和完整代次next_read；23项定向测试通过，去掉双期望字段的控制变异3项均失败。
包级动态建议使用原scoped目录、能力配置开关和候选预算，不预读/固定引用/增加模型请求；普通入口33项新测试通过，
独立审阅发现Compact另一个调用方未传scope/selected，真实prepare入口4项反例先失败，补原宿主范围和重验选择后通过；
推荐文件共37项，与两个相邻Compact文件组合65项通过。最终24个相关文件/入口414项通过，无失败、跳过或新增xfail；
Ruff、doc sync、strict code-size、diff check、clean-package均通过。第三候选本地严格gate通过，未推送，线上CI未作为验收来源。
这些组件结果不替代自然采用、原生资源物化和同任务换代的真实TUI验收。

### 第三轮真实 TUI（固定候选 `976690423`）

CAP01/A01、CAP02/B01、CAP03/C01、CAP04/L01及CAP06/E02均为全新会话、一次原冻结中文需求；
共用单个隔离Gateway，按实际profile的官方endpoint和逐线程usage联合核对MiniMax-M2.7，测试者未补提示、代执行业务或修改交付。
旧两轮失败原样保留，本轮入口读取、私有方法、原脚本执行和业务质量分别判定；有限单次观察不代表完整保留集通过。

| TUI／用例 | HTTP次数／耗时 | 包采用与资源使用 | 独立业务核验 |
| --- | --- | --- | --- |
| CAP01／A01 | 5／79.232秒 | 自然4次get；3次虚拟资源read_file失败后自行恢复；未物化或执行原checker | 来源覆盖、JSON解析成立，但file-v1摘要误作字节SHA，4个镜头缺正数seconds，完整质量失败 |
| CAP02／B01 | 9／91.507秒 | 自然4次get；3次虚拟路径失败后自行读取方法和示例；未用原脚本 | 制作资料关系、75秒、媒体计划状态基本通过；核对汇总3/6与明细5通过1警告不一致，质量警告 |
| CAP03／C01 | 4／36.602秒 | 自然入口get；2次方法路径read_file失败后未恢复，私有方法未读 | 保留来源、隔离资产、未启动新检查，但同资产同内容证据未合并，4组代替3组，归并失败 |
| CAP06／E02 | 5／70.856秒 | 显式正确读取入口、workflow与review，不计自然召回；未用原checker | 核对报告通过关系、时长及媒体状态检查；只有新报告，未新建完整制作资料 |
| CAP04／L01 | 主子共27／252.965秒 | 主读A/B入口、四子继承同代授权引用；主子私有方法读取0，子任务包读取0，未物化或执行原checker | 主及四子done，8集50镜头、48/48来源ID；人物JSON无效，镜头总557秒与声明600秒矛盾，完整质量失败 |

A01的样包0.1.0字段说明/模板与原checker不一致和文件版本标识误作内容SHA分别记录；不能仅归因为模型未遵守完整说明。
C01允许聊天交付，失败判据是归并事实；L01另有最终原因表述强于原文，来源ID齐全不代表完整内容正确。
L01无孙任务、无Compact，四子done和版本继承不能替代内部方法使用、孙代理或压缩恢复验收。
这五例的source_ref物化与原checker执行均未覆盖；第二轮RESOURCE-COPY-A的资源链失败仍保留，不能用本轮入口采用补成通过。
同任务控制另有两组真实原生 TUI（CAP05 管理、CAP06 旧任务，CAP01 新任务对照）：
F01读取并固定B后停止，确认Goal暂停、attempt取消、claim结束且无资源锁，再停用B并恢复同一Goal/task/thread；
引用和主运行身份保持，但恢复后没有再次包get，直接手写旧脚本，因此撤销读取拒绝未触发，原脚本复用仍失败。
F02在首个get后立即停止，同字节包停用再启用；原Goal/task/thread/旧引用保持，恢复后三次get均以
`SKILL_SNAPSHOT_UNAVAILABLE`／`CAPABILITY_PACKAGE_NOT_AVAILABLE`拒绝，未返回新正文。
另一个全新TUI任务实际get成功并固定新的activation，归档SHA相同；两任务终态且无资源锁。
该对照通过同字节换代隔离，不扩充为资源物化撤销、读中竞态、Compact或全部故障恢复通过。

第四候选开发证据单列：包资源命名空间/同代next_search修订14项新测试先红后绿，
`test_capability_package_resource_scope.py`、发现、原生结果及选择器四文件联合57项通过；
样包A的0.1.1字段说明与模板对齐另有75项组件通过。两组不相加成统一gate，也不回填第三轮旧样包的真实结果。
这些分线结果不构成第四候选的统一严格gate或真实TUI通过凭据，本记录不宣称寻址、原脚本执行或交付质量问题已解决；未推送，线上CI未作为验收来源。

第四候选最终统一27个相关文件/入口503项通过，0失败、0跳过；Ruff、doc sync、strict code-size、diff check和clean-package通过，新增行隐私检查无命中。
clean-package初次发现新测试未跟踪，加入版本控制后复验通过，原失败日志保留；未推送，线上CI未作为验收来源。

### 第四轮真实 TUI 与独立记忆边界

固定源码`ae2b1cae1a84`、A0.1.1和原B/C完成五个原冻结输入；官方MiniMax-M2.7来源和实际usage已核对，测试者没有代执行业务。
CAP06原生get/search/归档续读→完整source_ref复制原脚本→原Shell执行与报告一致，10次HTTP/56.226秒，资源工程链通过。
CAP01也原样复制原checker并在真实检查失败后自行改产物，原检查1→0；16次HTTP/206.670秒，但场次时长、场次数与语义质量未通过。
CAP02三次写参数流被guard打断，12次HTTP/99.654秒，0成功写入、无交付；缺原provider终止原因/实际cap，不能按文案断定长度上限。
CAP03入口读取后把resource_path传给search，成功返回索引未读取方法；6次HTTP/49.515秒，正文4组与最终称3组矛盾，去重失败。
CAP04主和4孩子49次HTTP/333.6秒，共9次get并实际读取方法/模板，但最终33镜头、无效JSON、字段及引用账问题，长任务仍失败。
所有原结果与逐项证据边界见[验收矩阵](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第四候选真实-tui固定-ae2b1cae1a84)。

五例主任务实际召回前轮L01记忆，计数为5/5/1/5/1；不能以新TUI或memory_resume标志宣称空记忆对照。
保留历史和原失败，后续仅专用测试owner沿原memory-policy关闭召回，三包安装记录保持；以实际原生Related Memory和响应used_memories=0联合核验。
A02/B02/C02/C03/N05/N03六个开发用例另开新会话一次提交，结果另记；保留集仍封存。

通用search/resource_path契约：新增10项反例旧实现全红，修正后`test_capability_package_resource_scope.py`24项通过；
两文件Ruff与diff检查通过。包含未知/受限包、空路径、默认action、0快照读取/0pin及原生错误入模，不新增自动读取或身份泄漏。
此源码修正尚未进入候选4安装包，组件通过不改报旧C01的实际失败。

记忆关闭臂六例已结算：原候选/原安装代次，全部实际Related Memory无历史记录且used_memories=0，输入未改。
A02（13HTTP/98.617秒）核心双人/2场4镜/60秒及来源通过；少name导致原样复制被拒，改写副本仅删注释、AST一致，副本实检exit0，不记原字节链通过。
B02（25HTTP/180.963秒）原source_ref复制/原checker实际执行成立，但两次exit1且来源ID被改错，业务失败。
C02（6HTTP/46.538秒）引用关系正确，4处SHA从64误抄成62字符，完整业务失败；旧search/path行为导致方法未读。
C03（2HTTP/22.697秒）正确报EV99缺失，无包采用，部分结论过强；N05（2HTTP/10.565秒）和N03（1HTTP/4.834秒）普通任务通过、无无关包使用。
结束后无运行attempt/资源锁；全部官方M2.7，无测试者代执行。完整分层裁决见验收矩阵；不覆盖孙代理、Compact、新TUI同任务续接、运行依赖缺失或公共Skill实际调用。

### 原生写恢复原因分类修正（本地）

`test_native_truncated_write_recovery.py`新增真实SSE parser→原backend→原decision反例，旧实现7 failed/18 passed；
修正后整文件25 passed，与`test_backends_native_tool_use.py`、`test_response_decision_native_tool_use.py`、
`test_truncated_output_resume.py`联合74 passed。非长度的坏JSON/非对象参数/EOF/content_filter保留原错误响应且不消耗写恢复预算，
真实长度截断仍最多两次纠偏，guard保留stop_reason/turn_end_reason/usage；正常native调用、text协议与原输出恢复保持。
两文件Ruff/diff检查通过，未改provider、任务完成或持久账，未发真实模型请求；不能倒推旧B01历史原因。
原长度guard测试增加`should_continue_task=false`和原Goal wake=0断言后25项仍通过；另一次无网络主宿主重放精确3请求/0工具，
保留max-tokens不新增CLI/Goal续跑资格。旧非权威会话Compact有独立续接条件，保持原有边界，不概括为禁止任何重新运行。

### 完整来源引用模型声明（本地）

运行时要求9字段而模型只见object的缺口已修正，唯一properties同时派生required与原解析器字段集合；
core/registry成对注入原resolver和schema，filesystem深拷贝声明，宿主缺配不退回宽泛object。
新`test_capability_resource_input_schema.py`初次13 failed/6 passed，修正后加零安装包边界共20 passed；
10个相关文件联合147 passed，包含原Executor逐字段缺失零handler、准确错误位置、额外字段/类型/摘要格式、
原name/激活代次/内容摘要拒绝、完整二进制复制、provider声明传递、关闭插件及普通文件写入不变。
定向Ruff/diff通过，实际安装仍为候选4；不回填A02失败，不把分线147与其它重叠测试累计成总gate数量。

### 第五候选统一本地 gate

上述三片统一26个相关测试文件，534 passed、0 failed、0 error、0 skipped；Ruff、doc sync、strict code-size、
工作树与暂存diff检查、clean-package均通过，尺寸基线未改。独立只读末审核对来源schema至provider声明的消费链，未发现阻断缺陷。
插件开启且零安装包时仍展示原source_ref入口，此次补充完整schema，不保证此条件下模型输入字节不变。
实际安装切换和六席原生TUI另记；组件与本地gate不作为真实模型通过凭据，未推送，线上CI未作为验收来源。

### 第五候选实际安装及首轮保留集

源码`f0f7fdedd93c`与wheel、已安装1,358个Python文件一致；三包代次保持，官方M2.7来源与实际usage联合核对。
所有新会话实际记忆为空，每例业务一次原冻结中文提示，观察者不执行业务脚本或补产物。
B01完整9字段原source_ref→原checker字节→真实执行退出0且业务通过；C02方法读取及证据关系通过。
A02原checker也真实退出0，但内容遗漏与新增事实仍需修订；不能用检查器覆盖范围代替完整质量。
真实主→统筹→两孙同代读取通过，分工覆盖和统计错误使业务失败；公共writing-plans实际调用通过但计划不完整。
REOPEN01非法索引使控制未触发；独立REOPEN02实际stop/compact/正常退出/新TUI续原session和Goal完成，
身份与原索引保持、attempt合法换代，但摘要是机械回退，恢复后未再读包，不算方法续用通过。
12个保留例各只做1/3重复：9领域例4例get包，3普通例均无误用；N09多给句子单列业务失败。
完整逐项表见[能力验收](docs/tasks/CAPABILITY_PACK_ACCEPTANCE.md#第五候选保留集首轮)；自然稳定采用未过，
剩余两轮重复尚未完成。保留集只评测，旧失败不覆盖，不以改提示重跑回填。
随后独立RUNTIME-MISSING01通过原生CAP05安装/启用私有夹具、CAP06一次业务（官方M2.7，6 HTTP／19.227秒）：
实际读取方法、原样source_ref复制并运行脚本，Python缺模块退出1，模型准确说明无预览且完成三项数量/合计12的内联记录。
外层Shell因echo返回0，与渲染失败分开记录；不算失败后报告写盘通过。原输入、三旧包及Gateway身份保持，无观察者代执行。
随后原生CAP05依次停用释放、卸载，安装表逐字节恢复原三包；所有既有业务文件保持。

### 摘要工具控制补片本地回归

原摘要请求保留已授权工具schema，仅将选择改为none；三协议实际组包与出站容量投影一致，违规工具回复不执行。
失败诊断只有响应形状，不写正文／思考／参数；分段次数、媒体失败、完整来源与原checkpoint/CAS保持。
25个相关文件联合537 passed，0 failed/error/skipped；独立审阅未发现阻断缺陷。
首次统一5失败保留：1项旧auto期望随明确合同改为none，工具/system/messages前缀断言仍在；
其余4项在精确候选5原源码同样失败。large输入430000校准为425000字符，保留150000≤候选<180000、
零业务发送／零提交断言；后台窗口15500校准为18000，原完整准备一次／摘要一次／真实请求相等保持，
新增候选低于触发线且加输出预留仍低于窗口的断言。生产容量限制未变，初次410000误校准也留证。
Ruff、doc sync、strict code-size、diff与clean-package通过，尺寸基线未改，线上CI未作为验收来源。
其它协议fake结果不代替供应商实测，后续main effort接口组合另列。

### 第六候选真实Compact控制

精确源码`d4dd6c094`、wheel和安装的1,358个Python文件一致；六席正常退出后仅切私有Gateway，三包安装账和记忆关闭策略保持。
CAP06用原session恢复已完成的REOPEN02，仅原生发送一次`/compact`，没有补业务提示或恢复已完成Goal。
官方M2.7实际一次HTTP，输入72,821、输出1,123，38.170秒提交原checkpoint，canonical generation 1→2；
历史估算77,141→14,061，生成连贯目标／文件／进度／约束摘要，原机械回退记录保留。
原七文件、task完成状态和pins保持，业务工具操作12→12；旧原始消息和checkpoint前缀保留、无资源锁。
没有额外供应商抓包，也未触发新版失败诊断分支；不据此宣称全部协议实测、缓存命中不变、方法续用或旧失败原因已知。
