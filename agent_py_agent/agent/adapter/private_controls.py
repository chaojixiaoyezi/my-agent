# LLM: 只转交 SDK 已验证的 actor 与真实 message/chat 身份，不传表单正文；原 Gateway 核对引用、密码和执行代次。
# 模块用途: 处理私人工具审批卡，返回原卡片的就地回执；需要首设/解锁时复用原密码卡。
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request, urlopen

from ..gateway_parts.client_credentials import GatewayClientCredentials


# LLM: 未匹配该 typed kind 返回 None 让原密码/persona 处理；此 kind 的失败不可转成普通聊天或调用模型。
# 函数用途: 将一张工具审批卡提交到原 Gateway，显示真实决定或需要解锁的回执。
def tool_approval_card_action(adapter, norm: dict) -> dict | None:
    value = norm.get("value") or {}
    if value.get("kind") != "my_agent_tool_approval":
        return None
    actor = str(norm.get("operator_open_id") or "")
    body = {"reference": value.get("reference"), "decision": value.get("decision"),
            "message_id": norm.get("message_id"), "chat_id": norm.get("chat_id")}
    if not actor or not body["message_id"] or not body["chat_id"]:
        return {"toast": {"type": "error", "content": "无法确认卡片来源，请回原私人卡片操作。"}}
    try:
        headers = {"Content-Type": "application/json", "X-Channel": "feishu", "X-User-Id": actor,
                   **GatewayClientCredentials(Path(adapter.my_agent_home)).headers()}
        request = Request(f"http://127.0.0.1:{adapter._gateway_port}/client/private-interaction",
                          data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with urlopen(request, timeout=2) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"toast": {"type": "error", "content": "确认未完成，请稍后重试原卡片。"}}
    if result.get("error_code") in {"IM_PASSWORD_SETUP_REQUIRED", "IM_UNLOCK_REQUIRED"}:
        adapter._send_password_card(actor, "set" if result["error_code"] == "IM_PASSWORD_SETUP_REQUIRED" else "unlock")
        return {"toast": {"type": "warning", "content": result["message"]}}
    if not result.get("ok"):
        return {"toast": {"type": "error", "content": "这张确认卡已失效，请检查原任务状态。"}}
    text = "已允许本次调用。" if result.get("decision") == "approved" else "已拒绝本次调用。"
    return {"toast": {"type": "success", "content": text}, "card": {"type": "raw", "data": {
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]}}}
