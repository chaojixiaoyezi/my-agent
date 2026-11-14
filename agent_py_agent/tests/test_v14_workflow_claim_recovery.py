"""W3：真实进程在绑定边界退出，以及释放确认的单点 I/O 恢复。"""
from __future__ import annotations

import errno
import inspect
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from agent_py_agent.agent.assets.script_store import ScriptStore
from agent_py_agent.agent.assets.workflow_store import WorkflowStore
from agent_py_agent.agent.workflows import concurrency_policy as policy
from agent_py_agent.agent.workflows import run_store
from agent_py_agent.agent.workflows.engine import WorkflowEngine


# LLM: 只有本测试的真实引擎执行定义；不读生产 Home 或调用模型。
# 函数用途: 提供单步骤、可观察副作用的工作流。
def _definition():
    return {'descriptor': {'id': 'recovery-flow', 'kind': 'workflow', 'name': '恢复核对',
            'access_targets': [], 'data_sources': [], 'credential_refs': [], 'io_contract': {}},
            'steps': [{'step_id': 'analysis', 'name': '核对', 'mode': 'llm_skill_analysis'}]}


# LLM: 回调入口与绑定边界分开记，不能用边界文件冒充回调执行。
# 函数用途: 构造记录实际回调入口的真实引擎。
def _engine(home):
    def analyze(spec, context, skills):
        with (home / 'callbacks.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(context.run_inputs.get('label', 'original') + '\n')
        return {'value': 7}
    return WorkflowEngine(home, analysis_handler=analyze)


# LLM: 每个受控退出只覆盖一个真实边界；排队场景由 Event 驱动原 holder 释放。
# 函数用途: 子进程执行崩溃、释放确认错误、重启恢复或新运行。
def _child(home, mode, strategy):
    home = Path(home)
    current = _engine(home)
    if mode in ('bootstrap', 'promotion', 'released-bootstrap'):
        if mode == 'released-bootstrap':
            assert current.run_workflow(_definition(), workflow_run_id='original',
                                        concurrency_policy=strategy).status == 'succeeded'
        bind = run_store.WorkflowRunStore._bind_concurrency_claim

        def boundary(store, lease):
            if lease.record['run_id'] == 'original':
                (home / 'boundary').write_text('post-create/pre-bind', encoding='utf-8')
                os._exit(41)
            return bind(store, lease)

        run_store.WorkflowRunStore._bind_concurrency_claim = boundary
        if mode == 'released-bootstrap':
            current.resume_workflow_run('original')
            raise AssertionError('released bootstrap boundary not entered')
        if mode == 'promotion':
            entered, release = threading.Event(), threading.Event()
            analyze = current.analysis_handler

            def holder(spec, context, skills):
                if context.run_inputs.get('label') == 'holder':
                    entered.set()
                    assert release.wait(10)
                return analyze(spec, context, skills)

            current.analysis_handler = holder
            with ThreadPoolExecutor(1) as pool:
                first = pool.submit(current.run_workflow, _definition(), {'label': 'holder'},
                                    workflow_run_id='holder', concurrency_policy='queue')
                assert entered.wait(5)
                queued = current.run_workflow(_definition(), workflow_run_id='original', concurrency_policy='queue')
                assert queued['status'] == 'queued'
                release.set()
                assert first.result(5).status == 'succeeded'
                threading.Event().wait(10)
            raise AssertionError('promotion boundary not entered')
        current.run_workflow(_definition(), workflow_run_id='original', concurrency_policy=strategy)
        raise AssertionError('bootstrap boundary not entered')
    if mode == 'ack-error':
        write = run_store._write_json
        errors = []

        def save(root, parts, value):
            proof = value.get('concurrency_claim', {}) if isinstance(value, dict) else {}
            if proof.get('phase') == 'released' and not errors:
                errors.append(proof['record']['nonce'])
                raise OSError(errno.EIO, 'simulated I/O error')
            return write(root, parts, value)

        run_store._write_json = save
        result = current.run_workflow(_definition(), workflow_run_id='original', concurrency_policy=strategy)
        assert result.status == 'succeeded' and len(errors) == 1
        assert current.store._read('original', 'run.json')['concurrency_claim']['phase'] == 'release_pending'
        return
    if mode == 'resume':
        assert current.resume_workflow_run('original').status == 'succeeded'
    else:
        assert current.run_workflow(_definition(), {'label': 'following'}, workflow_run_id='following',
                                    concurrency_policy=strategy).status == 'succeeded'


# LLM: 子进程沿当前工作树导入，继承隔离 Home；必须回收准确进程并核验退出码。
# 函数用途: 运行一次真实无界面引擎进程。
def _process(home, mode, strategy='skip_if_running'):
    return subprocess.run([sys.executable, '-B', '-c', (
        'import sys; from agent_py_agent.tests.test_v14_workflow_claim_recovery import _child; '
        '_child(sys.argv[1], sys.argv[2], sys.argv[3])'), str(home), mode, strategy],
        cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=20)


# LLM: 原 callback 从未进入才能回收；后继进程必须通过正式引擎成功运行。
# 函数用途: 核对两个限制策略和真实排队提升的未绑定崩溃恢复。
@pytest.mark.parametrize('mode,strategy', [('bootstrap', 'skip_if_running'), ('bootstrap', 'queue'),
                                         ('promotion', 'queue')])
def test_unbound_child_exit_recovers_without_replaying_callback(tmp_path, mode, strategy):
    home = tmp_path / 'home'
    exited = _process(home, mode, strategy)
    assert exited.returncode == 41, exited.stderr
    assert (home / 'boundary').read_text() == 'post-create/pre-bind'
    callbacks = (home / 'callbacks.jsonl').read_text().splitlines() if (home / 'callbacks.jsonl').exists() else []
    assert callbacks == (['holder'] if mode == 'promotion' else [])
    store = run_store.WorkflowRunStore(home)
    assert store.load_run('original').status == 'queued'
    assert 'concurrency_claim' not in store._read('original', 'run.json')
    following = _process(home, 'following', strategy)
    assert following.returncode == 0, following.stderr
    assert store._read('original', 'run.json')['concurrency_claim']['phase'] == 'released'
    assert (home / 'callbacks.jsonl').read_text().splitlines() == callbacks + ['following']


# LLM: 错误后换新进程，不能靠本进程清理缓存；同一成功运行恢复不能重跑步骤。
# 函数用途: 核对释放 ACK 单次错误后的原运行恢复和后继准入。
@pytest.mark.parametrize('strategy', ['skip_if_running', 'queue'])
def test_release_ack_error_converges_before_same_run_rebind(tmp_path, strategy):
    home = tmp_path / 'home'
    exited = _process(home, 'ack-error', strategy)
    assert exited.returncode == 0, exited.stderr
    resumed = _process(home, 'resume', strategy)
    assert resumed.returncode == 0, resumed.stderr
    following = _process(home, 'following', strategy)
    assert following.returncode == 0, following.stderr
    assert (home / 'callbacks.jsonl').read_text().splitlines() == ['original', 'following']
    assert run_store.WorkflowRunStore(home)._read('original', 'run.json')['concurrency_claim']['phase'] == 'released'


# LLM: 未绑定的租约来自真实子进程，不凭伪造死亡 PID 推导恢复权。
# 函数用途: 对活、未知、损坏证明与未结步骤验证 fail-closed。
@pytest.mark.parametrize('change', ['live', 'unknown', 'malformed', 'foreign', 'stamp', 'obligation'])
def test_absent_proof_recovery_requires_positive_exact_evidence(tmp_path, monkeypatch, change):
    home = tmp_path / 'home'
    exited = _process(home, 'bootstrap')
    assert exited.returncode == 41, exited.stderr
    store = run_store.WorkflowRunStore(home)
    scope = policy._scope(home, 'workflow', 'recovery-flow', 'original')
    with policy._gate(scope) as parent:
        lease = policy._claim(scope, parent)
        if change in ('live', 'unknown'):
            monkeypatch.setattr(policy, '_identity_live', lambda identity: True if change == 'live' else None)
        elif change in ('malformed', 'obligation'):
            data = store._read('original', 'run.json')
            if change == 'malformed':
                data['concurrency_claim'] = {}
            else:
                data['steps'][0]['escalation_result'] = {'effect_started': True, 'submission_state': 'unknown'}
            run_store._write_json(home, ('data', 'workflow_runs', 'original', 'run.json'), data)
        elif change == 'foreign':
            lease = replace(lease, record={**lease.record, 'asset_id': 'different-asset'})
        else:
            lease = replace(lease, stamp=(lease.stamp[0], lease.stamp[1] + 1))
        before = store._read('original', 'run.json')
        with pytest.raises(ValueError, match='WORKFLOW_CONCURRENCY_CLAIM_MISMATCH'):
            store._concurrency_claim_phase(lease, recover_exited=True)
        assert store._read('original', 'run.json') == before
        assert policy._claim(scope, parent) is not None


def test_ast_guard_recovery_order_and_runner_parameters():
    for cls in (ScriptStore, WorkflowStore):
        method = inspect.signature(cls.record_run_summary)
        assert {'run_at', 'last_error', 'blocking', 'run_id'} <= method.parameters.keys()
        assert not any(p.kind == p.VAR_KEYWORD for p in method.parameters.values())


# LLM: 原释放 ACK 在新进程再遇单点错误也只能重试同代，不能擦除旧证明后直接绑定。
# 函数用途: 核对旧代 ACK 在发表前后错误的幂等重试及新代绑定顺序。
@pytest.mark.parametrize('published', [False, True])
def test_previous_release_ack_retries_once_before_new_generation(tmp_path, monkeypatch, published):
    home = tmp_path / 'home'
    exited = _process(home, 'ack-error')
    assert exited.returncode == 0, exited.stderr
    current = _engine(home)
    old = current.store._read('original', 'run.json')['concurrency_claim']
    write = run_store._write_json
    events = []

    def save(root, parts, value):
        proof = value.get('concurrency_claim', {}) if isinstance(value, dict) else {}
        if proof.get('record', {}).get('nonce') == old['record']['nonce'] and proof.get('phase') == 'released':
            if not events:
                events.append('ack-error')
                if published:
                    write(root, parts, value)
                raise OSError(errno.EIO, 'simulated I/O error')
            events.append('acknowledged')
        if proof.get('phase') == 'executing' and proof['record']['nonce'] != old['record']['nonce']:
            assert current.store._read('original', 'run.json')['concurrency_claim']['phase'] == 'released'
            events.append('new-generation')
        return write(root, parts, value)

    monkeypatch.setattr(run_store, '_write_json', save)
    assert current.resume_workflow_run('original').status == 'succeeded'
    assert events == (['ack-error', 'new-generation'] if published else
                      ['ack-error', 'acknowledged', 'new-generation'])
    assert (home / 'callbacks.jsonl').read_text().splitlines() == ['original']


# LLM: 真实取消先提交终态，回收只补清理证明；不允许随后准入或恢复复活已取消运行。
# 函数用途: 验证未绑定退出后取消与后继执行的终态单调性。
def test_cancelled_unbound_run_is_not_revived_on_recovery(tmp_path):
    home = tmp_path / 'home'
    exited = _process(home, 'bootstrap')
    assert exited.returncode == 41, exited.stderr
    current = _engine(home)
    original = current.store.load_run('original')
    current.cancel_workflow_run('original', expected_revision=original.revision, operation_key='cancel-original')
    assert current.store.load_run('original').status == 'cancelled'
    assert current.run_workflow(_definition(), {'label': 'following'}, workflow_run_id='after-cancel',
                                concurrency_policy='skip_if_running').status == 'succeeded'
    with pytest.raises(ValueError):
        current.resume_workflow_run('original')
    assert current.store.load_run('original').status == 'cancelled'
    assert (home / 'callbacks.jsonl').read_text().splitlines() == ['following']


# LLM: released 前代是已确认完成事实，不等同于缺证明；后继仍须用准确新叶和死亡身份证明。
# 函数用途: 复现已完成运行续跑在新 claim 绑定前退出的同类窗口。
@pytest.mark.parametrize('strategy', ['skip_if_running', 'queue'])
def test_released_previous_generation_does_not_block_dead_unbound_successor(tmp_path, strategy):
    home = tmp_path / 'home'
    exited = _process(home, 'released-bootstrap', strategy)
    assert exited.returncode == 41, exited.stderr
    store = run_store.WorkflowRunStore(home)
    original = store._read('original', 'run.json')
    assert original['status'] == 'succeeded' and original['concurrency_claim']['phase'] == 'released'
    following = _process(home, 'following', strategy)
    assert following.returncode == 0, following.stderr
    assert store.load_run('original').status == 'succeeded'
    assert (home / 'callbacks.jsonl').read_text().splitlines() == ['original', 'following']


# LLM: 外来proof即使在另一真实run上完全合法，也不能借对方canonical为当前run授权恢复或ACK。
# 函数用途: 验证三个旧代交接入口都先锚定当前运行，拒绝时双方账本原样保留。
@pytest.mark.parametrize('entry,phase', [('recover', 'released'), ('bind', 'release_pending'),
                                       ('abort', 'release_pending'), ('bind', 'released'),
                                       ('abort', 'released')])
def test_foreign_existing_proof_cannot_modify_either_run(tmp_path, entry, phase):
    home = tmp_path / 'home'
    current = _engine(home)
    assert current.run_workflow(_definition(), workflow_run_id='other',
                                concurrency_policy='skip_if_running').status == 'succeeded'
    exited = _process(home, 'bootstrap')
    assert exited.returncode == 41, exited.stderr
    store = current.store
    other = store._read('other', 'run.json')
    other['concurrency_claim']['phase'] = phase
    run_store._write_json(home, ('data', 'workflow_runs', 'other', 'run.json'), other)
    original = store._read('original', 'run.json')
    original['concurrency_claim'] = json.loads(json.dumps(other['concurrency_claim']))
    run_store._write_json(home, ('data', 'workflow_runs', 'original', 'run.json'), original)
    before = {name: store._read(name, 'run.json') for name in ('original', 'other')}
    scope = policy._scope(home, 'workflow', 'recovery-flow', 'original')
    with policy._gate(scope) as parent:
        lease = policy._claim(scope, parent)
        with pytest.raises(ValueError, match='WORKFLOW_CONCURRENCY_CLAIM_MISMATCH'):
            if entry == 'recover':
                store._concurrency_claim_phase(lease, recover_exited=True)
            elif entry == 'bind':
                store._bind_concurrency_claim(lease)
            else:
                store._abort_concurrency_binding(lease)
        assert policy._claim(scope, parent) == lease
    assert {name: store._read(name, 'run.json') for name in before} == before
