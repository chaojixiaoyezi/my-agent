"""共享测试 fixtures - 为测试提供通用对象和 mock。"""
from __future__ import annotations

import os
import webbrowser
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
from agent_py_agent.agent.tooling import computer_use_macos, computer_use_x11
from agent_py_agent.tests._desktop_open_guard import (
    REAL_DESKTOP_MARKER,
    DesktopOpenGuard,
    failure_message,
)
from agent_py_agent.tests._repo_tree_guard import (
    REPO_ROOT,
    magicmock_failure_message,
    magicmock_fingerprint,
    magicmock_violation,
    remove_magicmock_dir,
    tracked_report_failure_message,
    tracked_report_fingerprint,
)
from agent_py_agent.tests._screen_capture_guard import (
    LANE_SKIP_MARKER,
    forbidden_real_macos_frameworks,
    forbidden_real_x11_libraries,
)

_DESKTOP_GUARD: DesktopOpenGuard | None = None
# 会话级 fixture 在最后一条测试收尾时就结束了，会话结束检查要用这份不清空的引用。
_FINISHED_DESKTOP_GUARD: DesktopOpenGuard | None = None


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        f"{REAL_DESKTOP_MARKER}: 测试确实需要调用真实的 open/xdg-open/osascript/通知/剪贴板程序；默认全部被测试防线拦截",
    )


