# 宿主托管文件对模型只读（H3）

- **状态**：已实现，待集成（2026-10-02，be；分支 `claude/be-host-config-guard`，基于 `claude/3a-step17f` `a9c2b691f`）。
- **起因**：语义记忆 M1 复审时发现，管理员的路径策略在 normal 和 full 两种模式下都允许模型的文件工具写 `~/.my-agent/config/`。模型直接改 `desktop.yaml`、模型目录，就绕过了参数中心的全部 `BOUNDARY_KEYS`、修改账本、撤销，以及 `manage_models` 的 M1 检查。
- **扩项**（3a 2026-10-02）：
  - 9b 发现隔离 Shell 能写自家根上的 `runtime.db`（宿主权威执行账），并进本项。
  - ae 能力包块 4 的核验记录（规范任务根的 `data/pack_verification/`）用同一机制。
  - 本项因此从“宿主配置只读”扩成“宿主托管文件对模型只读”。
- **原则**：
  - 改配置只有两个权威入口：参数中心（`user_config`、`/settings`）和 `manage_models`（`/model`）。运行状态和核验记录只由宿主写。
  - 越权写宿主文件属于铁律里的“越权写入”，按硬门处理，不加开关（3a 定，和 H2 一样）。
  - 读取不拦，凭据例外（见下）。

## 1. 保护什么（只在 `path_access_policy` 一处声明）

本模块被原样打进插件 SDK，所以全部写成路径片段，只用标准库。守卫用例核对这些片段和宿主布局函数一致。

| 类别 | 声明 | 位置 | 文件工具 | 命令（Shell） | 拒写码 |
| --- | --- | --- | --- | --- | --- |
| 宿主配置 | `HOST_CONFIG_HOME_PARTS`、`HOST_CONFIG_OWNER_PARTS` | 数据根 `config/`、`system/config/`；每个 owner home 的 `config/` | 拒写，读照常 | 拒写，读照常 | `PATH_HOST_CONFIG_WRITE_BLOCKED` |
| 宿主运行状态 | `HOST_STATE_OWNER_FILES`（连 `.lock`）、`HOST_STATE_OWNER_DIRS`、`HOST_STATE_OWNER_SQLITE`（连 `SQLITE_SIDECAR_SUFFIXES`） | owner home 里，按路径拒写、不管存不存在。文件：`permissions.json`、`quota.json`、`retention.json`、`memory_policy.json`、`skill_policy.json`、`tool_policy.json`、`audit_log.jsonl`、`memory/ops.jsonl`、`memory/candidates.jsonl`，每个旁边的 `.lock`；`runtime.db` 和 `-wal`/`-shm`/`-journal`。目录：`capability_requests/`、`temporary_grants/`、`compact/`、`logs/`、`audit/`、`workspace/runtime/`、`memory/curator/`、`memory_archive/`、`cache/`、`trash/`、`skills/`、家目录根的 `.agents/skills/` | 拒写，读照常 | 拒写，读照常 | `PATH_HOST_STATE_WRITE_BLOCKED` |
| 任务核验记录 | `TASK_ROOT_LAYOUT` + `HOST_STATE_TASK_PARTS` | 规范任务根（owner home 下 `runs/<日期>/<键>`、`tasks/<日期>/<名>`、`audits/<编号>`）的 `data/pack_verification/` | 拒写，读照常 | 只保护本次命令所在的任务（写边界里的 `task_root`，加上工作目录与写根认出的任务） | `PATH_HOST_STATE_WRITE_BLOCKED` |
| 宿主凭据 | `HOST_CREDENTIAL_HOME_PARTS`、`HOST_CONFIG_YAML_SEGMENTS`、`HOST_SECRET_DIR_NAME` | 数据根 `config/` 下的 `admin-password.json`、`shared-model-profiles.json`、`model-profiles/`、名字里有 yaml/yml 段的文件（用户配置及 `desktop.yaml.bak-*` 等备份）；数据根里 owner home 之外任何一层叫 `secrets` 的目录 | 读写都拒 | 拒写，**读照常**（已知边界） | `PATH_HOST_CREDENTIAL_BLOCKED` |

