# LLM: AGENTS URL 的可信读取边界；复用 HTTP 主链，逐跳 owner 权限、公网全部 DNS 和固定 IP，不扫描正文或写 last-good。
# 模块用途: 安全读取 HTTPS 来源，并返回供缓存逐跳重新授权的不可变原字节结果。
from __future__ import annotations

import math
import queue
import socket
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from urllib.parse import urlsplit

from ..contracts.gates.network_safety import (
    _canonical_ip,
    ip_address_or_none,
    ipv4_projected,
)
from ..tooling.models import ToolHandlerOutcome
from ..tooling.web_fetch_runtime import (
    FetchRawRequest,
    PinResult,
    RawResponseParts,
    fetch_raw_response,
)
from ._agents_contract_fields import AgentsContractError
from .agents_source_contract import _validate_url_locator

# 每次来源加载的绝对网络截止时间；ContextVar隔离并发调用，不保存owner、正文或授权。
_request_deadline = ContextVar("agents_source_deadline", default=None)


# LLM: 嵌套校验继承较早截止时间，退出复原；reserve只提前结束子阶段，不可延长十五秒总期限。
# 函数用途: 让DNS、redirect和缓存复核共享有界期限，可为失败后的安全复核预留时间。
@contextmanager
def source_request_budget(*, reserve_seconds=0):
    if type(reserve_seconds) not in (int, float) or not 0 <= reserve_seconds < 15:
        raise AgentsContractError("AGENTS_SOURCE_BUDGET_INVALID", "response")
    deadline = time.monotonic() + 15
    previous = _request_deadline.get()
    deadline = min(deadline, previous) if previous is not None else deadline
    token = _request_deadline.set(deadline - reserve_seconds)
    try:
        yield
    finally:
        _request_deadline.reset(token)


# LLM: 返回对象只说明本次读取事实，checked_urls 不是长期权限凭据；缓存消费前须逐项 validate_agents_url。
# 类用途: 交接未经换行或替换解码的原正文，以及实际检查过的全部跳转地址。
@dataclass(frozen=True)
class AgentsURLResponse:
    body: bytes
    checked_urls: tuple[str, ...]


# LLM: 每跳先检查权限再解析；公网专用策略不读取通用私网例外，连接层只收到本次解析的固定 IP。
# 函数用途: 校验一条缓存 URL 当前仍被允许，不发起 HTTP 连接、不修改来源或缓存。
def validate_agents_url(url: str, *, url_allowed: Callable[[str], bool], resolver=None) -> None:
    deadline = _request_deadline.get()
    _resolve_agents_pin(url, url_allowed, resolver, deadline if deadline is not None else time.monotonic() + 15)


# LLM: 请求预算不可放宽来源硬帽；显式timeout/max_bytes成组传递，仍由抓取入口逐次验证精确类型和范围。
# 类用途: 表达一次HTTPS来源抓取的有限时间与字节预算。
@dataclass(frozen=True)
class AgentsFetchBudget:
    timeout: float = 15
    max_bytes: int = 80000


# LLM: 整次抓取共享 deadline 和 80KB 硬上限，沿用现有HTTP代理策略；任何失败只向调用方暴露固定码与字段。
# 函数用途: 读取 HTTPS AGENTS 正文并严格核对 UTF-8，正文扫描和 last-good 仍由上层负责。
def load_agents_url(url: str, *, url_allowed: Callable[[str], bool], resolver=None,
                    budget: AgentsFetchBudget | None = None) -> AgentsURLResponse:
    budget = AgentsFetchBudget() if budget is None else budget
    if type(budget) is not AgentsFetchBudget:
        raise AgentsContractError("AGENTS_SOURCE_BUDGET_INVALID", "response")
    timeout, max_bytes = budget.timeout, budget.max_bytes
    _validate_budget(timeout, max_bytes)
    checked: list[str] = []
    deadline = time.monotonic() + timeout
    inherited = _request_deadline.get()
    if inherited is not None:
        deadline = min(deadline, inherited)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AgentsContractError("AGENTS_SOURCE_TIMEOUT", "response")
    # LLM: 传输器每跳调用一次；只记录完成安全检查的地址，不重复 DNS 或把失败地址作为成功事实。
    # 函数用途: 将 owner URL 校验与当前抓取的逐跳原文记录接入现有 HTTP 主链。
    def resolve_pin(target: str) -> PinResult:
        ip = _resolve_agents_pin(target, url_allowed, resolver, deadline)
        checked.append(target)
        return PinResult(ip, None)
    response = fetch_raw_response(
        FetchRawRequest("agents_source", url, "GET", {"User-Agent": "MyAgent-AgentsSource/1.0"},
                        None, remaining, max_bytes),
        resolve_pin=resolve_pin, format_http_error=_http_error,
    )
    if isinstance(response, ToolHandlerOutcome):
        _raise_transport_error(response.reported_error_code or response.error_code)
    if not isinstance(response, RawResponseParts) or not 200 <= response.status < 300:
        raise AgentsContractError("AGENTS_SOURCE_HTTP_FAILED", "response")
    if time.monotonic() >= deadline:
        raise AgentsContractError("AGENTS_SOURCE_TIMEOUT", "response")
    try:
        response.body.decode("utf-8", "strict")
    except UnicodeError:
        raise AgentsContractError("AGENTS_SOURCE_UTF8_INVALID", "response") from None
    return AgentsURLResponse(response.body, tuple(checked))


