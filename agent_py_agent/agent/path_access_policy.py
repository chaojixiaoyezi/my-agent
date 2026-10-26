# LLM: 路径裁决只有这一份实现；每个策略在构造时冻结宿主根，跨进程运输不得重新猜测。Darwin压缩投影不减少Python/Linux具体保护，动态来源不缓存。
# 模块用途: 判断目标是否处于当前 owner、已授权工作根或普通路径策略允许的范围，不读取文件内容。

from __future__ import annotations

import json
import math
import os
import platform
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlsplit

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
# 常量用途: 列出对模型工具不开放的宿主托管存储；私密交互密钥和加密材料与插件同样禁止直接读写，任何权限模式生效。
PRIVATE_INTERACTIONS_OWNER_PARTS: tuple[str, ...] = ("data", "private_interactions")
# Gateway/adapter 的服务队列是整机控制面，可能含多 owner 正文；即使物理放在 main 下，也不是管理员 Agent 的业务资料。
HOST_MANAGED_OWNER_STORE_PARTS: tuple[tuple[str, ...], ...] = (
    ("data", "plugins"), PRIVATE_INTERACTIONS_OWNER_PARTS, ("workspace", "runtime", "services"),
)
# 只读例外不拆整机服务隐藏根；Shell挂载体积及队列保护保持原声明。
ADMIN_READABLE_SERVICE_LOG_OWNER_PARTS: frozenset[tuple[str, ...]] = frozenset({
    ("workspace", "runtime", "services", "gateway", "gateway.log"),
    ("workspace", "runtime", "services", "gateway", "supervisor.log"),
})

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
#   （已从那里删掉，每条路径只在一处）。可被本任务工作目录穿透的任务树根（runs/、tasks/）不在这里，见
#   agent_core.tool_runtime_ledger._attach_owner_control_write_guards（B 类）。owner 根的 data/ 整个归 A 类（3a 2026-10-02 定）：
#   那里全是宿主状态（调度、决策、上下文校准、skill 提案与学习、产物备份、核验证据库、维护标记，data/plugins 另是 H2 宿主托管
#   存储），宿主以后还会往里加新东西；逐个列子目录是封闭清单，新目录默认没保护，违反“开放世界不靠写死清单”。模型确实该写的
#   路径只能作为显式例外列出（写明原因和用例），目前没有。agents/（旧子代理运行状态与派工报告；现在子代理工作区在
#   <任务根>/work/agents/ 下）不是任务树，归 A 类。注意：能力包核验记录在 <任务根>/data/，不是 owner 根的 data/。宿主自己的写入
#   （记忆工具、Curator、策略服务、维护、runtime 仓库）在宿主进程里，不经过模型工具，不受影响。
#   - 文件：权限、配额、保留、记忆、skill、工具策略，审计流水，记忆操作流水与候选；每个文件旁的 .lock 一并保护（抢锁会卡住宿主写入）。
#   - 目录：能力申请、临时授权、Compact、日志、审计、会话与事件库（workspace/runtime）、Curator 事务、记忆归档、缓存、回收站、
#     owner 级正式 skill（skills/ 与家目录根的 .agents/skills/；项目工作区里的 skills 不在 owner home 下，不受影响）。
#   - SQLite 库连 -wal/-shm/-journal 伴随文件一起保护：runtime.db（runtime_db.schema.RUNTIME_DB_FILENAME）。
# 常量用途: 列出 owner home 里对模型工具只读的宿主运行状态（文件、目录、SQLite 库）。
HOST_STATE_OWNER_FILES: tuple[tuple[str, ...], ...] = (
    ("permissions.json",), ("quota.json",), ("retention.json",), ("memory_policy.json",), ("skill_policy.json",),
    ("tool_policy.json",), ("owner_status.json",), ("audit_log.jsonl",), ("memory", "ops.jsonl"), ("memory", "candidates.jsonl"),
    # 治理基线只能由宿主更新；governance_policy.json和governance_reviews已分别由config/、data/整根保护。
    ("memory", "SOUL_ADMIN_BASE.md"), ("memory", "AGENTS_ADMIN_BASE.md"),
)
HOST_STATE_OWNER_DIRS: tuple[tuple[str, ...], ...] = (
    ("capability_requests",), ("temporary_grants",), ("compact",), ("logs",), ("audit",), ("workspace", "runtime"),
    ("data",), ("agents",),
    ("memory", "curator"), ("memory_archive",), ("cache",), ("trash",), ("skills",), (".agents", "skills"),
)
HOST_STATE_OWNER_SQLITE: tuple[str, ...] = ("runtime.db",)
SQLITE_SIDECAR_SUFFIXES: tuple[str, ...] = ("-wal", "-shm", "-journal")
HOST_STATE_LOCK_SUFFIX = ".lock"
# H2/H3 宿主托管文件的拒绝码：写边界（tooling/write_boundary）拒在这些码上时原码上报给模型，不再换成通用 WRITE_FORBIDDEN
#   （9b 二审建议，3a 定），模型能按码看到该改用哪个宿主工具（错误合同里的 recovery_hint）。
# 常量用途: 列出要原码透传的宿主托管文件拒绝码。
HOST_FILE_DENIAL_CODES: frozenset[str] = frozenset({
    "PATH_HOST_MANAGED_STORE_BLOCKED", "PATH_HOST_CONFIG_WRITE_BLOCKED", "PATH_HOST_STATE_WRITE_BLOCKED",
    "PATH_HOST_CREDENTIAL_BLOCKED", "PERSONA_WRITE_REQUIRES_TOOL", "PATH_AGENTS_SOURCE_PROTECTION_INVALID",
})
# H3：宿主给沙箱里跑的模型命令设的环境标记，值 "1" 表示这个进程在宿主命令沙箱里、宿主状态只读（任何模式）。只由
#   tooling/shell._subprocess_text_env 设（不进沙箱的宿主命令会去掉它）；my-agent CLI 按它把启动时的权限类失败换成结构化错误码
#   （cli/host_state_guard）。
# 常量用途: 命令沙箱里“宿主状态只读”的环境变量名。
HOST_STATE_READ_ONLY_ENV = "MY_AGENT_HOST_STATE_READ_ONLY"

