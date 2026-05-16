# LLM: DispatchSubagentsTool is the model-facing dispatch orchestration tool.
# 模块用途: 承接 dispatch_subagents 工具入口，把模型参数转成 DispatchParams，并返回 refs-first 调度报告。

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from ..action_protocol import subagent_dispatch_envelope_from_payload
from ..capabilities import CapabilityRouter
from ..capability.runtime_config import (
    default_capability_config_path,
    load_capability_config_snapshot,
)
from ..capability_config import CapabilityConfig
from ..tools import BaseTool, ToolExecutionResult
from .dispatch_no_progress import dispatch_no_progress_payload
from .dispatch_params import DispatchParams
from .orchestration_artifact_integrity_repair import artifact_integrity_repair_advice_from_records
from .orchestration_dispatch_payload import dispatch_record_payload, dispatch_recovery_payload
from .orchestration_dispatch_refs import related_task_refs, related_task_result_refs
from .orchestration_dispatch_scope import (
    dispatch_apply_default,
    dispatch_auto_apply_acceptance_followup_default,
    dispatch_exclude_run_ids,
    dispatch_execute_acceptance_tests_default,
    dispatch_execute_runners_default,
    dispatch_finalize_acceptance,
    dispatch_max_runners_default,
    dispatch_parent_run_id,
    dispatch_take_over_by_default,
    dispatch_workflow_mode,
)
from .orchestration_progress_payload import direct_children_progress_payload
from .orchestration_run_scope import (
    remember_orchestration_run_ids,
    remembered_orchestration_run_ids,
)
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
        apply = dispatch_apply_default(self.agent, params)
        execute_runners = dispatch_execute_runners_default(self.agent, params, apply=apply)
        if execute_runners and not apply:
            return ToolExecutionResult(
                "dispatch_subagents",
                False,
                "execute_runners=true 必须配合 apply=true，避免误触发真实 API runner。",
            )

        cfg, router = self._router()
        report = self.agent.dispatch_subagents(
            router,
            cfg,
            params=self._dispatch_params(params, apply, execute_runners),
        )
        remember_orchestration_run_ids(self.agent, _run_ids_from_dispatch(params, report))
        payload = self._report_payload(report)
        return ToolExecutionResult("dispatch_subagents", True, json.dumps(payload, ensure_ascii=False, indent=2))

    # LLM: _router builds the non-orchestration capability router used by dispatch planning.
    # 函数用途: 收集当前 agent 的普通工具规格，排除 orchestration 工具，构建 dispatch 所需 capability router。
    def _router(self) -> tuple[CapabilityConfig, CapabilityRouter]:
        cfg = _dispatch_capability_config(self.agent)
        tool_specs = [spec for spec in self.agent.tools.specs() if spec.category != "orchestration"]
        return cfg, CapabilityRouter(config=cfg, tool_specs=tool_specs)

    # LLM: _dispatch_params is the bundle boundary from model params into dispatch service params.
    # 函数用途: 把模型传入的散字段收敛成 DispatchParams，集中处理父级作用域、执行开关、验收跟进和 run id 过滤。
    def _dispatch_params(
        self,
        params: dict[str, object],
        apply: bool,
        execute_runners: bool,
    ) -> DispatchParams:
        execute_acceptance_tests = dispatch_execute_acceptance_tests_default(
            self.agent,
            params,
            apply=apply,
            execute_runners=execute_runners,
        )
        return DispatchParams(
            apply=apply,
            execute_runners=execute_runners,
            planner=_bool_param(params.get("planner"), default=False),
            workflow_mode=dispatch_workflow_mode(self.agent, params, tool_workflow_mode),
            max_runners=dispatch_max_runners_default(self.agent, params, execute_runners=execute_runners),
            limit=_non_negative_int(params.get("limit"), default=20),
            reviewer=str(params.get("reviewer") or "chat-tool").strip(),
            note=str(params.get("note") or "triggered by dispatch_subagents tool").strip(),
            runner_instruction=str(params.get("runner_instruction") or params.get("instruction") or "").strip(),
            max_cards=_non_negative_int(params.get("max_cards"), default=0),
            probe=not _bool_param(params.get("no_probe"), default=False),
            take_over_by=dispatch_take_over_by_default(self.agent, params),
            locked_files=_string_list(params.get("locked_files")),
            execute_acceptance_tests=execute_acceptance_tests,
            auto_apply_acceptance_followup=dispatch_auto_apply_acceptance_followup_default(
                self.agent,
                params,
                apply=apply,
                execute_runners=execute_runners,
                execute_acceptance_tests=execute_acceptance_tests,
            ),
            parent_run_id=dispatch_parent_run_id(self.agent, params),
            root_id=str(params.get("root_id") or "").strip(),
            include_run_ids=_string_list(params.get("run_ids") or params.get("include_run_ids")),
            exclude_run_ids=dispatch_exclude_run_ids(self.agent, params),
            finalize_acceptance=dispatch_finalize_acceptance(self.agent, params),
        )

    # LLM: _report_payload keeps dispatch output compact and recovery-friendly for the parent model.
    # 函数用途: 生成 dispatch_subagents 的 JSON 响应，包含顶层 runner_selection_recovery 和直接孩子进度摘要。
    def _report_payload(self, report) -> dict[str, object]:
        record_payloads = [dispatch_record_payload(item) for item in report.records]
        payload = {
            "dry_run": report.dry_run,
            "runner_selection_recovery": dispatch_recovery_payload(report.records),
            "summary": report.summary,
            "records": record_payloads,
            "result_refs_by_run": related_task_result_refs(self.agent, report),
            "dispatch_json": str(self.agent.subagents.workspace / "subagent_dispatch_report.json"),
            "dispatch_md": str(self.agent.subagents.workspace / "SUBAGENT_DISPATCH.md"),
        }
        payload.update(_dispatch_top_level_guidance(self.agent, report, record_payloads))
        if terminal := dispatch_no_progress_payload(report):
            payload["dispatch_terminal"] = terminal
        payload.update(artifact_integrity_repair_advice_from_records(report.records))
        payload.update(direct_children_progress_payload(self.agent))
        payload["typed_envelope"] = subagent_dispatch_envelope_from_payload(payload).to_dict()
        return payload


