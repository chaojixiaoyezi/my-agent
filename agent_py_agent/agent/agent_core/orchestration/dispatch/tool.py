
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.capability.config import CapabilityConfig

from ....action_protocol import subagent_dispatch_envelope_from_payload
from ....common.value_parsing import TOOL_TEXT_LIST_OPTIONS, string_list
from ....model_visible_refs import current_model_ref
from ....runtime_errors import runtime_error_report
from ....tooling.models import BaseTool, ToolExecutionResult
from ...parameters import _bool_param, _non_negative_int
from ..run_scope import (
    remember_dispatched_orchestration_run_ids,
    remember_orchestration_run_ids,
)
from ..scope_resolution import dispatch_scope_resolution, scope_resolution_payload
from ..tool_specs import build_dispatch_subagents_spec
from .no_progress import dispatch_no_progress_payload
from .params import DispatchExecutionPlan, DispatchParams
from .payload import (
    dispatch_record_payload,
    dispatch_recovery_payload,
)
from .progress_payload import direct_children_progress_payload
from .refs import related_task_result_refs
from .scope import (
    dispatch_apply_default,
    dispatch_exclude_run_ids,
    dispatch_include_run_ids_param,
    dispatch_max_runners_default,
    dispatch_parent_run_id,
    dispatch_start_runners_default,
    dispatch_take_over_by_default,
)
from .state_contract import dispatch_state_contract_payload

if TYPE_CHECKING:
    from ....core import SimpleAgent


def _unsupported_execution_param_error(params: dict[str, object]) -> str:
    unsupported = [key for key in ("apply", "start_runners", "execute_runners") if key in (params or {})]
    if not unsupported:
        return ""
    return (
        "dispatch_subagents 模型入口只接受 dry_run 作为执行开关；"
        f"请移除 {', '.join(unsupported)}，用 dry_run=true 预览，dry_run=false 真实推进。"
    )


@dataclass(frozen=True)
class DispatchToolRequest:
    """Boundary object for one model-visible dispatch_subagents call."""

    model_params: dict[str, object]
    dispatch_params: DispatchParams


