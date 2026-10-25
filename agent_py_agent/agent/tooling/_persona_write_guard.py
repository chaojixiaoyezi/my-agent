from __future__ import annotations

"""LLM: owner 人格三件套只能经 update_persona 修改，所有基础写工具共用本模块判定。

模块用途: 阻止 write/edit/patch/shell 绕过 USER/AGENTS 自主写入口与 SOUL 用户确认链。
"""

from pathlib import Path
from typing import Any

from ..path_access_policy import (
    AgentsSourceProtectionError,
    persona_path_is_protected,
    persona_protected_paths,
)

_PERSONA_FILE_NAMES = frozenset({"SOUL.md", "USER.md", "AGENTS.md"})
_PERSONA_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "run_command", "terminal_session"})


# LLM: root来自宿主home_paths；保护投影不可用必须向调用方报告，不能只剩三份默认文件。
# 函数用途: 获取当前owner人格、控制域及任意文件名来源的拒写路径。
def _current_owner_persona_paths(agent: object) -> tuple[Path, ...]:
    home = getattr(agent, "home_paths", None)
    paths: list[Path] = []
    for attr in ("owner_soul_md", "owner_user_md", "owner_agents_md"):
        raw = getattr(home, attr, None)
        if raw:
            paths.append(Path(raw).expanduser().resolve(strict=False))
    roots = tuple(dict.fromkeys(path.parent for path in paths))
    return tuple(dict.fromkeys(path for root in roots for path in persona_protected_paths(root)))


def _resolved_tool_path(raw_path: str, workspace_root: Path | None) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute() and workspace_root is not None:
        candidate = workspace_root / candidate
    return candidate.resolve(strict=False)


def _patch_target_paths(patch: str, workspace_root: Path | None) -> tuple[Path, ...]:
    markers = (
        "*** Add File: ",
        "*** Update File: ",
        "*** Delete File: ",
        "*** Move to: ",
    )
    targets: list[Path] = []
    for line in patch.splitlines():
        marker = next((item for item in markers if line.startswith(item)), "")
        if not marker:
            continue
        raw_path = line.removeprefix(marker).strip()
        if raw_path:
            targets.append(_resolved_tool_path(raw_path, workspace_root))
    return tuple(targets)


# LLM: 文本预识别只给恢复提示，安全执行仍依赖通用路径门和平台沙箱；补丁目的路径与原路径都检查。
# 函数用途: 从基础工具的结构参数识别对人格控制域、本地来源或其父目录的修改。
def _payload_mentions_persona_path(
    payload: dict[str, Any],
    protected: tuple[Path, ...],
    *,
    workspace_root: Path | None,
) -> bool:
    tool_name = str(payload.get("tool") or "").strip()
    if tool_name in {"write_file", "edit_file"}:
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            return False
        candidate = _resolved_tool_path(raw_path, workspace_root)
        return persona_path_is_protected(candidate, protected)
    if tool_name == "apply_patch":
        patch = str(payload.get("patch") or "")
        return any(persona_path_is_protected(path, protected) for path in _patch_target_paths(patch, workspace_root))
    if tool_name in {"run_command", "terminal_session"}:
        command = str(payload.get("command") or "")
        working_dir = str(payload.get("working_dir") or "").strip()
        if any(str(path) in command for path in protected):
            return True
        cwd = _resolved_tool_path(working_dir or ".", workspace_root)
        if any(cwd == path.parent and path.name in command for path in protected):
            return True
    return False


# LLM: 只做晋升前重定向；坏配置拒绝基础写入，不能把未知任意名来源当普通业务文件。
# 函数用途: 在工作区晋升前给人格/来源写入返回专用入口或保护不可用提示。
def _persona_runtime_redirect_error(
    agent: object,
    payload: dict[str, Any],
    *,
    workspace_root: Path | None = None,
) -> str:
    """晋升任务前识别当前 owner 人格写入，让专用工具错误不被工作区选择闸覆盖。"""
    tool_name = str(payload.get("tool") or "").strip()
    if tool_name not in _PERSONA_MUTATING_TOOLS:
        return ""
    try:
        protected = _current_owner_persona_paths(agent)
    except (AgentsSourceProtectionError, OSError, RuntimeError):
        return "AGENTS来源保护无法安全核验；本次未执行基础写入。请由宿主修复来源配置或文件身份。"
    if not protected or not _payload_mentions_persona_path(
        payload,
        protected,
        workspace_root=workspace_root,
    ):
        return ""
    return (
        "SOUL.md、USER.md、AGENTS.md、persona控制域及已配置AGENTS来源不能通过基础文件、补丁或 shell 工具修改；"
        "本次没有发生任何变更。请改用 update_persona：USER 画像可由当前 owner 的 Agent 结构化更新；"
        "AGENTS 必须走 /agents 显式预览确认，SOUL 走原用户确认链。工具失败后不得向用户宣称修改成功。"
    )


