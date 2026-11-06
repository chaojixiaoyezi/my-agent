"""Scale-path object counts with the real snapshot algorithms and in-memory storage ports."""
from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.observability.tracing import TraceContext
from agent_py_agent.agent.owner_object_store import OwnerFile, OwnerObjectConfig, OwnerSnapshotStore
from agent_py_agent.agent.scale_downstream import ScaleAgentPool, ScaleMessage

from .report import require_target


class ObjectClient:
    # LLM: The fake stores actual bytes and counts actual storage API calls.
    # 函数用途: Initialize an isolated in-memory object store.
    def __init__(self):
        self.objects, self.downloads, self.uploads = {}, [], []

    # LLM: Missing objects use the same structured code checked by the production upload path.
    # 函数用途: Look up a content-addressed object without treating a head call as an upload.
    def head_object(self, *, Bucket, Key):
        if Key not in self.objects:
            error = RuntimeError("missing fixture object")
            error.response = {"Error": {"Code": "404"}}
            raise error
        return {}

    # LLM: Downloaded bytes must pass the production digest and size checks.
    # 函数用途: Restore one recorded object into the temporary execution directory.
    def download_file(self, bucket, key, path):
        self.downloads.append(key)
        Path(path).write_bytes(self.objects[key])

    # LLM: Upload accounting captures actual byte transfer rather than enumeration or head calls.
    # 函数用途: Store the exact bytes supplied by the snapshot algorithm.
    def upload_file(self, path, bucket, key, **_kwargs):
        self.uploads.append(key)
        self.objects[key] = Path(path).read_bytes()


class MemoryBackend:
    # LLM: 仅替代外部数据库端口；生产租约SQL及快照算法照常运行，不声明真实PG/RLS验收。
    # 函数用途: 建立单测隔离的清单、租约与事务锁。
    def __init__(self, store):
        self.is_postgres, self.store = True, store
        self.lock, self.head, self.tenant = threading.RLock(), None, None

    # LLM: 同一事务绑定tenant，防续租线程与清单写入交错；不跳过tenant_session。
    # 函数用途: 提供生产租约助手实际调用的begin端口。
    @contextlib.contextmanager
    def begin(self):
        with self.lock:
            yield self

    # LLM: 未识别SQL必须失败；只模拟本fixture用到的tenant、incarnation、版本与清单持久值。
    # 函数用途: 执行快照租约和增量清单的内存数据库端口。
    def execute(self, statement, params):
        sql, value, count = str(statement), None, 0
        if sql.startswith("SELECT set_config"):
            self.tenant = params["t"]
        elif sql.startswith("INSERT INTO owner_snapshot_head"):
            assert params["tenant"] == self.tenant
            assert self.head is None or self.head["expires"] <= time.monotonic()
            version = self.head["version"] if self.head else 0
            self.head = {"version": version, "inc": params["inc"], "expires": time.monotonic() + params["ttl"]}
            value = version
        elif sql.startswith("UPDATE owner_snapshot_head"):
            assert params["tenant"] == self.tenant
            valid = self.head and self.head["inc"] == params["inc"]
            if "lease_expires_at > now()" in sql:
                valid = valid and self.head["expires"] > time.monotonic()
            if valid:
                count = 1
                if "version = version + 1" in sql:
                    self.head["version"] += 1
                    value = self.head["version"]
                if "holder = NULL" in sql:
                    self.head["inc"], self.head["expires"] = None, 0
                else:
                    self.head["expires"] = time.monotonic() + params["ttl"]
        elif sql.startswith("INSERT INTO owner_file_manifest"):
            rows = {row.relative_path: row for row in self.store.rows}
            for row in params:
                assert row["tenant"] == self.tenant
                rows[row["path"]] = OwnerFile(row["path"], row["object_key"], row["sha"], row["size"], row["mode"])
            self.store.rows = list(rows.values())
        elif sql.startswith("DELETE FROM owner_file_manifest"):
            removed = {row["path"] for row in params}
            self.store.rows = [row for row in self.store.rows if row.relative_path not in removed]
        else:
            raise AssertionError(f"unhandled snapshot SQL: {sql}")
        return SimpleNamespace(scalar_one_or_none=lambda: value, rowcount=count)


