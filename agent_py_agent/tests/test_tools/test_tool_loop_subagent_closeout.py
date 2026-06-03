"""Tool-loop tests for subagent dispatch handoff and prompt/tool allowlists."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core.runner.context import (
    restore_current_subagent_context,
    set_current_subagent_context,
)
from agent_py_agent.agent.agent_core.tool_loop.round_subagent_output import (
    subagent_output_json_response,
)
from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence

from .backends import DispatchCompletionBackend, OutputJsonCompletionBackend


class _DispatchThenQualityBackend:
    name = "fake_dispatch_then_quality_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"dispatch_subagents","dry_run":false}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(
            text="继续创建 tester 和 bug_finder 子代理，不能只因 worker 验收通过就收口。",
            backend=self.name,
        )


def test_subagent_runner_stops_after_output_json_write():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        task = agent.subagents.create_run(
            goal="写出 output.json 后收口",
            thought="模拟真实 runner 完成产物后等待最终收口。",
            plan=["写结果", "停止工具循环"],
            allowed_tools=["write_file"],
        )
        agent.backend = OutputJsonCompletionBackend(Path(task.output_json))

        result = agent.run_subagent(task.id, dry_run=False, probe=False)

        assert agent.backend.calls == 1
        assert result.status == "DONE"
        assert result.verification_status == "VERIFIED"
        assert result.structured_output_found is True
        assert result.structured_output_ok is True
        assert result.tool_rounds == 1


def test_subagent_output_json_closeout_reports_dirty_output_json():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        task = agent.subagents.create_run(
            goal="写出 output.json 后收口",
            thought="坏 output.json 不能被当成空结果。",
            plan=["写结果", "停止工具循环"],
            allowed_tools=["write_file"],
        )
        Path(task.output_json).write_text("{bad-output", encoding="utf-8")
        previous = set_current_subagent_context(agent, run_id=task.id)
        try:
            response = subagent_output_json_response(agent, ModelResponse(text="fallback", backend="fake"))
        finally:
            restore_current_subagent_context(agent, previous)

        assert "[SUBAGENT_RESULT_LOAD_ERROR]" in response.text
        assert "subagent_output_json.output_json" in response.text
        assert task.output_json in response.text
        assert "fallback" not in response.text


def test_completed_dispatch_returns_to_parent_synthesis_turn():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        _done_verified_task(agent)
        agent.backend = DispatchCompletionBackend()

        result = agent.run("推进并汇报已完成的子代理", save=False)

        assert agent.backend.calls == 2
        assert result.tool_rounds == 1
        assert "未再发起额外模型请求" not in result.response
        assert "我已经综合子代理结果" in result.response
        assert "deliverables/report.md" in result.response


def test_completed_dispatch_does_not_local_close_when_prompt_requires_more_work():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        _done_verified_task(agent, required_qa_roles=["tester", "bug_finder"])
        agent.backend = _DispatchThenQualityBackend()

        result = agent.run("worker 完成后必须继续创建 tester 和 bug_finder 做测试验收", save=False)

        assert agent.backend.calls == 2
        assert "tester" in result.response
        assert "bug_finder" in result.response
        assert "未再发起额外模型请求" not in result.response


def test_generic_acceptance_result_wording_does_not_require_bug_finder_role():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        _done_verified_task(agent)
        agent.backend = DispatchCompletionBackend()

        result = agent.run("请你作为主代理来安排和验收，完成后只汇报产物路径和验收结果。", save=False)

        assert agent.backend.calls == 2
        assert "未再发起额外模型请求" not in result.response
        assert "我已经综合子代理结果" in result.response
        assert "missing_quality_roles" not in result.response
        assert "结论修正" not in result.response
        assert "deliverables/report.md" in result.response


def test_tool_allowlist_limits_prompt_and_execution():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        result = agent.run("读取 notes.txt", save=False, allowed_tools=["read_file"])
        blocked = agent.tools.execute_call(
            {"tool": "write_file", "path": "x.txt", "content": "x"},
            allowed_tools=["read_file"],
        )

        assert "read_file [filesystem" in result.prompt
        assert "write_file [filesystem" not in result.prompt
        assert not blocked.ok
        assert "TOOL_NOT_ALLOWED" in blocked.output


def _done_verified_task(agent, *, required_qa_roles: list[str] | None = None):
    task = agent.subagents.create_run(
        goal="已完成任务 fixture",
        thought="用于测试 dispatch 后回到主代理汇总。",
        plan=["完成", "验收"],
        allowed_tools=[],
    )
    task.status = "DONE"
    task.verification_status = "VERIFIED"
    if required_qa_roles:
        task.attributes = {"required_qa_roles": required_qa_roles}
    task.evidence.append(VerificationEvidence(
        kind="note",
        summary="任务已有验收证据。",
        ok=True,
    ))
    task.artifact_refs = ["deliverables/report.md"]
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-dispatch-handoff",
        claim="任务已完成并可追踪。",
        checked_scope="dispatch handoff fixture",
        evidence_refs=[task.output_json],
        artifact_refs=[task.output_json],
        confidence=0.9,
    ))
    Path(task.output_json).write_text(
        json.dumps({
            "status": "DONE",
            "summary": "fixture done",
            "evidence_packets": [{
                "id": "evpkt-dispatch-handoff",
                "claim": "任务已完成并可追踪。",
                "checked_scope": "dispatch handoff fixture",
                "evidence_refs": [task.output_json],
                "artifact_refs": [task.output_json],
                "confidence": 0.9,
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    agent.subagents.save(task)
    return task
