# LLM: 出站协议合同的唯一位置。repair_native_messages 在三个后端出口对规范原生历史（Anthropic 形态 IR）的副本做确定性修整；
#   validate_* 在最终请求体上按协议复核，不合规就抛 ProviderRequestShapeInvalidError，请求不发出。只读 role/type/id 这些结构化
#   字段，不读正文语义，不按模型名或地址猜供应商，不改调用方历史，不写盘。改规则须同步 test_wire_contract.py（逐形状 + 随机历史
#   性质测试）和 docs/design/PROVIDER_WIRE_CONTRACT.md。
# 模块用途: 防止一条坏历史让之后每次模型请求都被服务端 400 拒绝。发送前先修：空块、空消息、工具调用与结果的配对和相邻；
#   再按 Chat Completions、Anthropic Messages、Responses 各自规则检查，发现问题在本地报错，而不是反复打到服务端。
#   只有思考的助手轮属于协议差异：Chat 与 Responses 的转换各自不发，Messages 按设计照原样回放 typed 思考（MiniMax 实测接受）。

from __future__ import annotations

from typing import Any

from .errors import ProviderRequestShapeInvalidError
from .message_adapter import ORPHAN_TOOL_RESULT_STUB


# LLM: 纯函数；None 表示 text 协议，原样返回。输出是新列表，没改动的消息和块沿用原对象，调用方历史不变。规则顺序固定：先删空块
#   和删空后的消息，再按相邻关系配对工具调用与结果。被删的只影响本次请求，规范原生历史仍保留原样。
# 函数用途: 把规范原生历史修整成各协议都接受的形状，由三个后端在协议转换前调用。
def repair_native_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if messages is None:
        return None
    kept = [row for row in (_without_empty_blocks(message) for message in messages) if row is not None]
    return _pair_tool_results(kept)


# LLM: 空白正文块、既没有思考文字也没有签名的思考块不发送（Anthropic 规范拒收空白文本块）；删完没有任何块就整条不发送。
#   只在确有删除时复制消息；非 dict 行、非法内容和空内容返回 None。
# 函数用途: 去掉一条消息里服务端会拒收或毫无内容的块，整条没有内容时返回 None。
def _without_empty_blocks(message: object) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return message if content.strip() else None
    if not isinstance(content, list):
        return None
    blocks = [block for block in content if isinstance(block, dict) and not _is_empty_block(block)]
    if not blocks:
        return None
    return message if len(blocks) == len(content) else {**message, "content": blocks}


# LLM: 只看结构化类型和字段是否为空；带签名的思考块即使文字为空也保留，签名是服务端校验思考连续性的凭据。
# 函数用途: 判断一个内容块发出去会不会是会被拒收或毫无内容的空块。
def _is_empty_block(block: dict[str, Any]) -> bool:
    kind = block.get("type")
    if kind == "text":
        return not str(block.get("text") or "").strip()
    if kind == "thinking":
        return not str(block.get("thinking") or "").strip() and not block.get("signature")
    return False


# LLM: 相邻配对是三种协议的共同要求（MiniMax-M3 实测也拒绝调用与结果之间夹 user 消息）。每条带 tool_use 的助手轮之后紧跟的连续
#   user 段由 _paired_segment 重排；不跟在工具调用后面的 user 消息里的结果一定是孤儿，删除（MiniMax 实测孤儿结果 400）。
# 函数用途: 保证每个工具调用的结果紧跟在它后面，每个结果都能找到紧挨着的调用。
def _pair_tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        call_ids = _tool_use_ids(message)
        if not call_ids:
            repaired.extend(_without_tool_results(message))
            index += 1
            continue
        end = _user_segment_end(messages, index + 1)
        repaired.append(message)
        repaired.extend(_paired_segment(call_ids, messages[index + 1:end]))
        index = end
    return repaired


# LLM: 只收 assistant 里 id 非空的 tool_use，按出现顺序去重；user 消息和字符串内容返回空列表。
# 函数用途: 取出一条助手消息发起的全部工具调用 id。
def _tool_use_ids(message: dict[str, Any]) -> list[str]:
    content = message.get("content")
    if message.get("role") != "assistant" or not isinstance(content, list):
        return []
    ids = [str(block.get("id") or "") for block in content if block.get("type") == "tool_use"]
    return list(dict.fromkeys(call_id for call_id in ids if call_id))


# LLM: 段落只包含紧跟在助手轮之后、role 为 user 的连续消息；遇到 assistant 或其它角色即停止。
# 函数用途: 找到工具调用之后那段连续 user 消息的结束位置。
def _user_segment_end(messages: list[dict[str, Any]], start: int) -> int:
    end = start
    while end < len(messages) and messages[end].get("role") == "user":
        end += 1
    return end