# H3 任务内的宿主托管位置（ae 能力包块 4）：相对规范任务根的路径片段，模型工具只读。规范任务根与
#   conversation.workspace_paths.canonical_task_root 一致：owner home 下 runs/<日期>/<键>、tasks/<日期>/<名>、audits/<编号>
#   （守卫用例在 test_host_files_access）。data/pack_verification 是能力包核验的原件清单与核验账本。
# 常量用途: 列出规范任务根的布局与任务里对模型工具只读的宿主托管位置。
TASK_ROOT_LAYOUT: tuple[tuple[str, int], ...] = (("runs", 2), ("tasks", 2), ("audits", 1))
HOST_STATE_TASK_PARTS: tuple[tuple[str, ...], ...] = (("data", "pack_verification"),)
# 常量用途: POSIX ERE 的元字符，路径拼进正则前逐个转义（host_readonly_patterns）。
_ERE_SPECIAL = frozenset(".^$*+?()[]{}|\\")

# H3 凭据（3a 定）：宿主配置里存放密钥的位置，文件工具连读也拒；命令只拒写不拒读（读取不在 H3 范围，命令能读到凭据是已知边界）。相对数据根。
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


# LLM: 描述来自同一政策声明，新增托管项自动进入目录；本模块仍仅用标准库，兼容 SDK 字节分发。
# 函数用途: 提供 H2/H3/凭据匹配家族的只读目录，不检查存在或读取文件。
def host_path_descriptions() -> tuple[dict[str, object], ...]:
    families = (
        ("managed", "owner", HOST_MANAGED_OWNER_STORE_PARTS),
        ("config", "home", HOST_CONFIG_HOME_PARTS), ("config", "owner", HOST_CONFIG_OWNER_PARTS),
        ("state", "owner", (*HOST_STATE_OWNER_FILES, *HOST_STATE_OWNER_DIRS)),
        ("state", "owner", tuple((name + suffix,) for name in HOST_STATE_OWNER_SQLITE
                                 for suffix in ("", *SQLITE_SIDECAR_SUFFIXES))),
        ("state", "owner", tuple((*parts[:-1], parts[-1] + HOST_STATE_LOCK_SUFFIX) for parts in HOST_STATE_OWNER_FILES)),
        ("state", "task", HOST_STATE_TASK_PARTS), ("credential", "home", HOST_CREDENTIAL_HOME_PARTS),
    )
    paths = tuple({"kind": kind, "base": base, "parts": parts} for kind, base, groups in families for parts in groups)
    matches = ({"kind": "credential", "base": "home", "match": "config/*." + segment + "*"}
               for segment in sorted(HOST_CONFIG_YAML_SEGMENTS))
    return (*paths, *matches, {"kind": "credential", "base": "home", "match": "**/" + HOST_SECRET_DIR_NAME + "/**"})


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

    # LLM: 配置读取只在构造处发生；配置切换应重建策略，不原地改变已在执行的调用。agent_home_root 同 from_values：拿得到宿主解析出
    #   的数据根（home_paths.root）就传进来，不传才退回环境变量。
    # 函数用途: 从当前配置创建冻结路径策略。
    @classmethod
    def from_config(cls, config: object | None, *, agent_home_root: object = None) -> PathAccessPolicy:
        return cls.from_values(
            mode=getattr(config, "path_access_mode", DEFAULT_PATH_ACCESS_MODE),
            dangerous_roots=getattr(config, "path_dangerous_roots", DEFAULT_DANGEROUS_PATH_ROOTS),
            agent_home_root=agent_home_root,
        )

    # LLM: 当前用户 home 过滤与数据根豁免必须由宿主计算一次；直接恢复协议字段不得再次应用此环境归一化。
    #   数据根的唯一权威是宿主启动时解析出的结果（home_paths：配置 my_agent_home 优先，其次环境变量），由构造处经
    #   agent_home_root 结构化传入（宿主 owner home 或写边界 canonical_owner_home_root 反推）；传了就不读进程环境变量。
    #   只有没有任何宿主事实时才按 owner 墙反推、再退回 MY_AGENT_HOME。两者不一致时（配置写了固定 home）若只看环境变量，
    #   H2/H3 的宿主文件判定会落到另一个目录上（9b 二审实测 Full Access 写进了 permissions.json 等）。
    # 函数用途: 规范化模式和危险根，并固定 owner 及数据根事实。
    @classmethod
    def from_values(
        cls,
        *,
        mode: object = DEFAULT_PATH_ACCESS_MODE,
        dangerous_roots: Iterable[object] | None = None,
        owner_scope_root: object = None,
        agent_home_root: object = None,
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
        data_root = _normalized_root(agent_home_root) if agent_home_root else None
        if data_root is None:
            data_root = _home_root_from_owner_scope(scope) if scope else _my_agent_home_root()
        return cls(mode=normalized_mode, dangerous_roots=roots, owner_scope_root=scope, agent_home_root=data_root)

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
                f"宿主托管存储（插件、私密交互、整机服务队列）不对模型工具开放，请使用对应宿主工具: target={resolved}",
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
        return (_host_write_block(resolved, self.agent_home_root)
                or persona_write_decision(resolved, self.agent_home_root, self.owner_scope_root)
                or PathAccessDecision(True))

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
            return self._owner_wall_decision(resolved, home_root)
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

    # LLM: owner 墙分支从 _mode_decision 拆出（只为降低嵌套，判定与顺序不变）：自己 home 放行；数据根里按 _owner_scope_decision
    #   分 shared/控制面/别人的家，放行后再拒凭据文件名；数据根以外一律 PATH_OWNER_SCOPE_BLOCKED。
    # 函数用途: 隔离 owner 的路径裁决。
    def _owner_wall_decision(self, resolved: Path, home_root: Path | None) -> PathAccessDecision:
        assert self.owner_scope_root is not None
        # WorkspaceOnly 的唯一硬边界就是 owner home。用户自己的文件（包含项目自用
        # .env/认证配置）不再被第二层文件名 denylist 误伤；凭据仍不会被日志主动输出，
        # 也不能借此跨到其他 owner 或宿主目录。
        if _is_relative_to(resolved, self.owner_scope_root):
            return PathAccessDecision(True)
        if home_root is None or not _is_relative_to(resolved, home_root):
            return PathAccessDecision(
                False,
                "PATH_OWNER_SCOPE_BLOCKED",
                f"当前用户只能访问自己的数据目录和 shared 公共能力区: target={resolved}",
                str(self.owner_scope_root),
            )
        decision = self._owner_scope_decision(resolved, home_root)
        if decision.allowed and _is_credential_filename(resolved.name):
            return _credential_file_decision(resolved)
        return decision

    # LLM: H2（2026-10-01）：宿主托管存储的唯一声明是本模块的 HOST_MANAGED_OWNER_STORE_PARTS；这里只用构造时冻结的
    #   agent_home_root 做路径运算（不读文件、不读环境），数据根未知时返回 None。check 与目录遍历类工具（list/find）共用它。
    # 函数用途: 返回路径所在的宿主托管存储根；不在其中返回 None。
    def host_managed_store(self, path: str | Path) -> Path | None:
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return None
        return self._host_managed_store_of(resolved)

    # LLM: 管理员身份及regular日志形状由同一helper核验；只豁免H2，H3写门及services整体隐藏不变。
    # 函数用途: 为Full/Auto/Ask管理员开放规范日志只读，其他owner仍由原托管存储门拒绝。
    def _host_managed_store_of(self, resolved: Path) -> Path | None:
        if self.agent_home_root is None:
            return None
        if self._is_admin_readable_service_log(resolved):
            return None
        return host_managed_store_for_path(resolved, self.agent_home_root)

    # LLM: 冻结owner墙必须为空或casefold精确等于local/main；再核home白名单及单链接regular元数据，不授跨租户或写权。
    # 函数用途: 在唯一读例外中识别Full/Auto/Ask管理员，排除其他本地/Provider用户及所有队列、目录、链接别名。
    def _is_admin_readable_service_log(self, resolved: Path) -> bool:
        if self.agent_home_root is None:
            return False
        main = self.agent_home_root / "owners" / "local" / "main"
        if self.owner_scope_root is not None and _relative_folded(self.owner_scope_root, main) != ():
            return False
        if _relative_folded(resolved, main) not in ADMIN_READABLE_SERVICE_LOG_OWNER_PARTS:
            return False
        try:
            info = resolved.stat()
        except OSError:
            return False
        return stat.S_ISREG(info.st_mode) and info.st_nlink == 1

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
        policy = external_policy or PathAccessPolicy.from_values(
            mode=self.mode, dangerous_roots=self.dangerous_roots, agent_home_root=self.agent_home_root)
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


# LLM: 写边界里宿主写的 canonical_owner_home_root（tool_runtime_ledger._attach_canonical_owner_home_root，来自 home_paths）是本次
#   调用的数据根来源；没有或推不出时返回 None，调用方不改策略的数据根。只做路径运算。
# 函数用途: 从写边界取宿主解析出的数据根。
def data_root_from_boundary(boundary: object) -> Path | None:
    owner_home = boundary.get("canonical_owner_home_root") if isinstance(boundary, dict) else None
    return agent_home_root_for_owner(owner_home) if owner_home else None


# LLM: 识别规范 owners/local/<uid>（含 main）、local/users/<uid>及 providers/<p>/<users|groups>/<id>，
#   只做路径运算、不读文件；调用方传已 resolve 的路径与数据根。改布局时两处一起改（守卫用例在 test_host_managed_store_access）。
#   比较大小写无关（见 _relative_folded），返回值取输入路径自己的写法。
# 函数用途: 从规范布局反推一个路径所在的 owner home；不在任何 owner home 里返回 None。
def owner_home_containing(path: Path, agent_home_root: Path) -> Path | None:
    parts = _relative_folded(path, agent_home_root / "owners")
    if parts is None:
        return None
    depth = 0
    if len(parts) >= 3 and parts[:2] == ("local", "users"):
        depth = 3
    elif len(parts) >= 2 and parts[0] == "local" and parts[1]:
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


# LLM: H3：给命令沙箱列 owner 级的只读覆盖。owner_home 给出时只列这个 owner 的 config/ 和运行状态文件（数据根的配置本来就在
#   owner 墙外）；为空时列数据根的 config/、system/config/ 和全部已存在 owner home 的。任务里的宿主托管位置不在这里：
#   它们只按写边界的结构化 task_root 由 registry_invoke 交给沙箱（3a 定：不从工作目录或写根反推）。不按存在过滤：macOS 对
#   还不存在的路径也能拒写，Linux 由挂载层只挂已存在的。
# 函数用途: 列出要对模型命令设成只读的 owner 级宿主托管路径。
def host_readonly_paths(agent_home_root: Path, owner_home: Path | None = None) -> tuple[Path, ...]:
    homes = (owner_home,) if owner_home is not None else _existing_owner_homes(agent_home_root)
    return _readonly_from_homes(agent_home_root, homes, include_home=owner_home is None)


# LLM: 纯投影共享原声明，不枚举owner、不按存在性删掉尚未创建的保护路径。
# 函数用途: 从本次owner集合生成宿主只读覆盖。
def _readonly_from_homes(agent_home_root: Path, homes: tuple[Path, ...], *, include_home: bool) -> tuple[Path, ...]:
    home_parts = HOST_CONFIG_HOME_PARTS if include_home else ()
    paths = [*(agent_home_root.joinpath(*parts) for parts in home_parts),
             *(home.joinpath(*parts) for home in homes for parts in (*HOST_CONFIG_OWNER_PARTS, *_owner_state_parts()))]
    return tuple(dict.fromkeys(paths))


# LLM: 写边界里宿主写的结构化 task_root（当前任务；子代理是它所属任务的 task_workspace_dir）→ 任务里的宿主托管位置
#   （HOST_STATE_TASK_PARTS）。只做路径拼接；registry_invoke 用它把本任务的位置交给命令沙箱，不依赖命令的工作目录在哪
#   （ae 块 4 发现的缺口，3a 定口径）。
# 函数用途: 列出一个任务根里对模型只读的宿主托管位置。
def task_host_state_paths(task_root: Path) -> tuple[Path, ...]:
    return tuple(task_root.joinpath(*parts) for parts in HOST_STATE_TASK_PARTS)


# LLM: 同一份任务布局（TASK_ROOT_LAYOUT × HOST_STATE_TASK_PARTS）的正则投影，给 macOS Seatbelt 盖住 owner home 下全部规范任务根
#   里的宿主托管位置：别的任务的、还没建出来的都算，不逐个列路径（9b 二审建议，3a 定）。owner 集合与 host_readonly_paths 相同
#   （隔离只本 owner，Full Access 全部已有 owner）。POSIX ERE，owner home 先解析符号链接再按字面转义；Seatbelt 在不区分大小写的
#   卷上按不区分大小写匹配（本机实测 RUNS/、DATA/、Pack_Verification 变体都拦住）。Linux bwrap 只能挂已存在的路径，不用它：Linux
#   只保护本任务（registry 按 task_root 给的路径），已知边界。改布局时同步 test_host_files_access.py。
# directory_operation 将目录类型与写操作分开：runs/audits 由宿主建根；业务 tasks 可新建，但不能挪走核验记录的祖先。
# 函数用途: 从原任务布局生成记录拒写或目录操作保护，不把同层业务文件当作目录。
def host_readonly_patterns(agent_home_root: Path, owner_home: Path | None = None, *, directory_operation: str = "") -> tuple[str, ...]:
    homes = (owner_home,) if owner_home is not None else _existing_owner_homes(agent_home_root)
    return _patterns_from_homes(homes, directory_operation=directory_operation)


# LLM: 复用原Seatbelt正则叶函数；只投影本次冻结的homes，不重新枚举或缓存权限结论。
# 函数用途: 生成指定owner的原拒写/目录保护正则。
def _patterns_from_homes(homes: tuple[Path, ...], *, directory_operation: str = "") -> tuple[str, ...]:
    return tuple(pattern for home in homes for pattern in _task_record_patterns(_ere_literal(str(home.resolve(strict=False))), directory_operation=directory_operation))


# LLM: 三类正则，前缀都是已转义的 owner home：
#   1. 核验记录本身及其下内容；
#   2. 布局各级目录本身（runs、runs/<日期>、runs/<日期>/<键>、audits、audits/<编号> 等，不含其下内容）；
#   3. 任务根到记录之间的目录本身（data）。
#   2、3 必须结合实际目录 vnode；runs/audits 拒写，业务 tasks 只拒删除/改名，里面的普通读写照常。9b 三审实测：只有第 1 类时，命令能把别的任务的 data/、
#   日期目录、整个 runs/ 或 audits/ 改名，写进记录再改回来，伪造它的核验记录（7 种绕过）；加上 2、3 后全拦。副作用：模型的命令
#   不能自己新建 runs/audits 宿主任务根；可按已授权的业务 tasks 创建目录。可选层级必须写成显式的 (/[^/]+)? 分组：Seatbelt
#   不认 {m,n} 区间，会静默失效（9b 实测）。
# 函数用途: 给一个 owner home 生成核验记录、布局目录和中间目录三类拒写正则。
def _task_record_patterns(home: str, *, directory_operation: str = "") -> list[str]:
    if directory_operation not in {"", "write", "unlink"}:
        raise ValueError("未知任务目录保护操作")
    selected = [item for item in TASK_ROOT_LAYOUT if not directory_operation or
                ((item[0] == "tasks") == (directory_operation == "unlink"))]
    roots = "|".join(_ere_literal(name) + "/[^/]+" * depth for name, depth in selected)
    parts = "|".join("/".join(_ere_literal(part) for part in item) for item in HOST_STATE_TASK_PARTS)
    layout = [f"^{home}/{_ere_literal(name)}" + "(/[^/]+)?" * depth + "$" for name, depth in selected]
    between = ["/".join(_ere_literal(part) for part in item[:size]) for item in HOST_STATE_TASK_PARTS for size in range(1, len(item))]
    if directory_operation:
        return [*layout, *(f"^{home}/({roots})/{prefix}$" for prefix in dict.fromkeys(between))]
    return [f"^{home}/({roots})/({parts})(/|$)"]


# 函数用途: 把一段文字转成 POSIX ERE 里的字面匹配（给正则元字符加反斜杠）。
def _ere_literal(text: str) -> str:
    return "".join("\\" + char if char in _ERE_SPECIAL else char for char in text)


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
            "宿主运行状态（runtime.db 等权威账本、Owner级 skills/ 目录、能力包核验记录）只由宿主写，模型工具不能改；"
            "项目/工作区普通 Skill 请写到 <workspace>/.agents/skills/<skill_id>/SKILL.md，固化脚本与工作流请用 scripts / workflows 工具"
            f"（勿对普通 Skill/工作流调 package_build），任务与运行状态经对应宿主工具改变，读取不受影响: target={resolved}", str(state))
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
    return _hidden_from_homes(homes)


