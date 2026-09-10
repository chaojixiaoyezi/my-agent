# TUI 极限测试矩阵

> 名称整理：产品统一称 my-agent。历史检出路径使用 `${MY_AGENT_CHECKOUT}`，旧会话及测试目录用“历史…”占位；实际定位以对应提交和 request/run ID 的原始记录为准。本次未移动目录或重命名真实会话。

本文是 5 路真实 TUI 实验室的权威测试账本。它记录可复现操作和客观证据，不把模型自述、窗口标题动画、
测试数量或夹具结果冒充真实后端通过。测试机固定为 `192.0.2.13`，会话固定为 `<历史会话:replica>`、
`<历史会话:input>`、`<历史会话:render>`、`<历史会话:lifecycle>`、`<历史会话:isolation>`；禁止触碰 PID `830976`。

## 状态与轮转合同

- `PENDING`：尚未执行；`RUNNING`：正在执行；`PASSED`：期望不变量有客观证据；`FAILED`：已保存最短复现；
  `FIXED_LOCAL`：本地候选已通过聚焦测试但尚未真机复验；`RETEST_PENDING`：已部署或具备复验条件；
  `BLOCKED_PLATFORM`：当前环境没有对应客户端或平台，不能写成通过。
- 四条极限路每次巡检都读取 processing request、结构化事件和进程事实，不能只看 TUI 标题。任一路完成后，
  先保存最终 ANSI、request/run/thread ID、事件和资源状态，再立即领取下一条 `PENDING` 用例。
- 滚动部署或重启一次只动一条路，尽量保持另外三条继续运行。底座问题等待用户讨论时，无关路继续轮转。
- 单因素边界必须全部执行；普通组合使用两两覆盖；`resize × stream × scroll × selection × compact × reconnect`
  使用定向 3–6 因素组合；无限事件排列交给有 seed、能缩减、能重放的状态机测试。

## 来源索引

