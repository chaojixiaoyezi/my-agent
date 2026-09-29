"""语义检索向量按正文哈希缓存：第二次检索不再重复嵌入未变化的事实（DESIGN_LEDGER 缺口 3）。

背景: 检索侧原先每次都把全部 active 事实重新送嵌入(_search_scoped → HybridRetriever._vector_order
里的 embed(texts)),事实越多越贵,召回前补充查询还会再翻一倍。本文件钉住:缓存只是派生索引,
有缓存与无缓存时检索结果逐条一致(顺序和分数都不变),且只对新增或改过的事实调用嵌入。

第二轮（复审后）新增的硬约束,每条对应一个真实缺陷或探针:
- 缓存独立成 ``memory_text_vectors.json``,检索路径永不重写含明文的 ``memory_vectors.json``(P6/P4);
- 缓存向量长度必须与本轮 query 一致,否则现场重嵌(P3,原先会整轮 IndexError);
- 缓存键用 ``sha256(实现类+api_base+model)[:16]`` 指纹,换端点/换模型自然失效;正文不进键;
- 写键与清理键同口径(index_text),带 keywords_en 的事实也删得掉(P1);replace 当场清旧键(P2);
- 取缓存之后才被删除的事实,其键不会被检索回写复活(P5);
- 缓存读写成功后健康状态必须恢复(不再永远停在 degraded,H1)。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.memory_store.jsonl import JsonlMemory
from agent_py_agent.agent.retrieval import hybrid as hybrid_mod
from agent_py_agent.agent.retrieval.hybrid import HybridRetriever
from agent_py_agent.agent.retrieval.text_vector_cache import (
    TextVectorCache,
    embedder_fingerprint,
    text_cache_key,
)
from agent_py_agent.agent.retrieval.vector_store import VectorStore
from agent_py_agent.tests._hashing_embedder import LocalHashingEmbedder

TEXT_CACHE_FILE = "memory_text_vectors.json"
VECTOR_FILE = "memory_vectors.json"


class _CountingEmbedder:
    """可计数的确定性 embedder：记录每次 embed 调用的文本条数，用于证明缓存命中时不再现嵌。"""

    def __init__(self, dim: int = 128, model: str = "counting-embedder") -> None:
        self._inner = LocalHashingEmbedder(dim=dim)
        self._model = model
        self.embedded_texts: list[str] = []
        self.calls: int = 0

    @property
    def dim(self) -> int:
        return self._inner.dim

    @property
    def model(self) -> str:
        return self._model

    @property
    def api_base(self) -> str:
        return "https://counting.example/v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.embedded_texts.extend(texts)
        return self._inner.embed(texts)

    def reset(self) -> None:
        self.calls = 0
        self.embedded_texts = []


def _seed(mem: JsonlMemory, texts: list[str]) -> None:
    for text in texts:
        mem.add("user", text)


def _all(_r) -> bool:
    return True


def _cache(mem: JsonlMemory) -> TextVectorCache:
    cache = mem._text_vector_cache()
    assert cache is not None
    return cache


def _key(mem: JsonlMemory, text: str, attributes: object = None) -> str:
    from agent_py_agent.agent.retrieval.text_vector_cache import index_text

    return text_cache_key(embedder_fingerprint(mem._embedder), index_text(text, attributes))


# ---------------------------------------------------------------- 基础：复用与一致性


def test_second_search_reuses_cache_and_only_embeds_new_fact(tmp_path) -> None:
    """第一次检索全量现嵌；第二次检索只为新增事实调用嵌入。"""
    embedder = _CountingEmbedder()
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    _seed(mem, ["支付系统迁移计划下季度启动", "团建活动定在周五下午"])

    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert embedder.calls >= 1
    # 第一次：查询 1 条 + 全部正文各 1 条
    assert len(embedder.embedded_texts) >= 3

    embedder.reset()
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    # 第二次：只剩查询本身要现嵌（全部正文命中缓存）
    assert embedder.embedded_texts == ["支付系统迁移"]

    mem.add("user", "客户本季度预算大约五十万元")
    embedder.reset()
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    # 新增事实后：查询 + 仅那条新事实
    assert embedder.embedded_texts[0] == "支付系统迁移"
    assert any("五十万" in text for text in embedder.embedded_texts[1:])
    assert len(embedder.embedded_texts) == 2


TEXTS = [
    "支付系统迁移计划下季度启动",
    "团建活动定在周五下午",
    "客户本季度预算大约五十万元",
    "数据库主从切换需要夜间窗口",
    "前端构建改用 bun 之后耗时减半",
    "飞书机器人只保留 Mac 上的一个实例",
    "周报每周一上午十点前提交",
    "测试机磁盘空间不足需要清理运行时",
]
QUERIES = ["支付系统迁移计划", "数据库主从切换窗口", "飞书机器人实例", "前端构建耗时", "周报提交时间"]


# E1（复审建议）：直接比较 rank 输出的 (id, score)，而不是最终 rerank 后的 entry_id 列表。
# 只比 entry_id 时，把缓存向量反转/置零/取负都不会被发现——那三个变异在旧测试下全部存活。
def test_rank_output_identical_with_full_and_partial_cache() -> None:
    """有缓存（全量与部分）时 rank 输出的 (id, score) 与无缓存逐条相同。"""
    emb = LocalHashingEmbedder(dim=128)
    docs = [(f"d{i}", t) for i, t in enumerate(TEXTS)]
    for query in QUERIES:
        base = HybridRetriever(emb).rank(query, docs, top_k=50, vector_min_score=0.30)
        warm = HybridRetriever(emb)
        warm.rank(query, docs, top_k=50, vector_min_score=0.30)
        full = dict(warm.last_fresh_doc_vectors)
        assert len(full) == len(docs)
        half = {k: v for k, v in full.items() if int(k[1:]) % 2 == 0}
        for cache in (full, half):
            got = HybridRetriever(emb).rank(
                query, docs, top_k=50, vector_min_score=0.30, cached_vectors=cache
            )
            assert got == base, query
            assert all(isinstance(score, float) for _id, score in got)


# E2（复审建议）：冷热两次都记录 rank 的真实输出，且缓存必须经过文件往返（换实例）。
def test_store_level_rank_output_survives_file_roundtrip(tmp_path, monkeypatch) -> None:
    """冷跑（无缓存）与热跑（缓存经文件往返）两个实例的 rank 输出逐条相同。"""
    captured: list[list[tuple[str, float]]] = []
    real_rank = hybrid_mod.HybridRetriever.rank

    def spy(self, *args, **kwargs):
        out = real_rank(self, *args, **kwargs)
        captured.append(list(out))
        return out

    monkeypatch.setattr(hybrid_mod.HybridRetriever, "rank", spy)
    path = tmp_path / "mem.jsonl"
    seed = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    _seed(seed, TEXTS)

    cold_results: dict[str, list[tuple[str, float]]] = {}
    with monkeypatch.context() as m:
        m.setattr(JsonlMemory, "_cached_vectors_for", lambda self, docs: {})
        for query in QUERIES:
            captured.clear()
            seed.search_scoped(query, 5, _all)
            cold_results[query] = captured[-1]

    warm = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))  # 新实例：缓存经文件往返
    for query in QUERIES:
        captured.clear()
        warm.search_scoped(query, 5, _all)
        assert captured[-1] == cold_results[query], query
    assert any(len(v) for v in cold_results.values())


def test_cached_and_uncached_results_identical(tmp_path) -> None:
    """有缓存与无缓存时检索结果逐条一致（端到端入口层面）。"""
    path = tmp_path / "mem.jsonl"

    cold = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    _seed(cold, TEXTS[:4])
    cold_ids = [r.entry_id for r in cold.search_scoped("支付系统迁移", top_k=5, predicate=_all)]

    warm = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    warm.search_scoped("支付系统迁移", top_k=5, predicate=_all)  # 先热身建缓存
    warm_ids = [r.entry_id for r in warm.search_scoped("支付系统迁移", top_k=5, predicate=_all)]

    assert cold_ids == warm_ids
    assert cold_ids  # 不是双双为空


# ---------------------------------------------------------------- 键与文件边界


def test_cache_lives_in_its_own_file_and_never_rewrites_plaintext_store(tmp_path) -> None:
    """缓存只写 memory_text_vectors.json；检索路径不新增权威向量库条目。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["支付系统迁移计划下季度启动"])
    vectors = tmp_path / VECTOR_FILE
    before = vectors.read_text(encoding="utf-8") if vectors.exists() else None

    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)

    assert (tmp_path / TEXT_CACHE_FILE).exists()
    assert _cache(mem).keys()
    # 检索路径不写含明文的权威向量库：内容与检索前完全一致（写入路径才维护它）。
    after = vectors.read_text(encoding="utf-8") if vectors.exists() else None
    assert after == before
    # 缓存文件里不含任何明文字段（只存键与向量）。
    assert "支付系统迁移" not in (tmp_path / TEXT_CACHE_FILE).read_text(encoding="utf-8")
    # 缓存键不进权威向量库的键空间。
    if after:
        assert not [k for k in json.loads(after) if k.startswith("textcache")]


