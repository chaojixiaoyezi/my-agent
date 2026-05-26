from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


# LLM: skipped hint rounds must still produce one correction instead of silently disappearing.
# 函数用途: 验证恢复状态越过提示节点时会补发最近的通用提示，并且同一节点不会重复刷屏。
def test_exploration_fuse_emits_crossed_hint_once(tmp_path: Path):
    import json

    from agent_py_agent.agent.agent_core.tool_exploration_fuse import exploration_fuse_context

    state_dir = tmp_path / ".agent_delivery"
    state_dir.mkdir()
    state_file = state_dir / "exploration_fuse.json"
    state_file.write_text(
        json.dumps({"exploration_rounds_without_local_progress": 61}),
        encoding="utf-8",
    )
    agent = SimpleNamespace(root=tmp_path)

    context = exploration_fuse_context(agent, redirects=0)

    assert "20%" in context
    assert "下一轮请执行本地落地动作" in context
    assert exploration_fuse_context(agent, redirects=0) == ""


# LLM: unlimited exploration mode still sends crossed reminders without ever becoming a block.
# 函数用途: 验证 threshold=0 时如果恢复状态跨过固定提醒节点，也会补发最近提醒且不进入阻断。
def test_exploration_fuse_unlimited_mode_emits_crossed_fixed_hint_once(tmp_path: Path):
    import json

    from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        exploration_fuse_context,
        has_pending_exploration_fuse,
    )

    state_dir = tmp_path / ".agent_delivery"
    state_dir.mkdir()
    (state_dir / "exploration_fuse.json").write_text(
        json.dumps({"exploration_rounds_without_local_progress": 151}),
        encoding="utf-8",
    )
    agent = SimpleNamespace(root=tmp_path, _exploration_fuse_config=ExplorationFuseConfig(round_threshold=0))

    context = exploration_fuse_context(agent, redirects=0)

    assert "第 150 轮固定提醒" in context
    assert has_pending_exploration_fuse(agent) is False
    assert exploration_fuse_context(agent, redirects=0) == ""


# LLM: exploration-only loops should warn at configured budget ratios before blocking.
# 函数用途: 验证默认 300 轮探索额度会在 1/5、2/5、4/5 处提示，最终到额度上限才阻断。
def test_exploration_fuse_uses_configured_budget_ratio_hints(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        exploration_fuse_block_response,
        exploration_fuse_context,
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(59):
        assert has_required_exploration_fuse(agent, calls) is False

    assert has_required_exploration_fuse(agent, calls) is True
    context = exploration_fuse_context(agent, redirects=0)
    assert "[tool-system exploration-fuse]" in context
    assert has_pending_exploration_fuse(agent) is False
    assert "20%" in context
    assert "连续探索额度" in context
    assert "建议先写出" in context
    assert "下一轮优先做一次本地落地动作" in context
    assert "保存来源索引、阶段笔记、检查点、草稿、结构化数据或目标产物" in context
    assert "materialize_local_progress" in context
    assert "write_file" in context

    for _ in range(59):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    assert "40%" in exploration_fuse_context(agent, redirects=0)

    for _ in range(119):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    urgent_context = exploration_fuse_context(agent, redirects=0)
    assert "80%" in urgent_context
    assert "请尽快" in urgent_context
    assert "下一轮必须优先物化本地进展" in urgent_context

    for _ in range(59):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    assert has_pending_exploration_fuse(agent) is True
    assert exploration_fuse_context(agent, redirects=0) == ""
    assert "[EXPLORATION_FUSE_BLOCKED]" in exploration_fuse_block_response(agent).text


# LLM: zero exploration fuse threshold disables the count cap while still recording exploration facts.
# 函数用途: 验证探索熔断阈值为 0 时不阻断，只在 50、150、250 轮给固定提醒。
def test_exploration_fuse_zero_threshold_uses_fixed_hints_without_blocking(tmp_path: Path):
    from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        exploration_fuse_context,
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path, _exploration_fuse_config=ExplorationFuseConfig(round_threshold=0))
    calls = [{"tool": "fetch_url", "url": "https://example.test/data.json"}]

    for _ in range(49):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    context = exploration_fuse_context(agent, redirects=0)
    assert "第 50 轮固定提醒" in context
    assert "不会因次数阻断" in context
    assert "下一轮优先做一次本地落地动作" in context

    for _ in range(99):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    assert "第 150 轮固定提醒" in exploration_fuse_context(agent, redirects=0)

    for _ in range(99):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    assert "第 250 轮固定提醒" in exploration_fuse_context(agent, redirects=0)

    for _ in range(50):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_pending_exploration_fuse(agent) is False


# LLM: durable local progress clears exploration debt so a long task can continue normally.
# 函数用途: 验证写文件、分块写入或构建类工具调用会重置只读探索计数。
def test_exploration_fuse_resets_when_local_progress_happens(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_exploration_fuse import (
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "read_artifact", "artifact_ref": "memory_archive/artifacts/tool_outputs/data.json"}]

    for _ in range(300):
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
    for _ in range(300):
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

    for _ in range(59):
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
    for _ in range(59):
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

    for _ in range(240):
        has_required_exploration_fuse(agent, calls)

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
