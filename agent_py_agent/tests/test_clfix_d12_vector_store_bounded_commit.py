"""D-12：长期记忆向量库单条提交的代价有界（不整份重写、常驻实例不整份重读），检索结果与持久性不变。

锁定：
- 大快照上只新增 id 的提交：不调用整份原子重写，文件 inode 不变、只多一条记录的字节；常驻实例只读新增字节，不再整份解析。
- 追加后的检索结果、ids 顺序与可复用向量，与同样内容的整份快照逐项相同；写入实例、常驻实例、新实例三方一致。
- 崩溃留下的半条记录不算已提交、不让库变成读不了；下一次追加先截掉它。写入失败时文件与实例状态都不变。
- 已有格式照常读：P14 前的整份 id→条目、旧代码写出的单行 v2 快照（新增 id 接在原字节后）、缩进多行的 v2 快照。
- 压实写完整 v3，保留全部活跃行；旧 v1/v2 仍可读取。
- 删除与覆盖仍整份写回，盘上不残留旧正文；追加段不超过预算，超了就整份压实；别代快照的记录不会被套到旧快照上。
- 非私有权限或符号链接的快照不追加，仍整份替换（收紧到 0600、不顺着链接写别处）；C 模式追加只过一次末写门。
全部用确定性向量和临时目录，不调真实嵌入、不碰真实 home。
"""
from __future__ import annotations

import json
import os
import random
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.common import global_write_commit as commits
from agent_py_agent.agent.common.owner_write_errors import LocalControlCoordinatorUnavailable
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval import vector_store
from agent_py_agent.agent.retrieval.embedding import l2_normalize
from agent_py_agent.agent.retrieval.text_vector_cache import index_text
from agent_py_agent.agent.retrieval.vector_store import VECTOR_STORE_SCHEMA_VERSION, VectorStore
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

_DIM = 512
_ID = embedding_identity("prof-d12", LocalHashingEmbedder(dim=_DIM))
# v3 基座要跨过追加门槛，且单条 JSON 记录仍小于基座的 1%。
_BASE_COUNT = 640
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位与符号链接语义")
# 新常量用 getattr 取：同一文件放到修复前的代码上跑时，各用例按缺陷本身失败，而不是统一死在属性缺失上。
_APPEND_SCHEMA = getattr(vector_store, "VECTOR_STORE_APPEND_SCHEMA_VERSION", "my-agent.memory-vectors.v2.append")
_MIN_BASE_BYTES = getattr(vector_store, "_VECTOR_APPEND_MIN_BASE_BYTES", 0)
_MAX_TAIL_PERCENT = getattr(vector_store, "_VECTOR_APPEND_MAX_TAIL_PERCENT", 25)


# 函数用途: 按种子生成一条 L2 归一的稠密确定性向量（与真实嵌入一样每维都是长小数，不同种子彼此近似正交）。
def _vector(seed: int) -> list[float]:
    generator = random.Random(seed)
    return l2_normalize([generator.uniform(-1.0, 1.0) for _index in range(_DIM)])


# 函数用途: 生成一批 (id, 向量, 正文, 元数据) 行。
def _rows(start: int, count: int) -> list[tuple[str, list[float], str, dict[str, object]]]:
    return [(f"mem-{index}", _vector(index), f"记忆正文 {index}", {"entry_id": f"mem-{index}", "content": f"记忆正文 {index}"})
            for index in range(start, start + count)]


# 函数用途: 用整份快照建一个超过追加门槛的向量库，返回建库实例。
def _large_store(path: Path, **kwargs) -> VectorStore:
    store = VectorStore(path, identity=_ID, **kwargs)
    store.replace_all(_rows(0, _BASE_COUNT))
    assert path.stat().st_size >= _MIN_BASE_BYTES
    return store


# 函数用途: 取一次检索的完整可观察结果（id、分数、正文、元数据）。
def _hits(store: VectorStore, query: list[float]) -> list[tuple[str, float, str, dict[str, object]]]:
    return [(hit.id, hit.score, hit.text, hit.metadata) for hit in store.search(query, top_k=10)]


