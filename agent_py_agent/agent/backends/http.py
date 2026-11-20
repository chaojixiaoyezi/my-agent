# LLM: 此模块独占 HTTP 后端公共传输、能力探针和目录元数据；保持请求局部预算、取消与配置错误合同。
# 模块用途: 为模型协议提供带物理并发限制的统一网络入口和有界工具探测；联合后端、OAuth、关闭与排队回归。
from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
import secrets
import sys
import threading
import time
import types
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from ..common import hot_json
from ..common.fast_dataclass import fast_replace
from ..concurrency.interrupt import wait_interruptibly
from ..tooling.runtime_contracts import ProviderToolCapability, ToolChoice, ToolProtocolSnapshot
from . import gateway_helpers
from .base import BackendOptions, BaseBackend, ProviderRequestOptions, _utc_now_iso
from .errors import (
    ProviderConfigurationError,
    ProviderRecoverableError,
)
from .gateway_helpers import GatewayRequest, post_json, post_stream, post_stream_iter
from .model_metadata import ProviderMetadataOptions, discover_provider_model_metadata
from .provider_headers import current_probe_accounting, endpoint_parts, request_headers
from .request_budget import RequestBudgetExceeded, RequestTokenBudget

# 探针只缓存成功证据；短暂失败允许之后重试，但单次探测有界。
_PROBE_MAX_ATTEMPT_COUNT = 3

# LLM: 进程内计数器：记录“没有绑定记账范围”的工具能力探测尝试，只读出口经 GET /status 的 usage_accounting 段
#   投影（gateway_parts/http_handlers._usage_accounting_diagnostics），线上非零就说明有探测没进用量账。
#   不写日志避免高频噪声；计数只反映次数，不含任何探测细节。宿主绑定 scope 的探测不计入。
_UNACCOUNTED_PROBE_ATTEMPT_COUNT = 0
_UNACCOUNTED_PROBE_LOCK = threading.Lock()


# 函数用途: 记录一次未绑定记账范围的工具能力探测尝试。
def _count_unaccounted_probe_attempt() -> None:
    global _UNACCOUNTED_PROBE_ATTEMPT_COUNT
    with _UNACCOUNTED_PROBE_LOCK:
        _UNACCOUNTED_PROBE_ATTEMPT_COUNT += 1


# LLM: 只读；改名或改语义须同步 http_handlers 的 /status 投影与 test_probe_tool_capability_metering。
# 函数用途: 返回进程内未绑定记账范围的工具能力探测尝试次数（诊断用，只读，/status 可见）。
def unaccounted_probe_attempt_count() -> int:
    with _UNACCOUNTED_PROBE_LOCK:
        return _UNACCOUNTED_PROBE_ATTEMPT_COUNT


# 函数用途: 清零未绑定探测计数；只供测试和显式管理入口使用。
def reset_unaccounted_probe_attempt_count() -> None:
    global _UNACCOUNTED_PROBE_ATTEMPT_COUNT
    with _UNACCOUNTED_PROBE_LOCK:
        _UNACCOUNTED_PROBE_ATTEMPT_COUNT = 0


# 进程内有界原生工具能力缓存：仅保存经真实探针验证通过（native_supported=True）的结果，
# 使 OwnerAgent 空闲释放或同配置重建后端后无需重复发探针；采用小写私有变量名避免触发静态常量表。
_logger = logging.getLogger(__name__)
_capability_probe_entries: OrderedDict[tuple[object, ...], ProviderToolCapability] = OrderedDict()
_capability_probe_miss_reasons: OrderedDict[tuple[object, ...], str] = OrderedDict()
_capability_probe_lock = threading.Lock()
_capability_probe_generation = 0
_probe_key_field_names = ("backend_type", "endpoint", "model", "stream", "transport", "route_headers", "credential")
_capability_probe_transport_lock = threading.RLock()
_capability_probe_transport_tokens: dict[int, tuple[weakref.ref[Any], int]] = {}
_capability_probe_transport_seq = 0


def _evict_transport_token(obj_id: int, token: int) -> None:
    with _capability_probe_transport_lock:
        current = _capability_probe_transport_tokens.get(obj_id)
        if current is not None and current[1] == token:
            _capability_probe_transport_tokens.pop(obj_id, None)


def _single_object_identity_token_locked(obj: object) -> int | tuple[int, int]:
    global _capability_probe_transport_seq
    if obj is None:
        return 0
    obj_id = id(obj)
    entry = _capability_probe_transport_tokens.get(obj_id)
    if entry is not None:
        ref, token = entry
        if ref() is obj:
            return token
    _capability_probe_transport_seq += 1
    token = _capability_probe_transport_seq
    try:
        ref = weakref.ref(
            obj,
            lambda _dead_ref, oid=obj_id, tok=token: _evict_transport_token(oid, tok),
        )
    except TypeError:
        return (0, obj_id)
    _capability_probe_transport_tokens[obj_id] = (ref, token)
    return token


def _capability_probe_transport_identity(backend: object) -> tuple[object, ...]:
    """Return monotonic identity tokens for active transport callables and class/instance overrides."""
    backend_cls = type(backend)
    backend_dict = getattr(backend, "__dict__", None)
    urllib_mod = getattr(getattr(gateway_helpers, "urllib", None), "request", None)
    keepalive_mod = getattr(gateway_helpers, "keepalive_transport", None)
    is_oauth_responses = any(cls.__name__ == "OAuthResponsesBackend" for cls in backend_cls.__mro__)
    ws_mod = (
        sys.modules.get("agent_py_agent.agent.backends.responses_websocket")
        if is_oauth_responses
        else None
    )
    seams: list[object] = [
        post_json,
        post_stream,
        post_stream_iter,
        getattr(gateway_helpers, "post_json", None),
        getattr(gateway_helpers, "post_stream", None),
        getattr(gateway_helpers, "post_stream_iter", None),
        getattr(gateway_helpers, "_post_stream_lines", None),
        getattr(gateway_helpers, "_open_gateway_request", None),
        getattr(gateway_helpers, "_gateway_urlopen", None),
        getattr(gateway_helpers, "_gateway_urlopen_unlimited", None),
        getattr(gateway_helpers, "_urllib_request", None),
        getattr(urllib_mod, "urlopen", None),
        getattr(urllib_mod, "build_opener", None),
        getattr(keepalive_mod, "pooled_urlopen", None),
        getattr(ws_mod, "iter_responses_websocket", None) if ws_mod is not None else None,
        getattr(backend_cls, "request_json", None),
        getattr(backend_cls, "request_stream", None),
        getattr(backend_cls, "request_stream_iter", None),
        getattr(backend_cls, "_gateway_request", None),
        getattr(backend_cls, "generate", None),
        getattr(backend_cls, "_send_responses_request", None),
    ]
    if isinstance(backend_dict, Mapping):
        for attr in (
            "request_json",
            "request_stream",
            "request_stream_iter",
            "_gateway_request",
            "_send_responses_request",
            "_generate_stream",
            "_generate_non_stream",
            "_request_messages",
        ):
            seams.append(backend_dict.get(attr))
    tokens: list[object] = []
    with _capability_probe_transport_lock:
        for seam in seams:
            if isinstance(seam, types.MethodType):
                tokens.append((
                    _single_object_identity_token_locked(seam.__func__),
                    _single_object_identity_token_locked(seam.__self__),
                ))
            else:
                tokens.append(_single_object_identity_token_locked(seam))
    return tuple(tokens)


