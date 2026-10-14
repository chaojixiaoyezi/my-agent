

# LLM: 飞书入站返回显式 accepted/ignored/retryable；未核验或落盘失败不得提交运输去重，原锁与 owner 仍守权限。
# 模块用途: 管理飞书受理与固定认证接口，附件上传消费已核验bytes、禁止重定向和原始错误泄露，不创建第二入站队列。
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..common.authenticated_http import open_authenticated_request
from ..common.nofollow_fs import write_bytes_atomic_beneath
from .base import BaseChannelAdapter
from .feishu_attachment import FeishuAttachmentMixin
from .feishu_media import (
    MAX_FILE_BYTES,
    MAX_IMAGE_BYTES,
    build_multipart,
    file_message_type,
    file_upload_type,
    safe_media_filename,
)
from .feishu_render import build_outbound_payload, split_message, strip_markdown_to_plain_text
from .feishu_typing import FeishuTypingMixin
from .feishu_unlock_queue import FeishuUnlockQueue, FeishuUnlockResumeMixin
from .protocol import (
    FeishuEventDisposition,
    IncomingMessage,
    OutgoingMessage,
    feishu_to_incoming,
    provider_message_reference,
)

logger = logging.getLogger(__name__)

_FEIHSU_API_BASE = "https://open.feishu.cn/open-apis"
# 幂等发送最多尝试 3 次：飞书接口偶发网络错误时重发，靠 message_id 去重防止重复投递。
_FEISHU_IDEMPOTENT_SEND_ATTEMPT_COUNT = 3
_FEISHU_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


# LLM: 标准 API 出站共用此入口；GET 不携带请求体，POST/PUT 仍发送 JSON，凭据只进认证头，不作为正文或日志。
# 函数用途: 调用机器人信息与消息接口，保持十秒运输超时及同一响应解析。
def _feishu_msg_api(method: str, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    """统一飞书消息 API 调用；GET 不携带正文，其余方法发送 JSON，返回响应对象。"""
    req = urllib.request.Request(
        f"{_FEIHSU_API_BASE}{path}",
        data=None if method == "GET" else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        method=method,
    )
    with open_authenticated_request(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# LLM: 只读取明确 message_id，不查历史；caller 必须核验 exact ID/chat/deleted/topic。凭据不进入响应事实。
# 函数用途: 用现有宿主认证单次查询被引用消息，认证不可用返回安全结构化原因。
def _fetch_message_by_id(adapter: Any, message_id: str) -> dict[str, Any]:
    from .feishu_quoted_media import FeishuQuotedMediaError

    token = adapter._get_tenant_access_token()
    if not token:
        raise FeishuQuotedMediaError('FEISHU_QUOTED_MEDIA_AUTH_UNAVAILABLE')
    return _feishu_msg_api('GET', '/im/v1/messages/' + quote(message_id, safe=''), {}, token)


def _feishu_msg_api_with_retry(
    method: str,
    path: str,
    body: dict[str, Any],
    token: str,
) -> dict[str, Any]:
    """只在请求带飞书原生 uuid 时重试传输故障；所有尝试复用同一 uuid。"""
    attempts = (
        _FEISHU_IDEMPOTENT_SEND_ATTEMPT_COUNT
        if str(body.get("uuid") or "").strip()
        else 1
    )
    for attempt in range(attempts):
        try:
            return _feishu_msg_api(method, path, body, token)
        except Exception as exc:
            if (
                attempt >= attempts - 1
                or not _feishu_retryable_send_error(exc)
            ):
                raise
            time.sleep(float(2**attempt))
    raise RuntimeError("unreachable Feishu send retry state")


def _feishu_retryable_send_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in _FEISHU_RETRYABLE_HTTP_STATUS
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, urllib.error.URLError),
    )


def _feishu_request_uuid(
    idempotency_key: str,
    *,
    component: str,
    index: int,
) -> str:
    """飞书 uuid 最长 50 字符；每个长消息分片派生独立且可重放的 48 位值。"""
    key = str(idempotency_key or "").strip()
    if not key:
        return ""
    payload = f"{key}:{component}:{max(0, int(index))}".encode()
    return hashlib.sha256(payload).hexdigest()[:48]


def _message_body(
    *,
    receive_id: str,
    msg_type: str,
    content: str,
    uuid_value: str,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": content,
    }
    if uuid_value:
        body["uuid"] = uuid_value
    return body


def _send_with_fallback(text: str, send: Callable[[str, str], dict[str, Any]]) -> bool:
    """渲染 + 发(send(msg_type, content_json)→响应);post 富文本被判格式错则剥 markdown 回落 text 重发。"""
    msg_type, content_json = build_outbound_payload(text)
    result = send(msg_type, content_json)
    if result.get("code") == 0:
        return True
    if msg_type == "post":
        fallback = json.dumps({"text": strip_markdown_to_plain_text(text)}, ensure_ascii=False)
        result = send("text", fallback)
        if result.get("code") == 0:
            return True
    logger.error('飞书发送失败 provider_code=%s', result.get('code') if type(result.get('code')) is int else None)
    return False


# LLM: 地址来自原 owner/origin 授权入口；只有两种飞书供应商编号，不能把群地址当用户或回落管理员。
# 函数用途: 为普通文字、附件和无引用进度选择准确的飞书接收编号类型。
def _message_send_path(target: str) -> str:
    if target.startswith('ou_') and len(target) > 3:
        return '/im/v1/messages?receive_id_type=open_id'
    if target.startswith('oc_') and len(target) > 3:
        return '/im/v1/messages?receive_id_type=chat_id'
    raise ValueError('CHANNEL_TARGET_INVALID')


