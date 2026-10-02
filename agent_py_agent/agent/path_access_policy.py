# LLM: 路径裁决只有这一份实现；每个策略在构造时冻结宿主根，跨进程运输不得重新猜测。核心墙外复核保留显式新策略构造，联查 owner、文件工具与读取上下文测试。
# 模块用途: 判断目标是否处于当前 owner、已授权工作根或普通路径策略允许的范围，不读取文件内容。

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

PATH_ACCESS_MODE_NORMAL = "normal"
PATH_ACCESS_MODE_FULL = "full"
DEFAULT_PATH_ACCESS_MODE = PATH_ACCESS_MODE_NORMAL
DEFAULT_DANGEROUS_PATH_ROOTS = (
    "/etc",
    "/private/etc",
    "/System",
    "/bin",
    "/sbin",
    "/usr/bin",
    "/usr/sbin",
    "/var/db",
    "/var/root",
    "/root",
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.kube",
    "~/.docker",
)
_VALID_MODES = {PATH_ACCESS_MODE_NORMAL, PATH_ACCESS_MODE_FULL}
# 一次运行的读取范围只有这三种，与 PathAccessPolicy.check 的判定顺序一一对应：先看 owner 墙，再看 full，最后是 normal。
# 常量用途: 给“按调用方实际路径范围渲染工具说明”这类展示用；它们是宿主策略的归类，不是授权本身。
PATH_SCOPE_OWNER_WALL = "owner_wall"
PATH_SCOPE_FULL = "full"
PATH_SCOPE_NORMAL = "normal"
# 这两类运行（子代理与控制面）受 SubAgentManager 的 owner 墙约束，即使父代理是管理员 Full Access。
_CHILD_CONTEXT_SCOPES = frozenset({"task_local", "control_plane"})

# 文件系统根级目录是操作系统本体而不是"工作目录"：管理员即使 full-access 也不把它自动
# 继承给子代理或当成工具执行根。这是写死的少量常量，不是可扩张的名单，也不做任何危险判断；
# 判据只看归一化后的真实路径，不读模型文字。
# 常量用途: 列出不得作为可继承工作根自动下发的根级目录。
UNINHERITABLE_ROOT_DIRS = ("/", "/System", "/usr", "/bin", "/sbin", "/private/etc", "/etc")

# H2（2026-10-01）宿主托管存储的唯一声明：owner home 里内容只由宿主组件按生命周期读写的目录（相对 owner home 的路径片段）。
#   data/plugins 是插件安装库（installations.json、packages/ 包库、environments/ 解压环境、data/ 插件私有数据），由安装、启用、停用、
#   换代统一裁决；模型的文件工具和 shell 直接读写会绕过这些裁决（真实复现：run_command 用 unzip 从包库读出已停用、已换代的旧包）。
#   必须与规范布局 user_space.owner_resolver 的 plugins_dir 一致（test_host_managed_store_access 有守卫用例）；新增同类存储只在这里加。
#   本模块被原样打进插件 SDK，所以布局写成路径片段而不是导入 owner_resolver。
# 常量用途: 列出对模型工具不开放的宿主托管存储相对 owner home 的位置。
HOST_MANAGED_OWNER_STORE_PARTS: tuple[tuple[str, ...], ...] = (("data", "plugins"),)

# H3（2026-10-02）宿主配置的唯一声明：改配置只有参数中心（user_config、/settings）和 manage_models（/model）两个权威入口，
#   模型的文件工具和命令直接写这些目录会绕过边界键、修改账本、撤销和模型目录检查（be 复审语义记忆时发现）。读取照常，只拒写。
#   数据根下 config/（用户配置及备份、模型目录、共享模型档案、修改账本、管理员密码）与 system/config/；每个 owner home 的 config/
#   （capability_config.yaml）。必须与 home_layout 的 config_dir、system_config_dir 及 runtime_config_reload.default_capability_config_path
#   一致（test_host_files_access 有守卫用例）。本模块被原样打进插件 SDK，所以写成路径片段。
# 常量用途: 列出对模型工具只读的宿主配置目录（分别相对数据根、相对 owner home）。
HOST_CONFIG_HOME_PARTS: tuple[tuple[str, ...], ...] = (("config",), ("system", "config"))
HOST_CONFIG_OWNER_PARTS: tuple[tuple[str, ...], ...] = (("config",),)

# H3 宿主运行状态（3a 2026-10-02 扩项，A 类“绝对只读”的唯一声明）：owner home 里只由宿主写的权威账本、控制面和派生数据，模型的
#   文件工具与命令在任何模式下都只能读、不能写（按路径拒写，不管存不存在；不可被任何允许根穿透）。来源：9b 的家目录盘点
#   （~/.my-agent/decision-evidence/owner-home-host-files-inventory-a9c2b691f/）与原 tool_runtime_ledger 控制面清单中的绝对项
#   （已从那里删掉，每条路径只在一处）。可被本任务工作目录穿透的任务树根（runs/、agents/、data/、tasks/）不在这里，见
#   agent_core.tool_runtime_ledger._attach_owner_control_write_guards（B 类）。宿主自己的写入（记忆工具、Curator、策略服务、
#   runtime 仓库）在宿主进程里，不经过模型工具，不受影响。
#   - 文件：权限、配额、保留、记忆、skill、工具策略，审计流水，记忆操作流水与候选；每个文件旁的 .lock 一并保护（抢锁会卡住宿主写入）。
#   - 目录：能力申请、临时授权、Compact、日志、审计、会话与事件库（workspace/runtime）、Curator 事务、记忆归档、缓存、回收站、
#     owner 级正式 skill（skills/ 与家目录根的 .agents/skills/；项目工作区里的 skills 不在 owner home 下，不受影响）。
#   - SQLite 库连 -wal/-shm/-journal 伴随文件一起保护：runtime.db（runtime_db.schema.RUNTIME_DB_FILENAME）。
# 常量用途: 列出 owner home 里对模型工具只读的宿主运行状态（文件、目录、SQLite 库）。
HOST_STATE_OWNER_FILES: tuple[tuple[str, ...], ...] = (
    ("permissions.json",), ("quota.json",), ("retention.json",), ("memory_policy.json",), ("skill_policy.json",),
    ("tool_policy.json",), ("audit_log.jsonl",), ("memory", "ops.jsonl"), ("memory", "candidates.jsonl"),
)
HOST_STATE_OWNER_DIRS: tuple[tuple[str, ...], ...] = (
    ("capability_requests",), ("temporary_grants",), ("compact",), ("logs",), ("audit",), ("workspace", "runtime"),
    ("memory", "curator"), ("memory_archive",), ("cache",), ("trash",), ("skills",), (".agents", "skills"),
)
HOST_STATE_OWNER_SQLITE: tuple[str, ...] = ("runtime.db",)
SQLITE_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
HOST_STATE_LOCK_SUFFIX = ".lock"

