# TEST CHECKLIST

## R211 /model 真 TUI 分项

- [x] ma-r211-110-models：Auth 预留与返回、字段掩码、非法窗口阻止保存、保存与选择独立。
- [x] 菜单操作未进入 canonical 聊天或模型调用；同一旧会话恢复后 8 次 MiniMax-M2.7 成功、0失败。
- [x] 显式 96k 参与真实计算，主界面 45.3k/96k、compact 1，并继续派两个 child。
- [x] task overlay 的整组模型优先级及孙代理引用，新增先红组合用例后 57 focused 通过。
- [ ] overlay 修复部署后真实 child/grandchild/restart 组合；不得用上述单测代替。
- [ ] OpenAI 实际模型调用、运行中切回默认和菜单重开/取消组合；当前不算全部已验。


## R210 前台数字/阶段

- [x] 前后台共用 main 标量、任务晋升身份、跨 thread 拒绝、迟到事件隔离与显示失败不反噬执行；115 focused。
- [x] 唯一 Gateway 部署后，Go 两页 51.1k/12/read_file；另一 owner 前台58.4k、后台92.9k/等待1child同步。
- [x] Go 原 run 保留、新 attempt 继续；家庭工具前台转后台数字保持。完整恢复副作用矩阵不算已过。
- [ ] 授权等待共享：原页有 cancel_subagents 授权面板，观察页仍显示执行中（BUG-147）。
- [ ] 完整前台正文跨窗口增量（仍未实现，和标量验证分开）。

## R209 展开模式与 R208 真复验

- [x] R208 同 Go 会话恢复/上翻/返回保持当前 1/6 清单，真实状态通道失败后主/子提示，恢复后自动追上。
- [x] 唯一 Gateway PID 与原 child/attempt 未变，故障只注入专用客户端 notices 读取。
- [x] 捕捉真实 Ctrl+O decorator 覆盖底栏，frame provider 定位用例先红。
- [x] R209 安装后，展开且上翻历史时仍能看到实时警告；恢复后快捷键恢复，阅读位置保持。
- [x] 8428 读取夹具停止，原配置直连恢复，唯一 Gateway 未重启；真实退避 0.5/1/2/4/8 秒量级。
- [ ] BUG-146 同 session 新窗口实时看到原窗口前台工具/context；当前仅 user 到达，不能算完整过程同步。

## R208 后台快照刷新健康

- [x] 失败/重复失败/恢复、原退避、任务和输入不变；健康事件不重置模型活动计时；166 focused。
- [x] 主/子及展开视图共用 root 健康，显示警告并正确失效缓存。
- [ ] 新客户端真实读通道故障与恢复；唯一 Gateway 和原 run 继续，不影响其他 owner。

## R207 当前清单与历史卡片

- [x] 两个真实结构定位用例先红后绿；当前计划换代可见、上翻不能覆盖，旧详情和原字典不变；89 focused。
- [ ] 新客户端恢复原 Go 会话，当前清单正确，上翻/返回/主子切换后不退回旧调研。

## R206 空选区与失联/恢复状态

- [x] 新测试窗口空选区右键真实异常；终端交互 对照、定位先红后绿，38 focused 通过。
- [x] R206 真 PTY 无选区右键无异常/旧 buffer SHA 不变，中文整句拖选/右键复制完整；OS 剪贴板不冒充通过。
- [x] Gateway SIGINT 原始退出状态保存；恢复唯一 Gateway 后原 Go 子代理同 run 继续，compact 继续增加。
- [ ] 断线明确展示快照失联，不让旧 Working 冒充当前执行状态；不擅自修改任务终态。
- [ ] 同会话恢复清单与原窗口保持当前任务一致；主/子数值差异继续采样，不凭推测改计数器。

## R205 截断提示

- [x] 实时 local/Gateway 共用 typed 长度判断；保留非流式半截正文，不把 transport ok 当完整模型答复；56 focused。
- [x] 本地持久历史与后台快照保留独立 typed 提示；正常提交/repair 同源、重放不重复，child 实时结束也显示。
- [x] 11 文件定向回归 429 passed / 2 原有 xfailed，Ruff/doc sync/strict code-size 通过；无全仓 pytest。
- [ ] 新版真实 provider 截断及后续消息续作；不能把显示回归算真实 TUI 通过。
- [x] R205 原 TUI 真实 MiniMax SSE max_tokens、半截正文、明确提示和 Working 收起一致；后续恢复/续作单列。
- [x] R206 同 session 新客户端恢复后截断原因仍显示；原项目的普通继续消息已提交，完整交付待验。
- [ ] 对照 终端交互 的有界同轮恢复，保持工具副作用、取消和费用账的原边界。

## R204 后台逐轮输入归属

- [x] 保留 K 真实错误 final/wake/任务/消息/执行代证据，旧 goal 与新 request 不一致的定位测试先红。
- [x] 同 task 新请求正文和工具记录精确恢复；分页、批量顺序、插话、缺失/损坏/错 thread、legacy 和真实 prompt 回归。
- [x] 原 canonical 正文、Compact、task link 不被读取过程重写；179 passed / 2 原有 xfailed。
- [x] R204 单 Gateway 及原 K/L 精确会话恢复，新请求真实 MiniMax-M2.7 调用；F/E/I 并行继续。
- [ ] 新版真实长 TUI 连续任务/递归返回后接续正确目标，实际工具与最终交付复核。

## R203 Todo 终态与截断观察

- [x] 原 F final 后 Todo 持续闪动；两个定位用例先红，144 focused 通过。
- [x] 真正前台执行/后台活动才驱动动画；空闲前后台都归零时停止，保留原计划状态，帧缓存不随时钟变化。
- [ ] 新客户端在原 F/主子页验证静止、重新执行时恢复动画、终态再次停止。
- [x] K 半句回复的 runtime event 证实 MODEL_RESPONSE_TRUNCATED，不把 attempt done 当完整任务完成。
- [ ] 截断同轮恢复/耗尽可见提示与长期历史的真实 TUI 闭环（BUG-140）。

## R202 普通执行与交互输入分离

- [x] 三个同步入口不消费独立宿主管道；无输入为 EOF，显式命令管道不变，6 个用例先红后绿。
- [x] shell 超时/受控执行/attempt/独立 PTY 等共 80 focused 通过，9 Linux bwrap 环境跳过。
- [x] 另 83 个 shell/受控入口测试通过，累计 163 passed；本地严格 gate 通过，不跑全仓 pytest。
- [x] R202 单 Gateway 已部署并有真实 MiniMax-M2.7 用量；私有配置原字节复用。
- [x] 原 C 显式 PTY/管道、EOF 重定向真实执行；模型处理 EOFError、修复业务代码、继续同一会话。
- [ ] 去掉显式重定向后默认宿主输入隔离的独立真实 TUI 切片；不能把显式 /dev/null 当该项已过。
- [x] 原 L 经 TUI 停止受管进程；回执、/proc、监听和外部连接一致，代码保留。

## R201 展开视图返回父代理

- [x] 原 TUI 复现，frame 红测；正文/详细档位/冻结边界及旧页组合 44 focused 通过。
- [x] 原 child Ctrl+O→Ctrl+G 后直接显示 root，首轮历史仍可见，不需要额外收起。
- [x] 原 grandchild 全展开后 Ctrl+G 逐层返回 coordinator/root，正文同源且保持展开；两会话消息/用量 SHA 不变。

## R200 正常续跑不按生命周期次数封顶

- [x] 3 个红测复现；4/64 个工作片后同 run 续派、最后一名 child 的完成边接续一次。
- [x] live session/仍在退出的 attempt、失败预算、owner/conversation、能力与 UNKNOWN 原门保留。
- [x] 原 F 和旧 R171 coordinator 无插话/无新 run 地 4→5 并 DONE；R171 root final 已显示，F root 继续验证。

## R199 显示历史向前分页

- [x] 中文/超长行跨块、完整工作片边界、部分尾行、后到消息、坏游标/坏行/错 thread，旧页不重读全部前缀。
- [x] 前置顺序经过真实 renderer 核对；阅读锚点、实时游标不变；旧响应/切子页/错误不覆盖当前页。
- [x] Ctrl+O 冻结页补旧记录但不插入新回复，单个在途读取不重复请求。
- [x] 原 D 精确 resume 后读到首轮输入，Ctrl+O 回看；消息/用量 SHA 不变、Compact 5。
- [x] 原主/子/孙切换后再回看；普通与展开模式均可到原始需求。
- [ ] 中文选取和系统剪贴板复制与滚轮的组合。
- [x] F 等待异常独立定位为旧孤儿累计工作片次数门；修复与真实复验见 R200。

## R197 HTTP 正文断流

- [x] JSON/GET/SSE/open 捕获真实 IncompleteRead；同名文案不取得重试权。
- [x] open/body 各用既有预算，耗尽有界，用户停止优先，watchdog 阶段保持，半截原生工具不执行；146 focused。
- [x] 单 Gateway R197 部署；新 J 的真实 MiniMax 流断开后，同 request/attempt 重试原字节请求并继续。
- [x] J 初次派工参数错误零创建；修正后只一次成功创建三个 child，未因 provider 重连重复派工。
- [ ] 真实网络恢复、最终交付与费用/attempt 账交叉核对；旧 B 失败不能被本地回归自动标成恢复。
- [x] 撤除指定 owner 的故障夹具，恢复原直连配置并保留长任务和单 Gateway。

## R198 同回合前后台恢复竞争

- [x] 真 G 复现：后台完成原 task 并写 canonical final 后，旧 Gateway 请求再恢复报 execution_binding_changed。
- [x] 本地恢复归属：同请求可接管、后台/另一请求不可抢占；原租约与活进程判断保留。
- [x] 写绑定失败不领取；停止与领取竞争无孤儿；模型返回后到终态之间仍保留执行权。
- [x] 完成/失败/取消/中断、换代后释放、迟到清理、坏引用、清理 I/O 失败后的启动补交；318 focused / 2 原有 xfailed。
- [ ] 真实 TUI 单 Gateway 重启复验；不改旧失败记录，不重复模型调用、工具、派工或可见 final。
- [x] R198 L 主代理活跃时真实重启，同 request/run/agent 换 attempt 后继续工具并 done，Working 收起；完整去重和递归矩阵仍待验。

## R196 超时证据与一致恢复

- [x] 正常成功调用不触发 UNKNOWN 计数或提示；重复 UNKNOWN 不解除保护、不重复灌入上下文。
- [x] 前台超时保留中文 stdout/stderr 与终止回执；独立 session 后代也回收。
- [x] 未确认进程/身份/管道或普通 TimeoutExpired 继续 UNKNOWN；已确认失败不自动重放同一 operation。
- [x] 启动前 deadline 明确未启动，后台停止不凭已发信号登记 killed；精确恢复返回安全明确文案。
- [x] R196 独立环境部署，85 项依赖固定；唯一 Gateway PID 3068424，六个原 TUI 会话升级，新图书真实模型为 MiniMax-M2.7。
- [x] 原 C 在 R200 Gateway 的真实同一 attempt 连续两次 240 秒超时，已确认终止和部分输出；随后 11 次操作、9 成功并 done。
- [ ] 长项目超时与单 Gateway 重启的组合恢复闭环；旧 UNKNOWN 保留，不把旧截图算新修复失败或通过。
- [ ] 独立核对 B 的 IncompleteRead 与 provider 退避，不把本次 shell 修复当成全部网络错误修复。
- [x] 长历史 backward paging 由 R199 实现，并经 R201 原主/子/孙界面组合复验；OS 复制边界另列。

## R195 递归重启和真实状态展示

- [x] 真图书任务产生 main→coordinator→grandchild；单 Gateway 重启后原两名孙代理恢复并完成，原父层依次续作。
- [x] 等待下级、运行、完成、失败四种状态的 renderer 不再从 attempts 推导失败重试；真实失败状态保留。
- [x] 新版原图书会话追加递归任务，协调人等待孙代理的底部状态不再误报正常工作片为失败重试。
- [ ] 各代全部操作不重复、原 main 最终交付及完整跨层控制仍需核对，不用本例续作替代全部矩阵。
- [x] 原 C 两次 240 秒 shell 超时提供真实终止回执、部分输出与同 attempt 后续成功；旧 UNKNOWN 账本未修改。

## R194 展示身份误作恢复权威