@pytest.fixture(scope="session", autouse=True)
def _desktop_open_guard(tmp_path_factory):
    """会话级防线：shim 目录放到 PATH 最前，webbrowser.open* 换成只记录的替身，任何测试都打不开用户桌面程序。

    子进程经 PATH 继承 shim（插件 MCP 子进程经 build_safe_env 保留 PATH）；自己改写 PATH 的测试自行负责。
    """
    global _DESKTOP_GUARD, _FINISHED_DESKTOP_GUARD
    guard = DesktopOpenGuard.install(tmp_path_factory.mktemp("desktop_open_guard"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", guard.path_with_shims(os.environ.get("PATH", "")))
        for name in guard.originals:
            patch.setattr(webbrowser, name, guard.python_opener(name))
        _DESKTOP_GUARD = guard
        try:
            yield guard
        finally:
            _DESKTOP_GUARD, _FINISHED_DESKTOP_GUARD = None, guard


@pytest.fixture(scope="session", autouse=True)
def _real_screen_guard():
    """会话级防线：两个桌面后端唯一的真实库加载入口换成直接失败（见 _screen_capture_guard），测试永远碰不到真实屏幕。

    X11 的入口只在 Linux 车道容器（车道标记为 1）里不换，那里是 Xvfb 假桌面；注入假库的后端不走这两个入口；
    子进程那一层由 test_screen_capture_guard 的扫描守卫兜住。
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(computer_use_macos, "load_real_macos_frameworks", forbidden_real_macos_frameworks)
        if os.environ.get(LANE_SKIP_MARKER) != "1":
            patch.setattr(computer_use_x11, "load_real_x11_libraries", forbidden_real_x11_libraries)
        yield


@pytest.fixture(autouse=True)
def _desktop_open_guard_per_test(request, _desktop_open_guard):
    """声明了真实调用的测试放行：PATH 去掉 shim、webbrowser 恢复原实现。

    用自己的 MonkeyPatch，不借用测试的 monkeypatch；检查放在 pytest_runtest_teardown 之后统一做。
    """
    if not hasattr(request.node, "_desktop_guard_mark"):
        # 本会话第一条测试在 setup 钩子里还没有防线实例，在这里补记游标。
        request.node._desktop_guard_mark = _desktop_open_guard.mark()
    if request.node.get_closest_marker(REAL_DESKTOP_MARKER) is None:
        yield
        return
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", _desktop_open_guard.path_without_shims(os.environ.get("PATH", "")))
        for name, original in _desktop_open_guard.originals.items():
            patch.setattr(webbrowser, name, original)
        yield


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    """在任何 fixture 生效之前记下本测试的游标（记录文件的字节长度，不读内容）。"""
    if _DESKTOP_GUARD is not None:
        item._desktop_guard_mark = _DESKTOP_GUARD.mark()
    return (yield)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_teardown(item, nextitem):
    """本测试完整收尾之后（它的 fixture 与 monkeypatch 都已撤销）再检查期间的拦截记录，有就让这条测试报错。

    测试体、夹具收尾和插件进程退出时的调用都在这里一次归属；读记录只用底层 os，测试的 IO 哨兵和打桩碰不到。
    """
    guard = _DESKTOP_GUARD
    mark = getattr(item, "_desktop_guard_mark", None)
    result = yield
    if guard is None or mark is None:
        return result
    records = guard.take_since(mark)
    if records and item.get_closest_marker(REAL_DESKTOP_MARKER) is None:
        pytest.fail(failure_message(records, f"测试 {item.nodeid} "), pytrace=False)
    return result


def pytest_sessionfinish(session, exitstatus):
    guard = _DESKTOP_GUARD or _FINISHED_DESKTOP_GUARD
    records = guard.unattributed() if guard is not None else []
    if records:
        session.config.get_terminal_writer().line(failure_message(records, "测试会话中（未能归属到单条测试的后台进程）"))
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(autouse=True)
def _repo_tree_guard(request):
    """仓库树防线：测试不能往起跑目录写 MagicMock/，也不能改写仓库根被跟踪的 CODE_SIZE_REPORT.md（见 _repo_tree_guard）。

    起跑目录取 pytest 启动时的 cwd，测试里切目录不影响；前后指纹不同就让这条测试在收尾时报错，
    这条测试新建的 MagicMock/ 顺手删掉，会话开始前的残留只比对不删除；报告文件只报错、不自动恢复。
    """
    root = str(request.config.invocation_params.dir)
    before, report_before = magicmock_fingerprint(root), tracked_report_fingerprint(REPO_ROOT)
    yield
    after, problems = magicmock_fingerprint(root), []
    if magicmock_violation(before, after):
        if before is None:
            remove_magicmock_dir(root)
        problems.append(magicmock_failure_message(request.node.nodeid, root, created=before is None))
    if tracked_report_fingerprint(REPO_ROOT) != report_before:
        problems.append(tracked_report_failure_message(request.node.nodeid, REPO_ROOT))
    if problems:
        pytest.fail("\n".join(problems), pytrace=False)


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
    # 在 Gateway 托管的工具进程里跑测试时会继承托管标记；清掉它，生命周期命令测试才不受宿主影响。
    monkeypatch.delenv("MY_AGENT_HOSTING_GATEWAY_PID", raising=False)



@pytest.fixture(autouse=True)
def _isolate_decision_reach_counts(monkeypatch):
    """每个测试用自己的决策点到达计数待写队列，测完丢弃。

    待写队列（conversation/decision_reach_counts）是进程级的；Gateway 收尾会真实落盘全部待写计数，
    不隔离的话会把别的测试留下的计数写进它们（可能已删除的）临时 home。
    """
    from agent_py_agent.agent.conversation import decision_reach_counts

    monkeypatch.setattr(decision_reach_counts, "_PENDING", {})
    monkeypatch.setattr(decision_reach_counts, "_LAST_FLUSH", {})


@pytest.fixture(autouse=True)
def _isolate_model_call_admission(monkeypatch):
    """每个测试用自己的模型调用准入表，测完丢弃。

    准入表（contracts/model_call_ledger）是进程级的：Gateway 收尾会关门、进程内不重开，账本构造时登记进去。
    不隔离的话，一条跑过收尾的测试之后，同一测试进程里所有账本都不再接新调用；别的测试留下的在途调用也会混进停机结清。
    """
    from agent_py_agent.agent.contracts import model_call_ledger

    monkeypatch.setattr(model_call_ledger, "_ADMISSION_REGISTRY", model_call_ledger._ModelCallAdmissionRegistry())

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
