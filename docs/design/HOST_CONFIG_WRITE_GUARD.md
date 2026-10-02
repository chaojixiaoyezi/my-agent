# 宿主配置目录对模型工具只读（H3）

- **状态**：设计，待 3a 确认后实施（2026-10-02，be；分支 `claude/be-host-config-guard`，基于 `claude/3a-step17e` `4dd56f627`）。
- **起因**：语义记忆 M1 复审时发现，管理员的路径策略在 normal 和 full 两种模式下都允许模型的文件工具写 `~/.my-agent/config/`。模型直接改 `desktop.yaml`、模型目录，就绕过了参数中心的全部 `BOUNDARY_KEYS`、修改账本、撤销，以及 `manage_models` 的 M1 检查。
- **原则**：改配置只有两个权威入口——参数中心（`user_config`、`/settings`）和 `manage_models`（`/model`）。越权写宿主配置属于铁律里的“越权写入”，按硬门处理。读取不拦。

## 1. 保护什么（只在一处声明）

仿照 H2 的 `HOST_MANAGED_OWNER_STORE_PARTS`，在 `path_access_policy` 里新增两组路径片段（只做路径运算，插件 SDK 照样只依赖标准库）：

| 声明 | 相对谁 | 实际目录 | 里面有什么（生产实况） |
| --- | --- | --- | --- |
| `HOST_CONFIG_HOME_PARTS = (("config",), ("system", "config"))` | 数据根（`~/.my-agent`） | `config/`、`system/config/` | `desktop.yaml` 及其备份、`model-profiles/`、`shared-model-profiles.json`、修改账本 `settings-changes.jsonl`、`admin-password.json` 和它的节流文件、`tests/`；`system/config/` 目前为空 |
| `HOST_CONFIG_OWNER_PARTS = (("config",),)` | 每个 owner home | `owners/…/config/` | `capability_config.yaml`（参数中心写入的唯一用户位置） |

- 整个目录都保护，不只列文件：模型新建的文件（比如还不存在的 `capability_config.yaml`、`desktop.yaml.bak-*`）也写不进去。
- 修改账本 `settings-changes.jsonl` 也因此受保护，撤销依赖的账不会被模型改掉。
- 开发模式下，用户配置文件可能不在数据根里（`--config` 指到别处）。此时把**这一个文件**另外加进保护名单（策略构造时从 `user_config_path(config)` 冻结），不保护它所在的整个目录，避免误伤仓库目录。

## 2. 拦截点

### 2.1 文件工具：唯一写门

- `FileSystemTool.resolve_write_path` 已经是文件写工具的统一硬门。用到它的有：`write_file`（含 `source_ref` 物化）、`edit_file`、`apply_patch` 的新增、修改、删除，以及移动的源和目标。
- 新增 `PathAccessPolicy.check_write(path)`：先走原 `check(path)`（owner 墙、H2 托管存储等原有拒绝码不变），通过后再判宿主配置。命中就返回 `PATH_HOST_CONFIG_WRITE_BLOCKED`。
- `resolve_write_path` 改成走 `check_write`；读取类工具（`read_file`、`list_files`、`find_files`、`search_text`）仍走 `check`，**不受影响**。
- 判定只看 `resolve()` 后的真实路径：
  - 软链接指进配置目录同样会被拦；
  - 路径还不存在也能判（新建文件被拦）；
  - 不读文件内容。

### 2.2 命令类工具：沙箱只读覆盖

- `run_command`（前台和后台）、`terminal_session`（`pty_sessions`）都经 `shell._sandbox_exec` 进 `AttemptExecutionSandbox`，只有这一个入口。
- **更正一个前提**：管理员在 Full Access 下，`run_command` 也进沙箱。它走的是 `full_access` 档：不加全局写拒绝，但支持精确的只读覆盖。人格目录、`protected_write_paths` 现在就是这样做的：
  - macOS Seatbelt 写在 `(allow default)` 之后的 `deny file-write*`；
  - Linux bwrap 是 `--ro-bind`。
- 做法：在 `_sandbox_exec` 里把宿主配置目录并进只读覆盖。
  - owner 隔离时只加本 owner 的 `config/`，数据根的 `config/` 本来就在墙外。
  - Full Access 时加数据根的 `config/`、`system/config/`，再加全部已存在 owner 的 `config/`。枚举方式和 H2 的 `host_managed_store_dirs` 一样。
