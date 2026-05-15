# LLM: Semantic checks for subagent context bundles live outside the bundle builder to keep it small.
# 模块用途: 校验 context bundle 有没有丢失用户明确要求的产物文件名，不读取 artifact 正文。

from __future__ import annotations

from typing import Any

from .required_file_terms import required_file_terms_from_text


# LLM: semantic_context_missing_fields catches structured contracts that lost explicit deliverables.
# 函数用途: 从 goal/验收/计划里重新提取轻量文件名，并校验 output_contract 与 task_packet 都完整携带。
def semantic_context_missing_fields(bundle: Any) -> list[str]:
    expected = _bundle_required_file_terms(bundle)
    if not expected:
        return []
    output_contract = getattr(bundle, "output_contract", {})
    output_required = set(_object_string_list(output_contract.get("required_files")))
    packet = getattr(bundle, "task_packet", {})
    packet = packet if isinstance(packet, dict) else {}
    file_contract = packet.get("file_contract") if isinstance(packet.get("file_contract"), dict) else {}
    packet_required = set(_object_string_list(file_contract.get("required_files")))
    missing: list[str] = []
    for filename in expected:
        if filename not in output_required:
            missing.append(f"output_contract.required_files:{filename}")
        if filename not in packet_required:
            missing.append(f"task_packet.file_contract.required_files:{filename}")
    return missing


# LLM: _bundle_required_file_terms uses the same parser as task building for gate self-checks.
# 函数用途: 从已生成 bundle 的轻量文本字段提取明确产物文件名，避免读取 artifact 正文。
def _bundle_required_file_terms(bundle: Any) -> list[str]:
    return _dedupe_file_terms(
        term
        for text in _bundle_file_contract_texts(bundle)
        for term in required_file_terms_from_text(text, extensions=r"py|md|json|ya?ml|txt|ts|tsx|js|jsx|css|html")
    )


# LLM: _bundle_file_contract_texts bounds semantic checking to small persisted task facts.
# 函数用途: 收集 gate 可用的目标、思路、计划和验收文本，不读取产物正文。
def _bundle_file_contract_texts(bundle: Any) -> list[str]:
    values: list[object] = [
        getattr(bundle, "goal", ""),
        getattr(bundle, "thought", ""),
        *(getattr(bundle, "plan", []) or []),
        *(getattr(bundle, "acceptance_checks", []) or []),
    ]
    return [str(value or "") for value in values if str(value or "").strip()]


# LLM: _dedupe_file_terms preserves user-mentioned order for semantic gate filenames.
# 函数用途: 对结构化文件清单去重，避免同一文件从 goal 和验收条件重复出现。
def _dedupe_file_terms(values) -> list[str]:
    terms: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in terms:
            terms.append(text)
    return terms


# LLM: _object_string_list safely reads contract arrays from loose JSON-shaped dicts.
# 函数用途: 把 required_files 这类未知输入规整成字符串列表，过滤空值。
def _object_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for item in value if (text := str(item or "").strip())]
