"""原生工具历史按原回合和位置回收成对记录。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_ir_compact import (
    compact_native_ir_to_token_budget,
    reclaim_oldest_native_ir_pairs,
    tool_use_ids_for_tool_records,
)
from agent_py_agent.agent.agent_core.tool_ir_history import (
    RetirementEntry,
    drop_tool_call_pairs,
    drop_tool_call_pairs_at,
    native_tool_result_sources,
    project_native_prompt_history,
    record_tool_call_ir,
    tool_pair_retirement_plan,
)
from agent_py_agent.agent.backends.message_adapter import AnthropicMessageAdapter
from agent_py_agent.agent.backends.tool_ir import (
    AssistantTurn,
    RuntimeFactsTurn,
    ToolResult,
    UserTurn,
)
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.tests._tool_runtime_harness import (
    canonical_history_call,
    canonical_history_result,
)


def _window(call_ids=("call_0", "call_0", "call_0")):
    params = SimpleNamespace(tool_ir_history=[])
    for number, call_id in enumerate(call_ids, start=1):
        call = canonical_history_call("read_file", {"path": f"item-{number}.txt"},
            call_id=call_id, turn_id=f"turn-{number}")
        record_tool_call_ir(params, tool_rounds=number, call=call,
            result=canonical_history_result(call, f"result-{number}"))
    return params


def test_ptl_reclaim_preserves_newest_reused_id():
    params = _window()
    dropped = reclaim_oldest_native_ir_pairs(params, fraction=0.4)
    remaining = [item for item in params.tool_ir_history if isinstance(item, ToolResult)]
    assert [item.output for item in remaining] == ["result-2", "result-3"]
    assert dropped == 1
    _assert_paired_replay(params.tool_ir_history, ["call_0", "call_0"])


def _assert_paired_replay(history, expected):
    remaining = []
    calls = []
    for message in AnthropicMessageAdapter().to_provider_messages(history):
        for block in message["content"]:
            if block["type"] == "tool_use":
                calls.append(block["id"])
            elif block["type"] == "tool_result":
                assert block["tool_use_id"] in calls
                calls.remove(block["tool_use_id"])
                remaining.append(block["tool_use_id"])
    assert not calls
    assert remaining == expected


def _compact_to_one(params):
    return compact_native_ir_to_token_budget(params, max_tokens=1, token_estimator=lambda: sum(
        isinstance(item, ToolResult) for item in params.tool_ir_history
    ))


def test_budget_bisection_keeps_newest_pair_and_user_input():
    params = _window()
    user = UserTurn("continue using the newest result")
    params.tool_ir_history.insert(4, user)
    assert _compact_to_one(params) == 2
    assert user in params.tool_ir_history
    assert [(source[2], result.output) for source, result in native_tool_result_sources(
        params.tool_ir_history
    )] == [("turn-3", "result-3")]
    _assert_paired_replay(params.tool_ir_history, ["call_0"])


@pytest.mark.parametrize(("ids", "legacy_count"), [
    (("old", "middle", "new"), 2),
    (("call_0", "call_0", "call_0"), 0),
    (("call_0", "unique", "call_0"), 1),
])
def test_legacy_and_position_entry_points(ids, legacy_count):
    params = _window(ids)
    history = list(params.tool_ir_history)
    plan = tool_pair_retirement_plan(history)
    assert [(entry.call_position, entry.result_position, entry.identity[2:]) for entry in plan.entries] == [
        (0, 1, ("turn-1", ids[0])), (2, 3, ("turn-2", ids[1])), (4, 5, ("turn-3", ids[2])),
    ]
    assert drop_tool_call_pairs(params, set(ids[:2])) == legacy_count
    if legacy_count == 0:
        assert params.tool_ir_history == history
    assert bool(plan.non_retirable) == (len(set(ids)) != len(ids))
    if len(set(ids)) != len(ids):
        assert ("", "", "", "call_0") in plan.non_retirable

    params.tool_ir_history[:] = history
    assert drop_tool_call_pairs_at(params, (*plan.entries[:2], plan.entries[0])) == 2
    assert drop_tool_call_pairs_at(params, plan.entries) == 0
    _assert_paired_replay(params.tool_ir_history, [ids[-1]])


def test_incomplete_and_duplicate_groups_retain_original_history():
    params = _window(("one", "two", "three"))
    history = params.tool_ir_history
    first, second, third = history[0], history[2], history[4]
    first.tool_calls.append(second.tool_calls[0])
    third.tool_calls.append(third.tool_calls[0])
    plan = tool_pair_retirement_plan(history)
    assert not plan.entries
    assert {source[-1] for source in plan.non_retirable} == {"one", "two", "three"}
    before = list(history)
    assert drop_tool_call_pairs(params, {"one", "two", "three"}) == 0
    assert drop_tool_call_pairs_at(params, [RetirementEntry(0, 1,
        ("history-run", "history-attempt", "turn-1", "one"))]) == 0
    assert history == before
    with pytest.raises(ConversationCompactError) as exc:
        native_tool_result_sources(history)
    assert exc.value.code == "COMPACT_TOOL_COVERAGE_UNKNOWN"

    orphans = SimpleNamespace(tool_ir_history=[history[1]])
    assert tool_pair_retirement_plan(orphans.tool_ir_history).non_retirable == (("", "", "", "one"),)
    assert compact_native_ir_to_token_budget(
        orphans, max_tokens=1, token_estimator=lambda: 100, preserve_newest_pair=False
    ) == 0
    assert orphans.tool_ir_history == [history[1]]


@pytest.mark.parametrize("group_type", [list, tuple])
def test_grouped_results_and_runtime_facts_retire_only_when_covered(group_type):
    first = canonical_history_call("read_file", {}, call_id="first")
    second = canonical_history_call("read_file", {}, call_id="second")
    old = RuntimeFactsTurn("old governing state", source="model")
    latest = RuntimeFactsTurn("new governing state", source="model")
    results = group_type([canonical_history_result(first, "A"), canonical_history_result(second, "B")])
    params = SimpleNamespace(tool_ir_history=[old, AssistantTurn(tool_calls=[first, second]), results, latest])
    plan = tool_pair_retirement_plan(params.tool_ir_history)
    assert [entry.result_position for entry in plan.entries] == [2, 2]
    assert drop_tool_call_pairs_at(params, plan.entries[:1], drop_completed_tool_turns=True) == 1
    assert old in params.tool_ir_history
    assert type(params.tool_ir_history[2]) is group_type
    _assert_paired_replay(params.tool_ir_history, ["second"])
    assert drop_tool_call_pairs(params, {"second"}, drop_completed_tool_turns=True) == 1
    assert params.tool_ir_history == [latest]


def test_tool_record_marker_refuses_reused_bare_id_and_unproven_positions():
    history = _window(("call_0", "unique", "call_0")).tool_ir_history
    assert tool_use_ids_for_tool_records(history, [
        "[tool-record round=1 index=1]", "[tool-record round=2 index=1]",
        "[tool-record round=3 index=1]", "[tool-record round=2 index=8]",
    ]) == {"unique"}


def test_result_projection_uses_position_even_when_original_result_object_is_shared():
    params = _window()
    history = params.tool_ir_history
    history[3] = history[1]
    first, second, _ = tool_pair_retirement_plan(history).entries
    stub = canonical_history_result(history[second.call_position].tool_calls[0], "stub for turn 2")
    params.compact_degradation_stubs = {second.identity: stub}
    _, projected = project_native_prompt_history(params, "prompt")
    assert projected[first.result_position] is history[first.result_position]
    assert projected[second.result_position] is stub
    assert history[1] is history[3]


def test_c128_retirement():
    workers = 128
    ready = Barrier(workers)

    def retire(index):
        params = _window()
        ready.wait(timeout=30)
        if index % 2:
            assert reclaim_oldest_native_ir_pairs(params, fraction=0.4) == 1
            _assert_paired_replay(params.tool_ir_history, ["call_0", "call_0"])
        else:
            assert _compact_to_one(params) == 2
            _assert_paired_replay(params.tool_ir_history, ["call_0"])
        assert native_tool_result_sources(params.tool_ir_history)[-1][1].output == "result-3"

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(retire, range(workers)))
