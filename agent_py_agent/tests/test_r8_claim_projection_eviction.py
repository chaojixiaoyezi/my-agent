"""多 store 交错不能因目录缓存淘汰永久失去续点。"""
import pytest

from agent_py_agent.agent.common.json_io import write_private_json_file_atomic_unlocked as write
from agent_py_agent.agent.conversation import claim_activity_projection as view
from agent_py_agent.agent.conversation.store import ConversationStore


@pytest.mark.parametrize("portable,count", [(False, 6), (True, 12)])
def test_six_large_stores_round_robin_converge(tmp_path, monkeypatch, portable, count):
    # 只在支持目录FD的平台启用原生cookie；Windows同时覆盖6/12个store的实际后端。
    monkeypatch.setattr(view, "_NATIVE", view._NATIVE and not portable)
    stores = []
    for number in range(count):
        store = ConversationStore(tmp_path / str(number))
        thread = store.threads.create()
        for item in range(300):
            write(store.storage.background_claims_dir / f'{item}.json',
                  {'status': 'finished', 'thread_id': thread.thread_id})
        stores.append((store, thread.thread_id))
    reads = []
    original = view._read_claim_report
    monkeypatch.setattr(view, '_read_claim_report', lambda path: (reads.append(path), original(path))[1])
    complete = set()
    for _ in range(30):
        for number, (store, thread_id) in enumerate(stores):
            reads.clear()
            state = view.claim_activity_state(store.storage, thread_id)
            assert len(reads) <= 128
            if state == '':
                complete.add(number)
        if len(complete) == len(stores):
            break
    assert len(complete) == len(stores), '交错轮询必须保留每个store的扫描续点'


def test_completed_cache_never_hides_bad_claim_or_deleted_directory(tmp_path):
    store = ConversationStore(tmp_path / 'store')
    directory = store.storage.background_claims_dir
    path = directory / 'bad.json'
    path.write_text('{broken', encoding='utf-8')
    assert view.claim_activity_state(store.storage, 'thread') == 'activity_unknown'
    path.unlink()
    assert view.claim_activity_state(store.storage, 'thread') == ''
    directory.rmdir()
    assert view.claim_activity_state(store.storage, 'thread') == ''
    directory.mkdir()
    write(directory / 'new.json', {'status': 'running', 'thread_id': 'thread'})
    assert view.claim_activity_state(store.storage, 'thread') == 'foreground_active'


def test_cache_sqlite_memory_and_scan_descriptors_are_fixed():
    with view._LOCK:
        conn = view._connection()
        assert conn.execute('PRAGMA cache_size').fetchone()[0] == -256
        assert conn.execute('PRAGMA mmap_size').fetchone()[0] == 0
        assert conn.execute('PRAGMA journal_mode').fetchone()[0] == 'memory'
        assert conn.execute('PRAGMA synchronous').fetchone()[0] == 0
        assert len(view._PORTABLE) <= 8


def test_projection_transaction_failure_never_signs_idle(tmp_path, monkeypatch):
    import sqlite3
    store = ConversationStore(tmp_path / 'failure')
    original = view._cleanup
    def fail(conn):
        original(conn)
        raise sqlite3.OperationalError('temporary projection failure')
    monkeypatch.setattr(view, '_cleanup', fail)
    assert view.claim_activity_state(store.storage, 'thread') == 'activity_unknown'
    monkeypatch.setattr(view, '_cleanup', original)
    assert view.claim_activity_state(store.storage, 'thread') == ''


@pytest.mark.skipif(not hasattr(__import__('os'), 'fork'), reason='requires POSIX fork')
def test_child_rebuild_keeps_parent_cache_directory(tmp_path):
    import os
    from pathlib import Path
    store = ConversationStore(tmp_path / 'fork')
    assert view.claim_activity_state(store.storage, 'thread') == ''
    parent_directory = Path(view._CACHE[1].name)
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            assert view._CACHE is None and not view._PORTABLE
            assert view.claim_activity_state(store.storage, 'thread') == ''
            assert Path(view._CACHE[1].name) != parent_directory
            view._CACHE[1].cleanup()
            os.write(write_fd, b'ok')
        finally:
            os._exit(0)
    os.close(write_fd)
    result = os.read(read_fd, 2)
    os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    assert status == 0 and result == b'ok'
    assert parent_directory.is_dir()
    assert view.claim_activity_state(store.storage, 'thread') == ''


def test_projection_commit_failure_overrides_idle_result(tmp_path, monkeypatch):
    import sqlite3
    store = ConversationStore(tmp_path / 'commit-failure')
    original = view._connection
    conn = original()
    class FailedCommit:
        def __enter__(self):
            conn.__enter__()
            return self
        def __exit__(self, *_args):
            conn.rollback()
            raise sqlite3.OperationalError('cache commit failed')
        def __getattr__(self, name):
            return getattr(conn, name)
    monkeypatch.setattr(view, '_connection', lambda: FailedCommit())
    assert view.claim_activity_state(store.storage, 'thread') == 'activity_unknown'
    monkeypatch.setattr(view, '_connection', original)
    assert view.claim_activity_state(store.storage, 'thread') == ''


@pytest.mark.skipif(not hasattr(__import__('os'), 'fork'), reason='requires POSIX fork')
def test_fork_waits_for_other_thread_sql_and_keeps_parent_cache(tmp_path, monkeypatch):
    import os
    import threading
    from pathlib import Path

    store = ConversationStore(tmp_path / 'concurrent-fork')
    assert view.claim_activity_state(store.storage, 'thread') == ''
    parent_directory = Path(view._CACHE[1].name)
    sql_entered, release_sql = threading.Event(), threading.Event()
    fork_requested, fork_returned = threading.Event(), threading.Event()
    errors, children = [], []
    original = view._cleanup

    def wait_in_sql():
        sql_entered.set()
        if not release_sql.wait(3):
            raise RuntimeError('test did not release SQL')
        return 1

    def cleanup(conn):
        original(conn)
        conn.create_function('claim_fork_wait', 0, wait_in_sql)
        conn.execute('SELECT claim_fork_wait()').fetchone()

    def read():
        try:
            view.claim_activity_state(store.storage, 'thread')
        except BaseException as error:
            errors.append(error)

    def fork():
        try:
            fork_requested.set()
            pid = os.fork()
            if pid == 0:
                # 再次 fork 也要读 child 当前锁，而不是注册时绑定的父锁。
                nested = os.fork()
                if nested == 0:
                    os._exit(0)
                _, status = os.waitpid(nested, 0)
                os._exit(0 if status == 0 and view._CACHE is None else 1)
            children.append(pid)
            fork_returned.set()
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(view, '_cleanup', cleanup)
    reader = threading.Thread(target=read, daemon=True)
    forker = threading.Thread(target=fork, daemon=True)
    reader.start()
    assert sql_entered.wait(2)
    forker.start()
    try:
        assert fork_requested.wait(2)
        assert not fork_returned.wait(.1), 'fork must wait until module SQL leaves its lock'
    finally:
        release_sql.set()
        reader.join(3)
        forker.join(3)
    assert not reader.is_alive() and not forker.is_alive() and not errors
    assert fork_returned.is_set()
    for pid in children:
        _, status = os.waitpid(pid, 0)
        assert status == 0
    monkeypatch.setattr(view, '_cleanup', original)
    assert parent_directory.is_dir()
    assert view.claim_activity_state(store.storage, 'thread') == ''
