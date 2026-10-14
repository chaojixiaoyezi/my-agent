# LLM: IM 执行策略的只读展示投影；身份、当日设置、SessionTask、准确输入 refs 和孩子终态均读取原事实源，不新增权限或状态账。
# 模块用途: 汇总当前聊天的委派设置、后台运行和待输入，供 Gateway 等控制页面共用。
from __future__ import annotations

from ..common.json_io import read_json_object_report
from .execution_threads import IM_TASK_EXECUTION_KEY
from .input_dependencies import input_dependency_current, input_dependency_path


# LLM: 准确根只取原目标线程的 root_task_id；输入 refs 是查找投影，状态仍读每条 canonical 行，坏账不补零。
# 函数用途: 判断一条已接单的后台任务是否待输入，只读该任务引用，不扫描 owner 输入历史。
def _waiting_input_runs(agent, task):
    target, error = agent.conversation_store.threads.load_report(task.target_thread_id)
    if error or target is None:
        raise ValueError('后台目标会话不可读')
    reference = target.metadata.get(IM_TASK_EXECUTION_KEY, {})
    if not isinstance(reference, dict):
        raise ValueError('后台根引用不可读')
    root_id = reference.get('root_task_id', '')
    if not root_id:
        return '', set()
    link, error = agent.conversation_store.tasks.load_report(root_id)
    if error or link is None or link.thread_id != target.thread_id:
        raise ValueError('后台任务根不可读或归属冲突')
    waiting = set()
    for input_id in link.input_dependency_refs:
        report = read_json_object_report(input_dependency_path(agent, input_id), context='im_status.input')
        row = report.payload
        if (report.load_error or not row or row.get('schema_version') != 'task_input.v1'
                or row.get('input_id') != input_id or row.get('scope', {}).get('root_task_id') != root_id
                or row.get('status') not in {'pending', 'ready', 'cancelled', 'expired', 'revoked', 'failed'}
                or not isinstance(row.get('armed'), bool)):
            raise ValueError('后台输入状态不可读或归属冲突')
        if row['status'] == 'pending' and row.get('armed'):
            scope = row['scope']
            origin, error = agent.conversation_store.threads.load_report(scope['thread_id'])
            if error or origin is None:
                raise ValueError('输入原会话状态不可读')
            if scope['agent_kind'] == 'subagent':
                child = agent.subagents.load(scope['run_id'])
                if child.root_id != root_id or child.agent_thread_id != scope['thread_id']:
                    raise ValueError('输入子代理归属冲突')
            elif scope['agent_kind'] != 'main' or scope['run_id'] != root_id:
                raise ValueError('输入执行身份不可读')
            if input_dependency_current(agent, row):
                waiting.add(scope['run_id'])
    return root_id, waiting


# LLM: 子代理状态只按本会话已接单根读取原 report；root 索引只是选择，读取错误不得静默当零。
# 函数用途: 读取相关后台任务的孩子，避免扫描 owner 全历史并明确暴露坏账。
def _background_children(agent, roots):
    children = []
    for root in roots:
        report = agent.subagents.list_runs_for_root_report(root)
        if report.load_errors:
            raise ValueError('后台子代理状态不可读')
        children.extend(report.runs)
    return children


# LLM: /status 只读当前 actor 设置与原 SessionTask/SubagentStore/input refs；waiting_input 不计运行，BLOCKED 单列等待，坏账显式不可用。
# 函数用途: 显示当天委派期限、后台任务运行与待输入数量，说明等待不占执行资源，不读取秘密或其它用户配置。
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
            if errors:
                raise ValueError('后台任务状态不可读')
            relevant = [task for task in tasks if task.sender_thread_id == thread.thread_id]
            accepted = [task for task in relevant if task.status == 'accepted']
            task_inputs = [_waiting_input_runs(agent, task) for task in accepted]
            waiting_count = sum(bool(runs) for _root, runs in task_inputs)
            result.update({"queued": sum(task.status == "queued" for task in relevant),
                           "running": len(accepted)-waiting_count, "waiting_input": waiting_count})
            from ..subagents.models import SUBAGENT_ENDED_STATUSES

            children = _background_children(agent, {root for root, _runs in task_inputs if root})
            input_runs = set().union(*(runs for _root, runs in task_inputs)) if task_inputs else set()
            result["subagent_running"] = sum(run.status not in SUBAGENT_ENDED_STATUSES for run in children)
            result["subagent_waiting"] = sum(run.status == 'BLOCKED' for run in children)
            result["subagent_waiting_input"] = sum(run.status == 'BLOCKED' and run.id in input_runs for run in children)
            result["root_subagent_limit"] = agent.config.max_subagents
            result["owner_subagent_limit"] = agent.owner_policy.max_subagents
        return result
    except (AttributeError, OSError, TypeError, ValueError, RuntimeError, KeyError):
        return {"available": False}
