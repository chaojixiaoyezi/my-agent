# LLM: IM 执行策略的只读展示投影；身份、当日设置、SessionTask 和孩子终态均读取原事实源，不新增权限或状态账。
# 模块用途: 汇总当前聊天的委派设置、后台任务和孩子占用，供 Gateway 等控制页面共用。
from __future__ import annotations


# LLM: /status 是确定控制投影，只读当前 actor 设置与原 SessionTask/SubagentStore；读坏账显示未知，不把投影当执行权。
# 函数用途: 显示当天委派有效期和本聊天独立后台任务占用，不读取任务正文或其它用户配置。
def im_task_status_facts(agent, thread, scope):
    from ..settings.im_task_preferences import delegation_preference
    from .channels import supports_transcript_delivery

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