# LLM: 仅检查声明托管目录是否存在，保持旧符号链接/目录语义，不枚举owner。
# 函数用途: 给同一批owner生成隐藏目录投影。
def _hidden_from_homes(homes: tuple[Path, ...]) -> tuple[Path, ...]:
    return tuple(
        home.joinpath(*parts) for home in homes for parts in HOST_MANAGED_OWNER_STORE_PARTS
        if home.joinpath(*parts).is_dir()
    )


# LLM: 仅一次命令准备内复用，冻结值不承载授权、无跨调用TTL；旧API复用同一组叶函数。
# 类用途: 保存本次沙箱的只读、隐藏和macOS目录保护路径。
@dataclass(frozen=True)
class HostSandboxPaths:
    readonly: tuple[Path, ...] = ()
    hidden: tuple[Path, ...] = ()
    patterns: tuple[str, ...] = ()
    directory_write_patterns: tuple[str, ...] = ()
    directory_unlink_patterns: tuple[str, ...] = ()


# LLM: Linux/通用调用保留完整具体路径；Darwin无owner时改用规范布局正则，覆盖未来owner且不枚举目录。
#   已知owner仍仅投影本owner。Python H2/H3、Linux挂载和人格来源的独立复核不依赖此压缩结果。
# 函数用途: 按平台生成保护集合，避免Seatbelt随历史owner数量膨胀，不缓存权限或目录。
def host_sandbox_paths(agent_home_root: Path, owner_home: Path | None = None, system: str | None = None) -> HostSandboxPaths:
    mac = (system or platform.system()).casefold() == "darwin"
    if mac and owner_home is None:
        return _macos_all_owner_paths(agent_home_root)
    homes = (owner_home,) if owner_home is not None else _existing_owner_homes(agent_home_root)
    return HostSandboxPaths(
        readonly=_readonly_from_homes(agent_home_root, homes, include_home=owner_home is None),
        hidden=_hidden_from_homes(homes),
        patterns=_patterns_from_homes(homes) if mac else (),
        directory_write_patterns=_patterns_from_homes(homes, directory_operation="write") if mac else (),
        directory_unlink_patterns=_patterns_from_homes(homes, directory_operation="unlink") if mac else (),
    )


