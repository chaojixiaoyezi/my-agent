"""验证工具循环冻结 DTO 的 dataclass 合同及相关运行行为。"""
from __future__ import annotations

import copy
import hashlib
import inspect
import json
import pickle
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, fields, make_dataclass, replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import (
    model_turn,
    natural_user_reply,
    response_decision,
    round_execution,
)
from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.runtime_context import ThreadLocalAgentAttribute

_DTO_CASES = {
    round_execution.ToolCallRecordParams: (
        (None, 1, 2, "call", "result"),
        {"model_call": "provider-call", "actor": "decision", "deferred": True},
    ),
    round_execution.ToolCallExecuteParams: (
        (None, 1, 2, "call"),
        {"model_call": "provider-call", "actor": "host"},
    ),
    round_execution.ToolRoundExecutionRequest: (
        (None, "params", 1, "response", [], tuple, list),
        {"current_prompt": "follow-up", "actor": "decision", "decision_ref": "record"},
    ),
    round_execution.ToolProgressEvent: (
        (None, 1, "call", "started", "开始"),
        {"phase": "finished", "started_at": 1.25, "result": "result"},
    ),
    round_execution._ParallelThreadContext: (
        ((("_current_user_prompt", "prompt"),), "run", "attempt", None),
        {"task_attributes": {"thread": "test"}},
    ),
    response_decision.ToolLoopRepairCounters: (
        (),
        {"cumulative_turn_repairs": 2},
    ),
    response_decision.ToolLoopResponseDecision: (
        ("continue", None, [], None),
        {"action": "break", "response": "reply"},
    ),
    response_decision.UnresolvedRuntimeIssueDecision: (
        ("continue", None, [], None),
        {"action": "break", "response": "reply"},
    ),
    response_decision.UnresolvedRuntimeIssueDecisionRequest: (
        (None, "params", "response", None),
        {"response": "new response"},
    ),
    response_decision._NoToolCallsRequest: (
        (None, "params", "response", None, False),
        {"probe_rollback": "prior response"},
    ),
    model_turn.ModelTurnRequest: (
        ("prompt", "response", "params"),
        {"prompt": "follow-up"},
    ),
}


def _hash_outcome(obj):
    try:
        return hash(obj)
    except TypeError as error:
        return type(error), str(error)


def _assert_same_value(actual, plain):
    assert tuple(getattr(actual, field.name) for field in fields(actual)) == tuple(
        getattr(plain, field.name) for field in fields(plain)
    )
    assert repr(actual) == repr(plain)
    assert _hash_outcome(actual) == _hash_outcome(plain)


@pytest.mark.parametrize("cls", _DTO_CASES, ids=lambda cls: cls.__name__)
def test_converted_dto_matches_plain_dataclass(cls, monkeypatch):
    args, changes = _DTO_CASES[cls]
    # 用普通 dataclass 从同一字段定义独立生成构造、默认值和冻结语义。
    plain_cls = make_dataclass(
        cls.__name__,
        [(field.name, field.type, copy.copy(field)) for field in fields(cls)],
        frozen=True,
        namespace={"__module__": __name__},
    )
    monkeypatch.setattr(sys.modules[__name__], plain_cls.__name__, plain_cls, raising=False)
    assert inspect.signature(cls) == inspect.signature(plain_cls)

    instance = cls(*args)
    plain = plain_cls(*args)
    _assert_same_value(instance, plain)
    assert instance == cls(*args)
    assert plain == plain_cls(*args)

    updated = fast_replace(instance, **changes)
    expected = replace(plain, **changes)
    _assert_same_value(updated, expected)
    assert (updated == instance) == (expected == plain)
    assert updated == replace(instance, **changes)
    if hasattr(copy, "replace"):
        assert copy.replace(instance, **changes) == updated

    for value in (instance, updated, plain, expected):
        restored = pickle.loads(pickle.dumps(value))
        assert type(restored) is type(value)
        _assert_same_value(restored, value)
        field_name = fields(value)[0].name
        with pytest.raises(FrozenInstanceError):
            setattr(value, field_name, None)
        with pytest.raises(FrozenInstanceError):
            delattr(value, field_name)

    with pytest.raises(TypeError) as standard_error:
        replace(instance, unexpected_field=True)
    with pytest.raises(type(standard_error.value)) as fast_error:
        fast_replace(instance, unexpected_field=True)
    assert str(fast_error.value) == str(standard_error.value)

    assert hasattr(type(instance), "__slots__")
    assert type(instance) in fast_dataclass.CONVERTED_CLASSES


