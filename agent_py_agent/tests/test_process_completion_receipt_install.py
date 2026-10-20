"""原redo安装writer的硬错/actual异常不能在完成通知消费叶转成普通pending。"""
import pytest

from agent_py_agent.tests.test_multiprocess_process_completion import _freeze, completion

_FIXTURES = (completion,)


@pytest.mark.parametrize('kind', ['permission', 'context_missing'])
def test_receipt_install_does_not_hide_hard_or_actual_exception(completion, monkeypatch, kind):
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    from agent_py_agent.agent.tooling import process_session_commit as commit
    agent, authority, record, _ = completion
    ref = _freeze(agent, record)
    error = PermissionError('synthetic permission failure') if kind == 'permission' else RequestExecutionContextUnavailable('synthetic')
    original = commit.write_json_file_atomic_unlocked
    def fail_exact_install(path, payload):
        if path == authority.record_path(record['session_id']):
            raise error
        return original(path, payload)
    monkeypatch.setattr(commit, 'write_json_file_atomic_unlocked', fail_exact_install)
    caught, result = None, None
    try:
        result = consume_process_completion(agent, ref)
    except BaseException as actual:
        caught = actual
    assert caught is error, {'returned': result, 'raised_type': type(caught).__name__ if caught is not None else None}


def test_committed_local_redo_io_keeps_pending_and_recovers_original_notice(completion, monkeypatch):
    from agent_py_agent.agent.gateway_parts.process_completion_source import (
        consume_process_completion,
    )
    from agent_py_agent.agent.tooling import process_session_commit as commit

    agent, authority, record, _ = completion
    ref = _freeze(agent, record)
    original = commit.write_json_file_atomic_unlocked

    def fail_exact_install(path, payload):
        if path == authority.record_path(record['session_id']):
            raise OSError('synthetic ordinary local I/O')
        return original(path, payload)

    with monkeypatch.context() as guard:
        guard.setattr(commit, 'write_json_file_atomic_unlocked', fail_exact_install)
        assert consume_process_completion(agent, ref) == 'pending'
    assert (authority.root / '.process-sessions.redo.json').exists()
    [first] = agent.conversation_store.wakes.pending()
    assert consume_process_completion(agent, ref) == 'source_changed'
    assert not (authority.root / '.process-sessions.redo.json').exists()
    assert authority.load(record['session_id']).record['completion_notice_id'] == first.wake_signal_id
    [same] = agent.conversation_store.wakes.pending()
    assert same.wake_signal_id == first.wake_signal_id
