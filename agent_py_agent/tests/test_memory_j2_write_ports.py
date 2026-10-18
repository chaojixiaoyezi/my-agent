"""J2原向量/缓存/lesson/HOT逐末口、真实partial和上层原catch；不调用模型/网络。"""
from __future__ import annotations

import json
from contextlib import contextmanager
from dataclasses import replace

import pytest

from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common import json_io
from agent_py_agent.agent.common.global_write_guard import GlobalWriteInWorkerError
from agent_py_agent.agent.common.owner_write_errors import (
    LocalControlCoordinatorUnavailable,
    OwnerManagementIngressUnavailable,
)
from agent_py_agent.agent.memory_store import lessons as lesson_module
from agent_py_agent.agent.memory_store import promotion
from agent_py_agent.agent.memory_store.lessons import (
    HOT_MAX_INJECT_CHARS,
    HotRuleRepository,
    LessonRepository,
    LessonRoutingPendingError,
    _render_hot,
)
from agent_py_agent.agent.retrieval import text_vector_cache as cache_module
from agent_py_agent.agent.retrieval import vector_store as vector_module
from agent_py_agent.agent.retrieval.text_vector_cache import TextVectorCache
from agent_py_agent.agent.retrieval.vector_store import VectorIdentityError, VectorStore
from agent_py_agent.tests.test_hot_budget import _hot_candidate, _hot_pile, _lesson
from agent_py_agent.tests.test_local_store_coordinator_io import Door
from agent_py_agent.tests.test_memory_j1_write_ports import promotion_resources

ERRORS = [commits.GlobalWriteCommitUnavailable, GlobalWriteInWorkerError,
          LocalControlCoordinatorUnavailable, OwnerManagementIngressUnavailable]
IDENTITY = {"profile": "original-test-space", "model": "test-dim-2"}


# LLM: 只从已有原schema测试候选生成不同正式目标，不手写业务成功状态或替代dispatcher。
# 函数用途: 构造会走原LessonRepository验证/文件算法的已审核测试输入。
def lesson_candidate():
    return replace(_hot_candidate(content="先验证准确失败，再核对正式写入。"),
        candidate_id="candidate-lesson-j2", candidate_type="lesson", promotion_target="lesson")


# LLM: 真实向量仓库与原global scope，fake仅外层同步末门；种子同样通过真实C末写。
# 函数用途: 建立原快照，供逐锁出生和raw末写撤权测试。
def vector_resources(tmp_path):
    store = VectorStore(tmp_path / "memory-vectors.json", IDENTITY, coordinator_io=True)
    door = Door()
    with commits.global_write_commit_scope(door):
        store.upsert("old", [1.0, 0.0], text="旧事实")
    return store, door


@pytest.mark.parametrize("error_type", ERRORS)
@pytest.mark.parametrize("offset", [1, 2])
@pytest.mark.parametrize("operation", ["upsert", "replace", "remove"])
def test_vector_each_birth_raw_writer_same_native_no_false_publication(tmp_path, error_type, offset, operation):
    store, door = vector_resources(tmp_path)
    before = store._path.read_bytes()
    header, items, stamp = store._header, store._items, store._stamp
    door.error = error_type("actual source rejected")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        if operation == "upsert":
            store.upsert("new", [0.0, 1.0])
        elif operation == "replace":
            store.replace_all([("new", [0.0, 1.0], "新事实", {})])
        else:
            store.remove("old")
    assert caught.value is door.error
    assert door.calls == door.reject_at
    assert store._path.read_bytes() == before
    assert store._header is header and store._items is items and store._stamp == stamp


def test_vector_original_identity_dimensions_reload_and_no_change_writes(tmp_path):
    store, door = vector_resources(tmp_path)
    other = VectorStore(store._path, IDENTITY, coordinator_io=True)
    with commits.global_write_commit_scope(door):
        other.upsert("other", [0.0, 1.0])
        before = door.calls
        assert store.remove("missing") is False
        assert door.calls == before + 1  # 原锁出生；没有raw writer。
        assert store.ids() == ["old", "other"]
        store.upsert_many([])
        assert door.calls == before + 1
        with pytest.raises(VectorIdentityError, match="VECTOR_DIMENSION_MISMATCH"):
            store.upsert("bad", [1.0])
    assert store.ids() == ["old", "other"]
    assert VectorStore(store._path, {"profile": "different"}).identity_status() == (False, "VECTOR_IDENTITY_MISMATCH")


