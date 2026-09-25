"""飞书出站渲染 — markdown 选型、post 富文本构建、长消息分片(纯函数)。

移植自 参考实现(长期助手 实战参数),解决"回复发纯 text、markdown 符号(**粗体**/列表/代码块)
原样显示难看"——把 markdown 转成飞书 post 富文本正确渲染。要点:
- 飞书 post 的 md 标签**不渲染 markdown 表格**(显示空白)→含表格降级纯文本发送。
- code fence 必须独立成 post 行,否则大段元素会吞掉 fence 后内容。
- 单条消息上限 8000 字符;分片按行边界切,fence 跨片时先闭合、下片带语言标记重开。
"""

from __future__ import annotations

import json
import re

MAX_MESSAGE_CHARS = 8000

# markdown 痕迹:标题/列表/有序列表/围栏/行内码/粗体/删除线/斜体链接/引用。
_MARKDOWN_HINT_RE = re.compile(
    r"(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(```)|(`[^`\n]+`)"
    r"|(\*\*[^*\n].*?\*\*)|(~~[^~\n].*?~~)|(\[[^\]]+\]\([^)]+\))|(^>\s)",
    re.MULTILINE,
)
_MARKDOWN_TABLE_RE = re.compile(r"^\|.*\|\s*\n\|[-|: ]+\|", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*```")


def looks_like_markdown(text: str) -> bool:
    return bool(_MARKDOWN_HINT_RE.search(text))


def has_markdown_table(text: str) -> bool:
    return bool(_MARKDOWN_TABLE_RE.search(text))


def build_outbound_payload(text: str) -> tuple[str, str]:
    """选择 (msg_type, content_json):表格→text;有 markdown→post 富文本;否则 text。"""
    if has_markdown_table(text):
        return "text", json.dumps({"text": text}, ensure_ascii=False)
    if looks_like_markdown(text):
        return "post", build_markdown_post_payload(text)
    return "text", json.dumps({"text": text}, ensure_ascii=False)


def build_markdown_post_payload(text: str) -> str:
    rows = build_markdown_post_rows(text)
    return json.dumps({"zh_cn": {"title": "", "content": rows}}, ensure_ascii=False)


def build_markdown_post_rows(text: str) -> list[list[dict[str, str]]]:
    """把 markdown 切成 post 行:code block 单独成行,散文段聚合成行。"""
    rows: list[list[dict[str, str]]] = []
    current: list[str] = []
    in_code = False

    def flush() -> None:
        nonlocal current
        chunk = "\n".join(current).strip("\n")
        if chunk.strip():
            rows.append([{"tag": "md", "text": chunk}])
        current = []

    for raw_line in text.splitlines():
        is_fence = bool(_FENCE_RE.match(raw_line.strip()))
        if is_fence and not in_code:
            flush()  # 开 fence 前:先结束散文段
        current.append(raw_line)
        if is_fence:
            in_code = not in_code
        if is_fence and not in_code:
            flush()  # 闭 fence 后:code block 单独成行
    flush()
    return rows


def strip_markdown_to_plain_text(text: str) -> str:
    """post 内容被 API 判格式错误时的纯文本回落:保语义去标记。"""
    out = text
    out = re.sub(r"```[A-Za-z0-9_+-]*\n?", "", out)
    out = re.sub(r"`([^`\n]+)`", r"\1", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"\1", out)
    out = re.sub(r"~~([^~\n]+)~~", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", out)
    out = re.sub(r"^#{1,6}\s+", "", out, flags=re.MULTILINE)
    out = re.sub(r"^>\s?", "", out, flags=re.MULTILINE)
    return out


def _wrap_line(line: str, limit: int) -> list[str]:
    """单行超长时按字符硬切成 ≤limit 段;否则原样返回。"""
    if len(line) <= limit:
        return [line]
    return [line[i : i + limit] for i in range(0, len(line), limit)]


def split_message(text: str, *, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """长文按行边界贪心合并成 ≤limit 的片;单行超长按字符硬切。返回至少一个元素。
    (从简:不做 参考实现 那种 fence 跨片闭合重开——回复>8000字符且代码块正好跨界是极边缘场景。)"""
    if len(text) <= limit:
        return [text]
    chunks = [c for line in text.split("\n") for c in _wrap_line(line, limit)]
    pieces: list[str] = []
    current = ""
    for chunk in chunks:
        candidate = f"{current}\n{chunk}" if current else chunk
        if len(candidate) > limit and current:
            pieces.append(current)
            current = chunk
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces or [text[:limit]]
