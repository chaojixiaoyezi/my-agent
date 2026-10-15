# LLM: /schedule只读当前owner原job/run；群中所有创建者可见，写入统一按宿主actor/实时成员证明与原版本CAS授权。
# 模块用途: 查询监控、规则和最近三次历史，并原地暂停、恢复、试跑和改通知，不创建替代任务。
from __future__ import annotations

import time
from contextlib import nullcontext
from datetime import datetime
from zoneinfo import ZoneInfo

from ..conversation.control_commands import ConversationControlResult
from ..scheduler.repository import SchedulerRepositoryError
from ..scheduler.schedule import ScheduleValidationError, _canonical_zone_name
from ..user_space.group_authorization import GroupAuthorizationError
from ..user_space.owner_resolver import OwnerIdentity
from .schedule_diagnostics import ScheduleOwnerChanged, schedule_arguments, schedule_failure


# LLM: 当前空间只取认证scope解析的唯一owner；群内所有创建者可见，私人不跨扫群，显示名不决定写授权。
# 函数用途: 读取当前助手空间的监控及创建者展示，不跨owner扫描或建立第二任务索引。
def visible_monitors(agent, scope, *, include_deleted=False):
    from ..scheduler.creator_access import creator_display_rows
    from .control_service import _request_agent_for_scope

    owner_agent = _request_agent_for_scope(agent, scope)
    repository = getattr(owner_agent, 'scheduler_repository', None)
    if repository is None:
        return [], []
    jobs, errors = repository.list_jobs(include_deleted=include_deleted)
    if repository.owner['kind'] == 'group':
        from ..scheduler.creator_migration import unmanaged_monitor_ids
        if unmanaged_monitor_ids(repository):
            errors.append('SCHEDULER_CREATOR_UNRESOLVED')
    return [(repository, job) for job in creator_display_rows(owner_agent, jobs)], errors


# LLM: 状态只读当前owner监控且与列表同范围；读取失败显示unknown，不能默认为零或跨空间总数。
# 函数用途: 为 /status 提供监控数、运行规则数及暂停数。
def monitor_counts(agent, scope):
    try:
        from .control_service import _request_agent_for_scope
        owner_agent = _request_agent_for_scope(agent, scope)
        repository = getattr(owner_agent, 'scheduler_repository', None)
        rows, errors = visible_monitors(owner_agent, scope)
        active = sum(job['status'] == 'active' for _repo, job in rows)
        jobs_known = not errors
        runs, run_errors, orphan_count = _visible_monitor_runs(repository, rows)
        runs_known = not run_errors
        return {'total':len(rows), 'active':active, 'paused':sum(job['status'] == 'paused' for _repo, job in rows),
                'completed':sum(_completed_monitor(job) for _repo,job in rows),
                'job_counts_known':jobs_known, 'run_counts_known':runs_known, 'orphan_run_count':orphan_count,
                'running_rules':sum(r['status']=='running' for r in runs) if runs_known else None,
                'queued_rules':sum(r['status'] in {'queued','claimed'} for r in runs) if runs_known else None,
                'waiting_rules':sum(r['status']=='waiting' for r in runs) if runs_known else None,
                'load_error_codes':sorted(set(errors + run_errors))}
    except (OSError, ValueError, AttributeError, SchedulerRepositoryError):
        return {'unavailable': True}


