from __future__ import annotations

"""LLM: implements outbound HTTP tools behind explicit timeout and output-size limits.

给人看的解释：
这个文件只负责访问网络。
`fetch_url` 偏向“简单打开一个网页”，`http_request` 偏向“调接口、带 header、带 body”。
这里统一限制超时时间和返回长度，避免一次请求把主流程卡死或把 prompt 撑爆。
"""

import json
import urllib.error
import urllib.request
from typing import Any

from .models import BaseTool, ToolExecutionResult, ToolSpec

class FetchUrlTool(BaseTool):
    """抓取网页或纯文本接口内容。"""

    def __init__(self, *, max_chars: int, timeout: int):
        self.max_chars = max_chars
        self.timeout = timeout
        self.spec = ToolSpec(
            name="fetch_url",
            category="web",
            description="抓取网页或文本接口内容，适合查在线文档、网页说明和纯文本页面。",
            use_cases=[
                "查看在线文档、普通网页正文或文本接口响应",
                "快速确认某个 URL 是否能访问、返回了什么文本",
            ],
            avoid_when=[
                "需要带复杂请求头、请求体或切换 HTTP 方法时，优先用 http_request",
            ],
            keywords=["网页", "抓网页", "文档", "URL", "fetch", "GET", "在线说明"],
            parameters={
                "url": "完整 URL",
            },
            parameter_details={
                "url": "必填，传入完整的 http 或 https 地址；工具内部固定按 GET 请求处理。",
            },
            examples=[
                '{"tool": "fetch_url", "url": "https://example.com/docs"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        url = str(params.get("url", "")).strip()
        if not url:
            return ToolExecutionResult("fetch_url", False, "缺少必填参数 url")

        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": "SimplePythonAgent/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                result = (
                    f"status={resp.status}\n"
                    f"content_type={resp.headers.get('Content-Type', '')}\n\n"
                    f"{body[: self.max_chars]}"
                )
                if len(body) > self.max_chars:
                    result += "\n... 已截断"
                return ToolExecutionResult("fetch_url", True, result)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            return ToolExecutionResult("fetch_url", False, f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            return ToolExecutionResult("fetch_url", False, f"请求失败: {exc}")


class HttpRequestTool(BaseTool):
    """通用 HTTP / API 调试工具。"""

    def __init__(self, *, max_chars: int, timeout: int):
        self.max_chars = max_chars
        self.timeout = timeout
        self.spec = ToolSpec(
            name="http_request",
            category="api",
            description="发送通用 HTTP 请求，适合调 REST API、Webhook 和普通接口。",
            use_cases=[
                "测试 GET/POST/PUT/DELETE 等接口返回是否正常",
                "带请求头、请求体去联调 API",
            ],
            avoid_when=[
                "只是想看一个普通网页正文时，fetch_url 更简单",
            ],
            keywords=["API", "接口", "HTTP", "POST", "GET", "Webhook", "请求头", "请求体"],
            parameters={
                "url": "完整 URL",
                "method": "HTTP 方法，默认 GET",
                "headers": "可选请求头",
                "body": "可选请求体",
            },
            parameter_details={
                "url": "必填，接口完整地址。",
                "method": "可选，支持 GET/POST/PUT/DELETE 等；默认是 GET。",
                "headers": "可传 JSON 对象或 JSON 字符串，常用于 Content-Type、Authorization 等。",
                "body": "可选，请求体会按 utf-8 文本发送；适合传 JSON 字符串或普通文本。",
            },
            examples=[
                '{"tool": "http_request", "url": "https://example.com/health"}',
                '{"tool": "http_request", "url": "https://example.com/api", "method": "POST", "headers": {"Content-Type": "application/json"}, "body": "{\\"name\\": \\"demo\\"}"}',
            ],
        )

    def execute(self, params: dict[str, Any]) -> ToolExecutionResult:
        url = str(params.get("url", "")).strip()
        if not url:
            return ToolExecutionResult("http_request", False, "缺少必填参数 url")

        method = str(params.get("method", "GET")).upper()
        headers = self._normalize_headers(params.get("headers"))
        body = params.get("body")
        data = None if body is None else str(body).encode("utf-8")

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body_text = resp.read().decode("utf-8", "replace")
                result = (
                    f"status={resp.status}\n"
                    f"content_type={resp.headers.get('Content-Type', '')}\n\n"
                    f"{body_text[: self.max_chars]}"
                )
                if len(body_text) > self.max_chars:
                    result += "\n... 已截断"
                return ToolExecutionResult("http_request", True, result)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            return ToolExecutionResult("http_request", False, f"HTTP {exc.code}: {detail}")
        except Exception as exc:
            return ToolExecutionResult("http_request", False, f"请求失败: {exc}")

    def _normalize_headers(self, headers: Any) -> dict[str, str]:
        """把请求头统一整理成 `dict[str, str]`。"""

        if headers is None:
            return {"User-Agent": "SimplePythonAgent/1.0"}
        if isinstance(headers, dict):
            normalized = {str(k): str(v) for k, v in headers.items()}
            normalized.setdefault("User-Agent", "SimplePythonAgent/1.0")
            return normalized
        if isinstance(headers, str):
            parsed = json.loads(headers)
            if not isinstance(parsed, dict):
                raise ValueError("headers 字符串解析后必须是 JSON 对象")
            normalized = {str(k): str(v) for k, v in parsed.items()}
            normalized.setdefault("User-Agent", "SimplePythonAgent/1.0")
            return normalized
        raise ValueError("headers 必须为空、对象或 JSON 字符串")
