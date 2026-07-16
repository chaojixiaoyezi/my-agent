# Subagent Progress

## 2026-07-10 runner 心跳窄写与 takeover 结构化 handoff

- 真实 900 秒 takeover run 产生约 197 份 compact 快照。根因不是模型反复 compact，
  而是 `runner_session_lease` 每 5 秒把 heartbeat 送进完整 `manager.save()`；完整保存
  会同步 task workspace、artifact manifest、compact chain、memory gate、daily ledger
  和 owner projections。
- `SubAgentPersistenceService.save_runner_session` 现只在 run-local guard 内更新
  `canonical_state.json` 的 runner-session/heartbeat 事实，并刷新 locator mtime 使
  `list_runs` 缓存失效。业务完整保存使用同一 guard，防止两个写者并发覆盖；heartbeat
  不再产生 compact/projection 副作用。
- takeover 创建时写入有界 `subagent_takeover_handoff.v1`，把来源 run 的状态、
  current_step、latest_summary、blockers 和 refs 注入 `context_bundle.takeover`。
  runner 先用嵌入 handoff 接续；受管状态面不能通过通用 `read_file/list_files` 读取，
  refs 仅在摘要不足且具体产物有读取授权时使用。
- 回归：runner lease 多次心跳和 completed 后 compact ledger 字节不变，`list_runs`
  可见最新 session；takeover prompt/context 含来源结构化 handoff 和读取边界。
- 第 5 个真实 runner 进一步发现：原始响应和 structured repair 都无结果块时，旧
  `record_finalized_runner_result` 会把 `found=false` 显式回填为 DONE/VERIFIED。
  现已 fail-closed 为 BLOCKED/UNVERIFIED/structured_output_parse_error；该失败类型
  保持 retryable，可在提高输出预算或 provider 恢复后重跑同一个 run，而不是制造假成功。

## 2026-07-03 持续型委派语义:service_window_seconds 端到端(底座提升 A4)

- **实锤**:盯守(无终态持续任务)派给子代理后,子代理按"做完即退"产出首批发现即
  DONE 提前收口,整任务停摆(真机 u-t1b 只 18 条;wake_queue 有 subagent-finished
  DONE)。`long_running` 此前唯一消费者是 compact 深度豁免,生命周期语义没下沉。
- **声明端**:`create_subagents` 新增 `service_window_seconds`(int,配合 long_running,
  声明最短值守窗口秒数),经 `create_policy._POSITIVE_INT_ATTRIBUTE_FIELDS` 透传
  task.attributes。唯一事实源 `agent/subagents/service_window.py`
  (`service_window_remaining_seconds`,锚 created_at——接管/重派生成新任务窗口重新起算)。
- **子代理端**:`agent_core/subagent/progress_closeout.py` 窗口未走完 → 不因"落了一次
  产物"被系统自动 DONE(收口抑制);窗口走完/未声明行为与旧完全一致。
- **父代理端**:`runner_completion_wake.py` 子代理终态且窗口未走完 → observation 概要
  + metadata 带结构化事实 `service_window_incomplete=true` 与剩余秒;整合提示词补 6b 条
  (重派或接管,别当完成);`_run_wake_signal` 在 subagent_runner_finished 时同步跑
  watch-lane 补岗扫描(原只挂定时 policy 轮,盯守子代理一退即机制层补岗)。
- 决策(重派 dispatch_subagents / 自己接管)归模型;不禁止派子代理。钉子:
  `tests/test_service_window_semantics.py`(窗口语义/收口抑制/透传/工具→create_run
  端到端/wake 载荷两态)。

## 2026-06-12 来源比例观测 + 启动孤儿检测(backlog Active 清零)

- **来源比例观测**(R8b 隐蔽编造实锤:静态列表复用 24 周+公式造数,验收照过):
  `delivery_closeout/source_volume.py`——closeout 报告新增
  `source_volume_observation`(网络成功调用数/交付文件数与字节/声明 min_count
  总数并排),挂双路径。检索侧按 `ToolSpec.category=="web"` 结构化判定零白名单;
  **设计裁决:纯观测零 finding**——"数据类 vs 分析类产物"是机器判不了的内容
  语义,任何阈值必误伤零网络的本地分析任务;比例判断留给把关者。钉子 4 条。
- **启动孤儿检测**(异常崩溃兜底,孤儿回收第二期):`startup_recovery.
  _detect_orphan_processes`——任务终态+落盘 pid 进程仍活+cmdline 含本系统特征
  (防 pid 复用误报)三条件才报告;observability 先行绝不自动 kill,启动文案
  提示用户手动处置。钉子 5 条(含防误杀三连)。

## 2026-06-12 交付写权限墙:声明产物目录围栏直授 + 拒因账本留痕(R8a 实锤)

- **实锤与取证**:R8a 主跑+接力共 17 次"子代理写自己声明的交付文件"被系统
  WRITE_FORBIDDEN(A1 账本铁证非幻觉),同子代理同文件"先拒后成"(capreq grant
  救场)证明拒因是瞬态边界状态;canonical 终态重放(含清空 grant)一律 ALLOWED
  ——瞬态不可复现,暴露"边界决策不留痕"根缺口。可控对照实验实锤独立通用缺陷:
  `delivery_root` 只认环境字段(attrs.run_workspace 等),创建链未透传时(worker
  孙派/CLI/gateway 等任何非主 run 上下文)为空 → **写边界不含声明位置,声明驱动
  语义断裂**——"output_files 声明了产物在哪,哪里却不可写"。
- **修复①声明目录围栏直授**:`output_alignment.declared_output_write_roots`——
  output_files/output_refs 声明的绝对路径父目录,过围栏(task_workspace_dir/
  task_dir/manager workspace 根,与 capability grant `_safe_grant_roots` 同款
  基准)后并入写边界(`_build_write_boundary` 接线);围栏外声明不自动授权
  (防自我扩权,仍走 capreq)。声明驱动的对偶:声明交付到哪,哪里就可写——
  不再依赖任何环境字段透传。
- **修复②拒因原文留痕**:A1 失败账本条目新增 `message`(系统拒绝/错误原文
  截断 240 字)。决策时刻的拒因(边界/锁/危险目录)随账本落盘,下次此类取证
  直读账本,不再终态重放考古。
