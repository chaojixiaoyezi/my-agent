from __future__ import annotations

from typing import Any

from ..runtime_errors import runtime_error_report


def channel_activity_items_report(cross_channel, user_id: str) -> tuple[list[dict], list[dict]]:
    items = []
    load_errors: list[dict] = []
    for channel in ["chat", "feishu", "qq", "web"]:
        session_ids, errors = cross_channel.list_sessions_by_channel_report(user_id, channel)
        load_errors.extend(errors)
        for session_id in session_ids:
            session_items, session_errors = session_channel_activity_report(cross_channel, session_id)
            items.extend(session_items)
            load_errors.extend(session_errors)
    return items, load_errors


def session_channel_activity_report(cross_channel, session_id: str) -> tuple[list[dict], list[dict]]:
    items = []
    data, load_error = cross_channel._load_channels_report(session_id)
    if load_error is not None:
        return [], [load_error]
    if data is None:
        return [], []
    for channel, info in data.get("channels", {}).items():
        if info.get("last_active_at", 0) <= 0:
            continue
        items.append(
            {
                "type": "channel_activity",
                "session_id": session_id,
                "channel": channel,
                "active": info.get("active", False),
                "timestamp": info.get("last_active_at", 0),
            }
        )
    return items, []


def session_update_items(session_manager, user_id: str) -> list[dict]:
    return [
        {
            "type": "session_update",
            "session_id": session.session_id,
            "channel": session.last_active_channel,
            "timestamp": session.updated_at,
        }
        for session in session_manager.list_sessions(user_id=user_id)
    ]


def task_update_items_report(
    task_registry_store,
    user_id: str,
    *,
    context: str,
) -> tuple[list[dict], list[dict]]:
    if not task_registry_store:
        return [], []
    try:
        from ..task_registry import TaskRegistry

        registry = TaskRegistry(task_registry_store)
        tasks = registry.query_tasks(user_id=user_id, limit=50)
    except Exception as exc:
        return [], [admin_load_error(exc, context=context)]
    return [
        {
            "type": "task_update",
            "task_id": task["task_id"],
            "status": task["status"],
            "goal": task["goal"],
            "timestamp": task.get("updated_at", 0),
        }
        for task in tasks
    ], []


def append_summary_sessions(lines: list[str], summary: dict) -> None:
    sessions = summary.get("sessions")
    if not sessions:
        return
    lines.append("最近会话:")
    for sess in sessions[:3]:
        lines.append(f"  - {sess['session_id']}: {sess['primary']}")


def append_admin_load_errors(lines: list[str], load_errors: list[dict]) -> None:
    if not load_errors:
        return
    lines.append("## 读取警告")
    for error in load_errors[:5]:
        context = error.get("context", "unknown")
        message = error.get("model_message") or error.get("message") or "读取失败"
        lines.append(f"- {context}: {message}")
    lines.append("")


def admin_load_error(exc: BaseException, *, context: str) -> dict[str, Any]:
    return runtime_error_report(exc, context=context)


def format_session_activity(item: dict, ts: str) -> str:
    return f"- [{ts}] 会话 {item['session_id'][:16]}... 在 {item['channel']}"


def format_task_activity(item: dict, ts: str) -> str:
    return f"- [{ts}] 任务 {item['task_id']} -> {item['status']}"


def format_channel_activity(item: dict, ts: str) -> str:
    active_str = "活跃" if item.get("active") else "非活跃"
    return f"- [{ts}] 通道 {item['channel']} ({active_str})"
