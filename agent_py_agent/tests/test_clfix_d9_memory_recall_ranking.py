"""D-9：新记忆冷热分层、融合候选补回强词面命中、同分按检索名次、普通对话跨项目召回需词面证据。"""

from __future__ import annotations

from agent_py_agent.agent.memory_store.generation_manifest import partition_records
from agent_py_agent.agent.memory_store.jsonl import (
    JsonlMemory,
    MemoryRecord,
    _rerank_memory_records,
)
from agent_py_agent.agent.memory_store.recall import (
    MemoryRecallScope,
    runtime_long_term_scope,
    runtime_recall_predicate,
)


# LLM: 只按正文标记返回固定三维向量，不访问网络；目标与查询反向，填充项与查询同向。
# 类用途: 让向量臂确定性地排除目标、收进全部填充项。
class _MarkerEmbedder:
    # LLM: 无副作用，写入与查询共用同一向量空间。
    # 函数用途: 目标正文给反向向量，其余文本给查询方向向量。
    def embed(self, texts):
        return [[0.0, 1.0, 0.0] if "功能矩阵" in text else [0.0, 0.0, 1.0] for text in texts]


# LLM: 从未被召回的新记录按写入时间参与 LRU，不能按 0 当作最久未用而一写入就转冷。
# 函数用途: 覆盖新写入、已访问旧记录和从未访问旧记录三者的淘汰顺序。
def test_never_accessed_new_record_ages_from_its_write_time():
    accessed_old = MemoryRecord("user", "a" * 10, entry_id="accessed-old", updated_at=50.0, last_accessed_at=100.0)
    fresh = MemoryRecord("user", "b" * 10, entry_id="fresh", updated_at=200.0)
    active, cold = partition_records([accessed_old, fresh], 10)
    assert [record.entry_id for record in active] == ["fresh"]
    assert [record.entry_id for record in cold] == ["accessed-old"]

    stale = MemoryRecord("user", "c" * 10, entry_id="never-read-stale", updated_at=10.0)
    active, cold = partition_records([accessed_old, stale], 10)
    assert [record.entry_id for record in active] == ["accessed-old"]
    assert [record.entry_id for record in cold] == ["never-read-stale"]


# LLM: 目标是唯一同时命中两个查询词的记录，但向量臂低于门槛；12 条只命中一个词的填充项两臂都在，
#   RRF 会把目标挤出 max(top_k*4, top_k+8) 候选。修复后按词面名次补回，词面优先 rerank 让它排第一。
# 函数用途: 端到端走真实 BM25、向量臂、RRF 和 rerank，证明强词面命中不再被融合丢弃。
def test_scoped_search_keeps_top_lexical_hit_that_fusion_drops(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl", embedder=_MarkerEmbedder())
    memory.add_record(MemoryRecord("user", "用户长期偏好：跨语言移植项目都必须交付一份功能矩阵。", entry_id="target"))
    for index in range(12):
        memory.add_record(MemoryRecord("user", f"服务部署记录{index}：交付窗口在周末", entry_id=f"filler-{index:02d}"))

    selected = memory.search_scoped_candidates("交付 偏好", 2, lambda record: True)

    assert [record.entry_id for record in selected][:1] == ["target"]


# LLM: 候选已按检索相关度排好时，词面同分、来源同权先看名次；默认调用保持原"先新后旧"。
# 函数用途: 直接约束 rerank 新旧两种排序口径。
def test_rerank_prefers_retrieval_order_over_recency_only_when_ordered():
    relevant_old = MemoryRecord("user", "用户偏好：交付时附带功能矩阵", entry_id="relevant-old", updated_at=100.0)
    unrelated_new = MemoryRecord("user", "端口 6120 的服务已上线", entry_id="unrelated-new", updated_at=900.0)
    candidates = [relevant_old, unrelated_new]

    ordered = _rerank_memory_records(candidates, "工作偏好 交付约定", 1, retrieval_ordered=True)
    legacy = _rerank_memory_records(candidates, "工作偏好 交付约定", 1)

    assert [record.entry_id for record in ordered] == ["relevant-old"]
    assert [record.entry_id for record in legacy] == ["unrelated-new"]


# LLM: 普通对话并入的其它项目范围只在查询共享词元时入选；本轮自身项目、personal 不受影响，范围相等比较不变。
# 函数用途: 用真实 scope_pairs 构造兼容范围并核对收窄只发生在兼容项目上。
def test_compat_project_scope_needs_lexical_evidence(tmp_path):
    memory = JsonlMemory(tmp_path / "memory.jsonl")
    personal = memory.add_record(MemoryRecord("user", "用户偏好：报告用表格汇总", entry_id="personal-pref",
        attributes={"scope_type": "personal", "scope_key": "personal"}))
    project = memory.add_record(MemoryRecord("user", "Project PYCHIBICC 是 chibicc（C 编译器）的 Python 移植",
        entry_id="chibicc-port", attributes={"scope_type": "project", "scope_key": "project:chibicc-port",
                                             "keywords_en": ["pychibicc", "chibicc", "python"]}))

    scope = runtime_long_term_scope(memory)
    assert ("project", "project:chibicc-port") in scope.keys
    assert scope.compat_keys == (("project", "project:chibicc-port"),)
    assert runtime_recall_predicate(scope, "C 编译器")(project) is True
    assert scope == MemoryRecallScope(scope.keys)

    english = memory.add_record(MemoryRecord("user", "Project KESTREL is the gopher-lua port for the team",
        entry_id="kestrel-port", attributes={"scope_type": "project", "scope_key": "project:kestrel"}))
    scope = runtime_long_term_scope(memory)
    unrelated = runtime_recall_predicate(scope, "what is the rust tokenizer benchmark")
    assert unrelated(personal) is True
    assert unrelated(project) is False
    assert unrelated(english) is False
    assert runtime_recall_predicate(scope, "gopher-lua port status")(english) is True
    related = runtime_recall_predicate(scope, "chibicc 移植进度")
    assert related(project) is True
    assert runtime_recall_predicate(scope, "C 编译器", kind="lesson")(project) is False

    own = runtime_long_term_scope(memory, task_attributes={"project_id": "project:chibicc-port"})
    assert own.compat_keys == (("project", "project:kestrel"),)
    assert runtime_recall_predicate(own, "rust tokenizer benchmark")(project) is True
