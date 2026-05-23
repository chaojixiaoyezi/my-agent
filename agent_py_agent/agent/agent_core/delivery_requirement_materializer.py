# LLM: Delivery requirement materializer validates LLM-created machine contracts.
# 模块用途: 把模型从普通用户需求中抽取出的 JSON 合同规整为通用 delivery_contract，不内置任务模板。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..contracts.delivery_contract_doctor import validate_delivery_contract

SCHEMA_VERSION = "delivery_contract.v1"
MATERIALIZER_SCHEMA_VERSION = "delivery_requirement_materializer.v1"


# LLM: build_delivery_requirement_materializer_prompt asks the model for machine fields only.
# 函数用途: 生成入口物化提示；提示用于 LLM 结构化抽取，系统事实仍只信返回 JSON。
def build_delivery_requirement_materializer_prompt(user_prompt: str) -> str:
    return (
        "请把下面的用户需求转换成一个最小 delivery_contract.v1 JSON 对象。\n"
        "只输出 JSON，不要解释，不要写具体执行步骤模板，不要替用户编造来源。\n"
        "产物可以只声明 artifact_id、kind、required、allowed_output_roots；不知道固定路径时不要硬写路径。\n"
        "分析型字段请放入 llm_generated_fields，不要把它们映射到来源 API 的普通 description 字段。\n"
        "可选字段包括 artifacts、delivery_quality_contract、bootstrap_contract。\n"
        "用户需求：\n"
        f"{user_prompt}"
    )


# LLM: materialized_delivery_contract validates a structured materializer result.
# 函数用途: 接收模型/外部 case 给出的结构化 JSON，过滤越界路径和坏字段后返回运行合同。
def materialized_delivery_contract(
    payload: object,
    *,
    workspace_root: Path | None = None,
) -> dict[str, Any]:
    value = _payload_object(payload)
    source_doctor = validate_delivery_contract(value, workspace_root=workspace_root)
    artifacts, findings = _artifact_contracts(value.get("artifacts"), workspace_root)
    contract: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "artifacts": artifacts,
    }
    for key in ("delivery_quality_contract", "bootstrap_contract"):
        if isinstance(value.get(key), dict):
            contract[key] = dict(value[key])
    if findings:
        contract["_preflight_findings"] = findings
    doctor = validate_delivery_contract(contract, workspace_root=workspace_root)
    if doctor.normalized_contract:
        contract = dict(doctor.normalized_contract)
    doctor_findings = [*source_doctor.findings, *doctor.findings]
    if doctor_findings:
        contract["_contract_doctor"] = _doctor_payload(
            {
                **doctor.to_dict(),
                "ok": not any(finding.severity == "hard" for finding in doctor_findings),
                "findings": [finding.to_dict() for finding in doctor_findings],
                "repair_actions": source_doctor.repair_actions or doctor.repair_actions,
                "should_rematerialize": source_doctor.should_rematerialize or doctor.should_rematerialize,
            }
        )
    return contract


# LLM: _payload_object normalizes materializer output without trusting prose.
# 函数用途: 接受 dict 或 JSON 字符串，解析失败时返回空结构供预检继续报告。
def _payload_object(payload: object) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


# LLM: _artifact_contracts extracts artifact contracts from structured JSON only.
# 函数用途: 过滤坏 artifact 条目，保留可运行产物合同和预检 finding。
def _artifact_contracts(value: object, workspace_root: Path | None) -> tuple[list[dict[str, Any]], list[dict[str, object]]]:
    artifacts: list[dict[str, Any]] = []
    findings: list[dict[str, object]] = []
    if not isinstance(value, list):
        return artifacts, findings
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            findings.append(_finding("DELIVERY_MATERIALIZER_ARTIFACT_INVALID", f"artifacts[{index}]"))
            continue
        normalized = _artifact_contract(item)
        path_finding = _path_finding(normalized, workspace_root, index)
        if path_finding:
            findings.append(path_finding)
            continue
        artifacts.append(normalized)
    return artifacts, findings


# LLM: _artifact_contract keeps only generic artifact fields from a materialized result.
# 函数用途: 丢弃执行步骤模板等非机器合同字段，补 required 默认值并规整 kind/root。
def _artifact_contract(item: dict[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "allowed_output_roots",
        "artifact_id",
        "kind",
        "path",
        "preferred_path",
        "required",
        "search_roots",
        "validation_contract",
    }
    result = {key: item[key] for key in allowed_keys if key in item}
    if "required" not in result:
        result["required"] = True
    if "kind" in result:
        result["kind"] = str(result["kind"]).strip().lower()
    if isinstance(result.get("allowed_output_roots"), list):
        result["allowed_output_roots"] = [str(value).strip() for value in result["allowed_output_roots"] if str(value).strip()]
    return result


# LLM: _path_finding validates materialized paths against the task workspace.
# 函数用途: 检查 path/preferred_path 是否越界，越界时返回结构化 warning。
def _path_finding(item: dict[str, Any], workspace_root: Path | None, index: int) -> dict[str, object] | None:
    if workspace_root is None:
        return None
    for key in ("preferred_path", "path"):
        raw = str(item.get(key) or "").strip()
        if not raw:
            continue
        root = Path(workspace_root).resolve(strict=False)
        candidate = Path(raw).expanduser()
        path = candidate.resolve(strict=False) if candidate.is_absolute() else (root / candidate).resolve(strict=False)
        try:
            path.relative_to(root)
        except ValueError:
            return _finding(
                "DELIVERY_MATERIALIZER_ARTIFACT_PATH_OUTSIDE_WORKSPACE",
                f"artifacts[{index}].{key}",
                value=raw,
            )
    return None


# LLM: _finding emits materializer preflight diagnostics.
# 函数用途: 构造 code/location/value finding，供入口层返工或观测使用。
def _finding(code: str, location: str, *, value: str = "") -> dict[str, object]:
    return {
        "code": code,
        "severity": "warning",
        "location": location,
        "message": code.lower(),
        "value": value,
    }


# LLM: _doctor_payload keeps materialized contracts from recursively embedding themselves.
# 函数用途: 只保留 Doctor 结论和返工动作，不把 normalized_contract 再塞回合同自身。
def _doctor_payload(report: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": report.get("schema_version", ""),
        "ok": report.get("ok", False),
        "findings": report.get("findings", []),
        "repair_actions": report.get("repair_actions", []),
        "should_rematerialize": report.get("should_rematerialize", False),
    }


__all__ = [
    "MATERIALIZER_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "build_delivery_requirement_materializer_prompt",
    "materialized_delivery_contract",
]
