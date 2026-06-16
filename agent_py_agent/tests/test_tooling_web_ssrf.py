"""web_fetch SSRF 内网防护(子项1)——工具边界集成测试。

钉死契约(在 WebFetchTool.execute 整条链路上,不只在 gate 单测):
1. file:// 及非 http(s) scheme 取数前被拒,绝不发起请求。
2. 云元数据端点(169.254.169.254 / metadata.google.internal)被拒。
3. 私网(10./172.16-31./192.168.)、loopback(127./::1)、link-local(169.254.)、
   0.0.0.0 解析后被拒(防 DNS 把公网域名解到内网)。
4. 正常外网(公网 IP)放行,链路照常走 urlopen。
5. DNS 重绑定:首查公网放行,复查解到私网时 NETWORK_DNS_REBINDING_BLOCKED。

标杆:长期助手 tools/url_safety.py / website_policy.py、通道运行时 ssrf-dispatcher / url-validation。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.tooling.web import WebFetchTool

pytestmark = pytest.mark.integration


def _resolver_to(*ips: str):
    def resolver(_host: str) -> tuple[str, ...]:
        return tuple(ips)
    return resolver


def _public_html_response() -> MagicMock:
    resp = MagicMock()
    resp.status = 200
    resp.headers = {"Content-Type": "text/html; charset=utf-8"}
    resp.read.return_value = b"<html><body><p>ok</p></body></html>"
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@patch("urllib.request.urlopen")
def test_file_scheme_blocked_before_request(mock_urlopen, tmp_path: Path) -> None:
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_resolver_to("8.8.8.8"), artifact_root=tmp_path)
    result = tool.execute({"url": "file:///etc/passwd"})
    assert result.ok is False
    # _normalize_url 先于 gate 卡住非 http(s) scheme → TOOL_INVALID_ARGUMENTS;
    # 任一拒绝码都可,关键是绝不发起请求。
    assert result.error_code in {"TOOL_INVALID_ARGUMENTS", "NETWORK_FILE_URL_BLOCKED"}
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
@pytest.mark.parametrize("scheme_url", ["ftp://internal/x", "gopher://x/", "dict://localhost:11211/"])
def test_non_http_schemes_blocked(mock_urlopen, scheme_url: str, tmp_path: Path) -> None:
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_resolver_to("8.8.8.8"), artifact_root=tmp_path)
    result = tool.execute({"url": scheme_url})
    assert result.ok is False
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_cloud_metadata_ip_blocked(mock_urlopen, tmp_path: Path) -> None:
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_resolver_to("169.254.169.254"), artifact_root=tmp_path)
    result = tool.execute({"url": "http://metadata.example.test/latest/meta-data/"})
    assert result.ok is False
    assert result.error_code in {"NETWORK_ALWAYS_BLOCKED_IP", "NETWORK_PRIVATE_IP_BLOCKED"}
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_metadata_google_internal_host_blocked(mock_urlopen, tmp_path: Path) -> None:
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_resolver_to("8.8.8.8"), artifact_root=tmp_path)
    result = tool.execute({"url": "http://metadata.google.internal/computeMetadata/v1/"})
    assert result.ok is False
    assert result.error_code == "NETWORK_ALWAYS_BLOCKED_HOST"
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
@pytest.mark.parametrize(
    "private_ip",
    ["10.0.0.5", "172.16.0.1", "172.31.255.254", "192.168.1.1", "127.0.0.1", "0.0.0.0", "::1"],
)
def test_private_and_loopback_ranges_blocked_after_dns(mock_urlopen, private_ip: str, tmp_path: Path) -> None:
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_resolver_to(private_ip), artifact_root=tmp_path)
    result = tool.execute({"url": "https://looks-public.example.test/data"})
    assert result.ok is False, f"{private_ip} 应被拦"
    assert result.error_code in {"NETWORK_PRIVATE_IP_BLOCKED", "NETWORK_ALWAYS_BLOCKED_IP"}
    mock_urlopen.assert_not_called()


@patch("urllib.request.urlopen")
def test_public_url_is_allowed(mock_urlopen, tmp_path: Path) -> None:
    mock_urlopen.return_value = _public_html_response()
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=_resolver_to("93.184.216.34"), artifact_root=tmp_path)
    result = tool.execute({"url": "https://example.com/page", "format": "text"})
    assert result.ok is True
    mock_urlopen.assert_called_once()


@patch("urllib.request.urlopen")
def test_gate_re_resolves_every_call_so_later_private_answer_is_blocked(mock_urlopen, tmp_path: Path) -> None:
    # 工具边界保证:每次 execute 都重新解析并校验,DNS 之后翻转到私网的请求会被拦
    #   (取数前必有一次私网拦截)。跨调用的 previous_resolved_ips 重绑定专项判定在
    #   test_network_safety_gate 的 gate 单测里覆盖。
    answers = [("93.184.216.34",), ("127.0.0.1",)]

    def resolver(_host: str) -> tuple[str, ...]:
        return answers.pop(0) if answers else ("127.0.0.1",)

    mock_urlopen.return_value = _public_html_response()
    tool = WebFetchTool(max_chars=10000, timeout=10, resolver=resolver, artifact_root=tmp_path)
    first = tool.execute({"url": "https://flip.example.test/x", "method": "POST", "body": "{}"})
    assert first.ok is True  # 首次解到公网,放行
    second = tool.execute({"url": "https://flip.example.test/x", "method": "POST", "body": "{}"})
    assert second.ok is False, "DNS 翻转到私网后,重新解析必拦"
    assert second.error_code == "NETWORK_PRIVATE_IP_BLOCKED"
