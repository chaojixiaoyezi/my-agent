"""通用模型作业经真实Gateway、W2派发和canonical会话任务推进。"""
import json
from uuid import uuid4

import pytest

from agent_py_agent.agent.conversation.session_tasks import SessionTaskUpdate
from agent_py_agent.agent.gateway_parts import business_api_v13 as v13
from agent_py_agent.agent.plugin_host.contracts import DomainResult
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_space

# 绑定原夹具对象，保留 pytest 发现语义，避免导入名与测试参数触发重定义。
v13_native = harness.v13_native


# LLM: 测试仅注册合成kind，仍使用正式HTTP注册器与原W2受理路径。
# 函数用途: 注册一个结果闭合的结构化模型作业。
@pytest.fixture
def model_kind(monkeypatch):
    result = {'type':'object','properties': {'score': {'type':'integer'}},
        'required':['score'],'additionalProperties':False}
    accepted = {'type':'object','properties': {'state': {'const':'accepted'},
        'operation_id': {'$ref':'#/$defs/Id'}, 'job_id': {'$ref':'#/$defs/Id'},
        'task_id': {'$ref':'#/$defs/Id'}},'required':['state','operation_id','job_id','task_id'],
        'additionalProperties':False}
    params = {'type':'object','properties': {'space_id': {'$ref':'#/$defs/Id'},
        'instruction': {'type':'string','minLength':1}}, 'required':['space_id','instruction'],
        'additionalProperties':False}
    definition = v13.V13Action('example.job.start', 'write', params, result,
        lambda call: v13.v13_start_model_job(call, 'example_job', call.params['instruction']),
        accepted_result_schema=accepted, ready=v13._jobs_ready)
    monkeypatch.setitem(v13.JOB_KINDS, 'example_job', {'result_schema':result, 'finalize':json.loads})
    monkeypatch.setitem(v13._ACTIONS, definition.name, definition)
    return definition


# LLM: 准确owner服务从真实请求缓存获取，不捏造owner上下文。
# 函数用途: 取得本测试用户当前使用的领域服务。
def current_services(native):
    return next(value for value in native[0]._business_services.values()
        if value.owner_id.endswith('/business-alice'))


# LLM: 辅助操作只经过原W2任务状态机与canonical消息追加。
# 函数用途: 给后台作业保存可核实的最终模型正文及完成状态。
def complete(native, task_id, text, *, failed=False):
    services = current_services(native)
    store = services.host.conversation_store
    task = store.session_tasks.require(task_id)
    store.session_tasks.advance(task_id, SessionTaskUpdate(status='accepted'))
    store.messages.append({'thread_id':task.target_thread_id,'role':'assistant','content':text,
        'metadata': {'task_id':task_id}})
    store.session_tasks.advance(task_id, SessionTaskUpdate(status='failed' if failed else 'done',
        summary='这不是结构化模型输出'))


# LLM: 返回结果必须来自当前任务正文，summary和别的任务消息均不能补空。
# 函数用途: 验证受理、幂等、隔离、完成及schema有效结果的真实HTTP链。
def test_model_job_states_and_schema_validated_result(v13_native, model_kind):
    space = v13_space(v13_native)
    key = 'job-' + uuid4().hex
    params = {'space_id':space,'instruction':'返回score整数'}
    status, body = v13_call(v13_native, model_kind.name, params,
        user='business-alice', idempotency_key=key)
    assert status == 202 and body['state'] == 'accepted' and body['error'] is None, body
    job = body['data']
    assert job['job_id'] and job['task_id'] and body['operation_id']
    replay = v13_call(v13_native, model_kind.name, params,
        user='business-alice', idempotency_key=key)
    assert replay[0] == 202 and replay[1]['data'] == job
    changed = v13_call(v13_native, model_kind.name, {**params,'instruction':'不同输入'},
        user='business-alice', idempotency_key=key)
    assert changed[0] == 409 and changed[1]['error']['code'] == 'IDEMPOTENCY_CONFLICT'
    status, accepted = v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-alice')
    assert status == 200 and accepted['data']['state'] == 'accepted', accepted
    assert accepted['data']['result'] is None and accepted['data']['error'] is None
    status, denied = v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'
    complete(v13_native, job['task_id'], '{"score":7}')
    status, final = v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-alice')
    assert status == 200 and final['state'] == 'completed' and final['error'] is None, final
    assert final['data']['state'] == 'completed' and final['data']['result'] == {'score':7}
    assert final['data']['coverage'] == 'complete' and final['data']['error'] is None
    assert v13_call(v13_native, 'async_jobs.get', {'job_id':job['job_id']}, user='business-alice')[1]['data'] == final['data']


# LLM: 坏JSON和不符schema的JSON都转作业失败，不是HTTP成功伪造模型结果。
# 函数用途: 验证完成后的输出格式校验与任务失败。
@pytest.mark.parametrize('text,failed,error_code', [('not json',False,'JOB_RESULT_INVALID'),
    ('{"score":"wrong"}',False,'JOB_RESULT_INVALID'), ('{"score":3}',True,'JOB_TASK_FAILED')])