class MemoryManifestStore(OwnerSnapshotStore):
    # LLM: Only the external DB/object ports are fake; restore, commit and checkout remain production code.
    # 函数用途: Construct a snapshot store with a test-owned complete manifest.
    def __init__(self, client):
        self.rows = []
        super().__init__(MemoryBackend(self), client, OwnerObjectConfig("fixture"))

    # LLM: Single-threaded test ownership replaces only the external advisory lock.
    # 函数用途: Provide the database lock port without a PostgreSQL dependency.
    @contextlib.contextmanager
    def _owner_lock(self, *_args):
        yield

    # LLM: A complete manifest is the authoritative fake DB state in every tested mode.
    # 函数用途: Read the current manifest without altering object counters.
    def _read_manifest(self, *_args):
        return list(self.rows)

    # LLM: Commit replacement preserves exactly the production-generated rows.
    # 函数用途: Replace the in-memory manifest after successful byte uploads.
    def _replace_manifest(self, _tenant, _kind, _owner, rows):
        self.rows = list(rows)


# LLM: Public ScaleAgentPool.run drives the real checkout/commit path; only Agent and IM ports are fake.
# 函数用途: Compare per-turn storage transfers in full and delta modes without claiming enumeration is O(1).
@pytest.mark.slo
@pytest.mark.parametrize("mode", ["full", "delta"])
def test_snapshot_counts(tmp_path, monkeypatch, slo_data, slo_report, mode):
    scenario = next(row for row in slo_data["scenarios"] if row["id"] == f"snapshot_{mode}")
    profile, client = scenario["load_profile"], ObjectClient()
    store = MemoryManifestStore(client)
    for index in range(profile["files"]):
        data = f"initial-{index}".encode()
        digest = hashlib.sha256(data).hexdigest()
        key = store._object_key("tenant", digest)
        client.objects[key] = data
        store.rows.append(OwnerFile(f"file-{index}.txt", key, digest, len(data), 0o600))
    pool = object.__new__(ScaleAgentPool)
    import threading
    from collections import OrderedDict
    pool._lock, pool._slots, pool._limit = threading.Lock(), OrderedDict(), profile["files"]
    pool._workspaces, pool._store = tmp_path / "scale", store
    pool._base_config = SimpleNamespace(scale_owner_snapshot_mode=mode)
    pool._snapshot_mode, pool._lease_ttl_seconds = mode, slo_data["fixture"]["condition_seconds"]
    pool._build_agent = lambda _message, home_root, _workspace: SimpleNamespace(
        home_root=home_root)
    turn = [0]
    # LLM: Only the model/tool execution port is fake; the pool selects its production snapshot mode.
    # 函数用途: Modify the configured files inside the current production checkout and return a deterministic response.
    def execute(agent, message, trace):
        from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
        home = resolve_owner_home(agent.home_root, OwnerIdentity.provider_user("feishu", message.owner_id)).home_dir
        for index in range(profile["changed_files"]):
            (home / f"file-{index}.txt").write_text(f"turn-{turn[0]}")
        turn[0] += 1
        return {"response": "fixture-answer", "turn_token_estimate": 1}
    pool._run_conversation_turn = execute
    monkeypatch.setattr("agent_py_agent.agent.scale_downstream._adapter", lambda: SimpleNamespace(reply_message=lambda *_args: True))
    measurements = []
    for index in range(profile["turns"]):
        before = len(client.downloads), len(client.uploads)
        pool.run(ScaleMessage("tenant", "user", "synthetic", "fixture", str(index), "session", "synthetic"),
                 TraceContext(f"{index + 1:032x}", f"{index + 1:016x}"))
        measurements.append({"downloaded": len(client.downloads) - before[0], "uploaded": len(client.uploads) - before[1]})
    report = slo_report(scenario["id"], profile, [], object_transfers=measurements,
        enumeration_complexity="O(F)", scope="scale_path_only", delta_api_available=callable(getattr(store, "checkout_delta", None)))
    if mode == "full":
        assert all(row["downloaded"] == scenario["targets"]["downloads_per_turn"] for row in measurements), report
        assert all(row["uploaded"] == scenario["targets"]["uploads_per_turn"] for row in measurements), report
    else:
        require_target(callable(getattr(store, "checkout_delta", None)), report)
        require_target(measurements[-1]["downloaded"] <= scenario["targets"]["downloads_warm_max"], report)
