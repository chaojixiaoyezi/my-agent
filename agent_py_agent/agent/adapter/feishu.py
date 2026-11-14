

# LLM: 飞书入站返回显式 accepted/ignored/retryable；未核验或落盘失败不得提交运输去重，原锁与 owner 仍守权限。
# 模块用途: 管理飞书受理与固定认证接口，附件只消费已核验bytes并禁止重定向；实时投影WS健康，不创建第二队列或状态账。
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..common.authenticated_http import open_authenticated_request
from ..common.http_listener import BoundThreadingHTTPServer
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
from .feishu_observations import FeishuObservationMixin
from .feishu_render import build_outbound_payload, split_message, strip_markdown_to_plain_text
from .feishu_typing import FeishuTypingMixin
from .feishu_unlock_queue import FeishuUnlockQueue, FeishuUnlockResumeMixin
from .inbound_images import InboundMediaInputError
from .protocol import (
    FeishuEventDisposition,
    IncomingMessage,
    OutgoingMessage,
    feishu_to_incoming,
    parse_feishu_conversation_address,
    provider_message_reference,
)

logger = logging.getLogger(__name__)

_FEIHSU_API_BASE = "https://open.feishu.cn/open-apis"
# 幂等发送最多尝试 3 次：飞书接口偶发网络错误时重发，靠 message_id 去重防止重复投递。
_FEISHU_IDEMPOTENT_SEND_ATTEMPT_COUNT = 3
_FEISHU_RETRYABLE_HTTP_STATUS = frozenset({408, 429, 500, 502, 503, 504})


@dataclass
class _TokenRefresh:
    token: str | None = None
    expires_at: float = 0.0
    retry_at: float = 0.0
    inflight: bool = False
    done: threading.Event = field(default_factory=threading.Event)


_TOKEN_REFRESH_LOCK = threading.Lock()
_TOKEN_REFRESHES: dict[tuple[str, str], _TokenRefresh] = {}


@dataclass
class _SharedConnection:
    home: str
    ready: threading.Event = field(default_factory=threading.Event)
    adapters: weakref.WeakSet = field(default_factory=weakref.WeakSet)
    client: Any = None
    thread: Any = None
    failed: bool = False
    stopping: bool = False
    closer: Any = None
    closing_started: threading.Event = field(default_factory=threading.Event)


_CONNECTIONS_LOCK = threading.Lock()
_CONNECTIONS: dict[tuple[str, str], _SharedConnection] = {}


# LLM: 同key共享运输采用全组停止；全局锁内只发布停止事实和唯一closer，调用方必须在锁外启动/等待。
# 函数用途: 让所有共享成员及新实例看到同一关闭屏障，不能因执行线程先退出就遗失SDK关闭责任。
def _reserve_shared_connection_stop(shared: _SharedConnection):
    with _CONNECTIONS_LOCK:
        shared.stopping = True
        start_closer = shared.closer is None
        if start_closer:
            def close_shared():
                try:
                    shared.client.close()
                except Exception as exc:
                    logger.warning('停止共享飞书运输失败 error_type=%s', type(exc).__name__)

            shared.closer = threading.Thread(target=close_shared, daemon=True)
        for member in tuple(shared.adapters):
            if getattr(member, '_shared_connection', None) is shared:
                member._running = False
                member._stop_event.set()
                member._ws_client, member._ws_thread = shared.client, shared.thread
                member._closing_thread = shared.closer
                member._closing_started = shared.closing_started
        return shared.closer, start_closer, shared.closing_started


# LLM: 标准 API 出站共用此入口；GET 不携带请求体，POST/PUT 仍发送 JSON，凭据只进认证头，不作为正文或日志。
# 函数用途: 调用机器人信息与消息接口，默认十秒超时；有总预算的成员查询显式传入剩余时间。
def _feishu_msg_api(method: str, path: str, body: dict[str, Any], token: str, *, timeout: float = 10) -> dict[str, Any]:
    """统一飞书消息 API 调用；GET 不携带正文，其余方法发送 JSON，返回响应对象。"""
    req = urllib.request.Request(
        f"{_FEIHSU_API_BASE}{path}",
        data=None if method == "GET" else json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
        method=method,
    )
    with open_authenticated_request(req, timeout=timeout) as resp:
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


def _send_with_fallback(text: str, send: Callable[[str, str], dict[str, Any]], *,
                        component_id: str = '', message_key: str = '', provider=None):
    """每段发送先提交原组件事实，未知响应不改正文或幂等键重发。"""
    from ..delivery.registry import current_component_delivery

    msg_type, content_json = build_outbound_payload(text)
    state = current_component_delivery() if component_id else None
    receipt = None
    if state is not None:
        receipt = state.ensure(component_id, message_key, hashlib.sha256(text.encode()).hexdigest())
        if receipt:
            return receipt
        prepared = state.before_send(receipt, provider)
        if prepared is None:
            return state.receipts[component_id]
        receipt = prepared
    result = send(msg_type, content_json)
    if result.get('code') == 0:
        return state.finish(receipt, result) if state is not None else True
    if msg_type == 'post' and type(result.get('code')) is int:
        fallback = json.dumps({'text': strip_markdown_to_plain_text(text)}, ensure_ascii=False)
        result = send('text', fallback)
        if result.get('code') == 0:
            return state.finish(receipt, result) if state is not None else True
    logger.error('飞书发送失败 provider_code=%s', result.get('code') if type(result.get('code')) is int else None)
    return state.finish(receipt, result) if state is not None else False