def test_invalid_result_never_fabricated(v13_native, model_kind, text, failed, error_code):
    space = v13_space(v13_native)
    status, body = v13_call(v13_native, model_kind.name, {'space_id':space,'instruction':'结构化输出'},
        user='business-alice', idempotency_key='job-' + uuid4().hex)
    assert status == 202, body
    complete(v13_native, body['data']['task_id'], text, failed=failed)
    status, observed = v13_call(v13_native, 'async_jobs.get', {'job_id':body['data']['job_id']}, user='business-alice')
    assert status == 200, observed
    assert observed['data']['state'] == 'failed' and observed['data']['result'] is None
    assert observed['data']['error']['code'] == error_code


# LLM: 未声明kind不能派发，缺源不能把模型任务当同步完成。
# 函数用途: 验证模型端口缺失时显式拒绝及DomainResult受理形状。
def test_job_requires_kind_and_native_sources(v13_native, model_kind, monkeypatch):
    space = v13_space(v13_native)
    monkeypatch.delitem(v13.JOB_KINDS, 'example_job')
    status, body = v13_call(v13_native, model_kind.name, {'space_id':space,'instruction':'无kind'},
        user='business-alice', idempotency_key='job-' + uuid4().hex)
    assert status == 503 and body['error']['code'] == 'HOST_NOT_READY'
    assert body['state'] != 'completed' and body['data'] == {}
    assert DomainResult(202, {'state':'accepted'}).http_status == 202


# LLM: 派发事实只由W0保存；相同业务意图的重放不能重算挂载快照或新派任务。
# 函数用途: 验证首次上下文钉住、动态提示变化重放及结果约束钩子。
@pytest.mark.parametrize('score,expected', [(3, 'completed'), (9, 'failed')])
def test_context_facts_are_pinned_and_validate_result_hook(v13_native, model_kind, monkeypatch, score, expected):
    from dataclasses import replace

    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    space = v13_space(v13_native)
    facts = {'max_score': 5}

    # LLM: 同一请求派生的运行提示可随目录变化，但首次任务上下文不可重写。
    # 函数用途: 调用带上下文的正式公共作业助手。
    def start(call):
        return v13.v13_start_model_job(call, 'example_job', call.params['instruction'] + json.dumps(facts),
            context_facts=facts)

    monkeypatch.setitem(v13._ACTIONS, model_kind.name, replace(model_kind, handler=start))
    monkeypatch.setitem(v13.JOB_KINDS['example_job'], 'validate_result',
        lambda call, row, result: result['score'] <= row['context_facts']['max_score'])
    params = {'space_id': space, 'instruction': '按固定上下文生成'}
    key = 'context-' + uuid4().hex
    status, accepted = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 202, accepted
    facts['max_score'] = 20
    status, replay = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 202 and replay['data'] == accepted['data'], replay
    services = current_services(v13_native)
    stored = V13Store(services.owner_home, owner_id=services.owner_id).get('async_jobs', accepted['data']['job_id'])
    assert stored['context_facts'] == {'max_score': 5}
    complete(v13_native, accepted['data']['task_id'], json.dumps({'score': score}))
    status, observed = v13_call(v13_native, 'async_jobs.get', {'job_id': stored['job_id']}, user='business-alice')
    assert status == 200 and observed['data']['state'] == expected, observed
    if expected == 'failed':
        assert observed['data']['error']['code'] == 'JOB_RESULT_INVALID' and observed['data']['result'] is None


