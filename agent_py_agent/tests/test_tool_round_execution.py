"""LLM: focused tests for single-round tool execution safeguards.

模块用途: 验证工具执行轮如何处理模型同轮依赖调用，避免子代理树拿脑补 run id 继续调度。
"""

from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_round_execution import (
    ToolRoundExecutionRequest,
    execute_tool_round,
)
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.tools import ToolExecutionResult


# LLM: same-turn hierarchy dispatch must wait for real schedule output before using run ids.
# 函数用途: 模型同一轮先 schedule 又 dispatch 时，只执行 schedule，并把 dispatch 延后到下一轮。
def test_tool_round_defers_dependent_dispatch_after_schedule():
    calls = [
        {"tool": "schedule_child_subagents", "children": [{"goal": "child"}]},
        {"tool": "dispatch_subagents", "run_ids": ["hallucinated-run-id"]},
    ]
    executed: list[str] = []
    records: list[tuple[str, bool, str]] = []

    def execute_one(request):
        tool_name = str(request.payload["tool"])
        executed.append(tool_name)
        return ToolExecutionResult(tool_name, True, '{"created_run_ids":["real-child-id"]}')

    def record_one(record):
        records.append((str(record.payload["tool"]), record.result.ok, record.result.output))

    completed = execute_tool_round(
        ToolRoundExecutionRequest(
            agent=SimpleNamespace(),
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="tool round", backend="test"),
            calls=calls,
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert completed is False
    assert executed == ["schedule_child_subagents"]
    assert records[0] == ("schedule_child_subagents", True, '{"created_run_ids":["real-child-id"]}')
    assert records[1][0] == "dispatch_subagents"
    assert records[1][1] is False
    assert "已延后" in records[1][2]


# LLM: test_tool_round_detects_bundled_filesystem_output_json covers real model write_file bundles.
# 函数用途: 模型用 filesystem.path 写 output.json 时，也应触发 runner 提前收口，避免再生成长结果块。
def test_tool_round_detects_bundled_filesystem_output_json(tmp_path):
    output_json = tmp_path / "output.json"
    task = SimpleNamespace(output_json=str(output_json))
    agent = SimpleNamespace(
        _current_subagent_run_id="run-1",
        subagents=SimpleNamespace(load=lambda run_id: task),
    )
    payload = {"tool": "write_file", "filesystem": {"path": str(output_json), "content": "{}"}}
    records: list[str] = []

    def execute_one(request):
        assert request.payload == payload
        return ToolExecutionResult("write_file", True, "ok")

    def record_one(record):
        records.append(record.result.tool)

    completed = execute_tool_round(
        ToolRoundExecutionRequest(
            agent=agent,
            params=SimpleNamespace(tool_context=[]),
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[payload],
            execute_one=execute_one,
            record_one=record_one,
        )
    )

    assert completed is True
    assert records == ["write_file"]