# LLM: 只按规范布局生成POSIX ERE，不枚举或读取owner；root按字面转义，local/users必须先于短布局匹配。
# 函数用途: 返回可覆盖已有及未来四种owner布局的锚定前缀。
def macos_owner_prefix(agent_home_root: Path) -> str:
    # POSIX ERE没有前瞻；逐前缀分支排除完整的users容器名，仍允许u/us/user/users2等owner ID。
    local_id = r"([^u/][^/]*|u|u[^s/][^/]*|us|us[^e/][^/]*|use|use[^r/][^/]*|user|user[^s/][^/]*|users[^/]+)"
    return re.escape(str(agent_home_root.resolve(strict=False) / "owners")) + rf"/(local/users/[^/]+|local/{local_id}|providers/[^/]+/(users|groups)/[^/]+)"


# LLM: H2声明的regex投影只增加隐藏，不代替Python裁决或Linux具体挂载；调用者必须放在读allow之后。
# 函数用途: 生成当前owner或全owner托管存储的拒读写模式，不检查存在性。
def macos_hidden_pattern(agent_home_root: Path, owner_home: Path | None = None) -> str:
    prefix = re.escape(str(owner_home.resolve(strict=False))) if owner_home is not None else macos_owner_prefix(agent_home_root)
    parts = "|".join(re.escape("/".join(item)) for item in HOST_MANAGED_OWNER_STORE_PARTS)
    return f"^{prefix}/({parts})(/|$)"