- 钉子:test_subagent_output_alignment 增 2、test_subagent_tool_failure_ledger
  增 1(既有全字典断言按新契约更新 2)。

## 2026-06-11 R7 真实轮四通用缺陷修复（交付链路与孤儿回收的实战补强）

R7 三任务（写死产物要求）实锤暴露并当轮修复，详见
docs/audits/R7-three-tasks-20260611.md 与 REFACTORING_BACKLOG 同日条目：

- **①pid 保留权威化**：`process_control.build_background_start_record`
  （BackgroundStartUpdate 入参包）成为 background_start 记录构造唯一权威，
  agent 侧与 CLI 侧（cli/dispatch_background）共用——CLI 手写 dict 抹 pid 的
  问题根治，孤儿回收进程层恢复弹药。
- **②合同空壳回落**：closeout 的 `_no_required_artifact_response` 在合同零
  required artifact 时,有真实产物即回落 uncontracted 验收链（gate 全跑,
  含 expected_outputs 对账门）,真零产物才打回。
- **③单次运行环境事实**：`_workspace_prompt_section(single_shot=True)` 对
  source=cli_run 注入"中途请示无人应答、不要以提问收尾、结束前交付或写
  不可行报告"软约束；gateway/chat 不注入。
- **④产物候选扫描兜底（最重）**：`_current_run_task_output_artifacts` 在写入
  记录为空时回落任务交付目录文件系统扫描（仅 task_output scope、上限 200、
  每文件照常过 validate_artifact）；出口合同 `_has_final_closeout_candidate`
  contract 分支同步回落同一产物事实链。修复 compact 后/run_command 生成文件
  对 closeout 完全不可见的问题（r7b：24 个真实 xlsx 曾全链路失明）。
- 钉子：test_exit_orphan_recovery +2、test_expected_outputs_gate +2、
  test_run_task_workspace_writer +1。

## 2026-06-11 检索完备性软引导：同工具连续失败即引导枚举未试渠道（R5b/R6c 实锤专项）

- **实锤**：R5b web_search 系统失败 2 次（TOOL_UNAVAILABLE×2，A1 账本确认真失败）
  后，模型断言"数据根本不存在"口头放弃——把关者核验 OSS Insight 等渠道实可得；
  R6c 同构（单一查询口径失败即下绝对结论）。
- **落地（纯软提示，零硬门零拦截）**：`tool_guard/loop_hints.py` 新增
  `append_tool_failure_channel_hint`：
  - 触发（全结构化）：同一工具的 archive ok=false 累计 ≥
    `tool_failure_channel_hint_threshold`（主配置三同步，默认 2，0=关闭）。
    按 tool name 通用计数，零工具类型枚举；`__parse_error__` 不计；只认系统事实
    （与 A1 失败账本同源），绝不解析模型文本。
  - 动作：tool_context 注入一条枚举引导——下"数据不存在/不可行/找不到"绝对结论
    之前，先枚举已试渠道（含失败证据）与已知未试渠道（其他工具/数据源/查询字段），
    换渠道再验证；确认不可行则把枚举写进结构化不可行报告
    （tried_channels/untried_channels_known，作为可审计的不可行说明字段）。
  - 幂等：每工具最多提示一次（tool_context 标记去重）。
  - 接线：`_tool_loop_service._run_tool_round`（与 guardrail 拦截回显并排），
    主代理与 worker 子代理共用此链路，子代理同样受益（R5b 失败发生在子代理）。
- **边界诚实声明**：R6c"检索成功但只用单一署名字段"的形态，机制层无法结构化判定
  （判断查询参数的语义完备性=解析自然语言，违铁律）；这部分留给教训记忆
  （P5-2 trigger_conditions）与把关层。
- 钉子：`test_tool_failure_channel_hint.py` 5 条（阈值触发/分工具计数/幂等/
  关闭开关/成功与解析错误不计数）。

## 2026-06-11 产物类型/数量对账门：声明驱动核对交付区实存（R6b/R6c 实锤专项）

- **实锤**：R6c prompt 要求"每篇论文一个 PDF"，实交 0 PDF（仅 1 个 md 检索报告）；
  R6b 要求 1–24 周每周一份，实交 1 份——closeout 只查"有没有产物文件"，两案均
  ok=true。缺口：声明的**类型与数量**无人对账。
- **设计（纯声明驱动，守三条铁律）**：自然语言产物要求不进机器决策——模型负责把
  prompt 要求翻译成结构化声明（spec 引导），机制只对账声明 vs 文件系统实存：
  - 声明侧：`task_progress` 新增 `expected_outputs` 字段（`{pattern, min_count,
    note}`）。pattern 是相对任务交付目录的文件名或 glob（`*.pdf`），扩展名天然
    携带类型（开放世界：不写任何格式专项分支）；min_count 声明数量（默认 1）。
    归一化（坏条目/路径逃逸丢弃）、合并（同 pattern 覆盖、新 pattern 追加、
    不带声明的更新不丢已有声明）、summary 投影见
    `task_progress.normalize_expected_outputs`。
  - 对账侧：新增 `delivery_closeout/expected_outputs_gate.py`——closeout 时逐条
    glob 交付区，实存文件数（目录不算）< min_count 即
    `EXPECTED_OUTPUTS_MISSING`（medium，**repair 非硬卡死**，附 declared/actual
    对照与 required_actions）。挂 contract 路径（gates.attach_closeout_gates，
    报告字段 expected_outputs_gate）与 uncontracted 路径双入口。
  - 零声明零影响（unchecked allow 带原因）；交付要求中途变化时更新声明即可
    （同 pattern 覆盖语义）。
- 钉子：`test_expected_outputs_gate.py` 9 条（R6c 类型错配形态 / R6b 数量缺口形态 /
  目录不算交付物 / 声明持久化与合并 / 坏条目丢弃 / uncontracted 端到端打回 + 补齐
  放行）。
- 对真实任务的使用提示：测试 prompt 应把产物要求写死（"必须 .xlsx / 每篇一个
  .pdf / 共 N 份"），模型把要求声明进 expected_outputs 后，对账门才有声明可对。