def test_vector_raw_and_algorithms_keep_original_bytes_outside_root(tmp_path, monkeypatch):
    store, door = vector_resources(tmp_path)
    functions = {name: getattr(vector_module, name) for name in ["_rows_dim", "_item"]}
    for name, original in functions.items():
        def checked(*args, _original=original, **kwargs):
            assert not door.inside
            return _original(*args, **kwargs)
        monkeypatch.setattr(vector_module, name, checked)
    dumps = vector_module.json.dumps
    def encode(*args, **kwargs):
        assert not door.inside
        return dumps(*args, **kwargs)
    monkeypatch.setattr(vector_module.json, "dumps", encode)
    with commits.global_write_commit_scope(door):
        store.upsert("new", [0.0, 1.0], text="正式数据", metadata={"owner": "same"})
    default = VectorStore(tmp_path / "default.json", IDENTITY)
    default.upsert_many([("old", [1.0, 0.0], "旧事实", {}), ("new", [0.0, 1.0], "正式数据", {"owner": "same"})])
    actual, expected = json.loads(store._path.read_bytes()), json.loads(default._path.read_bytes())
    for payload in [actual, expected]:
        payload.pop("generation")
        payload.pop("written_at")
    assert actual == expected


# LLM: 同一原缓存实例、原scope和真实文件，保原items/last_error，不造pending账或新缓存替身。
# 函数用途: 准备跨实例缓存写入与失败的准确盘上事实。
def cache_resources(tmp_path):
    cache = TextVectorCache(tmp_path / "text-cache.json", coordinator_io=True)
    door = Door()
    with commits.global_write_commit_scope(door):
        cache.put({"old": [1.0, 0.0]})
    return cache, door


@pytest.mark.parametrize("error_type", ERRORS)
@pytest.mark.parametrize("offset", [1, 2, 3])
@pytest.mark.parametrize("operation", ["put", "remove", "retain"])
def test_cache_each_birth_mkdir_raw_native_never_success_or_durable_cache_hit(tmp_path, error_type, offset, operation):
    cache, door = cache_resources(tmp_path)
    before = cache._path.read_bytes()
    original_id = id(cache)
    door.error = error_type("actual cache source rejected")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        if operation == "put":
            cache.put({"new": [0.0, 1.0]})
        elif operation == "remove":
            cache.remove(["old"])
        else:
            cache.retain_matching(lambda key: False)
    assert caught.value is door.error and cache.last_write_error is door.error
    assert cache._path.read_bytes() == before and id(cache) == original_id
    assert door.calls == door.reject_at
    # 原_read_stamp=None使get重核盘上事实；内存delta不当作耐久写入凭据。
    assert cache.get(["old", "new"]) == {"old": [1.0, 0.0]}


def test_cache_cross_instance_delta_delete_no_resurrection_no_unchanged_write(tmp_path):
    first, door = cache_resources(tmp_path)
    second = TextVectorCache(first._path, coordinator_io=True)
    with commits.global_write_commit_scope(door):
        second.put({"second": [0.0, 1.0]})
        first.remove(["second"])
        second.put({"third": [1.0, 1.0]})
        assert json.loads(first._path.read_bytes()) == {"old": [1.0, 0.0], "third": [1.0, 1.0]}
        for action in [lambda: first.put({"old": [1.0, 0.0]}), lambda: first.remove(["missing"]),
                       lambda: first.retain_matching(lambda key: True)]:
            before = door.calls
            action()
            assert door.calls == before + 1
    before = door.calls
    assert first.get(["third"]) == {"third": [1.0, 1.0]}
    assert door.calls == before


