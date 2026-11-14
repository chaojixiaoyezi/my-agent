"""W11 会话与 Copilot：真实 Gateway 往返、线程别名、上下文事实与流式过滤。"""
from __future__ import annotations

import http.client
import json
from uuid import uuid4

from agent_py_agent.tests.test_v13_w0_harness import (  # noqa: F401  共享夹具
    v13_call,
    v13_native,
)


# LLM: 唯一被测响应来自真实Gateway，不手写替代信封。
# 函数用途: 发起一次业务调用并返回HTTP状态与解码正文。
def call(native, action, params, *, idempotency_key=None, expected_revision=None, user='business-alice'):
    return v13_call(native, action, params, user=user, idempotency_key=idempotency_key,
        expected_revision=expected_revision)


# LLM: 幂等键必须满足冻结模式，测试用合成值不带真实凭据。
# 函数用途: 生成一次调用用的稳定幂等键。
def key(prefix):
    return prefix + '-' + uuid4().hex


# LLM: 空间名在owner内唯一，测试用唯一名创建独立空间。
# 函数用途: 经真实W1入口创建当前用户空间并返回ID。
def new_space(native, name):
    status, body = call(native, 'spaces.create', {'name': name}, idempotency_key=key('space'))
    assert status == 200, body
    return body['data']['space_id']


# LLM: 会话ID与线程ID来自真实创建回执，不从标题或路径推断。
# 函数用途: 经HTTP新建会话并返回会话视图。
def conversation(native, space_id, title):
    status, body = call(native, 'conversations.create', {'space_id': space_id, 'title': title},
        idempotency_key=key('conversation'))
    assert status == 200, body
    assert body['state'] == 'completed' and body['error'] is None
    return body['data']['conversation']


# LLM: 种入走原Store事务，不手写生产文件形状。
# 函数用途: 返回当前用户真实owner存储句柄。
def owner_store(native, user='business-alice'):
    from agent_py_agent.agent.business_spaces.v13_store import V13Store

    server = native[0] if isinstance(native, tuple) else native
    [services] = [item for item in server._business_services.values() if item.owner_id.endswith('/' + user)]
    return V13Store(services.owner_home, owner_id=services.owner_id), services


# LLM: 记录ID由记录内的规范ID字段决定，不按顺序猜。
# 函数用途: 向指定集合种入一条规范记录并返回其ID。
def seed(native, collection, record):
    store, _ = owner_store(native)
    identifier = next(item for item in record if item.endswith('_id') and item != 'owner_id')

    # LLM: 同名记录不覆盖，重复种入直接报错。
    # 函数用途: 写入一条种子记录。
    def mutate(rows):
        assert record[identifier] not in rows
        rows[record[identifier]] = record
        return {'count': len(rows)}

    store.transaction(collection, mutate, idempotency_key='seed-' + uuid4().hex,
        params_digest='seed-' + uuid4().hex)
    return record[identifier]


# LLM: 上下文事实只来自本owner真实记录，形状与W8集合声明一致。
# 函数用途: 种入一条告警记录并返回ID。
def seed_alert(native, space_id, *, title='可疑登录', description='合成描述'):
    _, services = owner_store(native)
    now = '2026-11-02T00:00:00+00:00'
    return seed(native, 'soc_alerts', {'alert_id': 'alert-' + uuid4().hex[:12], 'space_id': space_id,
        'title': title, 'description': description, 'severity': 'high', 'status': 'new', 'verdict': None,
        'ai_confidence': 0.82, 'disposition': None, 'rule_id': None, 'source': 'monitor',
        'external_ref': None, 'entities': [{'type': 'account', 'value': 'svc-synthetic'}],
        'techniques': ['T1078'], 'assignee': None, 'case_id': None, 'tags': ['synthetic'],
        'evidence_refs': [], 'acknowledged_at': None, 'resolved_at': None, 'revision': 0,
        'created_at': now, 'updated_at': now, 'owner_id': services.owner_id})


