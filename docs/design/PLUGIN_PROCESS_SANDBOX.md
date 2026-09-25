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