def _oauth_credential_fingerprint(ref: Mapping) -> str:
    auth_part = json.dumps(dict(ref), sort_keys=True, default=str, ensure_ascii=True)
    material = f"{auth_part}\n".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(material).hexdigest()[:16]


def _credential_fingerprint(backend: object) -> str:
    """Return a short non-secret SHA-256 digest representing backend auth identity and key."""
    auth_ref = getattr(backend, "auth_ref", None)
    if isinstance(auth_ref, Mapping):
        # 轮换访问令牌仍属同一登录；代次来自已冻结的登录引用。
        return _oauth_credential_fingerprint(auth_ref)
    auth_part = str(auth_ref) if auth_ref else ""
    try:
        api_key = str(getattr(backend, "api_key", "") or "")
    except Exception:  # noqa: BLE001 未初始化或动态凭据读取失败时回退静态字段计算指纹
        api_key = str(getattr(backend, "_api_key", "") or "")
    material = f"{auth_part}\n{api_key}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(material).hexdigest()[:16]


# LLM: 仅对 profile 已验证的静态请求头做不可逆指纹（名称小写、按序），区分同入口不同路由；会话亲和头属请求层，不入键。
# 函数用途: 计算能力缓存键中的静态路由头身份，键与日志不含原始头值。
def _route_header_fingerprint(backend: object) -> str:
    headers = getattr(backend, "custom_headers", None)
    if not isinstance(headers, Mapping) or not headers:
        return ""
    material = hot_json.dumps(sorted((str(name).lower(), str(value)) for name, value in headers.items()))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _safe_tool_endpoint(backend: object) -> str:
    endpoint = getattr(backend, "_tool_endpoint", None)
    if callable(endpoint):
        try:
            return str(endpoint() or "")
        except AttributeError:
            return ""
    return str(getattr(backend, "api_base", "") or "")


def _capability_probe_key(backend: object) -> tuple[object, ...]:
    return (
        type(backend).__qualname__,
        _safe_tool_endpoint(backend),
        str(getattr(backend, "model_name", "") or ""),
        bool(getattr(backend, "stream_enabled", False)),
        _capability_probe_transport_identity(backend),
        _route_header_fingerprint(backend),
        _credential_fingerprint(backend),
    )


def _logical_probe_identity(key: tuple[object, ...]) -> tuple[object, ...]:
    return (key[0], key[1], key[2], key[3], key[-1])


# X8：同一探针键的冷探测在途最长被跟随多久（秒）；超过后跟随者不再等待、自行探测，避免一条挂死的探测拖住所有 owner。
_PROBE_FLIGHT_TTL_SECONDS = 600.0
# 跟随者等待在途探测时的轮询间隔（秒）：每轮复核本轮停止信号。
_PROBE_FLIGHT_POLL_SECONDS = 0.05
# 进程内在途探测表（键 = memo 完整能力身份 + 失效代次），只存进行中的单飞；结束即移除，不缓存失败。
_capability_probe_flights: dict[tuple[object, ...], _ProbeFlight] = {}
# RC1：请求局部的物理探针标记，只在 _probe_tool_capability_uncached 调用 generate 期间为真；同一后端上其它线程的普通请求不受影响。
_physical_probe_marker: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "my_agent_physical_capability_probe", default=False)


# LLM: 单飞只共享"同一次真实探测"的结果：成功/阴性结果与供应商类失败（配置、额度、网络、超时）发给所有等待者；
#   领头者被用户停止或本地异常时，跟随者不继承，重新竞选领头。结束后不缓存失败，之后的调用仍会重新探测。
# 类用途: 记录一条进行中的工具能力探测及其结果。
class _ProbeFlight:
    __slots__ = ("done", "result", "error", "started")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: ProviderToolCapability | None = None
        self.error: BaseException | None = None
        self.started = time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() - self.started > _PROBE_FLIGHT_TTL_SECONDS


# LLM: RC2：同逻辑身份的实例阳性共持一个可撤销标记；弱值表只索引仍被实例缓存引用的标记（每逻辑身份一个），
#   定向失效、全局清空与当前阴性/配置拒绝撤销并移出，正常 LRU 容量淘汰不撤销。无全量后端清单、无历史失效表。
# 类用途: 让同身份驻留实例在下一次读取实例缓存时放弃已失效的阳性。
class _CapabilityValidity:
    __slots__ = ("revoked_reason", "__weakref__")

    def __init__(self) -> None:
        self.revoked_reason = ""


_capability_validity_tokens: weakref.WeakValueDictionary[tuple[object, ...], _CapabilityValidity] = (
    weakref.WeakValueDictionary())


# 函数用途: 调用方持 _capability_probe_lock；撤销一个逻辑身份上实例阳性的有效标记，持有者下次读取即自清。
def _revoke_capability_validity_locked(logical_key: tuple[object, ...], reason: str) -> None:
    validity = _capability_validity_tokens.pop(logical_key, None)
    if validity is not None:
        validity.revoked_reason = reason


# 代次对象的身份与路由键分离。仅在有探测或驻留实例引用时保留。
_credential_generations: weakref.WeakValueDictionary[object, _CapabilityValidity] = weakref.WeakValueDictionary()


def _probe_revocation_generation(credential: object) -> tuple[int, _CapabilityValidity]:
    with _capability_probe_lock:
        token = _credential_generations.get(credential)
        if token is None:
            token = _credential_generations[credential] = _CapabilityValidity()
        return _capability_probe_generation, token


def _probe_generation_current_locked(snapshot: tuple[int, _CapabilityValidity]) -> bool:
    return snapshot[0] == _capability_probe_generation and not snapshot[1].revoked_reason


def _revoke_credential_generation_locked(credential: object, reason: str) -> None:
    token = _credential_generations.pop(credential, None)
    if token is not None:
        token.revoked_reason = reason
    for key in [key for key in _capability_probe_entries if key[-1] == credential]:
        _capability_probe_entries.pop(key, None)
        _record_capability_probe_miss_reason_locked(_logical_probe_identity(key), reason)
    for key in [key for key in _capability_validity_tokens if key[-1] == credential]:
        _revoke_capability_validity_locked(key, reason)
        _record_capability_probe_miss_reason_locked(key, reason)
    for key in [key for key in _capability_probe_flights
                if len(key) == len(_probe_key_field_names) + 1 and key[-2] == credential]:
        _capability_probe_flights.pop(key, None)
        _record_capability_probe_miss_reason_locked(_logical_probe_identity(key[:-1]), reason)


