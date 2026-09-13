# Gateway Progress

## 2026-09-13 R270 插话三段状态：模型已引用插话、界面却仍写"稍后送入"

用户现场:模型回复里已经引用了新插话,底部仍显示"将在下一次工具调用后送入当前回合",可见历史里也没有这条
用户消息。只读核对确认根因在**状态边界缺失**:展示层只有"入队"和"已确认(consumed)"两态,而
`acknowledge_injected_turn_input` 只在整次 `next_tool_loop_model_response` 返回后才执行
(`_tool_loop_service.py`),`_persist_guidance_transcript` 也只随 consumed 落;慢流/失败时已提交这一
真实中间态没有任何人表达,`_render_pending_steers` 只能把未消费一律写成"将在下一次工具调用后送入"。

修复(不猜正文、不提前置 consumed、不重发):
- 新增**已提交**事实边界:`mark_injected_turn_input_submitted` 在账本 committed `submitted`、提供方调用
  即将发出时,向输出 sink 发 `submit_active_turn_input`(`request_execution` / `background_transcript`
  / `foreground_transcript` 三处同名接口),事件携带 `client_message_ids` + `provider_call_id` + 有界正文。
  该边界只播事实,不动账本、不结算回复欠账。
- TUI:`steer_submitted` 后按原提交序号把用户消息记入可见历史,等待区改为"已送入当前回合，等待模型回应"
  且不再重复正文;已确认(`steer_promoted`)只收标记不再插行;回合终态仍未确认时降级为
  "已送入当前回合但未获模型确认；不会自动重发";子代理视角同理。重连按持久事件重放,块 ID 去重。
- 身份只用 `channel_message_id`/`request_id`/`provider_call_id`,正文不参与任何判定。

## 2026-09-13 R269 验收反馈 A/B/C：事实缓存快照绑定、投影命中原子化、坏探针回退

三条都是上一轮我自己的提交留下的缺陷，逐条先复现再修（都不改变对外语义）：

- **A（`ce238ac4` 的 `_owner_fact_kind`）**：旧顺序是"先判定、后签名"，两者可以落在不同快照上。
  真实函数受控交错复现（判定返回后、签名计算前写入一条 pending wake）：第一次 `none`、第二次仍 `none`，
  且只现读 1 次，缓存里留下 `(kind=none, digest=有新 wake 的状态)`——后续按摘要命中它，最长 600s 发现不了
  新 wake。改为：判定前先取签名 → 判定 → 再取签名，两次一致才写缓存；不一致就有界重判一次（最多一次），
  仍不一致只返回结论不缓存。修复后同一探针：第一次即 `hard`、共 2 次判定、缓存条目与当前磁盘自洽。
- **B（`ce238ac4` 的 waiting 投影）**：命中路径"先 lookup 释放锁、再单独 `move_to_end`"，而投影缓存是
  多 owner 共享的（上限 4 条，不同 owner 的文件锁不互斥）。纯内存探针（填满 4 条 → 命中窗口内让第 5 个
  owner 写入触发 LRU 淘汰）复现 `KeyError(<cache key>)`。改为 `_lookup_waiting_projection()`：
  读条目 + 核对 stat 键 + 核对摘要 + 提级，全部同一把锁内完成，并区分 guard 与 miss。
- **C（`8c914dbb` 的 `runtime_snapshot`）**：文档承诺"解析异常也退回锁内权威读取"，但锁外解析
  `probe.data` 没有 try，异常被外层变成 `unavailable`。坏/旧探针字节 + 有效账本的竞态下会误报不可用。
  已按文档修：探针解析失败退回 `_load_store_unlocked()`，只有当前账本本身不可读才 `unavailable`。

审计：全仓 `move_to_end` 调用点逐一核对，"lookup 后再单独 touch"的非原子形态只有 waiting 投影这一处
（`web_fetch_tools`/`home_indexes`/`owner_scoped_pool`/`approval_session`/`background_transcript`/`_ScanIndex`
/owner 快照都在同一把锁内）。本轮不改模型作用域、权限、Compact、游标或用户任务。

## 2026-09-12 R267 scheduler status 的锁内全量解析收口（先测量后修）

- 实测 `SchedulerRepository.runtime_snapshot()`（模型显式 `scheduler status` 的唯一实现）改动前每次调用
  **进两次锁、两次都在锁内读+全量解析 store.json**：N=10000 条 run（4.2MB）时锁内 **12.3ms + 15.4ms**、
  `load_store` 2 次/调用、逐 run 解析 10000 次/调用，并发等同一把锁的竞争者中位等 **13.4ms**；
  N=2000 时 2.2ms+2.9ms；N=200 时 0.23ms+0.30ms。调用点频率低（仅模型显式 status），收益集中在
  **不阻塞 scheduler claim/finish 的同一把锁**。
- 改为锁外 `_probe_store_bytes` 读一次字节+摘要 → 锁外全量解析 → 锁内只做一次 `_store_stat_key` 复核；
  复核不过/探针不可用退回锁内权威读取。配对交替 5 轮（N=10000）：锁内持有 **39.6ms → 0.010ms（≈3964x）**、
  并发等锁者中位 **33.9ms → 0.090ms（377x）**、p95 **46.6ms → 0.273ms（171x）**、总耗时中位
  **75.5ms → 49.7ms（1.52x）**。
- 语义零改动：返回字段与旧实现逐字一致（含坏账本 `unavailable` 兜底），并有"与旧实现参考投影逐字相等"
  与"锁内不得出现 JSON/逐 run 解析"两组确定性用例守着。未部署、未真机。

## 2026-09-12 R265 GW-03 收口：等待任务投影缓存 + owner 事实判定缓存

- 实测（交错 A/B，本机 load 一度 12.96，故只报比值）：
  · `scheduler/repository.waiting_runs` 每轮在锁内全量解析 store.json：N=10000 条 run 时 **16.1ms、锁持有 14.0ms**，
    其中绝大多数 run 并非 waiting；命中投影后 **3.4ms、锁持有 9µs（4.7x）**，冷路径锁持有 14.6→15.1ms（+3%，噪声内，
    因为内容摘要的读盘被挪到锁外）。
  · `owner_wake_discovery._owner_fact_kind` 占整页成本 **96.6%–99.4%**（本机 19 owner 真实形状 warm≈cold，
    证明事实判定此前完全没缓存）；加结构化签名缓存后真实形状 warm **3.06x / 3.80x**（19/95 owner），
    按 120s 重扫、5 轮 1 miss 摊薄为 **1.73x / 2.0x**；安静 owner 因自适应阈值（判定 <0.3ms 不缓存）保持中性。
- 修法：`waiting_runs` 改为**锁外探针（stat+读字节+blake2b-128 摘要）+ 锁内三重校验**（锁内 stat 键、锁外探针 stat 键、
    探针摘要三者一致才命中），行数上界 512、条目上界 4；`_owner_fact_kind` 改为**结构化签名**（覆盖判定读到的全部路径：
    会话存储根/wake_queue/progress_policies/messages/agents state/tasks/runs/audit/watch_state/memory policy/curator state/
    candidates/scheduler store.json/runtime.db+wal+shm 的逐条目 mtime_ns+size+ino）+ 时间边界（policy/job 到点、策展
    interval/退避/日终/日切、watch 窗口关闭、执行权锁 30s 重探）+ 600s 兜底 TTL，条目上界 1024。
- 三态一致性：缓存命中 / 清缓存 / 全新实例三者结果逐字段相等；**外部改写立即可见**；**同 stat 不同内容**（等长改写 +
  还原 mtime_ns）被内容摘要守卫拦下（`scheduler_waiting_projection_guard` 计数）；解析期间被改写则不写缓存；
  owner 侧覆盖新增/删除/原地改写（policy enabled 翻转、子代理 PENDING→DONE）与时间边界用例。
- 为什么不是第二套权威状态：每次调用都重读字节重算摘要，缓存只是"绑定在当前字节上的解析投影"；
  写路径未变，缓存不参与任何状态变更；异常/超界一律回退权威路径并计数。
- 证据：`test_owner_wake_discovery` + 新增 `test_scheduler_scan_costs` 74 passed；26 个 scheduler/owner/wake 相关套件
  204 passed；ruff/code-size(hard=0)/diff 通过。**未部署、未真机**。
- 未做/风险：①miss 路径变慢 1.5–1.7x（签名遍历+watch 窗口读盘），文件频繁变化的 owner 收益归零；②600s TTL 是兜底，
  签名覆盖不到的"同尺寸原地改写 + 系统改钟"最坏陈旧窗口 10 分钟（(a) 侧有内容摘要无此问题）；③缓存命中会跳过
  `unfinished_task_ids` 的自愈投影，而自愈本身会改签名 → 最迟下一个 miss 触发；④`runtime_snapshot` 等其它锁内全量解析点未收口。

# Gateway Progress

## 2026-09-12 R263 GW-01 残留：误 ready 已复现并修复（心跳代际 + 记录优先级）

- **可达性先证后修**（受控 tmp 文件 + 真实写入方，不动共享网关）：
  · 组合 B「同 PID：heartbeat=running（旧格式无 started_at）+ state=failed」——真子进程跑 `cmd_gateway_run` +
    父进程按 `cmd_gateway_start` 原样等待，20ms 采样磁盘：`t+0.79s ready=True`（合法发布）→ `t+1.31s
    state=failed 而 heartbeat=running，ready 仍 True（进程 poll()=None）`→ `t+1.80s heartbeat=failed ready=False`。
    窗口约 0.5s（`http_server.stop()`+3×`join(timeout=2)`），是 0.2s 轮询的 2 倍以上；改前附着观察者 5/5 误判
    `alive=True` 并向正在退出的网关提交 HTTP，改后 5/5 `alive=False` 且发布窗口仍 5/5 ready。
  · 组合 E「heartbeat=running + state=http_server_error」——**永久误 ready**（服务循环不看 HTTP 服务、心跳线程
    继续写 running），改前 3/3 ready，改后 3/3 `failed/GATEWAY_START_FAILED`。
- 修法一（写入侧，`cli/gateway_loops.py` 仅心跳写入函数 + 其私有 helper）：心跳载荷新增 `started_at`，
  取自**同 pid 的 state 记录**（与 state running 同源），终止清理时继承同 pid 既有心跳里的同一值；
  两条都取不到写 0.0（=判据既有"无法证明陈旧"的兼容语义）。不读 mtime、不抄别的进程代。
  **踩坑记录**：第一版给心跳循环调用点加关键字参数，导致测试替身签名不匹配 → `except Exception` 吞掉
  AttributeError → 心跳永不写成功、永不 set stop_event → pytest 挂死；改为"写入函数内部解析锚点"后签名不变。
- 修法二（判据侧，`agent/gateway_parts/status_rendering.py`）：新增显式 `_record_precedence`/`_record_replaces` ——
  **档 3** = state 已越过 starting 占位（生命周期所有者权威，后台周期心跳不得改写成 ready）；**档 2** = 任一来源
  running（发布顺序 HTTP bind→heartbeat→state，state 仍 starting 时心跳必须能胜出）；**档 1** = 其余，同级保留 state。
  删除旧"running 优先"梯子；`_GATEWAY_FAILED_STATUSES` 加入 `http_server_error`。唯一权威仍是 `gateway_readiness`。
- 未改：HTTP bind → heartbeat → state 发布顺序、任何 timeout/间隔、`cli/gateway_process.py`（0 改动）、退出码语义。
- 证据：`test_gateway_readiness_generation`（新增 9 例，用真实写入方 + 真实判据，不注入判据替身）、
  `test_gateway_commands`、`test_tui_preflight` 共 66 passed；邻近 7 套 + gateway_client/cli_chat 全过；
  ruff/code-size(hard=0)/diff 通过。**未部署、未真机**。
- 未做/风险：①旧格式心跳（无 started_at）仍被接受——刻意兼容边界，否则升级期健康旧网关会被判死；
  代价是"旧心跳残留 + PID 复用"仍可能误 ready，需一次重启后消失；②`cli/gateway_process.py` 的失败/终止
  state 载荷没有 started_at（本轮无该文件权限），PID 复用下 µs 级窗口可能瞬时"误失败"（不是误 ready）；
  ③心跳新鲜度（`updated_at` 老化）刻意未纳入判据；④顺带发现 `gateway_loops.py:1568` 的 `except Exception`
  会吞掉写入方签名漂移（正是本轮踩到的坑），判活只看文件存在性——建议后续单独收紧。

# Gateway Progress

## 2026-09-12 R261 启动就绪判据统一（alive/starting/ready/failed 四态 + 消除 TUI 过早 prepare 竞态）

- 现场（交接文档 GW-01 复核）：就绪事实有两套实现——`cli/gateway_process.py` 的 `_gateway_ready_for_pid`
  带 `not_before` 代际校验，而 `agent/gateway_parts/status_rendering.py` 的 `gateway_running` /
  `wait_for_gateway_running` **只读 PID 存活**；`cli/chat_parts/tui_preflight.py` 在 PID 活之后立刻
  `prepare_session()`（HTTP 读恢复历史），HTTP 尚未 bind 时失败即 exit 2，用户看到的是"会话历史恢复失败"。
- 修法：唯一权威改为 `status_rendering.gateway_readiness()`（四态 ready/starting/failed/stopped）与
  `wait_for_gateway_readiness()`（有界等待，结局 ready/failed/timeout 各带稳定 reason 码）。ready 的唯一定义 =
  state/heartbeat 任一记录 pid 匹配 + 代际校验通过（`_record_is_current_generation`，容差仍 2.0s）+ `status=running`。
  `gateway_process` 只做转发导入，发布顺序、容差、`cmd_gateway_start` 退出码一字未动；判据放 agent 层是为了
  不让 TUI 启动路径反向 import 整个 CLI/Gateway 运行时。
- TUI 行为：starting 期间 footer 显示「Gateway 启动中…（等待 HTTP 就绪）已等待 Ns」并**有界等待**，不再
  提前 prepare_session；真失败分型呈现（等待中进程消退 → `GATEWAY_PROCESS_EXITED`；记录 failed →
  `GATEWAY_START_FAILED`）；预算耗尽 → `GATEWAY_START_TIMEOUT`；从未运行 → `GATEWAY_NOT_READY`。
  **未加长任何 timeout**（监督者禁止项）。
- 证据：`test_gateway_commands` + `test_tui_preflight` 56 passed（新增 11 例）+ 18 个相关测试文件 416 passed；
  受控验收用 fake 子进程 + tmp 状态文件（不重启真网关）：六种时序判定逐条命中。**真机验收未做**。
- 边界与未做：heartbeat 载荷没有 `started_at`（在 `cli/gateway_loops.py`，本轮未授权改）→ heartbeat 分支
  只能靠 pid+running，无法证明代际（与旧语义一致，判据已支持该字段，补字段即自动收紧）；
  `gateway_running` 的布尔语义仍是"进程存活"，保持 status/adapter/supervisor 展示语义不变；
  "等待开始前网关就已不在运行"会走完有界预算再报 `GATEWAY_NOT_READY`（容忍并发启动，不误判进程消退）。

# Gateway Progress

## 2026-09-12 R258 owner 发现分页不再每页全量枚举

- 现场（交接文档 GW-03）：`owner_wake_discovery.discover_owner_home_page` 每取一页都
  `sorted(_candidate_owner_homes(providers_root))` 全量枚举+整体排序，只用其中 `limit` 个；
  owner 数 > 页大小(64) 时相邻页在相邻 tick（约 1s）连跑，每页各来一次全量枚举。
- 修法：新增**有界目录快照**（结构签名 = providers 根下 provider 集合 + 每个 owner bucket 的
  mtime_ns/inode/条目数；外加 30s TTL 与 4 条 LRU 上界）。签名在枚举前取，枚举期间目录变化只会
  让下次失效；游标仍是排序键（`bisect_right`），新增 owner 在游标前跳过、之后照常取到，删除 owner
  由签名变化立刻重新枚举。**每页仍逐盘做 owner 事实判定**，缓存只复用目录清单，不构成第二套权威状态。
- 真机基准（500 个合成 owner home、limit=64、8 页）：枚举量 4000 → 500 条目/轮，
  一次全量枚举+排序+建键 2.32ms vs 签名检查 0.16ms（≈14×）；同进程交替 A/B 总耗时 0.0661 → 0.0470（1.40×）。
  正确性：两口径都 seen/unique=500、无丢项无重复。
- 边界：`_GatewayOwnerMaintenanceController` 的 60s tick > 30s TTL，该路径每页仍重新枚举（其速率下枚举非瓶颈）；
  剩余耗时主因是每页 64 个目标的 `_owner_fact_kind` 事实判定，本次**有意未缓存**，属另一条待办。

# Gateway Progress

## 2026-09-12 R256 启动就绪的代际判据与 macOS 出生时间

- 现场（交接文档 GW-01 复核）：`_gateway_ready_for_pid` 只比对 `pid` 数值 + `status == "running"`，
  state/heartbeat 任一命中即判就绪；`pid` 文件层的 PID 重用防护依赖 `_get_process_start_time`，
  而它只读 `/proc/<pid>/stat` —— **macOS 无 /proc，实测返回 None**，该防护在本机恒空转
  （pid 文件与 state 的 `start_time` 都是 null）。文档所称"3.043 秒出生→ready"在本机也复现不出
  （实测 0.115–1.114 秒），已如实记为证据不足。
- 修法一（就绪判据，`cli/gateway_process.py`）：`_gateway_ready_for_pid` 增加 `not_before` 代际参数，
  由 `_wait_for_gateway_start_ready` 在 spawn 后取当前时刻传入；只有 `started_at >= not_before − 2s 容差`
  的记录才算"本次启动的这一代"。缺 `started_at` 的旧格式按"无法证明陈旧"处理，保持向后兼容；
  不传 `not_before` 时语义与原来完全一致。这消除的是"上一代/CID 复用留下的同 PID running 记录"被误判成就绪。
- 修法二（出生时间来源，`gateway_parts/daemon_metadata.py`）：`_get_process_start_time` 在无 `/proc`
  的平台回落 `ps -o lstart= -p <pid>`，返回稳定字符串（只用于同一进程的前后相等比较，不解析成时间戳）；
  取值失败仍返回 None，调用方必须退化为"无法证明代际"。本机实测：`52778 → Sat Sep 12 18:48:14 2026`，
  不存在的 pid → None。
- 未做：文档要求的"把 `process_identity`（host_id/pid/start_time）纳入 ready 判据"只做了时间代际这一半；
  跨主机/容器复用 host_id 的场景仍靠既有 pid 文件路径。`_wait_for_gateway_start_ready` 的 3 秒预算未改
  （文档明确要求不要用加长 timeout 掩盖启动慢）。
- 边界：`gateway_process.py` 里"HTTP bind 之后才发布 ready"的顺序未动（该注释是硬约束）；
  本轮所有结论来自源码复核 + 单元测试 + 本机只读实测，**未做真实启动故障注入**。

## R254 手动 compact 的失败如实上报（三种既有红测试的根因与处置）

用户看到"手动 compact 未完成，请稍后重试"时，有一类失败**重试永远不会成功**：当模型窗口装不下
压缩指令与预留输出（例如窗口 20k、输出预算没按系统自身约定夹到 window//4），守卫会以
`COMPACT_FIXED_PREFIX_TOO_LARGE` fail-closed 拒绝。现在该 typed 失败按原话与失败码上报，
不再折成"请稍后重试"，也不改会话游标。

同时处置三条长期红测试（都**不是**产品回归）：`test_tooling_web` 的取消关闭阻塞读用例，其 fake
响应没跟上生产端新增的 `read(size)` 读取上限（TypeError 提前失败，测不到契约）；两条
`test_manual_compact_*` 的 fixture 用 20k 窗口却保留默认 16k 输出预算，与系统自身
`max_tokens ≤ window//4`（`settings/model_profiles.py`）的约定不一致，被守卫正确地拒了。
前者补 fake 签名，后者把 fixture 夹到 4096，并新增一条"typed 失败必须如实上报"的回归测试。

## R249 出口正文不再改写模型的原话（绝对路径 / 工具名）

真机：用户连问三次"要 / 开头的绝对路径"，界面始终显示 `src/`；审计里模型原文其实是完整绝对路径。
根因是出口把**所有**公开模型正文里的宿主绝对路径收敛成 basename（本意是外部 IM 通道防泄露宿主拓扑），
被无条件用在本机 TUI 上。现在按通道决定：本机私有通道（chat/tui/cli/gateway-cli/terminal/local）保留
可用绝对路径，外部与未知通道继续脱敏；流式、终稿、交付投影、commentary 落账、历史回放共用同一判定，
避免"实时看到一套、回放又变回去"。同一轮还收窄了 `redact_executed_operation_labels`：只遮蔽内部协议
形状 `tool/action`，不再把裸工具名换成"相关操作"（真机界面出现过"可以用 相关操作 设置"）。

## R248 回合终态释放未被认领的插话

插话回执把 `expected_turn_id` 当"只能由这一轮认领"的硬约束；用户在主代理收尾窗口插话时，那一轮可能
在认领前就结束，这条用户消息就永远等不到能认领它的轮次。现在 Gateway 回合终态（成功/失败同一出口）
释放仍为 `pending` 的回执，交给后续轮次接手；已 reserved/submitted（进过提示）的回执不动，未释放的回执
仍拒绝跨轮认领。

## R229 运行中请求拒绝（复验中）

真实主请求执行 4 工具轮后遇 400，不能提示模型/工具从未执行或直接归咎密钥。
请求拒绝文案已与配置错误分开；调用事实与重复展示证据见 [R229](../../audits/R229_LIVE_FAILURES.md)。
未知拒绝不盲重试，已有工作不撤销；`.10` 新会话与两个 child 真验通过，旧本机会话仍拒绝。
一次性同 payload 换上游会话号成功、恢复旧号再失败，提示上游绑定问题；未加入正式轮换/重试策略。
追加 runtime_fact 的请求拒绝单列、typed 不可重试先于正文匹配；HTTP 白名单事实进入主请求终态与 TUI。
R229d 原会话仍 400，界面明确显示该状态；不将准确报错标成恢复成功，原任务和配置保留。

## R228 发布候选：Context 与分段摘要

Compact 的当前窗口由压缩计划随进度提供，先保存同代 canonical Context 再通知 UI；不反向导入
agent_core runtime，不触碰模型输入、recency 或费用。失败遥测不阻断主链，旧代写入由 CAS 拒绝。
原本机长会话复验抓到分段摘要失败；分段后改为无执行工具的专用摘要面，保留所有历史字节和自定义要求。
同会话 264.7K → 35.4K、generation 2 → 3 后已继续工具调用；业务正确性仍单独验证，见 R228 台账。

R227 跨窗口真 TUI 已完成 595K → 16K 的 transcript 压缩，但后续找错项目目录，不能认定连续任务通过。
新增结构化原样工具路径投影并排除内部 runs 候选，补定向后继续真 TUI；错误源码丢失结论保留为失败样本。

## R227 压缩失败定位与修复中

真实切模型样本：DeepSeek 大窗口历史约 56 万 tokens，切 MiniMax 后摘要请求越界；原先被包装成
CONVERSATION_PERSISTENCE_UNAVAILABLE。现在保留 COMPACT 错误码和原异常链，提示历史未删除。
尾部预算计入 canonical native 工具历史；超大摘要请求按当前窗口分段，全段成功后才提交一次游标。
本机用户任务只读；定向与测试机真 TUI 验收分开记账，不把代码落地当作验收完成。

## R226 当前模型与用户自主权限

`gateway_status` 的 caller_model 绑定调用者工作片，启动默认值降为 deployment_defaults，避免 DeepSeek 被误报。
权限菜单走认证冷 owner 控制，不唤起模型；主/子工具共读 owner 的明确选择，挂起的精确审批可原地恢复。
路径仍按工作片冻结，其他 owner 不变，子代理不继承 Full Access；`.10` 单 Gateway 两路真 TUI 已验，
包括等待时切自主、并行 child 的 7 次 PTY 和 MiniMax/DeepSeek caller_model；本机新版未切换，旧任务未打断。

> 名称整理：产品统一称 my-agent。历史检出路径使用 `${MY_AGENT_CHECKOUT}`，旧会话及测试目录用“历史…”占位；实际定位以对应提交和 request/run ID 的原始记录为准。本次未移动目录或重命名真实会话。

## R223 执行与工具协议审计

- Shell 127 明确为进程失败，不宣称前置操作没有发生；凭据剥离优先于环境变量安全前缀。
  stdout/stderr 各保留最多 4 MiB 并继续排空，完整性、截断与真实字节数进入结构化回执。
- PTY 启动先预留容量，写入背压共享期限，历史已退出对象有界；Windows 缺 ConPTY 明确不可用。
- 沙箱包含性改成正向 `contained_by_parameter` 声明，不把未声明的危险动作视为已被沙箱包含。
- HTTP 跳转清理跨域敏感头、按状态维护 method/body；缓存容量/过期/刷新、批量抓取参数与字节预算明确。
  连接与读取用总 deadline 中断持续滴流，DNS 解析本身的不可中断边界仍单列。
- MCP stdio 完整分页与目录代次、在途 ID、发送/等锁总期限、取消通知、正文归档和有界 stderr 均补回归；
  不把已保存多媒体 block 等同于已实现所有模型视觉/音频消费能力。
- `schedule list` 按 limit 返回并给出 total/has_more；外部 IM ACK 丢失和高吞吐摄取仍须专项环境验收。

## 正式库提交前主链修正（本地定向，真实通道待验）

规模 worker 不再直接调用未认领的 `_run_gateway_ask`，改走同一持久入队、精确 claim、执行和终态链。
请求携带真实 user/group 通道身份，相同 trace 与输入摘要重放不再重复执行；结果读取公开 response 投影。
Gateway 的 Compact 临时工具读取移到 `tooling/tool_search_state.py`，消除反向导入执行循环，没有授权豁免。
已通过规模私聊连续历史、另一会话隔离、同群成员共享和同请求重放定向；不宣称真实飞书测试通过。

## R221 已完成公开过程先持久再投递（本地通过，待真实验收）

按会话运行时 session.send_event_raw_with_persistence/record_conversation_items读取持久先行链，及终端交互
sessionStorage.insertMessageChain、messages.handleMessageFromStream的完整消息与增量分离，适配本项目canonical账本。
共享sink逐块写display记录，Gateway冷热owner按typed显示能力投影process_event；TUI原位补缺口，不动任务/权限。
13文件288focused通过；.10仍不可达，未部署，不把本地重开当真实Gateway安全点恢复通过。

## R219 最近上下文独立于 Working（真实 TUI 分项通过）

主/子使用同一 preflight 数字记录到精确 thread，空闲 activity 单独返回 context_usage；成功 Compact 原子清代。
客户端区别显式空与读取失败，旧代迟到不覆盖新值；完整/最小模型 bundle 均排除遥测，不能影响缓存前缀。
原 R219 具体工具拒绝 TUI 已返回，未启动被拒绝 PTY；报告仍夸大交互验证，单列质量缺口。
5ad6696唯一Gateway3606014，三页main80706且空闲无Working，canonical/执行/模型账恢复前后相同。
child Compact1后的数值也由其精确thread恢复；任务中断前过程和运行中重启矩阵仍开放。

## R218 客户端复验通过，Gateway未重启

82da8d1独立客户端恢复R217真实任务：8条Bash标题、Ctrl+O头尾与原页一致；canonical消息/用量未变。
唯一3593187继续服务，原递归任务和追加已返回；防火墙排障仍失败。209focused及严格gate通过。
空闲Context与未提交过程仍开放，不能把用户可见标题修复改成额外模型调用或业务重跑。

## R217 真实过程验收与 R218 标题补修

独立1108abc包、唯一Gateway3593187；新owner任务18次MiniMax/0重试，原/观察页实时内容一致。
第三页恢复user/final各一条、18个思考、无Working，canonical及用量未变；但工具终态快照没有started的
invocation副本。R218由公共metadata统一投影已有detail，删除sink重复转换，140 focused通过。
先部署客户端验证旧记录，Gateway长递归任务不重启；空闲Context缺失及提交前崩溃过程仍未封板。

## R217 同会话前台完整过程（本地实现）

原 rich chunk 同时进入共享公开过程 mapper；按宿主请求精确去重，最终块接替候选且随 canonical final/repair
保存完整过程。先收 canonical 消息再补增量，解决思考早于用户输入。137 focused，通过旧能力/取消/缺帧/恢复
边界；新 Gateway 真 TUI 待验，默认子代理耐久 writer、模型请求、审批与任务状态不改。

## R216 真实消息同步分项通过

50cdfaf独立包升级唯一Gateway为3585410；原页/观察页/恢复页同session真实输入与final各一条，无Working残留。
8次MiniMax/0重试，恢复前后canonical消息和用量未变。默认launcher/旧TUI未切换；实时前台过程仍待下一片。

## R216 前台已提交消息（本地实现，待真实 TUI）

沿原消息分页增加显式 Gateway 前台 user/final，同一消息继续使用 canonical ID；旧客户端未声明
foreground_messages 时保留原后台消息合同。发送页在入队前登记真实 ID，通知/历史不会重复追加。
用户块 ID 不再依赖分页中的 index；冷 owner 读取不初始化 Agent，后台续片仍显示。
本片只封已提交消息缺口，执行中的思考/工具同步和原 P0/P1 尚未完成。

## R215 前台审批同步（本地修复，待真实 TUI）

共用 main 标量现在接收 permission_requested/resolved。维护当前工作片精确未决 ID，最后对应回执后解除；
工具/模型进展不能掩盖等待，context 数值仍更新。TUI观察页静态显示等待，不创建额外审批入口。
会话运行时参照 app-server `bespoke_event_handling.rs` 与 `outgoing_message.rs`，前端参考 终端交互 Spinner。
87 focused 通过；完整前台正文 BUG-146 和授权本身不在此片变更。

## R212 模型配置与真实传输（本地修复，待部署）

真实Qwen任务反证：菜单配置在保护线程丢失，普通模型轮回落部署默认，压缩才请求Qwen。
保护线程复制工作片Context；纯模型配置API只解析可信owner与canonical路径，不初始化完整Agent。
58 focused通过，含真实Thread成功/异常和冷owner读写先红用例。旧任务未算Qwen通过，不重启在途模型做假验收。

## R210 真 TUI 分项结果

受控升级唯一 Gateway 为 3494500，原 Go/家庭工具两会话的 main 数字和常规阶段同步；新真实 MiniMax
17 次完成、0失败。原 Go child 同 run、新 attempt 接续并保留 compact17，不证明完整重启/副作用矩阵。
真实发现 permission_requested 没进入共享活动，原页等待确认却显示工具执行中；下一片只补等待显示。
原 Go 的确认框保留；共享正文和 final 增量尚未做，不能把标量通过当完整 BUG-146 通过。

## R210 前台 main 数字/阶段（本地，真实 TUI 待验）

原 chunk 后发布到同一 owner/thread 标量，不增加读请求/模型调用。前台与后台互换时用显示工作片身份
拒绝旧 close/context；显示 task 跟随真实晋升绑定，不混用 RuntimeDB task。115 focused 通过。
一个旧 `/verbose` 测试在未改版也失败：夹具没构造 processing/attempt，先触发真实 active-turn 拒绝；
补齐已认领夹具，保留系统命令必须被路由拒绝的原断言。完整跨窗口正文另片完成，不关闭 BUG-146。

## R209 真复验与下一个缺口

展开/上翻时失联警告与恢复通过，8428 读取夹具已停止，原配置直连；Gateway 3336610 与业务 run 没重启。
新发现 BUG-146：同 session 的 foreground 工具/context 只在发送窗口可见，没有合入 background 主活动投影。
现有 request progress 可证实追问已收到；完整答复、Go/家庭工具整合与全目标仍待验。

## R209 展开提示补缺

R208 真 TUI 的清单恢复和普通主/子故障提示通过；Ctrl+O 二次装饰盖掉底栏，补 frame provider 组合修复。
Gateway 3336610、原 run/attempt 保持；仅客户端 notices 注入，未停业务，也未改模型参数。

## R208 客户端显示健康

沿既有完整快照读取结果和退避，失败标记状态未同步，恢复后清除；主/子页面共用 root 健康，不终结任务。
健康事件不推进模型活动计时；166 focused 通过，真实 TUI 读通道故障与恢复待验；Gateway 无改动。

## R207 旧计划恢复

历史恢复/补页的旧工具会消费 Todo 控制字段并锁住代号，两个定位用例先红。当前过滤只作用于显示 payload
副本，不改 Gateway、账本或模型请求；89 focused 通过，客户端真 TUI 待验。

## R206 客户端复制和失联观察

R205 已部署；原 Gateway 3334055 明确收到 SIGINT 退出，原状态保存后恢复唯一 3336610，原 Go run 继续。
这不证明信号发送者或所有副作用去重。未选中文字的右键报错已部署客户端，38 focused 通过；真实 PTY
无选区右键/不清空 buffer、中文整句复制通过，原截断提示恢复可见；系统剪贴板未核验。
新的同会话客户端出现旧 Todo；断线保留旧 Working 却无失联说明，二者仍待修。当前定向 provider 夹具
只限制标记 owner 一次真实响应长度，结束后仍需安全恢复直连。

## R205 截断的实时与历史显示（本地）

本地/Gateway 共用 typed 长度提示，半截正文仍保留。canonical final/repair 保存结束原因，后台只在 final
写入，历史提示按原消息 ID 幂等重放；child 的实时结束流也显示。3 个历史红测后 11 文件定向 429 passed /
2 原有 xfailed；尚未部署，有界恢复/真实截断复验未完成。当前唯一 Gateway 为 R204 / 3305553。

## R204 后台用户输入与稳定身份分离（本地通过）

K 真实连续任务中 child wake 已有新 request，但背景目标只读旧 task goal，最终答错任务。
现从同 thread 原消息精确恢复，缺失不猜、批量按消息顺序、旧无编号与 Audit 边界保持。
179 focused passed / 2 原有 xfailed，已部署；K/L 原 session 继续，新模型账为 MiniMax-M2.7。
K 正等待新 worker；F/E/I 并行继续，完整连续任务和 child 返回正确目标还未验收。

## R203 计划显示不代表执行（本地，待原 TUI）

前台 phase 与后台 active count 共用快照谓词；空闲 Todo 保留未完成事实但不再闪动或维持周期刷新。
144 focused 通过，原 F 客户端待验；单 Gateway 当前为 R202，后台协议不变。K 截断后提示缺失单列 BUG-140。

## R201 当前代理与冻结正文同源（已部署，原 TUI 组合通过）

Ctrl+O 冻结 snapshot 在代理导航换 store 时同步重绑，详细档位保持、搜索坐标清理；同来源刷新不解冻。
这只改客户端 frame-provider，未重启 R200 Gateway。44 focused 与严格 gate 通过；原 child/孙代理
逐层返回及原 D 上翻首条输入已通过，两会话消息/用量四份 SHA 不变。系统复制仍待验。

## R199 历史更早页（已部署，原 D 分页切片通过）

`/client/history` 增加独立向前字节游标，同 authenticated owner/thread 倒读完整工作片页。
保留实时读游标和模型上下文；损坏/非法游标显式失败，不回落最新窗口。原 D 会话恢复后上翻见到首条输入，
Ctrl+O 回看通过；121 条消息与 8 条用量 SHA 不变，Compact 5。主子切换/复制组合仍待验。

R199 当时唯一 Gateway 为 3231726，现已替换为 R200 / 3235693。R198 的 L 后续排查原请求真实重启后
同 run/agent 继续并 done。F 的协调员等待异常已由 R200 修复，原 run 自行接续；完整恢复矩阵仍待验证。

## R198 请求恢复专属执行权（本地通过，待真 TUI）

真 G 的后台完成唤醒与原请求恢复顺序竞争，导致 canonical final 后又报 execution_binding_changed。
已按 会话运行时 active-turn 归属适配原 claim：先绑定，再经原请求 transition 领取，同请求恢复、终态释放。
启动补交与正常收尾共用 exact-task 原子清理，清理失败不重跑模型。318 focused / 2 原有 xfailed；
旧失败账不改，真实重启与零重复副作用仍待验。


## R196 恢复失败的明确原因（本地完成，未部署）

真实 A 的 shell 超时先产生 UNKNOWN，再被重启恢复挡住；并非 R194 身份丢失复发。原工具循环按次数与
TOOL_TIMEOUT 豁免的旧分支已删除，shell 以退出回执和部分输出表达已知终止失败，真正 UNKNOWN 保持。
恢复收到结构化 `operation_outcome_uncertain` 时返回专用错误，不再让用户看到笼统失败后直接重做。
352 个唯一 focused 分组通过，`.10` 仍为原 R194 单 Gateway；真实 TUI 复验及历史 UNKNOWN 核对未完成。

## R194 冷 owner 和真实执行身份

R193 真重启证明 attempts 与失败预算已分开，但 A/C 的旧展示任务与真实 DB task 不同导致恢复拒绝。
core 现于模型前回写精确执行绑定，RuntimeDB 按 owner/run 的 current attempt 与 PID/start 证明恢复；
管理员启动调和不再是所有用户恢复的前提。停止、transport 换代、工具未知和资源 dirty 保持原硬门。
状态：216 focused / 1 原有 skipped；已部署 R194。原 A/C 同请求双重启后保持同根身份、执行代次 3、
已知处理失败 0，并继续新工具。E 原 child 跨重启继续后完成；完整递归矩阵及副作用组合仍待验证。

## R193 重启恢复不误算处理失败

总 attempts 继续标识派发代次；processing_failure_count 只在运行期租约真过期时累计。启动恢复保留原
回合和既有失败数，不按 lifetime attempts 杀掉长任务。完整终态保存两种数字，诊断不再把服务重启说成超时。
原租约/CAS、UNKNOWN 和用户停止不变，10 秒启动延后保留；本地定向通过后使用原多路 TUI 在线重启复验。

## R191 动态事实增量和一致预检查

对照 会话运行时 `ContextManager::update_world_state`，PromptBuilder 用已知字段产生来源分段；原生 IR 只追加
相对同来源最新值的变化。去掉第二份 ever-seen 基线，压缩后每类最新状态继续保留。
预检查与模型发送共用 `project_native_prompt_history`，探针不提交 IR、动态字段不污染稳定校准指纹。
260 focused 通过，三路原长 TUI 继续测试；真实费用和后续记忆不由单测代替。

## R190 native Compact 的旧状态最低水位

对照 会话运行时 `compact.rs` 的历史替换与当前上下文重建，删除摘要覆盖的连续旧工具区内过期运行快照。
最新 RuntimeFactsTurn、真实用户插话和保留工具尾部仍参与最低水位；探针与提交调用同一个 IR 回收入口。
141 focused 已过，取消/提交冲突还原原上下文。已部署唯一 Gateway R190；原 child 第 96 代后 DONE，main
真实恢复约半窗口后继续。频繁重复状态包和价格收益留给 R191，不宣称经济性已通过。

## R189 重启后的过程流续接

对照 会话运行时 `thread_routing::apply_refreshed_snapshot_thread` 的会话/缓冲交接，为现有易失环增加 stream ID。
客户端确认发布后才更新“流身份+序号”，旧 canonical 消息游标独立；冷 owner 回显旧流、不创建 Agent。
子代理 final 的旧调用遗漏同步修复：原消息库提供 thread/message ID，删除正文推导身份。
228 focused / 1 原有 skipped 通过。`.10` 原 TUI PID 不变跨唯一 Gateway 2492065→2499672，B 旧流游标 55
换新流 0 后实际继续显示主代理思考、工具和 Compact；三个活跃 child 原 ID 恢复，终态 child 不重跑。
B 终态 child 页面展开/返回也通过；不替代未提交过程持久化、完整长历史和任务质量验收。

## R185 后台消息单一来源（已部署，实时通过、恢复排序待修）

对照 会话运行时 `tui/src/app/thread_events.rs` 的快照重基与 终端交互 `assistant/sessionHistory.ts` 的消息游标，
删除后台 report 的 notices 正文副本。原 `/client/notices` 改投影 canonical message 字节页，恢复 history 带
实际末行位置；冷 owner 不加载 Agent，也能只读相同消息源。所有最终显示都复用原 thread/message ID，
相同文本不合并，模型上下文和 Compact 不写入。部署必须同步更换 Gateway/TUI；旧 notices 原处保留。
363 focused / 2 原有 xfailed 与严格检查通过。`.10` B 新后台 final 自动显示且 Working 收起；再恢复后
过程环从 0 重放到 final 后方，不能算完整通过。继续将过程快照和实时增量共同重基，不能跳过未知过程。


## R184 文件目录与运行记录分离（focused 与原会话真 TUI 通过）

R183 真机仍失败：正确旧 Go 绝对路径被重写成 HANDOVER 所在目录。按用户新决定删除写目标回绑和全部
业务目录别名，普通主/子代理使用完整 owner home；新宿主记录留在 runs/，tasks 由模型按稳定提示词整理。
已通过 532 项相关检查，包含跨自己目录读写、owner/符号链接隔离、停止、旧运行恢复与提示词稳定性。
`.10` 唯一 Gateway / MiniMax-M2.7，两路原 session 均继续完成实际读写并推进 Compact；新宿主归档写 runs，
旧业务文件原位修改。任务样本保护与汇报失真另记 BUG-115，不能混作文件权限或 lifecycle 失败。


## R183 同请求旧工作区重入（真 TUI 失败，业务目录绑定由 R184 删除）

- R182 实际 task links 是交接目录→Go 目录→交接目录；request-local 交接 successor 已 superseded，重入
  时唯一候选槽被占用而失败。现只对同路径/同请求的 superseded 槽生成下一代，不覆盖或复活旧终态。
- 重复选择同一活跃 successor 时按激活后的 task_id 判断是否 supersede，不再对比其历史父 id。
  Gateway callback、owner/线程边界、Todo 和工作区写入路径沿用原实现；不新增兼容执行或跨用户放权。

## R182 resume 接入既有 preflight（两路真 TUI 已通过）

- 修复 Gateway 未就绪时 exact resume 提前读历史并退出。薄客户端只向 TUI 传恢复意图，后台 readiness
  后才请求原 owner/session 历史，成功后启动 worker。错误码、退出晚到结果均在原 preflight 收口。
- 不新增 endpoint、队列、任务、配置或重试；普通终端/本地模式和模型上下文拼装不变。对照 会话运行时
  `tui/src/app.rs` 的 bootstrap→resume_thread、终端交互 `sessionRestore.ts` 首次 query 前恢复。
- `.10` 唯一 Gateway PID 2243914；两个旧 session 的 TUI 先启动、Gateway 随后启动，均正常恢复，回看调用
  保持 65/39。后续真实 MiniMax 追问均结束；旧 notice 重复与跨旧任务 rebind 失败作为独立问题继续处理。

## R181 显示层不再二次裁剪回合

- R180 长会话首屏复验反证：读取层已返回全部 53 行/5 user，却因 34 个后台显示组被裁成 20 组而丢头。
  删除 `history_display` 的预览回合上限，投影调用方已经读取的完整窗口；匿名后台消息不能算作用户回合。
- 新回归覆盖 32 条无 request id commentary 不挤掉第一条用户输入；分页与 Compact 前归档仍独立待验。
- R181 原长会话真 TUI 已回到首条八项目调研输入/思考/工具，回看调用保持 61、Compact 2。冷启动并行
  resume 另暴露历史读取早于 preflight 的时序问题，需后续通用修复，不能当成历史损坏。

## 2026-09-05 R180 canonical 历史显示恢复

- 修复 exact resume 仅加载问答预览导致已保存思考、工具和 commentary 不显示的问题。`client_service`
  先解析 authenticated owner/thread，再从相同 canonical rows 返回独立的 `display_events`；模型预览不变。
- `conversation/history_display.py` 只映射公开类型块；native user 内部注入、签名和参数不透传，工具按 exact id
  配对，状态只读 `is_error`，无结果显示缺口。TUI 复用同一 reducer，不回灌模型、不执行、不重新排队。
- 已有真实失败样本为 R179 process-resume，第一轮 canonical 54 native 消息仍在。定向验证覆盖顺序、幂等、
  空/残缺回合、Audit 排除、原数据不变与客户端边界；R180 两个旧会话真 TUI 已恢复并继续交互，只回看不增加
  模型调用。完整 Compact 前过程、child 有界环之外的历史与分页仍属 P1，不能提前封板。

## 2026-09-04 R168/R169 Shell 真机收口与 transcript Compact 缓存面

- R168 `.10` 单 Gateway 真 TUI `ma-r168-110-u381-artifact-linear` 已生成 5,000 行工具输出并执行后续 shell；
  canonical archive 保持可读，shell 不再递归备份工具回执，结算后 operation manifest 与无变化 blob 都为 0。
- R169 对账发现 R166 cache-safe fork 只覆盖 live-tool Compact；旧 transcript Compact 会把全部历史重拼为冷
  prompt。当前 Gateway 前台/后台、手动 `/compact` 和每层 agent thread 共用
  `conversation/compact_provider_surface.py`，保持普通轮的 stable prompt、system、tools、canonical native
  messages，只在末尾添加摘要请求。工具 Schema 不获执行入口；overflow 后未消费的 deferred tools 只从 typed
  archive 恢复。287 项相邻 focused 通过，`.10` main/child/grandchild 真 TUI 仍待完成。

## 2026-09-04 R167 Shell 产物保护迁出任务树

- R165 的项目级 pytest 重复收集不是 pytest 配置问题：foreground `run_command` 启动前把 ready
  `test_*.py` 原名备份到当前 task，pytest 在同一次命令里看见框架副本；unchanged 分支又从不删除。
- 备份根现由 owner HomePaths 显式注入 `data/artifact_backups`，不随 task cwd、Full Access 或 owner wall
  有无漂移。内容以无原扩展 hash blob 原子落盘，公共 registry 仅保留 opaque owner-local ref；无变化副本
  当次删除。终端交互 提供项目外哈希文件历史对照，会话运行时 提供 temp/rename 与漏件生命周期对照。
- focused 已覆盖 pytest 单 node、no-op 清理、修改/损坏保留、两 owner 隔离、Full Access 外部 cwd、路径逃逸
  与原子失败 fail-closed。`.10` 单 Gateway MiniMax-M2.7 真 TUI 待部署；后台 process session 的退出后复核
  仍是明确未覆盖边界。

## 2026-09-04 R163/R165 Compact 真机与历史续作占位收口

- R163 `.10` 长 TUI 已证明 child overflow 会先推进 exact child ConversationThread；DeepSeek child
  `118,383→71,087`、generation 1 后继续，8 名 child 全部 DONE。main 五代 checkpoint 严格递增，47 个
  live-tool source id 不重复；gen1/4/5 偏浅是后续成本问题，不再把有效 generation 说成漏记。
- Gateway 普通回合动态尾部新增最多四个同 owner/thread completed task-path 候选。模型自行判断是否与当前
  请求有关；候选不设置 cwd、不授予写权，真正续作继续走 exact mutation rebind。
- rebind 的 successor、thread/request binding 与本代 Todo 迁移全部成功后，只清除身份精确且未被修改的
  占位脚手架。R165 `.10` 同一 TUI 的 A→无关 B→自然续作 A 分别通过 23/17/26 项定向测试，第三轮最终
  Todo/回复收口，物理 tasks 只有 A/B 两个业务目录，superseded link 保持可审计。

## 2026-09-03 R162 exact workspace rebind 携带本代 Todo

- 真 TUI 最终 `task_progress` 已正确提交原 q1..q5；失败来自 rebind 前后的 task-path ledger key 改变，不是模型
  漏字段。目标历史项目被正确修改、测试也通过，但 TUI 只能读到占位账本的旧状态。
- `bind_current_conversation_workspace` 现在记录切换前后的结构化 workspace，只把 exact request display
  generation 从源 task-path 账本合并到目标账本；目标落盘并复核 item ids/generation 后才移除源文件。旧代、
  无源账本和同路径均不动，原始 tool archive 保留。
- 失败先行回归已证明修前原 `{id,status}` 被拒、修后直接收尾；相关 task progress、Gateway、tool loop 与 TUI
  投影 focused 全绿，R165 `.10` 同类真实 MiniMax-M2.7 TUI 已验收。

## 2026-09-03 R156 历史检索携带精确任务目录

- Gateway completion 过去只索引 response + prompt，会覆盖 queued/processing 阶段已有的
  `conversation_runtime`。同一 thread 后续说“回到上次项目”时，模型虽然会调用 `session_search`，
  但只能看到回答中的相对路径，因而可能在新任务目录重建项目。
- completion 现在沿用 done request 的 host-authored request/thread/task/path；LocalStore 仍只是搜索索引，
  `session_search` 只为 `gateway_request` 返回 typed `task_ref`。写入旧项目仍由统一 write boundary 的
  exact path rebind 二次裁决，搜索命中本身不授予写权限。
- 该阶段不恢复 terminal sticky task；R165 后续只增加最近四个同 thread completed path 的有界动态候选，
  更旧历史仍按需搜索，因此兼顾长会话续作、owner 隔离与缓存稳定前缀。
- `.10` 真 TUI 首轮发现：引用和检索返回都正确，但“回到刚才那个……”没有把历史工具排到推荐首位，模型
  先用文件工具遍历 owner，遇到多个日志分析项目后猜错。R157 补全 `session_search` 的自然续作 hints 和
  `gateway_request` 查询示例；完整默认工具快照下该工具现排首位。这个修复只改变软推荐，不改变 Schema、
  权限、任务状态或写入裁决。
- R157 二次真 TUI 又暴露：工具已被调用，但默认 5 条命中被普通消息和本轮 processing 请求占满，
  排在后面的已完成项目 task_ref 仍不可见。R158 对宽检索在本地最多取 20 条，再用 host-authored
  终态 task_ref 稳定分组；不读 prompt 正文做分类，不变更组内 FTS 顺序，不赋予写权。
- R158 第三次真 TUI 中 typed task_ref 已稳定排第一，但模型把它缩为 `tasks/...` 后，文件工具又按
  当前占位 cwd 解释，形成 `current/tasks/...`。R159 在共享参数规范化器中以结构化 owner wall 为唯一基准
  还原 owner-relative 任务地址；所有读写边界与 exact mutation rebind 继续在后续逐层检查。
- R159 干净真 TUI 证明 Full Access 管理员没有 owner wall 时上述分支仍无法运行。R160 把
  `canonical_owner_home_root` 作为独立地址事实写入每轮工具边界，`effective_owner_scope_root` 仍只表达
  实际安全上界；地址与授权不再复用同一个“有/无”信号。
- R160 真 TUI 的 tool-output index 同时记录正确旧任务 `model_parameters` 与被套入当前任务的执行
  `parameters`，定位到首轮 owner-home→task-root rebase。R161 在这条纯路径转换里保护 canonical
  `<owner>/tasks/...` 绝对地址；不保护普通 owner 子路径，也不改变任何 read/write grant。206 项相关 focused
  通过。R161 `.10` 真 TUI `ma-r161-110-history-address` 已完成 A/B 两个独立项目，再自然续作 A：写入只落原 A，
  pytest 从 23 增至 30 项，未产生嵌套 `tasks/`；同轮 Compact 自然提交 generation 1。回绑前占位 task 留下
  `ABANDONED` 元数据壳，已作为独立历史投影/目录整洁问题进入 ROADMAP。

## 2026-09-02 R155 终态工作区不隐式吸附，精确写路径可回绑

- `_gateway_workspace_task` 现只选 exact request `conversation_runtime`、thread Goal 或 active task。
  terminal ordinary sticky 只用于 UI/导航，新请求在 owner home 起步并懒建自己的任务目录。
- 写工具的显式目标路径仍在统一 tool runtime 入口裁决。同一 canonical task root 下多个
  completed/interrupted generation 按 `created_at + task_id` 选最新一代；当前占位 link 转
  `superseded`，再把 handler cwd、write roots、request runtime 与 thread pointer 一次同步到旧项目。
- 本机真 TUI `ma-r155-local-workspace-routing` 以无关 CSV 任务证明新目录，以旧文本
  检查工具的 Markdown 报告证明多代同根回绑。测试期间始终只有一个
  `local/main` Gateway，owner 只由 thin TUI 请求携带。

## 2026-09-02 R154 Compact 中断贯穿所有提交安全点（主代理真 TUI 通过）

- transcript、active-turn archive 与 native IR 统一读取 `CompactInterruptCheck`。摘要前后、候选改写前后、
  checkpoint 前后和 CAS 前都能中断；用户停止只发布中性 superseded，不再计入 provider 失败熔断。
- 本机 MiniMax-M2.7 真 TUI `ma-r154-local-compact-cancel` 在 active-turn 摘要 20% 时 Esc：request/task
  interrupted，operation `candidate_discarded`，thread 保持 generation 4/failure 0；同一会话随后正常续作。
- focused 覆盖五个竞态位置；主代理路径已验收，child/grandchild 自然真机停止仍待后续封板矩阵。

## 2026-09-02 Compact 来源与提交权统一投影（Focused 通过，真 TUI 待验）

- `conversation_compaction_progress.v1` 现在显式携带 `source_kind/commit_authority`；完整 transcript、可持久
  active-turn 工具归档与 turn-local 工具整理各有唯一合法组合，Gateway 不再复制一份独立 schema。
- Gateway rich chunk、后台 transcript 与 TUI 共同使用 `conversation/compact_progress.py` 校验。旧 v1 两字段
  同时缺失只投影为 `legacy/legacy`；部分缺失、未知或矛盾组合直接拒绝，展示兼容不能取得持久提交权。
- TUI 根据 typed 来源显示不同中文动作，`compact N` 仍只来自 canonical checkpoint/CAS。相关 223 项
  Compact/Gateway/TUI focused 测试通过；同 wheel 的 MiniMax-M2.7 真 TUI 仍需补标签和自动触发证据。

## 2026-09-02 R148 会话任务与 TaskRun 持久终态对齐（本机真 TUI 通过）

- 普通会话和 Goal 原本会先把持久 `ConversationTaskLink` 标为终态，再结束主代理；旧 RuntimeDB 收口仅依赖
  一轮内的临时属性，导致 `AgentRun` 已结束但 `TaskRun.closed_at` 仍为零，启动扫描会把已完成工作误报为在途。
- 当前收口只信两类结构化事实：同一 canonical task 的持久 link 已处于不可复活终态，且 exact TaskRun 下
  包含唯一 root 的整棵 AgentRun 树全部终态。最后一个 root/child 收口边都执行同一 CAS；Gateway 启动发现层
  还会重放一次，覆盖两类事实之间断电或崩溃的窗口。模型措辞、质量判断和 UNKNOWN 工具操作均不参与裁决。
- 本机 `ma-r148-local-goal-taskrun` 通过真实 `/goal` 完成 CSV 数据质量工具、15 项测试与示例；模型先成功调用
  `update_goal(complete)`，随后 root AgentRun 结束。最终 TaskRun 为 `done`、`closed_at` 非零且仅有一条
  `task_run.closed`。相关 117 项 focused 测试通过。

## 2026-09-02 R143 模型前置失败统一收口（本机真 TUI 通过）

- Gateway 请求终态原本能正确返回 failed，但主代理 RuntimeDB attempt 在 provider capability probe/上下文准备
  抛错时仍可能保持 running；根因是权威 attempt 已绑定，而 `runtime_mixin` 的异常边界从正式模型循环之后才
  开始。该缺口会让启动扫描误报崩溃卡死任务，也会给恢复器留下错误执行权事实。
- 当前复用唯一 `_settle_main_agent_run_exception`，把异常边界扩到已绑定 attempt 后的整个执行体。Gateway
  请求归档、provider typed retry、取消和 RuntimeDB CAS 均未另造分支。
- 本机独立 wheel 的 `ma-r143-local-preflight-closeout` 先缺密钥失败：run/attempt 都是 failed 且 ended_at 非零；
  再恢复同一 Gateway 和 TUI，下一轮完整单位换算 CLI 自然 done。focused 34 项及 Gateway/runtime 扩展 39 项
  通过。

## 2026-09-01 R121 空 Goal 工作区与真实丢失目录分流（最终 wheel 真 TUI 通过）

- `.10` 同一 fresh Goal TUI 的后续消息返回 `CONVERSATION_PERSISTENCE_UNAVAILABLE`。结构化诊断定位到
  `gateway.conversation.workspace_task`：纯聊天 Goal 没有调用工作工具，task link 的 `task_path` 合法为空；
  旧加载器却把空路径与一个曾存在但后来消失的非空 sticky workspace 合并成同一种损坏。
- `request_execution._gateway_workspace_task` 现在先保留原始 `configured_task_path`：exact selection 的空路径
  不产生 load error，继续使用 thread cwd；只有非空配置路径无法解析为现存目录时仍 fail closed。该改动不
  创建空任务目录、不猜用户意图，也不放宽 owner 或 workspace 权限。
- 新增正反回归：空路径 Goal 的下一轮实际 `_run_gateway_ask` 成功；移除一个已绑定的非空目录后仍产生精确
  workspace load error。整个 Gateway 会话上下文测试文件已通过；最终 wheel 在原 active Goal thread 中的
  后续消息、直接 final 与 canonical user/final 已真 TUI 复验。

## 2026-08-31 Gateway 会话车道与孤儿恢复解耦（R118 真 TUI 通过）

- `.10` 单 Gateway 出现 durable wake 已 ready、日志持续 seed owner，但 `bg-owner` worker 长时间空闲。
  `py-spy` 精确定位 background-main supervisor 卡在
  `prepare_tick -> supervise_stalled_orphans -> RuntimeDB`；同一 Gateway 的独立
  `_GatewayOrphanReconciler` 此时已经在并行负责死亡 runner 和孤儿恢复。
- Gateway 规划会话时现在显式使用 `prepare_tick(include_orphan_supervision=False)`，先提交 ready thread
  lane；独立 reconciler 继续有界恢复。直接/嵌入式同步 scheduler 保留默认 inline sweep 作为无 Gateway
  时的 crash-recovery 兜底，不因本修复失去恢复能力。
- focused 已覆盖 Gateway 对 base/owner 都传 false、同 owner 多车道、跳过与保留两种 prepare 语义；fresh
  MiniMax-M2.7 u328 已证明直属 child 完成后 main 自行续跑，u331 又证明孙代理→coordinator→main 两级 durable
  wake 都无需用户发“继续”，最终回复直接显示且 `Working` 撤下。
- 首个新 wheel 真测进一步暴露 builtin role JSON 未进包：coordinator 的结构化模板快照为空，递归工具被按
  leaf 删除。package-data 与 distribution boundary 现把 builtin role catalog 列为强制资源；同时把 Gateway
  active-turn 恢复依赖的 owner-root helper 下沉到 `user_space.runtime_paths`，清掉 gateway-parts 反向依赖
  agent-core 的已有分层违规。最终 wheel `0f8f97e8...` 已包含六个 builtin JSON；u331 中 coordinator 直接获得
  递归工具并创建两名 depth-2 researcher，三条 canonical state 全部 DONE、parent/root 血缘一致。

## 2026-08-31 R116/R117 上下文校准与后台 final 连续性

- provider 成功响应现在把数值 context observation 写入 exact thread；读取时要求稳定请求指纹和 Compact
  generation 都匹配。u323 八 child 完成后主 compact 0，立即追问命中 32,418 cache-read；u324 自然越过
  90% 后只提交 generation 0→1 一次并继续，旧频繁 Compact 未复现。
- u323 暴露本地 TUI 后台 final 只进 notice、不进 messages。R117 将 `tui` 加入 transcript delivery channel，
  复用现有 background transcript 幂等键提交 commentary/final；notice 仍独立投影，不取得历史权威。
- 首次 u325 因 Gateway 使用旧 checkout editable venv 而导入 schema v7，保留为部署负样本。u326 在追问前
  核对到 schema v8、6 条 commentary、1 条 final，随后召回正确。
- 最终发现仅改 cwd 仍不足：旧 executable 与 editable `.pth` 都指向 `/root/my-agent-src`。当前使用独立
  `runtime-venv-efa90d1` 普通 wheel；唯一 Gateway 为 `ma-gateway-efa90d1-110-runtime-venv`，
  MiniMax-M2.7，8420 单 listener。u327 的 2 child、245/198/290 行产物、追问前 4 commentary + 1 final 和
  零工具准确召回全部通过。默认 `/usr/local/bin/my-agent` 也已备份旧 symlink 后指向同一新 venv，
  防止后续用户 TUI 继续加载旧客户端。

## 2026-08-30 R114u/R114v 公平 Compact 让出与中性服务 cwd

- 旧 u305 在一个后台工作片连续推进 8 代后被普通 RuntimeError 误记失败，随后才靠下一次扫描继续；当前保留
  8 代公平上限，但用 typed yield 干净释放 claim，原 wake 不消费、policy 不记失败，下一片从 canonical
  generation 续跑。两条 failure-first focused 已转绿，普通异常和无代次进展仍 fail closed。
- systemd/launchd unit 不再把安装时 checkout 写成运行 cwd，统一建立并使用
  `<MY_AGENT_HOME>/service-cwd`。它不承载 owner/task/workspace，也不改变配置或密钥来源，只消除源码阴影。
- wheel `41f752d...` 已部署 `.10` 唯一 Gateway；从 `/root` 启动且模块来自 site-packages。重启时 u315 七个
  live child 精确 reclaim/revive、名册无重复。fresh 自然跨 8 代仍观察中，不用旧样本冒充新真机证据。

## 2026-08-30 R114t pending turn cwd 投影

- fresh 长任务证明：首轮模型看到 owner home 后会把它复制进同批 child goal；首个工作工具随后建立的
  canonical task root 与这些自然语言绝对路径冲突，安全墙拒绝但任务空转。
- 当前 pending 模型投影隐藏 owner/宿主绝对路径，只保留“相对读取、工作工具固定任务 cwd、相对新产物”三项
  事实；canonical context bundle 文件仍完整保留内部 refs。该变化不预建闲聊任务、不解析 prompt、不重写
  child goal，也不放宽 owner wall。任务晋升后的现有 cwd 主链不变。
- `gwreq-*` 同时进入机器 ID 识别，防止目录标题退化成请求号。本地 94 项 focused 与 `.10` u313 fresh
  真 TUI 通过：6 名 child 同一 canonical task root，owner-root 泄漏和路径拒绝均为 0。

## 2026-08-30 R114r 跨工作片 active-turn Compact

- `.10` 长任务证明当前 request 的完整工具 archive 会跨多个 `Agent.run` 恢复，但旧链只把它临时变成有界
  handoff；屏幕 Context 会下降，ConversationThread generation 却仍是 0，既让用户误判 Compact，也会让
  后续恢复再次把旧调用喂给模型。
- 当前完整 archive 与模型投影正式分离：预算、去重、已执行工具和副作用仍读全账；只有 thread 指针确认的
  `live_tool_ir` checkpoint exact source ids 从 provider 视图移除。孤儿 checkpoint 和自然语言没有隐藏权。
- Gateway/background main 的 overflow 后备不再读取“有进展”布尔值放行重试。transcript 无法推进时必须用
  同一 live checkpoint/CAS 提交 active-turn replacement，复用六字段摘要、失败熔断和 5/20/65/82/92/100
  进度；提交失败就停止，避免重复烧相同输入。focused 与 `.10` u314 fresh 长任务通过：8/8 child、三代
  canonical Compact、367 行最终报告和 provider cache-read 账均连续。

## 2026-08-30 R114o TUI 快速就绪首帧

- `.10` 唯一 Gateway 下批量新开 scoped TUI 暴露首帧竞态：进程和 worker 均已就绪，但屏幕长时停在
  “正在启动交互界面”，输入任意键后立即正常显示。这证明是 preflight 结果早于可见帧
  被消费，不是 Gateway 慢或模型慢。
- R114o 把 readiness 结果投递回 prompt_toolkit Application loop，同一回调内完成 runtime 状态、
  worker 启动与 invalidate。本地 deferred-loop 合同已通过；`.10` 在同一 Gateway 下同时新开
  8 个 owner TUI，不按键 5 秒后 8/8 自动进入正常首屏。

## 2026-08-30 R114n Compact 恢复目标留出长回合空间

- R114l 八子代理任务最终自然完成，但 main 的 90% 触发、约 81% 恢复使一份长整合报告立刻触发下一代，
  真实出现约 119.4k→100.1k、115.4k→94.7k 和总计 `compact 3`。
- 会话运行时 用摘要 + 最多约 20k 用户消息替换旧历史；终端交互 用 boundary + summary + 有界附件重建并监控
  `willRetriggerNextTurn`。当前新增 `memory_compact_recovery_target_percent=60`，与 90% 触发分离并由统一
  `RuntimeCompactPolicy` 同时供 main/child、live-tool/transcript 使用。
- 目标取窗口 60% 与 trigger-minus-tail 的较小值；128k 默认不高于 76.8k。旧会话基线已经高于该目标时
  跳过注定贴线的 live summary，直接进入 canonical transcript Compact，不改变 checkpoint、generation、
  active-turn、owner 隔离或 raw transcript 保留。focused 与 u314 fresh 长 TUI 通过；三代替换的是不同
  transcript/live-tool source，提交后的 source projection 均低于恢复目标。

## 2026-08-30 R114m 本地搜索默认正则

- 真 TUI 恢复任务暴露：模型按 会话运行时/终端交互/任务运行时 的 ripgrep 习惯传 `A|B`，旧
  `search_text` 却默认 literal，因而返回结构化但错误的本地空结果。
- 当前 schema 与两条执行后端统一改为默认 ripgrep 正则，精确原文显式 `literal=true`。网络边界、owner
  路径墙、非法正则错误码、分页和输出模式不变；58 项 focused 已通过，待下一 wheel 真 TUI。

## 2026-08-30 R114l 后台 Compact 原地续跑

- 真实 R114k 长任务的四次 live Compact 都在 measuring 后 superseded：候选未推进 generation 是正确回滚，
  根因是被删除工具对所属的长 assistant 思考正文仍留在 native IR，摘要后完整请求仍高于 recovery target。
- 现在只有完整 replacement summary 已生成时，pair reducer 才把已无保留调用的旧 assistant 工具轮一并删除；
  普通 PTL/window 没有摘要时不动正文，current UserTurn 与 orphan-safe 工具配对合同不变。
- background main 新增与 foreground/child 同语义的外层：`context_overflow` 后携带 typed archive/插话，强制
  ConversationStore Compact，并在同一工作片继续；无 generation 或结构化进展立即失败，不等下一次 scheduler
  policy 重复烧摘要。显示继续复用同一个 Compact 进度块和 canonical generation。
- focused 已通过，wheel 已部署 `.10` 单 Gateway；`ma-r114l-110-u295-compact-research-long` 已完成八子代理长
  调研。任务运行时 child 约 124,049→53,711 token、终端交互 child 同为 generation 1；八名 child
  全部 DONE，八份分报告和横向报告存在，main final 直接显示且 Working 收起。主链通过，连续三代问题转 R114n。

## 2026-08-30 R114 多页 owner 重启恢复与冷通知重放

- `.10` R113 顺序重启唯一 Gateway 后，长任务的 task/thread/run 持久事实完整，最终也能自动恢复；但 u276--u279
  约两分钟才重新出现 Working。日志与 discovery cursor 证明：这些 owner 位于第三页，旧 orphan/background
  controller 在相邻页之间错误等待完整 60/120 秒稳态重扫间隔。
- R114 保留每 tick 一页的有界扫描；页上有 continuation cursor 时下个 tick 立即继续，整轮结束后才等待配置
  间隔。明确 `in_process`/subprocess 形态且 exact PID 已死时，OS 事实可越过仍新鲜的最后心跳；PID 仍活或
  旧账缺字段时继续按原 fresh window 保守处理。
- 对照 会话运行时 resume 生命周期和 长期助手 Gateway orphan lease 回收后，没有新增恢复状态机、第二 Gateway 或
  全量阻塞扫描。`.10` 唯一 MiniMax-M2.7 Gateway 在 u283--u287 长任务运行时顺序重启；
  u287 的 7 个 runner 在 25 秒内原 run revive，八行名册无重复，u285/u286 也继续推进。
- passive notices 仍优先只读驻留 owner；cold owner 只用结构化身份扫自己固定深度的已存在会话库，
  重放 exact binding 的已提交 notice，不创建目录也不初始化 Agent。u283 原 TUI 在 Gateway 重启后已补出
  之前屏幕缺失的完整 final。
- child 用户插话在 provider consume 后同时写 exact child thread 和 durable agent transcript；重连客户端
  没有本地 pending map 时依 exact message id 重建 user block。u287 已通过 TUI 进程退出/resume 和
  Gateway 重启两层复验，补充要求仍在 researcher-7 原时序位置。
- u288 真实长任务进一步暴露 runtime/task 双投影收尾竞态：宿主 terminal event 已提交而 task 仍 RUNNING 时，
  旧 supervision 会把 `done/failed/cancelled` 一律当作“安全重排队”，已完成 child 因而重复执行。当前恢复
  先区分 natural model-turn closeout 与 recovery-generated closeout：前者复用 canonical runner-result service
  补写 task、runner session、父级 wake 和结构化覆盖，不再 abandon/requeue；后者继续按原崩溃路径恢复。
  参数化 focused 已覆盖完成、失败、取消和原 dead-runner requeue，真 TUI 重启复验列入 R114j gate。
- R114j 的 fresh/restart 复验已确认 u288 九名终态 child 不增加 generation，u289/u290 的 live attempt 才按
  原 run 恢复。压力样本同时发现主请求的未完成回合没有从 transcript 恢复工具历史：u290 原先已建 10 项
  Todo 和 6 名 child，Gateway 重启后模型只看原 prompt，又追加 6 项同义 Todo。
- R114k 复用现有跨进程工具输出索引和 Compact carried contract。requeue 落 typed active-turn marker，执行端
  按 exact owner + `conversation_request_id` 读取主回合工具调用并在第一次 provider call 前重建工具去重、轮次、
  参数和 operation refs；不读取 child 索引、不解析 assistant/错误正文。索引读取失败直接停止自动重放。

## 2026-08-30 R113 Todo 每轮纪律与 Compact typed 失败展示

- R112 fresh 长任务已经证明八个 child covers 项会持久追平到 8/9，但 MiniMax 在自然 final 前没有显式关闭
  root 自己负责的最后一项。R113 参考 会话运行时/终端交互，把“完成即按 exact id 更新、回复前核对、未完或阻塞
  如实保留”压成 Workspace Context 的一条短纪律；它替换旧说明并受现有开关控制，不新增自动打勾、隐藏
  模型轮、续跑或 final gate。
- 同一 R112 root live Compact 在 20% 摘要阶段收到 provider 失败，canonical generation 保持 0、原上下文
  继续完成任务，但旧投影只留下 `COMPACT_PROVIDERRESPONSEERROR`。R113 让 transcript/live-tool Compact
  优先保留 provider typed `error_code`，经 Gateway 白名单和 TUI 二次校验后在红色终态块显示，例如
  `COMPACT_MODEL_EMPTY_RESPONSE`；错误正文、摘要、prompt 和凭据仍不进入 rich 事件。
- 本地 9 文件组合 264 项 focused 通过；历史 12k Compact fixture 在 R112 HEAD 也处于单 token 恢复边界，
  只把测试窗口调到 13k 留出演进余量，生产触发点、恢复目标、CAS 和失败回滚均未修改。发布后继续用单
  Gateway、多 owner 的真实长 TUI 复验 Todo 收尾、主/子 Compact、连续消息与失败可见性。

## 2026-08-30 R105 十 owner 功能矩阵与 R106 本地候选

- `.10` 唯一 Gateway 同时承载 u246--u255；新增 10 路 TUI 后仍有约 9.7GiB available。Gateway RSS 约
  688MiB，单个常驻 TUI 约 68--110MiB；tmux 未退出时客户端进程保留是当前明确生命周期，不把它计成第二
  Gateway。`.7` 当前 SSH 端口拒绝连接，待恢复后再做 8GiB 同负载分组对照。
- u247 捕获成功命令的隔离视图误导：沙箱内绝对 `/root` 写入返回 0，而宿主无文件。R106 候选让所有
  owner-scoped Shell 回执都投影 `owner_workspace_only/external_host_paths_hidden/host_path_absence_proven`，
  不改变文件墙或网络。
- `/sessions` 候选使用当前 owner 的 canonical session store，只读给出最近记录和精确 resume 命令。
  终端交互/会话运行时 对照都有完整 app state 重建；本轮不以只换 session id 的伪 picker冒充原地切换。

## 2026-08-29 停止受理与工具清单单一快照（`.7` 真 TUI 已通过）

- `/agents/<run>/stop` 不再同步等待完整持久化收口：入口先完成 owner、conversation、parent 和 exact run
  鉴权，以唯一幂等键受理，再由一条后台 canonical cancel 完成 attempt signal、终态和索引更新。TUI 只把
  `accepted` 解释为“停止请求已接收”，继续读取真实 terminal，不能提前画成已停止。
- persistence/runner session 对 terminal state 加单调保护：旧 snapshot 与普通 heartbeat 不得把
  CANCELLED/DONE/FAILED 写回活动态；只有显式 user-stop recovery 可 opt-in reopen。r56 真 TUI 的 accepted
  约 6.06 秒、terminal 约 9.62 秒，未再出现客户端超时后的假“无法确认”。
- `list_tools` 与 provider Schema 现共同投影 exact `ToolRuntimeSnapshot`。旧 manifest 把远程
  `owner_type=user` 错当代理角色二次过滤，结果清单 0、模型却能调用；删除该二次白名单后，r57 普通用户
  返回 30 visible/30 executable，权限仍是 owner-scoped，内部/root-only/unavailable 工具仍隐藏。
- 当前单 Gateway PID `1725815`，MiniMax-M2.7；本地组合 74 项、测试机定向 12 项通过。过程和未覆盖项见
  `docs/audits/TUI_FUNCTION_AUDIT_20260828.md`。

## 2026-08-29 双用户同 Gateway 的文件、Shell 与错误语义隔离

- `.7` 同时启动 `fullcov-r48-u10` 与 `fullcov-r48-u11` 两个 owner TUI，始终复用唯一 8420 Gateway。
  两边自己的文件工具和 Shell 写读均成功，反向文件读写、跨 owner `working_dir` 均结构化拒绝；命令文本
  内嵌另一 owner 绝对路径时由 bwrap 不挂载目标，两个方向都没有生成越界文件。
- bwrap 隐藏目标会让进程看到 `ENOENT`，旧模型据此误报“对方目录未初始化”。对照 会话运行时
  `会话运行时-rs/core/src/exec.rs` 与 `unified_exec/process.rs` 后保留 OS sandbox 主链，不增加任意 shell 文本路径
  解析；owner-scoped Shell 在失败或存在 stderr 时改为投影 `file_scope=owner_workspace_only`、
  `external_host_paths_hidden=true`、`host_path_absence_proven=false`。
- 首轮真实回归又发现 MiniMax 在失败命令后追加 `; echo $?`，导致最后一个 echo 把最终退出码变成 0。
  工具说明已明确禁止用恒成功后缀检查状态；不把 stdout 中的数字当机器状态。fresh request
  `gwreq-1787985268-fd590c551b054c53afb1947caee696a0` 保留真实 `return_code=1`，operation failed，模型
  正确说明“当前不可访问，但不能证明宿主路径不存在”。
- 本地与测试机 49 项权限/沙箱 focused、Ruff、PyCompile 和 diff check 通过；生产改动未达 10,000 行，不跑
  全仓 pytest。测试过程、反例和宿主负证据继续记在 `docs/audits/TUI_FUNCTION_AUDIT_20260828.md`。

## 2026-08-28 WorkspaceOnly/Full Access 与完整 Compact 动画

- 本地 TUI、外部 owner、文件工具和进程工具现在共享同一 owner home 硬边界；空工作区不再继承 Gateway/TUI
  启动 cwd。只有结构化 `local/main + full-access` 可访问外部路径，远程 owner 配置会降权；子代理无论父级
  是否 Full 都恢复 owner/task 范围。文件墙不关闭网络。
- Full 管理员的 prompt 只增加行为提示：明确外部路径或系统排障才离开 home，其他 owner 必须点名且默认
  只读/少改。提示不负责授权。owner 控制面文件继续只读，普通用户自己的文件（含自己的配置）可正常使用。
- 单 Gateway 共享 registry 的 workspace、path policy 与私网字段改为 request-local 副本，避免多 TUI 并发串权。
  子代理内部 task state 与用户项目根分离：相对路径继续落用户项目，内部账本目录只负责状态。
- Compact 统一加入一段 recent-tail 恢复余量；候选未降到恢复目标就不推进 generation。live 尝试与 transcript
  后备使用独立 operation id，失败块不再遮住后备进度；child Compact 进入 child transcript。手动 `/compact`
  从 outbox 入队起显示持续动画，真实回执后才成功收起或冻结失败。
- live-tool 候选不足以达到恢复目标但原 IR 已恢复时，以 `superseded/candidate_discarded` 静默结束本次动画；
  它不写失败熔断、不推进 generation，并释放同代 transcript operation。真实摘要调用、checkpoint 或 CAS
  异常仍走红色 `failed`，两者不能混成同一种用户提示。
- 本地权限/沙箱 focused 与主/子/manual Compact focused 均已通过；生产改动未达 10,000 行，不跑全仓 pytest，
  推送前仍执行 Ruff、doc sync、strict code-size、diff 与 clean-package 严格门。
- `.7` fresh `ma-f6d58c0-compact-superseded-r3` 已用官方多子代理超级玛丽任务复验：8/8 child、Todo 10/10、
  终态 DONE；live 候选退出不再出现红字，后续 canonical Compact generation 1、failure count 0，模型继续记得
  全部文件与验证步骤。测试期间始终只有一个 `ma-gateway-f6d58c0-minimax`。

## 2026-08-28 后台三车道统一最终回执与会话 Compact 权威

- `.10` 真任务证明 due policy 已提交最终 assistant/任务终态，但附着 TUI 没有 notice；旧实现只在 child
  wake 车道调用 `_record_background_notice`。当前 child wake、observation、due policy 都经统一
  `_append_background_report`，可交付正文使用既有 `background_notice.v2`，suppressed/空正文不变。
- 后台 main 使用 `save=False` 只是避免 `Agent.run` 与 `_commit_background_response` 双写最终回复。只要工作片
  携带 exact task/thread 并标记 transcript authoritative，它就与前台共享 ConversationThread Compact
  checkpoint/CAS；普通辅助 no-save 仍不允许落盘。focused 已覆盖 policy 正反面和真实 overflow 提交。
- carried 工具恢复统一缩减大参数并保留最新尾部，使后台续片不再反复发送整份文件正文，也不会在超限时
  丢掉最后 write/status。待 `.10` fresh 真 TUI 复验 footer generation、直接 final 与后续模型连续性。
- 原有进度条只由 transcript Compact 发事件；live-tool Compact 结束前长期没有可见阶段。当前它也从摘要
  开始前发布同一 typed progress，按真实 pipeline 原位更新，TUI 中文显示 spinner、进度条和当前阶段；
  child 的 BackgroundTranscriptSink 与 main 的 Gateway writer 复用相同事件，不新增前端计数事实源。

## 2026-08-28 完成回合原生历史与缓存前缀收口

- Gateway 每轮最终化时同时生成 legacy transcript、canonical native envelope 与 artifact projection；三者
  来自同一批有界 conversation rows。下一轮的 `system/tools/messages` 前缀不再从展示文本重新推断工具调用，
  已完成的 assistant tool call/result 以原生配对历史精确回放。
- 当前用户输入与本轮 append-only runtime facts 只出现在当前 turn IR，旧轮事实留在 canonical envelope；
  自然语言追问不会重写历史前缀。真实 MiniMax 同线程后续轮已从 `cache-read=0` 恢复为 provider 命中，
  Compact 仍是唯一允许替换旧历史前缀的状态迁移。
- Gateway 对 legacy 展示历史和 canonical native history 使用同一个 bounded row selection，避免一侧已裁剪而
  另一侧仍无限增长。历史缺失或旧数据没有 envelope 时 fail-soft 到现有 transcript，不伪造 tool pair。

## 2026-08-27 跨普通回合的 canonical messages 缓存前缀（本地候选）

- MiniMax 长任务结束后的普通追问虽然能正确回忆，但旧 Gateway 把 transcript 渲染进每轮变化的 runtime
  injection，provider 账出现 `cache-read=0`。当前 Gateway 只生成一次已经 Compact/完整消息裁剪后的
  `ConversationHistorySeed`；native runtime 按旧 summary/历史、当前 user、本轮工具 IR 的时间顺序投影，
  text runtime 从同一 seed 渲染，双方不再各自回读 transcript。
- `CacheStructuredPrompt` 保留完整诊断字符串；Anthropic/OpenAI 原生适配器省略其中已在 messages 出现的
  current-user 副本，防止 token 与上下文压力重复计算。记忆召回、推荐工具、工作区、wake/runtime injection
  和执行事实统一放在消息尾部；只有真正 Compact 才替换旧消息前缀。
- main/child 相关 native、Gateway、Compact、上下文压力和模型账 focused 已通过。`.10` 原
  `ma-110-dispatch-cache-r1` 同一长会话部署后连续两个普通中文追问分别得到 24,168/24,388 provider
  cache-read，第二轮只发生 1 个物理调用且准确续接上一排序；不能用模型记得内容或屏幕 Context 代替该
  provider usage 证据。

## 2026-08-27 慢模型首事件与滚动 idle 分相（本地候选）

- r64 的本地模型 child 持续工作后仍被 600 秒固定流总墙钟误杀；这不是进程、Gateway 或模型完全静默。
  会话运行时 的 Responses SSE 对每次下一事件单独做 idle，通道运行时 另分 first-event 与后续 gap。
- 当前候选删除流式固定总墙钟，保留 connect、按本轮输入量估算的 request-local first-event 与首事件后的
  rolling idle。有效 SSE data 到达后，watchdog 和 socket 都切回稳定 `request_timeout`；空行/注释不续命。
- 默认慢速估算 200 token/s prefill、20 token/s output、安全系数 2、最大 10,800 秒，支持约 1M 输入的
  极慢预填充；小请求仍按 token 量得到短预算。共享 backend 字段不再被并发 main/child 临时修改。
- fake transport、真实 loopback SSE、账本 stage 与 backend/native 相关 focused 已通过；`.7` r65 已证明有效
  慢流不会被 600 秒误杀，也证明“连接仍活着”不能冒充任务推进。持续 8 小时以上后已按用户要求从 TUI
  `/stop`；后续真实长任务和 会话运行时/终端交互 对照统一使用 MiniMax-M2.7。

## 2026-08-27 thinking block-stop 先于正文（已部署真机验证）

- r55 原始 chunk 的最后一轮为 `thinking_delta* -> model_delta* -> assistant_thinking`，所以 TUI 已在正文开始
  时封口增量块，迟到全文又在 final 后创建第二块；这是 producer 顺序错误，不是模型完成后再次思考。
- 对照 会话运行时 reasoning delta/final 同 item 生命周期与 终端交互 `content_block_stop` 后，Anthropic collector
  在原 thinking block stop 调用同一 observer 的 typed `complete`；backend capability 控制该方法，已发布的
  物理调用跳过 response fallback。TUI 只为旧 chunk 消费一次迟到终态，不以文本相等判断去重。
- collector 顺序、response 不重放、旧流最终块三条失败优先回归已先红后绿；完整 focused 与严格 gate
  通过。`ae3fd1e` 已推送、快进部署 `.7`，原位重启后唯一 Gateway PID `1416524`、模型 MiniMax-M2.7。
- 恢复 `ma-r55-terminal-sticky-retest` 后用普通中文发起真实请求：chunk 2--94 为 thinking delta，95 为唯一
  thinking terminal，96--117 才是正文；TUI 显示一块灰色 Thought 后接 final，最底稳定块为 assistant。

## 2026-08-27 终态 sticky cwd 在首采样前生效（已部署真机验证）

- r55 证明 terminal successor 已继承原 task id lineage 与 task path，但模型第一轮仍先看到客户端启动 cwd；
  第一条 `run_command` 把这个旧目录写成绝对路径，后续 pre-handler promotion 已无法修正命令。
- `_gateway_task_attributes` 现在把 exact non-detached sticky `task_path` 直接投影为本轮
  `conversation_execution_cwd/runtime_workspace_roots`，使模型 Workspace Context、工具、审批和进程共用
  一个 cwd。completed 仍不预填 live `conversation_task_id/run_workspace`，纯聊天不会复活旧任务。
- completed/interrupted × read/list/search/find/write 的 10 项首工具回归已先红后绿；完整 focused 与严格
  gate 通过，`1e4c64d` 已推送并部署 `.7` 唯一 Gateway。原 r55 同提示复验的首个列表、审批、bwrap 与
  Python cwd 都在原 task root，localhost 正文为游戏 HTML；三个连续终态 request 的 `task_path` 精确相同。
  Mac 外部连接仍失败，模型保持未验证，故网络可达性不计为本切片通过项。

## 2026-08-26 owner TUI 子代理审批桥（本地候选）

- `/client/notices` 只有在客户端显式声明 `client_capabilities.tool_approval=true` 时才续 root consumer lease；
  pending rows 是有界只读投影，不能批准工具。
- `/client/agent-permission` 复用 agent control 的 owner/thread/root/current-attempt 授权，再对 canonical full
  request 做原子比对。stale/mismatch/unavailable 使用 typed HTTP 错误，不回退成聊天文字或自动重试批准。
- TUI 将 Gateway rows 与前台审批放入同一 FIFO，写回失败保留当前面板。薄客户端、Gateway service、真实
  sink、并发 UI 等 179 项 focused 已通过；待严格 gate、部署和 fresh 真 TUI。

## 2026-08-26 空输入 ↓ 返回当前视口尾部（已部署真机验证）

- r37 真 TUI 的 child 视图证明 canonical 历史和新事件都在：`Ctrl+Home` 离尾后出现 `1 new message ↓`，
  `Ctrl+End` 可达最新消息；缺口仅是普通 `↓` 被空输入的 child/history 导航提前消费。
- 当前候选让普通和详情 transcript 通过唯一 `is_following()` 暴露 typed follow 位。输入为空且离尾时，
  `↓` 复用既有 `end()`；已贴底或输入有字时保持原编辑、selection 和 history 行为。
- 该改动只影响本地 TUI 展示状态，不读 footer 文案，不改 Gateway、ConversationStore、消息投递、Compact
  或任务控制。6 个相关文件 145 项及本地严格 gate 通过；`814cc3b` 已部署 `.7` 唯一 Gateway。
- fresh `ma-scroll-r38-resume` 在 3.5 秒内恢复原复杂会话；主视图真实完成
  `Ctrl+Home → Down 回底 → Down 选择 child`，终态 child 也完成 `Ctrl+Home → Down` 独立回底。恢复页显示
  MiniMax-M2.7，随后普通中文 follow-up 立即入屏并触发真实流式 Thinking。

## 2026-08-26 provider 大工具参数临时进度（已部署真机验证）

- 真 TUI 已把“约 4 分钟没事件但 TLS 持续收包”收敛为 Anthropic parser 的完整块缓冲，而不是 Gateway
  进程/锁/idle timeout 故障。当前候选从 `input_json_delta` 只抽取工具名、流内序号、阶段和累计字符数，
  1 秒/8K 合批后送 rich 客户端；原始 `partial_json` 始终留在 parser 内。
- `BufferedChunkStreamWriter` 只在 `rich_transcript=true` 时写 `tool_input_progress`，并在真实 retry/tool 边界
  发送有状态 reset；后台 main/child 则复用有界 transcript event。TUI reducer 的 transient block 在
  ready/reset/turn terminal 后删除，不进入 stable history。
- 该路径不创建/修补 ToolCall，不提前触发审批或 handler，不刷新任务/Compact/验收，也不改变 provider
  timeout。8 个相关测试文件 195 项与本地严格 gate 通过；`8c11eab` 已在 Click 长任务安全终态后部署
  `.7` 唯一 Gateway。fresh r37 真 TUI 中 `create_subagents` 参数计数从 515 chars/5s 增长到
  3.0k chars/29s，55s 后完整工具卡创建 5 个 child，临时行无残留。

## 2026-08-26 Anthropic native 首轮缓存身份（已部署真机验证）

- `69bf2ec` 部署后的 fresh TUI 权威账本显示前三次 MiniMax-M2.7 调用仍为 0 cache-write/read；同配置的
  脱敏请求组装和直连重复前缀探针分别证明断点已加载、供应商能写 10,551 并读 10,541 tokens。
- 根因不在 Gateway、Key 或供应商，而是 native 第一轮空 IR 被 `_native_provider_messages()` 压成 `None`，
  backend 将它误作普通 text。当前保留 `[]=native empty`、`None=text`，使首轮 prompt/tools 也进入唯一
  Anthropic cache 投影；Compact、canonical IR 与任务状态不变。
- 9 个直接相关测试文件 193 项和本地严格 gate 通过；`df95d27` 已推送并部署 `.7`。旧任务先安全终态，
  随后只重启唯一 Gateway；`/status` 为 running，模型 MiniMax-M2.7，Python Gateway 进程精确为 1。
- fresh `ma-cache-firstturn-r34` 首请求 3 次物理调用真实得到 25,999 cache-creation、12,313 cache-read；
  同 thread 两个普通后续回合继续得到 37,234 与 46,972 cache-read。canonical generation 仍为 0，
  69.1k→46.3k 的终态折叠没有伪造 Compact，也没有切断 provider 缓存。

## 2026-08-25 assistant 终态折叠、缓存稳定前缀与 Compact 回执刷新（已部署真机验证）

- Gateway 完成回合仍只向用户保存原 assistant 正文；同一条消息 metadata 新增一次从 canonical archive 构造的
  `conversation_terminal_tool_fold.v2`。V2 一次保存固定 hot-tail/cold-fold/deadline；默认 300 秒内读取热尾，
  过期后读取短折叠，不复制完整 ToolCall/ToolResult，也不重新总结旧轮。旧 V1 恒按 cold-fold 兼容。
- `/context` 单列当前未压缩尾部的 fold 回合/调用数，普通折叠不推进 `compact_generation`；真正 Compact 才
  吸收它。消息 repair 使用完全相同的 metadata，不能因正常落账失败而丢掉续接事实。
- HEAD 基线可复现 overflow→Compact→成功继续时 model-usage event id 异值冲突。`7b14e34` finalizer 用物理调用累计
  数作 cursor，ConversationStore 在唯一锁内把累计快照换成增量，cache-read/cache-write 不双计。289 项相关
  focused 与本地严格 gate 已通过并部署 `.7` 唯一 Gateway。
- 原长真 TUI 的两轮 provider 回执分别有 42,107 与 12,987 cache-read，第二轮无需重读仍能准确回答前轮
  两次 Read；手动 Compact generation 1 把 45,639 降到 15,029 并吸收 98 条消息。普通 fold 不重写旧轮，
  真 Compact 才替换一次前缀，因此缓存命中与压缩节省不是同一个计数。
- 现场同时发现控制成功后 footer 仍留着压缩前 `Context/compact 0`。二层候选让 Gateway 回执携带 typed
  `task_status.compact_generation`，TUI 发布 canonical boundary 并撤下旧 snapshot；下一次真实模型调用再刷新
  数字，不解析文案、不额外调用模型。5 个直接相关测试文件当前 190 项通过。
- `f901645` 部署并恢复原 TUI 后，Gateway activity 明确返回 generation 1、真实模型轮把 Context 刷新到约
  29.4k，footer 仍为 0。根因是无 Working block 的 completed frame 没更新 status。当前候选让 idle/resume
  同帧水合 Compact，并在 controller/reducer 两层拒绝旧代数回退。`ff94d61` 的完整相关 251 项与严格 gate
  通过并部署；原 tmux 已验证 generation 1 水合、generation 2 即时刷新和 17,019 cache-read 的新前缀复用。

## 2026-08-25 DNS 瞬断的 会话运行时 式有界恢复（本地候选）

- `ma-97468f3-longchain-r27` 的 worker-6 已运行近两小时并 Compact 3 次，一次 typed
  `socket.gaierror [Errno -2]` 却直接终止；同一时段其它外网稍后恢复，证明 DNS 不能一律当永久配置错误。
- 会话运行时 把 transport Network 归入可重试 Stream/ConnectionFailed。当前候选只按异常链里的 typed
  `socket.gaierror` 扩展既有 2/5/15 秒物理退避，耗尽后保持 ProviderTransientError；不解析错误正文，
  不放宽 malformed URL、认证/额度或代理配置，也不添加无限 runner 重试。
- 普通 JSON/流式两个失败优先回归均已转绿，各证明 4 次 open、3 次 wait；待完整 gateway focused、严格门、
  推送和 `.7` 唯一 Gateway 部署，再用同一长 TUI 后续多子代理阶段观察自然网络抖动。

## 2026-08-25 有效提交恢复 follow-tail（本地候选）

- 同一长 session 的最终回复已经进入 ConversationStore，但 TUI footer 仍显示 `Jump to bottom`；根因是
  `TuiTranscriptControl` 的条件跟随本身正确，输入提交入口却没有像 终端交互 `REPL.tsx::repinScroll`
  那样把一次用户主动提交定义成 return-to-live。此前上翻/视图切换留下的 `follow=false` 会一直保留，用户
  因而看不见自己刚发出的消息和新回复。
- 当前候选只在非空、未超限、且没有被终态 child 只读门拒绝的提交上调用当前 transcript view 的唯一
  `end()`；main/child 各自 viewport 不合并，被动到达的新消息仍不会抢用户阅读位置。focused 已覆盖回底、
  follow flag 与 canonical enqueue 同时发生，待 `.7` 唯一 Gateway 真 TUI 复验。

## 2026-08-25 普通前台续轮补入子代理完成信封（已部署真 TUI）

- `.7` 原长会话证明 completion observation 与后台 wake 都有十名直属 child 的最终回复和 refs，但普通
  TUI 追加消息时 `_gateway_conversation_context` 只恢复 transcript/artifact/workspace，模型因此又猜
  `child_outputs` 并误报结果缺失。
- `24940a6` 首次部署后真实模型仍发出八次 `find_files`。现场结构化事实说明：普通追加轮会为同一工作目录
  创建新 task id，第一版把这个最新 id 误当原 child root，所以 completion 过滤为空；不是模型无视已注入
  数据。该轮已 Esc 停止。
- `999a621` 复用同一 ConversationStore observation，不新增消息或状态源。它按同 thread、非 detached
  task link 的 exact canonical task path 形成 workspace lineage，再要求 event root 位于 lineage 且
  parent 等于该 root；按 child task id 取最新终态，最多注入 12 项并保留 omitted_count。投影只含公开
  completion 字段；内部 runner/output JSON、孙代理、其它 workspace 与 Audit prepare 都被隔离。
- 定向回归覆盖 follow-up task-id 换代、最新终态去重、范围隔离、敏感字段隐藏和 12 项有界视图。119 项
  focused 与本地严格 gate 通过，提交已推送并部署 `.7` 唯一 Gateway。
- tmux `ma-41d5a4a-terminal-fix-r26` 的原长 session 在 workspace 已换多次 follow-up task id 后，普通中文
  续轮仍收到十份 completion，模型明确按信封内容完成八项目整合且没有目录搜索；随后两条小续轮继续
  复用同一 session 且零工具调用。`final_report_ref` 仍指向会被 Read 安全层拒绝的内部状态面，这是独立
  合同缺口；本轮以有界 completion 正文完成，不将它误报为详情引用已通过。

## 2026-08-24 child guidance 排队/消费两阶段回执（本地候选）

- `/client/agent-guidance` 的 HTTP 202 现在只返回 `delivery=queued,status=pending` 和 exact
  `expected_turn_id`，不再把消息箱接受写成模型已处理。薄 TUI 保留该 message-id 的 pending 行，
  明确失败才撤下并恢复输入。
- child runner 的 provider 请求成功后，既有 ConversationStore 先把 exact receipt 推进为
  `consumed`，同一边界再由 durable child transcript 发布 `active_turn_input_consumed`。TUI 只按
  exact client ids 提升自己已登记的 pending，不比对正文或提示文案。多条 receipt 按 provider 注入 FIFO
  提交/消费，不按随机 guidance UUID 重排。
- focused 回归已覆盖快速 202 不被后到的“正在确认”覆盖、两条 pending 持续可见、provider
  消费后 exact/FIFO 进入 user history，以及 child sink 的消费事件。待 `.7` 唯一 Gateway 真 TUI
  验证普通 assistant 回复和原任务继续。

## 2026-08-24 单 Gateway HTTP 固定工作池（已部署真 TUI）

- `.7` 唯一 Gateway PID `557079` 被 Linux OOM killer 杀死；系统证据为进程匿名常驻约 6.68 GB、整机内存
  7.3 GiB。新 TUI 因 8420 已无监听，只显示“正在连接 Gateway”后按 transport failure 退出；不是 session、
  历史或 MiniMax 配置错误。临时恢复的唯一 Gateway 为 tmux `ma-gateway-15db288`，启动后旧 TUI 自动恢复。
- 现场 `py-spy` 在 8 个存活 TUI 下采到十分钟内约 3,300 个 `process_request_thread`：标准库
  `ThreadingHTTPServer` 为每次 `/client/notices` / child view 轮询新建 OS 线程，handler 又会解析 transcript
  和复制 exact child 投影。重启后当前代码的索引读取已把 RSS 保持在约 150--220 MB，但旧线程模型仍会在
  慢请求/重连叠加时放大瞬时对象和 allocator 高水位。
- 对照 会话运行时 app-server 的固定 Tokio task、容量 128 的 bounded channel 和 thread-state semaphore，当前
  实现保留 stdlib handler/鉴权协议，只把 HTTP 接入换成 16 个可复用 daemon worker、128 个总在途上限；
  满载在 handler 前返回 typed 503 和 `Retry-After: 1`。本地真实 socket 覆盖过载、停机取消与 2,000 请求
  压测：全部 200，只创建 11 个 worker，未超过 16。多用户补充对照确认 通道运行时 的入口分层限流优于
  长期助手，长期助手 的 resolved-session lease/profile DB 更适合会话一致性；本项目保留现有鉴权后 per-user /
  per-conversation 公平，不在 transport 层相信身份头。所有响应另强制关闭 HTTP/1.1 keep-alive，避免固定
  worker 被空闲连接占满。40 项直接 focused 与本地严格 gate 已通过，`53498c1` 已推送并部署 `.7`。
- 新唯一 Gateway tmux `ma-gateway-53498c1`、PID `604186`；原有 TUI 自动恢复。fresh tmux
  `ma-53498c1-http-pool-r24` 约 1 秒进入首页，明确显示 MiniMax-M2.7，普通中文真实模型请求成功。9 个 TUI
  自然轮询时 `py-spy` 只见 `gateway-http_0..4`，没有 `process_request_thread`；12 秒 RSS 从约 132.7 MB
  降至 131.7 MB，8420 始终只有一个 listener。该短观察证明有界结构生效，不冒充长期无泄漏证明。
- 2026-08-28 补齐短连接断连收口：此前 `/client/notices` 客户端先离开会让 stdlib 打印整段
  `BrokenPipeError`。现在 JSON 与 metrics 共用精确 socket 写回边界，只静默结束 EPIPE/ECONNRESET/
  ECONNABORTED；其它 I/O 与编码失败仍抛出。25 项 HTTP/固定池 focused 通过；fresh
  `ma-cleanup-brokenpipe-r11` 在普通模型请求提交 0.35 秒后从 TUI `/exit`，原请求仍 6.175 秒自然 done，
  新 Gateway 启动段新增 BrokenPipe 栈为 0。

## 2026-08-24 child guidance exact-turn 入账（已部署真 TUI）

- `.7` Prompt 3 的运行中 child 收到用户普通中文后，Gateway receipt 缺少 `expected_turn_id`；runtime 已按
  current attempt reserve，provider submission 原子校验再报 `guidance submission reservation mismatch`，
  造成 child 失败。该错误与更早的 WebFetch 失败无关。
- `agent_control_service` 现在从 RuntimeDB exact AgentRun 读取 current AgentAttempt，只接受
  `pending/running`，并把 attempt id 写入 guidance metadata。task-local active pointer 只做附加一致性栅栏；
  没有活跃回合、投影冲突或权威库不可用时，入口分别返回 typed 409/503 且不写消息。
- ConversationStore 的 reserve/submission/consume 严格校验没有放宽；`6d33228` 的 429 项相关 focused 与
  本地严格 gate 全通过并部署 `.7`。fresh Prompt 3 中 researcher-1 receipt 的 expected turn 与 reservation
  attempt 同为 `attempt-1787621674-fdb6e96a`，状态最终 `consumed`；child 随后继续模型与 WebSearch，未失败。

## 2026-08-24 detached TUI 会话降载与退出语义（已部署真机）

- `.7` 现场的 tmux 窗口虽然全部 detached，但其中 10 个 TUI Python 进程仍存活并持续查询唯一 Gateway；
  `/client/notices` 每次又扫描 owner 下全部历史子代理，形成高 CPU、BrokenPipe 和新消息排队。它不是
  workspace/owner 锁，也不是 MiniMax 单次推理本身变慢。
- 对照 终端交互 后，普通 `/exit` 改为关闭本客户端及 poller、保留 durable session 和 Gateway task，
  并打印精确 resume 命令；tmux 自己的 detach 仍明确表示进程继续。活动 roster 改成 SQLite 选 exact id、
  canonical 文件复核；空闲健康轮询 5 秒，有任务 1 秒，断线维持独立指数退避。
- `c12ea57` 已推送并部署到 `.7` 唯一 Gateway：fresh TUI 的 `/exit` 后 PID 消失、原 session 文件保留，
  exact `resume` 没有新增 session；16 个会话并发 `/client/notices` 为 0.011--0.206 秒。随后关闭 9 个已无
  活动任务的部署前 TUI，用户当前直连 TUI 未动，所有对应 durable session 均保留。
- 关闭旧客户端后 Gateway 仍有约 40% CPU；`py-spy` 精确采到后台 readiness 在
  `_related_subagent_runs -> list_runs_report -> deepcopy`。当前候选让 lifecycle 合批也走 root 索引选 ID、
  canonical 精确读取。`e2aba94` 部署后 12 次 stack 采样均为等待态，不再出现该全扫栈；8 秒 `/proc`
  差分约 12% 单核，16 会话并发快照最慢 0.294 秒。
- fresh TUI `ma-e2aba94-session-r14` 显示 MiniMax-M2.7 与正确 cwd；首次 `/exit`、exact resume、再次
  `/exit` 的两个 PID 均消失且 status 0，canonical session 一直存在、总数一直 324，Gateway PID
  `481449` 始终是唯一 8420 listener。

## 2026-08-24 后台 main 富过程双游标（本地候选）

- 原 `/client/notices` 只返回 scalar activity、child/Todo 和持久 final notice，后台 main 的显式 thinking、
  工具 output/display、diff 与 Compact 过程在到达 TUI 前丢失。renderer 本身已经具备 终端交互 风格的
  `Update/Write/Bash`、缩进结果、折叠和红删蓝增，无需重写前端。
- Gateway 现在额外返回 `background_transcript_event.v1` 有界易失事件页，使用独立
  `event_after/event_cursor`；通知继续使用 created_at `after/cursor`，两条游标不能互相覆盖。薄 TUI
  仍按同一 1 秒健康周期/0.5--8 秒失败退避读取，并把事件发布到唯一 session sequencer/reducer。
- focused 回归已覆盖 HTTP page、二次游标去重、思考、过程段和 diff 样式。该候选仍待严格 gate、推送和
  `.7` 单 Gateway 的原样 Prompt 4 真机观察，不能把本地 renderer snapshot 写成远端已通过。

## 2026-08-23 单 Gateway 多 TUI 接入背压候选

- 首轮 会话运行时/my-agent 配对真 TUI 暴露的卡顿不是工作区锁：`.7` 唯一 Gateway 的标准库等待队列只有 5，
  历史和当前 TUI 又每 250ms 调一次 `/client/notices`。现场出现大量 `SYN-SENT`、Gateway CPU 约 75%，
  新窗口的活动快照与后续输入均被连接风暴拖慢；这批任务保留为 diagnostic，不进入正式评分。
- 对照 会话运行时 app-server 的 `watch`/server notification 和慢连接有界发送队列后，现有兼容协议先收紧为：
  accept backlog 128、daemon request threads；首次立即查询，成功后 1 秒刷新，失败按
  0.5/1/2/4/8 秒退避并在成功后重置。后台快照 HTTP 超时收为 2 秒，失败继续保留上一份真实投影。
- 本地 HTTP、鉴权与后台活动 focused 39 项通过。待严格 gate、推送和 `.7` 单 Gateway 重启后，使用至少
  四个真实 TUI 同时观察 backlog、CPU、线程、连接状态和活动刷新；通过后才重跑用户给定正式矩阵。

## 2026-08-23 每个 run 的模型 token 累计投影

- 双 TUI 对照需要区分“当前上下文大小”和“任务累计消耗”。现有 `ModelCallLedger` 在同一
  request/run 聚合中新增 provider input/output/cache read/cache creation；明细超过保留上限时累计值
  不丢，供应商不返回 usage 时按调用前 input 与响应估算并单独标记计数。
- `AgentRunResult`、内部 Gateway response、owner-scoped HTTP result 与 runtime fact 都只转发该结构化
  总账，不解析模型正文，也不拿 TUI `current_context_token_estimate` 反推成本。Compact 触发和常驻
  Context 展示保持原合同。
- focused 回归覆盖 OpenAI-compatible cached token、无 usage 估算、明细裁剪后的累计，以及 HTTP
  白名单投影；严格 gate 与 `.7` 真机 provider usage 仍待本轮完成。

## 2026-08-22 Gateway 与 delegated thread 共用请求身份

- Gateway 新 transcript 行同时写 canonical `conversation_request_id` 与显式旧 `gateway_request_id`；
  `conversation/compact.py` 优先按 canonical 字段排除已落盘但仍由 current prompt 单独携带的当前输入，
  旧字段只作为已持久数据的 schema migration read。
- child/grandchild 不经过 Gateway，但按同一个 canonical 字段记录 exact attempt，因此 main 与 delegated
  agent 共用同一 Compact 当前输入排除合同；通道路由、owner binding 和 Gateway request lease 仍各自独立。
- 正常 assistant 落账与 message repair 写完全相同的 canonical id、产物和 operation metadata，修复路径
  不能退回入口专名或建立第二种 transcript。相关 Gateway Conversation focused 回归已通过。

## 2026-08-22 Compact 触发点与 child 真实次数投影

- 常驻 main Context 现在把累计 `compact N` 与自动 `压缩点 90%` 分开显示；后者由同一
  `compact_trigger_tokens/context_window_tokens` 计算，不再冒充次数或操作进度。prompt/messages/tools
  仍只在 `/context` 展示。
- active-turn native IR 裁剪仍形成闭合数字事件并写 rich sink，但不再写 exact child canonical 属性；
  child 完成后的常驻累计只读其独立 ConversationThread generation。该事件没有生命周期、恢复或
  Compact CAS 权威，也不再与 durable 次数相加。
- 真实 Prompt 3 终屏又暴露 no-save presentation 把压缩点错显示为 100%。底层现只用
  `runtime_compact_policy.trigger_tokens` 投影公开压缩点；`allow_persistent_apply` 仍只决定当轮能否
  落盘与执行，不再篡改展示策略。
- `774c7fe` 的唯一 Gateway 真机轮已验证首轮与最终 no-save presentation 均显示 90%。
  该轮 8 个 child 都自然 DONE，最高 98.6k 未到触发线，因此未观察到真实 Compact 次数增加。

## 2026-08-22 执行前状态面拒绝的确定性结果

- Prompt 3 真机中 `WRONG_STATUS_SURFACE` 本来是在 shell 进程启动前完成的安全拒绝，旧结果却缺少
  `effect_outcome`，经统一 operation coordinator 被误判成副作用未知，child 因而 interrupted/PENDING。
- 当前候选保留拒绝并补 `not_started` 结构化事实，统一门将其归为 failed 并把原因交回模型继续修正；
  不修改 timeout、已启动命令或真正 unknown 的 fail-closed 语义。两层 focused 回归已通过，待部署后以
  MiniMax-M2.7 原样 TUI 复验。

## 2026-08-22 后台 Task Runtime State 统一进度账本身份

- 正式 Prompt 3 的工具写侧按 task path 指纹保存 8 项计划，后台续轮却按 durable request id 读取，造成
  main 看不到仍开放的 轻量运行时/通道运行时/横向汇总项并提前自然结束。六份 child completion wake 均已确认消费，
  排除了完成事件丢失。
- `agent_core/runtime/task_identity.py` 现在提供唯一 task-path ledger helper；后台上下文、dispatch seed
  终态同步、Goal continuation 和 activity projector 不再各自复制 hash 或回落到另一账本。typed Task
  Runtime State 因而在每轮模型调用前携带当前计划；该状态仍是软工作记忆，不参与普通任务机器验收或
  自动启动。负向 focused 回归同时放置真/假账并锁定只读真账，部署后的 Prompt 3 仍待真机复验。

## 2026-08-21 TUI 后台 Working 的 canonical 主任务与直属子代理投影

- `d928d77` 真机中前台 turn 让出后，child 仍在自动运行，但 TUI 没有持续活动提示。旧 notice 接口只返回
  偶发文本，不能回答“当前会话是否还有任务”。
- `/client/notices` 现在在同一个已鉴权 owner/thread 快照中返回 active task link 数量和
  canonical run 账本里的直属 child 行。当前 v3 每行只包含名称、status、职责短标题、耗时、当前上下文
  token、Compact 与 attempts；回复、运行碎片、工具输出、路径和权限不进入客户端 metadata。通知文件仍只
  负责新增用户可见消息，不承担活动权威。
- TUI 成功读取后在 composer 附近原位更新一个可移除 Working 区；相同快照不重复追加，活跃 root 归零原位收起，
  HTTP/解析失败保留上一次有效投影。该入口只读，不启动、停止、重试或验收任何任务。定向回归已通过，`.7`
  原样长任务画面仍待当前切片部署后复验。

## 2026-08-22 薄 TUI 首次提交 cwd 与 child 展示快照

- 单 Gateway 的 thread cwd 主链第一次真机提示词 2 暴露了更窄的提交缺口：薄 TUI 为避免在客户端构造
  第二个完整 Agent，会把 audit Agent 置空；旧 `submit_chat_request` 又只从该 Agent 读取 workspace，导致
  首次 ask 没有 `workspace`，child 最终写到 Gateway daemon 的 `/root/bbb`。
- audit owner 与执行 cwd 现为两个显式输入。薄 TUI 即使没有本地 audit Agent，也始终从当前 client config
  提交绝对 cwd/roots；Gateway 仍负责校验并持久化 thread v6。focused 回归直接读取真实 inbox JSON，证明
  第一个请求已带正确 workspace，不靠后续 turn 或 prompt 补救。
- activity endpoint 同批升到 `conversation_agent_activity.v5`。child 的 `context_tokens` 是 exact run 每次
  provider preflight 的当前总上下文，`description` 是创建时职责短标题；TUI 只在一行内按剩余宽度截断，
  不再把 runner 的“模型响应中/模型已生成回复”显示成子代理职责。main 同时透传同一 provider preflight
  的数字 context usage，不再停在启动时的 8.7k 快照。
- 正式提示词 2 的首轮确认职责行生效，同时暴露 Todo 仍重复显示自动 seed 的完整 child goal、后台更新
  未进入 TUI、最终 canonical 5/5 已完成但画面仍停在初始状态。当前 renderer 用 exact
  `item.id == child.run_id` 去重；activity v5 从当前 active task 的 canonical `task_progress.v1` 持续刷新
  普通 Todo，最终 assistant notice 再携带终态快照。只改展示，不删除账本、不解析标题，也不影响
  `progress_item_ids` 的完成标记。
- `conversation_agent_activity.v5` 同时加入主 ConversationThread 的 canonical `compact_generation`。
  常驻 Context 不再把触发线百分比写成 `compact 100%`，而是分开显示 `compact N · 压缩点 90%`；文本协议
  中容易被折叠为 0 的 prompt/messages/tools 不常驻。默认 Todo 固定四条状态窗口，运行项复用 Working
  动画，`Ctrl+T` 可展开全部。

## 2026-08-21 TUI 后台任务停止定位

- 真机子代理权限失败后，前台 turn 已让出，TUI 本地 `is_running=false`，但当前 thread 仍有
  4 个 typed live child。旧 keybinding 在 Gateway 收到命令前就拒绝 `/stop`，显示
  `No exact active turn yet`，因而用户无法停止真实后台任务。
- 当前候选保留前台精确性：正在提交/执行的 `/stop` 与所有 `/btw` 必须带 exact turn id。
  前台已让出时，`/stop` 以空 turn id 进入同一持久 outbox，Gateway 既有 owner/thread 逻辑只选
  唯一 live task；多目标或只有历史任务继续拒绝。

## 2026-08-21 同 owner 多会话后台车道

- `.7` 单 Gateway 原样植物大战僵尸任务中，3 个 child 已全部 `DONE` 且写出 8 个产物，
  但最后子代理 wake 长时间 pending。另一个旧 TUI 的 `ordinary_task_resume` 正在同
  `local/main` owner 内跑长模型回合，旧 supervisor 的 owner 级 single-flight 使当前会话无法唤醒，
  还与新任务争用了一个 child 容量。
- 当前底座已拆成 `prepare_tick -> ready_thread_ids -> tick_thread`；Gateway 以
  `(owner, thread)` 去重、按 owner 轮询提交，同会话仍使用既有 durable claim 单飞。
- 新增真实 scheduler 回归：同 `local/main` 下第一条会话的模型调用被故意挂起时，
  第二条会话仍会在 1 秒内进入模型。本条目前为本地 focused 验证，真机结论在部署后补充。

## 2026-08-21 统一回合结束原因与 Compact 流式进度

- Gateway 最终 response 现在透传主代理与子代理共用的 `turn_end_reason`，值只来自六类结构化回合终态；
  不再从模型正文、验收清单或产物数量反推“完成”。
- 持久会话 Compact 在 rich TUI 路径发布 typed `conversation_compaction_progress`。开始、摘要、计量、
  checkpoint、提交、完成或失败阶段均携带 0-100 百分比；普通客户端保持原行为。
- 进度出口只允许 schema、阶段、百分比、generation 和非负计数，摘要正文、prompt 与原始消息不会穿过
  Gateway chunk。TUI 因此能显示真实压缩进度，又不形成第二份 Compact 事实源。
- Gateway conversation/streaming 与 TUI focused 回归已通过；真实测试机仍以单 Gateway、多独立 TUI
  方式验收，不为每个会话启动额外 Gateway。
- 后台续跑的主 run 身份现优先绑定 exact `task_id`；同一 thread 的旧 `bg-main-*` run 若属于其它任务，
  运行时会回退当前 task 主链再创建 attempt，避免后台整轮工具因权威链错挂而全部拒绝。
- TUI 后台 notice 不再先空等 20 秒：启动后立即查询，随后每秒读取一次；同 thread 的每条 notice 使用
  单调独立 block id，避免 typed reducer 把后续进展当成首条稳定块重放而丢弃。

## 2026-08-18 TUI 活动回合普通输入与上下文可观察性候选

- 四路真机观察证明旧 TUI 在 Gateway 回合运行时仍把普通 Enter 建成下一条 `ChatJob`；两条补充消息虽然
  没丢，却一直等到长任务结束后才分别启动新回合。问题位于本地 TUI input routing，不是 provider 或
  Gateway guidance 丢消息。
- 当前运行中普通输入走 canonical `/ask`，每条正文先写 session outbox，再携带 opaque client
  `message_id` 和 exact `expected_turn_id`。HTTP 回包丢失后只按稳定 ingress ID 查询 `/input-status`；
  `runtime.guidance` 真正确认 provider 消费后才把精确消息转成稳定 user block。外部 ID、重复 ID 和顺序
  数量都不能替代精确匹配。
- Gateway 明确拒绝当前轮竞态时，TUI 只撤下对应 receipt，再由已有 ChatJob queue 接管同一正文；不会由
  HTTP client 自行建立第二请求。active→queued 的 prepared request 冻结 inject/files/save/resume、客户端
  能力和一次 chat-style 注入；提交前本地 `chat-*` 不会冒充 exact turn。pending steer 与真正 follow-up
  queue 已移出可滚动 transcript，固定在 composer 上方并按 会话运行时 每条三行预览，滚动离底后仍可见。
- 已有 receipt 的重放只按首次绑定 turn 在 `Gateway T -> mailbox M -> receipt` 锁序下裁决；全局 active
  投影冲突、文件暂缺或损坏保持 unknown。guidance receipt v4 从 embedded entry 重算 digest，并把 v3
  显式迁移写回 v4，不能用保存的摘要字符串掩盖正文篡改。
- 同轮上下文另增加两类 rich-only、无正文事件：每次模型调用前的数字 usage 快照，以及 native IR 真正
  裁剪 ToolCall/ToolResult 对时的前后 token/对数。后者不推进 conversation compact generation，解决一小时
  单 active turn 发生内部裁剪却没有可见证据的问题。
- 本地薄客户端、TUI input/worker、runtime guidance 与 Gateway control focused 组合均到 100%，py_compile
  与 changed-file Ruff 通过；`.13` 五路仍在做真实活动回合插入、结束竞态、滚动、compact 和隔离复验。

## 2026-08-18 `/context`、手动 compact 与真实 effort 能力

- `/context` 只读精确 owner/thread，并用自动 compact 同一 policy、token estimator 和 uncompacted tail
  显示模型窗口、90% 触发线、generation、summary 与 pending 消息；不创建 thread、不写状态。
- `/compact [可选摘要要求]` 在无 live turn 时申请正常请求共用的 conversation run lane，随后复用既有
  summary candidate、完整 checkpoint 和 generation CAS 强制压缩。可选要求是软上下文，不能覆盖权威
  operation evidence；并发、摘要失败或 checkpoint/CAS 失败均不推进 cursor。
- `/effort` 不把界面档位冒充模型参数。只有 backend 明示结构化 levels/setter 才可设置；当前测试模型
  MiniMax-M2.7 的 Anthropic-compatible 接口没有生效的 effort 档位，所以查询显示 provider-managed，
  设置明确失败且不改 temperature/prompt。CLI/local 非 canonical 会话不伪造 `/context` 或 `/compact`。
- 真机用普通中文问候建立 2 条 transcript 后，首次手动 compact 因上一 foreground lane 尚未释放而安全
  拒绝；6 秒后重试写入 generation 1，随后 `/context` 显示 pending=0、summary=有。查询、自动和手动入口
  均命中同一 thread，未建立第二份历史。
- 同一问候还发现本地 TUI 无外部通道却暴露 `send_message`。修复复用 ToolAvailability：注册实现保留，
  每轮 runtime snapshot 在缺 proactive owner route 时从 model schema/search/execution 一起移除；有真实绑定
  时仍可见。修复前同一请求失败调用 2 次，修复后工具调用 0 次并直接最终回复。

## 2026-08-18 aiohttp 收口反例、effect-bearing 终态与 Fiber 复验

- aiohttp→Go 真机请求精确累计 60 次 logical/model/provider HTTP attempt，零 provider retry；最终源码复制
  后自主 build、通过 29 项测试并完成 HTTP 200 E2E，EXEC-44 的 verification stale 修复由此闭环。
- 该请求最后一个可选清理命令在 handler 前被安全策略拒绝。后续曾用 effect-bearing mutation 再做一层
  `OPERATION_INCOMPLETE` 收口，但 2026-08-28 真 TUI 证明预期非零退出也会被误打回五轮。当前只保留完整
  operation ledger；模型看过工具结果并给出 plain final 后自然结束，UNKNOWN 副作用仍由执行期安全门收口。
- 模型调用累计不再受 128 条明细上限影响；aiohttp response 与 chunks 对账为 60/60/60、retry=0，证明
  request aggregate 进入真实 Gateway result。Fiber→TypeScript 后续以 188/188/188、retry=0、186 工具轮
  结束；测试者只从 TUI/队列观察并核验产物，没有修改任务文件。
- Fiber 首次 `npm install` 还暴露 owner home 只读时 npm 忽略 XDG cache；底座已在 owner-scoped 凭据擦洗
  后增加 `NPM_CONFIG_CACHE=/tmp/.cache/npm`，本地及 `.13` focused 通过并完成部署。该 Fiber 请求早于部署
  启动，仍需下一项新 Node 任务证明首次安装 E2E。

## 2026-08-18 验证新鲜度与模型调用累计统计

- Tornado→Go 真机请求在核心 10 项测试成功后再次写 examples，随后只有 grep/read 便尝试收口；最终正文
  仍是“让我完成最终验证”，Gateway 却投影 `done`。本轮结果据此判为整体未通过，不从自然语言正文反向
  修改 task 状态。
- 被动验证 envelope 现统一嵌入工具结果 canonical `handler_details`。manifest 结构化识别 Go/Cargo 的
  build/test 命令；成功验证后的 workspace mutation 把根标 stale，之后的 read/search 不会清除。completion
  以 root + 最近 verification event ID/status 做一次性 signature，新真实验证才重新武装软核对。
- 模型调用明细仍最多 128 条，但同一 ledger 另维护有界 request/run aggregate。最终 logical/model/provider
  统计不再因明细裁剪停在 128；该修复只观测结构化生命周期，不读取 prompt、response、endpoint 或 key。
- 本地与 `.13` 五个 focused 文件、py_compile、Ruff 已通过；精确包部署后 Gateway/TUI 以 PID
  922966/922999 恢复，另一条 PID 830976 未触碰。真实 E2E 收口行为留给下一项代码任务复验。

## 2026-08-18 长任务连接拒绝双层退避与 TUI 可见重连

- Click Python→Go 真机任务在 127 个工具轮、约 26 分钟后收到远端
  `ConnectionRefusedError(errno=ECONNREFUSED)`。旧 transport 把它与 DNS/api_base 配错合并为不可恢复
  `ProviderConnectionError`，因此既没有执行既有 `2/5/15` 秒 HTTP 退避，也没有进入
  `10/25/45/100/180` 秒模型回合恢复，最终被 Gateway 误投影为 `programmer_bug` 并直接终止。
- 当前分类只读取异常类型、`errno` 和受控异常链：typed `ECONNREFUSED` 与 typed `socket.gaierror` 归
  `ProviderTransientError`，先用三次传输退避，耗尽后再用五次模型回合退避；纯同名字符串、畸形 URL、
  认证和代理配置仍快速失败。用户中断继续优先退出，不进入重连。DNS 支持为 2026-08-25 长任务真机
  失败后追加，不回写成 2026-08-18 当时已经具备。
- provider attempt observer 继续先写唯一 model-call ledger，同时把 `retry_scheduled/attempt/total/wait`
  投影给显式 rich Gateway writer。TUI 立即显示“连接 1/3”或“模型回合 1/5”，公开事件不含 endpoint、
  原始异常或 key；普通客户端继续沿旧 runtime-progress 策略，不扩大 transcript。
- 本地连接分类、物理 HTTP attempt、模型回合恢复、runtime error、Gateway rich stream、model generation
  和 TUI 定向回归已通过；修复包只覆盖五个 provider/Gateway 文件并部署到 `.13`，保留精确回滚目录。

## 2026-08-18 TUI typed stream、可中断传输与工具审批续跑

- 最终候选已精确部署到 `192.0.2.13:${MY_AGENT_CHECKOUT}`：70 个文件与本机 SHA-256 一致、5 个废弃
  文件确认不存在，覆盖前文件已打回滚包。canonical `/root` Gateway 为 running/8420/队列 0/0，
  `my-agent-tui:work` 在 80→120 resize、帮助页和 idle 首帧后仍存活；MiniMax-M2.7 key 只从既有环境读取，
  实际 key 字节扫描部署文件和 evidence 均为 0 命中。
- TUI Gateway 启动不再在 alternate screen 前同步阻塞：`tui_preflight.py` 在画面内发布 typed
  `connection_started/resolved`，轮询 canonical Gateway readiness 成功后才启动唯一 chat worker；超时
  发布 typed error、返回 rc=2。plain 模式保留进入输入循环前的同步 readiness 等待。
- chat TUI 请求只有在 `client_capabilities.tool_approval=true` 时才启用交互审批；普通 CLI、IM 或未声明
  客户端遇到 `ask` 一律立即 `unavailable` 并关闭式失败，不等待一个不存在的界面。
- Gateway 的唯一 chunk writer 先写 `permission_requested`，再等待按 request/permission 哈希定位的原子
  decision 文件；读取端逐字段核对 `request_id`、`permission_id` 和完整 canonical binding，消费后只删除
  当前决定文件。TUI 回写 `approved/denied/cancelled` 后，同一 `ToolCall` 在原工具轮继续，不重交用户 prompt、
  不增加第二执行器。
- 同一 run 内拒绝/取消会把 `tool_name + args_hash` 指纹写入现有 ToolRuntime ledger；provider 即使换了
  operation/call id 重提完全相同调用，也只产生一条未执行结果而不再次弹框。参数真的改变后仍可重新 ask。
- `/stop` 的 typed interrupt 现在会关闭阻塞中的 HTTP response/header/SSE socket，并把该退出映射为
  `InterruptedError`；provider idle timeout、普通网络错误和用户中断不再混成可重试故障。
- Gateway TUI worker 不再把客户端已加载 history 作为新的模型输入注入；服务端仍是 conversation context
  的唯一注入点。客户端只在显式 session resume 时投影同 request 成对的 foreground `cli_chat`
  user/assistant 行，忽略 background、其它 channel 和不完整 request。
- Gateway 在 canonical conversation `compact_generation` 前进时写
  `conversation_compacted` typed chunk；事件只含 generation，不复制摘要正文。TUI 以稳定 compact block
  显示边界，conversation store 仍是唯一会话/Compact 权威。
- 定向回归覆盖 readiness、交互/非交互审批、精确 decision binding、同调用拒绝去重、参数变化再询问、
  socket 中断、session history 单一注入、compact boundary、typed stream/final 去重。测试机最终部署与
  MiniMax-M2.7 证据只记录 run id 和文件 hash，不记录 API key。

## 2026-08-04 Memory Curator 复用 owner 后台 lane

- Gateway 没有新增 Memory daemon。每个已解析 owner 的既有 background-main supervisor 在 scheduler
  tick 前调用同一 `MemoryCuratorService.run_if_due()`；Curator 异常只记录稳定失败，不终止普通请求或调度。
- turn threshold、interval、pre-compact、session close/reset、task complete、daily finalize 和 admin
  都只向同一 durable `memory/curator/state.json` 提交 reason；同一 owner 的 lease 与 cursor 由 Memory
  服务裁决，Gateway 不维护第二份策展状态。
- owner wake discovery 只根据 pending reason、lease、Daily 日期和输入 mtime 判断“需要唤醒”；真正的
  cursor/due/schema 校验仍回到 Curator，坏状态会保守唤醒并 fail closed，不能让 owner 永久漏处理。

## 2026-08-01 命名 Audit 准备工作区与控制面第一阶段

- CLI、Gateway 与 IM 共用一个 typed parser 和控制入口，正式语法收敛为 `/audit help`、
  `/audit <名称> prepare <内容>`、`/audit <时长> <名称> <任务>`、`/audit <名称> status`、
  `/audit <名称> clear`。Audit 名称按 owner/thread 精确且区分大小写，稳定 `audit_id` 才是执行
  权威；旧的未命名 Audit 解析、测试和 README 说明已删除。
- prepare 复用普通 conversation、Agent loop、Memory 与 Compact，只在本 turn 临时绑定
  `audits/<audit_id>/`。草稿、生效要求、验证引用分别保存；问题、试验和比较不发布，只有当前
  prepare scope 可见的 `publish_audit_update` 能发布精确 Audit。失败状态、缺失文件或越界引用
  保持旧配置。启动已准备项复用同一 `audit_id`，已发布要求不会被启动命令正文覆盖。
- harvester 把领取批次时的要求钉在 inflight 记录上；处理中更新和 redelivery 继续用旧要求，整批
  结论落账后下一次 claim 才读取新要求。没有新增 Audit 专用会话、Agent、Memory、Compact、业务
  来源模板或中文关键词判断器。
- 真实 MiniMax-M2.7 CLI 首轮暴露一个归档旁路：prepare 本身没有发布，但通用 workspace archive
  会把本轮用户文字回填进旧 `goal` 字段，使 `/audit status` 错报为已生效。现已让 transient
  named-work workspace 跳过通用目标回填并加回归；修后第一次 prepare 明确显示“尚未发布”。
- 修后 A/B 同会话验收包含 8 个模型请求、23 次 provider HTTP/物理模型调用、14 个工具轮，合计
  `turn_token_estimate=112804`，所有顶层请求终态成功。A 的问答和方案比较没有改生效配置，显式
  发布产生 revision 1；B 同时保持草稿。普通聊天 4.39 秒、普通文件任务 15.70 秒并落到独立
  `tasks/.../output/ordinary-check.txt`；精确 clear A 后 B 仍存在，错误大小写 `b` 查不到 B。
- macOS 没有 bwrap 时，owner-scoped 命令按设计返回 `SANDBOX_UNAVAILABLE`，没有宿主降级；因此
  首轮脚本只写出而未执行，不能拿该轮模型手算当真测试。随后只在隔离测试 home 使用项目既有、
  30 分钟到期的管理员 `owner.full_access` 授权补做单次 MiniMax 探针：47.96 秒、8 次模型调用、
  7 个工具轮，PTY 真正执行脚本并读回 4 条完整 JSON、605 字节和偏移 `0/138/281/460`。其中一次
  `run_command` 仍因本机 sandbox 拒绝，模型沿同一轮改用 PTY 成功；授权文件和临时 Gateway 均已
  删除/停止。该事实不外推为 Linux 多用户 sandbox 或生产部署证明。

## 2026-07-30 Audit 高频判读主链收紧

- 没有新增 Audit 专用 Agent、Memory、Compact 或角色模板；复用现有 long-running 子代理、
  `watch_stream`、durable spool、统一结构化模型调用和 finding 账本。
- 每路按等待时间、累计条数或累计数据长度任一条件成批；200K 模型的常规数据批封顶 32K tokens，
  辅助 JSON 输出单次封顶 8192 tokens。普通记录完整送审；只有单条自身超过安全输入预算时才生成
  明确标记的头尾模型视图，完整原文仍按 `ack_id/source_ref/hash` 留盘并可 inspect。
- Audit root task id 与 objective 跨前台、子代理和后台唤醒传递；同 URL 的不同 Audit 各有游标和账本。
  任一获得 exact task 和工具权限的 Agent 都可消费；模型自主决定是否委派、委派数量和角色，运行时
  不再固定“一源一名子代理”或主代理禁用 pull。exact clear 关闭指定任务的数据流但保留
  spool/archive/verdict；错误码进入统一 taxonomy，不再降级为 UNKNOWN。
- fake LLM、spool、Gateway 控制、后台续接、错误合同及后端请求级输出限制的聚焦回归已通过。
  1.10 正式 CLI 五源 10 分钟真模型测试已完成：五路各 600、合计 3,000 条全部有结论，游标、原文
  hash、source_ref 与隐藏答案定位均无缺失或重复。20 条隐藏真样本命中 14 条，召回 70%；48 条 hit
  中有 34 条误报，精确率约 29.17%，主要问题集中在 auth 与 dns。机制完整性通过，但当前模型质量和
  最慢约 25 分钟的输入后排空延迟不满足实时生产承诺，不能把二者混写成“全通过”。
- 同一 thread 可以继承一个旧 task workspace，但这不等于当前普通聊天拥有旧 task 的子代理。
  只有当前 request 与 task ID 精确相同，或本轮已由真实工作工具写入
  `conversation_task_turn_active=true`，才会把该任务的 child 状态用于阶段回复；后台 Audit
  运行期间的无工具聊天不会再被错误替换为第二次“子代理仍在运行”回复。
- 首个部署后 1 分钟 smoke 中，Audit child 活跃时的普通算术请求 3.60 秒完成、工具轮 0、
  `logical_model_turn_count=1`，双回答问题未复现；流式 chunks 只是同一正文的连续片段。该 smoke
  同时发现 cursor 0 的长日志积压会让默认 400 条 HTTP 页超过 1MB，连续失败且 spool 为 0。当前
  主链只根据 typed `ARTIFACT_TOO_LARGE` 在相同 cursor 将完整记录页长减半，保存有效页长并可降到
  1；单条仍过大才明确失败，不截记录、不按正文判断。最终制品需在这项代码后重建重部署。
- 该轮历史 10 分钟测试还暴露了两项未伪装为已解决的通用问题：模型创建 child 时把“写报告”写入目标，却
  没给 child `write_file` 权限；后台主代理为跟踪五路 worker 做了过多状态/文件查询。前者应在通用
  delegation contract 创建前校验目标与权限一致，后者应复用 typed progress 做事件汇总，均不应
  通过放宽权限、Audit 专用 Agent 或自然语言关键词补丁处理。

## 2026-07-30 命名多 Audit/Goal 与当前窗口停止发布

- CLI/IM 共用语法新增 `/audit <时长> <名称> <任务>`、`/goal <时长> <名称> <任务>`；
  同一 owner/thread 可登记多个命名项，名称只作显示和精确选择，真正运行身份仍是 task/goal ID。
  旧未命名 `/goal <目标>` 继续保持单目标兼容。
- `/status` 在原窗口状态后只列每个命名项的名称、已运行时长和状态。
  `/audit <名称> clear`、`/goal <名称> clear` 只停止精确匹配项；普通 `/stop` 仍是当前窗口的
  interrupt，不触碰 detached 命名项。主代理可调用 `stop_named_work`；只给名称时必须唯一匹配，
  同名 Audit/Goal 并存时 fail-closed。
- 多 Goal 的持久格式从旧单对象迁移为每 thread 一份集合；旧记录读取兼容，下一次写入转成新格式。
  所有后台续跑、计量和完成更新都以 exact goal/task ID 操作。普通前台 turn 遇到多个 Goal 时不猜
  当前目标；只向模型投影当前 owner/thread 内可管理项的 kind/name/status，不投影目标正文、内部
  task ID 或其他 owner 的事实，使用户能用普通话要求精确停止，同时不能把某个 Goal 偷当成本轮任务。
  `/status` 仍是完整名称概览入口。
- 命名 Audit 在模型/工具副作用前登记；同名活跃项拒绝。后台 Audit 属性只从 exact task link 恢复，
  删除了 owner 任意一条 Audit 让其他普通后台任务继承保证档的旧旁路。聚焦回归已覆盖重名拒绝、
  多项列表、逐项 clear、前台 stop 隔离、排队 clear、跨用户同名隔离、普通聊天不污染和旧单目标兼容；
  本地 8899 Qwen 完成真实写入/回读，MiniMax-M2.7 通过 `stop_named_work` 只停止指定 Goal。
- 两个已登录真实飞书客户端在 wheel SHA-256 `c206e8f2…0432b` 上分别从平台入站
  创建命名 Goal；A 同时保留两个 Goal，普通中文只停止其中一个，B 的 `/status` 只看见自己的
  `客户端乙验证`，看不到 A 的名称。两边逐项 clear 后状态均为空；闲置锁解锁后原命令也都沿原
  message 回放。随后修正名称恰为 `pause` 时的 clear 解析边界，最终 wheel SHA-256
  `129408b2…89b6` 已部署正式 1.10 并通过导入/命令 smoke。Gateway/Feishu active、
  `NRestarts=0`、正式队列为空，出站日志分别落到两个真实 open_id。

## 2026-07-30 Feishu 闲置锁原消息续送候选

- Feishu 私聊默认闲置锁由 1 小时改为 3 小时。锁定入站不再直接丢弃，而是保留原
  `IncomingMessage/message_id`，正确密码卡回调后沿既有 adapter callback 和 Gateway `/ask` 主链
  续送；错误密码、operator 不匹配、重复回调和平台重投不触发第二次执行。
- 待续送按用户 FIFO 隔离，默认每用户 20 条、全局 10,000 条，近期续送身份保留 10,000 条；drain
  过程中到达的新消息继续排队，避免抢在触发解锁的原消息前面。这里复用 通道运行时/长期助手 的有界队列、
  `message_id` 去重和单消费者 drain 形态，没有新建 conversation/task 状态机。
- 当前队列是 adapter 进程内短期状态，尚不承诺锁定与解锁之间重启后自动恢复。跨重启持久续送仍是
  明确缺口。
- 94 项相关聚焦回归以及 Ruff、doc-sync、strict code-size、compileall、diff 检查通过；候选尚未
  提交或部署。

## 2026-07-30 双真实用户长任务与 background task scope 收敛发布

- 正式 Feishu A/B 使用各自既有 owner/conversation/thread，在唯一 Gateway 和同一
  `MiniMax-M2.7` 上并行完成架构调研与 Go 复刻任务；初始请求重叠约 589 秒。A 交付 6 份报告和站点，
  精确创建 2 个 child；B 始终修原 `gobat-20260730`，最终独立统计 74 项 Go 测试及扩展黑盒全部通过。
  双方输出目录、USER/Persona/Memory/Compact 和 task lineage 未串线。
- 真测复现终态 sticky workspace 的 successor 错误复制旧 goal。`task_promotion` 现在只从本轮精确
  `RunParams` 取得新 goal，旧 link 只提供 cwd，本轮输入缺失时不创建 successor。`task_runtime_state`
  显式投影当前 goal/status/created_at/path/progress；`conversation.runtime` 保留完整 thread
  transcript/summary，同时只把当前 task 及持久 child lineage 的 task link 和 observation 带进后台轮。
  所有选择依据都是 typed id/status/path，不检查用户正文。
- 单文本删除继续复用唯一 `apply_patch` 主链：共享 `filesystem_text_mutation_rule` 同时供应主代理、
  child、长内容恢复提示和危险命令错误合同；`ToolModelSpec.input_schema` 明确 `*** Delete File`。目录和批量删除仍进入
  `task_trash`，没有第二个 delete tool、shell fallback 或 IM 专项分支。
- B 的最后纠错 request 连续消费 4 条真实飞书 `/btw`，没有创建第二个 request。`d94213c0` 与
  `b24f815f` 的聚焦回归 182 项通过，干净 wheel 已精确部署 1.10。
- 最终版本真实客户端双向隔离复测中，A/B 各自调用 `read_file` 读取对方 `USER.md`，均在 handler 前
  返回 `PATH_CROSS_OWNER_BLOCKED/runtime_gate/handler_executed=false`。A 的只读深度复查已经执行
  `list_files` 和多次 `read_file` 后，精确 `/stop` 把 request 持久化为
  `interrupted/INTERRUPTED`；15 秒内无迟到回复，随后同一 thread 的普通消息仍正确记得停止对象。
  相关 13 条飞书出站正文无工具 XML、内部完成块或协议泄露，Gateway/Feishu active、
  `NRestarts=0`，8420 loopback，live 队列为空。
- 工具与文件变化的公开解释按结构化范围分开：长任务累计工具记录、当前 request 的 ToolCall/ToolResult、
  当前 turn 的净文件 diff 是三个指标。B 的 3,266 行交付来自多轮真实工具操作；后续一次零工具复测中
  模型正文虚报了操作，程序记录仍正确为零。纠正轮真实创建再删除临时文件时，工具记录非零而净 diff
  为零。该语义与 会话运行时 `TurnDiffTracker` 的 net-zero 测试一致；长期助手/终端交互 也允许无工具正文
  正常结束。当前不增加自然语言完成词识别、第二模型裁判、强制最终工具或 closeout 硬门。
- 最终完整 pytest 首轮捕获到 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 未注册和 4 个仍放行 shell 删除的
  旧测试；前者已进入统一 error/recovery contract，后者作为与当前唯一删除主链冲突的死测试删除，
  没有恢复 rm/rmdir/unlink 旁路。141 项相关复验和第二轮完整 pytest 通过，其余本地门禁全绿。
  worktree clean-package 只因按要求保留的 83 个未跟踪运行文件失败，最终制品必须从干净 Git archive
  构建并单独通过 artifact clean-package。

## 2026-07-29 五套 CLI 对照后的工具执行、展示与后台续跑收敛候选

- 同一只读语料、同一中文任务与同一 MiniMax 模型依次对照 my-agent、会话运行时、模型助手 Code、
  终端交互、长期助手。my-agent 真实运行暴露的 Gateway 侧问题包括：进程 sandbox 看不到已结构化授权
  的额外读取根；网页逐页写入会被“全站尚未完成”误报成写失败；兼容 provider 可能混用
  delta/cumulative；旧 CLI 又会因程序核验尾注而重印已经流出的模型正文。子代理默认输出冲突和后台续跑
  claim 身份另记在 subagent 模块。
- Tool Gateway 当前把 `workspace_roots`、`allowed_read_roots` 和 owner workspace 解析为
  `sandbox_read_roots`，经 shell、后台命令、PTY、LSP 进入同一 sandbox 构造器。Linux bwrap 先只读
  挂载读取根，再把显式写根 carve out 为可写；Persona 只读根和多用户 fail-closed 不变。
- 文件写入的 side effect 成功后，全站未完成问题以 warning 返回；最终
  `static_site_check` 仍按同一结构化问题列表决定通过或失败。流式文本使用前一原始 chunk、当前累计值
  和新 chunk 判断真实前缀关系，支持纯 delta、纯 cumulative 和混合流；两个相同合法 delta 不会被
  错删，也没有自然语言去重。
- `AgentRunResult.model_response` 保存 provider 最终模型正文；当前 CLI 把 model delta 保持为未提交
  草稿，只流出 typed tool progress，最终打印统一出口处理后的 committed response。真实 MiniMax
  `write_file -> read_file` 测试中最终正文只出现一次，持久产物内容和 hash 与回复一致。
- 最终 129 项后台/会话聚焦测试与完整 pytest（100%、退出 0）通过；Ruff、import/offline/code-size、
  doc-sync、compile/diff、distribution boundary、artifact clean-package 和 fresh install/CLI 全绿。
  发布 wheel SHA-256 为
  `cd980d72e1d8e7939116152dae7188b9f398393a547823ccc79818022b71bb99`。1.10 唯一正式
  Gateway/Feishu 已精确部署，两项服务 active、`NRestarts=0`，8420 仅 loopback，模型仍为
  `MiniMax-M2.7`。

## 2026-07-29 删除自动检查点注入并补齐 standalone 终态

- `tool_loop.round_execution` 不再因为一次分段 `read_file` 就向下一轮模型塞入
  `long-read-facts`、`task_progress` 或“先写检查点”指令。连续只读轮次的
  `exploration_fuse` 也已连同配置、状态文件和专用测试删除；它实际只改变模型措辞和节奏，
  不承担安全门或真正熔断职责。长任务仍可由模型按任务需要使用计划、草稿和文件工具，但底座不再
  把每次读取强制改造成一轮阶段汇报。
- 取舍直接对照 会话运行时 `tools/handlers/plan.rs`：计划更新是模型显式调用的独立工具，不是每次读文件后
  由运行时暗中注入。真正的错误恢复、上下文预算、权限和工具调用门仍保留在现有 typed chokepoint，
  本轮没有删除这些客观保护。
- 默认 Prompt 也不再要求所有长任务“先写阶段笔记”。有明确交付物时直接逐步更新目标文件；
  `task_progress` 和草稿只在跨 Compact 确实需要恢复时按需使用，内部记录动作不能反复充当用户进度回复。
- standalone CLI/本地 run 在顶层返回时，`run_task_workspace_writer` 现在只根据
  `AgentRunResult.runtime_status/runtime_reason`，经 `user_space.run_workspace.finish_run_workspace`
  原子写入 `DONE/FAILED/BLOCKED/CANCELLED` 和 timeline；不解析模型正文，也不扫描 output 猜完成。
  conversation task 和 task-local child 继续使用各自原有的唯一生命周期状态，不经过这条投影。
- 本轮已通过 Prompt、工具循环、原生协议截断恢复、Compact、Gateway conversation、workspace、sandbox
  聚焦回归及全量 pytest。本机 echo CLI 与 8899 本地 Qwen 原生工具调用均验证：模型正常执行
  `list_files -> read_file`，正文无自动检查点话术，对应 standalone 状态为 `DONE/ok`。

## 2026-07-29 普通会话恢复为 会话运行时 式 active turn

> 2026-09-02 R155 已收窄本节的 sticky cwd 语义：terminal pointer 只作状态/导航，普通新工作不再继承；
> exact request、Goal、active task 或精确旧项目写路径才可复用旧目录。以下保留当时发布记录。

- 普通用户会话现在只有一个持续 transcript、一个 compact 链和一个 sticky cwd。每条新消息直接开始
  当前 active turn；旧 task/progress 只保留在历史、记忆和文件事实中，不再形成“未完成任务清单”菜单，
  也不要求用户或模型执行 select/start/finish/close 仪式。
- `task_progress` 删除 `select/start` 及相关 schema、错误码、提示词和候选渲染，只保留 `read/update`。
  普通 open item 不再拦截最终回复、追加隐藏 completion nudge 或注册后台 continuation。只有显式
  `/goal` 的 exact active goal + task id 可以沿 open plan 自动续跑。
- `/stop` 只按 exact live request、已登记 interruptible turn 或未过期 background claim 判断是否有
  当前执行；没有 live executor 时返回“无需停止”，不暂停旧 task/goal、不消费提醒，也不改变下一条消息。
  停止后 transcript、compact、Memory、Persona、工作目录和已产出文件仍保留，下一条普通消息直接决定
  聊天、继续原工作或开始别的工作。
- sticky cwd 仍是结构化路径事实。上一执行已 completed/interrupted 时，首个 `promotes_task` 工具会在
  同一目录建立当前 request 的新执行身份，并刷新 `task.yaml`、`run_workspace.json`、`state.json` 等
  当前投影；旧 task link 保持终态。工具携带同 thread 既有目录下的精确写入路径时，统一 Tool Gateway
  可以无歧义绑定该目录；普通正文、模糊相对路径和历史清单都没有绑定权。
- 代码级参考是 会话运行时 `session/session.rs` 的单 active task、`session/mod.rs::interrupt_task` 与
  `tasks/mod.rs` 的 abort-current-turn，以及 `tools/handlers/plan.rs` 中不拥有生命周期的 plan update；
  长期助手 参考是 `cli.py` 的 active-input interrupt/queue 和 `tools/todo_tool.py` 的 session-local todo。
  my-agent 只把这些语义适配到既有 owner/thread/workspace/goal 事实源，没有增加自然语言分类器、完成硬门
  或第二套 IM 会话。
- `b18f7774` 已通过干净 wheel 部署到 1.10。正式 Gateway CLI 验证同一 thread 连续记忆和 idle stop
  不改历史；既有 Feishu scope 的可信 localhost live request 验证 stop 精确中断且下一轮仍可续聊。
  两个真实飞书客户端账号在闲置锁解锁后分别发送 A2/B2 请求并收到唯一正确回复。对应 request、
  owner 私有 transcript 和 sent receipt 一一匹配，operation count 都为 0。最终 Gateway/Feishu
  active、`NRestarts=0`、8420 loopback、请求与投递 live 队列均为空。
- CLI 的 workspace 发现仍以 cwd 为身份：从 systemd service cwd 运行可连接正式 Gateway，从 `/root`
  运行会查看另一个 workspace 并误报 stopped。这是既有 CLI 入口可用性问题，不影响 Feishu 或本轮
  active-turn 语义，暂未顺手扩张修改范围。

## 2026-07-28 sticky workspace 与当前执行身份分离

> 2026-09-02 R155 已替换“terminal task 自动贡献 cwd”行为；以下保留当时的历史发布证据。

- Gateway 现在分别投影上一 task 的 workspace id/status 与当前 live execution id。普通终态 task 只提供
  cwd，纯聊天不会重开它；本轮第一个工作工具在同一 cwd 建立当前 request 的新 task id，并记录
  `continued_from_task_id`。只有精确持久 `/goal` 可以按原 task id 恢复。
- 新执行复用 sticky workspace 时也必须经过唯一的 `activate_run_workspace`：`task.yaml`、
  `run_workspace.json`、`state.json` 与 artifact manifest 一起投影为当前 request/run/task，既有
  `output/`、artifact 列表和历史 timeline 保留。相同执行的重复 materialize 不重复追加 timeline；
  损坏状态保留给 doctor，不静默覆盖。旧 task link 指向同一目录但身份已不是当前投影时，只作为历史
  关系保留，不得反向覆盖当前状态或反复报成数据损坏。
- 若 sticky task 仍 active 且已有真实 executor，本轮可以聊天，但工作工具会在结构化 admission 边界拒绝
  第二执行器；使用 `/btw` 引导或 `/stop` 停止。状态不可读同样不会猜测为可执行。
- 后台 scheduler 在取得 claim 后再次读取精确 task link，解决 eligibility 与 claim 之间的竞态；
  `/stop` 或前台完成先到达时，本次 run 标记 cancelled，并消费对应 wake 或关闭 policy，不启动模型。
- 内部 Gateway result 同时保存 logical turn、物理 model retry、provider HTTP retry 和状态计数；
  当前为观测字段，不改变公开回复或请求结果。

## 2026-07-28 原生工具长链完整计量与语义续接

- 已部署的共享窗口虽然删除了阈值附近 live replacement，但真实飞书 320 组 `rg` 差分长任务再次暴露：
  旧字符估算只统计 assistant 文字和工具结果，不统计 `write_file/edit_file` 等原生 ToolCall 的大参数。
  精确 preflight 到 180,000 token 后，Gateway 因此把同一 active turn 重新启动；工具轮出现
  `102→111`、`153→175` 跳跃，模型重新查找已经知道的路径和 checkpoint。请求最终运行
  6,508 秒、294 个工具轮，再次寻找已知 `rg` 后由真实飞书 `/stop` 中断；原 thread/task/workspace
  保留，Gateway/Feishu 均未重启。
- 对照 会话运行时 `3418498f0142` 的完整 provider-visible history/token lifecycle 和 长期助手
  `0b32ff708808` 的按模型窗口限制工具结果后，主代理、Gateway conversation 与子代理继续共用
  `build_tool_loop_prompt` 一个入口。它现在使用既有 `model_visible_context_tokens` 统计 prompt、工具
  Schema、ToolCall 参数、ToolResult、UserTurn 与运行引导，并只按完整调用/结果对删除最旧原生历史。
- `7642c134` 部署后又沿同一真实 owner/conversation/thread/task 续跑至少 289 个工具轮；旧历史回收后，
  模型再次执行 `find /root /usr /home -name rg`，寻找提示中已经明确要求不要重找的真实路径。这不是
  Gateway 重启或项目复制，而是“完整计量正确、语义续接缺失”的独立反例。
- 达到同一个配置阈值后，目标预算是不可删除的本轮基线加既有 recent-tail，而不是“刚低于 90%”。
  当前候选在删除旧调用对之前复用既有 `compact_semantic_summary` 后端，将同一 IR 历史压成一条
  可替换的 `CompactionSummary`；后续模型轮持续看到用户要求、精确引用、已完成/未解决状态和下一步。
  摘要与原 handoff marker 都纳入同一预算，后续跨阈值时原位替换，不堆叠多代摘要。
- 持久 thread summary/raw tail/checkpoint、operation ledger、用户 transcript、task workspace 与
  raw archive 都不新增第二份状态；摘要明确是非权威续接视图，失败时退回既有机械窗口。
- 新回归覆盖“大工具参数、短结果”、200K/90%、连续两次再次跨阈值、前代摘要参与下一次摘要、
  最新 checkpoint 与 `UserTurn` 保留、摘要最多一条、窗口标记不堆叠、原生调用/结果无孤儿，以及
  第二次构造不会继续无意义裁剪。

## 2026-07-27 恢复既有工具轮窗口，删除阈值附近抖动

- 真实 200K/90% 压力证据重新判定为回归：两轮分别只从 `180026` 降到 `179626`、
  `180989` 降到 `178570`，并在 93/40 个事件后仍贴近阈值；这不是有效 Compact。
- Gateway conversation 不再绕过既有工具历史窗口。每轮统一先收敛 `tool_context`；原生
  tool-use/tool-result 的最终预算与 2026-07-28 修复一样按完整 provider-visible token 计算。
  持久 thread summary/raw tail/checkpoint Compact 保持唯一且不变。
- 删除 request/final response 中没有独立权威含义的 `live_context_compaction` 字段。Gateway 只报告
  thread 持久 Compact 状态以及既有运行事实，不保留错误路线的兼容遥测。

## 2026-07-27 系统命令统一入口与窗口级硬停止发布

- 对照 会话运行时 `slash_dispatch.rs` 的模型外命令分发、`turn_processor.rs` 的精确 active turn interrupt，
  以及 通道运行时 `commands-session-abort.ts` 的 session target、队列清理和子代理停止后，Gateway
  `/ask` 成为 IM 普通消息和系统命令的统一入口。adapter 不再自己维护第二份控制命令 parser；
  `/status`、`/btw`、`/stop`、`/goal`、`/verbose` 在入队和 active-turn steer 前由同一 typed
  dispatcher 执行，未知 `/XXXX` 确定性拒绝。`/audit` 只把去前缀后的正文与白名单
  `system_task` 载荷送入正常任务，调用方伪造的该字段在 HTTP 入口先删除。
- `/stop` 不再先判断有没有 durable task。它先用可信 owner/channel/conversation 找到精确
  processing request，立即触发命名中断和模型传输关闭，再写 cancel marker、CAS 中断已绑定 task
  并异步回收子代理。当前 turn 尚未消费的 request/task guidance 同时确认作废；transcript、
  compact、memory 和 task workspace 保留，后续仍可结构化选择原 task 继续。
- 本地 CLI 修复了一个真实线程边界：`agent._current_run_params` 是 worker thread-local，界面线程不能
  用它定位当前执行。plain/TUI worker 现在把精确 request id 写入锁保护的共享状态，`/stop` 与
  `/btw` 只使用该 id；找不到已注册的执行线程就不谎报停止成功。
- 旧 `conversation/directives.py` 和重复的 `/audit` 文本解析器已删除。模型 worker 仍保留最后一道
  fail-closed 门：任何漏过入口的 slash command 以 `SYSTEM_COMMAND_ROUTING_ERROR` 失败，不能写 user
  transcript 或调用模型。聚焦控制、CLI、Gateway、adapter、身份和会话回归及当前 8,365 项全量
  pytest 均已通过，wheel 的 distribution/clean-package artifact gate 也通过。
- 提交 `7776a03f` 的 wheel SHA-256 为
  `14c6dbebac4367b9aa6a6cc40fc6679111d8a00c98472f9e65be2e63b99f1ed2`，已部署到 1.10
  唯一正式 Gateway/Feishu 运行面。部署前原 Compact 压力请求
  `req_1785139621984_1452647_4` 已自然终态，部署后两项服务 active、`NRestarts=0`。
- 两个已登录真实飞书客户端均沿各自既有 owner/conversation 复验。A
  `ou_6591…a895` 的只读请求 `req_1785149836112_1489561_0`、B
  `ou_1be…f921` 的只读请求 `req_1785150148687_1489561_2` 都在工具执行中收到 `/stop`，
  request 与 response 均为 `interrupted / INTERRUPTED` 且 `cancel_requested=true`；
  停止后没有迟到最终正文，普通续聊仍从原 transcript 准确召回刚才被停止的项目。
- A 的 `/verbose on/off`、`/status`、未知 `/future-mode` 与 B 的 `/status` 都由系统直接回复。
  新控制命令没有进入两边 transcript，也没有产生模型请求；两边 thread 的 compact/verbose/task
  状态保持各自独立。正式工作目录下的 plain CLI 复用了相同返回与 fail-closed 语义，测试会话随后由
  `SessionManager` 删除。不同工作目录不会误连正式 Gateway。

## 2026-07-27 单一 Compact 发布、完整恢复点与并发提交

- 本轮仍保留一条 owner/thread 自动 compact 主链，没有增加 task compact、IM 分支或第二份模型历史。
  对照 会话运行时 `32329b289d05` 的单一 compact lifecycle、近期用户上下文和 before/after 计量，长期助手
  `4be38125af06` 的 protected tail、commit fence 与持久失败保护，以及 终端交互 `7dc15d6` 的有界
  messages-to-keep 和连续失败 circuit，适配到现有 `ConversationStore`、owner 文件事实源和 Python 类型。
- 达到精确配置阈值后，运行时先生成不改状态的摘要候选，按包含 system/persona、summary、近期 raw
  完整回合、已压缩与近期工具事实以及当前用户输入的完整下一轮投影复量。只有严格低于同一阈值的候选
  才会先写 owner-scoped 完整 checkpoint，再以一次 generation CAS 提交 summary/cursor/checkpoint
  pointer；过大候选、checkpoint I/O 失败或 CAS 竞争都不推进 live 状态，raw transcript 始终不删。
- 近期尾部最多保留 4 个完整 user/assistant 回合，预算为触发点的 10% 且最多 20,000 token；若保留尾部
  仍过大，第二个也是最后一个候选会压缩全部旧段。该选择只看 role 和 token，不分析中文或任务语义。
  近期 assistant 的 `operation_verification` 继续作为独立程序事实注入，并纳入候选 token 投影。
- thread schema 候选升到 `conversation_thread.v5`，旧 v4 安全加载为空 guard 字段。连续三次失败后只
  冷却新的摘要调用 300 秒，不阻塞无需 compact 的普通消息；冷却后半开，成功提交清零。compact 成功/
  失败迁移已改为同一跨进程文件锁内读取、校验和写回，双 Store 争抢同一 generation 只有一个成功。
- 聚焦回归已覆盖两代 checkpoint 链、近期尾部、工具事实、旧 schema、过大候选、checkpoint 写失败、
  连续失败/冷却恢复、双 Store 并发 CAS、同一通用 Compact 在主代理与子代理运行范围内的续接、
  工具轮与恢复。进一步删除子代理专用 compact/session/recovery 模块后，Compact、子代理恢复与配置继承
  共 151 项回归通过；干净候选 wheel 有 1,004 个成员，旧模块命中为 0，distribution boundary 与
  artifact clean-package 均通过。
- 1.10 既有 child 的 9 次压力 Compact 与一次候选 apply 复核表明，最新 work-state 读取当前 会话运行时 审计
  task 的结构化目标，旧 通道运行时/LangChain 工具游标不再成为下一步。沿原 Feishu owner/conversation
  的纠错请求 `req_1785124143019_1442613_0`、
  `req_1785124595574_1442613_1`、`req_1785125511165_1442613_2` 没有新建或复制项目；独立复核两个
  会话运行时 JSON 各 70 条，源码路径与精确行号全部有效，通道运行时/LangChain 旧产物未被改动。
- 最终 wheel SHA-256 为
  `bb5026c7ff0125d834db77d8e4a92dd30c73747a32be4282e8f59535495a62d6`，含 1,004 个成员、
  2,911,383 bytes；已删除模块命中为 0，distribution boundary 与 artifact clean-package 均通过。
  1.10 正式 site-packages 与 `/root/my-agent-src/agent_py_agent` 的 998 个发布 payload 与 wheel
  逐项 hash 一致。正式配置保持 `anthropic_compatible + MiniMax-M2.7`、200,000 token、90%。
- 部署后 CLI `--no-save` 真模型检查返回 `CLI-SHARED-COMPACT-FINAL-OK`，工具轮与 Memory 使用均为
  0。两个既有 Feishu owner 并发续聊中，A 请求 `req_1785128065900_1448524_1` 只答
  `松针-741`，B 请求 `req_1785128065899_1448524_0` 只答 `海盐-852`，两边均为 0 工具轮；
  候选阶段双方私有目录对对方口令的文件命中也均为 0。这些是可信 localhost Feishu scope，不冒充
  新的客户端入站。
- 最终完整 pytest 收集 8,360 项、100% 且退出 0；Ruff、import/offline、strict code-size、
  doc-sync、compileall、diff 与两道制品门通过。worktree clean-package 正确拒绝 90 个保留运行项，
  未删除用户证据。Gateway/Feishu 均 active、`NRestarts=0`、队列为空，WebSocket connected。

## 2026-07-26 当前轮执行事实与双真实用户 Memory/Persona 复验

- 参考 会话运行时 `32329b289d05` 的 typed response/tool items 和 长期助手 `4be38125af06` 的工具句柄/
  会话循环后，现有工具循环在每次模型采样的 prompt 尾部投影
  `current_turn_execution.v1`。它只读取当前 request 的 canonical tool records，包含 call ID、
  effect、`ok/status/handler_executed`、失败阶段和有界 refs；不读模型正文、不按中文动作词分类，也
  没有增加 Feishu 或 MiniMax 分支。
- 旧 registry 中远离当前任务的重复“执行真实性”说明已删除；Tool Registry/operation store 仍是唯一
  执行权威，prompt 投影只是同一事实的模型可见视图。工具目录、Schema、执行、审计和 compact 没有
  第二条通道。
- 正式 1.10 的两个真实客户端 owner 沿原 conversation 测试。A 的 memory/persona 写入、跨轮召回、
  精确 list/remove 全部真实执行；B 的写入和召回真实执行，但两个清理请求分别只有一次
  `remember list` 或零工具调用，MiniMax 却在正文声称 remove/update_persona 成功。底层没有采信正文，
  权威文件保持未变，直到通过正式 Memory/Persona 工具入口确定性清理。
- 该反证后的底座新增 `operation_verification.v1`：从同一 current-request archive/operation 事实
  生成逐操作状态，副作用成功必须同时满足 `ok=true` 与 operation `succeeded`；幂等重放按 operation
  去重。公开投影删除 call/operation ID、参数、路径和 refs，进入 Gateway/HTTP、正常或 repair
  transcript、后台任务、历史索引和 compact；每条 assistant 都带投影，包括零操作。最终正文不追加
  固定程序核验；出口只按本轮 typed operation records 精确隐藏意外泄露的内部工具标签。它能证明
  “程序说做了什么/没做什么”，但在
  禁止自然语言语义判断时，不能理解并删除自由正文里的每一句错误自述；禁止用中文关键词、正则或
  Feishu 特判补这条边界。
- MiniMax 真实 compact 反例证明，把上述 metadata 仅交给摘要模型仍可能被误读：
  `remember/list` 曾被摘要成“成功删除”。会话 schema 现为 `conversation_thread.v4`，
  `compact_operation_evidence` 与 summary/cursor 原子推进并在后续 prompt 中独立放在摘要之后；
  同一反例下一轮按程序证据回答“没有删除”。旧消息缺核验 metadata 时 coverage 标为 partial，
  既不从正文推断也不伪装完整覆盖。
- A/B 的 active Memory、`USER.md` 和派生 memory index 最终都没有对方测试值；cross-owner 工具硬拒绝
  证据继续有效。最近真实出站未发现 `<memory-context>`、`current_turn_execution.v1`、工具 XML 或内部
  ledger。
- 最终发布复验复用 A/B 原 owner/conversation。A 的
  `req_1785050485322_1318040_0` 是四次真实 Memory 操作；B 首轮
  `req_1785050485332_1318040_1` 被机器核验为零操作，同会话
  `req_1785050618969_1318040_2` 纠正后才形成四次成功操作。A/B 随后的
  `req_1785050812427_1318040_3` / `req_1785050812653_1318040_4` 各只有一次 succeeded
  `send_message`，飞书 receipt 都是 sent。没有新增项目、Persona、Skill 或子代理；这是可信
  localhost Feishu scope 与真实出站，不冒充新的客户端入站。
- 最终 wheel SHA-256 为
  `b3084c12009b259aa1b50f4954a51c9ebcbfb6f0230990d1a4f1f3200657f1f2`，含 1,008 个成员、
  2,904,941 bytes。首次候选被 distribution boundary 拒绝旧 build 缓存中的两个 deleted member；
  清理后 distribution boundary 与 artifact clean-package 均通过。2026-07-26 15:19 CST 已安装该精确制品；
  正式配置保持 `anthropic_compatible + MiniMax-M2.7`，Gateway/Feishu 均 active、
  `NRestarts=0`，仅监听 loopback 8420，WebSocket 已连接且队列为空。
- 提交前完整 pytest 共收集 8,315 项，运行到 100% 且退出码为 0；正式 Ruff、架构守卫、
  import/offline/strict code-size/doc-sync/compile/diff 门禁通过。worktree clean-package 对 83 个、
  701,826 bytes 的保留未跟踪运行证据
  fail-closed，最终 wheel 的 artifact clean-package 通过。

## 2026-07-25 工具失败分层、统一权限上下文与真实通道收口

- 参考 会话运行时 `32329b289d05` 的集中 Registry dispatch、turn permission profile 和
  `handler_executed` outcome，以及 长期助手 `4be38125af06` 的工具耗时/状态表达后，当前唯一 Registry
  生命周期统一产生 `error_code + failure_stage + handler_executed + duration_ms`。七个结构化阶段为
  protocol、authorization、validation、runtime_gate、execution、effect_reconciliation、persistence；
  不从 provider、IM 或工具错误正文猜测层级。
- 协议 envelope、运行门、handler、effect coordinator、审计、tool index、runtime ledger、
  compact/recovery 共用同一事实。幂等重放不会谎称本次再次执行；超时或效果未知不会自动重试。长输出
  归档只保留白名单诊断字段，不能把工具私有字段带回模型。
- native Schema 继续来自同一个 `ToolModelSpec.input_schema`；`apply_patch` 模型说明精确采用 会话运行时 patch grammar，
  没有第二套兼容解析器。1.10 本地 Qwen 能在读取失败后自纠正，但补丁调用用了 7 次；MiniMax-M2.7
  第一次即成功，说明执行协议明确但本地模型效率仍弱。
- 真实 MiniMax CLI 长链为 16 轮、21 条工具记录，覆盖安全成功、缺文件、危险根路径、bwrap、非零退出、
  timeout/effect unknown、长输出外置、写/改/补丁和 owner 写边界。独立核对确认 `/etc/passwd` 未进入
  handler、超时未盲重试、逃逸文件不存在。
- 两个真实飞书客户端分别完成长链：A 请求 `req_1784986368980_1301799_1`，B 请求
  `req_1784986266050_1301799_0`。B 首轮发现 cross-owner 拒绝发生在 handler 内，安全结果正确但诊断
  层级偏晚；统一 runtime gate 随后接入同一 `owner_scope_root`。真实 B 客户端复测
  `req_1784987075421_1304872_0` 在 4 ms 内得到
  `PATH_CROSS_OWNER_BLOCKED/runtime_gate/handler_executed=false`，没有调用工具实现。
- 全量回归还验证了 owner 硬墙与显式 CLI workspace 不是同一概念：其他 owner、admin grant、凭据和
  dangerous root 继续硬拒绝；只有 runtime 的 `workspace_roots` 可以放行普通 owner-scope 外项目，
  不接受模型参数或自然语言扩权。
- A/B 产物、工具索引和 Persona/Memory 仍按 owner 隔离，用户可见回复无工具协议泄露。最终完整 pytest
  到 100% 且退出 0；Ruff、import/offline、strict code-size、doc-sync、compileall 和 diff 同轮通过。
  worktree clean-package 正确拒绝 84 个保留项并报告大体积运行数据。
- 最终 wheel `e7a77182df0e79d9e8dda08d296d06017b3a6e19969539cbae63509faa468a1a`
  通过 distribution boundary 与 artifact clean-package，并已精确安装到 1.10 现有 venv。最终仍只有
  正式 Gateway/Feishu、loopback 8420；两项服务 active、`NRestarts=0`、队列为空且 Feishu WebSocket
  connected。

## 2026-07-25 模型可见工具结果统一投影与通道消息身份收口

- 参考 会话运行时 history/tool result 的集中记录、有界输出和替换旧正文，以及 长期助手
  `tool_result_storage.py`、`tool_dispatch_helpers.py::_maybe_wrap_untrusted`、`redact.py` 后，没有增加
  provider、IM 或 MCP 专用旁路。Executor 在真实执行完成后附加 runtime output policy 最低投影，模型工具循环、
  compact、恢复和父子共享上下文统一消费。
- 外部数据包装与凭据脱敏发生在模型出口，完整 owner-scoped 原文仍供审计/分页读取。纯文本大输出归档
  再由 `search_text/read_file` 读取时也保留 external/default 策略，修复了候选飞书真测发现的来源传播
  缺口；判断只使用 canonical `work/blobs/tool_outputs/` 路径及实际命中记录，不解析用户或文件正文。
- 本地 Qwen 与 MiniMax 的真实 CLI 工具链已通过。真实飞书客户端 A 请求
  `req_1784968159181_1290822_0` 通过 `web_fetch` 读取 会话运行时 文档；B 请求
  `req_1784969134442_1290822_1` 及 `req_1784975003351_1290822_2` 通过
  `web_fetch/search_text/read_file` 读取 长期助手 源码。工具结果保持 external/default，未触发写文件、
  执行命令或额外消息副作用。
- B 客户端曾只显示第一批进度，尽管飞书 API 对后续进度和 final 都返回成功。根因是这些不同逻辑消息
  共用 request 级 provider 幂等键。修复在统一 adapter manager / DeliveryService 缝隙按可信
  message ID、request ID、phase、progress cursor 生成稳定身份；同一批重试复用 key，不同批次和 final
  使用不同 key，不按自然语言区分。修复后真实请求 `req_1784975858195_1299659_0` 在客户端完整显示
  5 条分阶段回复和 `DELIVERY_OK _is_destructive_command`。最终完整 pytest 100% 且退出 0，全部静态/
  合同/文档/编译/制品门通过；精确 wheel
  `5564877d0cebc8ffcb60139391740c705588ce6825cf0af4cf9f6bdfd914928c` 已安装到 1.10 唯一正式
  Gateway/Feishu；两项服务 active、`NRestarts=0`、8420 loopback、队列为空。

## 2026-07-25 incomplete 有界续接、runtime fact 终态与正式 Feishu 长任务复验

- 代码边界继续以 会话运行时 `会话运行时-api/src/sse/responses.rs` 的 `response.incomplete` 失败语义为主：
  无耐久工具结果的普通聊天或任务轮直接返回 `MODEL_INCOMPLETE_RESPONSE`，partial text 和未闭合
  tool arguments 不进入 transcript、执行器或成功响应。只在本轮已有 canonical tool result 时，适配
  长期助手 `agent/conversation_loop.py` 的 length continuation 做一次有界继续；再次 incomplete 就失败，
  不另造 provider 循环或无限重采样。
- empty/incomplete 共用当前 sampling 的单个 repair counter；正常模型响应后归零，因此相隔多个成功
  工具轮的两次独立空响应各有一次修复机会。等待中的 `/btw` typed UserTurn 会在安全点让旧采样失效并
  进入同一个 turn；判据只使用 mailbox 状态，不解析用户文字。
- `_run_once_with_params` 的唯一运行边界在模型、工具循环或 finalization 异常时调用同一个 terminal
  updater。已存在的 runtime fact 由 `running` 原子合并为 `failed/cancelled`，保留工具轮、已执行工具、
  artifact 与 next actions，并附结构化错误；从未创建 start/progress fact 时不凭空制造孤儿记录。
- 聚焦回归共 61 项，另有 2 项 `/btw` 空/incomplete stale-response 回归通过。正式 1.10 当前仍只运行
  `my-agent-gateway.service` 与 `my-agent-feishu.service`，唯一监听为 loopback 8420，MiniMax-M2.7
  长任务期间两项服务保持 active、`NRestarts=0`。
- 用户 B 的真实平台消息沿既有 conversation 做只读续接，请求耗时 `607.907s`、9 个工具轮次；独立复制
  验收确认原 `schedule-ts` 的 68 项测试证据、11 个发布文件、源码 commit 与 tgz SHA 未变，且没有缓存、
  依赖目录或软链接。模型没有因 node_modules 已按原验收要求清理而伪造重新执行测试。
- 用户 A 沿原 conversation 的 Feishu-scoped `/ask` 继续同一个四库深读项目。独立复核连续发现四类 API
  事实错误并沿同一 task/output 纠正，最终只有 `README.md + matrix.json` 两份产物，JSON 可解析，
  APScheduler 4.x、gocron、node-schedule 示例与并发参数归属均对照固定 commit 源码通过，四个仓库 clean。
  该结果保留一个产品边界：高要求报告的模型自报不能替代独立验收。
- A→B 与 B→A 的 `read_file` 越权反证都在实现前拒绝且零写入；A/B 私有 Persona、Memory、Skill/Tools
  没有对方完整 owner id 或私有 symlink，最近 assistant transcript 没有工具 XML、内部代理协议或 shell
  trace。A 的这些纠正和双向反证是可信 localhost Feishu scope，不冒充平台客户端入站；macOS 锁屏使
  两个真实桌面客户端同时跑长任务仍未在本轮证明。
- 最终本地 pytest 到 100% 且退出 0；Ruff、import/offline、strict code-size、doc-sync、compileall 与
  diff gate 全部通过。worktree clean-package 正确拒绝 83 个保留的未跟踪文件，并报告数 GB 运行数据；
  这些内容不得通过 `.gitignore` 被口头视为发布安全，最终结论只取当前源码新建 wheel 的 distribution
  boundary 与 artifact clean-package。最终 wheel SHA-256 为
  `995dee17dfe6327eb40df4de96686796ad73d5e5aad1ba4d8c7716e688347553`，1,004 个成员，
  两项制品门均通过且不含 `docs/`、`data/` 或运行目录。
- 该 wheel 已精确安装到 1.10 的现有 venv；五个改动生产文件的安装树 SHA-256 与 wheel 成员逐项一致，
  incomplete/terminal typed helper 导入通过。最终只有正式 Gateway/Feishu 两个进程和 loopback 8420；
  配置保持 `anthropic_compatible + MiniMax-M2.7`，两项服务 active、`NRestarts=0`、队列为空，
  Feishu WebSocket connected。

## 2026-07-24 工具缺参来源与有限补全发布

- 代码参考固定在 会话运行时 `会话运行时-rs/core/src/session/step_context.rs` 的 typed turn/cwd/environment，
  `会话运行时-rs/core/src/tools/router.rs` 的统一 typed arguments 入口，以及 长期助手
  `model_tools.py::coerce_tool_args` 的 Schema 引导转换。没有移植 长期助手 的标量包数组，也没有从
  用户自然语言、上一工具正文或字段名相似度推断参数。
- `ToolRuntimePolicy.input_policy.safe_parameter_defaults` 只承载工具作者逐字段确认的无歧义默认值；
  `trusted_parameter_bindings` 只可引用 Registry 构造的 `run_scope/write_boundary/registry` 路径并按
  精确字段值限定动作变体。Schema `default` 仍是注解，未同时列入安全默认值就没有执行权。
- 统一 normalize seam 先分离外层 envelope，再补缺失参数、做 Schema 强类型纠正和完整参数门，然后
  才进入 path/effect/审批/实现。显式模型字段永不覆盖；来源账目只写字段路径、`source/source_ref`，
  不写原值，并随短输出 index 或长输出 record/artifact/index 持久保存。来源元数据不是工具输入字段，
  模型伪造会被封闭 Schema 拒绝；归档投影还会删除任意私有键与来源项里的参数值。
- 已迁移 `run_command`、PTY start、`read_artifact`，删除主循环中只服务 artifact 的 scope 补参和
  执行器末端 process cwd 补参。聚焦 Schema、Registry、MCP、native、shell/PTY、artifact 回归已通过。
- 完整 pytest 跑到 100% 后只暴露两个 native 协议测试替身没有新可选属性；Schema 编译器改为与既有
  model-spec-like discovery object 一致的只读 `getattr`，失败项及 native/provider 邻接回归随后全绿。
  本地 8899 Qwen 的省略参数 Shell 调用留下三项正确来源，并因 macOS 无 bwrap 在实现前拒绝、零文件
  副作用；后续只读轮实际调用 `list_files/read_file`。同轮发现短输出只有 scoped call id、没有 artifact
  时仍给出读取提示会诱发无效工具调用；该旧分支已删除，实际 artifact 读取入口保持不变。
  MiniMax-M2.7 只读轮实际调用 `read_file`，
  两边标记、源文件 SHA-256 与唯一文件清单均核对通过。
- 完整本地门禁和干净 wheel 制品门通过后，实现提交
  `df00ec6af8acdf92197a0c28a9889e315943b94c` 已推送远程 `main`；精确 wheel SHA-256 为
  `e095a083a6ab07b87171893d75a9f31a6486534d6466b86173db304f42616d50`。首次 staging 的复制
  `pip` 脚本仍带旧 venv 绝对 shebang，源码/安装树 hash gate 在真实用户测试前发现 wheel 实际装入旧
  venv；部署随即改用目标 venv 的 `python -m pip`，重新核对源码、site-packages 和 commit/wheel marker
  后才放行。最终服务保持 active、`NRestarts=0`。
- 两个既有真实飞书账号从客户端沿各自原 conversation 发送同一普通中文只读请求。A 请求
  `req_1784884678795_420766_0`、B 请求 `req_1784885085848_420766_1` 均只调用
  `run_command pwd`，Linux bwrap 正常执行；工具索引分别只位于各自 owner，且都记录 command 为
  `model_proposed`、timeout/background 为 `safe_default`、working_dir 为
  `trusted_context:write_boundary.task_root`。两边 `input_sources` 无 `value`，没有 Memory、消息或
  其他工具调用，任务目录数保持 64/19；最终正文无绝对路径、工具协议或子代理内容。

## 2026-07-24 工具参数 Schema 单一入口发布与双真实 owner 复验

- 代码参考固定在 会话运行时 `808d3c27` 的 `会话运行时-rs/core/src/tools/router.rs` typed
  `serde_json::from_value` 入口和各 handler 的 `JsonSchema`，以及 长期助手 `91546b83` 的
  `model_tools.py::coerce_tool_args`、registry schema 和 MCP schema 处理。my-agent 没有复制第二套
  provider 专用校验器，而是把既有 `ToolModelSpec.input_schema` 作为 provider 展示、text/native 解析、恢复、
  MCP 注册和最终执行的唯一参数事实源。
- 强类型纠正只处理无歧义的整数/数字、boolean、null 和合法 JSON array/object 字符串；随后在
  effect、审批、路径和 handler 前统一检查 required、类型、enum/const、嵌套对象、
  `additionalProperties`、长度/范围、组合规则和本地 `$ref`。外层 ToolCallEnvelope 与工具参数分离，
  Schema 明确声明的 `kind/run_id/status/metadata/artifact_refs` 不再因协议同名而被误删或绕过。
  已删除旧 required/type 拍平副本和入口特判；MCP 畸形或不支持的 assertion 在注册时跳过单工具，
  不会宽松透传到执行器。
- 本地完整 pytest 两次均到 100% 且退出 0；Ruff、import/offline、strict code-size、doc-sync、
  compile/diff、distribution boundary 与 wheel artifact clean gate 均通过。本地 8899 Qwen 和
  MiniMax-M2.7 均完成真实 `PATH_NOT_FOUND → 替代读取 → 写出 → 回读` 工具恢复，缺失输入未被创建。
- 提交 `5d0822419d3bb36d758433cc10ea500cfec1fa2b` 已推送远程 `main`。从该精确提交构建的
  wheel SHA-256 为 `a0c0c72d9246c18512209af00c734ad94f2994392c806d5fe0e10ecc12a86460`；
  部署后本地源码树、1.10 staged 源码树和 venv 安装树的 18 个改动生产文件 SHA-256 全部一致，
  Linux bwrap sandbox 探针通过。
- 正式 8420 沿两个既有真实飞书 owner 和原 conversation 并发只读复验。请求
  `req_1784876282599_415881_0` 由 A 实际调用 `list_files`，工具路径只落在 A owner；B 第一条回答
  因 `tool_rounds=0` 未计作工具证据，随后请求 `req_1784876388592_415881_3` 实际调用
  `read_file`，收到 `PATH_NOT_FOUND` 后再调用 `list_files` 核实，两个工具路径都只落在 B owner。
  两边 `conversation_persist_degraded=false`，最终正文不含工具 XML、内部进度或绝对 owner 路径，
  失败读取没有创建缺失文件。该轮是可信 localhost 的 Feishu scope 主链测试，不冒充新的客户端入站。
- 最终 1.10 仍只运行 `my-agent-gateway.service` 与 `my-agent-feishu.service`，唯一监听端口为
  loopback 8420，模型保持 `anthropic_compatible + MiniMax-M2.7`；两项服务 active、
  `NRestarts=0`、Gateway 队列为空、Feishu WebSocket connected。1.9 未触碰。

## 2026-07-24 MCP 恢复、能力自述与双真实飞书用户收口

- 代码级复核使用当前干净参考：会话运行时 `808d3c27` 的
  `会话运行时-rs/core/src/session/step_context.rs`、`tools/router.rs` 和 spec plan 把一次 sampling
  的 MCP binding、模型可见 Schema 与最终 dispatch 固定在同一 `StepContext`；长期助手 `91546b83` 的
  `tools/registry.py` 用 `ToolEntry.check_fn`、registry `RLock/generation` 形成稳定快照，
  `tools/tool_search.py::scoped_deferrable_names` 又在 bridge 调用前复核当前 session scope。
- my-agent 没有复制第二套 registry 或 长期助手 的全局 generation cache，而是在既有
  `ToolRuntimeSnapshot` 窄腰上补齐两个漏口：`list_capabilities` 也消费当前 run 的同一快照，
  未授权工具完全隐藏，已授权但当前不可用的能力只显示为 unavailable，私有 readiness 原因不出站；
  `list_tools/tool_search/Schema/execute` 继续只在同一交集内做减法。
- MCP availability 查询保持无副作用；真正重连只发生在下一次 run 固定快照之前。启动失败的合法 MCP
  配置不再被永久丢弃，断线 client 由单连接 lifecycle lock 串行重建，失败按 1–60 秒有界指数退避。
  重新握手后以整张 dict 指针替换方式发布该 client 的精确新目录：旧 proxy 删除、同名 builtin 保留、
  冲突工具跳过。已经开始的 run 不会因新目录而扩大权限；若其旧实现已被替换或连接掉线，调用在实现前
  fail-closed，不把目录刷新伪装成当前 run 的热升级。
- 本地聚焦回归覆盖 stdio 进程死亡后同 binding 重连、启动失败后下一 run 恢复、工具目录
  `before → after` 精确替换、重连退避和 builtin 不丢失。普通 CLI 又由本地 8899 模型真实调用
  `list_tools → list_files → read_file`，读取隔离标记成功；真实 MCP echo/add、浏览器、clangd、网络、
  shell/PTY/process、Scheduler、Memory/Persona/Skill/compact 等工具族沿各自安全测试面复验，没有为
  飞书增加工具分支。
- 1.10 最终 wheel（SHA-256
  `e07e9b9ec75d846be683c6b3a711c88e1044690d00f90be0b631976309f65acd`）只运行唯一正式 Gateway（8420）
  和 Feishu 长连接。真实平台用户
  `ou_1be…f921` 从飞书客户端发起文件任务，并在同一 active request 发送 `/btw`；权威
  `guidance_delivered.json` 只消费一次，同一 thread 最终只有一个
  `output/飞书工具链复验.md`，准确包含原要求和引导两行，最终回复经原平台消息引用投递。此前真实平台
  用户 `ou_6591…a895` 的长任务与中途聊天闭环仍保留，因此已有两个不同真实用户各自的平台入站证据。
- 本地模型轮暴露的是模型效率问题：它多次猜错 owner/date 路径，但 owner gate 均在副作用前拒绝，
  随后模型从 run context 找回精确旧 task 并完成；没有用日期、用户名或中文任务内容加底座特判。
  MiniMax-M2.7 在 00:00 CST 刷新后的最小探针为 HTTP 200/`MINIMAX_OK`，队列为空时同一正式服务安全
  切回供应商。两个既有 owner 并发只读复验中，A 实际调用
  `list_capabilities/list_tools/read_file` 并只读到自己文件；B 的跨 owner `read_file` 返回
  `TOOL_INVALID_ARGUMENTS`。这两轮是可信 localhost 的 Feishu scope 主链测试，不冒充新的平台客户端入站。
- 两项服务最终均 active、`NRestarts=0`、8420 只监听 loopback、Feishu WebSocket connected。仍未覆盖
  MCP 主流 server 长稳、浏览器/LSP 的多版本组合、两个真实客户端同时跑长任务和十万 owner 容量。
- 最终 fast/slow pytest、架构守卫、Ruff、compileall、import/offline/strict code-size/doc-sync、
  distribution boundary 和 wheel clean-package 均通过；strict code-size 为
  `hard=0 / high-risk=170 / soft=61 / blocked=False`。worktree clean-package 只因明确保留的未跟踪
  `data/` 与 handoff 文档失败，并同时报告数 GB 运行数据；这些内容不在 wheel 中。
- 精确部署后请求 `req_1784828035414_319820_0` 在同一真实 owner/conversation 上由 MiniMax-M2.7
  实际成功调用 `list_capabilities/list_tools`，返回 45 个可用工具，视觉与 LSP 均未虚报；请求没有调用
  Memory、文件、消息或其他副作用工具。源码树、venv 安装树与本地七个改动模块逐文件 SHA-256 一致，
  队列最终为 `pending=0 / processing=0`。

## 2026-07-23 单一工具运行快照与双模型双 owner 反证

- 代码级第一参考为 会话运行时 每个 turn 固定 `StepContext/spec plan`，第二参考为 长期助手 的 session toolset、
  `check_fn` 与 search bridge 复核。my-agent 适配为每个 run 一个 `ToolRuntimeSnapshot`：目录、推荐、
  原生 Schema、`list_tools`、`tool_search` 和执行入口只能消费同一份
  `registered ∩ owner policy ∩ allowed_tools ∩ availability`，后续工具就绪不能在本轮扩权。
- `availability` 与授权分离：授权先 fail-closed，避免向未授权主体泄露 readiness；执行前再实时复检。
  视觉/LSP 的空配置、已退出 MCP 和不可用浏览器不会发给模型，检查过程不启动进程、浏览器、LSP、
  MCP 或网络请求。旧 `granted_capabilities` 没有任何工具要求或授权消费者，已从公开 run 参数、
  context、manifest、registry 和测试中整条删除；真实扩权仍只认 owner policy、`allowed_tools` 与现有
  typed grant ledger。
- 首次普通 CLI 真测只设置 `MY_AGENT_HOME`，但正式配置已经显式给出 `my_agent_home`；既有合同和回归都
  是“显式配置优先、留空才回落环境变量”。没有为测试改变正式优先级，只把写反的配置注释纠正；最终
  隔离复验使用独立临时配置文件，profile 只写 `/tmp`，真实工具记录只有
  `list_tools/list_files/read_file`。
- 本轮实际部署 wheel SHA-256 `fd9e6a1c28ff8235ddda59cf6db878b7ca45d4bb5415374f328beb3fc88adcfd`
  已装到 1.10。先用本地 8899，再在 20:00 CST 刷新点后用最小探针确认 MiniMax-M2.7 返回
  `MINIMAX_OK`，随后在队列为空时沿同一配置/服务链切回供应商；没有第二个 Gateway、飞书适配器或端口。
- 两个模型都完成普通 CLI 真实只读工具调用；正式 8420 又复用 Chi/Chalk 两个已有 Feishu-scoped
  合成 owner 与各自原 conversation 并发测试。四个 Feishu 请求都只调用 `list_tools/read_file`，
  `used_memories=0`；各自读取自己的标记成功，跨 owner 读取返回结构化拒绝，B 读取 A 的 result endpoint
  为 403。两边 USER/SOUL/AGENTS、Memory、Skill、tool policy 与 Scheduler 测前测后 SHA-256 相同，
  transcript 不含对方标记，临时文件已删除，用户正文和 channel delivery 均无内部协议。
- 当前正式配置为 `anthropic_compatible + MiniMax-M2.7`，Gateway/Feishu active、`NRestarts=0`、
  队列为空且飞书 WebSocket connected。以上是服务器侧 Feishu scope 主链测试，不冒充两个平台客户端
  真实入站或收件证明。

## 2026-07-21 Provider 原生多轮历史与本地模型连续运行

- [MiniMax Anthropic 兼容接口](https://platform.minimax.io/docs/api-reference/text-anthropic-api)要求多轮
  function call 把上一响应的完整有序 `content` 回放到历史，其中包括 thinking/signature、text 与
  tool_use。旧实现只保留可见 text 和重建后的 tool_use，签名推理块在下一轮丢失。该问题是 provider
  协议历史不完整，不是任务 prompt 或具体模型名称问题。
- 代码级第一参考为 会话运行时 的 typed `ResponseItem::Reasoning`、completed response item 记录和 conversation
  history 回放；长期助手 的 Anthropic adapter 只用于核对 replay block 白名单及 thinking signature 边界。
  当前候选在 `ModelResponse -> AssistantTurn -> message adapter` 唯一链保存有序白名单块；thinking 不进入
  可见 chunk、最终正文或 transcript，tool_use 的 id/name/input 仍由 canonical ToolCall 覆盖。compact 若
  改变原始块则清除对应签名块，不能拼接失效签名。
- 非流式、流式重建、下一轮回放、工具字段权威、无 reasoning 泄露、截断终态和 compact 失效处理的
  聚焦回归共 71 项通过，相关 Ruff 通过。最终完整门禁仍按收口阶段只运行一次，不把聚焦通过提前写成
  发布完成。
- 供应商 quota/unavailable 是结构化运行状态：MiniMax 未刷新时，1.10 唯一正式 Gateway 立即切到已探活的
  本地 8899 继续原 owner/thread/task，不等待、不新建测试服务或第二条会话。只有没有 live request、到达
  配置刷新点且供应商探活成功时才安全切回；4000 仅在自身探活成功时作为后备。

## 2026-07-21 会话运行时 式 thread 工作目录跨轮继承

> 2026-09-02 R155 已将跨轮继承收窄到 exact request、Goal 和 active task；普通 terminal pointer
> 只作状态/导航，旧项目续作由精确写路径回绑。以下保留初版设计与验收记录。

- 真实双长任务在停止后能够通过显式 `task_progress select` 找回项目，但这仍与 会话运行时 单个 task 的体验有
  差距：会话运行时 在 `会话运行时-rs/core/src/session/session.rs` 的 `SessionConfiguration.environments` 中持久保存
  thread 级环境/cwd，`apply` 在新 turn 没有覆盖值时继承旧值；`turn_context.rs` 每轮都从该 session
  configuration 重建 TurnContext。中断 active turn 不会清空 cwd。
- 当前工作树把同一语义适配到现有 conversation/task 事实源：`ConversationThread` 升级为 v3，并以唯一
  `workspace_task_id` 保存精确根任务。Gateway 入站只把它投影成 cwd；纯聊天没有
  `conversation_task_turn_active`，不会重开 task link、写任务运行或在结束时误关闭任务。第一个
  `promotes_task` 工具才激活该精确任务；`select` 只切换其他候选，`start + new_task=true` 才新建并切换。
  旧 v1/v2 数据只在持续目标精确命中或仅有一个合法根任务时无歧义迁移，正文不参与判断。
- store 是 sticky workspace 的唯一耐久写入口，写前核验同一 thread 的 task link 和索引；选择完成后才向
  Gateway processing record 发布 `thread_id/task_id/task_path`。聚焦回归覆盖完成/中断后的纯聊天、首次
  工作工具激活、重启后继承、显式新任务切换、后台主代理和子代理不误关父任务，相关会话/后台套件全绿。
- 最终 wheel `4b882778…bdcb` 已部署到 1.10 唯一正式 Gateway/Feishu 运行面，两个服务 active 且
  `NRestarts=0`。两个 Feishu-scoped 长任务请求均在首次工作工具后发布原 thread/task/path：Chi 进入原
  `chipy` 项目并独立跑出 51/51；Chalk 进入原 `pychalk` 项目，在供应商不可用时直接使用本地 8899 分段
  修复，远端原项目和逐文件哈希一致的独立干净副本最终均为 42/42。两项质量都以请求终态后的独立副本
  验收为准，本节不接受模型自报提前升级。

## 2026-07-21 1.10 正式飞书单运行面

- 1.10 的真机测试拓扑固定为唯一正式 `my-agent-gateway.service`（`127.0.0.1:8420`）和唯一正式
  `my-agent-feishu.service`（飞书长连接）。隔离 Gateway、额外飞书适配器及 `8421`–`8423` 测试端口全部
  退出后续测试链；模型后端切换仍发生在这同一正式运行面内。
- 每次部署和测试都先后核对 systemd 单元、监听端口、进程与 `NRestarts`。2026-07-21 本轮核验只有上述
  两项服务和 `8420` 监听，二者均 active 且 `NRestarts=0`；飞书 WebSocket 已连接。
- 同一正式服务上用 10 个 Feishu-scoped 合成 owner 做三轮上下文反证。第一轮 10/10 各自回复正确词，
  但本地 Qwen 10/10 误调用长期 Memory；第二轮 10/10 召回且 `used_memories=1`。通过正式版本化 remove
  接口 tombstone 全部合成记忆后，第三轮 `used_memories=0` 仍 10/10 找回各自词且无串 owner，9 条逐字
  一致、1 条多出空格。该失败保留为模型质量事实，没有用自然语言特判掩盖。
- 当前候选 wheel `739b330d…6019f4` 已通过 distribution boundary 与 clean-package artifact，并安装到
  正式服务；普通
  `create_skill` 工具已删除，供应商额度耗尽有正式错误合同。模型仍临时指向本地 8899，刷新后将在同一
  8420/Feishu 运行面切回 MiniMax，不创建测试旁路。
- 完整本地 pytest 在 83% 处发现 `task_local` 子代理误进入主代理的用户回复阶段：子代理已经生成的
  `SUBAGENT_RESULT` 会被改写成普通正文，外层因而一直认为子代理没有完成并重复 compact。对照 会话运行时
  为 child thread 显式保存 `SessionSource::SubAgent` 和 `parent_thread_id` 的边界，候选只按已有的结构化
  `context_scope=task_local` 禁止该用户出口阶段；不解析代理名称或任务文字。原无限循环回归现以 5 次
  backend 调用结束，完整本地 pytest 已运行到 100% 并通过。
- 同一轮完整测试还证明旧窗口逻辑只限制 `tool_context` 不够：工具目录、Persona、任务正文和原生工具
  message 合计后曾形成 `40054 > 40000` token。候选复用 conversation 已有的整段模型输入计量，把非会话
  工具轮也按完整 provider 可见输入回收最旧工具对；raw archive 仍保留，未增加第二套 compact。
- 同一正式 8420/Feishu owner 主链又用本地 Qwen 完成一轮精确三子代理真测：模型只提交一次
  `create_subagents(items=3)`，最终三个 canonical child 均为 `DONE/VERIFIED`，没有第 4 个 child；
  `python-context.md`、`http-idempotency.md`、`sqlite-wal.md` 三个产物均存在、非空且 SHA-256 不同。主代理
  在全部 child 终态后才写自然中文汇总，普通 transcript 没有子代理命令或内部协议。整轮约 34 分钟；
  本地模型反复尝试被 `USE_WAIT_FOR_DELAY` 拒绝的 shell `sleep` 并过度搜索，保留为模型效率失败，不用
  prompt 关键词或项目特判掩盖。
- 该轮运行中一条真实 `/btw` 以 task-scoped typed guidance 进入同一 thread，消费一次并写回 transcript。
  它到达时，本地 provider 的旧流随后两次返回 `MODEL_EMPTY_RESPONSE`；旧前台 Gateway 记录因此失败，但
  同一 durable task 被现有 background claim 接管并在第三个 child 结束后正确收口。对照 会话运行时
  `session/mod.rs::steer_input` 把输入加入 active turn 队列的行为，候选现把“已有 pending turn input 时旧
  provider 流结束为空”视为过期输出：在安全点把 typed input 注入原 turn 后重试，成功响应前仍不确认
  guidance。没有按 `/btw` 文本内容判断；runtime-guidance、Gateway control 和空响应回归三组聚焦测试通过。

## 2026-07-20 会话运行时 式 active turn、Compact 溢出恢复与真实双长任务候选

- 普通 Gateway conversation 以前被 task identity resolver 当成非 main scope，导致 `/btw` 已写入同一
  durable task，却可能在真实工具循环里按当前 request id 取错账本。候选把结构化
  `context_scope=conversation` 纳入 main-agent scope；child/control scope 仍保持隔离，没有从中文正文猜
  “这是不是续作”。聚焦回归和 1.10 同一任务实测均证明引导各消费一次，且没有启动第二个 executor。
- 对照 会话运行时 “active turn input 保留在 compact item 之外”的行为，当前 user message 即使已先持久化，也从
  本次 summary 输入中排除；历史压缩为单一 summary，raw transcript 继续保留。provider 返回结构化
  `context_overflow` 时，Gateway 强制推进同一 thread generation 后重试同一 turn；generation 不前进或
  压缩后仍溢出则明确失败，不重开会话。
- 1.10 本地 Qwen 真机压力轮已独立复核：模型窗口 30,000、配置 90%，事件准确记录
  `trigger_tokens=27000`、generation 1，压缩后当前上下文估算降到 22,134；会话暗号在后续提问及服务重启
  后均正确召回。该证据证明单一 thread 的阈值和恢复，不外推为所有供应商的极限质量。
- 同轮在隔离服务上让两个 Feishu-scoped owner 分别复刻 Chalk 与 Chi 的 Python 版本，并在活跃 turn 中各
  注入一条 `/btw`。截至本节记录时两个请求仍在原 task 内持续修错、运行测试，服务 active、零自动重启；
  最终质量和干净交付仍须等请求终态后由外部独立验收，不能用模型过程自述提前升级。
- `write_file` 恢复普通明确语义：省略 `mode` 始终覆盖，只有显式 `mode=append` 才追加。已删除根据“任务
  目录里同名文件存在”偷偷改成 append 的运行时分支，避免返修完整文件时把第二份模块拼在旧内容后。
- 双长任务真机 `/stop` 首次暴露模型传输关闭回调会同步拖住控制回复十余秒。对照 会话运行时 取消 token 后
  只等 `100 ms` 再 abort task handle 的实现，当前 typed interrupt 仍立即立旗，但关闭回调只在前台有界
  等待 `100 ms`，余下幂等清理由 daemon thread 完成。新增慢回调回归证明 control ack 小于 `0.5 s`；
  两个真实 turn 均进入 `interrupted` 后，普通中文“继续”通过 `task_progress select` 精确重开原 durable
  task，而非新建任务或项目。
- 真机 `/status` 还发现任务原文中的宿主绝对路径没有经过已有的外部出口脱敏。修正放在
  adapter-neutral 的 control result 边界，任务摘要和最近进展在确定性文字、typed DTO 两种投影中都只保留
  basename；没有新增飞书分支，内部 transcript 与结构化 task path 继续保留完整路径用于续接。
- 同一次真机检查还发现失败 turn 已没有 processing record、活跃 child 或 live claim，但 durable task link
  为了允许后续“继续”仍保持 active，旧 `/status` 因而误报运行数小时。候选按 会话运行时 的 persistent task 与
  active turn 分层：durable link 继续可选择和续接；只有 processing record、活跃 child 或结构化 execution
  source 才显示 running。无执行器时显示 idle，并清空旧 elapsed/progress；聚焦控制回归通过。
- 两个本地 Qwen 长任务虽然一直收到有效流式 chunk，旧模型 guard 仍在累计墙钟时间超过配置后报
  `ProviderTimeoutError`。对照 会话运行时 `provider.rs` 的 `stream_idle_timeout` 与 `sse/responses.rs` 的逐次
  `stream.next()` 等待，候选把流式 `request_timeout` 收敛为空闲超时：有效 `data:` 事件重置等待，注释、
  半行和静默不重置；工具循环不再把同一数值叠加成总时长上限。非流式 backend 的总时长保护、typed
  `/stop` 和工具安全点保持不变。聚焦回归已覆盖“总运行时间超过配置但持续有事件仍成功”与真正静默超时。
  本地 Qwen 真机进一步以 `5 s` idle timeout 运行 `7.807 s`，收到 `323` 条有效 SSE data，首条
  `0.124 s`、最大事件间隔 `0.066 s`，证明总时长超过阈值但持续有进展时不会被误杀。

## 2026-07-19 前台与后台共用唯一 conversation execution lane

- 1.10 的 B owner 在 `/stop` 后自然续作时，foreground request 与 `scheduled_progress_report` 对同一个
  `thread-2cad… + req_1784434105431…` 并行。后台在 12:24 主动发送“项目完成”，但前台随后仍执行
  13 个以上工具轮并在约 20 分钟后才真正 `done`；后台消息 metadata 明确记录
  `background_delivery_reason=internal_scheduled_completion`，不是工具主动消息或最终 request 回复。
- 具体代码参考不是概念类比：会话运行时 `会话运行时-rs/core/src/session/inject.rs` 在自动 idle turn 前原子预留
  `active_turn` 并在 pending input 竞态下撤销；通道运行时 `src/process/command-queue.ts` 用 lane queue 串行，
  `src/infra/heartbeat-runner.ts` 在 resolved session lane busy 时跳过 heartbeat。候选复用本项目已有
  conversation claim 文件作为持久 lane，不增加飞书分支或自然语言判断。
- `conversation/run_claim.py` 现在承载 claim heartbeat 和 foreground lane 生命周期；
  `gateway_parts/request_execution.py` 只在 lane 外解析 durable thread identity，拿到执行权后才读取
  compact/history/task state，并把 user append、模型 turn、assistant append 全部包在同一租约内。后台
  scheduler 仍走同一 store claim，因此同 thread 拿不到 claim 就不启动；不同 owner/thread 不互锁。
- 三个新增竞态回归覆盖 foreground/background 互斥、等待后新鲜历史以及两 foreground 串行，连续五轮
  无抖动；Gateway/conversation/scheduler 相关 196 项与 Ruff、strict code-size、diff 已通过。完整本地门、
  提交、精确部署和真实 LLM 双 owner 复测仍待下阶段。

## 2026-07-19 Scheduler 到期索引与飞书单次投递收口

- 真实 1.10 压测先暴露旧 Gateway 的 owner-page 轮扫会让短提醒在 135 个 owner 下晚约 159 秒。
  对照 通道运行时 `cron/service/timer.ts` 的最早 `nextRunAt` 定时器，新增全局 SQLite due-owner 投影；它只含
  owner 身份、最早到期时间和短租约，owner `data/scheduler/store.json` 仍是唯一 job/run 权威，实际执行前
  必须回到 owner 账本二次校验。升级扫描遇到不可读账本会在下次重启重试，且不跟随 owner symlink。
- 1.10 上已完成真实重启和常驻时延反证：合成 owner F 在到期后 2.18 秒被重启后的 Gateway claim；
  真实 Feishu owner 的后续两次 one-shot 分别在到期后 0.44 秒和 5.67 秒 claim。三者均只有一个 scheduler
  history run。`wait` 同时固定为 `internal` 路由，只唤醒当前 Agent，不再与用户提醒混淆。
- 第一次真实提醒暴露模型调用 `send_message` 后后台又自动发送最终回复的双发。按 通道运行时
  `embedded-agent-subscribe.handlers.tools.ts` 提交消息投递证据、`cron/isolated-agent/run.ts` 判断 source
  delivery 的代码路径，`send_message` 成功结果现在带 `message_tool_delivery.v1` 内部回执；scheduled run
  只把实际已发内容按 receipt 幂等镜像回原 transcript，并跳过兜底投递。普通任务中途主动消息不改变其
  最终回复语义。
- 候选重新部署后，自动兜底路径只有一次 `NATIVE_CHANNEL_SEND_OK`；明确要求主动飞书发送的真实 LLM
  路径历史为 `scheduled_message_tool_delivery`，同一时间窗也只有一次原生出站，原 transcript 只有一条
  对应最终提醒。Gateway/Feishu 均 active、`NRestarts=0`，1.9 未改动。完整 fast/slow pytest 与全部本地
  静态/制品门已通过；最终提交/push 和精确 commit wheel 重部署仍待本轮最终收口。

## 2026-07-18 基础能力发布、双 owner 长任务与运行时候选修正

- 基础能力提交 `5da7e21e` 已推送 `main`，干净 wheel SHA-256 为
  `ae24bbd1ab149ae3f097e480080f59231aadd55ccf0d1f38138fec0a94e591e0`；1.10 精确安装同一
  wheel/source，Gateway 与 Feishu active、`NRestarts=0`、模型保持 MiniMax-M2.7，1.9 未改动。
- A/B 两个全新 Feishu-scoped 合成 owner 各使用唯一 thread。A 保存称呼“青禾”、回答偏好、暗号和项目
  长期事实后完成 `event-lens`；B 保存自己的独立 Persona/Memory 后完成 `tree-sync`。双方召回只命中自己，
  owner 产物、Persona、USER、Skill 和 Memory 未发现交叉读取。该入口与 Feishu adapter 共用 owner/channel/
  conversation Gateway 主链，但不是平台客户端真实入站或收件证明。
- 两个模型均自主拆分而非由系统固定数量：A 创建 3 个 child，B 创建 4 个 child；A/B 各 3 条 `/btw`
  进入同一持久任务。B 长任务执行时，普通聊天可立即在同一 thread 回答，原任务继续运行。A/B 正式
  compact 阈值均为 90%，本轮 generation 仍为 0，故只证明单一 history 续接，不冒充 compact 触发证明。
- 独立验收不采用模型自报。B 首次在 macOS 暴露 `/var` 与 `/private/var` 路径别名、重复入口和缓存问题；
  沿原 thread/task 修复后 67/67 通过，JSON/CSV/Markdown、坏输入、汇总、稳定原因码、去重和干净交付通过。
  A 首次虽自报 19/19，但 `core.py`/`cli.py` 是两套逻辑；第一次纠错后虽自报 35/35，又由外部时区样例发现
  offset 只被删掉而未换算 UTC。第二次沿原 thread/task 纠正后，外部 41/41、弃用警告当错误、三格式、
  六类原因码、去重、坏输入及 1 小时时区间隔均通过；第三条短纠错只清理原项目缓存，远端最终扫描为
  18 个目录/文件、零 symlink、`.pytest_cache`、`__pycache__`、pyc/pyo 或 egg-info。
- 真任务还暴露并形成通用候选修正：owner quota 只忽略枚举后消失的单文件，其他错误继续 fail-closed；
  capability grant 用 child-link CAS 恢复同 run 且不复活 `/stop`；`raise_event` 按结构化 source child 取 lineage；
  子代理事实改为互斥 `status_counts`；删除 findings ledger 自动拼接用户正文的整条死路径，内部账只交主代理
  整合。具体参考到 会话运行时 `AgentStatus`/`wait`/notification、通道运行时 requester handoff 与 ENOENT 分流、
  长期助手 delegate summary 边界，未在 IM adapter 加特判或自然语言机器判据。
- 上述运行时候选已通过 181 项相关会话/工具回归及新增聚焦测试；完整本地门禁、提交、精确重部署和
  部署后 `/stop`/自然续作、Scheduler、协议出口复验仍待本轮最终收口。
- 首次部署后 capability 真测还发现 scoped owner 从自己的私有 gateway 目录读取 adapter PID/state，因而
  把健康 Feishu 误报为 `CHANNEL_ADAPTER_NOT_RUNNING`。对照 通道运行时 Gateway live snapshot 优先于本地
  config snapshot 的代码边界，现改为从基础 Gateway composition root 显式注入同一只读 health provider；
  owner registry、凭据与当前 thread binding 仍完全独立。聚焦回归实际写入共享 PID/state，再创建 scoped
  owner，已证明 `health=healthy + current_bound=true + state=ready` 且不泄露目标 ID；待 1.10 重部署复验。

## 2026-07-18 通道能力四层事实统一

- 对照 通道运行时 的 channel configuration/outbound selection 主链，把 installed、configured、health 和
  current-bound 收回现有 `ChannelAdapterRegistry`。`list_capabilities` 与 `send_message` 复用
  `SimpleAgent` composition root 的同一 registry/DeliveryService，不再扫描 adapter 模块或临时重建注册表。
- adapter manager 按每个实际通道记录 `starting/healthy/unhealthy/stopped`、检查时间和稳定错误码；daemon
  持续刷新结构化状态。Gateway 只通过 PID、heartbeat 和 JSON 状态投影健康，不解析日志或用户文字；
  进程死亡、状态损坏和心跳过期均 fail-closed。
- 当前投递绑定只从 owner-scoped conversation thread 的结构化 delivery binding 解析；能力清单只显示
  `current_bound` 与目标类型，不暴露 `open_id/chat_id`。QQ WebSocket 重连耗尽会同步清除 running 状态。
- 35 项首批能力/投递/健康回归、119 项 Gateway/后台唤醒/工具注册扩展回归、QQ 生命周期回归和
  完整本地 CI 通过；当前仍是未提交工作树，须完成提交和 1.10 真实 Feishu 探活才算发布。

## 2026-07-17 重启恢复不重放已结束 runner

- 1.10 切换最终文档快照后的只读恢复审计发现：一个多日前旧 task 的主状态残留 `RUNNING`，但 runner
  session 已明确 `completed`；旧 dead-worker reclaim 把所有非 fresh session 都当成宿主猝死，因此重启时
  又拉起同一个 child。
- 恢复入口已收紧到结构化 `runner_session.status in {starting,running}` 且心跳失效；显式终态不由该入口
  重放。真正宿主死亡、父/child link 都 active 的续跑语义保持不变，父生命周期门仍先于 reclaim 生效。
- `80a0527d` 的 35 项聚焦回归、选中 8,003 项且退出码为 0 的本地 fast suite、
  Ruff/import/doc-sync/strict code-size/compile、
  clean wheel 与三组 GitHub Actions 均通过。1.10 使用源码归档 SHA-256
  `5b4cd7f6c88bce79878c4ab3f46f02fad0ac4731f0dd8c9f8133e836863f12ae` 和 wheel SHA-256
  `47d05a83c3dd8664af536c4df4a6251ee15941a2fe326d37df2845fbc3469684` 精确部署；三处 marker 一致，
  source/site-packages 哈希一致，Gateway `/status` 为 running、队列 0、Feishu WebSocket 已连接。
- 反证目标在部署前后及一次周期 supervision 后的
  `session_id/status/history_count/updated_at` 完全相同，且日志没有非零 orphan reconcile；模型仍为
  `anthropic_compatible + MiniMax-M2.7`，1.9 未改动。

## 2026-07-17 `a7d6044e` 等待回执发布与双用户续作收口

- `ccb8d7f8` 的单一 thread history/compact 收口与 `acb1cfc5` 的父 conversation 生命周期门均已进入远端
  `main`，三组 GitHub Actions 通过；1.10 运行精确 `acb1cfc5`，Gateway/Feishu 全程 active、零重启。
  真机重启反证覆盖：父 task 已关闭的旧 child 被取消、父/child 都 active 的精确 child 可恢复、链接缺失或
  损坏的 child 保持 hold；1.9 未改动。
- 部署后另建 A/B 两个稳定 Feishu-scoped 合成身份。A 在唯一 `log-lens` task 中创建 5 个不同 child，B 在
  唯一 `tree-diff` task 中创建 3 个 child；没有重复 task 目录或重复派工。B `/stop` 后三个 child 全部取消，
  transcript、口令和 workspace 保留；自然补充后继续选择原 task，未重新创建 child。长任务让出期间，A/B
  普通聊天分别约 4 秒和 10 秒完成并只召回各自口令。
- A 的 2 条、B 的 3 条 `/btw` 均由 `guidance_delivered.json` 证明只消费一次，并在各自唯一 transcript 中
  各出现一次，零 pending。A/B 只有各自 1 个 task root，owner ID、口令、产物和符号链接零交叉；两个 root
  最终均 `completed`，work state 均 `DONE`，所有 progress policy 均 disabled。
- 独立干净副本验收不相信模型自报。A 首次遗漏 Unix 时间与 Top 5/unknown、README，沿同 task 返修后新
  虚拟环境安装、119/119 测试及 ISO/Unix 秒/毫秒、Top 5/unknown 外部断言通过，正式目录零缓存/临时脚本/
  checkpoint/符号链接。B 首次漏测二进制哈希、ignore 规则、CLI 崩溃和产物残留，沿同 task 两轮返修后
  新虚拟环境安装、64/64 测试及 11 项外部断言通过，`output/` 零缓存/旧副本/符号链接。
- 部署态同时暴露等待回执事实过少：A 模型说“继续等待进一步指示”，B 回执没有说明正在做什么。通用修复
  给同一个无工具表达轮提供有界的当前请求、已执行动作、当前引导和精确子代理统计，并明确
  `task_continues_without_more_user_input`；普通句子仍由模型生成，状态不从自然语言判定。`a7d6044e` 的
  60 项相关回归、8,057 项完整 pytest、Ruff/import/offline/code-size/doc-sync/compile、2.62MB wheel
  clean-package 与 distribution boundary 均通过；GitHub Actions Lint `29590701614`、Test `29590701591`
  和 Cross-platform guard `29590701693` 全绿。
- 1.10 以源码归档 SHA-256 `5a5914c5768a27b624ee7f7aae7f3b77efec65a5dd7dfca3f513f6536a44da66`
  和 wheel SHA-256 `595c510578f884940ba8f58ecb443ba0d7ac126baecbd5a04cd5a966f55b478c` 精确部署
  `a7d6044e`；源码、site-packages 与本地关键模块哈希一致，三处部署标记统一。Gateway/Feishu active、
  `NRestarts=0`、队列空闲，模型保持 `anthropic_compatible + MiniMax-M2.7`。
- 部署后新建 `ou_waitproof_a7d_20260717` / `oc_waitproof_a7d_20260717` 合成 scope。首轮 27.563 秒回执
  准确列出 parser/test 两个 child，明确无需用户补充且会自动整合；两个 child 依次 `DONE`，主代理在同一
  root/thread 读取报告、生成代码、运行测试并修复失败，约 12 分钟后自然最终回复。干净临时副本重新
  `py_compile`、运行测试和 parser 自测，结果为 23/23、零 symlink；root link=`completed`，transcript 恰为
  user/interim/final 三条。统一 DeliveryService 把内部宿主路径脱敏为 `wait-receipt-proof/` 与 `output/`。
- 上述请求使用与 Feishu adapter 相同的 owner/channel/conversation Gateway 主链，但 open_id/chat_id 是
  合成身份；这仍不是 Feishu 平台真实客户端入站或真实引用回复证明。

## 2026-07-17 双用户长任务矩阵与单一历史反证

- 1.10 MiniMax M2.7 上完成 A/B 两个 Feishu-scoped 合成用户的完整长任务矩阵。A 完成 Hyperfine、
  Zoxide、8 项对比、SL、Pastel；B 完成 Tokei、Navi、9 项对比、Tealdeer、Miniserve。B 长任务运行时，
  A 的普通追问 9.417 秒完成并只召回 A 的上下文；owner、thread、workspace、产物和口令未串线。
- 17 个不同 `/btw` 输入（3 request-scoped、14 task-scoped）全部在目标执行轮只确认一次，pending 最终为 0，
  并以 UserTurn 留在同一 transcript 的准确工具历史位置。确认事实来自 `guidance_delivered.json`，不可变
  inbox 只保留输入最初落账状态。控制与去重只认 guidance/request/task/thread id，不解析中文语义。
- Miniserve 的补充与修复继续绑定原 task `req_1784264255535_1355192_2`、原 thread 和原 workspace；没有
  另开 task 或重复派子代理。首次外部黑盒验收 54/58 暴露 4 个真实缺陷，修复后的干净 wheel 为 58/58；
  这项外部验收没有被加入 Gateway 或普通任务完成主链。
- 本轮请求经 Gateway `/ask` 进入 Feishu owner/channel/conversation 作用域，但使用合成身份，不代表
  Feishu 平台客户端真实入站/投递。正式阈值 90% 下本矩阵没有触发 compact；唯一 thread compact 的真机
  证据仍是既有 50% 压力轮。删除第二套 task history/compact 的 `ccb8d7f8` 已随 `acb1cfc5` 部署，并由
  post-deploy A/B 单一 history 续作和 `a7d6044e` 等待后自动续跑再次反证。
- 当前边界复核固定参考 会话运行时 `03bb3b12367397e14a8facc2e018d645ff4d8e83`、通道运行时
  `f2a46b0661206a0b7264ad05749e2304fbfe6a61`、长期助手
  `7d0246ab5715e9e18e156eb08912f4e24bd8d175`；只适配语义，不新增 IM 专属底座。

## 2026-07-17 父任务生命周期控制孤儿恢复

- 1.10 部署前只读检查发现旧 owner 下仍有多日前的 PENDING/BLOCKED/RUNNING 子代理投影。此前周期孤儿
  恢复只看 child status、runner heartbeat 和 PID，无法证明其 conversation root 仍允许执行，重启可能
  复活已经结束或 `/stop` 的旧任务。
- `acb1cfc5` 新增一份 batch conversation lifecycle decision，同一 thread 只读一次 task links。auto-start、
  普通 dispatch、watch takeover、RUNNING reclaim 和 orphan revive 共用它；父 root link 与当前 run link
  都是 active 才能启动同一 run。completed/cancelled/interrupted 等已关闭链接调用现有取消链收敛 canonical
  run，链接缺失、损坏、跨 thread、重复或未知状态一律 hold，不从 goal、聊天文字或展示状态猜测。
- 无 conversation attrs 的本地/admin run 保持原恢复语义；只出现 thread/task 其中一个身份字段则 fail-closed。
  聚焦回归覆盖 active 恢复、completed/interrupted 取消、missing/corrupt hold，以及 auto-start/dispatch 两个
  绕行入口；远端 CI 与上述 1.10 重启反证均已通过。

## 2026-07-18 持久 Scheduler 接入同 thread 主链

- composition root 为每个 owner 创建唯一 `SchedulerRepository`/`SchedulerService`，
  `schedule` action tool 不接收 owner/thread 参数，只从当前可信 RunParams 绑定原 thread。
- owner `data/scheduler/` 持久 job/run、历史、CAS 版本、claim/heartbeat 和 misfire。
  owner disk discovery 识别 due/queued 事实，重启后不依赖旧进程内存。
- 到期 run 使用自己稳定 scheduler run id，但仍进入原 owner/thread 的
  `BackgroundMainAgentRuntime`；加载同一 transcript/compact 和 owner tool policy，用原通道绑定投递。
  同 thread 同时到期的多个 job 不走普通 wake coalescing，每个 run 均单独关闭历史。
- 对照 通道运行时 `src/cron/service/timer.ts` 的持久 timer 与 长期助手 `cron/scheduler.py`
  的 claim heartbeat；my-agent 复用自己已有 ConversationStore 和 owner wake 路由，没有新建 IM 专用队列。
- 相关聚焦测试、完整本地 CI、distribution boundary 和干净 wheel artifact gate 已通过；
  1.10 和真飞书到期验证待本轮最终收口。

## 2026-07-18 owner quota 与自动 retention

- `OwnerQuotaEnforcer` 作为 composition root 的 owner-local 单例接入文件工具、Memory、Persona、Scheduler
  和 Skill draft；非 Agent 的飞书 Persona 确认入口从相同 `quota.json` 重建同一门。锁序统一为
  `owner quota -> repository/file lock -> mutation`，整批最终字节在 owner lock 内计算。
- 子代理创建前把 `max_active_agents` 与 owner/task/per-call 各级容量取严格交集；权威运行状态不可读时
  整批拒绝，不按零占用继续。
- Gateway 新增独立 maintenance controller，每 tick 只扫描一个有界 owner page，不实例化 Agent；owner
  自己的 policy 决定实际 24 小时维护间隔。task/subagent scratch 只依赖结构化终态和 `updated_at`，执行前
  二次校验；先移入 owner trash 并写 tombstone，再按期限删除，支持 legal hold 与 audit。
- 配额、retention、owner 发现、Gateway controller 和 Memory/Persona/Scheduler 组合写入聚焦回归与
  完整本地 CI 通过。Shell/PTY/LSP 任意进程写盘仍须正式部署的 filesystem/project quota 兜底；
  1.10 尚待收口。

## 2026-08-28 owner 磁盘配额默认不限与记忆命令非阻塞

- 真 TUI `/remember` 暴露一次保存会连续触发 Candidate observe/review/promote 三次 owner 全树扫描；`.7`
  当前 owner 约 14GB、26 万文件，单扫约 2 秒，客户端 10 秒后断开但 Gateway 随后完成写入，造成界面报
  `GATEWAY_UNAVAILABLE` 而 `/memory` 实际可查到。
- 新 owner 使用 `quota.v2`，`max_disk_mb=0` 表示不限制且不进入扫描/配额锁。初始化只把完全未改的旧
  `quota.v1` 100GB seed 迁移为 0；显式非零管理员策略继续走原门，坏 policy 继续 fail-closed。
- TUI 的 `/memory`、`/remember` 使用后台 worker 调用原 dispatcher，输入线程不再等待 HTTP；remember
  响应丢失返回 `MEMORY_WRITE_RESULT_UNKNOWN` 并提示用户查询，不自动重放可能已经提交的写入。

## 2026-07-17 单一 thread 历史收口

- 复核 会话运行时 当前实现后，Gateway 收敛为一个 owner/thread 和一份 summary + raw tail。聊天、文件工作、
  子代理协调、定时唤醒和普通小任务都续接同一份模型历史；IM 只传输消息，不建立
  额外 session、lane、任务 transcript 或 compact。
- `Running Work` 只保留为同一 prompt 中的结构化工作索引，用来阻止同一个后台任务被第二个执行器重复
  启动。task link、workspace、progress、wake 和 agent tree 都是运行事实，不得过滤、替换或复制 thread
  transcript；普通用户消息也不再复制进 task guidance 账本。
- 同轮还发现 `work/state.json` 过去只在建目录时写一次：task link 已完成或 `/stop` 后，它仍可能永久显示
  `RUNNING`；旧任务首次续接并懒建 workspace 时还可能把本轮 request id 写成 task id。当前 workspace
  writer 复用唯一 `durable_task_id` 解析器，conversation task link 的结构化生命周期迁移再投影到精确
  task path 下的 state；路径必须位于当前 `owner_home/tasks/`，越界、身份不一致或状态文件损坏时告警并
  跳过，不跨目录修补。

## 2026-07-17 模型首段原话进入低延迟进度通道候选

- Sl 真任务的续接轮在约 14 秒已经生成“先检查项目”的自然模型文字，但旧主链把全部 model delta
  只留在 chunk 文本缓冲，直到 94.342 秒的前台协作让出后用户才收到自然回执。当前候选把真实
  model delta 与 provider/runtime notice 分成 typed sink：第一次工具开始前已经形成的模型正文会经统一
  用户出口净化后写成一条 `assistant_commentary`，不拼“正在处理”等固定句子。
- commentary 是 presentation-only（只负责展示）的有序事件：每个 request 最多一条，`/verbose off`
  也可见；工具名、工具输出和逐步命令仍只在 `/verbose on|full` 下显示。它不写任务终态、不替代最终
  assistant reply，也不会让 delivery worker 误以为已经完成最终投递。
- 触发边界读取 typed `phase=started`，不读取本地化 `status` 展示词；commentary 发出后不再保留后续
  model delta，避免长任务把无用分片持续积在内存。
- commentary 投递失败按 cursor 至多尝试一次，避免坏 IM 路由每秒刷屏或阻塞耐久最终回复；最终回复仍
  沿原 pending/sent receipt 重试。runtime 自动恢复提示、provider notice 和旧 generic chunk callback
  不能伪装成模型原话。
- 代码边界对照 长期助手 `gateway/stream_events.py`、`stream_dispatch.py`、`stream_consumer.py` 的
  `Commentary`/final 分栏，以及 通道运行时 `reply-delivery.ts`、`block-reply-pipeline.ts`、
  `get-reply-run.ts` 在模型/工具边界按序投递 block reply 的做法；只复用 typed presentation event 与
  最终交付分离，不复制其 session 或 adapter 实现。聚焦回归通过，尚未发布到 1.10。

## 2026-07-16 分步任务追加要求进入同一 thread 历史

- 1.10 的分步复刻曾暴露：workspace 虽续接正确，后台轮却因读取 task-scoped transcript 而退回旧目标。
  把用户要求复制到 task guidance 的旧候选方案已经删除；它会制造第二份历史，并让
  同一句用户消息在 transcript 与任务账本之间产生确认竞态。
- 当前实现把每条普通用户消息幂等追加到唯一 thread transcript。前台、后台、retry 和 compact 后的续轮都
  读取同一份 thread summary + raw tail；`task_progress select` 只选择结构化 workspace，不改变会话历史，
  也不复制用户正文。持久化失败仍 fail-closed，归属只认 owner/thread/request/task 等结构化身份。

## 2026-07-16 旧任务续接与用户停止的结构化硬边界

- 1.10 双用户分步长任务实测发现：同一用户第二步已经拿到 Recent Completed Work，但 MiniMax 仍调用
  `task_progress action=start`，底层原先无条件接受，因而新建了第二个工作区，随后在错误目录里连续
  `PATH_NOT_FOUND`。这不是 transcript 缺失，而是 task start 入口缺少结构化确认。
- 现在会话存在 active/interrupted/recent-completed 候选时，`start` 必须显式携带布尔字段
  `new_task=true`；否则返回同一 `CONVERSATION_WORKSPACE_DECISION_REQUIRED` 和精确候选。继续旧任务仍只用
  `select + task_id`。没有候选时普通任务可直接 start。实现不匹配“继续、第二步、新任务”等自然语言。
- 后续双用户复刻真测又捕获到更具体的协议误用：模型虽在正文里说“继续同一个项目”，却一次调用
  `start + new_task=true + summary/next_action/items`。旧入口会静默忽略这些只属于 `update` 的字段，并立刻
  建立错误工作区。当前候选把 `read/update/select/start` 改为严格动作变体：`start` 只接受 `new_task`，
  `select` 只接受精确 `task_id`，进度字段只能在后续独立 `update` 中提交；混用返回
  `TOOL_INVALID_ARGUMENTS`，且不会创建 task link 或目录。该门只检查结构化字段，不判断用户文字。
- 该边界对应 会话运行时 的显式 `turn/start` 与带 expected turn id 的 `turn/steer`，并参考 通道运行时
  `src/talk/agent-run-control.ts` 的 active session + typed mode；my-agent 只适配自己的 owner/thread/task
  文件事实源，没有引入第二套控制协议。
- 同轮 `/stop` 已真实中断错误任务，但后台 claim 把 `InterruptedError` 误记成 failed，日志又因其继承
  `OSError` 而显示 I/O 故障。调度入口现在单独接住该类型，安静以 `cancelled/user_interrupted` 结束租约，
  不作为 recovery takeover 候选；持久任务链接仍保持 `interrupted`，所以用户随后选择原 task 可以继续。

## 2026-07-16 会话运行时 completion and `/goal` parity

- 普通任务完成路径已经改为 会话运行时 方式：主模型基于当前对话、工具、测试和子代理事实给出自然最终回复，
  回合随即结束。旧 `delivery_closeout`、`submit_for_acceptance`、完成 marker、目录扫描验收器、自动返工轮和
  最终摘要重写已从生产代码删除。
- `/goal` 公开状态、字段、三个模型工具、创建/更新约束、token 与在线时间记账、零工具停止续跑、用量限制
  和错误状态均按当前 会话运行时 实现适配到 owner/thread/task 文件事实源。普通任务无需 `/goal`，也可使用工具
  和子代理。
- artifact registry 继续负责附件路径、hash、owner 和发送权限；文件格式检查继续留在写入工具边界。两者
  都不再决定普通任务是否完成。
- 下方带日期的旧 closeout 条目保留为问题发现与演进记录，不再描述当前主链；当前事实以上述规则和
  `docs/PRODUCT_FACTS.md` 为准。
- 所有定时 progress wake 在消费前都重新读取精确 task link；任务已
  completed/cancelled/interrupted/abandoned/superseded 时直接归档旧唤醒，不得在终态后重新启动执行器。

## 2026-07-16 `/btw` 被自然回执误消费的真测与候选

- `0794c9fb` 部署后的双 Feishu-scoped owner 长任务确认了 owner/上下文隔离、模型自主
  子代理数量和最终收口；独立重跑分别得到家庭账本 37 项、日志分析器 30 项测试通过。A 任务的 `/btw`
  账本虽然被标为 delivered，最终 HTML 和测试却没有要求的导入/成功/跳过计数，因此不能把“账本已投递”
  当作真实执行通过。
- 根因是派工后还有一个只负责写用户自然回执的 isolated model round。旧链让这个展示轮读取 task guidance，
  随即提前写 `guidance_delivered.json`；展示轮结束后，真正的任务轮再也看不到该引导。
- 本地候选给展示轮显式设置 `consume_pending_turn_input=false`。展示轮开始前或生成中出现新的 `/btw`/任务
  事件时，旧展示草稿被丢弃，真实 active task turn 在下一安全点读取输入；不靠中文语义判断。guidance 与
  runtime event 统一延后到 provider 成功返回后确认，注入后崩溃仍保持 pending，恢复轮可重放。
- 对照 会话运行时 `core/src/session/input_queue.rs` / `turn.rs` 的同 active-turn drain，通道运行时
  `attempt.queue-message.ts` 的 transcript-commit 后确认，以及 长期助手 `conversation_loop.py` /
  `agent_runtime_helpers.py` 的真实工具轮 drain。聚焦回归覆盖展示轮前到达、生成中到达、旧回复丢弃和
  未确认恢复重放；候选尚待发布并在 1.10 重新做真实 `/btw` 产物验收。

## 2026-07-16 后台任务索引与第二执行器卡口候选

- `047e24f7` 部署后的双 owner 长任务证明后台工作可以耐久续跑，但非阻塞 `wait` 结束当前 turn 后，
  scheduler 后续 turn 与新普通请求的生命周期仍不同于 会话运行时 的同 turn `wait_agent`。本轮只删除第二份
  history/compact，不把这项既有调度差距伪装成已解决。
- 同一轮还发现后一条消息被模型再次 `task_progress select` 到已经运行的根任务，第二个执行器因此重新
  检查任务现场，并把精确工具轮数复述给用户。根因不是用户说了“继续”，而是 task candidate 缺少结构化
  execution occupancy；修复不得增加中文触发词。
- 当前候选从 enabled progress policy 和未过期 background claim 读取执行占用。运行中的 active task 只进入
  同一 thread prompt 的 `Running Work` 工作索引，选择卡口再次核验同一事实；已运行返回
  `CONVERSATION_TASK_ALREADY_RUNNING`，读取错误返回 `CONVERSATION_TASK_STATE_UNAVAILABLE` 并禁止创建
  第二执行器。该索引不是第二份上下文；内部执行来源和精确工具轮数也不进入用户回复。
- 对照 会话运行时 `multi_agents_spec.rs` 的明确 child message、turn/steer 的单 active turn，以及 通道运行时
  `sessions-spawn-tool.ts` 的 required task、active-run steer queue；my-agent 保留自己的 owner/thread/task
  文件事实源，不把 `/goal` 变成普通派工前置条件。专项回归和 code-size 基线已通过，待发布真测。

## 2026-07-16 运行中子代理事件进入同一主执行轮候选

- 1.10 双用户长任务进一步证明，子代理完成通知没有丢，但一条已启动的 scheduled progress turn
  可持有 thread background claim 四十余分钟；其间后续完成 wake 只能积压，必须等该轮退出后才统一处理。
  用户看到的是长时间无新阶段反馈，主代理也不能及时按最新子任务事实调整整合路径。
- 本地候选把子代理完成、能力申请和能力获批作为结构化 runtime event data 接入现有 tool-loop 安全点。
  每次 provider 返回后、工具副作用前都会检查同一 durable task 的新事件；若有新事件，丢弃基于旧状态
  的模型动作，下一轮按 FIFO 注入。事件文本明确标为运行事实而非用户指令，不做自然语言判断。
- 启动当前后台轮的 wake id 从 active-turn inbox 排除，仍由 scheduler 确认；运行中到达的其他 wake 只有
  在模型成功返回、证明已读取包含事件的 prompt 后才标 handled。provider 在注入后失败时 wake 保持 pending，
  可由同一任务下一轮重试。
- 对照代码：会话运行时 `session/input_queue.rs`、`session/turn.rs` 的 turn-local input drain 与 stale action
  边界；通道运行时 `agent-steering-queue.ts`、`subagent-announce-delivery.ts` 的 active-run steer、顺序投递和
  transcript commit。聚焦回归已覆盖同轮接收、旧响应丢弃、单 claim 以及 provider 失败不丢事件；完整门禁、
  发布和 1.10 真实复验尚未完成。

## 2026-07-16 `/btw` 单执行轮与 `/stop` 模型传输中断候选

- 1.10 双 Feishu owner 分步复刻真测暴露了同一任务的双主执行器：前台 request 已绑定根任务且
  仍在执行时，`/btw` 既将 guidance 写给这条 live turn，又发 urgent wake 启动一条后台主轮。
  两条执行链随后可并发修改同一 workspace，导致引导看似被忽略、测试修改被覆盖和 token 异常消耗。
- 控制层候选现只在根任务没有 linked live request 时发 wake；已有 live turn 时仅持久写入 FIFO
  guidance，由原执行链在下一安全点消费。这与 会话运行时 的 expected-turn + same-turn input queue、
  通道运行时 的 active-run steer queue 以及 长期助手 的 live session pending steer 保持同一运行身份边界。
- 同轮真测还发现，`/stop` 虽已持久记录 cancel 并给执行线程立旗，但若线程正阻塞在最长 600 秒的
  provider 响应读取中，旧实现只能等模型返回后才看到旗标。候选现让阻塞传输在同一线程中注册
  关闭回调，停止时直接关闭模型 HTTP/SSE 响应，并保留为 typed interrupt，不进入网络重试。
  进一步审视发现 provider 实际运行在 wall-timeout guard 子线程，而任务名登记在外层 worker；
  候选因此在模型调用边界增加外层到子线程的 typed interrupt relay，并有界等待连接收回。
- 本地已覆盖“live turn 收到 `/btw` 不产生 wake”、“同名停止触发传输关闭且退出时清理”、
  “600 秒模型流在停止后 2 秒内解除阻塞”以及“外层任务停止穿过超时保护线程到达真正 provider”。
  focused tests 与 Ruff 已通过；完整门禁、发布和 1.10
  双用户真实复验尚待完成，当前不升级为已部署事实。

## 2026-07-15 持续目标、显式审计模式与中断后续接

- `/goal` 按 会话运行时 的 thread-persistent overlay 边界落地：它是同一 owner/channel/chat/topic
  对话上的特殊持续目标，不创建第二会话。每 thread 同时只有一个未结束目标，
  绑定同一根 task/workspace，可查看、修改、暂停、恢复和清除；自动续跑只在目标仍 active 时
  发布一个去重 wake。模型只能通过 `update_goal` 写入 `complete` 或 `blocked`，不能绕过
  owner/thread/task 绑定改其他目标。
- `/audit` 收紧为显式前缀模式：入口把 guarantee/window 固化到结构化 task attributes，
  子代理通过调度继承，watch 只读这份权威事实。普通语句、goal、summary 或 child prompt 中提到
  `/audit` 都不会暗中开启审计保证。
- `/audit` 高频判读候选已在当前工作树改为时间/条数/数据量三条件合批；自动结论直接进入
  ack-on-judge 账本，已判原始行不再交给长命子代理重复判。正常事件模型视图保持完整，只有单条
  超过当前模型安全窗口才生成带标记的头尾视图；完整原文仍可用 owner-scoped `source_ref/ack_id`
  经 `watch_stream inspect` 查回。focused tests 已通过，待 1.10 正式 CLI 五源 10 分钟复验后
  才升级为部署完成事实。
- `/stop` 从“删掉可续接任务”收紧为 会话运行时 桌面端式 interrupt：立即停止当前根执行和子树，
  抑制迟到回复，但保留 transcript、compact、task workspace、artifact 和 memory。已中断任务仍可作为
  结构化候选；用户之后自然说“继续”，模型选中精确 task id 后重开原现场，无需重发原 prompt。
  active goal 被 `/stop` 时转为 paused，不被 clear。
- 同一 `SimpleAgent` 的前台聊天和后台续跑不再共享一份可变“当前 prompt/run/workspace”字段。
  运行态按 worker thread 与 agent 弱引用身份分栏；对象释放时自动清理，避免长驻服务中 Python object id
  复用导致低概率串 prompt 或串工作区。
- 远程 owner 的文件默认黑名单扩大到所有其他 user/group owner、根模板和旧顶层私有目录；
  只放行自己 owner home 与管理员明确发布的 `~/.my-agent/shared/`。随 wheel 发布的 builtin tools/skills
  仍是公共代码能力，不依赖私有文件穿透。
- 子代理数量由模型基于真实独立工作项显式提交，不再向普通聊天暴露固定数量
  `/subagents` 入口。运行时在任何创建前同时计算每批/任务/owner/全局余量，超限整批拒绝，不截断、
  不部分创建。
- closeout 仍强制聚合子代理、进度和能力请求，但只有显式 artifact contract/expected output
  要求文件时才强制文件交付。纯分析或问答可以 `delivery_mode=message` 收口，不再因“派过子代理”
  就人为要求生成空报告文件。

## 2026-07-15 会话运行时 式当前任务引导、模型回复出口与最终收口重验候选

- `/btw` 的语义从“一次模型调用”校正为“当前这一项持久任务”：同一任务经历前台回执、后台唤醒、
  compact 或多轮工具执行时，引导仍按 FIFO 在下一安全点进入该任务；每个工具循环只注入一次，不进入
  普通聊天、其他任务或未来任务。当前执行轮存在时，控制层在写入前后核对 processing record 上的精确
  task binding；没有执行轮时才核对 owner/thread 下 active 根任务。任务已结束或切换就拒绝迟到引导。
  provider 生成途中到达的新引导会使旧响应失效，旧响应不得执行工具或结束任务。
- 任务状态、引导和完成共用 `task_transition_guard` 与 active CAS。`/stop`、`/btw`、closeout 完成互斥
  迁移；取消/切换优先时，旧完成通知不会复活或外发。实现边界对照 会话运行时
  `db887d03e1f9` 的 `steer_input`、expected turn id、FIFO input queue、next safe point 和 stale response
  discard；my-agent 沿用自己的 owner/thread/TaskRun/RWX 文件账本，不复制 会话运行时 UI 或进程内会话存储。
- 除 `/status`、`/stop`、`/btw` 等显式控制命令外，普通聊天、派工回执、等待说明和最终交付正文必须由
  LLM 根据结构化运行事实自然撰写。表达短轮不再携带旧任务正文、工具历史或内部 advisory，避免模型把
  “写一句回复”误当成重新执行任务；出口仅按空正文、真实工具调用和内部协议等机器形态拒绝，重写仍
  失败则抑制正文，不回退“正在处理”一类固定模板。任务终态、时间和产物事实只认 typed runtime facts，
  不再用中英文关键词或正则反向猜测模型文案的语义。
- 用户出口净化收敛到 `conversation/user_visible_text.py`：Gateway response、IM、后台主动投递和 transcript
  共用一套 bracket/XML/native 降级协议清洗，内部 envelope 不进入后续 compact 或 memory。交付保障层
  只归集真实产物，不再拼接“系统自检汇总”用户文字。
- MiniMax M2.7 真机复验暴露同一 assistant turn 先批量创建 5 个子代理、又重复发出 4 个单项创建。
  一次工具循环现同时记录 exact call key 与结构化 child intent key；后续完全重叠调用不再产生重复副作用，
  compact 恢复也重建同一去重状态。
- 真机还暴露旧完成标记可绕过最新 `closeout.json`：根任务在清单仍 open 时被提前标成 completed，后台
  虽继续整合却无法发送最终结果。现在完成标记必须与当前 request/run/task 的最新通过报告一致，且进度
  无 open 项、子代理聚合门通过；人类可读任务目录不再冒充 task id，后台整合按 `params.task_id` 认领
  子代理。阶段性结果可以自然汇报，但不能关闭根任务。
- 最终候选 wheel（SHA-256 `2ddf8e416951f7cc89315b2ff7e3064105e5c9a6829e5d13d336a9deb7608496`）
  已部署到 1.10，MiniMax M2.7 双 Feishu-scoped 合成用户长任务 `failures=[]`：A 精确 5 子任务并在
  `/btw` 后完成，B 精确 4 子任务并在 `/stop` 后 cancelled，Gateway 重启后未复活且迟到投递为 0；
  独立口令无串词，用户 transcript 无内部协议。该实测经 Gateway `/ask` 进入真实 Feishu owner/channel/
  conversation 作用域，不冒充 Feishu 平台真实入站。首次模型自然回执为 37.3/50.7 秒，普通并行聊天为
  6.1/10.1 秒，MiniMax 表达延迟仍需后续优化。

## 2026-07-15 回执释放后控制继续跟随持久任务

- 1.10 双用户长任务复验发现：派工回执结束后，根 TaskRun 和子代理仍在 owner 目录运行，但旧控制层只
  扫描 Gateway `processing` 请求，因而 `/status` 错报空闲，`/btw` 错报没有运行中任务。任务执行本身
  没丢，这是控制目标生命周期短于任务生命周期造成的真实断链。
- 控制目标现按可信 owner/channel/conversation 解析同一 thread，优先选择最新的 user-selectable active
  根 task link；只有任务尚未晋升时才回落当前 processing request。普通聊天请求不会盖掉后台任务控制权，
  跨用户、跨 conversation 或 task-link 读取损坏仍 fail-closed。
- `/btw` 对已晋升任务写 `target_type=task` 的一次性 guidance，并发布带相同 root task id 的 urgent wake；
  下一安全点消费后即结束，不进入未来聊天。任务恰好终态时使用 active CAS 拒绝迟到引导。
- `/stop` 先用 active CAS 把根 task link 持久化为 interrupted，再中断同 task id 下可能并存的前台/后台主代理
  执行域，并异步取消准确 lineage 的子代理。背景轮在发送前读取 durable task status，取消后的迟到旧回复
  被抑制；同名 interrupt registry 支持多个执行线程，不再由后注册线程覆盖先注册线程。
- 双用户实测中，A 在旧并行设计下把同一 thread 的后一条聊天写进正在运行的旧任务产物，证明并行 turn
  本身会让消息的时间归属不明确。当前方案不再按 task lineage 切割 transcript，也不建立平行聊天入口：
  用户消息按顺序进入同一 thread；`/btw` 作为当前 active turn 的真实 UserTurn 在安全点注入并写回同一
  transcript。task id 只约束 wake、workspace、进度和子代理树等运行事实。
- 设计复核 通道运行时 的 session/active-run registry 与按 run id abort/steer，以及 长期助手 的 live session
  `running` 状态、`session.steer`/`session.interrupt`。复用的是“控制跟随稳定 run/session 身份而非一次 HTTP
  请求”的边界；my-agent 仍使用自己的 owner-scoped thread、TaskRun、guidance/wake 和 RWX 事实源。

## 2026-07-15 精确执行轮控制与 bwrap 后代树收口候选

- 双用户分步复刻真测暴露了比“持久任务可控”更细的一层断链：A 的当前 Gateway request 正在执行第五步，
  但会话里另有更新更晚的 active link；旧 `/status`、`/stop` 选中了旧根 id，而真正运行的 request 仍继续。
  当前执行轮现把选中/晋升的 `thread_id/task_id/task_path` 原子写入自己的 processing record。写入失败会在
  工具副作用前 fail-closed，不能创建一个控制面无法定位的任务。
- `/status` 现在用精确绑定的当前 request 显示本轮任务、时长和 typed progress，同时合并根 task 与当前
  request 的子代理；`/btw` 写同一根 task 的 FIFO guidance，并以 processing record 作 expected-turn 复核；
  `/stop` 同时把根 task 标为 interrupted、给当前 request 落 `cancel_requested`，并中断两种 runtime id 与
  两条 lineage 的子代理。中断只保留现场，不改为不可恢复的 completed/cancelled。
- 同一真测还发现 bwrap 内层 `--new-session` 会建立新 session/process group。旧代码只 kill 外层 pgid，
  内层 npm/Vitest 可继续存活并持有 stdout/stderr，使 Python 的无界 `communicate()` 看起来永久卡住。命令
  终止权威已收敛到 `tooling/process_registry.py`：先快照宿主后代树和进程出生标识，SIGTERM 宽限后对
  仍存活者 SIGKILL；shell 只做 2 秒有界 pipe drain。该边界对照 通道运行时 的 process-tree termination 与
  会话运行时 的 bounded pipe drain / bwrap signal forwarding，不复制它们的运行时。

## 2026-07-16 子代理进程工具写边界候选

- 真实 1.10 任务证明：子代理的 `write_file` 会被 `allowed_write_roots` 拒绝，但同一子代理可通过
  `run_command` 的重定向写入 owner 根项目。根因不是命令规则漏了某个语法，而是 shell bwrap 把
  整个 owner home 挂成可写，文件工具与执行工具没有共享同一结构化写域。
- 本地候选把 `allowed_write_roots` 从 registry 统一传给 `run_command`、后台命令、PTY 和 LSP；
  bwrap 将 owner home 作为只读基座，再叠加当前 task 的精确可写根。实现不识别命令文本或自然语言。
- PTY session 增加 owner/task scope；LSP server 的 scope 改变时关闭重建，避免长驻进程带着上一个
  task 的可写挂载被后续任务复用。聚焦 sandbox/shell/PTY/LSP/registry 回归已通过，1.10 真机尚待部署。
- 对照代码：会话运行时 `会话运行时-rs/linux-sandbox` 的只读基座与精确 writable roots；通道运行时
  `src/agents/sandbox/{docker,workspace-mounts,fs-bridge-path-safety}.ts` 的 `none/ro/rw` workspace
  access 和挂载路径校验。
- 后续 1.10 分步复刻又发现同一 owner 的根任务也需要同一边界：模型新建任务账本后仍可用绝对路径
  写旧任务。runtime ledger 现从结构化 provider/owner/task workspace 自动生成当前 task 可写域，并过滤
  已有授权中的兄弟任务；子代理已有的更窄根保持更窄，畸形 task root 明确 fail-closed，local/admin
  bypass 不受影响。该修复与 shell/PTY/LSP 共用同一个 `allowed_write_roots`，没有增加命令文本判断。
- 本地已覆盖“后代自行 `setsid` 且继承 pipe”、普通进程组、后台 kill、日志 watchdog、精确 task binding、
  `/btw` 不被无关更新 link 抢走，以及 `/stop` 同时中断 root/current turn。1.10 安装包和真实双用户续跑
  尚待本候选完整门禁、发布与部署后复验，当前不能写成已发布事实。

## 2026-07-15 模型自然回执提速、最终事实快照与中断恢复

- 派工/wait 的 LLM 自然回复不再携带完整 tool context、native tool IR、runtime injection 或 delivery
  contract；当前用户请求与 persona 文件仍保留。1.10 先前 58/155 秒的一句话回执由此去掉主要上下文负担。
- interim/final 回复共同拒绝内部协议和无结构化依据的 ETA；完成轮会在 closeout 最终时刻冻结
  `delivery_snapshot`，再由同一模型根据真实文件名、字节数、SHA-256、进度和 gate 状态重写摘要。旧
  `submit_for_acceptance` 摘要只作为可丢弃草稿，不再把修复前的“约 21KB”带到修复后的 25,771 字节文件。
- `background_claim_heartbeat_interval_seconds=0` 的实现已修正为自动间隔；默认 claim TTL 从 900 秒降到
  90 秒。claim 新增 process-domain+pid+start_time，同一进程域旧 gateway 已死时立即接管，Kubernetes
  不同 PID namespace 或旧 claim 无法证明时保守等待 TTL。
- Gateway SIGTERM/SIGINT 现在写 typed stop request 和轻量 shutdown forensics，再走既有 stop-file drain；
  计划 stop 保留原 reason，外部信号记录为 `signal_shutdown`，不再只在 systemd 中显示无原因 clean exit。
- 设计对照复核了 通道运行时 gateway signal/drain、typing/final delivery 分栏，以及 长期助手
  `shutdown_forensics`、`resume_pending`、PID+start-time process registry；复用其边界，仍沿用 my-agent 的
  owner-scoped transcript、RWX claim 和统一 DeliveryService。
- 本地聚焦合同覆盖模型短轮、无依据 ETA、最终大小一致性、同域死进程接管、跨 Pod fail-safe、信号分类
  与既有 closeout 全族；真实 1.10 多用户长任务/重启复验在发布后执行。

## 2026-07-14 普通聊天与后台任务并行、生命周期可诊断

- 未晋升的普通 thread 不再提前创建 task workspace；只有注册表 `promotes_task` 或结构化任务动作能在真实
  工作开始时惰性晋升。派出子代理后当前 IM/Gateway 请求立即释放会话顺序槽，用户可以继续聊天、
  `/btw` 纠偏或 `/stop`，后台 TaskRun 独立继续。
- 派工回执只从 lifecycle envelope 读取 recorded/accepted/running/failed，accepted 不再冒充 running；
  模型的 `[TOOL_CALL]` 文本和子代理命令日志不会进入普通 transcript，统一用户投影另有末端净化。
- 当时的 `dispatch_supervision_auto` policy 曾用 material signature 抑制无变化巡场；该历史方案已于
  2026-08-21 删除并自动退休旧 policy。当前 child 状态变化直接发 lifecycle wake，不再周期调用 LLM；
  数据监控和用户持久 schedule 继续走各自独立的结构化调度链。
- Gateway 的 watch 返回现在有三种持久终态：计划 stop、有限 `max_cycles` 完成、无 stop 的意外返回。
  第三种写 `GATEWAY_WATCH_UNEXPECTED_RETURN` 并以非零码退出；cleanup 单独记录 heartbeat/request/
  background 三线程的 drain 结果，超时写 `GATEWAY_DRAIN_INCOMPLETE`，不再显示成正常 stopped。
- 对照检查了 通道运行时 gateway lifecycle/restart coordinator、keyed wake coalescing、subagent acceptance/
  completion outbox，以及 长期助手 shutdown forensics/first terminal completion。复用的是 typed 生命周期与
  事件驱动原则；my-agent 继续使用自己的 owner-scoped file queue、TaskRun 和投递合同。

## 2026-07-14 普通会话即时状态、纠偏与停止

- 新增 adapter-neutral conversation control protocol。只有精确 `/status`、`/btw <内容>`、`/stop`
  能进入硬控制面；普通自然语言仍是软消息，不通过语义猜测获得取消或运行时改写权限。
- `ChannelManager` 在附件下载和 `/ask` 前识别控制命令，直接调用 Gateway `POST /control` 并通过既有
  `DeliveryService` 回复。它不占同会话普通请求单飞队列，因此长任务运行时仍能即时查询、纠偏和停止；
  新 IM 复用 manager/HTTP 协议，不增加平台分支。
- `/btw` 在任务尚未晋升时写当前 request id 的一次性 guidance；任务晋升后改由上方持久 task 主链承接。
  工具循环每轮构建 prompt 时消费并标记 delivered；若 guidance 在 provider 生成途中到达，旧响应不执行
  工具也不直接结束，下一轮先纳入新要求。任务不存在或刚结束时不保存到下一任务；旧 `/btw` 列表、
  永久 runtime injection 与 `/btw-clear` 删除。
- `/stop` 对尚未晋升的 processing 请求原子写 `cancel_requested`；已晋升任务改由 durable task link 先落
  取消事实。两条路径都按 typed lineage 中断主循环并异步取消活跃子代理树，不从 goal/agent name 猜归属。
  工具循环在模型前后和工具前检查；前台 shell 每 200ms 检查并终止整个进程组。管理员 Gateway 生命周期
  `POST /stop` 保持原义，用户任务使用 `POST /control`，两者没有混用。
- `/status` 只从当前 owner/channel/conversation 的 durable task/request、typed progress、subagent state 和
  thread compact/verbose 事实渲染；不显示内部工具名、命令、路径、引导内容或“最近一次引导”。用户只看到
  同一 thread 的 compact generation；task recovery checkpoint 是运行恢复文件，不是第二种上下文或状态
  计数。跨用户和损坏请求无法证明 owner 时 fail-closed。
- 1.10 双 owner 深度对比真测捕获了 MiniMax 的另一种协议降级：自然回执夹带
  `tool_call` Markdown 代码围栏，原 bracket/XML 清洗没有命中。统一用户出口现同时剥离 fenced
  tool/function call/result/output 块，围栏外模型正文继续投递；不在 Feishu adapter 做特判，也不解析任务
  中文。对照 通道运行时 最终 assistant text 的统一 sanitizer 和 长期助手 的结构化 tool_calls/message 分离。
- 2026-07-17 Tealdeer 长任务真测中，模型第二次自然回执实际生成“分析完成后再回来汇报进展”，旧
  `INTERIM_FINAL_CLAIM` 正则跨句把它误判为“工作已完成”，两次生成用尽后 Gateway 返回空正文并把空
  assistant 错记成落账降级。当前本地修复删除完成/ETA/大小语义正则及其死代码：辅助回执只校验 typed
  runtime status、结构化 tool call、空正文和内部协议；任务状态仍由运行事件决定。该改动对照 会话运行时 的
  `AgentMessage` 与 `TurnCompleted` 分离，不通过解析 agent prose 决定 turn 状态，待发布后真机复测。
- 同一 Tealdeer 的 `/stop` 后自然续接又暴露另一条空回复链：模型已经在流里两次写出“继续原项目收尾”，
  task 也按原 id 恢复到后台，但 presentation-only 轮仍夹带未授权的 native tool call；安全闸两次拒绝后
  把 `user_reply_unavailable` 当成 `ok=true` 空 final，并为不允许的空 assistant 创建 repair。候选保持首次
  重试，在第二次仍违规时只删除结构化调用、保留经过统一协议净化的真实模型正文；若没有正文则以
  `USER_REPLY_UNAVAILABLE` 终止 Gateway 请求，用户消息和后台 task 仍耐久保留。实现只检查 pending
  reply phase、tool blocks、runtime status 和协议净化结果，不判断中文含义。对照 通道运行时
  `tui-stream-assembler` 的空 final 保留已流式正文，以及 长期助手 stream consumer 明确不把 commentary
  误当最终交付的边界。
- 同轮 Sl 真测的第一条 `/btw` 恰好落在前台 request 原子移入 done、同一 durable task 接管后台的窗口，
  旧 expected-turn 检查把“request 文件已退休”混同为“task 已切换”而拒绝，重试才成功。当前本地修复
  将 linked request 分成 `current/retired/mismatch/unavailable`：仅 `retired` 可回落核对同 thread 的当前
  active TaskRun，真实 mismatch 或账本不可读仍 fail-closed；退休窗口接受后发布幂等 wake，避免没有执行
  线时引导滞留。该语义沿用 会话运行时 expected turn 的防串线原则，但 expected identity 适配为 my-agent 跨
  前后台执行轮不变的 durable task id，聚焦回归覆盖同任务交接和真实 task-switch race。
- 设计对照：通道运行时 `src/status/status-text.ts` / `src/status/status-message.ts` 的确定性状态投影；会话运行时
  `会话运行时-rs/core/src/session/mod.rs` 的 typed interrupt/steer 与 expected turn 边界、
  `会话运行时-rs/tui/src/chatwidget/status_controls.rs` 的独立状态渲染。复用的是边界，不复制其上下文实现。

## 2026-07-14 完成摘要进入回复信封与会话历史

- 1.10 双长任务实测发现 `submit_for_acceptance` 已写出准确摘要（25/25、19/19、`/btw` 新增功能），
  但旧 `project_user_reply` 只渲染 artifact 文件名，后续同用户追问只能从原需求猜测试数。
- closeout 现在优先保留模型自然最终答复；工具提交轮则从当前 run 最后一次成功验收提交提取
  `summary`，兼容旧 `note`，以结构化 `user_summary` 传递。1.10 MiniMax 复验捕获了“自然结束但未调用
  `submit_for_acceptance`”的真实分支，防止已经生成的 7/7 说明再次被机器完成块替换。摘要只负责沟通，
  不改变任何验收 gate。统一投影保留摘要和文件名，拒绝
  `MAIN_AGENT/RUN/SUBAGENT/TOOL_CALL` 脚手架并把宿主绝对路径替换成 basename。
- 对照 通道运行时 `agent-runner-payloads.ts` / `sanitize-user-facing-text.ts` 的“保留最终文本、只清洗内部
  脚手架”，以及 长期助手 `长期助手_cli/oneshot.py` 的“执行输出静默、最终答复单独输出”。没有引入第二套
  IM 路由或额外模型总结调用。

## 2026-07-13 普通飞书对话、工作与定时共用常规主链

- 普通最终回复、后台主动消息和显式 `send_message` 已收敛到同一 `DeliveryService`。可信
  `DeliveryContext` 持有 channel/target/reply_to，`ReplyEnvelope` 只持正文和 typed attachment；
  模型不能提供或覆盖收件人。`ChannelAdapterRegistry` 统一注册 adapter、capabilities 和 target
  validator，第二个 fake IM 契约证明接入无需修改投递主流程。
- Gateway response、飞书最终回复、后台自动续跑报告和 assistant transcript 共用
  `project_user_reply`：内部 `MAIN_AGENT/RUN/SUBAGENT` 协议只留在运行时，用户只看到简短正文；
  后台轮写 transcript 前先投影，防止机器协议进入后续 compact/旧聊天检索。公开 response 不包含
  服务器 path，owner transcript metadata 才保存最小产物引用。
- 主代理注册唯一通道无关 `send_message`。目标固定取当前 scoped owner 的真实 Feishu `open_id`，
  附件在副作用前核对 task artifact registry、owner 真实路径边界、ready 状态和 SHA-256，再走 adapter
  原生 `send_image/send_file`；发送回执持久化去重。同会话下一轮“发我”直接复用最近产物 path，
  不再搜索、复制或重新生成。用户可用 per-thread `/verbose on|full` 显式开启逐工具进度；更高层低频
  阶段汇报仍是后续项。
- 飞书适配器不再把 `user_id` 当会话：普通消息使用真实 `chat_id`，话题消息使用
  `chat_id:thread:<thread_id/root_id>`；该值从 adapter 一直传到 `/ask` 的结构化 conversation。
- 每轮执行前读取同一 owner、同一 channel conversation 的累计 user/assistant 历史；当前用户消息
  保持原文和最高当轮权威，回答后双方消息按 request/message ID 幂等写回。用户消息落账失败会在
  模型前拒绝；assistant 落账失败会先交付真实结果并进入持久化 repair，下轮幂等修复。
- 固定最近轮数不再是遗忘边界。Gateway 复用现有 runtime compact 阈值、token 估算和模型 backend：
  阈值前注入完整未 compact tail；到点后把较早段总结进同 thread 的 summary+cursor+generation，原始
  transcript 永不删除，保留近期 raw tail 后继续累计；message ID + byte cursor 让后续轮直接读取新增尾部，
  不随整份历史线性重扫。旧消息幂等投影到 owner-local LocalStore，
  `session_search` 可跨 compact 找回，但不同 owner 的索引物理分离。
- `/verbose off|on|full` 按 thread 持久化。工具循环把 typed progress 与 model delta 分栏写 chunk；
  `/progress/<request_id>` 复用 result owner 鉴权，delivery worker 按 cursor 回送且不重提任务。
  `full` 输出先做凭证脱敏、owner path 替换和长度限制。
- 删除“自动选择 active task 并把旧 goal 包住当前 follow-up”的默认行为。普通聊天即使同 thread
  有旧 active task，也不注入 goal/workspace；只有结构化内部 task ref 或显式特殊模式才续接。
- 普通“帮我做事/明早提醒我”不需要关键词和斜杠命令。模型仍在同一常规对话链上自然选择工具；
  `task_progress`、`create_subagents`、`wait` 真正执行时才把当前 run 绑定为后台任务。
- 同一会话准入上限固定为 1，确保第二条消息读取历史前第一条已落库；同一用户的不同会话仍可并行，
  每用户 8 / 全局 500 的原有公平上限不变。
- per-user owner 默认开启。远程 channel 身份缺失、owner home/agent 创建失败时返回
  `OWNER_SCOPE_UNAVAILABLE` 并终态归档，禁止回落到共享 main agent。
- adapter 现在把结构化 `channel_chat_type/channel_chat_id` 一并送入 Gateway：私聊按发送用户进入
  `users/<user_id>`，群聊按真实会话进入 `groups/<chat_id>`；同一群成员变化不会改 owner，群数据也
  不会落进首个发言人的私人目录。
- 远程 scoped agent 的 prompt、文件工具和 shell 共用 owner home 这一份工作区事实；文件写工具拒绝
  owner 外路径，shell 拒绝把公共 `service-cwd` 作为额外可写挂载，只有 delivery contract/write
  boundary 明确声明的外部输出根会临时加入本轮写入范围。
- root systemd 进程的宿主 home 放宽只用于无 owner scope 的本地管理员；Feishu 等远程 owner 保留
  `/root` dangerous-root 拒绝边界，只以精确 owner home 白名单读取自己的数据，避免宿主身份扩大租户权限。
- Feishu 默认长连接、私聊密码卡默认开启；只有 SOUL 修改必须由发起人点击确认卡片，USER/AGENTS
  由所属 Agent 通过专用入口自主维护。首次设置卡不吞首条消息。默认 prompt 使用包内 `builtin:` 资源，不受
  service cwd 影响。
- 普通对话 transcript 不再重复写 owner-global dialogue memory；历史 dialogue 在检索层先扩量后
  排除，避免把有效 preference/lesson 挤出 top-k。
- active task 只作为只读候选；模型用 `task_progress action=select` 结构化选择后，update/wait/子代理
  继承同一 task id/workspace。完整任务 ID 索引由 `task_ids` 保存，候选热索引由 `active_task_ids` 保存；
  结构化 closeout 完成后只从热索引移除，后台策略和审计仍能读取历史链接。子代理继承该引用只为
  归账，`subagent_*` run source 的完成块不能关闭父会话任务。最近完成项另作为有界、非默认候选；
  `subagent-*` 与 `bg-main-*` 内部链接不进入普通用户的 active/completed 候选，也不触发工作区选择；
  用户明确要继续/修改时模型必须在文件操作前结构化 select；原工作区继续复用，但普通终态 link 不会
  重新打开，而是为当前 request 建立新 task id，误建的新占位链接标为 `superseded`。候选加载和
  compact 加载各自报告错误，不会把残缺上下文伪装成正常空历史；后台
  主代理也携带精确 thread/task 引用，closeout 后不会继续唤醒已交付任务。同会话存在候选时，首次
  progress update 必须先结构化 `select` 或 `start`，普通用户无需特殊命令。
  select 后统一工具轮会把仍指向本轮占位目录的结构化路径改写到所选任务根，派工目标、输入输出引用
  和命令中的完整目录保持同一工作区，不会只切进度账本。
  子代理迟到完成时若其 root task 已非 active，wake 只归档不执行，避免 superseded/completed 旧任务
  回流污染当前聊天。
- Feishu adapter 提交后立即返回，持久化 delivery worker 负责长任务最终回送和重启恢复；scale worker
  也复用同一 gateway 对话主链，ASGI 卡片 action 不再进入普通消息队列。
- Feishu 主动发送和引用回复共用 8000 字符分片边界；长任务结果逐片引用原消息，所有分片都会尝试
  发送，任一失败则保留 delivery 失败态供既有重试链恢复。
- Gateway typed progress 与被认领的权威请求记录放在同一队列目录；per-owner Agent 只负责执行，不能
  把过程流写进自己的私有 Gateway 目录，否则 HTTP `/progress` 与 Feishu delivery worker 看不到。
- 长任务交付扫描不把 `node_modules`、`.venv/venv`、`_deps`、`site-packages`、`.tox`、测试/lint/Python cache 当用户产物，避免有界
  artifact 清单先被数千个依赖文件占满、真正代码和测试证据反而不可见；文件本身不会被删除。
- `/result/<request_id>` 的 USER 授权在所有状态都读取请求记录 owner：热请求读 pending/processing，
  完成请求读 done/failed 归档；响应正文不充当身份来源。归档缺失、损坏或多份身份不一致时默认拒绝，
  同一用户完成态可读且跨用户仍为 403。USER 的完成 JSON 另走字段白名单，默认不公开新增运行字段，
  且移除 request/chunk path、lease、prompt 和内部错误细节；可信管理员仍可读完整诊断记录。
- 参考核对：长期助手 用稳定会话重放；会话运行时 用统一 Regular 主链和结构化 tool call；claw 用持久
  per-user session。未照搬 会话运行时 goal 自动续跑、claw 群聊首发言人归属或双 persona 路径。
- 原生附件发送另核对 通道运行时 Feishu typed media dispatcher/outbound，以及 长期助手 的通用
  `send_message` + adapter native media；复用统一工具、结构化附件和当前通道 adapter 三个边界。
- 累计上下文另核对 通道运行时 stable sessionKey/per-session verbose/compaction handler，以及 长期助手
  stable gateway_session_key 贯穿 session、compression、memory provider 的作用域；只复用作用域传递，
  不复制它们的 compact 算法或 profile-wide memory 默认值。

## 2026-07-10 外部通道目标与日志边界加固

- 主动外呼在构建 adapter/发网络请求前按 channel 声明校验目标类型；Feishu `open_id` 只接受合法
  `ou_` 目标，无效目标返回 `CHANNEL_TARGET_INVALID`，不再让同一错误持续打到外部 API。
- `DeliveryReceipt` 提供结构化 delivery status/error code；adapter 不可用、明确发送失败和发送异常
  分别使用已注册恢复合同。相同 channel/target/error 的日志在进程内有界去重，日志只保留目标长度。
- service adapter 启动即安装统一日志脱敏；第三方 SDK 打出的 URL query、Bearer、secret assignment 等
  在进入 stderr/journald 前清理，避免 Feishu WebSocket ticket/access key 出现在运维日志。

## 2026-07-09 P0 lint 收敛

- `request_worker.py` 与 `gateway_process.py` 仅做 Ruff 的导入归属、排序和前向注解清理；gateway 入口、队列、lease、审批和执行语义没有变化。

## 2026-06-11 scoped lock 语义定性：进程级单例，非线程互斥（方案A）

- 全仓调用点排查实锤：`acquire_scoped_lock`/`release_scoped_lock` 生产代码零运行时调用
  （`daemon_control.py` 仅作公共 API 转口；`supervisor.py` 的 import 是死引用，已删；
  CLI/scripts/动态引用为零）；gateway 单进程多线程路径（heartbeat/request/background/
  worker 池）均未把它当临界区用，无存量数据竞争。
- 对照组核查（长期助手 ProcessRegistry）：进程内并发一律 `threading.Lock`，pid+start_time
  只做进程身份。据此定性"同进程线程重入=刷新心跳"是契约特性而非缺陷，采用方案A：
  文档化语义 + 模块注释禁止线程临界区用法；不给锁记录加 thread id（方案B 会破坏
  supervisor 重入刷新），也不新增无调用方的线程锁原语（线程互斥直接用
  `threading.Lock`，参考 `agent/io/jsonl.py` 双层锁先例）。
- 原 strict xfail 钉子（8 线程计数互斥）按正确语义改写为
  `test_scoped_lock_process_singleton_reentrant_threads_and_cross_process_mutex`：
  锁定同进程线程重入刷新、真实子进程抢锁必败、非持有进程 release 不误删、
  release 后干净重持有四条契约。`scoped_locks.py` 补齐 LLM/人类双层中文注释。

## 2026-06-10 空闲扫描门 + recover 节流 + 队列年龄观测

- 空闲 gateway 的每 0.2s 轮询不再做 inbox 全量 glob 和 processing 恢复扫描（mtime 门 + 节流）；
  请求拾取延迟上界不变（mtime 变化即扫描）。
- heartbeat 增加 queue_ages 结构化观测；jsonl 路径锁内存泄漏修复（引用计数回收）。
- chat 消息读取改尾部倒读（conversation/store.py `read_jsonl_tail_report`），5000 行账本取最近
  20 条实测 41x 提速、结果与全量读逐位一致。
- 评估后推迟：subagent tree 投影缓存（≤10 子代理的真实场景全量扫描仅数毫秒，待 R2 真实并发
  测出瓶颈再做，避免给可变 task 对象引入缓存别名风险）；lane 化并发同理待 R2 数据。

## 2026-06-09 Gateway ready、日志和后台提醒降噪

- `gateway start` 不再只等 PID 文件出现；启动确认会等当前 pid 对应的 `gateway_state.json`
  或 `gateway_heartbeat.json` 写入 `status=running`。子进程秒退或超时未 ready 时，start
  返回失败，避免 CLI 看到“pid 活着但 state 还是上轮 killed”。
- `status` 里的 `gateway.request_counts` 只表示当前 pending/processing 热队列；
  历史 done/failed/responses 进入 `gateway.archive_request_counts`。当前建议只看热队列，
  不会因为旧 failed 归档提示“现在要排查”。
- gateway run 会在 stdout/log 中写当前运行分隔线：
  `[gateway-run] status=starting ...`、`[gateway-run] status=running ...` 和停止 marker。
  `gateway logs` 优先从最近一次 starting marker 往后显示，因此旧 run 的
  `[gateway-background-main]` 行不会污染当前 tail。
- 同步 `gateway ask` 如果已经把流式正文打印给用户，最终 response 渲染只补结构化状态行，
  不再把同一段回复打印两次。`gateway result` 和 `--json` 仍按完整 response 输出。
- 后台主代理的 periodic progress policy 不做陈年追补：错过窗口太久的周期提醒只被 snooze
  到下一次，不唤醒模型；同一 thread/task/route 的重复 policy 同轮只跑一条，其余写入
  `progress_policy_suppressed` 诊断事实。这个降噪只基于 `ProgressPolicy` 和
  `ThreadTaskLink.status` 等结构化字段，不解析自然语言。

## 2026-07-15 后台完成事件合并与用户通知分层

- 成功子任务的 `subagent_runner_finished` 信号先按
  `background_completion_coalesce_seconds`（默认 5 秒）短暂合并；同一 thread 在窗口内连续完成的兄弟
  任务只触发一次后台主代理整合。失败、阻塞和能力申请不进入这个等待窗口。
- 后台主代理仍可逐批读取结果、补派依赖工作和更新任务账，但一次成功完成后若同 root 仍有子任务未结束，
  本轮自然语言回复标记为 `partial_subagent_success` 并留在内部，不写普通 transcript、不主动发 IM。
  root 下子任务全部结束、任一失败/阻塞或需要用户决定时才进入公开投递。
- 自动监督的 material signature 继续负责跳过“状态完全没变”的周期 LLM 调用；完成信号合并负责处理
  “短时间多次真变化”。两者职责不同，显式 wait 与监控发现仍保持原语义。
- 后台轮从 `ThreadTaskLink.task_path/goal` 恢复原任务 workspace 和标题；内部“定时唤醒”提示、子代理 runner
  prompt 不能再创建同级伪任务目录，也不能覆盖根任务 goal、task path 或 owner task index 标题。
- runner 完成通知用“wake 先落盘、observation 后落盘且双向关联”的单一发布入口，消除调度器恰好夹在
  两次写入之间造成的重复 LLM 轮；wake 写失败时保留 observation fallback，并沿用生命周期 reason。
- `wait`、自动派工监督、open-coverage 续推属于内部继续工作。相关 root 仍有非终态子任务时，模型可继续
  派工/整合，但“仍在处理”占位正文不进入普通 transcript；异常或全部终态仍公开。

## 2026-07-16 停止后续接与 live steer 精确绑定

- 对照 会话运行时 active turn 的 expected turn id/cancellation、通道运行时 active session run queue/abort 后，
  `/btw` 的消费目标改为本轮已结构化选择的 `conversation_task_id`；gateway request 自己的 task id
  不再冒充持久任务。引导仍按 FIFO 一次消费，已有 live turn 时不发布第二个 wake。
- interrupted task 以 `task_id/status/goal/task_path` 进入 Resumable Work Candidates。`task_progress select`
  只保留唯一 `task_id` 参数，删除 select 的旧 `run_id` 兼容入口。
- 文件写入、命令、浏览器、PTY/LSP，以及 `create_subagents`/`wait` 都由统一 `promotes_task` 执行门检查：
  同 thread 有可选现场但本轮未绑定时，必须先精确 select 或显式 start；选择失败后不得懒创建本轮任务。
- 状态判断只读 task links、RunParams task attributes 与工具结构化调用，不匹配“继续”等自然语言。

## 2026-07-16 live steer 跨前台/后台续跑

- 1.10 双用户复测曾暴露 `/btw` 在前台 turn 确认后、耐久续轮中丢失。把已确认内容复制成 task-id
  task-id 历史投影的候选已经删除，因为它会形成第二份 task history。
- 当前实现对照 会话运行时 `TurnInput::UserInput`：`/btw` 在安全点成为 provider-neutral UserTurn，模型成功接收后
  幂等追加到唯一 thread transcript；compact continuation 用 typed carrier 保留准确位置，后续耐久 turn 则从
  同一 thread summary + raw tail 读取。未确认输入仍按结构化 request/task identity 重试，不解析补充文字。

## 2026-07-17 `/btw` 在工具历史中的真实用户位置

- 1.10 Pastel 长任务真测中，前两条 `/btw` 能改变 gradient 实现，但后续“只接受标准 wheel 安装”在首次
  投递后，模型下一轮仍回到手工 wheel 和源码 `PYTHONPATH`，最终还把未完成的安装验收写成收口报告。
- 代码级复核发现旧 native 链只把 steer 临时接成一次收尾 `role=user`；下一轮精确文本去重后该消息从
  messages 尾部消失，只剩动态首轮 prompt 中时间位置错误的 runtime injection。它不是自然语言识别问题，
  而是 turn history（执行轮历史）缺少 user item 类型。
- 对照 会话运行时 `session/mod.rs::steer_input`、`session/input_queue.rs::TurnInput::UserInput` 和
  `session/turn.rs` 的 pending-input drain，本地候选给 provider-neutral 工具 IR 增加 `UserTurn`。`/btw`
  在安全点按 guidance id 注入一次，但作为同一 turn 的真实 user message 保留在准确的工具往返位置，之后
  每次采样继续可见；text 协议用同一位置的 `ACTIVE_TURN_USER_INPUT` transcript 条目。
- 用户引导不再进入 runtime injection，也不再携带 guidance id、target、priority 等内部控制字段给模型。
  task/subagent 运行事件仍走结构化 runtime guidance；两者不混用。模型成功接收后才确认 delivery，provider
  失败前仍可重试。判断和去重只认 typed guidance id 与 task/run identity，不解析中文内容。
- 同一 run 达到上下文压力后，compact 自动续跑会新建 `ToolLoopExecuteParams`。候选版本把已送达的
  current-turn user input 作为独立 typed packet 从 loop result 带到下一份 run params，再在新 loop 中
  重建 `UserTurn`/text transcript。它不回放 guidance inbox、不二次确认，也不从 compact 摘要或提示词
  反解析用户要求；多次 compact 以 input id 去重，文字相同但 id 不同的两次输入仍分别保留。

## 2026-07-16 终态归档一致性

- worker 收口时以最终 response 的 `done`、`interrupted` 或 `failed` 覆盖请求 lease 里的旧
  `processing` 状态，再移动到 `done/failed` 目录；owner、attempt 和 heartbeat 字段继续从 lease 保留。
- 这避免 `/stop` 后出现“task link 与 `/result` 已 interrupted，但归档 request 仍 processing”的三方漂移。
  本地专项回归已通过；1.10 真机复测需等当前长任务自然收口并部署后完成。

## 2026-07-16 恢复执行与持久任务共用同一事实账本

- `/stop` 后自然续接会创建新的 gateway request/run id，但这只是新的执行尝试；原任务、workspace、进度、
  子代理树和监督提醒仍归结构化 `conversation_task_id`。
- runtime 现在通过唯一的 task identity 解析器给 guidance、task_progress 工具、需求/派工 seed、coverage、
  wait、监督提醒和 delivery closeout 提供同一个账本键。主代理恢复轮不会另读一份空账，子代理 task_local
  scope 也不会因继承父 `conversation_task_id` 而误写或关闭父任务。
- 本地回归覆盖“恢复轮存在开放待办时必须被收口门看见”“恢复后派工仍写原账”“恢复后的主代理仍认领
  原任务下未结束子代理”；不读取“继续做”等自然语言来决定归属。

## 2026-07-16 目标续跑的紧凑任务现场

- `/goal` 每个 continuation turn 继续读取同一 thread 的 summary + raw tail；精确 task link、子代理树与
  `task_progress_summary` 只是补充运行事实，不替代或裁剪会话历史。
- 根任务进度、child 状态、checkpoint 与 agent tree 继续写入各自现有的结构化事实源；不再生成另一份
  task recovery rollup 或 continue packet。派工种下的 child 进度项只按精确
  `run_id + canonical DONE` 自动闭合；这些事实只供崩溃恢复和进度核对，不作为另一份模型上下文。
  `integrate-and-verify` 仍由主代理根据真实整合与测试事实更新，不成为系统验收硬门。
- `task_progress select` 成功结果同时返回 task 状态、是否复用原 workspace、匹配 goal 状态和是否已安排
  continuation。普通回复仍由模型生成，但模型不应在结构化事实显示原目标已恢复后再次向用户索要任务。
- `/stop` 后用户补充要求或说继续时，消息仍追加到原 thread 历史；模型可用结构化 task candidate 重新选择
  原 workspace。系统不再拼装一份“最近 guidance”表达事实包，也不要求用户重发原 prompt。

## 2026-07-17 live turn 期间普通消息不再排队等待

- 真实 Feishu 客户端从同一用户发起约十分钟长任务，任务运行中再问一句普通问题；旧实现直到长任务最终
  回复后才启动第二个 Gateway request，用户实际等待约八分钟。上下文回答正确，根因是同 conversation
  admission 把所有普通消息一律留在队列，而不是记忆或模型问题。
- 对照 通道运行时 默认 active-run `steer` queue，Gateway `/ask` 现在先按认证 owner/channel/thread 和 live
  processing record 决定是否进入当前 turn；消息正文保持不透明，不做自然语言任务分类。命中时复用现有
  `/btw` durable guidance、expected-turn 竞态保护、UserTurn 注入与旧响应失效主链，不新增 chat/task 会话。
- 只有 durable task、没有 live Gateway request 时，普通消息仍创建新 turn 和回复信封；显式 `/btw` 才能
  直接纠偏这种后台任务。adapter 收到 typed `status=steered` 后复用原 request 的投递记录，不重复派工。
  新用户输入会重新开放一次模型自然 commentary，普通工具轮仍保持抑制。
- 本地针对性回归覆盖 HTTP 真入口、owner 隔离、精确 linked task、无 live turn 回落、重复投递抑制、模型
  commentary 重开和 guidance safe point；网关/adapter/context 关联测试 207 项通过。1.10 真实 Feishu
  平台复验完成前仍按候选能力记录，不升级产品事实等级。
- `9e04affd` 精确部署到 1.10 后，真实飞书用户 `ou_6591…a895` 从客户端发送
  `FEISHU-LIVE-STEER-A-20260718` 长任务；request `req_1784311406795_1506981_0` 在约 12 分钟内始终是
  唯一 processing request。任务运行中到达的 `FOLLOW-A-20260718` 进入同一个 typed guidance/UserTurn
  队列，模型先用一句话回答双报告类型，原任务继续运行并最终 `DONE`，没有第二个 Gateway request。
- 最终飞书引用回复只包含模型自然摘要、相对任务位置和测试结果；当前任务的可见 commentary/final 未出现
  XML、工具协议或成串命令。交付目录复制到不含原缓存、原报告和 `work/` 的干净临时目录后，38/38 测试、
  CLI help、四份 YAML/JSON 示例预览、正式写出、坏 JSON 和缺失文件路径均独立复验；预览为 27 个改动和
  1 个刻意设置的引用错误，成功重新生成 JSON/Markdown 报告。第二个真实飞书用户尚未完成，因此本项仍
  保持“部分可用”，不把单用户平台证据外推为两用户隔离或规模证明。
- 第二个真实飞书用户 `ou_1be7…f921` 从客户端发起“青竹账单”长任务时，原消息在模型调用前失败为
  `ConversationPersistenceError`。根因不是模型或 Feishu：该 thread 留有旧版本合法 `cleared` goal tombstone，
  新版本目标状态枚举把它误判为损坏。当前工作树只兼容这一种有明确历史来源、身份字段完整的 tombstone，
  等价为“没有活跃 goal”；其他未知状态继续 fail-closed。store 与真实 Gateway 普通聊天回归均覆盖该边界。
- 修复后通过同一 Feishu owner、channel 和 `conversation_id` 的可信 localhost `/ask` 重试；该次是服务器侧
  模拟，不冒充第二次平台客户端入站。长任务只创建一个 child，并在 Gateway 重启后续跑到 48/48 自测，
  普通 follow-up 与 `/btw` 都作为同一 thread 的 UserTurn 入账，最终经真实 Feishu 主动投递。过程中还复现了
  child goal 携带同 owner 的旧 `tasks/.../output/<project>` 绝对路径，导致它反复写入被 owner wall 拒绝；
  当前工作树在已有输出引用边界内把这种显式路径事实重绑定到当前任务的 canonical output，保留 `output/`
  后的项目尾部，结构化 `user_requested_output_dir` 仍具有优先权，未增加项目名或中文语义判断。
- 独立干净副本验收没有接受模型的“48/48”自述：CSV 报告会重复写缺失项，常见“两笔银行扣款对一笔支付”
  没有产生 `DUPLICATE_CHARGE`，支付端已退款而银行无退款时没有产生 `MISSING_REFUND`。验收同时发现一个
  无关历史 child 记录损坏会让后台送达策略 fail-open；统一运行门已改为仅在精确根任务链接为 `completed`
  时允许最终发送，否则压住中间整合，并用真实坏 `task.json` 回归覆盖。
- 1.10 恢复后部署候选 wheel，并沿同一 owner `ou_1be7…f921`、同一 conversation
  `oc_388c…cddd` 通过可信 localhost Feishu-scoped `/ask` 发起纠错 request
  `req_1784346588830_4174_0`。该次是服务器侧模拟，不冒充第二次平台客户端入站。模型最初选中本轮空
  占位 task，但第一次文件变更带有原项目的显式绝对目标；统一 effect 前门据此只在同 thread 的精确旧 task
  中选回 `req_1784318904169_1524072_0`，重新打开旧现场并 supersede 空占位，没有创建或复制第二个项目。
- 运行中两次 `/btw` 都返回 typed `steered/active_turn_input`，在同一 request 的安全点成为真实 UserTurn；
  第二次纠正模型手敲错的中文目录名后，后续读取和编辑命中原项目。请求共 81 个工具轮、1715 秒并自然
  完成；HTTP response 与同一 transcript 的用户投影没有 XML、工具协议或成串命令。服务器侧 `/ask` 不走
  Feishu adapter 的 pending reply 队列，因此这条完成回复本身不作为平台原生收件证明。
- 把原项目复制到独立临时目录后，55/55 测试、CLI help、JSON/CSV/Markdown、月度/商户汇总、坏输入、
  六类稳定原因码、无逻辑重复异常均通过；远端项目和独立副本都没有 symlink、缓存、pyc/pyo，任务 output
  清理后只剩 `qingzhu-bill`。A/B owner 的产物、SOUL/USER/AGENTS、skills、memory 反向检索无交叉命中，
  A 的私有 token 在 B 为 0，B 的青竹项目 token 在 A 为 0。
- 真测还暴露出 selection 后进程工具默认 cwd 仍停在服务 workspace，导致模型反复拼接超长路径。对照 会话运行时
  的 turn cwd 和 通道运行时 的 workspaceDir，统一 registry 候选在没有显式 `working_dir` 时把结构化
  `task_root` 注入 `run_command` 与 PTY start；显式目录仍优先，ShellTool 继续校验 workspace roots，且完全
  不检查命令正文或用户文字。SHA-256
  `bd8f9eb2828f53a27622aeaa17f0596224b3219177861e2d27ce3718c9906ca1` 的最终 wheel 已精确部署到 1.10；
  安装后源码探针确认新逻辑已加载，Gateway/Feishu active、`NRestarts=0`、WebSocket connected。
- 部署后请求 `req_1784350294487_12075_0` 用同一 B owner/conversation 的普通中文要求主动发一条无附件
  结论。模型在 1 个工具轮调用统一 `send_message`，Gateway 记录
  `NATIVE_CHANNEL_SEND_OK channel=feishu attachments=0 mode=proactive`；响应投影为纯自然正文，无工具协议。
  A 中 `qingzhu-bill` 目录数仍为 0，B 中仍为 1，没有因发送验证新建或复制项目。

## 2026-07-19 前后台共用会话 lane 已发布

- `f19d0be4` 把 Gateway 前台 turn 与 scheduler/background main 都接到
  `conversation/run_claim.py` 的同一持久 per-thread lane；拿到 lane 后才读取 history、compact 和 task
  状态，释放前先写入 assistant transcript。不同 thread 不共享该 lane。
- 完整本地门禁、远端 main 和 1.10 精确 wheel 部署均完成。真实一次性提醒跨 Gateway/Feishu 重启后只
  产生一个 scheduler history 和一次 `NATIVE_CHANNEL_SEND_OK`；另一提醒到期时前台长任务持有同 thread，
  后台未并发执行，直到 `/stop` 释放 lane 后才投递一次并自动暂停。这条证据不依赖回复正文判断 busy、
  完成或送达。

## 2026-07-19 promotion 后动态 workspace 重绑定已发布

- 真实长任务在 `task_progress start` 后已有正确 owner task 目录，但 write/run 连续返回
  `WRITE_FORBIDDEN`。根因是 `ToolLoopExecuteParams.task_attributes.run_workspace` 已动态更新，而初始
  `write_boundary` 仍保留旧 service cwd；两份结构化路径事实发生漂移。
- 候选修复位于所有主/子代理工具共用的 `tool_runtime_ledger.write_boundary_with_runtime_ledger`：当前
  `run_workspace` 每次工具调用覆盖旧 task root/output/work；远程主会话以精确
  `conversation_thread_id + conversation_task_id + context_scope=default` 重绑定当前 task 写根。
  `task_local/control_plane` 仍过滤并保留子代理自己的窄授权，非法 owner/task 路径继续 fail-closed。
- 代码参考不是文档类比：会话运行时 `会话运行时-rs/core/src/session/turn_context.rs` 从当前
  `TurnEnvironment.cwd/workspace_roots` 构造 turn；通道运行时
  `embedded-agent-runner/run/attempt-tool-base-prepare.ts` 把 `effectiveCwd/effectiveWorkspace` 传给实时工具
  构造，`agent-tools.ts` 再用该 root 建 filesystem/shell guard。my-agent 只适配这一“当前结构化 workspace
  是工具权威输入”的做法，没有新增 IM 分支或自然语言规则。
- 聚焦回归已覆盖旧 bootstrap root 被替换、主会话重绑定、子代理不扩权、任务选择、write boundary、
  owner 隔离和并发隔离；完整本地 CI 通过。`04c34947` 已推送远端 main 并精确部署 1.10，配置未漂移，
  Gateway/Feishu active 且 `NRestarts=0`。正确 A/B conversation 的普通续作均选回原任务；A 的
  run/write 工具不再命中旧 bootstrap root 的 `WRITE_FORBIDDEN`。长任务独立产物验收继续单列记录，
  不用“能写入”替代功能正确性证明。

## 2026-07-19 completed projection 与 live turn 交接候选

- 真实 A owner 的根 task link 已是 `completed`，但 exact background claim 仍为 live；旧控制入口只读 active
  link，造成 `/status` 假空闲、`/btw`/`/stop` 失联。当前控制入口先读取该 thread 全部可选 root links，
  再用一次 per-thread execution snapshot 识别 `completed + live` 的短暂交接态。`interrupted`、过期 claim
  和不可读状态均不进入候选；`/stop` 用被选 link 的真实旧状态作 CAS expected status。
- `/btw` 命中已有 live claim/policy 时只追加同一 task 的 durable guidance，不再额外发 wake 创建第二执行器；
  状态不可读时不猜测。`_durable_task_is_current`、status、steer、stop 共用同一候选解析器。
- 代码级参考为 会话运行时 `session/mod.rs::steer_input` 对 exact active turn/expected turn id 的检查、
  `tasks/mod.rs::on_task_finished` 对 active turn 的清理；通道运行时 active-run control 也把 steer/abort 绑定到
  runtime run id。my-agent 的适配权威是 task link、claim、policy 和 CAS，没有自然语言分类。
- 聚焦回归覆盖 live completed 可 status/steer/stop、过期不复活、interrupted 不复活、同 thread 多个历史
  completed task 仍只读一次 policy/claim。完整本地门禁、发布和 1.10 真机反证尚待完成。

## 2026-07-19 显式文件发现不再对忽略目录假阴性

- 真实 B owner 两次宣称原项目没有 pyc，但外部 `find` 每次都看到同一 `__pycache__ + 4 pyc`。tool-output
  index 证明模型传入精确原路径和 `**/*.pyc`；结果为成功且 0 命中，因为公共 file discovery walker 在
  glob 匹配前裁掉了缓存目录。
- 当前 `find_files/list_files` 对宽泛发现仍默认跳过 `.git`、`node_modules`、venv 和缓存；只有调用方没有
  显式设置 `include_ignored`、显式 glob 在默认可见区零命中时，才自动补查忽略目录并在 result envelope
  标出原因。显式 false 可关闭回退，显式 true 始终包含，分页和上限语义不变。
- 对照的具体代码是 会话运行时 `linux-sandbox/src/bwrap.rs::ripgrep_files` 中 `rg --files --hidden --no-ignore`
  的显式 glob 语义，以及 工具运行时 `packages/工具运行时/src/tool/glob.ts` 到
  `packages/core/src/filesystem/ripgrep.ts::filesArgs` 的 host-side glob walker。my-agent 保留自己的降噪默认，
  只修显式查询忠实性；没有以用户正文或样例项目名作判断。

## 2026-07-19 子代理 workspace 与父 conversation task 解耦已验证

- 1.10 真实 A 长任务中，父代理开出两个 child 后，child 为写入用户明确延续的旧项目绝对路径，命中了
  `select_current_conversation_task`。旧实现把“child 选择自己的工作目录”错误提升成“全局切换当前
  conversation task”，将本轮父 link 写成 `superseded`；parent-link lifecycle 随后按结构化状态取消两个
  child，前台只留下进度话而没有最终交付。
- 当前候选保留 `conversation_task_id` 作为 child 汇报所归属的父任务 lineage，只在 child runner 的
  `run_workspace` 内重绑定精确命中的 owner task path。child 不调用 reopen、supersede 或全局 select；
  任务 promotion 只验证父 link 仍 active，不能把已重绑定 cwd 覆盖回父目录。另一个真实失败面
  `capability_request` 现按缺参数、tool not allowed、run lookup 三类结构化错误返回，删除无码
  `UNKNOWN_ERROR` 回落。
- 具体对照 会话运行时 `会话运行时-rs/core/src/tools/handlers/multi_agents_common.rs`：`thread_spawn_source` 单独保存
  `parent_thread_id`，`apply_spawn_agent_runtime_overrides` 单独复制 turn `cwd`；以及 通道运行时
  `src/gateway/session-child-sessions.ts`、`src/agents/tools/sessions-spawn-visible.ts`：child session 与
  `parentSessionKey/spawnedBy` 分字段持久化。适配没有新增 IM 分支，也不解析用户自然语言。
- 回归锁定：child 精确重绑定后原 task 与本轮父 task 均保持 `active`，父 task id 不变，后续 promotion
  不覆盖 child cwd；主代理原有 completed-task select 仍会 reopen 旧 task 并 supersede 占位 task。
  聚焦测试、完整本地 CI、wheel 发布边界和 artifact clean-package 均通过。
- 1.10 双 owner 真测各沿原 Feishu conversation、persistent root task 和原项目创建两个新 child，四个 child
  全部 `DONE`；父 task 未被 supersede，A/B 都没有新建或复制第二个项目。A 的 typed `/btw` 进入同一 live
  task，结束后的迟到引导明确拒绝；公开回复只含自然 Markdown，没有新工具协议、命令串或宿主绝对路径。

## 2026-07-19 scheduler 空轮询不再重写 owner 账本

- 1.10 两个真实任务结束后，Gateway 仍稳定占约一个 CPU 核。`py-spy` 的具体调用栈落在
  `reserve_due_runs -> _write_store_unlocked -> OwnerQuotaAdmission.check -> owner_logical_usage_bytes`：没有
  due run 的 background owner tick 仍重写相同 store，并扫描大体积 owner home。
- `SchedulerRepository.reserve_due_runs` 现在只有真正预留 run 或推进 misfire（`reserved or skipped`）时才
  写账本；未到期、或已有 active run 而不能选择的轮询保持字节与 mtime 不变，也不触发 quota scan。
  回归使用真实 `OwnerQuotaEnforcer`，把 quota walker 替换为“被调用即失败”，同时覆盖上述两个 no-op 分支。
- 具体参考 通道运行时 `src/cron/service/jobs.ts::nextWakeAtMs` 只返回最早 enabled `nextRunAtMs`，以及
  `src/cron/service/timer.ts::armTimer` 在无下次执行时等待、对 past-due tight loop 使用最小重触发间隔。
  my-agent 只适配“没有状态变化就不写权威账本”的边界，仍由 owner JSON 保存 job/run、SQLite 只保存 wake
  投影，不复制 通道运行时 的调度存储。
- 候选 wheel `8acd7d73…c3d7` 部署 1.10 后，同一 5 秒 `/proc` 口径从 `1.008` 核降为 `0.030` 核，RSS
  从约 444 MB 降为 130 MB，线程从 18 降为 12；398 MB active-task 索引的 mtime/size 未变化，`py-spy`
  只见各 Gateway loop 等待，原 owner quota 栈消失。配置 SHA-256 未变，Gateway/Feishu 均 active、
  `NRestarts=0`。

## 2026-07-21 正式 Gateway 不再运行全局模型 planner

- 1.10 空闲期在 `owners/local/main/.../subagents/parent_planner_report.json` 与
  `PARENT_PLANNER_LOG.md` 看到同一陈旧 cancelled child 被每 30–90 秒重复送入 LLM planner，决策均为
  `applied=false`；同一正式 Gateway PID 因而长期占第二个本地推理槽。该调用不是 Feishu owner 的 request、
  scheduler due run 或 active continuation。
- 根因是 `gateway run` 主线程仍执行 process-wide `watch_subagents`，其默认身份为 `local/main`；正式
  request pool 与 owner-scoped background loop 已经各自管理用户任务，第三条全局模型循环没有 owner
  authority。当前 `gateway run` 主线程只等待显式 stop record；用户 request 和 durable continuation 继续由
 既有 owner-scoped event loop 执行。Gateway 专属 planner/watch/force-lock CLI、options、临时 capability
  router 已删除，显式独立 daemon/subagent dispatch 能力不受影响。
- 同轮流式工具真测发现，完整工具块已经可执行时，provider worker 的尾部仍可能继续生成并与下一轮并占
  两个槽。公共 model-generation 边界现在先调用该 worker 已注册的 typed transport close、短暂 drain，
  再把完整工具块交给执行器；普通 `/stop` 复用同一路径。持续有字节到达的 SSE 则由 transport 自己执行
  idle timeout，不再被外层 `request_timeout` 当总墙钟二次截断。
- 聚焦 Gateway command/background/tool-generation 回归 92 项通过；候选 wheel
  `6d7d031171c33918e56e95cf9d1225f0c8dba71374f3683ed5fe385e481ba9b1` 部署后，正式服务 active、
  `NRestarts=0`，旧 parent planner 报告 mtime 冻结，空闲时无 8899 连接。Chalk 本地 Qwen 任务只出现一条
  正式 Gateway 模型 socket；模型自身长 reasoning 和 `MODEL_INCOMPLETE_RESPONSE` 作为完成质量失败单列，
  不再用第二个 planner 或自动 replay 掩盖。

## 2026-07-21 同一 Chalk thread 的 steer/stop 真实反证

- 普通 Feishu-scoped `/ask` 以顶层 `conversation_id` 精确恢复原
  `thread-316db5cc5d814cba`、task `req_1784617354048_243904_1` 和原 `output/pychalk`；没有创建第二个项目。
  第一轮本地 Qwen 用满 16,314 token 后返回 `MODEL_INCOMPLETE_RESPONSE`，没有编辑；底座没有把半截
  reasoning 当成功，也没有自动重放同一 turn。
- 第二轮运行中 `/btw` 返回 typed `steer`，其 guidance 在下一安全点以 dedupe key 写入同一 transcript，
  后续模型上下文确实包含纠偏消息；没有第二个 Gateway request。模型仍未形成编辑后，`/stop` 返回 typed
  stop，当前 request 进入 `interrupted/INTERRUPTED`，8899 socket 关闭，原 task 标为 interrupted，thread、
  workspace 和 transcript 保留；随后直接使用本地模型像 会话运行时 停止按钮一样在同一现场说“继续”，没有
  等待 MiniMax 刷新。
- 后续普通中文工作按样式顺序、list casting 与 `apply/call/bind`、HEX/level 0/`visible` 三段推进，多次
  `/btw` 都进入当时同一 active request；一轮无新工具动作后再次 `/stop`，然后仍从同一 task 继续。模型
  曾重复 helper、猜错路径和改错 level 0，分别由后续工具事实、写边界和测试纠正，没有项目专用底座补丁。
- 远端原项目最终 42/42。重新复制的 `/tmp/my-agent-accept-chalk-clean.Fs2Fij` 与远端 8 个文件逐文件
  SHA-256 一致，独立运行同为 42/42 且无 cache、pyc、egg-info、build、symlink；另一个临时副本完成
  fresh install、导入和关键行为断言。控制链和完成质量现在分别有证据，但本地模型的长 reasoning、重复
  读取和多次纠偏仍是效率限制。
- 最终 wheel SHA-256 `4b882778888ee915a54a8414651965753999acfd935a4c0053d0c2755052bdcb` 的
  安装 archive hash 在 1.10 精确匹配；正式配置继续指向本地 8899，Gateway/Feishu active、`NRestarts=0`、
  请求队列为空、飞书 WebSocket connected。部署过程没有启动隔离 Gateway 或第二份飞书服务。

## 2026-06-09 活跃请求状态可观测

- CLI/gateway status 会显示 `requests/processing/` 中活跃 request 的结构化事实：
  `id`、`status`、`lease_owner`、`attempts`、lease age、heartbeat age、updated age，以及仍存在的
  `chunk_stream_path`。这只用于观察和排障，不改变调度、不新增硬门。
- processing request JSON 损坏时会显示 `gateway processing_load_error=...`，避免慢请求、坏记录或
  worker owner 丢失时只看到队列计数。

## 2026-06-07 本地轮询降噪和入口唯一化

- `gateway_process.py` 统一导入 `cli/gateway_loops.py`，删除旧的 `_gateway_process_service.py` 副本，
  避免 gateway worker loop 两份实现漂移。
- gateway 命令实现并回 `gateway_process.py`，删除 `_gateway_commands.py` 私有转发层；测试也改为 patch
  公开入口，避免真实入口和测试入口分裂。
- gateway run 状态、线程装配、恢复记录和 stop/kill helper 都并回 `gateway_process.py`；
  gateway 命令主链路不再绕同目录私有 helper。
- systemd/launchd service unit 生成和安装/卸载收回 `gateway_service.py` 一个公开边界文件，删除
  `_gateway_service_handlers.py` / `_gateway_service_unit_gen.py` 私有 facade helper。
- `agent/gateway_parts/supervisor.py` 保持一个直接 supervisor 边界文件；启动、停止、重启、
  heartbeat 健康判断和 runtime status 写入在同一文件内组织，不再保留中段 facade 注释或重复模块
  import 块。
- 进程控制入口收敛到 `agent/gateway_parts/process_control.py`；`daemon_control.py`
  不再顺手转出口 `is_pid_alive` / `wait_for_pid_exit`，只保留 PID record、后台化、
  scoped lock 和 shutdown request。
- chat/TUI 和 `gateway ask` 的响应等待改为按响应文件 `(mtime,size)` 状态读取：
  响应文件不存在或未变化时不反复 JSON 解析，出现或变化时仍走统一 load_error 结构化报告。
- `gateway_request_poll_interval` 改为小数秒配置，默认 `0.2`；后台 request worker 空闲时更快发现新请求，
  但仍保留最小 `0.05s` 防止误配造成空转。
- 流式 chunk 不再长期留在 `requests/processing/`：request 完成时随 request 一起归档到
  `requests/done/` 或 `requests/failed/`，response 记录 `chunk_stream_path`；
  chat/TUI 和 `gateway ask` 会从 processing/done/failed 候选路径补读，避免 response
  先出现时丢最后一段流式输出，也避免 processing 被历史 chunk 污染。
- `submit_gateway_ask()` 统一使用 gateway UUID 请求 ID 生成器，不再用毫秒时间戳拼 `gw-*`。
  多个 chat/TUI/gateway client 同一毫秒提交时，请求文件不能互相覆盖。
- 普通 CLI `gateway ask` 不再提交孤立 request：默认带 `gateway-cli/default` conversation；
  HTTP `/ask` 也会从结构化会话字段生成 conversation。worker 执行 follow-up 时可直接注入
  active task root/output/work，避免续接请求新建空任务目录后反复找不到上一轮产物。

## 2026-06-06 入口收敛

- 删除 `agent/gateway_parts/runtime.py` 聚合层。
- adapter 直接调用 `request_worker`，processing 恢复直接调用 `lease_service`。
- `agent/gateway_parts/__init__.py` 仍是包级公开入口；内部实现不再通过 `runtime.py` 多跳转发。
- CLI/gateway/TUI 状态行的当前上下文 token 展示归 `response_renderer.py`，不再通过单函数
  `context_tokens.py` 跳转。

## 2026-06-06 会话上下文精确化

- gateway conversation follow-up 只把 `status=active` 的 task link 当作活跃任务。
- `completed`、`done`、`closed` 等旧/展示状态不会再通过“非关闭即活跃”的逻辑污染后续请求。

## 2026-06-04 收敛

- gateway 文档改为只描述当前 `gateway_parts/` 队列、worker、lease、HTTP 和 renderer。
- request/response/history/status/PID 读取错误必须结构化显示，不能吞成空状态。
- gateway heartbeat 和 HTTP `/status` 只统计 pending/processing 活跃队列；status/doctor
  等显式诊断命令才读取 done/failed/responses 归档计数，避免历史响应目录变大后拖慢本地热路径。
- 后台启动失败需要早期健康确认并写明确失败状态。

## 2026-07-24 通用副作用 operation claim 发布

- `ToolRegistry` 的正式副作用入口默认要求 owner 自己的 `LocalStore`；缺 store、claim 失败或 schema
  不完整都停在 handler 之前。只有明确的 dry-run/单元合同探针可显式关闭该要求，正式
  `SimpleAgent` 没有豁免分支。
- provider 原生 call id 进入可信 `ToolCallEnvelope`；没有 provider id 的文本调用才使用轮次位置。
  `status/operation_id/idempotency_key` 等同名工具参数不再被协议黑名单误删，模型 payload 也不能覆盖
  外层 operation identity。
- 所有 mutating/dangerous `ToolRuntimePolicy` 已声明 `idempotency_policy.scope="operation"`；静态测试会阻止新增副作用
  工具漏接账本。`send_message` 原进程缓存和磁盘 receipt 已删除，取消控制逻辑也从 Tool wrapper 提成
  共用领域函数，避免内部控制面看起来像直接绕过 Tool Gateway。
- 当前已通过原子 claim、并发同操作、进程失活、远端 lease、终态损坏、结果重放、身份冲突、owner
  隔离、读工具单次重试/副作用零盲重试及全部受影响回归。完整 pytest 到 100% 且退出 0；
  Ruff、compile、import/offline、contract pyramid、replay、strict code-size、doc-sync、diff 和干净
  wheel 制品门均通过。worktree clean-package 仅因明确保留的未跟踪运行数据/交接文档失败，并正确报告
  数 GB 运行数据；这些内容未进入 wheel。
- 本地 8899 Qwen 与 MiniMax-M2.7 分别在独立 profile 中实际走通原生
  `write_file → read_file`；两轮各只有一个目标文件和一条 succeeded operation 记录，operation id
  分别来自 provider 原生工具调用，没有消息、网络、命令、Memory、Persona、Skill 或 Scheduler
  副作用。
- 提交 `9f03140e` 已推送远程 `main`；由精确 Git archive 构建的 wheel SHA-256 为
  `f702784aa7a548261ff0d164beae836f00aadf1d2d2fcbc698aae2cd959e448b`，distribution boundary 与
  artifact clean-package 通过并部署到 1.10。源码/安装树七个关键模块逐文件一致，Gateway/Feishu
  active、`NRestarts=0`、8420 只监听 loopback、队列为空、bwrap 可用、模型保持 MiniMax-M2.7。
- 两个既有真实 Feishu owner 沿各自原 conversation 并发完成 `write_file/read_file/send_message`。
  每个 owner 恰好一个自己的标记文件、对方标记计数为 0；每个 run 的权威账本恰好一条 succeeded
  `write_file` 和一条 succeeded `send_message`，Feishu delivery 均返回 sent receipt。B 随后实际调用
  `read_file` 读取 A 的绝对路径，得到 `TOOL_INVALID_ARGUMENTS` 且无副作用。安装版额外验证相同
  operation 首次执行、精确重放不执行、同身份换参数拒绝，handler 计数严格为 1。
- 上述两个真实 owner 请求来自可信 localhost Feishu scope，真实消息由 Feishu API 发给两个账号；
  macOS 锁屏阻止了本轮再次从两个桌面客户端发起入站，故不把该部分写成新的客户端入站证明。

## 2026-07-25 超时后效应与 Feishu 原生去重

- Tool Gateway 不再把 mutating/dangerous timeout 当作普通失败：只要实现可能已经越过副作用边界，
  operation 就持久化为 unknown，后续同 call 或同 business key 先核对而不是重做。外层仍能看到原始
  provider/tool 错误码，但它没有重新执行权。
- `send_message` 从 operation scope 收紧为 business scope；稳定键只取当前 owner 的 provider/target、
  可信 request/run、规范正文和附件引用，不取模型 call id。DeliveryService 把该键分别派生给正文和每个
  附件，ReplyEnvelope 与模型参数不能注入或覆盖。
- Feishu adapter 对同一正文、引用回复、长消息分片和媒体消息生成稳定 UUID；一个分片重试时 UUID 和
  payload 保持不变，不同分片/附件互不碰撞。实现参考 长期助手 Feishu adapter 已使用官方 UUID 的做法，
  但没有照搬其每次 `_send_raw_message` 都生成新 UUID 的重试行为。
- 会话运行时 参考点是 `会话运行时-rs/core/src/tools/lifecycle.rs` 的统一 call id/start/finish/aborted 生命周期；
  长期助手 参考点是 `agent/tool_executor.py` 对 timeout 的 `effect_disposition=unknown`。两者都没有可直接
  复用的 owner-local、跨进程业务 operation ledger，因此 my-agent 继续使用自己的统一 Tool Gateway，
  没有新增 IM 专用执行器。
- 1.10 候选先切正式单实例到本地 8899，再恢复 MiniMax-M2.7；两边均沿现有两个 owner/thread 顺序调用
  真实 `send_message`。本地 A/B 与 MiniMax B、纠正后的 MiniMax A 都各自产生唯一 succeeded operation，
  无重试、无跨 owner、无 Memory/Persona 写入。MiniMax A 的一次“未调用工具却宣称发送”被账本与
  Feishu 历史反证，普通中文纠正后才真正发送；当前底座不会从自然语言猜“这句话本应调用什么工具”，
  也不会把模型自述当作送达事实。
- chat/gateway 多客户端共享队列时，应减少本地膨胀和重复读写，避免本地成为模型之外的瓶颈。

## 2026-07-25 多外部写部分结果与双 owner 复验

- Gateway 没有增加批事务、Saga 或自动补偿入口；同一模型轮的多个工具调用仍按原顺序通过统一 Registry
  和 operation coordinator。权威 completion 保存失败时，即使实现已返回成功也只能向模型交付
  `TOOL_OPERATION_OUTCOME_UNKNOWN`，并保留原 claim 阻止重复副作用。
- 显式绝对写路径不再经过旧 escape-relocate 改到 task output；目标身份保持不变，由 owner/write
  boundary 明确成功或拒绝。相对 task 路径的既有结构化解析不变，专属兼容函数、请求字段和旧测试已删除。
- 精确 wheel
  `01ab7d15df60b1db613e7d52e211ce46bb3fe04bb52b7a0ccb43dd835b587614` 部署后，A 的 Feishu-scope
  请求 `req_1784957378024_1282076_0` 实际写账本为 succeeded/failed/succeeded；B 的请求
  `req_1784957456380_1282076_1` 跨 owner 读失败后，自己的写/读继续成功。A/B 各自的
  `send_message` 只有一条 succeeded operation，generation=1；原生通道日志的目标后缀分别匹配两个
  open_id，Feishu API 均返回 sent receipt。
- 两项正式服务保持 active、`NRestarts=0`，队列回到 0/0，没有新增服务、端口、owner、conversation
  或项目。这些是真实 owner 隔离和 Feishu 出站证据；请求入口是可信 localhost scope，macOS 锁屏使本轮
  没有新增桌面客户端入站证据，文档不把两者混写。
## 2026-08-21 前台让出后的 `/stop` 与活动计数

- 根任务等待 child 时可能已经没有前台请求、进程或 claim，但普通根 task link 仍是 active。`/stop` 现在从线程绑定的普通根任务中选目标、过滤 child 和具名 audit/goal 根，再递归中止其运行树。
- `active_task_ids` 继续只承担可恢复索引；TUI notice 的 Working 数量改为只统计 task link 中 `status=active` 的真实任务。`interrupted` 可显式恢复，但不会继续显示为正在工作。
- Gateway 给模型的运行信息不再把内部 `task_path` 称为 workspace；用户项目 cwd 与内部任务状态目录分离，普通相对路径始终从项目 cwd 解析。
## 2026-08-21 主会话任务晋升后的 cwd 写权

- `.7` 原样 TUI 证明主代理 foreground 能创建 `/root/abc`，但持久任务 background 轮只剩隐藏
  `work/output` 写根；模型看到 `/root` cwd 却连续得到 `WRITE_FORBIDDEN`，最终绕到额外测试根并误报交付。
- `write_boundary_with_runtime_ledger` 现在为本地/admin 主会话同时固定 `execution_cwd` 和项目写根，任务
  `work/output` 只是附加运行区，不替代 cwd。远程 owner、task-local child 和 transient Audit 后续继续按
  结构化身份收窄。
- 聚焦回归覆盖 Gateway 任务晋升前后同一路径可写，以及既有远程/child/Audit 边界不放大；真机复验待部署。
## 2026-08-22 裸启动与崩溃恢复权威收口

- 裸命令 `my-agent` 已进入 chat/resume 轻量解析路径，默认只新建当前会话；全局任务扫描和无实际派工
  行为的 `[Y/n]` 提示已删除，旧会话只允许 `resume <session_id>` 精确恢复。
- 裸 TUI 与服务安装统一读取 `MY_AGENT_CONFIG` 作为默认配置路径，显式 `--config` 仍优先；测试部署不再
  出现 Gateway 使用 MiniMax、欢迎页却显示随包 DeepSeek 默认值的双配置现象。
- `status` 改为无开关、无副作用的显式诊断；普通 stale attempt 由 Gateway 启动调和，subagent runner
  继续由周期 supervision 负责。
- `subagent_orphan_supervision.lock` 已从 PID+JSON 文件存在性改成内核 advisory lock；空/坏元数据不会再
  永久阻断恢复，`force` 也不能绕过真实活锁。
- 本地 focused tests 已覆盖默认入口不构造第二 Agent、旧配置不隐藏 status、失联 runner 只读分类、
  空/坏锁接管、并发互斥及 Gateway stale-attempt owner。
- `.7` 已部署 `06b84e1`：单 Gateway PID `1918200`、单 8420 监听、MiniMax-M2.7；裸 TUI 在 1 秒采样点
  出现输入框并通过普通中文请求。v2 锁持有者与 Gateway 同 PID，首轮监督复活 4、按父会话取消 21、
  回收失联 runner 5，3 条 RUNNING 后续自然 DONE；连续两次 status 近期计数一致。

## 2026-08-24 owner 树内子代理详情与控制入口

- Gateway 新增 `/client/agent-view`、`/client/agent-guidance`、`/client/agent-stop` 三个可信本机入口，
  共用 `GatewayControlScope` 与 exact run id，不接受显示名、行号或自然语言状态作为目标。
- view 允许 owner/root/ancestry 已证明的历史 child 在 attempt 关闭后继续查看；guidance 和 stop 额外执行
  mutation authorization 与 active binding。guidance 以稳定 operation id 幂等追加，stop 复用 canonical
  cancel 服务，终态 child 的新输入返回结构化冲突且不会静默恢复。
- TUI 当前已接通这三个入口，未来 Web 继续复用同一 service。本地 focused 回归通过，`.7` 单 Gateway
  原样 Prompt 2 已在 `c5026a7` 的 tmux `ma-c5026a7-agent-nav-r12` 完成真实按键验收；同机仍只有一个
  Gateway，终态 child 的普通输入没有新建 attempt。

## 2026-08-22 单 Gateway 多项目 cwd 主链候选

- 真机第二条正式 TUI 在 `<历史工作目录:tui-p2-f5d28b8>` 启动时卡在 `Connecting to Gateway`，没有提交用户
  prompt。根因是 pid/heartbeat/queue 跟客户端 cwd 一起落到 workspace hash；已运行 Gateway 位于
  `/root` 的 hash，客户端却探测另一套空队列。
- 对照 会话运行时 `ThreadStartParams.cwd/runtime_workspace_roots` 与 turn cwd override 后，Gateway/adapter
  服务目录改为 owner 级固定路径；TUI/CLI 将 cwd/roots 放进 ask，Gateway 校验后写入 thread v6。
  工具门、资源锁、handler、任务交付和 child 相对输出统一读取该 typed cwd。
- 请求未携带 workspace 时保留 thread 原值；相对、不存在或远程 owner 伪造的 host cwd 在模型调用前以
  `GATEWAY_WORKSPACE_INVALID` 失败，不回退 daemon cwd。当前 focused 回归已通过，部署与正式提示词 2
  的真实 TUI 复验仍待完成。

## 2026-08-23 Gateway 模型用量分栏

- `/result/<request_id>` 在既有累计模型数字旁新增 `model_usage_breakdown`，原样区分 provider 真值与
  estimated 估算；input/output/cache read/cache creation 和 call count 不再需要由客户端从混合总数猜测。
- 同一冻结 summary 同时进入 owner/thread 的 append-only 用量账本。后台 main 即使不保存普通 transcript
  也要留下真实调用成本；无可信 thread identity 时明确跳过，不从 cwd、prompt 或模型正文猜归属。
- 该字段只做观测和对照测试，不改变请求状态、Compact 阈值、任务完成、授权或计费执行。

## 2026-08-26 完整助手分段、缓存稳定历史与网络事实候选

- Gateway rich sink 在每个真实工具边界把完整 commentary 登记到 result；持久化按
  `gateway_request_id + assistant_part_id` 去重，`commentary:N` 先落、`final` 后落。repair 文件也按 part
  分开，旧无 part 记录只兼容为 final。长过程/报告不再受 thinking 的 12K 展示上限。
- `_gateway_conversation_history` 删除单条消息中部裁剪，只在 legacy 总预算不足时整条淘汰最老消息；正常
  canonical supplied rows 仍按真实 Compact 预算进入。会话配对预览过滤 commentary，只选 final，避免列表
  中一条 user 被过程回复误配，同时 transcript/TUI 保留全部正文。
- 任务 link 已存在时，后台 wake 从 exact `task_path` 重建 cwd/roots，并阻止 thread 的旧 client cwd 覆盖；
  无任务轮才继承 client cwd。
- `process_session(network_status)` 新增 exact session listener/firewalld 只读投影，任何 non-loopback 结果仍
  要求外部探针，不改端口规则、不把本机成功写成局域网成功。当前待严格 gate、部署和真 TUI/独立 Mac 探测。

## 2026-08-28 Gateway 权威诊断与 Audit 完整收口

- 已新增本机 `main_agent` 专属只读 `gateway_status`，直接返回唯一 Gateway 的 validated process identity、
  127.0.0.1:8420、MiniMax-M2.7、config、heartbeat、hot queue 和本生命周期日志计数；普通 owner 不注册。
- 表达专用回复轮的未授权 tool_use 已按 会话运行时 风格回注为未执行失败 ToolResult，再由模型完整收口；达到上限
  也不会把工具前过渡句当终稿。
- `.7` `ma-cleanup-audit-r12/r13` 已真实复验：不再猜端口、不再输出半句，精确身份与日志计数均通过；单
  Gateway 拓扑保持不变。

## 2026-08-30 首次工具晋升投影与后台 Skill 续接

- 首个 `promotes_task` 工具成功建立 task root 后，工具循环自己的 task-attributes 投影会在权限/cwd 冻结前
  从外层活动回合权威状态刷新；这解决多工具并发轮里首个 shell 仍在 owner home 执行的问题。
- 后台 child-lifecycle 续轮默认保留 `skill_search`，使同一任务前台已经选择的 Skill 能在后续整合轮继续读取；
  显式 owner/task policy 仍可移除该工具。
- 本地 focused 已通过，真实 MiniMax-M2.7 TUI 的 fresh 目录和后台唤醒复验待 R114p 部署后完成。

## 2026-09-01 active turn 重排与 RuntimeDB unknown 冲突修复

- R129 在 `.10` 唯一 Gateway 的真实 TUI 中证明：processing 请求、conversation runtime、任务目录与 15 条
  工具归档都能跨进程重排，但启动调和会先把死亡进程持有的主 run/attempt 标为 unknown；第二次 worker
  随后被 generic authority hard gate 拒绝，不能只凭 `requeued_requests=1` 宣称恢复成功。
- Gateway 现在先按 `gateway_active_turn_recovery.v1` 恢复 exact request 的 carried tool records，再调用
  RuntimeDB 的结构化窄口。RuntimeDB 另行要求 exact task id + run/request id、current unknown attempt、全部
  已启动操作为 SUCCEEDED/FAILED 且有同 operation id/tool/status 的耐久记录、CANCELLED 从未进入 handler，
  并且该 attempt 没有 MUTATING/DIRTY resource。
- 全部成立时旧 attempt 记为 recovered、unknown run 回到 created、执行锁释放；普通 agent authority 随后
  创建新 generation 并把 carried records 交还模型。CLAIMED 且 handler_started_at=0 的占位在同一事务取消。
  缺身份但也没有 run/工具记录表示崩溃发生在 runtime 建链前，可按原请求首次进入；已有工具记录却查无
  exact run、记录缺失/冲突、执行中/unknown 工具或脏资源则继续 fail closed。
- 通用 `recover_attempt_unknown` 的人工出口没有改成自动；后台 wake、child 与普通 CLI unknown 也不会借用
  该窄口。
- R130 已在 `.10` 唯一 MiniMax-M2.7 Gateway 的 `ma-r130-110-main-active-restart` 完成第一安全点。请求
  `gwreq-1788301131-52a459b2470f43b99c5e73c7e26b79c0` 的 generation 1 在 21 个工具操作全部终态后被
  SIGTERM 打断；新 Gateway 写入 `attempt_recovered/recovery_mode=recorded_active_turn`，同一主 run 创建
  generation 2，只新增 5 个后续工具操作并自然 done。18 个 unittest、三组样例和两种报告都在原 owner task
  root，TUI 直接显示 final，零新任务和零已结算写入重放。
- R131 在 main 已以 `SUBAGENTS_ACTIVE` 等待唯一直属 child 时顺序重启。原 child generation 1 / runner PID
  167884 保持不变，完成后新 Gateway 自动启动原 main generation 2 并 final；canonical tree 始终只有 2 个 run。
- R133 形成精确 root→1 coordinator→2 workers。coordinator generation 1 以 `SUBAGENTS_ACTIVE` 让出、两名
  worker generation 1 正在运行时顺序重启；worker 无重复，原 coordinator generation 2 集成后原 main
  generation 2 汇报，91 个 unittest 全通过。R132 因模型多派一层 coordinator 且错过等待窗口只保留负样本。
  至此主代理活跃、main 等直属 child、coordinator 等孙代理三个真 TUI 重启安全点全部通过。
# R211 /model 配置主链（真实 TUI 待验）

新增 owner 认证后的 `/client/models`，列表不返回密钥，保存与选择走同一私有原子配置文件。
主工作片（前台/后台/手动 Compact）冻结模型依赖，不热改另一个会话的 Agent；子代理按创建时引用恢复。
当前处于定向验证与打包阶段，BUG-146/147 继续保留，不能将菜单功能当成原同步问题已修复。
