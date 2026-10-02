"""P14：向量库单文件快照的一致性——同代、重建全有或全无、维度以实际向量为准。

锁定：
- 身份与向量同在一个快照文件里；读者发现换代就重载，写入在正式跨进程锁里"读最新 → 裁决 → 整份写回"，
  不会出现"B 的身份认证 A 的向量"，旧实例也盖不掉新一代（第 3 条）。
- 重建先拼完整新快照，任一条嵌入或写盘失败就 ok=False、计数如实、旧库不动；首个失败即停，不再继续付费请求（第 4 条）。
- 维度来自端点实际返回：客户端默认值不进快照；一批里长度不一或与库里维度不同都明确拒绝（第 5 条）。
全部用测试替身、伪端点和临时目录，不调真实嵌入。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval import vector_store
from agent_py_agent.agent.retrieval.embedding import EmbeddingError, OpenAICompatibleEmbedder
from agent_py_agent.agent.retrieval.vector_store import VectorIdentityError, VectorStore
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ID_64 = embedding_identity("prof-x", LocalHashingEmbedder(dim=64))
_ID_128 = embedding_identity("prof-x", LocalHashingEmbedder(dim=128))
_FACTS = ["客户要求本季度内完成支付系统的迁移", "团建活动定在周五下午", "数据库主从切换需要夜间窗口"]
_QUERY = "付款模块大概什么时候搬完"


def _unit(dim: int, hot: int) -> list[float]:
    return [1.0 if index == hot else 0.0 for index in range(dim)]


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "等待超时"
        time.sleep(0.01)


def _seeded(tmp_path: Path, facts: list[str] = _FACTS) -> JsonlMemory:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=64), vector_identity=_ID_64)
    for fact in facts:
        mem.add("user", fact)
    return mem


# ---------------------------------------------------------------- 第 3 条：同代快照与跨实例/跨进程锁


def test_stale_reader_reloads_new_generation_after_rebuild(tmp_path) -> None:
    """审查复现 1：读者先载入 A 的向量，另一个实例完成 B 重建后，旧读者必须换到 B 的快照。"""
    path = tmp_path / "mem.jsonl"
    _seeded(tmp_path)
    stale_b = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128), vector_identity=_ID_128)
    assert stale_b.vector_index_status()["reason"] == "VECTOR_IDENTITY_MISMATCH"  # 已载入 A 快照
    rebuilder = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128), vector_identity=_ID_128)
    assert rebuilder.rebuild_vectors()["ok"] is True

    assert stale_b.vector_index_status()["ok"] is True  # 旧实例发现换代并重载
    query = LocalHashingEmbedder(dim=128).embed([_QUERY])[0]
    stale_hits = stale_b._vector_store().search(query, top_k=3)
    fresh_hits = VectorStore(tmp_path / "memory_vectors.json", identity=_ID_128).search(query, top_k=3)
    assert stale_hits and [(h.id, h.score) for h in stale_hits] == [(h.id, h.score) for h in fresh_hits]


def test_stale_writer_after_rebuild_is_refused_and_new_generation_survives(tmp_path) -> None:
    """审查复现 2：A 在 B 重建前载入快照，之后再写；锁内重读发现已是 B，拒绝写入，B 的新项不丢。"""
    path = tmp_path / "memory_vectors.json"
    store_a = VectorStore(path, identity=_ID_64)
    store_a.upsert("a-1", _unit(64, 0), text="甲")
    VectorStore(path, identity=_ID_128).replace_all([("b-1", _unit(128, 0), "乙", {}), ("b-2", _unit(128, 1), "丙", {})])

    with pytest.raises(VectorIdentityError) as caught:
        store_a.upsert("a-2", _unit(64, 1), text="丁")
    assert caught.value.reason == "VECTOR_IDENTITY_MISMATCH"
    final = VectorStore(path, identity=_ID_128)
    assert final.identity_status() == (True, "") and sorted(final.ids()) == ["b-1", "b-2"]
    assert json.loads(path.read_text(encoding="utf-8"))["identity"] == _ID_128


def test_rebuild_waits_for_inflight_write_and_lands_last(tmp_path, monkeypatch) -> None:
    """确定性交错：A 的增量写正在落盘时，B 的整库替换必须等锁，不能插进 A 的读-改-写中间。"""
    path = tmp_path / "memory_vectors.json"
    writer_a = VectorStore(path, identity=_ID_64)
    writer_a.upsert("a-1", _unit(64, 0), text="甲")
    rebuilder_b = VectorStore(path, identity=_ID_128)
    paused, release = threading.Event(), threading.Event()
    real_write = vector_store.write_text_file_atomic_unlocked

    def gated_write(target: Path, content: str) -> None:
        if threading.current_thread().name == "writer-a":
            paused.set()
            assert release.wait(10)
        real_write(target, content)

    monkeypatch.setattr(vector_store, "write_text_file_atomic_unlocked", gated_write)
    thread_a = threading.Thread(target=writer_a.upsert, args=("a-2", _unit(64, 1)), name="writer-a")
    thread_a.start()
    assert paused.wait(10)
    thread_b = threading.Thread(target=rebuilder_b.replace_all, args=([("b-1", _unit(128, 0), "乙", {})],))
    thread_b.start()
    thread_b.join(0.3)
    assert thread_b.is_alive()  # A 持锁落盘中，B 只能等
    release.set()
    thread_a.join(10)
    thread_b.join(10)
    final = VectorStore(path, identity=_ID_128)
    assert final.identity_status() == (True, "") and final.ids() == ["b-1"]  # B 最后落盘，身份与向量同代


def test_writes_wait_for_the_cross_process_file_lock(tmp_path) -> None:
    """复用正式跨进程锁：另一个进程持有 locked_json_path 时，本进程的写入要等它释放。"""
    path = tmp_path / "memory_vectors.json"
    held, release = tmp_path / "held", tmp_path / "release"
    code = (
        "import sys, time\nfrom pathlib import Path\n"
        "from agent_py_agent.agent.common.json_io import locked_json_path\n"
        "with locked_json_path(Path(sys.argv[1])):\n"
        "    Path(sys.argv[2]).write_text('held')\n"
        "    while not Path(sys.argv[3]).exists():\n"
        "        time.sleep(0.01)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(_REPO_ROOT), "PYTHONDONTWRITEBYTECODE": "1"}
    child = subprocess.Popen([sys.executable, "-c", code, str(path), str(held), str(release)], env=env)
    try:
        _wait_for(held.exists)
        store = VectorStore(path, identity=_ID_64)
        writer = threading.Thread(target=store.upsert, args=("a-1", _unit(64, 0)), daemon=True)
        writer.start()
        writer.join(0.3)
        assert writer.is_alive() and not path.exists()  # 子进程持锁期间写不进去
        release.write_text("go")
        writer.join(10)
        assert not writer.is_alive() and store.ids() == ["a-1"]
    finally:
        release.write_text("go")
        assert child.wait(timeout=10) == 0


# ---------------------------------------------------------------- 第 4 条：重建全有或全无、计数如实


class _FailingEmbedder(LocalHashingEmbedder):
    """第 fail_at 次 embed 调用抛 OSError（模拟端点中途出错），其余照常。"""

    def __init__(self, dim: int, fail_at: int) -> None:
        super().__init__(dim)
        self.calls = 0
        self.fail_at = fail_at

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.calls == self.fail_at:
            raise OSError("endpoint reset")
        return super().embed(texts)


@pytest.mark.parametrize(("fail_at", "embedded"), [(1, 0), (3, 2)])
def test_rebuild_embed_failure_reports_counts_and_keeps_old_store(tmp_path, fail_at, embedded) -> None:
    _seeded(tmp_path)
    vectors = tmp_path / "memory_vectors.json"
    before = vectors.read_bytes()
    flaky = _FailingEmbedder(64, fail_at=fail_at)
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=flaky, vector_identity=_ID_64)

    result = mem.rebuild_vectors()
    assert result == {
        "ok": False, "reason": "VECTOR_REBUILD_INCOMPLETE", "failed_stage": "embed", "failure": "OSError",
        "rebuilt": 0, "active_count": 3, "attempted": embedded + 1, "embedded": embedded, "failed": 1,
        "vector_count": 3,
    }
    assert flaky.calls == fail_at  # 首个失败即停，不为注定作废的重建继续发请求
    assert vectors.read_bytes() == before  # 旧库原样保留
    status = mem.runtime_snapshot()["semantic_recall"]
    assert status["state"] == "degraded" and status["errors"]["rebuild"] == "VECTOR_REBUILD_INCOMPLETE"


def test_rebuild_write_failure_keeps_old_store(tmp_path, monkeypatch) -> None:
    mem = _seeded(tmp_path, _FACTS[:2])
    vectors = tmp_path / "memory_vectors.json"
    before = vectors.read_bytes()

    def broken_write(target: Path, content: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(vector_store, "write_text_file_atomic_unlocked", broken_write)
    result = mem.rebuild_vectors()
    assert result["ok"] is False and result["failed_stage"] == "write" and result["failure"] == "OSError"
    assert result["embedded"] == 2 and result["failed"] == 0 and result["rebuilt"] == 0 and result["vector_count"] == 2
    assert vectors.read_bytes() == before


def test_successful_rebuild_after_failure_clears_errors(tmp_path) -> None:
    _seeded(tmp_path)
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=_FailingEmbedder(64, fail_at=1), vector_identity=_ID_64)
    assert mem.rebuild_vectors()["ok"] is False
    result = mem.rebuild_vectors()  # 端点恢复后重试
    assert result["ok"] is True and result["reason"] == ""
    assert result["rebuilt"] == result["attempted"] == result["embedded"] == result["vector_count"] == 3
    assert result["failed"] == 0
    assert mem.runtime_snapshot()["semantic_recall"] == {"state": "configured"}


# ---------------------------------------------------------------- 第 5 条：维度以实际向量为准


class _FakeEndpoint:
    """OpenAI 兼容 /embeddings 伪端点：第 i 条输入返回长度 dims[i % len(dims)] 的向量，不上网。"""

    def __init__(self, *dims: int) -> None:
        self.dims = list(dims)

    def __call__(self, request, timeout=None):
        texts = json.loads(request.data.decode("utf-8"))["input"]
        rows = [{"embedding": [1.0] + [0.5] * (self.dims[i % len(self.dims)] - 1)} for i in range(len(texts))]
        return _FakeResponse(json.dumps({"data": rows}).encode("utf-8"))


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _client_memory(tmp_path: Path, monkeypatch, endpoint: _FakeEndpoint) -> JsonlMemory:
    monkeypatch.setattr(urllib.request, "urlopen", endpoint)
    client = OpenAICompatibleEmbedder(api_base="https://embed.example/v1", model="text-embedding-x")
    return JsonlMemory(tmp_path / "mem.jsonl", embedder=client, vector_identity=embedding_identity("prof-x", client))


def test_dimension_comes_from_actual_vectors_not_client_default(tmp_path, monkeypatch) -> None:
    mem = _client_memory(tmp_path, monkeypatch, _FakeEndpoint(2))
    assert mem._embedder.dim == 256  # 客户端只是默认声明，请求里也不带维度
    mem.add("user", _FACTS[0])
    status = mem.vector_index_status()
    assert status["ok"] is True and status["meta"]["dim"] == 2 and status["vector_count"] == 1


def test_dimension_change_is_an_explicit_mismatch(tmp_path, monkeypatch) -> None:
    endpoint = _FakeEndpoint(2)
    mem = _client_memory(tmp_path, monkeypatch, endpoint)
    mem.add("user", _FACTS[0])
    endpoint.dims = [3]  # 端点背后换了模型，型号名没变
    mem.add("user", _FACTS[1])
    errors = mem.runtime_snapshot()["semantic_recall"]["errors"]
    assert errors["index"] == "VECTOR_DIMENSION_MISMATCH" and len(mem._vector_store()) == 1  # 不混进第二种长度
    assert mem._semantic_records(_FACTS[0], 3) == []  # 3 维 query 对 2 维快照：明确失配，不打垃圾分
    assert mem.runtime_snapshot()["semantic_recall"]["errors"]["identity"] == "VECTOR_DIMENSION_MISMATCH"


def test_mixed_length_batches_are_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", _FakeEndpoint(2, 3))
    client = OpenAICompatibleEmbedder(api_base="https://embed.example/v1", model="text-embedding-x")
    with pytest.raises(EmbeddingError):
        client.embed(["甲", "乙"])  # 同一批返回两种长度，整批作废
    path = tmp_path / "memory_vectors.json"
    with pytest.raises(VectorIdentityError) as caught:
        VectorStore(path, identity=_ID_64).upsert_many([("a", [1.0, 0.0], "", {}), ("b", [1.0, 0.0, 0.0], "", {})])
    assert caught.value.reason == "VECTOR_DIMENSION_MISMATCH" and not path.exists()