# H3 任务内的宿主托管位置（ae 能力包块 4）：相对规范任务根的路径片段，模型工具只读。规范任务根与
#   conversation.workspace_paths.canonical_task_root 一致：owner home 下 runs/<日期>/<键>、tasks/<日期>/<名>、audits/<编号>
#   （守卫用例在 test_host_files_access）。data/pack_verification 是能力包核验的原件清单与核验账本。
# 常量用途: 列出规范任务根的布局与任务里对模型工具只读的宿主托管位置。
TASK_ROOT_LAYOUT: tuple[tuple[str, int], ...] = (("runs", 2), ("tasks", 2), ("audits", 1))
HOST_STATE_TASK_PARTS: tuple[tuple[str, ...], ...] = (("data", "pack_verification"),)

# H3 凭据（3a 定）：宿主配置里存放密钥的位置，文件工具连读也拒；命令只拒写不拒读（my-agent CLI 要读它们，已知边界）。相对数据根。
#   用户配置的文件名由部署决定（Gateway --config），所以按“数据根 config/ 下名字里有 yaml/yml 段的文件”认，备份
#   desktop.yaml.bak-* 一并算上；数据根里 owner home 之外任何一层叫 secrets 的目录也按密钥目录处理。
# 常量用途: 列出对模型文件工具不可读的宿主凭据位置。
HOST_CREDENTIAL_HOME_PARTS: tuple[tuple[str, ...], ...] = (
    ("config", "admin-password.json"),
    ("config", "shared-model-profiles.json"),
    ("config", "model-profiles"),
)
HOST_CONFIG_YAML_SEGMENTS = frozenset({"yaml", "yml"})
HOST_SECRET_DIR_NAME = "secrets"

# 选项1-B 凭据文件名 denylist(抄 长期助手 file_safety):这些每每装 API key/密码,文件工具一律拒。
_CREDENTIAL_FILENAMES = frozenset(
    {
        ".git-credentials",
        "auth.json",
        ".anthropic_oauth.json",
        ".credentials.json",
        ".netrc",
        ".pgpass",
    }
)


def _is_credential_filename(name: str) -> bool:
    """文件名是否是凭据文件(.env 家族 / 凭据存储)。.env.example 是文档化模板,放行(不含真密钥)。"""
    n = str(name or "").strip().lower()
    if not n or n == ".env.example":
        return False
    if n in _CREDENTIAL_FILENAMES:
        return True
    # .env / .env.local / .env.production …(.env.example 上面已放行)
    return n == ".env" or n.startswith(".env.")


# LLM: 结构化决定供宿主和插件共同消费；code 保留原权限分类，不从消息正文推导放行。
# 类用途: 表达路径是否允许以及明确拒绝原因。
@dataclass(frozen=True)
class PathAccessDecision:
    allowed: bool
    code: str = ""
    message: str = ""
    dangerous_root: str = ""


