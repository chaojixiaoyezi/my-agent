# LLM: Declared output acceptance helpers; keep these path checks scoped to machine-declared refs.
# 模块用途: 验收 output_files/output_refs 是否真实落地，并提供缺失路径的可返工提示。

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..context_bundle_file_roots import is_contract_file_path
from ..reports import AcceptanceReviewFinding

if TYPE_CHECKING:
    from ..models import SubAgentTask


def _dict_list(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "")]
    return [str(value)]

# LLM: declared_output_refs_finding checks machine-declared deliverable paths at acceptance time.
# 函数用途: output_files/output_refs 是用户交付目标；runner 私有目录里有同名文件也不能冒充目标路径。
def declared_output_refs_finding(
    manager: Any,
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
) -> AcceptanceReviewFinding:
    declared = _declared_output_refs(task)
    if not declared:
        return AcceptanceReviewFinding(
            name="declared_output_refs_exist",
            ok=True,
            severity="P1",
            message="任务未声明 output_files/output_refs。",
            evidence_path=getattr(task, "output_json", ""),
            created_at=created_at,
        )
    missing = [ref for ref in declared if not _declared_output_exists(manager, task, ref)]
    return AcceptanceReviewFinding(
        name="declared_output_refs_exist",
        ok=not missing,
        severity="P1",
        message=_declared_output_message(missing, _actual_artifact_refs(task, output)),
        evidence_path=_declared_output_evidence_path(manager, task, declared[0]),
        created_at=created_at,
    )


def _declared_output_refs(task: SubAgentTask) -> list[str]:
    attrs = getattr(task, "attributes", {}) or {}
    refs: list[str] = []
    if isinstance(attrs, dict):
        for field in ("output_files", "output_refs", "artifact_refs"):
            refs.extend(_artifact_refs_from_output_value(attrs.get(field)))
    refs.extend(_output_scope_refs_from_context_packs(getattr(task, "context_packs", []) or []))
    return _unique_strings(ref for ref in refs if _looks_like_local_output_path(ref))


# LLM: output_refs can be logical result keys instead of filesystem paths.
# 函数用途: 只有绝对路径、带目录的相对路径或具体文件名才参加文件存在验收；source_file 这类结果键不硬查。
def _looks_like_local_output_path(ref: object) -> bool:
    text = str(ref or "").strip().replace("\\", "/")
    if not text or "://" in text:
        return False
    path = Path(text)
    return path.is_absolute() or "/" in text or is_contract_file_path(path)


def _output_scope_refs_from_context_packs(packs: object) -> list[str]:
    refs: list[str] = []
    for pack in packs if isinstance(packs, list) else []:
        if not isinstance(pack, dict):
            continue
        contract = pack.get("contract")
        if not isinstance(contract, dict) or not _is_output_scope_contract(contract):
            continue
        refs.extend(_artifact_refs_from_output_value(contract.get("scope_refs")))
        refs.extend(_artifact_refs_from_output_value(contract.get("target_artifact_refs")))
    return refs


def _is_output_scope_contract(contract: dict[str, object]) -> bool:
    kind = str(contract.get("kind") or "").strip()
    key = str(contract.get("idempotency_key") or contract.get("key") or "").strip()
    return kind == "system_derived_output_scope" or key.endswith(".output_refs")


def _declared_output_exists(manager: Any, task: SubAgentTask, ref: str) -> bool:
    return any(path.exists() for path in _declared_output_candidates(manager, task, ref))


def _declared_output_candidates(manager: Any, task: SubAgentTask, ref: str) -> list[Path]:
    text = str(ref or "").strip()
    if not text or "://" in text:
        return []
    path = Path(text).expanduser()
    if path.is_absolute():
        return [path.resolve(strict=False)]
    roots = _declared_output_roots(manager, task)
    return [(root / path).resolve(strict=False) for root in roots]


def _declared_output_roots(manager: Any, task: SubAgentTask) -> list[Path]:
    roots: list[Path] = []
    for raw in _declared_root_values(manager, task):
        _append_declared_root(roots, raw)
    return roots


def _declared_root_values(manager: Any, task: SubAgentTask) -> list[object]:
    return [
        getattr(manager, "workspace_root", None),
        *(getattr(manager, "workspace_roots", []) or []),
        *(getattr(task, "allowed_write_roots", []) or []),
    ]


def _append_declared_root(roots: list[Path], raw: object) -> None:
    if not isinstance(raw, str | Path) or not str(raw).strip():
        return
    root = Path(raw).expanduser().resolve(strict=False)
    if root not in roots:
        roots.append(root)


def _declared_output_evidence_path(manager: Any, task: SubAgentTask, ref: str) -> str:
    candidates = _declared_output_candidates(manager, task, ref)
    return str(candidates[0]) if candidates else getattr(task, "output_json", "")


def _actual_artifact_refs(task: SubAgentTask, output: dict[str, object]) -> list[str]:
    refs: list[str] = []
    refs.extend(_string_list(getattr(task, "artifact_refs", [])))
    refs.extend(_artifact_refs_from_output_value(output.get("artifact_path")))
    refs.extend(_artifact_refs_from_output_value(output.get("artifacts")))
    refs.extend(_artifact_refs_from_output_value(output.get("files_modified")))
    for packet in _dict_list(output.get("evidence_packets", [])):
        refs.extend(_string_list(packet.get("artifact_refs")))
    for packet in getattr(task, "evidence_packets", []) or []:
        refs.extend(_string_list(getattr(packet, "artifact_refs", [])))
    return _unique_strings(refs)


def _artifact_refs_from_output_value(value: object) -> list[str]:
    structured = _structured_ref_carrier(value)
    if structured is not value:
        return _artifact_refs_from_output_value(structured)
    if isinstance(value, str):
        return _string_list(value)
    if isinstance(value, list):
        return [item for raw in value for item in _artifact_refs_from_output_value(raw)]
    if isinstance(value, dict):
        refs: list[str] = []
        for field in ("path", "file", "file_path", "artifact_path", "ref", "href"):
            refs.extend(_artifact_refs_from_output_value(value.get(field)))
        return refs
    return []


# LLM: _structured_ref_carrier handles legacy dict-string refs without parsing task prose.
# 函数用途: 旧任务可能把 output_refs 对象保存成 "{'output_path': ...}"；这里只解析对象/列表载体再按字段取路径。
def _structured_ref_carrier(value: object) -> object:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            return value
    return parsed if isinstance(parsed, dict | list | tuple) else value


def _declared_output_message(missing: list[str], actual_refs: list[str]) -> str:
    if not missing:
        return "声明的 output_files/output_refs 已在目标路径落地。"
    actual = f"；runner 实际产物: {actual_refs[0]}" if actual_refs else ""
    return f"缺少声明输出路径: {missing[0]}{actual}；请把产物写到声明路径后重新提交验收。"


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _task_ready_or_already_accepted(task: SubAgentTask) -> bool:
    status = str(getattr(task, "status", "") or "").upper()
    verification = str(getattr(task, "verification_status", "") or "").upper()
    return (
        status == "AWAITING_ACCEPTANCE"
        or verification == "NEEDS_ACCEPTANCE"
        or (status == "DONE" and verification == "VERIFIED")
    )
