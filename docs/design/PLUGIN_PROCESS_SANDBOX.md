# 插件进程 OS 沙箱（试点）

状态：2026-09-25 本地实现、组件验证与真实 TUI 验收完成（macOS Seatbelt、测试机 Linux bubblewrap 0.8.0），默认关闭；尚未合并部署。
来源：用户对任意语言插件的第 4 项决定“OS 级沙箱可以试试”。

## 开关与语义

配置 `plugin_process_sandbox`（`agent_config.yaml`，默认 `false`）。打开后，**所有**插件进程（Python 包与 v6 非 Python 包、
业务连接、启用时的候选连接、面板连接）都经唯一的平台沙箱网关 `AttemptExecutionSandbox` 启动：

- 读：与宿主相同（不收窄）。Linux 用 bwrap 的“整根只读”形态（`--ro-bind / /`，再挂新的 `/dev`、`/proc`）；
  macOS 用 Seatbelt 的非 full 形态（默认放行，`deny file-write*`）。
- 写：只有该插件自己的数据目录（`MY_AGENT_PLUGIN_DATA_DIR`）；`TMPDIR` 指向其中的 `.tmp`，临时文件也落在这里。
  插件环境目录、工作区、my-agent 数据目录的其它部分都写不进。
- 网络：不变。这个沙箱**不是网络边界**；后台服务的监听范围检查只作用于 `run_command` 后台会话，不覆盖插件进程。
- 进程：Linux 另有独立 PID/UTS/IPC 命名空间与 `--die-with-parent`；macOS 的 `sandbox-exec` 直接 exec 目标程序，进程身份不变。
  停用或关闭连接时停掉包装进程即可带走内层进程，与 `run_command` 沙箱后台同一做法（测试验证停用后没有残留进程）。

改开关后重启 Gateway 生效（配置在启动时读取），之后启动的插件进程都按新值运行。

## 失败时怎么办

沙箱打开但本机不可用（Linux 没有可用的 bubblewrap 或无法创建命名空间，macOS 没有 `sandbox-exec`）时一律拒绝，
不退回无沙箱启动：

- 启用：在准备环境、启动候选之前返回 `reason=sandbox_unavailable`，TUI 显示具体说明。
- 显式调用 `/plugins@<插件>`：建运行之前以 `PLUGIN_RUNTIME_UNAVAILABLE` + `details.reason=sandbox_unavailable` 拒绝。
- 模型侧：插件业务连接创建失败，该插件的工具不接入本轮（原注册链的既有处理）。

## 已知限制（试点）

- 需要写工作区的插件（写入上下文协商成功、声明 mutating/dangerous 工具的，例如导出、按快照恢复）在沙箱里写工作区会失败。
  插件进程是长驻的、同一进程服务不同会话，沙箱只能在启动时定边界，没法按每次调用的写入上下文临时放开。
- 读范围没有收窄，这一版只防“改写”，不防“读取”；多用户（owner 墙）场景的读收窄留待后续。
- 程序若写死 `/tmp` 而不看 `TMPDIR`，在沙箱里写临时文件会失败（Linux 为只读，macOS 为拒绝写）。
- Windows 没有实现（插件本身也只支持 POSIX 宿主）。

## 实现位置

- `agent_py_agent/agent/plugin_sandbox.py`：插件沙箱规格（cwd 为插件环境、唯一写根为数据目录）、包装与就绪检查。
- `agent_py_agent/agent/tooling/sandbox.py`：`SandboxSpec.read_only_root` 新形态；默认 `False` 时两种既有形态的参数逐字节不变。
- `agent_py_agent/agent/attempt/sandbox.py`：`AttemptSandboxSpec.read_only_root` 透传给 Linux 构造器（macOS 规则不变）。
- 开关沿 `PluginManagementContext.process_sandbox`（启用与显式调用）、`ToolRegistryParams.plugin_process_sandbox`（模型工具）、
  面板服务的客户端工厂传到 `PluginMCPClient(process_sandbox=...)`。

## 验证

- `test_plugin_sandbox.py`：开关默认值（YAML、dataclass、规范化）与配置传到管理上下文/模型工具注册/面板客户端、bwrap 整根只读参数布局、客户端包装与 `TMPDIR`、
  沙箱不可用时启用与显式调用的结构化拒绝；真实平台沙箱（不可用时跳过）下“数据目录可写、临时文件落在数据目录、
  工作区与插件环境写不进、工作区可读”，以及沙箱内 hello-node 的解释器与按次授权读取正常、停用后无残留进程。
- 本机 macOS（Seatbelt）与测试机 Linux（bubblewrap 0.8.0）各实跑一遍；原有 `test_sandbox.py`、`test_attempt_sandbox.py` 全部通过。

## 真实 TUI 验收（2026-09-25）

构建自本分支提交的 wheel，隔离 `MY_AGENT_HOME`、Gateway 只绑 `127.0.0.1:8431`，配置 `plugin_process_sandbox: true`。
三类插件都装上并启用：Python 包 workspace-peek（启用时的 venv 准备与候选进程都在沙箱里）、随包可执行文件 hello-go、
系统解释器 hello-node。显式调用与真实模型一轮（同时调用 Node 读文件、Go 打招呼）都正常，停用后没有残留进程。

- 本机 macOS：运行中的三个插件进程经系统 `sandbox_check` 查询均为“处在沙箱中”（对照：Gateway 进程为否）；
  `sandbox-exec` 直接 exec 目标程序，ps 里看到的就是插件本身（Node 为固定的 Homebrew node 路径）。
