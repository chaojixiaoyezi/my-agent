# LLM: 原runtime终态helper故障保同outer/native链与Future；没有原核收就不得释放责任。
# 模块用途: 验证运行时外层错误不被普通失败收口，测试事实默认落自己的临时目录。
import threading

import pytest

from agent_py_agent.tests.test_approval_retirement_lifecycle import (
    _foreground,
    approval,
    install_original_approval_loop,
)


@pytest.mark.parametrize('lock_kind', ['permission', 'json'])
def test_runtime_outer_failure_retains_same_native_cleanup_debt(tmp_path, monkeypatch, lock_kind):
    import json
    import time
    import urllib.request

    from agent_py_agent.agent.gateway_parts.client_credentials import GatewayClientCredentials
    from agent_py_agent.agent.gateway_parts.http_service import (
        GatewayHTTPServer,
        GatewayHTTPServerParams,
    )
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
    from agent_py_agent.cli.gateway_loops import _RequestDispatcher
    from agent_py_agent.cli.models import GatewayRunContext

    world = _foreground(tmp_path, kind='user')
    agent, repo = world.agent, world.agent.subagents.runtime_db
    old = repo.get_attempt(world.params.attempt_id)
    repo.settle_agent_run(agent_run_id=old['agent_run_id'], attempt_id=world.params.attempt_id,
                          expected_attempt_status='running', status='cancelled')
    world.store.claims.finish({'thread_id': world.thread.thread_id, 'claim_id': world.claim['claim_id'], 'status': 'finished'})
    del agent._current_run_params
    ensure_gateway_folders(world.paths)
    seen = {'retain_lock': True, 'followup_started': threading.Event(), 'followup_release': threading.Event()}
    provider_messages = []
    generate = agent.backend.generate

    def observe_provider(*args, **kwargs):
        provider_messages.append(kwargs.get('messages'))
        return generate(*args, **kwargs)

    monkeypatch.setattr(agent.backend, 'generate', observe_provider)
    tool = install_original_approval_loop(world, monkeypatch, lock_kind, seen)
    from agent_py_agent.agent.agent_core import runtime_mixin
    outer_error = OSError('synthetic original runtime terminal writer unavailable')
    def fail_original_runtime_terminal(*args, **kwargs):
        raise outer_error
    monkeypatch.setattr(runtime_mixin, 'update_runtime_fact_terminal_if_enabled', fail_original_runtime_terminal)
    server = GatewayHTTPServer(0, world.paths, params=GatewayHTTPServerParams(agent=agent))
    dispatcher = _RequestDispatcher(GatewayRunContext(agent, world.paths, tmp_path / 'synthetic.yaml'), world.paths)
    server.start()

    def ask(message):
        headers = {'Content-Type': 'application/json', **GatewayClientCredentials(agent.home_paths.root).headers(),
                   'X-User-Id': 'ou_synthetic', 'X-Channel': 'feishu',
                   'X-Conversation-Id': 'oc_private_synthetic'}
        body = {'goal': '合成普通需求', 'user_id': 'ou_synthetic', 'channel': 'feishu',
                'conversation_id': 'oc_private_synthetic', 'metadata': {'message_id': message}}
        request = urllib.request.Request('http://127.0.0.1:%s/ask' % server.server.server_address[1],
            data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 202
            return json.loads(response.read())

    def done_future():
        deadline = time.monotonic() + 5
        while True:
            with dispatcher._future_gate:
                pending = list(dispatcher._request_futures)
            if pending and pending[-1].done():
                return pending[-1]
            assert time.monotonic() < deadline
            time.sleep(.01)

    try:
        submitted = ask('first-retirement')
        seen['first_request'] = submitted['request_id']
        assert dispatcher.tick() == 1
        future = done_future()
        assert 'error' in seen, {key: seen.get(key) for key in ('publish_error', 'resolver_return')}
        observed_error = future.exception()
        native = seen['error']
        canonical = read_json_file(world.paths.terminal / (submitted['request_id'] + '.json'))
        held_before = future in dispatcher._request_futures
        dispatcher._collect_request_futures()
        fact = {
            'outer_same_object': observed_error is outer_error,
            'outer_has_original_native_context': approval.find_approval_retirement_error(outer_error) is native,
            'runtime_attempt_status': repo.get_attempt(seen['params'].attempt_id)['status'],
            'runtime_exec_lock_present': repo.get_agent_run_exec_lock(repo.get_attempt(seen['params'].attempt_id)['agent_run_id']) is not None,
            'native_in_exception_chain': approval.find_approval_retirement_error(observed_error) is native,
            'pending_slot_same_native': native._retirement_origin.observation.pending_error is native,
            'native_terminal_accepted': native._retirement_origin.observation.terminal_acceptance is not None,
            'future_retained_before_collect': held_before,
            'future_retained_after_collect': future in dispatcher._request_futures,
            'permission_still_exists': seen['handle'].path.exists(),
            'callback_closed': native._retirement_origin.callback_closed,
            'handler_calls': tool.calls,
            'canonical_schema': canonical.get('schema_version'),
            'canonical_status': canonical.get('status'),
            'claim_status': world.store.claims.load_report(seen['thread_id'])[0].get('status'),
        }
        from pathlib import Path
        Path(__import__('os').environ.get('REVIEW39_FACTS', str(tmp_path))).joinpath('runtime_outer_' + lock_kind + '.json').write_text(json.dumps(fact, indent=2))
        assert observed_error is outer_error and tool.calls == 0
        assert approval.find_approval_retirement_error(observed_error) is native, fact
        assert future in dispatcher._request_futures, fact
        return
    finally:
        seen['followup_release'].set()
        if 'held_lock' in seen:
            seen.pop('held_lock').__exit__(None, None, None)
        dispatcher._executor.shutdown(wait=True)
        server.stop()
