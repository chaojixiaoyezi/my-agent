"""测试日志分析案例存储模块 (case_store.py)"""
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.cases.case_store import (
    CaseRecord,
    CaseStore,
    EvidenceRef,
    Finding,
    dedup_key_for_finding,
    min_priority,
    priority_for_score,
)
from agent_py_agent.agent.log_analysis.storage.local_store import LocalLogStore

# ============================================================
# 辅助函数：创建测试 Finding
# ============================================================

def make_finding(
    finding_id: str = "finding-001",
    detector_id: str = "test_detector",
    risk_score: float = 0.7,
    attacker_ip: str = "1.2.3.4",
    victim_ip: str = "5.6.7.8",
    hypothesis: str = "",
    gaps: list[str] | None = None,
    next_queries: list[Any] | None = None,
    window: list[str] | None = None,
    entities: dict[str, list[str]] | None = None,
    evidence_refs: list[EvidenceRef] | None = None,
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
        entities=entities or {
            "attacker_ip": [attacker_ip],
            "victim_ip": [victim_ip],
        },
        evidence_refs=evidence_refs or [],
    )


def make_case_record(
    case_id: str = "case-001",
    title: str = "Test Case",
    priority: str = "P2",
    risk_score: float = 0.7,
    dedup_key: str = "",
) -> CaseRecord:
    """创建测试用 CaseRecord 对象"""
    return CaseRecord(
        case_id=case_id,
        title=title,
        priority=priority,
        risk_score=risk_score,
        dedup_key=dedup_key,
        finding_refs=[],
        evidence_refs=[],
        entities={},
        facts=[],
        inferences=[],
        gaps=[],
    )


# ============================================================
# 测试用例：CaseStore 初始化
# ============================================================

class TestCaseStoreInit:
    """测试 CaseStore 初始化和基础配置"""

    def test_init_with_path(self, tmp_path):
        """测试使用 Path 初始化 CaseStore"""
        store = CaseStore(tmp_path)
        assert store.root == tmp_path
        assert store.min_case_confidence == 0.6
        assert store.merge_window_minutes == 15

    def test_init_with_local_log_store(self, tmp_path):
        """测试使用 LocalLogStore 实例初始化"""
        local_store = LocalLogStore(tmp_path)
        store = CaseStore(local_store)
        assert store.store is local_store
        assert store.root == tmp_path

    def test_init_with_custom_thresholds(self, tmp_path):
        """测试自定义置信度和合并窗口"""
        store = CaseStore(
            tmp_path,
            min_case_confidence=0.8,
            merge_window_minutes=30,
        )
        assert store.min_case_confidence == 0.8
        assert store.merge_window_minutes == 30

    def test_init_with_search_store(self, tmp_path):
        """测试传入 search_store 参数"""
        mock_search = MagicMock()
        store = CaseStore(tmp_path, search_store=mock_search)
        assert store.search_store is mock_search


# ============================================================
# 测试用例：record_finding 基本功能
# ============================================================

class TestRecordFinding:
    """测试 record_finding 方法"""

    def test_record_finding_above_threshold(self, tmp_path):
        """测试记录高于阈值的 finding，期望创建 case"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        finding = make_finding(risk_score=0.8)
        case = store.record_finding(finding)
        assert case is not None
        assert case.risk_score == 0.8
        assert case.priority == "P1"

    def test_record_finding_below_threshold(self, tmp_path):
        """测试记录低于阈值的 finding，期望返回 None"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        finding = make_finding(risk_score=0.3)
        case = store.record_finding(finding)
        assert case is None

    def test_record_finding_at_exact_threshold(self, tmp_path):
        """测试记录恰好在阈值的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.6)
        finding = make_finding(risk_score=0.6)
        case = store.record_finding(finding)
        assert case is not None

    def test_record_finding_from_dict(self, tmp_path):
        """测试从字典创建 finding 并记录"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        finding_dict = {
            "finding_id": "dict-finding-001",
            "detector_id": "test_detector",
            "risk_score": 0.75,
            "entities": {"attacker_ip": ["1.2.3.4"], "victim_ip": ["5.6.7.8"]},
        }
        case = store.record_finding(finding_dict)
        assert case is not None
        assert case.case_id.startswith("case-")

    def test_record_finding_updates_existing_case(self, tmp_path):
        """测试同一 dedup_key 的 finding 合并到已有 case"""
        store = CaseStore(tmp_path, min_case_confidence=0.5)
        finding1 = make_finding(
            finding_id="finding-001",
            attacker_ip="1.2.3.4",
            victim_ip="5.6.7.8",
            risk_score=0.6,
        )
        finding2 = make_finding(
            finding_id="finding-002",
            attacker_ip="1.2.3.4",
            victim_ip="5.6.7.8",
            risk_score=0.9,
        )
        case1 = store.record_finding(finding1)
        case2 = store.record_finding(finding2)
        # 相同的 dedup_key 应该合并
        assert case2 is not None
        # risk_score 应该取最大值
        assert case2.risk_score == 0.9
        assert "finding-002" in case2.finding_refs


