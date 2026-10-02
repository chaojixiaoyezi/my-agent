"""语义记忆的存储（S3，用户 10-02 拍板）：文件权限，以及第一次召回不重复嵌入。

- 权限：记忆本体、向量库、正文哈希缓存都按 0600 写，long_term 目录 0700。钉住的是“替换之后”的权限，不只是新建时：
  先放一个 0644 的旧文件、0755 的目录，下一次写入后必须收紧；umask 放到 0 也一样（临时文件一出生就是 0600）。
- 复用：写入与重建按召回同一段索引文本嵌入并存下这段文本；召回先用 memory_vectors.json 里身份对得上、文本逐字相同的向量，
  只嵌本轮查询。身份不同（档案、协议、模型、端点任何一项，模型名里含维度）或文本不同，一律重嵌，不复用。
全部用测试替身和临时目录，不调真实嵌入。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval.text_vector_cache import TextVectorCache
from agent_py_agent.agent.retrieval.vector_store import VectorStore
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

_IDENTITY = embedding_identity("prof-x", LocalHashingEmbedder(dim=64))
_POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")
_FACTS = ("客户要求本季度内完成支付系统的迁移", "团建活动定在周五下午", "数据库主从切换需要夜间窗口")
_QUERY = "付款模块大概什么时候搬完"


# 类用途: 记下每次送去嵌入的文本，用来数召回到底嵌了什么。
class _CountingEmbedder(LocalHashingEmbedder):
    # 函数用途: 建一个带计数的哈希嵌入替身。
    def __init__(self, dim: int = 64) -> None:
        super().__init__(dim)
        self.texts: list[str] = []

    # 函数用途: 记下这批文本再照常嵌入。
    def embed(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed(texts)


# 函数用途: 在 long_term 目录里写入几条记忆，返回记忆库、嵌入替身和正文路径。
def _written(tmp_path: Path, identity: dict | None = _IDENTITY):
    embedder = _CountingEmbedder()
    body = tmp_path / "memory" / "long_term" / "memory.jsonl"
    memory = JsonlMemory(body, embedder=embedder, vector_identity=identity)
    for fact in _FACTS:
        memory.add("user", fact)
    return memory, embedder, body


# 函数用途: 把进程 umask 临时放到 0，验证私有写入不依赖 umask；用例结束恢复原值。
@pytest.fixture
def open_umask():
    previous = os.umask(0)
    try:
        yield
    finally:
        os.umask(previous)


# 函数用途: 读一个路径的权限位。
def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@_POSIX_ONLY
def test_memory_body_and_vector_snapshot_are_tightened_on_the_next_write(tmp_path, open_umask):
    long_term = tmp_path / "memory" / "long_term"
    body, vectors = long_term / "memory.jsonl", long_term / "memory_vectors.json"
    memory = JsonlMemory(body, embedder=LocalHashingEmbedder(dim=64), vector_identity=_IDENTITY)
    memory.add("user", "客户要求本季度内完成支付系统的迁移")
    assert (_mode(body), _mode(vectors), _mode(long_term)) == (0o600, 0o600, 0o700)

    os.chmod(body, 0o644)
    os.chmod(vectors, 0o644)
    os.chmod(long_term, 0o755)  # 生产现状：老文件 644、目录 755
    memory.add("user", "团建活动定在周五下午")

    assert (_mode(body), _mode(vectors), _mode(long_term)) == (0o600, 0o600, 0o700), "下一次写入（原子替换）后必须收紧"
    assert not [path.name for path in long_term.iterdir() if path.name.endswith(".tmp")], "不留临时文件"


@_POSIX_ONLY
def test_text_vector_cache_is_private_after_replace(tmp_path, open_umask):
    path = tmp_path / "long_term" / "memory_text_vectors.json"
    cache = TextVectorCache(path)
    cache.put({"fingerprint:hash-1": [0.1, 0.2]})
    assert (_mode(path), _mode(path.parent)) == (0o600, 0o700)

    os.chmod(path, 0o644)
    os.chmod(path.parent, 0o755)
    cache.put({"fingerprint:hash-2": [0.3, 0.4]})

    assert (_mode(path), _mode(path.parent)) == (0o600, 0o700)
    assert sorted(TextVectorCache(path).get(["fingerprint:hash-1", "fingerprint:hash-2"])) == [
        "fingerprint:hash-1", "fingerprint:hash-2"]


def test_first_recall_reuses_vectors_written_under_the_same_identity_and_text(tmp_path):
    memory, embedder, body = _written(tmp_path)
    assert len(embedder.texts) == len(_FACTS), "写入时每条嵌一次"
    embedder.texts.clear()

    found = memory.search_scoped("支付系统迁移", 3, lambda _record: True)

    assert embedder.texts == ["支付系统迁移"], "第一次召回只嵌本轮查询，不把记忆再嵌一遍"
    assert [record.content for record in found][:1] == [_FACTS[0]], "复用的向量照常参与召回"
    assert not (body.parent / "memory_text_vectors.json").exists(), "全部复用，正文哈希缓存不用补"


def test_recall_does_not_reuse_vectors_from_another_identity(tmp_path):
    _memory, _embedder, body = _written(tmp_path)
    other = embedding_identity("prof-y", LocalHashingEmbedder(dim=64))  # 换了档案编号，其余相同
    embedder = _CountingEmbedder()
    reader = JsonlMemory(body, embedder=embedder, vector_identity=other)

    reader.search_scoped(_QUERY, 3, lambda _record: True)

    assert sorted(embedder.texts) == sorted([_QUERY, *_FACTS]), "身份不同一律重嵌"


def test_recall_does_not_reuse_a_vector_embedded_from_different_text(tmp_path):
    memory, embedder, body = _written(tmp_path)
    store = VectorStore(body.parent / "memory_vectors.json", identity=_IDENTITY)
    target = next(record for record in memory.all() if record.content == _FACTS[0])
    vector, _text = store.stored_vectors([target.entry_id])[target.entry_id]
    store.upsert(target.entry_id, vector, text="旧口径：只嵌了正文的老条目", metadata={"entry_id": target.entry_id})
    embedder.texts.clear()

    memory.search_scoped(_QUERY, 3, lambda _record: True)

    assert sorted(embedder.texts) == sorted([_QUERY, _FACTS[0]]), "只有文本对不上的那条重嵌"
