# ROADMAP

状态标记：

```text
设计中       已形成方向，还没写代码
部分落地     已有基础结构，但还没完整接入运行链路
待验证       已写代码，但还需要真实场景验证
暂停         暂时不做，但保留背景
```

每条记录必须包含"解决问题"，说明它解决哪个用户痛点、系统风险或交付缺口。

更新时机：开工前从这里领取任务，收工后更新状态或移到 COMPLETED.md。

---

## 下一版优先级

### 长会话封板与受管进程网络证据

状态：R178 五阶段已结束；R179 网络观测/停止与 R180 已保存历史恢复真 TUI 通过，完整归档仍待补齐

解决问题：后台服务存活不等于局域网可达，模型漏查 firewalld 后两次误归因客户端；已有网络观测还把查询
失败当未运行、部分端口放行当整体放行。当前按原生退出码保留未知和逐端口证据，不增加任务质量判官。
同一长 TUI 已完成两次大任务、两次小追问和追加修改；质量仍有 skip/占位实现，与生命周期通过分账。
exact resume 又发现只恢复问答预览、没有工具/思考的 BUG-111；R180 正在补已持久化内容的显示恢复。
Compact 前完整过程与历史分页仍待补齐，未具备的 IM/Audit 环境不得冒充通过。

### Shell 产物保护不污染用户项目

状态：R167/R168 已落地，focused 与 `.10` MiniMax-M2.7 真 TUI 通过；后台 shell 另待验证

解决问题：R165 真任务在项目根执行 pytest 时，`run_command` 会先把已登记的 `test_*.py` 原名复制到
`<task>/data/artifacts/shell_backups/`，同一条 pytest 随即把框架副本再次收集；无变化命令还会永久留下副本。
当前新增唯一 `owner_data/artifact_backups` 权威根，从 HomePaths 经 ToolRegistry 显式注入，管理员 Full Access
在外部 cwd 也不随项目漂移。副本使用无原扩展的哈希 blob、owner-local opaque ref、临时文件 + fsync +
原子 replace；shell 后内容未变立即删除本次 blob。终端交互 的 owner 配置目录/哈希文件历史用于存放与命名
对照，会话运行时 shell snapshot 的临时提交、Drop/过期清理用于原子性和生命周期对照。R168
`ma-r168-110-u381-artifact-linear` 已用 5,000 行真实工具输出验证 archive 仍可恢复，项目树没有内部备份，
结算后 manifest/blob 均为 0，工具历史不会被下一条 shell 递归复制。后台受管 shell 要等真实进程退出事件
接入产物复核，继续作为独立边界，不由本次前台证据冒充完成。

### Compact 浅压缩与缓存重建成本

状态：已落地，focused 与 `.10` MiniMax-M2.7 真 TUI 通过

解决问题：R163 已证明 child 溢出正式记代和五代 main Compact 都是真实新历史，不再账外续跑；但 main
gen1/4/5 只释放约 12%–18%，压后离 90% 触发线仅 2k 左右。当前已让最低水位估算在副本上执行真实 IR 删除器，
不再假设不可删的 User/RuntimeFacts 会消失；机械 fallback 输出独立封顶 4K。live 摘要同时按 会话运行时 的完整
history 追加请求顺序和 终端交互 cache-safe fork 复用主请求 system/tools/model/messages/thinking 前缀，工具
schema 只参与缓存且没有执行入口。低于 trigger 的有效候选继续提交，不能恢复“没到 60% 就作废”。R166
`ma-r166-110-compact-cache` 已在一个 8-child 长任务中验证 main 2 代、child 1 代、进度动画、持续 cache-read、
Compact 后继续工作与最终收口；child 从 118,696 压到 73,217，main live-tool 从 85,161 压到 726。结构化
RuntimeFacts 的进一步替换只在后续真实账本仍显示不可接受的 shallow/cost 时再设计，不预先扩大改动。

### 模型前置失败后的 run/attempt 终态

状态：已落地，本机 MiniMax-M2.7 真 TUI 复验通过

解决问题：Gateway 已登记权威 run/attempt 后，skill 快照、工具准备、上下文装配或 provider capability probe
可能在正式模型循环前失败。旧异常边界只包住模型循环和 finalization，导致界面已经报错并空闲，RuntimeDB
却永久保留 `created/running`。当前把“已绑定 attempt 后的整个执行体”统一置于既有异常收口边界：普通异常
写 failed，显式中断写 cancelled；不新增启动补扫，也不把 provider 错误当成可恢复任务。R143 本机真 TUI
先以缺密钥复现失败，再恢复同一 Gateway 完成下一轮完整单位换算工具，失败/成功两轮账本均与界面一致。

### 普通模式与 Goal 模式分离

状态：本地 focused 通过，待顺序重启/长任务真 TUI

解决问题：旧 `ordinary_task_resume` 会把普通回复后的工具上限、open Todo 等软事实变成后台自动模型轮，用户
以为任务结束却仍在 Working，也可能重复副作用。当前与 会话运行时 普通 turn 对齐：普通模式在本轮自然结束；只有
用户显式创建的 ThreadGoal 持有跨轮持续执行语义。旧 policy 只做 typed retirement，不再被执行；子代理等待、
生命周期 durable wake、显式插话和 provider transient retry 仍按各自结构化事件恢复，不能被误删成“都不续跑”。

### Computer Use 复用开源执行器

状态：已落地，`.10` MiniMax-M2.7 真 TUI 完整闭环通过

解决问题：my-agent 后续需要截图、点击、输入、滚动和等待界面状态，但不应在底座里自造一套桌面自动化引擎。
当前选定 MIT/PyPI 的 `computer-control-mcp==0.3.13`，通过 `my-agent[computer-use]` 随底座发布；my-agent
只按结构化 local/main + Full Access 注入一份保留名 MCP profile，复用现有 typed Tool Gateway、审批、取消、
超时、operation 和外部数据不可信投影。普通 owner/WorkspaceOnly 看不到该能力，GUI session 只透传明确的
DISPLAY/WAYLAND/XAUTHORITY/DBUS 字段，不泄漏 API Key。上游未注册滚轮时只在启动入口复用其既有 PyAutoGUI
公开动作补一个 `scroll_screen`，不复制执行引擎。下一步在 `.10` 单 Gateway 的 Xvfb 里用 MiniMax-M2.7
完成窗口枚举、OCR、激活、滚动、ASCII 输入、按键与 OCR 后置验证；同时记录当前上游 Python 3.14/Unicode 输入和
原始 image provider 投影限制。浏览器内任务继续优先 Browser，不能另造第二套浏览器控制协议。

首轮真 TUI 已暴露并定位一项底层缺口：16 个执行器工具均已注册，但旧逻辑把全部 MCP 无条件放入
`mcp` 延迟类别；MiniMax-M2.7 不支持 会话运行时/终端交互 的原生 tool-search 引用协议，首轮实际看不到桌面工具，
转而误选 `terminal_session`。当前增加 deployment-owned `catalog_category`：普通 MCP 仍默认延迟，官方
Computer Use 使用稳定 `computer_use` 分类直接进入首轮 Schema。这个字段只控制目录披露，不能改变 owner、
Full Access、effect 或审批。fresh TUI `ma-r123-110-u376-computer-use` 已完成 activate、截图、OCR、滚轮、
点击、ASCII 输入、回车与后置 OCR；模型从滚动后的屏幕读取 `SCROLL-R122-927`，目标应用回显
`COMPUTER_USE_PASS R122`。同一 Gateway 的 `ma-r123-110-u377-computer-stop` 用 120 秒 MCP wait 覆盖与慢
OCR 相同的 client cancellation 通道，`/stop` 后工具立即显示“已中断”；普通 owner TUI
`ma-r123-110-u378-computer-isolation` 则完全看不到桌面能力。验收后仍只有 1 个 Gateway 和 1 个 Computer
Use 子进程。

### 测试机部署空间的显式保留策略

状态：环境 P1，未改产品代码

解决问题：`.10` 根卷当前 47G/100%，普通 wheel 的新隔离 venv 首次因 ENOSPC 失败；清理可重建的 pip/Go
缓存后本次部署完成，但历次 deployment backup 约 2.4G。后续必须先盘点 owner task/会话/Memory 与纯部署
副本，设计“至少保留最近可验证回滚点 + 明确过期窗口 + 删除前可见清单”的运维规则，再由用户批准执行。
不能把用户资料或所有历史回滚点与编译缓存混为一谈，也不能依赖 `/tmp` venv 作为长期运行时。

### 三项只读审计发现的底层一致性缺口

状态：待验证，尚未修改产品代码

解决问题：本轮 provider/后台 final P0 已闭环，但只读审计还发现三处不能用本次真 TUI 证据顺带宣称完成的
边界：低层 owner-scoped process 若显式给出 read roots/cwd 却遗漏 `allowed_write_roots`，仍需确认是否可能把
`None` 误作可写默认；ordinary resume 丢失 `conversation_request_id` 时，Todo display generation 可能与后台
任务路径代次漂移；capability route 与 runner result 对同一 child state 的整对象保存可能存在后写覆盖。

下一步先各写一个 failure-first 合同测试，再对照 会话运行时 的 sandbox policy、turn identity 与 agent state event
合并入口做最底层修复。三项可以只读并行审计，但实现阶段必须按文件/函数划分唯一 owner；不得把自然语言、
界面刷新或重试次数当修复，也不得借机改 `/audit`。

### 后台连续 Compact 的自然跨切片真机样本

状态：R114u focused 与 `.10` 部署完成，fresh 长 TUI 观察中

解决问题：公平调度把一个后台工作片限制为最多连续 8 代 Compact，但旧实现达到上限后抛普通异常，把健康
续跑误记成失败。当前已改为 typed yield：claim 干净结束、原 wake 保持未消费、下一 slice 从 canonical
generation 继续；普通异常仍失败。单测与运行中 Gateway 重启恢复已经通过，尚缺一个新 wheel 下自然跨过
第 8 代的真实任务样本。继续观察 u315/u317 长任务；若未自然达到，不人为灌无意义 token 或修改产物来造通过。

### TUI 常驻进程回收与两台测试机资源同负载对照

状态：R105 `.10` 已分组采样，`.7` SSH 阻塞待恢复

解决问题：tmux 会话未退出时 Python TUI 会长期保留，测试累计约 50 个客户端后其总 RSS 已大于 Gateway；
如果只看整机 used，会误判为单 Gateway 内存泄漏。下一步在 `.7` 恢复后用相同 Gateway、owner 数、空闲/活跃
比例和长任务负载，分别记录 Gateway PSS、TUI RSS、runner RSS、后台进程与 10 分钟空闲回落；再决定是否需要
显式 idle close、会话管理提示或真正的进程修复。不得按墙钟杀仍有 active turn/child/审批/PTY 的客户端。

### child 创建后的启动阶段、roster revision 与详情首事件可解释性

状态：R109 单条八 child 长任务已真 TUI 通过，多 owner 启动延迟量化仍待后续矩阵

解决问题：r56 中从主代理发出创建请求到 child runner 真正开始约 231.7 秒；期间 roster 可能长时间只显示
“等待启动”，刚进入详情页又会暂时空白。用户无法区分 dispatcher 排队、进程启动、模型首事件慢还是投影
revision 迟到，容易把慢模型当成死锁，也可能在看不见 prompt 时重复操作。

当前候选已按 `queued → starting → waiting_first_event → running` 投影，并把首事件绑定到 exact run/attempt；
完整 child goal 可读后先显示为 user block，goal/首事件暂未持久化时保留启动说明和 Working；首次进入详情
从 goal 顶部打开，返回或重访恢复该页面原位置，回到底部后继续自动跟随。R109 fresh `.10` resume TUI 已
证明无需 `Ctrl+Home` 即可看到完整 prompt，翻页后回切位置保持，child 终态与 main 最终 Working 正确撤下。
后续只需在 `.7/.10` 单 Gateway、多 owner、多 TUI 重复测慢首事件并量化
`dispatch accepted → runner process → owner activity revision → first durable transcript event` 四段。不能把 PENDING
冒充 RUNNING，不能伪造 child prompt，也不能为了 UI 快而创建第二 runner。

### Owner 自主 Memory 与 SOUL 单独确认

状态：本地实现与 owner 隔离 focused 通过，待 `.7/.10` 真 TUI 验收

解决问题：旧入口把 USER/AGENTS/长期记忆、Lesson/HOT 的部分合法维护也统一推到人工确认，用户长期使用时
会积压候选；如果直接放开，又可能让模型跨 owner、无证据覆盖或把普通文本当机器授权。

当前候选按结构化 target/type/origin/action 推导晋升模式：当前 owner 的长期事实、USER、AGENTS、Lesson/HOT
在证据、scope、CAS、quota 和冲突门内自主推进；只有 `SOUL.md` 修改保留本人确认。候选与 Persona 全程从
同一 owner home 解析，replace/remove 必须 exact target，旧的合法非 SOUL manual 候选可升级到当前主链；
`/audit` 没有改。下一步用不同 owner 的真实 TUI 写入相近 subject，验证互不可见、幂等和重启后仍隔离。

### fresh capability 审批链与剩余环境能力验收

状态：focused/历史链有证据，当前全功能矩阵仍未 fresh TUI 完成

解决问题：`capability_request → resolve_capability_requests → controlled_exec` 目前分别有合同回归和历史样本，
但 R50--R57 没有一条同 fresh child、同 owner TUI 从请求、展示、批准到原调用续跑的完整证据；
`publish_audit_update` 也缺 active Audit 成功样本，`send_message` 则因没有真实 IM channel 正确 unavailable。

