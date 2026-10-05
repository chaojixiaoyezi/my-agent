"""供应商拒绝诊断（rejectdiag）：4xx/5xx 拒绝的结构化诊断与回执投影。

覆盖：403 空响应体、429 带 retry-after、401 带 www-authenticate、敏感头不落盘、正文摘要去控制字符。
"""
import io
import json
import urllib.error
from email.message import Message

from agent_py_agent.agent.backends.errors import (
    ProviderTransientError,
    is_provider_recoverable_error,
)
from agent_py_agent.agent.backends.gateway_helpers import _runtime_http_error
from agent_py_agent.agent.backends.rejection_diagnostics import public_rejection_diagnostic
from agent_py_agent.agent.gateway_parts.request_errors import (
    gateway_provider_error_projection,
    provider_rejection_user_notice,
)


# LLM: 测试辅助：构造带结构化头与正文的 HTTPError，模拟供应商 4xx/5xx 响应。
# 函数用途: 给拒绝诊断用例提供统一的 HTTPError 输入。
def _http_error(status, body=b"", headers=None):
    message = Message()
    for name, value in (headers or {}).items():
        message[name] = value
    return urllib.error.HTTPError(
        "https://provider.example.test/v1/messages", status, "rejected", message, io.BytesIO(body))


# LLM: 403 空响应体是 17k 现场的真实形状：诊断必须给出“拒绝但无更多线索”的原因码，而不是空。
# 函数用途: 钉住 403 空体的结构化诊断与回执投影。
def test_403_empty_body_records_forbidden_unknown_and_projects_cause():
    error = _runtime_http_error(_http_error(403, b""))
    diagnostic = error.details["rejection_diagnostic"]
    assert diagnostic["status_code"] == 403
    assert diagnostic["cause_code"] == "forbidden_unknown"
    assert diagnostic["body_bytes"] == 0 and diagnostic["body_excerpt"] == ""
    projection = gateway_provider_error_projection(error)
    assert projection["http_status"] == 403
    assert projection["cause_code"] == "forbidden_unknown"
    assert projection["rejection_diagnostic"]["status_code"] == 403


# LLM: 429 是瞬时族（ProviderUsageLimitError），投影只追加诊断与原因码，不改原有错误文案与恢复语义。
# 函数用途: 钉住 429 带 retry-after 的诊断与投影。
def test_429_retry_after_is_recorded_and_projected():
    error = _runtime_http_error(_http_error(
        429, b'{"error": "rate"}', {"retry-after": "30", "x-ratelimit-remaining": "0"}))
    diagnostic = error.details["rejection_diagnostic"]
    assert diagnostic["cause_code"] == "rate_limited"
    assert diagnostic["headers"]["retry-after"] == "30"
    assert diagnostic["headers"]["x-ratelimit-remaining"] == "0"
    projection = gateway_provider_error_projection(error)
    assert projection["cause_code"] == "rate_limited"
    assert projection["rejection_diagnostic"]["headers"]["retry-after"] == "30"


# LLM: www-authenticate 只留方案名；realm/error 参数可能含细节，不进入诊断。
# 函数用途: 钉住 401 的诊断原因与鉴权方案裁剪。
def test_401_www_authenticate_keeps_only_the_scheme():
    error = _runtime_http_error(_http_error(
        401, b"", {"www-authenticate": 'Bearer realm="x", error="invalid_token"'}))
    diagnostic = error.details["rejection_diagnostic"]
    assert diagnostic["cause_code"] == "auth"
    assert diagnostic["headers"]["www-authenticate"] == "Bearer"


# LLM: 认证/会话类头绝不进诊断（任务硬约束）；只保留白名单内的结构化头。
# 函数用途: 钉住敏感响应头不落盘、白名单头保留。
def test_sensitive_headers_never_land_in_diagnostic():
    error = _runtime_http_error(_http_error(403, b"", {
        "authorization": "Bearer secret-token", "set-cookie": "session=abc",
        "cookie": "sid=1", "x-request-id": "req-1"}))
    diagnostic = error.details["rejection_diagnostic"]
    assert diagnostic["headers"] == {"x-request-id": "req-1"}
    rendered = json.dumps(diagnostic, ensure_ascii=False)
    assert "secret-token" not in rendered and "session=abc" not in rendered


