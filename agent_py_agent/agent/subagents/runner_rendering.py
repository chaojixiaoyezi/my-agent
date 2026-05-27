# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

from .models import (
    ChannelProbeReport,
    ChannelProbeResult,
    SubAgentExecutionContext,
)
from .runner_rendering_context import render_context_bundle_section
from .runner_rendering_context_packs import render_context_packs_section
from .runner_rendering_sections import render_evidence_item_lines, render_granted_card_lines
from .runner_result_rendering import render_runner_result_markdown


# LLM: execution-context Markdown includes context bundle refs before capabilities so handoff checks are visible first.
# LLM: _render_execution_context_header 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总execution上下文header的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_execution_context_header(context):
    return [
        "# SUBAGENT EXECUTION CONTEXT",
        "",
        f"- run_id: {context.run_id}",
        f"- generated_at: {context.generated_at}",
        f"- status: {context.status}",
        f"- verification_status: {context.verification_status}",
        f"- channel_status: {context.channel_status}",
        f"- runner_attempts: {context.runner_attempts}",
        f"- runner_last_error: {context.runner_last_error or 'none'}",
        f"- agent: {context.agent_name}",
        f"- role: {context.role}",
        f"- owner: {context.owner or 'none'}",
        f"- supervisor: {context.supervisor or 'none'}",
        f"- final_owner: {context.final_owner or 'none'}",
        f"- parent_id: {context.parent_id or 'none'}",
        f"- root_id: {context.root_id or context.run_id}",
        f"- depth: {context.depth}",
        # LLM: Render session ids so resumed runners do not confuse run attempts with agent identity.
        f"- subagent_session_id: {context.subagent_session_id or 'none'}",
        f"- agent_thread_id: {context.agent_thread_id or 'none'}",
        f"- parent_subagent_session_id: {context.parent_subagent_session_id or 'none'}",
        f"- root_subagent_session_id: {context.root_subagent_session_id or context.subagent_session_id or 'none'}",
        f"- task_dir: {context.task_dir}",
        "",
        "## Goal",
        "",
        context.goal,
        "",
        "## Thought",
        "",
        context.thought or "未设置",
        "",
        "## Plan",
        "",
    ]
# LLM: _render_capabilities_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总能力section的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_capabilities_section(context):
    lines = ["## Allowed Capabilities", ""]
    lines.append(f"- skills: {', '.join(context.allowed_skills) or 'none'}")
    lines.append(f"- tools: {', '.join(context.allowed_tools) or 'none'}")
    shell_mode = ""
    if isinstance(context.effective_permissions, dict):
        shell_mode = str(context.effective_permissions.get("shell_access_mode") or "").strip()
    if shell_mode:
        lines.append(f"- shell_access_mode: {shell_mode}")
    lines.extend(["", "## Granted Cards", ""])
    if context.granted_cards:
        for card in context.granted_cards:
            lines.extend(render_granted_card_lines(card))
    else:
        lines.append("- none")
    return lines
# LLM: _render_write_boundary_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总boundarysection的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会改动任务状态、执行器结果、验收和报告展示，调用方依赖写入顺序和文件格式。
def _render_write_boundary_section(context):
    lines = ["", "## Write Boundary", ""]
    allowed_roots = context.write_boundary.get("allowed_write_roots") or []
    product_roots = context.write_boundary.get("product_write_roots") or []
    forbidden_roots = context.write_boundary.get("forbidden_write_roots") or []
    locked_files = context.write_boundary.get("locked_files") or []
    lines.append(f"- task_dir: {context.write_boundary.get('task_dir') or context.task_dir}")
    lines.append(f"- allowed_write_roots: {', '.join(allowed_roots) if allowed_roots else 'none'}")
    lines.append(f"- product_write_roots: {', '.join(product_roots) if product_roots else 'none'}")
    lines.append(f"- product_write_policy: {context.write_boundary.get('product_write_policy') or 'direct'}")
    if context.write_boundary.get("shell_access_mode"):
        lines.append(f"- shell_access_mode: {context.write_boundary.get('shell_access_mode')}")
    lines.append(
        f"- forbidden_write_roots: {', '.join(forbidden_roots) if forbidden_roots else 'none'}"
    )
    lines.append(f"- locked_files: {', '.join(locked_files) if locked_files else 'none'}")
    return lines


