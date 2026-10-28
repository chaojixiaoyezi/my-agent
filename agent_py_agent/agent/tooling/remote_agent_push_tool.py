# LLM: 只发固定/remote-agent/push，凭据只从宿主加密节点库取；原Registry负责审批/操作账，不开放任意HTTP或明文token参数。
# 模块用途: 将外部事件及脱敏历史单向投递到已配置节点，只返回短接收回执。
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import socket
import time
import uuid
from urllib.parse import urlsplit

from ..common.json_io import locked_json_path
from ..contracts.gates.network_safety import ipv4_projected, private_host_or_ip_is_allowlisted
from ..conversation.remote_push_context import export_remote_context, sanitize_remote_context
from ..gateway_parts.remote_push_service import (
    MAX_PUSH_BYTES,
    RemotePushError,
    persist_push_json,
    read_push_json,
    validate_push_payload,
)
from .models import (
    BaseTool,
    EffectResolverPolicy,
    IdempotencyPolicy,
    ToolAvailability,
    ToolHandlerOutcome,
    ToolModelHints,
    ToolModelSpec,
    ToolRuntimePolicy,
)
from .web_fetch_runtime import FetchRawRequest, PinResult, RawResponseParts, fetch_raw_response


# LLM: 只允许固定单向路径，不接受用户信息、查询、fragment和控制字符；此函数不发DNS或网络请求。
# 函数用途: 校验远端推送URL的协议和闭合路径。
def validate_push_url(url):
    if not isinstance(url, str) or len(url) > 4096 or any(ord(char) < 33 for char in url):
        raise RemotePushError("REMOTE_PUSH_ENDPOINT_INVALID")
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.path != "/remote-agent/push"
            or parts.username is not None or parts.password is not None or parts.query or parts.fragment
            or parts.port == 0):
        raise RemotePushError("REMOTE_PUSH_ENDPOINT_INVALID")
    return url


# LLM: 仅宿主节点管理入口调用，不暴露成模型操作；凭据复用原owner私密密钥派生的Fernet，不落明文。
# 函数用途: 保存当前owner的远端节点及加密Token，供后续别名投递。
def save_remote_endpoint(agent, reference, url, secret, *, allowed_private_hosts=()):
    from ..user_space.task_credentials import _cipher

    validate_push_url(url)
    if (not isinstance(reference, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", reference) is None
            or not isinstance(secret, str) or not 32 <= len(secret) <= 512
            or not isinstance(allowed_private_hosts, (list, tuple))
            or any(not isinstance(item, str) for item in allowed_private_hosts)):
        raise RemotePushError("REMOTE_PUSH_ENDPOINT_INVALID")
    home = agent.home_paths.owner_home_dir
    encrypted = _cipher(home).encrypt(secret.encode()).decode()
    path = home / "config" / "remote_endpoints.json"
    with locked_json_path(path):
        data = read_push_json(path.parent, path.name)
        data.setdefault("endpoints", {})[reference] = {"endpoint_url": url, "token_ciphertext": encrypted,
            "allowed_private_hosts": list(allowed_private_hosts), "enabled": True}
        persist_push_json(path, data)


# LLM: 显式URL也须匹配已配置的准确目标，不允许模型从另一origin搬凭据；host节点须显式caller ACL，owner节点只给本人。
# 函数用途: 从当前用户或宿主节点库解析目标和密文，不输出凭据。
def resolve_remote_endpoint(agent, params):
    from ..user_space.task_credentials import _cipher

    reference, explicit = params.get("endpoint_ref"), params.get("endpoint_url")
    if bool(reference) == bool(explicit):
        raise RemotePushError("REMOTE_PUSH_ENDPOINT_INVALID")
    home = agent.home_paths.owner_home_dir
    locations = [(home / "config", "remote_endpoints.json", home, False)]
    host_home = resolve_owner_main_home(agent)
    locations.append((agent.home_paths.root / "remote_push", "endpoints.json", host_home, True))
    for root, name, key_home, host in locations:
        endpoints = read_push_json(root, name).get("endpoints", {}) if root.is_dir() else {}
        rows = [endpoints.get(reference)] if reference else [row for row in endpoints.values()
            if isinstance(row, dict) and row.get("endpoint_url") == explicit]
        for row in rows:
            if not isinstance(row, dict) or row.get("enabled") is not True:
                continue
            if host and agent.home_paths.owner_id not in row.get("allowed_callers", ["local/main"]):
                continue
            url = validate_push_url(row["endpoint_url"])
            secret = _cipher(key_home, existing_only=True).decrypt(row["token_ciphertext"].encode()).decode()
            # 只有显式别名才有节点私网许可；直接URL不能借同源配置扩大SSRF范围。
            return url, secret, tuple(row.get("allowed_private_hosts", ())) if reference else ()
    raise RemotePushError("REMOTE_PUSH_ENDPOINT_NOT_CONFIGURED")


# LLM: 规范main home只用于显式host节点密文的解密，不能从模型路径取密钥。
# 函数用途: 取得宿主管理员节点库所用的原加密密钥位置。
def resolve_owner_main_home(agent):
    from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home

    return resolve_owner_home(agent.home_paths.root, OwnerIdentity.local_main()).home_dir


# LLM: 所有DNS回答先核验再固定连接IP；只在节点别名的明确授权下放行私网/回环，元数据和link-local永不放行。
# 函数用途: 为固定单向URL解析安全连接地址，不允许DNS重绑定或代理环境劫持。
def remote_push_pin(url, allowed_private_hosts):
    parts = urlsplit(validate_push_url(url))
    answers = tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(parts.hostname, parts.port or 443,
        type=socket.SOCK_STREAM)))
    if not answers:
        raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
    for value in answers:
        address = ipaddress.ip_address(value)
        projected = ipv4_projected(address) or address
        if (projected.is_link_local or projected.is_multicast or projected.is_unspecified
                or str(projected) in {"100.100.100.200", "fd00:ec2::254"}):
            raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
        if not projected.is_global and not private_host_or_ip_is_allowlisted(value, allowed_private_hosts):
            raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
        if parts.scheme != "https" and not private_host_or_ip_is_allowlisted(value, allowed_private_hosts):
            raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
    return answers[0]