# LLM: 权限回调异常也失败关闭；词法验证复用冻结合同，但不让它替代实际 DNS 与权限检查。
# 函数用途: 严格校验 URL，单次解析全部地址，返回可固定连接的首个公网地址。
def _resolve_agents_pin(url, url_allowed, resolver, deadline=None) -> str:
    try:
        allowed = url_allowed(url) is True
    except Exception:
        allowed = False
    if not allowed:
        raise AgentsContractError("AGENTS_SOURCE_URL_DENIED", "locator")
    if type(url) is not str:
        raise AgentsContractError("AGENTS_SOURCE_LOCATOR_INVALID", "locator")
    _validate_url_locator(url)
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port == 0 or "%" in host:
        raise AgentsContractError("AGENTS_SOURCE_LOCATOR_INVALID", "locator")
    literal = _canonical_ip(host)
    if literal and not _public_address(literal):
        raise AgentsContractError("AGENTS_SOURCE_NETWORK_BLOCKED", "locator")
    answers = _dns_answers(host, resolver, deadline)
    if not answers:
        raise AgentsContractError("AGENTS_SOURCE_DNS_FAILED", "locator")
    if any(not _public_address(address) for address in answers):
        raise AgentsContractError("AGENTS_SOURCE_NETWORK_BLOCKED", "locator")
    return str(ip_address_or_none(answers[0]))


# LLM: IPv6 内嵌 IPv4 也须公网；is_global 单独会放行 multicast，不能作为唯一条件。
# 函数用途: 拒绝私网、保留、共享、回环、链路、本机和内嵌非公网地址，无授权例外。
def _public_address(value: str) -> bool:
    address = ip_address_or_none(value)
    if address is None or "%" in value:
        return False
    projected = ipv4_projected(address) or address
    return all(candidate.is_global and not (candidate.is_multicast or candidate.is_reserved
               or candidate.is_loopback or candidate.is_link_local or candidate.is_unspecified)
               for candidate in (address, projected))


# LLM: 一跳只调用一次 resolver；daemon 线程仅隔离阻塞 DNS，超时后的结果永不进入连接或 checked_urls。
# 函数用途: 在剩余总期限内取得完整 DNS 回答，并把异常化为不含来源的固定失败码。
def _dns_answers(host, resolver, deadline) -> tuple[str, ...]:
    # LLM: 系统 getaddrinfo 一次返回全部地址；用户注入 resolver 使用相同的全量检查语义。
    # 函数用途: 获取一跳的原始地址列表，不在连接时再解析目标域名。
    def resolve():
        if resolver is not None:
            return tuple(str(address) for address in resolver(host))
        return tuple(str(row[4][0]) for row in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    try:
        if deadline is None:
            return resolve()
        if deadline <= time.monotonic():
            raise AgentsContractError("AGENTS_SOURCE_TIMEOUT", "locator")
        results = queue.Queue(maxsize=1)
        # LLM: 不写缓存或共享状态；阻塞解析迟到时只能落到这个请求私有的队列。
        # 函数用途: 让调用线程可在绝对截止时间返回，不受系统 DNS 阻塞拖延。
        def worker():
            try:
                results.put((True, resolve()))
            except Exception:
                results.put((False, ()))
        threading.Thread(target=worker, daemon=True).start()
        ok, answers = results.get(timeout=max(0.0, deadline - time.monotonic()))
        if ok:
            return answers
    except AgentsContractError:
        raise
    except queue.Empty:
        raise AgentsContractError("AGENTS_SOURCE_TIMEOUT", "locator") from None
    except Exception:
        pass
    raise AgentsContractError("AGENTS_SOURCE_DNS_FAILED", "locator")


# LLM: 不接受 NaN/Inf/bool 或无限字节预算；上层无法把固定来源上限放宽到任意值。
# 函数用途: 在请求前检查总期限与响应上限的合法范围。
def _validate_budget(timeout, max_bytes) -> None:
    try:
        valid_timeout = type(timeout) in (int, float) and math.isfinite(timeout) and timeout > 0
    except OverflowError:
        valid_timeout = False
    if not valid_timeout or type(max_bytes) is not int or not 0 < max_bytes <= 80000:
        raise AgentsContractError("AGENTS_SOURCE_BUDGET_INVALID", "response")


# LLM: HTTP 错误正文和 URL 不进入返回错误；复用传输回调但不使用面向网页预览的 formatter。
# 函数用途: 将 HTTP 错误转为只含固定码的失败结果。
def _http_error(tool, error, preview_chars) -> ToolHandlerOutcome:
    return ToolHandlerOutcome(tool, False, "AGENTS_SOURCE_HTTP_FAILED", error_code="NETWORK_REQUEST_FAILED",
                              reported_error_code="AGENTS_SOURCE_HTTP_FAILED")


# LLM: 只按结构化传输码转换；非法URL是安全失败，不得映射为可用last-good的技术失败。
# 函数用途: 把传输器失败映射为来源加载器的稳定错误合同。
def _raise_transport_error(code) -> None:
    failures = {"ARTIFACT_TOO_LARGE": "AGENTS_SOURCE_TOO_LARGE",
                "TOOL_TIMEOUT": "AGENTS_SOURCE_TIMEOUT",
                "HTTP_URL_INVALID": "AGENTS_SOURCE_LOCATOR_INVALID",
                "SYSTEM_PROXY_UNSUPPORTED": "AGENTS_SOURCE_PROXY_UNSUPPORTED",
                "AGENTS_SOURCE_HTTP_FAILED": "AGENTS_SOURCE_HTTP_FAILED"}
    raise AgentsContractError(failures.get(code, "AGENTS_SOURCE_FETCH_FAILED"), "response")