def test_cache_keep_original_twice_in_locks_and_json_outside_root(tmp_path, monkeypatch):
    cache, door = cache_resources(tmp_path)
    locked = False
    lock = json_io.locked_json_path
    @contextmanager
    def observe_lock(*args, **kwargs):
        nonlocal locked
        assert not door.inside
        with lock(*args, **kwargs):
            locked = True
            try:
                yield
            finally:
                locked = False
    monkeypatch.setattr(json_io, "locked_json_path", observe_lock)
    states = []
    def keep():
        assert cache._lock._is_owned() and not door.inside
        states.append(locked)
        return {"new"}
    dumps = cache_module.json.dumps
    def encode(*args, **kwargs):
        assert not door.inside
        return dumps(*args, **kwargs)
    monkeypatch.setattr(cache_module.json, "dumps", encode)
    with commits.global_write_commit_scope(door):
        cache.put({"new": [0.0, 1.0], "excluded": [1.0, 0.0]}, keep=keep)
    assert states == [False, True]
    assert "excluded" not in json.loads(cache._path.read_bytes())


@pytest.mark.parametrize("operation", ["put", "retain"])
def test_cache_ordinary_oserror_keeps_old_false_diagnostic(tmp_path, monkeypatch, operation):
    cache, door = cache_resources(tmp_path)
    before = cache._path.read_bytes()
    def fail(*args, **kwargs):
        assert door.inside and kwargs["replace_once"] is True
        raise PermissionError("ordinary local write failure")
    monkeypatch.setattr(json_io, "write_private_text_file_atomic_unlocked", fail)
    with commits.global_write_commit_scope(door):
        if operation == "put":
            assert cache.put({"new": [0.0, 1.0]}) is None
        else:
            assert cache.retain_matching(lambda key: False) == 0
    assert isinstance(cache.last_write_error, OSError)
    assert cache._path.read_bytes() == before


@pytest.mark.parametrize("offset", [1, 2])
def test_lesson_constructor_exact_directory_birth_partial(tmp_path, offset):
    door = Door()
    door.reject_at = offset
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        LessonRepository(tmp_path / "memory/lessons", tmp_path / "memory/routing/INDEX.md", coordinator_io=True)
    assert caught.value is door.error
    assert (tmp_path / "memory/lessons").exists() is (offset == 2)
    assert not (tmp_path / "memory/routing").exists()


def test_hot_constructor_exact_directory_birth(tmp_path):
    door = Door()
    door.reject_at = 1
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        HotRuleRepository(tmp_path / "memory/hot.md", coordinator_io=True)
    assert caught.value is door.error
    assert not (tmp_path / "memory").exists()


# LLM: 正文和routing分别真实落盘，fake只外层末门；partial从原文件读取，不补业务产物。
# 函数用途: 建立同一个原lesson仓库，验证逐出生/末写撤权。
def lesson_resources(tmp_path):
    door = Door()
    with commits.global_write_commit_scope(door):
        repo = LessonRepository(tmp_path / "memory/lessons", tmp_path / "memory/routing/INDEX.md", coordinator_io=True)
    return repo, door


@pytest.mark.parametrize("error_type", ERRORS)
@pytest.mark.parametrize("offset", [1, 2, 3, 4])
def test_lesson_body_then_routing_each_source_failure_preserves_exact_partial(tmp_path, error_type, offset):
    repo, door = lesson_resources(tmp_path)
    door.error = error_type("actual lesson source rejected")
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        repo.promote_with_receipt(lesson_candidate())
    assert caught.value is door.error and door.calls == door.reject_at
    assert bool(list(repo.lessons_dir.glob("*.md"))) is (offset >= 3)
    assert not repo.routing_index_path.exists()
    if offset >= 3:
        prior = next(repo.lessons_dir.glob("*.md")).read_bytes()
        door.reject_at = None
        with commits.global_write_commit_scope(door):
            _record, changed = repo.promote_with_receipt(lesson_candidate())
        assert changed is False and repo.routing_index_path.exists()
        assert next(repo.lessons_dir.glob("*.md")).read_bytes() == prior


def test_lesson_ordinary_routing_failure_still_original_pending(tmp_path, monkeypatch):
    repo, door = lesson_resources(tmp_path)
    write = lesson_module.write_private_text_file_atomic_unlocked
    failure = OSError("ordinary routing disk failure")
    def fail_routing(path, *args, **kwargs):
        if path == repo.routing_index_path:
            raise failure
        return write(path, *args, **kwargs)
    monkeypatch.setattr(lesson_module, "write_private_text_file_atomic_unlocked", fail_routing)
    with commits.global_write_commit_scope(door), pytest.raises(LessonRoutingPendingError) as caught:
        repo.promote_with_receipt(lesson_candidate())
    assert caught.value.__cause__ is failure and caught.value.formal_memory_changed is True
    assert repo.list() and not repo.routing_index_path.exists()


