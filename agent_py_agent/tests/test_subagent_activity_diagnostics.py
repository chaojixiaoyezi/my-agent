from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.runner import activity_diagnostics as diagnostics
from agent_py_agent.agent.agent_core.runtime.guidance import _task_event_payload
from agent_py_agent.agent.capability.config import CapabilityConfig, load_capability_config
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallRecord
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
    current_activity_diagnostic,
    mark_parent_waiting_for_direct_children,
    reconcile_parent_wait_for_child,
)
from agent_py_agent.agent.subagents.manager import SubAgentManager


def _environment(tmp_path, monkeypatch):
    # 测试默认没有审批；专门用例替换这一 canonical 查询，不影响诊断采样本身。
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.agent_tool_approval.list_pending_agent_tool_approvals",
        lambda *args, **kwargs: [],
    )
    clock = SimpleNamespace(wall=2000.0, mono=1000.0)
    monkeypatch.setattr(diagnostics, "time", SimpleNamespace(time=lambda: clock.wall, monotonic=lambda: clock.mono))
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({"canonical_user_id": "tester", "channel": "tui", "channel_conversation_id": "diagnostics"})
    manager = SubAgentManager(tmp_path / "subagents")
    manager.conversation_store = store
    task = manager.create_run(goal="检查文档链接", thought="执行", plan=["检查"], role="worker")
    task.parent_id = task.root_id = "root-task"
    task.status = "RUNNING"
    task.runner_active_attempt_id = "attempt-current"
    manager.save(task)
    for task_id in ("root-task", task.id):
        store.tasks.bind({"thread_id": thread.thread_id, "task_id": task_id, "goal": "测试", "status": "active"})
    records = []
    worker = SimpleNamespace(
        subagents=manager, conversation_store=store,
        _runner_activity_attempt_id=task.runner_active_attempt_id,
        _model_call_ledger=SimpleNamespace(records=lambda: list(records)),
        _capability_config_runtime_snapshot=SimpleNamespace(config=CapabilityConfig()),
    )
    return worker, manager.load(task.id), records, clock


def _model_record(task, **values):
    defaults = dict(call_id="call-a", backend="test", model="test", input_tokens=100000,
                    run_id=task.id, started_at=100.0, last_activity_at=100.0)
    return ModelCallRecord(**{**defaults, **values})


def test_long_first_token_wait_notifies_once_without_stopping_or_restarting(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task))
    for _ in range(3):
        diagnostics.observe_runner_activity(worker, task.id)
        clock.wall += 5
        clock.mono += 5
    current = worker.subagents.load(task.id)
    assert current.status == "RUNNING"
    assert current.runner_active_attempt_id == task.runner_active_attempt_id
    notice = current_activity_diagnostic(current)
    assert notice["phase"] == "first_token_wait"
    assert notice["failure_confirmed"] is False
    assert notice["automatic_action"] == "none"
    signals = worker.conversation_store.wakes.pending()
    assert len(signals) == 1
    assert _task_event_payload(signals[0])["activity_diagnostic"]["attempt_id"] == "attempt-current"
    worker.conversation_store.wakes.mark_handled(signals[0].wake_signal_id)
    diagnostics.observe_runner_activity(worker, task.id)
    assert not worker.conversation_store.wakes.pending()


def test_continuous_slow_stream_survives_hours_and_recovery_does_not_wake_again(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task, status="first_token", first_token_at=101.0))
    diagnostics.observe_runner_activity(worker, task.id)
    first = worker.conversation_store.wakes.pending()
    assert len(first) == 1
    for elapsed in (10, 3600, 10000):
        clock.mono += elapsed
        clock.wall += elapsed
        records[0] = replace(records[0], last_activity_at=clock.mono - 1)
        diagnostics.observe_runner_activity(worker, task.id)
    current = worker.subagents.load(task.id)
    assert current.status == "RUNNING"
    assert current_activity_diagnostic(current)["state"] == "progress_resumed"
    assert len(worker.conversation_store.wakes.pending()) == 1


def test_long_tool_is_reported_as_wait_not_failure(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    task.attributes["runtime_activity"] = {"kind": "runner_tool_call_started", "at": 500.0, "tool": "run_command"}
    worker.subagents.save(task)
    diagnostics.observe_runner_activity(worker, task.id)
    notice = current_activity_diagnostic(worker.subagents.load(task.id))
    assert notice["phase"] == "tool_wait"
    assert notice["tool"] == "run_command"
    assert notice["failure_confirmed"] is False


def test_provider_backoff_does_not_count_scheduled_delay_as_silence(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task))
    task.attributes["runtime_activity"] = {"kind": "runner_provider_retry_scheduled", "at": 1000.0, "delay_seconds": 1200, "attempt": 2}
    worker.subagents.save(task)
    diagnostics.observe_runner_activity(worker, task.id)
    assert not worker.conversation_store.wakes.pending()


@pytest.mark.parametrize("state", ["DONE", "CANCELLED", "BLOCKED", "PENDING"])
def test_non_running_attempt_is_never_notified_or_reanimated(tmp_path, monkeypatch, state):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    task.status = state
    worker.subagents.save(task)
    records.append(_model_record(task))
    diagnostics.observe_runner_activity(worker, task.id)
    assert worker.subagents.load(task.id).status == state
    assert not worker.conversation_store.wakes.pending()


def test_disabled_notices_and_zero_stage_threshold(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task))
    config = worker._capability_config_runtime_snapshot.config
    config.subagent_activity_notices_enabled = False
    diagnostics.observe_runner_activity(worker, task.id)
    config.subagent_activity_notices_enabled = True
    config.subagent_first_token_notice_seconds = 0
    diagnostics.observe_runner_activity(worker, task.id)
    assert not worker.conversation_store.wakes.pending()


