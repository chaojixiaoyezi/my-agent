# 当前产品事实

更新时间：2026-08-25（`my-agent` 测试机）。本文是当前工作树能力状态的唯一权威页；README、路线图和历史审计
只能引用这里，不能把“代码存在”“测试存在”或“设计完成”写成已经稳定可用。

## 状态定义

- **稳定**：在明确支持范围内有正式入口、默认配置、失败语义和持续门禁，可作为当前产品承诺。
- **部分可用**：主链已存在，但平台、部署、规模或真实场景验收仍有明确缺口。
- **实验性**：有代码或专项验证，不是默认生产入口，配置、兼容性或长期运维契约仍可能变化。
- **仅设计**：只有方案、接口预留或文档，不应向用户宣称可用。

状态只描述当前工作树。它不等同于已发布版本；未提交、未推送的能力不属于远程 `main`。

## 2026-08-25 跨回合工具终态折叠与缓存稳定前缀

- **状态：部分可用（`7b14e34` 已部署真机；手动 Compact 状态刷新二层候选尚未发布）**。普通 main/child 工具回合结束时，从 canonical
  archive 一次生成有界、脱敏、确定性的 `conversation_terminal_tool_fold.v1`，与 assistant 正文同一条
  ConversationStore 消息落账。用户可见正文不拼接折叠；下一模型轮才从 metadata 读取。
- 该折叠是 会话运行时 完整 ResponseItem 历史与 终端交互 cache-aware microcompact 之间的适配：完整工具输出继续
  由 owner archive 掌权，prompt 只带工具顺序、状态、有限参数结构、近期摘要和 refs。已结束旧回合不重新
  总结，历史保持 append-only 稳定前缀，供兼容 provider 自动复用 cache-read。
- 普通折叠不推进 Compact generation，也不重复累计运行中 native IR 已真压掉的 tool pairs。`/context` 单列
  当前未压缩尾部的折叠回合/工具调用数；真正 Conversation Compact 才把折叠吸收进 summary 并推进 generation。
- 同一 request/run 经 provider overflow 做 Compact 后继续时，内存 `ModelCallLedger` 的累计快照由会话存储按
  `physical_model_attempt_count` 游标换成 append-only 增量；事件保留原快照指纹以幂等重放，供应商 input/output/
  cache-read/cache-write 和 estimated 分区均只累计新增值。该修复避免第二次成功回复因 scope-only event id
  冲突而反向把 child 标成失败。
- `.7` 原长 TUI 已证明：前一轮 2 次 Read 能在下一轮无工具调用时准确续接，两轮 provider cache-read 分别为
  42,107 与 12,987；手动 Compact generation 1 把 45,639 降到 15,029 并吸收 98 条消息。缓存策略不是每轮
  重写旧摘要：普通 fold 保持旧历史 exact 稳定前缀，真正 Compact 才一次替换为新摘要前缀。
- 当前工作树的二层候选让手动 Compact 通过 typed `task_status.compact_generation` 立即更新 TUI，并清掉已经
  失效的压缩前 Context 数字；下一次真实模型调用再发布新 provider-visible snapshot。它不解析成功文案，
  不为界面刷新额外调用模型，也不新增一套 provider cache key。
- `f901645` 真机首次复验又证明 Gateway activity 已返回 generation 1，但无 Working block 的客户端完成事件
  丢掉了水合值。当前工作树继续让 idle/resume frame 更新 status，并在 controller/reducer 两层保持代数单调；
  服务端 ConversationThread 仍是唯一权威，客户端只保留该 session 已观察到的最大 generation。

## 2026-08-25 长 session 当前回合 Todo 与滚动锚点

- **状态：稳定（focused、严格 gate 与 `.7` 单 Gateway 原长 session 通过）**。完整 task-path
  `task_progress.v1` 继续保存跨阶段历史；底部 Todo 只按宿主的 exact
  `display_plan(generation_id/revision/item_ids)` 展示当前 ordinary user turn。lifecycle continuation 继承同代，
  新普通用户回合换代，迟到 poll/tool/final notice 不能把旧清单刷回来。
- 主/子页面各自保存 typed scroll anchor，并由 prompt_toolkit `Window.get_vertical_scroll` 落到真实 viewport。
  手动离底不被后台输出强拉；有效消息提交后立即回底。`4425618` 的 362 项 focused（2 xfail）与严格 gate
  通过，`.7` 的原 `ma-97468f3-longchain-r27` 两轮追加、PageUp/Enter 和终态 Working 已真机验证。
- Context 常驻行显示当前 provider-visible 估算与配置压缩点；`compact N` 只读成功提交的 canonical thread
  generation。128k、90% 配置下，46%–53% 显示 `compact 0` 是正确事实，不按屏幕历史长度推断压缩。

## 2026-08-22 子代理生命周期 active-turn 续接

- **状态：实验性（focused 回归通过，尚待 r11 真 TUI）**。child lifecycle wake 现在从 exact
  Conversation task link 恢复原始 objective/task path；原始 objective 保持在 `User Task` 与
  `root_user_prompt`，synthetic wake 只作为 runtime continuation。detached Audit 配额通知保持独立提示。
- 同一 root task 的 canonical tool-output index 按 exact `run_id + task_id` 恢复成功/失败调用、参数、refs、
  one-shot 去重和执行轮基线；child workspace 与其它 task 不参加。恢复历史后仍保留配置规定的每工作片
  新增工具额度。该投影是结构化续接事实，不从任务正文判断语言、质量或完成。
- Prompt 4 r10 的现场已证明原文仍在 task link/transcript，但旧后台轮会把 Rust 改成 Go；当前工作树已
  对照 会话运行时 child notification 与同一 session history 修复，尚未推送/部署，因此不能宣称远程稳定。

## 2026-08-22 Delegated Agent Conversation Compact

- **状态：实验性（统一账本已部署并真机触发，摘要顺序修复候选尚待部署）**。main、child、grandchild 各自拥有独立
  ConversationThread；child 创建/恢复、逐 attempt transcript、轮前 transcript Compact，以及运行中 native IR
  阈值/provider overflow 都接入同一 owner/thread checkpoint/generation CAS。后两者由
  `conversation/live_tool_compact.py` 适配，不建立第二条状态机。
- task-local 继续隔离长期 Memory、工具权限与工作区；父子 lineage 只由 run/thread metadata 表达，正文
  不共享。正式 child runner 不再生成旧 `compact_applies/continue`，TUI/Web/SQLite 的 child 次数只读取
  exact `agent_thread_id` 的 generation。允许持久化的 native IR 裁剪先写 `source_kind=live_tool_ir` checkpoint
  并推进该 generation；presentation/no-save 临时事件不属于 durable Compact。
- fake/replay 已覆盖阈值触发、连续 generation、provider forced、摘要/CAS 失败回滚、child/grandchild 隔离、
  旧计数不回读和 owner Memory 不污染。`680e209` 在 MiniMax-M2.7、单 Gateway、大型 lazygit 项目真测中已
  证明 119,295→36,586、generation 1 与 44/9 tool pairs 落账；但摘要 prompt 位于 history 最前导致模型只
  普通续写末尾动作。当前工作树对齐 会话运行时，把 Compact 指令追加为最后 user 消息；重新部署与多代真测前
  仍不能承诺数小时连续 Compact、崩溃恢复或完整项目交付稳定。

## 2026-08-18 终端交互 风格 TUI

- **状态：稳定（当前工作树与 `.13` 测试部署）**。Python/prompt_toolkit 单一 typed TUI 已覆盖消息块、
  Markdown/code/diff、thinking/tool/permission、输入/history/search/paste/completion/queue、scroll/transcript/
  mouse/resize、interrupt/exit 和 canonical session resume；终端交互 独有能力只做显式映射或不适用。
- 85 项行为矩阵全部关闭：38 `VERIFIED`、38 `MAPPED_VERIFIED`、9 `NOT_APPLICABLE`；10k 回合/20k block
  压测和 80/120/140 列回归通过。测试详情见 `docs/design/TUI_终端交互_PARITY_MATRIX.md`。
- `/context` 是自动 compact 同口径只读视图；自动 compact 按配置的 90% 窗口阈值运行，手动
  `/compact [instructions]` 只在空闲会话沿同一 checkpoint/generation 主链执行。`/effort` 可查询能力，
  但当前 MiniMax-M2.7 接口没有可调档位，设置会显式失败且不改变模型参数。
- 模型可见工具按每轮 runtime snapshot 的真实 availability 生成；本地 transcript 没有主动外部通道时
  不再展示 `send_message`，有结构化 provider/target/root/proactive capability 的 owner 才展示。该结论已由
  `.13` 同一普通中文问候修复前后 A/B 复验。
- 测试部署仅为 `192.0.2.13:/root/my-agent`；Gateway 为 MiniMax-M2.7、8420、队列 0/0，TUI 留在
  tmux `my-agent-tui:work`。最终部署/回滚/ANSI/secret 扫描证据位于
  `/root/tui-parity-evidence/final-20260818T071817CST/final-deploy/`。
- 该状态不适用于 `192.0.2.10`、青禾的 `my_agent` 或其它 checkout，也不代表远程 `main` 已发布。

## 1.10 测试部署不变量

- `192.0.2.10` 只保留正式 `my-agent-gateway.service`（`127.0.0.1:8420`）与正式
  `my-agent-feishu.service`（飞书长连接）两项服务。
- 所有 1.10 真机测试都沿这套正式 Gateway/飞书通道执行；不再启动隔离 Gateway、额外飞书适配器或
  `8421`–`8423` 等测试旁路。模型后端可以在同一正式服务下按测试需要切换，这不产生第二套运行时。
- 每次部署与真测都要核对服务清单、监听端口、进程和 `NRestarts`；发现额外实例时先停用并清除，再开始
  测试。该规则只约束 1.10，不授权触碰其他机器。

## 2026-08-03 Audit 窗口即时收口候选

- 真实飞书短窗口曾出现“最终补拉已经完成、原始记录已经全部签收，但逻辑来源工作者仍保持
  `PENDING`，直到后续 `/status` 或周期巡检才收口”的状态延迟。根因不是来源拉取或模型判读慢，
  而是最终边界事务没有向现有统一任务终态投影发送事件。
- 当前候选在 `window_finalized_at` 与最终游标、spool 同一事务提交成功后触发一次结构化完成事件，
  并复用周期监督器原有的唯一 `window + ACK ledger` 终态判断。还有未签收积压或新鲜 RUNNING
  attempt 时继续保持运行；回调异常时仍由周期监督器恢复，没有新增 Audit 状态机或模型 Prompt。
- 聚焦回归覆盖最终事件一次触发、普通空到非空唤醒、来源工作者直接收口和周期巡检兜底，相关四组
  测试文件全部通过。wheel SHA-256
  `ab05d5c54dc5ca3ec0a9095a48d3b6ffaf221a9b25bcd9e06d73101e4636117c` 已部署到 1.10 唯一正式
  Gateway/飞书服务；真实飞书 `飞书甲` 两分钟窗口在 `1785770678.017216` 写入最终边界，并于
  `1785770678.0513902` 自动投影为 `DONE + VERIFIED`，相差约 0.034 秒。测试期间没有发送
  `/status`，两项服务均为 active、`NRestarts=0`。
- 这只是当前未提交生产候选的一项实机证据；它不替代 5 路 30 分钟、故障矩阵、双真实用户飞书、
  候选冻结和最终真实生产数据验收。

## 2026-07-30 Audit 三阈值批处理与五路 10 分钟验证候选

- `/audit` 复用现有 durable spool、通用 long-running child 和结构化模型入口。每路判读由最长等待
  时间、累计完整记录数、累计数据 token 三者谁先到谁触发：高频短日志主要按条数成批，低频长日志
  主要按数据量成批，稀疏流最终由等待时间触发。程序不根据日志正文猜路由。
- 正常日志完整进入模型；单条可以超过常规 32K 批次目标。只有单条加上稳定 system prompt、请求
  信封和输出余量后仍超过 provider-safe 输入预算，才给模型明确标记的头尾视图；完整原文、SHA-256、
  `ack_id`、`source_ref` 和逐条 verdict/score 仍在 owner/task-scoped 本地账中可复查。
- spool 在完整落盘记录边界组批。已有记录时，下一份若会把 48 条额度推成 49 条，就留到下一批；
  第一份自身超过额度仍整份交付。这避免截断，也避免内部边界额外制造一次只判 1 条的模型调用。
- HTTP cursor 拉取与模型判读批次分层：前者若收到 typed `ARTIFACT_TOO_LARGE`，会在同一 cursor
  把完整记录页数减半并保存有效页长，最低为 1；只有 `limit=1` 的单条响应仍超过传输安全上限才
  明确失败。缩页不截正文、不推进失败页游标，也不读取内容决定大小。
- 1.10 唯一正式 Gateway/CLI 上用 `MiniMax-M2.7` 完成 auth/web/net/proc/dns 五路 10 分钟验证：
  每路 1 秒 1 条、各 600 条，合计 3,000/3,000 全部有合法 score/verdict；seq、游标、原文 hash、
  source_ref、20 条隐藏答案 hash 全部对齐，重复 ack 和未映射结论均为 0。证据保存在未跟踪运行目录
  `data/audit-r14-10m-20260730/`，不会进入发布包。
- 模型质量未达到生产承诺：20 条隐藏成功样本命中 14 条，召回 70%；48 条 hit 中 34 条误报，
  精确率约 29.17%。web/proc 为 4/4 且零误报，net 2/4，auth 3/4 且 7 误报，dns 1/4 且 27 误报。
  最慢一路在输入结束后又排空约 25.2 分钟。当前状态只能表述为“完整落盘与逐条结论机制通过，
  MiniMax 在 auth/dns 的准确率及总体排空延迟未通过”。
- 同时聊天复现过一次普通算术请求产生两份模型正文：sticky workspace 错把后台 Audit child 归给
  当前聊天。首个候选 wheel 部署后，Audit child 活跃时同一 CLI thread 的普通算术请求 3.60 秒返回，
  工具轮 0、`logical_model_turn_count=1`，只生成一份正文。该 smoke 随即暴露并推动修复上面的 HTTP
  长页问题，因此仍需以重建后的最终 wheel 再跑数据 smoke。另有 child 目标要求写报告但权限没有
  `write_file`、后台主代理轮询偏重两项通用缺口，均未用 Audit 专用模板、权限放宽或关键词补丁遮掩。
- 当前仍是未提交、未部署候选；完整本地门禁、最终干净 wheel、1.10 精确部署和部署后 smoke 完成前，
  不属于远程 `main` 或正式 1.10 能力。

## 2026-07-30 Feishu 私聊解锁原消息自动续送候选

- 私聊闲置锁默认阈值由 1 小时统一调整为 3 小时（`10800` 秒）；运行常量、`AgentConfig`、随包 YAML、
  CLI adapter fallback 和前端配置目录使用同一默认事实。
- 已设置密码的用户触发闲置锁时，原始 `IncomingMessage` 不再直接丢弃。Feishu adapter 按用户把原
  `message_id`、conversation、正文、时间和 metadata 放入有界 FIFO；密码卡验证成功后异步沿原
  `_dispatch -> ChannelManager -> Gateway /ask` 唯一入口按序续送。它不创建第二个 conversation、
  task 或 transcript，也不解析正文判断是否续送。
- 通道运行时 当前 follow-up queue 的 `message_id` 去重、20 条默认有界队列和 10,000 条近期身份缓存，
  以及 长期助手 当前 busy-session FIFO drain/防覆盖模式用于约束本实现。重复卡片回调、同一平台消息
  重投、错误密码和其他 operator 点击均不会重复续送；解锁 drain 期间新到消息继续排在原消息后。
- 待续送队列当前属于 Feishu adapter 进程内的短期状态：正常解锁会自动续送；若服务在锁定与解锁之间
  重启，尚未续送的原消息仍会丢失并需要用户重发。该跨重启缺口不得冒充已经解决。
- 本地 94 项 Feishu/session-lock/config 聚焦回归通过，覆盖原消息一次续送、错误密码不续送、重复卡片
  回调、迟到重投、多消息 FIFO、drain 中新消息排序、用户隔离和容量上限；Ruff、doc-sync、strict
  code-size、compileall 与 diff 检查通过。当前仍是未提交、未部署候选。

## 2026-07-30 双真实飞书用户长任务与后台续轮目标收敛发布

- 在 1.10 唯一正式 Gateway/Feishu、同一 `MiniMax-M2.7` 上，两个真实飞书私聊 owner 使用各自既有
  conversation/thread 并发执行不同长任务。A（`ou_1be7…f921`）做 会话运行时、长期助手、LangGraph、
  OpenAI Agents SDK 的代码级架构调研；B（`ou_6591…a895`）把约 59K Star 的 `sharkdp/bat`
  复刻为 Go CLI。A/B 初始请求实际重叠约 589 秒，没有建立额外 Gateway、飞书服务、owner、
  conversation、task 或项目副本。
- A 在自己的 `output/agent-runtime-architecture-study-20260730` 交付 1 个入口说明、6 份逐项目/交叉
  报告和 1 个可打开站点；16,010 个文件、326,900,605 bytes 包含四份固定提交源快照。主代理只创建
  2 个真实 child，分别研读 LangGraph 和 OpenAI Agents SDK，均为 `DONE + VERIFIED`，没有重复派工。
  独立检查了 13 个页面锚点、26 个链接、源码行号与四个 source SHA，旧错误结论和临时 `_verify.py`
  均不存在。
- B 在原 `output/gobat-20260730/src/gobat` 上连续纠正，没有重建项目。交付明确写成核心功能复刻而非
  虚报 bat 全量等价；当前共有 6 个 Go 文件、3,266 行。独立重新执行所有 package 后实际发现 74 个
  `Test*`，全部通过；重新构建后又逐项验证无参数/管道 stdin、显式 `-`、文件优先、多文件、help、
  version、`--` 结束选项、空格/等号参数、行号、非打印字符、缺失文件/目录、binary force，以及
  8 KiB 处中文/emoji 和非法 UTF-8 边界，全部通过。项目内无 cache、pyc、egg-info、测试二进制或
  symlink。模型曾只汇报一个 package 的 53/57 项数字，产品事实采用独立的 74 项计数。
- B 的最后一条普通飞书纠错在同一个 live request
  `req_1785365461664_2259862_5` 中连续消费 4 条真实 `/btw`：先阻止把无参数管道误改成 help，再补
  `--` 后的文件名语义、删除重复过滤层，并要求测试真的传入字面量 `--help`。每条都由飞书客户端显示
  “已补充到当前任务”，没有生成新 request；旧路线在下一安全点被替换。测试过程中真实出现的
  `COMMAND_FAILED`、`TOOL_INVALID_ARGUMENTS` 和一次越界临时目录拒绝均保留，模型随后在原项目修复，
  没有把失败改写成通过。
- 真测暴露的通用底座根因不是模型慢，而是终态 workspace 的 successor 曾复制旧 task link 的 goal；
  前台看似收到当前消息，后续 background wake 却可能恢复历史项目。当前实现让终态 link 只贡献 sticky
  cwd，successor goal 只取本轮精确 `root_user_prompt/user_prompt/prompt`；缺失时 fail-closed。后台仍
  读取同一 thread 的完整 transcript/summary，但 task link、observation、wake/progress 只按当前 task
  与持久 child lineage 投影，不从中文正文分类。实现对照 会话运行时 `3418498f0142` 的
  session/turn/task 边界，没有增加第二份会话或未完成清单。
- 另一个真测缺口是模型只能看到危险命令被拒绝，却没有单文件安全删除的权威恢复路径。当前实现复用
  既有 `apply_patch`：`*** Delete File` 同时进入 `ToolModelSpec.input_schema`、主代理、child 和统一错误恢复提示；目录或
  批量内容仍走正式 `task_trash`。实现对照 会话运行时 同一 apply-patch 生命周期和 长期助手
  `0b32ff708808` 的 patch parser，只增加一条共享规则，没有新增 delete 工具、shell 旁路或自然语言硬判。
- A/B 的 owner home、task/output、Persona、USER、Memory、Compact 与 thread 文件均是独立物理路径；
  双向产物扫描未出现对方项目，USER 内容 hash 不同。公共 `shared/{skills,tools,workflows}` 仍是同一
  公共层，当前目录为空，内置能力来自已安装 package。A 复用了既有 generation 6 的会话 Compact，
  B 保持自己的短历史；这两条新任务都没有重新达到 200K×90%=180K 的自动持久 Compact 触发点，因此
  本轮证明的是既有 Compact 续接不恢复旧 goal，不冒充一次新的 180K 自动触发证明。
- `3266 行` 与后来一轮 `operation_count=0` 不是同一统计范围。B 的长任务各轮有真实 ToolCall/ToolResult；
  最后一轮 `req_1785365461664_2259862_5` 单独就有 51 个工具轮、52 个逻辑模型回合和 56 次 provider
  HTTP 尝试。后来安全删除的第一次复测 `req_1785368956564_2345237_1` 确实为零工具调用，模型却用正文
  虚报创建/读取/删除；同一会话纠正后的 `req_1785369718599_2345237_2` 才真实执行
  `write_file/read_file/apply_patch/read_file`。后者“净文件变化”为零是因为临时文件先创建后删除，
  不等于没有工具调用。产品报告必须分别标明“长任务累计操作”“当前请求工具记录”和“当前请求净文件
  差异”，不得混成一个数字。
- 对照 会话运行时 `3418498f0142` 的 `TurnDiffTracker`、长期助手 `0b32ff708808` 的 no-tool-call 终止分支及
  终端交互 `7dc15d6c8fb0` 的 assistant-text 终止分支，三者都不会解析自由文本中的“已创建/已删除”
  来替代真实工具记录；会话运行时 的同轮先新增后删除也明确产生空的 net turn diff。因此没有增加中文完成词
  识别器、第二模型裁判、强制最终工具或 closeout 硬门。程序事实仍以 ToolCall/ToolResult、operation
  ledger 和文件系统为准，模型正文不取得机器权限。
