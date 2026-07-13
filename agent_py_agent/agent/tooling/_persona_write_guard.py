from __future__ import annotations

"""LLM: owner 人格三件套只能经 update_persona 修改，所有基础写工具共用本模块判定。

模块用途: 阻止 write/edit/patch/shell 绕过 USER 自主写与 SOUL/AGENTS 用户确认链。
"""

from pathlib import Path

_PERSONA_FILE_NAMES = frozenset({"SOUL.md", "USER.md", "AGENTS.md"})


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
