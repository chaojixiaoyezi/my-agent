"""WebSocket 握手超时的诊断字段 timeout_wait_phase（ds2p，2026-10-03）。

修法 A 把握手超时照 SSE 归成 ``first_event``，才能被回合层退避重试；代价是账本里
「握手没连上」和「连上了但首个事件超时」都记 ``first_event``，事后统计分不开。
本文件钉住新加的那条诊断事实：

* 握手超时：``stage`` 仍是 ``first_event``（回归规则不许动），``wait_phase`` 是 ``handshake``；
* 连接打开、response.create 发出之后的首事件超时：``stage`` 仍是 ``first_event``，
  但 ``wait_phase`` 为空串（没有更细的连接位置可标注）；
* 两种超时都能原样落进模型调用账本（``timeout_wait_phase`` 字段与 ``to_dict`` 键）；
* 封闭集合：异常构造与账本写入都对未登记值 fail-closed。

网络全部用假连接，不联网、不读凭据。
"""

import json
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.backends import responses_websocket as ws
from agent_py_agent.agent.backends.errors import ProviderTimeoutError
from agent_py_agent.agent.contracts.model_call_ledger import (
    TIMEOUT_STAGES,
    TIMEOUT_WAIT_PHASES,
    ModelCallLedger,
    ModelCallStartedParams,
    ModelCallTimeoutParams,
)
from agent_py_agent.agent.conversation import decision_model_call
from agent_py_agent.tests.test_responses_websocket import (  # noqa: F401
    FakeConnection,
    completed_events,
    request,
)


# LLM: 生产落账函数 _record_provider_timeout 还会向追踪与指标发一次外部副作用；本用例只关心
#   账本那几个结构化字段，这两个副作用与判定无关，替身让测试不碰真实追踪/指标通道。
# 函数用途: 把端到端用例里与账本无关的两个外部副作用替换成空操作。
def _mock_timeout_side_effects(monkeypatch) -> None:
    monkeypatch.setattr(generation, "_trace_model_failure", lambda request, exc: None)
    monkeypatch.setattr(generation, "publish_model_metrics", lambda agent, params, pending=False: None)


# ---------------------------------------------------------------- 1. 异常层：两个入口都带对值


def test_handshake_timeout_keeps_first_event_stage_and_marks_handshake(monkeypatch):
    """握手超时：stage=first_event（修法 A 的回归规则一字不改），wait_phase=handshake。"""
    monkeypatch.setattr(
        ws,
        "_connect",
        lambda req: (_ for _ in ()).throw(TimeoutError("The handshake operation timed out")),
    )
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(max_retries=0)))
    assert caught.value.stage == "first_event"
    assert caught.value.wait_phase == "handshake"


def test_post_open_first_event_timeout_has_no_handshake_mark(monkeypatch):
    """连接打开、response.create 已发出之后等首事件超时：stage 仍 first_event，但不带握手标记。"""
    connection = FakeConnection([])
    monkeypatch.setattr(ws, "_connect", lambda req: connection)
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(timeout=1, first_event_timeout=0.05)))
    assert caught.value.stage == "first_event"
    assert caught.value.wait_phase == ""
    assert connection.sent  # 证据：这次超时发生在请求已经发出去之后


def test_post_open_idle_timeout_has_no_handshake_mark(monkeypatch):
    """首事件之后数据流空闲超时：stage=stream_idle，同样不带握手标记。"""
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([completed_events()[0]]))
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(timeout=0.05)))
    assert caught.value.stage == "stream_idle"
    assert caught.value.wait_phase == ""


def test_default_stage_and_wait_phase_keep_legacy_readers_working():
    """不传新参数时行为与从前一致：stage 默认 provider_wall，wait_phase 默认空串。"""
    error = ProviderTimeoutError("模型接口流式响应空闲超时")
    assert error.stage == "provider_wall"
    assert error.wait_phase == ""


# ---------------------------------------------------------------- 2. 合同层：两个入口都 fail-closed


def test_error_constructor_rejects_unknown_wait_phase():
    """异常构造对未登记位置 fail-closed，防止新路径污染统计口径。"""
    with pytest.raises(ValueError, match="未知 ProviderTimeoutError wait_phase"):
        ProviderTimeoutError("超时", stage="first_event", wait_phase="tls")


def test_error_constructor_still_rejects_unknown_stage():
    """stage 的既有封闭集合不动：未登记值照旧抛错。"""
    with pytest.raises(ValueError, match="未知 ProviderTimeoutError stage"):
        ProviderTimeoutError("超时", stage="handshake")


def test_ledger_timeout_rejects_unknown_wait_phase():
    """账本写入侧同样 fail-closed：旁路调用写不进未登记位置。"""
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="c1", backend="b", model="m", input_tokens=1))
    with pytest.raises(ValueError, match="未知 timeout_wait_phase"):
        ledger.timeout(
            ModelCallTimeoutParams(
                call_id="c1", timeout_seconds=1.0, timeout_stage="first_event", timeout_wait_phase="tls"
            )
        )


