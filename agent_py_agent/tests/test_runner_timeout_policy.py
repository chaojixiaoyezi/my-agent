"""Runner timeout policy tests kept separate from the large dispatch test module."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent_py_agent.agent.settings.config import AgentConfig


def _timeout_config(value: object) -> AgentConfig:
    config = AgentConfig()
    config.runner_timeout_seconds = value
    return config


def test_role_timeout_uses_exact_structured_role():
    from agent_py_agent.agent.agent_core.runner.gate import get_task_timeout

    config = _timeout_config("off")
    config.runner_timeout_by_role = {"coordinator": "12"}

    task = MagicMock()
    task.id = "child-coord-run"
    task.root_id = "root-run"
    task.parent_id = "root-run"
    task.role = "coordinator"
    task.attributes = {}
    task.goal = "协调市场研究小组。"
    task.plan = []

    assert get_task_timeout(task, 0.0, config) == 12.0


def test_runner_timeout_config_rejects_old_disable_words():
    from agent_py_agent.agent.settings import normalize_agent_config

    normalized, warnings = normalize_agent_config({"runner_timeout_seconds": "disabled"})

    assert normalized["runner_timeout_seconds"] == "off"
    assert any("runner_timeout_seconds" in warning for warning in warnings)


def test_runner_timeout_config_rejects_non_finite_numbers():
    from agent_py_agent.agent.settings import normalize_agent_config

    normalized, warnings = normalize_agent_config({"runner_timeout_seconds": "inf"})

    assert normalized["runner_timeout_seconds"] == "off"
    assert any("runner_timeout_seconds" in warning for warning in warnings)


def test_timed_runner_interrupts_blocking_transport_after_attempt_is_fenced():
    from agent_py_agent.agent.agent_core.runner.worker import (
        RunSubagentWorkerParams,
        _run_subagent_worker_with_timeout,
    )
    from agent_py_agent.agent.concurrency.interrupt import (
        register_interrupt_callback,
    )
    from agent_py_agent.agent.subagents.models import SubAgentRunnerResult

    transport_started = threading.Event()
    transport_aborted = threading.Event()
    lifecycle_events: list[str] = []

    def _run_subagent(*, params):
        del params
        with register_interrupt_callback(transport_aborted.set):
            transport_started.set()
            transport_aborted.wait(2)
        return SubAgentRunnerResult(
            run_id="run-timeout",
            dry_run=False,
            ok=False,
            status="CANCELLED",
            verification_status="UNVERIFIED",
            message="old attempt interrupted",
        )

    timeout_result = SubAgentRunnerResult(
        run_id="run-timeout",
        dry_run=False,
        ok=False,
        status="TIMEOUT",
        verification_status="UNVERIFIED",
        message="runner timed out",
    )
    worker = SimpleNamespace(
        run_subagent=_run_subagent,
        subagents=SimpleNamespace(
            lifecycle=SimpleNamespace(
                prepare_runner_attempt=lambda *_args, **_kwargs: SimpleNamespace(
                    runner_active_attempt_id="attempt-timeout"
                ),
                abandon_runner_attempt=lambda *_args, **_kwargs: lifecycle_events.append(
                    "abandoned"
                ),
            ),
            runner_result=SimpleNamespace(
                record_runner_result=lambda _params: timeout_result
            ),
        ),
    )
    params = RunSubagentWorkerParams(
        config=AgentConfig(),
        root=MagicMock(),
        run_id="run-timeout",
        instruction="wait",
        dry_run=False,
        max_cards=1,
        probe=False,
        retry_reason="",
        timeout_seconds=0.02,
    )

    result = _run_subagent_worker_with_timeout(worker, params)

    assert transport_started.is_set()
    assert lifecycle_events == ["abandoned"]
    assert transport_aborted.wait(0.5)
    assert result is timeout_result


def test_non_timed_runner_registers_exact_attempt_interrupt_token():
    from agent_py_agent.agent.agent_core.runner.worker import (
        RunSubagentWorkerParams,
        _run_subagent_worker_interruptibly,
    )
    from agent_py_agent.agent.concurrency.interrupt import (
        interrupt_by_name,
        wait_interruptibly,
    )

    started = threading.Event()
    observed_attempt_ids: list[str] = []
    result: dict[str, object] = {}

    def _run_subagent(*, params):
        observed_attempt_ids.append(params.attempt_id)
        started.set()
        wait_interruptibly(10)

    worker = SimpleNamespace(
        run_subagent=_run_subagent,
        subagents=SimpleNamespace(
            lifecycle=SimpleNamespace(
                prepare_runner_attempt=lambda *_args, **_kwargs: SimpleNamespace(
                    runner_active_attempt_id="attempt-normal"
                ),
            ),
        ),
    )
    params = RunSubagentWorkerParams(
        config=AgentConfig(),
        root=MagicMock(),
        run_id="run-normal",
        instruction="wait",
        dry_run=False,
        max_cards=1,
        probe=False,
        retry_reason="",
        timeout_seconds=0.0,
    )

    def _target() -> None:
        try:
            _run_subagent_worker_interruptibly(worker, params)
        except InterruptedError:
            result["interrupted"] = True

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    assert started.wait(1.0)

    assert interrupt_by_name("subagent-runner-attempt:run-normal:attempt-normal") is True
    thread.join(1.0)

    assert result == {"interrupted": True}
    assert observed_attempt_ids == ["attempt-normal"]
    assert thread.is_alive() is False


def test_audit_runner_uses_stream_inactivity_not_total_wall_time():
    import time

    from agent_py_agent.agent.agent_core.runner.worker import (
        RunSubagentWorkerParams,
        _run_subagent_worker_with_timeout,
    )
    from agent_py_agent.agent.common.audit_activation import (
        AUDIT_ATTR,
        AUDIT_SOURCE_ID_ATTR,
        AUDIT_SOURCE_OWNER_HOME_ATTR,
        AUDIT_SOURCE_WATCH_ID_ATTR,
        AUDIT_SOURCE_WORKER_ATTR,
        AUDIT_SOURCE_WORKER_KEY_ATTR,
        audit_source_worker_key,
    )
    from agent_py_agent.agent.contracts.model_call_ledger import (
        ModelCallActivityParams,
        ModelCallLedger,
        ModelCallStartedParams,
    )
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_REQUEST_ID_ATTR,
    )
    from agent_py_agent.agent.subagents.models import SubAgentRunnerResult

    audit_id = "audit-idle-watchdog"
    watch_id = "watch-idle-watchdog"
    run_id = "run-idle-watchdog"
    task = SimpleNamespace(
        id=run_id,
        last_progress_at=0.0,
        attributes={
            AUDIT_ATTR: True,
            CONVERSATION_REQUEST_ID_ATTR: audit_id,
            AUDIT_SOURCE_WORKER_ATTR: True,
            AUDIT_SOURCE_ID_ATTR: "source-idle-watchdog",
            AUDIT_SOURCE_WATCH_ID_ATTR: watch_id,
            AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key(
                audit_id,
                watch_id,
            ),
            AUDIT_SOURCE_OWNER_HOME_ATTR: "/tmp/owner",
        },
    )
    ledger = ModelCallLedger()
    lifecycle_events: list[str] = []

    def run_subagent(*, params):
        del params
        ledger.started(
            ModelCallStartedParams(
                call_id="call-progressing",
                backend="test",
                model="test",
                input_tokens=100,
                run_id=run_id,
            )
        )
        for _index in range(8):
            time.sleep(0.01)
            ledger.activity(
                ModelCallActivityParams(
                    call_id="call-progressing",
                    output_tokens_seen=1,
                )
            )
        return SubAgentRunnerResult(
            run_id=run_id,
            dry_run=False,
            ok=True,
            status="DONE",
            verification_status="VERIFIED",
            message="completed after sustained stream activity",
        )

    worker = SimpleNamespace(
        _model_call_ledger=ledger,
        run_subagent=run_subagent,
        subagents=SimpleNamespace(
            load=lambda _run_id: task,
            lifecycle=SimpleNamespace(
                prepare_runner_attempt=lambda *_args, **_kwargs: SimpleNamespace(
                    runner_active_attempt_id="attempt-progressing"
                ),
                abandon_runner_attempt=lambda *_args, **_kwargs: lifecycle_events.append(
                    "abandoned"
                ),
            ),
            runner_result=SimpleNamespace(
                record_runner_result=lambda _params: (_ for _ in ()).throw(
                    AssertionError("progressing Audit runner must not time out")
                )
            ),
        ),
    )
    params = RunSubagentWorkerParams(
        config=AgentConfig(),
        root=MagicMock(),
        run_id=run_id,
        instruction="judge",
        dry_run=False,
        max_cards=1,
        probe=False,
        retry_reason="",
        timeout_seconds=0.025,
    )

    started = time.monotonic()
    result = _run_subagent_worker_with_timeout(worker, params)

    assert time.monotonic() - started > params.timeout_seconds * 2
    assert result.status == "DONE"
    assert lifecycle_events == []


def test_pending_source_slice_starts_exact_run_without_parent_wake(monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration.dispatch import (
        capability_auto_sweep,
    )
    from agent_py_agent.agent.agent_core.runner.worker import (
        _continue_source_worker_after_session,
    )

    started: list[str] = []
    monkeypatch.setattr(
        capability_auto_sweep,
        "auto_start_orphan_run",
        lambda _worker, run_id: started.append(run_id)
        or {"started": 1, "status": "started", "run_ids": [run_id]},
    )

    _continue_source_worker_after_session(
        SimpleNamespace(),
        "source-run-1",
        SimpleNamespace(status="PENDING"),
    )
    _continue_source_worker_after_session(
        SimpleNamespace(),
        "ordinary-done-run",
        SimpleNamespace(status="DONE"),
    )

    assert started == ["source-run-1"]
