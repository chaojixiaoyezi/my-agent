# LLM: 模型目录只提供结构化容量事实，鉴权及自定义头与生成快照一致，失败不冒充成功。
# 模块用途: 按需发现供应商的模型容量，不从名称猜容量。
from __future__ import annotations

"""Provider model metadata discovery kept separate from generation backends."""

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .gateway_helpers import GatewayRequest, get_json

_CONTEXT_WINDOW_FIELDS = (
    "context_window_tokens",
    "max_context_tokens",
    "context_length",
    "context_window",
    "input_token_limit",
    "max_input_tokens",
    "max_model_len",
)
_NESTED_METADATA_FIELDS = ("capabilities", "limits", "metadata", "model_info")


@dataclass(frozen=True)
class ProviderModelMetadata:
    """Provider facts discovered for one configured model."""

    context_window_tokens: int = 0
    record: dict[str, Any] = field(default_factory=dict)


# LLM: metadata 请求继承模型配置快照，头部不进入公开模型记录。
# 类用途: 描述目录请求的连接及兼容信息。
@dataclass(frozen=True)
class ProviderMetadataOptions:
    """Connection identity and bounded timeouts for metadata discovery."""

    api_base: str
    api_key: str
    model_name: str
    request_timeout: int
    connect_timeout: float
    custom_headers: dict[str, str] = field(default_factory=dict)


# LLM: Only explicit provider metadata is authoritative. Missing fields return zero so the
# configured context-window fallback remains the sole secondary source of truth.
# 人类: 模型目录探测失败不影响正常聊天；每个后端实例会缓存本函数结果。
def discover_provider_model_metadata(options: ProviderMetadataOptions) -> ProviderModelMetadata:
    headers = {
        **options.custom_headers,
        "Accept": "application/json",
        "Authorization": f"Bearer {options.api_key}",
        "x-api-key": options.api_key,
    }
    last_record: dict[str, Any] = {}
    for path in _metadata_paths(options.api_base, options.model_name):
        try:
            payload = get_json(_metadata_request(options, path, headers))
        except Exception:  # noqa: BLE001 - discovery must fall back to configured capacity
            continue
        record = _model_record(payload, options.model_name)
        if not record:
            continue
        last_record = record
        if window := _context_window_from_record(record):
            return ProviderModelMetadata(context_window_tokens=window, record=record)
    return ProviderModelMetadata(record=last_record)


def _metadata_paths(api_base: str, model_name: str) -> tuple[str, ...]:
    model_path = quote(model_name, safe="")
    if api_base.endswith("/v1"):
        return ("/models", f"/models/{model_path}")
    return ("/v1/models", f"/v1/models/{model_path}")


def _metadata_request(
    options: ProviderMetadataOptions,
    path: str,
    headers: dict[str, str],
) -> GatewayRequest:
    return GatewayRequest(
        api_base=options.api_base,
        api_key=options.api_key,
        path=path,
        payload={},
        headers=headers,
        timeout=min(3, options.request_timeout),
        connect_timeout=min(2.0, options.connect_timeout),
    )


def _model_record(payload: object, model_name: str) -> dict[str, Any]:
    """Extract the configured model from common provider model-list shapes."""
    if not isinstance(payload, dict):
        return {}
    values = payload.get("data") or payload.get("models")
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        values = [payload]
    target = str(model_name or "").strip().lower().split("/")[-1]
    for item in values:
        if not isinstance(item, dict):
            continue
        item_name = str(item.get("id") or item.get("name") or item.get("model") or "").strip().lower()
        if not target or item_name == target or item_name.split("/")[-1] == target:
            return dict(item)
    return {}


def _context_window_from_record(record: dict[str, Any]) -> int:
    """Read explicit capacity fields without inferring capacity from a model name."""
    containers = [record]
    containers.extend(
        nested
        for key in _NESTED_METADATA_FIELDS
        if isinstance((nested := record.get(key)), dict)
    )
    for container in containers:
        if value := _first_positive_window(container):
            return value
    return 0


def _first_positive_window(container: dict[str, Any]) -> int:
    for field_name in _CONTEXT_WINDOW_FIELDS:
        try:
            value = int(container.get(field_name) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0
