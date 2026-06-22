"""VectorStore 健壮性(保证后续不出问题):换模型维度守卫 / 坏数据不连累 / 重载 / 并发写不坏文件。

本地 per-owner 向量库是语义召回的存储底座。生产里真会遇到:换 embedding 模型(维度变)、文件被旧版本/
半写污染、同 owner 并发写。这些都不该静默错召回或崩或丢数据。
"""

from __future__ import annotations

import json
import threading

from agent_py_agent.agent.retrieval.vector_store import VectorStore


def test_dimension_switch_skips_mismatched_vectors(tmp_path) -> None:
    vs = VectorStore(tmp_path / "v.json")
    vs.upsert("old", [1.0, 0.0])  # 旧模型 2 维
    vs.upsert("new", [1.0, 0.0, 0.0, 0.0])  # 新模型 4 维
    hits = vs.search([1.0, 0.0, 0.0, 0.0], top_k=5)  # 4 维 query
    assert [h.id for h in hits] == ["new"]  # ⭐ 旧维度向量被跳过,不做截断比较(否则静默垃圾分错召回)


def test_corrupt_vector_entry_does_not_kill_search(tmp_path) -> None:
    vs = VectorStore(tmp_path / "v.json")
    vs.upsert("good", [1.0, 0.0])
    vs._items["bad_nonnum"] = {"vector": ["x", "y"], "text": "", "metadata": {}}  # 非数值向量
    vs._items["bad_type"] = {"vector": "not-a-list", "text": "", "metadata": {}}  # 非 list
    hits = vs.search([1.0, 0.0], top_k=5)
    assert [h.id for h in hits] == ["good"]  # ⭐ 单条坏向量被跳过,不连累整库语义召回


def test_corrupt_json_file_loads_as_empty(tmp_path) -> None:
    path = tmp_path / "v.json"
    path.write_text("{ this is not valid json", encoding="utf-8")
    vs = VectorStore(path)  # 半写/损坏文件
    assert len(vs) == 0  # 不崩,当空库;后续写入可恢复
    vs.upsert("a", [1.0, 0.0])
    assert len(vs) == 1


def test_reload_preserves_vectors_and_search(tmp_path) -> None:
    path = tmp_path / "v.json"
    vs = VectorStore(path)
    for i, vec in enumerate([[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]):
        vs.upsert(f"m{i}", vec, text=f"t{i}", metadata={"i": i})
    reloaded = VectorStore(path)  # 模拟进程重启:新实例同文件
    assert len(reloaded) == 3
    hits = reloaded.search([1.0, 0.0], top_k=1)
    assert hits and hits[0].id == "m0" and hits[0].metadata["i"] == 0  # 重载后向量+元数据完好


def test_empty_query_and_empty_store_are_graceful(tmp_path) -> None:
    vs = VectorStore(tmp_path / "v.json")
    assert vs.search([1.0, 0.0], top_k=5) == []  # 空库不崩
    vs.upsert("a", [1.0, 0.0])
    assert vs.search([], top_k=5) == []  # 空 query 向量(维度 0)→ 全不匹配,不崩不误召回


def test_search_at_scale_stays_correct_and_fast(tmp_path) -> None:
    """千级规模:暴力 cosine 仍把目标顶第一、延迟可接受——验证"百~千条够用,不必上 ANN"的设计判断。"""
    import time

    from agent_py_agent.agent.retrieval.embedding import LocalHashingEmbedder

    emb = LocalHashingEmbedder(dim=256)
    vs = VectorStore(tmp_path / "v.json")
    rows = [(f"m{i}", emb.embed([f"无关记忆条目{i}内容各异随机"])[0], f"d{i}", {"i": i}) for i in range(2000)]
    vs.upsert_many(rows)  # 一次 flush 灌 2000 条
    target = "支付系统迁移预算五十万这是独一无二的目标句"
    vs.upsert("TARGET", emb.embed([target])[0], text=target, metadata={"t": True})

    t0 = time.perf_counter()
    hits = vs.search(emb.embed([target])[0], top_k=3)
    dt = time.perf_counter() - t0
    assert hits[0].id == "TARGET"  # ⭐ 2001 条暴力扫仍正确命中目标
    assert dt < 2.0, f"千级暴力 cosine 延迟过高:{dt:.3f}s"  # 延迟可接受


def test_concurrent_upsert_and_search_no_corruption(tmp_path) -> None:
    path = tmp_path / "v.json"
    vs = VectorStore(path)
    errors: list[Exception] = []

    def worker(w: int) -> None:
        try:
            for j in range(10):
                vs.upsert(f"t{w}-{j}", [float(w), float(j), 1.0], text=f"{w}-{j}", metadata={"w": w})
                vs.search([1.0, 1.0, 1.0], top_k=3)  # 并发读写交织
        except Exception as exc:  # 收集而非吞,断言无错
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 upsert/search 不应出错(快照+唯一tmp):{errors[:3]}"
    assert len(vs) == 200  # 20×10 全部成功,无丢(共享 dict 在 GIL 下安全)
    assert len(VectorStore(path)) == 200  # ⭐ 文件未被并发 flush 损坏:新实例完整加载
    json.loads(path.read_text(encoding="utf-8"))  # 文件是合法 JSON(未被截断/交织)