## 2026-06-11 孤儿子代理回收：主代理退出前回收后台派工进程（R6a 实锤专项）

- **根因实锤**：真实模型路径的子代理派工是 `subprocess.Popen(start_new_session=True)`
  独立进程（durable 设计，`orchestration/background/dispatch.py`），不随主代理 run
  进程退出而停止——R6a 主代理 12:39 RUN_EXIT 后，后台 dispatch 进程活到 13:00
  （+21 分钟）继续往交付区写占位符。两处观测断链：①pid 只进内存 registry，落盘的
  `background_start` 属性无 pid（cancel_subagents 已预留的 `background_start.pid`
  终止路径永远 no_pid）；②CLI dispatch 进程从不更新 background_start.status
  （永远 launching），进程死活只能验 pid 不能信 status。
- **对照组**：通道运行时（级联 kill+SIGTERM→SIGKILL）/ 长期助手（ProcessRegistry pid
  落盘+kill -0 活性探测+树形终止）/ 会话运行时（SIGTERM→2s 宽限→SIGKILL 升级+进程组）
  三家全部显式 kill，不靠自然死亡。
- **落地**：
  - `subagents/process_control.py`（新）：进程治理原语唯一权威——`is_pid_alive`
    （kill -0 + 先非阻塞 reap 自己的僵尸子进程，防 zombie 误判活）、
    `terminate_pid_with_escalation`（SIGTERM 进程组优先→宽限→SIGKILL，结构化报告
    永不抛异常）。cancel_subagents 工具同步收敛到此原语（删三个私有重复，获得
    SIGKILL 升级能力）。
  - pid 落盘：`mark_background_start` 加 pid 字段——进程确认启动后写
    `background_start.pid`（status=running），后续无 pid 的 mark 不抹掉已落盘 pid。
  - `agent_core/tool_loop/exit_orphan_recovery.py`（新）：出口回收——任务工作区第一
    层子代理 + manager BFS 子树（覆盖孙代理自己 spawn 的进程）→ 非终态任务收集
    pid 去重终止（同 launch 共享进程只杀一次）→ **仅 RUNNING 任务** requeue
    （abandon attempt → PENDING，保住 resume 可重派；ABANDONED 终态会让 dispatch
    默认不捡）→ `orphan_recovery` 属性留痕（previous_status/pid_report/requeued）
    + background_start.status=terminated + work log。BLOCKED/WAITING 等状态语义与
    进程无关，只留痕不动状态。
  - 出口合同接线：`_unfinished_exit_response`（闸断放行+确有未收口）先回收再追加
    RUN_UNFINISHED_EXIT，resume 块带 `orphan_recovery` 报告；修正旧文案"进程退出后
    运行中的子代理会停止"（与事实相反）。新增 `unfinished_exit_passthrough` 直通口
    覆盖**非 break 系统截停出口**（工具轮数耗尽，R5a 形态）——不续航但同样回收+
    带 RUN_UNFINISHED_EXIT。
  - 配置三同步：`run_exit_orphan_recovery_enabled`（默认 true；false=不动进程且
    退出声明如实标注"后台进程仍在运行"）。
- 钉子：`test_exit_orphan_recovery.py` 15 条（真实 sleep 进程终止/zombie 容错/
  requeue 语义/BLOCKED 不动/孙代理子树/共享 pid 杀一次/出口接线开关双路/轮数耗尽
  直通/pid 落盘+保留）。

## 2026-06-11 锁生命周期 + 读边界 grant 闭环 + 引导前移 + 占位符明示（任务完成力底座第二/三批）

- **P3-1 锁生命周期**（R5a 实锤：23 次 WRITE_FORBIDDEN 锁的是子代理自己的交付目标）：
  `output_alignment.sanitize_self_locked_delivery_targets`——save 唯一权威口
  （persistence.save）统一剔除"锁住自己声明交付目标"的派工矛盾（含目录覆盖形态），
  结构化留痕 `attributes.locked_files_sanitized`；`record_locked_files_change` 给锁
  增删记流水账 `locked_files_changes`（R5a"中途谁动了锁"取证盲区的修复）。
  与自身目标无关的锁（兄弟产物/敏感文件）原样保留。锁来源无论模型参数
  （dispatch_subagents 的 locked_files）、takeover 透传还是 save 合并，一律过此口。
- **P3-2 读边界 grant 闭环**（R5a 实锤：子代理读不到分析材料、grant 后读边界不扩）：
  capability grant 的 path_scope 一律并入 `allowed_read_roots`（读是 grant 的最低
  权限；目录条目经执行端 workspace_roots 子树语义天然授权整棵树）。
- **P4-1 引导前移**（R5 三案实锤：A3 引导挂 closeout 不提交就看不到）：kernel 树
  快照的 `tool_contract` 存在 OPEN capreq 时直接带
  `recommended_tool="resolve_capability_requests"` + `open_capability_request_ids`，
  主代理在 inspect_agent_tree/watch 运行中即可照做。
- **P2-2 占位符明示**（R5a 实锤：交付区多数"分析文件"是兜底摘要占位）：
  materialize 兜底渲染的产物带 `placeholder: true` + 独立账本
  `attributes.placeholder_artifacts`；closeout 投影单列 `placeholder_artifact_count`。
  只观测明示，不改对账判定。
- 钉子：`test_subagent_lock_lifecycle.py`（5 条）+ capability 闭环测试读边界对偶断言
  + kernel 引导钉子 + 占位符投影钉子。

## 2026-06-11 run 出口合同：口头放弃走门 + 修复续航（任务完成力底座第一批）

- **P2-1 出口走门**：新增 `agent_core/tool_loop/final_exit_contract.py`——模型给最终
  回复（主循环 break）时，存在未收口任务态（非终态子代理 / open capreq / 派过子代理
  / 产物可验）即先走 delivery closeout；触发条件全部是结构化事实，纯问答 run 零影响。
  修复 uncontracted 的"零产物无条件早退"（R5b/R5c 口头放弃绕过所有 gate 的根因）与
  `_missing_contract_closeout_response` 用 non_terminal 报告覆盖完整阻断报告的问题。