# LLM: 正文摘要必须去控制字符并截断；坏字节经 replace 解码后仍安全。
# 函数用途: 钉住 body_excerpt 的安全边界与 body_bytes 的事实口径。
def test_body_excerpt_strips_control_chars_and_truncates():
    error = _runtime_http_error(_http_error(403, ("bad\x00news\n" * 100).encode()))
    diagnostic = error.details["rejection_diagnostic"]
    assert "\x00" not in diagnostic["body_excerpt"] and "\n" not in diagnostic["body_excerpt"]
    assert len(diagnostic["body_excerpt"]) <= 200
    assert diagnostic["body_bytes"] > 200


# LLM: 清洗层是投影/持久化共用的安全闸：坏状态码整体拒绝，白名单外的头与非法原因码被丢弃。
# 函数用途: 钉住 public_rejection_diagnostic 的输入清洗。
def test_public_rejection_diagnostic_cleans_inputs():
    assert public_rejection_diagnostic(None) == {}
    assert public_rejection_diagnostic({"status_code": "403"}) == {}
    cleaned = public_rejection_diagnostic({
        "status_code": 403, "cause_code": "made_up", "body_excerpt": "x\x00y",
        "headers": {"authorization": "Bearer x", "cf-ray": "r1"}, "body_bytes": -5})
    assert "cause_code" not in cleaned
    assert cleaned["headers"] == {"cf-ray": "r1"}
    assert cleaned["body_bytes"] == 0 and "\x00" not in cleaned["body_excerpt"]


# LLM: 初审补钉（V1 缺口）：原因码只由状态码判定，不解析响应体语言；正文里出现 auth/rate/quota
#   字样不得改变分类（此前无用例钉住，变异“按正文改判 auth”会存活）。
# 函数用途: 钉住 cause_code 只看结构化状态码、不被正文文本带偏。
def test_cause_code_follows_status_code_not_body_text():
    body = b'{"error": "auth rate limited quota forbidden"}'
    for status, expected in ((403, "forbidden_unknown"), (429, "rate_limited"),
                             (401, "auth"), (402, "quota")):
        error = _runtime_http_error(_http_error(status, body))
        assert error.details["rejection_diagnostic"]["cause_code"] == expected


# LLM: 初审补钉（V4 缺口）：瞬时族投影只追加结构化诊断与原因码，不覆盖/不添加 user_error，
#   可恢复语义不变（此前无用例钉住，变异“投影附带 user_error”会存活）。
# 函数用途: 钉住 429 投影的键集合与可恢复判定，防止诊断改动原有错误文案。
def test_transient_projection_appends_only_diagnostic_without_user_error():
    error = _runtime_http_error(_http_error(429, b'{"error": "rate"}', {"retry-after": "30"}))
    assert isinstance(error, ProviderTransientError)
    projection = gateway_provider_error_projection(error)
    assert set(projection) == {"rejection_diagnostic", "cause_code"}
    assert "user_error" not in projection
    assert is_provider_recoverable_error(error) is True


# LLM: rejectdiag2 必须改项：服务端错误正文可能回显请求凭据（sk-/Bearer/cookie），摘要必须走统一凭据清洗。
# 函数用途: 钉住 sk-/Bearer 回显在诊断序列化里只剩占位符。
def test_body_excerpt_redacts_credential_shapes_from_provider_echo():
    body = (b'{"error": "invalid api key sk-live-ABC1234567890; '
            b'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdef"}')
    error = _runtime_http_error(_http_error(403, body))
    diagnostic = error.details["rejection_diagnostic"]
    rendered = json.dumps(diagnostic, ensure_ascii=False)
    assert "sk-live-ABC1234567890" not in rendered
    assert "eyJhbGciOiJIUzI1NiJ9" not in rendered
    assert "[REDACTED]" in diagnostic["body_excerpt"]


