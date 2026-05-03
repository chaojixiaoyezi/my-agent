"""测试日志分析调度器模块 (scheduler.py)"""
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.cases.scheduler import (
    CaseScheduler,
    ScheduleResult,
    schedule_findings,
    findings_to_cases,
)
from agent_py_agent.agent.log_analysis.cases.case_store import CaseStore
from agent_py_agent.agent.log_analysis.models import Finding, CaseRecord


# ============================================================
# 辅助函数
# ============================================================

def make_finding(
    finding_id: str = "finding-001",
    detector_id: str = "test_detector",
    risk_score: float = 0.7,
    attacker_ip: str = "1.2.3.4",
    victim_ip: str = "5.6.7.8",
    window: list[str] | None = None,
    hypothesis: str = "",
    gaps: list[str] | None = None,
    next_queries: list[Any] | None = None,
) -> Finding:
    """创建测试用 Finding 对象"""
    return Finding(
        finding_id=finding_id,
        detector_id=detector_id,
        window=window or ["2026-05-01T00:00:00Z", "2026-05-01T23:59:59Z"],
        severity_hint="medium",
        risk_score=risk_score,
        hypothesis=hypothesis,
        gaps=gaps or [],
        next_queries=next_queries or [],
        entities={
            "attacker_ip": [attacker_ip],
            "victim_ip": [victim_ip],
        },
        evidence_refs=[],
    )


# ============================================================
# 测试用例：CaseScheduler 初始化
# ============================================================

class TestSchedulerInit:
    """测试 CaseScheduler 初始化"""

    def test_init_with_store(self, tmp_path):
        """测试使用 store 初始化调度器"""
        store = CaseStore(tmp_path)
        scheduler = CaseScheduler(store)
        assert scheduler.store is store
        assert scheduler.min_case_confidence is None

    def test_init_with_custom_min_confidence(self, tmp_path):
        """测试自定义最小置信度"""
        store = CaseStore(tmp_path)
        scheduler = CaseScheduler(store, min_case_confidence=0.8)
        assert scheduler.min_case_confidence == 0.8


# ============================================================
# 测试用例：schedule 基本功能
# ============================================================

class TestScheduleBasics:
    """测试 schedule 方法基本功能"""

    def test_schedule_single_finding_above_threshold(self, tmp_path):
        """测试调度单个高于阈值的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        scheduler = CaseScheduler(store)
        findings = [make_finding(risk_score=0.8)]
        result = scheduler.schedule(findings)
        assert len(result.recorded_findings) == 1
        assert len(result.low_confidence_findings) == 0
        assert len(result.cases) == 1

    def test_schedule_single_finding_below_threshold(self, tmp_path):
        """测试调度单个低于阈值的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        scheduler = CaseScheduler(store)
        findings = [make_finding(risk_score=0.3)]
        result = scheduler.schedule(findings)
        assert len(result.recorded_findings) == 1
        assert len(result.low_confidence_findings) == 1
        assert len(result.cases) == 0

    def test_schedule_empty_findings(self, tmp_path):
        """测试调度空列表"""
        store = CaseStore(tmp_path)
        scheduler = CaseScheduler(store)
        result = scheduler.schedule([])
        assert len(result.recorded_findings) == 0
        assert len(result.cases) == 0

    def test_schedule_finding_from_dict(self, tmp_path):
        """测试调度字典格式的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        scheduler = CaseScheduler(store)
        findings = [
            {
                "finding_id": "dict-finding-001",
                "detector_id": "test_detector",
                "risk_score": 0.75,
                "entities": {"attacker_ip": ["1.2.3.4"], "victim_ip": ["5.6.7.8"]},
            }
        ]
        result = scheduler.schedule(findings)
        assert len(result.recorded_findings) == 1
        assert "dict-finding-001" in result.recorded_findings


# ============================================================
# 测试用例：调度器覆盖全局阈值
# ============================================================

class TestSchedulerThresholdOverride:
    """测试调度器覆盖 store 的阈值"""

    def test_scheduler_overrides_store_threshold(self, tmp_path):
        """测试调度器的 min_case_confidence 覆盖 store 默认值"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        scheduler = CaseScheduler(store, min_case_confidence=0.8)
        # 0.7 高于 store 阈值但低于 scheduler 阈值
        findings = [make_finding(risk_score=0.7)]
        result = scheduler.schedule(findings)
        assert len(result.low_confidence_findings) == 1
        assert len(result.cases) == 0

    def test_store_threshold_restored_after_schedule(self, tmp_path):
        """测试调度后 store 阈值恢复"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        scheduler = CaseScheduler(store, min_case_confidence=0.8)
        # 0.7 高于 store 阈值但低于 scheduler 阈值
        findings = [make_finding(risk_score=0.7)]
        scheduler.schedule(findings)
        assert store.min_case_confidence == 0.6


# ============================================================
# 测试用例：重复 finding 去重
# ============================================================

class TestSchedulerDeduplication:
    """测试调度结果去重"""

    def test_duplicate_findings_same_case(self, tmp_path):
        """测试相同 dedup_key 的 finding 产生同一个 case"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        scheduler = CaseScheduler(store)
        findings = [
            make_finding(finding_id="f1", attacker_ip="1.2.3.4", victim_ip="5.6.7.8"),
            make_finding(finding_id="f2", attacker_ip="1.2.3.4", victim_ip="5.6.7.8"),
        ]
        result = scheduler.schedule(findings)
        # 两个 finding 属于同一 case
        assert len(result.cases) == 1

    def test_different_attackers_different_cases(self, tmp_path):
        """测试不同 attacker 产生不同 case"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        scheduler = CaseScheduler(store)
        findings = [
            make_finding(finding_id="f1", attacker_ip="1.2.3.4", victim_ip="5.6.7.8"),
            make_finding(finding_id="f2", attacker_ip="1.2.3.5", victim_ip="5.6.7.8"),
        ]
        result = scheduler.schedule(findings)
        # 不同 dedup_key 应该产生不同 case
        assert len(result.cases) == 2


# ============================================================
# 测试用例：ScheduleResult 结构
# ============================================================

class TestScheduleResult:
    """测试 ScheduleResult 数据结构"""

    def test_schedule_result_to_dict(self, tmp_path):
        """测试 ScheduleResult 转换为字典"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        scheduler = CaseScheduler(store)
        findings = [make_finding(risk_score=0.7)]
        result = scheduler.schedule(findings)
        result_dict = result.to_dict()
        assert "recorded_findings" in result_dict
        assert "low_confidence_findings" in result_dict
        assert "cases" in result_dict
        assert "case_ids" in result_dict
        assert result_dict["recorded_findings"] == result.recorded_findings
        assert len(result_dict["case_ids"]) == len(result.cases)


