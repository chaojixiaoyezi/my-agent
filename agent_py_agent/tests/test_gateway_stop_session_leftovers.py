"""C12a 合同单测：Gateway 会话内 /stop 能回收被中断任务留下的后台进程（TUI 与飞书共用这个入口）。

来源：R10 深度切片（2026-09-28）观察——回合被 /interrupt 后托管后台进程按设计继续跑；几分钟后发现层账本自愈把
仍 active 的中断任务记成 cancelled，此时同会话 /stop 只回"当前没有运行中的内容"，进程再没有入口能停。09-28 的
修复（3f17b1e7a）只覆盖本地 chat 入口，默认 TUI 与飞书走的 Gateway 入口一直没有。2026-10-01 在隔离 Gateway +
假模型 + 真 TUI 上复现（证据 ~/.my-agent/decision-evidence/c12-observations-20261001/）。

复现方法:
    bash ~/.my-agent/releases/claude-tools/3a-scripts/run_files312.sh <worktree> <basetemp> \
        agent_py_agent/tests/test_gateway_stop_session_leftovers.py

这里不起真实进程：登记记录按生产预留路径造（出生身份打桩），锁外清理换成记录器，测试只核对冻结的停止意图、
选择范围和回执，不向任何 pid 发信号。
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts import background_resource_report
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    execute_gateway_conversation_control,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling import background_process_launch as launch
from agent_py_agent.agent.tooling.process_scope import ProcessAccessScope, ProcessExecutionScope
from agent_py_agent.agent.tooling.process_session_store import (
    ProcessSessionStore,
    process_session_store_root,
)
from agent_py_agent.tests._managed_process_harness import managed_request


@pytest.fixture
def agent(tmp_path: Path) -> SimpleAgent:
    return SimpleAgent(AgentConfig(model_backend="echo", gateway_per_user_owner_scoping=False), tmp_path)


@pytest.fixture
def cleanups():
    """锁外清理换成记录器：只记下交来的冻结批次，绝不终止任何进程。"""
    calls: list[object] = []
    with patch.object(background_resource_report, "cleanup_session_leftover_processes", calls.append):
        yield calls


def _owner_home(agent: SimpleAgent) -> str:
    return str(Path(agent.home_paths.owner_home_dir).expanduser().resolve(strict=False))


def _thread(agent: SimpleAgent, conversation_id: str = "c-1"):
    return agent.conversation_store.threads.get_or_create({
        "canonical_user_id": "u-1", "channel": "feishu", "channel_conversation_id": conversation_id,
        "channel_user_id": "u-1", "now": time.time() - 40,
    })


def _link(agent: SimpleAgent, thread_id: str, task_id: str, fields: dict[str, str]):
    request = {"thread_id": thread_id, "task_id": task_id, "goal": "整理资料", "now": time.time() - 30, **fields}
    return agent.conversation_store.tasks.bind(request)


def _seed_running(agent: SimpleAgent, session_id: str, owner_ids: tuple[str, str, str]) -> None:
    """按生产预留路径造一条 running 登记（沙箱拿不到出生身份，只对该项打桩），store 根与写入端同一函数。

    owner_ids 是 (thread_id, root_task_id, run_id)，即登记记录的精确执行归属。
    """
    thread_id, task_id, run_id = owner_ids
    owner = _owner_home(agent)
    root = process_session_store_root(owner, owner)
    request = managed_request(
        Path(owner) / "bg-test",
        access_scope=ProcessAccessScope("owner-test", thread_id, owner),
        execution_scope=ProcessExecutionScope(owner, thread_id, task_id, run_id, f"attempt-{run_id}"),
        store_root=root,
    )
    with patch.object(launch, "capture_process_birth_token", lambda _pid: "launcher-birth"):
        record = launch._reservation(request, session_id)
    record.update(
        pid=12345, pid_birth_token="host-birth", child_pid=54321, child_pid_birth_token="child-birth",
        child_launch_started=True, started_at=1.0, status="running",
    )
    ProcessSessionStore(root).write(record)


def _stop_requested(agent: SimpleAgent, session_id: str) -> bool:
    owner = _owner_home(agent)
    report = ProcessSessionStore(process_session_store_root(owner, owner)).load(session_id)
    return bool(report.record and report.record.get("stop_requested"))


def _control(agent: SimpleAgent, text: str, conversation_id: str = "c-1"):
    command = parse_conversation_control(text)
    assert command is not None
    return execute_gateway_conversation_control(
        agent, gateway_paths(agent), command, GatewayControlScope("u-1", "feishu", conversation_id),
    )


def test_stop_reclaims_leftover_of_cancelled_interrupted_task(agent, cleanups):
    """R10 原场景：中断任务已被自愈记成 cancelled，进程仍登记 running → /stop 冻结它并列出 pid 与归属。"""
    thread = _thread(agent)
    _link(agent, thread.thread_id, "gwreq-old", {"status": "cancelled"})
    _seed_running(agent, "bg-left", (thread.thread_id, "gwreq-old", "gwreq-old"))

    result = _control(agent, "/stop")

    assert result.ok is True
    assert result.delivery_status == "accepted"
    assert "已受理停止本会话遗留的 1 个后台资源" in result.message
    assert "pid 54321" in result.message and "task gwreq-old / run gwreq-old" in result.message
    assert _stop_requested(agent, "bg-left") is True
    # 回收只交给锁外清理一次，批次里正是这条精确归属的冻结清单。
    assert len(cleanups) == 1
    [batch] = cleanups[0].batches
    assert [row["session_id"] for row in batch.receipt.records] == ["bg-left"]


def test_interrupt_without_running_turn_never_reclaims(agent, cleanups):
    """中断不冒充资源清理：没有运行中回合时 /interrupt 什么都不停。"""
    thread = _thread(agent)
    _link(agent, thread.thread_id, "gwreq-old", {"status": "cancelled"})
    _seed_running(agent, "bg-left", (thread.thread_id, "gwreq-old", "gwreq-old"))

    result = _control(agent, "/interrupt")

    assert result.ok is False
    assert result.message == "当前没有执行中的回合，无需中断。"
    assert _stop_requested(agent, "bg-left") is False
    assert cleanups == []


def test_other_session_and_live_audit_resources_are_untouched(agent, cleanups):
    """只认本会话精确 thread；本会话仍 active 的审计任务由 /audit 管，/stop 不碰它的进程。"""
    thread = _thread(agent)
    other = _thread(agent, "c-2")
    _link(agent, thread.thread_id, "audit-live", {"status": "active", "work_kind": "audit"})
    _link(agent, other.thread_id, "gwreq-other", {"status": "cancelled"})
    _seed_running(agent, "bg-audit", (thread.thread_id, "audit-live", "audit-run"))
    _seed_running(agent, "bg-other", (other.thread_id, "gwreq-other", "gwreq-other"))

    result = _control(agent, "/stop")

    assert result.ok is False
    assert result.message == "当前没有运行中的内容，无需停止。"
    assert _stop_requested(agent, "bg-audit") is False
    assert _stop_requested(agent, "bg-other") is False
    assert cleanups == []


def test_unreadable_task_links_fail_closed(agent, cleanups):
    """会话任务记录读不出时不能猜哪些进程已无主：报未确认、一个都不停。"""
    thread = _thread(agent)
    _link(agent, thread.thread_id, "gwreq-old", {"status": "cancelled"})
    _seed_running(agent, "bg-left", (thread.thread_id, "gwreq-old", "gwreq-old"))
    link_path = agent.conversation_store.storage.tasks_dir / "gwreq-old.json"
    link_path.write_text("{broken", encoding="utf-8")

    result = _control(agent, "/stop")

    assert result.ok is False
    assert result.error_code == "TASK_RESOURCE_STOP_UNCONFIRMED"
    assert "没有运行中的内容" not in result.message
    assert _stop_requested(agent, "bg-left") is False
    assert cleanups == []


def test_stop_with_nothing_left_still_says_none(agent, cleanups):
    _thread(agent)

    result = _control(agent, "/stop")

    assert result.ok is False
    assert result.message == "当前没有运行中的内容，无需停止。"
    assert cleanups == []
