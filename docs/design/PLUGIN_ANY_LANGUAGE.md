# 任意语言插件（plugin_package.v6）

状态：第一阶段本地实现与组件验证完成（2026-09-25，分支 `claude/plugin-any-language`），真实 TUI 验收记录见文末；
尚未合并部署。第二阶段的 OS 沙箱试点见最后一节，未开始实现。

## 用户决定（2026-09-25）

1. 插件可以用系统里已经装好的解释器（如 node、deno、ruby）运行。
2. 非 Python 插件怎么检查工作区路径，由开发方选方案并实测（用户不懂这块，交给我们测）。结论见“第 2 项实测”。
3. 包里带可执行文件、或要用外部解释器的插件，启用前必须由用户本人确认。
4. OS 级沙箱可以尝试（第二阶段试点）。

## 为什么这样做

插件和宿主之间走 MCP stdio，本身与语言无关。宿主真正绑死 Python 的只有三处：包描述（`entry_module`、wheel 清单）、
环境准备（venv + 离线 pip）和启动命令（`<venv>/bin/python -I -m <模块>`）。v6 只替换这三处；安装表、激活 CAS、
托管进程与资源账、完整工具目录核对、撤销、工具执行器、随包 Skill、面板、宿主只读 API、观察候选全部原样复用，
不另起状态或执行链。

入口只有两种**启动机制**：随包可执行文件（`executable`），或系统解释器加随包脚本（`interpreter`）。
解释器名、平台标记都是开放集合，只校验格式，不维护“支持哪些语言”的名单，也没有按语言分支的代码。

## 包描述 v6

与 v5 相比，去掉 `entry_module`、`entry_wheel`、`wheels`，新增三个字段；其余字段（actions、tools、settings_schema、
panels、skills、host_api，以及工具上的观察字段）含义不变，面板/Skill/宿主 API 可为空列表。

| 字段 | 形状 | 规则 |
| --- | --- | --- |
| `entry` | `{kind, command, args, interpreter?}` | `kind` 为 `executable` 或 `interpreter`；`command` 是随包文件路径；`args` 最多 16 个固定字符串、不含控制字符；`interpreter` 只能是程序名（不能带路径），且只在 `interpreter` 类型出现 |
| `files` | `[{path, sha256, executable}]` | 包内相对路径（每段 `[A-Za-z0-9_][A-Za-z0-9_.+-]*`，最多 8 层），不能是 `plugin.json`，大小写折叠后不重名，最多 127 个（读包器默认成员上限 128 含描述文件）；`executable` 必须是布尔值 |
| `platforms` | `["any"]` 或 `["darwin-arm64", "linux-x86_64", …]` | 至少一个；`any` 表示与平台无关（比如只有脚本）；其余格式为“系统-架构”，启用时与本机标记精确比较（`amd64/x64` 统一为 `x86_64`，`aarch64` 统一为 `arm64`） |

一致性约束：入口文件必须在 `files` 里，`executable` 类型的入口必须声明执行位；`skills/` 下的 `SKILL.md`（大小写折叠比较、
任意深度）必须恰好是声明的 `skills/<名称>/SKILL.md`——Skill 目录是按 `SKILL.md` 递归扫描的，未声明的也会被暴露。
ZIP 成员必须与 `plugin.json` + `files` 完全一致，逐个核对摘要；成员路径、大小、压缩方式等沿用原读包器规则。

兼容性：Python 包继续用 v1–v5，读写字节不变（用 14 份 v1–v5 声明在 main 与本分支上对比 `to_payload()` 输出，sha256 相同）；
激活目录摘要只哈希工具声明，v6 没有给 `PluginToolDeclaration` 加字段（`test_plugin_catalog_digest_stability.py` 通过）。

打包脚本 `scripts/build_plugin_files_package.py`：声明里的 `files` 只写 `path` 和 `executable`，摘要和协议版本由脚本生成；
`--platform` 与声明里的 `platforms` 二选一；成员时间戳固定，同一输入得到同一包字节；产物交宿主读包器复核后才独占写出。

## 启用前的用户确认

1. `/plugins enable <插件ID>`：宿主只读安装快照和本机事实，生成确认回执后以 `confirmation_required` 结束，**不准备环境、
   不启动任何程序**。回执列出插件/版本/包摘要、入口与固定参数、本机平台与包声明平台、每个随包文件的路径/大小/摘要/
   执行位/首行 shebang，以及解释器的名称、绝对路径和摘要，最后一行给出 `/plugins enable <插件ID> --confirm <确认码>`。
