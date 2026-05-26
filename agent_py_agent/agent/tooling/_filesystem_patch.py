# LLM: apply_patch is the generic local edit tool replacing many special mutation tools.
# 模块用途: 实现 会话运行时 风格文本补丁工具，支持新增、删除、更新和移动文件。

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._filesystem_helpers import _MAX_WRITE_TEXT_CHARS, _text_param
from ._filesystem_read import FileSystemTool
from ._filesystem_write import _atomic_write_bytes
from .models import ToolExecutionResult, ToolSpec


class ApplyPatchTool(FileSystemTool):
    # LLM: ApplyPatchTool provides one generic edit surface instead of many special file mutation tools.
    # 函数用途: 用 会话运行时 风格补丁安全地新增、删除、更新或移动文本文件。
    def __init__(self, workspace_root: Path, workspace_roots: list[Path] | None = None):
        super().__init__(workspace_root, workspace_roots)
        self.spec = ToolSpec(
            name="apply_patch",
            category="filesystem",
            effect="mutating",
            requires_idempotency=True,
            description="应用结构化文本补丁，适合局部修改、新增、删除或移动文本文件。",
            use_cases=[
                "局部修改已有代码、配置或文档",
                "一次补丁里处理多个相关文件",
            ],
            avoid_when=[
                "要完整重写一个文件时用 write_file",
                "要写 PDF、XLSX、图片等二进制文件时用 write_file 的 data_base64",
            ],
            keywords=["patch", "apply patch", "修改文件", "局部编辑", "新增文件", "删除文件"],
            parameters={"patch": "以 *** Begin Patch 开始、*** End Patch 结束的补丁文本"},
            parameter_details={
                "patch": (
                    "支持 *** Add File、*** Update File、*** Delete File、*** Move to。"
                    "新增行用 +，删除行用 -，上下文行用空格。"
                )
            },
            examples=[
                '{"tool": "apply_patch", "patch": "*** Begin Patch\\n*** Add File: notes.txt\\n+hello\\n*** End Patch\\n"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        try:
            patch = _text_param(params.get("patch"), name="patch", max_chars=_MAX_WRITE_TEXT_CHARS)
            changes = _parse_simple_patch(patch)
            touched = _apply_simple_patch(changes, self)
        except ValueError as exc:
            return ToolExecutionResult("apply_patch", False, str(exc))
        return ToolExecutionResult("apply_patch", True, "已应用补丁: " + ", ".join(touched))


# LLM: _parse_simple_patch parses the small 会话运行时 patch dialect exposed to models.
# 函数用途: 把补丁文本拆成 add/delete/update 变更列表，具体段落解析下沉到小函数。
def _parse_simple_patch(patch: str) -> list[dict[str, Any]]:
    lines = _patch_lines(patch)
    changes: list[dict[str, Any]] = []
    index = 1
    while index < len(lines) - 1:
        change, index = _parse_patch_change(lines, index)
        changes.append(change)
    if not changes:
        raise ValueError("apply_patch 没有任何变更")
    return changes


# LLM: _patch_lines validates envelope markers before detailed patch parsing.
# 函数用途: 标准化换行并检查 Begin/End Patch 边界。
def _patch_lines(patch: str) -> list[str]:
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError("apply_patch 必须以 *** Begin Patch 开始")
    if lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[-1] != "*** End Patch":
        raise ValueError("apply_patch 必须以 *** End Patch 结束")
    return lines


# LLM: _parse_patch_change dispatches one top-level patch section.
# 函数用途: 识别当前补丁段类型并返回结构化变更和下一行索引。
def _parse_patch_change(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    line = lines[index]
    if line.startswith("*** Add File: "):
        return _parse_add_file(lines, index)
    if line.startswith("*** Delete File: "):
        return {"type": "delete", "path": line.removeprefix("*** Delete File: ").strip()}, index + 1
    if line.startswith("*** Update File: "):
        return _parse_update_file(lines, index)
    raise ValueError(f"未知补丁段: {line}")


# LLM: _parse_add_file keeps add-file body validation out of the main parser.
# 函数用途: 解析 Add File 段，要求正文每行以 + 开头。
def _parse_add_file(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    path = lines[index].removeprefix("*** Add File: ").strip()
    body: list[str] = []
    index += 1
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        if not lines[index].startswith("+"):
            raise ValueError(f"新增文件 {path} 的内容行必须以 + 开头")
        body.append(lines[index][1:])
        index += 1
    content = "\n".join(body) + ("\n" if body else "")
    return {"type": "add", "path": path, "content": content}, index


# LLM: _parse_update_file keeps update/move section parsing small and explicit.
# 函数用途: 解析 Update File 段，包括可选 Move to 和上下文增删行。
def _parse_update_file(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    path = lines[index].removeprefix("*** Update File: ").strip()
    index += 1
    move_to, index = _parse_optional_move(lines, index)
    old: list[str] = []
    new: list[str] = []
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        _append_update_line(lines[index], old, new)
        index += 1
    return {"type": "update", "path": path, "move_to": move_to, "old": old, "new": new}, index


# LLM: _parse_optional_move reads the optional move target for update patches.
# 函数用途: 如果当前行是 Move to，返回目标路径并推进索引。
def _parse_optional_move(lines: list[str], index: int) -> tuple[str, int]:
    if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
        return lines[index].removeprefix("*** Move to: ").strip(), index + 1
    return "", index


# LLM: _append_update_line applies one patch content line to old/new buffers.
# 函数用途: 将上下文、删除、新增和 hunk 标记转换为旧/新文本列表。
def _append_update_line(current: str, old: list[str], new: list[str]) -> None:
    if current == "":
        old.append("")
        new.append("")
        return
    if current.startswith(" "):
        old.append(current[1:])
        new.append(current[1:])
        return
    if current.startswith("-"):
        old.append(current[1:])
        return
    if current.startswith("+"):
        new.append(current[1:])
        return
    if current.startswith("@@"):
        return
    raise ValueError(f"无法解析补丁行: {current}")


# LLM: _apply_simple_patch applies parsed patch sections in order.
# 函数用途: 顺序执行 add/delete/update，并返回被触达路径。
def _apply_simple_patch(changes: list[dict[str, Any]], tool: FileSystemTool) -> list[str]:
    touched: list[str] = []
    for change in changes:
        kind = str(change["type"])
        target = tool.resolve_path(str(change["path"]))
        if kind == "add":
            _apply_add_patch(change, target, tool, touched)
            continue
        if kind == "delete":
            _apply_delete_patch(target, tool, touched)
            continue
        if kind == "update":
            _apply_update_patch(change, target, tool, touched)
            continue
        raise ValueError(f"未知补丁类型: {kind}")
    return touched


# LLM: _apply_add_patch writes one Add File patch section.
# 函数用途: 校验目标不存在后原子写入新增文件。
def _apply_add_patch(
    change: dict[str, Any],
    target: Path,
    tool: FileSystemTool,
    touched: list[str],
) -> None:
    if target.exists():
        raise ValueError(f"新增文件已存在: {tool.display_path(target)}")
    _atomic_write_bytes(target, str(change["content"]).encode("utf-8"))
    touched.append(tool.display_path(target))


# LLM: _apply_delete_patch removes one existing file.
# 函数用途: 校验删除目标存在且是文件后删除。
def _apply_delete_patch(target: Path, tool: FileSystemTool, touched: list[str]) -> None:
    if not target.exists():
        raise ValueError(f"删除文件不存在: {tool.display_path(target)}")
    if not target.is_file():
        raise ValueError(f"删除目标不是文件: {tool.display_path(target)}")
    target.unlink()
    touched.append(tool.display_path(target))


# LLM: _apply_update_patch applies one context replacement and optional move.
# 函数用途: 校验上下文命中后写入更新内容，必要时移动文件。
def _apply_update_patch(
    change: dict[str, Any],
    target: Path,
    tool: FileSystemTool,
    touched: list[str],
) -> None:
    if not target.exists():
        raise ValueError(f"更新文件不存在: {tool.display_path(target)}")
    content = target.read_text(encoding="utf-8")
    old, new = _replacement_text(change, content, tool.display_path(target))
    updated = content.replace(old, new, 1)
    destination = _patch_destination(change, target, tool)
    _atomic_write_bytes(destination, updated.encode("utf-8"))
    if destination != target:
        target.unlink()
    touched.append(tool.display_path(destination))


# LLM: _replacement_text normalizes trailing newlines for patch context matching.
# 函数用途: 同时支持带换行和不带末尾换行的补丁上下文。
def _replacement_text(change: dict[str, Any], content: str, display_path: str) -> tuple[str, str]:
    old = "\n".join(change["old"]) + "\n"
    new = "\n".join(change["new"]) + "\n"
    if old in content:
        return old, new
    old = old.rstrip("\n")
    new = new.rstrip("\n")
    if old not in content:
        raise ValueError(f"补丁上下文未命中: {display_path}")
    return old, new


# LLM: _patch_destination resolves optional update move target.
# 函数用途: 对 update patch 的 Move to 字段做工作区路径解析。
def _patch_destination(change: dict[str, Any], target: Path, tool: FileSystemTool) -> Path:
    move_to = str(change.get("move_to") or "").strip()
    return tool.resolve_path(move_to) if move_to else target