def test_cache_key_binds_fingerprint_and_hides_plaintext(tmp_path) -> None:
    """键由 embedder 指纹与正文哈希组成；换模型/换端点/换文本都不同，且不含正文明文。"""
    text = "支付系统迁移计划"

    class _A(_CountingEmbedder):
        pass

    assert embedder_fingerprint(_A(model="m1")) != embedder_fingerprint(_A(model="m2"))
    assert text_cache_key("fp1", text) != text_cache_key("fp2", text)
    assert text_cache_key("fp1", text) != text_cache_key("fp1", text + "x")
    assert text_cache_key("fp1", text) == text_cache_key("fp1", text)
    assert text not in text_cache_key("fp1", text)  # 键里不含正文明文


def test_fingerprint_differs_on_endpoint_and_never_leaks_key() -> None:
    """指纹区分端点；embedder 只暴露 api_base，不暴露密钥。"""
    from agent_py_agent.agent.retrieval.embedding import OpenAICompatibleEmbedder

    a = OpenAICompatibleEmbedder(api_base="https://a.example/v1", model="m", api_key="secret-a")
    b = OpenAICompatibleEmbedder(api_base="https://b.example/v1", model="m", api_key="secret-a")
    assert embedder_fingerprint(a) != embedder_fingerprint(b)
    assert "secret-a" not in embedder_fingerprint(a)
    assert "https://a.example" not in embedder_fingerprint(a)  # 只存指纹，不落端点明文


def test_switching_model_does_not_hit_old_cache(tmp_path) -> None:
    """换模型后旧缓存键不再命中，本次必须重嵌全部正文（结果仍正确）。"""
    path = tmp_path / "mem.jsonl"
    first = _CountingEmbedder(dim=128, model="model-a")
    mem = JsonlMemory(path, embedder=first)
    _seed(mem, ["支付系统迁移计划下季度启动"])
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    first.reset()
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert first.embedded_texts == ["支付系统迁移"]  # 同模型：正文命中缓存

    swapped = _CountingEmbedder(dim=128, model="model-b")
    mem2 = JsonlMemory(path, embedder=swapped)
    mem2.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert any("支付系统迁移计划" in text for text in swapped.embedded_texts)


