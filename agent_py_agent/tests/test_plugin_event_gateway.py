"""B4 Gateway 真实入口观察；隔离 Agent 和队列，不连接生产 Gateway。"""
from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import http_handlers, http_service, plugin_panels_http
from agent_py_agent.agent.gateway_parts.control_operation_service import (
    execute_gateway_control_operation,
)
from agent_py_agent.agent.gateway_parts.control_service import GatewayControlScope
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_execution import _handle_gateway_request
from agent_py_agent.agent.settings import AgentConfig


class _Handler:
    headers = {}
    client_address = ('127.0.0.1', 1)

    def __init__(self, body):
        self.body = body
        self.replies = []

    def _read_json(self):
        return self.body

    def _send_json(self, status, body):
        self.replies.append((status, body))


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    cfg = AgentConfig(model_backend='echo', gateway_workspace='gateway')
    cfg.plugin_events_enabled = True
    cfg.my_agent_home = str(tmp_path / 'home')
    agent = SimpleAgent(cfg, tmp_path)
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    server = SimpleNamespace(agent=agent, paths=paths)
    events = []
    monkeypatch.setattr(http_service, '_server_instance', server)
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', lambda _s, owner, event: events.append((owner, event)))
    return agent, paths, server, events


@pytest.mark.parametrize('idempotent', [False, True])
def test_prompt_only_after_queue_commit_and_no_replay(gateway, idempotent):
    _agent, paths, server, events = gateway
    body = {'prompt': 'prompt-private-marker', 'conversation_id': 'thread-marker'}
    if idempotent:
        body['metadata'] = {'message_id': 'msg-1'}
    handler = _Handler(body)
    http_handlers.handle_ask(handler, server, lambda: 'ask-1')
    assert handler.replies[-1][0] == 202
    request_id = handler.replies[-1][1]['request_id']
    assert (paths.inbox / f'{request_id}.json').exists()
    assert len(events) == 1
    owner, event = events[0]
    assert owner.home_dir == server.agent.home_paths.owner_home_dir
    assert event.type == 'prompt_submitted'
    assert event.facts == {'request_id': request_id, 'chars': 21, 'has_attachments': False}
    assert 'prompt-private-marker' not in json.dumps(event.facts)
    if idempotent:
        http_handlers.handle_ask(handler, server, lambda: 'wrong-replay')
        assert len(events) == 1


def test_turn_real_gateway_path_has_exact_fields(gateway, monkeypatch):
    from agent_py_agent.agent.agent_core.models import AgentRunResult

    agent, paths, _server, events = gateway
    monkeypatch.setattr(agent, 'run', lambda *_args, **_kw: AgentRunResult(
        prompt='', response='hello', backend='echo', used_memories=0, tool_rounds=0))
    request = {'id': 'turn-1', 'kind': 'ask', 'prompt': 'hello', 'status': 'processing',
               'turn_phase': 'open', 'execution_attempt_id': 'turn-test-attempt',
               'conversation': {'channel': 'chat', 'channel_conversation_id': 'c-1',
                                'channel_user_id': 'local-agent', 'canonical_user_id': 'local-agent'}}
    path = paths.processing / 'turn-1.json'
    path.write_text(json.dumps(request), encoding='utf-8')
    response = _handle_gateway_request(agent, path)
    assert response['ok'], json.dumps(response, ensure_ascii=False)
    assert [event.type for _, event in events] == ['turn_started', 'turn_ended']
    assert set(events[0][1].facts) == {'request_id', 'model_name'}
    ended = events[-1][1]
    assert ended.facts['status'] == 'done'
    assert set(ended.facts) == {'request_id', 'status', 'duration_ms', 'tool_calls', 'error_code'}
    assert ended.facts['duration_ms'] >= 0 and ended.facts['tool_calls'] == 0
    assert all(not event.content for _, event in events)


def test_control_real_receipt_emits_only_first_completion(gateway):
    agent, paths, _server, events = gateway
    command = parse_conversation_control('/verbose on', reject_unknown_slash=True)
    scope = GatewayControlScope(user_id='admin', channel='chat', conversation_id='c-1',
                                metadata={'message_id': 'control-1'})
    receipt = execute_gateway_control_operation(agent, paths, command, scope, command_text='/verbose on')
    assert receipt.state == 'completed'
    assert len(events) == 1
    event = events[0][1]
    assert event.type == 'command_executed'
    assert event.facts == {'command': '/verbose', 'operation_id': receipt.operation_id, 'state': 'ok'}
    execute_gateway_control_operation(agent, paths, command, scope, command_text='/verbose on')
    assert len(events) == 1


