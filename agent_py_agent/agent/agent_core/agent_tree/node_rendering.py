# LLM: 代理树只展示结构化运行与插话状态，不充当身份、权限或调度事实源；修改须核对树查询错误投影。
# 模块用途: 将运行快照、工作进度和未读插话整理成代理树节点。
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from ...model_visible_refs import current_model_ref, current_model_ref_list, current_model_text
from ...runtime_errors import runtime_error_report
from ...subagents.models import TaskStatus, task_status_in, task_status_reason_code
from ...subagents.tool_failure_ledger import recent_tool_failure
from ...task_progress import progress_path, read_task_progress, task_progress_summary
from ..runtime.owner_roots import runtime_owner_root


# LLM: 节点只投影 kernel 快照与既有只读事实；recent_tool_failure 在读取时从 owner 权威 runtime_events 现算，
#   不写回任务、不新增状态文件。修改须同步 model_view 白名单与 list_agents 回归。
# 函数用途: 把一个子代理的运行快照整理成代理树节点，供 /status、TUI 和 list_agents 共用。
def node_from_kernel_run(agent: object, row: object) -> dict[str, object]:
    payload = asdict(row)
    refs = _node_ref_values(payload)
    node = _node_identity(payload)
    node.update(_node_status(payload, refs))
    node["liveness"] = _liveness_layer(payload)
    node["progress_layer"] = _progress_layer(agent, payload)
    node["evidence_layer"] = _evidence_layer(refs)
    node["guidance_layer"] = _guidance_layer(agent, str(node.get("run_id") or ""))
    # 模型与智能程度（结构化事实）：显示子代理实际使用的模型与档位，只读投影、不含凭据；
    # 权威来源是已物化线程，线程未物化/读取失败时回退创建时冻结的任务属性。
    node["model"], node["reasoning_effort"] = _model_effort_view(agent, payload)
    # 增量结论账行数(结构化事实):>0 提示整合轮"这条 run 有已确认结论,取消/整合前先读账"。
    node["findings_recorded"] = _findings_recorded(payload)
    # 最近工具失败段(结构化事实):让父级分清"被授权门反复拦下"和"模型还在慢慢想"。
    if failure := _recent_tool_failure(agent, str(node.get("run_id") or "")):
        node["recent_tool_failure"] = failure
    return node


# LLM: 只读 agent.subagents.runtime_db 这一 owner 权威库；没有库（本地非托管）或 run 未登记时不输出该字段。
# 函数用途: 取出子代理最近一次工具失败、错误码、失败阶段、同码连续次数和时间。
def _recent_tool_failure(agent: object, run_id: str) -> dict[str, object]:
    runtime_db = getattr(getattr(agent, "subagents", None), "runtime_db", None)
    return recent_tool_failure(runtime_db, run_id)


def _findings_recorded(payload: dict[str, object]) -> int:
    refs = _dict(payload.get("workspace_refs"))
    path_text = str(refs.get("agent_run_findings") or "").strip()
    if not path_text:
        return 0
    try:
        with open(path_text, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())
    except OSError:
        return 0


def _node_ref_values(payload: dict[str, object]) -> dict[str, list[object]]:
    tool_contract = _dict(payload.get("tool_contract"))
    return {
        "artifact_refs": current_model_ref_list(_list(payload.get("artifact_refs"))),
        "artifact_registry_refs": _current_registry_refs(payload.get("artifact_registry_refs")),
        "declared_output_refs": current_model_ref_list(_list(payload.get("declared_output_refs"))),
        "evidence_refs": current_model_ref_list(_list(payload.get("evidence_refs"))),
        "blockers": _list(payload.get("blockers")),
        "needs_capability": _needs_capability(tool_contract, _list(payload.get("needs_capability"))),
        "recent_tool_trace": _recent_tool_trace(payload.get("recent_tool_trace")),
    }


