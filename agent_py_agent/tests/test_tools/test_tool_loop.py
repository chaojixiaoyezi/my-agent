"""LLM: tests for tool loop, delegation, max rounds, allowlists, and security grants.

给人看的解释：
这个文件放和"工具循环流程"相关的测试：工具调用闭环、子代理派工和去重、最大轮数收口、
子代理 output.json 收口、工具授权过滤和安全能力授权。
"""

import json
import tempfile
from pathlib import Path

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.tool_round_execution import ToolCallExecuteParams
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.log_analysis.storage import LocalLogStore
from agent_py_agent.agent.subagent import EvidencePacket, VerificationEvidence
from agent_py_agent.agent.tools import ToolExecutionResult, ToolRegistry

from .backends import (
    DispatchCompletionBackend,
    DuplicateSubagentDelegationBackend,
    MaxToolRoundBackend,
    OutputJsonCompletionBackend,
    RepeatedDispatchBackend,
    StubbornToolAfterLimitBackend,
    SubagentDelegationBackend,
    ToolCallingBackend,
)


# LLM: _OneShotHarnessAgent gives ToolLoopService only the attributes needed for private one-shot tests.
# 函数用途: 避免为一次性调度去重单测启动完整 SimpleAgent，同时保持工具执行路径真实。
class _OneShotHarnessAgent:
    def __init__(self, tools):
        self.tools = tools


# LLM: _BlockedScheduleTools simulates a semantic schedule block with a successful tool envelope.
# 函数用途: 返回 ok=True 但 JSON 里 blocked=true 的真实 schedule_child_subagents 输出形状。
class _BlockedScheduleTools:
    def __init__(self):
        self.calls = 0

    def execute_call(self, payload, *, allowed_tools=None, granted_capabilities=None, write_boundary=None):
        self.calls += 1
        return ToolExecutionResult(
            "schedule_child_subagents",
            True,
            '{"blocked": true, "reason": "duplicate_leaf_target:app.js", "created_run_ids": []}',
        )


def test_tool_loop_and_prompt_transcript():
    """LLM: verify that a tool call round feeds tool output back to the model for a final answer.

    新手说明:
    模拟一次读文件工具调用，确认工具结果出现在后续 prompt 中，并且最终回答正确。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello tool world", encoding="utf-8")
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl")
        agent = SimpleAgent(cfg, workspace)
        agent.backend = ToolCallingBackend()
        result = agent.run("读取 notes.txt 并总结", save=False)
        assert result.response == "工具执行完成"
        assert result.tool_rounds == 1
        assert "hello tool world" in result.prompt


def test_agent_can_delegate_to_subagents_from_tool_call():
    """LLM: verify that a create_subagents tool call creates tasks and dispatch control works.

    新手说明:
    测试子代理创建、任务板查看、干跑调度和执行调度拦截。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = SubagentDelegationBackend()

        result = agent.run("请创建两个子代理做隔离 coding 场景测试", save=False)
        tasks = agent.subagents.list_runs()
        board = agent.tools.execute_call({"tool": "subagent_board", "limit": 5})
        dry_dispatch = agent.tools.execute_call(
            {"tool": "dispatch_subagents", "apply": False, "max_runners": 1}
        )
        blocked_dispatch = agent.tools.execute_call(
            {"tool": "dispatch_subagents", "execute_runners": True, "apply": False}
        )

        assert result.response == "已创建子代理任务并等待调度。"
        assert result.tool_rounds == 1
        assert len(tasks) == 2
        assert all("write_file" in task.allowed_tools for task in tasks)
        assert board.ok
        assert tasks[0].id in board.output
        assert dry_dispatch.ok
        assert '"dry_run": true' in dry_dispatch.output
        assert not blocked_dispatch.ok
        assert "必须配合 apply=true" in blocked_dispatch.output


def test_create_subagents_rejects_external_write_target_before_task_creation():
    """LLM: write-capable subagents should fail early for absolute paths outside workspace."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        external_dir = workspace.parent / "external-target"
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_subagents=3,
        )
        agent = SimpleAgent(cfg, workspace)

        result = agent.tools.execute_call(
            {
                "tool": "create_subagents",
                "goal": f"在 {external_dir} 创建一个 txt 文件",
                "allowed_tools": ["read_file", "write_file"],
            }
        )

        assert not result.ok
        assert "工作区外" in result.output
        assert agent.subagents.list_runs() == []


def test_repeated_orchestration_tool_call_is_not_executed_twice():
    """LLM: verify that identical consecutive orchestration tool calls are deduplicated.

    新手说明:
    连续两次 create_subagents 只应执行一次，第二次被拦截。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = DuplicateSubagentDelegationBackend()

        result = agent.run("请只创建一个子代理", save=False)
        tasks = agent.subagents.list_runs()

        assert result.response == "重复派工已被拦截并收口。"
        assert result.tool_rounds == 2
        assert len(tasks) == 1


