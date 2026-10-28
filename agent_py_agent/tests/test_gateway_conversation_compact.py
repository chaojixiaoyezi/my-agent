# LLM: 仅用隔离会话库和 fake provider 验证 Gateway Compact；重试读取结构化响应，不能执行工具或放宽提交边界。
# 模块用途: 检查压缩摘要、原文保留和有界空答修复，精确记录模型输入与诊断字段。
from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._tool_loop_service import _text_conversation_history_section
from agent_py_agent.agent.backends.anthropic_prompt_cache import (
    anthropic_messages_with_optional_cache,
    anthropic_prompt_cache_projection,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.backends.errors import ProviderResponseError
from agent_py_agent.agent.conversation import compact_request_budget as budget_module
from agent_py_agent.agent.conversation.compact import (
    ConversationCompactOptions,
    _merge_compact_operation_evidence,
    _projected_context_tokens,
    _summarize,
    _summary_content,
    inspect_conversation_context,
    prepare_conversation_context,
    render_conversation_context_usage,
)
from agent_py_agent.agent.conversation.compact_guard import (
    ConversationCompactCircuitOpenError,
    ConversationCompactError,
    compact_exception_code,
    split_recent_complete_turns,
)
from agent_py_agent.agent.conversation.history_seed import seed_text_messages
from agent_py_agent.agent.conversation.models import MessageLogEntry
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.request_context import (
    GatewayAskRunContext,
    GatewayConversationContext,
    GatewayConversationLoadRequest,
    gateway_conversation_context,
)
from agent_py_agent.agent.gateway_parts.request_execution import (
    _gateway_run_params,
    _GatewayRunParamsRequest,
)
from agent_py_agent.agent.gateway_parts.request_history import append_gateway_conversation_message
from agent_py_agent.agent.gateway_parts.request_prompt import (
    _conversation_prompt_section,
    _prompt_operation_evidence,
    gateway_conversation_history_seed,
)
from agent_py_agent.agent.memory_store import MemoryRecord
from agent_py_agent.agent.prompting_parts.cache_layout import prompt_cache_layout
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests._gateway_history_helpers import context_history


# LLM: 真实Gateway/恢复宿主/来源与provider builder，仅摘要故障和完整请求估算注入；摘要期不额外保存旧历史。
# 函数用途: 在已压缩线程上触发可选预检瞬断后，重建尾历史且不重复已应用的压缩摘要。
@pytest.mark.parametrize("failure", ["transient", "timeout", "circuit"])
def test_p2a_optional_preflight_failure_restores_seed_and_completes_turn(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
    from agent_py_agent.agent.backends.errors import ProviderTimeoutError, ProviderTransientError
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.gateway_parts import request_context, request_execution
    from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
    from agent_py_agent.tests.test_gateway_model_adoption import fake_http

    fixture = _history_request(tmp_path, original_window=100_000, history_repeat=1)
    fixture.agent.config.memory_compact_trigger_percent = 90
    monkeypatch.setattr(compact, "_summarize", lambda *a, **k: "已提交的第一代摘要")
    prepare_conversation_context(
        fixture.agent, fixture.agent.conversation_store,
        fixture.agent.conversation_store.threads.require(fixture.thread_id),
        options=ConversationCompactOptions(current_prompt="预压缩", force=True),
    )
    seeded = request_context.gateway_conversation_context(request_context.GatewayConversationLoadRequest(
        fixture.agent, fixture.context.request, fixture.context.request_id, fixture.context.request["prompt"],
    ))
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            fixture.agent, {}, seeded, request_id="post-compact-" + role, role=role, content="压后新增的原始资料",
        )
    state, summaries = {}, []
    original = recovery.PreparedCompactRecovery.select
    monkeypatch.setattr(recovery, "projected_model_context_components", lambda *a, **k: (95_000, {}))

    # LLM: 只持有原参数对象，不缓存或复制provider历史；进入摘要前后交给真实select裁决。
    # 函数用途: 记录可选恢复是否解绑、是否恢复以及是否错误提交候选。
    def select(self, agent, params, prompt):
        state["params"] = params
        state["before"] = len(params.provider_history_messages)
        assert not self.force and self.source.policy.trigger_tokens == 90_000
        value = original(self, agent, params, prompt)
        state["after"] = len(params.provider_history_messages)
        state["resolved"] = len(self.resolved_input.provider_history_messages)
        state["committed"] = self.committed
        return value

    # LLM: 业务HTTP仍走真实provider builder；只让本次摘要请求瞬断，旧历史必须已释放。
    # 函数用途: 在摘要期间验证原参数为空后抛供应商瞬时错误。
    def summarize(*args, **kwargs):
        assert state["params"].__dict__["provider_history_messages"] == []
        summaries.append(True)
        if failure == "timeout":
            raise ProviderTimeoutError("summary timeout", stage="first_event")
        if failure == "circuit":
            raise ConversationCompactCircuitOpenError("summary cooldown", code="COMPACT_CIRCUIT_OPEN")
        raise ProviderTransientError("test summary temporarily unavailable")

    monkeypatch.setattr(recovery.PreparedCompactRecovery, "select", select)
    monkeypatch.setattr(compact, "_summarize", summarize)
    business, _ = fake_http(monkeypatch, fixture)
    result = request_execution._run_gateway_ask(fixture.context)
    assert result.response == "资料整理完成。" and len(business) == 1 and summaries == [True]
    assert state["before"] > 0 and state["before"] == state["after"] == state["resolved"]
    assert not state["committed"]
    wire = json.dumps(business[0][0]["messages"], ensure_ascii=False)
    assert wire.count("Earlier Conversation Summary (generation 1)") == 1
    assert "已提交的第一代摘要" in wire and "压后新增的原始资料" in wire
    thread = fixture.agent.conversation_store.threads.require(fixture.thread_id)
    assert thread.compact_generation == 1 and thread.compact_consecutive_failures == 1


# LLM: 仍经真实Gateway恢复入口；仅在摘要阶段注入结构错误/取消/不安全载体，不允许可选降级掩盖合同问题。
# 函数用途: 锁住结构/坏账/未知结果错误、精确窗口上限、强制模式、无种子和中毒历史的拒恢复边界。
@pytest.mark.parametrize("failure", [
    "COMPACT_SOURCE_CHANGED", "COMPACT_REQUEST_PROJECTION_UNKNOWN", "COMPACT_REQUEST_PROJECTION_CHANGED",
    "COMPACT_TOOL_COVERAGE_UNKNOWN", "COMPACT_REQUEST_NON_TEXT", "uncertain", "corruption",
    "ceiling", "force", "no-seed", "poison", "cancel", "token",
])
def test_p2a_preflight_does_not_mask_hard_failures(tmp_path, monkeypatch, failure):
    from agent_py_agent.agent.agent_core import compact_request_recovery as recovery
    from agent_py_agent.agent.backends.errors import ProviderTransientError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.conversation import compact
    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.runtime_errors import DataCorruptionError
    from agent_py_agent.tests.test_compact_recovery_release import _PoisonedHistory
    from agent_py_agent.tests.test_gateway_compact_recovery import _history_request
    from agent_py_agent.tests.test_gateway_model_adoption import fake_http

    fixture = _history_request(tmp_path, original_window=100_000, history_repeat=1)
    fixture.agent.config.memory_compact_trigger_percent = 90
    state, reads, summaries = {}, [], []
    original = recovery.PreparedCompactRecovery.select
    tokens = 95_904 if failure == "ceiling" else 95_000
    monkeypatch.setattr(recovery, "projected_model_context_components", lambda *a, **k: (tokens, {}))

    # LLM: 不持有旧历史副本；强制模式覆盖发生在正式select前，其余仍为可选预压缩。
    # 函数用途: 保存本次参数供摘要故障注入，不改变真实解绑路径。
    def select(self, agent, params, prompt):
        state["params"] = params
        if failure == "force":
            self.force = True
        return original(self, agent, params, prompt)

    # LLM: 仅在旧历史已经解绑后故障；中毒列表的任何读取都会另记reads并报错。
    # 函数用途: 提供必须终止业务的结构错误、容量或停止边界。
    def summarize(*args, **kwargs):
        params = state["params"]
        assert params.__dict__["provider_history_messages"] == []
        summaries.append(True)
        if failure.startswith("COMPACT_"):
            raise ConversationCompactError("structural failure", code=failure)
        if failure == "uncertain":
            raise RequestExecutionOutcomeUncertainError(
                {"id": "req-1", "execution_attempt_id": "att-1", "lease_epoch": 1, "lease_owner": "w1"},
                "compact_commit",
            )
        if failure == "corruption":
            raise DataCorruptionError("corrupted compact ledger")
        if failure == "cancel":
            raise InterruptedError("summary cancelled")
        if failure == "token":
            params.cancellation_token.cancel("summary cancelled")
        if failure == "no-seed":
            object.__setattr__(params, "conversation_history_seed", None)
        if failure == "poison":
            object.__setattr__(params, "provider_history_messages", _PoisonedHistory(reads))
        raise ProviderTransientError("summary temporarily unavailable")

    monkeypatch.setattr(recovery.PreparedCompactRecovery, "select", select)
    monkeypatch.setattr(compact, "_summarize", summarize)
    business, _ = fake_http(monkeypatch, fixture)
    with pytest.raises((RuntimeError, InterruptedError)):
        request_execution._run_gateway_ask(fixture.context)
    assert summaries == [True] and business == [] and reads == []
    assert fixture.agent.conversation_store.threads.require(fixture.thread_id).compact_generation == 0


# LLM: 限定测试身份定位和外部活跃请求检查，车道、来源冻结、真实压缩及失败账仍走正式控制入口。
# 函数用途: 确认用户手动/compact在自动熔断期间仍能成功并清零。
def test_p2a_manual_control_bypasses_automatic_cooldown(tmp_path, monkeypatch):
    import time

    from agent_py_agent.agent.gateway_parts import control_service as control

    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    context = _context(agent, _request(), "manual-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(agent, {}, context, request_id=f"old-{role}", role=role, content="原始资料")
    for _ in range(3):
        agent.conversation_store.threads.record_compact_failure(
            context.thread_id, failure_code="COMPACT_TEST_FAILURE", expected_generation=0, now=time.time())
    thread = agent.conversation_store.threads.require(context.thread_id)
    monkeypatch.setattr(control, "_live_window_request", lambda *a: None)
    monkeypatch.setattr(control, "_request_agent_for_scope", lambda *a: agent)
    monkeypatch.setattr(control, "_conversation_thread_for_scope", lambda *a: thread)
    result = control._execute_registered_compact_control(agent, None, SimpleNamespace(value=""), SimpleNamespace())
    assert result.ok, (result.error_code, result)
    stored = agent.conversation_store.threads.require(context.thread_id)
    assert stored.compact_generation == 1 and stored.compact_consecutive_failures == 0


# LLM: 只观察真实车道请求的取消回调，用假单调时钟避免睡眠；用户停止始终优先，不触及锁实现。
# 函数用途: 确认手动车道允许五秒短暂争用，精确五秒和用户停止都能中断排队。
def test_p2a_manual_compact_lane_allows_five_seconds(monkeypatch):
    from agent_py_agent.agent.gateway_parts import control_service as control

    now, captured, stopped = [100.0], [], [False]
    marker = object()
    monkeypatch.setattr(control.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(control, "is_interrupted", lambda: stopped[0])
    monkeypatch.setattr(control, "conversation_run_lane", lambda request: captured.append(request) or marker)
    assert control._manual_compact_lane(SimpleNamespace(), object(), "thread") is marker
    check = captured[0].interrupt_check
    now[0] = 104.9
    assert not check()
    stopped[0] = True
    assert check()
    stopped[0] = False
    now[0] = 105.0
    assert check()

# LLM: These tests intentionally use tiny policy thresholds to exercise checkpoint/tail/CAS
# behavior, while the fake provider has no wire limit. Physical request budgeting is tested with
# explicit small windows and complete source coverage in test_compact_request_budget.
# 函数用途: 隔离旧会话状态测试的人工触发线和 fake 后端容量，避免完整工具表大于玩具窗口。

# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 为摘要测试固定本地provider容量观察，不接真实供应商网络。
@pytest.fixture(autouse=True)
def _fake_summary_provider_capacity(monkeypatch):
    from agent_py_agent.agent.conversation import compact_request_budget

    monkeypatch.setattr(compact_request_budget, "resolve_model_context_window_tokens", lambda _agent: 1_000_000)


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# Test helper: production transcript Compact now uses the same native provider surface as a normal
# turn, so summary fakes must explicitly declare native-tool capability instead of taking a text path.
# 类用途: 提供声明原生工具能力的摘要假后端，避免测试退回文本旁路。
class _NativeSummaryBackend:
    name = "native-summary-test"

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 为摘要假后端报告原生工具能力，不探测真实供应商。
    def probe_tool_capability(self) -> ProviderToolCapability:
        return ProviderToolCapability(
            provider=self.name,
            endpoint="local://conversation-compact-test",
            model="test-model",
            stream=False,
            native_supported=True,
            evidence="test_backend_declares_native_tools",
        )


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 类用途: 返回可控合法摘要并记录请求，供真实压缩管线回归。
class _SummaryBackend(_NativeSummaryBackend):
    name = "summary-test"

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 初始化假摘要后端的调用记录，不创建网络连接。
    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 记录摘要请求并返回本地可控结果。
    def generate(self, prompt: str, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        self.kwargs.append(dict(kwargs))
        return ModelResponse(
            text="用户的暗号是紫藤；较早工作已经讨论，仍需继续后续步骤。",
            backend=self.name,
        )


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 类用途: 返回与操作证据冲突的假摘要，测试证据权威性。
class _ConflictingSummaryBackend(_NativeSummaryBackend):
    name = "conflicting-summary-test"

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 返回特意矛盾的本地摘要，供证据优先级断言使用。
    def generate(self, prompt: str, **_kwargs) -> ModelResponse:
        return ModelResponse(
            text="助手已经成功删除海王星项目记忆。",
            backend=self.name,
        )


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 类用途: 返回超预算假摘要，验证候选接受门。
class _OversizedSummaryBackend(_NativeSummaryBackend):
    name = "oversized-summary-test"

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 初始化假后端的响应预算和调用状态。
    def __init__(self) -> None:
        self.calls = 0

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 返回本地超预算摘要，驱动真实候选接受门。
    def generate(self, prompt: str, **_kwargs) -> ModelResponse:
        self.calls += 1
        return ModelResponse(text="过大的摘要" * 20_000, backend=self.name)


# LLM: 第一份响应固定为空，后续响应由测试显式脚本提供；第三次请求会耗尽脚本，不能静默重复。
# 类用途: 记录实际摘要请求和返回对象，区分持续空答与一次修复成功，不访问网络。
class _EmptySummaryBackend(_SummaryBackend):
    name = "empty-summary-test"

    # LLM: 只初始化本用例内的响应脚本，默认恰好两份空答；记录沿原 fake 的请求捕获逻辑。
    # 函数用途: 准备一次空摘要及唯一补发的可观察结果。
    def __init__(self, texts: tuple[str, ...] = ("", "")) -> None:
        super().__init__()
        self._texts = iter(texts)
        self.responses: list[ModelResponse] = []

    # LLM: 保留父 fake 记录的完整请求，只替换返回正文；响应列表用于证明重试前后的实际结果。
    # 函数用途: 返回下一份脚本摘要，副作用只限本测试的内存记录。
    def generate(self, prompt: str, **kwargs) -> ModelResponse:
        response = replace(super().generate(prompt, **kwargs), text=next(self._texts))
        self.responses.append(response)
        return response


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 类用途: 在假摘要响应里带工具调用，验证压缩链不会执行它。
class _ToolCallingSummaryBackend(_SummaryBackend):
    name = "tool-calling-summary-test"

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 返回含工具调用的假摘要响应，验证不会执行该调用。
    def generate(self, prompt: str, **kwargs) -> ModelResponse:
        self.calls += 1
        self.prompts.append(prompt)
        self.kwargs.append(dict(kwargs))
        return ModelResponse(
            text="我将调用工具后再总结。",
            backend=self.name,
            tool_use_blocks=[
                {
                    "id": "compact-tool-call-must-not-run",
                    "name": "write_file",
                    "input": {
                        "path": "compact-tool-must-not-run.txt",
                        "content": "unsafe",
                    },
                }
            ],
        )


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩失败保留真实的结构化provider错误码。
def test_compact_failure_preserves_typed_provider_error_code() -> None:
    assert compact_exception_code(
        ProviderResponseError(
            "provider payload was empty",
            error_code="MODEL_EMPTY_RESPONSE",
        )
    ) == "COMPACT_MODEL_EMPTY_RESPONSE"
    assert compact_exception_code(
        ProviderResponseError("provider payload was malformed")
    ) == "COMPACT_PROVIDERRESPONSEERROR"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证会话压力统计不预扣尚未发生的未来输出。
def test_conversation_pressure_does_not_reserve_unspent_future_output() -> None:
    prompts = SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    small_output = SimpleNamespace(prompts=prompts, config=SimpleNamespace(max_tokens=64))
    large_output = SimpleNamespace(prompts=prompts, config=SimpleNamespace(max_tokens=64_000))

    small_projection = _projected_context_tokens(
        small_output,
        "较早摘要",
        [],
        "当前消息",
    )
    large_projection = _projected_context_tokens(
        large_output,
        "较早摘要",
        [],
        "当前消息",
    )

    assert small_projection == large_projection


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证近期操作证据进入真实会话容量投影。
def test_conversation_projection_counts_recent_operation_evidence() -> None:
    agent = SimpleNamespace(
        prompts=SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    )
    without_evidence = _projected_context_tokens(
        agent,
        "较早摘要",
        [],
        "当前消息",
    )
    with_evidence = _projected_context_tokens(
        agent,
        "较早摘要",
        [],
        "当前消息",
        recent_operation_evidence={
            "schema": "operation_verification.public.v1",
            "groups": [{"tool": "write_file", "status": "succeeded"}] * 100,
        },
    )

    assert with_evidence > without_evidence


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证真正压缩会统计并摘要终态工具折叠。
def test_true_compact_counts_and_summarizes_terminal_tool_fold() -> None:
    backend = _SummaryBackend()
    agent = SimpleNamespace(
        prompts=SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文"),
        backend=backend,
    )
    plain_row = MessageLogEntry(
        message_id="msg-plain",
        thread_id="thread-fold",
        role="assistant",
        content="本轮完成。",
    )
    folded_row = MessageLogEntry(
        message_id="msg-folded",
        thread_id="thread-fold",
        role="assistant",
        content="本轮完成。",
        metadata={
            "terminal_tool_fold": {
                "schema": "conversation_terminal_tool_fold.v1",
                "tool_call_count": 1,
                "successful_tool_call_count": 1,
                "non_successful_tool_call_count": 0,
                "text": (
                    "[conversation-terminal-tool-fold]\n"
                    "- tool_call_count: 1\n"
                    "- ordered_tool_index:\n"
                    "  - 1: tool=write_file status=ok ref=call-folded-write"
                ),
            }
        },
    )

    plain_tokens = _projected_context_tokens(agent, "", [plain_row], "继续")
    folded_tokens = _projected_context_tokens(agent, "", [folded_row], "继续")
    _summarize(agent, "", {}, [folded_row])

    assert folded_tokens > plain_tokens
    assert len(backend.prompts) == 1
    assert "terminal_tool_fold" in backend.prompts[0]
    assert "call-folded-write" in backend.prompts[0]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩只读一个活动v2终态工具折叠投影。
def test_true_compact_reads_only_one_active_v2_terminal_tool_fold_projection() -> None:
    row = MessageLogEntry(
        message_id="msg-folded-v2",
        thread_id="thread-fold-v2",
        role="assistant",
        content="本轮完成。",
        metadata={
            "terminal_tool_fold": {
                "schema": "conversation_terminal_tool_fold.v2",
                "tool_call_count": 1,
                "successful_tool_call_count": 1,
                "non_successful_tool_call_count": 0,
                "text": "[conversation-terminal-tool-fold]\n- projection: cold_fold",
                "hot_text": "[conversation-terminal-tool-fold]\n- projection: hot_tail",
                "fold_after_epoch": 9_999_999_999,
            }
        },
    )

    structured = _summary_content(row)
    terminal_fold = structured["terminal_tool_fold"]

    assert terminal_fold["projection"] == "hot_tail"
    assert "projection: hot_tail" in terminal_fold["text"]
    assert "hot_text" not in terminal_fold
    assert "fold_after_epoch" not in terminal_fold


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证只读上下文检查采用自动压缩策略且不写状态。
def test_context_inspection_uses_the_automatic_compact_policy_without_writing(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    request = _request("ou_context")
    context = _context(agent, request, "gw-create", "开始")
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-context-user",
        role="user",
        content="这是一条尚未压缩的消息",
    )
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None

    usage = inspect_conversation_context(agent, agent.conversation_store, thread)
    unchanged = agent.conversation_store.threads.load(context.thread_id)
    rendered = render_conversation_context_usage(
        usage,
        model_name="MiniMax-M2.7",
    )

    assert usage.context_window_tokens == 20_000
    assert usage.trigger_percent == 50
    assert usage.trigger_tokens == 10_000
    assert usage.pending_messages == 1
    assert usage.compact_generation == 0
    assert unchanged is not None and unchanged.compact_generation == 0
    assert "20,000 tokens" in rendered
    assert "50%" in rendered
    assert "未压缩消息 1 条" in rendered


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证终态工具折叠能进入下一轮而不冒增压缩代数。
def test_terminal_tool_fold_reaches_next_turn_without_incrementing_compact(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    request = _request("ou_fold")
    context = _context(agent, request, "gw-create", "开始")
    fold = {
        "schema": "conversation_terminal_tool_fold.v1",
        "tool_call_count": 2,
        "successful_tool_call_count": 2,
        "non_successful_tool_call_count": 0,
        "text": (
            "[conversation-terminal-tool-fold]\n"
            "- tool_call_count: 2\n"
            "- ordered_tool_index:\n"
            "  - 1: tool=read_file status=ok ref=call-read\n"
            "  - 2: tool=write_file status=ok ref=call-write"
        ),
    }
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-fold-user",
        role="user",
        content="检查并修改文件",
    )
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-fold-assistant",
        role="assistant",
        content="文件已经修改。",
        terminal_tool_fold=fold,
    )

    followup = _context(agent, request, "gw-followup", "继续检查")
    replayed_followup = _context(agent, request, "gw-followup-replay", "继续检查")
    thread = agent.conversation_store.threads.load(context.thread_id)
    usage = inspect_conversation_context(agent, agent.conversation_store, thread)
    rendered = render_conversation_context_usage(usage, model_name="MiniMax-M2.7")

    assert followup.compact_generation == 0
    assert context_history(followup)[-1][0] == "assistant"
    assert context_history(followup)[-1][1].startswith("文件已经修改。")
    assert "conversation-terminal-tool-fold" in context_history(followup)[-1][1]
    assert "call-write" in context_history(followup)[-1][1]
    assert context_history(replayed_followup) == context_history(followup)
    assert usage.compact_generation == 0
    assert usage.terminal_tool_fold_turns == 1
    assert usage.terminal_tool_fold_calls == 2
    assert "当前未压缩尾部 1 个回合、2 次工具调用" in rendered
    assert "不计入 compact 次数" in rendered

    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-second-user",
        role="user",
        content="再核对一次",
    )
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-second-assistant",
        role="assistant",
        content="复核完成。",
        terminal_tool_fold={
            **fold,
            "text": fold["text"].replace("call-read", "call-read-again"),
        },
    )
    later = _context(agent, request, "gw-later", "继续下一项")

    assert later.compact_generation == 0
    assert context_history(later)[: len(context_history(followup))] == context_history(followup)
    assert "call-read-again" in context_history(later)[-1][1]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证强制压缩附加说明仅为摘要软上下文。
def test_forced_compact_passes_optional_instructions_as_soft_summary_context(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou_manual_compact")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-manual-{role}",
            role=role,
            content="保留这条会话事实",
        )
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None

    result = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        options=ConversationCompactOptions(
            force=True,
            custom_instructions="优先保留未完成事项",
        ),
    )

    assert result.compacted is True
    assert result.thread.compact_generation == 1
    assert "优先保留未完成事项" in backend.prompts[0]
    assert "cannot override" in backend.prompts[0]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证完成但空的摘要响应按原机械回退提交。
