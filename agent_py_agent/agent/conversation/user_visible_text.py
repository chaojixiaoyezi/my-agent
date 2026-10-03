from __future__ import annotations

"""Keep model/runtime protocols out of text delivered to users.

The model-facing tool protocol has more than one wire spelling.  Besides the
canonical bracket/native forms, open models can downgrade tool calls to XML
or emit a tool name as the XML element itself.  Every user-facing channel must
use this module instead of maintaining a channel-specific deny list.
"""

import re
from dataclasses import dataclass

# LLM: “以这个标记开头的整行才是内部协议”的行首标记。必须按结构化形状匹配：行首（允许缩进）+
#   完整标签 + 标签后是行尾或空白。禁止退回“文本里出现该子串就算”的写法——普通代码里的
#   [run_id]、items[subagent_id]、calls[tool_call_index] 都是合法文本，不是协议。
# 常量用途: 声明会独占整行开头的内部协议标记（闭合/开标签成对出现时由块正则先处理）。
_LINE_LEADING_MARKERS = (
    "[natural-user-reply]",
    "[main_agent_",
    "[/main_agent_",
    "[run_",
    "[/run_",
    "[subagent_",
    "[/subagent_",
    # 宿主真实生成的整行运行/结果标记（context_pressure、subagents/parsing 里的全大写形态）
    # 由下面的 re.IGNORECASE 覆盖，不再单独列大写条目——大小写只有一个口径。
    "[run_context_pressure]",
    "[subagent_result]",
    "[/subagent_result]",
    "[subagent_call]",
    "[/subagent_call]",
    "[subagent_status]",
    "[/subagent_status]",
    "【逐条结论|",
)
# LLM: 括号类工具标记只在【行首】且后接闭合 ] 时算协议；标签名精确匹配，不做前缀/模糊匹配。
#   这样 <tool_call> 这类 XML 仍走各自块正则，而正文里的 [tool_call_index] 不会被误判。
_LINE_LEADING_TOOL_MARKERS = (
    "[tool_call]",
    "[/tool_call]",
    "[tool_result]",
    "[/tool_result]",
)
# LLM: XML 形态的工具标记（含属性/闭合标签）只按行首前缀认；它们由各自的成对块正则处理，
#   这里兜的是“块正则没覆盖到、但明显是协议开头”的残片。
_LINE_LEADING_XML_MARKERS = (
    "<tool_call",
    "</tool_call",
    "<tool_use",
    "</tool_use",
    "<tool_result",
    "</tool_result",
    "<function_call",
    "</function_call",
    "<function",
    "</function",
    "<function_response",
    "</function_response",
    "<minimax:tool_call",
    "</minimax:tool_call",
)
# LLM: 行首协议标记的唯一判定。分三种形状，都用 re.escape 精确匹配，不做模糊规则：
#   ① 前缀类标记（运行/子代理标签）——行首出现即协议，后面可跟标签名、可继续跟本行正文；
#   ② 完整方括号标签（工具调用/结果）——必须本行只剩空白才算协议，防止把行内提及当块首；
#   ③ XML 形态标记——按行首前缀认，属性/闭合由行内余下内容承担。
#   统一带 re.IGNORECASE：模型确实会输出全大写或大小写混写的标记（方括号工具块正则本来也是
#   大小写不敏感），只用小写口径会让大写真标记漏给用户；结构判定（行首、独占一行、成对闭标签）
#   不因大小写放宽。开放世界里除这些已知协议外，其余方括号文本一律当正文。
_LINE_LEADING_MARKER_RE = re.compile(
    r"(?m)^[ \t]*(?:"
    + "|".join(re.escape(marker) for marker in _LINE_LEADING_MARKERS)
    + r"|(?:"
    + "|".join(re.escape(marker) for marker in _LINE_LEADING_TOOL_MARKERS)
    + r")[ \t]*(?:\r?$|\r?\n)"
    + r"|(?:"
    + "|".join(re.escape(marker) for marker in _LINE_LEADING_XML_MARKERS)
    + r")"
    + r"[^\r\n]*(?:\r?$|\r?\n)"
    + r")",
    re.IGNORECASE,
)

