"""监控活动记录坏读时，状态保留诊断并显示未知，不阻塞控制。"""
import json

import pytest

from agent_py_agent.agent.conversation.control_commands import _monitor_status_text
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.schedule_control import monitor_counts
from agent_py_agent.agent.scheduler.repository import SchedulerJobCreateRequest
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests.test_schedule_control import _agent


@pytest.mark.parametrize('group',['','oc_group'])
def test_run_corruption_preserves_diagnostic_and_known_jobs(tmp_path,group):
    agent = _agent(tmp_path,group=group)
    agent.scheduler_repository.create_job(SchedulerJobCreateRequest(name='synthetic',prompt='synthetic',
        thread_id='source',schedule={'kind':'every','every_seconds':600,'timezone':'UTC','anchor_at':1000},
        creator={'channel':'feishu','actor_id':'ou_synthetic'} if group else {'local':True},now=1000))
    scope = GatewayControlScope('synthetic','feishu' if group else 'local',group or 'synthetic',
        resolved_owner=OwnerIdentity.provider_group('feishu',group) if group else OwnerIdentity.local_main())
    store_path = agent.scheduler_repository.store_path
    original = store_path.read_bytes()
    corrupted = json.loads(original)
    corrupted['runs']['synthetic-malformed-run'] = {'status':'running'}
    store_path.write_text(json.dumps(corrupted))
    facts = monitor_counts(agent,scope)
    assert facts['total'] == 1 and facts['job_counts_known'] is True
    assert facts['run_counts_known'] is False
    assert facts['running_rules'] is facts['queued_rules'] is facts['waiting_rules'] is None
    assert facts['load_error_codes'] == ['SCHEDULER_RUN_INVALID']
    text = _monitor_status_text(facts)
    assert '监控：1 个' in text and '执行 未知，排队 未知，等待 未知' in text
    assert '/schedule' in text
    store_path.write_bytes(original)
    assert monitor_counts(agent,scope)['run_counts_known'] is True
    assert '执行 0，排队 0，等待 0' in _monitor_status_text(monitor_counts(agent,scope))


def test_legacy_unknown_and_job_unknown_never_render_zero():
    assert '暂不可用' in _monitor_status_text({'load_error_codes':['broken']})
    assert '暂不可用' in _monitor_status_text({'job_counts_known':False})


@pytest.mark.parametrize('mode',['corrupt','orphan','healthy'])
@pytest.mark.parametrize('group',['','oc_group'])
def test_empty_job_list_still_diagnoses_only_current_owner_runs(tmp_path,group,mode):
    agent = _agent(tmp_path,group=group)
    repository = agent.scheduler_repository
    job,_ = repository.create_job(SchedulerJobCreateRequest(name='synthetic',prompt='synthetic',
        thread_id='source',schedule={'kind':'every','every_seconds':600,'timezone':'UTC','anchor_at':1000},
        creator={'channel':'feishu','actor_id':'ou_synthetic'} if group else {'local':True},now=1000))
    repository.reserve_manual_run(job['job_id'],now=1001)
    payload = json.loads(repository.store_path.read_bytes())
    payload['jobs'] = {}
    if mode == 'corrupt':
        payload['runs'] = {'invalid':{'status':'running'}}
    elif mode == 'healthy':
        payload['runs'] = {}
    repository.store_path.write_text(json.dumps(payload))
    before = repository.store_path.read_bytes()
    scope = GatewayControlScope('synthetic','feishu' if group else 'local',group or 'synthetic',
        resolved_owner=OwnerIdentity.provider_group('feishu',group) if group else OwnerIdentity.local_main())
    facts = monitor_counts(agent,scope)
    assert facts['total'] == 0 and facts['job_counts_known'] is True
    assert facts['run_counts_known'] is (mode == 'healthy')
    assert facts['orphan_run_count'] == int(mode == 'orphan')
    if mode != 'healthy':
        assert facts['load_error_codes'] == ['SCHEDULER_RUN_INVALID' if mode == 'corrupt' else 'SCHEDULER_RUN_JOB_UNAVAILABLE']
        assert facts['running_rules'] is None and '执行 未知' in _monitor_status_text(facts)
    else:
        assert not facts['load_error_codes'] and facts['running_rules'] == 0
    assert repository.store_path.read_bytes() == before