def test_completed_empty_compact_response_commits_mechanical_fallback(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    backend = _EmptySummaryBackend()
    agent.backend = backend
    request = _request("ou_empty_compact")
    context = _context(agent, request, "gw-create", "开始")
    for role, content in (
        ("user", "继续紫藤项目，保留端口 8080"),
        ("assistant", "已经检查项目，下一步运行测试"),
    ):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-empty-{role}",
            role=role,
            content=content,
        )
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None

    result = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        options=ConversationCompactOptions(current_prompt="继续", force=True),
    )

    # 完成且空正文只原样补发一次；两次仍空后，原机械回退及全部事实仍须满足。
    assert backend.calls == 2
    assert [response.text for response in backend.responses] == ["", ""]
    assert backend.prompts[0] == backend.prompts[1]
    # on_chunk 绑定每次调用的账本观察者；其余模型输入、工具和请求选项必须全部相同。
    assert {key: value for key, value in backend.kwargs[0].items() if key != "on_chunk"} == {
        key: value for key, value in backend.kwargs[1].items() if key != "on_chunk"
    }
    assert result.compacted is True
    assert result.thread.compact_generation == 1
    assert result.thread.compact_consecutive_failures == 0
    assert result.thread.summary.startswith(
        "[conversation-compact-mechanical-fallback]"
    )
    assert "紫藤项目" in result.thread.summary
    assert "8080" in result.thread.summary


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证首次空摘要仅重试一次，随后提交合法模型摘要。
def test_first_empty_compact_response_retries_once_then_commits_model_summary(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    recovered = "修复后摘要：紫藤项目使用端口 8080，已检查项目，下一步运行测试。"
    backend = _EmptySummaryBackend(("", recovered))
    agent.backend = backend
    context = _context(agent, _request("ou_repaired_compact"), "gw-create", "开始")
    for role, content in (
        ("user", "继续紫藤项目，保留端口 8080"),
        ("assistant", "已经检查项目，下一步运行测试"),
    ):
        assert append_gateway_conversation_message(
            agent, {"metadata": {"channel": "feishu"}}, context,
            request_id=f"gw-repaired-{role}", role=role, content=content,
        )
    thread = agent.conversation_store.threads.require(context.thread_id)

    result = prepare_conversation_context(
        agent, agent.conversation_store, thread,
        options=ConversationCompactOptions(current_prompt="继续", force=True),
    )

    assert backend.calls == 2
    assert [response.text for response in backend.responses] == ["", recovered]
    assert backend.prompts[0] == backend.prompts[1]
    assert {key: value for key, value in backend.kwargs[0].items() if key != "on_chunk"} == {
        key: value for key, value in backend.kwargs[1].items() if key != "on_chunk"
    }
    assert result.compacted is True and result.thread.compact_generation == 1
    assert result.thread.compact_consecutive_failures == 0
    assert recovered in result.thread.summary
    assert not result.thread.summary.startswith("[conversation-compact-mechanical-fallback]")


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证摘要响应中的工具调用始终不被执行。
def test_transcript_compact_tool_call_is_never_executed(tmp_path, caplog) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    backend = _ToolCallingSummaryBackend()
    agent.backend = backend
    request = _request("ou_compact_no_tools")
    context = _context(agent, request, "gw-create", "开始")
    for role, content in (
        ("user", "请保留本轮真实事实。"),
        ("assistant", "本轮已经正常完成。"),
    ):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-no-tools-{role}",
            role=role,
            content=content,
        )

    compacted = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-force", "继续"),
        force_compact=True,
    )

    assert compacted.compact_generation == 1
    # auto 和唯一 none 重试都回 tool_use 后，原机械回退保留事实；冻结前缀禁止额外分段请求。
    assert "- reason: TOOL_CALL" in compacted.compact_summary
    assert not (tmp_path / "compact-tool-must-not-run.txt").exists()
    assert backend.calls == 2
    assert backend.prompts[0] == backend.prompts[1]
    assert {key: value for key, value in backend.kwargs[0].items()
            if key not in {"on_chunk", "tools", "tool_choice", "request_options"}} == {
        key: value for key, value in backend.kwargs[1].items()
        if key not in {"on_chunk", "tools", "tool_choice", "request_options"}
    }
    original_options = backend.kwargs[0]["request_options"]
    retry_options = backend.kwargs[1]["request_options"]
    assert retry_options.system_instruction == budget_module._SUMMARY_SYSTEM_INSTRUCTION
    # none 请求移除工具后重算首包预算；它是传输等待元数据，摘要模型的其余采样选项不能改变。
    assert original_options.first_event_timeout_seconds > 0 and retry_options.first_event_timeout_seconds > 0
    assert replace(
        retry_options, system_instruction=original_options.system_instruction,
        first_event_timeout_seconds=original_options.first_event_timeout_seconds,
    ) == original_options
    assert backend.kwargs[0].get("tools")
    assert backend.kwargs[0].get("tool_choice").mode == "auto"
    assert backend.kwargs[1].get("tool_choice").mode == "none"
    assert all(not kwargs.get("tools") for kwargs in backend.kwargs[1:])
    diagnostic = [record.compact_response_shape for record in caplog.records
                  if hasattr(record, "compact_response_shape")]
    assert {item["reason"] for item in diagnostic} == {"TOOL_CALL"} and diagnostic[0]["tool_use_count"] == 1
    assert diagnostic[0]["purpose"] == "conversation_compact_summary" and diagnostic[0]["thread_id"]
    assert "unsafe" not in caplog.text and "我将调用工具后再总结" not in caplog.text


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证空摘要诊断只记录结构化形状、不泄露正文。
@pytest.mark.parametrize("thinking", [False, True])
def test_empty_transcript_summary_logs_typed_shape_without_content(caplog, thinking):
    response = ModelResponse(
        text="", backend="fake", truncated=thinking, stop_reason="max_tokens" if thinking else "end_turn",
        runtime_status="unfinished" if thinking else "ok", runtime_reason="MODEL_RESPONSE_TRUNCATED" if thinking else "",
        runtime_source="model_provider" if thinking else "", turn_end_reason="max-tokens" if thinking else "",
        assistant_content_blocks=[{"type": "thinking", "thinking": "PRIVATE-THINKING", "signature": "PRIVATE-KEY"}]
        if thinking else [],
    )
    calls = []
    agent = SimpleNamespace(backend=SimpleNamespace(generate=lambda prompt: calls.append(prompt) or response))
    row = MessageLogEntry(message_id="m", thread_id="t", role="user", content="PRIVATE-SOURCE")

    summary = _summarize(agent, "", {}, [row])

    expected_calls = 1 if thinking else 2
    assert len(calls) == expected_calls and summary.startswith("[conversation-compact-mechanical-fallback]")
    assert all(prompt == calls[0] for prompt in calls)
    records = [record.compact_response_shape for record in caplog.records if hasattr(record, "compact_response_shape")]
    assert len(records) == expected_calls
    expected_shape = {
        "reason": "EMPTY", "text_chars": 0, "tool_use_count": 0, "thinking_only": thinking,
        "stop_reason": response.stop_reason, "runtime_status": response.runtime_status,
        "runtime_reason": response.runtime_reason, "runtime_source": response.runtime_source,
        "turn_end_reason": response.turn_end_reason, "truncated": thinking,
    }
    for original in records:
        record = dict(original)
        identity = {key: record.pop(key) for key in ("request_id", "thread_id", "purpose", "logged_at")}
        assert identity["purpose"] == "conversation_compact_summary" and identity["logged_at"] > 0
        assert record == expected_shape
    assert all(value not in caplog.text for value in ("PRIVATE-THINKING", "PRIVATE-KEY", "PRIVATE-SOURCE"))


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩诊断对不可信标签作长度约束。
def test_compact_response_diagnostic_bounds_untrusted_labels(caplog):
    response = ModelResponse(text="", backend="fake", stop_reason="x" * 10000, runtime_reason="PRIVATE BODY\nPRIVATE KEY")
    calls = []
    agent = SimpleNamespace(backend=SimpleNamespace(generate=lambda prompt: calls.append(prompt) or response))
    _summarize(agent, "", {}, [])
    records = [record.compact_response_shape for record in caplog.records if hasattr(record, "compact_response_shape")]
    assert len(calls) == 2 and calls[0] == calls[1]
    assert len(records) == 2
    assert records[0]["stop_reason"] == "invalid_label"
    assert records[0]["runtime_reason"] == "invalid_label"
    assert all(record["stop_reason"] == "invalid_label" for record in records)
    assert all(record["runtime_reason"] == "invalid_label" for record in records)
    assert len(caplog.text) < 2000 and "PRIVATE" not in caplog.text


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证模型漏写时仍保存精确用户要求及最终答复地标。
def test_compact_keeps_exact_user_and_final_answer_landmarks_when_model_omits_them() -> None:
    backend = _SummaryBackend()
    agent = SimpleNamespace(backend=backend)
    rows = [
        MessageLogEntry(
            message_id="msg-user-title",
            thread_id="thread-landmarks",
            role="user",
            content="请告诉我 Python 官网页面标题。",
        ),
        MessageLogEntry(
            message_id="msg-commentary-title",
            thread_id="thread-landmarks",
            role="assistant",
            content="我正在抓取网页，请稍候。",
            metadata={"assistant_part_id": "commentary:1"},
        ),
        MessageLogEntry(
            message_id="msg-final-title",
            thread_id="thread-landmarks",
            role="assistant",
            content="页面标题是 Welcome to Python.org。",
            metadata={"assistant_part_id": "final"},
        ),
    ]

    summary = _summarize(agent, "", {}, rows)

    assert summary.startswith("用户的暗号是紫藤")
    assert "Exact Conversation Landmarks (non-authoritative)" in summary
    # 每条原话带消息编号，模型可用 session_search(message_id=...) 读回原文。
    assert '- user [msg-user-title]: "请告诉我 Python 官网页面标题。"' in summary
    assert '- assistant_final [msg-final-title]: "页面标题是 Welcome to Python.org。"' in summary
    assert "我正在抓取网页，请稍候" not in summary


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证严格空摘要保留完整旧摘要基底和选定消息。
def test_strict_empty_summary_keeps_full_previous_base_and_selected_rows(monkeypatch) -> None:
    from agent_py_agent.agent.conversation import compact_request_budget
    from agent_py_agent.agent.conversation.compact import _CompactSummaryCall

    previous = "旧摘要前段" * 1000 + "旧摘要中段唯一标记" + "旧摘要后段" * 1000
    content = "消息前段" * 1000 + "消息中段唯一标记" + "消息后段" * 1000
    row = MessageLogEntry(message_id="long-row", thread_id="thread", role="user", content=content)
    calls = []
    monkeypatch.setattr(
        compact_request_budget, "generate_bounded_compact_response",
        lambda request, **kwargs: calls.append(kwargs) or SimpleNamespace(text="", tool_use_blocks=[]),
    )
    result = _summarize(SimpleNamespace(), previous, {}, [row], call=_CompactSummaryCall(
        preserve_complete_fallback=True,
    ))
    assert previous in result and content in result
    assert calls[0]["preserve_complete_fallback"] is True


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证地标跨后续压缩代保存且不重复附加后缀。
def test_compact_landmarks_survive_later_generation_without_suffix_duplication() -> None:
    backend = _SummaryBackend()
    agent = SimpleNamespace(backend=backend)
    first = _summarize(
        agent,
        "",
        {},
        [
            MessageLogEntry(
                message_id="msg-old-user",
                thread_id="thread-landmarks",
                role="user",
                content="网页标题是什么？",
            ),
            MessageLogEntry(
                message_id="msg-old-final",
                thread_id="thread-landmarks",
                role="assistant",
                content="Welcome to Python.org",
                metadata={"assistant_part_id": "final"},
            ),
        ],
    )
    second = _summarize(
        agent,
        first,
        {},
        [
            MessageLogEntry(
                message_id="msg-new-user",
                thread_id="thread-landmarks",
                role="user",
                content="result.txt 有哪两行？",
            ),
            MessageLogEntry(
                message_id="msg-new-final",
                thread_id="thread-landmarks",
                role="assistant",
                content="第一行 first-line，第二行 second-line。",
                metadata={"assistant_part_id": "final"},
            ),
        ],
    )

    assert second.count("## Exact Conversation Landmarks (non-authoritative)") == 1
    assert second.count('- assistant_final [msg-old-final]: "Welcome to Python.org"') == 1
    assert '- assistant_final [msg-new-final]: "第一行 first-line，第二行 second-line。"' in second


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证长答复不会挤掉较短的原始用户要求。
def test_long_final_answers_cannot_displace_short_user_requirements_from_landmarks():
    from agent_py_agent.agent.conversation.compact_landmarks import (
        LandmarkOptions,
        summary_with_conversation_landmarks,
    )
    from agent_py_agent.agent.memory_archive import estimate_tokens

    rows = []
    for i in range(18):
        rows.extend([
            MessageLogEntry(message_id=f"u{i}", thread_id="t", role="user",
                            content=f"用户要求-{i}：沿用原分工，功能实现交给子代理，主代理负责测试整合。"),
            MessageLogEntry(message_id=f"a{i}", thread_id="t", role="assistant",
                            content=f"旧结论-{i}：" + "尚未验证的长篇分析。" * 180,
                            metadata={"assistant_part_id": "final"}),
        ])
    options = LandmarkOptions(landmark_budget_tokens=2_000)
    first = summary_with_conversation_landmarks("粗略摘要", "", rows, options=options)
    assert first.used_tokens <= 2_000 and estimate_tokens(first.text.split("\n\n", 1)[1]) == first.used_tokens
    # 用户原话先分预算：18 条短要求全部保留，长篇助手结论放不下；被省略的只有助手答复，不写用户省略行。
    assert all(f"用户要求-{i}：" in first.text for i in range(18))
    assert "旧结论-" not in first.text and "omitted_user_messages" not in first.text
    second = summary_with_conversation_landmarks("新摘要", first.text, [], options=options)
    assert all(f"用户要求-{i}：" in second.text for i in range(18))
    assert second.text.count("## Exact Conversation Landmarks") == 1


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证用户地标超预算时优先近期要求且不扩总预算。
def test_landmark_user_overflow_prefers_recent_requests_without_growing_budget():
    from agent_py_agent.agent.conversation.compact_landmarks import (
        LandmarkOptions,
        summary_with_conversation_landmarks,
    )
    from agent_py_agent.agent.memory_archive import estimate_tokens

    rows = [MessageLogEntry(message_id=f"u{i}", thread_id="t", role="user", content=f"request-{i} " + "x" * 240)
            for i in range(20)]
    selected = summary_with_conversation_landmarks("", "", rows, options=LandmarkOptions(landmark_budget_tokens=1_000))
    assert selected.used_tokens <= 1_000 and estimate_tokens(selected.text) == selected.used_tokens
    assert "request-19 " in selected.text
    assert "request-0 " not in selected.text
    # 放不下的旧要求以编号列出，可按编号读回原文。
    omitted = next(line for line in selected.text.splitlines() if line.startswith("- omitted_user_messages: "))
    assert omitted.split("[", 1)[1].split()[0] == "u0" and "u19" not in omitted


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证进度回调对应真实压缩管线阶段。
def test_compact_progress_callback_reports_real_pipeline_stages(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=20_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_compact_progress")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-progress-{role}",
            role=role,
            content=f"待压缩 {role}",
        )
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None
    events: list[dict[str, object]] = []

    result = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        options=ConversationCompactOptions(
            current_prompt="继续",
            force=True,
            progress_callback=lambda payload: events.append(dict(payload)),
        ),
    )

    assert result.compacted is True
    assert [event["stage"] for event in events] == [
        "preparing",
        "summarizing",
        "measuring",
        "checkpointing",
        "committing",
        "completed",
    ]
    assert [event["percent"] for event in events] == [5, 15, 52, 78, 92, 100]
    assert events[0]["before_tokens"] > 0
    assert events[-1]["after_tokens"] == result.projected_tokens
    assert all(event["generation"] == 1 for event in events)
    assert {event["source_kind"] for event in events} == {"conversation_transcript"}
    assert {event["commit_authority"] for event in events} == {
        "conversation_thread"
    }
    assert all("summary" not in event for event in events)


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证会话容量投影不计分离Audit投递正文。
def test_conversation_projection_does_not_count_detached_audit_delivery_body() -> None:
    agent = SimpleNamespace(
        prompts=SimpleNamespace(build=lambda *_args, **_kwargs: "完整输入上下文")
    )
    baseline = _projected_context_tokens(agent, "", [], "当前消息")
    projected = _projected_context_tokens(
        agent,
        "",
        [
            MessageLogEntry(
                message_id="msg-audit",
                thread_id="thread-audit",
                role="assistant",
                content="不应进入普通上下文的审计正文" * 5000,
                metadata={
                    "task_id": "audit-old",
                    "reason": "audit_finding",
                    "background_delivery_reason": "audit_finding_report",
                },
            )
        ],
        "当前消息",
    )

    assert projected == baseline


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证会话压缩不摘要分离Audit投递正文。
def test_conversation_compact_does_not_summarize_detached_audit_delivery_body() -> None:
    backend = _SummaryBackend()
    agent = SimpleNamespace(backend=backend)
    rows = [
        MessageLogEntry(
            message_id="msg-audit",
            thread_id="thread-audit",
            role="assistant",
            content="不应进入摘要的新受益人事件正文",
            metadata={
                "task_id": "audit-old",
                "reason": "audit_finding",
                "background_delivery_reason": "audit_finding_report",
            },
        ),
        MessageLogEntry(
            message_id="msg-user",
            thread_id="thread-audit",
            role="user",
            content="普通聊天里的青黛暗号",
        ),
    ]

    _summarize(agent, "", {}, rows)

    assert len(backend.prompts) == 1
    assert "普通聊天里的青黛暗号" in backend.prompts[0]
    assert "不应进入摘要的新受益人事件正文" not in backend.prompts[0]


