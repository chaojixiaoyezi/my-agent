"""模型调用账裁剪不能丢掉进行中的调用（2026-10-02 生产缺陷：sol 会话压缩 COMPACT_KEYERROR）。

现场：同一个 agent 共用一本调用账，明细只留最新 128 条，主回合与辅助调用（压缩摘要）不拿活令牌。sol 的压缩跑了约 11 分钟，
同一 agent 上另一会话发了约 139 次调用，进行中的摘要调用明细被裁，收尾时 _require_record 抛 KeyError，压缩失败。锁定：
1. 裁剪只针对已结束的历史；进行中（started / first_token）且近期有活动的调用不裁，之后照常写活动与终态；
2. 它所属请求的累计数（状态计数、用量）不因别的请求涌入而丢；
3. 停机结清 fail_open_calls 仍能看到这些长调用（J17）；
4. 防泄漏：在途调用超过 open_call_stale_seconds 无活动算失联，可像已结束明细一样被裁，并计入 stale_open_calls_trimmed；
5. 产品路径：压缩摘要（辅助调用）和主回合长调用进行中，同一 agent 上另发 129 次调用，收尾不抛 KeyError，账面“完成”数对得上。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import (
    model_call_summary,
    record_model_call_finished,
    start_model_call_record,
)
from agent_py_agent.agent.backends.base import ModelResponse
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallActivityParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation.auxiliary_model_call import (
    AuxiliaryModelCallRequest,
    generate_auxiliary_model_response,
)
from agent_py_agent.tests._tool_runtime_harness import make_test_protocol_snapshot

_FLOOD = 129  # 比生产默认的 128 条明细上限多一条；生产那段时间里约 139 次
_SETTLED = {"failed": 0, "finished": 1, "first_token": 0, "started": 0, "timed_out": 0}


class _Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def now(self) -> float:
        return self.value


def _ledger(clock: _Clock | None = None) -> ModelCallLedger:
    return ModelCallLedger(options=ModelCallLedgerOptions(max_records=4),
                           context=ModelCallLedgerContext(now=(clock or _Clock()).now))


def _start(ledger: ModelCallLedger, call_id: str, request_id: str) -> None:
    ledger.started(ModelCallStartedParams(call_id=call_id, backend="fake", model="m", input_tokens=10,
                                          request_id=request_id, run_id=request_id))


def _flood(ledger: ModelCallLedger, count: int = _FLOOD, *, prefix: str = "other") -> None:
    # 模拟同一 agent 上另一会话：每次调用各是一个请求，开始即结束。
    for index in range(count):
        call_id = f"{prefix}-{index}"
        _start(ledger, call_id, f"{prefix}-request-{index}")
        ledger.finished(ModelCallFinishParams(call_id=call_id, output_tokens=1))


def _ids(ledger: ModelCallLedger) -> set[str]:
    return {record.call_id for record in ledger.records()}


# ---------------------------------------------------------------- 账本层


@pytest.mark.parametrize("state", ["started", "first_token"])
def test_open_call_survives_trimming_and_still_finishes(state):
    ledger = _ledger()
    _start(ledger, "long-call", "long-request")
    if state == "first_token":
        ledger.first_token(ModelCallFirstTokenParams(call_id="long-call", output_tokens_seen=1))
    _flood(ledger)
    assert "long-call" in _ids(ledger)  # 进行中：不裁
    ledger.activity(ModelCallActivityParams(call_id="long-call", output_tokens_seen=5))
    assert ledger.finished(ModelCallFinishParams(call_id="long-call", output_tokens=7)).status == "finished"
    _flood(ledger, prefix="later")
    assert "long-call" not in _ids(ledger) and len(ledger.records()) == 4  # 结束之后就是普通历史，照常裁


def test_open_call_request_counts_survive_other_requests():
    ledger = _ledger()
    agent = SimpleNamespace(_model_call_ledger=ledger)
    _start(ledger, "long-call", "long-request")
    ledger.first_token(ModelCallFirstTokenParams(call_id="long-call", output_tokens_seen=1))
    _flood(ledger)  # 129 个别的请求，远超请求累计容器的上限
    ledger.finished(ModelCallFinishParams(call_id="long-call", output_tokens=7))
    summary = model_call_summary(agent, request_id="long-request")
    assert summary["status_counts"] == _SETTLED
    assert summary["physical_model_attempt_count"] == 1 and summary["output_tokens"] == 7


def test_shutdown_settlement_still_sees_long_open_calls():
    ledger = _ledger()
    _start(ledger, "long-call", "long-request")
    ledger.first_token(ModelCallFirstTokenParams(call_id="long-call", output_tokens_seen=1))
    _flood(ledger)
    settled = ledger.fail_open_calls(error_type="GatewayShutdown", error_code="MODEL_CALL_INTERRUPTED_BY_SHUTDOWN")
    assert [record.call_id for record in settled] == ["long-call"]  # J17：停机能结清它


def test_stale_open_calls_are_trimmed_and_counted():
    clock = _Clock()
    ledger = _ledger(clock)
    _start(ledger, "leaked-call", "leaked-request")  # 永远收不到结束或失败
    _start(ledger, "live-call", "live-request")
    clock.value += ledger.options.open_call_stale_seconds - 1
    ledger.first_token(ModelCallFirstTokenParams(call_id="live-call", output_tokens_seen=1))  # 一直有活动
    clock.value += 2  # leaked 已超过失联门槛；live 1 秒前刚有活动
    _flood(ledger)
    assert "leaked-call" not in _ids(ledger) and "live-call" in _ids(ledger)
    assert ledger.stale_open_calls_trimmed() == 1


# ---------------------------------------------------------------- 产品路径


def test_compact_summary_call_finishes_while_another_session_floods_the_ledger():
    ledger = ModelCallLedger()  # 生产默认：128 条明细

    def generate(prompt, on_chunk=None, **_options):
        on_chunk("摘要片段")
        _flood(ledger)  # 摘要流进行中，同一 agent 上另一会话发 129 次调用
        return ModelResponse(text="摘要", backend="fake")

    agent = SimpleNamespace(backend=SimpleNamespace(name="fake", model_name="m", max_tokens=128, generate=generate),
                            _model_call_ledger=ledger)
    request = AuxiliaryModelCallRequest(agent=agent, prompt="压缩来源", purpose="conversation_compact_summary",
                                        request_id="compact-request", run_id="compact-request")
    assert generate_auxiliary_model_response(request).text == "摘要"  # 改前：KeyError('unknown model call id: ...')
    assert model_call_summary(agent, request_id="compact-request")["status_counts"] == _SETTLED


def test_main_turn_long_call_finishes_while_another_session_floods_the_ledger():
    agent = SimpleNamespace(backend=SimpleNamespace(name="fake", model_name="m", max_tokens=128),
                            config=SimpleNamespace(request_timeout=10))
    request = SimpleNamespace(agent=agent, prompt="主回合", tool_rounds=0, params=SimpleNamespace(
        request_id="main-request", run_id="main-run", task_id="task-1", context_scope="isolated",
        tool_protocol_snapshot=make_test_protocol_snapshot(run_id="main-run", source_protocol="text"),
        effective_on_chunk=None))
    ledger, call_id, _ = start_model_call_record(request)
    ledger.first_token(ModelCallFirstTokenParams(call_id=call_id, output_tokens_seen=1))
    _flood(ledger)
    record_model_call_finished(ledger, call_id, ModelResponse(text="ok", backend="fake"))
    assert model_call_summary(agent, request_id="main-request")["status_counts"] == _SETTLED