下一轮按完整审计的 35 工具矩阵补这三条，不用 fake/模型口述冒充。Windows ConPTY、macOS 右键复制和
测试机 `libgbm.so.1` 浏览器依赖属于环境缺口，应在具备对应环境后单列验收，不反向写测试专用产品分支。

### live Compact 摘要污染与只读任务写报告冲突

状态：只读优先级已在 `.7` 真 TUI 通过；R154 主代理自动 Compact Esc 已由本机 MiniMax-M2.7 真 TUI 通过；
child/grandchild 自然中断与 live handoff 结构校验仍待后续真机样本

解决问题：真实植物大战僵尸只读验收在 generation 5/6 连续 Compact 时，MiniMax-M2.7 先返回一段原始
`<minimax:tool_call>` 伪写文件协议，后又只返回“写入验收报告”的半句。旧代码只检查非空，导致坏摘要进入
下一轮，模型在 6/6 Todo 后从头重读；通用“研究必须写报告”提示还与用户明确“只读、不要写文件”冲突。

当前候选要求 live handoff 使用固定 marker 和六个有序语义栏，拒绝工具协议、缺栏与过短动作句并退到 typed
IR 机械投影；通用研究纪律明确服从本轮只读/不落盘要求。两个 focused 文件 91 项、PyCompile、Ruff 和
`git diff --check` 已通过。部署后的原长 TUI 已连续两轮只读核对、纠正旧结论并直接最终回复，磁盘无报告
文件；期间触发的是 transcript Compact generation 7，并非 live-tool，因此不能把 fixed handoff validator
冒充真机已触发。随后手动 Compact 真机复现了 Esc 无目标、仍提交 generation 的独立缺口；当前候选已把
Compact 登记为 exact cancellable operation，精确 stop 走 durable urgent lane，供应商断流并在 checkpoint/CAS
前复核中断。R154 本机 fresh TUI 已在 `active_turn_tool_archive` 摘要 20% 时 Esc：operation 中性
`superseded/candidate_discarded`，request/task 为 interrupted，generation 保持 4、失败计数保持 0；随后同一
TUI 可继续原任务。主代理这条已完成，child/grandchild 仍须用自然长任务分别补证，不能拿合同测试冒充真机。

### 代码瘦身第二轮：区分产品合同与离线评测脚手架

状态：设计中

解决问题：`agent/contracts/` 仍同时承载产品运行协议、错误分类、离线评测和历史 acceptance 命名模块；仅按
文件名或单次 grep 批量删除，可能误伤仍由 CLI、测试或发布流程消费的合同，也会让测试代码继续混入发布包
边界。下一轮先生成“生产导入 / CLI 导入 / 测试专用 / 文档历史”四类依赖清单，再迁移或删除已证实的测试专用
产品文件。不得恢复本轮已退休的机器验收数据库，也不得为了缩行数删除结构化安全门。

### 主代理“正在调用模型”与“等待子代理”展示分相

状态：设计中

解决问题：当前 `Working · main` 是整棵任务树仍活跃的展示，父代理已经结束当前工作片、只等 child 时也会
继续闪动；用户因此误以为主模型一直占用，进而担心这时发的新消息必须等所有 child 完成。实际消息不会等
全体 child：live foreground 在当前 provider 安全点接收，父代理已让出时开新 foreground slice，后台正整合
时最多等待那个 slice 释放共享 lane。

下一步只改展示投影：用 typed active turn/run claim 与直属 child 状态区分“主代理工作中”“等待子代理”，
不得从 `Working` 文案、动画帧或模型回复推断调度状态，也不改变现有插话、队列和 child 生命周期。

### 子代理终态后的主代理整合工作片持续续跑

状态：本地实现与 focused 通过，待 `.10` 唯一 Gateway 真 TUI 复验

解决问题：真实 Ripgrep 换语言复刻中三名 child 已全部 `DONE`，主代理也真实执行了整合、安装和测试；但
该后台整合工作片达到 `TOOL_ROUND_LIMIT_REACHED` 后，旧代码把所有 `background_main_agent` 一律当成
“只读状态、给回执”，没有登记下一工作片。于是任务仍为 active、没有执行器，TUI 长时间显示等待，用户
误以为模型卡住，也收不到最终汇报。

当前进展：收口只读取本轮开始时冻结的 typed child phase。`subagents_active/no_subagents/状态未知` 继续
不轮询；只有 `subagents_terminal` 且本轮是结构化可续跑原因时，才用 exact durable root task id 在同一
thread 登记有界、立即到期的 ordinary resume。`save=False` 只表示后台 transcript/归档不重复写，不能再
顺带关闭 active-turn 续接。普通自然后台回执、child 自己的 task-local runner、机器未知态和模型正文都不
取得续跑权。下一步部署 `.10` 后用同一真实长复刻 prompt 验证跨工具轮整合、最终交付和 policy 退休。

### 主/子代理验证结论与受管后台动作边界

状态：approval/session/`network_status` 主链已部署；不误报合同真机通过，真实 LAN 可达性仍失败

解决问题：真实超级玛丽任务中，模型把测试机本机 HTTP 200 和 `0.0.0.0` 监听错误说成另一台电脑已经能
访问；即使用户明确要求“不能就说真实原因、不要把本机访问说成局域网访问”，后续模型仍重复误报。

当前进展：`f8a4744` 已在 r44 真实阻住后台 `run_command`，但模型拒绝后改用
`terminal_session.start` 真实启动同一服务。第六候选把“sandbox 未包住的参数变体”收入
`SandboxPolicy` 权威合同，Shell 后台和 PTY start 都走 exact approval，ActionPolicy 不按工具名分支。
PTY 已批准创建后，`write/read/close` 作为 会话运行时 `write_stdin` 同类运输不重复弹窗。fresh r47/r48 已分别
证明直接后台命令和 PTY start 拒绝时 handler 不执行；r47 同时发现主代理可派 child，child 获得 capability
grant 后经 `controlled_exec` 直接 Popen 同一服务。第七候选把 grant 与 exact approval 分开，并如实声明
`controlled_exec sandbox=none`。`c8a01f7` 部署后的 r51 已证明 grant 可以形成，但未获批准时仍返回
`APPROVAL_REQUIRED`，handler 未执行、测试端口关闭。该安全门同时暴露了下一层产品缺口：child 没有
`request_permission` consumer，用户看不到也无法裁决后让原调用续跑。第八候选对照 会话运行时 typed approval
等待链和 终端交互 worker→leader 标准确认队列，将 exact child request 放入 owner-scoped 耐久记录，
Gateway/TUI 只按显式 capability lease 领取；主代理与多个 child 共享一个 FIFO，决定经过现有 owner/root/
current-attempt 门后唤醒原 ToolCall。无界面、断线、取消和损坏全部 fail closed。下一步必须在 `.7` 唯一
Gateway 的 fresh MiniMax-M2.7 TUI 中证明：未批准前无副作用，面板标出 child，批准后同一调用原地成功。

当前实现继续区分“服务活着”和“用户能访问”：`process_session(network_status)` 只读取
exact managed session 进程树持有的 Linux listener，并查询 firewalld 的显式端口规则；loopback、
non-loopback、未显式放行和未知分别返回。任何结果都保留
`unverified_external_probe_required`，不会改防火墙或把本机 HTTP 200 升级为局域网成功。r55 已由 Mac
真实请求 `.7:8765` 并得到连接失败；模型保持未验证，故“诚实报告”通过、LAN 可达性本身未通过。本轮未
旁路开端口；后续应把网络/防火墙诊断作为独立运维验收，不再改模型语义掩盖失败。

### 长期会话完整正文、思考时序与 canonical task root

状态：final 后重复 thinking 已在 `.7` 真机通过；sticky cwd 的旧语义已由 2026-09-02 R155 收窄

解决问题：主/子代理工具前过程和最终长报告曾被活动块折叠，用户要按 `Ctrl+O` 才能看到；多次模型调用的
thinking 会覆盖、闪烁或留下空 spinner；滚轮一步过大。普通消息还有固定 12K 裁剪，既可能丢长期 TUI/IM
历史，又会改写 provider 缓存前缀。任务晋升后 child 还可能继承 daemon `/root` 或客户端 cwd，产物落错家。

当前进展：每个 provider thinking 独立按时间顺序封口，空首块用 typed discard 删除；工具边界 commentary
以 `assistant_part_id=commentary:N` 完整落账，final 用 `assistant_part_id=final`，长报告直接展示，滚轮一格
一行。普通 user/assistant 不再按固定字符数截断，预算只从最老完整消息边界回收，真正 Compact 才替换旧
前缀。按样本 `B=357,639/H=235,041`，普通价 5、缓存价 0.1/1 时成本分别为
`1,811,699.1/2,023,236`，相对全不命中节省 38.86%/31.73%；破坏 100K 稳定前缀会额外花
490K/400K。首个 `promotes_task` 动作后，main/child/grandchild 的 cwd 与写根统一切到
`<owner_home>/tasks/<task_path>/`。r55 第一段 5 名 child 全部自然完成、final 直接展示；第二段 successor
身份与 `task_path` 已正确续接，但模型首采样仍看到旧 TUI 启动 cwd，第一条后台命令因此服务了空 `bbb`。
`1e4c64d` 曾让 non-detached sticky root 在模型首采样前成为唯一 `execution_cwd`；R155 后 terminal
pointer 只作状态/导航，只有 exact request、Goal 或 active task 会在首采样前取得 cwd，精确旧项目写路径
则在 effect 前回绑。
原 r55 的同提示复验中，首个列表、审批、bwrap 和服务进程全部使用原 task root，localhost 返回真实游戏；
三个独立 completed run 的 `task_path` 精确相同。该切片完成，剩余独立问题是 LAN 外部失败、Gateway 冷启动
约 30 秒，以及 main 在“只协调”任务里仍亲自改功能文件。用户随后在同一 r55 指出最后稳定块仍是思考；
原始 chunk 证明完整 `assistant_thinking` 晚于最终 `model_delta`。`ae3fd1e` 把 Anthropic block-stop 变成
同一 typed observer 的完成边界，流内完成后禁止 response fallback 重放，并让 TUI 兼容消费一次旧迟到
终态。完整 focused 与严格 gate 已通过，代码已推送并部署 `.7` 唯一 Gateway。原 r55 恢复后的新请求中，
第 95 条唯一 `assistant_thinking` 严格早于第 96 条首个 `model_delta`，屏幕底部为 assistant final；该缺口完成。

2026-08-30 R106 递归复验发现孙代理虽继承 cwd/roots，却可能丢失父级完整 `run_workspace`，从而在 manager
runtime 另造任务树并把产物写错家。当前已让父级 canonical task root 覆盖 nested attributes；depth-2
focused 与 R107 fresh TUI 均通过，所有孙代理产物同根、父级可读且 Gateway 零路径拒绝，本条重新标为完成。

### 跨回合工具终态折叠与缓存稳定前缀

状态：V1/V2 与跨完成回合 messages 已部署 `.10` 真机；`.7` 慢模型长任务仍自然运行

解决问题：同一长 TUI 中，当前模型可见上下文从 60 多 K 回落到 50 多 K，但 canonical thread 仍为
`compact 0`。权威账本证明没有漏记 Compact；真实缺口是普通回合结束后 native ToolCall/ToolResult 只保留
最终 assistant 正文，工具历史无痕退出下一轮。用户认可合理终态折叠，但要求保留续做事实并合理利用缓存。

实现边界：每个 main/child 回合从 canonical archive 一次生成不可变、有界、脱敏的 typed metadata，与 assistant
消息同账落盘；V2 同时固定保存热尾、冷折叠与切换时间，默认 300 秒内读取热尾，过期后读取冷折叠，旧 V1
恒按冷折叠兼容。近期细节与 exact refs 保留，原始长输出仍按需读取。折叠不推进
Compact；真正 Compact 才汇总这些 fold 并推进 generation/transcript source-message；运行中 native IR 已压掉的
完整工具对继续单独计数，不能把同一次调用重复算两遍。历史旧前缀不按每轮预算重新改写，以便
MiniMax/Anthropic 兼容链继续命中 cache-read。

本地 V2 精确投影 A/B 已由 `127.0.0.1:8901` 的 provider usage 证明：热尾第二轮 14,998 input 中
14,336 cached；立即 cold 15,072 input 中 12,288 cached。按普通价 5、缓存价 0.1/1，热尾分别比立即 cold
省约 68.7%/32.7%。32 项折叠/搜索 focused 通过；MiniMax-M2.7 真实 native 探针能在未下载 GitHub 仓库、
本地精确类名与 local no-match 三种场景分别选 `web_search/search_text/web_search`。fresh r59 的真实工具轮和
热期普通追问均成功，V2 hot/cold/deadline 已同 assistant 消息落账；原 r58 第二轮失败也已定位并修复为
“缺失可选 observation ledger = 空集合”。deadline 后 selector 已明确返回 cold-fold，同一 r59 仍准确记住
唯一串和四字段。本机 4000/8901 路径在约 70/310 秒均命中 12,288 cached tokens，但两者共用一个 OMLX
后端；待办转为不同真实 provider、负载/容量淘汰和更长会话校准，不能把本机五分钟下界外推为统一 TTL。

2026-08-27 又补齐普通后续轮的稳定 system 前缀：旧 prompt 把约 23KB 的固定规则/Persona/Skill/工具目录
与每轮变化的记忆、时间、Conversation 和执行事实绑成一个 user 块，导致只命中 tools、反复写约 11K 动态
缓存。当前 typed `CacheStructuredPrompt` 保留完整字符串，但 Anthropic native 把固定段放进顶层 system
缓存块，动态段继续完整作为 user；不裁剪历史、不推进 Compact。`.7` r60 第三个连续回合已为
5,959 input / 19,300 cache-read / 0 cache-write，对照首轮 25,192 input；按输入 5、缓存 0.1/1 分别省约
74.8%/61.0%。该切片本身已通过，下一步仍要用同一个长多子代理 prompt 跑 my-agent 本地/MiniMax、会话运行时
MiniMax、终端交互 MiniMax，比较耗时、缓存、Compact、子代理和交付；短诊断链不能冒充长任务结论。

