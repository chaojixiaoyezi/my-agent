"""合法排队等待的结构化信号(admission_wait):慢模型下"上一条长回合占着车道、下一条排队"。

要求:排队等准入期间,worker 必须在**请求文件**上持续写结构化等待事实,让客户端只按机器可观测
活动续期,从而不把"合法排队"误判成等待超时;同时:

  · 只写等待事实(admission_wait_*),绝不写 status/lease/model 字段,不伪装成模型已推进;
  · 有节流(不会每次扫描都写)且有总预算(超过就停写,客户端在自己的空闲窗口内如实收口);
  · 取消/终态/网关失活/崩溃恢复仍然收口:文件消失或出现终态记录后客户端不再被续期。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    poll_gateway_chunks,
)


def _paths(tmp_path: Path) -> GatewayPaths:
    root = tmp_path / "gw"
    return GatewayPaths(
        root=root,
        pid=root / "gateway.pid",
        adapter_pid=root / "adapter.pid",
        state=root / "gateway_state.json",
        heartbeat=root / "gateway_heartbeat.json",
        stop_request=root / "gateway_stop.request",
        log=root / "gateway.log",
        inbox=root / "requests" / "pending",
        processing=root / "requests" / "processing",
        done=root / "requests" / "done",
        failed=root / "requests" / "failed",
        responses=root / "responses",
        history=root / "history.jsonl",
    )


@pytest.fixture()
def fresh_admission(monkeypatch):
    admission = rw.GatewayAdmission()
    monkeypatch.setattr(rw, "admission", admission)
    return admission


def _enqueue(paths: GatewayPaths, request_id: str, user: str = "u-1", *, created_at: float | None = None) -> Path:
    paths.inbox.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": request_id,
        "kind": "ask",
        "goal": "g",
        "user_id": user,
        "status": "queued",
        "created_at": time.time() if created_at is None else created_at,
        "conversation": {
            "canonical_user_id": user,
            "channel": "chat",
            "channel_conversation_id": f"conv-{user}",
            "channel_user_id": user,
        },
    }
    path = paths.inbox / f"{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _limits(*, budget: float = 3600.0) -> rw.AdmissionLimits:
    # 同会话顺序键保证第二个请求必然被限流(conversation_busy),不需要占满全局坑。
    return rw.AdmissionLimits(
        user_inflight=8, global_inflight=500, admission_wait_budget_seconds=budget
    )


def _hold_conversation(admission, payload: dict) -> None:
    """占住该请求的会话车道(模拟长回合仍在跑),后续扫描必然把它判为排队。"""
    user_key = rw.request_user_key(payload)
    conversation_key = rw.request_conversation_key(payload)
    assert admission.try_acquire(
        user_key, 8, 500, conversation_key=conversation_key
    ), "占位失败:测试前置不成立"


# --------------------------------------------------------------------------------------
# 服务端:结构化等待信号
# --------------------------------------------------------------------------------------
def test_blocked_request_records_structured_admission_wait(tmp_path, fresh_admission) -> None:
    paths = _paths(tmp_path)
    queued = _enqueue(paths, "req-queued", "u-1")
    _hold_conversation(fresh_admission, _payload(queued))

    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)

    payload = _payload(queued)
    assert payload["admission_wait_reason"] == "conversation_busy"
    assert payload["admission_wait_at"] > 0
    assert payload["admission_wait_count"] == 1
    assert payload["admission_wait_since"] > 0
    # 只写等待事实:不得伪装成已认领/模型在推进
    assert payload["status"] == "queued"
    assert "lease_heartbeat_at" not in payload
    assert "lease_started_at" not in payload
    assert "lease_epoch" not in payload
    assert "execution_attempt_id" not in payload


def test_admission_wait_write_is_cadenced(tmp_path, fresh_admission) -> None:
    paths = _paths(tmp_path)
    queued = _enqueue(paths, "req-queued", "u-1")
    _hold_conversation(fresh_admission, _payload(queued))

    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
    first = _payload(queued)["admission_wait_count"]

    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
    assert _payload(queued)["admission_wait_count"] == first, "节流窗口内不得重复写"

    # 把上次信号时间推到节流窗口外 → 必须再写一次
    payload = _payload(queued)
    payload["admission_wait_at"] = time.time() - rw._ADMISSION_WAIT_REFRESH_SECONDS - 1  # noqa: SLF001
    queued.write_text(json.dumps(payload), encoding="utf-8")
    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
    assert _payload(queued)["admission_wait_count"] == first + 1


def test_admission_wait_stops_after_budget_and_marks_expiry(tmp_path, fresh_admission) -> None:
    paths = _paths(tmp_path)
    queued = _enqueue(paths, "req-queued", "u-1", created_at=time.time() - 7200)
    _hold_conversation(fresh_admission, _payload(queued))

    rw.dispatch_pending_requests(paths, _limits(budget=60.0), lambda p, u, c: None)

    payload = _payload(queued)
    assert payload.get("admission_wait_expired_at", 0) > 0, "超预算必须留结构化过期事实"
    assert "admission_wait_at" not in payload, "超预算后不得再续期"

    # 再扫描一次:过期事实只写一次
    expired_at = payload["admission_wait_expired_at"]
    rw.dispatch_pending_requests(paths, _limits(budget=60.0), lambda p, u, c: None)
    assert _payload(queued)["admission_wait_expired_at"] == expired_at


def test_deferred_retry_requests_also_get_wait_signal(tmp_path, fresh_admission) -> None:
    paths = _paths(tmp_path)
    path = _enqueue(paths, "req-deferred", "u-1")
    payload = _payload(path)
    payload["not_before_at"] = time.time() + 30
    path.write_text(json.dumps(payload), encoding="utf-8")

    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)

    updated = _payload(path)
    assert updated["admission_wait_reason"] == "retry_backoff"
    assert updated["admission_wait_at"] > 0


def test_claimed_request_gets_no_admission_wait_fields(tmp_path, fresh_admission) -> None:
    """被正常认领的请求不得带等待字段(等待信号只属于真的在排队的那一个)。"""
    paths = _paths(tmp_path)
    path = _enqueue(paths, "req-solo", "u-1")
    claimed: list[str] = []

    rw.dispatch_pending_requests(
        paths, _limits(), lambda claim, u, c: claimed.append(str(claim.request_id))
    )

    assert claimed == ["req-solo"]
    assert not path.exists()


# --------------------------------------------------------------------------------------
# 客户端:只按机器可观测活动续期
# --------------------------------------------------------------------------------------
def _terminal(path: Path, request_id: str, response: str) -> None:
    terminal_response = {"id": request_id, "ok": True, "response": response, "status": "done"}
    path.write_text(
        json.dumps(
            {
                "schema_version": "gateway_terminal_request.v1",
                "id": request_id,
                "status": "done",
                "turn_phase": "closed",
                "terminal_response": terminal_response,
            }
        ),
        encoding="utf-8",
    )


def _client_wait(paths: GatewayPaths, request_id: str, *, window: float) -> dict:
    chunk_path = paths.processing / f"{request_id}.chunks.jsonl"
    terminal_path = paths.responses / f"{request_id}.json"
    return poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            terminal_path,
            time.time() + 0.05,  # 初始 deadline 早于真实完成时间
            lambda _chunk: False,
            [0],
            activity_paths=(
                paths.inbox / f"{request_id}.json",
                paths.processing / f"{request_id}.json",
                chunk_path,
            ),
            inactivity_timeout_seconds=window,
        )
    )


def test_client_keeps_waiting_while_worker_signals_admission_wait(tmp_path) -> None:
    """排队超过初始 deadline:只要 worker 在写等待信号,客户端就必须继续等(不误判超时)。"""
    paths = _paths(tmp_path)
    request_id = "req-long-queue"
    queued = _enqueue(paths, request_id)
    terminal_path = paths.responses / f"{request_id}.json"
    paths.responses.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def worker() -> None:
        # 模拟被长回合占住车道期间的真实 worker:周期性写等待信号,最后给出终态
        for index in range(6):
            if stop.is_set():
                return
            payload = _payload(queued)
            payload["admission_wait_at"] = time.time()
            payload["admission_wait_reason"] = "conversation_busy"
            payload["admission_wait_count"] = index + 1
            queued.write_text(json.dumps(payload), encoding="utf-8")
            time.sleep(0.06)
        _terminal(terminal_path, request_id, "排队后完成")

    thread = threading.Thread(target=worker)
    thread.start()
    response = _client_wait(paths, request_id, window=0.15)
    stop.set()
    thread.join(timeout=3)

    assert response, "有结构化等待信号的排队不得被客户端判成超时"
    assert response["response"] == "排队后完成"


def test_client_expires_when_worker_stops_signalling(tmp_path) -> None:
    """网关失活(停写):客户端必须在空闲窗口内收口,不允许无限续期。"""
    paths = _paths(tmp_path)
    request_id = "req-dead-queue"
    _enqueue(paths, request_id)

    started = time.monotonic()
    response = _client_wait(paths, request_id, window=0.15)
    elapsed = time.monotonic() - started

    assert response == {}
    assert elapsed < 3.0, f"停写后必须在窗口内收口,实际 {elapsed:.2f}s"


def test_client_closes_out_when_queued_request_is_cancelled(tmp_path) -> None:
    """取消:队列条目被移除且没有终态时,客户端同样在窗口内收口(不靠文件存在无限续期)。"""
    paths = _paths(tmp_path)
    request_id = "req-cancelled"
    queued = _enqueue(paths, request_id)

    def cancel() -> None:
        time.sleep(0.05)
        queued.unlink(missing_ok=True)

    thread = threading.Thread(target=cancel)
    thread.start()
    started = time.monotonic()
    response = _client_wait(paths, request_id, window=0.15)
    elapsed = time.monotonic() - started
    thread.join(timeout=2)

    assert response == {}
    assert elapsed < 3.0, f"取消后必须在窗口内收口,实际 {elapsed:.2f}s"


def test_real_worker_signal_keeps_real_client_waiting(tmp_path, fresh_admission, monkeypatch) -> None:
    """端到端(真实 worker + 真实客户端):车道被长回合占住时,排队请求仍能被客户端持续等到终态。

    这是本组最关键的一条:worker 侧真的调用 dispatch_pending_requests 写等待信号,客户端侧真的
    调用 poll_gateway_chunks 读活动。把 worker 的写入关掉,客户端就会在空闲窗口内收口(见下一条
    负向用例),因此两条一起才证明信号是必要条件而非装饰。
    """
    monkeypatch.setattr(rw, "_ADMISSION_WAIT_REFRESH_SECONDS", 0.0)  # 测试里不节流
    paths = _paths(tmp_path)
    request_id = "req-live-queue"
    queued = _enqueue(paths, request_id)
    _hold_conversation(fresh_admission, _payload(queued))
    terminal_path = paths.responses / f"{request_id}.json"
    paths.responses.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    rounds = {"n": 0}

    def worker() -> None:
        while not stop.is_set():
            rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
            rounds["n"] += 1
            if rounds["n"] >= 5:
                _terminal(terminal_path, request_id, "排队后完成")
                return
            time.sleep(0.04)

    thread = threading.Thread(target=worker)
    thread.start()
    response = _client_wait(paths, request_id, window=0.15)
    stop.set()
    thread.join(timeout=3)

    assert rounds["n"] >= 2, "worker 必须真的扫描过多轮(否则用例空转)"
    assert _payload(queued)["admission_wait_count"] >= 2, "worker 必须真的写过等待信号"
    assert response, "真实 worker 的等待信号必须让真实客户端继续等待"
    assert response["response"] == "排队后完成"


def test_without_worker_signal_the_same_client_wait_times_out(
    tmp_path, fresh_admission, monkeypatch
) -> None:
    """负向对照:关掉等待信号后,同样的排队场景会在空闲窗口内被判超时(证明信号是必要条件)。"""
    monkeypatch.setattr(rw, "_ADMISSION_WAIT_REFRESH_SECONDS", 0.0)
    monkeypatch.setattr(rw, "_record_admission_wait", lambda *a, **k: None)
    paths = _paths(tmp_path)
    request_id = "req-queue-no-signal"
    queued = _enqueue(paths, request_id)
    _hold_conversation(fresh_admission, _payload(queued))
    terminal_path = paths.responses / f"{request_id}.json"
    paths.responses.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
            time.sleep(0.04)

    thread = threading.Thread(target=worker)
    thread.start()
    started = time.monotonic()
    response = _client_wait(paths, request_id, window=0.15)
    elapsed = time.monotonic() - started
    stop.set()
    thread.join(timeout=2)

    assert response == {}
    assert elapsed < 3.0
    assert "admission_wait_at" not in _payload(queued)
