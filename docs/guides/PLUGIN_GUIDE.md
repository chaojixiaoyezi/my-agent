# 插件与安全开关使用说明

这份说明讲 my-agent 的**插件**怎么用，以及新出现的几个安全开关（新版插件总开关与隔离底座、工具把关、事件观察、屏幕观察、G2b 本机凭据强制）。界面细节以你实际看到的为准；命令和字段名照原样写，方便你直接抄。

**代码现状**：本说明里的功能都已经在集成分支 17j 上——屏幕观察、G2b、锁与私有文件、`/plugins info` 的查询与展示，以及工具把关（B5，`717fdb350`）、新版插件总开关与隔离底座（B7，`c5ee4884e`）、只看档开关进管理员白名单（obsset，`1e1aadb80`）。**注意**：17j 还没部署，**生产跑的仍是 `d05a0d075`**，所以生产上这些新能力还看不到；下面按代码事实描述，实际能不能用请以你所在版本的 `/plugins info` 和 `/settings` 为准。

## 一、插件是什么

插件是 my-agent 的"外部装备"：一个独立进程（Python、Go、Node 或任意可执行文件都行），装好启用后，它能给 my-agent 加上新工具、新面板，或者在关键动作前插一句话。

装、配、启、停、卸、更新都在 `/plugins` 里完成，TUI 和飞书走**同一个入口、同一份文字**——你在飞书里发的 `/plugins info` 和 TUI 里看到的内容逐字相同。

和相近概念的区别：

| 概念 | 是什么 | 和我的关系 |
| --- | --- | --- |
| 能力包 | 只含内容的资料包，不启动进程 | 能力包不跑代码；插件会跑进程，见[能力包使用说明](CAPABILITY_PACK_GUIDE.md) |
| Skill | 可独立复用的单项方法 | Skill 是方法；插件是可装卸的装备 |
| 插件 | 可装卸的工具、面板、事件观察或工具收紧 | 本说明的主角 |

## 二、常用命令（TUI 和飞书都一样）

| 想干什么 | 命令 |
| --- | --- |
| 看装了哪些插件 | `/plugins list` |
| 看某个插件的详情 | `/plugins info <插件名>` |
| 安装插件 | `/plugins install "<包路径>"` |
| 启用 / 停用 | `/plugins enable <插件名>` / `/plugins disable <插件名>` |
| 改插件设置 | `/plugins configure <插件名> --file ./settings.json` |
| 查某次请求的结果 | `/plugins status <原请求编号>` |
| 更新 / 卸载 | `/plugins update <插件名> "<新包路径>"` / `/plugins remove <插件名>` |
| 调用插件自己的工具 | `/plugins@<插件ID> <动作> [参数]` |

几条要注意的：

- **管理动作只有当前空间的管理员能做**。普通身份发管理命令会被直接拒绝，不会"半执行"。
- **安装路径**：`/plugins install` 和 `/plugins update` 给相对路径时，按**当前会话的工作区根**解析（TUI 标题栏显示的目录），不是终端所在目录。找不到时回执会写明按哪个目录解析，可以改用绝对路径。
- **更新不能跨版本回退**：`/plugins update` 用同一插件 ID 的新版本替换已停用插件；已启用的要先 `disable`。它不做双版本切换，也不做 rollback。
- **每次安装/启用含可执行程序的包，都会先给你一份确认码**，你按确认码原样提交才会真正执行。

## 三、`/plugins info` 现在能看什么（四段）

对**新版（v8）插件**执行 `/plugins info <插件名>`，会在使用卡后面追加四段：

1. **事件订阅**：这个插件订阅了哪几类宿主事件，以及每类要不要正文（`不含正文` 就是不拿内容，只拿结构化事实）。
2. **收紧工具**：它要在哪些工具上加把关，以及能看到多少参数（完整参数、还是只有摘要哈希）。
3. **网络与沙箱**：它要不要联网，以及沙箱要求（新版插件强制要求进程沙箱，沙箱不可用时不允许启用）。
4. **最近收紧决定**：最多列最近 10 条（新到旧），每条给时间、工具、结果、原因码；下面跟一行"无法审批：N 次"的累计计数，以及一行**观察计数**（这个插件收到/合并丢弃了多少事件）。

