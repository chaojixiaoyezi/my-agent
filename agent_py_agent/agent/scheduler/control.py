# LLM: 工具与 slash 共用原 owner 仓库及冻结 actor；删除保留 tombstone，停止只作用于准确 run/task 整树，不移除租约。
# 模块用途: 为监控删除和未来通知接管提供统一控制与准确取消回执，不建立第二份状态账本。
from __future__ import annotations

from ..conversation.host_actor import HostActorError
from ..user_space.admin_channel_identity import find_admin_channel_identity
from .notification_binding import SCHEMA, bound_notification_thread
from .repository import SchedulerConflictError


# LLM: 调用方提供宿主冻结 actor；本地主身份仅控制明确 local creator，旧不明创建人需显式迁移，不升级管理员群身份。
# 函数用途: 在敏感写入前核对准确 owner 和原创建人，工具与命令复用。
def authorize_job(repository, job, actor=None, *, home_root=None):
    if job.get('owner') != repository.owner:
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    creator = job.get('creator')
    local = repository.owner['provider'] == 'local' and repository.owner['kind'] == 'main'
    if creator == {'local': True} and (local or repository.owner['kind'] == 'user' and actor is None):
        if actor is not None and (actor.owner_id != repository.owner['id'] or home_root is None
                or find_admin_channel_identity(home_root, actor.channel, actor.actor_id) is None):
            raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
        return
    if actor is None or creator != {'channel': actor.channel, 'actor_id': actor.actor_id}:
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    if actor.owner_id != repository.owner['id']:
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')


# LLM: 删除先在原仓库 CAS 关闭未来与 queued/claimed，再请求准确 active run 整树停止；异常保存 requested，不能宣称已退出。
# 函数用途: 删除本人监控并回收其当前工作，重复删除沿同一 tombstone 重试未确认取消。
def delete_monitor(agent, repository, job_id, expected_version, *, actor=None):
    return _cancel_monitor(agent, repository, job_id, expected_version, actor=actor, delete=True)


# LLM: 取消当前与删除共用原 job/run 控制服务；future schedule 不变，取消意图和历史仍存在原账本。
# 函数用途: 只停止当前所有规则执行，保留未来周期。
def cancel_monitor(agent, repository, job_id, expected_version, *, actor=None):
    return _cancel_monitor(agent, repository, job_id, expected_version, actor=actor, delete=False)


# LLM: 已授权原 owner/creator 的版本 CAS 是唯一写入入口；坏 run 排除不代表无执行，读取不完整必须保留取消未确认，不猜原身份。
# 函数用途: 执行当前取消或删除，汇总所有规则的准确取消结果。
def _cancel_monitor(agent, repository, job_id, expected_version, *, actor, delete):
    job = repository.get_job(job_id, include_deleted=delete)
    authorize_job(repository, job, actor, home_root=getattr(getattr(agent, 'home_paths', None), 'root', None))
    before, errors = repository._valid_runs(repository._store_snapshot())
    deleted = (repository.delete_job(job_id, expected_version=expected_version) if delete
        else repository.cancel_job_runs(job_id, expected_version=expected_version))
    runs, failures = repository._valid_runs(repository._store_snapshot())
    selected = {run['run_id']: run for run in before if run['job_id'] == job_id and run['status'] in {'queued', 'claimed'}}
    selected.update({run['run_id']: run for run in repository.pending_cancellation_runs() if run['job_id'] == job_id})
    cancellation = [cancel_scheduled_task(agent, repository, run) for run in selected.values()]
    errors.extend(failures)
    return {'job': deleted, 'cancellation': cancellation, 'load_error_codes': sorted(set(errors)),
            'cancellation_status': 'cancel_requested' if errors or any(row['status'] != 'stopped' for row in cancellation) else 'stopped'}


