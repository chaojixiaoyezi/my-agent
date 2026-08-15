from __future__ import annotations

"""NET-01 回归：连接失败归 ProviderConnectionError（不可重试配置性失败），CLI 不打印 traceback。

2026-08-15 C3 真机：api_base 指向死端口时 CLI 打印完整 Python traceback（RC=1）。
修复后应抛 typed ProviderConnectionError，由 CLI 渲染稳定中文提示。
"""

import urllib.error

from agent_py_agent.agent.backends.errors import (
    ProviderConnectionError,
    ProviderRecoverableError,
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


# LLM: Connection refused 是不可重试配置性失败，必须归 ProviderConnectionError
#（而非 ProviderRecoverableError，否则重试层会误重试）。
# 函数用途: 验证连接拒绝被分类为 ProviderConnectionError 且不继承可重试基类。
def test_connection_refused_classified_as_provider_connection_error():
    exc = urllib.error.URLError("Connection refused")
    err = _runtime_network_error(exc, _request())
    assert isinstance(err, ProviderConnectionError)
    assert not isinstance(err, ProviderRecoverableError)
    assert "无法连接" in str(err)


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
    import socket

    err = _runtime_network_error(socket.timeout("timed out"), _request())
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError

    assert isinstance(err, ProviderTimeoutError)
