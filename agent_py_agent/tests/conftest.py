"""共享测试 fixtures - 为测试提供通用对象和 mock。"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.log_analysis.dispatch.budgets import DispatchBudget
from agent_py_agent.agent.log_analysis.models import CaseRecord, EvidenceRef, Finding
from agent_py_agent.agent.log_analysis.security.attack_chain import AttackChainStep
from agent_py_agent.agent.subagents.models import SubAgentTask

# ---------------------------------------------------------------------------
# Finding / Evidence fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_evidence_ref() -> EvidenceRef:
    """创建示例 EvidenceRef。"""
    return EvidenceRef(
        evidence_id="ev-sample-001",
        kind="event",
        source_id="waf-01",
        raw_ref="raw-001",
        summary="sample evidence",
    )


@pytest.fixture
def sample_finding(make_finding_func) -> Finding:
    """创建示例 Finding（使用 factory 函数）。"""
    return make_finding_func()


@pytest.fixture
def make_finding_func():
    """Factory 函数：创建自定义 Finding。"""
    def _make(finding_id: str = "f-test-001", **kwargs) -> Finding:
        defaults = {
            "finding_id": finding_id,
            "detector_id": "waf_attack_success_candidate",
            "window": ["2024-01-01T10:00:00Z"],
            "entities": {"attacker_ip": ["1.2.3.4"]},
            "confidence": 0.75,
            "risk_score": 0.75,
            "evidence_refs": [],
            "hypothesis": "test hypothesis",
        }
        defaults.update(kwargs)
        return Finding(**defaults)
    return _make


# ---------------------------------------------------------------------------
# CaseRecord fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_case_record() -> CaseRecord:
    """创建示例 CaseRecord。"""
    return CaseRecord(
        case_id="case-test-001",
        title="测试案例",
        status="OPEN",
        priority="P2",
        risk_score=0.75,
    )


# ---------------------------------------------------------------------------
# AttackChainStep fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_attack_chain_step() -> AttackChainStep:
    """创建示例 AttackChainStep。"""
    return AttackChainStep(
        time="2024-01-01T10:00:00Z",
        stage="initial_access",
        action="Web exploit",
        entities={"attacker_ip": ["1.2.3.4"]},
        confidence=0.85,
    )


# ---------------------------------------------------------------------------
# SubAgentTask fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_subagent_task() -> SubAgentTask:
    """创建示例 SubAgentTask。"""
    return SubAgentTask(
        id="test-task-001",
        goal="测试任务目标",
        thought="测试思考过程",
        plan=["步骤1", "步骤2", "步骤3"],
        status="PLANNING",
        depth=0,
    )


# ---------------------------------------------------------------------------
# DispatchBudget fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_dispatch_budget() -> DispatchBudget:
    """创建允许调度的 DispatchBudget。"""
    return DispatchBudget(
        case_auto_dispatch_enabled=True,
        max_parallel_analyst_agents=3,
        analyst_agent_budget_per_hour=10,
        p0_auto_dispatch_enabled=True,
        p1_auto_dispatch_enabled=True,
    )


@pytest.fixture
def disabled_dispatch_budget() -> DispatchBudget:
    """创建禁用的 DispatchBudget（所有能力关闭）。"""
    return DispatchBudget(
        case_auto_dispatch_enabled=False,
        max_parallel_analyst_agents=0,
        analyst_agent_budget_per_hour=0,
    )


# ---------------------------------------------------------------------------
# SubAgentTask mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_task(tmp_path: Path) -> MagicMock:
    """Create a mock SubAgentTask with file paths set under tmp_path."""
    from agent_py_agent.agent.subagents.models import SubAgentTask
    task = MagicMock(spec=SubAgentTask)
    task.id = "test-run-123"
    task.status = "RUNNING"
    task.verification_status = "UNVERIFIED"
    task.failure_type = ""
    task.result = ""
    task.ended_at = 0.0
    task.updated_at = 0.0
    task.heartbeat_at = 0.0
    task.runner_attempts = 0
    task.runner_last_attempt_at = 0.0
    task.runner_last_error = ""
    task.runner_active_attempt_id = ""
    task.runner_abandoned_attempt_ids = []
    task.used_tools = []
    task.used_skills = []
    task.evidence = []
    task.capability_requests = []
    task.capability_grants = []
    task.allowed_tools = ["tool_a", "tool_b"]
    task.allowed_skills = ["skill_x"]
    task.runner_prompt_file = str(tmp_path / "prompt.txt")
    task.runner_response_file = str(tmp_path / "response.txt")
    task.runner_result_file = str(tmp_path / "result.md")
    task.runner_result_json = str(tmp_path / "result.json")
    task.output_json = str(tmp_path / "output.json")
    task.debrief_file = str(tmp_path / "debrief.md")
    return task


# ---------------------------------------------------------------------------
# LocalStore mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_connection_ctx():
    """创建可配置 mock 连接上下文管理器。"""
    @contextmanager
    def _ctx(mock_conn: MagicMock):
        yield mock_conn
    return _ctx


@pytest.fixture
def mock_local_store(make_fake_store_func):
    """创建配置好的 FakeStore 实例（用于 LocalStoreMaintenanceMixin 测试）。"""
    return make_fake_store_func()


def _fake_store_options(kwargs):
    options = {
        "fts_available": kwargs.pop("fts_available", True),
        "db_path": kwargs.pop("db_path", None),
        "files_dir": kwargs.pop("files_dir", None),
        "events_path": kwargs.pop("events_path", None),
        "mock_conn": kwargs.pop("mock_conn", None),
    }
    if kwargs:
        raise TypeError(f"Unexpected FakeStore options: {sorted(kwargs)}")
    return options


def _attach_mock_connection(store, mock_conn) -> None:
    @contextmanager
    def conn_ctx():
        yield mock_conn

    store._connection = conn_ctx


@pytest.fixture
def make_fake_store_func():
    """Factory 函数：创建自定义 FakeStore。"""
    from agent_py_agent.agent.local_storage.maintenance import LocalStoreMaintenanceMixin

    def _make(**kwargs) -> MagicMock:
        """创建 FakeStore 实例。"""
        options = _fake_store_options(kwargs)

        class FakeStore(LocalStoreMaintenanceMixin):
            def __init__(self):
                self._fts_available = options["fts_available"]
                self.db_path = options["db_path"] or Path("/tmp/test.db")
                self.files_dir = options["files_dir"] or Path("/tmp/files")
                self.events_path = options["events_path"] or Path("/tmp/events.jsonl")

            @property
            def fts_available(self) -> bool:
                return self._fts_available

            def _read_content(self, row) -> str:
                return "mock content"

            def _replace_fts_row(self, conn, row_id, title, content):
                pass

            def _record_event(self, conn, event_type, source_id, metadata):
                pass

            def _resolve_content_path(self, content_path) -> Path:
                return Path(content_path) if content_path else Path("/tmp/unknown")

            def _record_filters(self, source_type=None):
                if source_type:
                    return "WHERE source_type = ?", (source_type,)
                return "", ()

        store = FakeStore()
        if options["mock_conn"]:
            _attach_mock_connection(store, options["mock_conn"])
        return store
    return _make


# ---------------------------------------------------------------------------
# Log event fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_waf_event() -> dict[str, Any]:
    """创建示例 WAF 事件。"""
    return {
        "event_class": "alert",
        "alert_type": "waf",
        "source_product": "waf",
        "uri": "/test",
        "src_ip": "1.2.3.4",
        "victim_ip": "10.0.0.1",
        "severity": "high",
        "event_time": "2024-01-01T10:00:00Z",
    }


@pytest.fixture
def sample_vpn_event() -> dict[str, Any]:
    """创建示例 VPN 事件。"""
    return {
        "event_class": "auth",
        "event_action": "login",
        "event_outcome": "success",
        "source_product": "vpn",
        "user": "alice",
        "src_ip": "1.2.3.4",
        "event_time": "2024-01-01T10:00:00Z",
    }


@pytest.fixture
def sample_auth_failure_events() -> list[dict[str, Any]]:
    """创建示例认证失败事件列表。"""
    return [
        {
            "event_class": "auth",
            "event_action": "login",
            "event_outcome": "failure",
            "user": "alice",
            "src_ip": "1.2.3.4",
            "victim_ip": "10.0.0.1",
            "event_time": "2024-01-01T10:00:00Z",
        },
        {
            "event_class": "auth",
            "event_action": "login",
            "event_outcome": "failure",
            "user": "alice",
            "src_ip": "1.2.3.4",
            "victim_ip": "10.0.0.1",
            "event_time": "2024-01-01T10:01:00Z",
        },
        {
            "event_class": "auth",
            "event_action": "login",
            "event_outcome": "success",
            "user": "alice",
            "src_ip": "1.2.3.4",
            "victim_ip": "10.0.0.1",
            "event_time": "2024-01-01T10:05:00Z",
        },
    ]


@pytest.fixture
def sample_process_event() -> dict[str, Any]:
    """创建示例进程事件。"""
    return {
        "source_product": "edr",
        "event_class": "process",
        "process_name": "bash",
        "parent_process_name": "nginx",
        "event_action": "exec",
        "cmdline": "curl http://evil.com",
        "src_ip": "1.2.3.4",
        "victim_ip": "10.0.0.1",
        "event_time": "2024-01-01T10:00:00Z",
    }


# ---------------------------------------------------------------------------
# Mock connection helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db_conn():
    """创建配置好的 mock 数据库连接。"""
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = []
    mock_result.fetchone.return_value = (0,)
    mock_conn.execute.return_value = mock_result
    return mock_conn


@pytest.fixture
def db_conn_with_records(records: list[dict[str, Any]]) -> MagicMock:
    """创建包含记录数据的 mock 数据库连接。"""
    mock_conn = MagicMock()
    mock_result = MagicMock()
    mock_result.fetchall.return_value = records
    mock_result.fetchone.return_value = (len(records),)
    mock_conn.execute.return_value = mock_result
    return mock_conn
