"""W只读校验真实物理锁红绿；原五回执断言、Agent、pool和权限链保持。"""
import gc
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import private_approval_control as private
from agent_py_agent.agent.gateway_parts import private_interaction_service as interactions
from agent_py_agent.tests.test_coordinator_approval_decision import (
    test_four_control_slots_do_not_wait_for_five_original_approval_deadlines as original_five,
)


# LLM: 只在合成原库持标准SQLite物理锁；原Agent/权限/回执/五断言不替换，观察器只记录input原writer次数。
# 函数用途: 为原五控制测试制造一次可短暂释放或持续的真实WAL读取冲突，finally关闭准确持锁线程。
def _physical_five(tmp_path, monkeypatch, *, transient, post_input_error=False, relock=False):
    from agent_py_agent.agent.common import json_io

    gate, local = threading.Lock(), threading.local()
    captured = {'injected': False, 'calls': {}, 'writes': {}, 'errors': [], 'waits': []}
    original_submit = private._submit_approval_input
    original_validate = interactions.validated_private_interaction_from_config
    original_write = json_io.write_json_file_atomic
    from agent_py_agent.agent.concurrency import interrupt
    original_wait = interrupt.wait_interruptibly

    def observe_wait(delay):
        captured['waits'].append(delay)
        return original_wait(delay)

    monkeypatch.setattr(interrupt, 'wait_interruptibly', observe_wait)

    def observe_write(path, value, **kwargs):
        if isinstance(value, dict) and value.get('decision_input'):
            op = value['decision_input']['operation_id']
            captured['writes'][op] = captured['writes'].get(op, 0) + 1
        result = original_write(path, value, **kwargs)
        if post_input_error and isinstance(value, dict) and value.get('decision_input') and not captured.get('post_input_raised'):
            captured['post_input_raised'] = True
            raise captured['errors'][0]
        return result

    def observe_validation(*args, **kwargs):
        current = getattr(local, 'current', None)
        if current:
            op, release, released = current
            captured['calls'][op] = captured['calls'].get(op, 0) + 1
        try:
            return original_validate(*args, **kwargs)
        except sqlite3.OperationalError as error:
            if current:
                captured['errors'].append(error)
                if transient:
                    release.set()
                    assert released.wait(2), '准确持锁线程未退出'
                    if relock:
                        import time

                        from agent_py_agent.agent.session_lock.store import SessionLockStore
                        selected = local.agent
                        SessionLockStore(selected.home_paths.root / 'session_lock/private_chat_locks.db').record_activity(
                            kwargs['actor_id'],
                            now=time.time() - selected.config.feishu_personal_idle_lock_seconds - 100)
            raise

    def hold(path, ready, release, released, errors):
        connection = sqlite3.connect(path, timeout=0)
        try:
            assert connection.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
            assert connection.execute('PRAGMA locking_mode=EXCLUSIVE').fetchone()[0] == 'exclusive'
            connection.execute('BEGIN EXCLUSIVE')
            connection.execute('UPDATE private_chat_locks SET updated_at=updated_at')
            ready.set()
            assert release.wait(2), '测试未释放真实物理锁'
        except BaseException as error:
            errors.append(error)
            ready.set()
        finally:
            connection.rollback()
            connection.close()
            released.set()

    def wrapped_submit(agent, receipt, **kwargs):
        with gate:
            if captured['injected']:
                return original_submit(agent, receipt, **kwargs)
            captured['injected'] = True
            captured['operation_id'] = receipt.operation_id
            gc.collect()
            ready, release, released = threading.Event(), threading.Event(), threading.Event()
            errors = []
            thread = threading.Thread(target=hold, args=(agent.home_paths.root / 'session_lock/private_chat_locks.db',
                                                        ready, release, released, errors), name='synthetic-wal-holder')
            thread.start()
            try:
                assert ready.wait(2) and not errors, errors
                local.agent = agent
                local.current = receipt.operation_id, release, released
                return original_submit(agent, receipt, **kwargs)
            finally:
                local.current = None
                release.set()
                thread.join(2)
                assert not thread.is_alive() and not errors, errors

    monkeypatch.setattr(json_io, 'write_json_file_atomic', observe_write)
    monkeypatch.setattr(interactions, 'validated_private_interaction_from_config', observe_validation)
    monkeypatch.setattr(private, '_submit_approval_input', wrapped_submit)
    if transient and not post_input_error and not relock:
        original_five(tmp_path, monkeypatch)
    else:
        with pytest.raises(AssertionError):
            original_five(tmp_path, monkeypatch)
    rows = [json.loads(path.read_text()) for path in (tmp_path / 'gateway/control_operations').glob('*.json')]
    assert len(rows) == 5
    assert all(type(error.sqlite_errorcode) is int and error.sqlite_errorcode & 255 == sqlite3.SQLITE_BUSY
               for error in captured['errors'])
    return captured, rows


def test_transient_real_wal_busy_keeps_all_five_original_controls_executing(tmp_path, monkeypatch):
    captured, rows = _physical_five(tmp_path, monkeypatch, transient=True)
    assert captured['errors'] and len(captured['errors']) == 1
    assert captured['waits'] == [0.005]
    assert captured['calls'][captured['operation_id']] == 2
    assert all(row['state'] == 'executing' for row in rows)
    assert len(captured['writes']) == 5 and set(captured['writes'].values()) == {1}


