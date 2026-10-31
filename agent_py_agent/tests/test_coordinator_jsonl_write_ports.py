"""J3实际JSONL固定末口、native传播、原费用边界和partial；不调用网络或模型。"""
from __future__ import annotations

import json
from contextlib import nullcontext

import pytest

from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from agent_py_agent.agent.common.request_execution_ports import (
    RequestExecutionContextUnavailable,
    RequestExecutionOutcomeUncertainError,
)
from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.memory_store import _jsonl_indexing as bridge
from agent_py_agent.agent.memory_store import jsonl
from agent_py_agent.agent.memory_store.candidates import CandidateService
from agent_py_agent.agent.memory_store.jsonl import (
    JsonlMemory,
    JsonlMemoryDependencies,
    MemoryRecord,
)
from agent_py_agent.agent.retrieval.text_vector_cache import embedder_fingerprint, text_cache_key
from agent_py_agent.tests.test_local_store_coordinator_io import Door
from agent_py_agent.tests.test_memory_j1_write_ports import observation

SOURCE_TYPES = [commits.GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
                LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable]


# LLM: fake只限外部provider；同原embed API/记录调用量，不替换Jsonl/LocalStore/身份/业务状态机。
# 类用途: 测试原付费边界的零重试与Root外调用，绝不发网络。
class Embedder:
    api_base = "https://embedding.example.invalid"
    model = "test-j3"
    dim = 2

    def __init__(self, door=None, error=None):
        self.calls = []
        self.door = door
        self.error = error

    def embed(self, texts):
        if self.door is not None:
            assert not self.door.inside
        self.calls.append(list(texts))
        if self.error is not None:
            raise self.error
        return [[1.0, 0.0] for _ in texts]


# LLM: 全部真实原领域对象，fake只有原外层同步门与外部embedding，原构造/目录/SQLite都实际执行。
# 函数用途: 建立同一scope内的C JSONL闭包，供逐阶段故障核验。
def memory(tmp_path, *, local=False, ops=False, semantic=False, candidates=False):
    door = Door()
    embedder = Embedder(door) if semantic else None
    with commits.global_write_commit_scope(door):
        store = LocalStore(tmp_path / "index.db", coordinator_io=True) if local else None
        service = CandidateService(tmp_path / "candidates.jsonl", coordinator_io=True) if candidates else None
        result = JsonlMemory.from_dependencies(tmp_path / "memory.jsonl", JsonlMemoryDependencies(
            local_store=store, ops_path=tmp_path / "ops.jsonl" if ops else None,
            candidate_service=service, embedder=embedder, coordinator_io=True))
    return result, door, embedder


@pytest.mark.parametrize("error_type", SOURCE_TYPES)
@pytest.mark.parametrize("offset", [1, 2])
def test_formal_lock_or_raw_failure_preserves_full_pending_same_object(tmp_path, error_type, offset):
    mem, door, _embedder = memory(tmp_path)
    mem._pending_access = {"inactive": 1.0}
    door.error = error_type("actual source rejection")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        mem.add("user", "not durable")
    assert caught.value is door.error
    assert mem._pending_access == {"inactive": 1.0} and not mem.path.exists()


def test_formal_success_clears_only_equal_old_snapshot_and_newer_touch_survives(tmp_path, monkeypatch):
    mem, door, _embedder = memory(tmp_path)
    with commits.global_write_commit_scope(door):
        record = mem.add("user", "durable")
    mem._pending_access = {record.entry_id: 1.0, "inactive": 2.0}
    write = jsonl.write_private_text_file_atomic_unlocked
    def change_pending(path, raw, **kwargs):
        assert mem._pending_access == {record.entry_id: 1.0, "inactive": 2.0}
        assert door.inside and kwargs["replace_once"] is True
        mem._pending_access[record.entry_id] = 3.0
        return write(path, raw, **kwargs)
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", change_pending)
    with commits.global_write_commit_scope(door):
        mem.add("user", "next durable")
    assert mem._pending_access == {record.entry_id: 3.0}
    assert mem.all()[0].last_accessed_at == 1.0


def test_ordinary_formal_permission_failure_keeps_pending_no_retry(tmp_path, monkeypatch):
    mem, door, _embedder = memory(tmp_path)
    mem._pending_access = {"inactive": 1.0}
    error = PermissionError("ordinary disk refusal")
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise error
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", fail)
    with commits.global_write_commit_scope(door), pytest.raises(PermissionError) as caught:
        mem.add("user", "must not commit")
    assert caught.value is error and calls == [{"replace_once": True}]
    assert mem._pending_access == {"inactive": 1.0}