- `d94213c0` 与 `b24f815f` 已通过本地 182 项聚焦回归，并由干净 wheel 精确部署到 1.10。最终 wheel
  上 A/B 两个真实客户端又分别读取对方 `USER.md`，两次真实 `read_file` 都在 handler 前以
  `PATH_CROSS_OWNER_BLOCKED/runtime_gate/handler_executed=false` 拒绝。A 随后在只读深度复查已经
  连续读取多个报告时发送精确 `/stop`；请求立即成为 `interrupted/INTERRUPTED`，15 秒后无迟到最终
  回复，下一条普通消息仍在原 thread 准确接上“刚才停止的复查”。13 条长任务/复测飞书出站正文扫描
  没有工具 XML、内部完成块或执行协议泄露。Gateway/Feishu 均为 active、`NRestarts=0`，8420 只监听
  loopback，请求队列为空，正式模型为 `anthropic_compatible + MiniMax-M2.7`。
- 最终全量门禁又发现新增 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 没有进入统一错误表，以及 4 个旧测试
  仍要求 shell 直接执行 `rm/rmdir/unlink`。当前已注册精确错误与恢复策略，删除这 4 个相反语义的旧
  测试，没有重新开放 shell 删除旁路；相关 141 项定向复验与完整 pytest 均通过。Ruff、import、
  offline、strict code-size、doc-sync、contract pyramid、replay、compileall 与 diff check 全绿。
  worktree clean-package 按设计拒绝 83 个现有未跟踪运行文件并识别多类大体积运行目录；这些文件被保留，
  不加入 Git，也不得进入最终 wheel。

## 2026-07-29 五套 CLI 深度分析对照后的底座修复发布

- 使用同一份 256 MiB、18,875 文件的只读七项目语料、同一普通中文任务和同一
  `MiniMax-M2.7`，依次运行 my-agent、会话运行时、模型助手 Code、终端交互 与 长期助手 CLI。语料运行前后
  manifest 完全一致；五套运行没有并发争抢供应商额度。完整对照记录见
  `docs/audits/CLI_DEEP_ANALYSIS_COMPARISON_20260729.md`。
- 这轮没有把“生成网页成功”冒充“结论都正确”。五套工具都能生成可打开的站点，但都出现过源路径、
  类名或能力边界写得比实际代码更肯定的问题；因此没有增加自然语言完成硬门或项目专用验收器。
- 对照及其修后续跑中有六项可独立复现、且由 my-agent 底座造成的问题，当前候选已做通用修复：
  1. 进程工具把结构化额外读取根映射为 Linux sandbox 只读挂载，写权限仍只来自显式写根；
  2. 单页成功落盘后，尚未完成的全站链接只作为过程 warning，最终
     `static_site_check` 仍执行完整硬验收；
  3. 当时的系统默认 child output ref 会在真实 run id 创建后重绑定到 run 专属目录，第二批子代理不再复用
     `01/02` 路径；2026-08-22 r9 证明内部 Markdown 会被误当成业务交付，故新任务已删除该默认引用。
     旧 durable task 只保留 rebind 迁移读取并从模型可见合同隐藏；用户显式输出路径和业务冲突锁保持原语义；
  4. provider 流式聚合只按结构化 delta/cumulative 前缀关系去重，兼容混合流且保留内容相同的两个
     合法增量，不做自然语言相似度判断；
  5. CLI 当前将 provider model delta 视为未提交草稿；工具进度仍走 typed progress，最终只打印经过
     统一出口处理的 committed response，不再提前泄露工具协议或因正文变化重复打印；
  6. 后台 scheduler 取得 thread/task claim 后，在当前 `RunParams` 写入既有
     `conversation_task_turn_active` typed fact；本轮可操作自己的任务，其他并发轮仍 fail-closed。
- 实现分别对照 会话运行时 的 readable/writable sandbox root 与流式事件主链、长期助手 的已流出前缀累计，
  终端交互 的原子写入成功语义，以及 会话运行时 active turn 对自身执行上下文的持有关系；只适配现有
  Tool Gateway、sandbox、child output rebind、backend usage、CLI 展示和后台 claim 路径，没有新增
  第二套工具、子代理、网页或等待系统。
- Linux/bwrap 已实测额外根可读不可写，任务输出可写且源 hash 不变。MiniMax 修后长链不再出现过程
  假失败；CLI 最终正文只输出一次，工具进度与结构化核验事实保持分离。另一个普通中文两批协作测试严格
  创建 2+2 共 4 个 child，均为 `DONE + VERIFIED`，后台第二批创建成功，主代理写出 4,314 bytes 汇总。
  汇总文件名没有严格遵循用户指定值，且仍有证据措辞过度肯定，按模型质量偏差记录，没有增加中文硬门。
- 最终代码通过 129 项后台/会话聚焦测试与唯一一次完整 pytest（100%、退出 0）；Ruff、import
  boundary、offline contract、strict code-size、doc-sync、compileall 与 diff check 全部通过。
  从干净 Git archive 构建的 wheel 共 1,001 个成员，SHA-256 为
  `cd980d72e1d8e7939116152dae7188b9f398393a547823ccc79818022b71bb99`；distribution boundary、
  artifact clean-package、全新虚拟环境安装/导入/CLI 均通过。worktree clean-package 按设计拒绝
  83 个未跟踪运行文件并报告多类大体积运行目录，这些数据没有删除、提交或进入 wheel。
- 上述 wheel 已精确部署到 1.10 唯一正式 Gateway/Feishu。两项服务 active、`NRestarts=0`，
  8420 仅监听 `127.0.0.1`，部署后 warning/error 日志为空；正式模型仍为
  `anthropic_compatible + MiniMax-M2.7`。

## 2026-07-28 原生工具完整计量后的语义续接修复候选

- 提交 `7642c134` 已进入远端 `main` 并部署 1.10：唯一 `build_tool_loop_prompt` 入口使用
  `model_visible_context_tokens` 统计 prompt、原生工具 Schema、ToolCall 参数、ToolResult、
  UserTurn 和待转发运行引导，达到配置阈值时只整对回收最旧 ToolCall/ToolResult。该版本修复了旧
  native 字符估算漏算 `write_file/edit_file` 大参数的问题，但回收后只留下数量 marker 和近期尾部。
- 同一正式飞书 owner/conversation/thread/task 随后运行请求
  `req_1785212641679_1652071_0`，始终续接原 `output/pyripgrep`，没有新建或复制项目，也没有
  Gateway/Feishu 重启。旧工具对被回收后，模型仍重新执行
  `find /root /usr /home -name rg`，再次发现早已在用户要求与旧 checkpoint 中明确的真实 `rg`
  路径。截至本节更新该请求已超过 340 个工具轮且仍在同一 active turn；这证明完整计量正确，
  但机械配对删除会丢工作语义。
- 当前候选不回退 `7642c134` 的完整计量。它在回收旧工具对之前，复用既有
  `compact_semantic_summary` 后端读取同一 native IR，并以最多一条 typed
  `CompactionSummary` 替换旧段。后续再次跨阈值时把前代摘要作为输入并原位替换；真实
  UserTurn、近期工具尾部、用户最新要求、精确路径/ID/端口/哈希/测试数字、未解决状态和下一步继续
  可见。摘要与原 handoff marker 都进入同一 recent-tail token 预算。
- 该 item 不是第二份 chat/task compact、会话、Memory 或事实账本。raw archive、operation ledger、artifact、
  workspace 文件和 thread transcript 保持权威；允许持久化的真实回合会把这份替代摘要与精确工具调用边界
  写入同一 ConversationThread checkpoint/generation，cursor 保持不动。摘要失败必须恢复原 IR 并进入同一
  失败熔断，不能账外继续丢历史。live 摘要同步等待 backend 已有界的 request timeout，不另起无法取消的
  20 秒后台线程，`/stop` 仍可从同一模型传输边界打断。
- 方案对照 会话运行时 `3418498f0142` 的同 history replacement summary、长期助手
  `0b32ff708808` 的会话压缩提交边界与 通道运行时 `05fb8e6e6190` 对 active task/status/精确引用的
  摘要约束；只适配到 my-agent 现有 IR、owner/thread/task 事实源，没有复制第二套状态机。
- 本地 46 项 compact/native 聚焦回归与当前收集到的 8,371 项完整 pytest 均退出 0；Ruff、
  import boundary、offline matrix、strict code-size、doc-sync、compileall 与 diff 检查通过。聚焦覆盖
  200K/90%、大工具参数短结果、连续两次跨阈值、前代摘要参与下一次摘要、摘要最多一条、真实
  UserTurn 与最新 checkpoint 保留、摘要失败回退、无 tool-use/tool-result 孤儿和无压力时不重复摘要。
- 从干净 Git 快照叠加本轮明确改动构建的候选 wheel 共 1,003 个成员、2,916,435 bytes，SHA-256 为
  `8e57556e226a77c2b6eabdac910034511378f94f313476f680d64909016721f2`；distribution boundary、
  artifact clean-package 和 `/tmp` 干净目录安装/导入/CLI 均通过。worktree clean-package 仍按设计
  拒绝 83 个保留的未跟踪文件，并明确报告 `live-agent-runs`、`data`、`validation/real_runs`、
  `memory_archive` 与 `memory` 等大体积运行目录；这些用户数据没有删除、提交或进入 wheel。
  当前语义候选尚未提交、推送或部署，部署后真实多次 Compact 复验仍待完成。
- 本次测试中最先发出的两条 `/btw` 被当时版本的私聊密码锁正确拦截，没有进入任务或 transcript；
  解锁后只补发一条，有效 guidance id 为 `guidance-c02acae5edda4b42`，并已在原 task 下一安全点
  消费。这里保留的是历史实测事实；当前候选已改为正常解锁后按原 `message_id` 自动续送，不把旧版本
  被拦截的消息冒充已生效引导。

## 2026-07-28 真实飞书 200K/90% 超长任务与工作区续接

- 同一真实飞书 owner `ou_1be…f921`、conversation `oc_388…cddd`、thread
  `thread-74479991be1c4144` 一直续接 2026-07-24 的原 task 和 `output/pyripgrep`；后续修复没有新建、
  复制或重做项目。正式配置始终为 `anthropic_compatible + MiniMax-M2.7`，
  `model_context_window_tokens=200000`、`memory_compact_auto_trigger_percent=90`。
- 首段真实工程请求 `req_1785151939712_1489561_4` 连续运行 306 个工具轮；第二段
  `req_1785174187665_1569565_0` 连续运行 147 个工具轮。两者虽然都在同一请求内结束，却暴露出明确
  Compact 回归：达到精确 180,000 token 触发点后，末次 replacement 仅从
  `180026→179626` 和 `180989→178570`，仍被当作成功，因而分别重复触发 93 次和 40 次。
  这只能证明任务没有中断，不能证明 Compact 健康。
- 当前修复候选删除这条“刚低于触发线即成功”的 live replacement 及其专属遥测，恢复回归前所有
  主代理/子代理共用的工具窗口：每次模型调用前限制文本工具历史，并对原生
  `tool_use/tool_result` 整对回收。90% 配置与持久会话 transcript 的自动 Compact 不变；live
  工具窗口使用同一已解析预算，但不会推进第二份持久 Compact。raw transcript、工具归档、
  checkpoint 和同一 thread/task 均不改变。
- 本地 200K/90% 集成回归把 8 组、约 96 万字符的原生工具往返一次收敛到最近 4 组，最新结果保留、
  调用和结果无孤儿，也没有误发第二次持久 Compact 请求。429 项
  Compact/Gateway/主代理/子代理定向回归与 8,366 项完整 pytest 均通过；Ruff、compileall、
  import boundary、offline matrix、strict code-size 和 doc-sync 均通过。临时 wheel
  SHA-256 为 `4baa9cfa019b160657031c25fefda606a7799929a246e3d98cb15647d48e5de0`，
  distribution boundary 与 artifact clean-package 通过；该候选尚未部署、提交或推送。
- 长链“正常结束”不等于产物自动合格。独立验收先后抓到 `--max-depth` 边界、JSON 事件语义和清理
  自述不准确；均沿同一 Feishu 会话和原项目纠正。`/btw` 在
  `req_1785182404270_1587412_1` 运行期间两次进入同一个 live request，要求拆短命令并校准 0/1/2
  深度表，没有产生新的 Gateway 请求；旧生成路线在安全点后被纠正。
- 真实纠正最终把项目测试修到 64/64。独立从 1.10 复制当前交付后，全新目录安装唯一标准 wheel，
  子包导入和 CLI 通过；另以 macOS 系统 `rg` 对照 28 个黑盒场景，结果 28/28。独立的
  `max-depth=0/1/2` 表与系统 `rg` 的输出及退出码也逐项一致，wheel 内源码与交付源码 hash 一致。
- 最后一轮 `req_1785183844424_1587412_2` 证明先前“交付目录锁定”不是永久能力缺口：绝对
  `/root/...` 删除路径被保护是正确行为，切换为项目工作目录内相对路径后，`.pytest_cache`、`build`、
  egg-info、全部 `__pycache__/pyc` 和 `work/` 均由正式 Agent 工具真实删除。远端最终顶层严格只有
  `README.md`、`pyproject.toml`、`pyripgrep`、`tests`、`test_data`、`dist-final`，无 symlink 或缓存；
  `dist-final` 只有 `pyripgrep-0.1.0-py3-none-any.whl`，25,725 bytes，SHA-256
  `5722fa23b75ee403f48ad1b8b31f741cab9fe436c5bbebab689491f8d760b855`，wheel 内 13 个成员且垃圾成员为 0。
- 这次真测同时暴露并修复一个通用底座竞态：消息、摘要、通道、verbose、observation/wake 等入口若把
  旧 thread 快照整份回写，会把刚选择的 sticky workspace 改回旧 task。现在这些入口都在文件锁内读取
  最新 thread、只合并自己负责的字段；工具入口只接受绝对路径或 durable `owner_home` 下规范且唯一的
  `tasks/...` 地址来恢复原 workspace，普通相对路径、`..`、歧义和跨 task 仍 fail-closed。
- 底座修复的 137 项会话/工作区/控制/Compact 定向测试与完整 pytest 均通过；Ruff、import boundary、
  offline contract、strict code-size、doc-sync、distribution boundary 与 artifact clean-package
  通过。部署 wheel SHA-256 为
  `8efa97f7ada56d1046c139078118473837bc6ccf2f3e89b84fc1244738009b04`；1.10 正式
  Gateway/Feishu 均 active、`NRestarts=0`、8420 仅 loopback。

## 2026-07-27 系统命令与当前窗口停止发布

- 提交 `7776a03f` 把 `/status`、`/btw`、`/stop`、`/goal`、`/verbose` 和 `/audit` 收敛到
  Gateway `/ask` 前的单一 typed dispatcher；IM adapter 与 CLI 不再各自解释一遍。未知 `/XXXX`
  fail-closed，模型 worker 另有 `SYSTEM_COMMAND_ROUTING_ERROR` 最后门，命令词不能成为普通模型输入。
  `/audit` 只把去前缀后的正文和白名单 `system_task` 送入任务链。
- `/stop` 的直接目标是可信 owner/channel/conversation 当前精确 live request，而不是先猜是否存在
  durable task。它立即关闭当前模型传输，再持久化 cancel、task/goal/guidance 与子代理终止状态；
  transcript、Compact、Memory、Persona 和 workspace 不删除，下一条普通消息仍在同一会话继续。
- 当前 8,365 项完整 pytest、Ruff、import boundary、offline matrix、strict code-size、doc-sync、
  compileall、diff、distribution boundary 与 artifact clean-package 均通过。最终 wheel SHA-256 为
  `14c6dbebac4367b9aa6a6cc40fc6679111d8a00c98472f9e65be2e63b99f1ed2`，已部署 1.10 唯一正式
  Gateway/Feishu；两项服务 active、`NRestarts=0`。
- 真实客户端 A `ou_6591…a895` 和 B `ou_1be…f921` 分别在只读工具长任务运行中发送 `/stop`。
  请求 `req_1785149836112_1489561_0` 与 `req_1785150148687_1489561_2` 的 request/response 都是
  `interrupted / INTERRUPTED` 且有 cancel marker；两边都没有迟到最终正文，停止后的普通消息仍准确
  记得各自刚才的任务。A 的 `/verbose on/off`、`/status`、未知 `/future-mode` 与 B 的 `/status`
  均为系统直接回复，没有模型请求，也没有写入任一 transcript；A/B 的 verbose、Compact 和旧任务摘要
  保持各自独立。正式工作目录下 plain CLI 得到同一控制结果；临时 CLI session 已清理。

## 2026-07-27 单一 Compact 与子代理恢复发布

- 子代理专用 Compact、session continuation、专属 continue packet、恢复目录和 task/agent compact
  索引实现已删除；主代理与 `context_scope=task_local` 的 child 调用同一通用 Compact。新 child
  只在自己的 `memory_archive/runs/<run_id>/compact_applies` 留通用归档，不写 owner 长期 Memory，
  也不创建旧 `compactions/` 或 `recovery/`。
- Compact work-state 以当前 task 的结构化 goal/next action 为权威；旧归档的工具进度、coverage 与
  cursor 只保留为事实。只有显式 `full_source_read` 合同可把未完成读取游标提升为下一动作，避免多次
  Compact 后沿旧文件读取路线偏航。相关 Compact/子代理恢复与配置继承共 151 项回归通过。
- 1.10 既有 child 的 9 次压力 Compact 与一次候选 apply 复核恢复了原 会话运行时 审计目标。随后同一
  Feishu owner/conversation 的三次纠错没有新建项目；独立验收两个 会话运行时 JSON 各 70 条，路径和精确
  行号全部有效，旧 通道运行时/LangChain 产物未改动。
- 最终完整 pytest 共收集 8,360 项，运行到 100% 且退出码为 0；Ruff、import boundary、offline
  matrix、strict code-size（`hard=0 / high-risk=235 / soft=88 / blocked=False`）、doc-sync、
  compileall 与 diff 均通过。全量首次暴露两个旧测试仍使用已删除语义：一个 task-local run 没有提供
  child workspace，另一个要求预览被截断时不保存完整恢复内容；生产 fail-closed 和 recovery artifact
  语义未回退，只把测试改为当前真实合同，相关 86 项和最终全量均通过。
- 最终 wheel 含 1,004 个成员、2,911,383 bytes，SHA-256 为
  `bb5026c7ff0125d834db77d8e4a92dd30c73747a32be4282e8f59535495a62d6`；已删除模块命中为 0，
  distribution boundary 与 artifact clean-package 均通过。worktree clean-package 正确拒绝 90 个、
  737,523 bytes 的保留 `data/`/handoff 项，并报告其他大体积运行目录；这些用户证据没有删除、提交或
  装入 wheel。
- 1.10 正式 site-packages 与 `/root/my-agent-src/agent_py_agent` 已安装同一 wheel，998 个发布 payload
  逐项 hash 一致。无保存 MiniMax CLI 返回 `CLI-SHARED-COMPACT-FINAL-OK`，0 工具轮、0 Memory；
  两个既有 Feishu owner 并发请求
  `req_1785128065900_1448524_1` / `req_1785128065899_1448524_0`
  分别只回答 `松针-741` / `海盐-852`，均为 0 工具轮。该请求是可信 localhost Feishu scope，
  不冒充新的客户端入站。Gateway/Feishu 均 active、`NRestarts=0`、队列为空，WebSocket connected。

## 2026-07-26 Memory/Persona 安全边界、真实 compact 与双客户端反证

- 当前 owner 的 `memory/long_term/memory.jsonl` 仍是唯一 active 长期记忆权威；Persona 仍由
  `SOUL.md/USER.md/AGENTS.md + PersonaRepository` 单链管理；普通 conversation compact 仍只有一套。
  本轮没有新增 Feishu 记忆、provider 记忆、task compact、第二个向量库或另一条工具执行器。
- `remember` 的 active 写入现在必须显式携带 `user_explicit`，或以本轮成功工具 ref 证明
  `tool_verified`；`model_inferred` 只写不可召回候选。完全重复写幂等，同一 `subject_key` 冲突要求
  list/replace，不静默覆盖。Related Memory 使用安全数据 envelope，加载再扫描，任何用户出口都会去掉
  完整或截断 envelope。
- hard delete 清除该 entry 的 long-term 历史正文、daily mirror、LocalStore/FTS 内容文件、可选向量、
  关联候选和 remember 结构化工具账本正文，只保留无正文 tombstone/hash/调用终态。conversation、
  gateway audit 与 task facts 记录真实交互历史，属于独立留存边界，不会被“删除长期记忆”偷改。
- compact 正式阈值继续是配置的 90%，直接计算 `context_window × 90%`；MiniMax API 未给窗口时才按
  本地配置回退。本轮独立 MiniMax owner 在 19K/17,100 token 压力配置下连续完成 8 次真实 compact，
  24K 人工链完成 4 次后保持有界；正式 200K 默认不受压力配置影响。
- 每轮工具采样尾部新增 `current_turn_execution.v1`，只从当前 request 的 canonical tool records
  投影成功/失败副作用和 refs。Registry、operation store 和权威文件仍是事实源；模型正文不是审计。
  最终结果现由同一批记录生成 `operation_verification.v1`：副作用成功必须同时满足工具 `ok=true` 与
  权威 operation `succeeded`，失败、未执行、未知、取消、未结束和缺终态分开表达；同一 operation 的
  幂等重放只算一次。公开投影不含 call/operation ID、参数、路径或 refs，并贯穿 CLI result、
  Gateway/HTTP、正常或延迟修复 transcript、后台长任务、历史索引和 compact。每条 assistant 记录都
  带该机器投影，包括 `operation_count=0`；用户正文不追加固定核验块，机器投影只保存在 metadata，
  出口按本轮 typed operation records 精确隐藏意外照抄的内部工具标签。真实 B 客户端已证明 MiniMax
  可能在只 list 或零调用时误称 remove/update_persona 成功；
  新投影使“程序做了什么/没做什么”不再依赖该正文，但在禁止自然语言语义判断的边界下，不承诺程序能
  理解并删掉自由正文里的每一句错误自述。