def test_corrupt_cache_falls_back_to_full_embed(tmp_path) -> None:
    """缓存文件损坏时退回全量重算，检索结果不变、不抛错。"""
    path = tmp_path / "mem.jsonl"
    mem = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["支付系统迁移计划下季度启动", "团建活动定在周五下午"])
    expected = [r.entry_id for r in mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)]

    (tmp_path / TEXT_CACHE_FILE).write_text("{ not json", encoding="utf-8")

    fresh = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    got = [r.entry_id for r in fresh.search_scoped("支付系统迁移", top_k=3, predicate=_all)]
    assert got == expected


def test_non_list_cache_entry_is_treated_as_missing(tmp_path) -> None:
    """缓存里出现非向量值时该条必须现嵌，不能当作已命中而漏掉。"""
    embedder = _CountingEmbedder()
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    _seed(mem, ["支付系统迁移计划下季度启动"])

    cache = _cache(mem)
    cache.put({_key(mem, "支付系统迁移计划下季度启动"): "not-a-vector"})  # type: ignore[dict-item]

    embedder.reset()
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert "支付系统迁移计划下季度启动" in embedder.embedded_texts  # 非法值不算命中


def test_partial_cache_still_embeds_missing_docs(tmp_path) -> None:
    """只有部分文档有缓存时，缺失的那些必须现嵌，语义臂不能漏项。"""
    embedder = _CountingEmbedder()
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    _seed(mem, ["支付系统迁移计划下季度启动", "团建活动定在周五下午"])

    target = next(r for r in mem.all() if "支付系统迁移" in r.content)
    _cache(mem).put({_key(mem, target.content, target.attributes): embedder.embed([target.content])[0]})

    embedder.reset()
    mem.search_scoped("团建活动", top_k=3, predicate=_all)
    assert target.content not in embedder.embedded_texts
    assert any("团建活动" in text for text in embedder.embedded_texts)


def test_legacy_entry_id_vectors_are_not_hits_for_text_cache(tmp_path) -> None:
    """旧格式 memory_vectors.json 只按 entry_id 存向量；它不能被新缓存键命中（须全量重算）。"""
    (tmp_path / VECTOR_FILE).write_text(
        '{"mem-legacy-1": {"vector": [1.0, 0.0], "text": "旧正文", "metadata": {}}}',
        encoding="utf-8",
    )
    embedder = _CountingEmbedder()
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    _seed(mem, ["支付系统迁移计划下季度启动"])

    embedder.reset()
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert any("支付系统迁移" in text for text in embedder.embedded_texts)


# ---------------------------------------------------------------- P3：长度守卫


class _AliasEmbedder:
    """模拟生产：OpenAI 兼容客户端 dim 恒为默认值、model 名不变，但端点背后实际向量长度变了。"""

    def __init__(self, length: int) -> None:
        self._inner = LocalHashingEmbedder(dim=length)

    @property
    def dim(self) -> int:
        return 256

    @property
    def model(self) -> str:
        return "alias-model"

    @property
    def api_base(self) -> str:
        return "https://alias.example/v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._inner.embed(texts)


# P3：同名模型、实际长度变化 → 缓存命中旧长度向量 → mean_center 越界，异常冒出 search_scoped。
def test_p3_same_model_name_new_length_does_not_crash(tmp_path) -> None:
    """同名模型但实际向量长度变化时，旧缓存必须当作缺失现嵌，检索不能抛 IndexError。"""
    path = tmp_path / "mem.jsonl"
    old = JsonlMemory(path, embedder=_AliasEmbedder(128))
    _seed(old, ["支付系统迁移计划下季度启动", "团建活动定在周五下午", "数据库主从切换需要夜间窗口"])
    old.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert _cache(old).keys()  # 旧长度向量已入缓存

    new = JsonlMemory(path, embedder=_AliasEmbedder(64))
    results = new.search_scoped("支付系统迁移", top_k=3, predicate=_all)  # 不能抛 IndexError
    assert results


# ---------------------------------------------------------------- P1/P2：键一致与 replace 清理


def test_p1_keywords_record_cache_is_removed(tmp_path) -> None:
    """带 keywords_en 的事实被删除后，其缓存项也必须清掉（写入键与清理键同口径）。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    rec = mem.apply_batch([
        {"action": "add", "role": "user", "content": "偏好用 Python 写部署脚本",
         "attributes": {"keywords_en": ["python"]}},
    ])[0]
    mem.search_scoped("Python 部署脚本", top_k=3, predicate=_all)
    before = _cache(mem).keys()
    assert len(before) == 1

    mem.remove(rec.entry_id)
    assert not [r for r in mem.all() if r.entry_id == rec.entry_id]
    assert _cache(mem).keys() == []  # 修复点：缓存项随事实删除一起清掉


def test_p2_replace_drops_old_text_key(tmp_path) -> None:
    """replace 当场清掉旧正文的缓存键；随后 remove 清掉最新正文的键。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    rec = mem.apply_batch([{"action": "add", "role": "user", "content": "团建定在周五下午"}])[0]
    mem.search_scoped("团建", top_k=3, predicate=_all)

    mem.apply_batch([{"action": "replace", "entry_id": rec.entry_id, "content": "团建改到周六上午"}])
    assert _cache(mem).keys() == []  # 修复点：replace 当下旧正文键就被清掉

    mem.search_scoped("团建", top_k=3, predicate=_all)
    assert len(_cache(mem).keys()) == 1  # 新正文重新嵌入
    mem.remove(rec.entry_id)
    assert not mem.all()
    assert _cache(mem).keys() == []


