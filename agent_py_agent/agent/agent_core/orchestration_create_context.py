# LLM: Create-subagent context helpers preserve refs-first inputs without expanding create policy.
# 模块用途: 归一化 create_subagents 传下来的资料路径和上下文包，写入子代理 Context Manifest。

from __future__ import annotations

from collections.abc import Iterable, Iterator

from ..subagents.services.idempotency_contract_identity import (
    idempotency_contract_identity_from_context_packs,
)
from .parameters import _string_list
from .runner_ref_fields import (
    _file_refs_from_value,
    _manifest_input_refs,
    _normalize_file_ref,
    params_output_refs,
)


# LLM: create_context_manifest preserves refs-first source paths as machine-readable child context.
# 函数用途: 把 create_subagents 的资料路径/上下文清单传给子代理 runner，避免 root 先读完正文再派工。
def create_context_manifest(raw_params: dict[str, object]) -> dict[str, object]:
    manifest = _dict_param(raw_params.get("context_manifest"))
    _set_list_if_present(manifest, "required_read_paths", _required_read_paths(raw_params, manifest))
    _set_list_if_present(manifest, "hint_read_paths", _hint_read_paths(raw_params, manifest))
    _set_list_if_present(manifest, "task_pack_refs", _task_pack_refs(raw_params, manifest))
    _set_list_if_present(manifest, "omitted_context", _omitted_context(raw_params, manifest))
    _copy_optional_manifest_scalars(manifest, raw_params)
    return manifest


# LLM: create_context_packs normalizes refs-only context pack hints without reading their bodies.
# 函数用途: 保留父级传下来的 context_packs；产物 refs 明确时补系统幂等合同，避免重复创建依赖模型主动传。
def create_context_packs(raw_params: dict[str, object]) -> list[dict[str, object]]:
    packs = _dict_list_param(raw_params.get("context_packs"))
    for ref in _string_list(raw_params.get("context_pack_refs")):
        if not _pack_has_ref(packs, ref):
            packs.append({"kind": "context_ref", "path": ref})
    _append_system_idempotency_pack(packs, raw_params)
    return packs


# LLM: _append_system_idempotency_pack derives replay safety from structured output refs only.
# 函数用途: 当模型没有主动传幂等合同，但工具参数已有明确产物 refs 时，系统生成可复用合同；普通 goal 文本不参与。
def _append_system_idempotency_pack(packs: list[dict[str, object]], raw_params: dict[str, object]) -> None:
    if idempotency_contract_identity_from_context_packs(packs):
        return
    refs = params_output_refs(raw_params)
    if not refs:
        return
    packs.append({
        "kind": "idempotency_contract",
        "contract": {
            "schema": "subagent_idempotency_contract.v1",
            "kind": "system_derived_output_scope",
            "idempotency_key": "create_subagents.output_refs",
            "scope_refs": refs,
        },
    })