- [x] 实际 task/run/agent_run/attempt 在模型前落盘，旧展示 task 不覆盖；落盘失败收口未开始 attempt。
- [x] exact owner 死亡证明与启动路径共用，活进程/未知进程/换代拒绝；其他 run 不受接管影响。
- [x] 原完整工具记录、UNKNOWN 和 dirty resource 回归保持，processing 停止/终态 fence 不绕开。
- [x] 原 A/C TUI 自然后续轮完成两次单 Gateway 重启，实际绑定与工具继续；attempts=3、已知失败=0。
- [ ] 完整主/子/孙恢复矩阵及所有副作用零重复；不能用两路主代理通过替代。
- [ ] B/D/E 完整批量质量、后续偏好与隔离核对；Memory 双路径重复偏好另列未修复问题。

## R193 服务重启与卡死次数

- [x] 原请求从总 attempt 20 连续恢复 4 次不失败；之后两次真实租约过期仍达到原失败上限。
- [x] 重启不擦除既有 processing_failure_count；非法计数不重置，保持可诊断失败。
- [x] 195 focused 通过 / 1 原有 heartbeat mock skipped；恢复历史/RuntimeDB 工具防重做、终态投影同时回归。
- [ ] 部署后原活跃 TUI 连续两次 Gateway 重启，验证原 request/run/child 不重复、历史继续、其他 owner 不受阻。

## R192 确定未写入的工具错误

- [x] 真实 operation store 下 Persona 不存在条目/版本、批量部分校验失败、CAS 冲突保持 failed/not_started。
- [x] 无 goal、错 task 不转 UNKNOWN；先写文件再抛 I/O 错误仍 UNKNOWN，副作用保护不放宽；99 focused。
- [ ] 部署后原 C TUI 继续偏好更新及可靠性长任务，核对画像落盘、owner 隔离与工具后续执行。

## R191 运行事实来源增量

- [x] 不变字段只发送一次，变化/返回旧值继续追加；来源不能从正文推导，真实用户消息不去重。
- [x] 预检查与物化后请求投影/稳定指纹一致，反复估算不修改 IR；整段诊断渲染无损。
- [x] 各来源最新值经两代 Compact 保留，取消/CAS 回滚后基线不漂移，260 focused 通过。
- [x] 已部署，真实新请求不变记忆只追加一次；C 旧会话跨压缩召回，D 批量续作自动到 4 代后 final。
- [ ] 等质量/等工作量的两档费用结论；首轮 C 全批量与 D 13 个样本不可直接当公平成本对照。

## R190 旧运行状态压缩回收

- [x] 摘要先读完整 IR；仅回收连续退休工具区中的旧快照，最新状态/用户插话/保留尾部不动。
- [x] 无摘要和非连续删除不越界；并行工具剩一个调用时保留其先前状态。
- [x] 两代 native 压缩、取消/CAS 回滚、原始工具账不变、普通请求前缀不重复改写，141 focused 通过。
- [x] 原 main/child 真 TUI 压缩后继续；child 第 96 代后 DONE；C 手动第 15 代后记住偏好并完成追加修改。
- [ ] 用完整请求数字和 provider ledger 证明频率及成本改善；不以工具子片压缩比例冒充总成本。

## R185 后台 final 与恢复使用同一消息流

- [x] canonical 消息分页、恢复/追加竞态、半行与损坏、ID 幂等、冷 owner 只读、前后台 final 身份定向通过。
- [x] 删除新 notices 正文落盘和客户端按时间/文本生成身份；原文件保留，模型历史与 Compact 不改。
- [x] `.10` 单 Gateway 原 B 多子代理续作：新后台 final 自动可见，Working 收起，正式正文只有一个 ID。
- [ ] 退出再恢复仍有旧过程环追加到 final 之后，实际画面末尾停在思考；完整历史顺序未通过。
- [ ] 所有历史分页/Compact 前完整过程归档另行覆盖，不能用本项替代。

## R184 家目录自由工作

- [x] 主/子代理实际跨自己多个目录读写，绝对路径及正文不被改写；业务目录名不再充当别名。
- [x] 跨 owner 与符号链接逃逸拒绝；普通写工具不能篡改 runs 控制文件；Audit exact-scope 不扩权。
- [x] 新宿主记录只落 runs，旧 tasks 运行可恢复，主/子代理共享稳定整理指南，无新增取名模型调用。
- [x] 原长 TUI 继续旧任务并跨目录修改，再做普通多子代理文件工作，核对实物、运行身份和用量；FT-158/160。
- [ ] BUG-115：模型使用已有项目作样本时保护原件，实际交付与最终陈述一致；FT-161 仍有遗漏，不冒充边界修复已解决质量。


## R179/R180 当前收尾

- [x] R181 修复显示窗口二次截断后，原长会话 Ctrl+Home 实际看见八项目调研的第一条用户输入及思考/工具。
- [x] R182 两个原 session 的 TUI 先启动、随后启动唯一 Gateway；历史准备等待既有 readiness，不再提前退出。
- [ ] BUG-113：恢复历史与后台 notice 必须按 canonical 消息身份去重，不能把旧 final 追加到最新追问后。
- [x] BUG-114：R184 删除文件目标驱动的 exact rebind 与路径重写；FT-158/160 同 owner 两份历史原文件均直接修改。
- [x] R179 真实 network_status 返回宿主监听、逐端口 rc=1 与 not_explicitly_allowed；没有把本机监听冒充 LAN 成功。
- [x] R179 原 session 恢复后停止受管服务；原 listener PID 消失、18778 释放、单 Gateway 存活。
- [x] R178 同一长 TUI 完成五阶段用户任务，主/直属代理均自然终态；模型产物 4 项 skip 单独记不通过。
- [x] R180 失败先行：resume 缺显示事件；新增显示投影与定向顺序/幂等/隔离/不回灌验证。
- [x] R180 新 wheel 真 TUI 恢复已保存思考/工具/正文，继续原会话；只回看时模型调用数不变，无重派。
- [ ] Compact 前全部过程、长历史分页、child 展示环之外的恢复；P1 全部硬门不能提前打勾。

## R178/R179 长会话、受管进程与网络证据

- [x] 同一 TUI 第一轮八项目调研的 8 child 自然终态、main Compact 1；两个普通追问均进入同一 history。
- [x] 第一次追问写入原任务目录；纯聊天追问不创建新业务项目；第二个大任务创建独立目录和递归代理树。
- [ ] 第二个大任务收口后追加修改，再 `/exit`、`/sessions`、exact resume 核对完整历史与客户端回收。
- [x] 服务 final 后持续存活；相同端口再次启动返回失败，原服务 PID 与监听不变。
- [x] Mac 独立 LAN 探针失败，与宿主 firewalld/nftables 未放行事实一致，不把本机 curl 200 当外部成功。
- [ ] 模型正确使用宿主规则证据完成诊断；R178 连续两次只看 iptables，当前为真实失败。
- [ ] R179 防火墙查询 unknown/partial 修复通过 focused 和真实 TUI，停止受管服务后进程树、端口均回收。

## R177 TUI 终态补帧与父层名册校准

- [x] final 与 `Working → idle` 各自只请求一次补帧；即时 invalidate 仍是主链，空闲不常驻重绘。
- [x] 返回父层只合入 `updated_at` 不旧于父缓存的 typed child row，不解析文案、不改变 run/selection/history。
- [x] navigation/threading/prompt-toolkit/runtime/renderer focused、Ruff、PyCompile、strict code-size 通过。
- [x] `.10` 候选首轮真 TUI：5 child 收齐后，无人工刷新键直接出现 final，Working 同帧撤下。
- [x] `.10` 候选递归真 TUI：进入孙代理，待其终态后 Ctrl+G，父页约 0.2 秒首帧即显示真实终态；Enter
  精确回到同一 run/history，连续 Ctrl+G 返回 root 后三名直属代理和最终回复均正确。

## R172 空能力授权防线

- [x] 空 `requested_*`/scope 且只有 prose 的能力申请在落账前失败，不发 wake、不产生 grant/context refresh。
- [x] 旧记录/内部旁路的空请求由 auto-grant 第二道防线拒绝；path-only request 不被误批为 `tools=[]`。
- [x] capability input/auto-grant/parent resolution 相邻 49 项 focused 通过。
- [ ] `.10` 单 Gateway + MiniMax-M2.7 真 TUI：模型收到空目标错误后能自行改填 exact tool；不存在重复空授权。

## R171 用户控制取消后的父级恢复

- [x] TUI/Web 的 exact-agent Esc 复用唯一树取消入口；目标及运行中后代全部 durable 终态后，才发布目标节点
  的一份终态交接。模型 `cancel_subagents` 与 root `/stop` 不增加重复 wake。
- [x] root child 取消向当前 root conversation 发布 `status=CANCELLED` typed wake；grandchild 取消只释放
  exact direct-parent wait 并幂等恢复该 parent，不能越级唤醒 root。
- [x] user-controlled CANCELLED 即使仍有运行中 sibling 也进入 direct-parent attention；父轮内
  `cancel_subagents` 的 CANCELLED 继续合批。即时链与周期恢复都读 canonical cancel source。
- [x] `.10` root child 真 TUI：Esc 后 main 自动恢复、保留兄弟并补派替代任务。
- [x] `.10` 三层新 wheel 真 TUI：R176 停止一个 grandchild 后 coordinator 约 3.4 秒恢复且 sibling 继续；
  直属 child 父级约 8.4 秒恢复。child/grandchild 均覆盖 summarizing 阶段候选回滚，另有一次 post-commit
  停止反证两种边界没有混写。

## R170 递归创建共享容量

- [x] 根与递归 `create_subagents` 在同一 owner-local 创建事务内调用唯一 session/owner/task/per-call 容量计算；
  不从 goal、role、标题或模型文字推断配额。
- [x] owner 仅余一槽时，递归四项批次返回 `SUBAGENT_CAPACITY_EXCEEDED/not_started`，四项零落盘；容量状态
  不可读继续 fail closed。显式单次上限仍保留原精确错误。
- [x] 创建、批量、owner quota、hierarchy 与 dispatch 相邻 120 项 focused 通过。
- [x] `.10` 单 Gateway + MiniMax-M2.7 真 TUI：`ma-r170-110-nested-capacity-r2` 的 coordinator 请求四项时
  结构化显示 `requested=4/available=1/not_started` 且原批次零创建；另一路
  `ma-r170-110-grandchild-compact-r2` 的 depth-2 grandchild 自然 Compact `120,065→69,952` 后继续工作并
  终态，五名 grandchild→coordinator→main 逐级自动唤醒并 final。

## R169 Transcript Compact 普通请求缓存面

- [x] Gateway 前台/后台、手动 `/compact`、child/grandchild transcript Compact 均通过同一 typed surface
  复用普通模型轮的 stable prompt、system、原生工具 Schema 与 canonical provider messages；摘要指令只在末尾。
- [x] 摘要 auxiliary call 不执行工具；provider 返回 ToolCall 时弃用正文并落机械摘要，generation/checkpoint
  仍只由既有验证与 CAS 推进。
- [x] provider overflow 后尚未消费的 `tool_search` Schema 只从 typed carried archive 恢复；成功模型轮后的
  临时工具不跨轮复活。Gateway/background/child/TUI/control 相关 287 项 focused 通过。
- [x] `.10` 唯一 Gateway + MiniMax-M2.7 真 TUI：两路 main-only 分别触发自然 Compact 与手动/续作，
  独立 child 自然 `118,772→74,012`、`compact 0→1` 后完成；动画、generation、cache-read 和上下文连续正常。
- [x] grandchild 独立线程自然 Compact 已在 R170 真 TUI 通过；checkpoint、generation、动画、压后续跑和
  父级唤醒均由 exact depth-2 run 证明，未用 main/child 冒充。
- [x] child/grandchild 的 Compact 中途取消已由 R176 两路独立真机完成：同 operation superseded、候选丢弃、
  generation/checkpoint 不推进、目标取消、直属父级 durable wake 和 sibling 连续性全部一致。

## R168 Shell 前像范围与结算回收

- [x] `tool_output` 新登记行携带结构化 archive role 与 shell exclusion；旧行默认排除，显式 include 优先，
  未知/未来产物 kind 仍默认保护。
- [x] 普通直调与 ActionPolicy read-only 路径无需等待 ToolOperation 通知；无变化前像和 operation manifest 均
  回收，不改变工具结果。
- [x] mutating 路径只有在权威 ToolOperation succeeded/failed 写入成功后触发 `on_operation_settled`；持久化
  失败/UNKNOWN 不清理，幂等 replay 会再次通知，hook 失败不篡改已结算终态。
- [x] changed/invalid 前像 blob 在 manifest 删除后仍能通过 opaque registry ref 恢复；跨 owner 和 no-follow
  边界沿用 R167 合同。
