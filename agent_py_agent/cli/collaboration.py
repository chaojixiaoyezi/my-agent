# LLM: Collaboration CLI exposes read-only views over the generic collaboration ledger.
# 模块用途: 提供 collaboration list/status 命令，方便查看协作 case、请求、证据和待响应情况。

from __future__ import annotations

import json
from typing import Any

from .common import make_agent


# LLM: cmd_collaboration_overview is a read-only health summary for all collaboration cases.
# 函数用途: 输出协作控制面总览，供真实任务前检查是否还有待主代理处理的阻塞。
def cmd_collaboration_overview(args) -> int:
    agent = make_agent(args)
    payload = {"ok": True, **agent.collaboration_store.overview()}
    _print_payload(payload, json_output=bool(getattr(args, "json", False)), text_renderer=_render_overview_text)
    return 0


# LLM: cmd_collaboration_list is a read-only CLI command over CollaborationStore.
# 函数用途: 列出协作 case 摘要，包括请求数、待响应请求数、证据数和决策数。
def cmd_collaboration_list(args) -> int:
    agent = make_agent(args)
    status_filter = str(getattr(args, "status", "") or "").strip() or None
    limit = max(0, int(getattr(args, "limit", 20) or 0))
    cases = agent.collaboration_store.list_cases(status=status_filter)
    if limit:
        cases = cases[-limit:]
    rows = [_overview_from_status(agent.collaboration_store.case_status(case.case_id)) for case in cases]
    payload = {
        "ok": True,
        "total": len(rows),
        "status_filter": status_filter or "",
        "cases": rows,
    }
    _print_payload(payload, json_output=bool(getattr(args, "json", False)), text_renderer=_render_list_text)
    return 0


# LLM: cmd_collaboration_status is a read-only CLI command for one case.
# 函数用途: 展示单个协作 case 的完整状态、请求、证据、参与者和决策摘要。
def cmd_collaboration_status(args) -> int:
    agent = make_agent(args)
    case_id = str(getattr(args, "case_id", "") or "").strip()
    if not case_id:
        _print_payload(
            {"ok": False, "error": "case_id_required", "message": "--case-id is required"},
            json_output=bool(getattr(args, "json", False)),
            text_renderer=_render_error_text,
        )
        return 2
    try:
        status = agent.collaboration_store.case_status(case_id)
    except KeyError:
        _print_payload(
            {"ok": False, "error": "case_not_found", "message": f"unknown collaboration case: {case_id}"},
            json_output=bool(getattr(args, "json", False)),
            text_renderer=_render_error_text,
        )
        return 2
    payload = {"ok": True, "overview": _overview_from_status(status), **status}
    _print_payload(payload, json_output=bool(getattr(args, "json", False)), text_renderer=_render_status_text)
    return 0


# LLM: cmd_collaboration_update_status is an audited mutating CLI command over case lifecycle.
# 函数用途: 推进协作 case 状态并写入决策摘要，关闭类状态缺摘要时返回结构化错误。
def cmd_collaboration_update_status(args) -> int:
    agent = make_agent(args)
    case_id = str(getattr(args, "case_id", "") or "").strip()
    try:
        case = agent.collaboration_store.record_case_status({'case_id': case_id, 'status': str(getattr(args, "status", "") or ""), 'actor_agent_id': str(getattr(args, "actor_agent_id", "") or ""), 'summary': str(getattr(args, "summary", "") or ""), 'decision_type': str(getattr(args, "decision_type", "") or "")})
    except (KeyError, ValueError) as exc:
        _print_payload(
            {"ok": False, "error": "case_status_update_failed", "message": str(exc)},
            json_output=bool(getattr(args, "json", False)),
            text_renderer=_render_error_text,
        )
        return 2
    decisions = agent.collaboration_store.case_decisions(case_id)
    decision = decisions[-1].to_dict() if decisions else {}
    payload = {"ok": True, "case": case.to_dict(), "decision": decision}
    _print_payload(payload, json_output=bool(getattr(args, "json", False)), text_renderer=_render_update_text)
    return 0


