from dataclasses import replace

import pytest

from agent_py_agent.agent.scheduler.repository import (
    SchedulerJobCreateRequest,
    SchedulerRepository,
    SchedulerRunFinish,
)
from agent_py_agent.agent.scheduler.schedule import ScheduleValidationError


def _repo(path):
    return SchedulerRepository(path / 'scheduler', owner_provider='feishu', owner_kind='user', owner_id='test', default_timezone='UTC')


def _create(repo):
    rules = [dict(id='rule1', name='分析', schedule={'kind':'every','every_seconds':1800,'anchor_at':1000,'timezone':'UTC'}, use_llm=True, prompt='分析证据'),
             dict(id='rule2', name='探测', schedule={'kind':'every','every_seconds':300,'anchor_at':1000,'timezone':'UTC'}, use_llm=False,
                  tool={'name':'run_command','arguments':{'command':'printf ok'}}),
             dict(id='rule3', name='汇报', schedule={'kind':'cron','expression':'0 9,18 * * *','timezone':'UTC'}, use_llm=True, prompt='汇报')]
    request = SchedulerJobCreateRequest(name='测试监控', prompt='', schedule={}, thread_id='thread-1', rules=rules, now=900, source_request_id='synthetic')
    return repo.create_job(request)[0], request


def test_rules_independent_claim_counts_and_restart(tmp_path):
    repo = _repo(tmp_path)
    job, _ = _create(repo)
    assert job['alias'] == 'job01' and 'schedule' not in job and 'prompt' not in job
    runs = repo.reserve_due_runs(now=1000)
    assert {r['rule_id'] for r in runs} == {'rule1','rule2'}
    assert len({r['thread_id'] for r in runs}) == 2
    assert repo.get_job(job['job_id'])['run_count'] == 0
    first = repo.claim_run(runs[0]['run_id'], lease_seconds=120, now=1001)
    repo.mark_run_running(first['run_id'], first['claim_id'], now=1002)
    repo.mark_run_running(first['run_id'], first['claim_id'], now=1003)
    assert repo.get_job(job['job_id'])['run_count'] == 1
    second = repo.claim_run(runs[1]['run_id'], lease_seconds=120, now=1001)
    repo.mark_run_running(second['run_id'], second['claim_id'], now=1002)
    repo.record_rule_result(second['run_id'], second['claim_id'], {'alive': True})
    repo.finish_run(second['run_id'], second['claim_id'], SchedulerRunFinish(status='done', now=1010))
    restarted = _repo(tmp_path)
    assert len(restarted.list_jobs()[0]) == 1
    next_runs = restarted.reserve_due_runs(now=1300)
    assert [r['rule_id'] for r in next_runs] == ['rule2']
    assert restarted.get_job(job['job_id'])['run_count'] == 2
    assert restarted.get_job(job['job_id'])['rules'][1]['result']['value'] == {'alive': True}


def test_paused_and_updates_keep_frozen_run(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    run = repo.reserve_due_runs(now=1000)[0]
    version = repo.get_job(job['job_id'])['version']
    changed = [dict(r, prompt='新内容') if r['use_llm'] else r for r in req.rules]
    updated = repo.update_job(job['job_id'], patch={'rules':changed,'delivery':{'mode':'none'}}, expected_version=version, now=1001)
    assert repo.get_active_run(run['run_id'])['prompt'] == '分析证据'
    assert repo.get_active_run(run['run_id'])['delivery']['mode'] == 'origin'
    repo.pause_job(job['job_id'], expected_version=updated['version'], now=1002)
    assert _repo(tmp_path).reserve_due_runs(now=10000) == []


def test_alias_never_reused_and_manual_rule_explicit(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    repo.delete_job(job['job_id'], expected_version=job['version'], now=901)
    new = repo.create_job(replace(req, source_request_id='second'))[0]
    assert new['alias'] == 'job02'
    with pytest.raises(ScheduleValidationError):
        repo.reserve_manual_run(new['job_id'], now=950)
    assert repo.reserve_manual_run(new['job_id'], rule_id='rule2', now=950)['use_llm'] is False


def test_disabling_rule_cancels_queued_and_claimed_but_preserves_other_and_running(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    runs = {r['rule_id']: r for r in repo.reserve_due_runs(now=1000)}
    claimed = repo.claim_run(runs['rule1']['run_id'], lease_seconds=60, now=1001)
    rules = [dict(r, enabled=False) if r['id'] == 'rule1' else r for r in req.rules]
    changed = repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=repo.get_job(job['job_id'])['version'], now=1002)
    assert repo.get_active_run(claimed['run_id']) is None
    assert repo.history()[0][0]['error_code'] == 'rule_disabled'
    assert repo.get_active_run(runs['rule2']['run_id'])['status'] == 'queued'
    rules = [dict(r, enabled=True) for r in rules]
    changed = repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=changed['version'], now=1003)
    running = repo.reserve_manual_run(job['job_id'], rule_id='rule1', now=1004)
    running = repo.claim_run(running['run_id'], lease_seconds=60, now=1004)
    repo.mark_run_running(running['run_id'], running['claim_id'], now=1005)
    rules = [dict(r, enabled=False) for r in rules]
    repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=repo.get_job(job['job_id'])['version'], now=1006)
    assert repo.get_active_run(running['run_id'])['status'] == 'running'
    assert repo.get_active_run(runs['rule2']['run_id']) is None