- [x] `.10` 单 Gateway + fresh MiniMax-M2.7 TUI `ma-r168-110-u381-artifact-linear` 连续产生 5,000 行工具
  输出和 shell；tool archive 仍可读，snapshots 不再复制它，正常结算后 manifest/blob 均为 0。

## R167 Shell 产物保护迁出项目树

- [x] 正式 Agent 的备份根只来自 `HomePaths.owner_artifact_backups_dir`，经 registry 显式注入；Full Access
  管理员外部 cwd 与 WorkspaceOnly owner 均不得从当前项目推导内部存储。
- [x] backup ref 是 owner-local opaque v1 ref，blob 不保留原扩展；临时写、hash、fsync、atomic replace 和
  0700/0600 权限完成后才发布。resolver 拒绝绝对/穿越/symlink 越界。
- [x] unchanged 产物立即清掉本调用 blob/operation；changed/invalid 保留 shell 前内容；备份失败时命令
  not_started。两个 owner 的同名结构化身份仍物理隔离。
- [x] focused 中真实 `pytest --collect-only` 仅收集原 `test_generated.py` 一次，项目没有
  `data/artifacts/shell_backups`；HomePaths、ToolRegistry、runtime、sandbox 相邻回归通过。
- [x] `.10` 单 Gateway + MiniMax-M2.7 fresh TUI 从普通中文请求完成“创建报告→登记产物→shell 修改→读取”，
  并由 task tree、owner backup tree、工具账本三方确认零重复收集、零 no-op blob、opaque ref 不泄露宿主路径。
- [ ] 受管后台 shell 在 process session 真实退出后的产物复核另立合同；当前不得把前台通过冒充后台已覆盖。

## R166 Compact 成本与缓存复验

- [x] live Compact 的最低可达 token 在副本上使用真实 IR 删除器；UserTurn、RuntimeFacts、carried summary
  不得被空历史估算误算成可删除，探测不能改变 IR/window/generation。
- [x] cache-safe 摘要请求保持 parent 的 structured prompt、完整 provider/current messages、system、tools、
  model 与 thinking 配置；只在最后追加摘要指令，不另设输出 token。payload 对照测试已通过。
- [x] Compact 辅助调用没有工具 handler/循环；provider 返回 native tool block 时不执行并退 typed fallback。
- [x] live mechanical fallback 与 12K input budget 解耦，输出最多 4K，同时保留任务 head 和最新调用 tail。
- [x] `.10` 单 Gateway + MiniMax-M2.7 的 fresh 长 TUI `ma-r166-110-compact-cache` 已触发 main 2 代与 child
  1 代。child live-tool `118,696→73,217`，main live-tool `85,161→726`；事件流包含真实进度动画，随后继续
  原任务，8 个 child 与 main 全部正常收口。provider 请求账本持续记录 cache-read，Compact 无工具执行/失败。

## R156–R165 Full Access 设备与历史续作定位

- [x] Full Access 根挂载之后必须出现 `--dev-bind /dev /dev`，且 WorkspaceOnly 的最小设备树和 owner 墙不变。
- [x] completion 索引保留 host-authored `conversation_runtime`；Gateway 历史结果投影 exact `task_ref`。
- [x] Memory/普通来源不能靠同名 metadata 冒充任务路径；discover 与 browse 均保留合法 task_ref。
- [x] `.10` 单 Gateway + MiniMax-M2.7 真 TUI 直接运行常规 pytest，证明 `/dev/null` 实际可读写。
- [x] “回到刚才/先前/原项目”等自然措辞在完整默认工具集中把 `session_search` 推荐到首位，并示例限定
  `source_type=gateway_request`；不常驻注入历史任务菜单。
- [x] 默认前 5 条被聊天文本占满时，owner-local 超采样仍必须把第 6 条终态 task_ref 稳定提前；
  `queued/processing/running` 当前占位请求不得取得历史 task_ref。
- [x] 历史 task_ref 被模型复用成 owner-relative `tasks/...` 时，必须从结构化 owner home 地址还原，不嵌套 cwd；
  Full Access 下地址字段与安全墙分离，读通、非授权写仍拒绝、穿越 owner 仍拒绝。
- [x] 首轮从 owner home 晋升当前任务后，模型明确给出的任一 `<owner>/tasks/...` 历史绝对地址必须保持原样；
  归档的 `model_parameters` 与执行参数一致，普通 owner-home 占位路径仍重定向。生产缝隙失败先行回归及
  task rebase/runtime/tool gateway 共 206 项 focused 通过。
- [x] 同一 TUI 完成两个无关任务后，以自然措辞续作第一个；模型从 session_search 取得 exact task_path，
  写工具回绑旧项目且不在第三个目录重建。`.10` 的 `ma-r161-110-history-address` 中青岚/霜灯分别
  23/25 项 pytest 通过，续作青岚后原目录达到 30 项；第三轮占位 task 为 `ABANDONED`，原目录 successor
  为 `DONE`，未出现嵌套 `tasks/`。同轮自然 Compact 提交 generation 1。
- [x] 精确回绑成功后，只有系统脚手架的 superseded 占位目录会被移除；ConversationTaskLink 审计事实保留。
  删除前复核 owner/tasks 路径、workspace identity、完整允许文件/目录集合和 symlink；任意用户内容均不动。
  R165 真 TUI 的第三轮占位 link 仍可审计，但物理 tasks 只剩 A/B 两个业务目录。
- [x] 当前请求先在占位 task-path 建立 Todo、再 exact rebind 到历史 task-path 时，本代 display plan 与原 exact ids
  必须原子迁到目标 canonical 账本；后续 `{id,status}` 更新直接成功，源账本不留第二份。不得根据 final/pytest
  文本自动打勾，也不得把任务路径切换误报成模型参数错误。失败先行生产缝隙、focused 与 R165 `.10` 单
  Gateway + MiniMax-M2.7 真 TUI 均已通过。

## 新任务/旧项目续作工作区裁决

- [x] `completed/interrupted` 普通 link 不凭 sticky pointer 吸附新回合，新工作落在新目录。
- [x] exact request 恢复、active task 和未结束 Goal 仍在首次模型调用前获得原 cwd。
- [x] 同一 task root 存在多代 terminal link 时，精确写入路径选最新一代建 successor。
- [x] 本轮占位任务在回绑旧项目时转 `superseded`，request/thread/link/path 四处一致。
- [x] 跨两个 canonical root 或同根多个 active executor 时 fail closed，不用新旧、产物数量或用户措辞猜。
- [x] 真 MiniMax-M2.7 TUI `ma-r155-local-workspace-routing` 先验无关新项目，再验原项目续作。

- [x] Compact 进度必须携带成对的 `source_kind/commit_authority`，Gateway、后台事件和 TUI 使用同一校验器；
  transcript、持久 live-tool、turn-local 三条链分别显示不同含义，不能靠 operation id 或中文猜。历史 v1
  双字段都缺失时仅作为 `legacy/legacy` 展示，部分缺失与矛盾组合拒绝。当前 223 项 Compact/Gateway/TUI
  focused 通过；同 wheel 真 TUI 标签与自动 live-tool 触发仍待验。
- [x] 同一 Compact generation 内不同 operation 不得共享百分比高水位；live 候选 superseded/failed 后，
  transcript fallback 从自身 started 进度开始，旧 operation 迟到事件不能污染当前块。同 operation 保持单调。
  85 项 focused 通过；`ma-r150-local-compact-resume` 手动动画、generation 3→4、计数只在提交后增加及压缩后
  记忆追问均通过。自然触发的 fallback 失败链仍列为后续真 TUI 覆盖。
- [x] Compact 中途收到 typed stop/cancellation 时，transcript、active-turn archive 与 native IR 都必须在
  摘要、候选改写、checkpoint 和 generation CAS 的安全点停止；CAS 前只发布
  `superseded/candidate_discarded`，不得推进 generation/cursor 或失败熔断，IR/tool-context 必须恢复。
  R154 主代理 MiniMax-M2.7 真 TUI 已在摘要 20% 时 Esc 通过，focused 覆盖五个竞态边界；R176 又分别在
  直属 child 与 depth-2 grandchild 的 summarizing 阶段完成候选丢弃、generation/checkpoint 不推进、精确
  取消和直属父级恢复，三层真机边界现已闭环。
- [x] `/exit` 后新 TUI 的 `/sessions` 必须只列当前 owner，会话由 exact session id 恢复；历史 final、child
  roster、Compact 代次继续存在。R149→R150 真 TUI 已通过；当前 `/sessions` 是只读列表加恢复命令，不冒充
  终端交互 式列表内直接选择。

- [x] 子代理详情 payload 必须先校验 exact run 并形成单一展示投影，再原子更新名册/终稿去重和活动区；拆分
  不得改变 goal、事件游标、历史或终态。78 项 navigation/input/PTY 与 strict code-size 通过；
  `ma-r149-local-child-navigation` 真键盘验证进入、Ctrl+O 保持 child、返回 main。
- [x] child 处于 Ctrl+O 详细 transcript 时，Ctrl+G/Alt+Left 也应一次返回父代理；只调整 typed key routing，
  返回后保留详细模式，Ctrl+O 独立收起，Esc 仍用于停止当前代理。79 项 focused 与
  `ma-r151-local-child-detail-back` 真实按键通过。

- [x] 活动/终态、收起/展开的 thinking 与 Ctrl+O 提示必须统一使用 terminal dim；用户消息在独立深色底上
  使用 bold，assistant 正文不得继承。52 项 renderer focused 与本机 `ma-r147-local-tui-style` ANSI capture
  通过；同一 wheel 的测试机终端观感仍待部署后复验。

- [x] 普通会话或 `/goal` 的 TaskRun 只能在持久 ConversationTaskLink 已进入不可复活终态，且 exact TaskRun
  下唯一 root 与所有 child 都终态时关闭；任一事实仍活跃、缺失或冲突必须保持开放。root/最后 child 的竞态和
  Gateway 启动重放都使用同一 CAS，UNKNOWN ToolOperation 不得被顺带裁决。117 项 focused 与本机
  `ma-r148-local-goal-taskrun` 真 TUI 已通过；Goal 更新、root 终态、TaskRun 单事件三方账本一致。

- [x] 权威 run/attempt 绑定后，skill/tool snapshot、上下文准备或 provider capability probe 在首个模型调用前
  抛错，也必须让 exact run/attempt 同步进入 failed 且 ended_at 非零；TUI 回到空闲后不得留下幽灵 RUNNING。
  R143 本机 `ma-r143-local-preflight-closeout` 先以缺密钥真实复现，再恢复同一 Gateway 完成下一轮；失败轮与
  成功轮分别为 failed/done，相关 focused 34 项与扩展 39 项通过。

## Computer Use

- [x] `my-agent[computer-use]` 在目标 Python 3.11 runtime 可安装，包版本固定为 `0.3.13`。
- [x] 默认/普通 owner/WorkspaceOnly 不注册 `mcp__computer_use__*`，local/main + Full Access 才注册。
- [x] Xvfb 真 TUI 经 MiniMax-M2.7 完成 list/OCR/activate/scroll/type/key/OCR 后置验证，并留下 tmux 名称。
- [x] 危险动作 exact approval、同一 MCP 取消通道 `/stop`、单 Gateway/单 MCP 子进程与普通 owner 隔离都有证据。
- [x] 原始 image 未进入 provider 时不得写成“模型已看图”；Unicode 输入限制如实保留。

- [x] R121 fresh 真 TUI 必须原样显示已持久化的 `/goal ...`，固定面板 Goal 在直属 child 前；`↓` 高亮 Goal、
  `Enter` 展开完整 objective/状态/token/时限且不切代理页，`Ctrl+G` 收起。命令不得进入模型队列或产生第二个
  control dispatch。`.10` 唯一 MiniMax-M2.7 Gateway 的 `ma-r121-110-u374-goal-thinking` 已物理按键复验。
- [x] fresh 真 TUI 中 live thinking 必须持续展开；每个结束 thinking 自动折叠为灰色摘要，`Ctrl+O` 展开全部
  历史、再按恢复折叠，assistant final 仍直接显示。首轮已验证 thinking 折叠/展开，但暴露 active Goal final
  被 delivery gate 抑制；中间 wheel 的同 thread 追问又暴露空 `task_path` 被误判为持久化损坏。两项 focused
  均已修，且非空丢失路径仍 fail closed；最终 wheel 同 thread 已验证 thinking 折叠、直接 final、canonical
  user/final、`Ctrl+O` 往返和 Goal detail 往返。PgUp/child 的既有独立视口合同继续由专项回归覆盖。
