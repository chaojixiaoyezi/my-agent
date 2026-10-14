# LLM: 只转交 SDK 已验证的 actor 与真实 message/chat 身份，不传表单正文；原 Gateway 核对引用、密码和执行代次。
# 模块用途: 固定回环认证HTTP拒绝跳转； 处理私人工具审批卡，返回原卡片的就地回执；需要首设/解锁时复用原密码卡。
from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

from ..common.authenticated_http import open_authenticated_request
from ..gateway_parts.client_credentials import GatewayClientCredentials


# LLM: 原typedkind与真实actor/message/chat必须匹配，宿主凭据只入原回环HTTP且不重定向。
# 函数用途: 提交原私人审批/凭据表单并回显原真实决定；失败不落聊天或授予权限。
def tool_approval_card_action(adapter, norm: dict) -> dict | None:
    value = norm.get("value") or {}
    if value.get("kind") not in {"my_agent_tool_approval", "my_agent_task_credential"}:
        return None
    actor = str(norm.get("operator_open_id") or "")
    body = {"reference": value.get("reference"), "decision": value.get("decision"),
            "message_id": norm.get("message_id"), "chat_id": norm.get("chat_id")}
    if value["kind"] == "my_agent_task_credential":
        body["fields"] = norm.get("form_value") or {}
    if not actor or not body["message_id"] or not body["chat_id"]:
        return {"toast": {"type": "error", "content": "无法确认卡片来源，请回原私人卡片操作。"}}
    try:
        headers = {"Content-Type": "application/json", "X-Channel": "feishu", "X-User-Id": actor,
                   **GatewayClientCredentials(Path(adapter.my_agent_home)).headers()}
        request = Request(f"http://127.0.0.1:{adapter._gateway_port}/client/private-interaction",
                          data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with open_authenticated_request(request, timeout=2) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, RuntimeError, TypeError, ValueError):
        return {"toast": {"type": "error", "content": "确认未完成，请稍后重试原卡片。"}}
    if result.get("error_code") in {"IM_PASSWORD_SETUP_REQUIRED", "IM_UNLOCK_REQUIRED"}:
        adapter._send_password_card(actor, "set" if result["error_code"] == "IM_PASSWORD_SETUP_REQUIRED" else "unlock")
        return {"toast": {"type": "warning", "content": result["message"]}}
    if not result.get("ok"):
        return {"toast": {"type": "error", "content": "这张确认卡已失效，请检查原任务状态。"}}
    text = (result.get("message") or "凭据输入已处理。") if value["kind"] == "my_agent_task_credential" else ("已允许本次调用。" if result.get("decision") == "approved" else "已拒绝本次调用。")
    return {"toast": {"type": "success", "content": text}, "card": {"type": "raw", "data": {
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]}}}