老版本插件（v1–v7）没有事件与收紧声明，第 3 段会显示"不适用"，第 4 段显示"暂无记录"。

**第 4 段的记录从哪来**：B5（工具收紧钩子）合入后，每次有工具真的去问插件，宿主都会写一条结构化决定（`plugin_gate.decided`），按设计第 9 节的字段白名单存进当前 owner 的权威库。`/plugins info` 第 4 段读的就是这些行，所以你**真拦过之后就会看到真实记录**（谁、什么工具、什么结果、什么原因码、耗时）。

**没有记录什么时候是正常的**：

- 这个插件**还没拦过任何东西**（没触发过它的收紧门）；
- 事件中心还没建立（比如你只发过飞书命令、没打开过 TUI 目录）——**观察计数会显示"暂无记录"**，这是已知取舍（宿主不为展示去新建事件中心）；
- B5 还没合入时第 4 段恒为"暂无记录"（见第十一节）。

## 四、插件能在关键动作前把关（B5 合入后生效）

装了带"工具收紧"的插件后，它在工具**真正执行之前**有机会说一句：照原样放行、要求确认一次、或者直接拒绝。这件事在 B5（工具收紧钩子）合入后才真正执行；B5 合入前，`/plugins info` 能看到声明，但**没有真正的把关**（见第十一节）。

### 它能做什么、不能做什么

- **只能更严，不能放宽**：插件结果和宿主原裁决**合并取更严**，插件给"放行"不会把宿主本来就拒绝的调用变成允许；它也不能改你的参数、不能替你把工具跑起来。宿主已经在工具执行前就 deny 的、以及宿主自己发起的命令，**根本不会去问插件**。
- **三种结果**：照原样放行（`allow_as_is`）、要求确认一次（`ask`）、直接拒绝（`deny`）。

### 要求确认时怎么回复

- **选项只有两个**：**"仅本次"** 和 **"拒绝"**——不会出现"一直允许"或长期授权。
- **TUI**：弹出审批框，点"仅本次"允许这一次。
- **飞书**：**没有审批卡片，只有一行文字提示**。回复 `/approve <管理员密码>` 允许本次，或 `/deny` 拒绝（提示里带插件编号和原因，放在最前面保证飞书前 200 字能看到）。

### 被插件拒绝时会怎样

工具**不会执行**，也不会产生工具事件；回执是结构化的 `PLUGIN_GATE_DENIED`（分类"权限"、不可重试）。这就是"插件说不，这条命令就不跑"。

### 没法审批、或插件超时

- 插件征询失败（拿不到插件、共用池出错等）：按**更严**处理，走确认，不静默放行。
- 真正没法收集到审批意见时，回执是 `PLUGIN_GATE_APPROVAL_UNAVAILABLE`，并计入 `/plugins info` 的"无法审批：N 次"——这个计数**只增不减、不设时间窗**，是设计上就这么定的。
- **超时**：等插件回答的总预算由 `plugin_tool_gate_timeout_ms` 控制（默认 `2000` 毫秒，可设 200–10000）。超了按上面的"更严"路径走。这个值**管理员可以用 /settings 调**（调大只会多等，超时仍然收紧成确认，不会变松）；模型用设置工具改它一律被拒。
- **总开关关掉时**：`plugin_events_enabled` 关着就不征询插件了（等于暂时停用"收紧"这项能力），执行结果完全按宿主原来的裁决走；`/plugins info` 会在"事件订阅"和"收紧工具"两段写明原因码 `plugin_events_disabled` 和怎么打开。这是**少一层收紧**，不是放宽：收紧层只会把 allow 变严，从来不会放行。

