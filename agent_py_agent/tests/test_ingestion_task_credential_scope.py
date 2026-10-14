"""来源凭据正式 private-card、原任务/监控与冷重放边界；只用合成 owner 和本机 HTTP。"""
from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.common.json_io import read_json_object_report
from agent_py_agent.agent.conversation.host_actor import actor_from_scope, background_host_actor
from agent_py_agent.agent.delivery import ChannelCapabilities
from agent_py_agent.agent.gateway_parts.private_interaction_service import (
    resolve_private_interaction,
)
from agent_py_agent.agent.ingestion.source_http import render_source_http_request
from agent_py_agent.agent.ingestion.watch_tool import WatchStreamTool
from agent_py_agent.agent.scheduler.service import SchedulerService
from agent_py_agent.agent.scheduler.tool import ScheduleTool
from agent_py_agent.agent.user_space.task_credentials import (
    credential_path,
    request_credential_card,
)
from agent_py_agent.tests._tool_runtime_harness import execute_registry_test_call
from agent_py_agent.tests.test_ingestion_secret_scope import _agent, sink

_FIXTURES = (sink,)
_SECRET = 'SYNTHETIC_PRIVATE_SOURCE_TOKEN_91e2'


# LLM: 本人可信入口经原 actor helper 与 TaskLink 写入，claim 来自原 Store；不制造 raw attrs 身份或跳过凭据检查。
# 函数用途: 给临时 owner 建立真实活动主任务，供私人卡和正式来源工具使用。
def _active(agent, *, actor_id=None, root_id=None):
    actor_id = actor_id or ('ou_synthetic' if agent.home_paths.owner_kind == 'user' else 'ou_actor_a')
    conversation = agent.config.my_agent_owner_id if agent.home_paths.owner_kind == 'group' else 'oc_private_synthetic'
    root_id = root_id or 'gwreq-' + uuid.uuid4().hex
    store = agent.conversation_store
    thread = store.threads.get_or_create({'channel': 'feishu', 'channel_conversation_id': conversation,
        'channel_user_id': actor_id, 'canonical_user_id': actor_id, 'owner_id': agent.home_paths.owner_id,
        'owner_home': str(agent.home_paths.owner_home_dir)})
    actor = actor_from_scope(agent, SimpleNamespace(channel='feishu', channel_user_id=actor_id,
        channel_conversation_id=conversation), thread.thread_id, root_id)
    store.tasks.bind({'thread_id': thread.thread_id, 'task_id': root_id, 'goal': '合成原任务', 'status': 'active'},
        host_channel_actor=actor)
    claim = store.claims.acquire({'thread_id': thread.thread_id, 'task_id': root_id, 'reason': 'thread_goal_continue'})
    agent._current_run_params = RunParams(request_id=root_id, task_id=root_id, run_id=root_id,
        task_attributes={'conversation_thread_id': thread.thread_id}, host_channel_actor=actor)
    return thread, actor, claim


# LLM: 模拟的只有卡片运输，申请/签名/提交/加密/作用域均走原产品入口，合成 secret 永不写普通文本。
# 函数用途: 为当前本人任务或监控输入一次私人凭据，返回不可直接解密的引用。
def _credential(agent, url, *, job_id=''):
    from agent_py_agent.agent.user_space.admin_channel_identity import bind_admin_channel_identity

    agent.config.feishu_session_lock_enabled = False
    if agent.home_paths.owner_kind == 'main':
        bind_admin_channel_identity(agent.home_paths.root, 'feishu', agent._current_run_params.host_channel_actor.actor_id)
    agent.channel_registry.register_adapter('feishu', SimpleNamespace(send_control_card=lambda *_args, **_kwargs:
        {'message_id': 'om_' + uuid.uuid4().hex, 'chat_id': 'oc_private_card'}),
        capabilities=ChannelCapabilities(control_cards=True, proactive=True))
    reference, _ = request_credential_card(agent, url=url, authentication='bearer', job_id=job_id)
    path = credential_path(agent.home_paths.owner_home_dir, reference)
    row = read_json_object_report(path, context='synthetic.private-card').payload
    body = {'reference': row['reference'], 'decision': 'submit', 'message_id': row['message_id'],
        'chat_id': row['chat_id'], 'fields': {'secret': _SECRET}}
    assert resolve_private_interaction(agent, body, channel='feishu', actor_id=row['actor_id'])['ok']
    assert _SECRET not in path.read_text()
    return reference