# LLM: 来源只来自当前已认证私人入口；接管目标不能由模型参数指定，保留原 source/creator、当前执行和已预留 run。
# 函数用途: 将本机监控以后的通知转交当前管理员私聊，明确只影响未来触发。
def takeover_notifications(agent, repository, job_id, expected_version, actor):
    from .schedule import ScheduleValidationError

    job = repository.get_job(job_id)
    authorize_job(repository, job, actor, home_root=getattr(getattr(agent, 'home_paths', None), 'root', None))
    home = agent.home_paths
    identity = find_admin_channel_identity(home.root, actor.channel, actor.actor_id) if actor else None
    if (identity is None or repository.owner != {'provider': 'local', 'kind': 'main', 'id': home.owner_id}
            or home.owner_provider != 'local' or home.owner_kind != 'main'
            or not actor.conversation_id or job.get('creator') != {'local': True}):
        raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
    source = agent.conversation_store.threads.require(job.get('source_thread_id') or job['thread_id'])
    from ..conversation.channels import supports_transcript_delivery
    if any(not supports_transcript_delivery(row.channel) for row in source.channel_bindings):
        raise ScheduleValidationError('只能接管原本机监控的未来通知')
    previous = job.get('notification_binding')
    binding = {'schema_version': SCHEMA, 'version': int((previous or {}).get('version') or 0) + 1,
        'owner': dict(repository.owner), 'source_thread_id': actor.source_thread_id,
        'binding': actor.binding(), 'admin_bound_at': identity.bound_at, 'request_id': actor.entry_request_id}
    bound_notification_thread(agent, binding)
    if previous and all(previous[key] == binding[key] for key in ('owner', 'source_thread_id', 'binding', 'admin_bound_at')):
        if int(job['version']) != expected_version:
            raise SchedulerConflictError('job version changed')
        return job
    return repository.update_notification_binding(job_id, binding, expected_version=expected_version)


# LLM: 原 task 锁及 child creation 锁内关闭 RuntimeDB 权限和输入、固定整树资源；claim 留给原执行者收口，绝不按名称杀进程。
# 函数用途: 请求停止准确定时主根及孩子，并报告已确认与仍待收口的区别。
def cancel_scheduled_task(agent, repository, run):
    from ..concurrency.interrupt import interrupt_by_name
    from ..conversation.control_commands import conversation_request_interrupt_name
    from ..conversation.input_dependencies import cancel_task_inputs
    from ..conversation.task_resources import (
        cleanup_task_resources,
        close_main_task_authority,
        freeze_task_resources,
    )

    task_id, thread_id = str(run['run_id']), str(run['thread_id'])
    store = agent.conversation_store
    facts = {'run_id': task_id, 'task_id': task_id, 'thread_id': thread_id, 'status': 'cancel_requested'}
    try:
        if run.get('owner') != repository.owner:
            raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
        with store.goals.transition_guard(thread_id), store.tasks.transition_guard(task_id):
            current = repository.get_active_run(task_id)
            if current is None:
                job = repository.get_job(run['job_id'], include_deleted=True)
                if task_id not in job.get('cancel_pending_run_ids', ()):
                    return {**facts, 'status': 'stopped'}
            elif current['thread_id'] != thread_id or current['owner'] != repository.owner or not current.get('cancel_requested_at'):
                raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
            link, error = store.tasks.load_report(task_id)
            if (link is None and (not error or error.get('error_type') == 'FileNotFoundError')
                    and current is None and not run.get('started_at')):
                repository.record_cancel_cleanup(task_id, confirmed=True)
                return {**facts, 'status': 'stopped'}
            if error or link is None or link.thread_id != thread_id:
                return {**facts, 'error_code': 'SCHEDULER_TASK_UNAVAILABLE'}
            with agent.subagents.creation_guard():
                resource_scope = close_main_task_authority(agent.subagents.runtime_db,
                    owner_home=agent.home_paths.owner_home_dir, task_id=task_id, thread_id=thread_id)
                main_signaled = interrupt_by_name(conversation_request_interrupt_name(task_id))
                if link.status == 'active':
                    store.tasks.update_status({'task_id': task_id, 'status': 'cancelled', 'expected_status': 'active'})
                cancel_task_inputs(agent, task_id)
                resources = freeze_task_resources(agent, resource_scope, task_id)
                unmanaged_pty = None
                if resource_scope is None:
                    from ..tooling.pty_sessions import reclaim_pty_sessions
                    unmanaged_pty = reclaim_pty_sessions(agent.home_paths.owner_home_dir,
                        thread_id=thread_id, root_task_id=task_id)
        facts['resources'] = cleanup_task_resources(resources)
        cleanup = facts['resources']
        main = cleanup['main']
        children = cleanup['children']
        facts['unmanaged_pty'] = unmanaged_pty
        confirmed = (not resources.unconfirmed and not main_signaled and children.get('ok') is True
            and all((child.get('pid_report') or {}).get('status') in {'no_pid', 'exited', 'instance_replaced'}
                and child.get('thread_interrupt') != 'attempt_signaled'
                and ((child.get('resources') or {}).get('pty') or {}).get('status') in {None, 'not_selected'}
                for child in children.get('cancelled', ()))
            and (unmanaged_pty or {}).get('status') in {None, 'not_selected'}
            and main.get('status') != 'unknown' and main.get('background_confirmed') is not False
            and (main.get('pty') or {}).get('status') in {None, 'not_selected'})
        service = getattr(agent, 'scheduler_service', None)
        execution_pending = True
        if service is not None:
            from .active_run_closeout import _task_execution_claim_live_or_unknown
            execution_pending = _task_execution_claim_live_or_unknown(service, run)
        repository.record_cancel_cleanup(task_id, confirmed=confirmed and (current is not None or not execution_pending))
        if service is not None:
            service.reconcile_waiting_run(task_id)
        if repository.get_active_run(task_id) is None and confirmed and not execution_pending:
            repository.record_cancel_cleanup(task_id, confirmed=True)
            facts['status'] = 'stopped'
        return facts
    except (OSError, RuntimeError, ValueError, KeyError, AttributeError) as exc:
        return {**facts, 'error_code': getattr(exc, 'error_code', 'SCHEDULER_CANCEL_UNCONFIRMED'), 'error_type': type(exc).__name__}


