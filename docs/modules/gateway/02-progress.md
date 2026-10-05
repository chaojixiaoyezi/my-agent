# Gateway 维护状态

## 插件事件 thread_ref 统一（tref2，2026-10-05，worker/tref2；待非作者初审）

- `gateway_parts/event_points.py`：`prompt_queued` 的 `thread_ref` 留空（入队时点线程可能未解析，A2），渠道会话号进 `channel_conversation_ref`；`turn_started` 增加 `thread_id` 参数（由调用方在回合解析后传入）；`command_completed` 用回执的 `conversation_thread_id`；`gateway_event_context` 透传渠道会话号。
- `gateway_parts/request_execution.py`：新增 `_gateway_turn_thread_id`（回合开始前只读 preflight 解析会话线程，与执行路径同一入口；取消/跳过路径不经过；失败留空），`turn_started` 用解析出的线程。
- `gateway_parts/control_operation_service.py`：`GatewayControlOperationReceipt` 新增 `conversation_thread_id`（执行时按冻结渠道身份只读解析一次，多 owner 先物化 owner agent，失败留空；重放/对账不回填旧行）。
- 详见 DESIGN_LEDGER / TESTS 同名节与 `docs/design/PLUGIN_EVENT_HOOKS.md` 第 6 节（事件目录 v2）。

## G2b 客户端收尾小修（g2bfix4，2026-10-05，基于 g2bfix3 头 `51b3efb20`；待终审）

- **背景**：g2bfix3r 初审给 g2bfix3 判"小问题，可以交终审"：① 解码层 `auth_denied` 置位无直接断言；② 投递线程兜底会每秒一条 warning 刷屏，且 `except Exception` 会吞 `InterruptedError`/`BlockingIOError`；③ `/progress` 对不存在记录回无码 403，与 `/result`、`/input-status` 的"不存在回 404"不一致。3a 采纳服务端做法 A，本批由初审者直接修。
- **② 投递兜底加固**：`_run` 增加 `except (InterruptedError, BlockingIOError): raise` 显式放行中断类（两者都是 OSError 子类，项目里 `InterruptedError` 是真实中断信号）；新增 `_log_loop_error` 按 60 秒窗口对同一异常限频（窗口内只累计次数、下次真正记录时先输出被抑制条数）；KeyboardInterrupt/SystemExit 等 BaseException 穿过兜底并补护栏用例。
- **① 解码层置位断言**：新增直接调用 `_active_turn_input_result` 的五格用例（401/403/带码 404 置位；无码 404、普通 202 不置位），两条既有端到端用例补 `auth_denied` 断言。
- **③ /progress 三态**：`_can_read_finished_request` 由 bool 改三态枚举 `_FinishedRequestAccess`（ALLOWED / NOT_FOUND / DENIED）；`handle_progress` 未找到回 404、无权限保持 403，两者都经 `_denial_body` 补缺凭据码；`_all_user_access`（管理员）分支不变。
- **验证**：见 TESTS「G2b 客户端收尾小修（g2bfix4）」；6 个变异全杀；相关文件与 guards9 全过，静态门禁全过。
- **未验证**：真实 TUI/飞书端到端与真实 Gateway 仍由 3a 在沙箱外复核；本树覆盖解码层、投递循环与 `/progress` HTTP 层。

## jb6联合用例（2026-10-04，WIP，待worker/b5fix9b）

承接sol2 e388b85e7两个联合文件及旧边界。观察空根因为夹具同activation让共用池取错连接，已改独立激活并核event-watch目标三事件。3a将漏账/interactive/无法审批修复交ds6 worker/b5fix9b，本线已撤全部产品改动，两ask的空账本专用strict xfail不吞观察/审批错误。当前29文件589 passed/1缺Node样例skip/2 xfailed；两直接deny唯一行/info通过，完整ask链待修。手工processing不代自动worker/生产入口，详见TESTS。

## 断网时模型调用卡住不超时（hang，2026-10-04，调查阶段结论；产品代码未改）

- 3a 补充的 `pmset` 记录确认测试时段包含 Clamshell Sleep，8 个结构化失败结果均早于完全唤醒；睡眠期间的墙钟跨度不能当作连续运行的超时证据。SSE / 首事件、重试预算与压缩调用各自时钟及 DarkWake 对齐见 `TESTS.md` hang 节。
- 睡眠恢复仍是**未落地后续项，状态：待设计**；风险包括校时/长调度停顿误判、重复计费、重放已提交工具副作用、接受迟到结果，见 `DESIGN_LEDGER.md` 顶部。
- CAB 本机慢滴流复现确认 Compact auxiliary 预算缺口：首事件未传动态预算，慢 body 的非流式调用可超过 `request_timeout`，而主模型被总时长守卫截断。修法和 streaming 总时长取舍待 3a 决定；本轮只补测试/文档，没有改产品代码。

## G2b 拒绝路径的客户端收口（g2bfix2 + g2bfix2b，2026-10-04；9b 终审通过，已并入 step17j）

- 强制档下 401/403 是确定性鉴权拒绝，客户端不再按暂时性错误处理：飞书轮询把 auth 类隔离转成一条用户可见的失败回复再写 quarantined 终态（其它隔离保持静默），`/ask` 提交把 401/403 收口成 G3 同款 credential_error 而不是无上限退避；TUI 插话按 REJECTED 处理并提示重启，持久 outbox 条目收终态、重启后不重发。
- 判据只用状态码与结构化 `error_code`（g2bfix1 约定 `LOCAL_CREDENTIAL_REQUIRED`）；5xx/429 等暂时性错误照旧退避重试。

## G2b 读路径补最后一处：归档读不出的 403 也带缺凭据码（g2bfix1c，2026-10-04，worker/g2b-denial-server-fix2）

- g2bfix1br 初审全面 grep 时查出：`_send_archived_terminal_result` 的 `load_error` 分支（归档文件存在但读不出）仍是裸 `_send_json(status, body)`，与已修的三处同在降匿名读路径上。
- 本次一处改动：`handler._send_json(status, body)` → `handler._send_json(status, _denial_body(handler, body))`。状态码、`error`、`request_id` 与管理员侧 500 + `result_load_error` 全不变——该格普通用户恒走 403，加码分支不可能命中管理员。
- 用例 21 → 23；变异 1 个（退回裸 `_send_json`）被抓。详见 TESTS.md 同名节。

## G2b 读路径补齐：/result 自判 403 也带缺凭据码（g2bfix1b，2026-10-04，worker/g2b-denial-server-fix）

- g2bfix1r 初审发现：`/result` 自己判的两处 403（队列记录损坏、队列记录属于别人）和归档终态那处 403 仍是手写裸 403，没走 `_denial_body`——同一条降匿名读路径上 `/progress` 带码、`/result` 不带，客户端仍分不清"没带凭据"和"真越权"。本次把这三处改成 `_denial_body(handler, {...})`。
- 状态码、`error`、`request_id` 全部不变，只在"强制档 + 回环 + 没带凭据"这一格追加 `error_code`。`/control-status` 的 403 保持裸返回（它前置已挂 `require_trusted_source`，没带凭据到不了那一支）。
- 用例 16 → 21；三个变异全红（两处 pending 403 退回裸、归档 403 退回裸、`_denial_body` 去掉判据）。详见 TESTS.md 同名节。

## G2b 本机凭据缺失的结构化拒绝码（g2bfix1，2026-10-04，worker/g2b-denial-server，待初审）

- 强制打开 `gateway_require_local_credential` 后，"回环 + 没带凭据"这一格的拒绝体加 `error_code: LOCAL_CREDENTIAL_REQUIRED`（状态码与既有 error/message 不变）。理由是 G2b 上线时客户端要能区分"本机客户端没换代码/没带凭据"（重启即可）和"远程来源真的越权"——只看 403 分不出来。
- 两类入口都覆盖：挂 `require_trusted_source` 的端点由中间件补码；`/progress`、`/input-status`、`/result` 三个不挂该闸、改为降匿名后由端点自判 403/404 的读端点，经共享 `_denial_body` 补码。判定只读结构化事实（强制档位、对端 IP、凭据比对结果），不解析路径或文案。
- 回环带错或过期凭据同样带这个码；远程不可信来源、来源未知不带；开关关着（迁移档）行为完全不变。

## B4 直接隔离与一次性诊断（b4gs，2026-10-04，待非作者复审）

- 直接上下文构造故障回归杀掉 ds2 存活的 M2；启用后的装配异常统一固定原因 warning，进程级锁+集合去重，不带配置值、正文或 traceback。关闭/未知开关静默，合法归属过滤不告警，坏日志 sink 不改变业务。
- 六份 plugin_event、managed_gate、十一份 guards9 合计十八文件 349 passed，六变异全部抓到且磁盘字节未变；工具局部导入故障的诊断也补了红→绿。生产与 B7 未验，完整命令/门禁见 TESTS。

## B5第5段与六条初审小项（b5s5，2026-10-04，已实施、整体WIP待复审）

- 接b5r `65f47f2c6`，三方叠源 `ff5d761d9..d517a8b7b`；原唯一审批、共用池及handler观察顺序不变，未新增Gateway服务或路由。
- 主claim换轮/子DONE后等待零轮询收口，resolve独立重查claim/attempt；I4门引用每个字段和参数哈希保持同源，新调用身份不继承旧批准。六项和变异回执见TESTS。
- rm-guard决定经真实执行器/归档/临时权威库由原info展示读取，组合三项转正；最终27文件563通过，完整11文件guards187。无交互混合门保真实deny、不误计无法审批。真实启用、TUI/IM/Gateway未验证，b4g保持17j保留点，由3a先叠再收本线。

## B5第1–4段迁到17j（b5r，2026-10-04，迁移已实施、整体WIP）

- 基于 `ebe621d87` 三方应用 `b6ede99e0..ff5d761d9`；B3/B4唯一事件hub与共用池原装配保持，只追加B5读取现有server池的入口，不造第二池。
- 原审批桥/轮/消费者继续守插件强制确认，17j动态审批读取与停机前后复核保留。B5宿主decide后只收紧，拒绝不发B4工具事件；sol2两处事件上下文配置读取保留17j，由3a后叠。
- 26相关+10guards回归678通过、13失败、5准备错误、1严格xfail；18项在真正17j基线同样失败。第5账本写入由ds2迁移、3a后叠；真实Gateway/TUI/IM和宿主启用未验证，详见TESTS。

## B4 全入口观察装配隔离（b4g，2026-10-04；ds2 初审可以挑入，已并入 step17j）

- 提示入队、回合开始/收口与命令观察统一安全检查开关，缺配置/getter/字段/装配异常不发事件、不反噬业务；关闭不读取请求、回执、响应或 owner。worker owner 同源门不变。
- 原 experiment 两个 HTTP 入队老例、正常真实 HTTP 与 Gateway 事件回归均纳入 33 文件 803 passed/2 skipped；跳过与未验边界见 TESTS，未动生产服务。

## 宿主私有 JSON 写保持 0600（sclk3，2026-10-04）

- `gateway_parts/daemon_metadata._write_json_file`（PID 记录 / 运行时状态 / 停止请求 / scoped lock 心跳刷新）临时文件按 0600 排他新建再替换，上级目录改 `ensure_private_dir`；修 9b 复核 sclk2 时发现的“刷新后锁文件变回 0644”。

## 私有写整包 9b 终审修复（pbfix，2026-10-04）

- `gateway_parts/io.write_json_file`、`scheduler/repository` 的建目录点改 `nofollow_fs.ensure_private_dir`（缺失段逐级 0700、已存在一律不动）。
- 锁只剩 5 处调用方，"找最近的已存在祖先"循环收进 `nofollow_fs.split_existing_anchor`；`open_private_lock_beneath_tightened` 加 `hasattr(os, "fchmod")` 守卫（Windows 无此函数不再抛 AttributeError）。
- 详见 `02-progress.md` 同名节与 `TESTS.md`。
## Gateway scoped lock 收私（sclk，2026-10-04，worker/scoped-locks-private，待复审）

- `gateway_parts/scoped_locks.py` 建锁文件与锁目录改走 `common/nofollow_fs.open_private_lock_beneath`：新锁文件 0600、新锁目录 0700（不再跟 umask 走），已存在的目录一律不改权限；`O_CREAT|O_EXCL` 的"已存在即失败"互斥语义由该函数新增的 `exclusive=True` 参数保留。
- 同批核对：`agent/task_progress.py:79` 是普通状态原子写（非锁创建点）；`concurrency/optimistic_lock.py:28` 是子代理工作区任务目录（非私有状态目录，未改）；`user_space/run_workspace.py:137` 是白名单集合（非创建点）。详见 DESIGN_LEDGER 同名节与 TESTS。
## Gateway 写入点私有写第三批（pw3，2026-10-04；pw3r 已搬到 17j 头 `35b1647e8`，待非作者初审，之后交 9b）

- 旧 /ask（无幂等 ID）inbox 写入 → `write_private_json_file_atomic_no_newline(sort_keys=False)`；流事件追加 → `append_private_text`、chunk 目录 0700；adapter outbox 三处 → 已私有的 `write_json_file_atomic`、adapter 五目录 0700；adapter_late 迟到登记追加/重写 → 私有 append/原子；message_repairs → 已私有的 `write_json_file_atomic`；scoped lock 由 17j sclk 覆盖（本批未重复改）。
- 格式与读取路径不变；建目录口径（显式准备目录已改用 pbfix 的 `nofollow_fs.ensure_private_dir`）与保留项见 DESIGN_LEDGER 同名节。

## G1 加固（2026-10-03，g1h，worker/g1-hardening，待 9b 核对）

- 真实 `home_paths` 合同作为凭据根来源；路径只经 `agent_home_root_for_owner` 从 owner home 推导并与 `home_paths.root` 核对。缺失属性是 `agent_contract`，没有 Agent 则 `LOCAL_CLIENT_CREDENTIAL_NO_DATA_ROOT`，不会从队列目录写入。
- G1 写锁、目录校验、读与原子写复用 no-follow 文件系统原语；目录符号链接在生成、锁文件写入或 chmod 前拒绝，既有权限错误不自动修复。验证命令与未覆盖边界见 TESTS.md 同名节。

## owner 维护挂历史悬挂 run 一次性补收口迁移（rcb，2026-10-04，分支 `worker/run-closeout-backfill`，已实现，已并入 step17j `39350230a`）

- Gateway 的 owner-maintenance 循环（`cli/gateway_loops._start_owner_maintenance_loop` → `user_space/owner_maintenance.run_owner_retention_if_due`）在跑 retention 的同时多做一步：对每个 owner home 的 runtime.db 执行一次历史悬挂 run 补收口迁移（`runtime_db/run_closeout_backfill.apply_run_closeout_backfill`）——把 rco 修好主链路前停在 created 的 run/task_run 按结构化分族补到终态。
- 迁移只执行一次（记录写 runtime.db metadata 的 `run_closeout_backfill.v1`，重复调用空操作；处理幂等，崩溃后重跑只补剩余）；判定与分族只读结构化字段（attempt 静止、无未结算操作、无活跃锁、结束超过 6 小时宽限；不可续跑族→failed、取消族→cancelled、可续跑/等用户族不动；recovered→cancelled、runtime_reason=attempt_recovered）；每条写 `run_closeout.backfilled` 审计事件，task_run 用树终态 CAS 补关。
- 回执写进 `O/data/maintenance.json` 的 `run_closeout_backfill` 键；只读预览 `preview_run_closeout_backfill(db_path)` 供生产副本核对数字。测试与门禁见 TESTS.md 顶部。

## 宿主侧抓取工具屏蔽本机 Gateway（G6，2026-10-03，luna1g6，`worker/luna1-g6-fetch-port`，ae 复审修正中，待复审/9b 终审）

- `tooling.web` 每跳读取 G4 的 `gateway_bound_ports()`，并合并正数 `gateway_port` 配置后备；端口 0 或无端口事实时 G6 不适用，端口无法读取/不合法时只拒确认的本机目标。
- 目标端口命中后用无缓存 UDP bind 分类解析 IP；成功表示本机，`EADDRNOTAVAIL` 表示非本机，其他错误为无法确认并拒绝。`web_fetch` 在每跳从调用副本当前属性重建私网授权，DNS pin 与重定向复查不变；不附本机凭据。
- G6 是宿主抓取层保护，不与 G4/G5 OS sandbox 的 `gateway_isolation` 状态关联；在 G4/G5 unavailable 时它不阻止模型从 `run_command` 连接 Gateway。详见 `TESTS.md` 的本轮结果与限制。

## 响应文件与终态归档的轮询去重改用文件代次指纹（mtc，2026-10-03，分支 `worker/mtime-cache-fix`，待 3a 复审）

- 起因：`response_renderer` 两个 when-ready 入口（`read_gateway_response_file_when_ready`、`read_gateway_terminal_response_file_when_ready`）
  的 `GatewayResponsePollState.stat_signature` 只看 `(mtime_ns, size)`，同时间片的原子替换会被当成“没变”而漏读一次；
  与子代理 `task.json` 事故同一类根因。
- 改动：签名改为 `common/cache_freshness.cache_stat_signature` 的五元指纹 `(dev, ino, size, mtime_ns, ctime_ns)`；
  即使指纹相同，mtime 距现在不足 `CACHE_TRUST_AGE_SECONDS`（2 秒）也一律重读（窗口内重复读取是幂等覆盖，
  CLI/chat 轮询循环拿到同样的终态只会再收口一次同样的结果，不会重复展示或重复收口）。
- 已知边界：窗口内对同一代文件会多读一次（代价是一次文件读取），换来确定性；窗口外行为与原来一致。
- 测试与变异见 `TESTS.md` 顶部同名节；真实 Gateway/TUI/飞书轮询未验证。

## 私有写只动自己建的东西（pdp，2026-10-03，分支 `worker/private-dirs-policy`，基于集成头 `3a42f457d`，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- `common/json_io._ensure_private_dir` 改口径（3a 裁定，与锁收私 ds8 同口径）：缺失目录按 0700 新建（no-follow 逐段），已存在的目录一律不改权限。
- `gateway_parts/io.write_json_file`、`scheduler/repository` 的建目录点改 `mkdir(..., mode=0o700)`；Gateway 队列目录（inbox/responses/history 等）此前由普通 mkdir 先建、靠私有写收紧，现在建出来即 0700；已存在的目录（含用户指定路径）不再被收紧。
- 已知边界：inbox 的 `.lock` 文件会随请求累积，本次不改，清理留给 owner 维护后续项。

## Gateway 队列与通用 JSON 写入口私有写入（pw2，2026-10-03，分支 `worker/private-writes-batch2`，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- `gateway_parts/io` 的三个通用写函数 `write_json_file_atomic` / `update_json_file_atomic` / `write_gateway_request` 改走私有原子写（目录 0700、文件 0600、存量宽权限下次写入收紧），内容逐字节不变（新增不带尾换行的私有原语以保持原格式）。
- 42 个调用方已逐个核对，写的是 Gateway 根下的宿主运行数据：请求队列（inbox/processing/done/failed/responses）、会话与任务存储、控制面/租约/实验记录状态、adapter 状态、停启请求与交接状态。读写方（TUI、适配器、派活工具、后台服务）都是同一个系统用户，收紧到 0600 不影响它们。
- 调度器历史账本 `scheduler/repository` 与任务 `timeline.jsonl` 同批改私有追加。用例与变异见 [TESTS](../../../TESTS.md)。

## 写锁 sidecar（.wlock）同批收私 + 初审三条小问题（lkf，2026-10-03，分支 `worker/ds3-lock-private`，基于 `199879045`，已实现，9b 终审通过（含 pbfix 修复），已并入 step17j `2ad257314`）

- **背景**：ds1（lockr）对 ds3 的锁收私交付做初审，结论「必须改 1 + 小问题 4」。3a 采纳后范围收缩：
  `gateway_parts/io.py` 的 `_locked_file_path`/`_open_lock_handle` 由 **ds9 的 `worker/private-writes-followup` `17dc9781b`** 负责，
  本分支不再动 io.py 的锁，3a 合并时用 ds9 的版本；本轮只收本分支归属的另一处旧写法。
- **本轮的必须改**：`gateway_parts/daemon_metadata._flocked_sidecar` 的写锁 `.wlock` 原来用 `lock_path.open("a+")`，
  按 umask 落成 0644（初审实测；父目录若是 Gateway 运行目录 0700 尚可，父目录一宽即可被同机他用户抢 flock）。
  现改经 `_open_private_wlock_descriptor` → `common/nofollow_fs.open_private_lock_beneath_tightened` 拿 fd：
  文件 0600、缺失目录按 0700 新建、已存在的目录一律不动（lkp 2026-10-03 修正，见 DESIGN_LEDGER 顶部）、
  锁叶子是符号链接或非单链接普通文件时抛 `NoFollowPathError`（不再静默跟随）、
  已存在的 0644 wlock 下次加锁即自愈收紧；只 flock 不写内容。阻塞加锁语义与 Windows 降级告警保持不变。
- **非阻塞语义用例**：`try_gateway_turn_transition` 走 `try_locked_file_transition`（线程锁 `acquire(blocking=False)` +
  `_try_flock_exclusive`），与阻塞入口 `gateway_turn_transition` 是两套实现。补 3 条用例钉住：锁被占用时立即返回 False、
  空闲时取得 True、持有者释放后可再次取得（`tests/test_gateway_io.py::TestTryGatewayTurnTransition`）。
- **初审三条小问题**：①TESTS.md 提交 2 的变异计数改准（实际是有 1 个被抓住，原来写成「2 个」又把第 2 个标「不适用」）；
  ②`memory/daily` 措辞改成「首次写入前有短暂的 0755 窗口，写入后是 0700」（现状：`DailyMemoryStore.__init__` 的 mkdir 按 umask，
  第一次 append 时 `_ensure_private_dir` 才收紧）；③两条已知边界写进台账（见下）。
- **已知边界（初审风险 2、3，3a 要求写进台账）**：
  - 锁路径的「向上找最近已存在父目录」会**穿过更高层的符号链接**：`_open_private_lock_descriptor` 系列先把绝对路径拆成
    「最近已存在的父目录 + 其余段」再交给 no-follow 原语，所以直接父目录是符号链接会被拒绝，但更高层（祖父级）是符号链接时
    锁会被建到那个链接指向的真实目录里，而不是报错。lkp 2026-10-03 已补用例钉住这条路径的权限行为（经符号链接进入的已存在目录权限不变），改这里的人要知道。
  - `open_private_lock_beneath_tightened` 的 `fchmod` 失败被 `except OSError: pass` 吞掉：设计上是「不让收紧失败拦住拿锁」，
    但因此「自愈没生效」在运行时完全不可观测——存量锁有没有真的收敛、下一次启动是否仍是 0644，没有任何埋点或 `/status` 口径能回答。
    只看最终收敛效果时需要另加只读计数。
- 测试与变异结果见 TESTS.md「三套锁写法统一成私有」一节的补充段。

## G2b 服务端强制：开关打开时回环无凭据按匿名（2026-10-03，g2b，worker/g2b-enforce，已实现，开关默认关，9b 终审通过，已并入 step17j `32551798d`）

- 开关 `gateway_require_local_credential` 打开后，鉴权只认有效本机/配置凭据：回环不再自带信任、`peer_ip` 拿不到按不可信、不带/错/空凭据回环请求降匿名（不认身份头、绝不给管理员），要身份的接口照原规则拒绝；开关关行为与 G2a 完全一致。
- 启动 fail-closed：凭据不可用（损坏/权限/数据根不可读或不可生成）时拒绝启动并给结构化原因码（`GatewayLocalCredentialRequired`）；凭据缺失仍是 G1 的「缺则生成」正常路径；开关关保持降级启动。
- 插件令牌路由（`/plugin-host/query`）先验 `X-Plugin-Host-Token`，有效即放行、不要求客户端凭据；令牌无效才回落原来源检查。
- TUI 启动预检（3a 插话）：`make_gateway_chat_client` 构造后调 `preflight_gateway_credential`，开关开且凭据不可用时拒绝启动并给原因码；开关关保持降级。
- 判定按路由模板（不是原始路径）、令牌常数时间比较；降级 warning 去重是进程级、有意的（凭据修好又坏时同一原因不再重复提示）。
- 已知边界（be 四条）：0600 凭据多挡的是非 Full Access 模型命令与其它系统用户；`submit_gateway_ask` 文件队列入口端口门管不到、靠文件权限自己把关、本次不改；插件令牌路由不受影响；`peer_ip=None` 不可信。详见 GATEWAY_LOCAL_TRUST 第 4 节与 DESIGN_LEDGER。
- 测试：19 项新用例 + 相关回归 13 文件 + guards9 全绿 + 5 项变异全抓；命令与结果见 TESTS.md 同名节。真实 Gateway/TUI/IM 与生产迁移未验证。

## G3 返工：凭据读不到时按 G2b 开关降级，不再提前拒绝（2026-10-03，g3f，worker/sol1-g3-clients，返工完成，待复审）

- 起因：现在是 G2a 阶段，服务端只计数、不拦；客户端在凭据读不到时发 HTTP 前就拒绝，等于提前做了 G2b。新 TUI 连上还没生成凭据的旧 Gateway（部署窗口）、或凭据权限被人改过时，TUI/飞书适配器/插件命令会全断。
- 改法：`client_credentials.GatewayClientCredentials.headers()` 读凭据失败时按新开关 `gateway_require_local_credential`（默认 false）分流——开关关（G2a）返回空头照常发送（服务端计“无凭据”），并记一条只带 G1 原因码的结构化 warning，同一原因同一进程只记一次；开关开才维持原行为，在网络请求前抛 G1 原因码、零请求。非空 `gateway_auth_token` 优先、direct 不读凭据这两条不变。
- 客户端与 Gateway 读同一份 `AgentConfig`，口径一致；开关是 G2b 上线闸，强制阶段稳定后连同旧分支一起退场。
- 测试：三类原因 × 开关关降级/开关开拒绝 × TUI/CLI/IM 全绿，另含 warning 去重、IM durable 两态、插件三运输、Goal 编辑器；见 TESTS.md 同名节。真实 Gateway、真实渠道与旧客户端迁移未验证。

## G3 本机客户端附凭据（2026-10-03，sol1c，worker/sol1-g3-clients，已实现，待 9b 终审）

