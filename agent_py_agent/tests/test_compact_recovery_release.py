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
from agent_py_agent.agent.conversation.compact_checkpoint import committed_compact_checkpoint_chain
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


def test_released_history_does_not_shrink_the_recorded_before_size(tmp_path, monkeypatch):
    # 2026-09-26 真机：决定摘要后先解绑旧历史、再量“压缩前”大小，checkpoint 记成只剩系统提示和工具的 35,915
    # （实际约 31.3 万），比压后还小。锁定：记录的是解绑前完整旧请求的计量，与自动判定同一口径。
    fixture = _history_request(tmp_path, history_repeat=3000)
    measured = []
    original_measure = recovery_core._full_request_tokens
    monkeypatch.setattr(recovery_core, "_full_request_tokens",
                        lambda *args: measured.append(original_measure(*args)) or measured[-1])
    seen = []

    def on_business(wire):
        if not seen:
            seen.append("overflow")
            raise ProviderContextWindowError("测试供应商上下文溢出")
        seen.append("sent")

    fake_http(monkeypatch, fixture, on_business=on_business)
    request_execution._run_gateway_ask(fixture.context)
    current = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    checkpoint = committed_compact_checkpoint_chain(fixture.agent, current)[-1]
    assert len(measured) == 1 and checkpoint["projected_tokens_before"] == measured[0]
    assert checkpoint["projected_tokens_before"] > checkpoint["projected_tokens_after"]


@pytest.mark.parametrize('forced', [False, True])
@pytest.mark.parametrize('with_seed', [False, True])
def test_candidate_capacity_failure_can_only_continue_after_real_trim_under_original_trigger(monkeypatch, forced, with_seed):
    from dataclasses import dataclass
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history
    from agent_py_agent.agent.backends.tool_ir import ToolResult
    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    error = ConversationCompactError('candidate oversized', code='COMPACT_CANDIDATE_TOO_LARGE',
        capacity=CompactCapacityFacts(900, 700, 200, 100, 4, 4, 400, 1))
    agent, params, _calls, snapshot = arrange(monkeypatch, error, tokens=850)
    object.__setattr__(params, "conversation_history_seed", object() if with_seed else None)
    policy = SimpleNamespace(context_window_tokens=1000, trigger_tokens=700, recovery_target_tokens=600)
    host = SimpleNamespace(agent=agent, source=SimpleNamespace(policy=policy, messages=[], compact_context=object()), calibration=None,
                           force=forced, degraded=False, resolved_input=None)

    @dataclass(frozen=True)
    class FrozenRequest:
        prompt_input: object = object()
        provider_history_messages: tuple = ()
        compact_degradation_stubs: tuple = ()

    monkeypatch.setattr(recovery_core, '_native_provider_history_messages', lambda *a, **k: [])
    monkeypatch.setattr(recovery_core, 'render_prepared_prompt', lambda *a: 'system')
    original_ir = list(params.tool_ir_history)
    assert recovery_core._restore_optional_preflight(host, params, FrozenRequest(), 850, error)
    assert host.degraded and host.resolved_input is not None
    assert host.degraded_capacity is error.capacity
    assert snapshot(agent, params, 'system').raw_estimated_tokens < 700
    assert params.tool_ir_history == original_ir
    _, projected = project_native_prompt_history(params, 'system')
    assert [v for v in projected if isinstance(v, ToolResult)][-4:] == [
        v for v in original_ir if isinstance(v, ToolResult)][-4:]


