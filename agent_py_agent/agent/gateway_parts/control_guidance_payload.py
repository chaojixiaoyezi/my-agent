# LLM: 与原control共用同一guidance正文/身份构造；纯投影，不读Store/热owner、不授写权，任何writer仍须原请求最后门。
# 模块用途: 避免普通输入运输与显式steer在字段和幂等摘要上分叉。


# LLM: scope须由调用方原认证或原prepared owner冻结；不解析正文为控制/审批，不修改传入对象或执行副作用。
# 函数用途: 生成原guidance append所需的完整结构化参数。
def control_guidance_payload(target_type, request_id, command, scope, thread_id, turn_id):
    from .control_service import _steer_channel_message_id

    channel_message_id = _steer_channel_message_id(scope)
    payload = {
        "target_type": target_type,
        "target_id": request_id,
        "message": command.value,
        "sender": scope.user_id,
        "priority": "high",
        "delivery": "current_task" if target_type == "task" else "current_request",
        "metadata": {
            "kind": "active_turn_user_input",
            "record_in_transcript": True,
            "thread_id": thread_id,
            "channel": scope.channel,
            "conversation_id": scope.conversation_id,
            "channel_message_id": channel_message_id,
            "expected_turn_id": str(turn_id or "").strip(),
            "gateway_input_request_id": str(
                scope.metadata.get("gateway_input_request_id") or ""
            ).strip(),
        },
    }
    if scope.resolved_owner is not None:
        from dataclasses import asdict

        from .request_owner_binding import REQUEST_OWNER_KEY, frozen_request_owner

        encoded = asdict(scope.resolved_owner)
        frozen_request_owner({REQUEST_OWNER_KEY: encoded})
        payload["metadata"][REQUEST_OWNER_KEY] = encoded
    if scope.input_media:
        payload["metadata"].update(_media_payload(scope, thread_id))
    return payload


# LLM: 只有 HTTP 顶层或原 prepared 的专有字段能成为媒体事实；metadata 同名输入不授文件读取权。
# 函数用途: 为原 guidance 摘要保存原宿主输入上下文及媒体小引用，不复制二进制。
def _media_payload(scope, thread_id):
    from dataclasses import asdict

    from ..conversation.host_actor import HostChannelActor
    from ..conversation.host_message_context import HostInputMediaRef, HostMessageContext
    from ..user_space.owner_resolver import _owner_id

    if scope.resolved_owner is None:
        raise ValueError("带图输入缺少冻结 owner")
    actor = HostChannelActor(_owner_id(scope.resolved_owner), thread_id,
        str(scope.metadata.get("gateway_input_request_id") or scope.metadata.get("message_id") or ""),
        scope.channel, scope.conversation_id, scope.user_id)
    context = HostMessageContext(actor, (), tuple(HostInputMediaRef(**ref) for ref in scope.input_media))
    return {"input_media": [asdict(ref) for ref in context.input_media], "host_message_context": context.to_record()}


# LLM: 当前 owner 已由原路由验证；先复用 typed context/总量规则，再有界读摘要；校验失败不能降为文字追加。
# 函数用途: 在原 guidance 首次写入之前核验归属、资源限制与实际字节一致性。
def validate_guidance_media(agent, scope, thread_id):
    if not scope.input_media:
        return
    import hashlib
    from pathlib import Path

    from ..conversation.host_message_context import HostMessageContext, context_for_input
    from ..conversation.input_media import InputMediaError

    try:
        context = HostMessageContext.from_record(_media_payload(scope, thread_id)["host_message_context"])
        context_for_input(agent, context.actor, input_media=scope.input_media)
    except (ValueError, TypeError) as error:
        raise InputMediaError("媒体引用无效，拒绝把图片降为纯文字。") from error
    for ref in scope.input_media:
        with Path(ref["path"]).open("rb") as stream:
            data = stream.read(ref["size_bytes"] + 1)
        if len(data) != ref["size_bytes"] or hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise InputMediaError("附件内容已变化，拒绝追加不一致的图片。")