# LLM: 目录请求同样走真实HTTP，不直接调用handler绕过运输层。
# 函数用途: 读取指定上下文类型的快捷动作目录。
def catalog(native, context_kind):
    status, body = call(native, 'copilot.contextual_actions.list', {'context_kind': context_kind})
    assert status == 200, body
    assert body['error'] is None
    return body['data']['items']


# LLM: 成功路径必须真的受理，依赖来源失败不能充当成功断言的替代分支。
# 函数用途: 核验冻结线程别名或Copilot动作的精确202载荷。
def assert_accepted(status, body, *, task_alias=False):
    assert status == 202, body
    assert body['state'] == 'accepted' and body['error'] is None
    expected = ({'task_id', 'request_id', 'operation_id', 'state'} if task_alias else
                {'state', 'operation_id', 'conversation_id', 'task_id', 'thread_id'})
    assert set(body['data']) == expected
    assert body['data']['state'] == 'accepted' and body['data']['task_id']


# LLM: 流入口沿用既有 NDJSON 路由，只按线程过滤，不新增执行器。
# 函数用途: 打开一次真实业务事件流并返回连接与响应。
def open_stream(native, request, *, user='business-alice', timeout=20):
    server = native[0] if isinstance(native, tuple) else native
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=timeout)
    connection.request('POST', '/client/business/stream', json.dumps(request).encode(),
        {'X-Gateway-Token': server._local_client_credential, 'X-User-Id': user,
         'X-Channel': 'feishu', 'Content-Type': 'application/json'})
    return connection, connection.getresponse()


