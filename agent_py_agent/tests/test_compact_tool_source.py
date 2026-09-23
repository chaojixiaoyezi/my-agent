"""活动与混合Compact共用的工具分区只授完整四元调用覆盖权。"""
from __future__ import annotations

from copy import deepcopy

import pytest

from agent_py_agent.agent.conversation.active_turn_compact import (
    CarriedToolCompactSource,
    partition_carried_tool_records,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.memory_archive import estimate_tokens


# LLM: 同名call属于不同runner轮时必须独立；嵌套参数用于核对分区深复制，未知行不得进入覆盖refs。
# 函数用途: 构造最小真实归档形状，供纯分区测试使用。
def _record(call_id: str, *, attempt: str, turn: str, run: str = "run-1") -> dict[str, object]:
    return {
        "run_id": run, "attempt_id": attempt, "turn_id": turn, "call_id": call_id,
        "tool": "read_file", "ok": True,
        "model_parameters": {"path": f"{attempt}/{turn}.txt", "nested": {"line": 1}},
        "output_preview": "已读取材料" * 12,
    }


def test_partition_keeps_recent_exact_call_and_unknown_rows_in_original_order():
    first = _record("same-call", attempt="attempt-1", turn="turn-1")
    second = _record("same-call", attempt="attempt-2", turn="turn-2")
    third = _record("same-call", attempt="attempt-3", turn="turn-3")
    old_unknown = {"call_id": "old-call", "tool": "read_file", "model_parameters": {"path": "old.txt"}}
    middle_unknown = {"run_id": "run-1", "call_id": "partial-call", "tool": "read_file"}
    records = [old_unknown, first, middle_unknown, second, third]
    original = deepcopy(records)

    source = partition_carried_tool_records(records, recent_tail_tokens=estimate_tokens(third))

    assert source is not None
    assert source.source_records == (first, second)
    assert source.retained_records == (old_unknown, middle_unknown, third)
    assert source.source_tool_refs == (
        {"run_id": "run-1", "attempt_id": "attempt-1", "turn_id": "turn-1", "call_id": "same-call"},
        {"run_id": "run-1", "attempt_id": "attempt-2", "turn_id": "turn-2", "call_id": "same-call"},
    )
    assert source.retained_tool_refs == (
        {"run_id": "run-1", "attempt_id": "attempt-3", "turn_id": "turn-3", "call_id": "same-call"},
    )
    records[1]["model_parameters"]["nested"]["line"] = 99
    records[4]["model_parameters"]["path"] = "changed.txt"
    assert source.source_records[0]["model_parameters"]["nested"]["line"] == 1
    assert source.retained_records[-1]["model_parameters"]["path"] == original[-1]["model_parameters"]["path"]


def test_single_known_call_is_source_even_when_recent_budget_could_hold_it():
    unknown = {"call_id": "legacy", "model_parameters": {"nested": {"value": 1}}}
    known = _record("call-1", attempt="attempt-1", turn="turn-1")
    source = partition_carried_tool_records([unknown, known], recent_tail_tokens=10_000)

    assert source.source_records == (known,)
    assert source.retained_records == (unknown,)
    assert source.source_tool_refs[0]["call_id"] == "call-1"
    assert source.retained_tool_refs == ()
    assert partition_carried_tool_records([unknown], recent_tail_tokens=10_000) is None
    assert partition_carried_tool_records([], recent_tail_tokens=0) is None


def test_constructed_source_detaches_nested_records_and_refs_from_caller():
    record = _record("call-1", attempt="attempt-1", turn="turn-1")
    ref = {key: record[key] for key in ("run_id", "attempt_id", "turn_id", "call_id")}
    source = CarriedToolCompactSource((record,), (), (ref,), ())

    record["model_parameters"]["nested"]["line"] = 99
    ref["call_id"] = "other-call"
    assert source.source_records[0]["model_parameters"]["nested"]["line"] == 1
    assert source.source_tool_refs[0]["call_id"] == "call-1"


@pytest.mark.parametrize("invalid", [
    "missing_source_ref", "extra_source_ref", "cross_partition", "duplicate_source",
    "unknown_source", "empty_source",
])
def test_source_object_rejects_unread_or_ambiguous_coverage(invalid):
    first = _record("call-1", attempt="attempt-1", turn="turn-1")
    second = _record("call-2", attempt="attempt-2", turn="turn-2")
    first_ref = {key: first[key] for key in ("run_id", "attempt_id", "turn_id", "call_id")}
    second_ref = {key: second[key] for key in ("run_id", "attempt_id", "turn_id", "call_id")}
    args = {
        "source_records": (first,), "retained_records": (second,),
        "source_tool_refs": (first_ref,), "retained_tool_refs": (second_ref,),
    }
    if invalid == "missing_source_ref":
        args["source_tool_refs"] = ()
    elif invalid == "extra_source_ref":
        args["source_tool_refs"] = (first_ref, second_ref)
    elif invalid == "cross_partition":
        args["retained_records"] = (first,)
        args["retained_tool_refs"] = (first_ref,)
    elif invalid == "duplicate_source":
        args["source_records"] = (first, deepcopy(first))
    elif invalid == "unknown_source":
        args["source_records"] = ({"call_id": "legacy"},)
    else:
        args["source_records"] = ()
        args["source_tool_refs"] = ()

    with pytest.raises(ConversationCompactError) as error:
        CarriedToolCompactSource(**args)
    assert error.value.code == "COMPACT_TOOL_COVERAGE_UNKNOWN"


def test_source_keeps_duplicate_retained_records_without_granting_coverage():
    first = _record("call-1", attempt="attempt-1", turn="turn-1")
    second = _record("call-2", attempt="attempt-2", turn="turn-2")
    keys = ("run_id", "attempt_id", "turn_id", "call_id")
    source = CarriedToolCompactSource(
        (first,), (second, deepcopy(second)),
        ({key: first[key] for key in keys},), ({key: second[key] for key in keys},),
    )
    assert source.retained_records == (second, second)
    assert len(source.retained_tool_refs) == 1


def test_partition_rejects_duplicate_exact_identity_but_accepts_reused_bare_call_id():
    first = _record("same-call", attempt="attempt-1", turn="turn-1")
    second = _record("same-call", attempt="attempt-2", turn="turn-2")
    assert partition_carried_tool_records([first, second], recent_tail_tokens=10_000) is not None
    with pytest.raises(ConversationCompactError) as error:
        partition_carried_tool_records([first, deepcopy(first)], recent_tail_tokens=10_000)
    assert error.value.code == "COMPACT_TOOL_COVERAGE_UNKNOWN"


def test_zero_recent_budget_compacts_all_known_and_retains_unknown():
    known = [_record("same", attempt="a", turn=str(index)) for index in range(3)]
    unknown = {"call_id": "old", "tool": "read_file"}
    source = partition_carried_tool_records([known[0], unknown, *known[1:]], recent_tail_tokens=0)
    assert source.source_records == tuple(known)
    assert source.retained_records == (unknown,)
    assert source.retained_tool_refs == ()