- macOS 上，owner 的 `config/` 还不存在时也照样写拒绝规则（Seatbelt 按路径匹配，不需要目录存在；探针第 2 项）。所以要把 `_readonly_path_denies` 的“不存在就跳过”改成只对 Linux 生效。
- **macOS 堵改名上级目录的口子**（探针第 4、5 项）：
  - 现在的只读覆盖只拒绝目标目录本身。命令可以先把它的上级目录改名，比如 `mv ~/.my-agent ~/.my-agent2`，写完再改回来，规则就落空了。
  - 现有的人格目录、`protected_write_paths` 覆盖也有同样的口子，这是已有问题。
  - 做法：`_readonly_path_denies` 给每个受保护路径的所有上级目录（一直到 `/`）都加一条 `(deny file-write* (literal 上级))`。这样只拦改名、删除、chmod、touch 上级目录本身，上级目录里的普通读写照常（探针第 5c 项）。
  - 这是沙箱的通用规则，会把人格目录的口子一起堵上。
  - Linux bwrap 的只读挂载跟着目录项走，改名上级目录不会让挂载失效。实施时在 Docker 车道实测确认。
- 命令回执的结构化事实 `sandbox` 里加 `read_only_host_paths`。命令因此报 `Operation not permitted` 时，模型能从结构化字段看出是宿主配置只读，而不是去猜。

### 2.3 拒绝时给什么

- 错误码 `PATH_HOST_CONFIG_WRITE_BLOCKED`，和 `PATH_HOST_MANAGED_STORE_BLOCKED` 同一族。工具回执沿用现有路径拒绝的映射（权限类），原码保留在结构化字段里。
- 提示文字示例：“宿主配置目录不对模型的文件工具开放写入。改设置请用 user_config（参数中心，带边界检查、修改记录和撤销），改模型和服务商请用 manage_models；读取不受影响。target=…”

## 3. 如实写明的已知边界

1. **Windows**：`run_command` 不进沙箱，命令类工具不受保护（文件工具照样受保护）。生产是 macOS，Docker 车道是 Linux。
2. **Linux 上还没有 `config/` 的 owner**：bwrap 只能把已存在的路径挂成只读。管理员 Full Access 的命令可以先替某个 owner 建出 `config/` 再写进去。
   - 缓解：`ensure_owner_home` 建 owner 时一并建出 `config/`（0700；现在不建）。之后凡是被加载过的 owner 都有这个目录。
   - 余下的缺口：从没被加载过的休眠 owner。只在 Linux 上存在。
3. **硬链接**：
   - 沙箱里的命令建不出指向受保护文件的硬链接：Seatbelt 会拒绝（探针第 3 项）；Linux 跨挂载点建链接会报 EXDEV。
   - 剩下的情况是沙箱外早就存在的硬链接，比如用户自己建的。命令顺着它写，Seatbelt 看不出来；这一条如实留作已知边界。
   - 文件工具自己建不了硬链接，但会顺着已有的硬链接写进去。加固做法：写之前，如果目标 `st_nlink > 1`，并且和受保护目录里某个文件是同一个 inode，就拒绝。
4. **插件进程**：`plugin_process_sandbox` 关闭时（默认关），插件进程以宿主权限运行，不受这道门约束。插件是管理员确认安装的代码，不属于“模型工具”。打开插件沙箱时只能写自己的数据目录，不受影响。
5. **宿主自己的写入不受影响**：参数中心、`manage_models`、TUI/IM `/model`、OAuth 登录、修改账本都在宿主进程里写，不经过模型工具的路径门和沙箱。

## 4. 会不会误伤现有功能

- **提示和技能**：`agent_py_agent/prompts/`、`agent_py_agent/skills/` 里没有任何一处让模型用文件工具写这些配置。默认提示已经要求改模型时调 `manage_models`。
- **行为变化**：用户说“帮我直接改一下 desktop.yaml”，以后会被拒绝，并指向 `user_config`。
  - 参数中心没有登记的键、注释、备份文件，模型都改不了了，要用户自己改或用 `/settings`。这正是想要的效果，写进 MODEL_GUIDE 和用户指南。
- **能力包**：`write_file.source_ref` 物化到工作区，不涉及配置目录。
- **测试**：文件工具、shell、沙箱的测试大多用临时目录，数据根来自 `MY_AGENT_HOME` 或 `~/.my-agent`。实施时跑全部文件工具、shell、沙箱和 H2 的相关测试，凡是拿临时数据根下 `config/` 当写目标的用例都会暴露出来，逐个改成走参数中心，或者改用别的路径。

