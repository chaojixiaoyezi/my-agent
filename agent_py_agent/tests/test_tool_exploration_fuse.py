from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_exploration_fuse_emits_crossed_hint_once(tmp_path: Path):
    import json

    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import exploration_fuse_context

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
    assert "不要向用户转述" in context
    assert "下一轮请执行本地落地动作" in context
    assert exploration_fuse_context(agent, redirects=0) == ""


def test_exploration_fuse_unlimited_mode_emits_crossed_fixed_hint_once(tmp_path: Path):
    import json

    from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
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
    assert "不要向用户转述" in context
    assert has_pending_exploration_fuse(agent) is False
    assert exploration_fuse_context(agent, redirects=0) == ""


def test_exploration_fuse_uses_configured_budget_ratio_hints_without_blocking(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        exploration_fuse_context,
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "web_fetch", "url": "https://example.test/data.json"}]

    for _ in range(59):
        assert has_required_exploration_fuse(agent, calls) is False

    assert has_required_exploration_fuse(agent, calls) is True
    context = exploration_fuse_context(agent, redirects=0)
    assert "[tool-system exploration-fuse]" in context
    assert has_pending_exploration_fuse(agent) is False
    assert "20%" in context
    assert "连续探索额度" not in context
    assert "不要向用户转述" in context
    assert "建议先写出" in context
    assert "下一轮优先做一次本地落地动作" in context
    assert "保存来源索引、阶段笔记、检查点、草稿、结构化数据或目标产物" in context
    assert "suggested_next_action" in context
    assert "required_next_action" not in context
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
    assert "建议尽快" in urgent_context
    assert "下一轮优先考虑物化本地进展" in urgent_context

    for _ in range(59):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is False
    assert exploration_fuse_context(agent, redirects=0) == ""
    assert has_pending_exploration_fuse(agent) is False


def test_exploration_fuse_zero_threshold_uses_fixed_hints_without_blocking(tmp_path: Path):
    from agent_py_agent.agent.agent_core.exploration_fuse_config import ExplorationFuseConfig
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        exploration_fuse_context,
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path, _exploration_fuse_config=ExplorationFuseConfig(round_threshold=0))
    calls = [{"tool": "web_fetch", "url": "https://example.test/data.json"}]

    for _ in range(49):
        assert has_required_exploration_fuse(agent, calls) is False
    assert has_required_exploration_fuse(agent, calls) is True
    context = exploration_fuse_context(agent, redirects=0)
    assert "第 50 轮固定提醒" in context
    assert "不要向用户转述" in context
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


def test_exploration_fuse_resets_when_local_progress_happens(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "read_artifact", "artifact_ref": "blobs/tool_outputs/data.json"}]

    for _ in range(300):
        has_required_exploration_fuse(agent, calls)
    assert has_pending_exploration_fuse(agent) is False

    write_calls = [{"tool": "write_file", "action": "finish", "path": "outputs/checkpoint.md"}]
    assert has_required_exploration_fuse(agent, write_calls) is False
    assert has_pending_exploration_fuse(agent) is False


def test_exploration_fuse_state_prefers_current_task_work_dir(tmp_path: Path):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        has_required_exploration_fuse,
    )

    workspace_root = tmp_path / "source-workspace"
    task_root = tmp_path / "tasks" / "2026-06-07" / "read-code"
    work_dir = task_root / "work"
    params = replace(
        _params(),
        task_attributes={
            "run_workspace": {
                "task_root": str(task_root),
                "output_dir": str(task_root / "output"),
                "work_dir": str(work_dir),
            }
        },
    )
    agent = SimpleNamespace(root=workspace_root)

    assert has_required_exploration_fuse(agent, [{"tool": "read_file", "path": "input.txt"}], params) is False

    assert (work_dir / ".agent_delivery" / "exploration_fuse.json").exists()
    assert not (workspace_root / ".agent_delivery" / "exploration_fuse.json").exists()


def test_exploration_fuse_resets_on_structured_writer_and_document_builder(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        has_pending_exploration_fuse,
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    calls = [{"tool": "web_fetch", "url": "https://example.test/data.json"}]
    for _ in range(300):
        has_required_exploration_fuse(agent, calls)
    assert has_pending_exploration_fuse(agent) is False

    assert has_required_exploration_fuse(agent, [{"tool": "write_file", "rows": [{"a": 1}]}]) is False
    assert has_pending_exploration_fuse(agent) is False
    assert has_required_exploration_fuse(agent, [{"tool": "write_file", "path": "outputs/report.pdf"}]) is False


def test_exploration_fuse_run_command_classification_uses_command_token(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        has_required_exploration_fuse,
    )

    agent = SimpleNamespace(root=tmp_path)
    assert has_required_exploration_fuse(agent, [{"tool": "run_command", "command": "lsof -i :3000"}]) is False

    for _ in range(59):
        assert has_required_exploration_fuse(agent, [{"tool": "run_command", "command": "ls -la"}]) is False
    assert has_required_exploration_fuse(agent, [{"tool": "run_command", "command": "ls -la"}]) is False


def test_tool_loop_decision_redirects_exploration_fuse_without_blocking(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_guard.exploration_fuse import (
        has_required_exploration_fuse,
    )
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    agent = _agent(tmp_path)
    calls = [{"tool": "web_fetch", "url": "https://example.test/data.json"}]
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
    assert decision.action == "run_tools"
    assert decision.calls == calls
    assert decision.counters.exploration_fuse_redirects == 0
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
    assert final_decision.response.text == "任务完成。"


def _agent(root: Path):
    class _Tools:
        workspace_root = root

        def parse_tool_calls(self, text: str):
            if text == "CALL_FETCH":
                return [{"tool": "web_fetch", "url": "https://example.test/data.json"}]
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