# LLM: 地址来自原owner/origin投影；内部topic必须先由授权绑定选basechat，运输不自行剥topic借权，不回落管理员。
# 函数用途: 选择供应商接收编号类型，原始topic或坏群地址在发请求前拒绝。
def _message_send_path(target: str) -> str:
    if target.startswith('ou_') and len(target) > 3:
        return '/im/v1/messages?receive_id_type=open_id'
    if target.startswith('oc_') and len(target) > 3:
        _chat, topic = parse_feishu_conversation_address(target)
        if topic:
            raise ValueError('CHANNEL_TARGET_INVALID')
        return '/im/v1/messages?receive_id_type=chat_id'
    raise ValueError('CHANNEL_TARGET_INVALID')


# LLM: 普通发送按准确接收编号构造一次逻辑消息，markdown 回落仍共用原 uuid；不能改变投递目标。
# 函数用途: 向用户或原群发送渲染后的文字。
def _send_feishu_rendered(
    user_id: str, text: str, token: str, uuid_value: str = '', *,
    component_id: str = '', provider=None,
):
    """普通文字沿原渲染发送，可选记录同一分片的提供方事实。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api_with_retry(
        'POST', _message_send_path(user_id),
        _message_body(receive_id=user_id, msg_type=mt, content=cj, uuid_value=uuid_value), token),
        component_id=component_id, message_key=uuid_value, provider=provider)


def _reply_feishu_rendered(
    reply_to: str, text: str, token: str, uuid_value: str = '', *,
    reply_in_thread: bool | None = None, component_id: str = '', provider=None,
):
    """所有分片共用原锚点；话题策略只在提供方边界映射。"""
    return _send_with_fallback(text, lambda mt, cj: _feishu_msg_api_with_retry(
        'POST', f'/im/v1/messages/{reply_to}/reply',
        {'msg_type': mt, 'content': cj, **({'uuid': uuid_value} if uuid_value else {}),
         **({'reply_in_thread': reply_in_thread} if reply_in_thread is not None else {})}, token),
        component_id=component_id, message_key=uuid_value, provider=provider)


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
    *, reply_to: str = '', reply_in_thread: bool | None = None,
    component_receipt=None, provider=None,
) -> bool:
    """发图片/文件/音视频消息(content 含 image_key/file_key)。"""
    from ..delivery.registry import current_component_delivery

    state = current_component_delivery() if component_receipt is not None else None
    if state is not None:
        if component_receipt:
            return component_receipt
        prepared = state.before_send(component_receipt, provider)
        if prepared is None:
            return state.receipts[component_receipt.component_id]
        component_receipt = prepared
    body = _message_body(receive_id=user_id, msg_type=msg_type,
        content=json.dumps(content, ensure_ascii=False), uuid_value=uuid_value)
    if reply_to:
        body.pop('receive_id', None)
    if reply_in_thread is not None:
        body['reply_in_thread'] = reply_in_thread
    result = _feishu_msg_api_with_retry(
        "POST",
        f'/im/v1/messages/{reply_to}/reply' if reply_to else _message_send_path(user_id),
        body,
        token,
    )
    if state is not None:
        return state.finish(component_receipt, result)
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


# LLM: 原认证请求禁止重定向；实际读取有界，明确超限抛typed输入错误，暂态网络仍返回None，不输出正文/token。
# 函数用途: 从原消息资源API下载有界附件；图片可使用本条剩余额度，普通文件保留原类型上限。
def _fetch_feishu_resource(message_id: str, key: str, kind: str, token: str, *, max_bytes: int | None = None) -> bytes | None:
    """下载入站媒体 bytes;失败 None。"""
    req = urllib.request.Request(
        f"{_FEIHSU_API_BASE}/im/v1/messages/{quote(message_id, safe='')}/resources/{quote(key, safe='')}?type={kind}",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with open_authenticated_request(req, timeout=30) as resp:
            limit = max_bytes if max_bytes is not None else MAX_IMAGE_BYTES if kind == 'image' else MAX_FILE_BYTES
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise InboundMediaInputError('附件大小超过本次输入上限，请压缩或减少附件后重新发送。')
            return data
    except InboundMediaInputError:
        raise
    except Exception as exc:
        logger.warning('飞书媒体下载失败 error_type=%s http_status=%s', type(exc).__name__,
            exc.code if isinstance(exc, urllib.error.HTTPError) else 0)
        if isinstance(exc, urllib.error.HTTPError):
            exc.close()
        return None


# LLM: 目录由原owner解析，实际下载上限贯穿HTTP；原子私有写不跟随链接，路径失败不能降级为坏图回执。
# 函数用途: 下载附件并按0600保存原owner目录，返回安全文件名；图片真实MIME随后由原生图片准备器验证。
def _fetch_media_to_dir(message_id: str, media: dict[str, str], dest_dir: Path, token: str, *, max_bytes: int | None = None) -> str | None:
    """下载入站媒体落盘 dest_dir,返回文件名;无媒体/失败 None。"""
    if media.get("image_key"):
        key, kind, suffix = str(media["image_key"]), "image", ".png"
    elif media.get("file_key"):
        key, kind, suffix = str(media["file_key"]), "file", ".bin"
    else:
        return None
    options = {'max_bytes': max_bytes} if max_bytes is not None else {}
    blob = _fetch_feishu_resource(message_id, key, kind, token, **options)
    if blob == b'' and kind == 'image':
        raise InboundMediaInputError('图片内容为空，请重新发送可读取的图片。')
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

    from .feishu_agent_panel import handle_panel_card_action

    panel_result = handle_panel_card_action(adapter, norm)
    if panel_result is not None:
        return panel_result

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
        )

        if not is_password_action(value):
            return False, None
        operator = str(norm.get("operator_open_id") or "").strip()
        expected_user = str(value.get("user_id") or "").strip()
        if not operator or operator != expected_user:
            return True, _password_operator_mismatch_response()
        from ..session_lock.card_control import resolve_password_card
        result = resolve_password_card(adapter,norm,value)
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
        return True, {'toast':{'type':'error','content':'这张密码卡已失效或来源不匹配，请重新请求原私人卡。'}}


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
    from ..session_lock import admission

    chat_type = str((getattr(msg, "metadata", {}) or {}).get("feishu_chat_type", ""))
    if chat_type == "group" or not getattr(adapter, "_session_lock_enabled", True):
        return None
    ensure_lock = getattr(adapter, '_ensure_session_lock', None)
    if callable(ensure_lock):
        ensure_lock()
    unlock = getattr(adapter, "_unlock", None)
    if unlock is None:
        logger.warning("私聊会话锁不可用，消息未分发")
        return FeishuEventDisposition('retryable', 'FEISHU_SESSION_LOCK_UNAVAILABLE')
    try:
        user_id = str(getattr(msg, "user_id", "") or "")
        if not user_id:
            return FeishuEventDisposition('ignored', 'FEISHU_EVENT_INVALID')
        status = admission.check_session_admission(unlock, user_id)
        if not status.allowed and not (status.locked or status.requires_password_setup):
            return FeishuEventDisposition('retryable', status.reason_code)
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
    from .feishu_group_events import GROUP_EVENT_TYPES, admit_group_provider_event
    from .feishu_mentions import bot_mention_decision

    if (payload.get('header') or {}).get('event_type') in GROUP_EVENT_TYPES:
        return admit_group_provider_event(adapter, payload)

    message = _feishu_event_message(payload)
    if not isinstance(message, dict) or not message.get('message_id'):
        return FeishuEventDisposition('ignored', 'FEISHU_EVENT_INVALID')
    bot = adapter._resolve_bot_open_id() if message.get('chat_type') == 'group' else ''
    facts['bot_known'] = bool(bot)
    facts['bot_retry_in_seconds'] = max(0, round(getattr(adapter, '_bot_identity_retry_at', 0.0) - time.monotonic())) if not bot else 0
    sender_type = ((payload.get('event') or {}).get('sender') or {}).get('sender_type')
    if sender_type in {'bot', 'app'}:
        from .feishu_observations import admit_bot_observation
        return admit_bot_observation(adapter, payload, bot)
    if sender_type != 'user':
        return FeishuEventDisposition('ignored', 'FEISHU_SENDER_TYPE_UNVERIFIED')
    if message.get('chat_type') == 'group' and adapter.group_require_bot_mention:
        decision = bot_mention_decision(message, bot)
        facts.update(mention_gate='matched' if decision.allowed else 'unmatched' if decision.verified else 'unverified',
                     mentions_count=decision.mentions_count, post_at_count=decision.post_at_count)
        if not decision.allowed:
            if not decision.verified:
                return FeishuEventDisposition('retryable', decision.reason_code)
            from .feishu_observations import admit_user_observation
            return admit_user_observation(adapter, payload, bot)
    msg = feishu_to_incoming(payload, bot_open_id=bot)
    if msg is None:
        return FeishuEventDisposition('ignored', 'FEISHU_MESSAGE_EMPTY')
    if not msg.user_id or not msg.conversation_id:
        return FeishuEventDisposition('ignored', 'FEISHU_EVENT_INVALID')
    lock_result = _session_message_lock_disposition(adapter, msg)
    if lock_result is not None:
        return lock_result
    if adapter.my_agent_home:
        from .feishu_agent_panel import handle_panel_command_event, is_panel_command

        if is_panel_command(msg.content):
            return handle_panel_command_event(adapter, msg)
    if adapter._dispatch(msg) is False:
        return FeishuEventDisposition('retryable', 'FEISHU_INGRESS_UNAVAILABLE')
    return FeishuEventDisposition('accepted')


# LLM: 密码卡发送失败只能影响 onboarding/解锁提示，不得抛异常破坏 adapter 消息循环。
# 函数用途: 构造并发送设置或解锁密码卡，返回飞书是否接收成功。
def _send_session_password_card(adapter: Any, user_id: str, mode: str) -> bool:
    try:
        from ..session_lock.card_control import bind_password_card, prepare_password_card
        from ..session_lock.feishu_cards import build_password_card
        reference,payload=prepare_password_card(adapter,user_id,mode)
        receipt=_send_feishu_control_card(adapter,user_id,build_password_card(mode=mode,user_id=user_id,reference=reference),
            idempotency_key=payload['nonce'])
        bind_password_card(adapter,payload,receipt)
        return bool(receipt.get('message_id') and receipt.get('chat_id'))
    except (OSError,RuntimeError,TypeError,ValueError):
        logger.error('发送密码卡失败，未修改密码或解锁状态')
        return False



# LLM: 启动原唯一WS客户端与卡片回调；running仍是宿主生命周期，连接健康只由SDK实时事实投影。
# 函数用途: 接好既有长连接入站并启动线程，实际建连和重试由原WS客户端负责，不添加第二连接。
def _start_feishu_long_connection(adapter: Any) -> None:
    from .feishu_ws import FeishuWsClient, run_ws_client_thread

    key = (adapter.app_id, adapter.config_generation)
    with _CONNECTIONS_LOCK:
        shared = _CONNECTIONS.get(key)
        if shared is not None and shared.stopping:
            if not shared.closing_started.is_set() or shared.closer is None or shared.closer.is_alive() or (
                    shared.thread is None or shared.thread.is_alive()):
                raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
            _CONNECTIONS.pop(key)
            shared = None
        if shared is not None and shared.ready.is_set() and (shared.failed or (
                shared.thread is not None and not shared.thread.is_alive())):
            _CONNECTIONS.pop(key)
            shared = None
        leader = shared is None
        if shared is None:
            shared = _SharedConnection(adapter.my_agent_home)
            _CONNECTIONS[key] = shared
        if shared.home != adapter.my_agent_home:
            raise RuntimeError('CHANNEL_ADAPTER_START_FAILED')
        if shared.client is not None and getattr(shared.client, '_stopped', False):
            raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
        adapter._shared_connection = shared
        shared.adapters.add(adapter)
    if leader:
        # 发布与观察原连接句柄，不持全局缓存锁做网络、close 或 join。
        def on_payload(payload):
            with _CONNECTIONS_LOCK:
                consumers = tuple(shared.adapters)
            results = [item._handle_feishu_event(payload) for item in consumers if not item._stop_event.is_set()]
            if not results or any(result.status == 'retryable' for result in results):
                return FeishuEventDisposition('retryable', 'FEISHU_INGRESS_UNAVAILABLE')
            return FeishuEventDisposition('accepted' if any(result.status == 'accepted' for result in results) else 'ignored')

        def on_dead():
            with _CONNECTIONS_LOCK:
                consumers = tuple(shared.adapters)
            for item in consumers:
                if item._ws_client is shared.client:
                    item._running = False

        try:
            shared.client = FeishuWsClient(app_id=adapter.app_id, app_secret=adapter.app_secret,
                on_payload=on_payload, ws_proxy=adapter.ws_proxy)
            shared.client.generation_ref = adapter._generation_ref
            shared.client.on_card_action = adapter._handle_card_action if adapter.my_agent_home else None
            adapter._ws_client = shared.client
            shared.thread = run_ws_client_thread(shared.client, on_dead)
        except BaseException:
            shared.failed = True
            raise
        finally:
            shared.ready.set()
    elif not shared.ready.wait(5.0):
        raise RuntimeError('CHANNEL_ADAPTER_START_FAILED')
    with _CONNECTIONS_LOCK:
        if shared.stopping:
            raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
        if shared.failed:
            raise RuntimeError('CHANNEL_ADAPTER_START_FAILED')
        adapter._ws_client, adapter._ws_thread = shared.client, shared.thread
    logger.info("飞书适配器已启动(长连接 WS 模式,免公网/不绑端口)")


# LLM: 原running与WS连接分别投影，健康读取不产生网络或持久状态；原JSON health协议供registry直接消费。
# 函数用途: 将飞书运输的固定状态码转换成通道健康，不用日志文字或线程存活推断已连接。
def _feishu_runtime_health(adapter: Any) -> dict[str, str]:
    checked_at = datetime.now(timezone.utc).isoformat()
    if not adapter.running:
        retained = any(getattr(adapter, field, None) is not None for field in ('_ws_thread', '_server_thread', '_closing_thread'))
        requested = adapter._stop_event.is_set() and (adapter._ws_client is not None or adapter._server is not None)
        if retained or requested:
            return {'state': 'unhealthy', 'error_code': 'CHANNEL_ADAPTER_STOP_FAILED', 'checked_at': checked_at}
        return {"state": "stopped", "error_code": "CHANNEL_ADAPTER_NOT_RUNNING", "checked_at": checked_at}
    if not adapter._is_long_connection():
        return {"state": "healthy", "error_code": "", "checked_at": checked_at}
    state, error = "unhealthy", "FEISHU_WS_STATE_UNKNOWN"
    try:
        transport = adapter._ws_client.transport_health() if adapter._ws_client is not None else {
            "status": "degraded", "reason_code": "FEISHU_WS_CONNECTING"}
        state, error = {
            ("healthy", "FEISHU_WS_CONNECTED"): ("healthy", ""),
            ("degraded", "FEISHU_WS_CONNECTING"): ("starting", "FEISHU_WS_CONNECTING"),
            ("degraded", "FEISHU_WS_DISCONNECTED"): ("unhealthy", "FEISHU_WS_DISCONNECTED"),
            ("degraded", "FEISHU_WS_STATE_UNKNOWN"): ("unhealthy", "FEISHU_WS_STATE_UNKNOWN"),
            ("stopped", "FEISHU_WS_STOPPED"): ("unhealthy", "FEISHU_WS_STOPPED"),
        }.get((transport.get("status"), transport.get("reason_code")), (state, error))
    except (AttributeError, TypeError, ValueError, RuntimeError):
        pass
    return {"state": state, "error_code": error, "checked_at": checked_at}


# LLM: 原send/reply的uuid只保证窗口内幂等；runtime_health实时读入站运输，不以running替连接，也不改变REST出站权限。
# 类用途: 提供飞书发送、回复、准确消息/媒体读取与入站健康快照，没有UUID到消息的只读查询保证。
class FeishuAdapter(FeishuObservationMixin, FeishuUnlockResumeMixin, FeishuTypingMixin, FeishuAttachmentMixin, BaseChannelAdapter):
    # LLM: uuid 是有限窗口去重，不覆盖跨窗口重发；channel delivery 必须先查首次 dispatch 时间。
    # 字段用途: 向统一通道注册表声明 provider 原生幂等能力。
    provider_idempotent_delivery = True
    provider_delivery_queryable = False
    provider_idempotency_window_seconds = 3600.0

    adapter_name = "feishu"
    supports_edit_progress = True
    supports_control_cards = True
    supports_progress_cards = True
    supports_component_receipts = True
    supports_reply_policy = True

    # LLM: 创建原通道状态、认证服务和有界待送队列；显式关闭与初始化失败分开，不把未知状态当作认证成功。
    # 函数用途: 初始化飞书接入、引用文字/附件开关与私聊密码门，不启动消息服务或发送网络请求。
    def __init__(
        self,
        config: dict[str, Any],
        callback_port: int = 8421,
        workspace_root: Path | None = None,
        *,
        live_config: object | None = None,
    ) -> None:
        super().__init__(config)
        self._live_config = live_config
        from .feishu_replay_guard import FeishuReplayGuard

        self._replay_guard = FeishuReplayGuard()
        self.callback_port = callback_port
        self.workspace_root = workspace_root or Path.cwd()

        self.app_id = config.get("feishu_app_id", "")
        self.app_secret = config.get("feishu_app_secret", "")
        # 未显式给代次的旧构造按有效配置摘要共享；摘要不进入用户回执。
        self.config_generation = str(config.get('config_generation') or config.get('feishu_config_generation')
            or hashlib.sha256(json.dumps([self.app_id, self.app_secret, config.get('feishu_ws_proxy', '')]).encode()).hexdigest())
        self._generation_ref = f'{self.config_generation}:0'
        self._generation_counter = 0
        self.verification_token = config.get("feishu_verification_token", "")
        self.encrypt_key = config.get("feishu_encrypt_key", "")
        # my_agent_home 根:卡片按钮回调据此读待确认记录、定位 owner 的 SOUL.md(与网关同一根)。
        self.my_agent_home = str(config.get("my_agent_home", "") or "")
        self._gateway_port = int(config.get("gateway_port", 8420))
        self._gateway_auth_token = str(config.get("gateway_auth_token", "") or "")
        # 默认长连接:免公网且能接密码/人格确认卡片回调；webhook 仍可显式选择。
        self.connection_mode = str(
            config.get("feishu_connection_mode", "long_connection") or "long_connection"
        ).strip().lower()
        self.ws_proxy = config.get("feishu_ws_proxy", "")
        self._bot_open_id = ""
        self._bot_identity_app_id = ""
        self._bot_identity_retry_at = 0.0
        self._bot_identity_lock = threading.Lock()

        self._server: BoundThreadingHTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._ws_client: Any = None
        self._ws_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        self._tenant_access_token: str | None = None
        self._token_expires_at: float = 0
        self._token_generation_key = (self.app_id, self.config_generation)

        # 个人私聊会话锁默认开启；显式 false 才关闭。首次私聊要求设置密码，
        # 未设置密码也暂存原消息；群聊不受个人锁影响。初始化失败不允许私聊越过认证门。
        self._unlock = self._init_session_lock(config)
        self._pending_unlock_messages = (
            FeishuUnlockQueue() if self._unlock is not None else None
        )
        # 解锁续送线程启动前在生命周期锁内登记线程及代次；本代停止事件只置位不清除，start 换新事件，stop 锁外 join。
        self._unlock_drain_stop = threading.Event()
        self._unlock_drain_threads: dict[threading.Thread, str] = {}


    # LLM: 宿主开关从原AgentConfig读取，未传宿主的嵌入入口仍用原映射；不改凭据或其它启动配置。
    # 函数用途: 为现有入站读取点提供同一个实时开关来源。
    def _switch_value(self, key: str) -> object:
        return getattr(self._live_config, key, self.config.get(key, True))

    @property
    def group_require_bot_mention(self) -> bool:
        return str(self._switch_value('feishu_group_require_bot_mention')).lower() not in {'false', '0', 'off'}

    @group_require_bot_mention.setter
    def group_require_bot_mention(self, value) -> None:
        self.config['feishu_group_require_bot_mention'] = value

    @property
    def quoted_media_enabled(self) -> bool:
        return str(self._switch_value('feishu_quoted_media_enabled')).lower() not in {'false', '0', 'off'}

    @quoted_media_enabled.setter
    def quoted_media_enabled(self, value) -> None:
        self.config['feishu_quoted_media_enabled'] = value

    @property
    def quoted_text_enabled(self) -> bool:
        return str(self._switch_value('feishu_quoted_text_enabled')).lower() not in {'false', '0', 'off'}

    @property
    def _session_lock_enabled(self) -> bool:
        return str(self._switch_value('feishu_session_lock_enabled')).strip().lower() not in {'0', 'false', 'no', 'off'}

    # LLM: 仅在私聊锁已开启且服务尚未装配时重试原初始化；失败保留不可用事实，由原锁门拒绝派发。
    # 函数用途: 支持关闭启动后首次启用会话锁，不要求重建适配器。
    def _ensure_session_lock(self) -> None:
        if self._unlock is None:
            with self._lock:
                if self._unlock is None:
                    unlock = self._init_session_lock(self.config)
                    if unlock is not None:
                        self._pending_unlock_messages = FeishuUnlockQueue()
                        # 队列先齐备，最后发布服务；并发首消息仍须等原初始化锁。
                        self._unlock = unlock

    # LLM: 每次读取当前开关；缺数据根或数据库失败返回不可用，由锁门拒绝私聊，不退化成关闭。
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
            if any(thread is not None and thread.is_alive() for thread in (
                    self._ws_thread, self._server_thread, getattr(self, '_closing_thread', None))):
                raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
            if getattr(self, '_closing_thread', None) is not None and not self._closing_started.is_set():
                raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
            if self._stop_event.is_set() and (self._ws_client is not None and self._ws_thread is None or
                    self._server is not None and self._server_thread is None):
                raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
            if self._unlock_drain_stop.is_set():
                # 已停止代次的解锁续送未 join 完不能重启；新代用新停止事件，旧线程不会因清除而复活。
                if any(thread.is_alive() for thread in self._unlock_drain_threads):
                    raise RuntimeError('CHANNEL_ADAPTER_STOP_FAILED')
                self._unlock_drain_threads.clear()
                self._unlock_drain_stop = threading.Event()
            self._closing_thread = None
            self._generation_counter += 1
            self._generation_ref = f'{self.config_generation}:{self._generation_counter}'
            self._stop_event.clear()
            self._running = True
            try:
                if self._is_long_connection():
                    self._start_long_connection()
                else:
                    self._start_webhook_server()
            except BaseException:
                self._running = False
                raise

    def _is_long_connection(self) -> bool:
        return self.connection_mode in ("long_connection", "longconn", "ws", "websocket")

    # LLM: 只投影原running与WS实时运输事实；不探测网络、不触发重连、不缓存第二份健康状态，registry读取本协议。
    # 函数用途: 区分适配器进程运行和入站连接可用，让SDK内部持续重试时也能如实显示未连接。
    def runtime_health(self) -> dict[str, str]:
        return _feishu_runtime_health(self)

    def _start_webhook_server(self) -> None:
        self._server = BoundThreadingHTTPServer(
            ("0.0.0.0", self.callback_port),
            _FeishuCallbackHandler,
        )
        self._server.adapter = self  # type: ignore[attr-defined]
        self._server.daemon_threads = True
        self._server_thread = threading.Thread(target=self._serve, daemon=True)
        self._server_thread.start()
        logger.info(f"飞书适配器已启动(webhook 模式)，回调端口={self.callback_port}")

    # LLM: 复用模块内原唯一WS接线，启动不代表连接成功；卡片仍仅在拥有home时注册。
    # 函数用途: 从既有生命周期入口启动长连接，健康状态由runtime_health独立读取。
    def _start_long_connection(self) -> None:
        _start_feishu_long_connection(self)

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

    # LLM: 锁内仅固定本代句柄和停止事实；退出观察在锁外，未退出保留旧代责任。
    # 函数用途: 有界请求停止并返回真实退出回执，不把关闭请求冒充线程退出。
    def stop(self, *, timeout: float = 5.0):
        from ..delivery.registry import ChannelStopResult

        with self._lock:
            self._running = False
            self._stop_event.set()
            self._unlock_drain_stop.set()
            drains = tuple(self._unlock_drain_threads)
            client, ws_thread = self._ws_client, self._ws_thread
            server, server_thread = self._server, self._server_thread
            generation = self._generation_ref
            shared = getattr(self, '_shared_connection', None)
            if client is not None and shared is not None and shared.client is client:
                closer, start_closer, closing_started = _reserve_shared_connection_stop(shared)
            else:
                # 非共享运输仍在本代锁内固定唯一关闭责任，启动和等待均留在锁外。
                start_closer = False
                closer = getattr(self, '_closing_thread', None)
                if closer is None and (client is not None or server is not None):
                    def close_handles():
                        try:
                            if client is not None:
                                client.close()
                            if server is not None:
                                server.shutdown()
                                server.server_close()
                        except Exception as exc:
                            logger.warning('停止飞书运输失败 error_type=%s', type(exc).__name__)
                    closer = threading.Thread(target=close_handles, daemon=True)
                    self._closing_thread = closer
                    self._closing_started = threading.Event()
                    start_closer = True
                closing_started = getattr(self, '_closing_started', None)
        deadline = time.monotonic() + max(0.0, timeout)
        if start_closer:
            closer.start()
            closing_started.set()
        ready = closer is None or closing_started is None or closing_started.wait(max(0.0, deadline - time.monotonic()))
        threads = (closer, ws_thread, server_thread, *drains)
        for thread in threads:
            if thread is closer and not ready:
                continue
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=max(0.0, deadline - time.monotonic()))
        exited = ready and all(thread is None or not thread.is_alive() for thread in threads)
        if client is not None and ws_thread is None or server is not None and server_thread is None:
            exited = False  # 只有关闭请求，没有执行线程的退出观察，不能宣称已退出。
        with self._lock:
            for thread in drains:
                if not thread.is_alive():
                    self._unlock_drain_threads.pop(thread, None)
            if exited and generation == self._generation_ref:
                self._ws_client = self._ws_thread = self._server = self._server_thread = None
                self._closing_thread = None
        return ChannelStopResult('exited' if exited else 'stop_requested' if timeout <= 0 else 'exit_unconfirmed',
            generation, '' if exited else 'CHANNEL_ADAPTER_STOP_FAILED')

    def _stop_ws_client(self) -> None:
        client, self._ws_client = self._ws_client, None
        if client is not None:
            client.close()  # 尽力关闭底层 ws(关不掉就靠 daemon 线程随进程退出)

    # LLM: callback 结果必须显式返给 WS/webhook；异常仅记类型，暂时失败不伪装接受或永久忽略。
    # 函数用途: 经原群门与认证入口接收消息，返回运输回执并记录有界脱敏状态。
    def _handle_feishu_event(self, payload: dict[str, Any]) -> FeishuEventDisposition:
        message = _feishu_event_message(payload)
        facts = _feishu_admission_facts(message if isinstance(message, dict) else {})
        try:
            if self._live_config is not None:
                from ..settings.live_switches import refresh_live_switches

                refresh_live_switches(self._live_config)
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
            from ..delivery.registry import current_component_delivery

            state = current_component_delivery()
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
                    **({'component_id': f'text:{index}', 'provider': self} if state is not None else {}),
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
        *, reply_in_thread: bool | None = None,
    ) -> bool:
        """引用回复某条消息；长结果逐片引用，任一片失败则整体失败。"""
        token = self._get_tenant_access_token()
        if not token:
            return False
        # 与主动发送共用同一平台长度边界。不能让普通消息会分片、Gateway 最终引用回复
        # 却把整篇长报告一次交给 Feishu API，否则长任务已经完成仍会在最后一步投递失败。
        pieces = split_message(text)
        results = []
        from ..delivery.registry import current_component_delivery

        state = current_component_delivery()
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
                    **({'reply_in_thread': reply_in_thread} if reply_in_thread is not None else {}),
                    **({'component_id': f'text:{index}', 'provider': self} if state is not None else {}),
                )
                if uuid_value
                else _reply_feishu_rendered(message_id, piece, token,
                    **({'reply_in_thread': reply_in_thread} if reply_in_thread is not None else {}),
                    **({'component_id': f'text:{index}', 'provider': self} if state is not None else {}))
            )
        return all(results)

    # LLM: 首次进度固定 text 类型并使用稳定 provider uuid，返回真实 message_id；不拆片、不唤醒会话、不伪造成功 ID。
    # 函数用途: 创建可反复编辑的一条进度消息，后续统一投递服务用此 ID 更新同一条。
    def start_progress_message(self, user_id: str, text: str, *, reply_to: str = '', idempotency_key: str = '',
                               reply_in_thread: bool | None = None, render_kind: str = 'text', card: dict | None = None) -> str:
        return _start_feishu_progress(self, user_id, text, reply_to=reply_to, idempotency_key=idempotency_key,
            reply_in_thread=reply_in_thread, render_kind=render_kind, card=card)

    # LLM: 原生卡片不回落文本；目标是宿主解析的个人或原群身份，uuid 幂等；回执仅含真实身份或安全平台错误事实。
    # 函数用途: 发送原生控制卡，返回消息/聊天 ID 或明确拒绝诊断，不返回供应商正文。
    def send_control_card(self, user_id: str, card: dict, *, idempotency_key: str) -> dict[str, Any]:
        return _send_feishu_control_card(self, user_id, card, idempotency_key=idempotency_key)

    def send_agent_panel_card(
        self,
        target: str,
        *,
        actor_open_id: str = "",
        origin_conversation_id: str = "",
        chat_type: str = "p2p",
        session_task_id: str = "",
        bound_root_id: str = "",
        steps: Sequence[Mapping[str, Any]] = (),
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        from .feishu_agent_panel import send_agent_panel_card

        return send_agent_panel_card(
            self,
            target,
            actor_open_id=actor_open_id,
            origin_conversation_id=origin_conversation_id,
            chat_type=chat_type,
            session_task_id=session_task_id,
            bound_root_id=bound_root_id,
            steps=steps,
            idempotency_key=idempotency_key,
        )

    # LLM: 进度始终使用首次创建的 text 类型，不能根据候选出现 Markdown/表格而改变消息类型；只编辑原 ID。
    # 函数用途: 更新原文本进度窗口，普通富文本订正仍由 edit_message 保持已有语义。
    def edit_progress_message(self, message_id: str, text: str, *, render_kind: str = 'text', card: dict | None = None) -> bool:
        token = self._get_tenant_access_token()
        if not token:
            return False
        if render_kind == 'card':
            if not isinstance(card, dict):
                return False
            result = _feishu_msg_api('PATCH', f'/im/v1/messages/{message_id}',
                {'content': json.dumps(card, ensure_ascii=False)}, token)
            return result.get('code') == 0
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

    supports_bounded_inbound_media = True

    # LLM: message_id来自原当前或已核parent，max_bytes只承载宿主预算；不改变认证/重定向或owner边界。
    # 函数用途: 使用原资源源消息有界下载到当前owner私有目录，返回安全文件名。
    def fetch_media_to(self, message_id: str, media: dict[str, str], dest_dir: Path, *, max_bytes: int | None = None) -> str | None:
        """下载入站媒体到 dest_dir,返回文件名;无媒体/失败 None。"""
        token = self._get_tenant_access_token()
        return _fetch_media_to_dir(message_id, media, dest_dir, token, max_bytes=max_bytes) if token else None

    # LLM: 只读取明确 message_id，不查历史或递归 root；caller 必须核验返回 exact ID/chat/deleted/topic 再下载。
    # 函数用途: 用宿主 token 单次查取被引用消息；认证失败只返回结构化原因，不暴露凭据。
    def fetch_message(self, message_id: str) -> dict[str, Any]:
        return _fetch_message_by_id(self, message_id)

    # LLM: 凭据仍沿原有效期缓存，失败日志只exceptiontype，不以异常正文形成用户回复。
    # 函数用途: 按原周期获取飞书token，失败明确不可发送。
    def _get_tenant_access_token(self) -> str | None:
        now = time.time()
        key = (self.app_id, self.config_generation)
        if key == self._token_generation_key and self._tenant_access_token and now < self._token_expires_at - 60:
            return self._tenant_access_token
        with _TOKEN_REFRESH_LOCK:
            state = _TOKEN_REFRESHES.setdefault(key, _TokenRefresh())
            if state.token and now < state.expires_at - 60:
                self._tenant_access_token, self._token_expires_at = state.token, state.expires_at
                self._token_generation_key = key
                return state.token
            if now < state.retry_at:
                return None
            leader = not state.inflight
            if leader:
                state.inflight = True
                state.done.clear()
        if not leader:
            if not state.done.wait(11.0):
                return None
            if key != (self.app_id, self.config_generation):
                return None
            self._tenant_access_token, self._token_expires_at = state.token, state.expires_at
            self._token_generation_key = key
            return state.token
        token, expires_at = None, 0.0
        try:
            result = self._request_tenant_access_token()
            if result.get("code") == 0:
                token = str(result.get('tenant_access_token') or '') or None
                expires_at = time.time() + float(result.get('expire', 7200))
        except Exception as exc:
            logger.error('获取飞书 token 失败 error_type=%s', type(exc).__name__)
        finally:
            with _TOKEN_REFRESH_LOCK:
                state.token, state.expires_at = token, expires_at
                state.retry_at = 0 if token else time.time() + 1.0
                state.inflight = False
                state.done.set()
        if key != (self.app_id, self.config_generation):
            return None
        self._tenant_access_token, self._token_expires_at = token, expires_at
        self._token_generation_key = key
        return token

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
                replay_guard=adapter._replay_guard,
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
def _start_feishu_progress(adapter: FeishuAdapter, user_id: str, text: str, *, reply_to: str, idempotency_key: str,
                           reply_in_thread: bool | None = None, render_kind: str = 'text', card: dict | None = None) -> str:
    token = adapter._get_tenant_access_token()
    if not token:
        return ""
    identities = []
    from ..delivery.registry import current_component_delivery

    state = current_component_delivery()
    content = json.dumps(card if render_kind == 'card' else {'text': text}, ensure_ascii=False)
    if render_kind not in {'text', 'card'} or render_kind == 'card' and not isinstance(card, dict):
        return ''
    uuid_value = _feishu_request_uuid(idempotency_key, component='progress', index=0)
    receipt = None
    if state is not None:
        receipt = state.ensure('progress:0', uuid_value, hashlib.sha256(content.encode()).hexdigest())
        if receipt:
            return str(receipt.provider_message_id or '')
        receipt = state.before_send(receipt, adapter)
        if receipt is None:
            return ''

    # LLM: 渲染回落仍复用同一逻辑 uuid；只读取供应商成功响应的 message_id，不根据请求推导。
    # 函数用途: 发送一种已渲染格式并记下真实消息身份。
    def send(msg_type: str, content: str) -> dict[str, Any]:
        body = {"msg_type": msg_type, "content": content, "uuid": uuid_value}
        if reply_in_thread is not None:
            body['reply_in_thread'] = reply_in_thread
        path = f"/im/v1/messages/{reply_to}/reply" if reply_to else _message_send_path(user_id)
        if not reply_to:
            body["receive_id"] = user_id
        result = _feishu_msg_api_with_retry("POST", path, body, token)
        if state is not None:
            state.finish(receipt, result)
        if result.get("code") == 0:
            identities.append(str((result.get("data") or {}).get("message_id") or ""))
        return result

    send('interactive' if render_kind == 'card' else 'text', content)
    return next((identity for identity in identities if identity), "")


# LLM: 卡片只认供应商成功身份；明确拒绝仅保留数字code和固定宿主码，绝不回传供应商正文或秘密。
# 函数用途: 沿原认证、准确个人/群编号与稳定 UUID 发控制卡，保留精确回调来源。
def _send_feishu_control_card(adapter: FeishuAdapter, user_id: str, card: dict, *, idempotency_key: str) -> dict[str, Any]:
    token = adapter._get_tenant_access_token()
    if not token:
        return {"error_code": "CHANNEL_CONTROL_AUTH_UNAVAILABLE", "effect_outcome": "not_started"}
    result = _feishu_msg_api_with_retry("POST", _message_send_path(user_id),
        _message_body(receive_id=user_id, msg_type="interactive", content=json.dumps(card, ensure_ascii=False),
                      uuid_value=_feishu_request_uuid(idempotency_key, component="control", index=0)), token)
    code = result.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code != 0:
        return {"error_code": "CHANNEL_CONTROL_PROVIDER_REJECTED", "provider_code": code, "effect_outcome": "not_started"}
    data = result.get("data") if code == 0 else None
    if not isinstance(data, dict):
        return {}
    return {"message_id": str(data.get("message_id") or ""), "chat_id": str(data.get("chat_id") or "")}