# LLM: 每个原子保留完整相对路径；同类锁/SQLite尾缀共用组，不重复展开，禁止删保护项换取字节达标。
# 函数用途: 从唯一H3声明生成可独立拆分的保护分支。
def _macos_readonly_atoms() -> list[tuple[str, str]]:
    plain = [(re.escape("/".join(item)), "") for item in (*HOST_CONFIG_OWNER_PARTS, *HOST_STATE_OWNER_DIRS)]
    files = [(re.escape("/".join(item)), f"({re.escape(HOST_STATE_LOCK_SUFFIX)})?") for item in HOST_STATE_OWNER_FILES]
    suffix = "(" + "|".join(re.escape(item) for item in SQLITE_SIDECAR_SUFFIXES) + ")?"
    return [*plain, *files, *((re.escape(name), suffix) for name in HOST_STATE_OWNER_SQLITE)]


# LLM: 只在同一deny内合并后缀；使用POSIX ERE普通组，保持开头与目录/文件边界，不引入前瞻或非捕获组。
# 函数用途: 为一个分支批次构造完整的只读模式。
def _macos_readonly_chunk(prefix: str, atoms: list[tuple[str, str]]) -> str:
    groups: dict[str, list[str]] = {}
    for name, suffix in atoms:
        groups.setdefault(suffix, []).append(name)
    parts = [f"({'|'.join(names)}){suffix}" if suffix else "|".join(names) for suffix, names in groups.items()]
    return f"^{prefix}/({'|'.join(parts)})(/|$)"


# LLM: 最终JSON/SBPL字面量严格小于请求预算及768B；根须显式，单原子不可表示时失败关闭，不能跳过保护。
# 函数用途: 按含引号与转义的字节预算切分H3拒写正则，不枚举owner；只有此复数接口维护投影。
def macos_readonly_patterns(root: Path | None = None, owner_home: Path | None = None, *, max_pattern_bytes: int = 700) -> tuple[str, ...]:
    if root is None and owner_home is None:
        raise ValueError("macOS protection requires an explicit root")
    prefix = re.escape(str(owner_home.resolve(strict=False))) if owner_home is not None else macos_owner_prefix(root)
    budget = min(max_pattern_bytes, 768)
    patterns, chunk = [], []
    for atom in _macos_readonly_atoms():
        candidate = _macos_readonly_chunk(prefix, [*chunk, atom])
        if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) >= budget:
            if chunk:
                patterns.append(_macos_readonly_chunk(prefix, chunk))
            chunk = [atom]
            candidate = _macos_readonly_chunk(prefix, chunk)
            if len(json.dumps(candidate, ensure_ascii=False).encode("utf-8")) >= budget:
                raise ValueError("macOS protection root exceeds SBPL literal budget")
        else:
            chunk.append(atom)
    if chunk:
        patterns.append(_macos_readonly_chunk(prefix, chunk))
    return tuple(patterns)


# LLM: 仅压缩Darwin投影；声明仍留在H2/H3原常量，任务目录保持vnode类型及unlink/write分界。
# 函数用途: 用固定数量规则保护全owner宿主状态、人格默认名称和祖先，Linux具体路径不受影响。
def _macos_all_owner_paths(root: Path) -> HostSandboxPaths:
    prefix = macos_owner_prefix(root)
    owners = re.escape(str(root.resolve(strict=False) / "owners"))
    ancestors = (f"^{owners}(/local(/users)?|/providers(/[^/]+(/(users|groups))?)?)?$",
                 f"^{prefix}(/(memory|workspace|\\.agents))?$")
    return HostSandboxPaths(
        readonly=_readonly_from_homes(root, (), include_home=True),
        patterns=(*macos_readonly_patterns(root), *_task_record_patterns(prefix)),
        directory_write_patterns=(*_task_record_patterns(prefix, directory_operation="write"), *ancestors),
        directory_unlink_patterns=tuple(_task_record_patterns(prefix, directory_operation="unlink")),
    )


