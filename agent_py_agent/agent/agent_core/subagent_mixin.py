

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..backends import is_provider_timeout_error, is_provider_transient_error
from ..capability.config import CapabilityConfig
from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import RecoverySnapshotInput
from ..runtime_errors import runtime_error_report
from ..subagents import ParentPlannerRecord
from ..subagents.manager_runner_result_payload import RecordRunnerResultParams
from ..subagents.models import (
    FailureType,
    SubAgentRunnerResult,
    SubAgentTask,
    task_status_reason_code,
)
from ..subagents.parsing import parse_parent_planner_output, parse_subagent_runner_output
from ..subagents.services.dispatch.params import ParentPlannerRecordParams
from .planner_service import (
    PARENT_PLANNER_SYSTEM_PROMPT,
    PlannerPromptParams,
    append_planner_memory_hint,
)
from .planner_service import (
    build_parent_planner_prompt as _build_parent_planner_prompt,
)
from .planner_service import build_parent_planner_state as _build_parent_planner_state
from .provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .runner.prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_prompt,
    _build_subagent_runner_repair_prompt,
)
from .runtime.owner_roots import runtime_owner_root
from .subagent.finalize_helpers import (
    FinalizedRecoverySnapshotRequest,
    FinalizedRunnerRecordRequest,
    parsed_output_from_delivery_complete_response,
    record_finalized_runner_result,
    write_finalized_recovery_snapshot,
)
from .subagent.lifecycle_service import SubagentLifecycleService
from .subagent.params import (
    RecoverySnapshotParams,
    RunParentPlannerParams,
    SpawnSubagentsParams,
    SubagentFinalizeParams,
    SubagentProbeParams,
    SubagentRunFailureParams,
    SubagentRunParams,
    spawn_subagents_params,
    subagent_run_params,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SubagentRepairParams:
    context: object
    result: object
    structured: object
    prompt_for_log: str
    response_for_log: str
    backend_name: str
    message: str


@dataclass(frozen=True)
class PlannerLLMParams:
    state: dict
    execution_plan: object
    runner_instruction: str


@dataclass(frozen=True)
class PlannerRecordBuildParams:
    result: object
    state: dict
    gate_summary: dict
    mutate_state: bool
    runner_instruction: str


@dataclass(frozen=True)
class PlannerErrorRecordParams:
    gate_summary: dict
    mutate_state: bool
    runner_instruction: str
    runtime_error: dict[str, object] | None = None


@dataclass(frozen=True)
class PlannerLLMResult:
    response: object | None
    runtime_error: dict[str, object] | None = None


@dataclass(frozen=True)
class StructuredRunnerOutputRequest:
    agent: object
    params: SubagentFinalizeParams
    structured: object
    repair_state: dict[str, object]


def _initial_repair_state(result) -> dict[str, object]:
    return {
        "prompt_for_log": result.prompt,
        "response_for_log": result.response,
        "backend_name": result.backend,
        "message": "runner 已完成模型调用，等待独立验收。",
        "attempted": False,
        "ok": False,
        "error": "",
    }


def _tuple_repair_state(value: tuple) -> dict[str, object]:
    structured, ok, error, backend_name, prompt_for_log, response_for_log, message = value
    return {
        "structured": structured,
        "prompt_for_log": prompt_for_log,
        "response_for_log": response_for_log,
        "backend_name": backend_name,
        "message": message,
        "attempted": True,
        "ok": ok,
        "error": error,
    }


class _SubagentLifecycleBase:

    _subagent_lifecycle_service: SubagentLifecycleService | None = None

    def _get_subagent_lifecycle_service(self) -> SubagentLifecycleService:
        if self._subagent_lifecycle_service is None:
            self._subagent_lifecycle_service = SubagentLifecycleService(self)
        return self._subagent_lifecycle_service

    def spawn_subagents(
        self,
        goal: str | None = None,
        count: int | None = None,
        *,
        params: SpawnSubagentsParams | None = None,
    ) -> list[SubAgentTask]:

        options = spawn_subagents_params(
            params,
            goal=goal,
            count=count,
        )
        return self._get_subagent_lifecycle_service().spawn_subagents(options)

    def run_subagent(
        self,
        run_id: str | None = None,
        *,
        params: SubagentRunParams | None = None,
        instruction: str = "",
        dry_run: bool = True,
        max_cards: int = 0,
        probe: bool = True,
        retry_reason: str = "",
        attempt_id: str = "",
    ) -> SubAgentRunnerResult:
        options = subagent_run_params(
            params,
            run_id=run_id,
            instruction=instruction,
            dry_run=dry_run,
            max_cards=max_cards,
            probe=probe,
            retry_reason=retry_reason,
            attempt_id=attempt_id,
        )
        return self._get_subagent_lifecycle_service().run_subagent(options)

    def _build_subagent_prompt(self, run_id, max_cards, instruction):
        return _build_subagent_prompt(self, run_id, max_cards, instruction)

    def _record_subagent_dry_run(self, run_id, active_attempt_id, prompt):
        return _record_subagent_dry_run(self, run_id, active_attempt_id, prompt)

    def _probe_subagent_channel(self, params: SubagentProbeParams):
        return _probe_subagent_channel(self, params)

    def _handle_subagent_repair(self, params: SubagentRepairParams):
        return _handle_subagent_repair(self, params)

    def _write_subagent_recovery_snapshot(
        self,
        run_id: str | None = None,
        *,
        params: RecoverySnapshotParams | None = None,
        user_prompt: str = "",
        response_text: str = "",
        backend: str = "",
        status: str = "",
        error_code: str = "",
        tool_calls: list[dict] | None = None,
    ) -> None:
        _write_subagent_recovery_snapshot(
            self,
            _recovery_snapshot_params(
                params,
                run_id=run_id,
                user_prompt=user_prompt,
                response_text=response_text,
                backend=backend,
                status=status,
                error_code=error_code,
                tool_calls=tool_calls,
            ),
        )

    def _handle_subagent_run_failure(self, params: SubagentRunFailureParams):
        return _handle_subagent_run_failure(self, params)

    def _finalize_subagent_run(self, params: SubagentFinalizeParams):
        return _finalize_subagent_run(self, params)

    def run_parent_planner(self, params: RunParentPlannerParams) -> ParentPlannerRecord:
        return _run_parent_planner(self, params)


def _build_subagent_prompt(agent, run_id, max_cards, instruction):
    context = agent.subagents.runner_context.write_execution_context(run_id, max_cards=max_cards)
    prompt = _build_subagent_runner_prompt(context, instruction)
    return context, prompt


def _record_subagent_dry_run(agent, run_id, active_attempt_id, prompt):
    return agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=run_id,
            attempt_id=active_attempt_id,
            dry_run=True,
            ok=True,
            message="dry-run: 已生成执行上下文和 runner prompt，未调用模型。",
            prompt=prompt,
        )
    )