class _ThreadContextHost:
    _current_user_prompt = ThreadLocalAgentAttribute("_current_user_prompt")
    _current_run_params = ThreadLocalAgentAttribute("_current_run_params")
    _current_skill_snapshot = "shared"


def test_parallel_context_preserves_missing_and_none_on_worker_exit():
    host = _ThreadContextHost()
    host._current_user_prompt = "main"
    host._current_run_params = None
    captured = round_execution._capture_parallel_thread_context(host)
    assert captured.transient_values == (("_current_user_prompt", "main"), ("_current_run_params", None))

    def worker():
        host._current_user_prompt = "worker"

        def verify_captured_context():
            assert host._current_user_prompt == "main"
            assert host._current_run_params is None
            assert host._current_skill_snapshot == "shared"
            raise RuntimeError("simulated worker error")

        with pytest.raises(RuntimeError, match="simulated worker error"):
            round_execution.run_with_parallel_thread_context(host, captured, verify_captured_context)
        assert host._current_user_prompt == "worker"
        assert not hasattr(host, "_current_run_params")

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(worker).result(timeout=5)
    assert host._current_user_prompt == "main"
    assert host._current_run_params is None


def test_reply_feedback_preserves_schema_digest_bytes():
    params = SimpleNamespace(run_id="run", attempt_id="attempt", live_archive_state={})
    arguments = {"z": ["中文", {"b": "🚦", "a": 1}], "a": True}
    feedback = {"tool_calls": [{"id": "call", "name": "lookup", "input": arguments}]}
    assistant, result = natural_user_reply._rejected_tool_feedback_ir(params, feedback)

    expected = json.dumps(
        {"name": "lookup", "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert assistant.tool_calls[0].schema_hash == "sha256:" + hashlib.sha256(expected).hexdigest()
    assert result.call_id == assistant.tool_calls[0].call_id


# 用显式的构造和替换调用测真实 DTO；每个 worker 自持可变参数。
def g1_case_tool_call_record_params():
    cls = round_execution.ToolCallRecordParams
    return lambda: fast_replace(cls(None, 1, 2, "call", "result"), actor="decision")


def g1_case_tool_call_execute_params():
    cls = round_execution.ToolCallExecuteParams
    return lambda: fast_replace(cls(None, 1, 2, "call"), actor="host")


def g1_case_tool_round_execution_request():
    cls = round_execution.ToolRoundExecutionRequest
    calls = []
    return lambda: fast_replace(cls(None, "params", 1, "response", calls, tuple, list), params="updated")


def g1_case_tool_progress_event():
    cls = round_execution.ToolProgressEvent
    return lambda: fast_replace(cls(None, 1, "call", "started", "started"), phase="finished")


def g1_case_parallel_thread_context():
    cls = round_execution._ParallelThreadContext
    prompt = "prompt"
    transient_values = (("_current_user_prompt", prompt),)
    return lambda: fast_replace(cls(transient_values, "run", "attempt", None), subagent_attempt_id="next_attempt")


def g1_case_tool_loop_repair_counters():
    cls = response_decision.ToolLoopRepairCounters
    return lambda: fast_replace(cls(), cumulative_turn_repairs=2)


def g1_case_tool_loop_response_decision():
    cls = response_decision.ToolLoopResponseDecision
    calls = []
    return lambda: fast_replace(cls("continue", None, calls, None), action="break")


def g1_case_unresolved_runtime_issue_decision():
    cls = response_decision.UnresolvedRuntimeIssueDecision
    calls = []
    return lambda: fast_replace(cls("continue", None, calls, None), action="break")


def g1_case_unresolved_runtime_issue_decision_request():
    cls = response_decision.UnresolvedRuntimeIssueDecisionRequest
    return lambda: fast_replace(cls(None, "params", "response", None), response="updated")


def g1_case_no_tool_calls_request():
    cls = response_decision._NoToolCallsRequest
    return lambda: fast_replace(cls(None, "params", "response", None, False), probe_rollback="prior_response")


def g1_case_model_turn_request():
    cls = model_turn.ModelTurnRequest
    return lambda: fast_replace(cls("prompt", "response", "params"), prompt="followup")
