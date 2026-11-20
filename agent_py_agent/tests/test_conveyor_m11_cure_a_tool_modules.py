from __future__ import annotations

import copy
import dataclasses
import inspect
import pickle
import sys
from dataclasses import FrozenInstanceError
from types import ModuleType, SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_call_runtime import ToolCallRuntimeRequest
from agent_py_agent.agent.agent_core.tool_context.call_reducer import (
    AssistantToolRoundContextRequest,
)
from agent_py_agent.agent.agent_core.tool_guard.identical_failure import (
    IdenticalFailureStreak,
    next_identical_failure_streak,
)
from agent_py_agent.agent.agent_core.tool_ir_compact import NativeCompactCandidate
from agent_py_agent.agent.agent_core.tool_ir_history import (
    _ensure_assistant_turn,
    _freeze_projection_value,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import ToolCallExecuteParams
from agent_py_agent.agent.agent_core.tool_request_projection import (
    InputModalityDecision,
    InputModalityFilter,
    ToolLoopRequestInput,
    ToolLoopRequestProjection,
)
from agent_py_agent.agent.agent_core.tool_stream.boundary import MalformedToolProtocolStreamAbort
from agent_py_agent.agent.agent_core.tool_stream.write_abort import (
    LongToolContentAbortPayload,
    LongToolContentStreamAbort,
)
from agent_py_agent.agent.common import fast_dataclass
from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall

_CALL = ToolCall("call-1", "read_file", {"path": "notes.txt"}, "native", "sha256:schema", "run-1", "turn-1", "attempt-1")
_EXECUTE_REQUEST = ToolCallExecuteParams(SimpleNamespace(), tool_rounds=1, idx=0, call=_CALL)


def g1_case_tool_call_runtime_request():
    return lambda: ToolCallRuntimeRequest(None, _EXECUTE_REQUEST, _CALL)


def g1_case_native_compact_candidate():
    return lambda: NativeCompactCandidate(dropped_pairs=2, after_tokens=800)


def g1_case_tool_loop_request_input():
    return lambda: ToolLoopRequestInput(tool_context=("tool result",))


def g1_case_tool_loop_request_projection():
    return lambda: ToolLoopRequestProjection(status="ready", prompt="continue")


def g1_case_input_modality_decision():
    return lambda: InputModalityDecision("applicable", "matched", required_modalities=("text",))


def g1_case_input_modality_filter():
    return lambda: InputModalityFilter("ready", "matched", applicable_profile_ids=("profile-1",))


def g1_case_assistant_tool_round_context_request():
    return lambda: AssistantToolRoundContextRequest("checking files", [])


def g1_case_identical_failure_streak():
    previous = IdenticalFailureStreak("read_file", "args", "IO_ERROR")
    return lambda: next_identical_failure_streak(previous, "read_file", "args", "IO_ERROR")


def g1_case_long_tool_content_abort_payload():
    return lambda: LongToolContentAbortPayload("write_file", "notes.txt", chars=12, limit=10)


def _plain_request_post_init(self):
    for name in ("native_tools", "tool_ir_history", "provider_history_messages", "tool_context", "compact_degradation_stubs"):
        value = getattr(self, name)
        if value is not None:
            object.__setattr__(self, name, tuple(copy.deepcopy(value)))
    if self.forwarded_guidance is not None:
        object.__setattr__(self, "forwarded_guidance", frozenset(self.forwarded_guidance))


def _plain_dataclass(cls, monkeypatch):
    # 以标准库生成普通对照类。独立模块使两个同名类都能按真实模块名完成 pickle 往返。
    module = ModuleType("_m11_plain_dataclass")
    monkeypatch.setitem(sys.modules, module.__name__, module)
    namespace = {"__module__": module.__name__}
    if cls is ToolLoopRequestInput:
        namespace["__post_init__"] = _plain_request_post_init
    plain = dataclasses.make_dataclass(
        cls.__name__,
        [(field.name, field.type, copy.copy(field)) for field in dataclasses.fields(cls)],
        namespace=namespace,
        frozen=True,
        slots=True,
    )
    plain.__module__ = module.__name__
    setattr(module, cls.__name__, plain)
    return plain


@pytest.mark.parametrize(
    ("cls", "arguments", "changes"),
    [
        (ToolCallRuntimeRequest, {"agent": None, "request": _EXECUTE_REQUEST, "call": _CALL},
         {"call": dataclasses.replace(_CALL, call_id="call-2")}),
        (NativeCompactCandidate, {"dropped_pairs": 1, "after_tokens": 800}, {"after_tokens": 640}),
        (ToolLoopRequestInput, {}, {"native_tools": [{"name": "read_file"}], "forwarded_guidance": ["hint"]}),
        (ToolLoopRequestProjection, {"status": "ready"}, {"messages": [{"role": "user", "content": "消息"}]}),
        (InputModalityDecision, {"status": "applicable", "reason_code": "matched"}, {"declared_modalities": ("text",)}),
        (InputModalityFilter, {"status": "ready", "reason_code": "matched"}, {"applicable_profile_ids": ("profile-1",)}),
        (AssistantToolRoundContextRequest, {"response_text": "检查文件", "tool_calls": []}, {"max_inline_chars": 500}),
        (IdenticalFailureStreak, {"tool_name": "read_file", "args_hash": "args", "error_code": "IO_ERROR"}, {"count": 2}),
        (LongToolContentAbortPayload, {"tool": "write_file", "path": "notes.txt", "chars": 12, "limit": 10}, {"path": ""}),
    ],
    ids=[
        "ToolCallRuntimeRequest", "NativeCompactCandidate", "ToolLoopRequestInput", "ToolLoopRequestProjection",
        "InputModalityDecision", "InputModalityFilter", "AssistantToolRoundContextRequest", "IdenticalFailureStreak",
        "LongToolContentAbortPayload",
    ],
)
def test_frozen_dto_matches_plain_dataclass(cls, arguments, changes, monkeypatch):
    assert cls in fast_dataclass.CONVERTED_CLASSES
    assert "__slots__" in cls.__dict__
    plain = _plain_dataclass(cls, monkeypatch)
    assert inspect.signature(cls) == inspect.signature(plain)

    dto = cls(**arguments)
    reference = plain(**arguments)
    assert dto == cls(**arguments)
    assert dto != cls(**{**arguments, **changes})
    restored = pickle.loads(pickle.dumps(dto))
    assert type(restored) is cls
    assert restored == dto
    assert pickle.loads(pickle.dumps(reference)) == reference
    assert dataclasses.asdict(dto) == dataclasses.asdict(reference)
    assert repr(dto) == repr(reference)
    try:
        expected_hash = hash(reference)
    except TypeError as expected:
        with pytest.raises(TypeError) as actual:
            hash(dto)
        assert str(actual.value) == str(expected)
    else:
        assert hash(dto) == expected_hash

    first_field = dataclasses.fields(cls)[0].name
    for edit, args in ((setattr, ("new",)), (delattr, ())):
        with pytest.raises(FrozenInstanceError) as expected:
            edit(reference, first_field, *args)
        with pytest.raises(FrozenInstanceError) as actual:
            edit(dto, first_field, *args)
        assert str(actual.value) == str(expected.value)

    replaced = fast_replace(dto, **changes)
    assert type(replaced) is cls
    assert dataclasses.asdict(replaced) == dataclasses.asdict(dataclasses.replace(reference, **changes))
    assert dataclasses.replace(dto, **changes) == replaced
    if hasattr(copy, "replace"):
        assert copy.replace(dto, **changes) == replaced

    with pytest.raises(TypeError) as expected:
        dataclasses.replace(reference, unknown_field=True)
    with pytest.raises(TypeError) as actual:
        fast_replace(dto, unknown_field=True)
    assert str(actual.value) == str(expected.value)


def test_request_input_keeps_snapshot_copy_and_missing_values():
    tools = [{"name": "read_file", "schema": {"properties": {}}}]
    content = [{"role": "user", "content": ["message"]}]
    stubs = [(("run-1", "turn-1", "attempt-1", "call-1"), {"text": ["summary"]})]
    dto = ToolLoopRequestInput(
        native_tools=tools,
        tool_ir_history=content,
        provider_history_messages=content,
        tool_context=[],
        forwarded_guidance={"hint"},
        compact_degradation_stubs=stubs,
    )
    tools[0]["schema"]["properties"]["new"] = True
    content[0]["content"].append("updated")
    stubs[0][1]["text"].append("updated")
    assert dto.native_tools == ({"name": "read_file", "schema": {"properties": {}}},)
    assert dto.tool_ir_history == dto.provider_history_messages == ({"role": "user", "content": ["message"]},)
    assert dto.tool_context == ()
    assert dto.forwarded_guidance == frozenset({"hint"})
    assert dto.compact_degradation_stubs == ((("run-1", "turn-1", "attempt-1", "call-1"), {"text": ["summary"]}),)
    assert ToolLoopRequestInput().tool_context is None

    text_context = ("tool result",)
    assert ToolLoopRequestInput(tool_context=text_context).tool_context is text_context
    mutable_context = ({"text": ["tool result"]},)
    frozen_context = ToolLoopRequestInput(tool_context=mutable_context)
    mutable_context[0]["text"].append("updated")
    assert frozen_context.tool_context == ({"text": ["tool result"]},)
    assert InputModalityFilter("ready", "matched").inapplicable is not InputModalityFilter("ready", "matched").inapplicable


def test_streak_increment_preserves_subclass_fields():
    @dataclasses.dataclass(frozen=True, slots=True)
    class RecordedStreak(IdenticalFailureStreak):
        source: str = ""

    plain = IdenticalFailureStreak("read_file", "args", "IO_ERROR")
    recorded = RecordedStreak("read_file", "args", "IO_ERROR", source="tool result")
    for previous in (plain, recorded):
        incremented = next_identical_failure_streak(previous, "read_file", "args", "IO_ERROR")
        assert type(incremented) is type(previous)
        assert incremented == dataclasses.replace(previous, count=2)
        assert previous.count == 1


def test_history_projection_supports_extension_dataclass_with_unhashable_type():
    class ExtensionType(type):
        __hash__ = None

    @dataclasses.dataclass(frozen=True)
    class ExtensionItem(metaclass=ExtensionType):
        text: str

    before = _freeze_projection_value(ExtensionItem("message"))
    assert before is not None
    assert _freeze_projection_value(ExtensionItem("message")) == before
    assert _freeze_projection_value(ExtensionItem("updated")) != before


def test_assistant_turn_reuses_marker_and_keeps_first_nonempty_fields():
    history = []
    turn = _ensure_assistant_turn(history, 1, "")
    blocks = [{"type": "text", "text": "message"}]
    assert _ensure_assistant_turn(history, 1, "message", blocks) is turn
    blocks[0]["text"] = "updated"
    assert _ensure_assistant_turn(history, 1, "later", []) is turn
    assert turn.text == "message"
    assert turn.content_blocks == [{"type": "text", "text": "message"}]
    for copied in (copy.deepcopy(history), pickle.loads(pickle.dumps(history))):
        assert _ensure_assistant_turn(copied, 1, "later") is copied[0]
    assert _ensure_assistant_turn(history, 2, "") is not turn
    assert len(history) == 2


@pytest.mark.parametrize(
    ("error_type", "args", "kwargs", "message"),
    [
        (MalformedToolProtocolStreamAbort, (), {"start_marker": "[TOOL_CALL]", "marker_count": 2, "limit": 1},
         "tool protocol emitted 2 unclosed [TOOL_CALL] markers"),
        (LongToolContentStreamAbort, (LongToolContentAbortPayload("write_file", "notes.txt", 12, 10),), {},
         "write_file.content inline content streaming exceeded 10 chars for notes.txt"),
    ],
)
def test_stream_abort_preserves_runtime_error_mro_and_message(error_type, args, kwargs, message):
    remaining = [error_type]
    while remaining:
        cls = remaining.pop()
        mro = cls.__mro__
        assert mro[mro.index(error_type) + 1] is RuntimeError
        remaining.extend(cls.__subclasses__())
    assert error_type(*args, **kwargs).args == (message,)