def _probe_subagent_channel(agent, params: SubagentProbeParams):
    if not params.probe:
        return None
    probe_result = agent.subagents.channel_probe.probe_channel(params.run_id)
    if probe_result.channel_status != "BROKEN":
        return None
    context, prompt = agent._build_subagent_prompt(params.run_id, params.max_cards, params.instruction)
    return agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            ok=False,
            message="通道健康检查为 BROKEN，未启动模型执行。",
            prompt=prompt,
            status="CHANNEL_ERROR",
            verification_status="UNVERIFIED",
            failure_type=FailureType.CHANNEL.value,
        )
    )


def _handle_subagent_repair(agent, params: SubagentRepairParams):
    repair_prompt = _build_subagent_runner_repair_prompt(
        params.context,
        original_prompt=params.result.prompt,
        original_response=params.result.response,
        parse_error=params.structured.parse_error,
    )
    try:
        repair_response = run_with_provider_transient_auto_resume(
            lambda: agent.backend.generate(repair_prompt),
            policy=getattr(agent, "runtime_guard_policy", None),
        )
        truncated = _is_truncated_result_failure(parse_subagent_runner_output(repair_response.text))
        if truncated and (compact_result := _compact_retry_on_truncation(agent, params, repair_prompt)) is not None:
            return compact_result
    except Exception as exc:
        return _repair_failure_tuple(params, exc)
    return _subagent_repair_tuple(params, repair_prompt, repair_response)


