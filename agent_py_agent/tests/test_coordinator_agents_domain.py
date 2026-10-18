# LLM: 只通过实际冷C HTTP、原G1/work与原AGENTS领域检查，不替业务dispatcher/权限/seal；初始资源由原fixture原入口建立。
# 模块用途: 验证冷C的普通用户与群AGENTS查看、预览、准确确认和重放，C不能物化Agent。
import http.client
import json

import pytest

from agent_py_agent.agent.gateway_parts.control_operation_service import (
    read_gateway_control_operation,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
from agent_py_agent.tests.test_coordinator_control_forward import cold


# LLM: 原gateway credential与headers来自临时server，body仅准确命令/消息及群事实；不调用领域服务或构造授权对象。
# 函数用途: 把一条用户控制送进真实HTTP，保留原响应及构造计数。
def submit(case, command, message, *, as_group=False, actor='alice'):
    server = case.server
    conversation = 'oc_coldagents' if as_group else 'control-session'
    metadata = {'message_id': message}
    if as_group:
        metadata.update(channel_chat_type='group', channel_chat_id=conversation)
    body = {'command': command, 'conversation_id': conversation, 'metadata': metadata}
    headers = {'Content-Type': 'application/json', 'X-Channel': case.owner.provider,
               'X-User-Id': actor, 'X-Gateway-Token': server._local_client_credential}
    count = len(case.constructions)
    connection = http.client.HTTPConnection('127.0.0.1', server.server.server_address[1], timeout=8)
    try:
        connection.request('POST', '/control', json.dumps(body), headers)
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        assert len(case.constructions) == count and server.agent is None
        return result
    finally:
        connection.close()


@pytest.mark.parametrize('cold,as_group', [('local',False),('feishu',False),('feishu',True)], indirect=['cold'])
def test_actual_cold_agents_original_preview_confirm_and_replay(cold, as_group):  # noqa: F811
    status, shown = submit(cold, '/agents', 'cold-agents-show', as_group=as_group)
    assert status == 200 and shown['ok'] and shown['details']['state'] == 'loaded', (status, shown)
    owner = OwnerIdentity.provider_group('feishu','oc_coldagents') if as_group else cold.owner
    home = resolve_owner_home(cold.agent.home_paths.root, owner).home_dir
    original = (home/'AGENTS.md').read_bytes()
    text = '经过原用户确认的约定\n保留尾空白  '
    command = '/agents write ' + json.dumps(text, ensure_ascii=False)
    status, preview = submit(cold, command, 'cold-agents-preview', as_group=as_group)
    assert status == 200 and preview['ok'] and preview['details']['state'] == 'preview', (status, preview)
    assert (home/'AGENTS.md').read_bytes() == original
    assert preview['details']['value'] == text
    confirmation = preview['details']['confirm_command']
    status, done = submit(cold, confirmation, 'cold-agents-confirm', as_group=as_group)
    assert status == 200 and done['ok'] and done['details']['state'] == 'completed', (status, done)
    assert (home/'AGENTS.md').read_text() == text
    receipt = read_gateway_control_operation(cold.server.paths, done['operation_id'], read_only=True)
    assert receipt.state == 'completed' and receipt.dispatch is None
    assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == (owner.provider, owner.owner_kind, owner.owner_id)
    status, replay = submit(cold, confirmation, 'cold-agents-confirm', as_group=as_group)
    assert status == 200 and replay == done
    assert (home/'AGENTS.md').read_text() == text
    status, consumed = submit(cold, confirmation, 'cold-agents-new-confirm', as_group=as_group)
    assert status == 200 and not consumed['ok'] and consumed['error_code'] == 'AGENTS_WRITE_REPLAYED'
    if as_group:
        status, denied = submit(cold, confirmation, 'cold-agents-other-actor', as_group=True, actor='bob')
        assert status == 200 and not denied['ok']
        assert denied['error_code'] == 'AGENTS_WRITE_BINDING_MISMATCH'
    assert cold.server.agent is None and cold.constructions == []


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
@pytest.mark.parametrize('as_group', [False, True])
def test_actual_cold_agents_observer_uses_current_server_and_emits_once(cold, monkeypatch, as_group):  # noqa: F811
    from agent_py_agent.agent.gateway_parts import plugin_panels_http
    cold.server.control_plane.config.plugin_events_enabled = True
    observed = []
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event',
        lambda server, owner, event: observed.append((server, owner, event)))
    status, result = submit(cold, '/agents show', 'event-original', as_group=as_group)
    assert status == 200 and result['ok'], (status, result)
    assert len(observed) == 1
    server, owner, event = observed[0]
    assert server is cold.server and event.type == 'command_executed'
    assert event.facts == {'command': '/agents', 'operation_id': result['operation_id'], 'state': 'ok'}
    receipt = read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
    assert receipt.conversation_thread_id
    expected = OwnerIdentity.provider_group('feishu', 'oc_coldagents') if as_group else cold.owner
    assert owner.home_dir == resolve_owner_home(cold.agent.home_paths.root, expected).home_dir
    assert submit(cold, '/agents show', 'event-original', as_group=as_group) == (status, result)
    assert len(observed) == 1


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
def test_ordinary_foreign_agents_denial_keeps_actor_thread_but_never_creates_target(cold):  # noqa: F811
    target = OwnerIdentity.provider_user('feishu', 'bob')
    target_home = resolve_owner_home(cold.agent.home_paths.root, target)
    assert not target_home.home_dir.exists()
    status, result = submit(cold, '/agents owner ' + target_home.owner_id + ' show', 'deny-foreign')
    assert status == 200 and not result['ok'] and result['error_code'] == 'AGENTS_WRITE_CROSS_OWNER_FORBIDDEN'
    receipt = read_gateway_control_operation(cold.server.paths, result['operation_id'], read_only=True)
    assert receipt.conversation_thread_id and not target_home.home_dir.exists()
    thread = cold.seeded.conversation_store.threads.require(receipt.conversation_thread_id)
    assert thread.owner_id == cold.seeded.home_paths.owner_id