# LLM: 找到的结果保持原出现顺序，缺失的按调用顺序补结构化"结果未知"占位（与 strip_orphaned_tool_blocks 同一占位），全部放在
#   第一条 user 消息最前面；段内插话、媒体、运行事实保持原顺序接在后面。不属于本轮调用或重复的结果删除。什么都不用动时原样
#   返回原对象，保持请求字节和前缀缓存稳定。
# 函数用途: 重排一次工具调用之后紧跟的 user 段，让本轮全部结果成为下一条 user 消息的开头。
def _paired_segment(call_ids: list[str], segment: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    rests = [_take_results(message, set(call_ids), found) for message in segment]
    results = [*found.values(), *(_missing_result(call_id) for call_id in call_ids if call_id not in found)]
    head = {**(segment[0] if segment else {"role": "user"}), "content": [*results, *(rests[0] if rests else [])]}
    rebuilt = [head, *_segment_tail(segment[1:], rests[1:])]
    return segment if rebuilt == segment else rebuilt


# LLM: found 就地登记，同一 id 只收第一次出现；返回这条消息里不是工具结果的剩余块，字符串内容视为一个文本块。
# 函数用途: 从一条 user 消息里取出本轮工具结果，返回其余内容块。
def _take_results(message: dict[str, Any], wanted: set[str], found: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rest: list[dict[str, Any]] = []
    for block in _content_blocks(message):
        if block.get("type") != "tool_result":
            rest.append(block)
            continue
        call_id = str(block.get("tool_use_id") or "")
        if call_id in wanted and call_id not in found:
            found[call_id] = block
    return rest


# LLM: 段内第二条起的 user 消息逐条交给 _tail_row，删空的不发。
# 函数用途: 生成重排后 user 段第一条之后的消息。
def _segment_tail(messages: list[dict[str, Any]], rests: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = (_tail_row(message, rest) for message, rest in zip(messages, rests))
    return [row for row in rows if row is not None]


# LLM: 原本没有工具结果的消息保持原对象（字符串内容也不改写）；有结果的换成去掉结果后的块，去空返回 None。
# 函数用途: 决定段内一条后续 user 消息重排后的样子。
def _tail_row(message: dict[str, Any], rest: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(rest) == len(_content_blocks(message)):
        return message
    return {**message, "content": rest} if rest else None


# LLM: 不紧跟工具调用的 user 消息里的结果无处配对，删除；删空则整条不发。助手消息和字符串内容原样返回。
# 函数用途: 去掉一条消息里找不到对应调用的工具结果。
def _without_tool_results(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if message.get("role") != "user" or not isinstance(content, list):
        return [message]
    rest = [block for block in content if block.get("type") != "tool_result"]
    if len(rest) == len(content):
        return [message]
    return [{**message, "content": rest}] if rest else []


# LLM: 占位内容与出站孤儿清扫共用同一常量，is_error 为真，只说明结果未知，不声称成功、失败或未执行。
# 函数用途: 为缺少结果的工具调用生成结构化"结果未知"回执。
def _missing_result(call_id: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": call_id, "content": ORPHAN_TOOL_RESULT_STUB, "is_error": True}


# LLM: 字符串内容视为一个文本块；非法内容视为空；只返回 dict 块，不复制块对象。
# 函数用途: 把一条消息的内容统一成块列表，供配对逻辑读取。
def _content_blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


# LLM: 只检查服务端确定会拒收、且出口修整保证不会产生的规则（OpenAI 规范、DeepSeek 与 MiniMax 实测）：助手消息须有正文或
#   tool_calls；带 tool_calls 的助手消息之后必须紧跟覆盖全部 id 的 tool 消息；tool 消息只能回应紧挨着的那批调用。
#   不检查未知规则，免得把服务端本可接受的请求拦在本地。
# 函数用途: 在 Chat Completions 请求发出前复核消息序列，违规时抛本地错误。
def validate_chat_messages(messages: list[dict[str, Any]]) -> None:
    pending: list[str] = []
    for index, message in enumerate(messages):
        if message.get("role") == "tool":
            call_id = str(message.get("tool_call_id") or "")
            _require(call_id in pending, "chat", "tool_reply_without_call", index)
            pending.remove(call_id)
            continue
        _require(not pending, "chat", "tool_calls_without_reply", index)
        if message.get("role") == "assistant":
            pending = _chat_call_ids(message, index)
    _require(not pending, "chat", "tool_calls_without_reply", len(messages))


# LLM: content 为 None、空串或空列表且没有 tool_calls 即违规（DeepSeek 官网对此 400，且这条历史会让之后每次请求都被拒）。
# 函数用途: 检查一条 Chat 助手消息并返回它发起的调用 id。
def _chat_call_ids(message: dict[str, Any], index: int) -> list[str]:
    calls = message.get("tool_calls") or []
    call_ids = [str(call.get("id") or "") for call in calls if isinstance(call, dict)]
    _require(bool(message.get("content")) or bool(call_ids), "chat", "assistant_without_content", index)
    return call_ids


# LLM: 依据 Anthropic Messages 规范与 MiniMax-M3 实测：role 只能是 user/assistant；列表内容不能为空；文本块须含非空白字符；
#   带 tool_use 的助手消息下一条必须是 user，且以覆盖全部 id 的 tool_result 开头；tool_result 只能回应紧挨着的上一条助手消息。
#   字符串内容是否为空不检查（旧组包在无历史时会发空 user，服务端现状接受）。
# 函数用途: 在 Messages 请求发出前复核消息序列，违规时抛本地错误。
def validate_anthropic_messages(messages: list[dict[str, Any]]) -> None:
    calls: list[str] = []
    for index, message in enumerate(messages):
        role = message.get("role")
        _require(role in ("user", "assistant"), "anthropic", "unknown_role", index)
        blocks = _anthropic_blocks(message, index)
        if role == "user":
            _check_anthropic_results(calls, blocks, index)
        else:
            _require(not calls, "anthropic", "tool_use_without_result", index)
        calls = [str(block.get("id") or "") for block in blocks if block.get("type") == "tool_use"] if role == "assistant" else []
    _require(not calls, "anthropic", "tool_use_without_result", len(messages))


# LLM: 列表内容必须非空、每项是 dict、文本块含非空白字符；字符串内容返回空列表（它不能携带工具块）。
# 函数用途: 校验并返回一条 Messages 消息的内容块。
def _anthropic_blocks(message: dict[str, Any], index: int) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return []
    _require(isinstance(content, list) and bool(content), "anthropic", "empty_content", index)
    _require(all(isinstance(block, dict) for block in content), "anthropic", "invalid_block", index)
    blank = [block for block in content if block.get("type") == "text" and not str(block.get("text") or "").strip()]
    _require(not blank, "anthropic", "blank_text_block", index)
    return content


# LLM: 结果块必须全部排在最前；前导结果的 id 集合必须恰好等于上一条助手消息的 tool_use id 集合。
# 函数用途: 检查一条 Messages user 消息里的工具结果是否与紧挨着的调用一一对应。
def _check_anthropic_results(calls: list[str], blocks: list[dict[str, Any]], index: int) -> None:
    result_ids = [str(block.get("tool_use_id") or "") for block in blocks if block.get("type") == "tool_result"]
    leading = blocks[:len(result_ids)]
    _require(all(block.get("type") == "tool_result" for block in leading), "anthropic", "tool_result_after_other_content", index)
    _require(set(calls) <= set(result_ids), "anthropic", "tool_use_without_result", index)
    _require(set(result_ids) <= set(calls), "anthropic", "tool_result_without_call", index)


# LLM: 依据 Responses 规范：reasoning 项（可连续多项）之后第一个非 reasoning 项必须是 function_call 或 assistant 消息；
#   function_call_output 必须回应此前出现的 function_call；每个 function_call 都要有输出。
# 函数用途: 在 Responses 请求发出前复核 input 项序列，违规时抛本地错误。
def validate_responses_input(items: list[dict[str, Any]]) -> None:
    calls: set[str] = set()
    answered: set[str] = set()
    for index in range(len(items)):
        _check_responses_item(items, index, calls, answered)
    _require(calls <= answered, "responses", "call_without_output", len(items))


# LLM: calls/answered 由调用方持有并在这里就地登记；其它类型的项（普通消息、未知新类型）不检查。
# 函数用途: 按类型检查一个 Responses input 项，并登记函数调用与输出。
def _check_responses_item(items: list[dict[str, Any]], index: int, calls: set[str], answered: set[str]) -> None:
    kind = items[index].get("type")
    call_id = str(items[index].get("call_id") or "")
    if kind == "reasoning":
        _require(_reasoning_has_follower(items, index), "responses", "reasoning_without_follower", index)
    if kind == "function_call":
        calls.add(call_id)
    if kind == "function_call_output":
        _require(call_id in calls, "responses", "output_without_call", index)
        answered.add(call_id)


# LLM: 跳过紧随其后的其它 reasoning 项；普通消息项没有 type 字段，按 role 识别助手消息。
# 函数用途: 判断一个 reasoning 项后面是否跟着它产出的助手消息或函数调用。
def _reasoning_has_follower(items: list[dict[str, Any]], index: int) -> bool:
    follower = next((item for item in items[index + 1:] if item.get("type") != "reasoning"), None)
    if follower is None:
        return False
    return follower.get("type") == "function_call" or (follower.get("type") in (None, "message") and follower.get("role") == "assistant")


# LLM: 违规信息只含协议、规则名和位置三个结构化字段，不含消息正文；调用方不得捕获后继续发送。
# 函数用途: 条件不成立时抛出本地请求形状错误。
def _require(condition: bool, protocol: str, rule: str, index: int) -> None:
    if not condition:
        raise ProviderRequestShapeInvalidError(protocol=protocol, rule=rule, index=index)


__all__ = [
    "repair_native_messages",
    "validate_anthropic_messages",
    "validate_chat_messages",
    "validate_responses_input",
]
