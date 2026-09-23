# LLM: 本模块只判断现有纯文字计量是否覆盖原生内容，不推断模型模态能力，不读取媒体或估算视觉token。
# 模块用途: 在分段摘要前检查文字内容是否完整；未知块保留原材料，不把媒体引用当作已读取的正文。
from __future__ import annotations


# LLM: 本次摘要沿同后端保留文字推理信封；只判断文字内容能否完整表示，未知模态返回False。
# 函数用途: 在适配器可能过滤内容之前判断原块能否由现有文字协议完整表示。
def text_content_supported(content: object) -> bool:
    if isinstance(content, str):
        return True
    if not isinstance(content, (list, tuple)):
        return False
    for block in content:
        if not isinstance(block, dict):
            return False
        kind = block.get("type")
        if kind == "tool_result":
            if not text_content_supported(block.get("content", "")):
                return False
        elif kind in {"thinking", "redacted_thinking"}:
            # 同一后端摘要保留原文字信封；这里不提供跨模型签名迁移能力。
            if any(not isinstance(value, str) for value in block.values()):
                return False
        elif kind not in {"text", "tool_use"}:
            return False
    return True


# LLM: 只认原role/content信封；额外未知字段不冒充完整容量证明，不读取外部资源。
# 函数用途: 核对完整消息列表，供原摘要分段入口检查是否可采用纯文字来源。
def text_messages_supported(messages: object) -> bool:
    return isinstance(messages, (list, tuple)) and all(
        isinstance(row, dict) and not set(row) - {"role", "content"}
        and text_content_supported(row.get("content")) for row in messages
    )
