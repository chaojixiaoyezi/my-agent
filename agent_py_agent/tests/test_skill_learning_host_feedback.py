import threading
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import skill_learning_host_feedback as feedback
from agent_py_agent.agent.capability.pack_verifier_runner import PackVerificationResult
from agent_py_agent.agent.capability.skill_learning_host_feedback import observe_pack_verification
from agent_py_agent.agent.capability.skill_learning_maintenance import (
    LearningObservationSource,
    observe_learning,
)
from agent_py_agent.agent.capability.skill_learning_prompt import parse_skill_learning_decision
from agent_py_agent.agent.capability.skill_learning_publish import (
    PublishRequest,
    SkillLearningGateError,
    publish_learned_skill,
)
from agent_py_agent.agent.capability.skill_learning_store import SkillLearningStore
from agent_py_agent.agent.verification.runtime import record_tool_verification
from agent_py_agent.tests.test_skill_learning import BODY, NAME, _output
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
    assert data['quality']=='trial' and data['helped_count']==2
    facts = [row for row in data['observations'] if row['authority']=='host']
    assert len(facts)==2 and {row['run_id'] for row in facts}=={'run1','run2'}
    assert all(row['receipt_id'].startswith('verification:') for row in facts)


def test_content_failures_cool_but_environment_and_repeated_job_do_not(tmp_path):
    ctx = _learned(tmp_path)
    def result(status,valid=None):
        return PackVerificationResult('v','p',status,valid=valid)
    # Environmental statuses and repeated observations within the same task do not advance strikes.
    _adopt(ctx, 'task0', 'run0', 'same-job')
    agent, _ = _host(ctx, tmp_path, 'run0')
    for status in ('error', 'not_run', 'cancelled'):
        observe_pack_verification(agent, agent._current_run_params, result(status), status)
    assert ctx.store.load_registry().skills[NAME].maintenance.get('hurt_count', 0) == 0

    for key_idx in range(3):
        observe_pack_verification(agent, agent._current_run_params, result('failed', False), f'dup-{key_idx}')
    assert ctx.store.load_registry().skills[NAME].maintenance['hurt_count'] == 1
    assert ctx.store.load_registry().skills[NAME].maintenance['quality'] == 'trial'

    # Second distinct task reaches hurt_count == 2 while keeping trial state; third retires and blocks NAME.
    _adopt(ctx, 'task1', 'run1', 'same-job')
    agent, _ = _host(ctx, tmp_path, 'run1')
    observe_pack_verification(agent, agent._current_run_params, result('failed', False), 'key1')
    data2 = ctx.store.load_registry().skills[NAME].maintenance
    assert data2['hurt_count'] == 2 and data2['quality'] == 'trial' and data2['state'] == 'trial'

    _adopt(ctx, 'task2', 'run2', 'different-job')
    agent, _ = _host(ctx, tmp_path, 'run2')
    observe_pack_verification(agent, agent._current_run_params, result('failed', False), 'key2')
    after_retire = ctx.store.load_registry()
    assert NAME not in after_retire.skills and NAME in after_retire.blocked_names


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


def _publish_update(ctx):
    current = ctx.store.load_registry().skills[NAME]
    decision = parse_skill_learning_decision(_output('update', update_target=NAME, body=BODY+'\n新版本核对。'), frozenset({NAME}))
    return publish_learned_skill(ctx.store, PublishRequest(decision, expected_revision=current.revision, expected_sha256=current.sha256))


def test_update_wins_feedback_race_preserves_real_verification_and_skips_old_credit(tmp_path, monkeypatch):
    ctx = _learned(tmp_path)
    _adopt(ctx, 'task', 'run')
    agent, project = _host(ctx, tmp_path, 'run')
    call = replace(_call('run_command', {'command': 'pytest -q', 'working_dir': str(project)}), run_id='run')
    ready, resume = threading.Event(), threading.Event()
    errors, results = [], []
    original = feedback.observe_learning

    def paused(*args, **kwargs):
        ready.set()
        assert resume.wait(10)
        return original(*args, **kwargs)

    def verify():
        try:
            results.append(record_tool_verification(agent, call, _success(call, '', {'process': {'status': 'exited', 'return_code': 0}})))
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(feedback, 'observe_learning', paused)
    thread = threading.Thread(target=verify)
    thread.start()
    try:
        assert ready.wait(10)
        assert _publish_update(ctx).event == 'updated'
    finally:
        resume.set()
        thread.join(10)
    assert not thread.is_alive() and errors == []
    assert len(results) == 1 and results[0].ok
    details = results[0].metadata['handler_details']
    assert details['verification_evidence']['status'] == 'passed'
    assert details['skill_learning_feedback'][0]['reason'] == 'SKILL_LEARNING_REVISION_CONFLICT'
    assert ctx.store.load_registry().skills[NAME].maintenance.get('helped_count', 0) == 0


def test_feedback_holding_original_state_lock_finishes_before_update(tmp_path, monkeypatch):
    ctx = _learned(tmp_path)
    _adopt(ctx, 'task', 'run')
    agent, _ = _host(ctx, tmp_path, 'run')
    held, release, attempted, updated = (threading.Event() for _ in range(4))
    original_lock = SkillLearningStore.state_lock
    errors, credited = [], []

    @contextmanager
    def tracked_lock(store):
        if threading.current_thread().name == 'learning-update':
            attempted.set()
        with original_lock(store) as admission:
            observing = threading.current_thread().name == 'learning-observe'
            if observing:
                held.set()
                assert release.wait(10)
            yield admission
            if observing:
                credited.append(store.load_registry().skills[NAME].maintenance['helped_count'])

    def work(update=False):
        try:
            if update:
                _publish_update(ctx)
                updated.set()
            else:
                observe_pack_verification(agent, agent._current_run_params, PackVerificationResult('v', 'p', 'passed', valid=True), 'receipt')
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(SkillLearningStore, 'state_lock', tracked_lock)
    observer = threading.Thread(target=work, name='learning-observe')
    updater = threading.Thread(target=lambda: work(True), name='learning-update')
    observer.start()
    try:
        assert held.wait(10)
        updater.start()
        assert attempted.wait(10) and not updated.is_set()
    finally:
        release.set()
        observer.join(10)
        if updater.ident is not None:
            updater.join(10)
    assert not observer.is_alive() and not updater.is_alive() and errors == []
    assert credited == [1] and updated.is_set()
    assert ctx.store.load_registry().skills[NAME].maintenance['helped_count'] == 0


def test_feedback_storage_and_unknown_gate_errors_are_not_silently_successful(tmp_path, monkeypatch):
    ctx = _learned(tmp_path)
    _adopt(ctx, 'task', 'run')
    agent, _ = _host(ctx, tmp_path, 'run')
    result = PackVerificationResult('v', 'p', 'passed', valid=True)
    for error in (OSError('storage failed'), SkillLearningGateError('SKILL_LEARNING_NOT_LEARNED')):
        def fail(*args, **kwargs):
            raise error
        with monkeypatch.context() as patch:
            patch.setattr(feedback, 'observe_learning', fail)
            with pytest.raises(type(error)) as caught:
                observe_pack_verification(agent, agent._current_run_params, result, 'receipt')
            assert caught.value is error
