# LLM: /schedule 是原 owner job/run 的确定性控制面；按可信发起 actor 过滤群记录，写入仍走原版本 CAS。
# 模块用途: 查询监控、规则和最近三次历史，并原地暂停、恢复、试跑和改通知，不创建替代任务。
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..conversation.control_commands import ConversationControlResult
from ..scheduler.repository import SchedulerRepository, SchedulerRepositoryError
from ..scheduler.schedule import ScheduleValidationError
from ..user_space.owner_resolver import OwnerIdentity, resolve_owner_home


# LLM: 本人身份来自认证 scope/admin 绑定；个人私聊可查看自己在各群创建的监控，群查询只在本 owner，不泄露私人监控。
# 函数用途: 汇总本人的合法监控仓库与 job，冷群只读取原账本、不实例化 Agent。
def visible_monitors(agent, scope):
    from ..user_space.admin_channel_identity import list_admin_channel_identities
    from .control_service import _request_agent_for_scope

    owner_agent = _request_agent_for_scope(agent, scope)
    repository = getattr(owner_agent, 'scheduler_repository', None)
    if repository is None:
        return [], []
    home = owner_agent.home_paths
    local = home.owner_provider == 'local' and home.owner_kind == 'main'
    group = home.owner_kind == 'group'
    identities = {(scope.channel, scope.user_id)}
    if local:
        identities.update((item.channel, item.user_id) for item in list_admin_channel_identities(home.root))
    repositories = [repository]
    if not group:
        for provider in {channel for channel, _actor in identities if channel not in {'local','cli','chat','http'}}:
            directory = resolve_owner_home(home.root, OwnerIdentity.provider_group(provider, '_index')).home_dir.parent
            if not directory.is_dir():
                continue
            for path in directory.iterdir():
                if path.is_symlink() or not path.is_dir():
                    continue
                resolved = resolve_owner_home(home.root, OwnerIdentity.provider_group(provider, path.name))
                store_path = resolved.scheduler_dir
                if not (store_path / 'store.json').is_file():
                    continue
                cold = SchedulerRepository(store_path, owner_provider=provider, owner_kind='group',
                    owner_id=resolved.owner_id, default_timezone=owner_agent.config.timezone)
                cold._monitor_query_identity = resolved.identity
                repositories.append(cold)
    rows, errors = [], []
    for repo in repositories:
        jobs, failures = repo.list_jobs()
        errors.extend(failures)
        for job in jobs:
            creator = job.get('creator')
            owned = ((creator or {}).get('channel'), (creator or {}).get('actor_id')) in identities
            if repo is repository and not group:
                owned = True
            if owned:
                rows.append((repo, job))
    return rows, errors


# LLM: 状态只读本用户可见监控；读取失败显示 unknown，不能默认为零或全系统数量。
# 函数用途: 为 /status 提供监控数、运行规则数及暂停数。
def monitor_counts(agent, scope):
    try:
        rows, errors = visible_monitors(agent, scope)
        active = sum(job['status'] == 'active' for _repo, job in rows)
        runs=_visible_monitor_runs(rows)
        return {'total':len(rows), 'active':active, 'paused':sum(job['status'] == 'paused' for _repo, job in rows),
                'completed':sum(_completed_monitor(job) for _repo,job in rows),
                'running_rules':sum(r['status']=='running' for r in runs),
                'queued_rules':sum(r['status'] in {'queued','claimed'} for r in runs),
                'waiting_rules':sum(r['status']=='waiting' for r in runs),'load_error_codes':errors}
    except (OSError, ValueError, AttributeError, SchedulerRepositoryError):
        return {'unavailable': True}


