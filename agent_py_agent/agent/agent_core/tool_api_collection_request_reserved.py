# LLM: API collection request metadata guards are shared tool-gateway checks.
# 模块用途: 校验 api_json_collection 参数保留合同声明的 request reserved 机器字段。

from __future__ import annotations


# LLM: missing_request_reserved_metadata reports request entries that dropped declared machine metadata.
# 函数用途: 对比 collection_contract.api_request 中声明的 reserved 路径，阻止模型改写请求时丢掉窗口/口径字段。
def missing_request_reserved_metadata(payload: dict[str, object], contract: dict[str, object]) -> list[str]:
    required = _declared_request_reserved_paths(contract)
    if not required:
        return []
    items = _payload_request_items(payload)
    if not items:
        return ["missing request reserved metadata: no request entries found"]
    findings: list[str] = []
    for index, item in enumerate(items, start=1):
        reserved = item.get("reserved") if isinstance(item, dict) else None
        missing = [path for path in required if not _has_reserved_path(reserved, path)]
        if missing:
            findings.append(f"missing request reserved metadata at request {index}: {', '.join(missing[:8])}")
    return findings


def _declared_request_reserved_paths(contract: dict[str, object]) -> list[str]:
    collection = contract.get("collection_contract")
    api_request = collection.get("api_request") if isinstance(collection, dict) else None
    if not isinstance(api_request, dict):
        return []
    paths: set[str] = set()
    _collect_reserved_paths(api_request.get("request_reserved"), "", paths)
    _collect_request_item_reserved_paths(api_request.get("request_ranges"), paths)
    _collect_request_item_reserved_paths(api_request.get("requests"), paths)
    return sorted(paths)


def _collect_request_item_reserved_paths(value: object, paths: set[str]) -> None:
    for item in value if isinstance(value, list) else []:
        if isinstance(item, dict):
            _collect_reserved_paths(item.get("reserved"), "", paths)


def _collect_reserved_paths(value: object, prefix: str, paths: set[str]) -> None:
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        text = str(key or "").strip()
        if not text:
            continue
        path = f"{prefix}.{text}" if prefix else text
        if isinstance(item, dict):
            _collect_reserved_paths(item, path, paths)
        else:
            paths.add(path)


def _payload_request_items(payload: dict[str, object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for key in ("requests", "request_ranges", "source_artifacts"):
        value = payload.get(key)
        if isinstance(value, list):
            items.extend(dict(item) for item in value if isinstance(item, dict))
    return items


def _has_reserved_path(value: object, path: str) -> bool:
    if not isinstance(value, dict):
        return False
    current: object = value
    for part in path.split("."):
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    return current not in (None, "")


__all__ = ["missing_request_reserved_metadata"]
