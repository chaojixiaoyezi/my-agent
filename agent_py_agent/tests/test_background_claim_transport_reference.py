# LLM: 原run_with_heartbeat→模型尝试→core task guard/callback全链；C/W claim与Store真实，仅模型/归档边界替身，不启动进程/网络。
# 模块用途: 核对同源运输定位的一次claim发布、Compact代次、Count0及原typed未结责任。
import copy
import json
from contextlib import contextmanager
from contextvars import copy_context
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin as core
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.conversation import claim_execution_binding as publisher
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import multiprocess_background_execution as bridge
from agent_py_agent.agent.gateway_parts.multiprocess_coordinator_execution import (
    coordinator_execution_scope,
)
from agent_py_agent.agent.gateway_parts.multiprocess_execution import worker_execution_scope
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.tests.test_background_execution_ports import (
    c_actual as c_actual,
)
from agent_py_agent.tests.test_background_execution_ports import (
    case as case,
)
from agent_py_agent.tests.test_background_execution_ports import (
    claimed,
    claims,
    execution,
)
from agent_py_agent.tests.test_background_execution_ports import (
    context as context,
)
from agent_py_agent.tests.test_background_execution_ports import (
    unknown as unknown,
)
from agent_py_agent.tests.test_gateway_claim_transition_scope import actual as actual


# LLM: 两种fixture都消费原GatewayClaim/P3/P5和真实owner Store，按显式参数选择scope，不伪造执行绑定。
# 函数用途: 给同一普通链路提供真实临时C或W身份。
@pytest.fixture(params=['C', 'W'])
def transport_case(request):
    if request.param == 'C':
        return request.param, request.getfixturevalue('case')
    binding, _request, agent = request.getfixturevalue('actual')
    store = ConversationStore(agent.home_paths.owner_home_dir / 'conversations')
    thread = store.threads.get_or_create({'canonical_user_id': 'owner', 'channel': 'internal',
        'channel_conversation_id': 'transport', 'channel_user_id': 'owner'})
    agent.conversation_store = store
    return request.param, (binding, binding.claim, agent, store, thread, None)


# LLM: 原scope只覆盖本次同步链，finally沿原lifetime退出；context从同一claim精确构造。
# 函数用途: 安装真实C/W和BG组合上下文，不另造claim或执行许可。
@contextmanager
def gateway_scope(kind, case):
    source, claim, agent, *_ = case
    scope = coordinator_execution_scope(source, claim, agent) if kind == 'C' else worker_execution_scope(source, agent)
    context = {'agent': agent, 'request': json.loads(claim.path.read_text()),
               'request_path': claim.path, 'request_id': claim.request_id}
    with scope, bridge.background_request_scope(context):
        yield


# LLM: 真RuntimeDB、原core task guard与发布callback保留；只跳过与本合同无关的归档和模型，不直调binder代替生产链。
# 函数用途: 把模型替身接到真实core身份创建和发布，供后台runner调用。
def install_core(case, tmp_path, monkeypatch, *, nested=False, failure=None):
    _source, _claim, agent, store, _thread, *_ = case
    agent.subagents = SimpleNamespace(runtime_db=RuntimeRepository(tmp_path / 'runtime.sqlite3'))
    monkeypatch.setattr(core, 'attach_run_task_workspace_context', lambda _agent, params, _prompt: params)
    bindings = []

    def run(prompt, *, params):
        def invoke():
            return core._bind_main_agent_turn_params(agent, prompt, params)
        bound = params.active_turn_transition_callback('submit', invoke) if nested else invoke()
        bindings.append(bound)
        if failure is not None:
            raise failure
        return 'offline-result'

    agent.run = run
    return bindings


# LLM: 观察真实publisher的原子updater，不能替代CAS/许可门；每次只写同一claim且两投影一起出现。
# 函数用途: 直接确认claim写入时仍持原BG T，记录原更新后的完整行。
def observe_claim_writes(monkeypatch):
    writes = []
    original = publisher.update_json_file_atomic

    def update(path, updater, **options):
        def checked(current):
            bridge.require_background_transaction('claim_transport_test')
            updated = updater(current)
            assert 'runtime_authority' in updated and 'gateway_transport_reference' in updated
            writes.append(copy.deepcopy(updated))
            return updated
        return original(path, checked, **options)

    monkeypatch.setattr(publisher, 'update_json_file_atomic', update)
    return writes


