# LLM: SDK actor/真实卡身份和表单只交认证宿主回环；群凭据逐次查成员，私人控制仍核对个人锁，表单不进聊天队列。
# 模块用途: 固定回环认证HTTP拒绝跳转； 处理私人工具审批卡，返回原卡片的就地回执；需要首设/解锁时复用原密码卡。
from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request

from ..common.authenticated_http import open_authenticated_request
from ..gateway_parts.client_credentials import GatewayClientCredentials
from ..gateway_parts.local_client_token import LocalClientCredentialError

logger = logging.getLogger(__name__)


# LLM: 调用方只传本模块已核白名单或字面量，禁止传表单、回执正文或原始异常；日志故障不得阻断实际控制。
# 函数用途: 尽力记录固定阶段，让磁盘或日志处理器异常不改变原卡片调用、返回和重试。
def _log_control_stage(stage, *, reason_code="", kind=""):
    try:
        logger.info("feishu_control stage=%s reason_code=%s kind=%s", stage, reason_code, kind)
    except Exception:
        pass


# LLM: 原typedkind与真实actor/message/chat必须匹配；诊断仅记录固定kind/阶段/错误码，表单、引用和回执正文不得进入日志。
# 函数用途: 提交认证回环并记录安全处理阶段；cancel丢弃表单，群资格查询和回环是网络副作用。
def tool_approval_card_action(adapter, norm: dict) -> dict | None:
    value = norm.get("value") or {}
    if value.get("kind") not in {"my_agent_tool_approval", "my_agent_task_credential", "my_agent_model_configuration", "my_agent_feishu_oauth"}:
        return None
    _log_control_stage("entered", kind=value["kind"])
    actor = str(norm.get("operator_open_id") or "")
    body = {"reference": value.get("reference"), "decision": value.get("decision"),
            "message_id": norm.get("message_id"), "chat_id": norm.get("chat_id")}
    if value["kind"] in {"my_agent_task_credential", "my_agent_model_configuration"}:
        body["fields"] = {} if value.get("decision")=="cancel" else dict(norm.get("form_value") or {})
        if value["kind"] == "my_agent_model_configuration" and value.get("decision") not in {"save","cancel"}:
            body["fields"] = {"profile_id": str(value.get("profile_id") or "")}
    if not actor or not body["message_id"] or not body["chat_id"]:
        _log_control_stage("prepare", reason_code="IM_CONTROL_IDENTITY_MISSING")
        return {"toast": {"type": "error", "content": "无法确认卡片来源，请回原私人卡片操作。"}}
    try:
        if value["kind"] in {"my_agent_task_credential","my_agent_model_configuration"} and _group_reference_hint(body["reference"],body["decision"]):
            member = _current_group_member(adapter, str(body["chat_id"]), actor)
            if not member:
                _log_control_stage("prepare", reason_code="IM_GROUP_MEMBER_REJECTED")
                return {"toast": {"type": "error", "content": "当前账号不在原群，无法提交这张群凭据卡。"}}
            body["submitter"] = {"actor_id": actor, "chat_id": body["chat_id"], "current_group_member": member}
        result = _submit_control_request(adapter, actor, body)
    except GroupMembershipError as exc:
        reason = "IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED" if exc.code == "IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED" else "IM_GROUP_MEMBERSHIP_UNAVAILABLE"
        _log_control_stage("prepare", reason_code=reason)
        message = "群成员核验超过本次时间或分页预算，请稍后重试原卡片。" if exc.code == "IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED" else "暂时无法核验群成员，请检查机器人查看群成员权限或网络后重试原卡片。"
        return {"toast": {"type": "error", "content": message}}
    except (OSError, RuntimeError, TypeError, ValueError):
        return _control_failure_toast("IM_CONTROL_PREPARATION_FAILED", "prepare")
    if result.get("error_code") in {"IM_PASSWORD_SETUP_REQUIRED", "IM_UNLOCK_REQUIRED"}:
        _log_control_stage("interaction", reason_code=result["error_code"])
        adapter._send_password_card(actor, "set" if result["error_code"] == "IM_PASSWORD_SETUP_REQUIRED" else "unlock")
        return {"toast": {"type": "warning", "content": result["message"]}}
    if not result.get("ok"):
        return _control_failure_toast(str(result.get("error_code") or "IM_INTERACTION_INVALID"),
                                      str(result.get("error_stage") or "interaction"))
    _log_control_stage("completed", reason_code="IM_CONTROL_COMPLETED")
    text = result.get("message") or ("私人配置已处理。" if value["kind"] in {"my_agent_task_credential", "my_agent_model_configuration", "my_agent_feishu_oauth"} else "已允许本次调用。" if result.get("decision") == "approved" else "已拒绝本次调用。")
    facts = result.get("clicked_by") or result.get("cancelled_by")
    if isinstance(facts,dict) and facts.get("clicked_at") and facts["clicked_at"] not in text:
        from ..user_space.interaction_actor_audit import interaction_actor_text
        text += "\n" + interaction_actor_text(facts)
    return {"toast": {"type": "success", "content": text}, "card": {"type": "raw", "data": {
        "elements": [{"tag": "div", "text": {"tag": "plain_text", "content": text}}]}}}