- 用户配置的文件名由部署决定（Gateway `--config`），所以按“数据根 `config/` 下的 YAML 文件”认，不写死 `desktop.yaml`。
- 凭据只对文件工具连读也拒，这是 3a 定的。命令不拒读，因为模型在 Full Access 下会经 `run_command` 跑 my-agent CLI，CLI 要读 `desktop.yaml` 和模型目录。
- 被拒时，提示指向 `user_config` / `manage_models`，它们给的是脱敏视图。

### 1.1 A 类与 B 类：每条路径只在一处（3a 定）

- **A 类：宿主托管文件，绝对只读**。上表各项都是 A 类，唯一声明在 `path_access_policy`。
  - 生效范围：任何模式（含管理员 Full Access）、所有 owner。
  - 不可被任何允许根穿透。
  - 文件工具、写边界、插件上下文、Shell 只读覆盖、上级目录改名规则都按它判。
  - 原 `tool_runtime_ledger._attach_owner_control_write_guards` 清单里的绝对项已搬到这里，从那里删掉：权限、配额、保留、记忆、skill、工具策略文件，审计流水，能力申请，临时授权，Compact，日志。
- **B 类：owner 写范围里默认禁写、可被本任务工作目录穿透的任务树根**。
  - 范围：`runs/`、`agents/`、`data/`、`tasks/`。
  - 留在 `_attach_owner_control_write_guards`，走写范围的 `forbidden_write_roots`，按“命中的最具体条目生效”裁决，只在隔离模式挂。
  - 举例：本任务的 `work/` 是更具体的允许根，照常可写；别的任务仍禁写。
  - `tasks/`（9b 第 3 项）放 B 类：旧版任务工作区 `tasks/<日期>/<名>` 恢复时，模型要在它的 `work/` 里写。
  - `data/` 不搬到 A 类：可能有允许根落在 owner `data/` 下，H2 已经整体隐藏 `data/plugins`。
- **不保护（3a 口径）**，这些属于模型：
  - `artifacts/`：经上下文包的 `owner_artifacts_root` 交给模型，是交付区；
  - `workspace/` 里除 `runtime/` 以外的内容；
  - `tmp/`；
  - 记忆正文（`memory.md`、`memory/daily|lessons|routing|long_term`），由记忆工具写；
  - 用户放在家目录根的文件；
  - 真实项目工作区里的 `.agent(s)/skills`：不在 owner home 下，用户可以让模型写项目 skill。
- **宿主自己的写入不受影响**：记忆工具（`remember`）、Curator、策略服务、runtime 仓库、skill 管理都在宿主进程里写，不走模型的文件工具和 Shell。用例钉住：保护打开后 `remember` 照常写候选，模型 `write_file` 写同一文件被拒。

## 2. 拦截点

### 2.1 文件工具、写边界、插件

- `PathAccessPolicy.check(path)` 是读写共用的裁决，顺序如下：
  1. 先拒 H2 插件库；
  2. 再走模式裁决：owner 墙、full、normal，原有拒绝码都不变；
  3. 模式放行后再拒宿主凭据。
- `PathAccessPolicy.check_write(path)` = `check` + `host_write_decision`，后者拒宿主配置和宿主运行状态。用到它的有两处：
  - 核心写边界 `tooling/write_boundary.validate_write_boundary`；
  - 插件写入上下文 `workspace_write_context.WorkspaceWriteContext.check`。
  - 两边裁决保持一致。
- 文件工具的唯一写门 `FileSystemTool.resolve_write_path`：
  - 先过墙外授权根复核（`resolve_path`），再调 `host_write_decision`；
  - 命中就抛 `WriteScopeError(message, access_code)`；
  - `write_file`、`edit_file`、`apply_patch` 的增、改、删，以及移动的源和目标，都按这个码上报，不再是笼统的 `WRITE_FORBIDDEN`。
