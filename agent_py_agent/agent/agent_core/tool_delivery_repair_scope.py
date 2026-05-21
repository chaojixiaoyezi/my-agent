# LLM: Delivery repair scope matching keeps stale closeout reports from leaking across runs.
# 模块用途: 按当前 delivery_contract 匹配 closeout 的 case_id 和产物 refs，再决定旧修复状态是否仍有效。

from __future__ import annotations


# LLM: report_matches_current_contract validates closeout scope without reading prompt prose.
# 函数用途: 用 case_id 和结构化产物 refs 判断 closeout 是否属于当前 delivery_contract。
def report_matches_current_contract(report: dict[str, object], current_contract: object | None) -> bool:
    if not isinstance(current_contract, dict) or not current_contract:
        return False
    report_case = str(report.get("case_id") or "").strip()
    contract_case = str(current_contract.get("case_id") or "").strip()
    if report_case and contract_case:
        return report_case == contract_case
    if report_case or contract_case:
        return _refs_overlap(report_scope_refs(report), contract_scope_refs(current_contract))
    return _refs_overlap(report_scope_refs(report), contract_scope_refs(current_contract))


# LLM: contract_scope_refs extracts machine refs from the active delivery contract only.
# 函数用途: 汇总 artifact、bootstrap 和 staging 路径，用来和 closeout 报告做范围匹配。
def contract_scope_refs(contract: dict[str, object]) -> set[str]:
    refs: set[str] = set()
    for item in _dict_items(contract.get("artifacts")):
        refs.update(_path_values(item, ("preferred_path", "path")))
        refs.update(_staging_refs(_dict_value(item, "validation_contract").get("staging_contract")))
    for target in _dict_items(_dict_value(contract, "bootstrap_contract").get("materialization_targets")):
        refs.update(_path_values(target, ("workspace_relative_path", "path")))
    return _normalized_refs(refs)


# LLM: report_scope_refs extracts artifact and recovery refs from closeout reports.
# 函数用途: 只读 closeout 的结构化路径字段，不解析最终回复文本。
def report_scope_refs(report: dict[str, object]) -> set[str]:
    refs: set[str] = set()
    for item in _dict_items(report.get("artifacts")):
        refs.update(_path_values(item, ("path", "artifact_path")))
    progress = _dict_value(report, "delivery_progress")
    for action in _dict_items(progress.get("recovery_actions")):
        refs.update(_path_values(action, ("artifact_path", "checkpoint_ref", "output_ref", "source_ref")))
        refs.update(str(item) for item in action.get("repair_targets", []) if isinstance(item, str))
    for target in _dict_items(progress.get("pending_materialization_targets")):
        refs.update(_path_values(target, ("workspace_relative_path", "path")))
    return _normalized_refs(refs)


# LLM: _staging_refs extracts staged builder/checkpoint refs from validation contracts.
# 函数用途: 支持 xlsx/pdf 等阶段产物合同按 source/workbook/checkpoint 绑定 closeout。
def _staging_refs(staging: object) -> set[str]:
    if not isinstance(staging, dict):
        return set()
    refs = _path_values(staging, ("source_json_ref", "workbook_ref", "pdf_ref", "output_ref"))
    refs.update(str(item) for item in staging.get("checkpoint_refs", []) if isinstance(item, str))
    return refs


# LLM: _dict_items narrows list entries to dicts.
# 函数用途: 让调用方用平铺循环处理结构化列表，避免层层嵌套判断。
def _dict_items(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


# LLM: _dict_value returns a mapping-valued field.
# 函数用途: 字段不是 dict 时返回空对象，保持路径提取流程稳定。
def _dict_value(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


# LLM: _path_values reads path-like fields from one structured dict.
# 函数用途: 统一提取路径字段，字段缺失或非字符串时忽略。
def _path_values(payload: dict[str, object], keys: tuple[str, ...]) -> set[str]:
    return {str(value) for key in keys for value in [payload.get(key)] if isinstance(value, str) and value.strip()}


# LLM: _refs_overlap handles absolute-vs-relative refs without using prose.
# 函数用途: 判断两组机器路径是否指向同一产物范围，支持 closeout 里的绝对路径和合同里的相对路径。
def _refs_overlap(left: set[str], right: set[str]) -> bool:
    return any(
        left_ref == right_ref or left_ref.endswith(f"/{right_ref}") or right_ref.endswith(f"/{left_ref}")
        for left_ref in left
        for right_ref in right
    )


# LLM: _normalized_refs makes path matching platform-neutral and stable.
# 函数用途: 归一化路径分隔符和首尾斜杠，避免同一 ref 因格式不同匹配失败。
def _normalized_refs(values: set[str]) -> set[str]:
    return {ref for value in values for ref in [_normalize_ref(value)] if ref}


# LLM: _normalize_ref normalizes one path-like string.
# 函数用途: 统一斜杠和首尾空白，供 refs overlap 使用。
def _normalize_ref(value: str) -> str:
    return value.replace("\\", "/").strip().strip("/")


__all__ = [
    "contract_scope_refs",
    "report_matches_current_contract",
    "report_scope_refs",
]