小例子：随包自带的 `rm-guard` 样例插件，看到 `run_command` 里出现 `rm` 加 `-r` 和 `-f` 的组合（`-rf`、`-fr`、`-r -f`、`--recursive --force` 等写法）就先要求确认一次；看到补丁里有删除段就直接拒绝。

## 五、随包样例与写插件的技能

**三个样例插件**（源码在仓库 `plugins/` 下，装了才生效，装/启需要管理员）：

| 样例 | 是什么 | 演示什么 |
| --- | --- | --- |
| `event-watch`（Python） | 订阅全部 6 类宿主事件，在只读表格面板里显示每类"收到"与"合并丢弃"条数 | 事件订阅与只读面板 |
| `rm-guard`（Python） | 删目录的命令先要求确认；删除文件的补丁直接拒绝 | 工具收紧（Python 写法） |
| `rm-guard-node`（Node） | 同上，用 Node 写 | 工具收紧（Node 写法） |

事件观察默认**不要正文**，只拿结构化事实（谁、什么类型、什么时候），所以插件看不到你的消息原文。

**写插件的技能**：内置技能 `write-my-agent-plugin` 会把事件字段名逐条列出来（有一节就叫"观察事件字段表，别猜字段名"），并给出 Python / Node 两个模板。想自己写插件时让它带你做：直接问"帮我写一个 my-agent 插件"，或让它讲讲怎么写。

## 六、新版插件总开关与隔离底座（B7 合入后生效）

### 6.1 总开关 `plugin_events_enabled`

新版（v8）插件的总开关，也是事件观察和工具收紧的总闸。

| 项目 | 说明 |
| --- | --- |
| 开关名 | `plugin_events_enabled` |
| 默认 | **关** |
| 谁能改 | **只有你本人**。它在两处登记为管理员边界项：参数中心（`settings/parameter_registry.py` 的 `_BOUNDARY_NAMES`）和用户设置白名单（`settings/user_config_capability.py` 的 `USER_SETTINGS_BOUNDARY_KEYS`）。所以你在 `/settings` 里能改，模型通过设置工具去改会被拒（`PARAMETER_BOUNDARY`）。改完**重启 Gateway** 生效。 |

**B7 合入前这个参数根本不存在**：`/settings` 会回 `PARAMETER_UNKNOWN`（17j 里就是这样）。所以这一节讲的是 B7 合入后的样子。

**关着的时候会怎样**：

- v8 插件**能装、不能启用**。启用会被拒，原因是 `plugin_events_disabled`，回执提示你**去 `/settings` 打开总开关**。
- **已经启用的** v8 插件，在开关关掉后**事件不再投递、收紧不再征询**（相当于停用了这两项能力），`/plugins info` 会显示原因。开关本身不卸载、不停用插件，只是让这两项能力停下来。

还有一个相关的全局开关 `enable_plugins`（默认**开**，管理员可改）：它是插件的**总开关**，关掉后插件仍可安装、但不能启用。这是更外层的一道，和 `plugin_events_enabled` 不是一回事——前者管"插件能不能启用"，后者管"v8 的事件/收紧这两项能力开不开"。

### 6.2 v8 插件必须在沙箱里跑

新版插件（清单里带 `permissions` 的）**强制**走进程沙箱，**不管全局的 `plugin_process_sandbox` 开没开**。沙箱不可用时**不允许启用**（回执是 `sandbox_unavailable`）。

沙箱具体收紧了什么：

- **整个 my-agent 家目录被隐藏**：插件**读不到**你的会话库、记忆、运行数据。
- **只放行插件自己的目录和解释器前缀**：能读的只有插件包目录、插件数据目录，以及解释器能跑起来必需的那些路径。**解释器本身如果落在 my-agent 数据根里，直接拒绝启用**（原因码 `interpreter_inside_hidden_root`），防止借解释器把隐藏根重新露出来。
- **默认断网**（清单里 `network:false`）：沙箱内既连不了外网，也连不了本机回环。

