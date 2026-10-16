"""MC4-402：全部关闭、恢复 Context、保留第一异常身份。"""

import sqlite3

import pytest

from agent_py_agent.agent.io import sqlite_scope
from agent_py_agent.agent.io.sqlite_connection import TrackedSQLiteConnection
from agent_py_agent.agent.io.sqlite_scope import scoped_connection, sqlite_connection_scope


class CleanupStop(BaseException):
    pass


@pytest.fixture
def closing_factory():
    closed, failures = [], {}

    class ControlledClose(TrackedSQLiteConnection):
        rollback_error = None

        def close(self):
            closed.append(self)
            super().close()
            if self in failures:
                raise failures[self]

        def rollback(self):
            if self.rollback_error is not None:
                raise self.rollback_error
            super().rollback()

    return ControlledClose, closed, failures


def fill_cache(tmp_path, factory):
    opened = []
    for index in range(3):
        path = tmp_path / f'close-{index}.db'
        with scoped_connection(path, False, lambda: sqlite3.connect(path, factory=factory)) as lease:
            lease.execute('SELECT 1').fetchone()
            opened.append(lease._target)
    return opened


def assert_all_closed(opened, closed, scope):
    assert closed == opened
    assert scope.entries == {}
    for conn in opened:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute('SELECT 1')


@pytest.mark.parametrize('position', [0, 1])
@pytest.mark.parametrize('error_type', [CleanupStop, RuntimeError])
def test_close_interrupt_processes_all_entries_and_preserves_first(tmp_path, closing_factory, position, error_type):
    factory, closed, failures = closing_factory
    first, later = error_type('first cleanup'), CleanupStop('later cleanup')
    with sqlite_connection_scope(True):
        outer = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
        with pytest.raises(BaseException) as caught, sqlite_connection_scope(True):
            scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
            opened = fill_cache(tmp_path, factory)
            failures[opened[position]] = first
            failures[opened[2]] = later
        assert caught.value is first
        assert sqlite_scope._SQLITE_ACTIVE_SCOPE.get() is outer
        assert_all_closed(opened, closed, scope)


def test_business_error_survives_scope_cleanup_interrupts(tmp_path, closing_factory):
    factory, closed, failures = closing_factory
    body = ValueError('business failure')
    before = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
    with pytest.raises(BaseException) as caught:
        with sqlite_connection_scope(True):
            scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
            opened = fill_cache(tmp_path, factory)
            failures.update({conn: CleanupStop('cleanup') for conn in opened})
            raise body
    assert caught.value is body
    assert sqlite_scope._SQLITE_ACTIVE_SCOPE.get() is before
    assert_all_closed(opened, closed, scope)


@pytest.mark.parametrize('body_failed', [False, True])
def test_return_interrupt_preserves_business_or_first_cleanup_error(tmp_path, closing_factory, body_failed):
    factory, closed, failures = closing_factory
    rollback_error, close_error = CleanupStop('rollback'), CleanupStop('close')
    body = ValueError('body')
    factory.rollback_error = rollback_error
    path = tmp_path / 'dirty.db'
    with pytest.raises(BaseException) as caught, sqlite_connection_scope(True), scoped_connection(
        path, False, lambda: sqlite3.connect(path, factory=factory),
    ) as conn:
        scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
        physical = conn._target
        failures[physical] = close_error
        conn.execute('CREATE TABLE dirty(value TEXT)')
        conn.execute("INSERT INTO dirty VALUES('pending')")
        if body_failed:
            raise body
    assert caught.value is (body if body_failed else rollback_error)
    assert_all_closed([physical], closed, scope)


@pytest.mark.parametrize('body_failed', [False, True])
def test_enabled_overflow_keeps_original_error(tmp_path, closing_factory, monkeypatch, body_failed):
    factory, closed, failures = closing_factory
    monkeypatch.setattr(sqlite_scope, '_SQLITE_SCOPE_MAX_CONNECTIONS_COUNT', 0)
    body, close_error = ValueError('business'), CleanupStop('overflow close')
    path = tmp_path / 'overflow.db'
    with pytest.raises(BaseException) as caught, sqlite_connection_scope(True), scoped_connection(
        path, False, lambda: sqlite3.connect(path, factory=factory),
    ) as conn:
        scope = sqlite_scope._SQLITE_ACTIVE_SCOPE.get()
        physical = conn._target
        failures[physical] = close_error
        conn.execute('SELECT 1').fetchone()
        if body_failed:
            raise body
    assert caught.value is (body if body_failed else close_error)
    assert_all_closed([physical], closed, scope)