def test_late_diagnostic_cannot_write_next_attempt(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    current = worker.subagents.load(task.id)
    current.runner_active_attempt_id = "attempt-new"
    worker.subagents.save(current)
    result = diagnostics._save_activity_diagnostic(worker.subagents, task, {"attempt_id": "attempt-current"})
    assert result is None
    assert "runtime_activity_diagnostic" not in worker.subagents.load(task.id).attributes


def test_recursive_wait_released_by_one_new_notice_not_every_heartbeat(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    parent = worker.subagents.create_run(goal="统筹解析", thought="等待", plan=["整合"], role="coordinator")
    parent.status = "PENDING"
    worker.subagents.save(parent)
    task.parent_id = parent.id
    worker.subagents.save(task)
    worker.subagents.add_child(parent.id, task.id)
    mark_parent_waiting_for_direct_children(worker.subagents, parent.id)
    records.append(_model_record(task))
    diagnostics.observe_runner_activity(worker, task.id)
    decision = reconcile_parent_wait_for_child(worker.subagents, task.id)
    assert decision.should_resume
    assert decision.active_run_ids == (task.id,)
    assert decision.terminal_run_ids == ()
    assert decision.attention_run_ids == (task.id,)
    assert not worker.conversation_store.wakes.pending()  # 不越级投给根会话。
    mark_parent_waiting_for_direct_children(worker.subagents, parent.id)
    assert not reconcile_parent_wait_for_child(worker.subagents, task.id).should_resume
    diagnostics.observe_runner_activity(worker, task.id)
    assert not reconcile_parent_wait_for_child(worker.subagents, task.id).should_resume


def test_shipped_capability_notice_settings_match_dataclass():
    from pathlib import Path

    config = load_capability_config(Path(__file__).parents[1] / "config/capability_config.yaml")
    defaults = CapabilityConfig()
    for name in ("subagent_activity_notices_enabled", "subagent_first_token_notice_seconds",
                 "subagent_stream_idle_notice_seconds", "subagent_tool_wait_notice_seconds"):
        assert getattr(config, name) == getattr(defaults, name)


def test_stale_progress_save_does_not_erase_notice_or_redeliver(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task))
    stale = worker.subagents.load(task.id)
    diagnostics.observe_runner_activity(worker, task.id)
    stale.current_step = "工具执行中"
    worker.subagents.save(stale)
    assert current_activity_diagnostic(worker.subagents.load(task.id))["state"] == "quiet"
    diagnostics.observe_runner_activity(worker, task.id)
    assert len(worker.conversation_store.wakes.pending()) == 1


def test_old_worker_does_not_diagnose_new_attempt(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task))
    task.runner_active_attempt_id = "new-attempt"
    worker.subagents.save(task)
    diagnostics.observe_runner_activity(worker, task.id)
    assert not worker.conversation_store.wakes.pending()


@pytest.mark.parametrize("kind,phase", [
    ("runner_model_request_started", "first_token_wait"),
    ("runner_model_stream_active", "stream_idle"),
    ("runner_tool_call_finished", "between_steps"),
])
def test_canonical_activity_preserves_phase_when_ledger_is_not_available(tmp_path, monkeypatch, kind, phase):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    task.attributes["runtime_activity"] = {"kind": kind, "at": 500.0}
    worker.subagents.save(task)
    diagnostics.observe_runner_activity(worker, task.id)
    assert current_activity_diagnostic(worker.subagents.load(task.id))["phase"] == phase


def test_missing_observation_does_not_invent_silence(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    clock.wall += 100000
    diagnostics.observe_runner_activity(worker, task.id)
    assert not worker.conversation_store.wakes.pending()


@pytest.mark.parametrize("kind", ["runner_tool_call_started", "runner_model_response_received"])
def test_known_pending_approval_is_not_called_tool_failure(tmp_path, monkeypatch, kind):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    task.attributes["runtime_activity"] = {"kind": kind, "at": 500, "tool": "run_command"}
    worker.subagents.save(task)
    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.agent_tool_approval.list_pending_agent_tool_approvals",
        lambda *args, **kwargs: [{"run_id": task.id, "request": {"permission_id": "permission-a", "arguments": "private"}}],
    )
    diagnostics.observe_runner_activity(worker, task.id)
    notice = current_activity_diagnostic(worker.subagents.load(task.id))
    assert notice["phase"] == "approval_wait"
    assert notice["permission_id"] == "permission-a"
    assert "private" not in str(notice)
    assert notice["automatic_action"] == "none"


def test_retry_after_notification_write_failure_keeps_original_dedupe_key(tmp_path, monkeypatch):
    worker, task, records, clock = _environment(tmp_path, monkeypatch)
    records.append(_model_record(task))
    notify = diagnostics.notify_parent_on_activity_notice
    monkeypatch.setattr(diagnostics, "notify_parent_on_activity_notice", lambda *args: False)
    diagnostics.observe_runner_activity(worker, task.id)
    notice_key = current_activity_diagnostic(worker.subagents.load(task.id))["notice_key"]
    assert not worker.conversation_store.wakes.pending()
    monkeypatch.setattr(diagnostics, "notify_parent_on_activity_notice", notify)
    diagnostics.observe_runner_activity(worker, task.id)
    assert len(worker.conversation_store.wakes.pending()) == 1
    assert current_activity_diagnostic(worker.subagents.load(task.id))["notice_key"] == notice_key
