"""摘要只缓存完整声明，不缓存任何路径或数据库初始化状态。"""

import hashlib
import json
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_py_agent.agent.io import sqlite_schema
from agent_py_agent.agent.local_storage.store import LocalStore
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def digest_calls(monkeypatch):
    cache = getattr(sqlite_schema, '_cached_fingerprint', None)
    if cache is not None:
        cache.cache_clear()
    calls = {'serialized': [], 'hashed': []}
    dumps, sha256 = json.dumps, hashlib.sha256

    def serialize(value, **kwargs):
        calls['serialized'].append(value)
        return dumps(value, **kwargs)

    def hash_bytes(value):
        calls['hashed'].append(value)
        return sha256(value)

    monkeypatch.setattr(sqlite_schema.json, 'dumps', serialize)
    monkeypatch.setattr(sqlite_schema.hashlib, 'sha256', hash_bytes)
    return calls


def test_equal_declarations_serialize_and_hash_once(digest_calls):
    declaration = ('digest-counter', ('CREATE TABLE facts(value TEXT)',))
    expected = sqlite_schema.schema_fingerprint(declaration)
    equal_copy = ['digest-counter', ['CREATE TABLE facts(value TEXT)']]
    assert equal_copy is not declaration
    assert sqlite_schema.schema_fingerprint(equal_copy) == expected
    assert sqlite_schema.schema_fingerprint(declaration) == expected
    assert len(digest_calls['serialized']) == 1
    assert len(digest_calls['hashed']) == 1


def test_declared_changes_invalidate_digest_without_poisoning_old_value(digest_calls):
    declarations = ['digest-change', ['CREATE TABLE facts(value TEXT)']]
    before = sqlite_schema.schema_fingerprint(declarations)
    declarations[1].append('CREATE INDEX facts_value ON facts(value)')
    after = sqlite_schema.schema_fingerprint(declarations)
    assert after != before
    assert sqlite_schema.schema_fingerprint(declarations) == after
    declarations[1].pop()
    assert sqlite_schema.schema_fingerprint(declarations) == before
    assert len(digest_calls['serialized']) == 2
    assert len(digest_calls['hashed']) == 2


@pytest.mark.parametrize('declarations', [
    (True,), (1,), (None,), {'nested': {'first': [1, False], 'second': 2}},
    [('nested', [('first', [1, False]), ('second', 2)])],
],
)
def test_cache_key_preserves_original_json_shape_and_scalar_types(declarations):
    encoded = json.dumps(declarations, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    expected = hashlib.sha256(encoded).hexdigest()
    assert sqlite_schema.schema_fingerprint(declarations) == expected


def test_concurrent_first_lookup_computes_once(digest_calls):
    barrier = threading.Barrier(4)

    def compute(_):
        barrier.wait(timeout=5)
        return sqlite_schema.schema_fingerprint(('digest-concurrent', ('CREATE TABLE facts(value TEXT)',)))

    with ThreadPoolExecutor(max_workers=4) as pool:
        values = list(pool.map(compute, range(4)))
    assert len(set(values)) == 1
    assert len(digest_calls['serialized']) == 1
    assert len(digest_calls['hashed']) == 1


@pytest.mark.parametrize('store_type', [RuntimeRepository, LocalStore])
def test_reconstructing_store_reuses_declaration_digest(tmp_path, store_type, digest_calls):
    first = store_type(tmp_path / 'store.db')
    assert first is not None
    initial = len(digest_calls['hashed'])
    assert initial == 1
    store_type(tmp_path / 'store.db')
    store_type(tmp_path / 'another-owner.db')
    assert len(digest_calls['hashed']) == initial


@pytest.mark.parametrize('store_type', [RuntimeRepository, LocalStore])
def test_cached_declaration_does_not_hide_persisted_fingerprint_change(tmp_path, store_type):
    path = tmp_path / 'restored.db'
    first = store_type(path)
    borrow = first._runtime_connection if store_type is RuntimeRepository else first._connection
    with borrow() as conn:
        original = conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0]
        conn.execute("UPDATE metadata SET value='old-restored-copy' WHERE key='sqlite_schema_fingerprint'")
        conn.commit()
    reopened = store_type(path)
    borrow = reopened._runtime_connection if store_type is RuntimeRepository else reopened._connection
    with borrow() as conn:
        assert conn.execute("SELECT value FROM metadata WHERE key='sqlite_schema_fingerprint'").fetchone()[0] == original


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='目标平台没有fork，须另做平台生命周期验收')
@pytest.mark.parametrize('enabled', [False, True])
def test_schema_queries_and_real_stores_survive_fork_with_another_thread_holding_lock(tmp_path, enabled):
    result = subprocess.run(
        [sys.executable, '-m', 'agent_py_agent.tests.support.sqlite_schema_lock_fork_probe',
         str(tmp_path), 'on' if enabled else 'off'],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {
        'hot_matches': True, 'cold_matches': True, 'lock_changed': True,
        'parent_lock_usable': True, 'stores': ['RuntimeRepository', 'LocalStore'],
    }