- 判定看的是 `resolve()` 后的真实路径：
  - 软链接指进来、目标还不存在都照样判。
  - **大小写无关**：macOS 和 Windows 默认文件系统不区分大小写，而 `Path.resolve` 保留输入的大小写。本机实测 `CONFIG/Desktop.YAML` 打开的就是 `config/desktop.yaml`。
    - 所以宿主声明位置一律按 casefold 比较。
    - H2 插件库原来是大小写敏感的字符串比较，有同样的口子，一并修好。
    - Linux 大小写敏感时，这样做只会多拦数据根里仅大小写不同的同名目录，不会少拦。
- 硬链接加固：目标是有多个链接的普通文件时，才逐个比对宿主配置和各 owner 运行状态文件的 inode。平时不扫描；任务里的托管位置不枚举。

### 2.2 命令类工具（沙箱只读覆盖）

- 入口：`run_command` 的前台和后台、`terminal_session`，都经 `shell._sandbox_exec` 进 `AttemptExecutionSandbox`。
- **管理员 Full Access 下 `run_command` 也进沙箱**，走 `full_access` 档。这一档不加全局写拒绝，但支持精确的只读覆盖：macOS 用 Seatbelt 的 `deny file-write*`，Linux 用 bwrap 的 `--ro-bind`。
- `shell.host_readonly_paths_for(owner, persona, task_anchors)` 并进 `protected_write_paths`，覆盖范围按模式分：
  - **隔离 owner**：只盖本 owner 的 `config/`、运行状态文件。数据根的配置在 owner 墙外。
  - **Full Access**：盖数据根的 `config/`、`system/config/`，加全部 owner 的。
  - 两种模式都加上本任务的 `data/pack_verification/`，来源有两个：
    - 写边界里宿主写的结构化 `task_root`：`registry_invoke` 经 `task_host_state_paths` 放进 `__sandbox_protected_write_paths`。命令在任务树外（用户项目目录）跑、写根也不含本任务时照样只读。这是 ae 块 4 发现的缺口，2026-10-02 修好。
    - task_anchors：工作目录与写根所在的任务。子代理的工作目录 `<任务根>/work/agents/<run>` 也能认出任务根。
- Seatbelt 侧的两处修改：
  - `_readonly_path_denies` 不再跳过还不存在的路径，写 literal+subpath。Seatbelt 按路径匹配，不要求目标存在，所以命令也建不出这些路径。
  - **沙箱通用规则 `_ancestor_write_denies`**（探针发现的已有口子）：
    - 问题：只读覆盖只认当前路径，命令可以先把上级目录改名（`mv ~/.my-agent ~/.my-agent2`），写完再改回来。人格文件、H2 隐藏路径、`runtime.db` 都会落空。
    - 做法：给每个受保护路径和人格根的全部上级目录（直到 `/`），各加一条 literal 写拒绝。这只拦改名、删除、chmod、touch 上级目录本身；上级目录里的普通读写照常。本机实测 `mkdir -p`、`os.makedirs`、git、cp、tar 均正常。
    - 顺序：放在私有读规则之后、隐藏路径之前，断网规则仍在最后。
  - Seatbelt 按磁盘上的真实路径匹配，大小写变体绕不过（探针实测）。
- Linux 侧：bwrap 的只读挂载跟着目录项走，改名上级目录不会让挂载失效（Docker 车道实测）。
- 回执：隔离 Shell 失败时的边界事实（模型可见）里，落在可写根里的宿主托管文件列进 `read_only`；不在可写根里的本来就只读，不多列，原有回执不变。

### 2.3 拒绝时给什么

| 码 | 恢复动作 | 提示 |
| --- | --- | --- |
| `PATH_HOST_CONFIG_WRITE_BLOCKED` | 换策略 | 改设置用 `user_config`（参数中心，带边界检查、修改记录和撤销），改模型和服务商用 `manage_models`；读取不受影响 |
| `PATH_HOST_STATE_WRITE_BLOCKED` | 换策略 | 运行状态与核验记录只由宿主写；任务、运行和核验状态经对应的宿主工具改变；读取不受影响 |
| `PATH_HOST_CREDENTIAL_BLOCKED` | 换策略 | 看设置用 `user_config`，看模型和服务商用 `manage_models`（脱敏视图） |