# LLM: 截断特征专项二试(R11a 实锤:正文很长时模型连修复轮也把结果块输出到
#   max_tokens 截断——旧链 repair 仅一次机会,两次截断即 BLOCKED 终判,干完的
#   活被状态掩埋。仅"块开了头但没闭合/JSON 不完整"的截断特征触发,追加硬约束
#   让模型给极简块;其他失败形态不重试,保持保守)。
# 函数用途: 修复轮又被截断时,再给一次"只许极简块"的机会;救回返回 tuple,
#   救不回返回 None 交回原链。
def _compact_retry_on_truncation(agent, params: SubagentRepairParams, repair_prompt: str):
    compact_prompt = repair_prompt + (
        "\n\n## Retry Constraint\n你上一次的修复输出又被长度截断。这次必须极简:"
        "summary 不超过 100 字;evidence/artifacts/tests/lessons 各最多 2 条;"
        "绝不复述正文内容,只输出结果块本身。\n"
    )
    retry_response = run_with_provider_transient_auto_resume(
        lambda: agent.backend.generate(compact_prompt),
        policy=getattr(agent, "runtime_guard_policy", None),
    )
    retried = parse_subagent_runner_output(retry_response.text)
    if retried.found and retried.ok:
        return _subagent_repair_tuple(params, compact_prompt, retry_response)
    return None


# 函数用途: 这次解析失败像不像"输出被截断"?(块开了头没闭合,或 JSON 没读完)
def _is_truncated_result_failure(parsed) -> bool:
    if not parsed.found or parsed.ok:
        return False
    error = str(getattr(parsed, "parse_error", "") or "")
    return ("结束标记" in error) or ("Unterminated" in error) or ("Expecting" in error)


def _subagent_repair_tuple(params: SubagentRepairParams, repair_prompt: str, repair_response):
    repaired = parse_subagent_runner_output(repair_response.text)
    prompt_for_log = _append_runner_repair_prompt(params.result.prompt, repair_prompt)
    response_for_log = _append_runner_repair_response(params.result.response, repair_response.text)
    backend_name = repair_response.backend or params.result.backend
    structured = params.structured
    message = params.message
    structured_repair_ok = False
    structured_repair_error = ""

    if repaired.found and repaired.ok:
        structured = repaired
        structured_repair_ok = True
        message = "runner 已完成模型调用，并已修复结构化结果，等待独立验收。"
    elif not structured.found and repaired.found:
        structured = repaired
        structured_repair_error = repaired.parse_error
    else:
        structured_repair_error = repaired.parse_error or "repair response still missing structured output"

    return (
        structured,
        structured_repair_ok,
        structured_repair_error,
        backend_name,
        prompt_for_log,
        response_for_log,
        message,
    )


def _write_subagent_recovery_snapshot(agent, snapshot: RecoverySnapshotParams) -> None:
    if not bool(getattr(agent.config, "memory_hook_enabled", True)):
        return
    try:
        task = agent.subagents.load(snapshot.run_id)
    except (FileNotFoundError, TypeError):
        task = None
    write_recovery_snapshot(
        _subagent_recovery_snapshot_root(agent, task),
        params=_recovery_snapshot_input(agent, snapshot, _recovery_content_paths(task)),
    )


