
from __future__ import annotations

import json
from typing import Any

from .common import make_agent


def cmd_collaboration_overview(args) -> int:
    agent = make_agent(args)
    payload = {"ok": True, **agent.collaboration_store.overview()}
    _print_payload(payload, json_output=bool(getattr(args, "json", False)), text_renderer=_render_overview_text)
    return 0


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


def _print_payload(payload: dict[str, Any], *, json_output: bool, text_renderer) -> None:
    if json_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print(text_renderer(payload))


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


def _render_update_text(payload: dict[str, Any]) -> str:
    case = _dict(payload.get("case"))
    decision = _dict(payload.get("decision"))
    return (
        f"collaboration case {case.get('case_id', '')} "
        f"status={case.get('status', '')} "
        f"decision={decision.get('decision_id', '')}"
    )


def _render_update_request_text(payload: dict[str, Any]) -> str:
    request = _dict(payload.get("request"))
    overview = _dict(payload.get("overview"))
    return (
        f"collaboration request {request.get('request_id', '')} "
        f"status={request.get('status', '')} "
        f"case={overview.get('case_id', '')}"
    )


def _render_error_text(payload: dict[str, Any]) -> str:
    return f"error={payload.get('error', '')} message={payload.get('message', '')}"


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


__all__ = [
    "cmd_collaboration_overview",
    "cmd_collaboration_list",
    "cmd_collaboration_status",
    "cmd_collaboration_update_request",
    "cmd_collaboration_update_status",
]
