"""R9-12：CLI 任务控制经实际配置 owner 的原宿主、原授权门、原 creation guard 与原停止/清理原语作用于 canonical 子代理。

真实配置文件 + make_agent 构造、真实 SubAgentManager/RuntimeDB/ConversationStore/LocalStore 与原 runner 会话租约；
控制服务与存储不 mock。只有“托管权威库挂载失败”一例把原 RuntimeRepository 构造换成模拟 I/O 错误。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from agent_py_agent.agent.agent_core.runner.session_pool import (
    RunnerSessionPoolLease,
    runner_session_lease,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.cancellation import (
    CancelSubagentTaskRequest,
    cancel_subagent_tree,
)
from agent_py_agent.agent.subagents.recovery_eligibility import user_stopped_run_is_resumable
from agent_py_agent.agent.subagents.runner_start import reserve_runner_start
from agent_py_agent.agent.subagents.task_control import task_control_record
from agent_py_agent.cli import task_commands
from agent_py_agent.cli.common import make_agent


# 函数用途: 生成与真实 CLI 相同形状的参数（配置文件 + 显式工作区），owner/home 全部来自配置。
def _args(tmp_path: Path, **extra) -> argparse.Namespace:
    config = tmp_path / "config.yaml"
    if not config.exists():
        config.write_text(f'model_backend: echo\nmy_agent_home: "{tmp_path / "home"}"\n', encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return argparse.Namespace(config=str(config), workspace_root=str(workspace), **extra)


# LLM: 业务方用同一 CLI 构造入口建立孩子、父会话链接与当前 exact attempt；控制命令另起自己的宿主实例。
# 函数用途: 建一个直属根会话任务、正在运行的孩子。
def _seed(tmp_path: Path):
    agent = make_agent(_args(tmp_path))
    store = agent.conversation_store
    thread = store.threads.get_or_create({
        "canonical_user_id": "local-agent", "channel": "chat", "owner_id": agent.home_paths.owner_id,
        "channel_conversation_id": "session-main", "channel_user_id": "local-agent",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-root", "goal": "整理资料", "status": "active"})
    child = agent.subagents.create_run(goal="整理资料", thought="", plan=["执行"], parent_id="task-root", root_id="task-root")
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": child.id, "goal": child.goal,
                      "task_path": str(child.task_dir or ""), "status": "active"})
    attempt = agent.subagents.lifecycle.prepare_runner_attempt(child.id).runner_active_attempt_id
    assert attempt
    return agent, child, attempt


# 函数用途: 以原租约发布原执行轮的 runner 会话（原宿主的句柄），返回可在测试里收尾的上下文。
def _owning_runner(agent, run_id: str, attempt_id: str):
    lease = runner_session_lease(RunnerSessionPoolLease(
        manager=agent.subagents, run_id=run_id, attempt_id=attempt_id, interval_seconds=30.0,
    ))
    session = lease.__enter__()
    return lease, session


def _cli(tmp_path, command, run_id: str) -> int:
    return command(_args(tmp_path, task_id=run_id))


def test_cli_pause_closes_original_authority_and_projects_paused(tmp_path, capsys):
    agent, child, attempt = _seed(tmp_path)
    lease, session = _owning_runner(agent, child.id, attempt)
    try:
        assert _cli(tmp_path, task_commands.cmd_task_pause, child.id) == 0
        assert "等待原执行轮宿主确认清理" in capsys.readouterr().out
        task = agent.subagents.load(child.id)
        assert task.status == "PAUSED" and task.failure_type == "" and task.ended_at == 0.0
        assert task.runner_active_attempt_id == "" and attempt in task.runner_abandoned_attempt_ids
        # 不伪造取消结果：没有 cancel_subagents 记录，也不进取消经验账。
        assert "cancel_subagents" not in task.attributes
        control = task_control_record(task)
        assert (control["generation"], control["action"], control["state"], control["target_status"]) == (
            1, "pause", "applied", "PAUSED")
        assert control["attempt_id"] == attempt and control["previous_status"] == "RUNNING"
        assert control["cleanup"] == {"state": "pending", "attempt_id": attempt}
        assert control["runtime_authority"] == "cancelled"
        # 原执行权在 RuntimeDB 里关闭，来源是任务控制；原 runner 会话句柄仍在，等原宿主收尾。
        repo = agent.subagents.runtime_db
        assert repo.agent_run_for_run_id(child.id)["status"] == "cancelled"
        assert repo.get_attempt(attempt)["status"] == "cancelled"
        completed = [row for row in repo.events_for_attempt(attempt, limit=100) if row["event_type"] == "agent_run.completed"]
        assert completed and completed[-1]["payload"]["runtime_source"] == "task_control"
        assert task.attributes["runner_session"]["session_id"] == session["session_id"]
        assert task.attributes["runner_session"]["status"] == "running"
        assert agent.conversation_store.tasks.load_report(child.id)[0].status == "interrupted"
        # TaskRegistry 只随 canonical 投影刷新，CLI 读取实际配置 owner 的同一投影库。
        assert task_commands._task_store(_args(tmp_path)).task_registry.lookup_task(child.id)["status"] == "PAUSED"
        ledger = Path(task.agent_run_lessons_jsonl) if task.agent_run_lessons_jsonl else None
        assert ledger is None or not ledger.exists() or "cancelled" not in ledger.read_text(encoding="utf-8")
        # 已暂停再暂停：幂等，不换代。
        assert _cli(tmp_path, task_commands.cmd_task_pause, child.id) == 0
        assert task_control_record(agent.subagents.load(child.id))["generation"] == 1
    finally:
        lease.__exit__(None, None, None)


def test_cli_resume_waits_for_owning_runner_cleanup_then_publishes_pending_same_run(tmp_path, capsys):
    agent, child, attempt = _seed(tmp_path)
    lease, session = _owning_runner(agent, child.id, attempt)
    assert _cli(tmp_path, task_commands.cmd_task_pause, child.id) == 0
    # CLI 本进程找不到原执行轮的中断令牌/PTY，这不是退出证明：清理仍待原宿主确认，恢复被拒。
    assert _cli(tmp_path, task_commands.cmd_task_resume, child.id) == 1
    assert "TASK_CLEANUP_UNCONFIRMED" in capsys.readouterr().out
    assert agent.subagents.load(child.id).status == "PAUSED"
    # 原宿主收尾：同一租约的最终写入先落正向清理证明，并结束同一会话句柄。
    lease.__exit__(None, None, None)
    task = agent.subagents.load(child.id)
    cleanup = task_control_record(task)["cleanup"]
    assert cleanup["state"] == "confirmed" and cleanup["attempt_id"] == attempt
    assert cleanup["session_id"] == session["session_id"] and cleanup["worker_pid"] == os.getpid()
    assert task.attributes["runner_session"]["status"] == "completed"

    assert _cli(tmp_path, task_commands.cmd_task_resume, child.id) == 0
    task = agent.subagents.load(child.id)
    assert task.status == "PENDING" and task.runner_active_attempt_id == ""
    control = task_control_record(task)
    assert (control["generation"], control["action"], control["state"], control["resumed_from_generation"]) == (
        2, "resume", "applied", 1)
    assert agent.conversation_store.tasks.load_report(child.id)[0].status == "active"
    # 恢复只发布 PENDING；同一 run 由原派工预留与激活新的执行轮，不新建 run。
    repo = agent.subagents.runtime_db
    agent_run_id = repo.agent_run_for_run_id(child.id)["agent_run_id"]
    assert [row["attempt_id"] for row in repo.attempts_for_run(agent_run_id)] == [attempt]
    successor = reserve_runner_start(agent.subagents, child.id)
    prepared = agent.subagents.lifecycle.prepare_runner_attempt(child.id, expected_attempt_id=successor)
    assert prepared.id == child.id and prepared.status == "RUNNING" and prepared.runner_active_attempt_id == successor
    assert successor != attempt
    assert [row["attempt_id"] for row in repo.attempts_for_run(agent_run_id)] == [attempt, successor]
    # 已恢复的 run 不能再沿旧回执恢复。
    assert _cli(tmp_path, task_commands.cmd_task_resume, child.id) == 1


def test_cli_abandon_projects_abandoned_without_cancellation_result(tmp_path, capsys):
    agent, child, attempt = _seed(tmp_path)
    lease, session = _owning_runner(agent, child.id, attempt)
    try:
        assert _cli(tmp_path, task_commands.cmd_task_abandon, child.id) == 0
        task = agent.subagents.load(child.id)
        assert task.status == "ABANDONED" and task.ended_at > 0
        assert task.failure_type != "cancelled" and "cancel_subagents" not in task.attributes
        control = task_control_record(task)
        assert (control["action"], control["state"], control["target_status"]) == ("abandon", "applied", "ABANDONED")
        assert agent.subagents.runtime_db.agent_run_for_run_id(child.id)["status"] == "cancelled"
        assert agent.conversation_store.tasks.load_report(child.id)[0].status == "abandoned"
        capsys.readouterr()
        assert _cli(tmp_path, task_commands.cmd_task_abandon, child.id) == 0
        assert "已经是 ABANDONED" in capsys.readouterr().out
        assert _cli(tmp_path, task_commands.cmd_task_resume, child.id) == 1
        assert "TASK_NOT_PAUSED" in capsys.readouterr().out
        assert _cli(tmp_path, task_commands.cmd_task_pause, child.id) == 1
        assert agent.subagents.load(child.id).status == "ABANDONED"
    finally:
        lease.__exit__(None, None, None)
    # 已关闭任务也由原宿主收尾写正向清理并结束同一句柄（窄心跳对已关闭任务仍拒写）。
    task = agent.subagents.load(child.id)
    assert task_control_record(task)["cleanup"]["state"] == "confirmed"
    assert task.attributes["runner_session"]["session_id"] == session["session_id"]
    assert task.attributes["runner_session"]["status"] == "completed"
    assert task.status == "ABANDONED"


def test_cli_control_without_managed_authority_fails_closed(tmp_path, monkeypatch, capsys):
    agent, child, attempt = _seed(tmp_path)

    def unavailable(self, *_args, **_kwargs):
        raise OSError("simulated I/O error")

    monkeypatch.setattr(RuntimeRepository, "__init__", unavailable)
    for command in (task_commands.cmd_task_pause, task_commands.cmd_task_abandon, task_commands.cmd_task_resume):
        assert _cli(tmp_path, command, child.id) == 1
        assert "无法打开配置的任务权威" in capsys.readouterr().out
    monkeypatch.undo()
    # 托管权威不可用不等于未托管许可：canonical、运行账与投影都未被改写。
    task = agent.subagents.load(child.id)
    assert task.status == "RUNNING" and task.runner_active_attempt_id == attempt
    assert task_control_record(task) == {}
    assert agent.subagents.runtime_db.get_attempt(attempt)["status"] == "running"
    assert agent.local_store.task_registry.lookup_task(child.id)["status"] == "RUNNING"


def test_cli_control_rejects_projection_only_task_without_writing_projection(tmp_path, capsys):
    agent, _child, _attempt = _seed(tmp_path)
    registry = task_commands._task_store(_args(tmp_path)).task_registry
    registry.register_task(task_id="goal-only", status="RUNNING", goal="只有投影的任务")
    for command in (task_commands.cmd_task_pause, task_commands.cmd_task_abandon, task_commands.cmd_task_resume):
        assert _cli(tmp_path, command, "goal-only") == 1
        assert "任务 goal-only 不存在" in capsys.readouterr().out
    assert registry.lookup_task("goal-only")["status"] == "RUNNING"


def test_cli_show_and_list_read_configured_owner_projection(tmp_path, capsys):
    agent, child, attempt = _seed(tmp_path)
    lease, _session = _owning_runner(agent, child.id, attempt)
    try:
        assert _cli(tmp_path, task_commands.cmd_task_pause, child.id) == 0
        capsys.readouterr()
        assert task_commands.cmd_task_show(_args(tmp_path, task_id=child.id)) == 0
        assert "PAUSED" in capsys.readouterr().out
        assert task_commands.cmd_task_list(_args(tmp_path, user_id=None, status="PAUSED", limit=10)) == 0
        assert child.id in capsys.readouterr().out
    finally:
        lease.__exit__(None, None, None)


def test_cancel_request_projection_defaults_to_cancelled_and_user_stop_is_unchanged(tmp_path):
    agent, child, _attempt = _seed(tmp_path)
    task = agent.subagents.load(child.id)
    assert CancelSubagentTaskRequest(task, "r").projection == "CANCELLED"
    with pytest.raises(ValueError):
        CancelSubagentTaskRequest(task, "r", projection="PAUSED")
    with pytest.raises(ValueError):
        CancelSubagentTaskRequest(task, "r", projection="DONE", control_generation=1)
    # 原会话用户停止仍写 CANCELLED 与取消记录，并保持同 run 可恢复资格；任务控制不借用它。
    result = cancel_subagent_tree(agent, CancelSubagentTaskRequest(task, "conversation_user_stop", kill_process=False))
    assert result["cancel_status"] == "CANCELLED"
    stopped = agent.subagents.load(child.id)
    assert stopped.status == "CANCELLED" and stopped.attributes["cancel_subagents"]["reason"] == "conversation_user_stop"
    assert user_stopped_run_is_resumable(stopped)
    assert task_control_record(stopped) == {}
