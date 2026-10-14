# LLM: 来源渲染只运输未解析引用；不得从模型 source/旧重放直接读取宿主 env/file。真正注入唯一属于受权 HTTP 出口。
# 模块用途: 生成来源请求结构和游标绑定，不读秘密、不发网络；变更需联查 watch HTTP 出口和来源重放测试。
"""HTTP source request facts learned for one concrete watch source.

The runtime knows how to validate and execute a request description.  It does
not know what the endpoint represents, how its records should be judged, or
which vendor/device produced them.  Those facts are learned by the Agent from
the user's documentation and real probes, then pinned to the source.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..settings.secret_ref import is_secret_ref
from ..tooling.web import _normalize_url
from ..tooling.web_http_helpers import normalize_headers, normalize_method

_CURSOR_PLACEHOLDERS = frozenset({"<next>", "<cursor>"})
_PAGE_SIZE_PLACEHOLDER = "<limit>"
_REQUEST_FIELDS = frozenset(
    {
        "method",
        "headers",
        "json_body",
        "cursor_binding",
        "page_size_binding",
        "secret_bindings",
        "credential_ref",
    }
)
_BINDING_FIELDS = frozenset({"location", "name", "path", "initial", "offset"})
_SECRET_BINDING_FIELDS = frozenset({"location", "name", "path", "secret_ref"})
# 响应体上限 1 MB：HTTP 采集源返回超大正文时截断，防内存被打爆。
_MAX_BODY_BYTES = 1_000_000
# 每个来源最多 32 个绑定：防止绑定数量失控。
_MAX_BINDING_COUNT = 32
# 字段路径最多 16 层：嵌套过深的字段按叶子收敛，防递归爆栈。
_MAX_PATH_DEPTH = 16
_SENSITIVE_PLAIN_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "api-key",
        "x-auth-token",
    }
)
_TOP_LEVEL_JSONPATH_RE = re.compile(r"^\$\.([^\.\[\]]+)(?:\[\]|\[\*\])?$")


# LLM: 载体只保留未解析 source 引用；模型 JSON 不能提供宿主授权，反射和冻结认证只在正式 HTTP choke 完成。
# 类用途: 运输已渲染的普通请求和待授权引用；不因这个 DTO 而获得文件或凭据读取权。
@dataclass(frozen=True)
class SourceHttpRequest:
    """普通数据已经渲染，秘密引用必须在正式 owner HTTP 入口重新授权。"""

    url: str
    method: str
    headers: dict[str, str]
    data: bytes | None
    secret_bindings: tuple[dict[str, Any], ...] = ()
    credential_ref: str = ""


_ADAPTER_REQUEST_FIELDS = frozenset({"url", "headers", "json_body"})


# LLM: Models often echo a top-level response field as ``$.items[]`` after a
# probe. Accept that exact structural shorthand, but reject nested JSONPath
# because this runtime deliberately supports only complete top-level records.
# 函数用途: 将单层 JSONPath 确定性归一为真实顶层字段名；嵌套或其他路径表达式
# 直接失败，避免 prepare 与真正 open 使用不同的结构合同。
def normalize_top_level_response_field(value: object) -> str:
    text = str(value or "").strip()
    if not text.startswith("$"):
        return text
    matched = _TOP_LEVEL_JSONPATH_RE.fullmatch(text)
    if matched is None:
        raise ValueError("响应字段只支持顶层字段名或等价的单层 JSONPath")
    return matched.group(1)


# LLM: 请求只登记未解析私人引用；不会读取 host secrets、授予 owner/root 权或把普通头变成秘密授权。
# 函数用途: 校验一次来源的普通 HTTP 与游标描述，让后续唯一 HTTP 出口能够完整核对待注入引用。
def normalize_source_http_request(
    raw_url: object,
    raw_request: object,
    *,
    poll: bool,
) -> tuple[str, dict[str, Any]]:
    """Validate one learned request description and return canonical facts.

    A URL query value of ``<next>``/``<cursor>`` or ``<limit>`` is accepted as
    structural shorthand from API documentation.  The parameter *name* is
    learned from that concrete URL; no name such as ``since`` is built in.
    """

    url = _normalize_url(raw_url)
    url, inferred_cursor, inferred_limit = _extract_query_bindings(url)
    if raw_request is None:
        raw: dict[str, Any] = {}
    elif isinstance(raw_request, dict):
        raw = dict(raw_request)
    else:
        raise ValueError("http_request 必须是对象")
    extras = sorted(str(key) for key in raw if key not in _REQUEST_FIELDS)
    if extras:
        raise ValueError(f"http_request 包含未知字段: {extras}")

    method = normalize_method(raw.get("method", "GET"))
    if method not in {"GET", "POST"}:
        raise ValueError("监测来源只允许 GET 或只读查询型 POST")
    headers = _plain_headers(raw.get("headers"))
    body = _json_body(raw.get("json_body"))
    if method == "GET" and body is not None:
        raise ValueError("GET 来源不能配置 json_body；请按接口文档选择正确请求方法")

    cursor = _normalize_binding(raw.get("cursor_binding"), kind="cursor")
    page_size = _normalize_binding(raw.get("page_size_binding"), kind="page_size")
    cursor = _merge_inferred_binding(cursor, inferred_cursor, "cursor_binding")
    page_size = _merge_inferred_binding(page_size, inferred_limit, "page_size_binding")
    secrets = _normalize_secret_bindings(raw.get("secret_bindings"))
    credential = _credential_reference(raw.get("credential_ref"))

    if poll:
        if cursor is not None or page_size is not None:
            raise ValueError("poll 快照来源不能同时配置游标或分页大小绑定")
    elif cursor is None:
        raise ValueError(
            "增量 HTTP 来源缺少现场学得的 cursor_binding；"
            "可在 URL 中使用任意参数名=<next>，或按接口文档显式配置。"
            "若它是快照接口，请显式使用 mode=poll。"
        )

    _reject_binding_conflicts(cursor, page_size, secrets)
    request: dict[str, Any] = {"method": method}
    if headers:
        request["headers"] = headers
    if body is not None:
        request["json_body"] = body
    if cursor is not None:
        request["cursor_binding"] = cursor
    if page_size is not None:
        request["page_size_binding"] = page_size
    if secrets:
        request["secret_bindings"] = secrets
    if credential:
        request["credential_ref"] = credential
    return url, request


# LLM: 普通结构和游标可由模型声明，但秘密绑定保持未解析；所有发送者必须沿 watch 原受权 HTTP choke。
# 函数用途: 不读取 env/file 地生成请求，旧 source 重放也不会直接解密或打开文件。
def render_source_http_request(
    source_url: str,
    request: dict[str, Any],
    *,
    cursor: int | None,
    page_size: int | None,
) -> SourceHttpRequest:
    """Render one request without mutating the persisted source facts."""

    method = str(request.get("method") or "GET")
    headers = dict(request.get("headers") or {})
    headers.setdefault("User-Agent", "MyAgent-WatchStream/1.0")
    body = copy.deepcopy(request.get("json_body"))
    query = parse_qsl(urlsplit(source_url).query, keep_blank_values=True)

    cursor_binding = request.get("cursor_binding")
    if isinstance(cursor_binding, dict):
        initial = int(cursor_binding.get("initial") or 0)
        if cursor is None:
            cursor = initial
        offset = cursor_binding.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError("cursor_binding.offset 必须是整数")
        request_cursor = max(initial, cursor + offset)
        query, body = _apply_value_binding(
            query,
            body,
            cursor_binding,
            request_cursor,
        )
    page_binding = request.get("page_size_binding")
    if isinstance(page_binding, dict) and page_size is not None:
        query, body = _apply_value_binding(query, body, page_binding, int(page_size))

    parts = urlsplit(source_url)
    rendered_url = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(query, doseq=True),
            parts.fragment,
        )
    )
    rendered_url = _normalize_url(rendered_url)
    data: bytes | None = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(data) > _MAX_BODY_BYTES:
            raise ValueError(f"来源请求体超过 {_MAX_BODY_BYTES} 字节")
        if not any(key.casefold() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
    return SourceHttpRequest(rendered_url, method, headers, data,
        tuple(_normalize_secret_bindings(request.get("secret_bindings"))), _credential_reference(request.get("credential_ref")))


# LLM: adapter 只能改原 origin 的普通传输字段；即使原绑定含旧 env/file，也只运输引用而不读取宿主秘密。
# 函数用途: 生成动态来源请求，将所有凭据留给正式 HTTP 出口按当前原授权核验。
def render_source_adapter_request(
    source_url: str,
    request: dict[str, Any],
    planned: object,
) -> SourceHttpRequest:
    """Render one adapter-planned request without granting a new origin.

    The learned adapter may vary path, query values, ordinary headers and the
    JSON body.  The host keeps the method, origin and SecretRef bindings under
    the already-published source authority, then re-runs the normal URL gate
    when the request is fetched.
    """

    if not isinstance(planned, dict):
        raise ValueError("来源适配器 request 必须是对象")
    extras = sorted(str(key) for key in planned if key not in _ADAPTER_REQUEST_FIELDS)
    if extras:
        raise ValueError(f"来源适配器 request 包含未知字段: {extras}")
    method = normalize_method(request.get("method", "GET"))
    if method not in {"GET", "POST"}:
        raise ValueError("监测来源只允许 GET 或只读查询型 POST")
    rendered_url = _normalize_url(planned.get("url") or source_url)
    if _origin_key(rendered_url) != _origin_key(source_url):
        raise ValueError("来源适配器不能把请求切换到已发布来源之外的 origin")

    headers = _plain_headers(request.get("headers"))
    dynamic_headers = _plain_headers(planned.get("headers"))
    headers.update(dynamic_headers)
    headers.setdefault("User-Agent", "MyAgent-WatchStream/1.0")
    body = (
        _json_body(planned.get("json_body"))
        if "json_body" in planned
        else _json_body(request.get("json_body"))
    )
    if method == "GET" and body is not None:
        raise ValueError("GET 来源适配器不能生成 json_body")

    query = parse_qsl(urlsplit(rendered_url).query, keep_blank_values=True)
    parts = urlsplit(rendered_url)
    rendered_url = _normalize_url(
        urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query, doseq=True),
                parts.fragment,
            )
        )
    )
    data: bytes | None = None
    if body is not None:
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(data) > _MAX_BODY_BYTES:
            raise ValueError(f"来源请求体超过 {_MAX_BODY_BYTES} 字节")
        if not any(key.casefold() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
    return SourceHttpRequest(rendered_url, method, headers, data,
        tuple(_normalize_secret_bindings(request.get("secret_bindings"))), _credential_reference(request.get("credential_ref")))


# LLM: 这里只校验原 TaskCredential 的引用形状，不读取记录、授予本人身份或把引用当 bearer token。
# 函数用途: 允许来源请求使用已有私人凭据编号，真正 scope/origin 校验仍在发送前执行。
def _credential_reference(value: object) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not re.fullmatch(r"cred_[0-9a-f]{32}", value):
        raise ValueError("credential_ref 必须是 request_credentials 返回的准确引用")
    return value


def _origin_key(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.casefold()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parts.hostname or "").casefold(), port


def public_source_http_request(request: object) -> dict[str, Any]:
    """Return model-visible transport facts without exposing SecretRef values."""

    if not isinstance(request, dict):
        return {}
    shown = copy.deepcopy(request)
    redacted: list[dict[str, Any]] = []
    for binding in list(shown.pop("secret_bindings", []) or []):
        if not isinstance(binding, dict):
            continue
        row = {key: value for key, value in binding.items() if key != "secret_ref"}
        row["configured"] = True
        redacted.append(row)
    if redacted:
        shown["secret_bindings"] = redacted
    return shown


def public_source_envelope(envelope: object) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        return {}
    shown = copy.deepcopy(envelope)
    if "request" in shown:
        shown["request"] = public_source_http_request(shown.get("request"))
    return shown


def _extract_query_bindings(
    url: str,
) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None]:
    parts = urlsplit(url)
    kept: list[tuple[str, str]] = []
    cursor: dict[str, Any] | None = None
    page_size: dict[str, Any] | None = None
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = value.strip().casefold()
        if lowered in _CURSOR_PLACEHOLDERS:
            if cursor is not None:
                raise ValueError("URL 中只能有一个游标占位参数")
            cursor = {"location": "query", "name": key, "initial": 0}
            continue
        if lowered == _PAGE_SIZE_PLACEHOLDER:
            if page_size is not None:
                raise ValueError("URL 中只能有一个分页大小占位参数")
            page_size = {"location": "query", "name": key}
            continue
        kept.append((key, value))
    canonical = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            urlencode(kept, doseq=True),
            parts.fragment,
        )
    )
    return canonical, cursor, page_size


def _plain_headers(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("http_request.headers 必须是对象")
    normalized = normalize_headers(value)
    supplied_user_agent = any(str(key).casefold() == "user-agent" for key in value)
    if not supplied_user_agent or (
        "User-Agent" in normalized
        and not any(str(key) == "User-Agent" for key in value)
    ):
        normalized.pop("User-Agent", None)
    for name in normalized:
        if name.casefold() in _SENSITIVE_PLAIN_HEADERS:
            raise ValueError(
                f"敏感 Header {name} 不能保存明文；请使用 secret_bindings + SecretRef"
            )
    return normalized


def _json_body(value: object) -> Any:
    if value is None:
        return None
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("http_request.json_body 必须是合法 JSON") from exc
    if len(encoded) > _MAX_BODY_BYTES:
        raise ValueError(f"http_request.json_body 超过 {_MAX_BODY_BYTES} 字节")
    return copy.deepcopy(value)


def _normalize_binding(raw: object, *, kind: str) -> dict[str, Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{kind}_binding 必须是对象")
    extras = sorted(str(key) for key in raw if key not in _BINDING_FIELDS)
    if extras:
        raise ValueError(f"{kind}_binding 包含未知字段: {extras}")
    location = str(raw.get("location") or "").strip().lower()
    binding = _binding_target(raw, location=location, kind=f"{kind}_binding")
    if kind == "cursor":
        initial = raw.get("initial", 0)
        if isinstance(initial, bool) or not isinstance(initial, int) or initial < 0:
            raise ValueError("cursor_binding.initial 必须是非负整数")
        binding["initial"] = initial
        offset = raw.get("offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise ValueError("cursor_binding.offset 必须是整数")
        if offset:
            binding["offset"] = offset
    elif "initial" in raw:
        raise ValueError("page_size_binding 不接受 initial")
    elif "offset" in raw:
        raise ValueError("page_size_binding 不接受 offset")
    return binding


def _normalize_secret_bindings(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_BINDING_COUNT:
        raise ValueError(f"secret_bindings 必须是最多 {_MAX_BINDING_COUNT} 项的数组")
    rows: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("secret_bindings 的每一项都必须是对象")
        extras = sorted(str(key) for key in raw if key not in _SECRET_BINDING_FIELDS)
        if extras:
            raise ValueError(f"secret_binding 包含未知字段: {extras}")
        location = str(raw.get("location") or "").strip().lower()
        if location not in {"header", "query", "json_body"}:
            raise ValueError("secret_binding.location 必须是 header、query 或 json_body")
        binding = _binding_target(raw, location=location, kind="secret_binding")
        ref = str(raw.get("secret_ref") or "").strip()
        if not is_secret_ref(ref):
            raise ValueError("secret_binding.secret_ref 必须使用 env: 或 file: 引用")
        binding["secret_ref"] = ref
        rows.append(binding)
    return rows


def _binding_target(raw: dict[str, Any], *, location: str, kind: str) -> dict[str, Any]:
    if location in {"query", "header"}:
        name = str(raw.get("name") or "").strip()
        if not name or len(name) > 128:
            raise ValueError(f"{kind}.name 必须是 1–128 字符")
        if raw.get("path") is not None:
            raise ValueError(f"{kind} 的 {location} 位置不能同时提供 path")
        if location == "header":
            normalized = normalize_headers({name: "x"})
            normalized.pop("User-Agent", None)
            name = next(iter(normalized))
        return {"location": location, "name": name}
    if location != "json_body":
        raise ValueError(f"{kind}.location 必须是 query 或 json_body")
    path = raw.get("path")
    if not isinstance(path, list) or not path or len(path) > _MAX_PATH_DEPTH:
        raise ValueError(f"{kind}.path 必须是 1–{_MAX_PATH_DEPTH} 段的字符串数组")
    segments = [str(segment).strip() for segment in path]
    if any(not segment or len(segment) > 128 for segment in segments):
        raise ValueError(f"{kind}.path 包含空段或超长段")
    if raw.get("name") is not None:
        raise ValueError(f"{kind} 的 json_body 位置不能同时提供 name")
    return {"location": location, "path": segments}


def _merge_inferred_binding(
    explicit: dict[str, Any] | None,
    inferred: dict[str, Any] | None,
    label: str,
) -> dict[str, Any] | None:
    if explicit is None:
        return inferred
    if inferred is None:
        return explicit
    if explicit != inferred:
        raise ValueError(f"URL 占位参数与 http_request.{label} 冲突")
    return explicit


def _binding_key(binding: dict[str, Any]) -> tuple[str, str]:
    location = str(binding.get("location") or "")
    if location == "json_body":
        return location, ".".join(str(item) for item in binding.get("path") or [])
    name = str(binding.get("name") or "")
    return location, name.casefold() if location == "header" else name


def _reject_binding_conflicts(
    cursor: dict[str, Any] | None,
    page_size: dict[str, Any] | None,
    secrets: list[dict[str, Any]],
) -> None:
    rows = [row for row in (cursor, page_size, *secrets) if row is not None]
    seen: set[tuple[str, str]] = set()
    for row in rows:
        key = _binding_key(row)
        if key in seen:
            raise ValueError(f"同一个请求位置被重复绑定: {key[0]}:{key[1]}")
        seen.add(key)


def _apply_value_binding(
    query: list[tuple[str, str]],
    body: Any,
    binding: dict[str, Any],
    value: object,
) -> tuple[list[tuple[str, str]], Any]:
    location = str(binding.get("location") or "")
    if location == "query":
        name = str(binding["name"])
        query = [(key, item) for key, item in query if key != name]
        query.append((name, str(value)))
        return query, body
    if location != "json_body":
        raise ValueError(f"不能把动态值写入请求位置: {location}")
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise ValueError("json_body 根必须是对象，才能写入结构化绑定")
    cursor = body
    path = list(binding.get("path") or [])
    for segment in path[:-1]:
        child = cursor.get(segment)
        if child is None:
            child = {}
            cursor[segment] = child
        if not isinstance(child, dict):
            raise ValueError(f"json_body 路径 {'.'.join(path)} 被非对象值阻断")
        cursor = child
    cursor[path[-1]] = value
    return query, body


__all__ = [
    "SourceHttpRequest",
    "normalize_source_http_request",
    "normalize_top_level_response_field",
    "public_source_envelope",
    "public_source_http_request",
    "render_source_adapter_request",
    "render_source_http_request",
]