# LLM: 原 Registry/Executor 建立正式 call/attempt，run_id 与当前执行相等；允许本机地址仅是合成宿主网络事实。
# 函数用途: 通过唯一网络工具调用来源，成功后回收临时 watch，不替换授权或 HTTP handler。
def _invoke(agent, url, request, *, network='enabled', call_run_id=None, extra_boundary=None):
    current = agent._current_run_params
    run_id = call_run_id or (str(current.run_id or current.task_id) if current else 'unknown-root')
    task_id = str(current.task_id or '') if current else ''
    boundary = {'allowed_private_hosts': ['127.0.0.1'], 'allow_private_resolution': True,
        'network_access_mode': network, 'task_id': task_id, **(extra_boundary or {})}
    result = execute_registry_test_call(agent.tools, 'watch_stream', {'action': 'open', 'url': url, 'mode': 'poll',
        'source_id': uuid.uuid4().hex, 'poll_query_seconds': 120, 'http_request': request},
        write_boundary=boundary, register_with=agent, run_id=run_id, call_id=uuid.uuid4().hex,
        attempt_id=getattr(current, 'attempt_id', '') or 'test-attempt')
    if result.ok:
        execute_registry_test_call(agent.tools, 'watch_stream', {'action': 'close', 'url': url},
            write_boundary=boundary, register_with=agent, run_id=run_id, call_id=uuid.uuid4().hex,
            attempt_id=getattr(current, 'attempt_id', '') or 'test-attempt')
    return result


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
@pytest.mark.parametrize('full', [False, True])
def test_private_task_credential_works_without_model_or_source_receiving_secret(tmp_path, sink, kind, full):
    agent = _agent(tmp_path, kind, full)
    _active(agent)
    url = f'http://127.0.0.1:{sink.server_port}/private'
    reference = _credential(agent, url)
    result = _invoke(agent, url, {'credential_ref': reference})
    assert result.ok, (result.error_code, result.output)
    assert sink.received and all(headers['Authorization'] == 'Bearer ' + _SECRET for _, headers in sink.received)
    assert _SECRET not in result.output


@pytest.mark.parametrize('change', ['other_root', 'other_actor', 'other_origin', 'no_context', 'call_mismatch'])
def test_private_reference_is_not_bearer_for_other_scope(tmp_path, sink, change):
    agent = _agent(tmp_path, 'group')
    _thread, _actor, claim = _active(agent)
    current = agent._current_run_params
    url = f'http://127.0.0.1:{sink.server_port}/scope'
    reference = _credential(agent, url)
    call_id = None
    if change in {'other_root', 'other_actor'}:
        agent.conversation_store.claims.finish({'thread_id': claim['thread_id'], 'task_id': current.task_id,
            'claim_id': claim['claim_id'], 'status': 'completed'})
        _active(agent, actor_id='ou_actor_b' if change == 'other_actor' else 'ou_actor_a')
    elif change == 'other_origin':
        url = f'http://127.0.0.1:{sink.server_port + 1}/scope'
    elif change == 'no_context':
        agent._current_run_params = None
    else:
        call_id = 'gwreq-different-formal-call'
    result = _invoke(agent, url, {'credential_ref': reference}, call_run_id=call_id)
    assert not result.ok and not sink.received
    assert _SECRET not in result.output


@pytest.mark.parametrize('kind', ['user', 'group', 'main'])
def test_parent_network_disabled_blocks_even_private_credential_or_file(tmp_path, sink, kind):
    agent = _agent(tmp_path, kind)
    _active(agent)
    url = f'http://127.0.0.1:{sink.server_port}/disabled'
    reference = _credential(agent, url)
    path = agent.home_paths.owner_workspace_dir / 'private-source-token.txt'
    path.write_text(_SECRET)
    for request in ({}, {'credential_ref': reference}, {'secret_bindings': [
            {'location': 'header', 'name': 'Authorization', 'secret_ref': 'file:' + str(path)}]}):
        result = _invoke(agent, url, request, network='disabled')
        # 正式网络上界在原 Executor 更早拒绝，不要求请求进入来源 handler 才报错。
        assert not result.ok and result.error_code == 'NETWORK_ACCESS_DISABLED'
        assert not sink.received


def test_detached_source_and_pure_adapter_cannot_borrow_latest_active_credential(tmp_path, sink):
    from agent_py_agent.agent.ingestion.source_http import render_source_adapter_request

    agent = _agent(tmp_path)
    _active(agent)
    url = f'http://127.0.0.1:{sink.server_port}/cold'
    reference = _credential(agent, url)
    request_facts = {'method': 'GET', 'credential_ref': reference}
    tool = WatchStreamTool(agent=agent)
    for request in (render_source_http_request(url, request_facts, cursor=None, page_size=None),
            render_source_adapter_request(url, request_facts, {'url': url})):
        assert not request.headers.get('Authorization')
        ok, output, code = tool._fetch_json_pinned(request, ('127.0.0.1',), True)
        assert not ok and code == 'SOURCE_SECRET_UNAVAILABLE'
        assert _SECRET not in str(output) and not sink.received


