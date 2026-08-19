from __future__ import annotations

"""NET-01 回归：系统级拒绝连接可退避，DNS/配置连接失败仍快速报错。

2026-08-15 C3 真机：api_base 指向死端口时 CLI 打印完整 Python traceback（RC=1）。
2026-08-18 长任务：远端 MiniMax 在 127 个工具轮后短暂拒绝连接，旧分类没有进入任何退避。
"""

import errno
import socket
import urllib.error

from agent_py_agent.agent.backends.errors import (
    ProviderConnectionError,
    ProviderRecoverableError,
    ProviderTransientError,
)
from agent_py_agent.agent.backends.gateway_helpers import (
    GatewayRequest,
    _runtime_network_error,
)


def _request() -> GatewayRequest:
    return GatewayRequest(
        api_base="http://127.0.0.1:9",
        api_key="test",
        path="/v1/messages",
        payload={},
        headers={},
        timeout=10,
    )


# LLM: typed ECONNREFUSED 是远端端点的瞬时供应事实，必须进入现有 ProviderTransientError 双层退避。
# 函数用途: 验证 urllib 包装后的系统级拒绝连接可重试，避免长任务因一次供应抖动直接终止。
def test_connection_refused_errno_classified_as_provider_transient_error():
    exc = urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused"))
    err = _runtime_network_error(exc, _request())
    assert isinstance(err, ProviderTransientError)
    assert isinstance(err, ProviderRecoverableError)
    assert "拒绝连接" in str(err)


# LLM: 错误文案没有机器权威；缺少 errno/type 的同名字符串不得扩大重试面。
# 函数用途: 验证仅写着 connection refused 的普通字符串仍按未知连接配置错误处理。
def test_connection_refused_text_without_errno_is_not_machine_retry_fact():
    err = _runtime_network_error(urllib.error.URLError("Connection refused"), _request())
    assert isinstance(err, ProviderConnectionError)
    assert not isinstance(err, ProviderRecoverableError)


# LLM: DNS 解析失败不是 ECONNREFUSED，必须保持快速失败，避免错误 api_base 消耗整套长退避。
# 函数用途: 验证无效域名继续提示用户检查 DNS/配置。
def test_dns_failure_stays_provider_connection_error():
    exc = urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "Name or service not known"))
    err = _runtime_network_error(exc, _request())
    assert isinstance(err, ProviderConnectionError)
    assert not isinstance(err, ProviderRecoverableError)


# LLM: 瞬时网络错误（连接重置）仍归 ProviderTransientError，重试语义不变。
# 函数用途: 验证分类边界：可重试网络错误不被误归连接错误。
def test_transient_network_error_stays_transient():
    exc = urllib.error.URLError("Connection reset by peer")
    err = _runtime_network_error(exc, _request())
    from agent_py_agent.agent.backends.errors import ProviderTransientError

    assert isinstance(err, ProviderTransientError)


# LLM: 超时仍归 ProviderTimeoutError。
# 函数用途: 验证超时分类不因本次改动回退。
def test_timeout_stays_timeout():
    err = _runtime_network_error(TimeoutError("timed out"), _request())
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    assert isinstance(err, ProviderTimeoutError)