# LLM: 归档态默认排除；显式 archived 与空间筛选都必须读真实记录。
# 函数用途: 验证示例参数下的会话列表筛选。
def test_list_example_params_filters_archived(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    other_space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    archived = conversation(v13_native, space, '待归档会话')
    kept = conversation(v13_native, space, '保留会话')
    foreign = conversation(v13_native, other_space, '另一空间会话')
    status, body = call(v13_native, 'conversations.archive', {'conversation_id': archived['conversation_id']},
        idempotency_key=key('archive'), expected_revision=archived['revision'])
    assert status == 200, body

    status, listed = call(v13_native, 'conversations.list', {'space_id': space, 'limit': 20})
    assert status == 200, listed
    assert listed['state'] == 'completed' and listed['error'] is None
    assert [row['conversation_id'] for row in listed['data']['items']] == [kept['conversation_id']]
    assert listed['data']['coverage'] in {'complete', 'partial', 'unknown'}
    assert listed['data']['next_cursor'] is None

    status, hidden = call(v13_native, 'conversations.list', {'space_id': space, 'archived': True})
    assert status == 200, hidden
    assert [row['conversation_id'] for row in hidden['data']['items']] == [archived['conversation_id']]
    assert hidden['data']['items'][0]['archived'] is True

    status, others = call(v13_native, 'conversations.list', {'space_id': other_space})
    assert status == 200, others
    assert [row['conversation_id'] for row in others['data']['items']] == [foreign['conversation_id']]

    status, filtered = call(v13_native, 'conversations.list', {'space_id': space, 'query': '保留'})
    assert status == 200, filtered
    assert [row['conversation_id'] for row in filtered['data']['items']] == [kept['conversation_id']]

    status, stranger = call(v13_native, 'conversations.list', {'space_id': space}, user='business-bob')
    assert status == 200 and stranger['data']['items'] == [], stranger


# LLM: 新建会话必须复用冻结后台身份，会话ID就是线程ID，可被三个线程别名接受。
# 函数用途: 验证新建会话与 tasks.submit/messages.list/events.list 的 thread_id 别名。
def test_create_then_thread_id_aliases_work(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    reuse_key = key('create')
    alert_id = seed_alert(v13_native, space)
    params = {'space_id': space, 'title': '解释会话',
        'context_ref': {'kind': 'alert', 'id': alert_id}}
    status, created = call(v13_native, 'conversations.create', params, idempotency_key=reuse_key)
    assert status == 200, created
    row = created['data']['conversation']
    assert row['conversation_id'] == row['thread_id'] == row['agent_id']
    assert row['space_id'] == space and row['archived'] is False and row['revision'] == 0
    assert row['context_ref'] == {'kind': 'alert', 'id': alert_id}

    status, replay = call(v13_native, 'conversations.create', params, idempotency_key=reuse_key)
    assert status == 200 and replay['data']['conversation'] == row
    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(listed['data']['items']) == 1, listed

    status, native_agent = call(v13_native, 'background_agents.get', {'agent_id': row['agent_id']})
    assert status == 200, native_agent
    assert native_agent['data']['agent']['thread_id'] == row['thread_id']

    status, messages = call(v13_native, 'messages.list', {'thread_id': row['thread_id']})
    assert status == 200, messages
    assert set(messages['data']) == {'items', 'next_cursor', 'coverage', 'history_boundary'}

    status, events = call(v13_native, 'events.list', {'thread_id': row['thread_id']})
    assert status == 200, events
    assert set(events['data']) == {'events', 'next_cursor', 'coverage', 'reset_required'}

    status, submitted = call(v13_native, 'tasks.submit',
        {'thread_id': row['thread_id'], 'space_id': space, 'instruction': '单步解释该告警'},
        idempotency_key=key('submit'))
    assert_accepted(status, submitted, task_alias=True)


# LLM: 详情恢复只读原生消息页，缺失与跨owner一致返回404。
# 函数用途: 验证会话详情与消息分页来自同一线程。
def test_get_restores_messages_page(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    row = conversation(v13_native, space, '详情会话')
    status, detail = call(v13_native, 'conversations.get',
        {'conversation_id': row['conversation_id'], 'limit': 10})
    assert status == 200, detail
    assert detail['state'] == 'completed' and detail['error'] is None
    assert detail['data']['conversation'] == row
    status, native_page = call(v13_native, 'messages.list', {'thread_id': row['thread_id'], 'limit': 10})
    assert status == 200, native_page
    assert detail['data']['messages'] == native_page['data']['items']
    assert detail['data']['coverage'] == native_page['data']['coverage']
    for message in detail['data']['messages']:
        assert message['thread_id'] == row['thread_id']

    status, missing = call(v13_native, 'conversations.get', {'conversation_id': 'conversation-absent'})
    assert status == 404 and missing['error']['code'] == 'RESOURCE_NOT_FOUND'
    status, foreign = call(v13_native, 'conversations.get',
        {'conversation_id': row['conversation_id']}, user='business-bob')
    assert status == 404 and foreign['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 归档走记录级修订，过期修订与同键异参都必须拒绝。
# 函数用途: 验证归档、恢复、修订CAS和幂等冲突。
def test_archive_hides_from_default_list_and_unarchive(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    row = conversation(v13_native, space, '归档目标')
    stale = call(v13_native, 'conversations.archive', {'conversation_id': row['conversation_id']},
        idempotency_key=key('stale'), expected_revision=row['revision'] + 5)
    assert stale[0] == 409 and stale[1]['error']['code'] == 'REVISION_CONFLICT'

    archive_key = key('archive')
    status, archived = call(v13_native, 'conversations.archive',
        {'conversation_id': row['conversation_id']}, idempotency_key=archive_key,
        expected_revision=row['revision'])
    assert status == 200, archived
    view = archived['data']['conversation']
    assert view['archived'] is True and view['revision'] == row['revision'] + 1

    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and listed['data']['items'] == []

    status, conflict = call(v13_native, 'conversations.archive',
        {'conversation_id': row['conversation_id'], 'archived': False},
        idempotency_key=archive_key, expected_revision=view['revision'])
    assert status == 409 and conflict['error']['code'] == 'IDEMPOTENCY_CONFLICT'

    status, restored = call(v13_native, 'conversations.archive',
        {'conversation_id': row['conversation_id'], 'archived': False},
        idempotency_key=key('restore'), expected_revision=view['revision'])
    assert status == 200 and restored['data']['conversation']['archived'] is False, restored
    status, visible = call(v13_native, 'conversations.list', {'space_id': space})
    assert [item['conversation_id'] for item in visible['data']['items']] == [row['conversation_id']]

    status, foreign = call(v13_native, 'conversations.archive', {'conversation_id': row['conversation_id']},
        idempotency_key=key('foreign'), expected_revision=restored['data']['conversation']['revision'],
        user='business-bob')
    assert status == 404 and foreign['error']['code'] == 'RESOURCE_NOT_FOUND'


# LLM: 解释只派发一次并只写会话元数据；进度复用既有 NDJSON 线程过滤流。
# 函数用途: 验证告警解释的受理、幂等防重派与事件流过滤。
def test_explain_streams_via_business_stream_thread_filter(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    alert_id = seed_alert(v13_native, space)
    explain_key = key('explain')
    params = {'context_ref': {'kind': 'alert', 'id': alert_id}, 'space_id': space,
        'question': '为什么判定为高危?'}
    status, body = call(v13_native, 'copilot.explain', params, idempotency_key=explain_key)
    assert_accepted(status, body)

    status, replay = call(v13_native, 'copilot.explain', params, idempotency_key=explain_key)
    assert status == 202 and replay['data'] == body['data'], replay
    status, task = call(v13_native, 'tasks.get', {'task_id': body['data']['task_id']})
    assert status == 200, task

    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(listed['data']['items']) == 1, listed
    created = listed['data']['items'][0]
    assert created['context_ref'] == {'kind': 'alert', 'id': alert_id}
    assert created['title'] == '告警会话 ' + alert_id

    connection, response = open_stream(v13_native, {'request_id': 'explain-stream',
        'filter': {'agent_id': created['thread_id']}})
    try:
        assert response.status == 200, response.read()
        assert response.getheader('Content-Type').startswith('application/x-ndjson')
        snapshot = json.loads(response.readline())
        assert snapshot['stream_item'] == 'snapshot'
    finally:
        response.close()
        connection.close()


# LLM: 未知或脚本动作都不能经 copilot.contextual_action 派发；AI动作只受理一次。
# 函数用途: 验证快捷动作拒绝规则与真实流式受理。
def test_contextual_action_unknown_action_rejected_and_stream(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    alert_id = seed_alert(v13_native, space)
    ref = {'kind': 'alert', 'id': alert_id}
    status, unknown = call(v13_native, 'copilot.contextual_action',
        {'action': 'delete_everything', 'context_ref': ref, 'space_id': space},
        idempotency_key=key('unknown'))
    assert status == 400 and unknown['error']['code'] == 'INVALID_SCHEMA'

    status, missing = call(v13_native, 'copilot.contextual_action',
        {'action': 'summarize', 'context_ref': {'kind': 'alert', 'id': 'alert-absent'}, 'space_id': space},
        idempotency_key=key('missing'))
    assert status == 404 and missing['error']['code'] == 'RESOURCE_NOT_FOUND'

    action_key = key('contextual')
    params = {'action': 'summarize', 'context_ref': ref, 'space_id': space, 'instruction': '只列三条'}
    status, body = call(v13_native, 'copilot.contextual_action', params, idempotency_key=action_key)
    assert_accepted(status, body)
    status, replay = call(v13_native, 'copilot.contextual_action', params, idempotency_key=action_key)
    assert status == 202 and replay['data'] == body['data']

    status, listed = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(listed['data']['items']) == 1, listed
    created = listed['data']['items'][0]
    assert created['title'] == '事实摘要'

    connection, response = open_stream(v13_native, {'request_id': 'contextual-stream',
        'filter': {'agent_id': created['thread_id']}})
    try:
        assert response.status == 200, response.read()
        snapshot = json.loads(response.readline())
        assert snapshot['stream_item'] == 'snapshot'
    finally:
        response.close()
        connection.close()


# LLM: 静态AI目录始终可读；脚本项只来自声明匹配的元数据，不执行代码。
# 函数用途: 验证快捷动作目录的AI与脚本组成。
def test_contextual_actions_catalog_ai_and_scripts(v13_native):  # noqa: F811
    items = catalog(v13_native, 'alert')
    ai_actions = [item for item in items if item['kind'] == 'ai']
    assert [item['action'] for item in ai_actions] == sorted(
        ['explain', 'summarize', 'next_steps', 'related_alerts', 'draft_report'])
    assert all(item['script_id'] is None for item in ai_actions)
    assert items == sorted(items, key=lambda item: item['action'])

    from agent_py_agent.agent.assets.descriptor import AssetDescriptor
    from agent_py_agent.agent.assets.script_store import ScriptStore

    _, services = owner_store(v13_native)
    ScriptStore(services.owner_home).save_script(AssetDescriptor(id='alert_helper', kind='script',
        name='告警分流助手', summary='按告警上下文给出处置清单',
        metadata={'contexts': ['alert']}), 'print("synthetic")', entrypoint='main.py')

    with_script = catalog(v13_native, 'alert')
    scripts = [item for item in with_script if item['kind'] == 'script']
    assert [item['script_id'] for item in scripts] == ['alert_helper']
    assert scripts[0]['action'].startswith('script_')
    assert catalog(v13_native, 'rule') == sorted(
        [item for item in with_script if item['kind'] == 'ai'], key=lambda item: item['action'])

    status, invalid = call(v13_native, 'copilot.contextual_actions.list', {'context_kind': 'host'})
    assert status == 400 and invalid['error']['code'] == 'INVALID_SCHEMA'


# LLM: 读动作 200、异步动作 202、未知动作 400，错误data为空且error非空。
# 函数用途: 验证本片动作的统一信封。
def test_envelopes(v13_native):  # noqa: F811
    space = new_space(v13_native, '空间-' + uuid4().hex[:8])
    standard = {'request_id', 'operation_id', 'state', 'data', 'error', 'allowed_actions', 'warnings',
        'effect_outcome'}

    for action, params in (('conversations.list', {}),
            ('copilot.contextual_actions.list', {'context_kind': 'case'})):
        status, body = call(v13_native, action, params)
        assert status == 200, body
        assert set(body) == standard
        assert body['state'] == 'completed' and body['error'] is None

    status, created = call(v13_native, 'conversations.create', {'space_id': space},
        idempotency_key=key('envelope'))
    assert status == 200 and set(created) == standard and created['error'] is None
    assert created['data']['conversation']['title'] == 'Copilot 会话'

    alert_id = seed_alert(v13_native, space)
    status, accepted = call(v13_native, 'copilot.explain', {'context_ref': {'kind': 'alert', 'id': alert_id},
        'space_id': space, 'conversation_id': created['data']['conversation']['conversation_id']},
        idempotency_key=key('envelope-explain'))
    assert set(accepted) == standard
    assert_accepted(status, accepted)

    status, unsupported = call(v13_native, 'conversations.unknown', {})
    assert status == 400 and unsupported['error']['code'] == 'UNSUPPORTED_ACTION'
    assert unsupported['state'] == 'failed' and unsupported['data'] == {}

    status, refused = call(v13_native, 'copilot.explain', {'context_ref': {'kind': 'alert', 'id': alert_id},
        'space_id': space}, idempotency_key='short')
    assert status == 400 and refused['error']['code'] == 'INVALID_SCHEMA'
    assert refused['data'] == {}


# LLM: 受理结构只由 DomainResult(202) 表达；同键重放必须复用已提交回执，不再派发。
# 函数用途: 用真实Store与已签发上下文核验派发回执与防二次派发。
def test_dispatch_returns_domain_result_202_and_never_dispatches_twice(tmp_path, monkeypatch):
    from pathlib import Path
    from types import SimpleNamespace

    from agent_py_agent.agent.business_spaces import v13_w11_conversations as module
    from agent_py_agent.agent.business_spaces.v13_store import V13Store
    from agent_py_agent.agent.conversation.store import ConversationStore
    from agent_py_agent.agent.gateway_parts import business_api as api
    from agent_py_agent.agent.gateway_parts.business_api_v13 import V13Call
    from agent_py_agent.agent.plugin_host.contracts import DomainResult
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        resolve_owner_home,
    )

    root = tmp_path / 'dispatch-home'
    root.mkdir()
    paths = home_paths_with_owner(home_paths(root), resolve_owner_home(root))
    host = SimpleNamespace(home_paths=paths, config=SimpleNamespace(business_spaces_enabled=True),
        conversation_store=ConversationStore(paths.owner_home_dir / 'conversations'))
    store = V13Store(paths.owner_home_dir, owner_id=paths.owner_id)
    now = '2026-11-02T00:00:00+00:00'
    row = {'conversation_id': 'thread-one', 'thread_id': 'thread-one', 'agent_id': 'thread-one',
        'space_id': 'space-one', 'title': '派发会话', 'archived': False, 'context_ref': None,
        'last_message_at': now, 'revision': 0, 'created_at': now, 'updated_at': now,
        'owner_id': paths.owner_id}

    # LLM: 会话记录经原集合事务种入，不从文件手写。
    # 函数用途: 写入待派发的会话记录。
    def seed(rows):
        rows['thread-one'] = row
        return {'count': 1}

    store.transaction('conversations', seed, idempotency_key='seed-dispatch', params_digest='seed-dispatch')
    # 独立派发测试也准备真实上下文，避免以缺失资源绕过记录级末核。
    store.transaction('soc_alerts', lambda rows: rows.update({'alert-one': {
        'alert_id': 'alert-one', 'space_id': 'space-one', 'owner_id': paths.owner_id,
        'title': '合成告警', 'severity': 'high', 'status': 'new'}}) or {'seeded': 1},
        idempotency_key='seed-context-dispatch', params_digest='seed-context-dispatch')
    context = api.bind_business_context(host, authorize=lambda action, facts: True)
    services = api.BusinessServices(host)
    request = V13Call('copilot.explain', {'context_ref': {'kind': 'alert', 'id': 'alert-one'}},
        'dispatch-request', paths.owner_id, 'host', context, services, Path(paths.owner_home_dir),
        idempotency_key='explain-dispatch-key-01')
    dispatched = []

    # LLM: 只替换跨分派发端口，会话集合、回执与CAS仍走真实Store。
    # 函数用途: 记录派发参数并返回冻结 tasks.assign 形状。
    def frozen(call, action, params, **options):
        assert action == 'tasks.assign' and params['agent_id'] == 'thread-one'
        dispatched.append(params)
        return {'assignment_ref': 'task-nine', 'task_id': 'task-nine', 'receipt_state': 'accepted'}

    monkeypatch.setattr(module, 'v13_call_frozen', frozen)
    expected = {'state': 'accepted', 'operation_id': 'task-nine', 'conversation_id': 'thread-one',
        'task_id': 'task-nine', 'thread_id': 'thread-one'}
    first = module._dispatch(request, space_id='space-one', conversation=row, instruction='解释')
    assert isinstance(first, DomainResult) and first.http_status == 202
    assert dict(first.data) == expected
    replay = module._dispatch(request, space_id='space-one', conversation=row, instruction='解释')
    assert dict(replay.data) == expected
    assert len(dispatched) == 1
    assert store.get('conversations', 'thread-one')['revision'] == 1


# LLM: 只有context_ref也属于合法请求，真实记录的空间必须成为新线程的唯一空间。
# 函数用途: 回归只给上下文的新会话解释与跨空间引用拒绝。
def test_context_only_infers_space_and_cross_space_is_rejected(v13_native):  # noqa: F811
    space = new_space(v13_native, '上下文空间-' + uuid4().hex[:8])
    other = new_space(v13_native, '目标空间-' + uuid4().hex[:8])
    alert = seed_alert(v13_native, space)
    ref = {'kind': 'alert', 'id': alert}
    status, body = call(v13_native, 'copilot.explain', {'context_ref': ref},
        idempotency_key=key('infer-space'))
    assert_accepted(status, body)
    status, rows = call(v13_native, 'conversations.list', {'space_id': space})
    assert status == 200 and len(rows['data']['items']) == 1
    for action, params in (
            ('copilot.explain', {'context_ref': ref, 'space_id': other}),
            ('copilot.contextual_action', {'context_ref': ref, 'space_id': other, 'action': 'summarize'})):
        status, refused = call(v13_native, action, params, idempotency_key=key('cross-space'))
        assert status == 404 and refused['error']['code'] == 'RESOURCE_NOT_FOUND', refused
    status, empty = call(v13_native, 'conversations.list', {'space_id': other})
    assert status == 200 and empty['data']['items'] == []


# LLM: 同键改变上下文/空间要在创建线程之前拒绝，不允许留下孤儿会话。
# 函数用途: 回归分阶段建会话的完整参数幂等准入。
def test_changed_context_key_does_not_create_orphan_conversation(v13_native):  # noqa: F811
    first = new_space(v13_native, '首次-' + uuid4().hex[:8])
    second = new_space(v13_native, '冲突-' + uuid4().hex[:8])
    a, b = seed_alert(v13_native, first), seed_alert(v13_native, second)
    operation = key('whole-request')
    status, accepted = call(v13_native, 'copilot.explain',
        {'space_id': first, 'context_ref': {'kind': 'alert', 'id': a}}, idempotency_key=operation)
    assert_accepted(status, accepted)
    status, refused = call(v13_native, 'copilot.explain',
        {'space_id': second, 'context_ref': {'kind': 'alert', 'id': b}}, idempotency_key=operation)
    assert status == 409 and refused['error']['code'] == 'IDEMPOTENCY_CONFLICT', refused
    status, rows = call(v13_native, 'conversations.list', {'space_id': second})
    assert status == 200 and rows['data']['items'] == [], rows
    status, agents = call(v13_native, 'background_agents.list', {'space_id': second})
    assert status == 200 and agents['data']['items'] == [], agents


# LLM: query必须先过滤再分页，旧cursor不能用于另一个查询条件。
# 函数用途: 回归小页搜索不会漏掉后面的命中会话。
def test_query_filters_before_paging_and_binds_cursor(v13_native):  # noqa: F811
    space = new_space(v13_native, '查询-' + uuid4().hex[:8])
    for title in ('暂定一', '暂定二', '暂定三'):
        conversation(v13_native, space, title)
    store, _ = owner_store(v13_native)
    identifiers = [row['conversation_id'] for row in store.list('conversations')]

    # LLM: 测试记录键排序固定后才设置标题，消除随机线程ID对回归触发条件的影响。
    # 函数用途: 使第一页原始记录不匹配、后两条匹配。
    def retitle(rows):
        for identifier, title in zip(identifiers, ('不相关', '命中甲', '命中乙'), strict=True):
            rows[identifier]['title'] = title
        return {'changed': len(rows)}

    store.transaction('conversations', retitle, idempotency_key=key('retitle'), params_digest='titles')
    params = {'space_id': space, 'query': '命中', 'limit': 1}
    status, first = call(v13_native, 'conversations.list', params)
    assert status == 200 and [row['title'] for row in first['data']['items']] == ['命中甲'], first
    cursor = first['data']['next_cursor']
    assert cursor
    status, second = call(v13_native, 'conversations.list', {**params, 'cursor': cursor})
    assert status == 200 and [row['title'] for row in second['data']['items']] == ['命中乙'], second
    status, stale = call(v13_native, 'conversations.list', {**params, 'query': '不相关', 'cursor': cursor})
    # plan-w11错误表明确沿冻结_error_code保留503 retryable，而不是另造400。
    assert status == 410 and stale['error']['code'] == 'CURSOR_EXPIRED', stale