# LLM: 终态已提交后回调失败可重试，回调不能更改公开或持久job，也不能重复派模型。
# 函数用途: 验证终态回调首次失败及后续终态读取的恢复入口。
def test_terminal_hook_retries_after_job_commit(v13_native, model_kind, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.plugin_host.contracts import HostAPIError
    calls = []

    # LLM: 模拟分片自身幂等回写暂时失源；只改传入副本不影响W0记录。
    # 函数用途: 记录终态回调并注入一次可重试故障。
    def finish(call, job):
        calls.append((call.owner_id, job['job_id'], job['state']))
        job['state'] = 'unknown'
        if len(calls) == 1:
            raise HostAPIError('HOST_NOT_READY', 503)

    monkeypatch.setitem(v13.JOB_KINDS['example_job'], 'on_complete', finish)
    space = v13_space(v13_native)
    status, accepted = v13_call(v13_native, model_kind.name, {'space_id': space, 'instruction': '终态回调'},
        user='business-alice', idempotency_key='finish-' + uuid4().hex)
    assert status == 202, accepted
    job = accepted['data']
    complete(v13_native, job['task_id'], '{"score":3}')
    status, failed = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
    assert status == 503 and failed['error']['code'] == 'HOST_NOT_READY', failed
    services = current_services(v13_native)
    assert V13Store(services.owner_home, owner_id=services.owner_id).get('async_jobs', job['job_id'])['state'] == 'completed'
    for _ in range(2):
        status, final = v13_call(v13_native, 'async_jobs.get', {'job_id': job['job_id']}, user='business-alice')
        assert status == 200 and final['data']['state'] == 'completed', final
    assert len(calls) == 3 and all(row[2] == 'completed' for row in calls)


# LLM: W2已派发而W0 journal尚未提交时注入故障，必须用首次上下文恢复原任务。
# 函数用途: 验证派发前耐久预留及中断后的真实HTTP恢复，不手工制造任务事实。
def test_assignment_before_job_commit_recovers_first_context(v13_native, model_kind, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.business_spaces import v13_store as storage
    space = v13_space(v13_native)
    facts, dispatched = {'catalog_revision': 1}, []
    failure = {'pending': True}
    original_call, original_write = v13.v13_call_frozen, storage.write_bytes_atomic_beneath

    # LLM: 相同params可派生不同目录提示，重试仍须使用第一次快照。
    # 函数用途: 调用携带可变目录上下文的正式helper。
    def start(call):
        return v13.v13_start_model_job(call, 'example_job', call.params['instruction'] + json.dumps(facts),
            context_facts=facts)

    # LLM: 不伪造W2回包，仅记录真实派发得到的任务引用。
    # 函数用途: 捕获模型作业的真实受理结果。
    def dispatch(call, action, params, *, idempotency_key=None, expected_revision=None):
        result = original_call(call, action, params, idempotency_key=idempotency_key,
            expected_revision=expected_revision)
        if action == 'tasks.assign':
            dispatched.append(v13._assignment_receipt(result)['task_id'])
        return result

    # LLM: 只中断W0派发后的提交，不拦W2事务或准备阶段。
    # 函数用途: 在真实W0 journal提交前注入一次磁盘故障。
    def interrupt(owner, parts, raw):
        if dispatched and failure['pending'] and parts[-1] == '.transaction.json':
            failure['pending'] = False
            raise OSError('synthetic precommit interruption')
        return original_write(owner, parts, raw)

    monkeypatch.setitem(v13._ACTIONS, model_kind.name, replace(model_kind, handler=start))
    monkeypatch.setattr(v13, 'v13_call_frozen', dispatch)
    monkeypatch.setattr(storage, 'write_bytes_atomic_beneath', interrupt)
    params, key = {'space_id': space, 'instruction': '固定首次目录'}, 'precommit-' + uuid4().hex
    status, failed = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 503 and len(dispatched) == 1, failed
    services = current_services(v13_native)
    store = storage.V13Store(services.owner_home, owner_id=services.owner_id)
    rows = store.list('async_jobs')
    assert len(rows) == 1 and rows[0]['state'] == 'unknown' and rows[0]['task_id'] is None
    assert rows[0]['context_facts'] == {'catalog_revision': 1}
    pinned_instruction = rows[0]['instruction']
    facts['catalog_revision'] = 2
    status, recovered = v13_call(v13_native, model_kind.name, params, user='business-alice', idempotency_key=key)
    assert status == 202 and recovered['data']['task_id'] == dispatched[0], recovered
    assert len(set(dispatched)) == 1
    final = store.get('async_jobs', recovered['data']['job_id'])
    assert final['context_facts'] == {'catalog_revision': 1} and final['instruction'] == pinned_instruction
    assert services.host.conversation_store.session_tasks.require(final['task_id']).goal == pinned_instruction


# LLM: 校验器是只读业务约束，即便它误写参数也不能污染持久事实和公开结果。
# 函数用途: 实测validate_result的两个参数均为副本。
def test_result_validator_cannot_mutate_job_or_output(v13_native, model_kind, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    # LLM: 主动误改传入对象，用最终HTTP与Store读回证伪共享引用。
    # 函数用途: 注入会改写参数的合成校验器。
    def validator(call, row, result):
        row['state'] = 'failed'
        result['score'] = 999
        return True

    monkeypatch.setitem(v13.JOB_KINDS['example_job'], 'validate_result', validator)
    space = v13_space(v13_native)
    status, accepted = v13_call(v13_native, model_kind.name, {'space_id': space, 'instruction': '只读校验'},
        user='business-alice', idempotency_key='readonly-' + uuid4().hex)
    assert status == 202, accepted
    complete(v13_native, accepted['data']['task_id'], '{"score":3}')
    status, observed = v13_call(v13_native, 'async_jobs.get', {'job_id': accepted['data']['job_id']}, user='business-alice')
    assert status == 200 and observed['data']['state'] == 'completed' and observed['data']['result'] == {'score': 3}, observed
    services = current_services(v13_native)
    row = V13Store(services.owner_home, owner_id=services.owner_id).get('async_jobs', accepted['data']['job_id'])
    assert row['state'] == 'completed' and row['result'] == {'score': 3}
