# LLM: 路径裁决只有这一份实现；每个策略在构造时冻结宿主根，跨进程运输不得重新猜测。核心墙外复核保留显式新策略构造，联查 owner、文件工具与读取上下文测试。
# 模块用途: 判断目标是否处于当前 owner、已授权工作根或普通路径策略允许的范围，不读取文件内容。

from __future__ import annotations

import os
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

# 文件系统根级目录是操作系统本体而不是"工作目录"：管理员即使 full-access 也不把它自动
# 继承给子代理或当成工具执行根。这是写死的少量常量，不是可扩张的名单，也不做任何危险判断；
# 判据只看归一化后的真实路径，不读模型文字。
# 常量用途: 列出不得作为可继承工作根自动下发的根级目录。
UNINHERITABLE_ROOT_DIRS = ("/", "/System", "/usr", "/bin", "/sbin", "/private/etc", "/etc")

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

    # LLM: owner 墙强于模式；只读构造时冻结的根，不受另一个会话或子进程环境影响，原凭据与危险根顺序保持。
    # 函数用途: 解析目标并判断是否位于当前 owner 或管理员允许访问的路径范围，不读取内容。
    def check(self, path: str | Path) -> PathAccessDecision:
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败，请检查路径是否有效。")
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


# LLM: 用户显式声明的工作目录（宿主写入 conversation_execution_cwd /
#   conversation_runtime_workspace_roots，或已授权的 allowed_write_roots）是结构化事实，
#   可以下发成子代理工作根与工具执行根；但文件系统根级目录、以及 my-agent 自己的运行记录区
#   （runs/agents/data/logs/其它 owner 的家）永远不是工作目录。
#   判据只用归一化路径，不读 goal 文字、不读模型自报的产物路径；无法解析的输入按"没有事实"丢弃。
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
        if text in UNINHERITABLE_ROOT_DIRS:
            continue
        if agent_home is not None and _is_relative_to(path, agent_home):
            if home is None or not _is_relative_to(path, home):
                # 数据根里只有这个 owner 自己的家算工作区；其余是宿主控制面。
                continue
        if text not in roots:
            roots.append(text)
    return roots


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


__all__ = [
    "DEFAULT_DANGEROUS_PATH_ROOTS",
    "DEFAULT_PATH_ACCESS_MODE",
    "PATH_ACCESS_MODE_FULL",
    "PATH_ACCESS_MODE_NORMAL",
    "UNINHERITABLE_ROOT_DIRS",
    "PathAccessDecision",
    "PathAccessPolicy",
    "agent_home_root_for_owner",
    "inheritable_declared_work_roots",
    "normalize_path_access_mode",
]
