"""共享测试 fixtures - 为测试提供通用对象和 mock。"""
from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_py_agent.agent.capability import CapabilityRouter, SkillsService
from agent_py_agent.agent.subagents.models import SubAgentTask


@pytest.fixture(autouse=True)
def _isolate_my_agent_home(tmp_path_factory, monkeypatch):
    """默认把每个测试的 MY_AGENT_HOME 隔离到临时目录。

    防两件事:① 测试污染用户真实 ~/.my-agent(测试不该写用户数据)② runtime workspace 在全局 home
    累积泄漏(实测曾累积 16 万目录、拖垮 collaboration 测试)。
    显式传 home/my_agent_home 的测试不受影响——resolve_my_agent_home 里 value 优先于 env;
    需要特定 MY_AGENT_HOME 的用例可在测试体内 monkeypatch 覆盖(后设生效)。
    """
    home = tmp_path_factory.mktemp("ma_home")
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    # Tests start real loopback HTTP servers.  A desktop/system proxy must not
    # intercept those private test fixtures; preserve any operator exclusions
    # while making the standard loopback set explicit for both spellings.
    exclusions = _loopback_no_proxy(os.environ.get("NO_PROXY", ""))
    monkeypatch.setenv("NO_PROXY", exclusions)
    monkeypatch.setenv(
        "no_proxy",
        _loopback_no_proxy(os.environ.get("no_proxy", "")),
    )


def _loopback_no_proxy(current: str) -> str:
    values = [item.strip() for item in str(current or "").split(",") if item.strip()]
    for item in ("127.0.0.1", "localhost", "::1"):
        if item not in values:
            values.append(item)
    return ",".join(values)


@pytest.fixture
def inline_watch_open(monkeypatch):
    """Make ordinary watch tests use the internal non-background lane.

    ``background_harvest`` is intentionally not a model-facing tool argument.
    Tests that exercise the older inline ordinary-watch behavior inject the
    host-owned tuning before the state is persisted instead of teaching callers
    a removed public parameter.
    """

    import importlib

    # The suite intentionally exercises both supported import roots.  Patch the
    # module used by each root; patching only one leaves the other free to start
    # a background harvester and makes otherwise-inline assertions race.
    for module_name in (
        "agent.ingestion.watch_tool",
        "agent_py_agent.agent.ingestion.watch_tool",
    ):
        watch_tool = importlib.import_module(module_name)
        original_new_state = watch_tool.new_state

        def _new_state(*args, _original=original_new_state, **kwargs):
            state = _original(*args, **kwargs)
            tuning = replace(state.tuning, background_harvest=0)
            state.tuning = tuning
            state.engine.tuning = tuning
            return state

        monkeypatch.setattr(watch_tool, "new_state", _new_state)


@pytest.fixture
def skill_catalog_factory():
    """Build the production SkillsService without restoring the deleted registry path."""

    def build(root: Path, *, extra_roots: list[Path] | None = None, policy_overrides=None):
        owner = root / "owner"
        shared = root / "shared"
        builtin = root / "builtin"
        workspace = root / "workspace"
        for directory in (owner / "skills", shared, builtin, workspace):
            directory.mkdir(parents=True, exist_ok=True)
        policy_values = {
            "owner_id": "test-owner",
            "skills_enabled": True,  # 总闸 effective flag(默认开启,测试覆盖关闭路径时置 False)
            "enabled_skill_sources": ("workspace", "owner", "shared", "builtin"),
            "enabled_shared_skills": (),
            "disabled_skills": (),
        }
        policy_values.update(policy_overrides or {})
        policy = SimpleNamespace(**policy_values)
        home = SimpleNamespace(
            owner_home_dir=owner,
            shared_skills_dir=shared,
            shared_builtin_dir=builtin,
        )
        service = SkillsService(
            home_paths=home,
            workspace_root=workspace,
            policy_provider=lambda: policy,
        )
        service.set_extra_roots(extra_roots or [])
        snapshot = service.snapshot_for(workspace)
        return SimpleNamespace(
            service=service,
            snapshot=snapshot,
            router=CapabilityRouter(skill_snapshot=snapshot),
            home=home,
            workspace=workspace,
            policy=policy,
        )

    return build


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
    task.evidence_packets = []
    task.findings = []
    task.evidence_refs = []
    task.artifact_refs = []
    task.blockers = []
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
