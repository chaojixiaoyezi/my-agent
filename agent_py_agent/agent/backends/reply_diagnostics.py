# LLM: 本模块只投影调用形状、类型和长度；不存原始对象，不参与回复、重试、授权或模型输入。
# 模块用途: 为不可交付回复提供无正文的请求级诊断，未知事实保留None而不补猜。
from __future__ import annotations

import math
import re

from ..conversation.channels import project_user_reply
from .tool_protocol_adapter import tools_for_choice


# LLM: 只接受有界协议标识，拒绝把畸形type/status/reason里的自由正文透传进诊断。
# 函数用途: 清洗供应商声明的类型和状态码，不读取任何错误说明或正文。
def _code(value: object) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) else None


# LLM: bool不是计数；未知、负值及非整数均保持None，不把缺证据伪装成真实零。
# 函数用途: 为诊断保留真实非负整数。
def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


# LLM: 仅message的output_text/refusal有可交付正文；思考、工具参数和压缩密文始终为零。
# 函数用途: 只计算一项回复的可交付字符数，不把文字放入返回值。
def _deliverable_chars(item: dict) -> int:
    if item.get("type") != "message":
        return 0
    parts = item.get("content")
    if not isinstance(parts, list):
        return 0
    texts = [_part_text(part) for part in parts if isinstance(part, dict)]
    return len(project_user_reply("".join(texts)).content)


# LLM: 文本字段只用于即时计长；不转换对象、不读summary/arguments/encrypted_content。
# 函数用途: 取得声明为正文或拒绝的字符串，供计长后立即丢弃。
def _part_text(part: dict) -> str:
    key = {"output_text": "text", "refusal": "refusal"}.get(_code(part.get("type")))
    value = part.get(key) if key else None
    return value if isinstance(value, str) else ""


# LLM: 捕获最终出站payload而非schema估算；HTTP、SSE和WS在同一原始Responses返回点投影。
# 函数用途: 保存真实tools数、宿主探测标记、压缩触发、输出类型长度及供应商终态。
def responses_call_diagnostic(obj: dict, payload: dict, request: object) -> dict:
    output = obj.get("output")
    items = [_output_item(item) for item in output if isinstance(item, dict)] if isinstance(output, list) else None
    details = obj.get("incomplete_details")
    return {
        "tools_count": len(payload.get("tools") or []),
        "is_probe": bool(getattr(request, "is_probe", False)),
        "compaction_trigger": any(item.get("type") == "compaction_trigger" for item in payload.get("input", [])),
        "output_items": items,
        "response_status": _code(obj.get("status")),
        "incomplete_reason": _code(details.get("reason")) if isinstance(details, dict) else None,
    }


# LLM: 新协议条目允许出现，保留合法声明类型；嵌套refusal按content_types留证，不复制内容。
# 函数用途: 把一项供应商输出压成类型、正文长度和内容类型列表。
def _output_item(item: dict) -> dict:
    parts = item.get("content")
    return {
        "type": _code(item.get("type")) or "unknown",
        "deliverable_text_chars": _deliverable_chars(item),
        "content_types": [_code(part.get("type")) or "unknown" for part in parts if isinstance(part, dict)]
        if isinstance(parts, list) else [],
    }


# LLM: 适配器已捕获的真实形状优先；旧/自定义后端只记录调用参数，原始输出及status未知不从归一结果反推。
# 函数用途: 把最后业务逻辑调用的无正文事实留在本轮volatile状态，辅助压缩和探测不会覆盖它。
def remember_model_call(params: object, response: object, kwargs: dict) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    call = getattr(response, "model_call_diagnostic", None)
    if not isinstance(call, dict):
        options = kwargs.get("request_options")
        call = {
            "tools_count": len(tools_for_choice(kwargs.get("tools"), kwargs.get("tool_choice"))),
            "is_probe": bool(getattr(options, "is_probe", False)),
            "compaction_trigger": bool(getattr(options, "compaction_trigger", False)),
        }
    state["_last_model_call_diagnostic"] = public_model_call_diagnostic(call)


# LLM: 运行结果和异常都必须过同一白名单；绝不透明透传额外字段或原始provider对象。
# 函数用途: 复制安全的最后调用投影，隔离调用方修改且丢掉不允许记录的内容。
def public_model_call_diagnostic(raw: object) -> dict:
    source = raw if isinstance(raw, dict) else {}
    items = source.get("output_items")
    return {
        "tools_count": _count(source.get("tools_count")),
        "is_probe": source.get("is_probe") if type(source.get("is_probe")) is bool else None,
        "compaction_trigger": source.get("compaction_trigger") if type(source.get("compaction_trigger")) is bool else None,
        "output_items": [_public_output_item(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else None,
        "response_status": _code(source.get("response_status")),
        "incomplete_reason": _code(source.get("incomplete_reason")),
    }


# LLM: 输出条目白名单只接收已计长数字和声明类型，不消费原始text字段。
# 函数用途: 清洗一条已经计长的输出诊断，防止扩展字段泄漏正文。
def _public_output_item(item: dict) -> dict:
    types = item.get("content_types")
    return {
        "type": _code(item.get("type")) or "unknown",
        "deliverable_text_chars": _count(item.get("deliverable_text_chars")),
        "content_types": [_code(value) or "unknown" for value in types] if isinstance(types, list) else [],
    }


# LLM: 只保留任务规定的五组字段；旧调用方缺失证据保持None，有限时长不接受NaN/Infinity。
# 函数用途: 为Gateway失败记录生成不会夹带正文的完整诊断字段。
def public_user_reply_diagnostic(raw: object) -> dict:
    source = raw if isinstance(raw, dict) else {}
    elapsed = source.get("seconds_since_last_compaction")
    return {
        "last_model_call": public_model_call_diagnostic(source.get("last_model_call")),
        "empty_text_repairs": _count(source.get("empty_text_repairs")),
        "empty_text_repair_limit": _count(source.get("empty_text_repair_limit")),
        "tool_rounds": _count(source.get("tool_rounds")),
        "seconds_since_last_compaction": elapsed
        if type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0 else None,
    }
