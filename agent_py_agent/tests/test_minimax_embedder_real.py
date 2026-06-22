"""#1 P2 真机:MiniMax embo-01 真打云端,验语义召回端到端(换词也能召回)。

真打 MiniMax 云、计费,故 gated:需 RUN_MINIMAX_REAL=1 且 AGENT_API_KEY。CI/常规 gate 自动 skip。
验两件事:① embo-01 真出 1536 维、语义相近的换词分更高;② JsonlMemory 接 MiniMax 后,纯关键词召不回
的换词 query 也能召回目标——这正是 #1 P2(治"换词就召不回")的最终验收。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("RUN_MINIMAX_REAL") and os.environ.get("AGENT_API_KEY")),
    reason="需 RUN_MINIMAX_REAL=1 且 AGENT_API_KEY(真打 MiniMax 云,计费)",
)

_API_BASE = "https://api.minimaxi.com/v1"


def _embedder():
    from agent_py_agent.agent.retrieval.embedding import MiniMaxEmbedder

    return MiniMaxEmbedder(api_base=_API_BASE, model="embo-01", api_key=os.environ["AGENT_API_KEY"])


def test_real_embo_dim_and_semantic_ordering() -> None:
    from agent_py_agent.agent.retrieval.embedding import cosine

    vecs = _embedder().embed(["项目总预算是五十万元", "这个项目一共花了大概五十万", "今天北京天气晴朗"])
    assert all(len(v) == 1536 for v in vecs)  # embo-01 真出 1536 维
    related = cosine(vecs[0], vecs[1])  # 同义换词
    unrelated = cosine(vecs[0], vecs[2])  # 无关
    assert related > unrelated  # ⭐ 语义相近的换词相似度更高


def test_real_memory_paraphrase_recall(tmp_path) -> None:
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    mem = JsonlMemory(tmp_path / "mem.jsonl", embedder=_embedder())
    mem.add("user", "客户要求本季度内完成支付系统的迁移")  # 目标
    for distractor in (
        "团建活动定在下周五下午",
        "新版本发布说明需要补充截图",
        "财务报销流程改成线上提交",
        "前端页面的暗色主题还没做",
        "招聘的面试安排在周三上午",
    ):
        mem.add("user", distractor)

    # query 全换词:付款≈支付、模块≈系统、搬完≈迁移——与目标零共享关键词,纯 BM25 召不回
    hits = mem.search("付款模块大概什么时候能搬完", top_k=2)
    assert any("支付系统" in r.content for r in hits)  # ⭐ 换词 query 也能召回目标(语义路生效)


def test_real_i18n_batch_robust() -> None:
    """服务几十国语言:多语言 + emoji + 混排 批量 embed,N 进 N 出、全 1536 维,不崩不串位。"""
    texts = [
        "项目上线 🚀 deadline is Q3",
        "日本語のテストです",
        "한국어 메모 테스트",
        "Café résumé naïve façade",
        "混合中英文 mixed text 123 🎉",
        "Здравствуйте мир",
    ]
    vecs = _embedder().embed(texts)
    assert len(vecs) == len(texts) and all(len(v) == 1536 for v in vecs)  # ⭐ 多语言/emoji 批量稳健


def test_real_empty_and_oversized_inputs_dont_crash() -> None:
    """边界输入(空 / 纯空白 / 超长超 token 上限):要么有效向量、要么抛 EmbeddingError 供降级,绝不挂死/崩。"""
    from agent_py_agent.agent.retrieval.embedding import EmbeddingError

    for text in ["", "   ", "支付系统迁移" * 5000]:  # 超长 ≈ 远超 embo-01 token 上限
        try:
            vecs = _embedder().embed([text])
            assert not vecs or all(len(v) == 1536 for v in vecs)  # 有效则必 1536 维
        except EmbeddingError:
            pass  # 端点拒绝边界输入 → 抛错供上层降级 BM25,符合预期