**想联网（`network:true`）的话**：

- **Linux 上直接拒绝启用**，原因码 `gateway_port_isolation_unavailable`——因为在插件端口隔离落地前，Linux 上没法把插件进程和本机服务隔开，这是**已知边界，不是 bug**。
- **macOS 上**：沙箱会**拒绝登记在案的 Gateway 端口**，也就是说插件即使能联网，也**碰不到本机 Gateway 的端口**。

### 6.3 只有本机管理员能启用 v8 插件

v8 插件的启用还多一道身份门：**只有本机管理员（身份是 `local/main`）能启用**，别的人（远程用户、其它 owner、身份不完整的会话）去启用会被拒，原因是 `plugin_events_owner_not_allowed`。

这道门的目的：v8 插件能跑进程、能收紧工具，**等价于本机代码执行**，所以只让本机管理员装/启。普通身份即使拿到了包、即使总开关开着，也启不了。

### 关掉总开关会发生什么（b5b7wire，2026-10-04）

`plugin_events_enabled` 关着时，v8 插件的事件订阅和"动作前把关"两项能力都不生效：事件不投、**收紧也不问插件**。
已经启用的插件不会被自动停用（它还装着），只是这两项能力暂停；`/plugins info` 会在这两段写明原因和怎么打开。
改这个开关要**重启 Gateway** 才生效。

## 七、屏幕观察：只看档怎么开

屏幕观察让 my-agent **看到屏幕内容**（给出窗口身份、截图代次、几何和 OCR 文字候选），并且**只看、不点**。

### 两个开关的四种组合

`computer_use_enabled`（总开关）和 `computer_use_observation_enabled`（观察开关）一起决定档位，这是显式的四种语义，不是"缺依赖时的兜底"：

| 组合 | 你会得到 |
| --- | --- |
| **都关（默认）** | 没有 Computer Use 适配器，工具目录不变 |
| **只看档**（总开关**关** + 观察**开**） | 只交出 `observe_window`：能看，不能点，也不交出任何上游桌面工具 |
| **完整档**（两个都开） | 上游桌面工具全交出，另加 `observe_window` 和点击/输入候选工具 |
| **只开总开关** | 上游桌面工具全交出，没有观察工具 |

### 怎么打开只看档