# LLM: 所有根在宿主构造时固定；owner 墙优先于 full，隔离进程应恢复原字段而非调用 from_values 重读环境。
# 类用途: 保存不可变的路径权限，统一核心文件工具和插件读取的裁决。
@dataclass(frozen=True)
class PathAccessPolicy:
    mode: str = DEFAULT_PATH_ACCESS_MODE
    dangerous_roots: tuple[Path, ...] = ()
    # 多用户隔离硬墙(0 层):设了 owner_scope_root = per-user/group agent 时,my-agent 数据目录
    #   只放行自己的 owner home 与 shared/ 公共能力区。其它 owner、identity、system、全局索引、
    #   根级模板和运行数据一律拒绝；即使 path_access_mode=full 也不能跨过租户边界。
    #   不设 = 本地管理员/单租户原行为(整个 .my-agent 豁免)。
    owner_scope_root: Path | None = None
    agent_home_root: Path | None = None

    # LLM: 配置读取只在构造处发生；配置切换应重建策略，不原地改变已在执行的调用。
    # 函数用途: 从当前配置创建冻结路径策略。
    @classmethod
    def from_config(cls, config: object | None) -> PathAccessPolicy:
        return cls.from_values(
            mode=getattr(config, "path_access_mode", DEFAULT_PATH_ACCESS_MODE),
            dangerous_roots=getattr(config, "path_dangerous_roots", DEFAULT_DANGEROUS_PATH_ROOTS),
        )

    # LLM: 当前用户 home 过滤与数据根豁免必须由宿主计算一次；直接恢复协议字段不得再次应用此环境归一化。
    # 函数用途: 规范化模式和危险根，并固定 owner 及数据根事实。
    @classmethod
    def from_values(
        cls,
        *,
        mode: object = DEFAULT_PATH_ACCESS_MODE,
        dangerous_roots: Iterable[object] | None = None,
        owner_scope_root: object = None,
    ) -> PathAccessPolicy:
        normalized_mode = normalize_path_access_mode(mode)
        roots = tuple(_normalized_root(item) for item in (dangerous_roots or DEFAULT_DANGEROUS_PATH_ROOTS))
        scope = _normalized_root(owner_scope_root) if owner_scope_root else None
        home = _current_user_home()
        # 当前用户 home 的整根放行只属于无 owner scope 的本地管理员：root 部署时否则会误伤
        # /root/my-agent-src。远程 owner 必须保留 /root 等宿主 home 危险根；它自己的数据目录会先经
        # MY_AGENT_HOME + owner_scope_root 窄白名单放行，不能借宿主进程身份读取 /root/secret。
        if scope is None:
            roots = tuple(root for root in roots if root is not None and root != home)
        else:
            roots = tuple(root for root in roots if root is not None)
        roots = tuple(dict.fromkeys(roots))
        return cls(mode=normalized_mode, dangerous_roots=roots, owner_scope_root=scope,
                   agent_home_root=_home_root_from_owner_scope(scope) if scope else _my_agent_home_root())

    # LLM: 宿主托管存储最先拒绝（PATH_HOST_MANAGED_STORE_BLOCKED，H2）；之后 owner 墙强于模式；模式放行后再拒宿主凭据
    #   （PATH_HOST_CREDENTIAL_BLOCKED，H3），所以原有拒绝码不变。只读构造时冻结的根，不受另一个会话或子进程环境影响。
    #   这是读写共用的裁决；写还要再过 check_write / host_write_decision。
    # 函数用途: 解析目标并判断是否位于当前 owner 或管理员允许访问的路径范围，不读取内容。
    def check(self, path: str | Path) -> PathAccessDecision:
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败，请检查路径是否有效。")
        # H2：宿主托管存储（插件安装库、包库）不随 owner 墙、Full Access 或数据根豁免开放，最先判定。
        store = self._host_managed_store_of(resolved)
        if store is not None:
            return PathAccessDecision(
                False,
                "PATH_HOST_MANAGED_STORE_BLOCKED",
                f"宿主托管存储（插件安装库、包库）不对模型工具开放，能力包内容请经宿主工具读取: target={resolved}",
                str(store),
            )
        decision = self._mode_decision(resolved)
        if not decision.allowed:
            return decision
        return self._host_credential_decision(resolved)

    # LLM: H3 写门：先走 check（读写共用的全部拒绝），再拒宿主配置与宿主运行状态。核心写边界（tooling/write_boundary）与插件
    #   写入上下文（workspace_write_context）都调它，两边裁决保持一致；文件工具的 resolve_write_path 因要先过墙外授权根，单独调
    #   host_write_decision。
    # 函数用途: 判断模型工具能否写这个路径。
    def check_write(self, path: str | Path) -> PathAccessDecision:
        decision = self.check(path)
        if not decision.allowed:
            return decision
        return self.host_write_decision(path)

    # LLM: H3：只判宿主托管文件（配置：HOST_CONFIG_*；运行状态：HOST_STATE_*），按解析后的真实路径、大小写无关地比较，目标不存在
    #   也判；与宿主配置或 owner 运行状态文件是同一个硬链接时同样拒绝。不读文件内容，硬链接比对只读元数据。数据根未知时放行。
    # 函数用途: 目标是宿主配置时返回 PATH_HOST_CONFIG_WRITE_BLOCKED，是宿主运行状态时返回 PATH_HOST_STATE_WRITE_BLOCKED。
    def host_write_decision(self, path: str | Path) -> PathAccessDecision:
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败，请检查路径是否有效。")
        if self.agent_home_root is None:
            return PathAccessDecision(True)
        return _host_write_block(resolved, self.agent_home_root) or PathAccessDecision(True)

    # LLM: H3 凭据只在模式已放行之后才拒（owner 墙、危险根等原拒绝码优先）；判定见 host_credential_for_path，只做路径运算。
    # 函数用途: 目标是宿主凭据时返回 PATH_HOST_CREDENTIAL_BLOCKED，提示改用脱敏的 user_config / manage_models。
    def _host_credential_decision(self, resolved: Path) -> PathAccessDecision:
        secret = host_credential_for_path(resolved, self.agent_home_root) if self.agent_home_root else None
        if secret is None:
            return PathAccessDecision(True)
        return PathAccessDecision(
            False,
            "PATH_HOST_CREDENTIAL_BLOCKED",
            "宿主凭据（用户配置、模型档案、管理员密码、密钥目录）不对模型的文件工具开放：看设置用 user_config，"
            f"看模型和服务商用 manage_models（都是脱敏视图）: target={resolved}",
            str(secret),
        )

    # LLM: check 拆出的模式裁决，顺序不变：owner 墙 → full → normal（凭据文件名、数据根豁免、危险根）。
    # 函数用途: 按 owner 墙和路径模式判断一个已解析路径是否可访问。
    def _mode_decision(self, resolved: Path) -> PathAccessDecision:
        # owner 隔离是租户边界，不是普通安全模式。远程 owner 的文件可见面
        # 只有自己 home + shared；不仅是 .my-agent 里的其他目录，宿主其他位置也默认拒绝。
        # 必须先于 full 判定，避免 path_access_mode=full 变成跨租户/跨宿主读权。
        home_root = self.agent_home_root
        if self.owner_scope_root is not None:
            # WorkspaceOnly 的唯一硬边界就是 owner home。用户自己的文件（包含项目自用
            # .env/认证配置）不再被第二层文件名 denylist 误伤；凭据仍不会被日志主动输出，
            # 也不能借此跨到其他 owner 或宿主目录。
            if _is_relative_to(resolved, self.owner_scope_root):
                return PathAccessDecision(True)
            if home_root is not None and _is_relative_to(resolved, home_root):
                decision = self._owner_scope_decision(resolved, home_root)
                if not decision.allowed:
                    return decision
                if _is_credential_filename(resolved.name):
                    return _credential_file_decision(resolved)
                return decision
            return PathAccessDecision(
                False,
                "PATH_OWNER_SCOPE_BLOCKED",
                f"当前用户只能访问自己的数据目录和 shared 公共能力区: target={resolved}",
                str(self.owner_scope_root),
            )
        if self.mode == PATH_ACCESS_MODE_FULL:
            return PathAccessDecision(True)
        # 无 owner wall 的 legacy normal 模式仍保护常见凭据文件；显式 Full Access 与
        # owner 自己家已在上方结构化放行，不靠自然语言猜授权。
        if _is_credential_filename(resolved.name):
            return _credential_file_decision(resolved)
        # my-agent 自己的数据目录(home,默认 ~/.my-agent,可经 MY_AGENT_HOME 覆盖)豁免 dangerous_roots:
        # agent 写自己的产物/记忆/审计天经地义。否则 root 用户场景下 /root 被列危险目录,会误伤
        # /root/.my-agent/.../output(agent 自己的产物目录)。豁免精确到 home 子树——/root/.ssh 等敏感
        # 目录不在 my-agent home 下,仍被 dangerous_roots 拦截,口子不扩大(resolve 已展开 .. 防逃逸)。
        if home_root is not None and _is_relative_to(resolved, home_root):
            return PathAccessDecision(True)
        for root in self.dangerous_roots:
            if _is_relative_to(resolved, root):
                return PathAccessDecision(
                    False,
                    "PATH_DANGEROUS_ROOT_BLOCKED",
                    f"路径位于危险目录，当前 path_access_mode=normal 不允许访问: target={resolved} dangerous_root={root}",
                    str(root),
                )
        return PathAccessDecision(True)

    # LLM: H2（2026-10-01）：宿主托管存储的唯一声明是本模块的 HOST_MANAGED_OWNER_STORE_PARTS；这里只用构造时冻结的
    #   agent_home_root 做路径运算（不读文件、不读环境），数据根未知时返回 None。check 与目录遍历类工具（list/find）共用它。
    # 函数用途: 返回路径所在的宿主托管存储根；不在其中返回 None。
    def host_managed_store(self, path: str | Path) -> Path | None:
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        return self._host_managed_store_of(resolved)

    # LLM: 调用方已 resolve；只做路径运算。本模块会被原样打进插件 SDK（scripts/build_plugin_api.SDK_SOURCES），必须只依赖标准库。
    # 函数用途: 用冻结的数据根判断已解析路径是否落在宿主托管存储里。
    def _host_managed_store_of(self, resolved: Path) -> Path | None:
        if self.agent_home_root is None:
            return None
        return host_managed_store_for_path(resolved, self.agent_home_root)

    # LLM: 只有 owner 普通墙拒绝允许原明确授权根复核，跨 owner/控制面等拒绝不能覆盖；插件须传宿主冻结的 external_policy。
    # 函数用途: 为核心文件工具和隔离插件统一检查已授权的墙外工作路径。
    def check_with_external_roots(
        self, resolved: Path, granted_external_roots: tuple[Path, ...],
        *, external_policy: PathAccessPolicy | None = None,
    ) -> PathAccessDecision:
        decision = self.check(resolved)
        if decision.allowed or decision.code != "PATH_OWNER_SCOPE_BLOCKED":
            return decision
        if not any(_is_relative_to(resolved, root) for root in granted_external_roots):
            return decision
        policy = external_policy or PathAccessPolicy.from_values(mode=self.mode, dangerous_roots=self.dangerous_roots)
        return policy.check(resolved)

    def _owner_scope_decision(self, resolved: Path, home_root: Path) -> PathAccessDecision:
        """多用户隔离:my-agent 数据目录内的 owner 级判定。

        - 自己 owner home 子树 → 放行(自己家随便读写);
        - shared/ → 放行公共 skills/scripts 等只读/受管能力区；内置工具来自代码 registry，
          管理员扩展工具来自显式安装的 plugin，不从 shared Markdown/目录自动执行；
        - admin_grants/ → 拦(宿主保留的控制目录，owner 不能写入或借此改变权限);
        - owners/ 下但不是自己的 → 拦(别人的家,PATH_CROSS_OWNER_BLOCKED);
        - 其余 .my-agent 顶层事实源全部拦；共享能力只有 shared/ 一个权威位置。
        """
        assert self.owner_scope_root is not None
        if _is_relative_to(resolved, self.owner_scope_root):
            return PathAccessDecision(True)
        shared_root = home_root / "shared"
        if _is_relative_to(resolved, shared_root):
            return PathAccessDecision(True)
        admin_grants_root = home_root / "admin_grants"
        if _is_relative_to(resolved, admin_grants_root):
            # 这是历史布局中仍保留的宿主控制目录，不再是 Full Access 的运行时来源。
            # owner-scoped agent 仍一律拦，避免用户通过写控制面文件改变自己的权限。
            return PathAccessDecision(
                False,
                "PATH_ADMIN_GRANTS_BLOCKED",
                f"禁止访问宿主保留的 admin_grants 控制目录: target={resolved}",
                str(admin_grants_root),
            )
        owners_root = home_root / "owners"
        if _is_relative_to(resolved, owners_root):
            return PathAccessDecision(
                False,
                "PATH_CROSS_OWNER_BLOCKED",
                f"禁止访问其他用户的数据目录(多用户隔离): target={resolved}",
                str(owners_root),
            )
        return PathAccessDecision(
            False,
            "PATH_OWNER_SCOPE_BLOCKED",
            f"当前用户只能访问自己的数据目录和 shared 公共能力区: target={resolved}",
            str(home_root),
        )