def _handle_subagent_run_failure(agent, params: SubagentRunFailureParams):
    failure_type = _subagent_run_failure_type(params.exc)
    failed_result = agent.subagents.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=params.run_id,
            attempt_id=params.active_attempt_id,
            dry_run=False,
            ok=False,
            message=f"runner 执行失败: {params.exc}",
            prompt=params.prompt,
            status="BLOCKED",
            verification_status="UNVERIFIED",
            failure_type=failure_type,
        )
    )
    agent._write_subagent_recovery_snapshot(
        params=RecoverySnapshotParams(
            run_id=params.run_id,
            user_prompt=params.context.goal,
            response_text=failed_result.message,
            backend="",
            status=failed_result.status,
            error_code=failure_type,
            tool_calls=[],
        )
    )
    return failed_result


def _finalize_subagent_run(agent, params: SubagentFinalizeParams):
    structured = parse_subagent_runner_output(params.result.response)
    repair_state = _initial_repair_state(params.result)
    structured, repair_state = _structured_or_repaired_runner_output(StructuredRunnerOutputRequest(
        agent=agent,
        params=params,
        structured=structured,
        repair_state=repair_state,
    ))
    recovered = _recover_with_registered_products(agent, params, structured)
    if recovered is not structured:
        repair_state["message"] = recovered.summary
        structured = recovered
    runner_result = record_finalized_runner_result(
        FinalizedRunnerRecordRequest(agent, params, structured, repair_state)
    )
    write_finalized_recovery_snapshot(FinalizedRecoverySnapshotRequest(agent, params, runner_result, repair_state))
    return runner_result


def _structured_or_repaired_runner_output(request: StructuredRunnerOutputRequest):
    structured = request.structured
    repair_state = request.repair_state
    if not (structured.found and structured.ok):
        delivery_structured = parsed_output_from_delivery_complete_response(request.params.result.response)
        if delivery_structured is not None:
            structured = delivery_structured
            repair_state["message"] = "runner 已完成模型调用，运行时 delivery closeout 已通过。"
    if structured.found and structured.ok:
        return structured, repair_state
    repaired = request.agent._handle_subagent_repair(
        SubagentRepairParams(
            context=request.params.context,
            result=request.params.result,
            structured=structured,
            prompt_for_log=repair_state["prompt_for_log"],
            response_for_log=repair_state["response_for_log"],
            backend_name=repair_state["backend_name"],
            message=repair_state["message"],
        )
    )
    return repaired[0], _tuple_repair_state(repaired)


def _recover_with_registered_products(agent, params: SubagentFinalizeParams, structured):
    """产出事实兜底(C3/G4 实锤):两类形式问题不该埋没已落地的合格产物——
    ①无可解析结果块(parse/delivery/repair 全失败);②子代理幻觉式自我否定
    (输出了有效结果块但 status=BLOCKED,blocked_reason 却与产出事实矛盾,如声称
    "缺少核心交付物"而 registry 已登记)。两种情况若 registry 有 ready 产物
    (通过 validate 的合格交付)即据产出收尾 DONE。带 open capability_request 的
    阻塞是正当求助(交父代理 resolve),不在此列。无 ready 产物则尊重原判
    (真失败/真未完成不会被误判 DONE)。"""
    if structured.found and structured.ok and not _is_self_negated_block(structured):
        return structured
    recovered = _structured_from_registered_products(agent, params)
    if recovered is None:
        return structured
    if structured.found and structured.blocked_reason:
        recovered.summary = (
            f"{recovered.summary}（子代理曾声明阻塞:{str(structured.blocked_reason)[:80]}，"
            "但 registry 产出事实推翻该判断）"
        )
    return recovered


def _is_self_negated_block(structured) -> bool:
    """子代理输出了有效结果块但 status=BLOCKED 且无 open capability_request——
    基于自我评估声明阻塞,可能与产出事实矛盾。带 capability_request 的阻塞是正当
    求助,排除在外(尊重模型,交父代理 resolve)。"""
    if str(getattr(structured, "status", "") or "").strip().upper() != "BLOCKED":
        return False
    return not (getattr(structured, "capability_requests", None) or [])