def test_wait_phase_vocabulary_is_a_closed_subset_of_stages_free_contract():
    """wait_phase 集合是独立封闭集合，且不改变 stage 集合（回归规则的结构性证据）。"""
    assert frozenset({"", "handshake"}) == TIMEOUT_WAIT_PHASES
    assert frozenset(
        {"first_event", "stream_idle", "wall_clock", "provider_declared", "provider_wall"}
    ) == TIMEOUT_STAGES
    assert "handshake" not in TIMEOUT_STAGES  # 握手不是 stage，只是诊断位置


# ---------------------------------------------------------------- 3. 账本层：字段真的读得到


def test_ledger_records_wait_phase_and_exports_it_in_to_dict():
    """账本记录与 to_dict 都能读到 timeout_wait_phase；stage 同时保持 first_event。"""
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="c1", backend="b", model="m", input_tokens=1))
    record = ledger.timeout(
        ModelCallTimeoutParams(
            call_id="c1", timeout_seconds=3.0, timeout_stage="first_event", timeout_wait_phase="handshake"
        )
    )
    assert record.timeout_stage == "first_event"
    assert record.timeout_wait_phase == "handshake"
    assert record.to_dict()["timeout_wait_phase"] == "handshake"
    assert record.to_dict()["timeout_stage"] == "first_event"


def test_ledger_default_wait_phase_stays_empty_for_other_timeouts():
    """别的超时（这里用 stream_idle）不传位置时，账本里是空串而不是被猜出来的值。"""
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="c2", backend="b", model="m", input_tokens=1))
    record = ledger.timeout(
        ModelCallTimeoutParams(call_id="c2", timeout_seconds=3.0, timeout_stage="stream_idle")
    )
    assert record.timeout_wait_phase == ""
    assert record.to_dict()["timeout_wait_phase"] == ""


# ---------------------------------------------------------------- 4. 端到端：握手异常一路走到账本


def test_handshake_timeout_reaches_the_ledger_with_its_wait_phase(monkeypatch):
    """端到端（同进程内）：握手超时异常经生产落账函数写进账本，stage=first_event、wait_phase=handshake。

    这里刻意调用生产代码 ``_record_provider_timeout``（而不是在测试里重抄一遍取值逻辑），
    所以生成层若不再从异常上取 ``wait_phase``，本用例会真的变红。
    """
    monkeypatch.setattr(
        ws,
        "_connect",
        lambda req: (_ for _ in ()).throw(TimeoutError("The handshake operation timed out")),
    )
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(max_retries=0)))
    exc = caught.value

    _mock_timeout_side_effects(monkeypatch)
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="call-1", backend="ws", model="m", input_tokens=10))
    generation._record_provider_timeout(
        SimpleNamespace(  # 只提供落账函数真正会读的四个结构化字段
            ledger=ledger, call_id="call-1", exc=exc,
            request=SimpleNamespace(agent=None, params=None, tool_rounds=0),
            first_token_timeout_seconds=30.0,
        )
    )
    stored = ledger.records()[0]
    assert stored.status == "timed_out"
    assert stored.timeout_stage == "first_event"
    assert stored.timeout_wait_phase == "handshake"
    assert json.dumps(stored.to_dict())  # 账本明细可序列化，字段不会在导出时丢失


def test_post_open_timeout_reaches_the_ledger_without_a_wait_phase(monkeypatch):
    """对照：连接打开后的首事件超时落账后，wait_phase 是空串——两种超时在账本里真的能分开。"""
    monkeypatch.setattr(ws, "_connect", lambda req: FakeConnection([]))
    with pytest.raises(ProviderTimeoutError) as caught:
        list(ws.iter_responses_websocket(request(timeout=1, first_event_timeout=0.05)))
    exc = caught.value

    _mock_timeout_side_effects(monkeypatch)
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="call-2", backend="ws", model="m", input_tokens=10))
    generation._record_provider_timeout(
        SimpleNamespace(
            ledger=ledger, call_id="call-2", exc=exc,
            request=SimpleNamespace(agent=None, params=None, tool_rounds=0),
            first_token_timeout_seconds=30.0,
        )
    )
    stored = ledger.records()[0]
    assert stored.timeout_stage == "first_event"
    assert stored.timeout_wait_phase == ""


def test_decision_error_records_provider_wait_phase_in_ledger():
    """决策路径把 ProviderTimeoutError 的诊断等待位置直接写入原调用账本。"""
    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="decision-1", backend="decision", model="test", input_tokens=1))
    started_at = time.monotonic()
    error = ProviderTimeoutError("等待模型响应超时", stage="first_event", wait_phase="handshake")

    decision_model_call._record_error(
        ledger, "decision-1", error, started_at=started_at, deadline=started_at + 2.0,
    )

    record = ledger.records()[0]
    assert record.status == "timed_out"
    assert record.timeout_stage == "first_event"
    assert record.timeout_wait_phase == "handshake"
