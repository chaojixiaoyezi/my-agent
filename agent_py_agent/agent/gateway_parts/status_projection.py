# LLM: /status 显示只读取 TUI 同源上下文快照与可信 owner 权限；不读取正文，不生成模型请求或写计量。
# 模块用途: 为 Gateway 和本地状态页提供角色、执行权限和本代上下文事实，未知数据不补零。
from __future__ import annotations

from ..conversation.context_usage import context_usage_from_thread


# LLM: 管理员只认可信 home 的 local/main 规范键（与 OwnerIdentity 的标量 ID 不混用）；未知身份明确展示未知，不把 UI 入口当授权事实。
# 函数用途: 生成状态页的公共字段；上下文沿 canonical thread 同代快照，不在状态查询时重算历史。
def status_display_facts(agent: object, thread: object = None, *, scope: object = None) -> dict[str, object]:
    if scope is not None:
        try:
            thread = agent.conversation_store.threads.resolve(
                channel=scope.channel, channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id
            )
        except (AttributeError, OSError, TypeError, ValueError):
            thread = None
    home = getattr(agent, "home_paths", None)
    key = tuple(str(getattr(home, name, "") or "") for name in ("owner_provider", "owner_kind", "owner_id"))
    role = "管理员" if key == ("local", "main", "local/main") else "普通用户" if all(key) else "未知"
    config = getattr(agent, "config", None)
    return {"user_role": role,
            "permission_mode": f"文件 {getattr(config, 'path_access_mode', '未知')}；Shell {getattr(config, 'access_mode', '未知')}",
            "context_usage": context_usage_from_thread(thread), "im_task_status": _im_task_status(agent, thread, scope)}


# LLM: /status 是确定控制投影，只读当前 actor 设置与原 SessionTask/SubagentStore；读坏账显示未知，不把投影当执行权。
# 函数用途: 显示当天委派有效期和本聊天独立后台任务占用，不读取任务正文或其它用户配置。
def _im_task_status(agent, thread, scope):
    from ..conversation.channels import supports_transcript_delivery
    from ..settings.im_task_preferences import delegation_preference

    if scope is None or supports_transcript_delivery(scope.channel):
        return {}
    try:
        setting = delegation_preference(agent, scope.channel, scope.user_id)
        result = {"delegation_enabled": setting["enabled"], "expires_at": setting["expires_at"], "timezone": setting["timezone"]}
        if thread is not None:
            tasks, errors = agent.conversation_store.session_tasks.list_report(limit=0)
            if not errors:
                relevant = [task for task in tasks if task.sender_thread_id == thread.thread_id]
                result.update({"queued": sum(task.status == "queued" for task in relevant),
                               "running": sum(task.status == "accepted" for task in relevant)})
                from ..subagents.models import SUBAGENT_ENDED_STATUSES

                targets = {task.target_thread_id for task in relevant}
                children = [run for run in agent.subagents.list_runs()
                            if (run.attributes or {}).get("conversation_thread_id") in targets]
                result["subagent_running"] = sum(run.status not in SUBAGENT_ENDED_STATUSES for run in children)
                result["root_subagent_limit"] = agent.config.max_subagents
                result["owner_subagent_limit"] = agent.owner_policy.max_subagents
        return result
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError):
        return {"available": False}