# LLM: 夹具使用新规范键，仍保持原50%观察点；旧YAML兼容另有Wave9回归，不放宽预算断言。
# 函数用途: 创建隔离真实Agent及明确比例，避免新默认80%改变原测试输入。
def _agent(tmp_path, *, context_tokens: int, max_turns: int = 20) -> SimpleAgent:
    config = AgentConfig(
        model_backend="echo",
        my_agent_home=str(tmp_path / "home"),
        prompt_files=[],
        max_tokens=64,
        model_context_window_tokens=context_tokens,
        memory_compact_trigger_percent=50,
        conversation_history_max_turns=max_turns,
    )
    config.config_sources = {"model_context_window_tokens": {"source": "test"}}
    return SimpleAgent(config, tmp_path)


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 构造测试Gateway请求，保留显式线程与用户输入字段。
def _request(user_id: str = "ou_alice") -> dict:
    return {
        "conversation": {
            "channel": "feishu",
            "channel_conversation_id": f"oc_{user_id}",
            "channel_user_id": user_id,
            "canonical_user_id": user_id,
        }
    }


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 用真实请求预检取得测试会话上下文。
def _context(agent: SimpleAgent, request: dict, request_id: str, prompt: str):
    return gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, request_id, prompt)
    )


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证同线程历史可超过近期轮数配置，直到实际压缩才移动游标。
def test_same_thread_accumulates_beyond_recent_turn_setting_until_compact(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=2)
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(30):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=f"第 {index} 轮 {role} 内容",
            )

    followup = _context(agent, request, "gw-follow", "继续")

    assert followup.compact_generation == 0
    assert len(context_history(followup)) == 60
    assert context_history(followup)[0][1] == "第 0 轮 user 内容"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩保留原始消息，并按owner建立旧消息索引。
