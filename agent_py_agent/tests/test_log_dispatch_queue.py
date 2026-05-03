"""测试 log_analysis dispatch/queue.py

测试内存调查队列：
- InvestigationQueue: 队列入队出队、状态管理
- DispatchRequest: 调度请求数据结构
- add/add_pending: 添加请求
- get/for_case: 查询请求
- pending/active: 按状态筛选
- mark_dispatched/mark_rejected: 状态转换
- counts_by_status/agent_backlog: 统计
"""
from __future__ import annotations

import time
from typing import Any

import pytest

from agent_py_agent.agent.log_analysis.dispatch.queue import (
    InvestigationQueue,
    DispatchRequest,
    PENDING_INVESTIGATION,
    QUEUED,
    DISPATCHED,
    AWAITING_REVIEW,
    REVIEWED,
    REJECTED,
    FAILED,
    ACTIVE_STATUSES,
    _new_request_id,
)


# ============================================================
# 测试用例：InvestigatonQueue 初始化
# ============================================================

class TestQueueInit:
    """测试 InvestigationQueue 初始化"""

    def test_init_empty(self):
        """测试空初始化"""
        queue = InvestigationQueue()
        assert len(queue) == 0
        assert queue.requests == []

    def test_init_with_requests(self):
        """测试带请求初始化"""
        req = DispatchRequest(case_id="init-case-001")
        queue = InvestigationQueue(requests=[req])
        assert len(queue) == 1
        assert queue.requests[0].case_id == "init-case-001"

    def test_len(self):
        """测试 len 方法"""
        queue = InvestigationQueue()
        assert len(queue) == 0
        queue.add_pending(case_id="len-case-001")
        assert len(queue) == 1


# ============================================================
# 测试用例：DispatchRequest 数据结构
# ============================================================

class TestDispatchRequest:
    """测试 DispatchRequest 数据结构"""

    def test_default_values(self):
        """测试默认值"""
        req = DispatchRequest(case_id="default-case")
        assert req.status == PENDING_INVESTIGATION
        assert req.role == "analyst"
        assert req.priority == ""
        assert req.request_id.startswith("logdisp-")
        assert req.evidence_refs == []
        assert req.attempts == 0

    def test_custom_values(self):
        """测试自定义值"""
        req = DispatchRequest(
            case_id="custom-case",
            role="reviewer",
            priority="P0",
            evidence_refs=["ev-001", "ev-002"],
        )
        assert req.case_id == "custom-case"
        assert req.role == "reviewer"
        assert req.priority == "P0"
        assert len(req.evidence_refs) == 2

    def test_to_dict(self):
        """测试 to_dict 方法"""
        req = DispatchRequest(case_id="dict-case")
        d = req.to_dict()
        assert isinstance(d, dict)
        assert d["case_id"] == "dict-case"
        assert d["status"] == PENDING_INVESTIGATION


# ============================================================
# 测试用例：队列添加操作
# ============================================================

class TestQueueAdd:
    """测试队列添加操作"""

    def test_add_request(self):
        """测试添加请求对象"""
        queue = InvestigationQueue()
        req = DispatchRequest(case_id="add-case-001")
        result = queue.add(req)
        assert len(queue) == 1
        assert result.case_id == "add-case-001"

    def test_add_pending(self):
        """测试 add_pending 便捷方法"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="pending-case-001", priority="P1")
        assert req.case_id == "pending-case-001"
        assert req.priority == "P1"
        assert req.status == PENDING_INVESTIGATION

    def test_add_pending_with_evidence(self):
        """测试带证据的 add_pending"""
        queue = InvestigationQueue()
        req = queue.add_pending(
            case_id="evidence-case-001",
            evidence_refs=["ev-001", "ev-002"],
            case_summary={"title": "Test"},
        )
        assert req.evidence_refs == ["ev-001", "ev-002"]
        assert req.case_summary == {"title": "Test"}

    def test_add_updates_timestamp(self):
        """测试添加更新 updated_at"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="time-case-001")
        original = req.updated_at
        time.sleep(0.01)
        req2 = queue.add_pending(case_id="time-case-002")
        assert req2.updated_at >= original

    def test_add_multiple_requests(self):
        """测试添加多个请求"""
        queue = InvestigationQueue()
        for i in range(5):
            queue.add_pending(case_id=f"multi-case-{i}")
        assert len(queue) == 5


# ============================================================
# 测试用例：队列查询操作
# ============================================================