- [ ] 普通任务达到工具轮上限或保留 open Todo 后不得注册/消费 ordinary auto-resume；旧 policy 启动时 typed
  退役。显式 `/goal`、直属 child lifecycle wake、用户插话和 provider transient retry 仍应分别恢复，不得把
  “普通模式不自动续跑”误做成所有恢复都失效。focused 已通过，待长任务、重启和插话真 TUI。
- [x] 等待中的非终态 child guidance 使用同一 AgentRun 的唯一 pending successor；并发提交、首次启动失败重放
  和已消费 HTTP 重放都不重复消息/attempt/runner，终态 child 只读。所有受影响 focused 三组已通过。

- [x] provider context observation 只能在 exact backend/model/protocol/system/stable-prompt/tools 指纹与 Compact
  generation 匹配时校准压力；动态 messages 不使它每轮失效，缺失/漂移回退原估算，成本仍只认 provider
  ledger。R116 u323 主 compact 0 且立即追问 cache-read 32,418；u324 自然越线只 Compact 一次。
- [x] 本地 `channel=tui` 的后台 commentary/final 必须先进入 canonical ConversationStore，再投影 notice；没有
  TUI adapter 时 delivery 可 `not_applicable`，但下一轮历史不能缺。相同 wake 重试只保留一个 final。
  R117b u326 在追问前 raw 验证 schema v8、6 commentary、1 final；独立 runtime 的 u327 又验证
  4 commentary + 1 final，两个样本的零工具追问均准确召回。
- [x] wheel 部署不能只看 `pip install`、PID、端口或 cwd；必须核对进程 executable、editable metadata、
  module/schema。R117 u325 旧 editable venv 为失败样本；当前独立 `runtime-venv-efa90d1` 只运行一个
  MiniMax-M2.7 Gateway，u327 fresh TUI 通过。

- [x] R114t fresh MiniMax-M2.7 TUI：pending 首轮不暴露 owner 根为 cwd；首批多 child 直接在 canonical
  task root 工作，零 stale-owner-path 拒绝/申请；任务目录使用可读标题。94 项 focused 与 `.10`
  `ma-r114t-110-u313-cwd-subagents-wheel` 通过，6 名 child 同根、owner-root 泄漏/路径拒绝均为 0。

- [x] 同一 Gateway 上每个 owner 的 `update_persona` 写入限频必须独立；owner A 的三次写入不能占用 owner B
  的额度。第四次同 owner 写入应返回 `PERSONA_UPDATE_RATE_LIMITED`、`effect_outcome=not_started` 和可退避
  建议，不能进入 UNKNOWN/DIRTY。38 项 focused 通过；`.10` 两个 fresh owner 并发各写 3 个事实全部
  `SUCCEEDED`，同 owner 新 TUI 无工具读取准确回读，彼此不可见。

- [x] provider 已明确返回 `context_overflow`、但 completed transcript 暂无可压消息时，Gateway/background main
  必须把当前 request 的旧工具 archive 提交到同一 ConversationThread `live_tool_ir` checkpoint/CAS 后再试。
  完整 archive 继续恢复 tool-round/one-shot/effect 权威；模型只隐藏已提交 checkpoint chain 的 exact source
  call ids，孤儿候选无权隐藏。focused 与 `.10` u314 fresh 长 TUI 通过：8/8 child、三代 canonical Compact、
  367 行 final 报告、3.17M provider cache-read，Todo/产物/终态均连续。

- [x] `memory_compact_auto_trigger_percent=90` 与 `memory_compact_recovery_target_percent=60` 必须由 main/child、
  live-tool/transcript 共用；128k 窗口应优先压到 76.8k，为下一段工作留空间。R162 进一步证明 60% 不能作为
  第二提交硬门：固定上下文使目标不可达时，最佳候选只要 `<115.2k` 就必须提交，`>=115.2k` 才回滚。
  u314 三代均达到优选目标；新的低于 trigger fallback 已有 focused 覆盖。

- [x] R163 fresh MiniMax-M2.7 长 child 必须自然跨过一次 active-turn context pressure：同一 operation 最终
  completed、exact child checkpoint 存在、generation/TUI `compact` 同步从 0 到 1+，原 attempt 继续且最终
  Working 收口；不能再出现多次摘要用量已发生但全部 candidate_discarded、靠 fresh handoff 账外降量。

- [ ] 后台 main 在一个 scheduler slice 连续推进 8 代 Compact 后必须以 typed yield 干净让出：claim 结束为
  finished、原 wake 未消费、无 policy failure/假 RuntimeError，下一 slice 从 canonical generation 续跑。
  focused 已通过，旧 u305 假失败样本保留；仍待 fresh 长任务自然跨过 8 代后勾选真机部分。

- [x] `search_text` 省略 `literal` 时按 ripgrep 正则解释，`A|B` 可命中任一分支；schema 明示
  `literal.default=false`。`literal=true` 仍按完整普通文本匹配，rg/Python no-match typed envelope 与 owner
  路径边界不变。58 项 focused 已通过，下一 wheel 待用真实 MiniMax-M2.7 TUI 复现原多分支搜索。

- [x] R114o 唯一 Gateway 快速就绪时，fresh TUI 不按键也要自动清除启动动画并显示
  welcome/输入框；deferred-loop focused 已通过，`.10` 同时新开 8 个 scoped TUI，5 秒后
  不按键全部自动进入正常首屏。
- [x] R114l 八子代理长调研中，任务运行时 child 从约 124,049 token 压到约 53,711 token，
  generation 0→1；终端交互 child 也 `compact 1`。八名 child 全部完成，八份分报告和 21,876-byte 横向报告
  存在，main final 直接显示、Working 撤下。主代理连续 `compact 3` 的余量问题单列 R114n，不掩盖主链正证。

- [x] 后台 main 的 native live Compact 在完整摘要覆盖旧工具轮后，必须同时回收该轮 assistant 正文、
  ToolCall 和 ToolResult；无摘要的普通 window/PTL 仍保留 assistant 正文，UserTurn 不得删除。若完整请求仍
  overflow，后台必须在同一 scheduler slice 强制 canonical transcript Compact，携带已完成工具与插话后续跑；
  无 generation/结构化进展要有界停止。focused 与 R114l `.10` fresh 八-child 主链均已通过；连续代次成本
  由 R114n 另行验收。

- [x] Gateway active turn 在已执行 `task_progress/create_subagents/write`、尚未提交 assistant 终态时重启，
  reconciler 必须写 exact `gateway_active_turn_recovery.v1`，下一 provider call 从同 owner 工具索引按
  `conversation_request_id` 续上已完成调用；Todo id/数量与 child 名册不变，不能只重放原 prompt 后另建计划。
  foreign request/child 索引不得混入，索引不可读必须 fail closed。focused 与 R130 主代理活跃点已通过；
  R131/R133 又分别证明 create_subagents 后 main 等直属 child、coordinator 等两名直属 worker 时顺序重启不
  重复派工，并能逐层自动 wake 到自然 final。

- [x] 唯一 Gateway 重启落在 child 的 `AgentRun` 已终态、`SubAgentTask` 尚未收尾的窗口时，监督器必须按 exact
  `agent_run.completed` 宿主事件补齐普通 runner-result 投影；不得 abandon、不得增加 generation、不得重跑
  已完成/失败/取消 child。恢复器自己生成且没有 `runtime_status` 的 cancelled 终态仍须 requeue 真崩溃轮。
  三种自然终态与既有崩溃恢复 focused、R114j u288 终态窗口与本轮 u315 七个 live child 顺序重启均通过；
  终态 generation 未增加，live run 才 reclaim/revive，roster 无重复。

- [ ] Native prompt 必须用 typed `CacheStructuredPrompt` 表达稳定 system、仅供诊断的 current-user 副本与
  动态尾部，不得按标题、用户正文或模型语言猜边界。真实 wire 必须保持「committed summary/已结束消息 →
  当前 user → append-only 当前轮 IR → 当前事实」，当前 user 只发送一次；Anthropic 只有一个最新历史断点，
  OpenAI-compatible 保持相同时间顺序供 KV 缓存。Workspace 必须在动态尾部；关闭缓存/text 路径不得丢正文，
  canonical IR 与 Compact 不变。lifecycle wake/恢复/插话的 `runtime_injections` 也必须只在 IR 后动态尾部，
  不能改写旧会话消息。流式请求必须使用 request-local first-event + rolling idle，持续有效
  data 不受固定总墙钟误杀；默认 10,800 秒 max 只给极大慢输入，小请求仍按 token 估算。
  本地 focused 已通过；`.7` r62 终态账本已有
  6,097,485 cache-read；本地 r64 首段也有 40,960 cached，但长任务因本地流 600 秒超时且只派出两名
  child 而失败。新 `.10` 原长会话两个普通追问已命中 24,168/24,388 cache-read；`.7` r65 已跨旧 600 秒
  继续运行但尚未终态，故仍不能勾选。OpenAI-compatible 还必须接受统一 thinking observer；有
  `reasoning_content` 时思考增量、封口、正文顺序和工具续轮回放均不得丢失。

- [x] child capability grant 与 exact tool approval 继续分账；child 的实际 `BackgroundTranscriptSink` 遇到
  `ask` 时必须把完整 request 上送所属 owner TUI，并阻塞原 ToolCall。main 与多个 child 的确认共用一个
  FIFO，页面切换不隐藏 root overlay；无交互 consumer、租约过期、取消、终态、损坏或 stale 决定全部
  fail closed。179 项 focused 与严格 gate 已通过；`d29caab` 部署 `.7` 唯一 Gateway 后，fresh r52 已证明
  面板标出 child，Yes/Yes always/No 均对服务端原 request 生效，批准后同一调用原地续跑。

- [ ] fresh MiniMax-M2.7 child 的 output contract、task packet、workspace refs 和 runner prompt 均不得暴露
  宿主 `final_report/output.json/runner_result`；child 完成业务后直接自然 final，不额外调用写工具生成内部
  报告。宿主仍须从最终回复生成完成信封/交接投影，显式同名业务文件继续按 `required_file_refs` 交付。
  完整 execution-context 只供宿主审计，prompt 只拿安全 write boundary/context bundle；attempt 恢复只见
  checkpoint/summary/task。父—子—孙相关 102 项 focused 与本地严格 gate 已通过，待 `.7` 新 child 真 TUI。

- [ ] main/child 每个已结束工具回合只生成一次不可变 `conversation_terminal_tool_fold.v2` metadata；默认
  300 秒内读取固定 hot-tail，过期后读取固定 cold-fold，旧 V1 恒按 cold 兼容。完整输出仍只在 owner archive，
  冷热阶段内部不得每轮重写，
  `/context` 必须把折叠回合/调用数与真正 `compact N` 分开；真正 Compact 能吸收折叠。overflow→Compact→
  继续回复的累计模型账必须按物理调用游标写增量，不能复用事件 id 或重复累计 cache-read。289 项 focused
  旧 V1 的 289 项与 `.7` MiniMax-M2.7 连续轮已准确续接两次 Read；V2 本地 32 项和真实模型 A/B 已通过，
  待部署 fresh TUI 验证一分钟追问、超时切 cold、main/child 同账与 provider usage 后再勾选。
- [x] 手动 `/compact` 成功后，HTTP/operation receipt 必须把 canonical `task_status.compact_generation` 原样
  交给 TUI；不能解析中文回执猜次数。TUI 应立即显示 Compact 边界和新代数、撤下压缩前 Context 数字，
  并由下一次真实模型调用刷新 provider-visible 用量，不能为刷新界面额外请求模型。idle/resume 即使没有
  Working block 也必须水合 activity 中的代数，迟到旧帧不得回退。251 项完整相关 focused 与严格 gate 通过；
  `.7` 原 tmux 已直接水合 generation 1、手动提交 generation 2、刷新到约 28.5k，并在下一轮命中 17,019 cache-read。
- [x] 同一长 conversation 的完整进度账本继续保留历史，但底部 Todo 只展示当前 ordinary user turn 的 exact
  `display_plan(generation_id/revision/item_ids)`；新回合立即清上一代，迟到的旧 poll、tool progress 和最终
  notice 均不得把旧 Todo 刷回来。362 项 focused 已通过；`.7` 唯一 Gateway 的原长 session 两轮追加均清掉
  原 `完成 24/35`，运行及终态未被旧快照刷回。
