# M 线第一期真实验收手册

状态：**待执行**。这是一份 B3–B9 合入后的操作手册，不是已运行或已通过的证据。执行人：3a 或 9b。适用设计以 [PLUGIN_EVENT_HOOKS 第 9 节（账本与展示）](PLUGIN_EVENT_HOOKS.md#9-账本与展示)、[第 13 节（拆块）](PLUGIN_EVENT_HOOKS.md#13-拆块给-my-agent-会话实现)、[第 14 节（验收）](PLUGIN_EVENT_HOOKS.md#14-验收goal-第四节) 和第 15 节裁定为准。

> **本手册已并入 17j 验收范围（rb2，2026-10-04）**：插件之外的安全开关项（G2b 本机凭据、锁与私有写权限、数据根收紧、屏幕观察只看档、飞书限流脚本凭据、后台收尾）见[附录 A](#附录-a插件之外的-17j-验收项)。附录 A 与正文同属一次验收，收尾一起记录。

## 1. 开始前的硬前提

### 1.1 集成版本和分支

不要在单块分支或旧版 Gateway 上做真实验收。**版本前置是 17j**：B3–B9 都在里面。先由集成者确认 17j 候选头已含下表所有合入项、依赖和必需复审；执行记录写入候选提交号，不靠分支名或模型自述推断。

**开始前必须成立的前提（缺一项就不要开始）**：

- **B5（收紧钩子）已合入并完成安全复审**——没有它就没有 `plugin_gate.decided` 账本行，本手册第 5–7 节的判据全部落空。
- **B5 同时提供配置项 `plugin_tool_gate_timeout_ms`**（默认 `2000`，一次工具调用等插件回答的总预算，含连接启动）。合入前 17j 里**没有这个键、也没有读它的代码**；开始前用 `/settings show plugin_tool_gate_timeout_ms` 核对它已是可识别参数（返回 2000，而不是"未知参数"），否则第 2 节的配置块与核对步骤都落空，停止。
- **B7（安全底座）已合入并完成安全复审**——本手册第 6.5 节的断网/收窄读探针依赖它。B7 未合入时该节只能记「未执行」。
- **B7 同时提供开关 `plugin_events_enabled`**（随包默认 `false`）并把它登记进 `AgentConfig` 与 `agent_config.yaml`。合入前 17j 只有 `getattr(config, 'plugin_events_enabled', False)` 的读取点，**`AgentConfig`、随包 YAML、参数中心登记表里都没有这个键**，`/settings` 列不出也设不了它；开始前用 `/settings show plugin_events_enabled` 核对（返回 `true`/`false`，而不是"未知参数"），否则第 2 节的配置块、第 7.1 节的开关流程都落空，停止。
- **老插件权限（`worker/plugin-legacy-permissions`）已合入**——它决定 v8 启用的 owner 限定与拒绝原因码 `plugin_events_owner_not_allowed`（该码由它提供；未合入时第 6.6 节记「未执行」）；第 6.6 节的非 local/main 负例依赖它。

以上任何一项未就位，**立即停止**，不要用"先跑能跑的部分"代替。

| 块 | 合入分支 | 必须确认的内容 |
| --- | --- | --- |
| B3 事件中心 | `worker/m1-b3-event-hub` | 事件分区、投递/合并计数、撤销；9b 复审结论已处理 |
| B4 事件点 | `worker/m1-b4-event-points` | 6 类事件都接到 Gateway 入口；本地直连 TUI 不代替 Gateway 路径 |
| B5 收紧钩子 | `worker/m1-b5-tool-gate` | ask/deny、用户拒绝、无法审批与 `call_origin` 语义；按设计要求完成安全复审 |
| B6 账本与展示 | `worker/m1-b6-ledger-display` | `plugin_gate.decided`、`/plugins info` 最近决定、观察计数和“无法审批”计数 |
| B7 安全底座 | `worker/m1-b7-fixes` | `plugin_events_enabled`、local/main 限定、强制沙箱、断网/收窄读；按设计要求完成安全复审 |
| B8 样例 | `worker/m1-b8-samples` | event-watch、Python/Node rm-guard 与本手册期待的声明/README 一致 |
| B9 写插件技能 | `worker/b9-facts-table`、`worker/sol2-m5` | 内置技能 `write-my-agent-plugin` 含事件字段表与 Python/Node 双语言模板；本手册 6.5 节的探针插件以它的 Python 模板为底（骨架见该节）。B9 功能本身不在本手册验收范围 |

底座还须含 step17i 已合入的 B1、B2、H3。联合冒烟安排在 **17j 合入 B3–B9 后、逐渠道完整验收前**。

**ds2 正在更新 B8 的删除门**：最终样例必须以 `apply_patch` 的删除文件段为输入并返回 `deny / DELETE_FILE_BLOCKED`；宿主没有 `delete_file` 工具。手册最初读取到的 B8 README/声明仍写 `delete_file`，属于过期样本，不能照旧构建。开始前核对 17j 中 `plugins/rm-guard/README.md`、`plugins/rm-guard/declaration.json` 和 Node 对应文件都已更新；任何一处仍写 `delete_file` 都是前置失败，等 ds2 更新合入后再执行，不临时改样例或绕过 gate。实际 `apply_patch` 参数投影以最终 B8 声明为准；声明不能让插件读取删除段时停止并交 ds2/3a 校正文档或样例。

### 1.2 测试身份、模型和沙箱

- 只使用新建的隔离 home、候选版本的 CLI/Gateway、一个专用飞书测试应用和私聊；不要复制真实 owner home、生产模型目录、Feishu 凭据或插件状态。
- 两个渠道都必须落在 M1 允许的 `local/main` 身份。飞书仅用已绑定的管理员私聊：在隔离 home 设置测试管理员密码，私聊发 `/admin <测试密码>` 后立即撤回含密码的消息。依据管理员身份设计，这个私聊随后才映射到 local/main；群聊和未绑定用户不适用。不要从 `/status` 文案猜 owner；通过该 Feishu 私聊的 `/plugins info` 与同一隔离 owner `runtime_events` 的插件行确认路由。若启用拒绝返回 `plugin_events_owner_not_allowed`，停止，不要放宽 owner 限定。
- `/model` 选择真实 **MiniMax-M2.7**。通过 `/status` 核对当前模型；另记录测试环境显示的实际 provider/model 标识。没有独立授权的 M2.7 测试档案/凭据时停止，不从生产配置复制密钥，也不把模型名称当作供应商来源证明。
- TUI 必须连本手册启动的 Gateway（`my-agent` 默认 Gateway/TUI 入口），不要用本地直连/`local_unmanaged` TUI 代替：设计明确指出本地直连 TUI 第一阶段只有两类工具事件，没有 `prompt_submitted`、回合和命令事件。
- 保持普通“默认确认”审批模式。测试者拒绝 rm-guard 的 ask；不要使用自主模式或长期授权。插件 v8 强制进插件沙箱；沙箱不可用时停止，不得关闭沙箱、换到无隔离运行或把该项算通过。

## 2. 建隔离 home、专用端口和 Gateway

在候选代码树根目录开一个专用终端，使用同一组路径启动 Gateway、TUI 和 Feishu CLI。下面端口示例固定为 `18420`，**绝不使用或连接生产端口 `8420`**。若 18420 已被占用，换一个未占用的私有高端口，并同步修改配置及验收记录；不要结束占用进程。

```bash
# 本段创建的所有临时文件都必须是私有的：先收紧 umask，后面新建的配置、哨兵、包都按 0600 落盘。
# 少了这一行，配置/哨兵/zip 的权限会跟着调用者 umask 走（常见 022 → 世界可读 0644）。
umask 077
M1_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/m1ar.XXXXXX")"
chmod 700 "$M1_ROOT"
M1_HOME="$M1_ROOT/home"
M1_CONFIG="$M1_ROOT/agent_config.yaml"
M1_TMUX="m1ar-$(date +%Y%m%d%H%M%S)"
# tmux 用私有套接字：套接字文件放隔离目录、权限 0700，全程 -S 指向它。
# 这样既不会碰到用户已有的 tmux 会话，也不会把验收会话暴露给同机其它用户。
M1_TMUX_SOCKET_DIR="$M1_ROOT/tmux"
M1_OWNER_HOME="$M1_HOME/owners/local/main"
M1_WORKSPACE="$M1_OWNER_HOME"  # 配置中的 workspace_root 为空时即 owner home
mkdir -m 700 -p "$M1_HOME" "$M1_ROOT/packages" "$M1_TMUX_SOCKET_DIR"
cat >"$M1_CONFIG" <<'YAML'
my_agent_home: ""
workspace_root: ""
gateway_port: 18420
gateway_bind_host: "127.0.0.1"
plugin_events_enabled: true
plugin_tool_gate_timeout_ms: 2000
YAML
for M1_CASE in joint tui feishu; do
  mkdir -m 700 -p "$M1_WORKSPACE/m1ar-probes/$M1_CASE/rm-guard-probe"
  printf 'm1ar-read-probe-%s\n' "$M1_CASE" > "$M1_WORKSPACE/m1ar-probes/$M1_CASE/read-probe.txt"
  printf 'keep-this-sentinel-%s\n' "$M1_CASE" > "$M1_WORKSPACE/m1ar-probes/$M1_CASE/rm-guard-probe/sentinel.txt"
  printf 'preserve-this-file-%s\n' "$M1_CASE" > "$M1_WORKSPACE/m1ar-probes/$M1_CASE/patch-delete-probe.txt"
done
export M1_ROOT M1_HOME M1_CONFIG M1_TMUX M1_TMUX_SOCKET_DIR M1_OWNER_HOME M1_WORKSPACE
export MY_AGENT_HOME="$M1_HOME"
export MY_AGENT_CONFIG="$M1_CONFIG"
```

这是专用、临时的稀疏配置：`my_agent_home` 留空才会采用 `MY_AGENT_HOME`；`MY_AGENT_CONFIG` 指向隔离配置。**这两个键不是 17j 现有功能**：`plugin_events_enabled`（随包默认 `false`）由 B7 提供，`plugin_tool_gate_timeout_ms`（默认 `2000`）由 B5 提供，两者合入前写在这里会被配置校验当作未知键或根本读不到，所以本块只有在 §1.1 两条前提都核对通过后才成立；任一条未通过就停止，不要先跑本节。`gateway_port` 和 `gateway_bind_host` 仅把测试服务绑在 loopback 私有端口。不要设置生产 home、不要写 `gateway_port: 8420`，也不要以 `plugin_process_sandbox` 开关代替 v8 强制沙箱。

**tmux 全程用私有套接字**：下面所有 tmux 命令都带 `-S "$M1_TMUX_SOCKET_DIR/default"`。`-S` 收的是**套接字文件路径**，所以套接字就落在已 0700 的隔离目录里；**不要用 `-L`**——`-L` 收的是套接字**名字**（不是路径），tmux 会把它拼到 `/tmp/tmux-<uid>/` 下面，传路径会直接报 `error creating /private/tmp/tmux-<uid>/…(No such file or directory)`，套接字也不会落在隔离目录里。这样本轮的验收会话与用户已有 tmux 完全隔离，且收尾删目录即可清干净。**不要**不带 `-S` 执行任何 tmux 命令，也不要 `tmux kill-server`。

1. 确认 `command -v my-agent` 指向 17j 候选 CLI，而非生产安装；确认端口空闲：`lsof -nP -iTCP:18420 -sTCP:LISTEN` 应无监听。若有监听，换端口，不要停止它。
   **前置检查**（缺哪个就先装/先换）：`command -v lsof`、`command -v tmux`、`command -v stat` 都必须有输出；macOS 上 `stat` 用 `-f`、Linux 上 GNU coreutils 用 `-c`（见第 3 节的权限核对），确认你的 `stat` 支持对应写法（`stat --version` 或直接拿 `$M1_CONFIG` 试一次）。任一命令不存在就换等价工具或停下报告，不要跳过核对。
2. 在已导出本手册环境变量的终端运行 `tmux -S "$M1_TMUX_SOCKET_DIR/default" new-session -s "$M1_TMUX"`，在该专用会话内运行 `my-agent gateway start`，再运行 `my-agent gateway status`。只在它显示 `status=running`、`http_port=18420`、`workspace` 位于隔离 home，且 PID 与 `lsof -nP -iTCP:18420 -sTCP:LISTEN` 显示的监听 PID 相同后继续；从临时配置确认 `gateway_bind_host: "127.0.0.1"`。显示 8420、PID 不匹配、配置来源不明或已有其它 Gateway 身份时立即停止，不结束任何未知进程。
3. 创建 Gateway TUI 窗口：`tmux -S "$M1_TMUX_SOCKET_DIR/default" new-window -t "$M1_TMUX" -n tui`，在该窗口用相同候选 CLI 和环境运行 `my-agent`。TUI 与 Feishu 的 `/status` 只核对当前模型、渠道和会话运行状态；Feishu 的 local/main 身份依据隔离管理员私聊绑定及后续插件账本/展示验证。Gateway 端口用 `my-agent gateway status` 的 `http_port`、配置里的 `gateway_bind_host` 和 `lsof` 监听 PID 三方核对。不要用会包含 timeline 等其它字段的整份 `my-agent status --json` 输出代替最小观察，也不要把 `/status` 的会话/模型文本当插件 gate 证据。
4. 配置专用飞书测试应用：在此隔离环境运行 `my-agent feishu connect --scan`，按正常授权流程连接测试应用；不能复用生产 Bot 或复制真实 token。使用 `my-agent admin-password set` 设置只用于该隔离 home 的测试密码，在飞书测试 Bot 的私聊中绑定 `/admin <测试密码>` 并撤回含密码的消息。绑定结果应让这个管理员私聊的插件命令和 gate 记录进入同一 local/main 主库；如果启用返回 `plugin_events_owner_not_allowed` 或查不到该隔离库记录，停止。
5. 以 `/settings show plugin_events_enabled` 核对当前值为 `true`（B7 提供），以 `/settings show plugin_tool_gate_timeout_ms` 核对 `2000`（B5 提供）。两条都必须是"已识别参数并返回该值"，出现"未知参数"就说明对应分支还没合入——按 §1.1 停止，不要改成别的方式来开总开关。模型不能通过 `user_config` 修改这两个管理员边界参数。

Gateway 端口只按 `my-agent gateway status`、隔离配置与对应监听 PID 核对；TUI/飞书里的 `/status` 用于确认当前请求身份、模型和运行状态。三者都不能代替 `runtime_events` 或 `/plugins info`。

## 3. 构建、安装并启用三个样例

从已合入 B8 的候选树根目录构建，包写到临时目录，不留在仓库。`declaration.json` 中的文件清单是构建输入；README 不进入安装包。

```bash
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/event-watch/declaration.json \
  --files-root plugins/event-watch \
  --output "$M1_ROOT/packages/event-watch.zip"
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/rm-guard/declaration.json \
  --files-root plugins/rm-guard \
  --output "$M1_ROOT/packages/rm-guard.zip"
python3 scripts/build_plugin_files_package.py \
  --declaration plugins/rm-guard-node/declaration.json \
  --files-root plugins/rm-guard-node \
  --output "$M1_ROOT/packages/rm-guard-node.zip"
```

**核对权限**（本段脚本已 `umask 077`，这里确认它真的生效；macOS 与 Linux 的 `stat` 参数不同，各给一条，按你的系统选一条跑）：

```bash
# macOS：
stat -f '%Sp %N' "$M1_CONFIG" "$M1_ROOT"/packages/*.zip \
  "$M1_WORKSPACE"/m1ar-probes/*/read-probe.txt "$M1_WORKSPACE"/m1ar-probes/*/patch-delete-probe.txt \
  "$M1_WORKSPACE"/m1ar-probes/*/rm-guard-probe/sentinel.txt "$M1_HOME" "$M1_ROOT/packages"
# Linux：
stat -c '%A %n' "$M1_CONFIG" "$M1_ROOT"/packages/*.zip \
  "$M1_WORKSPACE"/m1ar-probes/*/read-probe.txt "$M1_WORKSPACE"/m1ar-probes/*/patch-delete-probe.txt \
  "$M1_WORKSPACE"/m1ar-probes/*/rm-guard-probe/sentinel.txt "$M1_HOME" "$M1_ROOT/packages"
```

**通过条件**：配置文件、三个哨兵文件、三个 zip 都必须是 `-rw-------`；`$M1_HOME` 与 `$M1_ROOT/packages` 必须是 `drwx------`。任何一项出现 group/other 位（例如 `-rw-r--r--`）就是**不通过**，先查 `umask` 是否真的设成 `077`，修好再继续；不要把"根目录是 0700"当成"文件也私有"。

Node 变体要求 Gateway 的 `PATH` 能找到 `node`；先核对 `node --version`。没 Node 不把 Node 样例冒充已验，也不跳过联合冒烟。安装并启用在专用 local/main 会话内完成。**TUI/飞书 slash 命令不展开 shell 变量**：在原终端用 `cd "$M1_ROOT/packages" && pwd` 取得包目录的绝对路径，再把真实绝对路径逐字填进命令；下面的 `<M1_ROOT>` 是占位说明，不是要直接粘贴的字符串。

下面三行是**示例形状**，`<上一步显示的绝对目录>` 必须换成上一条 `pwd` 输出的真实绝对路径（TUI/飞书不展开 shell 变量，不能原样粘贴这个占位符）：

```text
/plugins install "<上一步显示的绝对目录>/event-watch.zip"
/plugins info event-watch
/plugins enable event-watch
```

`/plugins enable` 首次回执会给待确认预览和确认码。先核对插件 ID、六类事件均为 `content: none`、网络关闭及强制沙箱，再把回执最后一行的码原样填入：

```text
/plugins enable event-watch --confirm <刚收到的确认码>
```

Python 与 Node 的 rm-guard 同样分别执行 `/plugins install "<绝对 zip 路径>"`、`/plugins info <插件ID>`、`/plugins enable <插件ID>`；核对收紧范围后用各自回执给出的 `--confirm <确认码>` 完成启用。确认码不能猜、复用或写进证据。启用成功后再次查 `/plugins info <插件ID>`，确认版本、激活状态、预期 gate 与沙箱/网络状态。每个包的 preview 与激活状态分开记录；安装成功不等于启用成功。

> 后文联合冒烟会同时启用 Python 和 Node 两个 guard，故同一个收紧工具调用可能有两条按 plugin_id 区分的 `plugin_gate.decided` 行；不要把它误判成重复执行。

## 4. 只看结构化事实的取证方式

每次触发后立即检查以下三类证据，按 TUI 与飞书分别记录，不保存提示正文、模型回复正文、工具参数正文、原始会话、密钥或原始日志：

1. **`runtime_events`**：只查 `event_type='plugin_gate.decided'`。Owner 主库为 `$M1_HOME/owners/local/main/runtime.db`。下面只投影第 9 节定义的结构化键，不查询或打印完整 `payload_json`，尤其不查 `message`：

```bash
sqlite3 -readonly "$M1_HOME/owners/local/main/runtime.db" <<'SQL'
.headers on
.mode column
SELECT seq, event_id, event_type, attempt_id, agent_run_id, task_run_id, created_at,
       json_extract(payload_json,'$.plugin_id') AS plugin_id,
       json_extract(payload_json,'$.version') AS version,
       json_extract(payload_json,'$.activation_id') AS activation_id,
       json_extract(payload_json,'$.gate_id') AS gate_id,
       json_extract(payload_json,'$.tool') AS tool,
       json_extract(payload_json,'$.call_id') AS call_id,
       json_extract(payload_json,'$.operation_id') AS operation_id,
       json_extract(payload_json,'$.args_hash') AS args_hash,
       json_extract(payload_json,'$.actor') AS actor,
       json_extract(payload_json,'$.outcome') AS outcome,
       json_extract(payload_json,'$.verdict') AS verdict,
       json_extract(payload_json,'$.reason_code') AS reason_code,
       json_extract(payload_json,'$.latency_ms') AS latency_ms,
       json_extract(payload_json,'$.host_status') AS host_status,
       json_extract(payload_json,'$.final_status') AS final_status
FROM runtime_events
WHERE event_type='plugin_gate.decided'
  AND json_extract(payload_json,'$.plugin_id') IN ('rm-guard','rm-guard-node')
ORDER BY seq;
SQL
```

   将目标 `call_id` 与对应的结构化工具回执关联；用户拒绝 ask 时，回执错误码必须是 `APPROVAL_REJECTED`。`plugin_gate.decided` 本身不含审批消息正文。只允许在隔离库上执行只读查询；sqlite3 不可用或数据库路径不符时记阻塞，不改用会话正文/日志替代。
2. **`/plugins info <插件ID>`**：保存/抄录输出中的事件订阅、工具收紧、网络/沙箱、最近 10 条决定（时间、工具、outcome、final_status、reason_code）、观察计数（送达、合并、失败、不可用）及 `无法审批：N 次`。观察计数只在 Gateway 内存，不跨 Gateway 重启；每次重启前后分开记录，不用重启后的 0 覆盖先前结果。
3. **`/status` 与 Gateway status**：每个渠道测试前后，`/status` 只核对当前 MiniMax-M2.7、渠道/会话和运行状态；Feishu local/main 归属由隔离管理员私聊绑定以及对应插件命令/账本确认，不从 `/status` 文案推断。Gateway 的 `my-agent gateway status` 核对 `status`、`pid`、`http_port`、`workspace`。隔离配置中的 `gateway_bind_host` 必须是 `127.0.0.1`，并与私有端口 `lsof` 输出的监听 PID 相符。`/status` 与 Gateway status 只证明运行上下文，不证明 gate 结果；不要打印/通读包含 timeline 等无关字段的整份 `my-agent status --json`。

事件名及可见性：`prompt_submitted`、`turn_started`、`turn_ended`、`tool_call_started`、`tool_call_finished`、`command_executed`。观察默认只有结构化事实；event-watch 本身所有订阅均为 `content: none`。被拒绝或被 ask 后拒绝的工具调用**不**产生 `tool_call_started` / `tool_call_finished`，不要把缺少这两类计数判为故障。

### 4.1 判据：这次拦截是**宿主**拦的还是**插件**拦的

本手册多处要求"宿主放行或要确认、由插件收紧"。如果命令先被宿主自己的删除类硬拒，插件**一次都没被问到**，账本也不会有 `plugin_gate.decided` 行——那时把回执当成插件结果就是把宿主当插件，必须按不通过处理。

**宿主对删除类命令的前置规则**（`agent_py_agent/agent/contracts/gates/command_policy.py`，在工具执行前由 `agent/tooling/shell.py:1079` 与 `agent/tooling/action_policy.py:141` 两级读取）：

| 命令形状 | 宿主前置行为 | 依据 |
| --- | --- | --- |
| 裸 `rm` / `rmdir` / `unlink`（含 `rm -rf <workspace 内路径>`） | **直接拒绝**，回执 `COMMAND_POLICY_BLOCKED`，找不到 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 也找不到 gate 行；handler 未启动 | `command_policy.py:11` 把这三个可执行文件列入 `_MANAGED_DELETE_EXECUTABLES`；`command_policy.py:423-433` 对不在显式白名单里的它们一律产出 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED`；`shell.py:1079-1089` 命中即返回、不执行 |
| `rm` 删除受保护前缀（`/`、`/etc`、`~`、`$HOME/…` 等） | 更早更严的拒绝 `COMMAND_DANGEROUS_PATTERN_BLOCKED`（pattern `RM_PROTECTED_TARGET`） | `command_policy.py:368-375`、`_PROTECTED_DELETE_PREFIXES`（`command_policy.py:12-29`） |
| `sh -c "rm -rf <路径>"`、`bash -c "…"`、`env sh -c "…"`、`cd … && sh -c "…"` | **宿主放行**（`sh`/`bash` 段判为 unknown，不触发删除硬门；`allow_shell_operators=True` 下也不拦） | 实测：`analyze_command('sh -c "rm -rf …"')` 返回 `classification=unknown`、无 finding；`command_policy.py:513-514` 的 dangerous 仅含 `shutdown`/`reboot`/`halt`/`poweroff`/`telinit`/`mkfs*` 与 `_CATASTROPHIC_EXECUTABLES` |
| `find … -delete`、`python3 -c "import shutil; …"` | 宿主放行（`find` 判只读、`python3 -c` 判 mutating，都不到删除硬门） | 实测（`find`→`read_only`，`python3 -c`→`mutating`，均无 finding） |

> 触发陷阱：**模型常把 `rm -rf <目录>` 直接写进 `run_command`，那样永远走不到插件**。这不是插件没生效，而是宿主先按自己的安全边界拒了。要验插件，必须让命令形状落在"宿主放行、由插件按自己声明的门收紧"的格子里——本手册第 5、6.2、7.1 节统一改用 `sh -c "rm -rf <目标>"`。

**怎么区分是谁拦的**（每次触发都按这三条一起判）：

1. **看账本**：`runtime_events` 里有没有对应的 `plugin_gate.decided` 行（`event_type` + `plugin_id` 匹配）。有 → 是插件参与过；没有 → 插件**没被问到**，这次不是插件结果。
2. **看原因码来源**：插件侧行 `reason_code` 来自插件自己的声明（`RM_RF`、`DELETE_FILE_BLOCKED`、`NO_MATCH`、`ARGUMENTS_TRUNCATED` 等，见 `plugins/rm-guard/src/server.py` 的 `review_gate`）；宿主侧回执是宿主码（`COMMAND_POLICY_BLOCKED`、`COMMAND_DESTRUCTIVE_DELETE_BLOCKED`、`APPROVAL_REJECTED` 等）。
3. **看 handler 是否执行**：插件 `deny` 时工具回执 `error_code=PLUGIN_GATE_DENIED`、handler 未执行；插件 `ask` 被拒时回执 `error_code=APPROVAL_REJECTED`、handler 未执行；宿主硬拒时回执 `COMMAND_POLICY_BLOCKED`、handler 也未执行——**回执 `handler_executed=false` 本身不能区分是谁拦的**，必须靠前两条。

三者对不上（有 gate 行但回执是宿主码、或没有 gate 行却记成插件通过）即判不通过，停止并查证，不要用"反正都被拦住了"放过。

## 5. 联合冒烟（先于 TUI / 飞书完整验收）

准备步骤已在每个 case 目录建立独立哨兵：`$M1_WORKSPACE/m1ar-probes/joint/`、`.../tui/`、`.../feishu/`。shell 命令里的变量不会在 TUI/飞书消息中展开；模型提示使用下面列出的 workspace 相对路径。不要在仓库、真实 home 或用户目录建测试对象。

| 动作 | 可复现输入 | 通过所需结构化事实 |
| --- | --- | --- |
| 六类观察事件 | 通过 Gateway TUI 发：“请用 `read_file` 读取 `m1ar-probes/joint/read-probe.txt`，只回复该文件里的 M1 标记。”；再发送 `/status` | `/plugins info event-watch` 六类事件各有 `delivered >= 1`；`failed=0`、`unavailable=0`。因队列合并，`coalesced` 可非零，不要求精确等于请求数 |
| rm 命令 ask | 请模型对 `m1ar-probes/joint/rm-guard-probe/` 执行 `run_command: sh -c "rm -rf m1ar-probes/joint/rm-guard-probe"`（**必须是 `sh -c` 包起来的形状**：裸 `rm -rf …` 会被宿主以 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 前置拒绝，插件一次都问不到，见 §4.1）；在出现 `[插件 rm-guard 要求确认：RM_RF …]` 后明确拒绝 | rm-guard（两个 guard 都启用时含 Node）各有 `verdict=ask`、`reason_code=RM_RF`、`host_status=allow`、`final_status=ask` 的账本行；关联回执为 `APPROVAL_REJECTED`；`sentinel.txt` 仍存在、命令未执行。**宿主侧先决条件**：同一形状在只读预检里 `analyze_command` 返回 `classification=unknown`、无 finding（§4.1 已实测），即"宿主放行、由插件收紧" |
| 补丁删除 deny | 要求模型**只使用 `apply_patch`** 删除 `m1ar-probes/joint/patch-delete-probe.txt`，使用下面的真实多行补丁 | 对每个已启用 guard 的对应行 `tool=apply_patch`、`verdict=deny`、`reason_code=DELETE_FILE_BLOCKED`、`final_status=deny`；工具回执 `error_code=PLUGIN_GATE_DENIED`，handler 未执行，原文件仍存在 |
| **安全命令照常放行（allow_as_is）** | 要求模型对 `m1ar-probes/joint/` 执行一条**无害**的 `run_command`，例如 `ls -la m1ar-probes/joint`（不含任何删除动作） | 对每个已启用 guard 的对应行 `tool=run_command`、`verdict=allow_as_is`、`reason_code=NO_MATCH`、`host_status=allow`、`final_status=allow_as_is`；**不出现审批框**；工具回执正常（非 `PLUGIN_GATE_DENIED`、非 `APPROVAL_REJECTED`），handler 正常执行并输出 `ls` 结果。三种裁决里唯一"照常放行"的一支，缺它只验了两种 |
| **超长参数被截断** | 1）**无害但截断**：让模型用 `run_command` 执行一条**远超 4000 字**、可见片段不含 `rm -rf` 的命令。2）**已 deny 仍 deny**：让模型用 `apply_patch` 提交一个**超长但含 `*** Delete File: ` 删除段**的补丁 | 1）账本行 `verdict=ask`、`reason_code=ARGUMENTS_TRUNCATED`、`final_status=ask`。2）账本行仍是 `verdict=deny`、`reason_code=DELETE_FILE_BLOCKED`（**截断不放松已看到的拒绝**，原因码保持原样）。3）可见片段本来就 `ask` 时**保留原 `RM_RF`**、消息追加"参数还被截断了"，不得改成 `ARGUMENTS_TRUNCATED` |

联合冒烟实际交给 `apply_patch` 的测试补丁：

```text
*** Begin Patch
*** Delete File: m1ar-probes/joint/patch-delete-probe.txt
*** End Patch
```

若请求调用了其它工具、没有真实结构化 gate 行、reason code 错、handler 已执行，或哨兵被改/删，均判联合冒烟失败；停止后查证，不重试到误删。核对三插件 `/plugins info`；event-watch 观察 6 类，两个 guard 的最近决定与“无法审批”字段均可见。完成后执行 `/plugins disable rm-guard-node`，以 Python rm-guard 单独执行后续渠道矩阵，避免双 guard 令结果重复；记录 Node 联合冒烟的两条 gate 行后再停用。

**跑之前先做一次形状自检**：把准备发给模型的那条 `run_command` 字符串，先在只读预检里过一遍宿主的命令策略，确认宿主**放行**（否则模型照做也到不了插件）：

```bash
# 只读预检：不执行命令，只解析分类；任何一条报 COMMAND_DESTRUCTIVE_DELETE_BLOCKED 就换形状
PY=~/.my-agent/releases/claude-tools/ci-venv-312/bin/python
PYTHONPATH=<候选代码树> "$PY" -c 'import sys; from agent_py_agent.agent.contracts.gates.command_policy import evaluate_command_policy as e; \
c=sys.argv[1]; d=e(c, allow_shell_operators=True); print(d.allowed, d.finding_codes)' \
  'sh -c "rm -rf m1ar-probes/joint/rm-guard-probe"'
# 期望输出：True ()   —— 宿主放行，才会走到插件
```

**三条裁决的完整口径**（以 `plugins/rm-guard/src/server.py` 的 `review_gate` 为准，Node 版逐条对齐）：

- **`allow_as_is`**：门命中但可见参数没有任何危险特征（例子里的 `ls -la`）→ `NO_MATCH`，照常放行、无审批。
- **`ask`**：可见参数里有需要用户确认的特征（`rm -rf`）→ `RM_RF`；参数不足以判断 → `ARGUMENTS_UNAVAILABLE`；无害但被宿主截断 → `ARGUMENTS_TRUNCATED`。
- **`deny`**：可见参数里已看到必须拒绝的特征（补丁的 `*** Delete File: ` 段）→ `DELETE_FILE_BLOCKED`。
- **截断只加严、不放松**：`arguments_truncated` 严格为布尔 `true` 才生效（`1`、`"true"` 按未截断处理）。片段已 `deny` 保持 `deny` 与原原因码；片段本来就 `ask` 保留它自己的原因码、消息补一句"参数还被截断了"；只有片段本可放行才升到 `ask` + `ARGUMENTS_TRUNCATED`。

## 6. TUI 与飞书逐渠道完整验收

联合冒烟通过后，对下表 **TUI 与飞书私聊各独立完成一次**。使用不同的哨兵目录/文件名；TUI 用 `m1ar-probes/tui/`，飞书用 `m1ar-probes/feishu/`。每个用例开始先记录 `/status`、`/plugins info` 快照。两渠道按顺序运行，不同时并发，便于把计数增量归属到当前渠道。飞书的 local/main 身份由隔离管理员私聊绑定与成功读取/写入插件事实确认；`/status` 只确认模型、渠道和会话运行状态。TUI 必须走同一 Gateway。

### 6.1 event-watch 六类计数

1. 发普通、无敏感信息提示，并明确要求读一个合成文件：TUI 用 `read_file` 读取 `m1ar-probes/tui/read-probe.txt`；飞书私聊用 `m1ar-probes/feishu/read-probe.txt`。必须观察到对应 `read_file` 工具调用；不把模型口头说“读过”当证据。
2. 在同一渠道发 `/status`，触发 Gateway 控制命令 `command_executed`。
3. 用 `/plugins info event-watch` 读取计数。六类事件都须至少送达一次；`failed`/`unavailable` 必须为 0。记录本渠道前后计数差，不要求 exact total；`coalesced` 仅记录，不因合并非零而失败。`prompt_submitted`/`turn_started`/`turn_ended` 由 Gateway 普通提示回合提供，不能用本地直连 TUI 补齐。

### 6.2 rm-guard 要求确认并被拒绝（含首次征询冷启动耗时）

**命令形状必须先落在"宿主放行、由插件收紧"上**：请模型执行 `run_command: sh -c "rm -rf m1ar-probes/tui/rm-guard-probe"`（TUI）/ `sh -c "rm -rf m1ar-probes/feishu/rm-guard-probe"`（飞书）。**不要再写裸 `rm -rf <目录>`**——裸 `rm`/`rmdir`/`unlink` 会被宿主在 `ActionPolicy` 阶段以 `COMMAND_DESTRUCTIVE_DELETE_BLOCKED` 前置拒绝（`command_policy.py:11`、`:423-433`；`shell.py:1079-1089`），插件一次都问不到，账本不会有 `plugin_gate.decided` 行。判据见 §4.1。

仅当审批文案含 `[插件 rm-guard 要求确认：RM_RF …]` 才进入拒绝步骤；TUI 选择"拒绝"，飞书按审批卡片或当前提示用管理员审批拒绝命令。回执结构化 `error_code=APPROVAL_REJECTED`，`call_id` 对上 `runtime_events` 的 ask 行，`verdict=ask`、`reason_code=RM_RF`。对应目录和 `sentinel.txt` 必须仍在。若没有插件前缀、转成普通审批、没有调用 `run_command` 或看不到 ask 行，记失败/未命中，**不得批准或让命令继续执行**。

**同时记录首次征询的冷启动耗时**（ds10 b5b7x 建议）：这是本渠道**第一次**命中插件门的调用，计时要包含插件进程首次启动/握手——多插件共用池上的第一次往往明显大于后续。做法：

1. 记下这次调用对应的 `call_id`（从工具回执或账本行取）。
2. 只读查询该行的 `latency_ms`（本手册 §4 的账本投影已经带这一列，**它就是设计第 9 节的耗时字段**）：

   ```bash
   sqlite3 -readonly "$M1_HOME/owners/local/main/runtime.db" \
     "SELECT json_extract(payload_json,'\$.latency_ms') AS latency_ms, \
             json_extract(payload_json,'\$.outcome') AS outcome \
      FROM runtime_events \
      WHERE event_type='plugin_gate.decided' AND json_extract(payload_json,'\$.call_id')='<上面记下的 call_id>';"
   ```

3. 与预算 `plugin_tool_gate_timeout_ms`（默认 `2000` 毫秒，B5 提供；用 `/settings show plugin_tool_gate_timeout_ms` 读当前加载值）对比，并把两个数一起记进证据。

**通过条件**：`latency_ms` **小于** `plugin_tool_gate_timeout_ms`，且该行 `outcome=ok`。若 `latency_ms` 顶到或超过预算、或 `outcome` 是 `timeout`，记"首次征询超出预算"并停止后续用例查证——这时后续所有 ask 都可能被吞成"无法审批"，后面的判据都不成立。若账本行没有 `latency_ms`（值为空或 0 而调用明明发生了），记「未拿到耗时字段」并当作 B5 接线问题上报。

> 字段来源与不确定项：`latency_ms` 由 B5 的 `GateReview.latency_ms` 投影而来，落在 `plugin_gate.decided` 的 15 字段白名单里（`plugin_events/tool_gate.py:60-64`、`plugin_events/decision_ledger.py` 的 `PLUGIN_GATE_DECISION_FIELDS`，与 B6 的 `_PLUGIN_GATE_DECISION_FIELDS` 同口径）。**待核实**：该字段目前只覆盖共用池内一次协议请求的往返，**是否包含插件进程首次 `initialize` 握手**尚未在真实环境实测确认；如果实测发现耗时明显偏小（例如恒为个位数毫秒）而首次调用体感很慢，按"未覆盖冷启动、结论不成立"记录，不要据此判通过。

### 6.3 补丁删除被直接拒绝

先确认 TUI 文件 `m1ar-probes/tui/patch-delete-probe.txt` 和飞书文件 `m1ar-probes/feishu/patch-delete-probe.txt` 均存在。要求模型只使用 `apply_patch`；TUI 只提交下面这个补丁：

```text
*** Begin Patch
*** Delete File: m1ar-probes/tui/patch-delete-probe.txt
*** End Patch
```

飞书用自己的文件路径，提交：

```text
*** Begin Patch
*** Delete File: m1ar-probes/feishu/patch-delete-probe.txt
*** End Patch
```

预期不出现审批框，工具回执直接为 `PLUGIN_GATE_DENIED`，`handler_executed=false`；`runtime_events` 对应行 `verdict=deny`、`reason_code=DELETE_FILE_BLOCKED`、`tool=apply_patch`、`host_status=allow`、`final_status=deny`。重新只读检查原文件仍存在。若模型没有调用 apply_patch，或该样例最终合入的声明仍是旧 `delete_file`，不能算通过。

### 6.4 展示和状态读回

分别在 TUI、飞书执行 `/plugins info event-watch` 和 `/plugins info rm-guard`，核对两入口显示同一套只读结构信息；event-watch 的六类计数齐全，rm-guard 最近 10 条内能看到 ask 与 deny 的工具、`RM_RF` / `DELETE_FILE_BLOCKED` 原因码和最终状态；`无法审批：N 次` 必须存在且为非负整数（这两种交互场景下通常为 0，不能伪造为已测到 unavailable）。同时用 `/status` 复核模型、渠道和会话状态，用 `my-agent gateway status` / 隔离配置 / `lsof` 复核同一测试 Gateway 与私有端口。

### 6.5 B7 断网与收窄读（要用插件进程自己的探针）

**前提**：B7 已合入并完成安全复审。B7 未合入时本节记「未执行」，不得用别的步骤代偿。

**为什么不能用 `read_file` 证明**：`read_file` 是**宿主工具**，它读得到读不到反映的是宿主的路径策略，不是插件进程沙箱的边界。插件沙箱的读边界必须**由插件进程自己**去试——也就是让插件在它的 `review_gate`（或 `tools/call`）里真的执行一次探测，再把结果通过它自己的回执带出来。

**做法**：在隔离 home 里构建一个**一次性验收探针插件**（不是改 B8 样例），它的 `review_gate` 对一条固定 `gate_id` 执行下列探测并把结构化结果放进 `message`（回执仍只用宿主的 `verdict`/`reason_code` 语义，探测结果只作为数据）：

| 探测项 | 插件进程内的动作 | 通过所需结构化事实 |
| --- | --- | --- |
| 外网断连 | `network=false` 时对 `https://example.com`（或任一公网地址）发一次带短超时的连接尝试 | 连接**失败**（DNS 解析失败、连接被拒或超时均可）；回执里该探测记为 `blocked`。若成功 → 不通过 |
| 本机回环断连 | `network=false` 时对本机私有端口（例如测试 Gateway 的 `18420`）发一次短超时连接尝试 | 连接**失败**；记为 `blocked`。**绝不能真连 8420**；18420 是本次隔离 Gateway，探针只做连接尝试、不发业务请求 |
| 收窄读：宿主敏感区不可读 | 尝试读隔离 owner home 下的会话库、记忆文件（例如 `$M1_HOME/owners/local/main/runtime.db`、`.../memory/` 下的任一文件） | **读失败**（`PermissionError` / `FileNotFoundError` 均可）；记为 `blocked` |
| 收窄读：自己包与数据目录可读 | 读自己的包文件与自己的数据目录 | **读成功**；记为 `allowed`（这一项证明沙箱不是"什么都读不到"，否则上一条的失败没有意义） |
| 解释器可用 | 用声明的解释器执行一次最小动作（Python 打印、Node 打印） | 成功；记为 `allowed` |

通过条件：前三条全部 `blocked`、后两条全部 `allowed`，**五条一起看**。任何一条不符即记失败，并保留该插件的原始回执（脱敏后）。

**可直接复制的探针插件骨架**：以 B9 内置技能 `write-my-agent-plugin` 的 Python 模板（`agent_py_agent/skills/builtin/plugins/write-my-agent-plugin/templates/python/`）为底，只改 `review_gate` 让它在裁决前先跑一遍探测，把结果**只作为布尔量放进回执的 `message`**（回执的 `verdict`/`reason_code` 语义不变，探测结果不参与裁决）。放在隔离根下 `probe-net/sentinel.txt` 的哨兵文件由本手册第 2 节创建，探针只读它、绝不去碰真实 owner home。声明只给最小权限（`network:false`、不留写口）：

```python
# probe-net/src/server.py —— 一次性验收探针，只用于 6.5 节，验收后删除
import json, os, socket, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DECLARATION = json.loads((ROOT / "declaration.json").read_text(encoding="utf-8"))
LOOPBACK_PORT = 18420            # 本手册的隔离 Gateway 私有端口；绝不能填 8420
# 哨兵路径不打环境变量：插件进程在沙箱里，宿主不透传任意 env，顶层读 os.environ 会直接导入失败。
# 打包前把绝对路径写进包内的 probe_target.txt，探针从自己的包里读它。
TARGET = (ROOT / "probe_target.txt").read_text(encoding="utf-8").strip()
PROBE_ORDER = ("external_network", "loopback_port", "host_sensitive_read",
               "own_package_read", "interpreter")


def _err_name(exc: BaseException) -> str:
    # 只回错误类型名，不回读到的内容、不回路径正文
    return type(exc).__name__


def probe(name: str, action) -> dict:
    # 只把"被挡住的网络/权限"算 blocked；错误分类必须能区分"沙箱挡的"与"我们写错了"：
    #   - ConnectionRefusedError / timeout / socket.gaierror / PermissionError → blocked（沙箱或断网的正常表现）
    #   - FileNotFoundError → 单独抛出来（目标路径写错/文件没建，不是沙箱挡的；误判成 blocked 就是假阳性）
    #   - 其它异常一律照常抛出，插件回失败，不算通过。
    try:
        action()
        return {"probe": name, "result": "allowed", "error": None}
    except (ConnectionRefusedError, TimeoutError, socket.gaierror, PermissionError) as exc:
        return {"probe": name, "result": "blocked", "error": _err_name(exc)}


def _read_target() -> bytes:
    # 目标文件不存在/读不到 → 抛 RuntimeError（**故意不用 OSError 子类**）：
    # probe() 只把网络/权限类 OSError 记 blocked，RuntimeError 会照常抛出、插件回失败。
    # 这样"路径写错"再也不会被误读成"沙箱挡住了"（那是这次验收最怕的假阳性）。
    path = Path(TARGET)
    if not path.exists():
        raise RuntimeError(f"probe target missing: {path.name}")
    return path.read_bytes()


def _tcp(host: str, port: int) -> None:
    with socket.create_connection((host, port), timeout=2):
        pass


def run_probes() -> dict:
    results = [
        probe("external_network", lambda: _tcp("example.com", 443)),
        probe("loopback_port", lambda: _tcp("127.0.0.1", LOOPBACK_PORT)),
        probe("host_sensitive_read", _read_target),
        probe("own_package_read", lambda: (ROOT / "declaration.json").read_bytes()),
        probe("interpreter", lambda: sys.version_info),
    ]
    summary = {item["probe"]: item["result"] for item in results}
    expect = {"external_network": "blocked", "loopback_port": "blocked", "host_sensitive_read": "blocked",
              "own_package_read": "allowed", "interpreter": "allowed"}
    first_bad = next((name for name in PROBE_ORDER if summary[name] != expect[name]), "")
    return {"probe_summary": summary, "first_mismatch": first_bad,
            "probe_errors": {item["probe"]: item["error"] for item in results if item["error"]}}


def write_probe_result(payload: dict) -> None:
    # 详细结果写进插件自己的数据目录（B7 把它列为可写根）；宿主侧读这个文件看逐项明细。
    target_dir = Path(os.environ.get("MY_AGENT_PLUGIN_DATA_DIR") or (ROOT / "probe-result"))
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "probe-result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def review_gate(params: dict) -> dict:
    # 裁决部分照抄模板：门不匹配一律 deny，args 缺失宁严按 ask
    call = params.get("call") if isinstance(params.get("call"), dict) else {}
    if params.get("gate_id") != "probe-gate" or call.get("tool") != "run_command":
        return {"verdict": "deny", "reason_code": "OUT_OF_SCOPE"}
    findings = run_probes()
    write_probe_result(findings)
    # 结论走 reason_code（账本存它，且账本不存 message）：全符合回 PROBE_ALL_PASS，
    # 否则回 PROBE_FAIL_<第一个不符合的项名大写>。原因码正则 [A-Z0-9_]{1,40}，两者都合规。
    reason = "PROBE_ALL_PASS" if not findings["first_mismatch"] else f"PROBE_FAIL_{findings['first_mismatch'].upper()}"
    return {"verdict": "allow_as_is", "reason_code": reason,
            "message": "probe result written"}
```

配套 `probe-net/declaration.json` 只声明一条收紧门，事件可留空。**下面是完整可用的声明**（照 B8 `plugins/rm-guard/declaration.json` 与 B9 Python 模板的字段集补齐：`summary`/`default_action`/`actions`/`tools`/`settings_schema` 是清单校验的必需字段，缺任何一个打包都会报"插件包描述无效"）：

```json
{
  "plugin_id": "probe-net",
  "version": "0.1.0",
  "summary": "一次性验收探针：在插件进程内自查断网、收窄读与解释器可用，结果只回布尔量",
  "entry": {"kind": "interpreter", "interpreter": "python3", "command": "src/server.py", "args": []},
  "files": [{"path": "declaration.json", "executable": false}, {"path": "src/server.py", "executable": false},
            {"path": "probe_target.txt", "executable": false}],
  "platforms": ["any"],
  "events": [],
  "tool_gates": [{"id": "probe-gate", "tools": ["run_command"], "effects": [], "arguments": "full"}],
  "permissions": {"network": false},
  "default_action": "",
  "actions": [],
  "tools": [],
  "settings_schema": {"type": "object", "properties": {}, "additionalProperties": false}
}
```

**打包前先写哨兵路径**（插件在沙箱里读不到宿主环境变量，所以目标路径要进包）：

```bash
printf '%s\n' "$M1_WORKSPACE/m1ar-probes/joint/read-probe.txt" > "$M1_ROOT/probe-net/probe_target.txt"
chmod 600 "$M1_ROOT/probe-net/probe_target.txt"
```

**打包与安装启用**（与 B8 样例同一套命令；`probe-net/` 放在隔离根下，不放进仓库 `plugins/`）：

```bash
python3 scripts/build_plugin_files_package.py \
  --declaration "$M1_ROOT/probe-net/declaration.json" \
  --files-root "$M1_ROOT/probe-net" \
  --output "$M1_ROOT/packages/probe-net.zip"
```

**宿主侧对照（必须先做）**：启用探针**之前**，在宿主 shell（沙箱外、同一隔离环境）把下面三个动作各跑一遍，**三条都必须成功**：

```bash
# 1. 直连 example.com:443（探针用的是 socket 直连，不走环境代理；有代理的机器上直连成功才是有效对照）
python3 -c 'import socket; socket.create_connection(("example.com",443),timeout=5); print("external OK")'
# 2. 隔离 Gateway 在监听（用本手册的私有端口，绝不是 8420）
lsof -nP -iTCP:18420 -sTCP:LISTEN
# 3. 读得到哨兵文件
cat "$M1_WORKSPACE/m1ar-probes/joint/read-probe.txt"
```

- 三条都成功 → 沙箱里的 `blocked` 才算数（说明"能通/能读"是环境本来就能，是沙箱挡住了）。
- **第 1 条直连本来就不通**（例如必须走代理才能出网）→ 外网那一项记「**对照不成立、未执行**」，**不能把沙箱里的失败当成通过**；其余两项对照成功就照常判其余项。
- 对照结果（`external OK` / `lsof` 输出 / `cat` 输出）写进证据；**没做对照或对照不成立却判了通过，就是这次验收的不通过项。**

**怎么读结果**：

1. **结论看账本 `reason_code`**（`plugin_gate.decided` 有这一列，**账本里没有 `message`**）：全符合回 **`PROBE_ALL_PASS`**；否则回 **`PROBE_FAIL_<第一个不符合的项名大写>`**，例如 `PROBE_FAIL_LOOPBACK_PORT`、`PROBE_FAIL_EXTERNAL_NETWORK`。（原因码规则是 `[A-Z0-9_]{1,40}`，这两个形态都合规。）
2. **逐项明细看探针自己写的文件**：`<插件数据目录>/probe-result.json`，里面是 `probe_summary`（每项 `blocked`/`allowed`）、`first_mismatch` 与 `probe_errors`（只有错误类型名）。插件数据目录按产品路径就是 **`<owner home>/data/plugins/data/probe-net/`**（`plugin_data_dir(owner, plugin_id) = owner.plugins_dir / "data" / plugin_id`，B7 把它列为插件的可写根）。在隔离环境里就是：

   ```bash
   cat "$M1_HOME/owners/local/main/data/plugins/data/probe-net/probe-result.json"
   ```

   探针用环境变量 `MY_AGENT_PLUGIN_DATA_DIR` 定位数据目录：3a 已核对 B7 会注入它（`worker/m1-b7-on17j` 的 `plugin_runtime.py:162` `PLUGIN_DATA_DIR_ENV`，`:212`/`:231` 建好数据目录后传给子进程，`plugin_sandbox.py` 把它列为唯一可写根），宿主侧路径就是 `$M1_HOME/owners/local/main/data/plugins/data/probe-net/probe-result.json`。代码里落到包目录旁 `probe-result/` 的分支只是兜底；真跑时如果明细不在上面这个路径，说明注入没生效，记「未拿到逐项明细」并当作 B7 接线问题上报，不要凭 `reason_code` 反推明细。

**`message` 为什么不能用**：B5 会把回执 `message` 截到 80 字，而且 `plugin_gate.decided` 的字段白名单里**根本没有 `message`**（`runtime_db/repository.py` 的 `_PLUGIN_GATE_DECISION_FIELDS` 15 个字段），所以结论只能走 `reason_code`，明细只能走插件自己的文件。

**注意**：
- 探针插件同样要走正常的 `/plugins install` → `/plugins enable` 流程（含确认码），不能绕过宿主直接跑脚本——绕过就证明不了沙箱。
- 探针插件在验收结束后要 `/plugins disable` 并删除其包与数据目录；不要把探针插件留在隔离 home 里当作"已安装样例"。
- `network=true` 的情形本手册**只记录不判通过**：B7 的 `network:true` 在 macOS 拒绝登记的 Gateway 端口、Linux 端口隔离（G5）落地前直接拒绝，属已知边界，按实际结构化回执如实记录。

### 6.6 非 `local/main` 身份不得启用 v8 插件

**前提**：老插件权限（`worker/plugin-legacy-permissions`）已合入。

在**同一个隔离 Gateway** 上建第二个身份：不要新建真实用户账号，用产品自己的入口把请求身份换成非本机管理员（例如另一个 owner 的隔离 home，或按产品实际支持的方式切到非 `local/main` 归属的会话）。然后在该身份下尝试 `/plugins enable <任一 v8 插件>`。

**通过所需结构化事实**：
- 启用被**拒绝**，原因码是 **`plugin_events_owner_not_allowed`**（由老插件权限提供；不是 `PLUGIN_PERMISSION_DENIED`、不是 `plugin_events_disabled`）；
- 该次尝试**没有产生** `plugin_gate.decided` 行（新代没有起来）；
- 原 `local/main` 的安装/激活状态**没有被改动**。

**不通过**：允许启用、原因码不对、或起了新代。任一项不符即记失败，不要"放宽 owner 限定再试一遍"。

### 6.7 逐渠道与联合冒烟的对应关系

第 5 节是**一次联合冒烟**（三插件同时启用，验证接缝）；本节 6.1–6.4 是**两渠道各自的完整矩阵**；6.5 与 6.6 是**一次性的边界检查**，在任一渠道做完即可，不必两渠道各做一遍（它们验的是宿主边界，与渠道无关）。**6.8 例外：它要两渠道各做一遍**（插话是渠道侧行为）。

### 6.8 活动回合里插话（prompt_submitted 只记一次）

**要验什么**：往一个**正在跑的回合**里插话时，**不能再记一条 `prompt_submitted`**——插话是"往已有回合里送输入"，不是新提交一个提示。现由 `http_handlers` 的 `if created and receipt.state == 'queued'` 保证（插话落在 `active_pending`，不是 `queued`，所以不调 `prompt_queued`）；这条只能在真实渠道核对，隔离单测里造不出真实活动回合（st1 实测：echo 后端不走模型生成，只让请求停在 `processing` 时插话仍落回 `queued`）。

**TUI 一遍**：

1. 发一条会跑一段时间的普通提示（例如让模型读一个较大文件再总结）；**不要等它结束**。
2. 在同一个会话里紧接着发第二条普通提示（这就是插话）。
3. 记 `/plugins info event-watch` 快照，并查本渠道的 `runtime_events`。

**飞书一遍**：同上两步，在**同一个已绑定的管理员私聊**里做（用飞书自己的探针文件，不要与 TUI 混用）；同样记 `/plugins info` 与 `runtime_events`。

**通过所需的结构化事实**（两渠道各查一次）：

- `runtime_events` 里这两次请求的 `prompt_submitted` 行**只能有 1 条**，且它的 `facts.request_id` 等于**第一条**（原回合）的 `request_id`；
- **插话那条请求不产生** `prompt_submitted`；它的落点看 `/ask` 返回的 `disposition`，必须是 **`active_turn_input`**（不是 `queued`）；
- 原回合的 `turn_started` / `turn_ended` **各一次**。

**不通过 / 未命中**：

- `prompt_submitted` 出现 **2 条**；或插话那条带着自己的 `request_id` 出现在 `prompt_submitted` 里；
- **插话实际落成 `queued`**——那说明没插进活动回合，这次观测**无效，记「未命中」而不是通过**（不能因为"只看到 1 条 `prompt_submitted`"就判过：插话落回排队时原回合还没发第二条，1 条是必然的）；
- 只凭模型回复里"插话成功了"这类文字判断，不查 `runtime_events` 与 `disposition`。

## 7. 总开关关闭与插件停用

### 7.1 关闭 `plugin_events_enabled`（B7 提供该开关）

**前提**：`plugin_events_enabled` 由 B7 提供并已登记进 `AgentConfig`/`agent_config.yaml`；未合入时本节整节记「未执行」，不要在 17j 上试（那里 `/settings` 不认识这个键）。

**期望语义（与设计稿一致）**：按 [PLUGIN_EVENT_HOOKS 第 10 节安全底座](PLUGIN_EVENT_HOOKS.md)的总开关条目——"已启用的 v8 插件在开关关掉后，**事件不投、收紧不问**（相当于停用这两项能力），`/plugins info` 显示原因"。本节下面的期望（插件前缀不出现、无新 gate 行、观察计数不涨、`/plugins info` 显示 `plugin_events_disabled`）就是这条的逐项落地，不要自行加严或放松。

> **本节依赖 B5 × B7 的接线提交（待派）**：B7 提供开关与"事件不投"的判定，B5 提供收紧征询与账本。开关关掉后"收紧不问"这一条要在**征询被跳过**的地方生效（B5 的征询入口按开关短路，不产生 `plugin_gate.decided`）。这个接线提交尚未落地；**落地前**本节只能证明"事件不投"和 `/plugins info` 的原因码，**"收紧不问"（`sh -c "rm -rf …"` 下无新 gate 行）那一项记「未执行/待接线」，不要判通过**。接线落地后回来补跑该项。

插件仍安装/启用时，在管理员 TUI 或已绑定的 Feishu 私聊执行：

```text
/settings show plugin_events_enabled
/settings set plugin_events_enabled false
/settings show plugin_events_enabled
```

参数中心的配置在 Gateway 重启后生效。用本手册环境变量在**同一隔离 home/配置/端口**上只重启测试 Gateway（`my-agent gateway stop` 后 `my-agent gateway start`），然后用 `my-agent gateway status` 核对 `http_port` 仍为测试端口、`workspace` 仍在隔离 home，并用 `/settings show` 核对 `false`。不要连接 8420。重启后 event-watch 的内存观察计数从空开始，禁止拿重启前后累计值直接比较。

在 TUI、Feishu 各发一次安全普通提示、读取本渠道探针文件（TUI：`m1ar-probes/tui/read-probe.txt`；Feishu：`m1ar-probes/feishu/read-probe.txt`）与 `/status`；再各触发一次受控 rm-guard 请求（TUI：`run_command: sh -c "rm -rf m1ar-probes/tui/rm-guard-probe"`；Feishu：`run_command: sh -c "rm -rf m1ar-probes/feishu/rm-guard-probe"`——**必须用 `sh -c` 包起来的形状**：裸 `rm -rf …` 会被宿主前置拒（§4.1），那样即使插件照常征询也看不到区别，这个用例就失去意义）。插件前缀不应出现，`runtime_events` 不应新增这两个 guard 的 `plugin_gate.decided` 行，event-watch 不应产生任何观察计数。宿主自己的普通审批仍可能出现；若出现只作普通拒绝，不批准、不执行。`/plugins info` 应显示 `plugin_events_disabled` 原因/能力关闭（该原因码由 B7 提供；未合入时本项记「未执行」）。原因码、无新行和测试对象未改变三者必须一起核对，不能把宿主审批当作插件征询。**再次强调**：`sh -c "rm -rf …"` 在开关打开时**会**产生 ask 行（§6.2），所以"开关关闭后无新行"才有对照意义；用裸 `rm` 做的对照在两种状态下都没有 gate 行，等于没测。

随后恢复 `/settings set plugin_events_enabled true`、重启同一隔离 Gateway 并读回 `true`，以便完成插件停用检查。若切换后必须重启的语义与 17j 实际实现不符，记录实际结构化回执并停止修改生产配置。

### 7.2 停用 event-watch 后不再收到事件

在开关为 true 的同一 Gateway 上，先通过安全提示、工具和 `/status` 生成一组 event-watch 计数，并保存 `/plugins info event-watch` 快照。执行 `/plugins disable event-watch`；确认状态已停用，然后在 TUI、Feishu 各再做一次相同安全事件组。再查 `/plugins info event-watch`：B3 的统计在撤销订阅后保留在内存，停用期间各事件类型的 `delivered/coalesced/failed/unavailable` 计数不得增加。不得只凭面板按钮消失、模型自述或一条空日志判断；若 `/plugins info` 不返回可比较计数，记“停用后未观测到计数证据”，不得报通过。

## 8. 通过 / 不通过矩阵

| 检查项 | 通过 | 不通过 / 未命中 |
| --- | --- | --- |
| 版本及隔离 | 17j 含 B3–B9/B1/B2/H3；进程、配置、home、owner、端口都指向隔离测试实例，端口不是 8420 | 分支缺块、复审未完、B8 仍是 delete_file、配置/身份来源不明、碰到 8420、沙箱不可用 |
| 联合冒烟 | 同一真实 Gateway 下三插件安装启用；6 类事件都有 delivered；Python/Node 的 ask 与 patch-delete deny 均有结构化行及正确码 | 假 Gateway/假池；少事件/字段错；Node 未运行却报全过；工具走错或 handler 执行 |
| TUI | 走 Gateway；event-watch 6 类满足计数；`run_command: sh -c "rm -rf …"`（宿主放行形状，§4.1）ask 后 `APPROVAL_REJECTED` 且未删；apply_patch deny 为 `PLUGIN_GATE_DENIED` / `DELETE_FILE_BLOCKED` 且文件仍在 | 本地直连TUI代替、用户未拒绝、没有插件前缀/结构化行、目标已变、只看聊天正文；**用裸 `rm -rf` 触发而宿主先拒**（没有 gate 行，不是插件结果） |
| 飞书 | 专用 Bot 私聊已绑定为 local/main；相同事件、ask、deny 和 /plugins info 都有可复核事实 | 用生产Bot/群聊/非管理员 owner；无 `APPROVAL_REJECTED` / `PLUGIN_GATE_DENIED`；凭口头承诺判断；用裸 `rm -rf` 触发而宿主先拒 |
| 总开关/停用 | 开关 false 后两渠道事件无计数、插件 gate 无新行；恢复 true 后停用 event-watch，计数快照冻结 | 只读设置值未测生效；把宿主普通审批误当 plugin gate；停用后计数仍增加或没有可比较观察 |
| 展示/账本 | `/plugins info` 最近决定、原原因码和“无法审批”字段；SQL 仅筛 `plugin_gate.decided` allowlist；工具回执能以 call_id 对上 | 读完整对话/日志代替结构化证据；错插件/错 run；把 `暂无记录` 写成通过 |
| 三裁决齐全 | `allow_as_is`（`NO_MATCH`，无审批、handler 执行）、`ask`（`RM_RF`，拒绝后 `APPROVAL_REJECTED`）、`deny`（`DELETE_FILE_BLOCKED`，handler 未执行）各有结构化行 | 只验了 ask/deny 两支；allow 那支被跳过或没抓到 `NO_MATCH` 行 |
| **宿主删除硬拒对照**（§4.1 的负例，专门防止把宿主当插件） | 让模型对同一目标执行**裸** `run_command: rm -rf m1ar-probes/<渠道>/rm-guard-probe`：回执 `error_code=COMMAND_POLICY_BLOCKED`；`runtime_events` 里这两个 guard **没有**对应 `plugin_gate.decided` 行；目录与 `sentinel.txt` 仍在 | 把它当成"插件拦住了"记通过；或该形状竟然出现了 gate 行（说明 §4.1 的宿主规则前提变了，停下重新核代码再继续） |
| 截断只加严 | 无害但截断 → `ARGUMENTS_TRUNCATED`；已 deny 仍 `DELETE_FILE_BLOCKED`；已 ask 保留原 `RM_RF` 只补消息 | 把已 deny/ask 的结果改成 `ARGUMENTS_TRUNCATED`；用真值 `1`/`"true"` 当截断 |
| B7 断网/收窄读 | **先做宿主侧对照**（直连 example.com:443 / Gateway 端口在监听 / 读得到哨兵三条都成功），再在插件进程里自查五项：外网、回环、宿主敏感区全 `blocked`；自己的包+数据目录、解释器全 `allowed`。结论看账本 `reason_code`（`PROBE_ALL_PASS` / `PROBE_FAIL_<项名>`），逐项明细看插件数据目录的 `probe-result.json` | 用 `read_file` 等宿主工具代证；缺 `allowed` 两项（无法区分"沙箱正确"与"什么都读不到"）；探针绕过 `/plugins enable`；从账本读 `message`（账本不存这个字段）；**没做对照或对照不成立却判了通过**（外网直连本来就通不了时，那一项记「对照不成立、未执行」） |
| 非 local/main 负例 | 第二身份 `/plugins enable` 被拒且码为 `plugin_events_owner_not_allowed`；无新 `plugin_gate.decided` 行；原 local/main 状态未变 | 允许启用、码不对、或起了新代；放宽 owner 限定后重试 |
| 活动回合插话 | 两渠道各一次插话：`prompt_submitted` **只 1 条**且 `facts.request_id` 属原回合；插话的 `disposition` 是 `active_turn_input`；原回合 `turn_started`/`turn_ended` 各一次 | `prompt_submitted` 出现 2 条；插话带自己的 `request_id` 出现；**插话落成 `queued`** 时这次观测未命中、不算通过；只看模型回复文字 |

任何一步不满足均分项记失败、未命中或待查，不用其它步骤抵消；不删除/重写结构化记录。把通过矩阵和脱敏的结构化事实交 3a/9b，由集成者决定收口。

## 9. 收尾：只清理测试实例

1. 保持 `MY_AGENT_HOME`、`MY_AGENT_CONFIG` 指向临时隔离目录，运行 `my-agent gateway stop`；再查 `my-agent gateway status` 已停止、`lsof -nP -iTCP:18420 -sTCP:LISTEN` 无监听。若状态不清楚，先保留目录并报告，不能停其它进程。
2. 退出该测试 TUI，随后只关闭本轮创建且名字记录在案的 tmux 会话（**用同一个私有套接字**）：`tmux -S "$M1_TMUX_SOCKET_DIR/default" kill-session -t "$M1_TMUX"`。不要关用户已有 tmux 会话；**不要**执行 `tmux kill-server`（会连带杀掉用户的会话）。
3. 核对本轮所有 CLI 都显式使用候选 CLI、隔离 `MY_AGENT_HOME`/`MY_AGENT_CONFIG` 和私有端口；未复制或读取真实 owner 文件、未用生产 Bot/模型密钥、未向 8420 发送请求。候选包、临时配置、合成文件、测试应用授权和副本都归入 `$M1_ROOT`，证据只留脱敏字段。
4. 确认 `$M1_ROOT` 是本次 `mktemp` 创建的精确目录后才删除：`rm -rf -- "$M1_ROOT"`。**这一步同时清掉私有 tmux 套接字文件**（`$M1_TMUX_SOCKET_DIR` 在 `$M1_ROOT` 下面），所以顺序是先关会话、再删目录；删之前确认该套接字目录里没有其它会话残留（`tmux -S "$M1_TMUX_SOCKET_DIR/default" list-sessions` 应为空或报"无服务器"）。变量为空、路径前缀不对或来源不确定时停止，不要改成清理整个 `~/.my-agent`、源码树或其它 tmux/Gateway。检查专用端口已经释放、测试应用不再连到本机候选、工作树没有验收产生的意外文件。
5. 记录手册执行人、17j commit、测试日期、两个渠道各自是否通过、B8 包摘要、结构化行计数和失败项；不记录提示/回复正文、令牌、管理员密码、完整配置、会话正文或原始日志。
## 附录 A：插件之外的 17j 验收项

来源：3a/acc 写的 17j 真实验收清单（2026-10-04），由 rb2 并入本手册。**与正文同属一次验收**，收尾一起记录。下表各项的前提与正文一致：隔离 home、私有端口（**绝不用 8420**）、只 MiniMax-M2.7、只看结构化事实。

| # | 项 | 前提 / 开关 | TUI 与飞书 | 结构化判据 | 等待与用量 |
| --- | --- | --- | --- | --- | --- |
| A1 | **G2b：不带凭据就拒绝**（开关默认关） | `/settings set gateway_require_local_credential true`，重启隔离 Gateway 生效 | 两渠道都是带凭据的客户端，开关打开后应当**无感**（这是"不该坏"的回归） | ① 开关打开**前** `/status` 的 `uncredentialed_loopback_by_endpoint` 必须归零（这是前提，不是判据）；② 开关打开后先跑一轮正常流量，确认计数仍为零；③ 手工造一次**不带凭据**的回环请求，`extract_identity` 返回 `("anonymous", "external")`；④ 启动时凭据不可用（缺文件/权限错）应**拒绝启动**（fail-closed） | **要等**：开关关着时先跑流量等计数归零。token 很小（<2k） |
| A2 | **锁与私有写权限**（已合入） | 无开关，由正常任务自然触发 | 起一个会写私有数据/取锁的任务；**飞书无专属入口**，同任务可用私聊发起 | **文件权限位**：新建目录 `0700`、新建文件 `0600`；**已存在目录的权限不得被改动**；最近已存在的祖先是有效符号链接时跟随一次 | 无等待。<3k |
| A3 | **数据根收紧到 0700**（drt） | 无开关；挂在 owner 维护（默认每天一次，可手工触发） | **无用户入口**，只走维护流程 | 数据根本身与每个 owner home 的 group/other 位被摘掉、owner 位不动；审计记录落在数据根下的私有文件（0600）；同库再跑是空操作；符号链接根跳过并告警。**恢复入口**：`restore_data_root_permissions(home)` | 无等待（手工触发）。<1k |
| A4 | **屏幕观察"只看档"**（vho） | `computer_use_enabled=false` + `computer_use_observation_enabled=true`（用 `/settings` 设） | 让模型尝试用 `observe_window`；**飞书无用户入口** | 只看档下服务**只交出 `observe_window`**，不交出 `click_candidate` / `type_into_candidate`、不交出任何上游工具；适配器进程**不 import `pyautogui` / `computer_control_mcp`**；服务环境带 `MY_AGENT_COMPUTER_USE_OBSERVE_ONLY=1`；审批仍是 `always` 每次都问 | 无等待。<5k |
| A5 | **飞书限流脚本附凭据**（已合入） | 无开关；脚本在 `scripts/feishu_limit/`，要真实隔离 Gateway | **无用户入口**，命令行验收脚本 | 脚本带 `X-Gateway-Token` 后 POST `/ask` 返回 200 且拿到 `request_id`、GET `/result/<id>` 拿到结果；**G2b 开关打开后仍然如此**（这是补这条的目的） | d1/d3 是并发压测**要等**跑完。5–15k |
| A6 | **后台任务收尾**（rco） | 无开关 | 起一个子代理/后台任务让它自然终态；**飞书无专属入口** | 回合收口是否**统一推进 `agent_run`/`task_run`**：查 runtime.db 里这两表的终止状态随回合收口一起落定；不可续跑族收口为 `failed`。**不要看模型回复说"完成了"** | 要等任务自然终态（含后台唤醒），可能几分钟。10–30k |
| A7 | **能力包返工提示转 hint**（phh） | 无开关，要跑能力包流程 | **飞书无专属入口** | 返工提示里错误样例带 `hint` 键（上限 200 字符、清洗换行/控制符/双向控制符、为空则不写键）；**判定与计数只看 `code`**，hint 不参与 | 无特殊等待。10–20k |

#### A.7.1 A4 的前置：开关现在设不了，要等 obsset 合入

**当前 17j 上这一步做不了**：`computer_use_observation_enabled` 在参数登记表里属于安全边界（`agent/settings/parameter_registry.py:56-57` 的 `_BOUNDARY_NAMES`），它**不在**用户白名单 `USER_SETTINGS_BOUNDARY_KEYS`（`agent/settings/user_config_capability.py:184-187`）里。所以管理员用 `/settings set computer_use_observation_enabled true` 会被拒，回执 `PARAMETER_BOUNDARY`（`agent/settings/parameter_changes.py:172-176`：`not spec.writable and not user_allowed` → 抛 `PARAMETER_CHANGE_BOUNDARY`/`PARAMETER_BOUNDARY`）。

**前置**：等 **ds3 的 obsset**（把该键加入用户可写边界、并处理 Loader 归一化）合入 17j 之后再做 A4。合入前本节记「未执行（前置未就位）」，**不要**改用直接手改配置文件、`user_config` 工具或环境变量绕过——那验的不是产品入口，结论不可用。

**重启语义**：obsset 已并入 17j（1e1aadb80），改完要在同一隔离 Gateway 上发 `/restart` 才生效：配置在启动时读取，回执写 effect_when=restart_gateway。重启后用 `/settings show computer_use_observation_enabled` 读回 true，再做本项。

### A.8 建议执行顺序（最快暴露问题优先）
| 顺序 | 项 | 为什么排这里 |
| --- | --- | --- |
| 1 | **A1（G2b）** | 它同时卡着插件线（6.2/6.3 需要受信任回环）和 A5。一旦计数归不了零或带了凭据仍被拒，后面全部项都在错前提上跑。它是唯一"要等"的，早点起等 |
| 2 | **A2（锁与私有写）** | 已合入、无开关、判据是硬权限位，最便宜且能立刻证伪 |
| 3 | **正文第 5 节（联合冒烟）** | 一次跑同时覆盖 B4 六类事件 + B5 三裁决 + B7 沙箱 + B8 样例，单位成本暴露面最大 |
| 4 | **正文 6.1–6.4** | 依赖第 3 步的环境，接续做 |
| 5 | **正文 6.5 / 6.6**（B7 探针、非 local/main 负例） | 一次性边界检查，可插在任何空档 |
| 6 | **A3（数据根收紧）** | 无依赖、便宜 |
| 7 | **A4（只看档）** | 无依赖、便宜 |
| 8 | **A6（后台收尾）** | 要等自然终态，可与第 3 步并行等 |
| 9 | **A7（能力包 hint）** | 最贵、与 M1 线独立 |
| 10 | **A5（飞书限流脚本）** | 已合入且有守卫兜底，风险最低；放在 A1 打开后当回归跑 |

**总 token 粗估**：全部走完 60–120k（MiniMax-M2.7）。**只有 A1 与 A6 需要"等"**，建议并行。

### A.9 附录各项的不通过写法

- A1：计数不归零就打开开关、或打开后两渠道有任何一个客户端开始被当匿名 → 不通过，**先别往下做**，回来查原因。
- A2：新建文件不是 `0600`/新建目录不是 `0700`，或**已存在目录的权限被改了** → 不通过。
- A3：owner 位被改动、递归改了子目录、改了数据根之外的目录、或没留审计记录 → 不通过。
- A4：只看档下交出了点击工具、或适配器 import 了 `pyautogui` → 不通过。
- A5：G2b 打开后脚本拿不到结果 → 不通过。
- A6：模型说完成但 `agent_run`/`task_run` 仍是非终态 → 不通过（以表为准）。
- A7：返工提示里没有 `hint`，或 hint 影响了判定/计数 → 不通过。
