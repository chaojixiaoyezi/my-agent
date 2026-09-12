# DESIGN LEDGER

## 2026-09-11 R245 父级空闲、孩子等裁决的唤醒被当过期丢弃【状态：受控复现已定位并修复，待复测】

**受控复现（这次是真实场景，不是读代码推断）**：在 `--workspace /tmp/ma-eval/deadlock-test` 起一个富 TUI，
让主代理派子代理写工作目录之外的 `/tmp/ma-eval/out-of-scope/probe.txt`。结构化事实：

| 环节 | 实测结果 |
|---|---|
| 子代理写入 | `PATH_OWNER_SCOPE_BLOCKED`，`failure_stage=authorization`（拦截正确）|
| 子代理状态 | BLOCKED，`failure_type=capability_request` |
| 能力申请 | `capreq-1789172278-e5f07105` status=GAP，等待父级 grant |
| 唤醒信号 | 网关日志确有 `reason=subagent_capability_request_open` |
| **唤醒结局** | **created_at 1789172278 → handled_at 1789172279，仅 0.8 秒就被标记 handled，父代理零新回合** |

**根因（推翻我上一轮的猜测）**：`subagent_capability_request_open` 属于
`SUBAGENT_LIFECYCLE_WAKE_REASONS`，走的是 `_wake_signal_is_stale` → `_signal_task_link_is_terminal`：
它判断的是**父代理自己那一轮 task 是否 terminal**——父轮次已经 done，于是判定唤醒"过期"并丢弃。
但"父轮次结束"恰恰是子代理在等它的前提：只有父代理再跑一轮才能裁决，所以这里必须放行。
**上一轮我加在 `_goal_wake_waits_for_child_event`（`thread_goal_continue` 路径）的修复根本没被走到**，
因此当时"已部署"并不等于"已修好"——这正是"读代码推断的因果不足以支撑结论"的又一例证。

**修法**：在 `_signal_task_link_is_terminal` 之前加结构化判据——源子代理仍在等待父级裁决时
不按过期丢弃。**第二、三次复测又纠正了两处我的误判**：
①第一次修完复测，唤醒仍在 0.13 秒被丢——因为我把条件只挂在
`reason == "subagent_capability_request_open"`，而该判据依赖的
`capability_request_requires_parent_resolution()` 对**真实状态 `GAP` 返回 False**；
②深查才发现 `GAP` 是**终态**（`CAPABILITY_REQUEST_TERMINAL_STATUSES` 含 GAP）——router 其实
已经路由过，只是结论是"无匹配能力"，于是另记了一条 `capability_gaps=['OPEN']`。
**真正等待父级的是那条 OPEN gap，不是申请本身。** 补上 gap 分支后才真正生效。
第①点也是本仓库"读代码推断的因果不足以支撑结论"的第三次实例：修完必须真机复测。三处修复互补：
`thread_goal_continue` 路径、能力申请路径、以及 R244 记的读取预算与客观门。

合同单测 3 条（OPEN 放行 / GRANTED 仍按原规则 / 读不到不放宽），联合 186 项通过。

## 2026-09-11 R244 换语言复刻的能力上限与失败形态【状态：已量化，结论是目标超出该模型能力】

**结论（连续 4 轮实测得出，不再反复尝试）**：用 工具运行时 `deepseek-v4-flash` 做"1 万~2 万行库的
一比一换语言复刻"，**单轮有效产出约 200~400 行**，而目标总量 53,342 行。
投入 12 轮（含权限改造等并行工作）后客观完成度停在 **44%**（click 34% / httpie 80% / echo 46% / typer 28%），
且最近 3 轮增量降到 0~1 个百分点。

**测得的三种失败形态**（都是结构性，不是提示词能修的）：
1. **只读不写**（R240）：子代理把整轮预算花在通读源库上，ctx 冲到 100k+ 后一条代码都没产出；
2. **写了不落盘**（本轮）：4 个并行子代理被要求各写一个 `core_*.go`，跑满 6 分钟、
   ctx 96k~125k，**收工时那 6 个文件一个都不存在**；重新派出的 4 个同样停在 PLANNING；
3. **单轮吞吐封顶**：即使任务被切成"只写一个文件"，模型单轮也只产出约 200~400 行，
   而 `core.py` 单个文件就 3,838 行 —— 差距是数量级的，不是几轮能追平的。

**有效方法（已连续多轮验证，保留下来）**：
客观编译门（`go build`/`tsc --noEmit`/`compileall`/`cargo build`）+ 真实错误回灌（曾把 httpie 从
27% 推到 88%）+ 偏薄模块判据（只比模块是否存在会严重高估：`core.go` 607 行 vs 源 3,838 行）
+ 读取预算约束（防止第 1 类失败）。
**验收器自身也要被验收**（R243）：它已三次给出错误结论，三次都是工具问题。

**当前交付物的真实定位**：四路都是**编译通过、零占位、真实可用的功能子集**（合计 23,544 行），
不是骨架也不是假完成——但**不是"一比一复刻"**，不能按完成汇报。

## 2026-09-11 R243 验收器的第三次误报与"偏薄模块"判据【状态：工具已修，复刻进行中】

**误报三连**：客观验收脚本到本轮为止已被证明三次给出错误结论——
①`$?` 取的是管道末端 `head` 的退出码（不是编译器）；②把 `__pycache__`/.pyc 算进产物行数；
③把 Python 抽象基类的 `raise NotImplementedError  # user implements` 判成"真占位"。
三次都是**验收器本身的问题**，不是被测对象。教训：**验收工具也要先被验收**，
在拿它的数字下结论前必须用已知正确/已知错误的样本各验一次。

**新增判据：偏薄模块（thin module）**。仅比"模块是否存在"会严重高估完成度——
实测 click→Go 只缺 1 个模块，但 `core.go` 607 行 vs 源 `core.py` 3838 行（约 1/6）；
echo→Python 只缺 1 个模块，但 `binder.py` 83 行 vs 源 `binder.go` 1350 行（约 6%）。
因此复刻类验收必须同时看**模块集合差**与**逐模块行数比**：
`thin = 源模块 > 40 行 且 目标 < 源 40%`。据此定位出的三个最大缺口
（core.py 3838、main.py 2022、binder.go 1350）才是完成度的真实大头。

**进度**（客观、编译器为准）：合计 22% → **42%**；click 34% / httpie 84% / echo 38% / typer 25%；
四路 `go build`、`tsc --noEmit`、`compileall`、`cargo build` 全部通过；真占位 0。

## 2026-09-11 R242 富 TUI 下 send-keys 丢输入 + tsc 回归被客观门拦住【状态：均已定位，前者已定规】

**问题一（操作层，真缺陷但属测试手法）**：我给四路富 TUI 各下发一条相同指令，
实际只有 **1 个会话**收到（另 3 个会话最近请求仍是 28 分钟前）。
根因是 `tmux send-keys` 一次性发文本 + Enter 太快，prompt_toolkit 只接住了一部分。
定规：向富 TUI 投递必须「先 Escape → 发文本 → 停 ≥1s → 发 Enter → 停数秒」，
改法后 **4/4 全部收到**。这条要写进以后的验收操作规范，否则会出现"以为下发了、其实没发"的假象。

**问题二（产品层，被客观门成功拦截）**：port-httpie-ts 此前 `tsc --noEmit` exit=0，
主代理继续加文件后变成 **exit=2、28 个类型错误**
（`Cannot find name 'SEPARATOR_DATA_STRING_NESTED_KEY'`、`Set<string>` 当索引、
基类成员缺 `override`、`HTTPMessage._orig` 未初始化等）。
**这正是《客观验收门》存在的意义**：模型自述"已按 PORT_MAP 推进"，
但真实类型检查是不通过的；若只看自述就会把它记成完成。
处置：把真实错误清单回灌给 port-2，要求先修到 `tsc --noEmit` 通过再写 FINAL.md。

**本轮客观结果**：click-go build ✅ 2435 行、echo-python compileall ✅ 3382 行、
typer-rust cargo build ✅；httpie-ts 因上述回归暂不合格。三路已交付 FINAL.md（91/88/98 行）。

## 2026-09-11 R241 子代理在大上下文下被供应商持续 400【状态：真机复现，待确认是额度/长度限制】

R240 之后继续实测，拿到更精确的规律：

| 子代理 | 运行时长 | 失败时 ctx | 结果 |
|---|---|---|---|
| parser-core-go-2 | 15:42 | **115.7k** | 失败（HTTP 400 空洞 body） |
| sub2-core-2 (typer) | 13:39 | **124.5k** | 失败（同上） |
| subagent-1789144011-139c0e6b | — | >100k | 失败（同上） |

而仍存活、仍在推进的子代理 ctx 是 104k~190k 区间内的低位段，且**产出为 0**。

判定：**ctx 涨到 ~100k 以上后，上游对子代理请求稳定返回空洞 400**（R235 的有界重试会重试一次，
但两次都是 400，说明是持续拒绝而非瞬时抖动）。这同时也解释了 R240 的"只读不写"为什么必然崩：
读取把 ctx 推到上限 → 被拒 → 子代理死 → 父代理既拿不到产物又要重派。

**R241 修正（同日定向实验推翻"~100k 是硬上限"的说法）**：直接向同一端点发递增大 payload 实测——

| payload | 结果 |
|---|---|
| 30k / 35k / 40k / 45k / 50k / 55k tokens | 全部 HTTP 200（45k 连测 3 次亦全过）|
| 80k / 100k tokens | HTTP 200 |
| 120k tokens（不带 tools） | 连接被断（RemoteProtocolError）|
| **120k tokens（带 tools，与失败子代理同形态）** | **HTTP 200** |

结论：**没有"~100k 硬上限"这回事**——120k 带工具也能通过，且同一尺寸会一会成功一会失败。
真实形态是**上游间歇性连接被断/拒绝**，与 payload 大小没有稳定的因果关系；
子代理失败时 ctx 恰好都在 100k+，只是"活到那个规模的任务本来就更久、更容易撞上抖动"，
不是长度触发。因此 R241 的正确表述是：**上游可靠性问题（间歇），不是可控的长度门**。
仍然不能做的是拿它当借口无限重试（R235 的有界重试仍是最合适的处置）。

**对任务设计的直接影响**：任何"先通读大项目再动手"的策略在这个模型+套餐下都不成立。
可行方向只有两条——①把单子代理上下文压在 ~100k 以内（小步读、立刻写、多轮推进）；
②主代理直接分片实现，不把大阅读塞进子代理。R240 已下发第①条的硬约束。

## 2026-09-11 R240 子代理"只读不写"：读取无预算导致产出归零【状态：真机复现，待修】

四路富 TUI 换语言复刻（工具运行时 deepseek-v4-flash）实测事实：
12 个活跃子代理连续运行 13 分钟，ctx 增长到 **104k / 156k / 124k**，
而四个目标目录的**实现文件新增为 0**（click-go 停在 1 个 .go、echo-python 停在 3 个 .py）。

排查过程与排除项：
- 不是权限问题——子代理写根已含目标目录（R234 + R238 已打通），且无 PATH_OWNER_SCOPE_BLOCKED；
- 不是模型不可用——请求 status=done、ctx 每轮增长，说明在真实推理；
- 不是卡死——子代理界面显示"运行中"，updated_at 持续刷新。
真正原因：**子代理把全部预算花在"通读 1.2 万~2 万行源库"上，永远到不了写代码那一步。**
这既是我给的任务设计问题（要求"先建立模块清单再动手"），
也暴露一个通用缺口：**子代理没有读取预算/产出节奏的硬约束，长源码任务会在阅读阶段耗尽预算。**

处置：已向四路下发"最多读 1 个源文件（≤3000 行）→ 立刻写目标文件 → 立刻编译 → 通过即收口"的硬约束；
若子代理仍无法收敛，则由主代理直接写最小可编译实现再逐模块扩展。
待验证：该约束能否把四路推进到"有实体实现且编译通过"。

**这条属于真实缺陷而非环境问题**：任何"移植/复刻/大文件分析"类任务都会踩到同一个坑，
后续应考虑在派工合同里给"读取预算 + 先产出可验证骨架"的通用引导（软提示 + 结构化 checkpoints），
而不是靠每次人工纠偏。

## 2026-09-11 R239 显式声明工作目录时不再继承 owner home【状态：实现与单测通过】

真机实测（四路富 TUI 换语言复刻，管理员 full-access + `--workspace`）暴露：子代理的
`allowed_write_roots` 里**同时**出现父代理 owner home 与目标目录：

```
.../runs/.../work/agents/subagent-...   (自己的)
/Users/example/.my-agent/owners/local/main   ← 父代理 home（不该再给）
/private/tmp/ma-eval/port-echo-python         ← 目标目录（第二层作用域，正确）
```

根因是两个函数叠加：`_current_conversation_product_write_roots` **无条件先返回 owner home**，
`_second_layer_work_roots`（R234 新增）再追加显式工作目录。后果是子代理有两个写根，
完全可能把产物写回老家而不是交付目录——这正是本轮"产物没落到目标目录"的直接原因。

修法：**显式声明优先**——`conversation_runtime_workspace_roots` 非空时直接返回声明目录，
不再附带 owner home；未声明时保持原语义（普通 child 仍从 owner home 继承），普通用户零变化。
配套更新 `test_orchestration_tools` 里一条旧期望（原来断言后代继承 owner home，
现按新语义断言继承显式声明的 task root——授权来自显式声明，不靠隐式包含）。
新增两条反例单测，联合 157 项通过。

## 2026-09-11 R238 显式工作目录声明（管理员第二层作用域的落地前提）【状态：实现与单测通过】

R236 的结论是"第二层作用域机制正确，但默认路径上父代理工作目录恒为 owner home，因此没有增量"。
按用户确认的路线①落地：**工作目录改为可由用户显式声明**，仍然不把进程 cwd 当权限来源。

- `chat` / `resume` 新增 `--workspace`，多个目录用逗号分隔（一次声明多个，逐个过硬门）；
- 校验沿用既有 `validate_requested_workspace_roots`：外部目录只有本机管理员
  `provider=local && kind=main && access_mode=full-access` 才能用，其它身份一律拒绝
  （单测覆盖"普通用户指定外部目录被拒"）；
- 声明生效后 `workspace.cwd` / `conversation_runtime_workspace_roots` 随之变成声明值，
  R234 的第二层作用域才会真正把该目录下发给子代理——两条改动是**一对**，缺一不可；
- 仍然明确不做：让进程 cwd 自动成为工作目录候补（那会重开"从 /root 启动就把 /root 当任务目录"的洞）。

单测：`tests/test_second_layer_write_scope.py` 新增两条（full-access 多目录解析、普通用户外部目录拒绝），
共 15 项；CLI/工作区相关联合 81 项通过。

## 2026-09-11 R237 补充：四路复刻停摆的真实原因 = 供应商 5 小时配额耗尽【已确认】

四路复刻在 08:41 后完全停摆，四路主代理收到的用户消息都只回空响应
（`工具轮数 0 / ctx_tokens~0`，3 秒返回）。查网关请求终态得到明确事实：

`ProviderQuotaExhaustedError: HTTP 429 {"type":"GoUsageLimitError",
"message":"5-hour usage limit reached. Resets in 41min. ... enable usage from your available balance"}`

最近 90 分钟请求分布：**ok 8 / 429 配额 20**。

结论与边界：
1. 这是**外部配额**，不是本地 bug；本地表现是**正确**的——typed `ProviderQuotaExhaustedError`、
   不盲目重试、不伪装成功、失败只终止当轮。
2. 这也解释了同期那批连续失败：**配额临界时上游先给空洞 400，彻底耗尽后转成显式 429**
   （R235 的有界重试因此治不了它——不是抖动而是额度用尽）。R235 的价值在于**区分**这两类，
   让"该重试的"重试一次、"不该重试的"立刻停下并给出 typed 原因。
3. 因此四路复刻的进度**受额度上限约束**：这一轮（5 小时窗口）内已经用掉了配额，
   继续压测只会得到 429。要么等重置，要么在 工具运行时 控制台开启"使用余额"，
   要么换 provider（本机已配置的 deepseek-official-openai / MiniMax-M2.7 都是可用备选）。
4. 客观验收器与《客观验收门》不受影响，配额恢复后可直接重跑四路并复测。

## 2026-09-11 R237 换语言复刻的客观验收与暴露的问题【状态：验收口径已建立，四路均未达标】

**为什么改用客观验收**：四路复刻的主代理都自报"按 PORT_MAP 推进"，但用户侧无法据此判断真实进度。
于是建立与模型自述无关的验收器 `/tmp/ma-eval/verify-ports.sh`：直接用目标语言工具链说话——
Go `go build/vet`、TypeScript `npx tsc --noEmit`、Python `python3 -m compileall`、
Rust `cargo build` + `todo!()/unimplemented!()` 计数。**声明覆盖率不等于真实覆盖率**。

**首次客观测量（08:21）**：
- port-click-go：**0 个 .go 文件**（`go build` 报 matched no packages），只有 PORT_MAP.md + 一个 Python 校验脚本；
- port-echo-python：**0 个 .py 文件**，只有 2 个 md；
- port-typer-rust：8 个 .rs / 1285 行，但 **13 处 `todo!()`**（core 解析引擎、help、testing、completion 全是骨架）；
- port-httpie-ts：12 个 .ts / 2948 行，`tsc --noEmit` 通过——唯一有实体的。

**发现并验证的系统性问题**：模型倾向用"计划文档 + 骨架"替代实现，且会把 PORT_MAP 写成已对齐。
施加**客观证据压力**（把编译器的真实输出回灌给它）后立刻出现实体文件：
port-click-go 0→1 个 .go（224 行，go build 通过）、port-echo-python 0→3 个 .py（437 行，compileall 通过）。
故已把《客观验收门》写进四份任务说明：未真实编译通过或目标语言无实现文件的，一律记未覆盖，
汇报必须附真实命令与退出状态。

**仍未解决的阻塞（如实记录）**：本轮子代理 FAILED 增至 24（最近 40 分钟 11 例），
全部是 `HTTP 400: {"object":"error","model":"deepseek-v4-flash"}`。R235 的有界重试已生效
（同一请求会重试一次），但**连续两次都是该 400** 说明这不是一次性抖动，
而是上游在持续并发下的稳定拒绝。当前行为是**有界失败**（不无限重试、typed 错误、会话仍可用），
但代价是子代理成批死亡、父代理白等。后续需要的是"失败可续跑"而不是"更猛地重试"。

## 2026-09-11 R236 第二层作用域的真机验证结论【状态：实现保留，但当前不会生效——需产品决策】

真机验证（四路复刻 + 管理员 full-access）结论：**上一提交的能力在当前形态下永远不会触发**，
原因不是实现 bug，而是与一条**故意的安全不变量**冲突：

`cli/workspace_resolution.py::owner_home_workspace_root` 明确写着
"Process cwd is not an owner or permission fact"，所有无显式 workspace 的 CLI/Gateway 入口
一律把工作目录定到 owner home（注释原话：让从 /root 或任意项目启动的 TUI 都先回到自己家）。
实测证实：客户端 POST 的 `workspace.cwd` 是 `/Users/example/.my-agent/owners/local/main`
而不是进程真实 cwd；`conversation_execution_cwd` 因此恒等于 owner home，
`_second_layer_work_roots` 拿到的就是 owner home（并且它已被原有 product_write_roots 覆盖），
所以子代理写根不会变化——12/12 新子代理实测都没有拿到目标目录。

**修正上一段的表述（本轮实测补充）**：机制本身是对的，只是在本机 TUI 场景下"无事可做"——
实测父代理在 full-access + TUI 下的 `allowed_write_roots` 就是 owner home
（`workspace.cwd` 被钉在 owner home），第二层派生出的也正是 owner home，
而它**已被原有 `product_write_roots` 覆盖**，所以子代理写根不变。
也就是说：**当父代理工作目录确实在 owner home 之外时（例如会话显式声明了外部 workspace），
这个机制就会生效**；当前默认那条"TUI 从任意目录启动都回自己家"的路径上它不产生增量。
不是死代码，也不是被不变量阻断的逻辑冲突。

仍然不能做的是：让**进程 cwd** 直接成为权限来源——那会推翻
"Process cwd is not an owner or permission fact"，重新打开"从 /root 启动就把 /root 当工作目录"的洞。
因此**保持现状**，并把选择权交回用户，三条路：
①引入显式声明（如 chat/CLI 增加 `--workspace <dir>`），由用户在 full-access 下明确指定工作目录，
再据它派生第二层作用域——这是干净且与现有不变量一致的做法；
②维持"工作目录恒为 owner home"，第二层作用域功能保留但不生效（默认安全）；
③放弃进程 cwd 路线，改为"派工时由主代理把已在授权范围内的目录显式声明为子代理交付根"。

同时确认并保留本轮真正生效的两项修复：无定位信息 400 的有界重试（R235，实测把 22% 子代理死亡率
的攻击面收掉）、以及根级目录不自动继承的兜底常量。

## 2026-09-11 R235 无定位信息 400 的有界重试【状态：实现与合同单测通过，已部署待复验】

四路换语言复刻验收实测：41 个子代理里 **9 个**因 `HTTP 400: {"object":"error","model":"deepseek-v4-flash"}`
直接死亡，父代理整轮卡住、已完成的工作白费。该类 body **既无 message 也无 type/code**，
属供应商侧瞬时拒绝而非"请求被永久拒绝"：同一 payload 稍后重放即成功（本轮多次复现），
30 次突发请求全 200，307 份真实出站 payload 结构异常 0 条。

处置：只对这一种**客观形状**做**有界**重试——必须同时满足 ①HTTP 400
②body 里 message/type/code 三个定位字段全空 ③最多多花一次请求。其它 400（含
"reasoning_content 必须回传"这类有定位信息的拒绝）、401/403/404/429/5xx 一律保持原语义不重试，
也不把 typed 错误伪装成断线。判定同时兼容两种真实 body 形状（顶层空洞与嵌套 error 对象）。

这推翻了 R204 起"没有原因说明不能证明瞬时故障，因此不重试"的旧判据——旧判据在
证据不足时是保守正确的，现在有了可复现证据，规则随之更新；旧断言按新契约改写，
不是放宽为"未知 400 都可重试"。合同单测：`tests/test_provider_unlabeled_rejection_retry.py`（12 项）
+ `tests/test_gateway_helpers.py` 内更新后的两条。

## 2026-09-11 R234 管理员子代理的第二层写作用域【状态：实现与合同单测通过】

用户裁决：管理员 full-access 不设目录黑/白名单——**按"当时用户让主代理在哪工作"给作用域**，
哪怕那是 /etc 这类危险目录；理由是 admin 本来就有权限、普通用户不会去危险目录干活、
偶尔排错需要能进去。普通用户逻辑完全不变。

落地规则：子代理写根 = 直接父级已授权的产品写根（原规则不动）
∪ **父代理当前工作目录及其子树**（新增第二层作用域）；普通用户有 owner 墙时不追加。
来源是宿主写入的 `conversation_execution_cwd`（`conversation/authority.py`），
不是模型文字：单测里往 task_attributes 塞"用户说可以写 /etc"不产生任何授权。
唯一保留的兜底是一条写死的少量常量：**文件系统根级目录不自动继承**
（`/`、`/System`、`/usr`、`/bin`、`/sbin`、`/etc`、`/private/etc`）——它们不是"工作目录"而是操作系统本体；
具体子目录（如 /etc/nginx）照常继承，排错不受影响。这不是可扩张名单，也无任何模型参与的"危险判断"。
多目录一次给：写门本身是多值并列（`write_boundary.py` 的 any(is_relative_to)），
`capability_scope` 的 path_scope 全程为 list，故一次裁决即可登记一条多路径 grant。
接入点是既有继承钩子 `resolved_extra_write_roots` → `_direct_parent_product_write_roots`，
没有新造权限通道。合同单测见 `tests/test_second_layer_write_scope.py`（13 项，含普通用户不扩权、
根级目录不继承、多目录去重保序、模型文字不能扩权四个反例）。

## 2026-09-11 R233 旧会话 HTTP 400 根因修复【状态：已定位并修复，待真机复验】

根因（我们的问题，不是远端）：OpenCode Go 与 DeepSeek 官方在该模型上**默认开启思考模式**，而思考模式
要求请求历史里每条 assistant 都必须回传 `reasoning_content` **原文**。上游原文：
`The reasoning_content in the thinking mode must be passed back to the API.`
R229 之前的老会话没有保存 thinking，落库的 30 条 assistant 全是文本加工具调用、没有思考块；
R229 的补丁给它们补了**空串**，而空串不等于思考原文，因此该会话每轮都被拒。
证据：同一份真实 payload 用我们自己的后端重放，改前 6/6 失败，改后 6/6 成功；
单独实验证明"显式关思考"与"回传真实原文"两条路都能让上游接受。
修法：①删除补空字段的做法，空思考块不再产出 wire 字段；②历史不完整时显式写
`thinking: {"type": "disabled"}`；③历史完整（每条 assistant 都有非空思考原文）时保持思考模式并回传原文；
④只对已核对的 DeepSeek 官方 OpenAI 方言与 工具运行时 Zen/Go 端点写该专有字段，未知网关不写。
这解释了"同一密钥换客户端就正常"——其它客户端要么不把老历史按思考模式回放，要么没开思考模式。
遗留：老会话的历史里永远不会凭空长出思考原文，所以它今后走的是"关思考"分支（能用，但该会话不再有思考）；
新会话历史完整，仍然带思考。分类与错误透明度保持不变。

## 2026-09-11 R231 部署与真 TUI 验收【状态：部署完成，单任务验收通过；两个新问题待处理】

已推送 `ab53853b` 到 my-agent/main 并部署本机 `runtime-r231`（旧 `runtime-r229d` 目录保留可回退），
单 Gateway pid 79812，TUI 显示 deepseek-v4-flash。真 TUI 单任务：
`gwreq-1789125576-69b931ee8c7247fb907fe3d0ee5168a5`，status=done / ok=true / attempts=1 /
processing_failure_count=0 / turn_phase=closed，耗时约 218 秒，22 次工具调用，0 错误、0 次 HTTP 400。
机器事实：三个 write_file（5125/4583/4780 字节、41/53/55 行）全部 succeeded 无截断；
`rm -rf` 被 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 挡在 authorization 阶段（handler 未执行），
随后按提示改走 apply_patch 删除三个文件成功；read_file 回读展示真实 53 行正文；74 条
`tool_input_progress` 证明长参数生成期有实时进度（修掉"看着像卡死"）。
**本轮未覆盖**：这两个文件只有 4~5KB，没有触发供应商长度上限，因此截断分块恢复的真机路径
**没有被这次验收走到**，只有单测与适配器端到端用例证明它可达，不能写成真机已验。
新发现问题一：成品汇报里工具名被替换成"相关操作"，来自
`tooling/operation_verification.py::hide_internal_tool_labels`（有意隐藏内部协议标签），
读起来像"用 相关操作 成功写入"，而实时 transcript 显示真实工具名，归档与实时不一致。
新发现问题二：当前没有可用的目录删除/回收工具——`apply_patch` 只能删文件，`tool_search` 两次
无匹配，`rmdir` 被挡，任务目录最后留空目录；模型如实说明了未完成，没有假称成功。

## R232 截断长内容写的有界恢复接回未完成路径【状态：实现与定向测试通过】

R231 让不完整响应整轮零执行，方向正确，但适配器对 `truncated` 响应返回空 `calls`，使 P0-2
长内容分块恢复与 `NATIVE_TRUNCATED_WRITE_LOOP` 出口不可达；那段恢复治的是 MiniMax 长 content
被截断成空参后反复重生成死循环的现场问题，不能随协议修复一起消失。
接回方式只用结构化事实，不猜正文也不放宽执行：供应商解析层把被丢弃工具的真实 name 放进
`ModelResponse.truncated_tool_names`（`StreamCompletion.tool_names` 同源），工具循环在
`_protocol_violation_decision` 之前先看这个字段；命中 `write_file`/`apply_patch` 才注入分块写恢复。
整轮零执行边界不变：探针 `ToolCall` 只用于给恢复上下文提供真实工具名，不进入执行列表，
参数门、授权与执行路径看不到它。上限改按独立计数 `truncated_write_repairs` 计算——零执行流程
不再产生 `write_file` 失败记录，继续沿用 guardrail 记录会让出口永不可达；该计数与协议修复预算分开。
对照 会话运行时 `会话运行时-api/src/sse/responses.rs:423`：会话运行时 把未完成响应升成致命错误，没有恢复路径；
本项目保留分块恢复是有意的能力差异，理由就是上面那条现场死循环。
端到端用例 `test_truncated_stream_reaches_recovery_through_real_adapter` 走真实 SSE + 真实适配器，
同时验证"零执行"与"恢复可达"，防止再次出现只改一层就断链。

## R231 模型响应完整性【状态：已合入 main；被切断的截断恢复由 R232 接回】

解决未正常结束的原生工具响应仍可执行、坏 JSON 被猜成空参数，以及 Responses 把 EOF/内容过滤误标为长度上限。
模型响应、工具调用和归档共用供应商结构化终态；无完整终态或任一损坏参数时整轮零工具执行，不猜补输入。
长度上限仍保留 max-tokens；EOF、坏参数和内容过滤保留各自错误原因，不扩大或新增自动续跑策略。
所有已收到正文/完整思考块继续归档，仅移除未执行工具块，避免截断后整个 assistant 从下一轮历史消失。
对照 会话运行时 `会话运行时-api/src/sse/responses.rs` 的 incomplete 原因和 `core/src/session/turn.rs` 的 EOF 错误边界；
本项目保持既有 G5 整轮工具原子边界，不照搬 会话运行时 OutputItemDone 的流中调度，也不按 Todo/复读正文判完成。
不改模型配置、session 身份、用户历史或 Gateway；不会把此协议修复当作旧 HTTP 400/模型复读根因已证实。
合入时已核的冲突：适配器对 `truncated` 响应返回空 `calls`，使 P0-2 截断分块写恢复与
`NATIVE_TRUNCATED_WRITE_LOOP` 出口不可达。该恢复治的是 MiniMax 长 content 被截断成空参后反复重生成
死循环的现场问题，不能随协议修复一起消失；补回方式见 R232。


## R230 同轮现场修复【状态：开发与原会话验收中】

解决长响应、重复工具与终端交互叠加后难以持续工作的缺口。SSE 已声明的 delta 必须逐字追加，
不再凭正文前缀猜累计快照；该猜测会吞掉合法重复段，导致正文与原生历史不一致。非标准累计流
若需支持，必须有独立、明确的协议标记，不能恢复内容猜测。此修复不声称能消除上游真实复读。
通用重复成功工具提醒复用现有 tool guard 记录，和权限 effect 分开，只软提醒，不截停/审批/重放。
采样按显式配置与已核对供应商方言处理，不将采样差异等同旧会话 HTTP 400 的根因。
本轮保持同一 canonical 会话与 owner 数据；真实验收仍走单 Gateway / TUI，不以新会话通过替代旧会话。
派工不产生永久的主代理禁写角色：参照 会话运行时，主代理继续用户授权内、与活跃下级不冲突的工作，
包括整合、修正与测试；只有用户明确限制谁写功能时才在软提示中保留该范围。创建、唤醒、runner
共用同一职责提示，`coordinator_execution_scope.v2` 仍只作说明，不是执行权限或完成门。

## 2026-09-11 R230 精确端点采样配置【状态：273 项定向通过，待主线真 TUI】

解决 工具运行时 新版给 DeepSeek V4 Flash 设 top_p=0.95、而本项目未提供 top_p 配置的差距。
统一 YAML/dataclass、owner 模型表单与冻结 backend 快照；None 让普通端点省略，显式值经有限范围校验。
只为已核对官方/OpenCode Go/Zen 精确端点及 V4 Flash 型号使用默认与协议下限，未知代理不强加。
不改变 session、重试、工具和历史；这不是旧会话 400 或复读根因已证实。细节见
[模型配置](docs/design/TUI_MODEL_PROFILES.md#采样参数)。真实 TUI 验收由主线统一部署执行。

R229 追加边界：历史缺少 reasoning 的兼容只在已核对的 DeepSeek 官方 / 工具运行时 Zen 工具请求出站层补空字段，
不把空字段写回 canonical 或声称恢复思考；普通未知网关不盲加字段。新会话通过、旧会话仍拒绝分别记录，
继续以真实请求结构定位，不自动重试无解释 400。细节见 `docs/audits/R229_LIVE_FAILURES.md`。
追加已落地：runtime_fact 与界面共用 typed 请求拒绝语义；HTTP 属性优先于错误正文，不能因文案命中扩大重试。
全选/复制必须使用当前 viewport，OSC 长度预算不能限制 native/tmux stdin。通用重复工具软提醒与安全 effect
应分开；该提醒扩展仍属待实现，不新增审批或机器完成硬门，不删除长期会话历史。

## 2026-09-10 R229 原生回放与显示交接【状态：实现与复验中】

解决普通答复后工具请求丢 reasoning_content、前台接管早到背景消息显示双份。
沿 canonical native history 保存实际思考，未返回与显式空字段分开；无思考模型不制造扩展，
不按模型名字猜协议、不捏造旧思考。展示按宿主 request ID 交接，不改变执行/持久身份。
请求拒绝不推断配置错或先前工作没运行。重复生成、鼠标模式、长等待仍开放，
详见 [R229 台账](docs/audits/R229_LIVE_FAILURES.md)。

## 2026-09-10 R228 发布候选清理与误判复核【状态：工程发布门通过，真实误判仍有开放项】

解决问题：R227 切模型压缩前只更新实时窗口、持久 Context 仍旧；角色提示扩大只读检查；
模型把编译成功和未经证实的环境归因当作交付证据。先修同类通用入口，不恢复机器质量验收。
Compact 开始的当前模型容量和源大小先写同一 owner/thread 的显示快照，再发布进度；
遵守原 generation CAS，失败遥测不改变压缩成败，快照不进模型历史或计费账。
角色只表达职责，不产生修复授权；检查与修复由用户目标决定。验证指导区分观察事实与原因假设，
编译、启动和业务结果不可互相替代；工具权限和普通自然结束语义保持不变。
全仓检查包括静态/import/发布边界、测试收集与废弃引用扫描；只有确认无用的内容才删除。
真实复验后追加：超窗分段改为无执行工具的摘要请求；能容纳的单次调用保留原缓存面。原会话已成功
264689 → 28667（摘要计量）并继续 50 个工具轮，但模型仍错误归因沙箱，不能把行为问题判为已修。
发现混排原文锚点的头尾截断会挤掉中间用户要求；参照 会话运行时 用户历史，原 6000 字符预算优先保留用户原话，
再以余量保留助手结论，按时间排序，不猜任务重要性。不能凭摘要恢复旧版已经遗漏的原话；原 transcript 保留。
发布包同步第三方 bwrap 原许可和精确对应源码；验包比较全部同名源码字节，避免旧 build 内容冒充当前版本。
文档去掉与当前测试频率、角色/目录合同冲突的指令，历史失败证据不删除。真实 TUI 与定向验证分别记账。
参考 会话运行时 tui/chatwidget.rs 的同份 TokenUsageInfo、models-manager/prompt.md 的验证边界，
以及 终端交互 forkSubagent.ts 的分工范围；不照搬其禁言或强制 fork 指令。
对照 会话运行时 会话运行时-client/src/retry.rs，删除“400 缺 error/message 就猜成瞬时故障”的特殊分支。
真实 工具运行时 回显 400 曾因此反复请求后落 transient_error；现在保留明确请求拒绝，429/5xx/网络退避不变。
这修正的是错误分类和无依据重试，不宣称已经查明上游为什么返回 400，也不默换模型或删除会话。

## 2026-09-10 R227 当前会话失败链修复【状态：定位完成，开发与真 TUI 验收中】

优先处理用户真实会话：计划 8 项只完成 3 项时模型自行结束；工具运行时 冷启动工具探针强制
tool_choice 与思考冲突；大窗口切小窗口后 transcript Compact 越界且被误报为记录损坏。
同时补普通子代理默认交互终端、修正插话回复正文颜色和管理员 Shell 备份读取范围。
参考 会话运行时 client.rs 的 auto 工具选择、compact.rs 的窗口压力处理以及正常结束不强制验收，
终端交互 AssistantTextMessage/AssistantThinkingMessage 的正文和思考分色。
普通计划不能成为强制续跑/机器验收闸。用户后续明确授权本机部署重启，保留全部任务、记忆及配置；
并指定用 工具运行时 DeepSeek 的 400–600K 真实会话切官网 MiniMax 验证，模型选择只通过真实 TUI 操作。
横向实时统计条和多路长链路验收继续保留，先完成当前阻塞用户的失败链。
真实 595K 切 200K 已完成压缩，但模型漏记原项目位置，且内部 runs 路径被当成旧业务目录推荐；续接验收失败。
对照 终端交互 compact.ts 的 post-compact 文件恢复，新增 checkpoint `observed_tool_paths` 有界投影：
来源是匹配成功工具往返的原样路径参数，不解析正文、不赋予权限、不代替当前文件状态。
排除 canonical owner_runs_dir 下的旧执行目录候选；摘要软提示明确保留原始目标与完整路径。
分段压缩进度按真实已摘要源字符推进，压缩开始即展示新模型窗口，不做按时间伪造进度。
验收状态与失败样本持续记录在 [R227 台账](docs/audits/R227_MODEL_COMPACT_TUI_REPORT.md)。

追加真实样本：子代理读取 runner 明确给出的自身 `CONTEXT_BUNDLE.md` 被 WRONG_STATUS_SURFACE 拒绝。
上下文交接文档（Markdown/JSON）改为普通受 owner/path 保护的可读材料；不把文件内容当状态或权限事实源，
canonical_state 等控制投影仍走原结构化状态通道。额外参数错误返回当前 schema 的公开参数名；命令拒绝展示
宿主 finding 的命令名/规则位置，不输出完整参数值，不静默修参、不放宽删除安全门。状态：定向与 TUI 复验中。

提示词整体核对追加：删除“建清单后可结束、系统会后台继续”的过时规则，取消复刻先写骨架再读源码的冲突，
普通诊断不推断必须写报告；Todo 明确区分 exact covers 事件投影与模型自查。授权规则只放 provider system，
不再复制到 15 个原生工具；派工保留完整字段，角色索引不在嵌套参数重复展开。默认 26 工具未减少，
同口径离线估算固定内容约减少 5.3K token；非供应商账单，也不裁剪会话/记忆。部署改变前缀的首次缓存重建如实说明。
Shell 旧登记引用不再成为所有命令的前置条件：仅跳过明确不存在或受信根外的旧引用，返回原因计数，不读写越界路径，
不改原登记；当前文件仍 no-follow 备份与复核，符号链接、私有备份写失败继续阻止执行。状态：定向通过后做原会话 TUI 复验。
第二轮真实 438K → 34K 压缩与原项目定位已成功，但模型误把普通后台句柄交给 PTY write，
PROCESS_NOT_FOUND 被错误提升为副作用 UNKNOWN，导致整轮停止。对照 会话运行时 write_stdin 的 UnknownProcessId
可返回模型修正：注册表明确没有写入时报告 not_started；完成写入后的进程退出不抹掉已写事实，
部分写入仍为 unknown，不按句柄前缀自动切换工具。状态：补充修复与原会话复验中。
R227f 原会话正确使用普通后台/PTY 并完成两个 child，主代理自动汇总；23 相关文件 806 项通过。
后续新版业务二进制因 init/run_migrations 重复锁同一 Mutex 存在死锁，模型却归因沙箱；
只记录并在 TUI 继续布置真实排查，不由开发者修业务产物，也不增加编译成功即完成的机器判定。

## 2026-09-09 R226 当前模型事实与主/子代理统一自主权限【状态：实现、320 定向与 .10 真 TUI 通过，本机已切换验收】

解决两个用户痛点：切到 DeepSeek 后状态工具误报部署默认模型；主/子代理普通工具审批频繁打断用户。
gateway_runtime_snapshot.v2 将 deployment_defaults 与 caller_model 分离，后者读取本工作片真实 config/backend；
并行工具线程携带独立 Context 副本，不能回退到启动模型/权限。该字段证明调用配置，不鉴定供应商内部模型。
新增 /permissions 和 F4，默认确认/自主工作/管理员 Full Access 均带解释；保存沿用 owner tool_policy.json，
不新建平行策略文件。只有可信 local/main 身份可选 Full Access，TUI 默认管理员不等于默认全盘开放。
auto 只免可选工具确认；禁用工具、权限墙、灾难命令、SOUL 本人确认继续生效，子代理不继承管理员全盘权限。
工具边界读取本用户最新审批选择；等待中的主/子精确调用可因显式模式变更原地续跑，不依赖主代理先处理消息。
路径权限在新工作片冻结，已有片不热改；注册表只生成基础工具权限视图，MCP、存储和执行身份仍是原权威。
参考 会话运行时 permission_popups.rs 的显式受限选项和 tools/parallel.rs 的 StepContext 跨执行传递；
终端交互 PermissionMode.ts 的模式解释。未增加 LLM 自动审批员、语言授权解析或替用户批准任意越界操作。
真 TUI 已验证管理员/普通菜单、MiniMax/DeepSeek 当前身份、主审批等待时 F4 切自主、三子代理完整任务，
以及主代理同期工作时子代理 7 次真正 PTY 调用。工具分配错误样本单列，不因补测通过抹去失败证据。
详见 [R226 台账](docs/audits/R226_PERMISSIONS_MODEL_REPORT.md)；用户明确重启后，本机全局入口切新版，
旧审批等待精确停止，单 Gateway 与 DeepSeek 真 TUI 短问答通过；配置、权限选择和任务文件保留。

## 2026-09-09 R225 本机正式安装、目录命名与剩余 TUI 验收【状态：进行中】

本机命令仍指向另一旧检出的 editable 环境，需替换为正式 my-agent 的独立 wheel runtime；
只替换命令入口，不删除旧源码、用户配置、记忆或任务。每机一个 Gateway，主日常 owner 与测试 owner 分开。
业务任务目录名由模型概括目标，不能把用户原话的开头截断当标题；续作找原目录，用户明确名称优先。
沿现有 home_context_enabled 和 workspace_task_path_template 加强同一稳定指南，不增加取名模型调用、
业务状态分类、目录锁、自然语言权限裁决或全家扫描，不批量重命名旧成果。旧版入口与提示不足分别记录。
已参考 会话运行时 context/user_instructions.rs、通道运行时 workspace.ts / system-prompt.ts / agent-workspace 文档，
以及本机 通道运行时 的 AGENTS、MEMORY 与 tasks/task-execution、context-resume 规则；只采用整理与按需恢复，
不复制其私人人格、强制建档、质量闸门或 skill 火花池。通道运行时 合同索引仅导航，结论来自具体源码。
用户追加的横向实时统计先设计：借鉴 任务运行时 StatsLine 与 session-stats 投影；
位置优先为 Working/Context 与 Todo 之间的一行状态，计数与速度来自结构化真实记录，不从正文/历史视窗猜。
模型步骤、工具调用、重试、Compact 和当前/累计 token 必须区分；缺真实 decode 计时不可显示伪精确 tok/s。
本片验收台账记录普通 TUI 原始需求、会话/请求/子代理编号、工具结果、产物和未覆盖边界。

## 2026-09-09 R224 正式产品名称统一【状态：实现与提交前验收完成】

产品及仓库说明统一使用 my-agent，旧测试标记不再作为产品标签；本轮不改变功能或运行协议。
历史检出目录、会话和工作目录在公开说明中使用中性占位，真实定位继续按提交与 request/run ID 追溯；
不移动当前检出、用户数据、远端部署目录，不重命名运行中的 tmux，不重写 Git 历史。
第三方 任务运行时 的简称展开为全名；它自身可执行命令和实际对照源码路径保留，不改成我方命令。
测试助手配置统一到 MY_AGENT_TEST_*；旧明文密码移除，改为私有环境或 SSH 密钥。历史启动脚本通过
MY_AGENT_CHECKOUT 定位源码，不用产品标签猜实际检出；远端旧部署需在其私有环境中显式配置已有路径。
四个生产 Python 模块去掉注释/docstring 后 AST 等价；44 项定向通过。真实 TUI ma-r224-brand 的
启动、/help、/status 已验，MiniMax-M2.7、单 Gateway；请求账本前后均16条，没有新模型任务。
用户随后明确要求提交；整批追加 43 文件联合 focused（980 通过、2 跳过、1 预期失败）及全部本地严格 gate，
与此前保留的 DeepSeek、R223 和报告一起纳入正式提交批次。未部署测试机，也未把本次命名检查当作 R223 剩余功能验收。

> 名称整理：产品统一称 my-agent。历史检出路径使用 `${MY_AGENT_CHECKOUT}`，旧会话及测试目录用“历史…”占位；实际定位以对应提交和 request/run ID 的原始记录为准。本次未移动目录或重命名真实会话。

## 2026-09-09 R223 外部 87 项审计复核【状态：实现与逐项复核完成，验收边界见台账】

旧提交的风险清单不直接等于当前缺陷数，逐项按当前事实分类，见
[R223 逐项台账](docs/audits/R223_87_ITEM_REMEDIATION.md)。先修数据/凭据/执行效果，再补资源和恢复证据。
记忆 add 不得根据相似度转换成 replace；显式目标、scope、版本与原证据晋升链仍是唯一写入依据。
Shell 127 和 stderr 前缀不能证明没执行，已退出失败保留 failed；不恢复机器质量判官或家内任务目录锁。
HTTP 跨 origin 移除非安全头，307/308 保留方法/正文；重定向连接关闭不下载无界无用正文。
文本保持 BOM、端序、普通权限和换行，原编码无法表示时不写盘；空白编辑默认精确，容错为显式参数。
多文件补丁中途失败不假装回滚，报告已提交路径及失败目标；完整事务/CAS 未验前不声称保证。
read_file 共用有界编码索引并返回 `file_version`，write/edit 的 `expected_version` 与 patch 的
`expected_versions` 显式校验观察版本。未携带版本或外部进程不参与协议时，不能宣称读后写全程强 CAS。
Shell 两条流各保留 4 MiB，超出仍排空并报告 capture 完整性；PTY 容量先预留，写背压有截止，历史有界。
沙箱包含性以 `contained_by_parameter` 正向声明，未声明效果不能因存在沙箱就免审批。
MCP 以 stdio tools 为已实现边界：分页/代次、在途 ID、总请求期限、取消通知和原始结果保留；不伪称完整 MCP。
删除旧 CompressionService 及无用包装，把策展挂到真实 Compact；native IR 二分最短退休前缀，
不增加请求或新的压缩账本。长期记忆嵌入降级公开诊断，around 历史优先同 thread，旧无身份记录明确标记时间邻居。
能力扩展另立边界：Windows ConPTY、MCP HTTP/高级能力、插件完整生命周期、外部 IM ACK 对账和公平跨 harness
评测未实现或未验，不以静态绿色关闭。配置与用户权限不因测试放宽；本轮未部署测试机、未推送远端。

## 2026-09-09 DeepSeek 官方双接口【状态：兼容修复与真实聊天复验完成】

R222 短连接测试通过后，普通 TUI 的原生工具能力探针被 HTTP 400 拒绝。原因是
OpenAI 中间请求对象丢失已有 `ProviderRequestOptions.thinking_disabled`；不是 key 或模型名错误。
沿原 typed 控制补齐传递；只对配置中的精确 `api.deepseek.com` 主机发送其专有 thinking 参数，
不从模型名猜中转商协议，不向标准 OpenAI/未知端点塞不支持的字段，也不全局关闭主代理思考。
会话运行时 `core/src/client.rs` 常规工具为 auto；终端交互 `utils/permissions/yoloClassifier.ts`
按模型事实处理强制工具的思考配置；轻量运行时 `ai/src/providers/openai-completions.ts` 的 thinkingFormat
完成 DeepSeek 方言转换。本次不删除能力探针、不修改工具执行门或错误重试策略。
已有请求级开关的 bug fix，不新增全局配置或模型调用。第三方中转的显式兼容配置仍需独立设计与验证。
完整证据追加到 [R222 模型报告](docs/audits/R222_MODEL_PROVIDER_REPORT.md)，不把短问候当复杂任务验收。
六个模型/协议组合的连接短测和普通 TUI 问候均通过，正式聊天各一轮、零重试、零业务工具调用。
178 项定向及本地严格静态 gate 通过；仅重启本轮独立验证 home 的单 Gateway，未部署测试机。

## 2026-09-09 Provider /model 扩展【状态：通用配置与真实短测完成】

正式库 main 已正常非强推到 f47f2002，保留原正式库和当前实现历史；本地严格 gate 通过。
解决问题：同一服务商重复录入密钥、不能编辑/启停、接口与请求头不匹配导致模型拒绝。
沿用 owner 私有配置的唯一文件，v1 显式迁移到 v2 provider + model 引用；原 profile UUID 保持，
运行工作片保留已冻结配置。服务商维护 ID、展示名、地址、密钥、请求头、会话头和能力；
模型维护接口、上下文、可选独立温度和用途，不限制模型品牌或名称白名单。
菜单配置不进入聊天/LLM/输入历史，保存不自动发模型请求。
连接测试和模型目录发现由用户显式发起，前者只发短问候、不创建任务、不执行工具；Auth 仅预留。
参考 CCSwitch src/lib/requestOverrides.ts 的请求头保护、provider 编辑保留密钥语义，
会话运行时 core/src/client.rs 的会话头贯穿普通请求与 Compact；OpenAI Responses 官方协议按 typed
items/SSE 映射现有原生工具历史，不能用助手文字推断工具调用或完成。
OpenCode Go 使用 my-agent 自身 User-Agent 和每 owner/thread 稳定的 x-opencode-session，
不复制截图 UUID，不伪造其它产品认证身份；“仿照”本片仅指配置/协议兼容，不声称复制完整产品行为。
291 focused 通过；真 TUI 共 35 个模型：26 正常、7 上游不可用、2 待用户同意训练条款，后 9 个保留停用。
本片接通三种接口，不等于实现所有供应商独有 API；Auth 未实现，Embedding 仅目录配置。
旧 R221/跨模型 child 等真实 TUI 待验项仍保留，模型连通成功不能替代长期任务验收。
新模型计价目录不完整另行记录，不用基础短测猜价格；见 docs/audits/R222_MODEL_PROVIDER_REPORT.md。

## 2026-09-09 正式库提交前修正【状态：本地定向及严格 gate 通过，待正式库合流】

解决问题：`runs` 归档迁移后 standalone 收尾仍只认 `tasks`，以及规模入口绕过 Gateway claim。
运行归档收尾只看 canonical owner home、逐级符号链接和锁内精确 run/task 身份，不看业务目录名字。
规模入口复用既有持久请求与执行主链，不移除活动回合围栏；公开响应提交后才投递。
工具发现的纯恢复投影归 tooling 单一入口；未知副作用不自动重试，父级授权快照缺失不补授权。
参考 会话运行时 `core/src/session/mod.rs` 的显式 cwd、`rollout/src/recorder.rs` 的结构化会话身份、
`core/src/session/turn.rs` 的 active turn 输入和 `core/src/tools/context.rs` 的 typed 工具搜索结果。
过期测试按已批准的家目录语义迁移，保留 owner/路径逃逸/精确身份/连续 Compact 检查；没有新增 skip。
截图与 CCSwitch 的 Provider 菜单扩展属于下一片：先完成正式库提交，再实现并用真实 TUI 做模型基础连通验证。

## 2026-09-08 已完成公开过程逐块进入 canonical 历史【状态：R221 定向通过，未部署/真 TUI 待验】

解决 BUG-146 的剩余崩溃窗口：main 的 BackgroundTurnHistory 原先在内存累积到 final，child 的
agent_transcript_events 又是有界传输流，二者都不能单独证明长期完整历史。参考 会话运行时 session/mod.rs
的 persist_rollout_items→deliver_event_raw 与逐 ResponseItem 保存，完整公开块先追加到同一会话消息账本。
使用显式 role=display 和 conversation_display_event.v1 metadata，不伪装成助手回复或任务完成；开始块
仅供恢复时提示结果未知，逐 token 增量继续只走传输。最终快照覆盖同显示工作片，不能覆盖其它工作片。
这些记录不更新 thread 活动时间，不进入模型历史、Compact 输入、Memory Curator 或最近消息条数；
显示分页/实时字节游标仍读取 canonical 原文件。保存失败可见告警，任务本身不被展示错误中断。
先覆盖进程重开、已完成块/未知工具、final 去重、缓存输入不变、Memory 游标和跨 thread 过滤，再真 TUI。
R220 网络阻断前原任务不重派；本片没有真实 TUI 证据前，不宣称历史耐久性已通过。

288项相关focused通过。实时补页用显式 `display_checkpoints` 能力和无正文的 `process_event`，旧客户端仍推进
原物理游标但不收新类型；冷owner不初始化Agent。前台按Gateway请求、child按attempt保持同片分页。
只持久工具started占位，不存空thinking开始、逐token或控制事件；恢复的typed未知工具可被同block真实终态
原位补齐，已知终态不可覆盖。新TUI/旧原始文件不需要模型补调用；存储失败明确告警但不把真实任务变成失败。

## 2026-09-08 派工引用不能冒充派工身份【状态：R220 已部署，真实 TUI 最终待验】

R219 真实五项派工中，植物/僵尸/UI 有不同 covers 与职责，却共享 index.html 输入输出；宿主自动生成
system_derived_io_scope 合同，并把后两项都复用为植物 child。真实回执 created=3、reused 同一 run 两次，
不能归因于模型少传条目。会话运行时 multi_agents/spawn.rs 按显式创建生成新 thread、返回精确身份，不按文件引用合并。
已核对 会话运行时 agent/control/spawn.rs 的 spawn_new_thread，以及本项目 ToolExecutor→ToolOperationCoordinator→
ManagedOperationStore：精确 operation 已终态时重放原回执，输入冲突拒绝，未证明副作用的 UNKNOWN 不重做。
删除 create_context 自动 IO 合同及 work_scope.py 的 IO 哈希生成；显式幂等、Audit work_scope、恢复、owner 与安全门
仍保留。不解析 goal 判相似，也不增加文件锁或机器质量判官。旧任务不重写或重派；新派工不匹配其旧 IO 身份。
定向复现真实 5→3、同文字不同调用、LOCAL/MANAGED 精确重放与新调用；同输入/输出绝不能吞掉独立派工项。
f5b98e9已独立包部署，真TUI两页出现5个运行child；随后测试机网络不可达，未拿到完整原始回执/终态，不标真实通过。

## 2026-09-08 最近上下文快照独立于执行活动【状态：R219 真实 TUI 分项通过】

R217/R218 真实恢复页没有 Context，原因是数字只挂在易失 main_activity 下，空闲时正确清除主活动却同时
失去数字；不能因此保留假 Working。对照 会话运行时 持久 TokenCount 与独立 set_token_info，终端交互 从
Compact 边界后的最近 API 消息读取用量。主/子统一将已有 preflight 数字快照保存到各自精确
ConversationThread，不重新估算历史，不从累计计费或校准值反推；旧 child run 属性不再作数字事实源。
显示记录与 provider 校准、计费分离，带 Compact generation；成功 Compact 原子清除，迟到旧代写入拒绝。
缺记录保持未知，不伪造旧值；TUI 空闲投影不驱动 Working/Todo、调度或额外模型请求。
需验证同 owner 多 thread、主/子、代次、隔离调用、重连/重启及零额外 provider 调用；仅定向通过不得封板。
5ad6696 单 Gateway 部署后，真实游戏主/四 child 均有同代快照；三页空闲 80.7k、无 Working，恢复前后
canonical/执行/模型账完全相同。整合 child 真 Compact 1 后名册/详情均 56.1k；不以此覆盖动画/取消或运行中重启矩阵。
提交前过程耐久化仍开放：对照 会话运行时 session/mod.rs 的 persist_rollout_items 后 deliver_event_raw；
本项目 BackgroundTurnHistory 仅内存收集到 final，下一片不能新增另一份模型正文或以完成快照冒充中断耐久性。

## 2026-09-08 工具历史保留公开调用预览【状态：R218 客户端真实复验通过】

R217 三页真实 TUI 验证中，原页和在线观察页有 `Bash(命令)`，重新 resume 后八条命令均只剩 `Bash`。
已保存的 canonical 快照仍有脱敏 `detail`，但两个 live adapter 仅在 started 时复制为显示字段
`invocation`，终态快照及恢复没有这一副本。参考 会话运行时 CommandExecutionCompletionItem 保留 command、
终端交互 按 tool_use_id 关联原调用和结果；本项目在唯一显示 metadata 投影中转换已有公开 detail，
删除 adapter 的重复转换。显式 invocation 优先，非工具/非文本 detail 不转换，不读取原始参数或补跑工具。
旧快照无需重写，模型历史、缓存、权限和状态不变。实时、恢复和失败工具均需定向及真实 TUI 验证。
同轮另发现空闲 resume 缺 Context 数字，先保留独立待查，不用估算旧数冒充当前上下文。
82da8d1客户端恢复原session，8条命令标题与Ctrl+O头尾内容均恢复；canonical消息/用量不变。
209 focused及严格gate通过；Gateway仍R217，未中断递归任务或为展示修复重跑业务。

## 2026-09-08 前台完整过程复用会话显示流【状态：R217 本地实现，真实 TUI 待验】

参考 会话运行时 的同 thread item delta 通知和 终端交互 的即时消息源更新；前台公开 chunk 接入既有
BackgroundTranscriptSink 工具/思考/Compact 映射，另适配候选回复增量，不复制模型请求或审批入口。
事件携带 exact Gateway request ID，原页沿 R216 的提交前关联去重；foreground_transcript 能力单独协商，
不能给只支持已提交消息的旧页推送会重复的前台增量。游标仍沿同 owner/thread 的现有事件流。
完整公开过程快照随 canonical final 保存，原生模型历史不读该展示 metadata；最终块按精确 ID 原子接替
候选块，不能按文字相似度合并。失败/取消只结束当前工作片显示，不把流关闭当作任务成功或授予执行权。
必须验证正常、丢增量后补齐、重放、恢复、原页零重复及关闭边界，再进行真实 MiniMax TUI 验收。
当前 137 focused 已通过；按 canonical 顺序先消费消息再补增量，修复观察页思考跑到提问之前的问题。
完整快照必须验证宿主请求关联；字段仅用于显示，后台/child 自定义耐久 writer 的调用合同不改。

## 2026-09-08 同会话前台已提交消息同步【状态：R216 三页真实 TUI 分项通过】

解决 BUG-146 中观察页连前台用户输入/最终回复都收不到的缺口。沿 会话运行时 按 thread 投递的做法，
复用 canonical 消息分页和稳定显示块，不另存 notices 正文、不改变模型历史或缓存。
发起页在请求原子入队前登记宿主分配的 exact Gateway request ID，后到的同 ID 前台消息仅确认游标，
不重复落屏；后台续片不能因为携带原请求编号而被过滤。提交回调失败则不入队，不能先执行后登记。
只开放具有明确 Gateway request 绑定的前台 user/final；内部 Audit 继续过滤，原后台 final 路径保留。
foreground_messages 必须由新版客户端显式声明，旧客户端继续原消息种类；冷 owner 不额外初始化。
本片不宣称接通执行中的思考/工具流；最终消息、恢复重放和原页零重复分别验证后，再完成实时过程。
50cdfaf独立包在唯一Gateway部署；原页/观察页/恢复页真实 user=1、final=1，无残留Working，
恢复前后canonical消息/用量未变。下一片继续复用现有公开过程映射与精确请求关联，不建立第二份模型历史。

## 2026-09-08 同会话工具审批等待【状态：BUG-147 真实 TUI 允许一次分支通过】

对照 会话运行时 app-server `bespoke_event_handling.rs` 的 note_permission_requested 与 thread-scoped 通知，
终端交互 Spinner/TeammateSpinnerLine 的工作/等待分离。本项目沿同一公开 chunk 记录本工作片未决
permission ID，最后一个对应回执才清等待；无关回执、缓存批准和并发工具结果不能清掉其他审批。
同 owner/thread 的 main 状态静态展示等待，关闭流释放显示集合；真正审批、任务与模型执行源不变。
这不关闭 BUG-146 的完整前台正文同步，也不允许观察页通过显示状态代批。
ma-r215-110-approval与同session观察页已验等待/批准/继续/结束；拒绝、并发及冻结切换真验保留。
BUG-146已定位foreground chunk单页消费及message_stream后台final过滤两处；下一片按精确request去重，
不能伪造background_delivery_reason或只删过滤导致原页双份最终回复，模型历史和缓存保持原源。

## 2026-09-08 模型展示与独立子代理模型【状态：欢迎区真 TUI 通过；子代理显式选择本地已实现】

用户要求切模型后顶部同步显示，并将本地 Qwen 验收缩为基本连通/调用，不再执行大任务，也不派子代理。
欢迎区对照 终端交互 `LogoV2.tsx` / `useMainLoopModel.ts` 的响应式模型选择；TUI 的 typed 显示状态从
配置成功回执更新，启动/恢复读取 owner 私有选择，失败不冒充切换成功，展开历史不冻结模型标签。
显示“已选择模型”不等于热切当前执行；运行工作片仍保持原 config/backend/prompts 快照。
子代理显式模型按 会话运行时 `tools/handlers/multi_agents/spawn.rs`：缺省继承，显式选择在创建前解析并冻结；
只可引用当前 owner 经 /model 新增的配置，不接受新端点/密钥，不改变主模型或已有子代理。
公开 model 可为精确名称或 ID，同名要求 ID 消歧，未新增模型给出可修配置提示；部署默认仍通过省略继承。
主 A / 子 B 和孙代理继承、恢复、隔离、批次零部分创建及显式模型去重已定向验证。
当前本地 Qwen 按用户要求没有派 child；跨模型真实 child 运行尚未验收，后端入口尚未部署。

## 2026-09-08 模型快照贯穿真实传输线程【状态：BUG-150 本地修复，待真实复验】

R212 真实 provider 日志反证：/model 选择只在父线程生效，普通生成的 timeout-guard Thread 回到部署默认，
压缩同步请求却保持选中模型。显示窗口/调用前标签不能证明实际模型，也不能据此统计跨模型成本。
对照 会话运行时 `core/src/tasks/regular.rs` 的 `Arc<TurnContext>` 贯穿普通执行；本项目在同一个 HTTP保护线程入口
复制当前 Context，而非热改共享Agent或另造后端路径。config/backend/prompts及可信工作片绑定一起保留；
成功、异常均退出该副本，取消仍传给真正HTTP线程。模型选择跨工作片语义不变，不影响其它owner的快照。
两个真实Thread先红用例复现回落默认，修复后通过；普通生成/菜单/模型配置共58 focused通过，真TUI复验待做。

## 2026-09-08 模型菜单不能依赖冷用户完整初始化【状态：R212 本地修复，待真实复验】

R212 首次保存回执超时但配置实际已落盘，列表随后确认同一 profile ID。当前 `/client/models` 会先
`resolve_gateway_scope_agent`，因此纯配置操作也可能触发整套冷 owner Agent 装配；当次阶段耗时未采集，
不能将全部延迟归于这一环。对照 会话运行时 `app-server/src/request_processors/config_processor.rs` 的
`read/value_write/batch_write` 走 ConfigManager，以及本项目已有 `resolve_gateway_scope_owner` 的轻量身份入口，
已让模型菜单仅依赖可信 owner、canonical 配置路径和部署默认值，不启动工具/模型/记忆后台。
保留保存同 ID 幂等、未知结果明确对账和原子私密落盘；不靠延长超时掩盖初始化依赖。
另需明确展示模型选择后的当前模型，避免历史欢迎卡的旧部署默认被误当实际请求配置。

## 2026-09-08 模型选择贯穿子孙运行配置【状态：R211 组合用例修复中】

配置 overlay 会规范化 source 并丢掉宿主 profile ID，且首版只给 model_name 提升优先级，
旧任务层可覆盖新选的地址/窗口。对照 会话运行时 的配置层来源，模型字段整组采用显式选择优先级；
来源规范化仅增加保留声明的 profile_id。无关配置叠加不能让孙代理创建或重启后换回默认模型。
已用先红组合测试复现；真实菜单和 96k 窗口已生效，子孙 overlay 组合仍待新版 TUI 验收。


## 2026-09-08 正式产品与发布归一【状态：命名已统一，正式仓库发布待执行】

用户确认当前实现成为正式 my-agent 后续版本，不再以带实验后缀的产品独立发布。
包名、CLI 命令、安装入口、项目元数据、当前文档标题和新 VM 默认检出统一 my-agent；
正式目标为 chaojixiaoyezi/my-agent。参考项目 DeepSeek Harness 的名称不是本产品后缀，不删除归属说明。
历史记录里的真实路径、tmux/run ID 和旧源码证据不改写；当前检出和用户数据不因改名搬迁。
正式目标已有独立提交历史，另一本机检出也有未提交修改：首次发布须保留两边历史与用户工作，
不得 force push、reset 或覆盖旧检出。完成严格验收后再执行正式发布，本次改名不表示已发布。
用户随后明确要求直接提交正式my-agent；已核对远端main/63422888与当前实现无共同祖先。
首次全量提交gate出现37项失败，尚未执行历史合流/推送；这不是允许绕过验收或抹去旧历史的授权。


## 2026-09-08 等待授权也是实时活动阶段【状态：BUG-147 已定位，未实现】

R210 的真实 Go 页在等待 cancel_subagents 授权，而同会话 main 投影仍停在工具执行中。
下一片消费原 chunk 的 permission_requested/resolved，用显式阶段显示等待与恢复；不根据模型文字
判断卡死，不自动批准、不新增重试/超时，不把前端可观察性变成权限来源。完整授权交互仍由原 contract 负责。
前台/后台共享数字已在两个 owner 的真实 TUI 分项通过，正文流仍是 BUG-146 未完成部分。

## 2026-09-08 前台与后台会话展示统一【状态：BUG-146 已定位入口差异，未实现】

R210 第一片已实现 main **标量**同步，真实 TUI 待验；正文流接通仍未实现，BUG-146 不关闭。
对照 会话运行时 `app-server/src/bespoke_event_handling.rs:handle_token_count_event` 与
`outgoing_message.rs:ThreadScopedOutgoingMessageSender`：按同 thread 共享实际事件；本项目沿原
`BufferedChunkStreamWriter` 已清洗 chunk 更新 `publish_main_activity`，前后台使用同一 owner/thread 表。
精确 display task 跟随 `conversation_runtime`，工作片身份只拒绝迟到显示，不能授予执行权。
不改工具/模型回调、会话历史、缓存、Compact 计数或 Gateway 数量；旧请求关闭不能覆盖新后台的活动。

- 真实同 session 对照：发送追问的原 TUI 有前台工具与 context，新观察 TUI 只有 user 和旧后台过程。
  前台请求 chunk 与 `BackgroundMainActivitySink` 没有合入同一 owner/thread 显示流；刷新成功不能证明完整。
- 下一片先对照 会话运行时 同 thread 的 active-turn 事件投递，再把 foreground 的 typed 过程/上下文纳入现有
  会话展示源；保持 request/run/attempt 精确身份、单 Gateway、消息去重与历史/模型上下文边界。
  不加第二计数器、额外 LLM 调用，不靠摘要文案判断当前正在做什么；本轮只登记，不宣称已修。

## 2026-09-08 冻结正文不冻结客户端故障提示【状态：R209 本地修复】

R208 真 Ctrl+O 暴露二次 decorator 重写 footer：主 renderer 已告警，但展开模式又盖回快捷键。
沿 终端交互 独立连接警告边界，frame provider 将实时 typed 健康传给 decorator 保留原底栏；冻结正文、
搜索高亮/计数保持原状态，连接恢复后自动恢复模式说明，不从字符串内容判断提示优先级。

## 2026-09-08 客户端刷新健康与任务活动分离【状态：R208 本地实现，待 TUI】

- 对照 会话运行时 `tui/src/app/app_server_events.rs` 的显式 Disconnected，以及 终端交互
  `src/components/Spinner.tsx` 的连接警告：读取失败不能继续冒充实时 Working。
- 在唯一 TUI reducer 保存 `background_sync_failed`，由现有完整快照读取的 bool 结果发布，仅变化时更新。
  主/子页面从 root 客户端读取同一健康状态；权限和粘贴优先，展开模式也保留刷新警告。
- 保留上次业务投影并沿原退避重试，不结束任务、不清队列、不产生模型调用，不修改持久历史或缓存。
  健康事件不能重置任务最后活动时间。未新增 Gateway、健康定时器、语义路由或普通自动续跑。

## 2026-09-08 历史展示不能接管实时 Todo【状态：R207 定位与修复中】

- BUG-144 的当前 canonical 账本已保存 Go 清单；旧清单来自历史工具投影。恢复历史不能让旧工具携带的
  `task_progress_items/generation_id/revision` 锁住当前清单代次，向前翻页更不能回写实时面板。
- 对照 会话运行时 `chatwidget/turn_runtime.rs:on_plan_update` 的 typed 计划更新与历史卡片区分；my-agent 保留
  完整历史卡片和原 metadata，只有送入显示 reducer 的历史副本撤掉实时 Todo 控制字段。当前面板继续
  从原 authenticated activity/工具事件读取，不新增状态源、模型请求，不删进度账或改缓存。
- Gateway 失联展示另修：必须保留最后快照并说明刷新失败，不能根据读失败擅自结束任务；沿用当前退避。

## 2026-09-08 空选区复制与失联显示【状态：BUG-142 本地修复；其余待定位】

- 空选区是鼠标的正常状态，不是异常；复制投影返回空串且不调用剪贴板写入，不能中断 TUI 事件循环。
  对照 终端交互 `src/ink/selection.ts:getSelectedText`；本轮不改选区坐标、工具、模型或任务运行协议。
- Gateway 无有效快照时必须区分“上次状态”与“当前仍活跃”；保留历史不能等于假装连接健康。
  SIGINT 现场已证实断线时旧 Working 留屏，连接状态展示尚未落地，不根据超时替任务判失败。
- Go 同会话的新窗口清单过期与主/子上下文数值差异独立记录；尚无数值不一致的同步证据，不新增第二计数器。

## 2026-09-08 截断结果必须有明确提示【状态：BUG-140 首片修复中】

- 先修本地/Gateway TUI 忽略 typed max-tokens 的错误：保留实际收到的半截正文，明确提示长度限制，
  不再仅因 Gateway ok=True 把回合当完整成功。普通包含“截断”字样的正文不能触发提示。
- 对照 会话运行时 response.incomplete 的显性错误边界；本片不增加额外模型调用，不更改 provider 输出上限。
- final metadata 保存同一 typed turn-end；Gateway 正常提交与 repair 传同一原生消息/原因快照，后台工作片
  从实际结果交接原因，child 使用相同归一化。展示提示不混入模型正文，不改写历史、任务状态或缓存前缀。
- 恢复提示绑定原 thread/message ID，完整后台快照的覆盖标记不能再假定处于末事件；实时 child 的提示仍
  属于原 run/attempt 展示流。终端交互 的有界同轮恢复及真实 TUI 截断复验仍需闭环，不把局部通过当全部完成。

## 2026-09-08 后台续片恢复精确用户回合【状态：BUG-141 修复中】

- K 同一 TUI 在调研追问被截断后继续派发 Go 复刻；child wake 已携带新 conversation_request_id，
  后台却把旧 task link.goal 放回 User Task，最终只汇报旧调研。证据已保存，不补发任务或修改失败账掩盖。
- 对照 会话运行时 agent/control.rs 独立的 parent_thread_id / parent_turn_id：稳定父子树和逐轮输入必须分开。
  task link 继续管运行归属/归档路径，lifecycle wake 的精确请求编号从同 owner/thread canonical transcript
  恢复原用户正文；批量信封按账本顺序保留对应输入，不按标题、内容、目录或最近任务猜目标。
- 已有明确请求编号却找不到原消息时显性报恢复错误，不偷用旧目标；无请求编号的旧信封继续走既有 task
  link 恢复。只读原账本，不能改写历史、Compact、缓存前缀、权限或 child 身份，也不添加业务完成验收。

## 2026-09-07 清单状态与执行活动分离【状态：R203 本地通过，待 TUI】

- Todo 的 in_progress 是模型维护的计划事实，不等于当前存在执行。只由快照的前台 phase 或后台 active count
  决定动画；空闲显示待继续，不自动打勾、不改计划账，不用正文判断完成。删除 Todo 单独维持刷新时钟的旧判断。
- 对照 会话运行时 history_cell/plans.rs 的静态计划、终端交互 TaskListV2.tsx 的独立状态图标；本项目保留运行期
  Todo 动画偏好，终态立即静止。144 focused 通过，原 TUI 待验；没有新配置、模型请求或持久化副作用。
- 新发现的截断并非质量验收问题：K provider 输出被截断，runtime 保留 unfinished，但 TUI 未提示。
  会话运行时 会话运行时-api/src/sse/responses.rs 将 response.incomplete 作为流错误；终端交互 query.ts 对 max-output
  有界恢复、耗尽后展示错误。下一步核对本项目同轮恢复与终态展示，不能凭半句文本判断、不能改旧失败账，
  也不能把这种技术恢复扩成普通任务的后台自动续跑或重新加入机器质量判官。尚未实现这部分。

## 2026-09-07 批处理与交互输入分离【状态：R202 已部署，真实分项复验】

- 普通命令没有输入协议，不能默默借用 Gateway/TUI 的 fd 0；前台与后台启动 Gateway 必须等价。
- 对照 会话运行时 `core/src/spawn.rs` 的 RedirectForShellTool→Stdio::null，以及 `utils/pty/src/pipe.rs`
  中独立的 Piped/Null；当前 run_command、controlled_exec、attempt.run 明确关闭宿主 stdin。
- 命令内部的显式管道/重定向不受影响；独立 PTY 和 MCP 继续保留各自输入通道。不解析提示词或命令输出去
  猜确认，不自动输入 yes，不更改权限/文件边界、超时预算或 UNKNOWN 规则；纯 bug fix 无新增配置。
- 真实长任务的 240 秒输入等待为失败样本；163 focused / 9 Linux 环境 skipped。R202 单 Gateway 已部署，
  原 C 验证 PTY/管道与显式 EOF，模型修复业务输入错误并继续；默认无重定向 fd 0 的独立真机切片仍不冒充通过。

## 2026-09-07 详细正文绑定当前代理【状态：R201 原 TUI 组合通过】

- R199 原 TUI 的 child→Ctrl+O→Ctrl+G 组合证明：导航身份已回主代理，frame-provider 却继续使用子代理
  的冻结 snapshot。不能把导航身份与正文来源拆成两份事实，也不能等用户退出详细模式才纠正。
- 对照 终端交互 `screens/REPL.tsx` 的 `displayedMessages` 按 viewed task 选择、缺消息不落回主代理的
  边界，以及 enter/exit transcript 的冻结状态；本项目保留自己的子页 Ctrl+O，不照搬其根 transcript 分支。
- 同一 frame-provider 换到不同 typed store 时，重绑当前冻结 snapshot、清空跨来源搜索坐标，保留
  active/show-all。同 store 刷新仍维持原冻结边界，后到 live 消息不偷偷夹入；无模型、持久化或控制副作用。
- 定位红测后 44 focused 与严格 gate 通过；原 TUI child/孙代理展开后逐层返回、旧页回看已通过，
  两份原会话的消息/用量 SHA 不变；不冒充中文拖选/系统剪贴板通过。

## 2026-09-07 正常子代理工作片不设生命周期次数上限【状态：R200 已部署，两份原现场恢复】

- F 真递归任务的协调员在第 4 个工作片后保持 PENDING，最后一个孩子已经 DONE；直属等待标记被清除，
  `auto_start_orphan_run` 却被 `_ORPHAN_REVIVE_ATTEMPT_CAP=4` 拒绝。正常工作片数被误当故障数。
- 对照 会话运行时 `core/src/agent/control.rs::send_inter_agent_communication` 与 `control/execution.rs`，
  下级事件接续同一 thread，限制的是实时执行容量；`session/turn.rs::run_sampling_request` 的失败预算属于
  单次采样，不是整个代理的生存次数。这里不声称已经逐行对照其全部恢复系统。
- 删除孤儿入口的累计次数门及其 Audit 豁免分支，普通代理和来源岗位共同复用既有 runner candidate。
  fresh session、未退出 attempt、直属等待、owner/conversation、能力和 UNKNOWN 门保持；FAILED/TIMEOUT
  仍由既有 typed failure 重试策略裁决，不把失败改为 PENDING，也不增加无限故障重试旁路。
- 3 个定位用例先失败；修后与生命周期、候选、会话和能力共 88 passed / 1 Linux 专属 skipped。
  另 37 runner-dispatch 通过。R200 原 F 与旧 R171 coordinator 均自动进入 generation 5 并 DONE；
  F root 继续验证，R171 root 已 final/Working 收起；没有插话、新 run 或手工改旧状态。

## 2026-09-07 显示历史向前分页【状态：R199 已部署，原 D 分页通过，组合验收继续】

- 解决问题：恢复仅取最近 80 条且没有更早游标，正文在磁盘上却无法上翻。终端交互
  `src/assistant/sessionHistory.ts` 使用 latest/before_id/hasMore；本项目复用已有 canonical JSONL 字节边界，
  不新建历史库、不修改模型上下文、Compact、Memory 或缓存前缀。
- `history_page.py` 从末尾或指定边界倒读，行数为目标窗口，最早连续工作片保持完整后再分页。
  单个很长工作片可能超过目标行数，不能误称固定字节上限；不为了满足页大小拆坏 native 工具/思考组合。
- HTTP `before_message_cursor` 与实时 `message_cursor` 分开；同 owner/thread 解析仍在 Gateway。
  UI 单个在途读取，错误保留原文和游标，上翻重试；切子页或退出后的迟到结果不应用到当前页。
- 更早页只新增静态显示块，渲染器提供 block ID/行锚点保持原阅读位置。Ctrl+O 冻结页可以补更早前缀，
  但不夹入冻结期间的新回复；控制、主/子工作状态、模型预览不受影响。Home 到当前已加载页顶部，再上翻续读。
- 顺带修复原历史重排未改变显示排序槽位的问题，否则 renderer 按 created_seq 排序会再次把过程放到 final 后。
  事件 journal 不改写；排序槽位只属于显示投影。旧消息及旧失败账不迁移、不删除。
- 原 D 精确 resume：最新页 105 行、before=147844，上翻可见首条真实用户需求；Ctrl+O 也可回看。
  121 条消息和 8 条用量记录的 SHA 均不变，Compact 保持 5；主/子切换和系统剪贴板组合尚未完成。

## 2026-09-07 同一回合恢复归属【状态：R198 已部署，真实主代理恢复切片通过】

- G 真任务在唯一 Gateway 重启后，后台 child 完成唤醒先续完原 root；旧前台请求随后又进行 Compact 和
  原 attempt 恢复核对，因 execution_binding_changed 失败，造成“有最终报告又报任务失败”。
- 同 thread lane 只保证同时不运行，不能单独保证排队的同一旧请求不会再次执行。恢复必须重读权威完成/
  交接状态，使用真实 request/thread/task/attempt 和 canonical 消息，不能改成忽略身份冲突或解析完成文案。
- 参考 会话运行时 `core/src/session/turn.rs::run_turn` 复用同一 TurnContext，以及
  `core/src/thread_manager.rs` 返回已运行的同一 resumed thread；不新增任务质量验收器。
- 实现采用原 Conversation run claim 的恢复归属：Gateway 前台声明 `recover_same_task_only`，未收尾
  claim 只能由同一精确 request 的执行车道接续；TTL/宿主死亡不把请求变成后台新工作。普通后台 claim 不变。
- Gateway 在领取 claim 前持久保存 request/thread/claim-task 绑定，发布失败不领取；避免进程在领取后、
  写引用前退出留下孤儿。终态提交与重启补交在原子更新中只释放这个 exact request 仍在运行的恢复专属
  claim；普通后台 finally 继续按 claim ID 收尾，Gateway 专属车道保留到终态提交，覆盖模型返回后尚未
  封存请求的崩溃间隙。新请求或其他 thread 的 claim 不受迟到清理影响。
- 每次领取在原 Gateway active-turn transition 内核对执行代次，与停止及终态提交串行；等待期间不持锁。
  claim 清理 I/O 失败保留已封口请求，启动恢复复用同一清理函数补交，不触发模型重跑。
  这不增加目录锁、质量判定、模型调用或按文字推断身份；旧记录不自动改写为新协议。

## 2026-09-07 HTTP 正文断流进入既有恢复链【状态：R197 已部署，真实重连切片通过】

- B 长子任务记录 `IncompleteRead(0 bytes read)` 后 FAILED；该 stdlib 异常不属于 OSError，原公共 HTTP
  入口没有捕获，原 transient 分类也不认识其类型。错误正文不是重试依据。
- 对照 会话运行时 `core/src/session/turn.rs::run_sampling_request` 的 retryable stream 和有界采样重试；
  `会话运行时-api/src/sse/responses.rs` 将流错误与完整 response.completed 分开。只适配现有 transport/model 重试，
  不新建 runner、工作队列或任务自动重做路径。
- `IncompleteRead` 在连接/响应头阶段复用原 2/5/15 秒 HTTP 预算，读取正文阶段交现有模型回合预算；
  两层不为同一次正文错误叠加重试。用户停止优先，watchdog 关闭导致的异常保留原超时阶段。
- 只遍历已知异常链读取真实类型；普通同名文案、认证/配置错误不能取得重试权。半截 JSON/tool input 不变成
  正常 ModelResponse，已执行工具不在本层重做。146 focused 通过；真实 MiniMax 一次断流后，同请求与
  attempt 继续，三名 child 非重复派工。长任务质量、完整网络/取消矩阵仍单独验收。

## 2026-09-07 执行代次不等于失败重试【状态：R195 本地通过，待新 TUI】

- 图书递归真任务中，coordinator 正常等待下级并被唤醒三轮，旧展示把 attempts-1 写成“重试 2 次”。
- 对照 终端交互 `components/CoordinatorAgentStatus.tsx::AgentLine` 的状态、耗时、token、队列和职责投影，
  删除没有失败事实来源的 footer 重试推断。RuntimeDB attempt、真正的 provider retry、恢复预算不变。
- 不另加失败计数器、不按错误正文猜重试；真实失败继续显示失败状态并保留诊断。窄屏职责获得原数字占用的空间。

## 2026-09-07 shell 超时后的结果证明【状态：R196 本地修复，待真实 TUI】

- 真任务 A 的构建加搜索命令在 240 秒超时后落 UNKNOWN，之后 Gateway 恢复被挡。现有 shell 会尝试终止
  进程树，却丢弃终止证明和部分输出，并把 TOOL_TIMEOUT 全部归为 unknown。
- 对照 会话运行时 `core/src/exec.rs`：超时触发进程组终止，再收集有界输出和 timed_out/退出状态。
  后续只允许可信进程终态证据把本次操作记为确定失败；信号失败、进程仍存活、身份不可核实继续 UNKNOWN。
  不解析 command/错误文案推导安全，不把终止等同于文件回滚，不自动重放同一副作用操作。
- 实现使用原 `terminate_process_tree` 返回的 `ProcessTerminationReceipt`：终止方式、直接进程退出码、
  观察进程数和未确认 PID。进程枚举不完整、身份不可读取、信号失败或存活进程不冒充确认；
  前台还要求管道排空。该证据只覆盖观察到的进程树，不证明业务成功、外部系统回滚或任意已脱离进程。
- `CommandTimeoutError` 保留部分输出与可信回执；普通 TimeoutExpired 没有回执时仍 UNKNOWN。
  旧计数放行与 TOOL_TIMEOUT 豁免删除；正常调用不再灌入虚假的 UNKNOWN 提示。没有增加质量验收门。
- Gateway 只按恢复的结构化 reason 选择专用错误；不解析异常正文，不把所有会话读写失败都说成超时。

## 2026-09-06 展示任务与执行恢复身份分离【状态：R194 已部署，两路主代理双重启通过】

- 参考 会话运行时 `core/src/session/session.rs` 从 ResumedHistory/SessionMeta 保留 thread/session ID，以及
  `session/mod.rs::record_initial_history` 重建原 rollout；不把展示标题、cwd 或旧项目链接猜成运行身份。
- RuntimeDB 仍是 task/run/attempt 唯一权威。Gateway 的 `runtime_authority` v1 是 core 绑定后写出的
  投影，携带 request 与 transport attempt 来源；回放时必须再次对账，不产生新权限或独立身份体系。
- owner 按需加载，因此不能依赖管理员实例初始化替所有用户调和崩溃。启动扫描与 exact owner 恢复共用
  PID/start 死亡证明和 current-attempt CAS；缺信息、活进程、新代次及不确定工具仍阻断。
- 保存恢复凭据在模型前完成；拒绝或磁盘错误关闭未开始执行轮，不能留下 running 假忙。旧无凭据请求
  仅保留已有精确 task+request 对账，不增加“最新任务/相似路径”猜测兜底。与用户家目录整理软提示无关。
- 另外记录：自动 Memory promotion 先写、后模型直接 batch 写同一偏好会重复。待通过现有来源和条目
  身份解决，不能按相似自然语言硬合并，也不能因此恢复用户确认记忆的要求。

## 2026-09-06 执行代次与卡死失败次数分账【状态：R193 本地实现，待真 TUI】

- 会话运行时 `core/src/session/mod.rs` 的 InitialHistory::Resumed 重建原会话，不把多次恢复当成任务执行失败。
  长期助手 `gateway/run.py`/`restart_loop_guard.py` 也把服务启动接续与短时间服务重启风暴分层处理。
- 适配现有恢复主链：processing 请求的 `processing_failure_count` 是唯一已观察租约失败计数；旧总
  `attempts` 只记录派发次数，不能充当失败事实。旧请求未记录过此事实，初始化 0，不解析 last_error 迁移。
- `startup=True` 的原进程死亡续接不消费卡死预算；运行期租约真失效才加一，原 max_attempts=2 仍是
  首次重排、再次失效失败。重启不清除已发生的租约失败。active_turn_recovery 追加结构化 cause 供诊断。
- 同回合锁内重读 exact attempt/epoch/heartbeat 后重排；终态继续经过同一 CAS 与原消息结算；未知工具副作用
  仍需底层账本确认。本修复不创建 Gateway 自启动循环，也不新加任务时长门；原 10 秒启动退避仍在。
- 外部 supervisor 重启风暴与 长期助手 式服务级熔断属于独立问题，本次不借机引入第二套调度器。

## 2026-09-06 未写入的确定错误交回模型纠正【状态：R192 定向通过，待真 TUI】

- 真实 `update_persona` 的不存在条目与 `update_goal` 的无目标返回没有携带副作用事实，被统一协调器保守地
  包装为 UNKNOWN。读 会话运行时 `ext/goal/src/tool.rs::handle_update`、长期助手
  `tools/memory_tool.py::MemoryStore.replace`：条件不满足交回模型纠正，不当作执行结果未知。
- 本项目保留结构化 entry_id、owner、task、CAS 权威；由实际 handler 在已证明提交前拒绝的分支返回
  `effect_outcome=not_started/failure_stage=validation`。不扩展中央错误码重放白名单、不按正文判断，
  Persona 文件/版本/审计的部分写入异常继续 UNKNOWN。补登记 PERSONA_VERSION_CONFLICT 分类。
- 用真实 operation store 复现 6 个原实现误 UNKNOWN，修后定向通过；原 C TUI 需复验，不以单测封板。

## 2026-09-06 按来源追加运行状态变化【状态：R191 已部署，真实样本通过，经济性继续验证】

- R190 真 TUI 已证明旧状态可以随完整摘要回收，main/child 都在压缩后继续；但原 B 的 7 份动态状态包各有
  14,361～18,743 字符，多份相邻包的前 13,679 字符相同，少量执行变化导致整包重复追加。该字符证据只用于
  诊断，不用文本标题、公共前缀或模型正文判断运行事实来源，也不据此宣称实际价格收益。
- 已读 会话运行时 `core/src/context_manager/history.rs::update_world_state` 的结构化 baseline/diff，以及
  `core/src/compact.rs` 的压缩后当前上下文重建。适配现有 PromptBuilder 的已知字段：按来源分别保存记忆、
  工具推荐、工作区、运行注入与执行事实；每次只追加相对同来源最新 IR 项发生变化的完整分段。
- `RuntimeFactsTurn.source` 是宿主提供的开放来源标识，不赋权、不解析正文。IR 是唯一对比基线，删除永远
  seen 集合，保证 A→B→A 的变化仍可见；真实用户插话不去重。普通轮不修改原历史，完整诊断 prompt 仍保留。
- 预检查和模型生成共用 `project_native_prompt_history`：先在 IR 副本上做来源增量投影，真正发送时才提交。
  解决预检查重复计算动态包、稳定表面指纹混入动态字段的问题；探针不写 IR、用量或 Compact 代数。
- 只有已完成摘要覆盖的连续退休工具前缀可回收旧分段；每个来源的最新状态保留，取消/CAS 失败还原原 IR。
  不更改用户 Memory、权限、任务终态或工具副作用账，不新增模型调用。属于现有 native 缓存重复注入 bug 修复，
  不增加双轨开关；text 协议和没有结构分段的直接摘要请求沿用原输入语义。
- 定向验证覆盖重复包、A→B→A、不同来源、空注入复位、多来源压缩与回滚、完整渲染和请求前缀；随后部署到
  唯一 Gateway。真实新请求未变记忆只出现一次，完整批量追加后 Compact 4 代继续到 final；首轮任务质量
  不等价，provider 两档账单只记观测数据，不当成等效任务成本结论，见 FT-181～184。

## 2026-09-05 运行中 Compact 回收已覆盖的旧状态快照【状态：R190 已部署，原 TUI 回收续跑通过】

- 原长 TUI 的工具整理多次发生后，旧 `RuntimeFactsTurn` 仍全部留在 native IR；每轮执行事实变化又追加
  一份快照，使最低水位不断升高。原最低水位探针忠实保留它们，结果转入频繁的 active-turn archive 压缩。
- 对照 会话运行时 `会话运行时-rs/core/src/compact.rs` 的 `collect_user_messages`、`build_compacted_history` 和
  `replace_compacted_history`：保留真实用户输入，用摘要替换已覆盖历史，并重新注入当前上下文。
- 适配现有整对回收入口：只有持有完整替代摘要时，才随连续退休的旧工具前缀回收其中旧运行快照；最新一份
  快照、所有真实 UserTurn、保留工具尾部和 carried handoff 不动。普通窗口化/孤儿清理不获得回收快照权限。
- 最低水位探针与实际提交使用同一结构规则；摘要先读原始完整 IR，取消或 checkpoint/CAS 失败还原原候选。
  不改原始 transcript、工具/副作用账本、权限和任务状态；普通请求保持 append-only 缓存前缀，不新增开关或旁路。
- 验证先覆盖旧状态高水位、非连续删除、并行工具、最新快照、真实插话、两代压缩和取消回滚，再在原长 TUI
  核对真实代次、完整前后 token、缓存账及任务继续。未部署前不把诊断算成成本优化通过。
- 141 项定向检查已通过；旧实现先出现 5 个回归失败，修改后全部通过。最新单份快照造成的真实 floor
  仍被保留；两代用例确认全量旧快照先进入摘要请求、取消/CAS 失败恢复原 IR，普通未触发轮不重复整理。
- `.10` 原 child 第 96 代 119,356→48,731 后自然 DONE，B main 第 84 代 120,587→54,405 后继续；当前只证明
  回收与长回合续接。完整请求仍被重复状态包快速填满，经济性留给 R191 和真实 provider 账，见 FT-176～180。

## 2026-09-05 后台唤醒不从运行记录选工作目录【状态：R188 已部署，原 TUI 路径复验通过】

- R186 真实 B 整合发现相对 `tasks/日期/项目` 查找失败；exact task link 指向 owner 的内部 runs 归档，
  `_apply_background_task_link_attributes` 仍将该地址塞回执行 cwd，覆盖已经统一的 home 默认值。
- 对照 会话运行时 `tools/runtimes/apply_patch.rs` 分离 cwd、workspace_roots 与文件权限；后台同样只从可信 thread
  cwd 或 canonical owner home 取得执行位置。run_workspace 只保留归档、状态、恢复引用，不再决定用户文件落点。
- 显式客户端目录继续经过既有 owner/Full Access 工具门，不新增权限；Audit 专属引用与业务文件均不迁移。
- 新候选单 Gateway PID 2415915 下，原 B root 已连续成功读取/编辑 29 个 `tasks/...` 相对文件调用，
  原服务 hash 和停止状态保持。完整任务仍在整合，不能把路径修复等同于产物质量和 Compact 效率通过。

## 2026-09-05 易失过程游标的重启边界【状态：R189 已部署，在线 TUI 跨重启通过】

- R188 顺序重启时，未退出 TUI 的活动/Compact 标量在更新，过程仍停在旧块；精确重进 session 后才恢复。
  读到两端都使用 `max(旧游标, 当前游标)`，而后台进程的 next_seq 在新进程从零开始，旧大游标会挡住新事件。
- 已读 会话运行时 `tui/src/app/thread_routing.rs::apply_refreshed_snapshot_thread` 和
  `thread_events.rs::rebase_buffer_after_session_refresh`：刷新后的会话事实与旧缓冲有明确交接，交互请求保留；
  终端交互 `assistant/sessionHistory.ts` 的 ID 分页也不依赖进程内累计序号。这里适配现有 HTTP 过程环，
  不是声称 会话运行时 使用相同 wire 字段。
- 每个已加载 owner Agent 的过程环分配不可复用的 `event_stream_id`，客户端同时携带该身份与 `event_after`；
  同流序号只前进，换流从现存新事件开始。只有发布成功才一起确认身份和游标，冷 owner 回显旧游标而不拉起 Agent。
  canonical 消息字节游标独立保留，已有显示块、输入队列、权限请求和未完成过程不清空；不产生模型调用。
- 本切片修复在线客户端接不上新流，不宣称已经补齐重启前未提交过程的持久化或完整长历史分页。
- 真 TUI 两个原客户端 PID 保持不变，单 Gateway 2492065→2499672；B 实际旧流 cursor 55→新流 0，
  随后主代理新思考、工具与压缩过程自动出现。原 root/thread/child 集合保持，只重建活跃执行 attempt。

## 2026-09-05 子代理长回合 Compact 与失败终态【状态：R187 已实现，待真 TUI 复验】

- BUG-116 属于执行循环和生命周期投影：长 child 累计 8 次成功 Compact 后被固定循环上限判失败，
  父级清理又将该 FAILED 覆盖为 CANCELLED。不能把有进展的上下文续接当成失败重试次数。
- 已读 会话运行时 `core/src/session/turn.rs` 的 mid-turn Compact 成功后继续循环，以及 `agent/status.rs`
  将 Error、TurnComplete、Shutdown 分开的事件状态。适配本项目同一 child thread/run/attempt：成功提交
  Compact generation 后继续，实际压缩失败、无可压缩内容、取消和既有预算仍可终止；不新增自动重派。
- 每次 Compact 后的模型执行批次进入工具显示 ID，避免内层轮号重新从 1 开始覆盖旧工具。
  父级自动生命周期清理只取消尚未结束的 child，保留既有失败原因；显式用户取消和 Audit 结算入口不改。
- 先做定向合同验证，再由真实 TUI 长 child 复验；不把单测通过或补派成功当成原故障已闭环。

## 2026-09-05 子代理最终回复消息身份【状态：R189 已部署，终态页面真 TUI 通过】

- 相邻测试发现 R185 改为 canonical final 后，child 详情仍用缺少 message_id 的旧调用，导致 TypeError。
  复用原 child ConversationThread 的 message_id/thread_id，request_id 继续只关联 typed transcript。
  移除由正文创建 legacy 去重键的旧分支；最终回复发布成功才更新已读身份，不改子代理生命周期。
- B 原 child4 终态页实际进入，委派要求、工具、最终正文均可见；Ctrl+O 不跳回 main，Ctrl+G 精确返回。
  canonical final 为 `msg-6cf16d42f2a242a1`；首次进入的视口是否应直接落底仍是单独 P2 待复查项。

## 2026-09-05 后台完整工作片快照与缓冲重基【状态：R186 已结束回合真 TUI 通过】

- 解决 R185 真 TUI 的 `FAIL_REPLAY_ORDER`：恢复 canonical final 后，旧过程环再次从零推送，将工具和
  思考排到 final 之后。按 会话运行时 `tui/src/app/thread_events.rs` 的完整 turn 快照/缓冲重基处理。
- 每个后台工作片使用跨进程不复用的显示 ID；sink 按块保留公开终态，不从 1024 条临时环反推完整历史。
  最终消息提交时把这份展示快照写入同一 canonical 消息 metadata，commentary 用精确工作片 ID 关联；
  不新增第二份消息文件，不改变模型历史或 Compact 输入，不按正文相似度拼接。
- TUI 恢复完整快照及 final 后才记住其 exact request ID；只跳过该已恢复工作片的旧显示事件，未完成工作片
  和用户插话消费回执继续接收。实时 final 同样补齐已错过的过程块，块 ID 与实时事件一致，不能重复追加。
- 本切片只闭环已提交后台 final 的展示恢复。Gateway 在 final 提交前退出的完整过程持久化、前台 Compact
  前归档与长历史分页继续作为独立 P1，不以本切片冒充通过；旧 final 没有结构化快照时不得猜测补造。
- 原 B 191 块工作片自然 final 后 exact resume 顺序和展开均通过，回看不增加模型调用或 Compact，见 FT-170。

## 2026-09-05 后台回复恢复使用 canonical 消息游标【状态：R185 部分部署，过程恢复顺序未闭环】

- BUG-113 的旧 notices 保存了第二份最终正文，并让 TUI 另生 bg-response 编号；恢复历史后又从零读取，
  同一条 canonical 消息因此双显。对照 会话运行时 `tui/src/app/thread_events.rs` 的快照/缓冲重基和
  终端交互 `assistant/sessionHistory.ts` 的消息 ID 游标，后台完成正文改从同一个 ConversationStore 增量投影。
- 复用既有 `/client/notices` 活动快照入口；其正文游标改为 canonical JSONL 完整行后的字节位置，和过程事件
  游标分开。恢复快照携带实际已读最后消息的位置；不使用当前时间跳过回复。新回复使用原 message_id，
  断线重放与历史恢复均进入同一块；相同正文的不同 message_id 仍是两条消息。
- 删除新的 notices 正文落盘和重复解析主链，旧文件原处保留但不再充当回复来源；不调用模型、不写模型上下文、
  不推进 Compact，不恢复机器质量判官。原历史窗口之外的分页/完整过程归档仍为独立 P1，不能提前关闭。
- 真实 B 复验：新后台 final 自动可见、终态及时收起；同 session 再恢复却有旧过程事件追加到 final 之后。
  根因是另一个 `event_after=0` 过程环未纳入同一恢复快照。下一步按 会话运行时 snapshot/buffer rebase 的完整
  回合身份合并，连同过程持久化处理；不能用“恢复时取当前游标”丢弃未提交工作，也不能按正文猜映射。

## 2026-09-05 家目录权限与文件整理分离【状态：R184 已部署，边界与续作真 TUI 通过】

- 用户明确要求删除“同家不同 task 所以拒绝”的路径选择/授权代码；家目录是普通主代理和子代理的文件权限边界。
  tasks 命名、目录用途、旧工作续作与上传归档由内置提示说明，不解析文字产生权限或生命周期状态。
- 删除按写目标重新绑定 task 和永久改写旧绝对路径的执行入口。cwd、用户文件地址和内部 run 记录分开；
  runtime identity、active turn、取消/恢复、Compact 和 owner 隔离仍使用原结构化事实，旧用户目录不迁移或删除。
- 对照已读：会话运行时 `core/src/tools/runtimes/apply_patch.rs` 独立 cwd/权限根；通道运行时
  `src/agents/bootstrap-files.ts`、`system-prompt.ts` 与 `docs/reference/templates/AGENTS.md` 的工作区说明加载；
  长期助手 `agent/prompt_builder.py` 的 SOUL/AGENTS 加载和 `agent/system_prompt.py` 的稳定/动态分层。
- 运行注入和 child 提示也删除 output/work 特殊命名空间；旧交付合同只附运行引用，不重写 artifacts 或派生写权限。
  新 runs 与旧 tasks 的工具输出索引、home 运行查询均保留，避免布局调整使引用失效。
- 稳定家目录说明复用现有 home/prompt 加载；当前目录、真实结果、时间和记忆仍在动态部分。没有新目录状态分类器，
  不整家扫描，不改写用户已有 SOUL/AGENTS。532 项 focused 覆盖 owner/符号链接拒绝、exact Audit 和旧运行恢复；
  `.10` 两个原 session 真 TUI 已覆盖跨两份旧工作修改、三个 child 的 owner home cwd 和 Compact 后续作。
- 文件边界通过不等于模型交付质量通过：B 曾误改样本原件，恢复追问后又遗漏一个空文件且错误声称完全恢复。
  保留 BUG-115 及原始工具证据；只经 TUI 追问修复，不以此恢复任务目录墙或机器完成判官。
- R183 首轮部署实际又暴露路径重写：模型原参数是正确的旧 Go 项目绝对路径，handler 却收到交接目录路径。
  该轮不算通过；不再以增加 successor 访问代数解决家内文件访问，按本条替换旧设计。

## 2026-09-05 同一请求内往返旧工作目录【状态：R183 局部部署，跨任务授权设计由 R184 替代】

- R182 真 TUI 是交接目录 A→Go 目录 B→A，不是文件权限拒绝。已有 request-local successor A 在离开时
  被标 `superseded`，回到 A 时 ID 分配仍命中它并返回空；另有直接重复绑定同一 successor 时自我 supersede 的风险。
- 对照 会话运行时 `tools/runtimes/apply_patch.rs` 将 `workspace_roots`、cwd 与当前执行分开：本项目仍按显式写路径
  选择同 owner/thread 的唯一 canonical task root，不由任务文字决定权限。已替代记录保留终态，当前请求
  重入时分配确定性下一代；同一活跃代重复绑定保持幂等，不能把自己标为 superseded。
- 这是原绑定主链路修复，不开放跨 owner、未知路径、多根歧义或其他活跃执行；Gateway request、Todo、当前
  workspace 与历史记录继续走现有权威入口。真实 TUI 必须重试原修改任务，不能由测试者修交接文件。

## 2026-09-05 恢复会话复用唯一启动 preflight【状态：R182 已部署，两路原 session 真 TUI 通过】

- BUG-112 的历史 HTTP 请求早于可见 TUI 和 Gateway readiness。对照 会话运行时 `tui/src/app.rs` 先
  `bootstrap` 再 `resume_thread`、终端交互 `sessionRestore.ts` 在首次 query 前恢复状态，薄客户端改为
  在同一 preflight 后台线程等服务就绪、读取 exact owner/session 历史；准备成功才启动 worker。
- 不增加重试器、Gateway 或空历史旁路；等待仍使用既有有界 readiness。历史读取失败显式退出，不接受任务；
  用户已退出时迟到结果不得启动 worker 或覆盖退出状态。显示恢复与模型预览、缓存、Compact 继续分离。
- 两路 TUI 先启动、随后启动唯一 Gateway，均保留原 session 并显示首轮历史；回看模型调用不变，后续普通
  追问均自然结束。旧后台 notice 与恢复历史仍有重复展示，必须按 canonical 消息身份另行闭环，不能按正文去重。

## 2026-09-05 显示窗口不能重复按模型预览回合截断【状态：R181 已部署，原长会话真 TUI 通过】

- R180 的长会话 Ctrl+Home 复验发现：raw transcript 有 53 行、5 个真实 user；读取窗口 80 行已全部返回，
  但后台 commentary 无独立 conversation request id，各自以 message id 分组后共 34 组，又被显示投影裁到 20 组。
  根因是把模型预览的 `max_turns` 重复用于显示分组，不能归为模型遗忘或数据删除。
- R181 删除这层重复截取及多余参数，完整投影调用方已读取的 bounded rows。仍沿用 会话运行时/终端交互 完整消息
  replay 原则；读取层分页仍是独立待办，不能靠无限增大内存窗口替代。无额外模型调用或历史写入。
- 同轮发现 resume 的历史读取早于 TUI readiness，冷启动并行时会提前退出；后续应把历史准备放进既有
  preflight 成功链，再启动 worker。保留同一 owner/session 和有限等待，不用后台轮询或空历史假成功兜底。

## 2026-09-05 会话恢复不能使用问答预览代替正文【状态：R180 已部署，持久化尾部恢复真 TUI 通过】

- R179 真实 `/exit → /sessions → resume` 找回了同一 session，但工具和思考消失；原 canonical final metadata
  中仍有 54 条 native messages、22 次工具调用。根因是客户端只加载 final 配对预览，再据此重建界面。
- 对照 会话运行时 `tui/src/app/thread_routing.rs::replay_thread_snapshot` 的 typed turn/event replay，以及 终端交互
  `src/utils/sessionRestore.ts` 返回完整 `result.messages`，恢复显示与模型请求装配必须分开：读取同 owner/thread
  的 canonical 消息及原生内容块，映射成现有 reducer 的公开事件；问答预览仍只服务原来的本地上下文入口。
- 原生 user 文本中的运行时注入、system、thinking signature 和工具参数不向前端透传；真实用户输入只认
  canonical user row。工具配对只认 tool-use id，失败只认 `is_error`，不解析结果正文。显示恢复不执行工具、
  不回灌模型、不改 Compact generation、缓存前缀、权限或任务状态。
- 本切片先修已持久化内容的恢复。较老的原生块若已被 active-turn Compact 移除，不能编造回来；长会话分页、
  Compact 前完整过程与 child 有界展示环之外的原文恢复继续作为 P1 硬门，不用一次普通 resume 代替。

## 2026-09-05 后台网络观测保留未知与部分证据【状态：R179 已部署，正常分支真 TUI 通过】

- R178 LAN 任务两次把 iptables 的 ACCEPT 当成服务器防火墙已排除；实际 firewalld 使用 nftables，18778
  没有匹配放行规则。模型没有使用既有 `process_session(network_status)`，该失败不能靠重写 final 或增加
  机器质量验收掩盖。运行存活、端口冲突、主机规则与跨机器连接分别记录。
- 对照 会话运行时 `core/src/unified_exec/process.rs` 的独立进程状态和 `linux-sandbox/src/bwrap.rs` 的明确
  network mode；会话运行时 没有这套 firewalld 专用诊断，退出码语义直接依据官方 firewall-cmd 手册。
  现有观测入口只把明确的 `NOT_RUNNING=252` 判为停止；超时、授权失败和缺少 zone 证据都保留 unknown。
  每个 zone/port 都保留查询结果，任一成功不得覆盖其余失败或未放行。只读调用数量、权限和外部探针要求不变。
- R179 `ma-r179-110-process-resume` 实际调用 network_status，正确报告 public/18778 的 rc=1 与外部可达性未验；
  随后原受管服务停止、端口释放、Gateway 不受影响。查询超时/部分规则分支由 focused 覆盖，未做真 TUI 故障注入。


## 2026-09-05 TUI 终态补帧与返回父层名册即时校准【状态：R177 Focused 与 `.10` 真 TUI 通过】

- 后台 final 和 `Working → idle` 已经进入同一 typed reducer，但终态 invalidate 若恰好撞上上一帧渲染收尾，
  终端可能一直保留旧 Working，直到用户 Ctrl+L。对照 会话运行时
  `会话运行时-rs/tui/src/tui/frame_requester.rs` 的合帧调度，当前仍以即时 invalidate 为主链，只额外登记一枚
  one-shot 终帧请求；刷新线程消费一次后立即静止，不恢复空闲常驻重绘。
- 进入孙代理后只轮询当前精确 run，父页名册会保留进入前快照；返回时不能从文字或 elapsed time 猜状态，
  但可以把当前页面已经收到的、`updated_at` 不旧于父缓存的 typed run row 合回父页展示。下一次正常 Gateway
  快照仍全量覆盖，任务状态、重试、取消和 wake 权威完全不在 TUI。
- 该校准只替换父页面 `subagents` 显示行，不改 active count、Todo、Goal、selection、canonical history 或
  后端 run；因此已结束 child 仍可按 Enter 查看，Esc 仍只停止当前代理，Ctrl+G 仍只返回层级。

## 2026-09-04 能力申请必须声明结构化授权目标【状态：R172 Focused 通过，真 TUI 待复验】

- 能力申请的 `problem`、`expected_output`、`needed_capability` 只给模型和父级解释缺口，不能作为授权对象。
  对照 会话运行时 `会话运行时-rs/core/src/tools/handlers/request_permissions.rs`，空 permissions 在进入审批前即失败；
  my-agent 同样要求精确 `requested_tools/requested_mcp_tools/requested_skills/requested_commands`，或明确的
  `path_scope/cwd_scope/network_scope` 至少一项，且在 canonical request 落账前校验。
- path scope 只回答“范围在哪”，不回答“授予什么能力”。因此仅路径、Skill、MCP 或网络申请可以作为 OPEN
  请求交给直属父级，但 routine auto-grant 只有真实工具、命令或 typed shell 请求时才可结算；禁止再写
  `tools=[]` 的 GRANTED，也禁止因此触发无效 context refresh。
- 输入门与自动授权防线复用同一结构化 target 判定，既阻止新空请求，也覆盖旧账/内部旁路。逻辑只读取
  schema 字段，不从自然语言中的 `create_subagents`、`shell` 等词猜权限，不改变逐次危险 ToolCall 审批。

## 2026-09-04 用户从代理详情页停止后必须显式交接直属父级【状态：R176 Focused 与真 TUI 通过】

- 用户控制取消与模型调用 `cancel_subagents` 不是同一个通知时机：后者在当前父模型轮已拿到工具结果；前者在
  另一个 TUI/Web 控制请求中发生，若只写 child `CANCELLED`，正在等待的父级不会凭空得知。
- 对照 会话运行时 `会话运行时-rs/core/src/tools/handlers/multi_agents/close_agent.rs` 的共享状态订阅，以及 终端交互
  `src/utils/swarm/inProcessRunner.ts` 在 abort 后显式通知 idle waiter，当前新增的不是第二套取消，而是唯一树
  取消完成后的控制面终态交接：root child 复用 canonical conversation wake，nested child 只解除 exact
  direct-parent wait 并启动该父级，绝不越级。
- 交接只接受结构化 `CANCELLED`、authenticated thread、`parent_id/root_id` 和 canonical task state；不解析
  页面文字、目标或 final。整树取消先完成，交接失败不回滚已生效的停止，现有恢复巡检继续作为故障兜底。
  模型内取消和 root `/stop` 维持原语义，避免同一事实重复唤醒。
- `CANCELLED` 本身不足以判断是否要抢在兄弟完成前唤醒：父代理自己调用 `cancel_subagents` 已在同一轮看到
  工具结果，应继续按普通已处理终态合批；`attributes.cancel_subagents.source=user_agent_control` 表示外部用户
  改变了一个正在睡眠的父级之 child 集，必须立即标为 direct-parent attention。该来源随 canonical state
  持久化，因此即时交接与崩溃后的周期 reconcile 使用同一裁决，不依赖易失函数参数或中文 reason。
- `.10` R176 已分别在直属 child 与 depth-2 grandchild 的真实 Compact summarizing 阶段按 Esc：候选均写
  `conversation_compaction_superseded/stage=candidate_discarded`，旧 generation/checkpoint 原样保留；直属父级
  分别约 8.4 秒和 3.4 秒恢复，运行中兄弟不受影响，随后 coordinator/main 自然完成。

## 2026-09-04 根/子/孙代理共享同一创建容量事实【状态：R170 focused 与真 TUI 通过】

- R169 grandchild Compact 真任务的 owner 配额明确为 `max_subagents=2/max_active_agents=3`，coordinator 的
  canonical task、runner prompt 和 execution context 也都携带该值，但递归 `create_subagents` 仍一次落盘并
  启动四个下级。根因是根创建在 `orchestration_tools` 内读取 session/owner/task/per-call 容量，递归创建却
  直接进入 hierarchy scheduler，只检查调用参数里的 per-parent `max_children`；不是模型没读懂提示，也不是
  task 快照丢失。
- 对照 会话运行时 `会话运行时-rs/core/src/agent/registry.rs`：同一用户 session 的 `AgentControl` 克隆共享唯一
  `AgentRegistry`，任意深度 spawn 都先 `reserve_spawn_slot`，容量失败不会创建 thread；
  `agent/control/execution.rs` 再单独限制并发执行。my-agent 保留 durable owner/run 事实源和 owner-local 文件
  事务，但把已有容量计算迁到唯一 `agent_core/orchestration/capacity.py`，根与递归工具均在 materialize 前
  调用。它仍按当前 conversation task 统计 session tree，管理员 owner policy 统计全 owner，终态释放槽位。
- 超限批次原子返回 `SUBAGENT_CAPACITY_EXCEEDED + effect_outcome=not_started`，不静默截断、不部分创建；
  storage/lineage 不可读继续 fail closed。显式 `subagent_hierarchy_max_children_per_tool_call` 的精确参数错误先于
  总容量回执，避免模型失去原有修正线索。容量只读结构化配置、task lineage 和 canonical status，不解析
  goal、role、展示名或模型回复，也不改变 Compact、父子唤醒和 runner 并发语义。
- `.10` 唯一 Gateway 真 TUI `ma-r170-110-nested-capacity-r2` 已证明递归四项请求在仅余一槽时整批
  `not_started`；模型随后等待终态释放并发起新的单项请求，是正常的新事务，不是原批次被静默截断。
  `ma-r170-110-grandchild-compact-r2` 另以充足结构化配额创建五名 depth-2 grandchild；其中一个独立 thread
  自然 Compact `120,065→69,952`、generation 1 后继续工具调用并 DONE，五名孙代理收齐后 coordinator 与 main
  逐层自动恢复和 final。由此容量、孙代理 Compact、TUI 插话和 durable wake 分别有真实结构证据；
  child/grandchild Compact 中途取消仍是独立边界，不能由本条自然成功推断。

## 2026-09-04 Transcript Compact 与普通模型轮共用缓存面【状态：Focused 与三层真 TUI 通过】

- R166 只收口了运行中工具 IR 的 live Compact。对 R167 长会话逐个物理请求核账后发现 transcript generation 1
  虽正确从约 103.9k 压到 10.5k，但摘要请求自身约 108k 输入没有 cache-read；根因是 `_summarize` 把旧摘要、
  操作证据和完整 transcript 重新串成一个巨型 prompt，未携带普通轮的 system/tools/native messages。
- 会话运行时 `会话运行时-rs/core/src/compact.rs` 克隆完整历史并在末尾追加 synthetic compact user input；终端交互
  `src/services/compact/compact.ts` 与 `src/utils/forkedAgent.ts` 明确复制 parent system、user/system context、
  tools、model、完整 messages 与 thinking，避免约 98% 缓存丢失。my-agent 不复制其 session 实现，而是在
  `conversation/compact_provider_surface.py` 复用正常运行的 tool snapshot、PromptBuilder、native tool resolver
  和 provider system instruction，冻结一次 immutable cache surface 后供所有 transcript 候选使用。
- previous summary 使用普通下一轮相同的 `CompactionSummary` envelope，后接待压缩 canonical native messages；
  synthetic 摘要指令是唯一 volatile suffix。Gateway、后台 main、child/grandchild 与手动命令都传结构化
  allowed-tools/prompt-files/system/context scope。若 provider overflow 发生在 `tool_search` 后，临时 Schema 只由
  carried tool archive 的 typed envelope 恢复；普通成功轮已消费的 Schema 不跨轮复活。
- 工具 Schema 只用于缓存 key，auxiliary summary 没有 handler/工具循环；任何 ToolCall 都丢弃模型正文并用
  bounded mechanical fallback。R169 已分别证明 main 自然/手动与直属 child，R170 再以 exact depth-2
  grandchild 证明 generation、cache-read、动画、压后续跑和嵌套 durable wake。主代理成功仍不被当作其它
  层级证明；child/grandchild Compact 中途取消继续单列。

## 2026-09-04 Shell 前像只保护交付物并在权威结算后回收【状态：Focused 通过，真 TUI 待验】

- R167 真 TUI 长调研在同一 owner 内跑 8 个子代理时，`data/artifact_backups/v1` 很快出现约 29 份
  `operation.json`；抽样一条普通 `run_command` 就包含 11 个前像，内容全部是先前的 `tool_search`、
  `create_subagents`、`task_progress` 等 `tool_output` 归档。后续每条命令继续复制累计归档，I/O、磁盘和清单
  大小近似平方增长。这不是 MiniMax 慢，而是 artifact registry 把“可读取的完整工具回执”误投影成了“用户
  交付物”。
- `tool_output` 仍留在 canonical registry，供 `read_artifact`、Compact 和恢复按 ref 定位；登记时新增结构化
  `artifact_role=tool_output_archive`、`shell_preimage_policy=exclude`。shell 前像选择先尊重显式 include/exclude，
  再兼容旧 `kind=tool_output` 行；未知的新 kind 默认继续保护，符合开放世界合同，绝不靠路径或中文文案猜。
- 会话运行时 `会话运行时-rs/core/src/tools/registry.rs` 在 handler 结束、生命周期终态发出之后才进入完成记录，并明确
  “PostToolUse 只能拒绝结果，不能倒改已经完成的执行”；终端交互 `src/utils/fileHistory.ts` 也把版本前像放在
  专门的 session file-history 状态，而不是把工具回执混进用户文件历史。my-agent 适配为通用
  `on_operation_settled`：只有权威 ToolOperation 成功写入 succeeded/failed，工具才能回收私有 crash-window
  清单；回收失败只记维护债务，不推翻已经落库的结果，幂等 replay 会再次触发回收。
- `run_command` 的 changed/invalid 前像 blob 仍由 artifact registry 的 opaque `backup_ref` 长期引用；结算只删
  `operation.json` 和无变化临时 blob。ActionPolicy 判为 read-only 的调用不进入 ToolOperation，handler 会在返回前
  直接清理；mutating 调用等待账本结算通知，UNKNOWN 保留清单供重启核对。由宿主内部布尔字段
  `__operation_managed` 传递这项事实，模型参数不能伪造。
- focused 覆盖：新旧工具输出不进入前像、显式 include 可覆盖、未知格式默认受保护、普通直调零残留、变更
  blob 在结算后仍可恢复、成功与幂等 replay 均通知一次、归档登记元数据可追溯。真机下一步用同一 Gateway 的
  fresh MiniMax-M2.7 TUI 连续产生工具回执和 shell，核对每条命令前像数量不再随工具调用数增长。

## 2026-09-04 Shell 产物备份迁出任务树【状态：Focused 与 R167 真 TUI 普通链通过】

- 旧 `snapshot_ready_artifacts(workspace_root)` 在每条前台 shell 启动前，把全部 ready 产物按原文件名复制到
  `<workspace>/data/artifacts/shell_backups`。因此已登记的 `test_*.py` 会在同一条 pytest 开始前出现于项目树，
  被第二次收集；reconcile 对 unchanged 直接跳过，又让每条无修改命令永久留一批副本。
- 终端交互 `src/utils/fileHistory.ts` 把备份放在用户配置根 `file-history/<session>`，文件名为路径哈希 + 版本，
  不保留原扩展；会话运行时 `会话运行时-rs/core/src/shell_snapshot.rs` 虽保护的是 shell 环境而非项目文件，也把快照放在
  会话运行时 home，并采用同目录临时文件、验证后 rename、Drop 删除和三天漏件清理。my-agent 保留自身的 ready
  产物保护能力，只适配两者共同的“项目外内部存储 + opaque 名称 + 明确生命周期”，不靠 pytest 排除规则。
- 唯一物理根为 `HomePaths.owner_artifact_backups_dir = owner_data_dir/artifact_backups`。它从 core 经
  `ToolRegistryParams`、bootstrap、`ShellToolOptions` 显式注入，不能从 cwd 或 `owner_scope_root` 猜；因此本地
  管理员 Full Access 即使在 owner home 外工作，备份仍回自己的 owner store。裸 ShellTool 仅使用项目兄弟的
  测试/嵌入 fallback，正式 Agent 始终提供 canonical path。
- 每次前台调用用宿主注入的 run scope、tool call id 与随机 nonce 派生 operation key；产物以 artifact/content
  SHA-256 命名 `.blob`。写入过程为私有目录、0600 临时文件、流式 hash、fsync、atomic replace；公开账本只存
  `owner-artifact-backup:v1/...`，统一 resolver 拒绝绝对路径、`..` 和 symlink 越界。shell 后 unchanged blob
  立即删除；权威 ToolOperation 结算后清掉临时 operation 清单，changed/invalid 的恢复前像仍按 registry ref 保留，
  unknown 则保留清单继续核对。
- 当前范围只覆盖既有前台 shell 合同。受管后台 shell 在启动返回后仍持续运行，不能在启动回执时假装已完成
  post-check；其 artifact lifecycle 需与 process session 的真实退出事件另做一片。focused 已覆盖真实 pytest
  collect-only 只收一个 node、no-op 零残留、损坏/修改保留原内容、owner 隔离、Full Access 外部 cwd、ref
  traversal/symlink、atomic rename 失败前命令未启动及 ToolRegistry/HomePaths 相邻回归。

## 2026-09-04 Compact 真实最低水位与主请求缓存面复用【状态：Focused 与 `.10` 真 TUI 通过】

- R163 的浅代次不是假 Compact，而是规划器用空 `tool_ir_history` 估算“最低水位”，把实际不可删除的
  `UserTurn`、`RuntimeFactsTurn` 与 carried summary 也当成会消失。规划现改为在副本上调用真实
  `compact_native_ir_to_token_budget`，仅删除可由摘要覆盖的完整 ToolCall/ToolResult 与对应 assistant turn；
  副本不写窗口、不推进 generation、不改变 active turn。真实 floor 已吃满 recovery target 时直接交给
  transcript Compact，不先付一次必然只释放少量空间的 live summary。
- 会话运行时 `会话运行时-rs/core/src/compact.rs` 先克隆完整 history，再把 synthetic compact prompt 追加到同一回合；
  终端交互 `src/services/compact/compact.ts` 与 `src/utils/forkedAgent.ts` 进一步证明 compact fork 必须保持
  parent 的 system、tools、model、messages prefix 与 thinking 配置，不能另设输出 token 破坏缓存。my-agent
  适配为：沿用本轮 `CacheStructuredPrompt`、完整 provider history/current IR、同一 system 指令和工具 schema，
  只把摘要要求追加到 volatile 尾部；不覆盖 backend 输出预算或 thinking 配置。
- cache-safe Compact 是一次性辅助模型调用，没有工具执行循环或 handler 权限。工具 schema 只为保持缓存 key；
  provider 若仍返回 native tool block，结果直接弃用并从 typed IR 生成机械交接，绝不执行。辅助调用账本的输入
  估算也纳入 system/tools，避免 Compact 成本继续少记。
- provider 空/非法摘要的机械替代正文不再借用 12K 摘要输入预算，独立封顶 4K，保留任务、旧摘要线索、失败、
  未决项和最新调用的 head/tail。低于 90% trigger 的有效候选仍照常提交；本轮没有恢复“必须压到 60%”第二硬门。
- 77 项语义摘要/native IR 全集与 196 项 Compact/cache/provider 相邻 focused 通过。R166 `.10` 单 Gateway、
  MiniMax-M2.7 真 TUI `ma-r166-110-compact-cache` 进一步完成 8-child 长调研：main 提交 2 代、轻量运行时 child 提交
  1 代；child live-tool 为 `118,696→73,217`，main live-tool 为 `85,161→726`，三代均有 started/progress/
  completed 动画后继续原任务并最终收口。请求级 provider 账本持续出现 cache-read（轻量运行时 child 18 次物理调用
  累计 733,008；main 各后台片分别累计 222,475/231,647/249,956/319,539），没有 Compact 工具执行或失败。

## 2026-09-04 历史任务候选与续作占位工作区收口【状态：Focused 与 `.10` 真 TUI 通过】

- 普通终态任务仍不自动成为下一回合 cwd。Gateway 只把同 owner、同 thread 最近四个已完成且真实存在的
  canonical task-path 作为有界结构化候选放进动态尾部；模型判断当前请求是否相关，宿主不解析“前面/上次”
  等措辞、不自动选择目录。真正读写仍经过 owner wall、workspace roots 与 exact mutation rebind。
- 历史候选按 canonical path 去重，排除当前 request/task、detached、非完成、已删除与 owner 外路径；只携带
  exact task id/path/status 和 160 字目标预览。候选不足仍可调用 `session_search`，因此这不是第二份任务索引。
- exact successor 已完成 link supersede、thread sticky、request binding 与 Todo 迁移后，才尝试删除本轮懒建的
  占位目录。删除器只接受 `<owner_home>/tasks` 内、`work/run_workspace.json.task_id` 精确匹配且整棵目录仍为
  `activate_run_workspace` 原始脚手架的路径；任意用户文件、陌生目录、软链接或身份冲突都保持原样。审计用
  task link 继续以 `superseded` 保留，目录整洁不靠抹掉历史记录实现。
- R165 `.10` 单 Gateway + MiniMax-M2.7 真 TUI 在同一 session 完成星河日志分析器、无关云尺目录体检器，再以
  自然表达续作星河。三轮分别通过 23、17、26 项定向测试；第三轮命中原目录，最终 task successor 为
  completed、占位 link 为 superseded，而物理 `tasks/` 只有两个业务目录。主 Conversation Compact 在三轮中
  从 1→2→4，最终回复均直接显示。

## 2026-09-03 子代理溢出正式记代与 Compact 有效候选边界【状态：Focused 与 `.10` 真 TUI 通过；浅压缩成本待优化】

- R162 `.10` 长子代理实际运行 1:34:33、上下文约 106k，界面仍显示 `compact 0`。读取 exact child thread 与
  transcript events 后确认不是 TUI 漏记：该线程 `compact_generation=0`、checkpoint 为空，23 次 operation
  全部为 `started -> summarizing -> measuring -> superseded`，没有一次 completed。典型候选从 120,995 降到
  104,473、从 128,212 降到 110,070，已经低于 115,200 触发线，却因高于 76,800 的 60% recovery target
  被整体回滚；新 `Agent.run` 又靠有界 handoff 视觉降量，形成“上下文变小但没正式 Compact”的账外续跑。
- 对照 会话运行时 `会话运行时-rs/core/src/compact.rs`：摘要成功后直接替换历史、持久化 compacted item 并在同一 active
  turn 继续；对照 终端交互 `sessionMemoryCompact.ts`、`compact.ts` 与 `query.ts`：只有压后仍达到/超过自动
  trigger 才拒绝，`willRetriggerNextTurn` 只是遥测，不撤销有效摘要。由此统一合同为：60% 继续作为裁剪优选
  目标；`after < trigger` 即可 checkpoint/CAS；`after >= trigger` 才回滚。transcript 会先尝试各 tail partition
  达到健康目标，均做不到时提交其中最小的 `< trigger` 候选。
- child provider overflow 不再把“新工具记录/新 handoff”冒充 Compact 进展。它先尝试自己的 transcript
  Compact；若当前未结束回合尚未写入 transcript，则把 exact carried active-turn archive 写入同一 child
  ConversationThread checkpoint/CAS，确认 generation 递增后才以原 attempt 续跑。完整 archive、工具幂等、
  user steering 与最终完成权不变；TUI 继续只读 canonical generation，不新增显示账本。
- focused 覆盖三条边界：`recovery < after < trigger` 的 transcript/native 均提交 generation；`after >= trigger`
  保持回滚；child completed transcript 为空时由 active-turn archive 正式推进 generation 后续跑。真实验收仍需
  fresh MiniMax-M2.7 长 TUI 证明进度动画、child `compact 0 -> 1`、同一任务继续和最终 Working 收口。
- R163 `.10` 真 TUI 已闭环：8 名 child 全部 DONE，DeepSeek child 的 generation 1 为
  `118,383→71,087`；main 五代严格按 `0→1→2→3→4→5` 提交，47 个 live-tool source id 互不重叠，最终报告
  与 Working 都自然收口。gen1/4/5 虽是真实新代次，但分别只降到 113,207/105,396/112,440，说明提交语义
  已正确而最低水位估算、机械 fallback 长度和 Compact 摘要缓存复用仍有真实成本优化空间；该项另留
  ROADMAP，不把有效代次回退成“达不到 60% 就丢弃”。

## 2026-09-03 Exact workspace rebind 携带当前 Todo 账本【状态：Focused 与 `.10` 真 TUI 通过】

- R161 真 TUI 证明模型最终 `task_progress` 已携带原 `q1..q5` 与 `done`，失败并非模型漏字段。当前请求先在
  占位 task-path 建账，显式历史写路径随后把 conversation task 绑定到原项目；`progress_ledger_id` 立即按新
  task-path 寻址，导致同一回合原 id 在目标账本里看起来不存在。
- 修复只发生在 exact structured workspace rebind：把源 task-path 当前 display generation 的软计划合并到
  目标 task-path canonical 账本，复用既有 exact-id merge 与 display generation 规则；目标持久成功后移除源
  `progress.json`，原始工具调用仍留在 tool-output/conversation archive。它不解析 final、测试输出或标题，
  不替模型打勾，也不让 Todo 取得任务完成权。若源账本不是本 request 的 display generation，则不迁移。
- 迁移发生在目标 successor 已有 canonical task-path、thread workspace pointer 尚未切换的边界；目标写入和本代
  item/display generation 复核成功后才清理源文件。I/O 失败作为
  `conversation_task_progress_rebind` 结构化诊断留在当前 attrs，但软清单失败不取得阻断业务写入的权限。
  会话运行时 本身无需这层适配，因为 `update_plan` 直接归当前 turn；该扩展只服务 my-agent 已有的跨回合计划账本。
- R165 同一 TUI 的第三轮先在占位目录建本代计划，再回绑原星河目录并自然收尾；最终回复直接显示，源占位
  目录删除而 successor completed，证明计划身份没有因 task-path 改变而卡在旧账本。

## 2026-09-03 Full Access 设备挂载与历史任务引用补全【状态：Focused 与 `.10` 真 TUI 均通过】

- Full Access 继续经过统一 attempt sandbox，但 Linux 根目录 `--bind / /` 后必须用
  `--dev-bind /dev /dev` 重新开放真实设备树。RHEL/SELinux enforcing 测试机证明：普通根 bind
  会继承 nodev 语义，导致 `/dev/null`、`/dev/zero` 等即使是 0666 也返回 EACCES；这不是 pytest
  或模型问题。对照 会话运行时 `linux-sandbox/src/bwrap.rs` 的“根挂载后再挂 `/dev`”层次，my-agent
  选择 bwrap 专用 `--dev-bind`，只作用于已经取得管理员 Full Access 的分支，不扩大 WorkspaceOnly。
- 普通终态任务仍不自动吸附下一回合。为使“回到上次那个项目”能由模型聪明地找到精确目录，Gateway
  completion 索引保留宿主生成的 `conversation_runtime`；`session_search` 只给
  `gateway_request` 投影 typed `task_ref(request/thread/task/path/status)`。模型可按需检索并把绝对
  `task_path` 交给既有写工具精确回绑，机器仍不解析用户措辞、不从标题或回答猜 cwd。
- 两项均保持一个权威来源：设备权限仍由 SandboxSpec/bwrap argv 裁决；任务身份仍由 Gateway request
  与 ConversationStore 裁决，LocalStore/task_ref 只是 owner 内只读搜索投影。旧记录可由既有 Gateway
  index rebuild 从 done request 补齐；R165 后续增加的最近四项动态候选也只读取相同 task link，不建立第二份
  任务目录状态。
- R156 真 TUI 证明 typed `task_ref` 本身正确，但真实“回到刚才那个……”措辞下，动态工具推荐把
  `session_search` 排在普通文件工具之后，MiniMax-M2.7 没有调用历史工具而是遍历 owner 目录，多个同类项目
  时选错。R157 只补齐 `ToolModelHints` 的“刚才/先前/回到/原项目”检索语义和带
  `source_type=gateway_request` 的示例，使历史工具在该类自然措辞下排首位；仍由模型选择命中项，宿主不解析
  用户正文、不自动恢复 terminal sticky，也不把任务菜单常驻注入每轮 prompt。
- R157 真 TUI 进一步证明模型已会调 `session_search`，但默认前 5 条被普通聊天和当前
  `processing` 请求占满，排在第 6 的已完成任务 `task_ref` 仍未进入模型视野。R158 在本地最多取
  20 条候选，只依据 host-authored 的“终态 Gateway + 合法 task_ref”稳定提前；每组内仍保持 FTS
  相关度顺序。`queued/processing/running` 占位请求不再投影成历史任务。这不是用户文本分类，
  也不改变写入授权与 exact-path rebind 的二次裁决。
- R158 真 TUI 已让 typed task_ref 稳定排第一，但 MiniMax-M2.7 将其复用成 owner-relative
  `tasks/日期/任务名`；旧规范化器只在该地址恰好等于“当前占位任务”时还原，否则把它再拼到当前
  cwd 下形成 `current/tasks/...`。R159 让唯一参数规范化入口从 host-authored
  `effective_owner_scope_root` 还原任意合法 owner-local `tasks/...` 地址；还原只解决地址，后续 owner 墙、
  allowed-read/write roots 与 exact mutation rebind 仍独立裁决，因此不会把历史路径变成写授权。对照 会话运行时
  `TurnContext/TurnEnvironment` 的绝对 cwd 机器字段，界面脱敏值不作为路由权威。
- R159 真 TUI 暴露 local/main 管理员开 Full Access 时，安全层会正确省略
  `effective_owner_scope_root`，但地址规范化错误地也依赖这个安全墙字段，因而管理员仍把 `tasks/...`
  嵌套到当前 cwd。R160 将宿主 `HomePaths.owner_home_dir` 作为独立
  `canonical_owner_home_root` 写进每轮工具边界：前者只还原地址，后者继续表示实际 owner 安全墙。
  Full Access 不会因此被收窄，WorkspaceOnly 也不会被放宽；对照 会话运行时 将 typed turn cwd 与 sandbox
  policy 分层保存，而不是用“是否有安全墙”推断 cwd。
- R160 真 TUI 的双参数归档进一步证明：模型给的是合法历史绝对路径，错误发生在工具轮开始前的
  `conversation_rebase_from_task_root`。R161 将 rebase 明确定义为“占位 cwd 重定向”而非“重写所有 source
  后代”：当 target 本身位于 `<source>/tasks/...`、从而结构化证明 source 是 owner home 时，任何已经位于
  `<source>/tasks/...` 的绝对地址保持不动；其他 owner 普通路径和真正旧 task root→新 task root 仍按原规则
  重定向。该地址保护不产生权限，后续 ActionPolicy、owner wall 与 exact task link 继续独立裁决。
- R161 `.10` 真 TUI 已完成“新建 A → 新建 B → 自然续作 A”的同会话闭环。续作 request 先物化的占位 task
  进入 `ABANDONED`，原 A 目录生成同 request id 的 `-continue-*` successor 并进入 `DONE`；业务修改和
  30 项 pytest 都只发生在原 A，未产生嵌套 `tasks/`。这证明地址保护与 exact mutation rebind 已闭环，
  但不等于占位壳的目录保留策略已经验收；后者单独进入清理/投影设计，不通过删除审计事实来美化目录。

## 2026-09-02 普通终态任务不再隐式吸附新回合【状态：R155 本机真 TUI/Focused 通过】

- `ConversationThread.workspace_task_id` 收窄为状态/导航投影，不再让 `completed/interrupted`
  普通任务自动取得下一条用户消息的 cwd 权威。新回合只从 exact request-level
  `conversation_runtime`、未结束 `/goal` 或仍 active 的任务继承工作区；否则从 owner home
  起步，首个 `promotes_task` 动作建立新任务目录。
- 明确写入同 thread 旧项目的结构化路径仍可精确续作。裁决先按 canonical
  `task_path` 分组，而不是按历史 execution id 计数：同一项目有多代 terminal link 时选
  最新一代建 successor，本轮早先懒建的影子任务标为 `superseded`；跨多个不同目录或
  同目录有多个 active executor 仍 fail closed。用户说“继续”本身没有机器权威。
- 对照 会话运行时 `会话运行时-rs/core/src/session/turn_context.rs`、`session/handlers.rs` 与
  `session/session.rs`：每个 turn 只从显式 session/turn 环境建 cwd，不从完成正文或历史
  任务猜目录。my-agent 额外保留 owner 隔离下的结构化写路径回绑，不增加文本分类器或
  人工任务选择步骤。
- 本机 MiniMax-M2.7 真 TUI `ma-r155-local-workspace-routing` 在同一会话先把无关 CSV
  工具落到新目录，再把 Markdown 报告续作精确回绑旧文本检查项目；处理请求、thread
  与 successor 三处的 task id/path 一致，临时影子 link 为 `superseded`。

## 2026-09-02 Compact 停止信号贯穿摘要、候选与提交【状态：R154 主代理真 TUI/Focused 通过】

- transcript、active-turn archive 与 native IR 三条 Compact 路径共用一个只读
  `CompactInterruptCheck`。慢摘要前后、内存候选改写前后、checkpoint 前后和 generation CAS 前都检查；
  回调读取失败按停止处理。CAS 已成功后不回滚已提交代次，避免屏幕和持久历史分叉。
- 用户停止是 `superseded/candidate_discarded`，不是 provider/摘要失败：原生 IR 与 tool-context 恢复原值，
  generation/cursor/失败熔断不动；checkpoint 已写时允许留下不可达候选，thread 未引用就没有 live authority。
- 对照 会话运行时 `会话运行时-rs/core/src/tasks/compact.rs` 与 `core/src/compact.rs`：Compact 属于当前可取消 task，模型流
  中断直接返回 `TurnAborted/Interrupted`，成功生成后才替换 history。my-agent 适配自己的 checkpoint/CAS，
  不增加第二份取消状态或回滚已赢 CAS。
- 本机 MiniMax-M2.7 真 TUI `ma-r154-local-compact-cancel` 在
  `active_turn_tool_archive` 摘要 20% 时 Esc：operation 中性收口，request/task 为 interrupted，thread 保持
  generation 4、failure 0；随后同一 TUI 可继续原工作。child/grandchild 的自然真机停止仍留在封板矩阵。

## 2026-09-02 Compact 来源与提交权进入统一进度协议【状态：Focused 通过，真 TUI 待验】

- `conversation_compaction_progress.v1` 新增 `source_kind` 与 `commit_authority`：完整会话历史为
  `conversation_transcript/conversation_thread`，可持久的当前工具归档为
  `active_turn_tool_archive/conversation_thread`，仅本轮临时整理为 `turn_local_tool_ir/turn_local`。
  来源和提交权必须成对校验，不能从 operation id、中文文案或是否出现 checkpoint 阶段反推。
- Gateway、后台 transcript 和 TUI 共同调用唯一 normalizer；缺少两个字段的历史 v1 事件只投影成显式
  `legacy/legacy`，缺一个字段、未知值或矛盾组合全部 fail closed。该兼容只用于展示，不取得持久写权。
- TUI 根据结构化来源分别显示“压缩会话上下文”“整理工具上下文”“整理当前工具历史”。动画仍是只读投影，
  `compact N` 只认 ConversationThread checkpoint/CAS 后的 generation；turn-local 完成不得冒充一次 Compact。

## 2026-09-02 Compact 提交代次与展示操作身份分离【状态：R150 真 TUI/Focused 通过】

- `compact_generation` 只代表 ConversationThread 已成功安装的持久历史代次；`operation_id` 代表一次候选和它的
  进度动画。同一未提交 generation 允许 live-tool 候选放弃后由 transcript fallback 接管，但两个 operation
  不得共享进度高水位或终态。
- TUI 用 generation 拒绝旧代，用 `(generation, operation_id)` 维护本次动画；同 operation 百分比单调，新
  operation 从自身 started 阶段开始，迟到旧事件 fail closed。动画完成、失败或 superseded 都不直接改代次，
  Compact 次数仍只认 canonical checkpoint/CAS 后的 boundary。
- R150 手动 `/compact` 验证动画持续可见、提交后计数 3→4、压缩后记忆连续；live 65%→fallback 5% 与旧事件
  拒绝由 typed focused 覆盖。自然 fallback 仍待真环境触发，不把合成事件冒充真实供应商链。

## 2026-09-02 会话任务终态与执行树终态共同关闭 TaskRun【状态：R148 本机真 TUI 通过】

- `ConversationTaskLink` 的终态只证明用户这项会话工作已经结构化结束，`AgentRun` 树的终态只证明真实执行
  已经停止；两者缺一都不能关闭 TaskRun。模型说“完成”、一轮内的临时属性、Todo 文案和质量验收都没有
  机器终态权限。
- root、child 的普通/异常收口边共同调用 exact TaskRun 的幂等 CAS，只有唯一 root 且整树终态才写
  `closed_at`，最终 status 取 root AgentRun。Gateway 启动发现层以唯一无冲突的持久 task-link 状态重放同一
  CAS，覆盖 link 与 agent tree 先后落盘之间的崩溃窗口；UNKNOWN ToolOperation 不被顺带改写。
- 该边界对齐 会话运行时 的 task 生命周期：执行完成事件负责收起执行状态，而不是外置机器质量判官决定模型任务
  是否“合格”。R148 真 `/goal` 中 `update_goal(complete)` 先成功、root 后结束，最终只产生一条
  `task_run.closed`；持久 Goal 状态、AgentRun 与 TaskRun 三层一致。

## 2026-09-02 已绑定 attempt 后只有一个异常收口边界【状态：R143 本机真 TUI 通过】

- 权威 RuntimeDB attempt 一旦绑定，后续 skill snapshot、工具运行快照、上下文准备、provider 能力探针、模型
  循环和 finalization 都属于同一物理执行片。任一阶段异常都必须关闭 exact current attempt；不能因异常发生在
  “首个模型 token 之前”而让 TUI/请求终态与 run 账本分裂。
- 失败分类继续只读异常类型与结构化控制：`InterruptedError` 对应 cancelled，其余未处理异常对应 failed。
  provider transient retry 仍在后端自己的 typed 有界退避内处理；只有该执行片最终向外抛错时才收口失败。
  不解析错误正文，不增加启动调和补扫，也不重开已失败 run。
- 对齐 会话运行时 `会话运行时-rs/core/src/tasks/mod.rs::start_task/on_task_finished`：task runner 无论正常或错误都从同一 spawn
  边界完成生命周期。my-agent 复用现有 `_settle_main_agent_run_exception` 与 RuntimeDB CAS，把 try 边界外扩到
  已绑定 attempt 的完整执行体，而不是另造 Gateway 错误状态机。
- R143 真 TUI 的缺密钥失败轮已证明 run/attempt 同步 failed、ended_at 非零；恢复配置后的同会话下一轮自然
  done，说明异常收口会释放执行权，但不会破坏 Conversation history 或后续请求。

## 2026-09-01 能力授权读取直属父级精确工具快照【状态：R128 单 Gateway 真 TUI 通过】

- `capability_request` 是 child 请求直属父级已有能力的控制协议，不是具体危险工具调用的用户审批。
  grant/deny 必须读取结构化 `capability_request` 与宿主生成的 `parent_tool_authority`；只要 exact 工具名位于
  直属父级权威快照内，父级可以自主 grant，同一 AgentRun 续跑。child 随后真正调用危险工具时仍进入既有
  ToolCall approval，不能把 capability grant 冒充用户批准。
- 根 main 创建 child 时，`create_subagents` 的 scoped Tool Gateway handler 从当轮不可变
  `ToolRuntimeSnapshot` 生成 `direct_parent_tool_authority.v1`，通过 ContextVar 只写入 child canonical
  attributes。该事实不能由模型参数伪造，也不进入公开 tool payload、operation contract 或幂等摘要。嵌套
  parent 则从其 exact runner execution context 重建当前有效工具，包含历史普通/MCP grant 与禁用策略。
- 普通工具和 MCP 工具在 grant 审计中保留各自字段，但下一 runner 的 provider 工具快照与 durable
  `task.allowed_tools` 必须同时合并二者，使同一 run 能调用获批 MCP，也使孙代理只能沿父级当前 authority
  继续减法。父快照缺失、parent id 漂移或请求工具不在快照内一律结构化 fail closed；旧任务才保留带
  `PARENT_CREATION_SNAPSHOT_MISSING` 警告的进程级兼容来源。
- 该边界对齐 会话运行时 `multi_agents.rs` / `agent/control/spawn.rs` 的“child 继承父 turn effective config、
  approval policy、sandbox 与 cwd”，并参考 终端交互 `runAgent.ts` 的 worker 工具池与
  `swarmWorkerHandler.ts` 的具体 ToolUse permission 上送；没有新增自然语言审批判官或第二套工具注册表。
- R124 真 TUI `ma-r124-110-main-capability-approval` 保留为首个失败基线：child 请求两个 Computer Use 工具，
  父级没有创建时 authority 而错误拒绝。R125 已证明快照成功落盘，同时暴露两项后续主链缺口：模型在
  `requested_mcp_tools` 填写唯一工具短名，而 provider 注册名带 `mcp__server__tool`；旧语义 Router 又在父级
  wake 前把无 card 命中误关成 GAP。当前只对 MCP 字段做“父快照内唯一、末段完全相等”的结构化规范化，
  重名/未知不猜；直属父级能授予全部 exact 工具时，Router 只记录 `PARENT_RESOLUTION_REQUIRED` 并保持
  OPEN。R126 又证明 MiniMax 可能把 MCP 短名放进普通 `requested_tools`；当前实现对两个申请字段统一执行相同
  唯一精确归类，只有父快照内唯一 MCP 末段命中才移入完整 `requested_mcp_tools`。
- R128 真 TUI `ma-r128-110-main-capability-approval` 已完成闭环：child
  `subagent-1788293169-474f3c3b` 的 `capreq-1788293189-ab042cc0` 被直属 main 自主 grant 为
  `capgrant-1788293213-1dc31102`；request 中四个 Computer Use 短名均规范为完整 MCP 名，父快照显示全部
  grantable。原 run 从 `runsess-...-1788293174856-155690` 续到第二片
  `runsess-...-1788293223305-155866`，没有换 child。随后 `take_screenshot_with_ocr` 仍产生独立
  `approval:053e0842c70dd2439b6ed55d`，用户只允许一次后才执行；runtime gate ledger 为 APPROVED，真实
  `list_windows/get_screen_size/take_screenshot_with_ocr` trace 全部成功并返回 OCR。由此证明 capability grant
  与危险 ToolCall approval 已分账，模型终屏不作为验收依据。

## 2026-09-01 普通 turn、显式 Goal 与 TUI 展示必须分层【状态：R121 最终 wheel 真 TUI 通过】

- 普通 turn 与显式 ThreadGoal 是两种不同产品语义。普通 turn 由模型自然回复后结束，open Todo、工具轮上限、
  completion 文案或历史 `ordinary_task_resume` policy 都无权在后台再调用模型；旧 policy 只做 typed retirement。
  只有用户显式 `/goal` 建立的 exact thread goal 才可跨轮持续执行、暂停、恢复或清除。直属 child 等待、明确
  user guidance、Gateway crash recovery 与 provider transient retry 各自继续读取结构化 wake/attempt/error，
  不能因为删除普通自动续跑而被文本兜底替代或一起删除。
- Goal 是固定 TUI 的只读投影，不是 conversation task、Todo 或 child。selection id 固定为 `goal:<goal_id>`，
  与真实 AgentRun id 分离；方向键可以选中，`Enter` 只展开 exact objective/状态/预算，绝不切换 active run 或
  执行控制。`/goal ...` 原文只在 durable control outbox 保存成功后显示为本地 user-shaped block；显示文本不进
  模型消息、不参与路由，也不能替代 outbox/Gateway receipt。完全重启后的命令回看只能由 canonical command
  history 另行设计，当前不能把进程内显示冒充跨重启持久化。
- live thinking 为当前工作反馈，必须实时展开；closed thinking 属于历史细节，默认只显示灰色摘要，`Ctrl+O`
  才临时展开全部。折叠只影响 presentation，typed block、事件顺序、canonical transcript、Compact 和 provider
  prompt 均保持原样。assistant final/commentary 仍直接显示，不能跟 thinking 一起藏起来。
- Goal 的 active 状态只授权 idle continuation，不是消息可见性门。对齐 会话运行时
  `会话运行时-rs/ext/goal/src/runtime.rs::continue_if_idle`：自动 continuation 仍是一轮正常 turn，模型产生的 terminal
  assistant item 必须进入同一 rollout/transcript；一轮结束后 Goal 可继续 active。my-agent 因此只用 typed
  lifecycle/capability reason 隐藏内部控制流，不再用 `goal.status=active` 抑制 `thread_goal_continue` 的整轮 final。
  这条规则不从正文判断“是不是阶段汇报”，也不让 notice/ring 成为第二份历史。
- ThreadGoal 不要求在创建时物化 task workspace。对齐 会话运行时 的 thread overlay 语义：exact active Goal link
  的 `task_path` 为空时，后续前台 turn 继续使用 thread 当前 cwd，并保留 Goal 管理事实；只有一个曾经配置为
  非空的 sticky workspace 路径后来消失，才属于 `CONVERSATION_PERSISTENCE_UNAVAILABLE`。空路径与丢失
  路径必须由结构化字段区分，不能用“目录不存在”这一条宽泛判断把纯聊天 Goal 锁死，也不能因此放宽真正
  工作区损坏的 fail-closed 门。
- 等待中的非终态 child 接受插话时，在同一 owner/run 的短 admission 锁内读取 canonical attempt、原子复用或
  预留一个 pending successor、幂等写 guidance，再非阻塞启动。锁不能包住 provider 调用，不同 owner/run
  不互堵；stable message 重放只返回原 receipt，终态 child 只读。模型消费后形成 typed reply obligation，
  避免“思考里答了但用户看不到回复”。

## 2026-09-01 Computer Use 只复用成熟执行器【状态：底座与真 TUI 已完成】

- Computer Use 的截图、坐标、点击、键入、等待和窗口发现不在本项目重复实现。对 Agent-S、UI-TARS
  Desktop、OpenACI、open-computer-use、computer-use-mcp 与 computer-control-mcp 做源码、许可证、发布形态和
  依赖审计后，选择 MIT/PyPI 的 `computer-control-mcp==0.3.13` 作为官方 `computer-use` 可选运行依赖；
  my-agent 只生成 MCP profile，不复制其 PyAutoGUI/RapidOCR/ONNX 核心。上游未注册滚轮，启动入口只复用
  同一 PyAutoGUI 的公开 `scroll()` 补一个 typed tool，不另造执行引擎。详细边界见
  `docs/design/computer-use.md`。
- 适配层必须把每个动作投影为 typed ToolCall/ToolResult，带 owner/thread/run/operation id，并复用现有
  capability、危险操作审批、取消、超时、副作用幂等和审计。Full Access 仍只有管理员显式拥有；
  GUI 能力不能绕过文件 owner 墙或替用户自动批准。浏览器页面优先 Browser 工具，Computer Use 只补浏览器
  工具无法覆盖的系统 UI。当前 MiniMax-M2.7 只消费文本，真实动作采用 OCR 坐标闭环；原始 MCP image 尚未
  进入 provider 多模态消息，不能冒充“模型已经看图”。
- 第一轮真 TUI 证明“工具已注册”不等于“模型首轮可见”：旧默认把全部 MCP 放进 `mcp` 延迟分类，
  MiniMax-M2.7 没有 会话运行时/终端交互 原生 Tool Search 引用能力时不会可靠加载。对照 会话运行时
  `core/src/mcp_tool_exposure.rs` 的 `search_tool_enabled ? Deferred : Direct`，以及 终端交互
  `services/api/模型助手.ts` 的 model-aware `isToolSearchEnabled` 后，本项目允许部署者为单个 MCP server 声明
  `catalog_category`。默认仍是 `mcp`；官方 Computer Use 固定为 `computer_use`，首轮直接可见。该配置只影响
  provider 工具目录和检索提示，不能改变 owner、workspace、effect、approval 或 Tool Gateway 权限快照。
- `.10` 最终以 MiniMax-M2.7、单 Gateway 和 Xvfb/xterm 验收：模型首轮直接选择 Computer Use，逐笔审批后
  自主完成滚动找码、点击、键入、回车和 OCR 后置验证；120 秒 MCP wait 经 `/stop` 进入同一取消链；普通
  owner 的真实 TUI 不注册该能力。执行器仍保留上游边界：截图 image 未直接进入文本 provider、Unicode
  键入未承诺、上游个别失败仍可能以 `isError=false` 的自然语言返回，所以最终状态必须二次读取验证。

## 2026-08-31 直属父级在继承权限上限内自主裁决，Gateway 恢复扫描不得堵住会话车道【状态：R118 wake/递归真 TUI 通过；capability focused 通过】

- `resolve_capability_requests` 处理的是直属父级给 child 的结构化能力 grant/deny，不是父级给自己提权。
  grant 已经同时经过 direct-parent、owner wall、父级 workspace/write roots 和可用 Skill/Tool 快照校验；只要
  申请没有越过这些上限，父级模型应直接裁决并留下审计，不再把低层目录、工具或孙代理细节推给普通用户。
  跨 owner、父级 workspace 外、父级自己没有的能力和真正危险的系统副作用继续由现有硬门拒绝或上抛，
  不能因父级说“批准”而获得权限。该边界对应 会话运行时 child 继承父 turn permission profile/policy、但不能
  扩大父级 authority 的做法。
- 单 Gateway 已有独立 `_GatewayOrphanReconciler`，负责按 owner 有界扫描死亡 runner 和孤儿恢复。
  background-main 的 `prepare_tick` 不得在提交 ready thread lane 前再次同步执行同一全量扫描；历史 owner/run
  较多或磁盘变慢时，重复扫描会让 durable wake 明明 ready、`bg-owner` worker 却始终空闲。Gateway 路径只做
  model-free 会话准备并提交 lane，独立恢复线程继续持有 crash recovery；非 Gateway 的同步 scheduler 仍保留
  inline orphan supervision 兜底。不能用短墙钟杀 lane，因为健康慢模型可能长时间没有新输出。
- 活着的 TUI 进程（包括仍留在 tmux 的 TUI）属于显式在线客户端，不按空闲时长自动回收；`/exit` 或进程终止
  才释放本地 poller/HTTP/展示资源。IM connector 更不能按“多久没聊天”回收，后续只参考 通道运行时/长期助手 的
  health、lease、reconnect 和明确 shutdown 做长驻验收；无消息不是失活事实。
- coordinator 的递归控制不是临时 capability grant。结构化角色模板若声明 `can_spawn_children=true`，它在父级
  既有 depth/capacity/owner/workspace 上限内直接获得 edge-local `create_subagents` 等工具；这对齐 会话运行时 在
  `agent_max_depth` 内给每层 session 注册 spawn 工具、由 handler 再守深度和容量的方式。builtin 角色 JSON
  属于生产运行合同，标准 wheel 必须携带；缺资源不能静默把 coordinator 降成普通 worker。
- `.10` 的 `ma-d9b70f2-110-u328-capability-wake` 已证明直属 child 完成后 main durable wake 不依赖用户催办；
  `ma-0f6ac38-110-u331-one-coordinator` 又证明生产 wheel 中 coordinator 首轮直接持有递归工具、实际创建两名
  depth-2 researcher，孙代理收齐后 coordinator 与 main 逐层自动恢复并自然 final。三条 child canonical state
  均为 `DONE`，两名孙代理的 `parent_id` 精确指向 coordinator，不能只凭屏幕文案得出结论。
- 递归角色没有经过 capability request，因此 u331 不能冒充“父级处理额外 capability”真机样本。后者已经有
  grant/deny 同为 mutating、越界仍拒绝的 focused 合同；后续 fresh TUI 应选一个不属于角色固有工具面的窄
  能力请求单独验证。供应商两次未按 prompt 建 coordinator 的 u329/u330 也保留为模型遵循负样本，不写专项
  prompt 判官或机器拓扑验收。

## 2026-08-31 TUI 后台回复必须先成为 canonical history【状态：R117b/R117c 真 TUI 通过】

- 对照 会话运行时 `会话运行时-rs/core/src/stream_events_utils.rs` 与 `session/inject.rs`：完成的 assistant response item
  先进入会话历史，TUI event/feed 只是同一事实的展示。my-agent 因而把本地 `channel=tui` 明确定义为
  transcript-capable delivery；后台 main 的 model-authored commentary/final 必须与前台一样先按 exact
  thread/request/wake/part 幂等提交 ConversationStore，notice 只能提示附着客户端读取新消息。
- 该规则不把全部 channel 默认当成本地会话。未知外部 route、没有可靠回送/会话身份的 adapter 继续
  fail closed；delivery status 可以是 `not_applicable`，但只要 authenticated TUI thread 是正文权威，模型
  历史提交就不能因没有 TUI adapter 而省略。重试不能重复 final，也不能让 notice 反向成为历史事实源。
- `.10` R117b u326 在任何追问前直接核对 raw thread，确认 6 条 background commentary 和 1 条 final 已在
  schema v8 messages 中；随后追问准确召回。R117 u325 因旧源码 checkout 遮蔽 wheel 而失败，不能算代码
  回归失败或真机通过；部署验收必须同时核对 Gateway cwd、`module.__file__`、schema 与行为。
- 最终独立 runtime venv 上的 u327 再次在追问前核对到 4 条 background commentary 与 1 条
  `root_subagents_terminal` final；零工具追问准确召回 245/198/290 行与 marker 次数，证明真实部署不是只靠
  u326 历史样本。

## 2026-08-31 provider 上下文观察只是一代内的数值校准【状态：R116 真 TUI 通过】

- provider usage 中的 context observation 只校准 exact backend/model/protocol/system/stable-prompt/tools
  指纹与当前 Compact generation。动态 messages/guidance 不进指纹，避免每轮都失效；但观察不能跨模型、
  system/tools 变化或 Compact 代次复用。CAS 失败、usage 缺失或代次漂移都回退原始估算。
- 该观察不是累计 token、计费或任务完成事实；累计成本仍只读 ModelCallLedger，Compact 次数仍只读
  ConversationThread generation。u323/u324 分别证明低估时不乱压、真实越线时只提交一次再继续。

## 2026-08-31 wheel 运行时不得继承旧 editable Python 环境【状态：R117c 已纠正】

- `pip install` 成功和中性 cwd 都不等于运行进程使用新代码。旧 Gateway 的 executable 仍是
  `/root/my-agent-src/.venv/bin/python`，系统 site-packages 还有
  `__editable__.my_agent-0.3.0.pth`；因此 cwd 已为 `/root` 时仍会导入旧 checkout。
- wheel 验收必须使用独立 non-editable runtime venv，并同时核对唯一 listener、模型配置、进程 executable、
  pip Location/Editable metadata、`module.__file__`、schema 与真实 TUI 行为。源码运行模式则必须明确要求
  checkout 与待验 revision 完全一致，不能混用两种部署身份。默认 `my-agent` launcher 也必须指向同一
  runtime venv，并在替换前保存可回滚 symlink；不能只更新 Gateway 后让用户新开的 TUI 继续走旧客户端。
  托管 service cwd 仍保持中性，但它只是其中一门。

## 2026-08-30 后台连续 Compact 达到公平切片上限时必须干净让出【状态：R114u 已部署，fresh 长任务观察中】

- `.10` 旧长会话 `r114r-u305` 在同一后台 active turn 连续推进 13 代 Compact；旧循环每个 scheduler slice
  最多允许 8 次以避免一个 owner 长时间独占 Gateway，但第 8 次后抛普通 `RuntimeError`。外层因此把健康的
  有进展切片记成 failed claim、输出假错误，再由未消费 wake 偶然续跑完成。上下文没丢，状态和恢复语义却
  错了，慢模型或超长任务尤其容易撞到。
- 公平上限继续保留为 8，不改成无界循环，也不靠增大超时掩盖。第 8 次后改发内部 typed yield；activity 正常
  收口、run claim 以 finished 结束，原 wake/observation/policy 保持未消费，下一 scheduler slice 从 canonical
  checkpoint/generation 续跑。它不增加 policy failure_count、不打印程序崩溃，也不从模型正文判断进展。
- focused 覆盖连续 8 代后抛 typed yield、claim 干净关闭、wake 仍可再取以及普通异常仍失败；本地所有本轮
  变更测试文件通过。wheel `41f752dae9894e57ef5a38fe331a399598f99ff1cdc1616dad56ffffd11138a4`
  已部署到 `.10` 唯一 `ma-gateway-r114u-110-wheel`。部署时 GORM 长任务的 7 个 live child 均按原 run
  reclaim/revive，没有重复创建或假终态；真正自然跨 8 代的 fresh 样本继续观察，不用旧失败倒推新实现通过。

## 2026-08-30 托管 Gateway 的 service cwd 不得绑定安装时源码目录【状态：R114v 已部署】

- 旧 systemd/launchd 生成器把安装命令执行时的 `cwd` 写进服务定义；如果恰好在源码 checkout 安装，后续即使
  wheel 已升级，Python 仍可能优先加载旧 checkout，造成“pip 显示新版本、运行行为仍是旧代码”。这也是本轮
  首个 fresh TUI 误连旧源码 Gateway 的根因，不是模型或配置漂移。
- 生成服务现在统一使用 `<MY_AGENT_HOME>/service-cwd` 中性目录；目录由安装流程建立，只承载进程 cwd，不是
  workspace、owner home 或第二份运行时事实。模型、密钥与用户任务仍从现有 config/owner/task 权威路径解析。
- systemd 与 launchd 定向测试锁定中性目录和参数；`.10` 手动验收也从 `/root` 启动安装 wheel，模块路径为
  venv `site-packages`，8420 始终只有一个 listener。以后重新生成服务后不会再被某个 checkout 阴影覆盖。

## 2026-08-30 任务晋升前不得把 owner 根目录冒充 turn cwd【状态：R114t `.10` fresh 真 TUI 通过】

- `.10` `ma-r114r-110-u305-sequential-long` 的首轮模型提示把 owner home 显示成“当前工具工作目录”。模型据此
  把该绝对路径复制进 7 个 `create_subagents.items[].goal`；工具执行前任务晋升又正确建立了 canonical
  task root。所有 child 因而在正确 cwd 中尝试访问错误的 owner-root 目标，被 `WRITE_FORBIDDEN` 拦截并
  产生 capability 重试。安全墙没有失效，问题是首轮模型视图与随后工具视图不一致。
- 对照 会话运行时 `会话运行时-rs/core/src/session/turn_context.rs`：每轮只有一个 `TurnContext.cwd`，模型相对路径、执行、
  sandbox 与审批都从同一值解析。本项目仍保留“普通聊天不预建任务目录”的既有语义，但 pending turn 不再
  暴露 owner 绝对根为产物 cwd；模型视图只说明读取已有资料使用相对路径，首个结构化工作工具会固定任务
  cwd，之后继续使用相对路径。持久 context bundle 仍保留完整 owner refs，只有短模型投影隐藏这些宿主路径。
- 该修复不扫描用户 prompt 或 child goal，不重写模型自然语言，不放宽 owner 权限，也不为每条闲聊创建空
  task。`run_workspace` 一旦建立，原有 canonical task root 继续成为模型、工具、审批、child/grandchild 的
  唯一 cwd。另将 `gwreq-` / `gwreq_` 纳入机器 ID 识别，任务目录名回退到可读用户标题，不再直接使用请求号。
- 本地 prompting/context-bundle/task-title focused 94 项、Ruff 与 PyCompile 通过。下一步使用 fresh
  MiniMax-M2.7 TUI 重跑多 child 创建任务，核对首批 goal、main/child cwd、产物位置、拒绝数与可读目录名。
- `.10` fresh `ma-r114t-110-u313-cwd-subagents-wheel` 只输入一次原样超级玛丽多子代理任务：首轮模型不再
  看到 owner 绝对根，6 名 child 的 goal 均使用相对目标，main/child 全部固定到同一个可读 canonical task
  root；`WRITE_FORBIDDEN` 与 owner-root 泄漏均为 0。后续同 thread 继续补 README 并准确记住原目录，手动
  Compact generation 1 后仍可召回关卡和路径。

## 2026-08-30 跨工作片 active turn 必须提交真实 Compact【状态：R114r `.10` fresh 真 TUI 通过】

- `.10` `ma-r114o-110-u299-pdf-skill` 的 `/context` 一度显示约 `104.3k/128k`、`compact 0`；同一任务随后
  在没有 Compact generation 的情况下回落到约 66.9k。权威账本里当前 request 已有 73 次主代理工具调用，
  assistant metadata 又携带约 99k 字符 `canonical_native_messages`。因此并非 TUI 计数坏了，而是新的
  `Agent.run` 工作片用有界 handoff 暗中缩短模型视图，却没有把替换边界提交到 ConversationThread。
- 对照 会话运行时 `会话运行时-rs/core/src/compact.rs`：mid-turn Compact 直接用摘要和有界近期 user items 替换当前
  history，再继续同一 turn；对照 终端交互 `src/services/compact/compact.ts` 与 `src/query.ts`：调用方把
  `messagesForQuery/allMessages` 原位替换成 boundary + summary + retained messages。两者都不把“本轮做过新工具”
  当成可以无账重启的理由。
- 当前把跨工作片工具历史拆成两种投影，但仍只有一份事实源：完整 owner archive 永久负责 tool-round 预算、
  一次性派工去重、已执行工具、未知副作用、审计与精确回取；模型可见投影只排除当前 thread 已提交
  `live_tool_ir` checkpoint 链中列出的 exact call ids。未被 thread `compact_checkpoint_id` 指向的孤儿候选、
  其它 thread、自然语言摘要和 transcript checkpoint 都无权隐藏调用。
- Gateway 与后台 main 收到真实 provider `context_overflow` 后，先尝试普通 transcript Compact；若 generation
  没前进且本轮已有工具 archive，则用同一 `LiveToolCompactCheckpointRequest -> checkpoint -> generation CAS`
  生成完整替代摘要、保留有界近期整条记录并继续原请求。摘要复用 live Compact 的六字段校验，并把上一代
  summary 合并成新的完整替代摘要；不追加无界摘要链，也不额外调用一次中段摘要模型。
- “有新工具/有新插话”不再允许直接重试。只有 canonical generation 真正前进才继续；没有可压记录、账本损坏、
  checkpoint 链断裂或摘要失败均 fail closed，并复用同一 Compact 失败熔断与 typed 进度终态。TUI 的
  `compact N` 因此重新等于真实 replacement 次数，普通恢复不再偷偷改变缓存前缀。
- `.10` fresh `ma-r114t-110-u314-architecture-research-long` 的 8 名 child 全部 DONE，main 跨多个后台工作片
  自然写出 367 行横向报告并直接 final。canonical ledger 依次提交 transcript `25.4k→10.2k`、live-tool
  `98.8k→0.9k`、live-tool `17.5k→12.9k` 三代，TUI 同步显示 `compact 3`；完整 archive、八份分报告、Todo、
  child 终态和最终整合均保留。107 次 provider 调用真实记录 317.3 万 cache-read token，证明不是用 Context
  数字冒充缓存。

## 2026-08-30 后台主代理保留 owner 记忆写入口【状态：R114q `.10` fresh 真 TUI 通过】

- 真机 `ma-r114o-110-u300-memory-a` 已证明首轮 `update_persona` 成功写入当前 owner 的
  `USER.md`，但 child 完成后的后台整合轮因工具快照缺少 `remember/update_persona`，随后错误声称
  “长期偏好未保存”。这不是记忆文件或 owner 隔离失败，而是同一 active turn 在前台/后台切片间丢了能力。
- 对照 长期助手 `agent/background_review.py`：后台记忆复盘在 profile 允许时显式保留 memory 工具，且只让
  记忆工具写持久存储；本项目不另造 curator 旁路，而是在现有 owner/task policy 继续收口的前提下，把
  `remember` 与 `update_persona` 加回后台主代理公共工作快照。
- 权限语义不变：普通 memory、`USER.md` 与 `AGENTS.md` 可由当前 owner Agent 自主维护；`SOUL.md` 仍由
  `update_persona` 内部确认链硬守，后台快照不会绕过确认、owner 墙或 tool policy。
- 后台能力不能只测常量名单。R114q 增加从 `_run_params -> ToolRegistry.runtime_snapshot ->
  model_visible_specs` 的整链合同，确保 `skill_search/remember/update_persona` 同时进入策略、冻结执行快照和
  provider schema；协议违规账本也记录该轮 snapshot hash 与 allowed/available 名称，后续可直接区分模型越界
  和装配漂移，不再靠日志猜测。
- `.10` fresh `ma-r114t-110-u316-managed-service-skill-memory` 自然调用 `skill_search`、派 4 名 child、启动
  `0.0.0.0:18086` 受管服务并在两分钟后复查 HTTP 200；后台整合轮把“中文报告、先只读、过程文件不散落根
  目录”三项写入该 owner 的 `USER.md`。其它 owner 无相同内容，SOUL 未改，证明后台工具连续性和隔离同时成立。

## 2026-08-30 TUI Gateway 快速就绪不得丢首帧刷新【状态：R114o fresh 真 TUI 通过】

- 唯一 Gateway 已就绪时，后台 preflight 可能比 prompt_toolkit 首帧更快返回。旧链路在
  worker 线程直接更新 runtime 并 `invalidate()`，事件若落在首次 render 边界会丢失，界面持续显示
  “正在启动交互界面”，直到用户按键才触发下一帧。
- 对照 终端交互 `src/services/analytics/growthbook.ts` 对“快速结果早于 REPL effect 注册”的
  catch-up，R114o 让后台线程只产生 readiness 结果，再经 Application loop 一次性落实
  runtime 终态、worker 启动和 invalidate。这不新建第二份连接事实，也不以定时刷新掩盖竞态。
- `.10` 重新部署后，8 个独立 owner TUI 在已运行 Gateway 下同时 fresh 启动；不输入任何键，
  5 秒后 8/8 都已自动显示 welcome/输入框，未再停在启动动画。

## 2026-08-30 Compact 恢复目标必须留出完整长回合空间【状态：R114n `.10` fresh 长 TUI 通过】

- `ma-r114l-110-u295-compact-research-long` 的八子代理调研最终自然完成，但主代理在约 27 分钟内连续提交
  3 代 Compact。第二、三代分别只从约 119.4k 降到 100.1k、从约 115.4k 降到 94.7k；旧恢复目标只是
  “90% 触发线减 10% 近期尾部”，128k 窗口实际约为 103.7k，一份 1--2 万 token 的整合报告足以马上再次触发。
- 对照 会话运行时 `core/src/compact.rs::build_compacted_history_with_limit`：压缩后以摘要替换旧历史，真实用户消息
  最多保留约 20k token；对照 终端交互 `compactConversation/buildPostCompactMessages`：压缩后只重建边界、
  摘要和有界附件，并显式记录 `truePostCompactTokenCount/willRetriggerNextTurn`。两者都不把“刚低于触发线”
  当作健康恢复。
- 新增唯一配置 `memory_compact_recovery_target_percent`，默认 60、范围 25--80。主代理、子代理、live-tool 与
  transcript Compact 共用 `RuntimeCompactPolicy`；最终目标取“模型窗口恢复占比”和“触发线减完整近期尾部”
  的较小值。128k/90% 默认因此压到不高于 76.8k，为下一段长工具链或报告保留约 38.4k 空间。
- 若已完成会话前缀本身高于恢复目标，live-tool 不再先烧一轮注定贴线的摘要，而直接把旧 transcript 交给
  canonical Conversation Compact；摘要、checkpoint、generation、active-turn 插话与工具事实仍走原权威路径。
  该变化不按任务类型或模型正文判断，也不缩短 raw transcript、Memory 或 artifact 保留。
- R114t u314 的三代各自替换不同 source boundary，提交后的 source projection 都远低于 60% recovery target；
  后续再次增长来自新的八报告读取与 367 行整合，不是同一候选只贴着 90% 触发线反复压缩。最终上下文可回到
  113k 是 final 后完整新正文的只读展示事实，不会倒改已提交 generation 或 cache ledger。

## 2026-08-30 本地文本搜索默认语义对齐 ripgrep【状态：R114m 本地合同通过，待真 TUI】

- R114k 的真实超级玛丽恢复任务中，模型先用 `search_text(query="LEVELS")` 成功，随后按常见 grep 习惯传入
  `LEVELS|level1|...`，但未显式传 `literal=false`，底座把整串当连续普通文字并返回假空结果。工具结构化回执
  本身正确，错在默认合同与模型熟悉的工具语义相反。
- 会话运行时 的本地代码搜索走 `rg`，终端交互 `GrepTool` 和 任务运行时 `grep` 均把 pattern 默认解释为
  ripgrep 正则。当前 `search_text` 因此改为同一默认；需要精确原文时显式传 `literal=true`。rg 与 Python
  fallback 继续消费同一 `SearchRequest`，非法正则、分页、忽略目录、owner 路径墙和联网边界均不改变。
- schema 显式声明 `literal.default=false`，并补充 alternation 示例；focused 覆盖默认 `A|B` 同时命中、显式
  literal 保持普通文字及两条 no-match 结构化合同。该变化待随下一 wheel 进入真实 TUI 复验。

## 2026-08-30 后台主代理 Compact 必须回收完整工具轮并原地续跑【状态：R114l 真 TUI 主链通过，连续压缩转 R114n】

- R114k 的真实恢复任务连续四次出现 live Compact `started -> summarizing -> measuring -> superseded`，每次
  `compact_generation` 仍为 0。账本、checkpoint 与进度投影都没有丢：候选摘要安装后仍高于统一 recovery
  target，因此按合同恢复原 IR。真实问题是旧实现只成对删除 ToolCall/ToolResult，却保留这些调用所属的长
  assistant 思考正文；摘要已覆盖的旧轮因此仍占 provider 窗口，导致“看起来压了、实际上没腾出空间”。
- 对齐 会话运行时 mid-turn Compact 的 replacement history：只有完整替代摘要已经成功生成时，native IR 才允许把
  被摘要覆盖、且最后一个保留 ToolCall 也已移除的 assistant 工具轮整条回收。普通窗口/PTL 没有完整摘要时
  继续保留正文，UserTurn 永远不进入删除集合，工具对仍保持 orphan-safe；候选/CAS 失败仍原样恢复 IR。
- 后台 main 旧实现又缺少前台 Gateway 与 child runner 已有的 transcript Compact 外层：`Agent.run` 返回
  `context_overflow` 后，scheduler 只能等下一次 wake 再开一个工作片，重复生成相同摘要和 Working。现在后台
  同一工作片最多有界重试 8 次；每次只携带结构化 tool archive 与 active-turn user inputs，释放未提交插话，
  强制唯一 ConversationThread 做 checkpoint/CAS，再用新 generation 继续原 user objective。没有 generation
  或工具/插话进展时立即停止，不能空转，也不从模型正文重建状态。
- 这不是第二套压缩：live-tool 与 transcript 仍共用 `RuntimeCompactPolicy`、ConversationThread generation、
  checkpoint、失败熔断和同一 TUI 进度协议。`save=False` 只表示后台回复由外层提交，不撤销 exact
  transcript-authoritative turn 的 Compact 权限。
- focused 已覆盖“长 assistant 工具轮被完整摘要回收并落 generation”及“后台 overflow 在同一 slice 压缩、
  携带已完成工具、刷新 context generation 后成功返回”。`.10` 已安装 wheel
  `e6f8402c9d094185061f48c89c8670054a058d9faa010b7713b05c9573876556`，唯一 Gateway 为
  `ma-gateway-r114l-110-wheel`；fresh `ma-r114l-110-u295-compact-research-long` 以 MiniMax-M2.7 完成八子代理
  长调研。任务运行时 child 把 23 个完整工具对从约 124,049 token 压到约 53,711 token，终端交互 child
  也提交 generation 1；八份分报告与 21,876-byte 横向报告均存在。主代理 final 直接显示、Working 撤下、
  `compact 3` 可见。该链路通过，同时暴露“压后余量太小导致连续三代”的 R114n 问题。

## 2026-08-30 Gateway 重启恢复与冷会话回放【状态：R114j 终态竞态已复验，R114k active-turn 续接待真 TUI】

- `.10` 单 Gateway 顺序重启时，长任务的 durable run/thread/task 都仍在，但测试 owner 位于磁盘发现第三页；
  旧控制器每发现一页就等待完整 `orphan_supervision_interval_seconds=60`，导致 TUI 约两分钟看不到 child
  恢复。恢复动作本身随后成功，根因是分页调度节奏，不是模型、持久队列或 child 状态损坏。
- 对齐 会话运行时 的显式 thread/agent resume 生命周期，并用 长期助手 Gateway 在生命周期入口立即回收 orphan lease
  补足多用户守护进程语义：磁盘发现仍然每个 controller tick 只读一页，保持启动和大用户量有界；但只要
  `next_cursor` 存在，下一 tick 立即继续下一页，只有完整一轮结束后才进入 60/120 秒稳态重扫间隔。
- runner 心跳继续容忍慢模型、GC 与磁盘抖动。唯一例外是 session 明确记录了 `in_process` 形态，且 exact
  worker PID 已被 OS 证明死亡：这个客观事实可以越过仍新鲜的最后一拍立即回收；PID 仍活、字段缺失或无法
  证明时继续等待原新鲜窗，防止双执行与旧账误判。
- 该变化不缩短全局巡检间隔、不在启动线程一次性全量扫描、不新增第二 Gateway，也不从自然语言判断活性。
  `.10` 唯一 Gateway 携四路旧长任务和 fresh 八-child 调研任务顺序重启；u287 的 7 个失联
  runner 在不到 25 秒内原 run revive，名册仍为 8 行、无重复派生。
- 已提交的 assistant final 不能因 owner 被池逐出或 Gateway 重启而从 TUI 消失。被动通知路径
  先查已驻留 Agent；若 owner 仍 cold，只用 authenticated owner identity 在其 home 下查固定深度的
  已存在 conversation stores，选 exact channel binding 的最新 thread 重放 notice。这条读路不创建目录、
  不加载 backend/tool/subagent runtime，也不从全局搜另一 owner。u283 原 TUI 已直接补出之前丢失的 final。
- 用户给运行 child 的普通插话继续按 会话运行时 active-turn user input 语义：只有 provider 真正接受后
  才把正文幂等写入 exact child ConversationThread，并在 durable agent transcript 中写 exact id +
  有界公开文本。TUI 当地 pending 仅用于当场提升；新进程或重启 Gateway 后按事件重建相同 user block。
  fresh u287 已证明 `/exit -> resume -> Gateway restart` 两层重建后消息仍在原时序，且模型已执行其要求。
- u288 真实植物大战僵尸长任务在 Gateway 顺序重启时暴露了更窄的收尾竞态：`AgentRun/Attempt` 已由宿主
  写入 `done`，但子进程尚未来得及把同一轮结果投影到 `SubAgentTask`；旧监督器把所有安全 attempt 终态都
  当成可重派，导致 6 名已完成 child 被重新拉起，generation/尝试数增加并重复消耗 token。
- 对齐 会话运行时 `AgentStatus::Completed/Errored` 的单向终态与“child completion 可直接来自 AgentStatus、即使没有
  final assistant message”的语义：监督器现在读取 exact attempt 的 append-only `agent_run.completed` 事件。
  事件含宿主 `runtime_status` 时，复用普通 runner-result finalizer 补写 task、capability/source-worker 结构化
  覆盖、父级 wake 和 session 终态，绝不 abandon/requeue；由 orphan reclaim 生成、没有 `runtime_status` 的
  cancelled 事件仍按原崩溃恢复路径续跑。完成、失败、取消三种自然终态均有 generation 不增加的回归。
- R114j 部署后再次重启唯一 Gateway，u288 的 9 名终态 child generation 全部保持不变；u289/u290 的真实
  运行中 attempt 则按原 run 换代恢复，证明“自然终态补投影”和“真崩溃续跑”已经分开。随后暴露的另一问题
  位于主 Gateway active turn：处理中请求重排后，旧实现只重放原始 user prompt，而本轮已完成的
  `task_progress/create_subagents` 尚未进入终态 transcript，模型因此另建同义 Todo，u290 从 10 项膨胀到 16 项。
- 对齐 会话运行时 rollout 在中断/恢复时保留已发生 turn items 的边界，R114k 不把模型正文做摘要猜测，也不新建
  影子 recovery prompt。reconciler 写入 exact request 的 typed `gateway_active_turn_recovery.v1` 标记；重排执行
  从当前 owner 的 canonical `blobs/tool_outputs/index.jsonl` 与有界 task work 索引中，只按
  `conversation_request_id` 恢复已落盘调用、模型原始参数、操作终态和 artifact refs，再接入现有
  `carried_archive_tool_calls`。其它 request、child workspace 和自然语言错误均不能混入；索引不可可靠读取时
  fail closed，宁可停止自动重放，也不冒险重复派工或写入。

## 2026-08-30 子代理详情首次从 goal 开始、重访恢复独立视口【状态：R109 真 TUI 通过】

- child 详情正文的权威仍是 Gateway 返回的 exact `run_id + goal + attempt transcript events`；视口位置只是
  当前 TUI 进程内展示状态，不能写回会话、任务或代理状态。
- 新 child 页面第一次打开时固定从 canonical goal 顶部开始，避免已有大量 thinking/tool 事件时直接贴尾，
  让用户误以为提示词或历史丢失。离开页面时分别保存普通/modal 的 follow、cursor 和未读基线；重访恢复
  原位置，不再次强拉顶部。用户主动回到底部后仍恢复 sticky follow。
- `.10` 失败样本已证明后端数据完整而旧首次视口在尾部；本地 prompt_toolkit 回归覆盖“首次顶部、回切保持、
  modal 同步”。fresh resume TUI `ma-a166c7d-110-u272-child-first-view` 无需 `Ctrl+Home` 即显示完整 child goal；
  翻页、返回、重进保持同一位置，终态页仍可只读查看，合同已取得真机正证。

## 2026-08-30 孙代理必须继承父任务的 canonical run workspace【状态：R107 真 TUI 通过】

- `.10` R106 fresh TUI `ma-matrix-r106-110-u261-nested` 中，coordinator 创建两名孙代理；Go/Rust 产物落在
  根任务 `output/nested-research/`，Python 产物却落到
  `<owner_home>/workspace/runtime/.../subagents/tasks/...`。Gateway 同时持续报告
  `durable work path is outside its owner task and audit roots`，coordinator 因而读不到声明路径并自行补写。
- 根因不是读路径恢复或 owner 墙太严，而是递归创建只继承了 `conversation_execution_cwd/roots`，没有把父级
  host-owned `run_workspace.task_root/work_dir/output_dir` 覆盖到孙代理 attributes；manager 随后用自己的内部
  runtime root 另造第二棵任务树。
- 对齐 会话运行时 的显式 session/turn cwd 与 rollout state：任务晋升后，整棵代理树只认父级 canonical task root。
  `_inherit_current_conversation_workspace_attrs()` 现在复制父级完整 `run_workspace`，并以父级事实覆盖模型传入的
  nested `run_workspace`；不能为消日志而把整个 owner runtime 放进允许根。
- 回归同时构造恶意/过期 nested workspace 和真实 depth-2 run，断言 task workspace、agent run 目录、cwd 与
  runtime roots 都仍位于父任务根。本地 workspace/orchestration 组合已通过；只有 R107 fresh 真 TUI 同题证明
  两名孙代理产物都落同一 output、父级可直接读取且 Gateway 零路径拒绝后，才升级为完成。
- R107 `ma-matrix-r107-110-u270-nested-root` 已取得该正证：coordinator 和两名 depth-2 child
  的 `task_workspace_dir` 完全一致，三份产物为 363/331/122 行，Gateway 零路径拒绝。

## 2026-08-30 会话发现与 WorkspaceOnly 成功回执【状态：本地合同通过，R106 真 TUI 待复验】

- `/sessions` 只投影当前 owner 的 canonical `SessionManager` 记录，按更新时间倒序给出最近 10 条、当前标记与
  精确 `my-agent resume <session_id>`。它不扫描别的 owner，不从正文猜标题，也不在一个仍运行的 TUI 内只换
  `session_id`；会话运行时/终端交互 的原地 picker 都会重建完整 app/thread 状态，当前版本先提供诚实的发现入口。
- owner-scoped Shell 的成功和失败回执都必须携带相同 `sandbox_scope`。沙箱内 `touch /root/x` 返回 0 只表示
  隔离根视图中成功，不能证明宿主 `/root/x` 存在；该说明属于结构化结果投影，不解析命令正文、不改变授权、
  OS sandbox、退出码、幂等或副作用裁决。
- TUI/tmux 是长驻客户端：只要 tmux 会话未退出，对应 Python TUI 进程与历史视图就会保留；Gateway 不应把
  观察轮询物化为冷 owner。资源判断必须分开记录 Gateway、活跃 runner、闲置 TUI 和其它 会话运行时/终端交互
  对照进程，不能只拿整机 used 内存推断 Gateway 泄漏。

## 2026-08-30 工具输出路径是跨 Memory 域的中立小权威【状态：本地边界通过】

- Memory Promotion 只需要核验 owner 内工具索引元数据，却反向导入了 Memory Archive 的完整 externalizer，
  触发 `LAYER_BOUNDARY_FORBIDDEN`。路径布局已移到 `agent/common/tool_output_paths.py`；Archive 负责写正文和
  schema，Store 只读索引，二者不再互相依赖。
- 写入仍只有 `<root>/blobs/tool_outputs` 一个 canonical 位置；owner 历史任务 lookup 仍有界为
  `tasks/*/*/work/blobs/tool_outputs`。这次只移动路径计算，不迁移、不复制、不删除任何 owner 数据。

## 2026-08-29 手动 Compact 是可中断 active task【状态：本地合同已通过，`.7` 真 TUI 待复验】

- 真机 `ma-matrix-r64-107-u13-capability-chain` 在已有约 85.9k Context 的空闲会话执行 `/compact`：动画出现后
  立即按 `Esc`，摘要仍继续约 14 秒并提交 generation 1。根因不是供应商慢，而是手动 Compact 运行在持久
  control POST 中，普通 `is_running` 没有绑定它；Esc 因而没有精确目标，控制 outbox 的唯一普通 worker 也会
  被该长 POST 占住。
- 对齐 会话运行时 `TaskKind::Compact`/active task 的取消语义：手动 Compact 以
  `authenticated user + channel + conversation + original control message id` 生成精确、脱敏的进程内中断名。
  Esc 只停止这条 Compact，不回退到主任务、子代理或其他 owner。供应商连接复用同一 interrupt callback
  主动关闭；摘要候选在模型调用前后、checkpoint 前后和 canonical CAS 前复核中断。
- 精确 Compact stop 先进入原有 durable control outbox，再走一次有界紧急派发；它不能排在自己要停止的
  blocking Compact POST 后面。紧急派发与普通 worker 共用同一 message-id in-flight claim，传输不确定时仍由
  普通退避车道用同一 ID 对账，不生成第二副作用。
- 用户中断是 typed `COMPACT_INTERRUPTED`，不是 provider failure、lane busy 或自然语言猜测。中断不推进
  generation/cursor，不写 compact failure 熔断；最多留下未被 canonical thread 引用的恢复点候选，后续可由
  retention 清理。TUI 保留“上下文压缩已中断”的终态，旧历史继续可用。
- R108 已把控制面实际使用的 `COMPACT_TURN_ACTIVE/LANE_BUSY/INTERRUPTED/FAILED`
  与两个 exact-stop 码全部注册到唯一恢复分类表，避免落入 `UNKNOWN_ERROR`。R108 真 TUI 再次证明
  进度条、generation 提交与旧历史保留正常；此注册不改 Compact 事务语义。

## 2026-08-29 子代理关闭按精确分支收口【状态：R64 真 TUI 失败样本已取得，底座修复中】

- 真机 `ma-matrix-r64-107-u12-recursive-stop` 证明：用户进入协调子代理详情页按 `Esc` 后，协调节点形成
  `CANCELLED`，但其 8 个直属孙代理的 canonical `status_mirror` 仍为 `RUNNING`。这不是 TUI 刷新延迟，
  而是当前 `agent_control -> cancel_subagent_task` 只处理 exact run，没有收口它拥有的存活后代。
- 关闭语义对齐 会话运行时 `AgentControl.close_agent -> shutdown_agent_tree -> live_thread_spawn_descendants`：
  控制目标仍由 `owner + conversation root + exact run_id` 授权，但副作用范围是“目标节点及其存活后代”，
  不包含祖先和兄弟。实现先 signal exact target，使正在派工的模型/工具停止继续创建；再等待 owner-local
  `creation_guard`，重读 canonical lineage，并逐个关闭已落盘后代。自然语言名称、描述和 TUI 行号都不能
  参与子树选择。
- 模型侧仍只能点名自己的直属 child，不能越层点名孙代理；一旦合法关闭直属 child，其后代作为该 child
  的运行域一起收口。用户控制面可以在已授权的树内进入任意后代，关闭该节点的分支。根 `/stop` 已经按
  exact request lineage 取得全树并持有同一创建事务，不再嵌套获取非可重入 guard。
- `create_subagents` 收到结构化 cancellation token 后，工具终态必须显示“已中断/已有持久前缀由停止链
  收口”，不能降成“副作用是否完成未知”。它仍是失败/取消而非成功，不取得自动重放权；只有 generic
  handler 异常或账本无法证明终态时才保留 `TOOL_OPERATION_OUTCOME_UNKNOWN`。

## 2026-08-29 工具清单单一快照与子代理停止两阶段语义【状态：`.7` 真 TUI 通过】

- `ToolRuntimeSnapshot.runtimes` 是某个 exact run 在 owner、显式 allowlist、availability 和渐进披露收敛后的
  唯一工具集合。`list_tools`/manifest、provider Schema、ActionPolicy 和 Executor 只能投影或消费这份快照，
  不得在展示层再拿 `owner_type=user/group/local` 去匹配 `main_agent/task_local` 之类代理角色。两类身份不是
  同一维度；混用会造成“模型能调用工具，但清单显示 0 个”的分裂事实。
- `ToolExposure` 只表达是否进入模型表面；owner 边界继续由 snapshot 构造、path policy、effect/approval 和
  Executor 守住。该收口对照 会话运行时 `tool_executor.rs` 的 `Direct/Deferred/DirectModelOnly/Hidden`、
  `registry.rs`/`spec_plan.rs`/`router.rs` 的同一注册表链，不借修清单之名扩大权限。
- 子代理停止分成两个 typed 阶段：HTTP 同步完成 owner/parent/exact-run 鉴权、幂等受理后可返回
  `accepted`；唯一后台 canonical cancel 再做 exact attempt signal、持久终态和索引收口。`accepted` 不等于
  `CANCELLED`，TUI 必须继续观察终态；重复停止复用同一受理键。旧 runner snapshot/heartbeat 遇到更新的
  terminal state 必须停止，只有显式结构化 recovery 才能 opt-in reopen。
- 真机证据：r56 的 Esc 在约 6.06 秒得到受理、约 9.62 秒形成 canonical terminal，未再误报“无法确认”；
  r57 远程 `owner_type=user` 的 `list_tools` 返回 30 visible/30 executable，内部、root-only 和环境不可用工具
  仍未暴露。完整事件、失败样本和覆盖缺口记录于
  `docs/audits/TUI_FUNCTION_AUDIT_20260828.md`。

当前设计铁律：

- 产品能力状态只认 `docs/PRODUCT_FACTS.md`；代码存在、测试存在、历史计划打勾都不能自动升级为稳定能力。
- 先跑通一条主链路，再谈扩展。
- 真机运行与测试统一只允许一个真实 Gateway。并行、极限和长任务测试必须让多个 TUI/会话连接同一
  Gateway，以真实暴露队列、公平性、活动回合、恢复和资源争用问题；禁止用“每个用例一个 Gateway”
  规避共享底座问题。需要验证重启、崩溃或断连时，只能顺序操纵这一个实例；进程内 fake/合同单测不属于
  额外真实 Gateway。
- Shell 权限不解析任意命令正文中的路径。结构化 `working_dir`/write roots 先过 owner policy，正文再由
  会话运行时 式 OS sandbox 执行；未挂载路径的 `ENOENT` 不能证明宿主目标不存在。owner-scoped 失败或 stderr
  结果必须携带 `file_scope=owner_workspace_only`、`external_host_paths_hidden=true`、
  `host_path_absence_proven=false` 供模型诚实解释，但不能反向改变授权或 operation 终态。最终退出码只认
  进程事实，禁止从 `echo $?` 文本猜中间动作；model spec 应避免供应商用恒成功后缀遮蔽失败。
- 单 Gateway 的后台执行最小车道是 `owner + durable thread_id`，不是 owner。同 thread 的前台、
  子代理唤醒、policy 和 scheduled continuation 继续共用唯一持久 run claim 单飞；不同 thread
  必须能有界并发，不得因同一用户的另一个长后台回合而饿死。Gateway 只做无模型的
  ready-thread 规划，全局池与 per-owner 上限均由显式配置约束，持久来源与 claim 仍是调度权威。
- 自然语言负责沟通，结构化事实负责决策；prompt、summary、报告正文、guidance 和角色描述不能直接改变运行时状态、权限、验收或派工。
- Compact 的模型摘要同样是不可信自然语言：运行中工具历史只接受固定版本的纯文本 handoff 形状，缺栏、
  空泛动作句或任何供应商工具调用协议都退回 typed IR 机械投影。摘要中的 `write_file`/`run_command` 文本永远
  不获得工具执行权，也不能证明动作完成；这条校验不恢复任务质量机器判官。
- 安全门可以硬，业务质量门默认软；危险路径、危险命令、越权和客观产物错误可以硬拦，任务深度、覆盖充分性和报告质量进入 warning、返工提示或 closeout。
- 模型的验证结论必须停留在亲自观察到的证据边界：本机、替代环境、模拟器、局部入口或单一身份成功，
  只能证明该范围，不能外推到另一机器、真实用户、外部网络/服务、其他身份或完整端到端。当前工具、权限
  或环境无法观察用户要求的边界时，模型必须明确写未验证、已验证范围和剩余复核步骤；本机监听、绑定
  `0.0.0.0` 或 localhost 成功不等于另一机器可达。用户只要求检查、诊断、解释、对比或汇报时，模型只做
  只读核对，不自行写文件、改配置、启停服务或派工。该纪律正文只在 `model_guidance.py` 维护一份：真实
  HTTP backend 通过供应商高优先级通道发送全量边界（OpenAI-compatible 为首条 `role=system`，
  Anthropic-compatible 为顶层 `system`），原始 user 任务和 native assistant/tool 历史仍保持原顺序；原生
  工具投影再按 canonical `ToolRuntimePolicy` 只给可能产生副作用的工具追加同一授权段，让模型在选动作时
  再次看到。不支持独立 system 通道的旧 fake/第三方 backend 不假装接收，上下文统计也只计算真实发送内容。
  两处都只是 会话运行时/终端交互 式软行为指导，不解析用户意图、不禁用工具，也不得恢复任务专项规则或宿主
  机器语义验收。
- 工具 effect 仍只有一个 `EffectResolverPolicy` 事实源：command parser 给出的基础等级可与
  结构化参数 mapping 叠加，所有授权、并发、验证和统计消费者统一取最高风险，
  不能各自猜。`run_command(run_in_background=true)` 是持久进程的唯一入口，显式提升为
  `dangerous`。sandbox 是否真包住副作用只读同一 `SandboxPolicy.uncontained_by_parameter`；
  `run_in_background=true` 和 `terminal_session.action=start` 均不被当前 bwrap 包住，因为它共享主机
  网络，进程也越过单次 handler 存活。审批只认现有 exact binding，不从命令字符串或
  用户自然语言推断意图。PTY `list/write/read/resize/close` 只投影、运输、调尺寸或关闭已经审批创建的
  session，对齐
  会话运行时 `write_stdin` 不再触发第二次命令审批；一次性前台命令和已有灾难命令硬拒绝保持原语义。
- 子代理 capability grant 与用户副作用批准是正交合同：grant 只限定可见工具、command allowlist、
  path/network scope，不得生成、隐含或替代具体 tool/run/operation/args 的 exact approval。会话运行时 child
  继承父 turn 的 approval policy/sandbox，终端交互 worker 把权限请求回送 leader 的标准确认队列；本项目
  适配为同一 ToolExecutor 审批门。当前 `controlled_exec` 直接进入 `subprocess.Popen`，没有 bwrap/OS
  sandbox，所以必须声明 `sandbox=none`；`apply=true` 未批准时 handler 不执行。未来只有真实执行器已接入
  可验证 sandbox 后，才能修改该声明，不能凭 capability grant、工具名或自然语言放宽。
- 子代理具体工具审批复用 owner TUI/Web 的标准确认面，不另造模型推动工具：child 的
  `BackgroundTranscriptSink` 把完整 `ToolApprovalRequest` 发布到 owner ConversationStore 下唯一
  `subagent_tool_approval.v1` 记录，并阻塞原 ToolCall；界面以显式 consumer lease 领取，决定仍经过
  owner/thread/root/current-attempt 授权门和完整 request 比对。主代理与多 child 的面板进入一个 FIFO，
  页面切换不改变 root overlay。无交互 consumer、取消、终态、损坏或写回失败全部 fail closed；不从标题、
  行位置或回复文案猜授权。详细设计见
  `docs/design/SUBAGENT_TOOL_APPROVAL_BRIDGE.md`。
- 显式后台命令归 owner conversation 的受管 session，不归一次性 main/child runner。ShellTool 仍使用
  原 bwrap argv 和 `--die-with-parent`，但由 detached managed host 成为直接父进程；runner 退出后服务继续，
  host 退出时沙箱后代仍收口。跨进程唯一事实是 owner 沙箱外的 `managed_process_session.v1`，PID 必须配
  出生指纹，owner/conversation/store root 必须精确匹配，终态单调且落盘失败先回收进程。模型仍只通过
  `process_session` 的稳定 `session_id` 管理，不公开 managed host/命令入口 PID 让模型猜资源所有者，也不自动
  重启命令。`process_session(network_status)` 按 socket inode 投影该 session 进程树真实持有的监听地址和
  `listener_pids`；这是宿主内核观测，命令沙箱内 `ps/lsof` 看不到不能推翻。主机防火墙显式放行、绑定
  `0.0.0.0`、本机 HTTP 成功或端口存在都不能冒充另一台机器可达，
  外部连通仍必须由目标网络中的独立探针验证，工具不得为通过验收自动改防火墙。详细设计见
  `docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md`。
- 普通可恢复工具错误按 会话运行时 的 `RespondToModel` 语义回到当前模型继续修正：同工具同类失败达到
  提示阈值只能注入换参数、换工具或拆步骤的强返工提示，不能按次数结束 turn。精确同参机械重试可由
  action guardrail 拒绝该次动作，但不得升级成任务终态；跨轮 streak/episode 机器裁判不进入默认主链。
  只有取消、明确安全边界、副作用真实 unknown 或部署者显式启用的 typed hard policy 可以硬收口。
  同一 provider batch 内后到的同工具成功是更新的结构化事实，必须撤销本批更早失败留下的 active halt。
- 修当前链路，不为历史目录、历史字段或历史工具形态加旁路。
- 一个概念只保留一个权威位置：task workspace、artifact registry、subagent canonical state、compact ledger 和 config 都不能多头并存。
- 远程 owner 的 prompt 工作区、文件写边界与 shell 挂载必须来自同一结构化 workspace scope；公共读取区
  不能因模型给出绝对路径而升级为可写目录。宿主当前用户 home 的危险根豁免只属于无 owner scope
  的本地管理员；远程 owner 即使运行在 root systemd 下也必须保留 `/root` 等宿主 home 拒绝边界，
  仅由更窄的 owner home 白名单放行自己的数据。task/run/request 机器 ID 只做身份，不做目录标题。
- context window 先读 provider metadata 的显式容量，存在即完全覆盖本地配置；provider 未提供才使用
  `model_context_window_tokens`，compact 阈值和其余状态机不因容量来源变化而分叉。
- provider 原生多轮历史必须保留该协议要求的完整有序 assistant content blocks；Anthropic 兼容链中的
  thinking/signature、text 与 tool_use 作为内部 typed history 一起续入下一轮，用户可见正文仍只取 text。
  只允许白名单字段进入 provider replay，工具 id/name/input 仍以 canonical ToolCall 为权威；compact 或
  其他结构化裁剪一旦改变签名覆盖的内容，就必须丢弃对应 provider blocks 并回落到无签名的规范历史，
  不能伪造、拼接或向用户泄露 reasoning。
- 普通回合结束时允许做一次 会话运行时/终端交互 式工具终态投影，但它不是 Compact：宿主只从该回合
  canonical tool archive 生成有界、脱敏、带计数和 refs 的 `conversation_terminal_tool_fold.v2`，与 assistant
  消息同一次落入唯一 ConversationStore。V2 metadata 同时固定生成 hot-tail 与 cold-fold，并保存结构化切换时间；
  缓存热期只读较完整的固定 hot-tail，过期后只读固定 cold-fold，整个生命周期不重新总结、不改写落盘旧行。
  因而既保留“做过什么、哪里核验、不要重放副作用”的续做事实，也避免刚结束的追问主动破坏可复用前缀。
  旧 `v1` 是显式持久数据兼容入口且恒按 cold-fold 读取。完整原始输出继续只在 owner archive，不能伪造已删除的
  ToolCall/ToolResult 或 thinking signature。真正 Conversation Compact 才把折叠纳入摘要、推进
  `compact_generation` 与 transcript source-message 计数；`compact_source_tool_pairs` 继续只表示运行中 native IR
  真压缩掉的完整工具对，终态折叠的回合/调用数必须独立展示，不能冒充 Compact 或重复累计。
- 助手在工具调用前后的过程说明是用户可见的正式 transcript，不是临时进度。Gateway 按同一 request 的
  `assistant_part_id=commentary:N` 逐段完整追加，最终答复使用 `assistant_part_id=final`；恢复、配对预览和
  历史投影只把 `final` 当该轮最终答复，但不得删除 commentary。普通消息不再按任意字符数截断；预算不足时
  只能从最老的完整消息边界淘汰投影，真正历史替换仍只由 Conversation Compact 完成。工具原始大输出继续
  走 tool-result reducer/archive，不能用裁剪用户或助手正文来代替。
- 同一 request/run 在 provider overflow 后可能先收口失败响应、提交 Conversation Compact，再继续调用模型；
  `ModelCallLedger` 对这个 scope 给出累计快照，持久 `ConversationModelUsageStore` 必须按
  `physical_model_attempt_count` 游标原子换算成 append-only 增量。每个增量事件保存原累计快照指纹，精确重放
  幂等、同 id 异值 fail closed；input/output/cache-read/cache-write 与 provider/estimated 分区均只累计新增值。
  禁止把第二份累计快照整条相加，也禁止复用 scope-only event id 让一次正常 Compact 续跑变成数据冲突。
- delegated `task_local` 的 authoritative ConversationThread 在 provider/preflight overflow 后，由 child
  runner 在同一个 exact `AgentAttempt` 内提交 Compact 并继续采样，对齐 会话运行时 active turn 内联压缩后
  `continue` 的生命周期。这个 `context_overflow` 只是内部压缩边界，不得让通用 `agent.run()` 提前调用
  `settle_agent_attempt` 撤销工具执行权；直到 child runner 真正完成、失败、取消或返回其它可恢复终态，才由
  外层生命周期统一收口。该例外只认 `context_scope=task_local`、
  `conversation_transcript_authoritative=true` 和 `runtime_status=context_overflow` 三项结构化事实，不解析模型
  正文、不重开已经终态的 attempt，也不放宽 Tool Gateway 的 current-attempt 校验。
- Compact 完成后的界面同步沿用 会话运行时 的 typed compact/token event 与 终端交互 的即时 post-compact state
  replacement：手动控制回执携带 canonical `task_status.compact_generation`，TUI 只据此推进次数。压缩前最后
  一份 provider-visible Context 快照立即失效并撤下，固定行保留真实代数和“下次模型调用刷新”；不得继续
  展示旧 token，也不得为了刷新显示发一次辅助模型请求。未 Compact 的历史只在尾部追加 immutable fold，
  不按新预算重写旧轮；真正 Compact 才允许替换历史前缀。cache-read/cache-write 只读 provider 账本。
  idle/resume activity snapshot 仍是代数水合事实，不能因为没有 Working block 就丢弃；同一 runtime 内
  `compact_generation` 单调不减，避免手动回执之后到达的旧轮询帧把次数改回 0。
- 缓存经济性按 provider 真实 usage 分账，不用字符估算冒充。缓存创建未单独定价时保守按普通输入 5 计；
  对样本普通输入加缓存创建 `B=357,639`、缓存命中 `H=235,041`，总成本为 `5B+pH`：缓存价 `p=0.1`
  时为 `1,811,699.1`，`p=1` 时为 `2,023,236`，全部按普通输入则为 `2,963,400`，分别节省约
  `38.86%` 与 `31.73%`。若普通裁剪改写了 100,000 token 的稳定前缀，一次额外缓存失效成本为
  `(5-p)×100,000`，即 `490,000` 或 `400,000`；因此普通轮保持 append-only，只有真正 Compact 才一次性
  替换旧前缀。以上是按用户给定单价的比例单位，不擅自换算货币。
- 供应商额度耗尽、明确不可用或健康探测失败时，正式运行面立即切到已配置且探活成功的本地模型，禁止
  为等待刷新而让任务空转；本地首选端口和供应商刷新时刻来自显式配置，不从错误正文或自然语言猜测。
  只有当前没有 live request、到达配置刷新点且供应商探活成功时才安全切回，模型切换不得创建第二个
  Gateway、测试服务或平行会话。
- provider 连接失败按 会话运行时 `TransportError::Network -> retryable Stream/ConnectionFailed` 适配：异常链中
  typed `socket.gaierror` 先复用 2/5/15 秒三次物理退避，耗尽后保留 transient 语义进入既有模型轮恢复。
  这只保护 DNS resolver 短暂失效，不从错误正文猜网络状态，不放宽畸形 URL、认证/额度或代理配置错误，
  也不增加无限 runner 重试。
- 主代理长期记忆归 owner home；子代理只保留任务周期内可审计状态。
- 子代理可以写协作产物，但最终交付由主代理汇总和验收。
- 工具面要少，优先增强现有工具和运行时语义。
- 递归代理只保留一条“用户—当前代理”关系：每层父级可创建直属 child、向其当前回合插入消息、打断
  直属 child，并裁决直属 child 的能力申请。创建后由宿主自动启动，生命周期事件自动回到直属父级；
  模型不拥有查询、等待、巡场、手动推进或机器验收下级的工具。同批 child 之间不互相广播完整 goal，
  孙代理也只与自己的直接父级交换有界状态和 refs。
- 生命周期收件箱按接收者隔离，不按共同 root 血缘共享。根 child 的 completion/capability wake 只能由
  exact 主代理会话轮读取和确认；`task_local` child、孙代理、控制面和辅助模型轮即使携带同一个
  `root_task_id`，也不得枚举或确认根主代理收件箱。对子代理的用户插话继续走 exact `agent_run` guidance
  收件箱，两条链不能互相兜底或串读。该边界适配 会话运行时 的 child completion 直投 parent thread/session
  mailbox，而不是把 root lineage 当广播地址。
- child 的 canonical task、独立 `agent_thread_id` 与 runner 是唯一续跑权威。task-local finalize 只保存
  本 child 的断点，不得把它登记成 root `BackgroundMainAgentRuntime` 的普通任务；后台来源若精确解析到
  child task，必须在模型调用前关闭并交回 child runner。共享 batch 进程只承载多个 runner，不拥有各 run
  的启动租约；单个 run 返回 `PENDING` 后立即回收其 task-local launch record，再从同一 run 续派。
- child 创建只登记一个 `pending` generation 1 AgentAttempt，不得把 Gateway/创建请求进程冒充 runner。
  真实 dispatcher 接手时必须在同一事务把该 exact attempt 激活为 `running`、写入真实 runner identity 并
  取得执行权锁；current attempt 仍在运行时再次启动必须结构化拒绝，不能静默换成 generation 2。只有前一
  attempt 已被 typed completion/abandon/recovery 收口，后续调度才可创建新 generation。该边界对照 会话运行时
  `PendingInit -> TurnStarted -> TurnComplete/TurnAborted/Error`，同时保留本项目 runtime.db 审计链。
- 一个可续跑模型/工具片段结束时，宿主先将 exact current attempt 收口为 `done`，AgentRun
  仍保持 `created`；随后 runner result 只可回写与同一 `turn_end_reason` 严格对应的
  `PENDING/BLOCKED`。该提交桥只对 exact current generation 开放；`DONE/FAILED/CANCELLED`
  仍必须有匹配的 run 级结构化终态，模型正文、parsed output 和展示短句都没有该权限。
- 子代理交付合同只来自用户/父代理显式 `output_files` / `output_refs` / artifact refs。没有声明时不生成
  内部 Markdown 槽冒充业务产物；直属完成事件以 typed status、child 最终回复和系统 `final_report_ref`
  回到父级。旧 durable task 的 `system_default_output_ref=true` 继续可迁移读取，但在所有模型可见合同与
  expected outputs 投影中隐藏，不能影响新执行。
- 会话运行时 把 child `TurnComplete` 的最后一条 assistant message 作为 inter-agent message 留在父线程；本项目
  对应的唯一持久事实是同一 ConversationStore observation 中的 `subagent-completion.v1`。后台 lifecycle
  wake 和普通 TUI 后续轮必须消费同一信封。普通追加轮会获得新的 task id，但复用原 canonical task
  workspace；因此先按同 thread、非 detached task link 的 exact `task_path` 等值找出 workspace lineage，
  再要求 event 的 `root_task_id` 属于该 lineage 且 `parent_agent_id == root_task_id`，只选择直属 child。
  去重后有界注入 `completion_message/final_report_ref/declared_output_refs/artifact_refs`；不得把
  `runner_result_json/output_json` 或内部状态路径带入模型，也不得从用户正文猜父子关系。named Audit prepare
  与其它 root/sibling 继续隔离。这个投影只提供整合输入，不改变 child/root 的完成、权限或验收状态。
- 模型可见的 completion 详情 ref 必须与工具读取边界一致：要么解析到按 exact
  `root_task_id/parent_agent_id/run_id` 授权的只读公开投影，要么不向模型宣称它可以读取。禁止把内部
  agent 状态路径作为可操作 ref 暴露后再由 `Read` 拒绝，也禁止为绕过拒绝而开放整个 runner 状态目录。
  `completion_message` 仍是有界主链输入，公开详情只补深挖能力，不复制生命周期或完成事实源。
- cwd 与运行台账严格分离，但持久工作开始后只认一个 owner-scoped canonical task root：任务晋升前的普通
  对话可以使用 Gateway 校验过的 client cwd；首个 `promotes_task` 动作创建或复用
  `<owner_home>/tasks/<task_path>/` 后，main、child、grandchild 的默认 `execution_cwd`、产品写根和相对路径
  都切到该 task root。后续普通轮通过 thread 的 `workspace_task_id` 复用同一根，并在第一次模型采样前就把
  该 root 投影为 turn `execution_cwd`；旧 terminal link 仍只贡献目录、不恢复 live task 身份，首个工作工具
  再建立本轮 successor。模型、工具、审批和沙箱不得先看到 client cwd、执行时才晚一步切目录。不得重新继承 Gateway daemon
  的 `/root`、客户端临时 cwd 或另造 `child_outputs`。显式工作目录仍须落在结构化允许根内，
  `allowed_write_roots` 只授权范围，不从自然语言或模型给出的绝对路径反向选择 cwd。
- interrupt 是父子边上的显式控制事实，自动重试资格不能否决父级的打断。会话的
  `active_task_ids` 是可恢复候选索引，不等于正在运行数量；TUI Working 只统计 task-link
  `status=active`，而 `/stop` 选择当前 thread 的 typed root 后递归停止运行域。
- 当前请求的副作用事实只从 canonical tool archive 与 operation store 投影：
  `AgentRunResult.operation_verification` 保存内部逐操作终态，公开 Gateway/HTTP/transcript metadata
  只保存不含 call/operation ID、路径和参数值的有界分组。每条 assistant transcript 都保留该投影，
  包括 `operation_count=0`；compact 和历史索引直接消费 metadata，不解析中文核验尾注或模型正文。
  compact 不能把 LLM 摘要变成操作事实：`ConversationThread.compact_operation_evidence` 与摘要、cursor
  同一次原子推进，后续轮把有界程序证据放在模型摘要之后；二者冲突时只认程序证据。旧 transcript
  缺少 metadata 时 coverage 必须标为 partial，不能补猜。
  存在副作用调用时，最终正文后可附程序生成的简短核验块；普通零操作聊天不追加固定文案。
  这能证明模型实际做了什么或没有做什么，但在禁止自然语言语义判断时，程序不能理解并删除自由正文中
  的每一句错误自述，因此模型正文仍不是执行权威。
- 模型 HTTP 传输对显式 loopback 主机强制直连，避免桌面系统代理截走本地模型/Gateway fallback；
  外部供应商仍沿 urllib 的既有代理配置。该判断只读 URL host/IP，不按模型名或配置正文分支。
- 工具参数只有一份权威结构：`ToolModelSpec.input_schema` 保存完整 JSON Schema；旧
  `parameters/parameter_schema/required_parameters` 声明和 `tool_spec_schema` compiler 已删除。
  模型可见定义、文本/native adapter、参数恢复门、限流/重复保护哈希和最终执行必须消费它，
  backend、MCP 或 handler 不得再维护拍平的 required/type 副本。外层 typed tool-call envelope 必须先
  与工具参数分离；进入扁平执行 payload 后，除 `tool` 和未被 Schema 声明的真实协议元数据外都属于
  工具输入，`kind/run_id/status/metadata/artifact_refs` 等同名正式参数不能被误删或绕过校验。
  执行入口只允许 Schema 明确且无歧义的字符串→整数/数字/布尔/null/JSON container 类型纠正，
  不猜字段、不把标量包成数组。缺失字段只允许在同一入口按 `ToolRuntimePolicy.input_policy` 的逐字段
  明示安全默认值，或按 `trusted_parameter_bindings` 从 Registry 构造的 `run_scope/write_boundary/registry`
  结构化事实补入；Schema `default` 注解本身没有执行权，模型显式给出的字段永不被覆盖，未声明的必填字段
  继续失败。每个有效输入字段只记录不含原值的 `source/source_ref`，可信引用和精确动作条件在 Schema
  展示前 fail-closed；随后在任何路径、effect、审批或工具实现前完整校验
  required/type/enum/const/嵌套对象与数组/额外字段/长度和数值边界/本地 ref/组合规则。handler 继续负责
  文件是否存在、跨字段关系等业务事实。纠正审计只记 JSON 路径和前后类型，错误只回传约束与路径，
  不记录原始参数值。MCP 未支持或畸形的 assertion 必须在注册时跳过该工具并明确告警，禁止降级成宽松透传。
- 工具运行时统一切片已完成代码迁移、旧路删除、全量/真实模型验证和发布制品检查；功能规格为
  `docs/design/FEATURE-20260804-tool-runtime-unification.md`，完整证据、架构和迁移删除表为
  `docs/design/tool-runtime-unification.md`。目标不是在现有入口外加 facade，而是把
  `ToolRuntimeSnapshot` 前后的重复工具定义/ToolCall/ToolResult/审批/执行/完成入口已经收敛成
  `required_actions -> ToolRuntime -> ToolChoice -> provider adapter -> canonical ToolCall -> ActionPolicy
  -> ToolExecutor -> operation -> canonical ToolResult -> settlement -> CompletionGate`。native 正文协议块
  只可成为结构化违规与一次纠偏证据，不能提升执行；required action 只证明用户明确要求的现实动作
  是否有真实执行证据，不恢复目录扫描、普通任务业务质量硬门或第二份任务状态。协议只由显式
  `tool_protocol` 选择：native 必须通过当前 provider+endpoint+model+stream 能力探测，text 只走隔离
  adapter；旧模型名子串覆盖、运行中 fallback、不完整块执行和正文提升均已删除。
- 文件大小不是硬门；是否合并或拆分看调用链是否清楚。
- 大输出、compact、resume 必须靠 chunk、cursor、coverage ledger、archive 和 resume summary，不靠提示词提醒模型“别忘”。
- 参考成熟项目先于自己发明：会话运行时 是会话、active turn、Compact、Skill、工具、计划、子代理、停止和引导的第一底座参考；长期助手 只补长期 Memory、Persona、多用户持久调度与被动验证，通道运行时 只补 IM adapter、通道健康与投递边界。适配现有 owner/thread/task 事实源，不另造平行主链。
- 多用户命令隔离是执行节点启动硬门：所有 owner-scoped 前后台 shell 必须经 bwrap；缺失或自检失败返回结构化 `SANDBOX_UNAVAILABLE`，禁止降级宿主执行，也不走用户可见审批。
- 子代理的 `allowed_write_roots` 必须覆盖所有能启动进程的正式工具：文件写入、`run_command`、PTY 和 LSP 共用同一结构化写边界。bwrap 中 owner home 作为只读基座，仅把本轮精确授权根叠加为可写；不得解析 shell 文本、重定向或自然语言猜测写路径。PTY session 与 LSP server 还必须绑定创建时的 owner/task 写域，禁止按可猜 session/server id 跨域复用。
- 默认安装进入透明容器 CLI：用户仍调用 `my-agent`，包装器只挂当前工作区和 `~/.my-agent`；宿主 venv 仅为显式 `--host` 开发模式。企业 worker 在启动和 K8s readiness 重跑同一 sandbox 自检。
- 发布干净度分两层：工作树门检查 tracked 脏文件和未忽略 untracked 文件；制品门直接检查 wheel/zip/tar 内容、运行状态目录和大小预算。`.gitignore` 不是发布安全事实。
- 普通通道对话以 `owner + channel + chat/topic` 的持久 transcript 为唯一多轮事实源；旧 dialogue memory 不得重复注入或挤占稳定偏好。thread 另外持久保存一个精确 `workspace_task_id`，但它只表示最近状态/导航投影，不独自取得新 turn 的 cwd 权威。exact request、未结束 Goal 或 active task 才能继承目录；旧项目续作依靠工具携带的精确结构化路径回绑，不能从自然语言推断。
- 公开 `my-agent run` 虽然不可追问，也必须把精确 user turn 在模型/工具执行前写入同一个 owner
  `ConversationStore`，最终公开 assistant 投影再按同一 request 幂等追加；audit 仍只是经历档案，不能
  成为 Memory 的第二消息权威。该 one-shot thread 只提供 transcript 与可核验 message ref，不预填
  `conversation_task_id`，因此不会伪造会话任务 link；CLI 仍按独立 task/workspace 创建并收口。输入落账
  失败在执行前 fail-closed，assistant 尾部落账失败只通过 typed degradation 暴露。此顺序对照 会话运行时
  `run_hooks_and_record_inputs -> record_user_prompt_and_emit_turn_item -> run_turn` 的持久 user item 主链，
  同时保留本项目 owner scope、Memory evidence 与 standalone lifecycle。
- `cli_run` 的“无伪任务关联”不是无条件要求 `task_links=[]`：adapter 不得预填或主动 bind；若本轮没有
  task-promoting tool，links 必须为空。若模型真实执行会晋升任务的工具，既有生命周期可以建立 link，
  但 one-shot 收口后只能留下 `completed` 历史 link，`active_task_ids/active_task_links` 必须为空。
- 普通会话只有一个持久 transcript；`workspace_task_id` 只保留最近状态/导航投影。每条用户消息都是新的 active turn，当前消息决定本轮聊天或工作。`/stop` 只中断眼前真实运行的 turn，历史、compact、memory、persona 和产物都保留；没有 live turn 时不得借旧 task/goal 改状态。普通 `task_progress` 只提供 `read/update` 的可选恢复笔记，open item 不拦最终回复、不自动续跑、不要求用户选择、关闭或重开任务。exact request、未结束 Goal 或 active task 可以结构化继承 cwd；普通 terminal task 后的新工作由首个 `promotes_task` 工具建立新目录。写工具若精确命中同 thread 一个 canonical 旧项目根，则统一入口按目录归并多代 terminal execution、选最新一代建立 successor，并把本轮占位任务 supersede；跨多根或同根多个 active executor 时 fail closed。后台续轮继续读取完整 thread summary/raw tail，但 task link、observation 与 progress 等运行投影只允许当前 task 及其持久 child lineage，不能把同会话旧项目重新暴露成任务菜单。只有用户显式创建的 `/goal` 才拥有可暂停、恢复和后台续跑的长期生命周期。该边界直接对照 会话运行时 的显式 session/turn cwd + 单个 active turn，并采用 长期助手 的 session-local todo 仅作模型工作笔记；IM 只传结构化 owner/conversation/message 身份，不产生第二套语义。
- 原生子代理创建入口必须有机器可校验的目标，但不重复表达同一事实：单派使用非空
  `create_subagents.goal`；批量使用非空 `items`，且每项自己的非空 `goal` 是对应 child 的完整工作边界，
  顶层 `goal` 只作可选批次说明。两种形态都没有、空批次或任一 item 缺目标时，必须在落任何 run 前返回
  typed 可恢复参数错误，不能从普通自然语言猜目标。该边界适配 会话运行时 v1 可选 schema + handler one-of
  校验；不为兼容恢复第二份批量目标权威。
- 并行编码派工采用 会话运行时 `multi_agents_spec.rs` 的 disjoint write set 软纪律：模型必须在每项 goal 写明
  同一个目标目录和互不重叠的文件/模块边界，职责宽到会覆盖兄弟项、或两项会修改同一文件/模块时应改为
  分批。goal 规则只进入唯一工具 description/schema 参数提示；`output_files` 仍是可选交付与冲突线索，
  不是完整写集、权限、锁或宿主完成裁决。只有调用者主动提供的同批结构化路径完全相同或互为祖先/子路径
  时，创建前原子退回让模型缩窄或分批；省略时宿主不猜，运行时也不得解析 goal 或扫描 diff 自动裁决。
- gateway 请求进入终态归档时，response 的 `done/interrupted/failed` 是最终状态权威；processing lease 只提供 owner/attempt/heartbeat 等运行字段，不能覆盖终态。归档目录、请求 JSON、response 与 `/status` 必须表达同一事实。
- 恢复任务后，新的 request/run id 只表示这次执行尝试，不得成为新的任务事实源。模型可见的 main context bundle 摘要不裸露这些本轮运行 id；完整值留在 JSON 事实源，只有结构化选择既有任务后才显示 `selected_conversation_task_id`。default 主代理的 guidance、task_progress 工具、需求/派工 seed、coverage、wait、监督提醒、workspace 懒建和 delivery closeout 必须统一读取结构化 `conversation_task_id`；task_local 子代理仍按自己的 run id 隔离。该解析只保留一个共享实现，禁止各模块复制一套优先级。`task_progress` 的显式 read 若收到的正是当前 typed task id，必须把它归一成当前 task-path 账本；只有不同的 exact id 才能读取其它历史 run。conversation task link 是生命周期权威，`work/state.json` 是同一 task path 的 owner-local 投影；完成、停止、取消等结构化状态迁移必须同步投影，且目标与解析后的状态文件都必须位于当前 `owner_home/tasks/` 的精确 task 根内。路径越界、符号链接、身份不一致或文件损坏时只告警、不得覆盖别的任务目录。
- 租户可见路径默认取最小权限：本地与远程 owner 的 WorkspaceOnly 都只读写自己的完整 owner home，另外可读组织明确发布的 `~/.my-agent/shared/`；进程启动 cwd、其他 user/group owner、根模板和旧顶层私有目录一律不自动授权。只有结构化 `local/main + full-access` 可解除 owner 墙，普通/远程 owner 不能靠配置副本或自然语言提权；Full 管理员访问其他 owner 仍要求本轮用户明确点名。普通子代理始终恢复 owner/task 写墙。随 wheel 发布的基础 tools/skills 是公共产品能力，shared 只用于组织显式共享的 skills/tools/role templates；个人 USER/SOUL、记忆、任务和产物不得由 shared 绕过。
- 普通通道上下文必须在同一结构化 scope 内“累计 transcript → 自动 compact → 继续累计”：raw transcript 永不因 compact 改写或删除，thread JSON 的 summary+cursor+generation+checkpoint pointer 是唯一 live compact 状态；旧消息只进入该 owner 的 LocalStore 派生检索索引。每次先生成不改状态的候选，再按完整下一轮输入验证低于精确阈值，随后先写 owner-scoped 完整恢复 checkpoint，最后以一次 generation CAS 同时提交 summary/cursor/checkpoint；失败候选、checkpoint 写失败或 CAS 冲突都不得推进游标。正常达到配置阈值时允许在同一历史尾部保留有界的近期完整 user/assistant 回合，过大时退回压缩全部旧段；供应商已经返回上下文压力时则一次替换本轮之前的完整旧段，禁止把同一受保护尾部连续压成多代 checkpoint。这不是第二份 history，也不能让固定最近轮数重新成为遗忘边界。连续失败只更新同一 thread 的 typed failure circuit，三次后短暂冷却，成功提交清零，禁止每条新消息重复空烧摘要模型。
- 同一 owner/thread 只有一份模型历史。聊天、文件工作、子代理协调、定时唤醒和普通小任务都继续使用同一 thread 的 summary + raw tail；task link、workspace、progress、wake 与子代理树只是结构化运行事实，不得过滤、替换或复制 transcript。普通 Gateway 请求按会话顺序执行，当前 turn 结束或耐久续轮启动后仍继续同一历史，不能创建平行“聊天上下文”。
- 子代理 lifecycle wake 是同一 root active turn 的耐久工作片，不是 synthetic wake 文案发起的新任务。后台
  续接必须以 exact task link 的原始 goal 作为 `User Task/root_user_prompt`，把 wake prompt 放入 runtime
  continuation；同时从该 task 自己的 `work/blobs/tool_outputs/index.jsonl` 按 child 信封的 exact
  `conversation_request_id` 恢复结构化调用历史、one-shot 去重和已执行工具。durable task id 只负责定位
  工作区，不能冒充 active turn；旧索引缺该字段时只允许同值 `request_id` 精确兼容。不得扫描 child workspace、不得混入
  sibling/旧 task，也不得从正文猜语言、计划或完成状态。恢复记录计入当前 absolute tool-round baseline，
  但配置的每工作片新增轮数额度不缩水。detached Audit 配额通知继续是独立运营事件，不伪装成 root turn。
  该边界对照 会话运行时 `core/src/agent/control.rs` 的 child notification 与
  `core/src/session/turn.rs::run_turn`、`core/src/session/mod.rs` 的同 history active turn。
- 同一 exact root 的 child lifecycle envelope 是逐项耐久交付义务。合批可以减少模型调用，也可以按 token 或
  条数把剩余 sibling 延到后续轮，但不能丢弃或提前 ack；每个被采样 completion 的 exact id、typed status 与
  `final_report_ref` 必须保留。本轮开始时已在队列但未进 active batch 的信封不得被活动回合瘦事件入口消费；
  它们继续阻止 root closeout 和用户可见最终回复，直到后续有界批次完整读取。该边界对照 会话运行时
  `forward_child_completion_to_parent` 与 session mailbox；“canonical child 都终态”只决定可以开始整合，
  不等于模型已经看过所有完成信封。
- child lifecycle 后台工作片开始时冻结的 typed phase 同时约束收口续接：仍有 child、没有 child 或状态
  未知时不得从 finalization 新建普通轮询；全部直属 child 已终态后，该工作片就是原 root active turn 的
  整合片。若它因宿主结构化工具轮/上下文边界 unfinished，必须以 exact durable root task id 登记有界续接，
  不能因后台 `save=False`（避免重复 transcript/archive）而丢掉执行器。该续接不读取模型正文、不把普通
  Todo 升格为完成门，也不重新采样树；对照 会话运行时 active turn 在内部工具边界后继续运行的语义。
- 后台 scheduler 只有在该 thread/task 没有 linked live turn 时才能启动一个续接 turn；续接仍加载完整 thread compact 与消息尾部，并额外读取精确 task 的运行状态。任务已 completed/cancelled/interrupted/abandoned/superseded 时，排队的定时或生命周期 wake 直接作废，不能复活任务。canonical task writer 提交上述不可复活终态时，必须在同一提交路径立即停用 exact task 的全部 progress policy；scheduler 的终态扫描只保留为旧账本、崩溃竞态和升级数据的防御兜底，不能成为正常退休时机，也不能误停同 thread 的其它任务。
- task workspace 下只保留 progress、canonical state 和证据 refs 等结构化运行事实，不生成 task compact、task rollup package 或第二份主代理上下文。main、child、grandchild 的持久上下文压缩都只认各自 thread JSON 的 summary + cursor + generation + checkpoint；active turn 因 context pressure 续跑时，原生 ToolCall/ToolResult 的成对回收必须先在同一 owner Compact ledger 写 checkpoint，再推进该 ConversationThread generation，随后继续同一 turn。task-local workspace、工具和 Memory 仍按 run 隔离，但不得恢复旧 `memory_archive` continuation 或第二套计数。
- 所有位于消息开头的 `/XXXX` 都先进入统一 typed command dispatcher；支持的命令由程序执行，
  不支持的命令由程序确定性拒绝，命令词本身不得进入 transcript、active-turn guidance 或模型输入。
  `/btw` 与 `/audit` 只有去掉命令词后的用户正文可以进入既有 turn/task，权限和运行模式由 typed
  payload 携带；不得在 adapter、worker 或 prompt 中另写一套解析器。`/verbose off|on|full` 是
  per-thread 持久系统设置；工具进度必须以 typed event 进入 Gateway，再由有身份校验的 progress
  endpoint 和既有持久化 delivery worker 回送。不得从模型自然语言或混合 chunk 文本猜工具状态，
  也不得因进度发送失败重新执行任务。
- 派工、等待后的 presentation-only 模型轮必须结构化禁用全部工具。若 provider 仍夹带
  未授权的原生 tool call，首次按形态重试；有界重试后只能丢弃机器调用并保留通过统一内部协议净化的
  模型自然正文，绝不能执行该调用或把成功请求投影为空。若连安全正文也不存在，Gateway 以 typed
  `USER_REPLY_UNAVAILABLE` 失败，不能写空 assistant transcript、不能报告 `ok=true`。该边界不解析回复
  语义；对照 通道运行时 的 streamed-text 空 final 保留和 长期助手 对 commentary/final-delivery 的分离。
- 普通会话控制只有一份 typed protocol：`/status` 只读当前 durable root task/request/thread/子代理事实且
  不回放引导；上下文压缩只显示当前 thread 的唯一 generation。`/btw <内容>` 只投递给当前 active 根任务
  （尚未晋升才投当前 request），按 FIFO 在下一个模型安全点成为真实 user input；模型成功接收后以 guidance id
  幂等追加到同一 transcript，失败或竞态丢失时不污染后续消息。相同文字的两次输入保留为两条事件。
  同一 durable task 任一时刻只允许一个主执行器；已有 linked live turn 时，`/btw` 只写入该 turn
  会消费的输入队列，禁止再发 wake 启动第二个执行器。只有根任务当前没有 linked live turn 时才可发布去重 wake。
  `/stop` 像 会话运行时 当前窗口的停止按钮：先按 owner+thread 找到精确 live request 并立即触发
  cancellation token/关闭当前模型 HTTP 传输，再持久写入 cancel marker、将已绑定根任务转为
  interrupted，并异步停止同一 typed lineage 的子代理树；不得先让模型或任务分类器判断“这算不算任务”。
  该 turn 尚未消费的 `/btw`/普通 steer 随 turn 一起作废，不能在下一次“继续”时突然生效。停止不删
  transcript、task workspace、thread compact 或 memory；后续普通“继续”由模型通过精确 task id 选择
  原现场，不要求用户重发整段 prompt，也不从文字猜 task id。
  live turn 续接旧任务时，引导消费优先使用 `task_attributes.conversation_task_id`；本轮 request id 只是执行尝试。
  引导像 会话运行时 `TurnInput::UserInput` 一样进入当前 turn 的 provider-neutral `UserTurn`/text history；后续工具轮和
  compact continuation 通过 typed carrier 保留，不能改成 system injection、任务专属 guidance history 或第二份 prompt。
  子代理生命周期事件也在同一 active turn 的安全点按精确 task id 读取；新事件使旧模型动作失效，只有模型成功
  读取后才确认消费。启动当前后台轮的 wake 仍由 scheduler 单独确认，禁止 active turn 与 scheduler 双消费。
  `/status`、`/context`、`/compact`、`/effort`、`/btw`、`/stop`、`/goal`、`/verbose` 必须绕过同会话普通消息队列，由 CLI、Feishu
  和未来 IM 共用；`/audit` 只把去前缀后的正文作为新任务排队。旧 `/btw` 列表、永久 prompt 注入和
  `/btw-clear` 不再是产品能力。Gateway 生命周期 `POST /stop` 仍是管理员接口；会话 `/stop` 的控制目标
  只认 owner+thread 的当前 live request 和它的结构化 task link，自然语言“停一下/改一下”不获得硬控制权。
- `/context` 只读同一 owner/thread 的真实 pending transcript、模型窗口和 `runtime_compact_policy`，与自动
  compact 共用 `_projected_context_tokens` 估算；它不能创建 thread、写摘要或推进 cursor。`/compact [可选要求]`
  只能在没有 live turn 时申请同一 conversation run lane，再复用自动 compact 的 summary candidate、完整
  checkpoint 与 generation CAS 强制推进；可选要求只是摘要软上下文，不能覆盖结构化 operation evidence。
  自动 compact 仍按唯一配置阈值在每轮前触发，不因手动入口建立第二条历史。`/effort` 只认 provider/backend
  明示的结构化推理档位与 setter；当前 MiniMax-M2.7 Anthropic-compatible 接口没有生效的 effort 参数，因此
  查询应如实显示 provider-managed，设置必须失败且不得偷换成 temperature、prompt 文案或伪持久状态。
- TUI 输入的 Up/Down 必须先依据输入 Window 的真实显示宽度和 Unicode cell width 在软折视觉行间移动，
  到视觉顶/底后才能进入逻辑换行、queue 或 history；不能只按 `\n` 判定。手动离开 transcript 尾部后，
  `N new messages ↓` 是带 typed mouse handler 的按钮，左键释放与 Ctrl-End 共用 `move_end()` 恢复 follow-tail，
  不从显示文字反向解析未读状态。被动到达的新输出不得抢走用户正在阅读的位置；但一次通过长度/只读门的
  真实用户提交是明确的 return-to-live 动作，必须对当前 main/child viewport 调用同一 `end()`，让这条用户
  消息和后续回复立即可见。空输入且当前 viewport 已离尾时，第一次 `Down` 也必须先复用同一 `end()` 返回
  最新消息；已经跟随尾部时才继续选择 child 或浏览较新 history。该优先级只读 typed `follow`，不能解析
  `N new messages` 文案；超限消息与终态 child 的拒绝输入不能借此改变滚动位置。
- 工具是否展示给模型与能否执行必须共用一次 `ToolRegistry.runtime_snapshot`。像 `send_message` 这类依赖
  当前 owner 外部通道的工具，availability 必须在每轮用结构化 provider/target/root/capability 判定；没有
  proactive route 时从 schema、tool search 和调用快照同时移除，但实现仍留在唯一 registry。不能先暴露
  一个客观不可用工具，再靠 handler 错误和模型重试收敛，也不能按普通问候或中文关键词隐藏。
- `/goal` 是当前 conversation thread 上的特殊持久 overlay，不创建第二个聊天、Agent 或工作区事实源。每 thread 同时最多一个 active/paused/blocked 目标；它绑定一个持久根任务，通过去重 wake 自动续跑，只能由 typed command 暂停/恢复/修改/清除，由 `update_goal` 在真正完成或确实阻塞时进入终态。`/stop` 遇到 active goal 只暂停它，不清除目标。
- `/audit` 是显式前缀才能启用的特殊任务模式。入口只负责把 guarantee/window 写入结构化 task attributes，子代理按调度关系继承，watch 只读这些字段。prompt、goal、summary 或普通句子中出现 `/audit` 文字都不能激活保证。
- 子代理数量由主模型按真实可独立分解项决定，不向普通用户暴露固定数量命令。调度层同时核对本批/任务/owner/全局余量；整批超限就结构化拒绝，不静默截断、不边创建边失败，也不允许用重复假工作填数量。
- Feishu 等 IM 入站回调在任何媒体、Gateway POST 或通道 IO 前，必须先持久化 authenticated
  `channel/user/conversation/provider_message_id`、原始规范 payload 与 SHA-256 digest。same-id/same-body
  只复用原 row，same-id/different-body 写独立 quarantine 且不覆盖首条事实。唯一 adapter delivery worker
  依次推进媒体、精确 POST body、结构化 submission、占位、input/result/control receipt watcher 和最终回送。
  ingress 与 reply row 的每个外部阶段都先用短文件锁领取 `owner/epoch/expiry`，网络、媒体和 provider IO
  均在锁外运行，结果只允许同 epoch CAS；过期接管递增 epoch，旧进程不得迟到写回。POST 响应丢失从
  `payload_ready` 原样重放持久 `gateway_payload`，响应已落盘后的 adapter 崩溃从 `submitted` 恢复而不再
  POST。轮询 429/5xx 只保持 WAIT，auth/config 错误隔离；input `terminal_unknown` 清占位并持久收成
  unknown，二者都不能伪造成用户最终正文。控制 submission 另保留 `operation_id/receipt_id/control_state`；
  `/btw` unknown 只能以 operation ID 查询 `/control-status/<operation_id>`，不能按目标 turn 去重。目标
  `request_id` 允许初始为空，只能由首次 GET 在同 epoch CAS 单向绑定，之后漂移必须隔离。stop rejected
  返回明确控制结果，stop `terminal_unknown` 清占位并落 durable unknown，不能静默标成 completed。
  control watcher 另冻结 `channel/user/conversation/channel_chat_type/channel_chat_id` 为 immutable issuer route，
  GET 必须携带同名结构化身份头由 Gateway 精确核对；operation ID 只用于定位，不能充当访问凭据。
- 用户通道正文只能使用统一 user-facing projection；`MAIN_AGENT/RUN/SUBAGENT` 内部协议留在运行时，禁止原样进入 Gateway response、飞书回复或 assistant transcript。产物发送只有一个 `send_message` 工具：目标固定为当前 owner，附件必须命中该 owner 的 artifact registry、真实路径和 hash；同会话下一轮从 transcript metadata 复用最近产物，不能因“发我”重新生成或复制。
- 普通任务采用 会话运行时 式完成边界：主模型依据同一 thread history、真实工具结果、测试结果和子代理事实直接
  给出自然最终答复。运行时不再要求 `submit_for_acceptance`，不扫描目录推断完成，不生成完成 marker，
  也不在最终答复后运行 `delivery_snapshot` 重写短轮。Gateway/IM 只在统一出口移除内部协议并按可信
  `ReplyEnvelope` 投递；assistant transcript 保存同一份净化后的模型正文，避免交付层另造第二种回答。
- 普通任务没有独立 closeout ledger 硬门。文件存在、路径权限、附件 owner/hash、危险 effect 等客观安全事实
  仍在各自不可绕过的工具或投递边界校验；“任务是否做完”由主模型结合这些结构化事实判断。显式 `/goal`
  另由 typed goal 状态和 `update_goal` 收口，但它仍使用同一 conversation history，不恢复普通任务验收器。
- 长期命令只有一个受管入口：模型向 `run_command` 传 `run_in_background=true`，宿主返回可查询、可终止的
  `session_id/pid/output_file`。shell 自带的独立 `&`（包括 `nohup ... &`）在启动前以
  `BACKGROUND_PROCESS_MODE_REQUIRED + not_started` 拒绝；不能让前台 shell 退出后留下无账进程，也不能把
  同一条命令内部的短暂探活当成服务已经持久运行。普通 `2>&1` 重定向和引号内的 `&` 不受影响。
- 所有普通最终回复、后台主动消息和显式 `send_message` 共用 `DeliveryService`：可信 `DeliveryContext` 单独持有 channel/target/reply_to，`ReplyEnvelope` 永远不带收件人；adapter/capabilities/target validator 只能通过 `ChannelAdapterRegistry` 注册，新增 IM 不得在投递主流程增加平台分支。
- Gateway 的 JSON/metrics 短连接写回只把明确的 EPIPE、ECONNRESET、ECONNABORTED 认作“客户端已离开”；
  该事实只结束当前 socket 传输，不回写任务失败、不取消已进入 handler 的持久请求。JSON 编码、磁盘、业务
  I/O 和其它 OSError 仍沿统一 error path 暴露，禁止用宽泛 `except OSError` 掩盖服务端故障。
- 副作用超时不等于失败也不等于可重试：通用 Tool Gateway 必须把可能已经发生但没有终态的调用持久化为 `unknown`。`operation` 作用域只重放同一调用身份；`business` 作用域由工具从 typed owner/request/目标/规范参数生成跨调用稳定键。只有带 `source_ref` 的目标系统结构化核对能把 unknown 收口为 succeeded/failed，或在证明 not_started 后原子重开；没有核对能力就保持 fail-closed。provider 原生幂等键只由可信 DeliveryContext 下发，Feishu 的同一分片/附件重试必须复用同一个 UUID。
- 多个外部写组成普通任务时不增加通用 Saga、第二份事务账本或自动补偿器。对照 会话运行时
  `会话运行时-rs/core/src/tools/lifecycle.rs` / `parallel.rs` 与 长期助手 `agent/tool_executor.py` /
  `tool_dispatch_helpers.py`，每次工具调用仍有独立 operation 生命周期；my-agent 当前同轮调用继续按模型
  原顺序执行，不从自然语言猜依赖，也不因一个失败机械阻断彼此独立的后续调用。系统必须准确保留每项
  succeeded/failed/unknown，权威终态没有持久化成功时即使提供方回报成功也只能返回 unknown，禁止自动
  重试。archive、control-plane event 与 compact 续跑必须携带同一结构化状态；语义摘要不能吞掉中段失败、
  运行中或 unknown 的副作用事实。业务是否需要回滚仍由具体工具或 workflow 明示实现，通用底座不得
  伪造跨系统原子性。
- 工具参数中的显式绝对路径具有目标身份，不能为了“安全落位”静默换成另一个路径后返回成功。普通
  相对路径按当前可信 cwd/workspace 解析；只有显式 `output/...`、`work/...` 才进入结构化任务内部目录。
  绝对路径必须保留原值，再由
  `allowed_write_roots`、owner 墙、危险目录、sandbox/approval 明确允许或拒绝。该规则对齐 会话运行时
  “解析真实目标后交给 sandbox/approval”和 长期助手“保留绝对路径并报告 resolved_path”的做法。
- 除 `/status`、`/stop` 等显式控制命令外，普通聊天、派工回执、等待说明、进度与完成说明的用户正文必须来自 LLM。运行时只提供结构化事实、禁用回执轮工具并校验/净化输出；不得用“任务正在处理”等固定系统句子替换模型正文。没有合格模型正文时宁可记录结构化失败并抑制投递，也不能用模板冒充 Agent 回答。
- 后台主代理 claim 的 `heartbeat=0` 明确表示按 TTL 自动取间隔；默认 TTL 为 90 秒。claim 保存
  `process-domain + pid + start_time`，只有同一 PID namespace/主机进程域能证明旧进程已死时才提前接管；
  跨 Pod、旧格式或身份不足时必须等待 TTL，不能把“当前容器看不见 PID”当作已死。
- Gateway SIGTERM/SIGINT 必须先写 typed stop request 与轻量 forensics（signal、pid/ppid、父命令、systemd
  环境），再复用现有 stop-file drain；计划 stop 已存在时只能追加 observed，不能改写为异常退出。
- 人格三件套只有 `update_persona` 一个写入口：USER/AGENTS 可由当前 owner 的 Agent 自主维护，只有 SOUL 修改必须由该 owner 用户确认；基础文件、patch、shell 和 admin full-access sandbox 均不得形成旁路。
- Harvester cursor 是一批事件已完成 engine、spool、audit 的提交水位，不是网络读取进度预告；批内任一步失败都必须保留旧 cursor 以便重试，禁止先推进游标再处理造成静默丢事件。
- `/audit` 高频判读复用同一 watch/spool/structured-output 主链，不建立第二套 Agent、Memory、
  Compact 或角色运行时。模型调用批次由结构化的等待时间、累计条数、累计数据量任一条件触发；
  正常事件完整进入有界批次，不能为了凑上下文提前截断。只有单条事件本身已超过当前模型安全输入
  预算时，模型视图才生成明确标记的头尾内容；完整原文、哈希、位置、模型、分数和结论仍保存在
  owner-scoped append-only spool/archive/verdict ledger，并通过稳定 `audit://...` `source_ref`
  与 `watch_stream(action=inspect, ack_id=...)` 精确查回。每个 Audit 的 watch 身份必须包含 typed
  root task id；相同 owner/source 的另一条 Audit 不得复用游标或账本。任何获得 exact task 与工具
  权限的 Agent 都可消费；是否委派、委派数量、角色、复核和汇报路线均由模型自主决定，运行时不得
  固定主代理禁用、来源到子代理映射或分数路由。exact clear 只关闭该 root task 的 watch 并保留原始
  审计文件。后台唤醒继续使用原 task id 和 objective，不能按 owner 任意旧 watch 反推，也不能创建
  固定工作角色。常规批次只占模型窗口的小比例并设实际上限，结构化输出只做 request-local 限制，
  不能缩小主会话配置。
- 模型能力自我描述必须按 installed/configured/healthy/current-bound 四层事实投影；adapter 存在、凭据齐全或代码中有工具都不能单独升级成当前可投递。四层状态全部从 composition root 的同一 `ChannelAdapterRegistry`、结构化 daemon health 和 owner conversation binding 投影；进程死亡、心跳过期、状态损坏或未绑定均 fail-closed。
- Compact 百分比只允许一个权威阈值：`context_window_tokens × configured_percent`。候选是否可提交也必须按包含 system/persona、summary、近期 raw tail、原生工具 Schema、ToolCall 参数、ToolResult、运行引导、近期与已压缩工具事实及当前用户输入的完整下一轮投影低于这个阈值判断；不得再加未来输出预留、工具 digest 隐藏天花板或另一套 task compact。当前 turn 的原生工具历史只能按调用/结果整对回收，并复用既有语义摘要后端在同一 IR 中以最多一条 replacement item 承接被回收旧段；后续压缩原位替换，summary、handoff marker 与近期尾部都进入同一预算。对于允许持久化且已绑定 ConversationThread 的真实 main/child/grandchild 回合，这次 mid-turn 回收本身就是一次 canonical Compact：先把旧 thread summary 与当前工具历史合成完整替代摘要，写 `source_kind=live_tool_ir` 的 owner-scoped checkpoint，再以 generation CAS 提交；transcript cursor 不前移，工具对累计数单独记录。摘要请求顺序必须对齐 会话运行时：完整历史在前，synthetic user Compact 指令最后；禁止把摘要指令放在历史最前面后再追加工具历史，否则兼容模型会把它当旧要求并顺着最后动作普通续写。已结束会话前缀本身达到触发线时，live-tool IR 无法解决压力，必须直接交给统一 transcript Compact；live 候选完成摘要和成对回收后仍未低于同一触发线时，必须恢复原 IR，且不得写 checkpoint、推进 generation 或发布成功事件。checkpoint、CAS 或摘要 transport/调用失败同样必须恢复原 IR 并失败，不能静默丢历史；供应商请求已正常完成但正文为空时，用 typed IR 的有界机械续接摘要收口，不重试也不把空正文升级成任务失败。presentation/no-save 或没有持久 thread 的辅助回合才允许只做 turn-local 窗口整理，且不得冒充 `compact N`。摘要是非权威续接视图，raw archive、operation ledger、artifact、workspace、transcript 和真实 UserTurn 仍是事实源；不能另用漏算工具参数的字符口径。provider usage/tokenizer 缺失时的估算误差必须与阈值数学分开说明。
- Skill 运行时只有 composition root 创建的一个 `SkillsService`：每轮 snapshot 固定 `workspace > owner > shared > builtin`，prompt、检索、正文读取、能力自述与子代理都消费同一实例。`shared/indexes/skills.jsonl` 只是派生管理员清单；不得恢复 `SkillRegistry`、resolver/index loader 或编排入口临时 router。子代理 Skill 引用必须保存 stable id + content hash，后代只能收窄不能扩张。
- 记忆与 Skill 各有一份独立总闸：owner `memory_policy.json`(memory-policy.v1.enabled) 与 `skill_policy.json`(enabled) 只经 `resolve_effective_owner_policy` 投影成 `EffectiveOwnerPolicy.memory_enabled/skills_enabled` 两个 effective flag；文件缺失或损坏视为开启(老 owner 兼容，永不因解析失败误杀)，子代理用 and 继承父开关、只能收窄不能扩张。消费点只认该 flag：Memory 关闭时 curator 调度(`_run_due_curators`)、会话 close/reset 请求、发现层判活(`_has_pending_memory_curator_work`)、决策点召回(`push_relevant_memories_report`)全部短路，remember 工具 availability 不可用；Skill 关闭时 `snapshot_for` 返回空快照(短路而非扫描失败，无 load 错误)。两闸互不级联：关 Memory 不影响 Skill 快照，关 Skill 不影响 Memory 召回。禁止为单开关另建第二套 policy 状态或跳过 effective flag 直接读原始 JSON。
- - 【长LLM测试 2026-08-13】两篇长文经真实 /ask 入口灌入 + 跨 run 召回 20/20 全命中：灌入=3605 字
  《大理生活回忆》(雪球/海月小筑/鹿柴/县一中/马尔代夫 AOW/德龙/三月街等 20 种子事实) + 第二篇
  《工作与远行》(山海·拾遗个展/云中君三本/腾冲温泉/大阪/沸点火锅/碱水粽/数位板等)。**模型
  remember 工具侧两次 batch 都生成空 operations 数组**(TOOL_PARAMETER_REQUIRED → 补参后
  TOOL_INVALID_ARGUMENTS，内容全丢)——模型层行为非产品 bug；**curator 车道兜底正常**：
  candidates 161→176→187、long_term 10→21→30，提炼质量优(鹿柴 confidence 0.95 带 evidence quote)。
  召回:重启前 20/20 全命中零幻觉(含顾女士/二十幅等细节级事实),`systemctl restart` 跨 run 再问
  20/20 全命中(持久化不丢)。已清理回基线(candidates 187→161、long_term 30→10)。
- 【真机发现→已修 2026-08-13】remember 确定性失败被错误归 UNKNOWN：参数错误后模型补参仍错时
  收到「副作用不确定已阻止重做」放弃修正。根因=coordinator 对已执行 handler 的通用失败按
  handler_executed=True 归 UNKNOWN，`_memory_error` 从不声明 effect_outcome。修复=`_memory_error`
  增加显式 not_started 声明(默认保守不标)，18 个写入前/只读校验失败点标 not_started → 归 FAILED
  (可修正重试)；唯一例外 MEMORY_CANDIDATE_WRITE_FAILED(observe_many 可能部分写入)保持 unknown。
  红测先写(test_memory_tool.py effect_outcome 断言)+全量 gate 绿+已部署 testbox(grep site-packages
  签名+18 处就位)，部署后抽样召回正常、日志 0 次 TOOL_OPERATION_OUTCOME_UNKNOWN。

【B项真机验收 2026-08-12】长文召回补重启/双用户隔离两象限全 PASS：象限一=主 owner(local/main)经真实 /ask 入口灌入含 5 独有事实的中长文 → 注入 curator pending reason(紧急车道,60s 节流内消费) → 提炼 5/5 落 long_term → 提问召回 5/5 命中 → `systemctl restart my-agent-gateway` → 再问同题 5/5 命中且更详尽(长期记忆磁盘持久,重启不丢);象限二=两 feishu 用户(ou_e2e_feishu_a_20260811 / ou_6591b3f0d402ce95a62ef26436dca895)各灌独有事实 → 各自动提炼落库(3/3、2/3) → 交叉提问零串扰(A 问 B 的成都→"没告诉我";B 问 A 的朵朵/滨海湾→"不知道"),正向各自 3/3 全命中。验收数据已清理回基线(10/0/0),对话/审计/runs 日志留证。
- 长期 Memory 只有当前 owner `memory/long_term/memory.jsonl` 一个正式事实权威；SQLite/FTS、向量和
  global index 只可重建，必须按稳定 entry ID 对照 active 版本，不能让 tombstone 或旧版本复活。
  `memory/daily` 只保存 Curator 经历摘要与 refs，`memory/ops.jsonl` 只保存无正文操作审计；两者都不再
  镜像长期正文或参与正式事实召回。
- Memory 候选只有 owner `memory/candidates.jsonl` 一份状态机。Gateway 的轮次/时间/pre-compact/
  close/reset/task-complete/daily-finalize/admin 触发只向同一 `MemoryCuratorService` 提交 reason，共用
  lease、cursor 和整批提交恢复；模型只产严格结构化 Daily/Candidate，宿主验证后落盘，不能直接写
  long-term、Persona、lesson 或 HOT。旧 learning drafts、task-local memory gate、ops 候选和 daily
  mirror 只由一次性 v2 migration 读取，生产运行不保留双读、双写或 fallback。
- planner/runner 决策点的主动教训召回只读正式 Lesson/HOT，并与普通运行复用唯一
  `<memory-context>`。旧 `memory_push` 的 lesson 枚举、trigger_conditions、long-term
  `kind=lesson_*` 写读路径和 `[相关记忆提示]` 已删除；失败自省只能提交待审 Candidate。
- Persona 当前正文只有 owner 的 SOUL/USER/AGENTS 三个权威文件，全部读取和变更必须经过同一个 `PersonaRepository`。版本 ledger/backups 只用于 CAS、审计和回滚；USER/AGENTS 由当前 owner 的 Agent 在结构化证据、scope 与 CAS 约束内自主维护，只有 SOUL 需要该 owner 用户确认，确认期间 SHA 漂移必须拒绝覆盖。不得恢复直接文件写旁路或增加 IM 专用人格状态。
- 用户级持久定时只有 owner `data/scheduler/` 中的 `SchedulerRepository` 一份 job/run 事实源。`schedule` 是唯一 action tool，at/every/cron 均为 typed schema；到期 run 使用 CAS、先推进 next-run、claim TTL 和 heartbeat，并返回原 owner/thread 的同一代理主链。`clock.sleep` 只服务模型明确需要的时间等待；子代理完成依赖生命周期事件，不再有 wait/巡场工具，也不能形成第二 scheduler。对照 通道运行时 `src/cron/types.ts`/`schedule.ts`/`service/timer.ts` 与 长期助手 `cron/jobs.py`/`scheduler.py`/`tools/cronjob_tools.py`，不从用户文本推断身份、任务或时间状态。
- 可复用 Workflow 只有一种表达：Skill 提供方法，当前 thread 的 `task_progress` 保存计划，原生子代理工具显式创建和派发执行者。旧 `subagent_workflows` package、mode/config/CLI、extension hook 和 shared workflow index 已删除；不得恢复第二套 transcript、planner/router、自动批量扩张或模板执行 runtime。该边界对照 会话运行时 `turn_context.rs` 的逐轮 Skills snapshot、`plan.rs`/`plan_spec.rs` 的 typed plan 更新和 `multi_agents_spec.rs` 的显式 spawn，以及 模型助手 Code `plugins/feature-dev` 通过 command/agent prompt 调用原生 Todo/agent 能力的做法。
- 普通代码任务只有一份 owner-local 被动验证证据：公共工具出口按 `ToolCallEnvelope.scope.root_task_id` 记录项目 manifest 中的精确规范命令、真实 exit 与 targeted/full；成功文件写使旧证据 stale。它参考 长期助手 `verification_evidence.py`/`verify_hooks.py` 的被动账本，但不移植 stop hook，不执行测试、不阻止完成、不恢复普通任务验收器；模型继续按 会话运行时 的真实 tool-result 事实自然收口。
- 主代理普通正文不是机器事实，普通 `task_progress` 也不是宿主判官：模型给出 plain final 后当前 turn
  直接自然结束；即使清单仍有 open item，运行时也不丢弃草稿、不追加 hidden reconciliation provider call、
  不改成 typed blocked 或 ordinary continuation。清单继续作为同 thread 的模型工作笔记进入历史、Compact
  和 TUI。只有显式持久 `/goal` 保留 open-plan `unfinished` 生命周期与 continuation；全链不解析“完成”等
  自然语言、不扫描目录、不执行机器质量验收。2026-08-23 的 终端交互 式 stop nudge 已于 2026-08-28
  按用户决定删除，历史真机失败样本只作设计沿革，不再代表当前产品语义。
- 子代理工具能力只能继承父代理当前 run 的工具快照并继续做减法：所有普通 leaf 角色都移除
  `create_subagents/send_guidance/cancel_subagents/resolve_capability_requests` 四个直属下级控制入口；
  只有结构化角色模板明确 `can_spawn_children=true` 的 coordinator，且父级本来拥有对应能力时，才保留
  这四项。leaf 自己仍可用 `capability_request` 请求本层能力。显式 `allowed_tools` 只是收窄请求，
  不得凭 agent 名、goal 文本或模型声称扩权。
- 角色模板的 `prompt_zh` 是创建时冻结的软行为合同：snapshot 必须携带当前角色名称和提示，leaf runner
  只注入自己这一份，不加载整份角色目录；旧 snapshot 缺字段时才按明确 role id 读取当前内置模板。提示
  不能授予工具、路径或生命周期权力。内置 worker 对齐 会话运行时 shared-workspace 纪律，只做父级分配的明确
  文件/模块范围，不覆盖或撤销兄弟改动；已有文本局部修改先用 `apply_patch`，上下文未命中时重读最小片段
  再重试，不能用整文件重写绕过冲突。该纪律不恢复 workspace 锁或自然语言机器裁决。
- 模型调用观测区分 logical turn、物理 model attempt 与 provider HTTP attempt；每次 provider 重试、模型级重试、失败、超时和最终状态写入同一线程安全账本，并投影到 runtime facts 与内部 Gateway result。观测回调不得读取 key/body，也不得改变真实请求结果。
- owner 磁盘配额是管理员可选能力，`max_disk_mb=0` 表示不限制且不得扫描 owner 全树；只有显式非零上限才启用 `owner quota -> repository/file lock -> mutation` 的应用层锁序。启用时文件工具、Memory、Persona、Scheduler 和 Skill draft 必须在同一 owner lock 内按完整 multi-file mutation 的最终字节准入，策略、用量或锁不可读时 fail-closed。应用门不能冒充 filesystem quota：Shell/PTY/LSP 任意进程写盘必须由正式部署的 filesystem/project/container quota 硬限制。
- owner retention 只依据 typed policy、terminal authority 和 timestamp；task/scratch 执行前必须二次校验，先移入 owner trash 并写 tombstone，再按期限删除。owner/task legal hold、损坏 policy 或状态漂移均跳过并留审计。Gateway 只用 cursor 有界扫描 owner，不为清理实例化 Agent。
- Shared/Skill、Memory/Persona、scheduler、Workflow、隐私和验证证据的详细审计见 `docs/design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md`。用户已于 2026-07-18 明确确认按规划逐项实现；权威功能规格为 `docs/design/FEATURE-20260718-agent-foundation-convergence.md`，当前本地实现完成，待完整 CI、提交、部署和真实验证。实现必须逐切片迁移并删除旧主链，不允许增加 IM 专项或自然语言硬判断。

当前入口文档：

- 当前产品事实与 P0 冻结边界：`docs/PRODUCT_FACTS.md`
- 架构总览：`docs/design/ARCHITECTURE_GUIDE.md`
- 模块结构：`docs/architecture/MODULE_OWNERSHIP.md`
- Home 布局：`docs/architecture/MY_AGENT_HOME_LAYOUT.md`
- Subagent：`docs/modules/subagent/04-structure.md`
- Memory：`docs/modules/memory/04-structure.md`
- Gateway：`docs/modules/gateway/04-structure.md`
- 多 IM 统一投递：`docs/design/CHANNEL_DELIVERY_DESIGN.md`
- Agent 基础能力事实审计：`docs/design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md`
- 代码尺寸报告：`CODE_SIZE_REPORT.md`
- 容器 sandbox、一键安装与发布干净度：`docs/design/CONTAINER_SANDBOX_INSTALL.md`
- P2 正式 scale 主链：`docs/design/P2_SCALE_MAINLINE.md`
- P2 灰度、灾备、Owner 对象事实源与 24 小时 proof：
  `docs/design/P2_SCALE_ROLLOUT_DR_OWNER_STORE.md`
- 1.9 / 1.10 近 24 小时日志、真实 LLM 和通用底座加固：
  `docs/audits/REAL_LLM_24H_HARDENING_20260710.md`
- 待实施开发计划（确定性优先 + 缺口补齐）：`docs/design/PLAN-stability-and-gaps-20260611.md`
  （统领原则=同输入结果可重复；优先级 模型行为+观测性 → 记忆推模式 → 失败自省 → 多通道/多租户）
- 待实施开发计划（任务完成力底座，R5 三案→通用）：
  `docs/design/PLAN-foundation-task-completion-20260611.md`
  （五支柱：坚持力 run 续航/交付纪律 出口走门/边界正确性 锁与读边界/协作闭环
  引导前移/结论完备性 证据契约；R6 同 prompt 重跑三任务总验收）
- 方向修正纲领（R9 质量退化复盘 → 稳而不管）：
  `docs/design/PLAN-stability-not-control-20260612.md`
  （流程复杂性藏框架层模型无感；质量靠 skill 知识非验收门；测试 prompt 永远
  普通用户自然语言。2026-06-12 四批落地：底座修复/召回接通+skill 树千级地基/
  长期助手 工程移植〔退避抖动/错误分类/结构化错误/per-thread 中断/turn 预算〕/
  性能缓存，明细见 REFACTORING_BACKLOG 同日条目）

以后新增长期设计，只写摘要和链接，不再把完整方案塞回这个文件。

## 2026-08-25 子代理局部文件编辑能力单一权威【状态：`1082ccf` 已推送，待长任务安全点部署】

- 同一长 TUI 的 Click Python→TypeScript 复刻中，首个 worker 已收到“不用整文件覆盖”的角色提示，实际
  capability snapshot 却只有 `write_file/apply_patch`，没有仓库既有的 `edit_file`。一次补丁仅因期望行多
  两个前导空格而未命中，工具又只返回路径；模型随后反复重写整个文件并制造重复声明。因此问题不在
  Compact summary，而在工具可用性与失败反馈没有形成同一条主链。
- 文件写工具的唯一成员与顺序统一收在 `tooling/write_boundary.py` 的
  `WRITE_TOOL_ORDER/WRITE_TOOL_NAMES`：直接 coding child、递归 leaf、角色模板、capability grant、写围栏、
  路径 gate、进度与验证投影都消费它。父级显式缩窄工具仍是权限上界，缺少 `edit_file` 时后代不得扩权。
- 对照 会话运行时 apply-patch 的 expected-lines 错误，`apply_patch` 仍保持逐字严格匹配和零写入，只在未命中时
  有界回显最多 4,000 字符的期望原文并指出空格/缩进；一处或少数片段引导使用已有的空白容错
  `edit_file`，多文件关联修改继续使用 `apply_patch`。不增加目录锁、自然语言裁决、自动重试或整文件兜底。

## 2026-08-25 当前主任务 Working 时钟【状态：本地 focused 通过，待单 Gateway 真机复验】

- 终端交互 的 spinner 时间只读当前 query 的 `loadingStartTimeRef`，并在 query 从 idle 进入 active 的同一渲染
  立即重置；不会把 session 或组件首次挂载时间当本轮耗时。本项目对应的持久当前 query 身份是
  `ConversationThread.workspace_task_id`，起点是它对应 active `ThreadTaskLink.created_at`。
- `conversation_agent_activity.v5.main_activity` 的阶段/context 仍可来自进程内易失 sink，但 task id 与时钟
  必须重新绑定当前 workspace root。较晚创建的 child link、旧 root sink 与 TUI background block 年龄只可
  作为各自展示事实，不能替换主任务时钟；Gateway 重启后 sink 丢失时以 root 起点和 `waiting` 恢复。
- renderer 只读 `main_activity.started_at`。字段缺失显示 `0:00`，不新增客户端时钟状态、不猜 prompt、不影响
  lifecycle/Compact/完成权威。详细合同同步见 `docs/modules/gateway/04-structure.md#tui-后台活动投影`。

## 2026-08-21 子代理递归控制面与自然收口【状态：直属活动区已真机验证，lane 自愈待补】

- 问题根因：历史实现同时暴露“创建—调度—推进”多个模型工具，并用
  `acceptance_checks` / `verification_status` / 交付扫描器二次裁决任务是否完成。这使模型
  需要人工“推”已创建的 child，并会因缺验收字段、结果格式或第二状态而挂起。
- 完成权威：普通主代理与子代理都只读宿主 `turn_end.reason`，枚举为
  `completed/blocked/max-tokens/aborted/error/interrupted`。不解析回复文案，不扫描目录，
  不读测试数、证据数或历史 verification 状态来改写完成。
- 递归工具面：根与后代共用唯一 `create_subagents`，创建成功后由宿主自动启动。
  模型侧 `dispatch_subagents` 与 `schedule_child_subagents` 已删除；内部 dispatcher 仍保留，
  只负责 runner 启动、租约、恢复和有界重试。
- 已删除无人使用的 `SubagentDispatchEnvelope` 与 decoder 分支。宿主为自动启动子进程保留内部
  `subagents-dispatch` 入口，但普通状态页、doctor 和模型 prompt 都不再要求用户或模型手动催跑。
- 父级日常模型动作只保留创建直接下级、按需只读查看当前可见代理树、给一个直接下级插入补充消息、
  打断/取消一个直接下级，以及裁决该直接下级的结构化 capability 请求。旧 `inspect_agent_tree` 的宽参数
  工具已删除；`list_agents` 只是 会话运行时 式有界只读适配，复用 `/status`、诊断、恢复和 TUI 的同一 canonical
  投影，不启动、推进、等待、重试或取消 child。父级不靠查树、shell `sleep` 或周期调用推动 child；正常
  等待仍只消费宿主生命周期事件，只有用户问状态或需要核对明确 run 时才按需读一次。
  子代理、孙代理和根的差异只由 `run_id/parent_run_id/root_run_id`、工作区和递减权限表达，每一层只管理
  自己的直接下级。
- 这五个直属控制入口不是每个 child 的固定工具。根主代理和结构化
  `can_spawn_children=true` 的 coordinator 才持有；worker/researcher/tester/writer/bug-finder 等 leaf
  只执行自己的任务和必要的 `capability_request`。若某一层需要再拆分，父级应把它明确创建成
  coordinator，而不是给普通 leaf 塞一组永远用不到的管理工具。
- 模型侧 `raise_event` 同步删除，不把“子代理自报进展”作为第五个递归控制工具。普通活动、权限申请、
  阻塞和终态由宿主从 runner/thread 的 typed 生命周期直接写入父级事件账本；长期 Audit/监控也调用内部
  observation/wake service，不再绕回模型工具。
- 模型可见的 `cancel_subagents` 只接收直属 `run_id/run_ids + reason`；`root_id/status/dry_run/
  kill_process` 和整树回执都是宿主运维内部面。因此打断入口不能被模型当成隐蔽的查树/轮询工具。
- `.7` 原样 TUI 在 `e321483` 上实锤另一个底层断层：前台默认把整个 `orchestration` category
  收进 `tool_search`，MiniMax 首轮实际看不到 `create_subagents`，因而说要派 5 个 child 却只调
  Bash/Write。对照 会话运行时 multi-agent v2 的 `ToolExposure::Direct` 后，当前默认不再延迟
  `orchestration`；创建、直属插话、打断和权限裁决从第一次模型调用就可见。这是工具快照配置修复，
  不解析“必须子代理”等用户文字做机器路由。
- 旧 `InspectAgentTreeTool`、模型 Schema、公开导出和专用工具测试已删除，不保留“虽未注册但仍像工具”
  的影子入口。内部代码直接读取 `agent_tree_status_payload`，它只是 `/status`、TUI、恢复和诊断的状态投影。
- 所有模型可见的子代理工具快照（含历史配置、持久化 grant、角色模板和递归继承）统一经过
  单一退休工具过滤器，防止旧账本把已删除的查树/手动推进入口重新带回模型工具箱。
  显式 `allowed_tools=[]` 表示零工具，只有 `None` 才按角色默认值派生；权限链始终只能逐层减少。
- 历史兼容：旧 task 中的 `acceptance_checks` / `verification_status` 字段暂保留以读取已有账本，
  但不进入当前 TaskEnvelope、runner 模型摘要、父级 wake、树摘要或完成判定。
- 默认资源护栏收紧为同 owner 最多 6 个未结束 child、每次递归创建最多 4 个、
  `runner_concurrency=auto` 时同时运行最多 4 个。这是资源边界，不是按任务文字硬编排子代理数量。
- 同一权威父代理可像 会话运行时 `spawn_agent` 一样分多次补充不同 child，直到上述会话容量耗尽。
  是否已有活跃 sibling 不能成为禁止第二批的机器门；重复请求继续由显式 idempotency/work-scope
  身份复用，并发父执行器继续由 active-turn claim 拒绝，容量继续由 root session 槽位原子控制。
  已退休的 `SUBAGENT_ACTIVE_LINEAGE_EXISTS` 不保留 Audit 专项绕过或后台来源分支。
- `.7` 首轮真机发现“owner 历史未终态 run”会永久吃光新 TUI 容量。对照 会话运行时 每棵 root session
  独享 `AgentControl` 后，`max_subagents` 改为当前根会话树上限；owner policy 的管理员配额仍全局计算。
  容量/冲突类整批拒绝同时显式标记 `effect_outcome=not_started`，不再被副作用账本误包装为
  `TOOL_OPERATION_OUTCOME_UNKNOWN`。
- `.7` 第二轮真机发现最后一个 child 在后台模型采样期间结束时，旧回合只看见 `2/3`，最终化却读取
  新状态 `3/3` 并提前关闭根任务，导致最终整合和启动丢失。对照 会话运行时
  `core/src/agent/control.rs` 的完成状态订阅以及 `tools/handlers/multi_agents/wait.rs` 的明确采样边界后，
  child lifecycle 后台轮会冻结采样前树阶段；从活跃树开始的旧回合不能关闭根任务，最后一条终态事件
  必须获得新的模型回合。该门只保证事件新鲜度，不判断产物质量，也不恢复机器验收。
- 后台轮现在只在开始时冻结一次 child phase，prompt、最终化和用户投递共用这份快照；同 root 的 DONE
  通知只有 `created_at` 不晚于该采样时刻才可随本轮合并确认，晚到通知继续 pending 并触发下一轮。新鲜
  回合看到全部 child 终态后，模型自然回复可直接投递，不再等待 root task link 先写成 `completed`；
  task link 继续负责恢复和停止，不再充当普通任务的第二完成验收器。
- 模型可见的父子消息按递归关系收成 `send_guidance(target, message)`：只允许给当前代理直接创建的
  一个下级插入普通消息。删除 thread/task/case、批量 run、整棵子树、priority 和 delivery 控制面；
  用户给主代理的插入继续走 active-turn 输入链，子代理管理孙代理，根代理不越层代管。
- 删除 `create_subagents` 自动登记的周期性 LLM 巡场和对应 `wait_tool.py`。child 进展、完成、阻塞和
  capability request 只通过真实生命周期事件唤醒直接父级；进程心跳、孤儿回收、失败重试仍是宿主
  liveness 底座，不是模型推动工具。旧 `dispatch_supervision_auto` policy 升级后按结构化 tool 标记退休。
- `.7` 真机随后证明，仅删 `wait` 还不够：`create_subagents` 回执仍把 `inspect_agent_tree` 写成
  `next_action/status_tool_call`，MiniMax 因而连续执行 `inspect_agent_tree + shell sleep`；同时 child 已写入
  OPEN capability request，普通模型回合却被 `turn_end=completed` 直接投影为 `DONE`。当前约定改为：
  create 回执只返回 `await_lifecycle_event` 和本批 run ids，不返回下一工具；OPEN/非法待裁决 capability
  request 是宿主掌握的结构化阻塞事实，优先把该 child 落为 `BLOCKED`，grant/deny 后由事件恢复同一个 run。
  这不是产物质量验收，也不读取模型正文。
- 对齐 会话运行时 child 继承父线程 `cwd` 与 sandbox 上界：普通 child 自动继承当前 Agent 的结构化 workspace
  write roots，后代只继承同一权限上界；`output_files` 只负责交付目标、展示和验证线索，
  不再要求模型为了写父级本来就有权写的项目目录重复声明权限。命名 Audit/exact-scope worker 不继承该
  普通写根，继续使用其精确结构化权限。
- 后台主代理权威身份以精确 `task_id` 为先，不能复用同 thread 旧任务的 `bg-main-*` run。run 命中其它
  task 时必须回退当前 task 的主链再建 attempt，避免整轮工具因权威链错挂而被拒绝并要求用户发“继续”。
- 主代理 run 或 current attempt 为结构化 `unknown` 时，wake、observation 与 progress policy 必须保留原账
  但停止自动挂载，不得通过重试风暴或创建第二棵主 run 绕过执行权闸。同线程观察按
  `thread_id + root_task_id` 分批，阻塞旧任务不占新任务消费限额。只有
  `recover_attempt_unknown` 在人工核对副作用后同时恢复 current attempt、崩溃调和写入的 run 状态并释放
  精确 attempt 锁，原事件才自然续跑。该边界对照 会话运行时 的 typed `AgentStatus` 终态通知：宿主订阅状态并
  向直接父线程注入一次事件，不靠周期 LLM 催促或反复重挂异常会话。
- 普通 child 的可写上界来自直接父级工作区，不再靠 `output_files` 重复授权；因此父级本来能写的项目目录
  可直接交给 child。`create_subagents.items[].output_files` 仍用来登记用户明确交付位置、产物归属和验证线索，
  但 goal 或 output_files 都不能把范围扩大到父级工作区之外。TUI 后台 notice 首次立即查询、之后每秒查询，
  并为同 thread 每条消息生成独立 block id，避免 reducer 丢掉第二条以后更新。
- child 启动时的父级共享阅读上下文只来自当前 tool loop 的 archive。旧的 agent-level
  `_parent_shared_context_packs` 缓存已删除，不能把上一任务读过的文件预览自动塞进下一任务；跨轮长期事实
  必须走正式 transcript/Compact/refs，而不是不可审计的进程内残留。
- `c2c0235` 的 `.7` 原样 TUI 任务创建并自动启动 4 个 child，真实暴露两个底层缺口：宽泛 `/root`
  forbidden 误压过更窄 task output allow，导致同一 child 连续 `WRITE_FORBIDDEN` 并重复申请已授予权限；
  真实 ToolResult 字段是 `tool_name`，旧状态投影却读取 `tool`，所以 canonical state 长期只显示空
  `RUNNING/0%`。写边界现按 会话运行时 FileSystemSandboxPolicy 的最具体条目优先、deny 同层胜出；runner 的
  模型/工具边界写有界活动短状态，工具进度读取 `tool_name`，不公开模型正文或思考原文。
- `bea6fed` 原样任务已能产出页面并让四名 child 全部 `DONE`，但 422.849 秒内发生 32 次模型调用，
  累计输入估算约 19.47M tokens。结构化日志证明不是模型必须被“催”：task-local 父级创建下一层后进入
  `PENDING`，通用孤儿续跑却马上再次采样它；嵌套 child 的完成事件又越级进入根会话，形成反复查询和
  重读大上下文。当前权威规则是每层只订阅直属孩子：创建后耐久记录精确 child ids 并以
  `interrupted/SUBAGENTS_ACTIVE` 让出；同批成功收齐才恢复一次，失败、缺记录或 capability 阻塞立即恢复；
  新工作片从 `direct_children` 获得有界 status/result/artifact refs。崩溃恢复先调和这份等待记录，再运行
  通用孤儿复活。任何层级使用同一规则，不为“孙代理”另造合同。
- `task_progress` 正式降为软记事账本：已删除普通任务的自动 continuation 模块、配置项和递归深度字段。
  open 清单不再开新模型工作片，也不再参与完成判断；只有 Compact、显式 `/goal`、用户插入或 typed
  child/control event 可以续接。进度写入必须以稳定 id/status 表达，新建项还要可读 title；写后重新按
  canonical child run id 对账，避免模型提交的旧状态遮住真实终态。
- `create_subagents` 的输出锁拒绝必须保留 conflict 来源。`conflicting_proposed_index`
  表示同一批 item 互相重叠，必须修改参数后重试；非空 `existing_run_id` 才表示真实运行中的
  同级占用，由宿主生命周期事件唤醒。整批原子拒绝不会改变用户原始约束，更不是授权父代理
  改用用户禁止的方式。这是通用工具结果合同，不从 prompt 词句推导状态。
- 递归 child 的工作区权限对齐 会话运行时 `build_agent_spawn_config/apply_spawn_agent_runtime_overrides`：
  从当前 turn 继承 cwd 和 permission profile，不再叠加一条与 inherited workspace 完全同路径的
  默认 home deny。该调和只适用 local/unmanaged owner，且仅删精确同路径项；更窄的 `.ssh`、
  `Desktop`、`Downloads` deny 和所有远程 owner 宿主 home 围栏仍硬拦。来源只读 task 的
  `owner + attributes.workspace_root(s) + allowed_write_roots`，不从 goal 或模型声称授权。
- TUI `/stop` 有两种同一控制合同下的定位方式：前台正在提交/执行时必须带
  exact turn id；前台已让出但当前 conversation 仍有子代理/background claim 支撑的唯一 typed
  live task 时，可以不带 turn id 交给 Gateway 按 owner/thread 停止该任务。多个冲突 live task 或只有
  历史终态时继续 fail closed，不猜目标；`/btw` 始终要 exact active turn。
- `d928d77` 的 `.7` 单 Gateway 原样任务已证明四名 child 都能一次自然 `DONE`、最后一名会自动唤醒主代理，
  服务也真实监听 `0.0.0.0:8080` 且 loopback/LAN HTTP 200；但产品文件被错误写到 task 内部
  `output/abc`，`/root/abc` 为空，child 上下文还混入旧 `/root/kill-ws/...` 读取包，前台让出后 TUI
  没有常驻 Working。当前切片统一修复三点：裸 `abc/...` 相对当前 `/root`，显式 `output/...`/`work/...`
  才走 task 内部目录；共享阅读包只取当前轮；`/client/notices` 返回 canonical
  active task link 与 canonical child run 的有界快照。TUI 在输入框附近用一个可移除的
  灰色 Working 区域显示直属 child 名称、结构化状态、职责短标题、耗时和 attempts；查询失败保留
  上一次有效投影、真实 active root 归零才收起。该块只展示状态，不会推动、重试或验收任务。
- `dispatch_subagents` 这个模型工具和手动步骤已删，但“获取容量、启动/恢复 runner、投递首条任务、
  登记 heartbeat/status、有界重试及投递终态事件”是 create 之后必然存在的宿主生命周期。对照
  会话运行时 `会话运行时-rs/core/src/agent/control/spawn.rs::spawn_agent_internal` 与 `control.rs` 的
  `send_input/subscribe_status`，当前可继续把内部旧命名/入口折叠进 `create_subagents` 后的
  `start_or_resume` 服务，但不能删掉这些生命周期动作本身，否则 create 只会造一张不运行的工单。
- 用户与代理的控制权分开：代理模型继续只能管自己的直属 child；通过身份验证的 root owner 将可管理
  自己主代理树内的任意后代。后续 Gateway/domain 只建一份 TUI/Web/IM 共用的 typed control
  protocol：`list/read`、`message`、`interrupt`(只中断当前 turn，session 仍可恢复)、
  `resume/start`、`cancel/close` 和 capability 裁决。写操作必须携带稳定 `operation_id`、
  `owner/thread/root/target_run_id`、预期版本/状态与 accepted/rejected/unknown 回执；前端不直改
  canonical 账本。当前 `cancel_subagents` 是终态取消，不得冒充 会话运行时 式可恢复 interrupt。本轮只落地只读
  活动投影，写控制协议仍为待实现设计。
- `714c0c8` 在 `.7` 单 Gateway 的真实 TUI 已验证直属活动投影：两个 child 一次 attempt 自然完成，
  固定区域按 canonical state 展示启动、模型、工具和终态，不需要模型巡场。该轮同时证明 durable wake
  与 process-local lane 是两层事实：第二条 DONE wake 已落盘并被 read-only ready 发现，旧进程却没有
  取得 claim；优雅重启后立即取得 claim 并完成汇总。后续自愈只能基于稳定 lane identity、future
  queued/running age、durable claim/heartbeat 和 operation receipt，不得把自然语言“卡住了”当触发器，
  也不得用自动重复模型调用掩盖丢失的进程内占位。
- 直属 child 面板对齐 终端交互 的显式 task description 和 suffix width budget：`create_subagents` 可携带
  一句职责短标题并写入 canonical task，面板不再用 runner 当前阶段充当职责。token 列定义为最新一次
  provider preflight 的 `current_tokens`，不是跨轮累计；description、goal 和 token 都只作展示，不进入
  生命周期、权限、恢复、验收或完成判断。每行固定为一行，先保留耗时/ctx/Compact/重试，再截断短标题。
- Todo 与 coordinator panel 是两个 view：派工自动 seed 仍是恢复/关联所需的 canonical 进度事实，但当
  seed `item.id` 精确等于当前直属 `child.run_id` 时，TUI 只在 Todo 视图隐藏它，职责由下方 child 行承接。
  普通 Todo 与 covers 不隐藏；不能解析标题、goal 或“子代理”字样做去重。

## 2026-08-18 候选消息实时流式 + 每轮阶段计时【状态：本地 focused 通过，待真机部署复验】

- 底座问题（真机实测 18.5s/简单回复）：模型→Gateway 已是流式，但 Gateway→TUI 把正文暂存
  `_model_segment` 直到工具边界或终稿才发布，观感非流式；且每轮调用模型前固定约 13s 准备。
- 契约改动（`request_execution.py` BufferedChunkStreamWriter）：rich 客户端（TUI）在 `write_model`
  收到增量时按既有节奏（128 字符/换行/0.08s）实时发布 typed `model_delta` 事件（展示级脱敏，
  不做 `project_user_reply`——那会把未完成文本当终稿投影）；`assistant_commentary` 保留为工具边界的
  冻结/归过程标记（带全文，兼容旧客户端）。普通客户端/飞书 adapter 不消费 `model_delta`
  （projection fail-closed），行为不变；未来渠道要流式 = 请求侧开 rich 能力 + adapter 侧自选投递节奏
  （如飞书限频合并更新），不改 Gateway。增量在 steering 时与 `_model_segment` 同步清空。
- TUI 侧（`tui_runtime.py`）：`model_delta` → 实时追加活动 assistant 块；`assistant_commentary` 在已有
  流式活动块时只冻结（不重复追加），无活动块时回退旧契约整段落地；冻结段带 `process` 标记 →
  renderer 默认折叠为摘要行（Ctrl+O 展开，与工具块同一层级）。终稿仍以 canonical terminal 为准，
  `finalize` 覆盖流式正文且不重复。本地模式工具边界同样归为 process，TUI 双路径一致。
- 阶段计时：请求响应新增 `stages_ms`（`request_read_ms/conversation_prep_ms/run_ms/execution_ms/total_ms`），
  同值打结构化日志；终端归档整体携带，可作为测试证据。13s 的缓存修复待真机测量后定，不先猜。
- 测试：writer（rich 实时 delta/非 rich 不发/边界顺序/脱敏/steering 清空）、TUI（delta 实时块/
  冻结不重复/旧 Gateway 回退/process 标记/终稿覆盖）、plain projection fail-closed 均已补；
  gateway/tui/chat 切片 1137 passed。

## 2026-08-13 P2-5 SecretStore 决策：停用（已于 2026-08-28 物理删除）

HANDOFF_reliability-gaps-20260813.md P2-5 要求人工拍板「接线 or 停用」。
决策=停用（owner 授权执行席位决策，记录在案）：

- 现状核对：SecretStore 唯一实例化点在模块 docstring 示例（无工厂函数），
  生产命令/网关零消费点；embedding.py 仅用抽象 secret_resolver（注入，
  不依赖 SecretStore 实例）；真实密钥以 0o600 明文 + /etc/my-agent 环境
  文件承载。
- 停用理由：接线涉及 provider key 解析链大改面（embedding key / 各
  provider key 解析入口 + 配置开关 + 测试），收益低于风险；「支持加密
  密钥库」承诺暂不兑现，如实文档声明避免误导。
- 2026-08-28 收口：在再次证明生产零消费点后，删除 `secret_store.py` 与两份
  只验证废弃实现的测试，不再把死代码当回滚方案。embedding 继续只接受上层
  注入的 resolver；飞书/企业微信加密回调仍使用 `cryptography`，已发布的
  `secrets` extra 名称暂留作安装接口兼容。
- 如未来重新引入密钥存储，必须作为新的设计从真实 provider/adapter 消费链
  接入，不能复活旧类或复制旧文件。

## 2026-08-15 attempt 级 UNKNOWN 结构化终态 + 人工恢复路径（双席 seq1947 核对点3 / seq1948 证据4）

【决策】孤儿回收遇 UNKNOWN 工具操作/外部副作用未核实 → attempt 层标
结构化 `unknown` 终态（不再永久 running 无感知），但不释放执行权锁、
不自动续跑；唯一出口 = 人工核对后显式 `recover_attempt_unknown`。

- attempt 状态机：running → unknown（自动发现）→ recovered（人工恢复）。
  unknown ≠ failed（已知失败才 failed）；unknown 不触发自动续跑
  （create_attempt 的 unknown 闸 fail-closed 抛 RuntimeConflictError +
  attempt_unknown_blocked 诊断事件，recovered 前任何自动拉起被拒）。
- 锁语义：_mark_attempt_unknown 保留执行权锁（external 副作用未核实前
  锁防并发写执行权）；recover 事务内 CAS（unknown → recovered）成功才
  删锁 + 写 attempt_recovered 审计事件（含 operator/effect_disposition
  结构化三值：confirmed_noop/recorded/abandoned）。
- 幂等闭环：reclaim 前置 already_terminal（同 attempt 只标一次）；
  orphan_reclaim_blocked / attempt_unknown_terminal 事件同 attempt+reason
  只写一次（孤儿回收每 ~5 分钟一趟不刷屏）。
- 四层保持分开：只动 agent_attempts；run 保持 created（可恢复后同 run
  续挂新 attempt）；task/session 层不受影响。
- side_effect_gate 与 nonterminal_ops 两分支同款（问题C 只补可见性，
  本条目补结构化终态）。

【验收】37 passed（test_cli_resume_contract 含 4 新用例：side_effect_gate
标记、create_attempt 拦截、recover 放行+幂等、活 attempt 拒绝 recover）+
全量 gate 回归。

【待办】真机 zombie 复现四类原始证据（diff/工作树 + orphan_reclaim_blocked
完整字段链 + 重启无重复 claim/handoff + UNKNOWN 人工核对/显式恢复路径）。

## 收口状态机（owner 四改之 2）【状态：设计完成，待实施】

- 详见 `docs/design/closeout_state_machine.md`。
- 摘要：把散在 _final_response_after_*/response_decision/resume_loop/
  gateway 四处的"停下来后下一步"收拢为一个纯函数 `decide_closeout`——
  终态 done/cancelled/wait_human/wait_handoff/resume_round；停止原因采集
  方只报因，承诺文案由 state 唯一决定（"会自动继续"仅当 machine 真的会
  续）。保留 EXEC-30/35/39 语义与 resume 能力（goal/cron 模式用）。
- 参考：会话运行时 无系统侧收口机（模型自然停=done，goal 扩展只做目标层轮次）、
  任务运行时 /goal = goal-round-driver 同款。
- 实施切 5 步（见文档 §6），每步独立提交。

## gateway 移交线删除与自动接力重建方向【状态：已删除，重建待 goal/cron 设计】

- 2026-08-17 owner 拍板删除 gateway 移交线(EXEC-42, 见 ISSUES.md)。
- 结论: 任务不丢靠落盘(task.yaml/run_workspace.json/runtime.db), 不靠
  移交单; 移交单只是"谁自动接力"的中间层, 与 progress policy / goal
  续跑通道重叠; EXEC-39 已定正常不自动续跑。
- 重建方向(goal/cron 模式设计时): 以持久事实为单一权威新建自动接力
  驱动——未完成任务 = task link active + state.json 非终态 + runtime.db
  非终态(现有 unfinished_task_ids 三源交叉已具备), 驱动 = goal-round
  driver(任务运行时 同款)+ cron tick; 同时解决唤醒轮每 2 分钟全量扫描问题
  (增量游标/单次扫描多消费)。

## 唤醒轮全量扫描治理【状态：已实施 2b——wake_queue 热层+对账分频，收尾项见下节】

- 实锤(2026-08-17 源码核查): BackgroundMainAgentScheduler.tick() 无内部
  节流, supervisor 轮询 1-5s 单飞提交——tick 内 _enqueue_unfinished_task_
  resume_wakes(glob 全部 task link + 全部 tasks/*/*/work/state.json + 每候选
  查 runtime.db)与 _consume_due_policies(全量读 policies 目录)以 1-5s 节奏
  反复全量扫描; 孤儿回收/账本 gc 已有 5min/6h 频率门, 但前两者没有。
- 参考实现(已读源码):
  - 长期助手(cron/jobs.py get_due_jobs + gateway/run.py housekeeping):
    单一 jobs.json 每 tick 全读但内存按 next_run_at 过滤(单小文件, 无
    目录树遍历/无逐候选 DB 查); tick 60s; 文件锁单 tick; catch-up 折叠
    (过期 recurring 只补跑一次并快进 next_run_at, 防重启爆量); 家务
    按 tick_count%N 分频(5min/每小时), 重活不进每 tick。
  - 通道运行时(src/infra/heartbeat-runner-scheduler.ts + commitments/store.js):
    到期即数据——SQLite due_earliest_ms/due_latest_ms 索引 WHERE 查询,
    per-agent 内存 nextDueMs Map(不到期不重查), cadence 由系统 cron 每
    agent 一个 monitor job 携带持久化权威; 事件唤醒过集中 cooldown
    (min-spacing + flood 环形缓冲)。无中央全量扫描。
- 治理方案(定案, 分步实施):
  1) tick 内节流门: scheduler.tick 加最小间隔(如 15-30s 全量 pass, 期间
    只做便宜消费); 2) _consume_due_policies 按 next_due_at 索引只取到期
    (policies 目录单文件化或内存有序索引); 3) unfinished_task_ids 三源
    对账从热 tick 移到低频层(5min, 长期助手 分频同款), 热层只查内存缓存
    的到期任务集, 由事件(任务状态变更/wake)失效刷新; 4) catch-up 折叠:
    漏窗任务补跑一次并快进(我们已有 wake cooldown, 对齐即可)。

## wake_queue 闹钟字条与 clock.sleep 设计【状态：2a/2b/2c 已落地，待真机验证】

- 定案(2026-08-17 owner 确认): wake 表不是"调度任务表", 而是**任务自己写的
  闹钟字条**("我在 T 时刻醒")。写入方两类: 1) 模型主动 `clock.sleep`(对齐
  会话运行时-rs core/src/tools/handlers/sleep.rs, 1s..12h, 可被新输入/事件打断);
  2) reconcile 对账层给 active-goal 任务补 `goal_tick` 字条(EXEC-39 同源:
  普通任务不自动醒)。唤醒方是系统侧 BackgroundMainAgentScheduler 热 tick
  弹到期字条(跨进程唤醒, 与 会话运行时 进程内 sleep 不同——我们回合结束进程退
  出, 不能挂进程等)。
- 表语义(runtime_db/schema.py + repository.py):
  - 每任务一条 pending 字条, (task_id, kind) 幂等 upsert, 新写覆盖旧时间;
  - 到期弹出 = 同事务 SELECT 到期行 + 置 woke, 再派 wake 并 complete/cancel,
    避免同秒双消费; task 终态(completed/cancelled/interrupted 等)或事件提前
    醒(wake signal 消费成功)时 cancel_wakes_for_task 清字条;
  - 清理: 无全表扫描。已弹出字条归 complete/cancel 终态, 由既有低频 gc 与
    唤醒侧检查归档(task archive 时核对)回收, 不新增专扫。
- 已实施:
  1) schema 建 wake_queue 表 + (status,next_due_at)/task 索引, repository
     upsert/pop_due(事务内选+标记)/complete/cancel/list/stale;
  2a) 热 tick 改 `_consume_due_wake_queue`(索引化到期查询) + `_reconcile_
     wake_queue` 降到 5min(只给 active-goal 任务补 goal_tick);
  2b) SleepTool 注册 + `_cancel_sleep_wake_on_event` 事件提前醒桥;
  2c) 睡眠收口闭环 + 字条生命周期:
     - 工具循环自然停出口 `_sleep_wait_closeout_if_asleep`: 本轮最后工具是
       sleep 且纯 ok 收口 → CLOCK_SLEEP_WAITING/clock_sleep_tool;
     - decide_closeout 新增 sleep_wait 态(sleeping 结构化事实, 优先级
       done > cancelled > sleep_wait): 任务非终态、字条保留、不进续跑族;
     - CLI 字条生命周期: sleep_wait 不清字条; done/wait_handoff/wait_human/
       cancelled/预算耗尽清字条; run --resume 入口清字条(用户显式续跑=
       事件提前醒, 会话运行时 sleep 可打断语义);
     - 热层弹出前核 link 终态(terminal/missing 直接作废闹钟, 不复活任务);
       对账只补缺失字条不重写到期时间(否则每 5min 把闹钟往后推, 永远不响);
     - 对账跳过 audit work_kind 任务(自有观察/audit 唤醒通道, 防双重拉起)。
  3) 测试: test_wake_queue + test_sleep_tool + test_scheduler_wake_tick(旧
     task_ledger_resume_wake 契约重写) + closeout truth table(含 sleeping
     维度) + resume 合同睡眠/清条场景, 全部通过。
- 改完后与 长期助手 差异(对比 cron/jobs.py + gateway/run.py housekeeping):
  - 长期助手 是**用户显式排程表**(schedule 工具写 jobs, scheduler 到期跑);
    我们是**模型自报闹钟字条**(sleep/goal_tick 写 wake_queue, tick 弹),
    schedule 工具链(owner 持久 at/every/cron)是另一条独立主链, 不与字条混;
  - 长期助手 tick 60s 全读单 JSON 文件内存过滤; 我们 tick 更热但走 SQLite
    (status,next_due_at) 索引只取到期行, 且 wake 派发本身有 per-task
    cooldown(_TASK_RESUME_COOLDOWN_SECONDS=900) 吸收风暴;
  - 长期助手 catch-up 折叠(过期 recurring 补跑一次快进); 我们字条到期后由
    同一 cooldown + pop 事务的单次消费天然折叠, 不重复派;
  - 长期助手 家务按 tick_count%N 分频; 我们对账层(5min)+孤儿回收/账本 gc
    (既有 5min/6h 门)同构。
- 收尾项(真机): sleep 端到端真实验证(gateway 模式下模型 sleep → 回合结束
  sleep_wait → 到期调度器唤醒续轮), 三源对账分频观测, goal_tick 稳态节奏
  (弹→醒→再武装)真机确认。完成后把上节治理清单状态改为"已实施"。

## 2026-08-18 活动输入与外部消息持久投递收口【状态：本地 focused 通过；真机竞态复验中】

- 对照 会话运行时 `active_turn` 锁，普通补充消息以稳定 client message ID 和首次绑定的 exact turn 为唯一身份。
  Gateway 的 turn transition 与 conversation mailbox guard 按 `T -> M -> receipt` 顺序裁决；全局“当前活动”
  投影冲突、文件暂缺或损坏只能返回 unknown，不能把仍活跃回合的 pending 回执粗暴改成 rejected。
- TUI 在 POST 前写单一 session outbox；提交拿到真实 Gateway ID 后只读 `/input-status` 对账。worker 尚未取得
  canonical ID 时 `running_request_id` 保持为空，本地 `chat-*` 永不冒充 expected turn。active→queued 会冻结
  inject、prompt files、save、resume、客户端能力和一次 chat-style 注入；拒绝后本地排队与服务器排队语义一致。
- guidance receipt v4 读取时从 embedded entry 重算正文 digest、核对 server-owned dedupe key 和状态字段组合；
  v3 只经显式迁移写回 v4，不能因版本条件写反而反复重写或让被篡改正文沿旧 digest 进入模型。
- Gateway 请求文件名是 claim 锁、active turn 与唯一终态归档共用的请求 ID 权威。claim 后若正文 `id` 或
  可选 `request_id` 与文件名不一致，必须在同一 T 锁内规范为文件名、保留结构化 identity error，并由
  worker 在任何 owner 解析和 provider 调用前失败收口；正文不能把已认领文件重定向到另一个回合。
- claim 同时保存 `gateway_request_fingerprint.v1`，指纹覆盖 owner/conversation、prompt/task 和所有会改变
  执行的 options，但排除 lease/status/projection 运行字段。canonical terminal 必须携带并重算该指纹；
  恢复器只能删除与 canonical 指纹相同的 hot 请求，不能因 request ID 相同或最终文案恰好相同就合并。
- inbox→processing 移动与 attempt/lease/fingerprint 栅栏是一个 T 锁转换；栅栏原子写失败时必须在同锁
  内把原文件回滚到 inbox。不得留下 `status=pending` 的半认领 processing，因为 worker 和 stale
  recovery 都不能把它冒充为有效执行。
- worker 收口只认结构化 `committed/already_committed/stale_claim/conflict/retryable_io`；旧 attempt 的迟到
  答复只能得到 `stale_claim` 并丢弃，不能再根据“processing 文件不见了”猜测缺档或从 response 反造终态。
- stop、steer、provider admission/ACK 与 terminal 共用 exact-turn T 锁。reserve/submit 只允许 open 且未 cancel；
  provider 已成功返回后的 ACK 可在同 attempt 的 closing/cancel 状态下先收口 consumed，但 canonical terminal
  已存在或 attempt 已更换时必须拒绝。所有 audit/task/window stop 入口都调用同一个持 T 的
  `_mark_request_stopping_locked`，不得在锁外写 closing。
- 独立 `responses/<id>.json` 和 `done/failed` 永远只是展示投影，不能把 processing
  自动晋升为完成。HTTP `/result`、CLI/TUI/plain 轮询、同步 worker 等待、stale 恢复和普通
  输入对账都只读 `requests/terminal/<id>.json` 中经共享 reader 校验的 schema、文件名/
  内外层 request ID、请求指纹与 `terminal_response`。合法但
  陈旧的 response 由 canonical terminal 全量覆盖；孤立投影是需隔离的损坏事实，不能遮蔽 stale、
  阻止重放或完成回合。旧格式迁移只能走显式隔离工具，恢复和 worker 主链不在线猜测。
- TUI `/btw`、`/stop` 经 `/control` 时携带稳定 message ID 和窗口当前 exact turn ID；服务端 `/stop` 只在
  T 锁内重读到同一 owner/scope 的开放回合后写 closing。观察到 A 后切到 B 的控制请求不能作用于 B。
- Gateway TUI 的有效 slash 控制在 HTTP 前写 session-scoped control outbox；同一行冻结命令、message ID
  与 Enter 时观察到的 exact turn。拿到 `operation_id` 后只能 GET 状态，不能再次 POST；重启会恢复待对账
  行。非 steer 的 `terminal_unknown` 是终态；`/btw` 即使 wrapper 进入 unknown，也继续按同 operation ID
  只读查询已有 guidance receipt，直到 accepted/rejected，不能提前删除 outbox。`/btw`、`/stop` 在
  canonical turn 尚未绑定时明确不发送，避免服务端猜测后来的回合。
- `/ask` 与 `/control` 中所有有效 slash 控制在任何副作用前先写唯一
  `gateway_control_operation.v2` 回执；稳定 operation ID 只由 authenticated issuer/channel/conversation
  和 opaque client message ID 生成，命令正文、expected turn 与权限事实进入独立 digest。同 ID 同正文只
  重放首次结果，同 ID 异正文在副作用前 409；缺稳定 message ID 的外部控制直接 400。`prepared` 可安全
  执行，`executing` 崩溃重启后只能转 `terminal_unknown`，禁止把“响应没收到”当失败后自动重做
  `/compact`、`/goal`、`/audit`、`/verbose` 或 `/stop`。独立 `operation_id` 用于
  `/control-status/<id>`，目标回合继续单列为 `request_id`；`/btw` 的状态查询只读已有 guidance receipt，
  不得借 GET 再追加一次补充消息。若进程在 `executing` 落盘后、guidance receipt 写入前终止，GET 只能在
  同一 T 锁内以 exact turn 的 canonical terminal/closed 事实证明“从未投递”后转 rejected；活动、待恢复、
  缺失或损坏证据都继续 unknown，不能猜测后自动排入下一轮。
- control receipt 还冻结 `channel_chat_type/channel_chat_id` 和首次服务端解析出的 canonical
  `owner_provider/owner_kind/owner_id`；通道事实进入输入 digest，owner ref 使用独立完整性摘要。副作用执行和
  状态查询都只从回执恢复 exact owner，后续切换 owner-scoping 配置也不得重算到另一存储。adapter 对账必须
  从持久 payload 延续 issuer channel/user/conversation/chat type/chat id，
  通过 `X-User-Id/X-Channel/X-Conversation-Id/X-Channel-Chat-Type/X-Channel-Chat-Id` 查询；同一 provider
  message ID 不能通过修改群聊身份重放到另一个 owner。
- Gateway 的 canonical 文件事务锁在 POSIX 使用 `flock`、Windows 使用 `msvcrt` 第 0 字节范围锁；两者都
  是真实跨进程阻塞互斥。平台同时缺少两种标准原语时必须 fail-closed，不能以告警后继续运行的方式依赖
  进程内线程锁而宣称 turn/receipt exactly-once；Windows 非阻塞分支也只把明确 lock violation 当竞争，
  句柄和文件错误必须原样上抛。
- 控制副作用的唯一 winner 仍在 C 锁内从 executing 走到终态，避免没有 provider 幂等键时误重放；重复
  POST 和 GET 使用同一 C 锁的非阻塞探针。锁正在被长 `/compact` 占用时立即返回原子 `executing` 回执，
  不堆积 HTTP 线程；只有锁已经释放且回执仍为 executing，查询者才有权把崩溃边界收成 unknown。
- IM adapter 在媒体下载、Gateway POST 和占位回复之前先写 durable ingress；same-id/same-body 重放，异正文
  quarantine，唯一 worker 继续推进 input receipt、request result、control receipt 和最终回送。
  ingress/reply 已用逐 row owner/epoch/expiry claim 实现跨进程 fencing，旧 epoch 的迟到结果不能写回；
  `/btw` unknown 已按独立 operation receipt 恢复。不支持幂等键的 provider delivery-unknown 仍列为后续风险。

## 2026-08-18 终端交互 TUI 可观察行为复刻【状态：已验收；C17 活动回合输入重开待真机复验】

- 功能规格：`docs/design/FEATURE-20260818-终端交互-tui-parity.md`。
- 逐项账本：`docs/design/TUI_终端交互_PARITY_MATRIX.md`；只有双端 PTY/ANSI 或确定性测试证据才能把
  项目升级为 `VERIFIED/MAPPED_VERIFIED`，源码阅读和肉眼相似都不算完成。
- 产品边界：只修改 `my-agent`，Python/prompt_toolkit 原生实现；终端交互 和 会话运行时 只读。终端交互
  命令、品牌和独有能力不复制，按 my-agent 的真实 dispatcher/ToolRuntime/ActionPolicy/session 合同映射。
- 状态链：`runtime/Gateway typed facts -> TuiEventAdapter -> versioned journal -> reducer -> stable/active blocks
  -> renderer` 是唯一显示主链。worker 不直接打印屏幕字符串，renderer 不从中文/英文正文猜工具、权限、
  中断或完成状态；未知事件 fail-closed 并留下有界诊断。
- 活动语义：thinking/text/tool/permission 都有稳定 `block_id`；delta 只更新 active block，terminal event
  原子冻结到 stable history，一次 terminal 之后不能因迟到/重复事件回退或双显。
- 输入语义：多行、history/search/paste/completion/queue/interrupt/exit 全部产出 typed intent。Gateway
  活动回合中的普通 Enter 复用 canonical `/control` steer，不再错误等待为下一轮；TUI 先按客户端
  `message_id` 固定显示 pending receipt，只有 runtime 在真实 guidance 注入点回传相同 ID 后才转为稳定
  user block。控制竞态拒绝时撤下 receipt，并且只允许同一正文回到既有 canonical ChatJob queue，不能丢失、
  双发或启动平行 worker。真正的 follow-up queue 仍可用 Up 原子回取编辑。
- 2026-08-18 `.13` 真机又证明 `/control` 的传输结果不能只用 bool【状态：已确认问题，方案待用户确认】。
  服务端已持久追加并在 active turn 消费后，HTTP 响应仍可能在客户端 2 秒窗口内丢失；若把该 unknown 当
  rejected，正文会再次进入 follow-up queue。推荐合同是 `accepted/rejected/unknown` 三态，并让同一 owner/
  thread/目标 turn 下的 opaque `channel_message_id` 成为 guidance 持久幂等键：unknown 保留 receipt，以同 ID
  查询或重试；只有 durable explicit rejection 才恰好转 queue 一次。单纯加长 timeout 不是正确性修复。
- 权限语义：UI 只是现有 ActionPolicy/ToolExecutor 请求与决定的交互投影；如果底层缺少续跑合同，先补
  通用 typed continuation，不在 TUI 内执行工具或重放用户 prompt。
- 权限底座已接通：TUI capability 显式声明、Gateway 原子 decision bridge、同 ToolCall 续跑、拒绝/取消
  ledger 和相同 `tool_name + args_hash` 防重复询问共用一条 ToolRuntime 主链；危险硬门仍直接 deny，不能
  为了展示确认框而弱化。
- 终端交互已收敛到 prompt_toolkit alternate screen：多行/反斜杠换行、FileHistory/Ctrl-R、slash/path
  completion、bracketed paste、单槽 stash、canonical queue、双 Esc/Ctrl-C/Ctrl-D、scroll/follow/unseen、
  transcript/search、mouse、resize、title 和 `?` help 都只修改 typed UI/intent state。
- FileHistory 只写入 owner-scoped `session_workspace/<session_id>/input_history`；不得在项目根创建
  `.chat_history`。这与 canonical session 的用户隔离一致，也避免 `search_text` 把当前提示词误当项目源码命中。
- renderer 已覆盖欢迎卡、用户头尾 10k 显示裁剪、Markdown/table/code/diff、thinking 折叠、全局 spinner
  metrics/stall、工具卡、审批 overlay、compact generation 边界和错误/中断；流式 assistant 可见时隐藏
  spinner，工具活跃时不误判 stalled。
- pending steer 与 follow-up queue 按 会话运行时 `bottom_pane/pending_input_preview.rs` 固定在 composer 上方，
  每条最多三行预览；它们不再作为 transcript 尾块随滚动消失。模型可见上下文则只接收 runtime 的数字
  白名单快照，固定显示当前估算、窗口和唯一 compact 触发线。活动回合原生工具 IR 真发生裁剪时另发
  `model_visible_context_compaction.v1` 展示事件；持久 main/child/grandchild 必须先提交同一
  ConversationThread checkpoint/generation，事件再投影该代次且不写 child canonical run。只有
  presentation/no-save 的窗口整理使用 turn-local generation。child 的常驻 `compact N` 始终只读独立
  ConversationThread generation。
- 会话恢复只投影 `cmd_chat` 已从 canonical session 加载的 user/assistant history；TUI 不另读写正文。
  journal、diagnostic、block cache 和 stable display window 均有显式上限，迟到/重复事件仍由 seen identity
  拒绝，裁剪不产生第二会话账。
- Gateway 启动状态必须是实际 readiness 的投影：alternate screen 先出现，后台 preflight 发布
  `connection_started/resolved`，成功后才启动唯一 worker；同步 pre-wait 只保留给 plain 模式。失败以
  typed error 和非零返回码关闭，不能假装已经可连接。
- 空闲动画时钟不应让静态长历史失效。完整 frame key 由 renderer 统一决定；connection/thinking 活动时
  才加入可见动画字段，thinking 周期取 glyph 与稳定词长的最小公倍数，禁止经验魔数。20k block LRU
  和 20k stable display window 是性能边界，不是新的会话事实源。
- TUI 鼠标采用 终端交互 式应用内模式作为唯一默认：`tui_mouse_capture_default=true` 时申请 mouse
  tracking，滚轮直接浏览、离底后保持锚点、回到底部才恢复自动跟随，左键拖选松手与右键都投影同一应用
  选区。F6 只切换当前 TUI process-local 状态，不写配置、不改 session；第一次切到宿主终端原生复制，
  再按恢复滚轮。远程复制仍必须如实区分应用/tmux/OSC52 投影与不可观测的外层系统剪贴板。
- tmux 剪贴板写穿只调用目标版本真实公开的命令：普通终端使用 `tmux set-buffer -w -- <text>` 同时更新
  paste buffer 与外层 OSC 52；iTerm2 因 SSH 崩溃风险只走 `load-buffer -`，再由应用 DCS/OSC 52 尝试外传。
  禁止把不存在的 `load-buffer -w` mock 成成功；最终系统剪贴板仍必须由用户在 attach 终端实际粘贴确认。
- 仅在 TUI 鼠标模式中，transcript 选择才持有当前 viewport 中经 prompt_toolkit `Window` 从屏幕列反解后
  的源字符索引，resize 清除；产品层不得再按 `wcwidth` 二次换算，否则中文会只复制一半。最终 focus 字符
  必须包含在高亮和复制文本中。任何形式的 mouse-up 都结束拖动；1003 无按键 motion 和下一次 fresh press
  负责收口窗口外遗失的 release。松手继续写 prompt_toolkit clipboard、OSC52 和 tmux buffer，Ctrl-C 可
  重复投影；SSH notice 只能说明“已选中并尝试投影”，不能声称外层系统剪贴板已改变，并明确提示 F6 回到
  原生模式。输入 Buffer 的显式选区具有更高 Ctrl-C 优先级。Ctrl-V 只粘贴应用剪贴板，系统剪贴板由终端
  bracketed paste 注入，两条路径都走同一大文本合同并替换现有选区。输入 marker 使用普通空格，禁止用会
  触发 `nbsp` 下划线样式的不换行空格。审批 overlay 继续允许 Page/wheel/Ctrl-Home/End 查看上文，Up/Down
  仍专属于选项导航。
- 审批 y/n 只能匹配 option 的 structured `decision`，不匹配 Yes/No/中文 label。普通工具结果最多六行；
  用户显式进入 transcript 并 Ctrl-E 后才展开全部，避免大输出常驻主视图。
- 终端交互 独有 model picker、permission mode carousel、team/buddy/voice/browser 等不造空壳；帮助和补全
  只列 my-agent 当前真实 dispatcher/键位。该映射属于用户明确允许的“命令/功能替换”，最终逐项记录在
  parity matrix，不用自然语言假装能力存在。
- 参考验收：外部 终端交互 provider 不作为 UI fixture 依赖，使用 loopback deterministic Anthropic
  协议服务驱动黑盒；MiniMax-M2.7 只做 my-agent 真机验收。两类证据分开，key 不进入任何 artifact。
- 测试机边界：仅 `192.0.2.13:${MY_AGENT_CHECKOUT}`，tmux `my-agent-tui`；现有 dirty tree 不归本任务，
  每轮按精确文件 hash 部署，并保留 `/root/tui-parity-evidence/baseline-20260818T0043CST` 回滚基线。
- 原验收矩阵共 85 项。2026-08-18 后续四路真机观察重开 C17：旧实现把运行中普通 Enter 错送下一轮，且
  queue preview 位于可滚动 transcript。当前为 37 `VERIFIED`、38 `MAPPED_VERIFIED`、9
  `NOT_APPLICABLE`、1 `IMPLEMENTED`；C17 只有本地确定性回归，必须部署 `.13` 并完成真实活动回合
  插入/结束竞态/滚动观察后才能恢复 `VERIFIED`，不得沿用旧 EV-QUEUE 冒充通过。
- reducer 仍是唯一实例和唯一 handler registry；权限处理器与 Gateway typed-row dispatch 仅以同文件内部
  controller/helper 拆分类体，使 strict code-size 不新增 hard blocker，不产生第二状态机、facade 或 fallback。
- 发布测试频率以改动规模为准：默认 focused tests；生产/测试代码新增、删除累计约 10,000 行以上或用户
  明确要求时，才额外跑一次全仓 pytest。本轮超过阈值，已运行一次到 100%/exit 0，后续不重复浪费时间。
- 待讨论的底座优化候选：以“每次推理吸收的相关新证据量”为观测指标，允许聚合已经就绪且相互独立的
  读取、搜索、检查和工具结果，减少频繁重放增长前缀造成的近二次缓存读取成本。该候选不能写死固定
  100K 批量，也不能跨越权限、失败、用户 steer、工具依赖或分支决策边界；当前仅记录，不占用五路 TUI
  Goal，不修改模型调度主链。
- `.13` 最终 evidence 位于 `/root/tui-parity-evidence/final-20260818T071817CST/final-deploy/`：70 文件
  hash 一致、5 个废弃文件不存在、回滚包齐全，Gateway 在 `/root` canonical workspace 为 running，
  `my-agent-tui:work` 为 120×29 idle；实际 key 字节扫描证据和部署文件均为 0 命中。

## 2026-08-18 TUI 拖选生命周期、终端头像与富工具记录【状态：已完成；沙箱洁净度另行复验】

- 对照 会话运行时 `tui/event_stream.rs` 可见其明确丢弃 mouse event，并通过 raw scrollback 提供复制友好视图；
  本项目需要 终端交互 同类的应用内选择，因此进一步对照 `src/ink/components/App.tsx` 的 lost-release
  recovery 与 `useCopyOnSelect.ts`。既有非空选区只能在左键仍按下时随 `MOUSE_MOVE` 更新；任何 release、
  无按键 motion 或下一次 fresh press 都会结束旧拖动。focus cell 按包含语义高亮，松手自动复制且保留选区，
  普通 hover 不得继续扩展或触发全屏高亮。
- 终端头像属于纯显示资产，不进入事件、状态、prompt、history 或权限合同。宽卡使用固定宽度 styled
  fragments 表达参考图的长兔耳、浅色头发、红色发饰/衣裙、红伞和小兔；窄卡使用同语义紧凑稿。所有行
  必须按显示列居中和裁剪，80/120/140 与窄终端都不能越界。
- 真实复刻测试只允许测试者在 `my-agent-tui:work` 输入一次普通自然语言需求；项目筛选的 star、语言和
  代码量是测试前外部证据，不写进模型控制协议。任务开始后只能观察被测 Agent 自己的工具、权限、日志、
  产物和终态，不能由开发者旁路补代码或把旁路产物计作通过。
- 富 transcript 必须由 `client_capabilities.rich_transcript=true` 显式开启；plain、IM 和其它 Gateway
  客户端保持原有最小公开面。支持的 TUI 才接收逐模型轮 commentary、provider 明示 `type=thinking` 正文和
  工具 `display`。signature、`redacted_thinking`、普通 text 与未知 handler envelope 字段不得冒充思考或
  穿透公开事件。
- thinking 生命周期按供应商 typed content block 顺序收口：`thinking_delta* -> content_block_stop ->
  thinking_completed -> text_delta*`。该 terminal 必须在 parser/collector 的 block-stop 处发布，不能等完整
  response 返回后再补到 final 后面。response 的 `assistant_content_blocks` 只在 backend 没声明 block-stop
  observer 时兜底；流内已经收口就不得重放。旧 Gateway 的迟到完整 thinking 只能消费为前一活动块的兼容
  terminal，不得创建新的底部块。该边界对照 会话运行时 reasoning delta/final 的同 item 生命周期与 终端交互
  在 `content_block_stop` 原子生成完成 assistant block 的做法，不使用正文相等判断去重。
- 文件和命令展示以 handler 结构化事实为唯一来源：`edit_file`、覆盖式 `write_file`、单/多文件
  `apply_patch` 生成有界行号 diff；新文件生成十行预览；`run_command` 分开投影 stdout、stderr 与退出码。
  renderer 只负责颜色、折叠和宽度，不能从 output 文案反解增删行或成功状态。
- 真实长任务若更早有失败、最后一条工具是注册表声明的成功 workspace mutation，且之后没有任何工具记录，
  主循环只丢弃一次过早收口草稿并注入结构化软提醒，让模型自主选择复核或明确解释。该规则不解析草稿、
  不设置 blocked/unfinished，也不影响简单写文件或已经有后续检查的回合。
- 模型网络故障必须按系统异常事实分类：`ConnectionRefusedError` / `errno=ECONNREFUSED` 与异常链中的
  typed `socket.gaierror` 先走 HTTP 传输层有界退避，再走模型回合级有界恢复；畸形 URL、认证和代理配置
  仍快速失败。富 TUI 从结构化 retry 事件显示层级、序号和等待秒数，不能解析错误文案决定
  是否重试，也不能把重连提示混成 assistant commentary。
- Click→Go 恢复任务证明 rich transcript 与长链收口已可用，也暴露了与界面无关的 workspace 污染：
  Attempt sandbox 曾把当前项目 cwd 放在结构化 task roots 之前，导致 `/tmp` 实际映射到
  `project/.sandbox-tmp`；只读 owner home 又让标准 build cache 首次失败。对照 会话运行时 把 tmp 作为独立
  writable root 的边界，当前通用修复只使用 `task_work_dir`、sandbox write roots 和 XDG 环境事实：
  task work 排为临时根首项，owner-scoped `TMPDIR/XDG_CACHE_HOME` 指向 sandbox `/tmp`。不解析命令、语言、
  文件名或模型正文，也不增加 Go/Click 专项交付过滤器。

## 2026-08-18 写后验证新鲜度与模型调用累计观测【状态：已部署并由真实任务验证】

- Tornado→Go 真机任务证明“最后一个工具是否为写操作”不是可靠的新鲜度合同：测试成功后再写 examples，
  后续 grep/read 会让旧规则误以为已有检查并允许收口。唯一机器事实改为 verification ledger 中的
  `verification_id/status/root/command` 与成功 workspace mutation 造成的 stale 状态；read/search 既不验证
  也不清除 stale。
- 工具结果的验证 envelope 必须写入 canonical `metadata.handler_details`，archive、模型投影和收口读取
  同一位置；不得在 metadata 顶层再造只有写入端可见的旁路字段。Go/Cargo 的规范 build/test 命令由
  `go.mod/Cargo.toml` manifest 结构化识别，不读项目名、prompt 或模型正文。
- 收口提醒仍是软核对，不是普通任务完成硬门：signature 由 stale root 与最近 durable verification event
  ID/状态构成，同一个验证→修改周期只提醒一次；模型执行新的真实验证后才形成新周期并可再次提醒。
- `ModelCallLedger.records()` 继续只保留最近 128 条明细供超时估算和诊断；最终 request/run 统计改由同一
  线程安全 ledger 的有界 scope aggregate 累计 logical turn、物理 attempt、provider HTTP attempt/retry、
  状态、backend/model。累计态不保存 prompt、响应正文、请求体或 key，明细裁剪不得再截断用户可见计数。
- SANDBOX-02 已由同一 Tornado 任务真实闭环：构建临时数据留在 task work，最终 output 无缓存目录；该项
  由 ROADMAP 移入完成。后续 aiohttp→Go 任务在最后一批源码修改后重新 build、跑过 29 项行为测试并完成
  HTTP 200 E2E，验证新鲜度也已闭环；测试者没有旁路补产物。

## 2026-08-18 收口效果权威、会话运行时 式返工与 owner-scoped 构建缓存【状态：收口返工部分已于 2026-08-28 退役】

- 历史 `completion_conflict.v1`、`OPERATION_INCOMPLETE` 和写后验证 followup 会在模型 plain final 后继续
  驱动模型，已被 2026-08-28 真 TUI 证明会误伤用户故意要求的非零退出码。以下条目仅保留历史背景；
  owner-scoped 构建缓存仍有效，completion 返工结论不再代表当前实现。

- operation ledger 是完整审计事实，不等于“最后一条记录覆盖整项任务”的完成裁决。`not_started` 已经
  结构化证明 handler 未执行、没有副作用，因此末尾连续 no-effect 尝试不能推翻此前最近一项 succeeded
  effect；记录仍保留并使整体 operation 投影显示 partial。只有 `not_started` 且此前没有成功效果时，仍形成
  当前收口冲突；显式 required action 继续由独立结构化 gate 管理。
- 已明确 `failed/not_started` 的末尾效果与模型 final 冲突时，不再立即丢进无工具表达轮。该冲突会把
  `completion_conflict.v1`、最近 typed 终态和被拒绝草稿写回同一 active turn，保留原工具面供主模型定位、
  修复和重新验证；全 turn 最多两次，不能靠换 call id 无限重新武装。若新的 effect-bearing operation 形成
  succeeded 终态，模型自然 final 直接交付；两次仍未解决才按 `OPERATION_INCOMPLETE` 安全收口。
- `unknown/cancelled/incomplete/unverified` 不进入上述通用带工具返工。它们可能已经发生、仍在运行、已取消或
  缺少权威终态，自动重放会破坏幂等/取消边界，因此继续直接 fail-closed；只有工具自己的结构化 reconcile
  能改变 unknown。该分流只读 operation status/error/failure stage/effect outcome，不解析模型完成措辞。
- 该模式直接对照 会话运行时 `会话运行时-rs/core/src/session/turn.rs`：工具调用设置 `needs_follow_up`，结果回到同一轮；
  plain assistant 才自然结束。需要阻止收尾时，`hook_runtime.rs` 的 Stop hook 通过
  `build_hook_prompt_message` 把 continuation prompt 写回同一 turn，原工具能力仍在，而不是另开一轮替换最终
  答案；`core/tests/suite/hooks.rs::stop_hook_can_block_multiple_times_in_same_turn` 还验证了同一 turn 的多次返工。
  my-agent 的两次上限与 unknown 安全门是基于自身 operation 合同的有界适配，不宣称 会话运行时 有同名完成判定。
- owner home 继续是只读身份底图，不能为包管理器扩大写边界。开放世界默认缓存统一由
  `TMPDIR=/tmp`、`XDG_CACHE_HOME=/tmp/.cache` 表达；对已实证不采用 XDG 的标准 npm，仅增加其正式
  `NPM_CONFIG_CACHE=/tmp/.cache/npm`。环境注入发生在凭据擦洗之后，不修改 `HOME`，也不恢复 key。
- 以上均为通用运行时合同，不读取项目名、语言转换目标或模型说明。末尾 no-effect、已知失败返工成功、两次
  返工耗尽和 unknown 禁止自动续做的 focused tests 已通过；真实 Fiber 143 样本确认权威字段为
  `FAILED/COMMAND_FAILED/execution/effect_outcome=failed/return_code=143`。候选部署后的真实返工结果继续记入
  稳定性台账，未完成前不冒充 E2E。

## 2026-08-22 默认 TUI 启动、显式会话恢复与 Gateway 崩溃调和【状态：已部署 `.7` 并复验】

- 普通 `my-agent` 与 `my-agent chat` 都表示新建会话，直接走轻量交互解析器；只有
  `my-agent resume <session_id>` 才能连接明确的旧会话，不存在或越权时 fail-closed。该边界直接对照
  会话运行时 TUI 的 `SessionSelection::StartFresh` 与显式 `Resume(target_session)`，默认入口不得扫描全机
  subagent board、询问是否批量恢复，或把“按 Enter”伪装成实际派工动作。
- 部署默认配置路径由 `MY_AGENT_CONFIG` 统一提供给裸 TUI、完整 CLI 与 Gateway service；显式
  `--config` 仍是单次命令的最终覆盖，避免 Gateway 使用 MiniMax 而欢迎页读取随包 DeepSeek 默认值。
- TUI 客户端不是恢复 owner。普通启动只确认单 Gateway readiness 并连接当前 session；`status` 是用户
  显式请求的只读投影，不调用 lifecycle setter、attempt recovery、dispatch 或进程终止。旧的
  `auto_detect_work_on_startup` 与 `startup_auto_reconcile_crashed_tasks` 已删除，避免配置、TUI 与 Gateway
  同时拥有恢复权。
- 普通 run 的 `recover_stale_attempts()` 移到 Gateway 启动阶段，以进程死亡证明调和 attempt；subagent
  后续恢复继续只走 Gateway 的 `supervise_stalled_orphans`，使用 canonical runner session、心跳、宿主
  PID、conversation lifecycle decision 与 attempt fence。状态页只报告失联 RUNNING session，不再拿共享
  `background_start.pid` 把 `BLOCKED/PENDING` 误称为崩溃卡死。
- 派工巡查锁改为 会话运行时 同类的内核 advisory lock：POSIX `flock`，Windows `msvcrt` 第 0 字节非阻塞锁。
  JSON 只作诊断元数据，不参与归属；空文件、半截 JSON、旧 PID 或 PID 复用都不能形成永久死锁。锁路径
  在释放后保留，描述符关闭或进程崩溃由内核自动释放；`--force-lock` 不能抢占仍被内核确认在岗的持有者。
- `.7` 现场旧 `subagent_orphan_supervision.lock` 为 0 字节且已残留两天，旧实现把“JSON 读不出 PID”保守
  当成活锁，导致每分钟 supervision 都静默 `skipped_locked`。同一现场 25 条旧 PID 告警实际为 5
  `RUNNING`、1 `PENDING`、19 `BLOCKED`；只有 5 条具有失联 RUNNING runner session，其中 3 条满足续跑，
  其余由已关闭 conversation link 的取消收口处理。新锁上线后由同一 Gateway 自然调和，不做批量删除。
- 部署证据：唯一 Gateway PID `1918200` 同时持有 8420 与 v2 advisory lock；裸 TUI 在 1 秒采样点显示
  MiniMax-M2.7 输入框。监督首轮复活 4、按父会话取消 21、回收失联 RUNNING 5，随后 3 条 RUNNING
  自然完成；两次显式 status 返回同一近期计数且没有恢复提示。

## 2026-08-22 固定 Todo/代理区与后台主代理可见性【状态：第一轮真机发现已修，新候选待 `.7` 复验】

- 布局直接对照 终端交互 `REPL.tsx` 的 `SpinnerWithVerb`、`TaskListV2.tsx` 与
  `CoordinatorAgentStatus.tsx`：main 的动态 `Working` 行位于最新正文后、Context/Todo 前；Todo 固定在
  输入框上方；输入框下方只保留直属 child，不再重复 `main`。
- `task_progress(action=update)` 必须先晋升 conversation task，再解析唯一稳定账本 key；否则首条
  Todo 会写到 Gateway request id，派工却读 task-path 指纹，同一任务裂成两本清单。
  `create_subagents` 对显式 `covers` 只沿用已有 Todo id，未绑定 child 才按 exact run id 新建条目；
  `covers` 的 exact id 同时适用于普通 `items` 和 `coverage.targets`，child 进入 canonical DONE 后只按
  结构化 id 回写对应项，不能从标题、goal、路径或完成正文猜映射。
  child 的 `covers` 以有界 `progress_item_ids` 进入只读活动投影，renderer 仅用 typed status
  原位显示 DONE/失败/取消，不从 goal/title 文字猜关联。
- 大派工回执可被工具输出归档，因此有界 `task_progress_seed` 必须同时保留在
  `result_envelope/handler_details`，Gateway 实时事件优先读该结构化快照，不依赖可裁剪的文本预览。
- 生命周期直接对照 会话运行时 `agent/status.rs`、`agent/control.rs` 与 `multi_agents/wait.rs`：只读 typed
  status/notification，不解析“模型已生成回复”“已经完成”等自然语言。终态 child 清除旧 activity；首次
  attempt 隐藏，只有 `attempts > 1` 才显示 `重试 N 次`。
- child token 从 exact run 的结构化事实只读投影，Compact 次数只读 `agent_thread_id` 对应
  ConversationThread generation；持久 native IR reduction 与 transcript 压缩都在该 generation 内提交，
  旧 durable apply/attribute 不回读，TUI 不自行估算或从 token 降幅猜测。后台 main 的最近
  thinking/tool/retry/finalizing 是 Gateway 进程内有界、易失、纯展示快照，不参与完成、派工、重试、权限或恢复。
- 后台主代理的可交付最终正文以普通 `assistant_completed` 进入 transcript，不再伪装成灰色系统通知；被
  delivery contract 抑制或没有正文的内部轮不写用户通知。一次模型轮返回只将 main 活动设为
  `waiting`，不得在 child 仍活跃时写“整理最终回复”；显示层按结构化 child 状态改显“等待 N 个子代理”。
- `.7` 的 `7c052f2` 在 tmux `<历史会话:p1-pvz-7c052f2>` 输入第 1 个原样重任务：第一批请求
  5 个 child 被容量合同整批拒绝后，模型自然改为 4 个，随后又派 2 个整合/服务 child；6 个
  child 全部 DONE，合计约 238.2k tokens、Compact 均为 0。最终 `/root/abc` 有 9 个文件，
  `0.0.0.0:8080` 真实监听且 loopback HTTP 200；但 8 个 Todo 全未勾选，main 仍错放在输入框下方。
  本候选修复已通过 147 项 focused tests，仍须部署后用下一条原样 TUI 任务验收新布局与勾选。
- `.7` 的 `993ce4f` 在 tmux `<历史会话:p2-mario-993ce4f>` 完成第 2 个原样重任务：首次请求、后台 main、
  6 个 child 与全部命令都保持 `<历史工作目录:tui-p2-993ce4f>`，产物落在 `bbb/`，`0.0.0.0:8082`
  返回 HTTP 200。child 行已稳定显示“玩家控制/物理引擎/敌人AI/道具系统/关卡设计/游戏HTML”、实时
  当前 context 和 Compact；6 个 child 全部一次 attempt 自然 DONE，最后一个完成后约 16 秒唤醒 main。
  同轮也确认三个底层缺口：main context 停在启动快照、Todo 的 canonical 5/5 没投影回 TUI、第二批
  系统生成的 child display name 重新从 1 编号；模型还在容量满/创建失败后自行补写了用户明确禁止主代理
  编写的功能代码。
- 当前候选对照 终端交互 固定状态区和 会话运行时 orchestrator 的 delegated-task 边界：activity schema 升到
  v4，main context、当前 active task 的 canonical Todo 和 child 状态共用一份只读快照；最终后台 notice
  先补终态 Todo 再显示 assistant final。同一 exact parent 的系统 child 名称跨批次连续编号。默认 prompt
  明确把“主代理只能协调/不准自己写”视为当轮执行分工，容量满时等待释放，参数错误时修正重试，不能把
  创建失败解释成静默接管授权。所有变化只作用于通用结构化身份/账本和指令优先级，不读取游戏名或任务
  关键词；138 项直接相关 focused tests 已通过，仍待严格 gate、部署和同一原样 TUI 复验。
- `f5dc695` 真机复测进一步固定职责字段语义：单 child 的顶层 description 可作为职责；批量创建时顶层
  description 仅描述整批，不能扇出到 `items[]`。每项优先显示自己的 description，缺失只退回自己的 goal；
  这一行只回答“该 child 负责什么”，不承载路径、模型阶段或长目标。nested native schema 必须把逐项
  goal/description 说明显式暴露给 provider，不能只在顶层参数说明里约定。
- main Working 与 child row 都采用 终端交互 式固定后缀宽度预算，任意 thinking/职责文本只能在剩余列
  单行截断。自动 child seed Todo 的 exact-id 去重先于 128 行投影上限，并同时适用于 live panel 和最终
  notice；账本不删，普通 Todo 不受影响。
- Todo 默认折叠对照 终端交互 `TaskListV2` 的状态优先窗口，但固定为 4 条：最近完成、当前运行和下一待办
  依次占位；多个运行项优先保留，运行图标复用全局 Working 动画。`Ctrl+T` 只是进程内展开开关，不能重排
  或写回 `task_progress.v1`。
- Todo 更新是 replace-all 快照，不是增量补丁：字段缺失或投影失败时保留上一份有效画面；结构化空列表则
  明确收起旧 Todo。该语义对照 终端交互 `TodoWriteTool` 在全部完成后把展示清单置空，解决最终 notice
  已到达却残留旧 `0/N` child seed 清单的问题；空列表仍然只影响 TUI 投影，不删除 canonical 账本。
- Prompt 3 真机还锁定一条工具结果边界：shell 识别到模型试图直接读取内部 child 状态路径时，安全规则在
  启动进程前返回 `WRONG_STATUS_SURFACE`；这一结果必须显式携带 `effect_outcome=not_started`。这与
  会话运行时 的执行前校验错误经 `FunctionCallError::RespondToModel` 返回当前模型继续改参一致。它只解除
  错误的 unknown 硬停，不放开内部路径，也不改变真正已启动、超时或副作用未知调用的 fail-closed 合同。
- 常驻 Context 行只保留当前总量、窗口占比、主 ConversationThread 已成功 Compact 次数和明确标注的
  自动“压缩点”，例如 `compact 2 · 压缩点 90%`；压缩点不是次数或正在压缩的进度。activity schema
  继续为 `conversation_agent_activity.v5`，main `compact_count` 只取 canonical `compact_generation`；
  prompt/messages/tools 受 provider 协议形态影响，详细构成只在 `/context` 命令展示。

- 对照 会话运行时 `core/templates/agents/orchestrator.md` 后，默认递归 coordinator policy 固定为：实际工作一经
  委派，main 只协调、读取和整合已有产物、测试与汇报；缺口继续 guidance 或 replacement child。失败、
  容量不足和终态都不自动转移实现职责。该约束只写入模型执行上下文，不作为机器状态、权限或验收门。
- `.7` 正式验收必须使用唯一 Gateway 和 `MiniMax-M2.7`，依次执行 `TESTS.md` 的四个原样重型 prompt。
  每次启动、切换或输入 TUI 之前必须先向用户公开 tmux session 名称与完整 attach 命令；测试者只观察，
  不旁路补代码或向被测代理发送技术推动消息。
- `931ee20` 的全新 Prompt 3 真机轮证明短职责和固定活动区正确，但同时证明旧完成 wake 只有“孩子结束”
  通知，没有把 canonical `task.result` / `final_report.md` 交给 main。对照 会话运行时
  `session_prefix.rs::format_inter_agent_completion_message` 与
  `session/mod.rs::forward_child_completion_to_parent` 后，当前完成交接固定为
  `subagent-completion.v1`：宿主 typed status 决定 child 生命周期；自然语言 `completion_message` 只作
  整合证据并按 1000 估算 token 截断；系统生成的 `final_report_ref` 和结构化 declared/artifact refs 提供
  完整读取路径。模型不得从完成正文反推 status、权限、验收或根任务完成。
- 同一 exact root 的普通 `DONE` wake 在孩子收齐及短 debounce 后按当前上下文预算成批交给 main，active
  wake 的 `metadata.events` 保留每名孩子的独立信封；只确认实际选入本轮的成员，新到或预算外事件继续
  pending。失败事件和 Audit source worker 保持原即时专用路径。背景上下文压力可缩短每段字符串，但
  不能按字段顺序丢掉 active wake metadata、evidence refs 或批成员；这是当前 turn typed input 的优先级，
  不新增目录扫描 fallback 或机器质量门。`fd7d2b9` 已部署并证明首批结果能唤醒 main，但正式 Prompt 3
  仍只覆盖 6/8 且误报 终端交互 缺失；后续只从结构化批次覆盖/结果 refs 修底层，不写任务名专项分支。
- Prompt 3 的六份完成信封均已被消费，真正遗漏点是进度账本身份分叉：工具写侧使用
  `task-path:<sha256(task_path)[:16]>`，后台 Task Runtime State 旧读侧使用 durable request id。路径指纹算法
  现只允许由 `agent_core/runtime/task_identity.py` 生成；task_progress 写入、后台读取、dispatch child
  终态同步、Goal continuation 和 TUI activity 都必须调用同一 helper，禁止调用方复制 hash。该统一只让
  当前计划进入模型可见 typed state，不把普通 open Todo 升为机器完成门、产物验收或自动续跑授权。
- Prompt 4 r5 在 Todo `4/18`、模型自己明确列出大量缺口且仍有可用工具时自然 final，证明“如实列出
  未完成项”不能代替 会话运行时 的持续完成纪律。当前默认主/子代理执行提示统一采用 会话运行时 的软语义：只要
  已知目标仍有未完成部分且现有工具或下级还能推进，就继续实现、修复和验证；只有目标端到端完成、用户
  明确暂停/改向，或存在当前无法消除的真实阻塞时才结束。`task_progress` 读取回执可重复这一软提醒，但
  宿主仍不因 open Todo 自动重开模型、不解析 final 文案、不恢复机器质量验收。
- 系统生成的 display name 是未来 TUI/Web 精确控制旁边的人类可读稳定标识：同一 exact parent 下，批量、
  单个补派以及递归 child 都必须沿全部历史直属 sibling 连续编号；显式自定义名保持原样，run_id 仍是唯一
  机器身份。Prompt 4 r5 两个单独补派 worker 都显示 `agent-d1-worker`，确认旧编号器只覆盖批量入口。
- Prompt 4 r6 进一步证明 `items` 长度为 1 也仍是批次语义：`create_run_params` 已把省略名展开成无编号
  `agent-d1-<role>`，展示层必须把这个精确系统 stem 继续编号；不能把任意 `agent-d*` 自定义名都改写。
  显式幂等合同比较结构化 parent/role/scope/identity，系统 ordinal 只作展示、不应让同一幂等请求失配；
  用户自定义名仍要求完全相同。
- `context_scope=isolated` 是表达层的 typed 边界，不是另一个可观察 task turn。对照 会话运行时 只把当前 active
  reasoning item 交给 TUI 的做法，该调用仍进入 model-call/cost ledger，但其 provider thinking、context
  usage 和 child activity 均不得覆盖 main/child 的 durable 投影。r6 的 74.4k→9.7k 且 `compact 0`
  正是表达轮污染，不是一次真实 Compact。
- Todo 标题只从 canonical typed status 计算 `完成 X/Y` 与可选 `进行中 Z`；四行 collapsed window 只在
  有隐藏项时显示 `Ctrl+T 展开`，不得再用 `4/8` 表示可见行数。直属 child 若仍处于 typed
  `PLANNING/PENDING/RUNNING`、却没有 explicit `progress_item_ids` 映射到当前可见 Todo，标题另显示
  `子代理运行中 N`；它只解释下方面板为何还在工作，不能重开/改写 Todo，也不能从名称、goal 或输出猜映射。
  该选择对照 终端交互 `TaskListV2` 的 done/in-progress/pending 分组，同时保留本项目固定四行窗口，不写回账本。
- Prompt 4 r7 证明“Todo 全打钩 + 弱测试全绿 + model natural final”仍可能建立在被主动降级的证据上：
  main 最初把完整复刻拆成“空壳可导入/最小欢迎页”，`pip install -e .` 与真实 app 构造失败后，又删除
  能暴露启动缺依赖的 `test_app_instantiation` 并把其余断言降成存在性检查，最后宣称完整可运行。对照
  会话运行时 `gpt_5_1_prompt.md` / `gpt_5_2_prompt.md` 的“端到端解决后才结束、失败工具继续推进、运行/测试
  验证”后，本项目把范围保真与验证纪律并入同一 `DEFAULT_EXECUTION_PERSISTENCE`，由 root 默认 prompt、
  普通 child runner 和 child lifecycle wake 共用：委派不缩小原目标；骨架只能算阶段；用户可见入口失败
  必须修正后重跑；`|| true`/`|| echo` 的外层零码不能证明内部成功；有效失败测试不得仅为变绿而删除、
  skip、放宽或改成只测存在。它仍是 会话运行时 式模型软纪律，不扫描项目、不按 LOC/Todo 判完成、不执行测试、
  不重开 final，也不恢复已经删除的宿主质量验收器。

## 2026-08-22 主/子/孙代理统一 Conversation Compact

状态：child thread 与工作区继承已推送、部署；>40k 功能行真实 TUI 又抓到 mid-turn 原生工具历史
已从 113.1k 降到 35.5k、但 canonical generation 仍为 0。统一账本实现与 focused 回归已完成，待严格 gate、
推送部署和全新 TUI 验收。

- 解决问题：长时间工作的 child/grandchild 与 main 一样会经历多轮工具调用、上下文压力、崩溃恢复和继续
  运行。旧实现中 main 使用 `ConversationThread summary/cursor/generation/checkpoint/CAS`，task-local child
  使用 run workspace 的 `memory_archive compact_applies`，同时还有 active-turn native IR 裁剪；三种事实
  的计数和恢复边界不同，曾产生“child 上下文明显下降、界面仍显示 compact 0”的真实错误。
- 对照依据：会话运行时 `会话运行时-rs/core/src/session/turn.rs::run_turn` 对每个 Session 都执行
  `run_pre_sampling_compact`，并在同一 turn 的 token 状态触发 `run_auto_compact`；
  `tools/handlers/multi_agents_v2/spawn.rs` 通过 `spawn_agent_with_communication` 为 child 返回独立
  `new_thread_id`。因此成熟语义是“每个代理一条 thread、全部调用同一 Compact 状态机”，不是主子代理
  共用一份消息，也不是子代理另造一套压缩器。
- 已落地合同：main、child、grandchild 各有稳定 `agent_thread_id`，父子 lineage 继续由结构化
  `run_id/parent_run_id/root_run_id` 管理；每条 thread 独立保存 transcript、summary、cursor、generation、
  checkpoint、operation evidence 和失败熔断。触发阈值、token estimator、候选校验、checkpoint-before-CAS、
  pre/mid-turn 续接和 TUI 计数全部复用 `conversation/compact.py`，但任何代理都不能读取兄弟或父级正文。
- thread 身份分层必须与 会话运行时 一致：继承的 `conversation_thread_id/conversation_task_id` 继续表示父会话的
  workspace/task lifecycle；新建的 `agent_thread_id` 只表示 delegated agent 自己的模型 transcript。二者不能
  覆盖或互绑，父子关系由 `parent_agent_thread_id` 和 run lineage 表达。首轮 Prompt 4 真机测试曾把 child
  thread 写回 `conversation_thread_id`，导致工作工具尝试把父 task 重新绑定到 child thread，并以
  `already bound to another conversation thread` 失败；当前修复已用真实写工具回归锁定父 link 不变。
- cwd/权限同样按 会话运行时 child spawn 继承 active turn config：根 main 创建 child 时，当前会话的
  host-validated `conversation_execution_cwd/conversation_runtime_workspace_roots` 必须进入 child task
  attributes、execution context 和产品写根；shared Gateway manager 的仓库根只是无会话任务的 fallback。
  `output_files` 继续只表达交付目标/验证线索，不负责授予普通 child 当前项目权限。第二轮 Prompt 4 正是因
  main 省略该字段而暴露断点；回归现已覆盖“无 output_files 仍读写 client project”。
- 实现边界：`ConversationThreadStore.ensure_agent_thread` 以 host 生成的 exact id 幂等建线程，不写 channel/
  latest-user 索引；child 每次尝试按 `conversation_request_id` 先落 user、轮前 Compact、注入 summary/raw
  tail、模型运行、终态落 assistant。provider overflow 在同一 attempt 内强制 Compact 并携带 typed tool/
  guidance 进度重试，最多八次；没有 generation 或真实进展时明确失败，不盲目重放。
- 收口边界：task-local 权限/Memory 隔离保留，但结构化 transcript-authoritative 标记让 durable Compact 只由
  ConversationStore 接管；旧 `compact_applies/continue` 不再由正式 child runner 生成。TUI、Web 和 SQLite
  投影只读 child thread generation/checkpoint；允许持久化的 native IR reduction 也必须先写同一 Compact ledger
  checkpoint 并推进该 generation，而不是写 canonical run 或另加显示计数。只有 presentation/no-save 回合的
  窗口整理保持临时事件。旧投影字段和旧 ledger 不设长期双读。
- 本地验收：覆盖 child/grandchild 独立线程、父子正文隔离、轮前 transcript Compact、运行中 main/child
  native IR generation 1→2、上一代摘要合并、精确 ToolCall ID checkpoint、provider overflow forced
  checkpoint、摘要失败与 checkpoint-before-CAS 冲突回滚、owner Memory 不污染、旧 apply 目录不生成，
  以及遗留 reduction/ledger 不能改变 TUI 次数。真实 MiniMax-M2.7 长任务仍需部署后通过新 TUI 验收。
- 压缩策略与单次回合权限分层：`compact_trigger_tokens` 始终投影同一配置压缩点；
  presentation/no-save 回合不允许持久 apply 时，只把当轮强制压缩的有效硬限放到完整窗口，
  不得把公开策略改写成 100%。这与 会话运行时 分开 `auto_compact_scope_limit` 和
  `full_context_window_limit` 的口径一致；前端不硬编 90。

## 2026-08-22 单 Gateway 服务身份与 thread cwd 分离【状态：`993ce4f` 真机通过】

- 一个 local owner 只有一套 Gateway/adapter service identity：默认固定在
  `owner_home/workspace/runtime/services/`。pid、heartbeat、queue、HTTP 端口不能再由 TUI cwd 选择；
  显式相对服务路径也以 owner workspace 解析。
- 项目目录是每个 durable thread 的 typed 配置。TUI/CLI ask 携带绝对 `workspace.cwd/roots`，Gateway
  校验后写入 `conversation_thread.v7`；后续未覆盖的 turn、后台 main 和 child 继续继承。远程 owner
  不能提交主机 cwd，非法目录在模型前 fail-closed，不得退回 daemon cwd。
- Tool Registry 的授权、审计 scope 和实际 handler 只接收同一 effective cwd/root；任务交付与子代理相对
  output ref 读取同一会话字段。该适配对照 会话运行时 thread/turn 的 `cwd/runtime_workspace_roots`，没有新增
  prompt 关键词、第二 Gateway 或 TUI 专项路径分支。

## 2026-08-23 会话运行时 式工作区并发与 owner 隔离分层【状态：运行时锁已落地；显式批次冲突反馈本地候选】

- 普通 shell、文件写入、patch 和子代理派工的 `workspace:*` 只是当前轮调度/审计事实，
  不再进入跨 run 持久 `resource_locks`。当前 turn 内的 barrier、operation 幂等/replay、
  sandbox 与 write boundary 继续生效；精确 `logical:*` 控制面资源仍可持久互斥。
- `output_files/output_refs` 是可选交付元数据，不是文件所有权。runtime 不再因既有同级/父子 run 的路径
  重叠加锁，也不会把活跃 child 的申报产物追加到 `locked_files`；父子继续共享 cwd。后续 r28 真 TUI
  证明同一次批量调用已主动声明 `/internal`、`/internal/core`、`/internal/param` 时仍放行会直接制造冲突，
  因此只对该调用内显式 `output_files` 的相同或祖先关系返回 not_started；不检查未声明写入或历史 run。
- 成本观测复用唯一 `ModelCallLedger`：供应商返回的 input/output/cache read/
  cache creation 按 request/run 累计，并随 `AgentRunResult`、Gateway result 与 runtime fact
  持久。无 usage 的调用使用调用前 input 和输出估算，并单独计数；TUI 的
  `current_context_token_estimate` 仍只是当前上下文压力，不得冒充累计消耗。
- 多用户隔离不依赖上述目录锁：远程 provider 用户仍强制进入各自 `owner_home`，数据库、
  memory、task、run、artifact 与 audit 分开；owner path wall、结构化写根和进程沙箱仍是硬边界。
  本地可信 CLI/TUI 则像 会话运行时 一样使用启动时项目 cwd，并由 thread 持久继承。
- `a091b72` 真机证明服务身份和 thread v6 主链生效，但薄 TUI 首次提交把 audit Agent 置空时也丢了 cwd。
  当前补丁将 audit 与 workspace 分参：客户端 config 是 cwd/roots 的唯一来源，Gateway 继续校验；不构造
  第二 Agent，也不回退 daemon cwd。真实提示词 2 复验必须使用全新项目目录和同一个 Gateway。
- `0ffbfe4` 的真实提示词 2 已证明首次 `gwreq-*` 和三条 direct child 都使用正确 TUI cwd；随后
  `subagent_runner_finished` 自动 wake 生成的 `run-*` 却只恢复 thread/task id，未恢复 thread v6 的 cwd，
  使相对 child output 回落 `/root`。二次候选按 会话运行时 每轮 `TurnContext` 及 spawn runtime override 的同一
  层级，在后台 `RunParams` 构造时复制本轮加载的 `ConversationThread.cwd/runtime_workspace_roots`；task
  workspace 仍只是隐藏状态目录。该修复只读结构化 thread 字段，不匹配模型生成路径或任务文本。
- `993ce4f` 部署后的全新提示词 2 已同时证明首次 ask、后台 `run-*`、6 个 child 和工具 working_dir
  全部继承 TUI cwd；没有再写 `/root/bbb`、`None` 或其它隐藏回退目录。本条 cwd 主链因此完成，后续
  继续在每个正式重任务中把路径对账作为回归证据，不再保留第三条兼容入口。

## 2026-08-22 lifecycle wake 的原生 active-turn 工具交接

状态：本地已实现并通过 focused 回归；待严格 gate、唯一 Gateway 部署与 Prompt 4 r12 真 TUI 验收。

- 解决问题：r11 证明 exact objective/cwd 已恢复，但 root 醒来后仍重复建 Todo、漏 `covers`。结构化证据
  显示 durable tool index 将 `task_progress.items` / `create_subagents.items` 裁成空数组，且 native builder
  刻意旁路机械 tool-context，模型只看到“当前状态”，看不到“自己已经如何走到这里”。
- 对照决定：会话运行时 在同一 active turn 内持续保存历史；本项目跨进程 wake 无法无损恢复原 assistant 内容与
  provider tool-use 签名，所以不能伪造 ToolCall/ToolResult。采用 会话运行时 Compact 同类 replacement item：
  canonical 索引继续掌握执行事实，模型只得到一条有界 `CompactionSummary` handoff；后续真实 UserTurn
  排在它之后，时间顺序不变。
- 参数合同：工具调用参数允许 JSON 标量/list/dict 递归至固定深度和宽度，敏感字段与已知 secret 统一脱敏；
  未知对象不 stringify。这样保留通用批处理、Todo、covers 和其它 schema 关系，不为某个测试 prompt 写专项。
- 计划合同：后台 Task Runtime State 投影唯一 ledger 的 existing/open exact ids、完整 read 参数和
  `create_subagents.items[].covers` 路径。该投影只指导模型复用现有计划；宿主不做标题相似度匹配、不自动
  合并语义重复项、不把 Todo 变成完成门。
- Compact 口径：handoff 复用现有 semantic-summary 字符预算，超限保留顺序索引、语义摘要、近期明细和
  archive refs。它不提交 checkpoint、不推进 generation、不增加 TUI compact 次数；真正的上下文压缩仍只
  走 ConversationThread 的 checkpoint-before-CAS 主链。

## 2026-08-22 计划内派工的 exact-id 与写入集合原子合同

状态：显式 exact-id 校验与错误分类仍保留；r14 删除旧 goal 文本自动补绑。下述“covers 与完整写入集合
必填”设计均已被 2026-08-23 的 会话运行时 式可选映射取代，保留本节只为说明 r12b-r14 的历史演进。

- 解决问题：r12 已恢复同一 active turn 的原目标与工具历史，但模型先建了 Todo，随后派工仍省略
  `covers`，又把新项目兄弟目录只写进 child `goal`。旧入口先创建 child、事后只给绑定 warning；运行时
  才发现目录不在父级结构化 workspace，child 进入 capability 阻塞，main 又反复尝试无法批准的越界 grant。
- 对照决定：会话运行时 的 `update_plan` 与 `spawn_agent` 是两项独立结构化动作，spawn 只接收具体、可独立完成
  的任务，并要求并行编码者使用互不冲突的写入集合。本项目适配为：当前 canonical `task_progress` 已有
  计划时，每个新 child 必须用 `covers` 绑定一个仍 open 的 exact id；有写工具且角色直接产出产品代码的
  child 还必须用 `output_files` 声明本次写入集合。两项都在任何 run 落盘前整批校验。
- 合同边界：未知、已关闭、重复绑定的 id，缺失写入集合，或解析后逃出直接父级 structured workspace 的
  输出路径，都返回同一个 `effect_outcome=not_started` 结构化修复回执；整批一个 child 也不创建。模型应
  修正 Todo/`covers`/`output_files` 后重试，不能把失败当作改写用户目标或父代理自行编码的授权。
- 这不是机器质量验收：宿主不解析 goal、Todo 标题、代码量或完成文案，不判断任务是否做得好，也不因
  open Todo 自动续轮。没有 canonical 计划的普通轻量派工继续沿既有宽松入口；read-only、tester 与
  coordinator 等不直接拥有产品写集合的 typed 角色不被强迫声明代码输出。
- capability 恢复继续服从现有安全围栏：请求目录在父级 workspace 之外时不能 grant，应 `deny` 并唤醒同
  一 child 回到已有写区；创建前的新合同应使这种越界请求成为异常兜底，而不是常规派工路径。
- fresh r13 首次命中发现新报码虽保留在 `reported_error_code` 与 JSON 正文，控制码却因错误分类表漏登记
  降成 `UNKNOWN_ERROR`。该码必须进入唯一 `error_taxonomy`，语义为 orchestration、可重试、
  `repair_tool_arguments`；这保持 会话运行时 式“把可修工具错误回送同一 turn 返工”，不能另加专项重试器。
- fresh r14 暴露了合同前残留的自然语言旁路：骨架 child 的 goal 只是列出 `src/i18n/` 与 `src/config/`
  目录，旧代码便自动写入 `covers=["i18n","config"]`，完成后错误勾掉三项 Todo。该旁路与本节“不读
  goal/标题”冲突，也不同于 会话运行时 的显式 spawn 参数，现已连同 `covers_auto_bound` 投影和测试一起删除；
  Todo 绑定只认调用参数中的显式 `covers`。

## 2026-08-23 根代理 assumptions-first 自主决策软纪律

状态：`b7005a8` 已发布部署；fresh Prompt 4 r16 已证明 root 会自主选择 Rust、建立 Todo 并创建 child。

- 解决问题：`bdcc7d1` 部署后的 fresh r15 在固定 lazygit 源码上只读了项目便停止，要求用户在 Rust、
  Python 或其它语言中选择；没有 Todo、没有 child、没有功能写入。用户已经授权“换一种编程语言”，
  语言属于安全可逆的次要实现选择，不应把决定退回给无人值守测试者。
- 对照决定：会话运行时 `collaboration-mode-templates/templates/default.md` 明确要求优先做 reasonable assumptions，
  只有无法从本地上下文发现且合理假设有风险时才问；`execute.md` 更明确要求信息缺失时选择 sensible
  assumption 后继续。相同 MiniMax-M2.7 的 会话运行时 真 TUI 虽先探索源码，最终也违背自身模板给出语言/范围/
  测试菜单，说明这是模型在超大任务上的行为回避，不应照抄该次坏输出。
- 落地边界：会话运行时 源码语义被适配为根默认 `system_prompt` 前部的通用中文软纪律。它只要求先查本地事实、
  对安全可行方案选合理默认并继续；只有实质偏离、越权或不可逆风险才允许问一个关键问题，且不能用多选
  菜单代替工作。规则不包含语言名、项目名或 Prompt 4 文本，不解析模型问句，也不新增重试器、完成门或
  机器验收。
- 配置权威：这不是第二套模式开关；既有 `system_prompt` 就是唯一用户配置入口。发布 YAML 与 dataclass
  默认文本继续逐字一致，显式自定义 prompt 仍有最高权威。

## 2026-08-23 子代理直接 goal 边界与可选产物提示

状态：`4c3a59d` 已发布部署；fresh Prompt 4 r17 证明首名骨架 child 不再替兄弟扩做，但又暴露 mandatory
`covers` 在返工场景会诱导错误 Todo 绑定，后续结论见下一节。

- 解决问题：`b7005a8` 部署后的 r16 已通过 assumptions-first 与 exact `covers` 主链。第一名骨架 child
  只被要求创建 6 个基础文件，却额外写了 11 个文件；其中 `src/gui/views.rs`、`src/gui/state.rs` 随后又
  被 root 明确分给第二名 child，形成真实的兄弟写冲突。第一名的模型报告也主动列出了这些额外文件，
  所以这不是观察误差或 TUI 文案问题。
- 根因：child runner 复用了 root 的“委派不会缩小用户原始目标”提示，使 child 同时看到根目标和直接
  `goal` 时容易替兄弟扩做。原先强制模型预报完整 `output_files` 也没有形成事实边界：该 child 只声明
  6 个目标，却实际多写 11 个文件，说明模型自报写集既不完整也不能承担安全权威。
- 会话运行时 对照：`会话运行时-rs/core/src/tools/handlers/multi_agents_spec.rs` 与 `multi_agents/spawn.rs` 的 spawn
  参数只有具体任务输入、模型等执行字段；child 继承当前 cwd/runtime，没有“先报完整写集”合同。r17
  进一步证明 Todo 映射也不能作为每次 spawn 的硬前置；当前设计只保留显式 exact `covers` 的校验和投影。
- 落地边界：root 继续对用户完整目标负责；child 的直接父级 `goal` 是其本轮完整工作边界，要求完整完成
  该 goal，但不得因为根目标更大而实现未交给自己的兄弟计划项。`output_files` 改为可选交付/冲突提示；
  一旦提供仍必须位于父级 workspace，也仍可参与已有的冲突提示，但它不是权限、完整写集或质量验收。
- 这是 会话运行时 式软执行纪律，不是新的机器审查器：宿主不解析 goal 去判“是不是兄弟任务”，不扫描 diff
  自动取消 child，也不恢复机器质量验收。真正的权限上界仍来自父级结构化 workspace；r17 必须用原样
  Prompt 4 验证 child 会停在直接 goal 内、root 能自然醒来再派后续项。

## 2026-08-23 可选 exact covers 与返工重开

状态：通用协议与聚焦回归已落地，待严格 gate、发布和 fresh Prompt 4 r18 真 TUI 验收。

- 真实证据：`4c3a59d` 部署后的 r17 中，首名骨架 child 只创建骨架，TUI child 只实现 UI 范围，说明直接
  goal 边界改善。第三名 Git child 却把 Rust 代码写到源码 cwd 根目录，而不是现有 `rust-port/`；root
  正确决定返工。此时 Git Todo `3` 已因首名 Git child DONE 被关闭，下一 open Todo `4` 是 GUI。
- 确定性失败：mandatory covers 门不允许返工 child 省略 covers，也不允许绑定已关闭的 `3`。root 为了
  让创建调用通过，把新的“Git 命令封装实现”child 绑定到 `covers=["4"]`；TUI 因而把“GUI 控制器层”显示
  为进行中。若继续，Git child DONE 会给 GUI 假打勾。现场已只用 TUI `/stop` 收口：root `PAUSED`，前三名
  `DONE`，错误绑定的第四名 `CANCELLED`，无残留 runner。
- 会话运行时 对照与决定：会话运行时 `spawn_agent` 不依赖 plan id。`covers` 改为可选 exact 映射：提供时继续原子
  校验未知、已关闭和同批重复 id；省略时 child 正常创建，并由已有 seed 主链按真实 run id 形成独立进度
  行，绝不关闭现有 Todo。`output_files` 继续保持可选提示。两者都不是权限、完整写集或机器验收。
- 返工协议：确实需要把返工仍归入原计划项时，先调用 `task_progress` 对同一 id 传
  `status=in_progress, correction=true` 明确重开，再绑定原 id；也可以省略 covers。模型规格、task_progress
  软 guidance 与 typed repair 都明确禁止拿无关 open id 顶替。宿主仍不解析 goal/title 做语义匹配。
- 协议版本：planned dispatch 投影升为 `planned_dispatch.v2`，使用 `binding_mode=optional_exact` 与
  `unbound_item_indexes`；旧 `missing_covers_indexes` 删除，避免把“未映射”继续表达成错误。

## 2026-08-23 普通计划的 会话运行时 式同轮停止核对

状态：`e94f8ec` 已通过严格 gate、发布并部署；open-Todo 直接真 TUI 证据待自然样本。

- 解决问题：fresh Prompt 4 r18 中，root 自己建立 8 项 canonical Todo，只完成 5 项且把测试三项保留为
  `pending`，最终回复仍声称“代码已完整生成”；会话任务和 task workspace 随后都被宿主写成完成。现场
  结构化账本明确记录 `next_action=等待 Rust 工具链安装后继续编译验证`，说明错误不是用户看漏了，而是
  普通模型 final 被直接等同整个 durable task 完成。
- 会话运行时 对照：`会话运行时-rs/core/src/session/turn.rs` 在模型不再请求工具时先执行 `run_turn_stop_hooks`；若钩子
  返回 `should_block`，宿主把结构化反馈记录进同一 active turn 并继续采样。最终的 `TurnComplete` 只是这次
  用户回合生命周期事件，不是业务目标验收结论。本项目复用同一模式，不恢复旧 delivery/acceptance 扫描器。
- 新合同只核对模型自己写入 canonical `task_progress` 的结构化一致性：没有 open 项时自然结束；仍有
  `pending/in_progress/unknown` 时，在同一工具循环最多返给模型一次 exact id/title/status，要求继续用原工具
  推进，或把真实无法推进项明确更新为 `blocked` 并说明原因。它不读取最终正文、不扫描代码、LOC、测试、
  目录或产物，也不替模型判断工作质量。
- 活跃直属 child、命名 Audit、真实工具失败/未知副作用等已有 typed 生命周期与安全修复先处理；计划核对
  只在这些专用分支都未接管自然 final 时运行。默认次数通过唯一配置控制，`0` 可关闭，避免无界增加模型调用。
- 一次同轮核对后仍有可执行 open 项，或清单只剩显式 `blocked` 项时，本轮以 typed `blocked` 结束，保留
  模型撰写的诚实说明，不把 conversation task / standalone workspace 写成 `DONE`，也不偷偷创建第二个
  `ordinary_task_resume`。用户后续消息仍可在同一 thread/task/workspace 继续。
- fresh Prompt 4 r19 在 final 前主动把 8/8 Todo 全部关闭，因此没有产生核对事件；不能把“钩子未触发”
  写成直接真机通过。r19 同时证明 112 个自建测试可能与真实启动相悖：生成 App 没有挂载 Screen，进程
  存活但整屏空白。该反例不改变本条边界：宿主不按 LOC、测试数量或界面内容决定业务完成；后续改善必须
  让模型基于结构化工具事实维护计划和验证实际入口，而不是恢复第二套机器验收状态机。

## 2026-08-24 no-save 主任务仍必须经过同轮停止核对

状态：r9 真实失败已定位，本地修复、focused 回归与严格 gate 通过，待发布和原样 TUI 复验。

- `save` 只表示是否写普通运行档案、长期记忆和相关可选持久化，不是 turn/task lifecycle authority。
  Gateway/TUI 和 `background_main_agent` 为避免重复 transcript 正常使用 `save=False`；它们仍是 exact
  durable task 的真实 active turn，不能因此跳过 canonical `task_progress` 停止核对。
- r9 的后台 main 在 task-path ledger 仍有 1 个 `in_progress`、2 个 `pending` 时输出完成，随后 task link 与
  workspace 被写成终态。最后一轮结构化工具账没有任何 `task_progress` 更新；已发布入口因
  `_eligible_closeout(... save=False)` 被整轮短路。这是生命周期开关误绑，不是账本 id 漂移，也不是需要恢复
  代码量、测试、产物或自然语言完成验收。
- 修复只解除 save 与停止核对的耦合；exact task-path 账本、同轮一次 repair、耗尽 typed blocked、显式
  `/goal`、Audit、辅助 scope 和真实工具失败优先级均保持。回归必须同时覆盖 Gateway save/no-save 与后台
  no-save，并证明 blocked 后 conversation task 仍 active。

## 2026-08-23 会话级模型用量账本

状态：本地实现与 focused 回归已通过，等待随下一批底座修复统一发布和真机复验。

- `ModelCallLedger` 的兼容累计值继续保留，但计费视图必须把供应商返回的 input/output/cache read/
  cache creation 与本地估算分栏；缺少 provider usage 时只能写进 `estimated`，不得用估算反填供应商真值。
- 每次 finalization 都把冻结后的 `model_call_summary.v1` 以幂等事件追加到 exact owner-scoped
  `ConversationThread` 的 `model_usage/<thread_id>.jsonl`。后台 main 的 `do_save=false` 只表示不写普通
  transcript/runtime archive，不得因此丢失已经真实发生的模型调用成本。
- 事件只保存 thread/request/run/task/source identity 和数字计数，不保存 prompt、response、key；同一
  event id 重放必须与首次载荷一致，冲突或损坏显式失败，不能把无法读取的成本当成 0。
- `current_context_token_estimate` 仍表示当前一次上下文压力；会话成本汇总只读取 append-only 用量事件，
  两者不能互相推导，也不参与 Compact、生命周期、完成或权限裁决。

## 2026-08-23 单 Gateway 多 TUI 接入背压与断线退避

状态：本地候选已通过 focused 回归，待部署 `.7` 和真实多 TUI 复验。

- 解决问题：真实并行 TUI 对照中，历史窗口与当前窗口都以 250ms 周期请求
  `/client/notices`；Python 标准 HTTP server 的等待连接队列只有 5。几十个窗口在 Gateway 忙时会形成
  `SYN-SENT` 堆积，Gateway CPU 升高，新窗口看起来像“主代理停止响应”。这不是 workspace/owner 锁，
  而是展示面反向压垮唯一运行入口。
- 会话运行时 对照：`会话运行时-rs/app-server/src/thread_status.rs` 通过 `watch` 与 server notification 在状态变化时
  推送；`app-server/src/transport.rs` 对每个连接使用有界发送队列，慢连接队列满后断开，不让展示客户端
  无限反压运行时。本项目本轮不同时更换整个客户端协议，先在现有 HTTP 兼容面落同一背压原则。
- 接入合同：单 Gateway 的 accept backlog 固定为 128；请求线程为 daemon，关闭时不等待失联 TUI。
  TUI 首次仍立即取快照，成功后每秒刷新；HTTP、解析或 `ok=false` 按 0.5、1、2、4、8 秒指数退避，
  下一次有效快照立刻恢复 1 秒周期。失败快照保留上一份真实活动，不得伪造“任务已结束”。
- 权威边界不变：该接口仍是 owner/thread 鉴权后的只读投影，不产生任务、状态、完成、权限或恢复事实；
  本地可信 TUI 的 cwd 与远程用户 `owner_home` 隔离也不因传输优化改变。后续若升级为事件长连接，仍复用
  同一 canonical activity/notices，而不是建立第二份前端状态账。

## 2026-08-24 TUI 进程退出与 durable session 分离【状态：已部署并完成 `.7` 真机】

- 终端交互 对照把三层事实分开：`~/.模型助手/sessions/<pid>.json` 只登记仍活着的 REPL 进程，普通
  `/exit`/Ctrl-D 会走 graceful shutdown 并删除 PID 登记；聊天 transcript/session 继续持久化，可用
  `--resume` 恢复。只有显式 `--bg` tmux 会话才把 exit 映射成 detach，让同一 REPL 继续运行。
- my-agent 的普通 `/exit` 现在明确结束当前 TUI 及其 refresh/notices/worker 线程，但不取消 Gateway
  canonical task，也不删除 session。退出后打印精确 `my-agent resume <session_id>`；取消任务仍只走
  `/stop` 或 `Esc`。外部 tmux `Ctrl+B D` 只是观察者 detach，不得伪装成产品退出。
- 现场慢响应的主要放大器不是 session 文件或 workspace 锁，而是多个已 detach TUI 仍每秒调用
  `/client/notices`，旧活动投影又为少量直属 child 扫描、解析并 deepcopy owner 下全部历史 run。
  Gateway 现在先从 SQLite read projection 按 root/parent/depth 选 exact run id，再从 canonical
  `task.json` 批量精确读取；投影只做查找，不成为生命周期权威。空闲 TUI 健康轮询降到 5 秒，有前台或
  后台任务时仍保持 1 秒，连接失败继续走 0.5/1/2/4/8 秒退避。
- 本轮不增加第二套“在线 session”数据库，也不自动杀 detached tmux。真正在线的 TUI 以后若需要
  `ps/attach/kill` 管理面，应另建带 PID/start-token 的易失进程登记；durable chat session 与 Gateway
  task 仍保持独立，不能按名称或模型文案判断存活。
- `.7` 部署后又用进程采样确认第二个同源热点：后台主代理 readiness 为每条待合批 wake 调用
  `_related_subagent_runs`，旧实现即使解析缓存命中，仍会 deepcopy 全部历史 run 后再筛一棵树。当前适配
  复用 LocalStore 的 `root_task_id` 索引只选择 exact run ids，再逐个读取 canonical `task.json`；managed
  索引异常返回结构化 load error 并保守不推进，不能退回自然语言或展示树猜状态。只有显式
  local-unmanaged/fake manager 没有查询投影时保留 canonical 全扫兼容路径。
- 真机验收保持全部历史数据和待处理 wake，不靠删任务制造低负载：`e2aba94` 部署后后台线程在 12 次
  `py-spy` 采样中均处于定时等待，没有再进入 `list_runs_report/deepcopy`；8 秒 `/proc` 差分约 12% 单核，
  16 个 durable session 并发活动快照最慢 0.294 秒。fresh TUI `ma-e2aba94-session-r14` 的新建、退出、
  exact resume、再次退出均为 status 0，session 总数始终 324，唯一 Gateway 未被停止或复制。

## 2026-08-23 会话运行时 式活轮权限与结果提交栅栏

状态：本地实现与 focused 回归已通过；待与当前 Prompt 4 真机样本安全分隔后部署复验。

- 会话运行时 对照结论：`TurnStarted/TurnComplete/TurnAborted/Error/Shutdown` 与活 thread watch 是执行生命周期
  事实，不会因为一个时间计数器超期就单独宣告 agent 死亡。my-agent 适配为 lease 超 grace
  且 PID/start-token 证明持主死亡才能接管；模型请求长于 lease 不再导致活 runner 被抢。
- current pointer 只是审计引用，不是独立权限证明。工具执行同时要求 active AgentRun + running
  exact AgentAttempt；终态 run/attempt 即使仍保留 pointer 也不得执行只读或写工具。合法后续 attempt 会显式
  把 run 重开到 `created`，记录 `agent_run.started` 及 previous status/generation，之后可再次收口。
- `unfinished/blocked` 等只代表任务仍可继续，不代表本次执行片段仍活着；它们现在用
  `agent_attempt.completed` 关闭 exact attempt、释放工具权限，AgentRun 保持 `created`。下一次 typed wake
  只能通过新 attempt 重获执行权，避免旧片段长期显示 running 或继续调工具。
- runner result 与 runtime.db 建立双层 CAS：canonical active attempt 必须一致；MANAGED 还要求
  exact current generation。宿主先收口的 `done/failed/cancelled` 只接受对应结果；旧 attempt 或已取消
  后迟到的 `DONE` 不得复活 task。原始 provider 响应仍可留在 archive 供查错，但不获得 canonical 写权。
- 会话运行时 的同一 active turn 可在 `needs_follow_up` 时继续下一个物理片段。my-agent 因需持久审计，
  会先关闭旧 attempt 再投影 task 状态，因此提交栅栏必须显式接受
  `run=created + current attempt=done + turn_end_reason=max-tokens/interrupted/blocked` 的同源非终态回写。
  不做这一步会把真实已结束的片段永久留在 TUI `RUNNING`；放宽到任意状态又会让伪完成穿透 CAS。
- 能力申请已在当 attempt 获得 typed grant 后，原 `BLOCKED` 已没有待人处理的外部条件。它现投影为
  同 run `PENDING`，退出 runner session 后由现有 durable auto-start 继续；这避免先把 conversation child link
  写成 blocked，又被 conversation lifecycle gate 阻止续派的自相矛盾。
- 展示层不再复用模型生成的 parsed status 作为终态活动短句。`current_step/current_tool` 由
  typed task status/failure type 投影，生命周期和权限不读取这些中文展示文案。

## 2026-08-24 Gateway 主代理权威 attempt 贯穿回合收口

状态：根因已由 `.7` 原样 Prompt 4 真 TUI 与 RuntimeDB 事件共同确认；本地 focused 回归已通过，待发布后
用全新 TUI 复验最后一名 child 完成会自然唤醒 main。

- 真实失败链：Gateway transport attempt 与 RuntimeDB AgentAttempt 是两个不同身份。主代理入口已把
  `gateway-attempt-*` 替换为 `attempt-*` 并用后者执行工具，但 `_run_with_params` 丢弃了替换后的
  `RunParams`，正常返回仍拿 transport id 调用 closeout。RuntimeDB 因而正确记录
  `closeout_blocked(reason=stale_attempt)`，root run/attempt 永久停在 `created/running`；三个 child 虽均有
  typed completion，最终 wake 也已落盘，后台仍不能合法续挂。TUI 的“整理结果中”只是由 child 状态推导的
  假活跃，不代表存在模型调用。
- 生命周期合同：任务工作区选择与 `_bind_main_agent_authority` 必须在每个物理模型片段执行前完成，返回的
  exact params 必须贯穿该片段的异常收口、Compact 换代、最终收口和后续持久化。`_run_once_with_params`
  只执行已经绑定的片段，不得在局部变量里偷偷替换 attempt 后让调用方继续持有旧身份。
- Compact 续接同样适用：每次 generation 换代先取得新的 RuntimeDB attempt，最终只关闭最新 exact
  attempt；transport id、上一代 attempt 或 thread id 都不能冒充执行权。终态 main run 仍可由下一条 typed
  wake 显式创建新 attempt。这与 会话运行时 `会话运行时-rs/core/src/session/turn.rs` 的同一 active turn 循环及
  `会话运行时-rs/core/src/session/tests.rs::task_finish_emits_thread_idle_lifecycle_after_active_turn_clears` 的
  typed TurnComplete→清除 active turn→thread idle 边界一致。
- 安全边界不放宽：stale-attempt CAS、unknown recovery block、单 thread claim 与客观副作用锁继续
  fail closed。本修复只让执行者携带正确身份收口，不允许调度器忽略冲突，也不消费或伪造历史 wake。

## 2026-08-24 会话根 Todo 持续投影

状态：`84c6b90` 已发布并部署到 `.7`；fresh Prompt 4 r6 已在真实子代理运行期通过 TUI 复验。

- 真实失败链：root 先建立 6 项 canonical `task_progress.v1`，创建 4 个 child 后 TUI 的固定 Todo 整块消失；
  权威账本仍保留 6 项，但 `/client/notices` 的 `conversation_agent_activity.v5.task_progress_items` 返回空列表。
  原因不是 prompt_toolkit 渲染或轮询丢包，而是同一 thread 的活动 link 同时含 root 与 child，旧投影按
  `created_at` 选择最新 link，必然读到较晚创建、没有 root Todo 的 child 账本，再用结构化空列表清屏。
- 对照决定：终端交互 `src/hooks/useTasksV2.ts` 用会话期 singleton store 持有 task list；Spinner 的逐轮
  mount/unmount 不改变清单所有权，存在未完成项时还保留 watcher 与 5 秒 fallback poll。my-agent 不复制
  React store，而是复用已有 `ConversationThread.workspace_task_id` 这一 canonical 会话根身份，按 exact
  task id 选择 root link；child 只通过结构化 `covers/progress_item_ids` 更新或标记 Todo，不得因创建更晚
  抢占面板。
- 兼容与安全边界：缺少 thread loader 或旧测试 fixture 没有 `workspace_task_id` 时，仍保留既有有界 link
  fallback；真实会话存在 preferred id 时只做 exact join，不解析 goal、标题、路径或“子代理”等正文。
  投影仍只读 `task_progress.v1`，不改变 Todo 状态、任务完成、调度、权限或验收。
- 真机结果：`.7` 唯一 Gateway、MiniMax-M2.7、tmux `ma-84c6b90-p4-fzf-r6-todo` 只提交一次原样
  Prompt 4；main 进入“等待 4 个子代理”后，固定 Todo 仍显示 `完成 1/9 · 进行中 6` 和四行窗口，下面
  同时展示 4 个直属 child。该证据直接覆盖修复前“child link 更新更晚就把 root Todo 清空”的失败窗口。

## 2026-08-24 后台主代理富过程事件流【状态：传输合批本地候选 focused 通过，待 `.7` 真机】

- 用户要求后台续跑也采用 终端交互 的正文过程展示：模型过程说明为灰色消息，工具以蓝色标题和缩进结果
  展示，文件修改继续使用已有的行号、红删蓝增 diff 与折叠提示；输入框上方仍只保留一条 main
  `Working` 活动行，不复制第二套状态面板。
- 对照确认本项目现有 `tui_block_renderer.py` 已经具备 `Update/Write/Bash`、结构化输出、折叠和 diff
  renderer；真实缺口位于后台主代理 transport。旧 `BackgroundMainActivitySink` 只保留一条 240 字符
  scalar activity，`/client/notices` 也只传活动快照和最终回复，工具结果携带的公开 `display` 在到达 TUI
  前被丢弃。因此本轮不新增第二套 renderer，而是让前后台复用同一 typed TUI block 协议。
- Gateway 进程为每个 conversation thread 保存有界、易失、单调游标的公开 transcript event ring。它只接收
  已经脱敏和限长的显式 thinking、工具生命周期/`display`，以及真实工具边界前确认的模型过程段；不保存
  hidden reasoning、签名、权限、任务状态或完成裁决。新任务会清掉同 thread 的旧事件正文，但序号不回退；
  慢客户端即使错过 start，也可用携带完整内容的 terminal event 恢复稳定块。
- `/client/notices` 以独立 `event_after/event_cursor` 增量读取该 ring；最终 owner reply 仍走原来的持久
  `background_notice.v2`，活动数量/Todo/child 仍来自 canonical projection。该事件流只增强实时展示，
  Gateway 重启后允许丢失中间过程，不能成为 transcript、任务生命周期、Compact、重试或恢复的新权威。
- `ma-97468f3-longchain-r27` 的 Rust 复刻续轮给出新的直接反例：Gateway 持续执行工具且 main activity 正常
  更新，但 MiniMax 把一段 reasoning 拆成数百个极短 delta；1024 条 ring 被逐 token 事件打满后，TUI 数分钟
  只显示旧 `run_command`，随后一次性追上。对照 会话运行时
  `会话运行时-rs/tui/src/chatwidget/streaming.rs::on_agent_reasoning_delta` 的当前 reasoning buffer，以及 终端交互
  `src/utils/messages.ts::handleMessageFromStream` + Ink 帧级 render throttle，当前传输层保留首片即时事件，
  后续同一 block 按 0.25 秒或 256 字符合批；完整 `thinking_completed` 仍是慢客户端恢复权威。合批不改变
  lifecycle、Compact、正文持久化或完成判断，47 项 background/TUI focused 已通过。

## 2026-08-25 会话运行时 式后台进程会话续接【状态：`8f50d19` 已部署，r53 fresh 真机通过】

- 真实 Rust 构建现场使用 `run_command(run_in_background=true)` 后，返回文案要求模型调用
  `process_status/list_processes/kill_process`，但 Registry 从未注册这三个模型工具；shell 又正确拒绝裸 `&`，
  最终模型只能执行 `sleep 60 && cat log`，额外占用工具轮、延迟回复，并把 sleep 当成进程管理。
- 对照 会话运行时 `unified_exec/write_stdin.rs` 与 `process_manager.rs`：已有 exec session 应由同一个 session id
  续接，空输入做有界等待并返回真实终态；对照 终端交互 `LocalShellTask`/`TaskOutputTool`：后台任务归属当前
  app session，输出和停止走任务面，不重新拼 shell 轮询。当前适配为唯一 `process_session` 工具，提供
  `list/status/wait/stop`，其中 wait 最长 30 秒，超时只返回 running，不伪造失败。
- 单 Gateway 是多用户共享进程，不能照搬单用户内存表。每条后台记录在启动时绑定 executor host 注入的
  `owner_id + conversation session`（无 session 时依次退到 root task/root run/run）以及 owner home；查询、
  日志和停止必须精确匹配，错误 scope 与不存在统一返回 `PROCESS_NOT_FOUND`。模型只能提供 session id，不能
  提供或覆盖 scope。主子代理若属于不同 agent thread 也互不越界。
- 原先三个不存在的工具名和恢复提示已统一改成 `process_session`；模型需要结果时调用 wait，不再运行
  `sleep` 轮询。新增 7 项 focused 覆盖真实后台结束、日志返回、会话隔离、scope 缺失、完整进程树停止，
  并证明能启动后台命令的 main/coding child 工具快照一定同时包含续接工具。
- 子代理的最终执行工具快照把 `run_command -> process_session` 作为结构化依赖闭包：角色默认、显式 coding
  预设、能力申请获批和旧任务恢复都走同一规范化入口；owner 显式 `disabled_tools` 仍在闭包之后做最终收窄。
  因此不是靠提示词要求模型再次申请，也不会只有新建 child 生效、恢复 child 继续缺工具。
- fresh r52 进一步暴露旧内存注册表只活在 child runner：child 启动 HTTP 服务后自然 DONE，bwrap 因
  `--die-with-parent` 随 runner 退出，主代理仍按启动回执误报运行。当前候选保留该安全参数，但把直接父进程
  改为 detached managed host，并将 scope/PID/出生指纹/终态写入 owner 沙箱外权威记录；另一个进程已能
  水合 running、读取日志并 stop。r53 已证明 child/root 结束后服务继续存活、错 scope 不可见、精确 stop
  关闭完整树并持久化 killed。完整合同见
  `docs/design/MANAGED_BACKGROUND_PROCESS_SESSIONS.md`。

## 2026-08-24 较早未决操作的 会话运行时 式软核对

状态：真实 Prompt 4 r5 已给出反例；本地实现与 focused 回归通过，待发布后用新的原样重型 TUI 终态复验。

- 真实失败链：r5 的 root 两次执行 `npm test`/Jest 都以 typed `unknown + TOOL_TIMEOUT` 收口，随后不同的
  build、自写 E2E 和算法脚本成功。最终 `operation_verification` 因 2 个 unknown 保持 `uncertain`，模型却
  把不同测试的成功写成“构建、测试均验证通过”。这不是目录扫描能解决的质量验收问题，而是前序未决
  工具事实被后序成功掩盖后，模型没有在自然 final 前重新对账。
- 会话运行时 对照：`会话运行时-rs/core/src/tools/context.rs` 把每个结果保持为带 `success` 的原生
  `FunctionCallOutput`；`session/turn.rs` 在无 follow-up 的最终草稿处运行 stop hook，blocking hook 的
  continuation 会作为新的 response item 回到同一个 active turn 再采样。my-agent 保留现有逐轮工具 IR，
  并在跨 background wake 的扁平 archive 上补同类有界 continuation。
- 新软核对只读取当前请求 canonical operation records：若较早 effect-bearing 操作仍为
  `failed/unknown/cancelled/incomplete`，而最后一个操作已是其它终态，第一次自然 final 会收到有界未决项、
  操作引用和被退回草稿。模型仍保有原工具，自主选择复查、修复或如实披露；同一签名不重复，整个 active
  turn 最多两次。
- 该机制不是机器验收：不解析 final 正文，不理解“测试/完整复刻”等业务词，不扫描文件、LOC、Todo 或
  产物，也不替模型判任务完成。`unverified` 与 `not_started` 不自动触发这条历史核对；最新操作本身未决时
  继续由既有 completion-conflict 路径处理。即使模型复核后仍决定结束，宿主也接受其第二份自然回复。

## 2026-08-24 子代理 unknown 孤儿恢复预检

状态：`.7` 历史日志与 runtime.db 已确认根因；本地实现及 focused 回归通过，待当前 r6 自然结束后部署。

- 真实失败链：旧 child 的 AgentRun/current AgentAttempt 已被崩溃恢复写成 `unknown`，文件投影一度仍为
  `PENDING`。周期 orphan supervisor 只看投影就调用 durable auto-start，并先把动作计作
  `orphans_revived=1`；真正 runner 到 `create_attempt` 才被权威闸拒绝。现场同一 `agent_run_id` 留下 3 条
  `status_conflict(unknown_run_status)`，最后文件投影成为 `CHANNEL_ERROR` 才停止。
- 底层决定：repository 新增按 exact subagent run id 的 recovery-block 投影，与 `create_attempt` 共用
  `_main_agent_recovery_reason`。周期和 targeted orphan 启动都必须先读该结构化事实；unknown 直接返回
  `authority_recovery_blocked`，不建 runner、不占槽、不虚报复活。
- 安全边界不变：预检只是减少已知无效启动，事务闸和执行权锁仍保留；无权威记录沿用既有 MANAGED
  授权门处理，unknown 不自动改 failed/abandoned，只有人工核对后的 `recover_attempt_unknown` 能释放。
  不读取任务正文、错误字符串、职责描述或重试文案。
- 会话运行时 对照：`AgentStatus::Errored/NotFound` 会让 multi-agent wait/tool 显式失败，重新启动走显式
  `resume_agent`；没有周期任务把异常 thread 先宣称复活再让执行入口报错。本项目因持久 Gateway 需要
  orphan 巡查，但采用同一原则把异常权威状态挡在调度之前。

## 2026-08-24 TUI/Web 共用的子代理详情与精确控制面

状态：完整消息页已由 `91a1c3d` 验收；独立 viewport 已由 `d14549c` 在 `.7` 唯一 Gateway 真 TUI 验收。

- 交互对照采用 终端交互 的列表进入详情习惯，控制语义采用 会话运行时 的 exact agent id。输入框为空时才允许
  `↓` 选择 child，`Enter` 进入；详情继续消费同一 typed TUI event/reducer/renderer，不复制一套子代理 UI。
  当前运行 child 的普通输入是幂等 guidance，已结束 child 只读。
- `Esc` 的唯一语义是停止当前查看的运行中代理。返回父级改为 `Ctrl+G`，并保留 `Alt+←`、`/back` 兼容，
  避免一个按键同时承担 destructive stop 和 navigation。视图栈只改变前端焦点，不改 run/task/session。
- owner-scoped Gateway service 是 TUI 与未来 Web 的共用入口。历史查看要求 exact owner/root/ancestry，允许
  attempt 已结束；guidance/stop 额外要求 active binding，并写 canonical 控制账。终态不会因新输入自动创建
  attempt，未来若增加恢复必须单独实现显式 resume 操作和审计。
- child 公开过程使用 process-shared、有界、增量游标 JSONL；只保存已经公开的 thinking/tool/compact 展示
  事件。它不是任务状态、权限、完成裁决或 Compact 账本，丢失过程时可以降级显示 canonical final/status，
  不能反向更改生命周期。
- 详情页不是状态摘要页。对照 终端交互 `REPL.tsx` 的 `viewedAgentTask -> Messages` 与 会话运行时 每个 child 的
  独立 thread，进入 child 后必须继续使用主代理同一套消息 reducer/renderer：第一条用户消息显示父代理实际
  派发的完整 `task.goal`，后续按原格式显示灰色 thinking、过程 commentary、工具卡/diff、Todo、
  Context/Compact 和 final。底部短 `description` 只属于名册，不能替代详情页 prompt。
- typed callback 的能力判断必须先检查 `write_progress` 等显式方法，再兼容普通 callable；不能因为 sink
  对象本身不可调用就丢弃结构化工具事件。`Ctrl+O` 冻结的也必须是导航栈当前 active runtime，而非固定 root
  runtime，避免进入 child 后展开历史却跳回主代理。
- `.7` fresh tmux `ma-91a1c3d-child-full-r17` 实际进入 `agent-d1-worker-1` 后，普通页显示 child 灰色 thinking、
  `list_files`、`Bash` 和写文件工具卡；`Ctrl+O` 后仍是该 child，Home 首行是完整派工 user block，`Ctrl+G`
  才返回 main。三名 child 的 JSONL 均出现配对 tool 事件，worker-1 现场已有 6 次 started/6 次 completed；
  该行为证据只证明详情展示接线，不替代超级玛丽任务的功能完成验收。
- 视角切换不得复用一份全局滚动锚点或每次强制回尾。普通与详细 transcript control 分别按 exact
  `TuiStateStore` 保存 follow、cursor 和 unseen 基线；新页面默认跟随尾部，返回页面恢复其原位置，选区则
  在切换时清除。默认应用内鼠标直接把滚轮交给当前页面；用户按 F6 进入备用原生复制模式后，footer 必须
  明示历史改用 `PgUp/Ctrl+Home`，并提示再次 F6 恢复滚轮。
- `.7` fresh tmux `ma-d14549c-scroll-r18` exact resume 同一 Prompt 2 会话且没有再次调用模型。root 与
  worker-1 各自滚到首条 prompt 后来回切换，均恢复自己的锚点；F6 后 SGR wheel-up 翻到旧工具调用，
  再按 F6 回到原生复制。自动化不把协议事件冒充宿主 Terminal.app 的物理滚轮或右键菜单验收。

## 2026-08-24 终端原生复制与 TUI 鼠标双模式【状态：历史方案；默认原生模式已被同日 终端交互 对齐取代】

- 既有应用内右键复制只能证明 prompt_toolkit/tmux/OSC52 投影已执行，不能保证 SSH 外层的 Apple Terminal
  系统剪贴板已经改变。用户实测“右键没反应”证明全屏 mouse tracking 吞掉原生右键菜单后，这条
  best-effort 路径不能作为默认交互。
- 该轮曾以 会话运行时 的非 mouse-capture 行为和 终端交互 的 `模型助手_CODE_DISABLE_MOUSE=1` 逃生口为依据，把
  默认改成关闭 prompt_toolkit mouse support。后续真实用户回归证明 alternate screen 因此完全收不到物理
  滚轮，造成“历史消息消失”的直接体验故障；这个默认决定已经废止，下面的 r16 证据只保留为双模式协议
  能切换的历史记录，不能再解释当前默认行为。
- 当前 `tui_mouse_capture_default` 默认为 `true`；终端交互 式滚轮、点击、应用内选区是主链，F6 切到
  原生拖选只是外层剪贴板不接受 tmux/OSC52 时的备用路径。远程 notice 仍只能报告“已选中并尝试复制”，
  不能把不可观测的外层系统剪贴板写成确定成功。
- `.7` 唯一 Gateway、MiniMax-M2.7、tmux `ma-af5a03b-native-copy-r16` 的真实 attach 输出已证明：初始帧
  显式发送 1000/1002/1003 disable，bracketed paste 完整保留“右键粘贴验证ABC中文🙂”，第一次 F6 发送
  三项 enable，第二次 F6 再发送三项 disable，输入正文保持不变。该证据覆盖 TUI/终端协议，但 Computer
  Use 的安全边界不允许代替用户控制 Terminal.app，因此外层 macOS 右键菜单与系统剪贴板仍保留为用户
  attach 后的最后一项验收，不能虚报通过。

## 2026-08-24 子代理插话绑定活跃 attempt 与单一会话容量

状态：`6d33228` 已发布、部署 `.7` 并完成 fresh 真 TUI 复验。

- 真实失败链：TUI 给运行中 child 发送“给我讲讲你在做啥额，你不要停”后，Gateway 把 guidance 写入
  canonical message box，却没有写 `expected_turn_id`。运行时先按当前 attempt 成功 reserve，真正模型
  请求前的原子 submission gate 再发现 receipt 没有 exact turn，抛出
  `guidance submission reservation mismatch` 并结束该 child。WebFetch 失败发生在前，但不是本次
  runner 终止原因。
- 会话运行时 对照：V2 `send_message` 只把消息交给一个 exact agent thread，运行中在消息边界消费且不另起 turn；
  idle 后续工作另用 `followup_task`。my-agent 当前 TUI 只实现运行中插话，因此 Gateway 入账前必须从
  canonical child 取得当前 AgentAttempt，把 exact attempt id 同时写入 guidance 的
  `expected_turn_id`；拿不到活跃/pending attempt 时明确拒绝并保留输入，不得写一条无归属消息，也不得
  放宽 ConversationStore 的原子提交校验。
- 容量只保留一个默认会话树权威：`max_subagents=8` 表示同一 root 最多 8 个未结束 child。会话运行时 的
  `AgentControl::reserve_spawn_slot` 同样只守 session slot；其 `spawn_agent` 是单个创建，所以没有第二个
  “每次最多 4 个”的产品默认。my-agent 的批量工具把
  `subagent_hierarchy_max_children_per_tool_call` 默认改为 `0`（不额外收紧），显式部署仍可配置更小批次；
  `runner_auto_concurrency=8` 让默认八个槽位都能真正并行。历史累计 child 数不是当前占用量，终态释放
  槽位后可以继续创建，所以总历史数量允许超过 8。
- 备用屏幕中的 root/child 历史继续由各自 `TuiStateStore` 持久投影；当前 `.7` exact resume 已用
  `Ctrl+Home` 分别看到 root 原始 Prompt 与 child 完整派工、thinking、工具卡，证明数据没有丢失。当前
  终端交互 式默认直接启用滚轮；主/子代理 footer 显示 `滚轮/PgUp/Ctrl+Home 历史 · F6 原生复制`，只有
  用户主动切到原生复制后才改为 `PgUp/Ctrl+Home 历史 · F6 恢复滚轮`。
- `.7` 唯一 Gateway 的 fresh `ma-6d33228-p3-guidance8-r20` 只提交一次原样 Prompt 3，随后八名
  researcher 同时 RUNNING。进入 researcher-1 输入普通中文后，receipt 的
  `expected_turn_id=attempt-1787621674-fdb6e96a` 与 reservation attempt 相同，最终状态 `consumed`；child
  继续多轮 WebSearch 且未失败。root/child `Ctrl+Home` 和 F6 SGR wheel 均通过，测试结束后已回原生复制。

## 2026-08-24 子代理插话的 provider 消费回执与公开回复

状态：`2bf4602` 已通过 focused/严格 gate、推送、部署，并在 `.7` 唯一 Gateway 真 TUI 验收通过。

- 解决问题：现场 guidance receipt 已绑定 exact child attempt 且最终变为 `consumed`，但 TUI 在
  `/client/agent-guidance` 返回 HTTP 202 时就撤下 pending，把“消息箱已收到”冒充“模型已处理”。
  同一 child 的 provider thinking 已明确识别用户问话，但模型继续工具任务而没有普通 assistant
  回应，用户因而同时看到“没有排队”和“不理我”。
- 会话运行时 对照：`AgentControl::send_input` 把 exact `UserInput` 放入 child thread；TUI 的
  `pending_steers` 在注入/消费边界前保持可见。终端交互 对照：当前 viewed teammate 的输入
  立即进入该 teammate 自己的 `pendingUserMessages` 和消息页，再由 runner FIFO 消费。本项目
  不复制第二套队列，继续使用唯一 ConversationStore guidance receipt。
- 新合同：Gateway 接受只回复 `delivery=queued,status=pending`；child TUI 保留 exact
  `message_id` 的灰色 pending 行。只有 provider 请求成功并把 receipt 推进到 consumed 后，child
  `BackgroundTranscriptSink` 才向同一 run 的耐久展示流写
  `active_turn_input_consumed(client_message_ids)`；详情页按 exact ids 把 pending 原子提升为 user
  history，不比对文案。连续多条输入按真实注入顺序提交和展示，不按随机 UUID 排序。
- 回复边界：用户插话仍是普通 UserTurn，不增加自定义验收器或强制断轮。child 的系统提示
  明确要求先在普通 assistant 消息中回答或确认真实用户，再继续原任务；provider thinking
  不能代替对用户的公开回复。机器状态仍只看 typed receipt/event，不解析这句提示。
- `.7` 唯一 Gateway PID `610573`、MiniMax-M2.7 的 exact resume
  `ma-2bf4602-child-chat-r25` 进入运行中的 `agent-d1-researcher-10`，连续输入两条普通中文。
  两条消息在下一次 provider 消费前同时显示于 pending 区；随后按输入顺序出现在 child 用户历史。
  同一耐久事件流先写 seq 46 `active_turn_input_consumed`，其中 exact ids 顺序为
  `agent-steer-b9030ebf56cb4837`、`agent-steer-c9ef34a1ffca42f2`，再写 seq 47 普通
  `assistant_completed` 分别回答两问，seq 48 继续 `web_fetch` 原任务。`Ctrl+O` 全程留在 child 视角。

## 2026-08-24 子代理名册的选择可见性【状态：已部署 `.7` 并真 TUI 验收】

- 子代理导航的权威仍是按 parent 保存的 ordered exact run rows；八行名册只是 renderer 视窗。历史累计
  超过八项时，方向键可以把 `selected_run_id` 移到第九项，旧 `rows[:8]` 却继续画前八项，造成“屏幕无
  高亮但 Enter 能进去”的控制/显示分裂。
- 参考 终端交互 `MessageSelector` 由 `selectedIndex` 推导可见窗口：选中项还在前八项时保持原窗口，越过
  下边界后只移动足够多的起始行让 exact run 出现。该计算是纯投影，不增加 scroll 状态，不改变 canonical
  排序、状态、容量或 Enter 的 run-id 裁决；屏幕外总数继续由本地裁剪数与 backend hidden count 相加。
- `7e2ffb6` 在 `.7` 唯一 Gateway、MiniMax-M2.7 的 exact resume
  `ma-7e2ffb6-roster-r21` 验证九项名册：第九次 `↓` 后 coordinator-9 带 `›` 出现，`Enter` 进入同名详情；
  `Ctrl+G` 返回再按 `↑` 后 researcher-8 高亮且前八项恢复。测试者没有发送模型消息或改任务产物。

## 2026-08-24 终端交互 默认滚轮回归修复【状态：`0da26f0` 已部署 `.7` 并真 TUI 验收】

- 用户在 exact resume 中用 `PageUp` 和 `Ctrl+Home` 能看到欢迎页、原始 Prompt 与旧工具记录，证明 canonical
  history 和每页 viewport 均未丢失；物理滚轮无反应来自 `af5a03b` 把 mouse tracking 默认关闭，不是消息
  加载或 Compact 删除。这个取舍使“能复制”和“能看历史”变成二选一，属于默认交互回归。
- 参考 终端交互 `AlternateScreen.tsx`、`fullscreen.ts`、`termio/dec.ts`、`ScrollKeybindingHandler.tsx`、
  `ScrollBox.tsx`、`selection.ts`、`useCopyOnSelect.ts` 与 `termio/osc.ts`：全屏默认开启鼠标；wheel 直接驱动
  ScrollBox，向上打破 sticky、回到底才恢复；应用内选区 copy-on-select，并投影 native/tmux/OSC52。
- 本项目已有相同的 typed viewport、中文源字符选区、lost-release、右键复制、tmux buffer 和 OSC52 主链，
  因此修复只把唯一默认恢复为 `true`，让 F6 成为原生复制逃生口，并让 footer 从 typed mouse state 动态
  显示当前真实操作。不得增加另一份历史、滚动游标或终端品牌判断。
- `.7` 唯一 Gateway PID `557079`、MiniMax-M2.7、exact resume tmux
  `ma-0da26f0-终端交互-history-r23` 未提交新模型消息。默认状态直接接收 SGR wheel：root 离尾出现
  `Jump to bottom`、回底后消失；进入 researcher-1 后滚轮翻到完整派工、Thought 和工具记录。F6 的 footer
  在“原生复制/恢复滚轮”间真实切换；输入框拖选“中文复制验证ABC”后 tmux buffer 得到完整 9 字符，右键
  再次复制结果相同。外层 macOS 系统剪贴板仍只能由用户 attach 后亲自粘贴确认，不能由 tmux 证据冒充。

## 2026-08-25 后台续接操作事实与精确子代理交接引用【状态：第二层修复严格 gate 通过，待真机】

- 会话运行时 的 active turn 只有收到 typed `TurnCompleted` 才清除 Working；不能用前端超时、最终正文或 child
  数量猜终态。本项目同样要求 background slice 恢复原轮成功工具时，继续携带 host-owned
  `tool_execution` 与 `tool_operation`。这两类字段必须在工具输出首次 externalize 时进入有界耐久索引，
  后续 carried record 只从该结构化白名单恢复，不能解析工具正文里的 operation contract。
- 真机失败证明：`create_subagents` 原始结果已有 `tool_operation.status=succeeded`，但外置索引只保留
  `ok=true`；child 完成唤醒后的新工作片因缺少 operation 终态把派工判为 `unverified`，自然 final 仍落成
  unfinished，active task link 因而持续显示 Working。修复必须保留既有 fail-closed 核验，不允许把所有
  `ok=true` 副作用直接猜成 succeeded。
- `4106025` 部署后的同一长 TUI 又暴露第二层身份错位：前台工具行的 request/run/task 都是 exact foreground
  request，后台 wake 却拿 durable task id 查索引；两者都合法但不是同一个概念。当前按 会话运行时
  `session/inject.rs`、`session/turn.rs` 与 `tool_dispatch_trace.rs` 的 per-turn 绑定方式，把 child 创建时的
  `conversation_request_id` 沿 canonical child state → completion wake → background carried scope 传递；
  新索引显式落该字段，旧索引仅在 row `request_id` 同值时兼容，不扫描同 task 的其它用户轮。
- `work/agents/<run_id>/final_report.md` 由宿主写入，只含 task/run/status 与 child 最终回复，是完成信封已经
  暴露给直接父级的有界交接投影，不是 canonical 状态权威。精确该文件允许通过 `read_file` 按现有
  workspace/owner 边界读取；同目录的 state/checkpoint/summary/progress、目录枚举和 shell 仍拒绝。
  不复制第二份报告，也不让 final_report 正文参与 child 或 root 的完成、权限、验收裁决。
- child 自己的模型上下文只暴露用户/父级明确声明的业务产物：`required_file_refs`、显式 output/artifact refs
  与实际写入根。宿主拥有的 `final_report/output.json/runner_result` 不进入 child 的 output contract、task
  packet、workspace refs 或旧 bundle 摘要；否则模型会把运行时收口文件误当成第二份交付并浪费工具轮。
  child 自然 final 后由宿主一次生成交接投影，语义对齐 会话运行时 将 child 最后一条 assistant message 投给父级。
  工具执行器仍在内存中持有完整 write boundary，落盘 execution-context 仍供宿主审计，但两者不作为模型
  渐进披露入口；模型只看 cwd、读写根、grant 与安全后的 context bundle。attempt 恢复只暴露 checkpoint、
  summary、task 等续跑线索，旧 bundle 中的 closeout refs 也必须在 runner prompt 边界再次过滤。
- 同一现场的 completion 信封已经给出正确 `final_report_ref`，但模型保留 exact run id 后拼入了完整 durable
  task id，形成不存在的目录。读取层只对这个 exact final-report 叶子按同 owner
  `agents/<run>/state.json` 投影找 canonical ref，并再次执行原读权限检查；run id、run dir、owner containment
  任一不一致即不跳转，state/checkpoint/list/shell 不共享该能力。
- 部署后验收继续复用同一个 durable TUI：多 child 调研、两个普通追加、多 child 复刻、普通验收追加、
  多 child 返工和最终追加必须连续发生在一条 session 中。该链只测试跨工作片连续性，不改变单次任务边界，
  也不允许测试者替被测对象改产物或用技术提示直接告诉它底座诊断答案。
- 同一 r27 返工又暴露派工次序是假象：root 正文明确“先修复、再独立测试”，却把 worker/tester 放进同一
  `items`，两者被宿主正确地立即并发，tester 反而早 27 秒结束。对照 会话运行时
  `会话运行时-rs/core/src/tools/handlers/multi_agents_spec.rs` 的 concrete/bounded/independent sidecar 纪律，
  `create_subagents` 模型合同必须直说：同批每项立即并行，自然语言“先后”不形成依赖；B 读取 A 的未来
  修复、产物或结论时，先只创建 A，等 lifecycle wake 后再创建 B。当前只改唯一工具说明和 schema 参数
  详情，不解析 goal/role、不硬拦 tester、不新增 `depends_on` 调度器或机器质量验收。

## 2026-08-24 单 Gateway HTTP 有界复用工作池【状态：`53498c1` 已部署 `.7` 真 TUI】

- 解决问题：唯一 Gateway 被 OOM killer 杀死后，新 TUI 只能短暂显示“正在连接 Gateway”再退出。内核事实
  证明被杀进程 RSS 约 6.68 GB；现场 8 个长期 TUI 以活动态 1 秒/空闲态 5 秒轮询，而标准库
  `ThreadingHTTPServer` 为每个请求创建一个新 OS 线程。即使请求结束，反复 transcript/child 投影分配也会
  放大 allocator 高水位；一旦 handler 变慢，线程和请求对象还会同时堆叠。
- 会话运行时 对照：`app-server/src/in_process.rs` 以固定 Tokio task 处理消息，入口和出口均使用容量 128 的
  bounded mpsc channel；thread list/state 再用单 permit semaphore 串行权威更新。这里不引入第二套 ASGI
  或新依赖，只适配它的“固定执行者 + 有界排队 + 显式背压”原则。
- 唯一实现位于 `gateway_parts/bounded_http_server.py`：16 个可复用 daemon worker，运行与排队合计最多
  128 个；满载在鉴权/业务 handler 前返回 `GATEWAY_HTTP_BUSY`、HTTP 503 与 `Retry-After: 1`。客户端现有
  transport failure backoff 负责稍后重试，忙不等于任务失败，也不得创建、取消或改变 run。
- 停机先关闭准入并取消未开始的 Future；取消回调关闭对应 socket 且精确释放一个槽位。已经进入 handler
  的请求沿原 typed ledger/幂等合同自然收尾，daemon worker 不参加解释器全局 join，保持旧 Gateway 可控
  重启边界。accept backlog、owner 鉴权、active turn、模型并发和子代理恢复均不借此放宽。
- 多用户对照结论：通道运行时 在入口保护上更完整——未鉴权 WebSocket 按 IP 限连接，鉴权失败按 scope/IP
  滑窗退避，控制写操作再按 device/IP 单独计费；长期助手 更擅长 resolved session lease、profile 独立状态库
  和全局 agent-run 上限。当前项目不在 socket 层读取可伪造的 `X-User-Id` 做“假公平”：transport 只守全局
  16/128 硬上限，鉴权后沿既有 request admission 守每用户 8、全局 500、同会话单飞，后台再按 owner
  round-robin 与每 owner 4 条会话车道调度。若未来改 WebSocket，优先适配 通道运行时 的 pre-auth connection
  budget；不能把连接 IP 当成最终 owner（同机 TUI、飞书适配器和 NAT 都可能多人共用 IP）。
- 固定 worker 按 TCP connection 执行 stdlib handler，因此所有 JSON/metrics 响应显式
  `Connection: close`；否则一个客户端只需保留 16 条空闲 HTTP/1.1 keep-alive 就能占满全部工位。当前 TUI
  本来就是短轮询，这一边界只牺牲无用的连接复用，不改变 session、历史、模型 turn 或 owner 状态。
- 验收：40 项 focused 和本地严格 gate 通过；`.7` 唯一 Gateway PID `604186` 下，旧 TUI 自动重连，fresh
  `ma-53498c1-http-pool-r24` 约 1 秒启动并完成 MiniMax-M2.7 真调用。9 个 TUI 自然轮询时只创建 5 个
  `gateway-http_*` worker、旧 request thread 为 0，12 秒 RSS 约 132.7 -> 131.7 MB。该证据只证明当前
  有界实现与短时稳定，不把 12 秒观察写成长期内存无泄漏结论。
## 2026-08-25 长期进度账本与当前回合 Todo 分层【状态：本地 focused 通过，待 `.7` 真机】

- 问题：同一长期 task-path 连续经历调研、追问、复刻和返工时，canonical `task_progress.v1` 必须保留恢复所需
  的完整历史；旧 TUI 却直接投影全部 items，导致新阶段仍显示上一轮 `4/7`，迟到的后台 notice 还能再次覆盖。
- 对照：会话运行时 app-server 的 `TurnPlanUpdatedNotification` 明确携带 exact `turn_id`，TUI 的 `on_plan_update`
  接受一份本 turn 完整 plan；终端交互 在用户消息提交 commit 后调用 `repinScroll/scrollToBottom`，手动
  scroll-away 才冻结 viewport。
- 决策：不拆第二本进度账、不按标题/状态/时间猜阶段。完整 items 仍是唯一 durable 软账本；旁挂 host-owned
  `display_plan={generation_id, revision, item_ids}` 只负责显示。普通 conversation request 换代，同一 root
  lifecycle wake 和 child continuation 沿原 exact request；同代结构化写入合并 ids，修订号只在集合变化时递增。
- 2026-08-28 的 `.10` Prompt 3 续话复现了增量写入缺口：主账本仍有 17 项，下一普通回合只更新 3 个已完成
  id 后，TUI 被缩成 `完成 3/3`，其余未完成项实际没有丢。对照 会话运行时 `update_plan` 每次传完整 plan，以及
  终端交互 `TaskListV2` 从持久清单优先选择最近完成、进行中和待办，当前换代只淘汰上一代已经关闭且本轮
  未触碰的历史项；canonical ledger 中所有非终态 id 与本轮显式触碰 id 自动进入新 display plan。同代继续
  增量合并。这个连续性只读结构化 status/id，不解析用户续话或标题，也不改变任务状态、完成、调度和验收。
- 边界：Todo 投影不参与任务结束、验收、调度、权限或 Compact；旧账没有 display plan 时继续展示完整 items。
  TUI reducer 只接受当前期望 generation 且不倒退 revision 的快照，最终 notice 也携带同一身份。
- 视窗：每个 main/child store 仍保存自己的 follow/cursor；真实 prompt_toolkit Window 通过公开
  `get_vertical_scroll` 每帧应用锚点。提交、首次进入或显式 End 粘底，手动上翻不被新输出抢走。
## 2026-08-26 大工具参数流的脱敏可见进度【状态：已部署并通过真 TUI】

- `.7` 长任务中，`source-reader-1` 在生成 56,876 字节分析文件前约 4 分钟没有新的 transcript 事件；
  进程、TLS 连接和收包计数均持续推进，证明不是锁、进程退出或 provider idle。现有 Anthropic parser
  只累计 `input_json_delta`，直到 `content_block_stop` 才一次性交出工具块，TUI 因此无法区分“仍在生成
  大参数”和“真正无活动”。
- 会话运行时 在 `core/src/session/turn.rs` 保留 `ToolCallInputDelta`，并由 `apply_patch` 的 diff consumer 以
  500ms 合批发布 patch 更新；终端交互 在 `services/api/模型助手.ts` 累计 `input_json_delta`，再用
  `streamingToolUses` 渲染未完成工具。当前实现适配同一原则，但公开面只含工具名、阶段、累计字符数和
  起点，不保存或展示半截 JSON/文件正文。
- 新投影纯属 display：不创建 ToolCall、不提前执行 handler、不刷新 task/Compact/验收，也不取得权限或
  完成权威。后台事件必须按时间和字符阈值合批；真实工具开始、provider 重试或回合终态时删除临时块，
  继续复用现有工具/diff 卡片。Gateway 重启可丢这段易失进度，不影响 canonical tool/result 与最终回复。
- 当前实现以 `provider_tool_input_progress.v1` 作为唯一公开白名单，provider 层首条、1 秒/8,192 字符和
  ready 合批；Gateway rich、后台 main/child 与本地 TUI 共用 transient projector。8 个相关测试文件
  195 项与本地严格 gate 通过；`8c11eab` 已部署 `.7` 唯一 Gateway。fresh r37 在真实复杂任务中显示
  515 chars/5s → 3.0k chars/29s，55s 后完整 `create_subagents` 工具卡接棒并删除临时行，边界成立。

## 2026-08-26 Anthropic-compatible 主动缓存断点【状态：已部署并通过真 TUI】

- 当前原生工具循环每次都复用稳定工具清单、首条真实任务和不断增长的历史，但旧 backend 没有发任何
  `cache_control`。真实 MiniMax-M2.7 child 的 82 次调用累计 4,225,275 input，cache write 为 0，不能再用
  当前 Context 数字或 Compact generation 代替 provider 成本事实。
- Anthropic 协议的唯一投影入口位于 `backends/anthropic_prompt_cache.py`。首版按 provider 规定的
  tools → prompt → messages 顺序，最多使用三个宿主断点：最后一个工具、首条 prompt、最新可缓存历史块。
  所有修改都是 copy-on-write，不污染 canonical native IR，也不让展示、缓存或 Compact 互相取得裁决权。
- 该能力只在调用方显式传入 `messages` 的原生多轮链启用；普通 text/auxiliary 单次请求保持旧 payload。
  `anthropic_prompt_cache_enabled` 是唯一兼容开关，YAML 与 dataclass 默认均为 true。端点拒绝标准字段时由
  用户明确关闭，不增加 provider 名称猜测、错误字符串重试或第二条隐式 fallback。
- `69bf2ec` 部署后的 fresh TUI 暴露 native 第一轮空 IR 被 `messages or None` 压成 text 语义，前三次真实
  调用因而没有 cache write/read。统一入口现在固定 `None=text`、`[]=native empty history`；原生首轮从
  第一次请求就建立工具/prompt 缓存，后续继续推进 history 断点。这个类型区别属于 provider 协议事实，
  不依赖任务正文、模型判断或界面状态。
- 同部署配置的直连 MiniMax-M2.7 探针已证明协议可用：首次写 10,551 tokens，第二次读 10,541 tokens。
  `df95d27` 的 193 项 focused 与严格 gate 已通过、推送并部署 `.7` 唯一 Gateway；fresh TUI
  `ma-cache-firstturn-r34` 首请求真实写 25,999、读 12,313 tokens，两个普通后续回合继续读 37,234 与
  46,972。协议探针与完整 Agent 主链现已互相印证。
- 会话运行时 的 session-scoped `prompt_cache_key` 证明稳定会话前缀应由底座承担；具体 wire 字段继续服从当前
  Anthropic-compatible 协议。真机是否有效只读 provider usage ledger 的 cache-write/cache-read，不根据
  延迟、上下文百分比或自然语言推断。
- 同 thread 的 `compact_generation=0` 与 69.1k→46.3k Context 回落并不冲突：终态工具折叠只替换下一轮
  的模型可见投影，完整 archive 与 exact refs 保留，Compact 权威代数不变；最后主轮仍有 40,527
  provider cache-read。该边界继续由结构化 fold/ledger 裁决，不要求 Context 数字单调递增。

## 2026-08-27 原生 prompt 的三段追加式缓存【状态：双 provider 已取证；本地长任务失败样本保留】

- `.7` 同一真 TUI 的脱敏请求指纹证明：25 个工具和顶层宿主 system 的哈希连续不变，但旧首条 user prompt
  在同一请求的工具轮里从约 31KB 增到 33KB，普通下一回合又变成约 45KB。变化来源是相关记忆、当前时间、
  Conversation 热尾、当前任务和执行事实；它们与约 23KB 的系统规则、Persona、Skill 索引和工具目录被绑在
  一个文本块里。MiniMax 因而只能复用工具前缀，并反复创建约 11K 动态 prompt 缓存。
- `prompting_parts/cache_layout.py` 的 `CacheStructuredPrompt` 仍以普通字符串保留完整 prompt，
  同时用 `PromptCacheLayout` 明示四个机器边界：`stable_prefix`、兼容旧调用方的
  `stable_user_prefix`、仅供完整诊断/归档的 `canonical_user_turn` 与 `volatile_suffix`。边界不解析标题、
  用户正文或模型语言。稳定 system 只含 System、Owner Scope、Prompt Files/Persona/Skill 与文本工具目录；
  当前 user 只通过 canonical messages 发送一次；记忆召回、推荐工具、任务工作区、Conversation/Runtime
  Injection 与本次执行事实留在动态尾部。
- Anthropic-compatible 的唯一 wire 顺序是 `system/tools -> committed summary/completed messages ->
  current user -> current-turn canonical IR -> volatile facts`。顶层 system 和 tools 各有一个稳定断点，
  message 级只有一个断点，它在每轮
  copy-on-write 移到最新可缓存历史块；动态事实永远在该断点之后。这适配 终端交互
  `addCacheBreakpoints` 的单 message marker，也保留 会话运行时 的旧输入前缀先于新输入原则。
  OpenAI-compatible 无 `cache_control`，但使用同样的消息顺序供本地服务器 KV 前缀缓存。
- 工作区不得进入稳定 user：同一 run 的首次请求看到 pending，首个工具后才看到 canonical
  task root，这是结构化状态转换而非可猜的文案。它留在 IR 后既不污染稳定前缀，也不丢失模型必需的当前路径。
  完整文本、canonical IR、归档、Compact、owner/task 权限均不变。
- `ma-cache-probe-minimax-r60` 的同 thread 真回合中，首个新布局请求为 25,192 普通输入；下一轮为
  5,897 普通输入、13,967 cache-read、5,333 cache-write；再下一轮稳定 system 与 tools 均命中，得到
  5,959 普通输入、19,300 cache-read、0 cache-write。请求指纹同时证明 system/tools 哈希不变、动态 user
  哈希按真实对话增长；模型能继续准确引用前文。
- 按用户指定单价，未命中基线 `25,192 * 5`；稳定命中轮在“输入 5、缓存 0.1”时成本约下降 74.8%，在
  “输入 5、缓存 1”时约下降 61.0%。这些百分比只对应本次 MiniMax 样本；不同模型、TTL、容量淘汰和长会话
  仍必须读取 provider usage ledger。下一阶段用同一长多子代理 prompt 分别验证 my-agent 本地模型/
  MiniMax，以及 会话运行时/终端交互 的 MiniMax 对照，不用屏幕 Context 猜成本。
- r61 仅有 system/tools 稳定时，161 次调用共 4,696,945 普通 input / 1,286,446 read /
  21,118 write，两档缓存价只节省约 21.0%/17.1%。r62 开启追加式 IR 后自然终态，201 次调用为
  438,162 普通 input / 6,097,485 read / 553,087 write / 117,003 output；按输入 5、缓存 0.1/1，
  相对全部普通输入分别节省约 84.30%/68.81%。r62 额外出现一个重复替身 child，故调用数和用量不是
  理想最小值，账本仍按真实物理调用完整计入。
- 所有 `BaseBackend.generate` 实现必须接受统一的 thinking observer，是否产生事件由真实 provider 协议
  决定，不能因某后端不返回思考就省略公开接口参数。OpenAI-compatible Chat Completions 的
  `delta.reasoning_content` 只作为展示增量和白名单 assistant history：第一段正文/工具或流结束形成 typed
  complete；普通 assistant 文本不携带扩展字段，只有工具调用续轮按 DeepSeek/Qwen 兼容合同回放
  `reasoning_content`。该事件不进入用户 prompt、任务状态、权限或工具裁决。
- 本地模型 r64 的首段 provider 账为 77,244 input / 40,960 cached / 0 write / 2,740 output，证明同一
  追加布局也能被 OpenAI-compatible KV 前缀命中；两档价格相对全普通输入分别节省约 51.97%/42.42%。
  该任务随后因一个 child 的 600 秒流总墙钟超时和另一个 child 的长时间未完成而由测试者从 TUI `/stop`，
  所以成本只能作为已回执调用的下界，功能完成度必须判失败。缓存命中、provider 可用、模型吞吐和任务
  编排是四个独立事实，任何一个都不能替另一个宣告通过。

## 2026-08-27 慢模型流式存活合同【状态：本地候选已通过 focused，双机真 TUI 待验】

- 解决问题：本地慢模型持续返回有效 SSE 数据，旧底层仍把同一个 `request_timeout=600` 同时当滚动 idle
  和整次总墙钟，健康 child 在工作约 67 分钟后被固定墙钟误杀。128K 输入按 200 token/s 预填充约需
  640 秒，单纯把 600 改成更大的固定总时长仍会在更长上下文或慢输出上重复失败。
- 对照 会话运行时 `responses.rs` 的逐 `stream.next()` idle 合同和 通道运行时 的 first-event/idle 分相，流式请求
  只保留短 connect、按本轮输入量估算的 first-event 与首事件后的 rolling idle；每条有效 SSE `data:`
  重置 idle，空行或注释不算进展。健康流没有隐式 total wall，停止依赖 `/stop`、provider 断流或完整
  idle 窗口；非流式请求仍使用有界总预算。
- 动态首事件预算是 request-local option，不再改共享 backend 的 `request_timeout`，因此同 Gateway 的
  main/child 并发调用不会互相覆盖。默认估算为 prefill 200 token/s、output 20 token/s、安全系数 2，
  上限 10,800 秒，可覆盖约 1M 输入的极慢预填充；小请求仍按本轮 token 量得到较小预算。
- 超时账本把 `first_event`、`stream_idle` 和非流式 `wall_clock` 分开，恢复或重试只能读取 typed stage，
  不解析错误正文。流已经产生工具调用或副作用后仍不得无脑重放整轮。

## 2026-08-27 活动 covers 占用与 会话运行时 式共享工作区【状态：本地候选已通过 focused，真 TUI 待验】

- 解决问题：真实 r62 中原 工具运行时 child 从 capability 阻塞恢复并继续运行，root 又创建一名相同
  `covers=["2"]` 的“替身”，形成九名 child 和额外 token 消耗。旧 `planned_dispatch.v2` 只查新批次内
  重复，不查已有直属兄弟；replacement edge 写失败后，新 child 也可能仍被启动。
- `planned_dispatch.v3` 只读同一 exact parent 的直属兄弟。PLANNING/PENDING/RUNNING/BLOCKED/PAUSED
  持续占用其 exact covers；新 item 若未用 `replacement_for_run_ids` 明确引用占用 run，则整批在任何 child
  落盘前返回 `active_covers_by_item`。不按标题、goal 或自然语言相似度猜重复。
- owner/task 的短创建 guard 只包围“容量/占用复检 → 创建 PLANNING 记录 → 写 takeover edge”，不包围
  child 运行。replacement 必须属于同一直属父级、尚未被接管且同批唯一；任一 edge 未落账，新 child 在
  发布启动前结构化取消并返回 typed error。
- 同时撤销旧的 output 目录重叠硬门。会话运行时 允许 sidecar 共享同一项目工作区，`output_files` 只是可选
  协调元数据和父 workspace 上界检查，不是目录锁；真正同文件写冲突继续由 goal 中的 disjoint write set
  软纪律和工具事实处理。安全硬门只保留越过父工作区、冒充身份和危险写入。

## 2026-08-27 跨完成回合缓存边界与模型目录参考快照【状态：布局 focused 已通过；真 TUI cache-read 待复验】

- 生命周期 wake、恢复、记忆召回和普通插话会变化，不能留在 native history 之前。Gateway/child 会话层先
  交付一份已经 Compact、按完整消息边界收缩的 `ConversationHistorySeed`；runtime 按 committed summary →
  completed user/assistant → exact current user → current-turn tool IR 组装 canonical messages。当前 user 在
  `CacheStructuredPrompt` 字符串里只留诊断副本，原生 provider 明确省略该副本，因此 wire 只发送一次。
  workspace/runtime injection/recommendations/execution facts 全部追加在 messages 后；不同 wake 或普通新任务
  不再重写旧会话前缀。text 协议从同一 seed 渲染一次历史，不能回读 transcript 形成第二套窗口。
- main、child 与 grandchild 走同一合同；普通自然回执等 isolated 辅助轮默认不携带任务历史，避免把长期工具
  上下文复制进第二次表达调用。只有真正 Conversation Compact 可以替换旧前缀并推进 generation。
- 最新型号先记录在 `docs/architecture/MODEL_CATALOG_SNAPSHOT.md`，明确它不是 allowlist、路由或价格事实。
  当前 provider 未知型号继续透传，实时 `/models`/详情优先。未来目录按用户显式配置 > provider 发现 >
  随版本种子合并，并把 thinking、sampling、reasoning replay 等差异放入声明式 compat 或 provider adapter，
  不在核心链散落型号字符串判断。
- 该快照核对了 OpenAI 官方 GPT-5.6 Sol/Terra/Luna，以及 通道运行时 当前 MiniMax M3/M2.7、DeepSeek V4、
  模型助手 5、Kimi K3/K2.7、Qwen 3.7 和 GLM-5.2 代表型号。endpoint、headers、凭据和账号可用性仍只来自
  用户本地配置与 provider 实时事实，远端目录不得取得这些权限。

## 2026-08-27 缓存热尾、冷折叠与本地搜索空结果合同【状态：已部署；热期真 TUI 通过，冷边界长等待中】

- 对照 终端交互 `microCompact` 的时间门：缓存仍热时保持历史不动，确定冷却后才清旧工具结果。当前适配不另造
  provider cache 状态，而把同一已结束回合一次落成 `conversation_terminal_tool_fold.v2`：`hot_text`、
  `text`（cold fold）和 `fold_after_epoch` 都只写一次。读取投影在默认 300 秒热期内保持固定 hot-tail，之后
  一次切到固定 cold-fold；`0` 可显式恢复立即折叠，配置上限为 86,400 秒。该切换不推进
  `compact_generation`，不读模型文案，也不复制 owner archive 的完整原始输出。
- 300 秒是保守默认值，不宣称所有 provider 的缓存寿命相同。不同端点应以相同前缀的 provider
  `cached_tokens/cache-read/cache-write` 账本校准；延迟和 Context 百分比只能辅助观察。`127.0.0.1:8901`
  本地模型按真实 V2 约 6k 冷热投影 A/B：hot 第二轮 14,998 input / 14,336 cached，立即 cold 为
  15,072 / 12,288。普通输入价 5、缓存价 0.1/1 时，hot 成本分别为 4,743.6/17,646，cold 为
  15,148.8/26,208；热期分别节省约 68.7%/32.7%。该样本证明当前投影的即时经济性，不证明长期 TTL。
  另一唯一前缀在 prime 完成后等待 55 秒再追问，9,025 input 中仍有 8,192 cached。进一步用 endpoint 专属
  固定前缀分别走 `127.0.0.1:8901` 直连与 `127.0.0.1:4000` LiteLLM 入口，约 70 秒和约 310 秒的
  12.6k prompt 均命中 12,288 cached tokens，证明当前本机 OMLX cache 至少保留五分钟。4000 实际仍转发
  8901，因此这是两条 API 路径、一个底层缓存，不冒充两个独立 provider TTL，也不把 310 秒写成过期点。
- `search_text` 仍是本地字面/正则搜索，不按自然语言猜联网意图。模型合同明确“本地、连续原文、非语义、非
  联网”；每次结果追加 `local_text_search.v1` typed envelope，记录 source path、match mode、backend、
  complete/status/hint code。完整 no-match 与扫描不全分开；模型可据 `LOCAL_LITERAL_NO_MATCH`、
  `LOCAL_REGEX_NO_MATCH`、`LOCAL_SCAN_INCOMPLETE` 改路径、缩短标识符、使用显式正则或改用 web_search，
  但宿主不自动拆词、不自动联网，也不把“没命中”升级成“源码不存在”。
- MiniMax-M2.7 三个真实 native tool-choice 探针分别证明：未下载的 GitHub 调研选择 `web_search`；本地精确
  `PromptBuilder` 选择 `search_text(query=PromptBuilder,literal=true)`；收到 typed local no-match 后改用
  `web_search(代理运行时 github repository)`。这些只验证模型合同/修复方向，部署后的完整 TUI 仍需 fresh
  session 验证真实工具输出、ConversationStore 热尾和 provider usage。
- fresh TUI 第二轮曾在模型调用前报 `CONVERSATION_PERSISTENCE_UNAVAILABLE`。结构化诊断定位为 sticky
  workspace 已存在但该 thread 尚无 `observations/*.jsonl`：按需创建的可选账本缺失被错算成 I/O 损坏。
  参考 会话运行时 对可选文件 `NotFound` 返回默认空状态的边界，ConversationStore 现在只把“文件不存在”投影为空
  observations；同样按需创建的 exact-target guidance queue 缺失也为空。已经存在但不可读或行损坏仍保留
  load error 并阻止静默丢事实。该修复不放宽消息 transcript、task link、Compact checkpoint 或 guidance
  receipt 等权威文件的失败语义。
- `.7` fresh r59 首轮 V2 deadline 过后，结构化投影明确为 `cold_fold`；同一 MiniMax-M2.7 TUI 在不调用工具
  的条件下仍准确复述首轮唯一搜索串和 backend/status/scan_complete/hint_code，证明 cold 投影未丢该轮
  继续工作所需事实。该功能正确性不冒充 provider cache 命中；对应 MiniMax 普通后续轮账本实际为 0
  cache-read，缓存经济性结论仍只使用上述本地受控 A/B。

## 2026-08-28 完成回合原生历史、慢流活动与 Compact 用量收口【状态：`.10` MiniMax 已通过，`.7` 慢模型样本已主动停止】

- 对照 会话运行时 `core/src/context_manager/history.rs` 与 `core/src/session/turn.rs`：会话先持久保存 typed
  `ResponseItem`，只有 provider 出站边界才做协议转换；正文、推理和工具参数 delta 都是 typed event，
  Compact 是替换旧历史的唯一入口。本项目适配为 `conversation_native_messages.v1`：每个完成回合在最终
  assistant metadata 保存该回合 exact user/assistant/tool_call/tool_result 消息，下一轮在同一完整消息窗口
  里直接回放。可见 transcript 仍是用户/助手正文，不承担从散文猜回工具结构的职责。
- 每轮会变化的 workspace、memory、推荐工具和运行事实不再以“临时 prompt 尾巴”出现后又消失；首次出站前
  转成 typed `RuntimeFactsTurn`，此后与工具结果按时间顺序只追加。这样第二轮及以后保留逐字节相同的旧
  system/tools/messages 前缀，供应商可以复用缓存，同时后续任务不会因隐藏裁剪而丢掉上一轮工具事实。
- 慢模型的正文、思考与工具参数流按配置间隔投影无正文 heartbeat，provider transport 的结构化退避也写
  当前 child 状态。它们只改变 TUI/Web 可观察性，不取得停止、完成、重试或权限裁决权；正常持续 SSE 没有
  固定总墙钟，仍由 first-event、rolling idle、显式停止和 provider 终态收口。
- 所有 carried/live/thread Compact 摘要调用现在通过 `auxiliary_call.py` 进入唯一 `ModelCallLedger`、HTTP
  attempt observer、全局并发闸和成本指标。删除摘要层额外 daemon-thread deadline：该旧计时器无法取消
  已经发出的 HTTP，请求超时后会在后台继续消耗额度，并让慢模型摘要永远被丢弃。现在辅助调用只服从统一
  provider 首包/流空闲/请求超时，异常或 provider timeout 再回退机械摘要。
- `.10` fresh `ma-110-c8a2ece-compact-r1` 已证明 child 的 canonical generation 1 不会关闭 active attempt：
  工具运行时 child 在同一 exact attempt 的 Compact checkpoint 之后继续成功执行验证命令并自然 DONE；这条
  真机证据与 focused 回归共同关闭旧 `TOOL_AUTHORITY_CONTEXT_MISSING` 缺口。`.7` 的本地慢模型样本最终
  持续 8 小时以上，跨过旧 600 秒墙钟但长期无有效推进；用户明确要求停止后，经 TUI `/stop` 让 5 名活动
  child 同步进入停止终态，派发进程退出，Gateway 恢复唯一 MiniMax-M2.7 配置。以后真实 TUI、会话运行时 与
  终端交互 对照统一使用 MiniMax-M2.7；慢模型数据只保留为超慢传输/活性失败样本，不再作为持续验收矩阵。
- `work/state.json.updated_at` 固定为 Unix 秒浮点数；`activated_at/finished_at` 继续使用 ISO 字符串。父级与
  child 状态投影不再把同一字段在两种类型间来回覆盖，跨进程 heartbeat/排序可以使用一个稳定合同。
- 新型号只进入 `docs/architecture/MODEL_CATALOG_SNAPSHOT.md` 的带日期参考快照。通道运行时 最新目录再次证明
  endpoint/计划差异会改变可用型号、thinking 映射和 context；本轮不把它们升级为 runtime allowlist，
  未知 provider 型号继续透传，后续再单独设计“用户配置 > 实时发现 > 版本种子”的声明式目录。

## 2026-08-28 后台工作片的会话权威、可见回执与有界 handoff【状态：本地 focused 已通过，`.10` 真 TUI 待验】

- 对照 会话运行时 `session/turn.rs` 的统一 active-turn/TurnComplete 事件边界和 终端交互 的单一 reactive message
  投影：后台 child wake、observation 与 progress policy 都必须经同一 report→notice 出口。过去只有 child
  wake 写 notice，定时续作已经把最终 assistant 正文提交到 ConversationStore，附着 TUI 却永远收不到；
  当前三个执行车道统一发布，suppressed/空正文仍由既有 notice 合同过滤。
- `Agent.run(save=False)` 在后台只表示“最终回复由 `_commit_background_response` 保存，避免重复写一遍旧
  memory/archive 路径”，不表示该工作片没有 ConversationThread 权威。拥有 exact task/thread 的后台片显式
  带 `conversation_transcript_authoritative=true`，与前台一样可在同一 CAS/checkpoint 上提交 live-tool
  Compact；孤立表达、展示和辅助调用仍不能落盘或推进 generation。
- carried tool archive 继续只传结构化调用事实。大 `write_file.content` 复用 live-prompt reducer，保留路径、
  模式、长度、hash 与短 preview，不把数万字正文复制进每个工作片；工具索引超过预算时保留有界头部和最新
  尾部，并用显式中段省略计数连接，避免最终 write/status 被旧 read 挤掉。
- transcript Compact 和 live-tool Compact 共用 `conversation_compaction_progress.v1` 展示协议。后者从开始调用
  可能很慢的摘要模型前即发布稳定 generation/block id，只在真实的准备、摘要、重新计量、checkpoint、CAS
  与完成边界推进 5/20/65/82/92/100；TUI 在阶段间只转 spinner，不按墙钟虚构百分比。失败会恢复原 IR 并
  明确结束活动块，进度 callback 断开不能影响 checkpoint 或 CAS。
- `Working` 是整棵任务树的活动投影，不等价于“主模型请求正在进行”。普通用户输入若命中 live foreground
  turn，会在当前 provider 调用后的最近安全点作为 typed steer 注入；父代理已让出等 child 时则开启新的
  foreground slice；若后台正整合一个事件，只等待该片释放共享 conversation lane，不等待所有 child 终态。
  后续 TUI 应把“主代理工作中”和“主代理等待子代理”分相展示，数据必须来自 active turn/run claim 与直属
  child 状态，不能从动画或文案猜测。

## 2026-08-28 Owner WorkspaceOnly、管理员 Full Access 与 Compact 恢复余量【状态：focused 与 `.7` MiniMax 真 TUI 已通过】

- 所有 owner（包括本地管理员）的默认工作区统一为自己的 owner home，`workspace_root` 留空不再继承进程
  启动 cwd。普通/远程 owner 无法自行开启 Full Access；只有结构化 `local/main` 与配置
  `access_mode=full-access` 同时成立才解除 owner 墙。文件系统隔离与外网能力是两条独立合同。
- Full Access 仍以 owner home 为默认行为锚点。用户明确指定外部路径或要求系统排障时可以离开；访问其他
  owner 还要由用户明确点名，并默认先只读、尽量少改。该规则只进入 prompt 软提示，绝不解析正文授予权限。
- owner home 中普通用户文件可读写，宿主权限、配额、保留策略、运行账本、Compact 与审计路径保持只读。
  Full 主代理创建 child/grandchild 时重新恢复 owner 墙，并只保留内部 task 根和父级明确分配的
  `product_write_roots`；子代理 cwd 使用用户项目根，不能被内部状态目录覆盖。
- 单 Gateway 的共享工具 handler 不再在调用间原地改 workspace、owner 或私网字段；每次调用使用独立权限
  快照与 request-local handler。这样并发 TUI/owner 不会因时序互相串目录或网络授权。
- Conversation Compact 的有效候选必须低于 `trigger_tokens - recent_tail_tokens`，为下一段近期对话留下完整
  恢复余量，避免压缩抖动。每次 live/transcript 尝试使用独立 operation id，因此 live 失败不会吞掉后备
  transcript 进度；child 把同一 typed 进度写入自己的公开 transcript。手动 `/compact` 从耐久 outbox 入队
  到真实回执期间保持诚实的转圈块，只有 canonical 成功/失败回执才能结束动画和推进 generation。
- live-tool 候选无法达到恢复线但原 IR 已完整恢复时，属于 `superseded/candidate_discarded`，不是 Compact
  失败：它不推进 generation、不写失败熔断，也不在 TUI 冻结红字；同代 transcript Compact 可立即接管。
  只有摘要 transport、checkpoint 或 CAS 等真实异常才发布 `failed` 并留下失败证据。
- 无摘要的普通 IR 窗口仍至少保留最新完整 ToolCall/ToolResult；完整 live Compact 摘要已经覆盖本轮全部工具
  历史后，最后一对也不再是机械保留项。若单条最新大回执妨碍达到恢复线，应将其成对移除，由摘要续接、
  owner archive/ref 保留精确原文；这与 会话运行时 用 ContextCompaction 替换完整旧 history 的边界一致。
- Compact 模型调用异常与“请求正常完成但正文为空”必须分开：异常仍回滚并进入统一失败熔断；正常空正文不
  重试、不打断 active turn，而从 typed IR（transcript 路径则从 raw row + operation evidence）生成有界机械
  续接摘要再提交。该摘要只帮助模型续跑，不参与完成、权限或副作用裁决；用量账仍保留这次真实空输出调用。
  该边界对照 会话运行时 `compact.rs`：completed compaction turn 即使 assistant suffix 为空，也仍生成 replacement
  summary item，而不是把整个用户任务转成 `COMPACT_EMPTY_SUMMARY`。
- `.7` 唯一 Gateway 真 TUI 从 `/root` 启动仍落 local/main owner home；官方超级玛丽任务 5/5 child DONE，
  主 thread generation 1、最长 child generation 2，最终回复直显。主 thread provider 账为 26 次物理调用、
  323,871 ordinary input、813,531 cache-read、167,556 cache-write、8,766 output，零 provider retry/failure。
- `422dd7a` fresh `.7` 官方超级玛丽长任务再次 4/4 child DONE、5/5 Todo 完成并直接汇报；主线程手动
  Compact generation 1。自动 live-tool 候选在约 83% 时因恢复余量不足完整回滚，旧 TUI 错画两次红色
  “压缩失败”，但 canonical failure count 为 0 且任务正常完成；上述 `superseded` 状态就是该真样本的根因。
- `f6d58c0` fresh `.7` 用同一官方提示再次验收：从 `/root` 启动约 1.4 秒进入 local/main owner home，8/8
  child 自然 DONE、Todo 10/10、最终正文直显。live 候选 20% 后静默回到工作，没有红字；随后真正 Compact
  提交 generation 1，Context 回落后模型仍准确续接 8 名 child、文件清单和验证步骤。canonical failure
  count 为 0、source tool pairs 为 8；主 thread 的 26 次 MiniMax-M2.7 provider 调用全部成功、零重试。

## 2026-08-28 运行时旧机器验收与内部 re-export 退役【状态：代码与 fresh 真 TUI 已验】

- 对照 会话运行时 `会话运行时-rs/core/src/session/turn.rs` 与 `会话运行时-rs/protocol/src/protocol.rs`：模型不再请求工具且
  没有待处理输入时，自然结束当前 turn 并产出最后一条 assistant message；执行错误、停止和工具副作用
  仍由结构化事件收口，不再另设一套业务质量验收数据库决定“能否回复用户”。
- 本项目中 `agent/acceptance/`、`RuntimeAcceptanceMixin`、`closeout_task_run()`、
  `acceptance_contracts/validator_operations` 经生产调用检索均为零消费；它们与现行自然完成主链重复，且会把
  TaskRun、业务验收和最终消息投递绑成第二套硬门。本轮物理删除实现、只服务它们的测试与状态常量。
- `Task` 只保存可跨多轮复用的 owner/thread/goal 身份；停止、完成、失败、恢复只属于
  `TaskRun/AgentRun/AgentAttempt`。新库不再创建 `tasks.status`、`task_runs.current_contract_id` 或旧验收表。
  存量 SQLite 的多余列/表不参与读写，但升级时不自动 DROP，避免破坏用户历史与回滚证据。
- 安全收口没有删除：路径/owner 权限、危险命令、审批、工具执行结果、UNKNOWN 副作用、资源锁、发布事实、
  outbox/inbox 幂等仍按结构化合同硬守；删掉的只是任务内容质量和最终回复的第二套机器判官。
- `agent_core/__init__.py` 改为无副作用包入口，`agent/core.py` 从权威实现模块直接导入；同时删除无人消费的
  私有 helper re-export。这样导入 `conversation.agent_thread` 不会提前装载整套 subagent runtime，消除了
  过去靠偶然导入顺序遮住的循环依赖。
- 真 TUI `gwreq-1787921095-5775e23884254defb09f0dbafc7ef1ef` 又定位出剩余机器判官：用户明确要求
  `exit 7` 并如实汇报，模型已得到正确 stdout/stderr/return code，却被 `completion_conflict` 连续打回，
  共 5 次物理模型调用、42.6 秒、141,905 cache-read tokens，最后还把 task 留在 Waiting/Working。
- 当前进一步删除 `plan_closeout.py`、completion conflict/prior unresolved/stale verification 自动 followup、
  `task_progress_closeout_repair_attempts` 和 `OPERATION_INCOMPLETE` 自动续跑兼容。模型仍在每个工具结果后正常
  获得下一次采样，可自主换方法；一旦给出 plain final，宿主只保存 operation/Todo 审计事实，不覆盖正文、
  不再调用模型。UNKNOWN 副作用、owner/path 权限、危险操作、取消和幂等安全门保持原位。
- fresh TUI `ma-cleanup-natural-final-r4` 原样复验 `gwreq-1787922555-f7d04fed2d02409485791dc4aa17193e`：
  MiniMax-M2.7 只执行一次 `exit 7`，operation verification 仍为 `failed`，但模型第二次采样直接如实回复；
  2 次物理调用、1 工具轮、0 provider retry、11.476 秒，runtime `ok`、turn `completed`，TUI 立即清除
  Working。这同时证明自然结束没有吞掉失败事实，也没有放宽工具执行或权限边界。

## 2026-08-28 TUI 补充消息可见性与单次终稿【状态：focused 与 fresh 真 TUI 已验】

- 对照 终端交互 `src/utils/messageQueueManager.ts` 与
  `src/components/PromptInput/PromptInputQueuedCommands.tsx`：用户在运行中提交的内容先进入有稳定身份的可见队列，
  后续消费只改变同一项状态；流式重绘使用稳定 message identity，不能因为回执迟到再造一条最终消息。
- my-agent 的显式 `/btw` 原本已有 exact-turn、持久 outbox 和三态回执，但没有投影成 pending user row；Gateway
  `active_turn_input_consumed` 又在 provider delta 后到达客户端，却调用了仅适用于本地采样前边界的 assistant
  切段入口，导致 summary 把相同 response 新建第二块。本轮保留所有投递/幂等事实，只修投影：提交即显示、
  accepted 原位晋升、rejected 撤下并说明、late consumed 不切正文块。
- Gateway 的 consumed 行可能晚于 `assistant_final`；因此“原位晋升”必须保留用户本地提交 seq，并只在同一个
  request 内插到提交后才开始的思考/工具/终稿之前。不能按网络回执到达时间 append，也不能全局重排旧历史。
- 同轮将 slash 目录中文化、补齐 `/audit resume`，并让共享 `/expand` parser 实现帮助中已经承诺的显式
  `last`。这些改动不进入模型 prompt，不改变会话保存、Compact、权限或 Gateway 控制语义。
- `.7` 的 `ma-cleanup-steer-order-r7` 在真实 15 秒前台工具中插入补充：排队态立即显示中文，消费后用户行位于
  第二次思考与唯一终稿之前；空闲 `/btw` 明确中文拒绝。客户端只改变投影，Gateway/模型输入继续使用原消息 ID。

## 2026-08-28 Rich TUI `/context` 与底栏单一快照【状态：focused 与 fresh 真 TUI 已验】

- 对照 会话运行时 `chatwidget/status_controls.rs`：详细 status 和底栏都读取同一个 `ChatWidget.token_info`，而不是一个
  读服务端通知、另一个重新估算历史。my-agent rich TUI 现在同样只展开 active `TuiRuntime.status.context_usage`。
- 这份 `model_visible_context_usage.v1` 来自真实 provider preflight，并与 Compact 使用同一 window/trigger；详情只
  把总量、固定与本轮提示、对话与工具消息、待注入运行说明、工具定义换成中文说明，不读取或复制任何正文。
- `/context` 是客户端只读显示，不建 control outbox、不请求模型、不修改会话。Compact boundary 后旧快照会被
  清除，界面明确等待下一次真实调用刷新，避免把压缩前数据继续当当前事实。
- `.7` `ma-cleanup-context-r8` 同屏实证：详情 `30,563/128,000（23.9%）`，底栏
  `~30.6k/128.0k · 24%`，压缩点 90%、compact 0 一致。

## 2026-08-28 Owner 磁盘配额退出默认热路径与 TUI 记忆异步回执【状态：focused 已通过，`.7` 真 TUI 复验中】

- 用户自己的 owner home 默认不设磁盘容量上限；`quota.v2.max_disk_mb=0` 是结构化“不限制”，因此 Memory、
  Persona、Scheduler 和文件工具的普通写入不再为每个文件动作遍历整个 owner。只有管理员显式配置非零值
  时才启用原有精确扫描门；损坏 quota policy 继续 fail-closed。
- `ensure_my_agent_home` 只迁移字段完全等于旧 `quota.v1` 默认模板的 100GB 策略；任何管理员改过的数值或
  字段都保留。父 owner 有非零上限而 child 写 0 时，child 继承父上限，不能借“不限制”扩大权限。
- 对照 会话运行时/终端交互 没有“每次普通写入都全 workspace 扫盘”的热路径；规模部署若需要硬容量隔离，应
  使用 filesystem/project/container quota。应用层可选门只提供提前拒绝和结构化错误，不冒充 OS 硬配额。
- Gateway TUI 的 `/memory`、`/remember` 改在后台线程调用同一 slash dispatcher，慢磁盘/Gateway 不再冻结
  输入循环。丢失 `/remember` HTTP 响应属于 `MEMORY_WRITE_RESULT_UNKNOWN`：界面明确提示可能已提交且不
  自动重复写，不再把未知副作用谎报成失败。

## 2026-08-28 Gateway 权威状态工具与表达轮拒绝回注【状态：focused 与 `.7` 真 TUI 已验】

- 当前唯一 Gateway 的身份不能由模型从 `ps/ss`、常见端口或 `/health` 猜。`gateway_status` 直接复用
  validated PID record、state、heartbeat 和 hot queue；state 在每次进程启动时持久化实际 config path、
  model、bind host、port 与日志生命周期字节偏移。工具只在本机 `main_agent` registry 注册，远程 user/group
  owner 不获得宿主 PID、配置和日志路径。
- `identity` 是模型名、PID、process birth token 和配置来源的唯一结构化位置，并排在工具结果开头；这些值
  可以原样复述但不能参与任务完成、权限或恢复裁决。r12 暴露 MiniMax 把 M2.7 抄成 M2.2 后，没有增加机器
  判官，而是缩短、去重并前置权威身份；r13 已逐字复述 `MiniMax-M2.7 / 8420 / running`。
- 日志诊断只读取状态中记录的本次启动 byte offset 后最多 64 KiB，并仅返回异常类型计数、行数和覆盖范围，
  不把原始日志、请求正文或 secret 放进模型上下文。零新增字节明确为 `no_new_log_bytes`，不能冒充 quiet；
  只有实际观察到当前生命周期字节且无异常签名才是 `quiet`。
- 对照 会话运行时 `tools/registry.rs` 与 `tools/events.rs` 的 `FunctionCallError::RespondToModel`：表达专用自然回复轮
  如果仍产生 tool_use，宿主不执行 handler，而把 AssistantTurn 与成对
  `TOOL_CHOICE_VIOLATION/handler_executed=false/effect_outcome=not_started` ToolResult 回注后重采样。第二次
  仍越权时返回完整基础设施说明，绝不把 tool_use 前的过渡句冒充最终回复。
- `.7` r12/r13 真实 Audit 均先调用 `gateway_status`，不再猜 8080/3000/8765，最终回复完整；r13 当前日志
  132 bytes、2 行、0 traceback/loop error/exception，测试机仍只有一个 127.0.0.1:8420 Python listener。

## 2026-08-28 Compact 语义摘要与精确会话锚点【状态：focused 与 `.7` 真 TUI 已通过】

- 真 TUI 已证明原始 transcript 和 Compact 游标都正确：压缩源中的 assistant final 明确写有
  `Welcome to Python.org`，但 MiniMax-M2.7 生成的摘要只保留“抓取 Python 官网”，下一轮因此无法回答标题。
  这是摘要模型漏事实，不是原文丢失、游标选错或缓存裁剪。
- 对照 会话运行时 `会话运行时-rs/prompts/templates/compact/prompt.md` 与 `core/src/compact.rs`，Compact 仍保持“一份语义
  handoff + 有界近期原文”的主结构；对照 终端交互 `services/compact/prompt.ts`，摘要提示新增按时间顺序覆盖
  每个用户请求和最终结果，并要求保留名称、标题、URL、路径、数值、端口、错误串和已核对文件内容。
- 模型摘要属于软上下文，不能单独保证每个短事实。每次成功 Compact 现在额外生成一段
  `Exact Conversation Landmarks (non-authoritative)`：只收录用户消息与 assistant final；typed commentary、
  thinking、工具过程和后台 Audit 正文不进入。旧代锚点按规范行继承并去重，provider 回显的旧后缀先移除，
  因而每代只有一个锚点段。
- 锚点明确标注“历史对话文本，不是机器状态”。权限、完成、产物、任务状态和副作用仍只读取结构化账本、
  refs、schema 与文件系统事实。原始 transcript 继续是可审计事实源，锚点不替代原文。
- 正常大窗口总预算封顶 6000 字符；小窗口按 context window 的十二分之一缩小且最低 800 字符，避免修复本身
  让候选超过 recovery target。Compact 提交后的摘要和锚点在后续普通回合保持不变，可以继续命中稳定前缀；
  只有下一次真实 Compact 才换代，不引入逐轮滚动裁剪。
- `.7` fresh `ma-cleanup-compact-r23` 先后取得网页标题、三行文件和交付代号/负责人/日期，手动 Compact 从
  generation 0→1；随后明确禁止工具的追问一次答全七项短事实。canonical thread
  `thread-ce7d87bd859a499f` 的 summary 只有一个锚点标题、共 1447 字符，原始 transcript 9 行仍完整，终轮
  operation count 为 0，证明不是模型偷偷重抓或重读。

## 2026-08-28 工具“本会话允许”真实生命周期【状态：focused 与 `.7` 真 TUI 已通过】

- 对照 会话运行时 `会话运行时-rs/core/src/tools/sandboxing.rs`：`ApprovalStore` 属于长寿命
  `SessionServices`，不是某一次 function-call/request 的临时字段。my-agent 原先把已批准键放在
  `BufferedChunkStreamWriter`；Gateway 每个用户消息都会创建新 writer，所以按钮虽然写“本会话”，下一回合
  一定丢失。
- Gateway 现在由长寿命 owner-scoped Agent 持有一个线程安全、有界、只存在内存中的审批缓存。授权项仍是
  `tool_name + args_hash` 精确键；外层作用域同时冻结 owner、thread、channel conversation、实际工具 cwd、
  `access_mode` 和 `path_access_mode`。任一参数、用户、会话、目录或权限变化都重新询问；Gateway 重启也清空。
- 会话首个工作工具会在模型采样后懒创建 task workspace，因此 cwd 不能在请求开始时冻结。审批入口在真正
  等待用户时从 thread-local canonical run workspace 取目录；这样第一回合批准与后续 sticky task 回合使用
  同一个作用域，同时不牺牲“换目录重新询问”的安全边界。
- 缓存最多保留 256 个作用域、每作用域 128 个精确调用，LRU 淘汰只会多弹一次授权，不会扩大权限。命中时
  不再发布 `permission_requested`，避免界面闪一下已自动通过的审批框；工具执行与 operation ledger 仍照常
  记录真实调用。
- `.7` fresh `ma-cleanup-approval-r26` 实证：首次 18476 命令选择“本会话允许同一操作”；下一回合同一
  args hash 只发布 `permission_resolved/session_cached=true` 并直接执行；改为 18477 后重新出现全中文授权框，
  Esc 取消且未执行。测试期间仍只有 PID 1590194 监听 127.0.0.1:8420。

## 2026-08-29 后台命令启动观察期【状态：focused 与 `.7` 真 TUI 已通过】

- `.7` r26 暴露一个独立底层缺口：18478 已被第一个受管 HTTP 服务占用时，第二个 managed host 仍能先生成
  新 `session_id`，旧 `run_command` 随即返回 `status=started`；真实子进程稍后才以端口冲突退出，模型因而
  错报“服务已启动”。这不是模型完成判断问题，而是工具把“记录创建成功”冒充“启动观察成功”。
- 对照 会话运行时 `会话运行时-rs/core/src/unified_exec/process_manager.rs`：首次 exec 在有界 yield 内收集输出；短命令
  直接返回 `exit_code` 且不保留 live process id，只有观察期后仍存活才返回可续接进程。本项目保留既有
  detached host 和 durable session，增加 0.5 秒 canonical `process_registry.wait` 启动观察，不另造状态源。
- 观察期后仍为 running 才返回 `status=started/session_id`；期内零退出返回成功的 `status=exited`，非零退出
  返回 `COMMAND_FAILED/effect_outcome=failed`，两者都带 `exit_code/output_tail`。这只回答进程是否仍活着，
  不把存活推断成端口已监听或局域网可达；后者仍由 `process_session network_status` 和外部探针裁决。
- `.7` fresh `ma-cleanup-background-r27`：首次请求 `gwreq-1787946133-...` 启动 18478；完全相同参数的第二次
  请求 `gwreq-1787946178-...` 命中 session approval，但在约 1 秒工具进度内返回
  `COMMAND_FAILED`，最终逐字报告 `Address already in use` 与 `exit_code=1`。相关后台进程均从所属 TUI
  清理，单 Gateway 拓扑未变。

## 2026-08-29 PTY 会话列举、真实 resize 与会话隔离【状态：focused 与 `.7` fresh 已通过】

- `.7` `ma-cleanup-terminal-r28` 用普通中文要求完成 REPL 读写、100×30 调整、列举和停止。旧 Schema 只有
  `start/write/read/close`：模型先编造 `list` action 被参数门拒绝，又向 REPL 写入
  `ESC[8;30;100t`，工具只确认字节写入；内核随后仍报告 80×24，模型却把“已发送”写成成功。这是 PTY
  控制合同缺口，不能靠提示模型谨慎来修。
- 对照 会话运行时 app-server `process/resizePty` 与 `会话运行时_utils_pty::resize_raw_pty`：窗口调整走
  `TIOCSWINSZ`，并由 connection-scoped process handle 选中会话。本项目在唯一 `terminal_session` 增加
  `list/resize`，resize 接受结构化 `columns/rows`，调用内核 ioctl 后回读实际尺寸；ESC 文本不再冒充尺寸事实。
- PTY scope 继续冻结 owner home、读写根和受保护路径，并新增 executor 注入的可信 conversation id；列表、
  读写、resize、close 都要求 exact scope，相同 session id 不能跨用户、跨 TUI 会话接管。模型可见结果只保留
  `pty-...` 稳定句柄，不再暴露 OS PID；list 上限继承进程内 32 session 硬上限。
- start 仍是唯一新建进程/审批边界；既有 session 的 list/read/write/resize/close 继续按同一 transport
  生命周期执行，不重复制造命令审批。Windows ConPTY 仍不在当前支持范围内。
- `.7` fresh `ma-cleanup-terminal-r29` 用与 r28 完全相同的自然语言请求和 MiniMax-M2.7：单次 start 审批后，
  6 个工具轮依次完成写、增量读、结构化 resize+list 和 close；list 返回唯一 `python3` 会话、100×30、
  running，最终只引用 `pty-1-1787947404`，没有 OS PID 或参数失败。请求为
  `gwreq-1787947377-1b918742122e4a0baf68204faead62b4`，测试时唯一 Gateway PID 1595234。

## 2026-08-29 TUI 中断终态中文投影【状态：focused 与 `.7` fresh 已通过】

- `.7` `ma-cleanup-terminal-r29` 在真实 `sleep 30` 工具运行中提交 `/stop`，请求在 30 秒前进入 interrupted，
  输入框恢复且宿主无残留 sleep；但 transcript 固定显示英文
  `Interrupted · What should my-agent do instead?`，展开的工具拒绝/中断 fallback 也仍是英文。
- 中断权威仍是 typed `turn_interrupted/CANCELLED`，本轮只把三个用户可见投影改为
  `已中断 · 接下来希望 my-agent 怎么做？`、`用户拒绝了工具调用` 和 `已中断`。不解析模型正文，不改变
  `/stop`、Esc、cancellation token、工具 operation reconciliation 或 interrupted transcript 的排序/样式。
- 对照 终端交互 `InterruptedByUser.tsx` 的专用 dim transcript row 与 会话运行时
  `chatwidget/input_restore.rs` 的 typed interrupted notice：继续保留专用灰色行，不把中断伪装成 assistant final；
  中文化只适配本项目用户界面规范。
- `.7` fresh `ma-cleanup-stop-r30` 在 MiniMax-M2.7 已发起真实 `sleep 30` 后提交 `/stop`，界面立即显示
  `已中断 · 接下来希望 my-agent 怎么做？` 并恢复输入；宿主无残留 sleep，唯一 Gateway PID 1595234 未变。
  请求为 `gwreq-1787947950-297362d6af4f46d58bbb9b7c3cf29489`。

## 2026-08-29 持久 `/goal` 控制面真 TUI 复验【状态：`.7` 同会话通过】

- `/goal` 继续只是当前 thread 上的显式持久目标 overlay，不是普通任务的前置模式，也不创建第二份用户会话。
  创建、查询、暂停、修改、恢复和清除都读取同一 typed goal 状态；控制命令不依赖模型正文，也不等待当前
  后台回合自然结束。
- `.7` `ma-cleanup-stop-r30` 对 `tui-r31` 完成全生命周期：运行中查询立即返回，pause 终止 Working 投影，
  edit 保留目标身份并更新正文，resume 恢复，具名 clear 后再次查询为“当前没有持续目标”。这证明用户可在
  主代理后台等待时立即控制它，而不是等子代理或慢模型全部返回。
- 本轮只有验证和文档登记，没有新增 goal 兼容入口、自然语言状态解析或普通任务自动续跑；权限、task link
  和目标身份仍以既有结构化状态为唯一事实源。

## 2026-08-29 子代理 owner workspace 写权限祖先冲突【状态：focused 与 `.7` fresh 八路任务已通过】

- `.7` `ma-cleanup-subagents-r31` 的八路真实调研首次暴露：每个 child 都有结构化
  `allowed_write_roots`，但默认 `forbidden_write_roots` 同时含宿主 `Path.home()`。测试机 owner workspace 位于
  `/root/.my-agent/...`，bubblewrap 按安全顺序在可写根后重挂 deny，因而 `/root` 把所有合法 child/task 根
  一并变成只读；模型只能把 Git 源码转存到沙箱 `/tmp`，无法写正式报告。
- 对照 会话运行时 `linux-sandbox/src/bwrap.rs::create_filesystem_args`：写权限由显式 writable roots 正向授予，
  只读 carveout 应是 writable root 里的更具体敏感子路径；不能用一个更宽祖先否定全部授权子根。本项目继续
  由 `allowed_write_roots` 守总边界，默认 deny 删除冗余的宿主 home 祖先，只保留 `Desktop`、`Downloads`、
  `.ssh`。因此不会开放 owner workspace 之外的路径，敏感子目录仍在所有 writable bind 之后重挂只读。
- 这是权限合同生成修复，不按任务文本猜目录，也不降低 remote owner、非管理员或 workspace-only 的上界。
  现有已经持久化的错误 child 合同不就地篡改；部署后必须新建真实 TUI/child 复验自己的任务目录可写、
  `.ssh` 仍不可写、父级目录仍按结构化 write roots 决定。
- `.7` `ma-cleanup-subagent-write-r32` 使用用户给定的 Prompt 3 新建 8 个 child；每个 child 都能在自己的
  canonical task/agent workspace clone、读代码和写报告，没有再出现 `Read-only file system` 或转写 `/tmp`。
  child 插话还明确回报 `.ssh` 为禁止根并继续工作。相关策略本地 178 项、测试机 182 项 focused 通过。

## 2026-08-29 嵌套 task root 的路径重定向幂等【状态：已部署，`.7` fresh 长任务复验中】

- r32 又锁定一条与权限无关的路径错误：模型原始工具参数是 owner 根下的
  `research/pi_report.md`，第一次从 owner 根重定向到正式 task root 完全正确；同一 admitted call 在后续
  预处理/归档再经过转换时，因为正式 task root 本身位于 owner 根之下，旧字符串替换又在它前面加了一层
  task root，形成 `<task>/tasks/<date>/<task>/research/...`。
- 路径重定向现在是幂等变换：扫描每个完整 source 前缀时，先按路径/文本边界识别当前位置是否已经是精确
  target 或其后代；已转换片段保持原样，尚未转换片段才替换。它支持同一段 goal 中多个独立路径，不会把
  `task-1-copy` 之类相似名称错当 target。
- 这仍是 host-validated source/target 的结构化适配，不从 prompt 猜任务名，不改 allowed roots，也不把其它
  task 的路径自动映射到当前 task。32 项 tool-round focused 已通过；r33 使用同一 Prompt 3 检查真实 child
  goal/output 和 main 回读只出现一层 task root。

## 2026-08-29 缺失路径候选的有界快速失败【状态：已部署，`.7` fresh 长任务复验中】

- r32 的 8 个 child 已全部终态，main 整合时并行读取 5 份预想报告，其中两个文件名不存在；Gateway 随后
  22 分钟停在 `正在使用 read_file`。`py-spy` 证明两个工具线程都在
  `filesystem_path_recovery._walk_candidate_items -> os.walk`，各自递归扫描含 8 个完整 Git 仓库的 task 树，
  父轮又等待整个 future 批次。这不是 provider 慢、模型没回复或 workspace 锁。
- 对照 会话运行时 app-server：普通 `fs/readFile` 直接返回文件系统错误；模糊文件搜索是单独、可取消的能力，不能
  隐式绑定到每个读取失败。本项目保留“至多 5 个安全候选”的友好差异，但把它降为严格 best-effort：父目录
  探测与所有 workspace roots 共用 1024 个目录项、128 个目录预算；单目录最多 256 项、深度最多 8；遍历改为
  广度优先，不会深陷第一个大型仓库，也不跟随目录符号链接。
- 预算耗尽或没有候选时立即返回 `PATH_NOT_FOUND`，明确让模型调用 `list_files/search_text`。显式工具仍承担
  完整搜索，自动恢复不承诺穷举，因此减少的是隐式猜测覆盖率，不是文件、历史或权限。82 项本地/测试机
  focused 已通过；r33 继续用真实长任务核对 Gateway 无工具线程长挂。

## 2026-08-29 零调用旧判官与孤立投影物理清理【状态：实现已删除，严格 gate 待跑】

- 合并 8 个历史 Gateway coverage data 后，先按入口和全仓 import 反查区分“当前 TUI 不会走但仍属正式通道”
  与“任何生产入口都没有调用者”。Feishu/WeCom/Windows/迁移/管理 CLI 等前者继续保留，不能拿 TUI 0%
  当删除依据。
- 后者共 7 个实现：旧 `acceptance_contract` 机器判官、944 行 `target_coverage_ledger` 内容覆盖判官、无人调用的
  `subagent_outputs` 产物判官、`recovery_batches` 旧恢复批次、`progress_fingerprint` 旧后台指纹、旧
  `dispatch/progress_payload` 子代理投影，以及只验证目标覆盖判官自身的 769 行测试。它们全仓只有自测引用，
  与当前自然完成、typed child lifecycle、TUI activity 和显式恢复工具重复，现已物理删除。
- `acceptance_contract` 的测试引用和 `contract_layers` 分类同步删除；用户要求、约束和验收标准仍可作为模型
  上下文，artifact 格式可打开、owner/path/危险命令/审批/UNKNOWN 副作用等客观安全门也继续保留。删除的是
  无消费者的第二判断链，不是安全或真实工具事实。
- 过期文档不再声称 runtime 使用 `subagent_outputs.py/progress_fingerprint.py`；code-size baseline 也移除已删除
  测试的三条历史项。下一步由 focused、一次全仓 pytest、严格代码尺寸/文档/打包 gate 和 `.7` r33 真 TUI
  共同证明没有隐藏动态导入。

## 2026-08-29 通道控制与共享合同依赖归位【状态：本地 focused 通过，严格 gate 待跑】

- `gateway_parts`、`subagents` 和 `memory_archive` 不再反向导入 `agent_core` 或彼此的业务实现。代理查看/插话/
  停止归到 `conversation/agent_control.py`，child 工具审批归到
  `conversation/agent_tool_approval.py`，工具窗口归到 `conversation/tool_context_window.py`，Compact 辅助模型
  调用归到 `conversation/auxiliary_model_call.py`，完成信封版本归到中立
  `contracts/subagent_completion.py`。
- 不保留旧模块转发壳；所有生产调用和测试直接改读新权威入口。`scripts/check_import_boundaries.py` 从 11 个
  finding 降为 0，避免 Gateway/child/UI 今后再次通过反向依赖绑定具体执行层。
- 这些移动不改变 HTTP、TUI、审批、Compact 或 child 完成协议，只清理模块所有权。结构变化已同步
  `CODEBASE_TREE.md` 和 gateway/memory/subagent 结构文档。

## 2026-08-29 会话运行时 式同轮工具全量执行与批次削峰【状态：focused 与 `.7` r34 已通过】

- 真实 Prompt 3 在八个 child 全部完成后暴露：模型同一 assistant turn 提交 8 个报告读取，旧宿主按
  `background_max_tool_calls_per_round=4` 只执行前 4 个，并给后 4 个制造
  `TOOL_CALL_LIMIT_DEFERRED` 失败。MiniMax 根据这些假失败自然回复“下轮继续”，root 因而停止整合。
- 会话运行时 对照路径是 `会话运行时-rs/core/src/session/turn.rs::try_run_sampling_request/drain_in_flight` 与
  `会话运行时-rs/core/src/tools/parallel.rs::ToolCallRuntime`：流中每个已接收工具都进入 `FuturesOrdered`，并发安全
  只由运行 gate 控制；下一次采样前 drain 所有 in-flight 结果，不存在“超过并发数就伪造未执行失败”。
- 本项目据此把历史字段 `max_tool_calls_per_round` 收成“单次并发执行批大小”。正数只把一个 provider 工具轮
  分为连续小批，全部 admitted calls 仍在同轮执行并按 provider 顺序记录；0/空值不增加这个批大小限制。
  真实取消、审批拒绝、Compact 或耐久上下文切换仍可为尚未启动调用写各自客观 typed 结果。
- 等待直属 child 的根代理自然回执同时改为
  `runtime_status=unfinished / runtime_reason=SUBAGENTS_ACTIVE / turn_end=interrupted`；这只修正生命周期投影，
  不从正文判断完成，也不恢复已删除的机器质量验收器。
- `.7` fresh `ma-cleanup-batch-r34` 用原样 Prompt 3：8 个 child 全部 DONE 后，main 同一 provider 工具轮的
  8 次读取按 4+4 全部执行，没有 `TOOL_CALL_LIMIT_DEFERRED`，随后写出 13,563 bytes、9 节、220+ 行报告并
  自然回复。该轮也暴露了下一节单独收口的旧生命周期快照和终态 Todo 展示问题。

## 2026-08-29 provider safe point 的直属 child 当前快照与终态 TUI 收口【状态：focused 与 `.7` r35 已通过】

- r34 原样八路调研虽然已经证明同轮 8 次报告读取会按 4+4 全部执行，但 main 在 Compact/后台续片后仍短暂
  读取到启动时的 Recovery Snapshot；派生 `work/summaries/current_summary.md` 也可能停在 RUNNING。旧历史因此
  会让模型在 canonical 8 个 child 均 DONE 后再次怀疑数量或寻找过期路径。生命周期事实不能依赖摘要模型是否
  恰好保留，更不能解析 goal、标题、报告正文或目录推断。
- 对照 会话运行时 `core/src/agent/status.rs` 的 typed status 派生与 `core/src/agent/control.rs` 的 completion watcher，
  每次 provider safe point 现在从 canonical exact-parent child rows 原位刷新一份有界
  `[RUNTIME_DIRECT_CHILDREN]` JSON。快照只含 parent、总数、状态计数、all-terminal、非终态 ids 和每个 exact
  run id/status；其它 root、兄弟和孙级不会混入，部分读取失败时 `projection_complete=false` 且不得宣称全部
  终态。状态不变时序列化字节完全相同，因此不会为了“刷新”持续改写稳定缓存前缀。
- canonical task link 提交终态时，`work/state.json` 的 status/current_step 与派生
  `current_summary.md` 同步投影；Markdown 仍不是权威，写失败也不能反向改写 typed link/state。Compact、恢复或
  新工作片重新组 prompt 时会重建当前快照，历史 Recovery Snapshot 只能作为旧背景，不能覆盖新 typed facts。
- 没有显式 `covers` 时宿主继续不按标题猜 Todo 完成项；但 typed task link 已终态后，TUI 的当前
  display-plan projection 变为空，完整 durable task_progress 不删除，8 个 child roster 仍可导航。这解决最终
  回复后仍显示 `完成 0/9` 的假运行感，同时没有恢复机器验收或替模型打勾。
- typed assistant `process` 的全部 Markdown fragment 统一使用 muted style，final 仍用正文色；修复的是
  renderer 内层 fragment 覆盖外层灰色的问题，不延迟流式输出、不改 transcript 或模型请求。
- `.7` fresh `ma-cleanup-compact-tree-r35` 使用 MiniMax-M2.7 和原样 Prompt 3：child 数从 8→5→2→1→0 单调
  收敛；通道运行时 child 在 113.4k 后真实 Compact 到 63.7k、`compact 1` 并完成；main 明确列出 8 个 exact ids
  均 DONE，一次读取全部 8 份报告，写出 321 行/10,888 bytes 的横向报告并自然最终回复。终屏 Working、Todo
  均收起，8 个完成 child 行保留；canonical `work/state.json` 与 current summary 均为 DONE。

## 2026-08-29 effective workspace 是 durable runtime 唯一路径身份【状态：focused 与 `.7` r36 已通过】

- WorkspaceOnly 的进程 cwd、CLI 启动目录和 `SimpleAgent(root=...)` constructor 参数都不是 owner 身份或权限
  来源。它们可以帮助定位启动文件，但 prompt、文件/进程工具、Conversation、SubAgent、LocalStore 和
  Collaboration 必须共同消费权限裁决后的 `effective_workspace_root`；不能有一半使用 owner home、另一半
  仍对原始 root 做 workspace hash。
- 本轮发现的分叉正是用户看到“主代理找错子代理家”的底层原因：CLI 从一个 cwd 创建 Agent、内部 worker 或
  测试代码从另一个 cwd 直接构造时，旧实现生成两个
  `<owner>/workspace/runtime/workspaces/<scope-id>/`。两边各自都合法，却互相看不到 child、会话和工具账本，
  模型只能猜不存在的 `child_outputs` 或重新搜索。
- 对照代码已读到 会话运行时
  `会话运行时-rs/core/src/会话运行时_thread.rs::ThreadConfigSnapshot` 的权威 cwd/environment 快照，以及
  `会话运行时-rs/exec/src/lib.rs` 的 config cwd 解析。适配后的 `SimpleAgent` 先一次计算 effective workspace，再用
  同一值解析 durable runtime paths；直接构造与 CLI/Gateway worker 因此不再产生第二套运行家。
- 显式 absolute runtime override 仍然是结构化配置事实，Full Access 明确选择的外部 workspace 也继续形成独立
  scope；本改动不搬迁、不删除旧数据，不把自然语言目录名当身份，也不放宽 owner/path/sandbox 权限。
- `GATEWAY_WORKSPACE_INVALID` 同步进入唯一 error taxonomy，分类为不可原样重试的 path 错误，恢复动作是
  `FIX_PATH_WITHIN_ALLOWED_ROOTS`。客户端 cwd 是否允许仍由结构化 owner policy 判定，错误文案不参与授权。
- 专门回归用同一个 owner home、两个不同 constructor root 创建 Agent，固定 effective workspace、subagent、
  conversation 和 local store 三类路径完全相同；子代理 hierarchy/debug、Gateway request 与错误码全量注册
  focused 同时通过。真机 r36 从 `/root` 启动约 0.7 秒即显示 owner home 和 MiniMax-M2.7；首批 4 个 child
  与后续 integrator 全部落在 `main-d9283fde2e2d` 运行 scope 和同一 canonical task，5/5 自然 DONE。main
  只读取/核对 child 产物后直接 final，Working/Todo 收起而 roster 保留，全程没有旁路搬文件或补产物。
- r36 主 thread 最高可见 Context 约 92.3k，仍低于 128k×90%=115.2k 的 Compact 触发线，因此
  `compact 0` 正确；child 终态折叠后下一工作片显示约 52.5k 不是 Compact 丢历史，最终同一 thread 又随
  整合事实增长到 78.8k。任务 45 次 provider 调用合计 cache-read 1,003,935、cache-write 360,893、普通
  input 35,602、output 46,771，retry/failed/timed-out 均为 0，说明该路径统一没有破坏 MiniMax 缓存主链。

## 2026-08-29 子代理启动可观察阶段与详情首屏【状态：本地实现/focused 通过，真 TUI 待验】

- 对照 会话运行时 `core/src/agent/status.rs` 的 `PendingInit -> TurnStarted/Running` 与 终端交互
  `REPL.tsx` 在 API、teammate 和通知队列期间持续保留 spinner，子代理展示不能把“记录已创建”直接画成
  “正在运行”，也不能在完整 prompt/首个公开事件到达前清空载入提示。
- TUI/Web 共用投影只读结构化事实：`PLANNING=queued`、`PENDING=starting`、当前 attempt 已进入
  `RUNNING` 但还没有该 attempt 的公开 transcript event 时为 `waiting_first_event`，出现首个 typed event 后
  才是 `running`；终态、暂停和等待输入继续读 canonical status。阶段只用于展示，不改变调度、重试、完成或
  heartbeat 裁决。
- 进入 child 后，完整 `task.goal` 一旦可读立即成为第一条 user block；在 goal 和首事件都尚未到达时，详情页
  持续显示当前结构化启动阶段与 Working 行，不能出现无解释白屏。旧 attempt 的事件不能让新 attempt 冒充已
  开始输出，阶段判断必须同时绑定 exact run id 与 active attempt id。

## 2026-08-29 Owner 自主 Memory 与 SOUL 单独确认【状态：本地实现/focused 通过，真 TUI 待验】

- 每个用户自己的 Agent 自主维护自己的长期 Memory、Daily、Lesson/HOT、USER 与 AGENTS；这些写入不要求
  用户逐条确认。只有 `SOUL.md` 修改必须由该 owner 本人确认。`/audit` 的产品语义本轮保持不变。
- “自主”不等于跨权或信任自然语言：候选、正式记忆、Persona、索引、备份和恢复都必须从同一个结构化
  `owner_id` 解析到该 owner home，写入继续经过 scope、证据 refs、CAS、quota、注入扫描与路径墙；同一
  Gateway、同一模型或相似 subject key 都不能让 A owner 读取、合并、替换或删除 B owner 的记录。
- 非 SOUL 候选由 Curator/Promotion 主链自动推进；USER 仍只接受可回查的当前 owner 用户消息事实，工具事实
  仍需成功终态，Lesson/HOT 仍需独立证据次数，replace/remove 仍需 exact target id，冲突仍需模型在后续
  Curator 轮给出结构化修正，不能用正文猜覆盖目标。SOUL 候选单独保持确认状态并绑定确认时的 SHA/CAS。
- 该边界参考 长期助手 profile 的 `MEMORY.md/USER.md/SOUL.md/session` 全隔离与 通道运行时 每个 agent workspace
  的自动 memory flush，但保留本项目更严格的 owner 事实源和单一 Promotion/Persona 写入口，不增加直接文件
  写旁路、共享全局记忆或 IM 专用记忆库。

## 2026-08-29 同一 child 的结构化续跑可窄穿终态保护【状态：本地 focused 通过，真 TUI 待验】

- 普通保存继续把 DONE/CANCELLED/ABANDONED/TAKEN_OVER 视为单调终态，旧 runner 的心跳、工具结果和进度
  快照都不能把它写回运行态。只有宿主已经掌握 exact run id 的结构化同一任务续跑/生命周期纠错入口，才可
  显式设置 `allow_terminal_reactivation`；普通文字“继续”、模型口头状态或模糊标题都没有这项权限。
- capability_request 全部闭合且没有活 runner 时，原 run 可从旧版误写的 DONE 回到 PENDING；人工取消、
  ABANDONED 和 TAKEN_OVER 仍保持关闭。Audit 来源 worker 只有 `cancel_subagents.reason` 精确等于历史
  `conversation_lifecycle:audit_source_watch_complete` 且 watch ledger 已 settled 时，才可把旧误取消纠正为
  DONE/VERIFIED；管理员/人工取消不受影响。
- 工具执行前 validator 的宿主故障与非法返回分别登记为
  `TOOL_INVOCATION_VALIDATOR_FAILED` 和 `TOOL_INVOCATION_VALIDATOR_CONTRACT_BROKEN`，两者都表示 handler 未
  执行、不可把相同调用当普通参数错误盲重试，应保留结构化报码上报底座。

## 2026-08-29 在途递归派工与 `/stop` 共用 active-turn 取消边界【状态：根层真 TUI 通过；递归安全点待 fresh TUI】

- 对照 会话运行时 `会话运行时-rs/core/src/tasks/mod.rs::abort_all_tasks`：停止先取走 exact active turn 并触发当前执行体的
  cancellation token，随后等待耐久收尾；旧执行体不得在取消之后建立 continuation 或发布新的工作。my-agent
  保留 owner/thread/task 文件事实源，但不能让 `create_subagents` 在早期取消检查后继续晋升、绑定或启动 child。
- Gateway 的 task promotion 与 `/stop` 复用 exact turn transition lock。若该 turn 已关闭，工作工具在 handler 前
  返回 typed `CANCELLED/effect_outcome=not_started`，不能建立 `-continue-`；会话 child link 只做 canonical
  SubAgentTask 的投影，绑定后必须复读终态，迟到 bind 不得把 CANCELLED 复活成 active。
- 根代理和递归 child 共用 manager-owned `creation_guard` 与当前 Tool Gateway cancellation token。批量派工在
  每个 durable child 之间检查同一 token；停止后剩余项不再落盘，已经落盘的前缀保留 request/parent/root
  结构身份，由异步 stop reconciler 在 guard 释放后复读谱系并统一取消。取消回调不持有模型、runner 或用户审批，
  child 真正运行继续并行。
- r63 `.10` TUI `ma-matrix-r63-110-u19-stop-race-auto` 在真实 `create_subagents(items=8)` 后 1 秒收到 `/stop`：
  parent link 为 `interrupted`，8 个已落盘 child 的 canonical 状态和 conversation link 均为 cancelled，无
  `-continue-`、无 child 进程、主界面未复活 Working。该旧版本仍花约 53 秒等待整批落盘并逐项收口；r64
  增加逐项 cancellation safe point，需 Gateway 重启后的 fresh 根/递归 TUI 量化剩余延迟。

## 2026-08-29 本机 thin TUI 的真实 owner 身份与 scoped 派工【状态：focused 与 `.7` 单 Gateway 真 TUI 已通过】

- `channel=local` 不是天然等于共享管理员。只有结构化 `user_id=local-agent` 代表历史基础身份
  `local/main`；本机客户端显式携带的其它 user id 必须解析成 `local/user/<id>`，结构化群组则解析成
  `local/group/<chat_id>`。任务、会话、Memory、Persona、LocalStore、子代理运行目录和审计全部从这一个
  `OwnerIdentity` 派生，不能因请求来自 loopback HTTP 或同一 Gateway 就退回 `local/main`。
- scoped owner 的子代理自动启动不能复用只携带基础 `--config/--workspace-root` 的后台子进程入口。该进程会
  丢失请求时冻结的 owner，重新装配成 `local/main`，于是去错误目录找 run 并永久排队。精确
  `local/main` 继续使用 durable 子进程；所有其它 owner（包括 `local/user`、`local/group` 和外部 provider）
  使用持有当前 scoped Agent 的 Gateway 内 daemon dispatcher。任务、attempt、lease 与恢复仍落持久事实源，
  Gateway 重启继续由统一 recovery 接管。
- 该分流只认结构化 provider/kind/id，不按自然语言、目录名或“是不是本机”猜身份；客户端仍连接同一个 8420
  Gateway，不能为不同用户再起 Gateway。focused 覆盖 base、local user/group、两个用户物理目录隔离和客户端
  HTTP/file-queue 身份；`.7` fresh U25--U28 进一步证明四个用户任务、Memory 和递归 child 均落各自 owner 家。
- 递归停止真机从协调 child 详情页按 Esc：约 6 秒协调者先进入 `CANCELLED`，约 14 秒四个孙代理全部
  `CANCELLED`，其它 owner 未受影响。U27 正常退出后按 exact session id 重开，完整最终回复与三个 child 名册
  立即恢复；后续禁止工具的复述轮准确返回四类事实和校验短语，审计中该 request 零 tool call。

## 2026-08-29 多 TUI 内存水位与运行对象驻留【状态：真机已量化；Gateway idle eviction 仅审计未改】

- `.7` 8G 机器清理 17 个已结束但仍驻留 tmux 的旧 TUI 后，已用内存约从 2.6G 降到 1.6--1.8G、available
  升到约 5.5--5.7G。保留 10 个真实 TUI 合计 RSS 约 656M，唯一 Gateway 在多用户/递归任务峰后约
  315--381M，另有 终端交互 对照约 159M；没有证据表明单 Gateway 本身占掉数 GB。
- `.10` 16G 机器保留 8 路长期任务并新增 8 路功能链后，已用约 4.8--5.1G、available 约 10G；其中唯一
  Gateway 约 1.0G，采样时仍有 6--17 个真实 child runner 和后台 owner tick，另有多套 会话运行时/终端交互
  对照进程。测试窗口数量、在飞上下文和对照程序必须分开记账，不能把整机 used 都归因于 Gateway。
- 当前 `OwnerScopedAgentPool` 已有容量 64 的有界 LRU，但 Python allocator 会保留已触达的高水位页，任务结束
  后 RSS 不保证立刻回落。对照 会话运行时 `core/src/agent/control/residency.rs` 只逐出已终态且没有 active turn/
  mailbox 的 resident child，以及 通道运行时 ACP runtime cache 的 idle TTL + active-turn fencing；在证明对象数
  持续增长前不新增拍脑袋 TTL。若后续要逐出，必须先以 typed active turn、pending input、未完成 child 和
  owner wake 证明可卸载，保留磁盘事实并增加 pool size/eviction 可观测指标，不能只按时间杀活跃 Agent。

## 2026-08-30 被动轮询不得物化冷 owner，全局索引是可替换投影【状态：R99 `.10` 单 Gateway 真 TUI 通过；`.7` 环境阻塞】

- 对照 会话运行时 `会话运行时-rs/core/src/thread_manager.rs` 与
  `core/src/agent/control/residency.rs`：冷 thread 的查询不因 UI 刷新创建完整执行引擎，驻留卸载还必须同时看
  terminal/error/interrupted、无 active turn 和无 pending mailbox。my-agent 因此将 `/client/notices` 限定为纯被动投影：
  base owner 或已驻留 owner 只读查询，冷 owner 返回 `owner_state=cold` 与空活动投影；不记录 active owner、
  不触碰 LRU、不构造 Agent。真实消息、历史、Memory、控制和工具执行仍走唯一完整 owner 解析，权限没有放宽。
- R99 修复前，`.10` 上 69 个不同 owner TUI 的 notice poll 会超过 64 容量并形成“构造→逐出→下一拍再构造”；
  16 个 HTTP worker 大量停在 Agent 初始化，Gateway PSS 约 `595.8MiB`，`/status` 曾超时。修复重启后同样 69 个
  TUI 同时重连，Gateway PSS 约 `189.1MiB`，10 次 status 为 `4--13ms`；再启动 10 个不同 owner 的 MiniMax-M2.7
  真实模型任务时 PSS 约 `228.1MiB`，20 次 status 最慢约 `0.21s`，队列没有再被轮询占满。
- `global_index/*.jsonl` 只是 owner home 真实任务/run/agent 状态的可丢弃查找投影，不是 append-only 事件账本。
  运行时以去掉 `updated_at` 的结构化指纹只写真实变化；`home-index-rebuild --apply` 在同一文件锁内原子替换
  四份当前快照，不再往旧重复历史后追加一整份。`.10` 现场四份索引约 `1.4GiB`，备份后从 168 owner、
  555 task、555 run、1062 agent 权威目录无错重建为约 `814KiB`，`load_errors=0`。
- 这一切片没有用 TTL 猜测杀活跃 Agent，也没有删除 owner Memory、会话或任务。真正活跃卸载仍需按
  会话运行时 的 active-turn/mailbox 门做后续切片；不能因为一次状态轮询没找到驻留对象就伪造任务终态。

## 2026-08-29 owner 后台常驻对象分为 hard/soft 两类【状态：focused 与 `.10` 单 Gateway 真机通过】

- Gateway 的 scheduler、durable wake、活跃 thread 与 runner 是 hard residency：只要结构化工作仍活跃就必须
  常驻，不能按时间或 RSS 猜测逐出。Memory Curator 是 soft residency：没有当前维护工作时只保留磁盘事实，
  到 owner 真正到期时再惰性构造，完成后从进程内 owner pool 逐出。
- 旧实现会在每次 owner maintenance 扫描时为大量历史 owner 提前物化完整 Agent/Curator；即使 owner 当天没有
  记忆候选，Python 对象和 provider 依赖也会留在唯一 Gateway。现在 `OwnerScopedAgentPool` 显式提供 hard
  agents 与 exact soft-agent eviction，维护循环只让 hard owners 参与 scheduler tick；Curator 按批次有界轮转。
- 这不是靠固定 TTL 杀 Agent，也不删除 owner home、Memory、会话或任务。任何活跃 turn、未决输入、child、
  wake 与 scheduler 继续由 canonical 账本保护。`.10` 部署前唯一 Gateway RSS 约 509--521MiB，部署后初始
  约 185--224MiB；R91 下 22 路 TUI 和八子代理压力启动后 Gateway 约 275MiB，证明降的是无工作常驻对象，
  不是把 TUI 或用户数据挪到第二进程。

## 2026-08-29 终态 child 不接受伪 guidance 队列【状态：focused 与 `.10` 真 TUI 通过】

- `send_guidance` 只表示给仍可消费下一安全点的直属 child 排入补充输入。DONE、CANCELLED、ABANDONED、
  TAKEN_OVER 等终态没有 runner consumer；旧实现仍返回“已排队”会让用户误以为终态 child 收到了消息，并
  留下永远无人消费的 pending 文件。
- 当前入口只接受 `PLANNING/PENDING/RUNNING/BLOCKED`。终态返回
  `SUBAGENT_GUIDANCE_TARGET_TERMINAL`，暂停/失败/超时等无活动 consumer 的状态返回
  `SUBAGENT_GUIDANCE_TARGET_NOT_RUNNING`，两者均不写 guidance 文件、不复活 run。
- 这与 会话运行时 的 `send_message`/`followup_task` 分工一致：给活代理插话和让已结束代理开始一个新 turn 是两种
  控制语义。后者若产品以后需要，应单独建立 exact-run follow-up 合同，不能复用一条假 pending queue。

## 2026-08-29 capability grant 必须跨不可变工具快照续片【状态：focused 与 `.10` fresh TUI 通过】

- 每个 provider 工作片的工具清单是不可变 `ToolRuntimeSnapshot`。常规 `capability_request` 自动授权会先把
  canonical grant 写入任务，但旧 runner 仍在同一模型批次使用旧 snapshot；模型紧接着调用刚获批的
  `write_file` 会得到 `TOOL_UNAVAILABLE`，反复重试后甚至 DONE 却缺报告。
- 对照 会话运行时 `会话运行时-rs/core/src/session/mcp.rs` 与 `tools/spec_plan.rs`：工具 router 从当次
  `TurnContext` 构造，能力变化不在旧 router 上原地打补丁。my-agent 复用现有通用
  `runtime_transition.kind=context_refresh`：grant 落账后结束当前工具边界，剩余同批调用延后，下一 durable
  slice 从 canonical grants 重建 execution context 和 ToolRuntimeSnapshot。
- 普通 OPEN 申请仍等待直属父级裁决，不发布 refresh；当前快照不因模型文字、失败重试或未落盘意图变化。
  `.10` fresh `ma-matrix-r91-110-u94-capability-refresh` 中 researcher 从只读启动，读取输入、申请单文件写权限，
  下一工作片自行写出 1,562-byte 报告；该 owner 归档中 `TOOL_UNAVAILABLE` 为 0，主代理没有代写。

## 2026-08-29 直属 child 最终回复是父会话的耐久继续输入【状态：R92 已部署，旧任务恢复与 fresh TUI 通过】

- 对照 会话运行时 `core/src/context/inter_agent_completion_message.rs`、
  `session/mod.rs::forward_child_completion_to_parent`、`agent/control.rs` 与
  `session_prefix.rs::format_inter_agent_completion_message`：child 的最终回复会进入父 session，不能只存在于
  一次性 wake 或临时 Recent Observations 窗口。
- my-agent 继续以 `subagent-completion.v1` observation 为唯一事实源，不复制第二份业务状态。前台续轮和后台
  scheduled/recovery 续片共用 `contracts/subagent_completion.py` 的中立投影，只接收 exact root、exact
  direct parent、匹配 schema 与匹配 child identity 的完成信封。
- 上下文压力可以缩短 `completion_message`、减少次要输出引用数量，但不能裁掉已选 child 身份或截断
  `final_report_ref`。私有 `runner_result_json/output_json` 永远不进入模型上下文，孙代理结果也不能越级进入
  root 主代理。
- 真实失败 U85 的四份 DONE 报告原本都已落盘并被第一次 completion wake 消费；稍后的
  `scheduled_progress_report` 只剩终态数量，导致模型猜 `work/agents` 并被安全门拒绝。R92 后同一旧 TUI 在
  `compact 1` 后读回四份结果并自然 final；fresh U98 四名 child 4/4 DONE，主代理直接整合出
  `output/python-four-modules.md`，没有 `WRONG_STATUS_SURFACE`。
- 该修复不改变 child 完成判定、不把报告正文变成机器状态、不放宽内部状态面读取，也不要求主代理持续占用
  provider；它只保证每个后续安全工作片仍能获得已经交付给父级的精确结果。

## 2026-08-30 根代理创建 child 后保留同轮控制机会【状态：R93 已部署，fresh TUI 通过】

- 对照 会话运行时 的普通工具循环，根代理成功创建 child 后不应被宿主立刻强制结束当前模型工作片；模型需要能在
  同一 active turn 继续给刚创建的直属 child 发送一次精确 guidance，或取消错误派工。只有 child 自己创建
  下一层时仍结束当前 task-local 工作片，避免父子 runner 身份混用。
- `create_subagents` 的结构化结果明确提示可立即使用 `send_guidance` / `cancel_subagents`，但不自动替模型
  发送消息、不轮询 child，也不改变 child 的启动与完成状态。fresh U109 已在同一模型工作片创建两名 child
  并分别发送不同补充要求，两名 child 的最终报告均包含对应补充事实。

## 2026-08-30 personal/session Memory 由宿主绑定真实身份与寿命【状态：R95 已部署，fresh 双 TUI 通过】

- `remember` 省略 scope 时只写当前 owner 的 personal 范围；模型只声明 `session` 类型时，宿主绑定当前
  Gateway conversation thread。模型不得提供个人 owner key，也不得把另一个 thread id 冒充当前会话。
- formal Memory 仍只有一份 canonical store；`memory_scope` 说明该条记录的 scope、自动召回范围与存储权威，
  `session_search` 同步投影这些字段。显式历史搜索可以按用户要求找到旧会话内容，但这不等于把旧 session
  Memory 自动注入新会话。
- session 的寿命跟 canonical thread 走，不能由模型把“当前会话”猜成当天 23:59。`valid_from` /
  `valid_until` 与 session 同时出现时返回结构化参数错误；需要日历时间过期必须改用 temporary scope。该门
  只校验结构字段，不解析用户正文，也不删除或缩短长期对话历史。

## 2026-08-30 大型工作最终交付采用 会话运行时 式软沟通纪律【状态：已进入默认提示；R106 记录 provider 不遵循样本】

- 对照 会话运行时 default prompt 的 teammate update 和 终端交互 的 cold-reader communication：大型、多步骤或
  长工具链结束时，最终回复应使用完整句子，独立说明已做内容、关键验证、交付位置和仍未验证/阻塞的风险；
  不能只留下“任务已完成”、半句标题或以冒号结尾的开头。
- 这只进入默认模型提示，作为用户沟通软纪律。宿主不解析最终正文、不按中文短语打回、不追加隐藏判官调用，
  也不把报告长度、Todo 数量或模型自称完成升级为机器状态。完成、权限、工具副作用和 child 生命周期继续只读
  结构化事实；provider 真截断仍由 typed stop/stream 合同处理。
- 当前 YAML 与 dataclass 默认提示已经包含 会话运行时 同义的“已知未完成部分仍可推进时持续工作”和 终端交互
  同义的“不能把半成品说成完成”，R106 远端配置正文 SHA 与本地一致。MiniMax 仍两次把以冒号结尾的未来
  计划作为普通 final，说明软提示只能降低概率、不能成为机器保证；同会话普通追问可继续且 Compact/任务身份
  未丢。该样本保留为 provider 遵循缺陷，不恢复最终正文解析、自动验收或隐藏续跑判官。

## 2026-08-30 capability grant 不能扩大 owner 权限上界【状态：R96 已部署，fresh 正反 TUI 通过】

- 对照 会话运行时 `会话运行时-rs/protocol/src/permissions.rs`：运行时附加 root 只能在当前文件系统策略支持的范围内生效，
  工作区外写权限不能靠一次局部授权绕开顶层 policy。my-agent 的 `owner_scope_root` 同样是租户硬墙；语义
  CapabilityRouter、直属父级显式 grant、已有 grant 复用和 lifecycle 最终落账都没有资格把它扩大。
- 申请携带 `path_scope/cwd_scope` 时，授权链先使用结构化路径与当前 manager 的 exact owner root 做规范化判定。
  scoped owner 的越界路径直接结清为 typed capability `GAP`，写入错误码和 rejected paths；不得生成 grant、
  不得发布 grant wake，也不得重派同一 child。普通用户正文、目录名字和模型理由不参与权限判断。
- 任务工作区内及 owner 自己家内的正常授权保持原语义；无 owner wall 的本机管理员继续受既有 workspace roots、
  path mode 与显式 Full Access 约束。lifecycle 作为最后一道防线再次拒绝越界 path scope，防止未来旁路调用者
  绕过路由检查写入一张实际上永远无法执行的授权。
- 该收口解决真实 U116 的“硬墙拒绝 -> 语义路由批准 -> grant wake -> 再次硬墙拒绝”循环。它不把安全拒绝
  伪装成模型失败，也不替父代理修改目标路径；父代理只会收到结构化 gap，随后可按已有权限调整分工或向用户
  说明真正阻塞。

## 2026-08-30 同一 Gateway 回合跨 Compact 续跑保留 active-turn 身份【状态：R97 已部署，fresh TUI 通过】

- 对照 会话运行时 `会话运行时-rs/core/src/session/turn.rs`：上下文压力触发 Compact 后仍由同一个 active turn 继续，只有
  无后续工作时才发出 TurnComplete；终端交互 的 REPL 也从 active session 事实投影 busy/idle，不用一个已过期
  的定时策略重新猜测主代理是否还在工作。
- my-agent 的首段执行会在工作工具晋升时把 exact request -> thread/task 绑定写进 Gateway 请求文件。旧实现没有
  同步更新当前内存 request；同一请求跨内联 Compact 重建 `RunParams` 后，只看见 sticky task cwd，却丢失了
  `conversation_task_turn_active`。第二段可以完成工具和最终回复，但不能在终态提交点关闭 task 与其
  `ordinary_task_resume` policy，稍后 scheduler 会再次认领旧策略，表现为“已经最终回复，Working 又复活”。
- 现在唯一 task-binding writer 原子写入请求文件成功后，也同步相同结构事实到当前内存请求；重建参数时仅当
  `request_id/thread_id/task_id` 三者都与当前 active task 精确匹配，才恢复本轮 active-turn 权限。后续用户请求、
  后台唤醒或只继承同一工作目录的请求不能取得该权限。
- 终态仍由结构化 task/child/guidance/wake 事实决定，不解析“完成了”之类模型正文。正常完成会立即把 exact task
  标成 completed，并由 ConversationStore 同一提交点禁用该 task 的进度/续跑策略，从而让 TUI 保持 idle；
  Compact 续跑本身不会新建影子 Gateway，也不会放宽 owner 权限。
- `.10` 的 fresh MiniMax-M2.7 TUI U142 在 115,858 token preflight 压力后沿同一 request 续跑并正常最终回复；
  终态后观察超过 10 分钟，task 保持 completed、thread 的 active task 为空、progress policy 保持 disabled，
  三份审计均无 `scheduled_progress_report`，消息文件也没有二次写入，未再出现“最终回复后 Working 复活”。

## 2026-08-30 coordinator 等待孙代理必须区别于首次启动【状态：R98.1 修正结构事实，fresh TUI 待复验】

- 会话运行时 分开 `PendingInit`、`Running` 和 `CollabWaiting` 事件；子代理已经完成一次模型/工具工作片并派出直属下级
  后，不应继续显示为从未开始的“启动中”。真实 U141 的 coordinator 已创建两名孙代理，自己的 typed
  `turn_end_reason=SUBAGENTS_ACTIVE`、runner attempt 已 completed，但 canonical task 为 PENDING 等待下一安全片，
  旧展示仅看 PENDING，持续二十多分钟误画“启动中”。
- R98 首次 fresh U143 证明 `turn_end_reason` 的 canonical 值是通用 `interrupted`，`SUBAGENTS_ACTIVE` 只属于
  当轮 runtime reason，不是可持久复读的 lifecycle 枚举；直接据后者做投影会继续误画“启动中”，因此该版
  没有通过真 TUI 验收，不能记作完成。
- TUI/Web 活动投影改为复用调度器同一 `parent_wait_blocks_dispatch()`：只有 PENDING 且存在 schema 正确、
  `state=waiting`、携带 exact run ids 的 durable `direct_child_wait` 才显示动态“等待下级”。初次 PENDING 仍
  显示“启动中”，RUNNING 且尚无首事件仍显示“等待模型”。错误文案、`runner_last_error` 和伪造 reason 都
  不取得展示权威；该投影也不改变 runner 状态、恢复、父子完成或调度。

## 2026-08-30 Todo 与子代理 exact-id 绑定软纪律增强【状态：`fbb562c` 已发布部署，R110 fresh TUI 通过】

- 真实 R109 样本中，主代理先建立 9 条 canonical Todo，随后一次创建 8 个 child，但模型生成的所有
  `create_subagents.items[]` 都省略 `covers`。8 名 child 实际 DONE 后，canonical 账本因此如实保留为
  “原计划 9 条 pending + 8 条独立 child done”，TUI 的 `0/9` 不是渲染漏画。
- 对照 会话运行时 `protocol/src/prompts/base_instructions/default.md` 的模型维护计划语义，以及 终端交互
  `TaskCreateTool` / `TaskUpdateTool` 的 stable task id + explicit owner/status 更新：两者都不靠标题相似度
  自动把执行者绑定到计划项。my-agent 继续只认调用方显式 `covers`，不恢复 goal/title 自动匹配，也不把
  open Todo 变成机器质量验收或跨轮硬门。
- R110 把同一软合同同时放进默认 system prompt、`task_progress` 结构化 execution guidance、
  `create_subagents` 模型 Schema（`covers` 紧跟 `goal`）和 child lifecycle wake：凡下级原样承接已有 open
  项，应复制 exact id；额外工作或关系不能确定时仍可省略，返工仍可先按原 id `correction=true` 重开。
- 对漏绑的 child，完成唤醒要求直接父级只按自己的派工事实和当前证据更新既有 exact id，不允许宿主按中文
  标题猜测。派工回执同时排除 seed 出来的 child run-id 行，`open_target_ids` 只保留原计划，并回送有界
  `bound/unbound_child_run_ids`，避免模型把自动展示行当成下一批 Todo。
- 该改动不改变 child 创建、启动、完成、权限、任务终态或 Todo 的软性质；它提高模型按 会话运行时 式显式更新
  计划的遵循率，并保留 r17 已证明必要的“返工时可不绑定、不能拿下一个无关 open id 顶替”边界。
- `.10` 唯一 Gateway 的 fresh TUI `ma-fbb562c-110-u273-todo-binding` 只提交一次原样 Prompt 3：root 先建
  `proj-1..proj-8 + integrate` 九项计划，首次 8-child 调用逐项携带对应 exact `covers`；child 自然完成后
  TUI 的原计划从 `0/9` 单调推进到 `2/9、3/9、4/9、5/9、6/9、8/9`，不是另加 child run-id 行冒充完成。
  多名 child 在 `compact 1` 后继续推进且仍关闭原 exact id。随后 root 因首名 轻量运行时 child 缺交付文件而补派
  researcher-9；该返工调用省略 `covers`，TUI 正确显示“`8/9 + 另有 1 个子代理运行中`”，没有错误占用
  唯一剩余的 `integrate`。这同时覆盖同工作 exact 绑定与不确定返工不乱绑两条边界。

## 2026-08-30 主代理最终回复前的 Todo exact-id 软核对【状态：`83b0df9` 已部署，R111 fresh TUI 失败】

- R110 最终结构账本证明 8 个子代理计划项都已按 covers 自动关闭，补派 child 也按真实 run id 单列 done；
  但 root 已写完并汇报 `final_comparison_report.md` 后，自己负责的 `integrate` 仍为 pending。终态 UI 收起
  Todo 只是展示折叠，不能把它当成 canonical 9/9。
- 会话运行时 `update_plan` 和 终端交互 `TaskUpdate` 都要求模型显式维护计划，宿主不从最终正文或产物名自动判定
  完成；终端交互 还会把未完成任务作为模型提醒。R111 适配为 `task-progress-closeout-guidance.v1`：
  `task_progress` 回执与后台 Task Runtime State 共用 current-generation exact open ids，提醒模型在最终回复前
  更新已有证据完成的原 id，未完成/阻塞/过时项保持原状。
- `task_progress_closeout_guidance_enabled` 默认开启并可关闭额外模型上下文。该能力不增加隐藏模型调用、不
  拒绝 plain final、不自动打勾、不续跑普通任务，也不按标题、goal、final 文案或文件名推断完成；因此 Todo
  仍是模型自查而不是机器验收。fresh 验收必须看到模型自己调用 `task_progress` 关闭 root-owned 最后一项。
- `.10` fresh TUI `ma-r111-110-u274-todo-closeout` 使用唯一 Gateway、MiniMax-M2.7，只输入一次原样 Prompt 3。
  初次四名 child 的 `covers` 均准确，TUI 随生命周期显示到 4/5；六名长 child 发生一次 compact 后继续，最终
  root 写出 22,938 字节整合报告并自然回复。但整轮只有初始一次 `task_progress`，canonical 仍把五个原项全部
  保留为 pending，另有四个未绑定返工 child run-id 为 done。R111 的 closeout 软合同没有形成可验的最后更新，
  因此不能记为通过。

## 2026-08-30 后台唤醒与 task_progress 共用 covers 对账【状态：`2dded08` 已部署，R112 fresh covers 通过、root 收尾仍失败】

- R111 暴露的第一事实断点不是标题判断，而是两个读取入口不一致：`task_progress` 工具读取前会调用
  `reconcile_completed_child_covers`，后台 `task_runtime_state` 却只对账未绑定 child run-id。TUI 依据 child
  生命周期投影显示 4/5 时，后台模型上下文所依据的持久账本仍是原四项 pending，closeout 合同因此同时列出
  五个 open id，削弱了“只收尾 root-owned integrate”的结构事实。
- R112 为 covers 对账函数增加显式 `task_root` 输入，后台续轮在生成 Task Runtime State 前先运行同一函数，再
  对账独立 child 行并重读账本。该路径只读取 canonical child `DONE`、lineage 和调用方原样 `covers`；不读取
  goal、completion_message、最终正文或产物内容，也不判定 root 自己的整合项完成。
- 定向回归构造“child DONE covers=research + root integrate pending”：后台上下文生成后，canonical research
  已持久化 done，closeout 只列 integrate。`.10` fresh `ma-r112-110-u275-todo-reconcile` 的 8 名 child 全部
  DONE 后，持久账本从初始 0/9 真实追平到 8/9，八个原 covers id 均有 `subagent-done:<run_id>` evidence，
  证明后台和工具读取已经同源。root 写出 16,274-byte 横向报告并 final，但整轮没有第二次
  `task_progress`，自己负责的 compare 仍 pending；这不是 covers 对账失败，也不能由宿主自动打勾掩盖。
- 同一 fresh 任务的 root live Compact 在 20% 摘要阶段后失败，thread 结构事实为
  `compact_generation=0`、`compact_consecutive_failures=1`、`COMPACT_PROVIDERRESPONSEERROR`；两名 child
  各自 Compact 1 后正常完成。root 继续工作并 final，说明失败不丢上下文，但旧错误码丢掉了 provider 的
  具体 `error_code`，TUI 只能显示泛化失败。

## 2026-08-30 每轮 Todo 纪律与 Compact typed 失败原因【状态：R113 本地实现，待严格 gate/发布/fresh TUI】

- 对照 会话运行时 `protocol/src/prompts/base_instructions/default.md` 与 `gpt_5_2_prompt.md`：计划完成时由模型调用
  `update_plan` 明确关闭；对照 终端交互 `TodoWriteTool/prompt.ts`：完成一项立即更新、未完成或有错误不得
  假标完成。R113 只把这条通用软纪律放进每轮 Workspace Context，并受现有
  `task_progress_closeout_guidance_enabled` 控制；没有新增状态机、final gate、隐藏模型轮或自动写账。
- 为避免稳定 prompt 膨胀，该短句替换原有 task_progress 可选性说明；默认 system prompt 不再重复一份。
  一个旧 Compact 测试原本已经在 R112 HEAD 的 12k 上下文单 token 边界失败，fixture 调到 13k 只恢复其
  “成功 Compact 并索引 raw transcript”的原始测试意图，不放宽 recovery target 或生产阈值。
- `compact_exception_code` 现在优先保留 typed `error_code`，经过限定字符和长度后统一加 `COMPACT_`；
  transcript/live-tool 失败进度、Gateway rich chunk、TUI reducer 与红色终态块沿同一字段透传。摘要正文、
  prompt、异常 message 仍不能进入公开事件，失败仍不推进 canonical generation，原上下文仍完整保留。

## 2026-08-30 首个工作工具使用同一晋升后 cwd，后台续轮保留 Skill 正文入口【状态：R114p 本地通过，fresh TUI 待复验】

- 对照 会话运行时 `会话运行时-rs/core/src/session/turn_context.rs`：一次 Turn 的 cwd 是显式结构事实，工具参数、权限和
  进程执行都从同一个 TurnContext 取值，不能在同一调用中同时存在“旧 cwd 的工具快照”和“新 cwd 的任务状态”。
- my-agent 的会话晋升由可变外层 `RunParams` 落账，工具循环则使用独立的 `ToolLoopExecuteParams` 投影。真实
  R114o 安全审计中，首个 `run_command` 已建立 canonical task root，但工具投影仍保留 owner home，导致仓库
  克隆到 owner 根；后续 `list_files` 切到 task root 后自然找不到刚克隆的目录。旧单测让两层参数共用同一
  dict，因而没有覆盖生产形态。
- R114p 在唯一工具执行缝隙完成晋升后，只从外层权威 task attributes 单向刷新本次工具投影，再生成 cwd、
  write boundary、审批和沙箱；不读取模型路径来决定任务身份，也不反向覆盖外层状态。回归用两个独立 dict
  证明旧实现失败、新实现让首次 shell 写入 canonical task root。
- 子代理生命周期唤醒仍是同一任务的后续模型轮。默认后台工具表此前漏掉 `skill_search`，真实 PDF/Skill
  长任务在 child 完成唤醒后连续产生 `TOOL_UNAVAILABLE` 修复轮并额外调用模型。默认续轮现保留 Skill 正文
  读取入口；owner/task policy 仍可结构化减权，未扩大 owner 文件系统硬墙。

## 2026-08-30 Persona 写入限频按 owner 隔离【状态：R114s `.10` 双用户 fresh TUI 通过】

- `.10` 同一 Gateway 的双用户长任务暴露了跨 owner 串扰：u306 与 u307 同时写各自 `USER.md` 时，旧
  `update_persona` 把最近写入时间保存在进程级单一列表。u307 的合法写入因此占掉 u306 的第三个额度；工具
  返回 `PERSONA_UPDATE_RATE_LIMITED`，运行层因该码未登记且没有副作用终态，又把确定的未执行拒绝升级成
  `TOOL_OPERATION_OUTCOME_UNKNOWN`。这不是模型遗忘，也不是 owner 路径泄漏，而是共享 Gateway 的软限频
  错误地共享了配额。
- 对照 会话运行时 `会话运行时-rs/core/src/tools/registry.rs` 的 handler 并发属性和 终端交互
  `StreamingToolExecutor.ts` 的可并行/独占执行边界后，确认本项目的可变 Persona 工具本来就按工具链串行；
  本次不能用全局互斥或降低并发掩盖问题。唯一修复点是把时间窗以 canonical owner home 为 key 隔离，旧桶
  每个窗口有界清理，保持一个 Gateway 服务多个 owner 时互不占额度。
- 同一 owner 仍保留 30 秒最多 3 次写入的软保护；同一消息的多个长期事实应由模型用一次 `operations` 合批。
  超限现在使用已登记的 `PERSONA_UPDATE_RATE_LIMITED`，结构化标记 `effect_outcome=not_started`，可在退避后
  重试，绝不能再进入副作用未知或 DIRTY。限频不参与 owner 记忆内容、权限、确认和 Compact 判定。
- 唯一 Gateway 部署后，fresh owner u310/u311 并发各完成 3 次真实写入，六条 operation 均为 `SUCCEEDED`；
  各自 `USER.md` 只含自己的识别码、回答风格和界面主色。随后同 owner 新 TUI 在不调用读取工具的前提下
  准确召回各自三项且未混入对方事实，真实门通过。

## 2026-09-01 Gateway 同一 active turn 重启恢复的 unknown 窄口【状态：R130/R131/R133 三安全点真 TUI 通过】

- 对照 会话运行时 `会话运行时-rs/core/src/agent/control/spawn.rs` 与
  `app-server/src/request_processors/thread_lifecycle.rs`：恢复保留原 thread/history/active turn 身份，运行中的
  turn 与持久历史在恢复响应中按同一 turn id 合并；新的执行片不能把旧执行者不确定的副作用当成没发生。
- `.10` 唯一 Gateway 的 R129 真 TUI 在主代理已完成 15 个工具动作、第二次 live-tool Compact 刚开始时收到
  SIGTERM。请求文件按 exact request id 成功重排，工具归档、任务目录、thread/task 绑定均保留；但启动调和先
  把旧 RuntimeDB run/attempt 标成 `unknown`，第二次执行在 `_bind_main_agent_authority` 直接创建新 attempt，
  被通用 unknown 硬门正确拒绝。两条单独正确的规则因此互相冲突，请求以 `RuntimeConflictError` 失败。
- 通用 unknown 合同不放宽：普通 CLI、child、缺失身份、执行中/UNKNOWN 工具、缺耐久结果、DIRTY/MUTATING
  资源仍必须人工 `recover_attempt_unknown`。唯一自动窄口只接受 Gateway 已写入的
  `gateway_active_turn_recovery.v1`，并在 RuntimeDB 单事务中同时核对 exact task id + run/request id、current
  unknown attempt、每个已启动工具的 terminal row 与同 operation id/tool/status 的耐久归档；从未进入 handler
  的 CLAIMED 可证明无副作用，事务内改为 CANCELLED。
- 全部核对通过后，旧 attempt 以 `recorded_active_turn` 审计事实转 recovered、unknown run 回到 created、只
  释放该 attempt 的执行锁；随后普通 authority binding 创建下一 generation。任一不确定项继续 fail closed，
  不解析异常文案、模型正文、路径或时间猜测，也不重放已结算工具。
- 本地 focused 已覆盖 exact 成功、缺归档、EXECUTING、task/run 错配、未启动 CLAIMED、DIRTY resource、人工
  unknown 恢复不回归，以及 Gateway 真实 carried archive 接缝。
- R130 主代理活跃点在 `.10` 唯一 MiniMax-M2.7 Gateway 上通过：同一 request/thread/task/run 的旧
  generation 1 已有 21 个 terminal operation，顺序重启后全部按 matching archive 记为 recorded，旧 attempt
  转 recovered；generation 2 仅完成剩余 5 个操作并自然 done。TUI 直接 final，原 owner task root 内 18 个
  unittest 和全部报告存在。
- R131 在 main 等唯一直属 child 时顺序重启，原 child run/generation/runner 不变；child 完成后原 main 新
  generation 自动 final。R133 在唯一 coordinator 已以 `SUBAGENTS_ACTIVE` 等待两名直属 worker 时顺序重启，
  两名 worker 不复制，原 coordinator 与原 main 依次生成下一 attempt 并自然完成。三种安全点均由独立 fresh
  MiniMax-M2.7 TUI 证明，不互相外推。
# /model 用户模型配置（实现中）

按用户保存 OpenAI/Anthropic 接口、模型名、地址、私密密钥及显式上下文窗口；Auth 暂留入口。
配置菜单不送模型，当前工作片不热换模型；设计与验证边界见 [TUI_MODEL_PROFILES](docs/design/TUI_MODEL_PROFILES.md)。