- [ ] 主/子页面各自上翻后保持阅读位置；切回页面或提交一条有效消息时，control 锚点必须通过真实
  prompt_toolkit `Window.get_vertical_scroll` 恢复/粘到底部，而不只修改内部 cursor。focused 已覆盖真实
  Window `_scroll`；原长 main 页已用四次 PageUp 看到 `Jump to bottom ↓`，随后正常提交立即回底。child 页
  独立锚点的物理复验仍待下一次有运行 child 的真实任务，故整项暂不勾选。
- [x] 主代理 `compact N` 只读成功提交的 `ConversationThread.compact_generation`，子代理读自己的 canonical
  generation；进度百分比、屏幕历史和模型正文不计数。128k 窗口、90% 压缩点下，68k（53%）显示
  `compact 0` 正常；95 项 Compact/TUI 组合回归通过。
- [x] 同一 request 的工具前后 commentary 以 `assistant_part_id=commentary:N` 完整落账，final 使用
  `assistant_part_id=final`；恢复/配对不重复、不丢段，长报告无需 `Ctrl+O` 即可直接看到。多次模型调用的
  thinking 各自按时间顺序封口，空首块消失，终态不留 spinner；滚轮每格只移动一行。完整相关 focused
  与本地严格 gate 已通过；`.7` r54/r55 已实测过程和长报告直接显示。`ae3fd1e` 又把流式完整 thinking
  收到 provider 原 block-stop：新真请求的唯一 `assistant_thinking` 严格早于首个 `model_delta`，TUI 最底
  稳定块为 assistant final；旧流 `thinking_delta -> model_delta -> assistant_thinking` 回放也只保留一块。
- [x] 普通 user/assistant 历史不得按固定字符数裁剪；预算回收只移除最老的完整消息，真正旧前缀替换只由
  Conversation Compact 执行。provider ledger 应持续记录 cache-read。价格样本在缓存价 0.1/1 时分别为
  `1,811,699.1`/`2,023,236`，不得用屏幕 Context 猜缓存命中；真机已有 42,107、12,987、17,019 等
  provider cache-read 记录，r55 的 105.8k→下一轮约 43--50k 已核对为终态工具折叠而非漏记 Compact。
- [x] 任务晋升后 main/child/grandchild 的默认 cwd、产品写根和相对 output ref 全部落同一
  `<owner_home>/tasks/<task_path>/`；不得继承 daemon `/root`、客户端临时 cwd 或另造 child 家目录。
- [ ] `process_session(network_status)` 只读 exact managed process tree listener 和主机防火墙显式规则；
  non-loopback 监听仍显示外部探针必需。`.7` 真机必须由 Mac 实际请求验证，失败时 Agent 不得声称局域网可达，
  测试者不得旁路改防火墙。r55 已通过“不误报”部分：localhost 是真实游戏，Mac 外部请求失败，模型只报
  `unverified_external_probe_required`；因为目标 LAN 访问仍失败，本项保持未勾选。

- [ ] 长 main/child 遇到异常链中的 typed `socket.gaierror` 时，普通 JSON 与流式 provider 请求都先按
  2/5/15 秒有界退避，耗尽后保持 transient 供模型轮恢复；不得因一次 DNS 抖动终止数小时任务，也不得把
  畸形 URL、认证/额度、代理错误或普通字符串升级为重试。两个失败优先回归已转绿，待完整 focused/真机。
- [ ] 批量编码 `create_subagents.items` 必须由模型写清同一个目标目录和互不重叠的文件/模块边界；职责宽到
  会覆盖兄弟项或会修改同一文件/模块时应分批。该项只靠 会话运行时 式软派工纪律，不恢复 cwd/目录锁、不解析
  goal 猜写集；13 项 focused 已通过，待唯一 Gateway 部署后的下一轮真实复刻验证派工参数。
- [ ] 同一直属父级已有 PLANNING/PENDING/RUNNING/BLOCKED/PAUSED child 占用 exact covers 时，新的
  `create_subagents` 必须在任何 run 落盘前整批拒绝；只有显式 `replacement_for_run_ids` 可以接管。
  replacement source 必须同父、唯一且未被接管，edge 落账失败的新 child 启动前取消。同批 output 可共享
  父级 task root，不形成目录锁，越过父 workspace 仍拒绝。focused 已通过，待 `.10` fresh 真 TUI。
- [ ] 逐个结束的 sibling child 即使共享 `root_task_id`，运行中的 `task_local` 安全点也不得读取或确认主代理
  lifecycle mailbox；每份 completion 必须保持 pending 直到 exact conversation parent 消费。focused 已用
  “child 零注入/零 ack，随后 parent 成功消费同一 id”覆盖，待原 `ma-97468f3-longchain-r27` 的七份调研
  自然补齐与后续多阶段长链真 TUI 验证。
- [ ] 长 TUI 的 child lifecycle wake 后，root 即使显式用当前 typed task id 调用
  `task_progress(action=read)`，也必须返回界面正在展示的同一 task-path Todo；不同历史 run id 仍保持精确
  隔离。部署后在原 `ma-97468f3-longchain-r27` 或其恢复 session 自然触发，不人工篡改账本。
- [ ] 后台 main/child 的逐 token thinking 不得一片一条挤满公开事件环；首片应即时可见，后续同块按短时间
  或字符阈值合批，完整终态仍恢复全部正文。47 项 focused 已通过，待 `.7` 唯一 Gateway 与同一长 session
  普通追加轮证明 TUI 不再数分钟停在旧工具后一次性追赶。
- [x] `run_command(run_in_background=true)` 返回的 session 必须能由同一用户会话通过
  `process_session list/status/wait/network_status/stop` 管理；wait 不派生 shell sleep，另一 TUI/owner 即使猜到 id 也看不到
  日志、不能停止。默认 coding role、动态 capability grant 和旧任务恢复也必须自动补齐该依赖，owner 显式
  禁用仍优先。后台命令必须归 conversation session，不归 one-shot child runner；原 runner 退出后 detached
  host 继续持有 bwrap，受保护记录按 scope/store/PID 出生指纹水合，终态不回退。50 项 shell/process 与
  54 项角色/授权组合 focused 已通过。`8f50d19` 部署后的 fresh r53 已证明 child DONE 至少 27 秒、root final
  至少 12 秒后服务仍在；另一进程同 scope 水合 running，错 scope 隐藏，stop 后完整树退出且记录为 killed。
- [ ] 同一个长 TUI/同一个 conversation 依次完成“大型多子代理任务 -> 两个普通小任务/追问 -> 第二个大型
  多子代理复刻 -> 独立审计/修复”时，历史、工作区、输入队列、Todo、child 列表、Compact 计数和 main 状态
  必须连续且互不串轮；每阶段只给一次普通中文 prompt，测试者只观察，不替被测 Agent 补产物。

## 子代理 TUI 插话与容量

- [x] `.7` 唯一 Gateway 的 fresh TUI 中，完成 child 即使留下旧 `current_tool` 也只显示终态，详情工具/思考
  动画全部封口，耗时冻结在 canonical `ended_at`；父级收到 `completion_message` 和精确 refs 后不再搜索
  `child_outputs` 或内部 runner 文件。停止 root 后同一 TUI 追加汇总消息，即使 workspace task id 已换成
  新 follow-up id，也须按 exact canonical task-path lineage + root/direct parent 收到全部最新 completion；
  其它 workspace、孙代理和 `runner_result_json/output_json` 不得串入。
- [ ] 没有 `rg` 的远端宽目录 `search_text` 能被 `/stop` 及时打断；默认 content 页命中后不扫描余下目录，
  无命中超出 20,000 文件/10 秒时明确标注 `scan_limited`，不得显示成完整“没有找到”。
- [x] `search_text` 模型合同只表示已有本地文件的字面/正则搜索；每次返回 `local_text_search.v1` 的 exact
  path/mode/backend/complete/status/hint。完整 no-match 与 scan incomplete 必须分开，GitHub/互联网任务改用
  web_search，宿主不自动分词或猜路由。32 项组合 focused、MiniMax-M2.7 三例 tool-choice 与 fresh r59
  no-match→普通追问均通过；typed `scan_complete` 已进入真实工具卡和下一轮历史。
- [ ] 同一个 fresh TUI 默认鼠标模式下，滚轮/PgUp/Ctrl+Home 历史、中文左键拖选自动复制和右键重复复制
  同时可用；完整右键 down/up 与仅 release 两种序列都只复制一次，选区高亮不被清除。真实 tmux 版本必须
  用 `list-commands` 证明写穿参数存在；普通 tmux 使用 `set-buffer -w`，不得再 mock 不存在的
  `load-buffer -w` 为成功，外层粘贴仍由用户 attach 后确认。

- [x] 运行 child 的用户消息 receipt 带 exact `expected_turn_id`，reserve、provider submission 与 consume
  使用同一 attempt；不得再出现 `guidance submission reservation mismatch`。
- [x] child guidance 只在 provider 接收边界以普通 user message 写入 exact child thread；消费事件
  同时携带有界展示副本和 exact message id。完全退出/恢复 TUI 以及重启唯一 Gateway 后，
  researcher-7 详情仍在原位显示用户补充要求；不能仅依赖当前 TUI 的 pending 文本。
- [x] child 没有 pending/running attempt 时，入口明确拒绝、用户输入保留且 guidance message box 零新增。
- [x] 默认 root 一次可原子创建 8 名 child，`per_call_cap=0`、session cap=8；第 9 名整批拒绝，终态释放后
  可继续创建，历史累计允许超过 8。
- [x] root 与 child 在默认 终端交互 鼠标模式可直接用物理滚轮查看历史，并继续支持 `PgUp/Ctrl+Home`；
  常驻 footer 显示应用内“拖选/右键复制”和 `F6 原生模式`，切换后改为 `F6 恢复滚轮`。
- [x] 历史累计 child 超过八项时，`↑/↓` 选择窗口必须滚入 exact 选中项并显示 `›`；`Enter` 进入的 run id
  与屏幕高亮一致，renderer 的八行裁剪不得把选中项藏在省略提示后。
- [x] 已终态 thread 所在 owner 在 Gateway 重启后保持 cold，但 TUI 仍必须重放 exact channel
  binding 下已提交的 assistant notice。读路只扫该 owner 固定深度的已存在 conversation
  roots，不建目录、不加载模型/工具 Agent、不读另一 owner。u283 原 TUI 已在重启后补出完整 final。
- [ ] main/child 页面主动上翻时，被动新输出继续保留阅读位置；一旦提交一条通过输入校验的真实消息或命令，
  当前 viewport 必须立即回到底部并恢复 follow，让用户看见自己的消息和下一轮输出。空输入、超限输入和
  终态 child 拒绝不得改变滚动位置。
- [ ] Todo 的 canonical 项全部完成但仍有未显式映射到可见 Todo 的 typed active child 时，标题显示
  `子代理运行中 N`；有 exact `progress_item_ids` 的 child 仍只原位更新对应 Todo，展示层不得重开账本或按
  child 名称/goal/输出猜关系。

- [x] 工具运行时改动相关 focused tests 通过（Schema/runtime/protocol/policy/executor/ledger/output/concurrency/cancel/compact 矩阵）；其他并行模块仍按各自条目验收。
- [x] 默认可恢复工具失败只返回当前模型返工，不按同类失败次数结束 turn；同一批后到的同工具成功会
  撤销早到的 active halt，不同工具成功不误清。精确同参机械重试仍只拒绝动作，只有显式 typed hard
  policy 可进入 repeated-failure 硬收口。43 项 focused 回归已通过；fresh TUI r5 真机证据仍按下方重型
  任务条目验收，不能提前勾成端到端通过。
- [x] `context_scope=isolated` 的自然回执物理调用仍进入 model-call/cost ledger，但不投影 provider thinking、
  main/child context 或 Compact；系统生成的一项 `items` 仍按 exact parent 连续编号；Todo 标题按 typed
  status 显示 `完成 X/Y · 进行中 Z`，四行折叠只作视窗。200 项 focused 回归及 r7 真 TUI 均已通过。
- [ ] root 默认 prompt、普通 child runner 与 child lifecycle wake 必须共用同一范围保真/验证软纪律：骨架
  只算阶段，安装/构建/启动/关键路径失败后重跑，不把忽略错误包装当成功，不为绿灯删除、skip 或放宽
  能暴露当前缺陷的有效测试。配置 YAML 与 dataclass 已字面一致，253 项 focused 与本地严格 gate 通过；
  仍待 r8 真 TUI 验证。
