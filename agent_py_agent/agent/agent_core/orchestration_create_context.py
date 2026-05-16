# LLM: Create-subagent context helpers preserve refs-first inputs without expanding create policy.
# 模块用途: 归一化 create_subagents 传下来的资料路径和上下文包，写入子代理 Context Manifest。

from __future__ import annotations

from collections.abc import Iterable, Iterator

from .parameters import _string_list


# LLM: create_context_manifest preserves refs-first source paths as machine-readable child context.
# 函数用途: 把 create_subagents 的资料路径/上下文清单传给子代理 runner，避免 root 先读完正文再派工。
def create_context_manifest(raw_params: dict[str, object]) -> dict[str, object]:
    manifest = _dict_param(raw_params.get("context_manifest"))
    _set_list_if_present(manifest, "required_read_paths", _required_read_paths(raw_params, manifest))
    _set_list_if_present(manifest, "task_pack_refs", _task_pack_refs(raw_params, manifest))
    _set_list_if_present(manifest, "omitted_context", _omitted_context(raw_params, manifest))
    _copy_optional_manifest_scalars(manifest, raw_params)
    return manifest


# LLM: create_context_packs normalizes refs-only context pack hints without reading their bodies.
# 函数用途: 保留父级传下来的 context_packs；简写 refs 会变成 path pack，供 runner prompt 展示。
def create_context_packs(raw_params: dict[str, object]) -> list[dict[str, object]]:
    packs = _dict_list_param(raw_params.get("context_packs"))
    for ref in _string_list(raw_params.get("context_pack_refs")):
        if not _pack_has_ref(packs, ref):
            packs.append({"kind": "context_ref", "path": ref})
    return packs


# LLM: _required_read_paths merges the accepted source-path aliases for child reads.
# 函数用途: 收集 required/source/reference/material 路径字段，作为子代理必须自己读取的资料索引。
def _required_read_paths(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    return _merged_string_list([
        manifest.get("required_read_paths"),
        raw_params.get("required_read_paths"),
        raw_params.get("source_paths"),
        raw_params.get("source_refs"),
        raw_params.get("reference_paths"),
        raw_params.get("material_refs"),
    ])


# LLM: _task_pack_refs merges context pack reference aliases.
# 函数用途: 收集 task/context pack 引用，供 runner 在上下文包区展示。
def _task_pack_refs(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    return _merged_string_list([
        manifest.get("task_pack_refs"),
        raw_params.get("task_pack_refs"),
        raw_params.get("context_pack_refs"),
    ])


# LLM: _omitted_context keeps parent-declared omissions visible to the child.
# 函数用途: 合并 omitted_context，提醒子代理哪些正文被故意省略、需要按 refs 补读。
def _omitted_context(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    return _merged_string_list([
        manifest.get("omitted_context"),
        raw_params.get("omitted_context"),
    ])


# LLM: _copy_optional_manifest_scalars preserves scalar manifest hints without overriding explicit manifest fields.
# 函数用途: 将 role_pack、quality_contract_ref 和 token_budget 这类轻量字段补进 manifest。
def _copy_optional_manifest_scalars(manifest: dict[str, object], raw_params: dict[str, object]) -> None:
    for key in ["role_pack", "quality_contract_ref", "token_budget"]:
        if raw_params.get(key) not in (None, "") and key not in manifest:
            manifest[key] = raw_params[key]


# LLM: _set_list_if_present avoids writing empty manifest lists.
# 函数用途: 只有列表有内容时才写入 manifest，保持旧记录的空字段语义。
def _set_list_if_present(manifest: dict[str, object], key: str, values: list[str]) -> None:
    if values:
        manifest[key] = values


# LLM: _dict_param accepts only explicit mapping payloads.
# 函数用途: 读取可选 dict 参数；非对象输入按空对象处理，避免字符串被误当字段。
def _dict_param(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


# LLM: _dict_list_param keeps context pack lists tolerant but explicit.
# 函数用途: 将 context_packs 归一成对象列表；单个对象也作为一条 pack。
def _dict_list_param(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


# LLM: _pack_has_ref deduplicates shorthand pack refs against path/ref entries.
# 函数用途: 判断 context_packs 是否已经包含同一个 path/ref，避免重复显示。
def _pack_has_ref(packs: list[dict[str, object]], ref: str) -> bool:
    return any(str(item.get("path") or item.get("ref") or "") == ref for item in packs)


# LLM: _merged_string_list merges structured refs while preserving first occurrence order.
# 函数用途: 合并多种资料路径字段，去重后交给 ContextManifest。
def _merged_string_list(values: Iterable[object]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in _iter_string_list_items(values):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
    return merged


# LLM: _iter_string_list_items flattens candidate ref fields for shallow merge logic.
# 函数用途: 逐个产出多个参数里的字符串项，避免合并函数出现深层嵌套。
def _iter_string_list_items(values: Iterable[object]) -> Iterator[str]:
    for value in values:
        yield from _string_list(value)