# LLM: 输入复用本次人格快照的owner，不另扫owners；缓存只限本次父节点解析，不能跨调用缓存权限或链接目标。
#   普通路径交给regex，只有偏离规范路径的符号链接目标保留精确覆盖，否则压缩会丢掉原H2/H3的realpath保护。
# 函数用途: 找出宿主保护位置的实际别名，分别返回只读和完全隐藏的目标。
def macos_host_alias_paths(homes: tuple[Path, ...]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    readonly, hidden = set(), set()
    for home in homes:
        resolved = {home: home.resolve(strict=False)}
        groups = ((*HOST_CONFIG_OWNER_PARTS, *_owner_state_parts()), HOST_MANAGED_OWNER_STORE_PARTS)
        for parts, output in ((groups[0], readonly), (groups[1], hidden)):
            for item in parts:
                original = home.joinpath(*item)
                actual = _macos_resolve_owner_path(home, item, resolved)
                if actual != original:
                    output.add(actual)
    return tuple(sorted(readonly)), tuple(sorted(hidden))


# LLM: 每个中间节点仅本次检查一次链接；链接用原resolve语义，普通节点不从根逐层反复lstat。
# 函数用途: 用本次父路径缓存解析一个保护位置，保留中间目录的符号链接目标。
def _macos_resolve_owner_path(home: Path, parts: tuple[str, ...], resolved: dict[Path, Path]) -> Path:
    path = home
    for part in parts:
        original = path / part
        if original not in resolved:
            candidate = resolved[path] / part
            resolved[original] = candidate.resolve(strict=False) if candidate.is_symlink() else candidate
        path = original
    return resolved[path]


# LLM: 通用/Linux具体覆盖保留原main和providers，并包含local用户及local/users布局；Darwin固定投影不调用它。
# 函数用途: 为需要具体路径的调用枚举四种已存在owner，目录异常时保留已确认main，不猜路径。
def _existing_owner_homes(agent_home_root: Path) -> tuple[Path, ...]:
    owners = agent_home_root / "owners"
    main = owners / "local" / "main"
    try:
        local = sorted(path for path in owners.glob("local/*") if path.name != "users" and path.is_dir())
        local.extend(sorted(path for path in owners.glob("local/users/*") if path.is_dir()))
        providers = sorted(
            path for path in owners.glob("providers/*/*/*")
            if path.parent.name in {"users", "groups"} and path.is_dir()
        )
    except OSError:
        local, providers = [], []
    return tuple(dict.fromkeys((*((main,) if main.is_dir() else ()), *local, *providers)))


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
    "HOST_FILE_DENIAL_CODES",
    "HOST_MANAGED_OWNER_STORE_PARTS",
    "HOST_SECRET_DIR_NAME",
    "HOST_STATE_LOCK_SUFFIX",
    "HOST_STATE_OWNER_DIRS",
    "HOST_STATE_OWNER_FILES",
    "HOST_STATE_OWNER_SQLITE",
    "HOST_STATE_READ_ONLY_ENV",
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
    "data_root_from_boundary",
    "effective_owner_scope_root",
    "granted_external_work_roots",
    "host_config_root_for_path",
    "host_credential_for_path",
    "host_readonly_paths",
    "host_readonly_patterns",
    "host_state_for_path",
    "host_managed_store_dirs",
    "host_managed_store_for_path",
    "inheritable_declared_work_roots",
    "normalize_path_access_mode",
    "owner_home_containing",
    "path_scope_regime",
    "task_host_state_paths",
    "task_root_containing",
]


# LLM: 这些常量仅约束人格来源的拒写投影，不能替代来源仓库或授予读写权限。
# 模块用途: 为现有路径门扩展同owner来源保护，原凭据和整机服务队列规则保持原样。
AGENTS_PROTECTION_CONFIG_MAX_BYTES = 512 * 1024
_AGENTS_PROTECTION_KEYS = frozenset({"schema_version", "owner", "kind", "value", "revision", "updated_by", "updated_at", "identity"})
_PERSONA_PROTECTION_NAMES = ("SOUL.md", "USER.md", "AGENTS.md")

# LLM: 投影不可用不等于来源未配置；调用方必须拒写或拒绝启动，不能返回空保护列表。
# 类用途: 表示来源保护无法安全收窄，等待宿主修复配置或恢复严格文件读取能力。
class AgentsSourceProtectionError(ValueError):
    pass


# LLM: 原拒写投影只保存本次已核验事实，不提供配置/CAS或正文；persona条目的configuration冻结原权威record，空tuple表示缺配置。
# 类用途: 在一次平台投影中保存原名称、必需来源身份及原控制记录，供返回前只读复核。
@dataclass(frozen=True)
class PersonaProtectedPath:
    path: Path
    identity: str | None = None
    configuration: tuple[tuple[str, object], ...] | None = None


# LLM: 显式人格根是宿主事实；插件工厂把cwd写入owner_home时，仅从规范宿主插件库布局反推所属owner，不猜任意cwd。
# 函数用途: 为平台沙箱选取真实人格根，非owner的旧只读环境保持原权限合同。
def persona_sandbox_root(owner_home: Path, explicit_root: Path | None) -> Path | None:
    if explicit_root is not None:
        return Path(explicit_root)
    home = Path(owner_home)
    root = agent_home_root_for_owner(home)
    canonical = owner_home_containing(home, root) if root is not None else None
    if canonical == home:
        return home
    if canonical is not None and host_managed_store_for_path(home, root) is not None:
        return canonical
    if any((home / name).exists() for name in (*_PERSONA_PROTECTION_NAMES, "persona")):
        return home
    return None


# LLM: 每段都用父FD的nofollow stat/open/fstat复核；目录用search/metadata FD而非读取目录数据，本SDK只依赖stdlib。
# 函数用途: 安全打开一个路径节点并拒绝符号链接、文件多链接及节点身份替换。
def _protection_open_node(parent: int, name: str, *, directory: bool) -> int:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    if directory:
        flags = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY)) | os.O_NOFOLLOW | os.O_DIRECTORY
    descriptor = os.open(name, flags, dir_fd=parent)
    try:
        actual = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent, follow_symlinks=False)
        kind_ok = stat.S_ISDIR(actual.st_mode) if directory else stat.S_ISREG(actual.st_mode) and actual.st_nlink == 1
        if not kind_ok or not os.path.samestat(before, actual) or not os.path.samestat(after, actual):
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: unsafe node")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


# LLM: 配置有界、来源只读身份，目录仅search/metadata；缺strict能力不能普通open降级，所有FD在异常时关闭。
# 函数用途: 返回严格路径链身份摘要及可选有界配置原字节，保持业务解析权威不变。
def _protection_file(path: Path, *, read_config: bool = False) -> tuple[bytes, str]:
    parent = descriptor = None
    try:
        if (not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
                or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
                or os.stat not in os.supports_follow_symlinks):
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: no strict FD support")
        directory_flags = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY)) | os.O_NOFOLLOW | os.O_DIRECTORY
        parent = os.open(path.anchor, directory_flags)
        info = os.fstat(parent)
        chain = [(info.st_dev, info.st_ino)]
        for part in path.parts[1:-1]:
            child = _protection_open_node(parent, part, directory=True)
            os.close(parent)
            parent = child
            info = os.fstat(parent)
            chain.append((info.st_dev, info.st_ino))
        descriptor = _protection_open_node(parent, path.name, directory=False)
        info = os.fstat(descriptor)
        chain.append((info.st_dev, info.st_ino))
        raw = _protection_read_config(descriptor) if read_config else b""
        # 最后按整条原定位复核身份，不能因打开FD后目录被替换而解除原名称的保护。
        current = [(item.lstat().st_dev, item.lstat().st_ino) for item in (*reversed(path.parents), path)]
        if current != chain:
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: changed chain")
        return raw, sha256(json.dumps(chain, separators=(",", ":")).encode()).hexdigest()
    except (OSError, NotImplementedError, RuntimeError) as error:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: unsafe path") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent is not None:
            os.close(parent)


