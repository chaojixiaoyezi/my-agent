# 老格式插件权限盘点与收严方案（opp）

状态：**方案待 3a 定**。日期：2026-10-03。盘点基点：`worker/plugin-legacy-permissions`，step17i `c47d023b61bc1acb211326d731d0f98bb2e00383`。

本轮只读仓库代码、提出方案；没有修改产品、配置或真实安装记录，没有运行插件、连接 Gateway、测试真实 TUI/飞书或验证新沙箱。下文“需要授权”“会受影响”是源码推导，不是新规则已实现或运行验收通过。M 线 B7 的收严目标作为依赖，不能把它当成本基点的现状。

## 1. 逐项权限盘点（源码事实）

### 1.1 口径与完整范围

- `plugins/` 有 **17 个样例插件 + 1 个 SDK 目录**。源 `declaration.json` 多数没有 `schema_version`；下表版本是按现有构建器选版规则得到的包版本，不是把源声明当安装包。统计为 v1=6、v2=4、v3=2、v4=1、v5=1、v6=3。Python 构建器规则见 `scripts/build_plugin_package.py:58-82`，文件入口规则见 `scripts/build_plugin_files_package.py:31-66`。
- “基础”=读自己的包/激活环境、固定解释器依赖和系统运行资源，读写自己的数据目录（含私有临时目录），**不含读工作区、其它插件、宿主数据根或家目录**。面板经 stdio 收到的公开投影不是读磁盘、不是 `host_api`。
- `R`=管理员选定的工作区/项目读根；`W`=选定的输出或恢复目标写根（含原子替换所需父目录，通常同时需要读）；`E`=明确列出的额外只读运行依赖根；`N`=网络授权；`D`=桌面/IPC 兼容能力。这些是方案用语，**不是本版本已有的清单字段或命令参数**。
- “无需联网”只指所审阅业务没有网络需求，不能证明旧插件在 OS 层被断网。`requested_effect=read_only` 是宿主审批分类，不代表进程不能写私有数据或绕过 SDK。
- 文件:行是 `source_ref`，均相对上述仓库基点；同一单元格的 `:行` 续用前一个文件，短文件名相对该插件源码目录，宿主短路径相对 `agent_py_agent/agent/`。同时核对各插件同包 `declaration.json` 的 `tools[].requested_effect`、`host_api`、`panels`、`entry`。本轮不读取用户文件或凭据来验证推断。

### 1.2 样例、SDK/模板逐个表