# LLM: 788f9b847起正式写入后不再写旧LocalStore文本投影；正式提交后的下一原末门拒绝仍须在付费embedding前同对象抛出。
# 函数用途: 验证正式partial保留、旧SQL投影未被触达且embedding零调用。
@pytest.mark.parametrize("error_type", SOURCE_TYPES)
def test_actual_post_formal_native_stops_before_next_embedding_with_formal_partial(tmp_path, monkeypatch, error_type):
    mem, door, embedder = memory(tmp_path, local=True, ops=True, semantic=True)
    error = error_type("actual post-formal source rejected")
    sql_calls = []
    original = mem.local_store._coordinator_transaction
    def record_transaction(operation, **kwargs):
        sql_calls.append(operation)
        return original(operation, **kwargs)
    monkeypatch.setattr(mem.local_store, "_coordinator_transaction", record_transaction)
    write = jsonl.write_private_text_file_atomic_unlocked
    def reject_after_formal(path, raw, **kwargs):
        result = write(path, raw, **kwargs)
        if path == mem.path:
            # 正式正文已落盘，下一次原末门拒绝。
            door.error = error
            door.reject_at = door.calls + 1
        return result
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", reject_after_formal)
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        mem.add("user", "formal before index")
    assert caught.value is error and embedder.calls == [] and sql_calls == []
    assert mem.all()[0].content == "formal before index"


# LLM: 正式提交成功后新发生的撤权由document embedding前的原当前核验拦下，不付费、不回滚正式事实。
# 函数用途: 验证正式成功后的撤权在付费embedding之前生效。
def test_new_revocation_after_formal_success_is_checked_before_document_embedding(tmp_path, monkeypatch):
    mem, door, embedder = memory(tmp_path, local=True, semantic=True)
    write = jsonl.write_private_text_file_atomic_unlocked
    def revoke_after_success(path, raw, **kwargs):
        result = write(path, raw, **kwargs)
        if path == mem.path:
            door.reject_at = door.calls + 1
        return result
    monkeypatch.setattr(jsonl, "write_private_text_file_atomic_unlocked", revoke_after_success)
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        mem.add("user", "previous formal success")
    assert caught.value is door.error and not embedder.calls
    assert mem.all()[0].content == "previous formal success"


def test_query_embedding_current_check_precedes_provider_and_search_not_retried(tmp_path):
    mem, door, embedder = memory(tmp_path, semantic=True)
    with commits.global_write_commit_scope(door):
        mem.add("user", "needle current fact")
    embedder.calls.clear()
    door.reject_at = door.calls + 1
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        mem.search("needle")
    assert caught.value is door.error and not embedder.calls and not mem._pending_access


@pytest.mark.parametrize("operation", ["document", "query"])
def test_actual_physical_unknown_from_provider_keeps_same_object_and_no_replay(tmp_path, operation):
    mem, door, embedder = memory(tmp_path, semantic=True)
    if operation == "query":
        with commits.global_write_commit_scope(door):
            mem.add("user", "needle fact")
        embedder.calls.clear()
    error = BoundedCallExitUnconfirmedError("original-call", "test exit not confirmed")
    cause = OSError("original physical cause")
    error.__cause__ = cause
    embedder.error = error
    with commits.global_write_commit_scope(door), pytest.raises(BoundedCallExitUnconfirmedError) as caught:
        if operation == "document":
            mem.add("user", "already formal")
        else:
            mem.search("needle")
    assert caught.value is error and caught.value.__cause__ is cause
    assert len(embedder.calls) == 1


# LLM: 只构造既有canonical错误协议，request字段是异常身份数据，不当作claim或执行许可。
# 函数用途: 枚举原scope与原实际transport可能抛的真实异常对象。
def actual_errors():
    request = {"id": "original", "execution_attempt_id": "attempt", "lease_epoch": 1, "lease_owner": "owner"}
    return [*(kind("source rejected") for kind in SOURCE_TYPES),
        RequestExecutionContextUnavailable("context missing"), RequestExecutionOutcomeUncertainError(request, "commit"),
        ToolWriteContextUnavailable("tool source missing"), BoundedCallExitUnconfirmedError("call", "unknown")]


@pytest.mark.parametrize("error", actual_errors(), ids=lambda e: type(e).__name__)
def test_shared_removal_phase_never_wraps_actual_native_or_physical(error):
    cause = OSError("original cause")
    error.__cause__ = cause
    with pytest.raises(type(error)) as caught:
        with bridge.removal_cleanup_phase(formal_memory_changed=True):
            raise error
    assert caught.value is error and caught.value.__cause__ is cause


