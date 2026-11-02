"""R6-R1：真实插件授权须贯穿原 SessionTask 取消闭包。"""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.host_actor import HostChannelActor
from agent_py_agent.agent.gateway_parts.business_api import (
    _BusinessBackgroundService,
    bind_business_context,
)
from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
    plugin_observation_task,
)
from agent_py_agent.tests.test_plugin_host_gateway_composition import production_call
from agent_py_agent.tests.test_plugin_host_gateway_composition import (
    production_host as production_host,
)
from agent_py_agent.tests.test_plugin_host_v2_grants import http_request


@pytest.fixture(autouse=True)
def deterministic_transport_windows(monkeypatch):
    """只推进限流器的时间观察点，保持真实窗口、安装桶和正常配额。"""
    from itertools import count

    from agent_py_agent.agent.plugin_host import http_transport
    ticks = count(start=1000, step=2)
    monkeypatch.setattr(http_transport, 'time', SimpleNamespace(
        **{**vars(http_transport.time), 'monotonic': lambda: next(ticks)}))


def subtree(host, *, user=None, private=True, private_sender=False):
    """真实安装创建可见根；原生 assign 用原 origin_task_id 产生后代。"""
    if user:
        production_call(host, 'users.local.create', {'user_id': user}, key='round-six-create-user', status=201)
    services, parent = plugin_observation_task(host, user=user)
    store = services.host.conversation_store
    services.host.config.model_backend = 'echo'
    source = store.threads.require(parent.sender_thread_id)
    actor = HostChannelActor(services.owner_id, source.thread_id, 'r6-native-entry', 'local', 'local', 'human')
    context = bind_business_context(services.host, actor=actor, authorize=lambda _a, _f: True)
    native = _BusinessBackgroundService(services, context)
    space = parent.metadata['business_context']['space_id']
    if private:
        target = native.create({'space_id': space, 'name': '原生私有身份'}, actor=actor,
            idempotency_key='r6-native-agent')['agent']['thread_id']
    else:
        target = production_call(host, 'business.background_agents.create',
            {'space_id': space, 'name': '安装可见身份'}, user=user, key='round-six-plugin-agent')['agent']['thread_id']
    if private_sender:
        row = store.threads.create(owner_id=services.owner_id, owner_home=str(services.owner_home))
        actor = replace(actor, source_thread_id=row.thread_id)
    child_id = native.assign({'agent_id': target, 'space_id': space, 'instruction': '保留准确原正文',
        'origin_task_id': parent.task_id}, actor=actor, idempotency_key='r6-native-child')['task_id']
    child = store.session_tasks.require(child_id)
    return SimpleNamespace(services=services, store=store, parent=parent, child=child, native=native,
        actor=actor, user=user, target=target)


def snapshot(case, task):
    """包括规范正文、guidance、wake、claim；不能只断言任务展示状态。"""
    receipt = case.store.guidance.receipt(task.body_dedupe_key) if task.body_dedupe_key else None
    return (case.store.session_tasks.require(task.task_id).to_dict(),
        receipt.to_dict() if receipt else None,
        [row.to_dict() for row in case.store.wakes.pending(limit=0)
            if row.metadata.get('session_task_id') == task.task_id],
        case.store.claims.load_report(task.target_thread_id))


def cancel_http(host, task, *, user=None, descendants=True, key='round-six-original-cancel'):
    request = {'schema': 'plugin_host_call.v2', 'action': 'business.tasks.cancel',
        'payload': {'task_id': task.task_id, 'include_descendants': descendants, 'expected_revision': 0},
        'idempotency_key': key}
    if user:
        request['as_user'] = {'user_id': user}
    return http_request(host[0], host[1], request)


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'ordinary-owner'])
@pytest.mark.parametrize('private_sender', [False, True], ids=['private-target', 'private-source'])
def test_direct_and_visible_ancestor_cannot_cancel_private_child(production_host, user, private_sender):
    case = subtree(production_host, user=user, private=not private_sender, private_sender=private_sender)
    original, root = snapshot(case, case.child), snapshot(case, case.parent)
    status, denied = cancel_http(production_host, case.child, user=user)
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'
    assert snapshot(case, case.child) == original
    status, denied = cancel_http(production_host, case.parent, user=user, key='round-six-ancestor-cancel')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND', denied
    assert snapshot(case, case.child) == original
    assert snapshot(case, case.parent) == root, 'preflight denial must precede even root cancellation intent'


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'ordinary-owner'])
@pytest.mark.parametrize('descendants', [False, True], ids=['root-only', 'whole-subtree'])
def test_permitted_plugin_subtree_and_root_only_control(production_host, user, descendants):
    case = subtree(production_host, user=user, private=False)
    original = snapshot(case, case.child)
    status, result = cancel_http(production_host, case.parent, user=user, descendants=descendants)
    assert status == 200 and result['data']['state'] == 'completed', result
    assert result['data']['outstanding_refs'] == []
    assert case.store.session_tasks.require(case.parent.task_id).status == 'cancelled'
    if descendants:
        assert case.store.session_tasks.require(case.child.task_id).status == 'cancelled'
        assert case.store.guidance.receipt(case.child.body_dedupe_key).status == 'rejected'
    else:
        assert snapshot(case, case.child) == original


