# 宿主托管文件对模型只读（H3）

- **状态**：已实现，二审必须改已修完、待 9b 复核（2026-10-02，be；分支 `claude/be-host-config-guard`，基于 `claude/3a-step17g` `8a832d4e1`）。二审裁定见第 7 节。
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
| 宿主运行状态 | `HOST_STATE_OWNER_FILES`（连 `.lock`）、`HOST_STATE_OWNER_DIRS`、`HOST_STATE_OWNER_SQLITE`（连 `SQLITE_SIDECAR_SUFFIXES`） | owner home 里，按路径拒写、不管存不存在。文件：`permissions.json`、`quota.json`、`retention.json`、`memory_policy.json`、`skill_policy.json`、`tool_policy.json`、`audit_log.jsonl`、`memory/ops.jsonl`、`memory/candidates.jsonl`，每个旁边的 `.lock`；`runtime.db` 和 `-wal`/`-shm`/`-journal`。目录：`capability_requests/`、`temporary_grants/`、`compact/`、`logs/`、`audit/`、`workspace/runtime/`、`memory/curator/`、`memory_archive/`、`cache/`、`trash/`、`skills/`、家目录根的 `.agents/skills/`、`agents/`、owner 根的整个 `data/`（新子目录默认也在内） | 拒写，读照常 | 拒写，读照常 | `PATH_HOST_STATE_WRITE_BLOCKED` |
| 任务核验记录 | `TASK_ROOT_LAYOUT` + `HOST_STATE_TASK_PARTS` | 规范任务根（owner home 下 `runs/<日期>/<键>`、`tasks/<日期>/<名>`、`audits/<编号>`）的 `data/pack_verification/` | 拒写，读照常 | 本任务（写边界里的结构化 `task_root`）两个平台都保护；macOS 另用一条 Seatbelt 正则盖住 owner 下全部任务的，Linux 只保护本任务（已知边界） | `PATH_HOST_STATE_WRITE_BLOCKED` |
| 宿主凭据 | `HOST_CREDENTIAL_HOME_PARTS`、`HOST_CONFIG_YAML_SEGMENTS`、`HOST_SECRET_DIR_NAME` | 数据根 `config/` 下的 `admin-password.json`、`shared-model-profiles.json`、`model-profiles/`、名字里有 yaml/yml 段的文件（用户配置及 `desktop.yaml.bak-*` 等备份）；数据根里 owner home 之外任何一层叫 `secrets` 的目录 | 读写都拒 | 拒写，**读照常**（已知边界） | `PATH_HOST_CREDENTIAL_BLOCKED` |

- 用户配置的文件名由部署决定（Gateway `--config`），所以按“数据根 `config/` 下的 YAML 文件”认，不写死 `desktop.yaml`。
- 凭据只对文件工具连读也拒，这是 3a 定的。命令不拒读：读取不在 H3 范围，命令能读到凭据是已知边界（第 3 节）。注意：模型在命令里跑 my-agent CLI 本身起不来（第 3 节第 8 条），不要以为 CLI 能用。
- 被拒时，提示指向 `user_config` / `manage_models`，它们给的是脱敏视图。

### 1.1 A 类与 B 类：每条路径只在一处（3a 定）

- **A 类：宿主托管文件，绝对只读**。上表各项都是 A 类，唯一声明在 `path_access_policy`。
  - 生效范围：任何模式（含管理员 Full Access）、所有 owner。
  - 不可被任何允许根穿透。
  - 文件工具、写边界、插件上下文、Shell 只读覆盖、上级目录改名规则都按它判。
  - 原 `tool_runtime_ledger._attach_owner_control_write_guards` 清单里的绝对项已搬到这里，从那里删掉：权限、配额、保留、记忆、skill、工具策略文件，审计流水，能力申请，临时授权，Compact，日志。
