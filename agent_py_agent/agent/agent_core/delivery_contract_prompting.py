
from __future__ import annotations

import json

from .artifact_locator import artifact_can_be_located

DELIVERY_PREFLIGHT_FINDINGS_KEY = "_preflight_findings"


def render_delivery_contract_section(contract: dict[str, object]) -> str:
    return "\n".join(
        [
            "[tool-system delivery-contract]",
            json.dumps(_model_visible_contract(contract), ensure_ascii=False, sort_keys=True),
            *_bootstrap_guidance_lines(contract),
            *_artifact_guidance_lines(contract),
            *render_recovery_guidance_lines(contract, _artifact_items(contract)),
        ]
    )


def delivery_contract_preflight_findings(contract: dict[str, object]) -> list[dict[str, object]]:
    artifacts = contract.get("artifacts")
    if artifacts is not None and not isinstance(artifacts, list):
        return [
            _preflight_finding(
                "DELIVERY_CONTRACT_ARTIFACTS_INVALID",
                "artifacts",
                "delivery_contract.artifacts must be a list of artifact objects.",
            )
        ]
    findings = _artifact_preflight_findings(artifacts) if isinstance(artifacts, list) else []
    # 收口机器验证 gate(2026-08-15 3×3): verify_commands 声明产物验收命令,
    # 结构不合法 → warning(收口 gate 侧 fail-closed 不验证, 但绝不误放完成)。
    verify = contract.get("verify_commands")
    if verify is not None and not isinstance(verify, list):
        findings.append(
            _preflight_finding(
                "DELIVERY_CONTRACT_VERIFY_COMMANDS_INVALID",
                "verify_commands",
                "delivery_contract.verify_commands must be a list of {cwd, command, timeout_seconds}.",
            )
        )
    elif isinstance(verify, list):
        for index, item in enumerate(verify):
            if not isinstance(item, dict) or not str(item.get("command") or "").strip():
                findings.append(
                    _preflight_finding(
                        "DELIVERY_CONTRACT_VERIFY_COMMAND_INVALID",
                        f"verify_commands[{index}]",
                        "verify_commands entries must be objects with a non-empty command.",
                    )
                )
    return findings