- **P1-1 修复续航**：closeout 阻断（rework 已注入 tool_context）时在双闸内打回模型
  继续修——预算 `run_repair_max_continuations`（主配置三同步，默认 3，0=关闭）+
  进展签名闸（open 子代理数/open capreq 数/progress open 计数完全不变即停，防死循环）。
  续航状态挂 ToolLoopService 实例（每 run 新建，天然隔离）。
- **P1-2 余留合同**：REWORK 最终回复必带结构化 `resume` 块（task_root/progress_ref/
  open_count/恢复入口命令），非终态退出不再只有一段口头返工文本。
- **当前交付边界（2026-07-15 修正）**：“派过子代理”不等于“必须生成文件”。
  closeout 仍会强制聚合子代理、进度、capability 和证据事实；无显式 artifact contract
  的纯分析/问答以 `delivery_mode=message` 收口。只有显式声明 output/expected output 时，
  缺失对应文件才结构化返工。旧 `UNCONTRACTED_EMPTY_DELIVERY` 路径、schema 和恢复分支已删除。
- 行为变化：出口检查会让"未收口即收尾"的 run 多一轮续航（两个既有 closeout 测试的
  backend.calls 断言 3→4，已按新语义更新注明）。
- 钉子：`test_final_exit_contract.py` 覆盖纯问答零影响、子代理已收口时的 message
  交付、显式 artifact contract 缺失返工、open ledger 续航和 resume 块。

## 2026-06-11 确定性优先三件套：系统级工具失败账本 + 写边界一致性钉子 + capability 软引导（开发计划 A1-A3）

- **A1 系统级工具失败账本**（根治 R4b 模型归因幻觉）：新增
  `subagents/tool_failure_ledger.py`——子代理一轮 `agent.run` 的
  `archive_tool_calls`（registry 层 ToolExecutionResult 的 ok/error_code，系统事实）
  提取 ok=False 摘要（tool/call_id/error_code/target），经
  `RecordRunnerResultParams.tool_failures` 写进
  `task.attributes["tool_failure_ledger"]`。语义：`[]`=系统确认零失败（强事实，
  拆穿模型口头归因），`None`（超时/worker 异常拿不到数据）不覆盖旧账本。closeout
  的 `unresolved_children` 投影新增 `tool_failure_codes`（error_code→次数）——模型
  summary 声称 WRITE_FORBIDDEN 而系统账本为空时，矛盾在报告里直接可见。
  纯观测，不做硬门。钉子：`test_subagent_tool_failure_ledger.py`（11 条，含 R4b
  幻觉对照形态）。
- **A2 写边界一致性钉子**：`test_subagent_output_alignment.py` 增三条——R4b
  "祖先级 forbidden（/Users/<user>）不拦已授权交付区 + allowed 内 forbidden 子树
  必须收窄"双向钉死 narrowing 语义；boundary 的 forbidden/locked 恒等于 task
  字段（同源无漂移）；首轮 attempt 零 grant 即含交付区且重复构造稳定（排除
  时序窗口）。
- **A3 capability 闭环结构化软引导**（R4b 主代理 0 次调用 resolve 的针对性修复）：
  `SUBAGENTS_CAPABILITY_REQUESTS_OPEN` finding 的 evidence 新增
  `recommended_tool="resolve_capability_requests"` + `open_capability_request_ids`
  （主代理直接拿去调用，不必从文本猜）；`required_actions` 里的
  `resolve_open_capability_requests` 改为真实工具名（防诱导调用不存在的工具）。
  软引导，不拦主链路。

## 2026-06-11 失败自省 split 建议生产→消费打通（自动拆分闭环）

- 打通点：`FailureIntrospector` 产出的 `split_suggestions` 此前无人消费（死路）。
  现在 `_handle_failure_introspection` 在调参后调用新增的 `_apply_introspection_split`：
  `should_split` + 建议非空 + 配置开启 + 深度未超限时，复用
  `failure_analysis_service.split_task` 把失败任务真实拆成子任务（子任务 PLANNING
  先落盘、原任务 TAKEN_OVER 后落盘），拆分账本记进
  `failure_introspection_data.split_applied/split_into`，跳过原因结构化记录在
  `split_skipped_reason`（auto_split_disabled / depth_limit:N）。
- 配置（capability_config 三同步）：`subagent_failure_auto_split_enabled`（默认 false，
  拆分会创建新任务需显式开启）+ `subagent_failure_split_max_depth`（默认 2；0=不限制）。
  运行时读取统一走新增的 `capability.runtime_config_reload.capability_config_for_agent`
  （快照→缓存加载的唯一权威；context_compactor 原私有重复实现已收敛到它）。
- 消费 key 与生产端对齐：`new_timeout_seconds`（已有钉子）、`max_tool_rounds`
  （消费链真实存在，留给 LLM 自省/人工注入）；删除 `split_goal` 字符串拼接分支——
  它是无人生产的影子拆分路径，拆分唯一权威是 split_task 子任务。
- 自省吞异常修复：load 失败仅日志；apply 段失败把 `runtime_error_report` 写进
  `task.attributes["failure_introspection_error"]` 并补落盘，留痕再失败才降级日志；
  任何情况不向 runner 主链路抛异常。
- 钉子：`test_real_class_integration.py` 两条真实链路钉子（开关开→子任务真实落盘
  可派工；默认关→只记 skip 原因行为不变）；`test_dispatch_mixin.py` 两条留痕钉子
  + 救活 4 个曾被错误缩进成嵌套 def 的死测试（pytest 收集数 1→23）。

## 2026-06-10 合约身份 helper 去重

- 幂等/修复合约身份两个文件合一，消除 3 个逐字重复的私有 helper；纯内部去重，
  公开入口函数名和行为不变，focused 合约测试全绿。

## 2026-06-10 服务层 facade 清理与链路拉直

- 删除 `services/__init__.py` 的 11 个 service re-export；manager 与调用方全部直连实现模块，导入链不再经过包枢纽。
- `services/capabilities|runner_context|runner_result/` 三个单模块包打平为同级 `*_service.py` 文件，相对导入深度同步减一。
- `parsing/__init__.py` 尾部对 `envelope` 的反向 re-export 删除，`services/hierarchy/__init__.py` 清空转发；两处模块级循环导入消除（AST 级检测确认全包无真循环）。
- 行为不变：仅导入路径调整，focused tests（manager/board/hierarchy/parsing/capability requests）全绿。

