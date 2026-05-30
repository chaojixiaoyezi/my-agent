# LLM: Dispatch tool helper logic; keep model-facing dispatch output compact and refs-first.
# 模块用途: 拆分 dispatch_subagents 的范围、恢复建议和配置推导，避免工具入口过大。

from __future__ import annotations

from pathlib import Path

from ..capability.runtime_config import (
    default_capability_config_path,
    load_capability_config_snapshot,
)
from ..capability_config import CapabilityConfig
from .orchestration_dispatch_refs import related_task_refs
from .orchestration_dispatch_scope import dispatch_include_run_ids_param
from .orchestration_run_scope import remembered_orchestration_run_ids
from .parameters import _bool_param


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


# LLM: _run_ids_for_scope captures the requested root-turn scope from params and dispatch records.
# 函数用途: 记录本轮模型关心的 run_id；显式请求但因并发上限未执行的 run 仍属于当前轮状态范围。
def _run_ids_for_scope(params: dict[str, object], report, agent: object | None = None) -> list[str]:
    ids = dispatch_include_run_ids_param(params, agent=agent)
    for record in getattr(report, "records", []) or []:
        if not _record_touches_run_scope(record):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


# LLM: _run_ids_actually_dispatched only records per-run runner attempts that really crossed dispatch.
# 函数用途: 区分“模型请求了这个 run”和“这个 run 真的被执行/重试过”；被 max_runners 跳过的 PLANNING run 不能误记为已调度。
def _run_ids_actually_dispatched(report) -> list[str]:
    ids: list[str] = []
    for record in getattr(report, "records", []) or []:
        if not _record_is_actual_runner_attempt(record):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


# LLM: _normalized_dispatch_params keeps only dry_run-to-internal execution mapping.
# 函数用途: 模型可见协议保持扁平；这里只把 dry_run 映射成内部 apply/execute_runners。
def _normalized_dispatch_params(params: dict[str, object]) -> dict[str, object]:
    normalized = dict(params or {})
    _promote_dry_run_param(normalized)
    return normalized


def _promote_dry_run_param(params: dict[str, object]) -> None:
    if "dry_run" not in params:
        return
    dry_run = _bool_param(params.get("dry_run"), default=True)
    params["apply"] = not dry_run
    params["execute_runners"] = not dry_run
    if dry_run:
        params.setdefault("workflow_mode", "plan")
    else:
        params.setdefault("workflow_mode", "off")


# LLM: _record_touches_run_scope keeps old acceptance rows out of current-turn memory.
# 函数用途: dispatch 报告可能包含全局/历史记录；只有本轮 runner/修复动作能扩展范围，旧验收记录不能污染收口。
def _record_touches_run_scope(record: object) -> bool:
    run_id = str(getattr(record, "run_id", "") or "").strip()
    if not run_id:
        return False
    step = str(getattr(record, "step", "") or "").strip().lower()
    return step in {"runner", "action_apply", "capability_route", "patch_review"}


# LLM: _record_is_actual_runner_attempt excludes requested-but-skipped ids and dry-run inspections.
# 函数用途: 只有真实 runner 执行/重试记录才能让 pending-dispatch 认为该 run 已跨过一次调度。
def _record_is_actual_runner_attempt(record: object) -> bool:
    step = str(getattr(record, "step", "") or "").strip().lower()
    if step != "runner":
        return False
    action = str(getattr(record, "action", "") or "").strip().lower()
    if action not in {"execute_runner", "retry_runner"}:
        return False
    if bool(getattr(record, "dry_run", False)):
        return False
    return bool(getattr(record, "applied", False))


# LLM: _dispatch_top_level_guidance makes root dispatch results actionable before records are externalized.
# 函数用途: 汇总 dispatch 的阻塞 run、修复建议和产物 refs，避免 root 先写最终报告再被状态提示纠正。
def _dispatch_top_level_guidance(agent: object, report: object, records: list[dict[str, object]]) -> dict[str, object]:
    blockers = _blocking_run_ids(records)
    unfinished = _unfinished_remembered_run_ids(agent)
    payload: dict[str, object] = {
        "completion_status": _dispatch_completion_status(
            blockers,
            unfinished,
        ),
        "must_not_report_done": bool(blockers or unfinished),
    }
    if blockers:
        payload["blocking_run_ids"] = blockers
    if unfinished:
        payload["unfinished_run_ids"] = unfinished
    if blockers or unfinished:
        payload.setdefault("next_action", _dispatch_next_action(blockers))
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


def _dispatch_next_action(blockers: list[str]) -> str:
    return "repair_or_continue_blocking_run_ids" if blockers else "continue_dispatch_unfinished_run_ids"


# LLM: _child_result_index_hint is a soft rework hint, not a delivery gate.
# 函数用途: 提醒父级优先核对 child 摘要和 output_json refs，避免协调结果漏掉已产出的 child 发现。
def _child_result_index_hint(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    return (
        "先核对 child_result_index 中每个 child 的 summary、output_json 和 artifact refs；"
        "如果协调汇总和 child 摘要冲突，先修复汇总或继续调度，不要只看输出目录或单个协调产物。"
    )


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
    payload: dict[str, object] = {
        "status": "not_complete",
        "blocking_run_ids": blocking_run_ids,
        "unfinished_run_ids": unfinished_run_ids,
        "must_not_report_done": True,
        "recommended_next_action": "repair_or_continue_blocking_run_ids",
    }
    return payload


# LLM: _unfinished_remembered_run_ids keeps explicit dispatch scopes honest after partial execution.
# 函数用途: 当前轮已创建/调度的 run 只要还有未完成未收口，就在顶层阻止 root 汇报完成。
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
def _blocking_run_ids(
    records: list[dict[str, object]],
) -> list[str]:
    ids: list[str] = []
    for record in records:
        if bool(record.get("ok", True)):
            continue
        run_id = str(record.get("run_id") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids[:20]


# LLM: _unique_strings preserves first occurrence order for small model-facing lists.
# 函数用途: 给 run ids 和 refs 去重，保持调度输出稳定。
def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