# LLM: 未验签内容仅决定是否查询群成员，不授予权限；Gateway 仍重新验 MAC/owner/chat 与当前任务。
# 函数用途: 读取群呈现提示，避免个人卡额外调用群接口。
def _group_reference_hint(reference,decision=""):
    try:
        if not isinstance(reference, str) or len(reference) > 8192:
            return False
        prefix, encoded, signature = reference.split(".")
        if prefix != "ir1" or len(signature) != 64:
            return False
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        from ..user_space.interaction_refs import interaction_presentation
        presentation = interaction_presentation(payload,decision) if isinstance(payload, dict) else None
        return isinstance(presentation, dict) and presentation.get("kind") == "group"
    except (AttributeError, IndexError, TypeError, ValueError):
        return False


# LLM: 只承载固定失败码，不复制平台异常正文；调用方可区分资格阴性和无法核验，不改变授权裁决。
# 类用途: 表示成员查询未取得可靠结果，避免把权限或网络故障误说成卡片失效。
class GroupMembershipError(ValueError):
    # LLM: code 为宿主固定值，异常不含聊天、成员或表单正文。
    # 函数用途: 保存查询不可用或预算超限的结构化事实。
    def __init__(self, code="IM_GROUP_MEMBERSHIP_UNAVAILABLE"):
        super().__init__(code)
        self.code = code


# LLM: tenant token 的 is_in_chat 只能证明机器人自身，故逐页查询官方 members 接口匹配当前 operator open_id；不缓存离群资格。
# 函数用途: 每次提交最多查询十页且总预算1.5秒；故障或预算超限抛固定码，明确非成员才返回False，不记录正文。
def _current_group_member(adapter, chat_id, actor):
    from .feishu import _feishu_msg_api
    deadline = time.monotonic() + 1.5
    try:
        token = adapter._get_tenant_access_token()
    except (OSError, RuntimeError, TypeError, ValueError):
        raise GroupMembershipError() from None
    if not token or not chat_id or not actor:
        raise GroupMembershipError()
    cursor, seen = "", set()
    for _page in range(10):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise GroupMembershipError("IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED")
        params = {"member_id_type": "open_id", "page_size": 100}
        if cursor:
            params["page_token"] = cursor
        try:
            result = _feishu_msg_api("GET", "/im/v1/chats/" + quote(chat_id, safe="") + "/members?" + urlencode(params), {}, token, timeout=remaining)
        except (OSError, RuntimeError, TypeError, ValueError):
            raise GroupMembershipError() from None
        if time.monotonic() >= deadline:
            raise GroupMembershipError("IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED")
        if not isinstance(result, dict):
            raise GroupMembershipError()
        data = result.get("data")
        if result.get("code") != 0 or not isinstance(data, dict):
            raise GroupMembershipError()
        items = data.get("items")
        if not isinstance(items, list):
            raise GroupMembershipError()
        if any(item.get("member_id") == actor for item in items if isinstance(item, dict)):
            return True
        if not data.get("has_more"):
            return False
        cursor = data.get("page_token")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise GroupMembershipError()
        seen.add(cursor)
    raise GroupMembershipError("IM_GROUP_MEMBERSHIP_BUDGET_EXCEEDED")


