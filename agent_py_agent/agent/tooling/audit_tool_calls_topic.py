"""原工具遥测的有界只读页；不曝光工具参数、产物或异常正文。"""
from __future__ import annotations


def tool_calls_topic(agent, query):
    from ..observability.telemetry_ledger import ToolTelemetryLedger
    from .audit_lifecycle_topic import metadata_only

    if query.scope in ("all_owners", "current_thread"):
        return {"tool_calls": {
                    "entries": [], "coverage": {"status": "partial", "reason": "owner_scoped_telemetry_only"},
                    "error_code": "AUDIT_TOPIC_SCOPE_UNSUPPORTED", "requested_scope": query.scope,
                    "supported_scopes": ["owner"], "effect_outcome": "not_started", "read_only": True,
                    "source_read_started": False,
                    "next_action": "tool_calls 仅支持 scope=owner；本次未读取工具记录、未写入任何状态。"
                                   "如可接受查询本人全部会话，请显式复制 retry_example 调用；示例采用最近24小时，"
                                   "需要其它筛选时请重新明确填写，并清空旧 cursor。空 entries 不表示没有工具调用，"
                                   "owner 结果也不能声称仅覆盖当前线程或全部用户。",
                    "retry_example": {"topic": "tool_calls", "scope": "owner", "since_hours": 24, "limit": query.limit},
                }, "sources": ["tool_telemetry_ledger"]}
    ledger = ToolTelemetryLedger(agent.home_paths.owner_home_dir, owner_id=str(agent.home_paths.owner_id or "local/main"))
    reader = getattr(ledger, "query_retained_tool_calls_page", None)
    if reader is None:
        return {"tool_calls": {"entries": [], "coverage": {"status": "unavailable", "reason": "telemetry_reader_unavailable"}},
                "sources": ["tool_telemetry_ledger"]}
    import base64
    import json
    import sqlite3
    token = query.cursor
    if token.startswith("t:"):
        token = json.loads(base64.urlsafe_b64decode(token[2:]))["token"]
    try:
        page = reader(limit=query.limit, cursor=token, tool_name=query.tool_name or None, status=query.status or None,
                      error_code=query.error_code or None, min_duration_ms=query.min_duration_ms,
                      since=query.since, until=query.until,
                      scan_limit=min(300, max(1, getattr(agent.config, "retention_scan_slice_entries", 128) or 300)))
    except (OSError, sqlite3.DatabaseError) as error:
        page = {"rows": [], "coverage": {"status": "unavailable", "reason": type(error).__name__}}
    if page.get("next_cursor"):
        page["next_cursor"] = "t:" + base64.urlsafe_b64encode(json.dumps({"token": page["next_cursor"],
            "since": query.since, "until": query.until}).encode()).decode()
    return {"tool_calls": {**{key: page[key] for key in ("next_cursor", "scanned", "coverage") if key in page},
                           "entries": [metadata_only(row) for row in page.get("rows", [])], "counts_scope": "page"},
            "sources": ["tool_telemetry_ledger"]}