| 对象 / 包版本 | 实际要读什么 | 实际要写什么 | 网络 / host_api | 新规则下恢复现有功能的最小授权；基础模式影响 | 依据（source_ref） |
| --- | --- | --- | --- | --- | --- |
| activity-line / v2 | 包元数据；当次 `activity/run_state` 公开投影；不读工作区/会话正文 | 无业务落盘 | 无网络；无 host_api，stdio 面板 | 基础即可；无需工作区授权 | `plugins/activity-line/src/activity_line/server.py:19-31`、`:52-66`；同目录 `declaration.json` 的 `panels`、空 `tools` |
| browser-lite / v5 | 包声明；浏览器程序和依赖；`file:` 页面按逐次读取上下文访问；自动发现还看家目录 ms-playwright 缓存；Linux 回收读取 `/proc/*/cmdline/stat` | 自有数据目录的浏览器 profile、DevToolsActivePort、缓存，并在停止时清理；不写工作区 | 浏览器 CDP 的回环 HTTP/WebSocket；网页可访问 HTTP(S) 及页面子资源；无 host_api | 完整网页功能需 N；本地页另需 R；系统外浏览器/缓存需 E 或迁到明确允许的位置。断网连 CDP 也会坏；藏家会使缓存发现失效，桌面/浏览器依赖仍须真机复核 | `plugins/browser-lite/src/browser_lite/access.py:27-48`；`launcher.py:27-38`、`:83-104`、`:122-140`、`:187-242`；`websocket.py:144-146`；`session.py:149-160`；声明 `observation`/`observation_ref` 不授网络权 |
| context-inspector / v2 | 包元数据；当次 `context` 数字白名单，不读真实上下文正文 | 无业务落盘 | 无网络；无 host_api，stdio 面板 | 基础即可；不应为了显示 token 数授予宿主数据根 | `plugins/context-inspector/src/context_inspector/server.py:35-49`、`:70-84`；声明 `panels[].topics` |
| design-lite / v3 | 包声明/模板；`edit` 读工作区原 HTML；`create` 内容来自参数与包内模板 | 工作区创建/改写 HTML，经写上下文及 no-follow 原子替换 | 无网络、无 host_api；随包 Skill 只提供方法 | `create` 需 W（及操作所需父目录可见）；`edit` 需 R+W。基础模式不能创建/修改工作区文件 | `plugins/design-lite/src/design_lite/operations.py:35-60`、`:66-99`、`:107-132`；`server.py` 读取/写入扩展；声明 `skills`、两个工具 `mutating` |
| desktop-lite / v1 | 包声明；系统命令或设置指定的程序；`open` 先按读取上下文安全打开一个工作区普通文档 | 不直接落盘；通知、打开默认程序、写系统剪贴板是系统副作用，可能触发应用自身写入 | 插件无 HTTP 客户端、无 host_api；需桌面 IPC，打开的外部应用行为不能算“断网后无外部副作用” | `open` 需 R；通知/剪贴板/打开需 D，用户目录里的命令另需 E。**仅 R/W/N 三项不足以保证全部功能**，不可为兼容放开整个 HOME；桌面 broker 或逐平台 IPC 授权另定 | `plugins/desktop-lite/src/desktop_lite/opening.py:31-50`；`commands.py:61-95`、`:102-122`；声明三工具均 `mutating` |
| drama-media-shell / v1 | 工作区 JSON/JSONL、作业输入和来源；包内离线检查器；自有作业/确认/运行记录 | 私有作业账；`run/collect` 写工作区夹具媒体并读回；`prepare/confirm` 虽标 read_only 仍写私有账 | 无网络、无 host_api、无子进程；非 fixture 供应商明确拒绝 | 检查/准备/状态需 R；完整 fixture 流程需 R+W，私有账属基础。基础模式读不到输入；仅 R 时 run/collect 写出失败。授 N 也不会变成真实供应商适配器 | `plugins/drama-media-shell/src/drama_media_shell/workspace_io.py:21-45`；`private_store.py:26-35`、`:52-102`；`production.py:45-73`、`:78-100`、`:125-146`、`:190-242` |
| genui-lite / v3 | 包声明；工作区 JSON 表格 | `export` 原子写工作区独立 HTML；`table` 不写文件 | 无网络、无 host_api；随包 Skill 不授执行权 | table 需 R；export 需 R+W；基础模式无法读输入，只有 R 时不能导出 | `plugins/genui-lite/src/genui_lite/operations.py:33-47`、`:53-74`；同目录 `declaration.json` 的 table=`read_only` / export=`mutating` 与 `skills` |
| harness-console / v4 | 包内网页；宿主 API 的 threads/activity/plugins/gateway 公开投影；浏览器程序与可能的家目录 ms-playwright 缓存；不直接读工作区或会话库 | 自有 `last-link.txt`（0600）；desktop 子进程的自有 app-profile；默认浏览器可能在插件之外写自己的状态 | 监听 `127.0.0.1`；向宿主 `/plugin-host/query` POST（只读查询）；**唯一声明 `host_api:["read"]` 的样例** | 需允许回环监听及宿主 API 通路；若初版只有 N 布尔，必须授 N 并说明其实放开所有网络，不得称“仅宿主 API”。desktop/open_browser 另需 D、浏览器 E；不开浏览器可手动取链接。基础断网使工作台无法监听/查宿主 | `plugins/harness-console/src/harness_console/host.py:33-81`、`:86-120`；`console.py:21-39`；`server.py:31-39`、`:104-137`；`window.py:20-29`、`:52-62`、`:82-91`；声明 `host_api` |
| hello-go / v6 | 随包可执行文件（声明编译时嵌入）；Go/OS 运行资源；不读工作区 | 无业务落盘 | 无网络、无 host_api | 基础即可；保留既有 executable/平台/文件摘要确认 | `plugins/hello-go/main.go:1-18`、`:55-67`、`:119-148`；`plugins/hello-go/declaration.json` 的 `entry.kind=executable`、`files/platforms` |
| hello-node / v6 | 包声明、固定 Node 解释器；hello 只算问候；read_text 读当次授权工作区 UTF-8 文件 | 无业务落盘 | 无网络、无 host_api | hello 基础即可；read_text 需 R。藏家后的解释器路径/依赖需正确放行；Node 协议检查不替代 OS 墙 | `plugins/hello-node/src/server.js:13-14`、`:41-81`、`:86-108`；`plugins/hello-node/declaration.json` 的 `entry.kind=interpreter` |
| image-text / v1 | 工作区图片；系统/设置指定的 tesseract、动态库和语言数据；包声明 | 图片副本只进自有 data/tmp，finally 删除；OCR 返回 stdout，不写工作区 | 无网络、无 host_api；启动本地 OCR 子进程 | 需 R；系统外 tesseract/语言包需 E，系统正常安装无额外 W。基础模式读不到图片；藏家的用户语言包和任意 tesseract_path 可能不可见 | `plugins/image-text/src/image_text/reading.py:41-77`；`ocr.py:22-45`、`:51-77`；声明 read=`read_only` 不排除私有临时写 |
| savepoint-lite / v1 | 工作区原文件（save/list/restore 都会读）；自有 snapshots 元数据和内容 | save 写自有 snapshots；restore 唯一覆盖工作区，先校验 expect、写上下文和 no-follow | 无网络、无 host_api | save/list 需 R；restore 需 R+W（授权原子替换所需目录）；私有历史保留。基础模式失去原文件读取，只有 R 时恢复失败 | `plugins/savepoint-lite/src/savepoint_lite/operations.py:20-41`、`:47-65`；`snapshots.py:52-59`、`:71-93`、`:129-158`；声明 save/list=`read_only`、restore=`mutating` |
| shuohao-novel-gates / v6 | 包内固定模块；主输入/参考/日志/卡片逐项通过读取上下文取得；不使用上游隐式文件加载 | 无业务落盘；不写用户或私有目录 | 无网络、无 host_api，不接出图服务 | 五个检查工具均需 R；基础模式只能握手，检查输入不可读。无需为上游“出图”名称授 N/W | `plugins/shuohao-novel-gates/src/server.js:12-14`、`:34-43`、`:54-68`；`workspace_files.js:42-66`；同目录 `gates.js` 固定模块加载；声明五工具=`read_only` |
| status-pet / v2 | 包内声明/art；启动设置；当次 `activity/run_state` 公开投影 | 无业务落盘 | 无网络、无 host_api，stdio 面板 | 基础即可；无需读用户家/会话正文 | `plugins/status-pet/src/status_pet/server.py:23-24`、`:51-70`、`:93-108`、`:120-122`；声明 `panels` |
| web-board / v1 | 工作区目录、预览/原始文件，逐 HTTP 请求用 serve 时冻结的读取上下文裁决 | 自有 `last-link.txt`（0600）；服务不写工作区；可选系统默认浏览器有独立桌面副作用 | 只监听 `127.0.0.1` 随机端口，GET/HEAD；无 host_api、无插件公网客户端 | 需 R+回环监听；初版只有 N 布尔则授 N 并揭示宽度。open_browser 另需 D；基础断网不能 serve，授 N 不代表另一台机器/飞书手机能访问该回环地址 | `plugins/web-board/src/web_board/board.py:37-51`、`:138-189`；`server.py:30-53`、`:130-137`；声明 serve/stop=`mutating`、status=`read_only` |
| workspace-peek / v1 | 工作区 UTF-8 文件、受限目录及子项；包声明 | 不写工作区或私有状态；游标仅返回给调用方 | 无网络、无 host_api | show/tree 需 R；基础模式不放行工作区，不能只因工具标 read_only 就自动开放 | `plugins/workspace-peek/src/workspace_peek/reading.py:28-35`；`preview.py:18-47`；`tree.py:30-60`；声明两工具=`read_only` |
| worktable-lite / v2 | 包声明、启动设置；宿主给的 sessions 编号/时间/渠道/当前标记，不读会话目录/正文 | 无业务落盘 | 无网络、无 host_api，stdio 面板 | 基础即可；会话列表投影不是读取其它会话权限 | `plugins/worktable-lite/src/worktable_lite/server.py:28-29`、`:73-90`、`:98-101`、`:114-128`；声明 `panels[].topics` |
| sdk / 构建模板（非插件） | 构建期读取四份 canonical 源、SDK pyproject、LICENSE/NOTICE；运行期由使用方传逐次上下文 | 构建临时工程/wheel 输出；运行期 SDK 仅提供安全 I/O 原语，不自行创建写根 | 无宿主/第三方运行依赖；不自带联网或 host_api；本目录只有 pyproject 与 conformance，无独立可执行 scaffold | SDK 无启用授权。Python 标准入口参考 workspace-peek/server.py；跨语言模板参考 hello-node、hello-go。模板必须分别申请所用 R/W/N，导入 SDK 不获权限；构建输出不算插件运行写权限 | `plugins/sdk/pyproject.toml:6-15`；`scripts/build_plugin_api.py:19-41`、`:46-58`；`agent_py_agent/agent/workspace_read_context.py:39-63`、`workspace_write_context.py:60-90`；`plugins/sdk/conformance/workspace_read_check.json` |