## 2026-06-09 状态投影降噪

- `status --json` 的 `subagents.hot` 只显示当前可行动的近期风险项；超过 72 小时没有更新或进展的
  风险项计入 `historical_hot_count`，不再占满默认状态输出。
- 历史风险项不被删除，仍可通过 subagent board、`subagents --all` 和审计文件查看。这个变化只影响
  人和 gateway/chat 默认状态投影，不改变子代理调度、验收或历史记录。
- `SubAgentBoardItem` 现在携带结构化 `created_at`，启动恢复检测不再因为缺字段把 active work
  误报为 0；如果检测失败，`status --json.active_work.detection_errors` 会显式暴露结构化错误。
- `status` 展示层会截断长 goal，并拆出 `current_summary` / `history_summary`；
  原始 goal 和完整历史仍在 task/board/detail 事实源里，状态页不再承担大报告或历史审计职责。
- `subagents` 默认命令同样只展示当前 hot 或最近项；历史 hot 只计入 `historical_hot`。
  需要看完整历史时显式使用 `--all` 或 status/owner/root 过滤。

## 2026-06-07 真实 all-agent 子代理对照

- 真实运行目录：
  `validation/real_runs/20260607-000144-subagents-all-agent`。
- Prompt 使用普通中文：主代理找几个帮手分头阅读 `/Users/example/study-agent/all-agent`
  下的项目，最后由主代理核对、合并报告并提交验收。
- 运行完成并 `submit_for_acceptance` 通过：`tool_rounds=51`、
  `ctx_tokens≈137464`，任务内触发真实 compact 1 次，compact 后继续读取子代理产物并完成提交。
- 子代理链路可跑通：task workspace 下产生 14 个子代理目录，最终均为 `DONE`；
  `output/` 下生成综合报告和 12 份项目分报告。
- 暴露的真实问题：
  - 主代理会高频重复 `inspect_agent_tree` / `list_files output`，虽然已有 `wait` 工具，
    但 `inspect_agent_tree` 的重复查看 cooldown 默认只有 30 秒，模型一轮往返常常刚好越过该窗口。
  - `read_file` 读目录时返回过泛的失败面，真实 run 中表现为 `UNKNOWN_ERROR`，不利于自动改用
    `list_files`。
  - 无结构化交付合同时，uncontracted closeout 只能验收“本轮写了报告”，不能证明用户列出的
    每个项目都被等质量覆盖；本 run 中 `letta-main` 路径不存在，最终 note 仍说“13 个项目全部完成”，
    但独立分报告实际是 12 份。
  - `my_agent_main.md` 只有 25 行，说明父代理汇总时会过度相信子报告存在，缺少覆盖质量结构化索引。
- 已修复：
  - `inspect_agent_tree` 重复查看 cooldown 默认改为读取
    `subagent_watch_interval_seconds`，仍允许显式 `cooldown_seconds: 0` 关闭；这是软提示和缓存摘要，
    不是硬门。
  - `inspect_agent_tree` 在 cooldown 内先比较 task-local `task.json` 的 mtime/size 指纹；
    树状态没变时直接返回上次的紧凑提示，不再完整读取和渲染整棵树。任一 task 状态文件变化时仍回到完整
    kernel snapshot，避免隐藏新进展。
  - `read_file` 遇到目录返回 `PATH_IS_DIRECTORY` 和建议的 `list_files` 调用，不再给泛化未知错误。
  - 默认配置 `workspace_root` 改回空值，保持“未配置时使用启动目录”的主链路语义。
  - `create_subagents` 的 `count > 1` 模式不再把同一个 `output_files` /
    `output_refs` 复制给所有 child。共享目标会记录到 `shared_requested_output_*`，
    每个 child 获得 task-local `work/child_outputs/...` 独立结果槽，避免真实 runner
    把多个子代理产物写成同一个文件。
  - 子代理 runner finalize 现在优先识别当前 run 的
    `[MAIN_AGENT_DELIVERY_COMPLETE]` 成功块：如果 runtime closeout 已经验收 task
    output 产物，就合成标准 `DONE` / `VERIFIED` 子代理结果，不再进入
    `SUBAGENT_RESULT` repair 轮把 task output 误判成源目录缺文件。

## 2026-06-09 子代理路径合同收敛

- 真实 live lab 暴露子代理执行上下文同时暴露旧 `.my_agent/subagents/<run_id>` locator
  和当前 task workspace，导致模型把旧目录当自己的 `task_dir`，artifact 聚合也会接受旧目录产物。
- 当前修复把模型可见 `task_dir`、write boundary 和 protocol write contract 收敛到
  `tasks/<date>/<task>/work/agents/<run_id>/`。旧 locator 只保留为 owner/index 查找面，
  不再作为模型写入根或 artifact 候选根。
- `output_files` / `output_refs` 中带括号占位符路径段的值，例如 `[任务目录]/report.md`
  或 `[workspace]/report.md`，会被视为非真实文件合同；不会进入 required refs、
  allowed write roots、declared output refs 或自动物化路径。
- 这不是按中文词判断，而是结构规则：括号占位符路径段不是当前 run 的真实文件路径。
  真正的输出目录仍必须来自结构化参数、当前 task workspace、授权写入根或用户明确给出的普通路径。

## 2026-06-06 主链路小跳转清理

- 删除只服务单一调用点的 facade/helper 文件，把能力请求解析、action rescue 渲染、runner
  guidance 注入、runner tool 过滤和 root task policy 折回当前权威模块。
- 真实主代理 all-agent 阅读任务暴露了工具协议示例污染：目录示例仍写
  `parameter_name` 占位字段，模型会照抄成错误工具参数。当前工具协议示例改为真实
  `read_file` 顶层参数；registry 统一拒绝未知顶层参数并返回 `TOOL_INVALID_ARGUMENTS`，
  不再让 `list_files` 这类有默认值的工具静默把错参当成功。内部字段通过
  `ToolSpec.internal_parameters` 隐藏声明，不展示给模型。
