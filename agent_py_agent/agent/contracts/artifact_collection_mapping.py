# LLM: Collection mapping checks verify source items appear in final artifacts.
# 模块用途: 校验 source JSON 中的结构化条目是否能按 key_fields 映射到最终产物文本，避免“源数据有了但成品漏项”。

from __future__ import annotations

import json
from pathlib import Path

from .artifact_acceptance_models import ArtifactFinding
from .artifact_structured_contracts import positive_int, string_list


# LLM: mapping_findings 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 mapping findings 相关的结构化数据、路径或 finding，供当前合同链路调用。
def mapping_findings(
    items: list[object],
    contract: dict[str, object],
    source_ref: str,
    workspace_root: Path,
) -> list[ArtifactFinding]:
    mapping = contract.get("mapping")
    if not isinstance(mapping, dict):
        return []
    artifact_ref = str(mapping.get("artifact_ref") or "").strip()
    key_fields = string_list(mapping.get("key_fields"))
    if not artifact_ref or not key_fields:
        return [_finding("ARTIFACT_MAPPING_CONTRACT_INVALID", "mapping requires artifact_ref and key_fields.", source_ref)]
    artifact_path = _workspace_path(artifact_ref, workspace_root)
    if not _inside_workspace(artifact_path, workspace_root):
        return [_finding("ARTIFACT_MAPPING_OUTSIDE_WORKSPACE", "mapping artifact is outside workspace_root.", artifact_ref)]
    try:
        text = artifact_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [_finding("ARTIFACT_MAPPING_TARGET_MISSING", "mapping artifact does not exist.", artifact_ref)]
    mapped_items = [item for item in items if isinstance(item, dict) and _item_mapped(item, key_fields, text)]
    required = positive_int(mapping.get("min_mapped_items")) or len(items)
    if len(mapped_items) >= required:
        return []
    missing_keys = [
        _item_key_values(item, key_fields)
        for item in items
        if isinstance(item, dict) and not _item_mapped(item, key_fields, text)
    ]
    return [
        _finding(
            "ARTIFACT_MAPPING_MISSING",
            f"artifact maps {len(mapped_items)} source items, expected at least {required}.",
            artifact_ref,
            _compact_json({"mapped": len(mapped_items), "required": required, "missing_keys": missing_keys[:10]}),
        )
    ]


# LLM: _item_mapped 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item mapped 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_mapped(item: dict[str, object], key_fields: list[str], text: str) -> bool:
    keys = [str(item.get(field) or "").strip() for field in key_fields]
    return bool(keys) and all(key and key in text for key in keys)


# LLM: _item_key_values 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 item key values 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _item_key_values(item: dict[str, object], key_fields: list[str]) -> dict[str, str]:
    return {field: str(item.get(field) or "").strip() for field in key_fields if str(item.get(field) or "").strip()}


# LLM: _workspace_path 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 workspace path 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _workspace_path(ref: str, workspace_root: Path) -> Path:
    path = Path(ref)
    return path if path.is_absolute() else (workspace_root / path).resolve()


# LLM: _inside_workspace 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 inside workspace 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _inside_workspace(path: Path, workspace_root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(workspace_root.resolve(strict=False))
        return True
    except ValueError:
        return False


# LLM: _finding 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 finding 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _finding(code: str, message: str, location: str = "", value: str = "") -> ArtifactFinding:
    return ArtifactFinding(code=code, severity="hard", message=message, location=location, value=value)


# LLM: _compact_json 是 agent_py_agent/agent/contracts/artifact_collection_mapping.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 compact json 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["mapping_findings"]