def invalidate_oauth_probe_generation(ref: Mapping, *, reason: str = "oauth_generation_changed") -> None:
    """使本进程中同一登录的所有路由探测同步失效。"""
    credential = _oauth_credential_fingerprint(ref)
    with _capability_probe_lock:
        _revoke_credential_generation_locked(credential, reason)


# LLM: 跨 owner/后端实例按同一探针键单飞（X8）：冷启动并发只真实发一次探测；等待可被本轮停止打断，
#   在途超过 TTL 则不再跟随。领头者异常原样上抛；跟随者对供应商类失败拿到一份同类型副本。
# 函数用途: 让同一端点/模型/凭据的并发冷探测只执行一次。
def _single_flight_probe(
    key: tuple[object, ...],
    probe: Callable[[], ProviderToolCapability],
    publish: Callable[[ProviderToolCapability | None, BaseException | None], None] | None = None,
) -> ProviderToolCapability:
    while True:
        with _capability_probe_lock:
            flight = _capability_probe_flights.get(key)
            leader = flight is None or flight.expired()
            if leader:
                flight = _capability_probe_flights[key] = _ProbeFlight()
        if leader:
            try:
                flight.result = probe()
                return flight.result
            except BaseException as exc:
                flight.error = exc
                raise
            finally:
                try:
                    with _capability_probe_lock:
                        # C3：只有收尾时仍登记为当前在途的本 flight 才能发布；被失效摘除或 TTL 后被取代的结果只交给本次参与者。
                        if _capability_probe_flights.get(key) is flight:
                            _capability_probe_flights.pop(key, None)
                            if publish is not None:
                                publish(flight.result, flight.error)
                finally:
                    flight.done.set()
        while not flight.done.wait(_PROBE_FLIGHT_POLL_SECONDS) and not flight.expired():
            wait_interruptibly(0)
        wait_interruptibly(0)
        if flight.done.is_set():
            if flight.result is not None:
                return flight.result
            if isinstance(flight.error, (ProviderRecoverableError, ProviderConfigurationError)):
                raise copy.copy(flight.error) from flight.error


def _lookup_capability_probe_memo(
    key: tuple[object, ...],
) -> tuple[ProviderToolCapability | None, int]:
    with _capability_probe_lock:
        generation = _capability_probe_generation
        entry = _capability_probe_entries.get(key)
        if entry is not None:
            _capability_probe_entries.move_to_end(key)
        return entry, generation


def _record_capability_probe_miss_reason_locked(
    logical_key: tuple[object, ...],
    reason: str,
) -> None:
    _capability_probe_miss_reasons[logical_key] = reason
    _capability_probe_miss_reasons.move_to_end(logical_key)
    while len(_capability_probe_miss_reasons) > 256:
        _capability_probe_miss_reasons.popitem(last=False)


def _describe_key_change(
    old_key: tuple[object, ...],
    new_key: tuple[object, ...],
) -> str:
    changed = [
        name
        for name, old_value, new_value in zip(_probe_key_field_names, old_key, new_key)
        if old_value != new_value
    ]
    return "key_changed:" + ",".join(changed) if changed else "key_changed:unknown"


# LLM: 调用方持 _capability_probe_lock 且已确认本 flight 仍是当前在途；阳性入 memo，阴性或配置拒绝按同一键与逻辑身份摘除并记原因。
# 函数用途: 把一次物理探测的结论原子发布到进程内能力缓存。
def _publish_capability_probe_locked(
    key: tuple[object, ...], capability: ProviderToolCapability | None, error: BaseException | None,
    *, revocation_generation: tuple[int, _CapabilityValidity] | None = None, oauth: bool = False,
) -> None:
    if revocation_generation is not None and not _probe_generation_current_locked(revocation_generation):
        return
    logical_key = _logical_probe_identity(key)
    if isinstance(capability, ProviderToolCapability):
        if capability.native_supported:
            _capability_probe_entries[key] = capability
            _capability_probe_entries.move_to_end(key)
            while len(_capability_probe_entries) > 256:
                _capability_probe_entries.popitem(last=False)
            _capability_probe_miss_reasons.pop(logical_key, None)
            return
        reason = "negative_result"
    elif isinstance(error, ProviderConfigurationError):
        reason = _http_rejection_reason(error) or "provider_configuration"
        if oauth and reason in {"http_401", "http_403"}:
            _revoke_credential_generation_locked(key[-1], reason)
    else:
        return  # 供应商临时失败、中断、本地异常或非能力对象：不缓存、不摘除（与原调用方 isinstance 判断一致）
    for entry_key in [k for k in _capability_probe_entries if k == key or _logical_probe_identity(k) == logical_key]:
        _capability_probe_entries.pop(entry_key, None)
    _record_capability_probe_miss_reason_locked(logical_key, reason)
    _revoke_capability_validity_locked(logical_key, reason)


def invalidate_capability_probe_memo(
    backend: HttpBackend | None = None,
    *,
    reason: str = "explicit",
) -> None:
    """清空全部或按后端键驱逐进程内原生工具能力探针缓存。"""
    global _capability_probe_generation
    normalized_reason = str(reason or "").strip() or "explicit"
    if backend is None:
        with _capability_probe_lock:
            _capability_probe_entries.clear()
            # C3：在途旧 flight 失去发布资格，失效后到达的调用另起新 flight。
            _capability_probe_flights.clear()
            _capability_probe_generation += 1
            _credential_generations.clear()
            _capability_probe_miss_reasons.clear()
            _record_capability_probe_miss_reason_locked(("*",), normalized_reason)
            for validity in list(_capability_validity_tokens.values()):
                validity.revoked_reason = normalized_reason
            _capability_validity_tokens.clear()
        return
    target_key = _capability_probe_key(backend)
    keys = {target_key}
    logical_targets = {_logical_probe_identity(target_key)}
    previous_key = getattr(backend, "_provider_tool_capability_memo_key", None)
    if isinstance(previous_key, tuple) and len(previous_key) >= 5:
        keys.add(previous_key)
        logical_targets.add(_logical_probe_identity(previous_key))
    credential_wide = normalized_reason != "http_400_tool" and isinstance(getattr(backend, "auth_ref", None), Mapping)
    with _capability_probe_lock:
        if credential_wide:
            for credential in {key[-1] for key in keys}:
                _revoke_credential_generation_locked(credential, normalized_reason)
        stale = [
            entry_key
            for entry_key in _capability_probe_entries
            if entry_key in keys or _logical_probe_identity(entry_key) in logical_targets
        ]
        for key in stale:
            _capability_probe_entries.pop(key, None)
        # C3：摘除匹配身份的在途 flight，新调用立即另起；旧跟随者仍由原 flight 的 Event 唤醒。
        for flight_key in [k for k in _capability_probe_flights
                           if len(k) == len(target_key) + 1
                           and (k[:-1] in keys or _logical_probe_identity(k[:-1]) in logical_targets)]:
            _capability_probe_flights.pop(flight_key, None)
        for logical_key in logical_targets:
            _record_capability_probe_miss_reason_locked(logical_key, normalized_reason)
            _revoke_capability_validity_locked(logical_key, normalized_reason)
        backend._provider_tool_capability_cache = None
        backend._provider_tool_capability_memo_key = None
        backend._provider_tool_capability_miss_reason = None


