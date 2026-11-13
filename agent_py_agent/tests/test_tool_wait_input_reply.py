"""工具物理执行不动，等待安全点处理本人输入且保留唯一最终结果。"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import tool_wait_input as wait_input


def request(enabled=True, token=None):
    return SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(tool_wait_input_reply_enabled=enabled)),
        params=SimpleNamespace(consume_pending_turn_input=True, cancellation_token=token))


def test_input_checkpoint_runs_while_worker_alive_and_returns_original_result_once():
    running, release, replied = Event(), Event(), Event()
    expected = object()
    calls = []

    def worker():
        running.set()
        assert release.wait(5)
        return expected

    def reply(_):
        assert running.is_set() and not release.is_set()
        calls.append(True)
        replied.set()
        release.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker)
        assert running.wait(2)
        assert wait_input.await_tool_futures(request(), [future], input_reply=reply) == [expected]
    assert replied.is_set() and len(calls) == 1


def test_reply_failure_keeps_original_worker_result_and_does_not_retry_blindly():
    release = Event()
    expected = object()
    calls = []

    def reply(_):
        calls.append(True)
        release.set()
        raise ValueError('synthetic reply generation failure')

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: (release.wait(5), expected)[1])
        assert wait_input.await_tool_futures(request(), [future], input_reply=reply) == [expected]
    assert len(calls) == 1


def test_cancelled_wait_does_not_generate_reply_or_replace_real_tool_exception():
    token = SimpleNamespace(cancelled=True)
    error = RuntimeError('actual tool failed')
    release = Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: (release.wait(0.2), (_ for _ in ()).throw(error))[1])
        with pytest.raises(RuntimeError) as raised:
            wait_input.await_tool_futures(request(token=token), [future],
                input_reply=lambda _: pytest.fail('cancelled input must not generate'))
    assert raised.value is error


def test_disabled_setting_preserves_original_blocking_result_without_model_call():
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: 7)
        assert wait_input.await_tool_futures(request(False), [future],
            input_reply=lambda _: pytest.fail('disabled')) == [7]


def test_zero_scheduler_serial_uses_original_worker_once(monkeypatch):
    from agent_py_agent.agent.agent_core.tool_loop import round_execution as rounds

    req = request()
    req.agent.tools = None
    req.agent._current_run_task_workspace = None
    req.params.tool_runtime_snapshot = None
    req.params.tool_protocol_snapshot = None
    running, release = Event(), Event()
    expected = object()
    seen = []
    monkeypatch.setattr(rounds, 'scheduler_for_agent', lambda agent: None)
    monkeypatch.setattr(rounds, '_capture_parallel_thread_context', lambda agent: None)

    def worker(*args):
        seen.append(True)
        running.set()
        assert release.wait(5)
        return expected

    def reply(_):
        assert running.is_set()
        release.set()

    monkeypatch.setattr(rounds, '_owned_serial_call', worker)
    monkeypatch.setattr(wait_input, 'reply_to_pending_input', reply)
    call = SimpleNamespace(call_id='call-real', tool_name='run_command', arguments={})
    work = SimpleNamespace(call=call)
    assert rounds._execute_owned_serial(req, work) is expected
    assert seen == [True]


@pytest.mark.parametrize('failure', ['', 'display', 'append_after_commit', 'media', 'external'])
def test_original_guidance_ack_reply_once_and_ir_unchanged(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.conversation.guidance_reply_obligations import pending
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings import AgentConfig
    from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params

    agent = SimpleAgent(AgentConfig(model_backend='echo', my_agent_home=str(tmp_path / 'home')), tmp_path / 'work')
    thread = agent.conversation_store.threads.get_or_create({'channel': 'feishu' if failure == 'external' else 'chat',
        'channel_conversation_id': 'wait-reply', 'channel_user_id': 'local'})
    observed = []

    class Backend:
        name = 'wait_reply_fake'
        def generate(self, prompt, **kwargs):
            observed.append(prompt)
            return ModelResponse(backend='wait_reply_fake', text='命令仍在执行；我已收到新的要求，完成后继续处理。')

    agent.backend = Backend()
    marker = object()
    committed = []
    def display(payload):
        if failure == 'display':
            raise OSError('display delivery failed')
        committed.append(payload)
    sink = SimpleNamespace(commit_model_response=display)
    params = _tool_loop_params(task_attributes={'conversation_thread_id': thread.thread_id},
                               tool_ir_history=[marker], effective_on_chunk=sink)
    entry = agent.conversation_store.guidance.append_once({'target_type': 'agent_run', 'target_id': params.run_id,
        'message': '现在进展如何？结束后请按新的要求整理。', 'metadata': {
            'kind': 'active_turn_user_input', 'thread_id': thread.thread_id, 'channel_message_id': 'client-wait-1',
            'expected_turn_id': params.request_id}}, dedupe_key='wait-input-test-0001')
    if failure == 'media':
        from agent_py_agent.tests.test_active_turn_media_interjection import _media_packet
        # 用新的原入口合成媒体条目，保留同一回合范围；前一个纯文本条目先独立取消。
        agent.conversation_store.guidance.mark_status('wait-input-test-0001', 'rejected')
        entry = agent.conversation_store.guidance.append_once({'target_type': 'agent_run', 'target_id': params.run_id,
            'message': '请结合图片继续', 'metadata': {'kind': 'active_turn_user_input',
                'thread_id': thread.thread_id, 'expected_turn_id': params.request_id,
                'input_media': _media_packet()['input_media']}}, dedupe_key='wait-input-media-0001')
    req = SimpleNamespace(agent=agent, params=params)
    if failure == 'media':
        assert wait_input.reply_to_pending_input(req) is False
        assert observed == []
        assert agent.conversation_store.guidance.receipt('wait-input-media-0001').status == 'reserved'
        assert params.active_turn_user_inputs[0]['input_media']
        assert params.tool_ir_history == [marker]
        wait_input.flush_resume_inputs(params)
        assert params.tool_ir_history[-1].media
        return
    if failure == 'append_after_commit':
        append = agent.conversation_store.messages.append_once
        def commit_then_fail(*args, **kwargs):
            result = append(*args, **kwargs)
            if kwargs.get('dedupe_key', '').startswith('active-tool-input-reply:'):
                raise OSError('canonical committed before crash')
            return result
        monkeypatch.setattr(agent.conversation_store.messages, 'append_once', commit_then_fail)
        with pytest.raises(OSError):
            wait_input.reply_to_pending_input(req)
    else:
        assert wait_input.reply_to_pending_input(req)
    assert not wait_input.reply_to_pending_input(req)
    assert len(observed) == 1
    assert params.tool_ir_history == [marker]
    assert any('新的要求' in value for value in params.tool_context)
    rows, errors = agent.conversation_store.messages.recent_report(thread.thread_id, limit=20)
    assert not errors
    replies = [row for row in rows if row.role == 'assistant' and row.metadata.get('reply_is_interim')]
    assert len(replies) == 1 and replies[0].metadata['user_reply_input_ids'] == [entry.guidance_id]
    outstanding = pending(agent.conversation_store.storage, thread.thread_id, params.task_id)
    assert outstanding == ((entry.guidance_id,) if failure else ())
    if failure in {'display', 'external'}:
        assert not any('[工具等待时已向用户回复]' in value for value in params.runtime_injections)


def test_input_probe_skips_child_and_event_snapshot(monkeypatch):
    from agent_py_agent.agent.agent_core.runtime import guidance

    monkeypatch.setattr(guidance, 'has_pending_request_guidance', lambda *args, **kwargs: False)
    monkeypatch.setattr(guidance, '_has_unseen_direct_child_result', lambda *args: pytest.fail('child scan'))
    monkeypatch.setattr(guidance, '_pending_task_events', lambda *args: pytest.fail('event scan'))
    params = SimpleNamespace(pending_turn_input_source=None)
    assert guidance.has_pending_turn_input(object(), params, user_input_only=True) is False


@pytest.mark.parametrize('kind', ['cli', 'tui'])
def test_already_persisted_input_reply_is_displayed_without_second_commentary_persistence(kind, capsys):
    from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime
    from agent_py_agent.cli.run_output import make_run_chunk_writer
    from agent_py_agent.tests.test_committed_model_messages import _payload

    sink = (TuiRuntime('wait-reply').begin_turn('wait-turn') if kind == 'tui' else
            make_run_chunk_writer(SimpleNamespace(stop=lambda: None), {}))
    payload = _payload([{'text': '已经持久保存的临时答复'}], boundary='tools')
    payload['canonical_message_id'] = 'msg-already-persisted'
    sink.commit_model_response(payload)
    assert sink.assistant_commentary_messages() == ()
    if kind == 'cli':
        assert '已经持久保存的临时答复' in capsys.readouterr().out
