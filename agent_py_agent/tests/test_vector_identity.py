"""P14：向量库快照记录空间身份（档案编号/线路协议/端点摘要/模型名）和实际维度，身份不对就退回关键词。

锁定：
- 身份从真正发请求的客户端对象算：同一档案换了端点或协议就是另一个空间；账号口令和密钥不进身份（第 1 条）。
- 生产接线只解析一次档案；身份建不起来就关掉语义通道并给结构化诊断，不落入"身份 None = 不检查"（第 2 条）。
- 读侧身份不一致或没有身份 → 已有向量当不存在、退回关键词；写侧拒绝混写。
- 管理入口 memory vectors rebuild 只认身份完整的本机 local/main；拒绝时不调嵌入、不改向量文件（第 6 条）。
- 状态预览读不了的数量写 None，不用 0 冒充。
全部用测试替身和临时目录，不调真实嵌入、不碰真实向量文件或记忆正文。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import core
from agent_py_agent.agent.backends.errors import ModelNotConfiguredError
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval.embedding import MiniMaxEmbedder, OpenAICompatibleEmbedder
from agent_py_agent.agent.retrieval.vector_store import (
    VECTOR_STORE_SCHEMA_VERSION,
    VectorIdentityError,
    VectorStore,
)
from agent_py_agent.agent.settings import embedding_profile
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.embedding_profile import embedding_identity
from agent_py_agent.cli import memory_admin_commands
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

_CLIENT_A = OpenAICompatibleEmbedder(api_base="https://embed-a.example/v1", model="text-embedding-3-small", api_key="sk-a")
_CLIENT_B = OpenAICompatibleEmbedder(api_base="https://embed-b.example/v1", model="text-embedding-3-small", api_key="sk-b")
_IDENTITY_A = embedding_identity("prof-a", _CLIENT_A)
_IDENTITY_B = embedding_identity("prof-a", _CLIENT_B)  # 审查复现：同一档案编号、同协议同型号，只换了服务商端点
_FACT = "客户要求本季度内完成支付系统的迁移"
_QUERY = "付款模块大概什么时候搬完"
_LOCAL_MAIN = {"owner_provider": "local", "owner_kind": "main", "owner_id": "main"}


def _mem(tmp_path: Path, identity: dict[str, str] | None = None, embedder: object | None = None) -> JsonlMemory:
    return JsonlMemory(
        tmp_path / "mem.jsonl",
        embedder=embedder or LocalHashingEmbedder(dim=64),
        vector_identity=identity,
    )


def _snapshot(tmp_path: Path) -> dict:
    # 向量库固定叫 memory_vectors.json（与记忆 JSONL 同目录），身份、维度和向量在同一个文件里。
    return json.loads((tmp_path / "memory_vectors.json").read_text(encoding="utf-8"))


def _errors(mem: JsonlMemory) -> dict[str, str]:
    return mem.runtime_snapshot()["semantic_recall"].get("errors", {})


# ---------------------------------------------------------------- 第 1 条：身份来自客户端连接


def test_identity_tracks_endpoint_and_protocol_not_credentials() -> None:
    assert _IDENTITY_A is not None and _IDENTITY_B is not None
    assert set(_IDENTITY_A) == {"profile_id", "protocol", "endpoint_digest", "model_name"}
    assert _IDENTITY_A != _IDENTITY_B  # 同档案同型号，换端点就是另一个向量空间
    same = OpenAICompatibleEmbedder(api_base="HTTPS://user:pw@Embed-A.example/v1/", model="text-embedding-3-small", api_key="sk-z")
    assert embedding_identity("prof-a", same) == _IDENTITY_A  # 大小写、末尾斜杠、账号口令、密钥都不改变空间
    minimax = MiniMaxEmbedder(api_base="https://embed-a.example/v1", model="text-embedding-3-small")
    assert embedding_identity("prof-a", minimax) != _IDENTITY_A  # 换线路协议也是另一个空间
    dumped = json.dumps([_IDENTITY_A, _IDENTITY_B])
    assert "sk-" not in dumped and "embed-a.example" not in dumped and "pw" not in dumped  # 不落凭据和端点明文


def test_identity_unavailable_without_profile_protocol_or_endpoint() -> None:
    assert embedding_identity("", _CLIENT_A) is None
    assert embedding_identity("prof-a", OpenAICompatibleEmbedder(api_base="", model="m")) is None
    assert embedding_identity("prof-a", OpenAICompatibleEmbedder(api_base="embed.example/v1", model="m")) is None
    assert embedding_identity("prof-a", SimpleNamespace(model="m", api_base="https://e.example")) is None  # 没声明协议


def test_endpoint_change_under_same_profile_is_detected(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", _FACT)
    reader = _mem(tmp_path, _IDENTITY_B)  # 同一档案被改成另一个服务商端点
    status = reader.vector_index_status()
    assert status["ok"] is False and status["reason"] == "VECTOR_IDENTITY_MISMATCH"
    reader.add("user", "换端点后的新记忆")  # 拒绝混写：新端点的向量不进旧空间
    assert len(reader._vector_store()) == 1
    assert _errors(reader)["index"] == "VECTOR_IDENTITY_MISMATCH"
    assert not any(_FACT in r.content for r in reader.search(_QUERY, top_k=3))  # 语义路关闭，换词 query 召不回


# ---------------------------------------------------------------- 第 2 条：接线一次解析、身份缺失即关通道


def _wired_agent(tmp_path: Path) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        memory_semantic_recall=True,
        embedding_model_profile="prof-x",
        tool_vector_search_enabled=False,
    )
    return SimpleAgent(config, tmp_path)


def test_wiring_resolves_profile_once_and_derives_identity_from_same_client(tmp_path, monkeypatch) -> None:
    calls: list[str] = []

    # 审查复现：第一次解析成功、第二次失败。修复后接线只解析一次，身份由同一个客户端算出。
    def resolve_once(agent: object, profile_id: str) -> SimpleNamespace:
        calls.append(profile_id)
        if len(calls) > 1:
            raise ModelNotConfiguredError(profile_id=profile_id, profile_reason="catalog_unreadable")
        return SimpleNamespace(model_name="text-embedding-3-small", api_base="https://embed.example/v1", api_key="sk-x")

    monkeypatch.setattr(embedding_profile, "embedding_model_config", resolve_once)
    agent = _wired_agent(tmp_path)
    embedder = agent.memory._embedder
    assert isinstance(embedder, OpenAICompatibleEmbedder) and calls == ["prof-x"]
    assert agent.memory._vector_identity == embedding_identity("prof-x", embedder)
    assert agent.memory.runtime_snapshot()["semantic_recall"] == {"state": "configured"}


def test_wiring_closes_semantic_channel_when_identity_unavailable(tmp_path, monkeypatch) -> None:
    class _NoProtocolEmbedder(LocalHashingEmbedder):
        protocol = ""  # 没声明线路协议 → 身份建不起来

    monkeypatch.setattr(core, "_build_memory_embedder", lambda agent, **kwargs: _NoProtocolEmbedder(dim=64))
    agent = _wired_agent(tmp_path)
    assert agent.memory._embedder is None and agent.memory._vector_identity is None  # 关通道，不是"身份 None 放行"
    assert agent.memory.runtime_snapshot()["semantic_recall"] == {
        "state": "degraded", "error_code": "MEMORY_EMBEDDING_IDENTITY_UNAVAILABLE",
    }
    agent.memory.add("user", _FACT)
    assert not list((tmp_path / "home").rglob("memory_vectors.json"))  # 没有不受管的向量落盘


# ---------------------------------------------------------------- 读写身份裁决


def test_matching_identity_writes_snapshot_header_and_recalls(tmp_path) -> None:
    mem = _mem(tmp_path, _IDENTITY_A)
    mem.add("user", _FACT)
    snapshot = _snapshot(tmp_path)
    assert snapshot["schema_version"] == VECTOR_STORE_SCHEMA_VERSION
    assert snapshot["identity"] == _IDENTITY_A and snapshot["dim"] == 64  # 维度来自实际向量
    assert snapshot["generation"] and snapshot["written_at"] and len(snapshot["items"]) == 1
    assert not (tmp_path / "memory_vectors.json.meta.json").exists()  # 不再有分开写的旁路元数据
    status = mem.vector_index_status()
    assert status["ok"] is True and status["vector_count"] == 1 and status["reason"] == ""
    assert any("支付系统" in r.content for r in mem.search(_QUERY, top_k=3))


def test_mismatched_identity_falls_back_to_keywords(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", _FACT)
    reader = _mem(tmp_path, _IDENTITY_B)
    assert reader.vector_index_status()["reason"] == "VECTOR_IDENTITY_MISMATCH"
    assert not any("支付系统" in r.content for r in reader.search(_QUERY, top_k=3))
    assert _errors(reader)["identity"] == "VECTOR_IDENTITY_MISMATCH"


def test_uncertified_write_clears_identity_and_reader_falls_back(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", _FACT)
    VectorStore(tmp_path / "memory_vectors.json").upsert("foreign", [0.125] * 64, text="来历不明")  # 不管理身份的写入
    assert _snapshot(tmp_path)["identity"] is None  # 它没法为新向量担保，快照随之失去身份
    reader = _mem(tmp_path, _IDENTITY_A)
    assert reader.vector_index_status()["reason"] == "VECTOR_META_MISSING"
    assert not any("支付系统" in r.content for r in reader.search(_QUERY, top_k=3))


def test_legacy_plain_file_is_uncertified_and_left_untouched(tmp_path) -> None:
    legacy = {"mem-old": {"vector": [0.125] * 64, "text": "旧版向量", "metadata": {}}}
    path = tmp_path / "memory_vectors.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    store = VectorStore(path, identity=_IDENTITY_A)
    assert store.identity_status() == (False, "VECTOR_META_MISSING")
    with pytest.raises(VectorIdentityError) as caught:
        store.upsert("mem-new", [0.125] * 64, text="新向量")
    assert caught.value.reason == "VECTOR_META_MISSING"
    assert json.loads(path.read_text(encoding="utf-8")) == legacy  # 拒绝时原文件不动


def test_mismatched_identity_refuses_upsert_and_search(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    VectorStore(path, identity=_IDENTITY_A).upsert("a", [1.0] * 64, text="旧身份向量")
    store_b = VectorStore(path, identity=_IDENTITY_B)  # 换身份打开同一库
    with pytest.raises(VectorIdentityError) as caught:
        store_b.upsert("x", [1.0] * 64, text="新身份向量")
    assert caught.value.reason == "VECTOR_IDENTITY_MISMATCH"
    with pytest.raises(VectorIdentityError) as caught:
        store_b.search([1.0] * 64, top_k=3)  # 检索在同一份快照上裁决，不在别人的空间里打分
    assert caught.value.reason == "VECTOR_IDENTITY_MISMATCH"
    assert VectorStore(path).ids() == ["a"]


def test_remove_keeps_certification_and_ignores_identity(tmp_path) -> None:
    path = tmp_path / "memory_vectors.json"
    store_a = VectorStore(path, identity=_IDENTITY_A)
    store_a.upsert_many([("a-1", [1.0, 0.0], "甲", {}), ("a-2", [0.0, 1.0], "乙", {})])
    assert VectorStore(path, identity=_IDENTITY_B).remove("a-1") is True  # 硬删除清理不被身份失配挡住
    assert store_a.identity_status() == (True, "") and store_a.ids() == ["a-2"]


def test_rebuild_replaces_snapshot_with_new_identity(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", _FACT)
    writer.add("user", "用户偏好喝美式咖啡")
    result = writer.rebuild_vectors()  # 同身份重建
    assert result["ok"] is True and result["rebuilt"] == 2 and result["vector_count"] == 2
    assert _snapshot(tmp_path)["identity"] == _IDENTITY_A

    swapper = _mem(tmp_path, _IDENTITY_B)
    assert swapper.vector_index_status()["reason"] == "VECTOR_IDENTITY_MISMATCH"
    result = swapper.rebuild_vectors()  # 换身份后重建：整库替换成新身份的向量
    assert result["ok"] is True and result["rebuilt"] == 2 and result["vector_count"] == 2
    assert swapper.vector_index_status()["ok"] is True
    assert _snapshot(tmp_path)["identity"] == _IDENTITY_B


def test_status_without_embedder_reports_known_counts_and_unknown_as_none(tmp_path) -> None:
    writer = _mem(tmp_path, _IDENTITY_A)
    writer.add("user", _FACT)
    writer.add("user", "用户偏好喝美式咖啡")
    plain = JsonlMemory(tmp_path / "mem.jsonl")  # 没有嵌入客户端，也要如实报能确认的数
    status = plain.vector_index_status()
    assert status["reason"] == "no_embedder" and status["vector_count"] == 2 and status["active_count"] == 2
    assert status["meta"]["identity"] == _IDENTITY_A
    (tmp_path / "memory_vectors.json").write_text("{ broken", encoding="utf-8")
    assert plain.vector_index_status()["vector_count"] is None  # 读不了 = 未知，不用 0 冒充
    (tmp_path / "memory_vectors.json").unlink()
    assert plain.vector_index_status()["vector_count"] == 0  # 没有文件 = 确定是 0


# ---------------------------------------------------------------- 第 6 条：CLI 管理入口


class _CountingEmbedder(LocalHashingEmbedder):
    def __init__(self, dim: int = 64) -> None:
        super().__init__(dim)
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return super().embed(texts)


def _cli_agent(tmp_path: Path, embedder: object | None = None, owner: dict[str, str] | None = None) -> SimpleNamespace:
    mem = _mem(tmp_path, _IDENTITY_A, embedder)
    mem.add("user", _FACT)
    return SimpleNamespace(
        root=tmp_path,
        home_paths=SimpleNamespace(**(owner or _LOCAL_MAIN)),
        config=SimpleNamespace(),
        memory=mem,
    )


def _run_json(monkeypatch, capsys, agent: SimpleNamespace, *argv: str) -> tuple[int, dict]:
    monkeypatch.setattr(memory_admin_commands, "make_agent", lambda _args: agent)
    args = build_parser().parse_args(["memory", *argv, "--json"])
    code = args.func(args)
    return code, json.loads(capsys.readouterr().out)


@pytest.fixture
def assert_refused_without_side_effects(tmp_path, monkeypatch, capsys):
    def check(owner: dict[str, str], confirmed: bool) -> None:
        embedder = _CountingEmbedder()
        agent = _cli_agent(tmp_path, embedder, owner)
        vectors = tmp_path / "memory_vectors.json"
        before = (vectors.read_bytes(), vectors.stat().st_mtime_ns, embedder.calls)
        code, out = _run_json(monkeypatch, capsys, agent, "vectors", "rebuild", *(["--confirmed"] if confirmed else []))
        assert code == 1 and out["error_code"] == "MEMORY_VECTORS_ADMIN_ONLY" and out["executed"] is False
        assert (vectors.read_bytes(), vectors.stat().st_mtime_ns, embedder.calls) == before  # 不调嵌入、不改向量文件

    return check


@pytest.mark.parametrize("confirmed", [True, False])
def test_cli_rebuild_refuses_non_admin_owner(assert_refused_without_side_effects, confirmed) -> None:
    owner = {"owner_provider": "feishu", "owner_kind": "user", "owner_id": "ou_123"}
    assert_refused_without_side_effects(owner, confirmed)


@pytest.mark.parametrize("confirmed", [True, False])
def test_cli_rebuild_refuses_incomplete_identity(assert_refused_without_side_effects, confirmed) -> None:
    owner = {"owner_provider": "", "owner_kind": "", "owner_id": "main"}  # 空值在目录墙裁决里算本机，管理入口不认
    assert_refused_without_side_effects(owner, confirmed)


def test_cli_vectors_status_previews_identity_and_count(tmp_path, monkeypatch, capsys) -> None:
    agent = _cli_agent(tmp_path)
    code, out = _run_json(monkeypatch, capsys, agent, "vectors", "status")
    assert code == 0
    assert out["command"] == "memory vectors status"
    assert out["vector_count"] == 1 and out["identity_ok"] is True and out["meta"]["identity"] == _IDENTITY_A


def test_cli_rebuild_requires_confirmed_and_then_reindexes(tmp_path, monkeypatch, capsys) -> None:
    agent = _cli_agent(tmp_path)
    code, out = _run_json(monkeypatch, capsys, agent, "vectors", "rebuild")
    assert code == 0
    assert out["executed"] is False and out["preview"]["vector_count"] == 1  # 未确认只预览
    assert len(agent.memory._vector_store()) == 1  # 库没被动

    code, out = _run_json(monkeypatch, capsys, agent, "vectors", "rebuild", "--confirmed")
    assert code == 0
    assert out["executed"] is True and out["rebuilt"] == 1 and out["vector_count"] == 1  # 确认后才重嵌
    assert out["attempted"] == 1 and out["embedded"] == 1 and out["failed"] == 0
    assert agent.memory.vector_index_status()["ok"] is True