def test_cache_vector_length_mismatch_is_rembedded_not_used() -> None:
    """缓存向量长度与本轮 query 不一致时必须现场重嵌，且不抛异常（P3 的直接单元级钉法）。

    这条不看 entry_id 列表，直接断言 rank 的 (id, score)：把一份长度错误的缓存喂进去，
    输出必须与"完全无缓存"逐条相同——只要实现真的现嵌了，就一定成立。
    """
    emb = LocalHashingEmbedder(dim=128)
    docs = [(f"d{i}", t) for i, t in enumerate(TEXTS)]
    query = QUERIES[0]

    baseline = HybridRetriever(emb).rank(query, docs, top_k=50, vector_min_score=0.30)
    bogus = {f"d{i}": [0.1] * 64 for i in range(len(TEXTS))}  # 长度 64，实际 128
    got = HybridRetriever(emb).rank(query, docs, top_k=50, vector_min_score=0.30, cached_vectors=bogus)
    assert got == baseline

    # 且这些错长度向量确实被当成缺失而重新嵌入过
    probe = HybridRetriever(emb)
    probe.rank(query, docs, top_k=50, vector_min_score=0.30, cached_vectors=bogus)
    assert len(probe.last_fresh_doc_vectors) == len(docs)


def test_cache_put_does_not_persist_stale_vector_for_replaced_fact(tmp_path) -> None:
    """记录被替换后，旧索引文本的向量不得被回写（P5 的键级钉法，独立于删除路径）。"""
    embedder = _CountingEmbedder()
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    rec = mem.apply_batch([{"action": "add", "role": "user", "content": "团建定在周五下午"}])[0]
    mem.search_scoped("团建", top_k=3, predicate=_all)
    old_key = _key(mem, "团建定在周五下午")
    assert _cache(mem).get([old_key])  # 已缓存

    # 绕过 apply_batch 直接改正文（模拟另一进程写盘），检索回写必须按 active 身份复核后跳过。
    mem.apply_batch([{"action": "replace", "entry_id": rec.entry_id, "content": "团建改到周六上午"}])
    assert not _cache(mem).get([old_key])  # 旧键已随 replace 清掉

    embedder.reset()
    mem.search_scoped("团建", top_k=3, predicate=_all)
    assert not _cache(mem).get([old_key])  # 检索也不会把旧键写回来
    assert _key(mem, "团建改到周六上午") in _cache(mem).keys()  # 新正文有缓存


# ---------------------------------------------------------------------- P4


def test_p4_text_cache_does_not_crowd_unscoped_semantic(tmp_path) -> None:
    """缓存项不进权威向量库，全库 search/_semantic_records 的候选数不受建缓存影响。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    for i in range(30):
        mem.add("user", f"第{i}号项目的发布窗口安排在周{i % 7}晚上，负责人编号{i}")
    base = mem._semantic_records("项目发布窗口安排", 20)
    mem.search_scoped("项目发布窗口安排", top_k=5, predicate=_all)
    after = mem._semantic_records("项目发布窗口安排", 20)
    assert len(after) == len(base)  # 修复点：缓存不再挤掉真实条目
    assert _cache(mem).keys()


# ---------------------------------------------------------------- P5：删除与检索回写的竞态


class _HookEmbedder:
    """在检索现嵌文档时触发一次回调，模拟"检索嵌入期间另一线程删事实"的时序。"""

    def __init__(self, hook=None) -> None:
        self._inner = LocalHashingEmbedder(dim=128)
        self.hook = hook

    @property
    def dim(self) -> int:
        return 128

    @property
    def model(self) -> str:
        return "hook-model"

    @property
    def api_base(self) -> str:
        return "https://hook.example/v1"

    def embed(self, texts: list[str]) -> list[list[float]]:
        hook, self.hook = self.hook, None
        if hook is not None:
            hook()
        return self._inner.embed(texts)


# P5：检索读缓存(未命中) → 嵌入期间事实被删 → 检索回写 → 已删事实的孤儿键。
def test_p5_delete_during_embed_leaves_no_orphan(tmp_path) -> None:
    """检索取缓存之后才被删除的事实，其键不会被回写复活。"""
    mem_holder: dict[str, JsonlMemory] = {}
    victim_holder: dict[str, str] = {}
    calls = {"n": 0}

    def remove_victim() -> None:
        mem_holder["mem"].remove(victim_holder["id"])

    def hook() -> None:
        calls["n"] += 1

    embedder = _HookEmbedder(hook=hook)
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    mem_holder["mem"] = mem
    victim = mem.apply_batch([{"action": "add", "role": "user", "content": "临时口令放在备忘录第三页"}])[0]
    victim_holder["id"] = victim.entry_id
    mem.add("user", "团建活动定在周五下午")

    original_embed = embedder.embed

    def embed_second_triggers(texts):
        if calls["n"] == 1 and len(texts) > 1:
            calls["n"] += 1
            remove_victim()
        return original_embed(texts)

    embedder.embed = embed_second_triggers  # type: ignore[method-assign]
    mem.search_scoped("团建", top_k=3, predicate=_all)

    assert calls["n"] == 2
    assert all(r.entry_id != victim.entry_id for r in mem.all())
    orphan = _key(mem, victim.content)
    assert orphan not in _cache(mem).keys()  # 修复点：已删事实的向量不被检索回写


# ---------------------------------------------------------------- P6：纯检索实例不复活明文


def test_p6_search_only_instance_never_rewrites_plaintext_store(tmp_path) -> None:
    """只做检索的实例不写 memory_vectors.json，不会把已删除事实的明文写回。"""
    path = tmp_path / "mem.jsonl"
    writer = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    victim = writer.apply_batch([{"action": "add", "role": "user", "content": "旧门禁密码是四个八"}])[0]
    writer.add("user", "团建活动定在周五下午")

    reader = JsonlMemory(path, embedder=LocalHashingEmbedder(dim=128))
    reader.search_scoped("团建", top_k=3, predicate=_all)  # reader 载入含 victim 的快照

    writer.remove(victim.entry_id)
    vectors = tmp_path / VECTOR_FILE
    if vectors.exists():
        assert victim.entry_id not in json.loads(vectors.read_text(encoding="utf-8"))

    writer.add("user", "客户预算大约五十万元")  # reader 下次检索要现嵌这条 → 触发缓存回写
    reader.search_scoped("团建", top_k=3, predicate=_all)

    # 修复点：只做检索的实例只写自己的缓存文件，绝不整文件回写权威向量库。
    if vectors.exists():
        data = json.loads(vectors.read_text(encoding="utf-8"))
        assert not (victim.entry_id in data and data[victim.entry_id].get("text") == victim.content)
        assert victim.content not in vectors.read_text(encoding="utf-8")


# ---------------------------------------------------------------- H1：健康状态恢复


def test_h1_cache_read_success_restores_health(tmp_path, monkeypatch) -> None:
    """一次瞬时读缓存错误后，后续成功的读取必须把健康状态恢复（不能永远 degraded）。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    mem.add("user", "支付系统迁移计划下季度启动")
    real_get = TextVectorCache.get
    state = {"fail": True}

    def flaky(self, keys):
        if state["fail"]:
            state["fail"] = False
            raise ValueError("transient")
        return real_get(self, keys)

    monkeypatch.setattr(TextVectorCache, "get", flaky)
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert mem._semantic_status.get("state") == "degraded"

    for _ in range(3):
        mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert mem._semantic_status.get("state") == "configured"  # 修复点：成功读取恢复健康