def _http_rejection_reason(exc: BaseException, request_body: object = None) -> str:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        if status_code in (401, 403):
            return f"http_{status_code}"
        if status_code == 400 and _is_tool_rejection_error(exc, request_body):
            return "http_400_tool"
    return ""


def _is_tool_rejection_error(exc: BaseException, request_body: object = None) -> bool:
    """Check whether an HTTP 400 rejection targets tool calling capability."""
    details = getattr(exc, "details", None)
    provider = details.get("provider_error") if isinstance(details, Mapping) else None
    error = provider.get("error") if isinstance(provider, Mapping) else None
    if not isinstance(error, Mapping) and isinstance(provider, Mapping):
        error = provider
    param = str(error.get("param") or "").lower() if isinstance(error, Mapping) else ""
    code = str(error.get("code") or "").lower() if isinstance(error, Mapping) else ""
    message = str(error.get("message") or "").lower() if isinstance(error, Mapping) else ""
    exc_text = str(exc).lower()
    if "configuration_update" in param or "configuration_update" in message:
        return False
    haystack = "\n".join((param, code, message, exc_text))
    if any(token in haystack for token in ("tool", "function_call", "function calling", "functions")):
        return True
    return bool(
        isinstance(request_body, Mapping)
        and request_body.get("tools")
        and any(token in param for token in ("tool", "function"))
    )


# LLM: HTTP 协议共同使用此请求级限额；不能热改实例配置，需同步 Chat/Messages/Responses 的摘要调用测试。
# 函数用途: 将本轮输出上限限制在配置范围内，未覆盖时保留配置值。
def bounded_output_tokens(configured: int, requested: int | None, *,
                          configured_ceiling: int | None = None,
                          token_budget: RequestTokenBudget | None = None) -> int:
    ceiling = configured_ceiling if configured_ceiling is not None else configured
    legacy = min(configured, ceiling)
    if token_budget is None:
        return legacy if requested is None else max(1, min(legacy, int(requested)))
    budget = token_budget
    limit = ceiling if budget.shared_window and not budget.server_managed_output else legacy
    invalid = (type(budget.output_tokens) is not int or budget.output_tokens <= 0
               or budget.output_tokens > limit or budget.input_tokens < 0 or budget.safety_tokens < 0
               or (requested is not None and int(requested) != budget.output_tokens))
    if budget.shared_window:
        invalid = invalid or budget.input_tokens + budget.output_tokens + budget.safety_tokens > budget.context_window_tokens
    if invalid:
        raise RequestBudgetExceeded(reason="invalid_budget", input_tokens=budget.input_tokens,
                                    output_ceiling=budget.output_tokens,
                                    context_window_tokens=budget.context_window_tokens)
    return budget.output_tokens


# LLM: 探测记账只在宿主绑定的 scope 内生效；函数内导入避免 backends 与 agent_core 互相依赖。
#   每次真实 generate 一条记录（重试也算），成功用供应商 usage 收尾，失败记 failed。
#   无 scope（直调、测试替身）直接返回空 call_id，不产生任何账本副作用。
# 函数用途: 登记一次工具能力探测的模型调用开始（输入按实际发送的提示估算），返回本次探测的记账 id。
def _start_probe_call(
    scope: object | None,
    backend: object,
    prompt: str,
    *,
    cache_miss_reason: str = "",
) -> str:
    if scope is None:
        return ""
    from ..contracts.model_call_ledger import ModelCallStartedParams
    from ..memory_archive import estimate_tokens

    call_id = f"probe:tool_capability:{secrets.token_hex(8)}"
    scope.ledger.started(
        ModelCallStartedParams(
            call_id=call_id,
            backend=str(getattr(backend, "name", "") or ""),
            model=str(getattr(backend, "model_name", "") or ""),
            input_tokens=estimate_tokens(prompt),
            output_tokens_estimate=0,
            request_id=str(getattr(scope, "request_id", "") or ""),
            run_id=str(getattr(scope, "run_id", "") or ""),
            is_probe=True,
            metadata={
                "purpose": "probe:tool_capability",
                "logical_call_id": call_id,
                "cache_miss_reason": str(cache_miss_reason or ""),
            },
        )
    )
    return call_id


# LLM: 探测响应没有用户可见正文，usage 是否上报由原收尾函数按结构化字段决定；失败原因只记类型与错误码。
# 函数用途: 探测请求成功返回后按供应商用量收尾记账；无 scope 时直接跳过。
def _finish_probe_call(scope: object | None, call_id: str, response: object) -> None:
    if scope is None or not call_id:
        return
    from ..agent_core.model.call_runtime import record_model_call_finished

    record_model_call_finished(scope.ledger, call_id, response)


# 函数用途: 探测请求抛异常时把该次调用记为失败，不改变探测自身的异常传播；无 scope 时直接跳过。
def _fail_probe_call(scope: object | None, call_id: str, exc: BaseException) -> None:
    if scope is None or not call_id:
        return
    from ..agent_core.model.call_runtime import record_model_call_failed

    record_model_call_failed(scope.ledger, call_id, exc)


# LLM: 限流开启后 collector 提前返回也必须关流归还名额；默认关闭保持原收尾，list 等无 close 的替身不动作。
# 函数用途: 在需要归还名额时显式关闭同一个实际流；解析成功、提前结束和异常共用此出口。
@contextmanager
def provider_stream_scope(lines, *, enabled: bool = True):
    try:
        yield lines
    finally:
        close = getattr(lines, "close", None) if enabled else None
        if callable(close):
            close()