## 5. 普通 owner（隔离模式）现在挡住了多少

- **数据根的 `config/`**：已经挡住。文件工具报 `PATH_OWNER_SCOPE_BLOCKED`；命令在 Linux 上根本没挂载，在 macOS 上数据根整体拒读。这一块不用重复做。
- **自己 owner home 的 `config/`**：**没有完全挡住**。
  - 文件工具平时只能写本任务工作区，但用户声明的工作目录可以落在自己家里任意位置（`inheritable_declared_work_roots` 只排除别人的家和控制面）。比如把执行目录设成 owner home 根，`config/capability_config.yaml` 就能写了。命令沙箱的额外写根也一样。
  - 2.1 的路径级写门和 2.2 的只读覆盖对隔离 owner 同样生效，一并盖住，不用另写一套。

## 6. 开关

建议**不加开关**。理由：
- 这是安全边界上的硬门，和 H2（插件库）一样没有开关；
- 如果开关能被参数中心以外的路径改动，就形同虚设；
- 要是 3a 认为需要，就做成只有管理员能经 `/settings` 改的边界键，默认开。

## 7. 测试和变异（实施时）

- 路径策略单测：
  - 两组声明各自命中：`desktop.yaml`、`model-profiles/x.json`、修改账本、`system/config/`、owner `config/capability_config.yaml`；
  - 还不存在的文件也命中；软链接指进来也命中；
  - 读取放行；
  - 原拒绝码（owner 墙、H2）优先级不变；
  - 开发模式下单个用户配置文件命中、它所在的目录不命中。
- 文件工具：`write_file`、`edit_file`、`apply_patch` 的增、改、删、移都拦，移动的源和目标各测一次；回执码是 `PATH_HOST_CONFIG_WRITE_BLOCKED`，提示指向 `user_config`、`manage_models`。
- 隔离 owner：声明了自家根为工作目录时，`config/` 仍被拦。
- 沙箱：用真实 `sandbox-exec` 和 bwrap（车道里）跑。Full Access 下 `echo > desktop.yaml` 被拒、`cat` 能读；隔离 owner 写自家 `config/` 被拒；macOS 上不存在的 `config/` 也被拒。
- 改名上级目录：真实 `sandbox-exec` 下，给上级目录改名会被拒，上级目录里的普通写入照常；人格目录的覆盖也跟着堵上。Linux 在车道里实测改名上级目录后挂载仍然只读。
- 硬链接：文件工具写入一个和受保护文件同 inode 的工作区路径时被拒。
- 变异：至少 10 个。去掉文件门判定、去掉沙箱覆盖、读也拦、软链接不解析、只拦已存在的文件、owner 部分漏掉、错误码改成原码、回执里的提示事实去掉、去掉上级目录 literal 拒绝、去掉同 inode 检查。
- 门禁：相关测试 + guards9、import boundaries、ruff、doc_sync、size_diff、严格 code-size、diff --check、clean_package、Docker Linux 车道（因为改了沙箱）。

## 8. 工时估计

约 5.5–7.5 agent 小时：路径策略、文件写门和同 inode 检查 1.5–2 小时；沙箱覆盖、macOS 不存在目录和上级目录规则、回执事实 2 小时；测试、变异、Linux 车道、文档 2–3 小时。设计探针已完成（`~/.my-agent/decision-evidence/host-config-guard-design/`）。

## 9. 顺带发现（交 3a 定，不在本项）

- **S1 的前提**：S1 修复假定“重启是用户的动作”。但 `restart_gateway` 是模型工具，效果标的是 dangerous；在“完全放行”审批模式下，模型可以不经审批直接调用。这样模型可以先 `set_default` 到另一家、调用 `restart_gateway` 刷新启动快照、再 `set_embedding`。只有在用户开了完全放行时才成立。建议写进 S1 的已知限制，或者让 `restart_gateway` 在任何模式下都要确认。
- **`admin-password.json`**：
  - 写：现在模型能写，可以换成自己知道的 scrypt 值，或者清掉 `admin-password-attempts.json` 的失败节流。本项把整个 `config/` 设成只读，**这两种写法一并堵住**。
  - 读：按“读不拦”的口径仍然可读。文件里只有 scrypt 派生值和参数，弱密码可以离线暴力破解。它不在凭据文件名名单里，而管理员 Full Access 本来也不套用那份名单。要不要单独加读拒绝，由 3a 定。
