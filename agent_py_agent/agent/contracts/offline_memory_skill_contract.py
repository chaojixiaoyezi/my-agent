# LLM: Offline memory/skill contracts keep durable learning scoped, verified, and safe.
# 模块用途: 校验长期记忆写入、记忆使用和 skill 自学习 manifest 是否满足结构化底层合同。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .contract_validation_recovery import recovery_for_findings

SECRET_FIELD_NAMES = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
}
CURRENT_FACT_USAGES = {"current_fact", "authoritative_fact", "current_context"}


# LLM: OfflineMemorySkillValidation reports durable-memory and skill-manifest findings.
# 类用途: 返回 memory/skill 合同是否通过、错误码和逐项结构化 finding。
@dataclass(frozen=True)
class OfflineMemorySkillValidation:
    ok: bool
    error_codes: tuple[str, ...]
    findings: tuple[dict[str, object], ...]
    recovery: dict[str, object] | None = None


# LLM: validate_memory_skill_contract checks structured memory and skill records in one fixture.
# 函数用途: 校验 memory_writes、memory_uses、skill_manifests 和 tool_manifest 之间的合同一致性。
def validate_memory_skill_contract(contract: dict[str, Any]) -> OfflineMemorySkillValidation:
    findings: list[dict[str, object]] = []
    _validate_memory_writes(_record_list(contract.get("memory_writes")), findings)
    _validate_memory_uses(
        _record_list(contract.get("memory_records")),
        _record_list(contract.get("memory_uses")),
        findings,
    )
    _validate_memory_retrievals(_record_list(contract.get("memory_retrievals")), findings)
    _validate_skill_manifests(
        _record_list(contract.get("skill_manifests")),
        _section(contract.get("tool_manifest")),
        findings,
    )
    return OfflineMemorySkillValidation(
        ok=not findings,
        error_codes=tuple(dict.fromkeys(_text(item.get("code")) for item in findings)),
        findings=tuple(findings),
        recovery=recovery_for_findings("offline_memory_skill", findings),
    )


# LLM: _validate_memory_writes requires durable memories to be scoped and evidence-backed.
# 函数用途: 长期记忆写入必须有 namespace/owner_type 和 validation.ok/evidence_refs，并扫描敏感字段。
def _validate_memory_writes(records: tuple[dict[str, Any], ...], findings: list[dict[str, object]]) -> None:
    for record in records:
        memory_ref = _text(record.get("memory_ref"))
        if not _valid_scope(_section(record.get("scope"))):
            findings.append(_finding("MEMORY_SCOPE_MISSING", {"memory_ref": memory_ref}))
        if not _valid_validation(_section(record.get("validation"))):
            findings.append(_finding("MEMORY_VALIDATION_MISSING", {"memory_ref": memory_ref}))
        for field_path in _secret_field_paths(record):
            findings.append(
                _finding(
                    "MEMORY_SECRET_FIELD",
                    {"memory_ref": memory_ref, "field_path": field_path},
                )
            )


