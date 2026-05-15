# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Structured runner-output processing for subagent results."""

from dataclasses import dataclass

from .capability_request_identity import find_equivalent_capability_request
from .models import (
    CapabilityRequest,
    SubAgentParsedOutput,
    SubAgentTask,
    VerificationEvidence,
)
from .parsing import _normalize_runner_items, _split_allowed_items, _string_dict, _string_list
from .result_artifact_evidence import merge_artifact_evidence, normalize_artifact_items
from .result_structured_evidence import (
    process_evidence_items,
    process_evidence_packets,
    process_findings,
)
from .utils import _merge_list, _new_id

# LLM: artifact refs are normalized before merge so parent agents can read durable task-local paths.
# 函数用途: 本文件合并 structured output 时先把模型短路径修正为真实 artifact refs，再进入验收链路。


# LLM: MergeActualToolsParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存mergeactual工具参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class MergeActualToolsParams:

    task: SubAgentTask
    actual_tools: list[str]
    used_tools: list[str]
    allowed_tools: set[str]
    parsed_used_tools: list[str]
    now: float


# LLM: MergeTaskToolsParams 属于子代理任务管理的类边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 类用途: 集中保存merge任务工具参数字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class MergeTaskToolsParams:

    task: SubAgentTask
    used_tools: list[str]
    used_skills: list[str]
    actual_tools: list[str] | None
    parsed_used_tools: list[str]
    now: float


# LLM: _merge_actual_tools 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新actual工具对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _merge_actual_tools(params: MergeActualToolsParams):
    task = params.task
    actual_allowed_tools = [item for item in params.actual_tools if item in params.allowed_tools]
    task.used_tools = _merge_list(task.used_tools, actual_allowed_tools)
    if not params.actual_tools:
        return list(params.parsed_used_tools)
    ignored_tools = [item for item in task.used_tools if item not in actual_allowed_tools]
    for tool_name in actual_allowed_tools:
        if not any(
            item.ok
            and (
                item.kind == tool_name
                or item.command == tool_name
                or item.command.startswith(f"{tool_name} ")
            )
            for item in task.evidence
        ):
            task.evidence.append(
                VerificationEvidence(
                    kind=tool_name,
                    summary=f"系统记录 runner 实际执行过 {tool_name}。",
                    command=tool_name,
                    ok=True,
                    created_at=params.now,
                )
            )
    return ignored_tools


# LLM: _create_evidence_from_parsed 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建来自证据parsed所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _create_evidence_from_parsed(parsed, now):
    count = 0
    for item in parsed.evidence:
        summary = str(item.get("summary", "")).strip()
        if summary:
            count += 1
    return count


# LLM: _create_capability_requests_from_parsed 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建来自能力requestsparsed所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 可能触发网络输入输出或消费流式响应，需保留错误传播语义。
def _create_capability_requests_from_parsed(task, parsed, now):
    count = 0
    created_ids = []
    for item in parsed.capability_requests:
        problem = str(item.get("problem", "")).strip()
        needed = str(item.get("needed_capability", "")).strip()
        if not problem or not needed:
            continue
        request = CapabilityRequest(
            id=_new_id("capreq"),
            from_run_id=task.id,
            problem=problem,
            needed_capability=needed,
            expected_output=str(item.get("expected_output", "") or ""),
            capability_type=str(item.get("capability_type", "generic") or "generic"),
            tried=_string_list(item.get("tried", [])),
            evidence=_string_list(item.get("evidence", [])),
            constraints=_string_dict(item.get("constraints", {})),
            requested_tools=_string_list(item.get("requested_tools", [])),
            requested_skills=_string_list(item.get("requested_skills", [])),
            requested_mcp_tools=_string_list(item.get("requested_mcp_tools", [])),
            requested_commands=_string_list(item.get("requested_commands", [])),
            cwd_scope=_string_list(item.get("cwd_scope", [])),
            path_scope=_string_list(item.get("path_scope", [])),
            network_scope=_string_list(item.get("network_scope", [])),
            output_budget=_object_dict(item.get("output_budget", {})),
            risk_level=str(item.get("risk_level", "") or ""),
            fallback_attempted=_string_list(item.get("fallback_attempted", [])),
            escalation_target=str(item.get("escalation_target", "") or ""),
            created_at=now,
            reserved=_object_dict(item.get("reserved", {})),
        )
        if existing := find_equivalent_capability_request(task.capability_requests, request):
            created_ids.append(existing.id)
            count += 1
            continue
        task.capability_requests.append(request)
        created_ids.append(request.id)
        count += 1
    return count, created_ids


