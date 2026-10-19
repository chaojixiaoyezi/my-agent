# LLM: /ask和/control各走真实HTTP/G1/C原来源与领域，唯一Feishu发送替身沿既有delivery_case；不mock分派、不预写结果、不调用模型或外网。
# 模块用途: 固定同身份/正文的首次控制parity及规范回执，防止IM入口绕过已接通的C控制能力。
import json

import pytest

from agent_py_agent.agent.gateway_parts import control_operation_service as ops
from agent_py_agent.tests.test_coordinator_control_forward import cold
from agent_py_agent.tests.test_coordinator_model_card_delivery import delivery_case
from agent_py_agent.tests.test_coordinator_model_menu import post_model


# LLM: 只改变原C是否已拥有原同实例Agent，不创建foreign Agent或替代任何来源/领域方法。
# 函数用途: 在真实HTTP夹具内覆盖冷/ready两种正式状态，并生成两入口完全相同的原控制字段。
def parity_request(case, *, ready, text, group=False, message='parity-message'):
    agent, _host, _path, row, _body, server, _runtime, _sent = case
    if ready:
        agent._gateway_control_plane = server.control_plane
        server.agent = agent
    conversation = row['owner']['owner_id'] if group else 'parity-session'
    metadata = {'message_id': message, 'channel_chat_type': 'group' if group else 'p2p'}
    if group:
        metadata['channel_chat_id'] = conversation
    return {'command': text, 'goal': text, 'prompt': text,
            'conversation_id': conversation, 'metadata': metadata}