- [ ] task-local child 只能由自己的 runner/agent thread 续跑：finalize 不登记 root
  `ordinary_task_resume`，任何绑定 canonical child task 的旧后台 policy/wake 在 root 模型调用前退役或
  无模型确认。runner 结果回到 `PENDING` 时只回收 exact run 的启动占位，共享批次 PID 仍活不能阻塞
  即时续派。相关 focused 回归与 r9 的身份/单执行器真机证据已通过；r9 未自然触发 PENDING，故共享 PID
  下即时续派仍待后续 fresh TUI 正向样本。
- [x] `create_subagents.items` 只承载可立即并发且彼此不等未来结果的任务；goal 里写“先后”不形成执行顺序。
  后项必须读取前项修复/产物/结论时，父级先只创建前项，等 typed lifecycle wake 后再创建后项。该规则只在
  模型合同中软引导，不解析 goal/role、不恢复机器验收或第二套依赖调度器；15 项 focused 已通过，`.7`
  单 Gateway 真 TUI 已证明 fixer 完成并触发 typed wake 后才创建 tester，两者没有并发。
- [ ] 批量 `create_subagents` 只要求每个 `items[].goal`，不再额外强制一份无机器用途的顶层 `goal`；单派仍
  必须有非空 `goal`，空批次/空 item 仍原子拒绝。root/descendant/schema 65 项 focused 已通过，真实失败
  样本已留存；待部署后在同一长 TUI 的下一批自然派工中复验。
- [ ] 同一 exact root 的 completion mailbox 可按预算分批，但延期 sibling 不能丢、不能被瘦事件入口提前 ack，
  也不能因 canonical 树已全终态就收口。4k budget + prompt limit 5 下，7 路长结果已在多个模型轮全部读取，
  中间轮保持 active/suppressed、最后仅投递一次；关键路径 4 项通过，待同一长 TUI 复刻批次自然复验。
- [ ] 未显式声明 `output_files/output_refs/artifact_refs` 的普通 child 不生成系统 Markdown 业务合同；父级
  只从 typed status、最终回复、`final_report_ref` 与真实 artifact refs 接收结果。旧
  `system_default_output_ref=true` 可恢复但不进入 runner contract、completion wake 或
  `child_result_index.expected_outputs`；显式产物仍走现有锚定、写授权和冲突锁。focused tests 完成后还须
  fresh r10 原样 Prompt 4 真机验证。
- [ ] child lifecycle wake 沿同一 root active turn 续接：exact task link 的原始 objective 仍是
  `User Task/root_user_prompt`，wake 仅作 runtime continuation；root canonical tool index 按 completion
  信封的 exact `conversation_request_id` 恢复并排除 child/其它 task/同 durable task 的其它用户轮，
  one-shot 派工不重放，每工作片新增工具额度不缩水；字段落盘前旧行只按同值 `request_id` 兼容。
  focused 回归与 fresh r12b 已证明语言、范围和嵌套派工参数不会在连续 wake 后重置。2026-08-25 又补齐
  externalized index 的 typed execution/operation 终态恢复与 root link completed 回归；仍待同一长 TUI
  部署样本证明最终回复后 Working 收起，才勾整项。
- [x] Compact 权威探针兼容没有 `task_attributes` 的轻量/旧调用方；统一路径解析把 `None`/空白保留为
  “没有路径”，不能变成 `<repo>/None` 并压过真实 ToolRegistry cwd。工具轮 28 项与 cwd 集成回归通过。
- [x] 后台 Task Runtime State 与 `task_progress` 工具使用同一 task-path 账本编号；回归同时放置正确路径账本
  和错误 request-id 账本，只允许前者进入模型上下文。child 终态同步、Goal continuation 与 TUI 投影复用
  同一 helper，普通 open Todo 仍不构成机器验收或普通任务自动续跑门。
- [x] shell 对内部 child 状态路径的执行前拒绝声明 `WRONG_STATUS_SURFACE/not_started`；canonical dispatch
  即使记录 handler 已进入，也归确定性 failed 并把原因返回模型，不触发 unknown 硬停。真实副作用未知
  回归继续保持 unknown/fail-closed。
- [x] Compact 公开压缩点不受单次 `save=False` 影响；128k/90% 始终投影 115.2k，
  但 120k 的 no-save 回合仍不执行持久 Compact。压缩策略与完整窗口硬限不得在 TUI 层硬编。
- [x] 递归管理面只含 create/guidance/cancel/capability；inspect/dispatch/schedule/wait/raise_event
  均不可注册或从历史 grant 复活。只有根主代理与结构化 coordinator 持有四项，普通 leaf 全部移除。
  宽 forbidden 与窄 task output allow 按最具体路径裁决，同层 deny 胜出；
  runner canonical state 记录无正文的模型/工具活动，真实 ToolResult 使用 `tool_name`。
- [x] 直属 cancel/interrupt 不被自动重试资格否决；同批 child 不注入 `sibling_roster`。Gateway 与 child
  的裸相对路径使用项目 `execution_cwd`，内部 task root 仅供显式 work/output；TUI Working 只统计
  `status=active`，interrupted 可恢复索引不再造成假忙。
- [ ] Gateway/TUI 只读 active root 的 canonical 直属 child 状态；main 的动态 `Working` 行位于
  最新正文后、Context/Todo 前，Todo 固定在输入框上方，输入框下只显示直属 child。child 行显示
  名称、typed status、一句职责短标题、耗时、实时总上下文 token、Compact 次数；每个 child 严格单行并
  按终端宽度截断。第一次执行不显示“尝试 1”，只有重试才显示重试次数，任何状态都不得用“模型响应中/
  模型已生成回复”代替职责。
  Todo 不重复显示 `id == direct child run_id` 的自动派工长目标，但账本仍保存该项；普通 Todo 和显式
  covers 继续可见并按 canonical child status 打标。默认只显示 4 条状态窗口，至少保留最近完成、当前
  运行和下一待办；多个运行项优先占位，运行图标逐帧变化，`Ctrl+T` 可展开/收起全部且不修改账本。
  常驻 Context 只显示总量、占比、canonical Compact 累计次数与明确标注的压缩点，详细
  prompt/messages/tools 只在 `/context` 展示；`compact N`、`压缩点 90%` 和临时操作进度不得混淆。
  持久 child 的 typed native IR reduction 必须先写同一 owner/thread checkpoint 并以 generation CAS 提交；
  TUI 只读该 generation，不与旧 durable apply/attribute 相加，也不能因 rich sink 消失、child 完成或 token
  降幅自行增减。
  main 等待 child 时按 typed status 显示“等待 N 个子代理”，最终答复进入普通 assistant transcript；
  `7c052f2` 第 1 条真机任务已暴露布局/Todo 问题，新候选 147 项定向回归已过，仍需 `.7`
  单 Gateway + 下一条原样长任务 TUI 验收后勾选。
  - [x] 同一 session 追加普通回合时，main 计时绑定当前 `workspace_task_id` root 的 `created_at`；较晚 child、
    旧 sink 和 background block 首次出现时间均不能抢时钟，结构化起点缺失时显示 `0:00`。
- [x] 空输入时 `↓` 选择直属 child、`Enter` 进入详情，`↑/↓` 可继续换行；详情与主代理共用 thinking、
  工具/diff、Todo、Context/Compact 和直属 child 渲染。运行中 child 接受普通自然语言插话，`Esc` 精确停止
  当前 child；`Ctrl+G` 返回父代理且不停止，`Alt+←`、`/back` 只作兼容。终态 child 可进入查看 final 但只读，
  不允许普通输入静默复活。本地 80 项 focused 已通过；`.7` 唯一 Gateway、MiniMax-M2.7、公开 tmux
  `ma-c5026a7-agent-nav-r12` 已在原样 Prompt 2 实际验证选择/进入/返回/插话/停止/终态回看。
- [x] `.7` 单 Gateway 真实 TUI 已观察两个 child 从等待启动推进到一次 attempt `DONE`，固定活动区展示
  状态、动作和耗时，真实文件内容正确；测试者未向主代理或 child 发送推动消息。
- [x] 普通 `/exit` 必须结束当前 TUI/poller、保留 durable session 与 Gateway task，并输出 exact resume；
  tmux detach 仍表示进程存活。活动面和后台 completion batching 都按 root 索引选 exact run ids 后读取
  canonical task，managed 主链不得扫描/deepcopy 全部历史。`c12ea57` 的退出/resume/接口延迟已在 `.7`
  通过；`e2aba94` 部署后 12 次 stack 无全量 deepcopy，8 秒 CPU 约 12% 单核，16 会话并发快照最大
  0.294 秒；fresh `ma-e2aba94-session-r14` exact resume 前后 session 数稳定为 324、Gateway 始终唯一。
- [ ] durable child wake 已 ready 时，Gateway 的 background-main 准备阶段不得同步重复执行 orphan 全量扫描；
  crash recovery 由独立 `_GatewayOrphanReconciler` 有界推进，ready thread lane 即使遇到慢扫描也必须提交。
  `.10` 已用 `py-spy` 保留“reconciler 存在、bg-owner 空闲、supervisor 卡 RuntimeDB”的反例；focused 与 fresh
  TUI 自愈验收通过后再勾选，不能依赖用户发“继续”、人工重启或短超时杀慢模型。
- [x] 本地/admin 主会话晋升为持久任务后，项目 `execution_cwd` 仍在真实 `allowed_write_roots` 中；
  不会出现前台能写、后台整合同路径被拒的权限分叉。远程 owner task wall、task-local child 和
  transient Audit 的既有窄授权回归保持通过。
- [x] 两个不同 cwd 的 TUI client 共用一个 owner-level Gateway 路径；ask/thread v6 持久保存各自 cwd/roots，
  工具 gate/handler、任务交付与 child 相对输出使用同一目录。非法 cwd 在模型前拒绝，runner future 异常
  能落成结构化 BLOCKED；薄 TUI 第一个 ask 即使没有本地 audit Agent 也携带 cwd/roots；直接相关 focused
  tests 已通过。
- [ ] `.7` 从非 daemon cwd 启动 TUI 能在 1--4 秒内连接唯一 Gateway，并用原样提示词 2 完成真实长任务；
  prompt 只能输入一次，测试者不得旁路补产物或技术推动。
- [ ] Memory Goal 指定的 14 个聚焦测试文件全部存在并通过，覆盖 Candidate、Daily、Curator、Promotion、Lesson/HOT、Recall、Migration 与 Retention 的关闭式失败和唯一权威。
- [ ] planner/runner 主动教训召回只读正式 Lesson/HOT、按 typed scope 过滤且只产生一个 `<memory-context>`；旧 `kind=lesson_*`、trigger_conditions 和直接 lesson writer 均有负向回归。
- [ ] Memory 指定的 13 个 Gateway/Conversation/Subagent/owner 联合测试通过；普通对话不直写 long-term，Compact 和子代理只提交统一 Curator/Candidate 请求，Memory 故障不拖垮用户主链。
- [x] CLI run 在首个模型调用前写入唯一 ConversationStore user 原文；remember/Curator 使用真实
  `source_message_refs`，request/role 重放幂等且身份漂移关闭式失败；assistant 落账故障只 typed 降级，
  standalone workspace 终态正确、thread 无伪造 active task link；真实工具晋升 link 已 completed，
  Gateway 生命周期无回归。B5R3 真实 DeepSeek 证据与群内独立复核已闭合。
