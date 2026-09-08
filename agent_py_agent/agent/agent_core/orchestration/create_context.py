# LLM: 子代理上下文只描述资料和显式合同；输入输出引用不生成派工身份或文件权限，根/递归共用。
# 模块用途: 组装派工资料清单并规范显式路径；同一文件可被多个独立子代理使用，不因此合并任务。

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path

from ...common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ..runner.ref_fields import (
    _file_refs_from_value,
    _manifest_input_refs,
    _normalize_file_ref,
    params_output_refs,
)


def create_context_manifest(raw_params: dict[str, object]) -> dict[str, object]:
    manifest = _dict_param(raw_params.get("context_manifest"))
    _set_list_if_present(manifest, "required_read_paths", _required_read_paths(raw_params, manifest))
    _set_list_if_present(manifest, "hint_read_paths", _hint_read_paths(raw_params, manifest))
    _set_list_if_present(manifest, "task_pack_refs", _task_pack_refs(raw_params, manifest))
    _set_list_if_present(manifest, "omitted_context", _omitted_context(raw_params, manifest))
    _copy_optional_manifest_scalars(manifest, raw_params)
    return manifest


# LLM: 保留调用方明确提交的上下文和幂等合同；不得从 IO、goal 或名称推导创建身份，无落盘副作用。
# 函数用途: 合并显式上下文包与引用；真正的调用重放由 ToolOperation 账本负责，不能在这里猜重复派工。
def create_context_packs(raw_params: dict[str, object]) -> list[dict[str, object]]:
    packs = _dict_list_param(raw_params.get("context_packs"))
    for ref in string_list(raw_params.get("context_pack_refs"), TOOL_TEXT_LIST_OPTIONS):
        if not _pack_has_ref(packs, ref):
            packs.append({"kind": "context_ref", "path": ref})
    return packs


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


# runner 启动门或候选过滤条件，避免“未来输出文件”被误判为缺失输入。
def _hint_read_paths(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    goal_refs = (
        _explicit_goal_file_refs(raw_params.get("goal"))
        if raw_params.get("_include_goal_file_hints") is not False
        else []
    )
    return _merged_string_list([
        manifest.get("hint_read_paths"),
        raw_params.get("hint_read_paths"),
        goal_refs,
    ])


def _task_pack_refs(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    return _merged_string_list([
        manifest.get("task_pack_refs"),
        raw_params.get("task_pack_refs"),
        raw_params.get("context_pack_refs"),
    ])


def _omitted_context(raw_params: dict[str, object], manifest: dict[str, object]) -> list[str]:
    return _merged_string_list([
        manifest.get("omitted_context"),
        raw_params.get("omitted_context"),
    ])


def _copy_optional_manifest_scalars(manifest: dict[str, object], raw_params: dict[str, object]) -> None:
    for key in ["role_pack", "quality_contract_ref", "token_budget"]:
        if raw_params.get(key) not in (None, "") and key not in manifest:
            manifest[key] = raw_params[key]


# 不从字段名或业务语义推断规则。
def _manifest_file_refs(manifest: dict[str, object]) -> list[str]:
    return _manifest_input_refs({key: value for key, value in manifest.items() if key != "required_read_paths"})


def _set_list_if_present(manifest: dict[str, object], key: str, values: list[str]) -> None:
    if values:
        manifest[key] = values


def _dict_param(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return dict(value)
    refs = _context_manifest_shorthand_refs(value)
    return {"required_read_paths": refs} if refs else {}


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


def _refs_only_shorthand_items(value: object) -> list[str]:
    refs: list[str] = []
    for item in string_list(value, TOOL_TEXT_LIST_OPTIONS):
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


def _dict_list_param(value: object) -> list[dict[str, object]]:
    if isinstance(value, dict):
        return [dict(value)]
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _pack_has_ref(packs: list[dict[str, object]], ref: str) -> bool:
    return any(str(item.get("path") or item.get("ref") or "") == ref for item in packs)


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


def _iter_string_list_items(values: Iterable[object]) -> Iterator[str]:
    for value in values:
        yield from string_list(value, TOOL_TEXT_LIST_OPTIONS)


# 只把路径形态的 token 补成只读资料 refs；不根据普通描述推断任务规则。
def _explicit_goal_file_refs(value: object) -> list[str]:
    from ..runner.ref_fields import _file_refs_from_value

    return _file_refs_from_value(value)


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


_PRODUCT_TARGET_SUFFIXES = {
    ".html",
    ".htm",
    ".css",
    ".js",
    ".mjs",
    ".cjs",
    ".ts",
    ".tsx",
    ".jsx",
    ".py",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".txt",
    ".csv",
    ".xlsx",
    ".xls",
    ".pdf",
}


def normalized_write_root(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if _path_has_file_suffix(path):
        path = path.parent
    return str(path)



def agent_workspace_roots(agent: object, root: Path) -> list[Path]:
    raw_roots = getattr(getattr(agent, "subagents", None), "workspace_roots", None)
    if not isinstance(raw_roots, list):
        return [root]
    roots: list[Path] = []
    for raw in [root, *raw_roots]:
        if isinstance(raw, str | Path):
            roots.append(Path(raw).expanduser().resolve(strict=False))
    return roots or [root]


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False



def _path_has_file_suffix(path: Path) -> bool:
    suffix = path.suffix.lower()
    if not suffix:
        return False
    if suffix in _PRODUCT_TARGET_SUFFIXES:
        return True
    return len(suffix) > 1 and suffix[1:].replace(".", "").replace("_", "").replace("+", "").replace("-", "").isalnum()