# LLM: 普通发送按准确接收编号构造一次逻辑消息，markdown 回落仍共用原 uuid；不能改变投递目标。
# 函数用途: 向用户或原群发送渲染后的文字。
def _send_feishu_rendered(
    user_id: str,
    text: str,
    token: str,
    uuid_value: str = "",
) -> bool:
    """普通发送一条(markdown→post/否则 text;post 格式错回落 text)。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api_with_retry(
        "POST", _message_send_path(user_id),
        _message_body(
            receive_id=user_id,
            msg_type=mt,
            content=cj,
            uuid_value=uuid_value,
        ),
        token,
    ))


def _reply_feishu_rendered(
    reply_to: str,
    text: str,
    token: str,
    uuid_value: str = "",
) -> bool:
    """引用回复某条消息(渲染 + /reply 端点;post 格式错回落)。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api_with_retry(
        "POST",
        f"/im/v1/messages/{reply_to}/reply",
        {
            "msg_type": mt,
            "content": cj,
            **({"uuid": uuid_value} if uuid_value else {}),
        },
        token,
    ))


def _edit_feishu_rendered(message_id: str, text: str, token: str) -> bool:
    """编辑已发消息(PUT,流式落版/订正;post 格式错回落,编辑同一条)。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api(
        "PUT", f"/im/v1/messages/{message_id}", {"msg_type": mt, "content": cj}, token))


# LLM: 旧直接Path调用也要一次no-follow读成bytes，不在上传时重读；正式Delivery的同hash冻结内容走_upload_feishu_bytes。
# 函数用途: 保留直接附件调用的本地接口，安全读取后交同一bytes上传器；不会跟随符号链接。
def _upload_feishu(path: Path, kind: str, fields: dict[str, str], token: str) -> str | None:
    from ..common.nofollow_fs import read_bytes_beneath

    max_bytes = MAX_IMAGE_BYTES if kind == "image" else MAX_FILE_BYTES
    absolute = path.parent.resolve(strict=True) / path.name
    blob = read_bytes_beneath(Path(absolute.anchor), absolute.parts[1:], max_bytes=max_bytes, require_dir_fd=True)
    return _upload_feishu_bytes(blob, path.name, kind, fields, token) if blob is not None else None


# LLM: blob来自本次真实安全读；上传同一不可变bytes且限实际长度，固定认证接口禁止第二跳，失败日志不带API正文。
# 函数用途: 用原multipart格式上传已冻结附件字节，保留文件名、类型和供应商key，绝不重新打开path。
def _upload_feishu_bytes(blob: bytes, name: str, kind: str, fields: dict[str, str], token: str) -> str | None:
    if len(blob) > (MAX_IMAGE_BYTES if kind == 'image' else MAX_FILE_BYTES):
        return None
    body, content_type = build_multipart(fields, kind, name, blob)
    req = urllib.request.Request(f"{_FEIHSU_API_BASE}/im/v1/{kind}s", data=body,
        headers={"Content-Type": content_type, "Authorization": f"Bearer {token}"})
    with open_authenticated_request(req, timeout=30) as resp:
        result = json.loads(resp.read().decode("utf-8", "replace"))
    data = result.get("data") or {}
    key = str(data.get("image_key") or data.get("file_key") or "")
    if key:
        return key
    logger.error('飞书上传失败 provider_code=%s', result.get('code') if type(result.get('code')) is int else None)
    return None


# LLM: 附件与文字共用原地址类型判断和 uuid，群目标不能降级为管理员 open_id。
# 函数用途: 向已授权的用户或群发送图片、文件和音视频。
def _send_feishu_media(
    user_id: str,
    msg_type: str,
    content: dict[str, str],
    token: str,
    uuid_value: str = "",
) -> bool:
    """发图片/文件/音视频消息(content 含 image_key/file_key)。"""
    result = _feishu_msg_api_with_retry(
        "POST",
        _message_send_path(user_id),
        _message_body(
            receive_id=user_id,
            msg_type=msg_type,
            content=json.dumps(content, ensure_ascii=False),
            uuid_value=uuid_value,
        ),
        token,
    )
    if result.get("code") == 0:
        return True
    logger.error('飞书媒体消息发送失败 provider_code=%s', result.get('code') if type(result.get('code')) is int else None)
    return False


def _send_feishu_image(
    user_id: str,
    path: Path,
    token: str,
    idempotency_key: str = "",
) -> bool:
    key = _upload_feishu(path, "image", {"image_type": "message"}, token)
    return bool(key) and _send_feishu_media(
        user_id,
        "image",
        {"image_key": key},
        token,
        _feishu_request_uuid(idempotency_key, component="image", index=0),
    )


def _send_feishu_file(
    user_id: str,
    path: Path,
    token: str,
    idempotency_key: str = "",
) -> bool:
    fields = {"file_type": file_upload_type(path), "file_name": path.name}
    key = _upload_feishu(path, "file", fields, token)
    return bool(key) and _send_feishu_media(
        user_id,
        file_message_type(path),
        {"file_key": key},
        token,
        _feishu_request_uuid(idempotency_key, component="file", index=0),
    )


# LLM: 消息和资源 ID 都是独立 URL path 参数，不允许关闭路径；失败关闭响应，只记类型/数值状态，不输出正文或 token。
# 函数用途: 沿原消息资源 API 获取入站附件，未完成返回 None，私有文件落盘仍由原下载器负责。
def _fetch_feishu_resource(message_id: str, key: str, kind: str, token: str) -> bytes | None:
    """下载入站媒体 bytes;失败 None。"""
    req = urllib.request.Request(
        f"{_FEIHSU_API_BASE}/im/v1/messages/{quote(message_id, safe='')}/resources/{quote(key, safe='')}?type={kind}",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with open_authenticated_request(req, timeout=30) as resp:
            return resp.read()
    except Exception as exc:
        logger.warning('飞书媒体下载失败 error_type=%s http_status=%s', type(exc).__name__,
            exc.code if isinstance(exc, urllib.error.HTTPError) else 0)
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        return None


# LLM: 目录由 manager 的可信 owner 解析提供；原子私有写不跟随目标文件链接，失败保持异常给 durable worker 重试。
# 函数用途: 获取飞书附件并按 0600 写入指定 owner 目录，返回安全文件名；不使用适配器共享 cwd。
def _fetch_media_to_dir(message_id: str, media: dict[str, str], dest_dir: Path, token: str) -> str | None:
    """下载入站媒体落盘 dest_dir,返回文件名;无媒体/失败 None。"""
    if media.get("image_key"):
        key, kind, suffix = str(media["image_key"]), "image", ".png"
    elif media.get("file_key"):
        key, kind, suffix = str(media["file_key"]), "file", ".bin"
    else:
        return None
    blob = _fetch_feishu_resource(message_id, key, kind, token)
    if not blob:
        return None
    name = safe_media_filename(message_id, media, key, suffix)
    write_bytes_atomic_beneath(dest_dir, (name,), blob)
    return name


# LLM: 密码、具体审批和长期设定分属原状态源；审批按钮只承运引用，错误不能回落成聊天或批准。
# 函数用途: 分发已验证卡片事件，私人审批原地返回回执，密码与长期设定保留原流程。
def _reply_after_card_action(adapter: Any, norm: dict[str, Any]) -> dict[str, Any] | None:
    """飞书卡片回调。三类分流:
    - 会话锁密码卡:密码走 form_value(绝不进聊天/日志),处理完【返回】就地整卡替换响应
      ({toast, card:{type:raw,data:已决卡}})→ 飞书把原密码卡(含输入框+已输的密码)当场替换消失,
      密码不残留、不另发新消息。这是从 参考实现 抄全的关键:回调同步返回卡片,而非 send 一条新卡。
    - persona 人设确认卡:落写/取消后另发一条确认消息给发起人,返回 None(不走就地替换)。
    - 工具审批卡:只提交 SDK actor/消息/聊天和签名引用，Gateway 核对原审批后原地返回回执。
    fail-open,绝不抛回长连。"""
    value = norm.get("value") or {}
    password_matched, password_result = _password_card_action(adapter, norm, value)
    if password_matched:
        return password_result
    from .private_controls import tool_approval_card_action

    approval_result = tool_approval_card_action(adapter, norm)
    if approval_result is not None:
        return approval_result
    try:
        from .feishu_card import apply_card_action

        result = apply_card_action(
            value,
            Path(adapter.my_agent_home),
            operator_open_id=str(norm.get("operator_open_id") or ""),
        )
        reply_to = str(result.get("owner_id") or norm.get("operator_open_id") or "")
        text = str(result.get("reply_text") or "")
        token = adapter._get_tenant_access_token() if (text and reply_to) else None
        if token:
            _send_feishu_rendered(reply_to, text, token)
    except Exception as exc:
        logger.error('飞书卡片回调处理异常(不影响长连) error_type=%s', type(exc).__name__)


# LLM: 卡片操作用户必须匹配目标；只有原密码服务成功，首设/解锁才能按原消息队列续送，失败不改认证。
# 函数用途: 处理密码卡并就地替换为不含密码的回执，成功后恢复暂存消息。
def _password_card_action(
    adapter: Any,
    norm: dict[str, Any],
    value: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    """密码卡独立处理，返回 (是否匹配, 就地替换响应)；异常只结束该卡片回调。"""
    unlock = getattr(adapter, "_unlock", None)
    if unlock is None:
        return False, None
    try:
        from ..session_lock.feishu_cards import (
            build_password_resolved_card,
            is_password_action,
            password_action_outcome,
        )

        if not is_password_action(value):
            return False, None
        operator = str(norm.get("operator_open_id") or "").strip()
        expected_user = str(value.get("user_id") or "").strip()
        if not operator or operator != expected_user:
            return True, _password_operator_mismatch_response()
        result = password_action_outcome(unlock, value, norm.get("form_value") or {})
        resolved = build_password_resolved_card(ok=result.ok, message=result.message)
        ok = result.ok
        if ok and str(value.get("session_lock_action") or "") in {"pwd_set", "pwd_unlock"}:
            adapter._resume_pending_unlock_messages(expected_user)
        return True, {
            "toast": {"type": "success" if ok else "error", "content": "已处理" if ok else "未通过"},
            "card": {"type": "raw", "data": resolved},
        }
    except Exception as exc:
        logger.error('密码卡回调处理异常(不影响长连) error_type=%s', type(exc).__name__)
        return True, None


def _password_operator_mismatch_response() -> dict[str, Any]:
    return {
        "toast": {"type": "error", "content": "身份不匹配"},
        "card": {
            "type": "raw",
            "data": {
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": "⚠️ 未通过"},
                    "template": "grey",
                },
                "elements": [{"tag": "markdown", "content": "这张密码卡只能由对应用户操作。"}],
            },
        },
    }


# LLM: 启用时无密码、已锁或状态不可读都不能分发私聊；设置/解锁后沿原有界队列续送，群聊及显式关闭不受影响。
# 函数用途: 在正常分发前核对私聊认证状态，拦截时暂存原消息并发卡，不因卡片或数据库异常放行。
# LLM: 私聊认证和原解锁队列是唯一受理事实；服务不可读返回 retryable，只有保留消息或稳定拒绝才完成运输受理。
# 函数用途: 复用原密码门，区分待解锁、已放行与认证暂不可用，不让运输提前吞掉消息。
def _session_message_lock_disposition(adapter: Any, msg: IncomingMessage) -> FeishuEventDisposition | None:
    chat_type = str((getattr(msg, "metadata", {}) or {}).get("feishu_chat_type", ""))
    if chat_type == "group" or not getattr(adapter, "_session_lock_enabled", True):
        return None
    unlock = getattr(adapter, "_unlock", None)
    if unlock is None:
        logger.warning("私聊会话锁不可用，消息未分发")
        return FeishuEventDisposition('retryable', 'FEISHU_SESSION_LOCK_UNAVAILABLE')
    try:
        user_id = str(getattr(msg, "user_id", "") or "")
        if not user_id:
            return FeishuEventDisposition('ignored', 'FEISHU_EVENT_INVALID')
        status = unlock.status(user_id, is_group=False)
        mode = "set" if status.requires_password_setup else "unlock"
        from ..conversation.control_commands import parse_conversation_control

        control = parse_conversation_control(msg.content)
        if (status.locked or status.requires_password_setup) and control is not None and control.kind == "help":
            from ..command_catalog import CommandAccessContext, render_command_help

            token = adapter._get_tenant_access_token()
            sent = bool(token) and _send_feishu_rendered(user_id, render_command_help(
                    CommandAccessContext("im", False, private_chat=True, locked=True), control.value), token)
            return FeishuEventDisposition('accepted' if sent else 'retryable', '' if sent else 'FEISHU_INGRESS_UNAVAILABLE')
        pending = getattr(adapter, "_pending_unlock_messages", None)
        if pending is not None and pending.was_resumed(msg):
            return FeishuEventDisposition('accepted', 'FEISHU_UNLOCK_QUEUE_ACCEPTED')
        pending_status = adapter._queue_if_unlock_pending(msg)
        if pending_status != "inactive":
            adapter._report_unlock_queue_status(msg, pending_status)
            if not pending.is_draining(user_id):
                adapter._send_password_card(user_id, mode)
            return _unlock_queue_disposition(pending_status)
        if status.locked or status.requires_password_setup:
            queued = adapter._queue_locked_message(msg)
            adapter._report_unlock_queue_status(msg, queued)
            adapter._send_password_card(user_id, mode)
            return _unlock_queue_disposition(queued)
        unlock.record_activity(user_id)
        return None
    except Exception as exc:
        logger.warning("私聊会话锁状态不可读，消息未分发: %s", type(exc).__name__)
        return FeishuEventDisposition('retryable', 'FEISHU_SESSION_LOCK_UNAVAILABLE')


# LLM: 只读原队列的结构化结果，overflow 沿原用户拒绝提示收口，不把未保留消息当作接受。
# 函数用途: 把既有解锁暂存结果变成运输回执，不建立第二认证或消息状态。
def _unlock_queue_disposition(status: str) -> FeishuEventDisposition:
    if status in {'added', 'duplicate'}:
        return FeishuEventDisposition('accepted', 'FEISHU_UNLOCK_QUEUE_ACCEPTED')
    if status == 'overflow':
        return FeishuEventDisposition('ignored', 'FEISHU_UNLOCK_QUEUE_FULL')
    return FeishuEventDisposition('retryable', 'FEISHU_SESSION_LOCK_UNAVAILABLE')


# LLM: 只记录供应商短引用、类型和结构判断，不输出消息正文、显示名、token、响应文本或异常详情。
# 函数用途: 为群门、解析和消费者受理生成一条有界诊断，供现场区分没收到与没表情。
def _feishu_admission_facts(message: dict[str, Any]) -> dict[str, object]:
    kind = str(message.get('message_type') or message.get('msg_type') or '')
    return {'message_ref': provider_message_reference(message.get('message_id')),
            'chat_ref': provider_message_reference(message.get('chat_id')),
            'message_type': kind if kind in {'text', 'post', 'image', 'file', 'audio', 'media', 'video', 'sticker', 'share_chat', 'system'} else 'other',
            'chat_type': message.get('chat_type') if message.get('chat_type') in {'group', 'p2p'} else 'other',
            'bot_known': False, 'mention_gate': 'not_required'}


# LLM: 只校验供应商事件包裹，不从缺字段推断身份； malformed 交原受理入口稳定拒绝，不抛正文异常。
# 函数用途: 读取同一消息对象，保持 WS/webhook 的格式处理一致。
def _feishu_event_message(payload: object) -> dict[str, Any]:
    event = payload.get('event') if isinstance(payload, dict) else None
    message = event.get('message') if isinstance(event, dict) else None
    return message if isinstance(message, dict) else {}


# LLM: 此处只接原解析、认证、派发入口；受理以 callback 正常返回为准，显式 False 必须允许供应商重投。
# 函数用途: 完成一条已验证飞书事件的受理判断，把暂时失败留给原运输和入站幂等恢复。
def _admit_feishu_event(adapter: Any, payload: dict[str, Any], facts: dict[str, object]) -> FeishuEventDisposition:
    from .feishu_mentions import bot_mention_decision

    message = _feishu_event_message(payload)
    if not isinstance(message, dict) or not message.get('message_id'):
        return FeishuEventDisposition('ignored', 'FEISHU_EVENT_INVALID')
    bot = adapter._resolve_bot_open_id() if message.get('chat_type') == 'group' else ''
    facts['bot_known'] = bool(bot)
    facts['bot_retry_in_seconds'] = max(0, round(getattr(adapter, '_bot_identity_retry_at', 0.0) - time.monotonic())) if not bot else 0
    if message.get('chat_type') == 'group' and adapter.group_require_bot_mention:
        decision = bot_mention_decision(message, bot)
        facts.update(mention_gate='matched' if decision.allowed else 'unmatched' if decision.verified else 'unverified',
                     mentions_count=decision.mentions_count, post_at_count=decision.post_at_count)
        if not decision.allowed:
            return FeishuEventDisposition('ignored' if decision.verified else 'retryable', decision.reason_code)
    msg = feishu_to_incoming(payload, bot_open_id=bot)
    if msg is None:
        return FeishuEventDisposition('ignored', 'FEISHU_MESSAGE_EMPTY')
    if not msg.user_id or not msg.conversation_id:
        return FeishuEventDisposition('ignored', 'FEISHU_EVENT_INVALID')
    lock_result = _session_message_lock_disposition(adapter, msg)
    if lock_result is not None:
        return lock_result
    if adapter._dispatch(msg) is False:
        return FeishuEventDisposition('retryable', 'FEISHU_INGRESS_UNAVAILABLE')
    return FeishuEventDisposition('accepted')


# LLM: 密码卡发送失败只能影响 onboarding/解锁提示，不得抛异常破坏 adapter 消息循环。
# 函数用途: 构造并发送设置或解锁密码卡，返回飞书是否接收成功。
def _send_session_password_card(adapter: Any, user_id: str, mode: str) -> bool:
    try:
        from ..session_lock.feishu_cards import build_password_card
        from .feishu_card import send_interactive_card

        card = build_password_card(mode=mode, user_id=user_id)
        return bool(send_interactive_card(adapter.app_id, adapter.app_secret, user_id, card))
    except Exception as exc:
        logger.warning('密码卡发送失败 error_type=%s', type(exc).__name__)
        return False


# LLM: 原 send/reply 已派生最长 50 字符的分片 uuid；本声明只供 delivery 在有效窗口内恢复同一逻辑消息。
# 类用途: 提供飞书发送、回复与准确消息/媒体读取；没有 UUID 到消息的只读查询保证。
class FeishuAdapter(FeishuUnlockResumeMixin, FeishuTypingMixin, FeishuAttachmentMixin, BaseChannelAdapter):
    # LLM: uuid 是有限窗口去重，不覆盖跨窗口重发；channel delivery 必须先查首次 dispatch 时间。
    # 字段用途: 向统一通道注册表声明 provider 原生幂等能力。
    provider_idempotent_delivery = True
    provider_delivery_queryable = False
    provider_idempotency_window_seconds = 3600.0

    adapter_name = "feishu"
    supports_edit_progress = True
    supports_control_cards = True

    # LLM: 创建原通道状态、认证服务和有界待送队列；显式关闭与初始化失败分开，不把未知状态当作认证成功。
    # 函数用途: 初始化飞书接入、引用文字/附件开关与私聊密码门，不启动消息服务或发送网络请求。
    def __init__(
        self,
        config: dict[str, Any],
        callback_port: int = 8421,
        workspace_root: Path | None = None,
    ) -> None:
        super().__init__(config)
        self.callback_port = callback_port
        self.workspace_root = workspace_root or Path.cwd()

        self.app_id = config.get("feishu_app_id", "")
        self.app_secret = config.get("feishu_app_secret", "")
        self.verification_token = config.get("feishu_verification_token", "")
        self.encrypt_key = config.get("feishu_encrypt_key", "")
        # my_agent_home 根:卡片按钮回调据此读待确认记录、定位 owner 的 SOUL.md(与网关同一根)。
        self.my_agent_home = str(config.get("my_agent_home", "") or "")
        self._gateway_port = int(config.get("gateway_port", 8420))
        # 默认长连接:免公网且能接密码/人格确认卡片回调；webhook 仍可显式选择。
        self.connection_mode = str(
            config.get("feishu_connection_mode", "long_connection") or "long_connection"
        ).strip().lower()
        self.ws_proxy = config.get("feishu_ws_proxy", "")
        self.group_require_bot_mention = str(config.get("feishu_group_require_bot_mention", True)).lower() not in {"false", "0", "off"}
        self.quoted_media_enabled = str(config.get('feishu_quoted_media_enabled', True)).lower() not in {'false', '0', 'off'}
        self.quoted_text_enabled = str(config.get('feishu_quoted_text_enabled', True)).lower() not in {'false', '0', 'off'}
        self._bot_open_id = ""
        self._bot_identity_app_id = ""
        self._bot_identity_retry_at = 0.0
        self._bot_identity_lock = threading.Lock()

        self._server: ThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0

        # 个人私聊会话锁默认开启；显式 false 才关闭。首次私聊要求设置密码，
        # 未设置密码也暂存原消息；群聊不受个人锁影响。初始化失败不允许私聊越过认证门。
        self._session_lock_enabled = str(config.get("feishu_session_lock_enabled", True)).strip().lower() not in {"0", "false", "no", "off"}
        self._unlock = self._init_session_lock(config)
        self._pending_unlock_messages = (
            FeishuUnlockQueue() if self._unlock is not None else None
        )


    # LLM: 开关由构造时显式配置固定；缺数据根或数据库失败返回不可用，由锁门拒绝私聊，不退化成关闭。
    # 函数用途: 打开原私聊密码库与解锁服务，失败只记类型，不记录凭据或回调正文。
    def _init_session_lock(self, config: dict[str, Any]) -> Any:
        if not self._session_lock_enabled:
            return None
        if not self.my_agent_home:
            return None
        try:
            from ..session_lock import DEFAULT_IDLE_SECONDS, SessionLockStore, UnlockService

            try:
                idle = int(config.get("feishu_personal_idle_lock_seconds", DEFAULT_IDLE_SECONDS) or DEFAULT_IDLE_SECONDS)
            except (TypeError, ValueError):
                idle = DEFAULT_IDLE_SECONDS
            db_path = Path(self.my_agent_home) / "session_lock" / "private_chat_locks.db"
            return UnlockService(SessionLockStore(db_path), idle_limit_seconds=idle)
        except Exception as exc:
            logger.warning("会话锁初始化失败，私聊将等待服务恢复: %s", type(exc).__name__)
            return None


    def start(self) -> None:
        if self._running:
            return
        with self._lock:
            if self._running:
                return
            self._stop_event.clear()
            if self._is_long_connection():
                self._start_long_connection()
            else:
                self._start_webhook_server()
            self._running = True

    def _is_long_connection(self) -> bool:
        return self.connection_mode in ("long_connection", "longconn", "ws", "websocket")

    def _start_webhook_server(self) -> None:
        self._server = ThreadingHTTPServer(
            ("0.0.0.0", self.callback_port),
            _FeishuCallbackHandler,
        )
        self._server.adapter = self  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()
        logger.info(f"飞书适配器已启动(webhook 模式)，回调端口={self.callback_port}")

    def _start_long_connection(self) -> None:
        # 长连接 WS:主动连飞书网关(免公网/不绑端口),事件走与 webhook 同一条下游(_handle_feishu_event);
        # 异常退出回调置 _running=False,让健康探测/supervisor 感知通道死、触发重启。
        from .feishu_ws import FeishuWsClient, run_ws_client_thread

        self._ws_client = FeishuWsClient(
            app_id=self.app_id, app_secret=self.app_secret,
            on_payload=self._handle_feishu_event, ws_proxy=self.ws_proxy,
        )
        # 有 my_agent_home 才挂卡片回调(否则无处定位待确认记录/人格文件);无则不注册,行为不变。
        self._ws_client.on_card_action = self._handle_card_action if self.my_agent_home else None
        self._ws_thread = run_ws_client_thread(self._ws_client, lambda: setattr(self, "_running", False))
        logger.info("飞书适配器已启动(长连接 WS 模式,免公网/不绑端口)")

    # LLM: 原回调服务异常只记类型，不能回显消息/认证正文，不以日志推断已受理。
    # 函数用途: 维持原HTTP回调服务生命周期并安全记录失败。
    def _serve(self) -> None:
        if self._server is None:
            return
        try:
            self._server.serve_forever()
        except Exception as exc:
            # 原 except:pass 把异常静默吞掉 + _running 仍 True → 通道无声死亡而健康探测/supervisor 误判在线
            # (审计 #15)。记录异常并置 _running=False,让 running/状态检查感知它已死、可触发重启。
            logger.error('飞书回调服务器异常退出(通道已死,需重启) error_type=%s', type(exc).__name__)
            self._running = False

    def stop(self) -> None:
        if not self._running:
            return
        with self._lock:
            self._running = False
            self._stop_event.set()
            self._shutdown_server_async()  # webhook 模式关 HTTP server(长连模式 _server=None,空操作)
            self._join_server_thread()
            self._stop_ws_client()  # 长连模式:尽力关闭 ws 客户端(lark 无干净 stop,daemon 线程随进程退出)
            logger.info("飞书适配器已停止")

    def _stop_ws_client(self) -> None:
        client, self._ws_client = self._ws_client, None
        if client is not None:
            client.close()  # 尽力关闭底层 ws(关不掉就靠 daemon 线程随进程退出)

    # LLM: 只关闭原HTTP服务，异常日志只类型；不改timeout/线程退出或事件受理事实。
    # 函数用途: 在原关闭线程退出回调服务，保留既有生命周期清理。
    def _shutdown_server_async(self) -> None:
        if not self._server:
            return
        try:
            t = threading.Thread(target=self._do_shutdown, daemon=True)
            t.start()
        except Exception as exc:
            logger.warning('关闭飞书 HTTP 服务异常 error_type=%s', type(exc).__name__)
        self._server = None

    def _join_server_thread(self) -> None:
        if not self._server_thread:
            return
        self._server_thread.join(timeout=5)
        self._server_thread = None

    # LLM: 原服务关闭异常只记类型，不能从异常文本复制回调或认证数据。
    # 函数用途: 关闭原服务连接，不改变外发或owner状态。
    def _do_shutdown(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:
                logger.warning('飞书 HTTP shutdown 异常 error_type=%s', type(exc).__name__)


    # LLM: callback 结果必须显式返给 WS/webhook；异常仅记类型，暂时失败不伪装接受或永久忽略。
    # 函数用途: 经原群门与认证入口接收消息，返回运输回执并记录有界脱敏状态。
    def _handle_feishu_event(self, payload: dict[str, Any]) -> FeishuEventDisposition:
        message = _feishu_event_message(payload)
        facts = _feishu_admission_facts(message if isinstance(message, dict) else {})
        try:
            result = _admit_feishu_event(self, payload, facts)
        except Exception as exc:
            facts['error_type'] = type(exc).__name__
            result = FeishuEventDisposition('retryable', 'FEISHU_INGRESS_UNAVAILABLE')
        logger.info('feishu_ingress %s', json.dumps({**facts, 'status': result.status, 'reason_code': result.reason_code}, separators=(',', ':')))
        return result

    # LLM: 身份绑定 app_id，API 失败按单调时间节流；只缓存 open_id，不记录 token、响应正文或用户内容。
    # 函数用途: 向飞书机器人信息接口取得本应用的 open_id，群聊判断不从昵称猜机器人。
    def _resolve_bot_open_id(self) -> str:
        with self._bot_identity_lock:
            if self._bot_identity_app_id != self.app_id:
                self._bot_identity_app_id = self.app_id
                self._bot_open_id = ""
                self._bot_identity_retry_at = 0.0
            if self._bot_open_id or time.monotonic() < self._bot_identity_retry_at:
                return self._bot_open_id
            self._bot_identity_retry_at = time.monotonic() + 30.0
            try:
                token = self._get_tenant_access_token()
                if not token:
                    return ""
                result = _feishu_msg_api("GET", "/bot/v3/info", {}, token)
                bot = result.get("bot") or (result.get("data") or {}).get("bot") or {}
                if result.get("code") == 0 and isinstance(bot, dict):
                    self._bot_open_id = str(bot.get("open_id") or "")
            except (OSError, ValueError, TypeError):
                logger.warning("飞书机器人身份暂不可用；群聊未派发，30 秒后可重试")
            return self._bot_open_id

    # LLM: 保留既有 bool 认证接口；实际运输使用同一 typed lock disposition 区分暂时失败，不绕过权限。
    # 函数用途: 供原解锁调用方判断是否阻止直接分发，群聊和显式关闭仍放行。
    def _session_locked_gate(self, msg: IncomingMessage) -> bool:
        """个人私聊会话锁门。

        未设置密码与闲置锁定都暂存原消息，设置/解锁成功后续送。
        群聊、显式关闭不拦截；启用但服务异常时拒绝私聊分发。
        """
        return _session_message_lock_disposition(self, msg) is not None

    def _send_password_card(self, user_id: str, mode: str) -> bool:
        return _send_session_password_card(self, user_id, mode)

    def _handle_card_action(self, norm: dict[str, Any]) -> dict[str, Any] | None:
        """飞书卡片按钮回调(长连):逻辑在模块级 _reply_after_card_action。密码卡返回就地替换响应
        (dict,交 WS 包成 P2CardActionTriggerResponse 同步返回飞书);persona 卡另发消息、返回 None。"""
        return _reply_after_card_action(self, norm)


    # LLM: 正文来自统一投递的净化结果；认证运输不重定向，异常只输出类型，不回显供应商或用户内容。
    # 函数用途: 沿原富文本/分片/uuid通路发送消息，不保存第二份投递记录。
    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        try:
            token = self._get_tenant_access_token()
            if not token:
                logger.error("飞书: 无法获取 tenant_access_token")
                return False
            # markdown→post 渲染 + 长消息(>8000字符)分片;逐片全发(list 不短路),任一失败即整体失败。
            pieces = split_message(message.content)
            idempotency_key = str(
                (message.metadata or {}).get("delivery_idempotency_key") or ""
            )
            results = [
                _send_feishu_rendered(
                    user_id,
                    piece,
                    token,
                    _feishu_request_uuid(
                        idempotency_key,
                        component="message",
                        index=index,
                    ),
                )
                for index, piece in enumerate(pieces)
            ]
            return all(results)
        except Exception as exc:
            logger.error('飞书 send_message 异常 error_type=%s', type(exc).__name__)
            return False

    def reply_message(
        self,
        message_id: str,
        text: str,
        idempotency_key: str = "",
    ) -> bool:
        """引用回复某条消息；长结果逐片引用，任一片失败则整体失败。"""
        token = self._get_tenant_access_token()
        if not token:
            return False
        # 与主动发送共用同一平台长度边界。不能让普通消息会分片、Gateway 最终引用回复
        # 却把整篇长报告一次交给 Feishu API，否则长任务已经完成仍会在最后一步投递失败。
        pieces = split_message(text)
        results = []
        for index, piece in enumerate(pieces):
            uuid_value = _feishu_request_uuid(
                idempotency_key,
                component="reply",
                index=index,
            )
            results.append(
                _reply_feishu_rendered(
                    message_id,
                    piece,
                    token,
                    uuid_value,
                )
                if uuid_value
                else _reply_feishu_rendered(message_id, piece, token)
            )
        return all(results)

    # LLM: 首次进度固定 text 类型并使用稳定 provider uuid，返回真实 message_id；不拆片、不唤醒会话、不伪造成功 ID。
    # 函数用途: 创建可反复编辑的一条进度消息，后续统一投递服务用此 ID 更新同一条。
    def start_progress_message(self, user_id: str, text: str, *, reply_to: str = "", idempotency_key: str = "") -> str:
        return _start_feishu_progress(self, user_id, text, reply_to=reply_to, idempotency_key=idempotency_key)

    # LLM: 原生卡片不回落文本；目标是宿主解析的私人身份，uuid 保持逻辑发送幂等，回执来自真实 API。
    # 函数用途: 为审批或私密输入发送一张原生卡，返回后续回调要核对的消息与聊天 ID。
    def send_control_card(self, user_id: str, card: dict, *, idempotency_key: str) -> dict[str, str]:
        return _send_feishu_control_card(self, user_id, card, idempotency_key=idempotency_key)

    # LLM: 进度始终使用首次创建的 text 类型，不能根据候选出现 Markdown/表格而改变消息类型；只编辑原 ID。
    # 函数用途: 更新原文本进度窗口，普通富文本订正仍由 edit_message 保持已有语义。
    def edit_progress_message(self, message_id: str, text: str) -> bool:
        token = self._get_tenant_access_token()
        if not token:
            return False
        result = _feishu_msg_api("PUT", f"/im/v1/messages/{message_id}",
            {"msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}, token)
        return result.get("code") == 0

    def edit_message(self, message_id: str, text: str) -> bool:
        """编辑已发消息(流式落版/订正;markdown 渲染 + post 回落)。"""
        token = self._get_tenant_access_token()
        return bool(token) and _edit_feishu_rendered(message_id, text, token)

    def send_image(
        self,
        user_id: str,
        path: Path,
        *,
        idempotency_key: str = "",
    ) -> bool:
        """上传并发送图片。"""
        token = self._get_tenant_access_token()
        return bool(token) and _send_feishu_image(
            user_id,
            path,
            token,
            idempotency_key,
        )

    def send_file(
        self,
        user_id: str,
        path: Path,
        *,
        idempotency_key: str = "",
    ) -> bool:
        """上传并发送文件(按扩展名路由类型)。"""
        token = self._get_tenant_access_token()
        return bool(token) and _send_feishu_file(
            user_id,
            path,
            token,
            idempotency_key,
        )

    # LLM: message_id 来自原当前消息或已核验的准确 parent；凭据只给资源 API，不写入 owner。
    # 函数用途: 使用真实资源源消息下载附件到当前 owner 私有目录，返回原安全文件名。
    def fetch_media_to(self, message_id: str, media: dict[str, str], dest_dir: Path) -> str | None:
        """下载入站媒体到 dest_dir,返回文件名;无媒体/失败 None。"""
        token = self._get_tenant_access_token()
        return _fetch_media_to_dir(message_id, media, dest_dir, token) if token else None

    # LLM: 只读取明确 message_id，不查历史或递归 root；caller 必须核验返回 exact ID/chat/deleted/topic 再下载。
    # 函数用途: 用宿主 token 单次查取被引用消息；认证失败只返回结构化原因，不暴露凭据。
    def fetch_message(self, message_id: str) -> dict[str, Any]:
        return _fetch_message_by_id(self, message_id)

    # LLM: 凭据仍沿原有效期缓存，失败日志只exceptiontype，不以异常正文形成用户回复。
    # 函数用途: 按原周期获取飞书token，失败明确不可发送。
    def _get_tenant_access_token(self) -> str | None:
        now = time.time()
        if self._tenant_access_token and now < self._token_expires_at - 60:
            return self._tenant_access_token

        try:
            result = self._request_tenant_access_token()
            if result.get("code") == 0:
                self._tenant_access_token = result.get("tenant_access_token", "")
                self._token_expires_at = now + result.get("expire", 7200)
                return self._tenant_access_token
        except Exception as exc:
            logger.error('获取飞书 token 失败 error_type=%s', type(exc).__name__)
        return None

    # LLM: 应用凭据仅发原固定认证目标且拒绝HTTP跳转，原token响应只供本adapter内存缓存。
    # 函数用途: 用原超时申请宿主token，不记录或导出凭据正文。
    def _request_tenant_access_token(self) -> dict[str, Any]:
        url = f"{_FEIHSU_API_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with open_authenticated_request(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))


    def verify_feishu_signature(self, token: str, timestamp: str, signature: str) -> bool:
        import secrets

        # fail-closed:未配置任何验证手段时拒绝(不处理无验证事件,防伪造 webhook 驱动 agent)
        if not self.encrypt_key and not self.verification_token:
            return False
        if not self.encrypt_key:
            return secrets.compare_digest(token, self.verification_token)  # 常数时间比,防时序侧信道
        source = f"{self.encrypt_key}{timestamp}{token}"
        expected = hmac.new(source.encode(), b"", hashlib.sha256).hexdigest()
        return secrets.compare_digest(signature, expected)


# LLM: webhook 必须先核验供应商签名，retryable 受理失败返回非成功 HTTP，卡片与验证挑战保持原合同。
# 类用途: 接收飞书官方 webhook 并将运输 ACK 与真实受理结果对齐。
class _FeishuCallbackHandler(BaseHTTPRequestHandler):

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass  # 静默日志

    # LLM: 认证后的消息复用原 typed 入站；不能以 HTTP200 吞掉暂时失败，也不能把正文或凭据放错误响应。
    # 函数用途: 处理原回调路由，稳定接收返回成功，需要重投的消息返回503。
    def do_POST(self) -> None:
        if self.path != "/feishu/callback":
            self._write_json(404, {"error": "not found"})
            return

        adapter: FeishuAdapter | None = getattr(self.server, "adapter", None)
        if adapter is None:
            self._write_json(500, {"error": "adapter unavailable"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            self._write_json(400, {"error": "invalid content length"})
            return
        raw_body = self.rfile.read(content_length)
        from .feishu_crypto import FeishuWebhookDecodeRequest, verify_and_decode_webhook

        payload = verify_and_decode_webhook(
            FeishuWebhookDecodeRequest(
                raw_body=raw_body,
                encrypt_key=adapter.encrypt_key,
                verification_token=adapter.verification_token,
                timestamp=self.headers.get("X-Lark-Request-Timestamp", ""),
                nonce=self.headers.get("X-Lark-Request-Nonce", ""),
                signature=self.headers.get("X-Lark-Signature", ""),
            )
        )
        if payload is None:
            self._write_json(403, {"error": "signature mismatch"})
            return

        if payload.get("type") == "url_verification":
            self._write_json(200, {"challenge": payload.get("challenge", "")})
            return

        from .feishu_card import extract_webhook_card_action

        card_action = extract_webhook_card_action(payload)
        if card_action is not None:
            result = adapter._handle_card_action(card_action) if adapter.my_agent_home else None
            self._write_json(200, result if isinstance(result, dict) else {"code": 0})
            return

        result = adapter._handle_feishu_event(payload)
        self._write_json(503 if result.status == 'retryable' else 200,
                         {'status': result.status, 'reason_code': result.reason_code})

    def do_GET(self) -> None:
        body = b"feishu adapter is running"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


# LLM: 首次进度固定 text 类型并沿真实 API/稳定 uuid 创建；响应 ID 是唯一后续编辑身份，无 fallback 或新会话唤醒。
# 函数用途: 执行飞书首次草稿创建并返回真实 message_id；适配器类保留公共入口，发送实现在本模块集中维护。
def _start_feishu_progress(adapter: FeishuAdapter, user_id: str, text: str, *, reply_to: str, idempotency_key: str) -> str:
    token = adapter._get_tenant_access_token()
    if not token:
        return ""
    identities = []

    # LLM: 渲染回落仍复用同一逻辑 uuid；只读取供应商成功响应的 message_id，不根据请求推导。
    # 函数用途: 发送一种已渲染格式并记下真实消息身份。
    def send(msg_type: str, content: str) -> dict[str, Any]:
        uuid_value = _feishu_request_uuid(idempotency_key, component="progress", index=0)
        body = {"msg_type": msg_type, "content": content, "uuid": uuid_value}
        path = f"/im/v1/messages/{reply_to}/reply" if reply_to else _message_send_path(user_id)
        if not reply_to:
            body["receive_id"] = user_id
        result = _feishu_msg_api_with_retry("POST", path, body, token)
        if result.get("code") == 0:
            identities.append(str((result.get("data") or {}).get("message_id") or ""))
        return result

    send("text", json.dumps({"text": text}, ensure_ascii=False))
    return next((identity for identity in identities if identity), "")


# LLM: 卡片创建只接受供应商成功响应中的 message_id/chat_id；不根据 UUID 推断 ID，不回落普通消息。
# 函数用途: 沿原认证、准确个人/群编号与稳定 UUID 发控制卡，保留精确回调来源。
def _send_feishu_control_card(adapter: FeishuAdapter, user_id: str, card: dict, *, idempotency_key: str) -> dict[str, str]:
    token = adapter._get_tenant_access_token()
    if not token:
        return {}
    result = _feishu_msg_api_with_retry("POST", _message_send_path(user_id),
        _message_body(receive_id=user_id, msg_type="interactive", content=json.dumps(card, ensure_ascii=False),
                      uuid_value=_feishu_request_uuid(idempotency_key, component="control", index=0)), token)
    data = result.get("data") if result.get("code") == 0 else None
    if not isinstance(data, dict):
        return {}
    return {"message_id": str(data.get("message_id") or ""), "chat_id": str(data.get("chat_id") or "")}
