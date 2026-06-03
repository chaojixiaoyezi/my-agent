"""Capability service routing tests.

测试能力请求处理、授权管理、gap记录功能。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _capability_service(tmp_path: Path, selected_runs=None):
    from agent_py_agent.agent.subagents.services.capabilities import SubAgentCapabilityService

    class MockManager:
        def __init__(self):
            self.workspace = tmp_path
            self.workspace_root = tmp_path
            self._selected_runs = list(selected_runs or [])

        def _select_runs(self, run_ids):
            return list(self._selected_runs)

        def _new_id(self, prefix):
            return f"{prefix}_001"

    manager = MockManager()
    return manager, SubAgentCapabilityService(manager)


class TestCapabilityRequestQuery:
    """测试能力请求查询构建函数。"""

    def test_capability_request_query_basic(self):
        """测试基本查询构建。"""
        from agent_py_agent.agent.subagents.models import CapabilityRequest, SubAgentTask
        from agent_py_agent.agent.subagents.policies import _capability_request_query

        task = SubAgentTask(
            id="task_001",
            goal="测试目标",
            thought="测试思考",
            plan=["步骤1", "步骤2"],
        )
        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="缺少文件搜索能力",
            needed_capability="file_search",
            expected_output="搜索结果列表",
        )

        result = _capability_request_query(task, request)

        assert result is not None
        assert len(result) > 0


class TestSelectCapabilityHits:
    """测试能力命中选择函数。"""

    def test_select_hits_empty(self):
        """测试空命中列表。"""
        from agent_py_agent.agent.subagents.policies import _select_capability_hits

        cfg = MagicMock()
        cfg.capability_min_score = 0.3

        hits = []
        result = _select_capability_hits(hits, cfg)
        assert result == []

    def test_select_hits_with_candidates(self):
        """测试有候选时的选择。"""
        from agent_py_agent.agent.subagents.policies import _select_capability_hits

        cfg = MagicMock()
        cfg.capability_min_score = 0.3

        hit1 = MagicMock()
        hit1.score = 0.8
        hit1.card.name = "search_tool"

        hit2 = MagicMock()
        hit2.score = 0.5
        hit2.card.name = "another_tool"

        hits = [hit1, hit2]
        result = _select_capability_hits(hits, cfg)

        assert result is not None


class TestRouteCardPayload:
    """测试能力卡负载构建函数。"""

    def test_route_card_payload_basic(self):
        """测试基本负载构建。"""
        from agent_py_agent.agent.subagents.policies import _route_card_payload

        hit = MagicMock()
        hit.card.name = "test_tool"
        hit.card.kind = "tool"
        hit.card.description = "Test tool description"
        hit.score = 0.9
        hit.reasons = ["匹配关键词", "高置信度"]

        result = _route_card_payload(hit)

        assert result["name"] == "test_tool"
        assert result["kind"] == "tool"
        # score 可能被格式化为字符串
        assert result["score"] in (0.9, "0.90")


class TestCapabilityRoutingPolicies:
    """测试能力路由策略函数。"""

    def test_dedupe_granted_cards_empty(self):
        """测试空授权卡列表去重。"""
        from agent_py_agent.agent.subagents.models import CapabilityGrant
        from agent_py_agent.agent.subagents.policies import _dedupe_granted_cards

        grants = []
        result = _dedupe_granted_cards(grants)
        assert result == []

    def test_dedupe_granted_cards_with_duplicates(self):
        """测试带重复卡的去重。"""
        from agent_py_agent.agent.subagents.models import CapabilityGrant
        from agent_py_agent.agent.subagents.policies import _dedupe_granted_cards

        grant1 = CapabilityGrant(
            id="grant_001",
            request_id="req_001",
            grant_to_run_id="run_001",
            capability_cards=[
                {"kind": "tool", "name": "test_tool"},
            ],
        )
        grant2 = CapabilityGrant(
            id="grant_002",
            request_id="req_002",
            grant_to_run_id="run_001",
            capability_cards=[
                {"kind": "tool", "name": "test_tool"},
                {"kind": "skill", "name": "test_skill"},
            ],
        )

        grants = [grant1, grant2]
        result = _dedupe_granted_cards(grants)

        assert len(result) == 2


class TestCapabilityRouteService:
    """测试 SubAgentCapabilityService 的基本方法。"""

    def test_route_capability_requests_empty(self, tmp_path: Path):
        """测试无请求时的路由报告。"""
        _, service = _capability_service(tmp_path)
        router = MagicMock()

        report = service.route_capability_requests(
            router=router,
            apply=False,
            run_ids=None,
            limit=0,
        )

        assert report.summary["total"] == 0
        assert report.dry_run is True

    def test_route_capability_requests_with_limit(self, tmp_path: Path):
        """测试带限制的路由报告。"""
        _, service = _capability_service(tmp_path)
        router = MagicMock()

        report = service.route_capability_requests(
            router=router,
            apply=False,
            limit=5,
        )

        assert report.summary["total"] == 0


class TestCapabilityModels:
    """测试能力相关模型。"""

    def test_capability_request_model(self):
        """测试 CapabilityRequest 模型。"""
        from agent_py_agent.agent.subagents.models import CapabilityRequest

        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="缺少文件搜索能力",
            needed_capability="test_cap",
        )

        assert request.id == "req_001"
        assert request.needed_capability == "test_cap"
        assert request.status == "OPEN"

    def test_capability_request_defaults(self):
        """测试 CapabilityRequest 默认值。"""
        from agent_py_agent.agent.subagents.models import CapabilityRequest

        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="缺少能力",
            needed_capability="test_cap",
        )

        assert request.status == "OPEN"
        assert request.tried == []

    def test_grant_command_allowlist_excludes_delete_commands(self):
        """删除类命令不能作为 shell 白名单授权，只能走 task trash。"""
        from agent_py_agent.agent.subagents.capability_scope import grant_command_allowlist
        from agent_py_agent.agent.subagents.models import CapabilityRequest

        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="需要受控 shell 和删除替代验证",
            needed_capability="controlled_exec",
            requested_commands=["pwd", "rm", "python3", "unlink", "rmdir"],
        )

        assert grant_command_allowlist(request) == ["pwd", "python3"]

    def test_grant_command_allowlist_normalizes_full_command_strings(self):
        """模型申请完整 shell 命令时，grant 只下发 shell gateway 会检查的 base command。"""
        from agent_py_agent.agent.subagents.capability_scope import grant_command_allowlist
        from agent_py_agent.agent.subagents.models import CapabilityRequest

        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="需要受控 shell 跑完整命令",
            needed_capability="controlled_exec",
            requested_commands=["pwd", "rm sentinel.txt", "python3 -c \"print('x' * 2000)\"", "python3"],
        )

        assert grant_command_allowlist(request) == ["pwd", "python3"]

    def test_rm_only_request_reuses_existing_controlled_exec_grant(self, tmp_path: Path):
        """已有 controlled_exec grant 时，裸 rm 追加申请应复用旧 grant，不生成空白 shell grant。"""
        manager, task, grant, rm_request = _manager_with_rm_reuse_request(tmp_path)

        record = manager._route_capability_request(
            task=manager.load(task.id),
            request=rm_request,
            query="rm stale.txt controlled_exec",
            hits=[],
            selected_hits=[],
            apply=True,
        )
        loaded = manager.load(task.id)

        assert record.status == "GRANTED"
        assert record.grant_id == grant.id
        assert record.message == "已有 capability grant 覆盖该请求。"
        assert len(loaded.capability_grants) == 1
        assert loaded.capability_requests[1].status == "GRANTED"


def _manager_with_rm_reuse_request(tmp_path: Path):
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.services.lifecycle import (
        RecordCapabilityGrantParams,
        RecordCapabilityRequestParams,
    )

    manager = SubAgentManager(tmp_path)
    task = manager.create_run(goal="controlled exec task", thought="trash deletes", plan=["grant"])
    first_request = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="need controlled exec",
            needed_capability="controlled_exec",
            capability_type="shell",
            requested_tools=["controlled_exec"],
            requested_commands=["pwd", "python3", "rm"],
            path_scope=[str(tmp_path)],
        ),
    )
    grant = manager.record_capability_grant(
        task.id,
        RecordCapabilityGrantParams(
            request_id=first_request.id,
            grant_type="shell",
            tools=["controlled_exec"],
            command_allowlist=["pwd", "python3"],
            path_scope=[str(tmp_path)],
        ),
    )
    rm_request = manager.record_capability_request(
        task.id,
        RecordCapabilityRequestParams(
            problem="rm not in shell allowlist",
            needed_capability="controlled_exec",
            capability_type="shell",
            requested_tools=["controlled_exec"],
            requested_commands=["rm stale.txt"],
            path_scope=[str(tmp_path)],
        ),
    )
    return manager, task, grant, rm_request


class TestCapabilityRoutingDryRun:
    """测试能力路由 dry-run 行为。"""

    def test_route_capability_request_no_hits_returns_would_gap(self, tmp_path: Path):
        """测试无命中时返回 WOULD_GAP。"""
        from agent_py_agent.agent.subagents.models import CapabilityRequest, SubAgentTask
        _, service = _capability_service(tmp_path)

        task = SubAgentTask(
            id="test_task",
            goal="test goal",
            thought="test thought",
            plan=["step1", "step2"],
        )
        task.capability_requests = []

        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="test problem",
            needed_capability="test_cap",
            status="OPEN",
        )

        result = service._route_capability_request(
            task=task,
            request=request,
            query="test query",
            hits=[],
            selected_hits=[],
            apply=False,
        )

        assert result.status == "WOULD_GAP"
        assert result.dry_run is True

    def test_route_capability_request_with_hits_returns_would_grant(self, tmp_path: Path):
        from agent_py_agent.agent.subagents.models import CapabilityRequest, SubAgentTask
        _, service = _capability_service(tmp_path)

        task = SubAgentTask(
            id="test_task",
            goal="test goal",
            thought="test thought",
            plan=["step1", "step2"],
        )
        task.capability_requests = []

        request = CapabilityRequest(
            id="req_001",
            from_run_id="run_001",
            problem="test problem",
            needed_capability="test_cap",
            status="OPEN",
        )

        mock_hit = MagicMock()
        mock_hit.card.name = "test_tool"
        mock_hit.card.kind = "tool"
        mock_hit.reasons = ["匹配"]
        mock_hit.score = 0.95

        result = service._route_capability_request(
            task=task,
            request=request,
            query="test query",
            hits=[mock_hit],
            selected_hits=[mock_hit],
            apply=False,
        )

        assert result.status == "WOULD_GRANT"
        assert result.dry_run is True


class TestCapabilityGapAndGrant:
    """测试能力 gap 和 grant 记录方法。"""

    def test_record_capability_gap_signature(self, tmp_path: Path):
        """测试 record_capability_gap 方法存在且可调用。"""
        from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

        class MockManager(SubAgentLifecycleMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

            def _new_id(self, prefix):
                return f"{prefix}_001"

        manager = MockManager()
        # 方法存在即可，不实际调用（需要 load/save 等基础设施）
        assert hasattr(manager, 'record_capability_gap')
        assert callable(manager.record_capability_gap)

    def test_record_capability_grant_signature(self, tmp_path: Path):
        """测试 record_capability_grant 方法存在且可调用。"""
        from agent_py_agent.agent.subagents.manager_lifecycle import SubAgentLifecycleMixin

        class MockManager(SubAgentLifecycleMixin):
            def __init__(self):
                self.workspace = tmp_path
                self.workspace_root = tmp_path

            def _new_id(self, prefix):
                return f"{prefix}_001"

        manager = MockManager()
        # 方法存在即可，不实际调用（需要 load/save 等基础设施）
        assert hasattr(manager, 'record_capability_grant')
        assert callable(manager.record_capability_grant)


class TestCapabilityRouteSummary:
    """测试能力路由报告摘要。"""

    def test_route_report_summary_counts(self, tmp_path: Path):
        """测试路由报告摘要计数。"""
        _, service = _capability_service(tmp_path)
        router = MagicMock()

        report = service.route_capability_requests(
            router=router,
            apply=False,
        )

        assert "total" in report.summary
        assert report.dry_run is True