2. 用户核对后原样输入这条命令，启用才沿原链继续（准备环境 → 启动候选 → 核对完整工具目录 → 发布）。

确认码是回执内容的 sha256 前 12 位，不是密钥或授权令牌。包、平台、解释器任何一项变化，确认码都会变，旧码作废；
事实没变时再次启用得到同一个码（确认的是这些事实，不是一次性口令）。管理工具对模型不可见，确认码只能由用户在
TUI/Gateway 里输入命令。没有用宿主审批面板，是因为管理命令的审批说明只显示工具名，放不下文件和解释器事实；
两步命令把事实放进回执，确认绑定在事实摘要上。Python 包（v1–v5）启用流程不变。

## 运行时事实与解释器固定

- `executable`：事实只有“入口类型 + 本机平台”；包声明的平台不含本机时启用失败（`platform_unsupported`）。
- `interpreter`：启用时按 **Gateway 进程的 PATH** 解析解释器名 → 取真实路径 → 必须是普通可执行文件 → 计算 sha256。
  全程只读文件系统，不执行解释器（连 `--version` 也不跑）。找不到报 `interpreter_not_found`，不可读报 `interpreter_unreadable`。
- 事实的指纹写进环境计划原有的 `interpreter_fingerprint` 字段（格式不变），环境引用随之变化；确认回执、计划和准备用同一份事实。
- 准备环境时重新解析，指纹必须等于计划，否则 `environment_interpreter_changed`，不创建候选目录。
- 解释器类型在环境目录写 `runtime.json`（0600，排他创建，不跟随链接），记录解释器路径、摘要与文件 stat。每次启动插件进程前：
  stat 没变直接用；变了就重算摘要，内容不同、文件消失或不可读都报 `interpreter_changed`，插件工具不接入，
  需要停用后重新启用并确认。定位文件被改（指纹对不上）报 `interpreter_pin_invalid`。
- 可执行类型每次启动核对本机平台（`platform_changed`）。

## 环境准备与启动

- 不建 venv、不跑 pip、不执行任何随包文件；原计划、候选地址、配额准入、期限、原操作授权和候选身份核对不变。
- 随包文件解包到 `<环境>/files/`：目录逐段 no-follow 创建，文件排他创建且不跟随链接；可执行文件设为 0500，数据文件 0400；
  写完读回核对权限与摘要，任何不符都拒绝。
- 启动命令只由宿主推出，包不能提供宿主路径或额外参数：`executable` 为 `files/<command> <args>`；
  `interpreter` 为 `<固定的解释器绝对路径> files/<command> <args>`；工作目录是 `files/`。
  环境变量与 Python 插件相同（`MY_AGENT_PLUGIN_SETTINGS`、`MY_AGENT_PLUGIN_DATA_DIR`、声明 `host_api` 时的宿主只读 API），
  PATH 等基线变量照常传入。
- 注意：`executable` 类型的脚本如果用 `#!/usr/bin/env xxx`，解释器是启动时才从 PATH 找的，宿主不固定它——
  这正是回执要展示 shebang 的原因。需要固定解释器请用 `interpreter` 类型。
- 随包 Skill 位于 `files/skills/<名称>/SKILL.md`，随激活出现、随停用消失。

## 给插件作者：与语言无关的协议要点

- 传输：标准输入输出上一行一条 JSON-RPC 2.0 消息；标准输出只写协议帧，日志写标准错误。
- `initialize` 回 `protocolVersion`（宿主发 `2024-11-05`，也接受 2025-03-26、2025-06-18、2025-11-25）、`capabilities`、`serverInfo`。
- `tools/list` 返回的工具必须与包描述的 `tools` 完全一致（名称、说明、`inputSchema` 规范化后相等），否则启用失败。
- 环境变量：`MY_AGENT_PLUGIN_SETTINGS`（JSON 设置）、`MY_AGENT_PLUGIN_DATA_DIR`（插件私有数据目录，跨版本保留），
  声明 `host_api: ["read"]` 时另有宿主只读 API 地址与令牌，见[宿主 API](PLUGIN_HOST_API.md)。
- 工作区读取：`initialize` 在 `capabilities.experimental` 声明 `my-agent/workspace-read-context: {"versions": ["1"]}` 后，
  宿主在每次 `tools/call` 的 `_meta` 里附带本次上下文；读任何文件前都要按它裁决，见[读取上下文](PLUGIN_WORKSPACE_CONTEXT.md)。