# LLM: Credential denial is a legacy/unscoped normal-mode guard. Owner-home and explicit full
# access decisions must be made before calling it so filename heuristics never become authority.
# 函数用途: 生成统一的凭据文件拒绝结果，供未处于 owner 私有区或 Full Access 的路径使用。
def _credential_file_decision(resolved: Path) -> PathAccessDecision:
    return PathAccessDecision(
        False,
        "PATH_CREDENTIAL_FILE_BLOCKED",
        f"禁止读写凭据文件(可能含 API key/密码): target={resolved}；如需看结构请用 .env.example。",
        resolved.name,
    )


def normalize_path_access_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in _VALID_MODES else PATH_ACCESS_MODE_NORMAL


# LLM: 本 run 真正生效的 owner 墙只在这里算：子代理/控制面用 SubAgentManager 的墙，其次是 agent 当前工具视图自己的墙，
#   再次是写边界里已冻结的值，最后是执行注册表自己的墙。写边界合并、执行门、快照冻结和子代理读取预检都调用它，
#   不接受模型参数或自然语言；本机管理员 Full Access 的主 run 各项皆空，即没有 owner 墙。
# 函数用途: 算出一次运行实际受哪道 owner 墙约束，返回墙的根路径；空串表示没有 owner 墙。
def effective_owner_scope_root(agent: object = None, *, context_scope: object = "default",
                               write_boundary: object = None, registry_scope: object = "") -> str:
    candidates: list[object] = []
    if (str(context_scope or "").strip().lower() or "default") in _CHILD_CONTEXT_SCOPES:
        candidates.append(getattr(getattr(agent, "subagents", None), "owner_scope_root", ""))
    candidates.append(getattr(getattr(agent, "tools", None), "owner_scope_root", ""))
    if isinstance(write_boundary, dict):
        candidates.append(write_boundary.get("effective_owner_scope_root"))
    candidates.append(registry_scope)
    return next((text for text in (str(item or "").strip() for item in candidates) if text), "")


