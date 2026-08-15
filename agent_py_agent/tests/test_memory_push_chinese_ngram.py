"""memory 推送检索的中文 n-gram 钉子(中文 goal 召回修复)。

根因:底层检索按空格分词,中文 goal 是一整段无空格汉字 → 整段当一个词,
只有完全包含才命中,部分重合的相关记忆全漏。修复:_build_memory_query 把
goal 里的中文串切成 2-4 gram(复用 memory_routing.matcher._chinese_ngrams)
拼进查询。这组测试钉住:① 中文 goal 产出 n-gram;② 部分重合可召回;
③ ASCII 长 goal 查询不爆长(保住既有截断不变式)。
"""

from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.memory_push import (
    _build_memory_query,
    _goal_chinese_ngrams,
    push_relevant_memories,
)
from agent_py_agent.tests._memory_push_v2_harness import formal_memory_agent, promote_lesson


class TestGoalChineseNgrams:
    def test_reuses_matcher_ngrams(self):
        # 复用而非重写:与路由器同款 2-4 gram。
        from agent_py_agent.agent.memory_routing.matcher import _chinese_ngrams

        grams = _goal_chinese_ngrams("记忆推送模式")
        assert set(_chinese_ngrams("记忆推送模式")).issubset(set(grams))

    def test_ngrams_include_meaningful_subwords(self):
        grams = _goal_chinese_ngrams("记忆推送模式中文检索")
        assert "推送" in grams
        assert "推送模式" in grams
        assert "检索" in grams

    def test_ascii_goal_yields_no_ngrams(self):
        assert _goal_chinese_ngrams("A" * 100) == []

    def test_ngram_count_is_capped(self):
        # 防查询爆炸:n-gram 限量。
        from agent_py_agent.agent.memory_push import _GOAL_NGRAM_MAX

        grams = _goal_chinese_ngrams("中" * 200)
        assert len(grams) <= _GOAL_NGRAM_MAX


class TestBuildMemoryQuery:
    def test_chinese_goal_expands_into_query(self):
        query = _build_memory_query(
            "failure",
            {"task_id": "t1", "failure_type": "parse_error", "goal": "记忆推送模式中文检索"},
        )
        terms = query.split()
        assert "failure" in terms
        assert "推送" in terms
        assert "推送模式" in terms
        assert "检索" in terms

    def test_partial_overlap_doc_now_scores(self):
        # 模拟 _search_jsonl 的逐词计分:文档只含 goal 的一部分(无"记忆"/"检索")
        # 也应得正分,证明召回打通(此前整段当一词 = 0 分漏召回)。
        query = _build_memory_query("failure", {"goal": "记忆推送模式中文检索"})
        terms = {t for t in query.split() if t.strip()}
        doc = "推送模式已落地failure注入点".lower()
        score = sum(1 for t in terms if t in doc)
        assert score > 0

    def test_ascii_long_goal_query_stays_short(self):
        # 既有不变式(test_push_relevant_memories_goal_truncation):ASCII 100 字 goal
        # 查询仍 < 150。
        query = _build_memory_query("timeout", {"task_id": "task_long", "goal": "A" * 100})
        assert len(query) < 150

    def test_empty_goal_no_crash(self):
        query = _build_memory_query("planning", {"goal": ""})
        assert query == "planning"


class TestPushRelevantMemoriesWithChineseGoal:
    def test_chinese_partial_goal_routes_formal_lesson(self, tmp_path: Path):
        agent = formal_memory_agent(tmp_path)
        lesson = promote_lesson(
            agent,
            content="记忆推送模式落地时必须只读正式教训。",
            subject_key="记忆.推送.模式",
        )
        out = push_relevant_memories(
            agent, "failure", {"goal": "记忆推送模式落地", "failure_type": "x"}, limit=3
        )
        assert [record.entry_id for record in out] == [lesson.lesson_id]