- 真实 MiniMax Compact 反例进一步证明，摘要模型会把结构化 `remember/list` 错总结成“成功删除”。
  `conversation_thread.v4` 因此把有界 `compact_operation_evidence` 与摘要/cursor 原子推进，后续轮在
  摘要之后单独注入程序事实。反例复测中模型摘要仍写错，但下一轮依据唯一 `remember/list` 明确回答
  “没有删除”；旧消息缺机器 metadata 时 coverage 标为 partial，不补猜。该字段属于同一 thread 的
  compact metadata，不是第二套会话或 Memory。
- 本地 8899 完成基础 Memory/Persona 安全回归；MiniMax-M2.7 另以独立 owner 完成 600 条记忆、
  9 个工具轮、约 9.7 KiB 报告、来源/证据/冲突/敏感拒绝/hard-delete 与真实 compact 极限测试。
  两个真实飞书客户端 owner 都完成写入、跨轮召回和隔离；A 的清理全程有工具记录，B 的错误自述被
  反证后使用正式工具入口确定性清理。最终两边 active Memory、`USER.md` 与 memory index 无对方测试值，
  出站正文无 memory envelope、执行事实 JSON 或工具协议。
- 最终 1.10 发布复验继续复用 A/B 两个既有真实 owner 与原 conversation，没有新 owner、conversation
  或项目。A 请求 `req_1785050485322_1318040_0` 真实完成四次 Memory 操作；B 首轮
  `req_1785050485332_1318040_1` 的正文声称完成但机器投影为零操作，同会话纠正请求
  `req_1785050618969_1318040_2` 才真实完成四次。A/B 的临时值最终在双方 active Memory 中均为 0，
  Persona、Skill、项目和子代理无改动。随后两边各用一次 `send_message`，请求
  `req_1785050812427_1318040_3` / `req_1785050812653_1318040_4` 的 operation 与飞书 receipt
  分别为 `succeeded/sent`。这是服务器侧 Feishu scope + 真实飞书出站，不冒充新的客户端入站。
- 模型 HTTP 统一传输只对显式 loopback URL 强制直连，外部供应商继续使用现有代理配置。当前 Mac 在
  系统代理开启且未手工设置 `NO_PROXY` 时，8899 Qwen 已真实完成 `remember/add` 并被程序核验成功；
  本地 fallback 不再被 7890 代理截走。
- 最终 wheel 含 1,008 个成员、大小 2,904,941 bytes，SHA-256 为
  `b3084c12009b259aa1b50f4954a51c9ebcbfb6f0230990d1a4f1f3200657f1f2`；首次候选被
  distribution boundary 抓到旧 `build/` 缓存夹带两份已删除模块，清理后 distribution boundary
  与 clean-package artifact 均通过。2026-07-26 15:19 CST 已把该精确 wheel 安装到 1.10；
  正式配置保持 `anthropic_compatible + MiniMax-M2.7`，Gateway/Feishu 均 active、
  `NRestarts=0`，仅监听 loopback 8420，飞书 WebSocket 已重新连接，部署后队列为空。
- 完整 pytest 共收集 8,315 项，运行到 100% 且退出码为 0；Ruff（正式生产范围）、架构守卫、
  import boundary、offline matrix、strict code-size（`hard=0 / high-risk=226 / soft=79 /
  blocked=False`）、doc-sync、compileall、diff、distribution boundary 与 artifact clean-package 均通过。
  worktree clean-package 仍按预期拒绝 83 个、701,826 bytes 的既有未跟踪 `data/`/交接材料；这些运行证据
  按要求保留且未进入 wheel，不能把 worktree 的拒绝误写为制品失败。

## 2026-07-25 工具失败分层诊断与双真实 owner 权限收口

- 工具失败现在同时保留四项互不替代的结构事实：`error_code` 说明发生了什么，
  `failure_stage` 说明坏在协议、授权、参数、运行门、实现、效果核对还是持久化，
  `handler_executed` 说明真实工具实现是否已经进入，`duration_ms` 说明本次调用耗时。模型回复、协议
  envelope、审计、tool index、compact/recovery 与 runtime ledger 都消费同一份事实，不按错误文字
  猜层级。
- 统一生命周期参考 会话运行时 `32329b289d05` 的集中 Registry dispatch、turn permission profile 与
  handler-entered outcome；耗时和状态表达参考 长期助手 `4be38125af06`。没有复制 长期助手 可由模型覆盖的
  cross-profile 软警告：my-agent 的 owner 隔离继续是硬边界。错误分类只在统一 Registry、runtime gate、
  handler、effect coordinator 与 persistence 缝隙产生，没有为飞书、某个工具或某句中文增加特判。
- 精确幂等重放会把“本次是否进入实现”记录为 `false`，同时保留首次执行的嵌套事实；超时或外部效果未知
  进入 `effect_reconciliation`，不得盲目重试。长输出的安全外置记录也只白名单保存上述诊断字段，私有
  handler 数据和原始敏感参数不能借归档进入上下文。
- 参数 Schema 继续只有一个正式入口；native 工具说明使用同一份精确字段约束。`apply_patch` 的模型说明
  已收敛为 会话运行时 的 Begin/End Patch、Add/Update/Delete/Move 与 `-old/+new` 语法，没有另造宽松解析器。
  本地 Qwen 最终能自纠正完成补丁，但在 1.10 真实 CLI 中用了 7 次尝试；MiniMax-M2.7 第一次就使用正确
  语法。前者是模型效率差异，不是权限或执行器绕过。
- 本地 8899、1.10 Linux/bwrap CLI 与 MiniMax-M2.7 长链均已真实执行。MiniMax 链共 16 轮、21 条工具
  记录，覆盖成功、缺文件、危险根路径、隔离命令、非零退出、超时效果未知、长输出外置、文件写/改/补丁
  与 owner 写边界；`/etc/passwd` 在实现前拒绝，超时没有自动重试，逃逸文件不存在。
- 两个真实飞书客户端沿各自原 owner/conversation 完成工具长链。A 请求
  `req_1784986368980_1301799_1` 验证成功、缺文件、危险根、bwrap、写/改/补丁和非零退出；B 请求
  `req_1784986266050_1301799_0` 额外验证跨 owner 与超时。B 首轮暴露跨 owner 虽被 handler 安全拒绝，
  但中央 runtime gate 漏传 owner scope，导致诊断层级偏晚；修复后真实客户端请求
  `req_1784987075421_1304872_0` 精确得到
  `PATH_CROSS_OWNER_BLOCKED / runtime_gate / handler_executed=false`。
- 完整回归随后发现 owner scope 不能把 runtime 明确授权的外部 CLI workspace 一并封死。中央路径门现与
  文件实现共用同一层次：其他 owner、admin grant、凭据和危险根保留专用硬拒绝；只有当前
  `workspace_roots` 结构化授权范围可越过普通 `PATH_OWNER_SCOPE_BLOCKED`，用户文字和模型参数不能扩根。
- A/B 最终各自只能看到自己的 `output/feishu-A` 或 `output/feishu-B`，没有交叉产物、逃逸文件、
  Persona/Memory 改写或用户可见内部协议。最终完整 pytest 到 100% 且退出 0；Ruff、import/offline、
  strict code-size、doc-sync、compileall 和 diff 同轮通过。worktree clean-package 正确拒绝 84 个保留
  的未跟踪文件，并单列约 3.77 GB `live-agent-runs`、845 MB `data/` 与 202 MB
  `validation/real_runs`，没有删除或忽略这些运行事实。
- 最终 wheel SHA-256 为 `e7a77182df0e79d9e8dda08d296d06017b3a6e19969539cbae63509faa468a1a`，
  distribution boundary 与 artifact clean-package 均通过，并已精确安装到 1.10 现有 venv。最终仍
  只有正式 Gateway/Feishu 和 loopback 8420；两项服务 active、`NRestarts=0`、队列为空，Feishu
  WebSocket connected。

## 2026-07-25 模型可见工具结果的统一安全投影

- 工具完整原始结果不是直接拼进模型上下文。正式执行仍只有 ToolExecutor/Registry handler seam 一条主链：`ToolRuntimePolicy.output_policy` 声明最低
  输出信任/脱敏策略，执行结果只能继续收紧；live context、compact、恢复、父子代理共享和 handoff
  共用同一模型投影，不各自维护名单或包装器。
- 网页、浏览器、MCP、视觉、watch 和已归档工具正文按外部不可信数据进入模型。包装会中和伪造的边界
  标签，并明确其中的角色设定、操作要求或工具调用没有指令权；所有模型可见正文先经过统一凭据脱敏。
  源码读取使用 source-code redaction 以减少误删代码，但这不扩大路径或 owner 权限。
- 大结果的完整正文仍按 owner/task 隔离留在 `work/blobs/tool_outputs/`，上下文只保留有界 preview、
  hash、大小和稳定读取引用。通过 `read_artifact`、`read_file`、`search_text` 再进入模型时继续继承
  该目录的外部数据来源；JSON wrapper 和纯文本 resilience archive 使用同一结构判断，不按文件名后缀
  或自然语言内容猜测。
- 这项能力防的是凭据泄露、上下文膨胀和工具结果中的 prompt injection；它不替代工具授权、业务前置
  条件、幂等或外部写结果核验。聚焦回归、本地 8899 Qwen、MiniMax-M2.7 和两个真实飞书客户端均已
  验证：A 请求 `req_1784968159181_1290822_0` 读取 会话运行时 文档，B 请求
  `req_1784969134442_1290822_1` / `req_1784975003351_1290822_2` 读取 长期助手 源码；结果保持
  `external_data/default`，没有产品写入或外部副作用。
- 真实 B 客户端还发现同一 Gateway 请求的进度批次和最终回复曾错误共用 provider 幂等键，导致飞书把
  后续逻辑消息去重。当前通道无关的 DeliveryService 入口按可信 message ID、request ID、phase 和
  progress cursor 形成稳定消息身份：同一逻辑消息重试不重复发送，不同进度批次与 final 不再互相吞掉。
  修复后请求 `req_1784975858195_1299659_0` 在客户端显示 5 条分阶段回复和最终
  `DELIVERY_OK _is_destructive_command`。精确 wheel SHA-256 为
  `5564877d0cebc8ffcb60139391740c705588ce6825cf0af4cf9f6bdfd914928c`，distribution boundary 与
  artifact clean-package 均通过，并已部署 1.10 唯一正式 Gateway/Feishu 服务。最终完整 pytest 100%
  且退出 0；Ruff、import/offline、strict code-size、doc-sync、compileall 和 diff 同轮通过。

## 2026-07-25 长任务截断、运行事实与双 owner 收口

- provider 明确返回 incomplete 时仍按失败响应处理。普通聊天或尚无已完成工具结果的轮次立即返回
  `MODEL_INCOMPLETE_RESPONSE`；只有同一运行已经形成 canonical tool record/tool result 时才允许一次
  有界继续。半截正文和未闭合工具参数全部丢弃，已经完成的工具结果和已写产物保留；第二次仍截断就终止，
  不做无限重采样。任意一次正常模型响应后，本次 provider-response repair 额度重新计数。
- 该行为采用 会话运行时 `会话运行时-api/src/sse/responses.rs` 的 incomplete-is-error 边界，并只在已有耐久工具结果时
  适配 长期助手 `agent/conversation_loop.py` 的有界 length continuation；没有新增 provider 专用循环。
  `/btw` 仍只由 typed mailbox 决定：等待中的 UserTurn 会在安全点让旧空/incomplete 响应失效，不解析
  用户正文决定控制流。
- 模型、工具循环或 finalization 抛出终止异常时，既有 runtime fact 会由同一 terminal updater 从
  `running` 原子收敛为 `failed`，`InterruptedError` 收敛为 `cancelled`；已有工具轮数、已执行工具、
  artifact 和 next actions 保留，另附结构化错误。Gateway 请求终态与 Memory 恢复事实不再一边失败、
  一边长期谎报运行中。
- 1.10 正式 Feishu 用户 B 的平台真实入站沿原 conversation 做只读续接，Gateway 请求耗时
  `607.907s`、9 个工具轮次；模型没有因依赖已按要求清理而伪造重新跑测试，独立复制仍确认原
  `schedule-ts` 的 68 项测试证据、发布包 11 个文件、源码 commit `82a43db1…d3651`、tgz SHA-256
  `28203e1d…5d55`，且没有 cache、pyc、egg-info、node_modules 或 symlink。
- 用户 A 沿原 conversation 完成四个 scheduler 项目的源码深读报告。模型多次自报完成后，独立复核仍
  找到 APScheduler 4.x 导入、gocron 启动/错误处理、`max_running_jobs` 归属和 node-schedule API 示例
  错误；每次都沿同一 task/output 发送普通中文纠正，没有新建或复制项目。最终独立验收确认 output 只有
  `README.md` 与可解析的 `matrix.json`，四个源码仓库分别固定在
  `82a43db1…d3651 / 26bff5d1…b257 / cc444c2a…a10d / afaefb9b…eb9` 且全部 clean。
  这证明同一会话续作和工具链可用，也同时证明高要求研究报告仍不能把模型“已完成”当验收事实。
- A→B 与 B→A 各做一次真实 `read_file` 越界反证，两边均在实现前由 owner 路径边界拒绝，只发生一个
  失败工具轮，没有复制或写入。A/B 的任务目录仍为 64/19；`USER.md`、`SOUL.md`、`AGENTS.md`、
  Memory、私有 Skill/Tools 未发现对方完整 owner id，私有区无 symlink。最近用户可见 transcript 未发现
  tool XML、主/子代理协议或 shell trace。
- 本轮只有 B 的这次续接来自真实 Feishu 客户端；A 的纠正和双向越界反证是可信 localhost
  Feishu-scoped `/ask`，不能冒充平台客户端入站。macOS 再次锁屏后，两个真实桌面客户端同时发起长任务
  仍未取得新证据。另有一个已知性能缺口：约 4.4 GB、100 万文件的 owner 在应用层做精确逻辑配额扫描时，
  单次写入准入约 60 秒；它不影响本轮隔离正确性，但规模部署必须依赖 filesystem/project/container quota，
  不能继续把全树精确扫描当高频热路径。
- 最终本地全量 pytest 到 100% 且退出 0；Ruff、import boundary、offline contract、strict code-size、
  doc-sync、compileall 和 diff gate 全部通过，strict code-size 为
  `hard=0 / high-risk=199 / soft=69 / blocked=False`。worktree clean-package 按设计拒绝 83 个明确保留的
  未跟踪文件，并单独识别约 3.76 GB `live-agent-runs`、845 MB `data/`、202 MB `validation/real_runs`
  等大体积运行数据；这不是可忽略的绿色门，发布必须以随后从当前源码构建并通过 artifact gate 的干净
  wheel 为准。本轮最终 wheel SHA-256 为
  `995dee17dfe6327eb40df4de96686796ad73d5e5aad1ba4d8c7716e688347553`，共 1,004 个成员；
  distribution boundary 与 artifact clean-package 均通过，`docs/`、`data/` 和上述运行目录成员均为 0。
- 该精确 wheel 已安装到 1.10 现有 venv；五个改动生产文件与 wheel 成员 SHA-256 逐项一致，新增 typed
  helper 导入通过。最终仍只有正式 Gateway/Feishu 两个进程和 loopback 8420，模型配置保持
  `anthropic_compatible + MiniMax-M2.7`，两项服务 active、`NRestarts=0`、队列为空且 Feishu
  WebSocket connected。

## 当前能力矩阵

