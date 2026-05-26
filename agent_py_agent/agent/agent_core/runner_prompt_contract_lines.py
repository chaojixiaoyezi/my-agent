# LLM: Runner prompt contract helpers keep protocol rules out of the prompt assembler.
# 模块用途: 渲染子代理 runner 输入/输出合同提示，避免 runner_prompts.py 继续变大。

from __future__ import annotations

from ..subagent import SubAgentExecutionContext
from .runner_prompt_context_summary import runner_context_summary_payload


# LLM: read_ref_context_lines renders read refs as context, not as startup dependencies.
# 函数用途: 把父级显式给出的可读路径展示给 runner；路径缺失时只能记录限制或换线索，不能当成启动门。
def read_ref_context_lines(context: SubAgentExecutionContext) -> list[str]:
    payload = runner_context_summary_payload(context)
    refs = payload.get("read_refs") if isinstance(payload.get("read_refs"), dict) else {}
    declared = [str(item) for item in refs.get("required_read_paths") or [] if str(item).strip()]
    resolved = [str(item) for item in refs.get("resolved_read_paths") or [] if str(item).strip()]
    if not declared and not resolved:
        return []
    lines = [
        "- read_refs 是父级给你的可读线索和授权范围，不是启动前置门。"
        "能读就读；某条路径不存在时，记录限制、尝试其他线索或向父级说明，不要因为单条线索缺失直接卡死。",
    ]
    if declared:
        lines.append(f"- declared_read_paths: {', '.join(declared[:8])}")
    if resolved:
        lines.append(f"- resolved_read_paths: {', '.join(resolved[:8])}")
    return lines


# LLM: required_product_contract_lines makes exact deliverable paths explicit before the model writes files.
# 函数用途: 把 context bundle 中的 required_file_refs 渲染成硬执行提示，区分用户产物和 agent-run 内部报告。
def required_product_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    bundle = context.context_bundle if isinstance(context.context_bundle, dict) else {}
    contract = bundle.get("output_contract") if isinstance(bundle.get("output_contract"), dict) else {}
    refs = [str(item) for item in contract.get("required_file_refs") or [] if str(item).strip()]
    if not refs:
        return []
    return [
        "- 用户要求的业务产物必须写到 output_contract.required_file_refs 中的精确路径；"
        "最终 SUBAGENT_RESULT 的 artifacts/evidence_packets.artifact_refs 也必须引用这些业务产物路径。",
        "- agent_run_final_report_ref 是系统内部交接报告，不是用户要求的业务产物；"
        "除非它同时出现在 required_file_refs 里，否则不能把它当作交付文件。",
        f"- required_file_refs: {', '.join(refs[:8])}",
    ]
