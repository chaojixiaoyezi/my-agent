# LLM: 本模块只管理 TUI 文本粘贴引用的不可变快照、折叠和展开；引用是显示层结构，模型正文必须由显式展开结果提供。
# 模块用途: 把过长或多行粘贴在输入框中显示成 终端交互 风格占位符，同时完整保留实际文本供提交、stash 和恢复。

from __future__ import annotations

import re
from dataclasses import dataclass

PASTE_THRESHOLD = 800
PASTE_VISIBLE_LINE_BREAKS = 2
_PASTED_TEXT_REFERENCE = re.compile(
    r"\[Pasted text #(\d+)(?: \+(\d+) lines)?\]"
)


# LLM: TuiPastedTextRef 的 id 只在一份草稿内有意义；placeholder 与 content 一起冻结，禁止按正文重新猜行数或引用身份。
# 类用途: 保存一个折叠文本粘贴的显示占位符与原始内容。
@dataclass(frozen=True)
class TuiPastedTextRef:
    paste_id: int
    placeholder: str
    content: str

    # LLM: 引用构造必须拒绝非正 id 和不匹配自身 id 的占位符，避免提交时替换错正文。
    # 函数用途: 规范并验证一条粘贴引用。
    def __post_init__(self) -> None:
        paste_id = int(self.paste_id)
        placeholder = str(self.placeholder or "")
        content = str(self.content or "")
        match = _PASTED_TEXT_REFERENCE.fullmatch(placeholder)
        if paste_id <= 0 or match is None or int(match.group(1)) != paste_id:
            raise ValueError("invalid TUI pasted text reference")
        object.__setattr__(self, "paste_id", paste_id)
        object.__setattr__(self, "placeholder", placeholder)
        object.__setattr__(self, "content", content)


# LLM: collapse_tui_paste 只根据公开字符/换行阈值决定是否折叠；短 paste 返回原文且不制造隐藏状态。
# 函数用途: 为一次规范化后的粘贴生成可见文本和可选引用。
def collapse_tui_paste(
    text: str,
    *,
    paste_id: int,
    threshold: int = PASTE_THRESHOLD,
    visible_line_breaks: int = PASTE_VISIBLE_LINE_BREAKS,
) -> tuple[str, TuiPastedTextRef | None]:
    normalized = str(text or "")
    line_breaks = normalized.count("\n")
    if len(normalized) <= max(0, int(threshold)) and line_breaks <= max(
        0, int(visible_line_breaks)
    ):
        return normalized, None
    suffix = f" +{line_breaks} lines" if line_breaks else ""
    placeholder = f"[Pasted text #{int(paste_id)}{suffix}]"
    reference = TuiPastedTextRef(int(paste_id), placeholder, normalized)
    return placeholder, reference


# LLM: expand_tui_paste_refs 只替换当前草稿仍实际包含的已登记占位符，并按原始 offset 逆序拼接，隐藏内容里的伪占位符不能被二次解析。
# 函数用途: 将提交时的显示草稿还原为模型需要的完整正文。
def expand_tui_paste_refs(
    text: str,
    references: tuple[TuiPastedTextRef, ...],
) -> str:
    expanded = str(text or "")
    by_id = {reference.paste_id: reference for reference in references}
    matches = tuple(_PASTED_TEXT_REFERENCE.finditer(expanded))
    for match in reversed(matches):
        reference = by_id.get(int(match.group(1)))
        if reference is None or match.group(0) != reference.placeholder:
            continue
        expanded = expanded[: match.start()] + reference.content + expanded[match.end() :]
    return expanded


# LLM: merge_tui_paste_refs 保持 first-seen 次序并拒绝同 id 不同内容；跨草稿合并若需重编号必须由更高层显式完成。
# 函数用途: 合并若干草稿的粘贴引用且验证身份不冲突。
def merge_tui_paste_refs(
    groups: tuple[tuple[TuiPastedTextRef, ...], ...],
) -> tuple[TuiPastedTextRef, ...]:
    merged: list[TuiPastedTextRef] = []
    seen: dict[int, TuiPastedTextRef] = {}
    for group in groups:
        for reference in group:
            current = seen.get(reference.paste_id)
            if current is not None and current != reference:
                raise ValueError(f"conflicting TUI paste id: {reference.paste_id}")
            if current is None:
                seen[reference.paste_id] = reference
                merged.append(reference)
    return tuple(merged)


__all__ = [
    "PASTE_THRESHOLD",
    "PASTE_VISIBLE_LINE_BREAKS",
    "TuiPastedTextRef",
    "collapse_tui_paste",
    "expand_tui_paste_refs",
    "merge_tui_paste_refs",
]