# LLM: 当前请求只签一次nonce/body；resolve_pin只接受原准确URL，任何重定向在下一跳连接前拒绝，绝不外带秘密。
# 函数用途: 通过既有固定IP/取消/总超时HTTP传输发送一条推送，响应仅作接收回执。
def send_remote_push(url, secret, body, allowed_private_hosts, timeout):
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(raw) > MAX_PUSH_BYTES:
        raise RemotePushError("REMOTE_PUSH_CONTEXT_TOO_LARGE")
    pin = remote_push_pin(url, allowed_private_hosts)
    timestamp, nonce = str(int(time.time())), uuid.uuid4().hex
    signature = hmac.digest(secret.encode(), f"{timestamp}.{nonce}.{hashlib.sha256(raw).hexdigest()}".encode(), "sha256").hex()
    headers = {"Content-Type": "application/json", "X-Agent-Push-Token": secret,
        "X-Agent-Timestamp": timestamp, "X-Agent-Nonce": nonce, "X-Agent-Signature": signature}
    # LLM: 仅原URL返回已经校验的IP，不再次DNS解析，也不跟随任何同源/异源重定向。
    # 函数用途: 给原HTTP传输提供单目标固定地址。
    def resolve(target):
        if target != url:
            return PinResult(None, ToolHandlerOutcome("remote_agent_push", False, "拒绝推送重定向",
                error_code="REMOTE_PUSH_REDIRECT_DENIED", effect_outcome="unknown"))
        return PinResult(pin, None)

    # LLM: 不返回远端错误正文，避免其回显Token、路径或注入指令；明确HTTP拒绝也不自动重放POST。
    # 函数用途: 将远端HTTP错误压缩成安全的机器回执。
    def http_error(_tool, exc, _max_chars):
        return ToolHandlerOutcome("remote_agent_push", False, f"远端拒绝接收（HTTP {exc.code}）",
            error_code="REMOTE_PUSH_HTTP_REJECTED", result_envelope={"http_status": exc.code}, effect_outcome="unknown")

    result = fetch_raw_response(FetchRawRequest("remote_agent_push", url, "POST", headers, raw, timeout,
        max_bytes=4096, authorization_origin=""), format_http_error=http_error, resolve_pin=resolve)
    if not isinstance(result, RawResponseParts):
        if isinstance(result, ToolHandlerOutcome):
            result.effect_outcome = "unknown"
        return result
    try:
        receipt = json.loads(result.body)
        if (result.status != 202 or not isinstance(receipt, dict) or receipt.get("ok") is not True
                or receipt.get("status") != "accepted" or secret in json.dumps(receipt)):
            raise ValueError("invalid receipt")
        public = {key: receipt[key] for key in ("push_id", "thread_id", "run_id", "status")}
        if any(not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) is None for value in public.values()):
            raise ValueError("invalid receipt")
        if receipt.get("duplicate") is True:
            public["duplicate"] = True
        return ToolHandlerOutcome("remote_agent_push", True, json.dumps(public, ensure_ascii=False), result_envelope=public)
    except (ValueError, KeyError, TypeError):
        return ToolHandlerOutcome("remote_agent_push", False, "远端接收回执无效，未确认是否入队。",
            error_code="REMOTE_PUSH_RECEIPT_INVALID", effect_outcome="unknown")