# LLM: 原HOT过量输入只写临时夹具，实际promote/demote用原算法和scope，不能禁第二次治理写。
# 函数用途: 创建触发原HOT第二末写的真实已有文件。
def hot_resources(tmp_path):
    door = Door()
    with commits.global_write_commit_scope(door):
        repo = HotRuleRepository(tmp_path / "memory/hot.md", coordinator_io=True)
    repo.path.write_text(_render_hot(_hot_pile()), encoding="utf-8")
    return repo, door


@pytest.mark.parametrize("error_type", ERRORS)
@pytest.mark.parametrize("offset", [1, 2, 3])
def test_hot_first_write_then_second_demote_source_failure(tmp_path, error_type, offset):
    repo, door = hot_resources(tmp_path)
    before = repo.path.read_bytes()
    door.error = error_type("actual HOT source rejected")
    door.reject_at = door.calls + offset
    candidate = _hot_candidate(content="新短规则必须保留。")
    with commits.global_write_commit_scope(door), pytest.raises(error_type) as caught:
        repo.promote_with_receipt(candidate, lesson=_lesson())
    assert caught.value is door.error and door.calls == door.reject_at
    if offset < 3:
        assert repo.path.read_bytes() == before
    else:
        assert repo.path.read_bytes() != before
        assert any(row.candidate_id == candidate.candidate_id for row in repo.list())
        assert sum(len(row.rule) for row in repo.list()) > HOT_MAX_INJECT_CHARS
        door.reject_at = None
        with commits.global_write_commit_scope(door):
            assert repo.demote_cold_rules()
        assert sum(len(row.rule) for row in repo.list()) <= HOT_MAX_INJECT_CHARS


@pytest.mark.parametrize("offset", [1, 2])
def test_hot_standalone_demote_same_native_before_or_after_birth(tmp_path, offset):
    repo, door = hot_resources(tmp_path)
    before = repo.path.read_bytes()
    door.reject_at = door.calls + offset
    with commits.global_write_commit_scope(door), pytest.raises(LocalControlCoordinatorUnavailable) as caught:
        repo.demote_cold_rules()
    assert caught.value is door.error and repo.path.read_bytes() == before


def test_lesson_hot_render_validation_budget_are_outside_root(tmp_path, monkeypatch):
    repo, door = lesson_resources(tmp_path)
    for name in ["_validate_lesson_candidate", "_render_lesson", "_render_routing_index", "_read_lesson"]:
        original = getattr(lesson_module, name)
        def checked(*args, _original=original, **kwargs):
            assert not door.inside
            return _original(*args, **kwargs)
        monkeypatch.setattr(lesson_module, name, checked)
    with commits.global_write_commit_scope(door):
        record, changed = repo.promote_with_receipt(lesson_candidate())
        assert changed
    hot, hot_door = hot_resources(tmp_path / "hot")
    for name in ["_validate_hot_candidate", "_render_hot", "_read_hot_records", "_hot_activity_key"]:
        original = getattr(lesson_module, name)
        def checked(*args, _original=original, **kwargs):
            assert not hot_door.inside
            return _original(*args, **kwargs)
        monkeypatch.setattr(lesson_module, name, checked)
    candidate = replace(_hot_candidate(content="新规则"), target_entry_id=record.lesson_id)
    with commits.global_write_commit_scope(hot_door):
        hot.promote_with_receipt(candidate, lesson=record)
    assert sum(len(row.rule) for row in hot.list()) <= HOT_MAX_INJECT_CHARS


@pytest.mark.parametrize("resource", ["vector", "cache", "lesson", "hot"])
def test_explicit_c_missing_scope_rejects_without_default_fallback(tmp_path, resource):
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        if resource == "vector":
            VectorStore(tmp_path / "vectors.json", coordinator_io=True).upsert("id", [1.0])
        elif resource == "cache":
            TextVectorCache(tmp_path / "cache.json", coordinator_io=True).put({"key": [1.0]})
        elif resource == "lesson":
            LessonRepository(tmp_path / "lessons", tmp_path / "routing/INDEX.md", coordinator_io=True)
        else:
            HotRuleRepository(tmp_path / "hot.md", coordinator_io=True)


