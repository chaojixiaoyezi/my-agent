"""后台派工活性判据钉子(P2 非阻塞出口门 Step1,实锤来源:主代理派完子代理想
干净撒手时,出口门必须先确认"子代理还真在后台跑",才敢非阻塞 yield 而不误伤)。

钉死:
1. task_background_pid:从任务对象 / canonical dict 读 background_start.pid,坏值归 0。
2. is_task_background_live:pid 存活 → live;死 pid / 无记录 → not-live;
   无 pid 但进程内线程注册且存活 → live(gateway 隔离 owner 的 in-process 派工形态)。
3. open_children_all_background_live:open 子代理全 live → True;任一死 pid → False;
   无 open 子代理 → False。
4. is_wake_capable_source:只有 background_main_agent/gateway/chat 算 wake-capable。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.background_liveness import (
    is_task_background_live,
    is_wake_capable_source,
    open_children_all_background_live,
    task_background_pid,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager

pytestmark = pytest.mark.integration


def _dead_pid() -> int:
    """真实死 pid:拉起再杀掉一个进程,拿它已回收的 pid(不会误撞活进程)。"""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.kill()
    proc.wait(timeout=5)
    return proc.pid


def _task_with_pid(pid: int, *, run_id: str = "sub-1") -> SimpleNamespace:
    return SimpleNamespace(id=run_id, attributes={"background_start": {"pid": pid, "status": "running"}})


# ---------------------------------------------------------------------------
# 1. task_background_pid
# ---------------------------------------------------------------------------


def test_task_background_pid_reads_object_and_dict() -> None:
    assert task_background_pid(_task_with_pid(4321)) == 4321
    assert task_background_pid({"attributes": {"background_start": {"pid": 99}}}) == 99


def test_task_background_pid_bad_values_return_zero() -> None:
    assert task_background_pid(SimpleNamespace(attributes={})) == 0
    assert task_background_pid(SimpleNamespace(attributes={"background_start": {"pid": "x"}})) == 0
    assert task_background_pid({"attributes": {"background_start": "not-a-dict"}}) == 0
    assert task_background_pid(SimpleNamespace()) == 0


# ---------------------------------------------------------------------------
# 2. is_task_background_live
# ---------------------------------------------------------------------------


def test_live_pid_is_live() -> None:
    # 自身进程 pid 必然存活。
    assert is_task_background_live(_task_with_pid(os.getpid())) is True


def test_dead_pid_is_not_live() -> None:
    assert is_task_background_live(_task_with_pid(_dead_pid())) is False


def test_no_record_is_not_live() -> None:
    assert is_task_background_live(SimpleNamespace(id="sub-x", attributes={})) is False


def test_inprocess_thread_registration_counts_as_live() -> None:
    release = threading.Event()
    thread = threading.Thread(target=release.wait, name="my-agent-launch-live", daemon=True)
    thread.start()
    try:
        agent = SimpleNamespace(
            _background_subagent_dispatches={
                "launch-live": {"run_ids": ["sub-thread"], "thread_name": "my-agent-launch-live"}
            }
        )
        task = SimpleNamespace(id="sub-thread", attributes={})  # 线程派工无 pid
        assert is_task_background_live(task, agent) is True
        # agent 缺省(纯 pid 视角)时无法看到线程注册表 → not-live
        assert is_task_background_live(task) is False
    finally:
        release.set()
        thread.join(timeout=5)
    # 线程结束后不再算 live
    assert is_task_background_live(SimpleNamespace(id="sub-thread", attributes={}), agent) is False


def test_unregistered_run_id_thread_is_not_live() -> None:
    agent = SimpleNamespace(
        _background_subagent_dispatches={
            "launch-a": {"run_ids": ["other"], "thread_name": "MainThread"}
        }
    )
    assert is_task_background_live(SimpleNamespace(id="sub-thread", attributes={}), agent) is False


# ---------------------------------------------------------------------------
# 3. open_children_all_background_live(真实 manager + task_root 投影)
# ---------------------------------------------------------------------------


def _register_child(task_root: Path, run_id: str, *, status: str = "RUNNING") -> None:
    agent_dir = task_root / "work" / "agents" / run_id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "canonical_state.json").write_text(
        json.dumps({"id": run_id, "status": status, "capability_requests": []}, ensure_ascii=False),
        encoding="utf-8",
    )


def _make_child(manager: SubAgentManager, *, status: str, pid: int) -> str:
    task = manager.create_run(goal="活性判据子代理", thought="P2", plan=["run"])
    task.status = status
    attrs = dict(task.attributes or {})
    attrs["background_start"] = {"pid": pid, "status": "running", "launch_id": "launch-x"}
    task.attributes = attrs
    manager.save(task)
    return task.id


def _agent(manager: SubAgentManager) -> SimpleNamespace:
    return SimpleNamespace(subagents=manager)


def test_all_open_children_live_returns_true(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    run_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    _register_child(task_root, run_id)

    assert open_children_all_background_live(_agent(manager), task_root) is True


def test_dead_pid_child_blocks_all_live(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    live_id = _make_child(manager, status="RUNNING", pid=os.getpid())
    dead_id = _make_child(manager, status="RUNNING", pid=_dead_pid())
    _register_child(task_root, live_id)
    _register_child(task_root, dead_id)

    assert open_children_all_background_live(_agent(manager), task_root) is False


def test_no_open_children_returns_false(tmp_path: Path) -> None:
    manager = SubAgentManager(tmp_path / "subs")
    task_root = tmp_path / "task"
    done_id = _make_child(manager, status="DONE", pid=os.getpid())
    _register_child(task_root, done_id, status="DONE")

    # 全终态 → 无 open 子代理 → False(该走验收门,不是非阻塞 yield)
    assert open_children_all_background_live(_agent(manager), task_root) is False
    assert open_children_all_background_live(_agent(manager), tmp_path / "empty") is False


# ---------------------------------------------------------------------------
# 4. is_wake_capable_source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("background_main_agent", True),
        ("gateway", True),
        ("chat", True),
        ("cli_run", False),
        ("run", False),
        ("", False),
    ],
)
def test_is_wake_capable_source(source: str, expected: bool) -> None:
    assert is_wake_capable_source(SimpleNamespace(source=source)) is expected
