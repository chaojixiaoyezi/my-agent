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
from collections.abc import Mapping
from dataclasses import fields
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
from ..user_space.owner_policy import resolve_effective_owner_policy
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


# LLM: 结果离开工具前净化全部文本字段及嵌套键值；大小写变体覆盖Content-Encoding规范化，不改宿主效果事实。
# 函数用途: 防止已知出站秘密进入模型结果、恢复提示或操作账预览。
def scrub_push_outcome(result, secret):
    # LLM: 替换后再核密钥；标记与相邻文本重拼出秘密时清空整字段，不靠固定次数重复替换。
    # 函数用途: 递归去掉所有结果字段中的当前凭据及其大小写变体。
    def scrub(value):
        if isinstance(value, str):
            if not secret:
                return value
            pattern = re.compile(re.escape(secret), re.IGNORECASE)
            clean = pattern.sub("[REDACTED]", value)
            return "" if pattern.search(clean) else clean
        if isinstance(value, dict):
            return {scrub(key): scrub(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(scrub(item) for item in value)
        return value

    for field in fields(result):
        setattr(result, field.name, scrub(getattr(result, field.name)))
    return result


# LLM: 正式调用消费原Registry冻结的只读权限，直接调用兼容宿主当前边界；持久owner策略和冻结上界只可收紧。
# 函数用途: 在解密节点或DNS前拒绝禁网任务，不能由endpoint_ref或endpoint_url绕过。
def assert_push_network(agent, context):
    if context is not None:
        permissions = context.execution_permission_context or {}
        denied = permissions.get("network_access_mode") == "disabled" or permissions.get("network_enabled") is False
    else:
        boundary = getattr(getattr(agent, "_current_run_params", None), "write_boundary", None)
        boundary = boundary if isinstance(boundary, Mapping) else {}
        effective = boundary.get("effective_permissions") or {}
        denied = boundary.get("network_access_mode") == "disabled" or (
            isinstance(effective, Mapping) and effective.get("network_enabled") is False)
    if (denied or getattr(getattr(agent, "owner_policy", None), "network_enabled", True) is False
            or not resolve_effective_owner_policy(agent.home_paths).network_enabled):
        raise RemotePushError("NETWORK_ACCESS_DISABLED", 403)


# LLM: 所有DNS回答先核验再固定连接IP；原地址和IPv4投影各自核白名单，任何一侧的元数据/link-local永久拒绝。
# 函数用途: 为固定单向URL解析安全连接地址，不允许DNS重绑定或代理环境劫持。
def remote_push_pin(url, allowed_private_hosts):
    parts = urlsplit(validate_push_url(url))
    answers = tuple(dict.fromkeys(item[4][0] for item in socket.getaddrinfo(parts.hostname, parts.port or 443,
        type=socket.SOCK_STREAM)))
    if not answers:
        raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
    for value in answers:
        address = ipaddress.ip_address(value)
        for candidate in {address, ipv4_projected(address) or address}:
            if (candidate.is_link_local or candidate.is_multicast or candidate.is_unspecified
                    or str(candidate) in {"100.100.100.200", "fd00:ec2::254"}):
                raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
            restricted = (not candidate.is_global or candidate.is_private or candidate.is_loopback or candidate.is_reserved)
            if restricted and not private_host_or_ip_is_allowlisted(str(candidate), allowed_private_hosts):
                raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
        if parts.scheme != "https" and not private_host_or_ip_is_allowlisted(value, allowed_private_hosts):
            raise RemotePushError("REMOTE_PUSH_ENDPOINT_DENIED")
    return answers[0]


# LLM: 当前请求只签一次nonce/body；resolve_pin只放行第一次解析，同址/异址重定向都在下一跳连接前拒绝。
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
    resolved = False
    # LLM: 每次调用独立闭包只提供一次pin；即使同URL的307/308也不能重放签名POST。
    # 函数用途: 给原HTTP传输提供单次固定地址，任何第二跳立即拒绝。
    def resolve(target):
        nonlocal resolved
        if resolved or target != url:
            return PinResult(None, ToolHandlerOutcome("remote_agent_push", False, "拒绝推送重定向",
                error_code="REMOTE_PUSH_REDIRECT_DENIED", effect_outcome="unknown"))
        resolved = True
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
        return scrub_push_outcome(result, secret)
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

    # LLM: 正式Registry入口仅运输本次冻结权限，不改共享handler或读取别的并发任务边界。
    # 函数用途: 将宿主网络开关传给当前一次投递。
    def execute_scoped(self, params, context):
        return self.execute(params, _context=context)

    # LLM: 网络门在凭据/DNS前，source所有权在外发前；成功与失败都在原操作账记录之前统一脱敏，不重试未知效果。
    # 函数用途: 发送事件，按需携带原会话上下文，并返回短接收结果。
    def execute(self, params, *, _context=None):
        from cryptography.fernet import InvalidToken

        sending, secret = False, ""
        try:
            if self.agent is None:
                raise RemotePushError("REMOTE_PUSH_UNAVAILABLE")
            assert_push_network(self.agent, _context)
            snapshot = None
            if params.get("source_thread_id"):
                snapshot = export_remote_context(self.agent.conversation_store, params["source_thread_id"], self.agent.home_paths.owner_id)
            url, secret, allowed = resolve_remote_endpoint(self.agent, params)
            body = {key: value for key, value in params.items() if key not in {"endpoint_ref", "endpoint_url", "source_thread_id"}}
            if snapshot is not None:
                body["context_snapshot"] = snapshot
            body = validate_push_payload(sanitize_remote_context(body, secrets=(secret,)))
            sending = True
            return scrub_push_outcome(send_remote_push(url, secret, body, allowed, self.timeout), secret)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, InvalidToken) as exc:
            code = exc.error_code if isinstance(exc, RemotePushError) else str(exc) if str(exc) in {
                "REMOTE_PUSH_SOURCE_THREAD_DENIED", "REMOTE_PUSH_CONTEXT_TOO_LARGE"} else "REMOTE_PUSH_UNAVAILABLE"
            return scrub_push_outcome(ToolHandlerOutcome("remote_agent_push", False, code, error_code=code,
                effect_outcome="unknown" if sending else "not_started"), secret)
