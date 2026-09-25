# LLM: 决策侧走真实本地 HTTP 与原账本；Gateway 收尾只替换 pid/停止请求/心跳/事件等文件副作用，核对调用顺序与失败隔离。
#   每个测试先把进程关闭标记复位（monkeypatch 在收尾还原），不影响同进程其他测试。
# 模块用途: 验证 Gateway 停止时主动取消本进程在途决策（P4-F）：立即回原方案、不冒充用户停止、不进冷却、之后不再发新决策。
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model.call_runtime import model_call_summary
from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.cli import gateway_process
from agent_py_agent.tests.test_decision_service_http import configured, decide, server  # noqa: F401
from agent_py_agent.tests.test_decision_settings import patch


# LLM: 关闭标记是进程级且不复位的宿主事实；测试里用 monkeypatch 置回 False，收尾自动还原原值。
# 函数用途: 让每个测试从“宿主未关闭”开始。
@pytest.fixture(autouse=True)
def fresh_host(monkeypatch):
    monkeypatch.setattr(decision_policy, "_HOST_SHUTDOWN", False)


# LLM: call 是无参闭包，保持辅助函数参数有界；只等本地服务确认请求已到达，不替被测对象做任何事。
# 函数用途: 在后台线程里发一次决策，等请求真正到达本地服务后返回 future。
def _in_flight(pool, server, call):  # noqa: F811
    future = pool.submit(call)
    assert server.entered.wait(1)
    return future


def test_shutdown_cancels_an_in_flight_decision_back_to_the_original_plan(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, _thread, stage = configured(tmp_path, server, timeout=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _in_flight(pool, server, lambda: decide(host, params, stage))
        started = time.monotonic()
        assert decision_policy.cancel_active_decisions_for_shutdown() == 1
        outcome = future.result(timeout=0.8)
        elapsed = time.monotonic() - started
        server.release.set()
    assert (outcome.status, outcome.reason) == ("stale", "host_shutdown") and not outcome.may_apply
    assert elapsed < 0.8, "取消后调用方必须立即返回，不能等到 2 秒期限"
    summary = model_call_summary(host, request_id=params.request_id)
    assert summary["status_counts"].get("failed") == 1 and not summary["status_counts"].get("running")
    assert host.config.model_name == "deployment-model"


def test_shutdown_cancellation_does_not_start_a_connection_cooldown(tmp_path, server, monkeypatch):  # noqa: F811
    server.block = True
    host, params, _thread, stage = configured(tmp_path, server, timeout=2)
    before = dict(decision_policy._FAILURES)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _in_flight(pool, server, lambda: decide(host, params, stage))
        decision_policy.cancel_active_decisions_for_shutdown()
        future.result(timeout=0.8)
        server.release.set()
    assert dict(decision_policy._FAILURES) == before, "宿主关闭不是连接故障，不能新增冷却记录"
    monkeypatch.setattr(decision_policy, "_HOST_SHUTDOWN", False)
    server.block = False
    again = decide(host, params, stage)
    assert again.status == "success", "关闭标记复位后同一连接照常可用，没有被冷却挡住"


def test_after_shutdown_new_decisions_return_without_sending(tmp_path, server):  # noqa: F811
    host, params, _thread, stage = configured(tmp_path, server)
    assert decision_policy.cancel_active_decisions_for_shutdown() == 0
    outcome = decide(host, params, stage)
    assert (outcome.status, outcome.reason) == ("stale", "host_shutdown") and not outcome.may_apply
    assert server.requests == [] and decision_policy.host_shutdown_started()


def test_settings_revocation_is_reported_first_when_both_happen(tmp_path, server):  # noqa: F811
    server.block = True
    host, params, thread, stage = configured(tmp_path, server, timeout=2)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = _in_flight(pool, server, lambda: decide(host, params, stage))
        patch(host, {"points.subagent_model.mode": "observe"}, thread_id=thread.thread_id)
        decision_policy.cancel_active_decisions_for_shutdown()
        outcome = future.result(timeout=0.8)
        server.release.set()
    assert (outcome.status, outcome.reason) == ("stale", "settings_changed") and not outcome.may_apply


def test_capacity_refusal_keeps_its_own_reason_before_shutdown(tmp_path, server, monkeypatch):  # noqa: F811
    host, params, _thread, stage = configured(tmp_path, server)
    monkeypatch.setattr(decision_policy, "_MAX_ACTIVE", 0)
    outcome = decide(host, params, stage)
    assert (outcome.status, outcome.reason) == ("error", "notification_capacity") and server.requests == []


# LLM: 只替换收尾里的文件与日志副作用；线程未启动（ident 为 None）时原实现本就跳过 join。
# 函数用途: 构造一次 Gateway 收尾请求并记录关键步骤的先后顺序。
def _cleanup_request(monkeypatch, order, *, cancel):
    events = []
    monkeypatch.setattr(gateway_process, "remove_pid_file_if_owned", lambda *_args: None)
    monkeypatch.setattr(gateway_process, "remove_gateway_stop_request_if_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_process, "_gateway_context_process_identity", lambda _context: "identity")
    monkeypatch.setattr(gateway_process, "_write_gateway_heartbeat", lambda *_args, **_kwargs: order.append("heartbeat"))
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda _agent, name, payload: events.append((name, payload)))
    monkeypatch.setattr(decision_policy, "cancel_active_decisions_for_shutdown", cancel)

    class StopEvent(threading.Event):
        # 函数用途: 记录停止事件被置位的时刻。
        def set(self):
            order.append("stop_event")
            super().set()

    http = SimpleNamespace(stop=lambda: order.append("http_stop"))
    context = SimpleNamespace(paths=SimpleNamespace(pid="pid", stop_request="stop"), agent=object(), process_started_at=0.0)
    request = SimpleNamespace(context=context, pid=1, stop_event=StopEvent(), heartbeat_thread=threading.Thread(target=None),
                              request_thread=threading.Thread(target=None), background_thread=threading.Thread(target=None),
                              http_server=http, termination_status="stopped", termination_reason="test")
    return request, events


def test_gateway_cleanup_cancels_decisions_after_the_stop_event_and_before_http(monkeypatch):
    order = []
    request, events = _cleanup_request(monkeypatch, order, cancel=lambda: order.append("cancel_decisions") or 0)
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert order[:3] == ["stop_event", "cancel_decisions", "http_stop"] and report["drain_complete"]
    assert [name for name, _payload in events] == ["gateway_run_cleanup"]


def test_gateway_cleanup_survives_a_failing_decision_module(monkeypatch):
    order = []

    def broken():
        raise RuntimeError("secret detail that must not be logged")

    request, events = _cleanup_request(monkeypatch, order, cancel=broken)
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert order[:2] == ["stop_event", "http_stop"] and "heartbeat" in order and report["drain_complete"]
    failed = [payload for name, payload in events if name == "gateway_decision_cancel_failed"]
    assert failed == [{"error_type": "RuntimeError"}], "只记异常类型，不记异常正文"