# LLM: cmd_collaboration_update_request is a mutating CLI command for responder progress.
# 函数用途: 更新协作请求状态，并返回最新 request 和 case 概览。
def cmd_collaboration_update_request(args) -> int:
    agent = make_agent(args)
    case_id = str(getattr(args, "case_id", "") or "").strip()
    request_id = str(getattr(args, "request_id", "") or "").strip()
    try:
        request = agent.collaboration_store.update_request_status({'case_id': case_id, 'request_id': request_id, 'status': str(getattr(args, "status", "") or ""), 'actor_agent_id': str(getattr(args, "actor_agent_id", "") or ""), 'summary': str(getattr(args, "summary", "") or "")})
    except (KeyError, ValueError) as exc:
        _print_payload(
            {"ok": False, "error": "request_status_update_failed", "message": str(exc)},
            json_output=bool(getattr(args, "json", False)),
            text_renderer=_render_error_text,
        )
        return 2
    status = agent.collaboration_store.case_status(case_id)
    payload = {
        "ok": True,
        "request": request.to_dict(),
        "overview": _overview_from_status(status),
    }
    _print_payload(
        payload,
        json_output=bool(getattr(args, "json", False)),
        text_renderer=_render_update_request_text,
    )
    return 0


# LLM: _overview_from_status derives display counters from structured case status only.
# 函数用途: 从 case_status payload 计算列表页使用的摘要，不读取自然语言判断任务类型。
def _overview_from_status(status: dict[str, Any]) -> dict[str, Any]:
    case = _dict(status.get("case"))
    requests = [_dict(item) for item in _list(status.get("requests"))]
    evidence = [_dict(item) for item in _list(status.get("evidence"))]
    return {
        "case_id": str(case.get("case_id") or ""),
        "title": str(case.get("title") or ""),
        "status": str(case.get("status") or ""),
        "priority": str(case.get("priority") or ""),
        "thread_id": str(case.get("thread_id") or ""),
        "task_id": str(case.get("task_id") or ""),
        "created_by": str(case.get("created_by") or ""),
        "request_count": int(status.get("request_count") or len(requests)),
        "pending_request_count": int(status.get("pending_request_count") or 0),
        "blocked_request_count": int(status.get("blocked_request_count") or 0),
        "completed_request_count": int(status.get("completed_request_count") or 0),
        "declined_request_count": int(status.get("declined_request_count") or 0),
        "ready_for_main_agent": bool(status.get("ready_for_main_agent")),
        "evidence_count": int(status.get("evidence_count") or len(evidence)),
        "participant_count": int(status.get("participant_count") or len(_list(status.get("participants")))),
        "decision_count": int(status.get("decision_count") or len(_list(status.get("decisions")))),
    }


# LLM: _print_payload keeps JSON and human-readable output backed by the same payload.
# 函数用途: 根据 --json 选择输出结构化 JSON 或简短文本。
def _print_payload(payload: dict[str, Any], *, json_output: bool, text_renderer) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(text_renderer(payload))


# LLM: _render_list_text is intentionally compact for terminal status checks.
# 函数用途: 把 case 列表 payload 渲染成人可读文本。
def _render_list_text(payload: dict[str, Any]) -> str:
    lines = [f"collaboration cases total={payload.get('total', 0)}"]
    for item in _list(payload.get("cases")):
        row = _dict(item)
        lines.append(
            "- "
            f"{row.get('case_id', '')} "
            f"status={row.get('status', '')} "
            f"priority={row.get('priority', '')} "
            f"requests={row.get('request_count', 0)} "
            f"pending={row.get('pending_request_count', 0)} "
            f"blocked={row.get('blocked_request_count', 0)} "
            f"completed={row.get('completed_request_count', 0)} "
            f"evidence={row.get('evidence_count', 0)} "
            f"decisions={row.get('decision_count', 0)} "
            f"title={row.get('title', '')}"
        )
    return "\n".join(lines)