- TUI/CLI 的原 GET/POST、控制/控制对账、插件目录/面板/交互命令流，以及 IM 的 ask/四条对账，在现有身份头入口加唯一 X-Gateway-Token。凭据只经 G1 load_local_client_credential 取，不生成、不修文件权限。
- gateway_auth_token 非空时沿配置部署合同只发该值，不先读本机凭据。缺失/权限/损坏拒绝且带 G1 原因码与安全处置提示；插件/Goal 不混成未知执行，IM durable 未发送故障只回复一次，不重放 POST。
- 仓库五类 HTTP 验收脚本也接原身份头；公开 metrics 和 R1-02 代理健康不读/不带凭据。文件队列客户端、本地 direct、浏览器、仓库外工具不改。
- 临时数据根、随机端口假服务和四项逐次变异有验证；真实 TUI、IM 平台和生产迁移未验证。G2a/G2b 不在本分支，凭据还不强制；详见 TESTS 与 GATEWAY_LOCAL_TRUST。

## G1+G2a 本机凭据与旧客户端观察（2026-10-03，sol1g，已实现，待初审/9b 终审）

- 数据根 secrets/ 下凭据缺失才生成；私有原子写后严读，已有损坏/权限不对就报结构化错误，不轮换。load_local_client_credential(data_root) 是宿主客户端统一读取接口。
- HTTP启动先用 canonical home_paths.root 准备凭据，再绑定监听；不进环境、日志或状态。插件沙箱 H2 同时隐藏凭据文件与 secrets 目录，开关关时插件与宿主同信任域。
- G2a 已实现：两种 Gateway 凭据按原身份处理且不计数；无凭据回环仍按原规则放行，只在 credential/admin 档按实际路由模板累计。公开 `/status`、`/metrics` 与插件令牌 `/plugin-host/query` 不计。
- `/status.uncredentialed_loopback_by_endpoint` 是本次进程启动以来的模板 → `{count,last_at}`，不含请求编号或身份；无中间件档保持原单机行为。观察账不持久化，不能仅凭重启后的空账判客户端迁移完成。
- tmp_path 与随机端口组件验证、五项独立变异见 TESTS；真实客户端和平台隔离未验。G3由集成者另派，G2b强制/端口拒绝尚未实施，不能称安全缺口已闭合。

## M1 B4 真实 HTTP 与提示 owner 尾补（m1b4，2026-10-04，已实施，待复审）

- 提示入队观察复用 worker 的结构化 owner 解析；local/tui 其它用户不再按频道白名单投给基础 owner。开关先检查、未知不发，不创建用户 Agent。
- 真实随机回环 HTTP 服务、扫描 worker 认领与原终态链新增三例，主用户三事件顺序/精确字段，两个其它 owner 无主用户观察；finally 验证测试服务与新线程清理。修前两负例红、修后聚焦两文件 19 项绿，源码变异抓到，最终门禁见 TESTS。
- 仅隔离 echo/握手假插件；生产服务、B7 与 TUI/IM 未验证。luna6 完整初审尾补待复审，不改其审查结论为已通过。

## M1 B4 拒绝组合尾补（m1b4，2026-10-03，待非作者初审）

- 真 Gateway/Agent/工具链保留正常及类型错误，补策略拒绝和原交互文件桥用户拒绝；canonical 精确码/阶段/handler 与零工具事件断言通过，聚焦四文件 78 项通过，收尾见 TESTS。
- ds9 提前初审无必须改；装配关闭和观察输入不带参数有独立用例；B3 非提示剥正文仅一处实现改动。turn 每次真实执行一对，不跨重启去重，真实生产与授权正文链未验证。

## M1 B4 请求身份续段（m1b4，2026-10-03，WIP，待交叉初审）

- 主/子/决策装配已接生产 Registry；J16 自动执行和审批重入携带宿主 actor。子代理用真实创建/激活父链及生产 recovery 验证，不再只测执行器接缝。
- 真 Gateway/Agent/原生工具循环与控制操作向 B3 握手假插件送齐六事件；默认不带正文。拒绝组合的假模型错误码断言尚失败，保留用例待查，不能写全绿。
- 原 22 文件加 4 新相关文件与 guards9 输出 555 passed/1 failed；本段尺寸差集新增 0、消失 17。真实网络、真实插件启用及非作者正式初审仍未验证。

## M1 B4 Gateway 事件点续做（m1b4，2026-10-03，WIP，待复审）

- 指定变基至 `0ae0efe5e`；原定向清单当前 492 项全过，尺寸差集新增 0、消失 17。
- 提示首次成功排队、回合执行开始/收口、新控制 completed 回执已接 B3 发布口；原幂等/审批/租约顺序不变，事件点不读取安装表、不等待插件。
- `test_plugin_event_gateway.py` 与投影、控制操作、请求异常回归共 121 项通过；模型边界使用离线替身，不宣称真模型或真实 Gateway 验收。
- 工具三来源生产装配及假插件六事件组合仍待后续段；多用户提示入队尚无冻结 owner 时保守不发，状态仍为 WIP。

## 回合失败后收回在途模型调用（luna3a，2026-10-03，待初审/9b 终审）

- **已完成**：wall-clock 失败时 abandon 原账本后通知精确 worker 中断，并按既有 1 秒窗口 drain；SSE/Responses WebSocket 取消直接关闭本次 socket，WebSocket 请求发送另有首事件预算；`/status` 由既有模型账本投影在途调用数和最老年龄两个数字。
- **解决的问题**：回合已向用户失败返回，但底层模型线程仍等网络数据、账本卡在 started/first_token 的状态；现在各传输读取超时后收口调用记录，回合放弃时主动取消底层连接。
- **下一步**：由另一 my-agent 会话初审、9b 终审；系统 DNS `getaddrinfo` 的 OS 级可取消性请复审方保留为边界。
- **已跑测试**：11 个直接相关文件执行至 100%、退出码 0；guards9 清单（含 `test_packaging.py`）至 100%、退出码 0。具体命令见仓库根目录 `TESTS.md` 同名节。
- **未跑测试**：按请求不跑全仓 pytest；未启动 Gateway、连接真实模型或真实网络。
- **风险**：同步系统 DNS 解析没有本请求级取消句柄；若解析器本身永久阻塞，不能保证模型 worker 在 1 秒 drain 内退出。
## M1 B5 第4段（2026-10-04，接471b7b4fc，整体WIP）

- 原用户决定进入runtime_approved_actions，唯一执行器自行核对引用六身份与鲜活安装；auto/宿主不要求确认也能跳当前已批门。主/子六字段分别变更后重新发布确认，旧批准不进入新调用；等待换代的ask/deny不贴已应用批准。
- 用户拒绝保留APPROVAL_REJECTED/未执行且回执带清洗后的插件/原因。当前539项含完整十守卫通过，六有效变异；实际TUI/IM/Gateway未验，第5由ds2做，不可上线。

## M1 B5 第3段主/子与 I4（2026-10-04，本地隔离验证、整体WIP）

- 执行器原引用接canonical主claim/子attempt、notices和所属用户决定；两身份四自动批准模式仍挂确认，不写旧授权，无消费者明确插件无法审批。
- child旧确认跨恢复轮红测后，在原scope/审批行冻结execution_attempt_id；与main claim同样由展示、决定和等待复核，不改旧schema/路径、不另建批准源。
- I4真实请求处理/重排入口+宿主对象重建、死claim观察、停机故障注入验证再征询；联合489项含完整十守卫通过。实际Gateway重启/TUI/IM未验，第4未做，第5由ds2独立树并行；不可上线。

## M1 B5 第3段执行端（2026-10-03，部署窗口WIP）

- 原审批链附执行器JSON引用，同源前缀、仅本次/拒绝；StreamApprovalRequestOptions与等待入口一样固定选项，五点先守门。
- 隔离执行器→原轮审批→Gateway首次回归468通过含完整十守卫；真实TUI/IM/主子/I4未验，精确重跑/账本未做，不可上线。

## M1 B5 第 3 段消费端守门（2026-10-03，m1b5，部分实施、整体 WIP）

- StreamApproval 和 permission_bridge 等待自主提供者之前使用同一 plugin_gate_required；已有会话/长期授权不能跳过插件要求，也不把本次决定写入旧授权。
- ToolApprovalWaitOptions 收拢固定等待选项，原默认常量与用户取消/精确决定优先级保持；后台主/子路径使用同一合同判定。
- 执行端尚未附加 plugin_gate_ref，前缀、选项和无人审批回执仍待接线；本次仅消费端反证，不是 TUI/IM 或完整链验收。

## M1 B5 第 2 段：共用池提供方（2026-10-03，m1b5，组件已实现、整体 WIP）

- 组合根只注入读取当前 Gateway 唯一 B2 池的提供方；不启动/停止 Gateway，不建立第二池，权限视图及同进程子代理共用接线。
- 不经 Gateway 时提供方返回无通道，匹配插件按 ask；已停止的 server 保留 B2 的终态，不能偷偷建新池。
- 征询按 acquire→request 刷新使用时间，并校准 B2 revoked：只有安装快照证明停用/换代才作废，通道不可用但激活仍在必须确认。
- 组件三文件 105、guards9 十文件 172 项通过；真实插件启动/沙箱、TUI/IM 未验证。第三段双审批入口仍待实施，不把本段当生产可用。

## owner 维护新增 memory_archive 权限收紧（luna3，2026-10-03，分支 `worker/luna3-archive-private`，已实现，待 9b 复审）

- Gateway 的 owner-maintenance 循环（`cli/gateway_loops._GatewayOwnerMaintenanceController`，默认每 60 秒检查、按 owner 的 `retention.json` 区间到期，默认每天一次）在跑 retention 的同时多做一步：
  `user_space/owner_maintenance.run_owner_retention_if_due` 调 `memory_archive.storage.tighten_memory_archive_permissions`，
  把该 owner `memory_archive/` 下已有文件收紧到 0600、目录收紧到 0700（只收紧不放松、不跟随符号链接、失败按原因码计数）。
- 回执写进 `O/data/maintenance.json` 的 `memory_archive_permissions` 键（`tightened_count/files/directories`、`failed_count`、`failure_codes`、`symlink_skipped_count`）；
  归档目录不存在时全零；导入或整体失败只写 `error` 字符串，不影响 retention 主流程与维护状态写入。
- 详见 DESIGN_LEDGER「记忆归档目录收紧权限」与 docs/modules/memory。

## 模型回合重试总时长上限（修法 B）返工：到上限保留原异常类型（ds1b，2026-10-03，分支 `worker/ds1-turn-retry-cap`，基于 `claude/3a-step17h` `880aee17b`，9b 终审通过，并入 step17i）

- 到上限不再新建 `ProviderTransientError`：给触发判断的原异常就地补 `error_code=PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED`
  与 `retry_budget_seconds` 后原样重新抛出。后台 claim 结算、子代理失败类型、Goal 的 `usage_limited` 判定与"跑满阶梯"完全同口径
  （返工前到上限会被当成 transient 释放 claim、唤醒后重跑，子代理失败类型也从 PROVIDER_TIMEOUT 变成 TRANSIENT_ERROR）。
- 收口时经 `_emit_retry_notice`（final）发一条"已到自动重试的总时长上限、本轮不再重试"的进度提示；用户可见文案在
  `gateway_parts/request_errors.gateway_client_error_message` 与 `agent/runtime_errors._provider_supply_template` 按结构化码登记，
  不再说"系统会自动退避重试"；`contracts/error_taxonomy` 登记同一码。
- 配置解析与注释对齐：坏值、NaN、负数都回落默认 1800 秒，只有 0 表示不限。
- 已知边界（3a 裁定）：子代理重派上限（`provider_transient_redispatch_limit`，默认 8）不因本上限收缩，最坏耗时≈重派上限×总时长上限，另立一项处理；
  次数耗尽时 `runtime_errors` 的"会自动退避重试"文案同样不准，属原有行为不在本次范围。
- 测试与变异结果见 TESTS.md"修法 B 返工"。

## M1 B1：订阅静态合同与确认码（2026-10-03，本地已实施、待集成）

- Gateway 原插件管理入口仍消费 `PluginManifest` 和原启用确认链；v8 四项订阅/权限事实加入确认码，不新增控制命令或启动 Gateway。
- 除声明、构建和权限预览，B1 按 ae 意见补过渡关闭门：v8 安装允许，启用在确认前返回 `plugin_events_disabled`、`not_started/not_committed`；不创建环境计划。B7 须替换该拒绝与计划排除，接入真正开关、local/main、强制沙箱。事件投递/握手归 B2/B3，事件点归 B4，不能把 B1 当完整事件能力。
- 3a 2026-10-03 两条裁定已追加实施、交 ae 复审：观察正文仅提示文字；工具参数只经精确 full 收紧门；v8 非空订阅可独立贡献，旧版本规则不变。
- ae 意见修订的合同 125 项、常数目录 11 项及 --check 877 项一致已有本轮证据；新增关闭门变异被抓到。ae 沙箱外基线 `274cedb1e` 与 B1 头 `bf520e911` 的旧启用文件均 35 passed，六失败来自 my-agent 命令沙箱环境（3a 转述，本线未亲自外部复验）。本轮收尾及真实 TUI/IM 未验边界见 TESTS。
- **M 线第一期真实验收手册（m1ar）已记录**：[详细手册](../../design/PLUGIN_EVENT_HOOKS_ACCEPTANCE.md) 给出 17j 前置、隔离 home/端口、三样例联合冒烟、TUI/飞书矩阵、账本字段与收尾；本文编写任务没有启动 Gateway 或做真实验收。B8 删除门仍须 ds2 将旧 `delete_file` 改成 `apply_patch` 删除段后才能开跑。

## B2 四轮返工：停机终态、回收的过期证据、在途摘除按撤销（9b 并发复审，ds4b2，2026-10-03，基于 `5aec88524`）

- 停机是终态：`plugin_panels_http.close_plugin_channel(server)` 在 `_SERVICE_LOCK` 内先置
  `server.plugin_channel_closed` 标记、再摘掉 `plugin_display`/`plugin_channel_pool` 引用，锁外才真正 close；
  之后的 `plugin_display_service`/`plugin_channel_pool` 一律抛 `PluginChannelRevoked`，不会再建出没人关的池。
  `GatewayHTTPServer.stop()` 改为调用它（幂等）。面板入口把撤销转成"通道已关闭"的空面板，不是 500。
- 回收要有正面过期证据：池加单调递增创建序号（`current_seq()`），`retire_stale(owner, valid, scope)` 的
  `RetireScope(managed, created_before)` 只在"归自己管、建在时间界之前、且不在 valid 里"时摘；
  面板服务在读有效集合之前取时间界。这样别的调用方为刚启用激活新建的连接不会被误摘。
- 在途请求撞上停用/关闭：`request()` 的通用 except 先 `holds()`，连接已不在表里就抛 `PluginChannelRevoked`
  且不标退避，避免 B5 把"插件已停用"当成"连接坏了"多弹审批。
- 合同注释补：客户端 `stop` 必须幂等（停用撞后台启动会 stop 两次）。

## B2 二轮返工：共用池召回范围与退避分级（ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，基于 `fcb638182`，be、ae、9b 复审通过，并入 step17i）

- 面板服务交给共用池的"有效激活集合"改成**这个 owner 全部已启用的激活**（原先只有带面板的插件），
  所以同一 owner 下已启用但没有面板的插件（只带事件的 v8 插件）连接不会再被面板查询摘掉、进程不会被停。
- 安装表读不到时（`_enabled_installations` 返回 `None`）**一条连接都不回收**：不再当成"没有启用插件"去
  `retire_stale`/`close_idle`，避免读表失败把整个 owner 在共用池里的连接全停掉。
- 退避改成分级：只有连接级故障（启动失败、超时、连接断开）才退避整条连接；请求级错误
  （插件对单个请求回的 JSON-RPC 错误、`before_send` 校验拒绝）只算这一次请求失败，由面板服务按面板退避。
  为此远端错误回复改用独立码 `MCP_REMOTE_ERROR`，连接启动失败统一包成 `PluginChannelStartFailed`。
- `before_send` 拿到的客户端改在池锁内取并复核，连接被并发摘除时按撤销处理，不再把 `None` 交出去。
- `_new_pool` 转发函数删掉，服务创建处直接调 `_locked_shared_pool`。

## B2 复审返工：共用通道的关闭竞态、注入式共用池、退避改为按连接（ds4b2，2026-10-03，分支 `worker/ds1-b2-channel`，基于 `af3dd299b`，be、ae、9b 复审通过，并入 step17i）

- `plugin_panels_http` 新增 `plugin_channel_pool(server)`：一个 Gateway 进程只建一个 `PluginChannelPool`，建在唯一 HTTP server 上，
  面板服务和以后的事件中心都从这里取；`plugin_display_service` 改成把池注入 `PluginDisplayService`，不再自己 new 一个。
  两个函数用了同一把 `_SERVICE_LOCK`，所以创建服务的路径改成在锁内调用不重入锁的内部函数（否则自死锁）。
- `GatewayHTTPServer` 多了 `plugin_channel_pool` 字段，`stop()` 里在关掉展示服务之后关池；池的创建和关闭都归 Gateway，
  面板服务 close 只清自己的待发槽与结果缓存。
- 池的身份不变：共用池仍然按 (owner, 激活代次) 一条连接、单在途、3 秒请求超时、5 秒退避、120 秒空闲关闭；
  沙箱开关在建池时固定成 `plugin_display_client(process_sandbox=…)`，同一进程不会出现两种插件进程形态。
- 行为变化一处（表述已在二轮返工修正为**分级**）：连接级故障按连接退避，请求级错误仍按面板退避。
- 细节与验证见 `docs/design/PLUGIN_DISPLAY.md` 的「退避分工」和 TESTS.md 顶部「B2 复审返工」小节。

## /plugins list 末尾加 MCP 服务段（J16 片 F，2026-10-02，ef，分支 `claude/ef-j16-slice-f`，基于 `claude/3a-step17h` `afb15947b`，待集成）

- `plugin_command_service._scope_management` 组装管理服务时多做一步：`resolve_loaded_gateway_scope_agent` 被动查找已加载的 owner 实例，
  把它的工具注册表放进 `PluginManagementContext.live_registry`；冷 owner 保持冷（不初始化、不记活跃），字段为 None。
- `/plugins list` 的回执末尾由 `plugin_commands.render_mcp_server_section` 追加一段：每个 MCP 服务的运行状态与发布状态
  （`tooling/mcp_registration.mcp_server_facts`：已发布 N 个工具 / 被拒绝（码：tool/code）/ 不可用（码）），publication 为 None 的插件客户端不列；
  实例未加载时写“当前实例未加载，没有运行事实”。TUI 直连入口（`cli/chat_parts/plugin_command_client._direct_manager`）交本进程注册表，IM 与 TUI 经 Gateway 走同一段。
- 测试见 TESTS.md“J16 片 F”。

## `/model vector` 显示嵌入用量与召回方式（S7，2026-10-02，分支 `claude/be-embedding-usage-facts`，已实现，待集成）

- 管理员查看 `/model vector` 时，末尾加“本次 Gateway 启动以来”几行：各用途的嵌入请求、条数、失败、供应商回报的 token，以及召回方式计数。TUI 与 IM 同一入口。普通用户看不到（计数是全进程的）。详见 DESIGN_LEDGER 同名条目。

## 能力包宿主核验结论随回合结束发宿主提示（2026-10-02，ae，能力包 v2 块 3，分支 `claude/ae-capability-packs-v2-b3-17f`，基于 `claude/3a-step17f` `f6b63ab35`，待集成）

- 回合正常返回后，`request_pack_verification_notice.queue_pack_verification_notice` 用 `AgentRunResult.pack_verifications`（核验账本的结构化事实）写一条宿主提示：`source=pack_verification`，`code=summary`。检查结果、被就地改的输入原件、缺的必需交付物、本回合核验被取消四类事实任一为真就发（能力包块 4/5 起，只有后两类的回合也发；块 6a 起，只剩“被取消”的回合也发），details 带三类计数。
- 提示排入原 pending_host_notices，和决策实验晋升提示同一批发布与提交；文字只拼结构化事实，模型看不到。
- `request_history.persist_gateway_assistant_result` 把同一份事实写进 `channel_delivery.pack_verifications`，不进 `_public_channel_delivery`/`_public_result` 白名单。没有核验事实时两处都不出现。
- 设计见 [CAPABILITY_PACKS_V2](../../design/CAPABILITY_PACKS_V2.md) 3.1 节，测试见 TESTS.md 同名节。

## GET /status 新增 usage_accounting 段：用量账两项进程内诊断计数（2026-10-02，ef，分支 `claude/ef-probe-usage-tails`，基于 `claude/3a-step16z` `c6f28b150`，待集成）

- `http_handlers.handle_status` 响应多一段 `usage_accounting`：`unaccounted_probe_attempt_count`（没绑记账范围的工具能力探测次数，
  来自 `backends/http.py`）和 `unknown_purpose_keys`（持久用量账里读到的未知用途键 `{keys, overflow_count}`，来自 `conversation/store_usage.py`）。
- 和 `loop_health` 一样直接读进程内存、不经磁盘；只读、不清零、不写盘。不进按 owner 范围的 `audit_records`，理由见设计台账同名节。

## 主机名变化后 SIGTERM 仍能停网关（2026-10-02，分支 `claude/38-host-id`，基于 `claude/3a-step16z` `a52ac109c`，待集成）

- 信号处理函数改用启动时记下的本代身份，和主循环比对同一份。
- `process_host_id` 在进程内缓存；macOS 用 libc gethostuuid 取硬件 UUID，取不到才退回主机名。
- 设计、跨进程表现和一次性过渡见设计台账同名节，测试见 TESTS.md 同名节。

## 插件管理的管理员判定改用统一函数（2026-10-02，分支 `claude/38-admin-fold`，基于 `claude/3a-step16z` `6980e5f41`，待集成）

- `plugin_command_service._scope_management` 的 IM 管理员判定改为 `owner_access.is_complete_local_admin_owner(home)`，与 /settings 同一条规则、同一种 home；
  user_config 工具的本机配置动作同样改用它。
- 唯一行为差别是纯空白身份字段，原来插件会认作管理员，现在不认（/settings 一直不认）。详见设计台账同名节。

## C5 剩余竞态：熔断判定与用户回合登记同闸（2026-10-02，分支 `claude/38-c5-fuse-race`，基于 `claude/3a-step16z` `4c624ecd4`，待集成）

- “在场”查询和熔断落账之间原来没有互斥。现在 `run_claim.user_input_turn_gate` 持车道闸，读在场和落账一次做完。
- 网关前台用户回合的登记也要过这把闸：要么在判定之前登记（空片不计入），要么等落账做完再登记（D4 只清计数）。
- 设计与锁顺序见设计台账同名节，测试与变异见 TESTS.md 同名节。

## IM 出口脱敏宿主路径时附统一说明（2026-10-02，分支 `claude/be-im-path-note`，基于 `claude/3a-step16z` `4c624ecd4`，待集成）

请求历史的最终回复正文与逐条说明消息改走 `project_message_paths_for_channel`：对外通道脱敏过宿主绝对路径时，末尾附一次“路径只显示最后一段，完整路径请在本机 TUI 查看。”。之后投递服务看到的正文里已没有路径，不会重复附。TUI 等本机通道不变。详见 DESIGN_LEDGER 同名条目。

## /settings 管理员判定改用统一函数（P14 第 6 条，2026-10-02，分支 `claude/38-p14-embedding-fixes`，基于 `claude/3a-step16z` `f8ae11fe5`，待集成）

- `settings_control_service._is_admin` 改为调用 `user_space/owner_access.is_complete_local_admin_owner`，规则不变：
  provider/kind/id 都是非空字符串，且是本机 local/main。
- 记忆向量重建入口 `my-agent memory vectors rebuild --confirmed` 共用这一条，不再各写一份。插件管理和 user_config 工具里还有两处同规则的内联写法，本轮没动。

## Anthropic 预算空区间回执（2026-10-02，sol2，本地已实施，待集成）

- 解决小输出 cap 下回执仍声称发送非法预算的缺口；`/effort` 与参数中心继续共用 `describe_config_reasoning_effect`，不增加路由。
- 上限与工厂同源，预算/原因与后端裁决同源；空区间说明未发送 thinking，不承诺服务商已关闭思考，不抬高 cap。
- 真实控制服务 chat/feishu 路由与所选模型后端、参数中心隔离组件有断言；十一文件 494 项与三个变异结果见 TESTS。
  实际终端、IM 收信及生产 Gateway 未验证，仍由 3a 集成后复核。

## C8/C9：/recover 编号与 owner 历史恢复（2026-10-02，sol56，本地已实现，待集成/真实入口核对）

- `/recover <处置> <编号>` 已接入共用 Gateway 控制服务：编号按 opaque ID 校验，写事务内复核 thread、未关 TaskRun、
  current unknown；不带编号的旧主链优先/唯一子代理行为保持。
- `/recover owner` 只信任与 `/settings` 共用的完整 local/main 管理员裁决；查看和预览只读本 owner 的空 thread 历史 unknown，
  确认码绑定完整目标集合，变化拒绝且零写入，重复确认只读原批次回执。
- TUI 仍发送原命令，飞书走同一 `control_service`。临时库聚焦测试与变异有本地证据；没有启动 Gateway 或验证真实渠道，
  也没有读取、预览或处置生产运行库，后续按 ROADMAP 在隔离 owner 验收。
- 审查返工已补：thread 解析为空或纯空白时，编号写入口都在事务前拒绝，避免绕过管理员门命中 owner 历史；同范围重复编号返回
  `target_ambiguous`；owner 批次部分成功如实返回已处理/跳过及原因码，同码重放先提示“该确认已处理过”；根编号回执与主链共用继续提示。

## P8/P17 验收后续（P18 缺陷修复，2026-10-02，ds2，分支 `worker/ds2-p17-p8-followups`，已上线 step17a，main de222698b，2026-10-02）

- `settings_control_service` 的 `/settings show` 修复三处：
  - capability 来源键的“用户配置里”计入 capability 文件（运行时路径）里的覆盖（`_override_text`）；
  - runtime_guard 来源键“能否修改”一行与 set 被拒同说法（“属于 runtime_guard 配置，运行时只读随包文件、
    没有用户覆盖层，改了也不会生效；只能查看和搜索。”）；
  - set/revert 回执“原来是默认值/现在是默认值”去掉多余空格。