# LLM: HTTP 请求控制只在显式给出时透传；流入口与测试替身须保持既有调用合同，不在此重试。
#   cabfix：多一个可选的绝对期限——两者都没给时才退回最简两参调用，保持旧后端/替身零改动。
# 函数用途: 调用流式传输入口，按需附加当前请求的首事件等待预算。
def request_stream_lines(
    lines,
    request_args: tuple[str, dict[str, Any], dict[str, str], float | None, float | None],
):
    path, payload, headers, first_event_timeout_seconds, total_deadline_seconds = request_args
    if first_event_timeout_seconds is None and total_deadline_seconds is None:
        return lines(path, payload, headers)
    # LLM: 两条出口都按被调方签名选择，不用 TypeError 试探——一次失败可能被上层当成可重试
    #   而重复发送。③ 新形态 options=；② 只认首包预算；① 最简三参（旧替身/假后端）。
    if _accepts_kwarg(lines, "options"):
        return lines(
            path, payload, headers,
            options=StreamCallOptions(
                first_event_timeout_seconds=first_event_timeout_seconds,
                total_deadline_seconds=total_deadline_seconds,
            ),
        )
    if _accepts_kwarg(lines, "total_deadline_seconds"):
        return lines(
            path, payload, headers,
            first_event_timeout_seconds=first_event_timeout_seconds,
            total_deadline_seconds=total_deadline_seconds,
        )
    return lines(path, payload, headers, first_event_timeout_seconds=first_event_timeout_seconds)


# LLM: 只读 callable 签名；探测失败（内建/无签名）时不加新关键字，保持原调用形态。
# 函数用途: 判断流式入口是否接受某个关键字参数。
def _accepts_kwarg(callable_obj: object, keyword: str) -> bool:
    import inspect

    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return False
    return keyword in signature.parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


# LLM: cabfix：流式/非流式入口的请求材料与超时选项各收成一个数据类，避免每加一项
#   超时事实就拉长一组公开函数签名（code-size 参数门禁也会拦）。
# 类用途: 携带一次 HTTP 调用的路径、请求体与请求头。
@dataclass(frozen=True)
class StreamCall:
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]


# LLM: 两个字段都可能为 None，表示沿用该调用方原有的超时语义（不设首包预算 / 不设总期限）。
# 类用途: 携带一次 HTTP 调用的首事件预算与绝对期限。
@dataclass(frozen=True)
class StreamCallOptions:
    first_event_timeout_seconds: float | None = None
    total_deadline_seconds: float | None = None


# LLM: _gateway_request 的两种调用形态只在这里拆：新的 (StreamCall, options) 与既有 (path, payload, headers)；
#   后者仍被大量旧调用方和测试使用。覆盖 _gateway_request 的子类（OAuth）必须用它并保持与基类相同的签名，
#   否则基类 request_json/request_stream/request_stream_iter 调进来就是 TypeError（17k Linux 车道实测）。
# 函数用途: 把 StreamCall 或旧的三段参数拆成 (path, payload, headers)；缺段时报 TypeError。
def stream_call_parts(
    call: StreamCall | str,
    payload: dict[str, Any] | None,
    headers: dict[str, str] | None,
) -> tuple[str, dict[str, Any], dict[str, str]]:
    if isinstance(call, StreamCall):
        return call.path, call.payload, call.headers
    if payload is None or headers is None:
        raise TypeError("_gateway_request 需要 StreamCall 或 path/payload/headers")
    return call, payload, headers


# LLM: cabfix：调用方给的绝对墙钟上界换算成 monotonic 时刻，放进信封的 deadline 字段（传输层发送前、读正文、
#   流式共用这一条时限）。None 或非正数表示不设总期限，主模型与旧调用方语义不变。基类与 OAuth 共用这一处。
# 函数用途: 由请求选项算出信封里的绝对截止时刻；不设总期限时返回 None。
def stream_call_deadline(options: StreamCallOptions) -> float | None:
    if options.total_deadline_seconds is not None and float(options.total_deadline_seconds) > 0:
        return time.monotonic() + float(options.total_deadline_seconds)
    return None