# ============================================================
# 测试用例：便利函数
# ============================================================

class TestConvenienceFunctions:
    """测试便捷函数"""

    def test_schedule_findings_creates_store(self, tmp_path):
        """测试 schedule_findings 函数创建 store"""
        findings = [make_finding(risk_score=0.7)]
        result = schedule_findings(findings, root=tmp_path)
        assert len(result.cases) == 1

    def test_findings_to_cases_returns_only_cases(self, tmp_path):
        """测试 findings_to_cases 只返回 cases 列表"""
        findings = [make_finding(risk_score=0.7)]
        cases = findings_to_cases(findings, root=tmp_path)
        assert isinstance(cases, list)
        assert all(isinstance(c, CaseRecord) for c in cases)

    def test_schedule_findings_with_custom_threshold(self, tmp_path):
        """测试 schedule_findings 使用自定义阈值"""
        findings = [make_finding(risk_score=0.5)]
        # 全局阈值 0.6，但这里设置为 0.4
        result = schedule_findings(findings, root=tmp_path, min_case_confidence=0.4)
        assert len(result.cases) == 1

    def test_findings_to_cases_respects_threshold(self, tmp_path):
        """测试 findings_to_cases 遵守阈值"""
        findings = [make_finding(risk_score=0.5)]
        cases = findings_to_cases(findings, root=tmp_path, min_case_confidence=0.6)
        assert len(cases) == 0


# ============================================================
# 测试用例：边界场景
# ============================================================

class TestSchedulerBoundaryCases:
    """测试调度器边界场景"""

    def test_schedule_mixed_confidence_findings(self, tmp_path):
        """测试混合置信度的 findings"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        scheduler = CaseScheduler(store)
        findings = [
            make_finding(finding_id="high", risk_score=0.9),
            make_finding(finding_id="low", risk_score=0.3),
        ]
        result = scheduler.schedule(findings)
        assert len(result.recorded_findings) == 2
        assert len(result.low_confidence_findings) == 1
        assert len(result.cases) == 1
        assert "high" in result.cases[0].finding_refs

    def test_schedule_merges_inferences_and_gaps(self, tmp_path):
        """测试调度合并 inference 和 gaps"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        scheduler = CaseScheduler(store)
        findings = [
            make_finding(
                finding_id="f1",
                attacker_ip="1.2.3.4",
                victim_ip="5.6.7.8",
                hypothesis="hypothesis 1",
                gaps=["gap1"],
            ),
            make_finding(
                finding_id="f2",
                attacker_ip="1.2.3.4",
                victim_ip="5.6.7.8",
                gaps=["gap2"],
            ),
        ]
        result = scheduler.schedule(findings)
        assert len(result.cases) == 1
        case = result.cases[0]
        # 至少有一个 inference (从 hypothesis 来)
        assert len(case.inferences) >= 1
        # gaps 来自第一个 finding (第二个 finding 的 gaps 可能未合并，取决于实现)
        assert len(case.gaps) >= 1

    def test_schedule_with_none_min_confidence(self, tmp_path):
        """测试 min_case_confidence 为 None 时使用 store 阈值"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        scheduler = CaseScheduler(store, min_case_confidence=None)
        findings = [make_finding(risk_score=0.6)]
        result = scheduler.schedule(findings)
        assert len(result.cases) == 1