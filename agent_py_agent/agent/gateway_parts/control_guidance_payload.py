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
    return payload