# LLM: 重复JSON键不能由后值覆盖前值；此处仅验证保护需要的小型投影，不供应配置CAS或来源正文。
# 函数用途: 为保护层拒绝含重复键的控制记录。
def _protection_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    record: dict[str, object] = {}
    for key, value in pairs:
        if key in record:
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: duplicate key")
        record[key] = value
    return record


# LLM: owner身份只从可信home规范布局反推，不能接受配置自报的owner作为路径授权。
# 函数用途: 返回保护投影期望的canonical owner ID，不符合布局时明确拒绝。
def _protection_owner_id(home: Path) -> str:
    root = agent_home_root_for_owner(home)
    if root is None or owner_home_containing(home, root) != home:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: owner layout")
    return home.relative_to(root / "owners").as_posix()


# LLM: 定位仅用于增加拒写；绝对规范路径、无..、跨owner/控制根拒绝，不展开环境变量或用户输入别名。
# 函数用途: 验证本地保护定位并保持配置的原始路径名称。
def _protection_locator(value: object, home: Path) -> Path:
    if type(value) is not str or not value or value.strip() != value or "\x00" in value or "\\" in value:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: locator")
    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: locator")
    root = agent_home_root_for_owner(home)
    if root is not None and _is_relative_to(path, root) and not _is_relative_to(path, home):
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: cross owner")
    return path


# LLM: 文件门/SDK原tuple[Path]合同不变；配置仍由下方同一只读投影验证，不加载正文/网络或改变配置/CAS。
# 函数用途: 给原调用方返回当前owner人格控制域与任意名本地来源的拒写路径。
def persona_protected_paths(owner_home: Path) -> tuple[Path, ...]:
    return tuple(item.path for item in _persona_protection_snapshot(owner_home))


# LLM: AgentsSourceRepository仍为唯一来源权威；按原读取/验证合同冻结完整record及identity，不再签发CAS或读取正文/网络。
#   配置源失踪不能降为默认缺失路径，默认AGENTS与配置定位重叠时也保留配置identity。
# 函数用途: 生成同一次保护投影的原名称、必需身份与本次权威控制记录快照，不另建业务配置系统。
def _persona_protection_snapshot(owner_home: Path) -> tuple[PersonaProtectedPath, ...]:
    home = Path(owner_home).expanduser().absolute()
    paths = {path: PersonaProtectedPath(path) for path in
             (*(home / name for name in _PERSONA_PROTECTION_NAMES), home / "persona")}
    config = home / "persona/agents_source.json"
    try:
        record = _protection_configuration(config, home)
        paths[config.parent] = PersonaProtectedPath(
            config.parent, configuration=tuple(sorted(record.items())) if record is not None else ())
        if record is None:
            return tuple(paths.values())
        if record["kind"] == "url":
            _protection_url(record)
            return tuple(paths.values())
        locator = _protection_locator(record["value"], home)
        if record["kind"] == "owner_default" and locator != home / "AGENTS.md":
            raise ValueError("default")
        _, actual_identity = _protection_file(locator)
        if record["identity"] != actual_identity:
            raise ValueError("identity changed")
        paths[locator] = PersonaProtectedPath(locator, actual_identity)
        return tuple(paths.values())
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: cannot safely project source") from error


# LLM: 平台使用前/返回前都复核原完整nofollow身份链，不能因已保存canonical字符串而接受换inode、失踪或symlink。
#   FD仅在本次验证内打开并关闭；这不是从argv构造直到bwrap内核bind的FD固定保证。
# 函数用途: 按同一原身份算法失败关闭已配置来源的后续身份变化。
def validate_persona_source_identity(path: Path, identity: str) -> None:
    _, actual = _protection_file(path)
    if actual != identity:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: source identity changed")


# LLM: 复用原权威配置读取合同，只比较冻结record含原revision；坏JSON、切源、增代或缺配置变化都拒绝，不刷新旧值/另建CAS。
#   同一record的JSON排版不是新代次；此只读复核不保证返回后至内核bind无竞争，不触及许可或持久配置。
# 函数用途: 在平台保护返回前复核本次配置代次，配置变化不能因旧来源identity仍有效而漏检。
def validate_persona_configuration(item: PersonaProtectedPath) -> None:
    if item.configuration is None:
        return
    try:
        record = _protection_configuration(item.path / "agents_source.json", item.path.parent)
        current = tuple(sorted(record.items())) if record is not None else ()
        if current != item.configuration:
            raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: configuration changed")
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: cannot revalidate configuration") from error


# LLM: 保护路径包括控制域子树、源文件、原定位祖先和实际inode别名；不将整个业务workspace按子树拒写。
# 函数用途: 判断候选是否影响一个保护路径，目录祖先只拦节点自身而不拦正常同级业务文件。
def persona_path_is_protected(candidate: Path, protected: tuple[Path, ...]) -> bool:
    resolved = Path(candidate).expanduser().resolve(strict=False)
    for original in protected:
        target = original.resolve(strict=False)
        if (_relative_folded(resolved, target) is not None or _relative_folded(target, resolved) is not None):
            return True
        if _protection_same_inode(resolved, target):
            return True
    return False


# LLM: 原平台调用方的路径tuple保持不变；同一快照投影只增加拒写，不授予读权或改变配置。
# 函数用途: 返回当前owner或Full Access全部owner的原保护路径列表。
def persona_process_protected_paths(owner_home: Path, *, all_owners: bool = False) -> tuple[Path, ...]:
    return tuple(item.path for item in persona_process_protection_snapshot(owner_home, all_owners=all_owners))


# LLM: 与原文件门/平台使用同一严格owner枚举和配置投影，只额外携带已核验identity，不加载正文/网络或持久副作用。
# 函数用途: 为一次进程沙箱返回保护路径与必需身份，供使用前和返回前复核。
def persona_process_protection_snapshot(owner_home: Path, *, all_owners: bool = False) -> tuple[PersonaProtectedPath, ...]:
    home = Path(owner_home)
    root = agent_home_root_for_owner(home)
    homes = (home,)
    if all_owners and root is not None:
        homes = tuple(dict.fromkeys((home, *_persona_owner_homes(root))))
    return tuple(dict.fromkeys(item for owner in homes for item in _persona_protection_snapshot(owner)))