def test_compact_keeps_raw_transcript_and_indexes_old_messages_per_owner(tmp_path) -> None:
    # 给稳定 system/workspace prompt 留出小幅演进余量；本测试验证的是 transcript
    # 成功压缩与 owner 索引，不应卡在候选刚好高于优选 recovery target 的单 token 边界。
    agent = _agent(tmp_path, context_tokens=16_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(12):
        content = ("紫藤暗号 " if index == 0 else "阶段资料 ") + ("内容" * 900)
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-{index}-{role}",
                role=role,
                content=content,
            )

    followup = _context(agent, request, "gw-follow", "我们接着做")
    stored = agent.conversation_store.threads.load(first.thread_id)
    raw_rows = agent.conversation_store.messages.recent(first.thread_id, limit=0)
    hits = agent.local_store.search(
        "紫藤暗号",
        limit=5,
        source_type="conversation_message",
    )

    assert followup.load_errors == ()
    assert followup.compact_generation >= 1
    assert followup.compact_summary.startswith("用户的暗号是紫藤")
    assert backend.calls == 1
    assert stored is not None
    assert stored.compacted_through_message_id
    assert stored.compacted_through_byte_offset > 0
    assert len(raw_rows) == 24
    assert hits
    assert hits[0].metadata["thread_id"] == first.thread_id
    assert str(agent.home_paths.owner_home_dir) == followup.scope.owner_home
    # 摘要只经会话种子在准备边界提供；Gateway 上下文段不渲染摘要或历史正文。
    transcript = _text_conversation_history_section(gateway_conversation_history_seed(followup))
    assert f"## Earlier Conversation Summary (generation {followup.compact_generation})" in transcript
    assert followup.compact_summary in transcript
    assert "Earlier Conversation Summary" not in _conversation_prompt_section(followup)
    tail, tail_errors = agent.conversation_store.messages.after_compact_report(stored)
    assert tail_errors == []
    assert tail == [], "当前请求之前的完整历史应一次替换为摘要，原始 transcript 仍保留"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩保留有界且完整的近期回合及全量检查点。