def _structured_from_registered_products(agent, params: SubagentFinalizeParams):
    """产出事实兜底(C3/G4 实锤):runner 没产出可解析结果块(parse/delivery/repair
    全失败),但子代理已在 artifact_registry 登记 ready 产物(通过 validate 的合格文件)。
    据已登记产物收尾为 DONE,不把真实产出埋没成 BLOCKED、不让父代理空等或重做——
    结果块是形式,落地的合格产物才是交付事实。"""
    products = _registered_ready_products(agent, str(getattr(params, "run_id", "") or "").strip())
    if not products:
        return None
    from ..subagents.model_runtime import SubAgentParsedOutput

    names = "、".join(str(p["name"]) for p in products[:5])
    return SubAgentParsedOutput(
        found=True,
        ok=True,
        status="DONE",
        summary=f"runner 未输出可解析结果块,据 artifact_registry 已登记的 {len(products)} 个 ready 产物收尾:{names}",
        evidence=[{"kind": "registered_artifact", "path": p["path"], "bytes": p["bytes"]} for p in products],
    )


def _registered_ready_products(agent, run_id: str) -> list[dict[str, object]]:
    if not run_id:
        return []
    workspace = _subagent_workspace_dir(agent, run_id)
    if not workspace:
        return []
    from ..artifacts.registry import latest_artifact_records

    products: list[dict[str, object]] = []
    seen: set[str] = set()
    for record in latest_artifact_records(workspace).values():
        product = _ready_product_entry(record, seen)
        if product is not None:
            products.append(product)
    return products


def _subagent_workspace_dir(agent, run_id: str) -> str:
    try:
        task = agent.subagents.load(run_id)
    except (FileNotFoundError, TypeError):
        return ""
    return str(getattr(task, "task_workspace_dir", "") or "").strip()


def _ready_product_entry(record, seen: set[str]) -> dict[str, object] | None:
    if str(getattr(record, "status", "") or "").strip() != "ready":
        return None
    path = str(getattr(record, "path", "") or "").strip()
    if not path or path in seen:
        return None
    file_path = Path(path)
    if not file_path.is_file():
        return None
    if _registered_product_is_placeholder(file_path):
        return None
    seen.add(path)
    return {"path": path, "name": file_path.name, "bytes": file_path.stat().st_size}


def _registered_product_is_placeholder(file_path: Path) -> bool:
    """据 registry 产物兜底收尾的质量闸:空文件 / 系统兜底占位空壳不算合格交付,不能作为
    把子代理 BLOCKED 推翻成 DONE 的依据(否则占位据占位收尾、机制层糊弄)。兑现
    _recover_with_registered_products 注释承诺的"通过 validate 的合格产物"。复用交付
    验收门同一把尺(is_unfinished_placeholder_text),不另造阈值;二进制/非 UTF-8 产物
    读不出文本则不按占位拦(交回文件存在性,不误伤图片等)。"""
    try:
        if file_path.stat().st_size == 0:
            return True
        text = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    from ..contracts.artifact_structured_contracts import is_unfinished_placeholder_text

    return is_unfinished_placeholder_text(text)


class SimpleAgentSubagentMixin(_SubagentLifecycleBase):
    pass


def _run_parent_planner(agent, params: RunParentPlannerParams) -> ParentPlannerRecord:
    cfg = params.capability_config or CapabilityConfig()
    state = _build_parent_planner_state(
        agent,
        cfg,
        max_runners=params.max_runners,
        limit=params.limit,
        reviewer=params.reviewer,
        note=params.note,
    )
    gate_summary = {key: int(value) for key, value in state["gate"].items() if isinstance(value, int)}

    if not state["gate"].get("needs_planner", 0):
        return _make_heartbeat_ok_record(
            agent,
            not params.execution_plan.mutate_state,
            gate_summary,
        )

    result = _execute_planner_llm(
        agent,
        PlannerLLMParams(
            state=state,
            execution_plan=params.execution_plan,
            runner_instruction=params.runner_instruction,
        ),
    )
    if result.response is None:
        return _make_planner_error_record(
            agent,
            PlannerErrorRecordParams(
                gate_summary=gate_summary,
                runner_instruction=params.runner_instruction,
                mutate_state=params.execution_plan.mutate_state,
                runtime_error=result.runtime_error,
            ),
        )
    return _build_planner_record(
        agent,
        PlannerRecordBuildParams(
            result=result.response,
            state=state,
            gate_summary=gate_summary,
            mutate_state=params.execution_plan.mutate_state,
            runner_instruction=params.runner_instruction,
        ),
    )