| 能力 | 状态 | 当前事实与承诺边界 |
| --- | --- | --- |
| Python 包、`my-agent` CLI、默认 gateway/chat 主循环 | 稳定 | 唯一正式普通用户入口是无子命令 `my-agent`，自动确保 gateway 存活并 attach chat client。`run` 与 `chat --direct` 是脚本/调试面，不是另一套默认 runtime。显式 `my_agent_home` 是 profile 权威；只有留空时才回落到 `MY_AGENT_HOME`，配置注释与既有测试已统一。稳定范围不包含十万用户容量承诺。 |
| 本地文件工具、结构化 Tool Gateway、错误分类 | 稳定 | 正式工具调用统一经过注册、授权、参数、路径、限流、effect 和权威 operation claim；不得通过直接新增旁路执行器绕开。参数层以 `ToolModelSpec.input_schema` 为唯一 JSON Schema，provider、显式 text/native adapter、MCP、恢复门与最终执行共用同一 `schema_hash`；安全默认值和可信上下文补参只来自 `ToolRuntimePolicy.input_policy`。typed canonical ToolCall 将外层身份与参数分离，模型参数不能覆盖 operation identity。mutating/dangerous 工具在实现前以 `owner + run + operation_id` 原子占位，同一精确操作只重放，参数冲突、并发副本和崩溃歧义转 unknown，store 不可用默认 fail-closed。统一切片已接入 run 固定协议/ToolChoice、ActionPolicy、ToolExecutor、required actions/CompletionGate、策略并发与取消，并于 2026-08-04 完成全量 pytest、静态/制品门和 MiniMax-M2.7 四条普通中文真实验收；脱敏证据在 `validation/real_runs/tool-runtime-20260805T141123Z/report.json`。这些是当前未提交 worktree 证据，不冒充已部署版本。既有 operation claim 已随提交 `9f03140e` 进入远程 `main`，精确 wheel（SHA-256 `f702784a…e448b`）已部署 1.10；既有 8899 Qwen、两个 Feishu owner 的并发写入/回读/消息投递与跨 owner 拒绝证据仍成立。 |
| 发布干净度检查 | 稳定 | 工作树模式检查 tracked 和未忽略 untracked；制品模式直接检查 wheel/zip/tar 成员、运行目录、路径穿越和大小预算。distribution boundary 还逐项核对 wheel 中的 `agent_py_agent/` payload 必须存在于当前源码树，旧 `build/` 缓存不能把已删除模块重新带回发布物。 |
| 单用户 owner home、文件记忆、SQLite/FTS | 稳定 | 适用于本地/单节点；不是 PostgreSQL、RLS 或在线迁移的替代证明。 |
| 多用户 owner scope 与 Linux shell 隔离 | 部分可用 | owner-scoped 前后台 shell 必须经 bwrap；不可用时结构化 fail-closed，禁止宿主降级。root 部署的宿主 home 放宽仅限无 owner scope 的本地管理员。远程 owner 默认只能访问自己的 owner home 和管理员显式发布的 `~/.my-agent/shared/`；其他 user/group owner、根模板、旧顶层私有目录与未授权宿主路径在 full mode 下也拒绝。只有当前轮的结构化 capability/delivery contract 可精确加入额外 workspace root，且不能覆盖凭据文件或其他 owner 拒绝。子代理 shell/后台命令/PTY/LSP 使用 owner home 只读基座加精确 `allowed_write_roots` 可写叠层，并阻止 PTY/LSP 跨任务复用权限；该边界已随 SHA-256 `bd8f9eb2…06ca1` wheel 在 1.10 经第二 owner 的旧任务续作反证，A/B owner 私有产物、人格、`USER.md`、skills 和 memory 反向检索均未互读。进程工具在没有显式 `working_dir` 时使用结构化选中的 `task_root`，显式目录仍优先；不解析用户文字或 shell 命令。随 wheel 发布的 builtin tools/skills 是公共代码能力。Docker 真机已验，Kubernetes 目标集群仍需节点 profile 分发与验收。 |
| 一键容器安装 | 部分可用 | P0 容器与 bwrap 改动已进入远程 `main`；安装器可生成透明 `my-agent` 包装器。scale K8s 清单已有 migration、stable/canary ingress+worker、monitor、灾备 Job；目标节点 profile、镜像签名/SBOM 和集群滚动验收尚未完成。 |
| Feishu 接入、会话/身份边界 | 部分可用 | 默认长连接、密码/确认卡片、per-user/per-group owner 与普通自然语言入口已接通。Feishu 只负责入站和投递，不拥有会话、任务、compact 或 memory 语义；同一 `chat_id + thread/root_id` 只累计一份 thread transcript，task/workspace/子代理只是该 thread 的运行事实。CLI/IM 共用 `/status`、`/btw`、会话运行时 式 `/stop` 与 `/goal`；`/btw` 在 active turn 安全点作为真实 UserTurn 进入同一 transcript，`/stop` 保留 transcript/workspace/memory，普通回复仍由 LLM 根据结构化事实生成。真实平台用户 `ou_6591…a895` 已完成约 12 分钟工具长任务并在运行中继续聊天；2026-07-24 第二个真实平台用户 `ou_1be…f921` 从客户端发起同一旧 task 的文件任务并立即 `/btw`，权威 ledger 只消费一次，最终只有一个两行目标文件且经原平台消息引用回复。因而目前已有两个不同用户各自至少一次平台真实入站闭环。切回 MiniMax 后又复用两者原 owner/conversation 做并发服务器侧 Feishu-scope 只读测试，A 只读到自己文件，B 跨 owner 读取被拒；这部分不冒充新的客户端入站。两个真实客户端同时跑长任务、群组成员边界、十万用户容量、故障切换和长期运营仍未完成。 |
| 模型能力自我描述与通道发现 | 部分可用 | composition root 创建唯一 `ChannelAdapterRegistry`，`list_capabilities`、`send_message` 与 adapter manager 复用同一实例；Feishu/QQ 的 installed/configured/heartbeat health/error code/current binding 统一投影，收件人 ID 不进入模型清单。工具能力也不再从全局注册表推断：`list_capabilities.execute_scoped` 与 `list_tools/tool_search/Schema/execute` 消费当前 run 的同一 `ToolRuntimeSnapshot`；未授权工具完全隐藏，授权范围内但当前不可用的能力只显示 unavailable，私有 readiness 原因不出站。1.10 MiniMax 真测实际返回 45 个可用工具，未配置的视觉/LSP 没有被说成可用；仍缺更多真实通道和动态插件长期探活。 |
| Shared 与 Skill 加载 | 部分可用 | 当前工作树由 composition root 创建唯一 `SkillsService`，逐轮不可变 snapshot 固定 `workspace > owner > shared > builtin`，并统一供 prompt、`skill_search`、能力自述和子代理使用。管理员发布的 shared 与 wheel builtin 公共只读，owner/workspace 私有；source policy、shared allowlist、disable、bounded scan、frontmatter/guard 错误、symlink escape、缓存失效和内容 SHA-256 已进入主链。普通 turn 只暴露 Skill 发现/读取，不常驻暴露创建工具；显式学习/确认链保持独立。子代理只获得显式保存的 stable id + hash 子集，后代不能扩大。旧 `SkillRegistry`、resolver/index runtime、普通 `create_skill` 工具与编排临时 router 已删除；相关聚焦回归和 A/B owner 反证通过。1.10/Feishu 真测、长期发布版本与 supporting resources 仍未完成，因此不能标稳定。 |
| 长期 Memory 可维护性 | 部分可用 | 当前工作树保留每个 owner 的 `memory/long_term/memory.jsonl` 为唯一 active 事实源，`remember` 单工具支持稳定 ID 的 add/list/replace/remove/batch、版本、来源、typed kind 与过期；锁内重读和原子整批提交避免并发丢写，daily mirror 记录同一 operation。SQLite/FTS 与向量索引返回前按 JSONL active state 过滤，删除/旧版本不能从陈旧索引复活。40 writer 并发、batch 回滚、expiry、route 冲突/缺文件、损坏恢复包、Gateway/subagent/doctor 联合恢复、600 条 MiniMax 长链、连续 8 次真实 compact 和双飞书 owner 隔离均已验证。写入是同步 write-through，没有待 flush 的外部 provider 队列，因此不复制 长期助手 的 pre-compact flush；未接生产链且会绕过统一入口的 HOT/lesson 写 helper 已删除。保持部分可用只因为长期召回质量、数据增长和十万 owner 规模尚无持续运营证明，不再把第二套记忆整理器列为底座缺口。 |
| Persona 人格与用户画像 | 部分可用 | 当前工作树由唯一 `PersonaRepository` 统一 SOUL/USER/AGENTS 的逐轮加载与变更；owner 边界、regular-file/symlink、UTF-8、2 MiB、逐行威胁扫描和 prompt budget 形成结构化诊断。USER 仍须锚定当前用户原话，SOUL/AGENTS 仍需确认；版本 snapshot、history、CAS、rollback 和飞书确认期间 SHA 冲突保护已接主链。32 writer 并发、恶意行不进 prompt/list、确认幂等/CAS、user A/B/group 路径反证、MiniMax 长链和两个真实飞书 owner 的写入/召回/清理已通过；保持部分可用只因为默认人格内容属于持续产品迭代，群组长期使用和十万 owner 规模尚未证明。 |
| Owner 配额、隐私与自动清理 | 部分可用 | 当前工作树在创建 child 前把 `max_active_agents` 与子代理各级容量取严格交集；权威状态不可读时整批 fail-closed。文件 write/edit/patch、Memory 权威源和 daily mirror、Persona 正文/backup/version ledger、Scheduler store/history 与 Skill draft 共用一个 owner quota lock，固定锁序为 `quota -> repository/file lock -> mutation`，并发和多文件写按整批最终字节准入。Gateway 以 cursor 有界扫描 owner，按结构化 terminal status/timestamp 清 task 与 child scratch，执行前二次校验，先写 trash tombstone 再按期限删除，并支持 owner/task legal hold 与 audit；策略损坏时跳过。聚焦回归和完整本地 CI 通过。该应用门无法绝对拦截 Shell/PTY/LSP 任意进程写盘或 SQLite/向量派生页增长，十万用户正式部署还必须启用 filesystem/project/container quota；1.10 和双 owner 真测未完成。 |
| 用户级 Scheduler 定时与提醒 | 部分可用 | 每个 owner 的 `data/scheduler/` 仍是唯一 job/run 事实源；全局 SQLite 只投影 owner 身份、最早到期时间和短租约，使 Gateway 无需轮扫所有 owner 即可叫醒到期主体，执行前仍回到 owner JSON 账本重新校验。唯一 typed `schedule` action tool 支持 at/every/cron、相对秒数固化为绝对时刻、时区、CRUD、暂停/恢复、立即运行、历史与状态；`wait` 被固定为当前 active task 的内部 yield，不能由隐藏参数升级为用户通知。1.10 已证明 135 个 owner 场景从旧轮扫约 159 秒延迟降为重启后 2.18 秒、常驻时 0.44/5.67 秒发现到期 owner；真实 Feishu owner 的自动最终回复和显式主动消息两条路径均只产生一个历史 run、一次出站和一条最终 transcript。主动消息路径使用 `send_message` 的结构化送达回执跳过后台兜底，未从正文或日志猜测。当前仍缺双真实平台用户、周期任务长期运行和规模化故障切换，因此保持部分可用。 |
| 可复用 Workflow | 部分可用 | 当前工作树按 会话运行时/模型助手 Code 的公开代码路径收敛为 `Skill + 当前 task_progress + 原生 tools/subagents`：Skill 提供方法，当前 thread 保存计划，模型显式创建和派发子代理。旧 `subagent_workflows` 包、JSON 模板、mode/config/CLI、extension hook、shared workflow index 和自动 phase/router 已删除，旧持久字段只作为未知字段忽略。编排/配置/CLI/Skill 继承聚焦回归和完整本地 CI 通过；发布和 1.10 多长任务真测未完成。 |
| 被动验证证据 | 部分可用 | 当前工作树在主/子代理共用工具出口按结构化 root task 被动记录真实规范命令、cwd/root、exit 和 targeted/full；每个 owner 的事实源固定为自己的 `data/verification/evidence.sqlite3`。文件工具成功修改后旧证据变 stale，任意/链式命令、失败写入和自然语言不能改变状态，也没有恢复普通任务目录验收器或固定收口模板。聚焦回归和完整本地 CI 通过；发布和 1.10 真实代码长任务尚未完成。 |
| `/goal` 持续目标与 `/audit` 特殊模式 | 部分可用 | `/goal`、`/audit` 都是同一 conversation thread 上的持久 overlay，不建立第二份 transcript、Memory 或 Compact。旧未命名 `/goal <目标>` 保持单目标兼容；Audit 已收敛为 `/audit help`、`/audit <名称> prepare <内容>`、`/audit <时长> <名称> <任务>`、`/audit <名称> status|clear`，旧未命名 Audit 入口已删除。Audit 名称在 owner/thread 内精确且区分大小写，稳定 `audit_id` 才是执行权威；每项使用 owner 的 `audits/<audit_id>/`，不与普通 `tasks/` 或其他 Audit 混放。prepare 只是一次带准确 Audit scope 的普通 Agent turn，下一条普通消息不继承；`pending_prompt`、生效要求和验证引用分开，只有当前 prepare scope 的结构化发布工具能更新准确 Audit，失败/越界证据保持旧配置，已领取完整批次和 redelivery 用旧要求结束后下一批才切换。底座不再要求固定来源说明模板；主 Agent 可按现场需要在普通 Audit 工作区写脚本、Skill、字段文档或说明，`source_profile_ref` 仅是可选真实引用。来源发布用显式 `upsert/replace` 决定追加更新或完整替换，不从自然语言猜；完整替换会停止已移除来源而保留账本。prepare 探针身份包含准确 prepare 请求和机械接入事实，同轮相同配置幂等，不同轮或接入变化不会覆盖旧证据。`/status` 只列用户可见名称、状态和时长；单项 status 不显示内部 ID、版本或绝对路径；clear 与 `stop_named_work` 最终仍按 task ID 落账。普通 `/stop` 只打断当前窗口。2026-08-04 当前工作树的 prepare、来源接入、来源替换、来源工作者、重启与不漏账聚焦回归已通过。MiniMax-M2.7 真实 CLI 此前完成 A/B 交叉 prepare、问答不发布、比较不发布、显式 revision 1、普通聊天/普通文件任务隔离、精确 clear 与大小写反证。真实验收发现并修复通用 workspace archive 把 prepare 草稿回填为生效 goal 的旁路。macOS 无 bwrap 的 owner shell 先按设计 fail-closed；隔离测试 home 使用 30 分钟管理员授权后由 PTY 真正运行探针，读回 4 条/605 字节/偏移 `0/138/281/460`，随后授权和临时 Gateway 已清理。当前来源模板收敛候选尚未部署 1.10，也未做真实 Feishu、长时间多 Audit、故障恢复或十万 owner 容量证明，因此保持部分可用。 |
| Gateway、持久请求、lease/recovery | 部分可用 | 普通用户默认 gateway 仍是本地文件事实源；无限 watch 未收到 stop 却自行返回时记录明确 termination reason 并以非零码失败，计划停止、有限轮完成和清理 drain 分开记账。scale profile 另有 PostgreSQL SKIP-LOCKED 队列、Redis 跨副本准入/租约和真实 Agent worker。目标集群故障切换与容量仍未验证。 |
| 子代理、任务账本、会话 compact/resume | 部分可用 | 有正式运行链和大量回归；创建记录、调度接收、runner 实际运行三层事实分开，公开回执不再把 accepted 虚报为 running。模型按真实独立工作项自主决定数量：单个 `goal` 只建一个 child，多个 child 必须用不同的 `items` 明确拆分，重复项或超出本批/任务/owner/全局容量都会整批拒绝；模型入口不再提供 `count` 克隆，管理员低层 CLI 不在此范围。主代理是唯一用户聚合出口，子代理内部评论、命令和协议不入 transcript；子代理结果、artifact refs 和能力请求只作为结构化事实交给主代理判断与汇总。子代理账本读取错误现为 fail-closed：无法证明整棵任务树已终结时只保留内部整合，只有精确根任务链接已持久化为 `completed` 才允许最终回复；该边界已在 1.10 第二 owner 的真实损坏历史 child 记录下反证，未提前发送。普通任务与 会话运行时 一样由模型基于真实工具和测试事实给出自然最终回复，不再经过目录扫描验收器、完成 marker 或 `submit_for_acceptance`。根任务不再生成 task compact/rollup package；主代理维护唯一 thread history，每个独立子代理则在自己的 run workspace 调用同一套通用 Compact，并只保留通用 checkpoint/state/summary 与原始运行证据，不再有子代理专属 Compact、子代理专属 session packet、恢复目录或独立阈值。恢复时当前 task 的结构化 goal/next action 高于旧归档和工具游标；工具 coverage/cursor 仍保留为事实，只有显式 `full_source_read` 合同才可把未完成游标提升为下一动作。相关 151 项回归与 1.10 既有 child 的 9 次压力 Compact、1 次候选 apply 复核均通过，最新 work-state 没有被旧 通道运行时/LangChain 读取路线带偏。当前工作树已收敛 compact 百分比语义：配置值直接换算 active-context token 边界，不预留未来 `max_tokens`，也不再由工具 digest 绕到硬编码 95%；provider usage 优先，本地估算兜底。不执行 LLM 的独立孤儿回收、分页 owner 发现、in-process 取消边界与父 conversation lifecycle 门已随 `acb1cfc5` 通过 CI 并部署；1.10 重启反证覆盖 closed cancel、active resume 和 corrupt/missing hold。dead-worker reclaim 只重启 `starting/running` 且失去心跳的 runner，`completed/failed` 等显式终态不因 Gateway 重启被重放；`80a0527d` 已通过选中 8,003 项且退出码为 0 的本地 fast suite、三组远端 CI 并部署 1.10，启动与周期恢复扫描均未重放真实遗留 completed runner。post-deploy A/B 分别只创建 5/3 个 child，B `/stop` 后三者取消且续作未重复创建。完成质量、长期并发和十万 owner 恢复时延仍不作规模承诺。GitHub API、PyPI、npm 三路真实保证档已完成单一连续段超过 24 小时的逐拍 proof。 |
| MCP stdio 工具 | 实验性 | 未声明工具默认 `dangerous` 并进入统一 effect/幂等/审批门，只有部署配置可逐工具声明更低 effect。本地 Qwen 已驱动 `@modelcontextprotocol/server-filesystem` 完成 bwrap/stdio 握手、14 工具发现和只读调用，写工具在 client call 前仍被审批门阻断。availability 查询保持无副作用；首次启动失败的合法配置会保留，已退出进程只在下一 run 固定快照前按 1–60 秒退避重连。重连由 client lifecycle lock 和 registry prepare lock 串行，握手后原子发布该 server 的精确新目录，删除旧 proxy 而保留 builtin/其他 server；当前 run 不热扩权，掉线调用仍在实现前 fail-closed。真实多 server、长稳和网络型 MCP 兼容矩阵仍不足，因此保持实验性。 |
| 工具检索、权限范围与运行可用性 | 部分可用 | 关键词与真实 embedding 语义通道共用混合检索器，未配置 embedding 时不会伪装语义可用。按 会话运行时 `StepContext/ToolRouter` 与 长期助手 `check_fn/session toolset/scoped Tool Search` 收敛后，每个 run 的工具面固定为 `注册工具 ∩ owner policy ∩ allowed_tools ∩ availability`；目录、推荐、原生 Schema、`list_tools`、`tool_search`、`list_capabilities` 和最终执行共用同一不可变快照，搜索只能继续减法。执行前实时复检，快照后掉线以 `TOOL_UNAVAILABLE` 停在实现前；视觉/LSP/浏览器/MCP availability 检查不得启动资源或发网络请求。旧 `granted_capabilities` 无消费者链已删除，扩权只认结构化 policy/allowlist/grant。普通 CLI、本地 8899、MiniMax-M2.7、真实平台 A 请求和正式 Feishu-scope A/B 并发工具账本均已验证；跨 owner 读取被拒且未发现额外业务副作用。主流 MCP/浏览器/LSP 组合和十万用户长稳仍未证明。 |
| ASGI、SQLAlchemy/PostgreSQL、RLS scale profile | 部分可用 | scale worker 已要求 PG/RLS owner manifest + versioned S3 objects，Pod 只用 emptyDir；无 S3/bucket versioning 时 fail-closed，真 PG+MinIO API 已验。目标 Kubernetes context 当前不存在，尚未做真实集群灰度。 |
| 扩展插件加载 | 实验性 | 唯一加载链接受管理员显式配置的已安装模块或 `my_agent.plugins` entry point；不扫描用户可写目录，缺失/重复/注册失败会阻断启动。尚缺第三方生态兼容矩阵。 |
| PTY 交互终端 | 部分可用 | POSIX 已有真实 PTY start/write/read/close、增量游标与有界缓冲，并复用 shell 路径、命令策略和 bwrap。当前 wheel 已在 1.9/1.10 由本地 Qwen 经 4000 真实完成 Python REPL 五/八轮操作并 close；Windows ConPTY 尚未实现。 |
| LSP | 实验性 | 已有管理员配置、惰性 stdio server、initialize/request/didOpen/diagnostics/shutdown 全链；路径限于工作区，多用户 server 经 bwrap。当前工作树在 `lsp_servers` 为空时不再把 LSP Schema 发给模型，availability 检查本身不启动 language server；有配置后仍只在真实 action 时惰性启动。1.10 已用主流 `typescript-language-server 5.3.0 + TypeScript 5.9.3` 返回 TS2322/hover，并由本地 Qwen 两轮真实 LSP 工具账本复验；其他语言服务器兼容矩阵与长稳仍未验证。 |
| OpenAI 原生工具调用 | 实验性 | OpenAI-compatible `tools/tool_calls/role=tool` 的非流式和 SSE 分片链已接入 canonical ToolCall/ToolResult；坏参数、截断和正文伪调用均在 handler 前结构化拒绝。本地 Qwen 已完成三轮真实文件工具调用，但尚未扩大 provider/model、streaming 和长稳矩阵。 |
| Redis、OpenTelemetry、在线迁移 | 部分可用 | Redis Lua、OTLP exporter、迁移 1–10 和应用只读版本门已接主链；release channel、Gateway API canary、分析门、PG backup/隔离 restore 清单已存在。尚无目标集群 HA、collector 后端、真实流量灰度和灾备演练。 |
| 十万用户以上容量与可靠性证明 | 仅设计 | 已有一次三路真实异构来源连续 24 小时保证 proof，但尚无正式容量模型、SLO、十万用户压测、故障演练和多周期运行证据；该 proof 不能外推为容量证明。 |

## P0 已完成的冻结范围

P0 期间停止扩展新功能，只允许修复以下收敛项；该范围已经完成并进入远程 `main`：

1. 本事实页保持唯一权威，并同步 README、设计账本和测试入口。
2. 根目录 pytest 可直接运行；当前已知失败与默认配置漂移清零。
3. `ruff check agent_py_agent scripts` 清零；门禁报告必须区分 blocker 与 advisory。
4. MCP 工具不能伪装成只读绕过 effect gate。
5. 多用户 sandbox 不可用必须 fail-closed，不允许用户可见审批或宿主 fallback。
6. clean-package 必须发现未跟踪运行数据和真实制品污染。

P0 只说明当前底线可信，不说明十万用户规模已完成。P1 已进入远程 `main`；P2 的正式 scale 配置、
Redis、OTel、在线迁移和 RLS 主链已落地，单一连续段 24 小时真实异构来源 proof 已完成；集群灰度、
容量/SLO、灾备演练与多周期长稳仍未完成。

### 2026-07-09 P0 验收快照

| 项目 | 结果 | 证据边界 |
| --- | --- | --- |
| 产品事实页 | 完成 | 本页已接入 README、设计账本和代码树。 |
| 根目录 pytest 与 4 个历史失败 | 完成 | 根目录全量通过；配置漂移、offline gate 误判、cwd 路径依赖、孤儿进程身份误判均已闭环。 |
| Ruff | 完成 | 正式 CI 范围 `agent_py_agent scripts` 为 0。 |
| 门禁报告 | 完成 | code-size 的 high-risk/soft 继续如实展示；只有权威 `blocked` 决策会阻断 offline matrix。 |
| MCP effect 绕过 | 完成 | 未声明 MCP 工具默认 dangerous；无 approval/幂等绑定时在实际 client call 前阻断。 |
| 多用户 sandbox | 完成（P0 范围） | owner-scoped shell 没有宿主 fallback；bwrap probe 不通过即 worker fail-closed。目标 K8s 集群规模验收仍属后续部署工作。 |
| clean-package | 完成 | 当前未跟踪 `data/` 被正确阻断并报告大目录；实际构建 wheel 的 artifact 检查通过。用户运行数据未删除。 |

该 P0 快照已由 commit `b8927837` 推送到远程 `main`。真实工作树 clean-package 仍会因
`data/` 运行数据正确失败；应继续把运行数据排除在制品/Docker context 外，不能删数据换绿。

### 2026-07-09 P1 验收

| 项目 | 当前结果 | 证据边界 |
| --- | --- | --- |
| import 边界 | 已接主门禁 | 全部分层矩阵进入 CI；28 条既有反向依赖按精确 source/target 冻结，新增即失败，旧债未伪装成清零。 |
| 生产包剥离 | 已验证 | 真实 wheel 成员 1828→974，tests 801→0、offline contract 21→0、`real_e2e_commands` 不发布；脏 `build/lib` 注入复验仍为 0。 |
| 默认入口 / gateway | 已验证 | 无子命令 `my-agent` 仍是正式入口；`chat` 默认 gateway，只有显式 `--direct` 前台直连。 |
| 插件链 | 已验证 | 显式 module/entrypoint 发现、固定注册顺序、重复/缺失/注册失败 fail-closed；不扫描 `extensions_dir` 执行代码。 |
| 语义工具检索 | 已验证（确定性） | 真实 EmbeddingProvider 调用、缓存、cosine 与可观测降级均有测试；未在本轮调用付费外部 embedding provider。 |
| PTY / LSP | 已验证（本机协议） | 真实 PTY REPL 和真实 stdio JSON-RPC fake language server 全链通过；不是 Windows/主流 server 兼容声明。 |
| OpenAI native tools | 已验证（协议） | 非流式、SSE 参数分片、IR 历史、schema/消息转换和坏 JSON 截断均通过；未在本轮调用真实付费模型。 |
| 完整发布门 | 已通过（源码/制品） | 最终 fast/slow pytest 与架构守卫 100% 通过；Ruff、compileall、import boundary、doc-sync、offline matrix、git diff 和真实 wheel 两道制品门均通过。当前 strict code-size 为 `hard=0 / high-risk=170 / soft=61 / blocked=False`，全部非阻断项受冻结基线约束。 |

P1 已由 commit `82b287b0` 推送到远程 `main`。

### 2026-07-10 P2 当前工作树验收

| 项目 | 当前结果 | 证据边界 |
| --- | --- | --- |
| scale fail-closed | 已接线 | 缺 PG/Redis/OTLP/app role/tenant/真实 handler+downstream/持久路径或回复凭据会在领取消息前失败。 |
| PG / RLS / migration | 本机真依赖通过 | 第一轮真 PG 迁移 1–6；第二轮实现扩到 1–10，owner manifest 同样 FORCE RLS，跨租户直查为 0。 |
| Redis | 本机真依赖通过 | Redis 7 容器中两个独立 client 共享限流、预算、全局并发租约；不是 Redis Cluster/故障切换证明。 |
| OTel | 本机真 collector 通过 | OTLP HTTP protobuf 可解析，span 与 W3C trace id 一致；不是生产 Tempo/Jaeger 后端可用性证明。 |
| 真实 worker | 已接线 | 内置 owner-scoped Agent 下游与飞书原消息回复；当前工作树在回复前提交 PG/RLS + S3 owner 快照，RWX 回退被配置门禁止。 |
| 连续监控 proof | 已证明（单一连续段） | 2026-07-11T09:11:17Z 至 2026-07-12T09:27:49Z 的 87,391 秒连续段逐拍满足三路真实保证源、900 秒 freshness、至少两个异构签名和 120 秒采样 gap；独立复核为 2,879 拍、3 路/3 签名、最大 freshness 451.773 秒、最大段内 gap 31.381 秒。 |
| 十万用户 | 未证明 | 没有容量压测、SLO、故障演练、成本模型实测；状态仍为仅设计。 |

P2 实现已由 commit `6c00bf18` 提交；本发布事实同步随后一并推送到远程 `main`。

### 2026-07-10 P2 第二轮启动验收

- Kubernetes：stable/canary release channel 已进入队列事实，Gateway API 初始权重为 0，分析 Job
  检查 canary readiness、失败行与陈旧积压；本机没有 kube context，未应用到目标集群。