1. 保持 `computer_use_enabled: false`（默认就是关）。
2. 把 `computer_use_observation_enabled` 改成 `true`——**在 TUI 或飞书里发** `/settings set computer_use_observation_enabled true`（两边同一句）。**obsset 合入后生效**：在这之前，这个参数不在用户可写白名单里，连管理员发这条命令也会被拒（`PARAMETER_BOUNDARY`）；obsset 合入后，管理员可以改、模型不能改。`computer_use_enabled`（总开关）**仍然不能用聊天命令改**，要在配置文件里改。**改完要发 `/restart` 才生效**：配置在 Gateway 启动时读取，回执里会写明“重启后生效”（effect_when=restart_gateway）。见[给维护者的核对位置](#给维护者的核对位置)。
3. 装观察需要的依赖：`pip install 'my-agent[computer-use-observe]'`（只看档专用的依赖清单，含 MCP 协议包 mcp，不含点击用的 pyautogui 和上游执行器；不要装 `computer-use`，那是完整档，会带进点击和键盘能力）。

**只看档不需要装上游执行器**（不装 `pyautogui` 也能跑），而且那个适配器进程**根本不会加载**上游的鼠标键盘依赖——所以它没有能力去点你的屏幕。

### 谁能用、怎么审批

- **只有结构化的 local/main 管理员 + `full-access` 权限模式**才会看到这些工具；远程用户和普通 owner 即使改了配置也看不到，按名字调也会被拒。
- 每次观察都走 **always 审批，每次都问本人**——不会"批准一次就一直看"。

### 锁屏时会怎样

在 macOS 上锁屏时调用观察，会返回结构化结果 `screen_locked`，消息是"屏幕已锁定，解锁后再观察"。这个检查是只读的、不弹窗；如果查不到锁屏状态，会按"无法确认"继续原来的观察流程，**不会**因为查不到就拒绝你。Linux（X11）没有锁屏这个概念，行为不变。

## 八、本机凭据强制开关（G2b）

my-agent 的 Gateway 默认把**本机回环请求**当作可信。G2b 是上面再加一道：**本机客户端也要带凭据**才算管理员。

| 项目 | 说明 |
| --- | --- |
| 开关名 | `gateway_require_local_credential` |
| 默认 | **关** |
| 谁能改 | 管理员 |
| 打开后 | 不带凭据的本机请求降为**匿名普通用户**：不认身份头、绝不给管理员 |
| 打开前 | 行为不变，只**计数不拦** |

**打开前请先观察几天**：先看 `/status` 里这两个字段——

- `uncredentialed_loopback_by_endpoint`：按端点统计的"没带凭据的回环请求"次数；
- `local_credential`：本机凭据当前状态（正常 / 不可用及原因码）。

等"没带凭据"的计数归零（说明没有漏网的旧客户端），再打开开关。打开后，**不带凭据的本机请求会被降为匿名**（不认身份头、绝不给管理员）；如果你的客户端连本机凭据自己都有问题（文件丢了、格式坏了、权限不对），会拿到结构化的本机凭据错误码，据此提示"请重启客户端"，而不是对着 403 或匿名行为摸不着头脑。这类码是 `LOCAL_CLIENT_CREDENTIAL_*` 系列，例如 `LOCAL_CLIENT_CREDENTIAL_MISSING`（凭据文件不存在）、`LOCAL_CLIENT_CREDENTIAL_PERMISSIONS`（文件权限不对）、`LOCAL_CLIENT_CREDENTIAL_INVALID`（内容无效）。

**一条例外**：插件进程只拿得到 host-API 令牌、拿不到本机客户端凭据，所以 `/plugin-host/query` 这一条路由接受有效的 host-API 令牌，不要求客户端凭据——G2b 打开后插件宿主 API 照常可用。

## 九、锁和私有文件：只动自己建的东西

宿主在写自己的数据（锁文件、运行时记录、私有目录）时，**只动它自己新建的那些**：

- 新建的目录是 `0700`（只有你能进），新建的文件是 `0600`（只有你能读写）。
- **不会**顺手去改你已有目录的权限。
- 锁文件同样这个口径：新建私有，已存在的也不会被它改权限。

**符号链接的情况**：如果你的项目目录本身就是个符号链接（比如指到外置盘），宿主允许**跟随一次**这个已存在的链接锚点，在它指向的真实目录下新建需要的子目录——所以后台任务能正常建工作目录了。注意：

- 只有"最近的已存在祖先"能跟随一次；
- 从它往下新建的每一段仍然不跟随符号链接，段内遇到链接一律拒绝；
- 链接本身和目标目录的权限，一位都不动。

## 十、你能做什么、怎么排查（配小例子）

下面每条都写清 TUI 和飞书两个入口怎么做。命令两边**完全一样**（同一个入口），差别只在**审批的呈现方式**。

| 你想做的事 | TUI 怎么做 | 飞书怎么做 |
| --- | --- | --- |
| 打开 v8 的事件/收紧能力 | 发 `/settings set plugin_events_enabled true`，重启 Gateway | 私聊里发**同一句**，重启 Gateway |
| 看某个插件拦过什么 | `/plugins info rm-guard`，看第 4 段 | 发**同一句**，看到的是同一份文字 |
| 放行一次被要求确认的命令 | 审批框点"仅本次" | 回 `/approve <管理员密码>` |
| 拒绝一次被要求确认的命令 | 审批框点"拒绝" | 回 `/deny` |
| 查一次启用/命令请求的结果 | `/plugins status <原请求编号>` | 发**同一句** |

**例 1（确认被触发）**：你想让 my-agent 删一个临时目录，让它跑 `run_command rm -rf /tmp/x`。`rm-guard` 已启用、总开关已开，于是工具执行前先问你：

- **TUI**：屏幕弹出审批框，只有"仅本次 / 拒绝"两个按钮。
- **飞书**：来一行文字（**没有卡片**），写清是 rm-guard 要求确认、原因是 `RM_RF`；你回 `/approve <管理员密码>` 放行本次，或 `/deny` 拒绝。
- 你点了"仅本次"之后，这条命令才真的执行。

**例 2（被直接拒绝）**：你要让 my-agent 用 `apply_patch` 删一个文件，`rm-guard` 的删除门直接判定 deny：

- 工具**不会执行**，文件还在；回执是 `PLUGIN_GATE_DENIED`。
- **TUI**：活动行显示"插件 rm-guard 拒绝"。
- **飞书**：同一行文字提示进来，不用你回复，因为已经拒了。
- 之后 `/plugins info rm-guard` 第 4 段会多一条 deny 决定（`reason_code=DELETE_FILE_BLOCKED`）。

**例 3（排查"为什么启不了"）**：你装了个 v8 插件，`/plugins enable xxx` 失败。看回执的**结构化原因**：

- `plugin_events_disabled` → 总开关关着，去 `/settings` 打开 `plugin_events_enabled`，重启 Gateway。
- `plugin_events_owner_not_allowed` → 当前身份不是本机管理员，换本机管理员来做。
- `PLUGIN_GATEWAY_PORT_ISOLATION_UNAVAILABLE` → 这插件声明了 `network:true` 而你在 Linux 上，端口隔离还没做，属已知边界。
- `sandbox_unavailable` → 沙箱起不来，不要关沙箱硬跑。

**例 4（排查"命令怎么没跑"）**：你发的命令没反应。先看是不是插件把关了：`/plugins info <插件名>` 第 4 段有没有一条对应时间的决定；有的话按 `reason_code` 对照上面例 1、例 2 的说明。**"无法审批：N 次"在涨**说明有征询没人能审批，检查是不是管理员不在线或审批入口没接上。

## 十一、常见疑问

**Q：怎么打开 v8 插件的事件观察和工具收紧？**
TUI 或飞书里发 `/settings set plugin_events_enabled true`（只有你能改，模型改不了），然后**重启 Gateway**。打开后 `/plugins info` 里第 1、2 段的事件订阅和收紧工具才会真正生效。

**Q：总开关关着时，我装了个 v8 插件，为什么启不了？**
启用会被拒，原因是 `plugin_events_disabled`。这是故意的：关着总开关时 v8 只能装、不能启。按提示去 `/settings` 打开 `plugin_events_enabled` 再启用。

**Q：`rm-guard` 拦了我的删目录命令，让我确认，我该怎么办？**
**TUI**：会弹出审批框，选项只有"仅本次"和"拒绝"——点"仅本次"这次才放行。**飞书**：只会来一行文字（没有卡片），回复 `/approve <管理员密码>` 放行本次，或 `/deny` 拒绝。**没有"一直允许"**。

**Q：插件拒绝了我的工具调用，怎么确认它真的没执行？**
被拒绝的调用，回执是 `PLUGIN_GATE_DENIED`，工具**不会执行**，也不会产生工具事件；活动行和工具结果里会显示"插件 X 拒绝"。想复核，用 `/plugins info <插件名>` 看第 4 段有没有对应那条决定行。

**Q：插件说它想联网，行不行？**
**Linux 上直接不行**——`network:true` 的 v8 插件会被拒启用，原因是 `gateway_port_isolation_unavailable`（端口隔离还没做，这是已知边界）。**macOS 上可以，但连不到本机 Gateway 端口**（沙箱拒绝了登记在案的 Gateway 端口）。默认（`network:false`）是**全断网**，外网和本机回环都连不了。

**Q：插件会不会读到我的会话和记忆？**
不会。v8 插件强制进沙箱，**整个 my-agent 家目录都隐藏**，只放行插件自己的目录和解释器前缀；想把解释器藏在 my-agent 数据根里借道读，会在启用时就被拒（`interpreter_inside_hidden_root`）。

**Q：`/plugins info` 里"无法审批：N 次"为什么只涨不跌？**
那是设计如此——这个计数**不设时间窗、只增不减**，用来提示"有多少次插件想说但没人能审批"。以后再考虑要不要改成"最近 N 天"。

**Q：别人（远程用户 / 别的 owner）能启用我的 v8 插件吗？**
不能。只有本机管理员（`local/main`）能启用 v8 插件，别的人会被拒，原因是 `plugin_events_owner_not_allowed`。

**Q：`/plugins info` 显示"暂无记录"，是不是坏了？**
不是。三种正常情况：① 这个插件还没拦过东西；② 事件中心还没建立（只发过飞书、没打开过 TUI 目录）；③ B5 还没合入时第 4 段恒为"暂无记录"。真拦过一次之后就会看到记录（见第三节和第十一节）。

**Q：飞书和 TUI 看到的插件信息一样吗？**
一样。两边是同一个入口、同一份文字。唯一的区别是**审批呈现方式**：TUI 有审批框，飞书只有一行文字提示，需要你回复 `/approve` 或 `/deny`。

**Q：插件能读我的消息原文吗？**
取决于它的订阅声明。事件观察默认只拿结构化事实（类型、时间、身份），**不要正文**；`/plugins info` 的"事件订阅"那段会写明每类事件是不是"不含正文"。

**Q：打开屏幕观察后，它会自己点我的屏幕吗？**
不会。只看档只交出 `observe_window`，那个适配器进程连鼠标键盘依赖都没加载，点击/输入工具也不会出现在工具目录里。

**Q：G2b 打开后我的脚本都连不上了怎么办？**
先用 `/status` 的 `uncredentialed_loopback_by_endpoint` 找出是哪些端点在被拒；仓库内的 TUI/CLI、IM、开发脚本都已经会带凭据。仓库外的自建脚本需要照 `X-Gateway-Token: $GW_TOKEN` 的写法带上本机凭据。凭据只进环境变量，不回显、不落盘。

## 十二、还没到的部分

第四、六节描述的工具把关（**B5**，`717fdb350`）、总开关与 v8 隔离底座（**B7**，`c5ee4884e`），第七节"怎么打开只看档"第 2 步描述的 **obsset**（`1e1aadb80`，把观察开关加进管理员白名单）——**这三项都已经在集成分支 17j 上**，`/plugins info` 第 4 段会显示真实记录、v8 插件能启用、管理员能用 `/settings` 打开只看档。

**但 17j 还没部署**：生产运行的是 `d05a0d075`，那里还没有这些能力。要确认你手上这个版本有没有，看 `/plugins info` 有没有第 4 段记录、`/settings` 里 `computer_use_observation_enabled` 能不能改。

下面这些是**还没进 17j** 的：

- **老格式插件（v1–v7）的权限收紧**：改成"要用什么就明确授权什么"，还没合入。

## 给维护者的核对位置

下面这些是给改代码的人核对用的，用户不用看。每条对应正文里的一句话。

| 正文说的 | 在代码里核 |
| --- | --- |
| `/plugins info` 第 4 段的记录从哪来 | `agent_py_agent/agent/plugin_events/decision_ledger.py`、`agent_py_agent/agent/agent_core/tool_runtime_ledger.py`（写 `plugin_gate.decided`）；查询在 `agent_py_agent/agent/runtime_db/repository.py` 的 `plugin_gate_decisions` / `_PLUGIN_GATE_DECISION_FIELDS`；渲染在 `agent_py_agent/agent/plugin_commands.py` 的 `render_plugin_event_details` |
| 把关"只能更严"、不问宿主已拒的调用 | `agent_py_agent/agent/tooling/plugin_gate_policy.py` 的 `tighten_plugin_decision`（开头两条早返回：宿主 `deny`、`host_command`）；征询失败走 `except` 分支 |
| 审批选项只有"仅本次/拒绝" | `agent_py_agent/agent/tooling/plugin_gate_policy.py` 的 `plugin_gate_approval_request`（把批准项改标签为"仅本次"） |
| `PLUGIN_GATE_DENIED` / `PLUGIN_GATE_APPROVAL_UNAVAILABLE` | `agent_py_agent/agent/contracts/error_taxonomy.py`（两个码都在这）；"无法审批"计数在 `runtime_db/repository.py` 的 `plugin_gate_unavailable_count` |
| `plugin_tool_gate_timeout_ms`（默认 2000、范围 200–10000） | `agent_py_agent/agent/settings/config.py`、`agent_py_agent/agent/settings/services/runtime_tool_field_specs.py`、`agent_py_agent/config/agent_config.yaml` |
| `plugin_events_enabled` 默认关、管理员边界 | `agent_py_agent/agent/settings/config.py`（`bool = False`）、`config/agent_config.yaml`（`false`）、参数中心 `settings/parameter_registry.py` 的 `_BOUNDARY_NAMES`、用户设置白名单 `settings/user_config_capability.py` 的 `USER_SETTINGS_BOUNDARY_KEYS` |
| 关着时 v8 能装不能启用，原因 `plugin_events_disabled` | `agent_py_agent/agent/plugin_enable_tool.py` 的 `_preflight_failure` |
| v8 强制沙箱（不看全局开关） | `agent_py_agent/agent/plugin_enable_tool.py`（`process_sandbox = policy.process_sandbox or self._is_v8`） |
| 隐藏家目录 / 只放行插件目录与解释器前缀 / 默认断网 | `agent_py_agent/agent/plugin_sandbox.py` 的 `plugin_sandbox_spec`（`private_read_roots` / `public_read_roots` / `_interpreter_prefixes`）；解释器落在数据根内拒绝的码 `interpreter_inside_hidden_root` |
| Linux `network:true` 拒 / macOS 拒 Gateway 端口 | `agent_py_agent/agent/plugin_sandbox.py` 的 `plugin_sandbox_problem`（Linux 直接返回 `gateway_port_isolation_unavailable`）；`deny_gateway_ports=gateway_bound_ports()` 在 `plugin_sandbox_spec` 里、端口表在 `agent_py_agent/agent/attempt/sandbox.py` 的 `gateway_bound_ports` |
| 只有本机管理员能启用 v8，原因 `plugin_events_owner_not_allowed` | `agent_py_agent/agent/plugin_enable_tool.py` 的 `_preflight_failure`；判据 `agent_py_agent/agent/user_space/owner_access.py` 的 `is_complete_local_admin_owner` |
| G2b 打开后不带凭据降匿名 | `agent_py_agent/agent/auth/middleware.py` 的 `extract_identity`（不可信来源不认 header，固定匿名） |
| 本机凭据自身的问题码是 `LOCAL_CLIENT_CREDENTIAL_*` | `agent_py_agent/agent/gateway_parts/local_client_token.py`（`_MISSING` / `_PERMISSIONS` / `_INVALID`）；用例断言见 `agent_py_agent/tests/test_gateway_local_trust_enforcement.py`（`LOCAL_CLIENT_CREDENTIAL_MISSING` / `_INVALID` / `_PERMISSIONS`） |
| 只看档要用 `/settings set computer_use_observation_enabled true`（obsset 后） | 现在不在用户可写白名单，被拒 `PARAMETER_BOUNDARY`；obsset 把它加进白名单后管理员可改、模型不能改。`computer_use_enabled` 仍不进白名单。 |