# LLM: 展示和日志共用固定码/阶段白名单，不复制HTTP正文、异常、表单或未知错误值；不得放宽私人锁。
# 函数用途: 返回可追踪的失败回执并记录安全错误码，让真实点击与宿主故障可以对应。
def _control_failure_toast(code, stage):
    messages = {
        "IM_CONTROL_CREDENTIAL_UNAVAILABLE": "宿主认证凭据不可用，请检查适配器与Gateway使用同一数据根和配置。",
        "IM_CONTROL_HTTP_AUTH_REJECTED": "Gateway拒绝宿主认证，请检查适配器的端口、数据根及认证配置。",
        "IM_CONTROL_HTTP_ROUTE_MISSING": "Gateway没有卡片回调接口，请检查端口和运行版本。",
        "IM_CONTROL_TIMEOUT": "卡片处理超时，结果尚未确认；请重试原卡片，勿重复新增。",
        "IM_CONTROL_TRANSPORT_UNAVAILABLE": "无法连接Gateway，请检查实际端口与服务状态。",
        "IM_CONTROL_RESPONSE_INVALID": "Gateway回调响应格式无效，请检查端口与运行版本。",
        "IM_INTERACTION_HOST_UNAVAILABLE": "宿主暂时无法处理卡片，请检查存储和服务状态后重试。",
        "IM_INTERACTION_INVALID": "这张卡片的身份、版本或原会话已失效，请重新打开原控制入口。",
        "IM_INTERACTION_IN_PROGRESS": "原卡正在准备私人表单，请稍后重试；也可以取消原卡。",
        "IM_INTERACTION_DISPATCH_UNCONFIRMED": "私人表单投递结果尚未确认，请关闭原卡并重新打开模型入口。",
        "IM_INTERACTION_ALREADY_HANDLED": "这张卡片已处理，取消没有撤销或修改已完成的操作。",
        "LOCAL_CREDENTIAL_REQUIRED": "Gateway未收到有效宿主认证，请检查适配器认证配置。",
    }
    if code not in messages:
        code = "IM_CONTROL_PREPARATION_FAILED"
    if stage not in {"prepare", "credential", "http", "transport", "response", "interaction", "execution"}:
        stage = "interaction"
    _log_control_stage(stage, reason_code=code)
    return {"toast": {"type": "error", "content": messages.get(code, "卡片请求未完成，请检查宿主回调配置。") + f"（{code}/{stage}）"}}


# LLM: 端口/数据根/配置token由原宿主传入；请求只发一次且不跟随跳转，超时未知不可自动重放写操作。
# 函数用途: 发送认证卡片回调并将HTTP/凭据/格式故障转成安全固定事实，不返回底层异常或秘密。
def _submit_control_request(adapter, actor, body):
    try:
        credentials = GatewayClientCredentials(Path(adapter.my_agent_home), getattr(adapter, "_gateway_auth_token", ""))
        headers = {"Content-Type": "application/json", "X-Channel": "feishu", "X-User-Id": actor, **credentials.headers()}
    except (LocalClientCredentialError, OSError, RuntimeError, TypeError, ValueError):
        return {"ok": False, "error_code": "IM_CONTROL_CREDENTIAL_UNAVAILABLE", "error_stage": "credential"}
    try:
        request = Request(f"http://127.0.0.1:{adapter._gateway_port}/client/private-interaction",
                          data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with open_authenticated_request(request, timeout=2) as response:
            result = json.loads(response.read(65537).decode("utf-8"))
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise ValueError("invalid response")
        return result
    except HTTPError as exc:
        code = "IM_CONTROL_HTTP_AUTH_REJECTED" if exc.code in {401, 403} else "IM_CONTROL_HTTP_ROUTE_MISSING" if exc.code == 404 else "IM_CONTROL_TRANSPORT_UNAVAILABLE"
        try:
            result = json.loads(exc.read(65537).decode("utf-8"))
            if isinstance(result, dict) and result.get("error_code") in {"IM_INTERACTION_INVALID", "LOCAL_CREDENTIAL_REQUIRED", "IM_INTERACTION_HOST_UNAVAILABLE"}:
                return {"ok": False, "error_code": result["error_code"], "error_stage": "interaction" if result["error_code"] == "IM_INTERACTION_INVALID" else "credential" if result["error_code"] == "LOCAL_CREDENTIAL_REQUIRED" else "execution"}
        except (OSError, UnicodeError, ValueError):
            pass
        finally:
            exc.close()
        return {"ok": False, "error_code": code, "error_stage": "http"}
    except (TimeoutError, URLError) as exc:
        timeout = isinstance(exc, TimeoutError) or isinstance(getattr(exc, "reason", None), TimeoutError)
        return {"ok": False, "error_code": "IM_CONTROL_TIMEOUT" if timeout else "IM_CONTROL_TRANSPORT_UNAVAILABLE", "error_stage": "transport"}
    except OSError:
        return {"ok": False, "error_code": "IM_CONTROL_TRANSPORT_UNAVAILABLE", "error_stage": "transport"}
    except (UnicodeError, TypeError, ValueError):
        return {"ok": False, "error_code": "IM_CONTROL_RESPONSE_INVALID", "error_stage": "response"}
