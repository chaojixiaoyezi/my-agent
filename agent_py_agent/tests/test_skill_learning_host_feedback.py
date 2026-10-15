from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.capability.pack_verifier_runner import PackVerificationResult
from agent_py_agent.agent.capability.skill_learning_host_feedback import observe_pack_verification
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    observe_learning,
)
from agent_py_agent.agent.verification.runtime import record_tool_verification
from agent_py_agent.tests.test_skill_learning import NAME
from agent_py_agent.tests.test_skill_learning_maintenance import _learned
from agent_py_agent.tests.test_verification_runtime import _call, _success


def _adopt(ctx, task, run, job=''):
    row = ctx.store.load_registry().skills[NAME]
    observe_learning(ctx.store,NAME,sha256=row.sha256,task_id=task,outcome='adopted',source=LearningObservationSource(run_id=run,job_id=job))


def _host(ctx,tmp_path,run):
    project = tmp_path/'project'
    (project/'.git').mkdir(parents=True,exist_ok=True)
    (project/'pyproject.toml').write_text('[tool.pytest.ini_options]\naddopts="-q"\n')
    return SimpleNamespace(config=SimpleNamespace(enable_self_learning=True),home_paths=ctx.home,root=project,
        _current_run_task_workspace=str(project),_current_run_params=SimpleNamespace(run_id=run,task_id=run,attempt_id="attempt-1",task_attributes={})),project


def test_real_verification_seam_promotes_only_exact_adopted_run(tmp_path):
    ctx = _learned(tmp_path)
    _adopt(ctx,'task1','run1')
    _adopt(ctx,'task2','run2')
    for run in ('unadopted','run1','run2'):
        agent,project = _host(ctx,tmp_path,run)
        call = replace(_call('run_command',{'command':'pytest -q','working_dir':str(project)}),run_id=run)
        result = _success(call,'not interpreted',{'process':{'status':'exited','return_code':0}})
        record_tool_verification(agent,call,result)
    data = ctx.store.load_registry().skills[NAME].maintenance
    assert data['quality']=='established'
    facts = [row for row in data['observations'] if row['authority']=='host']
    assert len(facts)==2 and {row['run_id'] for row in facts}=={'run1','run2'}
    assert all(row['receipt_id'].startswith('verification:') for row in facts)


def test_content_failures_cool_but_environment_and_repeated_job_do_not(tmp_path):
    ctx = _learned(tmp_path)
    def result(status,valid=None):
        return PackVerificationResult('v','p',status,valid=valid)
    for number in range(4):
        run = f'run{number}'
        _adopt(ctx,f'task{number}',run,'same-job')
        agent,_ = _host(ctx,tmp_path,run)
        for status in ('error','not_run','cancelled'):
            observe_pack_verification(agent,agent._current_run_params,result(status),status)
        observe_pack_verification(agent,agent._current_run_params,result('failed',False),f'key{number}')
    assert ctx.store.load_registry().skills[NAME].maintenance['quality']=='trial'
    _adopt(ctx,'independent','different')
    agent,_ = _host(ctx,tmp_path,'different')
    observe_pack_verification(agent,agent._current_run_params,result('failed',False),'independent')
    data = ctx.store.load_registry().skills[NAME].maintenance
    assert data['quality']=='unreliable' and data['state']=='cooled'
    assert len([row for row in data['observations'] if row['authority']=='host'])==2


def test_generic_command_failure_and_success_are_not_method_evidence(tmp_path):
    ctx = _learned(tmp_path)
    _adopt(ctx,'task','run')
    agent,project = _host(ctx,tmp_path,'run')
    for command,code in [('pytest',1),('pytest',127),('echo hello',0)]:
        call = replace(_call('run_command',{'command':command,'working_dir':str(project)}),run_id='run')
        record_tool_verification(agent,call,_success(call,'',{'process':{'status':'exited','return_code':code}}))
    assert not any(row['authority']=='host' for row in ctx.store.load_registry().skills[NAME].maintenance['observations'])


def test_late_verifier_cannot_credit_new_current_run(tmp_path):
    ctx = _learned(tmp_path)
    _adopt(ctx,'new-task','new-run')
    agent,project = _host(ctx,tmp_path,'new-run')
    call = _call('run_command',{'command':'pytest -q','working_dir':str(project)})
    record_tool_verification(agent,call,_success(call,'',{'process':{'status':'exited','return_code':0}}))
    assert not any(row['authority']=='host' for row in ctx.store.load_registry().skills[NAME].maintenance['observations'])


def test_old_attempt_cannot_credit_current_same_run(tmp_path):
    ctx = _learned(tmp_path)
    _adopt(ctx,'task','run')
    agent,project = _host(ctx,tmp_path,'run')
    call = replace(_call('run_command',{'command':'pytest -q','working_dir':str(project)}),run_id='run')
    agent._current_run_params.attempt_id='new-attempt'
    record_tool_verification(agent,call,_success(call,'',{'process':{'status':'exited','return_code':0}}))
    assert not any(row['authority']=='host' for row in ctx.store.load_registry().skills[NAME].maintenance['observations'])