扩展 Compact 回归还复现了 HEAD 既有的 child 终态反转：同一 request/run 在 overflow 后先落一次累计模型账，
Compact 继续成功后再以同 event id 写更大的累计账，Store 正确 fail closed 却导致成功 child 被标失败。当前按
物理调用游标生成事件 id，并在唯一 Store 锁内把累计快照减去既有增量；精确重放核对原快照指纹，缓存读写
和 provider/estimated 分区不双计。该修复不改变 Compact 次数、task 状态或 provider cache 协议。

同 prompt 的 r62 MiniMax 长任务已自然终态：201 次物理调用为 438,162 普通 input、6,097,485
cache-read、553,087 cache-write、117,003 output，两档价格相对全普通输入节省约 84.30%/68.81%。
OpenAI-compatible 本地模型首次真 TUI 在请求发出前因 `generate` 缺少统一 thinking observer 参数失败；
当前候选已按 会话运行时 typed reasoning 事件与 DeepSeek Harness/工具运行时 `reasoning_content` 适配补齐接口、
流式封口和工具轮回放。fresh r64 首段真实命中 40,960 cached token，但约 1 小时 41 分内只派出两名
child；轻量运行时 遇到 600 秒流总墙钟超时，工具运行时 未结束，余下六项未派出，最终从 TUI `/stop`。缓存层的
双 provider 证据已经成立，但本地模型长任务的超时、派工吞吐和取消调用 usage 下界仍是独立待办，不能
用缓存命中替代任务通过。

当前本地候选已按 会话运行时 拆掉流式固定总墙钟：动态 first-event 与 rolling idle 分相，预算作为 request-local
option 传入，不修改共享 backend；默认慢速估算 200/20 token/s、最大 10,800 秒，可覆盖约 1M 输入的极慢
prefill。Gateway/child 已把 committed summary、已结束 user/assistant 与当前 user 作为 canonical messages
顺序发送；`runtime_injections`、memory、推荐和 workspace 位于 IR 后动态尾部，不再把 transcript 重复塞进
变化 prompt。`.10` 原长会话部署后两个普通追问分别得到 24,168/24,388 provider cache-read，第二轮按两档
价格节省约 66.12%/53.98%。`.7` r65 在 42 分钟时仍有七名 child 运行、没有固定墙钟 timeout，已证明
跨过旧 600 秒边界；仍需等待自然终态后核对完整产物、恢复 MiniMax 唯一 Gateway，并继续 会话运行时/终端交互
MiniMax 同题对照，不能用当前存活样本冒充完成度。

r62 的第九名替身已定位为 `planned_dispatch.v2` 未检查已有直属兄弟的 active covers。候选协议升为 v3：
活动直属 run 持续占用 exact covers，只有显式 replacement 可接管；创建复检、PLANNING 落账和 takeover edge
进入同一短 guard，接管落账失败的新 child 在启动前取消。按 会话运行时 共享 workspace 边界，旧 output 祖先/
相同路径硬拒绝已撤销，只保留父 workspace 上界；写集冲突继续是模型软分工，不恢复目录锁。派工 focused
已通过，仍需 `.10` fresh TUI 复验 capability 恢复后不再重复补派。

当前 `.7` 同一长 TUI 的新七路调研再次证明 7 个 child 都真实 `DONE`，但 root 最终只收到 3 份并错误收口。
逐条 wake 账本已定位到更底层的接收者串线：前 6 条 completion 在兄弟 child 仍运行时被其 task-local
模型安全点读取并确认，只有最后一条留给主代理。当前候选按 会话运行时 的 exact parent session mailbox 收口：
root lineage 只表示血缘，不授予 child 读取父收件箱的权限。下一步严格 gate 后部署唯一 Gateway，在同一
session 先补齐这次调研，再连续做两个小追加和多子代理复刻；不能另启测试 Gateway。

终态折叠真机已证明普通 follow-up 可直接回答前一轮 2 次 Read 的数量、成功状态与 exact path，且 provider
分别返回 42,107 与 12,987 cache-read token；手动 Compact 真实提交 generation 1、45,639 → 15,029。
新暴露的缺口只是控制完成没有向 TUI 发布 typed boundary，导致旧 `compact 0` 和压缩前 Context 留屏。
二层候选通过既有 `task_status.compact_generation` 跨 HTTP/operation receipt 传递 canonical 代数，立即撤下
失效 Context，固定显示下次真实模型调用刷新；不额外调用模型、不解析文案、不改 provider cache 协议。
`f901645` 首次部署后又定位到恢复水合缺口：Gateway activity 已是 1，客户端因没有 Working block 丢掉同帧
状态。`ff94d61` 让 idle/resume frame 同样更新 status，且 generation 单调不减。原 tmux 已验证恢复 generation 1、
手动 generation 2、下一真实轮约 28.5k/128k，以及再一普通轮 17,019 cache-read / 3,254 uncached input。

### Leaf 角色行为与共享工作区修改纪律

状态：`85433d4` 第一层已部署；r31 暴露工具缺口，第二层 `1082ccf` 已推送，待当前长任务安全点部署

解决问题：Click→Go 复刻的 worker-2 被分配 examples，却扩到 `internal/core`、覆盖兄弟文件，并在局部补丁
失败后用 `head/write_file/mv/heredoc` 整文件重写。对照 会话运行时 `core/src/agent/role.rs` 后确认，本项目 leaf
runner 把当前 role `prompt_zh` 直接丢弃，worker 实际从未看到共享工作区和职责边界。

当前进展：创建 snapshot 新增当前角色名称/提示，leaf 只注入自己一份；worker 内置规则明确独占分工、
不覆盖兄弟和局部补丁重试。自然语言仍只是软纪律，不增加目录锁、diff 机器验收或 Click 专项分支。
真实 r31 进一步证明提示已经到达，但 child 的工具快照没有既有 `edit_file`，而 `apply_patch` 未命中只返回
路径，模型仍会退回整文件重写。当前第二层把文件写工具收为唯一 canonical 顺序/集合，贯穿直属与递归
child、角色、授权和写围栏；会话运行时 式补丁错误有界回显 exact expected lines，小改优先走空白容错
`edit_file`。显式父级工具上界继续只减不增；11 个文件共 188 项定向回归通过，真 TUI 需部署后另取新 child 验收。

### heredoc 正文不能冒充后台 shell 操作符

状态：`85433d4` 已严格 gate、推送并部署；待真 TUI 自然命中 heredoc 源码正文复验

解决问题：worker 写 Go 源码时，heredoc 正文中的 `&Context{}` 被宿主当成 shell 独立 `&`，返回
`BACKGROUND_PROCESS_MODE_REQUIRED`；模型随后改用整文件覆盖，放大共享目录冲突。

当前进展：后台检测先以 quote-aware shell lexer 找出真实 heredoc opener，只分析 opener 与后续 shell
命令，忽略 payload 和结束标记；注释里的伪 opener 不能隐藏真正的 `sleep &`。独立后台操作符仍 fail closed。

### tmux 3.3a 拖选/右键复制写穿

状态：`85433d4` 已严格 gate、推送并部署；tmux 真实命令已核对，待用户 attach 后实际粘贴

解决问题：`.7` 的 tmux 3.3a 明确显示 `load-buffer` 没有 `-w`，旧实现却调用
`tmux load-buffer -w -`；mock 测试虚构返回 0，导致界面高亮、右键事件和内部 clipboard 都正常，外层系统
剪贴板始终没收到内容。

当前进展：普通 tmux 改用真实支持的 `set-buffer -w -- <text>`；iTerm2 保留无写穿的 stdin buffer 路径，
避免已知 SSH 崩溃风险。应用 clipboard、OSC 52、F6 原生复制均保留，最终外层结果不由自动化冒充。

### 新阶段仍展示上一份已关闭 Todo

状态：`4425618` 的已关闭旧阶段清理已真机收口；未完成项跨普通续话保留的补丁 focused 通过，待 `.10` 部署复验

解决问题：`ma-97468f3-longchain-r27` 已进入独立验收失败后的三路修复，底部 child 面板正确显示三名
RUNNING，固定 Todo 却仍显示上一轮调研的 `完成 4/7`（实际 4 done + 3 skipped）。这是旧 task-path 计划
投影，不是当前修复进度；不能按标题猜阶段，也不能清掉仍属同一 active turn 的真计划。

当前进展：对照 会话运行时 `update_plan` 的 exact turn notification，canonical task-path 账本保留全部历史，新增
host-owned `display_plan` 只列当前 conversation request 的 exact item ids。同一 lifecycle wake 继续原代，
下一普通用户回合换代；TUI 在 dequeue 时先清旧 Todo，并按代次/修订拒绝迟到 activity/notice。与此同时按
终端交互 `repinScroll` 补了 prompt_toolkit Window 的显式垂直滚动落点，提交与页面切换能真实粘底，手动
上翻仍不抢滚动。362 项 focused（2 xfail）及严格 gate 通过；原 `ma-97468f3-longchain-r27` 原位 resume 后，
两轮追加均清掉旧 `完成 24/35`，PageUp 离底可见 `Jump to bottom ↓`，下一次 Enter 立即回底且 Working 收口。

`.10` 的 `ma-110-native-cache-r2` 又暴露相反边界：同一长期调研还有未完成 Todo，用户续话后模型只增量更新
3 个 id，TUI 错显为 `完成 3/3`，而主账本 17 项仍完整。当前候选在换代时保留所有 canonical 非终态 id，
再合并本轮显式触碰 id；上一阶段已关闭且本轮未触碰的历史仍会消失。它不解析“继续”等自然语言，也不让
Todo 取得任务完成权威。task-progress、activity、notice、派工与 TUI 八文件 167 项 focused 及本地严格
gate 已通过；待部署后用普通中文续话核对四行窗口仍含进行中和下一待办。

### DNS 瞬断不能终止长代理

状态：本地候选失败优先回归通过，待完整 focused、严格 gate、推送与 `.7` 同 TUI 复验

解决问题：复刻 child 已运行近两小时、Compact 3 次，一次 typed DNS 解析失败便终止，前面全部上下文和
工具进展只能以失败交接。长任务不能把 resolver 的一次瞬断当成永久 api_base 配错。

当前进展：对照 会话运行时 retryable `ConnectionFailed`，异常链中的 typed `socket.gaierror` 复用现有
2/5/15 秒三次 HTTP 退避，耗尽后进入既有模型轮恢复；不解析错误正文、不放宽认证/额度/代理或畸形 URL。
普通/流式两个定向回归均已先红后绿。

来自 STATUS.md，子代理可进入视图、精确控制以及插话排队/公开回复已转入 COMPLETED；当前继续正式矩阵与
仍缺直接自然样本的生命周期分支。

### 父级生命周期收件箱接收者隔离

状态：本地候选 focused 通过，待严格 gate、推送与 `.7` 同一长 TUI 复验

解决问题：多个 child 并行时，每个 task-local 回合都携带共同的 root task id。旧安全点把这个血缘 id
误当作收件箱接收者，导致运行中的 sibling 能读取并 ack 主代理的 completion wake；canonical child 状态
虽全为 `DONE`，root 实际只看到残缺报告，却仍可自然误判完成。

当前进展：`runtime/guidance.py` 只允许 `default/conversation` 主代理作用域读取 root lifecycle mailbox；
`task_local/control_plane/isolated` 均返回空，direct child 插话仍独立走 exact `agent_run` guidance。
新增回归先在旧代码稳定复现失败，再证明 child 无法注入/确认父级 wake，而同一 wake 随后仍可由 exact
conversation parent 注入并确认。`test_runtime_guidance.py` 与后台 mailbox focused 已通过。

### 批量派工目标去重

状态：本地候选 focused 通过，待当前真实 child 结束后严格 gate、推送与 `.7` 真 TUI

解决问题：模型已经为每个并行 child 填好完整 `items[].goal`，旧 schema 仍要求再写一遍整批顶层
`goal`，真实 MiniMax 调用因此在 handler 前失败并多耗一个自纠回合。

当前进展：根与递归 coordinator 均改为“单个非空 goal 或非空 items”；批量每项 goal 继续原子校验，顶层
goal 仅作可选说明，两种形态都缺失时仍返回 typed 可恢复错误。对照代码为 会话运行时
`multi_agents_spec.rs` / `multi_agents_common.rs::parse_collab_input`；三个 focused 文件 65 项通过。

### 完成邮箱有界排空

状态：本地候选定向回归通过，待严格 gate、推送与 `.7` 真 TUI

解决问题：七个直属 child 都已 `DONE`，旧 successful-completion selector 却在上下文投影前按 token 预算
截成五条；root 只读五份报告便因 canonical 树全终态而关闭任务，另外两条完成通知随后被当成晚事件消费。

当前进展：对照 会话运行时 `forward_child_completion_to_parent` 与 session input queue 的逐项 mailbox 交付，同一
exact root 的 pending DONE 信封按条数/token 分成有界 active wake；未选成员保留到下一轮，并由采样快照阻止
安全点提前消费。root closeout 与用户最终投递共同等待邮箱排空。4k 总预算、prompt limit=5 的七路长结果已
跨多个模型轮逐条读取，只有最后一轮对用户投递一次。

### 当前任务 Todo 显式读别名

状态：底层候选 focused 通过，待严格 gate、推送与 `.7` 真 TUI

解决问题：长任务 child 完成后的后台 root 明明有完整 Todo，却把当前 `gwreq` task id 显式传给
`task_progress(read)`；旧工具把它当另一份历史账原样读取，模型收到 0 项，而 TUI 仍显示原 18 项。