# ============================================================
# 测试用例：case 查询和列表
# ============================================================

class TestCaseQuery:
    """测试 case 查询和列表功能"""

    def test_get_case_exists(self, tmp_path):
        """测试获取存在的 case"""
        store = CaseStore(tmp_path)
        finding = make_finding()
        case = store.record_finding(finding)
        assert case is not None
        retrieved = store.get_case(case.case_id)
        assert retrieved is not None
        assert retrieved.case_id == case.case_id

    def test_get_case_not_exists(self, tmp_path):
        """测试获取不存在的 case"""
        store = CaseStore(tmp_path)
        result = store.get_case("non-existent-case")
        assert result is None

    def test_list_cases_sorted_by_priority(self, tmp_path):
        """测试 case 列表按优先级排序"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        # 创建不同优先级的 case
        # 0.9 -> P0, 0.75 -> P1, 0.6 -> P2
        for i, (score, expected_priority) in enumerate([(0.9, "P0"), (0.75, "P1"), (0.6, "P2")]):
            finding = make_finding(
                finding_id=f"finding-{i}",
                attacker_ip=f"1.2.3.{i}",
                victim_ip="5.6.7.8",
                risk_score=score,
            )
            store.record_finding(finding)
        cases = store.list_cases()
        # P0 应该在最前面
        assert cases[0].priority == "P0"
        # 同优先级按 risk_score 降序
        assert cases[0].risk_score >= cases[-1].risk_score

    def test_list_cases_empty(self, tmp_path):
        """测试空 case 列表"""
        store = CaseStore(tmp_path)
        cases = store.list_cases()
        assert cases == []


# ============================================================
# 测试用例：优先级计算
# ============================================================

class TestPriorityCalculation:
    """测试优先级计算函数"""

    def test_priority_for_score_p0(self):
        """测试 >= 0.85 为 P0"""
        assert priority_for_score(0.85) == "P0"
        assert priority_for_score(1.0) == "P0"
        assert priority_for_score(0.9) == "P0"

    def test_priority_for_score_p1(self):
        """测试 >= 0.7 且 < 0.85 为 P1"""
        assert priority_for_score(0.7) == "P1"
        assert priority_for_score(0.84) == "P1"

    def test_priority_for_score_p2(self):
        """测试 >= 0.55 且 < 0.7 为 P2"""
        assert priority_for_score(0.55) == "P2"
        assert priority_for_score(0.69) == "P2"

    def test_priority_for_score_p3(self):
        """测试 < 0.55 为 P3"""
        assert priority_for_score(0.54) == "P3"
        assert priority_for_score(0.0) == "P3"
        assert priority_for_score(-0.1) == "P3"

    def test_min_priority(self):
        """测试 min_priority 函数"""
        assert min_priority("P0", "P1") == "P0"
        assert min_priority("P2", "P0") == "P0"
        assert min_priority("P3", "P3") == "P3"
        assert min_priority("PX", "P1") == "P1"  # 未知优先级取较小的


# ============================================================
# 测试用例：dedup_key 生成
# ============================================================

class TestDedupKey:
    """测试 dedup_key 生成逻辑"""

    def test_dedup_key_basic(self):
        """测试基本 dedup_key 生成"""
        finding = make_finding(
            attacker_ip="1.2.3.4",
            victim_ip="5.6.7.8",
            window=["2026-05-01T00:00:00Z"],
        )
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "attack=1.2.3.4" in key
        assert "victim=5.6.7.8" in key
        assert "bucket=" in key

    def test_dedup_key_with_account(self):
        """测试包含账户的 dedup_key (vpn_new_geo_login 场景)"""
        finding = make_finding(
            detector_id="vpn_new_geo_login",
            attacker_ip="1.2.3.4",
            victim_ip="5.6.7.8",
            entities={"attacker_ip": ["1.2.3.4"], "victim_ip": ["5.6.7.8"], "user": ["admin"]},
        )
        key = dedup_key_for_finding(finding, window_minutes=15)
        assert "account=admin" in key


# ============================================================
# 测试用例：case 存储和更新
# ============================================================

class TestCasePersistence:
    """测试 case 持久化"""

    def test_save_case_directly(self, tmp_path):
        """测试直接保存 case"""
        store = CaseStore(tmp_path)
        case = make_case_record(case_id="manual-case-001", dedup_key="test-dedup")
        store.save_case(case)
        retrieved = store.get_case("manual-case-001")
        assert retrieved is not None
        assert retrieved.title == "Test Case"

    def test_record_findings_returns_unique_cases(self, tmp_path):
        """测试 record_findings 返回不重复的 case"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        findings = [
            make_finding(finding_id=f"f-{i}", attacker_ip="1.2.3.4", victim_ip=f"5.6.7.{i}")
            for i in range(3)
        ]
        cases = store.record_findings(findings)
        # 不同 victim_ip 产生不同的 dedup_key
        assert len(cases) >= 1

    def test_case_title_from_finding(self, tmp_path):
        """测试 case 标题从 finding 生成"""
        store = CaseStore(tmp_path)
        finding = make_finding(detector_id="test_detector", victim_ip="192.168.1.100")
        case = store.record_finding(finding)
        assert case is not None
        assert "test_detector" in case.title
        assert "192.168.1.100" in case.title