# LLM: 使用原claim runner、模型尝试和core callback；nested额外证明同T窗口重入不再取非重入T。
# 函数用途: 对照C/W运输fence与会话/core编号，断言同一CAS发布而不是门外写字典。
@pytest.mark.parametrize('nested', [False, True])
def test_original_runner_core_callback_publishes_transport_inside_same_t(transport_case, tmp_path, monkeypatch, nested):
    kind, case = transport_case
    _source, claim, agent, store, thread, *_ = case
    bindings = install_core(case, tmp_path, monkeypatch, nested=nested)
    writes = observe_claim_writes(monkeypatch)
    params = RunParams(request_id='ports-turn', run_id='ports-turn', task_id='ports-task')
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通后台', params)[0])
    with gateway_scope(kind, case):
        assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'offline-result'
    assert len(writes) == len(bindings) == 1
    current = store.claims.load(thread.thread_id)
    ref = current['gateway_transport_reference']
    bound = bindings[0]
    assert ref == {'schema': 'gateway_claim_transport_reference.v1', 'request_id': claim.request_id,
        'gateway_execution_attempt_id': claim.execution_attempt_id, 'lease_epoch': claim.lease_epoch,
        'lease_owner': claim.lease_owner, 'invocation_run_id': 'ports-turn', 'claim_id': row['claim_id'],
        'thread_id': thread.thread_id, 'core_task_id': 'ports-task', 'core_run_id': bound.run_id,
        'core_agent_run_id': current['runtime_authority']['agent_run_id'], 'core_attempt_id': bound.attempt_id}
    assert type(ref['lease_epoch']) is int and ref['request_id'] != ref['invocation_run_id']
    assert current['status'] == 'finished'
    assert json.loads(claim.path.read_text())['runtime_authority']['attempt_id'] == ref['core_attempt_id']


# LLM: 同claim/原request内两次真实core绑定轮换attempt，原run与Gateway fence保持；不伪造Compact身份。
# 函数用途: 防止第二次模型尝试混用旧core attempt或换掉运输定位。
def test_original_compact_rebind_only_changes_core_attempt(transport_case, tmp_path, monkeypatch):
    kind, case = transport_case
    _source, claim, agent, _store, _thread, *_ = case
    install_core(case, tmp_path, monkeypatch)
    writes = observe_claim_writes(monkeypatch)
    params = RunParams(request_id=claim.request_id, run_id=claim.request_id, task_id='ports-task')

    def run_once(_):
        for _ in range(2):
            execution._run_background_model_attempt(agent, '普通续片', params)
        return 'continued'

    deps, args, row, _policy = claimed(case, run_once)
    with gateway_scope(kind, case):
        assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'continued'
    first, second = [item['gateway_transport_reference'] for item in writes]
    assert first['core_attempt_id'] != second['core_attempt_id']
    assert {k: v for k, v in first.items() if k != 'core_attempt_id'} == {k: v for k, v in second.items() if k != 'core_attempt_id'}


# LLM: Count0走同一真实claim/core发布，不安装BG源；原参数同对象且claim无运输字段，未加查询/第二写。
# 函数用途: 守住普通后台原publisher和单次原子写。
def test_count0_original_runner_has_no_transport_field_or_extra_source_query(case, tmp_path, monkeypatch):
    _source, _claim, agent, store, thread, *_ = case
    install_core(case, tmp_path, monkeypatch)
    params = RunParams(request_id='ports-turn', run_id='ports-turn', task_id='ports-task')
    monkeypatch.setattr(bridge, 'current_worker_execution', lambda: pytest.fail('Count0 queried worker'))
    assert bridge.bind_background_run_params(agent, params) is params
    writes = []
    original = publisher.update_json_file_atomic

    def update(*args, **options):
        writes.append(args[0])
        return original(*args, **options)

    monkeypatch.setattr(publisher, 'update_json_file_atomic', update)
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通后台', params)[0])
    assert claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id']) == 'offline-result'
    current = store.claims.load(thread.thread_id)
    assert current['runtime_authority']['claim_id'] == row['claim_id']
    assert 'gateway_transport_reference' not in current and len(writes) == 1