# LLM: rejectdiag2：cookie 也是 HTTP 凭据头（词表补入）；服务端回显 Cookie/set-cookie 时不能漏遮。
# 函数用途: 钉住 cookie 样式回显在诊断里被清洗。
def test_body_excerpt_redacts_cookie_style_echo():
    error = _runtime_http_error(_http_error(
        403, b'{"error": "bad cookie", "cookie": "session=abc123456"}'))
    diagnostic = error.details["rejection_diagnostic"]
    rendered = json.dumps(diagnostic, ensure_ascii=False)
    assert "session=abc123456" not in rendered
    assert "[REDACTED]" in diagnostic["body_excerpt"]


# LLM: rejectdiag2：x-ratelimit-* 可能一次回很多个；条数与总量双上限必须截断并记结构化标记，
#   且固定名单（请求编号类）优先保留，不被限流头挤掉。
# 函数用途: 钉住头截断行为、headers_truncated 标记与固定名单优先。
def test_rejection_headers_cap_count_and_total_chars_with_marker():
    headers = {f"x-ratelimit-{index}": "1" * 60 for index in range(200)}
    headers["x-request-id"] = "req-123"
    error = _runtime_http_error(_http_error(429, b"", headers))
    diagnostic = error.details["rejection_diagnostic"]
    assert diagnostic["headers_truncated"] is True
    assert len(diagnostic["headers"]) <= 16
    assert diagnostic["headers"]["x-request-id"] == "req-123"
    assert len(json.dumps(diagnostic, ensure_ascii=False)) < 2000


# LLM: rejectdiag2：正常少量白名单头不应触发截断标记，避免客户端把正常诊断当被裁剪的。
# 函数用途: 钉住 headers_truncated 在未超限时为 False。
def test_rejection_headers_small_set_not_truncated():
    error = _runtime_http_error(_http_error(403, b"", {"x-request-id": "req-1"}))
    diagnostic = error.details["rejection_diagnostic"]
    assert diagnostic["headers_truncated"] is False
    assert diagnostic["headers"]["x-request-id"] == "req-1"


# LLM: rejectdiag2 展示出口：失败提示（user_error）要带原因码与请求编号，且绝不带未清洗凭据；
#   摘要只在清洗后出现（服务端说明来自已清洗的 body_excerpt）。
# 函数用途: 钉住 provider_rejection_user_notice 的展示内容与安全边界。
def test_user_notice_adds_cause_and_request_id_without_credentials():
    error = _runtime_http_error(_http_error(
        403, b'{"error": "invalid api key sk-live-ABC1234567890"}',
        {"x-request-id": "req-9", "retry-after": "30"}))
    projection = gateway_provider_error_projection(error)
    notice = provider_rejection_user_notice("模型请求 HTTP 403：模型服务拒绝了本次请求。", projection)
    assert "原因码：forbidden_unknown" in notice
    assert "请求编号：x-request-id=req-9" in notice
    assert "retry-after=30" in notice
    assert "sk-live-ABC1234567890" not in notice
    assert "[REDACTED]" in notice


# LLM: rejectdiag2：瞬时族展示同样带原因码与 retry-after，但投影键集合保持 {rejection_diagnostic, cause_code}
#   （与 rejectdiag 合同一致，不新增 user_error 键）。
# 函数用途: 钉住瞬时族的展示追加与投影键集合不漂移。
def test_user_notice_transient_keeps_projection_keys_and_adds_cause():
    error = _runtime_http_error(_http_error(429, b'{"error": "rate"}', {"retry-after": "30"}))
    projection = gateway_provider_error_projection(error)
    assert set(projection) == {"rejection_diagnostic", "cause_code"}
    notice = provider_rejection_user_notice("模型服务暂时不可用，请稍后重试。", projection)
    assert "原因码：rate_limited" in notice
    assert "retry-after=30" in notice


# LLM: rejectdiag2：没有拒绝诊断的失败提示必须原样返回，不能凭空追加括号或空字段。
# 函数用途: 钉住无诊断输入的恒等行为。
def test_user_notice_without_diagnostic_is_unchanged():
    assert provider_rejection_user_notice("普通失败", {}) == "普通失败"
    assert provider_rejection_user_notice("普通失败", None) == "普通失败"
    assert provider_rejection_user_notice("普通失败", {"cause_code": "x"}) == "普通失败"
