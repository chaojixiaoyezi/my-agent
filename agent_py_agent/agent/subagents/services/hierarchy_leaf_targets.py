# LLM: Leaf target dedupe keeps concrete output-file checks out of broad hierarchy guards.
# 模块用途: 根据已完成 leaf 的 artifact 路径引用阻断同父级重复写同一目标文件。

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import SubAgentTask
from .repair_contract_identity import repair_contract_identity_from_context_packs


# LLM: LeafTargetDedupeRequest bundles all state needed for same-parent leaf output dedupe.
# 类用途: 集中保存 manager、parent、调度请求和 leaf 判断函数，避免去重入口散参扩张。
@dataclass(frozen=True)
class LeafTargetDedupeRequest:
    manager: Any
    parent: SubAgentTask
    schedule_request: Any
    leaf_like: Callable[[Any], bool]


# LLM: duplicate_verified_leaf_target_warnings audits repeated output targets without blocking collaboration.
# 函数用途: 同父级已有 DONE/VERIFIED leaf 写过同一目标文件时，返回审计提示；不阻断后续修复或协作写入。
def duplicate_verified_leaf_target_warnings(request: LeafTargetDedupeRequest) -> list[str]:
    seen_targets = _completed_leaf_targets(request)
    if not seen_targets:
        return []
    warnings: list[str] = []
    for spec in request.schedule_request.child_specs:
        warning = _duplicate_target_warning(spec, seen_targets, request.leaf_like)
        if warning and warning not in warnings:
            warnings.append(warning)
    return warnings


# LLM: duplicate_verified_leaf_target_reason preserves the legacy query shape for tests and callers.
# 函数用途: 兼容旧调用方需要单个原因字符串的场景；调度主路径只把它作为 warning 使用。
def duplicate_verified_leaf_target_reason(request: LeafTargetDedupeRequest) -> str:
    warnings = duplicate_verified_leaf_target_warnings(request)
    return warnings[0] if warnings else ""


# LLM: _duplicate_target_warning keeps the public warning collector shallow for size guards.
# 函数用途: 判断一个待建 leaf 是否重复已完成产物；修复类 leaf 和非 leaf 不返回 warning。
def _duplicate_target_warning(item: Any, seen_targets: list[set[str]], leaf_like: Callable[[Any], bool]) -> str:
    if not leaf_like(item) or _is_explicit_repair_leaf(item):
        return ""
    duplicate = _first_overlapping_target(_child_target_tokens(item), seen_targets)
    return f"duplicate_leaf_target:{duplicate}" if duplicate else ""


# LLM: _completed_leaf_targets reads only direct child metadata and output artifact path refs.
# 函数用途: 收集已验证 leaf 的产物文件名；不读取 artifact 正文，只读取 output.json 的结构化路径。
def _completed_leaf_targets(request: LeafTargetDedupeRequest) -> list[set[str]]:
    targets: list[set[str]] = []
    for child_id in request.parent.child_ids:
        try:
            child = request.manager.load(child_id)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            continue
        if not _is_verified_leaf(child, request.leaf_like):
            continue
        child_targets = task_actual_target_tokens(child)
        if child_targets:
            targets.append(child_targets)
    return targets


# LLM: _is_verified_leaf narrows dedupe to finished implementation leaves.
# 函数用途: 只有已 DONE/VERIFIED 的 leaf/worker 才作为重复产物依据，未完成 sibling 不会挡住正常拆分。
def _is_verified_leaf(item: Any, leaf_like: Callable[[Any], bool]) -> bool:
    return (
        leaf_like(item)
        and str(getattr(item, "status", "") or "").upper() == "DONE"
        and str(getattr(item, "verification_status", "") or "").upper() == "VERIFIED"
    )


# LLM: _child_target_tokens extracts explicit output filenames from structured roots and output refs.
# 函数用途: 识别 extra_write_roots/output.json 里的具体目标文件；不从自然语言 goal 猜产物 ownership。
def _child_target_tokens(item: Any) -> set[str]:
    targets = {
        token
        for root in getattr(item, "extra_write_roots", []) or []
        for token in _target_tokens_from_text(str(root or ""))
    }
    targets.update(_target_tokens_from_attributes(getattr(item, "attributes", {}) or {}))
    targets.update(_target_tokens_from_output_json(getattr(item, "output_json", "") or ""))
    return targets


# LLM: task_actual_target_tokens prefers structured runner refs before natural-language task goals.
# 函数用途: 返回任务实际触碰的产物文件名；给看板、收口和去重共享，避免“把 index.html 改成 index1.html”误认旧目标。
def task_actual_target_tokens(item: Any) -> set[str]:
    attribute_targets = _target_tokens_from_attributes(getattr(item, "attributes", {}) or {})
    if attribute_targets:
        return attribute_targets
    output_targets = _target_tokens_from_output_json(getattr(item, "output_json", "") or "")
    if output_targets:
        return output_targets
    result_targets = _target_tokens_from_result_json(_task_result_text(item))
    if result_targets:
        return result_targets
    return _child_target_tokens(item)


# LLM: _target_tokens_from_attributes reads machine output refs for target ownership.
# 函数用途: 从 attributes.output_refs/output_files/artifact_refs 提取目标文件名；不解析 goal 文本。
def _target_tokens_from_attributes(attributes: dict[str, object]) -> set[str]:
    if not isinstance(attributes, dict):
        return set()
    targets: set[str] = set()
    for field in ("output_refs", "output_files", "artifact_refs", "required_read_paths"):
        targets.update(_target_tokens_from_output_values(attributes.get(field)))
    return targets