- **B 类：owner 写范围里默认禁写、可被本任务工作目录穿透的根**。
  - 范围：只剩 `runs/`、`tasks/`（二审逐个核实：里面只有规范任务根）。
  - 留在 `_attach_owner_control_write_guards`，走写范围的 `forbidden_write_roots`，按“命中的最具体条目生效”裁决，只在隔离模式挂。
  - 举例：本任务的 `work/` 是更具体的允许根，照常可写；别的任务仍禁写。
  - `tasks/`（9b 第 3 项）放 B 类：旧版任务工作区 `tasks/<日期>/<名>` 恢复时，模型要在它的 `work/` 里写。
  - owner 根的 `data/` **整个归 A 类**（3a 2026-10-02 定）：
    - 那里全是宿主状态：调度、决策、上下文校准、skill 提案与学习、产物备份、核验证据库 `verification/evidence.sqlite3`（连伴随文件）、维护标记 `maintenance.json`；`data/plugins` 另是 H2 宿主托管存储，整体隐藏。
    - 宿主以后还会往里加新东西。逐个列子目录是封闭清单，新目录默认没保护，违反“开放世界不靠写死清单”的铁律；整体归 A 才是默认安全。
    - 模型确实该写的 `data/` 路径只能作为显式例外列出，写明原因和用例。按 9b 的盘点目前一个都没有。
    - 中间一版曾把 `data/` 留在 B 类、只把 6 个子目录列进 A 类，已按这条裁定改回整体。
  - `agents/` 二审后搬到 A 类：里面是旧子代理运行状态和派工报告，不是任务树（现在子代理工作区在 `<任务根>/work/agents/` 下）。
  - 不要混淆：能力包核验记录在 `<任务根>/data/pack_verification/`，是任务根里的 `data/`，不是 owner 根的 `data/`。
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
- **数据根只有一个权威来源**（二审必须改 1）：`PathAccessPolicy.from_values(agent_home_root=...)` 由构造处结构化传入宿主启动时解析出的数据根（`home_paths`：配置 `my_agent_home` 优先，其次环境变量），传了就不读进程环境变量。
  - 来源：文件工具和 Shell 用宿主 owner home（`home_paths.owner_home_dir`）反推；registry 每次调用的策略、`validate_write_boundary`、插件读写上下文、路径门用写边界里宿主写的 `canonical_owner_home_root`；插件管理用 owner home；墙外复核沿用原策略的数据根。
  - 只有拿不到任何宿主事实时，才按 owner 墙反推、再退回 `MY_AGENT_HOME`。
  - 原问题：没有 owner 墙（Full Access）时只看环境变量，配置写了固定 home 而环境变量指向别处时，9b 实测 `write_file` 能写进 `permissions.json`、`tool_policy.json`、`runtime.db` 旁的文件和 `config/`。
- **拒绝码透传**（二审建议）：`validate_write_boundary` 返回的拒绝结果（`WriteBoundaryDenial`，仍是字符串）带结构化 `code`。
  - 路径门拒在宿主托管文件上时是原码（`HOST_FILE_DENIAL_CODES`：`PATH_HOST_MANAGED_STORE_BLOCKED`、`PATH_HOST_CONFIG_WRITE_BLOCKED`、`PATH_HOST_STATE_WRITE_BLOCKED`、`PATH_HOST_CREDENTIAL_BLOCKED`），其余拒绝仍是 `WRITE_FORBIDDEN`。
  - 动作策略（`action_policy._task_boundary_decision`）和 registry（`_write_boundary_denied`）都只读这个码，不看消息文字。真实链路里模型现在看到的是具体码和它的恢复提示。
  - 文件工具在写之前返回的 `PATH_HOST_CONFIG_WRITE_BLOCKED`、`PATH_HOST_STATE_WRITE_BLOCKED` 登记进工具协调器的“执行前确定性失败”白名单：结果归 FAILED，不会被当成“结果未知”触发停机。

### 2.2 命令类工具（沙箱只读覆盖）

- 入口：`run_command` 的前台和后台、`terminal_session`，都经 `shell._sandbox_exec` 进 `AttemptExecutionSandbox`。
- **管理员 Full Access 下 `run_command` 也进沙箱**，走 `full_access` 档。这一档不加全局写拒绝，但支持精确的只读覆盖：macOS 用 Seatbelt 的 `deny file-write*`，Linux 用 bwrap 的 `--ro-bind`。
- `shell.host_readonly_paths_for(owner, persona)` 并进 `protected_write_paths`，覆盖范围按模式分：
  - **隔离 owner**：只盖本 owner 的 `config/`、运行状态文件。数据根的配置在 owner 墙外。
  - **Full Access**：盖数据根的 `config/`、`system/config/`，加全部 owner 的。