# LLM: Declared output refs are user-visible deliverable targets, not runner-private reports.
# 函数用途: 把父级收口要求了目标路径但子代理只看到 output.json。
def _render_declared_outputs_section(context):
    output_contract = (
        context.context_bundle.get("output_contract")
        if isinstance(context.context_bundle, dict)
        else {}
    )
    if not isinstance(output_contract, dict):
        output_contract = {}
    required_refs = _string_list(output_contract.get("required_file_refs"))
    declared_refs = _string_list(output_contract.get("declared_output_refs"))
    if not required_refs and not declared_refs:
        return []
    lines = ["", "## Declared Output Targets", ""]
    if required_refs:
        lines.append("- required_file_refs:")
        lines.extend(f"  - {item}" for item in required_refs)
    if declared_refs:
        lines.append("- declared_output_refs (may include logical result keys):")
        lines.extend(f"  - {item}" for item in declared_refs)
    if required_refs:
        lines.append(
            "- 如果上面有 required_file_refs，必须把交付产物写到这些路径；"
            "内部 output.json 只能作为运行报告，不能单独冒充用户产物。"
        )
    return lines


# LLM: _string_list keeps runner rendering tolerant of malformed list fields.
# 函数用途: 将 list/tuple/set 里的非空项转成字符串列表，其他类型返回空列表。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result
# LLM: _render_quality_contract_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总qualitycontractsection的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_quality_contract_section(contract):
    lines = ["", "## Quality Contract", ""]
    lines.append(f"- user_visible_goal: {contract.user_visible_goal or 'none'}")
    lines.append(f"- benchmark_sample: {contract.benchmark_sample or 'none'}")
    lines.append(f"- quality_bar: {contract.quality_bar or 'none'}")
    lines.append("- failure_conditions:")
    lines.extend(f"  - {item}" for item in contract.failure_conditions or ["none"])
    lines.append("- forbidden_delivery:")
    lines.extend(f"  - {item}" for item in contract.forbidden_delivery or ["none"])
    lines.append("- must_check:")
    lines.extend(f"  - {item}" for item in contract.must_check or ["none"])
    lines.append("- sampling_plan:")
    lines.extend(f"  - {item}" for item in contract.sampling_plan or ["none"])
    lines.append("- evidence_required:")
    lines.extend(f"  - {item}" for item in contract.evidence_required or ["none"])
    lines.append(f"- risk_report_required: {contract.risk_report_required or 'none'}")
    lines.append("- allowed_degradation:")
    lines.extend(f"  - {item}" for item in contract.allowed_degradation or ["none"])
    return lines
# LLM: _render_context_manifest_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总上下文manifestsection的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_context_manifest_section(manifest):
    lines = ["", "## Context Manifest", ""]
    lines.append(f"- core_pack_version: {manifest.core_pack_version}")
    lines.append(f"- role_pack: {manifest.role_pack or 'none'}")
    lines.append(f"- quality_contract_ref: {manifest.quality_contract_ref or 'none'}")
    lines.append(f"- token_budget: {manifest.token_budget}")
    lines.append("- task_pack_refs:")
    lines.extend(f"  - {item}" for item in manifest.task_pack_refs or ["none"])
    lines.append("- required_read_paths:")
    lines.extend(f"  - {item}" for item in manifest.required_read_paths or ["none"])
    lines.append("- omitted_context:")
    lines.extend(f"  - {item}" for item in manifest.omitted_context or ["none"])
    return lines
# LLM: _render_evidence_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总证据section的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_evidence_section(context):
    lines = ["", "## Evidence", ""]
    if context.evidence:
        for item in context.evidence:
            lines.extend(render_evidence_item_lines(item))
    else:
        lines.append("- 暂无")
    return lines


# LLM: _render_pending_requests_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总pendingrequestssection的展示文本，保持命令行、日志和审计输出一致；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _render_pending_requests_section(context):
    lines = ["", "## Pending Capability Requests", ""]
    if context.pending_requests:
        for item in context.pending_requests:
            lines.append(
                f"- `{item.get('id')}` needed={item.get('needed_capability')} "
                f"status={item.get('status')}: {item.get('problem')}"
            )
    else:
        lines.append("- none")
    return lines