# LLM: 查询含同群创建者，写入核原creator；恢复/试跑/改通知须持群能力锁，停止取消不依赖新建授权。
# 函数用途: 展示监控及历史并控制未来执行，异常提供可行动原因而不是要求用户刷新。
def execute_schedule_control(agent, command, scope):
    started, operation, stage = time.monotonic(), 'invalid', 'arguments'
    try:
        operation, identifier, parts = schedule_arguments(command.value)
        stage = 'query'
        rows, errors = visible_monitors(agent, scope, include_deleted=operation == 'delete')
        if not parts:
            text = '\n'.join(f"监控名（{job.get('alias',job['job_id'])}）：{job['name'][:70]}  {_state(job)}  创建者:{job['creator_display']}  last:{_time(job.get('last_run_at'),agent)}" for _repo, job in rows)
            return _result(True, _space_label(scope) + '\n' + (text or ('暂无可确认归属的监控。' if errors else '暂无监控。')) + _query_notice(errors), {'total':len(rows), 'load_error_codes':errors})
        matches = [(repo,job) for repo,job in rows if identifier in {job['job_id'],job.get('alias')}]
        if len(matches) != 1:
            return _result(False,'当前空间没有唯一匹配的监控，请用完整 job_id 查询。'+_query_notice(errors),
                           {'load_error_codes':errors}, code='SCHEDULER_NOT_FOUND')
        repo, job = matches[0]
        if operation == 'history':
            stage = 'history'
            history, failures = repo.history(job_id=job['job_id'], limit=3)
            text = '\n'.join(f"{_time(run.get('started_at') or run.get('scheduled_for'),agent)}  {run.get('rule_id') or 'rule1'}  {run['status']}  投递:{run.get('delivery_status') or '未知'}" for run in history)
            return _result(True, _space_label(scope) + '\n最近3次执行：\n'+(text or '暂无执行历史。')+_query_notice(failures), {'load_error_codes':failures})
        if operation not in {'get', 'history', 'execution', 'delete', 'cancel', 'takeover', 'recover'}:
            from ..scheduler.control import authorize_job
            owner_agent = _control_agent(agent, repo)
            authorize_job(repo, job, _control_actor(owner_agent, scope), home_root=owner_agent.home_paths.root,
                          agent=owner_agent, operation=operation)
        if operation not in {'get', 'history'}:
            stage = 'control'
        if operation in {'delete', 'takeover', 'execution', 'cancel', 'recover'}:
            from ..scheduler.control import (
                cancel_monitor,
                delete_monitor,
                inspect_monitor_execution,
                recover_monitor,
                takeover_notifications,
            )
            owner_agent = _control_agent(agent, repo)
            actor = _control_actor(owner_agent, scope)
            if operation in {'execution', 'recover'}:
                facts = (inspect_monitor_execution(owner_agent, repo, job['job_id'], actor=actor) if operation == 'execution'
                    else recover_monitor(owner_agent, repo, job['job_id'], job['version'], actor=actor))
                text = '\n'.join(f"{row['rule_id']} {row['run_id']}：{row['status']}；任务:{row['task_status']}；取消:{'已请求' if row['cancel_requested'] else '未请求'}"
                    for row in facts['runs'])
                return _result(True, ('已重新核对原执行；未启动新工作。\n' if operation == 'recover' else '当前执行：\n') + (text or '暂无当前执行。'), facts)
            if operation in {'delete', 'cancel'}:
                handler = delete_monitor if operation == 'delete' else cancel_monitor
                facts = handler(owner_agent, repo, job['job_id'], job['version'], actor=actor)
                message = ('已删除监控；后续运行已停止，当前执行已停止。' if facts['cancellation_status'] == 'stopped'
                    else '已删除监控；后续运行已停止，当前执行已请求取消，等待准确收口。')
                if operation == 'cancel':
                    message = ('当前执行已停止；未来计划保留。' if facts['cancellation_status'] == 'stopped' else '当前执行已请求取消，等待准确收口；未来计划保留。')
                if facts['load_error_codes']:
                    message = ('已删除监控；未来运行已停止，当前执行记录不完整，取消或停止尚未确认。' if operation == 'delete'
                        else '当前执行记录不完整，取消或停止尚未确认；未来计划保留。')
                return _result(True, message, {**facts, 'job_id': job['job_id'], 'version': facts['job']['version']})
            if scope.metadata.get('channel_chat_type') not in {'p2p', 'private'}:
                from ..conversation.host_actor import HostActorError
                raise HostActorError('CHANNEL_ACTOR_SCOPE_MISMATCH')
            updated = takeover_notifications(owner_agent, repo, job['job_id'], job['version'], actor)
            return _result(True, '已接管后续触发的通知到当前管理员私聊；已预留或正在执行的运行保持原通知目标和原执行者。',
                {'job_id': job['job_id'], 'version': updated['version'], 'notification_binding_version': updated['notification_binding']['version']})
        if operation == 'run':
            with _new_monitor_work_guard(agent, repo, scope, operation):
                run = repo.reserve_manual_run(job['job_id'], rule_id=parts[2] if len(parts)>2 else '')
            return _result(True,f"已排队：{job['name']} {run.get('rule_id') or 'rule1'}。", {'run_id':run['run_id']})
        creator_display = job['creator_display']
        with _new_monitor_work_guard(agent, repo, scope, operation):
            job = {**_apply_monitor_control(repo, job, operation, parts), 'creator_display': creator_display}
        text = f"监控名（{job.get('alias',job['job_id'])}）：{job['name']}  run次数:{job.get('run_count','未知')}  status:{_state(job)}  创建者:{job.get('creator_display', '身份待核验')}"
        rules = job.get('rules') or [{'id':'rule1','name':job['name'],'use_llm':True,'enabled':True}]
        text += '\n' + '\n'.join(f"{r['id']}：{r['name'][:80]}（{'LLM' if r['use_llm'] else 'NO'}）{' stop' if not r['enabled'] else ''}" for r in rules)
        text += f"\n通知:{job['delivery']['mode']}（更改只影响后续运行；暂停不终止已开工的规则）"
        return _result(True,_space_label(scope)+'\n'+text,{'job_id':job['job_id'],'version':job['version']})
    except GroupAuthorizationError as exc:
        return _result(False, str(exc), code=exc.error_code)
    except (OSError, ValueError, AttributeError, KeyError, SchedulerRepositoryError, ScheduleValidationError) as exc:
        return schedule_failure(exc, scope, operation=operation, stage=stage, started=started)


