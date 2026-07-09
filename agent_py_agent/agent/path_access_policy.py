"""Shared path access policy for main agents and subagents."""

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


@dataclass(frozen=True)
class PathAccessDecision:
    allowed: bool
    code: str = ""
    message: str = ""
    dangerous_root: str = ""


@dataclass(frozen=True)
class PathAccessPolicy:
    mode: str = DEFAULT_PATH_ACCESS_MODE
    dangerous_roots: tuple[Path, ...] = ()
    # 多用户隔离硬墙(0 层):设了 owner_scope_root = per-user agent,my-agent 数据目录里只放行
    #   自己 owner home 子树 + 顶层公共区,owners/ 下别人的家一律拦掉。不设 = 原行为(整个 .my-agent
    #   豁免,单租户/主代理用),向后兼容。根因:OwnerScopedAgentPool 给每个 owner 的 scoped agent
    #   传共享 base root,只 home_paths 按 owner 分,文件工具此前无 owner 硬墙 → A 能读 B 的家。
    owner_scope_root: Path | None = None

    @classmethod
    def from_config(cls, config: object | None) -> PathAccessPolicy:
        return cls.from_values(
            mode=getattr(config, "path_access_mode", DEFAULT_PATH_ACCESS_MODE),
            dangerous_roots=getattr(config, "path_dangerous_roots", DEFAULT_DANGEROUS_PATH_ROOTS),
        )

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
        home = _current_user_home()
        # 当前用户自己的 home 不整个列危险目录:root 用户场景 /root==home 会把 /root/my-agent-src 等
        # 源码/工作目录的 read 操作(list_files/find_files/read_file)也误伤拦掉,逼 agent 改用 run_command 绕。
        # 移除 ==home 的项后,home 下单独列的敏感子目录(~/.ssh/~/.aws 等)仍在 dangerous_roots 生效;
        # 非 root 用户 /root!=home 仍保留拦截(不碰别人的 root 目录),/etc 等系统目录也不受影响。
        roots = tuple(root for root in roots if root is not None and root != home)
        scope = _normalized_root(owner_scope_root) if owner_scope_root else None
        return cls(mode=normalized_mode, dangerous_roots=roots, owner_scope_root=scope)

    def check(self, path: str | Path) -> PathAccessDecision:
        if self.mode == PATH_ACCESS_MODE_FULL:
            return PathAccessDecision(True)
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError):
            return PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败，请检查路径是否有效。")
        # 选项1-B 凭据文件 denylist(按文件名,任何位置——含 owner 自己家,home 豁免不覆盖它;
        # 抄 长期助手 file_safety):.env 家族/凭据文件每每装 API key、DB 密码,文件工具不该读写它们
        # (要看结构用 .env.example)。.env.example 放行(安全模板)。
        if _is_credential_filename(resolved.name):
            return PathAccessDecision(
                False,
                "PATH_CREDENTIAL_FILE_BLOCKED",
                f"禁止读写凭据文件(可能含 API key/密码): target={resolved}；如需看结构请用 .env.example。",
                resolved.name,
            )
        # my-agent 自己的数据目录(home,默认 ~/.my-agent,可经 MY_AGENT_HOME 覆盖)豁免 dangerous_roots:
        # agent 写自己的产物/记忆/审计天经地义。否则 root 用户场景下 /root 被列危险目录,会误伤
        # /root/.my-agent/.../output(agent 自己的产物目录)。豁免精确到 home 子树——/root/.ssh 等敏感
        # 目录不在 my-agent home 下,仍被 dangerous_roots 拦截,口子不扩大(resolve 已展开 .. 防逃逸)。
        home_root = _my_agent_home_root()
        if home_root is not None and _is_relative_to(resolved, home_root):
            owner_decision = self._owner_scope_decision(resolved, home_root)
            if owner_decision is not None:
                return owner_decision
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

    def _owner_scope_decision(self, resolved: Path, home_root: Path) -> PathAccessDecision | None:
        """多用户隔离:my-agent 数据目录内的 owner 级判定。

        - 不设 owner_scope_root → 返回 None(走原行为:整个 .my-agent 豁免,单租户/主代理);
        - 自己 owner home 子树 → 放行(自己家随便读写);
        - admin_grants/ → 拦(admin 级 bypass 授权目录,owner 降权不可自授权,PATH_ADMIN_GRANTS_BLOCKED);
        - owners/ 下但不是自己的 → 拦(别人的家,PATH_CROSS_OWNER_BLOCKED);
        - .my-agent 顶层公共区(非 owners/、非 admin_grants/,如全局 SOUL/全局 skills/配置)→ 放行(公共可用)。
        """
        if self.owner_scope_root is None:
            return None
        if _is_relative_to(resolved, self.owner_scope_root):
            return PathAccessDecision(True)
        admin_grants_root = home_root / "admin_grants"
        if _is_relative_to(resolved, admin_grants_root):
            # admin bypass 授权目录:owner-scoped agent 一律拦(防自授权),与 ①归一/bwrap 三重堵。
            # 框架启动判 bypass、真人 admin 写授权都不经此 policy → 不受影响。
            return PathAccessDecision(
                False,
                "PATH_ADMIN_GRANTS_BLOCKED",
                f"禁止访问 admin 授权目录(降权用户不可自授权): target={resolved}",
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
        return PathAccessDecision(True)


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
    "PathAccessDecision",
    "PathAccessPolicy",
    "normalize_path_access_mode",
]
