# LLM: 本模块只判断现有纯文字计量是否覆盖原生内容，不推断模型模态能力，不读取媒体或估算视觉token。
# 模块用途: 为可选模型切换及Compact共用保守的内容完整性检查；未知块保留原模型和原材料。
from __future__ import annotations

from dataclasses import dataclass


# LLM: 文字/工具可沿原计量；同后端可保留文字推理，跨模型须关闭allow_reasoning；未知模态返回False，不猜MIME能力。
# 函数用途: 在适配器可能过滤内容之前判断原块能否由现有文字协议完整表示。
def text_content_supported(content: object, *, allow_reasoning: bool = True) -> bool:
    if isinstance(content, str):
        return True
    if not isinstance(content, (list, tuple)):
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        kind = block.get("type")
        if kind == "tool_result":
            if not text_content_supported(block.get("content", ""), allow_reasoning=allow_reasoning):
                return False
        elif allow_reasoning and kind in {"thinking", "redacted_thinking"}:
            # 同一后端保留原推理信封；跨模型调用方必须关闭此许可，不能转移供应商签名。
            if any(not isinstance(value, str) for value in block.values()):
                return False
        elif kind not in {"text", "tool_use"}:
            return False
    return True


# LLM: 只认原role/content信封；额外未知字段不冒充完整容量证明，allow_reasoning沿原调用方传递。
# 函数用途: 核对完整消息列表，供摘要分段、原始历史及候选投影复用。
def text_messages_supported(messages: object, *, allow_reasoning: bool = True) -> bool:
    return isinstance(messages, (list, tuple)) and all(
        isinstance(row, dict) and not set(row) - {"role", "content"}
        and text_content_supported(row.get("content"), allow_reasoning=allow_reasoning) for row in messages
    )


# LLM: 只按结构化块类型与 source.type 分类，不读正文；已知媒体严格等于运输层会展开的集合——顶层 user 行里 source.type == local_file 的 image/video 块；
# 嵌在 tool_result 里或在 assistant 一侧的媒体块一律 unknown，保证"分类说能摘"与"投影会替换"是同一集合。
# 类用途: 一段消息里非文本块的两类计数，供压缩策略判断"能否摘要"。
@dataclass(frozen=True)
class NonTextClasses:
    media: int = 0
    unknown: int = 0


_MEDIA_BLOCK_TYPES = frozenset({"image", "video"})


# LLM: canonical 媒体块的唯一识别规则：类型为 image/video，source 为带 sha256 的 local_file 引用；base64 或未知 source 不算。
# 函数用途: 判断一个内容块是否是 owner 附件目录里的已知媒体引用。
def is_local_media_block(block: object) -> bool:
    if not isinstance(block, dict) or block.get("type") not in _MEDIA_BLOCK_TYPES:
        return False
    source = block.get("source")
    return isinstance(source, dict) and source.get("type") == "local_file" and isinstance(source.get("sha256"), str)


# LLM: 与 text_content_supported 同一遍历规则（tool_result 递归、thinking 按 allow_reasoning）；非法 content 或非 dict 块计为 unknown。
# 函数用途: 统计消息列表里的媒体块与未知块数量，不修改输入。
def classify_nontext_content(messages: object, *, allow_reasoning: bool = True) -> NonTextClasses:
    media = unknown = 0
    for row in messages if isinstance(messages, (list, tuple)) else ():
        if not isinstance(row, dict) or set(row) - {"role", "content"}:
            unknown += 1
            continue
        row_media, row_unknown = _classify_blocks(row.get("content"), allow_reasoning, media_allowed=row.get("role") == "user")
        media += row_media
        unknown += row_unknown
    return NonTextClasses(media, unknown)


def _classify_blocks(content: object, allow_reasoning: bool, *, media_allowed: bool) -> tuple[int, int]:
    if isinstance(content, str):
        return 0, 0
    if not isinstance(content, (list, tuple)):
        return 0, 1
    media = unknown = 0
    for block in content:
        block_media, block_unknown = _classify_block(block, allow_reasoning, media_allowed=media_allowed)
        media += block_media
        unknown += block_unknown
    return media, unknown


# 函数用途: 单个内容块的分类：text/tool_use 与合法 thinking 不计，tool_result 递归，已知媒体按位置放行，其余 unknown。
def _classify_block(block: object, allow_reasoning: bool, *, media_allowed: bool) -> tuple[int, int]:
    if not isinstance(block, dict):
        return 0, 1
    kind = block.get("type")
    if kind in {"text", "tool_use"}:
        return 0, 0
    if kind == "tool_result":
        return _classify_blocks(block.get("content", ""), allow_reasoning, media_allowed=False)
    if kind in {"thinking", "redacted_thinking"}:
        reasoning_ok = allow_reasoning and all(isinstance(value, str) for value in block.values())
        return (0, 0) if reasoning_ok else (0, 1)
    if media_allowed and is_local_media_block(block):
        return 1, 0
    return 0, 1


# LLM: "能否摘要"的唯一判定：unknown 块永远不能；已知媒体块在策略不为 off 时可以（A 投影为引用，B 随图摘要）。
# 函数用途: 供宿主门与分段器判断这段历史能否进入压缩链；计量是否可知另由 text_request_capacity_known 回答。
def compact_source_supported(messages: object, *, media_policy: str, allow_reasoning: bool = True) -> bool:
    classes = classify_nontext_content(messages, allow_reasoning=allow_reasoning)
    return classes.unknown == 0 and (classes.media == 0 or media_policy != "off")
