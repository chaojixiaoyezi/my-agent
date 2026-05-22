# LLM: Delivery contract prompting renders structured contracts into model-visible execution hints.
# 模块用途: 把 RunParams.delivery_contract 转成人类可读执行提示；系统事实仍以结构化合同字段为准。

from __future__ import annotations

import json

from .delivery_contract_prompting_recovery import render_recovery_guidance_lines

DELIVERY_PREFLIGHT_FINDINGS_KEY = "_preflight_findings"


# LLM: render_delivery_contract_section keeps delivery instructions derived from JSON fields.
# 函数用途: 渲染交付合同提示段，帮助模型执行路径/格式/资源约束，不让系统从提示反向取事实。
def render_delivery_contract_section(contract: dict[str, object]) -> str:
    return "\n".join(
        [
            "[tool-system delivery-contract]",
            json.dumps(contract, ensure_ascii=False, sort_keys=True),
            *_bootstrap_guidance_lines(contract),
            *_artifact_guidance_lines(contract),
            *render_recovery_guidance_lines(contract, _artifact_items(contract)),
        ]
    )


# LLM: delivery_contract_preflight_findings reports malformed delivery-contract shapes before prompt rendering.
# 函数用途: 让 CLI/runner 产生机器可读 schema finding，避免 prompt renderer 对坏结构静默降级。
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


# LLM: _artifact_preflight_findings checks artifact object shape item by item.
# 函数用途: 混入脏 artifact 不会丢掉整段合同，只对具体坏项产出结构化 finding。
def _artifact_preflight_findings(artifacts: list[object]) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for index, item in enumerate(artifacts):
        findings.extend(_one_artifact_preflight_findings(index, item))
    return findings


# LLM: _one_artifact_preflight_findings validates one artifact entry.
# 函数用途: 只读 artifacts[index] 的结构字段，输出无效对象或目标路径缺失 finding。
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
    if _artifact_target_path(item):
        return []
    return [
        _preflight_finding(
            "DELIVERY_CONTRACT_ARTIFACT_TARGET_MISSING",
            location,
            "delivery_contract artifact entries must declare path or preferred_path.",
        )
    ]


# LLM: _preflight_finding keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _preflight_finding(code: str, location: str, message: str) -> dict[str, object]:
    return {
        "code": code,
        "severity": "warning",
        "location": location,
        "message": message,
    }


# LLM: _bootstrap_guidance_lines turns structured startup targets into concise first-round execution hints.
# 函数用途: 根据 bootstrap_contract 渲染通用开工顺序，避免模型前几轮一直只读检查目录。
def _bootstrap_guidance_lines(contract: dict[str, object]) -> list[str]:
    bootstrap = contract.get("bootstrap_contract")
    if not isinstance(bootstrap, dict):
        return []
    targets = _bootstrap_targets(bootstrap.get("materialization_targets"))
    actions = _bootstrap_actions(bootstrap.get("startup_actions"))
    if not targets and not actions:
        return []
    lines = ["开工顺序："]
    if targets:
        lines.append("- 前两轮至少让下面这些结构化目标中的一个真实出现，不要连续两轮只做目录查看。")
        lines.extend(f"  - {target}" for target in targets[:6])
        lines.append("- 阶段目标允许先写最小有效骨架，但 JSON checkpoint 必须是可验收的非空结构，不能只写空数组或空对象。")
    if actions:
        lines.extend(_startup_action_lines(actions))
    lines.append("- 如果暂时不确定具体工具，可以先 list_tools 一次，但紧接着就开始物化目标路径。")
    return lines


# LLM: _artifact_guidance_lines turns artifact machine fields into concise model guidance.
# 函数用途: 根据 artifacts.validation_contract 生成执行提示，避免模型忽略单文件和完整文档要求。
def _artifact_guidance_lines(contract: dict[str, object]) -> list[str]:
    lines = ["执行要求："]
    for artifact in _artifact_items(contract):
        lines.extend(_one_artifact_lines(artifact))
    lines.append("- 如果 required artifact 还不存在，前两轮优先对该 artifact 的目标路径动手：创建目录、开始写入或补齐阶段产物。")
    lines.append("- 不要把前两轮都花在只读检查上；先让 required artifact 或阶段产物出现，再继续精修。")
    lines.append("如果内容较长，使用 file_write_session 分块写入，并在最终答复前 finish。")
    return lines


# LLM: _bootstrap_targets formats materialization targets for model-visible startup hints.
# 函数用途: 把 bootstrap_contract.materialization_targets 里的结构化路径压缩成简短提示行。
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


# LLM: _bootstrap_actions normalizes startup action objects from the generic delivery contract.
# 函数用途: 读取 bootstrap_contract.startup_actions，过滤非对象项。
def _bootstrap_actions(items: object) -> list[dict[str, object]]:
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


# LLM: _startup_action_lines keeps startup guidance generic and based on structured action codes only.
# 函数用途: 渲染 materialize_target / invoke_builder_tool 等开工动作，不靠任务文案推断。
def _startup_action_lines(actions: list[dict[str, object]]) -> list[str]:
    lines: list[str] = []
    for action in sorted(actions, key=lambda item: int(item.get("priority", 0))):
        lines.extend(_startup_action_line_group(action))
    return lines


# LLM: _startup_action_line_group routes one structured startup action to its rendering helper.
# 函数用途: 按 action code 分发 materialize_target/checkpoint/builder 行文，保持启动提示仍只读结构化合同。
def _startup_action_line_group(action: dict[str, object]) -> list[str]:
    code = str(action.get("action") or "").strip()
    if code == "materialize_target":
        return ["- 先创建目录并开始写入第一个目标路径，再继续补齐其余内容。"]
    if code == "materialize_checkpoint":
        return _materialize_checkpoint_lines(action)
    if code == "invoke_builder_tool":
        return _builder_startup_lines(action)
    return []