- capability 配置文件唯一位置（`capability/runtime_config_reload`）：用户位置 `<owner home>/config/capability_config.yaml`，
  随包默认只读；旧候选非随包默认文件挂结构化告警。

## P13：/settings 展示嵌入档案（2026-10-02，ds1，已上线 step17a，main de222698b，2026-10-02）

- `settings_control_service` 新增 `embedding_model_profile` 的 show 支持：展示档案编号、模型名或失效原因，不含连接凭据；
  `_curator_profile_text` 泛化为 `_profile_text` 供两个档案共用。
- 该键在参数中心是 boundary / writable=False，仅可信管理员用户经 `/settings` 可改。

常数整改第三批（P10，分支 `worker/ds2-p10-batch3`，2026-10-02）：agent/gateway_parts 28 个＋agent/core.py 3 个待整改常数合规——
数量上限类补 `_COUNT` 后缀改名（如 `LOOP_GUARD_LIMIT→LOOP_GUARD_LIMIT_COUNT`、`_RECORD_LIMIT→_RECORD_LIMIT_COUNT`、
`_BRIEF_MAX→_BRIEF_MAX_CHARS`、`_SHORT_ID→_SHORT_ID_CHARS`），时间/长度类补上方中文说明（数值一律不变，改名引用全仓同步含测试）。

---

2026-10-02（分支 `worker/ds1-p10-batch4`）：常数整改第四批。cli 目录 41 个文件 100 个待整改常数已合规——按生成器后缀表补后缀改名
（如 `DAEMON_LIMIT`→`DAEMON_COUNT`、`_BACKGROUND_OWNER_WORKERS`→`_BACKGROUND_OWNER_WORKER_COUNT`、
`LOOP_ERROR_PRINT_EVERY`→`LOOP_ERROR_PRINT_EVERY_COUNT`、`TOOL_PREVIEW_MAX_LINES`→`TOOL_PREVIEW_MAX_LINE_COUNT`），
全部补上方中文说明；深度类无物理单位只补说明并挪入白名单单独组（reason“无物理单位”）。数值一律不变；白名单 497→398。

常数整改第一批（参数中心 P10，分支 `worker/ds2-p10-batch1`，2026-10-01）：agent/ingestion、agent/scheduler、
agent/session_lock、agent/user_space、agent/adapter 5 个模块 115 个待整改常数（无单位后缀或无中文说明）已合规——
时间/长度/个数类按生成器后缀表补后缀改名（如 `_RETIRE_STALE_WINDOWS→_RETIRE_STALE_WINDOW_COUNT`、`_KEY_LEN→_KEY_LEN_BYTES`），
全部补上方中文说明；无物理单位常数（倍数/指数/深度）只补说明并挪入白名单单独组（reason“无物理单位”）。
数值一律不变，改名引用全仓同步（含测试）。白名单 685→575＋7（无物理单位组）；`test_constant_names_unique._ALLOWED`
删 TIMEOUT_S（改名后不再重复）、加 REQUEST_TIMEOUT_SECONDS（飞书注册请求超时与推理探测等待上限含义不同）；
目录重新生成 799 项 `--check` 一致。详见 TESTS.md。

2026-10-02（分支 `worker/ds1-p10-batch2`）：常数整改第二批。`gateway_parts/request_context.py` 与 `owner_wake_discovery.py` 的常数改名/补说明，如 `DEFAULT_VISIBLE_SUBAGENT_COMPLETIONS`→`DEFAULT_VISIBLE_SUBAGENT_COMPLETION_COUNT`（网关轮内子代理完成回执展示上限），数值不变。

## C7：智能程度八档（2026-10-02，sol，已上线 step17a，main de222698b，2026-10-02）

TUI 菜单与 TUI/IM 共用命令均接受 xhigh/ultra，配置枚举与派工 schema 同步；用户档位不在入口提前降档。
回执与实际后端共用 reasoning_control 唯一表，依候选模型声明给实际发送值，无交集说明不改变请求。
Gateway 自动采用的候选与首发送字段有本地替身传输组件证据，未启动正式 Gateway 或验证真实客户端。
扩展回归旧失败见 TESTS；建议 3a 合入后核真实 TUI/IM 与模型出站字段。合同见 `docs/design/REASONING_EFFORT.md` 第 9 节。

`/settings internal <关键词>` 查代码常数（参数中心 P10，分支 `worker/ds1-constants-catalog`，2026-10-01）：
`settings_control_service._internal` 调 `settings/constants_catalog.search_constants`（只读随包
`config/constants_catalog.json`，由 `scripts/build_constants_catalog.py` 用 ast 静态扫描生成，799 项/328 文件；
**目录不存行号**，显示的文件:行由运行时 `locate_line` 按名字只读打开那一个文件定位，定位不到只显示文件，不扫描整个包），
按名字/说明/文件搜索，显示文件:行、值、单位、类别、中文说明，回执首行标“只读，改动需改代码”；TUI 与 IM 共用
Gateway 控制通道，与 user_config action=search 的 constants 结果同数据源。守卫见 `test_constants_catalog.py`
（目录与源码一致且**不比较行号**——目录唯一失效时机是常数增删、改名、改值、改说明或改单位/类别；待整改白名单只短不长、协议类排除；
只在常数上方插空行的行号漂移用例 `--check` 必须仍通过）。`control_commands._settings_command` 新增 internal 子命令（词法同 search）。

## P12：Curator 档案设置与编号展示（2026-10-01，sol，已上线 step17a，main de222698b，2026-10-02）

- `/settings show memory_curator_model_profile` 在原参数详情后补运行编号/型号；有未生效修改时另列保存编号/型号，TUI 与 IM 共用回执。
- `execute_settings_control` 只在原完整管理员身份校验后开启用户专属边界写作用域，退出还原；模型 set/reset/revert、伪造 actor 和普通 owner 仍拒绝。
- `/model` 的 IM 文本列表以及 TUI 选择/服务商管理列表展示稳定档案编号，区别于临时选择序号，不新增协议或路由。
- 定向与守卫结果见 TESTS；真实 Gateway、终端和 IM 客户端显示未验证，由 3a 集成部署后复核，不把组件渲染当收信验收。

C14 M-B1（2026-10-01，sol2，本地候选）：新增 `plugins/shuohao-novel-gates/` 五阶段只读工具，
复用原安装/本人解释器确认/Registry/HostCommand/ToolExecutor/MCP 链，不改 Gateway 或新增授权通道。
只从本次 `_meta` 读取上下文，门计算不写工作区；原样上游自检和来源摘要有仓库证据。
宿主启用失败与嵌套 Seatbelt 跳过保留，固定提交待 3a 沙箱外复验；真实 TUI/模型未验证，详见 TESTS 的 M-B1 节。
收尾补充（2026-10-02）：跨语言样例 2 passed、2 failed，Node/Go 均停在宿主启用确认处；
按 3a 明确范围只做定向文件与 guards9，不再跑全仓或无文件列表的-x，保持原安全校验与失败断言。

`/settings show` 显示参数元数据（参数中心 P8，分支 `worker/ds2-registry-metadata`，2026-10-01）：`settings_control_service._show`
对有值的参数补四行“单位／范围／归属模块／读取方”（`_metadata_line`，没推导出就不出现），来源是 `settings/parameter_metadata.py`
的自动推导：单位按键名后缀（_seconds/_ms/_chars/_bytes/_tokens/_percent 等，推不出留空）、范围取自现有规范化/校验规格
（_memory_coercion._FIELDS、runtime_tool_field_specs、services/_normalize 的 Service 规格与枚举、backends/sampling.validate_top_p）、
读取方与归属模块按 `test_config_field_readers` 同一套属性访问/字符串键引用扫描（decision_*/memory_decision_* 按
`decision_config_fields()` 映射补，读取方为 decision_settings_defaults）。user_config 的 view/search 同口径只在有值时带这四个字段。
回归见 `test_parameter_metadata.py`。

登记表增加来源维度（参数中心 P17，分支 `worker/ds2-registry-sources`，2026-10-01）：`parameter_registry.ParameterSpec` 加 `source` 字段，
三来源 `agent`（agent_config.yaml，224 键）/`capability`（capability_config.yaml，31 键）/`runtime_guard`（runtime_guard_config.yaml，22 键），
合计 277 键（log_analysis_config.yaml 已于 2026-10-01 删除：死配置、产品代码无任何读取点，见 `worker/ds2-del-log-analysis`）；
说明、类型、默认值从各随包 YAML 与 dataclass 取，规则与主配置一致。
写入口 `parameter_changes` 仍是唯一一套：agent 写在用户配置（user_path），capability 写在运行时实际读取的那份文件
（`capability_config_for_agent` 同一路径，由 `capability_config_path_for` 解析，文件不存在时新建、只写被改的键），
`_effective` 按来源选正式加载器（capability→`load_capability_config`、runtime_guard→`runtime_guard_policy`）
回读校验，写后回读不一致恢复原文件（或删除新建文件）并记 settings-changes 账本；capability 改动要重启 Gateway 才生效。
runtime_guard 运行时没有用户覆盖层，修改一律拒绝（PARAMETER_SOURCE_READ_ONLY，结构化原因“只能查看和搜索”），不新造写入口。
安全等级默认按边界（授权、权限、路径、执行权威链一律模型不可改），capability 的 17 个能力数值上限键为显式 free 名单（`_EXTRA_FREE_KEYS`，逐键带理由）。
`settings_control_service._show` 对每条参数加“来源：<source>_config.yaml”行，`_search/_all/_common_line` 运行值改走 `running_value`：
capability 按运行时路径读（owner 有覆盖时显示覆盖值），runtime_guard 显示随包默认并标“（随包默认、不可覆盖）”。
回归见 `test_parameter_sources.py`（16 项：三来源 search/view、capability 写运行时文件且运行时入口读到新值、文件缺失时新建、
只读来源结构化拒绝、回读失败恢复/删除、覆盖值显示、边界拒绝、user_config 工具链路）。

J6 决策实验自动晋升提示（2026-10-01，`worker/sol2-promotion-notice`，已上线 step17a，main de222698b，2026-10-02）：
- 原请求晋升回执追加唯一 `promotion_id` 与冻结规则；只返回新写入的 applied 回执，旧回执消费后重启不补投。
- 新 `request_experiment_notice.py` 只读结构化回执生成点位、前后模式、样本/门槛与真实恢复继承路径，排进原宿主提示队列。
- `finish_decision_experiment_turn` 返回本轮新提示；执行编排沿原发布 helper 追加并合入同批 final，保留开轮已发布提示且不捎带其它后台新提示。
- IM 仍走原 watcher/DeliveryService，无新线程、通道或通知账；沿原“提交即已读”与原队列失败口径。
- 111 项聚焦、166 项守卫与三项变异有本地证据；真实终端/飞书收信未验证，详细命令和风险见 TESTS 的 J6 节。

C10（2026-10-01，sol，已上线 step17a，main de222698b，2026-10-02）：飞书、QQ 等 IM 的 `/plugins` 与 `/plugins@<插件ID>` 已进入
会话控制，`/ask`、`/control` 先记原持久控制回执，不再走无回执的提前插件分路。执行复用 TUI 的
`plugin_command_service` / `PluginManagement`；IM 的管理员资格照 `/settings` 取可信解析 owner，
不从正文取得角色。错误码保留在结构字段和纯文本；可执行程序启用沿原预览、`--confirm`，不另开审批通道。
定向 155 项与三个独立变异已验，真实 IM 收发未验证；命令重放及边界见结构文档与 TESTS。

`/effort` 查看回执列出可选档位（分支 `claude/3a-effort-picker`，2026-09-30）：`control_service._execute_effort_control` 在查看
（operation=view）时于回执末尾追加 `reasoning_control.describe_level_choices()` 一行（可选档位与“/effort 加档位只改本会话、
/effort default 回到全局默认”），设置/检测/撤销回执不变。TUI 单独 `/effort` 改由本地档位菜单（`cli/chat_parts/tui_effort_menu.py`）
选好后发 `/effort <值>`，Gateway 收到的仍是同一控制命令。设计见 `docs/design/REASONING_EFFORT.md` 第 5 节。

/model 五项菜单与其他用户的初始模型（分支 `claude/3a-chatgpt-browser-login`，2026-09-30）：`/client/models` 不改路由，新操作
`add_models` / `set_initial` 与带 `connection` 的 `discover` 都经 `execute_model_profile_operation`。`model_profile_service.render_model_choices`
的「默认」行按 `default_source=admin_initial` 标「管理员指定的初始模型」，空列表提示改指「默认模型与共享」。
设计见 `docs/design/TUI_MODEL_PROFILES.md`「菜单结构」与 `docs/design/SHARED_MODEL_CATALOG.md`「其他用户的初始模型」。

唤醒毒丸第 3 步 C6（step16m，3a）：后台 supervisor 停机时先给本进程在途的唤醒尝试打停机标记
（`wake_attempt_tracking.mark_inflight_attempts_stopping`，写失败逐条吞掉），再关执行池；死进程那一片认领过的补充消息按尝试账里
持久化的回合号退回（`wake_domain_closeout.settle_abandoned_turn`）；结案顺序改为写记录 → 删 pending → 移坏账 → 删尝试账。

唤醒毒丸第 4 步复审跟进（step16m，3a）：`/wakes` 的领域终态改用 C4 的 `wake_domain_status` / `wake_domain_terminal`（唯一判定），
删掉本地版本，只在这里把裸状态拼成提示；重放来源的结案记录还原不成同 ID 信封时，重放与预览都返回 `WAKE_REPLAY_SOURCE_UNREADABLE`，
不再抛异常；归档清理须连同同键去重回执一起删（WAKE_POISON_PILL 第 8 节）。

出站协议合同的错误文案（分支 `claude/3a-wire-contract`，2026-09-30）：
- `request_errors.gateway_client_error_message` 新增 `PROVIDER_REQUEST_SHAPE_INVALID`：后端出口在发送前查出消息结构违规，
  请求没有发出、本轮停止，文案请用户反馈运行诊断。错误本身与规则见 `docs/design/PROVIDER_WIRE_CONTRACT.md`。

维护回收的错误码与缓存不可读时的行为（分支 `my-agent/self-dev-2-vcache`，2026-09-29）：
- **第五轮补充（2026-09-29）**：回收的错误码此前抓不到最可能出的错——缓存文件读不出内容时
  `_load` 静默返回空、`retain_matching` 把异常吞进 `last_write_error`，维护状态里的错误字段仍是空串
  （探针 V5）。现在 `_load` 对坏 JSON 记 `last_read_error`，`reclaim_text_cache_orphans` 把读/写错误
  一并带出来，`_reclaim_text_vector_cache_orphans` 直接透传它的 `(回收数, 错误说明)`。
  回归见 `test_v5_reclaim_reports_error_when_cache_corrupt`；变异 MY11/MY12 必须被杀掉。
- `_reclaim_text_vector_cache_orphans` 现在返回 `(回收数, 错误码)`。建缓存/读记忆失败同样返回 0，
  但与"确实没有孤儿"分得开——错误码写进维护状态 `text_vector_cache_reclaim_error`。
  此前只有 `text_vector_cache_reclaimed: 0`，两种情况看起来一样，又是一条假的结构化事实。
- 正文哈希缓存的"构造宽松、写入严格"见 memory 模块 04-structure。

owner 维护回收正文哈希缓存孤儿：改为 canonical 路径 + 不依赖 embedder（分支 `my-agent/self-dev-2-vcache`，2026-09-29）：
- `user_space/owner_maintenance._reclaim_text_vector_cache_orphans` 此前手拼 `owner_home/memory/memory.jsonl`（**生产实际在
  `home.owner_memory_long_term_jsonl`**，文件不存在 → 直接返回 0），且新建的 `JsonlMemory` 没有 embedder
  → `_text_vector_cache()` 为 None → 回收数永远是 0。后果是 `maintenance.json` 每天写
  `text_vector_cache_reclaimed: 0`，**看起来像"跑过、没有孤儿"，实为假的结构化事实**。
- 改用 canonical 路径；回收改走 `JsonlMemory.reclaim_text_cache_orphans()`，
  它按缓存键里的**正文哈希**（`DataTextVectorCache.retain_content_hashes`）比对 active 记录的
  `index_text` 哈希，不需要 embedder、不联网。
- 回归见 `test_memory_vector_cache.py::test_maintenance_reclaims_orphan_on_real_layout`；
  变异 MY1（回收函数开头直接 `return 0`）必须被杀掉——见 `scripts/mutate_text_vector_cache.py`。
global_index 只追加索引自动压缩（分支 `my-agent/self-dev-2-index`，2026-09-29，基于 `64f7ee64e`）：
- `user_space/home_index_compact.py`：四份 `global_index/*.jsonl` 在维护 tick 里按 key 内部压缩
  （纯投影，**不读任何权威源**；手动 `home-index-rebuild --apply` 仍是权威修复工具，不变）。
- **两遍流式**：第一遍只按 **LF** 切行、记下每个 key 最后一次出现的行号；第二遍按原顺序把那些行
  流式写进临时文件，**原字节照抄、不重新序列化**。读取方 `_latest_unique_refs` 是「先 reversed、
  再遇首次出现即取」，所以压缩后读取结果按构造逐条相同。
- **每个文件用自己的 key_fields**（owners 只有 owner_id；tasks/runs/agents 各自带自己的 id）。
  统一成一套 fields 会让 runs 的 task_id 版本被当成两个 key，出现"旧状态复活"。
- 峰值内存实测：**145.4 MB 文件 → 峰值增量 22.9 MB、0.7 秒**（第一版把整份前缀解析成 dict，
  同一文件是 952 MB、约 6.5 倍）。旧文档里"峰值 42 MB"是在小文件上量的数，已在 04-structure 更正。

38 复审跟进（2026-09-29）：
- **必须改：文件身份核对原本在锁外**，`_still_same_file` 通过之后才拿锁，而 rebuild 恰好能落在
  「核对通过 → 拿到锁」之间，把陈旧前缀盖到 rebuild 的新内容上（实测读回 `['B','A']`，
  rebuild 写的 `REBUILT` 丢了）。现在拿到锁之后**再核一次**，不一致就 `identity_changed` 并丢 tmp。
  锁外那次保留——它挡的是扫描期间换文件，两道作用不同。
- **失败原因单列**：`_compact_global_indexes` 原先只记 `compacted=True` 的结果，
  `io_error` / `identity_changed` 被静默丢掉，`maintenance.json` 分不清「没到期」和「压失败」。
  现在返回 `(成功摘要, 失败摘要)`，失败进 **`indexes_compact_failed`**；
  「没动手」的四个原因（`below_min_bytes` / `below_growth_ratio` / `in_cooldown` / `missing`）不算失败。
- **键定义单一来源**：`home_indexes.INDEX_KEY_FIELDS_BY_FILE` 是唯一权威，写入侧与压缩侧都从它派生。
- 触发用**零扫描判据**：当前大小 ≥ `max(64 MB, 2 × 上次压缩后大小)`；冷却 `COMPACT_COOLDOWN_SECONDS = 6 小时`。
  上次压缩结果**持久化到索引同目录的 `compact_state.json`**，否则 Gateway 每次重启后第一次 tick
  都会不受冷却限制地压一遍。
- 并发按长度切：锁内记 `(st_ino, prefix_len)` → 放锁压缩前缀 → 重拿锁核对 inode 与长度，
  把前缀之后新追加的字节接上，再原子替换。坏行计数上报在结果与维护状态 `indexes_compacted` 里，不静默。

停机时等 observe 后台执行器落账（分支 `claude/9a-jev-observe-async`，2026-09-28，dsh-ae 复审跟进）：
- `cli/gateway_process._cancel_active_decisions` 在 `cancel_active_decisions_for_shutdown` 之后，经 `wait_nonblocking_idle` 最多等 `_NONBLOCKING_DRAIN_SECONDS`（2 秒）。
- 取消与等待各在自己的 try 里（ae 复核建议）：取消的 try 与原来一致；执行器模块的导入与等待放在第二个 try，出错只记异常类型事件 `gateway_decision_drain_failed`，不会跳过取消，也不会被误记成 `gateway_decision_cancel_failed`。回归见 `test_gateway_decision_shutdown_cancel.py::test_gateway_cleanup_still_cancels_when_the_observe_drain_fails`（等待抛异常、执行器模块导入失败两组）。
- 被取消的调用不再等网络（排队中的不发送，在途的立即停止等待），通常几毫秒就写完；这一步只是防止丢掉最后一行结果和一条用量，等不到也照常收尾。
- 回归见 `test_decision_observe_nonblocking.py::test_gateway_shutdown_waits_briefly_for_background_rows`（写行被放慢时，返回前结果行已落盘）。
- 另核实：请求收口（terminalize）读的是盘上文件，Gateway 没有用内存副本整体回写请求记录的路径。`record_capability_presentation_observation` 与 `complete_deferred` 里同步更新内存副本，只是让同一请求里之后读内存的代码看到同一事实，注释已改正。

选模型观察转后台后的补记（分支 `claude/9a-jev-observe-async`，2026-09-28）：
- 决策设置 `observe_nonblocking_enabled` 打开时，选模型的 observe 当场返回 deferred；请求记录的观察标记先记 `deferred`，主模型不再等 Jev。
- 后台完成后，由 `GatewayModelObservation._complete_deferred` 经新增的 `GatewayModelObservationWriter.complete_deferred` 补记建议编号：
  - 只替换同一观察（op/claim 相同）的 started/deferred 标记，`adopted` 恒为 false；
  - 写入走原 active-turn 事务，回合已结束就不回写。
- 回归见 `test_decision_observe_nonblocking.py` 的三组选模型用例。
owner 维护顺带回收正文哈希缓存的孤儿键（分支 `my-agent/self-dev-2-vcache`，2026-09-28）：
- `user_space/owner_maintenance.run_owner_retention_if_due` 在既有维护事务（默认 24 小时一次、已在 `locked_json_path` 内）里
  多调一次 `_reclaim_text_vector_cache_orphans`，回收 `memory_text_vectors.json` 中不属于任何 active 记忆的键。
- 动因：这些孤儿键（换模型、迁移、绕过写入路径改正文留下）原先只有手动 `home-index`/`index_all` 才会清，
  挂进维护循环后不跑手动命令也能自动收口。回收只读 active 记忆算保留集合，不加载嵌入模型、不联网。
- 失败只返回 0 并照常写维护状态，绝不影响 retention 结果；新增字段 `text_vector_cache_reclaimed`。
- 回归见 `test_memory_vector_cache.py::test_index_all_reclaims_even_without_local_store`（无 LocalStore 时也必须回收）。

`/endtask` 结束卡在等待中的定时会话任务（分支 `claude/be-end-session-task`，2026-09-29）：
- 来源：2026-09-28 两个会话的定时运行里 `run_command` 结果未知，工作片停下而会话任务仍 active，定时运行停在 waiting 永不结算，同一 job 的到期派发被一直跳过。
- 新增会话控制 `/endtask`，由 `control_service` 按 kind 分派到 `gateway_parts/end_task_control.py`，TUI 与飞书共用；仅本机管理员可用。无参数列候选、只给任务 ID 只读预览、带 confirm 才写。
- 放行只认结构化事实：定时账本里是 waiting、会话任务链接是 active、运行库整棵执行树没有未结束的 attempt（运行库读不到按无法确认拒绝）。确认只写两处：`tasks.update_status(cancelled, expected_status=active)`，再对同一任务调 `reconcile_waiting_run`。
- 回归见 `test_end_task_control.py`。根因修复（只在确有后续工作时才进 waiting）另排，见 DESIGN_LEDGER。
- 9a 复审跟进：6 个 `END_TASK_*` 拒绝码登记进 `ERROR_CONTRACTS`（全仓守卫 `test_recovery_code_policy` 转绿，字典里的码另有模块测试钉住）；预览和确认结果固定写明“结束任务不会停止它启动的后台命令；这些命令结束后的通知会落到已取消的任务上”。
- 后续工作事实码（分支 `claude/be-endtask-follow-up-facts`，基于 `e850ceb04`）：列表每行和预览都附“后续工作”，直接调 owner 的 `scheduler_service.follow_up`（与定时执行收口同一个判定），只显示事实码与读不出的“项目:错误码”，没有写“无”，判定未注入写“判定不可用”；`GRACE_BOUND_FACTS` 里的码后面加“（宽限期内才算）”。

定时执行 waiting 死锁（分支 `claude/75-scheduler-waiting-deadlock`，2026-09-29）：一轮定时执行结束后任务仍是 active 时，
`_finish_scheduler_wake_claim` 改调 `scheduler/active_run_closeout.close_active_run`：`conversation/task_follow_up` 判定有结构化后续工作才进 waiting；
没有就把任务 CAS 成 blocked、排 `scheduler:<job_id>` 宿主提示、run 记 failed（工具结果无法确认为 `SCHEDULED_TASK_TOOL_OUTCOME_UNKNOWN`，
其它为 `SCHEDULED_TASK_UNFINISHED`），job 下一周期照常派发。`blocked` 进入任务终态映射；存量 waiting 停满 600 秒且无后续工作时由对账按
`SCHEDULED_TASK_WAITING_WITHOUT_FOLLOW_UP` 结算。报告新增 `runtime_status/runtime_reason`，来自 `AgentRunResult`。
设计见 `DESIGN_LEDGER.md` 同名条目，回归见 `test_scheduler_waiting_deadlock.py`、`test_tool_unknown_reason_preservation.py`。
复审修复 A：后续工作某一项读不出来时先按记录归属限定，剩下的继续等并按 run 节流打 `scheduler_follow_up_unreadable` 告警，
停满宽限期 6 倍按 `SCHEDULED_TASK_FOLLOW_UP_UNREADABLE` 结算；`_finish_scheduler_wake_claim` 收口没成（CAS 失败只释放租约）时退避 30 秒。
管理员的人工出口是 `/endtask`。
复审 A 的跟进：同类读错误不再遮住本任务确认存在的唤醒、进度策略或后台命令；后台命令坏记录的 `completion_target: {}` 按不欠通知跳过；
告警节流表清理先拍快照再遍历。
复审修复 B：已终态但完成结果还没交回父级的子代理算后续工作（修掉收口时子代理刚结束被误标受阻）；宽限期改为
`max(600, 5 × orphan_supervision_interval_seconds)`；Goal、guidance、进度策略只在宽限期内算；存量判定每 run 60 秒最多一次；
读不出从首次观察到“只剩读不出”起计时；子代理血缘记录按 id 精确读，缺失或损坏记为读不出。
复审 B 跟进：按“没有后续工作”结算要隔 60 秒两次确认（健康长 Goal 的续跑空隙不再被误结算）；本任务血缘子代理的坏完成唤醒按可能属于本任务处理。