def test_compact_preserves_bounded_complete_recent_turns_and_full_checkpoint(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=30_000)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou_recent_tail")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(20):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-tail-{index}-{role}",
                role=role,
                content=f"第 {index} 轮 {role} " + ("近期内容" * 75),
            )

    compacted = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-follow", "继续")
    )
    stored = agent.conversation_store.threads.load(context.thread_id)
    assert stored is not None
    tail, errors = agent.conversation_store.messages.after_compact_report(stored)
    checkpoint_path = (
        agent.home_paths.owner_compact_dir
        / "conversations"
        / f"{context.thread_id}.jsonl"
    )
    checkpoint = json.loads(
        checkpoint_path.read_text(encoding="utf-8").splitlines()[-1]
    )

    assert errors == []
    assert compacted.compact_generation == 1
    assert len(tail) == 4
    assert [row.content for row in tail] == [
        f"第 {index} 轮 {role} " + ("近期内容" * 75)
        for index in range(18, 20)
        for role in ("user", "assistant")
    ]
    assert checkpoint["schema"] == "conversation_compact_checkpoint.v3"
    assert checkpoint["scope"]["kind"] == "thread"
    assert checkpoint["source_message_ids"]
    assert checkpoint["status"] == "validated_candidate"
    assert checkpoint["checkpoint_id"] == stored.compact_checkpoint_id
    assert checkpoint["previous_checkpoint_id"] == ""
    assert checkpoint["summary"] == stored.summary
    assert checkpoint["summary_sha256"]
    assert checkpoint["source_start_byte_offset"] == 0
    assert checkpoint["source_end_byte_offset"] == stored.compacted_through_byte_offset
    assert checkpoint["source_messages"] == 36
    assert checkpoint["retained_tail_messages"] == 4
    assert checkpoint["retained_tail_message_ids"] == [
        row.message_id for row in tail
    ]
    assert checkpoint["projected_tokens_after"] < checkpoint["trigger_tokens"]
    assert compacted.compact_operation_evidence_ref == str(checkpoint_path)


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证多次压缩检查点组成单一递增代际链。
def test_repeated_compact_checkpoints_form_one_generation_chain(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_checkpoint_chain")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(6):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-chain-{index}-{role}",
                role=role,
                content=f"链路 {index} {role}",
            )

    first = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-first", "继续"),
        force_compact=True,
    )
    first_thread = agent.conversation_store.threads.load(context.thread_id)
    assert first_thread is not None
    first_tail, first_tail_errors = (
        agent.conversation_store.messages.after_compact_report(first_thread)
    )
    assert first_tail_errors == []
    assert first_tail == []
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            first,
            request_id=f"gw-new-{role}",
            role=role,
            content=f"新一代 {role}",
        )
    second = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-second", "再继续"),
        force_compact=True,
    )
    path = (
        agent.home_paths.owner_compact_dir
        / "conversations"
        / f"{context.thread_id}.jsonl"
    )
    checkpoints = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert first.compact_generation == 1
    assert second.compact_generation == 2
    assert [row["generation"] for row in checkpoints] == [1, 2]
    assert checkpoints[1]["previous_checkpoint_id"] == checkpoints[0]["checkpoint_id"]
    assert checkpoints[1]["source_start_byte_offset"] == checkpoints[0][
        "source_end_byte_offset"
    ]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证非法摘要不推进消息游标，并按原失败规则开启熔断。
