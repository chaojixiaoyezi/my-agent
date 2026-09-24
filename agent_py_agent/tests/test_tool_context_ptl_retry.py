"""单轮 PTL retry 钉子测试（compact 三件套之三，蓝本 终端交互 truncateHeadForPTLRetry）。

模型接口实报上下文超限时：回收最老工具结果正文 → 重拼 prompt 重试；
上限次数内救回则不触发 compact；无可回收/仍溢出则把溢出响应交回原 compact 路径。
"""

from __future__ import annotations

from agent_py_agent.agent.agent_core.tool_context.ptl_retry import (
    reclaim_oldest_tool_results_for_ptl,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot


def _entry(round_no: int, *, big: bool = True, anchor: bool = False) -> str:
    body = "X" * 3000 if big else "tiny"
    lines = [
        f"[tool-record round={round_no} index=1]",
        '{"tool":"read_file","path":"a.py"}',
        f"[tool-output-record round={round_no} index=1]",
        body,
    ]
    if anchor:
        lines.append('- read_artifact_hint: {"tool":"read_artifact"}')
    return "\n".join(lines)


def test_reclaims_oldest_fraction_in_place():
    ctx = [_entry(r) for r in range(10)]
    reclaimed = reclaim_oldest_tool_results_for_ptl(ctx)
    assert reclaimed == 2  # 10 * 0.2
    assert "ptl-reclaimed" in ctx[0] and "ptl-reclaimed" in ctx[1]
    assert "XXXX" not in ctx[0]
    assert "XXXX" in ctx[2]  # 较新条目保留


def test_unanchored_entries_also_reclaimed_as_last_resort():
    # 与 microcompact 不同：PTL 是最后手段，无锚点条目也回收
    ctx = [_entry(0, anchor=False), _entry(1, anchor=True)] + [_entry(r) for r in range(2, 10)]
    reclaim_oldest_tool_results_for_ptl(ctx)
    assert "ptl-reclaimed" in ctx[0]
    # 有锚点的保留取回线索
    assert "read_artifact_hint" in ctx[1]


def test_returns_zero_when_nothing_left_to_reclaim():
    ctx = [_entry(r) for r in range(3)]
    total = 0
    for _ in range(10):
        got = reclaim_oldest_tool_results_for_ptl(ctx, fraction=1.0)
        total += got
        if not got:
            break
    assert total == 3
    assert reclaim_oldest_tool_results_for_ptl(ctx, fraction=1.0) == 0  # 全部回收过 → 0


def test_small_entries_skipped():
    ctx = [_entry(r, big=False) for r in range(5)]
    assert reclaim_oldest_tool_results_for_ptl(ctx) == 0


def test_tool_loop_retries_until_provider_recovers():
    # 端到端：backend 前两次报 context 超限、第三次成功；重试在单轮内完成
    import tempfile
    from pathlib import Path

    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core._tool_loop_service import next_tool_loop_model_response
    from agent_py_agent.agent.backends import ModelResponse
    from agent_py_agent.agent.backends.errors import ProviderContextWindowError
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    class _OverflowThenOkBackend:
        name = "fake_overflow_then_ok"
        context_length = 200_000

        def __init__(self):
            self.calls = 0

        def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
            self.calls += 1
            if self.calls <= 2:
                raise ProviderContextWindowError("prompt too long, maximum context length exceeded")
            return ModelResponse(text="恢复成功，继续任务。", backend=self.name)

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl"),
            Path(td),
        )
        agent.backend = _OverflowThenOkBackend()
        params = ToolLoopExecuteParams(
            user_prompt="继续",
            memories=[],
            runtime_injections=[],
            prompt_files=[],
            tool_catalog_section="",
            tool_recommendations_section="",
            tool_context=[_entry(r) for r in range(10)],
            effective_on_chunk=None,
            allowed_tools=None,
            write_boundary=None,
            task_attributes=None,
            request_id="req-1",
            run_id="run-1",
            task_id="task-1",
            one_shot_tool_calls=set(),
            executed_tools=[],
            archive_tool_calls=[],
            save=False,
            tool_runtime_snapshot=agent.tools.runtime_snapshot(run_id="run-1"),
            tool_protocol_snapshot=make_test_protocol_snapshot(
                run_id="run-1",
                source_protocol="text",
            ),
        )
        turn = next_tool_loop_model_response(agent, params, tool_rounds=1)
        prompt, response = turn.prompt, turn.response

        assert agent.backend.calls == 3
        assert response.text == "恢复成功，继续任务。"
        # 两次重试各回收一批最老条目（第一轮 10*0.2=2 条，第二轮剩 8 条*0.2=1 条）
        assert sum("ptl-reclaimed" in item for item in params.tool_context) == 3


def test_tool_loop_falls_back_to_compact_when_disabled():
    # 配置 0 = 关闭：溢出响应原样返回，走原有 compact 路径
    import tempfile
    from pathlib import Path

    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core._tool_loop_service import next_tool_loop_model_response
    from agent_py_agent.agent.backends.errors import ProviderContextWindowError
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.config import AgentConfig

    class _AlwaysOverflowBackend:
        name = "fake_always_overflow"
        context_length = 200_000
        calls = 0

        def generate(self, prompt: str, on_chunk=None):
            type(self).calls += 1
            raise ProviderContextWindowError("prompt too long, maximum context length exceeded")

    with tempfile.TemporaryDirectory() as td:
        agent = SimpleAgent(
            AgentConfig(enable_tools=True, memory_path="memory.jsonl", tool_context_ptl_retry_max=0),
            Path(td),
        )
        agent.backend = _AlwaysOverflowBackend()
        params = ToolLoopExecuteParams(
            user_prompt="继续",
            memories=[],
            runtime_injections=[],
            prompt_files=[],
            tool_catalog_section="",
            tool_recommendations_section="",
            tool_context=[_entry(r) for r in range(4)],
            effective_on_chunk=None,
            allowed_tools=None,
            write_boundary=None,
            task_attributes=None,
            request_id="req-1",
            run_id="run-1",
            task_id="task-1",
            one_shot_tool_calls=set(),
            executed_tools=[],
            archive_tool_calls=[],
            save=False,
            tool_runtime_snapshot=agent.tools.runtime_snapshot(run_id="run-1"),
            tool_protocol_snapshot=make_test_protocol_snapshot(
                run_id="run-1",
                source_protocol="text",
            ),
        )
        turn = next_tool_loop_model_response(agent, params, tool_rounds=1)
        prompt, response = turn.prompt, turn.response

        assert _AlwaysOverflowBackend.calls == 1  # 关闭时不重试
        assert response.runtime_status == "context_overflow"
        assert all("ptl-reclaimed" not in item for item in params.tool_context)