# LLM: 只把（有效 owner 墙, 路径模式）归成三种范围之一，顺序与 check 相同：有墙时 full 也越不过墙。
# 函数用途: 告诉展示层这次运行属于哪种读取范围，好给出与路径门一致的说明。
def path_scope_regime(owner_scope_root: object, path_access_mode: object) -> str:
    if str(owner_scope_root or "").strip():
        return PATH_SCOPE_OWNER_WALL
    return PATH_SCOPE_FULL if normalize_path_access_mode(path_access_mode) == PATH_ACCESS_MODE_FULL else PATH_SCOPE_NORMAL


def _normalized_root(value: object) -> Path | None:
    text = os.path.expandvars(str(value or "").strip())
    if not text:
        return None
    try:
        return Path(text).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _current_user_home() -> Path | None:
    try:
        return Path.home().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _my_agent_home_root() -> Path | None:
    """my-agent 数据目录根(默认 ~/.my-agent,可经 MY_AGENT_HOME 覆盖)。agent 写自己 home 子树
    豁免 dangerous_roots——修 root 用户场景下 /root 被列危险目录误伤 /root/.my-agent 产物的问题。"""
    raw = os.environ.get("MY_AGENT_HOME", "").strip() or "~/.my-agent"
    try:
        return Path(os.path.expandvars(raw)).expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        return None


def _home_root_from_owner_scope(owner_scope_root: Path) -> Path | None:
    """Derive the canonical my-agent home from an already trusted owner path.

    Runtime configs may point at a non-default home without exporting
    ``MY_AGENT_HOME``.  The owner scope itself is the structured authority, so
    shared/cross-owner decisions must not fall back to a process-global env
    guess.
    """

    return agent_home_root_for_owner(owner_scope_root) or _my_agent_home_root()


# LLM: 任何需要区分"用户工作目录"与"my-agent 运行记录区"的模块都必须用这一个推导，
# 不要再各写一份 parents 遍历；布局不是 owners/<provider>/<owner> 时返回 None，调用方
# 自行决定回退，不能把 None 当成"整个 home 都不可用"。
# 函数用途: 从可信 owner home 反推 my-agent 数据根（例如 ~/.my-agent）。
def agent_home_root_for_owner(owner_home: object) -> Path | None:
    resolved = _normalized_root(owner_home)
    if resolved is None:
        return None
    for candidate in (resolved, *resolved.parents):
        if candidate.name == "owners":
            return candidate.parent
    return None


# LLM: 与 user_space.owner_resolver._owner_home_dir 互为逆运算（owners/local/main 或 owners/providers/<p>/<users|groups>/<id>），
#   只做路径运算、不读文件；调用方传已 resolve 的路径与数据根。改布局时两处一起改（守卫用例在 test_host_managed_store_access）。
#   比较大小写无关（见 _relative_folded），返回值取输入路径自己的写法。
# 函数用途: 从规范布局反推一个路径所在的 owner home；不在任何 owner home 里返回 None。
def owner_home_containing(path: Path, agent_home_root: Path) -> Path | None:
    parts = _relative_folded(path, agent_home_root / "owners")
    if parts is None:
        return None
    depth = 0
    if parts[:2] == ("local", "main"):
        depth = 2
    elif len(parts) >= 4 and parts[0] == "providers" and parts[2] in {"users", "groups"}:
        depth = 4
    return _prefix_of(path, parts, depth) if depth else None


# LLM: 只做路径运算：路径落在任一 owner home 的托管存储（含存储根本身）里就返回该存储根，否则 None。路径策略据此统一拒绝。
# 函数用途: 判断一个已解析路径是否属于宿主托管存储。
def host_managed_store_for_path(path: Path, agent_home_root: Path) -> Path | None:
    home = owner_home_containing(path, agent_home_root)
    if home is None:
        return None
    return _declared_root_containing(path, home, HOST_MANAGED_OWNER_STORE_PARTS)


