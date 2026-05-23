# LLM: delivery repair source helpers bind artifact reads to structured source refs.
# 模块用途: 判断 read_artifact 是否读取了 recovery action 明确声明的来源产物，避免随意读旧 artifact 被当作修复进展。

from __future__ import annotations

from .tool_delivery_repair_paths import call_path, same_path_ref


# LLM: read_artifact_matches_declared_source compares a tool call against source refs in one action.
# 函数用途: 只根据 source_artifact_refs/source_refs/input_artifacts 等机器字段判断 artifact 读取是否可作为 checkpoint 来源。
def read_artifact_matches_declared_source(call: dict[str, object], action: dict[str, object]) -> bool:
    ref = call_path(call)
    if not ref:
        return False
    return any(same_path_ref(ref, source_ref) for source_ref in declared_source_refs(action))


# LLM: declared_source_refs flattens structured source references from a recovery action.
# 函数用途: 收集 action 及其 collection_contract 中声明的来源 ref，不读取普通自然语言说明。
def declared_source_refs(action: dict[str, object]) -> list[str]:
    refs = _top_level_source_refs(action)
    refs.extend(_source_refs_from_sequence(action.get("source_artifact_refs")))
    refs.extend(_source_refs_from_sequence(action.get("source_artifacts")))
    refs.extend(_source_refs_from_sequence(action.get("source_refs")))
    refs.extend(_source_refs_from_sequence(action.get("input_artifacts")))
    refs.extend(_source_refs_from_sequence(action.get("artifact_refs")))
    collection = action.get("collection_contract")
    if isinstance(collection, dict):
        refs.extend(declared_source_refs(collection))
    return list(dict.fromkeys(refs))


# LLM: _top_level_source_refs extracts source-like scalar fields from one mapping.
# 函数用途: 将 source_ref/source_json_ref/input_ref 等结构化字段转成统一 ref 列表。
def _top_level_source_refs(value: dict[str, object]) -> list[str]:
    return [
        ref
        for key in (
            "source_ref",
            "source_artifact_ref",
            "source_data_ref",
            "source_json_ref",
            "input_artifact_ref",
            "input_ref",
        )
        for ref in [str(value.get(key) or "").strip()]
        if ref
    ]


# LLM: _source_refs_from_sequence extracts refs from list fields without nested control flow.
# 函数用途: 支持 source_artifact_refs/source_refs 既可以是字符串列表，也可以是对象列表。
def _source_refs_from_sequence(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        ref
        for item in value
        for ref in _source_refs_from_item(item)
    ]


# LLM: _source_refs_from_item normalizes one source list item.
# 函数用途: 从字符串或对象形式的来源条目里取 artifact_ref/path/ref/source_ref 等机器字段。
def _source_refs_from_item(value: object) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return _source_refs_from_mapping(value)
    return []


# LLM: _source_refs_from_mapping extracts source refs from one source object.
# 函数用途: 展平 artifact_ref/path/ref/source_artifact_ref/uri 等来源字段。
def _source_refs_from_mapping(value: dict[str, object]) -> list[str]:
    return [
        ref
        for key in ("artifact_ref", "path", "ref", "source_ref", "source_artifact_ref", "uri")
        for ref in [str(value.get(key) or "").strip()]
        if ref
    ]


__all__ = ["declared_source_refs", "read_artifact_matches_declared_source"]