# LLM: _render_open_gaps_section 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总开放gapssection的展示文本，保持命令行、日志和审计输出一致；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _render_open_gaps_section(context):
    lines = ["", "## Open Capability Gaps", ""]
    if context.open_gaps:
        for item in context.open_gaps:
            lines.append(
                f"- `{item.get('id')}` missing={item.get('missing_capability')} "
                f"status={item.get('status')}: {item.get('why_failed')}"
            )
    else:
        lines.append("- none")
    return lines
# LLM: render_execution_context_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总execution上下文markdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def render_execution_context_markdown(context: SubAgentExecutionContext) -> str:
    lines = _render_execution_context_header(context)
    lines.extend(f"- {item}" for item in context.plan or ["未设置"])
    lines.extend(render_context_bundle_section(context))
    lines.extend(_render_capabilities_section(context))
    lines.extend(_render_write_boundary_section(context))
    lines.extend(_render_declared_outputs_section(context))
    lines.extend(["", "## Acceptance Checks", ""])
    lines.extend(f"- [ ] {item}" for item in context.acceptance_checks or ["未设置"])
    lines.extend(_render_quality_contract_section(context.quality_contract))
    lines.extend(_render_context_manifest_section(context.context_manifest))
    lines.extend(render_context_packs_section(context))
    lines.extend(_render_evidence_section(context))
    lines.extend(_render_pending_requests_section(context))
    lines.extend(_render_open_gaps_section(context))
    lines.extend(["", "## Execution Rules", ""])
    lines.append("- Finish the assigned task, write concrete refs, and hand results back to the caller.")
    lines.extend(f"- {item}" for item in context.instructions)
    return "\n".join(lines) + "\n"


# LLM: render_channel_probe_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总通道probemarkdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def render_channel_probe_markdown(report: ChannelProbeReport) -> str:
    lines = [
        "# SUBAGENT CHANNEL PROBE",
        "",
        f"- generated_at: {report.generated_at}",
        f"- total: {report.summary.get('total', 0)}",
        "",
        "## Summary",
        "",
    ]
    for key in sorted(report.summary):
        lines.append(f"- {key}: {report.summary[key]}")
    lines.extend(["", "## Results", ""])
    if not report.results:
        lines.append("- 暂无可检查的子代理记录")
    for result in report.results[:100]:
        failed = [check for check in result.checks if not check.ok]
        goal = result.goal.replace("\n", " ")[:100]
        lines.append(
            f"- `{result.run_id}` channel={result.channel_status} "
            f"failed_checks={len(failed)} :: {goal}"
        )
        for check in failed[:5]:
            lines.append(f"  - [{check.severity}] {check.name}: {check.summary} {check.error}".rstrip())
    return "\n".join(lines) + "\n"
# LLM: render_single_channel_probe_markdown 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总单个通道probemarkdown的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def render_single_channel_probe_markdown(result: ChannelProbeResult) -> str:
    lines = [
        "# CHANNEL PROBE",
        "",
        f"- run_id: {result.run_id}",
        f"- channel_status: {result.channel_status}",
        f"- created_at: {result.created_at}",
        f"- task_dir: {result.task_dir}",
        "",
        "## Checks",
        "",
    ]
    for check in result.checks:
        status = "OK" if check.ok else "FAIL"
        lines.append(
            f"- [{status}] {check.name} severity={check.severity} "
            f"evidence={check.evidence_path or 'none'}"
        )
        lines.append(f"  - {check.summary}")
        if check.error:
            lines.append(f"  - error: {check.error}")
    return "\n".join(lines) + "\n"
# LLM: _render_runner_item_line 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 渲染或汇总执行器条目line的展示文本，保持命令行、日志和审计输出一致；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _render_runner_item_line(item: dict[str, object]) -> str:
    title = (
        item.get("path")
        or item.get("name")
        or item.get("command")
        or item.get("summary")
        or item.get("description")
        or "item"
    )
    details = []
    for key in ["kind", "status", "ok", "summary", "description"]:
        if key in item and item[key] not in ("", None):
            details.append(f"{key}={item[key]}")
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"- {title}{suffix}"