# ---------------------------------------------------------------- 回收与上限


def test_index_all_reclaims_orphan_cache_keys(tmp_path) -> None:
    """index_all 回收不属于任何 active 记录的缓存键（换模型/迁移留下的旧键）。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["支付系统迁移计划下季度启动"])
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)

    cache = _cache(mem)
    cache.put({"deadbeef:" + "0" * 64: [0.1, 0.2]})  # 模拟旧指纹/迁移遗留
    assert len(cache.keys()) == 2

    mem.index_all()
    keys = cache.keys()
    assert len(keys) == 1
    assert keys == [_key(mem, "支付系统迁移计划下季度启动")]


def test_index_all_reclaims_even_without_local_store(tmp_path) -> None:
    """没配 LocalStore 时 index_all 仍要回收孤儿键（它原本会提前 return 0，回收就被跳过）。"""
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["支付系统迁移计划下季度启动"])
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)
    assert mem.local_store is None  # 这条测试专门钉住"没有 FTS 索引"的路径

    cache = _cache(mem)
    cache.put({"deadbeef:" + "0" * 64: [0.1, 0.2]})
    assert len(cache.keys()) == 2

    assert mem.index_all() == 0  # 没有 LocalStore，没有记录进 FTS
    assert cache.keys() == [_key(mem, "支付系统迁移计划下季度启动")]  # 但孤儿键仍被回收


def test_removed_fact_drops_its_cached_vector(tmp_path) -> None:
    """删除事实后，对应正文的缓存向量被清掉，缓存不只增不减。"""
    embedder = LocalHashingEmbedder(dim=128)
    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=embedder)
    _seed(mem, ["支付系统迁移计划下季度启动", "团建活动定在周五下午"])
    mem.search_scoped("支付系统迁移", top_k=3, predicate=_all)

    target = next(r for r in mem.all() if "支付系统迁移" in r.content)
    key = _key(mem, target.content, target.attributes)
    assert _cache(mem).get([key])  # 缓存里已有

    mem.remove(target.entry_id)
    assert not _cache(mem).get([key])  # 删除后缓存项被清


# ---------------------------------------------------------------- 权威向量库仍然可用


def test_authoritative_vector_store_still_works(tmp_path) -> None:
    """缓存独立后，权威 VectorStore 的 entry 向量读写与全库检索不受影响。"""
    store = VectorStore(tmp_path / VECTOR_FILE)
    store.upsert("mem-1", [1.0, 0.0], text="甲", metadata={})
    store.upsert("mem-2", [0.0, 1.0], text="乙", metadata={})
    hits = store.search([1.0, 0.0], top_k=5)
    assert [h.id for h in hits] == ["mem-1", "mem-2"]
    assert store.remove("mem-1") is True
    assert [h.id for h in store.search([1.0, 0.0], top_k=5)] == ["mem-2"]


@pytest.mark.parametrize("text", ["支付系统迁移计划下季度启动", "团建活动定在周五下午"])
def test_cache_get_is_order_independent(tmp_path, text) -> None:
    """缓存读取与写入顺序无关，同键重复取出结果一致。"""
    cache = TextVectorCache(tmp_path / TEXT_CACHE_FILE)
    key = text_cache_key("fp", text)
    cache.put({key: [0.25, 0.75]})
    assert cache.get([key]) == {key: [0.25, 0.75]}
    assert cache.get([key, "missing"]) == {key: [0.25, 0.75]}


# ---------------------------------------------------------------- X1：跨进程合并不能复活别人删掉的键


def test_x1_flush_does_not_resurrect_key_deleted_by_other_instance(tmp_path) -> None:
    """两个实例共用一个缓存文件：A 删掉的键，B 写自己的键时不得把它带回来。

    关键构造：B 的内存快照里**必须真的持有那个旧键**（通过 B 自己 put 一次建立），
    否则 B 的 `_items` 是空的，"只写整份内存快照"这个变异体就没有东西可复活，
    测试会假绿（这正是先前 MX 存活的原因）。
    """
    path = tmp_path / TEXT_CACHE_FILE
    a = TextVectorCache(path)
    b = TextVectorCache(path)

    victim = text_cache_key("fp", "旧门禁密码是四个八")
    b.put({victim: [1.0, 0.0]})  # B 的内存快照持有 victim（也会落盘）
    assert victim in b.keys()
    a = TextVectorCache(path)  # A 载入含 victim 的状态
    assert victim in a.keys()

    a.remove([victim])  # A 删掉它
    b.put({text_cache_key("fp", "团建改到周六上午"): [0.0, 1.0]})  # B 写自己的新键

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert victim not in on_disk  # 修复点：B 的写不能复活 A 删掉的键
    assert text_cache_key("fp", "团建改到周六上午") in on_disk


def test_x1_flush_keeps_key_added_by_other_instance(tmp_path) -> None:
    """反向：另一个实例新增的键不能被本实例的写吞掉。"""
    path = tmp_path / TEXT_CACHE_FILE
    a = TextVectorCache(path)
    b = TextVectorCache(path)

    a.get([])  # A 载入空快照
    theirs = text_cache_key("fp", "客户预算大约五十万元")
    b.put({theirs: [0.5, 0.5]})

    a.put({text_cache_key("fp", "支付系统迁移计划下季度启动"): [1.0, 0.0]})
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert theirs in on_disk  # 别人新增的键不丢
    assert text_cache_key("fp", "支付系统迁移计划下季度启动") in on_disk


# ---------------------------------------------------------------- 坏值与写失败不能毁掉整个缓存


def test_load_skips_bad_value_but_keeps_other_keys(tmp_path) -> None:
    """文件里一个键含非数值时，只丢那个键，其余键照常可用（不能整个缓存作废）。"""
    path = tmp_path / TEXT_CACHE_FILE
    good = text_cache_key("fp", "支付系统迁移计划下季度启动")
    bad = text_cache_key("fp", "团建活动定在周五下午")
    path.write_text(
        json.dumps({good: [0.1, 0.2], bad: ["oops", 0.2], "notalist": 5}),
        encoding="utf-8",
    )

    cache = TextVectorCache(path)
    assert cache.get([good]) == {good: [0.1, 0.2]}
    assert cache.get([bad]) == {}
    assert bad not in cache.keys()


def test_get_returns_empty_on_corrupt_file(tmp_path) -> None:
    """整个文件坏掉时当作空缓存，构造不抛错。"""
    path = tmp_path / TEXT_CACHE_FILE
    path.write_text("{ not json", encoding="utf-8")
    cache = TextVectorCache(path)
    assert cache.keys() == []
    assert cache.get(["anything"]) == {}


# ---------------------------------------------------------------- P5b：复核与写入之间的窗口


def test_p5b_put_rechecks_validity_inside_lock(tmp_path) -> None:
    """传给 put 的 keep 判据在同一临界区内生效：判据里没有的键一律不落盘。"""
    path = tmp_path / TEXT_CACHE_FILE
    cache = TextVectorCache(path)
    stale = text_cache_key("fp", "临时口令放在备忘录第三页")
    live = text_cache_key("fp", "团建活动定在周五下午")

    cache.put({stale: [1.0, 0.0], live: [0.0, 1.0]}, keep=lambda: {live})

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert stale not in on_disk
    assert live in on_disk


# ------------------------------------------------ P5c/P5d：跨实例删除不能留孤儿


def test_p5c_keep_is_evaluated_inside_the_file_lock(tmp_path, monkeypatch) -> None:
    """keep() 在拿到跨进程文件锁之后才算：那一刻别的实例刚删掉的事实不会被写回（P5c）。"""
    from agent_py_agent.agent.common import json_io

    path = tmp_path / TEXT_CACHE_FILE
    cache = TextVectorCache(path)
    stale = text_cache_key("fp", "临时口令放在备忘录第三页")
    live = text_cache_key("fp", "团建活动定在周五下午")
    calls = {"n": 0}

    def keep():
        calls["n"] += 1
        # 第一次（实例锁内预过滤）说都有效；第二次（文件锁内）已经知道 stale 被删。
        return {stale, live} if calls["n"] == 1 else {live}

    real_locked = json_io.locked_json_path

    def locked(path_arg, **kwargs):
        assert calls["n"] >= 1, "keep 必须在进入文件锁前/中被调用"
        return real_locked(path_arg, **kwargs)

    monkeypatch.setattr(json_io, "locked_json_path", locked)
    cache.put({stale: [1.0, 0.0], live: [0.0, 1.0]}, keep=keep)
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert stale not in on_disk  # 文件锁内的那次复核把它挡掉了
    assert live in on_disk
    assert calls["n"] >= 2, "keep 必须在文件锁内被再算一次"


def test_p5d_remove_subtracts_from_disk_even_if_not_in_memory(tmp_path) -> None:
    """另一个实例写的键，本实例内存里没有，remove 也必须从磁盘上删掉（P5d）。"""
    path = tmp_path / TEXT_CACHE_FILE
    a = TextVectorCache(path)
    b = TextVectorCache(path)
    victim = text_cache_key("fp", "临时口令放在备忘录第三页")

    a.put({victim: [1.0, 0.0]})  # A 写入
    assert victim not in b.keys()  # B 的内存视图里没有这个键

    b.remove([victim])  # B 删它

    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert victim not in on_disk  # 修复点：不能因为"本实例内存里没有"就跳过落盘


# ------------------------------------------------ W1 / X1b：写失败与拿锁失败


def test_w1_primary_write_failure_records_error(tmp_path, monkeypatch) -> None:
    """拿到锁但 rename 失败（如磁盘满）时必须留下 last_write_error（W1）。"""
    from pathlib import Path as _Path

    path = tmp_path / TEXT_CACHE_FILE
    cache = TextVectorCache(path)
    real_replace = _Path.replace

    def failing_replace(self, target):
        if self.name.startswith(TEXT_CACHE_FILE) and self.name.endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(_Path, "replace", failing_replace)
    cache.put({text_cache_key("fp", "团建活动定在周五下午"): [0.0, 1.0]})
    assert cache.last_write_error is not None  # 修复点：写失败可见
    assert not path.exists()


def test_w1_write_failure_reaches_jsonl_health(tmp_path, monkeypatch) -> None:
    """写失败还必须一路反映到 JsonlMemory 的语义健康状态，不能只停在缓存对象上。

    这条是 MY8 的杀手：只把 `last_write_error` 留在缓存对象里、不报给健康状态，测试要变红。
    """
    from pathlib import Path as _Path

    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["团建活动定在周五下午"])
    real_replace = _Path.replace

    def failing_replace(self, target):
        if self.name.startswith(TEXT_CACHE_FILE) and self.name.endswith(".tmp"):
            raise OSError(28, "No space left on device")
        return real_replace(self, target)

    monkeypatch.setattr(_Path, "replace", failing_replace)
    mem.search_scoped("团建", top_k=3, predicate=_all)  # 检索会回写缓存 → 触发写失败

    status = mem._semantic_status
    assert status.get("state") == "degraded", f"健康状态没反映写失败：{status}"
    assert "cache_write" in (status.get("errors") or {})


def test_x1b_lock_failure_does_not_write(tmp_path, monkeypatch) -> None:
    """拿不到跨进程锁时不得写盘：磁盘内容不可信，写回会复活别的实例已删的键（X1b）。"""
    from agent_py_agent.agent.common import json_io

    path = tmp_path / TEXT_CACHE_FILE
    victim = text_cache_key("fp", "旧门禁密码是四个八")
    TextVectorCache(path).put({victim: [1.0, 0.0]})
    a = TextVectorCache(path)  # A 载入含 victim 的快照
    b = TextVectorCache(path)
    b.remove([victim])  # B 删掉
    before = json.loads(path.read_text(encoding="utf-8"))
    assert victim not in before

    def lock_unavailable(_path, **_kwargs):
        raise PermissionError(13, "Permission denied: lock file")

    monkeypatch.setattr(json_io, "locked_json_path", lock_unavailable)
    a.put({text_cache_key("fp", "团建改到周六上午"): [0.0, 1.0]})

    after = json.loads(path.read_text(encoding="utf-8"))
    assert victim not in after  # 修复点：回退路径不写盘
    assert a.last_write_error is not None  # 锁失败留下痕迹


# ------------------------------------------------ V1/V2：缓存不可读不能冒泡成业务失败


def _make_unreadable(path: Path) -> None:
    """把一个文件变成「存在但读不了」：chmod 000（macOS/linux 通用）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o000)