def test_shared_removal_phase_keeps_original_ordinary_io_partial_contract():
    error = OSError("ordinary IO")
    with pytest.raises(bridge.RemovalCleanupError) as caught:
        with bridge.removal_cleanup_phase(formal_memory_changed=True):
            raise error
    assert caught.value.formal_memory_changed is True and caught.value.__cause__ is error
    with pytest.raises(OSError) as caught:
        with bridge.removal_cleanup_phase(formal_memory_changed=False, enabled=False):
            raise error
    assert caught.value is error


@pytest.mark.parametrize("fault", ["vector", "cache", "local"])
def test_actual_delete_cleanup_native_preserves_formal_and_not_fake_pending(tmp_path, monkeypatch, fault):
    mem, door, embedder = memory(tmp_path, local=True, ops=True, semantic=True, candidates=True)
    with commits.global_write_commit_scope(door):
        record = mem.add("user", "private cleanup content")
        mem.candidate_service.observe(observation("private cleanup content"))
        cache = mem._text_vector_cache()
        key = text_cache_key(embedder_fingerprint(embedder), record.content)
        cache.put({key: [1.0, 0.0]})
    error = OwnerManagementIngressUnavailable("actual cleanup source rejected")
    if fault == "vector":
        from agent_py_agent.agent.retrieval import vector_store
        module = vector_store
    elif fault == "cache":
        from agent_py_agent.agent.retrieval import text_vector_cache
        module = text_vector_cache
    else:
        module = None
    if module is not None:
        # 原真正unlocked末writer抛出受控实际类，不能被bridge包装成RemovalCleanupError。
        if fault == "vector":
            def fail(*args, **kwargs):
                raise error
            monkeypatch.setattr(module, "write_private_text_file_atomic_unlocked", fail)
        else:
            original = mem._text_vector_cache_obj._flush
            # retain_matching原真正writer在common导入，按唯一cache path注入。
            from agent_py_agent.agent.common import json_io
            write = json_io.write_private_text_file_atomic_unlocked
            def fail(path, *args, **kwargs):
                if path == mem._text_vector_cache_obj._path:
                    raise error
                return write(path, *args, **kwargs)
            monkeypatch.setattr(json_io, "write_private_text_file_atomic_unlocked", fail)
            assert callable(original)
    else:
        original = mem.local_store.delete_record
        def reject(record_id):
            door.error = error
            door.reject_at = door.calls + 1
            return original(record_id)
        monkeypatch.setattr(mem.local_store, "delete_record", reject)
    with commits.global_write_commit_scope(door), pytest.raises(OwnerManagementIngressUnavailable) as caught:
        mem.remove(record.entry_id)
    assert caught.value is error and not mem.all()
    assert not isinstance(caught.value, bridge.RemovalCleanupError)
    assert all(not row["content"] for row in (json.loads(line) for line in mem.candidate_service.path.read_text().splitlines()))


def test_no_embedder_delete_uses_same_c_vector_and_text_cache_objects(tmp_path):
    mem, door, _embedder = memory(tmp_path)
    with commits.global_write_commit_scope(door):
        record = mem.add("user", "old plaintext")
        cache = mem._text_vector_cache_for_reclaim()
        cache.put({"fingerprint:" + "a" * 64: [1.0, 0.0]})
        assert mem._text_vector_cache_for_reclaim() is cache
        mem.remove(record.entry_id)
        vector = mem._vector_store_cache
        assert vector.coordinator_io and cache.coordinator_io
        # 第二次原清理不新建另一个实例；无embedder仍原子清除旧磁盘缓存。
        mem.purge_removed_indexes(record.entry_id)
    assert mem._vector_store_cache is vector and mem._text_vector_cache_obj is cache
    assert json.loads(cache._path.read_bytes()) == {}


# LLM: 788f9b847起condense只按字符预算转冷热层，旧archive+删除已退役；正文超预算才发表新代次，拒绝落在根发表之前。
# 函数用途: 构造超过热库字符预算的旧单文件正式记忆，返回原记录。
def _over_budget_records(mem):
    records = [MemoryRecord("user", f"fact-{i}:" + "x" * 3000, entry_id="entry-" + str(i), created_at=i + 1,
        attributes={"origin": "user_explicit" if i == 0 else "reviewed"}) for i in range(8)]
    assert sum(len(row.content) for row in records) > mem.memory_policy().active_library_max_chars
    mem.path.write_text("".join(row.to_json() + "\n" for row in records), encoding="utf-8")
    return records