# LLM: _object_dict preserves JSON-like scope/budget values without stringifying nested data.
# 函数用途: 把模型给出的对象字段规范成普通字典，用于能力申请里的范围、预算和预留字段。
def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items()}


# LLM: _split_tools_and_skills 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 拆分工具skills输入集合，给调度、验收或补丁处理提供分组结果；关键副作用: 需保持任务状态、执行器结果、验收和报告展示上的返回值和副作用边界稳定。
def _split_tools_and_skills(parsed, allowed_tools, allowed_skills):
    used_tools, ignored_tools = _split_allowed_items(parsed.used_tools, allowed_tools)
    used_skills, ignored_skills = _split_allowed_items(parsed.used_skills, allowed_skills)
    return used_tools, ignored_tools, used_skills, ignored_skills


# LLM: _merge_task_tools 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 更新任务工具对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、执行器结果、验收和报告展示，需避免破坏既有状态机约定。
def _merge_task_tools(params: MergeTaskToolsParams):
    if params.actual_tools is not None:
        return _merge_actual_tools(
            MergeActualToolsParams(
                params.task,
                params.actual_tools,
                params.used_tools,
                set(params.task.allowed_tools),
                params.parsed_used_tools,
                params.now,
            )
        )
    params.task.used_tools = _merge_list(params.task.used_tools, params.used_tools)
    return []


# LLM: merge_actual_tools_for_unparsed preserves executed tool facts even when result JSON is truncated.
# 函数用途: 结构化结果解析失败时，仍把 runner loop 实际执行过的授权工具写回 task.used_tools 和证据，供验收与调试读取。
def merge_actual_tools_for_unparsed(task: SubAgentTask, actual_tools: list[str] | None, now: float) -> list[str]:
    if actual_tools is None:
        return []
    return _merge_task_tools(MergeTaskToolsParams(task, [], [], actual_tools, [], now))


# LLM: _normalize_parsed_fields 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 解析并归一化parsed字段的输入形态，让下游只处理稳定结构；关键副作用: 主要返回派生结构或文本，需保持字段名、顺序和空值处理稳定。
def _normalize_parsed_fields(parsed):
    return {
        "artifacts": _normalize_runner_items(parsed.artifacts),
        "tests": _normalize_runner_items(parsed.tests),
        "patches": _normalize_runner_items(parsed.patches),
        "lessons": parsed.lessons,
        "next_actions": parsed.next_actions,
    }


# LLM: _process_structured_output 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 推进structuredoutput的运行阶段，串接调度、等待、回写或错误处理；关键副作用: 会影响任务状态、执行器结果、验收和报告展示，需保持重试、超时和状态迁移语义。
def _process_structured_output(
    task: SubAgentTask,
    parsed: SubAgentParsedOutput,
    now: float,
    actual_tools: list[str] | None,
) -> dict[str, object]:
    allowed_tools = set(task.allowed_tools)
    allowed_skills = set(task.allowed_skills)
    used_tools, ignored_tools, used_skills, ignored_skills = _split_tools_and_skills(
        parsed, allowed_tools, allowed_skills
    )
    if actual_tools is not None:
        ignored_tools = _merge_task_tools(
            MergeTaskToolsParams(task, used_tools, used_skills, actual_tools, parsed.used_tools, now)
        )
    else:
        task.used_tools = _merge_list(task.used_tools, used_tools)
    task.used_skills = _merge_list(task.used_skills, used_skills)

    structured_evidence_count = process_evidence_items(parsed, task, now)
    evidence_packets = process_evidence_packets(parsed, task, now)
    findings = process_findings(parsed, task, now)
    structured_request_count, created_request_ids = _create_capability_requests_from_parsed(task, parsed, now)
    normalized = _normalize_parsed_fields(parsed)
    normalized["artifacts"] = normalize_artifact_items(task, normalized["artifacts"])
    evidence_packets = merge_artifact_evidence(
        task,
        normalized["artifacts"],
        evidence_packets,
        now,
    )
    task.blockers = _merge_list(task.blockers, [parsed.blocked_reason] if parsed.blocked_reason else [])
    return {
        "ignored_tools": ignored_tools,
        "ignored_skills": ignored_skills,
        "structured_evidence_count": structured_evidence_count,
        "structured_request_count": structured_request_count,
        "evidence_packets": evidence_packets,
        "findings": findings,
        "created_request_ids": created_request_ids,
        "artifacts": normalized["artifacts"],
        "tests": normalized["tests"],
        "patches": normalized["patches"],
        "lessons": normalized["lessons"],
        "next_actions": normalized["next_actions"],
    }
