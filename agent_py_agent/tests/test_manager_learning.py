"""manager_learning 模块测试。

测试自学习候选草稿的生成、去重、确认和统计功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestNormalizeLearningText:
    """测试文本归一化函数。"""

    def test_normalize_basic(self):
        """测试基本归一化。"""
        from agent_py_agent.agent.subagents.manager_learning import _normalize_learning_text

        result = _normalize_learning_text("Hello World")
        assert result == "hello world"

    def test_normalize_whitespace(self):
        """测试多余空白字符处理。"""
        from agent_py_agent.agent.subagents.manager_learning import _normalize_learning_text

        result = _normalize_learning_text("Hello   World\n\tTest")
        assert "  " not in result

    def test_normalize_chinese(self):
        """测试中文处理。"""
        from agent_py_agent.agent.subagents.manager_learning import _normalize_learning_text

        result = _normalize_learning_text("你好世界")
        assert result == "你好世界"


class TestLearningTokens:
    """测试词元提取函数。"""

    def test_tokens_basic(self):
        """测试基本词元提取。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_tokens

        tokens = _learning_tokens("hello world")
        assert "w:hello" in tokens
        assert "c2:he" in tokens

    def test_tokens_empty(self):
        """测试空字符串。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_tokens

        tokens = _learning_tokens("")
        assert tokens == set()


class TestLearningSimilarity:
    """测试学习相似度计算函数。"""

    def test_similarity_identical(self):
        """测试完全相同的字符串相似度为1。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity("hello world", "hello world")
        assert result == 1.0

    def test_similarity_empty_left(self):
        """测试空字符串左侧返回0。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity("", "hello")
        assert result == 0.0

    def test_similarity_empty_right(self):
        """测试空字符串右侧返回0。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity("hello", "")
        assert result == 0.0

    def test_similarity_substring(self):
        """子串只作为相似信号，不再直接给硬编码高分。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity("hello", "hello world")
        assert 0.45 <= result < 0.9

    def test_similarity_long_context_containment_is_not_duplicate(self):
        """短教训出现在长报告里时，不应只靠包含关系当成同一条学习。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        short_lesson = "先确认真实路径再读文件"
        long_report = (
            "本轮复盘记录了很多互不相关的问题，包括模型网关错误、子代理状态树延迟、"
            "路径提示不稳定、产物注册表缺失、上下文压缩后的恢复顺序、日志归档策略、"
            "以及某个局部教训：先确认真实路径再读文件。后续还要继续观察其他模块。"
        )

        result = _learning_similarity(short_lesson, long_report)
        assert result < 0.45

    def test_similarity_near_duplicate_lesson_stays_high(self):
        """真正相近的学习条目仍应能合并。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity(
            "先确认真实路径再读文件",
            "执行前先确认真实路径，再读取文件",
        )
        assert result >= 0.45

    def test_similarity_reworded_lesson_stays_mergeable(self):
        """同一条经验换一种说法时，仍应超过候选合并阈值。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity(
            "先复现失败，再改代码，最后补一个最小回归测试",
            "遇到缺陷时先做最小复现，然后修改实现，最后补回归检查",
        )
        assert result >= 0.45

    def test_similarity_different(self):
        """测试完全不同字符串。"""
        from agent_py_agent.agent.subagents.manager_learning import _learning_similarity

        result = _learning_similarity("abc", "xyz")
        assert 0.0 <= result < 0.5


class TestCandidateConfidence:
    """测试候选置信度计算。"""

    def test_confidence_first_occurrence(self):
        """测试首次出现的置信度。"""
        from agent_py_agent.agent.subagents.manager_learning import _candidate_confidence

        result = _candidate_confidence(1)
        assert 0.45 <= result <= 0.57

    def test_confidence_multiple_occurrences(self):
        """测试多次出现的置信度递增。"""
        from agent_py_agent.agent.subagents.manager_learning import _candidate_confidence

        conf_1 = _candidate_confidence(1)
        conf_3 = _candidate_confidence(3)
        conf_5 = _candidate_confidence(5)
        assert conf_3 > conf_1
        assert conf_5 > conf_3

    def test_confidence_capped_at_095(self):
        """测试置信度上限为0.95。"""
        from agent_py_agent.agent.subagents.manager_learning import _candidate_confidence

        result = _candidate_confidence(100)
        assert result == 0.95


class TestNormalizeCandidate:
    """测试候选归一化函数。"""

    def test_normalize_candidate_basic(self):
        """测试基本候选归一化。"""
        from agent_py_agent.agent.subagents.manager_learning import _normalize_candidate

        payload = {
            "id": "learn_001",
            "lesson": "Test lesson",
            "normalized_key": "test lesson",
            "status": "draft",
            "confidence": 0.5,
            "occurrence_count": 1,
        }
        result = _normalize_candidate(payload)
        assert result.id == "learn_001"
        assert result.lesson == "Test lesson"
        assert result.status == "draft"

    def test_normalize_candidate_invalid_status(self):
        """测试无效状态默认值。"""
        from agent_py_agent.agent.subagents.manager_learning import _normalize_candidate

        payload = {
            "id": "learn_001",
            "lesson": "Test lesson",
            "status": "invalid_status",
        }
        result = _normalize_candidate(payload)
        assert result.status == "draft"

    def test_normalize_candidate_invalid_confidence(self):
        """测试无效置信度默认值。"""
        from agent_py_agent.agent.subagents.manager_learning import _normalize_candidate

        payload = {
            "id": "learn_001",
            "lesson": "Test lesson",
            "confidence": "invalid",
        }
        result = _normalize_candidate(payload)
        assert result.confidence == 0.5