- 灾备：新增小时 PG dump、版本化 S3 owner objects 和隔离 restore Job；尚未在目标账户执行恢复演练。
- Owner 存储：worker 的 RWX PVC 已替换为 emptyDir cache + PG/RLS manifest + S3；真 PG/MinIO API
  两文件恢复和跨租户 0 行通过。monitor watch/proof 仍使用 RWO 状态卷。
- 连续 proof：已修复真实 home 深递归、harvester sidecar 重复计数、历史成功冒充健康及旧 evaluator
  跨失格区间累计时长等失真；修正后的新连续段已超过 86,400 秒并独立逐拍复核通过。

本节实现已由 commit `7b6146a3` 提交；本发布事实同步随后一并推送到远程 `main`。最终 24 小时
proof 的事实见下方 2026-07-12 收口快照。

### 2026-07-12 修正后 24 小时 proof 收口

- 公共 summary 在 `continuous_seconds=87391` 时给出 `proven=true / reason=ok`。
- 独立扫描完整 NDJSON 共 5,768 拍、零 malformed；最后一次失格后只取当前连续段，共 2,879 拍，
  时间为 2026-07-11T09:11:17Z 至 2026-07-12T09:27:49Z，持续 87,391.407 秒。
- 当前连续段每一拍最少 3 路健康保证源、最少 3 个异构签名；所有健康源最大 freshness
  451.773 秒，段内最大采样 gap 31.381 秒，分别低于 900 秒和 120 秒硬门。
- 收口时 monitor PID 55241 已连续运行超过 24 小时，stderr 仍为 23,791 bytes，mtime 保持
  2026-07-10T07:04:09-0700；1.9 gateway 为 active，1.10 gateway 按计划保持 inactive。
- 该结果只证明这一个三路真实异构来源连续段满足保证合同，不证明十万用户容量、Kubernetes HA、
  多周期长稳、真实流量灰度或灾备恢复。

### 2026-07-21 正式飞书单运行面与 10 owner 上下文反证

- 1.10 已收敛为唯一正式 `my-agent-gateway.service`（`127.0.0.1:8420`）和唯一正式
  `my-agent-feishu.service`（飞书长连接）；没有隔离 Gateway、额外飞书适配器或 `8421`–`8423` 监听。
  本轮部署后两项服务 active、`NRestarts=0`，WebSocket 已连接。测试身份为 Feishu-scoped 合成 owner，
  走正式 owner/channel/conversation 主链，但不冒充十个真实平台账号入站。
- 10 个全新 owner 各在唯一 conversation 保存不同校验词。第一轮 10/10 回复自己的词、无跨 owner 内容；
  本地 Qwen 在 8 个推理槽下耗时 494–569 秒。模型同时违背“不要当长期偏好”的明确要求，10/10 误调用
  `remember` 写入 owner 长期记忆；每条只落自己的 owner，因此这是模型工具选择失败，不是隔离成功即可
  掩盖的行为。底座没有增加关键词拦截或 prompt 特判。
- 第二轮 10/10 召回自己的词且 `used_memories=1`。随后通过项目自己的版本化 Memory remove 接口给十条
  合成记忆写 tombstone，权威 JSONL 的 active 记录均为 0；第三轮仍沿原 thread 召回，10/10 没有串词且
  `used_memories=0`。其中 9/10 逐字一致，1 条多出一个空格，记录为本地模型格式质量失败。该三轮只证明
  当前单机同 thread 续接和 owner 隔离，不外推为群聊、规模容量或所有模型质量证明。
- 普通工具面曾让本地模型在“创建三个子代理”任务中误调用 `create_skill`。代码级对照 会话运行时 的
  snapshot/load 和 模型助手 Code 的显式 Skill 创建工作流后，普通 `create_skill` 工具与测试已删除；Skill
  发现/读取及独立的候选学习/用户确认链保留。供应商额度耗尽同时补入正式
  `PROVIDER_QUOTA_EXHAUSTED` 错误合同，不再退化为 `UNKNOWN_ERROR`。
- 完整本地 pytest 抓到 `task_local` 子代理误进入主代理用户回复阶段：结构化 `SUBAGENT_RESULT` 被普通
  正文替换后，外层会无限等待并重复本地 compact。候选按现有 `context_scope` 分离 child 与 root 用户
  出口；对照的是 会话运行时 显式 child session/`parent_thread_id`，没有按模型文字或代理名猜身份。原回归现
  以预期 5 次 backend 调用结束，完整 pytest 已运行到 100% 并通过。
- 工具轮窗口也从“只限制工具历史”收敛为“限制整个模型可见输入”，覆盖任务正文、Persona、工具目录、
  原生 tool messages 和工具历史。先前 `40054 > 40000` token 的真实测试已通过；旧记录仍在 raw archive，
  只是从当前 provider 输入回收最老工具对，不产生第二份会话或 compact 账本。
- 当前候选 wheel SHA-256 为 `739b330d0bd376882f753a2814fd314e4e625f18306eabe74b8a98b6436019f4`；
  distribution boundary 与 clean-package artifact 均为 `ok=true`。1.10 已安装该 wheel，部署后仍只有
  正式 Gateway/Feishu 两项服务和 `8420` 监听，二者 active、`NRestarts=0`、WebSocket 已连接。模型暂时
  保持正式服务内的本地 8899；MiniMax 未刷新时也由本地模型继续编排与双长任务验证，不允许空等。只有
  当前无 live request、到达配置刷新点且 MiniMax 探活成功后，才在同一运行面安全切回。当前工作树尚未
  最终提交或推送。
- 本地 Qwen 随后在同一正式入口完成精确三子代理真测：只创建 3 个 child，最终 3/3 为
  `DONE/VERIFIED`，三个要求的 Markdown 文件存在、非空且 SHA-256 各不相同；主代理约 34 分钟后才在
  三者全部终态时自然汇总，未把 child 命令或协议写进 transcript。模型在等待期间反复误用 shell `sleep`
  并让一个资料任务过度搜索，因此该时延只记为 fallback 模型质量事实，不升级为底座性能承诺。
- 运行中的 `/btw` 只进入同一 task/thread 并消费一次。旧 provider 流恰在该窗口返回空内容时，已部署版本
  会把前台 request 记为 `MODEL_EMPTY_RESPONSE`，虽然同一 durable task 随即由 background claim 正确接管
  并完成。当前候选按 会话运行时 active-turn input queue 语义补齐这一窄窗：只根据 pending typed turn input
  判断旧空响应已过期，在安全点注入后重试原 turn；成功模型响应前 guidance 仍可恢复重放。聚焦三套回归
  通过，尚待重建 wheel 和 1.10 复验。

### 2026-07-18 Agent 基础能力发布与双 owner 真模型验收

- `5da7e21e` 已推送远端 `main` 并精确部署到 1.10；干净 wheel SHA-256 为
  `ae24bbd1ab149ae3f097e480080f59231aadd55ccf0d1f38138fec0a94e591e0`。Gateway/Feishu
  active、`NRestarts=0`，模型为 MiniMax-M2.7，1.9 未改动。该提交包含通道四层能力事实、唯一 Skill
  snapshot、Memory 版本化 CRUD、Persona repository/CAS、owner Scheduler、单一 Workflow 组合方式、
  被动验证证据以及 owner quota/retention。
- 两个新 Feishu-scoped 合成 owner 各在唯一 thread 保存自己的 Persona/Memory 后执行长代码任务。
  A 自主创建 3 个 child 完成 `event-lens`，B 自主创建 4 个 child 完成 `tree-sync`；各 3 条 `/btw` 进入
  同一持久 task。B 长任务仍在运行时可以继续普通聊天，任务没有被聊天重启。A/B 的称呼、回答偏好、
  暗号、长期项目事实、产物、Persona、USER、Skill 和 Memory 未发现交叉读取。该测试走 Feishu owner/
  channel/conversation Gateway 主链，但使用合成 open_id/chat_id，不冒充平台客户端真实入站或收件。
- 独立外部验收再次证明模型自报不能当完成证据。B 首次交付在 macOS 暴露路径别名、重复入口和缓存；
  原 task 修正后 67/67 通过且干净。A 首次交付含两套解析/检测逻辑；原 task 第一次修正后又被外部时区
  样例证明 offset 换算错误，第二次修正后 41/41、弃用警告当错误、JSON/CSV/Markdown、坏输入、六类稳定
  原因码、去重、三类汇总、Top 5 及同一时刻跨 offset 比较通过；最后只在原 task 清理缓存，远端最终
  交付 18 个目录/文件，零 symlink、`.pytest_cache`、`__pycache__`、pyc/pyo 或 egg-info。
- 真任务同时暴露 5 个通用底座问题：owner quota 扫描原子消失竞态、capability grant 后 child link 未恢复、
  `raise_event` 用 root 查 child lineage、子代理状态重叠计数、内部 findings ledger 被自动拼进用户正文。
  当前候选均使用结构化 ID/status/CAS/文件错误类型修复，并删除旧 findings 自动出站整条死路径；没有按
  用户中文内容做机器判定，也没有在 Feishu adapter 分叉。相关具体代码参考和候选/发布边界见
  `docs/design/AGENT_FOUNDATION_CAPABILITY_AUDIT_20260718.md`。
- 两个正式 thread 的 compact generation 都是 0；90% 配置在本轮没有达到，因此本轮只证明同一 history
  连续聊天、任务、纠错和 `/btw`，不把它写成新的真实 compact 触发证明。

### 2026-07-18 第二 owner 原任务纠错与最终候选发布

- 1.10 恢复后，沿 Feishu owner `ou_1be7…f921` 与原 conversation 的可信 localhost `/ask` 续接
  “青竹账单”；这是服务器侧 Feishu scope 模拟，不冒充第二次平台客户端入站。显式绝对变更路径在统一
  effect 前门精确选回原 task，空占位被 supersede；两次 `/btw` 作为同一 active turn 的 UserTurn 生效，
  最终只有一个 `qingzhu-bill` 项目。
- 独立复制验收为 55/55，额外覆盖 JSON/CSV/Markdown、月度/商户汇总、坏输入、六类稳定原因码、无逻辑
  重复、无 symlink/缓存/pyc/pyo。A 中青竹项目数为 0、B 中为 1；两 owner 的私有 token、产物、
  SOUL/USER/AGENTS、skills 和 memory 反向检索无交叉命中。
- 本轮最终 wheel SHA-256 为 `bd8f9eb2828f53a27622aeaa17f0596224b3219177861e2d27ce3718c9906ca1`；
  distribution boundary 与 artifact clean-package 通过，精确安装到 1.10 后 Gateway/Feishu active、
  `NRestarts=0`、Feishu WebSocket connected。请求 `req_1784350294487_12075_0` 在 1 个工具轮调用统一
  `send_message`，日志记录 `NATIVE_CHANNEL_SEND_OK channel=feishu attachments=0 mode=proactive`。
- 工作树 clean-package 正确拒绝保留的 845MB 未跟踪 `data/`、未跟踪交接文档及其他运行目录；这些文件
  没有被删除、忽略、提交或装入 wheel。完整 pytest、Ruff、import/offline/code-size/doc-sync/compile 与
  diff 门均在本地完成；本轮按用户要求不运行远端 CI、不 push。

### 2026-07-17 两用户长任务、当前任务引导与独立产物验收

- 测试环境是 1.10、MiniMax M2.7，以及两组稳定且彼此隔离的 Feishu-scoped 合成身份。请求经 Gateway
  `/ask` 进入与 Feishu adapter 相同的 owner/channel/conversation 主链；没有伪装成 Feishu 平台客户端
  入站或引用回复验收。1.9 全程未改动。
- A 用户完成 Hyperfine 的一次性 `/goal` 复刻、分步 Zoxide 复刻、8 个项目深度对比、SL 与 Pastel
  两个协作任务；B 用户完成 Tokei 的一次性 `/goal` 复刻、分步 Navi 复刻、9 个项目深度对比、
  Tealdeer 与 Miniserve 两个协作任务。A、B 的口令、thread、workspace、task root、产物和搜索结果保持
  owner 隔离；B 长任务运行期间，A 的普通追问在 9.417 秒完成并正确沿用 A 自己的上下文。
- 全矩阵共选取 17 个不同的 `/btw` 输入：3 个落在 request 执行轮，14 个落在持久 task。17 个均只确认
  一次、待投递数最终为 0，并以真实 UserTurn 保存在同一 thread transcript 的准确工具往返位置。原始
  `guidance/*.jsonl` 是不可变追加记录，实际确认状态由独立 `guidance_delivered.json` 保存；不能用原始
  记录里的初始 `delivered_at=0` 误判为丢失。
- Miniserve 后续修复继续使用原 task `req_1784264255535_1355192_2`、原 thread 和原 workspace；没有建立
  第二个任务，也没有额外创建子代理。首次独立黑盒验收为 54/58，真实暴露 3 类上传残留和 1 个 WebDAV
  符号链接泄漏；同一任务修复后，从干净 wheel 新装并经真实 HTTP/HTTPS/TLS/WebDAV/上传路径重跑为
  58/58。wheel SHA-256 为 `444132b2177cd7556556e8c7a0f3639c5d818b5a09cda8d861f50c6f75a25962`；
  官方源码与只读快照验收前后均为 28 个文件，指纹保持
  `6bff9669dc47d384fe54a512a473a0177006f7ce5b9fe3d90f8afb5ba95497eb`，且没有构建缓存或临时上传残留。
- 本轮没有把独立验收器加入 my-agent 运行时，也没有恢复已删除的目录扫描验收/closeout 子系统。验收是
  用户外部黑盒复核；普通任务仍由模型基于真实工具、测试和子代理事实自然结束。
- 本轮 90% 正式 compact 阈值没有触发主 thread compact，因此不能拿该矩阵声称“本轮再次证明 compact”。
  主会话 compact 由既有 50% 真机证据与当前聚焦回归单独证明；根 task 不再拥有第二份 compact/history。
- 本轮代码边界复核固定在 会话运行时 `03bb3b12367397e14a8facc2e018d645ff4d8e83`、通道运行时
  `f2a46b0661206a0b7264ad05749e2304fbfe6a61`、长期助手
  `7d0246ab5715e9e18e156eb08912f4e24bd8d175`。这些引用只用于控制、会话和子代理边界对照；my-agent
  仍使用自己的 owner-scoped 文件事实源。
- 发布前只读检查发现 1.10 留有多日前的非终态子代理投影；旧孤儿恢复器只看 child status/session，可能在
  Gateway 重启后复活父任务已经 completed/interrupted 的旧 run。当前候选把 conversation parent link 与
  run link 接到 auto-start、dispatch runner selection、watch takeover 和 orphan reclaim 的同一结构化门：
  两个链接均 active 才能恢复；父/子链接终态或 interrupted 时旧 run 走与 `/stop` 共用的取消链；链接缺失、
  损坏、跨 thread 或未知状态时 fail-closed，只保留账本等待明确修复。普通无 conversation scope 的本地任务
  保持既有恢复行为。`acb1cfc5` 已通过远端 CI 并部署 1.10；重启反证确认 closed parent 取消、exact active
  child 恢复、missing/corrupt link hold，Gateway/Feishu 零重启且 1.9 未改动。

### 2026-07-17 `acb1cfc5` 部署后 A/B 停止续作与独立验收

- 1.10、MiniMax M2.7 上使用两个新的 Feishu-scoped 合成身份。A/B 各只有一个 thread、一个 task root 和
  一个任务目录；A 创建 5 个不同 child 并全部 `DONE`，B 创建 3 个 child 后经 `/stop` 全部 `cancelled`。
  B 随后用普通自然语言补充并继续原 `tree-diff` task，未新建 task/workspace/child；A 的两轮返修同样复用
  原 `log-lens` task。长任务让出期间的普通聊天分别约 4 秒和 10 秒完成并只召回各自口令。
- A 2 条、B 3 条 `/btw` 在 `guidance_delivered.json` 与 transcript 中均各出现一次，零 pending；两 owner
  的口令、ID、产物与 symlink 扫描零交叉。两个 root link 最终 `completed`、work state 均 `DONE`、所有
  progress policy disabled，Gateway pending/processing 为 0。
- A 的干净副本新装后 119/119 测试通过，额外验证 ISO/Unix 秒/毫秒边界、Top 5 和 `unknown`；正式项目
  零缓存、临时脚本、checkpoint、旧存根和 symlink。B 的干净副本新装后 64/64 测试通过，11 项额外断言
  覆盖同大小二进制内容、相对/文件名 ignore、最后规则优先、CLI、plan reason 和中文 README；`output/`
  零缓存、旧副本和 symlink。首次自报均遗漏过真实缺陷，修复通过普通用户消息送回同一 task；独立验收
  没有进入 my-agent 运行时，也没有恢复目录扫描验收硬门。
- 本轮正式 90% 阈值未触发 compact（两 thread generation 均为 0），因此只证明 single-history 续作，不把
  它冒充新的真机 compact 压力证明。请求走 Feishu-scoped Gateway 主链但不是 Feishu 平台真实客户端入站。
- 部署态曾暴露等待回执事实不足：A 要求用户“继续等待进一步指示”，B 没说正在做什么。通用修复把有界
  当前请求、动作/轮次、当前 `/btw` 和精确子代理统计交给同一个无工具模型表达轮，并明确任务会自行继续；
  不增加固定回执，不在 IM adapter 分叉，不从自然语言判断机器状态。`a7d6044e` 已通过 60 项聚焦回归、
  8,057 项完整 pytest、Ruff/import/offline/code-size/doc-sync/compile、2.62MB wheel clean-package 与
  distribution boundary，三组远端 CI 全绿后精确部署 1.10。新 Feishu-scoped 合成用户的真模型回执在
  27.563 秒内列出两个 child 的工作并承诺自动继续；两个 child 均 `DONE` 后主代理自动整合、连续修复真实
  测试失败并最终报告 23/23，独立干净副本复跑同为 23/23，根 task 为 `completed`、同一 transcript 只有
  user、interim assistant、final assistant 三条。该身份无法从 Feishu 平台真实收件，不能把服务器侧投影
  冒充客户端投递证明。

### 2026-07-13 普通飞书对话与工作链发布收口

- 普通用户不需要触发词：聊天、做事、派工和定时都先进入同一条常规对话链，由模型按自然语言
  选择工具。只有显式 `/audit`、`/goal` 保留特殊模式；其当前语义以上方能力矩阵和本轮设计文档为准。
- 飞书入站把真实 `chat_id` 作为会话 ID，话题消息再叠加 `thread_id/root_id`；owner 用户身份仍是
  独立隔离维度。网关每轮先读同一会话最近的 user/assistant 历史，当前消息保持独立的
  `# User Task`，回答后将本轮双方消息写回；不会把旧任务目标包进当前消息。
- 网关准入新增同会话单飞：同一用户同一会话一次只执行一条，后一条必须等前一条回答落库；
  同一用户的不同会话仍受每用户/全局上限并行。
- per-user owner 默认开启；远程身份缺失或 owner agent 创建失败时以
  `OWNER_SCOPE_UNAVAILABLE` 终态拒绝，不会回退共享 main owner 串户。
- 普通对话只有在真实调用文件、执行、`task_progress`、`create_subagents`、`wait` 等带
  `promotes_task` 的工作工具时，才在内部建立或激活 task/workspace 运行记录；用户无需知道内部 task id。
  thread 以 `workspace_task_id` 记住最近使用的根工作目录，后续 turn 像 会话运行时 一样继承 cwd。纯聊天
  虽能看到这个目录事实，但不会重开任务、写任务归档或改变生命周期；上一 task 已终态时，第一次工作工具
  会在同一目录建立当前 request 的新 task id，旧 task 保持终态，并记录 `continued_from_task_id`。只有
  持久 `/goal` 的精确暂停任务才按原 task id 恢复。普通用户和模型都不需要 select/start/close 任务；
  当前消息直接决定本轮做什么。若写工具携带同 thread 某个既有任务目录下的精确结构化路径，统一工具入口
  可以无歧义绑定该目录，但这不是自然语言任务判断，也不改变同一会话历史。最终运行事件关闭本轮执行记录，
  但不清空 sticky workspace，也不切换或重建会话历史。
- 会话任务使用两份非竞争索引：`task_ids` 是完整历史事实，供后台策略和审计精确读取；
  `active_task_ids` 只保存普通聊天可见的活跃候选。终态任务从热索引移除但不删除历史链接。子代理
  虽继承父任务的会话引用用于归档产物和进度，但它自己的收口无权关闭父会话任务；关闭入口按
  结构化 run source + 精确 task id 拒绝子任务关闭父链接；若子任务存在与自身 task id 完全相同的
  会话链接，则允许它关闭自己的 DONE 链接；子任务和 `bg-main-*` 内部链接即使处于 active/completed，
  也不会成为普通会话的工作目录。后台自动续跑只服务显式 `/goal`，并携带精确 thread/task link；只有结构化
  任务终态确认后才把该任务从活跃候选移除，
  避免“已经交付却仍被定时器重复做”。模型上下文只注入当前 sticky workspace，不再注入可恢复任务菜单
  或最近完成任务菜单。普通聊天继承 cwd 但不激活旧执行；文件、命令、浏览器、PTY、LSP、派工和 wait 的
  第一个工作入口会复用该目录并建立当前执行身份。内部绑定和权限失败均 fail-closed；代码不解析“继续、
  第二步”等自然语言决定目录或运行身份，也不要求用户输入触发词。
  历史 `ddfd942a` 的 1.10 第三步真测中，B 的回执与后台
  Navi 执行均正确且独立复验 62 项测试通过；A 虽已选择原 Zoxide 工作区并在后台继续，辅助表达轮却因一次
  失败 update 与自身零工具视图错误声称“没有工具”。当前本地候选已改为只认可 `ok=true` 的进度 transition，
  并把成功选择工作区、成功访问运行时作为表达事实；不解析正文、不改变任务状态，待下一 wheel 真机复验。
  若子任务在根任务已 completed/superseded 后才迟到结束，其持久 wake 记录会直接归档，不再唤醒
  旧根任务或把旧任务回复写回当前普通聊天。
- 会话 transcript 是普通多轮的唯一对话事实源：不会再把每轮对话自动写入 owner-global memory。
  旧库中的 dialogue 记录会在检索层排除并先扩量再过滤，不会挤掉 USER preference/lesson。
