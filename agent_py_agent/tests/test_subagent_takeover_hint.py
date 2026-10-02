"""C4（2026-10-02）：子代理执行器已退出时，给直属父级的唤醒回执里附结构化接替提示（开关默认关）。

O1 真实复测：子代理 runner 被 SIGKILL 后只以 BLOCKED 通知父级，blocked_reason 是子代理自己写的、被杀时为空；宿主侧的事实是
执行器退出（executor_process_died）。4 次唤醒模型都没有在 create_subagents 里声明 replacement_for_run_ids。现在：
1. 开关 subagent_takeover_hint_enabled 打开时，宿主在执行器退出收口那一份结果上附接替提示：哪个 run、退出原因码、
   怎么用 replacement_for_run_ids 声明接替（contracts.subagent_completion.subagent_takeover_hint）；
2. 提示随完成信封进唤醒 metadata、观察摘要和生命周期唤醒事件（父级模型看到的回执），各消费方共用同一合同投影；
3. 开关关闭（缺省）时唤醒回执与改前一致；提示只属于那一份结果，之后的结果或受控取消通知不带旧提示。
"""
from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace

from agent_py_agent.agent.capability.config import CapabilityConfig
from agent_py_agent.agent.contracts.subagent_completion import (
    SUBAGENT_TAKEOVER_HINT_SCHEMA_VERSION,
    completion_takeover_hint_facts,
    subagent_takeover_hint,
)
from agent_py_agent.agent.conversation.lifecycle_wake_event import lifecycle_wake_turn_trigger
from agent_py_agent.agent.runtime_db.executor_liveness import attempt_executor
from agent_py_agent.agent.subagents.runner_completion_payload import completion_handoff_payload
from agent_py_agent.agent.subagents.services.executor_recovery import recover_exited_runner
from agent_py_agent.tests.test_dispatch_liveness_and_revive import (
    _closeout_fixture,
    _wakes_for_attempt,
)


def _dead_executor(tmp_path):
    manager, _store, task, attempt, _run, _params, _ = _closeout_fixture(tmp_path, owner="test/takeover-hint")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='running', ended_at=0, metadata_json=? WHERE attempt_id=?",
                     (json.dumps({"runner_pid": child.pid}), attempt))
    return manager, manager.load(task.id), attempt


def _wake(tmp_path, task_id: str, attempt: str) -> dict:
    [(_path, payload)] = _wakes_for_attempt(tmp_path, task_id, attempt)
    return payload