def _make_heartbeat_ok_record(agent, dry_run, gate_summary):
    record = agent.subagents.parent_planner.make_parent_planner_record(
        params=ParentPlannerRecordParams(
            dry_run=dry_run,
            triggered=False,
            ok=True,
            decision="HEARTBEAT_OK",
            message="planner gate 确认无 active/pending/stalled/needs-intervention 事项，允许 HEARTBEAT_OK。",
            gate_summary=gate_summary,
            summary="no work",
        ),
    )
    report = agent.subagents.parent_planner.build_parent_planner_report(
        [record],
        dry_run=dry_run,
    )
    agent.subagents.parent_planner.write_parent_planner_report(report, append_log=False)
    return record


def _execute_planner_llm(agent, params: PlannerLLMParams):
    prompt = _build_parent_planner_prompt(
        params.state,
        params=PlannerPromptParams(
            params.execution_plan,
            params.runner_instruction,
        ),
    )
    # 记忆推模式(B1):planner 决策点自动注入相关教训(软注入,失败不阻断)。
    prompt = append_planner_memory_hint(agent, params.state, prompt)
    agent.subagents.parent_planner.write_parent_planner_exchange(prompt)
    try:
        response = agent.run(
            prompt,
            save=False,
            allowed_tools=[],
            resume_context=False,
            system_prompt_override=PARENT_PLANNER_SYSTEM_PROMPT,
            context_scope="control_plane",
        )
    except Exception as exc:
        return PlannerLLMResult(
            response=None,
            runtime_error=runtime_error_report(exc, context="parent_planner.run"),
        )
    return PlannerLLMResult(response=response)


def _make_planner_error_record(agent, params: PlannerErrorRecordParams):
    record = agent.subagents.parent_planner.make_parent_planner_record(
        params=ParentPlannerRecordParams(
            dry_run=not params.mutate_state,
            triggered=True,
            ok=False,
            decision="PLANNER_ERROR",
            message="父代理 planner 调用失败",
            gate_summary=params.gate_summary,
            runner_instruction=params.runner_instruction,
            runtime_error=params.runtime_error,
            evidence_paths=[],
        ),
    )
    report = agent.subagents.parent_planner.build_parent_planner_report(
        [record],
        dry_run=not params.mutate_state,
    )
    agent.subagents.parent_planner.write_parent_planner_report(
        report,
        append_log=params.mutate_state,
    )
    return record


def _build_planner_record(agent, params: PlannerRecordBuildParams):
    prompt_path, response_path = agent.subagents.parent_planner.write_parent_planner_exchange(
        params.result.prompt,
        params.result.response,
    )
    parsed = parse_parent_planner_output(params.result.response)
    ok, decision, message, parse_error = _planner_record_status(parsed, params.state)

    record = agent.subagents.parent_planner.make_parent_planner_record(
        params=ParentPlannerRecordParams(
            dry_run=not params.mutate_state,
            triggered=True,
            ok=ok,
            decision=decision,
            message=message,
            gate_summary=params.gate_summary,
            backend=params.result.backend,
            tool_rounds=params.result.tool_rounds,
            parse_error=parse_error,
            summary=parsed.summary,
            actions=parsed.actions,
            blockers=parsed.blockers,
            risks=parsed.risks,
            notes=parsed.notes,
            runner_instruction=parsed.runner_instruction,
            suggested_max_runners=parsed.suggested_max_runners,
            prompt_path=prompt_path,
            response_path=response_path,
            evidence_paths=[prompt_path, response_path],
        ),
    )
    report = agent.subagents.parent_planner.build_parent_planner_report(
        [record],
        dry_run=not params.mutate_state,
    )
    agent.subagents.parent_planner.write_parent_planner_report(
        report,
        append_log=params.mutate_state,
    )
    return record