# LLM: blocked hierarchy schedule attempts must remain retryable after the parent narrows scope.
# 函数用途: 复现 R38 中 schedule_child_subagents 返回 blocked=true 后，被一次性调用去重挡住修正重试的问题。
def test_blocked_schedule_result_does_not_consume_one_shot_key():
    params = ToolLoopExecuteParams(
        user_prompt="",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes=None,
        request_id="",
        run_id="",
        task_id="",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    agent = _OneShotHarnessAgent(_BlockedScheduleTools())
    service = ToolLoopService(agent)
    payload = {"tool": "schedule_child_subagents", "apply": True, "children": [{"goal": "cart"}]}

    first = service._execute_one_tool_call(ToolCallExecuteParams(params, 1, 1, payload))
    second = service._execute_one_tool_call(ToolCallExecuteParams(params, 2, 1, payload))

    assert first.ok is True
    assert second.ok is True
    assert agent.tools.calls == 2
    assert "阻止重复执行" not in second.output


def test_repeated_dispatch_is_allowed_for_parent_progress_loops():
    """LLM: dispatch_subagents may need repeated identical calls when rate limits leave pending children."""
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            subagent_workspace="subs",
            max_tool_rounds=4,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = RepeatedDispatchBackend()

        result = agent.run("继续推进父节点调度", save=False)

        assert result.response == "重复 dispatch 已允许继续推进。"
        assert result.tool_rounds == 2
        assert "阻止重复执行" not in result.prompt


def test_max_tool_rounds_generates_final_response():
    """LLM: verify that hitting max_tool_rounds still produces a final model response.

    新手说明:
    把 max_tool_rounds 设为 0，模型应该收到轮数限制提示并给出回答。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        (workspace / "notes.txt").write_text("hello", encoding="utf-8")
        cfg = AgentConfig(
            enable_tools=True,
            memory_path="memory.jsonl",
            max_tool_rounds=0,
        )
        agent = SimpleAgent(cfg, workspace)
        agent.backend = MaxToolRoundBackend()

        result = agent.run("读取 notes", save=False)

        assert result.response == "工具轮数到顶后已正常收口。"
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2


# LLM: tool loop must not return a fresh TOOL_CALL as the final answer after max rounds.
# 函数用途: 模拟模型不听收口提示仍继续要工具，验证系统返回确定性停止说明而不是继续误导上层。
def test_max_tool_rounds_hard_stops_when_model_still_requests_tools():
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        cfg = AgentConfig(enable_tools=True, memory_path="memory.jsonl", max_tool_rounds=0)
        agent = SimpleAgent(cfg, workspace)
        agent.backend = StubbornToolAfterLimitBackend()

        result = agent.run("读取 notes", save=False)

        assert "已达到最大工具轮数限制" in result.response
        assert "后续工具请求不会被执行" in result.response
        assert "[TOOL_CALL]" not in result.response
        assert result.tool_rounds == 0
        assert agent.backend.calls == 2


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


def _make_tool_registry(workspace: Path) -> ToolRegistry:
    from agent_py_agent.agent.tools import ToolRegistryParams
    return ToolRegistry(
        ToolRegistryParams(
            workspace_root=workspace,
            max_chars=12000,
            max_entries=100,
            max_matches=50,
            web_max_chars=12000,
            http_timeout=30,
            catalog_limit=20,
            retrieval_limit=3,
            vector_search_enabled=False,
        )
    )


def test_security_tools_are_hidden_by_default_and_require_authorization():
    """LLM: verify security tools are hidden from catalog and blocked without authorization.

    新手说明:
    安全工具默认不出现在工具目录和推荐列表中，调用时会被拒绝。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = _make_tool_registry(workspace)

        catalog = registry.render_catalog_section()
        recommended = registry.render_recommended_tools_section("investigate security logs for attacker ip")
        blocked = registry.execute_call(
            {
                "tool": "security_query",
                "start_time": "2026-04-30T09:00:00Z",
                "end_time": "2026-04-30T11:00:00Z",
                "limit": 10,
            }
        )

        assert "security_query [log_analysis]" not in catalog
        assert "security_query" not in recommended
        assert not blocked.ok
        assert "not authorized" in blocked.output


def test_security_tools_are_exposed_for_security_capability_or_tool_grant():
    """LLM: verify security tools appear and work when capability or tool grant is provided.

    新手说明:
    授予 logs/security 能力或显式允许 security_query 工具后，安全工具可正常使用。
    """
    with tempfile.TemporaryDirectory() as td:
        workspace = Path(td)
        registry = _make_tool_registry(workspace)
        store = LocalLogStore(workspace)
        store.upsert_event(
            {
                "event_id": "evt-1",
                "event_time": "2026-04-30T10:00:00Z",
                "source_id": "waf-prod",
                "alert_type": "web_attack",
                "attacker_ip": "198.51.100.10",
                "payload": "A" * 500,
            }
        )

        capability_catalog = registry.render_catalog_section(granted_capabilities=["logs/security"])
        allowed_catalog = registry.render_catalog_section(allowed_tools=["security_query"])
        result = registry.execute_call(
            {
                "tool": "security_query",
                "attacker_ip": "198.51.100.10",
                "start_time": "2026-04-30T09:00:00Z",
                "end_time": "2026-04-30T11:00:00Z",
                "limit": 10,
            },
            granted_capabilities=["logs/security"],
        )
        payload = json.loads(result.output)

        assert "security_query [log_analysis]" in capability_catalog
        assert "security_query [log_analysis]" in allowed_catalog
        assert result.ok
        assert payload["tool"] == "security_query"
        assert payload["row_count"] == 1
        assert payload["evidence_refs"]
        assert "rows" not in payload
        assert "preview_rows" in payload