def _observation_summaries(tmp_path) -> list[str]:
    rows = [json.loads(line) for path in sorted(tmp_path.rglob("observations/*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [str(row.get("summary") or "") for row in rows if row.get("event_type") == "subagent_runner_finished"]


def _event_child(payload: dict) -> dict:
    trigger = lifecycle_wake_turn_trigger("subagent_runner_finished", payload, ())
    return json.loads(trigger.event_facts)["child"]


def test_takeover_hint_contract_only_rebuilds_the_fixed_shape():
    hint = subagent_takeover_hint("run-1", "executor_process_died", True)
    assert hint == {
        "schema_version": SUBAGENT_TAKEOVER_HINT_SCHEMA_VERSION,
        "run_id": "run-1",
        "reason": "executor_process_died",
        "uncertain_effects": True,
        "declare_with": {"tool": "create_subagents", "replacement_for_run_ids": ["run-1"]},
    }
    noisy = {**hint, "extra": "x", "uncertain_effects": "yes", "declare_with": {"tool": "other"}}
    assert completion_takeover_hint_facts({"takeover_hint": noisy}) == {
        "takeover_hint": subagent_takeover_hint("run-1", "executor_process_died", False)}
    assert completion_takeover_hint_facts({"takeover_hint": {**hint, "schema_version": "v0"}}) == {}
    assert completion_takeover_hint_facts({"takeover_hint": {**hint, "run_id": " "}}) == {}
    assert completion_takeover_hint_facts({"takeover_hint": {**hint, "reason": ""}}) == {}


def test_dead_executor_with_the_switch_on_hands_the_parent_a_takeover_hint(tmp_path):
    manager, task, attempt = _dead_executor(tmp_path)

    result = recover_exited_runner(manager, task, takeover_hint=True)

    assert result["reason"] == "executor_process_died" and manager.load(task.id).status == "FAILED"
    expected = subagent_takeover_hint(task.id, "executor_process_died", False)
    payload = _wake(tmp_path, task.id, attempt)
    assert payload["metadata"]["takeover_hint"] == expected
    # 父级模型看到的生命周期唤醒事件里带同一份结构化提示：哪个 run、为什么、怎么声明接替。
    assert _event_child(payload)["takeover_hint"]["declare_with"] == {
        "tool": "create_subagents", "replacement_for_run_ids": [task.id]}
    [summary] = _observation_summaries(tmp_path)
    assert f'replacement_for_run_ids=["{task.id}"]' in summary and "executor_process_died" in summary
    assert "接手前先核对" not in summary


def test_unknown_effects_keep_blocked_and_the_hint_says_to_check_first(tmp_path):
    manager, task, attempt = _dead_executor(tmp_path)
    run = manager.runtime_db.agent_run_for_run_id(task.id)["agent_run_id"]
    with manager.runtime_db.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET metadata_json='{}' WHERE attempt_id=?", (attempt,))
    with attempt_executor(manager.runtime_db, task.id, attempt), manager.runtime_db.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES('effect',?,?,1,1,'write_file','EXECUTING',1,1,1)", (run, attempt),
        )

    recover_exited_runner(manager, manager.load(task.id), takeover_hint=True)

    assert manager.load(task.id).status == "BLOCKED"
    hint = _wake(tmp_path, task.id, attempt)["metadata"]["takeover_hint"]
    assert hint == subagent_takeover_hint(task.id, "executor_returned_without_result", True)
    [summary] = _observation_summaries(tmp_path)
    assert "接手前先核对" in summary


def test_switch_off_leaves_the_parent_receipt_unchanged(tmp_path):
    manager, task, attempt = _dead_executor(tmp_path)

    recover_exited_runner(manager, task)

    payload = _wake(tmp_path, task.id, attempt)
    assert "takeover_hint" not in payload["metadata"]
    assert "takeover_hint" not in _event_child(payload)
    assert all("replacement_for_run_ids" not in summary for summary in _observation_summaries(tmp_path))
    assert "takeover_hint" not in manager.load(task.id).attributes


def test_hint_belongs_to_that_one_result_only(tmp_path):
    manager, task, _attempt = _dead_executor(tmp_path)
    recover_exited_runner(manager, task, takeover_hint=True)
    failed = manager.load(task.id)
    assert completion_handoff_payload(failed)["takeover_hint"]["run_id"] == task.id

    # 之后的受控取消等其它状态通知不带旧提示。
    failed.status = "CANCELLED"
    assert "takeover_hint" not in completion_handoff_payload(failed)


def test_dead_runner_sweep_reads_the_switch_from_the_capability_snapshot(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep

    for enabled in (True, False):
        manager, task, attempt = _dead_executor(tmp_path / str(enabled))
        agent = SimpleNamespace(
            subagents=manager, conversation_store=manager.conversation_store,
            _capability_config_runtime_snapshot=SimpleNamespace(
                config=CapabilityConfig(subagent_takeover_hint_enabled=enabled)),
        )
        monkeypatch.setattr(capability_auto_sweep, "conversation_lifecycle_decisions",
                            lambda _agent, tasks: {item.id: SimpleNamespace(allowed=True, should_complete=False,
                                                                            reason="") for item in tasks})

        [reclaimed] = capability_auto_sweep._reclaim_dead_running_runs(agent)

        assert reclaimed["reason"] == "executor_process_died"
        metadata = _wake(tmp_path / str(enabled), task.id, attempt)["metadata"]
        assert ("takeover_hint" in metadata) is enabled
    assert CapabilityConfig().subagent_takeover_hint_enabled is False


def test_every_consumer_projects_the_same_contract_hint():
    from agent_py_agent.agent.agent_core.runner.prompt_context_summary import (
        _direct_child_prompt_row,
    )
    from agent_py_agent.agent.agent_core.runtime.guidance import _task_event_payload
    from agent_py_agent.agent.contracts.subagent_completion import (
        SUBAGENT_COMPLETION_SCHEMA_VERSION,
        subagent_completion_context_from_observations,
    )
    from agent_py_agent.agent.conversation.models import WakeSignal

    hint = subagent_takeover_hint("child-1", "executor_process_died", True)
    metadata = {"task_id": "child-1", "status": "BLOCKED", "completion_schema_version": SUBAGENT_COMPLETION_SCHEMA_VERSION,
                "takeover_hint": {**hint, "extra": "不外露"}}
    # 前台活动回合事件。
    event = WakeSignal(wake_signal_id="wake-1", thread_id="thread-1", reason="subagent_runner_finished",
                       source_agent_id="child-1", root_task_id="root-1", metadata=metadata)
    assert _task_event_payload(event)["takeover_hint"] == hint
    # 递归父级的直属孩子快照。
    assert _direct_child_prompt_row({"run_id": "child-1", "status": "BLOCKED", "takeover_hint": hint})["takeover_hint"] == hint
    # 后台完成清单。
    observation = SimpleNamespace(event_type="subagent_runner_finished", root_task_id="root-1", parent_agent_id="root-1",
                                  source_agent_id="child-1", metadata=metadata, observed_at=1.0)
    context, issues = subagent_completion_context_from_observations([observation], root_task_ids={"root-1"})
    assert issues == () and context["items"][0]["takeover_hint"] == hint


def test_a_result_without_a_hint_clears_the_previous_one():
    from agent_py_agent.agent.subagents.services.executor_recovery import (
        TAKEOVER_HINT_ATTR,
        record_takeover_hint,
    )

    hint = subagent_takeover_hint("run-1", "executor_process_died", False)
    task = SimpleNamespace(attributes={})
    record_takeover_hint(task, hint)
    assert task.attributes[TAKEOVER_HINT_ATTR] == hint and task.attributes[TAKEOVER_HINT_ATTR] is not hint
    record_takeover_hint(task, None)
    assert TAKEOVER_HINT_ATTR not in task.attributes
