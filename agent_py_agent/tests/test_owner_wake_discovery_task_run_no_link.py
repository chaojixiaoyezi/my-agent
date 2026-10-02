"""C12c 合同单测：发现层也能补关"根本没有会话任务"的执行总账（D3 崩溃窗口）。

来源：D3 修复（27b8228a4）让运行时收口边在关联文件确实不存在时按代理树关 TaskRun，但发现层的崩溃重放只认可读的
终态关联，分不出"没有关联"和"关联读坏"，于是主执行轮已收口、TaskRun 还没关时进程崩溃，这条总账就没人补关；
step16v 之前留下的同形历史行也一样（10-01 只读试算：生产各 owner 共 338 条）。2026-10-01 在隔离 Gateway 上用测试侧
暂停钩子停在收口边、SIGKILL 复现了崩溃现场（证据 ~/.my-agent/decision-evidence/c12-observations-20261001/）。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_owner_wake_discovery_task_run_no_link.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_py_agent.agent.owner_wake_discovery import unfinished_task_ids
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def _links(home: Path) -> Path:
    folder = home / "workspace" / "runtime" / "workspaces" / "main-x" / "conversations" / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _request_run(repo: RuntimeRepository, task_id: str, *, settle: bool = True) -> dict[str, str]:
    """按主链写入登记一次请求的根执行轮；settle=True 时根已 done（崩溃前已收口的那一步）。"""
    created = repo.record_run_creation(
        owner_id="local/main", goal="g", conversation_task_id=task_id, run_id=task_id, role="main",
    )
    if settle:
        repo.settle_agent_run(agent_run_id=created["agent_run_id"], status="done", attempt_id=created["attempt_id"])
    return created


def _task_run(repo: RuntimeRepository, task_run_id: str):
    return repo._runtime_connect().execute(
        "SELECT * FROM task_runs WHERE task_run_id = ?", (task_run_id,),
    ).fetchone()


def _closed_events(repo: RuntimeRepository, task_run_id: str) -> list[dict]:
    rows = repo._runtime_connect().execute(
        "SELECT payload_json FROM runtime_events WHERE task_run_id = ? AND event_type = 'task_run.closed'",
        (task_run_id,),
    ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def test_absent_link_with_terminal_tree_is_closed_by_discovery(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-crashed")
    # 同一 owner 下另有别的任务关联，扫描本身不是空目录。
    (_links(home) / "other-task.json").write_text(json.dumps({"task_id": "other-task", "status": "active"}))

    # 第二遍幂等：已关的不再写事件。
    assert unfinished_task_ids(home) == []
    assert unfinished_task_ids(home) == []

    row = _task_run(repo, created["task_run_id"])
    assert row["status"] == "done" and row["closed_at"] > 0
    [closed] = _closed_events(repo, created["task_run_id"])
    assert closed["operator"] == "wake-discovery-task-run-reconcile"
    assert closed["reason"] == "no_conversation_task"


def test_owner_without_any_conversation_store_is_also_absent(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-cli")

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] > 0


def test_unreadable_link_file_keeps_task_run_open(home):
    """文件在、读不出：分不出是不是终态关联，保持打开（与运行时收口边同一规则）。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-broken")
    (_links(home) / "gwreq-broken.json").write_text("{broken", encoding="utf-8")

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0
    assert _closed_events(repo, created["task_run_id"]) == []


def test_active_link_between_turns_keeps_task_run_open(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "task-persistent")
    (_links(home) / "task-persistent.json").write_text(json.dumps({"task_id": "task-persistent", "status": "active"}))

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


def test_running_root_without_link_keeps_task_run_open(home):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-running", settle=False)

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


def test_incomplete_link_scan_keeps_task_run_open(home, monkeypatch):
    """列不全关联目录时不能把"没扫到"当成"没有关联"。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-scan")
    _links(home)
    original = Path.glob

    def flaky_glob(self, pattern, *args, **kwargs):
        if pattern == "*/conversations/tasks":
            raise OSError("scan failed")
        return original(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", flaky_glob)

    unfinished_task_ids(home)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


# ---- 配置了 conversation_workspace 指向默认布局之外（第 15 条，2026-10-02）----
# 补关扫描要跟着运行时实际在用的会话存储走：调用方把 conversation_store.storage.tasks_dir 交进来，
# 关联文件在那里时不能当成"根本没有会话任务"误关；默认布局行为不变。


def _custom_links(tmp_path: Path) -> Path:
    folder = tmp_path / "elsewhere" / "conversations" / "tasks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_active_link_in_configured_root_keeps_task_run_open(home, tmp_path):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "task-configured")
    (_links(home) / "other-task.json").write_text(json.dumps({"task_id": "other-task", "status": "active"}))
    custom = _custom_links(tmp_path)
    (custom / "task-configured.json").write_text(json.dumps({"task_id": "task-configured", "status": "active"}))

    unfinished_task_ids(home, conversation_tasks_dir=custom)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0
    assert _closed_events(repo, created["task_run_id"]) == []


def test_unreadable_link_in_configured_root_keeps_task_run_open(home, tmp_path):
    """配置位置下文件在、读不出：与默认布局同一规则，保持打开。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-broken-elsewhere")
    (_custom_links(tmp_path) / "gwreq-broken-elsewhere.json").write_text("{broken", encoding="utf-8")

    unfinished_task_ids(home, conversation_tasks_dir=_custom_links(tmp_path))

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