# LLM: 冷热分片写入期间的原末门拒绝必须同对象抛出；旧权威文件、可见事实不变，不发表根也不写旧archive。
# 函数用途: 验证代次根发表前失败的准确partial。
@pytest.mark.parametrize("offset", [1, 2, 3])
def test_condense_tier_publish_before_root_failure_is_exact_partial(tmp_path, offset):
    mem, door, _embedder = memory(tmp_path)
    records = _over_budget_records(mem)
    before = mem.path.read_bytes()
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        mem.condense(max_records=4, target_records=2, max_ratio=0.25)
    assert caught.value is door.error and mem.path.read_bytes() == before
    assert not mem.path.with_name(mem.path.name + ".manifest.json").exists()
    assert not mem.path.with_name(mem.path.name + ".archive.jsonl").exists()
    assert [(row.entry_id, row.content) for row in mem.all()] == [(row.entry_id, row.content) for row in records]


# LLM: 旧ratio/条数参数不再授权删除，转冷不改版本；扫描读取仍在原末门之外进行。
# 函数用途: 验证condense只转层不遗忘，且读取不进入Root。
def test_condense_ignores_legacy_ratio_and_keeps_root_outside_scan(tmp_path, monkeypatch):
    mem, door, _embedder = memory(tmp_path)
    records = _over_budget_records(mem)
    reads = []
    for name in ("all", "active_records"):
        original = getattr(mem, name)
        def read(original=original, name=name):
            assert not door.inside
            reads.append(name)
            return original()
        monkeypatch.setattr(mem, name, read)
    with commits.global_write_commit_scope(door):
        moved = mem.condense(max_records=4, target_records=2, max_ratio=0.25)
    assert reads and moved and door.calls
    assert not mem.path.with_name(mem.path.name + ".archive.jsonl").exists()
    assert {row.entry_id for row in moved} < {row.entry_id for row in mem.all()}
    assert [(row.entry_id, row.version) for row in mem.all()] == [(row.entry_id, 1) for row in records]


def test_default_jsonl_retains_original_virtual_hooks_and_no_new_global_guard(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("default JSONL used new C global guard")
    monkeypatch.setattr(commits, "require_global_writer", forbidden)
    mem = JsonlMemory(tmp_path / "memory.jsonl")
    first = mem.add("user", "default original")
    mem.search("original")
    mem.flush_access_events()
    mem.remove(first.entry_id)
    assert not mem.all()


def test_explicit_c_constructor_missing_source_no_io(tmp_path):
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        JsonlMemory.from_dependencies(tmp_path / "new/memory.jsonl", JsonlMemoryDependencies(coordinator_io=True))
    assert not (tmp_path / "new").exists()


def test_real_dependency_factory_preserves_references_before_io_and_default_constructor_shape(tmp_path, monkeypatch):
    import inspect

    path = tmp_path / "memory.jsonl"
    door = Door()
    embedder = Embedder(door)
    with commits.global_write_commit_scope(door):
        store = LocalStore(tmp_path / "real.db", coordinator_io=True)
        candidates = CandidateService(tmp_path / "candidates.jsonl", coordinator_io=True)
        dependency = JsonlMemoryDependencies(local_store=store, candidate_service=candidates,
            embedder=embedder, semantic_status={"state": "configured"}, vector_identity={"profile": "test"},
            coordinator_io=True)
        obj = JsonlMemory.from_dependencies(path, dependency)
    assert type(obj) is JsonlMemory and obj.local_store is store and obj.candidate_service is candidates
    assert obj._embedder is embedder and obj.coordinator_io is True
    assert obj._semantic_status == dependency.semantic_status and obj._semantic_status is not dependency.semantic_status
    assert obj._vector_identity == dependency.vector_identity and obj._vector_identity is not dependency.vector_identity
    assert "coordinator_io" not in inspect.signature(JsonlMemory.__init__).parameters
    assert not hasattr(obj, "dependencies") and not hasattr(obj, "source")


def test_explicit_dependency_factory_rejects_custom_subclass_or_wrong_bundle_before_io(tmp_path):
    class CustomMemory(JsonlMemory):
        pass

    with pytest.raises(TypeError):
        CustomMemory.from_dependencies(tmp_path / "subclass/memory.jsonl", JsonlMemoryDependencies(coordinator_io=True))
    with pytest.raises(TypeError):
        JsonlMemory.from_dependencies(tmp_path / "wrong/memory.jsonl", object())
    assert not list(tmp_path.iterdir())