def test_v1_add_succeeds_when_cache_unreadable(tmp_path) -> None:
    """缓存文件读不了时，add 必须正常返回——它在权威 JSONL **已经提交之后**才清缓存，
    读错误冒泡会让调用方以为写入失败而重试（V1）。"""
    import os as _os

    from agent_py_agent.agent.retrieval.text_vector_cache import TextVectorCache as _C

    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    cache_path = tmp_path / TEXT_CACHE_FILE
    _make_unreadable(cache_path)
    # 先确认"读不了"这件事真的被构造出来了，否则测试会假绿
    assert _C(cache_path).last_read_error is not None
    try:
        record = mem.add("user", "团建改到周六上午")  # 不能抛
        assert record is not None
        assert any(r.content == "团建改到周六上午" for r in mem.all())
    finally:
        _os.chmod(cache_path, 0o600)


def test_v2_search_degrades_when_cache_unreadable(tmp_path) -> None:
    """缓存文件读不了时检索必须退化为当场嵌入，不能整次失败（V2）。"""
    import os as _os

    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["团建活动定在周五下午"])
    cache_path = tmp_path / TEXT_CACHE_FILE
    _make_unreadable(cache_path)
    try:
        results = mem.search_scoped("团建", top_k=3, predicate=_all)  # 不能抛
        assert results
    finally:
        _os.chmod(cache_path, 0o600)