# LLM: H3 只做路径运算：数据根 config/、system/config/ 或任一 owner home 的 config/（含目录本身）里就返回该目录，否则 None。
#   与 owner 墙无关：隔离 owner 声明自家根为工作目录时，自家 config/ 也靠它拒写。
# 函数用途: 判断一个已解析路径是否落在宿主配置目录里。
def host_config_root_for_path(path: Path, agent_home_root: Path) -> Path | None:
    found = _declared_root_containing(path, agent_home_root, HOST_CONFIG_HOME_PARTS)
    if found is not None:
        return found
    home = owner_home_containing(path, agent_home_root)
    return None if home is None else _declared_root_containing(path, home, HOST_CONFIG_OWNER_PARTS)


# LLM: H3 凭据只做路径运算（声明见 HOST_CREDENTIAL_HOME_PARTS）：命中声明位置、数据根 config/ 下的 YAML 配置文件（含备份），
#   或 owner home 之外名为 secrets 的目录时返回命中的那一层，否则 None。owner home 里是用户自己的工作，不按目录名拦。
# 函数用途: 判断一个已解析路径是否是宿主凭据。
def host_credential_for_path(path: Path, agent_home_root: Path) -> Path | None:
    found = _declared_root_containing(path, agent_home_root, HOST_CREDENTIAL_HOME_PARTS)
    if found is not None:
        return found
    parts = _relative_folded(path, agent_home_root)
    if parts is None or owner_home_containing(path, agent_home_root) is not None:
        return None
    if len(parts) == 2 and parts[0] == "config" and HOST_CONFIG_YAML_SEGMENTS & set(parts[1].split(".")[1:]):
        return path
    if HOST_SECRET_DIR_NAME in parts:
        return _prefix_of(path, parts, parts.index(HOST_SECRET_DIR_NAME) + 1)
    return None


# LLM: H3 运行状态只做路径运算：owner home 里声明的宿主权威文件（含 SQLite 伴随文件，不管存不存在），或规范任务根里声明的
#   宿主托管位置（HOST_STATE_TASK_PARTS）时返回命中的那一层，否则 None。
# 函数用途: 判断一个已解析路径是否是宿主运行状态。
def host_state_for_path(path: Path, agent_home_root: Path) -> Path | None:
    home = owner_home_containing(path, agent_home_root)
    if home is None:
        return None
    found = _declared_root_containing(path, home, _owner_state_parts())
    if found is not None:
        return found
    task = task_root_containing(path, home)
    return None if task is None else _declared_root_containing(path, task, HOST_STATE_TASK_PARTS)


# LLM: 与 conversation.workspace_paths.canonical_task_root 同一布局（TASK_ROOT_LAYOUT），只做路径运算、大小写无关；
#   路径就是任务根本身或更深时返回任务根，否则 None。
# 函数用途: 从规范布局找出路径所在的任务根。
def task_root_containing(path: Path, owner_home: Path) -> Path | None:
    parts = _relative_folded(path, owner_home)
    for name, depth in TASK_ROOT_LAYOUT:
        if parts and parts[0] == name and len(parts) > depth:
            return _prefix_of(path, parts, depth + 1)
    return None


# LLM: H3：给命令沙箱列只读覆盖。owner_home 给出时只列这个 owner 的 config/ 和运行状态文件（数据根的配置本来就在 owner 墙外）；
#   为空时列数据根的 config/、system/config/ 和全部已存在 owner home 的。anchors（本次命令的工作目录与写根）所在任务根里的
#   宿主托管位置也列上（别的任务不枚举）。不按存在过滤：macOS 对还不存在的路径也能拒写，Linux 由挂载层只挂已存在的。
# 函数用途: 列出要对模型命令设成只读的宿主托管路径。
def host_readonly_paths(agent_home_root: Path, owner_home: Path | None = None,
                        anchors: tuple[Path, ...] = ()) -> tuple[Path, ...]:
    homes = (owner_home,) if owner_home is not None else _existing_owner_homes(agent_home_root)
    home_parts = () if owner_home is not None else HOST_CONFIG_HOME_PARTS
    paths = [*(agent_home_root.joinpath(*parts) for parts in home_parts),
             *(home.joinpath(*parts) for home in homes for parts in (*HOST_CONFIG_OWNER_PARTS, *_owner_state_parts()))]
    for anchor in anchors:
        home = owner_home_containing(anchor, agent_home_root)
        task = task_root_containing(anchor, home) if home is not None else None
        paths.extend(task.joinpath(*parts) for parts in HOST_STATE_TASK_PARTS if task is not None)
    return tuple(dict.fromkeys(paths))


# LLM: 写门的拒绝结果只在这里拼：配置优先于运行状态，再看硬链接（按它连到的那个文件归类）。提示文字给模型下一步该用的入口。
# 函数用途: 目标是宿主托管文件时返回拒写结果，否则 None。
def _host_write_block(resolved: Path, agent_home_root: Path) -> PathAccessDecision | None:
    linked = _hardlinked_host_file(resolved, agent_home_root)
    target = linked or resolved
    config = host_config_root_for_path(target, agent_home_root)
    if config is not None:
        return PathAccessDecision(
            False, "PATH_HOST_CONFIG_WRITE_BLOCKED",
            "宿主配置目录不对模型工具开放写入：改设置请用 user_config（参数中心，带边界检查、修改记录和撤销），"
            f"改模型和服务商请用 manage_models；读取不受影响: target={resolved}", str(config))
    state = host_state_for_path(target, agent_home_root)
    if state is not None:
        return PathAccessDecision(
            False, "PATH_HOST_STATE_WRITE_BLOCKED",
            "宿主运行状态（runtime.db 等权威账本、能力包核验记录）只由宿主写，模型工具不能改；任务、运行和核验状态请经对应的宿主工具"
            f"改变，读取不受影响: target={resolved}", str(state))
    return None