def _artifact_preflight_findings(artifacts: list[object]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for index, item in enumerate(artifacts):
        findings.extend(_one_artifact_preflight_findings(index, item))
    return findings


def _one_artifact_preflight_findings(index: int, item: object) -> list[dict[str, object]]:
    location = f"artifacts[{index}]"
    if not isinstance(item, dict):
        return [
            _preflight_finding(
                "DELIVERY_CONTRACT_ARTIFACT_INVALID",
                location,
                "delivery_contract artifact entries must be objects.",
            )
        ]
    if artifact_can_be_located(item):
        return []
    return [
        _preflight_finding(
            "DELIVERY_CONTRACT_ARTIFACT_TARGET_MISSING",
            location,
            "delivery_contract artifact entries must declare path/preferred_path or kind with allowed_output_roots.",
        )
    ]


def _preflight_finding(code: str, location: str, message: str) -> dict[str, object]:
    return {
        "code": code,
        "severity": "warning",
        "location": location,
        "message": message,
    }

# LLM: Delivery guidance is model discipline only; it may demand continued work
# but must never become a host-side quality or completion decision.
# 函数用途: 把交付合同转成模型可读的实现、验证和如实收尾提示。
def _artifact_guidance_lines(contract: dict[str, object]) -> list[str]:
    artifacts = _artifact_items(contract)
    if not artifacts:
        return []
    lines = ["执行要求："]
    if _has_target_coverage_contract(contract):
        lines.append("- 这个任务有目标覆盖清单；先用读取、搜索或执行工具覆盖清单里的来源/分片，再写最终产物。最终回复必须如实说明仍未覆盖的 required 目标。")
    for artifact in artifacts:
        lines.extend(_one_artifact_lines(artifact))
    lines.append("- 如果 required artifact 还不存在，优先对该 artifact 的目标路径动手：创建目录、开始写入或补齐阶段产物。")
    lines.append("- 可以继续必要的检索，但应同步留下本地草稿、数据或阶段产物，避免只读检查长期空转。")
    lines.append(
        "- 普通报告用 write_file.content 完整写入；超长文本用 mode=\"append\" 分成多个规范 write_file 调用。"
        "二进制产物用脚本生成后通过 write_file.data_base64 写入。"
    )
    lines.append(
        "- 交付前运行与任务相称的针对性检查；已知仍有缺口且现有工具或下级还能推进时继续工作，"
        "不要用一份诚实的未完成项列表代替交付。只有目标完成、用户暂停/改向或存在当前无法消除的"
        "真实阻塞时才给最终回复；真实阻塞和限制仍要如实说明。"
    )
    return lines


def _bootstrap_guidance_lines(contract: dict[str, object]) -> list[str]:
    bootstrap = contract.get("bootstrap_contract")
    if not isinstance(bootstrap, dict):
        return []
    targets = _bootstrap_targets(bootstrap.get("materialization_targets"))
    actions = _bootstrap_actions(bootstrap.get("startup_actions"))
    if not targets and not actions:
        return []
    lines = ["开工参考："]
    if targets:
        lines.append("- 可在合适时让下面这些结构化目标中的一个真实出现，避免长期只做目录查看。")
        lines.extend(f"  - {target}" for target in targets[:6])
        lines.append("- 阶段目标可以先写草稿或最小有效骨架，后续再根据证据和验证结果持续修订。")
    if actions:
        lines.extend(_startup_action_lines(actions))
    lines.append("- 如果暂时不确定具体工具，可以先 list_tools 一次，再按任务进展选择检索、写入或构建工具。")
    return lines


def _bootstrap_targets(items: object) -> list[str]:
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        relative = str(item.get("workspace_relative_path") or "").strip()
        target_type = str(item.get("target_type") or "").strip()
        if relative:
            lines.append(f"{target_type or 'target'}: {relative}")
    return lines


def _bootstrap_actions(items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _startup_action_lines(actions: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for action in sorted(actions, key=lambda item: int(item.get("priority", 0))):
        lines.extend(_startup_action_line_group(action))
    return lines


def _startup_action_line_group(action: dict[str, object]) -> list[str]:
    code = str(action.get("action") or "").strip()
    if code == "materialize_target":
        return ["- 先创建目录并开始写入第一个目标路径，再继续补齐其余内容。"]
    if code == "materialize_checkpoint":
        return _materialize_checkpoint_lines(action)
    if code == "invoke_builder_tool":
        return _builder_startup_lines(action)
    return []


def _materialize_checkpoint_lines(action: dict[str, object]) -> list[str]:
    checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
    if not checkpoint_ref:
        return []
    if action.get("research_first") is True or action.get("requires_auditable_source_evidence") is True:
        fields = ", ".join(str(item) for item in action.get("required_structured_fields", []) if str(item).strip())
        suffix = f"，必须包含 {fields}" if fields else ""
        if action.get("research_first") is True:
            return [
                f"- 先完成来源采集/读取，再写 checkpoint: {checkpoint_ref}",
                f"- 这是来源型 checkpoint，优先用采集/转换工具物化{suffix}。",
            ]
        return [
            f"- 先真实写出 checkpoint: {checkpoint_ref}",
            f"- 这是来源型 checkpoint，优先用采集/转换工具物化{suffix}。",
        ]
    return [
        f"- 先真实写出 checkpoint: {checkpoint_ref}",
        f"- 如果资料还没收全，可以给 {checkpoint_ref} 写阶段草稿，再继续抓取/整理。",
    ]


def _builder_startup_lines(action: dict[str, object]) -> list[str]:
    builder = str(action.get("builder_tool") or "").strip()
    if not builder:
        return []
    source_ref = str(action.get("source_ref") or "").strip()
    output_ref = str(action.get("output_ref") or "").strip()
    if source_ref and output_ref:
        return [f"- 阶段数据就绪后，用通用写入/命令工具生成 {output_ref}；来源参考 {source_ref}。"]
    return ["- 阶段数据就绪后，用通用写入/命令工具生成后续产物。"]


def _has_target_coverage_contract(contract: dict[str, object]) -> bool:
    coverage = contract.get("target_coverage_contract")
    if not isinstance(coverage, dict):
        return False
    return bool(_coverage_target_items(coverage))


def _model_visible_contract(contract: dict[str, object]) -> dict[str, object]:
    visible = dict(contract)
    coverage = visible.get("target_coverage_contract")
    if isinstance(coverage, dict):
        visible["target_coverage_contract"] = _model_visible_target_coverage_contract(coverage)
    return visible


def _model_visible_target_coverage_contract(coverage: dict[str, object]) -> dict[str, object]:
    visible = {
        key: value
        for key, value in coverage.items()
        if key != "target_items"
    }
    targets = _coverage_target_items(coverage)
    if not targets:
        return visible
    preview = _target_preview_items(targets)
    visible["target_count"] = len(targets)
    visible["target_items_preview"] = preview
    if len(preview) < len(targets):
        visible["targets_omitted"] = len(targets) - len(preview)
    return visible


def _coverage_target_items(coverage: dict[str, object]) -> list[object]:
    value = coverage.get("target_items")
    return list(value) if isinstance(value, list) else []


def _target_preview_items(targets: list[object]) -> list[object]:
    if len(targets) <= 8:
        return list(targets)
    return [*targets[:5], *targets[-3:]]


def _one_artifact_lines(artifact: dict[str, object]) -> list[str]:
    contract = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    requirements = contract.get("quality_requirements") if isinstance(contract.get("quality_requirements"), dict) else {}
    target = artifact.get("preferred_path") or artifact.get("path")
    if target:
        lines = [f"- 写入产物路径: {target}"]
    else:
        roots = artifact.get("allowed_output_roots") or artifact.get("search_roots") or artifact.get("artifact_roots")
        roots_text = ", ".join(str(item) for item in roots if str(item).strip()) if isinstance(roots, list) else "outputs, artifacts"
        lines = [f"- 写入产物目标: kind={artifact.get('kind') or '<missing>'}, allowed_output_roots={roots_text}"]
    if artifact.get("kind"):
        lines.append(f"- 产物类型: {artifact.get('kind')}")
    if requirements.get("complete_html_document"):
        lines.append("- HTML 必须包含完整 doctype/html/head/body 闭合结构。")
    if requirements.get("single_file_no_external_assets"):
        lines.append("- 单文件产物不得引用 http/https 外部 CSS、字体、图片或脚本。")
    if requirements.get("min_size_bytes"):
        lines.append(f"- 产物体量至少 {requirements.get('min_size_bytes')} bytes，但不要无意义膨胀。")
    lines.extend(_staging_lines(contract))
    return lines


def _staging_lines(contract: dict[str, object]) -> list[str]:
    staging = contract.get("staging_contract")
    if not isinstance(staging, dict):
        return []
    refs = staging.get("checkpoint_refs")
    if not isinstance(refs, list):
        return []
    lines = ["- 阶段产物:"]
    lines.extend(f"  - {ref}" for ref in refs if str(ref).strip())
    builder_tool = str(staging.get("builder_tool") or "").strip()
    if builder_tool:
        lines.append(f"- 阶段构建建议: 原合同提到 {builder_tool}，当前请用通用写入/命令工具完成。")
    source_ref = _staging_source_ref(staging)
    output_ref = _staging_output_ref(staging)
    if source_ref and output_ref and builder_tool:
        lines.append(f"- 阶段输入就绪后生成 {output_ref}，来源参考 {source_ref}。")
        if source_ref.lower().endswith(".json"):
            lines.append("- JSON checkpoint 用 write_file 写完整 JSON；大批量数据可用授权命令/脚本生成后写入。")
    return lines


def _staging_source_ref(staging: dict[str, object]) -> str:
    for key in _staging_source_keys(staging):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def _staging_output_ref(staging: dict[str, object]) -> str:
    for key in _staging_output_keys(staging):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def _staging_ref_keys(staging: dict[str, object]) -> tuple[str, ...]:
    return (*_staging_source_keys(staging), *_staging_output_keys(staging))


def _staging_source_keys(staging: dict[str, object]) -> tuple[str, ...]:
    keys = [
        str(staging.get("source_ref_key") or "").strip(),
        str(staging.get("input_ref_key") or "").strip(),
        "source_json_ref",
        "source_markdown_ref",
        "source_ref",
        "input_ref",
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


def _staging_output_keys(staging: dict[str, object]) -> tuple[str, ...]:
    keys = [
        str(staging.get("output_ref_key") or "").strip(),
        "workbook_ref",
        "pdf_ref",
        "output_ref",
        "artifact_ref",
    ]
    return tuple(dict.fromkeys(key for key in keys if key))


def render_recovery_guidance_lines(
    contract: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    findings = _runtime_findings(contract)
    if not findings:
        return []
    lines = ["恢复要求："]
    for finding in findings:
        lines.extend(_one_runtime_finding_lines(finding, artifact_items))
    return lines


def _runtime_findings(contract: dict[str, object]) -> list[dict[str, object]]:
    recovery = contract.get("recovery")
    acceptance = recovery.get("acceptance") if isinstance(recovery, dict) else None
    findings = acceptance.get("runtime_findings") if isinstance(acceptance, dict) else None
    if not isinstance(findings, list):
        return []
    return [dict(item) for item in findings if isinstance(item, dict)]


def _one_runtime_finding_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    code = str(finding.get("code") or "").strip()
    if code == "STAGED_JSON_INVALID":
        return _staged_json_invalid_lines(finding, artifact_items)
    if code == "STAGED_JSON_NO_ROWS":
        return _staged_json_no_rows_lines(finding, artifact_items)
    location = str(finding.get("location") or finding.get("stage_ref") or "").strip()
    return [f"- runtime_finding={code or '<unknown>'} location={location or '<none>'}"]


def _staged_json_no_rows_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    stage_ref = str(finding.get("stage_ref") or finding.get("location") or "")
    artifact = _artifact_for_stage_ref(artifact_items, stage_ref)
    validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
    columns = validation.get("required_columns") if isinstance(validation.get("required_columns"), list) else []
    lines = [
        "- staged_json_no_rows:",
        f"  - source_ref={_staging_source_ref(staging) or stage_ref}",
        f"  - write_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - required_columns={', '.join(str(item) for item in columns)}",
        f"  - writer_tool={finding.get('writer_tool') or 'write_file'}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - output_ref={_staging_output_ref(staging)}",
        "  - 优先用 write_file 写完整 JSON checkpoint；数据很大时可用授权命令/脚本生成文件。",
        "  - source_ref 有非空 rows/sheets 或声明形状后，再调用 builder_tool；不要把空 checkpoint 当完成。",
    ]
    lines.extend(_collection_contract_lines(validation))
    return lines


def _staged_json_invalid_lines(
    finding: dict[str, object], artifact_items: list[dict[str, object]]
) -> list[str]:
    stage_ref = str(finding.get("stage_ref") or finding.get("location") or "")
    artifact = _artifact_for_stage_ref(artifact_items, stage_ref)
    validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
    parse_error = str(finding.get("parse_error") or "")
    return [
        "- staged_json_invalid:",
        f"  - source_ref={_staging_source_ref(staging) or stage_ref}",
        f"  - required_shape={_checkpoint_shape_hint(stage_ref, staging)}",
        f"  - parse_error={json.dumps(parse_error, ensure_ascii=False)}",
        f"  - writer_tool={finding.get('writer_tool') or 'write_file'}",
        f"  - builder_tool={staging.get('builder_tool') or ''}",
        f"  - output_ref={_staging_output_ref(staging)}",
        "  - 优先用 write_file 重写完整 checkpoint，避免手动修补截断 JSON。",
        "  - 先把 source_ref 修成可解析的完整 checkpoint，再继续 builder_tool 或下一阶段产物。",
    ]


def _artifact_for_stage_ref(
    artifact_items: list[dict[str, object]], stage_ref: str
) -> dict[str, object]:
    for artifact in artifact_items:
        validation = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
        staging = validation.get("staging_contract") if isinstance(validation.get("staging_contract"), dict) else {}
        refs = staging.get("checkpoint_refs") if isinstance(staging.get("checkpoint_refs"), list) else []
        if stage_ref in refs or stage_ref in _staging_refs(staging):
            return artifact
    return {}


def _staging_refs(staging: dict[str, object]) -> set[str]:
    return {
        str(staging.get(key) or "").strip()
        for key in _staging_ref_keys(staging)
        if str(staging.get(key) or "").strip()
    }


def _collection_contract_lines(validation: dict[str, object]) -> list[str]:
    collection = validation.get("collection_contract")
    evidence = validation.get("evidence_contract")
    lines: list[str] = []
    if isinstance(collection, dict):
        lines.extend(_collection_shape_lines(collection))
        lines.extend(_item_evidence_contract_lines(collection, evidence))
    if isinstance(evidence, dict):
        lines.extend(_evidence_contract_lines(evidence))
    return lines


def _collection_shape_lines(collection: dict[str, object]) -> list[str]:
    lines = [
        f"  - {key}={value}"
        for key in ("groups_path", "items_path", "min_groups", "min_items_per_group")
        if (value := collection.get(key))
    ]
    fields = collection.get("required_item_fields")
    if isinstance(fields, list) and fields:
        lines.append(f"  - required_item_fields={', '.join(str(item) for item in fields)}")
    return lines


def _evidence_contract_lines(evidence: dict[str, object]) -> list[str]:
    lines: list[str] = []
    if evidence.get("require_verified") is not None:
        lines.append(f"  - require_verified_evidence={_json_bool(evidence.get('require_verified'), default=False)}")
    fields = evidence.get("required_fields")
    if isinstance(fields, list) and fields:
        lines.append(f"  - evidence_required_fields={', '.join(str(item) for item in fields)}")
    return lines


def _item_evidence_contract_lines(collection: dict[str, object], evidence: object) -> list[str]:
    evidence_fields = collection.get("required_item_evidence_fields")
    if not isinstance(evidence_fields, list) and isinstance(evidence, dict) and collection.get("require_item_evidence") is not False:
        evidence_fields = evidence.get("required_fields")
    if not isinstance(evidence_fields, list) or not evidence_fields:
        return []
    return [
        f"  - item_evidence_required_fields={', '.join(str(item) for item in evidence_fields)}",
        "  - item_evidence_shape=field_source_ids 或 row-scoped claims.item_path",
    ]


def _checkpoint_shape_hint(stage_ref: str, staging: dict[str, object]) -> str:
    hints = staging.get("checkpoint_shape_hints")
    if isinstance(hints, dict):
        hint = str(hints.get(stage_ref) or "").strip()
        if hint:
            return hint
    return "[] 或 {\"rows\":[...]} 或 {\"sheets\":[{\"name\":\"...\",\"rows\":[...]}]} 这类非空结构化 JSON"


def _json_bool(value: object, *, default: bool) -> str:
    return "true" if bool(default if value is None else value) else "false"


def _artifact_items(contract: dict[str, object]) -> list[dict[str, object]]:
    items = contract.get("artifacts")
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


__all__ = [
    "DELIVERY_PREFLIGHT_FINDINGS_KEY",
    "delivery_contract_preflight_findings",
    "render_delivery_contract_section",
]