@pytest.mark.parametrize('user', [None, 'alice'], ids=['main', 'ordinary-owner'])
def test_trusted_native_subtree_still_cancels_private_original_tasks(production_host, user):
    case = subtree(production_host, user=user)
    reply = case.native.cancel_task(case.parent.task_id, actor=case.actor, include_descendants=True)
    assert reply['state'] == 'completed' and reply['outstanding_refs'] == []
    assert case.store.session_tasks.require(case.child.task_id).status == 'cancelled'
    assert case.store.guidance.receipt(case.child.body_dedupe_key).status == 'rejected'


@pytest.mark.parametrize('private_descendant', [False, True], ids=['private-task', 'private-descendant'])
def test_retire_cancel_tasks_uses_same_live_task_authority(production_host, private_descendant):
    case = subtree(production_host, private=private_descendant, private_sender=not private_descendant)
    # 退休目标本身可见；责任可以是不可见任务本身，也可以是其不可见后代。
    target = case.parent.target_thread_id if private_descendant else case.child.target_thread_id
    if private_descendant:
        # 原观察夹具的目标不是后台身份，用原创建的可见身份接收一条真正原生任务。
        permitted = production_call(production_host, 'business.background_agents.create',
            {'space_id': case.parent.metadata['business_context']['space_id'], 'name': '退休原身份'},
            key='round-six-retire-visible')['agent']['thread_id']
        task_id = case.native.assign({'agent_id': permitted, 'space_id': case.parent.metadata['business_context']['space_id'],
            'instruction': '原退休任务'}, actor=case.actor, idempotency_key='round-six-retire-root')['task_id']
        parent = case.store.session_tasks.require(task_id)
        child_id = case.native.assign({'agent_id': case.child.target_thread_id,
            'space_id': case.parent.metadata['business_context']['space_id'], 'instruction': '原退休私有后代',
            'origin_task_id': parent.task_id}, actor=case.actor, idempotency_key='round-six-retire-private')['task_id']
        case.child = case.store.session_tasks.require(child_id)
        target = permitted
    original = snapshot(case, case.child)
    row = case.store.threads.require(target)
    revision = row.metadata['background_agent']['identity_revision']
    request = {'schema': 'plugin_host_call.v2', 'action': 'business.background_agents.retire',
        'payload': {'agent_id': target, 'disposition': 'cancel_tasks', 'expected_revision': revision},
        'idempotency_key': 'round-six-retire-cancel-tasks'}
    status, denied = http_request(production_host[0], production_host[1], request)
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND', denied
    assert snapshot(case, case.child) == original


def test_live_installation_revocation_after_root_effect_stops_later_target(production_host, monkeypatch):
    from agent_py_agent.tests.test_mega_business_plugin_compatibility import (
        revoke_business_installation,
    )
    case = subtree(production_host, private=False)
    original = snapshot(case, case.child)
    advance = case.store.session_tasks.advance
    effects = []

    def revoke_after_root(task_id, *args, **kwargs):
        result = advance(task_id, *args, **kwargs)
        effects.append(task_id)
        if task_id == case.parent.task_id:
            revoke_business_installation(production_host)
        return result

    monkeypatch.setattr(case.store.session_tasks, 'advance', revoke_after_root)
    status, denied = cancel_http(production_host, case.parent)
    assert status in {401, 403}, denied
    assert effects == [case.parent.task_id]
    assert snapshot(case, case.child) == original
    root = case.store.session_tasks.require(case.parent.task_id)
    assert root.status == 'cancelled'
    assert 'descendants:pending' in root.metadata['cancellation_control']['outstanding_refs']


@pytest.mark.parametrize('result', [False, 1, None, 'true', ValueError('denied'), OSError('denied')])
def test_canonical_authorizer_requires_exact_true_and_never_absorbs_errors(production_host, result):
    from agent_py_agent.agent.conversation.session_messaging import cancel_owned_session_task
    case = subtree(production_host, private=False)
    original = snapshot(case, case.parent)

    def authorize(task):
        assert task == case.store.session_tasks.require(task.task_id)
        if isinstance(result, Exception):
            raise result
        return result

    with pytest.raises((PermissionError, ValueError, OSError)):
        cancel_owned_session_task(case.services.host, case.parent, include_descendants=True, authorize_task=authorize)
    assert snapshot(case, case.parent) == original