当前进展：已从真实 tool-output index 证明调用参数和空返回，对照 会话运行时 `update_plan` 的 session/turn
定域，把“显式 id 精确等于当前 typed task id”收为当前 canonical task-path 账本别名；其它历史 id
保持精确读取。不解析 id 前缀或正文、不新增账本、不恢复机器验收。两个 focused 文件当前 16 项通过。

### 子代理完成详情引用与安全状态面一致化

状态：第二层本地候选严格 gate 通过；待推送、`.7` 同一长 TUI 复验

解决问题：普通前台续轮现在能直接收到并整合直属 child 的有界 `completion_message`，不再猜目录；但模型
若按信封里的 `final_report_ref` 深挖完整报告，当前 `Read` 会把该路径识别为内部 agent 状态面并拒绝。
当前决定不复制第二份生命周期事实源：系统生成的精确
`work/agents/<run_id>/final_report.md` 本身已经是有界交接投影，只含 task/run/status 与 child 最终回复，
因此只允许 `read_file` 读取这一文件。`canonical_state.json`、checkpoint、summary、progress、目录枚举和
shell 读取仍按内部状态面拒绝。后台续接还必须把成功副作用的 typed operation/execution 事实写入耐久
工具索引，避免 child 完成后的新工作片把已成功派工误降为 unverified，导致用户已收到最终回复而 root
active task link 仍不关闭。

当前进展：`4106025` 已推送部署，原长 tmux r27 证明 operation 字段本身已落盘，却暴露 foreground request
与 durable task id 的第二层错位；模型还把 exact run id 拼进过期 task 目录。当前候选把 typed
`conversation_request_id` 沿 child state/completion wake/index carry 传到底，旧行只按同值 request id
兼容；错误 task 目录只可经同 owner canonical agent projection 找回 exact `final_report.md`，并再次过读
权限门。5 个直接相关测试文件共 206 项，结果 204 passed、2 个既有 xfailed；Ruff、doc-sync、strict
code-size、diff 与 clean-package 均通过。推送、`.7` 单 Gateway 部署和同一 tmux 复验尚待完成。

### 子代理业务产物与宿主交接文件隔离

状态：第二层本地严格 gate 通过，待推送和 `.7` fresh child 真 TUI

解决问题：r31 的 Shell 补全 child 已经完成业务代码，却又多调用一次 `write_file` 去写内部
`final_report.md`；随后宿主用 child 的最终回复覆盖同一文件。这不是业务工作，而是 output contract、task
packet、workspace refs 和执行提示把宿主收口路径暴露给了执行模型，既浪费模型/工具轮，也容易让它把内部
状态文件误报成用户产物。

当前进展：对照 会话运行时 `forward_child_completion_to_parent` / `last_agent_message`，child 现在只看到父级或
用户明确声明的业务文件与写入根；`final_report/output.json/runner_result` 全部留给宿主在自然 final 后生成。
旧持久 bundle 即使仍含这些键，runner prompt 也会过滤。显式业务产物本身恰好叫 `final_report.md` 时仍按
`required_file_refs` 正常交付，不按文件名猜内部状态。进一步审计发现完整 execution-context ref、write
boundary 与 recovery refs 仍是旧旁路；当前只把安全后的 context bundle 给模型，恢复清单只留
checkpoint/summary/task，宿主完整账本仍保留。父—子—孙完成、prompt、context 和持久化相关 102 项通过；
全量 Ruff、doc-sync、strict code-size、diff 与 clean-package 同时通过。当前 r31 运行的是旧 Gateway，只保留
失败基线，不能冒充部署验收。

### 会话运行时 式依赖派工分批

状态：模型合同候选 focused 通过，待严格 gate、推送与 `.7` 真 TUI

解决问题：同一长 TUI 中，主代理明确说“先修复、再独立测试”，实际却把 worker/tester 放进同一个
`create_subagents.items`。宿主按既有合同立即并发，tester 比 worker 早 27 秒结束，造成“独立修后验收”
名义成立、时间顺序不成立。

当前进展：已对照 会话运行时 `multi_agents_spec.rs`，把 items 收紧为模型可见的独立并行合同：后项必须读取前项
未来结果时，先只创建前项，等 typed lifecycle wake 后再创建后项。自然语言“先后”不产生机器顺序；
不解析 goal/role、不新增依赖状态机或机器质量验收。两个 focused 文件 15 项通过。部署后继续使用同一
长 tmux，以普通中文要求“先修复、再派独立测试”，核对 child 的真实创建/终态时间顺序。

### 会话运行时 式并行编码互斥写入范围

状态：第二层本地候选 59 项 focused 通过，待严格 gate、部署与同 TUI r29 复验

解决问题：当前 Click→Go 复刻的多个 child 虽有不同职责标题，却同时写旧目标的同一组参数文件；宽职责
child 还覆盖多个兄弟模块。三路运行到 230+ 轮仍互相制造编译错误，既浪费 token，也使最终产物不可归因。

当前进展：第一层已对照 会话运行时 把“共同目标目录 + 每项独占文件/模块范围 + 重叠职责分批”写入唯一模型
说明。部署后 r28 又主动声明 `/internal`、`/internal/core`、`/internal/param` 并被放行，三个 worker 数分钟内
写出重复 API。第二层只比较同一次调用中主动提供的结构化 `output_files`：相同或祖先/子路径整批
`not_started` 并返回 exact conflicts；省略时不猜，不恢复完整写集、权限、锁或业务验收。59 项 focused 通过。

### 会话运行时 式同一父级分批创建

状态：`fb75b68` 已严格 gate、推送并部署；fresh r9 后台第二批通过，活跃 sibling 间的第二次独立调用待自然样本

解决问题：后台 main 在第一批完成后会继续原任务；若第二批先创建一名仍在运行的 child，旧硬门会把
其余不同职责全部当作“重复 lineage”拒绝，哪怕会话容量仍有空位。当前候选按 会话运行时 spawn 语义删除该门，
同一 canonical parent 可多次创建到容量上限；重复请求由 typed idempotency/work-scope 复用，双执行器由
active-turn claim 拒绝。当前默认容量已进一步收口为单一会话 8 槽，单次额外限制默认关闭；fresh Prompt 4 必须看到第二批多名 child 在首名
仍运行时继续成功创建，并核对没有重复 run、没有额外 Gateway。r9 已由一个 items 调用创建第二批 3 名，
没有错误或额外 Gateway；因为三名在同一原子调用中创建，它不等价于“第二次独立调用遇到活跃 sibling”，
后者当前由 focused 回归覆盖，继续等待真实任务自然触发。

### 会话运行时 式子代理切片续跑提交桥

状态：`31648ce` 已严格 gate、推送并部署；fresh r8 四名长 child 自然完成，待自然截断直接样本

解决问题：子代理一轮在 max-token/context 截断时，宿主会先正确关闭 exact AgentAttempt，
再把逻辑 child 交回 PENDING 以便续跑。旧 MANAGED 结果栅栏只接受 running attempt 或 run 级终态，
将该顺序当成冲突，造成 session/attempt 已结束而 TUI 永久 RUNNING。当前候选只接受 exact current
generation 且与 typed `turn_end_reason` 严格对应的 PENDING/BLOCKED；终态仍要求 run 级事实。
fresh Prompt 4 需自然观察一次截断后新 generation 立即启动，同时确认父级自然唤醒。

### 会话运行时 式工作区并发与双 TUI 对照

状态：`f07c647` 隔离回归与 `53498c1` HTTP 固定工作池均已推送、部署 `.7` 并通过真 TUI

解决问题：旧 `resource_locks` 把 `/root` 与所有子目录当成跨任务独占资源，死进程留下的
过期行可以使 `run_command/create_subagents` 在 handler 前永久失败。当前实现按 会话运行时
turn/cwd 语义过滤所有普通 `workspace:*` 持久锁，保留 operation ledger、精确逻辑锁、
owner 墙、写边界与沙箱；`output_files` 降为交付/验证元数据，不再拒绝共享 cwd 的父子代理。
现有 ModelCallLedger 同时累计每个 request/run 的 provider input/output/cache-read/cache-write；
详细记录被裁剪后总账不截断，供应商没有 usage 时明确标记为估算，不拿 TUI 当前上下文冒充成本。
部署后使用单 Gateway、MiniMax-M2.7 和用户给定的 3 条长任务 + 4 个跨语言复刻任务，
与 会话运行时 同题对照耗时、子代理、Skill、Compact、token、LOC、可运行性和独立验收分。

当前进展：`.7` 已确认唯一 Gateway、有效模型 `MiniMax-M2.7`、真实模型调用成功，以及 owner-local
durable path 和不同 cwd/session 隔离。首批 Prompt 1/2 配对 TUI 暴露新的非锁故障：旧客户端每
250ms 请求活动快照，而标准库接入队列只有 5，历史 TUI 重连可把唯一 Gateway 挤满。当前实现已对照
会话运行时 的事件通知/有界慢连接原则，将 backlog 提高到 128，健康刷新收为 1 秒，失败按 0.5 至 8 秒退避；
部署后先把这批故障样本标为 diagnostic，再在新目录重跑同题，不混入正式评分。2026-08-24 旧 Gateway
进一步被 OOM killer 以约 6.68 GB RSS 杀死，8 个存活 TUI 在十分钟内已产生约 3,300 个短命 HTTP request
thread。`53498c1` 按 会话运行时 容量 128 的 bounded channel 适配为 16 个复用 daemon worker、128 总在途
请求上限与 typed 503 退避。`.7` 已以唯一 Gateway `604186` 验证：fresh TUI 约 1 秒启动、MiniMax-M2.7
真实调用成功，9 个 TUI 自然轮询只生成 5 个 `gateway-http_*` worker，旧 request thread 为 0，RSS 约
132 MB 稳定。下一阶段回到用户给定长任务/会话运行时 对照矩阵，不再把本次 transport 故障混入正式评分。

### 普通计划的 会话运行时 式自然结束

状态：2026-08-28 已删除 Todo/operation 的普通 final 后置机器判官，focused 通过，待真 TUI 复验

模型自己留下 open Todo、测试失败或命令非零时，Todo 与 operation ledger 会继续作为下一次模型采样、
Compact/resume 和 TUI 的结构化事实；它们不再覆盖 plain final，也不自动追加 provider call。历史
`plan_closeout.py`、`completion_conflict.v1`、写后验证 followup 和 `OPERATION_INCOMPLETE` 续跑已物理删除。
显式 `/goal`、直属 child 等待、UNKNOWN 副作用、危险路径、权限和幂等仍由各自结构化合同守住。

直接证据是 `.7` 请求 `gwreq-1787921095-5775e23884254defb09f0dbafc7ef1ef`：用户故意要求 `exit 7`，
旧门额外消耗 5 次模型调用、42.6 秒和 141,905 cache-read tokens 后仍显示 Working。候选代码的 fake
provider 回归要求相同场景只发生“工具调用 + 模型 final”两次采样；下一步在唯一 Gateway 的 fresh TUI
原样复测非零退出码，再继续后台进程、网络与中断矩阵。

### 单 Gateway 多 TUI 项目目录与正式提示词 2

状态：`993ce4f` 已部署并由原样提示词 2 真机证明前后台 cwd 一致；转入持续回归

解决问题：从非 Gateway 启动目录运行 TUI 时会误找另一套 cwd-hash 队列，等待一分钟后退出；即使只放宽
readiness，模型和工具仍可能在 daemon cwd 工作，造成项目写错位置。

当前进展：服务目录已固定为 owner 级唯一 Gateway，客户端 cwd/roots 改为 thread v6 的 typed 状态；
前台、后台、Tool Gateway、任务交付和 child 输出必须共用该值。`a091b72` 部署后原样提示词 2 已在一个
Gateway 中创建 child，但首个 ask 因薄 TUI 的 audit Agent 为空而漏传 workspace，产物写到 `/root/bbb`。
后续候选把 audit 与 workspace 分参，首个 inbox JSON 回归已证明携带 client cwd；同时 child 行改为职责
短标题、实时上下文 token 和单行宽度预算。`0ffbfe4` 部署后的正式 TUI 已证明 workspace 与三条 child 行
正确，但 Todo 仍重复完整 child goal；`669c228` 已按 exact run id 只在视图隐藏重复 seed。继续观察又证明
自动 wake 的 `run-*` 丢失 thread cwd，relative child output 回落 `/root`；`993ce4f` 在后台 turn 构造处
恢复同一 thread 快照。全新 tmux `dsh-p2-mario-993ce4f` 已证明首次 ask、后台 main、6 个 child 与工具
working_dir 全部保持 `/root/dsh-tui-p2-993ce4f`，最终 `bbb/` 产物齐全且 HTTP 200。该项不再等待专项
补丁；后续四个正式任务仍持续对账，发现回归时只修 thread/turn workspace 主链，不增 cwd fallback。

### TUI 活动状态、跟随滚动与 Compact 真机复验

状态：直属 child 活动区已部署并完成真实 TUI smoke；submit 回底与 Todo 额外 child 提示为本地候选，
其余交互矩阵和 process-local lane 自愈待验证

解决问题：旧 TUI 在长任务运行时把普通 Enter 当成下一轮队列，用户补充消息要等当前任务结束才执行；
queue preview 又位于可滚动 transcript，离开尾部后看不见。并行实验室若完成后长期空闲，也会浪费四路
换时间的测试目标。