def test_capacity_failure_with_fixed_floor_above_user_limit_is_not_locally_trimmed(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    error = ConversationCompactError('fixed floor', code='COMPACT_CANDIDATE_TOO_LARGE',
        capacity=CompactCapacityFacts(950, 700, 50, 710, 4, 4, 100, 1))
    agent, params, calls, _ = arrange(monkeypatch, error, tokens=850)
    host = SimpleNamespace(agent=agent, source=None, calibration=None, force=False, degraded=False)
    assert not recovery_core._restore_optional_preflight(host, params, None, 850, error)
    assert not params.compact_degradation_stubs and not host.degraded and calls == []


def test_capacity_report_keeps_real_budget_and_prior_tool_stage_without_plaintext(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.agent.runtime_errors import runtime_error_report
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    original = ConversationCompactError('retained history floor', code='COMPACT_CANDIDATE_TOO_LARGE',
        capacity=CompactCapacityFacts(820, 700, 60, 510, 4, 4, 250, 1))
    agent, params, _, _ = arrange(monkeypatch, original)
    host = SimpleNamespace(agent=agent, source=SimpleNamespace(policy=SimpleNamespace(
        context_window_tokens=1000, trigger_tokens=700, recovery_target_tokens=600)), calibration=None, force=True)
    error = recovery_core._recovery_failure(host, params, 900, original)
    report = runtime_error_report(error)
    assert report['compact_capacity']['candidate_tokens'] == 820
    facts = report['compact_recovery']
    assert facts['current_tokens'] == 900 and facts['trigger_tokens'] == 700
    assert facts['context_window_tokens'] == facts['physical_input_ceiling_tokens'] == 1000
    assert facts['prior_tool_results'] == 10 and facts['next_request_sent'] is False
    assert facts['compact_committed'] is False and facts['prior_tools_replayed'] is False
    assert 'memory_compact_auto_trigger_max_tokens' in report['message']
    assert 'memory_compact_trigger_percent' in report['message']
    assert 'file0.txt' not in str(report) and 'x' * 100 not in str(report)


def test_candidate_degradation_latched_next_step_cannot_revert_to_wider_physical_limit(monkeypatch):
    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    agent, params, _calls, _ = arrange(monkeypatch,
        ConversationCompactError('candidate', code='COMPACT_CANDIDATE_TOO_LARGE'), tokens=900)
    with pytest.raises(ConversationCompactError) as error:
        service._continue_after_compact_failure(agent, params, lambda: 750,
            reason='COMPACT_CANDIDATE_TOO_LARGE', force=False, fresh_failure=False)
    assert error.value.code == 'COMPACT_BUDGET_EXCEEDED'
    assert not params.compact_degradation_stubs


def test_compact_public_error_projection_rejects_non_scalar_forged_capacity():
    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.agent.runtime_errors import runtime_error_report

    capacity = CompactCapacityFacts('private prompt', 100000, 100, None, 4, 4, 500, 1)
    report = runtime_error_report(ConversationCompactError('capacity', code='COMPACT_CANDIDATE_TOO_LARGE', capacity=capacity))
    assert 'candidate_tokens' not in report['compact_capacity']
    assert report['compact_capacity']['input_ceiling_tokens'] == 100000
    assert 'private prompt' not in str(report)


@pytest.mark.parametrize('messages,context', [(None, object()), ([object()], object()), ([], None)])
def test_missing_seed_cannot_restore_unknown_or_nonempty_transcript(monkeypatch, messages, context):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    error = ConversationCompactError('candidate', code='COMPACT_CANDIDATE_TOO_LARGE',
        capacity=CompactCapacityFacts(900, 700, 100, 100, 4, 4, 400, 1))
    agent, params, _, _ = arrange(monkeypatch, error, tokens=850)
    host = SimpleNamespace(agent=agent, source=SimpleNamespace(messages=messages, compact_context=context),
                           calibration=None, force=False, degraded=False)
    assert not recovery_core._restore_optional_preflight(host, params, None, 850, error)
    assert not params.compact_degradation_stubs and not host.degraded


def test_gateway_failure_keeps_compact_exact_budget_in_user_notice(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.agent.gateway_parts.request_execution import _apply_gateway_request_failure
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    original = ConversationCompactError('candidate', code='COMPACT_CANDIDATE_TOO_LARGE',
        capacity=CompactCapacityFacts(120000, 100000, 7000, 50000, 4, 4, 60000, 1))
    agent, params, _, _ = arrange(monkeypatch, original)
    host = SimpleNamespace(agent=agent, source=SimpleNamespace(policy=SimpleNamespace(
        context_window_tokens=128000, trigger_tokens=100000, recovery_target_tokens=75000)), calibration=None, force=True)
    error = recovery_core._recovery_failure(host, params, 115000, original)
    response = {}
    _apply_gateway_request_failure(agent, {'request': {}}, response, error)
    assert response['compact_capacity']['candidate_tokens'] == 120000
    assert response['compact_recovery']['next_request_sent'] is False
    assert '触发100000' in response['user_error']
    assert 'memory_compact_auto_trigger_max_tokens' in response['user_error']
    assert 'memory_compact_trigger_percent' in response['user_error']


def test_local_trim_updates_host_attached_projection_without_changing_canonical_ir(monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.agent_core import _tool_loop_service as service
    from agent_py_agent.agent.agent_core.tool_ir_history import project_native_prompt_history
    from agent_py_agent.agent.backends.tool_ir import ToolResult
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    agent, params, _, _ = arrange(monkeypatch,
        ConversationCompactError('candidate', code='COMPACT_CANDIDATE_TOO_LARGE'), tokens=850)
    original = [item.with_live_prompt_projection(item.render_for_prompt()) if isinstance(item, ToolResult) else item
                for item in params.tool_ir_history]
    params = replace(params, tool_ir_history=original)

    def estimate():
        _, history = project_native_prompt_history(params, 'system')
        return 850 - (sum(len(item.render_for_model_prompt()) for item in original if isinstance(item, ToolResult))
                      - sum(len(item.render_for_model_prompt()) for item in history if isinstance(item, ToolResult))) // 10

    assert service._continue_after_compact_failure(agent, params, estimate,
        reason='COMPACT_CANDIDATE_TOO_LARGE', force=True, input_ceiling_tokens=700) > 0
    assert estimate() < 700
    assert params.tool_ir_history == original
    _, projected = project_native_prompt_history(params, 'system')
    raw_results = [item for item in original if isinstance(item, ToolResult)]
    sent_results = [item for item in projected if isinstance(item, ToolResult)]
    assert sent_results[-4:] == raw_results[-4:]
    assert all(raw.refs == sent.refs and raw.status == sent.status
               for raw, sent in zip(raw_results, sent_results, strict=True))
    assert any('TOOL_OUTPUT_COMPACTED' in item.render_for_model_prompt() for item in sent_results[:-4])


def test_capacity_notice_does_not_suggest_trigger_raise_when_physical_window_is_also_exceeded(monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.compact_guard import CompactCapacityFacts
    from agent_py_agent.tests.test_wave9_mid_loop_compact_continuation import arrange

    original = ConversationCompactError('candidate', code='COMPACT_CANDIDATE_TOO_LARGE',
        capacity=CompactCapacityFacts(1200, 700, 200, 900, 4, 4, 250, 1))
    agent, params, _, _ = arrange(monkeypatch, original)
    host = SimpleNamespace(agent=agent, source=SimpleNamespace(policy=SimpleNamespace(
        context_window_tokens=1000, trigger_tokens=700, recovery_target_tokens=600)), calibration=None, force=True)
    error = recovery_core._recovery_failure(host, params, 950, original)
    assert '候选也超物理输入门' in str(error)
    assert '不要虚增 model_context_window_tokens' in str(error)
    assert '不能仅提高触发线' in str(error)
    assert '下一步：管理员调整 memory_compact_auto_trigger_max_tokens' not in str(error)


@pytest.mark.parametrize('outcome', ['ok', 'context_overflow'])
def test_background_degraded_attempt_is_accepted_once_or_stops_without_replaying_candidate(tmp_path, monkeypatch, outcome):
    from types import SimpleNamespace

    from agent_py_agent.agent.conversation.compact_guard import (
        CompactCapacityFacts,
        CompactRecoveryFacts,
    )
    from agent_py_agent.agent.runtime_errors import runtime_error_report
    from agent_py_agent.tests.test_background_compact_recovery import _background

    _agent, store, thread, request, execution, sink = _background(
        tmp_path, backend='anthropic_compatible', detached=True, with_history=False)
    attempts, persisted = [], []
    facts = CompactRecoveryFacts(115000, 100000, 75000, 128000, 111616, 16384, 16384, 8, True)
    capacity = CompactCapacityFacts(120000, 100000, 1000, 33000, 4, 4, 50000, 1)
    recovery = SimpleNamespace(committed=False, degraded=True, degraded_capacity=capacity, degraded_recovery_facts=facts)
    result = SimpleNamespace(runtime_status=outcome)

    def attempt(_execution, _prompt, params, *_args, **_kwargs):
        attempts.append(params.request_id)
        return result, params, recovery

    monkeypatch.setattr(background_execution, '_run_background_recovery_attempt', attempt)
    monkeypatch.setattr(background_execution, '_persist_background_native_turn', lambda *args: persisted.append(args))
    def run():
        return background_execution.run_background_turn_with_compact(
            execution, thread, request, user_prompt='继续合成任务', continuation_injection=[],
            proactive_delivery_available=False, activity_sink=sink)
    if outcome == 'ok':
        assert run() is result and len(persisted) == 1
    else:
        with pytest.raises(ConversationCompactError) as failed:
            run()
        assert failed.value.code == 'COMPACT_LOCAL_RECOVERY_EXHAUSTED'
        assert failed.value.capacity is capacity
        report = runtime_error_report(failed.value)
        assert 'next_request_sent' not in report['compact_recovery']
        assert persisted == []
    assert len(attempts) == 1
    assert store.threads.require(thread.thread_id).compact_generation == 0