# LLM: _materialize_checkpoint_lines explains how to start a staged checkpoint from structured refs only.
# 函数用途: 渲染“先写 checkpoint 再继续整理”的通用提示，不依赖任务名称或自然语言模板。
def _materialize_checkpoint_lines(action: dict[str, object]) -> list[str]:
    checkpoint_ref = str(action.get("checkpoint_ref") or "").strip()
    if not checkpoint_ref:
        return []
    if action.get("requires_auditable_source_evidence") is True:
        fields = ", ".join(str(item) for item in action.get("required_structured_fields", []) if str(item).strip())
        suffix = f"，必须包含 {fields}" if fields else ""
        return [
            f"- 先真实写出 checkpoint: {checkpoint_ref}",
            f"- 这是来源型 checkpoint，优先用采集/转换工具物化{suffix}。",
        ]
    return [
        f"- 先真实写出 checkpoint: {checkpoint_ref}",
        f"- 如果资料还没收全，先给 {checkpoint_ref} 写可验收的非空结构骨架，再继续抓取/整理。",
    ]


# LLM: _builder_startup_lines turns a structured builder action into the next-step call hint.
# 函数用途: 当 staged contract 指明 builder_tool/source/output 时，提示模型优先切到构建步骤而不是继续空转。
def _builder_startup_lines(action: dict[str, object]) -> list[str]:
    builder = str(action.get("builder_tool") or "").strip()
    if not builder:
        return []
    source_ref = str(action.get("source_ref") or "").strip()
    output_ref = str(action.get("output_ref") or "").strip()
    if source_ref and output_ref:
        return [f"- 阶段数据就绪后，优先调用 {builder}: {_builder_source_param(builder)}={source_ref}, path={output_ref}"]
    return [f"- 阶段数据就绪后，优先调用 {builder} 生成后续产物。"]


# LLM: _one_artifact_lines summarizes one artifact contract without changing validation behavior.
# 函数用途: 输出单个产物的路径、类型和关键质量要求，供模型更稳地生成可验收文件。
def _one_artifact_lines(artifact: dict[str, object]) -> list[str]:
    contract = artifact.get("validation_contract") if isinstance(artifact.get("validation_contract"), dict) else {}
    requirements = contract.get("quality_requirements") if isinstance(contract.get("quality_requirements"), dict) else {}
    lines = [f"- 写入产物路径: {artifact.get('preferred_path') or artifact.get('path') or '<missing>'}"]
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


# LLM: _artifact_target_path keeps this contract helper structure-first and stable.
# 函数用途: 支撑本模块的机器字段校验、转换或汇总，不读取普通自然语言作为事实。
def _artifact_target_path(artifact: dict[str, object]) -> str:
    return str(artifact.get("preferred_path") or artifact.get("path") or "").strip()


# LLM: _staging_lines renders structured checkpoint refs for long-running deliverables.
# 函数用途: 把 validation_contract.staging_contract 展示给模型，帮助按数据/脚本/最终产物分段执行。
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
        lines.append(f"- 阶段构建工具: {builder_tool}")
    source_ref = _staging_source_ref(staging)
    output_ref = _staging_output_ref(staging)
    if source_ref and output_ref and builder_tool:
        source_param = _builder_source_param(builder_tool)
        lines.append(f"- 阶段输入就绪后优先调用 {builder_tool}: {source_param}={source_ref}, path={output_ref}")
        if source_ref.lower().endswith(".json"):
            lines.append("- JSON checkpoint 优先用 write_structured_json 写入 rows/sheets/data；不要手写大型 JSON 字符串。")
            lines.append("- 大批量表格可用 write_structured_json.generated_rows 声明 count/columns/fields/sheets，由工具生成 rows。")
            lines.append(
                "- 多个 JSON API 来源要汇成表格时，可用 api_json_collection 直接生成带 source_refs/claims 的 checkpoint；"
                "大量同形日期/分页请求优先用 request_ranges，避免手写长 JSON。"
            )
    return lines


# LLM: _builder_source_param maps builder tools to their structured source parameter names.
# 函数用途: 渲染工具调用提示时使用工具 schema 参数名，而不是固定 source_json_path。
def _builder_source_param(builder_tool: str) -> str:
    if builder_tool == "markdown_to_pdf":
        return "source_markdown_path"
    if builder_tool == "data_to_workbook":
        return "source_json_path"
    return "source_ref"


# LLM: _staging_source_ref reads all supported staged source aliases.
# 函数用途: 同时支持 JSON、Markdown 和未来通用 source_ref。
def _staging_source_ref(staging: dict[str, object]) -> str:
    for key in ("source_json_ref", "source_markdown_ref", "source_ref"):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


# LLM: _staging_output_ref reads all supported staged output aliases.
# 函数用途: 同时支持 workbook、PDF 和未来通用 output_ref。
def _staging_output_ref(staging: dict[str, object]) -> str:
    for key in ("workbook_ref", "pdf_ref", "output_ref"):
        if value := str(staging.get(key) or "").strip():
            return value
    return ""


# LLM: _artifact_items normalizes contract artifacts without reading prompt text.
# 函数用途: 从结构化合同读取 artifact 列表；非对象条目会被忽略。
def _artifact_items(contract: dict[str, object]) -> list[dict[str, object]]:
    items = contract.get("artifacts")
    return [dict(item) for item in items if isinstance(item, dict)] if isinstance(items, list) else []


__all__ = [
    "DELIVERY_PREFLIGHT_FINDINGS_KEY",
    "delivery_contract_preflight_findings",
    "render_delivery_contract_section",
]
