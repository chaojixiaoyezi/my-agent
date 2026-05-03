"""测试 log_analysis dispatch/engine.py

测试调度引擎核心逻辑：
- DispatchEngine: 案例提交、预算控制、调度决策
- submit_case: 提交案例到队列
- enqueue_case: 公开协议别名
- health: 健康状态汇总
- build_analyst_input: 构建分析输入
- review_report: 报告审核
- DispatchResult: 调度结果数据结构
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.dispatch.budgets import BudgetDecision, DispatchBudget
from agent_py_agent.agent.log_analysis.dispatch.engine import (
    DispatchEngine,
    DispatchResult,
    _case_id,
    _evidence_refs_from_summary,
    _get,
    _priority,
)
from agent_py_agent.agent.log_analysis.dispatch.queue import (
    DISPATCHED,
    PENDING_INVESTIGATION,
    QUEUED,
    DispatchRequest,
    InvestigationQueue,
)

# ============================================================
# 测试用例：DispatchEngine 初始化
# ============================================================

class TestDispatchEngineInit:
    """测试 DispatchEngine 初始化"""

    def test_init_with_no_args(self):
        """测试无参数初始化"""
        engine = DispatchEngine()
        assert engine.queue is not None
        assert isinstance(engine.queue, InvestigationQueue)
        assert engine.budget is not None

    def test_init_with_queue(self):
        """测试传入自定义队列"""
        queue = InvestigationQueue()
        engine = DispatchEngine(queue=queue)
        # engine 应该使用传入的队列（或基于它）
        assert engine.queue is not None
        assert isinstance(engine.queue, InvestigationQueue)

    def test_init_with_budget(self):
        """测试传入自定义预算"""
        budget = DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=2,
            analyst_agent_budget_per_hour=5,
        )
        engine = DispatchEngine(budget=budget)
        assert engine.budget.case_auto_dispatch_enabled is True
        assert engine.budget.max_parallel_analyst_agents == 2

    def test_init_with_budget_mapping(self):
        """测试使用字典初始化预算"""
        engine = DispatchEngine(budget={"max_parallel_analyst_agents": 3})
        assert engine.budget.max_parallel_analyst_agents == 3


# ============================================================
# 测试用例：_case_id 辅助函数
# ============================================================

class TestCaseIdHelper:
    """测试 _case_id 辅助函数"""

    def test_case_id_from_case_field(self):
        """测试从 case 字段获取 case_id"""
        case = {"case_id": "case-001"}
        summary = {}
        assert _case_id(case, summary) == "case-001"

    def test_case_id_from_id_field(self):
        """测试从 id 字段获取 case_id"""
        case = {"id": "case-002"}
        summary = {}
        assert _case_id(case, summary) == "case-002"

    def test_case_id_from_summary(self):
        """测试从 summary 获取 case_id"""
        case = {}
        summary = {"case": {"case_id": "case-003"}}
        assert _case_id(case, summary) == "case-003"

    def test_case_id_unknown_when_missing(self):
        """测试缺失时返回 unknown-case"""
        case = {}
        summary = {}
        assert _case_id(case, summary) == "unknown-case"


# ============================================================
# 测试用例：_priority 辅助函数
# ============================================================

class TestPriorityHelper:
    """测试 _priority 辅助函数"""

    def test_priority_from_case_priority(self):
        """测试从 case.priority 获取"""
        case = {"priority": "P0"}
        summary = {}
        assert _priority(case, summary) == "P0"

    def test_priority_from_case_severity(self):
        """测试从 case.severity 获取（作为优先级别名）"""
        case = {"severity": "high"}
        summary = {}
        assert _priority(case, summary) == "high"

    def test_priority_from_summary(self):
        """测试从 summary 获取"""
        case = {}
        summary = {"case": {"priority": "P1"}}
        assert _priority(case, summary) == "P1"

    def test_priority_empty_when_missing(self):
        """测试缺失时返回空字符串"""
        case = {}
        summary = {}
        assert _priority(case, summary) == ""


# ============================================================
# 测试用例：_evidence_refs_from_summary 辅助函数
# ============================================================

class TestEvidenceRefsHelper:
    """测试 _evidence_refs_from_summary 函数"""

    def test_refs_from_case_evidence_refs(self):
        """测试从 case.evidence_refs 获取"""
        case = {"evidence_refs": ["ev-001", "ev-002"]}
        summary = {}
        result = _evidence_refs_from_summary(case, summary)
        assert "ev-001" in result
        assert "ev-002" in result

    def test_refs_from_case_evidence(self):
        """测试从 case.evidence 获取（别名）"""
        case = {"evidence": ["ev-003"]}
        summary = {}
        result = _evidence_refs_from_summary(case, summary)
        assert "ev-003" in result

    def test_refs_from_summary_evidence(self):
        """测试从 summary.evidence 获取"""
        case = {}
        summary = {"evidence": ["ev-004"]}
        result = _evidence_refs_from_summary(case, summary)
        assert "ev-004" in result

    def test_refs_empty_when_missing(self):
        """测试全部缺失时返回空列表"""
        case = {}
        summary = {}
        result = _evidence_refs_from_summary(case, summary)
        assert result == []


# ============================================================
# 测试用例：DispatchEngine.submit_case
# ============================================================

class TestSubmitCase:
    """测试 submit_case 方法"""

    def test_submit_case_not_dispatched_when_budget_exhausted(self):
        """测试预算耗尽时不调度"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=0,  # 禁用
        ))
        case = {"case_id": "case-001", "priority": "P1"}
        result = engine.submit_case(case)
        assert result.dispatched is False
        # 原因消息包含 "disables" 表示被禁用
        assert "disables" in result.reason.lower() or "exhausted" in result.reason.lower()

    def test_submit_case_dispatched_when_budget_allows(self):
        """测试预算允许时立即调度"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": "case-002", "priority": "P1"}
        result = engine.submit_case(case)
        assert result.dispatched is True
        assert result.agent_id.startswith("analyst-")

    def test_submit_case_adds_to_queue(self):
        """测试提交案例添加到队列"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": "case-003"}
        engine.submit_case(case)
        # 调度允许时请求被标记为 DISPATCHED，所以检查队列长度
        assert len(engine.queue) >= 1

    def test_submit_case_with_evidence_refs(self):
        """测试带证据引用的案例"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": "case-004", "evidence_refs": ["ev-001", "ev-002"]}
        result = engine.submit_case(case)
        assert result.request.evidence_refs == ["ev-001", "ev-002"]

    def test_submit_case_priority_extracted(self):
        """测试优先级提取"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": "case-005", "priority": "P0"}
        result = engine.submit_case(case)
        assert result.request.priority == "P0"