# LLM: _required_read_paths preserves accepted source-path aliases as child read hints.
# 函数用途: 收集 required/source/reference/material 路径字段，作为子代理可读线索；不作为启动前置门。
def _required_read_paths(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    refs = _merged_string_list([
        manifest.get("required_read_paths"),
        _manifest_file_refs(manifest),
        raw_params.get("required_read_paths"),
        raw_params.get("input_refs"),
        raw_params.get("input_files"),
        raw_params.get("source_paths"),
        raw_params.get("source_refs"),
        raw_params.get("reference_paths"),
        raw_params.get("material_refs"),
    ])
    return _without_current_outputs(refs, params_output_refs(raw_params))


# LLM: _hint_read_paths grants literal goal refs as optional read roots, not dependency blockers.
# 函数用途: 模型常把资料路径只写进 goal；这些路径可以帮助子代理读文件，但不能作为
# runner 启动门或候选过滤条件，避免“未来输出文件”被误判为缺失输入。
def _hint_read_paths(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    return _merged_string_list([
        manifest.get("hint_read_paths"),
        raw_params.get("hint_read_paths"),
        _explicit_goal_file_refs(raw_params.get("goal")),
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


# LLM: _manifest_file_refs preserves generic ref-valued manifest fields.
# 函数用途: 模型常写 source_file/alert_file/source_analyses 等开放字段名；这里只看值是否像文件 ref，
# 不从字段名或业务语义推断规则。
def _manifest_file_refs(manifest: dict[str, object]) -> list[str]:
    return _manifest_input_refs({key: value for key, value in manifest.items() if key != "required_read_paths"})


# LLM: _set_list_if_present avoids writing empty manifest lists.
# 函数用途: 只有列表有内容时才写入 manifest，保持旧记录的空字段语义。
def _set_list_if_present(manifest: dict[str, object], key: str, values: list[str]) -> None:
    if values:
        manifest[key] = values


# LLM: _dict_param accepts explicit mappings and refs-only manifest shorthand.
# 函数用途: 读取可选 dict 参数；列表/字符串短写只作为 read refs，避免显式资料路径静默丢失。
def _dict_param(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    refs = _context_manifest_shorthand_refs(value)
    return {"required_read_paths": refs} if refs else {}


# LLM: _context_manifest_shorthand_refs preserves model-written refs-only manifests.
# 函数用途: 兼容 context_manifest=["/path/a.txt"] 或 context_manifest="/path/a.txt"；
# 对象条目不转成字符串，避免把上下文对象误当文件路径。
def _context_manifest_shorthand_refs(value: object) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return _refs_from_shorthand_sequence(value)
    if isinstance(value, str):
        return _refs_only_shorthand_items(value)
    return []


def _refs_from_shorthand_sequence(value: object) -> list[str]:
    refs: list[str] = []
    for item in value if isinstance(value, (list, tuple, set)) else []:
        if isinstance(item, dict):
            continue
        refs.extend(_refs_only_shorthand_items(item))
    return _merged_string_list([refs])


# LLM: _refs_only_shorthand_items keeps prose context from becoming hard input dependencies.
# 函数用途: context_manifest 字符串短写只接受纯路径项；普通说明文字可留给 goal/context，不进入启动前必读门。
def _refs_only_shorthand_items(value: object) -> list[str]:
    refs: list[str] = []
    for item in _string_list(value):
        if not _is_file_ref_token(item):
            continue
        refs.append(_normalize_file_ref(str(item).strip().strip("- ").strip()))
    return refs


def _is_file_ref_token(value: object) -> bool:
    text = str(value or "").strip().strip("- ").strip()
    if not text:
        return False
    normalized = _normalize_file_ref(text)
    file_refs = _file_refs_from_value(text)
    return len(file_refs) == 1 and file_refs[0] == normalized


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
        ref = _normalize_file_ref(item)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        merged.append(ref)
    return merged


# LLM: _iter_string_list_items flattens candidate ref fields for shallow merge logic.
# 函数用途: 逐个产出多个参数里的字符串项，避免合并函数出现深层嵌套。
def _iter_string_list_items(values: Iterable[object]) -> Iterator[str]:
    for value in values:
        yield from _string_list(value)


# LLM: _explicit_goal_file_refs extracts literal file refs only, not task semantics.
# 函数用途: 当模型把明确文件路径放在 create_subagents.goal 而漏填 required_read_paths 时，
# 只把路径形态的 token 补成只读资料 refs；不根据普通描述推断任务规则。
def _explicit_goal_file_refs(value: object) -> list[str]:
    from .runner_ref_fields import _file_refs_from_value

    return _file_refs_from_value(value)


# LLM: _without_current_outputs prevents a child from waiting for its own future artifact.
# 函数用途: goal 里可能同时出现输入路径和 output_files 路径；结构化 output refs 是写入目标，
# 不应作为启动前必须存在的 required_read_paths，否则新产物会把 runner 自己卡住。
def _without_current_outputs(refs: list[str], output_refs: list[str]) -> list[str]:
    if not output_refs:
        return refs
    return [ref for ref in refs if not _matches_any_output_ref(ref, output_refs)]


def _matches_any_output_ref(ref: str, output_refs: list[str]) -> bool:
    return any(_path_ref_matches(ref, output_ref) for output_ref in output_refs)


def _path_ref_matches(ref: str, output_ref: str) -> bool:
    left = str(ref or "").strip()
    right = str(output_ref or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    left_path = left.replace("\\", "/")
    right_path = right.replace("\\", "/")
    return left_path.endswith("/" + right_path) or right_path.endswith("/" + left_path)