**补充破坏点，不得按表机械自动授权：**

1. `browser-lite` 的 http(s) 入口还受现有设置 `allowed_hosts` 约束（`plugins/browser-lite/src/browser_lite/access.py:22-36`）；该校验与 file: 读取校验不是网络隔离，浏览器还会取网页子资源。授 N 不能取消此合作门，也不能把它宣传成 OS 域名白名单。Chrome 的缓存、系统服务、字体、临时目录及子进程隔离也需真机验证；已有“只能写 data”并不能证明新读墙下可启动。
2. `desktop-lite`、web-board 的自动打开、harness-console 的窗口会交互桌面服务；Unix socket/DBus/Wayland、macOS Mach/LaunchServices 不是“授工作区 + 联网”就完整覆盖。经已有桌面程序的沙箱外操作尤其不能宣称继承插件文件墙。
3. tesseract、Chrome、Node/venv 路径只是程序入口，依赖目录另需核对；解释器前缀不能取成整个 `/Users/<用户>`、`.my-agent`、Homebrew 全前缀而顺便放行所有用户材料。
4. 长生命周期 web-board 会保留 serve 的读取上下文；进程授权撤销时必须停止该精确实例并确认退出，否则网页仍可能沿旧上下文读取。不能只从模型工具目录删掉它。

### 1.3 v1–v7 能声明的能力，不等于 OS 权限