# LLM: 只搬运 kernel 快照里的身份、状态与接替关系（replaced_by 来自 takeover_by/superseded_by 权威字段），不重新查树、
#   不推断完成。新增字段须同步 model_view 白名单与 list_agents 回归。
# 函数用途: 生成代理树节点的身份、状态和进度摘要部分。
def _node_identity(payload: dict[str, object]) -> dict[str, object]:
    timing = _timing(payload)
    return {
        "task_id": payload.get("task_id") or payload.get("run_id", ""),
        "run_id": payload.get("run_id", ""),
        "parent_id": payload.get("parent_id", ""),
        "parent_task_id": payload.get("parent_task_id", ""),
        "parent_run_id": payload.get("parent_run_id", ""),
        "root_id": payload.get("root_id", ""),
        "root_run_id": payload.get("root_run_id", ""),
        "depth": payload.get("depth", 0),
        "agent_kind": payload.get("agent_kind", ""),
        "role": payload.get("role", ""),
        "agent_name": payload.get("agent_name", ""),
        # 派工时的 goal 摘要:整合轮据此逐子代理核对"计划 vs 实交",缺的补建或如实标注。
        "goal_digest": current_model_text(payload.get("goal_digest", "")),
        "status": payload.get("status", ""),
        # 已被接替时标出接替者与处置（superseded/taken_over），父级不要再把旧结果当成当前交付。
        "replaced_by": _dict(payload.get("replaced_by")),
        "failure_type": payload.get("failure_type", ""),
        "progress": payload.get("progress", 0.0),
        "current_step": current_model_text(payload.get("current_step", "")),
        "current_tool": payload.get("current_tool", ""),
        "heartbeat_at": payload.get("heartbeat_at", 0.0),
        "updated_at": payload.get("updated_at", 0.0),
        "last_progress_at": payload.get("last_progress_at", 0.0),
        "last_progress_summary": current_model_text(payload.get("last_progress_summary", "")),
        "latest_summary": current_model_text(payload.get("latest_summary", "")),
        "running_seconds": timing["running_seconds"],
        "seconds_since_progress": timing["seconds_since_progress"],
        "not_done_reason": _not_done_reason(payload),
        "child_ids": payload.get("child_ids", []),
        "resume_eligibility": _dict(payload.get("resume_eligibility")),
    }


def _node_status(payload: dict[str, object], refs: dict[str, list[object]]) -> dict[str, object]:
    return {
        "artifact_refs": refs["artifact_refs"],
        "artifact_registry_refs": refs["artifact_registry_refs"],
        "declared_output_refs": refs["declared_output_refs"],
        "evidence_refs": refs["evidence_refs"],
        "blockers": refs["blockers"],
        "workspace_refs": _workspace_refs(payload.get("workspace_refs")),
        "recovery_refs": _recovery_refs(payload.get("recovery_refs")),
        "tool_contract": _dict(payload.get("tool_contract")),
        "needs_capability": refs["needs_capability"],
        "recent_tool_trace": refs["recent_tool_trace"],
    }


def _liveness_layer(payload: dict[str, object]) -> dict[str, object]:
    heartbeat_at = payload.get("heartbeat_at", 0.0)
    timing = _timing(payload)
    return {
        "status": payload.get("status", ""),
        "heartbeat_at": heartbeat_at,
        "updated_at": payload.get("updated_at", 0.0),
        "has_heartbeat": bool(heartbeat_at),
        "running_seconds": timing["running_seconds"],
        "seconds_since_progress": timing["seconds_since_progress"],
        "not_done_reason": _not_done_reason(payload),
    }


def _progress_layer(agent: object, payload: dict[str, object]) -> dict[str, object]:
    timing = _timing(payload)
    layer = {
        "progress": payload.get("progress", 0.0),
        "current_step": current_model_text(payload.get("current_step", "")),
        "current_tool": payload.get("current_tool", ""),
        "last_progress_at": payload.get("last_progress_at", 0.0),
        "last_progress_summary": current_model_text(payload.get("last_progress_summary", "")),
        "latest_summary": current_model_text(payload.get("latest_summary", "")),
        "running_seconds": timing["running_seconds"],
        "seconds_since_progress": timing["seconds_since_progress"],
    }
    attach_task_progress(agent, str(payload.get("run_id") or ""), layer)
    return layer


def attach_task_progress(agent: object, run_id: str, layer: dict[str, object]) -> None:
    root = _progress_root(agent)
    if not root or not run_id:
        return
    path = progress_path(root, run_id)
    if not path.exists():
        return
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    if summary["summary"] or summary["next_action"] or summary["counts"].get("total", 0):
        layer["task_progress"] = summary


def _progress_root(agent: object):
    try:
        return runtime_owner_root(agent)
    except AttributeError:
        return None


