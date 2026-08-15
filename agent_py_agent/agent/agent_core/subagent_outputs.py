from __future__ import annotations

from pathlib import Path

from ..subagents.services.output_alignment import looks_like_output_path


# LLM: Declared child outputs are checked only against exact structured paths; never fuzzy-search names.
# 函数用途: 判断子代理派工时明确声明的产物是否全部存在；无声明时不作完成证明。
def declared_output_refs_satisfied(item: dict[str, object]) -> bool:
    attrs = item.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    declared = [
        text
        for field_name in ("output_files", "output_refs")
        for text in _declared_path_texts(attrs.get(field_name))
    ]
    if not declared:
        return False
    workspace = str(item.get("task_workspace_dir") or "").strip()
    return all(_declared_path_exists(text, workspace) for text in declared)


def _declared_path_texts(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text
        for raw in value
        if isinstance(raw, str)
        and (text := raw.strip())
        and looks_like_output_path(text)
    ]


def _declared_path_exists(text: str, workspace: str) -> bool:
    path = Path(text).expanduser()
    if not path.is_absolute():
        if not workspace:
            return False
        path = Path(workspace).expanduser() / path
    try:
        return path.resolve(strict=False).exists()
    except OSError:
        return False


__all__ = ["declared_output_refs_satisfied"]
