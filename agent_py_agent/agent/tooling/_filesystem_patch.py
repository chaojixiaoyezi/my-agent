
# LLM: 本模块实现 canonical apply_patch 解析、全量预检、配额核算与原子文件变更；展示 diff 不参与补丁是否允许或成功的裁决。
# 模块用途: 应用新增、更新、移动、删除文本文件的结构化补丁，并报告涉及文件及可视化差异。

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

from ..user_space.owner_quota import OwnerQuotaChange, OwnerQuotaExceeded, OwnerQuotaUnavailable
from ._filesystem_display import build_text_diff_display
from ._filesystem_helpers import _MAX_WRITE_TEXT_CHARS, _text_param
from ._filesystem_read import (
    FileSystemAccessOptions,
    FileSystemTool,
    WriteScopeError,
    owner_quota_error_result,
)
from ._filesystem_write import _atomic_write_bytes
from ._persona_write_guard import _persona_approval_write_error
from .models import (
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)

_MAX_MISSING_CONTEXT_PREVIEW_CHARS = 4_000


class PatchTargetMissingError(ValueError):
    """apply_patch 的 Update/Delete 目标文件不存在：是路径/状态问题，不是补丁格式错。

    专门区分于补丁解析错/上下文未命中(那些才是 TOOL_INVALID_ARGUMENTS，改补丁文本可修)。
    分流到 PATH_NOT_FOUND，引导模型先确认路径或用 read_file/list_files 定位，而不是
    反复重写补丁文本。仍继承 ValueError，既有 except ValueError 调用方不受影响。
    """


# LLM: The patch schema stays 会话运行时 and now routes small exact
# replacements toward edit_file; hints are selection guidance, not authority.
# 函数用途: 构建 apply_patch 的模型说明，讲清多文件补丁语法和少量局部修改的更合适入口。
def _build_apply_patch_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="apply_patch",
        description=(
            "应用 Codex 格式文本补丁。每个文件头必须写成 *** Update File: <相对路径> "
            "（冒号后一个空格），Update 的每个正文行必须以 +、- 或一个真实空格开头。"
            "例如保留末行 last line 并在后面追加 appended line：\n"
            "*** Begin Patch\n*** Update File: notes.txt\n-last line\n+last line\n"
            "+appended line\n*** End Patch"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "patch": {
                    "type": "string",
                    "description": (
                        "严格使用 Codex apply_patch 语法：第一行必须是 *** Begin Patch；"
                        "每个变更头必须把相对路径写在同一行，格式严格为 "
                        "*** Add File: <path>、*** Update File: <path> 或 "
                        "*** Delete File: <path>（冒号后有一个空格）；"
                        "仅 Update File 可紧跟 *** Move to；新增/删除/上下文行分别以 +、-、空格开头；"
                        "Update 不能是空段；末尾追加时把最后一行同时作为 -/+ 上下文，"
                        "再在 + 版本后写新增行；最后一行必须是 *** End Patch。"
                        "不要使用 ---/+++ 统一 diff。"
                    ),
                }
            },
            "required": ["patch"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=(
                "局部修改已有代码、配置或文档",
                "一次补丁里处理多个相关文件",
                "安全删除单个文本文件，使用 *** Delete File 而不是 rm/rmdir/unlink",
            ),
            avoid_when=(
                "只改已有文件的一处或少数片段时优先用 edit_file",
                "要完整重写一个文件时用 write_file",
                "要写 PDF、XLSX、图片等二进制文件时用 write_file 的 data_base64",
            ),
            keywords=("patch", "apply patch", "修改文件", "局部编辑", "新增文件", "删除文件"),
            examples=(
                '{"tool": "apply_patch", "patch": "*** Begin Patch\\n*** Update File: notes.txt\\n-last line\\n+last line\\n+appended line\\n*** End Patch\\n"}',
                '{"tool": "apply_patch", "patch": "*** Begin Patch\\n*** Update File: notes.txt\\n-old\\n+new\\n*** End Patch\\n"}',
                '{"tool": "apply_patch", "patch": "*** Begin Patch\\n*** Add File: notes.txt\\n+hello\\n*** End Patch\\n"}',
                '{"tool": "apply_patch", "patch": "*** Begin Patch\\n*** Delete File: obsolete.txt\\n*** End Patch\\n"}',
            ),
        ),
    )