def test_default_three_resources_never_call_new_global_guard(tmp_path, monkeypatch):
    def forbidden(*args):
        raise AssertionError("old default entered new global guard")
    monkeypatch.setattr(commits, "require_global_writer", forbidden)
    vectors = VectorStore(tmp_path / "vectors.json")
    vectors.upsert("a", [1.0])
    assert vectors.remove("a")
    cache = TextVectorCache(tmp_path / "cache.json")
    cache.put({"a": [1.0]})
    cache.remove(["a"])
    lessons = LessonRepository(tmp_path / "memory/lessons", tmp_path / "memory/routing/INDEX.md")
    record = lessons.promote(lesson_candidate())
    hot = HotRuleRepository(tmp_path / "hot.md")
    hot.promote(replace(_hot_candidate(content="原HOT"), target_entry_id=record.lesson_id), lesson=record)


@pytest.mark.parametrize("target", ["lesson", "hot"])
def test_actual_promotion_valueerror_handler_preserves_ingress_object(tmp_path, target):
    service, _quota, door = promotion_resources(tmp_path)
    repo, _repo_door = lesson_resources(tmp_path / "actual")
    service.lessons = repo
    with commits.global_write_commit_scope(door):
        lesson = repo.promote(lesson_candidate())
        service.hot = HotRuleRepository(tmp_path / "actual/hot.md", coordinator_io=True)
    candidate = replace(lesson_candidate(), candidate_id="next-lesson") if target == "lesson" else replace(
        _hot_candidate(content="原HOT"), target_entry_id=lesson.lesson_id)
    error = OwnerManagementIngressUnavailable("actual owner revoked")
    door.error = error
    door.reject_at = door.calls + 1
    context = promotion._PromotionWriteContext("test", False, None)
    with commits.global_write_commit_scope(door), pytest.raises(OwnerManagementIngressUnavailable) as caught:
        promotion._commit_target_blocked(service, candidate, context)
    assert caught.value is error


def test_default_valueerror_handler_retains_original_threshold_result(tmp_path):
    service, _quota, _door = promotion_resources(tmp_path)
    service.coordinator_io = False
    candidate = replace(lesson_candidate(), occurrence_count=0)
    with pytest.raises(promotion._PromotionBlocked) as caught:
        promotion._commit_target_blocked(service, candidate, promotion._PromotionWriteContext("test", False, None))
    assert caught.value.reason_code == "PROMOTION_THRESHOLD_NOT_MET"


@pytest.mark.parametrize("resource", ["vector", "cache", "lesson", "hot"])
def test_expired_original_scope_cannot_be_reused_by_same_resource(tmp_path, resource):
    from contextvars import copy_context

    door = Door()
    with commits.global_write_commit_scope(door):
        if resource == "vector":
            obj = VectorStore(tmp_path / "vectors.json", coordinator_io=True)
            def action():
                return obj.upsert("id", [1.0])
        elif resource == "cache":
            obj = TextVectorCache(tmp_path / "cache.json", coordinator_io=True)
            def action():
                return obj.put({"key": [1.0]})
        elif resource == "lesson":
            obj = LessonRepository(tmp_path / "lessons", tmp_path / "routing/INDEX.md", coordinator_io=True)
            def action():
                return obj.promote(lesson_candidate())
        else:
            obj = HotRuleRepository(tmp_path / "hot.md", coordinator_io=True)
            def action():
                return obj.promote(_hot_candidate(content="原HOT"), lesson=_lesson())
        late = copy_context()
    before = door.calls
    with pytest.raises(commits.GlobalWriteCommitUnavailable):
        late.run(action)
    assert door.calls == before


def test_cache_ordinary_error_code_text_is_not_upgraded_to_native(tmp_path):
    cache, door = cache_resources(tmp_path)
    failure = RuntimeError("ordinary error, GLOBAL_WRITE_REJECTED text is not authority")
    door.error = failure
    door.reject_at = door.calls + 1
    with commits.global_write_commit_scope(door):
        assert cache.put({"new": [0.0, 1.0]}) is None
    assert cache.last_write_error is failure
    assert json.loads(cache._path.read_bytes()) == {"old": [1.0, 0.0]}
