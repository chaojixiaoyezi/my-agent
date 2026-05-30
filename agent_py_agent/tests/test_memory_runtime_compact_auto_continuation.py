"""Runtime compact auto-continuation tests.
运行时自动 compact 续接测试。"""

from __future__ import annotations

from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.finalization_compact_auto import (
    _should_return_after_continuation,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent


class CaptureBackend:
    name = "capture"

    def __init__(self):
        self.prompts: list[str] = []
        self.usages: list[dict[str, int]] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        usage = self.usages[min(len(self.prompts) - 1, len(self.usages) - 1)] if self.usages else {}
        return ModelResponse(text=f"capture response {len(self.prompts)}", backend=self.name, usage=usage)


class ContextOverflowThenCaptureBackend:
    name = "context-overflow-then-capture"
    context_window_tokens = 20_000

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        if len(self.prompts) == 1:
            return ModelResponse(
                text="context overflow before completion",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 19_500, "output_tokens": 50},
            )
        return ModelResponse(
            text=f"capture response {len(self.prompts)}",
            backend=self.name,
            usage={"input_tokens": 500, "output_tokens": 100},
        )


class RepeatingContextOverflowBackend:
    name = "repeating-context-overflow"
    context_window_tokens = 20_000

    def __init__(self):
        self.prompts: list[str] = []

    def generate(self, prompt: str, on_chunk=None):
        self.prompts.append(prompt)
        if len(self.prompts) in {1, 3}:
            return ModelResponse(
                text=f"context overflow checkpoint {len(self.prompts)}",
                backend=self.name,
                runtime_status="context_overflow",
                runtime_reason="context_overflow",
                runtime_source="provider_error",
                usage={"input_tokens": 19_500, "output_tokens": 50},
            )
        if len(self.prompts) == 2:
            return ModelResponse(
                text=(
                    "[TOOL_CALL]\n"
                    '{"tool":"write_file","path":"outputs/compact-repeat.txt","content":"继续后写入一次进展。"}\n'
                    "[/TOOL_CALL]"
                ),
                backend=self.name,
                usage={"input_tokens": 500, "output_tokens": 100},
            )
        return ModelResponse(
            text="重复压缩后完成。",
            backend=self.name,
            usage={"input_tokens": 500, "output_tokens": 100},
        )


def _finalize_context_for_continuation(*, tool_rounds: int, executed_tools: list) -> FinalizeContext:
    return FinalizeContext(
        user_prompt="继续做当前任务",
        final_prompt="",
        final_response=ModelResponse(text="", backend="test"),
        memories=[],
        executed_tools=executed_tools,
        archive_tool_calls=[],
        routed_context=None,
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id="req",
        run_id="run",
        task_id="task",
        source="test",
        do_save=True,
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
        tool_rounds=tool_rounds,
        compact_auto_continue_depth=1,
    )


def test_preflight_continuation_can_compact_again_after_tool_progress() -> None:
    ctx = _finalize_context_for_continuation(
        tool_rounds=3,
        executed_tools=[{"tool": "read_file", "success": True}],
    )

    assert _should_return_after_continuation(ctx, {"trigger_source": "preflight"}) is False


def test_preflight_continuation_returns_when_no_tool_progress() -> None:
    ctx = _finalize_context_for_continuation(tool_rounds=0, executed_tools=[])

    assert _should_return_after_continuation(ctx, {"trigger_source": "preflight"}) is True


def test_run_auto_compact_apply_continues_once_after_continue_packet(tmp_path):
    """LLM: Tests saved auto compact apply performs one guarded continuation turn."""
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    agent.backend.context_window_tokens = 20

    result = agent.run(
        "验收: auto compact packet exists\n约束: no automatic tool execution\n测试: focused compact runtime test",
        save=True,
        request_id="req-auto-compact",
        run_id="run-auto-compact",
        task_id="run-auto-compact",
        recovery_next_actions=["continue only after reading continue packet"],
    )

    assert result.memory_compact_auto_status == "returned_after_continuation"
    assert result.memory_compact_auto_next_action == "return_result"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_continue_ready is False
    assert result.memory_compact_auto_tool_execution == "none"
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    assert "# Compact Auto Continuation" in result.prompt
    assert (tmp_path / "memory_archive" / "compact_applies").exists()


