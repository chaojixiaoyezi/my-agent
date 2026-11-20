"""LLM: 交付清单收口门的合同单测。

主回合和子任务均检查宿主的明确文件声明；修复次数有界，超限按未交付收口。
本文件同时覆盖"门本身"与"收口路径真的调用了它"两层，避免出现"写了门但没人用"。
"""

from __future__ import annotations

import types

from agent_py_agent.agent.agent_core.tool_loop.deliverable_closeout import (
    declared_deliverables,
    deliverable_closeout_block,
    missing_deliverables,
)


def _params(tmp_path, *, scope="task_local", declared=(), state=None):
    attrs: dict[str, object] = {}
    if declared:
        attrs["required_file_refs"] = list(declared)
    return types.SimpleNamespace(
        context_scope=scope,
        task_attributes=attrs,
        live_archive_state=dict(state or {}),
    )


def test_child_with_missing_deliverable_is_blocked(tmp_path):
    target = tmp_path / "core.go"
    params = _params(tmp_path, declared=[str(target)])
    declared, missing = missing_deliverables(params)
    assert declared == [str(target)]
    assert missing == [str(target)]
    block = deliverable_closeout_block(params)
    assert block and str(target) in block
    assert params.live_archive_state["deliverable_closeout_repairs"] == 1


def test_block_is_bounded(tmp_path):
    target = tmp_path / "core.go"
    params = _params(tmp_path, declared=[str(target)])
    assert deliverable_closeout_block(params)
    assert deliverable_closeout_block(params)
    assert deliverable_closeout_block(params) is None
    assert params.live_archive_state["deliverable_closeout_repairs"] == 2


def test_existing_deliverable_passes(tmp_path):
    target = tmp_path / "core.go"
    target.write_text("package x", encoding="utf-8")
    params = _params(tmp_path, declared=[str(target)])
    assert deliverable_closeout_block(params) is None


def test_main_conversation_only_checks_required_files(tmp_path):
    target = tmp_path / "core.go"
    params = _params(tmp_path, scope="default")
    params.task_attributes["output_refs"] = [str(target)]
    assert deliverable_closeout_block(params) is None
    params.task_attributes["required_file_refs"] = [str(target)]
    assert str(target) in deliverable_closeout_block(params)


def test_no_declaration_no_block(tmp_path):
    params = _params(tmp_path)
    assert declared_deliverables(params) == []
    assert deliverable_closeout_block(params) is None


def test_relative_and_artifact_refs_ignored(tmp_path):
    params = _params(tmp_path)
    params.task_attributes.update({"output_files": ["core.go"], "artifact_refs": [str(tmp_path / "final_report.md")]})
    assert declared_deliverables(params) == []
    assert deliverable_closeout_block(params) is None


# LLM: 光有纯函数正确还不够——必须证明收口路径真的消费了它；否则门写了但没人调用，
#   真机上仍然会"缺产物也报完成"。
# 函数用途: 用最小请求对象验证 _no_tool_calls_decision 在有缺产物时返回 continue 而不是 break。
def test_no_tool_calls_decision_continues_when_deliverable_missing(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        _no_tool_calls_decision,
        _NoToolCallsRequest,
    )

    target = tmp_path / "core.go"
    params = _params(tmp_path, declared=[str(target)])
    params.tool_context = []
    params.executed_tools = []
    response = types.SimpleNamespace(
        text="已经完成，产物很快写出。",
        truncated=False,
        runtime_status="ok",
        runtime_reason="",
        runtime_source="",
    )
    request = _NoToolCallsRequest(
        agent=types.SimpleNamespace(config=types.SimpleNamespace(enable_tools=True)),
        params=params,
        response=response,
        counters=ToolLoopRepairCounters(),
        has_protected_marker=False,
    )

    decision = _no_tool_calls_decision(request)
    assert decision.action == "continue"
    assert any("deliverable-closeout-blocked" in str(item) for item in params.tool_context)


# 函数用途: 验证没有声明交付物时收口路径照常 break。
def test_no_tool_calls_decision_breaks_without_declaration(tmp_path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        _no_tool_calls_decision,
        _NoToolCallsRequest,
    )

    params = _params(tmp_path, scope="default")
    params.tool_context = []
    params.executed_tools = []
    response = types.SimpleNamespace(
        text="已经完成。",
        truncated=False,
        runtime_status="ok",
        runtime_reason="",
        runtime_source="",
    )
    decision = _no_tool_calls_decision(
        _NoToolCallsRequest(
            agent=types.SimpleNamespace(config=types.SimpleNamespace(enable_tools=True)),
            params=params,
            response=response,
            counters=ToolLoopRepairCounters(),
            has_protected_marker=False,
        )
    )
    assert decision.action == "break"
    assert params.tool_context == []