def test_condition_only_confirmed_json_and_staleness(tmp_path):
    repo = _repo(tmp_path)
    job, req = _create(repo)
    rules = [req.rules[1], dict(req.rules[0], depends_on=['rule2'], condition={'rule_id':'rule2','pointer':'/alive','op':'eq','value':False,'max_age_seconds':300})]
    job = repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=job['version'], now=950)
    runs = repo.reserve_due_runs(now=1000)
    assert [r['rule_id'] for r in runs] == ['rule2']
    claim = repo.claim_run(runs[0]['run_id'], lease_seconds=60, now=1001)
    repo.mark_run_running(claim['run_id'], claim['claim_id'], now=1002)
    repo.record_rule_result(claim['run_id'], claim['claim_id'], {'alive':False})
    repo.finish_run(claim['run_id'], claim['claim_id'], SchedulerRunFinish(status='done', now=1010))
    # 在规则定义里安排同一监控的分析轮，已确认依赖允许触发。
    rules[1] = dict(rules[1], schedule={'kind':'every','every_seconds':60,'anchor_at':1060,'timezone':'UTC'})
    job = repo.get_job(job['job_id'])
    repo.update_job(job['job_id'], patch={'rules':rules}, expected_version=job['version'], now=1050)
    assert [r['rule_id'] for r in repo.reserve_due_runs(now=1060)] == ['rule1']


def test_no_llm_rule_uses_real_executor_and_scheduler_closeout(tmp_path):
    from agent_py_agent.tests.test_scheduler_execution import execution_case
    work=tmp_path/'home/owners/local/main/workspace/probe-work'
    work.mkdir(parents=True)
    case=execution_case(tmp_path,cwd=work)
    repository=case.agent.scheduler_repository
    job=repository.get_job(case.job['job_id'])
    # 已预留的旧 LLM 运行取消，随后使用同一监控的新规则试跑。
    old=repository.get_active_run(case.signal.root_task_id)
    claim=repository.claim_run(old['run_id'],lease_seconds=30,now=1001)
    repository.finish_run(claim['run_id'],claim['claim_id'],SchedulerRunFinish(status='cancelled',now=1002))
    case.store.wakes.mark_handled(case.signal.wake_signal_id,now=1002)
    definition={'id':'probe','name':'读取合成探测','use_llm':False,'tool':{'name':'read_file','arguments':{'path':'probe.json'}},
                'schedule':{'kind':'every','every_seconds':60,'anchor_at':1060,'timezone':'UTC'}}
    (work/'probe.json').write_text('{"alive":true}')
    job=repository.get_job(job['job_id'])
    repository.update_job(job['job_id'],patch={'rules':[definition],'delivery':{'mode':'none'}},expected_version=job['version'],now=1050)
    case.agent.scheduler_service.enqueue_ready_runs(now=1060)
    case.scheduler.tick(now=1060)
    history,errors=repository.history(limit=3)
    assert not errors and history[0]['rule_id']=='probe'
    assert history[0]['status']=='done' and history[0]['structured_result'] is not None, (history[0]['error_code'], history[0]['error_message'], history[0]['response'])
    assert not case.backend.prompts