- 当前工作树已把固定“最近 20 轮”从遗忘边界改成 compact 后的保留尾部：同一 thread 在阈值前注入
  完整未压缩段；到达现有 `memory_compact_auto_trigger_percent` 阈值时，用同一 token 估算和当前模型
  生成 thread summary，原始 JSONL 不删除，thread JSON 原子记录 message+byte cursor/generation；
  首次 compact 后直接从 byte cursor 读取新增尾部，不再每轮重扫旧前缀。旧消息另做
  `conversation_message` 派生索引，只写入当前 owner 的 LocalStore，供既有 `session_search` 召回。
  正式默认与包内 YAML 已统一为 90%；50% 只用于本轮真实压力验证。主运行仍用厂商 usage 与本地
  prompt 估算的较大值保护低报场景，累计账本不参与当前轮触发判断。
- `/verbose off|on|full`（以及 `/v` 查询）按 thread 持久化。Gateway 写 typed 工具事件，USER 只能经
  身份校验读取自己的 `/progress/<request_id>`；已有 delivery worker 按 cursor 回送，不重提任务。
  `on` 只发步骤摘要，`full` 才附脱敏且限长的工具结果，`off` 只保留占位和最终答复。
- 用户消息在调用模型前必须可靠落账，否则 fail-closed；模型已经完成后若 assistant 落账短暂失败，
  真实结果仍先返回并写持久化 repair，下轮幂等补账，避免重跑工具造成重复副作用。
- Gateway、飞书回复与 assistant transcript 共用用户回复投影：内部运行协议不得写入普通聊天；后台报告
  也只返回投影后的模型正文。这样 compact 与旧聊天检索不会再把机器协议混进用户上下文。
- 2026-07-16 双 owner 对比任务真测发现 MiniMax 会在自然回执中降级输出 Markdown
  `tool_call` 代码围栏；旧投影只覆盖 bracket/XML/direct-tag 形态，导致 B 用户看到 `task_progress`
  参数。当前统一出口增加 fenced tool/function call/result/output 清洗，保留围栏前后的自然正文；Gateway、
  transcript 和所有 IM 共用，不在飞书适配器另加关键词表。该边界对照 通道运行时 的
  `sanitizeAssistantVisibleTextWithProfile`/结构化 tool message 分离和 长期助手 的 tool_calls 字段分离；
  聚焦投影回归通过，尚待发布后 1.10 复测。
- 2026-07-17 Tealdeer 复刻真测确认模型已生成自然回执，但旧的 interim 完成语义正则把“分析完成后”
  跨句误判成“整个工作已完成”，最终公开响应为空。当前本地候选删除完成/ETA/大小语义正则和对应死
  代码；回执出口仅校验 typed runtime status、结构化工具调用、空正文和内部协议，任务终态仍只认运行
  事件。聚焦回归通过，尚未发布到 1.10，故暂不宣称空回执已在生产修复。
- 同轮 Sl 真测捕获 `/btw` 的前台到后台交接窄窗：linked request 已移入 done，而同一 durable task 仍
  active，旧检查错误返回“任务已切换”。当前本地候选用结构化 `current/retired/mismatch/unavailable`
  状态区分请求退休和任务切换；只有 retired 才回落核对同 thread 当前 TaskRun 并补幂等 wake，真实
  mismatch/读失败仍拒绝。单测已覆盖交接接受和切换拒绝，尚待 1.10 发布复测。
- 同一 Sl 任务在 `/stop` 后用一句“继续”已精确恢复原任务、工作区和文件访问，但辅助回执仍错误声称
  “没有之前的上下文”。候选工作树现把精确 owner store 与 durable task id 下的原 goal、workspace
  basename 和最近 8 条已确认 guidance 投影给回执模型；其他 task、待确认引导和宿主绝对路径不进入。
  这是表达事实补全，不解析用户自然语言，也不参与任务选择、状态或权限；仍待 1.10 真机复验。
- 同一续接轮还记录到：真实模型在约 14 秒已说出下一步，但用户直到 94.342 秒协作让出才收到自然回执。
  当前本地候选只在第一次工具开始边界，把已生成的真实模型正文净化成一条 typed
  `assistant_commentary`，独立于 `/verbose` 工具日志和最终回复；provider/runtime notice 不可进入，失败
  进度投递只前移 cursor、不会阻塞耐久最终交付。该设计已对照 长期助手 commentary 与 通道运行时 block
  reply 管线并通过聚焦回归，尚未发布到 1.10，故首次真实飞书可见延迟仍是待复验缺口。
- 普通任务最终答复直接采用模型基于当前对话、工具、测试和子代理事实写出的自然正文；不再压成文件清单，
  也不再经过提交工具、目录扫描验收器、完成 marker 或第二次摘要重写。外部投递边界移除内部协议并把
  宿主绝对路径降成文件名，内部 transcript 保留真实路径供后续同用户续接。
- 模型已注册唯一通道无关的 `send_message`：收件人固定为当前 scoped owner，不让模型传任意飞书 ID；
  附件必须命中该 owner 的 task artifact registry，且文件仍在 owner 根内、状态 ready、hash 未漂移。
  assistant transcript metadata 保留最近产物引用；下一轮用户只说“发我”时直接复用并原生发送，
  不重新搜索、复制或生成文件。长任务的逐工具过程可由用户用 `/verbose` 显式开启；更高层、低频的
  长任务阶段汇报仍是后续体验项。
- artifact registry 只登记工具显式写出或结构化返回的产物，不再扫描整个任务目录推断任务是否完成。
  `node_modules`、虚拟环境、缓存和 `.git` 等运行文件不会被自动升级成用户附件；用户文件不会被删除。
  HTML、压缩包和文档格式的客观完整性仍在对应写入工具边界检查，坏候选不能覆盖已有好文件。
- 默认规则改为随 wheel 发布的 `builtin:prompts/default.md`，不再依赖 systemd WorkingDirectory。
  owner 的 `AGENTS.md → SOUL.md → USER.md` 仍从唯一 owner 路径逐轮注入。USER 画像/偏好可由 Agent
  通过结构化 `update_persona` 自主更新；单项只处理一个单行事实，多项用一次原子 `operations` 批量提交。
  可选 `source_quote` 仅进入版本审计，不用自然语言子串匹配决定授权。SOUL/AGENTS 只能走
  `update_persona`，飞书必须由发起人点击确认卡片后才写。基础文件工具、patch 和 owner-scoped bwrap
  shell 均不能绕过；专用人格路由在普通工作任务晋升之前执行，因此不会被任务工作区选择错误遮住。
- Feishu 默认 `long_connection`、私聊密码锁默认开启，默认闲置阈值为 3 小时；首次无密码时发设置卡
  但不吞掉第一条消息。设过密码且超时后原消息进入有界待续送 FIFO，正常解锁后沿唯一消息入口自动
  交给 Agent。显式配置可关闭密码锁或改用 webhook。
- 单机 Feishu 回调不再同步等待模型：提交 Gateway 后立即返回，pending/sent 回送记录持久化，后台
  worker 可跨进程重启继续轮询同一 request_id，超过旧 60 秒窗口仍送达真实结果且不重新提交任务。
- `/goal` 子代理阶段使用持久化 task/goal/run 状态驱动：有非终态子代理时不再登记即时自唤醒或用
  `wait` 轮询，完成事件会叫回同一个目标；全部终态后，主代理在同一目标上下文完成产物整合、客观
  验证和 `update_goal`。未终态自动轮、能力事件和迟到的已终态事件不进入普通 transcript；只有目标
  真正 complete/blocked 等终态才允许模型自然回复进入用户出口。该判定不解析模型或用户自然语言。
- Feishu 主动发送和引用回复共用 8000 字符分片边界，长报告不会再在最终引用回复时整篇一次提交；
  所有分片都会尝试发送，任一失败由既有持久回送记录继续重试。
- typed progress 现在与被认领的权威请求记录共址；per-owner 执行不会再把过程流写到 owner 私有
  Gateway 目录，`/progress` 和 Feishu delivery worker 能读取同一份经过 owner 鉴权的事件。
- USER 读取 `/result/<request_id>` 时，queued/processing/done/failed 都统一从请求记录解析 owner；
  响应文件不再承担身份事实。完成态同一用户可读、跨用户不可读；请求归档缺失、损坏或身份冲突时
  fail-closed。普通 USER 的完成结果使用顶层字段白名单，只返回状态、用户正文和安全计数；服务器
  path、lease、prompt 与内部错误细节不公开。管理员和 Feishu delivery worker 的可信回环路径保持
  完整诊断语义。
- scale worker 同样复用 gateway transcript/历史预算/任务候选主链，topic-aware lane 串行；ASGI 卡片
  action 明确分流，未配置 handler 时返回 503，不再误进普通消息队列和 dead-letter。
- 离线验收覆盖真实会话 ID 传递、两轮历史落库/重放、不同会话隔离、旧任务不污染、同会话并发排队、
  owner fail-closed、默认 prompt、人格卡片 owner 绑定、密码锁和 sandbox 只读人格文件。真实 1.10
  MiniMax 验证也已完成：`e947237e` 部署后，MiniMax M2.7 对同一会话正确回忆“蓝杉-472”，不同
  会话只答“不知道”；gateway/Feishu 两服务均 active、零重启、无 error 级日志。
- owner 修复提交 `36f5eb81` 的远端 Lint、Python 3.10/3.11/3.12、macOS 与 Windows CI 全绿。当前
  发布候选收集 8,337 项，完整 pytest 100% 且无失败；Ruff、import、offline、code-size strict、doc-sync、
  diff 均通过。worktree 检查按设计拒绝未跟踪运行数据；实际 2.66MB wheel 的
  distribution/artifact 两道门均为 `ok=true`、零 findings。1.10 已部署同一提交，Gateway/Feishu
  active、零重启、近 10 分钟零 error 日志；同一 USER 读取既有完成响应为 200/“蓝杉-472”，另一个
  USER 为 403，证明完成态可读和跨用户隔离同时成立。
- 1.10 的双用户 MiniMax M2.7 长链验收已经完成。A 用户先后完成健康预约平台和社区图书馆两个长任务；
  图书馆任务在后续自然语言检查/结案时继续使用原工作区，最终 30/30 自动测试、10/10 冒烟检查通过。
  B 用户把校园闲置交易网站分步布置，第二步完成后再次自然语言续接，22 条工具记录中 21 条引用原始
  task root、新请求占位 root 为 0，回归与新增测试共 37 项通过。任务终态后后台 claim 与 active link
  都正常清空；迟到 wake 没有复活旧分支。
- 50% 临时阈值下，A 的同一 Feishu thread 累计到 48 条、401,392 bytes 后自动推进到 compact generation
  4；byte cursor 为 211,732，正好落在 JSONL 行边界并与第 38 条 message ID 对齐，cursor 后仍保留
  10 条 raw tail。原始 JSONL 只增不删。压缩后的“银杏桥 / 每周四 19:20 / 青瓦计划 7426”可直接从
  summary 正确回答；更早的图书馆数据由 `session_search` 一轮成功找回 30/30 与 10/10。
- B 对同一 A 专属细节执行两次 owner-local `session_search`，结果明确为无记录；B 的工具索引没有 A 的
  owner path，A/B 的 transcript、thread state 和 LocalStore 数据库均位于不同 owner root。`/verbose`
  设置也在各自 thread 上持久保持 A=`on`、B=`full`。这证明本轮 owner/thread 数据隔离成立，不等于
  已完成十万用户并发容量证明。
- 真实召回测试发现 MiniMax 从 native tool-use 降级后会输出边界明确但非 JSON 的
  `tool => ... / --argument value` 调用；旧解析器连续拒绝，导致首次旧记录查询用 11 轮仍失败。统一解析层
  现只兼容无歧义、单行 JSON 值、无重复参数的该方言，随后仍走原有 schema/auth/path/runtime gates；
  多行含糊命令和夹带正文继续拒绝。部署 `da1a55e8` 后同一问题 1 轮搜索成功且零 parse error。
- 本轮双用户请求使用与 Feishu adapter 相同的 `X-Channel=feishu`、稳定 chat/user identity 和 Gateway
  `/ask` 主链，但测试 open_id 是隔离的合成身份，不能由真实 Feishu 服务端投递；因此证明的是服务器侧
  Feishu-scoped 会话/任务/compact/memory 链，不声称真实客户端收到了这些合成用户的引用回复或进度卡片。
  试验结束后 1.10 源码与运行配置均恢复 90%，Gateway/Feishu 为 active，健康探针返回预期 404。

### 2026-07-13 多 IM 统一投递当前工作树

- 普通最终回复、后台主动消息和显式 `send_message` 不再各自直调 adapter，统一进入
  `DeliveryService`。可信 `DeliveryContext` 持有 channel/target/reply_to，模型可影响的
  `ReplyEnvelope` 只持正文和已校验附件，没有收件人字段。
- `ChannelAdapterRegistry` 是 adapter、懒工厂、capabilities 和 target validator 的唯一解析入口。
  Feishu 的 `open_id` 合同位于注册层；未知通道不会回落到默认平台，新增 IM 不需要修改投递服务。
- `reply` 模式保留 adapter 的 `finalize_response`，所以 Feishu 仍会撤掉 typing reaction 并引用回复
  原消息；`proactive` 模式继续使用原生 text/image/file API。内部运行协议在统一出口净化或抑制。
- 普通回送继续使用 pending/sent receipt 做耐久投递；显式 `send_message` 的副作用身份和结果重放已
  归入通用 tool operation 账本，不再维护工具私有 receipt。发送失败不会重新提交 Gateway 任务，
  也不会重新生成附件。
- 定向回归与受影响的后台会话测试已经通过；第二个 fake IM 通过纯注册接入，证明主流程无平台分支。
  这不是第二个生产 IM 已可用的声明，也尚未替代下一次正式 Feishu 部署后的真实引用回复/附件复验。
- 详细合同见 `docs/design/CHANNEL_DELIVERY_DESIGN.md`。

### 2026-07-13 owner/thread memory + compact 部署证据

- 实现、离线定向回归与上述 1.10 双用户 MiniMax 验收均已完成，覆盖超过旧最近轮数仍累计、自动
  compact、raw transcript 不丢、旧消息可搜索、owner LocalStore 隔离、per-thread verbose、typed
  progress、progress cursor 与最终回复不重跑。
- 参考范围与设计合同见 `docs/design/CONVERSATION_CONTEXT_DESIGN.md`。这里复用 通道运行时/长期助手 的
  “稳定 IM 会话键贯穿 compact/memory”和 per-session verbose 作用域，不复制它们的摘要算法或
  profile-wide memory 默认值。
- 本轮已经证明 1.10 单机、两个 Feishu-scoped 合成用户、MiniMax M2.7、50% 自动 compact 与恢复 90%
  的组合；真实 Feishu 客户端投递、更多并发用户、跨节点迁移和长期故障恢复仍未由这次测试覆盖。
- `session_search` 的主链已证明可用，但一次查询可能只召回问题的一部分：本轮首次成功查询准确找回测试
  数量，却只找回部分“已知限制”。模型能继续改写查询，但当前不保证每次都自动扩大搜索直到信息完整，
  这是后续召回排序与多查询收敛仍需补强的点。

### 2026-07-14 普通聊天与后台工作运行时加固

- 普通聊天不再在请求入口预创建 task workspace。只有 `write/edit/patch/shell/browser/PTY` 等注册表中
  明确带 `promotes_task` 的工作工具，或 `task_progress/create_subagents/wait` 等结构化任务动作，才把
  当前会话提升为 TaskRun 并在该 owner 的任务树中惰性创建工作区；不解析“帮我做一下”等自然语言。
- `create_subagents` 成功后，wake-capable 的 IM/Gateway 请求会进入一次无工具的短模型轮并随即释放
  同会话顺序槽。模型只收到调度账本确认的 recorded、accepted、runner-confirmed running 和 failed
  事实，再用自己的语气回复；系统不复用本轮 `[TOOL_CALL]` 文本，也不拼固定回执。模型若输出内部协议、
  工具调用或空正文会重试一次，仍不合格就记结构化失败并抑制，不用“正在处理”冒充回答。
- 同一会话仍按顺序写 transcript，避免并发 assistant 回复乱序；后台子代理继续使用独立 TaskRun、工单、
  状态和产物账本。用户随后发来的闲聊/补充要求进入同一 thread，但不会注入子代理的 shell 命令、碎碎念
  或原始工具结果。完成/阻塞/需决策事件再由后台主代理通过统一投递出口回到用户。
- 自动派工监督现在保存结构化 material signature：状态、进度、阻塞、能力申请、产物与结果摘要都没变时，
  只顺延下一检查，不花一次模型调用；变化后才唤醒。显式 `wait` 和数据源定时巡检不套这条跳过规则。
- 多个成功子任务在默认 5 秒窗口内连续结束时合并为一次主代理整合；若同一 root 仍有兄弟任务在跑，
  这轮模型正文只作内部推进，不写普通聊天也不主动发 IM。失败/阻塞/需决策和全部结束仍立即公开，
  避免每个子代理都向用户发碎片进度或重复派工回执。
- 子代理完成时现在先原子发布 wake，再追加带反向 wake ID 的 observation；调度器不再可能卡在两次写入
  之间，把同一完成既当 observation 又当 wake 各跑一轮。若 wake 队列写失败，observation 仍作为兜底，
  且按同一完成事件投递规则处理。内部 `wait`/自动续推在仍有子任务运行时也不写占位回复进普通聊天。
- 后台续跑从结构化 task link 恢复原 goal、task path 和 owner task index 标题。`定时唤醒`、wait reason、
  子代理 runner prompt 不能再生成旁路任务目录或覆盖父任务身份；任务事实仍只认原 owner workspace。
- `ConversationStore.bind_task` 现在是并发安全的“首次创建或补齐空字段”，不是任意 upsert：已有非空
  goal/task_path/created_at 永久保留，跨 thread 重绑直接拒绝，终态不会被遗漏 status 的调用重新放回活跃索引。
  后续计划、wait reason 和子代理 prompt 必须写进各自账本，不能借绑定接口改写父任务身份。
- persona 工具使用稳定 entry ID 做 list/add/replace/remove；USER 可由所属 Agent 维护，SOUL/AGENTS 的
  replace/remove 仍须所属用户卡片确认。密码、OTP 和临时验证码在写 owner durable memory 前被结构化拒绝。
- POSIX shell foreground/background 与 bwrap 路径统一用 `bash -o pipefail -c`；结果 envelope 明确记录
  `return_code/command_succeeded`。工具错误同时保留归一控制码与原始报码，审计只保存输入字段名、类型和
  不可逆摘要，不复制命令、密钥或正文。
- 强制 Tool Gateway 的错误码注册检查已覆盖实际执行管线及直接子门，不再只扫描 manifest/effect：
  tool protocol、path/command、owner scope、guardrail、rate limit/circuit、approval binding、idempotency
  和 gate pipeline 的拒绝原因都必须命中统一 taxonomy。命令引号未闭合会保留为可修复的
  `COMMAND_PARSE_FAILED`，动态 artifact-ref finding 先归一成稳定协议码；未知新报码仍 fail closed，
  但不能再由已知门禁静默退化成 `UNKNOWN_ERROR`。本地专项与全管线防漏测试已通过，1.10 待当前长任务
  自然结束后随最新 wheel 部署复验。
- 工具调用的 `RunScope` 已按结构化运行身份区分主代理与 task-local 子代理：后台主代理不再把
  `bg-main-*` 临时轮次当成子代理 ID 查询账本，也不再为每个工具调用附带虚假的子代理
  `FileNotFoundError`；其 `root_task_id` 保留真实持久任务 ID。真正的子代理仍读取 canonical task
  lineage，账本真实损坏时仍保留结构化 load error。该修复已随 `606fe20a` wheel 部署 1.10；部署后两个
  全新 Feishu-scoped owner 的后台任务均未再出现 `tool_call_scope.subagents.load` 假错，服务零重启。
- 协作工具现在复用同一 `RunScope.root_task_id` 绑定当前持久任务：后台主代理不必让模型重复提供
  `task_id/thread_id`，也不再因只看子代理 runner context 而返回 `thread_required`。协作域原始报码与统一
  控制码分开保留，例如 `THREAD_REQUIRED` 对应可修复的 `TOOL_PARAMETER_REQUIRED`；工具输出索引同时保存
  两者，不再只留下 `UNKNOWN_ERROR`。专项协作/错误契约/归档测试已通过，1.10 待当前长任务自然结束后
  随最新 wheel 部署复验。
- 后台主代理的公开完成回复使用实际落账/投递时间，不再沿用可能早了数分钟的 wake/request 时间；
  append-only transcript 仍以追加顺序为会话权威，thread `updated_at` 同时保证单调递增，延迟事件不能把
  刚活跃的会话排回旧位置。该模式与 通道运行时 在实际完成 hook 时记录 `Date.now()`、会话运行时 在结果返回后
  emit completed event 的边界一致。
- 1.10 双用户复验确认普通聊天能在长任务让出后继续，且分别找回各自代号；同时发现派工自然回执会把
  模型可见事实键 `runner_confirmed_running` 翻成用户正文。当前工作树已把表达层事实改为 planned/ready/
  started/failed-to-start 四个用户语义字段，保留内部生命周期精度但不再把 runner 词汇交给回复模型；
  专项回归已通过，待下一 wheel 真机复验。首次自然回执仍约 50 秒，继续作为性能缺口。
- Gateway watch 的计划 stop、有限 max-cycles 完成和意外返回已有不同终态；无限 watch 无 stop 返回会写
  `GATEWAY_WATCH_UNEXPECTED_RETURN` 并以 2 退出。cleanup 记录 heartbeat/request/background 三线程是否
  drain 完成，未在期限内退出则改记 `GATEWAY_DRAIN_INCOMPLETE`，不再显示成普通 stopped。
- 同一模型轮可连续提交多次 `create_subagents`，运行时按原顺序逐个执行并把各次 lifecycle 事实聚合后
  交给同一个无工具模型回复轮；仍依赖前一次返回 ID 的 dispatch/inspect 等动作继续延到下一轮，并返回
  `ORCHESTRATION_CALL_DEFERRED`，不再丢成 `UNKNOWN_ERROR`。这使“5 个不同子任务”既不会虚报，也不会
  因原生工具调用批次只实际创建第一个。
