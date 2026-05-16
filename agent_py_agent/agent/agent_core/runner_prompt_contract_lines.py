# LLM: Runner prompt contract helpers keep protocol rules out of the prompt assembler.
# 模块用途: 渲染子代理 runner 输入/输出合同提示，避免 runner_prompts.py 继续变大。

from __future__ import annotations

from ..subagent import SubAgentExecutionContext
from .runner_prompt_context_summary import runner_context_summary_payload


# LLM: input_dependency_contract_lines tells runners to use resolved upstream files before prose aliases.
# 函数用途: 防止下游读取一个不存在的自然语言路径后立刻 BLOCKED，而忽略已完成上游 artifact 的真实路径。
def input_dependency_contract_lines(context: SubAgentExecutionContext) -> list[str]:
    payload = runner_context_summary_payload(context)
    contract = payload.get("input_contract") if isinstance(payload.get("input_contract"), dict) else {}
    resolved = [str(item) for item in contract.get("resolved_read_paths") or [] if str(item).strip()]
    if not resolved:
        return []
    return [
        "- 读取上游/依赖输入时，先使用 input_contract.resolved_read_paths 里的真实存在路径；"
        "某个自然语言路径不存在时，必须继续尝试同名或已解析候选，不要立刻 BLOCKED。",
        f"- resolved_read_paths: {', '.join(resolved[:8])}",
    ]


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