class TestQueueQuery:
    """测试队列查询操作"""

    def test_get_existing_request(self):
        """测试获取存在的请求"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="get-case-001")
        found = queue.get(req.request_id)
        assert found is not None
        assert found.case_id == "get-case-001"

    def test_get_nonexistent_request(self):
        """测试获取不存在的请求"""
        queue = InvestigationQueue()
        found = queue.get("nonexistent-id")
        assert found is None

    def test_for_case(self):
        """测试 for_case 查找"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="for-case-001")
        queue.add_pending(case_id="for-case-001")  # 同一 case_id 多个请求
        queue.add_pending(case_id="for-case-002")
        results = queue.for_case("for-case-001")
        assert len(results) == 2

    def test_for_case_nonexistent(self):
        """测试 for_case 查找不存在的 case"""
        queue = InvestigationQueue()
        results = queue.for_case("nonexistent")
        assert results == []


# ============================================================
# 测试用例：pending 和 active 筛选
# ============================================================

class TestQueueFilters:
    """测试 pending 和 active 筛选"""

    def test_pending_empty(self):
        """测试空队列的 pending"""
        queue = InvestigationQueue()
        assert queue.pending() == []

    def test_pending_returns_pending_requests(self):
        """测试 pending 返回 PENDING_INVESTIGATION 状态的请求"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="pending-case-001")
        queue.add_pending(case_id="pending-case-002")
        pending = queue.pending()
        assert len(pending) == 2
        assert all(r.status == PENDING_INVESTIGATION for r in pending)

    def test_pending_with_role_filter(self):
        """测试按 role 筛选 pending"""
        queue = InvestigationQueue()
        # 需要手动创建带 role 的请求
        queue.add(DispatchRequest(case_id="analyst-case-001", role="analyst"))
        queue.add(DispatchRequest(case_id="reviewer-case-001", role="reviewer"))
        analyst_pending = queue.pending(role="analyst")
        assert len(analyst_pending) == 1
        assert analyst_pending[0].case_id == "analyst-case-001"

    def test_active_empty(self):
        """测试空队列的 active"""
        queue = InvestigationQueue()
        assert queue.active() == []

    def test_active_returns_active_requests(self):
        """测试 active 返回活跃状态的请求"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="active-case-001")
        pending_req = queue.add_pending(case_id="active-case-002")
        # 标记为 DISPATCHED
        queue.mark_dispatched(pending_req.request_id, agent_id="agent-001")
        active = queue.active()
        assert len(active) == 1
        assert active[0].case_id == "active-case-002"

    def test_active_with_role_filter(self):
        """测试按 role 筛选 active"""
        queue = InvestigationQueue()
        r1 = queue.add(DispatchRequest(case_id="role-case-001", role="analyst"))
        queue.add(DispatchRequest(case_id="role-case-002", role="reviewer"))
        queue.mark_dispatched(r1.request_id, agent_id="agent-001")
        analyst_active = queue.active(role="analyst")
        assert len(analyst_active) == 1


# ============================================================
# 测试用例：dispatched_since
# ============================================================

class TestDispatchedSince:
    """测试 dispatched_since 方法"""

    def test_dispatched_since_empty(self):
        """测试空队列"""
        queue = InvestigationQueue()
        since = time.time() - 3600
        results = queue.dispatched_since(since)
        assert results == []

    def test_dispatched_since_returns_updated(self):
        """测试返回指定时间后更新的请求"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="since-case-001")
        queue.mark_dispatched(req.request_id, agent_id="agent-001")
        since = time.time() - 1  # 1秒前
        results = queue.dispatched_since(since)
        assert len(results) == 1

    def test_dispatched_since_excludes_old(self):
        """测试排除旧请求"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="old-case-001")
        queue.mark_dispatched(req.request_id, agent_id="agent-001")
        # 1小时前
        req.updated_at = time.time() - 7200
        since = time.time() - 3600
        results = queue.dispatched_since(since)
        assert len(results) == 0


# ============================================================
# 测试用例：状态转换
# ============================================================