# LLM: reserve/enqueue/claim 均使用原 scheduler 和不可变来源；私人凭据仍在同 actor 的旧根申请，只允许准确监控后续 run。
# 函数用途: 准备原监控的真实执行关联和会话 claim，不从当前群成员补造身份。
def _scheduled_execution(agent):
    tool = ScheduleTool(agent)
    result = tool.execute({'action': 'create', 'name': '合成凭据监控', 'prompt': '检查合成 API',
        'schedule_kind': 'every', 'every_seconds': 600, '__tool_call_id': uuid.uuid4().hex})
    assert result.ok, result.output
    job_id = json.loads(result.output)['job']['job_id']
    return job_id


@pytest.mark.parametrize('other_monitor', [False, True])
def test_current_exact_monitor_run_reuses_original_private_credential(tmp_path, sink, other_monitor):
    agent = _agent(tmp_path, 'group')
    _active(agent)
    job_id = _scheduled_execution(agent)
    url = f'http://127.0.0.1:{sink.server_port}/monitor'
    reference = _credential(agent, url, job_id=job_id)
    if other_monitor:
        job_id = _scheduled_execution(agent)
    run = agent.scheduler_repository.reserve_manual_run(job_id)
    service = SchedulerService(agent.scheduler_repository, conversation_store=agent.conversation_store)
    service.enqueue_ready_runs(now=run['dispatch_after'] + 1)
    signal = agent.conversation_store.wakes.pending()[0]
    claimed = service.claim_wake(signal, now=run['dispatch_after'] + 2)
    assert claimed.status == 'claimed'
    agent.conversation_store.claims.acquire({'thread_id': signal.thread_id, 'task_id': run['run_id'],
        'reason': 'scheduled_job_due'})
    agent._current_run_params = RunParams(run_id=run['run_id'], task_id=run['run_id'],
        task_attributes={'conversation_thread_id': signal.thread_id},
        host_channel_actor=background_host_actor(agent, signal.thread_id, run['run_id']))
    result = _invoke(agent, url, {'credential_ref': reference})
    if other_monitor:
        assert not result.ok and not sink.received
    else:
        assert result.ok, (result.error_code, result.output)
        assert sink.received and all(headers['Authorization'] == 'Bearer ' + _SECRET for _, headers in sink.received)
    assert _SECRET not in result.output


@pytest.mark.parametrize('depth', [1, 2])
def test_child_and_grandchild_use_original_credential_without_host_or_read_scope_expansion(tmp_path, sink, depth):
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_run_params
    from agent_py_agent.agent.conversation.host_actor import task_host_actor
    from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
        HierarchyChildSpec,
        HierarchyScheduleRequest,
    )

    agent = _agent(tmp_path, 'group', full=True)
    _active(agent)
    url = f'http://127.0.0.1:{sink.server_port}/child'
    reference = _credential(agent, url)
    params = create_run_params(agent, {'goal': '合成来源子任务'}, '合成来源子任务', ['watch_stream'])
    child = agent.subagents.create_run(params=params)
    if depth == 2:
        scheduled = agent.subagents.hierarchy.schedule_child_runs(params=HierarchyScheduleRequest(
            parent_run_id=child.id, child_specs=[HierarchyChildSpec(goal='合成来源孙任务', role='worker')],
            apply=True, requested_by=child.id))
        child = agent.subagents.load(scheduled.created_run_ids[0])
    child = agent.subagents.lifecycle.prepare_runner_attempt(child.id)
    agent._current_run_params = RunParams(task_id=child.root_id, run_id=child.id,
        attempt_id=child.runner_active_attempt_id, task_attributes=child.attributes,
        host_channel_actor=task_host_actor(agent, child))
    result = _invoke(agent, url, {'credential_ref': reference})
    assert result.ok, (result.error_code, result.output)
    assert sink.received and all(headers['Authorization'] == 'Bearer ' + _SECRET for _, headers in sink.received)
    sink.received.clear()
    path = agent.home_paths.owner_workspace_dir / 'child-forbidden-file.txt'
    path.write_text(_SECRET)
    allowed = agent.home_paths.owner_workspace_dir / 'only-this-child-tree'
    allowed.mkdir()
    request = {'secret_bindings': [{'location': 'header', 'name': 'Authorization', 'secret_ref': 'file:' + str(path)}]}
    result = _invoke(agent, url + '-exact', request, extra_boundary={'read_scope_mode': 'exact',
        'allowed_read_roots': [str(allowed)]})
    assert not result.ok and not sink.received
    assert _SECRET not in result.output