- 面板、观察候选的字段在 v6 里同样可用，见[面板](PLUGIN_DISPLAY.md)与[观察候选](PLUGIN_OBSERVATION_CANDIDATES.md)。

## 第 2 项实测：非 Python 插件怎么检查工作区路径

做法：宿主 Python 实现（`workspace_read_context.py` + `path_access_policy.py`）是唯一参考；每种语言各自移植这段检查
（Node 版约 270 行含注释），必须跑通同一批一致性用例。用例在 `plugins/sdk/conformance/workspace_read_check.json`：
一棵含符号链接的目录树、7 个上下文、74 条路径裁决和 20 个必须整体拒绝的畸形上下文，期望值由参考实现裁决，
pytest 同时断言参考实现与期望逐条一致、Node 移植与期望逐条一致。

结果：

- Node 样例的移植（`plugins/hello-node/src/workspace_read.js`）74 + 20 条全部一致。
- 变异验证：8 种典型错误写法全部被用例抓出——先按字面折叠 `..`、直接用 Node 自带的 `fs.realpathSync`、按字符串前缀而不是
  按路径段判断包含、full 模式先于 owner 墙、漏掉外部授权根复核、`.env` 前缀判断过宽、不核对外部策略模式、
  按请求的文件名而不是真实目标判断凭据文件。
- 结论：方案可行。最容易错的是“先解析符号链接再处理 `..`”：多数语言自带的 realpath/normalize 与宿主语义不同
  （要么要求路径存在，要么先按字面折叠 `..`），必须照参考实现逐段解析。
- 打开文件：Node 没有 openat，样例采用“打开裁决给出的真实目标（末段不跟随链接）→ 重新解析并比对 dev/ino”的打开后复核，
  检查与打开之间被换成链接时拒绝。竞态强度低于 Python SDK 的逐段目录描述符打开，这点写在样例说明里。
- v1 不提供写入上下文的跨语言用例：非 Python 插件暂不建议协商写入扩展（宿主仍按声明下发，但没有可对照的移植用例）。

## 样例

- `plugins/hello-node`：`interpreter` 类型（node），平台 `any`；工具 `hello` 与 `read_text`（按宿主读取上下文读文本）。
- `plugins/hello-go`：`executable` 类型，按目标平台交叉编译，平台标记在打包时用 `--platform` 给出；工具 `hello`。
- 构建与安装步骤见两个样例目录的 README。

## 边界与已知限制

- 插件仍是受信代码，以用户本人权限运行；读取上下文是协作协议，不是 OS 沙箱（第二阶段试点）。安装与启用仍只限管理员。
- 与 Python 插件一样只支持具备 no-follow/目录描述符能力的 POSIX 宿主；Windows 未验。
- 解释器按 Gateway 进程的 PATH 解析；以服务方式启动的 Gateway，PATH 可能和交互 shell 不同，找不到时回 `interpreter_not_found`。
- `interpreter` 类型只固定解释器可执行文件本身；它加载的动态库、全局模块目录（例如 node 的全局 `node_modules`）不在固定范围内。
  样例只用标准库。

## 验证

- `test_plugin_any_language.py`：包描述合法/非法、可复现打包与篡改拒绝、运行时事实与真实路径、确认回执与确认码、
  未确认不执行、带码启用后真实 MCP 调用与停用释放、解释器固定（stat 未变不重算、变了重算、内容变/文件消失/定位文件被改均拒绝）、
  平台变化拒绝、准备阶段拒绝解释器被换、解包权限与随包 Skill。
- `test_plugin_any_language_samples.py`：一致性用例（参考实现 + Node 移植），hello-node / hello-go 经宿主真实安装、确认、调用
  （本机没有 node / go 时跳过）。
- 宿主侧 12 个变异（跳过确认、确认码不含解释器摘要、定位文件不比指纹、不重算摘要、一律给执行位、准备不核对计划、
  不取真实路径、忽略解释器、Skill 不核对、读包只认 wheel、可执行入口无执行位、平台不检查）全部被测试抓出。

## 第二阶段：OS 沙箱试点（计划，未实现）

- 仓库已随包带 bubblewrap（Linux）并有 `tooling/sandbox.py`；macOS 用 sandbox-exec。试点只包插件进程（Python 与 v6 同一入口），
  默认关闭，开关按配置规范同步 YAML 与 dataclass。
- 插件启动链路经过后台进程启动与监听范围检查（对端维护），接入前与对端对齐包装方式与进程身份记录。