# LLM: 原main绑定/真实G1/C/Permission由临时delivery fixture建立；不替业务dispatcher，target先由规范owner入口创建。
# 函数用途: 验证管理员跨owner规则确认同时保本人thread P2与目标正文P2，失败或重复不能换owner。
def test_actual_admin_foreign_agents_uses_distinct_actor_and_target_gates(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.multiprocess_route import owner_digest
    from agent_py_agent.agent.user_space.owner_resolver import ensure_owner_home
    from agent_py_agent.tests.test_coordinator_model_menu import delivery_case, post_model
    with delivery_case(tmp_path, monkeypatch, kind='main') as (case, delivered):
        server, row = case[5], case[3]
        config = server.control_plane.config
        config.access_mode = 'full-access'
        server.auth_middleware.auth_manager.admin_user_id = row['actor_id']
        target = OwnerIdentity.provider_user('feishu', 'foreign-agents-target')
        home = ensure_owner_home(server.control_plane.home_root, target)
        original = home.agents_md.read_bytes()
        from agent_py_agent.agent.capability import agents_source_local
        from agent_py_agent.agent.conversation.store_threads import ThreadStore
        calls, active, writes = [], [], []
        create, write = ThreadStore.get_or_create, agents_source_local._write_text_locked
        def actual_thread(store, *args, **kwargs):
            writes.append(('thread', store.storage.root, tuple(active)))
            return create(store, *args, **kwargs)
        def actual_body(repo, *args, **kwargs):
            writes.append(('body', repo.owner_home, tuple(active)))
            return write(repo, *args, **kwargs)
        monkeypatch.setattr(ThreadStore, 'get_or_create', actual_thread)
        monkeypatch.setattr(agents_source_local, '_write_text_locked', actual_body)
        controller = server.control_plane.controller()
        launch = controller.launch_owner_writer
        def observe(owner, operation):
            calls.append(owner)
            def original_operation():
                active.append(owner)
                try:
                    return operation()
                finally:
                    active.pop()
            return launch(owner, original_operation)
        monkeypatch.setattr(controller, 'launch_owner_writer', observe)
        def send(text, message):
            return post_model(server, {'command': text, 'conversation_id': 'foreign-agents-session',
                'metadata': {'message_id': message, 'channel_chat_type': 'p2p',
                             'channel_chat_id': 'foreign-agents-session'}}, route='/control', actor=row['actor_id'])
        raw = '/agents owner ' + home.owner_id + ' write "跨owner原确认"'
        status, preview = send(raw, 'foreign-preview')
        assert status == 200 and preview.get('details', {}).get('state') == 'preview', (status, preview)
        assert home.agents_md.read_bytes() == original
        status, done = send(preview['details']['confirm_command'], 'foreign-confirm')
        assert status == 200 and done['ok'], (status, done)
        assert home.agents_md.read_text() == '跨owner原确认'
        receipt = read_gateway_control_operation(server.paths, done['operation_id'], read_only=True)
        assert receipt.owner_provider == 'local' and receipt.owner_kind == 'main' and receipt.owner_id == 'main'
        assert receipt.conversation_thread_id
        actor_home = resolve_owner_home(server.control_plane.home_root, OwnerIdentity.local_main())
        assert actor_home.home_dir != home.home_dir
        actor_digest, target_digest = owner_digest(OwnerIdentity.local_main()), owner_digest(target)
        assert set(calls) == {actor_digest, target_digest}
        assert len([row for row in writes if row[0] == 'thread']) == 2
        assert len([row for row in writes if row[0] == 'body']) == 1
        for kind, path, held in writes:
            assert held == (actor_digest if kind == 'thread' else target_digest,)
            assert path.is_relative_to(actor_home.home_dir if kind == 'thread' else home.home_dir)
        assert receipt.conversation_thread_id not in [x.stem for x in home.home_dir.rglob('threads/*.json')]
        before = list(calls)
        assert send(preview['details']['confirm_command'], 'foreign-confirm') == (status, done)
        assert calls == before and not delivered and server.agent is None
