# LLM: edit_file 默认精确且唯一匹配；空白容错只按 allow_fuzzy 显式启用，不能把字符串内空白视为无意义。
#   与 write_file/apply_patch 共享权限、原编码、原子发布及乐观版本检查；不是跨所有写入者的内核 CAS。
# 模块用途: 小范围修改已有文本，保留普通权限和原格式，并展示实际变化位置；陈旧版本要求重新读取。
from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Any

from ..common.encoding_detect import decode_bytes, encode_like_original
from ..common.file_version import StaleFileVersionError, check_file_version, file_version
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
from ._persona_write_guard import (
    _persona_approval_write_error,
    _persona_injection_write_error,
)
from .filesystem_path_recovery import MissingPathRequest, missing_path_result
from .models import (
    EffectResolverPolicy,
    IdempotencyPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)


def _build_edit_file_model_spec() -> ToolModelSpec:
    return ToolModelSpec(
        name="edit_file",
        description="把已有文本文件里的 old_string 精确替换成 new_string。改几行时首选；成功后返回实际修改处的新片段。空白容错需要显式 allow_fuzzy=true，字符串和正则中的空白可能有语义，默认不放宽。",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "要编辑的文件路径。"},
                "expected_version": {"type": "string", "description": "基于 read_file 修改时填它返回的 file_version；版本过期先重新读取合并。"},
                "old_string": {
                    "type": "string",
                    "description": (
                        "必须能在文件中唯一定位；若多处相同，请多带上下文或设 replace_all=true。"
                        "默认逐字匹配；空白不一致时先读回原文。"
                    ),
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本；允许空串以删除 old_string，并保持原有缩进风格。",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "true 时替换所有匹配处；默认只允许唯一匹配。",
                },
                "allow_fuzzy": {"type": "boolean", "default": False, "description": "显式允许空白容错；可能影响有语义的空白，默认 false。"},
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
        hints=ToolModelHints(
            category="filesystem",
            use_cases=(
                "修改已有代码/配置/文档里的一处或几处文本",
                "把 new_string 设为空串即可删除 old_string 这段",
            ),
            avoid_when=("新建文件用 write_file", "整文件重写用 write_file", "一次要改很多文件用 apply_patch"),
            keywords=("编辑", "替换", "改文件", "edit", "str_replace", "局部修改"),
            examples=(
                '{"tool": "edit_file", "path": "app.py", "old_string": "timeout = 30", "new_string": "timeout = 60"}',
                '{"tool": "edit_file", "path": "config.yaml", "old_string": "debug: true", "new_string": "debug: false", "replace_all": true}',
            ),
        ),
    )


