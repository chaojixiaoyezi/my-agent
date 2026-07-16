from __future__ import annotations

"""LLM: owner 人格三件套只能经 update_persona 修改，所有基础写工具共用本模块判定。

模块用途: 阻止 write/edit/patch/shell 绕过 USER 自主写与 SOUL/AGENTS 用户确认链。
"""

from pathlib import Path
from typing import Any

_PERSONA_FILE_NAMES = frozenset({"SOUL.md", "USER.md", "AGENTS.md"})
_PERSONA_MUTATING_TOOLS = frozenset({"write_file", "edit_file", "apply_patch", "run_command"})


def _current_owner_persona_paths(agent: object) -> tuple[Path, ...]:
    home = getattr(agent, "home_paths", None)
    paths: list[Path] = []
    for attr in ("owner_soul_md", "owner_user_md", "owner_agents_md"):
        raw = getattr(home, attr, None)
        if raw:
            paths.append(Path(raw).expanduser().resolve(strict=False))
    return tuple(paths)


def _tool_workspace_root(agent: object, tool_name: str) -> Path | None:
    tools = getattr(getattr(agent, "tools", None), "tools", None)
    tool = tools.get(tool_name) if isinstance(tools, dict) else None
    raw = getattr(tool, "workspace_root", None)
    return Path(raw).expanduser().resolve(strict=False) if raw else None


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


def _payload_mentions_persona_path(
    agent: object,
    payload: dict[str, Any],
    protected: tuple[Path, ...],
) -> bool:
    tool_name = str(payload.get("tool") or "").strip()
    workspace_root = _tool_workspace_root(agent, tool_name)
    if tool_name in {"write_file", "edit_file"}:
        raw_path = str(payload.get("path") or "").strip()
        if not raw_path:
            return False
        candidate = _resolved_tool_path(raw_path, workspace_root)
        return candidate in protected
    if tool_name == "apply_patch":
        patch = str(payload.get("patch") or "")
        return any(path in protected for path in _patch_target_paths(patch, workspace_root))
    if tool_name == "run_command":
        command = str(payload.get("command") or "")
        working_dir = str(payload.get("working_dir") or "").strip()
        if any(str(path) in command for path in protected):
            return True
        cwd = _resolved_tool_path(working_dir or ".", workspace_root)
        if any(cwd == path.parent and path.name in command for path in protected):
            return True
    return False


def _persona_runtime_redirect_error(agent: object, payload: dict[str, Any]) -> str:
    """晋升任务前识别当前 owner 人格写入，让专用工具错误不被工作区选择闸覆盖。"""
    tool_name = str(payload.get("tool") or "").strip()
    if tool_name not in _PERSONA_MUTATING_TOOLS:
        return ""
    protected = _current_owner_persona_paths(agent)
    if not protected or not _payload_mentions_persona_path(agent, payload, protected):
        return ""
    return (
        "SOUL.md、USER.md、AGENTS.md 不能通过基础文件、补丁或 shell 工具修改；"
        "本次没有发生任何变更。请改用 update_persona：USER 画像变更还必须携带当前用户消息的"
        "逐字 source_quote；SOUL/AGENTS 走用户确认链。工具失败后不得向用户宣称修改成功。"
    )


# LLM: USER 同样阻止基础工具写入，但恢复指引必须说明 target=user 无需用户确认。
# 函数用途: 判断目标是否是受保护人格文件，并返回应改走 update_persona 的提示。
def _persona_approval_write_error(target: Path, protected_root: Path | None = None) -> str:
    """人格三件套只允许 update_persona 写；USER 走该工具但无需用户确认。"""
    if target.name not in _PERSONA_FILE_NAMES:
        return ""
    resolved = target.expanduser().resolve(strict=False)
    root = protected_root.expanduser().resolve(strict=False) if protected_root else None
    is_current_owner_file = root is not None and resolved.parent == root
    parts = resolved.parts
    is_known_owner_file = ".my-agent" in parts and "owners" in parts
    if not is_current_owner_file and not is_known_owner_file:
        return ""
    if target.name == "USER.md":
        return (
            "USER.md 是用户画像和偏好的权威文件，不能用基础文件或 shell 直接修改。"
            "请使用 update_persona target=user；该操作可由 Agent 自主完成，不需要用户确认。"
        )
    return (
        f"{target.name} 是需要用户同意的长期设定，不能用基础文件或 shell 直接修改。"
        "请使用 update_persona 的用户确认链。"
    )


# LLM: 这是 update_persona/受控写入口的内容安全扫描，不能替代路径级统一写入口守卫。
# 函数用途: 检查准备写入人格文件的内容是否含长期提示注入或凭据外泄特征。
def _persona_injection_write_error(target: Path, content: str | None) -> str:
    """人格三件套(owner home 下的 SOUL/USER/AGENTS.md)写入前执行注入扫描。"""
    if content is None:
        return ""
    if target.name not in _PERSONA_FILE_NAMES:
        return ""
    if ".my-agent" not in target.resolve(strict=False).parts:
        return ""
    from ..capability.memory_threat_scan import scan_memory_content

    scan = scan_memory_content(content)
    if scan.safe:
        return ""
    return (
        f"人格文件写入被拒(命中注入/外泄特征): {scan.reason()}。SOUL/USER/AGENTS.md 每轮读回系统上下文,"
        "不能落可执行指令/凭证语义;若确为正常人设/画像,改写成纯描述再写。"
    )
