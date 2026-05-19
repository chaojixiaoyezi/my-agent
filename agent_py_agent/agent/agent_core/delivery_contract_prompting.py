# LLM: Delivery contract prompting renders structured contracts into model-visible execution hints.
# 模块用途: 把 RunParams.delivery_contract 转成人类可读执行提示；系统事实仍以结构化合同字段为准。

from __future__ import annotations

import json


# LLM: render_delivery_contract_section keeps delivery instructions derived from JSON fields.
# 函数用途: 渲染交付合同提示段，帮助模型执行路径/格式/资源约束，不让系统从提示反向取事实。
def render_delivery_contract_section(contract: dict[str, object]) -> str:
    return "\n".join(
        [
            "[tool-system delivery-contract]",
            json.dumps(contract, ensure_ascii=False, sort_keys=True),
            *_artifact_guidance_lines(contract),
        ]
    )


# LLM: _artifact_guidance_lines turns artifact machine fields into concise model guidance.
# 函数用途: 根据 artifacts.validation_contract 生成执行提示，避免模型忽略单文件和完整文档要求。
def _artifact_guidance_lines(contract: dict[str, object]) -> list[str]:
    lines = ["执行要求："]
    for artifact in _artifact_items(contract):
        lines.extend(_one_artifact_lines(artifact))
    lines.append("如果内容较长，使用 file_write_session 分块写入，并在最终答复前 finish。")
    return lines


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
    return lines


# LLM: _artifact_items normalizes contract artifacts without reading prompt text.
# 函数用途: 从结构化合同读取 artifact 列表；非对象条目会被忽略。
def _artifact_items(contract: dict[str, object]) -> list[dict[str, object]]:
    items = contract.get("artifacts")
    return [dict(item) for item in items] if isinstance(items, list) and all(isinstance(item, dict) for item in items) else []


__all__ = ["render_delivery_contract_section"]