def test_invalid_summary_candidate_never_advances_cursor_and_opens_circuit(tmp_path) -> None:
    # 初始化会话时不能先用小于固定 prompt 的窗口；该测试随后才显式制造坏候选。
    agent = _agent(tmp_path, context_tokens=20_000)
    backend = _OversizedSummaryBackend()
    agent.backend = backend
    request = _request("ou_bad_candidate")
    context = _context(agent, request, "gw-create", "开始")
    agent.config.model_context_window_tokens = 2_000
    for index in range(2):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-bad-{index}-{role}",
                role=role,
                content=f"消息 {index} {role}",
            )

    for _attempt in range(3):
        thread = agent.conversation_store.threads.load(context.thread_id)
        assert thread is not None
        with pytest.raises(
            ConversationCompactError,
            match="did not fit the input trigger and known output reserve",
        ):
            prepare_conversation_context(
                agent,
                agent.conversation_store,
                thread,
                options=ConversationCompactOptions(current_prompt="继续", force=True),
            )
    calls_before_circuit = backend.calls
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None
    agent.config.model_context_window_tokens = 1_000_000
    normal_context = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        options=ConversationCompactOptions(current_prompt="普通短消息仍可继续"),
    )
    assert normal_context.compacted is False
    assert backend.calls == calls_before_circuit
    with pytest.raises(ConversationCompactCircuitOpenError):
        prepare_conversation_context(
            agent,
            agent.conversation_store,
            thread,
            options=ConversationCompactOptions(current_prompt="继续", force=True),
        )
    stored = agent.conversation_store.threads.load(context.thread_id)

    assert stored is not None
    assert backend.calls == calls_before_circuit
    assert stored.compact_generation == 0
    assert stored.compacted_through_message_id == ""
    assert stored.compacted_through_byte_offset == 0
    assert stored.compact_checkpoint_id == ""
    assert stored.summary == ""
    assert stored.compact_consecutive_failures == 3
    assert stored.compact_failure_code == "COMPACT_CANDIDATE_TOO_LARGE"
    checkpoint_path = (
        agent.home_paths.owner_compact_dir
        / "conversations"
        / f"{context.thread_id}.jsonl"
    )
    assert not checkpoint_path.exists()


# LLM: 使用新规范比例键，仍强验8500候选/9000触发线和单次摘要，不放宽失败边界。
# 函数用途: 验证健康目标不可达时保留低于实际触发线的候选。
def test_transcript_compact_keeps_valid_candidate_below_trigger_when_target_is_unreachable(
    tmp_path,
    monkeypatch,
) -> None:
    """60% 是优选目标；候选已低于 90% 触发线时不能反复丢弃并重烧摘要。"""

    from agent_py_agent.agent.conversation import compact as compact_module

    agent = _agent(tmp_path, context_tokens=10_000)
    agent.config.memory_compact_trigger_percent = 90
    agent.config.memory_compact_recovery_target_percent = 60
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou-trigger-fallback")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(4):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                context,
                request_id=f"gw-trigger-fallback-{index}-{role}",
                role=role,
                content=f"第 {index} 轮 {role} " + ("历史内容" * 80),
            )
    original_build = compact_module._build_compact_candidate

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 构造低于触发阈值的摘要候选，固定容量观察边界。
    def build_candidate_below_trigger(*args, **kwargs):
        candidate = original_build(*args, **kwargs)
        return replace(candidate, projected_tokens_after=8_500)

    monkeypatch.setattr(
        compact_module,
        "_build_compact_candidate",
        build_candidate_below_trigger,
    )
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None

    compacted = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        options=ConversationCompactOptions(current_prompt="继续", force=True),
    )
    stored = agent.conversation_store.threads.load(context.thread_id)

    assert compacted.compacted is True
    assert compacted.projected_tokens == 8_500
    assert compacted.trigger_tokens == 9_000
    assert stored is not None and stored.compact_generation == 1
    assert stored.compact_consecutive_failures == 0
    assert backend.calls == 1