# LLM: 群能力锁覆盖实际 reserve/update，不能只在前面的授权预检时持有；停止、取消、删除、恢复核对都不申请新能力。
# 函数用途: 为人工恢复/试跑/通知修改绑定当前认证 actor，非群与只读保持原行为。
def _new_monitor_work_guard(agent, repository, scope, operation):
    if repository.owner['kind'] != 'group' or operation not in {'resume', 'run', 'notify'}:
        return nullcontext()
    from ..user_space.group_authorization_context import group_capability_guard
    owner_agent = _control_agent(agent, repository)
    actor = _control_actor(owner_agent, scope)
    return group_capability_guard(owner_agent, 'monitors.create', thread_id=actor.source_thread_id, actor=actor)


# LLM: 仅输出仓库结构化失败的存在事实，不泄露未授权旧任务名或其它创建人；部分查询不能伪装完整空表。
# 函数用途: 提示监控读取不完整或旧归属未确认，避免误报没有任务。
def _query_notice(errors):
    if not errors:
        return ''
    return '\n部分记录无法核验或旧创建人尚未确认；已读取的当前空间监控如上，请管理员核对归属与账本。'


# LLM: 状态只根据 job.status，不用名称或回复判断运行；running 表示计划已开启，规则实际开工另有统计。
# 函数用途: 将监控生命周期投影成用户约定的两种计划状态。
def _state(job):
    return 'running' if job['status']=='active' else 'stop'


# LLM: 统计只读当前认证owner唯一仓库；空job仍校验run，孤儿不计作正常零，不跨owner扫描或变更任何状态。
# 函数用途: 返回当前空间活动规则、原诊断和已证孤儿数量，不修复或启动孤儿。
def _visible_monitor_runs(repository, visible):
    if repository is None:
        return [], [], 0
    if any(repo.owner != repository.owner for repo, _job in visible):
        raise ScheduleOwnerChanged('监控 owner 已变化')
    store = repository._store_snapshot()
    rows, failures = repository._valid_runs(store)
    jobs, job_errors = repository._valid_jobs(store)
    known_ids = {job['job_id'] for job in jobs}
    visible_ids = {job['job_id'] for _repo, job in visible}
    orphan_count = sum(run['job_id'] not in known_ids for run in rows)
    errors = [*failures, *job_errors]
    if orphan_count:
        errors.append('SCHEDULER_RUN_JOB_UNAVAILABLE')
    return [run for run in rows if run['job_id'] in visible_ids], errors, orphan_count


# LLM: 结束必须来自一次性规则及原终态记录，用户主动暂停不能被展示成已完成。
# 函数用途: 区分已结束的一次性监控和普通暂停计划。
def _completed_monitor(job):
    rules=job.get('rules')
    if rules:
        return all(rule['schedule']['kind']=='at' and not rule['enabled'] and rule.get('last_run_status')=='done' for rule in rules)
    return job['status']=='paused' and job['schedule']['kind']=='at' and job.get('last_run_status')=='done'