# LLM: 只读取测试隔离C的control账与inbox字节；不得用投影生成实际回执或清理失败现场。
# 函数用途: 固定HTTP拒绝前后的业务与控制副作用，时间戳只允许原HTTP观察器自身变化。
def control_files(server):
    return {str(path.relative_to(server.paths.root)): path.read_bytes()
            for directory in [server.paths.root / 'control_operations', server.paths.inbox]
            if directory.exists() for path in directory.rglob('*') if path.is_file()}


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('text', ['/model', '/agents show', '/memory candidates'])
def test_cold_im_first_control_uses_same_actual_C_domain(tmp_path, monkeypatch, route, text):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        agent, host, _path, row, _body, server, _runtime, _sent = case
        body = {'command': text, 'goal': text, 'prompt': text,
                'conversation_id': 'first-parity-session',
                'metadata': {'message_id': 'first-parity-control', 'channel_chat_type': 'p2p'}}
        assert server.agent is None
        status, result = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 200 and result.get('ok') is True, result
        receipt = ops.read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
        assert receipt.state == 'completed' and receipt.dispatch is None
        assert receipt.command_text == text
        assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == ('feishu', 'user', row['actor_id'])
        assert receipt.conversation_thread_id
        if text == '/model':
            assert len(delivered) == 1 and delivered[0][0] == row['actor_id']
        else:
            assert not delivered
        before = ops.gateway_control_operation_path(server.paths, receipt.operation_id).read_bytes()
        assert post_model(server, body, route=route, actor=row['actor_id']) == (status, result)
        assert ops.gateway_control_operation_path(server.paths, receipt.operation_id).read_bytes() == before
        assert len(delivered) == (1 if text == '/model' else 0)
        assert server.agent is None and host.home_paths.root == agent.home_paths.root


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
@pytest.mark.parametrize('group', [False, True], ids=['user', 'group'])
@pytest.mark.parametrize('text', ['/model', '/agents show', '/memory candidates'])
def test_actual_domain_parity_keeps_owner_and_terminal_replay(tmp_path, monkeypatch, route, ready, group, text):
    with delivery_case(tmp_path, monkeypatch, kind='group' if group else 'user') as (case, delivered):
        agent, _host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=ready, text=text, group=group)
        status, result = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 200 and result.get('ok') is True, result
        receipt = ops.read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
        assert receipt.state == 'completed' and receipt.dispatch is None
        assert receipt.command_text == text
        assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == (
            'feishu', 'group' if group else 'user', row['owner']['owner_id'])
        assert receipt.user_id == row['actor_id']
        before = control_files(server)
        assert post_model(server, body, route=route, actor=row['actor_id']) == (status, result)
        assert control_files(server) == before
        assert len(delivered) == (1 if text == '/model' else 0)
        if delivered:
            assert delivered[0][0] == (row['owner']['owner_id'] if group else row['actor_id'])
        assert server.agent is (agent if ready else None)


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
@pytest.mark.parametrize('template', ['/model {id}', '/model default {id}'])
def test_actual_text_model_selection_and_default_parity(tmp_path, monkeypatch, route, ready, template):
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        profile_id = 'b00f0289-a209-40fe-a3b7-a01cdf972abb'
        text = template.format(id=profile_id)
        body = parity_request(case, ready=ready, text=text)
        status, result = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 200 and result.get('ok') is True, result
        receipt = ops.read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
        assert receipt.state == 'completed' and receipt.dispatch is None and receipt.command_text == text
        thread = host.conversation_store.threads.resolve(
            channel='feishu', channel_conversation_id='parity-session', channel_user_id=row['actor_id'])
        if 'default' in text:
            assert read_model_profiles(model_profiles_path(host.home_paths))['selected'] == profile_id
        else:
            assert thread.model_profile_id == profile_id
            assert thread.model_selection_revision == 2 and thread.model_selection_source == 'explicit'
        before = control_files(server)
        assert post_model(server, body, route=route, actor=row['actor_id']) == (status, result)
        assert control_files(server) == before and not delivered


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('text', ['/model', '/agents show', '/memory candidates'])
@pytest.mark.parametrize('failure', ['credential', 'message_id'])
def test_actual_control_rejection_has_no_domain_or_business_side_effect(tmp_path, monkeypatch, route, text, failure):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=False, text=text)
        if failure == 'message_id':
            body['metadata'].pop('message_id')
        before = control_files(server)
        owner_before = {str(p.relative_to(host.home_paths.owner_home_dir)): p.read_bytes()
                        for p in host.home_paths.owner_home_dir.rglob('*') if p.is_file()}
        status, result = post_model(server, body, route=route, actor=row['actor_id'], credential=failure != 'credential')
        assert status == (403 if failure == 'credential' else 400), result
        if failure == 'message_id':
            assert result['error_code'] == 'CONTROL_OPERATION_ID_REQUIRED'
        assert control_files(server) == before and not delivered and server.agent is None
        assert {str(p.relative_to(host.home_paths.owner_home_dir)): p.read_bytes()
                for p in host.home_paths.owner_home_dir.rglob('*') if p.is_file()} == owner_before


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('text', ['/model vector', '/model vector off'])
def test_text_route_does_not_borrow_client_only_or_vector_operation(tmp_path, monkeypatch, route, text):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=False, text=text)
        body.update(operation='add', profile={'model_name': 'must-not-save'})
        before = control_files(server)
        status, result = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 503 and result['error_code'] == 'COORDINATOR_BUSINESS_NOT_READY', result
        assert control_files(server) == before and not delivered and server.agent is None


@pytest.mark.parametrize('route,field', [('/ask', 'goal'), ('/ask', 'prompt'), ('/control', 'command'), ('/control', 'prompt')])
def test_actual_route_uses_original_text_authority_over_other_body_fields(tmp_path, monkeypatch, route, field):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=False, text='/model')
        body.pop('goal')
        body.pop('prompt')
        body.pop('command')
        body[field] = '/model'
        body['command' if route == '/ask' else 'goal'] = '/delegate enable'
        if field != 'prompt':
            body['prompt'] = '/memory candidates'
        status, result = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 200 and result.get('ok') is True, result
        receipt = ops.read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
        assert receipt.command_text == '/model' and receipt.state == 'completed' and receipt.dispatch is None
        assert len(delivered) == 1 and delivered[0][0] == row['actor_id']
        assert body[field] == '/model' and server.agent is None


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('text', ['/model', '/agents show', '/memory candidates'])
def test_cross_entry_replay_and_conflict_keep_authenticated_owner(tmp_path, monkeypatch, route, text):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, row, _body, server, _runtime, _sent = case
        body = parity_request(case, ready=False, text=text)
        body.update(user_id='ou_foreign', channel='local',
                    owner={'provider': 'local', 'owner_kind': 'main', 'owner_id': 'main'})
        status, result = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 200 and result['ok'], result
        receipt = ops.read_gateway_control_operation(server.paths, result['operation_id'], read_only=True)
        assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id, receipt.user_id) == (
            'feishu', 'user', row['actor_id'], row['actor_id'])
        before = control_files(server)
        other = '/ask' if route == '/control' else '/control'
        assert post_model(server, body, route=other, actor=row['actor_id']) == (status, result)
        changed = {**body, 'command': '/status', 'goal': '/status', 'prompt': '/status'}
        code, refusal = post_model(server, changed, route=other, actor=row['actor_id'])
        assert code == 409 and refusal['error_code'] == 'IDEMPOTENCY_CONFLICT'
        assert control_files(server) == before and len(delivered) == (1 if text == '/model' else 0)
        assert server.agent is None


