# LLM: 普通请求和控制入口共用宿主目录校验；输入只是路径声明，不能替代 owner 权限或持久线程事实。
# 模块用途: 校验执行目录与工作区根，拒绝远程覆盖、身份冲突、不存在及越权路径，保持唯一规则实现。
from __future__ import annotations

from pathlib import Path


# LLM: 非法目录是客观路径或权限错误；必须在历史写入和模型执行之前返回稳定错误码，不能静默改用 daemon 目录。
# 类用途: 表示 TUI/CLI 提交的工作目录不存在、格式错误或越过当前 owner 边界。
class GatewayWorkspaceScopeError(ValueError):
    error_code = "GATEWAY_WORKSPACE_INVALID"


# LLM: 显式绑定的目录经统一安全检查；本地管理员普通模式可声明墙外项目，其他owner只允许自己的家。
# 函数用途: 校验客户端执行目录和根，拒绝危险根、控制面与跨owner路径，不改默认owner home语义。
def gateway_request_workspace_scope(
    agent: object,
    request: dict[str, object],
) -> tuple[str, tuple[str, ...]]:
    if "workspace" not in request:
        return "", ()
    raw = request.get("workspace")
    if not isinstance(raw, dict):
        raise GatewayWorkspaceScopeError("workspace 必须是对象")
    cwd = _existing_absolute_workspace_dir(raw.get("cwd"), field_name="cwd")
    raw_roots = raw.get("roots")
    if raw_roots is None:
        raw_roots = []
    if not isinstance(raw_roots, list):
        raise GatewayWorkspaceScopeError("workspace.roots 必须是数组")
    roots: list[Path] = []
    for value in raw_roots:
        path = _existing_absolute_workspace_dir(value, field_name="roots")
        if path not in roots:
            roots.append(path)
    if not roots:
        roots.append(cwd)
    if not any(path_is_within(cwd, root) for root in roots):
        raise GatewayWorkspaceScopeError("workspace.cwd 不在声明的 roots 内")
    _validate_owner_workspace(agent, [cwd, *roots])
    return str(cwd), tuple(str(path) for path in roots)


# LLM: 远程config先拒绝；HomePaths管理员还须与显式config kind一致，空工具墙不授管理员，薄Host保留原身份硬门。
# 函数用途: 核对真实用户身份和目录边界，防止旧HomePaths覆盖远程或普通身份配置。
def _validate_owner_workspace(agent: object, roots: list[Path]) -> None:
    from ..conversation.workspace_context import validate_workspace_directory
    from ..user_space.owner_access import is_local_admin_owner

    home = getattr(agent, "home_paths", None)
    config = getattr(agent, "config", None)
    provider = str(getattr(config, "my_agent_owner_provider", "local") or "local").strip().lower()
    if provider not in {"", "local"}:
        raise GatewayWorkspaceScopeError("远程 owner 不能覆盖主机工作目录")
    if home is not None:
        cfg_kind = str(getattr(config, "my_agent_owner_kind", "") or "").strip().lower()
        is_admin = is_local_admin_owner(home) and cfg_kind in {"", "main"}
        owner_text = getattr(home, "owner_home_dir", "")
    else:
        kind = getattr(config, "my_agent_owner_kind", None)
        is_admin = provider in {"", "local"} and kind in {"", "main"}
        owner_text = getattr(getattr(agent, "tools", None), "owner_scope_root", "")
    if not owner_text:
        raise GatewayWorkspaceScopeError("缺少规范的owner home，不能绑定目录")
    try:
        for root in roots:
            validate_workspace_directory(root, owner_home=Path(owner_text), is_admin=is_admin,
                home_root=getattr(home, "root", None))
    except ValueError as error:
        raise GatewayWorkspaceScopeError(str(error)) from error



# LLM: 将符号链接解析为现存绝对目录；缺失或相对路径不能改用 daemon cwd，以免在别处执行。
# 函数用途: 将一个客户端目录字段校验为现存绝对目录。
def _existing_absolute_workspace_dir(value: object, *, field_name: str) -> Path:
    text = str(value or "").strip()
    candidate = Path(text).expanduser()
    if not text or not candidate.is_absolute():
        raise GatewayWorkspaceScopeError(f"workspace.{field_name} 必须是绝对目录")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise GatewayWorkspaceScopeError(f"workspace.{field_name} 目录不存在或不可访问") from exc
    if not resolved.is_dir():
        raise GatewayWorkspaceScopeError(f"workspace.{field_name} 不是目录")
    return resolved



# LLM: 只比较已规范化的路径关系，不用字符串前缀猜目录包含关系。
# 函数用途: 判断 cwd 是否位于某个声明的工作区根目录内。
def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