- 本任务的 `data/pack_verification/` 只来自写边界里宿主写的结构化 `task_root`（3a 定，不从工作目录或写根反推）：`registry_invoke` 经 `task_host_state_paths` 放进 `__sandbox_protected_write_paths`，两个平台都生效。命令在任务树外（用户项目目录）跑、写根也不含本任务时照样只读。回执 `sandbox.task_records` 写明 `protected` 或 `no_task_root`。
- **全部任务的核验记录（macOS）**：`shell.host_readonly_patterns_for` → `path_access_policy.host_readonly_patterns`，按同一份任务布局（`TASK_ROOT_LAYOUT` × `HOST_STATE_TASK_PARTS`）给每个 owner home 生成一组 POSIX 正则：
  - 核验记录本身及其下内容：`^<owner>/((runs|tasks)/[^/]+/[^/]+|audits/[^/]+)/(data/pack_verification)(/|$)`；
  - 布局各级目录本身（不含其下内容）：`^<owner>/runs(/[^/]+)?(/[^/]+)?$`、`^<owner>/tasks(/[^/]+)?(/[^/]+)?$`、`^<owner>/audits(/[^/]+)?$`；
  - 任务根到记录之间的目录本身：`^<owner>/(<任务根布局>)/data$`。
  - 为什么要后两类（9b 三审实测）：只有第一类时，命令能把别的任务的 `data/`、日期目录、整个 `runs/` 或 `audits/` 改名，写进记录再改回来，伪造它的核验记录（7 种手法）。后两类只拦这些目录自己的改名、删除、新建、chmod，里面的普通读写照常。
  - 可选层级必须写成显式的 `(/[^/]+)?` 分组：Seatbelt 不认 `{m,n}` 区间，会静默失效（9b 实测）。用例断言生成的正则里没有 `{`。
  - 副作用：模型的命令不能自己新建任务根、不能在没有 `data` 的任务里建 `data`，也不能改名或删除任务目录本身。这些本来就是宿主的事。
  - owner 集合与只读覆盖相同；owner home 先解析符号链接，再按字面转义正则元字符。
  - 沙箱规格字段 `protected_write_patterns`，Seatbelt 写成 `(deny file-write* (regex "..."))`，排在写根放行之后。
  - 别的任务的、还没建出来的都盖住；本机实测改写、新建、改名 `pack_verification` 本身、`RUNS/`、`DATA/Pack_Verification` 等大小写变体都拦住（Seatbelt 在不区分大小写的卷上按不区分大小写匹配）。
  - Linux bwrap 只能挂已存在的路径，做不到按模式匹配，忽略这一项（已知边界）。
- **“宿主状态只读”环境标记**：进了沙箱的命令（前台、后台、PTY）环境里带 `MY_AGENT_HOST_STATE_READ_ONLY=1`（`HOST_STATE_READ_ONLY_ENV`），不进沙箱的宿主命令（Windows）主动去掉同名变量，只有宿主能设。my-agent CLI 按它给结构化错误码（第 3 节第 8 条）。
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

文件工具、写边界和动作策略在真实链路里都按这些具体码上报（2.1 节“拒绝码透传”）。

## 3. 已知边界（如实写明）

1. **Windows**：`run_command` 不进沙箱，命令类工具不受保护；文件工具照样受保护。生产是 macOS，Docker 车道是 Linux。
2. **命令可以读凭据**：读取不在 H3 范围（3a 定只拒写）。文件工具读不到。
3. **Linux 上还不存在的路径**：bwrap 只能把已存在的路径挂成只读，有两种情况：
   - 还没加载过的休眠 owner 没有 `config/`。缓解：`ensure_owner_home` 建 owner 时一并建出 `config/`。
   - `runtime.db-wal`、`-shm`、`-journal` 只在宿主开着库时存在。在 Full Access，或声明了自家根为写根的隔离 owner 下，命令可以抢先建出它们。这一条留作已知边界：在 bwrap 里占位会在真实 owner home 里建文件，owner home 只读挂载时还会让沙箱起不来。
   - macOS 没有这个问题。
4. **别的任务的核验记录**：文件工具按路径判，所有任务都拒写。命令这边：
   - macOS：一组 Seatbelt 正则盖住 owner 下全部任务的 `data/pack_verification/`，以及布局各级目录和 `data` 目录本身。伪造、新建、整个挪走都不行（9b 三审列的 10 种手法全拦）。
   - 更正：上一版写过“伪造不了，但能挪走”，这句不对。只盖记录本身时，命令能把上级目录改名、写进记录再改回来完成伪造（9b 三审实测），现已按上面的修法堵上。
   - Linux：只保护本任务（写边界的 `task_root`）；Full Access 下别的任务的记录命令挡不住（已知边界，ae 接受）。
