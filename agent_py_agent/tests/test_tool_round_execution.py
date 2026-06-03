"""LLM: focused tests for single-round tool execution safeguards.

模块用途: 验证工具执行轮如何处理模型同轮依赖调用，避免子代理树拿脑补 run id 继续调度。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.subagent.progress_closeout import (
    subagent_progress_closeout_response,
)
from agent_py_agent.agent.agent_core.tool_loop.round_execution import (
    ToolRoundExecutionRequest,
    execute_tool_round,
    subagent_output_json_response,
)
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.tools import ToolExecutionResult


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


def test_tool_round_reports_subagent_output_json_scope_load_error(tmp_path):
    output_json = tmp_path / "output.json"

    def broken_load(run_id):
        del run_id
        raise OSError("canonical state missing")

    agent = SimpleNamespace(
        _current_subagent_run_id="run-1",
        subagents=SimpleNamespace(load=broken_load),
    )
    payload = {"tool": "write_file", "path": str(output_json), "content": "{}"}
    params = SimpleNamespace(tool_context=[])

    def execute_one(_request):
        return ToolExecutionResult("write_file", True, "ok")

    completed = execute_tool_round(
        ToolRoundExecutionRequest(
            agent=agent,
            params=params,
            tool_rounds=1,
            response=ModelResponse(text="", backend="test"),
            calls=[payload],
            execute_one=execute_one,
            record_one=lambda _record: None,
        )
    )

    assert completed is False
    assert any("SUBAGENT_RESULT_LOAD_ERROR" in str(item) for item in params.tool_context)
    assert any("subagent_output_json.subagents.load" in str(item) for item in params.tool_context)


def test_subagent_output_json_response_derives_packet_from_report(tmp_path):
    output_json = tmp_path / "output.json"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    report = reports_dir / "coordinator_report.md"
    report.write_text("完成协调报告", encoding="utf-8")
    output_json.write_text(
        json.dumps(
            {
                "status": "DONE",
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


def test_subagent_output_json_response_reports_task_load_error():
    def broken_load(run_id):
        del run_id
        raise OSError("subagent ledger unavailable")

    agent = SimpleNamespace(_current_subagent_run_id="root-1", subagents=SimpleNamespace(load=broken_load))

    response = subagent_output_json_response(agent, ModelResponse(text="fallback", backend="test"))

    assert "[SUBAGENT_RESULT_LOAD_ERROR]" in response.text
    assert "subagent_output_json.subagents.load" in response.text
    assert "subagent ledger unavailable" in response.text


def test_subagent_output_json_response_does_not_hide_bad_packet(tmp_path):
    output_json = tmp_path / "output.json"
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    (reports_dir / "coordinator_report.md").write_text("report", encoding="utf-8")
    original = {
        "status": "DONE",
        "summary": "bad packet",
        "evidence_packets": [{"id": "bad", "claim": "done"}],
    }
    output_json.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    task = SimpleNamespace(id="root-1", output_json=str(output_json), reports_dir=str(reports_dir))
    agent = SimpleNamespace(_current_subagent_run_id="root-1", subagents=SimpleNamespace(load=lambda _: task))

    subagent_output_json_response(agent, ModelResponse(text="fallback", backend="test"))
    written = json.loads(output_json.read_text(encoding="utf-8"))

    assert written == original


def test_subagent_progress_closeout_response_uses_latest_tool_progress(tmp_path):
    artifact = tmp_path / "lab_outputs" / "site-output" / "index.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_text("<html><body><main id='home'>done</main></body></html>", encoding="utf-8")
    workspace = tmp_path / "tasks" / "worker" / "agents" / "worker"
    progress_dir = workspace / "progress"
    progress_dir.mkdir(parents=True)
    progress_ref = progress_dir / "latest_tool_progress.json"
    progress_ref.write_text(
        json.dumps(
            {
                "schema_version": "subagent_tool_progress.v1",
                "kind": "subagent_tool_progress",
                "run_id": "worker",
                "latest_written_path": str(artifact),
                "artifact_integrity": {
                    "kind": "html",
                    "ok": True,
                    "blocker_codes": [],
                    "warning_codes": [],
                    "issues": [],
                },
                "latest_tool_progress_ref": str(progress_ref),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    task = SimpleNamespace(
        id="worker",
        agent_run_workspace_dir=str(workspace),
        output_json=str(tmp_path / ".my_agent" / "subagents" / "worker" / "output.json"),
    )
    agent = SimpleNamespace(_current_subagent_run_id="worker", subagents=SimpleNamespace(load=lambda _: task))

    response = subagent_progress_closeout_response(agent, ModelResponse(text="fallback", backend="test"))

    assert response is not None
    assert "[SUBAGENT_RESULT]" in response.text
    assert '"status": "DONE"' in response.text
    assert str(artifact) in response.text
    assert str(progress_ref) in response.text


def test_subagent_progress_closeout_reports_task_load_error():
    def broken_load(run_id):
        del run_id
        raise OSError("progress ledger unavailable")

    agent = SimpleNamespace(_current_subagent_run_id="worker", subagents=SimpleNamespace(load=broken_load))

    response = subagent_progress_closeout_response(agent, ModelResponse(text="fallback", backend="test"))

    assert response is not None
    assert "[SUBAGENT_PROGRESS_LOAD_ERROR]" in response.text
    assert "subagent_progress_closeout.subagents.load" in response.text
    assert "progress ledger unavailable" in response.text


def test_subagent_progress_closeout_reports_dirty_latest_progress(tmp_path):
    workspace = tmp_path / "tasks" / "worker" / "agents" / "worker"
    progress_dir = workspace / "progress"
    progress_dir.mkdir(parents=True)
    progress_ref = progress_dir / "latest_tool_progress.json"
    progress_ref.write_text("{bad-progress", encoding="utf-8")
    task = SimpleNamespace(
        id="worker",
        agent_run_workspace_dir=str(workspace),
        output_json=str(tmp_path / "output.json"),
    )
    agent = SimpleNamespace(_current_subagent_run_id="worker", subagents=SimpleNamespace(load=lambda _: task))

    response = subagent_progress_closeout_response(agent, ModelResponse(text="fallback", backend="test"))

    assert response is not None
    assert "[SUBAGENT_PROGRESS_LOAD_ERROR]" in response.text
    assert "subagent_progress_closeout.latest_tool_progress" in response.text
    assert str(progress_ref) in response.text