# 函数用途: owner home 里运行状态的全部路径片段：声明的目录，声明的文件及其 .lock，每个 SQLite 库及其伴随文件。
def _owner_state_parts() -> tuple[tuple[str, ...], ...]:
    return (*HOST_STATE_OWNER_DIRS, *_owner_state_file_parts())


# 函数用途: owner home 里运行状态的文件片段（不含目录）：声明的文件、它们的 .lock、SQLite 库及伴随文件；硬链接比对只看这些。
def _owner_state_file_parts() -> tuple[tuple[str, ...], ...]:
    locks = tuple((*parts[:-1], parts[-1] + HOST_STATE_LOCK_SUFFIX) for parts in HOST_STATE_OWNER_FILES)
    sqlite = tuple((name + suffix,) for name in HOST_STATE_OWNER_SQLITE for suffix in ("", *SQLITE_SIDECAR_SUFFIXES))
    return (*HOST_STATE_OWNER_FILES, *locks, *sqlite)


# LLM: H3 硬链接加固：文件工具自己建不了硬链接，但会顺着已有的硬链接写进去。只在目标是有多个链接的普通文件时才比对：
#   各配置目录里的文件、各 owner 运行状态里声明的文件（st_dev + st_ino）。运行状态目录（日志、缓存、归档等可能很大）和任务里的
#   托管位置不枚举，平时不扫描（uv 等工具建的硬链接很常见，扫大目录会拖慢每次写）。只读元数据，出错按没命中处理。
# 函数用途: 目标和某个宿主托管文件是同一个文件时返回那个文件，否则 None。
def _hardlinked_host_file(path: Path, agent_home_root: Path) -> Path | None:
    try:
        info = path.stat()
    except OSError:
        return None
    if info.st_nlink < 2 or not stat.S_ISREG(info.st_mode):
        return None
    homes = _existing_owner_homes(agent_home_root)
    roots = (*(agent_home_root.joinpath(*parts) for parts in HOST_CONFIG_HOME_PARTS),
             *(home.joinpath(*parts) for home in homes for parts in (*HOST_CONFIG_OWNER_PARTS, *_owner_state_file_parts())))
    candidates = (item for root in roots for item in _files_at(root))
    return next((item for item in candidates if item != path and _same_file(info, item)), None)


# 函数用途: 列出一个路径本身（是文件时）或它下面的全部文件；不存在或读取出错时返回空元组。
def _files_at(root: Path) -> tuple[Path, ...]:
    try:
        if root.is_file():
            return (root,)
        return tuple(item for item in root.rglob("*") if item.is_file())
    except OSError:
        return ()


# 函数用途: 判断一个路径是否就是已 stat 过的那个文件（同一设备同一 inode）。
def _same_file(info: os.stat_result, candidate: Path) -> bool:
    try:
        return os.path.samestat(info, candidate.stat())
    except OSError:
        return False


# LLM: 返回 path 落在 base 下某个声明片段里时的那一层（写法取自 path），否则 None；比较大小写无关。
# 函数用途: 在基准目录下按声明的路径片段找出包含目标的那一层。
def _declared_root_containing(path: Path, base: Path, declared: tuple[tuple[str, ...], ...]) -> Path | None:
    parts = _relative_folded(path, base)
    if parts is None:
        return None
    for item in declared:
        if parts[:len(item)] == tuple(part.casefold() for part in item):
            return _prefix_of(path, parts, len(item))
    return None


# LLM: macOS 与 Windows 默认文件系统大小写不敏感，而 Path.resolve 保留输入的大小写（本机实测：config/desktop.yaml 写成
#   CONFIG/Desktop.YAML 照样打开同一个文件）。宿主声明位置因此按 casefold 比较；Linux 大小写敏感时只会多拦数据根里
#   仅大小写不同的同名目录，不会少拦。Seatbelt 按磁盘上的真实路径匹配，不受影响。
# 函数用途: 返回 path 相对 base 的各段（已 casefold）；path 不在 base 下时返回 None。
def _relative_folded(path: Path, base: Path) -> tuple[str, ...] | None:
    folded = tuple(part.casefold() for part in path.parts)
    prefix = tuple(part.casefold() for part in base.parts)
    return folded[len(prefix):] if folded[:len(prefix)] == prefix else None


# 函数用途: 取 path 里 base 之后再往下 depth 段的那一层目录（relative 是 path 相对 base 的各段）。
def _prefix_of(path: Path, relative: tuple[str, ...], depth: int) -> Path:
    return Path(*path.parts[:len(path.parts) - len(relative) + depth])


# LLM: owner_home 给出时只列这个 owner 的；为空时列数据根下全部 owner 的（Full Access 的进程沙箱用）。只列已存在的目录，
#   只读目录元数据，不读内容。
# 函数用途: 列出要对模型进程隐藏的宿主托管存储目录。
def host_managed_store_dirs(agent_home_root: Path, owner_home: Path | None = None) -> tuple[Path, ...]:
    homes = (owner_home,) if owner_home is not None else _existing_owner_homes(agent_home_root)
    return tuple(
        home.joinpath(*parts) for home in homes for parts in HOST_MANAGED_OWNER_STORE_PARTS
        if home.joinpath(*parts).is_dir()
    )