def _workspace_refs(value: object) -> dict[str, object]:
    refs = _dict(value)
    task_root = current_model_ref(refs.get("task_root") or refs.get("task_workspace") or "")
    task_work_dir = current_model_ref(refs.get("task_work_dir") or "")
    task_output_dir = current_model_ref(refs.get("task_output_dir") or "")
    if task_root:
        from pathlib import Path

        task_work_dir = task_work_dir or str(Path(task_root) / "work")
        task_output_dir = task_output_dir or str(Path(task_root) / "output")
    normalized = {
        "task_root": task_root,
        "task_work_dir": task_work_dir,
        "task_output_dir": task_output_dir,
        "agent_work_dir": current_model_ref(refs.get("agent_work_dir") or refs.get("agent_run_workspace")),
        "shared_blackboard": current_model_ref(refs.get("shared_blackboard")),
        "inbox": current_model_ref(refs.get("agent_run_inbox") or refs.get("inbox")),
        "outbox": current_model_ref(refs.get("agent_run_outbox") or refs.get("outbox")),
        "final_report": current_model_ref(refs.get("agent_run_final_report") or refs.get("final_report")),
        # 增量结论账:整合轮/取消裁决前先读账,别把已确认结论跟着 run 一起扔掉。
        "findings_ledger": current_model_ref(refs.get("agent_run_findings")),
    }
    return {key: item for key, item in normalized.items() if item}


def _recovery_refs(value: object) -> dict[str, object]:
    refs = _dict(value)
    return {
        str(key): projected
        for key, item in refs.items()
        if (projected := _recovery_ref_value(item))
    }


def _recovery_ref_value(value: object) -> object:
    if isinstance(value, list | tuple | set):
        return current_model_ref_list(value)
    return current_model_ref(value)


def _current_registry_refs(value: object) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for item in _dict_list(value):
        rows.append(_current_registry_ref(item))
    return rows


def _current_registry_ref(item: dict[str, object]) -> dict[str, object]:
    row = dict(item)
    if "path" not in row:
        return row
    projected = current_model_ref(row.get("path"))
    if projected:
        return {**row, "path": projected}
    return {key: value for key, value in row.items() if key != "path"}


def _evidence_layer(values: dict[str, list[object]]) -> dict[str, object]:
    return {
        "artifact_refs": values["artifact_refs"],
        "artifact_registry_refs": values["artifact_registry_refs"],
        "declared_output_refs": values["declared_output_refs"],
        "evidence_refs": values["evidence_refs"],
        "blockers": values["blockers"],
        "needs_capability": values["needs_capability"],
        "recent_tool_trace": values["recent_tool_trace"],
    }


# LLM: 代理树只投影当前 run 的 guidance 队列；读取错误作为诊断返回，不赋予跨邮箱访问权限。
# 函数用途: 为代理树节点读取未处理插话及加载错误。
def _guidance_layer(agent: object, run_id: str) -> dict[str, object]:
    store = getattr(agent, "conversation_store", None)
    if store is None or not run_id:
        return {"pending_count": 0, "recent_pending": []}
    try:
        pending = list(store.guidance.pending("agent_run", run_id, limit=5))
    except Exception as exc:
        return {
            "pending_count": 0,
            "recent_pending": [],
            "warnings": ["guidance_unavailable"],
            "guidance_load_error": runtime_error_report(exc, context="agent_tree.guidance.pending"),
        }
    return {
        "pending_count": len(pending),
        "recent_pending": [_guidance_item(item) for item in pending],
    }


def _guidance_item(item: object) -> dict[str, object]:
    return {
        "guidance_id": str(getattr(item, "guidance_id", "") or ""),
        "message": str(getattr(item, "message", "") or ""),
        "priority": str(getattr(item, "priority", "") or "normal"),
        "sender": str(getattr(item, "sender", "") or ""),
    }


def _dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _timing(payload: dict[str, object]) -> dict[str, float]:
    now = time.time()
    started = _safe_float(payload.get("heartbeat_at")) or _safe_float(payload.get("updated_at"))
    progress_at = (
        _safe_float(payload.get("last_progress_at"))
        or _safe_float(payload.get("heartbeat_at"))
        or _safe_float(payload.get("updated_at"))
    )
    return {
        "running_seconds": max(0.0, now - started) if started > 0 else 0.0,
        "seconds_since_progress": max(0.0, now - progress_at) if progress_at > 0 else 0.0,
    }


def _not_done_reason(payload: dict[str, object]) -> str:
    status = str(payload.get("status") or "").strip()
    if task_status_in(payload.get("status"), {TaskStatus.DONE.value}):
        return ""
    failure_type = str(payload.get("failure_type") or "").strip()
    if failure_type:
        return f"failure_type:{failure_type}"
    blockers = _list(payload.get("blockers"))
    if blockers:
        return f"blocked:{blockers[0]}"
    current_tool = str(payload.get("current_tool") or "").strip()
    if current_tool:
        return f"running_tool:{current_tool}"
    current_step = str(payload.get("current_step") or "").strip()
    if current_step:
        return f"current_step:{current_step}"
    return task_status_reason_code(status) or f"raw_status:{status}" if status else "not_done"