- persistence 保存链路继续收直：`identity`、`security`、`status_report`、`failure_handoff`、
  `inheritance`、`output_load_errors`、`recovery_outputs` 等只服务持久化保存的私有 helper
  已折回 `persistence/service.py`；`thought.md` 渲染折回 projection 写入点。
- session progress 写入链路继续收直：工具结果路径提取不再放单独 `paths.py`，而是跟
  `record_subagent_tool_progress` 保持在同一入口里，方便排查“工具写了什么、进度如何投影”。
- subagent markdown 渲染继续收直：dispatch/watch/parent planner 和 patch review/apply
  渲染不再通过 `rendering_dispatch.py`、`rendering_patch.py` 两个中转文件跳转，统一由
  `subagents/rendering.py` 持有。
- `runner_context` 现在直接构造执行上下文、写入边界、runtime guidance 和 runner allowed
  tools；角色模板相关判断留在 `role_templates`。
- `subagent_mixin.py` 现在直接持有 run/finalize、结构化修复、recovery snapshot 和 parent
  planner 记录链路；旧 `_subagent_repair_mixin.py`、`_subagent_planner_mixin.py`
  两个私有跳转层已删除，跨模块参数类统一放在 `agent_core/subagent/params.py`。
- 这轮清理不新增工具、不新增硬门，只减少跨文件跳转和旧入口。
- 父代理汇总子代理结果时，优先读取创建/树快照返回的 `child_output_read_order`、
  `primary_artifact_refs` 和 `expected_outputs`。没有声明产物路径的子代理会获得
  task-local `work/child_outputs/...` 默认产物路径；`count > 1` 批量复制出来的共享
  `output_files` / `output_refs` 也会被拆成这样的独立结果槽。`work/agents/<run_id>/`
  继续作为内部状态、审计和恢复目录；父代理查状态走 `inspect_agent_tree`，等待走
  `wait`，不把 shell sleep 或内部目录遍历当成正常控制面。

## 2026-06-06 状态精确化

- 子代理运行、恢复、tree、closeout 统一按当前协议状态判断；`COMPLETED`、`SUCCESS`、`ERROR`
  等旧标签只保留为原始审计文本，不再隐式兼容成 `DONE`、`FAILED` 或 `CHANNEL_ERROR`。
- 显式写入子代理状态时只能使用当前 `TaskStatus` 协议值；`completed`、`succeeded`
  这类旧成功别名会 fail closed，不会静默改写任务状态。
- dispatch workflow 候选和 runner 子结果摘要继续收敛到当前 `TaskStatus` /
  `DISPATCH_INELIGIBLE_STATUSES`；`CANCELLED`、`ABANDONED`、`TAKEN_OVER`
  不再被漏判成未完成子代理，`PAUSED` 仍按未完成保留给父代理处理。
- remembered run unfinished、parent-timeout recovery、compact continue packet 和 board risk
  也改为调用 `subagents.models` 的共享状态 helper；旧大小写/别名状态不会在这些链路里
  被各模块单独解释成完成、失败或可收口。
- agent tree 展示、due-check、leadership recovery、recovery orchestration、runner
  payload 和 QA repair payload 也不再维护本地失败状态集合；机器判断统一走
  `TaskStatus` / `SUBAGENT_FAILURE_STATUSES`，展示文案只消费已经归一的状态。
- 派发状态投影遇到旧标签或未知状态时，仍保留原始 status 供审计，但不会给父代理
  `summarize_or_report_verified_runs` 这类收口建议；必须先检查 agent tree 或人工处理。
- 恢复状态机不再把 `PLANNED`、`QUEUED`、`WAIT_CHILD` 旧别名提升成当前协议状态；旧状态进入
  `manual_review`，避免跨版本残留污染当前 run。
- 缺少结构化 `failure_type` 时，恢复快照不再从 `runner_last_error` 或自由文本错误里猜恢复码；
  工具错误文本分类只保留在工具结果诊断层，不能替代任务状态事实。
- 恢复 mode 只认当前显式枚举，例如 `rerun_from_continue_packet`、`rerun_from_checkpoint`、
  `takeover_from_continue_packet`、`takeover_from_checkpoint`；不再用 `rerun_*` / `takeover_*`
  前缀把未知旧值提升成自动重跑或接管。
- capability 等待状态只认当前协议 `PENDING_CAPABILITY_REQUEST`，不再把 `NEEDS_TOOL`、
  `WAITING_FOR_TOOL` 等旧/模糊状态别名自动升级成能力申请。
- capability request 自身只认当前状态：`OPEN` 是待处理，`GRANTED` 是已授权，
  `GAP` 是没有可用能力，`CLOSED` 是本轮已关闭。历史 `RESOLVED`、`APPROVED`、
  `REJECTED` 不能静默当成已处理终态；它们会继续作为需要人工/路由处理的状态暴露出来。
- 工具结果没有显式 `error_code` / `error_type` 时，机器错误码统一是 `UNKNOWN_ERROR`；
  日志里的错误正文可以给模型看，但不能反推出结构化错误码、任务状态或验收结论。
- Tool Gateway 自己产出的结构化 finding 不属于“没有显式码”：实际强制执行管线及 path/command、owner scope、
  rate limit/circuit、approval binding、idempotency 等直接子门的稳定码必须注册到统一 taxonomy。门禁已识别
  `COMMAND_PARSE_FAILED` 时要原样保留并给出 `repair_tool_call`，不能再次降级为 `UNKNOWN_ERROR`。
- 本地运行时失败的模型提示只读取结构化 `context` code，例如 `*.subagents.load`
- 子代理 task workspace 的身份只来自当前 `root_id` / `id` / `parent_id` 和明确的
  `run_workspace.task_root`；已有 `work/state.json` / `work/task.yaml` 不再反推本轮
  task_id，避免同名旧目录污染当前子代理树。
  或 `conversation.guidance.*`。普通错误文本或自由格式 context 里出现
  `subagent/load/guidance` 这类词，不会改变失败类型、任务状态或验收语义。