class EditFileTool(FileSystemTool):
    model_spec = _build_edit_file_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("mutating"),
        idempotency_policy=IdempotencyPolicy("operation"),
        resource_scopes=ResourceScopePolicy(parameter_names=("path",)),
        promotes_task=True,
        mutates_workspace=True,
    )

    def __init__(
        self,
        workspace_root: Path,
        workspace_roots: list[Path] | None = None,
        access_options: FileSystemAccessOptions | None = None,
    ):
        super().__init__(workspace_root, workspace_roots, access_options)

    # LLM: 编辑成功必须同时保留模型用最新片段和有界结构化 diff；后者只供富客户端展示，不参与工具成功或文件状态判断。
    # 函数用途: 替换目标文本、原子写回文件，并返回可供终端高亮展示的增删行事实。
    def execute(self, params: dict[str, Any]) -> ToolHandlerOutcome:
        try:
            target = self.resolve_write_path(
                _text_param(params.get("path"), name="path", max_chars=4096, strip=True)
            )
            old = _text_param(params.get("old_string"), name="old_string", max_chars=_MAX_WRITE_TEXT_CHARS)
            new = _text_param(params.get("new_string"), name="new_string", max_chars=_MAX_WRITE_TEXT_CHARS, allow_empty=True)
            replace_all = bool(params.get("replace_all"))
            if old == new:
                raise ValueError("old_string 与 new_string 相同，无需编辑")
        except WriteScopeError as exc:
            return ToolHandlerOutcome("edit_file", False, str(exc), error_code="WRITE_FORBIDDEN")
        except ValueError as exc:
            return ToolHandlerOutcome("edit_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        # 文件不存在是路径/状态问题，不是"改 old_string/new_string 格式"能修的：
        # 旧实现把它和参数错混在一个 except 里报 TOOL_INVALID_ARGUMENTS，会让模型
        # 反复纠结 old_string 是否匹配，而真正该做的是先创建文件(write_file)或改对路径。
        # 分流到 PATH_NOT_FOUND(retryable，引导 list_files/search_text 重新定位)。
        if not target.exists():
            return missing_path_result(MissingPathRequest(
                tool_name="edit_file",
                raw_path=self.display_path(target),
                target=target,
                workspace_roots=self.workspace_roots,
                display_path=self.display_path(target),
                expected_kind="file",
                retry_tool="read_file",
            ))
        try:
            observed_version = check_file_version(target, params.get("expected_version"))
            original = target.read_bytes()
            content, _encoding = decode_bytes(original)
            content = content.replace("\r\n", "\n").replace("\r", "\n")
            updated, strategy, count = _replace_in_content(
                content, old, new, replace_all=replace_all, allow_fuzzy=params.get("allow_fuzzy") is True,
            )
        except StaleFileVersionError as exc:
            return ToolHandlerOutcome("edit_file", False, str(exc), error_code="STALE_VERSION", effect_outcome="not_started", retryable=True)
        except ValueError as exc:
            return ToolHandlerOutcome("edit_file", False, str(exc), error_code="TOOL_INVALID_ARGUMENTS")
        except OSError as exc:
            return ToolHandlerOutcome("edit_file", False, f"读取失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
        approval_error = _persona_approval_write_error(target, self.protected_persona_root)
        if approval_error:
            return ToolHandlerOutcome(
                "edit_file", False, approval_error, error_code="PERSONA_WRITE_REQUIRES_TOOL"
            )
        persona_error = _persona_injection_write_error(target, updated)  # 改 owner SOUL/USER/AGENTS 也过注入扫描
        if persona_error:
            return ToolHandlerOutcome("edit_file", False, persona_error, error_code="PERSONA_INJECTION_BLOCKED")
        try:
            updated_bytes = encode_like_original(updated, original)
            with self.quota_changes([OwnerQuotaChange(target, len(updated_bytes))]):
                _atomic_write_bytes(target, updated_bytes, expected_version=observed_version)
        except StaleFileVersionError as exc:
            return ToolHandlerOutcome("edit_file", False, str(exc), error_code="STALE_VERSION", effect_outcome="not_started", retryable=True)
        except (OwnerQuotaExceeded, OwnerQuotaUnavailable) as exc:
            return owner_quota_error_result("edit_file", exc)
        except (OSError, UnicodeError) as exc:
            return ToolHandlerOutcome("edit_file", False, f"写入失败: {exc}", error_code="TOOL_EXECUTION_FAILED")
        note = "" if strategy == "exact" else f"（{strategy} 容错匹配）"
        # EXEC-31: 编辑成功只回显"替换 N 处"，
        # 模型手里文件状态越改越旧 → 每次 edit 前 read_file 读回最新状态
        # (ma-a r2 真机: db.py 读 59 次、共 100+ 次读自己产物, 占 135min 的
        # 48% 轮次; 对照 fc 的 Read/Edit 会把文件内容同步进上下文缓存,
        # 会话运行时 的 apply_patch 回显 hunk 应用结果)。回显替换后的最新片段,
        # 让模型直接用它做下一次编辑, 无需读回确认。
        preview = _replaced_context_preview(content, updated, new, count)
        rendered = f"已编辑 {self.display_path(target)}：替换 {count} 处{note}\n{preview}"
        return ToolHandlerOutcome(
            "edit_file",
            True,
            rendered,
            result_envelope={
                "path": str(target),
                "target_path": str(target),
                "replacement_count": count,
                "file_version": file_version(target),
                "strategy": strategy,
                "display": build_text_diff_display(
                    self.display_path(target),
                    content,
                    updated,
                ),
            },
        )


# LLM: 默认必须精确匹配；allow_fuzzy 是显式工具参数，不从语言或文件类型推测可忽略的空白。
# 函数用途: 唯一定位并替换文本；只有明确打开容错时才尝试空白匹配。
def _replace_in_content(content: str, old: str, new: str, *, replace_all: bool, allow_fuzzy: bool = False) -> tuple[str, str, int]:
    exact_count = content.count(old)
    if exact_count > 0:
        if exact_count > 1 and not replace_all:
            raise ValueError(
                f"old_string 在文件中出现 {exact_count} 次,不唯一;请多带几行上下文使其唯一,或设 replace_all=true。"
            )
        replaced = content.replace(old, new) if replace_all else content.replace(old, new, 1)
        return replaced, "exact", (exact_count if replace_all else 1)
    if allow_fuzzy:
        return _fuzzy_replace(content, old, new, replace_all=replace_all)
    raise ValueError("old_string 未精确命中，请先 read_file 读取原文；确认空白可忽略时显式设 allow_fuzzy=true。")


_LINE_TRIM = ("line_trimmed", lambda line: line.strip())
_WS_NORM = ("whitespace_normalized", lambda line: re.sub(r"\s+", " ", line).strip())


# LLM: 编辑结果回显片段是模型免读回确认的事实源(EXEC-31)；片段取替换点
# 前后各 8 行、new_string 超长时截前 12 行，输出上限 ~30 行防刷屏。
# 函数用途: 生成"替换后文件的最新片段"文本，供模型直接用于下一次编辑。
def _replaced_context_preview(before: str, after: str, new_text: str, count: int) -> str:
    try:
        _ = new_text
        changes = difflib.SequenceMatcher(None, before.splitlines(True), after.splitlines(True), autojunk=False)
        first = next((op for op in changes.get_opcodes() if op[0] != "equal"), None)
        pos = sum(len(line) for line in after.splitlines(True)[:first[3]]) if count and first else -1
        if pos < 0:
            return ""
        start = after.rfind("\n", 0, max(pos - 1, 0)) + 1
        end_line = after.count("\n", 0, pos + len(new_text))
        lines = after.split("\n")
        head = max(0, end_line - 8)
        tail = min(len(lines), end_line + 9)
        shown = lines[head:tail]
        if len(new_text.split("\n")) > 12:
            first_lines = new_text.split("\n")[:12]
            shown = lines[head:end_line - len(new_text.split("\n")) + 1] + first_lines + lines[end_line + 1:tail] if end_line + 1 <= tail else lines[head:end_line + 1]
        body = "\n".join(shown)[:2400]
        marker = "…" if len(shown) < (tail - head) else ""
        return f"替换后最新片段（第 {head + 1}-{head + len(shown)} 行）{marker}：\n```\n{body}\n```"
    except Exception:
        return ""


# 函数用途: 精确失配时的容错替换——按行规范化(先行首尾空白,再行内空白)滑窗
#   找连续块,唯一命中即用文件里的真实行替换;多处命中且未开 replace_all 则报错。
def _fuzzy_replace(content: str, old: str, new: str, *, replace_all: bool) -> tuple[str, str, int]:
    content_lines = content.split("\n")
    old_lines = old.split("\n")
    while old_lines and old_lines[-1] == "":
        old_lines.pop()
    if not old_lines:
        raise ValueError("old_string 为空白,无法定位。")
    for label, normalizer in (_LINE_TRIM, _WS_NORM):
        spans = _matching_spans(content_lines, old_lines, normalizer)
        if not spans:
            continue
        if len(spans) > 1 and not replace_all:
            raise ValueError(
                f"容错匹配({label})在文件中找到 {len(spans)} 处,不唯一;请多带上下文或设 replace_all=true。"
            )
        new_block = new.split("\n")
        result = list(content_lines)
        applied = spans if replace_all else spans[:1]
        for start, end in reversed(applied):
            result[start:end] = _reindent(new_block, old_lines, content_lines[start:end])
        return "\n".join(result), label, len(applied)
    raise ValueError(
        "old_string 未在文件中找到(精确/行空白/全空白三级匹配均失败);"
        "请先用 read_file 确认确切文本再编辑。"
    )


# 函数用途: 取一行的前导空白(缩进)。
def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# 函数用途: 容错命中时把"真实块比 old_string 多出的公共前导缩进"补到 new 每行——
#   模型给无缩进的 old/new,在缩进代码里替换后也能保持缩进对齐(超越点)。
def _reindent(new_block: list[str], old_lines: list[str], real_lines: list[str]) -> list[str]:
    old_min = min((_leading_ws(line) for line in old_lines if line.strip()), default="", key=len)
    real_min = min((_leading_ws(line) for line in real_lines if line.strip()), default="", key=len)
    if not real_min.startswith(old_min) or len(real_min) <= len(old_min):
        return new_block
    delta = real_min[len(old_min):]
    return [delta + line if line.strip() else line for line in new_block]


# 函数用途: 用给定的行规范化函数,在内容里找出所有与 old_lines 连续匹配的行区间。
def _matching_spans(content_lines: list[str], old_lines: list[str], normalizer: Any) -> list[tuple[int, int]]:
    norm_old = [normalizer(line) for line in old_lines]
    window = len(norm_old)
    spans: list[tuple[int, int]] = []
    index = 0
    limit = len(content_lines) - window
    while index <= limit:
        if [normalizer(content_lines[index + offset]) for offset in range(window)] == norm_old:
            spans.append((index, index + window))
            index += window
        else:
            index += 1
    return spans


__all__ = ["EditFileTool"]