@pytest.mark.parametrize('route', ['/control', '/ask'])
@pytest.mark.parametrize('ready', [False, True], ids=['cold', 'ready'])
@pytest.mark.parametrize('group', [False, True], ids=['user', 'group'])
def test_agents_write_uses_original_preview_confirmation_and_owner(tmp_path, monkeypatch, route, ready, group):
    with delivery_case(tmp_path, monkeypatch, kind='group' if group else 'user') as (case, delivered):
        _agent, host, _path, row, _body, server, _runtime, _sent = case
        text = '明确确认后写入的原用户规则\n保留尾空白  '
        body = parity_request(case, ready=ready, text='/agents write ' + json.dumps(text, ensure_ascii=False), group=group)
        path = host.home_paths.owner_home_dir / 'AGENTS.md'
        before = path.read_bytes()
        status, preview = post_model(server, body, route=route, actor=row['actor_id'])
        assert status == 200 and preview['ok'] and preview['details']['state'] == 'preview', preview
        assert preview['details']['value'] == text and path.read_bytes() == before
        confirmation = preview['details']['confirm_command']
        confirm_body = parity_request(case, ready=ready, text=confirmation, group=group, message='parity-confirm')
        status, done = post_model(server, confirm_body, route=route, actor=row['actor_id'])
        assert status == 200 and done['ok'] and done['details']['state'] == 'completed', done
        assert path.read_text() == text
        receipt = ops.read_gateway_control_operation(server.paths, done['operation_id'], read_only=True)
        assert receipt.state == 'completed' and receipt.dispatch is None
        assert (receipt.owner_provider, receipt.owner_kind, receipt.owner_id) == (
            'feishu', 'group' if group else 'user', row['owner']['owner_id'])
        files = control_files(server)
        assert post_model(server, confirm_body, route=route, actor=row['actor_id']) == (status, done)
        assert control_files(server) == files and path.read_text() == text and not delivered


@pytest.mark.parametrize('cold', ['feishu'], indirect=True)
@pytest.mark.parametrize('route', ['/control', '/ask'])
def test_memory_candidate_uses_original_preview_and_cancel_without_promotion(cold, route):  # noqa: F811
    from agent_py_agent.tests.test_memory_preview_transaction import seed

    row = seed(cold)
    # LLM: 原HTTP/G1/C来源和领域消费不替换，正文仅用户明确命令与稳定消息身份。
    # 函数用途: 向同一临时C提交预览/取消并读取原回执，不为测试补写结果。
    def send(text, message):
        body = {'command': text, 'goal': text, 'conversation_id': 'control-session',
                'metadata': {'message_id': message, 'channel_chat_type': 'p2p'}}
        return post_model(cold.server, body, route=route, actor='alice')
    status, preview = send('/memory candidates preview ' + row.candidate_id, 'parity-memory-preview')
    assert status == 200 and preview['ok'] and preview['details']['state'] == 'preview', preview
    assert cold.seeded.memory_candidates.get(row.candidate_id).status == 'pending_review'
    status, done = send(preview['details']['cancel_command'], 'parity-memory-cancel')
    assert status == 200 and done['ok'] and done['details']['state'] == 'cancelled', done
    receipt = ops.read_gateway_control_operation(cold.server.paths, done['operation_id'], read_only=True)
    assert receipt.state == 'completed' and receipt.dispatch is None
    assert cold.seeded.memory_candidates.get(row.candidate_id).status == 'pending_review'
    before = control_files(cold.server)
    assert send(preview['details']['cancel_command'], 'parity-memory-cancel') == (status, done)
    assert control_files(cold.server) == before and cold.server.agent is None and cold.constructions == []