当前进展：ordinary input 和控制操作已使用稳定 message/operation ID、exact expected turn、持久 outbox、
accepted/rejected/unknown 三态和 GET-only reconciler。对照 终端交互 `repinScroll` 的本地候选把一次有效
用户提交定义为 return-to-live：当前 main/child viewport 立即回底并恢复 follow；被动新输出继续不抢用户
上翻位置。Todo 全部打钩但仍有未映射的 typed active child 时，标题只读补充“子代理运行中 N”，不修改
账本或猜任务关系。2026-08-20 的灰色思考、灰色 `Ctrl+O` 提示、
tmux 左键复制已部署 `.7`；右键直接复制仍是本地候选。2026-08-21 本地又补齐 Working 动画、仅在用户
原本位于页底时自动跟随、后续 thinking 增量持续显示，以及 typed Compact 活动块和 5/15/52/78/92/100
阶段进度。输入后续消息时也会保留可见回执；只有被动更新保持离底，用户实际提交会明确回到底部。
真实植物大战僵尸复验又暴露同 thread 旧后台 run 错挂新任务权威链、后台 notice 复用 block id 被丢、
用户目录只写在 child goal 而未进入结构化权限，以及创建后周期性 LLM 巡场。当前已改为 task-bound
后台 run、notice 立即/每秒独立块、`items[].output_files` 完整 schema，并删除自动巡场与 wait 工具；
`send_guidance` 也收为只能向一个直接 child 插入消息。
首轮原样任务还证明 OPEN capability request 会被通用 completed 收尾误写为 DONE，grant 后原 run 没有
重新排队；模型因此误以为 child 已结束并自己接管。当前已改为结构化 OPEN 优先、grant/deny 后同 run
自动续跑，普通 child 逐层继承父级工作区；旧 `inspect_agent_tree` 工具类与 Schema 也已彻底删除，内部
代理树只服务 `/status`、TUI 和恢复诊断。
`e321483` 原样 TUI 又确认模型首轮看不到 `create_subagents`：0 child、13 次 Bash、10 次 Write，
导致“嘴上派 5 个，实际自己写”。本地已从默认渐进披露中移除 `orchestration`，按 会话运行时
multi-agent v2 使递归代理控制首轮直出；待二次部署和全新 TUI 复验。
`c2c0235` 二次部署后已真实创建 4 个 child，但其中 2 个因路径规则误判反复写失败超过 17 分钟；
宽 `/root` forbidden 错误压过 task output 的窄 allow，grant 同一路径也无法改变当前尝试。状态投影又读错
ToolResult 字段，只有空 `RUNNING/0%`。当前候选按 会话运行时 最具体路径优先修复，并记录不含正文的模型/工具
活动；同时彻底删除重复宿主生命周期的 `raise_event` 模型工具，历史 grant 也不能复活它。

`d257dfb` 已推送并部署 `.7`。新轮 3 个 child 全部自然 `DONE`，实际写出 8 个游戏文件，
没有 `WRITE_FORBIDDEN` 或重复 capability request，活动状态也已显示模型/工具阶段。但最后 child wake
排队超过 150 秒仍未唤醒主代理：同 `local/main` 的另一旧 TUI 正在跑长
`ordinary_task_resume`，owner 级唯一 tick 把所有会话错当成一条车道，并且旧任务还抢走了一个 child 名额。
当前本地候选已对照 会话运行时 的 per-thread active turn，拆成无模型准备、ready-thread 规划和单 thread 消费，
以 `(owner, thread)` 去重、同 thread 持久 claim 单飞、不同 thread 有界并发。真实 scheduler 阻塞对照回归已通过；
待严格 gate、推送部署和全新 TUI 最终复验。

`bea6fed` 的后续原样轮已拿到四名 child 全部 `DONE`、完整产物和 HTTP 200，但仍耗时 422.849 秒、
32 次模型调用，累计输入估算约 19.47M tokens。新定位的主因是 task-local 父级在等孙代理时被孤儿器
误复活，以及孙代理成功事件越级逐条唤醒根会话。当前本地候选改为精确直属等待和递归事件链：创建后
typed 让出，同批成功只恢复一次，失败立即恢复，父级直接收到 `direct_children` refs；同时删除
`task_progress` 普通任务自动续跑，使 open 软清单不再制造额外模型轮。待严格 gate、推送部署后，用
同一原样 TUI prompt 比较模型调用数、耗时、子代理终态、产物与 8080。

`d928d77` 后续原样轮已把递归事件链验证到位：四名 child 都只运行一次并自然 `DONE`，最后一个完成会
自动唤醒主代理，无需用户发“继续”；受管服务真实监听 `0.0.0.0:8080`，loopback 与局域网 HTTP 都为
200。仍未通过的是交付位置和可观察性：`/root/abc` 为空，页面实际落在 task 内部 `output/abc`；child
上下文混入旧 `/root/kill-ws/...` read pack；前台让出后 TUI 没有持续 Working；普通 leaf 还收到四个
无法使用的下级管理工具。当前本地切片已改为裸相对路径继承 cwd、只有显式 output/work 前缀进入 task
内部目录，shared context 只取当前轮，Gateway/TUI 用 canonical active task count 显示一个可移除的
Working 块，并按 `can_spawn_children` 裁剪 leaf/coordinator 工具面。
当前轮继续对照 终端交互 `CoordinatorTaskPanel` 收口：Gateway 从 active task link 和
canonical subagent run 生成有界直属 child 快照，TUI 在输入框下固定显示每个 child 的名称、状态、
职责短标题、耗时、当前上下文 token 与 Compact。固定后缀先占宽度，短标题按剩余列截断且永不换行；
该区域不进 transcript，不影响用户上翻，也不作完成或重试权威。

`f5dc695` 的新鲜 Prompt 2 轮证明 durable wake 创建到后台 claim 约 7.56 秒，长等待主要来自 main 被
唤醒后又做了已委派实现。当前候选继续收口：批量顶层 description 不复制给每个 child，逐 item schema
明示独立职责；main/child 都严格单行；final notice 与 live Todo 共用 exact run-id 去重；会话运行时 式
coordinator 在委派后只整合、测试和汇报，缺口继续交给 child。8 个直接相关 focused 文件已通过，待严格
gate、部署和下一条原样 MiniMax-M2.7 TUI 复验。

`84c6b90` 的 fresh Prompt 4 r6 已证明 root Todo 在四名 child 运行期间持续可见，四名 child 最终全部
DONE；但 root 在后台整合轮又亲自调用 `edit_file/write_file` 修复功能，且最终 244 tests/build 只覆盖
TypeScript library，`dist/index.js` 没有 fzf 可执行入口。当前本地候选把 会话运行时 no-duplicate-work 规则收口
到 root 工具说明、递归 coordinator prompt 和结构化创建回执；同时将后台 main 原先单行摘要扩成有界 typed
过程事件流，复用现有 终端交互 风格 thinking/tool/diff renderer。focused 已证明 `Update + Added/removed +
红删蓝增` 可从后台链路显示且游标不重放；待严格 gate、部署后以全新 tmux 原样 Prompt 4 真机同时复验
“root 不自写”和“后台两分钟工作期间持续可见”。

同一长会话 `ma-97468f3-longchain-r27` 的 Rust 复刻又证明富事件流本身会被逐 token thinking 淹没：Gateway
工具索引持续前进，TUI 却数分钟停在旧工具，1024 条 ring 已裁剪后才一次性补画。当前候选按 会话运行时 reasoning
buffer 和 终端交互 状态累积/帧级刷新，把首片之外的同块 delta 按 0.25 秒或 256 字符合批，终态完整正文
继续兜底；47 项 focused 已通过。待当前真实复刻阶段自然结束后部署唯一 Gateway，并在同一 session 的下一条
普通追加中核对事件数、首屏延迟和持续思考可见性。

后台进程工具名与续接入口已收口为 scope-aware `process_session(list/status/wait/stop)`；但 fresh r52 又证明
旧进程所有权仍在一次性 child runner：child DONE 后 bwrap 按 `--die-with-parent` 杀掉服务，主代理却按旧
启动回执误报运行。当前候选对照 会话运行时 Session process manager，将原 bwrap 的父进程改为 detached managed
host，并把 owner/conversation/PID 出生指纹/终态写到 owner 沙箱外唯一记录；不采用 终端交互 的 worker-exit
即清理语义，也不自动重放命令。`8f50d19` 的 140 项直接相关回归与严格 gate 已通过并部署一个 Gateway；
fresh r53 已证明 child/root 工作片结束后端口仍在、另一进程同 scope 可查询停止、错 scope 不可见、stop
收掉完整树。本项转入 COMPLETED。r53 同时留下下一项：主代理在“只协调”任务中仍亲自执行 mkdir，需要
继续对照 会话运行时 coordinator/no-duplicate-work 的真实 prompt 和工具选择，不增加任务专项硬门。

子代理详情的真机回看暴露的三项同源缺口已由 `91a1c3d` 收口：详情首条改读完整 `task.goal`，非 callable
child sink 先按 typed `write_progress` 接工具事件，`Ctrl+O` 改为冻结 active runtime。218 项直接 focused 与
本地严格 gate 通过后已推送并部署 `.7` 唯一 Gateway。公开 tmux `ma-91a1c3d-child-full-r17` 使用原样 Prompt 2，
实按 `↓/Enter/Ctrl+O/Home/Ctrl+G` 后显示 child 完整 prompt、灰色思考/过程、工具卡和代码内容，展开未跳 root；
三名 child 的 JSONL 均已有 tool started/completed。界面切片已完成，超级玛丽产物仍由被测任务自行继续。

R149 本机 fresh TUI 重新验证上述主链，并把 `apply_agent_view` 的严格尺寸 hard finding 清零；同时发现 child
进入 Ctrl+O 详细 transcript 后 Ctrl+G 没有 modal binding。R151 已按 mode/global key ownership 补齐
Ctrl+G/Alt+Left，两态都走同一 typed back；真实按键一次返回 root，同时保留详细模式。Esc 继续只停止当前
代理，没有改成返回。本项转入 COMPLETED。

同一 tmux 后续暴露“进入 child 再返回后，鼠标上翻像没有历史”。现场 `Ctrl+Home` 能立即显示 root 首条原始
Prompt，证明 canonical history 未丢；根因一是普通/modal control 复用全局 cursor 并在每次切换时强制 End，
二是该轮采用的默认原生复制模式在 alternate screen 中不会把滚轮交给应用。`d14549c` 已按 exact store 保存
每页 follow/cursor/unseen，首次进入才默认尾部，返回恢复原位置并清跨页选区；这些 viewport 合同继续有效。
后续 `0da26f0` 根据 终端交互 主链把默认恢复为 mouse tracking/wheel，F6 改为原生复制逃生口，并在 `.7`
fresh `ma-0da26f0-终端交互-history-r23` 证明 root/child 默认滚轮和回底 follow；旧 r18 的 F6 证据只保留为
双模式切换历史，不再代表当前开箱行为。

`931ee20` 已完成上述展示复验：四条短职责、实时 context、单行 main 和终态标记均正确。新阻塞转到底层
完成交接：旧 wake 没带 child 最终回复和 `final_report.md`，main 因而猜错目录。当前候选按 会话运行时 标准
completion message 补 `subagent-completion.v1`，同根 DONE 信封一次合批，并让 active wake 在上下文压力
下优先保留 metadata/报告 refs。`fd7d2b9` 部署后的全新 Prompt 3 已证明首批结果能被 main 读取并继续
派第二批，但最终只创建 6/8 个 child，漏 轻量运行时/通道运行时，并误报已有 终端交互 结果缺失，正式任务仍失败。
下一切片先从结构化批次覆盖、完成事件与 result refs 查清原因，不用用户插话或任务名专项提示补救。

该 Prompt 3 的下一层事实已经查清：6 个 completion envelope 都完整且分别被 main 消费，遗漏不是 wake
丢失；canonical `task_progress` 以 `task-path:<目录指纹>` 保存 8 项计划，后台上下文却按 durable request id
读到了另一份空账。当前本地候选把路径指纹算法收口到 `runtime/task_identity.py`，后台 Task Runtime
State、child 终态同步、Goal 续跑和 TUI 投影全部复用同一编号。普通清单依旧不充当机器验收或自动续跑门，
只确保模型每个后台轮都看得到自己尚未完成的计划。待 focused/严格 gate 后部署，再用全新 tmux 原样重跑
Prompt 3，重点验收 8 个目标全覆盖、最终横向汇总及 Todo 终态。

`ac1f4dc` 已按 终端交互 `TaskListV2`/全局 animation frame 收口默认任务摘要并部署 `.7`：固定四条，优先
最近完成、当前运行和下一待办；`Ctrl+T` 展开全部。常驻 Context 去掉不直观的协议构成，只保留总量、
占比、canonical Compact 次数和明确命名的自动压缩点；prompt/messages/tools 留给 `/context`。定向回归
已通过，待严格 gate、部署及正式任务中的动画/按键/次数真机复验。全新 Prompt 3 已证明默认 `4/9`、展开/收起和
`compact 0` 生效；原始 Todo 仍全是 pending，因为模型派工没有传 typed `covers`，后续应加强结构化绑定
而不是按标题猜任务对应关系。

同轮四名 child 中 代理运行时 在读取内部 child 状态文件时被 `WRONG_STATUS_SURFACE` 正确拒绝，但旧
ShellTool 没声明“进程未启动”，operation coordinator 因 run_command 的潜在副作用把它保守升级为
`TOOL_OPERATION_OUTCOME_UNKNOWN`，导致整个 child 回合 interrupted/PENDING。当前候选只在这一条
执行前拒绝上补 `effect_outcome=not_started`，保持安全门不变，让错误回到模型换正式结果引用；真正未知
副作用仍 fail-closed。待 focused gate、部署后用新的原样 TUI 任务复验 child 不再因此挂起。