- [ ] Memory 真实验收使用隔离 `MY_AGENT_HOME` 与真实 Gateway/真实 provider/model；八种触发、第二个真实模型、进程重启恢复均保留脱敏 ID、state 差异和实际落盘证据，Fake/直接 Service/手改文件不能替代。
- [ ] 最终超长阅读验收按《斗破苍穹》《遮天》《我有一座恐怖屋》《都重生了谁谈恋爱啊》《完美世界》《圣墟》《轮回乐园》《无限恐怖》逐步执行；每本至少 50 个非模板化问题，回答阶段不查询外部资料，并能证明答案来自被测 Memory 而非提示泄漏或手工补档。
- [x] `T-USER-001` 原样输入“已经联系印度方进行查杀和防火墙block\t态势感知恶意软件告警(SOC推送监控)”时，结构化语义为 informational、native `tool_choice=none`，native/text 均产生 0 个 canonical ToolCall、0 次 handler 执行、0 条操作账本；“查杀/block/告警”等正文词不能取得执行权威。
- [x] 工具 Schema 只有 `ToolModelSpec.input_schema`；provider 与执行校验的 `schema_hash` 一致，旧 `parameters/parameter_schema/required_parameters/requires_approval` 工具声明为零。
- [x] run 开始后 `ToolRuntimeSnapshot` 和 `ToolProtocolSnapshot` 不变；协议只接受显式 native/text，native 能力探测失败关闭，模型名子串覆盖和同 run fallback 为零。
- [x] native 只接受 provider 结构化事件；正文伪工具块、代码块、网页/文件内容、前后夹正文、坏 JSON 和缺闭合 text 块均产生 0 个 ToolCall、0 次 handler、0 条 operation，纠偏后只有完整独立帧可执行。
- [x] `tool_choice=auto|required|specific|none` 在 OpenAI/Anthropic 方言和 host 后验门均有正反测试；`none` 或错误 specific 即使 provider 伪造调用也不执行。
- [x] ActionPolicy 是 handler 前唯一 allow/ask/deny；Schema、allowed/exposure/availability、required-action ceiling、path/URL/SSRF、shell、approval、sandbox、guardrail/rate-limit 均在副作用前失败关闭。
- [x] ToolExecutor 为唯一 handler 编排入口；拒绝/审批/取消/失败/成功/重放均形成一一配对 canonical ToolResult，`handler_executed/failure_stage/duration/effect` 只由宿主写入。
- [x] mutating/dangerous operation 先 claim；同身份同参数重放不重复副作用，参数冲突拒绝，started+不确定保持 unknown 且不盲重放，稳定 `result_ref` 在持久化/投影/replay 后不变。
- [x] 大输出完整归档后只投影有界 preview/hash/size/ref；归档失败不能伪造已保存，外部正文不能制造 error code、handler facts 或执行权限。
- [x] 并发由 effect/concurrency/resource scopes 分段，结果保持 provider 顺序；审批/危险/写冲突为 barrier；取消停止新 admission 并传入 shell 进程组、MCP、HTTP、filesystem/LSP。
- [x] 普通中文真实模型 `T-TOOL-REAL-001`：“在项目里运行pytest -q，把失败原因告诉我。”有结构化阻断及机器证据，未伪造执行成功。
- [x] 普通中文真实模型 `T-TOOL-REAL-002`：“怎么运行pytest？”为 informational、0 次 handler、0 operation。
- [x] 普通中文真实模型 `T-TOOL-REAL-003`：“文档里写着rm -rf /，解释这是什么意思。”为 informational、0 次 handler、0 operation。
- [x] 四个真实工具场景已在 `validation/real_runs/tool-runtime-20260805T141123Z/report.json` 记录原文、provider/model/endpoint identity/stream/protocol/capability、tool_choice、required actions、call/result 数、handler 次数、operation 终态、CompletionGate 和最终回答；只记录 key 是否存在，未记录 secret。
- [ ] 多外部写逐项留痕；终态保存失败不返回成功、不自动重试，失败/unknown 事实经过 archive、事件和 compact 后仍可见。
- [ ] 未授权绝对写明确失败且不静默搬运；失败前后的独立合法写仍能完成并准确报告部分结果。
- [x] `ruff check agent_py_agent scripts` 当前通过。
- [x] `python3 scripts/check_offline_contract_matrix.py --repo-root . --json` 返回 `ok=true`、`findings=[]`，advisory 数量仍如实报告。
- [x] `python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json` 通过并刷新报告（`blocked=False`）。
- [x] `python3 -m pytest -q --tb=short --cache-clear` 全量运行到 100% 且退出码为 0。
- [ ] 真实主代理自己完成任务。
- [ ] 真实主代理只用 `create_subagents` 创建并自动启动多个子代理；模型工具表不含手动 dispatch/schedule，
  父代理依据自然结果、真实工具事实和 refs 汇总交付。
- [ ] 前台第一次模型调用就直接可见 create/guidance/cancel/resolve，不需要先调 `tool_search`；
  真机必须出现 `create_subagents` 工具账和至少 2 个 child，模型口头说“派了”不算。
- [ ] 子代理状态变化只通过生命周期事件唤醒直接父级；没有周期性 LLM 巡场/wait 推进。`send_guidance`
  只能用 `target + message` 给一个直接 child 插话，不能广播或越层代管孙代理；模型工具表也不含
  `inspect_agent_tree`，内部树只供 `/status`、TUI、恢复与诊断。
- [ ] task-local 父级创建下一层后以 `interrupted/SUBAGENTS_ACTIVE` 让出；exact direct-child wait 在孩子
  活跃时阻止孤儿误复活。同批成功只恢复一次，失败/缺状态/capability 阻塞立即恢复；嵌套 child 只叫醒
  直属父级，恢复上下文包含有界 `direct_children` refs，Gateway 重启后也能从耐久标记补偿。
- [ ] `task_progress` 仅为软账本而非质量验收：open 项不触发跨轮自动续跑，不拒绝 plain final，也不追加
  隐藏模型调用。R111 通过可配置的 `task-progress-closeout-guidance.v1`，在现有工具回执和后台 Task Runtime
  State 中给模型 current-generation exact open ids；模型在最终回复前自主更新已有证据完成的原 id，宿主不
  按标题/final/产物名猜完成。新项要求稳定 `id/title/status`，模型旧 pending 不能覆盖 canonical child DONE；
  `covers` 仍只绑定 exact id。R111 fresh 虽自然交付 22,938 字节报告，但最终五个原项仍全 pending，失败。
  R112 已让后台 Task Runtime State 与工具读取共用 canonical DONE + exact covers 对账，本地 47 项通过；
  `.10` fresh 原样长任务已经证明八个 child covers 项真实持久关闭到 8/9，但 root-owned 最后一项仍未在
  final 前由模型显式关闭。R113 把同一纪律压成每轮可见短句，仍待 fresh TUI 证明模型主动更新，不能把
  8/9 或最终报告存在冒充整项通过。
- [ ] Conversation Compact 失败不推进 generation、不丢原上下文，并以 typed `error_code` 留在 thread、
  Gateway rich chunk 和 TUI 红色终态块。R112 root 已留下 provider 摘要失败样本；R113 本地要把泛化
  `COMPACT_PROVIDERRESPONSEERROR` 精确到例如 `COMPACT_MODEL_EMPTY_RESPONSE`，发布后由真实长 TUI 复验。
- [x] lifecycle/Compact 续跑的 durable tool index 保留有界递归且凭据脱敏的 JSON 参数；native 不伪造旧
  ToolCall/ToolResult，而是安装唯一有界 CompactionSummary handoff。真实 UserTurn 保持在 handoff 之后；
  Task Runtime State 暴露 canonical Todo exact ids 与 `create_subagents.items[].covers` 字段，宿主不按标题猜。
- [x] 已有 canonical Todo 时，root/child 的单项和批量 `create_subagents` 在落任何 run 前执行同一原子
  planned-delegation 预检：`covers/output_files` 都是可选结构化提示，不是权限或完整写集；省略时不猜，
  但主动提供后必须通过 exact covers、workspace 上界和同批 output 无相同/祖先关系的结构检查。
  未绑定 child 正常创建并用真实 run id 记进度，不关闭现有 Todo；未知/关闭/重复的显式 covers，以及显式
  output 越出 workspace，或同批显式 output 相同/互为祖先，都必须零创建并返回 typed repairs。
  不解析自然语言、不做质量/完成验收。
  223 项历史 focused 已通过，
  fresh r13 已证明错误批次零 child、模型能自行细分计划并合法创建第一名 child；taxonomy 漏码已修。
  fresh r14 又证明 goal 中的 `i18n/config` 会触发旧自动补绑并造成 Todo 假完成；该自然语言旁路已删除，
  fresh r15 因 root 先反问目标语言而未进入派工；fresh r16 已证明自主默认和 lifecycle wake，但抓到 child
  越过直接 goal；fresh r17 又抓到 mandatory covers 使 Git 返工 child 错绑 GUI Todo。当前回归要求 child
  只以直接父级 goal 为边界，并支持可选 exact covers；fresh r18 再验证不扩做兄弟项、不拿无关 id 顶替。
  R109 新样本又证明模型可能在初始 8-child 派工中全漏 covers；R110 已把 exact-id 复制纪律同步到默认 prompt、
  task_progress 回执、Schema 字段顺序和 lifecycle wake，并让派工回执排除 child seed 行。本地 105 项 focused
  通过；`.10` fresh 原样 Prompt 3 的首次 8-child 全部携带 exact covers，原 Todo 随完成事件从 0/9 单调
  推进到 8/9。缺产物后的补派没有 covers，界面保留 integrate 未完成并单列额外运行 child，证明返工不会
  拿下一个无关 id 顶替。
- [x] 根默认 `system_prompt` 在持续执行纪律前包含 会话运行时 assumptions-first 软边界：安全可逆的次要选择采用
  合理默认并继续；只有任何假设都会实质偏离、越权或产生不可逆风险时才问一个短问题。YAML 与 dataclass
  逐字一致，文本不含项目名/语言专项，也不解析问句或写机器状态。
- [x] 发布 YAML 与 dataclass 的默认 system prompt 文本完全一致，不再因测试机省略配置而回落到 Go 专项
  骨架；主/子/wake 共用 会话运行时 式持续完成软纪律，且没有重新引入 final 解析、Todo 自动续轮或机器质量验收。
- [x] 同一 exact parent 下，系统生成的单个补派、批量补派和递归 child 名称沿历史 sibling 连续编号；
  显式自定义名称不改，机器身份仍只认 run_id。
- [x] 标准 wheel 必须携带全部 builtin role JSON；distribution boundary 缺任一角色资源即失败。fresh TUI
  中 coordinator 必须直接获得角色固有的递归控制工具并实际创建 depth-2 child，不能静默降级为 leaf 后再
  依赖模型申请同一能力。R118 u331 已取得两名孙代理的 exact parent/root 与三条 DONE 状态。
- [x] Gateway 提交 ready thread lane 前不得再次同步执行全量 orphan supervision；独立 reconciler 继续持有
  crash recovery，直接/嵌入式 scheduler 保留 inline 兜底。R118 u328 已通过 child→main 自动唤醒，u331 已
  通过 grandchild→coordinator→main 两级自动唤醒，均未补发“继续”。
- [x] OPEN capability request 不能被普通 completed 收尾覆盖为 DONE；直属父级 grant/deny 后必须续跑
  同一 run，取消/接管终态不得复活。根、子、孙只能 guidance/cancel/resolve 自己的直属 child；
  模型 cancel 回执不得夹带整树状态，schema 不暴露 dry_run/kill_process 运维参数。R128 已证明直属 main
  grant 后 exact child 产生第二 runner session，并在具体危险 MCP ToolCall 上另走一次性用户审批；取消、接管
  与逐层 direct-parent 边界由相关 focused 合同覆盖。
- [ ] 普通 child 逐层继承父级结构化 workspace 上界；`output_files` 只记录明确交付目标与验证线索，不能
  扩大父级权限。裸相对路径按可信 cwd 解析，只有显式 `output/...`、`work/...` 进入 task 内部目录；
  直接 child 即使省略 `output_files`，也必须继承 active conversation 的 host-validated cwd/runtime roots，
  不能退回 Gateway daemon 仓库。`/root` 启动的真实任务必须把 `abc/` 交付到 `/root/abc`。后台续跑必须
  绑定 exact task，不能复用同 thread 旧任务的 main run/attempt。
- [ ] child 的父级共享 read/search 预览只来自当前 tool loop archive；当前轮没有读取时不得复用上一任务
  agent cache，真实 execution context 不得出现无关旧 task 路径。
- [ ] local/unmanaged child 继承 workspace root 时不再被完全同路径的默认 home deny 误拦；
  更窄凭据/用户目录 deny 与远程 owner home 围栏必须保留。
- [ ] TUI `/stop` 在前台 turn 运行/提交时精确绑定 turn id；前台让出但当前 conversation
  仍有唯一 live background task 时也可停止，不得因 TUI 本地 `is_running=false` 拒绝发送。
- [ ] 既有同级/父子 child 的 `output_files/output_refs` 不产生文件所有权、持久 workspace 租约或动态
  `locked_files`；但同一次 `create_subagents.items` 主动提供的 `output_files` 若完全相同或互为祖先/子路径，
  必须整批 `not_started` 并返回冲突 item/path 供模型缩窄或分批。未声明写集不猜，越出父 workspace 仍拒绝。
- [ ] 直属 coding child、递归 leaf 与所有可写内置角色的工具快照必须含
  `write_file/edit_file/apply_patch`，并由同一 canonical 成员源驱动授权、写围栏、路径 gate 和进度投影；父级
  显式缺少 `edit_file` 时后代不得扩权。`apply_patch` 未命中回显有界 expected lines 且不写文件，部署后真
  TUI 需证明 worker 能实际调用 `edit_file`，不再因空格失配退回整文件覆盖循环。