# LLM: 方括号工具块只在【行首开标签 + 成对闭标签】时算协议；缺闭标签的旧写法会把正文里单独
#   提到的一个开标签当成块首、把后面整段吞掉（本轮误判同类）。开闭标签同在一行或跨行都算。
#   闭标签后的换行只向前看、不吃掉：块前块后的段落之间保留原来的空行，与旧投影的排版一致。
# 常量用途: 匹配以行首工具调用/结果开标签开始、到成对闭标签为止的整段。
_BRACKET_TOOL_BLOCK_RE = re.compile(
    r"(?m)^[ \t]*\[TOOL_(?:CALL|RESULT)\][^\r\n]*?"
    r".*?"
    r"\[/TOOL_(?:CALL|RESULT)\][ \t]*(?=\r?\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_FENCED_TOOL_BLOCK_RE = re.compile(
    r"^[ \t]*(?:`{3,}|~{3,})[ \t]*"
    r"(?:tool[ _-]*(?:calls?|results?|outputs?)|function[ _-]*(?:calls?|responses?))"
    r"[^\r\n]*(?:\r?\n|\Z).*?"
    r"(?:^[ \t]*(?:`{3,}|~{3,})[ \t]*(?:\r?\n|\Z)|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?P<tag>tool_calls?|tool_results?|function_calls?|function_responses?|function)\b[^>]*>"
    r".*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_XML_TOOL_BLOCK_RE = re.compile(
    r"<(?:tool_calls?|tool_results?|function_calls?|function_responses?)\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)
_BARE_FUNCTION_BLOCK_RE = re.compile(
    r"<function\s*=\s*['\"]?[A-Za-z_][A-Za-z0-9_.:-]{0,119}['\"]?\s*>"
    r".*?(?:</function\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_PLAIN_TEXT_TOOL_LINE_RE = re.compile(
    r"(?im)^[ \t]*\[tool:[A-Za-z_][A-Za-z0-9_.:-]{0,119}\][^\r\n]*(?:\r?\n|\Z)"
)
_DIRECT_XML_TOOL_OPEN_RE = re.compile(
    r"(?im)^[ \t]*<(?P<tag>[a-z][a-z0-9]*(?:[_:][a-z0-9]+)+)\b[^>]*>"
)
_MEMORY_CONTEXT_BLOCK_RE = re.compile(
    r"<memory-context\b[^>]*>.*?(?:</memory-context\s*>|\Z)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class UserVisibleTextSanitization:
    content: str
    removed_protocol: bool = False


def sanitize_user_visible_text(content: object) -> UserVisibleTextSanitization:
    """Remove internal/tool envelopes while preserving surrounding model prose."""
    text = str(content or "").strip()
    if not text:
        return UserVisibleTextSanitization(content="")

    cleaned = text
    removed = False
    for pattern in (
        _MEMORY_CONTEXT_BLOCK_RE,
        _FENCED_TOOL_BLOCK_RE,
        _BRACKET_TOOL_BLOCK_RE,
        _XML_TOOL_BLOCK_RE,
        _UNCLOSED_XML_TOOL_BLOCK_RE,
        _BARE_FUNCTION_BLOCK_RE,
        _PLAIN_TEXT_TOOL_LINE_RE,
    ):
        cleaned, count = pattern.subn("", cleaned)
        removed = removed or count > 0

    cleaned, direct_count = _strip_direct_xml_tool_blocks(cleaned)
    removed = removed or direct_count > 0

    # 只认“独占一行的内部协议标记”：命中处及其所在行起整段截断，与旧行为的剥离范围一致，
    # 但不再把 `self._run_cache[run_id] = ...` 这类含 [run_ 子串的正文误当协议。
    match = _LINE_LEADING_MARKER_RE.search(cleaned)
    if match is not None:
        cleaned = cleaned[: match.start()]
        removed = True

    if removed:
        cleaned = re.sub(r"[ \t]+(?=\r?$)", "", cleaned, flags=re.MULTILINE)
        cleaned = re.sub(r"(?:\r?\n){3,}", "\n\n", cleaned)
    return UserVisibleTextSanitization(content=cleaned.strip(), removed_protocol=removed)


def contains_internal_protocol(content: object) -> bool:
    return sanitize_user_visible_text(content).removed_protocol


def _strip_direct_xml_tool_blocks(text: str) -> tuple[str, int]:
    """Strip standalone ``<tool_name>...</tool_name>`` model downgrades.

    A colon/underscore in a line-leading element name distinguishes these
    model protocol envelopes from ordinary HTML.  A missing close tag consumes
    the remaining text so truncated tool arguments cannot reach an IM.
    """
    cleaned = text
    removed = 0
    cursor = 0
    while match := _DIRECT_XML_TOOL_OPEN_RE.search(cleaned, cursor):
        tag = match.group("tag")
        close = re.search(rf"</{re.escape(tag)}\s*>", cleaned[match.end() :], re.IGNORECASE)
        end = len(cleaned) if close is None else match.end() + close.end()
        while end < len(cleaned) and cleaned[end] in " \t\r\n":
            end += 1
        cleaned = cleaned[: match.start()] + cleaned[end:]
        removed += 1
        cursor = match.start()
    return cleaned, removed


__all__ = [
    "UserVisibleTextSanitization",
    "contains_internal_protocol",
    "sanitize_user_visible_text",
]