# LLM: 检查只读准确 job 所有 active rules 和 TaskLink，不读正文或其它监控；runner 活性由原出生指纹合同判定。
# 函数用途: 展示当前执行、等待及取消状态，供用户决定是否重新核对或停止。
def inspect_monitor_execution(agent, repository, job_id, *, actor=None):
    from .repository import _runner_liveness
    job = repository.get_job(job_id)
    authorize_job(repository, job, actor, home_root=getattr(getattr(agent, 'home_paths', None), 'root', None))
    rows, errors = repository._valid_runs(repository._store_snapshot())
    result = []
    for run in rows:
        if run['job_id'] != job_id:
            continue
        link, error = agent.conversation_store.tasks.load_report(run['run_id'])
        result.append({'run_id': run['run_id'], 'task_id': run['run_id'], 'thread_id': run['thread_id'],
            'rule_id': run.get('rule_id') or 'rule1', 'status': run['status'],
            'task_status': getattr(link, 'status', 'unknown') if not error else 'unknown',
            'runner_liveness': _runner_liveness(run) if run['status'] in {'claimed', 'running'} else 'not_claimed',
            'cancel_requested': bool(run.get('cancel_requested_at')), 'cancel_cleanup_confirmed': run.get('cancel_cleanup_confirmed') is True,
            'pending_input_count': len(getattr(link, 'input_dependency_refs', ())) if not error else None})
    return {'job_id': job_id, 'version': job['version'], 'runs': result, 'load_error_codes': errors}


# LLM: recovery 只做原死亡证明与同 ID waiting 对账，禁止重开存活执行或借用另一监控；CAS 由原恢复事务复核。
# 函数用途: 重新核对选定监控的陈旧执行，返回准确恢复和剩余状态，不代替进程迁移。
def recover_monitor(agent, repository, job_id, expected_version, *, actor=None):
    job = repository.get_job(job_id)
    authorize_job(repository, job, actor, home_root=getattr(getattr(agent, 'home_paths', None), 'root', None))
    recovered = repository.recover_interrupted_executions(job_id=job_id, expected_version=expected_version)
    for run in repository.pending_cancellation_runs():
        if run['job_id'] == job_id:
            cancel_scheduled_task(agent, repository, run)
    service = getattr(agent, 'scheduler_service', None)
    if service is not None:
        rows, _errors = repository._valid_runs(repository._store_snapshot())
        for run in rows:
            if run['job_id'] == job_id and run['status'] == 'waiting':
                service.reconcile_waiting_run(run['run_id'])
    return {**inspect_monitor_execution(agent, repository, job_id, actor=actor), 'recovered_run_ids': recovered}