- `DONE` 仍是唯一已完成状态；`FAILED`、`TIMEOUT`、`CHANNEL_ERROR`、`BLOCKED`
  是可恢复/阻塞状态，恢复器和 strategy 只扫描这些结构化状态。
- offline subagent closeout contract 也只认当前 `TIMEOUT`；`TIMED_OUT` 这类旧别名
  只能作为异常/未知状态处理，不能触发 `CHILD_TIMEOUT` 语义。
- `blocked_reason` 只是解释字段：它可以写入 blockers、报告和父代理提示，但不能单独把
  `DONE`、`RUNNING` 或未知状态改成 `BLOCKED`，也不能把 `failure_type` 猜成
  `capability_request`。需要阻塞时必须写结构化 `status=BLOCKED`、`failure_type`
  或正式 `capability_requests`。
- `output.json`、checkpoint 和 QA payload 只读结构化字段、`ok` 布尔、blockers 和 refs；
  summary、角色描述、旧状态词只作为展示或软上下文。
- 子代理结果产物只从当前结构化 schema 进入 artifact refs：`artifacts`、
  `artifact_refs`、`evidence kind=artifact` 和 `evidence_packets[].artifact_refs`。
  `deliverables`、`files`、`output_files`、`files_modified`、顶层 `path/file_path`
  等旧结果别名不再被悄悄恢复成产物；派任务时的 `output_files` 仍是创建子代理的目标路径字段。
- `create_subagents` 不再用 `working_buttons`、`verified_images`、`no_comments`
  这类专项交付约束做入口硬拦。它们可以作为结构化任务上下文传给子代理，最终由父代理验收、
  QA 或 closeout 证据判断，不在派工前阻断主链路。
- test failure classification 不再从 stdout/stderr 文本里的 `SyntaxError`、`AssertionError`
  或超时词猜恢复类别。机器分类只读 `executed`、`passed`、`validation_method`、
  `validation_result.reason` 等结构化字段；输出尾部只保留为人类审计摘要。
- 全局错误 taxonomy 不再用多语言正则从普通错误正文猜 `TOOL_TIMEOUT`、`WRITE_FORBIDDEN`
  等机器码。工具/后端/runner 必须产出显式 `error_code` 或 `failure_type`；没有结构化码时就是
  `UNKNOWN_ERROR`。
- 测试准备不再把 `command: static_site_check` 解释成验证器选择。验证器入口只认
  `validation_method`；`command` 是实际执行命令，不承担 schema 选择。
- 文件存在性验证只认 `validation_method: file_check`。旧的 `file_exists`、
  `path_exists`、`artifact_exists` 不再作为可执行验证方法别名。
- 协作能力匹配只读取显式 capability 字段和真实工具名；不再从工具名里的
  `read/search/write/dispatch` 等字样自动生成 `query/write/delegate` 这类抽象能力。
- 工具动作布尔参数只认 JSON 布尔、数字和 `true/false/1/0`；`yes/on/apply/run/full`
  这类普通词不能改变执行行为。

## 2026-06-04 收敛

- 删除旧 manager mixin 和过渡转发文件，`SubAgentManager` 现在直接拥有初始化、基础生命周期和工单路径。
- `SubAgentBoardService`、`SubAgentPatchService`、`SubAgentHierarchyService` 直接作为当前服务入口，不再保留单独转发文件。
- CLI 子代理命令注册合并到 `cli/subagents.py`，不再保留单独的 registration / hierarchy 注册 facade。
- 子代理状态继续以 task-local canonical state 为权威；owner projection 和 global index 只做查找。
- runner 终态通知不再分别调用 observation/wake 两个写入口；统一由 conversation store 先发布
  durable wake，再追加带 wake ID 的 observation。这样调度器看见 observation 时，对应 wake 已存在，
  同一次 DONE 不会触发两轮后台主代理；wake 队列失败时仍保留 observation fallback。
- `cancel_subagents` 是父代理处理卡住下级的控制面：可取消、废弃 attempt、记录审计，再由父代理接管或汇总；如果 canonical loader 读不到该 run，会返回结构化 load error，不用旧路径扫描假装取消成功。
- `inspect_agent_tree` 重复查看只给紧凑提示和直接摘要；需要等待时用 `wait` 登记下次查看间隔，不把轮询做成硬门。
- `create_subagents` 不再因为已经有活跃子代理就默认拒绝第二批；父代理可以先派一批，后面按需要继续派。
- 子代理可以写 task workspace 里的协作产物；最终交付由主代理汇总到 `output/` 或用户指定目录。

## 2026-07-14 IM 批量派工、交付路径与完成投递收口

- 原生模型在同一轮返回多个 `create_subagents` 时，运行时按顺序执行全部创建；依赖上一调用返回 ID 的
  dispatch/inspect 等编排动作仍延到下一轮。延后结果使用 `ORCHESTRATION_CALL_DEFERRED`，避免真实原因
  落成 `UNKNOWN_ERROR`。公开派工回执聚合这一轮全部 lifecycle envelope，只报告账本确认的
  recorded/accepted/running 数量。
- 已绑定当前任务且用户未显式指定输出目录时，模型生成的 owner-home 任务外绝对交付路径会归入当前
  `task/output/`，goal/thought/plan 中相同引用同步改写；显式用户目录仍按 capability 与路径边界执行。
- 后台自动续跑和成功子代理终态只在统一用户回复投影为 `delivery_complete` 时外发；监督、等待、内部
  整合和占位文字不会写入普通聊天。失败、阻塞和需决策继续按结构化事件及时投递。
- 完成轮同时有 findings delta 时，以结构化 closeout 为投递权威；IM 信封使用投影后正文，不把内部协议
  或 findings 的原始宿主路径传给 adapter。定向钉子覆盖“同轮完成+delta”只产生一条干净最终回复。

## 2026-07-16 主代理与子代理工具运行身份分界

- `tool_loop.recovery.runtime_run_scope` 不再对任意 `run_id` 调用 `subagents.load`。只有 runner 的线程级
  subagent context，或显式 `context_scope=task_local`，才具备查询子代理 canonical ledger 的结构权限；
  不从 `bg-main-*` 名字或普通 prompt 推断身份。