# 函数用途: 生成一条与库内写法相同的追加记录字节。
def _append_record(generation: str, items: dict[str, dict[str, object]]) -> bytes:
    record = {"schema_version": _APPEND_SCHEMA, "generation": generation, "items": items}
    return ("\n" + json.dumps(record, ensure_ascii=False)).encode("utf-8")


# 类用途: 计数并可按次拒绝的同步末写门替身，只运输原操作。
class _Door:
    # 函数用途: 初始化调用计数与拒绝点。
    def __init__(self) -> None:
        self.calls = 0
        self.reject_at: int | None = None
        self.error: Exception = LocalControlCoordinatorUnavailable("injected_source_revoked")

    # 函数用途: 计一次门调用，到拒绝点时抛出注入错误，否则执行原操作。
    def __call__(self, operation):
        self.calls += 1
        if self.calls == self.reject_at:
            raise self.error
        return operation()


def test_single_new_record_commit_appends_without_rewrite_or_reparse(tmp_path, monkeypatch) -> None:
    path = tmp_path / "memory_vectors.json"
    _large_store(path)
    writer, resident = VectorStore(path, identity=_ID), VectorStore(path, identity=_ID)
    rewritten: list[int] = []
    parsed: list[int] = []
    tail_read: list[int] = []
    real_write, real_parse = vector_store._write_private_snapshot_unlocked, vector_store._parse_snapshot
    real_tail = getattr(vector_store, "_tail_records", None)

    def counting_write(target, content, **kwargs):
        rewritten.append(len(content))
        return real_write(target, content, **kwargs)

    def counting_parse(raw):
        parsed.append(len(raw))
        return real_parse(raw)

    def counting_tail(generation, data, start):
        tail_read.append(len(data) - start)
        return real_tail(generation, data, start)

    monkeypatch.setattr(vector_store, "_write_private_snapshot_unlocked", counting_write)
    monkeypatch.setattr(vector_store, "_parse_snapshot", counting_parse)
    monkeypatch.setattr(vector_store, "_tail_records", counting_tail, raising=False)
    before = path.stat()
    writer.upsert("mem-new", _vector(9_999), text="新记忆", metadata={"entry_id": "mem-new"})
    after = path.stat()

    assert rewritten == []  # 没有整份原子重写
    assert after.st_ino == before.st_ino  # 同一个文件，只是变长
    growth = after.st_size - before.st_size
    assert 0 < growth < before.st_size // 100  # 只多写了一条记录
    assert len(resident) == _BASE_COUNT + 1 and "mem-new" in resident.ids()
    assert parsed == []  # 常驻实例没有整份解析快照
    assert sum(tail_read) == growth  # 只读了新增的字节


def test_appended_store_searches_exactly_like_a_full_snapshot(tmp_path) -> None:
    path, plain = tmp_path / "memory_vectors.json", tmp_path / "plain" / "memory_vectors.json"
    _large_store(path)
    resident, writer = VectorStore(path, identity=_ID), VectorStore(path, identity=_ID)
    base = path.read_bytes()
    extra = _rows(10_000, 5)
    writer.upsert_many(extra[:1])
    writer.upsert_many(extra[1:2])
    writer.upsert_many(extra[2:])  # 一批多条也是一条追加记录
    assert path.read_bytes().startswith(base)  # 三批新增仍附在原快照之后
    reference = VectorStore(plain, identity=_ID)
    reference.replace_all(_rows(0, _BASE_COUNT) + extra)  # 同样内容的整份快照
    queries = [_vector(seed) for seed in (3, 250, 10_001, 10_004)]
    expected = [_hits(reference, query) for query in queries]

    for store in (writer, resident, VectorStore(path, identity=_ID)):
        assert store.ids() == reference.ids()
        assert [_hits(store, query) for query in queries] == expected
        assert store.stored_vectors(["mem-7", "mem-10003"]) == reference.stored_vectors(["mem-7", "mem-10003"])
        assert store.summary()["vector_count"] == _BASE_COUNT + 5
        assert store.identity_status() == (True, "")