# ============================================================
# 测试用例：DispatchEngine.enqueue_case
# ============================================================

class TestEnqueueCase:
    """测试 enqueue_case 方法"""

    def test_enqueue_case_is_alias_for_submit_case(self):
        """测试 enqueue_case 是 submit_case 的别名"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": "case-enqueue-001"}
        result1 = engine.submit_case(case)
        # 重置引擎
        engine2 = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        result2 = engine2.enqueue_case(case)
        assert result1.case_id == result2.case_id
        assert result1.dispatched == result2.dispatched

    def test_enqueue_case_accepts_context(self):
        """测试 enqueue_case 接受 context 参数（忽略）"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": "case-ctx-001"}
        result = engine.enqueue_case(case, context={"extra": "data"})
        assert result.case_id == "case-ctx-001"


# ============================================================
# 测试用例：DispatchEngine.health
# ============================================================

class TestHealth:
    """测试 health 方法"""

    def test_health_returns_dict(self):
        """测试 health 返回字典"""
        engine = DispatchEngine()
        h = engine.health()
        assert isinstance(h, dict)

    def test_health_contains_expected_keys(self):
        """测试 health 包含预期键"""
        engine = DispatchEngine()
        h = engine.health()
        assert "case_backlog" in h
        assert "agent_backlog" in h
        assert "dispatch_budget" in h

    def test_health_reflects_queue_state(self):
        """测试 health 反映队列状态"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        engine.submit_case({"case_id": "health-case-001"})
        h = engine.health()
        # queue 有 1 个请求
        assert h["agent_backlog"].get("total", 0) >= 1


# ============================================================
# 测试用例：DispatchEngine.build_analyst_input
# ============================================================

class TestBuildAnalystInput:
    """测试 build_analyst_input 方法"""

    def test_build_analyst_input_basic(self):
        """测试基本构建"""
        engine = DispatchEngine(budget=DispatchBudget(
            max_case_rounds=3,
            analyst_agent_timeout_seconds=300,
        ))
        request = DispatchRequest(
            case_id="case-input-001",
            evidence_refs=["ev-001"],
            case_summary={"title": "Test Case"},
            route_summary={"entry_candidates": []},
        )
        result = engine.build_analyst_input(request)
        assert result["case_id"] == "case-input-001"
        assert "ev-001" in result["evidence_refs"]
        assert "available_tools" in result

    def test_build_analyst_input_includes_budget(self):
        """测试输入包含预算信息"""
        engine = DispatchEngine(budget=DispatchBudget(
            max_case_rounds=5,
            analyst_agent_timeout_seconds=600,
        ))
        request = DispatchRequest(
            case_id="case-budget-001",
            evidence_refs=[],
        )
        result = engine.build_analyst_input(request)
        assert result["budget"]["max_case_rounds"] == 5
        assert result["budget"]["timeout_seconds"] == 600


# ============================================================
# 测试用例：DispatchEngine.review_report
# ============================================================

class TestReviewReport:
    """测试 review_report 方法"""

    def test_review_report_calls_review_analyst_report(self):
        """测试 review_report 调用正确函数"""
        engine = DispatchEngine()
        # 有效报告应该被批准
        report = {
            "case_id": "review-case-001",
            "summary": "Test analysis",
            "evidence_refs": ["ev-known"],
            "facts": ["fact 1"],
        }
        result = engine.review_report(report, known_evidence_refs=["ev-known"])
        assert result["approved"] is True

    def test_review_report_rejects_unknown_evidence(self):
        """测试未知证据被拒绝"""
        engine = DispatchEngine()
        report = {
            "case_id": "review-case-002",
            "summary": "Test analysis",
            "evidence_refs": ["ev-unknown"],
            "facts": ["fact 1"],
        }
        result = engine.review_report(report, known_evidence_refs=["ev-known"])
        assert result["approved"] is False


# ============================================================
# 测试用例：DispatchResult 数据结构
# ============================================================

class TestDispatchResult:
    """测试 DispatchResult 数据结构"""

    def test_dispatch_result_properties(self):
        """测试 DispatchResult 属性"""
        request = DispatchRequest(case_id="result-case-001")
        result = DispatchResult(request=request, dispatched=True, reason="ok", agent_id="agent-001")
        assert result.case_id == "result-case-001"
        assert result.status == PENDING_INVESTIGATION
        assert result.run_id == "agent-001"

    def test_dispatch_result_message(self):
        """测试 message 属性"""
        request = DispatchRequest(case_id="msg-case-001")
        result = DispatchResult(request=request, dispatched=False, reason="budget exhausted")
        assert result.message == "budget exhausted"

    def test_dispatch_result_metadata(self):
        """测试 metadata 属性"""
        request = DispatchRequest(
            case_id="meta-case-001",
            priority="P0",
            evidence_refs=["ev-001"],
        )
        result = DispatchResult(request=request, dispatched=True, reason="ok", agent_id="agent-002")
        meta = result.metadata
        assert meta["priority"] == "P0"
        assert "ev-001" in meta["evidence_refs"]
        assert meta["agent_id"] == "agent-002"

    def test_dispatch_result_to_dict(self):
        """测试 to_dict 方法"""
        request = DispatchRequest(case_id="dict-case-001")
        result = DispatchResult(request=request, dispatched=True, reason="ok")
        d = result.to_dict()
        assert d["case_id"] == "dict-case-001"
        assert d["dispatched"] is True


# ============================================================
# 测试用例：边界场景
# ============================================================

class TestBoundaryCases:
    """测试边界场景"""

    def test_submit_case_with_empty_case_id(self):
        """测试空 case_id"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": ""}
        result = engine.submit_case(case)
        # 应该仍然创建请求，只是 case_id 为空

    def test_submit_case_with_none_values(self):
        """测试 None 值"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=5,
            analyst_agent_budget_per_hour=10,
        ))
        case = {"case_id": None, "priority": None}
        result = engine.submit_case(case)

    def test_dispatch_result_run_id_fallback(self):
        """测试 run_id 降级到 request_id"""
        request = DispatchRequest(case_id="fallback-case-001")
        result = DispatchResult(request=request, dispatched=True, reason="ok", agent_id="")
        assert result.run_id == request.request_id

    def test_budget_disabled_for_all_priorities(self):
        """测试所有优先级都被禁用"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=False,
            p0_auto_dispatch_enabled=False,
            p1_auto_dispatch_enabled=False,
        ))
        case = {"case_id": "disabled-case-001", "priority": "P0"}
        result = engine.submit_case(case)
        assert result.dispatched is False

    def test_concurrent_submissions(self):
        """测试并发提交（顺序模拟）"""
        engine = DispatchEngine(budget=DispatchBudget(
            case_auto_dispatch_enabled=True,
            max_parallel_analyst_agents=2,
            analyst_agent_budget_per_hour=10,
        ))
        for i in range(5):
            engine.submit_case({"case_id": f"concurrent-case-{i}"})
        # 队列中应该有 5 个请求
        assert len(engine.queue) == 5