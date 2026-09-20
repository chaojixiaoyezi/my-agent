# LLM: 普通请求和控制入口共用宿主目录校验；输入只是路径声明，不能替代 owner 权限或持久线程事实。
# 模块用途: 校验执行目录与工作区根，拒绝不存在、相对或越权路径，保持唯一规则实现。
from __future__ import annotations

from pathlib import Path


# LLM: 非法目录是客观路径或权限错误；必须在历史写入和模型执行之前返回稳定错误码，不能静默改用 daemon 目录。
# 类用途: 表示 TUI/CLI 提交的工作目录不存在、格式错误或越过当前 owner 边界。
class GatewayWorkspaceScopeError(ValueError):
    error_code = "GATEWAY_WORKSPACE_INVALID"


# LLM: 只有权限墙已解除的本地管理员可使用外部目录；宿主校验路径，不从路径声明本身取得授权。
# 函数用途: 校验客户端这轮希望使用的目录；WorkspaceOnly 用户只能提交自己 owner home 内路径，管理员 Full Access 才能提交外部目录。
def gateway_request_workspace_scope(
    agent: object,
    request: dict[str, object],
) -> tuple[str, tuple[str, ...]]:
    if "workspace" not in request:
        return "", ()
    raw = request.get("workspace")
    if not isinstance(raw, dict):
        raise GatewayWorkspaceScopeError("workspace 必须是对象")
    provider = (
        str(getattr(getattr(agent, "config", None), "my_agent_owner_provider", "local") or "local")
        .strip()
        .lower()
    )
    if provider not in {"", "local"}:
        raise GatewayWorkspaceScopeError("远程 owner 不能覆盖主机工作目录")
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
    owner_scope_text = str(
        getattr(getattr(agent, "tools", None), "owner_scope_root", "") or ""
    ).strip()
    if owner_scope_text:
        owner_scope = Path(owner_scope_text).expanduser().resolve(strict=False)
        if not path_is_within(cwd, owner_scope) or any(
            not path_is_within(root, owner_scope) for root in roots
        ):
            raise GatewayWorkspaceScopeError(
                "当前身份处于 WorkspaceOnly；外部目录只有本机管理员开启 Full Access 后才能使用"
            )
    return str(cwd), tuple(str(path) for path in roots)



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
