"""语义记忆文件权限（S3，用户 10-02 拍板）：记忆本体、向量库、正文哈希缓存都按 0600 写，long_term 目录 0700。

钉住的是“替换之后”的权限，不只是新建时：先放一个 0644 的旧文件、0755 的目录，下一次写入后必须收紧；
umask 放到 0 也一样（临时文件一出生就是 0600，不靠 umask）。全部用测试替身和临时目录，不调真实嵌入。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval.text_vector_cache import TextVectorCache
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位语义")
_IDENTITY = embedding_identity("prof-x", LocalHashingEmbedder(dim=64))


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
