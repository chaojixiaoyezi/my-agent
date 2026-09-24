"""恢复宿主决定摘要后解绑旧请求的完整原生历史：失败、取消、超限收尾都不再读它，自动 noop 仍发送完整原请求。"""
from __future__ import annotations

from dataclasses import replace

import pytest

from agent_py_agent.agent import gateway_compact_recovery as recovery
from agent_py_agent.agent.agent_core import compact_request_recovery as recovery_core
from agent_py_agent.agent.agent_core import provider_transient_auto_resume
from agent_py_agent.agent.backends.errors import (
    ProviderContextWindowError,
    ProviderTimeoutError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation import active_turn_compact, background_execution, compact
from agent_py_agent.agent.conversation.compact_guard import ConversationCompactError
from agent_py_agent.agent.gateway_parts import request_execution
from agent_py_agent.agent.model_request_selection import _HOST
from agent_py_agent.tests.test_compact_native_ir_recovery import _native_ir_attempt
from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
from agent_py_agent.tests.test_gateway_model_adoption import fake_http
from agent_py_agent.tests.test_subagent_compact_recovery import _http


# LLM: 测试替身：已解绑的旧历史若被任何收尾路径读取，就记录并抛错；不改变被测流程的其它行为。
# 类用途: 代替解绑后的空列表，钉住"失败、取消、超限收尾不读旧请求历史"。
class _PoisonedHistory(list):
    def __init__(self, reads: list[str]):
        super().__init__()
        self.reads = reads

    # LLM: 任何读取都记账后抛错，调用方不能静默吞掉。
    # 函数用途: 记录一次对已解绑历史的访问并中断当前路径。
    def _read(self, name: str):
        self.reads.append(name)
        raise AssertionError(f"收尾读取了已解绑的旧请求历史: {name}")

    def __iter__(self):
        return self._read("__iter__")

    def __len__(self):
        return self._read("__len__")

    def __getitem__(self, key):
        return self._read("__getitem__")

    def __bool__(self):
        return self._read("__bool__")

    def __eq__(self, other):
        return self._read("__eq__")

    __hash__ = None


# LLM: 只在恢复宿主已决定摘要（解绑之后）调用：确认解绑已发生，再换上会报错的替身。
# 函数用途: 断言原参数的旧历史已解绑，并安装读取即报错的替身。
def _poison_released_history(params, reads: list[str], released: list[bool]) -> None:
    history = params.provider_history_messages
    released.append(type(history) is list and history == [])
    object.__setattr__(params, "provider_history_messages", _PoisonedHistory(reads))


_SUMMARY_FAILURES = {
    "summary_transient": lambda: ProviderTransientError("503 temporary overload"),
    "summary_timeout": lambda: ProviderTimeoutError("first event timeout", stage="first_event"),
}


# 函数用途: 模拟另一个提交者先推进了代次。
def _competing_commit(thread):
    return replace(thread, compact_generation=thread.compact_generation + 1, summary="另一个提交者已经获胜")


# 函数用途: 候选期代次冲突。
def _bump_generation(fixture, _host) -> None:
    fixture.agent.conversation_store.threads.update_atomic(fixture.thread_id, _competing_commit)


# 函数用途: 候选期取消。
def _cancel_candidate(_fixture, _host) -> None:
    raise InterruptedError("测试取消候选")


# 函数用途: 只取消运行令牌。
def _cancel_token(_fixture, host) -> None:
    host.compact_recovery.render_params.cancellation_token.cancel("只取消运行令牌")


# 函数用途: 候选投影未知。
def _unknown_projection(_fixture, _host) -> None:
    raise ConversationCompactError("测试未知完整输入", code="COMPACT_REQUEST_PROJECTION_UNKNOWN")


# LLM: 与原失败用例同款故障，只在第一次候选投影时注入；不改变其它流程。
_CANDIDATE_FAILURES = {
    "generation": _bump_generation, "cancel": _cancel_candidate,
    "token": _cancel_token, "unknown": _unknown_projection,
}


# LLM: 只记录事实并注入故障；替身装在解绑之后最先到达的钩子上（摘要或候选投影）。
# 类用途: 驱动一次 Gateway 恢复失败，记录旧历史是否已解绑、收尾是否读过它。
class _GatewayFailureRun:
    def __init__(self, fixture, failure: str):
        self.fixture, self.failure = fixture, failure
        self.reads, self.released, self.hosts, self.summaries, self.seen = [], [], [], [], []
        self.original_project = recovery._project_recovery_candidate
        self.original_summarize = compact._summarize

    # 函数用途: 第一次到达解绑之后的钩子时安装读取即报错的替身。
    def poison_once(self) -> None:
        if not self.released:
            _poison_released_history(self.hosts[0].compact_recovery.render_params, self.reads, self.released)

    # 函数用途: 替身化后照原样摘要，或按失败类型抛出摘要错误。
    def summarize(self, *args, **kwargs):
        self.poison_once()
        self.summaries.append(True)
        error = _SUMMARY_FAILURES.get(self.failure)
        if error is not None:
            raise error()
        return self.original_summarize(*args, **kwargs)

    # 函数用途: 候选投影时替身化并注入候选期故障。
    def project(self, *args):
        if args[-1].is_candidate:
            self.poison_once()
            inject = _CANDIDATE_FAILURES.get(self.failure)
            if inject is not None:
                inject(self.fixture, self.hosts[0])
        return self.original_project(*args)

    # 函数用途: 首个业务请求返回溢出以进入恢复；之后只允许摘要请求。
    def on_business(self, wire) -> None:
        if not self.seen:
            self.seen.append("overflow")
            self.hosts.append(_HOST.get())
            raise ProviderContextWindowError("测试供应商上下文溢出")
        assert not self.hosts[0].compact_recovery.committed, "失败路径不能发送业务请求"
        self.seen.append("summary")


@pytest.mark.parametrize(
    "failure", ["generation", "cancel", "token", "unknown", "summary_transient", "summary_timeout", "too_large"],
)
def test_gateway_recovery_closeout_never_reads_released_history(tmp_path, monkeypatch, failure):
    fixture = _history_request(tmp_path)
    run = _GatewayFailureRun(fixture, failure)
    original_ceiling = compact._compact_request_input_ceiling
    monkeypatch.setattr(provider_transient_auto_resume, "_wait_before_retry", lambda *_: None)
    monkeypatch.setattr(compact, "_summarize", run.summarize)
    monkeypatch.setattr(recovery, "_project_recovery_candidate", run.project)
    if failure == "too_large":
        monkeypatch.setattr(compact, "_compact_request_input_ceiling",
                            lambda *args: 1 if run.summaries else original_ceiling(*args))
    fake_http(monkeypatch, fixture, on_business=run.on_business)
    with pytest.raises((RuntimeError, InterruptedError)):
        request_execution._run_gateway_ask(fixture.context)
    assert run.released == [True], "决定摘要后、候选投影或摘要之前，原参数的旧历史已解绑"
    assert run.reads == [], "失败、取消、超限收尾不得读取已解绑的旧请求历史"
    assert run.hosts[0].compact_recovery.committed is False
    thread = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.compact_generation == (1 if failure == "generation" else 0)


@pytest.mark.parametrize("failure", ["cancel", "too_large"])
def test_background_recovery_closeout_never_reads_released_history(tmp_path, monkeypatch, failure):
    agent, store, thread, execution, sink, history, params, context, _recorded = _native_ir_attempt(
        tmp_path, monkeypatch, backend="anthropic_compatible", full_result="FULL-CONTENT-" + "y" * 5_000,
    )
    reads, released, summaries = [], [], []
    original_project = recovery_core._project_mixed_recovery_material
    original_summary = active_turn_compact._active_turn_replacement_summary
    original_ceiling = compact._compact_request_input_ceiling
    in_summary = False

    def summary(*args, **kwargs):
        nonlocal in_summary
        if not released:
            _poison_released_history(_HOST.get().render_params, reads, released)
        in_summary = True
        try:
            return original_summary(*args, **kwargs)
        finally:
            in_summary = False

    def project(*args):
        if failure == "cancel" and args[1].is_candidate:
            raise InterruptedError("测试取消原生IR候选")
        return original_project(*args)

    def on_http(wire, _number):
        if in_summary:
            summaries.append(wire)
        else:
            pytest.fail("原生 IR 候选未提交时发送了业务请求")

    monkeypatch.setattr(recovery_core, "_project_mixed_recovery_material", project)
    monkeypatch.setattr(active_turn_compact, "_active_turn_replacement_summary", summary)
    if failure == "too_large":
        monkeypatch.setattr(compact, "_compact_request_input_ceiling",
                            lambda *args: 1 if summaries else original_ceiling(*args))
    _http(monkeypatch, backend="anthropic_compatible", on_business=on_http)
    expected = InterruptedError if failure == "cancel" else ConversationCompactError
    with pytest.raises(expected):
        background_execution._run_background_recovery_attempt(
            execution, "继续核对完整工具结果", params, history, context, recovering=True, activity_sink=sink,
        )
    assert released == [True] and reads == []
    assert store.threads.require(thread.thread_id).compact_generation == 0


def test_automatic_noop_sends_original_request_with_complete_history(tmp_path, monkeypatch):
    fixture = _history_request(tmp_path, history_repeat=1)
    selects, sends = [], []
    original_select = recovery_core.PreparedCompactRecovery.select

    def select(self, agent, params, prompt):
        history_before = len(params.provider_history_messages)
        value = original_select(self, agent, params, prompt)
        selects.append((self.force, self.committed, history_before, len(params.provider_history_messages), value[0] is params))
        return value

    monkeypatch.setattr(recovery_core.PreparedCompactRecovery, "select", select)
    fake_http(monkeypatch, fixture, on_business=lambda wire: sends.append(wire))
    request_execution._run_gateway_ask(fixture.context)
    assert selects, "带可压缩来源的普通请求会安装自动恢复宿主"
    force, committed, before, after, same_params = selects[0]
    assert (force, committed, same_params) == (False, False, True)
    assert before > 0 and after == before, "自动 noop 原样发送原请求，旧历史不解绑"
    assert len(sends) == 1


def test_committed_candidate_shares_tool_context_with_original_params(tmp_path, monkeypatch):
    fixture = _history_request(tmp_path)
    shared = []
    original_select = recovery_core.PreparedCompactRecovery.select

    def select(self, agent, params, prompt):
        value = original_select(self, agent, params, prompt)
        if self.committed:
            # 合同：候选参数与原参数共享同一个 tool_context 列表（dataclasses.replace 的既有行为），
            # 提交后写入任一份的插话或修复提示对另一份可见；改为复制前必须先迁移这条合同。
            shared.append(value[0].tool_context is params.tool_context)
        return value

    seen = []

    def on_business(wire):
        if not seen:
            seen.append("overflow")
            raise ProviderContextWindowError("测试供应商上下文溢出")
        seen.append("sent")

    monkeypatch.setattr(recovery_core.PreparedCompactRecovery, "select", select)
    fake_http(monkeypatch, fixture, on_business=on_business)
    request_execution._run_gateway_ask(fixture.context)
    assert shared == [True]
