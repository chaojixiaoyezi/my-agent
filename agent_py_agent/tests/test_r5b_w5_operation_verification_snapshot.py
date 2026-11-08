"""R5B-W5：一次操作核验投影只取一份工具运行时快照，效果与动作投影不变。"""
from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.tooling.operation_verification import (
    build_operation_verification,
    render_current_turn_execution_facts,
)
from agent_py_agent.tests._tool_runtime_harness import runtime_snapshot_for_tools
from agent_py_agent.tests.test_current_turn_execution import _ProjectionTool


def _counted_agent():
    snapshot = runtime_snapshot_for_tools({
        "remember": _ProjectionTool(name="remember", mutating=True),
        "read_file": _ProjectionTool(name="read_file", mutating=False),
        "write_file": _ProjectionTool(name="write_file", mutating=True, mutates_workspace=True),
    })
    calls = []

    def runtime_snapshot():
        calls.append(1)
        return snapshot

    return SimpleNamespace(tools=SimpleNamespace(runtime_snapshot=runtime_snapshot)), calls


def _records(count):
    tools = ("remember", "read_file", "write_file")
    return [{"tool": tools[i % 3], "call_id": f"call-{i}", "turn_id": "turn-1", "tool_round": 1,
             "ok": True, "status": "ok", "handler_executed": True, "operation_id": f"operation-{i}",
             "tool_operation_status": "succeeded",
             "parameters": {"action": "list"} if tools[i % 3] == "remember" else {"path": f"f{i}"}}
            for i in range(count)]


def test_final_operation_verification_takes_one_snapshot_for_many_records():
    agent, calls = _counted_agent()
    result = build_operation_verification(agent, _records(24))
    assert len(calls) == 1
    # 只有 write_file 是变更类；remember(action=list) 与 read_file 是只读，效果分类必须与逐条快照时一致。
    assert result["operation_count"] == 8 and result["counts"]["succeeded"] == 8
    assert {item["tool"] for item in result["operations"]} == {"write_file"}


def test_prompt_execution_facts_take_one_snapshot_and_keep_effect_and_action():
    agent, calls = _counted_agent()
    rendered = render_current_turn_execution_facts(agent, _records(6))
    assert len(calls) == 1
    payload = json.loads(rendered.split("```json\n", 1)[1].split("\n```", 1)[0])
    effects = {call["call_id"]: (call["effect"], call.get("action", "")) for call in payload["calls"]}
    assert effects["call-0"] == ("read_only", "list")
    assert effects["call-1"] == ("read_only", "")
    assert effects["call-2"][0] != "unknown" and effects["call-2"][1] == ""


def test_missing_registry_still_projects_unknown_without_raising():
    payload = build_operation_verification(SimpleNamespace(), _records(3))
    assert payload["operation_count"] == 0
    rendered = render_current_turn_execution_facts(SimpleNamespace(), _records(3))
    body = json.loads(rendered.split("```json\n", 1)[1].split("\n```", 1)[0])
    assert {call["effect"] for call in body["calls"]} == {"unknown"}