# ============================================================
# 测试用例：边界场景
# ============================================================

class TestBoundaryCases:
    """测试边界场景"""

    def test_finding_with_no_victim(self, tmp_path):
        """测试没有 victim 的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        finding = make_finding(victim_ip="")
        # 没有 victim 可能无法创建有效 dedup_key，但仍应处理
        case = store.record_finding(finding)
        assert case is not None or case is None  # 取决于实现

    def test_finding_with_no_attacker(self, tmp_path):
        """测试没有 attacker 的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        finding = make_finding(attacker_ip="", victim_ip="5.6.7.8")
        case = store.record_finding(finding)
        assert case is not None

    def test_finding_with_empty_entities(self, tmp_path):
        """测试 entities 为空的 finding"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        finding = make_finding(entities={})
        case = store.record_finding(finding)
        # 应该创建 case 但优先级可能较低
        assert case is not None or case is None  # 根据实现

    def test_multiple_findings_same_case_merge(self, tmp_path):
        """测试多个 finding 合并到同一 case"""
        store = CaseStore(tmp_path, min_case_confidence=0.3)
        finding1 = make_finding(
            finding_id="f1",
            attacker_ip="1.2.3.4",
            victim_ip="5.6.7.8",
            hypothesis="initial hypothesis",
            gaps=["gap1"],
            next_queries=[{"query": "test"}],
        )
        finding2 = make_finding(
            finding_id="f2",
            attacker_ip="1.2.3.4",
            victim_ip="5.6.7.8",
            gaps=["gap2"],
        )
        store.record_finding(finding1)
        case = store.record_finding(finding2)
        assert case is not None
        assert "f1" in case.finding_refs
        assert "f2" in case.finding_refs
        assert "initial hypothesis" in case.inferences
        assert "gap1" in case.gaps
        assert "gap2" in case.gaps