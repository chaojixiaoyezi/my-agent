from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


# LLM: exploration-only loops should be detected from structured tool calls, not prompt wording.
# 函数用途: 验证连续只读/抓取工具达到阈值后，会要求先物化本地 checkpoint、草稿或产物。
def test_exploration_fuse_redirects_after_repeated_read_only_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        exploration_fuse_context,
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(9):
        assert has_required_exploration_fuse(agent, calls) is False

    assert has_required_exploration_fuse(agent, calls) is True
    assert has_pending_exploration_fuse(agent) is True

    context = exploration_fuse_context(agent, redirects=0)
    assert "[tool-system exploration-fuse]" in context
    assert "materialize_local_progress" in context
    assert "write_file" in context


# LLM: zero exploration fuse threshold disables the count cap while still recording exploration facts.
# 函数用途: 验证探索熔断阈值为 0 时表示不限制轮数，不会把普通长研究任务卡死。
def test_exploration_fuse_zero_threshold_is_unlimited(tmp_path: Path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_exploration_fuse

    monkeypatch.setattr(tool_exploration_fuse, "_EXPLORATION_ROUND_THRESHOLD", 0)
    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(12):
        assert tool_exploration_fuse.has_required_exploration_fuse(agent, calls) is False
    assert tool_exploration_fuse.has_pending_exploration_fuse(agent) is False


# LLM: durable local progress clears exploration debt so a long task can continue normally.
# 函数用途: 验证写文件、分块写入或构建类工具调用会重置只读探索计数。
def test_exploration_fuse_resets_when_local_progress_happens(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/data.json"}]

    for _ in range(10):
        has_required_exploration_fuse(agent, calls)
    assert has_pending_exploration_fuse(agent) is True

    write_calls = [{"tool": "file_write_session", "action": "finish", "path": "outputs/checkpoint.md"}]
    assert has_required_exploration_fuse(agent, write_calls) is False
    assert has_pending_exploration_fuse(agent) is False


# LLM: structured writers and document builders count as durable local progress outside delivery contracts too.
# 函数用途: 验证无 delivery_contract 的真实长任务也不会把 JSON writer/PDF builder 当成探索空转。
def test_exploration_fuse_resets_on_structured_writer_and_document_builder(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]
    for _ in range(10):
        has_required_exploration_fuse(agent, calls)
    assert has_pending_exploration_fuse(agent) is True

    assert has_required_exploration_fuse(agent, [{"tool": "write_structured_json", "rows": [{"a": 1}]}]) is False
    assert has_pending_exploration_fuse(agent) is False
    assert has_required_exploration_fuse(agent, [{"tool": "markdown_to_pdf", "path": "outputs/report.pdf"}]) is False


# LLM: run_command classification should use the shell command token rather than brittle string prefixes.
# 函数用途: 验证 ls 会计入探索，但 lsof 不会因为同前缀被误判为 ls。
def test_exploration_fuse_run_command_classification_uses_command_token(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import has_required_exploration_fuse

    agent = SimpleNamespace(root=tmp_path)
    assert has_required_exploration_fuse(agent, [{"tool": "run_command", "command": "lsof -i :3000"}]) is False

    for _ in range(9):
        assert has_required_exploration_fuse(agent, [{"tool": "run_command", "command": "ls -la"}]) is False
    assert has_required_exploration_fuse(agent, [{"tool": "run_command", "command": "ls -la"}]) is True


# LLM: the tool-loop decision layer must redirect tool calls and final prose once exploration debt is active.
# 函数用途: 验证模型达到探索熔断阈值后，不能通过继续 fetch 或改成普通文字收口绕过本地落地要求。
def test_tool_loop_decision_redirects_and_blocks_exploration_fuse(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import has_required_exploration_fuse
    from agent_py_agent.agent.agent_core.tool_loop_repair_counters import ToolLoopRepairCounters
    from agent_py_agent.agent.agent_core.tool_loop_response_decision import (
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backend import ModelResponse

    agent = _agent(tmp_path)
    calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]
    for _ in range(9):
        assert has_required_exploration_fuse(agent, calls) is False

    params = _params()
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="CALL_FETCH", backend="fake"),
            counters=ToolLoopRepairCounters(),
        )
    )
    assert decision.action == "continue"
    assert decision.counters.exploration_fuse_redirects == 1
    assert any("exploration-fuse" in item for item in params.tool_context)

    final_decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=_params(),
            response=ModelResponse(text="任务完成。", backend="fake"),
            counters=ToolLoopRepairCounters(exploration_fuse_redirects=2),
        )
    )
    assert final_decision.action == "break"
    assert "[EXPLORATION_FUSE_BLOCKED]" in final_decision.response.text
    assert final_decision.response.runtime_status == "blocked"
    assert final_decision.response.runtime_reason == "EXPLORATION_FUSE"


def _agent(root: Path):
    class _Tools:
        workspace_root = root

        def parse_tool_calls(self, text: str):
            if text == "CALL_FETCH":
                return [{"tool": "fetch_url", "url": "https://example.test/data.json"}]
            return []

    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=root,
        tools=_Tools(),
    )


def _params():
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="test",
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
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={},
    )