def _list(value: object) -> list:
    return list(value) if isinstance(value, list) else []


def _dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _needs_capability(tool_contract: dict[str, object], explicit: list[object]) -> list[str]:
    if explicit:
        return [str(item) for item in explicit if str(item or "").strip()]
    needs: list[str] = []
    if _safe_int(tool_contract.get("open_request_count")) > 0:
        needs.append("capability_request")
    if _safe_int(tool_contract.get("gap_count")) > 0:
        needs.append("capability_gap")
    return needs


def _recent_tool_trace(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value[-5:] if isinstance(item, dict)]


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


# LLM: 权威来源是已物化线程上的 model_profile_id / reasoning_effort（创建时从任务属性写入）；
#   线程读取失败或未物化时回退 kernel 快照里冻结的任务属性值。任何失败显示"未知"，绝不让
#   list_agents/status 失败，也不输出密钥、地址或请求头。修改须同步 model_view 白名单与测试。
# 函数用途: 生成节点上的模型与智能程度展示字段（只读投影）。
def _model_effort_view(agent: object, payload: dict[str, object]) -> tuple[str, str]:
    profile_id, effort = _thread_model_refs(agent, payload)
    if not profile_id and not effort:
        profile_id = str(payload.get("model_profile_id") or "")
        effort = str(payload.get("reasoning_effort") or "")
    return _model_label(agent, profile_id), _effort_label(effort)


# LLM: 只读已物化线程；store 缺失、线程缺失或损坏一律返回空，由调用方回退冻结值，不抛错。
# 函数用途: 从会话线程读取模型引用与智能程度，拿不到就返回空。
def _thread_model_refs(agent: object, payload: dict[str, object]) -> tuple[str, str]:
    thread_id = str(payload.get("thread_id") or "").strip()
    store = getattr(agent, "conversation_store", None)
    if not thread_id or store is None:
        return "", ""
    try:
        thread = store.threads.load(thread_id)
    except Exception:
        return "", ""
    return (
        str(getattr(thread, "model_profile_id", "") or ""),
        str(getattr(thread, "reasoning_effort", "") or ""),
    )


# LLM: 只取模型名称与档案编号，绝不读地址、密钥或请求头；default 表示继承会话默认，
#   名称解析失败（档案删除/目录损坏）显示"未知"。shared: 前缀走共享目录。
# 函数用途: 把模型编号换算成"名称（编号）"展示文本。
def _model_label(agent: object, profile_id: str) -> str:
    profile_id = str(profile_id or "").strip()
    if not profile_id or profile_id == "default":
        return "继承会话默认"
    name = _resolve_model_name(agent, profile_id)
    return f"{name}（{profile_id}）" if name else "未知"


# LLM: 只读模型目录与共享目录的公开字段，任何失败返回空串（展示"未知"）；不缓存、不发请求。
# 函数用途: 按档案编号解析模型显示名称；查不到或目录不可读时返回空。
def _resolve_model_name(agent: object, profile_id: str) -> str:
    try:
        from ...settings.shared_model_catalog import shared_profile_key

        if shared_profile_key(profile_id):
            return _shared_model_name(agent, profile_id)
        return _own_model_name(agent, profile_id)
    except Exception:
        return ""


# LLM: 只读管理员公开的共享目录（不含密钥）；没找到返回空串，异常交给调用方统一兜底。
# 函数用途: 在共享目录里按编号找模型名称。
def _shared_model_name(agent: object, profile_id: str) -> str:
    from ...settings.shared_model_catalog import public_shared_profiles

    return next((str(row.get("model_name") or "") for row in public_shared_profiles(agent.home_paths)
                 if row.get("id") == profile_id), "")


# LLM: 只取当前 owner 目录里这条档案的 model_name，不读地址、密钥或请求头；异常交给调用方统一兜底。
# 函数用途: 在当前用户自己的模型目录里按编号找模型名称。
def _own_model_name(agent: object, profile_id: str) -> str:
    from ...settings.model_profiles import model_profiles_path, read_model_profiles

    row = read_model_profiles(model_profiles_path(agent.home_paths))["profiles"].get(profile_id)
    return str(row.get("model_name") or "") if isinstance(row, dict) else ""


# LLM: 档位规范化复用同一入口；空串/非法值按"默认"展示（线程未设置，运行时回落全局默认）。
# 函数用途: 把档位值换算成展示文本。
def _effort_label(effort: str) -> str:
    from ...backends.reasoning_control import normalize_reasoning_level

    level = normalize_reasoning_level(effort)
    return level if level else "默认"


__all__ = ["node_from_kernel_run"]