class TestStateTransitions:
    """测试状态转换方法"""

    def test_mark_dispatched(self):
        """测试标记为已调度"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="dispatch-case-001")
        result = queue.mark_dispatched(req.request_id, agent_id="agent-001")
        assert result.status == DISPATCHED
        assert result.assigned_agent_id == "agent-001"
        assert result.attempts == 1

    def test_mark_dispatched_increments_attempts(self):
        """测试标记调度增加 attempts"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="attempts-case-001")
        queue.mark_dispatched(req.request_id, agent_id="agent-001")
        queue.mark_dispatched(req.request_id, agent_id="agent-002")
        found = queue.get(req.request_id)
        assert found.attempts == 2

    def test_mark_dispatched_unknown_raises(self):
        """测试标记未知请求抛出异常"""
        queue = InvestigationQueue()
        with pytest.raises(KeyError):
            queue.mark_dispatched("nonexistent-id", agent_id="agent")

    def test_mark_rejected(self):
        """测试标记为已拒绝"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="reject-case-001")
        result = queue.mark_rejected(req.request_id, reason="invalid case")
        assert result.status == REJECTED
        assert result.reason == "invalid case"

    def test_mark_rejected_unknown_raises(self):
        """测试拒绝未知请求抛出异常"""
        queue = InvestigationQueue()
        with pytest.raises(KeyError):
            queue.mark_rejected("nonexistent-id", reason="test")


# ============================================================
# 测试用例：统计方法
# ====================================

class TestStatistics:
    """测试统计方法"""

    def test_counts_by_status_empty(self):
        """测试空队列的状态计数"""
        queue = InvestigationQueue()
        counts = queue.counts_by_status()
        assert counts == {}

    def test_counts_by_status(self):
        """测试状态计数"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="count-001")
        queue.add_pending(case_id="count-002")
        r3 = queue.add_pending(case_id="count-003")
        queue.mark_dispatched(r3.request_id, agent_id="agent-001")
        counts = queue.counts_by_status()
        assert counts.get(PENDING_INVESTIGATION, 0) == 2
        assert counts.get(DISPATCHED, 0) == 1

    def test_agent_backlog(self):
        """测试 agent_backlog"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="backlog-001")
        queue.add_pending(case_id="backlog-002")
        r = queue.add(DispatchRequest(case_id="backlog-003", role="reviewer"))
        queue.mark_dispatched(r.request_id, agent_id="reviewer-001")
        backlog = queue.agent_backlog()
        assert backlog["total"] == 3
        assert backlog["pending_investigation"] == 2
        assert backlog["dispatched"] == 1
        assert backlog["active_analyst_agents"] == 0  # analyst 未被 dispatch


# ============================================================
# 测试用例：to_dict
# ====================================

class TestQueueToDict:
    """测试 to_dict 方法"""

    def test_to_dict_structure(self):
        """测试 to_dict 结构"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="dict-case-001")
        d = queue.to_dict()
        assert "summary" in d
        assert "requests" in d
        assert len(d["requests"]) == 1

    def test_to_dict_summary(self):
        """测试 to_dict 包含摘要"""
        queue = InvestigationQueue()
        queue.add_pending(case_id="summary-case-001")
        d = queue.to_dict()
        assert d["summary"]["total"] == 1


# ============================================================
# 测试用例：边界场景
# ====================================

class TestBoundaryCases:
    """测试边界场景"""

    def test_request_id_uniqueness(self):
        """测试 request_id 唯一性"""
        queue = InvestigationQueue()
        ids = set()
        for i in range(100):
            req = queue.add_pending(case_id=f"id-case-{i}")
            assert req.request_id not in ids
            ids.add(req.request_id)

    def test_pending_with_none_role(self):
        """测试 role=None 筛选"""
        queue = InvestigationQueue()
        queue.add(DispatchRequest(case_id="none-role-001", role="analyst"))
        queue.add(DispatchRequest(case_id="none-role-002", role="reviewer"))
        results = queue.pending(role=None)
        assert len(results) == 2

    def test_multiple_status_changes(self):
        """测试多次状态变更"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="multi-status-001")
        queue.mark_dispatched(req.request_id, agent_id="agent-001")
        queue.mark_rejected(req.request_id, reason="rejected after dispatch")
        found = queue.get(req.request_id)
        assert found.status == REJECTED

    def test_empty_case_id(self):
        """测试空 case_id"""
        queue = InvestigationQueue()
        req = queue.add_pending(case_id="")
        assert req.case_id == ""

    def test_active_statuses_constant(self):
        """测试 ACTIVE_STATUSES 包含正确状态"""
        assert QUEUED in ACTIVE_STATUSES
        assert DISPATCHED in ACTIVE_STATUSES
        assert AWAITING_REVIEW in ACTIVE_STATUSES