# LLM: _validate_memory_uses rejects stale memory as a current fact while allowing historical citation.
# 函数用途: 以 current_fact 等用法读取记忆时，引用的 memory_record 不允许 is_stale=true。
def _validate_memory_uses(
    records: tuple[dict[str, Any], ...],
    uses: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    records_by_ref = {_text(record.get("memory_ref")): record for record in records if _text(record.get("memory_ref"))}
    for use in uses:
        memory_ref = _text(use.get("memory_ref"))
        usage = _text(use.get("usage")).lower()
        record = records_by_ref.get(memory_ref, {})
        if usage in CURRENT_FACT_USAGES and record.get("is_stale") is True:
            findings.append(_finding("MEMORY_FACT_STALE", {"memory_ref": memory_ref, "usage": usage}))


# LLM: _validate_memory_retrievals checks recall results against structured expected/irrelevant refs.
# 函数用途: memory_retrievals 可声明 expected_refs/irrelevant_refs，用于离线测试召回准确性。
def _validate_memory_retrievals(
    retrievals: tuple[dict[str, Any], ...],
    findings: list[dict[str, object]],
) -> None:
    for retrieval in retrievals:
        query_ref = _text(retrieval.get("query_ref"))
        selected = set(_string_list(retrieval.get("selected_refs")))
        expected = set(_string_list(retrieval.get("expected_refs")))
        irrelevant = set(_string_list(retrieval.get("irrelevant_refs")))
        missing = sorted(expected - selected)
        noisy = sorted(selected & irrelevant)
        if missing:
            findings.append(_finding("MEMORY_RECALL_EXPECTED_MISSING", {"query_ref": query_ref, "missing_refs": missing}))
        if noisy:
            findings.append(_finding("MEMORY_RECALL_IRRELEVANT_SELECTED", {"query_ref": query_ref, "irrelevant_refs": noisy}))


# LLM: _validate_skill_manifests checks machine-readable skill activation, inputs, safety, and tools.
# 函数用途: skill 自学习结果必须有 trigger、input_contract、干净 safety_scan，并且工具在 tool_manifest 可见。
def _validate_skill_manifests(
    manifests: tuple[dict[str, Any], ...],
    tool_manifest: dict[str, Any],
    findings: list[dict[str, object]],
) -> None:
    visible_tools = set(_string_list(tool_manifest.get("visible_tools")))
    for manifest in manifests:
        skill_id = _text(manifest.get("skill_id") or manifest.get("name"))
        if not _section_non_empty(manifest.get("trigger")):
            findings.append(_finding("SKILL_TRIGGER_MISSING", {"skill_id": skill_id}))
        if not _section_non_empty(manifest.get("input_contract")):
            findings.append(_finding("SKILL_INPUT_CONTRACT_MISSING", {"skill_id": skill_id}))
        _validate_skill_safety(manifest, skill_id, findings)
        _validate_skill_tools(manifest, visible_tools, skill_id, findings)


# LLM: _validate_skill_safety requires an explicit clean safety scan before promotion.
# 函数用途: safety_scan.ok 必须为 true 且 finding_codes 为空。
def _validate_skill_safety(
    manifest: dict[str, Any],
    skill_id: str,
    findings: list[dict[str, object]],
) -> None:
    safety_scan = _section(manifest.get("safety_scan"))
    finding_codes = _string_list(safety_scan.get("finding_codes"))
    if safety_scan.get("ok") is not True or finding_codes:
        findings.append(
            _finding(
                "SKILL_SAFETY_FINDING",
                {"skill_id": skill_id, "finding_codes": finding_codes},
            )
        )


# LLM: _validate_skill_tools binds skill allowed_tools to the current visible tool manifest.
# 函数用途: skill 声明的 allowed_tools 必须都在 visible_tools 中，不能靠 prompt 说可用。
def _validate_skill_tools(
    manifest: dict[str, Any],
    visible_tools: set[str],
    skill_id: str,
    findings: list[dict[str, object]],
) -> None:
    allowed_tools = _string_list(manifest.get("allowed_tools"))
    missing_tools = [tool for tool in allowed_tools if tool not in visible_tools]
    if missing_tools:
        findings.append(
            _finding(
                "SKILL_TOOL_NOT_VISIBLE",
                {"skill_id": skill_id, "missing_tools": missing_tools},
            )
        )


# LLM: _valid_scope accepts scoped memory only when namespace and owner_type are explicit.
# 函数用途: 判断 memory scope 是否有 namespace 和 owner_type 两个机器字段。
def _valid_scope(scope: dict[str, Any]) -> bool:
    return bool(_text(scope.get("namespace")) and _text(scope.get("owner_type")))


# LLM: _valid_validation accepts memory writes only after evidence-backed validation.
# 函数用途: 判断 validation.ok 是否为 true 且 evidence_refs 非空。
def _valid_validation(validation: dict[str, Any]) -> bool:
    return validation.get("ok") is True and bool(_string_list(validation.get("evidence_refs")))


# LLM: _secret_field_paths recursively scans structured keys, not free-form memory content.
# 函数用途: 递归找出敏感字段名路径，用于阻止 secret 写入长期记忆。
def _secret_field_paths(value: object, prefix: str = "") -> tuple[str, ...]:
    paths: list[str] = []
    stack: list[tuple[str, object]] = [(prefix, value)]
    while stack:
        current_prefix, current = stack.pop()
        if isinstance(current, dict):
            paths.extend(_dict_secret_field_paths(current_prefix, current, stack))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend(_indexed_children(current_prefix, current))
    return tuple(paths)


# LLM: _dict_secret_field_paths records sensitive keys and pushes child values onto the scan stack.
# 函数用途: 处理一层 dict，避免 _secret_field_paths 出现深层嵌套。
def _dict_secret_field_paths(
    prefix: str,
    value: dict[object, object],
    stack: list[tuple[str, object]],
) -> list[str]:
    paths: list[str] = []
    for key, child in value.items():
        key_text = _text(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if key_text.lower() in SECRET_FIELD_NAMES:
            paths.append(path)
        stack.append((path, child))
    return paths


# LLM: _indexed_children returns list children with stable path suffixes.
# 函数用途: 将 list/tuple 子项转成扫描栈条目。
def _indexed_children(prefix: str, value: list[object] | tuple[object, ...]) -> list[tuple[str, object]]:
    return [
        (f"{prefix}[{index}]" if prefix else f"[{index}]", child)
        for index, child in enumerate(value)
    ]


# LLM: _record_list normalizes arrays of contract records.
# 函数用途: 只接受 dict 列表，忽略无结构项。
def _record_list(value: object) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


# LLM: _section_non_empty checks nested dict sections are present and contain non-empty values.
# 函数用途: 用于 trigger/input_contract 等 manifest section 的空值判断。
def _section_non_empty(value: object) -> bool:
    section = _section(value)
    return bool(section) and any(_present(item) for item in section.values())


# LLM: _present treats scalar and list fields as present only when non-empty.
# 函数用途: 判断结构字段是否有值。
def _present(value: object) -> bool:
    if isinstance(value, (list, tuple, set)):
        return bool(_string_list(value))
    if isinstance(value, dict):
        return _section_non_empty(value)
    return bool(_text(value))


# LLM: _finding creates compact machine findings without prose parsing.
# 函数用途: 生成 code 和额外结构字段。
def _finding(code: str, extra: dict[str, object] | None = None) -> dict[str, object]:
    return {"code": code, **(extra or {})}


# LLM: _section normalizes nested dict sections.
# 函数用途: 非 dict 字段按空 section 处理。
def _section(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# LLM: _string_list normalizes list-like fields without parsing embedded prose.
# 函数用途: 把结构化数组规整成去空字符串列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value for text in [_text(item)] if text]


# LLM: _text normalizes optional scalar values for exact comparisons.
# 函数用途: 把 None 或标量转成去空白字符串；不解析自然语言含义。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["OfflineMemorySkillValidation", "validate_memory_skill_contract"]