- 测试机 Linux：ps 可见每个插件都由 `bwrap --die-with-parent --unshare-pid … --ro-bind / / --dev /dev --proc /proc
  --bind <插件数据目录> …` 包着（Node 为固定的 `/opt/…/node` 真实路径）；三个插件停用后 bwrap 与插件进程全部退出。
- 写边界（数据目录可写、工作区与插件环境写不进）由两平台的真实沙箱组件测试覆盖，验收里没有再用写工作区的插件演示。
- 证据在仓库外的验收目录；两台机器的隔离 home、venv 与模型目录副本已删除。

## v8 事件插件强制沙箱并断网（B7，2026-10-03）

- v8 事件/收紧插件（清单 `permissions` 非空）不管 `plugin_process_sandbox` 开没开，都强制进本沙箱；本机沙箱不可用时启用失败（`sandbox_unavailable`），不退回无沙箱。
- 网络：`network:false` 时 Linux `--unshare-net`、macOS `(deny network*)`；macOS `network:true` 仍由 G4 实际绑定端口表拒绝 Gateway 端口。Linux 在 v8 插件沙箱接入 G5 Landlock `CONNECT_TCP` 端口拒绝前，以 `gateway_port_isolation_unavailable` 拒绝 `network:true`，接入并验证端口边界后才可放开。MCP 的宿主通信仍走标准输入输出。
- 收窄读：隐藏 Gateway 用户整个家目录；在其中只放行插件自己的环境、自己的数据目录及宿主核验的解释器前缀。数据根、会话、记忆、secrets、`.ssh` 等其它家目录内容均不可见。macOS 用 `private_read_roots` 拒读 + `_ancestor_metadata_rules`；Linux 整根只读形态先 tmpfs 覆盖隐藏根，再将授权根挂回。
- 共用受限策略入口：`plugin_restricted_sandbox_spec(*, cwd, data_dir, owner_home, policy: PluginRestrictedSandbox) -> AttemptSandboxSpec`；`plugin_sandbox_spec(..., sandbox_policy=...)` 将 v8 与老插件受限策略路由到同一构造器。`policy` 由宿主提供有限 `read_roots`、`write_roots`、`execute_roots`、`network` 和 `hidden_read_root`，候选预检、业务连接及面板连接只复用规格，不复制 Seatbelt/bwrap 规则。根路径必须是仍存在的普通目录或文件；目录根只开放该目录，文件型程序根只开放真实文件，不扩大到父目录。Linux 收窄读形态只对真实目标 bind，再用 `--symlink` 重建经核验的原始入口；Seatbelt 逐条允许 alias 与 realpath，写规则仍只落已授权写目标。读/执行根或写根若覆盖隐藏根则失败关闭。`execute_roots` 表示宿主核验的启动程序可见根，不替代更广义的子进程 `execve` 策略。
- 详见 DESIGN_LEDGER 的 B7 条目与 `PLUGIN_EVENT_HOOKS.md` 第 10、16 节。

## B7 老格式受限策略的读取底图（rdfloor，2026-10-05）

`PluginRestrictedSandbox.read_mode` 是结构化字段：`hide_home`（默认）保留 v8 既有“根只读、隐藏 HOME、重挂授权根”行为；`allowlist` 则只开放 `read_roots`、`write_roots`、`execute_roots` 及平台系统必要根。v8 显式使用 `hide_home`，Seatbelt profile 和 bwrap argv 通过固定快照确保逐字节不漂移。未知读模式在 Linux/macOS 分派前统一结构化拒绝 `SANDBOX_UNAVAILABLE: RESTRICTED_READ_MODE_INVALID`，绝不回退到更宽的 `hide_home`。

`allowlist` 不自动放开 `cwd`、`owner_home` 或解释器父目录；工作目录必须被明确读/写根或系统根覆盖，否则 fail-closed。执行根仍只暴露已核验文件及真实目标，原路径 alias 由公共 B7 symlink 处理重建。写权限只给明确写根与插件私有 data；不存在整根 bind 或从 cwd 推写权限的回退。

Seatbelt 规则先 `(deny file-read*)`，再允许每个系统/授权根；未开放根之间的祖先只可 `file-read-metadata`（供 `stat`/路径规范化），不能列目录或读取兄弟路径。隐藏凭据规则保持最后拒绝。

Linux 从空 `tmpfs /` 创建 `/dev`、`/proc`、`/tmp` 和挂载点骨架，系统根与授权读根只读 bind，显式写根 bind，最后 remount 根只读。R/W/E symlink alias 在真实根挂载之后恢复；alias 已包含在另一个授权父目录或系统根时不重复覆盖。系统目录按平台放在 `SYSTEM_READ_ROOTS_BY_PLATFORM`：Linux 为 `/bin`、`/sbin`、`/lib`、`/lib64`、`/usr/bin`、`/usr/sbin`、`/usr/lib`、`/usr/lib64`、`/usr/share`、`/etc/alternatives`；Darwin 为 `/System/Library`、`/usr/bin`、`/usr/lib`、`/usr/share`、`/bin`、`/sbin`。不把整个 `/usr` 或宿主 `/` 当系统根；解释器前缀仍须由宿主作为授权读根提供。解析后若落到 `/`、用户 home 或 `/private` 边界则拒绝；`/dev`、`/proc`、`/tmp` 是沙箱自身构造的伪文件系统/骨架，不作为宿主整目录挂载。

策略构造与平台规则测试命令、真实 Seatbelt/bwrap 的未验证边界见 `TESTS.md` 的 rdfloor 小节。组件测试不等同于真实进程或生产插件端到端验收。