class TestSubAgentLearningMixin:
    """测试 SubAgentLearningMixin 类。"""

    def test_learning_enabled_disabled(self, tmp_path: Path):
        """测试学习功能默认关闭。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path
                self.enable_self_learning = False

        manager = MockManager()
        assert manager.learning_enabled() is False

    def test_learning_enabled_true(self, tmp_path: Path):
        """测试学习功能开启。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path
                self.enable_self_learning = True

        manager = MockManager()
        assert manager.learning_enabled() is True

    def test_list_learning_candidates_empty(self, tmp_path: Path):
        """测试空候选列表。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        drafts_dir.mkdir(parents=True, exist_ok=True)
        result = manager.list_learning_candidates()
        assert result == []

    def test_list_learning_candidates_report_keeps_load_errors(self, tmp_path: Path):
        """坏学习草稿不应被伪装成没有候选。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        (drafts_dir / "broken.json").write_text("{", encoding="utf-8")
        (drafts_dir / "list.json").write_text("[]", encoding="utf-8")

        report = manager.list_learning_candidates_report()

        assert report.candidates == []
        assert len(report.load_errors) == 2
        assert {
            item["error"]["context"]
            for item in report.load_errors
            if isinstance(item.get("error"), dict)
        } == {"subagent_learning.candidates.load"}

    def test_learning_drafts_dir_creates_directory(self, tmp_path: Path):
        """测试学习草稿目录自动创建。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        assert drafts_dir.exists()
        assert drafts_dir.is_dir()







class TestSubAgentLearningCandidateMixin:
    """测试 SubAgentLearningMixin 类。"""

    def test_save_and_load_learning_candidate(self, tmp_path: Path):
        """测试保存和加载学习候选。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin
        from agent_py_agent.agent.subagents.models import LearningCandidate

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        drafts_dir.mkdir(parents=True, exist_ok=True)

        candidate = LearningCandidate(
            id="learn_test_001",
            lesson="Test lesson content",
            normalized_key="test lesson content",
            status="draft",
            confidence=0.5,
            occurrence_count=1,
            evidence_count=1,
            source_runs=["run_001"],
            evidence=[],
            variants=["Test lesson content"],
        )

        saved = manager.save_learning_candidate(candidate)
        assert saved.id == "learn_test_001"

        loaded = manager.load_learning_candidate("learn_test_001")
        assert loaded.lesson == "Test lesson content"
        assert loaded.status == "draft"

    def test_set_learning_candidate_status(self, tmp_path: Path):
        """测试设置候选状态。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin
        from agent_py_agent.agent.subagents.models import LearningCandidate

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        drafts_dir.mkdir(parents=True, exist_ok=True)

        candidate = LearningCandidate(
            id="learn_test_002",
            lesson="Test lesson",
            normalized_key="test lesson",
            status="draft",
        )
        manager.save_learning_candidate(candidate)

        updated = manager.set_learning_candidate_status("learn_test_002", "accepted")
        assert updated.status == "accepted"

    def test_set_learning_candidate_status_invalid(self, tmp_path: Path):
        """测试无效状态抛出异常。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin
        from agent_py_agent.agent.subagents.models import LearningCandidate

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        drafts_dir.mkdir(parents=True, exist_ok=True)

        candidate = LearningCandidate(
            id="learn_test_003",
            lesson="Test lesson",
            normalized_key="test lesson",
            status="draft",
        )
        manager.save_learning_candidate(candidate)

        with pytest.raises(ValueError, match="unsupported learning candidate status"):
            manager.set_learning_candidate_status("learn_test_003", "invalid_status")

    def test_learning_stats_empty(self, tmp_path: Path):
        """测试空统计。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        stats = manager.learning_stats()
        assert stats["total"] == 0
        assert stats["draft"] == 0
        assert stats["load_errors"] == []
        assert stats["accepted"] == 0

    def test_learning_stats_with_candidates(self, tmp_path: Path):
        """测试有候选时的统计。"""
        from agent_py_agent.agent.subagents.manager_learning import SubAgentLearningMixin
        from agent_py_agent.agent.subagents.models import LearningCandidate

        class MockManager(SubAgentLearningMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

        manager = MockManager()
        drafts_dir = manager.learning_drafts_dir()
        drafts_dir.mkdir(parents=True, exist_ok=True)

        for i in range(3):
            candidate = LearningCandidate(
                id=f"learn_stat_{i}",
                lesson=f"Lesson {i}",
                normalized_key=f"lesson {i}",
                status="draft" if i % 2 == 0 else "accepted",
                confidence=0.5 + i * 0.1,
            )
            manager.save_learning_candidate(candidate)

        stats = manager.learning_stats()
        assert stats["total"] == 3
        assert stats["draft"] == 2
        assert stats["accepted"] == 1