@pytest.mark.parametrize('fault', ['disabled', 'publish_error', 'queue_error'])
def test_prompt_fault_or_disable_never_breaks_queue(gateway, monkeypatch, fault):
    import stat

    agent, paths, server, events = gateway
    if fault == 'disabled':
        agent.config.plugin_events_enabled = False
        monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', lambda *_: pytest.fail('关闭不发布'))
    if fault == 'publish_error':
        def fail(*_):
            raise RuntimeError('plugin observation unavailable')
        monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', fail)
    if fault == 'queue_error':
        # 旧 /ask 现在经 http_handlers 的私有原子写落盘（不再走 Path.write_text）；按模块属性打桩才能命中真调用点。
        def fail_write(*_args, **_kwargs):
            raise OSError('queue unavailable')
        monkeypatch.setattr(http_handlers, 'write_private_json_file_atomic_no_newline', fail_write)
    handler = _Handler({'prompt': 'normal', 'conversation_id': 'c-1'})
    http_handlers.handle_ask(handler, server, lambda: 'fault-1')
    assert handler.replies[-1][0] == (500 if fault == 'queue_error' else 202)
    assert not events
    if fault != 'queue_error':
        # 不注入故障时，这条用例同时是旧 /ask 私有写的端到端检查：inbox 队列文件必须 0600。
        queued = list(paths.inbox.glob('*.json'))
        assert len(queued) == 1
        assert stat.S_IMODE(queued[0].stat().st_mode) == 0o600


@pytest.mark.parametrize('failure', [False, True])
def test_turn_exception_and_publish_exception_keep_business_result(gateway, monkeypatch, failure):
    from agent_py_agent.agent.gateway_parts import request_execution

    agent, paths, _server, events = gateway
    def body(context, _writer):
        if failure:
            raise ValueError('private error marker')
        context['response'].update(ok=True, status='done')
    monkeypatch.setattr(request_execution, '_execute_gateway_request_body', body)
    request = {'id': 'outcome-1', 'kind': 'ask', 'prompt': 'normal'}
    path = paths.processing / 'outcome-1.json'
    path.write_text(json.dumps(request))
    result = _handle_gateway_request(agent, path)
    assert [e.type for _, e in events] == ['turn_started', 'turn_ended']
    assert events[-1][1].facts['status'] == ('failed' if failure else 'done')
    def broken(*_):
        raise RuntimeError('publishing failed')
    monkeypatch.setattr(plugin_panels_http, 'publish_plugin_event', broken)
    again = _handle_gateway_request(agent, path)
    assert (again['ok'], again['status'], again['error_code']) == (result['ok'], result['status'], result['error_code'])


def test_disabled_gateway_context_does_not_assemble(gateway, monkeypatch):
    from agent_py_agent.agent.gateway_parts import event_points

    agent, _paths, server, _events = gateway
    agent.config.plugin_events_enabled = False
    assembled = []
    original = event_points.EventPointContext
    def context_probe(*args, **kwargs):
        assembled.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(event_points, 'EventPointContext', context_probe)
    assert event_points.gateway_event_context(agent, {}, server=server) is None
    assert assembled == []


@pytest.mark.parametrize('enabled', [False, True])
def test_prompt_owner_resolution_failure_is_closed_and_disabled_does_not_resolve(gateway, monkeypatch, enabled):
    from agent_py_agent.agent.gateway_parts import event_points, request_worker

    agent, _paths, server, events = gateway
    agent.config.plugin_events_enabled = enabled
    calls = []
    def unavailable(*_args):
        calls.append(True)
        raise request_worker.OwnerScopeUnavailableError('isolated owner unavailable')
    monkeypatch.setattr(request_worker, '_resolve_request_owner_identity', unavailable)
    event_points.prompt_queued(server, {'id': 'unknown-owner', 'prompt': 'private-marker'})
    assert calls == ([True] if enabled else [])
    assert events == []


# LLM: 活动回合里成功插话的回执是 active_pending，不是 created 的排队；_handle_idempotent_ordinary_ask 只在
#   "created and receipt.state == 'queued'" 时发 prompt_submitted，所以插话不能重复发一次"用户提交了提示"。
#   这里不伪造插话结果：processing 里放一条真实活动回合记录（同渠道/会话/用户、turn_phase=open），插话仍经
#   真实 handle_ask → steer_active_conversation_if_running 路径命中活动回合。
# 函数用途: 断言活动回合内的插话不产生第二条 prompt_submitted，原回合只有一次。
def test_active_turn_steer_does_not_publish_a_second_prompt_submitted(gateway):
    agent, paths, server, events = gateway
    ask = _Handler({"prompt": "开始任务", "conversation_id": "thread-steer",
                    "metadata": {"message_id": "msg-start"}})
    http_handlers.handle_ask(ask, server, lambda: "ask-start")
    assert ask.replies[-1][0] == 202 and ask.replies[-1][1]["status"] == "queued"
    request_id = ask.replies[-1][1]["request_id"]
    assert (paths.inbox / f"{request_id}.json").exists()

    # 把首条请求搬进 processing 并标成开放阶段：这就是"回合正在跑"的结构化事实，
    # 与真实 worker 认领后写入的形状一致（见 test_plugin_event_e2e.py 的同一做法）。
    request = json.loads((paths.inbox / f"{request_id}.json").read_text(encoding="utf-8"))
    request.update(status="processing", turn_phase="open")
    (paths.processing / f"{request_id}.json").write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    (paths.inbox / f"{request_id}.json").unlink()

    steer = _Handler({"prompt": "改用已有资料直接收口", "conversation_id": "thread-steer",
                      "metadata": {"message_id": "msg-steer"}})
    http_handlers.handle_ask(steer, server, lambda: "ask-steer")
    status, receipt = steer.replies[-1]
    # 回执 disposition 是结构化判据：active_turn_input 表示插话被绑到正在跑的回合，
    # 而不是像首次提交那样排队（那才会 disposition == 'queued' 并发 prompt_submitted）。
    assert status == 202 and receipt["disposition"] == "active_turn_input", receipt
    assert receipt["status"] != "queued" and receipt["target_turn_id"] == request_id, receipt
    assert not (paths.inbox / f"{receipt['request_id']}.json").exists(), "插话没有被当成新一轮排队"

    assert [event.type for _owner, event in events] == ["prompt_submitted"], \
        "原回合只发一次 prompt_submitted，插话不再发"
    _owner, event = events[0]
    assert event.facts["request_id"] == request_id
    assert event.facts["chars"] == len("开始任务")


