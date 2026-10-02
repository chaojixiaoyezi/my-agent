"""P14：向量库旁记录嵌入身份（memory_vectors.meta.json），身份不一致或缺失时退回关键词。

锁定：读侧身份不一致 → 已有向量当不存在（语义召回退回关键词），给结构化原因码，不静默混用两个向量空间；
写侧身份不一致 → 拒绝写入，不把新模型的向量混进旧库；
重建入口 memory vectors status / rebuild --confirmed：先预览数量与身份，显式确认才清库重嵌。
全部用测试替身 LocalHashingEmbedder + 临时目录，绝不碰真实向量文件或记忆正文。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval.vector_store import VectorIdentityError
from agent_py_agent.cli import memory_admin_commands
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

_IDENTITY_A = {"profile_id": "prof-a", "provider": "openai_compatible",
               "model_name": "text-embedding-3-small", "dim": 64}
_IDENTITY_B = {"profile_id": "prof-b", "provider": "minimax",
               "model_name": "embo-01", "dim": 64}


def _mem(tmp_path: Path, identity: dict[str, object] | None = None) -> JsonlMemory:
    return JsonlMemory(
        tmp_path / "mem.jsonl",
        embedder=LocalHashingEmbedder(dim=64),
        vector_identity=identity,
    )


def _store(mem: JsonlMemory):
    return mem._vector_store()


def _meta_path(tmp_path: Path) -> Path:
    # 向量库固定叫 memory_vectors.json（与记忆 JSONL 同目录），元数据写在其旁。
    return tmp_path / "memory_vectors.json.meta.json"


def test_matching_identity_writes_meta_and_recalls(tmp_path) -> None:
    mem = _mem(tmp_path, _IDENTITY_A)
    mem.add("user", "客户要求本季度内完成支付系统的迁移")
    meta_path = _meta_path(tmp_path)
    assert meta_path.exists()  # 首次写入落盘身份元数据
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["profile_id"] == "prof-a" and meta["model_name"] == "text-embedding-3-small"
    assert meta["dim"] == 64 and "written_at" in meta and "schema_version" in meta
    status = mem.vector_index_status()
    assert status["ok"] is True and status["vector_count"] == 1 and status["reason"] == ""
    assert any("支付系统" in r.content for r in mem.search("付款模块大概什么时候搬完", top_k=3))


def test_mismatched_identity_falls_back_to_keywords(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", "客户要求本季度内完成支付系统的迁移")
    assert _store(writer).ids()  # A 身份写入成功

    reader = _mem(tmp_path, _IDENTITY_B)  # 换模型 B 再读
    status = reader.vector_index_status()
    assert status["ok"] is False and status["reason"] == "VECTOR_IDENTITY_MISMATCH"
    # 纯关键词召不回换词 query；身份不一致时语义路被关，不能静默混用旧向量
    assert not any("支付系统" in r.content for r in reader.search("付款模块大概什么时候搬完", top_k=3))


def test_missing_meta_falls_back_to_keywords(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", "客户要求本季度内完成支付系统的迁移")
    _meta_path(tmp_path).unlink()  # 旧文件没有元数据（或丢失）
    reader = _mem(tmp_path, _IDENTITY_A)
    status = reader.vector_index_status()
    assert status["ok"] is False and status["reason"] == "VECTOR_META_MISSING"
    assert not any("支付系统" in r.content for r in reader.search("付款模块大概什么时候搬完", top_k=3))


def test_mismatched_identity_does_not_write_new_vectors(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", "第一条记忆")
    assert len(_store(writer)) == 1

    swapper = _mem(tmp_path, _IDENTITY_B)  # 换身份后写入：拒绝混库，不落向量
    swapper.add("user", "换模型后的新记忆")
    assert len(_store(swapper)) == 1  # 旧库保持 1 条，新向量没混进来
    errors = swapper.runtime_snapshot()["semantic_recall"].get("errors", {})
    assert errors.get("index") == "VECTOR_IDENTITY_MISMATCH"


def test_upsert_rejects_mismatched_identity(tmp_path) -> None:
    import pytest

    from agent_py_agent.agent.retrieval.vector_store import VectorStore

    path = tmp_path / "mem.jsonl"
    VectorStore(path, identity=_IDENTITY_A).upsert("a", [1.0] * 64, text="旧身份向量")
    store_b = VectorStore(path, identity=_IDENTITY_B)  # 换身份打开同一库
    with pytest.raises(VectorIdentityError) as caught:
        store_b.upsert("x", [1.0] * 64, text="新身份向量")  # 库里已有 A 身份向量时,换 B 写被拒
    assert caught.value.reason == "VECTOR_IDENTITY_MISMATCH"


def test_rebuild_resets_store_and_writes_new_meta(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", "客户要求本季度内完成支付系统的迁移")
    writer.add("user", "用户偏好喝美式咖啡")
    assert len(_store(writer)) == 2

    result = writer.rebuild_vectors()  # 同身份重建：清库重嵌
    assert result["ok"] is True and result["rebuilt"] == 2 and result["vector_count"] == 2
    meta = json.loads(_meta_path(tmp_path).read_text(encoding="utf-8"))
    assert meta["profile_id"] == "prof-a"

    swapper = _mem(tmp_path, _IDENTITY_B)
    assert swapper.vector_index_status()["reason"] == "VECTOR_IDENTITY_MISMATCH"
    result = swapper.rebuild_vectors()  # 换身份后重建：旧向量清空、按新身份重嵌
    assert result["ok"] is True and result["rebuilt"] == 2 and result["vector_count"] == 2
    assert swapper.vector_index_status()["ok"] is True
    assert json.loads(_meta_path(tmp_path).read_text(encoding="utf-8"))["profile_id"] == "prof-b"


def _cli_agent(tmp_path: Path) -> SimpleNamespace:
    mem = _mem(tmp_path, _IDENTITY_A)
    mem.add("user", "客户要求本季度内完成支付系统的迁移")
    return SimpleNamespace(
        root=tmp_path,
        home_paths=SimpleNamespace(owner_id="main"),
        config=SimpleNamespace(),
        memory=mem,
    )


def _run_json(monkeypatch, capsys, agent: SimpleNamespace, *argv: str) -> tuple[int, dict]:
    monkeypatch.setattr(memory_admin_commands, "make_agent", lambda _args: agent)
    args = build_parser().parse_args(["memory", *argv, "--json"])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


def test_cli_vectors_status_previews_identity_and_count(tmp_path, monkeypatch, capsys) -> None:
    agent = _cli_agent(tmp_path)
    code, out = _run_json(monkeypatch, capsys, agent, "vectors", "status")
    assert code == 0
    assert out["command"] == "memory vectors status"
    assert out["vector_count"] == 1 and out["identity_ok"] is True


def test_cli_rebuild_requires_confirmed_and_then_reindexes(tmp_path, monkeypatch, capsys) -> None:
    agent = _cli_agent(tmp_path)
    code, out = _run_json(monkeypatch, capsys, agent, "vectors", "rebuild")
    assert code == 0
    assert out["executed"] is False and out["preview"]["vector_count"] == 1  # 未确认只预览
    assert len(_store(agent.memory)) == 1  # 库没被动

    code, out = _run_json(monkeypatch, capsys, agent, "vectors", "rebuild", "--confirmed")
    assert code == 0
    assert out["executed"] is True and out["rebuilt"] == 1 and out["vector_count"] == 1  # 确认后才清库重嵌
    assert agent.memory.vector_index_status()["ok"] is True