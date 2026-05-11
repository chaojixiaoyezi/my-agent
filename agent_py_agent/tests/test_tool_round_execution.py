"""LLM: focused tests for single-round tool execution safeguards.

模块用途: 验证工具执行轮如何处理模型同轮依赖调用，避免子代理树拿脑补 run id 继续调度。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_round_execution import (
    ToolRoundExecutionRequest,
    execute_tool_round,
    subagent_output_json_response,
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


# LLM: output.json closeout should be accepted only when refs are traceable on disk.
# 函数用途: 模型漏写 evidence_packets 但已写 coordinator report 时，系统要补最小证据包并回写 output.json。
def test_subagent_output_json_response_derives_packet_from_report(tmp_path):
    output_json = tmp_path / "output.json"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report = reports_dir / "coordinator_report.md"
    report.write_text("完成协调报告", encoding="utf-8")
    output_json.write_text(
        json.dumps(
            {
                "status": "AWAITING_ACCEPTANCE",
                "summary": "root 已完成协调并写出报告。",
                "evidence_packets": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task = SimpleNamespace(id="root-1", output_json=str(output_json), reports_dir=str(reports_dir))
    agent = SimpleNamespace(_current_subagent_run_id="root-1", subagents=SimpleNamespace(load=lambda _: task))

    response = subagent_output_json_response(agent, ModelResponse(text="fallback", backend="test"))
    written = json.loads(output_json.read_text(encoding="utf-8"))

    assert "[SUBAGENT_RESULT]" in response.text
    assert written["evidence_packets"][0]["id"] == "evpkt-output-json-root-1"
    assert str(report) in written["evidence_packets"][0]["evidence_refs"]
    assert str(output_json) in written["evidence_packets"][0]["evidence_refs"]


# LLM: malformed packets are left strict so acceptance can reject them instead of hiding bad evidence.
# 函数用途: 如果模型写了非空但无 refs 的 evidence_packets，自动收口不应偷偷追加好证据掩盖问题。
def test_subagent_output_json_response_does_not_hide_bad_packet(tmp_path):
    output_json = tmp_path / "output.json"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "coordinator_report.md").write_text("report", encoding="utf-8")
    original = {
        "status": "AWAITING_ACCEPTANCE",
        "summary": "bad packet",
        "evidence_packets": [{"id": "bad", "claim": "done"}],
    }
    output_json.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    task = SimpleNamespace(id="root-1", output_json=str(output_json), reports_dir=str(reports_dir))
    agent = SimpleNamespace(_current_subagent_run_id="root-1", subagents=SimpleNamespace(load=lambda _: task))

    subagent_output_json_response(agent, ModelResponse(text="fallback", backend="test"))
    written = json.loads(output_json.read_text(encoding="utf-8"))

    assert written == original