# LLM: agent只由组合根绑定；模型仅选择配置别名及事件正文，构造不读盘/解密/联网。
# 类用途: 为主代理和监控提供单向事件投递及上下文分叉工具。
class RemoteAgentPushTool(BaseTool):
    model_spec = ToolModelSpec("remote_agent_push", "向已配置远端节点单向投递事件；只返回接收回执，不查询报告。",
        {"type": "object", "properties": {
            **{key: {"type": "string"} for key in ("endpoint_ref", "endpoint_url", "target_owner", "instruction",
                "title", "idempotency_key", "source_thread_id", "thread_reuse_key")},
            "thread_mode": {"type": "string", "enum": ["new_thread", "reuse_key"]},
            "context_payload": {"type": "object"}},
         "required": ["target_owner", "instruction", "idempotency_key"], "additionalProperties": False},
        hints=ToolModelHints(category="network", default_deferred=True, deferred_summary="单向推送事件与脱敏上下文到已配置节点"))
    runtime_policy = ToolRuntimePolicy(effect_resolver=EffectResolverPolicy(default_effect="mutating"),
        idempotency_policy=IdempotencyPolicy(scope="business"))

    # LLM: agent是当前owner实际依赖，不接收模型注入；无IO副作用。
    # 函数用途: 绑定所属用户的节点密钥和会话库。
    def __init__(self, agent, *, timeout=15):
        self.agent, self.timeout = agent, timeout

    # LLM: 无agent时不猜root；只报告组合根是否接线，不读私密配置。
    # 函数用途: 让未接入宿主的纯Registry明确显示不可用。
    def availability(self):
        return ToolAvailability.ready() if self.agent is not None else ToolAvailability.unavailable("未接入宿主节点配置")

    # LLM: 业务幂等域覆盖节点、owner和显式键；正文变更由远端同键摘要冲突拒绝，原工具操作账仍核完整参数。
    # 函数用途: 为同一投递生成稳定的内部操作身份。
    def business_idempotency_key(self, params):
        return hashlib.sha256(json.dumps([params.get(key) for key in
            ("endpoint_ref", "endpoint_url", "target_owner", "idempotency_key")], sort_keys=True).encode()).hexdigest()

    # LLM: 先核source所有权再取凭据/联网；所有外发字段先脱敏，唯一副作用是一次原HTTP POST，不重试未知效果。
    # 函数用途: 发送事件，按需携带原会话上下文，并返回短接收结果。
    def execute(self, params):
        from cryptography.fernet import InvalidToken

        sending = False
        try:
            if self.agent is None:
                raise RemotePushError("REMOTE_PUSH_UNAVAILABLE")
            snapshot = None
            if params.get("source_thread_id"):
                snapshot = export_remote_context(self.agent.conversation_store, params["source_thread_id"], self.agent.home_paths.owner_id)
            url, secret, allowed = resolve_remote_endpoint(self.agent, params)
            body = {key: value for key, value in params.items() if key not in {"endpoint_ref", "endpoint_url", "source_thread_id"}}
            if snapshot is not None:
                body["context_snapshot"] = snapshot
            body = validate_push_payload(sanitize_remote_context(body, secrets=(secret,)))
            sending = True
            return send_remote_push(url, secret, body, allowed, self.timeout)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, InvalidToken) as exc:
            code = exc.error_code if isinstance(exc, RemotePushError) else str(exc) if str(exc) in {
                "REMOTE_PUSH_SOURCE_THREAD_DENIED", "REMOTE_PUSH_CONTEXT_TOO_LARGE"} else "REMOTE_PUSH_UNAVAILABLE"
            return ToolHandlerOutcome("remote_agent_push", False, code, error_code=code,
                effect_outcome="unknown" if sending else "not_started")