# LLM: _render_overview_text keeps preflight health readable without dumping every case.
# 函数用途: 把协作总览 payload 渲染为简短文本。
def _render_overview_text(payload: dict[str, Any]) -> str:
    readiness = _dict(payload.get("readiness"))
    lines = [
        "collaboration overview",
        (
            f"cases={payload.get('case_count', 0)} "
            f"open={payload.get('open_case_count', 0)} "
            f"ready={payload.get('ready_case_count', 0)} "
            f"requests={payload.get('request_count', 0)} "
            f"pending={payload.get('pending_request_count', 0)} "
            f"blocked={payload.get('blocked_request_count', 0)} "
            f"completed={payload.get('completed_request_count', 0)} "
            f"evidence={payload.get('evidence_count', 0)}"
        ),
        f"readiness={readiness.get('ready', False)} next_action={readiness.get('next_action', '')}",
    ]
    for blocker in _list(readiness.get("blockers"))[:5]:
        row = _dict(blocker)
        lines.append(
            "- blocker "
            f"case={row.get('case_id', '')} "
            f"blocked_requests={row.get('blocked_request_count', 0)} "
            f"missing_evidence={row.get('missing_evidence_request_count', 0)} "
            f"title={row.get('title', '')}"
        )
    return "\n".join(lines)


# LLM: _render_status_text shows one case's counters and latest refs without dumping large payloads.
# 函数用途: 把单 case status payload 渲染成人可读文本。
def _render_status_text(payload: dict[str, Any]) -> str:
    overview = _dict(payload.get("overview"))
    lines = [
        f"collaboration case {overview.get('case_id', '')}",
        f"status={overview.get('status', '')} priority={overview.get('priority', '')}",
        (
            f"requests={overview.get('request_count', 0)} "
            f"pending={overview.get('pending_request_count', 0)} "
            f"blocked={overview.get('blocked_request_count', 0)} "
            f"completed={overview.get('completed_request_count', 0)} "
            f"evidence={overview.get('evidence_count', 0)} "
            f"participants={overview.get('participant_count', 0)} "
            f"decisions={overview.get('decision_count', 0)}"
        ),
    ]
    for evidence in _list(payload.get("evidence"))[-5:]:
        row = _dict(evidence)
        refs = ", ".join(str(item) for item in _list(row.get("evidence_refs")))
        lines.append(f"- evidence {row.get('evidence_id', '')} source={row.get('source_agent_id', '')} refs={refs}")
    return "\n".join(lines)


# LLM: _render_update_text keeps lifecycle updates compact and auditable in terminal output.
# 函数用途: 把状态推进结果渲染成人可读文本。
def _render_update_text(payload: dict[str, Any]) -> str:
    case = _dict(payload.get("case"))
    decision = _dict(payload.get("decision"))
    return (
        f"collaboration case {case.get('case_id', '')} "
        f"status={case.get('status', '')} "
        f"decision={decision.get('decision_id', '')}"
    )


# LLM: _render_update_request_text keeps request lifecycle updates short for shell use.
# 函数用途: 把协作请求状态更新结果渲染为一行文本。
def _render_update_request_text(payload: dict[str, Any]) -> str:
    request = _dict(payload.get("request"))
    overview = _dict(payload.get("overview"))
    return (
        f"collaboration request {request.get('request_id', '')} "
        f"status={request.get('status', '')} "
        f"case={overview.get('case_id', '')}"
    )


# LLM: _render_error_text keeps CLI failures readable while JSON output remains structured.
# 函数用途: 把错误 payload 渲染为一行错误文本。
def _render_error_text(payload: dict[str, Any]) -> str:
    return f"error={payload.get('error', '')} message={payload.get('message', '')}"


# LLM: _dict safely normalizes optional object payloads.
# 函数用途: 非 dict 输入返回空字典，避免坏行影响整段展示。
def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# LLM: _list safely normalizes optional list payloads.
# 函数用途: 非 list 输入返回空列表，避免展示命令崩溃。
def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


__all__ = [
    "cmd_collaboration_overview",
    "cmd_collaboration_list",
    "cmd_collaboration_status",
    "cmd_collaboration_update_request",
    "cmd_collaboration_update_status",
]
