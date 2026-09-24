# LLM: 验证连续段判定的查询短路和动态边界，不替代执行轮的审批、取消及 provider 顺序记账回归。
# 模块用途: 用可观察查询证明屏障、冲突、批上限和 Compact 状态变化能在启动工具前截住下一段。
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop import segment_planning
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    _effective_parallel_batch_limit,
)
from agent_py_agent.agent.agent_core.tool_loop.segment_planning import (
    parallel_segment_end,
    parse_optional_batch_limit,
    resolve_parallel_batch_limit,
)
from agent_py_agent.agent.tooling.concurrency import ToolConcurrencyDescriptor
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall


# LLM: 测试只创建已接纳的 canonical 调用身份，不借工具名决定并发资格。
# 函数用途: 为段扫描提供有序调用，资格由各测试的显式调度描述决定。
def _calls(*names: str) -> list[ToolCall]:
    return [
        ToolCall(
            call_id=f"segment-call-{index}", tool_name=name, arguments={},
            source_protocol="native", schema_hash="sha256:segment-test",
            run_id="segment-run", turn_id="segment-turn", attempt_id="segment-attempt",
        )
        for index, name in enumerate(names)
    ]


@pytest.mark.parametrize("barrier", [
    ToolConcurrencyDescriptor("serial", "read_only"),
    ToolConcurrencyDescriptor("parallel_safe", "mutating"),
    ToolConcurrencyDescriptor("parallel_safe", "read_only", barrier_reason="approval_barrier"),
])
def test_policy_barrier_stops_before_querying_the_tail(barrier):
    trace = []

    def defer(name):
        trace.append(("compact", name))
        assert name not in {"already_handled", "tail"}
        return False

    def describe(call):
        trace.append(("describe", call.tool_name))
        return barrier if call.tool_name == "barrier" else ToolConcurrencyDescriptor("parallel_safe", "read_only")

    end = parallel_segment_end(
        _calls("already_handled", "reader", "barrier", "tail"), 1,
        batch_limit=0, defer_for_compact=defer, describe_call=describe,
    )
    assert end == 2
    assert trace == [
        ("compact", "reader"), ("describe", "reader"),
        ("compact", "barrier"), ("describe", "barrier"),
    ]


def test_conflict_leaves_the_call_outside_the_segment_and_does_not_query_the_tail(monkeypatch):
    trace = []

    def describe(call):
        trace.append(("describe", call.tool_name))
        assert call.tool_name != "tail"
        return ToolConcurrencyDescriptor("parallel_safe", "read_only", (call.tool_name,))

    # 当前 read/read 不冲突；在已有冲突谓词处注入冲突事实，验证选段不能忽略它或扫描后续调用。
    def conflict(left, right):
        trace.append(("conflict", left.resource_scopes, right.resource_scopes))
        return left.resource_scopes == ("conflicting",)

    monkeypatch.setattr(segment_planning, "concurrency_conflicts", conflict)
    end = parallel_segment_end(
        _calls("reader", "conflicting", "tail"), 0, batch_limit=0,
        defer_for_compact=lambda name: trace.append(("compact", name)) or False,
        describe_call=describe,
    )
    assert end == 1
    assert trace == [
        ("compact", "reader"), ("describe", "reader"),
        ("compact", "conflicting"), ("describe", "conflicting"),
        ("conflict", ("conflicting",), ("reader",)),
    ]


def test_compact_is_rechecked_after_a_prior_descriptor_changes_live_state():
    compact_due = False
    trace = []

    def defer(name):
        trace.append(("compact", name, compact_due))
        return compact_due

    def describe(call):
        nonlocal compact_due
        trace.append(("describe", call.tool_name))
        compact_due = True
        return ToolConcurrencyDescriptor("parallel_safe", "read_only")

    end = parallel_segment_end(
        _calls("first", "second", "tail"), 0, batch_limit=0,
        defer_for_compact=defer, describe_call=describe,
    )
    assert end == 1
    assert trace == [("compact", "first", False), ("describe", "first"), ("compact", "second", True)]


def test_batch_limit_stops_before_any_query_of_the_next_call():
    seen = []

    def describe(call):
        seen.append(call.tool_name)
        assert call.tool_name == "first"
        return ToolConcurrencyDescriptor("parallel_safe", "read_only")

    end = parallel_segment_end(
        _calls("first", "tail"), 0, batch_limit=1,
        defer_for_compact=lambda name: seen.append(f"compact:{name}") or False,
        describe_call=describe,
    )
    assert end == 1
    assert seen == ["compact:first", "first"]


@pytest.mark.parametrize("failure_site", ["compact", "descriptor", "conflict"])
def test_query_exception_keeps_its_identity_and_prevents_later_queries(monkeypatch, failure_site):
    failure = RuntimeError("query failed")
    trace = []

    def defer(name):
        trace.append(("compact", name))
        if failure_site == "compact":
            raise failure
        return False

    def describe(call):
        trace.append(("describe", call.tool_name))
        if failure_site == "descriptor":
            raise failure
        return ToolConcurrencyDescriptor("parallel_safe", "read_only")

    def conflict(_left, _right):
        trace.append(("conflict", "second"))
        raise failure

    monkeypatch.setattr(segment_planning, "concurrency_conflicts", conflict)
    with pytest.raises(RuntimeError) as caught:
        parallel_segment_end(
            _calls("first", "second", "tail"), 0, batch_limit=0,
            defer_for_compact=defer, describe_call=describe,
        )
    assert caught.value is failure
    expected = [("compact", "first")]
    if failure_site != "compact":
        expected.append(("describe", "first"))
    if failure_site == "conflict":
        expected.extend([("compact", "second"), ("describe", "second"), ("conflict", "second")])
    assert trace == expected


@pytest.mark.parametrize(("parallel", "batch", "expected"), [
    (None, None, 8), (-1, 0, 8), (0, 0, 0), (0, 3, 3),
    (8, 2, 2), (2, 8, 2), (3, -1, 3),
])
def test_batch_limits_keep_zero_unlimited_and_distinct_negative_defaults(parallel, batch, expected):
    assert resolve_parallel_batch_limit(parallel, batch) == expected


def test_task_overrides_do_not_read_unused_configuration():
    class UnreadableConfig:
        def __getattr__(self, key):
            raise AssertionError(f"unused config was read: {key}")

    request = SimpleNamespace(
        agent=SimpleNamespace(config=UnreadableConfig()),
        params=SimpleNamespace(task_attributes={"max_parallel_tool_calls": 0, "max_tool_calls_per_round": 2}),
    )
    assert _effective_parallel_batch_limit(request) == 2


def test_invalid_task_values_fall_back_without_turning_boolean_into_a_limit():
    request = SimpleNamespace(
        agent=SimpleNamespace(config=SimpleNamespace(max_parallel_tool_calls=3, max_tool_calls_per_round=8)),
        params=SimpleNamespace(task_attributes={"max_parallel_tool_calls": True, "max_tool_calls_per_round": "2"}),
    )
    assert _effective_parallel_batch_limit(request) == 2
    with pytest.raises(OverflowError):
        parse_optional_batch_limit(float("inf"))