`714c0c8` 已推送并部署 `.7`，本地 117 项 focused、远端 11 项投影/渲染 focused 与提交前严格 gate
均通过，按用户约定未重跑全仓 pytest。真实 TUI 已证明两个 child 一次 attempt 自然完成，固定活动区能
逐步显示状态、动作、耗时并在根任务收口后移除。该轮也抓到旧 Gateway 进程保留 process-local thread
lane 占位的样本：durable wake 与 ready 事实均正常，但重启前未领取，优雅重启后立即领取并约 30 秒自动汇总。
待做：补齐 lane queued/running age 诊断与有界自愈，再验证 child 完成后父会话无需重启且不受其它 TUI 长回合阻塞；继续验证 Working 动画、thinking 连续增量、页底自动跟随、
离底不抢滚动、Compact 百分比、后续消息可见、右键复制和输入框粘贴。外层系统剪贴板仍须用户在已 attach
的本机终端亲自粘贴确认，自动化不能冒充这一步通过；同一普通中文游戏 prompt 还要证明 child 自动
完成后父级无需用户发“继续”，能读到产物、整合到 `/root/abc` 并实际监听 `0.0.0.0:8080`。

边界：当前测试机为 `192.0.2.7:/root/my-agent`；保持单 Gateway、多 TUI、tmux 观察会话和远端
key/config/runtime 数据，不触碰其它项目。较早大切片已按约定执行过一次全仓 pytest；当前切片远低于
10,000 行，只跑相关 focused tests 与远端提交前严格 gate，不重复浪费时间跑全仓。

### 主/子/孙代理统一 Conversation Compact

状态：统一账本与摘要顺序已真机触发；正在修复同批成功后旧失败闸仍误杀 child

解决问题：子代理也可能连续工作数小时。旧实现中主代理用 owner/thread 的 Conversation Compact，
task-local 子代理用 run workspace 的旧 compact continuation，回合内工具 IR 又独立裁剪；这让次数、
恢复、checkpoint 和失败熔断表现不一致，并出现过 child 实际裁剪后仍显示 `compact 0`。

当前进展：已对照 会话运行时 `session/turn.rs` 与 multi-agent spawn，并完成每个 child/grandchild 独立
ConversationThread 接线。创建时以 exact `agent_thread_id` 建立无通道绑定线程；每次 child 尝试按统一
`conversation_request_id` 写 user/assistant；轮前 transcript 由 `conversation/compact.py` 提交，运行中 native
IR 与 provider overflow 由 `conversation/live_tool_compact.py` 适配到同一个 checkpoint/CAS、失败熔断与
summary 权威，注入和 raw tail 仍与 main 共用。
task-local 工具/Memory/写边界保持不变，只有 durable Compact owner 改为 ConversationStore。
`774c7fe` 部署后，全新 tmux `dsh-p3-774c7fe-compact` 的原样 Prompt 3 已证明首轮和终屏都是
`压缩点 90%`；8 个 child 全部 DONE，最高上下文 98.6k，未达 115.2k，所以真实 child
Compact 触发仍需下一个更长任务复验。同轮 main 最终只整合 7/8 且 Todo 未勾选，
作为结果批次覆盖与 typed covers 绑定的独立失败样本保留，不计入 Compact 通过。

第三轮原样 Prompt 4 已在 `jesseduffield/lazygit` 固定提交（61,258 功能行）真实触发 child native IR
裁剪：worker-6 从 113.1k 降至 35.5k，但 exact child thread 仍是 generation 0、checkpoint/summary 为空。
测试已用结构化 `/stop` 保存现场，确认根因不是 TUI，而是 `_tool_loop_service` 将成对删除放在
ConversationStore 账外。

本地收口：正式 child runner 不再生成 task-local `compact_applies/continue`；允许持久化的 main/child/
grandchild 在 native IR 阈值或 provider overflow 时，先把上一代 summary 与当前工具历史合成完整替代摘要，
写 `source_kind=live_tool_ir` checkpoint，再以同一 generation CAS 提交；transcript cursor 不动，工具往返数
单独累计。摘要/checkpoint/CAS 失败会恢复压缩前 IR/tool-context 并进入同一 failure circuit；no-save 辅助回合
只保留不计数的临时事件。TUI/Web/SQLite 继续只读该 thread generation，不新增前端计数器。

第四轮全新 Prompt 4 已证明上述账本真实生效：worker-3 在 119,295 tokens 自动提交 generation 1，
`source_kind=live_tool_ir`，44 对旧工具往返被 checkpoint、9 对保留，模型窗口降到 36,586 且仍运行。
但 checkpoint summary 只有一句普通后续动作，证明摘要 prompt 被 backend 放在历史最前后被 MiniMax 忽略。
现场已 `/stop`。`83faddb` 已改为真实任务 user 在前、native history 居中、synthetic Compact user 指令
最后，与 会话运行时 `run_compact_task_inner_impl` 的 `history.record_items(compact prompt)` 顺序一致。

r4 全新 TUI 已证明该顺序修复：worker-2 generation 1 从 116,644 降到 37,483，2,150 字符 summary
保留目标、路径、完成工作、错误和下一步，并在 checkpoint 后继续创建文件。该轮同时抓到独立底座问题：
worker-3 前部因 sibling 项目根锁得到 13 次 `TOOL_OPERATION_BUSY_CONFLICT`，后部同一批 3 次写入已成功，
旧 repeated-failure halt 仍强制结束 child 并留下 PENDING。当前本地候选对齐 会话运行时
`RespondToModel`：默认可恢复失败只返给模型换路，不再按次数结束 turn；同批后到的同工具成功会撤销
早到的 active halt，只有显式 typed hard policy 才能硬收口。43 项 focused tests 已通过。

r5 已在全新目录与 tmux 中只投递一次原样 Prompt 4：7 个 child 全部自然 `DONE`，没有重复失败误杀；
至少一条 child Compact 账本与 r4 已有证据保持成立。但 main 在 Todo `4/18`、自己明确知道大量功能缺口时
仍 final，产物只是 3,210 行 Pre-Alpha 子集。现场同时出现三套同义 Todo、两个单独补派 worker 同名。
这证明下一层问题是模型执行上下文与 typed 计划绑定，而不是应该恢复机器质量验收。

`b7d513c` 已推送并部署后，r6 在 tmux `dsh-p4-lazygit-r6-ea91639` 对固定
`jesseduffield/lazygit@ea916395` 只投递一次原样 Prompt 4。10 名 child 全部自然 `DONE`，其中 3 名真实
Compact 一次；main 等首批结束后又自主补派修复/测试 child，Todo 最终全部打钩并自然 `DONE`，没有用户
发送“继续”。这证明软持续纪律、事件唤醒与 Compact 主链有进步，但任务仍判失败：三套并列输出未整合，
canonical Python 产物只有 9,543 行功能代码、55 个空桩，`App.run()` 仍是 `pass`，8 项所谓集成测试全部
无条件 skip，打包配置还漏掉核心模块；远未复刻原项目的 91,175 行功能代码和 368 个测试函数。

r6 同时抓到四个与样例无关的展示/身份缺口：用户回执的 `isolated` 模型轮把私有 thinking 显示成主代理
思考，并把真实 main context 从 74.4k 覆盖成约 9.7k；单项 `items` 返工 child 退回无编号系统名；Todo
标题 `4/8` 只是四行视窗却像完成数。当前候选对照 会话运行时 active-turn reasoning 投影与 终端交互
`TaskListV2`：isolated 调用仍记成本但不投影 thinking/context；系统单项批次沿 sibling 序号；Todo 标题改为
`完成 X/Y · 进行中 Z`，四行窗口与完成率彻底分开。200 项 focused 回归通过。

`19d4cea` 已推送并部署到 `.7` 唯一 Gateway。全新 tmux `dsh-p4-lazygit-r7-ea91639` 在干净 cwd 对同一
固定 lazygit commit 只投递一次原样 Prompt 4：7 个 child 全部自然 DONE，补派单项正确连续为 worker-6/7；
isolated 私有 thinking 未再显示，main context 没有被表达轮的小数字覆盖，Todo 从 0/11 到 11/11 均显示
真实完成数和四行视窗。四个 r6 展示/身份问题因此通过真实 TUI。

r7 的交付本身仍是 P0 失败：产物只有 29 个生产 Python 文件、6,985 行功能代码、2 个测试文件和 20 个
弱测试；`app.py` 实际只 compose Header、欢迎文字与 Footer。`pip install -e .` 因 owner site-packages
只读失败，独立构造 App 又因缺 `textual` 失败。更严重的是 main 把初始 6 个失败测试逐步削弱，删除真实
`test_app_instantiation` 后才得到 20 passed，随后误报“所有核心功能已实现、可正常导入运行”。当前候选
按 会话运行时 的端到端持续与真实验证纪律，统一增强 root/child/lifecycle wake 的软提示；不恢复机器验收门。
253 项直接相关 focused 测试与本地严格 gate 已通过。待推送和单 Gateway 部署后，以全新 r8 tmux/cwd
再只投递一次原样 Prompt 4，重点看
模型能否保留完整范围、修真实失败而不是删测试，并按真实运行结果收尾。测试者仍不得旁路改被测产物。

`b3c2daa` 部署后的 r8 在 tmux `dsh-p4-lazygit-r8-ea91639` 对同一固定源码仍只投递一次原样 Prompt 4。
范围软纪律让产物早期增长到 793 个 Rust 文件、9,718 行，但运行链路被新的 P0 身份问题污染：一个 child
因截断回到 PENDING 后，其启动记录仍指向承载兄弟的共享批次 PID，无法重试；child finalize 又建立
`ordinary_task_resume(task_id=child, thread_id=root)`，root 后台模型于是带着 child task 身份和主工具运行，
覆盖 Todo 并越权创建孙代理，同时真实 child retry 又启动，形成双执行器。

`93e18f6` 已按 会话运行时 的 child 独立 session/thread 所有权收口并部署：task-local finalize 不再租用 root 后台轮；
后台 policy/wake 精确解析为 canonical child 时在模型前退役或无模型确认；PENDING 只回收 exact run 的
task-local launch record，不终止共享宿主，使既有 runner auto-start 可立即续派。fresh r9 的 5 名 child
没有再串入 root、也没有双执行器；该轮没有 PENDING 样本，所以即时重派真机证据仍待自然触发。

r9 新抓到更底层的交付合同错误：模型没声明 `output_files` 时，编排器仍生成 task-local Markdown，runner
把它说成“用户要求的业务产物”，两名编码 child 因而只交报告。main 后续把代码写成两套目录，在 Todo
6/12 时自然误报完成；约 4,970 行产物的普通安装失败，隐藏 venv 的产品 TUI 又在启动时抛
`Stylesheet.parse()` TypeError。当前候选按 会话运行时 的 child final message/status watcher 删除新任务假文件
合同，历史 `system_default_output_ref` 只保留迁移读取并从模型合同/完成信封/expected outputs 隐藏。
严格 gate、推送、单 Gateway 部署后以 fresh r10 原样 Prompt 4 验证；仍不增加宿主机器质量门。

`0c6c916` 部署后的 fresh r10 已证明上述假文件合同消失，前两名 Rust child 通过 typed status、最终回复
和系统 final report 自然交接。但第二个 completion wake 后，root 把 Rust 改成 Go 并派出“只做基础框架”
任务。原始 Prompt 4 仍完整存在于 task link/transcript；真正断点是后台每次拿 synthetic wake 文案新建
`agent.run()`，既替换 User Task，又不携带前一工作片的 root tool archive。当前候选按 会话运行时 同一 active
turn/history 修复：exact task goal 保持根用户任务，wake 只作 runtime continuation，root canonical tool
index 按 exact run/task 恢复，child/Audit 不混入。待严格 gate、推送、唯一 Gateway 部署后，以 fresh r11
原样 Prompt 4 验证语言/范围/派工连续性；仍禁止测试者补代码、追加技术推动或恢复机器质量验收。

`a190378` 部署后的 fresh r11 已证明原任务与 Python + Textual 方案跨两批 child 保持不变；但 root 又建立
`p1/p2` 同义清单，第二批没有传 `covers`。索引实证显示 `task_progress.items`、
`create_subagents.items` 在持久化时均变为 `[]`，且 native provider 不消费机械重建的 tool-context。当前
候选改为保留有界递归脱敏参数，并把 carried 轨迹作为同一 IR 的单条 CompactionSummary handoff；
Task Runtime State 额外投影 exact Todo ids 与 covers 字段合同。focused 回归已通过，待严格 gate、部署和
fresh r12 原样 Prompt 4 验证“不重复计划、派工绑定、长任务继续”三项；仍不新增机器质量验收。

`5f1f485` 部署后的 fresh r12b 已证明 native handoff 保留原目标、语言、Todo 和嵌套工具参数；但 root 在
容量拒绝后重派 4 项时仍漏掉 `covers`，并把新实现只在 goal 中指向源码 cwd 的兄弟目录。旧运行时先落
child，随后写边界拒绝，main 又反复尝试不能批准的越界 grant，最后改变语言和目录；现场已 `/stop`。
当前候选新增 create-time 原子合同：已有 canonical 计划时，每个 item 必须绑定独占 open exact id；直接
编码的写工具角色还必须声明父 workspace 内互不重叠的 `output_files`。错误批次零创建并返回结构化
`required_repairs`，目录与其子路径也算冲突；不解析自然语言、不判断质量或完成。严格 gate 与唯一 Gateway
部署后，用 fresh r13 原样 Prompt 4 验证模型能按回执自行修正参数、在当前 cwd 内创建新语言实现，并继续
保持原目标。测试者仍只输入一次原 prompt，不补代码、不追加推动消息。