# LLM: 按规范布局列出数据根下已存在的 owner home；列目录出错时只返回已确认的本机主用户，不猜路径。
# 函数用途: 给 Full Access 沙箱枚举所有 owner home。
def _existing_owner_homes(agent_home_root: Path) -> tuple[Path, ...]:
    owners = agent_home_root / "owners"
    main = owners / "local" / "main"
    try:
        providers = sorted(
            path for path in owners.glob("providers/*/*/*")
            if path.parent.name in {"users", "groups"} and path.is_dir()
        )
    except OSError:
        providers = []
    return ((main,) if main.is_dir() else ()) + tuple(providers)


# LLM: 用户显式声明的工作目录（宿主写入 conversation_execution_cwd /
#   conversation_runtime_workspace_roots，或已授权的 allowed_write_roots）是结构化事实，
#   可以下发成子代理工作根与工具执行根；但文件系统根级目录、以及 my-agent 自己的运行记录区
#   （runs/agents/data/logs/其它 owner 的家）永远不是工作目录。
#   判据只用归一化路径，不读 goal 文字、不读模型自报的产物路径；无法解析的输入按"没有事实"丢弃。
#   根级目录按字面和解析后两种形态一起排除：merged-/usr 系统上 /bin、/sbin 是指向 /usr/bin、/usr/sbin 的符号链接，
#   归一化后不再等于常量本身（2026-09-25 ubuntu-24.04 CI 发现）。
# 函数用途: 把"声明过或已授权的目录"过滤成可以继承给子代理和工具执行层的可信工作根。
def inheritable_declared_work_roots(
    raw_roots: object,
    *,
    owner_home: object = "",
) -> list[str]:
    home = _normalized_root(owner_home)
    agent_home = agent_home_root_for_owner(home) if home is not None else None
    roots: list[str] = []
    for raw in raw_roots if isinstance(raw_roots, (list, tuple)) else ():
        path = _normalized_root(raw)
        if path is None:
            continue
        text = str(path)
        if text in _uninheritable_root_forms():
            continue
        if agent_home is not None and _is_relative_to(path, agent_home):
            if home is None or not _is_relative_to(path, home):
                # 数据根里只有这个 owner 自己的家算工作区；其余是宿主控制面。
                continue
        if text not in roots:
            roots.append(text)
    return roots


# LLM: "墙外已授权工作根"是宿主事实：只从写边界的 allowed_write_roots / product_write_roots 派生，
#   落在 owner 墙内的不算（墙内本来就走 owner 语义），系统根目录、宿主控制面、其它 owner 的家由
#   inheritable_declared_work_roots 过滤掉。工具 handler 的 owner 墙逃生口与创建子代理时的可见性预检共用这一份，
#   不认 workspace_roots，避免"改一个可变列表就放权"。owner_scope 为空（无 owner 墙）时返回空。
# 函数用途: 计算一次工具调用（或一个待创建子代理）可以穿过 owner 墙访问的已授权工作根。
def granted_external_work_roots(write_boundary: object, owner_scope: object) -> tuple[Path, ...]:
    if not str(owner_scope or "").strip() or not isinstance(write_boundary, dict):
        return ()
    owner_root = Path(str(owner_scope)).expanduser().resolve(strict=False)
    values: list[object] = []
    for key in ("allowed_write_roots", "product_write_roots"):
        raw = write_boundary.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(raw)
    granted: list[Path] = []
    for item in inheritable_declared_work_roots(values, owner_home=owner_root):
        root = Path(str(item)).expanduser().resolve(strict=False)
        if root == owner_root or _is_relative_to(root, owner_root) or root in granted:
            continue
        granted.append(root)
    return tuple(granted)


# LLM: 唯一权威常量仍是 UNINHERITABLE_ROOT_DIRS；这里只补每个常量当前宿主上的解析形态，供归一化后的路径比对。
# 函数用途: 返回根级目录的字面与解析后两套字符串，避免符号链接（/bin→/usr/bin）绕过排除。
def _uninheritable_root_forms() -> frozenset[str]:
    forms = set(UNINHERITABLE_ROOT_DIRS)
    for item in UNINHERITABLE_ROOT_DIRS:
        try:
            forms.add(str(Path(item).resolve(strict=False)))
        except (OSError, RuntimeError):
            continue
    return frozenset(forms)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "DEFAULT_DANGEROUS_PATH_ROOTS",
    "DEFAULT_PATH_ACCESS_MODE",
    "HOST_CONFIG_HOME_PARTS",
    "HOST_CONFIG_OWNER_PARTS",
    "HOST_CONFIG_YAML_SEGMENTS",
    "HOST_CREDENTIAL_HOME_PARTS",
    "HOST_MANAGED_OWNER_STORE_PARTS",
    "HOST_SECRET_DIR_NAME",
    "HOST_STATE_LOCK_SUFFIX",
    "HOST_STATE_OWNER_DIRS",
    "HOST_STATE_OWNER_FILES",
    "HOST_STATE_OWNER_SQLITE",
    "HOST_STATE_TASK_PARTS",
    "PATH_ACCESS_MODE_FULL",
    "PATH_ACCESS_MODE_NORMAL",
    "PATH_SCOPE_FULL",
    "PATH_SCOPE_NORMAL",
    "PATH_SCOPE_OWNER_WALL",
    "UNINHERITABLE_ROOT_DIRS",
    "PathAccessDecision",
    "PathAccessPolicy",
    "SQLITE_SIDECAR_SUFFIXES",
    "TASK_ROOT_LAYOUT",
    "agent_home_root_for_owner",
    "effective_owner_scope_root",
    "granted_external_work_roots",
    "host_config_root_for_path",
    "host_credential_for_path",
    "host_readonly_paths",
    "host_state_for_path",
    "host_managed_store_dirs",
    "host_managed_store_for_path",
    "inheritable_declared_work_roots",
    "normalize_path_access_mode",
    "owner_home_containing",
    "path_scope_regime",
    "task_root_containing",
]