## 3. 已知边界（如实写明）

1. **Windows**：`run_command` 不进沙箱，命令类工具不受保护；文件工具照样受保护。生产是 macOS，Docker 车道是 Linux。
2. **命令可以读凭据**：3a 定的，my-agent CLI 要读配置。文件工具读不到。
3. **Linux 上还不存在的路径**：bwrap 只能把已存在的路径挂成只读，有两种情况：
   - 还没加载过的休眠 owner 没有 `config/`。缓解：`ensure_owner_home` 建 owner 时一并建出 `config/`。
   - `runtime.db-wal`、`-shm`、`-journal` 只在宿主开着库时存在。在 Full Access，或声明了自家根为写根的隔离 owner 下，命令可以抢先建出它们。这一条留作已知边界：在 bwrap 里占位会在真实 owner home 里建文件，owner home 只读挂载时还会让沙箱起不来。
   - macOS 没有这个问题。
4. **别的任务的核验记录**：命令只保护本次命令所在任务的 `data/pack_verification/`。Full Access 下，别的任务目录在两个平台上命令都挡不住（ae 接受）。文件工具按路径判，所有任务都拒写。
5. **沙箱外早已存在的硬链接**：命令顺着它写挡不住，Seatbelt 只拦新建；文件工具有 inode 比对。
6. **插件进程**：`plugin_process_sandbox` 关闭时（默认关），插件进程以宿主权限运行，不受这道门约束。插件是管理员确认安装的代码。插件经 SDK 判路径时照样用 `check` / `check_write`。
7. **用户配置放在数据根以外**（开发时 `--config` 指到仓库里）：不受本门保护，路径策略多处构造拿不到配置路径。
8. **管理员在命令里跑会写宿主状态的 my-agent CLI**：Full Access 下模型经 `run_command` 跑的子命令也在沙箱里，写日志、缓存、runtime.db 等会被拒，这是本项的目的（和隔离模式原来就一样）。读配置、读状态的命令不受影响。
9. **宿主自己的写入不受影响**：参数中心、`manage_models`、`/model`、OAuth、修改账本、runtime 仓库、能力包核验都在宿主进程里写，不经过模型工具的路径门和沙箱。

## 4. 会不会误伤现有功能

- **提示与技能**：`prompts/`、`skills/` 里没有任何一处让模型用文件工具写这些文件。默认提示本来就要求改模型时调 `manage_models`。
- **行为变化**：
  - 用户说“帮我直接改一下 desktop.yaml”，以后会被拒，并指向 `user_config`。参数中心没登记的键、注释、备份文件，模型都改不了了，要用户自己改或用 `/settings`。
  - 模型读 `desktop.yaml`、模型目录会被拒，改用脱敏视图。
- **隔离 owner 的边界事实**：只有落在可写根里的宿主文件才列进 `read_only`，原有回执不变（`test_shell_sandbox_boundary_facts` 照旧通过）。
- **原 B 清单里的绝对项改由 A 类拦**：隔离模式下写这些文件的拒绝码从笼统的 `WRITE_FORBIDDEN` 变成 `PATH_HOST_STATE_WRITE_BLOCKED`。管理员 Full Access 下这些文件原来能写，现在只读。

## 5. 顺带决定（3a）

- `restart_gateway` 改成任何审批模式都要用户确认（`ApprovalPolicy("always")`，完全放行也弹）。理由有两个：
  - 重启影响所有会话；
  - 顺带堵上 S1 的“改默认 → 重启刷新快照 → set_embedding”。
  - IM 走现有审批卡。
- `admin-password.json`：写这一侧由本项堵住（原来模型能换成自己知道的 scrypt 值，或清掉失败节流），读这一侧按凭据拒读。

## 6. 证据

- 设计探针：`~/.my-agent/decision-evidence/host-config-guard-design/`，内容：
  - Seatbelt 只读覆盖与改名上级目录绕过；
  - 上级目录规则对常用命令的兼容性；
  - 大小写变体。
- 测试见 TESTS.md 同名条目。
