"""验证模型流、决策、调用账本与跟踪数据类转换前后的运行时契约。"""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import json
import pickle
import time
import weakref
from typing import NamedTuple

import pytest

from agent_py_agent.agent.agent_core.runner.stage_trace import (
    RunnerModelStageTraceRequest,
    RunnerModelStreamActivityTraceRequest,
    RunnerProviderRetryTraceRequest,
    RunnerToolStageTraceRequest,
)
from agent_py_agent.agent.backends import errors
from agent_py_agent.agent.backends.decision_protocol import (
    DecisionAnswer,
    DecisionBinding,
    DecisionInputError,
    DecisionPrivacySkip,
    DecisionRequest,
    DecisionResponse,
)
from agent_py_agent.agent.backends.reasoning_control import (
    ReasoningPayloadLimits,
    _AnthropicBudgetDecision,
    _ReasoningLevelRule,
)
from agent_py_agent.agent.backends.stream_parsers import StreamCompletion, StreamEvent
from agent_py_agent.agent.backends.text_protocol_parser import ScannedTextBlock, TextBlockScan
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    CompactionSummary,
    RuntimeFactsTurn,
    UserTurn,
)
from agent_py_agent.agent.backends.tool_protocol_adapter import (
    ProviderToolCallResult,
    ToolProtocolViolation,
    _protocol_evidence,
)
from agent_py_agent.agent.backends.typesafe_decision import PreparedDecisionCall
from agent_py_agent.agent.backends.vision_capability import VisionCapability
from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.contracts import model_call_ledger as ledger
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


class _DtoCase(NamedTuple):
    dto: type
    args: tuple
    changes: dict

    def __call__(self):
        """为 G1 的每个线程提供原类型的独立构造操作。"""
        dto, args, _ = self
        return lambda: dto(*args)


_BINDING = DecisionBinding("check", "owner", "operation", "policy", "candidates")
_REQUEST = DecisionRequest(_BINDING, {}, {"choice": {}})
_CALL = ToolCall("call", "sample", {}, "native", "sha256:sample", "run", "turn", "attempt")