@pytest.mark.parametrize('private', [False, True])
def test_captured_harvester_keeps_public_lifetime_but_does_not_refresh_private_actor(tmp_path, sink, monkeypatch, private):
    agent = _agent(tmp_path, 'group')
    thread, _actor, claim = _active(agent)
    original_current = agent._current_run_params
    url = f'http://127.0.0.1:{sink.server_port}/captured'
    reference = _credential(agent, url) if private else ''
    facts = {'method': 'GET', 'credential_ref': reference} if private else {'method': 'GET'}
    original = WatchStreamTool._harvester_fetch
    captured = []

    def capture(tool):
        callback = original(tool)
        captured.append(callback)
        return callback

    monkeypatch.setattr(WatchStreamTool, '_harvester_fetch', capture)
    result = _invoke(agent, url, facts)
    assert result.ok and captured
    sink.received.clear()
    agent.conversation_store.claims.finish({'thread_id': thread.thread_id, 'task_id': original_current.task_id,
        'claim_id': claim['claim_id'], 'status': 'completed'})
    _active(agent, actor_id='ou_actor_b')
    request = render_source_http_request(url, facts, cursor=None, page_size=None)
    for callback in captured:
        ok, output, code = callback(request)
        if private:
            assert not ok and code == 'SOURCE_SECRET_UNAVAILABLE'
            assert not sink.received
        else:
            assert ok and not code and sink.received
        assert _SECRET not in str(output)
    sink.received.clear()
    path = agent.home_paths.owner_permissions_json
    payload = json.loads(path.read_text())
    payload['network']['enabled'] = False
    path.write_text(json.dumps(payload))
    for callback in captured:
        ok, _output, code = callback(request)
        assert not ok and code == 'NETWORK_DISABLED' and not sink.received


@pytest.mark.parametrize('kind', ['env', 'foreign_file'])
def test_replayed_source_refs_remain_unresolved_and_are_refused_without_authority(tmp_path, sink, monkeypatch, kind):
    from agent_py_agent.agent.ingestion.source_http import render_source_adapter_request

    agent = _agent(tmp_path, 'group', full=True)
    _active(agent)
    marker = 'SYNTHETIC_COLD_HOST_TOKEN_91e2'
    if kind == 'env':
        monkeypatch.setenv('SYNTHETIC_COLD_HOST_KEY_91e2', marker)
        ref = 'env:SYNTHETIC_COLD_HOST_KEY_91e2'
    else:
        path = tmp_path / 'cold-host-token.txt'
        path.write_text(marker)
        ref = 'file:' + str(path)
    url = f'http://127.0.0.1:{sink.server_port}/old-source'
    facts = {'method': 'GET', 'secret_bindings': [{'location': 'header', 'name': 'Authorization', 'secret_ref': ref}]}
    for request in (render_source_http_request(url, facts, cursor=None, page_size=None),
            render_source_adapter_request(url, facts, {'url': url})):
        assert marker not in repr(request) and 'Authorization' not in request.headers
        ok, output, code = WatchStreamTool(agent)._fetch_json_pinned(request, ('127.0.0.1',), True)
        assert not ok and code == 'SOURCE_SECRET_UNAVAILABLE'
        assert marker not in str(output) and not sink.received


def test_one_unauthorized_ref_prevents_all_http_and_symlink_owner_escape(tmp_path, sink, monkeypatch):
    agent = _agent(tmp_path, full=True)
    _active(agent)
    own = agent.home_paths.owner_workspace_dir / 'legal-token.txt'
    own.write_text(_SECRET)
    outside = tmp_path / 'other-owner-token.txt'
    outside.write_text('SYNTHETIC_FOREIGN_TOKEN_91e2')
    alias = own.parent / 'foreign-token-alias.txt'
    alias.symlink_to(outside)
    monkeypatch.setenv('SYNTHETIC_REF_SECOND', 'SYNTHETIC_HOST_SECOND_91e2')
    url = f'http://127.0.0.1:{sink.server_port}/all-or-none'
    for invalid in ('env:SYNTHETIC_REF_SECOND', 'file:' + str(alias)):
        result = _invoke(agent, url, {'secret_bindings': [
            {'location': 'header', 'name': 'X-First', 'secret_ref': 'file:' + str(own)},
            {'location': 'header', 'name': 'X-Second', 'secret_ref': invalid}]})
        assert not result.ok and not sink.received
        assert _SECRET not in result.output and 'SYNTHETIC_' not in result.output