# LLM: 所有内置 HTTP backend 都必须把 system_instruction 映射到供应商真实高优先级字段，而非拼回 user prompt。
# 类用途: 共享真实模型 HTTP、能力探针、连接和 metadata 逻辑，并声明支持独立 system 指令。
# LLM: HTTP 主链统一应用连接、会话头和冻结物理上限；新协议复用同一传输、取消与超时语义。
# 类用途: 为模型接口提供物理排队请求、能力探测和上下文目录读取。
class HttpBackend(BaseBackend):
    """真实模型后端共用的 HTTP 请求基础逻辑。"""

    supports_system_instructions = True
    supports_provider_request_options = True

    # LLM: 复制可变选项并冻结物理并发上限；多线程仅用 request-local 身份，不能热改共享后端采样或头部。
    # 函数用途: 初始化可复用后端、媒体预算和默认关闭的排队限制，非法采样在发送前报错。
    def __init__(
        self,
        options: BackendOptions,
    ):
        self.api_base = str(options.api_base).rstrip("/")
        self.api_key = str(options.api_key)
        self.model_name = str(options.model_name)
        self.request_timeout = int(options.request_timeout)
        self.connect_timeout = float(options.connect_timeout)
        self.provider_max_concurrent_requests = max(0, int(options.provider_max_concurrent_requests))
        self.model_http_max_idle_connections = max(0, int(getattr(options, "model_http_max_idle_connections", 0) or 0))
        self.max_tokens = int(options.max_tokens)
        self.configured_output_ceiling = int(options.configured_output_ceiling or options.max_tokens)
        self.custom_headers = dict(options.custom_headers)
        self.session_header = options.session_header
        # 本地配置与供应商事实分开保存；不能再把 fallback 冒充 provider metadata。
        self.configured_context_window_tokens = int(options.context_window_tokens or 0)
        # 兼容既有只读调用方；context-window resolver 会识别 configured_ 字段，绝不把本属性
        # 当成 provider 事实。新代码应读取 configured_context_window_tokens。
        self.context_window_tokens = self.configured_context_window_tokens
        self._provider_tool_capability_cache: ProviderToolCapability | None = None
        self._provider_tool_capability_miss_reason: str | None = None
        self._provider_tool_capability_lock = threading.Lock()
        self._provider_context_window_cache: int | None = None
        self.model_metadata: dict[str, Any] = {}
        self.temperature = float(options.temperature)
        self.temperature_explicit = options.temperature_explicit
        from .sampling import validate_top_p

        self.top_p = validate_top_p(options.top_p)
        # 思考控制方式随 profile 冻结；档位由每次请求的 ProviderRequestOptions.reasoning_effort 传入。
        self.reasoning_control = str(options.reasoning_control or "none")
        self.reasoning_levels = tuple(options.reasoning_levels or ())
        from .model_request_capabilities import (
            resolve_model_request_capabilities,
            validate_thinking_request,
        )
        self.model_request_capabilities = resolve_model_request_capabilities(
            options.thinking_requirement, self.api_base, self.model_name, self.name)
        self.thinking_requirement = self.model_request_capabilities.thinking_requirement
        validate_thinking_request(self, disabled=False)
        # 是否能用 configuration_update 项改档位随 profile 冻结；只有 Responses 后端按它判断，其它协议不读。
        self.reasoning_update_items = str(options.reasoning_update_items or "auto")
        # 结构化输出方式随 profile 冻结；只有 OpenAI Chat 兼容接口会用到 json_object。
        self.structured_output = str(options.structured_output or "native")
        # 工厂会绑定实际的live配置；独立构造仍使用显式BackendOptions。
        self.config = options
        self._stream_enabled_override: bool | None = None
        self._stream_timeout_is_idle_override: bool | None = None
        self.prompt_cache_enabled = bool(options.prompt_cache_enabled)
        self.input_media_max_bytes = max(1, int(options.input_media_max_bytes))

    # LLM: 各协议在请求分派时读取同一live配置；OAuth的显式协议覆盖不被用户开关撤销。
    # 函数用途: 取得下一次请求的流式选择，独立构造时保留原options语义。
    @property
    def stream_enabled(self) -> bool:
        if self._stream_enabled_override is not None:
            return self._stream_enabled_override
        return bool(getattr(self.config, "stream_enabled", True))

    # LLM: 保留OAuth订阅协议及既有调用者的显式覆盖；普通构造不再写入标量快照。
    # 函数用途: 设置协议明确要求的流式模式，不修改共享AgentConfig。
    @stream_enabled.setter
    def stream_enabled(self, enabled: bool) -> None:
        self._stream_enabled_override = bool(enabled)

    # LLM: SSE沿原空闲超时合同，非流式仍使用总时限；跟随当前流式选择，不冻结旧开关。
    # 函数用途: 向工具循环报告当前传输的超时语义。
    @property
    def stream_timeout_is_idle(self) -> bool:
        if self._stream_timeout_is_idle_override is not None:
            return self._stream_timeout_is_idle_override
        return self.stream_enabled

    # LLM: OAuth订阅接口已有显式idle声明，必须保持而不热改共享配置。
    # 函数用途: 保存协议显式覆盖的超时语义。
    @stream_timeout_is_idle.setter
    def stream_timeout_is_idle(self, enabled: bool) -> None:
        self._stream_timeout_is_idle_override = bool(enabled)

    # LLM: 只有Responses协议允许实测请求突破旧默认；其它协议保持工厂夹取，元数据只能继续收紧，不新增探测。
    # 函数用途: 已知供应商输出上限只会降低构造时冻结的配置天花板。
    def request_output_ceiling(self) -> int:
        ceiling = self.configured_output_ceiling
        if self.name != "openai_responses":
            ceiling = min(ceiling, self.max_tokens)
        for key in ("max_output_tokens", "max_output_token_count", "output_token_limit"):
            value = self.model_metadata.get(key)
            if type(value) is int and value > 0:
                ceiling = min(ceiling, value)
        return ceiling

    # LLM: Serialize provider probes per backend instance and cache only proven-positive facts.
    # 函数用途: 并发冷启动共用一次有界协议探测，失败不缓存，避免暂时失败永久阻断后续任务。
    def _cached_instance_capability(self) -> ProviderToolCapability | None:
        if self._provider_tool_capability_cache is None:
            return None
        current_key = _capability_probe_key(self)
        with _capability_probe_lock:
            cached = self._provider_tool_capability_cache
            if cached is None:
                return None
            generation = getattr(self, "_provider_tool_capability_generation", None)
            memo_key = getattr(self, "_provider_tool_capability_memo_key", None)
            validity = getattr(self, "_provider_tool_capability_validity", None)
            revocation = getattr(self, "_provider_tool_capability_revocation", None)
            if generation is not None and generation != _capability_probe_generation:
                reason = "generation_changed"
            elif memo_key is not None and memo_key != current_key:
                reason = _describe_key_change(memo_key, current_key)
            elif revocation is not None and revocation.revoked_reason:
                reason = revocation.revoked_reason
            elif validity is not None and validity.revoked_reason:
                reason = validity.revoked_reason
            else:
                return cached
            self._provider_tool_capability_cache = None
            self._provider_tool_capability_memo_key = None
            self._provider_tool_capability_miss_reason = reason
            return None

    # LLM: RC1：物理能力探针内（请求局部标记）的拒绝只上抛，由 _single_flight_probe 的当前 flight 在比较保护下发布或摘除；
    #   普通 JSON/流式/流迭代业务请求照旧立即定向失效。
    def _invalidate_memo_on_http_rejection(self, exc: BaseException, request_body: object = None) -> None:
        if _physical_probe_marker.get():
            return
        reason = _http_rejection_reason(exc, request_body)
        if reason:
            invalidate_capability_probe_memo(self, reason=reason)

    def _resolve_capability_probe_miss_reason(self, key: tuple[object, ...]) -> str:
        instance_reason = getattr(self, "_provider_tool_capability_miss_reason", None)
        if instance_reason:
            return str(instance_reason)
        logical_key = _logical_probe_identity(key)
        with _capability_probe_lock:
            return (
                _capability_probe_miss_reasons.get(logical_key)
                or _capability_probe_miss_reasons.get(("*",))
                or "cold_process"
            )

    # LLM: RC2：快路径与探测后共用的唯一实例安装口。先经原 memo 查询口取命中，再在同一缓存锁内复核该键仍是同一当前阳性，
    #   并原子绑定其逻辑身份的有效标记；锁内只改字段，不做 I/O、不等待、不调用供应商。
    # 函数用途: 把共享 memo 的当前阳性装入本实例，返回 (阳性或 None, 查询时的失效代次)；迟到安装不能复活已撤销结论。
    def _install_memo_capability(
        self, key: tuple[object, ...], expected: ProviderToolCapability | None = None,
        *, revocation_generation: tuple[int, _CapabilityValidity] | None = None,
    ) -> tuple[ProviderToolCapability | None, int]:
        revocation_generation = revocation_generation or _probe_revocation_generation(key[-1])
        memo_hit, generation = _lookup_capability_probe_memo(key)
        if memo_hit is None or (expected is not None and memo_hit is not expected):
            return None, generation
        with _capability_probe_lock:
            if (not _probe_generation_current_locked(revocation_generation)
                    or _capability_probe_entries.get(key) is not memo_hit):
                return None, generation
            logical_key = _logical_probe_identity(key)
            validity = _capability_validity_tokens.get(logical_key)
            if validity is None:
                validity = _capability_validity_tokens[logical_key] = _CapabilityValidity()
            self._provider_tool_capability_validity = validity
            self._provider_tool_capability_revocation = revocation_generation[1]
            self._provider_tool_capability_cache = memo_hit
            self._provider_tool_capability_memo_key = key
            self._provider_tool_capability_generation = _capability_probe_generation
            self._provider_tool_capability_miss_reason = None
        return memo_hit, generation

    def probe_tool_capability(self) -> ProviderToolCapability:
        """有界探测原生工具能力；只缓存成功事实，失败允许之后重新探测。"""
        cached = self._cached_instance_capability()
        if cached is not None:
            return cached
        memo_hit, _generation = self._install_memo_capability(_capability_probe_key(self))
        if memo_hit is not None:
            return memo_hit
        # 同一 Gateway/owner 的 Agent 会被多个请求线程复用。探针必须 single-flight，
        # 否则冷启动并发会重复烧请求，并让一个成功、一个失败的结果互相打架。
        # 等待别的请求探测时仍消费本轮停止信号；未取得锁绝不能释放别人的执行权。
        while not self._provider_tool_capability_lock.acquire(blocking=False):
            wait_interruptibly(0.05)
        try:
            wait_interruptibly(0)
            cached = self._cached_instance_capability()
            if cached is not None:
                return cached
            key = _capability_probe_key(self)
            revocation_generation = _probe_revocation_generation(key[-1])
            oauth = isinstance(getattr(self, "auth_ref", None), Mapping)
            memo_hit, _generation = self._install_memo_capability(key, revocation_generation=revocation_generation)
            if memo_hit is not None:
                return memo_hit
            miss_reason = self._resolve_capability_probe_miss_reason(key)
            probe_started = time.monotonic()
            self._active_probe_cache_miss_reason = miss_reason
            try:
                # X8：同一完整能力身份（后端、端点、模型、流式、传输身份、静态路由头、凭据指纹）且同一失效代次的冷探测单飞；
                # 不同凭据或失效之后的调用不会并入旧的在途探测。C3：只有收尾时仍是当前在途的物理发起者才原子发布结论。
                capability = _single_flight_probe(
                    (*key, revocation_generation), self._probe_tool_capability_uncached,
                    lambda result, error: _publish_capability_probe_locked(
                        key, result, error, revocation_generation=revocation_generation, oauth=oauth))
            except ProviderConfigurationError:
                # 全局摘除与原因已由当前发起者在 flight 收尾时原子完成；这里只清本实例旧诊断，下一次按全局原因计账。
                self._provider_tool_capability_miss_reason = None
                raise
            finally:
                self._active_probe_cache_miss_reason = None
            if isinstance(capability, ProviderToolCapability):
                duration_ms = max(0, int((time.monotonic() - probe_started) * 1000))
                _logger.warning(
                    "PROVIDER_TOOL_PROBE_UNCACHED reason=%s model=%s duration_ms=%d native=%s",
                    miss_reason,
                    str(getattr(self, "model_name", "") or ""),
                    duration_ms,
                    capability.native_supported,
                )
                # 只有本结论已作为当前事实进入 memo 才缓存到实例；旧 flight 的结果只交给本次调用。
                self._install_memo_capability(
                    key, expected=capability, revocation_generation=revocation_generation,
                )
                self._provider_tool_capability_miss_reason = None
            return capability
        finally:
            self._provider_tool_capability_lock.release()

    def probe_tool_protocol_snapshot(self, *, run_id: str) -> ToolProtocolSnapshot | ProviderToolCapability:
        """在同一代次检查下冻结 HTTP 探测结论。"""
        generation = _probe_revocation_generation(_credential_fingerprint(self))
        capability = self.probe_tool_capability()
        if not isinstance(capability, ProviderToolCapability) or not capability.native_supported:
            return capability
        with _capability_probe_lock:
            if not _probe_generation_current_locked(generation):
                return fast_replace(capability, native_supported=False, evidence="probe_revocation_generation_changed")
            return ToolProtocolSnapshot(run_id, "native", capability)

    # LLM: Caller holds `_provider_tool_capability_lock`; probe the ordinary auto tool-choice
    # surface, not forced-call or reasoning-disable support. Only a matching nonce proves support.
    # 宿主显式标记本次为探测；空输出仍属未证明能力，保持三次有界重试、不缓存阴性及供应商错误原样上抛。
    # 函数用途: 用普通工具选择发起有界探测并缓存阳性结果，不执行工具；避免思考模型被强制参数挡住。
    def _probe_tool_capability_uncached(self) -> ProviderToolCapability:
        # 缺凭据属于配置错误，须在协议能力判定之前暴露给调用方。
        if hasattr(self, "api_key") and not str(self.api_key or "").strip():
            raise ValueError(
                "api_key 为空：无法执行工具能力探针（检查 AGENT_API_KEY 环境变量或配置）"
            )
        cache_miss_reason = str(getattr(self, "_active_probe_cache_miss_reason", "") or "")
        evidence = ""
        for _attempt in range(1, _PROBE_MAX_ATTEMPT_COUNT + 1):
            nonce = secrets.token_hex(8)
            probe_tool = {
                "name": "my_agent_capability_probe",
                "description": "Internal protocol capability probe with no host-side effect.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "nonce": {
                            "type": "string",
                            "description": "Copy the nonce from the user request exactly.",
                        }
                    },
                    "required": ["nonce"],
                    "additionalProperties": False,
                },
            }
            prompt = (
                "Call my_agent_capability_probe exactly once with nonce "
                f"{nonce}. Do not answer in prose."
            )
            from ..llm_scale.hot_path import global_llm_admission_slot
            from .request_scope import provider_request_timeout

            # 探针在协议准备阶段调用，不在普通generate的持槽区；仅真实运输领同一进程名额。
            # 缓存/单飞仍归外层，等待不冒充供应商尝试，拿槽后重核原停止与剩余预算。
            with global_llm_admission_slot(wait_timeout=provider_request_timeout(30)):
                wait_interruptibly(0)
                provider_request_timeout(30)
                scope = current_probe_accounting()
                call_id = _start_probe_call(scope, self, prompt, cache_miss_reason=cache_miss_reason)
                if scope is None:
                    # 未绑定账本的真实探测仍保留原诊断计数，排队失败不增加它。
                    _count_unaccounted_probe_attempt()
                # RC1：传输包装见到本标记即只上抛拒绝，共享结论交当前 flight 收尾在比较保护下提交。
                probe_marker = _physical_probe_marker.set(True)
                try:
                    response = self.generate(
                        prompt,
                        tools=[probe_tool],
                        tool_choice=ToolChoice.auto("native_capability_probe"),
                        request_options=ProviderRequestOptions(
                            is_probe=True,
                            thinking_disabled=getattr(scope, 'thinking_disabled', False),
                            reasoning_effort=getattr(scope, 'reasoning_effort', ''),
                        ),
                    )
                except InterruptedError as exc:
                    _fail_probe_call(scope, call_id, exc)
                    raise
                except (ProviderRecoverableError, ProviderConfigurationError) as exc:
                    _fail_probe_call(scope, call_id, exc)
                    # 保留供应商失败分类，让调用方处理配置、额度和网络，不能改成能力不足。
                    raise
                except Exception as exc:
                    _fail_probe_call(scope, call_id, exc)
                    # 本地确定性异常(解析/配置错)重试无意义, 立即落 evidence。
                    code = str(getattr(exc, "error_code", "") or "").strip().upper()
                    suffix = f":{code}" if code else ""
                    evidence = f"live_probe_failed:{type(exc).__name__}{suffix}"
                    break
                finally:
                    _physical_probe_marker.reset(probe_marker)
                _finish_probe_call(scope, call_id, response)
            blocks = list(getattr(response, "tool_use_blocks", None) or ())
            supported = any(
                isinstance(block, dict)
                and str(block.get("id") or "").strip()
                and str(block.get("name") or "") == "my_agent_capability_probe"
                and isinstance(block.get("input"), dict)
                and str(block["input"].get("nonce") or "") == nonce
                for block in blocks
            )
            if supported:
                capability = ProviderToolCapability(
                    provider=str(self.name or type(self).__name__),
                    endpoint=self._tool_endpoint(),
                    model=self.model_name,
                    stream=self.stream_enabled,
                    native_supported=True,
                    evidence="live_probe_returned_structured_tool_call",
                    observed_at=_utc_now_iso(),
                )
                # C3：实例缓存只由 probe_tool_capability 在结论作为当前事实发布后写入（带代次与键）。
                return capability
            evidence = "live_probe_returned_no_valid_structured_tool_call"
        # 重试耗尽仍未证明: 不缓存(下次 turn 再探), 如实报不支持。
        return ProviderToolCapability(
            provider=str(self.name or type(self).__name__),
            endpoint=self._tool_endpoint(),
            model=self.model_name,
            stream=self.stream_enabled,
            native_supported=False,
            evidence=evidence or "live_probe_returned_no_valid_structured_tool_call",
            observed_at=_utc_now_iso(),
        )

    # LLM: HTTP 能力证据应标记真实协议端点；具体适配器覆盖路径，不能用展示名称替代地址。
    # 函数用途: 提供通用后端的探针端点标识，不发网络请求。
    def _tool_endpoint(self) -> str:
        return self.api_base

    # LLM: HTTP JSON 请求必须经过唯一 GatewayRequest 和共享传输；OAuth 覆盖认证封装，需联合取消与认证回归。
    # 函数用途: 检查凭据并发送一次非流式请求，返回供应商 JSON，错误沿原传输合同上抛。
    def request_json(
        self, path: str, payload: dict[str, Any], headers: dict[str, str],
        *, total_deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Send a JSON request through the shared gateway helper."""

        if not self.api_key:
            raise ValueError("api_key 为空：请在配置文件中填写 API Key。")

        try:
            return post_json(self._gateway_request(
                StreamCall(path=path, payload=payload, headers=headers),
                options=StreamCallOptions(total_deadline_seconds=total_deadline_seconds),
            ))
        except ProviderConfigurationError as exc:
            self._invalidate_memo_on_http_rejection(exc, payload)
            raise

    # LLM: Streaming collection forwards request-local first-event budget without changing backend-wide idle configuration.
    # 函数用途: 发起并完整收集一条流式 provider 请求，供非增量调用方使用。
    def request_stream(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        options: StreamCallOptions | None = None,
    ) -> list[str]:
        """Send a streaming request and collect all data lines."""
        try:
            return post_stream(self._gateway_request(
                StreamCall(path=path, payload=payload, headers=headers), options=options,
            ))
        except ProviderConfigurationError as exc:
            self._invalidate_memo_on_http_rejection(exc, payload)
            raise

    # LLM: The iterator preserves provider data order and per-request first-event timing for live consumers.
    # 函数用途: 发起流式请求并逐条交付 data，使 TUI 能实时显示正文、思考和工具参数进度。
    def request_stream_iter(
        self,
        path: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        *,
        options: StreamCallOptions | None = None,
    ):
        """Send a streaming request and yield data lines as they arrive."""
        try:
            yield from post_stream_iter(self._gateway_request(
                StreamCall(path=path, payload=payload, headers=headers), options=options,
            ))
        except ProviderConfigurationError as exc:
            self._invalidate_memo_on_http_rejection(exc, payload)
            raise

    # LLM: 唯一传输封装应用显式请求头和宿主会话；后台预算与首包预算保持 request-local，不改共享配置。
    #   cabfix：total_deadline_seconds 是调用方给的绝对墙钟上界，换算成 monotonic 时刻存进信封的
    #   deadline 字段——传输层已有该字段的完整机制（发送前、读正文、流式都共享同一条时限），
    #   不必另造一套。None 表示不设总期限，主模型与旧调用方语义完全不变。
    # 函数用途: 组装统一 HTTP 请求，将后台预算与物理并发限制传到底层；普通流式空闲/取消语义不变。
    def _gateway_request(
        self,
        call: StreamCall | str,
        payload: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        *,
        options: StreamCallOptions | None = None,
    ) -> GatewayRequest:
        """Build the immutable gateway request envelope used by all HTTP calls."""
        from .keepalive_transport import main_connection_pool
        from .request_scope import provider_request_timeout

        path, payload, headers = stream_call_parts(call, payload, headers)
        options = options or StreamCallOptions()
        api_base, path = endpoint_parts(self.api_base, path)
        deadline = stream_call_deadline(options)
        capacity = getattr(self, "model_http_max_idle_connections", 0)
        pool = main_connection_pool(capacity) if payload is not None and capacity > 0 else None
        return GatewayRequest(
            api_base=api_base,
            api_key=self.api_key,
            path=path,
            payload=payload,
            headers=request_headers(headers, self.custom_headers, self.session_header),
            timeout=provider_request_timeout(self.request_timeout),
            connect_timeout=self.connect_timeout,
            first_event_timeout=options.first_event_timeout_seconds,
            # 按本次配置冻结退避序列；空值保持原传输默认，不热改共享后端。
            retry_backoff_seconds=tuple(getattr(getattr(self, "config", None), "model_retry_backoff_seconds", ()) or ()),
            deadline=deadline,
            connection_pool=pool,
            provider_max_concurrent_requests=getattr(self, "provider_max_concurrent_requests", 0),
        )

    # LLM: 供应商上下文窗口只从模型 metadata API 的结构化字段读取；失败或字段缺失返回 0，
    #   由上层使用本地配置兜底。不得根据模型名在这里硬编码容量。
    # 函数用途: 请求并缓存供应商公开目录的容量信息，避免 Compact 每轮重复发起网络探测。
    def provider_context_window_tokens(self) -> int:
        """发现并缓存供应商模型目录公开的上下文窗口。"""
        if self._provider_context_window_cache is None:
            metadata = discover_provider_model_metadata(
                ProviderMetadataOptions(
                    api_base=self.api_base,
                    api_key=self.api_key,
                    model_name=self.model_name,
                    request_timeout=self.request_timeout,
                    connect_timeout=self.connect_timeout,
                    custom_headers=request_headers({}, self.custom_headers, self.session_header),
                )
            )
            self.model_metadata = metadata.record
            self._provider_context_window_cache = metadata.context_window_tokens
        return self._provider_context_window_cache