SLP-2A scheduler claim 栅栏（2026-10-05，`worker/slp2a`；状态：已实现，待 3a 复审）：`claim_run` 与 `recover_interrupted_executions` 现共用 PID/starttime 三态死亡证明；expired+live/unverifiable 保留当前 claim，dead proof 后先 CAS 为 queued，再由普通 claim 生成递增 epoch token；迟到 heartbeat/finish 的 epoch 写入会被拒。misfire policy 和 `scheduler/service.py` 未变；SLP-2B 的 action operation identity 仍待实现。19 文件 sweep 的 9 项 `test_scheduler_waiting_deadlock.py` fixture 失败已在基线 `812828b98` 复现，其余相关用例通过；guards9 前 11 项通过（基线无第 12 项），静态与尺寸门禁通过。命令和未验证项见 `TESTS.md` 的 SLP-2A 小节。

capability 配置缺文件用默认值（分支 `claude/9a-capcfg-missing-defaults`，2026-09-28）：
- `/settings` 与 `/settings all` 的配置告警原本从主配置对象上找 `capability_config`，但 AgentConfig 没有这个属性，所以 capability 文件里没生效的键在生产上从来显示不出来。
- 现在由 `execute_settings_control` 经 `capability_config_for_agent(base_agent)` 取得 capability 配置，作为关键字参数只交给这两个视图。
- 回归见 `test_settings_config_warnings_display.py`：告警来自真实的 capability 文件。

压缩触发来源与校准口径（分支 `claude/38-compact-calibration`，2026-09-28）：`request_execution` 首次准备按 `preflight` 安装恢复宿主；
溢出循环把结果的 `runtime_source`（`preflight` / `provider_error` / `tool_context_overflow`）写进 `RunParams.compact_trigger_source`，
`_gateway_compact_overflowing_turn` 再经 `prepare_gateway_compact_recovery(trigger_source=…)` 交给公共恢复器，压缩进度事件（含 started）
带 `trigger_source`。恢复器在 `select` 冻结校准观测后，候选与压缩前都按同一口径折算，压缩开始时写进线程快照的 `before_tokens`
不再是未校准值。设计见 `DESIGN_LEDGER.md` 同日一节，回归见 `test_compact_calibrated_candidate_gate.py`（含两回合假 LLM 复现）。

TUI 插话丢失修复（分支 `claude/be-steer-loss`，2026-09-28）：生产结构化事实显示，插话随模型调用提交后，这次调用以
`ProviderTransientError` 失败。当时两层重试都因为“有未确认插话”不重发，attempt 失败；新 attempt 又不重发“提交不明”的插话，
入口回执永远停在 `active_pending`，TUI 每 2–3 秒轮询。修复：失败调用按调用编号把插话退回预留，同一 attempt 的重试重新提交；
重试守卫只看在途提交；没有请求文件的后台目标（定时任务 `srun_*`）在会话任务终态后，未认领的插话转成下一轮。
结构见 `04-structure.md` 同日一节，回归见 `test_steer_delivery_recovery.py`。

`/settings` 列表隐藏加载器元数据（分支 `claude/9a-batch3-e`，2026-09-27）：`settings_control_service._overview`、`_all` 改用`parameter_registry.listed_parameters()`，总数、“改过”统计与分类清单都不再包含 `config_path/config_sources/config_layers/config_warnings/memory_config_warnings`（加载器写进 AgentConfig 的元数据，不是参数）；参数搜索与 user_config 的可改数量同口径。字段与登记表项不删，`/settings show config_warnings` 仍可查看。回归见 `test_settings_chat_control.py`。
参数减量第 3 批 B 组（分支 `claude/38-internal-constants-bd`，2026-09-27）：Gateway 心跳节奏、心跳陈旧判定、request worker 空闲轮询、后台普通异常冷却、磁盘级 owner 唤醒重扫间隔、控制命令 HTTP 等待上限、一次 tick 消费的未处理观察上限七项不再是配置项，降级为读取点旁的具名常量（值不变：`lease_service.GATEWAY_HEARTBEAT_INTERVAL_SECONDS`=5、`status_rendering.GATEWAY_STALE_SECONDS`=120、`gateway_loops.GATEWAY_REQUEST_POLL_INTERVAL_SECONDS`=0.2、`gateway_loops.BACKGROUND_MAIN_ERROR_BACKOFF_SECONDS`=30、`gateway_loops.BACKGROUND_OWNER_WAKE_RESCAN_SECONDS`=120、`chat_parts/control_runtime.GATEWAY_SERVICE_COMMAND_TIMEOUT_SECONDS`=30、`conversation/runtime.CONVERSATION_UNHANDLED_OBSERVATION_LIMIT_COUNT`=20）；用户配置里残留的旧键只在加载时告警。场景测试与 Live Lab 生成的配置不再写 `gateway_request_poll_interval: 1`（这两处的 Gateway 改按默认 0.2 秒轮询）。相关测试改为 patch 常量（`test_lease`、`test_gateway_loops_resilience`、`test_slow_model_liveness`、`test_chat_control_runtime`、`test_background_main_agent_runtime`）。

宿主提示（分支 `claude/be-host-notices`，2026-09-27）：前台回合在用户消息落账后、模型执行前发布会话待送达的 `host_notice` 流事件；正常回复提交时按编号取走（提交即已读），写进最终消息元数据与 `channel_delivery.host_notices`，`/result` 白名单放行、`/progress` 不转发；停止或失败不取走。飞书在同一条回复正文前加“【提示】”，TUI 与同会话窗口画成灰色系统行，历史回放排在用户消息之后。设计见 `docs/design/HOST_NOTICES.md`，回归见 `test_host_notices.py`。

`/settings` 值文字与其它回显出口统一（分支 `claude/9a-mask-value-display`，2026-09-27）：`settings_control_service._value` 删掉为绕开旧 `mask_value` 写的布尔、数字特判，值文字只由 `user_config_capability.mask_value` 给出（非凭据布尔 true/false、数字照实、None/空串/空列表/空映射为空串、凭据遮住），聊天里空值仍显示“（空）”。`mask_value` 根修后 user_config 的运行值/默认值/查看报告、改参回执的 `effective` 与命令行 `config get` 不再把 False、0 给成空串。回归见 `test_value_display_parity.py`。
`/effort` 智能程度检测（分支 `claude/be-effort-probe`，2026-09-27）：`_execute_effort_control` 在设置档位后调用 `settings/reasoning_probe.effort_probe_lines`（先执行再渲染回执）：档位设成 auto 以外、当前模型未声明且解析为不支持时后台自动检测一次；`/effort probe` 手动检测，`/effort revert <编号>` 撤销检测写入的档案修改，`/effort` 显示进度或结论。检测在后台线程跑，控制命令不等网络。回归见 `test_reasoning_probe.py`。
`/settings` 默认只看常用参数（分支 `claude/9a-settings-common-view`，2026-09-27）：`settings_control_service._overview` 只列参数中心
常用层级（`parameter_registry.COMMON_KEYS`，21 项）的当前运行值、是否改过、说明第一句，改了没重启注明“发 /restart 后生效”，常用以外
改过的只报个数；新子命令 `/settings all`（`_all`）是原总览加按分类的全部参数清单，［改过］［安全边界］标记。解析器 `all` 与不带参数同样
不接受多余参数，TUI 文本还原为 `/settings all`，命令目录补了帮助条目；IM 长回执由飞书适配器按行分片。回执里的布尔与数字不再经
`mask_value`（原来 False、0 显示成“（空）”；该特判已由最上面一条在 `mask_value` 根修后删除）。回归见 `test_settings_chat_control.py`、`test_parameter_registry.py`。
停止收尾补写决策点到达计数（分支 `claude/9b-owner-path-scope`，2026-09-27）：`_cmd_gateway_run_cleanup` 在结清在途模型调用、列出存活后台会话之后调用 `_flush_decision_reach_counts`，把 `conversation/decision_reach_counts` 里还没落盘的计数写出，部署重启不再丢最后一段；只接正常停止路径，不注册 atexit，出错只记 `gateway_decision_reach_flush_failed{error_type}`。原先内联的“取消在途决策”抽成 `_cancel_active_decisions`，行为与事件名不变。同分支修复 Gateway 用户读到本机主用户`memory_policy.json` 的作用域问题（`owner_resolver`）。回归见 `test_gateway_decision_shutdown_cancel.py`、`test_gateway_per_user_scoping.py`。
参数减量第 2 批（分支 `claude/9a-merge-config`，2026-09-27）：后台会话执行权只剩 `background_claim_ttl_seconds` 一个旋钮，续约心跳始终由 `run_claim.claim_heartbeat_interval_seconds(ttl_seconds=…)` 推导（90 秒时 30 秒）；手动 Compact 车道（`control_service._manual_compact_lane`）和前台请求车道（`request_binding`）不再读 `background_claim_heartbeat_interval_seconds`（已删除，旧键只告警并忽略），子代理 runner 会话心跳固定 5 秒。默认行为不变。

`/settings` 回显的结构脱敏（分支 `claude/be-structured-masking`，2026-09-27）：`show`、`search`、总览经 `mask_value` 结构脱敏，请求头与 MCP 服务器 env 的值只留键名、args 里凭据开关的值、名字是凭据的 `名字=值`、`--header`/`--env` 的值与网址密码遮值；`history` 行先经 `parameter_changes.displayed_change` 再遮一次，旧记录也不漏明文。回归见 `test_structured_masking.py`。

`/settings` 回执与脱敏补全（同一分支第二个提交，2026-09-27）：`show` 那一行由“实际使用值”改名为“实际效果”；`reset`、`revert`
回执与 `set` 同一口径附“按新值在默认模型上的实际效果”（回到默认时按登记默认值算，`parameter_changes.applied_after_change`）；
回显脱敏改用唯一的凭据判定 `user_config_capability.is_credential_key`，原先 `api_key`、`gateway_auth_token` 等 5 个凭据在
`/settings show` 与 `search` 里是明文。回归见 `test_settings_chat_control.py`、`test_parameter_registry.py`。

`/settings set` 附上新值的实际效果（分支 `claude/be-param-descriptions`，2026-09-27）：`settings_control_service._set` 对登记了派生规则的参数（`parameter_registry._APPLIED_RULES`：max_tokens、model_reasoning_effort）多一句“按新值在默认模型上的实际效果”，与 `show` 同一口径（Gateway 启动配置即默认模型，注明 /model 切换过的会话可能不同）；推理强度在不支持调节的模型上如实说“不改变请求”。计算经 `applied_value_with` 的只读新值视图，不另写判断；没有派生规则的参数回执不变。回归见 `test_settings_chat_control.py`。

参数中心同名常数收敛（分支 `claude/param-center-dup-constants`，2026-09-27）：流式 chunk 单次读取上限只在
`gateway_parts/io.STREAM_CHUNK_READ_MAX_BYTES` 定义（8 MiB），CLI 与 TUI 两个网关客户端改为导入；适配器入口不再另写领取时限，
直接用 `GatewayClaimLeaseConfig` 的默认值。数值不变。

`/settings show` 显示实际使用值（分支 `claude/settings-view-facts`，2026-09-27）：配置值会在运行时按规则派生的参数（目前只有
max_tokens，按模型窗口 ÷ 4 夹取）多一行“实际使用值”，按 Gateway 启动配置即默认模型计算，并注明 /model 切换过的会话可能不同；
派生函数来自参数中心 `parameter_registry.applied_value`。测试见 `test_settings_chat_control.py`。

聊天 `/settings`（参数中心阶段 2，分支 `claude/param-center-phase2`，2026-09-27）：用户希望 my-agent 与自己都能改更多参数、
改错能回滚，且用户几乎不用命令行。`control_service` 新增 `settings` 分派（在 steer/stop 默认路径之前），交给
`settings_control_service.execute_settings_control`：只有管理员可用；查找、查看、修改、恢复默认、修改记录与回滚都走参数中心，
修改写入当前加载的用户配置并记入 `settings-changes.jsonl`，重启 Gateway 后生效。TUI 本地模式明确拒绝，文本还原保留原值。
测试见 `test_settings_chat_control.py`，设计见 `docs/design/PARAMETER_CENTER.md`。