# LLM: 四种原actual异常在core真实发布之后发生；不清原P3/claim、不release未知输入，异常身份逐对象比。
# 函数用途: 保持原后台未结责任，不因新增投影把未知执行误收尾。
def test_original_typed_unknown_after_publish_keeps_claim_and_p3(case, unknown, tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_admission import reservation_file

    source, claim, agent, store, thread, *_ = case
    install_core(case, tmp_path, monkeypatch, failure=unknown)
    released = []
    monkeypatch.setattr(execution, '_release_failed_attempt_inputs', lambda *_: released.append(True))
    params = RunParams(request_id='ports-turn', run_id='ports-turn', task_id='ports-task')
    deps, args, row, policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通后台', params)[0])
    reservation = reservation_file(source.paths, claim.request_id)
    before = reservation.read_bytes()
    with gateway_scope('C', case), pytest.raises(type(unknown)) as caught:
        claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id'])
    assert caught.value is unknown and not released and not policy
    current = store.claims.load(thread.thread_id)
    assert current['status'] == 'running' and current['claim_id'] == row['claim_id']
    assert current['gateway_transport_reference']['request_id'] == claim.request_id
    assert reservation.read_bytes() == before


# LLM: 真实临时请求沿原T写入显式取消事实，仍由原core callback/原source拒绝，不伪造权限或row投影。
# 函数用途: 取消到达时不发布迟到会话运输定位，原未知claim保持未结。
def test_original_cancel_before_core_publish_keeps_claim_unsettled(transport_case, tmp_path, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.gateway_parts.io import (
        gateway_turn_transition,
        update_json_file_atomic,
    )
    from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root

    kind, case = transport_case
    _source, claim, agent, store, thread, *_ = case
    install_core(case, tmp_path, monkeypatch)
    original_run = agent.run

    def run(prompt, *, params):
        paths = gateway_paths_from_root(claim.path.parent.parent.parent)
        with gateway_turn_transition(paths, claim.request_id):
            update_json_file_atomic(claim.path, lambda row: {**row, 'cancel_requested': True, 'turn_phase': 'closing'})
        return original_run(prompt, params=params)

    agent.run = run
    params = RunParams(request_id='ports-turn', run_id='ports-turn', task_id='ports-task')
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通取消', params)[0])
    with gateway_scope(kind, case), pytest.raises(RequestExecutionOutcomeUncertainError):
        claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id'])
    current = store.claims.load(thread.thread_id)
    assert current['status'] == 'running' and 'gateway_transport_reference' not in current


# LLM: 先完成真实生产链再观察已退出来源，复制上下文不能延长原lifetime；只尝试原捕获publisher，不重建许可。
# 函数用途: 确认旧绑定退出后不能重写已发布的claim。
def test_original_lifetime_exit_refuses_captured_claim_publisher(transport_case, tmp_path, monkeypatch):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )

    kind, case = transport_case
    _source, _claim, agent, store, thread, *_ = case
    install_core(case, tmp_path, monkeypatch)
    original_run = agent.run
    captured = []

    def run(prompt, *, params):
        result = original_run(prompt, params=params)
        control = params.conversation_task_binding_callback
        captured.append((copy_context(), control._previous,
                         {'invocation_run_id': params.request_id, **control.runtime_authority()}))
        return result

    agent.run = run
    params = RunParams(request_id='ports-turn', run_id='ports-turn', task_id='ports-task')
    deps, args, row, _policy = claimed(case, lambda _: execution._run_background_model_attempt(agent, '普通完成', params)[0])
    with gateway_scope(kind, case):
        claims.run_with_heartbeat(deps, row['claim_id'], args, claim_scope_id=row['claim_scope_id'])
    path = store.claims.storage.background_claim_path(row['claim_scope_id'] or thread.thread_id)
    before = path.read_bytes()
    context, producer, binding = captured[0]
    with pytest.raises(RequestExecutionOutcomeUncertainError):
        context.run(producer.publish_claim_runtime_authority, binding)
    assert path.read_bytes() == before
