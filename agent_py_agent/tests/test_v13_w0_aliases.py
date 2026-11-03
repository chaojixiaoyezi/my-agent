"""thread_id参数显式归一、真实会话消息和原W2任务直派回归。"""
from uuid import uuid4

import pytest

from agent_py_agent.agent.business_spaces.v13_store import V13Store
from agent_py_agent.agent.gateway_parts import business_api as api
from agent_py_agent.tests import test_v13_w0_harness as harness
from agent_py_agent.tests.test_v13_w0_harness import v13_call, v13_space

# 绑定原夹具对象，保留 pytest 发现语义，避免导入名与测试参数触发重定义。
v13_native = harness.v13_native


# LLM: 通过冻结原生创建后台身份；不把显示名解释成线程归属。
# 函数用途: 创建带规范空间的真实后台线程。
def background(native):
    space = v13_space(native)
    status, body = v13_call(native, 'background_agents.create', {'space_id':space,'name':'别名测试'},
        user='business-alice', idempotency_key='alias-' + uuid4().hex)
    assert status == 200, body
    services = next(row for row in native[0]._business_services.values() if row.owner_id.endswith('/business-alice'))
    return space, body['data']['agent']['thread_id'], services


# LLM: 别名与规范参数走同一canonical消息和游标签名。
# 函数用途: 验证消息、事件别名真实HTTP和显式参数冲突。
def test_thread_aliases_messages_events_and_conflicts(v13_native):
    _space, thread, services = background(v13_native)
    message = services.host.conversation_store.messages.append({'thread_id':thread,'role':'assistant','content':'别名原文'})
    status, alias = v13_call(v13_native, 'messages.list', {'thread_id':thread}, user='business-alice')
    assert status == 200 and alias['error'] is None, alias
    assert message.message_id in [row['message_id'] for row in alias['data']['items']]
    status, canonical = v13_call(v13_native, 'messages.list',
        {'target': {'kind':'background_main','id':thread}}, user='business-alice')
    assert status == 200 and canonical['data']['items'] == alias['data']['items']
    status, events = v13_call(v13_native, 'events.list', {'thread_id':thread,'filter':{}}, user='business-alice')
    assert status == 200 and events['state'] == 'completed' and events['error'] is None, events
    conflicts = [('messages.list', {'thread_id':thread,'target':{'kind':'background_main','id':thread}}),
        ('events.list', {'thread_id':thread,'filter':{'agent_id':'other-thread'}})]
    for action, params in conflicts:
        status, body = v13_call(v13_native, action, params, user='business-alice')
        assert status == 400 and body['error']['code'] == 'INVALID_SCHEMA'
    status, denied = v13_call(v13_native, 'messages.list', {'thread_id':thread}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 原生直派仍经W2幂等账与原submit受理schema，不调用模型线程。
# 函数用途: 验证thread_id任务别名与真实任务ID及空间边界。
def test_submit_thread_alias_uses_native_assignment(v13_native):
    space, thread, services = background(v13_native)
    params = {'thread_id':thread,'space_id':space,'instruction':'记录规范任务'}
    key = 'alias-task-' + uuid4().hex
    status, body = v13_call(v13_native, 'tasks.submit', params, user='business-alice', idempotency_key=key)
    assert status == 202 and body['state'] == 'accepted', body
    task = services.host.conversation_store.session_tasks.require(body['data']['task_id'])
    assert task.target_thread_id == thread and task.goal == params['instruction']
    status, replay = v13_call(v13_native, 'tasks.submit', params, user='business-alice', idempotency_key=key)
    assert status == 202 and replay['data']['task_id'] == task.task_id
    for extra in ({'asset_refs':[]}, {'controller_agent_id':thread}):
        status, bad = v13_call(v13_native, 'tasks.submit', {**params,**extra},
            user='business-alice', idempotency_key='alias-bad-' + uuid4().hex)
        assert status == 400 and bad['error']['code'] == 'INVALID_SCHEMA'
    status, other = v13_call(v13_native, 'spaces.create', {'name':'别的空间'},
        user='business-alice', idempotency_key='alias-space-' + uuid4().hex)
    assert status == 200
    status, mismatch = v13_call(v13_native, 'tasks.submit', {**params,'space_id':other['data']['space_id']},
        user='business-alice', idempotency_key='alias-mismatch-' + uuid4().hex)
    assert status == 404 and mismatch['error']['code'] == 'SPACE_SCOPE_MISMATCH'


# LLM: W11会话登记是读取普通线程的必要事实，不允许任意同owner线程冒充后台身份。
# 函数用途: 验证规范会话消息、分页和未登记线程拒绝。
def test_registered_conversation_thread_messages(v13_native):
    space = v13_space(v13_native)
    services = next(row for row in v13_native[0]._business_services.values() if row.owner_id.endswith('/business-alice'))
    native_store = services.host.conversation_store
    paths = services.host.home_paths
    thread = native_store.threads.create(canonical_user_id='business-alice', title='普通会话',
        owner_id=services.owner_id, owner_home=str(paths.owner_home_dir))
    status, refused = v13_call(v13_native, 'messages.list', {'thread_id':thread.thread_id}, user='business-alice')
    assert status == 404 and refused['error']['code'] == 'RESOURCE_NOT_FOUND'
    store = V13Store(services.owner_home, owner_id=services.owner_id)
    row = {'conversation_id':thread.thread_id,'owner_id':services.owner_id,'space_id':space,'revision':0}
    store.transaction('conversations', lambda rows: rows.setdefault(thread.thread_id, row),
        idempotency_key='conversation-' + uuid4().hex, params_digest=uuid4().hex)
    expected = [native_store.messages.append({'thread_id':thread.thread_id,'role':'assistant','content':f'原文{index}'}).message_id
        for index in range(3)]
    status, first = v13_call(v13_native, 'messages.list', {'thread_id':thread.thread_id,'limit':2}, user='business-alice')
    assert status == 200 and first['data']['coverage'] == 'partial', first
    assert first['data']['next_cursor'] is not None
    status, second = v13_call(v13_native, 'messages.list',
        {'thread_id':thread.thread_id,'limit':2,'cursor':first['data']['next_cursor']}, user='business-alice')
    assert status == 200 and second['data']['coverage'] == 'complete', second
    assert {item['message_id'] for page in (first,second) for item in page['data']['items']} == set(expected)
    assert all(item['space_id'] == space for item in first['data']['items'])
    status, denied = v13_call(v13_native, 'messages.list', {'thread_id':thread.thread_id}, user='business-bob')
    assert status == 404 and denied['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 原可信context末核撤权不能由别名绕过；冻结target参数仍原样工作。
# 函数用途: 验证伪造context不获得thread别名访问。
def test_alias_rejects_unissued_context(v13_native):
    _space, thread, services = background(v13_native)
    status, body = api.execute_business({'action':'messages.list','request_id':'fake-context',
        'params':{'thread_id':thread}}, object(), services)
    assert status == 403 and body['error']['code'] == 'CAPABILITY_DENIED'


# LLM: 路径只由可信组合根绑定；业务JSON不能传路径，句柄本身也不授予跨owner读取。
# 函数用途: 核验真实native owner宿主拿到准确Gateway路径且外部输入仍闭合。
def test_native_gateway_paths_are_trusted_internal_binding(v13_native):
    _space, thread, services = background(v13_native)
    assert services.host.business_v13_gateway_paths is v13_native[0].paths
    assert services.owner_home != services.host.business_v13_gateway_paths.root
    status, bad = v13_call(v13_native, 'messages.list', {'thread_id': thread,
        'gateway_paths': {'root': '/synthetic/other-owner'}}, user='business-alice')
    assert status == 400 and bad['error']['code'] == 'INVALID_SCHEMA'