- 同一 root 的成功 child 完成通知可以合成有界后台模型轮；`.7` 七路调研曾在只读五份后错误收口。当前
  候选保留 token/条数分批，但延期 sibling 继续留在 durable mailbox，不能被 mid-turn 瘦事件入口提前确认，
  并在 root closeout 与用户最终投递前形成等待；每批仍保留 child id、typed status 与报告引用。
- 2026-07-16 双用户长任务实测发现 MiniMax 会发出完全空参数 `create_subagents`。当前合同不再用“全局
  required 顶层 goal”误伤批量形态，而是在统一 handler 校验二选一：单派必须有非空顶层 `goal`；批量
  必须有非空 `items` 且每项独立 `goal`，顶层批次 goal 可选。完全空调用仍在创建任何 run 前返回 typed
  可恢复错误；`.7` 七路调研的真实失败样本证明旧全局 required 会白耗一次自纠，65 项 focused 已覆盖新
  合同，待当前长任务自然结束后部署正向复验。
- 同轮 `/stop` 实测还发现终态请求已移入 `done/`、task link 与 response 均为 `interrupted`，但归档 JSON
  残留 `processing`。当前工作树已改成以 response 终态覆盖旧 lease 状态；本地专项回归通过，1.10 部署
  验证尚未完成。
- 同一实测还复现了恢复后的 request id 与原持久任务 id 分叉：进度工具写原任务账本，旧验收器却读取
  新 request 的空账，随后普通追问又选中旧任务并重复执行。当前工作树保留唯一结构化任务身份解析器供
  guidance、task_progress、需求/派工 seed、coverage、wait、监督提醒和子代理归属使用；同时彻底移除
  普通任务的独立验收器。default 主代理使用 `conversation_task_id`，task_local 子代理继续按自己的 run
  隔离。恢复账本与子代理归属专项回归已通过；1.10 尚待部署后真机复测。
- 当前任务已经结构化建立后，模型若把交付文件臆造到该 owner home 的任务外位置，且用户没有显式指定
  该目录，创建策略会把路径及 goal/plan 中的同一引用一起归回当前任务 `output/`；从另一个任务复制来的
  绝对路径只保留文件名，避免在新任务中套出第二棵 `tasks/...`。用户明确指定的目录仍按原权限合同处理。
- 2026-07-16 的 1.10 分步续作真测进一步证明，仅做路径归一还不够：A 在新建 TaskRun 后用绝对路径
  回写了同一 owner 的旧 Zoxide 任务。当前候选在共用 runtime ledger chokepoint 按结构化
  `provider + owner_scope_root + task_root` 自动生成任务级 `allowed_write_roots`：远程普通 owner 仍可读
  自己的旧任务作参考，但文件工具与 bwrap shell 只能写当前任务；已有子代理窄授权不会被放大，兄弟任务
  授权被过滤，畸形 task root 显式空白名单 fail-closed。local 入口和有效 admin bypass 保持原权限。
  该实现不解析“继续”等自然语言；已对照 会话运行时 的 thread/turn identity 与 writable roots、通道运行时 的
  scoped session key，聚焦 runtime/write-boundary/sandbox 回归通过，尚待发布后 1.10 反证。
- 后台自动续跑、定时监督和子代理终态唤醒不以自然语言正文判断任务状态；是否仍在后台运行、是否已经
  协作式让出以及 conversation task 是否结束都来自结构化 task attributes 和 lifecycle 事实。最终公开正文
  仍由模型生成，内部监督/等待信号不进入普通 transcript 或 IM。
- 普通用户回复新增“模型正文 / 运行状态”硬边界：除显式控制命令外，通道正文只接受模型自然文本；
  `RUN/MAIN_AGENT/SUBAGENT` 协议和非阻塞状态只保留在结构化字段。只有内部协议而没有模型正文时不再
  写空占位 assistant 消息，也不再投递固定“任务正在处理”句子。
- 普通任务不必先进入 `/goal` 才能派子代理。长工作、子代理事件、定时唤醒和 compact 后续轮都在同一
  thread 历史中继续；耐久 progress policy 只负责崩溃恢复和后续调度，任务已停止或不再 active 时不得
  复活。非阻塞 `wait` 仍会结束当前 turn 后由 scheduler 续接，这一点尚未与 会话运行时 的同 turn `wait_agent`
  完全一致，但不会建立第二条聊天轨道或第二份上下文。
- `create_subagents.goal` 是模型工具调用里的整批派工说明，不是用户 `/goal` 模式开关。该边界与 会话运行时
  `spawn_agent.message`、通道运行时 `sessions_spawn.task` 一致：普通任务可以自主派工，但每个新执行单元都
  必须拿到明确工作说明。my-agent 的 `items` 批量形态还要求顶层整批说明和每项独立说明同时存在。
- 2026-07-18 本地候选已加入唯一 `schedule` action tool：支持 typed `at/every/cron`、
  IANA timezone、create/list/get/update/pause/resume/delete/run-now/history/status。每个 user/group
  owner 只读写自己 home 的 `data/scheduler/store.json` 和 `history.jsonl`，工具参数不能指定
  owner 或任意 thread。它不用 `wait` 假装提醒，也不用自然语言关键词改机器状态。
  到期 run 在模型执行前预留并推进 recurring job 的下次时间，使用请求/工具调用去重、
  version/CAS、misfire grace、claim TTL、takeover 和 heartbeat；重启后 owner disk discovery 会找回
  due/queued 事实。它仍是原 owner agent 在创建时同一 thread 上的新 turn，消费同一
  transcript/compact/Memory/Persona/Tool registry；Skill 引用按 stable ID + SHA 固定，版本漂移在模型执行前
  fail-closed。schedule/DST、CRUD/CAS、misfire/claim takeover、restart dedupe、owner 损坏、同 thread
  多 job 聚焦回归已通过；完整 CI、1.10 和真飞书到期验证未做，状态仍是待验证。
- 1.10 部署后的并行真测发现，后台根任务运行时，后一条普通聊天仍可能由模型调用 `task_progress select`
  变成第二个执行器，继而重复检查工作区，并把“前台工具轮数”等内部事实复述给用户。当前本地候选在
  任务选择卡口只读精确 task link、有效 background claim 与 enabled progress policy：已经执行的任务只
  作为 `running_in_background` 只读背景，第二次 select 返回已注册的
  `CONVERSATION_TASK_ALREADY_RUNNING`；状态损坏或不可读时返回
  `CONVERSATION_TASK_STATE_UNAVAILABLE` 并 fail-closed。运行来源和精确工具轮数不再进入模型回复事实。
  同一 task/kind 的前台续跑 policy 也会复用，避免重复唤醒。以上候选已通过专项回归，尚未部署 1.10。
- 普通任务最终正文不再经过冻结 snapshot 或第二个模型重写；主模型已经写出的项目目录、主要功能、测试
  结果和限制原样进入统一通道投影。真正交给 IM adapter 的 `ReplyEnvelope` 只装净化后的人话，内部运行
  协议和宿主绝对路径不进入外部正文；内部 transcript 保留真实路径供后续工作续接。
- `047e24f7` 的历史部署曾证明耐久后台派工可以完成，也暴露同一任务第二执行器和并行 turn 的语义问题；
  该结果只作为缺陷证据，不代表当前单一 history/compact 候选已发布或已通过真实 Feishu 验收。

### 2026-07-10 两机日志与真实 LLM 加固快照

- 1.9 / 1.10 的近 24 小时日志已逐项审计；1.10 service-cwd 的真实 memory 索引缺口已通过
  非破坏性 `local-rebuild --source memory` 从 0/8 修复到 8/8，复查 doctor 为绿色。
- 当前工作树已修复日志 secret 泄漏、Feishu 无效目标外发、结构化大批截断、取消态误恢复、原生工具
  历史 role 漂移、真实测试假绿、delivery materializer 隐藏硬门、Markdown prose 路径误判，以及
  LiteLLM `available context size` 未进入统一 compact/resume 错误分类的问题。
- 本地 4000 端口后的 Qwen 已通过 preflight、三轮原生工具、48 项结构化判断、长报告
  compact/resume、失败工具恢复、gateway ask、1 个真实子代理、两机 PTY、LSP stdio 和 MCP 调用。
  两台正式安装现已升级到同线 wheel，配置为 `openai_compatible` / `local-qwen` / `125184`；1.9 正式
  gateway ask 以 295.122 秒成功，1.10 服务保持受控停机。共享模型在多个 23k–70k token 后台请求下
  让新探针超过 120 秒，因此连接已证明，容量、尾延迟、隔离和十万用户规模仍未证明。
- 完整发现、参考文件、证据边界和未测清单见
  `docs/audits/REAL_LLM_24H_HARDENING_20260710.md`。
- 24 小时 proof 的旧 evaluator 在 86,457 秒一度给出 `proven=true`，完成审计发现它只检查最后一拍
  健康，却把整份 evidence 首尾当连续时长。真实 NDJSON 中 2026-07-10T19:53:35Z–19:56:36Z
  有一路超过 900 秒 freshness 门，因此旧结果已作废。公共 evaluator 现逐拍检查并在失格时重置，
  同一证据正确回退到约 46,984 秒、`duration_too_short`。response-body timeout 修复仍有效，
  monitor stderr 自 2026-07-10T07:04:09Z 后未增长。
  新 evaluator 重载时原 86,400 秒 watch 窗口已经到期，三路 harvester 按设计停止并导致最新
  freshness 失格；测试窗口保留原游标/账本延长到 200,000 秒后已恢复，pull 计数继续增长。
  因最新失格不能跨段拼接，2026-07-11T09:11:47Z 的新连续段从约 30 秒重新计时。
- 后续两机同时经 4000 的真实 Qwen 调用均完成。LAN 地址漂移暴露出旧端点会占满 600 秒的问题；
  当前 provider transport 已把 connect 默认限制为 10 秒，并保留长 reasoning 的 600 秒 read/request 窗口。
  旧地址在 10 秒失败、正常请求持续约 7–8 分钟后成功，仍需 DHCP reservation 或受管 DNS 消除地址漂移。
- runner-session 心跳不再每 5 秒触发完整 workspace/compact/projection 保存；takeover replacement 现在从
  `context_bundle.takeover` 直接获得有界结构化来源 handoff，不再被提示去用普通文件工具读取受管状态面。
- 真实第 5 个 runner 还暴露出 unstructured fallback 假绿：原始与 repair 正文都为空时，旧 finalizer
  仍写 `DONE/VERIFIED`。当前已改为 `BLOCKED/UNVERIFIED/structured_output_parse_error` fail-closed；
  只有可解析的结构化 runner result 能进入成功态。
  同一个 run 在隔离配置把输出预算从 512 提到 2048 后，真实返回结构化摘要
  “结构化接管成功 / 子任务1”，0 工具调用并进入 DONE/VERIFIED；没有创建第 6 个测试子代理。

### 2026-07-19 同一 thread 前后台执行 lane 与动态任务工作区

- 1.10 的双 owner 长任务复测发现一条不能算成功的事实：B 在 `/stop` 后用一句自然语言继续原任务时，
  前台 request 仍在第 44–57 个工具轮修改原项目，后台 `scheduled_progress_report` 却已对同一
  `thread_id + task_id` 启动第二次模型执行并主动发送 930 字“完成”消息。前台请求约 20 分钟后才真正
  `done`。这证明旧 Gateway 的入站 single-flight 只约束前台 request，不约束 scheduler 与前台共用一个
  会话执行权；12:24 的回复已明确作废，不能当作完成证据。
- 候选修复直接对照 会话运行时 `会话运行时-rs/core/src/session/inject.rs::try_start_turn_if_idle` 的原子
  `active_turn` 预留，以及 通道运行时 `src/process/command-queue.ts::enqueueCommandInLane`、
  `src/infra/heartbeat-runner.ts` 对 resolved session lane 的 busy 检查。my-agent 不新增 IM 锁：
  `agent/conversation/run_claim.py` 把既有持久 claim 提升为 foreground/background 共用的 per-thread lane；
  Gateway 先只解析 thread identity，拿到 lane 后才读取 compact、raw tail、task 候选并执行/落账完整 turn。
  scheduler 拿不到同一 claim 时维持既有跳过/重试，不产生第二执行者；不同 thread 仍可并行。
- 回归覆盖前台持有时后台 claim 被拒、前台等待后台后读取其最新落盘消息、两个并发前台严格串行且第二轮
  看见第一轮历史；三项竞态测试连续重复五轮通过，相关 Gateway/conversation/scheduler 196 项通过。
  `f19d0be4` 已通过完整本地门禁、推送远端 main 并精确部署到 1.10。真实一次性提醒跨
  Gateway/Feishu 重启后仍只执行、投递一次；第二个提醒到期时同 thread 正被一个前台长任务持有，后台
  多次只记录 busy 而没有并发生成或发送，前台被 `/stop` 终止并释放 lane 后才发送一次并进入
  `one_shot_finished`。这证明前后台共用执行权已在真实 MiniMax M2.7 + Feishu 通道成立。
- 同轮新差旅项目真测又暴露出独立底座问题：`task_progress start/select` 已把结构化
  `run_workspace` 切到 owner 当前任务，但每次工具调用仍合并请求进入时的旧 `write_boundary`，导致主代理
  的 write/run 被错误收窄为空。当前候选在唯一工具执行缝隙始终以本轮 `run_workspace` 覆盖旧
  task root；只有 `context_scope=default` 且同时绑定精确 conversation thread/task 的主代理，才把
  `allowed_write_roots` 重绑定到当前 task root。`task_local/control_plane` 子代理继续保留原窄授权。
  该判断只读取结构化 scope、thread/task id 和路径，不解析用户或命令正文。会话运行时 的
  `TurnContext/TurnEnvironment` 与 通道运行时 的 `effectiveCwd/effectiveWorkspace` 工具构造链是代码级参考。
  聚焦会话、任务选择、写边界、子代理和多用户隔离回归已通过；完整本地 CI、1.10 重部署及原任务续作
  反证完成前，本条仍只算候选。
- B 用户有一轮测试误用了相近但错误的 `conversation_id`，因此创建的新 thread、找不到旧任务及其后续
  行为全部排除在产品证据之外；它既不能证明也不能否定正确 thread 上的 `/stop`/续作语义。
- B 的最终项目请求本身已 `done` 且原 task/workspace 被复用，内部 25/25 测试通过；但独立坏输入验收
  发现空文件和无待办文件都返回 0，其中无待办还报告“全部检查通过”。因此该用户任务仍需沿原 thread/task
  返修，不能用模型自报或内部测试替代外部验收。
- `04c34947` 已通过完整本地门禁、推送远端 main 并精确部署 1.10；正确 A/B conversation 的停止后续作
  均选回原任务，A 的主代理 write/run 已不再被旧 bootstrap workspace 错误拒绝。真实差旅任务随后暴露
  另一条独立底座缺陷：子代理只写出一个政策 JSON，旧 result materializer 却把它复制成七个不同声明
  文件，CSV、测试和 README 内容与 SHA 相同。当前工作树已删除缺失声明物化、同名递归搜索、文本后缀
  猜源和 summary 占位整条路径；声明仅作为预期，registry 只登记真实存在的 artifact，唯一允许的复制是
  typed `output_delivery_map` 的现存 source 到受围栏 target。该改动对照 会话运行时 真实 patch success 事件和
  通道运行时 真实子会话 output capture，未增加 IM 分支或自然语言判断；subagent/result/artifact 聚焦回归、
  Ruff、import、offline、strict code-size、doc-sync 与全量 pytest 已通过，尚待最终 wheel 部署后的真实反证。
- 同一轮双 owner 真实 MiniMax 任务继续暴露了一个 task projection 竞态：A 的根 link 已先写
  `completed`，但同 thread 的 background claim 仍在运行；旧 `/status` 因而错误显示空闲，`/btw`、`/stop`
  也会失去精确控制对象，parent lifecycle reconciler 还把后续两个 child 取消为
  `conversation_lifecycle:parent_link_closed`。当前候选把 task link 与 per-thread claim/enabled policy 合成
  一份结构化执行快照：只有 `completed + exact live execution` 在交接窗口内仍可控制、允许 child；
  `interrupted` 永不复活，过期租约不复活，状态不可读时控制不认领、child 只 HOLD 不破坏。每个 thread
  每轮只读一次快照，不按历史 task 或 child 数重复扫描。该语义直接对照 会话运行时
  `会话运行时-rs/core/src/session/mod.rs::steer_input` 的 `active_turn + expected_turn_id` 与
  `tasks/mod.rs::on_task_finished` 的 active-turn 生命周期，不解析用户文字。
- 后台 continuation 创建 child 时，线程局部 `_current_run_task_workspace` 可能为空，但
  `task_attributes.run_workspace.task_root` 已是当前任务的结构化事实；旧默认 output ref 因而缺失。当前候选
  保留线程局部值优先，并以该结构化 task root 作唯一后备，给未显式声明产物的 child 生成各自不同的
  `work/child_outputs/<index>-<slug>.md`，不从 goal 文本推断路径。
- 双项目独立副本验收没有接受模型自报：A 的 clinic-log-check 为 23/23、安装后 CLI 正常、正常/缺列退出
  0/2、JSON/Markdown 两次 SHA-256 一致；B 的 inventory-diff 为 21/21、安装后 CLI 正常、正常/非法数量/
  缺列退出 0/2/1、两类报告两次 SHA-256 一致。B 连续两次回复“零缓存”，但原项目仍有
  `__pycache__ + 4 pyc`。审计证明工具收到的是精确原项目路径，假阴性来自 `find_files/list_files` 默认
  跳过缓存目录。当前候选保留宽泛发现降噪；未显式设置 `include_ignored` 且显式 glob 在可见区零命中时，
  自动补查忽略目录并返回结构化 `discovery.included_ignored=true`；显式 false 仍严格排除。该适配参考
  会话运行时 `linux-sandbox/src/bwrap.rs::ripgrep_files` 对显式 glob 使用 `--hidden --no-ignore`，没有增加项目名、
  中文提示词或完成声明硬编码。最终发布和 1.10 部署后必须重跑同一真实反证，当前仍只算候选。
- `d815592c` 的 wheel 已精确部署 1.10 后，B owner 沿原 conversation/task 完成库存项目返修：原项目
  缓存已由工具发现并清为 0；独立复制到本机的副本 31/31 通过，正常、重复 SKU、缺列退出码分别为
  0/2/1，JSON/Markdown 两次逐字一致，临期、仓库汇总及 `UNKNOWN` 仓库均有实际输出。该轮同时暴露
  A 的新底座缺陷：子代理按精确绝对路径写旧项目时复用了主代理的全局 task select，导致本轮父 link
  被标 `superseded`，两个 child 随后被 lifecycle 取消。
- `ec6a1ae6` 把 child runner identity、parent task lineage 与 cwd 分开：child 只能在本 runner 内重绑定精确
  workspace，不 reopen/supersede/select 全局 conversation task；后续 task promotion 也只验证父 link，不能
  覆盖 child cwd。代码级参考为 会话运行时
  `tools/handlers/multi_agents_common.rs::thread_spawn_source/apply_spawn_agent_runtime_overrides` 中独立
  `parent_thread_id + config.cwd`，以及 通道运行时 `session-child-sessions.ts`、`sessions-spawn-visible.ts` 中独立
  `parentSessionKey + child session`。同时 `capability_request` 的拒绝、缺参和 run 不存在均返回注册错误码，
  不再退化成 `UNKNOWN_ERROR`。
- 1.10 真实 MiniMax M2.7 双 owner 反证已完成：A/B 都沿原 Feishu conversation、原 persistent root task 和
  原项目继续，各自只新建两个 child，四个 child 全部 `DONE`，没有 supersede 父 task、没有复制第二个项目。
  A 的多次 typed `/btw` 只进入同一运行任务；任务结束后的迟到 `/btw` 被明确拒绝且未写入后续上下文。
  独立副本最终分别为 A 59/59、B 51/51，稳定 JSON/CSV/Markdown、坏输入、汇总、原因码、缓存、symlink
  均按外部验收通过。A 中模型曾多次把弱测试说成完成，均由独立边界样例驳回并沿同一 task 返修；因此
  该证据证明的是底座续接、派工和隔离成立，不把模型自述当质量证明。
- 真测空闲期还定位到 scheduler no-op poll 的通用性能缺陷：`reserve_due_runs` 即使没有预留或 misfire
  推进也重写 owner store，继而扫描整个 owner home 的逻辑字节配额。1.10 同一 5 秒口径修复前约占
  `1.008` 个 CPU 核、RSS 约 444 MB；只在 `reserved or skipped` 时写账本后降为 `0.030` 核、RSS 130 MB，
  398 MB active-task 索引在采样期未变化，`py-spy` 中 owner quota 扫描栈消失。该边界对照 通道运行时
  `cron/service/jobs.ts::nextWakeAtMs` 与 `cron/service/timer.ts::armTimer` 的按最早到期事实唤醒和空闲等待，
  没有新增第二套调度事实源。

### 2026-07-21 正式 1.10 多用户与长任务候选验证

- 1.10 只保留正式 `my-agent-gateway.service` 与 `my-agent-feishu.service`；最终 wheel
  SHA-256 `4b882778888ee915a54a8414651965753999acfd935a4c0053d0c2755052bdcb` 已部署，安装记录的 archive hash
  命中新实现，Gateway/Feishu active、`NRestarts=0`、Feishu WebSocket connected。正式配置暂时指向
  `http://192.168.1.5:8899/v1` 的本地 Qwen；进程、cgroup、容器、timer、环境和全部 established socket
  复查没有其他 1.10 my-agent 快照或 MiniMax 调用。1.9 的 `my-agent-claw.service` 已停止为
  `inactive/dead`，无残留 agent 进程或业务连接；unit 仍 enabled，重启后仍可能自动启动。
- 10 个 Feishu-scoped 合成 owner 各三轮、六条消息的现存证据重新从正式 owner store 读取：每个 owner
  只含自己的校验词，10/10 没有跨 owner 命中。该证据证明 owner transcript/Memory 文件边界和同 thread
  续接，不把合成 `/ask` 冒充成十个真实飞书客户端入站，也不外推成十万用户容量。
