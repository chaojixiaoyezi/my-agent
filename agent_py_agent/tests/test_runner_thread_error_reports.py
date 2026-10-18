"""错误展示只消费实际runner线程出处，不创建执行许可或泄漏上下文对象。"""
from __future__ import annotations

import copy

import pytest

from agent_py_agent.agent.gateway_parts import multiprocess_subagent_execution as child_execution
from agent_py_agent.agent.runtime_errors import runtime_error_report
from agent_py_agent.tests import test_runner_thread_start_boundary as original_cases


@pytest.mark.parametrize("kind", ["heartbeat", "timeout_worker"])
def test_actual_runner_thread_unknown_uses_contract_without_exposing_context(tmp_path, monkeypatch, kind):
    observed = []
    original = child_execution._observe_original_child_unknown

    def observer(execution, error):
        observed.append((runtime_error_report(error), runtime_error_report(copy.copy(error))))
        return original(execution, error)

    monkeypatch.setattr(child_execution, "_observe_original_child_unknown", observer)
    original_cases.test_original_ack_and_join_unknown_keeps_same_error_claim_and_no_runner_result(
        tmp_path, monkeypatch, kind)
    assert observed
    for actual, copied in observed:
        assert actual["category"] == "state" and actual["recoverable"] is False
        assert actual["error_code"] == "TASK_RESOURCE_STOP_UNCONFIRMED"
        assert actual["recommended_action"] == "manual_review"
        assert actual["operation_entered"] is None
        assert actual["phase"] == ("child_runner_heartbeat_stop" if kind == "heartbeat" else "child_runner_worker_stop")
        assert actual["runner_run_id"] and actual["runner_attempt_id"]
        assert not {"call_id", "captured", "thread", "origin", "context_bundle"}.intersection(actual)
        assert copied["category"] == "programmer_bug" and "error_code" not in copied


def test_ordinary_same_code_and_fields_never_gain_runner_authority():
    error = RuntimeError("ordinary failure")
    error.error_code = "TASK_RESOURCE_STOP_UNCONFIRMED"
    error.phase = "child_runner_worker_stop"
    error.runner_run_id = "claimed-run"
    error.runner_attempt_id = "claimed-attempt"
    report = runtime_error_report(error)
    assert report["category"] == "programmer_bug" and "recommended_action" not in report