# LLM: _is_explicit_repair_leaf lets parent coordinators create bounded fixes for known bad artifacts.
# 函数用途: 只通过 repair_contract/context_packs 判断修复任务；不从“修复/fix”等自然语言猜。
def _is_explicit_repair_leaf(item: Any) -> bool:
    return bool(repair_contract_identity_from_context_packs(getattr(item, "context_packs", [])) or _identity_has_repair_marker(item))


# LLM: _identity_has_repair_marker treats explicit repair names as bounded repair tasks.
# 函数用途: app-js-repair-worker 这种结构化名字不触发重复产物 warning。
def _identity_has_repair_marker(item: Any) -> bool:
    text = f"{getattr(item, 'role', '')} {getattr(item, 'agent_name', '')}".lower().replace("_", "-")
    return "repair" in text


# LLM: _target_tokens_from_output_json reads artifact path refs without expanding artifact contents.
# 函数用途: 从 output.json 的 artifacts 里提取文件名，让已完成 leaf 的真实产物参与去重。
def _target_tokens_from_output_json(output_json: str) -> set[str]:
    path = Path(str(output_json or ""))
    if not output_json or not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    targets.update(_target_tokens_from_output_values(payload.get("artifact_path")))
    targets.update(_target_tokens_from_output_values(payload.get("artifacts")))
    targets.update(_target_tokens_from_output_values(payload.get("files_modified")))
    targets.update(_target_tokens_from_output_values(payload.get("patches")))
    return targets


# LLM: _target_tokens_from_artifact normalizes string or dict artifact path refs.
# 函数用途: 兼容 runner 输出的常见 artifact 形状，只保存文件名 token，不保存正文。
def _target_tokens_from_artifact(artifact: Any) -> set[str]:
    if isinstance(artifact, str):
        return set(_target_tokens_from_text(artifact))
    if not isinstance(artifact, dict):
        return set()
    values = [
        artifact.get(key)
        for key in ("path", "file", "file_path", "artifact_path", "ref", "href")
    ]
    return {
        token
        for value in values
        for token in _target_tokens_from_text(str(value or ""))
    }


# LLM: _target_tokens_from_result_json recovers refs from the runner's explicit SUBAGENT_RESULT block.
# 函数用途: output.json 未带 files_modified 时，从 task.result 的结构化结果补回产物 refs，仍不读取产物正文。
def _target_tokens_from_result_json(result_text: str) -> set[str]:
    text = str(result_text or "")
    import re

    match = re.search(r"\[SUBAGENT_RESULT\]\s*(\{.*\})\s*\[/SUBAGENT_RESULT\]", text, re.DOTALL)
    if not match:
        return set()
    try:
        payload = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    targets.update(_target_tokens_from_output_values(payload.get("artifact_path")))
    targets.update(_target_tokens_from_output_values(payload.get("artifacts")))
    targets.update(_target_tokens_from_output_values(payload.get("files_modified")))
    targets.update(_target_tokens_from_output_values(payload.get("patches")))
    return targets


# LLM: _target_tokens_from_output_values supports the small structured ref shapes models commonly emit.
# 函数用途: 递归兼容 string/list/dict 形式的 path/file/ref 字段，只提取文件名 token。
def _target_tokens_from_output_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_target_tokens_from_text(value))
    if isinstance(value, dict):
        values = [
            value.get(key)
            for key in ("path", "file", "file_path", "artifact_path", "ref", "href")
        ]
        return {token for item in values for token in _target_tokens_from_output_values(item)}
    if isinstance(value, list):
        return {token for item in value for token in _target_tokens_from_output_values(item)}
    return set()


# LLM: _task_result_text avoids adding result text to board rows while still letting status logic recover refs.
# 函数用途: 优先用 task.result；看板条目只有 task_dir 时，读取小型 task.json 里的 result 字段作为回退。
def _task_result_text(item: Any) -> str:
    direct = str(getattr(item, "result", "") or "")
    if direct:
        return direct
    task_dir = Path(str(getattr(item, "task_dir", "") or ""))
    if not str(task_dir):
        return ""
    try:
        payload = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    return str(payload.get("result") or "")


# LLM: _target_tokens_from_text keeps dedupe tied to concrete local files.
# 函数用途: 从文本中提取常见代码/文档/网页文件名；跳过目录根和 URL。
def _target_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _target_file_match_candidates(text):
        token = Path(match.strip("`'\" ,;:，。；：、)]}）】")).name.lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


# LLM: _target_file_match_candidates keeps regex scanning outside the public token normalizer.
# 函数用途: 从输出目标片段中产出文件路径候选，并过滤 URL，降低去重函数复杂度。
def _target_file_match_candidates(text: str) -> list[str]:
    import re

    return [
        match
        for match in re.findall(r"[\w./~:-]+\.(?:html|css|js|ts|tsx|jsx|py|md|json|txt|csv|yaml|yml)", str(text or ""))
        if "://" not in match
    ]


# LLM: _first_overlapping_target keeps duplicate leaf errors deterministic.
# 函数用途: 返回新 leaf 和已完成 leaf 产物文件名的第一个交集。
def _first_overlapping_target(targets: set[str], seen_targets: list[set[str]]) -> str:
    for seen in seen_targets:
        overlap = sorted(targets & seen)
        if overlap:
            return overlap[0]
    return ""
