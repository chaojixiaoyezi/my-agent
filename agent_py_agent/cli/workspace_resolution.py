from __future__ import annotations

from pathlib import Path, PureWindowsPath

# LLM: This module is the dependency-light authority for CLI workspace-root normalization. Keep
# it free of Agent/Core imports so lightweight clients and full agents resolve identical roots;
# owner helpers are imported lazily only when the default root is requested.
# 模块用途: 统一解析命令行工作区路径，兼容 owner home 默认值、显式单/多目录、相对路径和跨平台绝对路径。


# LLM: An explicit command argument overrides config only when it is a non-empty path-like value.
# 函数用途: 从命令参数中读取明确指定的工作区，并展开为绝对路径。
def explicit_workspace_root(args) -> Path | None:
    raw = getattr(args, "workspace_root", None)
    if not isinstance(raw, (str, Path)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    return Path(text).expanduser().resolve()


# LLM: Process cwd is not an owner or permission fact. All CLI/Gateway entrypoints with no
# explicit workspace must derive the same canonical owner home without creating it here.
# 函数用途: 返回当前结构化 owner 的默认工作目录，让从 /root 或任意项目启动的 TUI 都先回到自己的家。
def owner_home_workspace_root(config) -> Path:
    from ..agent.user_space.home_layout import home_paths
    from ..agent.user_space.home_root import configured_home_root
    from ..agent.user_space.owner_resolver import owner_identity_from_config, resolve_owner_home

    base = home_paths(configured_home_root(config))
    owner = resolve_owner_home(base.root, owner_identity_from_config(config))
    return owner.home_dir.resolve(strict=False)


# LLM: CLI/config paths are requests, not authority. WorkspaceOnly accepts only owner-home roots;
# an external root requires the structured local/main identity and access_mode=full-access.
# 函数用途: 在创建 Agent 前检查显式工作区，避免命令行静默忽略外部目录而 Gateway 却报错。
def validate_requested_workspace_roots(config, roots: list[Path]) -> None:
    owner_home = owner_home_workspace_root(config)
    if all(_is_relative_to(root, owner_home) for root in roots):
        return
    provider = str(getattr(config, "my_agent_owner_provider", "") or "local").strip().lower()
    owner_kind = str(getattr(config, "my_agent_owner_kind", "") or "main").strip().lower()
    access_mode = (
        str(getattr(config, "access_mode", "workspace-write") or "workspace-write")
        .strip()
        .lower()
        .replace("_", "-")
    )
    if provider in {"", "local"} and owner_kind in {"", "main"} and access_mode == "full-access":
        return
    raise ValueError(
        "当前身份处于 WorkspaceOnly；外部工作区只有本机管理员开启 Full Access 后才能使用"
    )


# LLM: The first normalized root is the canonical primary workspace; callers needing all roots
# must use resolve_workspace_roots rather than reparsing config themselves.
# 函数用途: 返回配置中第一个有效工作区；调用方可把 owner home 作为缺省目录传入。
def resolve_workspace_root(
    config,
    config_path: str | Path,
    *,
    current_dir: str | Path | None = None,
) -> Path:
    return resolve_workspace_roots(config, config_path, current_dir=current_dir)[0]


# LLM: Preserve declared order, remove duplicates, and ignore foreign Windows absolute paths on
# POSIX rather than accidentally treating them as relative local paths.
# 函数用途: 解析全部工作区路径并去重，列表为空或无有效项时回退调用方给定的缺省目录。
def resolve_workspace_roots(
    config,
    config_path: str | Path,
    *,
    current_dir: str | Path | None = None,
) -> list[Path]:
    raw_value = getattr(config, "workspace_root", "")
    raw_roots = _raw_workspace_roots(raw_value)
    cwd = (
        Path(current_dir).expanduser().resolve()
        if current_dir is not None
        else owner_home_workspace_root(config)
    )
    roots: list[Path] = []
    for raw in raw_roots:
        candidate = _resolve_one_workspace_root(raw, config_path, current_dir=cwd)
        if candidate is not None and candidate not in roots:
            roots.append(candidate)
    return roots or [cwd]


# LLM: Normalize scalar/list config shapes without interpreting path text.
# 函数用途: 把单个工作区值统一成列表，方便后续逐项处理。
def _raw_workspace_roots(raw_value: object) -> list[object]:
    if isinstance(raw_value, list):
        return raw_value
    return [raw_value]


# LLM: Relative config paths resolve beside the selected config file, matching the historical CLI
# contract. Empty values intentionally select the canonical owner home supplied by the caller.
# 函数用途: 解析一条工作区配置；空值使用 owner home，Windows 异机绝对路径在 POSIX 下跳过。
def _resolve_one_workspace_root(
    raw_value: object,
    config_path: str | Path,
    *,
    current_dir: Path,
) -> Path | None:
    raw = str(raw_value or "").strip()
    if not raw:
        return current_dir
    if _is_foreign_windows_absolute_path(raw):
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path(config_path).expanduser().resolve().parent / candidate
    return candidate.resolve()


# LLM: A native absolute Path is never foreign; drive-plus-root is the portable Windows signal.
# 函数用途: 判断当前系统不能安全解释的 Windows 绝对路径，防止误拼到本机目录。
def _is_foreign_windows_absolute_path(raw: str) -> bool:
    path = Path(raw)
    if path.is_absolute():
        return False
    windows_path = PureWindowsPath(raw)
    return bool(windows_path.drive and windows_path.root)


# LLM: Path containment always compares normalized absolute paths; callers must not use string
# prefixes because sibling owner identifiers may share text prefixes.
# 函数用途: 判断请求工作区是否真的位于当前 owner home 内。
def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


__all__ = [
    "explicit_workspace_root",
    "owner_home_workspace_root",
    "resolve_workspace_root",
    "resolve_workspace_roots",
    "validate_requested_workspace_roots",
]
