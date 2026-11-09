# LLM: 使用计数对象存储和事务记录器核对增量快照协议，不把假连接结果视为真实 PostgreSQL 验收。
# 模块用途: 覆盖 stat、暂存上传、租约围栏、温缓存和并发 slot pin。
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_object_store as snapshots
from agent_py_agent.agent import scale_downstream as downstream
from agent_py_agent.agent.owner_snapshot_index import OwnerStatIndex, stage_and_hash


def test_stat_index_exact_signature_racy_and_roundtrip(tmp_path):
    source = tmp_path / "data"
    source.write_bytes(b"content")
    st = source.lstat()
    sha = hashlib.sha256(b"content").hexdigest()
    index = OwnerStatIndex()
    index.record("data", st, sha, st.st_mtime + 3)
    assert index.lookup("data", st) == sha
    for field in ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode"):
        values = {key: getattr(st, key) for key in (
            "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_mode",
        )}
        values[field] += 1
        assert index.lookup("data", SimpleNamespace(**values)) is None
    path = tmp_path / "stat-index.json"
    index.save(path)
    assert json.loads(path.read_text())["version"] == 1
    loaded = OwnerStatIndex.load(path)
    assert loaded.lookup("data", st) == sha
    assert loaded.paths() == {"data"}
    loaded.record("data", st, sha, st.st_mtime)
    assert loaded.lookup("data", st) is None
    loaded.forget("data")
    assert loaded.paths() == set()


@pytest.mark.parametrize("content", ["{", "[]", '{"version": 9}', '{"version": 1, "entries": {"x": []}}',
                                     '{"version": true, "entries": {}}', '{"version": 1, "entries": null}'])
def test_stat_index_corrupt_or_unknown_is_empty(tmp_path, content):
    path = tmp_path / "index"
    path.write_text(content)
    assert OwnerStatIndex.load(path).paths() == set()
    assert OwnerStatIndex.load(tmp_path / "absent").paths() == set()


def test_stage_hash_uses_copy_after_live_rewrite(tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"before")
    staged, digest, size = stage_and_hash(source, tmp_path / "staging")
    source.write_bytes(b"after")
    assert staged.read_bytes() == b"before"
    assert digest == hashlib.sha256(staged.read_bytes()).hexdigest()
    assert size == len(b"before")


