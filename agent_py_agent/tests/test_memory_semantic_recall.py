"""检索拓宽 #1 P1 真测:记忆召回加语义向量一路 + RRF 融合(本地 per-owner 向量,不碰共享库)。

记忆召回原本纯关键词(FTS5/BM25),"换词就召不回"。本条加一路语义向量召回融合。向量只存各 owner
自己 home 的本地 memory_vectors.json(零外部依赖、不碰共享向量库、按 owner 隔离)。默认无 embedder=
纯关键词不变;embed-on-write 把记忆写进本地向量库;端点抖动/无 embedder 自动降级不崩。
"""

from __future__ import annotations

import threading

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval.embedding import LocalHashingEmbedder


def test_no_embedder_is_pure_keyword(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl")  # 无 embedder
    mem.add("user", "deploy the payment service")
    assert mem._embedder is None
    assert mem._vector_store() is None  # 不建向量库
    assert any("deploy" in r.content for r in mem.search("deploy"))  # 关键词召回照常


def test_embed_on_write_uses_local_vector_file(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=64))
    mem.add("user", "客户预算五十万元")
    store = mem._vector_store()
    assert store is not None and len(store) == 1  # 写时 embed 进本地向量库
    assert (tmp_path / "memory_vectors.json").exists()  # ⭐ 向量在 owner home 本地,不碰共享向量库


def test_semantic_channel_returns_records(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "项目总花费是五十万元")
    mem.add("user", "今天天气不错")
    hits = mem._semantic_records("项目总花费是五十万元", top_k=5)
    assert hits and any("五十万" in r.content for r in hits)  # 语义一路真能召回


def test_search_fuses_keyword_and_semantic(tmp_path) -> None:
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "alpha beta gamma delta")
    mem.add("user", "unrelated content here")
    results = mem.search("alpha beta", top_k=5)
    assert any("alpha" in r.content for r in results)  # 融合后仍召回到目标


def test_embedder_failure_degrades_to_keyword(tmp_path) -> None:
    class _BoomEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("embeddings endpoint down")

    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=_BoomEmbedder())
    mem.add("user", "deploy the service now")  # embed-on-write 失败被吞,JSONL 仍写入
    results = mem.search("deploy")  # 语义路抛错 → 降级纯关键词,不崩
    assert any("deploy" in r.content for r in results)


def test_model_switch_skips_old_dim_then_new_writes_recall(tmp_path) -> None:
    """换 embedding 模型(维度变)后:旧维度向量被守卫跳过,不崩不垃圾召回;新写入按新维度正常语义召回。"""
    path = tmp_path / "mem.jsonl"
    old = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=64))
    old.add("user", "支付系统迁移计划下季度启动")  # 内容含"支付系统迁移"子串,供关键词兜底命中
    old.add("user", "团建活动定在周五下午")

    new = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=256))  # 换模型,同一 memory_vectors.json
    hits = new.search("支付系统迁移", top_k=3)  # 不崩
    assert hits and hits[0].content.startswith("支付系统迁移")  # 旧 64 维被跳(不截断成垃圾分把"团建"顶上来),关键词兜底命中目标

    new.add("user", "客户本季度预算大约五十万元")  # 新模型新写入 → 256 维
    hits2 = new.search("预算五十万", top_k=3)
    assert any("五十万" in r.content for r in hits2)  # ⭐ 新维度向量语义召回正常(关键词子串不命中,纯语义路捞回)


def test_search_time_endpoint_failure_degrades_to_keyword(tmp_path) -> None:
    """写入时 embed 正常、查询时端点挂掉(429/超时):语义路抛错被吞,降级纯关键词,不崩。"""

    class _FlakyEmbedder:
        def __init__(self) -> None:
            self.fail = False

        def embed(self, texts: list[str]) -> list[list[float]]:
            if self.fail:
                raise RuntimeError("embeddings endpoint down at query time")
            return [[1.0, 0.0] for _ in texts]

    emb = _FlakyEmbedder()
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=emb)
    mem.add("user", "把支付服务部署到生产环境")  # 写时正常 embed
    emb.fail = True  # 查询时端点故障
    hits = mem.search("支付服务", top_k=3)
    assert any("支付" in r.content for r in hits)  # 不崩,关键词兜底


def test_concurrent_memory_writes_same_owner_no_loss(tmp_path) -> None:
    """同 owner 并发写记忆(JSONL 追加 + 向量 upsert 一起):不崩、不丢、JSONL 与向量库条数一致、可召回。

    比单独测 VectorStore 并发更进一层——走完整 add_record 写路径(同会话本应被队列串行,但底座要自洽)。
    """
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    errors: list[Exception] = []

    def writer(w: int) -> None:
        try:
            for j in range(10):
                mem.add("user", f"记忆条目 owner-write {w}-{j} 各异内容")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(w,)) for w in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发写记忆不应出错:{errors[:3]}"
    assert len(mem.all()) == 120  # 12×10 全部落 JSONL,无丢(并发 append 不串行损坏)
    assert len(mem._vector_store()) == 120  # 向量库与 JSONL 一致(每条都 embed-on-write 进库)
    assert any("5-3" in r.content for r in mem.search("owner-write 5-3", top_k=5))  # 抽样可召回


def test_fusion_dedups_record_hit_by_both_channels(tmp_path) -> None:
    """同一条记录同时被关键词路和语义路命中,RRF 融合后只出现一次(_record_vec_id 跨两路对齐去重)。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "alpha beta gamma 支付系统迁移计划")
    mem.add("user", "delta epsilon 完全无关的内容")
    results = mem.search("支付系统迁移", top_k=5)
    contents = [r.content for r in results]
    assert contents.count("alpha beta gamma 支付系统迁移计划") == 1  # ⭐ 双路命中不重复计入


def test_build_memory_embedder_gating(tmp_path) -> None:
    from agent_py_agent.agent.core import _build_memory_embedder
    from agent_py_agent.agent.settings.config import AgentConfig

    assert _build_memory_embedder(AgentConfig()) is None  # 默认关 → None
    assert _build_memory_embedder(AgentConfig(memory_semantic_recall=True)) is None  # 开但没配 model → None
    embedder = _build_memory_embedder(
        AgentConfig(memory_semantic_recall=True, memory_embedding_model="text-embedding-3-small")
    )
    assert embedder is not None  # 开 + 配 model → 建出 embedder


def test_search_scoped_no_embedder_returns_ranked_active_only(tmp_path) -> None:
    """运行时召回主路径:无 embedder → HybridRetriever 降级纯 BM25;predicate allowlist 生效。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl")
    mem.add("user", "萧炎在加玛帝国用焚决修炼斗气")
    mem.add("user", "用户偏好喝美式咖啡")
    mem.add("user", "石昊在荒域采集灵药")  # 不属于本 scope,被 predicate 滤掉

    hits = mem.search_scoped(
        "焚决 斗气",
        top_k=5,
        predicate=lambda r: "石昊" not in r.content,
    )

    assert hits and "焚决" in hits[0].content  # BM25 词面排序目标在前
    assert not any("石昊" in r.content for r in hits)  # allowlist 外记录绝不复活


def test_search_scoped_with_embedder_fuses_and_still_scopes(tmp_path) -> None:
    """运行时召回主路径:有 embedder → 混合两路融合;scope 过滤仍先于检索。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "项目总花费是五十万元")
    mem.add("user", "今天天气不错")

    hits = mem.search_scoped(
        "项目花费 五十万",
        top_k=5,
        predicate=lambda r: "天气" not in r.content,
    )

    assert hits and any("五十万" in r.content for r in hits)
    assert not any("天气" in r.content for r in hits)