5. **沙箱外早已存在的硬链接**：命令顺着它写挡不住，Seatbelt 只拦新建；文件工具有 inode 比对。
6. **插件进程**：`plugin_process_sandbox` 关闭时（默认关），插件进程以宿主权限运行，不受这道门约束。插件是管理员确认安装的代码。插件经 SDK 判路径时照样用 `check` / `check_write`。
7. **用户配置放在数据根以外**（开发时 `--config` 指到仓库里）：不受本门保护，路径策略多处构造拿不到配置路径。
8. **模型在命令里跑 my-agent CLI 会失败**（9b 二审实测，3a 最终裁定这次不修 CLI）：
   - 现象：Full Access 下 `status`、`home-status`、`subagents`、`memory-list`、`local-store-status`、`timeline`、`task-workspace-list`、`runtime-stale-attempts` 等只读命令，基线 rc=0，H3 后 rc=1。
   - 原因：CLI 每条命令都要构造完整的 `SimpleAgent`，启动时要写 `workspace/runtime` 下的本地库（`local_store/local.db`，A 类），沙箱里写不了。
   - 隔离 owner 原来就这样：基线和 H3 头上都起不来，数据根的 `config/` 在 owner 墙外读不到。
   - 请模型改用内置工具（记忆、模型、设置、任务都有对应工具）。
   - 起不来时 CLI 输出一行结构化错误 `error_code=CLI_HOST_STATE_READ_ONLY path=<被拒的路径>`，退出码 1，不再抛 `sqlite3.OperationalError` 堆栈。判断只看宿主设的环境标记和异常的 errno（EPERM/EACCES/EROFS）或 sqlite 错误码（PERM/READONLY/CANTOPEN），不解析报错文字（`cli/host_state_guard`）；沙箱外的 CLI 行为不变。Python 3.10 的 sqlite 异常没有错误码字段，照旧抛出。
   - 后续项（台账，待做）：只读子命令走只读启动，本地库用 `mode=ro` 打开或推迟到真要写时再建。
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
- 9b 二审（头 `49ef93330`）：`~/.my-agent/decision-evidence/review-h3-49ef93330/`（README、探针、数据根最小复现 `m1-policy/`）。
- 测试见 TESTS.md 同名条目。

## 7. 二审裁定与修法（9b 复审，3a 最终裁定，2026-10-02）

| 项 | 裁定 | 修法 |
| --- | --- | --- |
| 必须改 1：数据根 | 路径策略和宿主用同一个解析结果 | `from_values(agent_home_root=...)` 结构化传入，构造处见 2.1 节；用例：配置的 home ≠ 环境变量的 home，Full Access 下 `write_file` 写 A 类被拒（单元和真实链路各一条） |
| 必须改 2：只读 CLI | 这次不修 CLI，写清现状并用例锁住 | 第 3 节第 8 条；真实沙箱用例（Full Access、隔离各一遍）锁住“失败、给结构化码、A 类字节不变”；顺手项结构化错误码已做；只读启动记台账待做 |
| 必须改 3：data/ | 先裁“6 个子目录进 A、`data/` 留 B”，随后改为 owner 根的 `data/` 整体归 A（开放世界，不靠清单），B 类只剩 `runs/`、`tasks/` | 1.1 节；已知子目录逐项用例；`data/` 下新建一个原本没有的子目录，两种模式下文件工具和 Shell 都写不进，宿主维护照常写 `data/maintenance.json` |
| 必须改 4：task_root 锚点 | 只用写边界的 `task_root`，不从工作目录反推 | 2.2 节 |
| 建议 1：全部任务的核验记录 | macOS 用 Seatbelt 正则；Linux 只保护本任务，记已知边界。9b 三审补：布局各级目录和 `data` 目录本身也拒写，堵住“改名上级目录再写回” | 2.2 节、第 3 节第 4 条；真实 Seatbelt 用例覆盖 10 种手法 |
| 建议 2：拒绝码透传 | 写边界把具体码传出去 | 2.1 节；真实链路用例 |