| 清单 | 声明能力 | 不能据此推导的权限 / 实施差异 | source_ref |
| --- | --- | --- | --- |
| v1 | Python entry_module、entry_wheel/wheels；工具 input_schema/requested_effect；actions/default_action、settings_schema | 无读根/写根/网络权限字段；设置或工具参数路径不授 OS 访问；现有 Python 启用不走 v6 程序事实确认 | `agent_py_agent/agent/plugin_manifest.py:133-156`、`:305-326`、`:333-385`；`plugin_enable_tool.py:90-99`、`:197-203` |
| v2 | v1 + panels（公开 topics、渲染形状） | 面板进程仍需沙箱；topics 不授权会话文件或网络 | `agent_py_agent/agent/plugin_manifest.py:39-42`、`:144-170`、`:317-319` |
| v3 | 前项 + 随包 skills | SKILL.md 是方法，模型另走宿主工具权限；不是 plugin 读写声明 | `agent_py_agent/agent/plugin_manifest.py:231-235`、`:319-320` |
| v4 | 前项 + `host_api:["read"]` | 只读公开投影、activation 令牌按次代有效；不是管理员控制 API，更不隐含“公网可联网” | `agent_py_agent/agent/plugin_manifest.py:38-53`、`:321-323`；`plugin_host_api.py:14-20`、`:60-74`、`:117-157` |
| v5 | 前项 + tools 的 observation/observation_ref（候选、目标配对） | 只做观察候选合同，不是 OS 路径/网络授权；不能从候选参数猜权限 | `agent_py_agent/agent/plugin_manifest.py:38-78`、`:324-326`、`:497-511` |
| v6 | `entry/files/platforms` 文件入口；保留 tools/actions/settings/panels/skills/host_api/观察能力 | 固定可执行/系统解释器、文件摘要与平台确认；无 permissions；不要因换语言就降级沙箱 | `agent_py_agent/agent/plugin_manifest.py:280-293`、`:411-450`；[任意语言合同](PLUGIN_ANY_LANGUAGE.md#包描述-v6) |
| v7 | `package_kind=capability`、私有 capability/files/settings；可选 `capability.verification` 描述交付物/固定检查程序/关联输入/保留原件 | **无常驻插件进程、无 entry/tools/panels/skills/host_api**。内容检索不执行代码；有核验声明的包会另走管理员同意和专用检查器执行链，不能被 v1–v6 进程策略误改或漏报为“永不执行” | `agent_py_agent/agent/plugin_manifest.py:201-214`、`:295-300`；`capability_package_manifest.py:29-44`、`:47-104`；`capability_verification_manifest.py:1-5`、`:63-125`；`plugin_enable_tool.py:94-99`、`:153-169` |

SDK 的 `my-agent/workspace-read-context` / `workspace-write-context` 是握手协商后逐次 `_meta` 扩展，**各版本清单均不能静态声明这些读写根**。读上下文取 cwd 与精确范围交集，每个子项再走原路径墙；写上下文保留禁止根、锁文件、产物根限制，空根 deny-all。见 `agent_py_agent/agent/plugin_runtime.py:120-140`、`workspace_read_context.py:39-63`、`workspace_write_context.py:60-90`。它们是受信代码合作协议，不拦恶意原生文件访问。

v7 不在 `plugins/` 的 17 个样例中；补看 `examples/capability-packages/{drama-text-a,drama-workflow-b,security-evidence}/declaration.json`：前两者 `capability.verification.verifiers` 引用随包 Python 检查器及 target/关联输入，后者无 verification。包内脚本被保存为资源不授执行权；启用检查器的同意不允许常驻 MCP、联网、宿主控制面写入，也不替代检查器现有独立运行授权。本轮未执行检查程序。

**v7 核验执行链的读边界须单列**：`agent_py_agent/agent/capability/pack_verifier_runner.py:181-195` 核对 active 内容代、同意摘要、包与成员；`:201-218` 强制沙箱就绪后才启动；`:235-250` 规格为断网、整根只读、只写本次临时目录，参数只带 target/已解析关联输入。这**没有把 OS 读取收窄到 target/关联输入，更不等于新藏家读墙**。本轮未实测能读哪些系统路径；不能把“v7 无常驻插件进程”算“所有包内程序读取权限已收紧”。建议 3a 将其读墙接入同一底座作为独立切片：每次检查只放行固定检查程序/解释器必要依赖、临时目录、当次 target/关联输入及必要祖先，保持原强制断网与临时写规则、原 consent，不借 v1–v6 的 grandfather 放宽。是否与旧插件实施同批收口需 3a 定。

## 2. 现状与方案裁决建议

### 2.1 当前真正挡住什么

- `agent_py_agent/config/agent_config.yaml:819-824` 和 `agent_py_agent/agent/settings/config.py:180-183`：`plugin_process_sandbox=false`。关闭时插件继承系统用户权限；venv、`-I`、MCP 和管理员安装确认不是 OS 隔离。
- 开启现有开关：`agent_py_agent/agent/plugin_sandbox.py:19-23` 用 `read_only_root=True`，只有 data_dir 额外写根；网络未收、读取范围未收。Linux `agent_py_agent/agent/tooling/sandbox.py:271-282` 整根只读挂载同样不是“藏家目录”。该模式会挡 design/export/restore/fixture 的工作区写入，但不能防读取家目录。
- 数据目录由 `agent_py_agent/agent/plugin_runtime.py:174-175` 从 owner 的插件数据根推出；启动配置、host_api 令牌、沙箱/TMPDIR 沿同一客户端构造（`:187-221`）。读取、写入 `_meta` 仅在协商后发送；write 还检查工具 effect（`:120-140`）。不协商的旧插件不能自动获得可信上下文。
- 启用候选、业务连接、面板连接都是执行入口，不能只收紧其中之一。见 `agent_py_agent/agent/plugin_enable_tool.py:87-99`、`tooling/plugin_registration.py:17-53`、`gateway_parts/plugin_panels_http.py:98-100`、`:129-131`。
- 管理管理员门与执行原账保持，不能把业务操作者身份或模型的“已同意”当管理员许可：`agent_py_agent/agent/plugin_management.py:146-160`、`:207-242`、`:279-301`。v6 的确认事实包含包/程序/解释器，v1–v5 当前启用无同等程序事实确认（[原合同](PLUGIN_ANY_LANGUAGE.md#启用前的用户确认)）。新授权必须补到所有可执行旧包，不能只改 v6。
- 本基点 v8 启用在确认前返回 `plugin_events_disabled`，`agent_py_agent/agent/plugin_enable_tool.py:82-84`；本文没有验证 B7 返工分支或声称 v8 新读墙已能用。

### 2.2 建议接受主方向，但必须补四个条件

| 3a 倾向 | opp 评估 / 建议 |
| --- | --- |
| 老格式默认沙箱，读墙与 v8 一致 | **可作为目标**，限定可执行 v1–v6；共用 B7 真正的藏家底座而非把旧开关翻 true。基础仅读自有包/环境/依赖及系统必要资源，写自有 data。v7 保留内容/检查器的原独立合同。沙箱 unavailable 应拒绝而不是直跑。 |
| 启用时管理员确认工作区/联网 | **可行但必须是宿主结构化授权**。不改旧清单、不由插件名称/源码字符串/设置路径推断、不把建议表自动执行。需要选定路径根、分读写、揭示原子替换父目录权限；网络默认 deny，若只实现一个布尔，回环服务也必须明确授“所有网络”，不能虚称细粒度。 |
| 已安装升级照旧，重新启用新规则 | **须冻结旧代有效策略**，不是开机把所有无字段记录都当兼容。建议仅升级前已启用的固定安装/包/激活获兼容存续；已安装但停用的包首次/再次启用直接走新规则。若 3a 要所有旧已安装包首次启用也保留旧权限，须另裁定更宽的兼容窗口。停用、更新、卸载重装或主动重新启用终结旧豁免。 |
| 一个管理员边界配置控制新默认 | 建议新增 `plugin_legacy_permissions_enforced: true`（**拟议名**），只控制旧包下一次授权/启用的默认，不热扩已确认的权限；原 `plugin_process_sandbox` 保留为旧兼容模式写限制语义，避免老 YAML 的 false 把新规则整体关掉。v8 强制隔离不受这个新开关影响。 |

四个不能省的条件：

1. **读写工作区不是一个安装布尔。** 激活授权有具体项目根；逐次实际可合作访问范围是“管理员授根 ∩ 本调用原 cwd/精确 roots/路径政策”。写仍保留 effect 审批、H3、锁文件和产物限制。多工作区插件进程如果挂全部授根，恶意代码能直接读这些根，`_meta` 不能隔离它；因此不能宣传逐调用 OS 精确隔离。
2. **网络与 host_api 不能混成已安全闭合。** harness-console 的 host_api 是回环 HTTP，彻底断网会坏。初版 N=true 是对外、回环、监听一起开放；仅读宿主白名单靠 activation 令牌和路由鉴权，不能允许它借网络访问管理员控制 API。新方案仍依赖 Gateway 本机凭据强制链收口，未部署强制前不能声称防住网络提权。细粒度回环/域名策略需要平台能力或宿主 broker，不由确认文案凭空实现。
3. **外部运行依赖与桌面能力必须列出来。** 除 R/W/N 外有 E/D：用户目录里的浏览器、tesseract 数据或解释器依赖，桌面 IPC/默认应用都是实在的兼容点。不得按某样例写专用分支，也不得以 `chrome_path` 等任意字符串自动开目录。选定额外运行根须绑定程序事实；桌面 broker 或通用 `desktop_ipc` 授权模式需 3a 另定并实测。底座未支持时明确报能力不支持，保持旧激活兼容到管理员迁移，不能假称“新模式下完整照常”。
4. **权限必须随固定激活进入启动事实。** 确认只是同意，不是隔离证据；候选、业务、面板、重连、派生子进程都消费同一不可变策略。现有全局 bool 直接改变已运行插件将违背“升级照旧”。

## 3. 授权与执行合同（待定，未实现）

### 3.1 管理员授权的结构化事实

不把 `permissions` 塞进 v1–v7 安装包：`agent_py_agent/tests/test_plugin_manifest_v8.py:84-91`、`:240-247` 固定旧序列化且拒绝旧包偷带 v8 字段。新增宿主侧 `legacy_permission_grant` 可作为**原安装/激活记录的可选子对象**，无值时维持旧字节/目录摘要；不另建与激活竞争的授权表。

拟议子对象（字段形状、错误码和命令参数由 3a 定；不是当前 API）：

| 字段 | 事实来源 / 语义 |
| --- | --- |
| policy_version、mode | 宿主定义的 `restricted` / `legacy_compat`，非插件自报。未知版本拒绝新启动。 |
| install_ref、package_sha256、activation_generation | 复用当前安装引用、包摘要、激活代次；不同安装/更新/代次不借旧授权。 |
| read_roots / write_roots | 管理员选定的 canonical 有限绝对根；写根所需父目录读/改名/临时文件能力一并预览。原子替换若要授父目录，必须说明 OS 权限也覆盖该目录的其它子项，不能显示为“只准改一个文件”；SDK 的逐次裁决不拦恶意代码直接写兄弟文件。空数组为不放行，不回退 cwd/HOME。不存在的输出取已验证父目录，不接受符号链接或 `..` 放大根。 |
| runtime_read_roots | 解释器必要依赖 + 管理员明确选的外部程序/数据根。系统自动必要根和管理员额外根分列；来源及解析事实绑定摘要。 |
| network | 初版布尔：false=不开放网络；true=开放网络（含回环、公网、监听），不宣称限制域名或只连 host_api。若改细粒度，必须换结构化版本并测 OS 实施。 |
| desktop_ipc | 若获裁定，独立的通用能力/平台模式；未实现不接受 true。不得用网络授权替代 IPC、默许调用沙箱外代理程序。 |
| consent_facts_sha256 | 包/安装/运行时固定事实、策略版本、全部实际权限及声明 host_api 的同一规范摘要；原管理操作记已认证操作者/确认时间，不伪造 actor 授权。 |

建议管理员命令按现有 `/plugins enable` 增加结构化授权选择（精确 R/W/E、网络、已支持桌面模式），**输出带相同参数的完整 --confirm 命令**。当前实现没有这些参数；现有 TUI/飞书入口只保留原两步确认。缺授权也要给安全默认预览，而不是自动分析源码替用户勾权限。

确认事实必须明确列出：

- 插件 ID、版本、安装引用、包摘要；v6 继续列平台/入口/固定 argv、随包文件/shebang、固定解释器路径与摘要；Python 列固定 entry_module/环境和依赖身份，不执行用户代码来做预览。
- 基础只读目录和自有可写 data/TMPDIR；**额外工作区读根、写根、运行依赖根**逐项列真实解析路径及授权宽度。保留原根和新增根来源差异，不能只写“已授权工作区”。
- “禁止联网”或“允许全部网络（含公网、回环、监听）”；host_api 声明另列“可查宿主公开状态，不得操作管理员接口”。若桌面兼容存在，列通知/剪贴板/默认应用与沙箱外副作用边界。
- 模式、策略版本、沙箱可用/未验证/不可用；“要求隔离”不能显示成“隔离已实施”。确认前不准备运行环境、不启动候选或业务程序。
- 已存在授权项目对持久进程的可见性：一个进程可见其所有已授根，不承诺恶意代码只访问当前 `_meta` 的文件；新项目须管理员扩权并生成新代次/确认。

确认码覆盖**规范化事实**，不包含运行令牌或密钥。包、安装引用、策略、路径真实身份/依赖、解释器、网络/IPC 任何变化都使旧码失效；管理员第二步和候选启动前复核，发生漂移返回新预览。模型/插件不能经 arguments、_meta、自述、`requested_effect`、配置文本或旧确认码扩权。拒绝/取消/过期均不改现有激活、不启动新代码。

### 3.2 OS 边界及合作上下文

- 共用 M/B7 底座：家目录、宿主数据根、其它 owner/插件目录默认不可读写；放行固定包/环境、data、解释器的必要依赖、系统必要目录，再叠加当前固定授权根。**系统目录不是 `/`、`/Users`、`/home` 或 `/private` 的别名**；临时目录用 data 内专有目录，不能因 Linux `/tmp` 或 macOS `/var` 通配泄漏家目录。
- 宿主控制面、凭据/队列/配置、其它 owner 精准保护的优先级高于 R/W/E。授权一个含 `.my-agent` 的宽根或解释器前缀不能穿墙；以全 HOME、其它 owner 或宿主控制目录申请时拒绝，提示选具体项目。SDK 的 H3/原路径拒绝保持，不能借“管理员启用插件”取消它。
- macOS 拒家与子目录例外、Linux 空家视图/明确挂载、no-follow 打开所需祖先 metadata 必须组合验证；按具体底座合同实现，不能假定规则排列就能可靠覆盖。祖先可遍历不等于能列出/读取祖先内容。
- 未协商 SDK 的旧插件也只能访问 OS 授根；协商的插件仍获得原冻结上下文与 grant 的交集。缺失可信上下文不从插件 cwd、安装配置或 parameters 补权限。写 W 不使 read_only 工具得到逐次写授权，私有 data 写例外须在预览写清。
- 复用原激活、进程资源账、配额、期限、取消、退出确认和通道池。受限通道键至少绑定激活和权限摘要；若未来按任务沙箱实现更窄的工作区视图，通道键还须绑定有效 OS 根/身份，不能在共享客户端上热换权限。现阶段可先按有限项目根固定视图实施，但如实标为激活级隔离。
- 停用/撤销/缩权后不得新调用或重连；旧连接与其桌面/HTTP/浏览器子进程按精确归属回收并确认。退出未知保留 unknown/cleanup_pending，不开放新代。data 不因缩权删除，savepoint/作业账可保留，但不能因此让新授权外的原文件变可读。
- 沙箱不可用、应用依赖不可见、断网无法施加时 fail-closed，分别给原因；不静默兼容、不临时直跑、不自动放宽 E/N/D。v7 检查器按原专用同意和运行链处理，本轮不统一重写。

### 3.3 一个新默认开关的管理员边界

`plugin_legacy_permissions_enforced` 拟默认 true；不进 `TUNABLE_KEYS`，同时进入 `BOUNDARY_KEYS` 与 `USER_SETTINGS_BOUNDARY_KEYS`，复用已认证管理员 `/settings` 的短生命周期授权。依据：`agent_py_agent/agent/settings/user_config_capability.py:96-98`、`:155-187`（当前名单**还没有该键**）。普通 owner、主/子模型的 `user_config`、CLI 自助白名单不能写，也不能借说“用户让我改”放开。

未来产品提交须同步 AgentConfig、YAML 中文注释、布尔 normalize、参数中心登记/说明和管理入口测试；不因存在 bool 字段便默认模型可改。本轮没有新增配置。

策略优先级建议：v8 强制策略 > 已确认 restricted 激活的冻结策略 > 显式旧代 legacy_compat 记录 > 新开关决定待启用旧包的默认。原 `plugin_process_sandbox=true` 的兼容记录仍是只写 data、读/网未收，不得展示成 restricted；false 仍是系统用户权限。管理员关闭新默认只影响下一次管理授权，须显示“以旧宽权限启用”的确认事实；**不会自动把已 restricted 的激活降级**，也不能关闭 v8 的隔离。

不引入热改运行中沙箱。配置保存提示下一次 Gateway 配置加载生效（当前进程未变），且只在后续授权时选默认；重新启用后的模式由固定记录决定，不被全局开关每次启动重新覆盖。只提供一个控制新默认的配置；R/W/N/E/D 是逐插件授权事实，不再增一堆全局开关。

## 4. 兼容存续与迁移步骤

### 4.1 状态与升级行为（建议口径）

| 原状态 / 动作 | 升级后的行为 | `/plugins` 展示 |
| --- | --- | --- |
| 升级前已启用 v1–v6，包/安装/激活身份完整 | 一次性迁移原激活为 `legacy_compat`，冻结原实际沙箱模式；服务重启/业务重连仍按这个固定策略，**不借新默认偷偷收紧** | “权限待收紧，重新启用时会按新规则确认”；同时如实列“系统用户权限”或“仅限制写入数据目录，读取/网络未收紧” |
| 已安装但停用 / 从未启用的 v1–v6 | 不启动；下一次启用按新默认预览/确认，无旧权限自动豁免 | “未启用；下次启用需权限确认” |
| 新安装 / 卸载重装 / 更新包 | 安装仍只做原静态检查；启用需新包新授权。安装确认≠运行权限确认，不借旧 install_ref 或旧包授权 | “待启用 / 待确认”；更新需提醒旧权限不会自动带到新包 |
| 旧兼容插件主动重新启用（即使目前 enabled） | **必须进入权限预览**，不能被原“已启用”幂等返回吞掉。管理员完成新事实确认后，新代采用 restricted；并沿原事务/资源撤销处理旧代 | 预览旧权限→新权限及将停用旧实例的影响；发布并确认收尾后才显示“已按新规则启用” |
| 已 restricted 插件重连/宿主重启 | 从原记录恢复已确认权限，不重新询问、不临时扩大；包/解释器或依赖漂移则拒绝并提示重新确认 | 显示固定模式、授根/网络摘要、实际沙箱状态/失败原因 |
| 改授权范围、网络、运行依赖、IPC / 换解释器或平台事实 | 新预览、新确认、新代次；旧码作废；不得原地改共享连接 | “权限变更待确认”；尚未执行不能显示“已改好” |
| v7 无检查器内容包 | 原内容激活/资源读取，无插件进程可套此默认 | “内容包（无常驻插件进程）”，不打“旧宽权限”标签 |
| v7 有 verification | 保留原检查程序同意、专用沙箱与同意摘要复核；本文默认不重定义它 | “内容包；已同意/待同意宿主检查程序”，不冒充纯文本永不执行 |
| v8 | 仍走 M 线强制策略；本基点关闭门不变 | 当前按 `plugin_events_disabled` 报不可启用，不伪造 B7 状态 |

**兼容窗口是主动留下的风险，不是修复完成：**旧插件继续可能读家目录、凭据和队列，未断网的插件继续可能访问回环控制面。升级只保护新授权/新代；列表提示不是 OS 收紧。管理员重新启用完成前，不能汇报“所有插件权限已经安全”。不自动扫描旧插件来“猜必要权限”，不设无人确认的强制收紧期限。

### 4.2 一次性迁移如何不变成永久旁路

1. 3a 先定兼容集合和配置语义。推荐只保留升级前**已启用且原身份完整**的激活；宽到所有已安装记录须明确接受风险。
2. 宿主升级首次迁移读取原安装表/激活及旧生效配置，沿原安装事务一次性记录迁移版本和精确 grandfather 集合/策略。迁移快照与新安装写入串行/CAS，不把未来“缺少新字段”记录自动视为旧安装。无字段不是权限证明；损坏/未知安装身份不能自动获得兼容权。
3. 新版可读旧字段、旧摘要和工具目录；原 v1–v7 包字节、catalog digest 不加授权字段。记录中的新可选子对象与迁移事实有唯一权威；只读视图派生标签，不另做控制授权的缓存文件。尚未完成原事务尾部的条目按原 pending/unknown 恢复，不能冒认已迁移。
4. 先只展示警示，旧固定激活按原有效模式继续；**不能直接把 plugin_process_sandbox 默认改成 true**。原全局 false 的旧实例需要明确兼容标记，原 true 的旧实例保留原写限制，二者都不是 restricted。
5. 管理员选一个插件和具体项目：照第 1 节选 R/W/E/N，基础足够的面板也要确认基础事实。新增项目或输出根另确认；飞书输入的相对路径必须由宿主当次可信 cwd 解析并展示 canonical 路径，不能拿插件/消息进程 cwd 猜。
6. 预览阶段不启动新代码、不撤旧激活。带码执行复核身份/权限/可用底座，按原固定激活 CAS、候选握手/完整目录校验、发布和尾部确认收口。旧兼容实例的精确撤销必须完成，不能保留带旧授权的 HTTP 服务或浏览器进程。资源未确认退出不开始新代，不能借此绕原资源账。
7. 失败分阶段报告：未执行/未发布保持原事实；若旧代已撤销、候选又失败，则明确“旧实例已停，新规则启用失败、待处理”，保留安装/私有数据，**不自动重新无沙箱运行**。不承诺原链尚未支持的零中断切换或失败无损回滚。产品实施时需专门覆盖“enabled 的重新授权”而不把它当普通首次 enable。
8. 完成发布及收尾后移除该旧代兼容标签，新代持久化 restricted；其它插件不受波及。停用再启用、更新/重装不复活 grandfather；宿主重启不丢授权。兼容码不得用于降级新代。
9. 实施与发布先试基础面板和 hello-go，再试只读工作区、工作区写、网络/host_api、外部依赖/桌面；每步按真实功能复核。管理界面显示仍有旧权限的插件，而不是根据总开关宣布收口。回退二进制可能不识别新授权，必须单独设计 downgrade 阻断/管理员警示，不把旧程序读取新记录当安全回滚。

## 5. TUI 与飞书分别看到什么

只扩现有 `/plugins` 服务/预览回执，不另造安装入口。飞书 C10 已复用 TUI 管理服务，当前真实渠道验收仍待做（`docs/ROADMAP.md:76-79` 是现有状态来源，不修改此文档）。下面是**拟议展示**，不是当前截图或已验证页面。

### 5.1 TUI

- `/plugins`：原 enabled/版本/简介保留，附权限状态摘要。旧启用样例显示：
  - `workspace-peek · 已启用 · 旧权限`
  - `权限待收紧，重新启用时会按新规则确认。当前仍按旧权限运行，读取和网络尚未收紧。`
- 详情：基础目录、额外 R/W/E、网络、host_api/桌面模式、来源（旧兼容/管理员确认）、固定包与代次摘要、**观察到的**沙箱 applied/unavailable/未验证。列表摘要不等于实际进程隔离证据，未启动仅显示要求与就绪检查。
- `/plugins enable ...` 新预览按第 3.1 节完整显示事实。示例说明（使用占位项目根，不是真实用户授权）：
  - `沙箱：要求启用；基础目录外拒绝。`
  - `工作区读取：<管理员选定项目根>；工作区写入：不授予。`
  - `网络：禁止；宿主 API：未声明。`
  - `权限只绑定这个安装/包；新增项目需重新确认。`
  - 最后一行是宿主生成的、**带相同权限参数**的 `/plugins enable ... --confirm <码>`。本轮未设计固定命令语法，不应手抄一个现在无法解析的参数示例。
- 确认前不启动；按码提交才执行，取消不改当前实例。失败保留原因：无沙箱、依赖不可读、缺项目授权、桌面不支持、授权/解释器漂移、退出未确认等，不能只显示“启用失败”。

### 5.2 飞书私聊与群聊

- 未绑定管理员的普通私聊/群聊可按原可见目录读列表和使用卡；不显示完整管理员路径/安装设置或确认码，管理拒绝并提示去本机 TUI 或已绑定管理员私聊。不因发消息者文本自称管理员提升权限。
- 已经通过宿主 `/admin` 绑定的管理员私聊消费**同一份结构化预览/确认事实**，内容与 TUI 一致，不能少掉网络/额外根/桌面边界。沿 C10 两步 `/plugins enable ...` + `--confirm`，不假设现有飞书有新权限选择按钮或确认卡。
- 长回执须可分页/分段读完，首段明确未完整展示，确认命令仅在完整预览交付后给出；分页绑定同一事实摘要，不把截断或未送达当作管理员看过全部权限。失败/超时沿原操作编号查询，不偷偷重试授权。
- 重新启用成功后，回复新模式和收尾状态；unknown/cleanup_pending 分开报告。“已批准”不能显示成“已隔离”。跨群转发、过期消息或旧码不换身份/安装绑定。
- web-board/harness-console 的 `127.0.0.1` 指 **Gateway 所在机器**，飞书手机/另一台电脑不是那个回环。已授网络≠外部可达；此轮不设计公网转发或 LAN 暴露，需在宿主机器打开链接或另行安排已有授权的访问方式。真实飞书收发、分页完整性及管理员拒绝链**未验证**。

## 6. 交给 3a 的实施切分与验收条件

### 6.1 裁定（3a 2026-10-03 定）

1. **一次性兼容仅给升级前已启用的激活**；重新启用、更新、重装结束豁免，不能因新字段缺失补兼容。实现以唯一安装表的显式旧版本来源迁移，不另建授权账。
2. **N 初版是诚实的总开关**，回环、公网、监听一起授权；确认事实必须写“这个插件能连网，也能连本机端口”。G2b 服务端强制落地前不宣传隔离闭合。
3. **E 必做且是通用结构化授权**，管理员指定哪些程序路径或前缀，完整写入确认事实；不按样例开口。D 底座未好，相关功能保留旧激活兼容，明确已知边界。
4. **新配置 `plugin_legacy_sandbox_default=true`**，进入 `parameter_registry._BOUNDARY_NAMES` 和模型不可写边界，测试钉住 `PARAMETER_BOUNDARY`。管理员关闭只影响之后的显式宽权限授权，不降级既有 restricted 激活；旧 `plugin_process_sandbox` 不承担新默认。
5. **v7 当次输入读墙另片后续**：保留核验器原 consent、断网和临时写合同，不在本批实现，也不借 v1–v6 的兼容迁移。

本批按段交付，先做授权、唯一安装表版本迁移、管理员配置和 B7 接缝；OS 规则不得另写。候选/业务/面板接线、完整确认运输及非作者初审完成之前为 WIP，不可直接部署或称权限目标完成。

### 6.2 后续实现边界

- M/B7 负责统一读墙/网络施加底座、macOS/Linux 探针；opp 后续若获授权负责旧包管理员授权事实、一次性迁移/有效策略与管理投影。开始实现前对齐 B7 精确接口，不能复制另一套沙箱。
- v7 核验器由能力包执行链单独接读墙，保留每次检查和原 consent/断网/临时写语义；不与 v1–v6 常驻插件迁移混写权威记录。需要与相邻 M/能力包 worker 约定接口和写入边界。
- 候选/业务/面板三路接线必须同批合同核对，或明确由单一集成者接入；共享文件 `plugin_runtime.py`、`plugin_enable_tool.py`、安装/激活记录不要同时派多工作树改同一区域。
- TUI/飞书只负责同一事实格式、权限参数入口、管理员边界与完整预览；不得按展示文本写判断。样例依赖说明/模板可与核心授权工作并行，但不写“按插件 ID 特判权限”的产品代码。
- 初版盘点只改本设计与台账；3a 2026-10-03 已授权产品实现。实现按规范同步配置/dataclass/normalize、模块文档、TESTS、CODEBASE_TREE、LLM_GUIDE 和尺寸守卫；仍不改 STATUS/ROADMAP/COMPLETED。

### 6.3 必测清单（未来实现不能拿本轮静态结果替代）

- 旧清单 v1–v7 固定字节、catalog digest、原安装/启用/停用/重装/升级；v7 有/无 verification，v8 强制规则不受旧开关影响。
- 新安装、旧停用、旧 enabled 重授权、旧兼容重启/重连、一轮迁移与并发新安装/更新/重复命令；旧 code/旧包/旧解释器/旧权限/新 install_ref 拒绝，unknown 不能冒充已授权。
- 非管理员 TUI/飞书/模型主子代理不能启用或改边界开关；管理员配置入口能保存并如实报生效时间，模型 `user_config`/CLI 自助路径拒绝。
- 不启动的预览；候选、工具、面板、HTTP 留存实例与派生进程统一策略；失败及撤销精确回收并确认，不发生直跑降级、旧实例残留或共享客户端热扩权。
- macOS/Linux 真 OS：基础目录可读、自有 data 可写；家目录/其它 owner/其它插件/宿主凭据及队列/未授项目不可读写；授 R 仍不能写、授 W 能完成原子替换、链接与祖先/改名/宽前缀不能逃逸；解释器位于用户前缀、浏览器缓存/tesseract 数据按有限 E 工作；祖先 metadata 不能泄露正文或列表。
- 默认断网同时验证公网、IPv4/IPv6 回环、监听；N=true 行为按回执实际宽度验，host_api 令牌停用/换代失效且不能操作管理路由。若实现细粒度，逐种验证不允许流量真的被拒，不只测参数 bool。
- 按第 1 节逐样例实际功能：四面板、hello、预览/目录/Node/五阶段门、OCR、create/edit、table/export、save/list/restore、fixture 全流程、file/HTTP 页面、web-board 取页、harness-console 取宿主公开状态及桌面/剪贴板。拒绝行为也验；未来没授权的功能应该明确失败，而不是静默返回空数据。
- TUI/飞书真实管理员与普通身份、完整回执、按码再启用、列表警示消失、权限漂移错误/操作查询；网页的外部观察点另测，回环成功不算手机/另一机器可达。

## 7. 本轮验证记录与未验证边界

本轮是方案交付，不是权限功能交付。验证针对 step17i 基点的现有合同和本轮两个文档；产品版本、输入、观察点未变化的结果复用，不为每次文档追加重跑产品测试。

已执行（仓库根、指定 Python）：

```bash
PY=~/.my-agent/releases/claude-tools/ci-venv-312/bin/python
PYTHONPATH=$PWD PYTHONDONTWRITEBYTECODE=1 $PY -m pytest \
  agent_py_agent/tests/test_plugin_manifest_v8.py \
  agent_py_agent/tests/test_plugin_catalog_digest_stability.py \
  agent_py_agent/tests/test_workspace_read_context.py \
  agent_py_agent/tests/test_workspace_write_context.py \
  -q --tb=short -p no:cacheprovider --basetemp=/private/tmp/claude-501/m-opp
```

结果：退出码 0，四个指定文件实际执行至 100%，输出无失败/跳过。只证明现有旧清单字节、关闭门及合作读写上下文合同，不证明拟议权限/迁移已实现。使用临时测试文件、伪造 owner 和不执行插件的合同入口，没有启动真实 Gateway 或插件沙箱。

```bash
PYTHONDONTWRITEBYTECODE=1 bash ~/.my-agent/releases/claude-tools/3a-scripts/size_diff.sh "$PWD"
```

结果（脚本原输出）：

```text
新增告警: 0
消失告警: 16
```

退出码 0；差集是本基点对脚本持有的线上清单，并非本次文档删除了 16 条产品告警。产品代码未变，后续只改文档可复用这个尺寸结果；脚本未写仓库 CODE_SIZE_REPORT.md。

文档检查：仓库内 Python 静态核对第 1.2 节行名与 `plugins/` 目录逐项双向一致（18 目录/18 行、missing/extra 均空）；解析 129 个完整/省略的文件:行范围，文件及行范围错误 0；本设计 2 个链接目标文件缺失 0；唯一声明 host_api 的样例为 harness-console。检查退出码 0，并断言只变更这两个文档、分支/基点正确；此检查不替代 OS 行为验收。另执行：

```bash
git diff --check
PYTHONDONTWRITEBYTECODE=1 $PY scripts/check_doc_sync.py
```

结果：退出码 0、`DOC_SYNC_PASS`；加入新文档后的 `git diff --cached --check` 及 `scripts/check_doc_sync.py --staged` 也已实际执行、退出码 0。之后只补本节结果和授权宽度说明，提交前再检查最终暂存 diff；没有把未暂存新文档时的旧检查当完整提交检查。

未做全仓 pytest、未运行 guards9/产品发布整套门禁（本轮无产品改动）、未改 TESTS/CODEBASE_TREE 或状态文档，未执行新默认/授权/迁移/OS 真隔离/真实 TUI/飞书/桌面验收；没有伪造跳过或删除测试。3a 裁定方案并派实现后，须按第 6.3 节及工作规则在可真实启动沙箱的环境复核。
