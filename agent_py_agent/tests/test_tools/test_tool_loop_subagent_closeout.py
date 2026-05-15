"""Focused tool-loop tests for subagent closeout and prompt/tool allowlists."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.backend import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence

from .backends import DispatchCompletionBackend, OutputJsonCompletionBackend


# LLM: _DispatchThenQualityBackend proves explicit QA role requirements must return control to the model.
# 函数用途: 第一次要求 dispatch，第二次给最终文本；若系统本地提前收口就不会发生第二次调用。
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
                    '{"tool":"dispatch_subagents","apply":true,"execute_runners":false,"no_probe":true}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(
            text="继续创建 tester 和 acceptor 子代理，不能只因 worker 验收通过就收口。",
            backend=self.name,
        )


# LLM: _DispatchThenOverclaimBackend reproduces a root model calling blocked subagents "passed".
# 类用途: 第一次执行 dispatch，第二次故意过度乐观收口，用来验证系统会用真实状态覆盖报喜稿。
class _DispatchThenOverclaimBackend:
    name = "fake_dispatch_then_overclaim_backend"

    def __init__(self):
        self.calls = 0

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"dispatch_subagents","apply":false,"execute_runners":false,"no_probe":true}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
            )
        return ModelResponse(text="测试通过，所有子代理已经完成。", backend=self.name)


# LLM: verifies runner completion artifacts short-circuit extra model turns.
# 函数用途: 子代理成功写出自己的 output.json 后，应直接进入等待验收，避免继续请求模型导致卡住或烧 token。
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
            thought="模拟真实 runner 完成产物后等待父级验收。",
            plan=["写结果", "停止工具循环"],
            allowed_tools=["write_file"],
        )
        agent.backend = OutputJsonCompletionBackend(Path(task.output_json))

        result = agent.run_subagent(task.id, dry_run=False, probe=False)

        assert agent.backend.calls == 1
        assert result.status == "AWAITING_ACCEPTANCE"
        assert result.verification_status == "NEEDS_ACCEPTANCE"
        assert result.structured_output_found is True
        assert result.structured_output_ok is True
        assert result.tool_rounds == 1


# LLM: verifies top-level dispatch completion does not need a final model turn.
# 函数用途: 子代理任务全都 DONE/VERIFIED 后，顶层主代理执行 dispatch_subagents 应本地收口，避免真实网络 final-call 卡住。
def test_completed_dispatch_closes_without_extra_model_call():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        task = _done_verified_task(agent)
        agent.backend = DispatchCompletionBackend()

        result = agent.run("推进并汇报已完成的子代理", save=False)

        assert agent.backend.calls == 1
        assert result.tool_rounds == 1
        assert "未再发起额外模型请求" in result.response
        assert task.id in result.response


# LLM: explicit QA/acceptor instructions must override deterministic one-worker closeout.
# 函数用途: 用户要求 worker 完成后继续创建测试/验收角色时，主循环不能因为已有 worker DONE/VERIFIED 就本地收口。
def test_completed_dispatch_does_not_close_when_prompt_requires_quality_roles():
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
        agent.backend = _DispatchThenQualityBackend()

        result = agent.run("worker 完成后必须继续创建 tester 和 acceptor 做测试验收", save=False)

        assert agent.backend.calls == 2
        assert "tester/acceptor" in result.response
        assert "不能按完成汇报" in result.response
        assert "未再发起额外模型请求" not in result.response


# LLM: generic human wording like "验收结果" should not force a separate acceptor role.
# 函数用途: 复现真实 E2E 里用户只要求主代理安排和验收，却被误判缺 acceptor 导致完成态自相矛盾。
def test_generic_acceptance_result_wording_does_not_require_acceptor_role():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        task = _done_verified_task(agent)
        agent.backend = DispatchCompletionBackend()

        result = agent.run("请你作为主代理来安排和验收，完成后只汇报产物路径和验收结果。", save=False)

        assert agent.backend.calls == 1
        assert "未再发起额外模型请求" in result.response
        assert "missing_quality_roles" not in result.response
        assert "结论修正" not in result.response
        assert task.id in result.response


# LLM: final root answers must not overclaim success when persisted subagent tasks are blocked.
# 函数用途: 复现真实 E2E 里产物存在但 task.json 未全绿，模型却说测试通过的问题。
def test_final_response_warns_when_subagent_tree_still_has_blockers():
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
            goal="残留阻塞任务 fixture",
            thought="用于测试最终回答不能过度乐观。",
            plan=["失败", "等待恢复"],
            allowed_tools=[],
        )
        task.status = "BLOCKED"
        task.verification_status = "FAILED"
        task.failure_type = "acceptance_failed"
        agent.subagents.save(task)
        agent.backend = _DispatchThenOverclaimBackend()

        result = agent.run("推进恢复并汇报", save=False)

        assert "测试通过，所有子代理已经完成。" not in result.response
        assert "结论修正" in result.response
        assert "不能按完成汇报" in result.response
        assert task.id in result.response


# LLM: _done_verified_task creates a traceable finished subagent for top-level closeout tests.
# 函数用途: 构造已完成且已验收的子代理任务，并写入最小 output.json，供 dispatch 收口测试复用。
def _done_verified_task(agent):
    task = agent.subagents.create_run(
        goal="已完成任务 fixture",
        thought="用于测试顶层 dispatch 本地收口。",
        plan=["完成", "验收"],
        allowed_tools=[],
    )
    task.status = "DONE"
    task.verification_status = "VERIFIED"
    task.evidence.append(VerificationEvidence(
        kind="note",
        summary="任务已有验收证据。",
        ok=True,
    ))
    task.evidence_packets.append(EvidencePacket(
        id="evpkt-dispatch-closeout",
        claim="任务已完成并可追踪。",
        checked_scope="dispatch closeout fixture",
        evidence_refs=[task.output_json],
        artifact_refs=[task.output_json],
        confidence=0.9,
    ))
    Path(task.output_json).write_text(
        json.dumps({
            "status": "AWAITING_ACCEPTANCE",
            "summary": "fixture done",
            "evidence_packets": [{
                "id": "evpkt-dispatch-closeout",
                "claim": "任务已完成并可追踪。",
                "checked_scope": "dispatch closeout fixture",
                "evidence_refs": [task.output_json],
                "artifact_refs": [task.output_json],
                "confidence": 0.9,
            }],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    agent.subagents.save(task)
    return task


def test_tool_allowlist_limits_prompt_and_execution():
    """LLM: verify that allowed_tools filters both the prompt catalog and tool execution.

    新手说明:
    只允许 read_file 时，write_file 不应出现在 prompt 里，执行也应被拒绝。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        result = agent.run("读取 notes.txt", save=False, allowed_tools=["read_file"])
        blocked = agent.tools.execute_call(
            {"tool": "write_file", "path": "x.txt", "content": "x"},
            allowed_tools=["read_file"],
        )

        assert "read_file [filesystem]" in result.prompt
        assert "write_file [filesystem]" not in result.prompt
        assert not blocked.ok
        assert "未授权" in blocked.output