g1_case_tool_protocol_violation = _DtoCase(
    ToolProtocolViolation, ("invalid", "Invalid message body", "text"), {"evidence_preview": "message"},
)
g1_case_provider_tool_call_result = _DtoCase(ProviderToolCallResult, (), {"calls": (_CALL,)})
g1_case_stream_event = _DtoCase(StreamEvent, ("delta",), {"thinking_content": "reasoning"})
g1_case_stream_completion = _DtoCase(StreamCompletion, (), {"saw_message_stop": True})
g1_case_scanned_text_block = _DtoCase(ScannedTextBlock, (0, 6, 4, {}, None), {"body_chars": 6})
g1_case_text_block_scan = _DtoCase(TextBlockScan, ((), 0), {"unclosed_count": 1})
g1_case_assistant_turn = _DtoCase(AssistantTurn, ("reply",), {"tool_calls": [_CALL]})
g1_case_compaction_summary = _DtoCase(CompactionSummary, ("summary",), {"source": "local"})
g1_case_runtime_facts_turn = _DtoCase(RuntimeFactsTurn, ("fact",), {"source": "host"})
g1_case_user_turn = _DtoCase(UserTurn, ("message",), {"input_ids": ("message-2",)})
g1_case_decision_answer = _DtoCase(DecisionAnswer, ("choice", "label"), {"value": "yes"})
g1_case_decision_response = _DtoCase(
    DecisionResponse, (_BINDING, _REQUEST.input_digest, "asked", "selected", (), b"{}"), {"usage_reported": True},
)
g1_case_prepared_decision_call = _DtoCase(
    PreparedDecisionCall, (_REQUEST, {}, b"{}", "https://example.invalid", "selected", 2.0), {"deadline": 4.0},
)
g1_case_reasoning_level_rule = _DtoCase(_ReasoningLevelRule, ("medium", ("medium",), ("medium",), 6144), {"budget": 2048})
g1_case_reasoning_payload_limits = _DtoCase(ReasoningPayloadLimits, (4096,), {"levels": ("low",)})
g1_case_anthropic_budget_decision = _DtoCase(_AnthropicBudgetDecision, (1024,), {"reason_code": "bounded"})
g1_case_vision_capability = _DtoCase(VisionCapability, ("unknown", "profile"), {"evidence": "model profile"})
g1_case_model_call_ledger_options = _DtoCase(ledger.ModelCallLedgerOptions, (), {"max_records": 8})
g1_case_model_call_ledger_context = _DtoCase(ledger.ModelCallLedgerContext, (), {"now": time.time})
g1_case_model_call_started_params = _DtoCase(
    ledger.ModelCallStartedParams, ("call", "fixture", "selected", 10), {"metadata": {"purpose": "decision"}},
)
g1_case_model_call_first_token_params = _DtoCase(ledger.ModelCallFirstTokenParams, ("call",), {"output_tokens_seen": 2})
g1_case_model_call_activity_params = _DtoCase(ledger.ModelCallActivityParams, ("call",), {"output_tokens_seen": 3})
g1_case_model_call_finish_params = _DtoCase(ledger.ModelCallFinishParams, ("call",), {"output_tokens": 4})
g1_case_model_call_timeout_params = _DtoCase(
    ledger.ModelCallTimeoutParams, ("call", 10.0, "first_event"), {"timeout_wait_phase": "handshake"},
)
g1_case_model_call_failure_params = _DtoCase(
    ledger.ModelCallFailureParams, ("call", "ProviderResponseError"), {"error_code": "INVALID_RESPONSE"},
)
g1_case_model_call_provider_attempt_params = _DtoCase(
    ledger.ModelCallProviderAttemptParams, ("call", "attempt", "started"), {"method": "POST"},
)
g1_case_model_call_record = _DtoCase(
    ledger.ModelCallRecord, ("call", "fixture", "selected", 10), {"status": "finished", "output_tokens": 4},
)
g1_case_runner_model_stage_trace_request = _DtoCase(RunnerModelStageTraceRequest, (None, None, 0), {"prompt": "message"})
g1_case_runner_model_stream_activity_trace_request = _DtoCase(
    RunnerModelStreamActivityTraceRequest, (None, None, 0, "text"), {"observed_chars": 5},
)
g1_case_runner_provider_retry_trace_request = _DtoCase(
    RunnerProviderRetryTraceRequest, (None, None, 0, 1, 2, 0.25), {"error_type": "ProviderTransientError"},
)
g1_case_runner_tool_stage_trace_request = _DtoCase(RunnerToolStageTraceRequest, (None, None, 0, 1, _CALL), {"idx": 2})


def g1_case_model_call_usage_summary():
    return ledger._ModelCallTotals().to_summary


_DTO_CASES = (
    g1_case_tool_protocol_violation,
    g1_case_provider_tool_call_result,
    g1_case_stream_event,
    g1_case_stream_completion,
    g1_case_scanned_text_block,
    g1_case_text_block_scan,
    g1_case_assistant_turn,
    g1_case_compaction_summary,
    g1_case_runtime_facts_turn,
    g1_case_user_turn,
    g1_case_decision_answer,
    g1_case_decision_response,
    g1_case_prepared_decision_call,
    g1_case_reasoning_level_rule,
    g1_case_reasoning_payload_limits,
    g1_case_anthropic_budget_decision,
    g1_case_vision_capability,
    g1_case_model_call_ledger_options,
    g1_case_model_call_ledger_context,
    g1_case_model_call_started_params,
    g1_case_model_call_first_token_params,
    g1_case_model_call_activity_params,
    g1_case_model_call_finish_params,
    g1_case_model_call_timeout_params,
    g1_case_model_call_failure_params,
    g1_case_model_call_provider_attempt_params,
    g1_case_model_call_record,
    g1_case_runner_model_stage_trace_request,
    g1_case_runner_model_stream_activity_trace_request,
    g1_case_runner_provider_retry_trace_request,
    g1_case_runner_tool_stage_trace_request,
)


@pytest.mark.parametrize("case", _DTO_CASES, ids=lambda case: case.dto.__name__)
def test_model_dto_uses_fast_frozen_slots(case):
    actual = case()()
    assert "__slots__" in type(actual).__dict__
    assert type(actual) in fast_dataclass.CONVERTED_CLASSES