def test_torn_tail_is_not_committed_and_next_append_repairs_it(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    _large_store(path)
    writer = VectorStore(path, identity=_ID)
    writer.upsert("mem-a", _vector(1_001), text="甲")
    resident = VectorStore(path, identity=_ID)
    generation = resident.summary()["meta"]["generation"]
    blob = _append_record(generation, {"mem-torn": {"vector": _vector(1_002), "text": "残尾", "metadata": {}}})
    with path.open("ab") as handle:
        handle.write(blob[: len(blob) // 2])  # 进程在写这条记录时崩溃
    assert b"mem-torn" in path.read_bytes()

    for store in (VectorStore(path, identity=_ID), resident, writer):
        assert store.identity_status() == (True, "")  # 残尾不让库变成读不了
        assert "mem-a" in store.ids() and "mem-torn" not in store.ids()
        assert [hit.id for hit in store.search(_vector(1_001), top_k=1)] == ["mem-a"]

    VectorStore(path, identity=_ID).upsert("mem-b", _vector(1_003), text="乙")
    assert b"mem-torn" not in path.read_bytes()  # 下一次追加先截掉残尾
    for store in (VectorStore(path, identity=_ID), resident, writer):
        assert {"mem-a", "mem-b"} <= set(store.ids()) and "mem-torn" not in store.ids()
        assert len(store) == _BASE_COUNT + 2


def test_failed_append_publishes_nothing(tmp_path, monkeypatch) -> None:
    path = tmp_path / "memory_vectors.json"
    store = _large_store(path)
    before = path.read_bytes()
    header, items, stamp = store._header, store._items, store._stamp

    def failing_fsync(descriptor):
        raise OSError("disk unavailable")

    monkeypatch.setattr(vector_store.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="disk unavailable"):
        store.upsert("mem-new", _vector(5), text="新")
    monkeypatch.undo()

    assert path.read_bytes() == before  # 写了一半的记录被截回
    assert store._header is header and store._items is items and store._stamp == stamp
    assert "mem-new" not in VectorStore(path, identity=_ID).ids()
    store.upsert("mem-new", _vector(5), text="新")  # 故障消失后照常写入
    assert "mem-new" in VectorStore(path, identity=_ID).ids()


def test_existing_snapshot_formats_keep_loading(tmp_path) -> None:
    legacy_path = tmp_path / "legacy" / "memory_vectors.json"
    legacy_path.parent.mkdir()
    legacy = {f"mem-{index}": {"vector": _vector(index), "text": f"t{index}", "metadata": {}} for index in range(3)}
    legacy_path.write_text(json.dumps(legacy), encoding="utf-8")  # P14 之前：整份就是 id→条目
    assert VectorStore(legacy_path, identity=_ID).identity_status() == (False, "VECTOR_META_MISSING")
    untyped = VectorStore(legacy_path)
    assert untyped.ids() == ["mem-0", "mem-1", "mem-2"]
    assert untyped.search(_vector(1), top_k=1)[0].id == "mem-1"

    header = {"schema_version": VECTOR_STORE_SCHEMA_VERSION, "identity": _ID, "dim": _DIM,
              "generation": "old-generation", "written_at": "2026-11-01T00:00:00+00:00"}
    items = {key: {"vector": vector, "text": text, "metadata": metadata} for key, vector, text, metadata in _rows(0, _BASE_COUNT)}
    path = tmp_path / "v2" / "memory_vectors.json"
    path.parent.mkdir()
    original = json.dumps({**header, "items": items}, ensure_ascii=False).encode("utf-8")  # 旧代码的写法
    path.write_bytes(original)
    os.chmod(path, 0o600)
    store = VectorStore(path, identity=_ID)
    assert store.identity_status() == (True, "") and len(store) == _BASE_COUNT
    store.upsert("mem-new", _vector(9_999), text="新")
    assert path.read_bytes().startswith(original)  # 原快照字节不动，新增 id 接在其后
    assert len(VectorStore(path, identity=_ID)) == _BASE_COUNT + 1

    pretty = tmp_path / "pretty" / "memory_vectors.json"
    pretty.parent.mkdir()
    pretty.write_text(json.dumps({**header, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(pretty, 0o600)
    store = VectorStore(pretty, identity=_ID)
    assert store.identity_status() == (True, "") and len(store) == _BASE_COUNT
    store.upsert("mem-new", _vector(9_999), text="新")  # 多行快照不追加：整份重写成单行快照
    loaded = VectorStore(pretty, identity=_ID)
    assert loaded.summary()["meta"]["v"] == 3 and len(loaded) == _BASE_COUNT + 1


def test_physical_removal_compacts_existing_tail(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    store = _large_store(path)
    store.upsert("mem-new", _vector(23), text="新行")
    assert store.remove("mem-3")
    loaded = VectorStore(path, identity=_ID)
    header = loaded.summary()["meta"]
    assert header["v"] == 3 and header["identity"] == _ID and header["dim"] == _DIM
    assert "mem-new" in loaded.ids() and "mem-3" not in loaded.ids() and len(loaded) == _BASE_COUNT
    assert loaded._tail[0] == loaded._tail[1] == path.stat().st_size


def test_removal_and_overwrite_leave_no_stale_text_on_disk(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    store = _large_store(path)
    store.upsert("mem-removed", _vector(7_777), text="旧会议室门牌号是四个八")
    assert "旧会议室门牌号是四个八".encode() in path.read_bytes()
    assert store.remove("mem-removed") is True
    raw = path.read_bytes()
    assert "旧会议室门牌号是四个八".encode() not in raw  # 删除立即从盘上清掉明文
    assert VectorStore(path, identity=_ID).summary()["meta"]["v"] == 3

    store.upsert("mem-new", _vector(9_999), text="第一版正文")
    store.upsert("mem-new", _vector(9_998), text="第二版正文")  # 覆盖已有 id：整份写回
    raw = path.read_bytes()
    assert "第一版正文".encode() not in raw and "第二版正文".encode() in raw
    assert VectorStore(path, identity=_ID).search(_vector(9_998), top_k=1)[0].text == "第二版正文"


def test_append_tail_stays_within_budget_then_compacts(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    store = _large_store(path)
    resident = VectorStore(path, identity=_ID)
    base = path.stat().st_size
    budget = base * _MAX_TAIL_PERCENT // 100
    compacted_at = None
    for index in range(_BASE_COUNT):
        inode = path.stat().st_ino
        store.upsert(f"mem-extra-{index}", _vector(20_000 + index), text=f"追加 {index}")
        current = path.stat()
        if current.st_ino != inode:
            compacted_at = index
            break
        assert current.st_size - base <= budget  # 追加段始终在预算内

    assert compacted_at is not None and compacted_at > 10  # 先追加多条，快超预算时才整份压实一次
    loaded = VectorStore(path, identity=_ID)
    assert loaded.summary()["meta"]["count"] == _BASE_COUNT + compacted_at + 1
    assert len(resident) == _BASE_COUNT + compacted_at + 1


def test_resident_instances_follow_each_other_and_reject_foreign_generation(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    _large_store(path)
    first, second = VectorStore(path, identity=_ID), VectorStore(path, identity=_ID)
    first.upsert("mem-first", _vector(31), text="一")
    second.upsert("mem-second", _vector(32), text="二")  # 锁内先增量读到 mem-first，再接在其后追加
    for store in (first, second, VectorStore(path, identity=_ID)):
        assert {"mem-first", "mem-second"} <= set(store.ids()) and len(store) == _BASE_COUNT + 2

    # 同一 inode 被原地换成另一代快照（模拟 inode 复用）：读者不能把别代的记录套到自己的旧快照上。
    end = path.stat().st_size
    other = {"schema_version": VECTOR_STORE_SCHEMA_VERSION, "identity": _ID, "dim": _DIM, "generation": "other-generation"}
    one = {"mem-other": {"vector": _vector(77), "text": "别代", "metadata": {}}}
    unpadded = len(json.dumps({**other, "written_at": "", "items": one}, ensure_ascii=False).encode("utf-8"))
    base = json.dumps({**other, "written_at": "x" * (end - unpadded), "items": one}, ensure_ascii=False).encode("utf-8")
    assert len(base) == end
    later = _append_record("other-generation", {"mem-later": {"vector": _vector(78), "text": "别代追加", "metadata": {}}})
    with path.open("r+b") as handle:
        handle.truncate(0)
        handle.write(base + later)
    assert sorted(first.ids()) == ["mem-later", "mem-other"]
    assert first.search(_vector(77), top_k=1)[0].text == "别代"


def test_coordinator_append_passes_one_raw_write_through_the_gate(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    door = _Door()
    with commits.global_write_commit_scope(door):
        store = _large_store(path, coordinator_io=True)
        calls = door.calls
        store.upsert("mem-a", _vector(41), text="甲")
        assert door.calls == calls + 2  # 锁 FD 出生 + 一次追加末写
        before = path.read_bytes()
        header, items, stamp = store._header, store._items, store._stamp
        door.reject_at = door.calls + 2  # 拒绝这一次的追加末写
        with pytest.raises(LocalControlCoordinatorUnavailable) as caught:
            store.upsert("mem-b", _vector(42), text="乙")
        assert caught.value is door.error
    assert path.read_bytes() == before
    assert store._header is header and store._items is items and store._stamp == stamp
    assert "mem-b" not in VectorStore(path, identity=_ID).ids()


@_POSIX_ONLY
def test_non_private_or_linked_snapshot_is_replaced_not_appended(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    _large_store(path)
    os.chmod(path, 0o644)
    inode = path.stat().st_ino
    VectorStore(path, identity=_ID).upsert("mem-a", _vector(51), text="甲")
    assert path.stat().st_ino != inode and stat.S_IMODE(path.stat().st_mode) == 0o600  # 整份写回并收紧
    assert VectorStore(path, identity=_ID).summary()["meta"]["v"] == 3

    target = tmp_path / "elsewhere.json"
    path.rename(target)
    path.symlink_to(target)
    VectorStore(path, identity=_ID).upsert("mem-b", _vector(52), text="乙")
    assert not path.is_symlink()  # 与原来一样替换链接本身，不顺着链接改别处的文件
    assert b"mem-b" not in target.read_bytes() and "mem-b" in VectorStore(path, identity=_ID).ids()


def test_memory_commit_through_jsonl_memory_appends(tmp_path, monkeypatch) -> None:
    vectors = tmp_path / "memory_vectors.json"
    _large_store(vectors)
    memory = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=_DIM), vector_identity=_ID)
    rewritten: list[int] = []
    real_write = vector_store._write_private_snapshot_unlocked

    def counting_write(target, content, **kwargs):
        rewritten.append(len(content))
        return real_write(target, content, **kwargs)

    monkeypatch.setattr(vector_store, "_write_private_snapshot_unlocked", counting_write)
    inode = vectors.stat().st_ino
    record = memory.add("user", "客户要求本季度内完成支付系统的迁移")
    assert rewritten == [] and vectors.stat().st_ino == inode  # 正式提交后的向量投影只追加
    assert memory.vector_index_status()["vector_count"] == _BASE_COUNT + 1
    stored = memory._vector_store().stored_vectors([record.entry_id])
    vector, text = stored[record.entry_id]
    assert text == index_text(record.content, record.attributes)
    assert VectorStore(vectors, identity=_ID).search(vector, top_k=1)[0].id == record.entry_id