# LLM: _dispatch_capability_config aligns model-facing due-check with runner timeout policy.
# 函数用途: 生成 dispatch_subagents 内部能力配置；关闭 runner 总超时不关闭心跳挂死检测。
def _dispatch_capability_config(agent: SimpleAgent) -> CapabilityConfig:
    cfg = _runtime_capability_config(agent)
    if _runner_timeouts_disabled(getattr(agent, "config", None)):
        cfg.subagent_run_timeout = 0
    return cfg


# LLM: _runtime_capability_config lets model-facing dispatch pick up safe config patches automatically.
# 函数用途: 从 agent 上的 capability_config_path 热加载配置；失败时回退默认值，不打断 dispatch。
def _runtime_capability_config(agent: SimpleAgent) -> CapabilityConfig:
    path = Path(getattr(agent, "capability_config_path", "") or default_capability_config_path(getattr(agent, "root", ".")))
    try:
        snapshot = load_capability_config_snapshot(path)
    except (FileNotFoundError, OSError, ValueError):
        return CapabilityConfig()
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = snapshot
    return snapshot.config


# LLM: _runner_timeouts_disabled keeps the accepted off/none/disabled spellings in one small check.
# 函数用途: 判断 agent_config 里 runner_timeout_seconds 是否表示不限制，用于自动 dispatch 的 due-check 降噪。
def _runner_timeouts_disabled(config: object) -> bool:
    raw = getattr(config, "runner_timeout_seconds", "off")
    if isinstance(raw, str):
        return raw.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(raw) == 0.0
    except (TypeError, ValueError):
        return False


# LLM: _run_ids_from_dispatch captures the current root-turn scope from explicit params and dispatch records.
# 函数用途: 记录本轮 dispatch 真实触碰的 run_id，最终收口不要扫描旧任务工作区。
def _run_ids_from_dispatch(params: dict[str, object], report) -> list[str]:
    ids = _string_list(params.get("run_ids") or params.get("include_run_ids"))
    for record in getattr(report, "records", []) or []:
        if not _record_touches_runner_scope(record):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


# LLM: _record_touches_runner_scope keeps old acceptance rows out of current-turn memory.
# 函数用途: dispatch 报告可能包含验收/历史记录；只有真实 runner 选择记录能扩展本轮收口范围。
def _record_touches_runner_scope(record: object) -> bool:
    step = str(getattr(record, "step", "") or "").strip().lower()
    action = str(getattr(record, "action", "") or "").strip().lower()
    if step != "runner":
        return False
    return action in {"execute_runner", "retry_runner", "runner_dry_run"}


