# LLM: 本模块只判断现有纯文字计量是否覆盖原生内容，不推断模型模态能力，不读取媒体或估算视觉token。
# 模块用途: 为可选模型切换及Compact共用保守的内容完整性检查；未知块保留原模型和原材料。
from __future__ import annotations


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