# LLM: 命令参数只选择已授权列表的精确 job/alias；模糊别名冲突拒绝，不修改来源、当前运行或他人的监控。
# 函数用途: 展示简短监控列表、详情、历史，或控制原监控未来执行和回执方式。
def execute_schedule_control(agent, command, scope):
    try:
        rows, errors = visible_monitors(agent, scope)
        parts = command.value.split()
        if not parts:
            text = '\n'.join(f"监控名（{job.get('alias',job['job_id'])}）：{job['name'][:70]}  {_state(job)}  last:{_time(job.get('last_run_at'),agent)}" for _repo, job in rows)
            return _result(True, text or '暂无监控。', {'total':len(rows), 'load_error_codes':errors})
        operation, identifier = (parts[0], parts[1]) if parts[0] in {'history','pause','resume','run','notify'} and len(parts) > 1 else ('get',parts[0])
        if (operation=='get' and len(parts)!=1) or (operation=='history' and len(parts)!=2) or (operation in {'pause','resume','run'} and len(parts) not in {2,3}):
            raise ScheduleValidationError('监控命令参数无效')
        matches = [(repo,job) for repo,job in rows if identifier in {job['job_id'],job.get('alias')}]
        if len(matches) != 1:
            return _result(False,'没有唯一匹配的本人监控，请用完整 job_id 查询。', code='SCHEDULER_NOT_FOUND')
        repo, job = matches[0]
        if operation == 'history':
            history, failures = repo.history(job_id=job['job_id'], limit=3)
            text = '\n'.join(f"{_time(run.get('started_at') or run.get('scheduled_for'),agent)}  {run.get('rule_id') or 'rule1'}  {run['status']}  投递:{run.get('delivery_status') or '未知'}" for run in history)
            return _result(True, '最近3次执行：\n'+(text or '暂无执行历史。'), {'load_error_codes':failures})
        if operation not in {'get', 'history'}:
            repo = _live_repository(agent, repo)
        if operation == 'run':
            run = repo.reserve_manual_run(job['job_id'], rule_id=parts[2] if len(parts)>2 else '')
            return _result(True,f"已排队：{job['name']} {run.get('rule_id') or 'rule1'}。", {'run_id':run['run_id']})
        job = _apply_monitor_control(repo, job, operation, parts)
        text = f"监控名（{job.get('alias',job['job_id'])}）：{job['name']}  run次数:{job.get('run_count','未知')}  status:{_state(job)}"
        rules = job.get('rules') or [{'id':'rule1','name':job['name'],'use_llm':True,'enabled':True}]
        text += '\n' + '\n'.join(f"{r['id']}：{r['name'][:80]}（{'LLM' if r['use_llm'] else 'NO'}）{' stop' if not r['enabled'] else ''}" for r in rules)
        text += f"\n通知:{job['delivery']['mode']}（更改只影响后续运行；暂停不终止已开工的规则）"
        return _result(True,text,{'job_id':job['job_id'],'version':job['version']})
    except (OSError, ValueError, AttributeError, SchedulerRepositoryError, ScheduleValidationError):
        return _result(False,'监控查询或控制未完成，请刷新后重试。',code='SCHEDULER_UNAVAILABLE')


# LLM: 状态只根据 job.status，不用名称或回复判断运行；running 表示计划已开启，规则实际开工另有统计。
# 函数用途: 将监控生命周期投影成用户约定的两种计划状态。
def _state(job):
    return 'running' if job['status']=='active' else 'stop'


# LLM: 统计只汇总通过 creator/owner 过滤的原 run，一次读取每个仓库，不把全群执行数量泄露给某个用户。
# 函数用途: 返回本人当前监控的活动规则，用于运行、排队和等待计数。
def _visible_monitor_runs(rows):
    identifiers={}
    for repository,job in rows:
        identifiers.setdefault(repository,set()).add(job['job_id'])
    return [run for repository,ids in identifiers.items() for run in repository._valid_runs(repository._store_snapshot())[0]
        if run['job_id'] in ids]


# LLM: 结束必须来自一次性规则及原终态记录，用户主动暂停不能被展示成已完成。
# 函数用途: 区分已结束的一次性监控和普通暂停计划。
def _completed_monitor(job):
    rules=job.get('rules')
    if rules:
        return all(rule['schedule']['kind']=='at' and not rule['enabled'] and rule.get('last_run_status')=='done' for rule in rules)
    return job['status']=='paused' and job['schedule']['kind']=='at' and job.get('last_run_status')=='done'


# LLM: 时间来自原 run/job，缺失明确显示暂无，不用当前时间补造执行。
# 函数用途: 按部署时区显示最近开工及历史时间。
def _time(value,agent):
    return datetime.fromtimestamp(float(value), ZoneInfo(agent.config.timezone)).strftime('%m-%d %H:%M:%S') if value else '暂无'


# LLM: 查询与控制返回结构化错误和原 job/run ID；展示不作为状态写入或执行授权。
# 函数用途: 统一确定性 schedule 控制回执。
def _result(ok,text,details=None,*,code=''):
    return ConversationControlResult('schedule',ok,text,details=details,error_code=code)


# LLM: 只为已经按原创建人过滤的精确 group owner 加载原 Agent，写入必须沿其真实配额、due_index 和运行资源。
# 函数用途: 把冷查询仓库换成原控制仓库，禁止无配额临时仓库写入。
def _live_repository(base,repository):
    from .request_worker import _resolve_request_agent_for_owner
    identity=getattr(repository,'_monitor_query_identity',None)
    if identity is None:
        return repository
    live=_resolve_request_agent_for_owner(base,identity).scheduler_repository
    if live.owner!=repository.owner:
        raise ValueError('监控 owner 已变化')
    return live


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
