"""复用项目真实 PostgreSQL 测试入口；每个用例仅操作新建的随机测试库/角色。"""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from agent_py_agent.agent.owner_object_store import (
    OwnerObjectConfig,
    OwnerSnapshotLeaseLost,
    OwnerSnapshotStore,
)
from agent_py_agent.agent.pg_rls import tenant_session
from agent_py_agent.agent.runtime_schema import (
    apply_runtime_migrations,
    require_runtime_schema_current,
)
from agent_py_agent.agent.storage_backend import StorageBackend
from agent_py_agent.tests.test_pg_rls import _pg
from agent_py_agent.tests.test_r8_owner_snapshot_changes import Journal
from agent_py_agent.tests.test_s1w2_owner_snapshot_delta import _Objects, _write_old


@pytest.fixture
def snapshot_pg():
    admin = _pg()
    import os

    original = make_url(os.environ.get('TEST_POSTGRES_URL', 'postgresql+psycopg://localhost:5432/postgres'))
    name, role, password = 'r8_snapshot_' + uuid4().hex[:12], 'r8_app_' + uuid4().hex[:12], uuid4().hex
    created_database = created_role = False
    db = app = None
    try:
        with admin.connect() as connection:
            connection = connection.execution_options(isolation_level='AUTOCOMMIT')
            connection.execute(text(f'CREATE DATABASE {name}'))
            created_database = True
            connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'"))
            created_role = True
        db = StorageBackend(original.set(database=name).render_as_string(hide_password=False))
        apply_runtime_migrations(db, include_scale_data=True, app_role=role)
        app = StorageBackend(original.set(database=name, username=role, password=password).render_as_string(hide_password=False))
        require_runtime_schema_current(app, include_scale_data=True, app_role=role)
        store = OwnerSnapshotStore(app, _Objects(), OwnerObjectConfig('test'))
        store._changes = Journal()
        yield db, app, store
    finally:
        for backend in (app, db):
            if backend is not None:
                backend.engine.dispose()
        with admin.connect() as connection:
            connection = connection.execution_options(isolation_level='AUTOCOMMIT')
            if created_database:
                connection.execute(text(f'DROP DATABASE {name} WITH (FORCE)'))
            if created_role:
                connection.execute(text(f'DROP ROLE {role}'))
        admin.engine.dispose()


def _seed_file(store, tenant, relative, data):
    digest = hashlib.sha256(data).hexdigest()
    key = store._object_key(tenant, digest)
    store._client.values[key] = data
    with tenant_session(store._backend, tenant) as conn:
        conn.execute(text('''INSERT INTO owner_file_manifest VALUES(:tenant,'user','owner',:path,:key,:digest,:size,384,1)
            ON CONFLICT(tenant,owner_kind,owner_id,relative_path) DO UPDATE SET
            object_key=EXCLUDED.object_key,content_sha256=EXCLUDED.content_sha256,size_bytes=EXCLUDED.size_bytes'''),
                     {'tenant': tenant, 'path': relative, 'key': key, 'digest': digest, 'size': len(data)})


@contextmanager
def _checkout(store, tmp_path):
    warm = tmp_path / 'warm'
    home = warm / 'home/owners/u1'
    with store.checkout_delta('acme', 'user', 'owner', home, warm_dir=warm, lease_ttl_seconds=30,
                              hold_advisory_lock=True):
        yield home


def test_real_postgres_legacy_manifest_writes_feed_incremental_warm_restore(snapshot_pg, tmp_path):
    db, app, store = snapshot_pg
    _seed_file(store, 'acme', 'a', b'old')
    _seed_file(store, 'acme', 'b', b'delete')
    _seed_file(store, 'globex', 'other', b'private')
    with _checkout(store, tmp_path):
        pass
    _seed_file(store, 'acme', 'a', b'new')
    with tenant_session(app, 'acme') as conn:
        conn.execute(text("DELETE FROM owner_file_manifest WHERE relative_path='b'"))
    store._read_manifest = lambda *_: pytest.fail('remote change used complete manifest')
    store._warm_paths = lambda *_: pytest.fail('remote change used complete local scan')
    with _checkout(store, tmp_path) as home:
        assert (home / 'a').read_bytes() == b'new'
        assert not (home / 'b').exists()
    with tenant_session(app, 'acme') as conn:
        rows = conn.execute(text('SELECT tenant FROM owner_file_changes')).all()
        assert rows and {row.tenant for row in rows} == {'acme'}
    with tenant_session(app, 'globex') as conn:
        assert conn.execute(text('SELECT count(*) FROM owner_file_changes')).scalar_one() == 1
    from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex
    with OwnerSnapshotIndex.open(tmp_path / 'warm/snapshot-index.sqlite3') as index:
        with tenant_session(app, 'acme') as conn:
            version = conn.execute(text("SELECT version FROM owner_snapshot_head WHERE owner_id='owner'")).scalar_one()
        assert index.metadata()['version'] == version
        assert index.metadata()['mode'] == 'incremental'


def test_real_postgres_generation_fences_out_of_protocol_concurrent_manifest_write(snapshot_pg, tmp_path):
    db, app, store = snapshot_pg
    _seed_file(store, 'acme', 'a', b'old')
    with _checkout(store, tmp_path):
        pass
    with pytest.raises(OwnerSnapshotLeaseLost):
        with _checkout(store, tmp_path) as home:
            _write_old(home / 'a', b'local uncommitted')
            store._changes.record('a')
            _seed_file(store, 'acme', 'a', b'remote winning write')
    assert store._read_manifest('acme', 'user', 'owner')[0].content_sha256 == hashlib.sha256(b'remote winning write').hexdigest()


def test_real_postgres_projection_floor_is_partial_and_trigger_readiness_is_mandatory(snapshot_pg):
    db, app, store = snapshot_pg
    for number in range(100):
        _seed_file(store, 'acme', str(number), str(number).encode())
    with tenant_session(app, 'acme') as conn:
        conn.execute(text("UPDATE owner_snapshot_head SET version=65599 WHERE owner_id='owner'"))
    _seed_file(store, 'acme', 'latest', b'latest')
    delta = store._read_manifest_delta('acme', 'user', 'owner', after=0, through=65600)
    assert not delta.complete and delta.reason == 'remote_change_cursor_expired'
    with tenant_session(app, 'acme') as conn:
        assert conn.execute(text('SELECT count(*) FROM owner_file_changes')).scalar_one() == 37
        assert conn.execute(text('SELECT count(*) FROM owner_file_manifest')).scalar_one() == 101
    with db.begin() as conn:
        conn.execute(text('ALTER TABLE owner_file_manifest DISABLE TRIGGER owner_manifest_generation'))
    try:
        with pytest.raises(RuntimeError, match='触发器'):
            require_runtime_schema_current(app, include_scale_data=True, app_role=make_url(str(app.engine.url)).username)
    finally:
        with db.begin() as conn:
            conn.execute(text('ALTER TABLE owner_file_manifest ENABLE TRIGGER owner_manifest_generation'))
