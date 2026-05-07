# LLM: Log-analysis module; keep ingest, query, and detector data contracts stable.
# 模块用途: 支撑日志导入、查询、检测、案例和分析报告生成。

from __future__ import annotations

"""Attack-chain draft generation from soft findings."""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from ..models import CaseRecord, EvidenceRef, Finding


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 AttackChainStep 前先核对字段语义、序列化形态和调用方假设。
# 类用途: 承载 AttackChainStep 的字段集合，在模块边界间传递结构化状态和结果。
@dataclass
class AttackChainStep:
    time: str
    stage: str
    action: str
    entities: dict[str, list[str]] = field(default_factory=dict)
    evidence_refs: list[str] = field(default_factory=list)
    basis: str = "finding"
    confidence: float = 0.0

    # LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 to_dict 时同步检查返回值、异常处理和读写副作用。
    # 函数用途: 把 to dict 对应对象转换成字典、JSON 或文本形态，供持久化和输出层复用。
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 build_attack_chain 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 组装 build attack chain 的对象、payload 或展示文本，供报告、CLI 或下游流程消费。
def build_attack_chain(case_or_findings: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[AttackChainStep]:
    findings = sorted(_extract_findings(case_or_findings), key=lambda item: item.window[0] if item.window else "")
    steps: list[AttackChainStep] = []
    for finding in findings:
        stage, action = _stage_for_detector(finding.detector_id)
        steps.append(
            AttackChainStep(
                time=finding.window[0] if finding.window else "",
                stage=stage,
                action=action,
                entities=finding.entities,
                evidence_refs=_ref_ids(finding.evidence_refs),
                confidence=finding.confidence if finding.confidence is not None else finding.risk_score,
            )
        )
    return steps


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 lateral_movement_signs 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 lateral movement signs 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def lateral_movement_signs(case_or_findings: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[dict[str, Any]]:
    signs: list[dict[str, Any]] = []
    for finding in _extract_findings(case_or_findings):
        targets = _entity_values(finding.entities, "victim_ip", "dst_ip", "host")
        if finding.detector_id in {"bruteforce_then_success", "vpn_new_geo_login"}:
            signs.append(
                {
                    "kind": "identity_to_asset_access",
                    "summary": finding.hypothesis,
                    "entities": finding.entities,
                    "confidence": finding.confidence if finding.confidence is not None else finding.risk_score,
                    "evidence_refs": _ref_ids(finding.evidence_refs),
                }
            )
        if len(targets) >= 3:
            signs.append(
                {
                    "kind": "multi_target_activity",
                    "summary": "The finding touches multiple target assets in one window.",
                    "targets": targets,
                    "confidence": min(0.75, finding.confidence if finding.confidence is not None else finding.risk_score),
                    "evidence_refs": _ref_ids(finding.evidence_refs),
                }
            )
    return signs


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _stage_for_detector 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 基于规则或事件字段计算 stage for detector 的判定结果，避免把推测当作事实写入。
def _stage_for_detector(detector_id: str) -> tuple[str, str]:
    mapping = {
        "waf_attack_success_candidate": ("initial_access", "Web exploit candidate with post-alert server behavior."),
        "web_to_process_anomaly": ("execution", "Web service launched an unusual child process."),
        "vpn_new_geo_login": ("initial_access", "VPN login used novel or unusual source context."),
        "bruteforce_then_success": ("credential_access", "Repeated failed authentication was followed by success."),
        "rare_egress_after_alert": ("command_and_control", "Alerted asset reached a rare external destination."),
        "multi_source_weak_signal": ("correlation", "Multiple weak signals overlapped on one entity."),
    }
    return mapping.get(detector_id, ("unknown", detector_id or "unknown finding"))


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _extract_findings 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 提取、合并或规范化 extract findings 涉及的字段，让后续匹配和存储使用同一形态。
def _extract_findings(value: CaseRecord | Mapping[str, Any] | Sequence[Finding | Mapping[str, Any]]) -> list[Finding]:
    if isinstance(value, CaseRecord):
        attributes = value.attributes if isinstance(value.attributes, Mapping) else {}
        return [Finding.from_dict(item) for item in attributes.get("finding_summaries", []) if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        if "findings" in value:
            return [Finding.from_dict(item) for item in value.get("findings", [])]
        attributes = value.get("attributes")
        if isinstance(attributes, Mapping) and "finding_summaries" in attributes:
            return [Finding.from_dict(item) for item in attributes.get("finding_summaries", [])]
        return [Finding.from_dict(value)]
    return [item if isinstance(item, Finding) else Finding.from_dict(item) for item in value]


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _entity_values 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 完成 entity values 在当前模块中的核心转换或协调步骤，衔接 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源。
def _entity_values(entities: Mapping[str, Sequence[Any]], *keys: str) -> list[str]:
    result: list[str] = []
    for key in keys:
        _append_entity_values(result, entities.get(key, []))
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _append_entity_values 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 写入或登记 append entity values 相关记录，集中处理目标路径、格式化和状态更新。
def _append_entity_values(result: list[str], values: Sequence[Any]) -> None:
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _ref_ids 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 ref ids 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _ref_ids(refs: Sequence[Any]) -> list[str]:
    result: list[str] = []
    for ref in refs:
        result.append(_ref_id(ref))
    return result


# LLM: 日志分析检测逻辑以规范化事件、规则和实体字段为事实来源；修改 _ref_id 时同步检查返回值、异常处理和读写副作用。
# 函数用途: 计算 ref id 的稳定值、时间窗口或标识符，供去重、排序和检索使用。
def _ref_id(ref: Any) -> str:
    if isinstance(ref, EvidenceRef):
        return ref.evidence_id
    if isinstance(ref, Mapping):
        return str(ref.get("evidence_id") or ref.get("raw_ref") or ref)
    return str(ref)


__all__ = ["AttackChainStep", "build_attack_chain", "lateral_movement_signs"]