# LLM: _dispatch_top_level_guidance makes root dispatch results actionable before records are externalized.
# 函数用途: 汇总 dispatch 的阻塞 run、修复建议和产物 refs，避免 root 先写最终报告再被状态提示纠正。
def _dispatch_top_level_guidance(agent: object, report: object, records: list[dict[str, object]]) -> dict[str, object]:
    blockers = _blocking_run_ids(records)
    unfinished = _unfinished_remembered_run_ids(agent)
    payload: dict[str, object] = {
        "completion_status": _dispatch_completion_status(blockers, unfinished),
        "must_not_report_done": bool(blockers or unfinished),
    }
    if blockers:
        payload["blocking_run_ids"] = blockers
        payload.update(_aggregate_parent_acceptance_repair_advice(records))
    if unfinished:
        payload["unfinished_run_ids"] = unfinished
    if blockers or unfinished:
        payload["next_action"] = "repair_or_continue_blocking_run_ids" if blockers else "continue_dispatch_unfinished_run_ids"
    artifact_refs = related_task_refs(agent, report, "artifact_refs")
    evidence_refs = related_task_refs(agent, report, "evidence_refs")
    if blockers or unfinished:
        if artifact_refs:
            payload["pending_artifact_refs"] = artifact_refs
        if evidence_refs:
            payload["pending_evidence_refs"] = evidence_refs
        return payload
    if artifact_refs:
        payload["deliverable_artifact_refs"] = artifact_refs
    if evidence_refs:
        payload["deliverable_evidence_refs"] = evidence_refs
    return payload


# LLM: _dispatch_completion_status is a compact gate, not a replacement for acceptance reports.
# 函数用途: 给模型一个明确的“是否可报完成”机器字段。
def _dispatch_completion_status(
    blocking_run_ids: list[str],
    unfinished_run_ids: list[str] | None = None,
) -> dict[str, object]:
    unfinished_run_ids = list(unfinished_run_ids or [])
    if not blocking_run_ids and not unfinished_run_ids:
        return {
            "status": "complete_or_no_blockers",
            "blocking_run_ids": [],
            "unfinished_run_ids": [],
            "must_not_report_done": False,
        }
    return {
        "status": "not_complete",
        "blocking_run_ids": blocking_run_ids,
        "unfinished_run_ids": unfinished_run_ids,
        "must_not_report_done": True,
        "recommended_next_action": "repair_or_continue_blocking_run_ids",
    }


# LLM: _unfinished_remembered_run_ids keeps explicit dispatch scopes honest after partial execution.
# 函数用途: 当前轮已创建/调度的 run 只要还有未完成未验收，就在顶层阻止 root 汇报完成。
def _unfinished_remembered_run_ids(agent: object) -> list[str]:
    load = getattr(getattr(agent, "subagents", None), "load", None)
    if not callable(load):
        return []
    unfinished: list[str] = []
    for run_id in sorted(remembered_orchestration_run_ids(agent)):
        try:
            task = load(run_id)
        except Exception:
            continue
        status = str(getattr(task, "status", "") or "").strip().upper()
        verification = str(getattr(task, "verification_status", "") or "").strip().upper()
        if status != "DONE" or verification != "VERIFIED":
            unfinished.append(run_id)
    return unfinished[:20]


# LLM: _blocking_run_ids extracts machine run ids from failed dispatch records.
# 函数用途: dispatch 出现 reject/fail 时，顶层直接暴露阻塞 id，避免模型只看见已生成文件就收尾。
def _blocking_run_ids(records: list[dict[str, object]]) -> list[str]:
    ids: list[str] = []
    for record in records:
        if bool(record.get("ok", True)):
            continue
        run_id = str(record.get("run_id") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids[:20]


# LLM: _aggregate_parent_acceptance_repair_advice keeps repair hints visible outside bulky records.
# 函数用途: records 被外置时，仍在顶层保留父级验收失败的 refs-first 修复建议。
def _aggregate_parent_acceptance_repair_advice(records: list[dict[str, object]]) -> dict[str, object]:
    advices = [
        item.get("parent_acceptance_repair_advice")
        for item in records
        if isinstance(item.get("parent_acceptance_repair_advice"), dict)
    ]
    if not advices:
        return {}
    failed_ids = _unique_strings([
        str(run_id)
        for advice in advices
        for run_id in list(advice.get("failed_run_ids") or [])
    ])
    failure_refs = [
        ref
        for advice in advices
        for ref in list(advice.get("failure_refs") or [])
        if isinstance(ref, dict)
    ][:12]
    suggested_calls = [
        call
        for advice in advices
        if isinstance(call := advice.get("suggested_tool_call"), dict)
    ][:5]
    advice: dict[str, object] = {
        "phase": "parent_acceptance_repair_recommended",
        "failed_run_ids": failed_ids,
        "failure_refs": failure_refs,
        "llm_next_step": (
            "还有子代理未通过父级验收；先按 failure_refs 创建修复/接管小傻妞，"
            "重新 dispatch 并通过验收后再向用户报完成。"
        ),
    }
    if suggested_calls:
        advice["suggested_tool_call"] = suggested_calls[0]
        advice["suggested_tool_calls"] = suggested_calls
    return {"parent_acceptance_repair_advice": advice}


# LLM: _unique_strings preserves first occurrence order for small model-facing lists.
# 函数用途: 给 run ids 和 refs 去重，保持调度输出稳定。
def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
