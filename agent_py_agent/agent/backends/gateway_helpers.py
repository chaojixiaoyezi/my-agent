from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class _GatewayRequest:
    api_base: str
    api_key: str
    path: str
    payload: dict[str, Any]
    headers: dict[str, str]
    timeout: int

    @property
    def url(self) -> str:
        return self.api_base + self.path


def post_json(
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    **options: Any,
) -> dict[str, Any]:
    timeout = int(options["timeout"])
    request = _GatewayRequest(api_base, api_key, path, payload, headers, timeout)
    _require_api_key(request.api_key)
    req = _urllib_request(request)
    try:
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc


def post_stream(
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    **options: Any,
) -> list[str]:
    timeout = int(options["timeout"])
    request = _GatewayRequest(api_base, api_key, path, payload, headers, timeout)
    return list(_post_stream_lines(request))


def post_stream_iter(
    api_base: str,
    api_key: str,
    path: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    **options: Any,
):
    timeout = int(options["timeout"])
    request = _GatewayRequest(api_base, api_key, path, payload, headers, timeout)
    yield from _post_stream_lines(request)


def _post_stream_lines(request: _GatewayRequest) -> Iterator[str]:
    request.payload["stream"] = True
    _require_api_key(request.api_key)
    req = _urllib_request(request)
    try:
        with urllib.request.urlopen(req, timeout=request.timeout) as resp:
            yield from _iter_sse_data_lines(resp)
    except urllib.error.HTTPError as exc:
        raise _runtime_http_error(exc) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _runtime_network_error(exc, request) from exc


def _urllib_request(request: _GatewayRequest) -> urllib.request.Request:
    return urllib.request.Request(
        request.url,
        data=json.dumps(request.payload).encode("utf-8"),
        method="POST",
        headers=request.headers,
    )


def _require_api_key(api_key: str) -> None:
    if not api_key:
        raise ValueError("api_key 为空：请在配置文件中填写 API Key。")


def _runtime_http_error(exc: urllib.error.HTTPError) -> RuntimeError:
    detail = exc.read().decode("utf-8", "replace")
    return RuntimeError(f"HTTP {exc.code}: {detail}")


def _runtime_network_error(exc: BaseException, request: _GatewayRequest) -> RuntimeError:
    parsed = urllib.parse.urlparse(request.url)
    host = parsed.netloc or parsed.path.split("/", 1)[0] or request.api_base
    reason = getattr(exc, "reason", None) or str(exc) or exc.__class__.__name__
    return RuntimeError(
        "网络请求失败: "
        f"无法连接模型接口 {host}（{request.url}）。"
        "请检查 DNS、网络/代理和 api_base 配置；"
        f"底层错误: {reason}"
    )


def _iter_sse_data_lines(response) -> Iterator[str]:
    for raw_line in response:
        line = raw_line.decode("utf-8").strip()
        if _is_sse_data_line(line):
            yield line[5:].strip()


def _is_sse_data_line(line: str) -> bool:
    return bool(line and line.startswith("data:"))