def test_private_source_redirect_cannot_send_header_to_another_origin(tmp_path, sink, monkeypatch):
    agent = _agent(tmp_path, 'group')
    _active(agent)
    url = f'http://127.0.0.1:{sink.server_port}/redirect'
    reference = _credential(agent, url)

    def redirect(handler):
        sink.received.append((handler.path, dict(handler.headers)))
        handler.send_response(302)
        handler.send_header('Location', f'http://127.0.0.1:{sink.server_port + 1}/foreign')
        handler.send_header('Content-Length', '0')
        handler.end_headers()

    monkeypatch.setattr(sink.RequestHandlerClass, 'do_GET', redirect)
    result = _invoke(agent, url, {'credential_ref': reference})
    # handler已进入原mutating watch事务，原账保留unknown禁止盲重做；真实拒绝原因仍准确保留。
    assert not result.ok and result.reported_error_code == 'SOURCE_SECRET_UNAVAILABLE'
    assert result.error_code == 'TOOL_OPERATION_OUTCOME_UNKNOWN'
    assert result.metadata['handler_details']['tool_operation']['status'] == 'unknown'
    assert len(sink.received) == 1 and sink.received[0][1]['Authorization'] == 'Bearer ' + _SECRET
    assert _SECRET not in result.output


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('location', ['header', 'query', 'json_body'])
def test_authorized_file_injection_echo_is_scrubbed_in_every_request_location(tmp_path, sink, monkeypatch, failed, location):
    from urllib.parse import parse_qs, quote_plus, urlsplit

    agent = _agent(tmp_path)
    _active(agent)
    # HTTP头沿原latin-1传输合同，Unicode编码回显使用JSON/query两条正式可编码路径。
    secret = 'SYNTHETIC_SECRET_quote"_91e2' if location == 'header' else 'SYNTHETIC_SECRET_带 引号"_91e2'
    path = agent.home_paths.owner_workspace_dir / 'scoped-echo-token.txt'
    path.write_text(secret)
    received = []
    observed = []
    original = WatchStreamTool._fetch_json_pinned

    # LLM: observer只记录原HTTP choke的实际返回，不改认证、请求、响应或净化结果；open回执本身不含原body不能作为保密证明。
    # 函数用途: 检查JSON/spool之前的真正正文和HTTP错误，防止仅断言最终简短回执而漏掉可还原编码。
    def observe(tool, *args, **kwargs):
        result = original(tool, *args, **kwargs)
        observed.append(json.dumps(result[1], ensure_ascii=False))
        return result

    def echo(handler):
        sink.received.append((handler.path, dict(handler.headers)))
        body = json.loads(handler.rfile.read(int(handler.headers.get('Content-Length', '0'))) or b'{}')
        value = (handler.headers.get('X-Credential', '') if location == 'header' else
            parse_qs(urlsplit(handler.path).query).get('token', [''])[0] if location == 'query' else body['auth']['token'])
        received.append(value)
        raw = json.dumps({'echo': value, 'raw_request_path': handler.path}).encode()
        handler.send_response(401 if failed else 200)
        handler.send_header('Content-Type', 'application/json')
        handler.send_header('Content-Length', str(len(raw)))
        handler.send_header('X-Secret-Echo', value.encode('unicode_escape').decode())
        handler.end_headers()
        handler.wfile.write(raw)

    monkeypatch.setattr(sink.RequestHandlerClass, 'do_POST', echo, raising=False)
    monkeypatch.setattr(WatchStreamTool, '_fetch_json_pinned', observe)
    binding = {'location': location, 'secret_ref': 'file:' + str(path)}
    if location == 'json_body':
        binding['path'] = ['auth', 'token']
    else:
        binding['name'] = 'X-Credential' if location == 'header' else 'token'
    result = _invoke(agent, f'http://127.0.0.1:{sink.server_port}/echo-location',
        {'method': 'POST', 'json_body': {'auth': {}}, 'secret_bindings': [binding]})
    assert result.ok is not failed, (result.error_code, result.output)
    assert received and all(value == secret for value in received)
    assert secret not in result.output and json.dumps(secret)[1:-1] not in result.output
    assert observed and all(secret not in row and json.dumps(secret)[1:-1] not in row and
        quote_plus(secret, safe='') not in row for row in observed)