def test_persistent_real_wal_busy_exhausts_and_never_republishes_input(tmp_path, monkeypatch):
    captured, rows = _physical_five(tmp_path, monkeypatch, transient=False)
    assert captured['calls'][captured['operation_id']] == 3
    assert len(captured['errors']) == 3
    assert captured['waits'] == [0.005, 0.010]
    assert [row['state'] for row in rows].count('terminal_unknown') == 1
    assert captured['operation_id'] not in captured['writes']
    assert len(captured['writes']) == 4 and set(captured['writes'].values()) == {1}


@pytest.mark.parametrize('code', [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_READONLY, sqlite3.SQLITE_CORRUPT, None, '5', True])
def test_only_actual_integer_primary_busy_is_retryable(monkeypatch, code):
    error = sqlite3.OperationalError('synthetic boundary error')
    if code is not None:
        error.sqlite_errorcode = code
    calls = []
    def fail(*_args, **_kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(interactions, 'validated_private_interaction_from_config', fail)
    from agent_py_agent.agent.concurrency import interrupt
    monkeypatch.setattr(interrupt, 'wait_interruptibly', lambda _delay: pytest.fail('不可重试的错误不能等待'))
    # 只测异常分流，不构造实际Agent或授予权限；stub在触碰任何宿主字段之前失败。
    from types import SimpleNamespace
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=Path('.')), config=None)
    receipt = SimpleNamespace(channel='synthetic', user_id='synthetic')
    with pytest.raises(sqlite3.OperationalError) as caught:
        private._validated_approval_input(agent, receipt, {}, retry_read_busy=True)
    assert caught.value is error and len(calls) == 1


def test_default_path_does_not_retry_busy(monkeypatch):
    from types import SimpleNamespace
    error = sqlite3.OperationalError('synthetic boundary busy')
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    calls = []
    def fail(*_args, **_kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(interactions, 'validated_private_interaction_from_config', fail)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=Path('.')), config=None)
    receipt = SimpleNamespace(channel='synthetic', user_id='synthetic')
    with pytest.raises(sqlite3.OperationalError) as caught:
        private._validated_approval_input(agent, receipt, {})
    assert caught.value is error and len(calls) == 1


@pytest.mark.parametrize('result', ['expired', 'revoked', 'cancelled'])
def test_full_validation_repeats_and_stops_when_current_authority_changes(monkeypatch, result):
    from types import SimpleNamespace
    error = sqlite3.OperationalError('synthetic boundary busy')
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    terminal = ValueError('synthetic original validation expired') if result == 'expired' else InterruptedError('synthetic cancellation')
    calls, waits = [], []
    def validate(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise error
        if result == 'revoked':
            return {}, {'error_code': 'IM_UNLOCK_REQUIRED'}
        raise terminal
    from agent_py_agent.agent.concurrency import interrupt
    monkeypatch.setattr(interactions, 'validated_private_interaction_from_config', validate)
    monkeypatch.setattr(interrupt, 'wait_interruptibly', waits.append)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=Path('.')), config=None)
    receipt = SimpleNamespace(channel='synthetic', user_id='synthetic')
    if result == 'revoked':
        assert private._validated_approval_input(agent, receipt, {}, retry_read_busy=True)[1]['error_code'] == 'IM_UNLOCK_REQUIRED'
    else:
        with pytest.raises(type(terminal)) as caught:
            private._validated_approval_input(agent, receipt, {}, retry_read_busy=True)
        assert caught.value is terminal
    assert len(calls) == 2 and waits == [0.005]


def test_real_busy_after_durable_input_is_never_retried(tmp_path, monkeypatch):
    captured, rows = _physical_five(tmp_path, monkeypatch, transient=True, post_input_error=True)
    assert captured['post_input_raised'] is True
    assert captured['calls'][captured['operation_id']] == 2
    assert len(captured['writes']) == 5 and set(captured['writes'].values()) == {1}
    assert [row['state'] for row in rows].count('terminal_unknown') == 1


def test_real_personal_lock_change_during_backoff_is_read_again_and_not_authorized(tmp_path, monkeypatch):
    captured, rows = _physical_five(tmp_path, monkeypatch, transient=True, relock=True)
    assert captured['calls'][captured['operation_id']] == 2
    assert len(captured['errors']) == 1
    assert not captured['writes']
    assert all(row['state'] == 'terminal_unknown' for row in rows)


def test_actual_typed_execution_error_is_preserved_without_retry(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
    )
    from agent_py_agent.agent.concurrency import interrupt
    error = RequestExecutionContextUnavailable('synthetic typed boundary')
    calls = []
    def fail(*_args, **_kwargs):
        calls.append(True)
        raise error
    monkeypatch.setattr(interactions, 'validated_private_interaction_from_config', fail)
    monkeypatch.setattr(interrupt, 'wait_interruptibly', lambda _delay: pytest.fail('typed错误不能等待'))
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=Path('.')), config=None)
    receipt = SimpleNamespace(channel='synthetic', user_id='synthetic')
    with pytest.raises(RequestExecutionContextUnavailable) as caught:
        private._validated_approval_input(agent, receipt, {}, retry_read_busy=True)
    assert caught.value is error and len(calls) == 1