`611ee55` 部署后的 fresh r13 已证明上述 create-time 合同：5 批重复 covers 全部零 child 原子拒绝；模型
随后把 8 项计划细分为 18 项，并成功创建一名带 exact covers、当前 cwd 内两个精确输出文件的 Rust child。
该 child 写出 1,296 行代码，失败后继续修正并自然 DONE，root 收到 wake；因测试机无 cargo，本轮不声称
编译通过。新发现是 `reported_error_code` 虽正确，新码未登记 taxonomy，控制层/TUI 显示
`UNKNOWN_ERROR`。当前候选只补唯一错误分类为 retryable + repair arguments，不造第二个重试器；部署后
fresh r14 原样验证工具状态、模型返工和后续多批派工。r13 已 `/stop` 为 PAUSED，无残留 runner。

`c5cc7c2` 部署后的 fresh r14 已证明第一名 child 完成后 root 会自动醒来并派第二批；但首名“项目骨架”
goal 中出现 `src/i18n/`、`src/config/` 时，历史正文自动补绑旁路把两项 Todo 一起误判完成，TUI 从 0/9
跳到 3/9。当前候选已删除 root/items/nested 全部 goal-id 自动补绑与回执投影，计划绑定只认显式
`covers`；新增同名目录回归已通过，`bdcc7d1` 已发布部署。fresh r15 没再走到 Todo/派工，而是在用户已
授权自行换语言时停止并输出 Rust/Python/其它菜单；同模型 会话运行时 对照先探索、随后也输出语言/范围/测试
菜单，均违背 会话运行时 Default 源码的 assumptions-first 规则。当前候选只把该源码语义适配到根默认
`system_prompt` 前部，不解析问句、不造重试器或机器验收。`b7005a8` 发布后的 fresh r16 已证明 root 会
自行选择 Rust、建 7 项计划、修正漏 covers 并派 child，首名 DONE 后也会自然创建第二名。r16 的新失败是
首名骨架 child 声明 6 个基础文件却额外写 11 个文件，其中两个正是第二名的任务，形成真实兄弟冲突。
会话运行时 spawn 不要求预报完整写集，现场也证明该模型声明不完整；下一切片保留 exact covers，把
`output_files` 改为可选交付/冲突提示，并规定 child 的直接父级 goal 是完整工作边界。严格 gate、部署后
用 fresh r17 再只输入一次原样 Prompt 4，验证 child 不替兄弟扩做、root 仍能按 lifecycle event 继续派工。

`4c3a59d` 部署后的 fresh r17 已证明首名骨架 child 不再替 TUI/Git/GUI 扩做，第二名也基本停在 TUI
范围；第三名 Git child 却把代码写成 cwd 根部第二棵 Rust 树。root 正确决定返工，但原 Git Todo `3` 已
关闭，mandatory covers 又不允许省略，于是新 Git child 被绑定到下一 open GUI Todo `4`，TUI 当场把 GUI
误显示为进行中。现场已 `/stop`，错绑 child CANCELLED。当前切片按 会话运行时 spawn 独立于 plan 的事实，把
`covers` 改为可选 exact 映射：未绑定 child 用真实 run id 形成独立进度行；提供的错/关/重复 id 仍原子
拒绝。返工若继续映射原项，先用 `task_progress(status=in_progress, correction=true)` 重开原 id。严格 gate、
部署后用 fresh r18 原样 Prompt 4 验证返工不再顶替兄弟 Todo，同时继续观察输出路径与完整交付。

### 用户直控子代理的共享控制面

状态：详情/插话/停止与 exact-attempt 修复已部署真 TUI；显式 interrupt/resume 仍待设计

解决问题：当前用户只能给主代理插话或停整个任务，无法定位一个正在跑的 child；现有
`cancel_subagents` 又是终态取消，不是“打断当前一轮后仍能继续”。如果 TUI 和未来 Web 各自直改账本，会导致状态、
权限和恢复逻辑分裂。

当前进展：已有 owner/thread 范围的 active root + direct-child 投影，TUI 可进入详情、给运行 child 发消息、
Esc 停止并在终态只读回看。真实 Prompt 3 暴露 guidance 未绑定 exact turn 会杀掉 child，`6d33228` 已要求
pending/running AgentAttempt 后再入账；默认容量同时从会话 6/单次 4/runner 4 收口为会话 8/单次 0/runner 8。
fresh `ma-6d33228-p3-guidance8-r20` 已证明八名 child 同时运行，中文插话 exact receipt 最终 consumed 且 child
继续工作，root/child 历史均可查看。
R64 递归真机又暴露：在协调 child 详情页按 Esc 只关闭协调节点，8 个孙代理仍继续运行。当前按 会话运行时
`shutdown_agent_tree` 收口为 exact branch cancel：先 signal 目标节点，再等同一 owner 创建事务退出并重读
canonical lineage，关闭全部存活后代；祖先和兄弟不受影响。修复必须同时覆盖在途孙代理创建与取消工具
展示，不能把结构化 user cancellation 误报成未知副作用。
后续仍待做 interrupt 当前 turn 后可恢复、resume/start 同一 session、cancel/close 和 capability 裁决。
root owner 可操作自己树内任意后代，模型代理仍只能管直属下级。
每个写操作必须带幂等 `operation_id`、exact target、expected version/state 和 accepted/rejected/unknown 回执，
TUI/Web/IM 都只调用这一份服务。

边界：本阶段不做 Web 页面，不开放任意数据库修改或越 owner 操作；不能用终态 cancel 伪装成可恢复 interrupt。

### npm 缓存全新 Node 任务真机复验

状态：待验证

解决问题：Fiber→TypeScript 因 npm 默认写只读 `$HOME/.npm` 先失败一次，让模型浪费调用自行探索环境
workaround；需要一项全新 Node 任务证明底座默认值首次就生效。

当前进展：EXEC-45 已由 Fiber 真实请求闭环；owner-scoped shell 增加标准
`NPM_CONFIG_CACHE=/tmp/.cache/npm`，不修改 `HOME` 或写边界，并已部署 `.13`。本地和远端 focused tests、
Ruff、py_compile 已通过；Fiber 自身早于部署启动，只能证明模型 workaround，不算底座 E2E。测试者在下一
Node 任务仍只观察，不能修改任务产物。

边界：仍只修改 `/Users/example/my-agent`、部署 `192.0.2.13:/root/my-agent`；不触碰青禾项目，
不输出测试机 key，小于 10,000 行的本轮修复只跑 focused tests。

### Memory v2 单一主链收敛

状态：代码迁移完成，最终全量与真实模型验证中

解决问题：旧实现把候选正文、每日镜像、learning draft、task-local memory gate 与正式长期记忆分散在
多套账本和状态机中，后台提炼也缺少统一触发、证据核验、失败恢复和真实 Gateway 验收，容易产生双写、
旧事实复活、跨 scope 覆盖和模型绕过审核的问题。

当前进展：已落地 owner 唯一 Candidate/Daily/Curator/Promotion/Lesson/HOT/Recall 主链、一次性 v1→v2
Migration、Retention v2、统一管理员 CLI 与 Gateway owner-maintenance 接线；旧 learning/memory-gate/daily
mirror 生产路径已删除。一次性 `my-agent run` 也已在模型前接入唯一 ConversationStore user 原文和
Memory message ref，并保持 standalone workspace 生命周期；focused 回归通过，隔离 testbox B5R3 已用
真实 DeepSeek 完成 remember、Candidate、Promotion、正式记忆、workspace 终态和同 request 存储重放，
群内独立审查通过且无 P0/P1。最终全量/静态门禁、剩余真实 Gateway 触发矩阵、真实双模型、重启恢复和超长阅读仍待完成，
因此尚未标记完成。

设计与证据：`docs/modules/memory/03-purpose.md`、`04-structure.md`、`05-memory-v2-layout.md`、
`06-runtime-memory-requirements.md`。

### Agent 基础能力单一主链收敛

状态：待验证

解决问题：能力自述、Shared/Skill、Memory/Persona、Scheduler、Workflow 和验证证据存在重复事实源或
主链接入不完整，导致模型能力与真实运行状态漂移，并增加多用户串权和长任务不可靠风险。

当前进展：用户已确认 `docs/design/FEATURE-20260718-agent-foundation-convergence.md`；owner/channel
registry、能力自述、Skill 单一逐轮 snapshot、Memory 稳定 ID CRUD/batch、Persona 单一 repository/
版本/CAS/回滚、owner-scoped 持久 Scheduler，以及 Workflow 收敛为 Skill + 当前 task plan + 原生
tools/subagents 均已完成聚焦验证；旧 workflow package/mode/config/CLI/extension/index 已删除。
被动验证证据也已接入公共工具出口，能保留真实命令/exit/targeted/full 并在文件写后过期。
工具参数也已收敛到单一 `ToolModelSpec.input_schema`：provider 展示、文本/native 入口、MCP、运行门和 handler
前校验不再各保留 required/type 副本；保守强类型纠正、嵌套/枚举/范围/额外字段校验及协议同名参数
碰撞已完成本地回归，并由本地 8899 与 MiniMax-M2.7 的隔离工具失败恢复链验证。当前后续切片又把
缺失参数限制为逐字段明示的安全默认值或 Registry 可信上下文绑定，并为每个有效输入保留脱敏
`source/source_ref`；旧 `read_artifact` 运行身份和进程工作目录专项补参已删除，待完整门禁、模型、
1.10 与正式双用户复验后发布。
外部多写已继续复用唯一 operation store，没有新增 Saga 或自动回滚。提供方结果只有在权威终态保存
成功后才能作为成功返回；保存失败降级为 unknown，结构化状态会穿过 archive、control-plane event 与
compact，语义摘要另保留中段非成功副作用事实。完整门禁、本地 8899 基础 CLI、MiniMax 长链/极端
CLI、1.10 双 Feishu owner scope 与真实出站均已通过；新的桌面客户端入站仍按产品事实页保留为外部
验收边界。
Fiber 历史 `return_code=143` 样本仍保留为模型判断失误证据，但不再由普通完成机器门强制返工。模型能在
失败工具结果后的正常采样中自主修复；若它给出 plain final，turn 自然结束，operation ledger 继续如实标记
failed 供用户、后续消息和审计查看。UNKNOWN 副作用仍直接 fail-closed，不能自动重放。
极端 MiniMax CLI 发现并删除了旧“绝对路径写飞后静默搬进 task output”兼容层。显式绝对路径现在保留
原目标身份，由唯一写边界返回明确成功或 `WRITE_FORBIDDEN`；相对 `output/`、`work/` 任务落位不变。
修复后的真实链已在 CLI 和 1.10 正式 owner scope 验证“成功—拒绝—继续成功”及模型准确部分结果。
`max_active_agents` 与结构化 owner 写入口已接同一 quota lock；Gateway 已按有界 owner page 自动执行
基于结构化终态、二次校验、trash tombstone、legal hold 和审计的 retention。应用门不能覆盖任意
Shell/PTY/LSP 进程写盘，正式规模部署仍需 filesystem/project quota。完整本地 CI、严格 code-size、
本轮 Schema 改动现已通过完整 pytest、Ruff、import/offline/code-size/doc-sync、compile、diff、
distribution boundary 和干净 wheel artifact gate。未跟踪运行数据继续由 worktree clean-package
正确阻断且不得进入 wheel。Schema 这一切片已随精确提交进入远程 `main`，部署 1.10 唯一正式
Gateway/Feishu，并完成本地 8899、MiniMax-M2.7 和两个既有真实飞书 owner 的只读工具调用复验；
当前 Compact 收口进一步删除了漏算原生 ToolCall 参数的字符预算，改由主代理、Gateway conversation
与子代理共用完整 provider-visible token 估算和既有 recent-tail；真实旧版长链又证明配对删除本身
会丢路径、checkpoint 和未完成状态，当前候选因此复用既有语义摘要后端，在同一 native IR 中用最多
一条 replacement item 承接旧段，不新增 task/chat Compact。本地 8,371 项完整 pytest、静态门禁与
干净 wheel 已通过；仍待提交、精确 wheel 部署和同一真实飞书项目的 200K/90% 多次续作复验后移入完成项。
更大范围的基础能力长稳、真实群组和规模验证仍属于本路线项。

验收：完整本地 CI 通过，推送远端 main，部署 1.10，并完成既有飞书双用户、多长任务真实 LLM 标准。

补充记录：2026-05-05 已完成一轮 CI 回归修复，解决 gateway 测试互相污染、日志证据测试缺导入、runner 解析失败结果写回不一致等问题；细节已移入 `docs/COMPLETED.md`。

---

## 设计中（未开工）

### Subagent 自然收口与递归控制面

状态：本地已落地，待 `.7` 真实 TUI 验证

解决问题：旧链同时存在创建、手动派工、层级调度和机器验收，多套控制面让模型重复创建、等待或被宿主
粗暴打断。当前已收敛为递归一致的父子关系：所有层级只用 `create_subagents` 创建并自动启动；父级只需
接收生命周期事件、补充 guidance、取消/中断和自然汇总。主代理、子代理、Gateway 共用六类 `turn_end`，普通完成不再
依赖 `acceptance_checks`、`VERIFIED` 或模型生成的专用结果壳。

待做：部署 `.7` 后只从真实 TUI 输入一次植物大战僵尸任务，观察 child 数量、自动启动、父级汇总、失败
恢复、Compact 和最终 0.0.0.0:8080 可访问事实；测试者不得旁路修改任务产物。

设计台账：`DESIGN_LEDGER.md` “2026-08-21 子代理递归控制面与自然收口”

### 注释重构规范

状态：设计已记录，待分阶段落地

解决问题：大量函数 docstring 是一句话说明，缺少 LLM/人类双层说明，新接手的 AI 或开发者难以理解副作用和边界。

待做：按模块分阶段补齐双层注释（config → tools → core → CLI → subagent）。

设计台账：DESIGN_LEDGER.md "注释重构规范"

