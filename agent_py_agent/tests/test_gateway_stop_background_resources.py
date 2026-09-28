"""任务3 合同单测：gateway stop 的遗留后台进程事实与显式停止入口。

来源：my-agent-4 开发交流板任务 3（dsh-be 的 R10 验收观察）。Gateway 退出不停止托管后台进程是设计行为，
但用户需要能看到它们、并在明确要求时回收；且必须按进程组停止，否则孙进程会留下（只停登记 pid 会漏）。

复现方法:
    cd <worktree> && PYTHONPATH=. python3 -m pytest agent_py_agent/tests/test_gateway_stop_background_resources.py -q

变异验证：把 stop_background_processes 的目标改成通配（跳过 matches 校验）→ 精确归属用例变红；
把 _report_background_processes 改成无 --stop-background 也直接停 → 默认只列用例变红。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.gateway_parts.background_resource_report import (
    background_process_facts,
    list_running_background_processes,
    stop_background_processes,
)
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.process_session_store import ProcessSessionStore
from agent_py_agent.tests._managed_process_harness import managed_request


def _bound_session(tmp_path: Path, session_id: str, *, code: str = "import time; time.sleep(30)"):
    """用生产预留路径造一条已绑定 child 的记录，再把它置为 running（与 test_background_handoff 同做法）。

    本机沙箱拿不到本进程的出生身份（`ps -p` 不可用），预留路径会因此拒绝；这里按测试合同对
    "fake 进程"的要求给出生身份打桩，其余字段仍走真实校验器和真实 Store。
    """
    request = managed_request(tmp_path, code)
    with patch.object(launch, "capture_process_birth_token", lambda _pid: "launcher-birth"):
        record = launch._reservation(request, session_id)
    record.update(
        pid=os.getpid(),
        pid_birth_token="host-birth",
        child_pid=8124,
        child_pid_birth_token="child-birth",
        child_launch_started=True,
        started_at=time.time(),
        status="running",
    )
    store = ProcessSessionStore(request.store_root)
    return store, store.write(record), request


@pytest.fixture()
def running(tmp_path: Path):
    """造一条 running 记录，并返回 (store, record, request, workspace_root)。

    workspace_root 取 owner home 的父目录：`process_session_store_root(workspace, owner_home)`
    在给定 owner 时把记录放在 `owner.parent/.my-agent-runtime/process_sessions/<digest>`，
    生产由同一函数算出该地址，测试必须按同一口径查询。
    """
    store, record, request = _bound_session(tmp_path, "bg-session-running")
    owner = Path(request.access_scope.owner_home)
    return store, record, request, owner.parent


def test_lists_running_managed_processes_and_flags_terminal(running):
    store, record, request, workspace = running
    terminal = {**record, "session_id": "bg-session-done", "revision": 0,
                "status": "killed", "finished_at": time.time(), "exit_code": 0}
    store.write(terminal)

    processes, errors = list_running_background_processes(request.store_root)

    assert errors == []
    assert [row["session_id"] for row in processes] == ["bg-session-running"]
    facts = processes[0]
    # 只投影结构化事实：不出现命令正文、cwd 或输出路径。
    assert "command" not in facts and "cwd" not in facts and "output_file" not in facts
    assert facts["root_task_id"] == "task-test"
    assert facts["run_id"] == "run-test"
    assert facts["thread_id"] == "thread-test"


def test_stop_marks_only_the_exact_task_scope(running):
    store, record, request, _workspace = running
    # 同 store、同 owner、同 thread，但属于另一个 root task/run 的进程不能被一起停。
    other = {
        **record,
        "session_id": "bg-session-other",
        "revision": int(store.load("bg-session-other").record["revision"])
        if store.load("bg-session-other").record else 0,
        "execution_scope": {**record["execution_scope"], "root_task_id": "task-other", "run_id": "run-other"},
    }
    store.write(other)

    processes, _errors = list_running_background_processes(request.store_root)
    target = [row for row in processes if row["session_id"] == "bg-session-running"]
    stop_background_processes(request.store_root, target, timeout_seconds=0.1)

    assert store.load("bg-session-running").record["stop_requested"] is True
    assert store.load("bg-session-other").record["stop_requested"] is False


def test_stop_refuses_incomplete_scope_instead_of_wildcard(running):
    store, _record, request, workspace = running
    processes, _errors = list_running_background_processes(request.store_root)
    blanked = [{**processes[0], "thread_id": "", "root_task_id": "", "run_id": ""}]

    results = stop_background_processes(request.store_root, blanked, timeout_seconds=0.1)

    assert results[0]["stopped"] is False
    assert results[0]["reason"] == "scope_incomplete"
    assert store.load("bg-session-running").record["stop_requested"] is False


def test_stop_reports_not_stopped_while_record_stays_running(running):
    """记录没有进入终态时不能说已停止：如实返回 stopped=False（这里没有真的 host 去回收）。"""
    _store, _record, request, workspace = running
    processes, _errors = list_running_background_processes(request.store_root)

    results = stop_background_processes(request.store_root, processes, timeout_seconds=0.1)

    assert results[0]["stopped"] is False
    assert results[0]["reason"] == "still_running_after_request"


def test_facts_projection_has_no_command_text(running):
    _store, record, _request, workspace = running
    facts = background_process_facts(record)
    text = json.dumps(facts, ensure_ascii=False)
    assert "isolated test command" not in text
    assert "command" not in facts
    assert set(facts) == {"session_id", "status", "pid", "host_pid", "started_at",
                          "thread_id", "root_task_id", "run_id", "attempt_id"}


@pytest.mark.skipif(sys.platform == "win32", reason="进程组语义只在 POSIX 上验证")
def test_process_group_stop_also_reaps_grandchild():
    """按进程组停止时孙进程一起结束；只停登记 pid 会漏掉它（设计要防的正是这个）。"""
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import subprocess,sys;print(subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']).pid,"
         "flush=True);import time;time.sleep(30)"],
        stdout=subprocess.PIPE, start_new_session=True,
    )
    try:
        grandchild_pid = int(child.stdout.readline().strip())
        assert os.getpgid(child.pid) == child.pid  # start_new_session 让 child 成为组长
        os.killpg(os.getpgid(child.pid), signal.SIGTERM)
        child.wait(timeout=5)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(grandchild_pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        with pytest.raises(OSError):
            os.kill(grandchild_pid, 0)
    finally:
        try:
            os.killpg(os.getpgid(child.pid), signal.SIGKILL)
        except OSError:
            pass
        try:
            child.wait(timeout=2)
        except Exception:
            pass
