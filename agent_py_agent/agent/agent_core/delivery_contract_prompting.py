
from __future__ import annotations

import json

from .artifact_locator import artifact_can_be_located
from .delivery_contract_prompting_recovery import render_recovery_guidance_lines

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
    return _artifact_preflight_findings(artifacts) if isinstance(artifacts, list) else []


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


from .delivery_contract_prompting_bootstrap import _bootstrap_guidance_lines


def _artifact_guidance_lines(contract: dict[str, object]) -> list[str]:
    artifacts = _artifact_items(contract)
    if not artifacts:
        return []
    lines = ["执行要求："]
    if _has_target_coverage_contract(contract):
        lines.append("- 这个任务有目标覆盖清单；先用读取、搜索或执行工具覆盖清单里的来源/分片，再写最终产物。未覆盖的 required 目标会导致验收返工。")
    for artifact in artifacts:
        lines.extend(_one_artifact_lines(artifact))
    lines.append("- 如果 required artifact 还不存在，优先对该 artifact 的目标路径动手：创建目录、开始写入或补齐阶段产物。")
    lines.append("- 可以继续必要的检索，但应同步留下本地草稿、数据或阶段产物，避免只读检查长期空转。")
    lines.append(
        "- 普通报告用 write_file.content 完整写入；超长文本可在 [TOOL_CALL] 外使用 "
        "[WRITE_FILE_RAW path=\"...\"]...[/WRITE_FILE_RAW] 原文块，不要把 WRITE_FILE_RAW 当 JSON tool 名。"
        "二进制产物用脚本生成后通过 write_file.data_base64 写入。"
    )
    lines.append("- 当你确认交付物已经准备好时，调用 submit_for_acceptance 提交验收；普通最终回复不会触发验收。")
    return lines


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
        return [f"- 阶段数据就绪后，用通用工具生成 {output_ref}；来源参考 {source_ref}，不要依赖固定 builder 工具。"]
    return ["- 阶段数据就绪后，用通用写入/命令工具生成后续产物，不要依赖固定 builder 工具。"]


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


def _artifact_target_path(artifact: dict[str, object]) -> str:
    return str(artifact.get("preferred_path") or artifact.get("path") or "").strip()


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


def _builder_source_param(staging: dict[str, object], builder_tool: str) -> str:
    for key in ("source_param", "source_param_name", "input_param"):
        value = str(staging.get(key) or "").strip()
        if value:
            return value
    return "source_ref"


def _staging_source_ref(staging: dict[str, object]) -> str:
    keys = [
        str(staging.get("source_ref_key") or "").strip(),
        str(staging.get("input_ref_key") or "").strip(),
        "source_json_ref",
        "source_markdown_ref",
        "source_ref",
        "input_ref",
    ]
    for key in keys:
        if not key:
            continue
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def _staging_output_ref(staging: dict[str, object]) -> str:
    keys = [
        str(staging.get("output_ref_key") or "").strip(),
        "workbook_ref",
        "pdf_ref",
        "output_ref",
        "artifact_ref",
    ]
    for key in keys:
        if not key:
            continue
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


def _artifact_items(contract: dict[str, object]) -> list[dict[str, object]]:
    items = contract.get("artifacts")
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


__all__ = [
    "DELIVERY_PREFLIGHT_FINDINGS_KEY",
    "delivery_contract_preflight_findings",
    "render_delivery_contract_section",
]