def test_flush_does_not_overwrite_when_disk_unreadable(tmp_path, monkeypatch) -> None:
    """_flush 遇到读错误时放弃这次写，绝不把读不出来的缓存当成空再写回去。"""
    from agent_py_agent.agent.common import json_io

    path = tmp_path / TEXT_CACHE_FILE
    keep = text_cache_key("fp", "团建活动定在周五下午")
    cache = TextVectorCache(path)
    cache.put({keep: [1.0, 0.0]})
    before = path.read_text(encoding="utf-8")

    import agent_py_agent.agent.retrieval.text_vector_cache as mod

    real_locked = json_io.locked_json_path

    def locked_then_unreadable(p, **kwargs):
        # 拿锁之后让读盘失败：模拟权限变化
        monkeypatch.setattr(mod.TextVectorCache, "_load", lambda self: (_ for _ in ()).throw(PermissionError(13, "denied")))
        return real_locked(p, **kwargs)

    monkeypatch.setattr(json_io, "locked_json_path", locked_then_unreadable)
    cache.put({text_cache_key("fp", "另一条"): [0.0, 1.0]})
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before  # 原文件没被清空/覆盖


def _make_unreadable_path(tmp_path) -> str:
    return str(tmp_path / TEXT_CACHE_FILE)


# ------------------------------------------------ 维护入口：真实布局 + 无 embedder