# LLM: 与四种规范布局一致，local/users容器不能误当owner；沿原严格目录枚举拒绝symlink/IO错误，不能猜空集合。
# 函数用途: 为动态来源与实际别名严格枚举全部本地、用户和群owner，不缓存配置或身份。
def _persona_owner_homes(root: Path) -> tuple[Path, ...]:
    main = root / "owners/local/main"
    homes = [main] if main.is_dir() else []
    homes.extend(path for path in _persona_owner_directories(root / "owners/local") if path.name not in {"main", "users"})
    homes.extend(_persona_owner_directories(root / "owners/local/users"))
    for provider in _persona_owner_directories(root / "owners/providers"):
        for scope in ("users", "groups"):
            homes.extend(_persona_owner_directories(provider / scope))
    return tuple(homes)


# LLM: 缺目录表示尚未建立；symlink/非目录/读取错误不能按缺失处理，否则可漏掉另一个owner来源。
# 函数用途: 安全枚举一级owner布局目录，异常交给调用方失败关闭。
def _persona_owner_directories(directory: Path) -> tuple[Path, ...]:
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return ()
    if not stat.S_ISDIR(metadata.st_mode):
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: owner directory")
    children = tuple(directory.iterdir())
    if any(path.is_symlink() for path in children):
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: owner symlink")
    return tuple(path for path in children if path.is_dir())


# LLM: 配置或全owner枚举不可读时不能猜任意文件名并放行；只拒写且无配置持久副作用。
# 函数用途: 为通用路径门返回人格或来源拒写结果，普通业务路径返回None。
def persona_write_decision(path: Path, agent_home_root: Path, owner_home: Path | None = None) -> PathAccessDecision | None:
    try:
        homes = (owner_home,) if owner_home is not None else _persona_owner_homes(agent_home_root)
    except (AgentsSourceProtectionError, OSError, RuntimeError):
        return PathAccessDecision(False, "PATH_AGENTS_SOURCE_PROTECTION_INVALID", "无法安全枚举人格保护范围", str(agent_home_root))
    for home in homes:
        decision = _persona_home_write_decision(path, home)
        if decision is not None:
            return decision
    return None


# LLM: 已有严格FD；最多512KiB+1判断超限，不无界读，不静默裁剪，返回只读保护投影的原字节。
# 函数用途: 有界读回来源控制记录。
def _protection_read_config(descriptor: int) -> bytes:
    if os.fstat(descriptor).st_size > AGENTS_PROTECTION_CONFIG_MAX_BYTES:
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: oversized config")
    raw = b""
    while len(raw) <= AGENTS_PROTECTION_CONFIG_MAX_BYTES:
        chunk = os.read(descriptor, AGENTS_PROTECTION_CONFIG_MAX_BYTES + 1 - len(raw))
        if not chunk:
            return raw
        raw += chunk
    raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: oversized config")


# LLM: 缺配置只能在persona目录自身非链接且文件确实不存在时成立；其它异常由上层失败关闭。
# 函数用途: 安全取得配置保护投影，缺失返回None。
def _protection_configuration(config: Path, home: Path) -> dict | None:
    try:
        control_info = config.parent.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(control_info.st_mode):
        raise AgentsSourceProtectionError("PATH_AGENTS_SOURCE_PROTECTION_INVALID: persona directory")
    try:
        config.lstat()
    except FileNotFoundError:
        return None
    raw, _ = _protection_file(config, read_config=True)
    record = json.loads(raw.decode("utf-8"), object_pairs_hook=_protection_json_object)
    _protection_validate_record(record, home)
    return record


# LLM: 只验证拒写投影，不能供应业务配置/CAS或授权；字段及精确类型不因拆分放宽。
# 函数用途: 核对来源配置的owner、schema、代次和定位类型。
def _protection_validate_record(record: dict, home: Path) -> None:
    if type(record) is not dict or set(record) != _AGENTS_PROTECTION_KEYS:
        raise ValueError("fields")
    if type(record["owner"]) is not str or record["owner"] != _protection_owner_id(home):
        raise ValueError("owner")
    if type(record["schema_version"]) is not int or record["schema_version"] != 1:
        raise ValueError("schema")
    if type(record["revision"]) is not int or record["revision"] <= 0:
        raise ValueError("revision")
    if type(record["updated_by"]) is not str or not record["updated_by"].strip():
        raise ValueError("actor")
    if type(record["updated_at"]) is not float or not math.isfinite(record["updated_at"]) or record["updated_at"] <= 0:
        raise ValueError("time")
    if type(record["kind"]) is not str or record["kind"] not in {"owner_default", "local_path", "url"}:
        raise ValueError("kind")
    identity = record["identity"]
    if record["kind"] != "url" and (type(identity) is not str or len(identity) != sha256().digest_size * 2
                                     or any(char not in "0123456789abcdef" for char in identity)):
        raise ValueError("identity")


# LLM: 网络来源不额外保护本地正文，但坏URL或非空本地identity不能被当合法保护投影。
# 函数用途: 严格核验URL配置所需的拒写元数据。
def _protection_url(record: dict) -> None:
    if type(record["value"]) is not str or record["value"].strip() != record["value"]:
        raise ValueError("url")
    url = urlsplit(record["value"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment or record["identity"] != "":
        raise ValueError("url")


# LLM: 缺文件仅不能证明inode别名；原路径及祖先保护已由调用方独立比较，不因此解除它们。
# 函数用途: 判断两定位是否是同一个实际文件。
def _protection_same_inode(candidate: Path, target: Path) -> bool:
    try:
        return os.path.samestat(candidate.stat(), target.stat())
    except OSError:
        return False


# LLM: 单owner只读保护投影每次重新加载；坏配置拒绝基础写，不缓存许可或推进epoch。
# 函数用途: 给一份owner来源返回拒写结果，普通业务文件返回None。
def _persona_home_write_decision(path: Path, home: Path) -> PathAccessDecision | None:
    try:
        protected = persona_protected_paths(home)
        is_protected = persona_path_is_protected(path, protected)
    except (AgentsSourceProtectionError, OSError, RuntimeError):
        return PathAccessDecision(False, "PATH_AGENTS_SOURCE_PROTECTION_INVALID",
                                  "AGENTS来源保护无法安全核验；本次未写入，请由宿主修复来源配置或文件身份。", str(home))
    if not is_protected:
        return None
    return PathAccessDecision(False, "PERSONA_WRITE_REQUIRES_TOOL",
                              "人格、persona控制域及已配置AGENTS来源不能用基础文件或命令修改；"
                              "USER请使用 update_persona target=user（无需用户确认），AGENTS请使用 /agents 显式预览确认，"
                              "SOUL请使用 update_persona target=soul 用户确认链。", str(home))