def test_stage_hash_rejects_write_during_copy(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.write_bytes(b"before")
    copy = shutil.copyfile

    def rewriting_copy(src, dst):
        copy(src, dst)
        src.write_bytes(b"changed during copy")

    monkeypatch.setattr("agent_py_agent.agent.owner_snapshot_index.shutil.copyfile", rewriting_copy)
    with pytest.raises(RuntimeError, match=f"owner 文件在快照时仍被写入: {source}"):
        stage_and_hash(source, tmp_path / "staging")
    assert list((tmp_path / "staging").iterdir()) == []


def test_snapshot_head_ddl_and_rls_registry():
    from agent_py_agent.agent.runtime_schema import runtime_migrations

    statements = []
    conn = SimpleNamespace(execute=lambda sql: statements.append(str(sql)))
    migrations = runtime_migrations(SimpleNamespace(is_postgres=True), include_scale_data=True, app_role="app_role")
    migrations[10].apply(conn)
    assert statements == [
        "CREATE TABLE IF NOT EXISTS owner_snapshot_head (tenant TEXT NOT NULL, owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, version BIGINT NOT NULL DEFAULT 0, holder TEXT NULL, holder_incarnation TEXT NULL, lease_expires_at TIMESTAMPTZ NULL, updated_at BIGINT NOT NULL, PRIMARY KEY (tenant, owner_kind, owner_id))"
    ]
    migrations[11].apply(conn)
    assert statements[1:4] == [
        "ALTER TABLE owner_snapshot_head ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE owner_snapshot_head FORCE ROW LEVEL SECURITY",
        "DROP POLICY IF EXISTS owner_snapshot_head_tenant_isolation ON owner_snapshot_head",
    ]
    assert "USING (tenant = current_setting('app.tenant_id', true)) WITH CHECK" in statements[4]
    assert statements[-2:] == ["REVOKE ALL ON owner_snapshot_head FROM PUBLIC",
                               "GRANT SELECT, INSERT, UPDATE, DELETE ON owner_snapshot_head TO app_role"]


class _Objects:
    def __init__(self):
        self.values = {}
        self.uploads = []
        self.downloads = []
        self.heads = []

    def head_object(self, *, Bucket, Key):
        self.heads.append(Key)
        if Key not in self.values:
            error = RuntimeError("missing")
            error.response = {"Error": {"Code": "404"}}
            raise error

    def upload_file(self, source, bucket, key, *, ExtraArgs):
        self.uploads.append((Path(source), key))
        self.values[key] = Path(source).read_bytes()

    def download_file(self, bucket, key, target):
        self.downloads.append(key)
        Path(target).write_bytes(self.values[key])


class _FakeHead:
    def __init__(self):
        self.rows = {}
        self.now = 0.0
        self.expires = 0.0
        self.incarnation = None
        self.version = 0
        self.busy = 0
        self.releases = []
        self.commits = []
        self.renewals = 0
        self.renewed = threading.Event()
        self.fail_commit = None

    def acquire(self, tenant, kind, owner, *, holder, incarnation, ttl_seconds):
        if self.busy:
            self.busy -= 1
            return None
        if self.incarnation and self.expires > self.now:
            return None
        self.incarnation = incarnation
        self.expires = self.now + ttl_seconds
        return self.version

    def seconds_until_expiry(self, tenant, kind, owner):
        return 5.0

    def renew(self, tenant, kind, owner, *, incarnation, ttl_seconds):
        self.renewals += 1
        if self.renewals >= 2:
            self.renewed.set()
        if incarnation != self.incarnation or self.expires <= self.now:
            return False
        self.expires = self.now + ttl_seconds
        return True

    def commit(self, tenant, kind, owner, *, incarnation, removed, changed, expected_version=None):
        if self.fail_commit:
            raise self.fail_commit
        if (incarnation != self.incarnation or self.expires <= self.now
                or expected_version is not None and expected_version != self.version):
            raise snapshots.OwnerSnapshotLeaseLost("expired or generation changed")
        self.commits.append((list(removed), list(changed)))
        for rel in removed:
            self.rows.pop(rel, None)
        self.rows.update((row.relative_path, row) for row in changed)
        self.version += 1
        self.incarnation = None
        return self.version

    def release(self, tenant, kind, owner, *, incarnation):
        self.releases.append(incarnation)
        if self.incarnation == incarnation:
            self.incarnation = None


def _delta_store(tmp_path):
    store = object.__new__(snapshots.OwnerSnapshotStore)
    store._client = _Objects()
    store._head = _FakeHead()
    store._bucket, store._prefix, store._sse, store._kms_key = "owners", "prod", "AES256", ""
    store._read_manifest = lambda *_args: list(store._head.rows.values())
    warm = tmp_path / "warm"
    home = warm / "home" / "owners" / "u1"
    return store, warm, home


def _checkout(store, warm, home, ttl=90, compat=False):
    assert callable(getattr(store, "checkout_delta", None)), "delta checkout is required"
    return store.checkout_delta("acme", "user", "u1", home, warm_dir=warm,
                                lease_ttl_seconds=ttl, hold_advisory_lock=compat)


def _write_old(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)
    old = time.time() - 10
    os.utime(path, (old, old))


def _seed(store, rel, payload):
    digest = hashlib.sha256(payload).hexdigest()
    key = store._object_key("acme", digest)
    store._client.values[key] = payload
    store._head.rows[rel] = snapshots.OwnerFile(rel, key, digest, len(payload), 0o600)


def test_delta_first_restore_and_clean_unchanged_modified_removed(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    restores = []
    restore = store.restore

    def observed_restore(*args):
        assert json.loads((warm / "state.json").read_text()) == {"state": "DIRTY"}
        restores.append(True)
        return restore(*args)

    store.restore = observed_restore
    with _checkout(store, warm, home):
        _write_old(home / "a", b"a")
        _write_old(home / "b", b"b")
    assert restores == [True]
    assert len(store._client.uploads) == 2
    assert json.loads((warm / "state.json").read_text()) == {"state": "CLEAN"}
    store._client.uploads.clear()
    store._client.heads.clear()
    with _checkout(store, warm, home):
        assert (home / "a").read_bytes() == b"a"
    assert store._client.downloads == store._client.uploads == store._client.heads == []
    assert store._head.commits[-1] == ([], [])
    with _checkout(store, warm, home):
        _write_old(home / "a", b"changed")
        (home / "b").unlink()
    assert len(store._client.uploads) == 1
    removed, changed = store._head.commits[-1]
    assert removed == ["b"]
    assert [r.relative_path for r in changed] == ["a"]
    assert set(store._head.rows) == {"a"}
    assert not (warm / "staging").exists()


def test_clean_reconcile_detects_full_writer_and_prunes_every_other_path(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    with _checkout(store, warm, home):
        _write_old(home / "a", b"old")
        _write_old(home / "b", b"keep")
    _seed(store, "a", b"remote")
    (home / "tmp").mkdir()
    (home / "tmp" / "x").write_bytes(b"discard")
    sibling = warm / "home" / "other"
    sibling.mkdir()
    (sibling / "secret").write_bytes(b"discard")
    with _checkout(store, warm, home):
        assert (home / "a").read_bytes() == b"remote"
        assert (home / "b").read_bytes() == b"keep"
        assert not sibling.exists()
        assert not (home / "tmp").exists()
    assert store._client.downloads == [store._head.rows["a"].object_key]


def test_body_failure_is_dirty_released_and_next_turn_restores(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    _seed(store, "data", b"durable")
    with pytest.raises(ValueError, match="body"):
        with _checkout(store, warm, home):
            (home / "data").write_bytes(b"uncommitted")
            raise ValueError("body")
    assert store._head.commits == []
    assert len(store._head.releases) == 1
    assert json.loads((warm / "state.json").read_text())["state"] == "DIRTY"
    with _checkout(store, warm, home):
        assert (home / "data").read_bytes() == b"durable"
    assert len(store._client.downloads) == 2


def test_expired_lease_cannot_commit_even_without_takeover(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    assert hasattr(snapshots, "OwnerSnapshotLeaseLost")
    with pytest.raises(snapshots.OwnerSnapshotLeaseLost):
        with _checkout(store, warm, home, ttl=3):
            _write_old(home / "data", b"stale")
            store._head.now = 4
    assert store._head.rows == {}
    assert json.loads((warm / "state.json").read_text())["state"] == "DIRTY"


def test_commit_unknown_keeps_dirty_and_staged_upload_matches_hash(tmp_path, monkeypatch):
    store, warm, home = _delta_store(tmp_path)
    assert hasattr(snapshots, "OwnerSnapshotCommitUnknown")
    stage = snapshots.stage_and_hash

    def rewrite_after_stage(source, staging):
        result = stage(source, staging)
        source.write_bytes(b"later live bytes")
        return result

    monkeypatch.setattr(snapshots, "stage_and_hash", rewrite_after_stage)
    store._head.fail_commit = snapshots.OwnerSnapshotCommitUnknown("commit disconnected")
    with pytest.raises(snapshots.OwnerSnapshotCommitUnknown):
        with _checkout(store, warm, home):
            _write_old(home / "data", b"staged bytes")
    assert list(store._client.values.values()) == [b"staged bytes"]
    assert hashlib.sha256(b"staged bytes").hexdigest() in store._client.uploads[0][1]
    assert store._client.uploads[0][0].suffix == ".stage"
    assert not store._client.uploads[0][0].exists()
    assert json.loads((warm / "state.json").read_text())["state"] == "DIRTY"


def test_busy_lease_waits_without_raising_and_renewer_stops(tmp_path, monkeypatch):
    store, warm, home = _delta_store(tmp_path)
    store._head.busy = 2
    sleeps = []
    monkeypatch.setattr(snapshots.time, "sleep", sleeps.append)
    with _checkout(store, warm, home, ttl=6):
        pass
    assert sleeps == [2, 2]
    with _checkout(store, warm, home, ttl=0.3):
        assert store._head.renewed.wait(0.35)
    count = store._head.renewals
    threading.Event().wait(0.12)
    assert store._head.renewals == count
    assert not any(t.name == "owner-snapshot-lease-renew" for t in threading.enumerate())


def test_symlink_forces_cold_restore_and_turn_symlink_refuses_commit(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    assert hasattr(snapshots, "OwnerSnapshotLeaseLost")
    with _checkout(store, warm, home):
        _write_old(home / "data", b"safe")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    (home / "link").symlink_to(outside)
    with _checkout(store, warm, home):
        assert not (home / "link").exists()
    assert len(store._client.downloads) == 1
    before = len(store._head.commits)
    with pytest.raises(RuntimeError, match="快照拒绝符号链接"):
        with _checkout(store, warm, home):
            (home / "link").symlink_to(outside)
    assert len(store._head.commits) == before
    assert json.loads((warm / "state.json").read_text())["state"] == "DIRTY"
    assert outside.read_bytes() == b"outside"


class _RecordingBackend:
    def __init__(self, *, version=7, exit_error=None, statement_error=None):
        self.version = version
        self.exit_error = exit_error
        self.statement_error = statement_error
        self.statements = []
        self.connections = []
        self.active = 0
        self.rolled_back = False

    @contextmanager
    def begin(self):
        backend = self

        class Connection:
            tenant = None

            def execute(self, sql, params=None):
                sql = str(sql)
                backend.statements.append((self, sql, params))
                if "set_config('app.tenant_id'" in sql:
                    self.tenant = params["t"]
                else:
                    assert self.tenant == "acme"
                    if backend.statement_error and "owner_snapshot_head" in sql:
                        raise backend.statement_error
                value = 5.0 if "AS wait_seconds" in sql else backend.version
                return SimpleNamespace(scalar_one_or_none=lambda: value,
                                       rowcount=0 if backend.version is None else 1)

        conn = Connection()
        self.connections.append(conn)
        self.active += 1
        try:
            yield conn
        except BaseException:
            self.rolled_back = True
            raise
        else:
            if self.exit_error:
                raise self.exit_error
        finally:
            self.active -= 1


def _sql_head(backend):
    assert hasattr(snapshots, "_OwnerSnapshotHead"), "transactional lease helper is required"
    return snapshots._OwnerSnapshotHead(backend)


def test_sql_head_db_clock_and_lease_incarnation_predicates():
    db = _RecordingBackend()
    head = _sql_head(db)
    assert head.acquire("acme", "user", "u1", holder="host:1", incarnation="inc", ttl_seconds=9) == 7
    assert head.renew("acme", "user", "u1", incarnation="inc", ttl_seconds=9)
    assert head.seconds_until_expiry("acme", "user", "u1") == 5.0
    head.release("acme", "user", "u1", incarnation="inc")
    statements = [sql for _, sql, _ in db.statements if "owner_snapshot_head" in sql]
    assert "WHERE owner_snapshot_head.lease_expires_at IS NULL OR owner_snapshot_head.lease_expires_at <= now() RETURNING version" in statements[0]
    assert "make_interval(secs => :ttl)" in statements[0]
    assert "holder_incarnation = :inc AND lease_expires_at > now()" in statements[1]
    assert "lease_expires_at - now()" in statements[2]
    assert "holder_incarnation = :inc" in statements[3]
    assert all("lease_expires_at > :" not in s and "lease_expires_at <= :" not in s for s in statements)
    db.version = None
    assert head.acquire("acme", "user", "u1", holder="host", incarnation="inc", ttl_seconds=9) is None
    assert not head.renew("acme", "user", "u1", incarnation="inc", ttl_seconds=9)
    assert db.active == 0


def test_sql_commit_cas_precedes_delta_and_uses_one_tenant_transaction():
    db = _RecordingBackend()
    head = _sql_head(db)
    row = snapshots.OwnerFile("new", "object", "sha", 3, 0o600)
    assert head.commit("acme", "user", "u1", incarnation="inc", removed=["old"], changed=[row]) == 7
    assert len(db.connections) == 1 and db.active == 0
    assert len({id(conn) for conn, _, _ in db.statements}) == 1
    sql = [s for _, s, _ in db.statements]
    assert "set_config('app.tenant_id'" in sql[0]
    assert sql[1].startswith("UPDATE owner_snapshot_head SET version = version + 1")
    assert "holder_incarnation = :inc AND lease_expires_at > now() RETURNING version" in sql[1]
    assert sql[2] == "DELETE FROM owner_file_manifest WHERE owner_kind=:kind AND owner_id=:owner AND relative_path = :path"
    assert db.statements[2][2] == [{"kind": "user", "owner": "u1", "path": "old"}]
    assert "ON CONFLICT (tenant, owner_kind, owner_id, relative_path) DO UPDATE SET" in sql[3]
    assert db.statements[3][2][0]["path"] == "new"


def test_sql_fencing_rejects_before_any_manifest_statement():
    db = _RecordingBackend(version=None)
    head = _sql_head(db)
    with pytest.raises(snapshots.OwnerSnapshotLeaseLost):
        head.commit("acme", "user", "u1", incarnation="stale", removed=["old"], changed=[])
    assert db.rolled_back
    assert not any("owner_file_manifest" in sql for _, sql, _ in db.statements)


def test_commit_exit_connection_failure_is_unknown_but_body_error_is_original():
    error = ConnectionError("commit disconnected")
    db = _RecordingBackend(exit_error=error)
    head = _sql_head(db)
    with pytest.raises(snapshots.OwnerSnapshotCommitUnknown) as caught:
        head.commit("acme", "user", "u1", incarnation="inc", removed=[], changed=[])
    assert caught.value.__cause__ is error
    error = ConnectionError("statement disconnected")
    db = _RecordingBackend(statement_error=error)
    with pytest.raises(ConnectionError) as caught:
        _sql_head(db).commit("acme", "user", "u1", incarnation="inc", removed=[], changed=[])
    assert caught.value is error and db.rolled_back


@pytest.mark.parametrize("ttl", [0, -1, float("nan")])
def test_delta_validation_rejects_nonpositive_ttl(tmp_path, ttl):
    store, warm, home = _delta_store(tmp_path)
    with pytest.raises(ValueError):
        with _checkout(store, warm, home, ttl=ttl):
            pytest.fail("invalid lease must never execute")
    assert store._head.releases == []


def test_delta_validation_requires_head_and_bounded_home(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    with pytest.raises(ValueError):
        with _checkout(store, warm, tmp_path / "outside"):
            pytest.fail("escaped home must never execute")
    store._head = None
    with pytest.raises(RuntimeError):
        with _checkout(store, warm, home):
            pytest.fail("no head must never execute")


@pytest.mark.parametrize("mode,expected", [(None, "full"), ("delta", "delta"),
                                          (" Delta_Compat ", "delta_compat"), ("bogus", "full")])
def test_mode_reader_once_with_legacy_default(tmp_path, monkeypatch, mode, expected):
    config = SimpleNamespace(background_claim_ttl_seconds=12)
    if mode is not None:
        config.scale_owner_snapshot_mode = mode
    monkeypatch.setattr(downstream, "load_config", lambda _: config)
    monkeypatch.setattr(downstream, "apply_runtime_config_environment", lambda value: value)
    monkeypatch.setattr(downstream, "ScaleRuntimeConfig", SimpleNamespace(from_env=lambda *_: SimpleNamespace(database_url="unused")))
    monkeypatch.setattr(downstream, "StorageBackend", lambda _: None)
    monkeypatch.setattr(downstream, "owner_store_from_runtime", lambda *_: None)
    monkeypatch.setenv("MY_AGENT_SCALE_WORKSPACE_ROOT", str(tmp_path))
    pool = downstream.ScaleAgentPool()
    assert getattr(pool, "_snapshot_mode", None) == expected
    assert pool._lease_ttl_seconds == 12
    config.scale_owner_snapshot_mode = "bogus"
    assert pool._snapshot_mode == expected


@pytest.mark.parametrize("mode", ["full", "delta", "delta_compat"])
def test_execute_checkout_compatibility_and_commit_before_reply(tmp_path, monkeypatch, mode):
    assert hasattr(downstream.ScaleAgentPool, "_snapshot_mode")
    events, calls = [], []

    class Store:
        @contextmanager
        def checkout_delta(self, *args, **kwargs):
            calls.append((args, kwargs))
            yield
            events.append("commit")

    pool = object.__new__(downstream.ScaleAgentPool)
    pool._workspaces, pool._store = tmp_path, Store()
    pool._snapshot_mode, pool._lease_ttl_seconds = mode, 9
    pool._build_agent = lambda *_: None
    pool._run_conversation_turn = lambda *_: {"response": "done", "turn_token_estimate": 4}
    monkeypatch.setattr(downstream, "_adapter", lambda: SimpleNamespace(
        reply_message=lambda *_: events.append("reply") or True))
    msg = downstream.ScaleMessage("acme", "user", "u1", "hello", "m1")
    trace = SimpleNamespace(trace_id="trace")
    assert pool._run_locked(msg, trace) == 4
    assert events == ["commit", "reply"]
    assert len(calls[0][0]) == 4
    assert calls[0][1] == {"warm_dir": pool._warm_dir(("acme", "user", "u1")),
                            "lease_ttl_seconds": 9, "hold_advisory_lock": mode != "delta"}
    assert not any((tmp_path / ".owner-executions").iterdir())


def test_pins_prevent_active_eviction_and_repin_same_slot_during_removal(tmp_path, monkeypatch):
    assert hasattr(downstream.ScaleAgentPool, "_warm_dir")
    pool = object.__new__(downstream.ScaleAgentPool)
    pool._lock, pool._slots, pool._limit = threading.Lock(), OrderedDict(), 1
    pool._workspaces, pool._snapshot_mode, pool._lease_ttl_seconds = tmp_path, "delta", 9
    entered, finish, evicting, delete, repinned = [threading.Event() for _ in range(5)]
    failures, observations = [], []
    zchecked = threading.Event()
    xkey = ("acme", "user", "X")
    xwarm = pool._warm_dir(xkey)
    calls = []

    class Store:
        @contextmanager
        def checkout_delta(self, tenant, kind, owner, home, *, warm_dir, **kwargs):
            assert not pool._lock.locked()
            if owner == "X" and calls.count("X") == 1:
                assert zchecked.wait(2)
            observations.append((owner, not (warm_dir / "state.json").exists()))
            if owner == "Z":
                zchecked.set()
            home.mkdir(parents=True, exist_ok=True)
            yield
            (warm_dir / "state.json").write_text('{"state":"CLEAN"}')

    pool._store = Store()
    pool._build_agent = lambda *_: None

    def turn(_agent, msg, _trace):
        calls.append(msg.owner_id)
        if msg.owner_id == "X" and calls.count("X") == 1:
            entered.set()
            assert finish.wait(2)
        return {"response": "ok"}

    pool._run_conversation_turn = turn
    monkeypatch.setattr(downstream, "_adapter", lambda: SimpleNamespace(reply_message=lambda *_: True))
    rmtree = shutil.rmtree

    def removing(path, *args, **kwargs):
        if Path(path) == xwarm:
            assert not pool._lock.locked() and xslot.lock.locked()
            evicting.set()
            assert delete.wait(2)
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(downstream.shutil, "rmtree", removing)

    def run(owner, trace):
        try:
            pool.run(downstream.ScaleMessage("acme", "user", owner, "hi", trace), SimpleNamespace(trace_id=trace))
        except BaseException as exc:
            failures.append(exc)

    first = threading.Thread(target=run, args=("X", "x1"))
    first.start()
    assert entered.wait(2)
    xslot = pool._slots[xkey]
    run("Y", "y1")
    assert pool._slots[xkey] is xslot and xslot.pins == 1
    finish.set()
    first.join(2)
    third = threading.Thread(target=run, args=("Z", "z1"))
    third.start()
    assert evicting.wait(2)
    slot = pool._slot

    def observing_slot(msg):
        result = slot(msg)
        if msg.owner_id == "X":
            assert result is xslot
            repinned.set()
        return result

    pool._slot = observing_slot
    again = threading.Thread(target=run, args=("X", "x2"))
    again.start()
    assert repinned.wait(2)
    assert xslot.pins == 2
    delete.set()
    third.join(2)
    again.join(2)
    assert not first.is_alive() and not third.is_alive() and not again.is_alive()
    assert failures == []
    assert observations == [("X", True), ("Y", True), ("Z", True), ("X", True)]
    assert all(s.pins == 0 for s in pool._slots.values()) and xslot.pins == 0


# LLM: 权限变化不能被 glob 的静默漏项解释成删除，失败时远端清单保持不变。
# 函数用途: 验证不可枚举目录使增量快照失败并保留 DIRTY。
def test_unreadable_directory_never_commits_false_deletions(tmp_path, monkeypatch):
    store, warm, home = _delta_store(tmp_path)
    with _checkout(store, warm, home):
        _write_old(home / "d" / "data", b"durable")
    previous = dict(store._head.rows)
    count = len(store._head.commits)
    directory = home / "d"
    if getattr(os, "geteuid", lambda: -1)() == 0:
        # root 的 chmod 不产生 EACCES；注入同一种扫描错误，不跳过保护断言。
        scandir = os.scandir

        def denied_scan(path):
            if Path(path) == directory and directory.stat().st_mode & 0o777 == 0:
                raise PermissionError("directory unreadable")
            return scandir(path)

        monkeypatch.setattr(os, "scandir", denied_scan)
    try:
        with pytest.raises(PermissionError):
            with _checkout(store, warm, home):
                directory.chmod(0)
        assert store._head.rows == previous
        assert len(store._head.commits) == count
        assert json.loads((warm / "state.json").read_text())["state"] == "DIRTY"
    finally:
        directory.chmod(0o700)


# LLM: 两秒 racy 是冻结的保守规则，未命中必须逐行下载；本例记录其代价，不冒称所有空回合都零 IO。
# 函数用途: 验证近期 mtime 不会绕过清单摘要校验。
def test_racy_restored_file_is_conservatively_redownloaded(tmp_path):
    store, warm, home = _delta_store(tmp_path)
    _seed(store, "data", b"fresh")
    with _checkout(store, warm, home):
        pass
    with _checkout(store, warm, home):
        assert (home / "data").read_bytes() == b"fresh"
    assert len(store._client.downloads) == 2


@pytest.mark.parametrize("state", ["{", "[]", '{"state":"DIRTY"}', '{"state":"unknown"}'])
def test_corrupt_or_dirty_marker_restores_authoritative_bytes(tmp_path, state):
    store, warm, home = _delta_store(tmp_path)
    with _checkout(store, warm, home):
        _write_old(home / "data", b"durable")
    (home / "data").write_bytes(b"local dirty")
    (warm / "state.json").write_text(state)
    with _checkout(store, warm, home):
        assert (home / "data").read_bytes() == b"durable"
    assert len(store._client.downloads) == 1


def test_successful_commit_but_index_save_failure_never_marks_clean(tmp_path, monkeypatch):
    store, warm, home = _delta_store(tmp_path)

    def fail_save(self, path):
        raise OSError("index fsync failed")

    from agent_py_agent.agent.owner_snapshot_cache import OwnerSnapshotIndex

    monkeypatch.setattr(OwnerSnapshotIndex, "set_metadata", fail_save)
    with pytest.raises(OSError, match="index fsync"):
        with _checkout(store, warm, home):
            _write_old(home / "data", b"committed")
    assert len(store._head.commits) == 1
    assert store._head.rows["data"].content_sha256 == hashlib.sha256(b"committed").hexdigest()
    assert json.loads((warm / "state.json").read_text())["state"] == "DIRTY"