def _repair_failure_tuple(params: SubagentRepairParams, exc: Exception) -> tuple:
    return (
        params.structured,
        False,
        str(exc),
        params.backend_name,
        params.prompt_for_log,
        _append_runner_repair_failure(params.result.response, exc),
        params.message,
    )


def _recovery_snapshot_params(
    params: RecoverySnapshotParams | None,
    *,
    run_id: str | None,
    user_prompt: str,
    response_text: str,
    backend: str,
    status: str,
    error_code: str,
    tool_calls: list[dict] | None,
) -> RecoverySnapshotParams:
    if params is not None:
        if not isinstance(params, RecoverySnapshotParams):
            raise TypeError("subagent recovery snapshot requires params: RecoverySnapshotParams")
        return params
    return RecoverySnapshotParams(
        run_id=str(run_id or ""),
        user_prompt=str(user_prompt),
        response_text=str(response_text),
        backend=str(backend),
        status=str(status),
        error_code=str(error_code),
        tool_calls=list(tool_calls or []),
    )


def _recovery_content_paths(task) -> list[str]:
    if task is None:
        return []
    return [
        task.status_file,
        task.work_log_file,
        task.runner_result_file,
        task.runner_result_json,
        task.output_json,
        task.handoff_file,
    ]


def _subagent_recovery_snapshot_root(agent, task: SubAgentTask | None) -> Path:
    raw_workspace = getattr(task, "agent_run_workspace_dir", "") if task is not None else ""
    workspace = str(raw_workspace).strip() if isinstance(raw_workspace, str | Path) else ""
    if workspace:
        return Path(workspace).expanduser().resolve(strict=False)
    return runtime_owner_root(agent)


def _recovery_snapshot_input(
    agent,
    snapshot: RecoverySnapshotParams,
    content_paths: list[str],
):
    next_actions = [
        "先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"
    ]
    return RecoverySnapshotInput(
        session_id=getattr(agent, "session_id", agent.config.agent_name),
        user_prompt=snapshot.user_prompt,
        response_text=snapshot.response_text,
        backend=snapshot.backend,
        source="subagent_run",
        request_id=f"subagent-run:{snapshot.run_id}",
        run_id=snapshot.run_id,
        task_id=snapshot.run_id,
        status=task_status_reason_code(snapshot.status) or "unknown",
        error_code=snapshot.error_code,
        tool_calls=snapshot.tool_calls,
        task_refs=[snapshot.run_id],
        content_paths=content_paths,
        next_actions=next_actions,
        archive_level=int(getattr(agent.config, "memory_hook_archive_level", 3)),
    )


def _planner_record_status(parsed, state: dict) -> tuple[bool, str, str, str]:
    ok = parsed.found and parsed.ok
    decision = parsed.decision or "PARSE_ERROR"
    message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
    parse_error = parsed.parse_error
    if not parsed.found:
        return (
            False,
            "PARSE_ERROR",
            "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。",
            "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。",
        )
    if parsed.decision == "HEARTBEAT_OK" and state["gate"].get("needs_planner", 0):
        return (
            False,
            decision,
            "状态快照仍有待处理事项，planner 只返回了 HEARTBEAT_OK；请给出推进建议。",
            parse_error or "planner_empty_heartbeat_with_pending_work",
        )
    return ok, decision, message, parse_error


def _subagent_run_failure_type(exc: BaseException) -> str:
    if is_provider_timeout_error(exc):
        return FailureType.PROVIDER_TIMEOUT.value
    if is_provider_transient_error(exc):
        return FailureType.TRANSIENT_ERROR.value
    return FailureType.RUNNER_ERROR.value