- Chi owner 的单次长任务沿原 conversation/thread 完成唯一 `chipy` 项目；最终从远端项目重新复制到
  `/tmp/my-agent-accept-chi-final.8a5hju` 的干净副本，51/51 测试、安装导入和缓存/pyc/egg-info/symlink
  检查通过。一次验收脚本漏传顶层 `conversation_id` 只创建了空 task，已立即停止并从产品证据排除。
- Chalk owner 的后续普通中文请求都由顶层 `conversation_id` 精确绑定原
  `thread-316db5cc5d814cba`、原 task `req_1784617354048_243904_1` 和原 `output/pychalk`；没有新建或复制
  第二个项目。初始独立事实是 28/42 通过、14 个失败。本地 Qwen 第一轮在读取源码后用满
  16,314 token，结构化结束为 `MODEL_INCOMPLETE_RESPONSE` 且没有编辑；第二轮 `/btw` 在下一安全点成为
  同一 transcript 的真实 UserTurn，但模型继续产生长 reasoning。人工 `/stop` 后请求为
  `interrupted/INTERRUPTED`、模型连接关闭、task/workspace/transcript 保留，证明 steer/stop 主链成立；
  随后 MiniMax M2.7 沿同一会话运行 5 个工具轮，但在 754.444 秒后以 `max_tokens` 结束，结构化失败仍为
  `MODEL_INCOMPLETE_RESPONSE`。没有等待额度刷新：正式服务立即改用已探活的本地 8899；第一轮本地续作
  运行 7 个工具轮后用满 16,314 token，同样以 `length` 明确失败。把输出上限调到本地服务实际允许的
  32,768 后，第二轮运行 1,161.106 秒、9 个工具轮，仍以 `length/MODEL_INCOMPLETE_RESPONSE` 结束且没有
  源码修改。随后仍不等待 MiniMax，在同一 owner/thread/task 用普通中文分阶段继续：先修样式顺序，再修
  list casting、`apply/call/bind`，最后修 level 1/2/3 HEX、level 0 与 `visible`。运行中多次 typed `/btw`
  都在下一安全点进入同一请求；一轮长时间无新工具动作后 typed `/stop` 关闭当前模型连接，再从同一现场
  继续。写边界拒绝了模型猜错的目录，模型也修正了重复 helper 和错误 level 0 实现。远端原项目最终
  42/42；重新复制的干净副本 `/tmp/my-agent-accept-chalk-clean.Fs2Fij` 与远端 8 个文件逐文件 SHA-256
  一致，在禁用 bytecode/cache 的独立运行中也是 42/42，且无 cache、pyc、egg-info、build、symlink。
  另一个临时副本完成 fresh venv 安装、导入和链式样式、三档 HEX、level 0、`visible`、`chalkStderr`
  行为断言。该结果证明本地 fallback 能完成任务，也保留一个明确限制：本地模型需要更细的阶段和更多纠偏，
  不能把模型过程自述当作完成证据。
- 真实运行还定位到两个独立的通用资源问题。其一，流式响应出现完整工具块后，旧代码先返回工具循环却
  没有关闭 provider worker，忽略尾部仍会占第二个推理槽直到耗尽预算；当前统一走 typed interrupt close
  并 drain 精确 worker 后才开始下一轮。流式 transport 的 `request_timeout` 同时明确为 idle timeout，持续
  收到数据的长 reasoning 不再被第二个总墙钟错误截断。其二，正式 Gateway 主线程旧有 process-wide
  `watch_subagents` 会对 `local/main` 的陈旧 child 反复调用 LLM planner，且 decision 从未 apply；当前
  Gateway 只承载 HTTP/request pool 和 owner-scoped event-driven continuation，删除 Gateway 专属 planner、
  watch、force-lock 参数与重复 capability router。聚焦 Gateway/background/tool-generation 92 项回归通过；
  最终整套 pytest 收集 8,172 项并以退出码 0 完成；Ruff、import boundary、offline contract、strict code-size、
  doc-sync、compileall、distribution boundary 和 wheel artifact clean-package 全部通过。全量门先暴露了一个已
  删除参数仍留在 fake 心跳函数中的测试死循环，删除旧参数后该组 3/3；随后又暴露
  `MODEL_INCOMPLETE_RESPONSE` 未登记统一错误合同，补为 `model/retryable/change_strategy` 后相关 33 项和
  最终全量均通过。worktree clean-package 仍按设计拒绝保留的未跟踪 `data/` 与 handoff 文档，并提示另外
  几个已忽略运行目录；最终 wheel 没有这些文件。该 wheel 已精确部署到 1.10，安装 hash 一致，正式两个
  服务 active、`NRestarts=0`、队列为空、飞书 WebSocket connected；1.9 服务仍为 `inactive/dead`。

### 2026-07-25 工具超时未知态与真实通道证据

- mutating/dangerous 工具 timeout 不再进入普通失败重试：权威 operation 记为 `unknown`，原错误只保留
  为诊断。同一精确操作和显式 business key 都会被挡住；只有目标系统的结构化核对带 `source_ref`
  证明 succeeded、failed 或 not_started 后才能收口，其中 not_started 以 generation CAS 原子重开。
  核对证据随 operation 持久化，重放不会丢失。
- `send_message` 使用 business key，Feishu 正文、引用回复、长消息分片和媒体消息使用稳定 UUID。
  只有带 UUID 的同 payload 才对传输错误与 408/429/指定 5xx 做有界重试；4xx、无 UUID 和上传动作
  不盲重试。没有核对能力的平台发生超时时保持 fail-closed，而不是冒险重复发消息。
- 本地 8899 与 MiniMax-M2.7 的独立 CLI 各完成一次真实 `write_file → read_file`，每轮只有一条成功
  写 operation。1.10 唯一正式 Gateway/Feishu 又沿两个既有真实 owner 做了真实消息发送：本地 A/B、
  MiniMax B 与纠正后的 MiniMax A 各只有一条 succeeded `send_message`，owner、正文和 receipt 分离，
  两人的 SOUL、USER、长期 Memory 哈希不变。
- MiniMax A 首轮没有调用工具却在正文中声称发送；operation 数为 0，A 的 Feishu 历史精确正文数也为
  0。底座因此没有把模型自述升级为送达事实，沿同一会话普通中文纠正后才产生唯一真实发送。这个案例
  说明“副作用执行真伪”已由程序掌权，但自然语言任务是否应当选择某工具仍属于模型完成质量；系统不会
  通过中文关键词硬判并擅自补执行。
- 正式飞书没有故意制造真实 timeout，以免把用户可见副作用置于不确定状态；超时、迟到完成、核对成功/
  失败/not-started、缺证据、双核对者竞态和重开后重放由 fake transport 与 owner SQLite 合同测试覆盖。
  最终完整 pytest 已到 100% 且退出 0；Ruff、compileall、import/offline、strict code-size、doc-sync 与
  diff gate 均通过。worktree clean-package 按设计拦住保留的未跟踪运行数据和 handoff 文档；最终发布物
  还必须单独通过 distribution boundary、artifact clean-package 与 1.10 部署后 smoke。

### 2026-07-25 多外部写部分结果与目标路径身份

- 普通任务没有通用跨工具事务或 Saga。每次外部写各自经过唯一 `tool_operations` 生命周期，按模型给定
  顺序执行；没有 typed 依赖时，一个失败不会机械阻止独立后续调用。系统准确保存每项
  `succeeded/failed/unknown`，业务回滚只能由具体工具或 workflow 明示，底座不承诺跨系统原子性。
- provider/handler 回报成功后，如果权威 completion 保存失败，调用现在返回
  `TOOL_OPERATION_OUTCOME_UNKNOWN` 而不是 `ok=true`；同一 claim 保持占用，不能因模型换 call id、
  换说法或普通重试再次产生副作用。归档、控制面事件、模型恢复上下文与 compact 共用同一组
  operation/effect 字段，语义摘要会保留中段失败、运行中和 unknown 的精确事实。
- 显式绝对路径具有目标身份。旧代码会把白名单外绝对路径静默搬进 task output 后返回成功，虽然没有穿透
  安全墙，却会让调用方误以为原目标已经写入。该 escape-relocate 链、`owner_scope_root` 死字段和专属
  旧测试已删除；相对 `output/...`、`work/...` 仍按 task root 解析，绝对路径必须由统一写边界按原目标
  明确允许或拒绝。这个边界对照 会话运行时 保留解析目标再交 sandbox/approval，以及 长期助手 报告真实
  `resolved_path` 的代码路径。
- 聚焦回归和完整本地门禁通过。本地 8899 Qwen 完成 3 写 3 读基础 CLI；MiniMax-M2.7 完成 8 写 8 读
  长链，并在极端 CLI 精确得到 `succeeded/failed(WRITE_FORBIDDEN)/succeeded`，三项 generation=1；
  受限原目标和伪造替代目标都不存在。运行时代码提交 `2bd8622f` 的精确 wheel SHA-256 为
  `01ab7d15df60b1db613e7d52e211ce46bb3fe04bb52b7a0ccb43dd835b587614`，已部署 1.10。
- 1.10 的既有 Feishu owner A 请求 `req_1784957378024_1282076_0` 得到同样的
  `succeeded/failed/succeeded` 三写账本；owner B 请求 `req_1784957456380_1282076_1` 的跨 owner
  读取在实现前拒绝，随后自己的写入和回读成功。A/B 各自另有一条 generation=1 的 succeeded
  `send_message`，Gateway 日志和 Feishu sent receipt 证明消息分别发往对应 open_id。服务保持唯一
  正式实例、active、`NRestarts=0`、8420 loopback、队列为空。
- 这四个请求复用了既有 owner/conversation，由可信 localhost Feishu scope 提交；出站确实经过飞书 API，
  但不是新的客户端入站消息。随后已从 macOS 飞书客户端切换两个真实登录账号补证：两边首次消息都被
  闲置密码卡片 fail-closed 拦住，解锁后分别重发 A2/B2。平台入站消息 id、owner/thread transcript、
  Gateway request 和 sent receipt 完整对应；A 只回复 `客户端-A729`，B 只回复 `客户端-B729`，
  两轮 operation count 都为 0，没有工具调用、跨用户内容或内部协议正文。

### 2026-07-29 普通会话、工作目录与停止语义收敛

- 普通 owner/thread 只有一份持续 transcript、compact、Memory/Persona 注入和 sticky cwd；每条用户消息
  都是新的 active turn，当前消息直接决定聊天、工具工作或派工。历史 task/progress 只作审计和恢复事实，
  不再注入 active/completed 候选菜单，也不要求用户或模型选择、开始、完成或关闭旧任务。
- `task_progress` 当前公开动作只有 `read/update`。普通 open item 是可选工作笔记，不阻止模型回复、
  不追加隐藏“完成核对”轮、不注册后台 continuation；只有显式 `/goal` 的 exact active goal/task
  继续拥有暂停、恢复、open-plan 生命周期和耐久续跑。
- `/stop` 是当前窗口的中断按钮：优先中断 exact live Gateway request；没有前台 request 时只接受已登记
  interruptible turn 或未过期 background claim。仅有旧 active link、open checklist 或未来 reminder
  不算正在运行，因此不会被 `/stop` 改状态。停止后历史、compact、记忆、人格、cwd 和文件均保留，
  下一条普通消息无需补发旧 prompt 或任何任务管理命令。
- sticky workspace 的内部结构化绑定仍保留，但旧 `select_current_conversation_task` 名称和公共导出已
  删除。终态目录被下一轮工作复用时，系统创建新的执行 identity 并刷新 canonical workspace 投影；
  旧 task 保持终态。精确结构化写路径可以在统一 Tool Gateway 绑定同 thread 的既有目录，历史菜单和
  自然语言没有执行权。
- 本轮实现直接核对 会话运行时 `session/session.rs`、`session/mod.rs`、`tasks/mod.rs`、
  `tools/handlers/plan.rs` 与 长期助手 `cli.py`、`tools/todo_tool.py`；只适配现有 owner/thread/goal
  数据结构，删除了候选加载/渲染、普通清单提醒、普通 continuation、select/start handler 和废弃错误码，
  没有增加完成硬门、正文关键词判断或 IM 专用分支。
- 运行代码提交为 `b18f7774`。干净 Git archive 构建 wheel
  `my_agent-0.3.0-py3-none-any.whl`，SHA-256 为
  `1c8cbefb353079b9defcdbe5a7328869e12c2e753a83256cf186a8856ebb2d7e`；distribution boundary 和
  artifact clean-package 通过，已用正式部署链安装到 1.10。Gateway/Feishu 最终均 active、
  `NRestarts=0`，8420 只监听 loopback，request JSON 队列和 delivery pending 均为 0；模型配置保持
  `MiniMax-M2.7`，没有新增服务、端口、owner、conversation 或项目。
- 本地全量 pytest 已跑到 100%，只剩两条仍断言旧语义的测试期望：一次仍期待隐藏 completion nudge，
  一次仍期待公开 `select/start`。更新这两条测试后，覆盖本次变更面的 308 项回归全部通过；Ruff、
  import boundary、offline matrix、strict code size、doc sync、contract pyramid、replay 9/9、
  compileall 和 `git diff --check` 均通过。工作树 clean-package 仍按设计只因保留的未跟踪 `data/`
  运行数据和 handoff 文档而拒绝，干净 artifact 检查通过。
- 1.10 正式 Gateway CLI 同一 thread 连续两轮记住并准确回忆 `蓝松-729`；idle `/stop` 不改变历史，
  后续仍能回忆。另用既有 owner/conversation 的可信 localhost Feishu scope 创建 live request，
  `/stop` 得到 `interrupted/INTERRUPTED`，下一轮仍能正常聊天；这条只证明控制主链，不冒充客户端入站。
  CLI 目前仍要求从 Gateway 的 service cwd 运行才能定位同一 cwd-derived workspace；从 `/root`
  直接运行会误报 Gateway 未启动，这是既有 CLI 可用性缺口，不属于本轮会话语义修复。
- 两个真实飞书客户端账号的最终请求分别是
  `req_1785323824294_2105796_8`（owner A/thread `thread-74479991be1c4144`）和
  `req_1785323962358_2105796_9`（owner B/thread `thread-2cadb8bf610a41ff`）。两边模型回复、私有
  transcript、audit 和 sent receipt 都只落在自己的 owner；A/B 客户端分别看见
  `客户端-A729` / `客户端-B729`，服务日志各有一次 Feishu native send success。

### 2026-07-29 自动检查点话术与 standalone 假未完成状态收敛

- “明白，先写检查点记录当前进度”有两个底座来源：分段 `read_file` 后自动追加的
  `long-read-facts`，以及连续只读到固定轮次后追加的 `exploration_fuse`。两者都会把模型从当前工作
  拉去写阶段笔记；后者虽名为熔断，实际不阻断任何调用。本轮删除了两条运行路径、两份专用模块、
  对应配置、状态文件协议和死测试，不留下兼容分支。
- 本轮参照 会话运行时 的职责边界：`update_plan` 是模型按需显式调用的 TODO 工具，工具执行循环不会在
  每次读取后暗中要求更新计划。my-agent 仍保留真正必要的权限、Sandbox、参数 Schema、错误恢复、
  大输出外置和 Compact 门；删除的是工作风格注入，不是安全保护。默认 Prompt 同时改为优先直接更新
  真实目标文件，`task_progress`/草稿只在跨 Compact 确需恢复时按需使用，不再要求形式化阶段笔记。
- standalone CLI/本地 run 原先会创建 `RUNNING` 工作区，却没有在正常返回时统一落终态，后续索引容易
  把已结束运行显示成旧的未完成工作。本轮在所有 Compact 续接结束后的唯一返回点，根据结构化
  `runtime_status/runtime_reason` 原子写入 `DONE/FAILED/BLOCKED/CANCELLED` 和 timeline；不读模型正文、
  不扫描产物，也不影响 conversation task、显式 `/goal` 或 child 自己的生命周期。
- 聚焦回归及全量 pytest 已覆盖工具循环、原生工具协议、截断恢复、Compact、Gateway conversation、工作区和
  owner-scoped Sandbox。本机 8899 Qwen 真实 CLI 连续执行 `list_files -> read_file` 后只回答
  `# my-agent`，未出现检查点话术；echo 与真实模型的多次 standalone 工作区均为
  `DONE/ok/progress=1.0`。Ruff、导入边界、离线契约、代码体积、文档同步和差异检查均通过。
- 本机 `/opt/homebrew/bin/my-agent` 已改为指向当前
  `my-agent-main/.venv/bin/my-agent` 的符号链接；从仓库外目录运行时，Python 导入也解析到
  `my-agent-main/agent_py_agent`，不再经过旧同级 checkout 的 editable 映射。

### 2026-07-28 主代理完成表达、执行身份与调用观测（历史候选，普通清单软核对已于 2026-07-29 删除）

- 对照本机 终端交互 `7dc15d6c8fb0` 的 `TaskUpdateTool`，只复用其中 structural verification nudge
  的思路，没有移植 TaskCompleted hook、强制 verifier、完成硬门或目录验收器。根代理已有持久
  `task_progress` 且仍有 open item 时，第一版 plain final 会被当作可丢弃草稿；原工具循环追加
  结构化 `open_count` 软核对，下一轮模型仍持有正常工具，可读/更新清单或继续工作，成功响应后提醒
  立即移除。提醒按当前请求内累计的真实 `executed_tools` 进展段去重：没有新工具动作不会重复提示，
  提醒后若又执行了工具，后续 plain final 可再次核对。普通 task 不以可能过期的清单形成完成硬门；
  只有显式持久 `/goal` 的 open plan 保持
  `unfinished` 并沿既有 continuation 续跑。代码不判断正文是否含“全部完成”，也不从模型文字反向
  改变任务状态。
- sticky workspace 与 live execution 已分开：普通终态 task 不会被下一轮工具复活。纯聊天只继承 cwd；
  真正开始工作时在同一 cwd 上创建当前 request 的新 task id，并保存 `continued_from_task_id`。只有精确
  持久 `/goal` 允许原 task id 恢复。后台 claim 后还会重读精确 task link，若 `/stop` 或前台完成已先到达，
  就取消本次 claim 并退休对应 wake/policy，不再启动模型或工具。
- 子代理 `allowed_tools` 改为父 run 工具快照的严格子集：worker 固定去掉继续派工能力，coordinator
  也只有父代理已有该能力时才保留。child 携带父 conversation id 只用于 lineage、wake 和归档；
  `context_scope=task_local` 有自己的 runner lane，不会被主会话“已有执行器”误拦工作工具。模型调用账本
  现在分别记录 logical turn、物理 model retry 和 provider
  HTTP retry，并把失败/超时/最终状态及有界计数投影到 runtime facts 和内部 Gateway result；不记录 key
  或请求正文，观测失败也不影响真实调用。
- 提交 `28ba06c0` 已修复 sticky workspace 复用时四份当前执行投影仍保留旧 request/run/task identity
  的问题，并已部署 1.10。主代理软核对语义仍是当前工作树候选；聚焦回归已通过，完整门禁、提交、
  推送、最终 wheel 部署与同一真实 Feishu 长任务复验完成前，不把它写成已发布能力。

## 本轮参考核对

- 先查阅 `长期助手_contract_code_files.xlsx`、`通道运行时_contract_code_files.xlsx`、
  `终端交互_contract_code_files.xlsx` 缩小 Tool Gateway / permission 范围。
- 长期助手 实际源码核对 `agent/tool_executor.py` 与 `agent/tool_guardrails.py`：桥接/检索得到的
  真实底层工具名必须先进入统一 hook、guardrail 和 scope 判定，再 dispatch。
- 通道运行时 实际源码核对 `src/plugins/tools.ts`、`src/skills/runtime/tool-dispatch.ts` 与
  `src/mcp/plugin-tools-serve.ts`：动态工具仍由 host 注册和执行边界持有，不以模型文本作为权限事实。
- 终端交互 实际源码核对 `src/services/tools/toolExecution.ts`、`toolOrchestration.ts` 与
  `src/hooks/useCanUseTool.tsx`：正式执行路径在动作边界统一调用 permission decision。

本轮复用的是“动态工具也必须穿过不可绕过的 host 执行门”这一模式，不复制参考项目的工具数量或 UI。

普通对话收口另核对了 长期助手、会话运行时 与 `fable_my-agent-claw`：复用了稳定 thread、逐轮历史和
结构化工具续接；没有照搬 长期助手/会话运行时 的旧 goal 自动注入，也没有照搬 claw 的群聊首位发言人
owner 和双套 SOUL/USER 路径。当前权威顺序是基础系统规则、内置产品规则、单一 owner 人格/画像、
同会话历史、当前用户消息。

本轮原生附件与统一投递继续核对 通道运行时 的 per-run current channel/target、typed reply/media、
provider plugin 和 Feishu dispatcher/outbound，以及 长期助手 `tools/send_message_tool.py` 的通道无关
`send_message` + adapter 路由。项目采用“可信上下文与回复信封分离、一个统一消息工具、adapter 注册”
三个 chokepoint，不复制它们的工具数量、平台枚举或 `MEDIA:path` 正文标记。

P2 的参考文件和取舍见 `docs/design/P2_SCALE_MAINLINE.md`。

## 发布判定

发布前至少同时满足：

```bash
python3 -m pytest -q --tb=short
ruff check agent_py_agent scripts setup.py package_boundary_policy.py
python3 scripts/check_import_boundaries.py
python3 scripts/check_offline_contract_matrix.py --repo-root . --json
python3 scripts/check_code_size.py --mode strict --baseline CODE_SIZE_BASELINE.json
python3 scripts/check_doc_sync.py
python3 scripts/check_clean_package.py --mode worktree .
git diff --check
```

容器发布还必须在最终镜像执行完整 sandbox probe；制品发布必须对实际 wheel/tar 运行
`check_distribution_boundary.py <wheel>` 和 `check_clean_package.py --mode artifact`。工作树有用户运行数据时，clean-package 正确失败，
不得通过删除、忽略或缩小统计口径把失败改成绿色。