class ApplyPatchTool(FileSystemTool):
    model_spec = _build_apply_patch_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        promotes_task=True,
        mutates_workspace=True,
    )

    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(
            workspace_root,
            workspace_roots,
            access_options,
        )

    # seq 253 #5：apply_patch 的写目标在 patch 文本里（Add/Update/Delete File
    # 逐目标），不在 resource_scopes 参数中——经协议从 patch 结构化解析全部
    # 目标文件，operation lock 逐目标覆盖（多文件 patch 全上锁）。
    def effective_write_roots(
        self,
        arguments: dict[str, Any],
        write_boundary: dict[str, Any] | None,
        workspace_root: Path,
    ) -> tuple[str, ...]:
        _ = write_boundary
        try:
            patch = _text_param(
                arguments.get("patch"), name="patch", max_chars=_MAX_WRITE_TEXT_CHARS
            )
            changes = _parse_simple_patch(patch)
        except (ValueError, TypeError):
            return ()
        roots: list[str] = []
        for change in changes:
            path = Path(str(change.get("path") or ""))
            if not path.is_absolute():
                path = workspace_root / path
            roots.append(str(path.resolve()))
        return tuple(roots)

    # LLM: 成功结果附带预检阶段生成的有界结构化 diff；display 仅供客户端渲染，不参与写入、配额或成功判断。
    # 函数用途: 校验并应用一份文本补丁，同时返回单文件或多文件的终端差异展示数据。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            patch = _text_param(params.get("patch"), name="patch", max_chars=_MAX_WRITE_TEXT_CHARS)
            changes = _parse_simple_patch(patch)
            if approval_error := _persona_patch_approval_error(changes, self):
                return ToolHandlerOutcome(
                    "apply_patch",
                    False,
                    approval_error,
                    error_code="PERSONA_WRITE_REQUIRES_TOOL",
                )
            quota_changes, display = _preview_patch_quota_changes(changes, self)
            with self.quota_changes(quota_changes):
                touched = _apply_simple_patch(changes, self)
        except (OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
            return owner_quota_error_result("apply_patch", exc)
        except PatchTargetMissingError as exc:
            # 目标文件不存在(Update/Delete)→PATH_NOT_FOUND(改路径/先定位)，而非
            # TOOL_INVALID_ARGUMENTS——后者会让模型反复重写补丁文本而非确认路径。
            return ToolHandlerOutcome("apply_patch", False, str(exc), error_code="PATH_NOT_FOUND")
        except WriteScopeError as exc:
            return ToolHandlerOutcome("apply_patch", False, str(exc), error_code="WRITE_FORBIDDEN")
        except ValueError as exc:
            return ToolHandlerOutcome("apply_patch", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        except OSError as exc:
            return ToolHandlerOutcome("apply_patch", False, f"补丁写入失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
        return ToolHandlerOutcome(
            "apply_patch",
            True,
            "已应用补丁: " + ", ".join(touched),
            result_envelope={
                "files_modified": list(touched),
                **({"display": display} if display else {}),
            },
        )


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


def _patch_lines(patch: str) -> list[str]:
    lines = patch.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError("apply_patch 必须以 *** Begin Patch 开始")
    if lines[-1] == "":
        lines = lines[:-1]
    if not lines or lines[-1] != "*** End Patch":
        raise ValueError("apply_patch 必须以 *** End Patch 结束")
    return lines


def _parse_patch_change(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    line = lines[index]
    if line.startswith("*** Add File: "):
        return _parse_add_file(lines, index)
    if line.startswith("*** Delete File: "):
        return {"type": "delete", "path": line.removeprefix("*** Delete File: ").strip()}, index + 1
    if line.startswith("*** Update File: "):
        return _parse_update_file(lines, index)
    raise ValueError(
        f"未知补丁段: {line}。文件头必须把相对路径写在同一行，且冒号后保留一个空格；"
        "例如：*** Update File: notes.txt。完整最小示例："
        "*** Begin Patch\\n*** Update File: notes.txt\\n-old\\n+new\\n*** End Patch"
    )


# LLM: Add headers require a non-empty relative path and at least one + line, matching 会话运行时's add_line+ grammar; empty files should use write_file explicitly.
# 函数用途: 解析新增文件段；缺路径或没有任何新增行时给模型可直接照抄的格式错误。
def _parse_add_file(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    path = lines[index].removeprefix("*** Add File: ").strip()
    if not path:
        raise ValueError("Add File 缺少相对路径；正确格式：*** Add File: notes.txt")
    body: list[str] = []
    index += 1
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        if not lines[index].startswith("+"):
            raise ValueError(f"新增文件 {path} 的内容行必须以 + 开头")
        body.append(lines[index][1:])
        index += 1
    if not body:
        raise ValueError(
            f"Add File 段不能为空: {path}；文件内容的每一行都必须以 + 开头"
        )
    content = "\n".join(body) + ("\n" if body else "")
    return {"type": "add", "path": path, "content": content}, index


# LLM: Update headers require a path and a real mutation (or an explicit move); rejecting no-op sections prevents false succeeded operations and mirrors 会话运行时's empty-hunk contract.
# 函数用途: 解析更新/移动段；空补丁不能伪装成成功，并用完整追加示例帮助模型一次修对。
def _parse_update_file(lines: list[str], index: int) -> tuple[dict[str, Any], int]:
    path = lines[index].removeprefix("*** Update File: ").strip()
    if not path:
        raise ValueError("Update File 缺少相对路径；正确格式：*** Update File: notes.txt")
    index += 1
    move_to, index = _parse_optional_move(lines, index)
    old: list[str] = []
    new: list[str] = []
    while index < len(lines) - 1 and not lines[index].startswith("*** "):
        _append_update_line(lines[index], old, new)
        index += 1
    if not move_to and old == new:
        raise ValueError(
            f"Update File 段不能为空或只有未变上下文: {path}。"
            "替换示例：-old\\n+new；末尾追加示例："
            "-last line\\n+last line\\n+appended line"
        )
    return {"type": "update", "path": path, "move_to": move_to, "old": old, "new": new}, index


def _parse_optional_move(lines: list[str], index: int) -> tuple[str, int]:
    if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
        return lines[index].removeprefix("*** Move to: ").strip(), index + 1
    return "", index


# LLM: Update line parsing remains byte-explicit and safe; malformed lines get a complete prefixed-line example instead of a vague parser error that causes blind retries.
# 函数用途: 把一行 Update 补丁分到旧内容、新内容或共同上下文；漏写前缀时直接说明空格也是语法的一部分。
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
    raise ValueError(
        f"无法解析 Update 补丁行: {current}。每行必须以 +（新增）、-（删除）"
        "或一个真实空格（未变化上下文）开头；例如：-old\\n+new。"
        "不要直接写没有前缀的文件正文。"
    )


def _apply_simple_patch(changes: list[dict[str, Any]], tool: FileSystemTool) -> list[str]:
    touched: list[str] = []
    for change in changes:
        kind = str(change["type"])
        target = tool.resolve_write_path(str(change["path"]))
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


# LLM: 预检必须按补丁顺序模拟同路径的连续变更，并同时生成与最终字节一致的 UI diff；UI 数据不能反向改变配额裁决。
# 函数用途: 在真正写盘前验证全部目标、计算配额变化，并整理单文件或多文件差异预览。
def _preview_patch_quota_changes(
    changes: list[dict[str, Any]],
    tool: FileSystemTool,
) -> tuple[list[OwnerQuotaChange], dict[str, Any]]:
    """Validate the complete text patch and project its final file sizes before mutation."""

    states: dict[Path, bytes | None] = {}
    display_files: list[dict[str, Any]] = []

    def current_bytes(path: Path) -> bytes | None:
        if path in states:
            return states[path]
        if not path.exists():
            return None
        if not path.is_file():
            raise ValueError(f"补丁目标不是文件: {tool.display_path(path)}")
        states[path] = path.read_bytes()
        return states[path]

    for change in changes:
        kind = str(change["type"])
        target = tool.resolve_write_path(str(change["path"]))
        existing = current_bytes(target)
        if kind == "add":
            if existing is not None:
                raise ValueError(f"新增文件已存在: {tool.display_path(target)}")
            added_content = str(change["content"])
            states[target] = added_content.encode("utf-8")
            display_files.append(
                build_text_diff_display(tool.display_path(target), "", added_content)
            )
            continue
        if kind == "delete":
            if existing is None:
                raise PatchTargetMissingError(f"删除文件不存在: {tool.display_path(target)}")
            deleted_content = _decode_patch_display_text(existing)
            if deleted_content is not None:
                display_files.append(
                    build_text_diff_display(tool.display_path(target), deleted_content, "")
                )
            states[target] = None
            continue
        if kind != "update":
            raise ValueError(f"未知补丁类型: {kind}")
        if existing is None:
            raise PatchTargetMissingError(f"更新文件不存在: {tool.display_path(target)}")
        content = existing.decode("utf-8")
        old, new = _replacement_text(change, content, tool.display_path(target))
        updated_text = content.replace(old, new, 1)
        updated = updated_text.encode("utf-8")
        destination = _patch_destination(change, target, tool)
        states[destination] = updated
        if destination != target:
            states[target] = None
        display_files.append(
            build_text_diff_display(
                tool.display_path(destination),
                content,
                updated_text,
            )
        )
    quota_changes = [
        OwnerQuotaChange(path, None if content is None else len(content))
        for path, content in states.items()
    ]
    return quota_changes, _patch_display(display_files)


# LLM: 删除补丁可能命中历史上允许的二进制文件；展示解码失败必须静默省略该文件，不能改变原有删除语义。
# 函数用途: 尝试把补丁目标字节解成 UTF-8 文本供差异展示。
def _decode_patch_display_text(content: bytes) -> str | None:
    try:
        return content.decode("utf-8")
    except UnicodeError:
        return None


# LLM: 单文件沿用 diff schema，多文件使用显式 patch/files 投影；数量裁剪只限制 UI envelope，不遗漏真实 files_modified。
# 函数用途: 将若干文件差异整理成有界的补丁展示结构。
def _patch_display(files: list[dict[str, Any]]) -> dict[str, Any]:
    if len(files) == 1:
        return files[0]
    if not files:
        return {}
    limit = 50
    return {
        "kind": "patch",
        "files": files[:limit],
        "hidden_files": max(0, len(files) - limit),
    }


def _persona_patch_approval_error(changes: list[dict[str, Any]], tool: FileSystemTool) -> str:
    """整份补丁先预检，避免先改普通文件、后碰 SOUL 时留下半份变更。"""
    for target in _persona_patch_paths(changes, tool):
        if error := _persona_approval_write_error(target, tool.protected_persona_root):
            return error
    return ""


def _persona_patch_paths(changes: list[dict[str, Any]], tool: FileSystemTool) -> Iterator[Path]:
    for change in changes:
        yield tool.resolve_write_path(str(change.get("path") or ""))
        if move_to := str(change.get("move_to") or "").strip():
            yield tool.resolve_write_path(move_to)


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


def _apply_delete_patch(target: Path, tool: FileSystemTool, touched: list[str]) -> None:
    if not target.exists():
        raise PatchTargetMissingError(f"删除文件不存在: {tool.display_path(target)}")
    if not target.is_file():
        raise ValueError(f"删除目标不是文件: {tool.display_path(target)}")
    target.unlink()
    touched.append(tool.display_path(target))


def _apply_update_patch(
    change: dict[str, Any],
    target: Path,
    tool: FileSystemTool,
    touched: list[str],
) -> None:
    if not target.exists():
        raise PatchTargetMissingError(f"更新文件不存在: {tool.display_path(target)}")
    content = target.read_text(encoding="utf-8")
    old, new = _replacement_text(change, content, tool.display_path(target))
    updated = content.replace(old, new, 1)
    destination = _patch_destination(change, target, tool)
    _atomic_write_bytes(destination, updated.encode("utf-8"))
    if destination != target:
        target.unlink()
    touched.append(tool.display_path(destination))


# LLM: Context mismatch feedback follows 会话运行时 by returning the exact expected
# old lines, while staying bounded and pointing small edits to edit_file.  It
# must never relax apply_patch's byte-accurate match or mutate the file on miss.
# 函数用途: 找到补丁要替换的原文；未命中时回显有界的期望行，帮助模型修正空格或改用局部编辑工具。
def _replacement_text(change: dict[str, Any], content: str, display_path: str) -> tuple[str, str]:
    old = "\n".join(change["old"]) + "\n"
    new = "\n".join(change["new"]) + "\n"
    if old in content:
        return old, new
    old = old.rstrip("\n")
    new = new.rstrip("\n")
    if old not in content:
        expected = old
        if len(expected) > _MAX_MISSING_CONTEXT_PREVIEW_CHARS:
            expected = expected[:_MAX_MISSING_CONTEXT_PREVIEW_CHARS] + "\n…（期望行已截断）"
        raise ValueError(
            f"补丁上下文未命中: {display_path}\n"
            "未找到以下补丁原始行（逐字匹配，包含空格与缩进）:\n"
            f"{expected}\n"
            "请读取目标位置的最新片段后重试；只改一处或少数片段时可改用 edit_file。"
        )
    return old, new


def _patch_destination(change: dict[str, Any], target: Path, tool: FileSystemTool) -> Path:
    move_to = str(change.get("move_to") or "").strip()
    return tool.resolve_write_path(move_to) if move_to else target