def _plain_dataclass(dto):
    """用同一组字段和标准库构造器复现转换前的数据类。"""
    fields = [(item.name, item.type, copy.copy(item)) for item in dataclasses.fields(dto)]
    return dataclasses.make_dataclass(dto.__name__, fields, frozen=True)


def _hash_result(value):
    try:
        return hash(value)
    except TypeError as exc:
        return type(exc), str(exc)


@pytest.mark.parametrize("case", _DTO_CASES, ids=lambda case: case.dto.__name__)
def test_model_dto_matches_plain_dataclass(case):
    dto, args, changes = case
    plain = _plain_dataclass(dto)
    signature = inspect.signature(dto)
    assert signature == inspect.signature(plain)
    actual = dto(*args)
    expected = plain(*args)
    duplicate = dto(*args)

    assert actual == duplicate
    assert dataclasses.asdict(actual) == dataclasses.asdict(expected)
    assert repr(actual) == repr(expected)
    assert _hash_result(actual) == _hash_result(expected)
    assert pickle.loads(pickle.dumps(actual)) == actual
    if dto.__module__ == ledger.__name__:
        assert weakref.ref(actual)() is actual

    supplied = signature.bind(*args).arguments
    for item in dataclasses.fields(dto):
        if item.default_factory is not dataclasses.MISSING and item.name not in supplied:
            assert getattr(actual, item.name) is not getattr(duplicate, item.name)

    first_field = dataclasses.fields(dto)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(actual, first_field, getattr(actual, first_field))
    with pytest.raises(dataclasses.FrozenInstanceError):
        delattr(actual, first_field)

    expected_update = dataclasses.replace(expected, **changes)
    actual_update = fast_dataclass.fast_replace(actual, **changes)
    assert type(actual_update) is dto
    assert actual_update != actual
    assert actual_update == dataclasses.replace(actual, **changes)
    assert repr(actual_update) == repr(expected_update)
    assert dataclasses.asdict(actual_update) == dataclasses.asdict(expected_update)
    if hasattr(copy, "replace"):
        assert copy.replace(actual, **changes) == actual_update

    with pytest.raises(TypeError) as plain_error:
        dataclasses.replace(expected, missing_dto_field=None)
    with pytest.raises(type(plain_error.value)) as fast_error:
        fast_dataclass.fast_replace(actual, missing_dto_field=None)
    assert str(fast_error.value) == str(plain_error.value)


def _descendants(cls):
    yield cls
    for descendant in cls.__subclasses__():
        yield from _descendants(descendant)


@pytest.mark.parametrize(("cls", "base"), [
    (DecisionPrivacySkip, DecisionInputError),
    (errors.ProviderModelCapabilityError, errors.ProviderConfigurationError),
    (errors.ModelNotConfiguredError, errors.ProviderConfigurationError),
    (errors.ProviderRequestRejectedError, errors.ProviderConfigurationError),
    (errors.ProviderRequestShapeInvalidError, errors.ProviderRequestRejectedError),
    (errors.ProviderTimeoutError, errors.ProviderRecoverableError),
    (errors.ProviderTransientError, errors.ProviderRecoverableError),
    (errors.ProviderQuotaExhaustedError, errors.ProviderRecoverableError),
    (errors.ProviderResponseError, errors.ProviderRecoverableError),
    (errors.ProviderContextWindowError, errors.ProviderResponseError),
    (ledger.ModelCallAdmissionClosedError, RuntimeError),
    (ledger._ModelCallAggregate, ledger._ModelCallTotals),
], ids=lambda cls: cls.__name__)
def test_explicit_base_preserves_subclass_mro(cls, base):
    for descendant in _descendants(cls):
        mro = descendant.__mro__
        assert mro[mro.index(cls) + 1] is base, descendant


@pytest.mark.parametrize(("body", "markers"), [
    ("", []),
    ("模型输出 😀", []),
    ("[TOOL_CALL]{}[/TOOL_CALL]", ["[/tool_call]", "[tool_call]"]),
    ("<Tool_use>正文</Tool_use>", ["</tool_use", "<tool_use"]),
])
def test_tool_protocol_evidence_matches_standard_json(body, markers):
    expected = {
        "chars": len(body),
        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "protocol_markers": markers,
    }
    assert _protocol_evidence(body) == json.dumps(
        expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