# LLM: 新规范键显式90%，所有原输出预留与拒绝断言不变；不拿80%新默认改变旧观察输入。
# 函数用途: 在多种输出预留和OAuth模式下核候选容量硬边界。
@pytest.mark.parametrize("output_cap,candidate_tokens,explicit,oauth,force,accepted", [
    (4_000, 5_999, True, False, False, True),
    (4_000, 6_000, True, False, True, False),
    (4_000, 7_000, True, False, True, False),
    (2_000, 7_500, True, False, True, True),
    (2_000, 8_000, True, False, True, False),
    (10_000, 0, True, False, True, False),
    (0, 8_500, True, False, True, True),
    (4_000, 8_500, False, False, True, True),
    (4_000, 8_500, True, True, True, True),
    (0, 9_000, True, False, True, False),
])
def test_transcript_candidate_respects_output_reserve_before_target_or_fallback(
    tmp_path, monkeypatch, output_cap, candidate_tokens, explicit, oauth, force, accepted,
) -> None:
    from agent_py_agent.agent.backends.base import BackendOptions
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.agent.backends.responses import OpenAIResponsesBackend
    from agent_py_agent.agent.conversation import compact as module

    agent = _agent(tmp_path, context_tokens=10_000)
    if not explicit:
        agent.config.model_context_window_tokens = None
    agent.config.memory_compact_trigger_percent = 90
    agent.config.memory_compact_recovery_target_percent = 60
    backend_type = OpenAIResponsesBackend if oauth else HttpBackend
    agent.backend = backend_type(BackendOptions(api_base="https://example.invalid", api_key="fake-key",
        model_name="test-model", context_window_tokens=10_000, max_tokens=output_cap))
    if oauth:
        agent.backend.auth_ref = {"mode": "chatgpt"}
    store = agent.conversation_store
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "capacity"})
    for role in ("user", "assistant"):
        store.messages.append({"thread_id": thread.thread_id, "role": role, "content": "完整旧轮次",
            "metadata": {"conversation_request_id": "prior-turn"}})
    # 这里只隔离测量值和摘要供应商；候选分区、接受判断、checkpoint/CAS及失败记账均走生产链。
    monkeypatch.setattr(module, "_summarize", lambda *args, **kwargs: "新摘要")
    monkeypatch.setattr(module, "_projected_context_tokens", lambda _agent, summary, *args, **kwargs:
                        candidate_tokens if summary else 8_500)
    options = ConversationCompactOptions(current_prompt="继续完整任务", force=force)
    if accepted:
        result = prepare_conversation_context(agent, store, thread, options=options)
        assert result.compacted and result.projected_tokens == candidate_tokens
        assert result.trigger_tokens == 9_000
    else:
        with pytest.raises(ConversationCompactError) as error:
            prepare_conversation_context(agent, store, thread, options=options)
        assert error.value.code == "COMPACT_CANDIDATE_TOO_LARGE"
    stored = store.threads.load(thread.thread_id)
    assert stored.compact_generation == int(accepted)
    if not accepted:
        assert stored.compact_checkpoint_id == "" and stored.summary == ""
        assert stored.compacted_through_byte_offset == 0


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证冷却后压缩熔断进入半开态，成功后重置计数。
def test_compact_circuit_half_opens_after_cooldown_and_success_resets_it(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_compact_half_open")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-half-open-{role}",
            role=role,
            content=f"待压缩 {role}",
        )
    for failure_index in range(3):
        agent.conversation_store.threads.record_compact_failure(
            context.thread_id,
            failure_code=f"TEST_FAILURE_{failure_index}",
            expected_generation=0,
            now=1.0 + failure_index,
        )

    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None
    assert thread.compact_consecutive_failures == 3
    compacted = prepare_conversation_context(
        agent,
        agent.conversation_store,
        thread,
        options=ConversationCompactOptions(current_prompt="继续", force=True),
    )
    stored = agent.conversation_store.threads.load(context.thread_id)

    assert compacted.compacted is True
    assert stored is not None
    assert stored.compact_generation == 1
    assert stored.compact_consecutive_failures == 0
    assert stored.compact_failure_updated_at == 0.0
    assert stored.compact_failure_code == ""


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证检查点写入失败时不提交摘要候选。
def test_checkpoint_write_failure_does_not_commit_candidate(tmp_path, monkeypatch) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_checkpoint_failure")
    context = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-checkpoint-{role}",
            role=role,
            content=f"待压缩 {role}",
        )

    # LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
    # 函数用途: 在真实检查点写入边界注入确定错误，不伪造摘要提交成功。
    def fail_checkpoint(*_args, **_kwargs):
        raise OSError("checkpoint unavailable")

    monkeypatch.setattr(
        "agent_py_agent.agent.conversation.compact.write_compact_checkpoint",
        fail_checkpoint,
    )
    thread = agent.conversation_store.threads.load(context.thread_id)
    assert thread is not None
    with pytest.raises(OSError, match="checkpoint unavailable"):
        prepare_conversation_context(
            agent,
            agent.conversation_store,
            thread,
            options=ConversationCompactOptions(current_prompt="继续", force=True),
        )
    stored = agent.conversation_store.threads.load(context.thread_id)

    assert stored is not None
    assert stored.compact_generation == 0
    assert stored.compacted_through_byte_offset == 0
    assert stored.compact_checkpoint_id == ""
    assert stored.compact_consecutive_failures == 1


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证近期尾部保留结构化操作证据。
def test_recent_tail_keeps_typed_operation_evidence_visible(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=30_000)
    agent.backend = _SummaryBackend()
    request = _request("ou_recent_operation")
    context = _context(agent, request, "gw-create", "开始")
    for index in range(20):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-operation-{index}-user",
            role="user",
            content=f"第 {index} 步 " + ("近期内容" * 75),
        )
        verification = (
            {
                "schema": "operation_verification.public.v1",
                "status": "succeeded",
                "operation_count": 1,
                "counts": {"succeeded": 1},
                "groups": [
                    {
                        "tool": "write_file",
                        "action": "write",
                        "label": "write_file/write",
                        "status": "succeeded",
                        "count": 1,
                        "replayed": False,
                    }
                ],
            }
            if index == 19
            else None
        )
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            context,
            request_id=f"gw-operation-{index}-assistant",
            role="assistant",
            content=f"完成第 {index} 步 " + ("近期内容" * 75),
            operation_verification=verification,
        )

    compacted = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-follow", "继续")
    )
    rendered = _conversation_prompt_section(compacted)

    assert compacted.recent_operation_evidence["operation_count"] == 1
    assert "Program-Verified Operations From Recent Raw History" in rendered
    assert "write_file/write" in rendered


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证近期回合选取依据消息角色，而非正文词句。
def test_recent_tail_selection_uses_roles_not_message_wording() -> None:
    rows = [
        MessageLogEntry(
            message_id=f"msg-{index}-{role}",
            thread_id="thread-1",
            role=role,
            content=f"任意语言 {index} {role}",
        )
        for index in range(6)
        for role in ("user", "assistant")
    ]
    rows.append(
        MessageLogEntry(
            message_id="msg-interrupted",
            thread_id="thread-1",
            role="user",
            content="没有 assistant 的中断输入",
        )
    )

    prefix, tail = split_recent_complete_turns(
        rows,
        max_turns=2,
        max_tail_tokens=1_000_000,
    )

    assert [row.message_id for row in prefix] == [
        f"msg-{index}-{role}"
        for index in range(4)
        for role in ("user", "assistant")
    ]
    assert [row.message_id for row in tail] == [
        *[
            f"msg-{index}-{role}"
            for index in range(4, 6)
            for role in ("user", "assistant")
        ],
        "msg-interrupted",
    ]


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩与历史索引保留结构化操作核验结果。
def test_compact_and_history_index_keep_typed_operation_verification(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request()
    context = _context(agent, request, "gw-create", "开始")
    verification = {
        "schema": "operation_verification.public.v1",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "groups": [
            {
                "tool": "remember",
                "action": "remove",
                "label": "remember/remove",
                "status": "succeeded",
                "count": 1,
                "replayed": False,
            }
        ],
    }
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-operation",
        role="assistant",
        content="已经处理。",
        operation_verification=verification,
    )

    row = agent.conversation_store.messages.recent(context.thread_id, limit=1)[0]
    stored_verification = row.metadata["operation_verification"]
    projected = _summary_content(row)
    indexed = agent.local_store.get_record(
        agent.local_store.make_record_id(
            "conversation_message",
            f"{context.thread_id}:{row.message_id}",
        )
    )

    assert projected == {
        "content": "已经处理。",
        "operation_verification": stored_verification,
    }
    assert indexed is not None
    assert indexed.metadata["operation_verification"] == stored_verification


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证操作证据独立保存，不被矛盾模型摘要覆盖。
def test_compact_keeps_operation_evidence_outside_conflicting_model_summary(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _ConflictingSummaryBackend()
    request = _request()
    context = _context(agent, request, "gw-create", "开始")
    verification = {
        "schema": "operation_verification.public.v1",
        "status": "succeeded",
        "operation_count": 1,
        "counts": {"succeeded": 1},
        "groups": [
            {
                "tool": "remember",
                "action": "list",
                "label": "remember/list",
                "status": "succeeded",
                "count": 1,
                "replayed": False,
                "call_id": "must-not-survive",
            }
        ],
    }
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        context,
        request_id="gw-conflicting-claim",
        role="assistant",
        content="已经删除海王星项目记忆。",
        operation_verification=verification,
    )

    followup = gateway_conversation_context(
        GatewayConversationLoadRequest(
            agent,
            request,
            "gw-follow",
            "请准确说明是否真的删除。",
        ),
        force_compact=True,
    )

    assert followup.compact_summary.startswith("助手已经成功删除海王星项目记忆。")
    assert "Exact Conversation Landmarks (non-authoritative)" in followup.compact_summary
    evidence = followup.compact_operation_evidence
    assert evidence["schema"] == "conversation_operation_evidence.v1"
    assert evidence["coverage"] == "complete"
    assert evidence["assistant_message_count"] == 1
    assert evidence["verified_assistant_message_count"] == 1
    assert evidence["operation_event_count"] == 1
    assert evidence["operation_count"] == 1
    assert evidence["events"][0]["verification"]["groups"] == [
        {
            "tool": "remember",
            "action": "list",
            "label": "remember/list",
            "status": "succeeded",
            "count": 1,
            "replayed": False,
        }
    ]
    # 冲突的模型摘要只进会话种子的历史段，程序核验的操作证据只在 Gateway 上下文段，两者互不混入。
    section = _conversation_prompt_section(followup)
    transcript = _text_conversation_history_section(gateway_conversation_history_seed(followup))
    assert "Program-Verified Operations From Compacted History" in section
    assert "助手已经成功删除" not in section
    assert "助手已经成功删除" in transcript
    assert "Program-Verified Operations From Compacted History" not in transcript
    assert "remember/list" in section
    assert "remember/remove" not in section
    assert "must-not-survive" not in section


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证操作证据有界，并如实标记旧历史缺口。
def test_compact_operation_evidence_is_bounded_and_reports_legacy_gaps() -> None:
    rows = [
        MessageLogEntry(
            message_id=f"msg-{index}",
            thread_id="thread-1",
            role="assistant",
            content=f"assistant {index}",
            metadata=(
                {
                    "operation_verification": {
                        "schema": "operation_verification.public.v1",
                        "status": "succeeded",
                        "operation_count": 1,
                        "counts": {"succeeded": 1},
                        "groups": [
                            {
                                "tool": "remember",
                                "action": "list",
                                "label": "remember/list",
                                "status": "succeeded",
                                "count": 1,
                                "replayed": False,
                            }
                        ],
                    }
                }
                if index != 7
                else {}
            ),
        )
        for index in range(40)
    ]

    evidence = _merge_compact_operation_evidence({}, rows)

    assert evidence["assistant_message_count"] == 40
    assert evidence["verified_assistant_message_count"] == 39
    assert evidence["unverified_assistant_message_count"] == 1
    assert evidence["coverage"] == "partial"
    assert evidence["operation_event_count"] == 39
    assert evidence["operation_count"] == 39
    assert len(evidence["events"]) == 32
    assert evidence["omitted_event_count"] == 7
    assert evidence["events"][0]["assistant_sequence"] == 9
    assert evidence["events"][-1]["assistant_sequence"] == 40


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证提示中保留全量操作计数，仅裁剪近期明细。
def test_prompt_operation_evidence_keeps_full_counts_and_only_recent_details() -> None:
    evidence = {
        "schema": "conversation_operation_evidence.v1",
        "coverage": "complete",
        "assistant_message_count": 20,
        "verified_assistant_message_count": 20,
        "unverified_assistant_message_count": 0,
        "operation_event_count": 12,
        "operation_count": 12,
        "counts": {"succeeded": 12},
        "omitted_event_count": 3,
        "events": [
            {
                "assistant_sequence": index,
                "verification": {
                    "schema": "operation_verification.public.v1",
                    "status": "succeeded",
                    "operation_count": 1,
                    "groups": [{"label": f"event-{index}"}],
                },
            }
            for index in range(12)
        ],
    }

    projection = _prompt_operation_evidence(evidence)
    section = _conversation_prompt_section(
        GatewayConversationContext(
            thread_id="thread-bounded-evidence",
            compact_operation_evidence=evidence,
            compact_operation_evidence_ref="/owner/compact/conversations/thread.jsonl",
        )
    )

    assert projection["operation_event_count"] == 12
    assert projection["operation_count"] == 12
    assert projection["counts"] == {"succeeded": 12}
    assert projection["omitted_event_count"] == 11
    assert projection["prompt_event_limit"] == 4
    assert [row["assistant_sequence"] for row in projection["events"]] == [8, 9, 10, 11]
    assert "event-0" not in section
    assert "event-8" in section
    assert "/owner/compact/conversations/thread.jsonl" in section
    assert len(evidence["events"]) == 12


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证模型正文不会被推断成已完成的工具操作。
def test_summary_content_does_not_infer_operation_from_assistant_prose() -> None:
    row = MessageLogEntry(
        message_id="msg-1",
        thread_id="thread-1",
        role="assistant",
        content="我已经删除了记忆。",
        metadata={},
    )

    assert _summary_content(row) == "我已经删除了记忆。"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证强制压缩不把当前Gateway未完成回合纳入摘要。