# LLM: 时间来自原 run/job，时区复用调度同一解析，空配置表示本机时区；不以当前时间补造执行。
# 函数用途: 按部署时区显示最近开工及历史时间。
def _time(value,agent):
    return datetime.fromtimestamp(float(value), ZoneInfo(_canonical_zone_name(agent.config.timezone))).strftime('%m-%d %H:%M:%S') if value else '暂无'


# LLM: 查询与控制返回结构化错误和原 job/run ID；展示不作为状态写入或执行授权。
# 函数用途: 统一确定性 schedule 控制回执。
def _result(ok,text,details=None,*,code=''):
    return ConversationControlResult('schedule',ok,text,details=details,error_code=code)


# LLM: 所有变更走原 version CAS，来源和已预留 run 不改；通知字段不从 prompt 推断。
# 函数用途: 对原监控应用明确暂停、恢复或通知设置，其它操作只读。
def _apply_monitor_control(repository,job,operation,parts):
    if operation in {'pause','resume'} and len(parts)==3:
        from ..scheduler.monitor_state import rule_enable_patch
        updated=repository.update_job(job['job_id'],patch=rule_enable_patch(job,parts[2],operation=='resume'),expected_version=job['version'])
        return repository.resume_job(job['job_id'],expected_version=updated['version']) if operation=='resume' else updated
    if operation=='pause':
        return repository.pause_job(job['job_id'],expected_version=job['version'])
    if operation=='resume':
        return repository.resume_job(job['job_id'],expected_version=job['version'])
    if operation=='notify':
        if len(parts)!=3 or parts[2] not in {'origin','local','none'}:
            raise ScheduleValidationError('用法：/schedule notify job01 origin|local|none')
        return repository.update_job(job['job_id'],patch={'delivery':{'mode':parts[2]}},expected_version=job['version'])
    return job


# LLM: 当前空间控制只加载原Repository所属的准确owner；权限另由共同授权门核对，不借本机管理员或其它群Agent。
# 函数用途: 取得当前owner原监控的执行资源宿主，供授权与整树停止使用。
def _control_agent(base, repository):
    from .request_worker import _resolve_request_agent_for_owner
    if getattr(base, 'scheduler_repository', None) is repository:
        return base
    owner = repository.owner
    identity = OwnerIdentity(owner['provider'], owner['kind'], owner['id'].split('/')[-1])
    candidate = _resolve_request_agent_for_owner(base, identity)
    if candidate.scheduler_repository.owner != repository.owner:
        raise ScheduleOwnerChanged('监控 owner 已变化')
    return candidate


# LLM: slash actor只取认证scope与原ThreadStore；缺线程只按已解析owner创建准确入口绑定，不消费正文、名称或模型参数。
# 函数用途: 给共用控制服务提供准确入口身份；必要时写原线程绑定，本地控制保持无IM actor。
def _control_actor(agent, scope):
    from types import SimpleNamespace

    from ..conversation.channels import supports_transcript_delivery
    from ..conversation.host_actor import HostActorError, actor_from_scope
    if supports_transcript_delivery(scope.channel):
        return None
    thread, error = agent.conversation_store.threads.resolve_report(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id)
    if error:
        raise HostActorError('CHANNEL_ACTOR_IDENTITY_INVALID')
    if thread is None:
        thread = agent.conversation_store.threads.get_or_create({'channel': scope.channel,
            'channel_conversation_id': scope.conversation_id, 'channel_user_id': scope.user_id,
            'canonical_user_id': scope.user_id, 'owner_id': agent.home_paths.owner_id,
            'owner_home': str(agent.home_paths.owner_home_dir)})
    return actor_from_scope(agent, SimpleNamespace(channel=scope.channel,
        channel_conversation_id=scope.conversation_id, channel_user_id=scope.user_id),
        thread.thread_id, str(scope.metadata.get('channel_message_id') or 'schedule-control'))


# LLM: 空间名称只展示已解析owner身份；不把显示文字作为路由或权限来源，未知不猜管理员/群。
# 函数用途: 为列表、详情和历史明确当前助手空间，避免误认为跨群个人汇总。
def _space_label(scope):
    owner = getattr(scope, 'resolved_owner', None)
    if owner is None:
        return '监控空间：当前助手'
    if owner.owner_kind == 'group':
        return f'监控空间：当前群（{owner.provider}:{owner.owner_id}）'
    return '监控空间：本人私人助手'
