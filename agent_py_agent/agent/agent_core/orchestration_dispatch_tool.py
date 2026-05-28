# LLM: DispatchSubagentsTool is the model-facing dispatch orchestration tool.
# 模块用途: 承接 dispatch_subagents 工具入口，把模型参数转成 DispatchParams，并返回 refs-first 调度报告。

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..action_protocol import subagent_dispatch_envelope_from_payload
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..tools import BaseTool, ToolExecutionResult
from .dispatch_no_progress import dispatch_no_progress_payload
from .dispatch_params import DispatchParams
from .orchestration_dispatch_payload import (
    dispatch_record_payload,
    dispatch_recovery_payload,
)
from .orchestration_dispatch_refs import related_task_result_refs
from .orchestration_dispatch_scope import (
    dispatch_apply_default,
    dispatch_exclude_run_ids,
    dispatch_execute_runners_default,
    dispatch_include_run_ids_param,
    dispatch_max_runners_default,
    dispatch_parent_run_id,
    dispatch_take_over_by_default,
    dispatch_workflow_mode,
)
from .orchestration_dispatch_state_contract import dispatch_state_contract_payload
from .orchestration_dispatch_tool_helpers import _run_ids_from_dispatch
from .orchestration_progress_payload import direct_children_progress_payload
from .orchestration_run_scope import (
    remember_dispatched_orchestration_run_ids,
    remember_orchestration_run_ids,
)
from .orchestration_runner_instruction import resolved_runner_instruction
from .orchestration_scope_resolution import dispatch_scope_resolution, scope_resolution_payload
from .orchestration_tool_specs import build_dispatch_subagents_spec
from .orchestration_workflow_mode import tool_workflow_mode
from .parameters import _bool_param, _non_negative_int, _string_list

if TYPE_CHECKING:
    from ..core import SimpleAgent


# LLM: DispatchSubagentsTool must keep dispatch params, runner execution, and recovery payloads stable.
# 类用途: 提供 dispatch_subagents 模型工具入口，按父子作用域推进可运行的子代理，并把错误 run id 的恢复线索放到顶层。
class DispatchSubagentsTool(BaseTool):

    # LLM: __init__ wires a SimpleAgent facade to the dispatch tool spec.
    # 函数用途: 初始化调度工具依赖和模型可见 spec，后续 execute 会使用同一个 agent 状态。
    def __init__(self, agent: SimpleAgent):
        self.agent = agent
        self.spec = build_dispatch_subagents_spec()

    # LLM: execute runs dispatch planning or runner execution through the manager-owned service.
    # 函数用途: 解析调度参数、校验 execute/apply 组合、调用 agent.dispatch_subagents，并返回 JSON 报告。
    def execute(self, params: dict[str, object]) -> ToolExecutionResult:
        params = _normalized_dispatch_params(params)
        apply = dispatch_apply_default(self.agent, params)
        execute_runners = dispatch_execute_runners_default(self.agent, params, apply=apply)
        if execute_runners and not apply:
            return ToolExecutionResult(
                "dispatch_subagents",
                False,
                "execute_runners=true 必须配合 apply=true，避免误触发真实 API runner。",
            )

        dispatch_params = self._dispatch_params(params, apply, execute_runners)
        cfg, router = self._router()
        report = self.agent.dispatch_subagents(
            router,
            cfg,
            params=dispatch_params,
        )
        scoped_run_ids = _run_ids_for_scope(params, report, agent=self.agent)
        dispatched_run_ids = _run_ids_actually_dispatched(report)
        remember_orchestration_run_ids(self.agent, scoped_run_ids)
        remember_dispatched_orchestration_run_ids(self.agent, dispatched_run_ids)
        payload = self._report_payload(report, params=params, dispatch_params=dispatch_params)
        return ToolExecutionResult("dispatch_subagents", True, json.dumps(payload, ensure_ascii=False, indent=2))

    # LLM: _router builds the non-orchestration capability router used by dispatch planning.
    # 函数用途: 收集当前 agent 的普通工具规格，排除 orchestration 工具，构建 dispatch 所需 capability router。
    def _router(self) -> tuple[CapabilityConfig, CapabilityRouter]:
        cfg = _dispatch_capability_config(self.agent)
        tool_specs = [spec for spec in self.agent.tools.specs() if spec.category != "orchestration"]
        return cfg, CapabilityRouter(config=cfg, tool_specs=tool_specs)

    # LLM: _dispatch_params is the bundle boundary from model params into dispatch service params.
# 函数用途: 把模型传入的散字段收敛成 DispatchParams，集中处理父级作用域、执行开关和 run id 过滤。
    def _dispatch_params(
        self,
        params: dict[str, object],
        apply: bool,
        execute_runners: bool,
    ) -> DispatchParams:
        return DispatchParams(
            apply=apply,
            execute_runners=execute_runners,
            planner=_bool_param(params.get("planner"), default=False),
            workflow_mode=dispatch_workflow_mode(self.agent, params, tool_workflow_mode),
            max_runners=dispatch_max_runners_default(self.agent, params, execute_runners=execute_runners),
            limit=_non_negative_int(params.get("limit"), default=20),
            reviewer=str(params.get("reviewer") or "chat-tool").strip(),
            note=str(params.get("note") or "triggered by dispatch_subagents tool").strip(),
            runner_instruction=resolved_runner_instruction(
                self.agent,
                params.get("runner_instruction")
                or params.get("instruction")
                or params.get("prompt")
                or params.get("message")
                or params.get("guidance"),
            ),
            max_cards=_non_negative_int(params.get("max_cards"), default=0),
            probe=not _bool_param(params.get("no_probe"), default=False),
            take_over_by=dispatch_take_over_by_default(self.agent, params),
            locked_files=_string_list(params.get("locked_files")),
            parent_run_id=dispatch_parent_run_id(self.agent, params),
            root_id=str(params.get("root_id") or "").strip(),
            include_run_ids=dispatch_include_run_ids_param(params, agent=self.agent),
            exclude_run_ids=dispatch_exclude_run_ids(self.agent, params),
        )

    # LLM: _report_payload keeps dispatch output compact and recovery-friendly for the parent model.
    # 函数用途: 生成 dispatch_subagents 的 JSON 响应，先给父级结果索引，再给详细记录，避免长 records 截断关键 child 摘要。
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
            "dispatch_json": str(self.agent.subagents.workspace / "subagent_dispatch_report.json"),
            "dispatch_md": str(self.agent.subagents.workspace / "SUBAGENT_DISPATCH.md"),
        })
        payload["typed_envelope"] = subagent_dispatch_envelope_from_payload(payload).to_dict()
        return payload


from .orchestration_dispatch_tool_helpers import (
    _child_result_index_hint,
    _dispatch_capability_config,
    _dispatch_top_level_guidance,
    _normalized_dispatch_params,
    _run_ids_actually_dispatched,
    _run_ids_for_scope,
)


# LLM: _model_facing_dispatch_summary tolerates legacy string summaries from older dispatch reports.
# 函数用途: 把报告 summary 统一成对象，并把逐记录 dry_run/applied 计数改名，避免模型误读整次工具模式。
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