def resolved_runner_instruction(agent: object, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    workspace_root = _agent_workspace_root(agent)
    if workspace_root:
        text = text.replace("{workspace_root}", workspace_root).replace("{{workspace_root}}", workspace_root)
    return text


def _agent_workspace_root(agent: object) -> str:
    raw = getattr(getattr(agent, "subagents", None), "workspace_root", None)
    if not isinstance(raw, str | Path):
        return ""
    return str(Path(raw).expanduser().resolve(strict=False))


class DispatchSubagentsTool(BaseTool):

    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_dispatch_subagents_spec()

    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        unsupported_error = _unsupported_execution_param_error(params)
        if unsupported_error:
            # 模型传了不支持的执行开关(apply/start_runners/execute_runners) → 改参数可修，
            # 给精确码而非无码兜底成 UNKNOWN_ERROR(否则模型以为该放弃而非移除多余参数重发)。
            return ToolExecutionResult(
                "dispatch_subagents", False, unsupported_error, error_code="TOOL_INVALID_ARGUMENTS"
            )
        request = self._request(params)
        guidance_ids, guidance_errors = _persist_dispatch_guidance(self.agent, request.dispatch_params)
        cfg, router = self._router()
        report = self.agent.dispatch_subagents(
            router,
            cfg,
            params=request.dispatch_params,
        )
        scoped_run_ids = _run_ids_for_scope(request.model_params, report, agent=self.agent)
        dispatched_run_ids = _run_ids_actually_dispatched(report)
        remember_orchestration_run_ids(self.agent, scoped_run_ids)
        remember_dispatched_orchestration_run_ids(self.agent, dispatched_run_ids)
        payload = self._report_payload(
            report,
            params=request.model_params,
            dispatch_params=request.dispatch_params,
        )
        if guidance_ids:
            payload["guidance_ids"] = guidance_ids
        if guidance_errors:
            payload["guidance_persist_errors"] = guidance_errors
        if load_error := getattr(self.agent, "_capability_config_load_error", None):
            payload["capability_config_load_error"] = load_error
        return ToolExecutionResult("dispatch_subagents", True, json.dumps(payload, ensure_ascii=False, indent=2))

    def _router(self) -> tuple[CapabilityConfig, CapabilityRouter]:
        cfg = _dispatch_capability_config(self.agent)
        router = getattr(self.agent, "capability_router", None)
        if not isinstance(router, CapabilityRouter):
            raise RuntimeError("agent capability router is unavailable")
        return cfg, router

    def _request(self, params: dict[str, object]) -> DispatchToolRequest:
        model_params = dict(params or {})
        dry_run = _bool_param(model_params.get("dry_run"), default=True)
        apply = dispatch_apply_default(self.agent, model_params, dry_run=dry_run)
        start_runners = dispatch_start_runners_default(self.agent, model_params, mutate_state=apply)
        if start_runners and not apply:
            raise ValueError("dispatch_subagents internal execution mode is inconsistent")
        plan = DispatchExecutionPlan.from_parts(
            mutate_state=apply,
            start_runners=start_runners,
            max_runners=dispatch_max_runners_default(self.agent, model_params, start_runners=start_runners),
        )
        return DispatchToolRequest(
            model_params=model_params,
            dispatch_params=self._dispatch_params(model_params, plan),
        )

    def _dispatch_params(
        self,
        params: dict[str, object],
        execution_plan: DispatchExecutionPlan,
    ) -> DispatchParams:
        return DispatchParams(
            execution_plan=execution_plan,
            planner=_bool_param(params.get("planner"), default=False),
            limit=_non_negative_int(params.get("limit"), default=20),
            reviewer=str(params.get("reviewer") or "chat-tool").strip(),
            note=str(params.get("note") or "triggered by dispatch_subagents tool").strip(),
            runner_instruction=resolved_runner_instruction(
                self.agent,
                params.get("runner_instruction"),
            ),
            recovery_mode=str(params.get("recovery_mode") or "").strip(),
            max_cards=_non_negative_int(params.get("max_cards"), default=0),
            probe=not _bool_param(params.get("no_probe"), default=False),
            take_over_by=dispatch_take_over_by_default(self.agent, params),
            locked_files=string_list(params.get("locked_files"), TOOL_TEXT_LIST_OPTIONS),
            parent_run_id=dispatch_parent_run_id(self.agent, params),
            root_id=str(params.get("root_id") or "").strip(),
            include_run_ids=dispatch_include_run_ids_param(params, agent=self.agent),
            exclude_run_ids=dispatch_exclude_run_ids(self.agent, params),
        )

    def _report_payload(
        self,
        report,
        *,
        params: dict[str, object] | None = None,
        dispatch_params: DispatchParams | None = None,
    ) -> dict[str, object]:
        record_payloads = [dispatch_record_payload(item) for item in report.records]
        result_index = related_task_result_refs(self.agent, report)
        resolution = dispatch_scope_resolution(
            self.agent,
            params or {},
            effective_parent_run_id=str(getattr(dispatch_params, "parent_run_id", "") or ""),
            effective_root_id=str(getattr(dispatch_params, "root_id", "") or ""),
            effective_run_ids=list(getattr(dispatch_params, "include_run_ids", None) or []),
        )
        payload = {
            "dry_run": report.dry_run,
            "runner_selection_recovery": dispatch_recovery_payload(report.records),
            "summary": _model_facing_dispatch_summary(report.summary),
        }
        payload.update(scope_resolution_payload(resolution))
        payload.update(_dispatch_top_level_guidance(self.agent, report, record_payloads))
        if hint := dispatch_no_progress_payload(report):
            payload["dispatch_no_progress_hint"] = hint
        payload.update(dispatch_state_contract_payload(self.agent))
        payload.update(direct_children_progress_payload(self.agent))
        payload.update({
            "child_result_index": result_index,
            "child_result_index_hint": _child_result_index_hint(result_index),
            "result_refs_by_run": result_index,
            "records": record_payloads,
            "dispatch_json": current_model_ref(self.agent.subagents.workspace / "subagent_dispatch_report.json"),
            "dispatch_md": current_model_ref(self.agent.subagents.workspace / "SUBAGENT_DISPATCH.md"),
        })
        payload["typed_envelope"] = subagent_dispatch_envelope_from_payload(payload).to_dict()
        return payload


from .tool_helpers import (
    _child_result_index_hint,
    _dispatch_capability_config,
    _dispatch_top_level_guidance,
    _run_ids_actually_dispatched,
    _run_ids_for_scope,
)


def _model_facing_dispatch_summary(summary: object) -> dict[str, object]:
    """Rename per-record dry-run counts so models do not confuse them with tool mode."""
    if isinstance(summary, dict):
        rendered = dict(summary)
    elif summary:
        rendered = {"text": str(summary)}
    else:
        rendered = {}
    if "dry_run" in rendered:
        rendered["record_dry_run_count"] = rendered.pop("dry_run")
    if "applied" in rendered:
        rendered["record_applied_count"] = rendered.pop("applied")
    return rendered


def _persist_dispatch_guidance(agent: object, params: DispatchParams) -> tuple[list[str], list[dict[str, object]]]:
    instruction = str(getattr(params, "runner_instruction", "") or "").strip()
    run_ids = [str(item).strip() for item in (getattr(params, "include_run_ids", None) or []) if str(item or "").strip()]
    if not instruction or not run_ids:
        return [], []
    store = getattr(agent, "conversation_store", None)
    if store is None:
        return [], []
    guidance_ids: list[str] = []
    guidance_errors: list[dict[str, object]] = []
    for run_id in run_ids:
        try:
            entry = store.append_guidance(
                {
                    "target_type": "agent_run",
                    "target_id": run_id,
                    "message": instruction,
                    "sender": "dispatch_subagents",
                    "metadata": {"tool_name": "dispatch_subagents"},
                }
            )
        except Exception as exc:
            guidance_errors.append(_guidance_persist_error(run_id, exc))
            continue
        guidance_id = getattr(entry, "guidance_id", "")
        if isinstance(guidance_id, str) and guidance_id:
            guidance_ids.append(guidance_id)
    return guidance_ids, guidance_errors


def _guidance_persist_error(run_id: str, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="dispatch.guidance.persist")
    return {"run_id": run_id, **report}
