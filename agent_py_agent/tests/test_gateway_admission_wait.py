# LLM: 使用真实队列/文件锁验证扫描缓存不成为身份或claim权威；假时钟只控制terminal等待，不替代真实worker/client等待回归。
# 模块用途: 验证合法排队等待、精确字节缓存隔离及有界终态轮询。
"""合法排队等待的结构化信号(admission_wait):慢模型下"上一条长回合占着车道、下一条排队"。

要求:排队等准入期间,worker 必须在**请求文件**上持续写结构化等待事实,让客户端只按机器可观测
活动续期,从而不把"合法排队"误判成等待超时;同时:

  · 只写等待事实(admission_wait_*),绝不写 status/lease/model 字段,不伪装成模型已推进;
  · 有节流(不会每次扫描都写)且有总预算(超过就停写,客户端在自己的空闲窗口内如实收口);
  · 取消/终态/网关失活/崩溃恢复仍然收口:文件消失或出现终态记录后客户端不再被续期。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts import io as gateway_io
from agent_py_agent.agent.gateway_parts import request_worker as rw
from agent_py_agent.agent.gateway_parts.paths import GatewayPaths
from agent_py_agent.cli.chat_parts import gateway_client
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


def _limits() -> rw.AdmissionLimits:
    # 同会话顺序键保证请求必然被限流(conversation_busy),不需要占满全局坑。
    return rw.AdmissionLimits(user_inflight=8, global_inflight=500)


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


@pytest.mark.parametrize(
    "waited_seconds",
    [
        3601.0,  # 刚过 1 小时
        12 * 3600.0,  # 半天
        72 * 3600.0,  # 3 天
        30 * 24 * 3600.0,  # 30 天
    ],
)
def test_admission_wait_has_no_cumulative_lifetime(
    tmp_path, fresh_admission, waited_seconds
) -> None:
    """健康网关持续证明"仍在合法等待"时,累计等待多久都不判失败、不停止续期。

    只有"权威收口"(取消/终态/网关真的停写)才结束等待;不做"等够多久就前台报失败、后台照常执行"。
    """
    paths = _paths(tmp_path)
    created = time.time() - waited_seconds
    queued = _enqueue(paths, "req-queued", "u-1", created_at=created)
    _hold_conversation(fresh_admission, _payload(queued))
    payload = _payload(queued)
    # 预置"很久以前写过一次信号"的状态:节流窗口已过,应继续写新信号
    payload["admission_wait_since"] = created
    payload["admission_wait_at"] = created + 1
    payload["admission_wait_count"] = 1
    queued.write_text(json.dumps(payload), encoding="utf-8")

    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)

    updated = _payload(queued)
    assert updated["admission_wait_count"] == 2, "累计等待再久也必须继续发新的等待信号"
    assert updated["admission_wait_at"] > created + 1
    assert "admission_wait_expired_at" not in updated, "不得发明等待过期事实"
    assert updated["status"] == "queued"


def test_admission_wait_survives_many_throttled_rounds(tmp_path, fresh_admission, monkeypatch) -> None:
    """节流只决定"多久写一次",不决定"还能写多久":连续多轮扫描后信号必须一直存在。"""
    monkeypatch.setattr(rw, "_ADMISSION_WAIT_REFRESH_SECONDS", 0.0)
    paths = _paths(tmp_path)
    queued = _enqueue(paths, "req-queued", "u-1", created_at=time.time() - 10 * 24 * 3600)
    _hold_conversation(fresh_admission, _payload(queued))

    for _ in range(5):
        rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)

    payload = _payload(queued)
    assert payload["admission_wait_count"] == 5
    assert "admission_wait_expired_at" not in payload


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


def _client_wait(paths: GatewayPaths, request_id: str, *, window: float, initial: float = 0.05) -> dict:
    chunk_path = paths.processing / f"{request_id}.chunks.jsonl"
    terminal_path = paths.responses / f"{request_id}.json"
    return poll_gateway_chunks(
        GatewayChunkPollRequest(
            chunk_path,
            terminal_path,
            time.time() + initial,  # 初始 deadline 早于真实完成时间
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


# 函数用途: 把客户端的活动续期变成可观测事件：每次真的续期就放行一次 observed（首次调用先放行一次，
#   让 worker 在客户端开始采样前写下的第一条信号不至于卡住后续写入）。
def _extend_observed_by_worker(observed: threading.Semaphore):
    original_extend = gateway_client._extend_active_request_deadline
    calls = {"n": 0}

    def extend_and_notify(request, fingerprints, *, deadline):
        extended = original_extend(request, fingerprints, deadline=deadline)
        calls["n"] += 1
        if calls["n"] == 1 or extended > deadline:
            observed.release()
        return extended

    return extend_and_notify


def test_real_worker_signal_keeps_real_client_waiting(tmp_path, fresh_admission, monkeypatch) -> None:
    """端到端(真实 worker + 真实客户端):车道被长回合占住时,排队请求仍能被客户端持续等到终态。

    这是本组最关键的一条:worker 侧真的调用 dispatch_pending_requests 写等待信号,客户端侧真的
    调用 poll_gateway_chunks 读活动。把 worker 的写入关掉,客户端就会在空闲窗口内收口(见下一条
    负向用例),因此两条一起才证明信号是必要条件而非装饰。

    时序由客户端观测驱动,不靠墙钟:worker 每写一次等待信号都等客户端真的续期过一次再写下一次,
    第 8 次之后才写终态。客户端每次采样至少隔 0.1 秒,所以完成时间不少于 0.8 秒,远晚于 0.5 秒的
    初始 deadline——续期一失效,客户端在 0.5 秒内就收口(负向验证)。不活跃窗口 2 秒只给满载下的采样
    抖动留余量,过期语义由下一条负向用例单独锁定。
    """
    monkeypatch.setattr(rw, "_ADMISSION_WAIT_REFRESH_SECONDS", 0.0)  # 测试里不节流
    paths = _paths(tmp_path)
    request_id = "req-live-queue"
    queued = _enqueue(paths, request_id)
    _hold_conversation(fresh_admission, _payload(queued))
    terminal_path = paths.responses / f"{request_id}.json"
    paths.responses.mkdir(parents=True, exist_ok=True)
    observed = threading.Semaphore(0)
    monkeypatch.setattr(gateway_client, "_extend_active_request_deadline", _extend_observed_by_worker(observed))
    stop = threading.Event()
    rounds = {"n": 0}

    def worker() -> None:
        while not stop.is_set():
            rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
            rounds["n"] += 1
            if rounds["n"] >= 8:
                _terminal(terminal_path, request_id, "排队后完成")
                return
            if not observed.acquire(timeout=5.0):  # 等客户端观测到这次信号;5 秒只是防挂起
                return

    thread = threading.Thread(target=worker)
    thread.start()
    response = _client_wait(paths, request_id, window=2.0, initial=0.5)
    stop.set()
    thread.join(timeout=5)

    assert rounds["n"] == 8, "worker 必须真的扫描过多轮并写下终态(否则用例空转)"
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


def test_legacy_request_without_queue_timestamp_keeps_stable_order(
    tmp_path, fresh_admission
) -> None:
    """老请求没有进队时间戳时,等待信号的写入不得让它每轮都被排到更后面(制造饥饿)。"""
    paths = _paths(tmp_path)
    path = _enqueue(paths, "req-legacy", "u-1")
    payload = _payload(path)
    for key in ("created_at", "submitted_at", "requeued_at"):
        payload.pop(key, None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    original_mtime = path.stat().st_mtime
    _hold_conversation(fresh_admission, payload)

    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
    first = _payload(path)
    assert first["created_at"] == original_mtime, "首次写信号必须钉住进队顺序位"

    time.sleep(0.02)
    rw.dispatch_pending_requests(paths, _limits(), lambda p, u, c: None)
    assert _payload(path)["created_at"] == first["created_at"], "顺序位不得随后续信号漂移"


# LLM: 每条缓存回归独占字典，旧实现也能建立fixture并以行为断言失败，不制造缺失属性setup错误。
# 函数用途: 隔离进程级扫描缓存，不改生产锁或权威reader。
@pytest.fixture
def pending_scan_cache(monkeypatch):
    cache = OrderedDict()
    monkeypatch.setattr(rw, "_PENDING_SCAN_CACHE", cache, raising=False)
    return cache


# LLM: 不替换rw的reader身份，否则会误走兼容旁路；计数真实JSON解析和原文件事务锁。
# 函数用途: 观测扫描是否真正省掉重解析与文件锁。
@pytest.fixture
def pending_io_counts(monkeypatch):
    counts = {"loads": 0, "locks": 0}
    loads, locked = json.loads, gateway_io._locked_json_path

    # LLM: 保持原解析行为与参数，不能用伪payload制造缓存命中。
    # 函数用途: 记录JSON解析次数。
    def counted_loads(*args, **kwargs):
        counts["loads"] += 1
        return loads(*args, **kwargs)

    # LLM: 真正进入既有文件锁，并完整转发blocking等参数。
    # 函数用途: 记录文件事务取锁次数。
    @contextmanager
    def counted_lock(path, **kwargs):
        counts["locks"] += 1
        with locked(path, **kwargs):
            yield

    monkeypatch.setattr(json, "loads", counted_loads)
    monkeypatch.setattr(gateway_io, "_locked_json_path", counted_lock)
    return counts


# LLM: 常量为真实生产默认值；不能靠测试fixture补上预算而遗漏生产声明。
# 函数用途: 钉住LRU条目及原字节预算合同。
def test_pending_cache_default_budgets():
    assert getattr(rw, "_PENDING_SCAN_CACHE_MAX_ENTRIES", None) == 1024
    assert getattr(rw, "_PENDING_SCAN_CACHE_MAX_BYTES", None) == 4 * 1024 * 1024


# LLM: 首次真实读取后冻结计数；扫描与单文件排序均不得再次解析或取文件锁。
# 函数用途: 证明不变请求确实命中缓存，而非只创建一个闲置字典。
def test_pending_unchanged_scan_skips_json_and_file_lock(tmp_path, pending_scan_cache, pending_io_counts):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "cached", created_at=10)
    first = rw._iter_pending_requests(paths)
    warm = dict(pending_io_counts)
    assert warm["loads"] > 0 and warm["locks"] > 0
    second = rw._iter_pending_requests(paths)
    assert rw._pending_request_sort_key(path) == (1, 10.0, path.name)
    assert pending_io_counts == warm
    assert first[0].payload == second[0].payload
    assert len(pending_scan_cache) == 1
    assert next(iter(pending_scan_cache))[-1] == path.read_bytes()


# LLM: 原位同长改写保inode并恢复mtime；额外冻结完整stat，证明原字节本身参与命中而非仅靠ctime兜底。
# 函数用途: 相同stat身份也必须读到新的owner-b。
@pytest.mark.parametrize("freeze_stat", [False, True])
def test_pending_same_stat_key_rewrite_still_reads_fresh(tmp_path, monkeypatch, pending_scan_cache, freeze_stat):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "same-stat", "owner-a", created_at=10)
    assert rw._iter_pending_requests(paths)[0].payload["user_id"] == "owner-a"
    before = path.stat()
    raw = path.read_bytes()
    rewritten = raw.replace(b"owner-a", b"owner-b")
    assert rewritten != raw and len(rewritten) == len(raw)
    with path.open("r+b") as handle:
        handle.write(rewritten)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if freeze_stat:
        original_stat = Path.stat
        monkeypatch.setattr(Path, "stat", lambda self, *a, **kw: before if self == path else original_stat(self, *a, **kw))
    assert rw._iter_pending_requests(paths)[0].payload["user_id"] == "owner-b"


# LLM: 同时污染miss返回值和hit返回值，dict/list都必须隔离，不只拷贝最外层。
# 函数用途: 保证跨扫描的嵌套payload不共享可变对象。
def test_pending_nested_payload_is_not_shared_across_scans(tmp_path, pending_scan_cache):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "nested", "owner-a")
    payload = _payload(path)
    payload["metadata"] = {"nested": [{"value": "clean"}]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    first = rw._iter_pending_requests(paths)[0]
    first.payload["conversation"]["channel_conversation_id"] = "polluted"
    first.payload["metadata"]["nested"][0]["value"] = "polluted"
    second = rw._iter_pending_requests(paths)[0]
    assert second.payload["conversation"]["channel_conversation_id"] == "conv-owner-a"
    assert second.payload["metadata"]["nested"] == [{"value": "clean"}]
    second.payload["metadata"]["nested"].append({"value": "second pollution"})
    assert rw._iter_pending_requests(paths)[0].payload == payload


# LLM: 深拷贝递归也不能持有LRU锁；使用非阻塞取锁观察，不让错误实现挂住pytest。
# 函数用途: 保证保存和命中返回的深拷贝都在缓存锁外执行。
def test_pending_deepcopy_runs_outside_cache_lock(tmp_path, monkeypatch, pending_scan_cache):
    paths = _paths(tmp_path)
    _enqueue(paths, "copy")
    original, copied = copy.deepcopy, []

    # LLM: 仅断言缓存锁此刻空闲，随后执行真实递归深拷贝。
    # 函数用途: 捕捉锁内深拷贝这一性能回归。
    def checked_copy(value, *args, **kwargs):
        assert rw._PENDING_SCAN_CACHE_LOCK.acquire(blocking=False)
        rw._PENDING_SCAN_CACHE_LOCK.release()
        copied.append(True)
        return original(value, *args, **kwargs)

    monkeypatch.setattr(copy, "deepcopy", checked_copy)
    rw._iter_pending_requests(paths)
    rw._iter_pending_requests(paths)
    assert copied


# LLM: 确认读取前文件再次变化时，不能把旧reader快照绑定到新字节；Python的True==1也不能冒充类型未变。
# 函数用途: 验证reader与确认字节不一致时拒绝缓存。
@pytest.mark.parametrize("change", ["owner", "numeric_type"])
def test_pending_confirmation_rejects_changed_file(tmp_path, monkeypatch, pending_scan_cache, change):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "confirm", "owner-a")
    payload = _payload(path)
    payload["metadata"] = {"flag": True}
    path.write_text(json.dumps(payload), encoding="utf-8")
    locked, changed = gateway_io._locked_json_path, []

    # LLM: 原锁内读完后模拟外部写入一次；后续读取仍走真实reader，不替换其身份。
    # 函数用途: 精确触发权威读取与缓存确认间的竞争。
    @contextmanager
    def rewrite_after_read(current, **kwargs):
        with locked(current, **kwargs):
            yield
        if current == path and not changed:
            changed.append(True)
            update = {**payload, "user_id": "owner-b"} if change == "owner" else {**payload, "metadata": {"flag": 1}}
            path.write_text(json.dumps(update), encoding="utf-8")

    monkeypatch.setattr(gateway_io, "_locked_json_path", rewrite_after_read)
    rw._iter_pending_requests(paths)
    assert not pending_scan_cache
    second = rw._iter_pending_requests(paths)[0].payload
    if change == "owner":
        assert second["user_id"] == "owner-b"
    else:
        assert type(second["metadata"]["flag"]) is int


# LLM: 缩小单一预算分别施压；命中a后b必须成为最旧项，不以FIFO或只限条目冒充LRU双预算。
# 函数用途: 验证条目与原字节上限各自独立淘汰。
@pytest.mark.parametrize("budget", ["entries", "bytes"])
def test_pending_cache_lru_limits(tmp_path, monkeypatch, pending_scan_cache, budget):
    paths = _paths(tmp_path)
    a, b, c = [_enqueue(paths, name, created_at=10) for name in ("a", "b", "c")]
    monkeypatch.setattr(rw, "_PENDING_SCAN_CACHE_MAX_ENTRIES", 2 if budget == "entries" else 1024, raising=False)
    monkeypatch.setattr(rw, "_PENDING_SCAN_CACHE_MAX_BYTES", len(a.read_bytes()) * 2 if budget == "bytes" else 4 * 1024 * 1024, raising=False)
    rw._pending_request_sort_key(a)
    rw._pending_request_sort_key(b)
    rw._pending_request_sort_key(a)
    assert [key[0] for key in pending_scan_cache] == [str(b), str(a)]
    rw._pending_request_sort_key(c)
    assert [key[0] for key in pending_scan_cache] == [str(a), str(c)]
    assert sum(len(key[-1]) for key in pending_scan_cache) <= rw._PENDING_SCAN_CACHE_MAX_BYTES


# LLM: 第二个inbox故意共用字符串前缀；prune只能清本目录消失项，不能串根或把claimed缓存永久留下。
# 函数用途: 检查删除/认领后的扫描清理与相邻队列隔离。
@pytest.mark.parametrize("removal", ["delete", "claim"])
def test_pending_cache_prunes_removed_or_claimed_files(tmp_path, pending_scan_cache, removal):
    paths = _paths(tmp_path)
    neighbor = replace(paths, inbox=paths.inbox.with_name("pending-neighbor"))
    path = _enqueue(paths, "gone")
    other = _enqueue(neighbor, "kept")
    rw.ensure_gateway_folders(paths)
    rw._iter_pending_requests(paths)
    rw._iter_pending_requests(neighbor)
    assert {key[0] for key in pending_scan_cache} == {str(path), str(other)}
    if removal == "claim":
        assert rw.claim_request(paths, path) is not None
    else:
        path.unlink()
    assert rw._iter_pending_requests(paths) == []
    assert {key[0] for key in pending_scan_cache} == {str(other)}


# LLM: 被monkeypatch的reader哪怕返回只有payload的对象，也必须原样调用，不能先命中热缓存或读不存在的报告字段。
# 函数用途: 守住既有reader注入及扫描/排序context兼容性。
def test_pending_patched_reader_bypasses_warm_cache(tmp_path, monkeypatch, pending_scan_cache):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "patched", "owner-a")
    rw._iter_pending_requests(paths)
    injected, contexts = {"user_id": "owner-b", "created_at": 42}, []

    # LLM: 故意不提供ok/load_error/lock_busy，旁路必须只读取payload。
    # 函数用途: 模拟外部测试的reader替身。
    def reader(current, *, context):
        assert current == path
        contexts.append(context)
        return SimpleNamespace(payload=injected)

    monkeypatch.setattr(rw, "read_json_file_report", reader)
    assert rw._iter_pending_requests(paths)[0].payload is injected
    assert rw._pending_request_sort_key(path) == (1, 42.0, path.name)
    assert contexts == ["gateway.worker.pending_scan.read", "gateway.worker.pending_priority.read"]


# LLM: 小文件边界按原始字节而非字符或解析后大小计量；大一字节必须继续原锁读，不能缓存。
# 函数用途: 验证64KiB单文件缓存上限。
@pytest.mark.parametrize("size", [65536, 65537])
def test_pending_cache_file_size_boundary(tmp_path, pending_scan_cache, size):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "size")
    raw = path.read_bytes()
    path.write_bytes(raw + b" " * (size - len(raw)))
    assert rw._iter_pending_requests(paths)[0].payload["id"] == "size"
    assert len(pending_scan_cache) == int(size == 65536)


# LLM: 错误、空对象和非对象都不能负缓存；每次仍走真实锁读保留已有错误分类。
# 函数用途: 验证不可缓存文件持续使用原读取路径。
@pytest.mark.parametrize("wire", [b"", b"{}", b"[]", b"{", b"\xff"])
def test_pending_invalid_or_empty_payload_is_not_cached(tmp_path, pending_scan_cache, pending_io_counts, wire):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "invalid")
    path.write_bytes(wire)
    for _ in range(2):
        assert rw._iter_pending_requests(paths)[0].payload == {}
    assert pending_io_counts["locks"] == 2
    assert not pending_scan_cache


# LLM: 快速原字节读取失败不能覆盖原reader仍能读到的合法payload，不把缓存探针失败变成请求丢失。
# 函数用途: 检查stat/bytes预读失败时的活读取回退。
def test_pending_raw_read_failure_falls_back(tmp_path, monkeypatch, pending_scan_cache):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "fallback", "owner-a")
    original, attempts = Path.read_bytes, []

    # LLM: 只使缓存预读故障；权威reader的UTF-8文本读取仍能成功。
    # 函数用途: 模拟缓存字节探针的OS错误。
    def unavailable(current):
        if current == path:
            attempts.append(True)
            raise PermissionError("raw probe unavailable")
        return original(current)

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    assert rw._iter_pending_requests(paths)[0].payload["user_id"] == "owner-a"
    assert attempts and not pending_scan_cache


# LLM: 生产报告没有ok属性；即便带payload，load_error/lock_busy也禁止填缓存，但不改变原payload返回合同。
# 函数用途: 保证暂时锁忙或损坏报告不会被提升成可信缓存。
@pytest.mark.parametrize("failure", ["load_error", "lock_busy"])
def test_pending_failed_report_is_not_cached(tmp_path, monkeypatch, pending_scan_cache, failure):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "report")
    payload = _payload(path)
    report = gateway_io.GatewayJsonReadReport(payload, {"type": "OSError"} if failure == "load_error" else None,
                                             lock_busy=failure == "lock_busy")
    # LLM: 与默认reader同对象以执行成功分类分支，不走monkeypatch身份旁路。
    # 函数用途: 返回带错误状态的真实报告类型。
    def reader(*args, **kwargs):
        return report

    monkeypatch.setattr(rw, "read_json_file_report", reader)
    monkeypatch.setattr(rw, "_DEFAULT_READ_JSON_FILE_REPORT", reader, raising=False)
    assert rw._iter_pending_requests(paths)[0].payload is payload
    assert not pending_scan_cache


# LLM: 各线程拿到的嵌套对象可各自改动；所有任务在fixture恢复前join，不能污染后续测试或制造后台空转。
# 函数用途: 真实多线程重复命中时仍保持payload隔离与LRU容器安全。
def test_pending_cache_parallel_scans_are_isolated(tmp_path, pending_scan_cache):
    paths = _paths(tmp_path)
    _enqueue(paths, "parallel", "owner-a")
    rw._iter_pending_requests(paths)
    barrier = threading.Barrier(4)

    # LLM: 仅修改本次返回副本，读取缓存本体的线程不得看到这些写入。
    # 函数用途: 并行施压深拷贝与缓存锁。
    def scan(index):
        barrier.wait(timeout=5)
        for _ in range(10):
            payload = rw._iter_pending_requests(paths)[0].payload
            assert payload["conversation"]["channel_conversation_id"] == "conv-owner-a"
            payload["conversation"]["channel_conversation_id"] = str(index)

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(scan, index) for index in range(4)]
        for future in futures:
            future.result(timeout=10)
    assert len(pending_scan_cache) == 1
    assert rw._iter_pending_requests(paths)[0].payload["conversation"]["channel_conversation_id"] == "conv-owner-a"


# LLM: 缓存只存payload，时间相关not_before每次现算；等待更新和claim都必须重新读取当前文件，不用扫描副本写回。
# 函数用途: 验证缓存不会冻结延迟、覆盖外来字段或在claim时复活旧owner。
def test_pending_cache_keeps_defer_update_and_claim_live(tmp_path, monkeypatch, pending_scan_cache):
    paths = _paths(tmp_path)
    rw.ensure_gateway_folders(paths)
    path = _enqueue(paths, "live", "owner-a", created_at=10)
    original = _payload(path)
    original["not_before_at"] = 120
    path.write_text(json.dumps(original), encoding="utf-8")
    clock = [100.0]
    monkeypatch.setattr(rw, "time", SimpleNamespace(time=lambda: clock[0]))
    first = rw._iter_pending_requests(paths)[0]
    assert rw._request_deferred_until_later(first) is True
    clock[0] = 121
    assert rw._request_deferred_until_later(rw._iter_pending_requests(paths)[0]) is False
    path.write_text(json.dumps({**original, "external_field": "keep"}), encoding="utf-8")
    assert rw._write_admission_wait_field(path, {"admission_wait_at": clock[0], "admission_wait_reason": "busy"}, expected_payload=first.payload)
    assert _payload(path)["external_field"] == "keep"
    path.write_text(json.dumps({**_payload(path), "user_id": "owner-b"}), encoding="utf-8")
    claim = rw.claim_request(paths, path)
    assert claim is not None and _payload(claim.path)["user_id"] == "owner-b"


# LLM: 只替换轮询时钟/reader，保留生产等待循环；墙钟跳跃故意与单调钟分离，缺单调属性也不能AttributeError。
# 函数用途: 建立可计量、不真实sleep的terminal等待现场。
def _wait_poll_probe(monkeypatch, *, mode="missing", ready_after=None):
    elapsed, sleeps, reads = [0.0], [], []
    response = {"id": "wait", "status": "done", "ok": True}

    # LLM: 每次推进恰好请求的时长，检查等待循环自身是否睡过deadline。
    # 函数用途: 记录睡眠并推进虚拟时钟。
    def sleep(delay):
        assert delay > 0
        sleeps.append(delay)
        elapsed[0] += delay

    # LLM: reader返回时机由调用次数决定，不改变等待循环的终态读取入口。
    # 函数用途: 记录canonical路径和context并按需返回终态。
    def read(path, *, request_id, context):
        reads.append((path, request_id, context))
        return response if ready_after is not None and len(reads) >= ready_after else {}

    clock = SimpleNamespace(time=lambda: elapsed[0], sleep=sleep)
    if mode == "none":
        clock.monotonic = None
    elif mode == "monotonic":
        clock.monotonic = lambda: elapsed[0]
        clock.time = lambda: 1_000_000 * len(sleeps)
    monkeypatch.setattr(rw, "time", clock)
    monkeypatch.setattr(rw, "read_gateway_terminal_response_file", read)
    return elapsed, sleeps, reads, response


# LLM: 零预算且建deadline后时钟已推进也必须先读一次现成终态，不能用while时间判断跳过首次读取。
# 函数用途: 证明准备好的终态无初始等待且不因timeout=0漏读。
def test_wait_response_ready_never_sleeps(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    clock = iter([100.0, 101.0])
    sleeps, reads = [], []
    response = {"id": "wait", "status": "done", "ok": True}
    monkeypatch.setattr(rw, "time", SimpleNamespace(time=lambda: next(clock), sleep=sleeps.append))
    monkeypatch.setattr(rw, "read_gateway_terminal_response_file", lambda *a, **kw: reads.append(kw) or response)
    assert rw.wait_for_gateway_response(paths, "wait", 0) == response
    assert len(reads) == 1 and sleeps == []


# LLM: 完整观察步长与最终截短，mono模式故意让墙钟巨跳，不能用墙钟提前中止本次等待。
# 函数用途: 守住20/50/100/200毫秒阶梯、200毫秒封顶及兼容时钟回退。
@pytest.mark.parametrize("mode", ["missing", "none", "monotonic"])
def test_wait_response_bounded_steps_and_clock_fallback(tmp_path, monkeypatch, mode):
    paths = _paths(tmp_path)
    elapsed, sleeps, reads, _response = _wait_poll_probe(monkeypatch, mode=mode)
    assert rw.wait_for_gateway_response(paths, "wait", 0.58) == {}
    assert sleeps == pytest.approx([0.02, 0.05, 0.1, 0.2, 0.2, 0.01])
    assert elapsed[0] == pytest.approx(0.58)
    assert len(reads) == len(sleeps) + 1
    assert all(row == (paths.terminal / "wait.json", "wait", "gateway.worker.terminal.read") for row in reads)


# LLM: 短超时末拍只能睡余量，非正超时仍读一拍但绝不sleep；不依赖真实调度器计时精度。
# 函数用途: 验证剩余预算截短与零/负预算立即收口。
@pytest.mark.parametrize("timeout,expected", [(0.05, [0.02, 0.03]), (0.01, [0.01]), (0.0, []), (-1.0, [])])
def test_wait_response_clamps_short_timeout(tmp_path, monkeypatch, timeout, expected):
    elapsed, sleeps, reads, _response = _wait_poll_probe(monkeypatch)
    assert rw.wait_for_gateway_response(_paths(tmp_path), "wait", timeout) == {}
    assert sleeps == pytest.approx(expected)
    assert elapsed[0] == pytest.approx(max(0.0, timeout))
    assert len(reads) == len(expected) + 1


# LLM: 每次sleep后先读终态，出现即返回，不能为了凑满下一步或timeout继续等待。
# 函数用途: 验证退避途中终态抵达的早返回。
def test_wait_response_returns_on_intermediate_poll(tmp_path, monkeypatch):
    elapsed, sleeps, reads, response = _wait_poll_probe(monkeypatch, ready_after=3)
    assert rw.wait_for_gateway_response(_paths(tmp_path), "wait", 1.0) == response
    assert sleeps == pytest.approx([0.02, 0.05])
    assert elapsed[0] == pytest.approx(0.07) and len(reads) == 3


# LLM: 真实整数位数限制只在本测试临时设置且finally恢复；原锁读成功后再写不可解析大整数，只使缓存确认失败。
# 函数用途: 复现确认解析的普通ValueError不得破坏已经成功的权威读取。
def test_pending_confirmation_value_error_keeps_live_payload(tmp_path, monkeypatch, pending_scan_cache):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "value-error", "owner-a")
    locked = gateway_io._locked_json_path
    previous_limit = sys.get_int_max_str_digits()
    limit = sys.int_info.str_digits_check_threshold

    # LLM: 不替换reader身份；原读取退出后才换字节，确保错误来自新增的确认解析而非权威读。
    # 函数用途: 在确认阶段制造普通ValueError的真实JSON输入。
    @contextmanager
    def rewrite_after_read(current, **kwargs):
        with locked(current, **kwargs):
            yield
        if current == path:
            path.write_bytes(b'{"value":' + b"9" * (limit + 1) + b"}")

    monkeypatch.setattr(gateway_io, "_locked_json_path", rewrite_after_read)
    failure, rows = None, []
    try:
        sys.set_int_max_str_digits(limit)
        try:
            rows = rw._iter_pending_requests(paths)
        except ValueError as exc:
            failure = exc
    finally:
        sys.set_int_max_str_digits(previous_limit)
    assert failure is None, f"optional cache confirmation escaped: {failure}"
    assert rows[0].payload["user_id"] == "owner-a"
    assert not pending_scan_cache


# LLM: 预读探针返回后若伴随.lock被并发替换为符号链接，热命中必须失效并交由权威reader按安全门拒绝。
# 函数用途: 验证_pending_scan_cache_key之后的伴随.lock替换不会返回陈旧缓存成功结果。
def test_pending_lock_symlink_replacement_after_key_probe_invalidates_hit(tmp_path, monkeypatch, pending_scan_cache):
    paths = _paths(tmp_path)
    path = _enqueue(paths, "lock-swap", "owner-a")
    assert rw._iter_pending_requests(paths)[0].payload["user_id"] == "owner-a"
    lock_path = path.with_name(f"{path.name}.lock")
    saved_lock = tmp_path / "saved.lock"
    original_key = rw._pending_scan_cache_key
    swapped = False

    def key_then_swap(candidate):
        nonlocal swapped
        res = original_key(candidate)
        if candidate == path and not swapped:
            swapped = True
            lock_path.rename(saved_lock)
            lock_path.symlink_to(saved_lock.name)
        return res

    monkeypatch.setattr(rw, "_pending_scan_cache_key", key_then_swap)
    assert rw._iter_pending_requests(paths)[0].payload == {}
    assert not pending_scan_cache
