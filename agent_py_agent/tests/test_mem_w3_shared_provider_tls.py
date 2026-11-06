from __future__ import annotations

# LLM: MEM-W3 让模型 HTTPS 连接共用一份进程级默认 TLS 上下文；校验语义必须与标准库 context=None 时自建的完全相同。
# 模块用途: 回归保护"每次模型调用新建 SSLContext"（实测每份约 0.73 MB、约 20 ms CPU）不再出现，且证书/主机名校验不放松。
import http.client
import ssl
import threading

from agent_py_agent.agent.backends import gateway_helpers, keepalive_transport

_COMPARED_SETTINGS = ("verify_mode", "check_hostname", "minimum_version", "maximum_version", "options",
                      "verify_flags", "protocol", "post_handshake_auth")


# 函数用途: 构造一份与产品相同形状的双超时选项（不发起连接）。
def _options():
    return gateway_helpers._SplitTimeoutOptions(5.0, 5.0, None, None, None)


# LLM: 非池路径每次请求都会新建 handler；两次构造必须拿到同一个上下文对象，且就是 _tls_context() 的返回值。
# 函数用途: 确认 HTTPS handler 不再各自重建 TLS 上下文。
def test_https_handlers_share_one_process_tls_context():
    first = gateway_helpers._SplitTimeoutHTTPSHandler(_options())
    second = gateway_helpers._SplitTimeoutHTTPSHandler(_options())
    assert isinstance(first._context, ssl.SSLContext)
    assert first._context is second._context is keepalive_transport._tls_context()


# LLM: 共享上下文的每一项校验设置都要与标准库默认 HTTPS 客户端上下文相同，CA 存储规模也相同。
# 函数用途: 核对共享上下文没有放松证书校验、主机名校验或协议下限。
def test_shared_tls_context_matches_stdlib_default_https_context():
    shared = keepalive_transport._tls_context()
    factory = getattr(http.client, "_create_https_context", None)
    reference = factory(http.client.HTTPSConnection._http_vsn) if callable(factory) else ssl.create_default_context()
    for name in _COMPARED_SETTINGS:
        assert getattr(shared, name) == getattr(reference, name), name
    assert shared.verify_mode == ssl.CERT_REQUIRED and shared.check_hostname is True
    assert shared.cert_store_stats() == reference.cert_store_stats()


# 函数用途: 确认多线程并发首次取用也只得到一份上下文。
def test_concurrent_first_use_yields_single_context():
    seen: set[int] = set()
    lock = threading.Lock()

    def grab():
        context = keepalive_transport._tls_context()
        with lock:
            seen.add(id(context))

    threads = [threading.Thread(target=grab) for _ in range(32)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(seen) == 1


# LLM: 长连接池新建 HTTPS 连接时同样用共享上下文；测试替换 _tls_context 的既有方式继续生效。
# 函数用途: 核对 _new_connection 把共享上下文交给连接对象，且模块属性替换仍被尊重。
def test_keepalive_new_https_connection_uses_shared_context(monkeypatch):
    route = keepalive_transport.KeepAliveRoute(scheme="https", host="example.invalid", port=443)
    connection = keepalive_transport._new_connection(route, _options())
    try:
        assert connection._context is keepalive_transport._tls_context()
    finally:
        connection.close()
    marker = ssl.create_default_context()
    monkeypatch.setattr(keepalive_transport, "_tls_context", lambda: marker)
    assert gateway_helpers._SplitTimeoutHTTPSHandler(_options())._context is marker