def test_terminal_link_in_configured_root_closes_with_its_status(home, tmp_path):
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "task-done-elsewhere")
    custom = _custom_links(tmp_path)
    (custom / "task-done-elsewhere.json").write_text(
        json.dumps({"task_id": "task-done-elsewhere", "status": "completed"})
    )

    unfinished_task_ids(home, conversation_tasks_dir=custom)

    [closed] = _closed_events(repo, created["task_run_id"])
    assert closed["reason"] == "conversation_task_completed"


def test_unlistable_configured_root_keeps_task_run_open(home, tmp_path, monkeypatch):
    """配置位置列不全：同样不能把"没扫到"当成"没有关联"。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-elsewhere")
    custom = _custom_links(tmp_path)
    original = Path.glob

    def flaky_glob(self, pattern, *args, **kwargs):
        if self == custom:
            raise OSError("scan failed")
        return original(self, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, "glob", flaky_glob)

    unfinished_task_ids(home, conversation_tasks_dir=custom)

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0


def test_missing_configured_root_keeps_task_run_open(home, tmp_path):
    """配置位置整个不在（存储初始化时就会建好，不在只可能是外接盘没挂上这类异常）：按列不全处理，保持打开。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-unmounted")
    (_links(home) / "other-task.json").write_text(json.dumps({"task_id": "other-task", "status": "active"}))

    unfinished_task_ids(home, conversation_tasks_dir=tmp_path / "unmounted" / "conversations" / "tasks")

    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0
    assert _closed_events(repo, created["task_run_id"]) == []


def test_default_layout_tasks_dir_behaves_like_no_argument(home, monkeypatch):
    """传进来的就是默认布局里的目录：与不传逐字一致（缺关联照常按 no_conversation_task 补关，不重复扫描）。"""
    repo = RuntimeRepository(home / "runtime.db")
    created = _request_run(repo, "gwreq-default")
    default_dir = _links(home)
    (default_dir / "task-live.json").write_text(json.dumps({"task_id": "task-live", "status": "active"}))
    reads: list[Path] = []
    original = Path.read_text

    def counting_read(self, *args, **kwargs):
        if self.suffix == ".json" and self.parent == default_dir:
            reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read)
    unfinished_task_ids(home, conversation_tasks_dir=default_dir)
    monkeypatch.undo()

    assert reads == [default_dir / "task-live.json"]
    [closed] = _closed_events(repo, created["task_run_id"])
    assert closed["reason"] == "no_conversation_task"


def _scheduler_tick(agent):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.runtime import _BackgroundSchedulerTickMixin

    tick = _BackgroundSchedulerTickMixin()
    tick.runtime = SimpleNamespace(agent=agent)
    tick.store = agent.conversation_store
    return tick


@pytest.mark.parametrize("configured", [True, False], ids=["configured-root", "default-layout"])
def test_wake_reconcile_follows_the_runtime_conversation_store(tmp_path, configured):
    """真实链路：agent 按配置建会话存储并登记任务，唤醒对账（基础 owner 的补关入口）看得到关联。"""
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    extra = {"conversation_workspace": str(tmp_path / "elsewhere" / "conversations")} if configured else {}
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", enable_tools=False, my_agent_home=str(tmp_path / "home"), **extra), tmp_path,
    )
    store = agent.conversation_store
    owner_home = Path(agent.home_paths.owner_home_dir)
    assert (store.storage.root == tmp_path / "elsewhere" / "conversations") is configured
    if configured:  # 配置的会话存储确实在 owner 默认布局之外
        assert owner_home not in store.storage.root.parents
    thread = store.threads.get_or_create({"canonical_user_id": "u-elsewhere", "now": 10.0})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-elsewhere", "now": 20.0})
    repo = agent.subagents.runtime_db
    created = _request_run(repo, "task-elsewhere")
    tick = _scheduler_tick(agent)

    tick._reconcile_wake_queue(now=100.0)
    assert _task_run(repo, created["task_run_id"])["closed_at"] == 0

    store.tasks.update_status({"task_id": "task-elsewhere", "status": "completed", "now": 30.0})
    tick._reconcile_wake_queue(now=200.0)
    [closed] = _closed_events(repo, created["task_run_id"])
    assert closed["reason"] == "conversation_task_completed"