def test_maintenance_reclaims_orphan_on_real_layout(tmp_path) -> None:
    """owner 维护按 canonical 路径回收孤儿键，且不依赖 embedder（M1/M1b 的正式回归）。"""
    from agent_py_agent.agent.user_space.home_layout import ensure_my_agent_home
    from agent_py_agent.agent.user_space.owner_maintenance import run_owner_retention_if_due

    home = ensure_my_agent_home(tmp_path / "home")
    memory_path = home.owner_memory_long_term_jsonl
    mem = JsonlMemory(memory_path, embedder=LocalHashingEmbedder(dim=128))
    _seed(mem, ["团建活动定在周五下午"])
    mem.search_scoped("团建", top_k=3, predicate=_all)

    orphan = text_cache_key("deadbeef", "已经删除的旧门禁密码")
    data = json.loads((memory_path.parent / TEXT_CACHE_FILE).read_text(encoding="utf-8"))
    data[orphan] = [0.5] * 128
    (memory_path.parent / TEXT_CACHE_FILE).write_text(json.dumps(data), encoding="utf-8")

    policy = json.loads(home.owner_retention_json.read_text(encoding="utf-8"))
    policy.update({"maintenance_interval_seconds": 100})
    home.owner_retention_json.write_text(json.dumps(policy), encoding="utf-8")

    result = run_owner_retention_if_due(home, now=200_000)
    marker = json.loads((home.owner_data_dir / "maintenance.json").read_text(encoding="utf-8"))
    assert result.ran is True
    assert marker["text_vector_cache_reclaimed"] == 1  # 修复点：不再是假的 0
    after = json.loads((memory_path.parent / TEXT_CACHE_FILE).read_text(encoding="utf-8"))
    assert orphan not in after


def test_content_hash_retain_keeps_other_models_keys(tmp_path) -> None:
    """按正文哈希回收：正文仍 active 的键即使指纹不同也要保留（维护无 embedder 用的判据）。"""
    from agent_py_agent.agent.retrieval.text_vector_cache import text_content_hash

    path = tmp_path / TEXT_CACHE_FILE
    cache = TextVectorCache(path)
    text = "团建活动定在周五下午"
    keep_key = text_cache_key("fp-old-model", text)
    drop_key = text_cache_key("fp-old-model", "早已删除的事实")
    cache.put({keep_key: [1.0, 0.0], drop_key: [0.0, 1.0]})

    dropped = cache.retain_content_hashes({text_content_hash(text)})
    assert dropped == 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert keep_key in on_disk  # 正文仍 active → 保留（换过模型也一样）
    assert drop_key not in on_disk


# ------------------------------------------------ V4 / V5：跳过无意义重写、回收错误可见


def test_v4_remove_absent_key_does_not_rewrite_file(tmp_path) -> None:
    """删一个盘上本来就没有的键：文件 inode 与 mtime 都不变（V4，跳过整文件重写）。"""
    import os

    path = tmp_path / TEXT_CACHE_FILE
    cache = TextVectorCache(path)
    cache.put({text_cache_key("fp", "留下的那条"): [1.0, 0.0]})

    before = path.stat()
    os.utime(path, (before.st_atime - 10, before.st_mtime - 10))  # 挪旧时间戳，便于看出有没有被重写
    stamped = path.stat()

    dropped = cache.remove([text_cache_key("fp", "盘上根本没有这条")])

    after = path.stat()
    assert dropped == 0
    assert after.st_ino == stamped.st_ino, "文件被替换了，说明还是整文件重写"
    assert after.st_mtime == stamped.st_mtime, "mtime 变了，说明还是整文件重写"


def test_v4_remove_present_key_still_rewrites(tmp_path) -> None:
    """对照：删一个盘上确实有的键，必须真的落盘（跳过写不能把该写的也跳掉）。"""
    path = tmp_path / TEXT_CACHE_FILE
    cache = TextVectorCache(path)
    victim = text_cache_key("fp", "这条会被删掉")
    cache.put({victim: [1.0, 0.0]})

    dropped = cache.remove([victim])

    assert dropped == 1
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert victim not in on_disk


def test_v4_noop_remove_still_refreshes_memory_view(tmp_path) -> None:
    """跳过写盘时仍要用盘上内容刷新内存视图，不能让本实例视图与盘上脱节。"""
    path = tmp_path / TEXT_CACHE_FILE
    a = TextVectorCache(path)
    b = TextVectorCache(path)
    other = text_cache_key("fp", "另一个实例写的")
    a.put({other: [1.0, 0.0]})

    # b 内存里没有 other；删一个无关键会走"内容没变"分支，但内存视图要随之更新。
    b.remove([text_cache_key("fp", "不存在")])

    assert b.get([other]) == {other: [1.0, 0.0]}


def test_v5_reclaim_reports_error_when_cache_corrupt(tmp_path) -> None:
    """缓存文件读不出内容时，回收必须带回非空错误，不能报 0 让人以为"没有孤儿"（V5）。"""
    memory_path = tmp_path / "memory.jsonl"
    memory_path.write_text("", encoding="utf-8")
    (tmp_path / TEXT_CACHE_FILE).write_text("{ 这不是合法 JSON", encoding="utf-8")

    memory = JsonlMemory(memory_path)
    dropped, error = memory.reclaim_text_cache_orphans()

    assert dropped == 0
    assert error, "缓存读不出来却报了空错误——维护状态又会写成假的结构化事实"


def test_v5_reclaim_empty_error_on_clean_run(tmp_path) -> None:
    """对照：缓存正常时错误说明必须为空串，否则"分得开"就成了永远非空。"""
    memory_path = tmp_path / "memory.jsonl"
    memory_path.write_text("", encoding="utf-8")
    (tmp_path / TEXT_CACHE_FILE).write_text(json.dumps({}), encoding="utf-8")

    memory = JsonlMemory(memory_path)
    dropped, error = memory.reclaim_text_cache_orphans()

    assert (dropped, error) == (0, "")
