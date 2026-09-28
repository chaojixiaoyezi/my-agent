"""主代理同一回合“同一调用、同一失败”硬上限：合同单测 + 真实工具循环的 fake LLM 验证。

判定只看工具名、规范化参数摘要和归一化错误码；阈值复用 repeated_failure_halt_threshold（默认 15）。
命中后只结束当前回合（unfinished + REPEATED_IDENTICAL_TOOL_FAILURE），任务保持活跃，原因不可自动续跑。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import _conversation_turn_is_terminal
from agent_py_agent.agent.agent_core._tool_loop_service import (
    _final_response_after_repeated_failure,
    _mark_identical_failure_halt,
    _mark_repeated_failure_halt,
)
from agent_py_agent.agent.agent_core.tool_guard.call_guardrail_config import (
    repeated_failure_halt_threshold,
)
from agent_py_agent.agent.agent_core.tool_guard.identical_failure import (
    REPEATED_IDENTICAL_TOOL_FAILURE,
    IdenticalFailureStreak,
    next_identical_failure_streak,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.agent.turn_end import infer_turn_end_reason, should_continue_task

_THRESHOLD = 4


def _params(*, context_scope="default", threshold=_THRESHOLD):
    return SimpleNamespace(
        context_scope=context_scope, identical_failure_streak=None, identical_failure_halt=None,
        repeated_failure_halt=None, repeated_failure_halt_exhausted=False, authorization_failure_halt=None,
        tool_context=[], runtime_guard_policy=None,
        task_attributes={"repeated_failure_halt_threshold": threshold},
    )


def _record(params, spec=None):
    """spec 可覆盖 tool/args/code；code=None 表示这次调用成功。"""
    spec = {"tool": "skill_search", "args": "sha256:a", "code": "SKILL_SNAPSHOT_UNAVAILABLE", **(spec or {})}
    call = SimpleNamespace(tool_name=spec["tool"], args_hash=spec["args"])
    result = SimpleNamespace(ok=spec["code"] is None, error_code=spec["code"] or "", error_category="", output="")
    return SimpleNamespace(params=params, call=call, result=result)


def _feed(params, records):
    for spec in records:
        _mark_identical_failure_halt(_record(params, spec))
    return params


class TestStreak:
    def test_same_triple_accumulates_and_any_change_restarts(self):
        streak = None
        for _ in range(3):
            streak = next_identical_failure_streak(streak, "t", "a", "E")
        assert streak == IdenticalFailureStreak("t", "a", "E", 3)
        for changed in (("u", "a", "E"), ("t", "b", "E"), ("t", "a", "F")):
            assert next_identical_failure_streak(streak, *changed).count == 1


class TestMarkIdenticalFailureHalt:
    def test_halts_exactly_at_threshold(self):
        params = _feed(_params(), [{}] * (_THRESHOLD - 1))
        assert params.identical_failure_halt is None and params.repeated_failure_halt is None
        _feed(params, [{}])
        assert params.identical_failure_halt.to_dict() == {
            "tool_name": "skill_search", "args_hash": "sha256:a", "error_code": "SKILL_SNAPSHOT_UNAVAILABLE",
            "count": _THRESHOLD,
        }
        assert params.repeated_failure_halt == ("skill_search", "code:SKILL_SNAPSHOT_UNAVAILABLE", _THRESHOLD)
        assert params.repeated_failure_halt_exhausted is True

    @pytest.mark.parametrize("breaker", [
        {"code": None}, {"tool": "read_file"}, {"args": "sha256:b"}, {"code": "TOOL_INVALID_ARGUMENTS"},
    ])
    def test_interleaved_success_or_different_call_resets(self, breaker):
        params = _feed(_params(), [{}] * (_THRESHOLD - 1) + [breaker] + [{}] * (_THRESHOLD - 1))
        assert params.identical_failure_halt is None and params.repeated_failure_halt is None

    def test_later_success_in_same_batch_revokes_halt(self):
        params = _feed(_params(), [{}] * _THRESHOLD)
        assert params.identical_failure_halt is not None
        _feed(params, [{"tool": "read_file", "code": None}])
        assert params.identical_failure_halt is None and params.repeated_failure_halt is None
        assert params.repeated_failure_halt_exhausted is False

    def test_disabled_by_zero_threshold_and_skipped_for_task_local_children(self):
        assert _feed(_params(threshold=0), [{}] * 30).identical_failure_halt is None
        child = _feed(_params(context_scope="task_local"), [{}] * 30)
        assert child.identical_failure_halt is None and child.identical_failure_streak is None

    def test_existing_repair_hint_does_not_clear_or_contradict_the_halt(self):
        params = _feed(_params(), [{}] * _THRESHOLD)
        agent = SimpleNamespace(_tool_call_guardrail_records=())
        _mark_repeated_failure_halt(agent, _record(params))
        assert params.identical_failure_halt is not None and params.tool_context == []


class TestCloseoutContract:
    def test_host_closeout_is_unfinished_not_continuable_and_keeps_task_active(self, monkeypatch):
        from agent_py_agent.agent.agent_core import _tool_loop_service

        monkeypatch.setattr(_tool_loop_service, "build_tool_loop_prompt", lambda agent, params: "PROMPT")
        params = _feed(_params(), [{}] * _THRESHOLD)
        agent = SimpleNamespace(backend=SimpleNamespace(name="fake"))
        prompt, response = _final_response_after_repeated_failure(agent, params, 7)
        assert prompt == "PROMPT"
        assert (response.runtime_status, response.runtime_reason, response.runtime_source) == (
            "unfinished", REPEATED_IDENTICAL_TOOL_FAILURE, "tool_loop")
        assert "skill_search" in response.text and "SKILL_SNAPSHOT_UNAVAILABLE" in response.text
        assert should_continue_task(response) == (False, REPEATED_IDENTICAL_TOOL_FAILURE)
        assert infer_turn_end_reason(runtime_status=response.runtime_status,
                                     runtime_reason=response.runtime_reason) == "interrupted"
        assert _conversation_turn_is_terminal(SimpleNamespace(final_response=response)) is False

    def test_main_path_reads_shipped_threshold(self):
        assert repeated_failure_halt_threshold(SimpleNamespace(task_attributes={}, runtime_guard_policy=None)) == 15


# LLM: 假后端只按真实模型输入序列返回工具调用或最终文字，不替宿主执行工具或写回执。
# 类用途: 在真实主代理工具循环里重放“同一 read_file 反复失败”，观察宿主何时结束本轮。
class RepeatingBackend:
    name = "fake_identical_failure"

    # LLM: plan(n) 返回第 n 次模型调用要发出的参数；None 表示给最终文字。
    # 函数用途: 保存调用序列与计数。
    def __init__(self, plan):
        self.plan = plan
        self.calls = 0

    # LLM: 只声明本地假 native 能力，不探测供应商、不联网。
    # 函数用途: 让原运行循环使用结构化工具调用协议。
    def probe_tool_capability(self):
        return ProviderToolCapability(provider=self.name, endpoint="local://fake", model="", stream=False,
                                      native_supported=True, evidence="test_fake_native")

    # LLM: 不解析宿主提示正文，只按调用序号出牌；这样能证明结束来自宿主计数而不是模型“听劝”。
    # 函数用途: 发出预定的工具调用或最终答复。
    def generate(self, prompt, on_chunk=None, **kwargs):
        self.calls += 1
        arguments = self.plan(self.calls)
        if arguments is None:
            return ModelResponse(text="换了办法，完成。", backend=self.name)
        return ModelResponse(text="", backend=self.name, tool_use_blocks=[{
            "id": f"call_read_{self.calls}", "name": "read_file", "input": arguments}])


def _run(tmp_path, plan):
    (tmp_path / "present.txt").write_text("ok", encoding="utf-8")
    config = AgentConfig(my_agent_home=str(tmp_path / "home"), enable_plugins=False, enable_subagents=False,
                         home_context_enabled=False, max_tool_rounds=60, auto_save_memory=False)
    agent = SimpleAgent(config, tmp_path)
    backend = RepeatingBackend(plan)
    agent.backend = backend
    return agent.run("读一下资料。", save=False, allowed_tools=["read_file"]), backend


def test_fake_llm_identical_failing_call_ends_turn_at_threshold(tmp_path):
    result, backend = _run(tmp_path, lambda n: {"path": "missing.txt"})
    assert backend.calls == 15  # 第 15 次同调用同失败后宿主直接结束本轮，不再请求收口模型
    assert (result.runtime_status, result.runtime_reason) == ("unfinished", REPEATED_IDENTICAL_TOOL_FAILURE)
    assert result.turn_end_reason == "interrupted"
    assert "read_file" in result.response and "15" in result.response


@pytest.mark.parametrize("plan", [
    lambda n: None if n > 20 else {"path": f"missing-{n}.txt"},
    lambda n: None if n > 20 else {"path": "present.txt" if n == 10 else "missing.txt"},
], ids=["different-args", "success-in-between"])
def test_fake_llm_changed_args_or_interleaved_success_does_not_halt(tmp_path, plan):
    result, backend = _run(tmp_path, plan)
    assert backend.calls == 21
    assert result.runtime_reason != REPEATED_IDENTICAL_TOOL_FAILURE
    assert result.response == "换了办法，完成。"