def test_run_auto_compact_apply_continues_with_home_entries_and_packet(tmp_path):
    home = tmp_path / "home"
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(home)), tmp_path)
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend
    agent.home_paths.agents_md.write_text("执行制度：每轮先读 AGENTS。\n", encoding="utf-8")
    agent.home_paths.soul_md.write_text("人格：稳住状态继续干。\n", encoding="utf-8")
    agent.home_paths.user_md.write_text("用户偏好：大白话汇报。\n", encoding="utf-8")
    agent.home_paths.memory_md.write_text("关键记忆：不要重做已完成步骤。\n", encoding="utf-8")

    result = agent.run(
        "验收: continuation packet injected\n约束: do not redo completed work\n测试: focused compact auto continuation",
        save=True,
        request_id="req-auto-continuation",
        run_id="run-auto-continuation",
        task_id="run-auto-continuation",
        recovery_next_actions=["continue from compact next_step"],
    )

    assert len(backend.prompts) == 2
    assert result.response == "capture response 2"
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    second_prompt = backend.prompts[1]
    assert "# Home Entry: AGENTS.md" in second_prompt
    assert "# Home Entry: SOUL.md" in second_prompt
    assert "# Home Entry: USER.md" in second_prompt
    assert "# Home Entry: memory.md" in second_prompt
    assert second_prompt.index("# Home Entry: AGENTS.md") < second_prompt.index("# Compact Auto Continuation")
    assert "continue from compact next_step" in second_prompt
    assert "Do not redo completed work" in second_prompt


def test_run_auto_compact_apply_returns_after_no_tool_continuation(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend

    result = agent.run(
        "验收: compact packet exists\n约束: do not redo completed work\n测试: focused compact",
        save=True,
        request_id="req-auto-return-compact",
        run_id="run-auto-return-compact",
        task_id="run-auto-return-compact",
        recovery_next_actions=["continue from compact packet"],
    )

    apply_dir = tmp_path / "memory_archive" / "compact_applies"
    metadata_files = [
        path
        for path in apply_dir.glob("apply-*.json")
        if not any(marker in path.name for marker in (".apply_bundle.", ".restore_refs.", ".work_state_snapshot.", ".self_check"))
    ]
    assert len(backend.prompts) == 2
    assert len(metadata_files) >= 1
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continuation_depth == 1
    assert result.memory_compact_auto_status == "returned_after_continuation"


def test_run_auto_compact_apply_can_repeat_when_continuation_makes_tool_progress(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", enable_tools=True), tmp_path)
    backend = RepeatingContextOverflowBackend()
    agent.backend = backend

    result = agent.run(
        "做一个需要连续续接的长任务，过程中写一条进展记录。",
        save=True,
        request_id="req-repeat-compact",
        run_id="run-repeat-compact",
        task_id="run-repeat-compact",
    )

    apply_dir = tmp_path / "memory_archive" / "compact_applies"
    metadata_files = [
        path
        for path in apply_dir.glob("apply-*.json")
        if not any(marker in path.name for marker in (".apply_bundle.", ".restore_refs.", ".work_state_snapshot.", ".self_check"))
    ]
    assert len(backend.prompts) == 4
    assert len(metadata_files) >= 2
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continuation_depth == 2
    assert (tmp_path / "outputs" / "compact-repeat.txt").read_text(encoding="utf-8") == "继续后写入一次进展。"


def test_run_auto_compact_apply_continues_with_optional_work_notes_missing(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    backend = ContextOverflowThenCaptureBackend()
    agent.backend = backend

    result = agent.run("请生成足够长的 compact 提示触发内容", save=True, request_id="req-blocked-continuation")

    assert len(backend.prompts) == 2
    assert result.memory_compact_auto_continued is True
    assert result.memory_compact_auto_continued_from_apply_id
    assert "# Compact Auto Continuation" in backend.prompts[1]


def test_run_auto_compact_normal_final_returns_without_auto_continuation(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo"), tmp_path)
    backend = CaptureBackend()
    agent.backend = backend
    agent.backend.context_window_tokens = 20_000
    backend.usages = [{"input_tokens": 19_000, "output_tokens": 100}]

    result = agent.run("请整理材料，写完后直接汇报完成。", save=True, request_id="req-normal-final-compact")

    assert len(backend.prompts) == 1
    assert result.memory_compact_suggested is True
    assert result.memory_compact_auto_status == "applied_return_result"
    assert result.memory_compact_auto_next_action == "return_result_after_compact"
    assert result.memory_compact_auto_allowed_to_continue is False
    assert result.memory_compact_auto_continued is False