2026-09-27：新定时运行在原claim成功后、模型前准确绑定原TaskStore，旧任务/pins/marker保持。合法冻结回复经当前pending回读后优先走原交付；定时工作使用原终态映射，无法确认状态或结算CAS失败时不消费wake。独立复核发现的claim接手反例已复现并修复，最终14文件375项通过；完整组合全仓与原生验证仍待完成，见[回归记录](../../../TESTS.md#c16全仓回归修复2026-09-27验证中)。

`/skills` 异常回执修正（分支 `claude/skills-receipt-fix`，2026-09-27，Codex 静态复核发现）：`execute_skill_control` 的普通异常
原来一律回“原记录没有改动、请稍后重试”，但回滚/删除是先改目录和登记表、再追加账本，账本追加抛 OSError 时改动已经生效。
现在按子命令是否写入选回执：写子命令（confirm/reject/learned_revert/learned_remove）只说结果没能完整确认、先查当前状态，
只读子命令只说暂时读不到；可预期失败的回执不变。不加事务或新状态层。测试见 `test_skill_chat_control.py` 的提交后失败用例。

能力包第七候选：新Goal绑定任务时可初始化原TaskLink的一次选包pending（默认关闭）；只读当前owner的元数据资格，旧任务不补字段，不新增模型调用或Goal状态。组件与主流程组合验收中，未发布。

聊天 `/skills`（分支 `claude/skill-proposals-tui-im`，2026-09-27）：用户几乎不用命令行，技能提案与自动总结 Skill 原来只有
`my-agent skills …` 入口。`control_service.execute_gateway_conversation_control` 新增 `skills` 分派（在 steer/stop 默认路径之前），
交给 `skill_control_service.execute_skill_control`：按控制范围解析 owner，提案确认/拒绝必须带用户看到的版本号并由服务端锁内复核，
列提案时调用自学习审核顺序点，自动总结 Skill 走 `capability/skill_learning_report.py`（与 CLI 共用）。TUI 本地模式明确拒绝，
文本还原带上参数。测试见 `test_skill_chat_control.py`，设计见 `docs/design/SKILL_AUTO_SUMMARY.md` 第 10 节。

`/effort` 从空壳改为真实会话设置（分支 `claude/reasoning-effort`，2026-09-26）：`control_service._execute_effort_control`
读写当前 thread 的 `reasoning_effort`（auto/off/low/medium/high/max，`default` 清除回全局默认），回执说明当前会话模型的实际效果；
与 `/verbose` 共用新抽出的 `_settings_thread`（行为不变）。每轮请求在 `tool_model_generation._provider_request_options` 现读线程
档位，网关自动选模 `gateway_model_adoption._payload` 用同一函数投影（参数收进 `_PayloadSurface`），逐字核对保持一致。
设计见 `docs/design/REASONING_EFFORT.md`，测试见 `test_reasoning_effort.py`。

自学习 S3 接入后台策展车道（分支 `claude/skill-auto-summary`，2026-09-26）：`cli/gateway_loops.py` 的策展车道在记忆整理之后
处理自动总结 Skill 请求。owner 有待处理学习请求时，即使记忆总闸关闭或当日记忆配额用完也会被准入，但那时只跑自动总结、
不跑记忆整理（准入后按原条件重算）；`memory_curator_enabled=false` 而 `enable_self_learning=true` 时车道照常运转。
学习失败只写 `gateway_skill_learning.iteration` 诊断，不影响记忆整理与其它 owner。设计见 `docs/design/SKILL_AUTO_SUMMARY.md`，
测试见 `test_skill_learning_integration.py`。

Gateway 安全重启第三批：TUI 续跑边界与确认框作废（分支 `claude/turn-resumed-boundary`，2026-09-26）。
被重启或执行超时打断的回合由接班进程按原请求号续跑，TUI 读同一个 chunk 文件，以前没有任何“上一代已结束”的信号：
上一代开着的确认框让新确认在 `_TuiPermissionController.open` 报“已有待确认”被吞，回合可能一直等；旧回复、旧思考和续跑文本拼在一起；
死掉那一代的执行中工具卡一直显示运行中；续跑轮号按已落账调用数接着数，被打断那一轮没有落账时新卡与旧卡同号。现在：
- `request_execution._handle_gateway_request` 对 `request_binding.gateway_request_is_active_turn_recovery` 成立的请求，
  在创建 chunk writer 之后、执行本代之前经 `BufferedChunkStreamWriter.write_turn_resumed` 写一次 `{"kind": "turn_resumed", "cause": ...}`，
  cause 原样取 `active_turn_recovery.cause`（旧形式为空）；认领时已被停止的请求不执行也不写。
- TUI `tui_runtime._close_resumed_turn_generation`：旧审批按 cancelled 本地关闭、不写回；活动思考与回复按 interrupted 冻结；
  参数临时行与进行中的 Compact 进度收口；adapter 登记的未终态工具卡按中断收口，已终态的不重发；按 cause 显示“已自动续跑”提示；
  续跑代次的工具卡与审批块号追加 `:resume<代次>`。
- 旧版 TUI、普通 CLI 与 `gateway ask` 对该行投影为空；IM `/progress` 白名单忽略它。
- 未处理：同会话其它窗口经 `bg-main:` 后台显示流看到的旧代活动块；恢复放弃续跑时 TUI 终态仍不关闭运行中的工具卡。
回归见 `test_tui_runtime.py`、`test_tui_stateful.py`、`test_gateway_safe_restart.py`、`test_gateway_streaming.py`、
`test_gateway_client.py`、`test_gateway_verbose_progress.py`，设计见 `docs/design/GATEWAY_SAFE_RESTART.md` 文末第三批。

`/admin` 指引修正与审计软提示（主线，2026-09-26，真实验收发现）：执行飞书请求的是 owner 池里按用户隔离的 agent，其 owner 字段被改成该用户，原判定里的“基础 owner 是 local/main”永远不成立，指引没有出现；`admin_binding_hint_for_request` 改为只看开关、全局数据根的密码文件与绑定表。`audit_records` 的 requests 主题给管理员附软提示（查飞书用 all_owners、已设密码未绑定时发 /admin），`MODEL_NOT_CONFIGURED` 处理建议补上 `/admin`。真实验收里 my-agent 两轮工具即给出正确结论。

审计工具新增 `requests` 主题（主线，2026-09-26，用户要求“这种东西以后 my-agent 能帮我解决”）：每个请求开始执行时响应带 `owner_id`（宿主解析的执行 owner 规范编号，`request_execution._executing_owner_id`），`request_audit_records.request_outcome_records` 按 `OutcomeQuery` 读窗口内请求结果（状态、错误码与错误分类表的处理建议、渠道、私聊/群聊、耗时），归属优先 `terminal_response.owner_id`、旧记录退回会话，都没有的列为 `unattributed`；经 `GatewayTaskBindingWriter.request_audit_outcomes` 供 `audit_records`（`tooling/audit_requests_topic.py`）使用，跨用户沿用管理员两道门。回归见 `test_audit_requests_topic.py`。

未绑定管理员时的 `/admin` 指引（主线，2026-09-26，用户真实使用中发现）：管理员设好密码后在飞书私聊直接发消息，因为还没 `/admin` 绑定，按飞书普通用户运行得到 `MODEL_NOT_CONFIGURED`，提示只提 `/model`。现在 IM 私聊、开关生效、已设管理员密码且该私聊未绑定时，失败回复（`request_execution._gateway_user_error`）和 `/model` 没有可选模型的回复（`model_profile_service._admin_hint`）追加 `/admin <管理员密码>` 指引；判定唯一入口 `request_worker.admin_binding_hint_for_request`。回归见 `test_admin_identity_gateway.py` 末尾两例。

IM 管理员身份与聊天内审批（主线，2026-09-26 合入，用户决定“支持注册飞书账号为管理员、确认用管理员密码”）。
以前管理员只有本机 local/main：飞书用户永远是自己的 owner，IM 请求也不带审批能力，需要确认的工具一律被拒。现在：
- 本机 `my-agent admin-password set` 保存 scrypt 管理员密码（`config/admin-password.json`，0600）。
- 飞书一对一私聊发 `/admin <密码>`，把 `(channel, user_id)` 精确绑定为管理员（`config/admin-channel-identities.json`）。
  之后这个私聊的请求与控制作用域都经 `admin_channel_identity_for_request` 解析为 local/main。
- 同一渠道身份 10 分钟内错 5 次锁 10 分钟，节流记录持久化，拒绝文案不区分原因。
- Gateway 服务端为这些私聊开启原 `StreamApproval`。`/progress` 投影待确认工具，适配器提示 `/approve`、`/deny`。
  `/approve <密码>` 只批准本会话唯一待决的一次，`/deny` 拒绝；决定都写原 permission bridge 的精确决定文件。
- 密码不进回执（`/admin ******`）、会话记录、请求队列、日志、适配器持久入站或 TUI 输入历史；终端本地拒绝这三条命令。
- 开关 `admin_channel_identity_enabled`；新增错误码 `ADMIN_PASSWORD_REJECTED` 等 6 个。
- 已知边界：后台续跑与子代理的审批在 IM 里仍无人接收；飞书保留原消息，需要用户撤回。
回归见 `test_admin_identity_store.py`、`test_admin_identity_gateway.py`、`test_admin_identity_clients.py`，
设计见 `docs/design/ADMIN_CHANNEL_IDENTITY.md`。

Gateway 安全重启第二批（主线，2026-09-26）：终端 `gateway restart` 默认经 `cli/gateway_restart_handover.safe_restart_from_cli` 写 kind=cli 请求并按状态文件等新进程号 running 或本请求 cancelled（托管自己的工具进程仍拒绝），`--force` 保留先停后起；排空期间 `_RequestDispatcher` 以 `hold_reason=gateway_restart_draining` 调 `dispatch_pending_requests`，只给待处理请求写 `admission_wait_*` 等待事实、不认领，客户端据此续期；TUI `tui_upgrade_follow` 读 `restart_drain.phase` 在页脚提示正在安全重启。回归见 `test_gateway_safe_restart.py`、`test_tui_upgrade_follow.py`、`test_gateway_commands.py`（旧先停后起用例改为显式 `--force`）。

审计只读决策观察（决策线，2026-09-25，本地分支 `claude/decision-audit-controls`，已合入 main `2093631e5`）：新增 `gateway_parts/request_audit_records.py`，
为统一审计工具 `audit_records` 只读扫描请求记录里的 `model_selection_observation` 与 `capability_presentation_observation`，
按 `conversation_claim.thread_id` 归属调用方给出的 owner 会话，字段白名单投影（不含 prompt、工具名清单等），
只看窗口内修改过的记录、一次最多读 300 份，超出标 `truncated`，不写任何文件。入口是 `GatewayTaskBindingWriter.decision_audit_observations`：
队列位置只取写入器自己的请求路径，与实验证据读取同一做法，不信任 agent 自身的 Gateway 配置、不接受模型参数。
回归见 `test_decision_audit_controls.py`，设计见[决策开关、超时自调与审计](../../design/DECISION_AUDIT_AND_ADMIN_CONTROLS.md#5-统一审计工具-audit_records)。

Gateway 安全重启第一期（主线，2026-09-26，用户批准“代理要能自己重启且不出事”、full access 下免确认）：重启请求、两段排空、换进程、续跑与通知落地。`gateway_parts/restart_service.py` 是唯一状态源：`gateway_restart.request`（目标进程号、发起方结构化身份、原因；同一目标的重复请求合并为 `additional_requesters`）、进程内排空阶段、`gateway_restart.completed` 标记与 `gateway_restart_state.json`（冷却起点、近期请求）。服务主循环（`cli/gateway_restart_handover.drain_for_requested_restart`）发现指向本进程的请求后：第一段置 `restart_draining`，请求派发器不再认领新请求（留在 pending）、后台 supervisor 只回收已完成车道，等本进程 admission 在飞数归零，上限 `gateway_restart_turn_wait_seconds`；第二段关闭 `concurrency/restart_gate`，`tool_operation_coordinator` 的新副作用工具停在领取之前（被中断则按 not_started 的 `CANCELLED` 收口），等执行中的工具归零，上限 `gateway_restart_drain_timeout_seconds`（0 不限），超时撤销请求、恢复服务并给发起会话写取消通知。排空成功后写完成标记，按 `planned_restart` 收尾，再由旧进程拉起带 `--after-pid` 的接班进程（systemd 单元 cgroup 或 launchd 标签托管时改为退出码 75 交给管理器）；接班进程等旧进程退出后先消费标记，启动恢复以 `gateway_safe_restart` 原因立即重排旧回合（不加 10 秒延迟，排在新请求之前），再给发起会话写“重启已完成、不要再次重启”的持久唤醒。入口：管理员主代理的 `restart_gateway` 工具（effect=dangerous，只写请求立刻返回）与管理员 `/restart` 控制命令；托管自停闸的拒绝文案改为指向它们。冷却 `gateway_restart_cooldown_seconds`（默认 30 秒），同一会话 10 分钟内最多安排 3 次。回归见 `test_restart_gate.py`、`test_gateway_safe_restart.py`、`test_gateway_restart_tool.py`。未做（后续分期）：终端 `gateway restart` 与部署工具改走安全重启、重启窗口内 `/stop` 找续跑回合、TUI/IM 的重启提示与确认框作废。

托管自停闸与聊天 `/model`（主线，2026-09-25，用户确认 `/recover` 事故的两项后续）：`cli/gateway_host_guard.py` 让 Gateway 服务进程启动时把自身进程号写进 `MY_AGENT_HOSTING_GATEWAY_PID`，工具子进程经 `_subprocess_text_env` 继承（凭据擦洗不删它）；`gateway stop`、`restart`、`start --force` 在写停止请求前比对目标进程号，相等即拒绝并以 2 退出，回合不再被自己切断。`/model` 在命令目录增加会话后缀，成为 kind=model 的聊天控制：`control_service` 延迟导入 `model_profile_service.execute_model_text_control`，与 `/client/models` 菜单共用抽出的 `_scoped_model_host`（owner/线程解析）和 `execute_model_profile_operation`；只做 list/select/set_default，编号取同一列表的可选行，渲染不含接口地址和密钥。TUI 单独 `/model` 在 `_tui_submit_control_operation` 让回本地菜单。回归见 `test_gateway_host_guard.py`、`test_model_text_control.py`。

结果未确认的执行轮有了会话内显式出口 `/recover`（主线，2026-09-25，用户批准第 1 项）：真实会话里模型在回合中执行 `my-agent gateway restart`，Gateway 自杀后启动恢复把该回合 run/attempt 记为 unknown，那条 `run_command` 停在 EXECUTING；自动续跑按 `recover_recorded_active_turn_attempt` 拒绝（`ACTIVE_TURN_OUTCOME_UNCERTAIN`），之后每条新消息都续同一个 active 工作任务，在 `create_attempt` 的 unknown 闸被拒，而唯一人工出口 `recover_attempt_unknown` 没有任何用户入口，会话永久卡死。现在 `gateway_parts/turn_recovery_control.py` 按已认证 scope 解析 owner/thread，读 `thread.workspace_task_id` 的 `main_agent_recovery_block_for_task` 投影：`/recover` 只读列出 `unsettled_attempt_operations`（工具名、状态、开始时间，不读参数和结果正文）；`/recover recorded|confirmed_noop|abandoned` 以用户选定的结构化处置调用 `recover_attempt_unknown`，释放阻塞，下一条消息接着原任务新开一轮。处置取值表 `ATTEMPT_EFFECT_DISPOSITIONS` 放在 `runtime_db/operations.py`，解析器与仓储共用。`create_attempt` 的两处 unknown 闸改抛 `RuntimeRecoveryRequiredError`（`RuntimeConflictError` 子类，`error_code=RUN_RECOVERY_REQUIRED`），客户端文案指向 `/recover`，不再是“请稍后重试”；`ACTIVE_TURN_OUTCOME_UNCERTAIN` 文案同样指向 `/recover`。不自动重做旧操作，不改聊天记录，不按正文猜目标；状态不是 unknown attempt 的阻塞返回 `RUN_RECOVERY_REJECTED`。回归见 `test_turn_recovery_control.py`。

无进程身份的悬挂运行轮改为可见并可显式结清（主线，2026-09-24 晚，用户决定第 5 项自愈）：Gateway 启动的 `_recover_gateway_stale_attempts` 在原进程死亡证明之外，另经 `RuntimeRepository.unidentified_stale_attempts()` 列出 metadata 里没有 `runner_pid` 的 current attempt（旧版本写入、永远无法证实死活），写进 `state.json`/`gateway_run_started` 的 `unidentified_stale_attempts` 计数与 `gateway_stale_attempts_reconciled` 事件的 `unidentified` 列表；不自动判死（同一 owner 库可能被别的运行版本写入，原 RUN-01 合同保持）。结清走显式命令 `my-agent runtime-stale-attempts --settle [--older-than-days N]`，经 `settle_unidentified_attempts` 的 current_attempt CAS 记为 unknown 并在 metadata 记 `recovery_reason=no_runner_identity`。测试 `test_runtime_db_recover_stale.py`、`test_startup_commands.py`。

Gateway 停止时结清在途模型调用（主线，2026-09-24，用户决定第 4 项，已合入）：`_cmd_gateway_run_cleanup` 收完三条循环后调用 `agent_core/model/call_runtime.settle_open_model_calls_for_shutdown()`，把进程 agent 账本里仍是 started/first_token 的调用一次性记为 failed，错误码 `MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN`、类型 `HostShutdownInterrupted`，用量继续按缺报处理（不补零）；有在途调用才写事件 `gateway_model_calls_interrupted`（call/request/run 身份、后端、模型、账本用途桶、是否见首 token、耗时、估算输入、HTTP 尝试数、是否探针、原因码，不含正文），收尾事件与控制台行新增 `interrupted_model_calls` 条数。账本模块出错只记 `gateway_model_call_settlement_failed{error_type}`，不中断收尾。边界：只覆盖 Gateway 进程 agent 自己的账本（2026-10-02 J17 起，同进程登记过的 runner worker 与 owner 池 agent 账本一并结清）；子代理 runner worker 的账本在其自身生命周期内结算，非正常退出（kill -9、断电）留给启动对账切片。测试 `test_gateway_model_call_shutdown_settlement.py`。

线程中断标志不再随 ident 复用串到新线程（已合入 main `761ef2ab2`，双机已部署 `runtime-step11b-240d0f70`）：`concurrency/interrupt.py` 的中断标志改为记住立旗时的线程对象（弱引用）。线程已退出或 ident 换了主人即视为过期并清除；给已退出线程立旗直接落空，关闭竞态里晚到的立旗不再残留。此前长期运行的 Gateway 里，新线程可能复用带脏标志的 ident，被静默、随机地取消。公共接口不变。

Gateway 停止时主动取消在途决策（已合入 main `25650830d`，主线 owner 同意的一行）：`_cmd_gateway_run_cleanup` 置位停止事件后，立即调用 `decision_policy.cancel_active_decisions_for_shutdown()`，让正在等待决策模型的前台/后台调用回到原方案，关闭后不再发新决策。它只取消本进程内登记的决策句柄，不读写持久状态；用 try/except 包住，出错只记异常类型事件 `gateway_decision_cancel_failed`，不中断后续清理。停止时后台模型请求的结构化"被中断"记录由主线 owner 紧接着另加。详见[接入设计](../../design/DECISION_MODEL_INTEGRATION.md)第 4.2 节。

决策实验对照记录与授权内自动晋升（本地分支 `claude/decision-experiment-records`，已合入 main `5306c9483`）：只观察实验调用经原账结算后，结构化对照条目（身份、配置版本、基线/候选名单、结算视图）经能力观察出口拆出写进同一请求记录的 `experiment_records`；回合正常收尾时按结构化工具账补写实际调用工具名，停止/关闭的回合不补写。`/experiment apply skill_tool …` 另授权宿主在证据规则（≥3 可比较样本、全部 charged、短名单召回 1.0、有节省）满足时，于回合收尾在精确回合锁内经原设置 CAS 把本会话 skill_tool 改为 apply，用户后改、撤销、到期、被替换都跳过不覆盖；回执写在 `experiment_records.promotion`，已有即不重试。普通请求零 I/O、请求字节不变。详见[E1 交接第三片](../../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第三片e2-对照记录f1-证据评估与授权内自动晋升2026-09-25)。

决策实验授权入口（本地分支 `claude/decision-experiment-send-gate`，已合入 main `2cf214bd5` 等）：HTTP `/ask` 与文件队列沿 `/audit … prepare` 同一任务命令机制接收 `/experiment observe skill_tool <时长> <HTTP次数> <输入token上限> <任务>`，参数冻结进排队请求的 `system_task`、模型只见任务正文；新增 `request_experiment.py` 在主轮绑定后、首个模型调用前于精确回合锁内写 `experiment_grant` 回执并调用 E1 授权原语，重放/重启不再授权，失败只提示用户、不阻断业务。发送硬门、经验输入上界与结算归决策服务和传输层，详见[E1 交接](../../tasks/DECISION_MODEL_EXPERIMENT_E1_HANDOFF.md#第二片experiment-授权入口经验输入上界与发送硬门2026-09-24)。

能力推荐观测写进请求记录（本地分支 `claude/decision-capability-observation`，已合入 main（P4-G 真实验收随 `7143ef631` 关闭 item 13））：真的发起过能力推荐决策时，结构化观测（码、版本、名称与计数，无正文）经独立 observer 追加到 `capability_presentation_observation.entries`，最多 8 条；与模型观察同一 active-turn 事务，回合终结时照原语义抛中断，其它写盘失败只放弃这一条，内存请求同步更新。原展示回调、已有键不变。

Gateway 消息文件流式读取（本地分支 `claude/decision-gateway-message-reads`，待审）：建索引、近期产物、追加与补写去重不再按行数整块物化尾部，改为与原实现逐项等价的字节有界流式读取；4.2M 字符夹具上准备期峰值 21.33→0.82MB、全程 22.65→10.03MB，每次请求三次读取约 122→19ms。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#gateway-213mb-峰值来自整块读取消息文件2026-09-24已实施待审)。

媒体会话越过压缩点（本地分支 `claude/decision-media-preflight`，.9 真实验收已通过，待审）：未压历史带图时，preflight 只守窗口硬上限，越过压缩点也不再整轮失败；越过窗口时强制恢复报 `COMPACT_REQUEST_NON_TEXT`，客户端文案说明是图片等非文本内容使压缩不可用。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#媒体会话越过压缩点2026-09-24本地修复)。

12.4来源生命周期首片仅机械兼容显式只读Sequence：`_gateway_conversation_refs`按是否给出来源启用完整投影，防止换容器后误走普通展示窗口；history_projection接受非字符串Sequence。宿主的完整请求冻结/释放尚未重构，相邻回归另行记录，不把本片当全链内存收口。12.7固定旧包的同会话压缩/显式跨模型真实缓存另有证据，详见[真实验收](../../tasks/DECISION_MODEL_REAL_VALIDATION.md)。

12.4 第二片 2a（本地分支 `claude/decision-12.4-2a`，未合入）：Gateway 上下文与恢复候选只保存只读历史来源，移除 `_gateway_conversation_refs` 和具体副本；4.2M 字符下种子准备驻留约 54KB，首次发送前峰值从 29.8MB 降到 21.3MB，摘要期峰值留给 2b。独立评审后：来源冻结投影时刻，终态折叠不随解析时间变化；删除 `_conversation_prompt_section` 生产不可达的 `include_transcript` 正文/摘要分支，原先断言该分支的两个用例改为断言生产路径（摘要只在种子历史段，操作证据只在上下文段）。详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md#宿主历史种子只读来源2a2026-09-23本地)。

12.4保留历史完整投影已本地实现：Gateway、后台和child的Compact来源/候选不再套普通字符窗，完整材料统一进入原容量门；普通展示保持原规则。73项联合及416项相邻回归通过（含重叠，不累加），整项12.4及11/18不变。见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Gateway Compact的原visible范围规则现编译成逐行selector，公共message_selection沿固定完整尾界两遍验证/筛选；后台通过原Store延后正文，共用同次任务范围。writer/CAS与执行身份不变。12文件326项通过，4项主线独占后台fake Store签名待集成，整体gate未通过；12.4仍开放，详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

决策模型第12.4媒体整合本地1419项联合与严格gate通过：普通媒体沿原模型发送，未知模态不自动切模型或提交强制Compact；摘要覆盖只到完整文字前缀，原生媒体后缀保留。原媒体M3验收不替代集成版证据；11/18和旧全仓八项失败状态不变，详见[容量审计](../../tasks/DECISION_MODEL_CONTEXT_AUDIT.md)。

Gateway/child的无transcript活动归档已移除先粗估提交再重新准备的旁路，统一复用完整请求候选与同次发送。混合transcript+carried在公共恢复器同时替换原历史及已标记工具交接，单次CAS封印双来源；普通摘要失败、未知IR、超量及取消不发送恢复业务。本片32文件738项与严格gate通过，准确边界见TESTS；初次/手动与真实native工具IR组合仍未完成。

Gateway/child现通过原canonical loader绑定同一Compact scope/view，真实恢复参数在CAS后取得获胜checkpoint。后台也已接公共完整请求恢复及活动归档纯投影；定向验收见TESTS。初次/手动、其它宿主活动归档和混合超大来源仍待统一，12.4保持未完成，未部署。

后台Compact已本地接同一scope/view的摘要注入和精确覆盖，局部来源/提交不改全线程摘要和游标；18文件联合420项通过，最终验证见TESTS。此片不证明完整恢复payload，Gateway/child准备同view、初次/手动和真实缓存仍待验；唯一TODO的12.4保持未完成。

Compact检查点底座已写v3，区分提交前驱与摘要基础；局部CAS保留全线程摘要/游标，新工具恢复按完整执行身份处理。后台实际选择scope并将同一摘要view交给注入和隐藏的接线尚未完成，12.4仍不关闭。

后台上下文的 `prepare_background_context` 保留原事实读取与进度对账，`render_background_context` 只消费冻结值并调用原预算器；`BackgroundHistoryProjection` 保存同次任务范围与摘要投影，纯种子投影不重读任务。完整后台Compact接线仍待作用域检查点边界闭合，不能把全局新摘要给detached或窄审计事件。

Gateway恢复协调已抽到 `agent_core/compact_request_recovery.py` 与child共用，原payload/CAS/取消证据保持；Gateway模块只负责自己的历史投影和边界事件。

- 第 12.4 项 Gateway overflow 已本地接通完整恢复请求计量：只读保留原来源，真实请求准备后生成摘要候选，原 checkpoint/CAS 成功后直接发送获选材料。两协议、工具开关、取消/代次竞争/摘要错误及后续工具轮等 16 文件联合 337 项通过；HTTP 为内存替身，未部署。子代理、后台、初次加载及手动 Compact 仍待接入，完整进度见 `docs/tasks/DECISION_MODEL_CONTEXT_AUDIT.md`。

- 主会话自动模型选择的 Stage A 已建立原线程版本事实：`model_selection_revision/source/last_explicit_revision`
  随原线程一次原子更新，显式同值选择也前进版本并终结 pending 子代理建议；旧数据全缺才归一为未知，坏字段拒绝。
  本片只提供宿主并发/恢复事实，尚未启用主会话自动采用，也不增加逐片确认或永久固定模型。
- P5-D Stage C 在后续独立片已把 Gateway 请求级建议接到主会话首次真实发送前：原完整请求与候选 provider payload 验证、目录代次→准确车道 T→线程 CAS；发送前明确拒绝才回原模型一次，HTTP 后不跨模型重发。fake HTTP 本片34项、联合302项通过，真实供应商验收待做；工程容量估计不能称为精确 token 上界，详见 `docs/tasks/DECISION_MODEL_MAIN_MODEL_ADOPTION_HANDOFF.md`。
  详见 `docs/tasks/DECISION_MODEL_MAIN_MODEL_SELECTION_HANDOFF.md`；Gateway 的 lane/模型作用域重排仍待后续片。

- Jev 能力推荐的同一 Gateway 请求展示复用已核对的内存选择：原执行回调保存是否评估过和采用的 Skill/工具展示，transcript Compact 与超窗重试按当前身份、权限和连接重新核验。无效则清除本回合旧值，已评估回合不再次调用 Jev；新请求从基础面开始。122 项本地组合覆盖 DB attempt 轮换、动态名卡/schema、失效和单次建议；原溢出及更多回归仍在收口，尚非真实 TUI 验收。

TUI 观察超时不再标记业务失败：canonical terminal 优先检查，同请求/同游标退避续等，页面退出收口；plain 有限等待保留。官网 M2.7 原页迟到回复及暂停恢复对照通过，详见 TESTS。

独立资源线新增连接寿命和空闲 owner 回收：16 个 HTTP worker / 128 在途不变，socket 空闲 5 秒、排队 2 秒后明确拒绝；状态计数改为目录类型复用。请求持有 owner 精确实例租用，维护不续空闲期，60 秒空闲且无持久硬事实才退池；不改执行状态和原队列。100 身份/50 执行槽已在受限测试机用假模型验证，官方真实 TUI 单列；尚未合并默认环境，见 [资源合同](../../design/TUI_RESOURCE_LIFETIME.md)。
- 插件面板入口 `/client/plugin-panels`（第 9 步，本地开发）：可信来源检查先于读正文，owner 只由 Gateway 作用域解析，冷 owner 不加载实例；
  活动投影复用 `conversation_agent_activity` 只读结果，交给进程内唯一展示服务；服务随 HTTP server 停止关闭。合同测试见 test_gateway_plugin_panels。
  第 10 步：另附本 owner 会话列表的惰性读取函数（只在面板订阅 `sessions` 主题时调用，白名单字段，读取失败按空列表）。
- 插件宿主只读 API `/plugin-host/query`（第 10 步）：回环来源 + 按插件激活发放的令牌，只读主题白名单；服务启动时登记回环地址、停止时清空全部令牌。

插件命令流的取消原语现直接引用 `common/cancellation.py`，不再越层依赖 tooling。保持逐请求取消和原审批运输，发布前回归进行中。

- 独立插件命令的交互审批已有本地实现：原 HTTP 请求线程执行，消息流运输原审批和结果，心跳仅检测本连接离开。
  同机 TUI 使用原 GatewayPaths 及服务端规范 owner 派生审批地址，覆盖按用户隔离关闭时的 owner 映射。
  复用 StreamApproval 和原文件桥且禁用会话批准缓存；每次 Enter 的令牌不借主/子任务，断连不自动重发。
  临时 HTTP/MCP 与 TUI 控制器组件已验证，完整实际多 TUI 装卸仍待验收，未发布部署；详见 TESTS。

- 配置命令已有本地接线：HTTP/direct 共用原管理员、来源权限、宿主请求和 ToolExecutor；正文只含文件引用，值只写私有安装表。
  安装表 v2 同次保存配置与版本，旧 v1 显式迁移；目录 v2 带安装版本使旧配置请求过期。Gateway/客户端开发回归已覆盖原入口。
  查询仍只读原结果，UNKNOWN 不重跑；没有新增队列、Agent 初始化或后台进程。启用、撤销与真实 TUI 装卸尚未完成。

- 第 4 步环境准备已有内部源码与临时 venv/pip 组件验证，尚未接 Gateway 启用动作。
  原 owner 配额锁增加显式非阻塞准入，旧调用默认等待不变；环境竞争时返回配额不可用，不另建锁或后台队列。
  原安装状态不因候选目录存在而启用；激活、撤销和实际多 TUI 验收仍待完成，详见环境合同及 TESTS。

- 第 4 步管理适配已在本地接线、未发布部署：`/client/plugins` 与 ask/control 复用原管理员授权，安装进入独立原运行及唯一工具执行器。
  查询按原请求只读，不初始化冷 owner 或重跑操作；超时保留未知及查询编号，目录刷新失败不抹掉已知结果。
  模型配置和插件管理共用 `owner_conversation_store.py`；完整代理和冷入口共用 owner 路径权限裁决。独立环境、激活和撤销仍待完成。

- 宿主目录片已发布同版双机，TUI 151—153 所测框架入口通过：`/client/plugins` 和 ask/control 共用原 owner 解析，返回不可变声明及 revision。
  冷用户不创建 Agent/thread；无中间件本机与群聊 metadata 保留原规则，不重做 auth。
  旧版本或缺失业务版本明确拒绝，不自动重放，不进入原控制或队列；实际插件贡献仍为空，装卸尚未实现。

- 参数次片已发布同版双机，所测 TUI 入口已复验：`ask/control` 在原鉴权后消费公共插件静态帮助或结构化错误。
  不新增 ControlKind，不触发旧控制回执持久化、guidance、模型或普通队列；后续宿主片已接 owner 声明投影，实际插件执行身份留生命周期实现。
  483 项相关回归及严格 gate 通过；TUI 146 的 Tab→Enter、两端各 13 类命令检查通过，143 原失败及普通任务质量单列。
  TUI 150 的活动请求首秒已验证静态命令分流，原 attempt 自然完成；未进入原生正文、guidance 或 Shell。

- 插话网络重放已收紧到原 mailbox 的旧/新 turn 排序锁与准确回执：先修提交/确认批次，只有最新 pending 才预留后继。
  回应显示实际回执状态；候选 ID 与 DB current 成对 CAS，半写失败沿原 pending 重试，模型与 runner 启动留在锁外。
  文件模式准入也已实现并发布，累计源码全仓与严格 gate 通过；TUI 138—142 分项复验和未实测旁支见 TESTS。

- 当前停止源码在主 Goal/task 外层锁下短读 Gateway T，释放 T 后进入 creation→子 Goal；主/子资源在同一控制边界固定。
  异步清理只接收冻结批次，不持 agent、不重扫后来恢复的孩子；PTY 或子树准备错误仍保留其它已提交清单。
  终态孩子保留业务结果，独立 runner 沿原心跳转交原轮取消。已同版部署双机，实际主后台、孩子、PTY、孙代理与另一会话隔离已验。

- 主资源停止已发布：持久主任务按正式 task/run/attempt 关闭权限，再冻结 v2 后台清单。
  原热请求绑定过期时不向恢复轮发任务中断；停止准备与 Goal 显式恢复串行，旧后台片在绑定新 attempt 前检查中断。
  进程清理在锁外消费原清单，部分失败保留已提交回执；后台退出确认与 PTY 异步请求分开。
  该片 20 个相关文件 735 项开发回归通过；后续源码已补 direct/local 与无持久任务热请求的主链，完整子树后台资源现已在源码接通固定清单，TUI 137 原失败未关闭。
- 无持久链接的热请求在原 T 锁内读取正式运行绑定，释放 T 后关闭精确权限并冻结主资源；已晋升、会话/绑定不可读和过期代次返回未确认。
  不补建 task link 或按请求编号扫描历史 main；此旁支本轮仅有开发回归，不能借 Gateway Goal 验收声称真实命中。

- 公共命令首片已发布并同包部署：HTTP ask/control、普通文件提交和旧队列执行统一读取公共命名空间判据。
  `/plugins@` 的缺 ID、异常后缀与正文参数均明确拒绝；原请求、中断回调和旧 guidance 保持不变。
  不新增插件控制类型或执行器，187 项相关定向（含文档测试）通过；实际命名空间与普通工具链已验，TUI 137 暴露的后台资源停止缺口已修复并有新版独立复验，参数与宿主声明目录随后独立发布验收，第 3 步本轮框架范围收口；真实插件装卸与业务权限仍待后续实现。

- 同一前台请求和后台工作片跨 Compact 保留宿主的精确拒绝列表，修复重建运行参数后再次询问已拒绝调用的问题。
  与子代理 Goal/Compact 接续共用运行参数链，不新增持久表、不提升批准、不改变控制语义。
  529 项相关定向及本地严格 gate 通过；当前为本地候选，双机安装版复验尚未完成。

- 请求适配已独立上下文、绑定、历史和输入渲染，后台从会话领域共享完整历史行；原锁、持久路径和提交顺序不变。
  请求执行文件 3,630→1,182 行，110 个定义/常量逻辑一致、666 项定向通过；新版双 TUI 暂停、压缩、重连、
  70 秒原程序续采和双子代理插话路径通过。报告初次遗漏合计、错误心算和未完全修正的间隔范围保持失败记录。

- 请求编排已分离流式缓冲、事件投影及审批组件，恢复直接沿原 chunk 地址发布终态。
  前后台 Compact 携带共用纯计算，释放和提交时机保持；定向 510 passed、3 skipped、3 xfailed，
  严格尺寸 hard=0、基线不变。真实双 TUI 的子代理/插话、审批缓存、压缩重连已验；暂停后工具仍写入的
  失败已沿公共进程树终止修复并复验：暂停静止、压缩重连和真实程序续采通过，另一会话未中断。
  强制终止的 native 信封缺口保留为边界；请求适配后续拆分见上项，下一批进入存储组合。

- 后台单片执行、历史提交与交付已经分别独立；调度器保留准入、唤醒确认和退避。
  交付沿原顺序外发、canonical 提交及整封冻结，状态在原抑制位置实时读取；没有新队列或旧入口转发。
  四路真实 TUI 已覆盖返回、停止续做和 Goal 压缩重连；路径和时间戳产物错误按失败留证。
- 同任务新请求的归档先使用绑定后的 canonical run，避免 workspace 与收尾身份冲突而残留 RUNNING。
  request 保持当前消息编号；准备目录失败只结算本次新 attempt，既有 CAS、权限和未知副作用门不变。

- 后台工具策略已从 `runtime.py` 独立到纯计算模块，目录、owner/task 收紧及投递限制语义保持。
- 后台 Goal 状态处理与地址选择分别归 `background_goal.py`、`background_routing.py`；原事务、读取时机和租约顺序保持，删除混合职责 GoalMixin。353 项相关回归通过，新安装版实际 TUI 尚待验。
  持续 Goal 自动续跑新增结构化连续空片熔断：默认 3 片，配置 0 不限；到限沿既有 paused/`/goal resume` 路径恢复，reason code 与提示入原 host notice 队列。
  wake 基线与每片工具账只读，不解析回复正文；详见 `docs/design/THREAD_GOAL_LIFECYCLE.md`。定向测试通过，真实 MiniMax 与 TUI/IM 收件仍未验证。
  现有后台运行、观察、进程测试及独立导入边界共 `201 passed`；真实多 TUI 长任务矩阵已留证，边界见可维护性评估。
- 后续上下文与历史种子拆分保留同一 canonical 历史、任务范围和失败合同；不迁移执行权、锁及投递事务。
  当前候选真实双子代理返回与插话通过，Goal 暂停、压缩、重连后恢复到 8 条唯一记录，平方合计 204。
  同轮发现首次 `/goal` 丢 TUI 工作目录，已沿控制回执与共用校验修复；355 项定向通过。
  新 TUI 的实际 pwd、线程 cwd 和产物一致；启动后替换为空目录外部链接，在创建 Goal 前被拒绝，无外部写入。

## 当前事实源

单 Gateway 管理请求、会话控制、调度与交付；多个 TUI 是独立客户端。owner/thread/task/run/attempt 必须保持显式，展示与扫描索引不拥有执行权。

## 已落地约束

- 真实多 owner TUI 补验发现第二层标识遮蔽仍改写路径中的 owner/request；已让标识投影使用同一
  私有通道事实并保留路径，独立编号继续遮蔽。最终回复/流式/历史与外部通道分别验证。

- 本地队列前台消息按宿主 `cli_chat/cli_gateway` 来源选择私有展示通道；自定义 owner provider
  继续只负责身份，不再导致完整绝对路径被砍成文件名。流式、final、repair 和落账 channel 同源，
  历史回放不二次缩短路径；HTTP/IM 和未知来源保持原脱敏，rich 开关不扩大可见性。

- 停止结果不投递空回复，但保留已经发生的原生工具往返；模型/工具循环抛错也先通过精确会话回调
  提交历史，再返回原错误。空正文异常沿正常 final/repair 去重，结果未知不能假称成功或已压缩。
  前台正常/慢模型及子代理真实 TUI 已验证 5/7/10 组中断前工具往返进入下一轮投影。
  后台静默工作片补齐 native 写入；公开 final/投递重试按独立宿主回合去重，不补造旧版本缺失历史。

- 旧会话未配置模型仅冷却仍会反复报错；已改为 typed 配置依赖等待，当前会话选择可用模型后恢复。
  普通错误保持原 30 秒单调时钟冷却，健康会话不受阻。退避独立、有界、线程安全，原持久事件和 Goal 不变。
  后端构造与恢复检查复用同一缺配置判据；384 项联合定向通过，真实 Goal 菜单选模型后原任务自动完成。
  原有 Goal 错误收口不再消费配置依赖的 wake；其它错误与显式暂停仍保持原边界。

- 主会话后台命令完成通知进入原 wake 队列；owner 硬事实发现覆盖未发布记录，Gateway 重启可补发。
  活动回合消费同一事件，不另起主代理。自然收尾补报共用队列和 claim 准入校验，回执未落盘先等待。
  新版真实 TUI 首轮结束后收到约 91 秒采样退出通知，自动读取日志并公开汇报；配置可关闭。
- 长后台片已接收完子代理结果后，开始时冻结的 child phase 不再抑制 final 或要求无事件的额外轮次；
  当前子树、未读邮箱和 Goal 状态仍生效。定向竞态覆盖，新双子代理 TUI 有最终回复和 completed 记录。

- 客户端等待补修：系统时间前跳导致 TUI 误报等待超时、后台却继续执行。轮询入口将 deadline 转为单调时钟，
  活动续租也使用同一时钟；前跳、回拨、真实无活动超时及队列租约定向验证通过。
  正常模型真实 TUI 注入客户端一小时校时跳变后，七轮任务自然完成并收到唯一终态；未修改主机时钟。

- 新中断语义及后台插话已过真实 TUI：主代理 `/interrupt` 仅中断当前轮，active Goal
  保留原任务身份并使用现有去重 wake；`/stop` 才暂停目标并回收子树。普通聊天不暗中恢复暂停目标。
  新 attempt 与关联变更使用同一转换锁；旧取消结论不能覆盖新一轮，绑定冲突不伪称历史损坏。
  后台普通输入不再只认 processing 请求；精确 running claim 可沿原 guidance/输入回执消费，裸活动链接仍不授予插话权。
  暂停后合法恢复在 attempt 换代事务重开 TaskRun，历史关闭记录保留，避免最终完成仍挂旧 cancelled。
  后台模型输入编号与原 task 一致，回执真实消费后撤下等待提示；先后完成的历史目标不触发冲突迁移。
  六路增量验收发现子代理完成信封把该 Goal task 编号当普通 user request 查询；现先验证 Goal 归属，
  排除不对应用户消息的持久编号，混合普通请求保持严格历史校验。原现场和第二个新目标真实复验已过。
  追加任务的主代理追问又暴露新消息编号误作执行 run；现主入口回填真实 run/attempt，保留消息 ID。
  真实权限门、Compact 再入、旧代次拒绝定向已过；原会话恢复 14 项测试通过，另一次等待两名孩子时
  追问可执行真实命令且孩子继续运行，未放宽权限门。

- 前后台使用同一 canonical 历史，展示摘要的 recent limit 不裁掉模型历史。
- 后台答复先落权威会话记录；模型 provider 名不能冒充消息通道。
- 请求重复、控制 outbox、父级唤醒和交付回执使用稳定身份去重。
- 就绪检查、监听地址与鉴权分开；后台扫描不能阻塞客户端输入线程。
- 模型统计通过原有有序事件和会话活动快照投影；主/子独立保存最近数字，渲染和逐 token 到达都不扫描用量文件。
- 正常回合、持久目标与依赖等待使用不同结构化语义，不因临时沉默自动宣告完成。
- 持续 Goal 的安全续跑不依赖工具次数或 Todo，统一使用去重 wake；每个命名目标有精确任务身份。
  目标回合快照区分兄弟目标，前后台 Store 共用目标时钟。单目标真实完成，多目标最终表现继续复验。
- 未配置模型时 Gateway 仍可承载设置与历史；生成入口返回 `MODEL_NOT_CONFIGURED` 并提示 `/model`，不选择占位或离线模型。`gateway_status` 只投影当前调用方模型，不再向模型展示另一份启动默认模型。

## 未关闭

执行器无结果退出、恢复积压公平性、旧目标冲突与小时级慢模型组合仍需修复或验收，见 [全局状态](../../../STATUS.md)。不要将进程存活等同于每条 runner 存活。

## 修改入口与验证

结构见 [04-structure](04-structure.md)。改生命周期或恢复时，检查运行账本、队列、wake 回执和 TUI 可见终态。最终验收按 [TESTS](../../../TESTS.md) 走真实 TUI；生产用户会话、秘密配置及私有日志不纳入仓库。

首次自动Compact已本地接公共完整请求准备：原会话加载暂缓提交，PromptBuilder冻结后先压缩再自动选模。手动Compact在车道内按全线程来源判空，回执只报告历史估算。组合验收见TESTS，真实供应商与完整IR边界仍待验。

首次恢复宿主只在请求带有会话来源时安装（2026-09-23 修复）：未绑定 thread 的 ask 此前会在 `compact_source` 为空时抛 AttributeError，导致无会话请求全部失败，Gateway 场景测试因此失败。现在与 overflow 入口共用同一判定，没有来源就不安装宿主；入站附件无效的 `INPUT_MEDIA_INVALID` 也已登记到唯一错误合同。

外层typed overflow已接同宿主原生IR carry；真实循环释放未提交插话，Gateway按ID过滤后交下一次完整准备，原ToolCall身份和完整正文保留。恢复权限和模型前缀重新准备；无可压来源显式拒绝。此片隔离联验中，实际安装版与供应商证据仍待补。

<!-- 媒体来源片 3adb61904 的既有记录；不代表当前 Compact 集成已验。 -->
TUI 媒体请求已接通：input_media refs 与 ask 执行选项及幂等指纹同行，worker 在 owner 解析后验证路径/大小，再进入原 native user history。官网 M3 图片、视频与重连续问通过；官网 M2.7 100 请求/50 槽全部完成。详细资源口径见 TESTS。
- 客户端错误文案新增 `COMPACT_VISION_SUMMARY_FAILED`（随图摘要本次失败，下一次压缩自动改走归档引用，不必换模型），先于通用 `COMPACT_` 前缀匹配；见 [媒体压缩策略](../../design/COMPACT_MEDIA_POLICY.md)。
- 2026-09-24 深夜：`my-agent status` 与 `gateway status` 在 Gateway 启动写进 state.json 的 `unidentified_stale_attempts` 大于 0 时出一行提醒并指向 `runtime-stale-attempts`；只读投影，不查库、不结清。同批：预检与 `_automatic_noop` 的上下文估算按已知图块数 × `input_media_token_reserve` 加预留，多图上下文不再被低估（见[媒体压缩策略](../../design/COMPACT_MEDIA_POLICY.md)）。
- 2026-09-25：审批链新增 owner 长期授权——工具 runtime policy 用 `ApprovalPolicy.owner_grant_parameters` 声明"可长期允许的一类操作"，审批请求 binding 带 `grant_key`，面板多出 `approved_owner`；`StreamApproval` 先经宿主提供者核对 owner `operation_grants`（已授权直接 `permission_resolved`，不发面板），用户选长期允许时由 `request_execution` 注入的 `grant_recorder` 写 owner `tool_policy.json`。首个使用方是后台服务 `background_listen_scope=lan`。
- 2026-09-24 深夜：Gateway 停机收尾在模型调用结清之后只读列出 owner 后台会话权威目录里仍未终态的受管进程（`gateway_parts/background_sessions.py`），写事件 `gateway_background_sessions_surviving`（条数 + session_id/status/uptime/command/监听范围事实），收尾后把条数并进 state.json 的 `surviving_background_sessions`；`my-agent status` 在 Gateway 未运行时显示 `background_sessions_after_stop=N`。这些进程按设计跨 Gateway 存活，停止仍走显式控制，不在停机时杀进程；监听地址边界（是否只允许 loopback）待用户拍板，见 DESIGN_LEDGER。
- 2026-09-25：实验授权回执 `request_experiment._receipt` 的可选字段改为显式关键字参数（`authorization_id`、`code`，非空才写入），回执形状不变。原先的 `**fields` 违反架构守卫 `test_product_code_has_no_var_keyword_service_interfaces`，是全仓回归发现的。
- 2026-09-25：插件面板服务 `plugin_display_service` 按配置 `plugin_process_sandbox` 给面板连接的插件客户端带上同一沙箱开关（与业务连接、启用候选一致），默认关时行为不变；见[插件进程 OS 沙箱](../../design/PLUGIN_PROCESS_SANDBOX.md)。
- 2026-09-25：Gateway 状态文件 `gateway_state.json` 与 `/status` 新增结构化 `runtime_prefix`（进程 `sys.prefix`），供 TUI 判断自己是否与 Gateway 同一安装并在空闲时自动重启到同版客户端；见 [TUI 交互规范](../../design/TUI_DESIGN.md)。发布工具切完 Gateway 后会检测并重启同机 IM 适配器守护进程（仓库外 claude-tools）。

## 2026-09-25 能力包调度与长任务（本地组件阶段）

定时任务的 skill_refs 现在保留包类型、包 ID、摘要与激活代次；后台启动前必须全部匹配。
主会话首次读取能力包，在原 ThreadTaskLink.skill_snapshot_refs 固定版本；重读幂等，原锁内并发合并。
新轮验证已用版本并保持发现其他包，升级或重装不会静默替换旧任务；普通临时轮继续使用逐轮快照。
本地引用／调度／会话／能力授予七文件114项组合通过，真实原生 TUI 与发布尚未开始。
详见[能力包 Goal](../../tasks/CAPABILITY_INTERNALIZATION_GOAL.md)。

- 2026-09-26：owner 唤醒发现 `owner_wake_discovery._has_pending_memory_curator_work` 的失败退避改为与 Curator 自身同源的 `curator_failure_retry_seconds`。普通失败仍是 300 秒；`CURATOR_MODEL_NOT_CONFIGURED`（owner 没选模型）等一小时，发现层不再按维护周期反复种回登记表、重建 owner 实例（真机：两个未选模型的 owner 每约 7 分钟失败一次）。见 [memory 进度](../memory/02-progress.md)。
- 2026-09-27：`/settings` 与 `/settings all` 的总览新增“配置告警 N 条”：把 `AgentConfig.config_warnings` 与 `CapabilityConfig.config_warnings` 合并列出，每条注明来源（agent 主配置 / capability 配置），N 为 0 时不显示这一行。此前两个来源都没有展示点，参数减量忽略掉的已删/未知键用户看不到。取告警只读字段，不解析消息文字；memory doctor 的 `memory_config_warnings` 出口未动。
- 2026-09-27 后续两项同批：①`settings/services/_normalize.py` 新增 `describe_raw_value(key, value)`，告警回显配置值统一走它——凭据键不回显（写“已隐藏”），其他键截到 80 字符并标“已截断”，覆盖原 6 处 `got {value!r}`；②`_config_warning_lines` 改成先压平两个来源再过滤空项（原实现是 for→for→if 三层嵌套，违反“新代码不超过两层”的项目约定，code-size 报 `nesting:...:_config_warning_lines`）。
- 2026-09-28：凭据类字符串配置补类型校验（T3 真实 TUI 验收发现写错类型时没有任何告警：`embedding_api_key` 写成列表会原样进入运行配置，`feishu_app_secret` 写成列表会被静默变成空串）。
  - `settings/services/_normalize.py` 新增 `CredentialFieldsService`，排在归一服务最前面。
  - 字段名单由 `credential_string_fields(AgentConfig)` 从配置类声明推出：键名是凭据且声明为 `str` 的字段，新增凭据字段自动纳入。
  - 统一口径：
    - 字符串原样保留；
    - 整数沿用“纯数字没加引号也按字符串还原”；
    - `None` 与留空读成的 `[]` 按没填取默认值，不告警；
    - 其它类型告警 `<键>: expected a string, got 已隐藏（凭据不显示原值）; using default`，并回落默认值。
  - `/settings` 总览的“配置告警 N 条”因此会列出这些键，原值不出现。
- 2026-09-28：插件命令执行前被拒（rejected）时，回执在中文说明后另起一行“错误码：X”（如普通用户执行仅管理员子命令得到
  `PLUGIN_PERMISSION_DENIED`），Gateway 普通回执与交互命令流两条路径都按 WARNING 记 `PLUGIN_COMMAND_REJECTED error_code=… action=…
  request_id=…`。此前服务层已有码，但只转成中文说明、TUI 只打印说明、Gateway 不记日志，面板与日志都看不到码（R16 实测）。
  管理员执行行为与执行后的失败/成功文案不变；合同测试见 test_plugin_management、test_gateway_plugin_management、test_host_command_stream。
- 2026-10-02（C14 后续，ds1，分支 `worker/ds1-c14-followups`）：非 Python 插件 enable 的确认预览回执不再是“被拒”，
  `plugin_command_service._log_rejection` 对 `details.reason == "confirmation_required"` 不再追加“错误码：X”文本；
  回执带自己的状态 `state=confirmation_required`，error_taxonomy 登记 `PLUGIN_CONFIRMATION_REQUIRED`
  （category=state、retryable=True、RecoveryAction.REQUEST_USER_INPUT），不按文案或参数错误处理。
  TUI（plugin_http_response）与 IM（execute_plugin_control）同经 `_log_rejection`，两入口一致；
  普通被拒（如权限）仍按 2026-09-28 口径追加错误码。普通用户看到的管理动作文案也改为结构化区分：
  权限不足标“（仅管理员可用）”，真正未开放的动作仍标“（尚未开放）”，见 04-structure 的 C10 段。


## gateway stop 列出遗留后台进程，并可显式一并停止（2026-09-28，分支 `my-agent/self-dev-4`）

托管后台进程不随 Gateway 退出而停止（生产部署依赖这一点），但原先既看不到它们、也没有回收入口：
回合被 /interrupt 后后台进程继续运行，用户再 /stop 只会得到"当前没有运行中的内容"。
`gateway stop` 现在默认按结构化事实列出本 gateway 登记且仍在运行的后台进程（所属 root task/run、pid、启动时间、状态），
**默认不停止**；加 `--stop-background` 才走既有资源停止路径（`ProcessSessionStore.request_stop` 冻结停止意图，
原 host 的 `terminate_process_tree` 按进程组回收，孙进程随之结束——只给登记 pid 发信号会漏掉它们），
`--background-timeout` 控制等待确认的秒数。停止失败按非零退出码如实报告，不谎报已停。
重启（`gateway restart`）不隐式停止后台资源，只有用户显式要求才停。本地六项合同单测通过，真机未复验。

## 会话内 /stop 回收被中断任务的后台资源（2026-09-28，分支 `my-agent/self-dev-4`）

回合被 /interrupt 后托管后台进程按设计继续运行，但原先同会话再执行 /stop 只会得到"当前没有运行中的内容"，
用户没有入口回收。现在 `/stop` 在**没有运行中回合**时会再退一步：按本会话精确 thread_id 从受管登记表里
筛出仍在运行的资源并停止。选扩展 `/stop` 而不是新命令的理由是——用户第一反应就是 /stop，新命令要求用户
先知道"有遗留资源"；两条路径（`/stop` 与 `gateway stop --stop-background`）复用同一实现，不新增第二套停止逻辑。
没有登记仍如实回"没有运行中的内容"；登记表不可读或停止未确认时报 `TASK_RESOURCE_STOP_UNCONFIRMED`。
本地四文件通过，真机未复验。

## 派发线程不再被错误打印杀死，心跳与 /status 暴露派发存活（2026-09-28，分支 `claude/38-gateway-dispatcher-resilience`，基于 `c101d325a`）

生产 step15t 从 15:11 启动后一个前台请求都没处理：15:13 磁盘写满时，tick 里某段出错走到 `_print_gateway_loop_error`，
它直接 `print` 到 stderr 没有任何保护，打印本身抛 OSError 逃出 except 块；`_gateway_request_loop` 的 while 外没有 try，
派发线程静默退出，连死因都没能写进日志，之后 pending 一直涨，`/status` 只看得到 pending 数。
- `_print_gateway_loop_error` 现在绝不抛：打印失败只在进程内账本 `gateway_parts/loop_health.py` 记一笔（次数、上下文、错误类型），
  不做 IO。这个入口被所有后台循环共用，心跳、后台主循环的错误打印同样受益。
- 派发循环每拍走 `_guarded_dispatch_tick`：从 tick 逃逸的任何 Exception 记账本、打印，按"本轮没派发"继续；派发者构造/关闭失败与
  BaseException 逃逸经 `_record_request_loop_exit` 留下退出事实（账本 + 打印 + `gateway_request_loop_exited` 事件），
  Exception 按原语义吞掉返回，BaseException 原样上抛。
- 心跳载荷与 `/status` 响应并入账本快照：`dispatcher_alive`、`dispatcher_state`（not_started/running/vanished/exited）、
  `last_dispatch_tick_at`、`last_dispatch_tick_started_at`、`dispatch_tick_count`/`dispatch_tick_errors`、`dispatcher_exit_error`、
  `loop_error_print_failures` 等。`/status` 直接读内存，磁盘写满时也能看出"有 pending 但派发已死或卡住"。
- 扫描门 `GatewayInboxScanGate` 改在扫描**之前**取样 inbox mtime，并以它为"已见过"的基线；扫描期间目录再变就强制下一轮重扫。
  原来在扫描结束时取样：请求先写 .tmp 再 rename，rename 落在 glob 之后、record_scan 之前时被记成已见过，负载高、一轮超过
  2 秒粗粒度保护窗口时该文件被永久跳过。
- 合同测试 `test_gateway_dispatcher_resilience.py`：注入 ENOSPC 的 stderr、段内/段外异常、卡住后抛 SystemExit 的 tick、glob 之后的
  rename，全部确定性构造。真机未复验，部署后在生产心跳里核对这些字段。
- 同一事故的第二个受害者是飞书适配器：`cli/adapter.py` 每 5 秒写一次 `adapter_state.json`，15:13 写入撞 ENOSPC，异常没人接，
  进程整个退出，状态文件却还写着 running。现在周期写入走 `_write_adapter_state_guarded`：OSError 只记进程内账（次数、最近错误）并打
  WARNING，下一轮重试，恢复后把 `state_write_failures`/`last_state_write_error` 写进状态文件；状态文件改为原子写，失败不留半截 JSON。
  启动/等待阶段任何异常逃出都先 `_finish_adapter_process`（停适配器 → 删 pid 文件 → 状态写 failed 带 reason/error）再上抛；
  状态写不进去时 pid 文件已经没了（删文件不占空间），另记 `adapter_state_write_failed_at_exit` ERROR 日志。
- `/status` 新增适配器事实（`channel_health.adapter_process_facts`）：`adapter_alive` 只按 `adapter.pid` 记录对应的进程是否真活着
  判定（含启动指纹防 PID 复用），状态文件只给 `adapter_state`/`adapter_state_updated_at`/`adapter_state_stale`；读取失败单列
  `adapter_state_error`/`adapter_pid_error`，只读、不清理陈旧 pid 文件。合同测试 `test_adapter_state_write_resilience.py`。
- 9a 复审跟进（同分支第二个提交）：tick 持续出错时原来每 0.2 秒打一条 `[gateway-loop-error]`（stdout 追加进 paths.log，一天两百多 MB），
  正好能把刚腾出的磁盘再写满。派发循环与后台主循环共用 `cli/gateway_loop_backoff.LoopErrorBackoff`：连续出错等
  min(0.2·2^k, 30) 秒、成功一次清零；同种错误（context + 异常类型）只打第 1 次、之后每 10 次打 1 次，次数仍全部记进
  `dispatch_tick_errors`。其余：`dispatcher_alive` 改为保存线程对象用 `is_alive()`（ident 在 macOS 上会立即复用）；
  `dispatcher.shutdown()` 抛错记成 `gateway_request_pool.shutdown` 阶段；`/status` 的 `adapter_pid_error`/`adapter_state_error`
  改为结构化子集（category、context，不带路径）；账本与适配器状态里的错误消息先过 `redact_sensitive_text`。
  9a 建议的 hdiutil 小磁盘镜像写满真机复现留作后续验收。

## 后台循环退避覆盖到全部循环，包装异常按原因链归类（2026-09-29，分支 `claude/38-loop-backoff-everywhere`，基于 `64f7ee64e`）

ENOSPC 真机验收（releases/step16e-50651d81/enospc-acceptance/）里 scheduler_due 的 tick 在磁盘写满时打了一条 `SchedulerDueIndexError`
且被归为 programmer_bug；盘点发现 `LoopErrorBackoff` 只接了派发循环与后台主循环。本轮：
- **盘点**（见 04-structure 的表）：维护循环、调度器到期循环、孤儿恢复循环改走 `_run_loop_with_backoff`（成功等 interval 并清零退避，
  出错记账、限流打印、等 `max(interval, 退避)`——退避只能放慢，不能把 60 秒一次的维护变成 0.2 秒重试）；心跳循环记账、限流但**不退避**
  （节奏本身就是限流，退避只会把"磁盘腾出后重新被看见"推迟到 30 秒）；派发 tick 的四个段（派发/恢复/终态投影/输入回执调和）的段内错误
  也进同一份账本与限流，但只有派发段失败（没请求可派时）让循环退避；恢复/终态投影/输入回执调和三个后台修复段各自一份退避，失败只推迟
  自己的下次到期（`max(0.75, 该段退避)`、`_RecoverThrottle.defer`），派发轮询保持 0.2 秒——9a 复审指出一张坏回执不能让新消息等 30 秒才被认领。请求租约心跳、supervisor 进程、后台主循环内的 owner 车道不接（各有自己的
  停机/冷却/一次性语义）。
- **账本**：`loop_health.note_loop_error(loop, context, exc)` 按 loop id 计数，快照新增 `loop_error_counts`、`last_loop_errors`；
  `dispatch_tick_errors`/`last_dispatch_tick_error` 现在只投影派发循环（9a 之前指出的"后台错误混进派发计数"就此拆开）。
- **归因**：`runtime_error_report` 对不认识的包装异常只沿显式 `__cause__` 找环境类根因（`OSError`、`sqlite3.OperationalError`），
  **不改 category**（仍是 programmer_bug，因为 category 参与取消工具等控制流），另加 `cause_type`/`cause_category` 两个字段，账本记录
  （`loop_health._error_record`）同样带上；只看类型，不看消息文本，不特判 errno；不沿 `__context__`（except/finally 里顺带发生的
  KeyError/AttributeError 会误归环境）。`SchedulerDueIndexError` 本来就 `raise ... from exc`，没改 scheduler。顺手把
  `cancel._explicit_target_is_absent` 收紧到它注释里的契约：只认 `error_type == "FileNotFoundError"`（原来 `category == "io"` 会把
  PermissionError、被包装的 sqlite 锁定判成"run 不存在"）。
- 合同测试 `test_gateway_loop_backoff_coverage.py`（9 条，确定性构造，含"库文件路径是目录"的真实 sqlite 环境错误）。真机未复验。

## 插话幂等回执指纹兼容旧口径，坏账回执不再被静默重写（2026-09-29，分支 `claude/38-guidance-receipt-digest-compat`，基于 `bac2f176d`）

生产实锤（ae 核对 step16g 时发现）：input_receipts 里一条 09-27 发给已结束定时 run 的插话回执 state=terminal_unknown，
reconcile_error 是 DataCorruptionError「input digest mismatch」，每 15 秒被 reconcile_gateway_input_receipts 重写一次，不进任何计数。
根因：a646a4885 在 `_guidance_input_digest` 里加了 `canonical_metadata.pop("expected_turn_id")`，改了已落盘指纹的口径却没兼容旧回执；
插话的元数据一定带 expected_turn_id，所以之前写的幂等回执被 `_validate_guidance_once_receipt` 重算时全部对不上。
- **指纹分版本**：`GUIDANCE_INPUT_DIGEST_VERSION = 2`；`_guidance_input_digest(request, version=…)` 保留 v1（只去 dedupe_key）与 v2
  （再去 expected_turn_id）两种计算；回执新增 `input_digest_version`（新写的记 2）。校验统一走 `guidance_receipt_input_matches`：记了版本
  按版本严格重算；没记版本的旧回执（a646a4885 前后各写过一种口径）两种任一匹配即视为同一条。`append_once` 的同键重试也走它。
- **对账**：`_write_terminal_unknown` 在状态已是 terminal_unknown 且错误的结构化字段（error_type/category/context）没变时不重写；
  带对账错误的终态未知计入 summary 的 `terminal_unknown_errors` 并记 `last_terminal_unknown_error`，派发者的对账段经
  `raise_if_input_reconcile_unsettled` 抛 `GatewayInputReconcileUnsettledError`（category 取持久化错误的类别），进 loop_health 计数与限流打印，
  只推迟对账段自己的下次到期。
- **收敛**：修好部署后那条回执不用手工改：旧口径校验通过 → 目标任务已终态 → guidance 被拒绝 → 回执转排队并进 inbox（既有语义；
  生产上那条 09-27 的旧回执由集成者在部署前挪到备份目录，不让两天前的指令迟到送达，见 DESIGN_LEDGER 同日裁定）。`test_guidance_receipt_digest_compat.py` 用冻结的旧算法写真实形状回执证明这一点。
- **规矩**：持久化指纹改口径必须加版本号并保留旧版本的计算，不能原地改；固定样本的指纹十六进制钉在测试里。

## 会话互通取消：停止后台派活回合 + 消息唤醒不再空转（2026-09-28，分支 `my-agent/self-dev-3`）

`control_service.execute_gateway_conversation_control` 的 `stop` 分派新增一条后台回合停止路径：
前台窗口请求集合里找不到精确 `expected_turn_id` 时，改按**会话任务的绑定**去后台定位这一轮
（`task.conversation_request_id == expected_turn_id` **且** 目标会话后台认领仍在 `running`），
再打这一片自己注册的**专用可中断名** `session-task-turn:{turn_id}`（与前台 `conversation-request:` 不同空间）；
拿不到绑定、认领已不在跑，或有其它来源占着这个回合，都如实返回未确认（fail closed），不改任务状态、不伪造成功。

为什么要专用名：派活回合跑在后台片里，`task_id` 为空，原逻辑在 `run_claimed` 里退化成 `nullcontext()`
——**这一轮从来没注册过可中断名**，取消永远打不到东西。`task_id` 在 Skill 快照绑定/持久任务处有既定语义，
不得挪用（实测挪用会让整片因 `SKILL_TASK_BINDING_INVALID` 开不出来）。

同时补两处交付前的取消判定：`background_claim.run_with_heartbeat` 在 `run_once` 返回后再查一次本线程中断旗，把认领结算成 cancelled；
`runtime._run_agent` 在交付前按**持久的结构化事实**（该会话任务是否已 `cancelled`）丢弃答复——这一处**不能用本线程的 `is_interrupted()`**：
它是 per-thread 的，停止旗不在本线程时读不到。交付前的持久检查是**纵深防线**：常规停法有两道在它之前——
①停止旗立到本线程后，在途模型调用在等待关卡里看到旗就丢掉结果并抛中断
（`tool_model_generation._wait_for_generation_result`）；②`run_with_heartbeat` 的二次检查把认领结算成 cancelled。
（注：早先注释里的"嵌套注册退出会清旗"说法**不成立**，集成方用 60 段轨迹否掉了它。）
`run_with_heartbeat` 的二次检查只对**绑定了会话任务**的回合生效（`_host_delivery_bound_turn` 非空）：
普通后台运行能被普通 `/stop` 打到，对它套这道检查会丢掉已交付的答复、认领记成 cancelled。

消息唤醒：本片带上会话身份（回合号取唤醒自己的 `wake_signal_id`），消息在唤醒回合里被认领、确认，不会到下一轮再注入一遍；
目标忙时消息已在它自己的前台回合里被消费（回执为 `consumed`）的，领取后准入 `session_message_consumed` 不开回合，
并经 `retire_source` 把唤醒结案（2026-09-29 接手收尾时补上：原实现只结 claim，唤醒每拍被重新认领）；
`submitted`/`rejected`/`reserved` 都不算已消费，照常开回合把消息交给目标（ae 在真实链路里复现了被 /stop 后 `rejected` 的消息）。

回归：`test_session_task_real_chain.py` 的 `HOLD-FIRST` / `HOLD-AFTER-TOOL` 两个窗口，以及忙碌目标消费后断言唤醒已结案；
另加 `test_background_claim_interrupt_scope.py`（绑定回合 vs 未绑定普通后台的正向对照）、
`test_session_message_consumed_admission.py`（逐状态判据、读取抛异常放行、只在已消费时结案）。
2026-09-29 由 75 从 `my-agent/self-dev-3-cancel-final` 接手，压成干净提交落在分支 `claude/75-cancel-line-finish`。真机复验由集成方跟进。

## 环境级故障按车道暂停（2026-09-29，分支 `claude/9b-lane-env-pause`，基于 `89af6b07a`，唤醒毒丸第 3 步 C5）

- **一个权威**：`backends/errors.is_provider_environment_fault`：整数 HTTP 状态 401/402/403/404/407，或除 `ProviderRequestRejectedError`
  以外的 `ProviderConfigurationError`。状态码集合从 `wake_poison` 搬来，毒丸 `_is_uncounted` 改为引用它，判定逐字不变。
- **车道第三类**：`BackgroundLaneRetry` 增加 waiting_for_environment。分类顺序固定：先等模型配置（`ModelNotConfiguredError` 也命中环境判定，
  必须先走这条，配好模型立即恢复），再环境暂停，其余仍按 30 秒冷却（400/413 等请求本身的问题不变）。暂停到两件事之一：
  - `thread_model_fingerprint` 变了：暂停后第一次就绪检查记下基线，之后每次规划比较，变了立即放行并删掉暂停（之后再失败从 60 秒重新计）；
    失败到下一次规划之间发生的改动不算变化，只能等探测放行（最长 60 秒）。
  - 到了探测时刻：`LANE_ENVIRONMENT_PROBE_BASE_SECONDS = 60` 起，探测再失败翻倍，`LANE_ENVIRONMENT_PROBE_MAX_SECONDS = 900` 封顶（内部常量）；
    到点放行一次真实尝试，暂停记录留到探测有结果，成功由 `succeeded` 清除。
  指纹在锁外读，锁内只推进同一条记录（读指纹期间被成功清除或换成新失败，就不写回旧记录）。取指纹出错（OSError、DataCorruptionError、
  ModelProfileError 等任何 Exception）按未知处理，只按探测时刻放行，暂停和翻倍保持，不再经 Gateway 记成新失败把暂停冲掉
  （9a 复审建议 1，C5 补充三）。状态只在进程内，重启即清。
- **日志**：`[gateway-lane-retry]` 一行 JSON：`lane_environment_paused`（owner 标签、thread_id、`probe_in_seconds`、异常类名、状态码）与
  `lane_environment_resumed`（reason 为 `model_fingerprint_changed` 或 `probe_succeeded`）；不含异常正文或配置，打印失败只记 `loop_health`。
- **指纹**：`thread_model_fingerprint(agent, thread_id)` 直接 `threads.load`，不经会给空引用迁移写库的 `thread_model_profile_id`；由
  model_profile_id、model_selection_revision 和所选模型的连接字段（协议、模型名、接口地址、密钥、密钥环境变量名、请求头、会话头、登录引用）
  组成，用 `decision_policy.connection_revision` 的进程盐 HMAC 摘要，不落盘、不打印、不构造后端、不联网。空引用按 owner 默认推算；
  模型引用失效（删除、停用、撤销共享）记作不可用，指纹随之变化。改 owner 的新会话默认值不影响已选模型的旧会话。
- **额度耗尽核对（结论，没改代码）**：`ProviderQuotaExhaustedError` 不会被两套进程内机制重复处理。供应退避
  `_absorb_provider_supply_failure` 只吸收 `is_provider_transient_error`（含 `ProviderUsageLimitError`），额度耗尽原样抛出、不记账；
  唤醒路径由 `_run_wake_signal` 就地转成额度通知（Goal 转 usage_limited，未送达的只重投通知、不再调模型），tick 正常返回，车道记成功；
  观察路径抛到 `_safe_thread_tick`，车道按 30 秒普通冷却（不是环境暂停）；策略路径先在 `run_with_heartbeat` 记持久策略失败账（300 秒起、
  连续 3 次退休），再抛到车道 30 秒冷却——这是持久策略账与进程内车道两层，不是供应退避加车道冷却。
  跟 9a 改判相邻的影响：每周额度用完的 429 以前是 `ProviderUsageLimitError`，三条路径都走供应退避（30→900 秒）、策略不记失败；改判成额度耗尽后，
  唤醒路径改走额度通知（预期），观察路径变成车道每 30 秒固定重试一次注定失败的模型请求（直到额度重置），策略路径开始记失败账、3 次后退休。
  **3a 裁定（2026-09-29）并入车道暂停**：车道分类改为 `is_provider_environment_fault(exc) or is_provider_quota_exhausted_error(exc)`，
  额度只在车道这层显式加，共享判定 `is_provider_environment_fault` 不变（毒丸那边额度本来就按瞬时类不计数）。观察和策略路径遇到额度用完
  也按车道暂停（60→900 秒探测，换模型立即放行），不再每 30 秒空打一次；唤醒路径仍就地转额度通知，不受影响。
  **额度共用判定（2026-09-29，9a 复审 be 的压缩额度修复后补充，分支 `claude/be-quota-predicate`）**：大线程请求前先压缩，压缩调用撞额度时
  抛的是 `ConversationCompactError`（`COMPACT_PROVIDER_QUOTA_EXHAUSTED`），车道分类、策略失败账和毒丸原来都用 isinstance，认不出。
  现在四处（含唤醒额度分路）都只读 `conversation/compact_guard.is_provider_quota_failure`；后端层的 isinstance 判定不变。
- **策略失败账不记环境级故障（3a 裁定，C5 补充二）**：现状核实（草稿探针）——401/407/连接失败/额度用完都原样穿过真实后台回合，
  每次记进持久策略失败账（`failure_count` 加 1、退避约 300 秒），连续 3 次策略退休。改为 `background_claim._counts_as_policy_failure`：
  供应瞬时、配置暂缺、环境级故障与额度用完都不记，不因此退休；重试节奏交给车道暂停。普通程序错误 3 次退休不变。只改记账条件，
  不动持久账 schema。
- 测试与变异见 TESTS.md 同名节。

会话消息去重键按单条消息区分（分支 `claude/75-session-message-dedupe`）：键 = 会话对 + 这次发送的身份（模型工具的 `__operation_id`、
`/tell` 每次新生成），唤醒 metadata 的 `message_dedupe_key` 带上它，"已消费"判据按它查回执；同一对会话连发多条各自入队、各自送达，
工具结果返回回执真实状态。

会话消息在目标回合没消费就结束（/stop、报错、崩溃）时释放给下一回合（方案 A）：`reject_pending` 的唯一收尾规则
`settle_unconsumed_receipt` 对会话消息改为释放回 pending 并记 `released_turn_ids`，下一回合认领时改绑到自己名下；
释放满 5 次后转 rejected（`SESSION_MESSAGE_RELEASE_LIMIT_REACHED`），领取后准入按来源已处理完结案唤醒；submitted、插话、派活正文行为不变。

后台片没有正常结束时同样收尾（`runtime._settle_unconsumed_background_turn_input`）：定时 run、派活、会话消息唤醒这三种自带精确
回合号的片，模型调用失败、取消或中断时对本片回合号 `reject_pending(reject_reserved=True)`，Compact 公平让出除外；此前消息唤醒
回合认领后失败，消息卡在 reserved。释放改为按次计数（回执 `migration.release_count`），同一条唤醒重跑（同一回合号）反复失败也会
到上限。（更正：123f6f3b4 另写的「同一回合重新认领时补回回合索引」不需要，读回执时的投影修复会补回，下一提交已还原。）

派活正文在后台片非取消的失败后退回：后台收尾按结构化任务状态传 `release_task_body`，任务没取消时派活正文退回 pending、
按同一个 `release_count` 计次，不授权跨回合，只有同一 `session_task_id` 的重跑能认领；任务已取消照旧 rejected，前台终态不变。
取消路径同步撤回：先失败、后取消时正文已退回 pending，`_apply_cancel` 推进到 cancelled 后把它撤成 rejected，重试不再注入已取消任务的正文。
已取消派活任务的唤醒在开跑前结案（`_retire_cancelled_task_wake`），重试等待中被取消的任务不再重跑一片。
开跑前判定按唤醒自带的 `session_task_id` 直接读任务状态（R-a）：排队中就取消、从未绑定回合的任务也在开跑前结案，不再开一个
没有正文的空派活回合、向目标会话交付答复。
派活正文只许它自己的派活回合认领（R-b）：目标正忙时前台回合不再在安全点领走派活正文，任务留在队列，前台结束后由派活唤醒开
派活回合、收口并回报发送方；派活回报（带 `SESSION_TASK_STATUS_FIELD`）不受影响。

释放只计会让回合崩溃的失败（3a 裁定 (a)）：`reject_pending(failure=...)` 按唤醒毒丸的 `verdict_for_error` 判定，超时、429、连接、
环境故障、/stop 与取消只释放不计次；前台 `_handle_gateway_request` 的收尾与后台片收尾都把回合抛出的异常交进来。


唤醒毒丸第 3 步 C2/C3 接线（分支 `claude/75-wake-poison-wiring`，step16m）：`run_claimed` 支持领取观察者（`begin`/`settled`），
唤醒车道按尝试写尝试账：计数失败按 30/60/120/240 秒持久退避、同因 5 次按 `failed_permanently` 结案（之后不再领取、换进程也生效），
瞬时/环境类不计数只退避；批次里有带账成员时逐条执行；进程死亡的在途尝试在跳过阶段补记；尝试账读不出按 `attempt:ledger_corrupt`
结案。在途记录带这一片的回合号，供 C6 收尾死进程认领的补充消息。

唤醒毒丸第 3 步 C4（同分支）：结案后派活任务收成 failed（`SESSION_TASK_WAKE_QUARANTINED`）并回报发送方；会话消息回执不动，只留注明
仍待投递的宿主提示（2026-09-29 3a 以做法 2 取代原裁定 b，消息去留只由回执层上限决定）；派活正文已放弃时准入先收任务再结案唤醒；
宿主提示按原因码合并。

派活回合的取消判定统一按任务号读（窄窗口，step16m）：开跑前、交付前、后台收尾用同一个判据，开跑后、第一次认领正文之前到的
取消也会在交付前丢弃答复、结案唤醒（那一次没有正文的模型调用是已知代价）；不再全量扫描会话任务。

## 维护状态说清「执行了没有」：只加字段、不改旧口径（2026-09-29，分支 `claude/9b-maintenance-apply-outcome`，基于 step16l `5ec2db2e0`）

- **起因**：step16i 首跑积压时，local/main 其实执行了 11742 个动作、失败 0。但 `maintenance.json` 记的是 `policy_unavailable`、
  `last_success_at` 为空，Gateway 摘要也把它算成 `failed`；同一轮的审计事件却写 `ok=true`、`errors=[]`。
  - 前两者的原因：R4 之后路径级错误只隔离重叠动作，而 `_maintenance_status` 只要有错误就记 `policy_unavailable`。
  - 审计的原因：它在 `execute_retention_plan` 里只看到隔离后剩下的可执行部分。
- **修法**（3a 裁定，持久化字段只加不改）：
  - `MemoryRetentionReport.isolated_errors`（末尾、带默认值）：`apply()` 用 `replace(_without_errored_subtrees(plan), isolated_errors=plan.errors)`
    把扫描期路径级错误随可执行计划带进执行器，合并回执时原样带出；整份拒绝、法律保留时为空。
  - 审计事件 `owner_retention_applied` 新增 `isolated_error_count` 与 `isolated_error_codes`（按码计数，不含路径）。
  - `maintenance.json` 新增：
    - `apply_outcome`（`applied` / `refused` / `legal_hold`，由 `_apply_outcome` 只看 `report.applied` 与 `legal_hold` 推出）；
    - `isolated_error_count`；
    - `last_applied_at`（最近一次 `applied`，被拒时沿用上次，旧文件按 0）。
    - `status` / `last_success_at` 不变。
  - `OwnerMaintenanceResult` 新增 `apply_outcome` / `isolated_error_count`，`not_due` 时 `to_dict` 与旧版逐字相同。
  - Gateway 摘要：`failed` 改为只算整次被拒与执行期动作失败，「执行了、只有隔离错误」不算（3a 据 9a 对 355 条的诊断补充裁定；
    摘要只打印、没有持久化读取方）；另加 `refused` 与 `isolated`。`OwnerMaintenanceResult` 同步带出 `failed_action_count`。
- **不改**：/status、TUI 不读维护状态，展示维护状况属于新功能，记台账待定。
- **计数口径**：`isolated_error_count` 是错误条数。解析不了（坏 JSON）的 `state.json` 会被 completed_task 与 tool_output 两个扫描器
  各报一次（既有行为），这时不等于子树数，按 (错误码, 路径) 去重待定；生产 local/main 那 355 条是能解析、只缺 status 的旧格式，
  只由 completed_task 报一次、路径不重复，上线后预期 `isolated_error_count` 约为 355。
- **同轮处理的两个 v1 策略 owner**（3a 裁定）：`93b8c3ffffb8`（local/users，测试名）用产品 `_migrate_retention_policy` 一次性迁移，
  原文件备份进证据目录；`ebd40e6fc3ec`（feishu/users，像真实用户）不动、交用户决定，状态修复上线后它每天会记为 `refused`。
- 38 复审跟进（同分支补充提交，纯抽取、语义不变）：代码尺寸身份比对新增了 3 条（`execute_retention_plan` 越 soft 线，
  `run_owner_retention_if_due` 与维护 `tick` 进 high-risk），分别抽出：
  - `retention_apply._apply_candidate_phase`；
  - `owner_maintenance._outcome_fields(retention, previous, current)`，算 status / last_success_at / 动作计数 / 三个新键；
  - `gateway_loops._maintenance_summary(reports, scanned)`。

  抽取后对比基线 `5ec2db2e0` 的身份比对：新增 0、消失 0。
- 测试与变异见 TESTS.md 同名节。

- 2026-09-29 唤醒毒丸第 4 步运维面（分支 `claude/be-wake-ops`，待复审）：管理员 `/wakes`、`/wakes replay <ID> [confirm]`（TUI 与飞书共用 Gateway 控制入口），`/status` 与 `gateway_status` 显示已结案唤醒条数，账本整理把满 14 天的结案留档移进 `quarantine/archive/`（发布语义不变，重放拒绝 `WAKE_REPLAY_ARCHIVED`）。测试与变异见 TESTS.md 同名节。

## TUI 上下文数字忽高忽低：持久校准指纹跨进程稳定（2026-09-30，分支 `claude/38-context-usage-flicker`，基于 `c80c8b5c2`）

- **起因**：用户长期会话的状态条每次部署后第一次调用偏高 25–29%，下一次调用落回；数据与根因见设计台账同名节。
- **改动**：`context_pressure._stable_context_surface_fingerprint` 的连接部分改由 `_tokenizer_connection_identity` 给出（模型档案、地址、
  鉴权方式），不再用进程加盐的 `connection_revision`；密钥与请求头不进指纹。
- **测试**：`test_runtime_context_pressure.py` 新增重启存活、分词身份两个用例，原“换密钥回到原始估算”断言改为继续校准。

## /effort 回执与 Responses 实际发送同一换算（2026-09-30，分支 `claude/38-effort-receipt`，基于 main `bf4f740c3`）

- **起因**：智能程度真实审计里，ChatGPT 订阅模型 `/effort off` 实际不发字段（目录没有任何模型声明 none / minimal），回执却写“请求时关闭思考”；
  旧档案没有 `reasoning_levels` 时 `/effort max` 实际发 high，回执写“最高”。
- **改动**：`control_service._render_effort` 与参数中心 `_reasoning_effect` 都改用 `reasoning_control.describe_config_reasoning_effect`，
  Responses 协议下回执由 `responses_reasoning_field` 的换算结果生成。
- 测试与变异见 TESTS.md 同名节。

## 上下文数字第二个来源：owner 级校准比值缓存（2026-09-30，分支 `claude/38-context-calibration-carry`，基于 main `bf4f740c3`）

- **起因**：新开的会话第一次调用显示 44,852、实际 35,806（偏高 25%）；线程校准只在同线程、同指纹、同代次时可用。
- **改动**：`context_pressure._provider_context_observation` 在本轮/线程观测都对不上时回落 `context_calibration_carry` 的 owner 比值；
  成功观测顺手更新；`compact_calibration` 新增 `owner_ratio` 作用域，只按比例折算。新增规范路径 `owner_context_calibration_json`
  （`home_layout` / `home_layout_v2` / `owner_resolver` 同步）。
- **开关**（同分支追加提交）：`memory_context_calibration_carry_enabled`（默认开，YAML 中文注释、`AgentConfig`、前端配置目录只插入这一项）；关掉时缓存不读不写。
- 测试与变异见 TESTS.md 同名节。

## 分段摘要来源不收 Responses 思考密文（2026-09-30，分支 `claude/38-compact-segment-strip-ciphertext`，基于 step16t `10041de02`）

- **起因**：3a 修好 GPT 回合内压缩（`c07823671`）后，分段摘要仍把每个助手轮约 3.6K 字符的思考密文编码进摘要文字。
- **改动**：`compact_message_source.summary_source_message` + `CompactMessageSource.projected`；`compact_request_budget._summarize_segments`
  两种来源工厂都经这一投影。整请求原协议发送路径不变。
- 测试与变异见 TESTS.md 同名节。

## 没有会话任务的请求，代理树结束后 TaskRun 也关闭（2026-09-30，分支 `claude/38-taskrun-close-without-conversation-runtime`，基于 main `a8c71f0e0`）

- **起因**：ae 真实模型验收 D3，从未升格成会话任务的请求 done 后 TaskRun 永远 created。
- **改动**：`runtime_mixin._settle_terminal_conversation_task_run` 在关联文件确实不存在时，也走同一个树终态 CAS 关闭
  （`reason=no_conversation_task`）；读坏、无存储、空身份、未终态关联保持开放；unknown attempt 仍挡住关闭。
- 测试与变异见 TESTS.md 同名节。

## /recover 能看到并处置本会话子代理留下的未知执行轮（2026-09-30，分支 `claude/38-recover-child-unknown`，基于 step16v `199c1933e`）

- **起因**：ae 真实模型验收 O1，被 SIGKILL 的子代理留下 unknown 执行轮，TaskRun 不关，`/recover` 只看根主代理，用户没有入口。
- **改动**：新增 `runtime_db/child_recovery.py`；`/recover` 主链不阻塞时列出本线程子代理，恰好一条才处置，之后做接替收口与
  TaskRun 收口（会话层 `conversation/task_run_closeout.settle_terminal_task_run`）。主链行为不变，没有自动处置；目标参数待定。
- 测试与变异见 TESTS.md 同名节。

## /recover 子代理分支的分层边界修正（2026-09-30，分支 `claude/38-recover-child-import-boundary`，基于 step16v `d9abcb5ec`）

- **起因**：首版 `turn_recovery_control.py` 导入 `agent_core.runtime_mixin` 与 `subagents.models`，`check_import_boundaries.py` 报两处
  `LAYER_BOUNDARY_FORBIDDEN`，step16v 没有上线。
- **改动**：TaskRun 收口判定移到 `conversation/task_run_closeout.settle_terminal_task_run`（`runtime_mixin` 只委托，网关直接调用）；
  接替判定改为 `SubAgentManager.taken_over_successor`，网关经 `owner_agent.subagents` 调用。边界规则没改，行为不变。
- 测试与变异见 TESTS.md 同名节。

## Gateway 前台新消息清零 Goal 空片计数（D4）与 O2 裁定（2026-10-01，分支 `claude/38-goal-fuse-gateway-reset`，基于 main `b7058e33a`）

- **起因**：ae 复测 G05，Gateway 来的新消息之后 `idle_slices` 仍是 3；另有“熔断暂停后 0.1 s 又起一片”的观察 O2。
- **改动**：`request_execution._execute_gateway_conversation_turn` 用户消息写入成功后调用 `reset_goal_progress_fuse`。
- **O2**：不是竞态，是熔断用调度 tick 的 `now` 记 `updated_at`，那一片其实就是触发熔断的第 3 片；不改代码，见设计台账同名节。
- 测试与变异见 TESTS.md 同名节。

## 持续目标空片熔断时用户消息先处理（C5/O4，2026-10-02，分支 `claude/38-c5-user-msg-before-fuse`，基于 step16z `f9ca83242`）

- **起因**：第 3 个空片还在跑时用户发来消息，消息转成下一轮排队；第 3 片记账先熔断暂停，用户回合写入后只清计数，目标仍暂停（真实链路复现）。
- **改动**：网关前台车道（`request_binding.gateway_conversation_execution_lane`）标 `carries_user_input=True`，从排队到退出车道在
  `conversation/run_claim` 里登记“用户回合在场”；后台 Goal 记账时用户回合在场的空片不计入。清零仍按 D4 在用户消息写入后完成。
- 设计与边界见设计台账同名节，测试与变异见 TESTS.md 同名节。

## Gateway 会话内 /stop 收回被中断任务的后台进程（C12a，2026-10-01，分支 `claude/38-c12-stop-after-interrupt`，基于 main `34e4d874e`）

09-28 的"会话内 /stop 回收遗留资源"只接在本地 chat 入口，默认 TUI 和飞书走的 Gateway 入口没有。所以中断后几分钟、发现层自愈把
任务记成 cancelled 以后，Gateway `/stop` 仍只回"当前没有运行中的内容"（10-01 真实链路复现）。现在 `control_service` 在没有热请求、
也没有可控根任务时调用 `_stop_session_leftovers`。它只收本会话精确 thread_id 的登记记录，保护仍 active 的任务和执行状态里仍在跑的
任务，按每条记录的 (thread, root_task, run) 走 `freeze_process_stop`，锁外按进程组回收，回执列出 pid 与 task/run；读不出就报未确认、
不停。没有运行中回合时的 `/interrupt` 两个入口都不碰资源。合同测试 `test_gateway_stop_session_leftovers.py`。

## 发现层补关"根本没有会话任务"的执行总账（C12c，2026-10-01，分支 `claude/38-c12c-discovery-no-link`，基于 main `34e4d874e`）

D3 让运行时收口边在关联文件确实不存在时按代理树关 TaskRun，但发现层的崩溃重放只认可读的终态关联。所以收口边前崩溃、又没有请求恢复
重跑的执行（例如 CLI 一次性 run）会永远留着开放的总账，step16v 之前的同形历史行也一样（生产试算 338 条）。现在
`owner_wake_discovery._task_run_reconcile_reason` 补上同一规则：规范目录列全、任何目录里都没有 `<task_id>.json`（读坏的文件算"有"）、
根主执行轮已终态，才以 `no_conversation_task` 走树终态 CAS。Gateway 前台请求的崩溃由重启时的请求恢复重跑补上（实测），不靠这条。
合同测试 `test_owner_wake_discovery_task_run_no_link.py`。

## 宿主托管存储（插件安装库、包库）对模型的文件工具和 shell 不开放（H2，2026-10-01，分支 `claude/38-h2-host-store-read`，基于 `claude/3a-step16z` `efaedfab2`）

ae 的 C3 真实补测里，模型用 `run_command` 的 `unzip -p` 从 owner 插件库读出已停用、已换代的旧包。原因是插件库就在 owner home 里，
而路径策略与 shell 沙箱都整个放行 owner home。现在由 `path_access_policy.HOST_MANAGED_OWNER_STORE_PARTS` 唯一声明宿主托管存储：
- 文件工具经 `PathAccessPolicy.check` 最先拒绝（`PATH_HOST_MANAGED_STORE_BLOCKED`），遍历类工具不交出其中条目；
- shell 沙箱把它们藏起来（Linux 用只读空 tmpfs、macOS 用 Seatbelt 末尾拒读写）。
用户工作区里同名目录和自己的 zip 不受影响。合同测试 `test_host_managed_store_access.py`。

## Gateway 停机一并结清进程内 runner worker 与 owner 池 agent 的在途模型调用（J17，2026-10-02，分支 `claude/38-j17-stop-settles-runner-calls`，基于 step16z `51e52f04a`）

- **起因**：停机结清只结网关 agent 自己的账本；非 local/main owner 的进程内 runner worker 和 owner 池作用域 agent 各有账本，停机后在途调用一直是 started。
- **改动**：`call_runtime.track_shutdown_ledger` 弱引用登记（runner worker 构建、owner 池建 agent 两处），停机结清一并处理；事件与原因码不变。设计见台账 J17 节，测试与变异见 TESTS.md 同名节。

## Gateway 停机准入栅栏：结清之后不再接新的模型调用（J17 必须修，2026-10-02，分支 `claude/38-j17-shutdown-fence`，基于 step16z `7b21f38b9`）

- **起因**：sol2 审查发现停机结清只拿一次账本快照。排空没收住的 worker 在结清后还能登记新调用，快照后新建的账本也会漏掉（`still_open=["started-after-shutdown-snapshot"]`）。
- **改动**：
  - `contracts/model_call_ledger` 加进程级模型调用准入表（账本构造即登记）和 `close_model_call_admission`；
  - `started` 在关门后抛 `MODEL_CALL_ADMISSION_CLOSED`；
  - `settle_open_model_calls_for_shutdown()` 先关门再结清；
  - 删掉 `track_shutdown_ledger`。
  - 事件和原因码不变。设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 停机准入拒绝的调用方收尾（sol2 复审 J17 栅栏，2026-10-02，分支 `claude/38-fence-callers`，基于 step16z `00bcf7d45`）

- **起因**：栅栏关门后，决策入口把准入拒绝说成 `enhancement_failed`，子代理说成可自动重跑的 `runner_error`，运行错误报告说成 `programmer_bug`，唤醒毒丸还把它计数。
- **改动**：四处共用 `find_model_call_admission_error`。
  - 决策返回 `stale/host_shutdown`，不进冷却；
  - 子代理记 `model_call_admission_closed`，不自动重跑；
  - 运行错误报告给 `host_stopping`，带结构化原因码；
  - 唤醒毒丸判不计数。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## Esc／/interrupt 中断后过一会儿再发消息仍接着原任务（第 7 条，2026-10-02，分支 `claude/9b-interrupt-resume`，基于 step16z `5a56714dc`）

- **起因**：中断只结束本轮、关联保持 active，约 2–4 分钟后发现层账本自愈把它收成 cancelled，之后的新消息开新任务；用户习惯 Esc 后过一会儿发"继续"。
- **改动**：
  - `control_service._stop_active_task` 的纯中断分支在确认中断到一轮之后调 `_record_user_interrupt`，给主执行轮当前代次记 `agent_run.user_interrupted`（`runtime_db/user_interrupt.py`）；本地 chat 入口同一事实源。
  - `owner_wake_discovery._filter_by_runtime_authority` 在 24 小时续接窗口内跳过这类 cancelled 执行，不投影、不诊断、不驱动；窗口截止时刻进发现缓存期限。过期照原规则收口，原因 `user_interrupt_expired`。
  - `/stop` 语义不变：窗口内走正常任务停止并回收资源，过期后走 C12a 遗留资源回收。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## Gateway 收尾给本进程在跑的子代理执行器打停机记号（step17c 预演观察 1，2026-10-02，分支 `claude/38-shutdown-label-v2`，基于 `5a56714dc`）

- **起因**：自然停机时网关进程里的子代理随进程消失，重启后被收尾成 `runner_error`，看不出原因是停机。
- **改动**：
  - Gateway 关门结清后，给本进程在跑的执行器在各自 attempt 上打 `executor.host_shutdown` 记号，个数写进收尾载荷 `host_shutdown_executors`；
  - 重启收尾看到记号，记 `host_shutdown_interrupted`（不自动重跑），有未确认工具效果时仍先核对。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## /status 的子代理异常按界面标签细分（2026-10-02，分支 `claude/9b-shutdown-label`）

- `control_service._subagent_status` 返回 `_SubagentStatusCounts`（前四项位置不变，多 `other_labels`），异常一类按 `agent_activity.subagent_display_label` 细分；`ConversationTaskStatus.subagent_other_labels` 让 IM 与 TUI 的 `/status` 显示“异常 3：宿主停机中断 2，失败 1”，没有异常时原文不变。

## schedule 工具声明默认收起（工具瘦身 T1，2026-10-02，分支 `claude/75-tool-default-defer`，基于 `c6f28b150`）

- `scheduler/tool.py` 的 `schedule` 在 ToolModelHints 里声明 `default_deferred` 和用途句“创建、查看、暂停或删除定时任务与提醒”。开关 `tool_default_deferral_enabled` 打开时前台回合不再每轮发它的 Schema，目录索引列出它，模型说“提醒我…”时 tool_search 一步加载。工具行为不变。设计见台账同名节，测试见 TESTS.md 同名节。

## 停机关门后迟到的模型响应里的工具不执行（I3，第 8 条①，2026-10-02，分支 `claude/38-late-tool-fence`，基于 step17e `4dd56f627`）

- **起因**：停机结清只在账本里把在途模型调用记成 failed；物理响应稍后照常回到工具循环，响应里的工具调用（例如写文件）照常执行。
- **改动**：工具轮每个未启动调用前的顺序屏障先读 `model_call_admission_closure()`；关门后当前及后续调用不启动，记 `HOST_SHUTDOWN_TOOL_NOT_STARTED`（cancelled、未执行），关门原因码与账本同源；之后下一次模型调用照旧被准入拒绝，回合按宿主停机收尾。
  - 9b 复核小意见（随 I4 分支交）：审批前先读准入，关门了不再询问；档案和索引共用 `tooling.runtime_facts.project_host_shutdown_facts`（单行、不超过 128 字）。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 被宿主停机打断的回合重启后统一自动续跑（I4，第 8 条②，2026-10-02，分支 `claude/38-resume-rule`，基于 step17f `f6b63ab35`）

- **起因**：同样是停机打断，被准入拒绝的回合写成 failed、不续跑，随进程消失的回合却会续跑；IM 也看不到续跑提示。
- **改动**：`request_execution._host_shutdown_resume_marker` 认出宿主停机准入拒绝时，响应带 `restart_resume` 标记，不写终态、不收口插话、不记审计；`request_worker._finish_claimed_gateway_request` 见到标记就把请求留在 processing，交给重启恢复。用户停止优先。续跑回合的最终结果给 IM 补宿主提示，文案与 TUI 共用 `conversation/turn_resume_notice.py`。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 同一回合因非计划重启最多自动续跑 3 次（I4 续，2026-10-02，分支 `claude/38-resume-limit`，基于 step17g `72ddc2b5c`）

- **起因**：启动续跑没有次数上限，本身会把进程弄崩的回合会无限续跑（崩溃循环）。
- **改动**：
  - `recovery._settle_stale_processing` 统一过期请求的三选一：卡死超时、续跑上限、重排。
  - 非计划重启的续跑次数记在 `active_turn_recovery.unplanned_resume_count`。满 `MAX_UNPLANNED_RESUME_COUNT`（3）后再被打断，按 `TURN_RESUME_LIMIT_EXCEEDED` 收成 failed，和卡死超时共用 `_commit_stale_processing_failure`。
  - 安全重启接班、服务内租约过期不计。
  - 设计见台账同名节，测试与变异见 TESTS.md 同名节。

## 续跑上限收口时插话都有终态和提示（step17h，2026-10-02，分支 `claude/38-limit-steer-note`，基于 `afb15947b`）

- **起因**：step17g 冒烟里，被最后一代续跑回合取走、送进被杀调用的插话停在 submitted，用户看不到说明（3a 定：不能悄悄悬着）。
- **改动**：
  - `recovery._settle_turn_guidance`：续跑上限收口改走 `GuidanceRecovery.settle_dead_turn`，没写进历史的死提交拒收、起备用下一轮，已写进历史的记成已消费。
  - `_apply_limit_settlement`：按结算计数换提示句、写 `guidance_settlement`。terminalize 和已封存热记录的补交两处共用。
  - 设计见台账同名节“插话怎么办”，测试与变异见 TESTS.md 同名节。

## 没有入口回执的插话收口不丢内容（step17h，2026-10-03，分支 `worker/ds2-steer-noreceipt`，基于 `a602d6ad6`）

- **起因**：上面那条“拒收后由入口回执排成备用下一轮”只对有 `gateway_input_request_id` 的插话成立。`/steer` 控制命令且 scope 里没带入口请求号（IM 多是这种）的插话，收口时被拒收、也不起备用下一轮，内容就丢了（9b 复审发现的老问题，不是 38 那批引入的）；普通回合结束时同理。
- **改动**：
  - 会话层 `store_guidance_recovery`：没有入口请求号的插话被收口拒收时，在回执 `migration.closeout_replay` 打 `pending`；新增 `closeout_replay_receipts` / `mark_closeout_replay` 读待办、落终值（`replay_queued` / `replay_unavailable`），收口重跑不重复排队。
  - 入口层 `input_delivery_service.settle_gateway_inputs_for_turn`：入口对账后逐条分流。可重放的经 `steer_closeout_replay.queue_steer_replay`（同一个 `load_or_prepare_gateway_input_locked` + `queue_gateway_input_locked` 入口，请求号按插话身份哈希推出）排成备用下一轮；重放不了的给会话留宿主提示“你刚才补充的话没有被处理，请重新发送。”（`STEER_CLOSEOUT_UNAVAILABLE_NOTICE`）。新增计数 `steer_replay_queued` / `steer_replay_unavailable`，续跑上限收口一并写进 `guidance_settlement`。
  - 顺带修 `host_notices_from` 只认字典、不认 `HostNotice` 的渲染缺陷（取走的提示会整条消失）。
  - 设计见台账同名节“插话怎么办”，测试与变异见 TESTS.md 同名节。

## G4：模型命令沙箱按端口拒绝连本机 Gateway（Gateway 本机信任第 (1) 层，2026-10-03，分支 `claude/be-g4-port-deny`，基于 step17i `3a81e6c4b`）

- **起因**：Gateway 把回环来源一律当可信、无头即本机管理员，而模型命令沙箱默认联网不区分回环（见设计台账与 `docs/design/GATEWAY_LOCAL_TRUST.md`）。第 (1) 层纵深：让模型命令连不到本机 Gateway 端口。
- **改动**：
  - `http_service.start()` 登记实际绑定端口（`server_address[1]`，不是配置值）到进程内注册表 `attempt.sandbox.register_gateway_bound_port`；`stop()` 注销。
  - 模型命令沙箱（`tooling/shell._sandbox_exec`）构造 `AttemptSandboxSpec(deny_gateway_ports=gateway_bound_ports())`；macOS Seatbelt 规则 `(deny network-outbound (remote tcp "*:<port>"))`。用 `*:` 而不是 `localhost:`——否则沙箱用 IPv6 连 `::ffff:127.0.0.1` 能绕过、Gateway 认成 127.0.0.1 即管理员（ae 实测、be 复核）。
  - 端口拒绝只对模型命令沙箱置位，不进插件/Shell 共用的 `_network_rules`；插件沙箱网络由 M 线 B7 管。`full_access` 档也拒（这一档只剩第 (1) 层）。
  - Linux（bwrap）此项为 G5（Landlock 启动器）落地，本块不含。
- 设计见 `docs/design/GATEWAY_LOCAL_TRUST.md` 第 2.1、2.3、3 节；测试与变异见 TESTS.md 同名节。

### G4 补（ae 复审，2026-10-03）

- **启动顺序预登记**：`cli/gateway_process._cmd_gateway_run_threads` 在起请求/后台线程之前先按配置端口 `register_gateway_bound_port`（启动恢复会立即续跑、这两条线程在 HTTP 绑定前就能起模型命令）；绑定后 `http_server.start()` 再登记实际端口（同值幂等），绑定失败注销预登记。`conftest` 加 autouse 夹具每用例前后复原 `_LOCAL_GATEWAY_PORTS`。
- **结构化事实 `gateway_isolation`**：`AttemptExecutionSandbox.gateway_isolation_fact()`——macOS 登记端口即 `applied`；Linux 在 G5 落地前恒 `unavailable:landlock_not_implemented`（如实标注，命令照跑、靠第 (2) 层兜底）。供 /status 等机器读取。

### G4 再补（ae 复看，2026-10-03）

- **isolation 事实单一来源**：新增模块级 `attempt.sandbox.gateway_isolation_status(ports, system=None)`——`not_applicable`（没登记端口）/`applied`（macOS）/`unavailable:landlock_not_implemented`（Linux，G5 前）/`unavailable:unsupported_platform`。沙箱对象 `gateway_isolation_fact()` 和 `/status` 都调它，一个事实一处定义。`/status` 新增字段 `gateway_isolation`。G5 落地后由就绪探针改 Linux 的值。
- **绑定失败回滚补用例**：`_start_gateway_http` 绑定失败注销预登记端口，补 `test_start_gateway_http_unregisters_port_on_bind_failure`，变异“删掉注销”被抓。
- `CODE_SIZE_REPORT.md` 不进提交（跑完 code-size 后 `git checkout -- CODE_SIZE_REPORT.md`）。

## 插件事件中心 B3：观察投递（M 线第一期，m1b3，2026-10-03，分支 `worker/m1-b3-event-hub`，提交 `c9571b037`、返工 `11a56269a`、返工 2 `d8970996c`、拆平 `815151369`，基于 step17i `b6ede99e0`）

- **起因**：设计稿 [插件事件与收紧钩子](../../design/PLUGIN_EVENT_HOOKS.md) 第 7 节：把宿主事件合并投给「已启用 + 清单订阅 + 握手声明」的插件，投递走 B2 的共用通道；事件点接线在 B4，本块只提供 publish 入口与组装。
- **改动**：
  - 新 `plugin_events/protocol.py`：事件公共字段（`event_id`/`type`/`seq`/`occurred_at`/`dropped_before`/`channel`/`thread_ref`/`actor`）、`my-agent/events` 握手能力门、payload 组装与输入归一化；正文只在该插件声明 `content: "text"` 时保留。
  - 新 `plugin_events/hub.py`：`PluginEventHub` 按 owner 分区；每个插件按事件类型只留最新（`dropped_before` 只算槽建立之后被合并的条数，中途启用从当前最新一条开始收、第一条为 0）；一批最多 6 条、一次 `my-agent/events.observe`；每个（owner, 激活）同时只有一个发送任务（单在途，返工 2 起）；发送前 `acquire` 刷新使用时间、发送前后由通道复核代次；回收只摘「自己 acquire 过、且不在有效集合里」的激活（先取 `current_seq` 时间界再读表）；计数（`delivered`/`coalesced`/`failed`/`unavailable`/`last_error_code`/`last_delivered_at`）只在内存，不写盘。
  - `gateway_parts/plugin_panels_http.py`：`plugin_event_hub(server)` 懒建唯一 hub 挂 server（与面板服务共用同一个池，不建第二个池）；`publish_plugin_event(server, owner, event)` 是事件点统一入口（停机等拿不到 hub 时丢弃、永不抛）；`close_plugin_channel` 停机时一并关闭 hub。
- **边界**：publish 只加锁更新待发并调度后台线程池，永不阻塞、永不抛异常；退避沿用通道的分级（连接级故障退避整条连接、请求级错误只算这一次）；B4 未接、v7/v8 启用门未开（B7 前），本块用假插件与测试替身验证。
- **返工（3a 复审）**：`seq` 定为按（owner, 插件激活）各自计数、换代从 1 重新开始；新槽（中途启用）水位对齐到「当前最新一条之前」，`dropped_before` 只算槽建立之后被合并的条数；两条真线程池用例改门闩 + submit 计数同步。
- **返工 2（9b 复审后）**：调度粒度从「每 owner 一个投递循环」改成「每（owner, 激活）一个发送任务」（`_inflight` 标记、线程池 4 个、与面板服务同档）——一个插件挂住只占自己的线程，不再拖同 owner 或其它 owner 的插件；`publish_plugin_event` 补 `except Exception` 兜底（首次取用要新建 hub/池，任何异常都不许抛回主流程）；删掉锁内恒真的版本比较死代码；hub 每轮顺手对自管连接调 `close_idle`；`MAX_BATCH_EVENTS` 改为直接引用 `MAX_PLUGIN_EVENT_COUNT`；hub 的池调用改用独立 `pool_clock`（默认 monotonic、与池同基准）——原先 acquire 传墙钟，会让面板侧空闲关闭失效、hub 侧误关面板刚用过的连接。调度改动后类 span 超 near-soft 上限，把发送/收尾/记账与池维护拆成模块级函数（拆平 `815151369`，纯结构重构、行为不变）。
- **验证**：事件中心用例 32 项通过（合并与丢弃计数、批量、单在途、慢插件、停用与换代、跨 owner、握手门、订阅过滤、退避分级、计数接口、Gateway 组装；含 seq 按插件/按 owner 计数与换代重置、中途启用 dropped=0、兄弟/跨 owner 不被挂住插件拖累、回收不摘别人的连接、停用回收自己的连接、空闲关闭按池时钟）；15 个变异全部被杀死；命令与结果见 [TESTS](../../../TESTS.md) 顶部「B3 事件中心」小节。

## 修法 B 收口提示走结构化通道（rfs，2026-10-03，分支 `worker/retry-final-sink`，基于 step17i `9490cdf90`，待复审）

- **起因**：修法 B 到总时长上限的收口提示只走文本回调，富客户端（TUI 状态行、飞书卡片）看不到结构化的「已停止重试」，只能在正文里读到一段文字。
- **改动**：
  - `provider_transient_auto_resume._publish_typed_retry_notice`：收口事件先走 typed sink，额外携带收口参数包 `params={"final": True, "error_code": PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED}`；老 sink 未知参数抛 TypeError、或抛异常/返回 False 时照旧退回文本回调，文本逐字不变；普通重试一个字段都不加。
  - `stream_events.provider_retry_event`：收口事件在 `retry` 载荷带 `final/error_code`，`text` 复用 `request_errors.gateway_client_error_message` 已登记用户文案；`stream_writer.write_provider_retry` 原样透传。
  - `background_transcript`（`_retry_notice_text`）、`agent_activity`、`foreground_transcript`（`_retry_forward_params`）、`main_activity`（`_retry_activity`）按结构化字段显示收口信息，不再显示「N 秒后重连」。
- **边界**：判断只看结构化字段，不解析文案；文案单一来源（request_errors 已登记）；未跑真实 TUI/飞书渲染与真实 Gateway 请求。测试与变异见 [TESTS](../../../TESTS.md) 顶部「修法 B 收口提示结构化」小节。
- **初审补齐（rfsf，分支 `worker/rfs-followups`）**：状态行收口短句统一登记 `request_errors.PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED_STATUS_TEXT`（`agent_activity`/`main_activity` 引用，不再各自硬编码）；补 3 条用例（agent_activity 状态行、main_activity 状态行、foreground 转发）把初审 3 个存活变异全部转红；回退文本断言升级为完整字符串逐字 `==`；新增 ast 守卫用例（扫产品代码所有 `write_provider_retry` 实现方，必须显式声明 `params`、不得用 `**kwargs` 静默收下），协议约定写进发布方注释；命令与结果见 TESTS.md「修法 B 收口提示结构化」小节。

## 插件账本与展示 B6（M 线第一期，m1b6，2026-10-03，分支 `worker/m1-b6-ledger-display`，头 `d9f158e8d`、文档补丁 `e80db5918`、初审 M1 修复 `b6f`，已并入 step17j `24c3aec0f`）

- **起因**：设计稿 [插件事件与收紧钩子](../../design/PLUGIN_EVENT_HOOKS.md) 第 9 节与第 13 节 B6 行。B6 分两半：B5 写 `plugin_gate.decided` 账本，B6 做查询与 `/plugins info` 四段展示；本轮先做不依赖 B5 的查询与展示半。
- **改动**（Gateway 侧只做只读穿透，不改任何控制语义）：
  - `gateway_parts/plugin_command_service.py`：`_scope_management` 从 `scope.event_hub` 取只读事件中心并写进 `PluginManagementContext`；TUI 的 `/client/plugins` 入口（`plugin_http_response` / `_management`）取 `getattr(server, "plugin_event_hub", None)` 放进 scope，读不到就按“暂无记录”展示，不新建 hub。
  - `gateway_parts/control_service.py`、`gateway_parts/control_operation_service.py`、`gateway_parts/http_handlers.py`：`/plugins` 控制分派与幂等执行链把 hub 随 `GatewayControlScope.event_hub` 带下去（不改位置参数顺序、不给内部函数加必传关键字，见下方二次修复）。
- **边界**：hub 由 TUI 的 HTTP 插件入口或事件中心懒建在 Gateway server 上，全进程只有这一个来源；真进程里两条入口的计数共享未在沙箱内复核，只做了组件级逐字同文用例。
- **验证**：`tests/test_plugin_event_display.py` 12 项与 6 个变异见 [TESTS](../../../TESTS.md) 顶部「M1 B6 账本与展示」小节。

### B6 二次修复：确认门回归（b6f，2026-10-03）

- **真回归**：`test_plugins_chat_control.py::test_real_confirmation_gate_stays_closed_before_explicit_user_confirmation`（`[executable]` / `[interpreter]`）在 `d9f158e8d` 之后失败，返回 `PLUGIN_COMMAND_OUTCOME_UNKNOWN` 而不是要求 `--confirm` 的提示。3a 在沙箱外逐提交核对过：`b6ede99e0` / `0a3064078` / step17i 头都是 38 passed，`1a4860903` 起 2 failed。
- **根因（文件:行）**：`agent_py_agent/agent/gateway_parts/plugin_command_service.py:155`，
  `manager = _scope_management(base, scope, event_hub=...)`——**给一个会被测试打桩的内部函数加了必传关键字参数**。用例在 `test_plugins_chat_control.py:196` 打了 `monkeypatch.setattr(module, "_scope_management", lambda *_: service)`，这个替身**只收位置参数**，多传 `event_hub` 就抛 `TypeError`；`execute_plugin_control` 紧邻的 `except Exception` 把它吞成 `plugin_command_unknown(request_id)` → 命令根本没走到插件服务，用户看到“未能确认插件请求结果”。**注意这与 M1 是同一个改动面**：M1 之前（`1a4860903`）那行传的是 `event_hub=_hub_for_control(base)`，同样带关键字，所以两条用例在修复前就已经红了——不是我的 M1 修复引入的，但确实是 B6 引入的。
- **修法**：`_scope_management` **去掉 `event_hub` 形参**，hub 从 `scope.event_hub` 取（调用方把 hub 放进 scope）；`execute_plugin_control` 改成 `_scope_management(base, replace(scope, event_hub=hub))`——**调用点不再传任何关键字**，老替身/老调用方形态都能正常工作。`_management`（HTTP 目录入口）同样把 hub 放进 scope 后按位置参数调。
- **补的不依赖真宿主的用例**：`test_scope_management_stub_without_keyword_args_is_not_turned_into_outcome_unknown`——用**与真宿主用例完全相同的替身形态**（只收位置参数）跑一次真实控制链，断言命令走到了服务、错误码是 `PLUGIN_CONFIRMATION_REQUIRED` 而不是 `PLUGIN_COMMAND_OUTCOME_UNKNOWN`。变异“调用点退回传 `event_hub` 关键字” → **KILLED**（这条新用例 + 5 条既有用例一起红）。
- **顺带（3a 定）**：`GatewayControlScope.event_hub` 改成 `field(default=None, compare=False, hash=False, repr=False)`——展示依赖不该参与相等、哈希和 repr。补 `test_scope_equality_and_hash_ignore_display_only_event_hub`。**如实说明一处**：`GatewayControlScope` 的 `metadata` 是 dict，这个类整体本来就不可 `hash()`，所以该用例只断言相等与 repr（`hash=False` 声明本身仍是对的，保证 hub 不会进某个可哈希字段集合）。

### B6 初审 M1 修复（b6f，2026-10-03）

- **问题（初审 b6r 的必须改 1）**：hub 挂在 Gateway server 上（`plugin_panels_http.plugin_event_hub` 写 `server.plugin_event_hub`），TUI 入口从 server 取到；IM 链却从 `server.agent` 取——而 `http_service.GatewayHTTPServer` 的 `self.agent = server_params.agent` 说明 **server 与 server.agent 是两个不同对象**。IM 永远拿不到 hub，`/plugins info` 的“观察计数”恒显示“暂无记录”。
- **修法（单一来源）**：删掉 `_hub_for_control(base)`（避免第二个取用口）；`execute_plugin_control(base, command, scope, *, event_hub=None)`、`execute_gateway_conversation_control(..., *, event_hub=None)`、`execute_gateway_control_operation(..., *, event_hub=None)`、`_execute_gateway_control_operation_locked(..., event_hub=None)` 一律按关键字参数透传；唯一取 hub 的地方是 `http_handlers._handle_persistent_control_operation` 的 `event_hub=getattr(server, "plugin_event_hub", None)`（那里是唯一能拿到 server 的位置）。**不把 hub 再挂一份到 agent 上**——那样就有两个来源。
- **用例改成真实拓扑**：`test_tui_http_and_im_render_byte_identical_text` 里 TUI 走 `/client/plugins` HTTP 路由，IM 走真实 IM 链 `http_handlers._handle_persistent_control_operation`（不再用 `/client/plugins` 假装 IM 入口，那也是初审指出的“用例名不副实”）；只在 server 上挂 hub、`server.agent` 上什么都不挂；断言 IM 计数不是“暂无记录”、且与 TUI 逐字相同；另加反向断言：server 上没挂 hub 时同一条 IM 链降级成“暂无记录”，不报错、不新建 hub。
- **变异**：把 IM 链的 hub 退回从 `server.agent` 取（`http_handlers.py` 的 `event_hub=getattr(server.agent, "plugin_event_hub", None)`），**被 `test_tui_http_and_im_render_byte_identical_text` 抓到**（IM 变“暂无记录”、与 TUI 不再逐字相同）。
- **给 B5 的接缝（payload 形状）**：`plugin_gate.decided` 行的字段必须写在 `payload_json` 里（`plugin_id`/`version`/`activation_id`/`gate_id`/`tool`/`call_id`/`operation_id`/`args_hash`/`actor`/`outcome`/`verdict`/`reason_code`/`latency_ms`/`host_status`/`final_status`），查询用 `json_extract(payload_json, '$.plugin_id')` 过滤。**B5 必须照这个形状写，否则查询会静默返回空**。
- **已知边界（只增不减）**：“无法审批”计数不设窗口、只增不减（`plugin_gate_unavailable_count` 全表统计，不受最近 10 条窗口影响）。以后再定要不要改成“最近 N 天”或“本激活代次内”。
