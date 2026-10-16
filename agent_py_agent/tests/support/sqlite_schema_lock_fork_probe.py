"""MC4-1101隔离探针：只fork本探针，超时只回收自己创建的子进程。"""

import hashlib
import json
import os
import select
import signal
import sys
import threading
from pathlib import Path

from agent_py_agent.agent.io import sqlite_schema
from agent_py_agent.agent.io.sqlite_lease import SQLiteConnectionLease
from agent_py_agent.agent.io.sqlite_scope import sqlite_connection_scope
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


def expected_digest(declaration):
    encoded = json.dumps(declaration, ensure_ascii=False, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def construct_store(root, store_type, enabled):
    store = store_type(root / f'{store_type.__name__}.db')
    borrow = store._runtime_connection if store_type is RuntimeRepository else store._connection
    with borrow() as conn:
        assert isinstance(conn, SQLiteConnectionLease) is enabled
        fingerprint = conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0]
        assert len(fingerprint) == 64
    return store_type.__name__


def construct_stores(root, enabled):
    with sqlite_connection_scope(enabled):
        return [construct_store(root, store_type, enabled) for store_type in (RuntimeRepository, LocalStore)]


def run_child(write_fd, root, inherited):
    old_lock, hot, enabled = inherited
    status = 0
    try:
        hot_digest = sqlite_schema.schema_fingerprint(hot)
        cold = ('child-cold', ['CREATE TABLE cold(value TEXT)'])
        cold_digest = sqlite_schema.schema_fingerprint(cold)
        facts = {'hot_matches': hot_digest == expected_digest(hot),
                 'cold_matches': cold_digest == expected_digest(cold),
                 'stores': construct_stores(root, enabled),
                 'lock_changed': sqlite_schema._SQLITE_SCHEMA_DIGEST_LOCK is not old_lock}
    except BaseException as exc:
        facts, status = {'error': repr(exc)}, 1
    os.write(write_fd, json.dumps(facts).encode())
    os.close(write_fd)
    os._exit(status)


def wait_child(pid, read_fd):
    readable, _, _ = select.select([read_fd], [], [], 5)
    if not readable:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        raise AssertionError('schema child timed out after parent lock holder released')
    facts = json.loads(os.read(read_fd, 8192))
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, facts
    return facts


def fork_while_locked(root, inherited, holder):
    release, thread = holder
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        run_child(write_fd, root, inherited)
    os.close(write_fd)
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    try:
        return wait_child(pid, read_fd)
    finally:
        os.close(read_fd)


def main():
    root, enabled = Path(sys.argv[1]), sys.argv[2] == 'on'
    hot = ('parent-hot', ['CREATE TABLE hot(value TEXT)'])
    assert sqlite_schema.schema_fingerprint(hot) == expected_digest(hot)
    old_lock = sqlite_schema._SQLITE_SCHEMA_DIGEST_LOCK
    cached = sqlite_schema._cached_fingerprint
    held, release = threading.Event(), threading.Event()

    def delayed_cache(key):
        if threading.current_thread() is thread:
            held.set()
            assert release.wait(timeout=10)
        return cached(key)

    thread = threading.Thread(target=lambda: sqlite_schema.schema_fingerprint(('holder',)), daemon=True)
    sqlite_schema._cached_fingerprint = delayed_cache
    thread.start()
    try:
        assert held.wait(timeout=5)
        facts = fork_while_locked(root, (old_lock, hot, enabled), (release, thread))
    finally:
        release.set()
        thread.join(timeout=5)
        sqlite_schema._cached_fingerprint = cached
    facts['parent_lock_usable'] = sqlite_schema.schema_fingerprint(hot) == expected_digest(hot)
    print(json.dumps(facts))


if __name__ == '__main__':
    main()