| 来源 | 原始依据 | 转成的测试风险 |
|---|---|---|
| SRC-FREE | 本机 `终端交互-main/src/ink/`、`src/components/PromptInput/`、`src/components/diff/`、`src/components/permissions/` | 参考界面、输入、终端、diff、权限的行为序列 |
| SRC-会话运行时-PENDING | [会话运行时 pending input preview](（外部资料链接已移出发布文档）) | pending 与 queue 固定预览、三行截断、拒绝后保留 |
| SRC-会话运行时-PASTE | [会话运行时 paste burst](（外部资料链接已移出发布文档）) 与 [chat composer](（外部资料链接已移出发布文档）) | 首字符延迟、快速字符、bracketed paste、大粘贴展开、立即 Enter |
| SRC-会话运行时-TEST | [会话运行时 AGENTS testing guide](（外部资料链接已移出发布文档）) | 完整对象断言、TUI snapshot、benchmark |
| SRC-PTK | [prompt_toolkit unit testing](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/advanced_topics/unit_testing.html) | `create_pipe_input`、`DummyOutput` 和应用状态断言 |
| SRC-TMUX-CONTROL | [tmux Control Mode](https://github.com/tmux/tmux/wiki/Control-Mode) | `%output`、慢客户端 flow control、控制客户端大小 |
| SRC-TMUX-ADV | [tmux Advanced Use](https://github.com/tmux/tmux/wiki/Advanced-Use) | `send-keys -H`、`capture-pane`、pane/window 状态 |
| SRC-HYPOTHESIS | [Hypothesis stateful testing](https://hypothesis.readthedocs.io/en/latest/stateful.html) | 随机动作序列、前置条件、不变量、最短失败序列与 seed |
| SRC-KITTY-KEY | [kitty keyboard protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/) | Esc/Alt 歧义、按下/重复/释放、模式栈及退出恢复 |
| SRC-KITTY-TERM | [kitty terminal protocol extensions](https://sw.kovidgoyal.net/kitty/protocol-extensions/) | focus、同步输出、剪贴板、扩展模式的启停恢复 |
| SRC-XTERM | [XTerm control sequences](https://invisible-island.net/xterm/ctlseqs/ctlseqs.html) | CSI/OSC/DCS/APC/PM/ST、鼠标、alternate screen、标题与剪贴板 |
| SRC-UNICODE | [UAX #9](https://www.unicode.org/reports/tr9/)、[UAX #11](https://www.unicode.org/reports/tr11/)、[UAX #15](https://www.unicode.org/reports/tr15/)、[UAX #29](https://www.unicode.org/reports/tr29/) | 双向文本、单元宽度、规范化、grapheme 边界 |
| SRC-OBSERVED | `.13` 的 tmux、Gateway request、chunks、日志和进程现场 | 当前产品的真实回归样本 |

公开 issue 只能提供待验证风险，不能单独证明本产品有同一缺陷。已纳入的风险包括：长多行粘贴导致 UI/传输
挂死、paste 后立即 Enter 被误路由、输入在会话操作期间重放、in-flight await 阻塞主事件循环，以及大粘贴
占位在 slash 命令提交前没有展开。

## 方法与治理矩阵

| 测试ID | 来源或风险依据 | 所属 TUI | 前置状态 | 操作序列 | 期望不变量 | 实际结果 | ANSI/截图/事件/日志/进程证据 | 问题所属层 | 修复状态 | 复验结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| OPS-001 | Goal 持续轮转 | 四路 | 四会话已启动 | 同时核对 request/事件/进程；完成路留证并续例 | 不因单例结束长期空闲；标题不作事实源 | 2026-08-18 23:08 发现三路完成，留证后续上 R3；四路恢复运行 | `/root/tui-extreme-lab/{input,lifecycle,isolation}/evidence/rotation/round-20260818-2308-final.ansi`；新 request 见各路行 | 测试编排 | PASSED | 下一次巡检继续执行 |
| OPS-002 | 隔离合同 | 五路 | 五套 runtime 已建 | 对照 owner/thread/task/workspace/evidence 和 PID | 五路 ID、目录和进程不串；不触碰 830976 | 已建立独立目录与 tmux；完整串线审计未结束 | tmux `list-panes -a`、各路 runtime 与 request tree | 编排/隔离 | RUNNING | PENDING |
| OPS-003 | 证据合同 | 五路 | 任一用例结束 | 保存 ANSI、events、request、资源、最短复现 | 模型最终文字不作为通过证据 | 已用于轮转 R2→R3 | 各路 `evidence/` | 测试治理 | RUNNING | PENDING |
| OPS-004 | SRC-TMUX-ADV / 观察者误窗 | lifecycle | 同会话含 TUI 与 Gateway 两个 window | 观察者误切 Gateway 后连续按方向键，再切回 TUI | 后台窗输入不得冒充 TUI 乱码；被测任务不中断；观察命令能明确回到 TUI | 真实复现成排 `^[[A/^[[B`；根因是 session 当前 window=1 的后台日志窗，不是 renderer；切回 window 0 后 TUI 正常、Python 任务仍活 | `/root/tui-extreme-lab/lifecycle/evidence/rotation/observer-on-gateway-window-escape-bytes.ansi`；`list-windows`/pane process | tmux harness | PASSED | `Ctrl-b 0` 或 `tmux select-window -t "$TUI_SESSION:0"` 可恢复；后续隔离后台窗 |
| M-001 | reducer 确定性 | 本地 | 固定事件序列 | 重放相同 typed events 多次及重复事件 | canonical snapshot 深比较完全一致 | focused reducer 用例已有，完整矩阵未执行 | `test_tui_runtime.py`、`test_tui_view_model.py` | TUI reducer | RUNNING | PENDING |
| M-002 | SRC-PTK | 本地 | app session + pipe input | 逐字、按键、paste、resize 输入；断言应用状态 | 不依赖 stdout 时序；输入状态精确 | 新增 bracketed paste 紧跟 CR 的完整 app 测试并通过；其余矩阵未全跑 | `test_tui_prompt_toolkit_pipe.py::test_bracketed_paste_followed_immediately_by_enter_submits_once` | 输入 | RUNNING | 0ms 路径 PASSED |
| M-003 | 真实 PTY | 本地/.13 | 固定 TERM/locale/尺寸 | PTY 启动、字节输入、信号、EOF、抓原始输出 | 可重放；退出 tty 状态恢复 | 基础 recorder/test 已存在，极限项未全跑 | `scripts/tui_pty_recorder.py`、`test_tui_pty.py` | 终端协议 | PENDING | PENDING |
| M-004 | SRC-TMUX-ADV | 五路 | 真实 tmux pane | `send-keys`、`-H`、paste、resize、capture、detach | 操作可重复且 pane 身份不漂移 | 正在日常使用，边界矩阵未完成 | tmux 控制日志 | harness/TUI | RUNNING | PENDING |
| M-005 | ANSI/cell snapshot | render/isolation | 固定事件与尺寸 | 录 ANSI，解析 cell，重放并深比较 | 样式变化不能改 canonical 文本/状态 | 基础脚本已存在 | `scripts/tui_ansi_snapshot.py`、`test_tui_ansi_snapshot.py` | 渲染 | PENDING | PENDING |
| M-006 | SRC-FREE 差分 | input/render | 相同尺寸与事件序列 | 分别驱动 终端交互 与本 TUI | 明确允许差异外，布局/按键/队列/折叠行为一致 | 参考会话已启动，完整序列未跑 | `ref-终端交互`、`ref-终端交互-probe`、`<历史会话:*>` capture | TUI parity | RUNNING | PENDING |
| M-007 | 变形测试 | 四路 | 固定逻辑事件 | 只改变 chunk、速度、resize、detach 时机 | canonical 状态、消息顺序、最终文本不变 | 未执行 | — | 多层 | PENDING | PENDING |
| M-008 | SRC-HYPOTHESIS | 本地 | 状态模型已定义 | 随机 input/scroll/resize/stream/permission/compact/reconnect | 不变量恒真；失败缩减并保存 seed | 测试文件已存在，覆盖审计未完成 | `test_tui_stateful.py` | 状态机 | PENDING | PENDING |
| M-009 | 两两组合 | 四路 | 单因素值表冻结 | 生成并跑 all-pairs | 每对参数值至少同例出现一次 | 未执行 | pairwise manifest 待生成 | 测试设计 | PENDING | PENDING |
| M-010 | 高风险多因素 | 四路 | 六类高风险动作 | 定向组合 resize/stream/scroll/selection/compact/reconnect | 无丢失、重复、越界、终端模式泄漏 | 未执行 | — | 多层 | PENDING | PENDING |
| M-011 | fixture + 真实后端 | 四路 | 同一黑盒序列 | fixture 故障注入后再用 MiniMax-M2.7 复验 | fixture/真实结果分栏；不得互相冒充 | 两类环境已建，完整一一复验未完成 | fixture audit、真实 request/chunks | provider/Gateway | RUNNING | PENDING |
| M-012 | 终端交叉 | input/render | 客户端可用 | macOS Terminal/iTerm2/kitty/WezTerm/SSH/tmux/ConPTY 分别复验 | 只标实际运行的平台 | 当前仅 SSH+tmux 已在跑 | TERM、客户端版本、PTY 录制 | 兼容性 | BLOCKED_PLATFORM | SSH+tmux RUNNING，其余 PENDING |
| M-013 | 性能与 soak | 四路 | 采样器就绪 | 长流、10k blocks、慢消费者、多客户端，周期采样 | UI 可响应；资源有界；退出无孤儿 | 未执行 | CPU/RSS/fd/thread/file-size 时序待建 | 性能 | PENDING | PENDING |
| M-014 | SRC-TMUX-CONTROL | render/isolation | control client 接入 | 暂停读取触发 flow control，再恢复 | 不丢控制事件；观察者不改变窗口尺寸 | 未执行 | `%output`/`%pause` 原始流待存 | harness/终端 | PENDING | PENDING |

## 输入、编辑、队列与鼠标矩阵（`<历史会话:input>`）

| 测试ID | 来源或风险依据 | 所属 TUI | 前置状态 | 操作序列 | 期望不变量 | 实际结果 | ANSI/截图/事件/日志/进程证据 | 问题所属层 | 修复状态 | 复验结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| I-001 | SRC-UNICODE | input | 空 composer | 逐项输入中文、日文、韩文 | grapheme 不拆、cell 宽度正确、提交原文一致 | R3 含三种文字，任务中 | request `gwreq-1787065770-13158663dda74d2a8aa13390dc025d0a` | 输入/Unicode | RUNNING | PENDING |
| I-002 | SRC-UNICODE | input | 空 composer | 输入阿拉伯文、希伯来文及混合 RTL/LTR/数字/标点 | cursor/选择/提交逻辑顺序正确且不丢字 | R3 含阿拉伯文和希伯来文，精确回读待验 | 同 I-001 | 输入/Bidi | RUNNING | PENDING |
| I-003 | UAX #15/#29 | input | 空 composer | 输入组合音标、NFC/NFD、零宽连接/非连接/空格 | 不按 code point 错删；提交不擅自规范化 | R3 含 `é`/`é`，零宽项未执行 | 同 I-001 | 输入/Unicode | RUNNING | PENDING |
| I-004 | UAX #11/#29 | input | 空 composer | Emoji ZWJ 家庭、肤色、旗帜、变体选择符 | 一个 grapheme 的移动/删除/选择不撕裂；宽度稳定 | R3 含家族/肤色/旗帜/心形，交互待验 | 同 I-001 | 输入/Unicode | RUNNING | PENDING |
| I-005 | 宽度边界 | input | 多尺寸 | 全角/半角、制表符、控制图片、超长单行 | 不越界；tab 行为明确；横向内容可达 | 本轮 tab 被外层交互 shell 吞为 BEL，属于 harness 复现失败 | SSH shell 原始回显 | harness | FAILED | 改用 `send-keys -H` 后重测 |
| I-006 | 基础提交 | input | 空闲 | 提交空串、空格、全角空格、仅换行 | 不创建空 request；草稿行为一致 | 未执行 | — | 输入 | PENDING | PENDING |
| I-007 | 换行编码 | input | 空 composer | 分别注入 LF、CR、CRLF、多行末尾无换行 | composer 语义一致；提交正文精确 | 未执行 | — | 输入/终端 | PENDING | PENDING |
| I-008 | SRC-会话运行时-PASTE | input | 空 composer | 单个 ASCII 首字符；慢速第二字；快速两字；modified key | 正常打字无可感延迟；仅 paste-like burst 缓冲 | 未执行 | raw key timing 待存 | 输入/paste detector | PENDING | PENDING |
| I-009 | 键盘时序 | input | 空 composer | 快速连按 Enter/Backspace/Delete/Tab；按键边界拆包 | 每个 intent 至多一次；不把 Enter 当 paste | 未执行 | — | 输入 | PENDING | PENDING |
| I-010 | SRC-KITTY-KEY | input | legacy 与增强键盘模式 | Esc 单独、Esc+字符、Alt、超时前后输入 | Esc/Alt 不混淆；退出恢复原模式栈 | 未执行 | 原始 PTY bytes/tty flags | 终端输入 | PENDING | PENDING |
| I-011 | 软折行导航 | input | 3+ 行和超宽软折行 | Up/Down、Left/Right、Home/End、Ctrl-A/E | 行内导航优先；只有边界才进入历史 | 已知 Down 不能去第2/3视觉行；待最短复现 | 用户现场截图/待 PTY 录制 | 输入导航 | FAILED | PENDING |
| I-012 | 历史边界 | input | 有多条历史与多行草稿 | Up/Down 穿过历史；回到末尾；修改后再导航 | 草稿不丢；末条 Down 回当前草稿；不遮住新消息提示 | 用户报告末行 Down 不能看 `N new messages`，待复现 | 用户现场 + PTY 待录 | 输入/滚动 | FAILED | PENDING |
| I-013 | Ctrl 系列 | input | 空闲/运行/选区/overlay | Ctrl-C/D/O/R/L/U/K/W/Y/Z 等 | 每键只在对应状态生效；不误杀/误复制 | Ctrl-O 展开已有基础；全矩阵未跑 | TUI capture/PTY 待存 | keybinding | PENDING | PENDING |
| I-014 | IME | input | 中文/日文输入法组合中 | composition 更新、确认、取消、resize、焦点切换 | 未确认 composition 不提交或拆分 | 当前远端无 GUI IME | — | 平台兼容 | BLOCKED_PLATFORM | 本机客户端待测 |
| I-015 | 短 paste | input | 空 composer | 粘贴单行/多行，结束后一次 Enter | 正文精确且只建一个 request | 真实 tmux 0ms paste+Enter 成功且只建一条 request；本地完整 app pipe 同样通过 | request `gwreq-1787068687-a53b413fbba74f29b2eafdd4b77ff954`；`paste-immediate-enter-*.ansi`；M-002 | 输入 | PASSED | PTY/application 两层已过 |
| I-016 | 公开 paste/Enter 风险 | input | bracketed paste | payload 后 0/1/8/30/100/500/5000ms Enter | 有界退出 paste 状态；一次 Enter 一次提交 | 0ms 短 paste 与 1,649 字符/36 行长 paste 均一次提交；先前“需第二次 Enter”未稳定复现，不能据此改生产状态机 | requests `gwreq-1787068687-a53b413fbba74f29b2eafdd4b77ff954`、`gwreq-1787068941-2ed4288876f04b8c9af382ee858fc35b`；M-002 | 输入状态机 | RUNNING | 0ms PASSED，其余时序 PENDING |
| I-017 | 大粘贴 | input | 空 composer | 1k/100k/1M 字符及大 Markdown，多次 Enter | UI 不冻结；有上限/占位；提交精确；内存可回落 | 1,649 字符/36 行已触发折叠并立即提交，100k/1M 与资源回落未跑 | `large-paste-immediate-enter.ansi`；request `gwreq-1787068941-2ed4288876f04b8c9af382ee858fc35b` | 输入/传输 | RUNNING | 1k PASSED；100k/1M PENDING |
| I-018 | 不完整 paste | input | 空 composer | 嵌套、重复、残缺 begin/end bracketed-paste 序列 | 不永久卡 paste；后续正常键可恢复 | 未执行 | raw bytes | 终端输入 | PENDING | PENDING |
| I-019 | slash paste | input | 空 composer | 粘贴 `/context`、`/compact`、`/goal` 与正文中 slash | 只有明确单条命令走 dispatcher；大 paste 提交前精确展开 | R3 正文含 `/compact` 并已作为普通任务提交 | request/chunks 待核对原文 | 命令解析 | RUNNING | PENDING |
| I-020 | 控制字符 paste | input | 安全夹具 | NUL/BEL/ESC/DEL/C0/C1 与无效 UTF-8 | 不控制宿主；显示/拒绝策略明确；日志不注入 | 未执行 | raw ANSI + sanitized event | 输入/安全 | PENDING | PENDING |
| I-021 | active steer | input/lifecycle | 长任务运行中 | 连续输入两条普通补充 | 同一 active turn 精确注入；不创建下一 ChatJob；不丢不重 | 前两条均在同一 request 以精确 client ID 消费并成为稳定 user block；第三条也被 runtime 消费，但暴露客户端响应丢失竞态，见 I-030 | guidance `guidance-4786995bbcab4bf1`/`guidance-6ae43330973e4c9d`；client `steer-3ce85df7a6fc48b3`/`steer-0abb4bc5c6114881`；chunks | Gateway/TUI | RUNNING | 注入主链 PASSED；未知结果竞态 FAILED |
| I-022 | SRC-会话运行时-PENDING | input | active steer pending | 滚离底部、resize、再有新消息 | pending 固定在 composer 上方，最多3行；不被 transcript 滚走 | 真机已看到固定 `Messages to be submitted after next tool call` 三行预览，滚离 transcript 后仍在 composer 上方 | `active-steer-second-submit.ansi` | TUI 投影 | PASSED | `.13` PASSED |
| I-023 | rejected steer | input | turn 在提交竞态结束 | submit 正好撞 final；Gateway 拒绝 | 仅同 ID pending 撤销并恰好排入一次 follow-up queue | 本地单测通过 | `test_tui_input.py`、`test_tui_runtime.py` | Gateway/TUI | FIXED_LOCAL | `.13` RETEST_PENDING |
| I-024 | 外部 ID/重复 receipt | input | 有本地 pending A/B | 注入未知 ID、重复 A、乱序 B/A | 未知 ID 不删本地；每条只提升一次且顺序可解释 | 本地单测覆盖未知 ID/精确提升 | `test_tui_runtime.py` | reducer/身份 | FIXED_LOCAL | `.13` RETEST_PENDING |
| I-025 | 鼠标基础 | input | mouse on/off | 单击定位、双击、正向/反向拖选、跨行拖选 | 选择不变成全屏；宽字符边界正确 | 用户报告鼠标自动框所有字符，待 PTY 最短复现 | 用户截图 + 待录 mouse bytes | mouse/终端 | FAILED | PENDING |
| I-026 | 鼠标边界 | input | 长行、多行、CJK | 拖出窗口、resize 中拖、快速滚轮、选择后 Ctrl-C | 不捕获失控；选择可复制；UI 状态不损坏 | 未执行 | — | mouse/scroll | PENDING | PENDING |
| I-027 | tmux mouse | input | tmux mouse on/off | 普通拖选、Shift 绕过、两个只读观察者 | TUI 与 tmux 选择权明确；观察者不改变被测尺寸 | 未执行 | tmux options + client size | harness/terminal | PENDING | PENDING |
| I-028 | completion/history | input | `/`、路径、历史均有候选 | 上下/Tab/Enter/Esc，候选为空/很多/超长 | menu、history、soft-line navigation 不抢键 | 基础单测已有，真机极限未跑 | `test_tui_input.py` | 输入 | PENDING | PENDING |
| I-029 | fork/session 操作时输入 | input | resume/fork/compact 正在切换 | 快速 Esc/Esc/Enter、paste、普通字符 | 输入不重放到新 thread；不会链式创建会话 | 未执行 | event/request/thread tree | 会话/TUI | PENDING | PENDING |
| I-030 | active steer 响应丢失 | lifecycle | Gateway 已接受但 HTTP 客户端 2s 内未拿到响应 | 提交普通补充，服务端追加并消费；客户端把 timeout 当 False | unknown 不得等同 rejected；同一正文不能再进入 follow-up queue | 真实发生：`steer-e10ecb4542a94960` 已在 chunk 事件消费并进入当前 turn，但 TUI 又显示为 queued；测试者用 Up 回取并清空，避免实际双执行 | `active-steer-timeout-duplicate-queue.ansi`；chunk event line 19 | control/guidance idempotency | FAILED | 推荐 accepted/rejected/unknown + `channel_message_id` 幂等；待用户确认底座方案 A |

## 消息、Markdown、tool、diff 与滚动矩阵（`<历史会话:render>`）

| 测试ID | 来源或风险依据 | 所属 TUI | 前置状态 | 操作序列 | 期望不变量 | 实际结果 | ANSI/截图/事件/日志/进程证据 | 问题所属层 | 修复状态 | 复验结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| R-001 | 空/极短输出 | render | fixture | 空 message/chunk、仅换行、无 finish | 不崩溃、不造幽灵 block；终态可解释 | 未执行 | — | reducer/render | PENDING | PENDING |
| R-002 | 长消息 | render | fixture | 超长段落、超长不换行、无末尾换行 | cell 裁剪正确；全文在 transcript 可达 | 当前 R3 真实任务运行中，专项未执行 | request `gwreq-1787064766-7aec4b1ee30b44c193fb98ec9436b1ca` | render | RUNNING | PENDING |
| R-003 | 高数量 | render | fixture | 10,000 消息/稳定 blocks | append/scroll 有界；ID 不碰撞；CPU/RSS 可量化 | 未执行 | benchmark/profile | reducer/perf | PENDING | PENDING |
| R-004 | 巨大 tool 输出 | render | fixture | 100k 行、1M 长行、二进制样式、stdout/stderr 交错 | 预览有界可展开；不饿死输入；完整证据外置 | 未执行 | chunks/artifact/RSS/latency | tool/render | PENDING | PENDING |
| R-005 | CommonMark 基础 | render | 多尺寸 | 标题、强调、引用、列表、分隔线、链接 | 文本/样式与 终端交互 允许差异表一致 | 基础单测已有，差分未全跑 | `test_tui_markdown.py` | markdown | PENDING | PENDING |
| R-006 | Markdown 极限 | render | 多尺寸 | 深层嵌套、表格、未闭合 fence、未知语言、HTML、长 URL | 不丢正文、不越界、不把后续消息吞进 fence | R3 任务覆盖部分，专项待验 | render capture | markdown | RUNNING | PENDING |
| R-007 | chunk 边界 | render | 固定 Unicode/Markdown 文本 | 在每字节、code point、grapheme、Emoji、Markdown token 边界切流 | 合并后 canonical 文本完全相同 | 未执行 | property manifest/snapshots | stream parser | PENDING | PENDING |
| R-008 | chunk 异常 | render | fixture | 空、重复、乱序、迟到 chunk | 幂等去重；顺序规则显式；不污染下一段 | 未执行 | event journal | protocol/reducer | PENDING | PENDING |
| R-009 | 生命周期缺口 | render | fixture | 缺 start、缺 finish、finish 重复、final 后迟到 | 不挂死；终态单一；迟到事件有账可查 | 未执行 | event journal | reducer | PENDING | PENDING |
| R-010 | event 交错 | render | fixture | thinking/commentary/tool/permission/retry/final 全排列样本 | 类型和块顺序稳定；final 不吞前块 | 未执行 | replay seed | reducer | PENDING | PENDING |
| R-011 | thinking 折叠 | render | 有短/长 thinking | Ctrl-O 展开/折叠，流中操作，resize | 默认摘要、可展开；状态不受新 chunk 重置 | 已有界面，极限序列未跑 | pane capture | TUI projection | PENDING | PENDING |
| R-012 | tool 折叠 | render | 成功/失败/运行中 tool | 展开、收起、长参数/输出、退出码 | 工具名/状态/摘要持续可见；详情可达 | 基础已展示，专项未跑 | pane capture | TUI projection | PENDING | PENDING |
| R-013 | diff 新增/删除 | render | fixture patch | 新文件、删文件、空文件、无 newline | 红绿行/行号/统计准确，空文件可见 | 用户要求的高亮差异本地已实现基础 | snapshot/tests | diff render | RUNNING | PENDING |
| R-014 | diff 复杂 | render | fixture patch | 重命名、多文件、二进制、巨大 diff、同名路径 | 不伪造文本 diff；折叠与统计准确 | 未执行 | snapshots/artifacts | diff render | PENDING | PENDING |
| R-015 | stdout/stderr | render | shell fixture | 交错小块、无换行、颜色码、非零退出 | 通道/顺序/退出码可辨；控制码不执行 | 未执行 | raw stream + canonical events | tool protocol | PENDING | PENDING |
| R-016 | follow/unseen | render | 滚离底部 | 持续注入消息，点击/按键回尾 | 锚点不跳；unseen 精确；回尾清零 | 用户报告无法滚回历史/看到新消息，待系统复现 | 用户现场 + PTY 待录 | scroll/view | FAILED | PENDING |
| R-017 | resize anchor | render | 中间历史锚点 | 多次 79↔80↔81、宽窄 storm | 同一逻辑内容保持视野；unseen 不被误清 | 未执行 | cell snapshots | scroll/view | PENDING | PENDING |
| R-018 | 搜索 | render | 长历史和折叠块 | Ctrl-R、上下命中、Unicode、隐藏详情 | 命中数/位置准确；退出回原锚点 | 基础实现已有，极限未跑 | `test_tui_transcript.py` | transcript | PENDING | PENDING |
| R-019 | compact 边界 | render | 长会话 | compact 前/中/后滚动、搜索、恢复 | 历史代际可解释；当前 active blocks 不丢 | durable compact 与 active-turn compact 已区分，本地候选待真机 | chunks/context events | conversation/TUI | FIXED_LOCAL | `.13` RETEST_PENDING |
| R-020 | context strip | render | active turn | token 使用变化、窗口变窄、compact | 仅结构化数字；实时更新；不泄露正文 | 真机随模型轮更新为 `~18.2k/128.0k · 14% · compact 90% · prompt 9.3k · messages 885 · tools 8.0k`，未携带正文 | `active-steer-two-consumed.ansi`；typed usage chunks | runtime/TUI | PASSED | `.13` PASSED；compact 事件仍待阈值触发 |
| R-021 | final 宣称与合同相悖 | render/replica | 产物显著不完整 | 模型说完成但结构化验收失败 | UI 展示返工原因并让模型续做；不粗暴截断/误完成 | Fiber 旧产物曾提前完成；当前长任务继续运行 | replica chunks/artifact audit | Agent closeout | RUNNING | PENDING |
| R-022 | stalled await | render | provider/tool await 挂起 | 同时输入 Esc、滚动、help、resize | UI 主循环仍响应；允许中断并留证 | 未执行 | heartbeat/stack/PTY | event loop | PENDING | PENDING |
| R-023 | 工具 Schema 与模型调用相悖 | lifecycle | 模型调用 `task_progress` | 模型发送带 `todos` 的调用，Registry 校验后继续 | Schema 必须与模型可见定义一致；确定性参数错可返工且不冒充完成 | 真机被 `$.todos: 未声明字段` 拦截，模型随后绕过清单直接写代码；是否为 provider/schema 生成问题待对照 会话运行时 与当前工具 spec | `active-steer-two-consumed.ansi`；request `gwreq-1787067879-f0924c6fcf464b148a7bd840b2533f43` | ToolRuntime/模型工具协议 | FAILED | 底座候选，先讨论再改 |

## 启停、断线、重试、compact 与恢复矩阵（`<历史会话:lifecycle>`）

| 测试ID | 来源或风险依据 | 所属 TUI | 前置状态 | 操作序列 | 期望不变量 | 实际结果 | ANSI/截图/事件/日志/进程证据 | 问题所属层 | 修复状态 | 复验结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| L-001 | 启动 SLA | lifecycle | 冷/热 Gateway | 连续测冷启动、Gateway 已就绪、并发第2客户端 | 优秀 1–2s，最多 3–4s 可输入；阶段可见 | 用户曾观察 >1 分钟；当前未完成分阶段量化 | `scripts/tui_startup_probe.py` 待跑 | bootstrap/Gateway | FAILED | PENDING |
| L-002 | 慢启动 | lifecycle | fixture 延迟 readiness | 0/1/2/4/10/60s 后 ready | TUI 立即出现且显示真实阶段；有界等待/重试 | 未执行 | preflight events | bootstrap | PENDING | PENDING |
| L-003 | 启动失败 | lifecycle | 端口占用/配置错/依赖缺 | 启动并观察退出/重试 | 快速、可解释、可重试；tty 恢复 | 未执行 | stderr/exit/tty | bootstrap | PENDING | PENDING |
| L-004 | Ctrl-C 多态 | lifecycle | 空闲/thinking/tool/final | 各阶段一次/连按 Ctrl-C | 空闲退出、运行中精确中断、无重复 final | 未执行 | control events/request status | control | PENDING | PENDING |
| L-005 | Ctrl-C 特殊态 | lifecycle | permission/selection/overlay | 中断时有选区或权限框 | 复制/取消/中断优先级明确，不误杀 | 未执行 | raw key + events | key/control | PENDING | PENDING |
| L-006 | Ctrl-D/EOF | lifecycle | 空输入/有草稿/运行中 | 发送 EOF | 不丢草稿/任务；退出语义明确且 tty 恢复 | 未执行 | PTY/exit | lifecycle | PENDING | PENDING |
| L-007 | POSIX 信号 | lifecycle | 各活动阶段 | SIGTERM、SIGHUP、SIGWINCH、SIGTSTP/SIGCONT | 状态落账；恢复/退出一致；无孤儿 | 未执行 | process tree/tty/events | lifecycle | PENDING | PENDING |
| L-008 | SSH/tmux | lifecycle | 长任务运行 | detach/reattach、SSH 断线、两观察者 | 任务继续；重新观察不改尺寸/状态 | tmux detach 日常可用，断线矩阵未跑 | tmux clients/request heartbeat | harness/TUI | RUNNING | PENDING |
| L-009 | Gateway restart | lifecycle | thinking/tool/final 三阶段 | 每阶段滚动重启该路 Gateway | request 不丢/不重复；恢复语义明确 | 未执行 | PID/request/chunks/lease | Gateway | PENDING | PENDING |
| L-010 | 连接拒绝/超时 | lifecycle | fixture | refused、connect/read timeout、半开 | 分类准确；UI 活着；进入有界重试 | 旧定向测试覆盖部分，真机矩阵未跑 | retry events/timestamps | provider/Gateway | PENDING | PENDING |
| L-011 | 响应损坏 | lifecycle | fixture | 截断 JSON/SSE、坏 UTF-8、缺 finish、超大 frame | 可恢复或明确失败，不生成伪 final | 未执行 | raw response/parser events | provider parser | PENDING | PENDING |
| L-012 | 限流/服务错误 | lifecycle | fixture | 429、Retry-After、5xx、连续失败后恢复 | 仅可重试类退避；尊重边界；恢复一次 | 未执行 | attempt timeline | retry | PENDING | PENDING |
| L-013 | 有界退避 | lifecycle | fixture 时钟 | 失败到成功、一直失败、停止期间 | 延迟符合 `2/5/15` 与 `10/25/45/100/180` 所属层；有 jitter/上限；stop 后不重试 | 现有 focused 有基础，真实 UI 未跑 | retry typed events | retry | PENDING | PENDING |
| L-014 | 自动 compact | lifecycle | 长会话逼近阈值 | 连续 active turn 与多个 completed turns | durable 只压完整前缀；active-turn 收缩有独立可见事件 | 第一真实长轮达到约 67k/128k（52%）仍低于 90%，generation=0 符合策略；已在同 thread 发起下一长轮继续逼近阈值 | lifecycle context strip/chunks；新 request `gwreq-1787069069-36eaa996ddae4b4a93dfdb7a7590b9fd` | context runtime | RUNNING | 等待真实 90% 自动 compact |
| L-015 | 手动/自动竞争 | lifecycle | 接近阈值 | `/compact` 与自动触发同时、重复点击、断线 | 仅一次 CAS 提交；代际单调；失败可恢复 | 未执行 | compact ledger/events | conversation | PENDING | PENDING |
| L-016 | compact 与 active lane | lifecycle | task 正运行 | `/compact`、active turn、pending steer 交错 | 不压未完成事实；输入不丢；状态可解释 | 未执行 | thread/request/compact ledger | conversation | PENDING | PENDING |
| L-017 | compact 恢复 | lifecycle | compact 中 | Gateway/SSH 断线后重启/resume | checkpoint 可恢复；无双 generation/断链 | 未执行 | checkpoint/CAS audit | conversation | PENDING | PENDING |
| L-018 | slash 并发 | lifecycle | active turn | `/status`、`/stop`、`/btw`、`/goal`、`/context`、`/compact`、`/effort` 快速交错 | 每条走明确结构化 command；普通正文不误触发 | `/context` 等基础存在性仍需完整对照 | command events | command/control | PENDING | PENDING |
| L-019 | 终端清理 | lifecycle | 每种退出/崩溃路径 | 退出后检查 stty、cursor、mouse、paste、alt screen、keyboard mode | shell echo/光标/模式全部恢复 | 光标下划线用户曾报告；本地 BLOCK cursor 候选 | `stty -a`、mode query、PTY | terminal cleanup | FIXED_LOCAL | `.13` RETEST_PENDING |
| L-020 | 长任务续跑 R3 | lifecycle | R2 已结束 | 自然语言要求 30 批、断点、失败、停止、重启 | 被测 Agent 自己实现并验证；测试者不补产物 | 旧轮已终态并保存；新轮继续做数千任务、十次随机中断/损坏/双启动 | 旧 request `gwreq-1787067879-f0924c6fcf464b148a7bd840b2533f43`；新 `gwreq-1787069069-36eaa996ddae4b4a93dfdb7a7590b9fd` | Agent/产物 | RUNNING | 新轮 PENDING |
| L-021 | final race + steer | lifecycle | 长任务临近 final | 连续两条普通补充，并让一条撞 final | 接受的进 active turn；明确拒绝的恰好进一个下一 turn；unknown 不转队列 | 两条明确接受主链通过；第三条服务端接受但客户端 timeout 后误排队，证明二值客户端不满足不变量 | exact IDs 见 I-021/I-030 | Gateway/control | FAILED | 待底座方案 A；明确 rejected 分支仍需复验 |
| L-022 | resume 代际 | lifecycle | 多次中断/compact | resume、再输入、再次中断 | thread/task/request 身份保持；历史无重复/缺口 | 未执行 | thread ledger/events | conversation | PENDING | PENDING |
| L-023 | 观察者误入后台窗 | lifecycle | tmux session 有 `tui`/`gateway` 两窗 | 第四个观察终端停在 window 1 并按 Up/Down | 不把观察 harness 的后台 stdin 回显误报成产品乱码；切回不影响任务 | window 1 留下大量 `^[[A/^[[B`，window 0 同时正常；切回 0 后任务继续 | OPS-004 证据；pane `%31` Python alive | harness | PASSED | 已恢复；后续观察默认锁定 window 0 |

## 并发、隔离、安全、尺寸与性能矩阵（`<历史会话:isolation>`）

| 测试ID | 来源或风险依据 | 所属 TUI | 前置状态 | 操作序列 | 期望不变量 | 实际结果 | ANSI/截图/事件/日志/进程证据 | 问题所属层 | 修复状态 | 复验结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| S-001 | 五路并发 | isolation | 5 session active | 同时产生消息/tool/permission/history/queue/compact/stop | 任何投影和控制只作用目标 session | 当前五路独立运行，完整字段对账未完成 | tmux panes + runtime trees | identity/isolation | RUNNING | PENDING |
| S-002 | owner/thread 组合 | isolation | 可建测试身份 | 同 owner 不同 thread、不同 owner 同文本 | owner/thread/task/workspace refs 精确隔离 | 未执行 | request payload/paths | identity | PENDING | PENDING |
| S-003 | 重复 request | isolation | 固定 idempotency key | 并发提交同 request、重放完成事件 | 工具/产物/用户消息至多一次 | R3 Agent 任务含重复提交，底座专项未跑 | request/event ledger | Gateway | PENDING | PENDING |
| S-004 | 乱序/迟到事件 | isolation | fixture | 打乱 start/delta/finish/final，跨 request 注入 | 按 request identity 收敛；不得串到邻会话 | 未执行 | fixture audit/journal | protocol/reducer | PENDING | PENDING |
| S-005 | workspace/artifact | isolation | 五路同名文件 | 同时写相同相对路径、登记同名 artifact | 实际路径和归属不串；越界 fail closed | 未执行 | inode/path/artifact refs | workspace | PENDING | PENDING |
| S-006 | cache/temp/history | isolation | 五路并发 | 检查 cache、临时、history、compact 文件 | owner/task canonical path 唯一，无交叉内容 | 未执行 | file hash/path audit | storage | PENDING | PENDING |
| S-007 | CSI 注入 | isolation | untrusted fixture | 清屏、移光标、颜色、erase、private mode 完整/残缺/超长 | 只作为安全文本/样式；不能移动宿主或改模式 | fixture server已准备，完整矩阵未跑 | `terminal-control-fixture-audit.jsonl` | terminal security | RUNNING | PENDING |
| S-008 | OSC 注入 | isolation | untrusted fixture | 标题、OSC 8、OSC 52、颜色；BEL/ST 终止及残缺 | 不改标题/链接目的/剪贴板，不泄露秘密 | 未执行 | title/clipboard query/raw ANSI | terminal security | PENDING | PENDING |
| S-009 | DCS/APC/PM | isolation | untrusted fixture | 完整、嵌套、残缺、超长 payload | 不触发 terminal capability；解析有界 | 未执行 | raw ANSI/RSS | terminal security | PENDING | PENDING |
| S-010 | C0/C1/无效 UTF-8 | isolation | fixture | BEL/BS/CR/DEL/C1/invalid bytes | 不响铃/回退/移光标；替换策略稳定 | 未执行 | bytes/cell snapshot | terminal security | PENDING | PENDING |
| S-011 | terminal width 边界 | isolation | 固定 height 24 | 宽度 1,2,3,10,20,40,58,79,80,81,119,120,121,200 | 不崩溃/负宽/无限循环；内容可恢复 | 未执行 | per-size ANSI/canonical hash | layout | PENDING | PENDING |
| S-012 | terminal height 边界 | isolation | 固定 width 120 | 高度 1,2,3,5,8,24,29,36,80 | composer/状态/overlay 优先级明确；无异常 | 未执行 | per-size ANSI | layout | PENDING | PENDING |
| S-013 | resize storm | isolation | 流式长输出 | 每 5–50ms 随机变宽高 10k 次 | 无 race/crash；最终状态等于不 resize 对照 | 未执行 | seed/events/CPU | layout/event loop | PENDING | PENDING |
| S-014 | rapid input/wheel | isolation | 长历史 | 按键自动重复、快速滚轮、同时流入 | UI 可中断；计数/锚点有界正确 | 未执行 | PTY timing/latency | input/scroll | PENDING | PENDING |
| S-015 | 10k blocks | isolation | fixture | append/expand/search/compact/退出 | p95 redraw/输入延迟有记录；内存不无界 | 未执行 | benchmark/RSS | performance | PENDING | PENDING |
| S-016 | 长时间流 | isolation | 2h/8h soak | 稳定/突发/静默交替，周期 resize/detach | 无 freeze/泄漏；event/file 增长受控 | 未执行 | time series | performance | PENDING | PENDING |
| S-017 | 慢消费者 | isolation | tmux control client | 不读输出、跨 pause-after，再恢复 | Agent 不被观察者阻塞；可检测丢流并补 capture | 未执行 | control protocol raw log | harness/perf | PENDING | PENDING |
| S-018 | 多客户端观察 | isolation | 2–4 client | 不同客户端大小、只读 attach/离开 | 被测 window size 不变；输入客户端唯一 | 未执行 | `list-clients`/window-size | harness | PENDING | PENDING |
| S-019 | 资源回收 | isolation | 每类退出后 | 采 CPU/RSS/fd/thread/queue/file/PID | 回到基线范围；无孤儿/句柄增长 | 未执行 | `/proc` 时序 | runtime/perf | PENDING | PENDING |
| S-020 | 文件系统恶意名称 | isolation | R3 Agent 任务 | 空格/CJK/Emoji/`../`/绝对路径/超长/符号链接 | fail closed 于 workspace；不以文本特判 | RUNNING | request `gwreq-1787065771-2344a21842774d798f6fc699896acdb1` | Agent/tool sandbox | RUNNING | PENDING |
| S-021 | 权限并发 | isolation | 多会话 permission overlay | 同时 allow/deny/timeout/关闭/重复 decision | decision 绑定 request/tool；不能跨 pane 消费 | 未执行 | approval records/events | permission | PENDING | PENDING |
| S-022 | secret/redaction | isolation | canary secret | 放入模型/tool/错误/terminal control payload | UI/日志/证据只出现脱敏值；OSC52 不能复制 | 未执行 | canary scan | security | PENDING | PENDING |
| S-023 | symlink/TOCTOU | isolation | workspace 内外路径 | 校验后换链接、并发 rename、权限决定期间替换 | 实际操作时再次约束；越界无副作用 | 未执行 | inode/path/tool ledger | sandbox | PENDING | PENDING |

## Fiber 复刻与 Agent 产物矩阵（`<历史会话:replica>`）

| 测试ID | 来源或风险依据 | 所属 TUI | 前置状态 | 操作序列 | 期望不变量 | 实际结果 | ANSI/截图/事件/日志/进程证据 | 问题所属层 | 修复状态 | 复验结果 |
|---|---|---|---|---|---|---|---|---|---|---|
| P-001 | 冻结上游 | replica | 干净目录 | 记录 gofiber/fiber commit、tag、文件与功能行 | 对照基线不可漂移 | 原版初步统计 169 个生产 Go 文件、约 27,354 功能行；commit 证据待归档 | 上游 checkout/hash 待归档 | 测试治理 | RUNNING | PENDING |
| P-002 | 单次自然语言任务 | replica | 真后端 | 只输入 Goal 指定中文需求后观察 | 测试者不修改产物、不代跑修复 | 长任务已运行 1h+、267+ tool rounds | request `gwreq-1787058767-efe3906cde2041fa8d882b2771f6d8bd` | Agent E2E | RUNNING | PENDING |
| P-003 | 文件/功能规模 | replica | Agent 终态 | 统计源码文件、功能行、空壳/noop/fixed return | 重要公开能力缺失即不等价 | 旧快照仅 5 src/约1,093 功能行，显著不足；当前产物待结束重算 | artifact tree/hash | 产物 | FAILED | 当前长任务 RUNNING |
| P-004 | API/模块 | replica | Agent 终态 | 路由、context、req/res、中间件、client、binder、hooks、state、services、static、errors逐项对照 | 每项有实现+测试+示例或明确未完成 | 旧产物缺 client/binder/hooks/state/services 等 | parity audit 待更新 | 产物 | FAILED | PENDING |
| P-005 | 构建/测试/示例 | replica | 干净环境 | 安装锁定依赖、build、tests、启动示例、HTTP 黑盒 | 退出0与行为覆盖分开；无 open handle | 旧产物 18 Jest 通过但有 open-handle warning | command logs | 产物 | FAILED | PENDING |
| P-006 | 安全与语义 | replica | build 成功 | 路径、header/body、并发、错误、静态文件边界 | 不以 stub 假过；行为与基线契约一致 | 当前未独立审计 | HTTP traces/tests | 产物 | PENDING | PENDING |
| P-007 | 交付卫生 | replica | Agent 终态 | 检查 README、lock、examples、tests、缓存/临时/隐藏文件、体积 | 交付可重现且无垃圾/秘密 | 未执行最终检查 | manifest/hash/size | 产物 | PENDING | PENDING |
| P-008 | active-turn context | replica | 超长单 turn | 观察 usage、pair removal、durable compact | active turn 收缩可见；durable generation 语义不混淆 | 旧 generation=0 且 pair removal 仅 INFO；本地候选增加 typed 数字事件 | chunks/thread/model ledger | runtime/TUI | FIXED_LOCAL | `.13` RETEST_PENDING |
| P-009 | 完成/返工 | replica | closeout 判断与模型结论冲突 | 保留模型结论、结构化缺口和返工事件 | 先把差异与证据给模型返工；硬门仅安全/事实 | 当前长任务继续，最终出口待验 | chunks/closeout ledger | Agent closeout | RUNNING | PENDING |

## 当前运行快照

| 会话 | 当前轮 | request | 状态 | 下一次领取方向 |
|---|---|---|---|---|
| `<历史会话:replica>` | Fiber→TypeScript 完整复刻 | `gwreq-1787058767-efe3906cde2041fa8d882b2771f6d8bd` | RUNNING | 终态后执行 P-003…P-009 全量独立审计 |
| `<历史会话:input>` | 1,649 字符多行旅行清单，大 paste 后立即 Enter | `gwreq-1787068941-2ed4288876f04b8c9af382ee858fc35b` | RUNNING | 结束后跑 100k paste、I-011/I-012 导航 |
| `<历史会话:render>` | 控制字符、Bidi、零宽与复杂 diff 安全显示 | `gwreq-1787069068-efbb6908954e4eb2a5673e03883b7bd6` | RUNNING | R-007…R-010 分块与乱序 fixture |
| `<历史会话:lifecycle>` | 数千任务、十次随机中断、损坏检查点与快速重启 | `gwreq-1787069069-36eaa996ddae4b4a93dfdb7a7590b9fd` | RUNNING | 观察 90% 自动 compact；复验明确 rejected steer |
| `<历史会话:isolation>` | 8 人保险箱、并发、只读、链接、特殊名称、备份恢复 | `gwreq-1787069069-10eaac87cdaf486186a8de2a76ca8ebe` | RUNNING | S-007…S-010 终端控制序列夹具 |

## 关闭条件

只有同时满足以下条件才允许把总 Goal 标记完成：全部已知单因素有结果；两两组合清单执行完；高风险多因素
组合和状态机 fuzz 有可重放证据；fixture 与真实后端边界分清；可用终端客户端实测、不可用平台明确保留；
长时间 soak 有资源曲线；所有失败都有最短复现、所属层、修复/不修决定和复验结果；Fiber 产物由测试者
独立审计且不能靠模型自述关闭。