---

## 部分落地（有骨架但未完整接通）

### Subagent Channel Probe

状态：部分落地

解决问题：通道故障和任务失败必须分开判断，接管前先确认 workdir、机器 JSON、写入现场是否正常。

已有：本地工单现场 probe、channel_status 状态机、CLI 命令。

待做：模型接口 probe、ACP/adapter/session probe、派工前自动 probe 和失败阻断。

### Grant 注入执行上下文

状态：部分落地，grant 后自动唤醒已落地

解决问题：capability grant 不能只停留在运行记录里，必须能变成子代理实际可读取的执行包。

已有：`SubAgentExecutionContext`、`build_execution_context()`、`write_execution_context()`、CLI 命令；capability grant 后会创建 observation/wake signal，并把 `capability_grant_wake` 写回 task。

待做：按 token 预算裁剪 execution context；子代理写 capability request 统一入口继续精简。

### 层级能力上抛

状态：部分落地，授权后下发唤醒已落地

解决问题：子代理遇到问题不应自己全局搜索 skill/tool，应描述能力缺口，由父代理发现和下发。

已有：`CapabilityRequest`/`CapabilityGrant`/`CapabilityGap`、单个子代理 runner 入口、capability request 路由、grant 后 wake signal 和同 run follow-up。

待做：跨多层级的批量上抛汇总；tool failure mode 和显式 alternative group 参与排序。

### 本地恢复、诊断、worker 和 adapter 第一版

状态：部分落地，runner session heartbeat 已落地

解决问题：LocalStore 一致性诊断、gateway 请求崩溃恢复、adapter 文件协议。

已有：`local-doctor`、`local-rebuild`、gateway failed 归档、processing lease、保守 worker pool、runner session heartbeat 账本、`adapter file`、启动恢复结构化检测错误、后台 dispatch 启动标记错误报告；普通回复/主动消息/附件已共用 DeliveryService 与 adapter registry；adapter 入站现已在媒体和 Gateway POST 前落盘可信 channel/user/conversation/provider message identity 与 canonical digest，由单 worker 恢复精确 POST body、输入/结果/控制回执、占位和回送，ingress/reply row 都以 owner/epoch/expiry 短租约实现跨进程接管和旧结果 fencing，冲突正文隔离，429/5xx 保持等待，auth/config 与 input terminal-unknown 进入无正文持久终态；`/btw` unknown 按独立 operation ID 恢复而不拿目标 turn 去重，首次 GET 可单向绑定空 target，控制回执 watcher 冻结并发送 channel/user/conversation/chat type/chat id 身份供 Gateway 精确鉴权，stop rejected/terminal-unknown 也有明确或 durable unknown 收口；当前会话的 typed slash dispatcher 已统一 `/status`、`/btw`、`/stop`、`/goal`、`/verbose` 与 `/audit` 入口，未知 `/XXXX` fail-closed，命令词不进入模型；`/stop` 已改为优先中断当前窗口精确 live request，再持久清理 task/subagent。普通派工/等待/完成新增瘦身 LLM 回复轮与最终 delivery snapshot；后台 claim 具备 90 秒 fail-safe 和同进程域死 owner 立即接管，Gateway signal shutdown 有 typed forensics。

待做：LocalStore compact/backup/export、gateway 请求优先级、独立子进程隔离版 runner worker、adapter HTTP/WebSocket 版，以及第二个生产 IM 的真实 API/媒体/重启复验。系统命令与窗口级 `/stop` 已随 `7776a03f` 部署 1.10，并由两个既有真实飞书客户端在运行中完成中断、无迟到回复、停止后续聊和 transcript 不含命令的复验。

### Runtime 配置层与错误报告全链路

状态：部分落地，子代理 run/task overlay 与主要 best-effort 结构化已落地

解决问题：运行时 overlay、owner/task/run scoped 配置、错误报告和恢复摘要必须成为一条真实链路，不能只停在合同测试或局部 helper。

已有：基础配置来源链、`RuntimeConfigLayer`、CLI runtime overlay 环境入口、子代理 `config_overlay_ref` 创建继承/接管记录/worker 装载、后台启动和启动恢复错误可见化、gateway/audit 旁路错误结构化。

待做：远端 session/ACP 入口接入 scoped config；继续扩展少数低频 gateway adapter/supervisor 旁路异常的统一持久化。

### 可见真实环境测试台 Live Lab

状态：部分落地

解决问题：开发 agent 不能只靠单元测试，需要能在用户看得见的终端里跑真实 runtime。

已有：`live_agent_lab.py`、隔离配置、命令执行、transcript 和 summary、`open_live_lab.sh`。

待做：R113 发布后用单 Gateway、多隔离 owner 继续跑 memory 长任务、tools 边界任务、问题任务、多子代理
整合和多轮恢复场景；每一路保留 tmux、request/thread/task、Compact 代次与 owner 隔离证据，缺环境项单列。

### 并行开发 Workstream 工作台

状态：部分落地

解决问题：多个 AI 可以并行，但必须先把目录、分支、职责边界和交接格式定清楚。

已有：`WORKSTREAMS.md`、`HANDOFF_TEMPLATE.md`、worktree 创建/状态/打开脚本。

待做：`workstream_sync.sh`、`workstream_handoff_check.sh`、主线集成固定 checklist。

### 外部痛点文档：DISPATCH_PAIN_POINTS / PAIN_POINTS

状态：已记录，部分约束已落地

解决问题：workdir 不可靠、子代理启动后不建工单、completion notification 不等于完成、BLOCKED 不能当终点等系统性痛点。

已落地：标准工单目录模板、`ACTION_RECEIPTS.md`/`TEST_CHECKLIST.md`/`BUGS.md`/`SKILL_USAGE.md`/`HANDOFF.md`。

未落地：顶层任务现场模板、SPEC→TEST_CHECKLIST 追踪链、Tool failure log、Browser/PWA/Game 自动验收器、数据规模断言器、安全任务隔离策略。

---

## 待验证

### 普通 Feishu Agent 对话与工作真实验收

旧部署的双 owner 真测确认身份与产物隔离，但也暴露 `/btw` 会被辅助回复轮提前消费、以及 task-scoped
history 让补充要求在续轮中丢失。当前候选已禁止辅助轮消费 active-turn input，并按 会话运行时 的 transcript
commit 语义把成功接收的 `/btw` 作为真实 UserTurn 幂等写入唯一 thread history；compact 续轮使用 typed
carrier，不再维护 task-id guidance history。focused tests 已通过，待发布后在 1.10 重新做两用户真实
Feishu 长任务、连续 `/btw` 和独立产物验收。

解决问题：同一用户多轮忘记、旧任务污染闲聊、首条消息被密码卡吞掉，以及长任务超过同步等待窗后真实结果无法送达。

已验：1.10 MiniMax M2.7 双 owner/thread 长任务、compact、旧聊天检索、跨用户/跨话题隔离、
`/verbose`、人设、USER 偏好、SOUL 确认卡与长任务回送；生产 compact 阈值已恢复 90%。

本轮待部署后复验：`/goal` 同 thread 持续续跑/暂停/恢复，`/audit` 显式结构化激活，
`/btw` 写入并在原 live turn 下一安全点真正消费且不创建后台重复执行器，`/stop` 能立即打断模型流，
之后模型以精确结构化 task id 重开原 workspace，失败选择不能新建旁路任务，以及前台聊天和后台
长任务并行时不串 prompt/workspace。还需验证前台让出后两个用户的普通聊天都能在后台主代理继续工作时及时回答、
后台可按任务结构自主派工、最终消息包含真实目录/功能/测试结果且不泄露内部协议。

`/audit` 高频候选当前已完成本地机制收口：等待时间、累计条数或累计数据量任一条件触发无历史
批次；正常记录保持完整，极端超窗单条才生成显式头尾视图；原文、哈希、分数、结论和模型来源均
可按 `source_ref/ack_id` 查回。focused 与完整 pytest 已通过，待 1.10 正式 CLI 五源 10 分钟实测
确认吞吐、召回、误报、重复和排空情况后再从本节移入部署事实。

### chat 交互体验：中文输入和后台输出

状态：待验证

解决问题：`input()` 和后台线程同时输出时破坏输入行，中文删除有视觉残留。

待做：确认 `prompt_toolkit` + `patch_stdout()` 在各终端下稳定。

### 裸 TUI 启动与单 Gateway 恢复权威

状态：已完成；`06b84e1` 已部署 `.7` 并完成真实 TUI 复验

解决问题：直接运行 `my-agent` 会扫描全机历史任务、阻塞在没有真实恢复动作的 `[Y/n]`；同时旧的
0 字节派工锁会让 Gateway 的 runner supervision 永久跳过，真正失联任务反而得不到调和。

已有：裸启动走轻量 fresh chat；旧 session 只允许显式 resume；status 只读；普通 stale attempt 移到
Gateway 启动恢复；subagent supervision 使用内核 advisory lock，空/坏元数据不再形成永久锁。

结果：`.7` 保持单 Gateway 与 MiniMax-M2.7，裸 TUI 在 1 秒采样点已出现输入框且无全局恢复提问；旧
0 字节锁被同一 Gateway 重写为 v2 内核锁元数据。首轮 supervision 结构化记录 4 条复活、21 条父会话
关闭取消和 5 条失联 runner 回收，随后 3 条真实 RUNNING 自然完成，没有手工批量改账。

### 后台主代理的 capability 审批进入统一 TUI 面板

状态：R118 focused 通过；非角色型额外 capability 的 fresh TUI 待补

解决问题：一级 child 申请创建孙代理等管理员级能力时，直属主代理会调用
`resolve_capability_requests(decision=grant)`；旧 runtime policy 又把该父子控制动作标成 dangerous，导致
background-main 只拿到 `APPROVAL_REQUIRED`。普通用户既不了解内部 run/path/tool，也不应替直属父级做
本来就在父级权限上限内的编排决定，模型因而反复尝试而无法继续。

当前实现：`resolve_capability_requests` 的 grant/deny 都按 mutating 父子控制执行；handler 内既有 direct-parent、
owner wall、父级 workspace/write roots、Skill/Tool availability 仍是唯一硬上限，越界 grant 继续结构化拒绝。
这不是取消具体危险工具的 exact approval，也不允许主代理给自己提权。grant/deny 与越界拒绝 focused 已通过；
fresh TUI 仍需选一个不属于 coordinator 固有工具面的窄请求，复验“child request → 直属父级裁决 → 同 run
续跑”。递归创建本身不再错误地走这条申请链。

fresh wheel 复验另发现 builtin coordinator JSON 没有进入生产包，导致 role 文案仍是 coordinator、结构化
`can_spawn_children` 快照却为空，`create_subagents` 被 leaf policy 删除。当前同步把 builtin role catalog 加入
package-data 和 distribution boundary；递归创建本身按 会话运行时 式 depth/capacity 硬门直接继承，不再靠模型先
发现并申请本来就在角色合同里的工具。`.10` u331 已真实形成 coordinator + 两名 depth-2 researcher，三者
全部 DONE 并逐层自动唤醒；其它新增目录、MCP、Skill 或外部权限仍走上面的直属父级裁决。

### durable wake 被重复孤儿扫描堵住

状态：R118 fresh TUI 通过

解决问题：`.10` 的 durable child wake 持续 ready，Gateway 日志反复发现 hard owner，但主代理没有提交
thread lane。`py-spy` 证明 `bg-owner` worker 空闲，background-main supervisor 卡在
`prepare_tick -> supervise_stalled_orphans -> RuntimeDB`。Gateway 已有独立 orphan reconciler，旧代码却在
每个 owner scheduler 的准备阶段同步重复扫描；历史账多、磁盘 100% 时会长期饿死真正会话工作。

当前实现：Gateway 调度调用 `prepare_tick` 时显式关闭 inline orphan supervision，只保留独立 reconciler；
直接/嵌入式同步 scheduler 仍默认保留原恢复兜底。u328 的直属 child 完成后 main 自动 final；u331 的两名
孙代理收齐后 coordinator 自动恢复，随后 main 再自动恢复并 final，两轮都没有“继续”消息。focused 同时证明
Gateway 跳过、非 Gateway 保留两种 prepare 语义；没有引入短超时误杀慢模型。

### 8G 测试机当前资源复测

状态：环境恢复后执行

解决问题：上一轮已证明 `.7` 的高 used 主要来自 17 个旧 TUI、对照进程和 page cache，清理后约下降 1GiB；
本轮主机 ICMP 间歇可达但 SSH 22 拒绝、Gateway 8420 不可达，无法取得当前进程级 RSS。

待做：SSH 恢复后不重启、不删任务，先记录 Gateway/TUI/runner/终端交互/LiteLLM 的数量与 RSS、`available`、
swap、owner pool hard/soft 数和 10 分钟空闲回落，再决定是否还有真实泄漏。不得把整机 `used` 或旧快照当成
当前 Gateway 占用。

### 直属 child 结果跨后续工作片丢失

状态：R92 已解决；focused、旧长任务恢复与 fresh TUI 均通过

原问题：完成信封只在当次 lifecycle wake 和普通观察尾窗中可见。四名 child 已 DONE、报告存在且 wake 已
handled 后，后续 scheduled progress 工作片只看到“全部终态”，看不到精确报告引用，模型便猜内部目录并被
正确的状态面安全门拒绝。

结果：前台与后台共用 exact-root 直属完成投影；后续 scheduled/recovery 续片继续获得有界最终回复和完整
`final_report_ref`。U85 已在 `compact 1` 后恢复，U98 fresh 4/4 交付。后续不再为这条问题增加 fallback；
只继续在 U99--U105 的长会话、并发 guidance、Search、Memory/Compact 与 owner 隔离中做回归观察。