class _EventConfigUnavailable:
    @property
    def config(self):
        raise RuntimeError('optional event config unavailable')


class _EventFlagUnavailable:
    @property
    def plugin_events_enabled(self):
        raise RuntimeError('optional event flag unavailable')


class _UnreadableEventFields:
    def __init__(self, reads):
        self.reads = reads

    def __getattr__(self, name):
        self.reads.append(name)
        raise RuntimeError('disabled observation must not read event fields')


class _BrokenEventFields(dict):
    def get(self, *_args):
        raise RuntimeError('optional event field failed')

    def __getattr__(self, _name):
        raise RuntimeError('optional event receipt failed')


@pytest.mark.parametrize('fault', ['missing_config', 'config_read', 'switch_read'])
def test_prompt_optional_config_failure_keeps_successful_ingress(gateway, fault):
    _agent, paths, server, events = gateway
    server.agent = (object() if fault == 'missing_config' else _EventConfigUnavailable()
                    if fault == 'config_read' else SimpleNamespace(config=_EventFlagUnavailable()))
    handler = _Handler({'prompt': 'normal', 'conversation_id': 'c-1'})
    http_handlers.handle_ask(handler, server, lambda: 'config-fault-1')
    assert handler.replies == [(202, {'request_id': 'config-fault-1', 'status': 'queued'})]
    assert json.loads((paths.inbox / 'config-fault-1.json').read_text())['goal'] == 'normal'
    assert events == []


@pytest.mark.parametrize('entry', ['prompt', 'turn', 'command'])
@pytest.mark.parametrize('fault', ['missing_config', 'config_read', 'switch_read'])
def test_gateway_event_entries_config_fail_closed_without_business_exception(entry, fault):
    from agent_py_agent.agent.gateway_parts import event_points

    agent = (object() if fault == 'missing_config' else _EventConfigUnavailable()
             if fault == 'config_read' else SimpleNamespace(config=_EventFlagUnavailable()))
    if entry == 'prompt':
        assert event_points.prompt_queued(SimpleNamespace(agent=agent), {}) is None
    elif entry == 'turn':
        assert event_points.turn_started(agent, {}) is None
    else:
        receipt = SimpleNamespace(channel='local', conversation_id='thread', command_kind='verbose',
                                  operation_id='event-op', result={'ok': True})
        assert event_points.command_completed(agent, receipt) is None


@pytest.mark.parametrize('entry', ['prompt', 'turn', 'command', 'ended'])
def test_gateway_event_entries_disabled_never_read_routing_or_receipt(gateway, entry):
    from agent_py_agent.agent.gateway_parts import event_points
    from agent_py_agent.agent.plugin_events.points import EventPointContext

    reads = []
    agent, _paths, server, events = gateway
    agent.config.plugin_events_enabled = False
    source = _UnreadableEventFields(reads)
    context = EventPointContext(agent.config, lambda e: events.append(e))
    calls = {'prompt': partial(event_points.prompt_queued, server, source),
             'turn': partial(event_points.turn_started, agent, source),
             'command': partial(event_points.command_completed, agent, source),
             'ended': partial(event_points.turn_ended, context, source, 1)}
    assert calls[entry]() is None
    assert reads == []
    assert events == []


@pytest.mark.parametrize('entry', ['prompt', 'turn', 'command', 'ended'])
def test_gateway_event_entries_bad_routing_never_escapes(gateway, monkeypatch, entry):
    from agent_py_agent.agent.gateway_parts import event_points

    agent, _paths, server, events = gateway
    broken = _BrokenEventFields()
    monkeypatch.setattr(event_points, '_prompt_has_base_owner', lambda *_: True)
    context = event_points.gateway_event_context(agent, {}, server=server)
    calls = {'prompt': partial(event_points.prompt_queued, server, broken),
             'turn': partial(event_points.turn_started, agent, broken),
             'command': partial(event_points.command_completed, agent, broken),
             'ended': partial(event_points.turn_ended, context, broken, 1)}
    assert calls[entry]() is None
    assert events == []