# LLM: 任意名AGENTS来源使用标准库安全投影，不按文件名放行；USER恢复仍说明无需用户确认。
# 函数用途: 拒绝人格、来源、控制域及其父目录的基础写入，并返回专用工具恢复提示。
def _persona_approval_write_error(target: Path, protected_root: Path | None = None) -> str:
    """人格三件套只允许 update_persona 写；USER 走该工具但无需用户确认。"""
    resolved = target.expanduser().resolve(strict=False)
    root = protected_root.expanduser().resolve(strict=False) if protected_root else None
    source_error = _source_approval_write_error(target, resolved, root)
    if source_error is not None:
        return source_error
    if target.name not in _PERSONA_FILE_NAMES:
        return ""
    is_current_owner_file = root is not None and resolved.parent == root
    parts = resolved.parts
    is_known_owner_file = _legacy_owner_persona_file(resolved)
    if not is_current_owner_file and not is_known_owner_file:
        return ""
    if target.name == "USER.md":
        return (
            "USER.md 是用户画像和偏好的权威文件，不能用基础文件或 shell 直接修改。"
            "请使用 update_persona target=user；该操作可由 Agent 自主完成，不需要用户确认。"
        )
    if target.name == "AGENTS.md":
        return (
            "AGENTS.md 是当前 owner 的长期工作约定，不能用基础文件或 shell 直接修改。"
            "请使用 update_persona target=agents；该操作可由 Agent 自主完成。"
        )
    return (
        "SOUL.md 是需要用户同意的长期性格设定，不能用基础文件或 shell 直接修改。"
        "请使用 update_persona target=soul 的用户确认链。"
    )


# LLM: 来源错误提示只作为基础文件门恢复信息；不能授予默认路径或替代原生来源授权入口。
# 函数用途: 拆平任意文件名的来源保护提示，None表示继续旧默认人格提示。
def _source_approval_write_error(target: Path, resolved: Path, root: Path | None) -> str | None:
    if root is None:
        return None
    try:
        protected = persona_path_is_protected(resolved, persona_protected_paths(root))
    except (AgentsSourceProtectionError, OSError, RuntimeError):
        return "AGENTS来源保护无法安全核验；本次未写入，请由宿主修复配置或来源身份。"
    if not protected:
        return "" if target.name not in _PERSONA_FILE_NAMES else None
    if resolved not in {root / name for name in _PERSONA_FILE_NAMES}:
        return "人格控制域、已配置AGENTS来源及其父目录不能经基础文件工具修改；请使用 /agents 显式预览确认入口。"
    return None


# LLM: legacy识别含local普通用户，但必须是owner根文件，不能误认深处项目AGENTS.md。
# 函数用途: 识别旧默认.my-agent/owners布局里的根人格文件，保持旧单层owner兼容。
def _legacy_owner_persona_file(path: Path) -> bool:
    parts = path.parts
    for index, part in enumerate(parts):
        if part != ".my-agent" or parts[index + 1:index + 2] != ("owners",):
            continue
        tail = parts[index + 2:-1]
        if len(tail) == 1 or (len(tail) == 2 and tail[0] == "local" and bool(tail[1])) or (
                len(tail) == 4 and tail[0] == "providers" and tail[2] in {"users", "groups"}):
            return True
    return False


# LLM: 受控内容扫描只针对owner根人格文件；业务项目AGENTS.md不因在.my-agent后代目录中就被扫描。
# 函数用途: 检查人格内容的长期提示注入/凭据外泄特征，保持正常业务工程写入。
def _persona_injection_write_error(target: Path, content: str | None) -> str:
    """人格三件套(owner home 下的 SOUL/USER/AGENTS.md)写入前执行注入扫描。"""
    if content is None:
        return ""
    if target.name not in _PERSONA_FILE_NAMES:
        return ""
    if not _legacy_owner_persona_file(target.resolve(strict=False)):
        return ""
    from ..memory_store.security import scan_memory_content

    scan = scan_memory_content(content)
    if scan.safe:
        return ""
    return (
        f"人格文件写入被拒(命中注入/外泄特征): {scan.reason()}。SOUL/USER/AGENTS.md 每轮读回系统上下文,"
        "不能落可执行指令/凭证语义;若确为正常人设/画像,改写成纯描述再写。"
    )