def test_forced_compact_keeps_current_gateway_turn_out_of_summary(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    for index in range(2):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-old-{index}-{role}",
                role=role,
                content=f"旧消息 {index} {role}",
            )
    current_marker = "本轮输入不能进入较早摘要"
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        first,
        request_id="gw-current",
        role="user",
        content=current_marker,
    )

    refreshed = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-current", current_marker),
        force_compact=True,
    )
    stored = agent.conversation_store.threads.load(first.thread_id)
    tail, errors = agent.conversation_store.messages.after_compact_report(stored)

    assert errors == []
    assert refreshed.compact_generation == 1
    assert backend.calls == 1
    assert current_marker not in backend.prompts[0]
    assert current_marker not in json.dumps(
        backend.kwargs[0].get("messages") or [],
        ensure_ascii=False,
    )
    assert any(
        row.metadata.get("gateway_request_id") == "gw-current" and row.content == current_marker
        for row in tail
    )
    assert all(current_marker not in content for _role, content in context_history(refreshed))
    event_path = agent.home_paths.owner_compact_dir / "conversations" / f"{first.thread_id}.jsonl"
    assert '"forced": true' in event_path.read_text(encoding="utf-8")


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证同一Gateway请求内已完成检查点可进入后续强制压缩。
def test_forced_compact_includes_completed_checkpoint_from_same_gateway_request(
    tmp_path,
) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request()
    first = _context(agent, request, "gw-create", "开始")
    request_id = "gw-long-running"
    user_marker = "同一长任务的原始需求"
    assistant_marker = "子代理已完成，主代理即将整合"
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        first,
        request_id=request_id,
        role="user",
        content=user_marker,
    )
    assert append_gateway_conversation_message(
        agent,
        {"metadata": {"channel": "feishu"}},
        first,
        request_id=request_id,
        role="assistant",
        content=assistant_marker,
    )

    refreshed = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, request_id, user_marker),
        force_compact=True,
    )
    stored = agent.conversation_store.threads.load(first.thread_id)
    tail, errors = agent.conversation_store.messages.after_compact_report(stored)

    assert errors == []
    assert refreshed.compact_generation == 1
    assert backend.calls == 1
    provider_history = json.dumps(
        backend.kwargs[0].get("messages") or [],
        ensure_ascii=False,
    )
    assert user_marker in provider_history
    assert assistant_marker in provider_history
    assert user_marker not in backend.prompts[0]
    assert assistant_marker not in backend.prompts[0]
    layout = prompt_cache_layout(backend.prompts[0])
    assert layout is not None
    assert "# System" in layout.stable_prefix
    assert "# Tools" in layout.stable_prefix
    assert "You maintain a conversation summary" in layout.volatile_suffix
    tools = backend.kwargs[0].get("tools")
    assert isinstance(tools, list) and tools
    assert getattr(backend.kwargs[0].get("tool_choice"), "mode", "") == "auto"
    projection = anthropic_prompt_cache_projection(
        system_instruction="",
        prompt=backend.prompts[0],
        cache_enabled=True,
        native_messages=True,
    )
    wire_messages, wire_tools = anthropic_messages_with_optional_cache(
        prompt=projection.prompt,
        messages=list(backend.kwargs[0].get("messages") or []),
        tools=tools,
        cache_enabled=True,
        stable_user_prefix=projection.stable_user_prefix,
        stable_system_cache_active=projection.stable_system_cache_active,
        structured_native_layout_active=projection.structured_native_layout_active,
    )
    assert "You maintain a conversation summary" in json.dumps(
        wire_messages[-1],
        ensure_ascii=False,
    )
    assert "cache_control" not in wire_messages[-1]["content"][-1]
    assert any(
        isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}
        for message in wire_messages[:-1]
        for block in (
            message.get("content", [])
            if isinstance(message.get("content"), list)
            else []
        )
    )
    assert wire_tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert stored is not None and stored.compact_source_messages == 2
    assert tail == []


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证上下文溢出后仍保留待延后工具的原生调用界面。
def test_transcript_compact_preserves_pending_deferred_tool_surface_after_overflow(
    tmp_path,
) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    backend = _SummaryBackend()
    agent.backend = backend
    request = _request("ou_deferred_tool_compact")
    first = _context(agent, request, "gw-create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent,
            {"metadata": {"channel": "feishu"}},
            first,
            request_id=f"gw-deferred-{role}",
            role=role,
            content=f"待压缩 {role}",
        )

    refreshed = gateway_conversation_context(
        GatewayConversationLoadRequest(
            agent,
            request,
            "gw-force-deferred",
            "继续",
            loaded_tool_names=("get_goal",),
        ),
        force_compact=True,
    )
    tool_names = {
        str(item.get("name") or "")
        for item in (backend.kwargs[0].get("tools") or [])
        if isinstance(item, dict)
    }

    assert refreshed.compact_generation == 1
    assert "get_goal" in tool_names


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证Gateway线程把运行上下文标记为会话作用域。
def test_gateway_thread_marks_runtime_context_as_conversation_scoped(tmp_path) -> None:
    agent = _agent(tmp_path, context_tokens=1_000_000)
    request = _request()
    conversation = _context(agent, request, "gw-context", "开始")
    context = GatewayAskRunContext(
        agent=agent,
        request=request,
        request_id="gw-context",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        on_chunk=lambda _chunk: None,
    )

    params = _gateway_run_params(
        _GatewayRunParamsRequest(request, context, conversation, "开始")
    )

    assert params.context_scope == "conversation"


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证压缩替换旧消息视图后，下一提示仍保留owner人格与相关记忆。
def test_conversation_compact_keeps_persona_and_related_memory_in_next_prompt(tmp_path) -> None:
    """Compact 只替换旧 transcript 的模型视图，不应吞掉 owner Persona/Memory。"""

    agent = _agent(tmp_path, context_tokens=30_000, max_turns=3)
    backend = _SummaryBackend()
    agent.backend = backend
    agent.home_paths.owner_soul_md.write_text("人格原则：耐心、直接。\n", encoding="utf-8")
    agent.home_paths.owner_user_md.write_text("称呼用户为小明。\n", encoding="utf-8")
    request = _request("ou_persona_memory")
    first = _context(agent, request, "gw-create", "开始")
    for index in range(20):
        for role in ("user", "assistant"):
            assert append_gateway_conversation_message(
                agent,
                {"metadata": {"channel": "feishu"}},
                first,
                request_id=f"gw-old-{index}-{role}",
                role=role,
                content=f"旧消息 {index} {role} " + ("阶段内容" * 75),
            )

    current_prompt = "暗号是什么"
    compacted = gateway_conversation_context(
        GatewayConversationLoadRequest(agent, request, "gw-current", current_prompt)
    )
    context = GatewayAskRunContext(
        agent=agent,
        request=request,
        request_id="gw-current",
        request_path=tmp_path / "request.json",
        response_path=tmp_path / "response.json",
        on_chunk=lambda _chunk: None,
    )
    params = _gateway_run_params(
        _GatewayRunParamsRequest(request, context, compacted, current_prompt)
    )
    rendered = agent.prompts.build(
        current_prompt,
        memories=[MemoryRecord(role="user", content="长期暗号是白鹭湾", kind="preference")],
        inject=params.inject,
        context_scope=params.context_scope,
    )

    assert compacted.compact_generation == 1
    history_seed = params.conversation_history_seed
    assert history_seed is not None
    assert history_seed.compact_summary == compacted.compact_summary
    assert history_seed.compact_generation == 1
    assert "人格原则：耐心、直接。" in rendered
    assert "称呼用户为小明。" in rendered
    assert "长期暗号是白鹭湾" in rendered
    history_text = "\n".join(content for _role, content in seed_text_messages(history_seed))
    assert "旧消息 0" not in history_text
    assert "旧消息 19 user" in history_text
    assert "旧消息 19 assistant" in history_text


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证会话历史索引不跨owner本地存储检索。
def test_conversation_search_index_does_not_cross_owner_local_stores(tmp_path) -> None:
    alice = _agent(tmp_path / "alice", context_tokens=1_000_000)
    bob = _agent(tmp_path / "bob", context_tokens=1_000_000)
    alice_context = _context(alice, _request("ou_alice"), "gw-a", "开始")
    assert append_gateway_conversation_message(
        alice,
        {"metadata": {"channel": "feishu"}},
        alice_context,
        request_id="gw-a",
        role="user",
        content="只属于 Alice 的银杏计划",
    )

    assert alice.local_store.search("银杏计划", source_type="conversation_message")
    assert bob.local_store.search("银杏计划", source_type="conversation_message") == []


# LLM: 本地压缩回归；副作用仅限测试数据，替身不代表真实供应商，保留原Store/CAS及错误断言。
# 函数用途: 验证非文本分段来源不能误提交机械摘要。
@pytest.mark.parametrize("block", [
    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "test"}},
    {"type": "future_media", "ref": "opaque-source"},
])
def test_nontext_segment_source_does_not_commit_mechanical_summary(tmp_path, monkeypatch, block):
    from agent_py_agent.agent.conversation import compact, compact_request_budget
    from agent_py_agent.agent.conversation.compact_message_source import CompactMessageSource
    from agent_py_agent.agent.conversation.compact_provider_surface import (
        ConversationCompactModelSurface,
    )

    agent = _agent(tmp_path, context_tokens=1_000_000)
    agent.backend = _SummaryBackend()
    context = _context(agent, _request("nontext_compact"), "create", "开始")
    for role in ("user", "assistant"):
        assert append_gateway_conversation_message(
            agent, {"metadata": {"channel": "feishu"}}, context,
            request_id=f"source-{role}", role=role, content=f"原始来源 {role}",
        )
    thread = agent.conversation_store.threads.load(context.thread_id)
    original_messages = agent.conversation_store.messages.recent(thread.thread_id)
    # 来源投影夹具保留未知媒体块；真实分段器、失败处理与Store提交路径照常运行。
    monkeypatch.setattr(compact, "conversation_compact_provider_source", lambda *_args, **_kwargs: CompactMessageSource(
        lambda: iter(({"role": "user", "content": [block]},)),
    ))
    monkeypatch.setattr(compact_request_budget, "resolve_model_context_window_tokens", lambda _agent: 1)
    with pytest.raises(ConversationCompactError) as error:
        prepare_conversation_context(
            agent, agent.conversation_store, thread,
            options=ConversationCompactOptions(
                current_prompt="继续", force=True, model_surface=ConversationCompactModelSurface(),
            ),
        )
    assert error.value.code == "COMPACT_SOURCE_NON_TEXT"
    stored = agent.conversation_store.threads.load(thread.thread_id)
    assert stored.compact_generation == 0
    assert stored.compacted_through_byte_offset == 0
    assert stored.compact_checkpoint_id == ""
    assert stored.summary == thread.summary
    assert stored.compact_consecutive_failures == 1
    assert agent.conversation_store.messages.recent(thread.thread_id) == original_messages
    assert agent.backend.calls == 0
