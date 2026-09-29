"""被墙钟或用户停止放弃的物理模型调用，其迟到的工作线程不得再提交插话或发出请求（2026-09-29）。

复现的竞态：guard 在 0.05 秒墙钟后放弃第一次调用并转入重试；第一次调用的工作线程此时还没走到发出前的登记，
放行后它会把插话提交到已经作废的调用编号上，重试的提交随即撞上 "guidance submission was not reserved"。
这里用确定性钩子把工作线程停在"放弃之后、发出之前"，再放行，只看结构化事实：提交批次、后端调用次数、账本事件。
"""
from __future__ import annotations

import threading
import time
from queue import Queue
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core.tool_model_generation import generate_model_response
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.tests.test_steer_delivery_recovery import _batches, _generation_request, _steer


# 类用途: 每次调用都立刻回复的假后端，只记调用次数；被放弃的调用如果真的发出就会被计数。
class _AcceptBackend:
    name = "accept"

    def __init__(self) -> None:
        self.calls = 0

    # 函数用途: 记一次调用并返回固定回复。
    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        del prompt, on_chunk
        self.calls += 1
        return ModelResponse(text="accepted", backend=self.name)


# 函数用途: 轮询账本直到指定调用出现某个事件名（结构化事实），最多等 bound 秒。
def _wait_for_event(ledger: ModelCallLedger, call_id: str, event: str, bound: float = 5.0) -> bool:
    deadline = time.monotonic() + bound
    while time.monotonic() < deadline:
        record = next((item for item in ledger.records() if item.call_id == call_id), None)
        if record is not None and event in record.events:
            return True
        time.sleep(0.01)
    return False


# 函数用途: 生成三个钩子：第一次调用的工作线程停在发出前直到 gate 放行；第一次调用只等 0.05 秒墙钟；记录插话提交所用的调用编号。
def _race_hooks(store: ConversationStore, gate: threading.Event):
    entered: list[str] = []
    wall_timeouts: list[float] = []
    submitted_call_ids: list[str] = []
    original_backend_response = generation._generate_backend_response
    original_wait = generation._wait_for_generation_result
    original_mark = store.guidance.submissions.mark_submitted

    def hold_first_worker(request_, state, timeout):
        entered.append(state.call_id)
        if len(entered) == 1:
            assert gate.wait(timeout=10.0)
        return original_backend_response(request_, state, timeout)

    def short_first_wall_clock(*call):
        *wait_args, timeout = call
        timeout = timeout if wall_timeouts else 0.05
        wall_timeouts.append(timeout)
        return original_wait(*wait_args, timeout)

    def observed_mark_submitted(turn_id, entries, **kwargs):
        submitted_call_ids.append(str(kwargs.get("provider_call_id") or ""))
        return original_mark(turn_id, entries, **kwargs)

    hooks = {"_generate_backend_response": hold_first_worker, "_wait_for_generation_result": short_first_wall_clock}
    return hooks, observed_mark_submitted, SimpleNamespace(entered=entered, wall_timeouts=wall_timeouts, submitted=submitted_call_ids)


def test_abandoned_call_worker_skips_submission_and_send_after_wall_timeout(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    entry = _steer(store, "req-steer", "steer-a")
    backend = _AcceptBackend()
    request = _generation_request(store, backend, entry, timeout=10.0)
    gate = threading.Event()
    hooks, observed_mark_submitted, seen = _race_hooks(store, gate)
    for name, hook in hooks.items():
        monkeypatch.setattr(generation, name, hook)
    monkeypatch.setattr(store.guidance.submissions, "mark_submitted", observed_mark_submitted)

    try:
        assert generate_model_response(request).text == "accepted"
    finally:
        gate.set()  # 放行被放弃的第一次调用的工作线程

    abandoned_call, retry_call = seen.entered
    ledger = generation.model_call_ledger(request.agent)
    assert _wait_for_event(ledger, abandoned_call, generation.MODEL_CALL_SUBMISSION_SKIPPED_EVENT)
    abandoned = next(item for item in ledger.records() if item.call_id == abandoned_call)
    assert abandoned.status == "timed_out" and abandoned.provider_attempt_count == 0
    assert seen.wall_timeouts == [0.05, 10.0]
    assert seen.submitted == [retry_call]  # 迟到线程没有把插话提交到作废的调用编号上
    assert backend.calls == 1  # 也没有再发出请求
    assert _batches(store, "req-steer") == [(retry_call, "submitted", [entry.guidance_id])]
    assert store.guidance.receipt("steer-a").submission_id == retry_call


def test_user_stop_marks_the_call_abandoned_before_the_worker_sends(monkeypatch):
    monkeypatch.setattr(generation, "is_interrupted", lambda: True)
    state = SimpleNamespace(liveness=generation._CallLiveness())
    worker = threading.Thread(target=lambda: None)
    worker.start()
    worker.join()
    with pytest.raises(InterruptedError):
        generation._wait_for_generation_result(
            SimpleNamespace(agent=SimpleNamespace(backend=None)), state, Queue(), worker, 1.0,
        )
    ran: list[str] = []
    assert state.liveness.abandoned
    assert generation._CallLiveness.run_if_current(state.liveness, lambda: ran.append("sent")) is False
    assert ran == []


def test_liveness_runs_the_registration_exactly_once_while_current():
    liveness = generation._CallLiveness()
    ran: list[str] = []
    assert liveness.run_if_current(lambda: ran.append("sent")) is True
    liveness.abandon()
    assert liveness.run_if_current(lambda: ran.append("late")) is False
    assert ran == ["sent"]


def test_ledger_note_event_keeps_terminal_status_and_dedupes():
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="call-1", backend="fake", model="m", input_tokens=1))
    ledger.failed(ModelCallFailureParams(call_id="call-1", error_type="X", error_code="Y"))
    first = ledger.note_event("call-1", "submission_skipped_after_abandon")
    second = ledger.note_event("call-1", "submission_skipped_after_abandon")
    assert first.status == second.status == "failed"
    assert second.events[-2:] == ("failed", "submission_skipped_after_abandon")
    assert second.events.count("submission_skipped_after_abandon") == 1