- 后台主代理的工具 envelope 继续记录本轮 `run_id`，但 `root_task_id` 使用会话绑定的持久任务 ID；
  因此不会每轮产生“子代理记录不存在”的假异常，也不会把临时唤醒轮误当成根任务事实源。
- 真正的子代理仍从 canonical task 恢复 parent/root/depth；账本不可读时仍输出结构化 load error。
  root、load-failure child、grandchild lineage 及相邻后台/runtime envelope 专项回归均已通过。

## 运行约定

- 子代理没有长期个人记忆，只保留 task-local 状态、事件、artifact refs、compact 和候选经验。
- 子代理模板可以定义简短 persona、description、skills 和机器能力字段；当前 runner 仍走现有执行链路。
- `role` 是模板选择字段，`agent_name` 只是展示名。创建任务时会把模板能力快照写入
  `attributes.role_template`，后续调度、恢复、timeout 和 runner prompt 只读这个快照或模板字段，
  不从显示名或普通中文/英文描述里猜角色。
- 默认 `agent_name` 也只作为展示标签，格式为 `agent-d<depth>-<role>-<index>`；多层级调度只用
  `depth` / `parent_id` / `root_id` 等结构化字段，不再从默认名或用户叫法里解析层级。
- 层级继承标记只写 `attributes.inherited_parent_context=true`；给模型阅读的 `goal`
  不再塞 `inherited_parent_context=true` 这类内部机器标记。
- capability request/grant/gap 是可观察工作项，不是默认阻断任务的硬门。
- workflow mode 是显式配置能力，不应该替普通中文任务自动加限制。

## 2026-06-11 R4 交付链路四子项落地

- 执行合同产物落点对齐（R4 子项①）：attributes 里的 `output_files`/`output_refs`
  保持"最终交付意图"不动；`services/output_alignment.py` 投影层把执行合同
  （task_packet/output_contract）的目标 refs 翻译成子代理自己 `output_dir` 下的可写
  落点，`write_contract.output_delivery_map`（落点→意图位置）渲染进 runner prompt；
  落点被 locked_files 盖住记结构化 `OUTPUT_TARGET_LOCKED` warning，不静默。
- capability 处理回路（子项②）：新增模型工具 `resolve_capability_requests`
  （grant/deny 显式裁决；grant 落 path_scope+写工具即时生效到写边界，目录围栏=
  任务工作区+主代理 workspace，越界结构化拒绝；deny 走协议终态 CLOSED+
  denial_reason）；`capability_request` 提交后立刻向父级线程发 requires_main_agent
  观察+wake（`runner_completion_wake.notify_parent_on_capability_request`）。
- 声明产物对账（子项③）：closeout 的 subagent_aggregation gate 新增
  `SUBAGENTS_DECLARED_OUTPUTS_MISSING`——DONE 子代理声明产物在声明位置缺失即
  NEED_REPAIR；同时修复 gate 把 runner 自己算成未完成子代理的自指拦截
  （评估时排除 closeout 当事人 run_id）。
- 汇总搬运（子项④）：runner result 写回时 `deliver_anchored_outputs_to_declared`
  按 delivery_map 把锚定落点的真实产物搬到声明位置（先于 summary 物化），结果进
  `attributes.output_delivery_results`（delivered/skipped_existing/source_missing/
  target_outside_workspace）。

## 2026-06-11 产物落点改回家目录交付区方案

按用户确认调整子项①的落点方向：默认交付区 = 任务工作区/output（用户主目录下
tasks/<日期>/<任务>/output，即用户拿走的东西），而非子代理家 work/agents/<id>/output。
- `output_alignment.delivery_root(task)`：优先 attributes.run_workspace.output_dir
  （主代理/用户显式指定），否则 task_workspace_dir/output。
- `output_write_grant_roots(task)`：交付区授权进真实写边界
  （runner_context_service._build_write_boundary）和模型可见 allowed_write_roots
  （context_bundle_contracts.allowed_write_roots），子代理直接写交付区、用户拿走即可。
- delivery_map 收窄：只记"声明落在交付区外、被重定位"的条目（reason=relocated_*）；
  相对声明锚到交付区、已指向交付区的绝对声明都不进 delivery_map、无需搬运。
- 搬运（deliver_anchored_outputs_to_declared）降级为兜底，只处理任务外/任务内非
  交付区的少数声明；锁冲突 OUTPUT_TARGET_LOCKED 警告与声明对账全部保留。

## 2026-06-11 声明对账加结构化豁免出口

- `resolve_capability_requests` 复用扩展第三个 decision `accept_output_gaps`（不新增
  工具，按参数复用）：把声明产物缺失豁免登记到子代理
  attributes.output_delivery_exemptions（{ref, reason, accepted_by, at}）。
  exempt_refs 指定具体声明，缺省登记通配 "*"（整体豁免，用于纯汇报任务/已确认接受）。
- closeout 的 `_missing_declared_refs` 先扣除豁免再算缺失：通配 "*" 直接返回空缺失；
  具体 ref 豁免逐条扣除。SUBAGENTS_DECLARED_OUTPUTS_MISSING 拦截因此可被显式解除。
- 豁免是结构化、可审计记录，不是静默放水、不中断任务；gate 文案与 required_actions
  指向 resolve_capability_requests(decision=accept_output_gaps)。

## 子代理产物写区兜底(batch3 C3/G4 实锤,2026-06-13)

- 实锤:子代理 `allowed_write_roots` 只含自己的 agent 目录(没声明 output_files、
  declared_output_write_roots 未生效)时,`task_product_write_roots` 过滤掉自己
  目录后为空 → `run_tool_preflight` 报 `missing_allowed_write_roots` → 子代理
  写不了产物 → BLOCKED → 主代理空等、未收口(C3 主代理 112 轮 0 write)。
- 修复:`runner_context_service.task_product_write_roots` 在结果为空时回退到任务
  工作区 `task_workspace_dir/{output,work}`——子代理总有产物写区(交付事实优先,
  减少"必须先声明才能写"的过度约束),围栏在本任务工作区内、安全。
- 钉子:test_subagent_output_alignment.test_product_write_roots_fallback_when_only_agent_dir
  (只自己目录→回退 / 有声明→用声明不回退 / 无工作区→空不崩)。