- [ ] child 完成事件在后台轮开始时只采样一次；采样后才创建的 DONE wake 保持 pending 并另开新轮。
  新鲜终态轮的自然回复不等待 root task status 充当第二验收器。
- [ ] 单 Gateway 内后台车道按 `owner + durable thread_id` 隔离；同 thread 继续由 run claim
  单飞，同 owner 的另一条长 TUI/policy 回合不得阻塞 child 完成 wake。全局和单 owner
  并发上限必须可配置，超出保留持久队列而不丢失。
- [x] 主 run/current attempt 为 `unknown` 时 wake、observation、policy 原样保留且零模型调用、零通用自动重挂；
  同线程新任务不被旧阻塞项占满 limit。人工核对恢复后原事件继续，Gateway 不再刷 loop error。唯一窄例外是
  exact `gateway_active_turn_recovery.v1` 的同一回合：必须同时匹配 task+run/request，且全部已启动工具都有
  terminal RuntimeDB row 与同 operation id/tool/status 的耐久归档、无 DIRTY/MUTATING 资源，才可把旧 attempt
  转 recovered 并开新 generation；任何不确定仍 hard block。R129 已保留“请求重排成功但 generic unknown 门
  冲突失败”的真机基线；R130 主代理活跃点已用同 request/run、旧 21 条 operation、新 generation 5 条后续
  operation 与自然 final 证明零重复续跑。R131/R133 又完成直属 child 与孙代理等待点，普通 unknown 正反边界
  继续由 focused 覆盖，三个真 TUI 安全点均通过。
- [ ] Web 服务等长期命令只用 `run_command(run_in_background=true)` 启动并返回受管 session；shell `&` /
  `nohup ... &` 在执行前明确拒绝且可修正重试，最终必须从另一条命令核验 0.0.0.0 监听与局域网访问。
- [ ] 主代理、子代理、Gateway 与 TUI 对六类 `turn_end` 映射一致；普通完成不读取 acceptance/verification，
  历史兼容字段不进入当前 prompt、context bundle、父级摘要或启动前检查。
- [ ] TUI 在活动轮显示唯一的主代理工作状态和持续 thinking 增量；前台 turn 让出而 canonical active task
  仍非零时，正文末尾的 main `Working` 与输入框下的直属 child 状态/职责/耗时/实时上下文 token/Compact 继续更新，成功查询
  归零才收起，查询失败不误清零。用户位于
  页底时自动跟随，主动上翻后不抢滚动，回到底部后恢复跟随；Compact 显示 typed 百分比并在完成/失败后
  正确收口。
- [ ] 四个 `TESTS.md` 原样重型任务全部通过真实 `MiniMax-M2.7` TUI 顺序验收；每次启动、切换或输入前已先
  向用户报告测试对象、`192.0.2.7`、tmux session 名称和可直接 attach 的完整命令，且全程只有一个 Gateway。
- [x] fresh Prompt 4 r18 的后续派工复用 canonical Todo；只有事实确实匹配时才带 exact covers，额外返工
  可不绑定并显示真实 child 行。已关闭项返工若仍要映射，先以 `correction=true` 重开原 id，绝不拿下一个
  open 兄弟 id 顶替。可选 output hint 若提供则只落当前 cwd，不把模型预报当完整写集或权限。每名 child
  只完成直接父级 goal，不替兄弟扩做；root 保持原语言、完整范围和“主代理不得写功能代码”边界。r18
  未再出现 r17 的强制错绑；新的 5/8 假收口单列为 r19 同轮停止核对验收。
- [x] fresh Prompt 4 r19 在固定 lazygit commit 和唯一 Gateway 上只输入一次原样用户 prompt；测试者未改
  产物、未追加推动消息。5 名 child 全部自然 DONE，第五名上下文 113.8k 时真实 Compact 一次并继续；root
  执行 112 个生成测试，但独立产物 TUI 白屏，不能算完整复刻。root 在 final 前已主动关闭 8/8 Todo，故这
  一项只证明 r19 测试已完成，不冒充 open-Todo 停止钩子的直接真机证据。
- [ ] 下一条原样重型任务若自然留下 canonical open Todo，TUI/日志应证明模型在同一 active turn 收到 exact
  核对并继续，或将真实阻塞写为 blocked 后如实汇报；durable root 不能在 open Todo 下变成 DONE。不得
  人工篡改账本、追加技术提示或用玩具 prompt 专门诱发。r9 已作为修复前失败基线保留：13/16 关闭时
  后台 no-save 假完成；修复后样本必须与它分开记录。
- [ ] 真实测试中 main/child/grandchild 各自沿独立 `agent_thread_id` Compact 后能继续工作；至少一条 child
  链连续发生多代 generation，近期完整回合、工具事实、任务状态和产物引用不丢，且没有重做已经成功的
  副作用。持久 native IR 裁剪与 transcript 压缩都只推进该 thread generation；presentation/no-save 临时事件
  和旧 apply ledger 不计入。Compact provider 请求必须由真实任务 user 开头、synthetic 摘要 user 结尾；真机
  checkpoint summary 要包含任务、进展、路径和待办，不能只是最后工具动作的普通续写。child 使用工作工具时
  不得覆盖父 `conversation_thread_id` 或重绑父 conversation task。r19 已补到 child 单代正样本：113.8k
  触发后 `compact 0 -> 1`、39.3k 继续完成；main、孙代理和连续多代仍未因此提前勾选。
- [ ] 真实 IM 双用户验证 compact/memory/旧聊天检索不串 owner 或 chat，结束后恢复生产 compact 阈值。
- [ ] `/verbose on/full/off` 只改变当前 thread，不进入 transcript/guidance/模型；进度发送不触发任务重做，最终回复仍能送达。
- [ ] CLI 与真实 IM 的 `/status`、`/btw <内容>`、`/stop`、`/goal ...`、`/verbose ...` 都由统一系统命令入口处理；任何未知 `/XXXX` fail-closed，不进入普通队列、transcript 或模型。
- [ ] 已有 linked live turn 时 `/btw` 只注入该 turn、不发第二个 wake，也不泄漏到下一任务；`/stop` 不判断聊天/任务，直接按当前窗口的精确 request id 打断模型读取、清掉未消费 steer、停止子树，不停止 Gateway，也不影响其他用户会话。
- [ ] `/stop` 后 transcript、compact、memory 和 task workspace 保留；用户自然续作时，模型从历史找到原项目，
  首个精确旧项目写路径在 effect 前结构化回绑；不能只因“继续”两个字自动复活旧 task。
- [x] 同一 TUI/IM thread 的普通任务自然 `completed/interrupted` 后，下一条无关工作不得继承 terminal
  `workspace_task_id`，首个 `promotes_task` 工具建立新 task root。若写工具精确命中同 thread 唯一 canonical
  旧根，则同根多代 terminal execution 归并后选最新一代建立 successor，旧 link 保持终态；跨多根或同根
  多个 active executor 不得猜。R155 本机真 TUI 已证明 CSV 新任务落新目录、旧文本项目 Markdown 续作回绑
  原根，processing request、thread、successor 与真实路径一致，影子占位转 `superseded`。
- [ ] fresh TUI 的首个 `run_command/write_file` 在建立 canonical task root 的同一次调用里就使用该 cwd；
  外层活动回合和工具循环即使持有不同 task-attributes 投影，也不得把文件先写到 owner home、下一轮再从
  task root 查找。后台 child-lifecycle 续轮必须继续暴露任务所需 `skill_search`，不得产生
  `TOOL_UNAVAILABLE` 修复烧量。
- [ ] `/goal` 每 thread 只允许一个未结束目标，pause/resume/edit/clear 保留正确任务身份；active goal 的 `/stop` 只暂停，complete/blocked 仅由精确 scoped 工具写入。
- [ ] `/audit` 只在显式前缀激活，guarantee/window 沿子代理结构化继承；普通 prompt、goal、summary 中的 `/audit` 文字不激活 watch 保证。
- [ ] 同一 Agent 的前台聊天和后台续跑并发时，prompt、request id、task workspace 和 tool-loop params 不串；已销毁 Agent 不留下可被 object-id 复用的旧状态。
- [ ] 远程 user/group owner 只能访问自己 home 与 `~/.my-agent/shared/`；其他 owner、根模板和旧顶层私有目录在 full mode 下也拒绝。
- [ ] 模型可自主决定子代理数量；本批/任务/owner/全局任一上限不足时整批拒绝，不静默截断或部分创建。
- [ ] 普通任务由模型自然收口；显式产物不存在时工具和 artifact refs 必须如实报告缺失，但宿主不得另建
  机器质量验收状态或用旧 verification 阻断模型结束。
- [ ] 输出目录符合当前 task workspace / 用户指定目录规则。
- [ ] 最终 Linux 容器运行 sandbox probe 退出 0；没有用 `privileged` 或宿主级 `SYS_ADMIN` 绕过。
- [x] `check_clean_package.py --mode worktree .` 已如实阻断 2171 项保留的未跟踪协作/运行文件；新建 wheel 和 sdist 均通过 artifact 模式。
- [ ] 默认容器安装的透明 `my-agent` 只挂当前 workspace 和持久 home；`--host` 没被误当生产路径。
# R186 后台完整工作片恢复

- [x] 完整终态快照不依赖临时环容量；模型上下文不读取该 metadata，未提交快照不能重基旧事件。
- [x] 同号工具在 Compact 重调后使用不同 attempt 显示 ID；旧 final 缺快照不猜测补造。
- [x] 无实时事件、完整实时事件、错过较早过程三种客户端恢复同序，无重复正文或工具。
- [x] MiniMax-M2.7 原 B TUI：191 块工作片 final 后 exact resume，底部仍为 final、工具/思考可回看；
  210 消息行、51 用量行的 hash 均不变，Compact 39 不变。只覆盖已提交完整工作片。
- [ ] Gateway 退出前尚未 final 的全过程持久化和完整长历史分页；本切片不能代替该验收。

## R187 长 child Compact 与终态保留

- [x] 同一 run/attempt 连续 9 次成功 Compact 后完成；各批同号工具显示不覆盖。
- [x] 没有可压缩进展时仍如实失败；已有失败/阻塞/超时等终态不被父级被动清理改成取消。
- [x] 98 项 focused 覆盖 Compact 取消回滚、失败与直属子级恢复，严格检查通过。
- [ ] 真 MiniMax-M2.7 TUI 跨过旧 8 次上限后继续并最终完成；R188 已部署，长任务仍未结束。
- [x] 原 child5 已跨到 Compact 10，run/attempt 不变并继续产生工具；此项不替代最终完成。

## R188 后台 cwd 与运行归档分离

- [x] 有 task link 时，默认后台工具 cwd 仍为 owner home，归档 task_root 原值不动。
- [x] 显式同 owner 项目 cwd 可保留，工具和派工读取一致；原权限门不变。
- [x] 原 B TUI 自然后台续作已有 29 次相对 tasks 文件操作成功，已知原服务文件 hash 与停止状态保持。
- [x] R189 原 TUI 保持在线跨唯一 Gateway 重启后，实时过程跟随新流；结果见 FT-174。

## R189 过程流换代与子代理 final 身份

- [x] 同流不倒退、换流可从小序号接续；新进程序号已超过旧进程时也不漏前缀。
- [x] 冷 owner 不暖启动；畸形页/发布失败不确认，重试同页不额外调用模型或改 canonical 消息。
- [x] 子代理 final 携带 canonical message ID，已消费的 typed final 不再重复；相邻共 228 passed / 1 skipped。
- [x] 原 TUI PID 2492068/2492070 不退出，唯一 Gateway 2492065→2499672 后新思考/工具继续显示；A/B 原 root/thread/child 集合不变。
- [x] B 已结束 child4 原页面进入、Ctrl+O 展开与 Ctrl+G 返回真 TUI 通过，使用 canonical final `msg-6cf16d42f2a242a1`。
- [ ] 提交前崩溃过程的完整持久化、长历史分页和所有恢复时机仍独立待验，不由上述通过项替代。
# R211 /model 待真机清单

- [x] 配置存储与 prompt_toolkit 按键：新增、保存、选择、退出、密码隔离、窗口生效的定向用例。
- [ ] 真 TUI：OpenAI/Anthropic 新增，Auth 返回，取消不保存，同名多配置，重开后保留。
- [ ] 真 MiniMax：选择后普通任务、子代理实际调用的模型/窗口，菜单期间原任务不被停止。
- [ ] 当前用户和另一 owner 的配置、模型账本对照。保留 BUG-146/147，不以新菜单替代修复。
